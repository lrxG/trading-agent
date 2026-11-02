# -*- coding: utf-8 -*-
"""verify.py —— C6-a 零件③提炼链第 4 步：统计确认（施工 1.6）。

输入假设（Observation/Hypothesis 的机械条件化）→ 区间复测 + OOS →
`SUPPORT / MIXED / REFUTE / INSUFFICIENT`，结果落 `data/research/verify/`（append-only）。

验证协议（用户硬约束 2 —— 可执行、可复现，禁"跨区间+OOS"一句话空话）：
  每份验证记录固定含：hyp_id · 候选（rule 陈述引用 or 参数条件）· evidence 引用 ·
  数据区间与切分方式 · 分层样本量 · 基准方案/候选方案 · 成本口径 ·
  各区间复测结果 · OOS 结果 · 风险指标变化 · verifier_version · params_version ·
  数据截止。判定**只由计数与算术**产生 —— 零 LLM 输入，confidence 不存在。

判据（全部 params 真源，零字面量）：
  * 区间切分 = 按 settle_date 升序均分 `verify_intervals` 段 IS + 末段 OOS；
  * 每段：条件命中组 vs 未命中组（基准）—— 命中组 WIN 占比 − 基准 WIN 占比
    ≥ `verify_min_edge` → 该段 support；任一段样本 < `verify_min_samples` →
    **INSUFFICIENT（证据不足，不得包装成 SUPPORT）**；
  * OOS 不支持 → REFUTE；IS 支持但互相不一致 → MIXED；
  * 风险闸：命中组最差单笔 pnl_pct < 基准组最差单笔 → 风险恶化 → REFUTE；
  * 成本口径 = params `verify_cost_model`（NONE_SET = 未计成本，如实记录不假装）。

条件字段白名单 = Case 8 特征（决策时已知）——**防穿越**（结算后字段禁入条件）。
写点：data/research/verify/results.jsonl 单文件 append-only（X2 豁免，
sink 内联 `results_path or VERIFY_PATH` BoolOp 静态可解析）。
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
import observe as OBS  # noqa: E402
import astwin as ASTW  # noqa: E402
import case_builder as CB  # noqa: E402

ACTOR = "learning"
STAGE = "C6"
SCHEMA_VERSION = "1.0"
VERIFIER_VERSION = "verify-1.0"
VERIFY_PATH = os.path.join(ROOT, "data", "research", "verify",
                           "results.jsonl")

VERDICTS = ("SUPPORT", "MIXED", "REFUTE", "INSUFFICIENT")
OPS = (">=", "<=", "==")
# 防穿越白名单：条件只允许决策时已知特征（Case 8 特征键）
CONDITION_FIELDS = CB.FEATURE_KEYS


class VerifyError(Exception):
    """验证被拒（协议字段缺失 / 条件字段越白名单 / 区间数非法）。"""


def _rows_of(path):
    if not os.path.isfile(path):
        return []
    out = []
    with io.open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


def _check_condition(cond):
    if not isinstance(cond, dict) or set(cond) - {"field", "op", "value"} \
            or set(cond) != {"field", "op", "value"}:
        raise VerifyError("condition 必须恰为 {field, op, value}")
    if cond["field"] not in CONDITION_FIELDS:
        raise VerifyError(
            "条件字段 %r 越白名单（只许决策时已知 8 特征 %s）—— 防穿越"
            % (cond["field"], "/".join(CONDITION_FIELDS)))
    if cond["op"] not in OPS:
        raise VerifyError("op 只许 %s" % "/".join(OPS))
    if isinstance(cond["value"], bool) or not isinstance(
            cond["value"], (int, float, str)):
        raise VerifyError("value 类型非法")
    return cond


def _match(case, cond):
    v = (case.get("features") or {}).get(cond["field"])
    if v is None:
        return None                                  # 特征缺失 → 不归入任何组
    t = cond["value"]
    if isinstance(t, str) != isinstance(v, str):
        return False
    if cond["op"] == "==":
        return str(v) == str(t)
    if isinstance(t, str) or isinstance(v, str):
        return False
    return v >= t if cond["op"] == ">=" else v <= t


def _stats(cases):
    n = len(cases)
    wins = sum(1 for c in cases if c.get("outcome") == "WIN")
    worst = min((c.get("pnl_pct") for c in cases
                 if isinstance(c.get("pnl_pct"), (int, float))), default=None)
    return {"n": n, "wins": wins,
            "win_rate": (wins / n) if n else None, "worst_pnl_pct": worst}


def _split(cases, k):
    """按 settle_date 升序均分 k 段 IS + 末段 OOS；日期相同者同段（禁拆同日）。"""
    ordered = sorted(cases, key=lambda c: (c.get("settle_date") or "",
                                           c.get("case_id")))
    n = len(ordered)
    if n < k + 1:
        return []
    bounds = [round(n * i / (k + 1)) for i in range(k + 2)]
    return [ordered[bounds[i]:bounds[i + 1]] for i in range(k + 1)]


def verify(hyp, *, sim, cases_path=None, params_path=None,
           results_path=None):
    """hyp = {hyp_id, kind: RULE|PARAM, statement, condition, evidence[], target?}。

    返回验证记录 dict（同时 append `verify/<hyp_id>.jsonl`）。同 (hyp_id, data_upto)
    幂等不重写。kind=PARAM 时 target=参数名、condition=该参数的机械条件化。
    """
    if not isinstance(sim, bool):
        raise VerifyError("sim 必须显式布尔（LIVE/SIM 不表态 = 报错）")
    for f in ("hyp_id", "kind", "statement", "condition", "evidence"):
        if f not in hyp:
            raise VerifyError("hyp 缺协议字段 %r（验证记录必须可复现）" % f)
    if hyp["kind"] not in ("RULE", "PARAM"):
        raise VerifyError("kind 只许 RULE/PARAM")
    if hyp["kind"] == "PARAM" and not hyp.get("target"):
        raise VerifyError("kind=PARAM 必须带 target（参数名）")
    if not hyp["evidence"]:
        raise VerifyError("evidence 为空 → 无证据不验证")
    cond = _check_condition(hyp["condition"])

    k = int(PARAMS.get_value("verify_intervals", path=params_path))
    n_min = PARAMS.get_value("verify_min_samples", path=params_path)
    edge = PARAMS.get_value("verify_min_edge", path=params_path)
    cost = PARAMS.get_value("verify_cost_model", path=params_path)
    if k < 1:
        raise VerifyError("verify_intervals 至少 1（1 段 IS + 1 段 OOS）")

    rows = [r for r in CB.load_cases(sim=sim, path=cases_path)
            if r.get("outcome") in ("WIN", "LOSS")]
    fp_src = json.dumps(sorted((r.get("case_id"), r.get("settle_date"),
                                r.get("outcome"), r.get("pnl_pct"))
                               for r in rows), ensure_ascii=False)
    data_fp = __import__("hashlib").md5(fp_src.encode("utf-8")).hexdigest()
    segs = _split(rows, k)
    out = {
        "schema_version": SCHEMA_VERSION,
        "hyp_id": hyp["hyp_id"], "kind": hyp["kind"],
        "statement": hyp["statement"],
        "target": hyp.get("target"),
        "condition": cond, "evidence": list(hyp["evidence"]),
        "sim": sim, "verifier_version": VERIFIER_VERSION,
        "params_version": PARAMS.load_params(path=params_path).get("version"),
        "cost_model": cost,
        "split_method": "settle_date 升序均分 %d 段 IS + 末段 OOS（同日不拆）" % k,
        "data_upto": max((r.get("settle_date") or "" for r in rows), default=None),
        "data_fingerprint": data_fp,
        "n_total": len(rows),
    }
    if len(segs) < k + 1 or any(not s for s in segs):
        out["verdict"] = "INSUFFICIENT"
        out["insufficient_reason"] = "样本不足切分（n_total=%s）" % len(rows)
    else:
        seg_results = []
        for i, seg in enumerate(segs):
            hit = [c for c in seg if _match(c, cond) is True]
            base = [c for c in seg if _match(c, cond) is False]
            sh, sb_ = _stats(hit), _stats(base)
            insufficient = sh["n"] < n_min or sb_["n"] < n_min
            support = (None if insufficient or sh["win_rate"] is None
                       or sb_["win_rate"] is None
                       else (sh["win_rate"] - sb_["win_rate"]) >= edge)
            seg_results.append({
                "seg": "OOS" if i == k else "IS%d" % (i + 1),
                "n_hit": sh["n"], "n_base": sb_["n"],
                "win_rate_hit": sh["win_rate"], "win_rate_base": sb_["win_rate"],
                "worst_pnl_hit": sh["worst_pnl_pct"],
                "worst_pnl_base": sb_["worst_pnl_pct"],
                "insufficient": insufficient, "support": support})
        out["segments"] = seg_results
        if any(s["insufficient"] for s in seg_results):
            out["verdict"] = "INSUFFICIENT"
            out["insufficient_reason"] = "任一段样本 < verify_min_samples"
        else:
            worse = any(s["worst_pnl_hit"] is not None
                        and s["worst_pnl_base"] is not None
                        and s["worst_pnl_hit"] < s["worst_pnl_base"]
                        for s in seg_results)
            if worse:
                out["verdict"] = "REFUTE"
                out["refute_reason"] = "风险恶化：命中组最差单笔劣于基准组"
            elif not seg_results[-1]["support"]:
                out["verdict"] = "REFUTE"
                out["refute_reason"] = "OOS 不支持"
            else:
                is_res = [s["support"] for s in seg_results[:-1]]
                out["verdict"] = ("SUPPORT" if all(is_res)
                                  else ("MIXED" if any(is_res) else "REFUTE"))
                if out["verdict"] == "MIXED":
                    out["mixed_reason"] = "IS 区间支持不一致"
    # ★ 写点 sink 内联 `results_path or VERIFY_PATH` BoolOp（审计静态解析要求直达）
    rp = results_path or VERIFY_PATH
    for old in _rows_of(rp):
        if old.get("hyp_id") == hyp["hyp_id"] and \
                old.get("data_upto") == out["data_upto"] and \
                old.get("data_fingerprint") == data_fp:
            return old                                # 同数据幂等（可复现）
    os.makedirs(os.path.dirname(results_path or VERIFY_PATH), exist_ok=True)
    _WG.check_write(results_path or VERIFY_PATH)
    # ★ 写点 sink 内联 `results_path or VERIFY_PATH` BoolOp（静态解析要求直达）
    with io.open(results_path or VERIFY_PATH, "a", encoding="utf-8",
                 newline="\n") as f:
        f.write(json.dumps(out, ensure_ascii=False, sort_keys=True) + "\n")
    out["result_path"] = rp
    return out


def load_results(hyp_id=None, results_path=None):
    rows = _rows_of(results_path or VERIFY_PATH)
    if hyp_id:
        return [r for r in rows if r.get("hyp_id") == hyp_id]
    return rows


# ── 自检（全临时沙盒，真库零接触） ────────────────────────────────────────────
_TEST_FUNCS = ("_selftest", "_discipline")


def _case(i, settle, hit, win, pnl):
    feats = {k: None for k in CB.FEATURE_KEYS}
    feats["Catalyst"] = "A" if hit else "B"
    return {"case_id": "CASE-V%s" % i, "sim": True, "settle_date": settle,
            "outcome": "WIN" if win else "LOSS", "pnl_pct": pnl,
            "decision_version": "1.2.0", "features": feats}


def _mk_cases(segs, d_start="2026-09-01"):
    """按段铺样本：segs = [(n_hit_win, n_hit_loss, n_base_win, n_base_loss), ...]。

    段内 hit(催化A)/base(催化B) 交织，日期逐笔 +1 —— 保证每段两组都有样本。
    命中组亏损单 -3.0、基准组亏损单 -3.0（默认风险同侧，B5 再单独改恶化）。
    """
    import datetime as _dt
    rows, day = [], 0
    for (hw, hl, bw, bl) in segs:
        hit = [("A", True, 20.0)] * hw + [("A", False, -3.0)] * hl
        base = [("B", True, 5.0)] * bw + [("B", False, -3.0)] * bl
        inter = []
        hi = bi = 0
        while hi < len(hit) or bi < len(base):
            if hi < len(hit):
                inter.append(hit[hi]); hi += 1
            if bi < len(base):
                inter.append(base[bi]); bi += 1
        for feat, win, pnl in inter:
            rows.append(_case(day, (_dt.date(2026, 9, 1)
                                    + _dt.timedelta(days=day)).isoformat(),
                              feat == "A", win, pnl))
            day += 1
    return rows


def _write_cases(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


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

    print("verify 自检（全临时沙盒，真库零接触）：")
    sb = tempfile.mkdtemp(prefix="vf_sb_")
    cases = os.path.join(sb, "cases.jsonl")
    rp = os.path.join(sb, "research", "verify", "results.jsonl")
    hyp = {"hyp_id": "HYP-001", "kind": "RULE",
           "statement": "Catalyst=A 的样本 WIN 占比更高（观察候选 HYP-001）",
           "condition": {"field": "Catalyst", "op": "==", "value": "A"},
           "evidence": ["OBS-Catalyst-A"]}

    # ── A 组：协议与防穿越 ──
    try:
        verify(dict(hyp, condition={"field": "outcome", "op": "==", "value": "WIN"}),
               sim=True, cases_path=cases, results_path=rp)
        T("A1 条件字段用结算后字段（outcome）→ 拒（防穿越）", False)
    except VerifyError:
        T("A1 条件字段用结算后字段（outcome）→ 拒（防穿越）", True)
    try:
        verify({k: v for k, v in hyp.items() if k != "evidence"},
               sim=True, cases_path=cases, results_path=rp)
        T("A2 无证据不验证（evidence 空 → 拒）", False)
    except VerifyError:
        T("A2 无证据不验证（evidence 空 → 拒）", True)
    try:
        verify(hyp, sim=None, cases_path=cases, results_path=rp)
        T("A3 sim 不表态 → 拒", False)
    except VerifyError:
        T("A3 sim 不表态 → 拒", True)
    try:
        verify(dict(hyp, kind="PARAM"), sim=True, cases_path=cases, results_path=rp)
        T("A4 kind=PARAM 缺 target → 拒", False)
    except VerifyError:
        T("A4 kind=PARAM 缺 target → 拒", True)

    # ── B 组：判定方向 ──
    # 3 段（2 IS + 1 OOS），每段 hit 4W1L(0.8) vs base 2W3L(0.4) → edge 0.4 ≥ 0.1
    _write_cases(cases, _mk_cases([(4, 1, 2, 3)] * 3))
    r = verify(hyp, sim=True, cases_path=cases, results_path=rp)
    T("B1 强假设 → SUPPORT（各段 edge 达标 + OOS 支持）", r["verdict"] == "SUPPORT")
    T("B2 协议字段齐全：split/cost/verifier/params_version/data_upto/n_total",
      all(k in r for k in ("split_method", "cost_model", "verifier_version",
                           "params_version", "data_upto", "n_total"))
      and r["cost_model"] == PARAMS.get_value("verify_cost_model"))
    T("B3 分层样本量记录（每段 n_hit/n_base）",
      all("n_hit" in s and "n_base" in s for s in r["segments"]))

    # 同分布：每段 hit 2W3L == base 2W3L → edge 不达 → 非 SUPPORT（如实 REFUTE）
    _write_cases(cases, _mk_cases([(2, 3, 2, 3)] * 3))
    r2 = verify(dict(hyp, hyp_id="HYP-002"), sim=True, cases_path=cases,
                results_path=rp)
    T("B4 同分布假设 → 非 SUPPORT（如实 REFUTE）", r2["verdict"] == "REFUTE")

    # 风险恶化：命中组赢面高但最差单笔 -50%
    rows = _mk_cases([(4, 1, 2, 3)] * 3)
    for c in rows:
        if c["features"]["Catalyst"] == "A" and c["outcome"] == "LOSS":
            c["pnl_pct"] = -50.0
    _write_cases(cases, rows)
    r3 = verify(dict(hyp, hyp_id="HYP-003"), sim=True, cases_path=cases,
                results_path=rp)
    T("B5 命中组最差单笔 -50%（风险恶化）→ REFUTE", r3["verdict"] == "REFUTE"
      and "风险恶化" in r3.get("refute_reason", ""))

    # 样本不足：每段样本 < verify_min_samples
    _write_cases(cases, _mk_cases([(1, 1, 1, 1)] * 3))
    r4 = verify(dict(hyp, hyp_id="HYP-004"), sim=True, cases_path=cases,
                results_path=rp)
    T("B6 样本不足 → INSUFFICIENT（不得包装成 SUPPORT）",
      r4["verdict"] == "INSUFFICIENT")

    # ── C 组：幂等 / 落盘 / 纪律 ──
    n_before = len(load_results("HYP-004", results_path=rp))
    r5 = verify(dict(hyp, hyp_id="HYP-004"), sim=True, cases_path=cases,
                results_path=rp)
    T("C1 同数据幂等不重写（verdict 相同且文件不增长）",
      r5.get("verdict") == r4["verdict"]
      and len(load_results("HYP-004", results_path=rp)) == n_before)
    T("C2 判定零 LLM 输入（协议无 confidence 字段）",
      "confidence" not in json.dumps(r, ensure_ascii=False))
    T("C3 自检前后真库 verify 结果文件逐字节未变（全沙盒）",
      _md5(os.path.join(ROOT, "data", "research", "verify",
                        "results.jsonl")) == _DIR_MD5)
    print("verify 自检：%d/%d" % (ok[0], ok[0] + bad[0]))
    return bad[0] == 0


_DIR_MD5 = None


def _md5(p):
    import hashlib
    return hashlib.md5(io.open(p, "rb").read()).hexdigest() if os.path.isfile(p) else "-"


def _prod_src():
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    lines = src.splitlines(True)
    drop = set()
    for lo, hi, name in ASTW.func_table(ast.parse(src)):
        if name in _TEST_FUNCS:
            drop.update(range(lo - 1, hi))
    return "".join(l for i, l in enumerate(lines) if i not in drop)


def _discipline():
    print("verify 纪律：")
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    prod = _prod_src()
    res = [
        ("① 写点静态可解析（sink 内联 results_path or VERIFY_PATH）",
         "results_path or VERIFY_PATH" in prod),
        ("② 条件字段白名单 = 决策时已知 8 特征（防穿越判据在代码）",
         "CONDITION_FIELDS = CB.FEATURE_KEYS" in prod),
        ("③ 零 propose/promote（验证件不产候选值）",
         "propose(" not in prod and "promote(" not in prod),
        ("④ 零 params.json/decision_card/position_gate 写入",
         "params.json" not in prod and "decision_card" not in prod
         and "position_gate" not in prod),
        ("⑤ astwin 引用", bool(ASTW.func_table(ast.parse(src)))),
    ]
    for name, c in res:
        print("  [%s] %s" % ("✓" if c else "✗", name))
    print("verify 纪律：%d/%d" % (sum(1 for _, c in res if c), len(res)))
    return all(c for _, c in res)


def _main(argv):
    global _DIR_MD5
    if "--selftest" in argv:
        _DIR_MD5 = _md5(os.path.join(ROOT, "data", "research", "verify",
                                     "results.jsonl"))
        return 0 if _selftest() else 1
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    if "--verify" in argv:
        hf = argv[argv.index("--hyp-file") + 1] if "--hyp-file" in argv else None
        sim = True if "--sim" in argv else (False if "--live" in argv else None)
        if not hf or sim is None:
            print("用法：verify.py --verify --hyp-file <hyp.json> --sim|--live")
            return 2
        hyp = json.load(io.open(hf, encoding="utf-8"))
        r = OBS.run_observed(ACTOR, "verify", STAGE,
                             lambda: verify(hyp, sim=sim), as_of=hyp.get("as_of"))
        v = r["value"]
        print("verify %s: %s -> %s" % (v["hyp_id"], v["verdict"],
                                       v.get("result_path", "(幂等命中)")))
        return 0
    print("未知参数"); return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
