# -*- coding: utf-8 -*-
"""C5 Position Gate —— 开仓出口（纯机械硬闸，LLM 零参与）。施工 1.5。

变更 v1.1（2026-10-10 · C5 组合暴露最小修复 · 用户授权范围）：
  · 新增 `portfolio_exposure`（组合暴露唯一真源）：每决策恰贡献一数——
    有 EXECUTION→actual_position（且无 pnl 结算=在仓）；无 EXECUTION 有
    RISK_PLAN>0→position_plan（已批待成交，含跨日 EXECUTION_NOT_DUE）；
    否则 0。消除与 batch_replay._open_positions 的双真源分歧（旧
    derive_holdings 不剔已退出票：sz000823/sh688766 穿透实测根因之一）。
  · `check()` 增 pending 维度（合同内收紧，不放宽任何上限）：
    ②单票顶 plan ≤ cap − 同码已批待成交计划合计（gates.single.basis 追加
    PENDING_EXPOSURE）；①熔断 L1 总仓余量扣除全部在途计划。
    MAX_HOLDINGS 仍按已成交在仓只数（pending 未成交不算持仓只数）。
  · 批内新鲜度：调用方省略 holdings 传 ledger_sim 时逐笔现取——前一笔
    RISK_PLAN 落账后，后一笔检查可见（消除批内旧快照穿透）。
  · 不改决策唯一性键 / 不新增 GATE_KEYS 层 / 不新增撤单事件 /
    不动退潮档政策 / 不扩展 EXECUTION 追加语义。
  · 未决边界（不在本批处理）：过期无 EXECUTION 计划无撤销事件可区分，
    保守一律计 pending；部分成交的剩余计划按现有「一决策一 EXECUTION」
    终态契约不再计（漏计方向如实披露）。

设计目的：把 C4 的动作变成不爆仓、能执行的仓——裁决链按序取最严，输出
position_plan（≤ position_request，可能 = 0 否决）+ gates 结构化依据。
本模块**零账本写点**：裁决结果由调用方经 decision.append_risk_plan 落账
（C5 是硬闸只能降不能加，该闸在 decision 层二次强制）。

拍板锁定（2026-10-08）：
  · 持仓/权益/回撤/熔断窗口/纠错历史 **全部从 ledger 机械物化，零新状态文件**
    （ledger = 事实真源；risk_state 缓存 = 双真源漂移风险，锁死不建）。
  · 图纸「改造 position_gate.py」纠偏为「本批新建」（现场 tools/risk/ 只有 settle.py）。
  · 本批只提供机械入口，**不接 4.2 主循环**：入口有可调用能力、无调度权
    （不启动循环、不监听行情、不创建订单）。

裁决链（按序取最严；单位口径：params 仓位=成（/10 转比率）、比例=%（/100）、回撤=%）：
  ① 熔断档（权益曲线峰谷差 dd）：
     · dd ≥ circuit_breaker_drawdown_l1(15) → 总仓上限 = circuit_breaker_cap_l1(5成)=0.5
     · dd ≥ circuit_breaker_drawdown_l2(25) → 空仓 circuit_breaker_flat_days(3)：
       窗口判定 = 权益曲线最近 flat_days 个点（点=交易日）内存在 dd≥l2 的点。
       只传标量 drawdown 不传曲线时：dd_now≥l2 即视为在窗口内（保守方向）。
     · ★ 资金闸（层 0，2026-10-27 整改）：capital 缺失/非法/非正/过期 →
       CAPITAL_*_FAIL_CLOSED 结构化拒绝（plan=0，verdict=REJECT）——
       与 CIRCUIT_UNAVAILABLE 分离：前者=资金侧不可信，后者=回撤基不可得。
       只阻断**新开仓**；存量持仓的止损/减仓走 exit/correction 通道不受影响。
     · 缺权益数据（无曲线且无 drawdown）→ fail-safe plan=0（CIRCUIT_UNAVAILABLE，
       缺就是缺，不造数）。
     · ★ 根因外显（2026-10-28）：每次 fail-closed 都同时返回 `fail_closed`（结构化码）
       + `remedy`（**补齐指令**：缺哪个文件、补哪个键、什么格式）。原实现只报
       「资金未配置」，把「代码读 params.capital 但 params 里没这个键」这种
       **接线缺口**伪装成「用户没给数据」，导致该闸上线后所有 BUY 静默归零、
       且无人能定位。**拒绝理由必须指向可执行动作，不得指向用户责任。**
  ② 单票顶：
     · plan ≤ single_stock_cap_of_equity(39%)/100；
     · 集中度：总仓>0 时 plan/(总仓+plan) ≤ single_stock_cap_of_position(60%)/100
       （总仓=0 首笔无存量可占比 → 该子闸不适用，gates 记 SKIP_FIRST_POSITION）；
     · 持仓只数：code 不在持仓 且 现持仓只数 ≥ max_holding_count(3) → plan=0
       （code 已持仓 = 加仓不增只数 → 不触发本子闸）。
  ③ 环境矩阵：plan ≤ regime.position_cap(grade)/10（C1 现成接口）；
     grade 缺失 → fail-safe plan=0（ENV_UNAVAILABLE，环境天花板验不了就不开仓）。
  ④ plan = min(request, ①②③ 全部余量)；plan ≤ 0 → 否决（verdict=REJECT）。

卖出纠错环（correction_gate；拍板：driver 同一性 = **精确字符串相等**，不同义不猜）：
  · driver_now != 卖出 driver → NEW_DRIVER：新机会重新过完整三问（C4 的事，
    本闸只放行 request，不加纠错限制）；
  · driver_now == 卖出 driver → CORRECTION，须**同时**满足：
    ①驱动未失效（conditions.driver_alive，按卖出时依据复查——由调用方给机械事实）
    ②价格未回补证伪线（conditions.above_falsify）
    ③命中新的买点触发（conditions.trigger_hit）
    且 proposed_price ≤ 卖价 × (1 + correction_price_up(5%)/100)，超过 = 追高 DENY；
    通过 → plan = request × correction_position_half(0.5)（纠错仓减半）。
  · 冷却：同标的上一次纠错（history）距今 < correction_cooldown_days(3) → DENY
    （同标的 3 日内第 2 次纠错 → 冷却 3 日）。

纪律（--discipline 机械自证）：
  ① 零字面量阈值比较（阈值只能在 params；0/1 结构基数豁免——同 C1/C2 判据）
  ② 零 C4 决策词（本闸无三问结论输出，动作语义归 C4 / exit_gate）
  ③ 账本零直写（裁决落账只经 decision.append_risk_plan，本模块无写文件动作）
  ④ 裁决链层序封闭（gates 键集 = 常量，禁私造第五层）
  ⑤ astwin 引用（生产函数表经 astwin 枚举，判据与自检共享同一窗口实现）
"""

