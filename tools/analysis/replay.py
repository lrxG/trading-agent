# -*- coding: utf-8 -*-
"""回放引擎 v2 —— 四层分离架构（2026-10-27 重新设计，取代 step_replay v1）。

设计史：v1 照搬了旧系统 step.py 的交互式 CLI（init/next/apply/finish）。
用户裁定（2026-10-27）：**理解旧流程的设计理念后重新设计，不是照抄**。
本 v2 = 理念继承 + 形态重构。

旧系统 step.py 可取的理念（全部继承）：
  ① 信息隔离：决策者只见 ≤D 世界（build_world 物理保证，非决策者自觉）
  ② 真实约束：T+1 开盘成交、涨跌停拒单留痕、成本正式入账（params 真源）
  ③ 止损引擎兜底：−8% 机械卖单（防决策者逃避止损；与实盘止损参数不同层）
  ④ 边界纪律：回测产出 = 研究证据，禁直接修改生产规则（走 learning 归仓）

旧形态的缺陷（v2 抛弃）：
  ✗ 交互式步进是主接口 → 不可重复、跑不了全历史、产不出统计功效
  ✗ 反推只是事后单票查询 → 不进学习闭环
  ✗ 无版本钉扎 → V0/V1 对比无判据（执行器与数据一变，对比即失效）

v2 架构 = 四层分离 + 闭环：

  数据平面  World Feed   build_world(D)：≤D 冻结快照；FEED_VERSION 钉扎数据口径
  决策平面  Policy       decide(world) → orders[]。纯函数、确定性（并列用 code 决胜）：
                         · mech_v0         机械基线（环境档闸 × 封板动量），全历史秒级可重复
                         · mech_v0_nogate  同策略去环境闸 —— 专供 audit ② 做闸门反事实
                         · agent step 模式保留（init/next/apply/finish）＝检验"完整智能"
                           的工具，**降级为非主接口**
  执行平面  Executor     execute_pending / stoploss_scan：所有 policy 共用同一执行器
                         ⇒ V0/V1 对比时执行差异=0，差异只能来自策略本身
  反推平面  Audit        audit(BT) 三路反推：
                         ① 逐笔退出归因：实际退出 vs +5d/+10d 开盘替代退出 → 时机缺陷
                         ② 闸门反事实：nogate 有而 gate 没有的票（=被环境闸拦掉），
                            按真实行情核算，**按入场日环境档分组** = "环境闸错杀成本"的直接证据
                         ③ 实际交易按环境档分组 n/胜率/净期望/95%CI（n<20 → INSUFFICIENT）
                         → audit.json（研究证据，可接 hypothesize/verify 学习闭环）

  闭环：run(policy V0) → audit → 缺陷/证据 → （校准 or policy V1）→ run(V1) → compare
       compare 硬闸：feed/exec/capital 不一致 → 拒绝比较（不同世界比成绩 = 作弊）

  ★ 架构裁定（2026-10-27 用户）：一套硬风控、一套统一环境判断、策略自主选机会、
    一个执行结算闭环。闸门台账——每道闸必须证明独立增益足以抵消错失交易的代价，
    否则不设；audit/OOS/CI 是研究工具，不是实盘决策链上的额外闸门：

    层              闸门                          状态
    --------------------------------------------------------------
    ① 硬风控(执行器)  −8%止损/MAX_POS/T+1/涨跌停/成本  保留，不靠回测取消
    ② 环境层 C1      risk_budget 唯一入口：           候选规则 A/B/C 公平对照
                    pause_new 暂停新开仓，            验证中（退潮弱保护证据；
                    不判断个股好坏                    震荡无证据 ⇒ 不扩阈值）
    ③ 策略层 C2-C4   MIN_POOL 冰点 / 封板动量          策略本体；新增过滤须证增益
    ④ 研究(非交易闸)  audit 三路反推 / OOS / CI        保留为验证工具

用法：
  replay.py run --policy mech_v0 --start 2024-01-01 --end 2026-08-31 --capital 100000
  replay.py audit BT-xxx
  replay.py compare BT-A BT-B
  replay.py init/next/apply/finish/look/status/counterfactual   # agent 模式（非主接口）
  replay.py selftest

边界：所有产出落 data/research/replay/（X2 研究豁免区）；审计结论是证据不是参数，
      修改生产阈值必须走校准管线（独立训练数据 + 版本化），不得拿 OOS 反复调参。
"""
import csv
import io
import json
import os
import subprocess
import sys
import time
from collections import defaultdict

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
for _p in ("fs", "analysis"):
    if os.path.join(_TOOLS, _p) not in sys.path:
        sys.path.insert(0, os.path.join(_TOOLS, _p))

KL = os.path.join(_ROOT, "data", "raw", "bus", "klines.csv")
ENV_CSV = os.path.join(_ROOT, "data", "research", "env_recalc",
                       "env_metrics_recalc_full.csv")
# ★ 全量缓存文件（含 OOS 区间）：训练/OOS 边界由校准脚本按 date 切分，
#   禁用训练集文件承载跨边界数据（2026-10-27 修正：load_env 补算曾回写训练集
#   文件，把 09 月 OOS 数据混入 train 文件，破坏 OOS 独立保留纪律）。
BT_DIR = os.path.join(_ROOT, "data", "research", "replay")

NEAR_LIMIT_UP = 0.0965    # 开盘涨幅 ≥ 此值 ⇒ 视为近涨停开盘，买不进
NEAR_LIMIT_DN = -0.0935   # 开盘跌幅 ≤ 此值 ⇒ 视为近跌停开盘，卖不出
STOP_LOSS = -0.08         # 引擎强制止损线（旧系统同款；与实盘止损参数不同层）
TOP_MOVERS = 20           # 世界输出中的涨跌幅榜宽度
SEALED_PCT = 0.0955       # 封板判定：pct ≥ 此值 且 close ≥ high×0.997（统计等价口径，
                          #     与 env_recalc 同哲学：钝化除权/ST/精确涨停价噪音）
EXEC_VERSION = "replay-exec@2-accgate"   # 执行平面版本：变动即令历史对比失效
# ★ @2 变更（2026-10-27 整改 P1b）：账户级熔断进执行器——回撤≥params.l1(15%)
#   或 l2(25%) ⇒ 当日新开仓全部拦除（存量退出不受影响）；MAX_POS 统一读
#   params.max_holding_count（原硬编 4 与 params 3 冲突）。变体不得覆盖。
CI_MIN_N = 20             # 分组统计最小样本（对齐 verify_min_samples 语义）


def _feed_version():
    """数据平面版本钉扎：klines 行数 + 环境缓存行数（同机确定性）。"""
    def _n(p):
        try:
            with open(p, "rb") as f:
                return sum(1 for _ in f)
        except OSError:
            return -1
    return "kl:%d|env:%d" % (_n(KL), _n(ENV_CSV))


# ── 数据装载（窗口内 + 前收盘映射；环境档走 env_recalc 产物）────────────────
def load_window(start, end):
    """→ (prices{code:{date:(o,h,l,c)}}, names{}, days[窗口内升序], get_pc(code,date))。

    只加载 [start 前 14 自然日, end] 的行（prev_close 判定需要前一日）；
    get_pc 为惰性缓存的前收盘查询（全历史回放 O(n²) 排序优化为 O(n)）。
    """
    import datetime as _dt
    d0 = _dt.date(*map(int, start.split("-")))
    lo = (d0 - _dt.timedelta(days=14)).isoformat()
    prices, names, alld = defaultdict(dict), {}, set()
    amts = defaultdict(dict)
    with open(KL, encoding="utf-8", errors="ignore", newline="") as f:
        for r in csv.DictReader(f):
            c, d = r.get("code") or "", r.get("date") or ""
            if c.startswith("bj") or c.startswith(("sh000", "sz399")) or not (lo <= d <= end):
                continue
            try:
                o, h, l, cl = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
            except (ValueError, TypeError):
                continue
            if cl <= 0:
                continue
            prices[c][d] = (o, h, l, cl)
            try:
                amts[c][d] = float(r.get("amount") or 0)
            except (ValueError, TypeError):
                amts[c][d] = 0.0
            if r.get("name"):
                names[c] = r["name"]
            if d >= start:
                alld.add(d)
    days = sorted(alld)

    ctx_cache = {}

    def get_ctx(c, d):
        """→ (pc1, pc2, pc20, prev_open, prev_close) 或 None（惰性逐码构建）。"""
        m = ctx_cache.get(c)
        if m is None:
            ds = sorted(prices.get(c, {}))
            m = {}
            px = prices[c]
            for i, dd in enumerate(ds):
                m[dd] = (
                    px[ds[i - 1]][3] if i >= 1 else None,       # pc1 前收
                    px[ds[i - 2]][3] if i >= 2 else None,       # pc2 前前收（=D-1 收）
                    px[ds[i - 3]][3] if i >= 3 else None,       # pc3（=D-2 收）
                    px[ds[i - 20]][3] if i >= 20 else None,     # pc20（20 日回溯）
                    px[ds[i - 1]][0] if i >= 1 else None,       # 前日开盘
                    px[ds[i - 1]][1] if i >= 1 else None,       # 前日最高
                )
            ctx_cache[c] = m
        return m.get(d)

    def get_pc(c, d):
        t = get_ctx(c, d)
        return t[0] if t else None

    amt_cache = {}

    def get_amt(c, d):
        m = amt_cache.get(c)
        if m is None:
            m = amts.get(c, {})
            amt_cache[c] = m
        return m.get(d)

    return prices, names, days, get_pc, get_amt, get_ctx


def load_env(start, end):
    """→ {date: {limit_up_count, max_board, fail_rate, up_down_ratio, grade}}。

    优先读 env_recalc 产物 CSV；缺失窗口 ⇒ 现场补算（调 env_recalc）并补写 CSV。
    档位用 regime 种子档判定并标注 UNCALIBRATED。
    """
    import regime as RG
    env = {}
    if os.path.isfile(ENV_CSV):
        with io.open(ENV_CSV, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                env[r["date"]] = r
    need = [d for d in [start, end] if d]
    if any(d not in env for d in need):
        import env_recalc as ER
        m = ER.recalc(*ER.load_klines())
        ER.write_csv(m, ENV_CSV)               # 写全量缓存文件（非训练集文件）
        env = {**{d: {"date": d, **v} for d, v in m.items()}, **env}
    for d, r in env.items():
        if "grade" not in r:
            try:
                sig = {"limit_up_count": _f(r.get("limit_up_count")),
                       "max_board": _f(r.get("max_board")),
                       "fail_rate": _f(r.get("fail_rate"))}
                r["grade"] = RG.regime(sig)
            except Exception:                                 # noqa: BLE001
                r["grade"] = None
            r["threshold_status"] = "UNCALIBRATED_SEED"
    return {d: r for d, r in env.items() if start <= d <= end}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── 状态持久化 ─────────────────────────────────────────────────────────────
def bt_dir(btid):
    return os.path.join(BT_DIR, btid)


def state_path(btid):
    return os.path.join(bt_dir(btid), "state.json")


def load_state(btid):
    p = state_path(btid)
    if not os.path.isfile(p):
        raise SystemExit("⛔ 未找到实验 %s（%s）" % (btid, p))
    return json.load(io.open(p, encoding="utf-8"))


def write_result(btid, doc):
    """期末归档 result.json（唯一写点，静态不可解析 ⇒ landing 登记）。"""
    out = os.path.join(bt_dir(btid), "result.json")
    tmp = out + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, out)
    return out


