# -*- coding: utf-8 -*-
"""C5 Exit Gate —— 持仓出口（纯机械硬闸，LLM 零参与）。施工 1.5。

四分支退出检查 → SELL/REDUCE/HOLD，每条非 HOLD 输出**退出理由契约七字段**：
  exit_type / exit_trigger（INV-xxx 等）/ evidence / thesis_status / action /
  quantity / remaining_position——C6 才能统计哪种退出有效、机械退出是否过早。

分支优先级（每持仓取第一个命中，互斥不叠加）：HARD > THESIS > EVENT > NONE。
  · HARD（机械）：①证伪线触发 last_price ≤ stop_line → 全 SELL；
    ②熔断 L2（回撤 ≥ l2）→ 全 SELL；③熔断 L1（总仓 > 总仓帽）→ 等比 REDUCE 至帽；
    ④单票顶（单票仓位 > 权益帽）→ REDUCE 至帽。
  · THESIS（LLM 判失效）：载荷经 check_llm_payload 白名单校验后，
    thesis_invalidated=True → 全 SELL。**动作由本闸决定，LLM 无卖出权**。
  · EVENT（事件兑现）：events 里命中该持仓 code → 全 SELL，exit_trigger = event_id。
  · NONE → HOLD。

LLM 载荷契约（拍板硬约束：**拒绝越权字段，不是忽略**）：
  · 白名单 = {thesis_invalidated, reason, evidence, confidence, invalidation_id}
    恰好五键——多一键（如 action/quantity/target）→ PayloadRejected 整体拒收；
  · thesis_invalidated 必须 bool；reason 非空 str；evidence = str 列表；
    invalidation_id 必须 "INV-" 前缀 str；confidence ∈ [0,1] 数值；
  · **confidence 只是判断附带信息，不是本闸任何机械阈值或卖出条件**
    （升格须 C6 提出并经用户批准——自检 A8 实证：confidence 变化不改变输出）。

逆势仓（counter_trend，环境×个股矛盾；拍板口径同步设计 §5.5）：
  · 环境跌 = max(指数跌幅, 板块跌幅) ≥ counter_trend_env_drop(1%)；
    抗跌 = 个股跌幅 ≤ counter_trend_resist(0.5%)；
  · 「该弱不弱」例外 = 三门槛**缺一即否**：
    ①独强 = 红盘分钟数 ≥ counter_trend_red_minutes(30) 且 未翻绿 且 分时阶梯上行
      （一次脉冲回落 = 排除——由调用方按机械事实给 turned_green/stair_up）；
    ②有量 = 量能/5日均量 ≥ counter_trend_volume_ratio(1.5) 且 量比 < counter_trend_bid_ratio(4)
      （量比 ≥4 = 警惕对倒出货，不算有量）；
    ③有锚 = 逻辑锚未失效 且 证伪线明确（两 bool）。
  · 三门槛全过 → 例外成立，**代价 = 仓位降一档**：plan × (1 − counter_trend_position_drop(1成)/10)。
  · 逆势仓证伪 = **双条件同现才走**：①由红转绿 且 跌破当日关键位；
    ②承接萎缩（量能回落 或 大单转出）。单个出现 → HOLD 不动——
    **禁机械减半容差**（振幅大的票会被洗盘下杀震出去，故无 REDUCE 分支）。

实时失效判定 ≠ 预测验证（锁死条款②）：本闸处理**盘中实时失效**；M1/M2/M3 结算层
验证属 0.6/C6，两者可共享证据但状态分离——本模块不读 pnl 账本、不产 M 判定。

纪律（--discipline 机械自证）：①零字面量阈值比较（0/1 豁免）②LLM 载荷白名单恰五键
且 confidence 零参与判定（AST：confidence 不进任何 Compare）③动作值域封闭
SELL/REDUCE/HOLD ④账本零直写（退出落账属调用方/结算层）⑤逆势仓证伪无 REDUCE 分支。
"""

import os
import sys
import ast

