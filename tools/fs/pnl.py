#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pnl.py · 净值账本（施工方案 0.4 · v1.0 · 2026-10-08 建）

用途：**结算层**的唯一管理者 —— 只记「后来实际发生了什么」。
      与决策快照层（`tools/fs/decision.py` 0.3）**严格分层、两文件、不合并**（设计 §395）。

落点：
  LIVE → data/generated/pnl_ledger.jsonl
  SIM  → data/generated/sim/pnl_ledger.jsonl      ← 与决策轴一一对应

★ 一、严格分层的执行点（不是口号）
  · 结算**只向 pnl_ledger 追加一行**，`decision_ledger.jsonl` 一个字节都不动
    （自检 E1 用逐字节对比证明）；
  · DECISION 行的 `outcome` 恒为 null —— 结果**永不回填**决策行；
  · pnl 行**不重复存 thesis / 驱动链 / env** —— 那些是「当时的判断」，读决策层（0.3）。
    结果表只记结果：把判断也抄一份 = 造第二个真源。
  · 轴由 decision_id **派生**（不由调用方传 sim）—— 给 SIM 决策记的结果必落 SIM 轴，
    从接口层杜绝「SIM 结果被写进 LIVE 净值曲线」。

★ 二、机械件把「可算的都算掉」（防手滑与后视）
  · `pnl_pct`   不传 → 自动算 (exit−entry)/entry×100；传入 → 与算式比对，不符即拒；
  · `pnl_amount` 不传 → 自动算 capital × pnl_pct/100；传入 → 一致性校验（金额盈亏是净值曲线唯一来源）；
  · `outcome` 与收益率**符号一致性**校验（v1.2：WIN/LOSS/FLAT = 最终盈亏结果，严格符号对齐；
    「止损触发但卖出价仍高于入场价」落 outcome=WIN + exit_kind=STOP，事件与结果分开记录）；
  · `exit_kind`（v1.2）：STOP / TARGET / WINDOW_END / NOT_FILLED —— 退出原因事件，
    与 outcome 分开记录（P0 审计裁决 2026-10-10）；exit_date = 退出日；
  · `fee_amount` / `net_amount`（v1.3，任务9#1 成本建模）：fee = 该笔全部费用（非负，
    结算方按正式费率参数计算传入）；net = pnl_amount − fee（机械可算即比对，不符即拒）。
    pnl_amount 保持毛盈亏语义；net_curve / summary 消费费后口径（旧 schema≤2 行无
    费用字段 → 回退毛口径，不补造费用）；
  · 可成交性（0.6 口径）在传入 day_low/day_high 时机械校验：做多 entry 必须落在当日区间内。

★ 三、防后视：判定指标必须与下注时冻结的一致
  M1/M2/M3 逐项判 SUPPORT / MIXED / REFUTE，**判定用的 metric / data_source 必须与决策行里
  冻结的 `thesis.prediction[]` 逐项相同** —— 换了指标再判自己赢 = 后视污染，一律拒（METRIC_DRIFT）。
  三项必须齐全（**禁 M1M3 合并缩写**，M2=板块扩散不能漏）。

★ 四、字段来源与两处补口（原条未定义，本条定死并回写施工方案）
  · `capital`（该笔实际投入金额）—— 原条只有比例与金额，**没有本金则 pnl_amount 无法与手算对齐**，
    故补此字段作为金额口径的唯一基数；`pnl_amount = capital × pnl_pct / 100` 硬校验。
  · `hold_days`（持有交易日数）—— 结算结果的时间维度，C6 归因 L4「时间」环的必需输入。
  · `attribution_L1L5` 五键 = L1 环境 / L2 方向 / L3 选股 / L4 时间 / L5 执行（设计 §324 机制归因）；
    `attribution_三问` 三键 = 驱动 / 赔率 / 证伪。值域 OK / WRONG / NA（NA = 该环未参与或无法判）。

★ 五、本模块零交易动作
  无下单 / 撤单 / 成交接口，无网络与子进程导入；写账报告 `orders_created` 恒 0。
  **写账永不反过来触发交易**。

被谁调用：tools/risk/settle.py（0.6 五步结算之「盈亏结算 / 复盘归因」步）、
          C6 / E3 / E6（净值曲线与判断质量统计）、tools/learning/case_builder.py（1.6）。

用法：
  python tools/fs/pnl.py                       # 只读总览（两轴）
  python tools/fs/pnl.py --curve               # 净值曲线（LIVE）
  python tools/fs/pnl.py --curve --sim         # 净值曲线（SIM）
  python tools/fs/pnl.py --selftest            # 内置验收（临时文件，不碰真数据）