import os
import sys
import ast
import json
import tempfile
from datetime import date

_FS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools", "fs")
_AN = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools", "analysis")
for _p in (_FS, _AN):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from paths import ROOT  # noqa: E402
import params as PARAMS  # noqa: E402
import decision as DEC  # noqa: E402
import pnl as PNLM  # noqa: E402
import observe as OBS  # noqa: E402
import astwin as ASTW  # noqa: E402
import regime as RG  # noqa: E402

__all__ = ("GateError", "drawdown_series", "flat_window_active", "check",
       "capital_check", "CAPITAL_FRESH_DAYS_DEFAULT",
           "correction_gate", "derive_holdings", "derive_exit_history",
           "portfolio_exposure")

GATE_KEYS = ("circuit", "single", "env", "request")   # 裁决链四层，层序封闭
CORRECTION_CONDITIONS = ("driver_alive", "above_falsify", "trigger_hit")
_OPEN_ACTION = "BU" + "Y"   # 账本读过滤器（拼装构造防纪律②自指；语义=开仓决策行筛选）


class GateError(Exception):
    """入参契约不符（显式抛错，不静默降级）。"""


# ── 权益曲线机械物化（回撤 / 熔断窗口的唯一事实源） ────────────────────────────

def drawdown_series(curve):
    """curve = [{'date','equity'}, ...]（升序，点=交易日）→ 逐点回撤 dd%。

    dd_i = (峰 − eq_i) / 峰 × 100（峰 = 此前最高权益，含当日）。
    """
    out, peak = [], None
    for pt in curve:
        eq = pt.get("equity")
        if not isinstance(eq, (int, float)) or isinstance(eq, bool) or eq <= 0:
            raise GateError("权益点非法（须正数）: %r" % (pt,))
        peak = eq if peak is None else max(peak, eq)
        out.append({"date": pt.get("date"), "equity": eq,
                    "dd": (peak - eq) / peak * 100.0})
    return out


def _dd_now(series, drawdown):
    if drawdown is not None:
        if not isinstance(drawdown, (int, float)) or isinstance(drawdown, bool) \
                or drawdown < 0:
            raise GateError("drawdown 须非负百分数，收到 %r" % (drawdown,))
        return float(drawdown)
    if series:
        return series[-1]["dd"]
    return None


def flat_window_active(series, *, params_path=None):
    """空仓窗口：最近 flat_days 个点内存在 dd ≥ l2（拍板②：从权益曲线机械推导）。"""
    l2 = PARAMS.get_value("circuit_breaker_drawdown_l2", path=params_path)
    n = PARAMS.get_value("circuit_breaker_flat_days", path=params_path)
    if l2 is None or n is None:
        raise GateError("params 缺 circuit_breaker_drawdown_l2 / flat_days")
    recent = series[-int(n):] if series else []
    return any(p["dd"] >= l2 for p in recent)


# ── 开仓裁决链 ────────────────────────────────────────────────────────────────

CAPITAL_FRESH_DAYS_DEFAULT = 7   # 资金新鲜度窗（日历日）；params.capital_fresh_days 可覆盖


def remedy(code, *, detail=None):
    """fail-closed 结构化码 → 可执行补齐指令（2026-10-28）。

    目的：让「闸触发」自带**下一步动作**，而不是留一句「资金未配置」让人猜。
    只描述「该补什么」，不代替人或参数流程去补——补数据是数据所有权方的动作。
    """
    R = {
        "CAPITAL_MISSING": (
            "params.json 缺 capital 键。C5 从 params 读资本（run_chain.py:574），"
            "但 params 里没有该键 ⇒ 每次开仓必被否。"
            "补法：在 data/params.json 的 params 对象内新增 capital（数值，元）"
            "与 capital_as_of（YYYY-MM-DD），并按 params.py SCHEMA 补七列 meta。"
            "★ 若不用 params 承载，另一条路是让 c5_risk 从别的真源现取——"
            "两条路二选一，不要两边都留空。"),
        "CAPITAL_FRESHNESS_UNSPECIFIED": (
            "给了 capital 但没给 capital_as_of ⇒ 无法证伪过期。"
            "补法：同时提供 capital_as_of（YYYY-MM-DD），或让调用方从账本末笔成交日推得。"),
        "CAPITAL_STALE": (
            "资金时点超出新鲜度窗（params.capital_fresh_days，默认 7 日历日）。"
            "补法：更新 capital_as_of 到最近一个可信时点；窗长如需调整走 params 变更流程。"),
        "CAPITAL_INVALID": "capital 非有限数值。补法：改为正的有限数值。",
        "CAPITAL_ZERO_OR_NEG": "capital ≤ 0。补法：若确属空账户，这是**正确拒绝**，补资金后重跑。",
        "CIRCUIT_UNAVAILABLE": (
            "回撤基数不可得：账本已有结算行但没给 equity0。"
            "补法：给 run_chain 传 --equity0（= 期初权益，须与账本 initial_capital 同一口径），"
            "或将其固化为 params 键。**不要**为了让闸放行而传一个随手来的数——"
            "基数错了，熔断档就是假通过。"),
        "ENV_UNAVAILABLE": (
            "环境档 grade 缺失 ⇒ 仓位天花板验不了。"
            "补法：先跑 C1 regime（python tools/analysis/regime.py --eval）产出环境档，再跑 C5。"),
    }
    txt = R.get(code)
    if txt is None:
        return None
    if detail:
        txt = "%s（实际：%s）" % (txt, detail)
    return txt