def write_audit(btid, doc):
    """反推平面归档 audit.json（唯一写点，静态不可解析 ⇒ landing 登记）。"""
    out = os.path.join(bt_dir(btid), "audit.json")
    tmp = out + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, out)
    return out


def save_state(btid, st):
    p = state_path(btid)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


# ── 执行平面（所有 policy 共用；版本 EXEC_VERSION）─────────────────────────
def execute_pending(st, prices, names, buy_rate, sell_rate, d, get_pc=None):
    """在 d 开盘执行昨日盘后登记的订单 → (成交/拒单事件列表)。

    拒单规则（留痕不静默）：近涨停开盘买不进；近跌停开盘卖不出；
    重复信号（已持有或已有在途买单）拒单 —— 引擎层兜底，policy 不必自觉。
    买单 shares 由引擎按开盘价整百股折算（weight × capital / open）。
    """
    events = []
    keep = []
    held_codes = {h["code"] for h in st.get("holdings", [])}
    for od in st.get("pending_orders", []):
        c = od.get("code")
        px = prices.get(c, {}).get(d)
        pc = get_pc(c, d) if get_pc else None
        if pc is None:
            d_prev = _prev_day(prices, c, d)
            pc = prices[c][d_prev][3] if d_prev else None
        if not px or pc is None:
            keep.append(od)
            events.append({"d": d, "kind": "reject", "order": od,
                           "why": "当日无行情/无前收（停牌？）→ 订单顺延"})
            continue
        o, h, l, cl = px
        chg = o / pc - 1
        if od["action"] == "buy":
            if c in held_codes or any(x.get("action") == "buy" and x.get("code") == c
                                      for x in keep):
                events.append({"d": d, "kind": "reject", "order": od,
                               "why": "重复信号（已持有或在途）"})
                continue
            cond = od.get("cond") or {}
            if cond and not (cond.get("gap_min", -9) <= chg <= cond.get("gap_max", 9)):
                # 条件单不满足 = 撤单留痕（不顺延）——预案触发区的回测实现
                events.append({"d": d, "kind": "cancel", "order": od,
                               "why": "开盘缺口 %+.2f%% 不在触发区 [%.1f%%, %.1f%%] → 条件单撤"
                                      % (chg * 100, cond.get("gap_min", 0) * 100,
                                         cond.get("gap_max", 0) * 100)})
                continue
            if chg >= NEAR_LIMIT_UP:
                events.append({"d": d, "kind": "reject", "order": od,
                               "why": "近涨停开盘（%+.2f%%）买不进" % (chg * 100)})
                continue
            weight = od.get("weight") or 0
            budget = min(weight * st["capital"], st["cash"])
            shares = int(budget / (o * (1 + buy_rate)) / 100) * 100
            if shares <= 0:
                events.append({"d": d, "kind": "reject", "order": od,
                               "why": "资金不足 1 手（预算 %.0f / 开盘 %.2f）" % (budget, o)})
                continue
            cost = shares * o * (1 + buy_rate)
            st["cash"] -= cost
            held_codes.add(c)
            st["holdings"].append({"code": c, "name": names.get(c, ""),
                                   "shares": shares, "entry_price": o,
                                   "entry_date": d, "cost": cost,
                                   "entry_low": l,          # 入场日最低=证伪线锚
                                   "reason": od.get("reason", ""),
                                   "kind": od.get("kind", "agent")})
            events.append({"d": d, "kind": "fill", "action": "buy", "code": c,
                           "shares": shares, "price": o, "cost": round(cost, 2)})
        elif od["action"] == "sell":
            held = next((x for x in st["holdings"] if x["code"] == c), None)
            if not held:
                events.append({"d": d, "kind": "reject", "order": od,
                               "why": "无持仓"})
                continue
            if chg <= NEAR_LIMIT_DN:
                events.append({"d": d, "kind": "reject", "order": od,
                               "why": "近跌停开盘（%+.2f%%）卖不出 → 顺延" % (chg * 100)})
                keep.append(od)
                continue
            proceeds = held["shares"] * o * (1 - sell_rate)
            st["cash"] += proceeds
            held_codes.discard(c)
            st["holdings"].remove(held)
            st["trades"].append({
                "code": c, "name": held["name"], "shares": held["shares"],
                "entry_date": held["entry_date"], "entry_price": held["entry_price"],
                "exit_date": d, "exit_price": o,
                "pnl": round(proceeds - held["cost"], 2),
                "ret": round(proceeds / held["cost"] - 1, 6),
                "kind": held.get("kind", "agent"),
                "reason_in": held.get("reason", ""), "reason_out": od.get("reason", "")})
            events.append({"d": d, "kind": "fill", "action": "sell", "code": c,
                           "shares": held["shares"], "price": o,
                           "pnl": round(proceeds - held["cost"], 2)})
    st["pending_orders"] = keep
    return events


def _prev_day(prices, code, d):
    ds = sorted(prices.get(code, {}))
    lower = [x for x in ds if x < d]
    return lower[-1] if lower else None


def account_halt(equity_curve):
    """账户级熔断判定（P0 统一风控入口的回测侧实现）。

    权益曲线自峰回撤 ≥ params.circuit_breaker_drawdown_l1(15%) 或 l2(25%)
    ⇒ True（当日禁新开仓）。存量持仓的止损/目标退出**不拦**——
    「禁新开仓」与「存量风险管理」分离（整改令原文）。
    强制平仓不在模拟内执行（保守方向：真实账户 L2 是空仓 N 日，此处
    只封新开仓 ⇒ 回测结果不会比真实执行更乐观的通道已封，但存量尾部
    风险仍在策略自身退出规则内）。
    """
    if not equity_curve or len(equity_curve) < 2:
        return False, None
    l1 = _rate("circuit_breaker_drawdown_l1", 15.0)
    l2 = _rate("circuit_breaker_drawdown_l2", 25.0)
    peak = max(x[1] for x in equity_curve)
    last = equity_curve[-1][1]
    dd = (peak - last) / peak * 100.0 if peak > 0 else 0.0
    return (dd >= l1 or dd >= l2), round(dd, 2)


def stoploss_scan(st, prices, d_prev):
    """按 d_prev 收盘检查持仓浮亏 ≤−8% → 生成 kind=stoploss 次日开盘卖单。"""
    added = []
    for h in st.get("holdings", []):
        px = prices.get(h["code"], {}).get(d_prev)
        if not px:
            continue
        ret = px[3] / h["entry_price"] - 1
        _sl = st.get("stop_loss") or STOP_LOSS
        if ret <= _sl and not any(
                o.get("code") == h["code"] and o.get("kind") == "stoploss"
                for o in st.get("pending_orders", [])):
            st.setdefault("pending_orders", []).append({
                "action": "sell", "code": h["code"], "kind": "stoploss",
                "reason": "止损兜底：浮亏 %.1f%% ≤ %.0f%%（引擎强制，非决策）"
                          % (ret * 100, (_sl) * 100),
                "decided_at": d_prev})
            added.append(h["code"])
    return added


def force_close(st, prices, sell_rate):
    """期末按末日收盘强平（所有 policy / agent 模式共用）。"""
    last = st["days"][-1]
    for h in list(st["holdings"]):
        px = prices.get(h["code"], {}).get(last)
        cl = px[3] if px else h["entry_price"]
        proceeds = h["shares"] * cl * (1 - sell_rate)
        st["cash"] += proceeds
        st["holdings"].remove(h)
        st["trades"].append({
            "code": h["code"], "name": h["name"], "shares": h["shares"],
            "entry_date": h["entry_date"], "entry_price": h["entry_price"],
            "exit_date": last, "exit_price": cl,
            "pnl": round(proceeds - h["cost"], 2),
            "ret": round(proceeds / h["cost"] - 1, 6),
            "kind": "force_close", "reason_in": h.get("reason", ""),
            "reason_out": "期末强平"})


# ── 数据平面：世界构造（物理隔离未来）──────────────────────────────────────
def build_world(st, prices, names, env, d, get_pc=None, get_amt=None, get_ctx=None,
                top=TOP_MOVERS):
    """构造 D 日决策世界。**只读 days[:cursor+1]**——任何 >D 数据不进本函数。

    limit_up_pool 用统计等价封板口径（pct≥9.55% 且 close≥high×0.997），
    与 env_recalc 同哲学：钝化除权/精确涨停价噪音。
    """
    day_px = {}
    for c, m in prices.items():
        if d in m:
            o, h, l, cl = m[d]
            pc = get_pc(c, d) if get_pc else None
            if pc is None:
                d_prev = _prev_day(prices, c, d)
                pc = m[d_prev][3] if d_prev else None
            ctx = get_ctx(c, d) if get_ctx else None
            pc2 = ctx[1] if ctx else None          # = D-1 收盘
            pc3 = ctx[2] if ctx else None          # = D-2 收盘
            pc20 = ctx[3] if ctx else None
            day_px[c] = {"o": o, "h": h, "l": l, "c": cl,
                         "pct": round(cl / pc - 1, 6) if pc else None,
                         "pc": pc, "amt": (get_amt(c, d) if get_amt else None),
                         "pc2": pc2, "pc20": pc20,
                         "r20": round(cl / pc20 - 1, 6) if pc20 else None,
                         "dn1": round(pc2 and cl / pc2 - 1, 6) if pc2 else None,   # D-1 日涨幅
                         "dn2": round(pc3 and pc2 / pc3 - 1, 6) if (pc3 and pc2) else None,  # D-2 日涨幅
                         "o1_gap": round((ctx[4] and pc2) and ctx[4] / pc2 - 1, 6)
                                   if (ctx and ctx[4] and pc2) else None}   # D-1 低开缺口
    ups = sum(1 for v in day_px.values() if (v["pct"] or 0) > 0)
    dns = sum(1 for v in day_px.values() if (v["pct"] or 0) < 0)
    ranked = sorted((c, v) for c, v in day_px.items() if v["pct"] is not None)
    # 并列确定性：pct 同值用 code 决胜（stable sort 的插入序依赖文件序 ⇒ 显式决胜）
    gainers = [{"code": c, "pct": v["pct"], "c": v["c"]}
               for c, v in sorted(ranked, key=lambda x: (-x[1]["pct"], x[0]))[:top]]
    losers = [{"code": c, "pct": v["pct"], "c": v["c"]}
              for c, v in sorted(ranked, key=lambda x: (x[1]["pct"], x[0]))[:top]]
    def _amt(c):
        a = day_px[c].get("amt")
        return a if a is not None else 0.0
    sealed = [c for c, v in day_px.items()
              if v["pct"] is not None and v["pct"] >= SEALED_PCT
              and v["c"] >= v["h"] * 0.997]
    pool = sorted(sealed, key=lambda c: (-_amt(c), c))
    # 炸板池：触板（最高价涨幅≥阈值）但未封死 —— 弱转强战法候选源（战法调研 2026-10-27）
    broken = sorted((c for c, v in day_px.items()
                     if v.get("pc") and v["h"] / v["pc"] - 1 >= SEALED_PCT
                     and c not in set(sealed)),
                    key=lambda c: (-_amt(c), c))
    holdings = []
    for h in st["holdings"]:
        px = day_px.get(h["code"], {})
        try:
            held_n = st["cursor"] - st["days"].index(h["entry_date"])
        except ValueError:
            held_n = None
        holdings.append({**{k: h[k] for k in
                            ("code", "name", "shares", "entry_price", "entry_date",
                             "entry_low")},
                         "held_days": held_n,
                         "last": px.get("c"),
                         "ret": round(px["c"] / h["entry_price"] - 1, 6) if px.get("c") else None})
    ev = env.get(d, {})
    return {
        "date": d, "cursor": st["cursor"], "days_remaining": len(st["days"]) - 1 - st["cursor"],
        "env": {"limit_up_count": _f(ev.get("limit_up_count")),
                "max_board": _f(ev.get("max_board")),
                "fail_rate": _f(ev.get("fail_rate")),
                "up_down_ratio": _f(ev.get("up_down_ratio")),
                "grade": ev.get("grade"),
                "threshold_status": ev.get("threshold_status", "UNCALIBRATED_SEED"),
                "note": "env_recalc 口径（校准训练集同源）；阈值 UNCALIBRATED 种子，非冻结版"},
        "breadth": {"up": ups, "down": dns, "n": len(day_px)},
        "limit_up_pool": pool,
        "broken_pool": broken,
        "pool_amt": {c: day_px[c].get("amt") for c in pool},
        "day_px": {c: {k: v[k] for k in ("pct", "r20", "dn1", "dn2", "o1_gap",
                                         "amt") if v.get(k) is not None}
                   for c, v in day_px.items()},
        "_names": names,
        "gainers_top": gainers, "losers_top": losers,
        "holdings": holdings, "cash": round(st["cash"], 2),
        "pending_orders": st.get("pending_orders", []),
        "capital": st["capital"],
        "last_events": st.get("last_events", []),
        "instruction": ("盘后决策：基于以上 ≤%s 信息给出决策 JSON，"
                        "用 apply 提交；成交在次一交易日开盘。" % d),
    }


