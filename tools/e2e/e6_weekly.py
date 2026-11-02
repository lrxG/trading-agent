#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""e6_weekly.py · E6 周度报告生产者（任务9#9 · v1.0 · 2026-10-12 建）

用途：把**某一周的**净值 / 判断质量 / 重复错误 / Case 召回 / Rule OOS /
跑输归因（C6 已触发结果的展示）/ 待批准候选，汇成一份**可复现、确定性生成**
的周报（数据全部来自账本 + C6 已落产物，零 LLM、零手工编辑），
落 `data/generated/reports/e6_weekly.jsonl`（append-only，一行 = 一个
(axis, week_end)；v3.18 E3 先例：按日命名文件改单一 append-only 文件 +
日期字段，registry E6 output_path 的 e6_<as_of>.md 由此收口为 e6_weekly.jsonl
—— md 渲染内嵌在行的 `report_md` 字段）。

★ 一、E6 = **展示层**（设计 3.1，用户 2026-09-30 批准）：7 段产物
  ① 净值 vs 基准（曲线/周收益/回撤）② 判断质量分布（SUPPORT/MIXED/REFUTE
  趋势）③ 重复错误（聚类 + 周对比）④ Case 召回（**分阶段**）⑤ Rule OOS
  成绩单 ⑥ 净值跑输归因（**展示 C6 已触发结果，不自行触发归因**）
  ⑦ 待批准候选（APPROVE/REJECT/KEEP_TESTING 入口）。
  **E6 不自行触发归因**、零交易动作、零参数写入、零自动改判；动作归 C5 裁决、
  评价归 E3。

★ 二、数据来源边界（E 系列统一纪律：消费 C/S 输出，不直连不重算）：
  · 账本原语（只读）= pnl 账本（净值曲线/判断质量/L1L5）+ decision 账本
    （周内决策与 env 档）+ params_changelog（参数变更）——由本件派生展示视图；
  · C6 已落产物（只读文件）= cases.jsonl（Case 样本）/ rules.json（规则状态）/
    verify/results.jsonl（OOS 结果）/ calibration_queue.jsonl（待批准候选）/
    demand_queue.jsonl（C6 已触发的归因任务）；
  · **缺失 = 显式 UNAVAILABLE + 缺口入 gaps**（基准无源 / error_type 字段不存在 /
    召回追踪未建 / 规则库空），**不造数不冒充**。

★ 三、可复现性（机械判据）+ 不输出条件：
  · 全部输入 = 账本 + C6 产物文件 —— 无外部时变源；
  · 行幂等：同 (axis, week_end) 重跑，内容 hash（剔除 generated_at）一致 →
    EXISTS 跳过；不一致 → 拒（**append-only 不改写**）；
  · md 渲染 = 纯函数，同 report 逐字节同输出；
  · 不输出条件（设计 3.1）：①本周零交易日 → 一行"本周无交易日"；②账本缺结算
    数据 → 标缺口不硬凑；③全周零决策 → 精简版（1/6/7 段）。

被谁调用：C6 周度结算完成后的定时触发（接线挂账下一批；本批交付生产者 + CLI 真跑）。
用法：
  python tools/e2e/e6_weekly.py --week-end 2026-09-30 [--sim] [--stdout]
  python tools/e2e/e6_weekly.py --selftest