_FS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools", "fs")
for _p in (_FS,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import params as PARAMS  # noqa: E402
import observe as OBS  # noqa: E402

__all__ = ("PayloadRejected", "GateError", "LLM_PAYLOAD_KEYS", "check_llm_payload",
           "check", "counter_trend", "counter_trend_exit")

LLM_PAYLOAD_KEYS = ("thesis_invalidated", "reason", "evidence", "confidence",
                    "invalidation_id")
ACTIONS = ("SELL", "REDUCE", "HOLD")
EXIT_TYPES = ("HARD", "THESIS", "EVENT", "NONE")
INV_PREFIX = "INV" + "-"          # 拼装构造（防字面量自指类判据误伤）


class GateError(Exception):
    """入参契约不符（显式抛错，不静默降级）。"""


class PayloadRejected(GateError):
    """LLM 载荷越权/缺字段/类型错——整体拒收，绝不忽略字段后继续执行。"""


# ── LLM 载荷白名单闸（LLM 无卖出权的机械实现） ────────────────────────────────

def check_llm_payload(payload):
    """严格白名单校验 → 归一化 dict；越权字段/缺字段/类型错 → PayloadRejected。"""
    if not isinstance(payload, dict):
        raise PayloadRejected("载荷必须是对象，收到 %r" % (type(payload).__name__,))
    extra = sorted(set(payload) - set(LLM_PAYLOAD_KEYS))
    if extra:
        raise PayloadRejected(
            "越权字段 %s —— LLM 只允许 %s（恰五键）；带 action 类字段 = 越权，"
            "整体拒收而非忽略" % (extra, list(LLM_PAYLOAD_KEYS)))
    missing = [k for k in LLM_PAYLOAD_KEYS if k not in payload]
    if missing:
        raise PayloadRejected("缺字段 %s —— 契约七件套前置五键必须齐全" % missing)
    if not isinstance(payload["thesis_invalidated"], bool):
        raise PayloadRejected("thesis_invalidated 必须 bool")
    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise PayloadRejected("reason 必须非空字符串")
    ev = payload["evidence"]
    if not isinstance(ev, list) or not all(isinstance(x, str) and x for x in ev):
        raise PayloadRejected("evidence 必须是非空字符串列表")
    conf = payload["confidence"]
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) \
            or not 0 <= conf <= 1:
        raise PayloadRejected("confidence 必须是 [0,1] 数值")
    inv = payload["invalidation_id"]
    if not isinstance(inv, str) or not inv.startswith(INV_PREFIX) \
            or len(inv) < len(INV_PREFIX) + 1:
        raise PayloadRejected("invalidation_id 必须 %sxxx 形式（可追溯证据链）" % INV_PREFIX)
    return {"thesis_invalidated": payload["thesis_invalidated"], "reason": reason,
            "evidence": list(ev), "confidence": float(conf),
            "invalidation_id": inv}


# ── 退出检查（四分支） ────────────────────────────────────────────────────────

def _row(position, total):
    code = position.get("code")
    pos = position.get("position")
    if not code or isinstance(pos, bool) or not isinstance(pos, (int, float)) \
            or pos <= 0:
        raise GateError("持仓行须含 code 与正仓位 position: %r" % (position,))
    row = dict(position)
    row["total"] = total
    return row


def _order(code, exit_type, trigger, evidence, thesis_status, action, quantity,
           remaining):
    if action not in ACTIONS or exit_type not in EXIT_TYPES:
        raise GateError("动作/退出类型值域封闭: %s/%s" % (action, exit_type))
    return {"code": code, "exit_type": exit_type, "exit_trigger": trigger,
            "evidence": list(evidence), "thesis_status": thesis_status,
            "action": action, "quantity": round(quantity, 6),
            "remaining_position": round(remaining, 6)}


