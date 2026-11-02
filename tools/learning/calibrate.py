# -*- coding: utf-8 -*-
"""calibrate.py —— C6-a 零件④⑤：参数候选 / 批准转正 / Rule 生命周期 / 周报（施工 1.6）。

职责边界（用户硬约束 1 / 3 —— 不造第二套审批、不造第二规则真源）：
  * **候选链唯一**：本件只做「复测确认 → 经 0.9 `params.propose` 交候选」——
    calibration_queue **就是** 0.9 那一份（`data/calibration_queue.jsonl`），
    本件零自建队列；候选值生成 = `propose`（evidence 引 verify 结果），
    **批准 = 用户 `params.promote`**（APPROVED 末态 + approver 非空），本件
    **零 promote 调用** —— SUPPORT/OOS 通过/风险未恶化都**不会**自动转正；
  * **Rule 生命周期账本** `data/research/rules/rules.json`：三态
    `CANDIDATE → VALIDATED → DECAYING / REJECTED`。VALIDATED ≠ 自动进驱动依据区
    （驱动依据区正式启用/撤出的机械审批接口**当前不存在** → 记
    `card_sync: PENDING_GOVERNANCE` 待治理挂账，本件**零决策卡写入**）；
    DECAYING 只继续观察不删不降；REJECTED 只撤正式引用，原文与证据留 research；
  * 周度 5 指标（E6）→ `data/generated/reports/e6_weekly.jsonl`
    （X1 豁免 · 单文件 append-only，week_end 为字段）；
    **净值连续跑输基准 → 只产归因任务（demand_queue GAP 行），零自动改参/改仓**；
    基准数据缺失 → 如实记 BENCHMARK_UNAVAILABLE，不猜。

零交易动作；orders_created 恒 0。
"""

import os
import sys
import io
import ast
import json
import tempfile

_FS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools", "fs")
if _FS not in sys.path:
    sys.path.insert(0, _FS)

from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
import params as PARAMS  # noqa: E402
import pnl as PNLM  # noqa: E402
import observe as OBS  # noqa: E402
import astwin as ASTW  # noqa: E402
import case_builder as CB  # noqa: E402
import verify as VF  # noqa: E402

ACTOR = "learning"
STAGE = "C6"
SCHEMA_VERSION = "1.0"
RULES_PATH = os.path.join(ROOT, "data", "research", "rules", "rules.json")
DEMAND_PATH = os.path.join(ROOT, "data", "learning", "demand_queue.jsonl")
REPORTS_PATH = os.path.join(ROOT, "data", "generated", "reports",
                            "e6_weekly.jsonl")
RULE_STATUSES = ("CANDIDATE", "VALIDATED", "DECAYING", "REJECTED")


class CalError(Exception):
    """校准被拒（无 SUPPORT 证据 / 越权批准 / 状态机非法迁移）。"""


def _load(path):
    if not os.path.isfile(path):
        return {"schema_version": SCHEMA_VERSION, "updated_at": None,
                "rules": []}
    return json.load(io.open(path, encoding="utf-8"))


def _save(d, path=None):
    """原子写；写点 sink（os.replace 终点）内联 `path or RULES_PATH` BoolOp
    —— regime 先例：mkstemp(dir=…) 非审计 sink，replace 终点必须 BoolOp 直达。"""
    os.makedirs(os.path.dirname(path or RULES_PATH), exist_ok=True)
    _WG.check_write(path or RULES_PATH)
    d["updated_at"] = _now()
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path or RULES_PATH),
                               suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path or RULES_PATH)


def _now():
    import datetime as dt
    return dt.datetime.now().isoformat(timespec="seconds")