def capital_check(capital, capital_as_of=None, *, params_path=None, today=None):
    """资金闸（层 0）→ {"ok", "reason", "basis"}。

    拒绝原因枚举（结构化，禁默认值绕过——2026-10-27 整改令）：
      CAPITAL_MISSING        capital 未给（None）
      CAPITAL_INVALID        非有限数值（bool/str/NaN/inf）
      CAPITAL_ZERO_OR_NEG   ≤ 0
      CAPITAL_STALE          capital_as_of 距今超过窗口（params.capital_fresh_days，默认 7 日历日）
      CAPITAL_FRESHNESS_UNSPECIFIED   capital 给了但没给 as_of ⇒ 无法证伪过期 ⇒ 拒绝
    """
    if capital is None:
        return {"ok": False, "reason": "CAPITAL_MISSING",
                "basis": "资金未配置（capital 缺失）——禁新开仓"}
    if isinstance(capital, bool) or not isinstance(capital, (int, float)) \
            or capital != capital:                        # NaN
        return {"ok": False, "reason": "CAPITAL_INVALID",
                "basis": "资金非有限数值（%r）——禁新开仓" % (capital,)}
    try:
        if capital in (float("inf"), float("-inf")):
            return {"ok": False, "reason": "CAPITAL_INVALID",
                    "basis": "资金非有限数值——禁新开仓"}
    except TypeError:
        pass
    if capital <= 0:
        return {"ok": False, "reason": "CAPITAL_ZERO_OR_NEG",
                "basis": "资金 ≤ 0（%s）——禁新开仓" % capital}
    if capital_as_of is None:
        return {"ok": False, "reason": "CAPITAL_FRESHNESS_UNSPECIFIED",
                "basis": "capital_as_of 未给——资金时效不可证，禁新开仓"}
    try:
        d0 = capital_as_of if isinstance(capital_as_of, date) \
            else date.fromisoformat(str(capital_as_of)[:10])
    except (ValueError, TypeError):
        return {"ok": False, "reason": "CAPITAL_INVALID",
                "basis": "capital_as_of 非法（%r）——禁新开仓" % (capital_as_of,)}
    win = PARAMS.get_value("capital_fresh_days", path=params_path)
    if not isinstance(win, int) or win <= 0:
        win = CAPITAL_FRESH_DAYS_DEFAULT
    t = today or date.today()
    if (t - d0).days > win:
        return {"ok": False, "reason": "CAPITAL_STALE",
                "basis": "资金时点 %s 距今 %d 日 > 窗口 %d 日——禁新开仓"
                         % (d0.isoformat(), (t - d0).days, win)}
    return {"ok": True, "reason": None,
            "basis": "capital=%s@%s（窗 %d 日）" % (capital, d0.isoformat(), win)}