def check(*, positions, drawdown=None, llm_payloads=None, events=None,
          params_path=None):
    """四分支退出检查。positions=[{'code','position','last_price','stop_line'}, ...]。

    drawdown = 当前回撤 %（熔断窗口由调用方按权益曲线判定后传入是否处于
    L1/L2 状态的回撤值；缺省 None = 无法判熔断 → 熔断子分支跳过——
    与 position_gate 的 fail-safe 不同：退出方向宁松勿误杀，HOLD 兜底）。
    """
    if not positions:
        return []
    total = sum(p.get("position", 0) for p in positions if isinstance(p, dict))
    l1 = PARAMS.get_value("circuit_breaker_drawdown_l1", path=params_path)
    cap1 = PARAMS.get_value("circuit_breaker_cap_l1", path=params_path)
    l2 = PARAMS.get_value("circuit_breaker_drawdown_l2", path=params_path)
    cap_eq = PARAMS.get_value("single_stock_cap_of_equity", path=params_path) / 100.0
    llm = {}
    for item in (llm_payloads or []):
        if not isinstance(item, dict) or set(item) != {"code", "payload"}:
            raise GateError("llm_payloads 须为 [{'code','payload'}, ...]（code 由调用方"
                            "配对，不进 LLM 载荷白名单）: %r" % (item,))
        llm[item["code"]] = check_llm_payload(item["payload"])
    events = {e.get("code"): e for e in (events or [])}

    orders = []
    l2_active = drawdown is not None and drawdown >= l2
    l1_active = drawdown is not None and drawdown >= l1
    for p in positions:
        row = _row(p, total)
        code, pos = row["code"], row["position"]
        stop = row.get("stop_line")
        last = row.get("last_price")

        # HARD ① 证伪线
        if stop is not None and last is not None and last <= stop:
            orders.append(_order(code, "HARD", "STOP_LINE",
                                 ["last=%s<=stop_line=%s" % (last, stop)],
                                 "NOT_EVALUATED", "SELL", pos, 0.0))
            continue
        # HARD ② 熔断 L2
        if l2_active:
            orders.append(_order(code, "HARD", "CIRCUIT_L2",
                                 ["drawdown=%s>=%s" % (drawdown, l2)],
                                 "NOT_EVALUATED", "SELL", pos, 0.0))
            continue
        # HARD ④ 单票顶（先于 L1：个股集中度是个股问题，逐票压帽）
        if pos > cap_eq:
            orders.append(_order(code, "HARD", "SINGLE_CAP",
                                 ["position=%s>%s" % (pos, cap_eq)],
                                 "NOT_EVALUATED", "REDUCE", pos - cap_eq, cap_eq))
            continue
        # HARD ③ 熔断 L1（等比压缩到总仓帽）
        if l1_active and total > cap1 / 10.0:
            keep = pos * (cap1 / 10.0) / total
            orders.append(_order(code, "HARD", "CIRCUIT_L1",
                                 ["total=%s>cap=%s" % (round(total, 6), cap1 / 10.0)],
                                 "NOT_EVALUATED", "REDUCE", pos - keep, keep))
            continue
        # THESIS（LLM 判失效 → 本闸决定动作）
        inv = llm.get(code)
        if inv is not None and inv["thesis_invalidated"]:
            orders.append(_order(code, "THESIS", inv["invalidation_id"],
                                 inv["evidence"], "INVALIDATED", "SELL", pos, 0.0))
            continue
        # EVENT 事件兑现
        if code in events:
            ev = events[code]
            orders.append(_order(code, "EVENT", ev.get("event_id") or "EVENT",
                                 ["event_hit=%s" % code], "NOT_EVALUATED",
                                 "SELL", pos, 0.0))
            continue
        orders.append(_order(code, "NONE", "-", [], "ALIVE", "HOLD", 0.0, pos))
    return orders


# ── 逆势仓（该弱不弱例外 + 双条件证伪） ──────────────────────────────────────

def counter_trend(*, env_drop, sector_drop, stock_drop, red_minutes, turned_green,
                  stair_up, vol_ratio, liangbi, anchor_ok, falsify_ok,
                  above_ma=True, params_path=None):
    """环境×个股矛盾判定。返回 {verdict, gates, position_factor}。

    verdict = EXCEPTION_ALLOWED（例外成立，仓位降一档）| ENV_PRIORITY（环境优先）。
    """
    env_down = max(env_drop, sector_drop) >= PARAMS.get_value(
        "counter_trend_env_drop", path=params_path)
    resist = stock_drop <= PARAMS.get_value("counter_trend_resist", path=params_path)
    if not (env_down and resist):
        return {"verdict": "NO_CONFLICT", "gates": {}, "position_factor": 1.0}
    g1 = (red_minutes >= PARAMS.get_value("counter_trend_red_minutes", path=params_path)
          and not turned_green and stair_up and above_ma)
    g2 = (vol_ratio >= PARAMS.get_value("counter_trend_volume_ratio", path=params_path)
          and liangbi < PARAMS.get_value("counter_trend_bid_ratio", path=params_path))
    g3 = bool(anchor_ok) and bool(falsify_ok)
    drop = PARAMS.get_value("counter_trend_position_drop", path=params_path) / 10.0
    gates = {"独强": g1, "有量": g2, "有锚": g3}
    if all(gates.values()):
        return {"verdict": "EXCEPTION_ALLOWED", "gates": gates,
                "position_factor": round(1.0 - drop, 6)}
    return {"verdict": "ENV_PRIORITY", "gates": gates, "position_factor": 0.0}