# ── 决策平面：Policy（纯函数、确定性）──────────────────────────────────────
TARGET_RET = 0.15         # mech_v0 目标收益
HOLD_DAYS = 5             # mech_v0 最长持有交易日
def _max_pos():
    """账户硬风控：最大持仓数（params 唯一权威，原硬编 4 与 params 3 冲突）。"""
    v = _rate("max_holding_count", 4)
    return int(v) if v else 4


MAX_POS = 4               # ↓ 已由 _max_pos() 接管（保留常量名防外部引用断）
WEIGHT = 0.25             # 单笔权重
MIN_POOL = 5              # 封板池最少家数（冰点不开仓）
# ── C1 环境层：风险预算唯一入口 ────────────────────────────────────────────
# 环境层不替策略判断个股好坏，只回答一个问题：当前系统允许承担多大风险。
# 暂停名单=该档暂停新开仓（存量持仓退出不受影响，退出永远策略自主+止损兜底）。
# 三变体 = 闸门公平对照（同一世界/同一执行器，compare 直接比）：
#   A mech_v0          退潮+震荡都停（现状基线）
#   B mech_v0_nozh     仅退潮停   ⇒ A vs B 回答「震荡闸有没有独立增益」
#   C mech_v0_nopause  全开       ⇒ B vs C 回答「退潮闸有没有独立增益」
# 阈值不再细分（用户裁定：参数越多越容易过拟合），校准 v2 前维持三档粗糙粒度。
PAUSE_V0 = ("退潮", "震荡")
PAUSE_NOZH = ("退潮",)
PAUSE_NONE = ()


def risk_budget(env_row, pauses):
    """C1 统一输出：{pause_new, grade}。环境层唯一对外接口。"""
    g = (env_row or {}).get("grade")
    return {"pause_new": g in pauses, "grade": g}


def _mech_v0(w, pauses):
    """mech_v0 机械基线策略。只读 world，无随机、无外部状态 ⇒ 全历史可重复。

    开仓：C1 风险预算未暂停 且 封板池 ≥ MIN_POOL 家
          → 买封板池前 2 只（code 序决胜），各 25%，最多 4 仓；
          T+1 近涨停开盘由执行器拒单（现实约束，audit ② 据此核算闸门+执行双重成本）。
    平仓：+15% 目标 / 持有 ≥5 交易日 / −8% 引擎止损兜底（与环境无关，策略自主）。
    """
    orders = []
    for h in w["holdings"]:
        if (h["ret"] or 0) >= TARGET_RET:
            orders.append({"action": "sell", "code": h["code"],
                           "reason": "目标 +%.0f%% 达成（ret %.1f%%）"
                                     % (TARGET_RET * 100, (h["ret"] or 0) * 100)})
        elif h.get("held_days") is not None and h["held_days"] >= HOLD_DAYS:
            orders.append({"action": "sell", "code": h["code"],
                           "reason": "持有 ≥%d 交易日到期" % HOLD_DAYS})
    g = (w.get("env") or {}).get("grade")
    if risk_budget(w.get("env"), pauses)["pause_new"]:
        return orders
    if len(w.get("limit_up_pool") or []) < MIN_POOL:
        return orders
    held = {h["code"] for h in w["holdings"]}
    pend = {o.get("code") for o in w.get("pending_orders", []) if o.get("action") == "buy"}
    room = MAX_POS - len(held)
    for c in sorted(w["limit_up_pool"]):        # 显式 code 序（V0 历史语义不变）
        if room <= 0:
            break
        if c in held or c in pend:
            continue
        orders.append({"action": "buy", "code": c, "weight": WEIGHT,
                       "reason": "mech_v0：D 日封板（池 %d 家，档 %s）"
                                 % (len(w["limit_up_pool"]), g)})
        pend.add(c)
        room -= 1
    return orders


def policy_mech_v0(w):
    return _mech_v0(w, PAUSE_V0)


def _mech_v1(w, pauses, cond, target, hold):
    """V1 低吸变体：候选与 V0 同源（D 日封板池），但**次日开盘缺口条件单**——
    只在触发区内成交（追高禁），目标/持有期收紧。与 V0 唯一差异=入场结构，
    同一世界/同一执行器 ⇒ compare 可归因。"""
    orders = []
    for h in w["holdings"]:
        if (h["ret"] or 0) >= target:
            orders.append({"action": "sell", "code": h["code"],
                           "reason": "目标 +%.0f%% 达成" % (target * 100)})
        elif h.get("held_days") is not None and h["held_days"] >= hold:
            orders.append({"action": "sell", "code": h["code"],
                           "reason": "持有 ≥%d 交易日到期" % hold})
    g = (w.get("env") or {}).get("grade")
    if risk_budget(w.get("env"), pauses)["pause_new"]:
        return orders
    if len(w.get("limit_up_pool") or []) < MIN_POOL:
        return orders
    held = {h["code"] for h in w["holdings"]}
    pend = {o.get("code") for o in w.get("pending_orders", []) if o.get("action") == "buy"}
    room = MAX_POS - len(held)
    for c in sorted(w["limit_up_pool"]):        # 显式 code 序（V1 历史语义不变）
        if room <= 0:
            break
        if c in held or c in pend:
            continue
        orders.append({"action": "buy", "code": c, "weight": WEIGHT, "cond": cond,
                       "reason": "mech_v1：D 日封板，次日开盘缺口 [%.1f%%,%.1f%%] 才接"
                                 % (cond["gap_min"] * 100, cond["gap_max"] * 100)})
        pend.add(c)
        room -= 1
    return orders


def policy_mech_v1(w):
    return _mech_v1(w, PAUSE_V0, {"gap_min": -0.05, "gap_max": 0.02}, 0.10, 3)


def policy_mech_v1_tight(w):
    return _mech_v1(w, PAUSE_V0, {"gap_min": -0.03, "gap_max": 0.0}, 0.08, 2)


def _mech_v2(w, pauses, cond, target, hold):
    """V2 = V1 + 证伪线退出：入场日最低价被收盘跌破 ⇒ 次日开盘走。

    归因依据（BT-mech_v1 全历史）：25 笔「持有到期」均值 −1.79% 是出血主力——
    阴跌票既没到目标也没触发 −8%，白拿 3 天。预案证伪线（跌破入场日低点=分时
    结构坏）直接进策略 ⇒ 预案与回测同构。
    """
    orders = []
    for h in w["holdings"]:
        if (h["ret"] or 0) >= target:
            orders.append({"action": "sell", "code": h["code"],
                           "reason": "目标 +%.0f%% 达成" % (target * 100)})
        elif h.get("entry_low") and h.get("last") and h["last"] <= h["entry_low"]:
            orders.append({"action": "sell", "code": h["code"],
                           "reason": "证伪线破位（收盘 %.2f ≤ 入场日低点 %.2f）"
                                     % (h["last"], h["entry_low"])})
        elif h.get("held_days") is not None and h["held_days"] >= hold:
            orders.append({"action": "sell", "code": h["code"],
                           "reason": "持有 ≥%d 交易日到期" % hold})
    g = (w.get("env") or {}).get("grade")
    if risk_budget(w.get("env"), pauses)["pause_new"]:
        return orders
    if len(w.get("limit_up_pool") or []) < MIN_POOL:
        return orders
    held = {h["code"] for h in w["holdings"]}
    pend = {o.get("code") for o in w.get("pending_orders", []) if o.get("action") == "buy"}
    room = MAX_POS - len(held)
    for c in w["limit_up_pool"]:
        if room <= 0:
            break
        if c in held or c in pend:
            continue
        orders.append({"action": "buy", "code": c, "weight": WEIGHT, "cond": cond,
                       "reason": "mech_v2：D 日封板，缺口 [%.1f%%,%.1f%%] 接，破入场日低点走"
                                 % (cond["gap_min"] * 100, cond["gap_max"] * 100)})
        pend.add(c)
        room -= 1
    return orders


def policy_mech_v2(w):
    return _mech_v2(w, PAUSE_V0, {"gap_min": -0.05, "gap_max": 0.02}, 0.10, 3)


# ── V4~V7 战法变体（来源：2026-10-27 网络战法调研，逐条可测）──────────────────
def _pick_buy(orders, w, cond, reason, picks=None, mainboard_only=False):
    held = {h["code"] for h in w["holdings"]}
    pend = {o.get("code") for o in w.get("pending_orders", []) if o.get("action") == "buy"}
    cands = list(w["limit_up_pool"])            # pool 已按成交额降序（世界层）
    if picks == "code":
        cands = sorted(cands)
    if mainboard_only:
        cands = [c for c in cands if not c.startswith(("sz30", "sh68"))]
    room = MAX_POS - len(held)
    for c in cands:
        if room <= 0:
            break
        if c in held or c in pend:
            continue
        orders.append({"action": "buy", "code": c, "weight": WEIGHT, "cond": cond,
                       "reason": reason % c})
        pend.add(c)
        room -= 1
    return orders