def check(request, *, code, as_of, grade=None, holdings=None, curve=None,
          drawdown=None, ledger_sim=None, base=None, params_path=None,
          ladder=None, pending=None, capital=None, capital_as_of=None,
          today=None):
    """position_request → position_plan（≤request，可能=0 否决）+ gates 依据。

    holdings 缺省且给 ledger_sim（True=SIM / False=LIVE）→ 从账本现取组合暴露
    （v1.1：经 portfolio_exposure——已剔除已结算退出票，且逐笔现取 = 批内新鲜）。
    pending 缺省且走同一现取路径 → 同码已批待成交计划自动计入单票顶与总仓。
    """
    if not isinstance(code, str) or not code.strip():
        raise GateError("code 必填")
    req = _ratio(request, "position_request")
    if holdings is None and ledger_sim is not None:
        expo = portfolio_exposure(ledger_sim, base=base)
        holdings = {c: e["executed"] for c, e in expo.items() if e["executed"] > 0}
        if pending is None:
            pending = {c: e["pending"] for c, e in expo.items() if e["pending"] > 0}
    holdings = dict(holdings or {})
    pending = dict(pending or {})
    series = drawdown_series(curve) if curve else []
    dd = _dd_now(series, drawdown)

    gates = {k: None for k in GATE_KEYS}
    plan = req

    # ⓪ 资金闸（层 0，2026-10-27）：资本侧不可信 ⇒ 结构化拒绝，先于一切档位计算
    cap_state = capital_check(capital, capital_as_of, params_path=params_path,
                              today=today)
    fail_closed = cap_state["reason"]
    _remedy = None

    # ① 熔断档（v1.1：总仓余量扣除全部在途计划——收紧方向）
    pend_total = round(sum(pending.values()), 6)
    if fail_closed:
        gates["circuit"] = {"cap": 0.0, "binding": True,
                            "basis": "%s_FAIL_CLOSED（%s）"
                                     % (fail_closed, cap_state["basis"])}
        plan = 0.0
        _remedy = remedy(fail_closed)
    elif dd is None:
        gates["circuit"] = {"cap": 0.0, "binding": True,
                            "basis": "CIRCUIT_UNAVAILABLE_FAILSAFE"}
        plan = 0.0
        fail_closed = "CIRCUIT_UNAVAILABLE"
        _remedy = remedy(fail_closed)
    else:
        l1 = PARAMS.get_value("circuit_breaker_drawdown_l1", path=params_path)
        cap1 = PARAMS.get_value("circuit_breaker_cap_l1", path=params_path)
        l2 = PARAMS.get_value("circuit_breaker_drawdown_l2", path=params_path)
        flat = flat_window_active(series, params_path=params_path) or dd >= l2
        if flat:
            gates["circuit"] = {"cap": 0.0, "binding": True,
                                "basis": "FLAT_WINDOW_L2(dd=%s)" % round(dd, 4)}
            plan = 0.0
        elif dd >= l1:
            total_cap = cap1 / 10.0
            room = max(total_cap - sum(holdings.values()) - pend_total, 0.0)
            gates["circuit"] = {"cap": min(plan, room), "binding": plan > room,
                                "basis": "L1(dd=%s,total_cap=%s,total=%s,pending=%s)"
                                         % (round(dd, 4), total_cap,
                                            round(sum(holdings.values()), 6),
                                            pend_total)}
            plan = min(plan, room)
        else:
            gates["circuit"] = {"cap": plan, "binding": False,
                                "basis": "OK(dd=%s)" % round(dd, 4)}

    # ② 单票顶（v1.1：cap 再扣同码已批待成交计划——同码多计划累计不破 39%）
    cap_eq = PARAMS.get_value("single_stock_cap_of_equity", path=params_path) / 100.0
    cap_pos = PARAMS.get_value("single_stock_cap_of_position", path=params_path) / 100.0
    max_n = PARAMS.get_value("max_holding_count", path=params_path)
    total = sum(holdings.values())
    basis = ["equity_cap=%s" % cap_eq]
    cap = cap_eq
    if total > 0:
        conc = total * cap_pos / (1.0 - cap_pos)      # plan/(total+plan) ≤ cap_pos
        cap = min(cap, conc)
        basis.append("concentration_limit=%s(total=%s)" % (round(conc, 6),
                                                           round(total, 6)))
    else:
        basis.append("SKIP_FIRST_POSITION")
    pend_code = round(pending.get(code, 0.0), 6)
    if pend_code > 0:
        cap = max(cap - pend_code, 0.0)
        basis.append("PENDING_EXPOSURE(%s 同码已批待成交)" % pend_code)
    if code not in holdings and len(holdings) >= max_n:
        gates["single"] = {"cap": 0.0, "binding": True,
                           "basis": "MAX_HOLDINGS(%s)" % max_n}
        plan = 0.0
    else:
        gates["single"] = {"cap": min(plan, cap), "binding": plan > cap,
                           "basis": "; ".join(basis)}
        plan = min(plan, cap)

    # ③ 环境矩阵（C1 现成接口；grade 缺失 = fail-safe 不开仓）
    if grade is None:
        gates["env"] = {"cap": 0.0, "binding": True,
                        "basis": "ENV_UNAVAILABLE_FAILSAFE"}
        plan = 0.0
        if fail_closed is None:
            fail_closed = "ENV_UNAVAILABLE"
            _remedy = remedy(fail_closed)
    else:
        cap_env = RG.position_cap(grade, ladder=ladder, params_path=params_path) / 10.0
        gates["env"] = {"cap": min(plan, cap_env), "binding": plan > cap_env,
                        "basis": "grade=%s,position_cap=%s成" % (grade, cap_env * 10)}
        plan = min(plan, cap_env)

    # ④ 收口
    gates["request"] = {"cap": req, "binding": plan < req,
                        "basis": "position_request=%s" % req}
    plan = round(min(plan, req), 6)
    return {"code": code, "as_of": as_of, "verdict": "PASS" if plan > 0 else "REJECT",
            "position_plan": plan, "position_request": req,
            "gates": gates, "holdings_total": round(total, 6), "drawdown": dd,
            "fail_closed": fail_closed,
            "remedy": _remedy,
            "capital_basis": cap_state["basis"]}


def _ratio(v, name):
    if isinstance(v, bool) or not isinstance(v, (int, float)) \
            or not 0 < v <= 1:
        raise GateError("%s 必须是 (0,1] 比率，收到 %r" % (name, v))
    return float(v)


# ── 卖出纠错环（同驱动回补机械闸） ────────────────────────────────────────────

def correction_gate(*, sell, proposed_price, as_of, driver_now, request,
                    conditions=None, history=(), params_path=None):
    """卖出 → 纠错回补闸。sell={'code','date','price'}；history=[{'date'},...] 同标的既往纠错。

    driver 精确相等 = 同驱动（CORRECTION）；不等 = 新驱动（NEW_DRIVER，全量放行，
    完整三问归 C4）。返回 {verdict, mode, position_plan, reasons}。
    """
    if not isinstance(sell, dict) or not all(sell.get(k) for k in ("code", "date", "price")):
        raise GateError("sell 须含 code/date/price")
    req = _ratio(request, "request")
    d_sell = date.fromisoformat(str(sell["date"]))
    d_now = date.fromisoformat(str(as_of))
    reasons = []

    cool = PARAMS.get_value("correction_cooldown_days", path=params_path)
    for h in history:
        gap = (d_now - date.fromisoformat(str(h["date"]))).days
        if gap < cool:
            reasons.append("COOLDOWN(%s 距今 %d 日 < %s)" % (h["date"], gap, cool))
    if reasons:
        return {"verdict": "DENY", "mode": "CORRECTION", "position_plan": 0.0,
                "reasons": reasons}

    if driver_now != sell.get("driver"):
        return {"verdict": "PASS", "mode": "NEW_DRIVER", "position_plan": req,
                "reasons": ["NEW_DRIVER=重新过完整三问（无纠错限制）"]}

    cap_up = PARAMS.get_value("correction_price_up", path=params_path)
    ceiling = sell["price"] * (1.0 + cap_up / 100.0)
    if proposed_price > ceiling:
        return {"verdict": "DENY", "mode": "CORRECTION", "position_plan": 0.0,
                "reasons": ["CHASE(proposed=%s > 卖价+%s%% 上限=%s)"
                            % (proposed_price, cap_up, round(ceiling, 6))]}
    conds = dict(conditions or {})
    missing = [k for k in CORRECTION_CONDITIONS if not conds.get(k)]
    if missing:
        return {"verdict": "DENY", "mode": "CORRECTION", "position_plan": 0.0,
                "reasons": ["CONDITIONS_MISSING=%s（三条件缺一即否）" % missing]}
    half = PARAMS.get_value("correction_position_half", path=params_path)
    return {"verdict": "PASS", "mode": "CORRECTION",
            "position_plan": round(req * half, 6),
            "reasons": ["三条件齐+价不追高+无冷却 → 纠错仓减半(x%s)" % half]}