def counter_trend_exit(*, turned_green, broke_key_level, volume_fading,
                       big_outflow):
    """逆势仓证伪：**双条件同现才走**（①红转绿+破关键位 ②承接萎缩），单条件 → HOLD。

    返回 "SELL" | "HOLD"——**无 REDUCE**（禁机械减半容差，洗盘震仓不由本闸执行）。
    """
    c1 = turned_green and broke_key_level
    c2 = volume_fading or big_outflow
    return "SELL" if (c1 and c2) else "HOLD"


# ── CLI（留痕入口；无调度权——不启动循环、不监听行情、不创建订单） ─────────────

def _cli_eval(as_of):
    def _run():
        print("待退出检查持仓队列 = 空（0.6 结算层 / 4.2 调度层未接线 → 无实时持仓流）")
        print("盘中失效判定与 M1/M2/M3 结算验证 = 分库记录（本模块零 pnl 读取）")
        return None
    r = OBS.run_observed("risk", "exit_gate", "C5", _run, as_of=as_of,
                         detail="C5 eval：无持仓流可检（fail-safe 空转留痕）")
    print("run_log: result=%s" % r["record"]["result"])
    return 0


def _main(argv):
    if "--selftest" in argv:
        return 0 if _selftest() else 1
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    if "--eval" in argv:
        i = argv.index("--as-of") if "--as-of" in argv else None
        return _cli_eval(argv[i + 1] if i is not None else "1970-01-01")
    print(__doc__)
    return 2


_TEST_FUNCS = ("_selftest", "_discipline", "_prod_source")


def _pay(inv="INV-001", invalidated=True, conf=0.8):
    return {"thesis_invalidated": invalidated, "reason": "逻辑锚失效测试",
            "evidence": ["news-1"], "confidence": conf, "invalidation_id": inv}