def policy_mech_v4a(w):
    """V4a 资金共识排序（战法：首板看封单——成交额最大=资金分歧后共识最强）。"""
    orders = []
    for h in w["holdings"]:
        if (h["ret"] or 0) >= 0.10:
            orders.append({"action": "sell", "code": h["code"], "reason": "目标 +10% 达成"})
        elif h.get("held_days") is not None and h["held_days"] >= 3:
            orders.append({"action": "sell", "code": h["code"], "reason": "持有 ≥3 交易日到期"})
    if risk_budget(w.get("env"), PAUSE_V0)["pause_new"]:
        return orders
    if len(w.get("limit_up_pool") or []) < MIN_POOL:
        return orders
    return _pick_buy(orders, w, {"gap_min": -0.05, "gap_max": 0.02},
                     "mech_v4a：封板池资金共识榜 %s（成交额降序）", picks="amt")


def policy_mech_v5(w):
    """V5 主板首板（战法：首板看封单=10cm 体系；20cm 创业科创波动结构不同，剔除）。"""
    orders = []
    for h in w["holdings"]:
        if (h["ret"] or 0) >= 0.10:
            orders.append({"action": "sell", "code": h["code"], "reason": "目标 +10% 达成"})
        elif h.get("held_days") is not None and h["held_days"] >= 3:
            orders.append({"action": "sell", "code": h["code"], "reason": "持有 ≥3 交易日到期"})
    if risk_budget(w.get("env"), PAUSE_V0)["pause_new"]:
        return orders
    if len(w.get("limit_up_pool") or []) < MIN_POOL:
        return orders
    return _pick_buy(orders, w, {"gap_min": -0.05, "gap_max": 0.02},
                     "mech_v5：主板封板 %s（20cm 剔除）", picks="code", mainboard_only=True)


def policy_mech_v6(w):
    """V6 弱转强（战法调研明确参数：昨日断板烂板 + 次日高开 2~5% → 点火买）。

    候选=D 日炸板池（broken_pool），条件单 gap [2%,5%]（与调研的竞价参数一致），
    目标 +10%，持有 3 日，C1 暂停名单同 V0。
    """
    orders = []
    for h in w["holdings"]:
        if (h["ret"] or 0) >= 0.10:
            orders.append({"action": "sell", "code": h["code"], "reason": "目标 +10% 达成"})
        elif h.get("held_days") is not None and h["held_days"] >= 3:
            orders.append({"action": "sell", "code": h["code"], "reason": "持有 ≥3 交易日到期"})
    if risk_budget(w.get("env"), PAUSE_V0)["pause_new"]:
        return orders
    if len(w.get("broken_pool") or []) < 2:
        return orders
    held = {h["code"] for h in w["holdings"]}
    pend = {o.get("code") for o in w.get("pending_orders", []) if o.get("action") == "buy"}
    room = MAX_POS - len(held)
    for c in w["broken_pool"]:
        if room <= 0:
            break
        if c in held or c in pend:
            continue
        orders.append({"action": "buy", "code": c, "weight": WEIGHT,
                       "cond": {"gap_min": 0.02, "gap_max": 0.05},
                       "reason": "mech_v6：弱转强——D 日炸板 %s，次日高开 2~5%% 点火" % c})
        pend.add(c)
        room -= 1
    return orders


def policy_mech_v6b(w):
    """V6b = V6 但仅退潮停（震荡也开）——隔离「环境闸收紧」对弱转强的贡献。"""
    orders = _wzw_core(w, PAUSE_NOZH, {"gap_min": 0.02, "gap_max": 0.05})
    return orders


def policy_mech_v6c(w):
    """V6c = V6 但缺口放宽 [0%,6%]（隔离触发区宽度贡献）。"""
    orders = _wzw_core(w, PAUSE_V0, {"gap_min": 0.0, "gap_max": 0.06})
    return orders


def _wzw_core(w, pauses, cond):
    """弱转强核心逻辑（V6 家族共用）：D 日炸板池 + 次日缺口条件单。"""
    orders = []
    for h in w["holdings"]:
        if (h["ret"] or 0) >= 0.10:
            orders.append({"action": "sell", "code": h["code"], "reason": "目标 +10% 达成"})
        elif h.get("held_days") is not None and h["held_days"] >= 3:
            orders.append({"action": "sell", "code": h["code"], "reason": "持有 ≥3 交易日到期"})
    if risk_budget(w.get("env"), pauses)["pause_new"]:
        return orders
    if len(w.get("broken_pool") or []) < 2:
        return orders
    held = {h["code"] for h in w["holdings"]}
    pend = {o.get("code") for o in w.get("pending_orders", []) if o.get("action") == "buy"}
    room = MAX_POS - len(held)
    for c in w["broken_pool"]:
        if room <= 0:
            break
        if c in held or c in pend:
            continue
        orders.append({"action": "buy", "code": c, "weight": WEIGHT, "cond": cond,
                       "reason": "弱转强：D 日炸板 %s，次日缺口 [%.0f%%,%.0f%%] 点火"
                                 % (c, cond["gap_min"] * 100, cond["gap_max"] * 100)})
        pend.add(c)
        room -= 1
    return orders


# ── V8/V9 逆势族（行情矩阵的退潮/冰点补位；2026-10-27 战法调研）────────────────
# 注意：这两族**不吃 C1 档位闸**（它们的入场条件自带环境判据：
#   V8 反弹日闸=涨家占比≥65%（旧系统 659 天实证）；V9 自带恐慌前置条件），
#   架构上记为「策略自带环境判据」，与 C1 pause 并列于闸门台账。

REB_BREADTH = 0.65          # 反弹日闸：上涨家数占比（旧 oversold_scan 原值）
DEEP_FALL = -0.35           # 深超跌：20 日收益 ≤ −35%
REB_DAY_PCT = 0.03          # 反弹日个股当日涨幅 ≥ 3%


def _exits_std(w, target, hold):
    orders = []
    for h in w["holdings"]:
        if (h["ret"] or 0) >= target:
            orders.append({"action": "sell", "code": h["code"],
                           "reason": "目标 +%.0f%% 达成" % (target * 100)})
        elif h.get("held_days") is not None and h["held_days"] >= hold:
            orders.append({"action": "sell", "code": h["code"],
                           "reason": "持有 ≥%d 交易日到期" % hold})
    return orders


def _entries(w, cands, cond, reason, room_max=MAX_POS, weight=WEIGHT):
    held = {h["code"] for h in w["holdings"]}
    pend = {o.get("code") for o in w.get("pending_orders", []) if o.get("action") == "buy"}
    room = room_max - len(held)
    for c in cands:
        if room <= 0:
            break
        if c in held or c in pend:
            continue
        orders_d = {"action": "buy", "code": c, "weight": weight, "cond": cond,
                    "reason": reason % c}
        orders_d["_out"] = True
        yield orders_d


def policy_mech_v8(w):
    """V8 深超跌×普涨反弹（旧系统 oversold_scan 659 天实证口径）：
    r20 ≤ −35% 且当日涨 ≥3%，且反弹日闸（涨家占比 ≥65%）。
    T+1 开盘买（深低开 ≤−2% 弃），目标 +10%，持有 5 日。
    """
    orders = _exits_std(w, 0.10, 5)
    br = w.get("breadth") or {}
    n = br.get("n") or 0
    if n and (br.get("up") or 0) / n < REB_BREADTH:
        return orders                                   # 反弹日闸：非普涨日不开
    day_px = w.get("day_px") or {}
    cands = sorted(c for c, v in day_px.items()
                   if v.get("r20") is not None and v["r20"] <= DEEP_FALL
                   and (v.get("pct") or 0) >= REB_DAY_PCT
                   and not (v["code"] if False else c).startswith("sh68"))
    cands = [c for c in cands if "ST" not in (w.get("_names") or {}).get(c, "")]
    for od in _entries(w, cands[:2] if False else cands, {"gap_min": -0.02},
                       "mech_v8：深超跌反弹 %s（r20≤-35%% 且当日涨≥3%%，普涨日）"):
        od.pop("_out", None)
        orders.append(od)
    return orders


def policy_mech_v9(w):
    """V9 暴跌次日反弹（三板斧 84,712 笔 53.2% 胜率口径，去 ST）：
    D 日涨>4% 且（前日跌>4% 且前日低开>4%，或前两日累计跌>4.9%）。
    次日低开 ≤−1.5% 弃（调研止损建议），目标 +6%，持有 3 日。
    """
    orders = _exits_std(w, 0.06, 3)
    day_px = w.get("day_px") or {}
    names = w.get("_names") or {}
    cands = []
    for c, v in day_px.items():
        pct = v.get("pct") or 0
        if pct < 0.04:
            continue
        if "ST" in names.get(c, ""):
            continue
        dn1 = v.get("dn1")              # D-1 日涨幅
        o1g = v.get("o1_gap")           # D-1 低开缺口（开盘/前收）
        dn2 = v.get("dn2")              # D-2 日涨幅
        # XG1：昨日跌超 4% 且昨日低开超 4%，今日涨超 4%
        condA = (dn1 is not None and dn1 <= -0.04
                 and o1g is not None and o1g <= -0.04)
        # XG2：前两日累计跌超 4.9%，今日涨超 4%
        condB = ((dn1 is not None and dn2 is not None)
                 and (dn1 + dn2) <= -0.049)
        if condA or condB:
            cands.append(c)
    cands = sorted(cands)
    for od in _entries(w, cands[:2] if False else cands, {"gap_min": -0.015},
                       "mech_v9：暴跌次日反弹 %s（D日涨>4%% 前置恐慌）"):
        od.pop("_out", None)
        orders.append(od)
    return orders


POLICY_STOP = {"mech_v7": -0.05}             # V7：单笔 −5% 无条件砍（调研铁律）vs 引擎 −8%


def policy_mech_v7(w):
    """V7 = V1 但止损 −5%（陈小群铁律：单笔亏损超 5% 无条件砍仓）。"""
    return _mech_v1(w, PAUSE_V0, {"gap_min": -0.05, "gap_max": 0.02}, 0.10, 3)


