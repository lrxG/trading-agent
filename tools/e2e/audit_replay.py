#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_replay.py · W1/W2 逐笔账目审计（WB 裁决 P0–P3 · 2026-10-10 建）

用途：对 batch_replay 的回放结果做**逐笔、可独立复算**的审计——
  P0  逐笔账目：决策日 / 信息截止 / 成交日价 / 退出日价 / C5 状态转换 / 毛与净盈亏 /
      资金占用 / 净值 / 回撤；**独立复算器**不经 settle/pnl 模块，直接从旧包 klines
      真源重演 v1.1 成交-退出路径，与账本逐字段对账；差异逐条列出并解释。
  P0  专项：outcome=LOSS 且 pnl_pct>0 的 v1.1 案例逐笔判定（止损事件 vs 最终盈亏）。
  P1  退潮档全部 BUY 案例逐笔抽取：环境档 / 读取的参数版本 / C5 gates 全文 /
      计划仓 vs 实际仓 / 单股占权益 / 总仓 / 持仓数 / 熔断状态。
  P2  每日、每笔的数据来源与证据等级（dt_source / news / A 事件 / S1 / fail_rate 源）
      → 四类样本划分（S1 重叠训练区 / 仅 B-C 驱动 / 候选样本外 / 不可纳入）。
  P3  成本口径：费率读正式参数（v1.2 起与结算账本同源）→ 独立精算对账账本
      fee_amount/net_amount（fee_match 逐笔）；旧报告「笔数×20 万常数×0.30%」粗估与
      精算的差额逐条列出（口径更正记录）。
  确定性：同窗回放两次，账本逐行 diff（排除时间戳键）——覆盖逐笔成交、退出、
      成本、净值，而非只比 pnl_total。

证据落点：data/generated/reports/audit_replay/（X1 豁免区 · E 系列交付物）。
独立复算真源：旧包 data/raw/bus/klines.csv 的 OHLC（原始成交记录层）。

语义模式（--pre-fix / --post-fix，对账规则随 pnl SCHEMA_VERSION 切换）：
  pre-fix  （v1）：STOP→LOSS / TARGET→WIN / WINDOW_END→按符号（outcome 混合语义，审计对象）
  post-fix （v2）：outcome 一律按符号对齐 + exit_kind 记退出原因（事件与盈亏分离）

用法：
  python tools/e2e/audit_replay.py --window w1            # 单窗审计
  python tools/e2e/audit_replay.py --window w2            # 单窗审计
  python tools/e2e/audit_replay.py --determinism          # W2 双跑逐笔 diff
  python tools/e2e/audit_replay.py --all                  # w1 + w2 + 确定性