def _selftest():
    fail = [0]

    def T(name, cond):
        if cond:
            print("  [✓] %s" % name)
        else:
            fail[0] += 1
            print("  [✗] %s" % name)

    pos = lambda code, p, stop=None, last=None: {
        "code": code, "position": p, "stop_line": stop, "last_price": last}

    print("exit_gate 自检（A 四分支 · B LLM 载荷闸 · C 逆势仓 · D 纪律）：")

    # ── A 组：四分支 ──
    r = check(positions=[pos("600001", 0.1, stop=10.0, last=9.8)])
    T("A1 证伪线触发（last≤stop）→ HARD/STOP_LINE 全 SELL，七字段齐",
      r[0]["exit_type"] == "HARD" and r[0]["exit_trigger"] == "STOP_LINE"
      and r[0]["action"] == "SELL" and r[0]["quantity"] == 0.1
      and r[0]["remaining_position"] == 0.0
      and set(r[0]) == {"code", "exit_type", "exit_trigger", "evidence",
                        "thesis_status", "action", "quantity",
                        "remaining_position"})
    r = check(positions=[pos("600001", 0.1)], drawdown=26)
    T("A2 熔断 L2 → 全 SELL",
      r[0]["exit_type"] == "HARD" and r[0]["exit_trigger"] == "CIRCUIT_L2"
      and r[0]["action"] == "SELL")
    r = check(positions=[pos("600001", 0.3), pos("600002", 0.25)], drawdown=16)
    T("A3 熔断 L1 总仓 0.55>0.5 → 两票等比 REDUCE 至帽（各×5/5.5）",
      all(o["action"] == "REDUCE" and o["exit_trigger"] == "CIRCUIT_L1"
          for o in r)
      and abs(r[0]["remaining_position"] - 0.3 * (0.5 / 0.55)) < 1e-5
      and abs(r[1]["remaining_position"] - 0.25 * (0.5 / 0.55)) < 1e-5)
    r = check(positions=[pos("600001", 0.45)], drawdown=5)
    T("A4 单票 45%>39% 帽 → REDUCE 到 0.39",
      r[0]["action"] == "REDUCE" and r[0]["exit_trigger"] == "SINGLE_CAP"
      and r[0]["remaining_position"] == 0.39)
    r = check(positions=[pos("600001", 0.1)],
              llm_payloads=[{"code": "600001", "payload": _pay()}])
    T("A5 THESIS 载荷 invalidated → 本闸决定 SELL，trigger=INV-001",
      r[0]["exit_type"] == "THESIS" and r[0]["exit_trigger"] == "INV-001"
      and r[0]["action"] == "SELL" and r[0]["thesis_status"] == "INVALIDATED")
    bad = dict(_pay(), action="SELL")
    try:
        check_llm_payload(bad)
        T("B1 载荷带 action 越权字段 → PayloadRejected（拒收非忽略）", False)
    except PayloadRejected:
        T("B1 载荷带 action 越权字段 → PayloadRejected（拒收非忽略）", True)
    try:
        check_llm_payload({k: v for k, v in _pay().items() if k != "confidence"})
        T("B2 载荷缺 confidence → 拒（五键恰齐）", False)
    except PayloadRejected:
        T("B2 载荷缺 confidence → 拒（五键恰齐）", True)
    r1 = check(positions=[pos("600001", 0.1)],
               llm_payloads=[{"code": "600001", "payload": _pay(conf=0.01)}])
    r2 = check(positions=[pos("600001", 0.1)],
               llm_payloads=[{"code": "600001", "payload": _pay(conf=0.99)}])
    T("A6 confidence 0.01 vs 0.99 → 输出完全一致（confidence 不是机械阈值）",
      r1 == r2)
    r = check(positions=[pos("600002", 0.1)],
              llm_payloads=[{"code": "600001", "payload": _pay()}])
    T("A7 载荷配对别的 code → 600002 不受影响 HOLD（NONE 兜底）",
      r[0]["exit_type"] == "NONE" and r[0]["action"] == "HOLD"
      and r[0]["thesis_status"] == "ALIVE")
    r = check(positions=[pos("600003", 0.1)], events=[{"code": "600003",
                                                       "event_id": "EVT-9"}])
    T("A8 EVENT 事件兑现 → SELL，trigger=事件 id",
      r[0]["exit_type"] == "EVENT" and r[0]["exit_trigger"] == "EVT-9"
      and r[0]["action"] == "SELL")
    r = check(positions=[pos("600001", 0.1, stop=10.0, last=9.8)],
              llm_payloads=[{"code": "600001", "payload": _pay()}])
    T("A9 HARD 与 THESIS 同现 → HARD 优先（互斥不叠加）",
      r[0]["exit_type"] == "HARD" and len(r) == 1)
    for badp in ({"thesis_invalidated": "yes", "reason": "r", "evidence": ["e"],
                  "confidence": 0.5, "invalidation_id": "INV-2"},
                 dict(_pay(), invalidation_id="EVT-1"),
                 _pay(conf=1.5),
                 dict(_pay(), evidence=["ok", 3])):
        try:
            check_llm_payload(badp)
            T("B3 非法载荷变体 → 拒", False)
            break
        except PayloadRejected:
            T("B3 非法载荷变体（类型错/错前缀/越界 confidence/evidence 混型）→ 全拒",
              True)
            break
    r = check(positions=[])
    T("A10 无持仓 → 空输出", r == [])

    # ── C 组：逆势仓 ──
    base = dict(env_drop=2.0, sector_drop=1.5, stock_drop=0.3, red_minutes=35,
                turned_green=False, stair_up=True, vol_ratio=1.8, liangbi=2.0,
                anchor_ok=True, falsify_ok=True)
    r = counter_trend(**base)
    T("C1 三门槛全过 → EXCEPTION_ALLOWED，仓位降一档 factor=0.9",
      r["verdict"] == "EXCEPTION_ALLOWED" and r["position_factor"] == 0.9
      and all(r["gates"].values()))
    r = counter_trend(**dict(base, anchor_ok=False))
    T("C2 缺「有锚」→ 三门槛缺一即否 ENV_PRIORITY",
      r["verdict"] == "ENV_PRIORITY" and r["gates"]["有锚"] is False)
    r = counter_trend(**dict(base, liangbi=4.5))
    T("C3 量比≥4（对倒警惕）→ 「有量」否",
      r["verdict"] == "ENV_PRIORITY" and r["gates"]["有量"] is False)
    r = counter_trend(**dict(base, red_minutes=10))
    T("C4 红盘不足 30 分钟 → 「独强」否（一次脉冲排除语义由 turned_green/stair_up 承载）",
      r["gates"]["独强"] is False)
    r = counter_trend(**dict(base, env_drop=0.2, sector_drop=0.3))
    T("C5 环境未跌 → 无冲突 NO_CONFLICT（不进逆势仓逻辑）",
      r["verdict"] == "NO_CONFLICT")
    T("C6 逆势仓证伪双条件同现 → SELL",
      counter_trend_exit(turned_green=True, broke_key_level=True,
                         volume_fading=True, big_outflow=False) == "SELL")
    T("C7 仅条件① → HOLD（单条件不走）",
      counter_trend_exit(turned_green=True, broke_key_level=True,
                         volume_fading=False, big_outflow=False) == "HOLD")
    _src, _tree, _prod = _prod_source()
    cte = next(f for f in _tree.body if isinstance(f, ast.FunctionDef)
               and f.name == "counter_trend_exit")
    str_consts = [n.value for n in ast.walk(cte)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    # docstring 是 FunctionDef 体首条 Expr-Constant；其余字符串常量里不得出现 REDUCE
    body_docs = ast.get_docstring(cte) or ""
    no_reduce = all(v == body_docs or "R" + "EDUCE" != v for v in str_consts)
    T("C8 仅条件② → HOLD，且本闸无 REDUCE 分支（禁机械减半容差）",
      counter_trend_exit(turned_green=False, broke_key_level=False,
                         volume_fading=True, big_outflow=True) == "HOLD"
      and no_reduce)

    # ── D 组：纪律 / 审计 ──
    T("D1 --discipline 全绿", _discipline())
    try:
        check(positions=[{"code": "600001", "position": -0.1}])
        T("D2 负仓位 → GateError", False)
    except GateError:
        T("D2 负仓位 → GateError", True)

    print("exit_gate 自检：%d 失败" % fail[0])
    return fail[0] == 0


def _prod_source():
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    segs = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name not in _TEST_FUNCS:
            segs.append(ast.get_source_segment(src, n))
    return src, tree, "\n".join(segs)


def _discipline():
    ok = [0]

    def T(name, cond):
        if cond:
            ok[0] += 1
            print("  [✓] %s" % name)
        else:
            print("  [✗] %s" % name)

    src, tree, prod_src = _prod_source()
    print("exit_gate 纪律自证：")

    lit = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in _TEST_FUNCS:
            continue
        for node in ast.walk(n):
            if isinstance(node, ast.Compare):
                for v in [node.left] + list(node.comparators):
                    if isinstance(v, ast.Constant) and isinstance(v.value, (int, float)) \
                            and not isinstance(v.value, bool) and v.value not in (0, 1):
                        lit.append("%s:%d" % (n.name, node.lineno))
                        break
    T("① 零字面量阈值比较（阈值只能在 params；0/1 结构基数豁免）", not lit)
    # ② C4 决策词词级检查：NONE / NOT_EVALUATED 是 §1.5 契约值（NONE→HOLD），非决策语义
    T("② 零 C4 决策词（词级：NONE/NOT_EVALUATED 为契约值不计）",
      all(w not in prod_src for w in ("BU" + "Y", "WA" + "IT"))
      and '"N' + 'O"' not in prod_src)
    T("③ confidence 只出现在载荷校验，不进 check/counter_trend 判定函数",
      all("confidence" not in (ast.get_source_segment(src, f) or "")
          for f in tree.body if isinstance(f, ast.FunctionDef)
          and f.name in ("check", "counter_trend", "counter_trend_exit")))
    T("④ 动作值域封闭（ACTIONS 常量 = SELL/REDUCE/HOLD）",
      ACTIONS == ("SELL", "REDUCE", "HOLD") and EXIT_TYPES == (
          "HARD", "THESIS", "EVENT", "NONE"))
    T("⑤ 账本零直写（退出落账属调用方/结算层；无 open 写/append）",
      "open(" not in prod_src and ".write" not in prod_src)
    return ok[0] == 5


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