def _append_demand(row, path=None):
    """学习队列 append-only；写点 sink 内联 `path or DEMAND_PATH` BoolOp。"""
    os.makedirs(os.path.dirname(path or DEMAND_PATH), exist_ok=True)
    _WG.check_write(path or DEMAND_PATH)
    with io.open(path or DEMAND_PATH, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


# ── 零件④：参数候选（验证通过 → 0.9 propose；批准属用户 promote） ─────────────
def propose_param(as_of, hyp_id, candidate_value, *, params_path=None,
                  queue_path=None, verify_dir=None):
    """凭 verify SUPPORT 结果把参数候选交给 0.9 队列（唯一候选入口）。

    拒收：无该 hyp_id 验证记录 / 最新 verdict 非 SUPPORT / 缺 candidate_value /
    kind 非 PARAM。本函数**不批准**——队列候选仍须用户 promote 才生效。
    """
    res = VF.load_results(hyp_id, results_path=verify_dir)
    if not res:
        raise CalError("无 %s 验证记录 —— 未验证不得提案" % hyp_id)
    latest = res[-1]
    if latest.get("verdict") != "SUPPORT":
        raise CalError("%s verdict=%s —— 非 SUPPORT 不得提案（不得包装）"
                       % (hyp_id, latest.get("verdict")))
    if latest.get("kind") != "PARAM" or not latest.get("target"):
        raise CalError("%s 非 PARAM 类假设（RULE 走 Rule 生命周期）" % hyp_id)
    ev = ("verify:%s verdict=SUPPORT data_upto=%s segments=%s cost=%s"
          % (hyp_id, latest.get("data_upto"), len(latest.get("segments") or []),
             latest.get("cost_model")))
    return PARAMS.propose(as_of, latest["target"], candidate_value, evidence=ev,
                          source="BACKTEST", path=params_path,
                          queue_path=queue_path)


# ── 零件③末段：Rule 生命周期（账本，非第二执行真源） ─────────────────────────
def rule_candidate(as_of, hyp_id, statement, *, path=None, verify_dir=None):
    """verify SUPPORT 的 RULE 假设 → CANDIDATE 入账本（尚未生效）。"""
    res = VF.load_results(hyp_id, results_path=verify_dir)
    if not res or res[-1].get("verdict") != "SUPPORT":
        raise CalError("Rule 候选须凭 verify SUPPORT（%s 不符）" % hyp_id)
    d = _load(path)
    rid = "RULE-%s-%s" % (as_of.replace("-", ""), hyp_id.split("-")[-1])
    if any(r["rule_id"] == rid for r in d["rules"]):
        raise CalError("rule_id 重复：%s" % rid)
    d["rules"].append({
        "rule_id": rid, "status": "CANDIDATE", "statement": statement,
        "hyp_id": hyp_id,
        "evidence": ["verify:%s" % hyp_id] + list(res[-1].get("evidence") or []),
        "approver": None, "approved_at": None,
        "card_sync": "PENDING_GOVERNANCE",       # 驱动依据区接入=待治理挂账
        "refute_streak": 0, "history": [],
        "created_at": as_of})
    _save(d, path)
    return rid


def rule_approve(as_of, rule_id, approver, *, path=None, verify_dir=None):
    """用户批准 CANDIDATE → VALIDATED。approver 为空 = 拒（批准只能来自人）。"""
    if not approver or not str(approver).strip():
        raise CalError("approver 为空 —— 程序不得自行批准（候选≠生效）")
    d = _load(path)
    r = _rule_of(d, rule_id)
    if r["status"] != "CANDIDATE":
        raise CalError("%s 状态 %s ≠ CANDIDATE，不可批准" % (rule_id, r["status"]))
    res = VF.load_results(r["hyp_id"], results_path=verify_dir)
    if not res or res[-1].get("verdict") != "SUPPORT":
        raise CalError("批准前置消失：%s 现无 SUPPORT 证据" % rule_id)
    r["status"] = "VALIDATED"
    r["approver"] = str(approver).strip()
    r["approved_at"] = as_of
    r["history"].append({"at": as_of, "to": "VALIDATED", "by": r["approver"]})
    _save(d, path)
    return r


def rule_reject(as_of, rule_id, reason, *, path=None):
    """REJECTED = 从驱动依据区撤出正式引用；原文与证据留 research 可追。"""
    d = _load(path)
    r = _rule_of(d, rule_id)
    r["status"] = "REJECTED"
    r["rejected_at"] = as_of
    r["reject_reason"] = reason
    r["history"].append({"at": as_of, "to": "REJECTED", "reason": reason})
    _save(d, path)
    return r


def decay_scan(as_of, *, sim, cases_path=None, params_path=None, path=None,
               out_dir=None, verify_dir=None):
    """机械衰减扫描：VALIDATED 规则按其 condition 重验 → 连续 REFUTE 达阈值 →
    DECAYING（继续观察，不删不降级——除非规则自身定义有降级条款）。"""
    d = _load(path)
    streak_max = PARAMS.get_value("rule_decay_refute_streak", path=params_path)
    changed = []
    for r in d["rules"]:
        if r["status"] not in ("VALIDATED", "DECAYING"):
            continue
        _prev = VF.load_results(r["hyp_id"], results_path=verify_dir)
        hyp = {"hyp_id": r["hyp_id"], "kind": "RULE", "statement": r["statement"],
               "condition": (_prev or [{}])[-1].get("condition"),
               "evidence": r["evidence"]}
        if not hyp["condition"]:
            continue
        out = VF.verify(hyp, sim=sim, cases_path=cases_path,
                        params_path=params_path, results_path=out_dir)
        if out["verdict"] == "REFUTE":
            r["refute_streak"] = int(r.get("refute_streak") or 0) + 1
        elif out["verdict"] == "SUPPORT":
            r["refute_streak"] = 0
        if r["status"] == "VALIDATED" and r["refute_streak"] >= streak_max:
            r["status"] = "DECAYING"
            r["decayed_at"] = as_of
            r["history"].append({"at": as_of, "to": "DECAYING",
                                 "reason": "连续 REFUTE %d 次（机械扫描）"
                                           % r["refute_streak"]})
            changed.append(r["rule_id"])
    _save(d, path)
    return {"changed": changed, "as_of": as_of}


def _rule_of(d, rule_id):
    for r in d["rules"]:
        if r["rule_id"] == rule_id:
            return r
    raise CalError("rule 不存在：%s" % rule_id)


# ── 周度 5 指标（E6）+ 强制归因判据 ──────────────────────────────────────────
def weekly_report(week_end, *, initial_capital, benchmark=None, params_path=None,
                  reports_path=None, demand_path=None, base=None,
                  cases_path=None, rules_path=None):
    """周度 5 指标报告（只读账本派生）+ 跑输判据 → 只产归因任务。

    5 指标：净值曲线 / 判断质量分布 / 重复错误数 / Case 召回有效率 / Rule OOS。
    拿不到的指标 = 显式 UNAVAILABLE（不造数）。跑输 `weekly_underperform_weeks`
    连续周 → demand_queue 记 FORCED_ATTRIBUTION 任务（零自动改参/改仓/改策略）；
    基准缺失 → BENCHMARK_UNAVAILABLE 留痕。
    """
    live = list(PNLM.iter_pnl(False, base=base))
    curve = PNLM.net_curve(False, initial_capital, base=base)["points"]
    verd = {k: 0 for k in PNLM.VERDICTS}
    wrong = {k: 0 for k in PNLM.L1L5_KEYS}
    for r in live:
        for s in PNLM.SLOTS:
            v = (r.get(s) or {}).get("verdict")
            if v:
                verd[v] = verd.get(v, 0) + 1
        for k in PNLM.L1L5_KEYS:
            if (r.get("attribution_L1L5") or {}).get(k) == "WRONG":
                wrong[k] += 1
    n_cases = len(CB.load_cases(sim=False, path=cases_path))
    rules = _load(rules_path or RULES_PATH)["rules"]
    by_status = {}
    for r in rules:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    wk = PARAMS.get_value("weekly_underperform_weeks", path=params_path)
    under = _under_streak(curve, benchmark, week_end)
    gap = None
    if under is None:
        gap = {"kind": "BENCHMARK_UNAVAILABLE",
               "where": "E6 周报基准对比", "detail": "沪深300 投放数据缺失，不猜"}
    elif under >= wk:
        gap = {"kind": "FORCED_ATTRIBUTION",
               "where": "E6 周报净值对比",
               "detail": "连续 %d 周跑输基准 → 强制归因（≠自动改参数）" % under}
    if gap:
        _append_demand({"as_of": week_end, "schema_version": SCHEMA_VERSION,
                        "kind": gap["kind"], "gaps": [gap], "n_settled": len(live),
                        "scanner": "E6_WEEKLY", "created_at": _now()},
                       path=demand_path)
    rep = {"schema_version": SCHEMA_VERSION, "week_end": week_end,
           "net_curve_points": len(curve),
           "judgement_verdicts": verd, "repeat_wrong_by_L": wrong,
           "case_recall_rate": "UNAVAILABLE",      # 召回追踪未建 → 如实
           "rule_status_counts": by_status,
           "underperform_streak": under if under is not None else "UNAVAILABLE",
           "forced_attribution": bool(gap and gap["kind"] == "FORCED_ATTRIBUTION")}
    os.makedirs(os.path.dirname(reports_path or REPORTS_PATH), exist_ok=True)
    _WG.check_write(reports_path or REPORTS_PATH)
    # ★ 写点 sink 内联 `reports_path or REPORTS_PATH` BoolOp（审计静态解析要求直达）
    # E6 周报 = 单文件 append-only jsonl（week_end 为字段）；人读视图可后续派生
    row = dict(rep)
    row["kind"] = "E6_WEEKLY"
    with io.open(reports_path or REPORTS_PATH, "a", encoding="utf-8",
                 newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    rep["report_path"] = reports_path or REPORTS_PATH
    return rep


def _under_streak(curve, benchmark, week_end):
    """净值 vs 基准周收益连周比较；基准缺失 → None（不猜）。

    benchmark = [{"week_end": "YYYY-MM-DD", "ret": 周收益率}, ...]（loader 投放派生，
    由调用方从 ext index 数据机械折算；无投放 → None）。
    """
    if not benchmark:
        return None
    pts = [p for p in curve if (p.get("settle_date") or "") <= week_end]
    if len(pts) < 2:
        return 0
    streak = 0
    for b in sorted(benchmark, key=lambda x: x["week_end"]):
        sys_ret = _week_ret(curve, b["week_end"])
        if sys_ret is None:
            return None
        if sys_ret < b["ret"]:
            streak += 1
        else:
            streak = 0
    return streak


def _week_ret(curve, week_end):
    import datetime as dt
    d0 = (dt.date.fromisoformat(week_end) - dt.timedelta(days=6)).isoformat()
    ps = [p for p in curve if d0 <= (p.get("settle_date") or "") <= week_end]
    if len(ps) < 2:
        return None
    a, b = ps[0]["equity"], ps[-1]["equity"]
    return (b - a) / a if a else None


# ── 自检（全临时沙盒，真库零接触） ────────────────────────────────────────────
_TEST_FUNCS = ("_selftest", "_discipline")


def _selftest():
    ok = [0]
    bad = [0]

    def T(name, cond):
        if cond:
            ok[0] += 1
            print("  [✓] %s" % name)
        else:
            bad[0] += 1
            print("  [✗] %s" % name)

    print("calibrate 自检（全临时沙盒，真库零接触）：")
    sb = tempfile.mkdtemp(prefix="cal_sb_")
    rules_p = os.path.join(sb, "research", "rules", "rules.json")
    queue_p = os.path.join(sb, "calibration_queue.jsonl")
    params_p = os.path.join(sb, "params.json")
    demand_p = os.path.join(sb, "learning", "demand_queue.jsonl")
    rep_p = os.path.join(sb, "reports", "e6_weekly.jsonl")
    with io.open(params_p, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": "t", "updated_at": "2026-10-09", "params": {
            "trial_position": {"value": 0.1},
            "verify_intervals": {"value": 2},
            "verify_min_samples": {"value": 5},
            "verify_min_edge": {"value": 0.1},
            "verify_cost_model": {"value": "NONE_SET"},
            "weekly_underperform_weeks": {"value": 2},
            "rule_decay_refute_streak": {"value": 2}}}, f)

    # 真沙盒 verify 结果（PARAM 类 SUPPORT）—— 走 verify 真件产证据
    import verify as _VF
    cases = os.path.join(sb, "cases.jsonl")
    rows = _VF._mk_cases([(4, 1, 2, 3)] * 3)
    with io.open(cases, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    hyp = {"hyp_id": "HYP-P1", "kind": "PARAM", "target": "trial_position",
           "statement": "Catalyst=A 条件下试仓参数条件复测",
           "condition": {"field": "Catalyst", "op": "==", "value": "A"},
           "evidence": ["OBS-Catalyst-A"], "candidate_value": 0.08}
    vres = _VF.verify(hyp, sim=True, cases_path=cases,
                      params_path=params_p,
                      results_path=os.path.join(sb, "research", "verify",
                                                "results.jsonl"))
    T("P0 前置：沙盒 PARAM 假设 verify=SUPPORT", vres["verdict"] == "SUPPORT")

    # ── A 组：候选链（只 propose，不批准） ──
    try:
        propose_param("2026-10-09", "HYP-NOPE", 0.08, params_path=params_p,
                      queue_path=queue_p)
        T("A1 无验证记录 → 拒（未验证不得提案）", False)
    except CalError:
        T("A1 无验证记录 → 拒（未验证不得提案）", True)
    q = propose_param("2026-10-09", "HYP-P1", 0.08, params_path=params_p,
                      queue_path=queue_p,
                      verify_dir=os.path.join(sb, "research", "verify", "results.jsonl"))
    T("A2 SUPPORT → 走 0.9 propose 入队（evidence 引 verify）",
      "QC-" in json.dumps(q, ensure_ascii=False)
      and "verify:HYP-P1" in json.dumps(q, ensure_ascii=False))
    T("A3 候选只进 0.9 队列、params.json 正式值未动",
      os.path.isfile(queue_p)
      and json.load(io.open(params_p, encoding="utf-8"))
      ["params"]["trial_position"]["value"] == 0.1)

    # ── B 组：Rule 生命周期 ──
    try:
        rule_candidate("2026-10-09", "HYP-NOPE", "伪规则", path=rules_p)
        T("B1 无 SUPPORT 不得立 Rule 候选", False)
    except CalError:
        T("B1 无 SUPPORT 不得立 Rule 候选", True)
    rid = rule_candidate("2026-10-09", "HYP-P1", " Catalyst=A 复测成立",
                         path=rules_p,
                         verify_dir=os.path.join(sb, "research", "verify", "results.jsonl"))
    d = _load(rules_p)
    T("B2 候选态 CANDIDATE + card_sync=PENDING_GOVERNANCE（待治理挂账）",
      d["rules"][0]["status"] == "CANDIDATE"
      and d["rules"][0]["card_sync"] == "PENDING_GOVERNANCE")
    try:
        rule_approve("2026-10-09", rid, "", path=rules_p)
        T("B3 approver 为空 → 拒（程序不得自行批准）", False)
    except CalError:
        T("B3 approver 为空 → 拒（程序不得自行批准）", True)
    rule_approve("2026-10-09", rid, "USER", path=rules_p,
                 verify_dir=os.path.join(sb, "research", "verify", "results.jsonl"))
    T("B4 用户批准 → VALIDATED（留 approver/批准日）",
      _load(rules_p)["rules"][0]["status"] == "VALIDATED")

    # ── C 组：衰减扫描（连续两次 REFUTE 达阈值） ──
    rows_bad = _VF._mk_cases([(2, 3, 2, 3)] * 3)
    with io.open(cases, "w", encoding="utf-8", newline="\n") as f:
        for r in rows_bad:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    vd = os.path.join(sb, "research", "verify", "results.jsonl")
    decay_scan("2026-10-08", sim=True, cases_path=cases,
               params_path=params_p, path=rules_p, out_dir=vd, verify_dir=vd)
    dscan = decay_scan("2026-10-09", sim=True, cases_path=cases,
                       params_path=params_p, path=rules_p, out_dir=vd,
                       verify_dir=vd)
    d2 = _load(rules_p)
    T("C1 连续 REFUTE 达阈值 → DECAYING（继续观察，行保留）",
      d2["rules"][0]["status"] == "DECAYING"
      and rid in dscan["changed"])
    T("C2 DECAYING 不删除：原文/证据/历史全留",
      d2["rules"][0]["statement"] and d2["rules"][0]["evidence"]
      and len(d2["rules"][0]["history"]) >= 2)
    rule_reject("2026-10-09", rid, "人工复核撤出", path=rules_p)
    T("C3 REJECTED：状态记录 + 原文留账本可追",
      _load(rules_p)["rules"][0]["status"] == "REJECTED"
      and _load(rules_p)["rules"][0]["statement"])

    # ── D 组：周报 + 强制归因只产任务（真账本：3 笔亏损跨两周） ──
    import thesis as TH
    import decision as DEC
    TH.init_file(os.path.join(sb, "thesis.md"))
    for code, ddate in (("600001", "2026-09-26"), ("600002", "2026-10-02"),
                        ("600003", "2026-10-03"), ("600004", "2026-10-09")):
        th = TH.create_thesis(ddate, "A", "亏损样本", code=code,
                              gap=TH.GAP_NONE, path=os.path.join(sb, "thesis.md"))
        chain = {"evidence": ["e"], "inference": ["i"],
                 "prediction": [{"prediction_id": s, "metric": "m",
                                 "data_source": "d", "baseline": "b",
                                 "direction": "UP", "threshold": 1.0,
                                 "condition": "c", "window": 3}
                                for s in ("M1", "M2", "M3")],
                 "five": {"驱动存在": "x", "当前证据": "x", "延续机制": "x",
                          "失效条件": "x", "剩余预期": "x"}}
        env = {"leg": "EOD", "source": "EXPLICIT", "regime": "弱势",
               "signals": {}}
        dd = DEC.write_decision(ddate, code, "BUY", th["id"], chain, sim=False,
                                driver="A", thesis_state=th, entry_limit=10.0,
                                stop_line=9.5, target=12.0, window=5,
                                position_request=0.08, env=env, base=sb,
                                thesis_path=os.path.join(sb, "thesis.md"))
        DEC.append_risk_plan(dd["id"], 0.08, gates={"g": "ok"}, base=sb)
        DEC.append_execution(dd["id"], 0.08, "filled", as_of=ddate,
                             avg_price=10.0, base=sb)
        verd = {s: {"prediction_id": s, "metric": "m", "data_source": "d",
                    "verdict": "REFUTE", "observed": -1.0, "note": ""}
                for s in ("M1", "M2", "M3")}
        att5 = {k: "WRONG" for k in PNLM.L1L5_KEYS}
        att3 = {k: "WRONG" for k in PNLM.THREE_KEYS}
        PNLM.write_pnl(dd["id"], "LOSS", verdicts=verd,
                       attribution_L1L5=att5, attribution_三问=att3,
                       entry_price=10.0, exit_price=9.7, capital=100000.0,
                       exit_kind="STOP", exit_date=ddate,
                       settle_date=ddate, base=sb)
    rep = weekly_report("2026-10-09", initial_capital=100000.0,
                        benchmark=None, params_path=params_p,
                        reports_path=rep_p, demand_path=demand_p, base=sb,
                        cases_path=cases, rules_path=rules_p)
    T("D1 周报 5 指标齐全、缺的显式 UNAVAILABLE",
      os.path.basename(rep["report_path"]) == "e6_weekly.jsonl"
      and rep["case_recall_rate"] == "UNAVAILABLE")
    dm = [json.loads(l) for l in io.open(demand_p, encoding="utf-8")]
    T("D2 基准缺失 → BENCHMARK_UNAVAILABLE 留痕（不猜）",
      any(g["kind"] == "BENCHMARK_UNAVAILABLE" for g in dm))
    bench = [{"week_end": "2026-10-02", "ret": 0.05},
             {"week_end": "2026-10-09", "ret": 0.05}]
    curve = [{"settle_date": "2026-09-26", "equity": 97000.0},
             {"settle_date": "2026-10-02", "equity": 94000.0},
             {"settle_date": "2026-10-03", "equity": 94000.0},
             {"settle_date": "2026-10-09", "equity": 91000.0}]
    T("D3 周收益 -3% < 基准 5% → 连跑输计数 = 2",
      _under_streak(curve, bench, "2026-10-09") == 2)
    rep2 = weekly_report("2026-10-09", initial_capital=100000.0,
                         benchmark=bench, params_path=params_p,
                         reports_path=rep_p, demand_path=demand_p, base=sb,
                         cases_path=cases, rules_path=rules_p)
    dm2 = [json.loads(l) for l in io.open(demand_p, encoding="utf-8")]
    T("D4 连 2 周跑输 → 只产 FORCED_ATTRIBUTION 任务（零自动改参）",
      rep2["forced_attribution"]
      and any(g["kind"] == "FORCED_ATTRIBUTION" for g in dm2)
      and json.load(io.open(params_p, encoding="utf-8"))
      ["params"]["trial_position"]["value"] == 0.1)

    # ── E 组：越权与真库 ──
    prod = _prod_src()
    T("E1 零 promote 调用（批准只能来自用户）", "promote(" not in prod)
    T("E2 零 decision_card 写入", "decision_card" not in prod)
    T("E3 零 params.json 写接口（只经 propose 入队）",
      "_write_params" not in prod and "params.json\"" not in prod)
    T("E4 自检前后真库 rules/queue/demand 逐字节未变",
      _md5(RULES_PATH) == _M_RULES and _md5(os.path.join(
          ROOT, "data", "calibration_queue.jsonl")) == _M_QUEUE
      and _md5(DEMAND_PATH) == _M_DEMAND)
    print("calibrate 自检：%d/%d" % (ok[0], ok[0] + bad[0]))
    return bad[0] == 0


def _md5(p):
    import hashlib
    return hashlib.md5(io.open(p, "rb").read()).hexdigest() if os.path.isfile(p) else "-"


_M_RULES = _M_QUEUE = _M_DEMAND = None


def _prod_src():
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    lines = src.splitlines(True)
    drop = set()
    for lo, hi, name in ASTW.func_table(ast.parse(src)):
        if name in _TEST_FUNCS:
            drop.update(range(lo - 1, hi))
    return "".join(l for i, l in enumerate(lines) if i not in drop)


def _discipline():
    print("calibrate 纪律：")
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    prod = _prod_src()
    res = [
        ("① 写点静态可解析（rules/demand/reports sink 内联 BoolOp）",
         "path or RULES_PATH" in prod and "path or DEMAND_PATH" in prod
         and "reports_path or REPORTS_PATH" in prod),
        ("② 零 promote 调用（候选≠生效，批准属用户）",
         "promote(" not in prod),
        ("③ 零 decision_card / position_gate 写入",
         "decision_card" not in prod and "position_gate" not in prod),
        ("④ Rule 状态机值域封闭", "RULE_STATUSES" in prod
         and "CANDIDATE" in prod and "PENDING_GOVERNANCE" in prod),
        ("⑤ astwin 引用", bool(ASTW.func_table(ast.parse(src)))),
    ]
    for name, c in res:
        print("  [%s] %s" % ("✓" if c else "✗", name))
    print("calibrate 纪律：%d/%d" % (sum(1 for _, c in res if c), len(res)))
    return all(c for _, c in res)


def _main(argv):
    global _M_RULES, _M_QUEUE, _M_DEMAND
    if "--selftest" in argv:
        _M_RULES = _md5(RULES_PATH)
        _M_QUEUE = _md5(os.path.join(ROOT, "data", "calibration_queue.jsonl"))
        _M_DEMAND = _md5(DEMAND_PATH)
        return 0 if _selftest() else 1
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    print("用法：calibrate.py --selftest | --discipline "
          "（propose_param/rule_*/decay_scan/weekly_report 经 Python API 调用）")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