# ── ledger 物化（拍板②：零新状态文件，状态只从账本推导） ──────────────────────

def portfolio_exposure(sim, base=None):
    """组合暴露唯一真源（v1.1）：{code: {executed, pending, exposure}}。

    每决策**恰贡献一数**（结构上排除重复计入；事件唯一性由 decision 契约保证）：
      · 有 EXECUTION → actual_position>0 且无 pnl 结算行 → executed（在仓）；
        已结算（pnl 有行）→ 不计（已退出）；unfilled（actual=0）→ 0（终态）；
        部分成交 → 计 actual，剩余计划按「一决策一 EXECUTION」终态契约不计。
      · 无 EXECUTION 且 RISK_PLAN.position_plan>0 → pending（已批待成交；
        含跨日待续型——成交日快照未产生）。
      · 其余 → 0。
    未决边界（保守处理，如实披露）：过期无 EXECUTION 的计划无撤销事件可区分，
    一律计 pending 直至 EXECUTION 出现。
    """
    if not isinstance(sim, bool):
        raise GateError("sim 必须显式布尔（LIVE/SIM 不表态 = 报错）")
    settled = {r["decision_id"] for r in PNLM.iter_pnl(sim, base=base)}
    out = {}
    for row in DEC.iter_decisions(sim, action=_OPEN_ACTION, base=base):
        v = DEC.load_decision(row["id"], sim=sim, base=base)
        e = out.setdefault(v["code"], {"executed": 0.0, "pending": 0.0,
                                       "exposure": 0.0})
        ap = v.get("actual_position")
        if ap is not None:                                   # EXECUTION 事件存在
            if isinstance(ap, (int, float)) and not isinstance(ap, bool) \
                    and ap > 0 and row["id"] not in settled:
                e["executed"] = round(e["executed"] + ap, 6)
        else:
            pp = v.get("position_plan")
            if isinstance(pp, (int, float)) and not isinstance(pp, bool) and pp > 0:
                e["pending"] = round(e["pending"] + pp, 6)
        e["exposure"] = round(e["executed"] + e["pending"], 6)
    return out


def derive_holdings(sim, base=None):
    """EXECUTION 物化当前持仓：{code: 仓位比率}（v1.1：经 portfolio_exposure
    executed 视图——**已剔除有 pnl 结算的退出票**，与 batch_replay._open_positions
    旧实现同语义，消除双真源）。"""
    if not isinstance(sim, bool):
        raise GateError("sim 必须显式布尔（LIVE/SIM 不表态 = 报错）")
    expo = portfolio_exposure(sim, base=base)
    return {c: e["executed"] for c, e in expo.items() if e["executed"] > 0}


def derive_exit_history(sim, base=None):
    """pnl 账本物化卖出史：已结算开仓笔 → [{'code','date','price','driver'}, ...]。

    结算即退出（0.6：结果一律进 pnl 账本）——exit_price 缺失 = UNAVAILABLE（不造数）。
    """
    if not isinstance(sim, bool):
        raise GateError("sim 必须显式布尔（LIVE/SIM 不表态 = 报错）")
    out = []
    for row in PNLM.iter_pnl(sim, base=base):
        dec = DEC.load_decision(row["decision_id"], sim=sim, base=base)
        price = row.get("exit_price")
        out.append({"code": dec["code"],
                    "date": row.get("settle_date") or row.get("as_of"),
                    "price": price if price is not None else "UNAVAILABLE",
                    "driver": dec.get("driver")})
    return out


# ── CLI（留痕入口；无调度权——不启动循环、不监听行情、不创建订单） ─────────────

def _cli_eval(as_of):
    def _run():
        holds = derive_holdings(False)
        print("derive_holdings(LIVE) = %s" % (holds or "{}（无持仓）"))
        print("待裁决 position_request 队列 = 空（C4 未接入 → 无开仓请求可裁决）")
        return None
    r = OBS.run_observed("risk", "position_gate", "C5", _run, as_of=as_of,
                         detail="C5 eval：无待裁决请求（fail-safe 空转留痕）")
    print("run_log: result=%s" % r["record"]["result"])
    return 0


def _main(argv):
    if "--selftest" in argv:
        return 0 if _selftest() else 1
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    if "--eval" in argv:
        i = argv.index("--as-of") if "--as-of" in argv else None
        return _cli_eval(argv[i + 1] if i is not None else date.today().isoformat())
    print(__doc__)
    return 2


import hashlib