def policy_mech_v3(w):
    """V3 = V1 但持有到期仅限弱势：持有≥3日且收盘<入场价才走。

    依据：V1 的 25 笔到期单均值 −1.79%（阴跌拖累）；V2 教训=对封板股硬性
    结构退出会被洗盘打脸。只对「到期且已亏损」的票执行时间止损，横盘强势票
    多给一天（同世界同执行器，compare 归因）。"""
    orders = []
    for h in w["holdings"]:
        if (h["ret"] or 0) >= 0.10:
            orders.append({"action": "sell", "code": h["code"],
                           "reason": "目标 +10%% 达成"})
        elif (h.get("held_days") is not None and h["held_days"] >= 3
              and (h.get("ret") or 0) < 0):
            orders.append({"action": "sell", "code": h["code"],
                           "reason": "持有≥3日且已亏损（时间止损）"})
    g = (w.get("env") or {}).get("grade")
    if risk_budget(w.get("env"), PAUSE_V0)["pause_new"]:
        return orders
    if len(w.get("limit_up_pool") or []) < MIN_POOL:
        return orders
    held = {h["code"] for h in w["holdings"]}
    pend = {o.get("code") for o in w.get("pending_orders", []) if o.get("action") == "buy"}
    room = MAX_POS - len(held)
    for c in w["limit_up_pool"]:
        if room <= 0:
            break
        if c in held or c in pend:
            continue
        orders.append({"action": "buy", "code": c, "weight": WEIGHT,
                       "cond": {"gap_min": -0.05, "gap_max": 0.02},
                       "reason": "mech_v3：D 日封板低吸，弱势才时间止损"})
        pend.add(c)
        room -= 1
    return orders


def policy_mech_v0_nozh(w):
    return _mech_v0(w, PAUSE_NOZH)


def policy_mech_v0_nopause(w):
    return _mech_v0(w, PAUSE_NONE)


POLICIES = {"mech_v0": policy_mech_v0, "mech_v0_nozh": policy_mech_v0_nozh,
            "mech_v0_nopause": policy_mech_v0_nopause,
            "mech_v1": policy_mech_v1, "mech_v1_tight": policy_mech_v1_tight,
            "mech_v2": policy_mech_v2, "mech_v3": policy_mech_v3,
            "mech_v4a": policy_mech_v4a, "mech_v5": policy_mech_v5,
            "mech_v6": policy_mech_v6, "mech_v6b": policy_mech_v6b,
            "mech_v6c": policy_mech_v6c, "mech_v7": policy_mech_v7,
            "mech_v8": policy_mech_v8, "mech_v9": policy_mech_v9}
POLICIES["mech_v0_nogate"] = policy_mech_v0_nopause   # 兼容别名（v1 命名）


# ── 仿真内核（run 与 audit 共用 ⇒ 反事实与真实走同一执行器）────────────────
def new_state(btid, days, capital, env_missing=(), policy=None):
    import datetime as _dt
    return {"id": btid, "start": days[0], "end": days[-1], "capital": capital,
            "cash": capital, "cursor": -1, "days": list(days), "holdings": [],
            "pending_orders": [], "trades": [], "equity_curve": [],
            "events": [], "threshold_status": "UNCALIBRATED_SEED",
            "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "env_missing_days": list(env_missing), "policy": policy,
            "stop_loss": POLICY_STOP.get(policy, STOP_LOSS),
            "feed_version": _feed_version(), "exec_version": EXEC_VERSION}


def _simulate(prices, names, days, env, capital, policy_fn, btid,
              buy_rate, sell_rate, policy_name=None, get_pc=None, get_amt=None,
              get_ctx=None):
    """整窗仿真：逐日 止损扫描→成交→记净值→世界→policy 决策。返回终态。"""
    st = new_state(btid, days, capital,
                   env_missing=[d for d in days if d not in env],
                   policy=policy_name or getattr(policy_fn, "__name__", str(policy_fn)))
    for i, d in enumerate(days):
        events = []
        if i:
            d_prev = days[i - 1]
            for c in stoploss_scan(st, prices, d_prev):
                events.append({"d": d, "kind": "stoploss_armed", "code": c})
            events += execute_pending(st, prices, names, buy_rate, sell_rate, d)
        st["cursor"] = i
        eq = st["cash"] + sum(
            (prices.get(h["code"], {}).get(d, (0, 0, 0, h["entry_price"]))[3]) * h["shares"]
            for h in st["holdings"])
        st["equity_curve"].append([d, round(eq, 2)])
        st["last_events"] = events
        w = build_world(st, prices, names, env, d, get_pc, get_amt=get_amt,
                        get_ctx=get_ctx)
        # ★ 账户级熔断（P1b）：统一风控入口，策略变体不可覆盖
        _halt, _dd = account_halt(st["equity_curve"])
        if _halt:
            events.append({"d": d, "kind": "acc_halt", "dd": _dd})
        for od in policy_fn(w):
            od = dict(od)
            od["decided_at"] = d
            od.setdefault("kind", "policy")
            if _halt and od.get("action") == "buy":
                od["kind"] = "acc_halt_reject"
                events.append({"d": d, "kind": "acc_halt_reject",
                               "code": od.get("code"), "dd": _dd,
                               "why": "账户回撤 %.1f%% ≥ 熔断线——新开仓拦除" % _dd})
                continue
            st["pending_orders"].append(od)
    force_close(st, prices, sell_rate)
    st["status"] = "finished"
    st["metrics"] = metrics(st)
    return st


# ── 指标 ───────────────────────────────────────────────────────────────────
def metrics(st):
    eq = st.get("equity_curve") or []
    rets = [e / st["capital"] - 1 for _d, e in eq]
    total = rets[-1] if rets else 0.0
    peak, mdd = -1e18, 0.0
    for e in [x for _d, x in eq]:
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak)
    tr = st.get("trades", [])
    wins = [t for t in tr if t["pnl"] > 0]
    losses = [t for t in tr if t["pnl"] <= 0]
    avg_w = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0.0
    avg_l = (abs(sum(t["pnl"] for t in losses) / len(losses))) if losses else 0.0
    return {"total_return": round(total, 6),
            "max_drawdown": round(mdd, 6),
            "n_trades": len(tr), "win_rate": round(len(wins) / len(tr), 4) if tr else None,
            "avg_win": round(avg_w, 2), "avg_loss": round(avg_l, 2),
            "profit_loss_ratio": round(avg_w / avg_l, 4) if avg_l else None,
            "n_stoploss": sum(1 for t in tr if t.get("kind") == "stoploss")}


# ── 反推平面：Audit（三路）─────────────────────────────────────────────────
def _cf_exit(prices, days, c, i_entry, hold):
    """反事实退出：入场后第 hold 个交易日开盘卖；超窗用末日（或入场日）收盘并标 clamped。"""
    j = i_entry + hold
    clamped = j > len(days) - 1
    j = min(j, len(days) - 1)
    if j < i_entry:
        return None, None, True
    d = days[j]
    px = prices.get(c, {}).get(d)
    if not px:
        return None, None, True
    # j == i_entry 只发生在入场日即末日（钳制到底）⇒ 用当日收盘而非"开盘卖自己"
    price = px[3] if j == i_entry else px[0]
    return d, price, clamped


def _ci95(vals):
    n = len(vals)
    if n < 2:
        return None
    m = sum(vals) / n
    sd = (sum((x - m) ** 2 for x in vals) / (n - 1)) ** 0.5
    half = 1.96 * sd / (n ** 0.5)
    return [round(m - half, 6), round(m + half, 6)]


def run_audit(btid, prices, names, days, env, buy_rate, sell_rate, get_pc=None,
              get_amt=None, get_ctx=None):
    """三路反推 → audit 文档。①归因 ②闸门反事实 ③环境档分组 CI。"""
    st = load_state(btid)
    if st.get("status") != "finished":
        raise SystemExit("⛔ 先 finish 再 audit")
    trades = st.get("trades", [])
    br = buy_rate

    # ① 逐笔退出归因：实际 vs +5d/+10d 开盘替代
    attribution = []
    idx = {d: i for i, d in enumerate(days)}
    for t in trades:
        i0 = idx.get(t["entry_date"])
        if i0 is None:
            continue
        row = {"code": t["code"], "entry": t["entry_date"], "exit": t["exit_date"],
               "ret": t["ret"], "kind": t.get("kind")}
        for hold, key in ((5, "alt5"), (10, "alt10")):
            d_x, px, clamped = _cf_exit(prices, days, t["code"], i0, hold)
            if px:
                alt = px * (1 - sell_rate) / (t["entry_price"] * (1 + br)) - 1
                row[key] = round(alt, 6)
                row[key + "_clamped"] = clamped
        a5, a10 = row.get("alt5"), row.get("alt10")
        tags = []
        if a5 is not None and t["ret"] < a5 - 0.02:
            tags.append("早退（比+5d开盘少 %.1fpp）" % ((a5 - t["ret"]) * 100))
        if a10 is not None and t["ret"] > a10 + 0.02:
            tags.append("拿久了（比+10d开盘多 %.1fpp）" % ((t["ret"] - a10) * 100))
        if t.get("kind") == "stoploss":
            tags.append("止损单")
        row["tags"] = tags
        attribution.append(row)

    # ③ 实际交易按【信号日】环境档分组。
    #    ★ 锚定决策发生日而非成交日：闸门在信号日生效，且 T+1 成交日环境可能已翻脸
    #    （全历史首跑实测：54 笔"退潮日入场"实为强势日信号次日环境转退潮的成交）。
    def _sig_grade(d_entry):
        i0 = idx.get(d_entry)
        if i0 is None or i0 <= 0:
            return "NA"
        return (env.get(days[i0 - 1]) or {}).get("grade") or "NA"
    by_grade = defaultdict(list)
    for t in trades:
        by_grade[_sig_grade(t["entry_date"])].append(t["ret"])
    regime_table = {}
    for g, rs in sorted(by_grade.items(), key=lambda x: str(x[0])):
        n = len(rs)
        mean = sum(rs) / n if n else None
        ci = _ci95(rs)
        regime_table[g] = {
            "n": n, "win_rate": round(sum(1 for x in rs if x > 0) / n, 4) if n else None,
            "mean_ret": round(mean, 6) if mean is not None else None,
            "ci95": ci,
            "status": "OK" if n >= CI_MIN_N else "INSUFFICIENT（n<%d，不下结论）" % CI_MIN_N}

    # ② 闸门反事实：本实验策略 → 其「去闸孪生」（同一策略族，唯一差异=暂停名单）
    gate_cf = {"applicable": False}
    CONTRAST = {"mech_v0": "mech_v0_nopause", "mech_v0_nozh": "mech_v0_nopause"}
    _contrast = CONTRAST.get(st.get("policy"))
    if _contrast:
        nog = _simulate(prices, names, days, env, st["capital"],
                        POLICIES[_contrast], btid + "-cf", buy_rate, sell_rate,
                        policy_name=_contrast, get_pc=get_pc, get_amt=get_amt,
                        get_ctx=get_ctx)
        gate_keys = {(t["code"], t["entry_date"]) for t in trades}
        out = [t for t in nog["trades"]
               if (t["code"], t["entry_date"]) not in gate_keys]
        rows = []
        for t in out:
            i0 = idx.get(t["entry_date"])
            d_x, px, clamped = _cf_exit(prices, days, t["code"], i0, HOLD_DAYS)
            g = _sig_grade(t["entry_date"])
            rows.append({"code": t["code"], "fill_date": t["entry_date"],
                         "buy_open": t["entry_price"], "grade": g,
                         "cf_ret": round(px * (1 - sell_rate)
                                         / (t["entry_price"] * (1 + br)) - 1, 6) if px else None,
                         "cf_clamped": clamped,
                         "why_blocked": "信号日环境档=%s 不在开仓白名单" % g})
        byg = defaultdict(list)
        for r in rows:
            if r["cf_ret"] is not None:
                byg[r["grade"]].append(r["cf_ret"])
        # grade 已是信号日档位（rows 构造处用 _sig_grade）
        gate_cf = {"applicable": True,
                   "n_blocked": len(rows),
                   "blocked": rows[:50],
                   "by_grade": {g: {"n": len(v), "mean_cf_ret": round(sum(v) / len(v), 6),
                                    "ci95": _ci95(v)}
                                for g, v in sorted(byg.items())},
                   "note": "被闸拦掉的反事实收益：正=闸错杀（闸在挡钱），负=闸挡灾（闸在保值）；"
                           "n 小只作线索，须过 verify 统计确认"}
    return {"schema": "replay_audit@1", "bt": btid, "policy": st.get("policy"),
            "grouping": "signal_day_grade（闸门在信号日生效；成交日≠决策日）",
            "feed_version": st.get("feed_version"), "exec_version": st.get("exec_version"),
            "trade_attribution": attribution, "regime_table": regime_table,
            "gate_counterfactual": gate_cf,
            "threshold_status": st.get("threshold_status"),
            "boundary": "回测/审计产出=研究证据；禁直接修改生产规则（走 learning 归仓）"}