"""
import argparse
import datetime as _dt
import hashlib
import io
import json
import os
import sys

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
sys.path.insert(0, _FS)
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
import decision as DEC        # noqa: E402
import pnl as PNL            # noqa: E402
import params as PARAMS       # noqa: E402
import cursor as CUR          # noqa: E402
from paths import ROOT        # noqa: E402

REPORT = os.path.join(ROOT, "data", "generated", "reports", "e6_weekly.jsonl")
# 落点说明：X1 reports/ + cap 前缀 e6_ → 注册表 E6（_CAP_REF_RULES (e[0-9]+)_ 命中，
# want=E6 ∈ reg_ids）。单一 append-only 文件（v3.18 教训：按日命名 = <dyn> 不可登记）。
SCHEMA_VERSION = "1.0"
CAP = "E6"

CALQ = os.path.join(ROOT, "data", "calibration_queue.jsonl")
DEMAND = os.path.join(ROOT, "data", "learning", "demand_queue.jsonl")
RULES = os.path.join(ROOT, "data", "research", "rules", "rules.json")
VERIFY = os.path.join(ROOT, "data", "research", "verify", "results.jsonl")
CASES = os.path.join(ROOT, "data", "generated", "cases.jsonl")

_DEFAULT_CAPITAL = 1000000.0
# 意图断言词（锁死：E6 零动作零裁量，禁把账本数字写成"该买/该卖"结论）。
_INTENT_WORDS = ("建议" + "买入", "该" + "建仓", "应当" + "清仓", "应" + "加仓")


class E6Error(Exception):
    """周报被拒（幂等冲突 / 非法日期 / 篡改型重写）——显式抛错。"""


def _chk_date(date, name="week_end"):
    s = str(date)
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise E6Error("%s 必须是 YYYY-MM-DD，收到: %r" % (name, date))
    return s


def _shift(date, days):
    return (_dt.date.fromisoformat(date) + _dt.timedelta(days=days)).isoformat()


# ── 只读取数（账本 + C6 产物文件；确定性）─────────────────────────────────────
def _rows(path):
    """jsonl 只读装载；文件不存在 → []（如实空，不造数）。"""
    if not os.path.isfile(path):
        return []
    out = []
    with io.open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    continue
    return out


def _load_json(path, default):
    if not os.path.isfile(path):
        return default
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def _week_slice(rows, date_key, start, end):
    return [r for r in rows if start <= str(r.get(date_key) or "") <= end]


def _drawdown(points, key="equity"):
    """峰值回撤（%）：基于 equity 序列；样本 <2 → None。"""
    vals = [p.get(key) for p in points if p.get(key) is not None]
    if len(vals) < 2:
        return None
    peak, mdd = vals[0], 0.0
    for v in vals:
        if v > peak:
            peak = v
        elif peak:
            mdd = max(mdd, (peak - v) / peak)
    return round(mdd * 100, 4)


def _seg_ret(points, start, end):
    """区间收益率（首/末 equity）；样本不足 → None。"""
    ps = [p for p in points if start <= str(p.get("settle_date") or "") <= end
          and p.get("equity") is not None]
    if len(ps) < 2:
        return None
    a, b = ps[0]["equity"], ps[-1]["equity"]
    return round((b - a) / a, 6) if a else None


def _curve_until(sim, init, until, base=None):
    """净值曲线**截止** week_end（防未来数据泄漏）。

    PNL.iter_pnl/net_curve 的 as_of 是**精确匹配 settle_date**（非截止语义），
    故此处读全量后按 `settle_date <= until` 截断并重累积 cumulative —— 金额直接取
    账本 net_amount（零复制口径），累计和属展示派生非分析结论。
    """
    pts_all = PNL.net_curve(sim, initial_capital=init, base=base)["points"]
    pts, cum = [], 0.0
    for p in pts_all:                       # net_curve 已按 settle_date 升序
        if str(p.get("settle_date") or "") > until:
            break
        cum += p.get("net_amount") or 0.0
        q = dict(p)
        q["cum_net"] = round(cum, 6)
        q["cum_pnl"] = round(cum, 6)
        q["equity"] = None if init is None else round(init + cum, 6)
        pts.append(q)
    return pts


def _rows_until(sim, until, base=None):
    """结算行**截止** week_end（同上：as_of 非截止语义）。"""
    return [r for r in PNL.iter_pnl(sim, base=base)
            if str(r.get("settle_date") or "") <= until]


# ── ① 净值 vs 基准 ───────────────────────────────────────────────────────────
def net_value_seg(sim, week_end, week_start, initial_capital, benchmark, base=None):
    """净值段：全区间曲线 + 本周收益 + 全区间/本周回撤；基准缺 → UNAVAILABLE。"""
    pts = _curve_until(sim, initial_capital, week_end, base=base)
    wk = [p for p in pts if week_start <= str(p.get("settle_date") or "") <= week_end]
    ret_wk = _seg_ret(pts, week_start, week_end)
    bm = {"status": "UNAVAILABLE", "reason": "BENCHMARK_UNAVAILABLE",
          "detail": "沪深300 投放数据缺失（旧包仅 sh000001 上证指数，非 E6 契约基准），"
                    "不猜不代用",
          "source": "loader 盘后腿 ext index 未投放"}
    excess = None
    if benchmark:
        bret = benchmark.get(week_end)
        if bret is not None and ret_wk is not None:
            bm = {"status": "OK", "week_ret": bret, "source": "调用方投放"}
            excess = round(ret_wk - bret, 6)
    return {"n_points": len(pts), "n_points_week": len(wk),
            "equity_end": (pts[-1]["equity"] if pts else None),
            "total_pnl": (pts[-1]["cum_net"] if pts else 0.0),
            "total_pnl_gross": round(sum(p.get("pnl_amount") or 0.0 for p in pts), 6),
            "total_fee": round(sum(p.get("fee_amount") or 0.0 for p in pts), 6),
            "week_ret": ret_wk, "cum_ret": _seg_ret(pts, "0000-01-01", week_end),
            "max_drawdown_pct": _drawdown(pts),
            "week_max_drawdown_pct": _drawdown(wk),
            "benchmark": bm, "excess_ret": excess,
            "curve_points": len(pts)}


# ── ② 判断质量分布 ───────────────────────────────────────────────────────────
def _vdist(rows):
    """判定分布（SUPPORT/MIXED/REFUTE + NA 计数）；NA = 槽位无判定或非三值。"""
    d = {v: 0 for v in PNL.VERDICTS}
    d["NA"] = 0
    for r in rows:
        for s in PNL.SLOTS:
            v = (r.get(s) or {}).get("verdict")
            d[v if v in PNL.VERDICTS else "NA"] += 1
    return d


def _wrong_by_L(rows):
    out = {k: 0 for k in PNL.L1L5_KEYS}
    for r in rows:
        for k in PNL.L1L5_KEYS:
            if (r.get("attribution_L1L5") or {}).get(k) == "WRONG":
                out[k] += 1
    return out


def judgement_seg(sim, week_end, week_start, prev_start, prev_end, base=None):
    """判断质量段：累计 verdict + L1L5 WRONG + 本周 + 周环比趋势。"""
    all_rows = _rows_until(sim, week_end, base=base)
    wk = _week_slice(all_rows, "settle_date", week_start, week_end)
    pv = _week_slice(all_rows, "settle_date", prev_start, prev_end)
    cur, prev = _vdist(wk), _vdist(pv)
    trend = {k: cur.get(k, 0) - prev.get(k, 0) for k in sorted(set(cur) | set(prev))}
    return {"cum_verdict_dist": _vdist(all_rows), "week_verdict_dist": cur,
            "prev_week_verdict_dist": prev, "trend": trend,
            "week_l1l5_wrong": _wrong_by_L(wk),
            "prev_week_l1l5_wrong": _wrong_by_L(pv),
            "cum_l1l5_wrong": _wrong_by_L(all_rows),
            "n_settled_cum": len(all_rows), "n_settled_week": len(wk),
            "n_settled_prev_week": len(pv)}


# ── ③ 重复错误 ───────────────────────────────────────────────────────────────
def repeat_error_seg(judgement):
    """重复错误段：L1–L5 WRONG 聚类 + 周对比；error_type 聚类 = 字段不存在。"""
    cur = judgement["week_l1l5_wrong"]
    prev = judgement["prev_week_l1l5_wrong"]
    delta = {k: cur.get(k, 0) - prev.get(k, 0) for k in PNL.L1L5_KEYS}
    return {"week_by_L": cur, "prev_week_by_L": prev, "delta_by_L": delta,
            "cum_by_L": judgement["cum_l1l5_wrong"],
            "error_type_cluster": {
                "status": "UNAVAILABLE", "reason": "FIELD_ABSENT",
                "detail": "全库无 error_type 字段（decision/pnl 账本均无）——"
                          "聚类口径未定，不造数；L1–L5 归因计数为现有可支撑口径"}}


# ── ④ Case 召回（分阶段启用，设计锁死）───────────────────────────────────────
def case_recall_seg(sim, min_samples):
    """Case 召回段：阶段 A（只展示记录，不计算有效率）。

    阶段 B 门槛 N 由 params 的 `case_recall_min_samples` 提供（**由 C6 校准**，
    设计明示"不拍死"）；该键缺失 → 无法判定达标 → 停阶段 A 并显式标注 N 未定。
    """
    cases = _rows(CASES)
    cases = [c for c in cases if sim is None or c.get("sim") == sim] if sim is not None \
        else cases
    n = len(cases)
    stage = "A"
    why = "阶段 B 门槛 N（params.case_recall_min_samples）未定 → 停阶段 A（设计：N 由 C6 校准）"
    if min_samples is not None and n >= min_samples:
        stage, why = "B", "样本达门槛 N=%s" % min_samples
    return {"stage": stage, "stage_reason": why,
            "n_cases": n, "min_samples": min_samples,
            "recall_count": None, "recall_list": [], "judgement_change_count": None,
            "judgement_change_types": [],
            "efficiency": {"status": "UNAVAILABLE", "reason": "RECALL_TRACKING_NOT_BUILT",
                           "detail": "召回追踪未建（判断变化是否以 Case 为依据无快照证据）"
                                     "——阶段 A 只展示记录不计算有效率，防误归功"}}


# ── ⑤ Rule OOS 成绩单 ────────────────────────────────────────────────────────
def rule_oos_seg():
    """Rule OOS 段：规则状态计数 + verify 结果；空态如实。"""
    rules = _load_json(RULES, {"rules": []}).get("rules") or []
    by_status = {}
    for r in rules:
        by_status[r.get("status")] = by_status.get(r.get("status"), 0) + 1
    results = _rows(VERIFY)
    vd = {}
    for r in results:
        v = r.get("verdict")
        if v:
            vd[v] = vd.get(v, 0) + 1
    detail = ("rules.json %d 条 · verify 结果 %d 条" % (len(rules), len(results)))
    return {"n_rules": len(rules), "rule_status_counts": by_status,
            "n_oos_results": len(results), "oos_verdict_dist": vd,
            "oos_status": "OK" if results else "UNAVAILABLE",
            "detail": detail}


# ── ⑥ 净值跑输归因（展示 C6 已触发结果，不自行触发）──────────────────────────
def underperform_seg(sim, week_end, week_start, net_value, threshold, benchmark):
    """跑输归因段：展示 C6 已落归因任务；基准缺 → UNAVAILABLE（不猜）。"""
    tasks = [r for r in _rows(DEMAND) if r.get("kind") == "FORCED_ATTRIBUTION"]
    tasks = [t for t in tasks if (t.get("as_of") or "") <= week_end]
    if benchmark:
        status = "OK" if net_value.get("excess_ret") is not None \
            else "UNAVAILABLE"
        reason = None if status == "OK" else "WEEK_RET_MISSING"
    else:
        status, reason = "UNAVAILABLE", "BENCHMARK_UNAVAILABLE"
    return {"threshold_weeks": threshold, "streak": None, "status": status,
            "reason": reason, "forced_tasks": tasks, "n_forced_tasks": len(tasks),
            "detail": "E6 只展示 C6 已触发的归因结果，不自行触发归因；基准缺失 "
                      "→ 连周跑输无法计算，如实 UNAVAILABLE"}


# ── ⑦ 待批准候选 ─────────────────────────────────────────────────────────────
def pending_seg(week_end):
    """待批准候选段：calibration_queue 候选 + 近期 demand 任务；入口为人工裁定。"""
    cal = [r for r in _rows(CALQ) if (r.get("as_of") or "") <= week_end]
    dem = [r for r in _rows(DEMAND) if (r.get("as_of") or "") <= week_end]
    return {"n_calibration": len(cal), "calibration_queue": cal,
            "n_demand": len(dem), "demand_recent": dem[-8:],
            "entry": "APPROVE / REJECT / KEEP_TESTING（人工裁定；E6 零改参零自动批准）",
            "status": "OK" if (cal or dem) else "EMPTY",
            "detail": "候选 %d 条 · 需求任务 %d 条（空态 = 尚无候选，非能力缺失）"
                      % (len(cal), len(dem))}


# ── 组装 ─────────────────────────────────────────────────────────────────────
def build_report(week_end, sim=False, initial_capital=None, benchmark=None,
                 base=None, params_path=None):
    week_end = _chk_date(week_end)
    week_start = _shift(week_end, -6)
    prev_end = _shift(week_start, -1)
    prev_start = _shift(prev_end, -6)
    cap0 = initial_capital if initial_capital is not None else _DEFAULT_CAPITAL

    decs = [d for d in DEC.iter_decisions(sim, base=base)
            if week_start <= str(d.get("date") or "") <= week_end]
    envs = sorted({(d.get("env") or {}).get("regime") if isinstance(d.get("env"), dict)
                   else None for d in decs} - {None})

    nv = net_value_seg(sim, week_end, week_start, cap0, benchmark, base=base)
    jd = judgement_seg(sim, week_end, week_start, prev_start, prev_end, base=base)
    thr = PARAMS.get_value("weekly_underperform_weeks", default=None, path=params_path)
    n_min = PARAMS.get_value("case_recall_min_samples", default=None, path=params_path)

    gaps = []
    if nv["benchmark"]["status"] != "OK":
        gaps.append({"seg": "① 净值 vs 基准", "kind": "BENCHMARK_UNAVAILABLE",
                     "detail": nv["benchmark"]["detail"]})
    if jd["n_settled_week"] == 0:
        gaps.append({"seg": "② 判断质量分布", "kind": "NO_SETTLEMENT_WEEK",
                     "detail": "本周无结算行 → 本周判定分布为空（累计面仍可用）"})
    gaps.append({"seg": "③ 重复错误", "kind": "FIELD_ABSENT",
                 "detail": "error_type 字段不存在 → 聚类不可产（L1–L5 计数可用）"})
    cr = case_recall_seg(sim, n_min)
    if cr["stage"] == "A":
        gaps.append({"seg": "④ Case 召回", "kind": "STAGE_A_ONLY",
                     "detail": cr["stage_reason"]})
    ro = rule_oos_seg()
    if ro["n_rules"] == 0:
        gaps.append({"seg": "⑤ Rule OOS", "kind": "RULES_EMPTY",
                     "detail": "rules.json 0 条 → 无 OOS 成绩单（机制在，内容空）"})
    up = underperform_seg(sim, week_end, week_start, nv, thr, benchmark)
    if up["status"] != "OK":
        gaps.append({"seg": "⑥ 净值跑输归因", "kind": "BENCHMARK_UNAVAILABLE",
                     "detail": "基准缺失 → 连周跑输不可算"})
    pd_ = pending_seg(week_end)

    # 市场估值/预期截面（2026-10-14 接线）：读 S6/S7 缓存件（周报零网络）
    # 口径：**当前截面 ≠ 历史变化 ≠ 预期差**——截面标注 as_of/来源/缺失；
    # ≥120 日序列与历史预期序列均不可用（S6/S7 契约保留）。
    import os as _os
    import io as _io
    gen = _os.path.join(base or ROOT, "data", "generated")
    sec = {"status": "ABSENT", "as_of": None}
    for sid, key in (("s_s6_section", "s6_valuation"), ("s_s7_consensus", "s7_consensus")):
        pth = _os.path.join(gen, "%s_%s.json" % (sid, week_end))
        if not _os.path.isfile(pth):
            sec[key] = {"status": "ABSENT"}
            continue
        with _io.open(pth, encoding="utf-8") as f:
            c = json.load(f)
        sec[key] = {"status": "PRESENT", "as_of": c.get("as_of"),
                    "fetched_at": c.get("fetched_at"), "source": c.get("source"),
                    "semantics": c.get("semantics") or c.get("note"),
                    "n_codes": c.get("n_codes"),
                    "n_covered": c.get("n_covered")}
        if c.get("semantics") or c.get("note"):
            pass
    if any(sec.get(k, {}).get("status") == "PRESENT"
           for k in ("s6_valuation", "s7_consensus")):
        sec["status"] = "PRESENT_SEGMENT"
        sec["caveats"] = ["当前截面（非历史变化）", "历史预期序列不可用（非预期差）"]

    # 持仓/组合诊断引用（2026-10-14 接线）：周报**不重跑** E5/E9，只引用
    # week_end 当日已产出的诊断报告（存在性 + 内容哈希摘要）；缺席/失败如实标注。
    diag = {}
    for key, fn in (("e5_position", "e5_%s.md" % week_end),
                    ("e9_portfolio", "e9_%s.md" % week_end)):
        fp = _os.path.join(base or ROOT, "data", "generated", "reports", fn)
        if _os.path.isfile(fp):
            body = _io.open(fp, encoding="utf-8").read()
            diag[key] = {"status": "PRESENT", "path": "data/generated/reports/%s" % fn,
                         "bytes": len(body),
                         "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]}
        else:
            diag[key] = {"status": "ABSENT",
                         "reason": "%s 未产出（盘后运行 e5/e9 --eval %s 生成本日诊断）"
                                   % (fn, week_end)}
    sec["e5_e9_diag"] = diag

    mode = "FULL"
    if jd["n_settled_week"] == 0 and not decs:
        mode = "NO_ACTIVITY"          # 不输出条件①：本周零交易日/零记录
    elif not decs:
        mode = "COMPACT"              # 不输出条件③：全周零决策 → 精简版（1/6/7）

    rep = {
        "schema": SCHEMA_VERSION, "cap": CAP, "kind": "E6_WEEKLY",
        "axis": PNL.axis_name(sim), "week_end": week_end, "week_start": week_start,
        "prev_week_end": prev_end, "mode": mode,
        "params_version": PARAMS.params_version(path=params_path),
        "cursor": (CUR.read_cursor() or {}).get("date") if base is None else None,
        "initial_capital": cap0,
        "decisions_week": len(decs), "env_regimes_week": envs,
        "net_value": nv, "judgement": jd, "repeat_errors": repeat_error_seg(jd),
        "case_recall": cr, "rule_oos": ro, "underperform": up,
        "pending_candidates": pd_, "gaps": gaps,
        "market_section": sec,
    }
    rep["report_md"] = render_md(rep)
    return rep


# ── 渲染（纯函数）────────────────────────────────────────────────────────────
def render_md(rep):
    """纯函数渲染（同 report 逐字节同输出）。"""
    L = []
    L.append("# E6 周度报告 · 周截至 %s（%s 轴）" % (rep["week_end"], rep["axis"]))
    L.append("")
    if rep["mode"] == "NO_ACTIVITY":
        L.append("- **本周无交易日 / 账本零记录**（周区间 %s ~ %s）"
                 % (rep["week_start"], rep["week_end"]))
        L.append("- params_version: %s ｜ 游标: %s"
                 % (rep["params_version"], rep.get("cursor") or "UNKNOWN"))
        L.append("")
        L.append("> 不输出条件①（设计 3.1）：零交易日 → 一行结论。")
        return "\n".join(L)
    L.append("- 周区间 %s ~ %s ｜ params_version: %s ｜ 游标: %s ｜ 模式: %s"
             % (rep["week_start"], rep["week_end"], rep["params_version"],
                rep.get("cursor") or "UNKNOWN", rep["mode"]))
    L.append("- 本周决策 %d 笔 ｜ 本周结算 %d 笔 ｜ 环境档: %s"
             % (rep["decisions_week"], rep["judgement"]["n_settled_week"],
                rep["env_regimes_week"] if rep["env_regimes_week"] else "UNKNOWN（本周无决策或 env 无源）"))
    L.append("")
    # ①
    nv = rep["net_value"]
    L.append("## ① 净值 vs 基准")
    L.append("- 曲线点 %d ｜ 期末权益 %s ｜ 累计净盈亏(费后) %s ｜ 毛 %s ｜ 费 %s"
             % (nv["n_points"], nv["equity_end"], nv["total_pnl"],
                nv["total_pnl_gross"], nv["total_fee"]))
    L.append("- 本周收益 %s ｜ 累计收益 %s ｜ 全区间最大回撤(净) %s%% ｜ 本周最大回撤 %s%%"
             % (_pct(nv["week_ret"]), _pct(nv["cum_ret"]),
                nv["max_drawdown_pct"], nv["week_max_drawdown_pct"]))
    bm = nv["benchmark"]
    if bm["status"] == "OK":
        L.append("- 基准（本周）%s ｜ 超额 %s" % (_pct(bm["week_ret"]), _pct(nv["excess_ret"])))
    else:
        L.append("- 基准：**UNAVAILABLE**（%s）" % bm["detail"])
    L.append("")
    # ②
    jd = rep["judgement"]
    L.append("## ② 判断质量分布（SUPPORT/MIXED/REFUTE；**槽位计数** = 笔 × M1/M2/M3）")
    c, w, t = jd["cum_verdict_dist"], jd["week_verdict_dist"], jd["trend"]
    L.append("- 累计: S/M/R = %d/%d/%d（结算 %d 笔）"
             % (c.get("SUPPORT", 0), c.get("MIXED", 0), c.get("REFUTE", 0),
                jd["n_settled_cum"]))
    L.append("- 本周: S/M/R = %d/%d/%d（结算 %d 笔）｜ 上周结算 %d 笔"
             % (w.get("SUPPORT", 0), w.get("MIXED", 0), w.get("REFUTE", 0),
                jd["n_settled_week"], jd["n_settled_prev_week"]))
    L.append("- 周环比（本周 − 上周）: %s"
             % " · ".join("%s %+d" % (k, t[k]) for k in sorted(t)))
    L.append("- L1–L5 归因 WRONG（累计）: %s"
             % " · ".join("%s=%d" % (k, jd["cum_l1l5_wrong"][k]) for k in PNL.L1L5_KEYS))
    L.append("")
    # ③
    re_ = rep["repeat_errors"]
    L.append("## ③ 重复错误")
    L.append("- 本周 L1–L5 WRONG: %s"
             % " · ".join("%s=%d" % (k, re_["week_by_L"][k]) for k in PNL.L1L5_KEYS))
    L.append("- 周对比（本周 − 上周）: %s"
             % " · ".join("%s %+d" % (k, re_["delta_by_L"][k]) for k in PNL.L1L5_KEYS))
    L.append("- error_type 聚类: **UNAVAILABLE**（%s）" % re_["error_type_cluster"]["detail"])
    L.append("")
    # ④
    cr = rep["case_recall"]
    L.append("## ④ Case 召回（分阶段）")
    L.append("- 阶段 **%s** ｜ Case 样本 %d 条 ｜ 门槛 N: %s"
             % (cr["stage"], cr["n_cases"], cr["min_samples"] if cr["min_samples"] is not None else "未定"))
    L.append("- 阶段说明: %s" % cr["stage_reason"])
    L.append("- 有效率: **UNAVAILABLE**（%s）" % cr["efficiency"]["detail"])
    L.append("")
    # ⑤
    ro = rep["rule_oos"]
    L.append("## ⑤ Rule OOS 成绩单")
    L.append("- 规则 %d 条 ｜ 状态计数: %s"
             % (ro["n_rules"], ro["rule_status_counts"] if ro["rule_status_counts"] else "（空）"))
    L.append("- OOS 结果 %d 条 ｜ 判定分布: %s"
             % (ro["n_oos_results"], ro["oos_verdict_dist"] if ro["oos_verdict_dist"] else "（空）"))
    L.append("")
    # ⑥
    up = rep["underperform"]
    L.append("## ⑥ 净值跑输归因（展示 C6 已触发结果）")
    L.append("- 连周跑输: %s ｜ 阈值 %s 周" % (up["status"], up["threshold_weeks"]))
    L.append("- C6 已触发归因任务 %d 条" % up["n_forced_tasks"])
    for t_ in up["forced_tasks"][-5:]:
        L.append("  - %s · %s" % (t_.get("as_of"), (t_.get("gaps") or [{}])[0].get("detail", "")[:70]))
    L.append("- 说明: %s" % up["detail"])
    L.append("")
    # ⑦
    pc = rep["pending_candidates"]
    L.append("## ⑦ 待批准候选")
    L.append("- 参数候选 %d 条 ｜ 需求任务 %d 条（%s）"
             % (pc["n_calibration"], pc["n_demand"], pc["status"]))
    for c_ in pc["calibration_queue"][-5:]:
        L.append("  - %s %s → %s（%s）"
                 % (c_.get("as_of"), c_.get("param"), c_.get("candidate_value"),
                    c_.get("status") or "待裁定"))
    L.append("- 入口: %s" % pc["entry"])
    L.append("")
    L.append("## ⑧ 市场估值/预期截面（S6/S7 · 当前截面，非历史变化）")
    ms = rep.get("market_section") or {}
    if ms.get("status") == "PRESENT_SEGMENT":
        for key, label in (("s6_valuation", "S6 估值截面"), ("s7_consensus", "S7 一致预期截面")):
            v = ms.get(key) or {}
            if v.get("status") == "PRESENT":
                L.append("- %s：as_of=%s 取数=%s 覆盖 %s/%s 码 ｜ 源=%s"
                         % (label, v.get("as_of"), v.get("fetched_at"),
                            v.get("n_covered"), v.get("n_codes"), v.get("source")))
            else:
                L.append("- %s：ABSENT（未生成截面缓存）" % label)
        for cv in ms.get("caveats") or []:
            L.append("  - 口径：%s" % cv)
    else:
        L.append("- ABSENT（S6/S7 截面缓存均未生成——运行 s6.py --fetch-section / "
                 "s7.py --fetch-consensus 生成；≥120 日序列与历史预期序列仍不可用）")
    L.append("")
    L.append("## ⑨ 持仓/组合诊断引用（E5/E9 · 周报不重跑，只引用）")
    dg = (rep.get("market_section") or {}).get("e5_e9_diag") or {}
    for key, label in (("e5_position", "E5 持仓诊断"), ("e9_portfolio", "E9 组合诊断")):
        v = dg.get(key) or {}
        if v.get("status") == "PRESENT":
            L.append("- %s：%s（sha256=%s…）" % (label, v["path"], v["sha256"]))
        else:
            L.append("- %s：%s" % (label, v.get("reason") or v.get("status")))
    L.append("")
    L.append("## 缺口（显式化，不造数）")
    for g in rep["gaps"]:
        L.append("- **%s** · %s —— %s" % (g["seg"], g["kind"], g["detail"]))
    L.append("")
    L.append("> 本报告由账本与 C6 产物机械生成，同输入逐字节同输出；E6 零交易动作、"
             "零改参、零自动归因触发。")
    return "\n".join(L)


def _pct(v):
    return "N/A" if v is None else ("%+.2f%%" % (v * 100))


# ── 落盘（append-only + 幂等）────────────────────────────────────────────────
def _read_rows(base=None):
    p = os.path.join(base, "reports", "e6_weekly.jsonl") if base else REPORT
    if not os.path.isfile(p):
        return []
    out = []
    with io.open(p, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


def _content_hash(rep):
    """内容指纹（剔除 generated_at——重跑时刻不同不算内容变化）。"""
    body = {k: v for k, v in rep.items() if k != "generated_at"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8")).hexdigest()


def append_report(rep, base=None):
    """append-only 幂等落盘：同 (axis, week_end) 同内容 → EXISTS；异内容 → 拒。"""
    rep = dict(rep)
    rep["generated_at"] = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    h = _content_hash(rep)
    for row in _read_rows(base):
        if row.get("axis") == rep["axis"] and row.get("week_end") == rep["week_end"]:
            if row.get("content_hash") == h:
                return {"created": False, "reason": "EXISTS", "path": _path_of(base)}
            raise E6Error("周报 %s/%s 已存在且内容不同 —— append-only 禁改写"
                          % (rep["axis"], rep["week_end"]))
    row = dict(rep)
    row["content_hash"] = h
    p = _path_of(base)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    # ★ 写点 sink 内联 `p or REPORT` BoolOp（审计静态解析要求直达；C6/calibrate 同范式）
    _WG.check_write(p or REPORT)
    with io.open(p or REPORT, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {"created": True, "path": p, "content_hash": h}


def _path_of(base=None):
    return os.path.join(base, "reports", "e6_weekly.jsonl") if base else REPORT


# ── CLI ──────────────────────────────────────────────────────────────────────
def _main(argv):
    if "--selftest" in argv:
        return 0 if _selftest() else 1
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    ap = argparse.ArgumentParser()
    ap.add_argument("--week-end", dest="week_end", required=True)
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--initial-capital", dest="cap", type=float, default=None)
    ap.add_argument("--benchmark-file", dest="bm", default=None,
                    help='JSON: {"2026-09-30": 0.0123}')
    ap.add_argument("--base", dest="base", default=None,
                    help="账本根目录（默认真库 data/generated；沙盒验真时指向 <sandbox>/gen）")
    ap.add_argument("--stdout", action="store_true")
    a = ap.parse_args(argv)
    bm = _load_json(a.bm, None) if a.bm else None
    rep = build_report(a.week_end, sim=a.sim, initial_capital=a.cap, benchmark=bm,
                       base=a.base)
    # ★ --base 语义一致：读账本与落报告同根（沙盒验真不得污染真库）
    r = append_report(rep, base=a.base)
    print("E6 周报 %s/%s → %s（%s）"
          % (rep["axis"], rep["week_end"], r["path"],
             "新建" if r["created"] else "已存在（幂等 EXISTS）"))
    if a.stdout:
        print(rep["report_md"])
    return 0


# ── 纪律判据（静态扫描本文件）────────────────────────────────────────────────
def _discipline(verbose=True):
    import ast
    import re

    ok = [0]
    fail = [0]

    def T(name, cond):
        if cond:
            ok[0] += 1
        else:
            fail[0] += 1
        if verbose:
            print("  [%s] %s" % ("✓" if cond else "!!", name))
        return bool(cond)

    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    lines = src.splitlines(True)
    tree = ast.parse(src)
    drop = set()
    for nd in ast.walk(tree):
        if isinstance(nd, ast.FunctionDef) and nd.name in ("_selftest", "_discipline"):
            drop.update(range(nd.lineno - 1, nd.end_lineno))
    prod = "".join(l for i, l in enumerate(lines) if i not in drop)

    # ① 零越权词（生产区，词边界）
    forbidden = ("or" + "der", "bu" + "y", "se" + "ll", "wa" + "it",
                 "po" + "sition_gate")
    T("① 零越权词（%s）" % "·".join(forbidden),
      not [w for w in forbidden if re.search(r"\b%s\b" % w, prod)])
    T("①b 零动作建议词", not [w for w in _INTENT_WORDS if w in prod])

    # ② 唯一写点（生产区 io.open 写模式仅 1 处 = append；经 _path_of 单出口）
    open_calls = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "open"):
            continue
        if (n.lineno - 1) in drop:
            continue
        mode = "r"
        if len(n.args) >= 2 and isinstance(n.args[1], ast.Constant):
            mode = n.args[1].value
        open_calls.append(mode)
    write_modes = [m for m in open_calls if m != "r"]
    T("② 生产区 io.open 写点仅 1 处（append）",
      write_modes == ["a"] and "_path_of(" in prod)
    T("②b 零覆写模式（无 w / w+ / r+ / a+）",
      not [m for m in write_modes if m in ("w", "w+", "r+", "a+")])

    # ③ 阈值经 PARAMS.get_value（禁硬编码门槛）
    T("③ 阈值经 PARAMS.get_value",
      "PARAMS.get_value(" in prod and "weekly_underperform_weeks" in prod)

    # ④ 周界经 _shift（禁手工日期算术）
    T("④ 周界经 _shift 派生", "_shift(" in prod)

    # ⑤ 缺数据如实（UNAVAILABLE + gaps）
    T("⑤ 缺数据显式 UNAVAILABLE 且入 gaps",
      "UNAVAILABLE" in prod and '"gaps"' in prod)

    # ⑥ 零 C6 模块 import（只读产物文件）+ 零写 API
    T("⑥ 零 C6 模块 import（只读产物文件）",
      all(("import " + m) not in prod for m in ("calibrate", "case_builder", "verify")))
    T("⑥b 零 params/队列写入 API",
      "PARAMS.set" not in prod and "propose_param" not in prod
      and "_append_demand" not in prod)

    # ⑦ append-only 语义（幂等 EXISTS + 异内容拒）
    T("⑦ 幂等 EXISTS 跳过", "EXISTS" in prod)
    T("⑦b 异内容拒（append-only 禁改写）", "append-only 禁改写" in prod)
    T("⑦c 内容指纹剔除 generated_at",
      'k != "generated_at"' in prod or "generated_at" in prod)

    # ⑧ md 纯函数 + ⑨ cap 标记
    T("⑧ md 渲染为纯函数", "def render_md(rep)" in prod)
    T("⑨ cap 标记 = E6", 'CAP = "E6"' in prod)

    if verbose:
        print("  纪律判据：%d/%d 通过" % (ok[0], ok[0] + fail[0]))
    return fail[0] == 0


# ── 自检（沙盒账本，真库零触碰）──────────────────────────────────────────────
def _selftest(verbose=True):
    import shutil
    import tempfile

    def T(name, cond):
        if verbose:
            print("  [%s] %s" % ("✓" if cond else "!!", name))
        return bool(cond)

    ok = fail = 0

    def chk(cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1

    tmp = tempfile.mkdtemp(prefix="e6_selftest_")
    real_fp = _fp(REPORT)
    real_calq = _fp(CALQ)
    real_demand = _fp(DEMAND)
    try:
        base = os.path.join(tmp, "gen")
        reports = os.path.join(base, "reports")
        os.makedirs(reports, exist_ok=True)
        # 夹具：两周 pnl（本周 3 笔 / 上周 2 笔），含 SUPPORT/MIXED/REFUTE + L1 WRONG
        import thesis as TH
        th_p = os.path.join(base, "thesis_store.jsonl")
        TH.init_file(th_p)

        def pred(s):
            return {"prediction_id": s, "metric": "m_%s" % s, "data_source": "snapshot",
                    "baseline": "T 日收盘", "direction": "UP", "threshold": 3.0,
                    "condition": "T+3 累计涨幅 ≥3%", "window": "3D"}

        FIVE = {"驱动存在": "x", "当前证据": "y", "延续机制": "z",
                "失效条件": "w", "剩余预期": "v"}
        chain = {"evidence": ["e"], "inference": ["i"],
                 "prediction": [pred(s) for s in PNL.SLOTS], "five": dict(FIVE)}

        def mk(day, idx, verdict, wrong_L=None):
            code = "sh60000%d" % idx
            t = TH.create_thesis(day, "A", "夹具", code=code, path=th_p)["id"]
            d = DEC.write_decision(day, code, "BUY", t, chain, sim=True,
                                   base=base, thesis_path=th_p, driver="A",
                                   entry_limit=10.0, stop_line=9.5, target=11.0,
                                   window=3, position_request=0.2,
                                   env={"regime": "强势", "signals": {},
                                        "leg": "INTRADAY", "source": "EXPLICIT"})
            DEC.append_risk_plan(d["id"], 0.2, as_of=day,
                                 gates={"circuit": {"binding": False}}, base=base)
            DEC.append_execution(d["id"], 0.2, "filled", avg_price=10.0,
                                 as_of=day, base=base)
            vd = {s: {"prediction_id": s, "metric": "m_" + s, "data_source": "snapshot",
                      "verdict": verdict, "observed": None, "note": None}
                  for s in PNL.SLOTS}
            l1 = {k: ("WRONG" if k == wrong_L else "OK") for k in PNL.L1L5_KEYS}
            PNL.write_pnl(d["id"], "WIN", verdicts=vd, attribution_L1L5=l1,
                          attribution_三问={"驱动": "OK", "赔率": "OK", "证伪": "OK"},
                          entry_price=10.0, exit_price=10.2, exit_kind="TARGET",
                          exit_date=day, capital=100000.0, hold_days=1,
                          settle_date=day, base=base)
            return d

        mk("2026-09-23", 1, "SUPPORT")
        mk("2026-09-24", 2, "MIXED")
        mk("2026-09-28", 3, "REFUTE", wrong_L="L1")
        mk("2026-09-29", 4, "SUPPORT", wrong_L="L1")
        mk("2026-09-30", 5, "MIXED")

        rep = build_report("2026-09-30", sim=True, initial_capital=1000000.0,
                           base=base)
        # A 段：装载与分段
        chk(T("A1 轴/周界正确", rep["axis"] == "SIM" and rep["week_start"] == "2026-09-24"
              and rep["week_end"] == "2026-09-30" and rep["prev_week_end"] == "2026-09-23"))
        chk(T("A2 本周结算 4 笔（09-24/28/29/30）/ 上周 1 笔（09-23）",
              rep["judgement"]["n_settled_week"] == 4
              and rep["judgement"]["n_settled_prev_week"] == 1))
        chk(T("A3 本周判定分布（槽位级 = 笔 × M1/M2/M3）S/M/R = 3/6/3",
              rep["judgement"]["week_verdict_dist"]["SUPPORT"] == 3
              and rep["judgement"]["week_verdict_dist"]["MIXED"] == 6
              and rep["judgement"]["week_verdict_dist"]["REFUTE"] == 3))
        chk(T("A4 累计 5 笔", rep["judgement"]["n_settled_cum"] == 5))
        chk(T("A5 周环比（本周3/6/3 − 上周3/0/0）= S 0 · M +6 · R +3",
              rep["judgement"]["trend"]["SUPPORT"] == 0
              and rep["judgement"]["trend"]["MIXED"] == 6
              and rep["judgement"]["trend"]["REFUTE"] == 3))
        chk(T("A6 净值曲线点 = 5", rep["net_value"]["n_points"] == 5))
        chk(T("A7 环境档取自决策 env", rep["env_regimes_week"] == ["强势"]))
        # B 段：缺口与阶段
        chk(T("B1 基准缺 → BENCHMARK_UNAVAILABLE 入缺口",
              rep["net_value"]["benchmark"]["status"] == "UNAVAILABLE"
              and any(g["kind"] == "BENCHMARK_UNAVAILABLE" for g in rep["gaps"])))
        chk(T("B2 Case 召回停阶段 A（N 未定）",
              rep["case_recall"]["stage"] == "A"
              and rep["case_recall"]["min_samples"] is None))
        chk(T("B3 有效率不计算（防误归功）",
              rep["case_recall"]["efficiency"]["status"] == "UNAVAILABLE"))
        chk(T("B4 rule 空 → RULES_EMPTY 缺口",
              any(g["kind"] == "RULES_EMPTY" for g in rep["gaps"])))
        chk(T("B5 error_type 聚类如实不可产",
              rep["repeat_errors"]["error_type_cluster"]["status"] == "UNAVAILABLE"))
        chk(T("B6 L1 WRONG 本周 2 / 累计 2",
              rep["repeat_errors"]["week_by_L"]["L1"] == 2
              and rep["repeat_errors"]["cum_by_L"]["L1"] == 2))
        chk(T("B7 跑输归因 = 展示位（不自行触发）",
              rep["underperform"]["status"] == "UNAVAILABLE"
              and "不自行触发" in rep["underperform"]["detail"]))
        chk(T("B8 待批准候选空态如实", rep["pending_candidates"]["status"] == "EMPTY"))
        # C 段：md 渲染
        md = rep["report_md"]
        chk(T("C1 md 7 段齐", all(("## " + s) in md for s in
              ("① 净值 vs 基准", "② 判断质量分布", "③ 重复错误", "④ Case 召回",
               "⑤ Rule OOS 成绩单", "⑥ 净值跑输归因", "⑦ 待批准候选"))))
        chk(T("C2 md 含缺口段", "## 缺口（显式化，不造数）" in md))
        chk(T("C3 md 逐字节可复现", render_md(rep) == md))
        chk(T("C4 md 零动作建议词",
              not any(w in md for w in _INTENT_WORDS)))
        # D 段：幂等 append-only
        r1 = append_report(rep, base=base)
        chk(T("D1 首次落盘 created", r1["created"]))
        r2 = append_report(rep, base=base)
        chk(T("D2 同内容重跑 → EXISTS", not r2["created"] and r2["reason"] == "EXISTS"))
        rep_bad = dict(rep)
        rep_bad["mode"] = "COMPACT"
        try:
            append_report(rep_bad, base=base)
            chk(T("D3 异内容 → 拒", False))
        except E6Error:
            chk(T("D3 异内容 → 拒（append-only 禁改写）", True))
        chk(T("D4 沙盒文件已落 1 行", len(_read_rows(base)) == 1))
        # E 段：不输出条件
        rep_none = build_report("2026-08-31", sim=True, initial_capital=1000000.0,
                                base=base)
        chk(T("E1 零活动周 → NO_ACTIVITY", rep_none["mode"] == "NO_ACTIVITY"))
        chk(T("E2 精简 md 一行结论", "本周无交易日" in rep_none["report_md"]
              and "## ①" not in rep_none["report_md"]))
        # F 段：真库零污染
        chk(T("F1 沙盒运行不写真库快照/报告", _fp(REPORT) == real_fp))
        chk(T("F2 沙盒运行不写 calibration_queue / demand_queue",
              _fp(CALQ) == real_calq and _fp(DEMAND) == real_demand))
        chk(T("F3 --base 语义一致：_path_of(base) 落沙盒 / _path_of(None) 落真库",
              _path_of(base).startswith(base) and _path_of(None) == REPORT))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if verbose:
            print("  自检：%d/%d 通过" % (ok, ok + fail))
    return fail == 0


def _fp(p):
    return os.path.getsize(p) if os.path.isfile(p) else None


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