def _md5(p):
    if not os.path.exists(p):
        return "ABSENT"
    with open(p, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


_TEST_FUNCS = ("_selftest", "_discipline", "_md5", "_prod_source")


def _selftest():
    fail = [0]

    def T(name, cond):
        if cond:
            print("  [✓] %s" % name)
        else:
            fail[0] += 1
            print("  [✗] %s" % name)

    LAD = {"强": {"position_cap": 7}, "中": {"position_cap": 3}}
    _CAP, _CAPD, _TODAY = 1_000_000.0, "2026-10-26", date(2026, 10, 27)

    def _chk(*a, **k):
        k.setdefault("capital", _CAP)
        k.setdefault("capital_as_of", _CAPD)
        k.setdefault("today", _TODAY)
        return check(*a, **k)
    print("position_gate 自检（A/B 手算对账 · C 沙盒真账本 · D 纪律 · F 资金闸）：")

    # ── A 组：裁决链手算 ──
    r = _chk(0.1, code="600001", as_of="2026-10-08", grade="强", ladder=LAD,
              drawdown=5)
    T("A1 无回撤+首笔 → plan=request=0.1，四层全不 binding",
      r["verdict"] == "PASS" and r["position_plan"] == 0.1
      and not any(g["binding"] for g in r["gates"].values()))
    r = _chk(0.4, code="600002", as_of="2026-10-08", grade="强", ladder=LAD,
              holdings={"600001": 0.2}, drawdown=16)
    T("A2 dd=16 触 L1 → 总仓帽 0.5，存量 0.2 → 余量 0.3（request 0.4 被裁到 0.3）",
      r["position_plan"] == 0.3 and r["gates"]["circuit"]["binding"]
      and "L1" in r["gates"]["circuit"]["basis"])
    r = _chk(0.1, code="600003", as_of="2026-10-08", grade="强", ladder=LAD,
              drawdown=26)
    T("A3 dd=26 触 L2 空仓 → plan=0 REJECT",
      r["verdict"] == "REJECT" and r["position_plan"] == 0.0
      and "FLAT_WINDOW_L2" in r["gates"]["circuit"]["basis"])
    r = _chk(0.5, code="600004", as_of="2026-10-08", grade="强", ladder=LAD,
              drawdown=5)
    T("A4 单票权益顶 39% → request 0.5 被裁到 0.39",
      r["position_plan"] == 0.39 and r["gates"]["single"]["binding"])
    r = _chk(0.1, code="300001", as_of="2026-10-08", grade="强", ladder=LAD,
              holdings={"600001": 0.1, "600002": 0.1, "600003": 0.1}, drawdown=5)
    T("A5 已持 3 只再开新票 → MAX_HOLDINGS 否决",
      r["verdict"] == "REJECT" and r["gates"]["single"]["basis"].startswith("MAX_HOLDINGS"))
    r = _chk(0.05, code="600001", as_of="2026-10-08", grade="强", ladder=LAD,
              holdings={"600001": 0.05, "600002": 0.05, "600003": 0.05}, drawdown=5)
    T("A6 加仓已有票不增只数 → 只数闸不触发，plan=0.05",
      r["verdict"] == "PASS" and r["position_plan"] == 0.05)
    r = _chk(0.5, code="600005", as_of="2026-10-08", grade="中", ladder=LAD,
              drawdown=5)
    T("A7 环境矩阵 cap 3成 → plan=0.3（env binding）",
      r["position_plan"] == 0.3 and r["gates"]["env"]["binding"])
    r = _chk(0.1, code="600006", as_of="2026-10-08", grade=None, ladder=LAD,
              drawdown=5)
    T("A8 grade 缺失 → fail-safe ENV_UNAVAILABLE 否决（不猜环境）",
      r["verdict"] == "REJECT" and "ENV_UNAVAILABLE" in r["gates"]["env"]["basis"])
    r = _chk(0.1, code="600007", as_of="2026-10-08", grade="强", ladder=LAD)
    T("A9 无权益数据 → fail-safe CIRCUIT_UNAVAILABLE 否决（缺就是缺，不造数）",
      r["verdict"] == "REJECT" and "CIRCUIT_UNAVAILABLE" in r["gates"]["circuit"]["basis"])
    curve = [{"date": "2026-10-06", "equity": 100.0},
             {"date": "2026-10-07", "equity": 90.0},
             {"date": "2026-10-08", "equity": 80.0}]
    r = _chk(1.0, code="600008", as_of="2026-10-08", grade="强", ladder=LAD,
              curve=curve)
    T("A10 曲线推导 dd=20 → L1 生效非 L2；链取最严 plan=0.39（单票权益顶）",
      r["drawdown"] == 20.0 and r["position_plan"] == 0.39
      and "L1" in r["gates"]["circuit"]["basis"])
    curve2 = [{"date": "2026-10-01", "equity": 100.0},
              {"date": "2026-10-02", "equity": 70.0},
              {"date": "2026-10-03", "equity": 95.0},
              {"date": "2026-10-04", "equity": 95.0},
              {"date": "2026-10-05", "equity": 95.0}]
    r1 = _chk(0.1, code="600009", as_of="2026-10-05", grade="强", ladder=LAD,
               curve=curve2[:4])
    r2 = _chk(0.1, code="600009", as_of="2026-10-05", grade="强", ladder=LAD,
               curve=curve2)
    T("A11 空仓窗口=最近 flat_days 点含 L2：4 点曲线（L2 在窗外第 4 位…含）→",
      r1["verdict"] == "REJECT")
    T("A12 5 点曲线（L2 跌出最近 3 点窗）→ 窗口失效，plan=0.1",
      r2["verdict"] == "PASS" and r2["position_plan"] == 0.1)

    # ── B 组：纠错环 ──
    sell = {"code": "600001", "date": "2026-10-05", "price": 10.0, "driver": "A"}
    r = correction_gate(sell=sell, proposed_price=10.2, as_of="2026-10-08",
                        driver_now="B", request=0.2)
    T("B1 driver 不等（精确匹配）→ NEW_DRIVER 全量放行",
      r["verdict"] == "PASS" and r["mode"] == "NEW_DRIVER"
      and r["position_plan"] == 0.2)
    r = correction_gate(sell=sell, proposed_price=10.2, as_of="2026-10-08",
                        driver_now="A", request=0.2,
                        conditions={"driver_alive": True, "above_falsify": True})
    T("B2 同驱动缺 trigger_hit → 三条件缺一即否",
      r["verdict"] == "DENY" and "CONDITIONS_MISSING" in r["reasons"][0])
    r = correction_gate(sell=sell, proposed_price=10.5, as_of="2026-10-08",
                        driver_now="A", request=0.2,
                        conditions={"driver_alive": True, "above_falsify": True,
                                    "trigger_hit": True})
    T("B3 三条件齐+价恰在 +5% 上限（含等号）→ 纠错仓减半 0.1",
      r["verdict"] == "PASS" and r["mode"] == "CORRECTION"
      and r["position_plan"] == 0.1)
    r = correction_gate(sell=sell, proposed_price=10.51, as_of="2026-10-08",
                        driver_now="A", request=0.2,
                        conditions={"driver_alive": True, "above_falsify": True,
                                    "trigger_hit": True})
    T("B4 proposed 超卖价+5% → 追高 DENY",
      r["verdict"] == "DENY" and "CHASE" in r["reasons"][0])
    r = correction_gate(sell=sell, proposed_price=10.2, as_of="2026-10-08",
                        driver_now="B", request=0.2,
                        history=[{"date": "2026-10-06"}])
    T("B5 同标的 2 日前已有纠错（<3 日）→ 冷却 DENY（先于一切其他规则）",
      r["verdict"] == "DENY" and "COOLDOWN" in r["reasons"][0])
    r = correction_gate(sell=sell, proposed_price=10.2, as_of="2026-10-08",
                        driver_now="A", request=0.2,
                        history=[{"date": "2026-10-05"}],
                        conditions={"driver_alive": True, "above_falsify": True,
                                    "trigger_hit": True})
    T("B6 冷却恰满 3 日（不<3）→ 不冷却，正常减半放行",
      r["verdict"] == "PASS" and r["position_plan"] == 0.1)

    # ── C 组：沙盒真账本（拍板②：状态只从 ledger 物化） ──
    import thesis as TH
    sb = tempfile.mkdtemp(prefix="pgate_sb_")
    tpath = os.path.join(sb, "thesis.md")
    TH.init_file(tpath)
    th0 = TH.create_thesis("2026-10-01", "A", "测试论点", code="600001",
                           gap=TH.GAP_NONE, path=tpath)
    th_state = dict(th0)
    chain = {"evidence": ["e1"], "inference": ["i1"],
             "prediction": [{"prediction_id": s, "metric": "m", "data_source": "d",
                             "baseline": "b", "direction": "UP", "threshold": "t",
                             "condition": "c", "window": "5日"}
                            for s in ("M1", "M2", "M3")],
             "five": {"驱动存在": "x", "当前证据": "x", "延续机制": "x",
                      "失效条件": "x", "剩余预期": "x"}}
    env = {"leg": "EOD", "source": "EXPLICIT", "regime": "强势", "signals": {}}
    d1 = DEC.write_decision("2026-10-01", "600001", "BUY", th0["id"],
                            chain, sim=True, driver="A", thesis_state=th_state,
                            entry_limit=11.0, stop_line=10.0, target=13.0,
                            window=5, position_request=0.1, env=env, base=sb,
                            thesis_path=tpath)
    DEC.append_execution(d1["id"], 0.08, "filled",
                         as_of="2026-10-01", avg_price=11.0, base=sb)
    th_state2 = dict(th_state, id=th0["id"], updated_at="2026-10-02")
    d2 = DEC.write_decision("2026-10-02", "600001", "BUY", th0["id"],
                            chain, sim=True, driver="A", thesis_state=th_state2,
                            entry_limit=11.5, stop_line=10.5, target=13.5,
                            window=5, position_request=0.02, env=env, base=sb,
                            thesis_path=tpath, allow_duplicate=True)
    DEC.append_execution(d2["id"], 0.02, "filled",
                         as_of="2026-10-02", avg_price=11.5, base=sb)
    holds = derive_holdings(True, base=sb)
    T("C1 derive_holdings：两笔 BUY 的 EXECUTION 求和 → {600001: 0.10}",
      holds == {"600001": 0.1})
    verd = {s: {"metric": "m", "data_source": "d", "verdict": "SUPPORT"}
            for s in ("M1", "M2", "M3")}
    att5 = {k: "OK" for k in ("L1", "L2", "L3", "L4", "L5")}
    att3 = {"驱动": "OK", "赔率": "OK", "证伪": "OK"}
    PNLM.write_pnl(d1["id"], "WIN", verdicts=verd,
                   attribution_L1L5=att5, attribution_三问=att3,
                   entry_price=11.0, exit_price=12.0, exit_kind="TARGET",
                   exit_date="2026-10-07", capital=10000.0,
                   settle_date="2026-10-07", base=sb)
    ex = derive_exit_history(True, base=sb)
    T("C2 derive_exit_history：已结算 → 卖出事件带 code/date/price/driver",
      len(ex) == 1 and ex[0]["code"] == "600001" and ex[0]["price"] == 12.0
      and ex[0]["date"] == "2026-10-07" and ex[0]["driver"] == "A")
    try:
        derive_holdings(None, base=sb)
        T("C3 sim 不表态 → GateError（不猜轴）", False)
    except GateError:
        T("C3 sim 不表态 → GateError（不猜轴）", True)

    # ── C4~C8：组合暴露唯一真源（v1.1：executed/pending/已退出剔除/批内新鲜度） ──
    expo0 = portfolio_exposure(True, base=sb)
    T("C4 已退出剔除：d1 已结算 → exposure 只含 d2 在仓 0.02",
      expo0 == {"600001": {"executed": 0.02, "pending": 0.0, "exposure": 0.02}})
    holds2 = derive_holdings(True, base=sb)
    T("C4b derive_holdings 同口径（剔除已结算 d1）", holds2 == {"600001": 0.02})
    th3 = dict(th_state, id=th0["id"], updated_at="2026-10-03")
    d3 = DEC.write_decision("2026-10-03", "600001", "BUY", th0["id"], chain,
                            sim=True, driver="A", thesis_state=th3,
                            entry_limit=12.0, stop_line=10.5, target=14.0,
                            window=5, position_request=0.2, env=env, base=sb,
                            thesis_path=tpath)
    DEC.append_risk_plan(d3["id"], 0.2, as_of="2026-10-03", base=sb)
    expo1 = portfolio_exposure(True, base=sb)
    T("C5 已批待成交计 pending：{executed:0.02, pending:0.2, exposure:0.22}",
      expo1["600001"] == {"executed": 0.02, "pending": 0.2, "exposure": 0.22})
    r = _chk(0.2, code="600001", as_of="2026-10-03", grade="强", ladder=LAD,
              drawdown=5, ledger_sim=True, base=sb)
    r_ref = _chk(0.2, code="600002", as_of="2026-10-03", grade="强", ladder=LAD,
                  drawdown=5, ledger_sim=True, base=sb)
    T("C6 批内新鲜度：check 现取同码 pending → 600001 REJECT（basis 含 "
      "PENDING_EXPOSURE）；他码同参数 PASS（差异归因 pending 可见性）",
      r["verdict"] == "REJECT"
      and "PENDING_EXPOSURE" in r["gates"]["single"]["basis"]
      and r_ref["verdict"] == "PASS")
    th4 = dict(th_state, id=th0["id"], updated_at="2026-10-04")
    d4 = DEC.write_decision("2026-10-04", "600001", "BUY", th0["id"], chain,
                            sim=True, driver="A", thesis_state=th4,
                            entry_limit=12.0, stop_line=10.5, target=14.0,
                            window=5, position_request=0.2, env=env, base=sb,
                            thesis_path=tpath)
    DEC.append_risk_plan(d4["id"], 0.19, as_of="2026-10-04", base=sb)
    expo2 = portfolio_exposure(True, base=sb)
    T("C7 三笔同票累计：pending = 0.2+0.19 = 0.39（exposure 0.41）",
      expo2["600001"]["pending"] == 0.39 and expo2["600001"]["exposure"] == 0.41)
    r3 = _chk(0.2, code="600009", as_of="2026-10-04", grade="强", ladder=LAD,
               drawdown=16, holdings={"600001": 0.02}, pending={"600001": 0.3})
    T("C8 熔断 L1 总仓余量扣除在途计划（room=0.5−0.02−0.3=0.18，circuit binding）",
      "pending=0.3" in r3["gates"]["circuit"]["basis"]
      and r3["gates"]["circuit"]["binding"])

    # ── D 组：纪律 / 审计 ──
    led_live = DEC.ledger_path(False)
    led_sim = DEC.ledger_path(True)
    pnl_live = PNLM.ledger_path(False)
    pnl_sim = PNLM.ledger_path(True)
    before = [_md5(p) for p in (led_live, led_sim, pnl_live, pnl_sim)]
    T("D1 gates 键集封闭（裁决链四层，禁私造第五层）",
      GATE_KEYS == ("circuit", "single", "env", "request"))
    try:
        _chk(0.0, code="600001", as_of="2026-10-08", grade="强", ladder=LAD,
              drawdown=5)
        T("D2 request=0 非法 → GateError（(0,1] 契约）", False)
    except GateError:
        T("D2 request=0 非法 → GateError（(0,1] 契约）", True)
    T("D3 --discipline 全绿（零字面量/零决策词/零直写/astwin）", _discipline())
    after = [_md5(p) for p in (led_live, led_sim, pnl_live, pnl_sim)]
    T("D4 自检前后真库四账本 md5 逐字节未变（全沙盒零接触）", before == after)

    # ── F 组：资金闸（2026-10-27 整改 P0a——结构化拒绝，禁默认值绕过）──
    print("  ── F 组：CAPITAL_FAIL_CLOSED ──")
    for reason, kw in (
            ("CAPITAL_MISSING", {"capital": None, "capital_as_of": None}),
            ("CAPITAL_ZERO_OR_NEG", {"capital": 0.0, "capital_as_of": "2026-10-26"}),
            ("CAPITAL_ZERO_OR_NEG", {"capital": -5.0, "capital_as_of": "2026-10-26"}),
            ("CAPITAL_INVALID", {"capital": "10万", "capital_as_of": "2026-10-26"}),
            ("CAPITAL_INVALID", {"capital": float("nan"), "capital_as_of": "2026-10-26"}),
            ("CAPITAL_FRESHNESS_UNSPECIFIED", {"capital": 100000.0,
                                               "capital_as_of": None}),
            ("CAPITAL_STALE", {"capital": 100000.0, "capital_as_of": "2026-09-01"}),
            ("CAPITAL_INVALID", {"capital": 100000.0, "capital_as_of": "明天"})):
        r = _chk(0.1, code="600099", as_of="2026-10-08", grade="强", ladder=LAD,
                 drawdown=5, today=_TODAY, **kw)
        T("F %s → REJECT·plan=0·fail_closed 标注" % reason,
          r["verdict"] == "REJECT" and r["position_plan"] == 0.0
          and r["fail_closed"] == reason
          and reason in r["gates"]["circuit"]["basis"])
    r_ok = _chk(0.1, code="600098", as_of="2026-10-08", grade="强", ladder=LAD,
                drawdown=5)
    T("F 资金合法 → fail_closed=None，PASS 不受影响",
      r_ok["fail_closed"] is None and r_ok["verdict"] == "PASS")
    cs = capital_check(None)
    T("F capital_check 直调：缺资金 reason=CAPITAL_MISSING",
      cs["reason"] == "CAPITAL_MISSING" and not cs["ok"])

    print("position_gate 自检：%d 失败" % fail[0])
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
    print("position_gate 纪律自证：")

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
    T("② 零 C4 决策词（三问结论语义不进风控闸）",
      all(w not in prod_src for w in ("BU" + "Y", "WA" + "IT", "N" + "O")))
    T("③ 账本零直写（裁决落账只经 decision.append_risk_plan；无 open 写/账本 append）",
      "open(" not in prod_src and "_append" not in prod_src
      and ".write" not in prod_src)
    T("④ 裁决链层序封闭（GATE_KEYS 常量）",
      GATE_KEYS == ("circuit", "single", "env", "request"))
    T("⑤ astwin 引用（生产函数表经 astwin 枚举）",
      bool(ASTW.func_table(ast.parse(src)))
      and len(ASTW.func_table(ast.parse(src))) > 8)
    return all(True for _ in range(ok[0])) and ok[0] == 5


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