# ── 对比（V0/V1 硬闸）──────────────────────────────────────────────────────
def compare_results(r1, r2):
    """同 feed + 同 exec + 同 capital 才可比；返回 (ok, reason, 表)。"""
    for k in ("feed_version", "exec_version", "capital"):
        if r1.get(k) != r2.get(k):
            return False, "%s 不一致（%r vs %r）——不同世界/执行器比成绩 = 作弊，拒绝" % (
                k, r1.get(k), r2.get(k)), None
    keys = ("total_return", "max_drawdown", "n_trades", "win_rate",
            "profit_loss_ratio", "n_stoploss")
    tbl = {k: [r1.get("metrics", {}).get(k), r2.get("metrics", {}).get(k)] for k in keys}
    return True, "可比（同世界/同执行器/同本金）", tbl


# ── CLI：run / audit / compare（主接口）+ agent step（非主接口）────────────
def cmd_run(a):
    import datetime as _dt
    prices, names, days, get_pc, get_amt, get_ctx = load_window(a.start, a.end)
    if not days:
        raise SystemExit("⛔ 窗口内无数据：%s ~ %s" % (a.start, a.end))
    env = load_env(days[0], days[-1])
    policy = POLICIES.get(a.policy)
    if policy is None:
        raise SystemExit("⛔ 未知 policy %s（可选：%s）" % (a.policy, ", ".join(POLICIES)))
    btid = "BT-%s-%s" % (a.policy, _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    t0 = time.time()
    st = _simulate(prices, names, days, env, a.capital, policy, btid,
                   _rate("cost_buy_rate", 0.00125), _rate("cost_sell_rate", 0.00175),
                   policy_name=a.policy, get_amt=get_amt, get_ctx=get_ctx)
    save_state(btid, st)
    out = write_result(btid, {
        "id": btid, "start": st["start"], "end": st["end"], "capital": st["capital"],
        "policy": st["policy"], "feed_version": st["feed_version"],
        "exec_version": st["exec_version"], "metrics": st["metrics"],
        "trades": st["trades"], "equity_curve": st["equity_curve"],
        "threshold_status": st.get("threshold_status"),
        "boundary": "回测结论=研究证据；禁直接修改生产规则（走 learning 归仓）"})
    print(json.dumps({"bt_id": btid, "policy": a.policy, "days": len(days),
                      "env_missing_days": len(st["env_missing_days"]),
                      "metrics": st["metrics"], "result": out,
                      "elapsed_sec": round(time.time() - t0, 1),
                      "next": "audit %s" % btid,
                      "note": "环境档=UNCALIBRATED 种子档 ⇒ 结论是基线证据，不是冻结版结论"},
                     ensure_ascii=False, indent=1))
    return 0


def cmd_audit(a):
    st = load_state(a.bt)
    prices, names, days, get_pc, get_amt, get_ctx = load_window(st["start"], st["end"])
    env = load_env(st["start"], st["end"])
    doc = run_audit(a.bt, prices, names, days, env,
                    _rate("cost_buy_rate", 0.00125), _rate("cost_sell_rate", 0.00175),
                    get_amt=get_amt, get_ctx=get_ctx)
    out = write_audit(a.bt, doc)
    summary = {"audit": out,
               "regime_table": doc["regime_table"],
               "gate_counterfactual": {k: v for k, v in
                                       (doc["gate_counterfactual"] or {}).items()
                                       if k != "blocked"}}
    print(json.dumps(summary, ensure_ascii=False, indent=1)[:6000])
    return 0


def cmd_compare(a):
    r1 = json.load(io.open(os.path.join(bt_dir(a.bt1), "result.json"), encoding="utf-8"))
    r2 = json.load(io.open(os.path.join(bt_dir(a.bt2), "result.json"), encoding="utf-8"))
    ok, reason, tbl = compare_results(r1, r2)
    print(json.dumps({"ok": ok, "reason": reason,
                      "a": a.bt1, "b": a.bt2, "table": tbl},
                     ensure_ascii=False, indent=1))
    return 0 if ok else 2


def cmd_init(a):
    prices, names, days, get_pc, get_amt, get_ctx = load_window(a.start, a.end)
    if not days:
        raise SystemExit("⛔ 窗口内无数据：%s ~ %s" % (a.start, a.end))
    env = load_env(days[0], days[-1])
    miss = [d for d in days if d not in env]
    import datetime as _dt
    btid = "BT-agent-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    st = new_state(btid, days, a.capital, miss, policy="agent")
    save_state(btid, st)
    print(json.dumps({"bt_id": btid, "days": len(days), "first": days[0],
                      "last": days[-1], "env_missing_days": len(miss),
                      "feed_version": st["feed_version"],
                      "note": "agent 模式（非主接口）：检验完整智能用；"
                              "机械策略统计请走 run"},
                     ensure_ascii=False, indent=1))
    return 0


def cmd_next(a):
    st = load_state(a.bt)
    prices, names, _, get_pc, get_amt, get_ctx = load_window(st["start"], st["end"])
    env = load_env(st["start"], st["end"])
    nxt = st["cursor"] + 1
    if nxt >= len(st["days"]):
        print(json.dumps({"eof": True, "hint": "用 finish 收口"}, ensure_ascii=False))
        return 0
    d = st["days"][nxt]
    d_prev = st["days"][nxt - 1] if nxt else None
    buy_rate = _rate("cost_buy_rate", 0.00125)
    sell_rate = _rate("cost_sell_rate", 0.00175)
    events = []
    if d_prev:
        for c in stoploss_scan(st, prices, d_prev):
            events.append({"d": d, "kind": "stoploss_armed", "code": c})
        events += execute_pending(st, prices, names, buy_rate, sell_rate, d, get_pc)
    st["cursor"] = nxt
    eq = st["cash"] + sum(
        (prices.get(h["code"], {}).get(d, (0, 0, 0, h["entry_price"]))[3]) * h["shares"]
        for h in st["holdings"])
    st["equity_curve"].append([d, round(eq, 2)])
    st["last_events"] = events
    save_state(a.bt, st)
    world = build_world(st, prices, names, env, d, get_pc, get_amt=get_amt)
    if a.json:
        print(json.dumps(world, ensure_ascii=False, indent=1))
    else:
        ev = world["env"]
        print("== %s（第 %d/%d 日 · 剩余 %d 日）=="
              % (d, world["cursor"] + 1, len(st["days"]), world["days_remaining"]))
        print("环境档: %s（涨停 %s · 高度 %s · 炸板率 %s · 涨跌比 %s）【%s】"
              % (ev.get("grade"), ev.get("limit_up_count"), ev.get("max_board"),
                 ev.get("fail_rate"), ev.get("up_down_ratio"), ev.get("threshold_status")))
        print("宽度: 涨 %d / 跌 %d / 共 %d · 封板池 %d"
              % (world["breadth"]["up"], world["breadth"]["down"],
                 world["breadth"]["n"], len(world["limit_up_pool"])))
        print("现金 %.2f · 持仓 %d 笔 · 事件 %d 条"
              % (world["cash"], len(world["holdings"]), len(events)))
        for e in events:
            print("  ·", json.dumps(e, ensure_ascii=False)[:160])
        print("封板池:", " ".join(world["limit_up_pool"][:12]))
        for h in world["holdings"]:
            print("  持仓 %s %s %d股 @%.2f 现收益 %.2f%% 持有%d日"
                  % (h["code"], h["name"], h["shares"], h["entry_price"],
                     (h["ret"] or 0) * 100, h.get("held_days") or 0))
        print(world["instruction"])
    return 0


def cmd_apply(a):
    st = load_state(a.bt)
    try:
        dec = json.loads(a.decision)
    except Exception as e:                                    # noqa: BLE001
        raise SystemExit("⛔ 决策 JSON 解析失败：%s" % e)
    if st["cursor"] < 0:
        raise SystemExit("⛔ 先 next 再决策")
    d = st["days"][st["cursor"]]
    act = dec.get("action")
    if act not in ("buy", "sell", "hold"):
        raise SystemExit("⛔ action 只能是 buy/sell/hold")
    if act in ("buy", "sell") and not dec.get("code"):
        raise SystemExit("⛔ %s 必须给 code" % act)
    entry = {"action": act, "decided_at": d, "reason": dec.get("reason", ""),
             "code": dec.get("code"), "weight": dec.get("weight")}
    if act in ("buy", "sell"):
        st.setdefault("pending_orders", []).append(entry)
    save_state(a.bt, st)
    print(json.dumps({"registered": act, "at": d,
                      "executes_at": "次一交易日开盘",
                      "note": "引擎机械校验（涨跌停/资金/持仓/重复信号）在成交时进行"},
                     ensure_ascii=False))
    return 0


def cmd_finish(a):
    st = load_state(a.bt)
    prices, names, _, get_pc, get_amt, get_ctx = load_window(st["start"], st["end"])
    force_close(st, prices, _rate("cost_sell_rate", 0.00175))
    m = metrics(st)
    st["status"] = "finished"
    st["metrics"] = m
    save_state(a.bt, st)
    out = write_result(a.bt, {"id": a.bt, "start": st["start"], "end": st["end"],
                              "capital": st["capital"], "policy": st.get("policy"),
                              "feed_version": st.get("feed_version"),
                              "exec_version": st.get("exec_version"),
                              "metrics": m,
                              "trades": st["trades"], "equity_curve": st["equity_curve"],
                              "threshold_status": st.get("threshold_status"),
                              "boundary": "回测结论=研究证据；禁直接修改生产规则（走 learning 归仓）"})
    print(json.dumps({"finished": a.bt, "metrics": m, "result": out,
                      "boundary": "审计产出禁直接改生产规则"}, ensure_ascii=False, indent=1))
    return 0


def cmd_look(a):
    st = load_state(a.bt)
    prices, names, _, get_pc, get_amt, get_ctx = load_window(st["start"], st["end"])
    m = prices.get(a.code)
    if not m:
        raise SystemExit("⛔ %s 无数据" % a.code)
    ds = sorted(d for d in m if d <= st["days"][max(st["cursor"], 0)])[-a.bars:]
    print("== %s %s（截至 %s，共 %d 根）==" % (a.code, names.get(a.code, "?"),
                                            ds[-1] if ds else "-", len(ds)))
    for d in ds:
        o, h, l, cl = m[d]
        print("  %s o=%.2f h=%.2f l=%.2f c=%.2f" % (d, o, h, l, cl))
    return 0


def cmd_counterfactual(a):
    """单票反事实查询（上帝视角工具；批量反推走 audit）。"""
    prices, names, days, get_pc, get_amt, get_ctx = load_window(a.entry, (a.until or a.entry))
    if a.entry not in days:
        raise SystemExit("⛔ entry %s 非交易日或无数据" % a.entry)
    i = days.index(a.entry)
    if i + 1 >= len(days):
        raise SystemExit("⛔ entry 之后无交易日，无法 T+1 成交")
    d_buy = days[i + 1]
    c = a.code
    if not prices.get(c, {}).get(d_buy):
        raise SystemExit("⛔ %s 在 %s 无行情" % (c, d_buy))
    pc = prices[c][a.entry][3]
    o_buy = prices[c][d_buy][0]
    chg = o_buy / pc - 1
    if chg >= NEAR_LIMIT_UP:
        print(json.dumps({"code": c, "entry": a.entry, "buy_date": d_buy,
                          "open_gap": round(chg, 4),
                          "verdict": "近涨停开盘，买不进（反事实不成立）"},
                         ensure_ascii=False))
        return 0
    i_exit = min(i + 1 + (a.days or 10), len(days) - 1)
    d_sell = days[i_exit]
    o_sell = prices[c][d_sell][0]
    br, sr = _rate("cost_buy_rate", 0.00125), _rate("cost_sell_rate", 0.00175)
    shares = int(a.capital / (o_buy * (1 + br)) / 100) * 100
    cost = shares * o_buy * (1 + br)
    proceeds = shares * o_sell * (1 - sr)
    print(json.dumps({
        "code": c, "name": names.get(c, ""), "counterfactual": True,
        "signal_day": a.entry, "buy_date": d_buy, "buy_open": o_buy,
        "shares": shares, "cost": round(cost, 2),
        "sell_date": d_sell, "sell_open_or_close": o_sell,
        "proceeds": round(proceeds, 2), "pnl": round(proceeds - cost, 2),
        "ret": round(proceeds / cost - 1, 6),
        "note": "上帝视角工具——用于审计放弃票/漏买票；产出属学习证据，禁直接改生产规则"},
        ensure_ascii=False, indent=1))
    return 0


def cmd_status(a):
    st = load_state(a.bt)
    print(json.dumps({k: st.get(k) for k in
                      ("id", "start", "end", "capital", "cursor", "status",
                       "policy", "threshold_status", "feed_version",
                       "exec_version", "env_missing_days")},
                     ensure_ascii=False, indent=1))
    return 0


def _rate(key, default):
    try:
        import params as PARAMS
        v = PARAMS.get_value(key)
        return float(v) if v is not None else default
    except Exception:                                         # noqa: BLE001
        return default


# ── 自检 ───────────────────────────────────────────────────────────────────
def _selftest():
    import tempfile as _tf
    import shutil as _sh
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== replay --selftest ==")
    sb = _tf.mkdtemp(prefix="replay_")
    try:
        globals()["BT_DIR"] = os.path.join(sb, "replay")
        # 迷你行情：3 票 × 6 日
        # sh600001: D2 封板（pct+10%、c=h）→ gate/nogate 都在 D2 信号、D3 开盘 11.2 成交
        # sh600002: D5 封板（pc=10.2 → 12.5 = +22.5%）→ gate 拒（D5=退潮）、nogate D6 买入
        # sh600003: 供执行器 A 组（止损/卖出）
        seq = {
            "sh600001": [("2026-01-05", 10.0, 10.2, 9.9, 10.0),
                         ("2026-01-06", 10.2, 11.0, 10.15, 11.0),
                         ("2026-01-07", 11.2, 11.4, 11.0, 11.3),
                         ("2026-01-08", 11.3, 11.6, 11.2, 11.5),
                         ("2026-01-09", 11.5, 11.6, 11.3, 11.4),
                         ("2026-01-12", 11.4, 11.5, 11.2, 11.3)],
            "sh600002": [("2026-01-05", 10.0, 10.1, 9.9, 10.0),
                         ("2026-01-06", 10.0, 10.1, 9.9, 10.0),
                         ("2026-01-07", 10.0, 10.2, 9.9, 10.05),
                         ("2026-01-08", 10.05, 10.3, 10.0, 10.2),
                         ("2026-01-09", 11.2, 12.5, 11.1, 12.5),
                         ("2026-01-12", 12.6, 12.8, 12.4, 12.6)],
            "sh600003": [("2026-01-05", 10.0, 10.1, 9.9, 10.0),
                         ("2026-01-06", 10.0, 10.2, 9.9, 10.1),
                         ("2026-01-07", 10.1, 10.3, 10.0, 10.2),
                         ("2026-01-08", 10.2, 10.3, 9.1, 9.15),
                         ("2026-01-09", 9.1, 9.2, 8.9, 9.0),
                         ("2026-01-12", 9.0, 9.1, 8.9, 9.0)],
        }
        # sh600004~08：D5 封板（pc=10.0 → 12.5 = +25%）、D6 开盘 12.6 可买
        # —— 供 nogate 在退潮日买入、gate 拒掉，构成闸门反事实样本
        for _i in range(4, 9):
            seq["sh60000%d" % _i] = [
                ("2026-01-05", 10.0, 10.1, 9.9, 10.0),
                ("2026-01-06", 10.0, 10.1, 9.9, 10.0),
                ("2026-01-07", 10.0, 10.2, 9.9, 10.0),
                ("2026-01-08", 10.0, 10.2, 9.9, 10.0),
                ("2026-01-09", 11.0, 12.5, 10.9, 12.5),
                ("2026-01-12", 12.6, 12.8, 12.4, 12.6)]
        # sh600009：D3 开盘 11.6（前收 10.1 → +14.85%）专测近涨停拒买
        seq["sh600009"] = [
            ("2026-01-05", 10.0, 10.1, 9.9, 10.0),
            ("2026-01-06", 10.0, 10.2, 9.9, 10.1),
            ("2026-01-07", 11.6, 11.7, 11.5, 11.6),
            ("2026-01-08", 11.6, 11.7, 11.5, 11.6),
            ("2026-01-09", 11.6, 11.7, 11.5, 11.6),
            ("2026-01-12", 11.6, 11.7, 11.5, 11.6)]
        kl = os.path.join(sb, "klines.csv")
        with io.open(kl, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["code", "date", "open", "high", "low", "close", "name"])
            for c, s in seq.items():
                for d, o, h, l, cl in s:
                    w.writerow([c, d, o, h, l, cl, "测试" + c[-3:]])
        globals()["KL"] = kl
        # 迷你环境档：D1 震荡 / D2-D4 主升 / D5-D6 退潮
        envc = os.path.join(sb, "env.csv")
        with io.open(envc, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["date", "limit_up_count", "max_board", "fail_rate",
                        "up_down_ratio", "grade"])
            for d, g, n in [("2026-01-05", "震荡", 10), ("2026-01-06", "主升", 40),
                            ("2026-01-07", "主升", 45), ("2026-01-08", "主升", 42),
                            ("2026-01-09", "退潮", 8), ("2026-01-12", "退潮", 6)]:
                w.writerow([d, n, 3, 0.2, 1.5, g])
        globals()["ENV_CSV"] = envc
        globals()["MIN_POOL"] = 1   # 迷你夹具封板池只有 1~6 只，放宽冰点闸以驱动信号

        pr, nm, days, get_pc, get_amt, get_ctx = load_window("2026-01-05", "2026-01-12")
        T("A1 窗口加载：9 票 × 6 日 + 前收盘查询", len(pr) == 9 and len(days) == 6
          and get_pc("sh600001", "2026-01-06") == 10.0)
        env = load_env(days[0], days[-1])
        T("A1b 环境档加载：6 日全有档", len(env) == 6
          and env["2026-01-09"]["grade"] == "退潮")

        st = {"id": "BT-TEST", "start": days[0], "end": days[-1], "capital": 100000,
              "cash": 100000, "cursor": -1, "days": days, "holdings": [],
              "pending_orders": [], "trades": [], "equity_curve": [], "events": []}
        w1 = build_world(st, pr, nm, env, days[0], get_pc)
        blob = json.dumps(w1)
        T("A2 世界输出物理隔离未来（JSON 不含 >D 日期）",
          all(x not in blob for x in days[1:]))
        T("A2b 封板池口径：D1 无封板（首日无 pct）", w1["limit_up_pool"] == [])
        w2 = build_world({**st, "cursor": 1}, pr, nm, env, days[1], get_pc)
        T("A2c D2 封板池 = [sh600001]（pct≥9.55% 且 c≥0.997h）",
          w2["limit_up_pool"] == ["sh600001"])

        # 执行器回归（A 组，沿用 v1 已验证语义）
        st["pending_orders"].append({"action": "buy", "code": "sh600001",
                                     "weight": 0.3, "kind": "agent", "reason": "测试"})
        ev = execute_pending(st, pr, nm, 0.00125, 0.00175, days[1], get_pc)
        fills = [e for e in ev if e["kind"] == "fill"]
        T("A3 T+1 开盘成交：买入 sh600001 @10.2",
          len(fills) == 1 and fills[0]["price"] == 10.2 and st["cash"] < 100000)
        st["pending_orders"].append({"action": "buy", "code": "sh600001",
                                     "weight": 0.3, "kind": "agent"})
        ev = execute_pending(st, pr, nm, 0.00125, 0.00175, days[2], get_pc)
        T("A3b 重复信号拒单（引擎兜底，policy 不必自觉）",
          len(ev) == 1 and ev[0]["kind"] == "reject" and "重复" in ev[0]["why"])
        st["pending_orders"].append({"action": "buy", "code": "sh600009",
                                     "weight": 0.3, "kind": "agent"})
        ev = execute_pending(st, pr, nm, 0.00125, 0.00175, days[2], get_pc)
        T("A4 近涨停开盘拒买留痕（sh600009 D3 开盘 +14.85%）",
          all(e["kind"] == "reject" for e in ev) and len(ev) == 1)
        st["pending_orders"].append({"action": "buy", "code": "sh600003",
                                     "weight": 0.3, "kind": "agent"})
        execute_pending(st, pr, nm, 0.00125, 0.00175, days[2], get_pc)
        armed = stoploss_scan(st, pr, days[3])
        T("A5 止损兜底：浮亏≤-8% 自动生成 stoploss 卖单",
          armed == ["sh600003"]
          and st["pending_orders"][-1]["kind"] == "stoploss")
        st["pending_orders"].append({"action": "sell", "code": "sh600001", "kind": "agent"})
        ev = execute_pending(st, pr, nm, 0.00125, 0.00175, days[5], get_pc)
        sells = {e["code"]: e for e in ev if e.get("action") == "sell" and e["kind"] == "fill"}
        T("A6 卖出成交：agent 卖 sh600001 @11.4 + stoploss 卖 sh600003",
          sells.get("sh600001", {}).get("price") == 11.4
          and "sh600003" in sells)

        # ── A7/A8：账户级熔断（2026-10-27 整改 P1b，执行器 v2）──
        T("A7 账户熔断：权益自峰回撤 ≥15% → halt；未及 → 不拦",
          account_halt([["2026-01-05", 100000.0], ["2026-01-06", 92000.0],
                        ["2026-01-07", 84900.0]])[0] is True
          and account_halt([["2026-01-05", 100000.0],
                            ["2026-01-06", 95000.0]])[0] is False)
        T("A8 MAX_POS 统一 params（max_holding_count=3，原硬编 4 已废）",
          _max_pos() == 3)

        # ── E 组：条件单（预案触发区的回测实现）──
        st["pending_orders"].append({"action": "buy", "code": "sh600002", "weight": 0.3,
                                     "kind": "agent",
                                     "cond": {"gap_min": 0.03, "gap_max": 0.05}})
        ev = execute_pending(st, pr, nm, 0.00125, 0.00175, days[2], get_pc)
        T("E1 条件单缺口不符 → 撤单留痕不顺延（D3 开盘 +0% ∉ [3%,5%]）",
          len(ev) == 1 and ev[0]["kind"] == "cancel" and "撤" in ev[0]["why"])
        # sh600001 D3 开盘 11.2 / pc 11.0 = +1.8% ∈ [-5%,2%] → 应成交
        st["pending_orders"].append({"action": "buy", "code": "sh600003", "weight": 0.3,
                                     "kind": "agent",
                                     "cond": {"gap_min": -0.05, "gap_max": 0.02}})
        ev = execute_pending(st, pr, nm, 0.00125, 0.00175, days[2], get_pc)
        fills2 = [e for e in ev if e["kind"] == "fill"]
        T("E2 条件单缺口相符 → 正常成交（+1.8% ∈ [-5%,2%]）",
          len(fills2) == 1 and fills2[0]["code"] == "sh600003")

        # ── P 组：决策平面（policy 纯函数）──
        wD2 = build_world({**st, "holdings": [], "pending_orders": [], "cursor": 1},
                          pr, nm, env, days[1], get_pc)
        o1 = policy_mech_v0(wD2)
        o2 = policy_mech_v0(wD2)
        T("P1 policy 确定性：同世界两次调用输出一致（可重复回放的前提）",
          json.dumps(o1, sort_keys=True) == json.dumps(o2, sort_keys=True))
        wD5 = build_world({**st, "holdings": [], "pending_orders": [], "cursor": 4},
                          pr, nm, env, days[4], get_pc)
        wV1 = build_world({**st, "holdings": [], "pending_orders": [], "cursor": 1},
                          pr, nm, env, days[1], get_pc)
        T("P2b V1 低吸变体：买单带条件单（缺口触发区）",
          all(o.get("cond", {}).get("gap_max") == 0.02
              for o in policy_mech_v1(wV1) if o["action"] == "buy"))
        # V2 证伪线：sh600003 D4 收 9.15 ≤ 入场日低点（D3 low 10.0）⇒ 必触发卖出
        wF = build_world({"id": "T", "start": days[0], "end": days[-1], "capital": 1e5,
                          "cash": 1e5, "cursor": 3, "days": days,
                          "holdings": [{"code": "sh600003", "name": "丙", "shares": 1000,
                                        "entry_price": 10.1, "entry_date": days[2],
                                        "entry_low": 10.0}],
                          "pending_orders": [], "trades": [], "equity_curve": []},
                         pr, nm, env, days[3], get_pc)
        T("P2c V2 证伪线：收盘 ≤ 入场日低点 ⇒ 次日开盘卖",
          any(o["action"] == "sell" and "证伪" in o.get("reason", "")
              for o in policy_mech_v2(wF)))
        T("P2 C1 风险预算：退潮日 A/B 都暂停开仓 / C 全开",
          not any(x["action"] == "buy" for x in policy_mech_v0(wD5))
          and not any(x["action"] == "buy" for x in policy_mech_v0_nozh(wD5))
          and any(x["action"] == "buy" for x in policy_mech_v0_nopause(wD5)))
        wZH = build_world({**st, "holdings": [], "pending_orders": [], "cursor": 1},
                          pr, nm, env, days[1], get_pc)
        wZH["env"] = dict(wZH["env"], grade="震荡")   # 同一世界只改档位 ⇒ 纯函数可控
        T("P3 震荡闸对照：A 停 / B（仅退潮停）开 —— 公平对照的机械前提",
          not any(x["action"] == "buy" for x in policy_mech_v0(wZH))
          and any(x["action"] == "buy" for x in policy_mech_v0_nozh(wZH)))

        # ── R 组：run 全流程（gate vs nogate）──
        stg = _simulate(pr, nm, days, env, 100000, POLICIES["mech_v0"],
                        "BT-G", 0.00125, 0.00175, policy_name="mech_v0")
        stb = _simulate(pr, nm, days, env, 100000, POLICIES["mech_v0_nozh"],
                        "BT-B", 0.00125, 0.00175, policy_name="mech_v0_nozh")
        stn = _simulate(pr, nm, days, env, 100000, POLICIES["mech_v0_nopause"],
                        "BT-N", 0.00125, 0.00175, policy_name="mech_v0_nopause")
        tg = {(t["code"], t["entry_date"]) for t in stg["trades"]}
        tn = {(t["code"], t["entry_date"]) for t in stn["trades"]}
        T("R1 run(gate)：sh600001 于 D3 成交（信号 D2 → T+1）",
          ("sh600001", "2026-01-07") in tg)
        T("R2 run(gate)：退潮日封板票 sh600002 未被买入",
          ("sh600002", "2026-01-12") not in tg)
        tb = {(t["code"], t["entry_date"]) for t in stb["trades"]}
        T("R3 闸门包含链：A ⊆ B ⊆ C（差异只能来自暂停名单）",
          tg <= tb <= tn and ("sh600002", "2026-01-12") in tn)
        T("R4 result 版本钉扎：feed/exec 版本在 state",
          bool(stg["feed_version"]) and stg["exec_version"] == EXEC_VERSION)

        # ── X 组：反推平面 ──
        globals()["_AUDIT_ST"] = stg
        save_state("BT-G", stg)
        doc = run_audit("BT-G", pr, nm, days, env, 0.00125, 0.00175)
        T("X1 归因：每笔交易有 alt5/alt10 替代退出",
          all(("alt5" in r or "alt5_clamped" in r) for r in doc["trade_attribution"]))
        gcf = doc["gate_counterfactual"]
        T("X2 闸门反事实：n_blocked≥1 且按档分组（退潮档被拦）",
          gcf["applicable"] and gcf["n_blocked"] >= 1
          and "退潮" in gcf["by_grade"])
        rt = doc["regime_table"]
        T("X3 环境分组：实际交易按入场日档位分组且 n<20 → INSUFFICIENT（不下结论）",
          "主升" in rt and rt["主升"]["status"].startswith("INSUFFICIENT")
          and rt["主升"]["ci95"] is None)
        T("X4 边界声明：audit 文档带禁改生产规则边界",
          "禁直接修改生产规则" in doc["boundary"])

        # ── CMP 组：对比硬闸 ──
        r1 = {"feed_version": "F1", "exec_version": "E1", "capital": 100000,
              "metrics": {"total_return": 0.1}}
        r2 = {"feed_version": "F1", "exec_version": "E1", "capital": 200000,
              "metrics": {"total_return": 0.2}}
        r3 = {"feed_version": "F2", "exec_version": "E1", "capital": 100000,
              "metrics": {"total_return": 0.3}}
        ok1, why1, _ = compare_results(r1, dict(r2, capital=100000))
        ok2, why2, _ = compare_results(r1, r2)
        ok3, why3, _ = compare_results(r1, r3)
        T("C1 同世界/执行器/本金 → 可比", ok1)
        T("C2 本金不同 → 拒绝", not ok2 and "capital" in why2)
        T("C3 数据版本不同 → 拒绝（不同世界比成绩=作弊）",
          not ok3 and "feed_version" in why3)
    finally:
        globals()["KL"] = os.path.join(_ROOT, "data", "raw", "bus", "klines.csv")
        globals()["ENV_CSV"] = os.path.join(_ROOT, "data", "research", "env_recalc",
                                            "env_metrics_recalc_full.csv")
        globals()["BT_DIR"] = os.path.join(_ROOT, "data", "research", "replay")
        globals()["MIN_POOL"] = 5
        _sh.rmtree(sb, ignore_errors=True)
    print("== replay --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def _main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "selftest":
        return _selftest()
    cmd, rest = argv[0], argv[1:]
    if cmd == "run":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--policy", default="mech_v0")
        ap.add_argument("--start", required=True)
        ap.add_argument("--end", required=True)
        ap.add_argument("--capital", type=float, default=100000)
        return cmd_run(ap.parse_args(rest))
    if cmd == "audit":
        return cmd_audit(type("A", (), {"bt": rest[0]})())
    if cmd == "compare":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("bt1")
        ap.add_argument("bt2")
        return cmd_compare(ap.parse_args(rest))
    if cmd == "init":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--start", required=True)
        ap.add_argument("--end", required=True)
        ap.add_argument("--capital", type=float, default=100000)
        return cmd_init(ap.parse_args(rest))
    if cmd == "next":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("bt")
        ap.add_argument("--json", action="store_true")
        return cmd_next(ap.parse_args(rest))
    if cmd == "look":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("bt")
        ap.add_argument("--code", required=True)
        ap.add_argument("--bars", type=int, default=30)
        return cmd_look(ap.parse_args(rest))
    if cmd == "apply":
        bt = rest[0]
        dec = rest[1] if len(rest) > 1 else rest[1:]
        return cmd_apply(type("A", (), {"bt": bt, "decision": dec})())
    if cmd == "finish":
        return cmd_finish(type("A", (), {"bt": rest[0]})())
    if cmd == "status":
        return cmd_status(type("A", (), {"bt": rest[0]})())
    if cmd == "counterfactual":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--code", required=True)
        ap.add_argument("--entry", required=True)
        ap.add_argument("--days", type=int, default=10)
        ap.add_argument("--until", default=None)
        ap.add_argument("--capital", type=float, default=100000)
        return cmd_counterfactual(ap.parse_args(rest))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