"""
import csv
import io
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
for _p in ("fs", "analysis", "decision", "risk", "learning", "e2e", "discovery"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）

import batch_replay as BR          # noqa: E402   ← 回放 harness（沙盒不清理模式复用）
import decision as DEC             # noqa: E402   ← 仅用 ledger 路径定位；对账不复用其计算
import pnl as PNL                  # noqa: E402   ← 仅用路径定位函数；对账不复用其计算
import real_replay as RR           # noqa: E402   ← 涨停价比例规则（同一真源，禁抄第二份）
import params as PARAMS            # noqa: E402   ← 费率真源（v1.2 成本入账后与账本同源对账）

OUT_DIR = os.path.join(_ROOT, "data", "generated", "reports", "audit_replay")
EQUITY0 = BR.EQUITY0
EPS = 1e-9
# v1.2 起费率不再硬编码：读正式参数（与 settle 落账同源）——审计职责改为
# 独立精算并与账本 fee_amount/net_amount 对账（fee_match）。
TS_KEYS = {"recorded_at", "frozen_at", "settled_at", "created_at", "updated_at"}
# P2 样本隔离锚点（效应库 S1 effect_base v1.0.0：as_of=2026-09-21，有效统计事件约
# 截至 8 月下旬、结果窗延伸至 09-21）
S1_STAT_CUTOFF = "2026-08-31"     # S1 统计可能重叠的训练/验证区上界（保守取月末）
S1_OOS_FROM = "2026-09-22"        # 候选样本外区间起点（效应库 as_of=09-21 次一交易日）


# ── 沙盒账本原始读取（不经 DEC/PNL 解析逻辑——审计要读原始行） ─────────────────
def _read_jsonl(p):
    if not os.path.isfile(p):
        return []
    out = []
    for i, line in enumerate(io.open(p, encoding="utf-8"), 1):
        s = line.strip()
        if not s:
            continue
        try:
            out.append(json.loads(s))
        except ValueError as e:
            raise RuntimeError("账本 %s 第 %d 行非法 JSON: %s" % (p, i, e))
    return out


def _sim_ledgers(base):
    """base=沙盒 gen 根 → (decision 行们, risk_plan 事件, execution 事件, pnl 行们)。"""
    dec_rows = _read_jsonl(DEC.ledger_path(True, base=base))
    plans = [r for r in dec_rows if r.get("record_type") == "RISK_PLAN"]
    execs = [r for r in dec_rows if r.get("record_type") == "EXECUTION"]
    decisions = [r for r in dec_rows if r.get("record_type") == "DECISION"]
    return decisions, plans, execs, _read_jsonl(PNL.ledger_path(True, base=base))


# ── 独立复算器：不经 settle —— 从 kb 真源重演 v1.1 成交-退出 ───────────────────
def _recompute_fill(dec, all_dates, kb):
    """重演 v1.1 成交：返回 dict(exec_date, open, filled, reason) 或 None（窗口外）。"""
    d = dec["date"]
    if d not in all_dates:
        return None
    i = all_dates.index(d)
    if i + 1 >= len(all_dates):
        return None                                    # 成交日超出数据 → 窗口外
    d_exec = all_dates[i + 1]
    row = (kb.get(d_exec) or {}).get(dec["code"])
    prev = (kb.get(d) or {}).get(dec["code"])
    if row is None or prev is None:
        return {"exec_date": d_exec, "filled": False, "reason": "NO_QUOTE"}
    op, lo, hi, cl = row["open"], row["low"], row["high"], row["close"]
    one_board = (op == hi == lo == cl)
    lim = round(float(prev["close"]) * (1 + RR._limit_ratio(dec["code"], row.get("name"))), 2)
    limit_hit = op >= lim - 0.005
    if one_board:
        return {"exec_date": d_exec, "open": op, "filled": False, "reason": "ONE_BOARD"}
    if limit_hit:
        return {"exec_date": d_exec, "open": op, "filled": False, "reason": "LIMIT_OPEN"}
    if op > float(dec["entry_limit"]) + EPS:
        return {"exec_date": d_exec, "open": op, "filled": False,
                "reason": "OPEN_ABOVE_LIMIT"}
    return {"exec_date": d_exec, "open": op, "filled": True, "reason": "OK"}


def _recompute_exit(dec, fill, all_dates, kb):
    """重演 v1.1 退出（T+1 / 止损优先 / 跳空 open / 窗口末收盘）——settle 语义的独立重写。

    返回 dict(kind, exit_date, exit_price, hold_days, sign)：kind ∈ STOP/TARGET/
    WINDOW_END/PENDING_*；sign ∈ WIN/LOSS/FLAT（**最终盈亏结果**，与事件类型分离）。
    """
    if not fill or not fill.get("filled"):
        return {"kind": "NOT_FILLED", "sign": None}
    w = int(dec["window"])
    stop, tgt = float(dec["stop_line"]), float(dec["target"])
    i0 = all_dates.index(fill["exec_date"])
    win_days = all_dates[i0:i0 + w]
    if len(win_days) < w:
        return {"kind": "PENDING_WINDOW", "sign": None,
                "reason": "窗口 %d 日截至数据末只走过 %d 日" % (w, len(win_days))}
    entry = fill["open"]
    for k, d in enumerate(win_days):
        row = (kb.get(d) or {}).get(dec["code"])
        if row is None:
            return {"kind": "PENDING_DATA", "sign": None, "reason": "窗口内 %s 缺行情" % d}
        if k == 0:
            continue                                   # T+1：成交日不可卖
        lo, hi, op = row["low"], row["high"], row["open"]
        if lo <= stop + EPS:                           # 止损优先（同日双触发同此）
            px = op if (op is not None and op < stop) else stop
            sign = "WIN" if px > entry + EPS else ("LOSS" if px < entry - EPS else "FLAT")
            return {"kind": "STOP", "exit_date": d, "exit_price": px,
                    "hold_days": k + 1, "sign": sign}
        if hi >= tgt - EPS:
            px = op if (op is not None and op > tgt) else tgt
            sign = "WIN" if px > entry + EPS else ("LOSS" if px < entry - EPS else "FLAT")
            return {"kind": "TARGET", "exit_date": d, "exit_price": px,
                    "hold_days": k + 1, "sign": sign}
    d = win_days[w - 1]
    row = (kb.get(d) or {}).get(dec["code"])
    if row is None or row.get("close") is None:
        return {"kind": "PENDING_DATA", "sign": None, "reason": "窗口末日 %s 缺收盘" % d}
    x = row["close"]
    sign = "WIN" if x > entry + EPS else ("LOSS" if x < entry - EPS else "FLAT")
    return {"kind": "WINDOW_END", "exit_date": d, "exit_price": x, "hold_days": w,
            "sign": sign}


def _expect_outcome(rc, sem):
    """复算结果 → 期望账本 outcome。sem='pre'（v1 混合语义）/ 'post'（v2 符号对齐）。"""
    if rc["kind"] == "NOT_FILLED":
        return "NOT_FILLED"
    if rc.get("sign") is None:
        return None                                    # PENDING_* → 无 pnl 行
    if sem == "pre":
        return {"STOP": "LOSS", "TARGET": "WIN"}.get(rc["kind"], rc["sign"])
    return rc["sign"]


# ── 逐笔拼装 + 对账 ───────────────────────────────────────────────────────────
def _collect(window, tmp, all_dates, kb, sem):
    base = os.path.join(tmp, "gen")
    decisions, plans, execs, pnl_rows = _sim_ledgers(base)
    plans_by = {}
    for p in plans:
        plans_by.setdefault(p["decision_id"], []).append(p)
    execs_by = {}
    for e in execs:
        execs_by.setdefault(e["decision_id"], []).append(e)
    pnl_by = {r["decision_id"]: r for r in pnl_rows}
    trades = []
    for dec in sorted([d for d in decisions if d.get("action") == "BUY"],
                      key=lambda d: (d["date"], d["id"])):
        pid = dec["id"]
        plan = sorted(plans_by.get(pid, []), key=lambda p: p["as_of"])[-1] \
            if plans_by.get(pid) else None
        ex = sorted(execs_by.get(pid, []), key=lambda e: e["as_of"])[-1] \
            if execs_by.get(pid) else None
        row = pnl_by.get(pid)
        fill = _recompute_fill(dec, all_dates, kb)
        rc = _recompute_exit(dec, fill, all_dates, kb) if (fill and row
                                                           and row["outcome"] != "NOT_FILLED") \
            else (_recompute_exit(dec, fill, all_dates, kb) if fill else
                  {"kind": "NOT_FILLED", "sign": None})
        t = {
            "decision_id": pid, "code": dec["code"], "date": dec["date"],
            "driver": dec.get("driver"), "window": dec.get("window"),
            "entry_limit": dec.get("entry_limit"), "stop_line": dec.get("stop_line"),
            "target": dec.get("target"), "position_request": dec.get("position_request"),
            "regime": (dec.get("env") or {}).get("regime"),
            "position_cap": (dec.get("env") or {}).get("position_cap"),
            "params_version": dec.get("params_version"),
            "c5_plan": (plan or {}).get("position_plan"),
            "c5_pass": ((plan or {}).get("position_plan") or 0) > 0,
            "c5_note": (plan or {}).get("note"),
            "c5_gates": (plan or {}).get("gates"),
            "actual_position": (ex or {}).get("actual_position"),
            "exec_result": (ex or {}).get("execution_result"),
            "exec_date": (ex or {}).get("as_of"),
            "avg_price": (ex or {}).get("avg_price"),
            "exec_note": (ex or {}).get("note"),
            "pnl": ({k: v for k, v in row.items()
                     if k not in ("attribution_L1L5", "attribution_三问", "M1", "M2", "M3")}
                    if row else None),
        }
        # —— 独立复算对账 ——
        chk = {}
        if fill:
            chk["fill"] = {"exec_date": fill["exec_date"], "open": fill.get("open"),
                           "filled": fill["filled"], "reason": fill["reason"]}
            if ex and ex.get("execution_result") == "filled":
                chk["fill"]["match"] = (fill["filled"]
                                        and fill["exec_date"] == ex["as_of"]
                                        and abs(fill["open"] - ex["avg_price"]) < 1e-6)
            if row and row["outcome"] != "NOT_FILLED":
                exp_oc = _expect_outcome(rc, sem)
                exp_amt = (round(row["capital"] * (rc["exit_price"] - fill["open"])
                                 / fill["open"], 6) if rc.get("exit_price") else None)
                chk["exit"] = {
                    "kind": rc["kind"], "exit_date": rc.get("exit_date"),
                    "exit_price": rc.get("exit_price"), "hold_days": rc.get("hold_days"),
                    "sign": rc.get("sign"),
                    "exp_outcome": exp_oc,
                    "match": (exp_oc == row["outcome"]
                              and rc.get("exit_price") is not None
                              and abs(rc["exit_price"] - row["exit_price"]) < 1e-6
                              and rc.get("hold_days") == row.get("hold_days")
                              and (exp_amt is None
                                   or abs(exp_amt - row["pnl_amount"]) < max(1e-4, abs(row["pnl_amount"]) * 1e-4))),
                }
        t["recompute"] = chk
        trades.append(t)
    return trades, decisions, pnl_rows


def _equity_chain(pnl_rows):
    """复现 settle 的 equity 链：capital = actual_position × (EQUITY0 + 已实现至前一日)。

    settle 调用点 equity=EQUITY0+_realized(当日结算前) → 同一结算日多笔共用同一 equity。
    返回 {decision_id: equity_used}。
    """
    by_day = {}
    for r in pnl_rows:
        by_day.setdefault(r["settle_date"], []).append(r)
    days = sorted(by_day)
    out, cum = {}, 0.0
    for d in days:
        eq = EQUITY0 + cum
        for r in sorted(by_day[d], key=lambda x: x["decision_id"]):
            out[r["decision_id"]] = eq
        cum = round(cum + sum(x["pnl_amount"] for x in by_day[d]), 6)
    return out


def _curve_and_dd(pnl_rows, equity0=EQUITY0):
    """逐日净值（按 settle_date 聚合）与最大回撤（已实现口径，与 _drawdown 同定义）。"""
    by_day = {}
    for r in pnl_rows:
        by_day.setdefault(r["settle_date"], 0.0)
        by_day[r["settle_date"]] = round(by_day[r["settle_date"]] + r["pnl_amount"], 6)
    pts, cum, peak, dd = [], 0.0, equity0, 0.0
    for d in sorted(by_day):
        cum = round(cum + by_day[d], 6)
        eq = round(equity0 + cum, 6)
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak if peak else 0)
        pts.append({"settle_date": d, "day_pnl": by_day[d], "cum_pnl": cum, "equity": eq})
    return {"points": pts, "total_pnl": cum, "final_equity": round(equity0 + cum, 6),
            "max_drawdown_pct": round(dd * 100, 4)}


def _cost_rows(trades):
    """P3：逐笔精算成本（费率读正式参数 params.json，v1.2 起与结算账本同源）+
    **对账账本落账费用**（fee_amount 应等于独立精算；net_amount 应等于毛−费）。"""
    br = PARAMS.get_value("cost_buy_rate")
    sr = PARAMS.get_value("cost_sell_rate")
    rows = []
    for t in trades:
        p = t.get("pnl")
        if not p or p["outcome"] == "NOT_FILLED":
            continue
        buy_amt = p["capital"]
        sell_amt = p["capital"] * (p["exit_price"] / p["entry_price"])
        cb, cs = round(buy_amt * br, 2), round(sell_amt * sr, 2)
        ledger_fee = p.get("fee_amount")
        fee_ok = (None if ledger_fee is None
                  else abs(round(ledger_fee, 2) - round(cb + cs, 2)) < 0.011)
        rows.append({"decision_id": t["decision_id"], "code": t["code"], "date": t["date"],
                     "regime": t["regime"], "capital": buy_amt, "buy_cost": cb,
                     "sell_cost": cs, "cost_total": round(cb + cs, 2),
                     "ledger_fee": ledger_fee, "fee_match": fee_ok,
                     "pnl_gross": p["pnl_amount"],
                     "pnl_net": round(p["pnl_amount"] - cb - cs, 2),
                     "cost_pct_of_capital": round((cb + cs) / buy_amt * 100, 4)})
    return rows


def _regime_cases(trades):
    """P1：按环境档拆案例（重点退潮档），逐笔带 gates 全文与仓位链。"""
    out = {}
    for t in trades:
        if t.get("c5_plan") is None and t.get("actual_position") is None \
                and t.get("pnl") is None:
            continue
        g = t["regime"]
        cap_eq = None
        p = t.get("pnl")
        if p and p.get("capital"):
            cap_eq = round(p["capital"] / EQUITY0 * 100, 4)   # 单股占期初权益 %
        out.setdefault(g, []).append({
            "decision_id": t["decision_id"], "code": t["code"], "date": t["date"],
            "driver": t["driver"], "position_cap(成)": t["position_cap"],
            "position_request": t["position_request"], "position_plan": t["c5_plan"],
            "gates": t["c5_gates"], "params_version": t["params_version"],
            "actual_position": t["actual_position"], "exec_result": t["exec_result"],
            "capital": (p or {}).get("capital"), "capital_pct_of_equity0": cap_eq,
            "outcome": (p or {}).get("outcome"), "pnl_amount": (p or {}).get("pnl_amount"),
            "pnl_pct": (p or {}).get("pnl_pct"),
            "c5_binding_env": ((t["c5_gates"] or {}).get("env") or {}).get("binding"),
            "c5_binding_circuit": ((t["c5_gates"] or {}).get("circuit") or {}).get("binding"),
            "c5_binding_single": ((t["c5_gates"] or {}).get("single") or {}).get("binding"),
        })
    return out


def _stratify(trades, rep):
    """P2：数据分层——每笔交易的驱动路径与证据等级 → 四类样本。"""
    days = rep.get("days") or {}
    for t in trades:
        d = days.get(t["date"]) or {}
        t["data_day"] = {"dt_source": d.get("dt_source"), "news_rows": d.get("news_rows"),
                         "a_events": d.get("a_events"),
                         "fail_rate_src": d.get("fail_rate_src"),
                         "s1_calls": len(d.get("s1") or []),
                         "s1_unavailable": len(d.get("s1_unavailable") or [])}
        drv, dd = t["driver"], t["date"]
        filled = (t.get("actual_position") or 0) > 0
        if drv == "A":
            if dd <= S1_STAT_CUTOFF:
                cls = "S1_训练验证重叠区"
            elif dd >= S1_OOS_FROM:
                cls = "候选样本外（S1 截止后）"
            else:
                cls = "S1 窗口过渡区（08-31~09-21，效应库结果窗延伸内）"
        elif drv == "B":
            cls = "仅 B/C 类驱动（fundflow/LHB，不受 S1 污染）"
        else:
            cls = "其他"
        if t.get("pnl") is None:
            cls += "｜未结算（窗口/数据缺）"
        elif not filled and t.get("actual_position") == 0:
            cls += "｜未成交"
        t["sample_class"] = cls
    return trades


# ── 单窗审计 ──────────────────────────────────────────────────────────────────
def audit_window(window, sem="pre", verbose=True):
    tmp = tempfile.mkdtemp(prefix="audit_replay_%s_" % window)
    try:
        rep = BR.run_batch(window, sandbox=tmp, second_pass=True, verbose=False)
        series, all_dates = BR._klines_series(RR.OLD_ROOT, "2026-03-01")
        w0, w1 = BR.WINDOWS[window]
        dates = [d for d in all_dates if w0 <= d <= w1]
        need = set()
        for d in dates:
            i = all_dates.index(d)
            for j in range(max(0, i - 1), min(len(all_dates), i + 4)):
                need.add(all_dates[j])
        kb = BR._load_kb_days(RR.OLD_ROOT, need)
        trades, decisions, pnl_rows = _collect(window, tmp, all_dates, kb, sem)
        _stratify(trades, rep)
        cost = _cost_rows(trades)
        cost_by = {c["decision_id"]: c["cost_total"] for c in cost}
        pnl_net_rows = [dict(r, pnl_amount=round(r["pnl_amount"]
                                                  - cost_by.get(r["decision_id"], 0.0), 6))
                        for r in pnl_rows]
        curves = {"gross": _curve_and_dd(pnl_rows), "net": _curve_and_dd(pnl_net_rows)}
        equity_chain = _equity_chain(pnl_rows)
        # capital 复现对账：capital == actual_position × equity_used（settle 调用口径复现）
        base = os.path.join(tmp, "gen")
        execs_by = {}
        for e in _read_jsonl(DEC.ledger_path(True, base=base)):
            if e.get("record_type") == "EXECUTION":
                execs_by.setdefault(e["decision_id"], []).append(e)
        cap_bad = []
        for r in pnl_rows:
            if r["outcome"] == "NOT_FILLED":
                continue
            ex = sorted(execs_by.get(r["decision_id"], []), key=lambda e: e["as_of"])[-1]
            exp_cap = round((ex["actual_position"] or 0) * equity_chain[r["decision_id"]], 2)
            if abs(exp_cap - r["capital"]) > 0.011:
                cap_bad.append({"decision_id": r["decision_id"], "ledger": r["capital"],
                                "recomputed": exp_cap})
        # P0 专项：outcome=LOSS 且 pct>0
        loss_pos = [{"decision_id": r["decision_id"],
                     "outcome": r["outcome"], "pnl_pct": r["pnl_pct"],
                     "exit_price": r["exit_price"], "entry_price": r["entry_price"],
                     "kind_recomputed": next((t["recompute"].get("exit", {}).get("kind")
                                              for t in trades
                                              if t["decision_id"] == r["decision_id"]), None)}
                    for r in pnl_rows
                    if r["outcome"] == "LOSS" and (r.get("pnl_pct") or 0) > 0]
        # 对账差异汇总
        mism = []
        for t in trades:
            f = t["recompute"].get("fill") or {}
            if f and f.get("match") is False:
                mism.append({"decision_id": t["decision_id"], "layer": "fill",
                             "ledger": {"exec_date": t["exec_date"],
                                        "avg_price": t["avg_price"]},
                             "recomputed": {k: f[k] for k in
                                            ("exec_date", "open", "filled", "reason")}})
            e = t["recompute"].get("exit") or {}
            if e and e.get("match") is False:
                mism.append({"decision_id": t["decision_id"], "layer": "exit",
                             "ledger": {"outcome": t["pnl"]["outcome"],
                                        "exit_price": t["pnl"]["exit_price"],
                                        "hold_days": t["pnl"]["hold_days"],
                                        "pnl_amount": t["pnl"]["pnl_amount"]},
                             "recomputed": e})
        rep2 = {
            "window": window, "sem": sem,
            "range": rep["range"], "n_days": rep["n_days"],
            "decision_stats": rep["decision_stats"],
            "pnl_summary_ledger": rep["pnl_summary"],
            "recomputed": {
                "n_trades": len(trades),
                "n_pnl_rows": len(pnl_rows),
                "total_pnl_gross": curves["gross"]["total_pnl"],
                "final_equity_gross": curves["gross"]["final_equity"],
                "max_drawdown_pct_gross": curves["gross"]["max_drawdown_pct"],
                "ledger_pnl_total": rep["pnl_summary"]["pnl_total"],
                "ledger_match": abs(curves["gross"]["total_pnl"]
                                    - rep["pnl_summary"]["pnl_total"]) < 1e-4,
                "capital_mismatch": cap_bad,
                "fill_exit_mismatch": mism,
                "loss_with_positive_pct": loss_pos,
            },
            "cost": {
                "口径": "费率读正式参数 params.json（cost_buy_rate/cost_sell_rate，相对各自成交金额）"
                        "· v1.2 起与结算账本同源，审计=独立精算对账账本 fee_amount/net_amount",
                "n_filled": len(cost),
                "cost_total": round(sum(c["cost_total"] for c in cost), 2),
                "ledger_fee_total": round(sum((c["ledger_fee"] or 0.0) for c in cost), 2),
                "fee_match_n": sum(1 for c in cost if c["fee_match"] is True),
                "fee_mismatch": [c["decision_id"] for c in cost if c["fee_match"] is False],
                "pnl_gross_total": round(sum(c["pnl_gross"] for c in cost), 2),
                "pnl_net_total": round(sum(c["pnl_net"] for c in cost), 2),
                "net_final_equity": curves["net"]["final_equity"],
                "net_max_drawdown_pct": curves["net"]["max_drawdown_pct"],
                "旧报告粗估口径": "笔数×20万常数×0.30%%（未用逐笔真实 capital/成交价）",
            },
            "regime_cases": _regime_cases(trades),
            "sample_strata": _sample_strata(trades),
            "idempotency": rep["idempotency"],
            "days": rep["days"],
        }
        _write_evidence(window, rep2, trades, cost, curves)
        if verbose:
            _print_window(rep2)
        return rep2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _sample_strata(trades):
    out = {}
    for t in trades:
        out.setdefault(t["sample_class"], []).append(
            {"decision_id": t["decision_id"], "code": t["code"], "date": t["date"],
             "driver": t["driver"], "regime": t["regime"],
             "outcome": (t.get("pnl") or {}).get("outcome"),
             "pnl_amount": (t.get("pnl") or {}).get("pnl_amount")})
    return {k: {"n": len(v), "pnl_sum": round(sum(x["pnl_amount"] or 0 for x in v), 2),
                "trades": v} for k, v in sorted(out.items())}


# ── 确定性：同窗双跑逐笔 diff ─────────────────────────────────────────────────
def determinism(window="w2", verbose=True):
    tmps = [tempfile.mkdtemp(prefix="audit_det_a_"), tempfile.mkdtemp(prefix="audit_det_b_")]
    try:
        reps = [BR.run_batch(window, sandbox=t, second_pass=False, verbose=False) for t in tmps]
        ledgers = []
        for t in tmps:
            base = os.path.join(t, "gen")
            decisions, plans, execs, pnl_rows = _sim_ledgers(base)
            ledgers.append({
                "decision": sorted((_strip_ts(r) for r in decisions),
                                   key=lambda r: (r["date"], r["id"])),
                "risk_plan": sorted((_strip_ts(r) for r in plans),
                                    key=lambda r: (r["decision_id"], r["as_of"])),
                "execution": sorted((_strip_ts(r) for r in execs),
                                    key=lambda r: (r["decision_id"], r["as_of"])),
                "pnl": sorted((_strip_ts(r) for r in pnl_rows),
                              key=lambda r: (r["settle_date"], r["decision_id"])),
            })
        diffs = []
        for k in ("decision", "risk_plan", "execution", "pnl"):
            a, b = ledgers[0][k], ledgers[1][k]
            if len(a) != len(b):
                diffs.append({"layer": k, "type": "ROW_COUNT",
                              "a": len(a), "b": len(b)})
                continue
            for ra, rb in zip(a, b):
                ja, jb = json.dumps(ra, sort_keys=True, ensure_ascii=False), \
                    json.dumps(rb, sort_keys=True, ensure_ascii=False)
                if ja != jb:
                    key = ra.get("id") or ra.get("decision_id")
                    for kk in set(list(ra) + list(rb)):
                        if ra.get(kk) != rb.get(kk):
                            diffs.append({"layer": k, "id": key, "field": kk,
                                          "a": ra.get(kk), "b": rb.get(kk)})
        res = {"window": window, "identical": not diffs,
               "rows": {k: len(ledgers[0][k]) for k in ledgers[0]},
               "diffs": diffs[:50], "n_diffs": len(diffs),
               "pnl_total_a": reps[0]["pnl_summary"]["pnl_total"],
               "pnl_total_b": reps[1]["pnl_summary"]["pnl_total"]}
        _dump("determinism_%s.json" % window, res)
        if verbose:
            print("确定性 [%s] 双跑逐笔 diff：rows=%s identical=%s（n_diffs=%d）"
                  % (window, res["rows"], res["identical"], res["n_diffs"]))
            for d in res["diffs"][:10]:
                print("   diff: %s" % json.dumps(d, ensure_ascii=False, default=str))
        return res
    finally:
        for t in tmps:
            shutil.rmtree(t, ignore_errors=True)


def _strip_ts(r):
    return {k: v for k, v in r.items() if k not in TS_KEYS}


# ── 证据落盘（X1 豁免区：data/generated/reports/audit_replay/） ────────────────
def _dump(name, obj, *, overwrite=False):
    """证据落盘（D-01/R-02 修复）：**禁静默覆盖降级**——
      · 文件不存在 → 写；
      · 已存在且新内容逐字节同 → 幂等跳过（返回原路径）；
      · 已存在且内容不同 → AuditError 拒写（证据 = 不可变档案；确需换入必须
        显式 overwrite=True，且调用方留痕）。
    写点路径内联完整 join（落点审计器静态解析 open 目标 → 命中 X1 豁免词汇）。"""
    d = os.path.join(_ROOT, "data", "generated", "reports", "audit_replay")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    _WG.check_write(p)
    body = json.dumps(obj, ensure_ascii=False, indent=1, default=str) + "\n"
    if os.path.isfile(p) and not overwrite:
        cur = io.open(p, encoding="utf-8").read()
        if cur == body:
            return p
        raise AuditError(
            "EVIDENCE_EXISTS: 证据件 %s 已存在且内容不同（D-01：禁覆盖写静默降级）——"
            "确需换入须显式 overwrite=True 并留痕" % name)
    with io.open(os.path.join(_ROOT, "data", "generated", "reports", "audit_replay", name),
                 "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    return p


def _write_evidence(window, rep2, trades, cost, curves):
    _dump("%s_summary.json" % window, rep2)
    _dump("%s_trades.json" % window, trades)
    _dump("%s_costs.json" % window, cost)
    _dump("%s_curve_gross.json" % window, curves["gross"])
    _dump("%s_curve_net.json" % window, curves["net"])


def _print_window(r):
    print("=" * 76)
    print("逐笔审计 [%s] 语义=%s · %s → %s（%d 日）" % (r["window"], r["sem"],
                                                       r["range"][0], r["range"][1], r["n_days"]))
    rc = r["recomputed"]
    print("  逐笔: n_trades=%d pnl_rows=%d" % (rc["n_trades"], rc["n_pnl_rows"]))
    print("  独立复算总盈亏 %.2f vs 账本 %.2f → match=%s"
          % (rc["total_pnl_gross"], rc["ledger_pnl_total"], rc["ledger_match"]))
    print("  回撤(毛) %.2f%% · capital 复现不一致 %d · 成交/退出复算不一致 %d"
          % (rc["max_drawdown_pct_gross"], len(rc["capital_mismatch"]),
             len(rc["fill_exit_mismatch"])))
    if rc["fill_exit_mismatch"]:
        for m in rc["fill_exit_mismatch"][:5]:
            print("    不一致: %s" % json.dumps(m, ensure_ascii=False, default=str))
    print("  LOSS且pct>0（v1.1 专项）: %d 笔" % len(rc["loss_with_positive_pct"]))
    for x in rc["loss_with_positive_pct"][:6]:
        print("    %s %s exit=%s entry=%s kind=%s"
              % (x["decision_id"], x["outcome"], x["exit_price"], x["entry_price"],
                 x["kind_recomputed"]))
    c = r["cost"]
    print("  成本(P3): 成交 %d 笔 · 精算成本 %.2f（费率=正式参数，与账本同源对账 fee_match %d/%d%s）"
          " · 毛 %.2f → 净 %.2f · 净期末 %.2f · 净回撤 %.2f%%"
          % (c["n_filled"], c["cost_total"], c["fee_match_n"], c["n_filled"],
             (" · 不一致:" + ",".join(c["fee_mismatch"][:4])) if c["fee_mismatch"] else "",
             c["pnl_gross_total"], c["pnl_net_total"],
             c["net_final_equity"], c["net_max_drawdown_pct"]))
    print("  退潮档案例: %s"
          % json.dumps({k: len(v) for k, v in r["regime_cases"].items()}, ensure_ascii=False))
    tide = r["regime_cases"].get("退潮") or []
    if tide:
        print("    退潮档 BUY=%d · 放行=%d · plan 分布=%s"
              % (len(tide), sum(1 for x in tide if (x["position_plan"] or 0) > 0),
                 json.dumps(sorted({x["position_plan"] for x in tide}), default=str)))
    print("  样本分层:")
    for k, v in r["sample_strata"].items():
        print("    %-40s n=%3d pnl=%+10.2f" % (k, v["n"], v["pnl_sum"]))


class AuditError(Exception):
    """D-01：未知参数 / 证据覆盖拒绝（原实现零异常类、全静默）。"""


_KNOWN_ARGS = ("--post-fix", "--window", "--w1", "--w2", "--determinism")


def _main(argv):
    # D-01 修复：未知参数 **fail-closed**（原实现静默忽略——曾致 --selftest 重跑
    # 实际执行全窗审计且丢 --post-fix，证据被降级覆盖）
    unknown = [a for a in argv if a.startswith("--") and a not in _KNOWN_ARGS]
    if unknown:
        raise AuditError("UNKNOWN_ARGS: 本件不支持参数 %s（支持=%s）——禁静默忽略"
                         "（D-01：曾致 --selftest 重跑降级证据）" % (unknown, _KNOWN_ARGS))
    sem = "post" if "--post-fix" in argv else "pre"
    wins = ["w1"] if "--window" in argv and "w1" in argv else (
        ["w2"] if "--window" in argv and "w2" in argv else [])
    if "--determinism" in argv:
        determinism("w2")
        return 0
    if not wins:
        wins = ["w1", "w2"]
    for w in wins:
        audit_window(w, sem=sem)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