规则：禁在别处内联本账本读写；字段增删必须升 SCHEMA_VERSION 并同步施工方案 0.4。
"""
import datetime as _dt
import io
import json
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.dirname(os.path.abspath(__file__))))
from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
import decision as DEC  # noqa: E402
import astwin as ASTW  # noqa: E402   ← AST 窗口（尾界）唯一实现，禁本模块自写

SCHEMA_VERSION = 3
LEDGER_NAME = "pnl_ledger.jsonl"
SIM_DIR = "sim"

# ── 枚举（改枚举 = 改契约，须升 SCHEMA_VERSION） ──────────────────────────────
OUTCOMES = ("WIN", "LOSS", "FLAT", "NOT_FILLED")   # NOT_FILLED = 未在场/未成交（记事实，不算胜负）
                                                   # v1.2：WIN/LOSS/FLAT = **最终盈亏结果**（符号对齐）
EXIT_KINDS = ("STOP", "TARGET", "WINDOW_END", "NOT_FILLED")   # v1.2：退出原因（事件），与盈亏分离
VERDICTS = ("SUPPORT", "MIXED", "REFUTE")          # 判断质量 ≠ 盈亏（设计 §327）
SLOTS = ("M1", "M2", "M3")                         # 三槽位，禁合并缩写
JUDGE = ("OK", "WRONG", "NA")
L1L5_KEYS = ("L1", "L2", "L3", "L4", "L5")
L1L5_LABEL = {"L1": "环境", "L2": "方向", "L3": "选股", "L4": "时间", "L5": "执行"}
THREE_KEYS = ("驱动", "赔率", "证伪")
VERDICT_ITEM_FIELDS = ("prediction_id", "metric", "data_source", "verdict", "observed", "note")

# ── 行字段（施工 0.4 + v1.2 exit_kind/exit_date + v1.3 fee_amount/net_amount）──
# v1.2（2026-10-10 P0 审计裁决）：exit_kind（退出原因事件）与 exit_date（退出日）——
# 退出原因与最终盈亏**分开记录**；outcome 语义统一为最终盈亏结果（符号对齐）。
# v1.3（2026-10-10 任务9#1 成本建模）：fee_amount（该笔全部费用）与 net_amount
# （费后净盈亏 = pnl_amount − fee_amount）——费用进入正式净值账本；
# pnl_amount 保持**毛盈亏**（纯价差）语义不变，费前/费后两个口径都留痕。
FIELDS = ("schema_version", "decision_id", "sim", "outcome",
          "entry_price", "exit_price", "exit_kind", "exit_date", "capital",
          "pnl_amount", "fee_amount", "net_amount", "pnl_pct",
          "attribution_L1L5", "attribution_三问",
          "M1", "M2", "M3", "hold_days", "settle_date", "settled_at")

PCT_TOL = 1e-4          # 百分数一致性容差（四舍五入容忍）
AMT_TOL = 1e-6          # 金额一致性相对容差


class PnlError(Exception):
    """净值账本操作被拒（非法字段 / 与决策层不一致 / 重复结算 / 篡改型追加）——显式抛错。"""


# ── 基础校验 ──────────────────────────────────────────────────────────────────
def _check_date(date, name="date"):
    s = date.isoformat() if hasattr(date, "isoformat") else str(date)
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise PnlError("%s 必须是 YYYY-MM-DD，收到: %r" % (name, date))
    try:
        _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        raise PnlError("%s 非法: %r" % (name, date))
    return s


def _check_num(v, name, allow_none=False, positive=False, non_negative=False):
    if v is None:
        if allow_none:
            return None
        raise PnlError("%s 必填" % name)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise PnlError("%s 必须是数值，收到: %r" % (name, v))
    if positive and v <= 0:
        raise PnlError("%s 必须 > 0，收到: %r" % (name, v))
    if non_negative and v < 0:
        raise PnlError("%s 必须 >= 0，收到: %r" % (name, v))
    return float(v)


def _check_enum(v, allowed, name):
    if v not in allowed:
        raise PnlError("%s 只能是 %s，收到: %r" % (name, " / ".join(allowed), v))
    return v


def _now():
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ── 轴（与决策轴同名同目录，一一对应） ────────────────────────────────────────
def ledger_root(base=None):
    """base → 账本根目录（`data/generated`）。**两种调用口径归一**（同 decision.ledger_root）。

    ★ 2026-10-20 修：编排层传「项目根」、本模块契约是「账本根」，混用导致 SIM 账本
      落到 `<项目根>/sim/`（真账本读成空 ⇒ 回撤/收益恒 0 的静默错）。判据**在场证据
      优先**：base 下已有任一轴账本 ⇒ base 即账本根；否则 `<base>/data/generated`
      存在 ⇒ base 是项目根，下钻一级。
    """
    if not base:
        return os.path.join(ROOT, "data", "generated")
    if _looks_like_ledger_root(base):
        return base
    if os.path.isdir(os.path.join(base, "data")):
        # 项目根结构性特征 = 有 data/ 目录（同 decision.ledger_root 口径）
        return os.path.join(base, "data", "generated")
    return base


def _looks_like_ledger_root(p):
    """p（或其 sim/）下已有任一轴账本文件 ⇒ p 是账本根（在场证据优先，同 decision 口径）。

    认 `*_ledger.jsonl` 家族：决策账本已在场而盈亏账本尚未创建时，仍须认得出账本根。
    """
    for d in (p, os.path.join(p, SIM_DIR)):
        if not os.path.isdir(d):
            continue
        try:
            for fn in os.listdir(d):
                if fn.endswith("_ledger.jsonl"):
                    return True
        except OSError:
            pass
    return False


def ledger_path(sim, base=None):
    """轴 → 文件路径。base = 账本**根目录**或项目根（自动下钻，见 `ledger_root`）；SIM 自动落 base/sim/。"""
    if not isinstance(sim, bool):
        raise PnlError("sim 必须显式布尔（False=LIVE / True=SIM），收到: %r" % (sim,))
    root = ledger_root(base)
    return os.path.join(root, SIM_DIR, LEDGER_NAME) if sim else os.path.join(root, LEDGER_NAME)


def axis_name(sim):
    return "SIM" if sim else "LIVE"


def _read_rows(sim, base=None):
    p = ledger_path(sim, base)
    if not os.path.exists(p):
        return []
    rows = []
    with io.open(p, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except ValueError as e:
                raise PnlError("账本第 %d 行不是合法 JSON（%s）：%s" % (i, e, p))
            if not isinstance(obj, dict):
                raise PnlError("账本第 %d 行必须是 JSON 对象：%s" % (i, p))
            rows.append(obj)
    return rows


def _append(row, sim, base=None):
    """追加一行 —— 本模块唯一的写动作（模式 "a"；无 update / delete 路径）。"""
    p = ledger_path(sim, base)
    _WG.check_write(p)
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with io.open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return p


# ── 归因结构校验 ──────────────────────────────────────────────────────────────
def _norm_l1l5(v):
    if not isinstance(v, dict):
        raise PnlError("attribution_L1L5 必填（机制归因五环：哪一环判断错）")
    extra = set(v) - set(L1L5_KEYS) - {"notes"}
    miss = set(L1L5_KEYS) - set(v)
    if extra or miss:
        raise PnlError("attribution_L1L5 字段不符（多=%s 缺=%s）—— 必须五键 %s"
                       % (sorted(extra), sorted(miss), list(L1L5_KEYS)))
    out = {}
    for k in L1L5_KEYS:
        out[k] = _check_enum(v[k], JUDGE, "attribution_L1L5.%s(%s)" % (k, L1L5_LABEL[k]))
    notes = v.get("notes")
    if notes is not None:
        if not isinstance(notes, dict) or set(notes) - set(L1L5_KEYS):
            raise PnlError("attribution_L1L5.notes 只允许 L1~L5 键")
    out["notes"] = dict(notes or {})
    return out


def _norm_three(v):
    if not isinstance(v, dict):
        raise PnlError("attribution_三问 必填（驱动 / 赔率 / 证伪）")
    extra = set(v) - set(THREE_KEYS) - {"notes"}
    miss = set(THREE_KEYS) - set(v)
    if extra or miss:
        raise PnlError("attribution_三问 字段不符（多=%s 缺=%s）—— 必须三键 %s"
                       % (sorted(extra), sorted(miss), list(THREE_KEYS)))
    out = {k: _check_enum(v[k], JUDGE, "attribution_三问.%s" % k) for k in THREE_KEYS}
    out["notes"] = dict(v.get("notes") or {})
    return out


def _norm_verdicts(verdicts, frozen):
    """M1/M2/M3 逐项判定校验 —— **判定指标必须与下注时冻结的一致**（防后视）。"""
    if not isinstance(verdicts, dict):
        raise PnlError("verdicts 必填（M1/M2/M3 逐项判定）")
    extra = set(verdicts) - set(SLOTS)
    miss = set(SLOTS) - set(verdicts)
    if extra or miss:
        raise PnlError("verdicts 键必须恰为 %s（禁 M1M3 合并缩写，M2=板块扩散不能漏），"
                       "收到: %s" % (list(SLOTS), sorted(verdicts)))
    by_id = {p["prediction_id"]: p for p in frozen}
    out = {}
    for k in SLOTS:
        item = verdicts[k]
        if not isinstance(item, dict):
            raise PnlError("verdicts.%s 必须是对象（含 metric / data_source / verdict）" % k)
        extra = set(item) - set(VERDICT_ITEM_FIELDS)
        miss = {"metric", "data_source", "verdict"} - set(item)
        if extra or miss:
            raise PnlError("verdicts.%s 字段不符（多=%s 缺=%s）—— 允许 %s"
                           % (k, sorted(extra), sorted(miss), list(VERDICT_ITEM_FIELDS)))
        if item.get("prediction_id") not in (None, k):
            raise PnlError("verdicts.%s.prediction_id=%r 与键名不符"
                           % (k, item.get("prediction_id")))
        fz = by_id.get(k)
        if fz is None:
            raise PnlError("决策行未冻结槽位 %s —— 未冻结的项不得事后判（防后视）" % k)
        for f in ("metric", "data_source"):
            if item[f] != fz[f]:
                raise PnlError("METRIC_DRIFT：verdicts.%s.%s=%r 与下注时冻结的 %r 不一致 —— "
                               "换了指标再判自己赢 = 后视污染，一律拒" % (k, f, item[f], fz[f]))
        out[k] = {"prediction_id": k, "metric": item["metric"],
                  "data_source": item["data_source"],
                  "verdict": _check_enum(item["verdict"], VERDICTS, "verdicts.%s.verdict" % k),
                  "observed": item.get("observed"), "note": item.get("note")}
    return out


# ── 写入：结算一笔 ────────────────────────────────────────────────────────────
def write_pnl(decision_id, outcome, *, verdicts, attribution_L1L5, attribution_三问,
              entry_price=None, exit_price=None, exit_kind=None, exit_date=None,
              capital=None, pnl_pct=None,
              pnl_amount=None, fee_amount=None, net_amount=None,
              hold_days=None, settle_date=None, day_low=None,
              day_high=None, settled_at=None, base=None):
    """结算一笔决策（append-only）。返回写入行 + 审计信息。

    前置：该 decision_id 必须是**本模块账本同一根目录**里的一条 `action=BUY` 决策
          （WAIT/NO 不产生资金结果 → 不结账）。
    幂等：同一 decision_id 已结算且内容相同 → EXISTS；内容不同 → 拒（**不得改写已结算结果**）。
    自动算：pnl_pct（未传）→ (exit−entry)/entry×100；pnl_amount（未传）→ capital × pnl_pct/100；
            net_amount（未传）→ pnl_amount − fee_amount。
    v1.2 语义（P0 审计裁决 2026-10-10）：
      · outcome = **最终盈亏结果**：WIN/LOSS/FLAT 必须与 sign(pnl_pct) 一致（守卫恢复）；
        「止损触发但卖出价仍高于入场价」的真实情形 → outcome=WIN + exit_kind=STOP。
      · exit_kind = 退出原因事件：STOP（止损触发）/ TARGET（目标触发）/ WINDOW_END
        （窗口末日收盘离场）/ NOT_FILLED（未成交）；与 outcome **分开记录**。
      · exit_date = 退出日（事件发生日）；NOT_FILLED 时两者均无。
    v1.3 语义（任务9#1 成本建模 2026-10-10）：
      · pnl_amount 保持**毛盈亏**（纯价差）语义不变；
      · fee_amount = 该笔全部费用（买入侧+卖出侧，由结算方按正式费率参数计算后传入；
        非负）；NOT_FILLED → fee=0、net=0；
      · net_amount = 费后净盈亏，自动算 = pnl_amount − fee_amount；传入则机械比对。
        净值曲线（net_curve）消费 net_amount——费用进入正式净值账本，报告层不再自扣。
    """
    dec = DEC.load_decision(decision_id, base=base)      # 不存在 / 轴不符 → 由 decision 层拒
    sim = bool(dec["sim"])
    if dec["action"] != "BUY":
        raise PnlError("决策 %s 的 action=%s —— 只有 BUY 产生资金结果，WAIT/NO 不结账"
                       % (decision_id, dec["action"]))
    if dec["actual_position"] is None:
        raise PnlError("决策 %s 尚无 EXECUTION 事件（实际成交仓未知）—— 先记成交再结算"
                       % decision_id)
    oc = _check_enum(outcome, OUTCOMES, "outcome")
    sd = _check_date(settle_date, "settle_date") if settle_date else _check_date(dec["date"])
    if sd < dec["date"]:
        raise PnlError("settle_date=%s 早于决策日 %s（时间不得倒流）" % (sd, dec["date"]))
    hd = hold_days
    if hd is not None:
        if isinstance(hd, bool) or not isinstance(hd, int) or hd < 0:
            raise PnlError("hold_days 是持有交易日数（非负整数），收到: %r" % (hold_days,))

    frozen = dec["thesis"]["prediction"]
    # ★ v1.4（2026-10-19）：`NOT_FILLED` = **机械终态**，不产生判定。
    #   判定（M1/M2/M3 verdicts + L1L5/三问归因）是对「已成交笔的结果」作出的；
    #   未成交笔没有成交、没有盈亏、没有结果可判 ⇒ 三者全不给时按「不判」落账，
    #   M 槽位与归因字段存 None（字段仍在，只是无值），**不得用占位判定冒充**。
    #   向后兼容：旧调用方若照旧给全三件，仍走原校验路径（行为完全不变）。
    if (oc == "NOT_FILLED" and verdicts is None
            and attribution_L1L5 is None and attribution_三问 is None):
        vd = {"M1": None, "M2": None, "M3": None}
        l1 = None
        th = None
    else:
        vd = _norm_verdicts(verdicts, frozen)
        l1 = _norm_l1l5(attribution_L1L5)
        th = _norm_three(attribution_三问)

    e = _check_num(entry_price, "entry_price", allow_none=True, positive=True)
    x = _check_num(exit_price, "exit_price", allow_none=True, positive=True)
    cap = _check_num(capital, "capital", allow_none=True, non_negative=True) or 0.0

    if day_low is not None or day_high is not None:
        _assert_fillable(e, day_low, day_high)

    if oc == "NOT_FILLED":
        if e is not None or x is not None:
            raise PnlError("outcome=NOT_FILLED（未在场/未成交）时 entry_price / exit_price 必须为 null")
        if exit_kind not in (None, "NOT_FILLED") or exit_date is not None:
            raise PnlError("outcome=NOT_FILLED 时 exit_kind 只能为 NOT_FILLED、exit_date 必须为 null")
        exit_kind = "NOT_FILLED"
        cap = 0.0
        if pnl_pct is None:
            pnl_pct = 0.0
        if pnl_amount is None:
            pnl_amount = 0.0
    else:
        if e is None or x is None:
            raise PnlError("outcome=%s 时 entry_price / exit_price 必填（结算必须有成交价）" % oc)
        if cap <= 0:
            raise PnlError("outcome=%s 时 capital（该笔投入金额）必须 > 0 —— "
                           "金额盈亏没有基数就与手算对不上" % oc)
        if exit_kind not in ("STOP", "TARGET", "WINDOW_END"):
            raise PnlError("exit_kind 必填且 ∈ STOP/TARGET/WINDOW_END（退出原因与盈亏分开记录，"
                           "v1.2 契约），收到: %r" % (exit_kind,))
        xd = _check_date(exit_date, "exit_date")
        if xd < dec["date"]:
            raise PnlError("exit_date=%s 早于决策日 %s（退出不得早于决策）" % (xd, dec["date"]))

    pct = _check_num(pnl_pct, "pnl_pct", allow_none=True)
    if oc != "NOT_FILLED":
        exp_pct = (x - e) / e * 100.0
        if pct is None:
            pct = round(exp_pct, 6)
        elif abs(pct - exp_pct) > max(PCT_TOL, abs(exp_pct) * 1e-4):
            raise PnlError("pnl_pct=%s 与 (exit−entry)/entry×100=%s 不一致（机械可算的就不许手填错）"
                           % (pct, round(exp_pct, 6)))
        # v1.2（2026-10-10）：恢复 outcome=最终盈亏结果的符号一致性守卫。
        # 「止损触发但盈利」不再是 LOSS 配正 pct——它落 outcome=WIN + exit_kind=STOP
        # （触发事件与盈亏结果分开记录，见模块头 v1.2）。
        if oc == "WIN" and pct <= 0:
            raise PnlError("outcome=WIN 但 pnl_pct=%s（最终盈亏结果不得配非正收益）" % pct)
        if oc == "LOSS" and pct >= 0:
            raise PnlError("outcome=LOSS 但 pnl_pct=%s（最终盈亏结果不得配非负收益；"
                           "止损触发但盈利请落 outcome=WIN + exit_kind=STOP）" % pct)
        if oc == "FLAT" and abs(pct) > max(PCT_TOL, abs(exp_pct) * 1e-4):
            raise PnlError("outcome=FLAT 但 pnl_pct=%s 非零（FLAT 必须价格持平）" % pct)
    else:
        pct = 0.0

    amt = _check_num(pnl_amount, "pnl_amount", allow_none=True)
    exp_amt = cap * pct / 100.0
    if amt is None:
        amt = round(exp_amt, 6)
    else:
        tol = max(AMT_TOL, abs(exp_amt) * AMT_TOL)
        if abs(amt - exp_amt) > tol:
            raise PnlError("pnl_amount=%s 与 capital×pnl_pct/100=%s 不一致（净值曲线的唯一来源）"
                           % (amt, round(exp_amt, 6)))

    # v1.3：费用与费后净盈亏（NOT_FILLED 恒 0；净 = 毛 − 费，机械可算即比对）
    fee = _check_num(fee_amount, "fee_amount", allow_none=True, non_negative=True)
    if oc == "NOT_FILLED":
        fee = 0.0
        net = 0.0
    else:
        if fee is None:
            fee = 0.0
        net = _check_num(net_amount, "net_amount", allow_none=True)
        exp_net = amt - fee
        if net is None:
            net = round(exp_net, 6)
        else:
            tol = max(AMT_TOL, abs(exp_net) * AMT_TOL)
            if abs(net - exp_net) > tol:
                raise PnlError("net_amount=%s 与 pnl_amount−fee_amount=%s 不一致"
                               "（费后净盈亏必须机械可算）" % (net, round(exp_net, 6)))

    row = {"schema_version": SCHEMA_VERSION, "decision_id": decision_id, "sim": sim,
           "outcome": oc, "entry_price": e, "exit_price": x,
           "exit_kind": exit_kind, "exit_date": exit_date, "capital": cap,
           "pnl_amount": amt, "fee_amount": fee, "net_amount": net, "pnl_pct": pct,
           "attribution_L1L5": l1, "attribution_三问": th,
           "M1": vd["M1"], "M2": vd["M2"], "M3": vd["M3"],
           "hold_days": hd, "settle_date": sd, "settled_at": settled_at or _now()}
    if tuple(row.keys()) != tuple(FIELDS):
        raise PnlError("行字段不符：期望 %s，收到 %s" % (list(FIELDS), list(row.keys())))

    for old in _read_rows(sim, base):
        if old.get("decision_id") != decision_id:
            continue
        same = all(old.get(k) == row.get(k) for k in FIELDS if k != "settled_at")
        if same:
            return {"created": False, "reused": True, "reason": "EXISTS",
                    "row": dict(old), "orders_created": 0, "axis": axis_name(sim)}
        raise PnlError("决策 %s 已结算且内容不同 —— 已结算结果不得改写（append-only）；"
                       "确需更正走「作废事件 + 新一笔」，不覆盖历史" % decision_id)
    _append(row, sim, base)
    return {"created": True, "reused": False, "row": dict(row), "orders_created": 0,
            "axis": axis_name(sim), "ledger": ledger_path(sim, base)}


def _assert_fillable(entry, day_low, day_high):
    """可成交性（0.6 口径）：做多 entry 必须落在当日 [low, high] 区间内。"""
    if entry is None:
        return True
    lo = _check_num(day_low, "day_low", allow_none=True, positive=True)
    hi = _check_num(day_high, "day_high", allow_none=True, positive=True)
    if lo is not None and entry < lo - 1e-9:
        raise PnlError("entry_price=%s 低于当日最低 %s —— 该价位买不到，不得记账" % (entry, lo))
    if hi is not None and entry > hi + 1e-9:
        raise PnlError("entry_price=%s 高于当日最高 %s —— 该价位买不到，不得记账" % (entry, hi))
    return True


def fillable(entry_price, day_low, day_high, one_board_unfilled=False):
    """纯函数（0.6 复用）：返回 {"fillable":bool,"reason":str} —— 一字板未排到不判成交。"""
    try:
        if one_board_unfilled:
            return {"fillable": False, "reason": "一字板未排到，判未成交（未在场）"}
        _assert_fillable(entry_price, day_low, day_high)
        return {"fillable": True, "reason": "OK"}
    except PnlError as e:
        return {"fillable": False, "reason": str(e)}


# ── 读取与统计 ────────────────────────────────────────────────────────────────
def read_pnl(decision_id, sim=None, base=None):
    """读一笔结算结果（轴由 decision_id 前缀判定，可显式校验）。"""
    prefix = DEC.axis_of(decision_id)
    if sim is not None and bool(sim) != prefix:
        raise PnlError("AXIS_MISMATCH：%s 属 %s 轴，显式 sim=%s —— 拒绝跨轴读取"
                       % (decision_id, axis_name(prefix), axis_name(bool(sim))))
    hits = [r for r in _read_rows(prefix, base) if r.get("decision_id") == decision_id]
    if not hits:
        return None
    if len(hits) > 1:
        raise PnlError("同 decision_id 出现 %d 条结算行（账本被篡改）: %s"
                       % (len(hits), decision_id))
    return dict(hits[0])


def iter_pnl(sim, as_of=None, outcome=None, base=None):
    """按轴遍历结算行（**sim 必填**：统计接口不做默认轴）。"""
    if not isinstance(sim, bool):
        raise PnlError("iter_pnl 的 sim 必须显式布尔（不做默认轴，防混轴统计）")
    out = []
    for r in _read_rows(sim, base):
        if as_of and r.get("settle_date") != _check_date(as_of, "as_of"):
            continue
        if outcome and r.get("outcome") != outcome:
            continue
        if bool(r.get("sim")) is not sim:
            continue
        out.append(dict(r))
    return sorted(out, key=lambda r: (r["settle_date"], r["decision_id"]))


def net_curve(sim, initial_capital=None, as_of=None, base=None):
    """净值曲线（0.4 主用途）：按 settle_date 升序累计**费后净盈亏**（v1.3）。

    每点金额 = net_amount（费后）；schema≤2 的旧行无该字段 → 回退 pnl_amount
    （旧口径 = 未计费，如实回退不补造费用）。
    `NOT_FILLED` 计入曲线点但标 executed=False（金额 0，不改变净值）—— 记事实，不假装成交。
    """
    if not isinstance(sim, bool):
        raise PnlError("net_curve 的 sim 必须显式布尔")
    init = _check_num(initial_capital, "initial_capital", allow_none=True, non_negative=True)
    rows = iter_pnl(sim, as_of=as_of, base=base)
    cum = 0.0
    cum_gross = 0.0
    total_fee = 0.0
    pts = []
    for r in rows:
        net = r.get("net_amount")
        if net is None:                       # schema≤2 旧行：未计费口径
            net = r["pnl_amount"]
        fee = r.get("fee_amount")
        if fee is None:
            fee = 0.0
        cum += net
        cum_gross += r["pnl_amount"]
        total_fee += fee
        pts.append({"settle_date": r["settle_date"], "decision_id": r["decision_id"],
                    "executed": r["outcome"] != "NOT_FILLED",
                    "pnl_amount": r["pnl_amount"], "fee_amount": fee,
                    "net_amount": net, "cum_net": round(cum, 6),
                    "cum_pnl": round(cum, 6),
                    "equity": None if init is None else round(init + cum, 6),
                    "outcome": r["outcome"]})
    return {"axis": axis_name(sim), "initial_capital": init, "n": len(pts),
            "total_pnl": round(cum, 6),                 # 费后（净值口径，v1.3）
            "total_pnl_gross": round(cum_gross, 6),     # 费前毛盈亏（对账口径）
            "total_fee": round(total_fee, 6),
            "equity": None if init is None else round(init + cum, 6), "points": pts}


def summary(sim, as_of=None, base=None):
    """判断质量 vs 盈亏（设计 §327：**判断质量不判驱动对错，判机制链命中程度**）。"""
    rows = iter_pnl(sim, as_of=as_of, base=base)
    filled = [r for r in rows if r["outcome"] != "NOT_FILLED"]
    wins = [r for r in filled if r["outcome"] == "WIN"]
    verdict_cnt = {v: 0 for v in VERDICTS}
    for r in rows:
        for k in SLOTS:
            vd = r.get(k) or {}
            if vd.get("verdict") in verdict_cnt:
                verdict_cnt[vd["verdict"]] += 1
    l1 = {k: {j: 0 for j in JUDGE} for k in L1L5_KEYS}
    for r in rows:
        for k in L1L5_KEYS:
            v = (r.get("attribution_L1L5") or {}).get(k)
            if v in l1[k]:
                l1[k][v] += 1
    return {"axis": axis_name(sim), "n": len(rows), "filled": len(filled),
            "win": len(wins), "loss": sum(1 for r in filled if r["outcome"] == "LOSS"),
            "not_filled": len(rows) - len(filled),
            "win_rate": (round(len(wins) / len(filled), 4) if filled else None),
            "pnl_total": round(sum(r["pnl_amount"] for r in rows), 6),
            "fee_total": round(sum(r.get("fee_amount") or 0.0 for r in rows), 6),
            "net_total": round(sum(r.get("net_amount")
                                   if r.get("net_amount") is not None
                                   else r["pnl_amount"] for r in rows), 6),
            "verdict_dist": verdict_cnt, "l1l5_dist": l1}


def _scan_source():
    """静态扫描本文件：返回 (imported, open_modes, defs)。open_modes 项 = (mode, lineno, fn)。"""
    import ast
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = ASTW.func_table(tree)         # 函数区间表 = astwin 唯一实现（尾界含多行尾部）
    imported = set()
    modes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        if isinstance(node, ast.Call):
            fn = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if fn == "open":
                m = None
                for a in node.args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        m = a.value
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        m = kw.value.value
                owner = ASTW.owner_of(funcs, node.lineno)
                modes.append((m, node.lineno, owner))
    return imported, modes, [nm for _, _, nm in funcs]


def _selftest():
    import hashlib
    import shutil
    import tempfile
    sys.path.insert(0, os.path.normpath(os.path.dirname(os.path.abspath(__file__))))
    import thesis as TH
    ok = fail = 0
    fails = []

    def T(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [OK] %s" % name)
        else:
            fail += 1
            fails.append(name)
            print("  [!!] %s" % name)

    def raises(fn, *a, **kw):
        try:
            fn(*a, **kw)
            return False
        except (PnlError, DEC.DecisionError, TH.ThesisError, TypeError):
            return True

    def _fp(p):
        if not os.path.exists(p):
            return None
        return hashlib.md5(io.open(p, "rb").read()).hexdigest()

    real = (_fp(ledger_path(False)), _fp(ledger_path(True)))
    tmp = tempfile.mkdtemp(prefix="ta_pnl_selftest_")
    base = os.path.join(tmp, "generated")
    th_p = os.path.join(tmp, "thesis.md")
    D1, D2, D3, D4 = "2026-10-08", "2026-10-12", "2026-10-13", "2026-10-14"
    ENV = {"regime": "过热", "signals": {"limit_up_count": 62}, "leg": "INTRADAY",
           "source": "EXPLICIT"}
    FIVE = {"驱动存在": "板块级订单落地", "当前证据": "公告 + 资金同步流入",
            "延续机制": "订单排产 → 业绩预期", "失效条件": "公告被澄清",
            "剩余预期": "3 日内板块扩散"}

    def chain():
        return {"evidence": ["公告：公司获批量订单"], "inference": ["产业催化 → A 类驱动"],
                "five": dict(FIVE),
                "prediction": [
                    {"prediction_id": "M1", "metric": "个股 T+3 涨幅",
                     "data_source": "snapshot.daily_quote", "baseline": "T 日收盘",
                     "direction": "UP", "threshold": 3.0, "condition": "T+3 内涨幅>=3%",
                     "window": "3D"},
                    {"prediction_id": "M2", "metric": "板块扩散家数",
                     "data_source": "snapshot.limit_up", "baseline": "T 日家数",
                     "direction": "UP", "threshold": 3, "condition": "扩散家数>=3",
                     "window": "3D"},
                    {"prediction_id": "M3", "metric": "成交额维持",
                     "data_source": "snapshot.daily_quote", "baseline": "T 日成交额",
                     "direction": "FLAT", "threshold": -20.0, "condition": "成交额不低于 -20%",
                     "window": "3D"}]}

    def vd(m1="SUPPORT", m2="MIXED", m3="SUPPORT"):
        return {"M1": {"metric": "个股 T+3 涨幅", "data_source": "snapshot.daily_quote",
                       "verdict": m1, "observed": 10.0},
                "M2": {"metric": "板块扩散家数", "data_source": "snapshot.limit_up",
                       "verdict": m2, "observed": 4},
                "M3": {"metric": "成交额维持", "data_source": "snapshot.daily_quote",
                       "verdict": m3}}

    L1 = {"L1": "OK", "L2": "OK", "L3": "OK", "L4": "WRONG", "L5": "WRONG",
          "notes": {"L4": "持有窗口偏短"}}
    THQ = {"驱动": "OK", "赔率": "WRONG", "证伪": "OK"}

    TH.init_file(th_p)
    t1 = TH.create_thesis(D1, "A", "政策催化：板块级订单落地", code="sh600000",
                          candidate_id="CD-20261008-001", path=th_p)
    TH_ID = t1["id"]
    try:
        def mk_buy(code, sim=False, as_of=D1):
            r = DEC.write_decision(as_of, code, "BUY", TH_ID, chain(), sim=sim, base=base,
                                   thesis_path=th_p, entry_limit=10.0, stop_line=9.4,
                                   target=11.5, window=3, position_request=0.2, env=ENV)
            DEC.append_risk_plan(r["id"], 0.15, as_of=as_of, base=base)
            DEC.append_execution(r["id"], 0.15, "filled", as_of=as_of, avg_price=10.0, base=base)
            return r["id"]

        print("== A. 契约 ==")
        T("A1 行字段 21 项（v1.3：+fee_amount/+net_amount）", len(FIELDS) == 21)
        T("A2 outcome 四枚举（含 NOT_FILLED = 未在场/未成交）",
          OUTCOMES == ("WIN", "LOSS", "FLAT", "NOT_FILLED"))
        T("A2b exit_kind 四枚举（STOP/TARGET/WINDOW_END/NOT_FILLED · 事件与盈亏分离）",
          EXIT_KINDS == ("STOP", "TARGET", "WINDOW_END", "NOT_FILLED"))
        T("A3 verdict 三枚举 SUPPORT/MIXED/REFUTE", VERDICTS == ("SUPPORT", "MIXED", "REFUTE"))
        T("A4 L1-L5 = 环境/方向/选股/时间/执行（机制归因五环）",
          [L1L5_LABEL[k] for k in L1L5_KEYS] == ["环境", "方向", "选股", "时间", "执行"])
        T("A5 三问 = 驱动/赔率/证伪", THREE_KEYS == ("驱动", "赔率", "证伪"))
        T("A6 轴分流：LIVE 落 base/、SIM 落 base/sim/",
          ledger_path(False, base) == os.path.join(base, LEDGER_NAME)
          and ledger_path(True, base) == os.path.join(base, SIM_DIR, LEDGER_NAME))

        print("== B. 写入与手算一致 ==")
        DID = mk_buy("sh600000")
        r = write_pnl(DID, "WIN", verdicts=vd(), attribution_L1L5=L1, attribution_三问=THQ,
                      entry_price=10.0, exit_price=11.0, exit_kind="WINDOW_END",
                      exit_date="2026-10-12", capital=15000.0, hold_days=3,
                      base=base)
        T("B1 首写 created=True", r["created"] is True)
        T("B2 pnl_pct 自动算 = (11-10)/10×100 = 10", r["row"]["pnl_pct"] == 10.0)
        T("B3 pnl_amount 自动算 = capital×pct/100 = 1500", r["row"]["pnl_amount"] == 1500.0)
        T("B3b exit_kind/exit_date 落账（v1.2）",
          r["row"]["exit_kind"] == "WINDOW_END" and r["row"]["exit_date"] == "2026-10-12")
        T("B3c v1.3 未传费 → fee=0、net=毛盈亏 1500（未计费口径如实）",
          r["row"]["fee_amount"] == 0.0 and r["row"]["net_amount"] == 1500.0)
        # v1.3：费用进入正式净值账本——fee 传入 net 自动算，net 传入机械比对
        fdid = mk_buy("sh600045")
        fr = write_pnl(fdid, "LOSS", verdicts=vd(), attribution_L1L5=L1,
                       attribution_三问=THQ, entry_price=10.2, exit_price=9.8,
                       exit_kind="STOP", exit_date="2026-10-12", capital=200000.0,
                       fee_amount=586.27, hold_days=2, base=base)
        T("B3d v1.3 fee 传入 → net 自动算 = 行内毛盈亏 − 586.27",
          abs(fr["row"]["net_amount"] - (fr["row"]["pnl_amount"] - 586.27)) < 1e-6)
        T("B3e fee 非负校验：负费用 → 拒（费用不是收入）",
          raises(write_pnl, mk_buy("sh600046"), "WIN", verdicts=vd(),
                 attribution_L1L5=L1, attribution_三问=THQ, entry_price=10.0,
                 exit_price=11.0, exit_kind="WINDOW_END", exit_date="2026-10-12",
                 capital=1000.0, fee_amount=-1.0, base=base))
        T("B3f net 与 毛−费 不一致 → 拒（机械可算的不许手填错）",
          raises(write_pnl, mk_buy("sh600047"), "WIN", verdicts=vd(),
                 attribution_L1L5=L1, attribution_三问=THQ, entry_price=10.0,
                 exit_price=11.0, exit_kind="WINDOW_END", exit_date="2026-10-12",
                 capital=1000.0, fee_amount=10.0, net_amount=999.0, base=base))
        T("B4 轴由决策派生（SIM 决策 → SIM 轴）", r["axis"] == "LIVE")
        T("B5 传入 pnl_pct 与算式一致 → 通过",
          write_pnl(DID, "WIN", verdicts=vd(), attribution_L1L5=L1, attribution_三问=THQ,
                    entry_price=10.0, exit_price=11.0, exit_kind="WINDOW_END",
                    exit_date="2026-10-12", capital=15000.0, hold_days=3,
                    base=base)["reason"] == "EXISTS")
        T("B6 传入 pnl_pct 与算式不符 → 拒（机械可算的不许手填错）",
          raises(write_pnl, DID, "WIN", verdicts=vd(), attribution_L1L5=L1,
                 attribution_三问=THQ, entry_price=10.0, exit_price=11.0,
                 exit_kind="WINDOW_END", exit_date="2026-10-12", capital=15000.0,
                 pnl_pct=99.0, base=base))
        T("B7 outcome 与收益符号矛盾（WIN 配负收益）→ 拒",
          raises(write_pnl, DID, "WIN", verdicts=vd(), attribution_L1L5=L1,
                 attribution_三问=THQ, entry_price=11.0, exit_price=10.0,
                 exit_kind="WINDOW_END", exit_date="2026-10-12", capital=1000.0,
                 base=base))
        # v1.2：止损触发但卖出价仍高于入场价（低开便宜成交后跳空穿止损以 open 卖出）
        # → outcome=WIN（最终盈亏结果）+ exit_kind=STOP（触发事件）——分开记录。
        T("B7b v1.2 止损触发但盈利 → outcome=WIN + exit_kind=STOP 放行（事件与结果分离）",
          write_pnl(mk_buy("sh600042"), "WIN", verdicts=vd(), attribution_L1L5=L1,
                    attribution_三问=THQ, entry_price=9.43, exit_price=9.69,
                    exit_kind="STOP", exit_date="2026-10-12",
                    capital=10000.0, hold_days=1, base=base)["row"]["pnl_pct"] > 0)
        T("B7c v1.2 LOSS 配非负收益 → 拒（符号守卫恢复；止损触发但盈利须落 WIN+STOP）",
          raises(write_pnl, mk_buy("sh600043"), "LOSS", verdicts=vd(),
                 attribution_L1L5=L1, attribution_三问=THQ, entry_price=9.43,
                 exit_price=9.69, exit_kind="STOP", exit_date="2026-10-12",
                 capital=10000.0, base=base))
        T("B7d exit_kind 缺失（有价结算）→ 拒（v1.2 退出原因必填）",
          raises(write_pnl, mk_buy("sh600044"), "WIN", verdicts=vd(),
                 attribution_L1L5=L1, attribution_三问=THQ, entry_price=10.0,
                 exit_price=11.0, capital=1000.0, base=base))
        T("B8 未成交（NOT_FILLED）带价格 → 拒",
          raises(write_pnl, DID, "NOT_FILLED", verdicts=vd(), attribution_L1L5=L1,
                 attribution_三问=THQ, entry_price=10.0, capital=1000.0, base=base))
        n0 = len(iter_pnl(False, base=base))
        wdid = DEC.write_decision(D1, "sz000001", "WAIT", TH_ID, chain(), sim=False,
                                  base=base, thesis_path=th_p, env=ENV)["id"]
        T("B9 WAIT 决策不结账 → 拒（只有 BUY 产生资金结果）",
          raises(write_pnl, wdid, "FLAT", verdicts=vd(), attribution_L1L5=L1,
                 attribution_三问=THQ, capital=1000.0, base=base))
        T("B10 拒绝后账本行数不变", len(iter_pnl(False, base=base)) == n0)
        T("B11 改内容重结算 → 拒（不改写已结算结果）",
          raises(write_pnl, DID, "LOSS", verdicts=vd(), attribution_L1L5=L1,
                 attribution_三问=THQ, entry_price=10.0, exit_price=10.5,
                 exit_kind="WINDOW_END", exit_date="2026-10-12", capital=15000.0,
                 base=base))
        T("B12 不存在的 decision_id → 拒", raises(write_pnl, "DEC-20260101-001", "FLAT",
                                                verdicts=vd(), attribution_L1L5=L1,
                                                attribution_三问=THQ, capital=1.0, base=base))
        T("B13 可成交性：entry 不在当日区间 → 拒",
          raises(write_pnl, DID, "WIN", verdicts=vd(), attribution_L1L5=L1,
                 attribution_三问=THQ, entry_price=9.0, exit_price=11.0, capital=1000.0,
                 base=base, day_low=9.8, day_high=10.5))

        print("== C. 防后视：判定指标必须与下注冻结一致 ==")
        d2 = mk_buy("sh600519")
        T("C1 三槽位齐全 → 通过",
          write_pnl(d2, "WIN", verdicts=vd(), attribution_L1L5=L1, attribution_三问=THQ,
                    entry_price=10.0, exit_price=11.0, exit_kind="TARGET",
                    exit_date="2026-10-12", capital=1000.0, base=base)["created"])
        T("C2 缺 M2 → 拒（禁 M1M3 合并缩写，板块扩散不能漏）",
          raises(write_pnl, d2, "WIN", verdicts={"M1": vd()["M1"], "M3": vd()["M3"]},
                 attribution_L1L5=L1, attribution_三问=THQ, entry_price=10.0, exit_price=11.0,
                 capital=1000.0, base=base))
        bad = vd()
        bad["M1"]["metric"] = "个股 T+5 涨幅"
        T("C3 METRIC_DRIFT：换指标再判自己赢 → 拒",
          raises(write_pnl, d2, "WIN", verdicts=bad, attribution_L1L5=L1,
                 attribution_三问=THQ, entry_price=10.0, exit_price=11.0, capital=1000.0,
                 base=base))
        bad2 = vd()
        bad2["M2"]["data_source"] = "snapshot.news"
        T("C4 数据源不一致 → 拒", raises(write_pnl, d2, "WIN", verdicts=bad2,
                                       attribution_L1L5=L1, attribution_三问=THQ,
                                       entry_price=10.0, exit_price=11.0, capital=1000.0,
                                       base=base))
        bad3 = vd()
        bad3["M3"]["verdict"] = "YES"
        T("C5 verdict 非法值 → 拒", raises(write_pnl, d2, "WIN", verdicts=bad3,
                                          attribution_L1L5=L1, attribution_三问=THQ,
                                          entry_price=10.0, exit_price=11.0, capital=1000.0,
                                          base=base))
        bad4 = vd()
        bad4["M1"]["prediction_id"] = "M3"
        T("C6 prediction_id 与键名不符 → 拒",
          raises(write_pnl, d2, "WIN", verdicts=bad4, attribution_L1L5=L1,
                 attribution_三问=THQ, entry_price=10.0, exit_price=11.0, capital=1000.0,
                 base=base))
        T("C7 判定落盘为独立 M1/M2/M3 三列（不是合并项）",
          isinstance(read_pnl(DID, base=base)["M1"], dict)
          and read_pnl(DID, base=base)["M1"]["prediction_id"] == "M1")

        print("== D. 归因结构 ==")
        T("D1 L1L5 五键齐全 + notes 落盘",
          set(read_pnl(DID, base=base)["attribution_L1L5"]) == set(L1L5_KEYS) | {"notes"})
        T("D2 L1L5 缺 L4 → 拒",
          raises(write_pnl, d2, "WIN", verdicts=vd(),
                 attribution_L1L5={"L1": "OK", "L2": "OK", "L3": "OK", "L5": "OK"},
                 attribution_三问=THQ, entry_price=10.0, exit_price=11.0, capital=1.0,
                 base=base))
        T("D3 L1L5 值非法 → 拒",
          raises(write_pnl, d2, "WIN", verdicts=vd(),
                 attribution_L1L5=dict(L1, L4="MAYBE"), attribution_三问=THQ,
                 entry_price=10.0, exit_price=11.0, capital=1.0, base=base))
        T("D4 三问三键齐全",
          set(read_pnl(DID, base=base)["attribution_三问"]) == set(THREE_KEYS) | {"notes"})
        T("D5 三问缺「证伪」→ 拒",
          raises(write_pnl, d2, "WIN", verdicts=vd(), attribution_L1L5=L1,
                 attribution_三问={"驱动": "OK", "赔率": "OK"}, entry_price=10.0,
                 exit_price=11.0, capital=1.0, base=base))

        print("== E. 严格分层：结算不碰决策账 ==")
        lp = DEC.ledger_path(False, base)
        before = io.open(lp, "rb").read()
        d3 = mk_buy("sh600036")
        before_settle = io.open(lp, "rb").read()
        write_pnl(d3, "LOSS", verdicts=vd("REFUTE"), attribution_L1L5=L1, attribution_三问=THQ,
                  entry_price=10.0, exit_price=9.5, exit_kind="WINDOW_END",
                  exit_date="2026-10-13", capital=20000.0, hold_days=2, base=base)
        T("E1 结算一笔后决策账本逐字节未变（分层的执行点）",
          io.open(lp, "rb").read() == before_settle)
        T("E2 DECISION 行 outcome 仍为 null（结果永不回填决策行）",
          all(d["outcome"] is None for d in DEC.iter_decisions(False, base=base)))
        T("E3 结算行不含判断类字段（thesis / env / driver）—— 结果表只记结果",
          not (set(read_pnl(d3, base=base)) & {"thesis", "thesis_state", "env", "driver",
                                              "call_chain"}))
        sid = mk_buy("sh600000", sim=True)
        sr = write_pnl(sid, "WIN", verdicts=vd(), attribution_L1L5=L1, attribution_三问=THQ,
                       entry_price=10.0, exit_price=11.0, exit_kind="TARGET",
                       exit_date="2026-10-12", capital=1000.0, base=base)
        T("E4 SIM 决策的结果落 SIM 轴（不会污染 LIVE 净值）",
          sr["axis"] == "SIM" and read_pnl(sid, base=base) is not None
          and all(x["decision_id"] != sid for x in iter_pnl(False, base=base)))
        T("E5 读接口跨轴显式 sim 不符 → AXIS_MISMATCH 拒",
          raises(read_pnl, sid, False, base=base))
        T("E6 LIVE 账本新增行只来自 LIVE 决策",
          all(r["decision_id"].startswith("DEC-") for r in iter_pnl(False, base=base)))
        T("E7 结算不产生交易动作（orders_created 恒 0）",
          r["orders_created"] == 0 and sr["orders_created"] == 0)

        print("== F. 净值曲线 ==")
        d4 = mk_buy("sh601318")
        d5 = mk_buy("sz300750")
        write_pnl(d4, "WIN", verdicts=vd(), attribution_L1L5=L1, attribution_三问=THQ,
                  entry_price=10.0, exit_price=10.5, exit_kind="WINDOW_END",
                  exit_date=D3, capital=10000.0, settle_date=D3, base=base)
        write_pnl(d5, "LOSS", verdicts=vd("REFUTE"), attribution_L1L5=L1,
                  attribution_三问=THQ, entry_price=10.0, exit_price=9.8,
                  exit_kind="WINDOW_END", exit_date=D2, capital=10000.0,
                  settle_date=D2, base=base)
        c = net_curve(False, initial_capital=100000.0, base=base)
        dates = [p["settle_date"] for p in c["points"]]
        T("F1 曲线按 settle_date 升序（写入乱序也重排）",
          dates == sorted(dates) and len(dates) >= 4)
        exp = round(sum(p["net_amount"] for p in c["points"]), 6)
        T("F2 累计 = 逐笔费后净盈亏手算和（v1.3 净值口径）", c["total_pnl"] == exp)
        T("F2b 费后 = 费前 − 总费用（全曲线机械对账）",
          abs(c["total_pnl"] - (c["total_pnl_gross"] - c["total_fee"])) < 1e-6
          and c["total_fee"] > 0)
        T("F3 给了初始资金 → equity = 初始 + 累计",
          c["equity"] == round(100000.0 + exp, 6))
        nf = DEC.write_decision(D1, "sh600009", "BUY", TH_ID, chain(), sim=False, base=base,
                                thesis_path=th_p, entry_limit=10.0, stop_line=9.4,
                                target=11.5, window=3, position_request=0.2, env=ENV)
        DEC.append_execution(nf["id"], 0.0, "unfilled", as_of=D1, base=base)
        write_pnl(nf["id"], "NOT_FILLED", verdicts=vd(), attribution_L1L5=L1,
                  attribution_三问=THQ, base=base)
        c2 = net_curve(False, base=base)
        p_nf = next(p for p in c2["points"] if p["decision_id"] == nf["id"])
        T("F4 未成交计入曲线点但 executed=False 且金额 0",
          p_nf["executed"] is False and p_nf["pnl_amount"] == 0.0
          and c2["total_pnl"] == c["total_pnl"])
        # v1.3 旧账本兼容：schema≤2 行无 fee/net 字段 → 曲线回退毛口径，不补造费用
        old_row = {"schema_version": 2, "decision_id": "DEC-20260999-999", "sim": False,
                   "outcome": "WIN", "entry_price": 10.0, "exit_price": 11.0,
                   "exit_kind": "TARGET", "exit_date": D3, "capital": 1000.0,
                   "pnl_amount": 100.0, "pnl_pct": 10.0,
                   "attribution_L1L5": {}, "attribution_三问": {},
                   "M1": {"verdict": "SUPPORT"}, "M2": {"verdict": "SUPPORT"},
                   "M3": {"verdict": "SUPPORT"}, "hold_days": 1,
                   "settle_date": D3, "settled_at": "2026-10-10T00:00:00"}
        with io.open(ledger_path(False, base), "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(old_row, ensure_ascii=False) + "\n")
        c3 = net_curve(False, base=base)
        p_old = next(p for p in c3["points"] if p["decision_id"] == "DEC-20260999-999")
        T("F6 旧 schema-2 行回退毛口径：net_amount 缺失 → 按 pnl_amount 计入（不补造费用）",
          p_old["net_amount"] == 100.0 and p_old["fee_amount"] == 0.0)
        T("F5 统计接口必须显式表态轴（sim=None → 拒）",
          raises(iter_pnl, None, base=base) and raises(net_curve, None, base=base))

        print("== G. 纪律与零污染 ==")
        imported, modes, defs = _scan_source()
        T("G1 无网络/子进程导入",
          not (imported & {"requests", "urllib", "http", "socket", "subprocess", "openai"}))
        T("G2 无下单/交易类接口定义",
          not [d for d in defs if any(b in d.lower() for b in
                                      ("order", "place_", "submit", "trade", "buy", "sell"))])
        T("G3 覆盖写模式只出现在自检内（账本零 update/delete 路径）",
          all(f == "_selftest" for m, _, f in modes if m in ("w", "w+", "r+", "a+")))
        T("G4 真账本未被自检触碰（两轴指纹不变）",
          (_fp(ledger_path(False)), _fp(ledger_path(True))) == real)
        s = summary(False, base=base)
        T("G5 判断质量与盈亏分开统计（胜率 + 机制链分布）",
          s["filled"] == s["win"] + s["loss"] and sum(s["verdict_dist"].values()) == 3 * s["n"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：%s" % " / ".join(fails))
    return 1 if fail else 0


def _main(argv):
    if "--selftest" in argv:
        print("pnl.py 自检（SCHEMA_VERSION=%d · 临时文件运行）" % SCHEMA_VERSION)
        return _selftest()
    sim = "--sim" in argv
    if "--curve" in argv:
        c = net_curve(sim)
        print("净值曲线 · %s（%d 笔 · 累计 %s）" % (c["axis"], c["n"], c["total_pnl"]))
        for p in c["points"]:
            print("  %s  %s  %-4s  %+10.2f  累计 %+10.2f%s"
                  % (p["settle_date"], p["decision_id"], p["outcome"], p["pnl_amount"],
                     p["cum_pnl"], "" if p["executed"] else "  （未成交，不计净值）"))
        return 0
    print("净值账本（0.4 结算层 · append-only · SIM/LIVE 分轴）")
    for ax in (False, True):
        p = ledger_path(ax, None)
        s = summary(ax) if os.path.exists(p) else None
        print("  [%s] %s" % (axis_name(ax), p))
        if s is None:
            print("       尚未创建（无结算记录）")
        else:
            print("       结算 %d 笔（成交 %d · 未成交 %d）胜率 %s 总盈亏 %s"
                  % (s["n"], s["filled"], s["not_filled"], s["win_rate"], s["pnl_total"]))
            print("       机制链判定 %s" % s["verdict_dist"])
    print("  分层    : 本账本只记结果；当时的判断读 decision_ledger（两文件不合并）")
    print("  纪律    : append-only；写账恒不产生交易动作（orders_created=0）")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
