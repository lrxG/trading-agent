# -*- coding: utf-8 -*-
"""param_sweep.py —— 0.16 #5 新建（图纸「保留」纠偏，现场无旧文件）。

阈值扫描器：对 Case 池的某特征/字段，按候选阈值网格逐点做**机械分组计数**
（n / WIN 占比 / 平均 pnl_pct / 最差单笔）→ 事实表落
`data/research/sweep/sweep_<as_of>.jsonl`（X2 豁免，append-only）。

边界（0.16 #5 验收 + 用户硬约束 1）：
  * 扫描结果 = 研究事实，**只落 research 事实表**；params.json 零候选、
    0.9 候选队列零直写 —— 进队唯一路径 = 观察 → verify(SUPPORT)
    → 校准件提案（验证在前，提案在后）；
  * 零排序、零推荐、零"最优值"结论 —— 只输出每格计数，怎么选属验证+批准；
  * 零 LLM、零决策输出。
"""

import os
import sys
import io
import ast
import json
import itertools
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
SWEEP_PATH = os.path.join(ROOT, "data", "research", "sweep", "sweep_facts.jsonl")
OPS = (">=", "<=", "==")


class SweepError(Exception):
    """扫描被拒（字段越白名单 / 网格空）。"""


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


def _match(case, field, op, value):
    v = (case.get("features") or {}).get(field)
    if v is None or isinstance(v, str) != isinstance(value, str):
        return False
    if op == "==":
        return str(v) == str(value)
    if isinstance(value, str) or isinstance(v, str):
        return False
    return v >= value if op == ">=" else v <= value


def _stats(group):
    n = len(group)
    wins = sum(1 for c in group if c.get("outcome") == "WIN")
    pnls = [c.get("pnl_pct") for c in group
            if isinstance(c.get("pnl_pct"), (int, float))]
    return {"n": n, "wins": wins,
            "win_rate": round(wins / n, 4) if n else None,
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 4) if pnls else None,
            "worst_pnl_pct": min(pnls) if pnls else None}


def sweep(as_of, *, sim, field, values, op, cases_path=None, facts_path=None):
    """单字段阈值扫描：每个 (op, value) 一格计数事实。同 (field,op,value,data_upto) 幂等。"""
    if not isinstance(sim, bool):
        raise SweepError("sim 必须显式布尔（LIVE/SIM 不表态 = 报错）")
    if field not in CB.FEATURE_KEYS:
        raise SweepError("字段 %r 越白名单（只许决策时已知 8 特征）—— 防穿越" % field)
    if op not in OPS:
        raise SweepError("op 只许 %s" % "/".join(OPS))
    if not values:
        raise SweepError("values 网格为空")
    rows = [r for r in CB.load_cases(sim=sim, path=cases_path)
            if (r.get("settle_date") or "") <= as_of
            and r.get("outcome") in ("WIN", "LOSS")]
    fp = facts_path or SWEEP_PATH
    have = {(r.get("field"), r.get("op"), str(r.get("value")))
            for r in _rows_of(fp)}
    out, n_new = [], 0
    for v in values:
        ident = (field, op, str(v))
        if ident in have:
            continue
        hit = [c for c in rows if _match(c, field, op, v)]
        st = _stats(hit)
        rec = {"schema_version": SCHEMA_VERSION, "field": field, "op": op,
               "value": v, "n_total": len(rows), "sim": sim,
               "data_upto": as_of, "kind": "SWEEP_FACT",
               "note": "计数事实，无推荐无排序；采用须 verify+用户批准",
               **st}
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        _WG.check_write(facts_path or SWEEP_PATH)
        # ★ 写点 sink 内联 `facts_path or SWEEP_PATH` BoolOp（审计静态解析要求直达）
        with io.open(facts_path or SWEEP_PATH, "a", encoding="utf-8",
                     newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        out.append(rec)
        n_new += 1
    return {"n_grid": len(values), "n_new": n_new, "out_path": fp,
            "as_of": as_of, "sim": sim}


# ── 自检 / 纪律 ──────────────────────────────────────────────────────────────
_TEST_FUNCS = ("_selftest", "_discipline")


def _case(i, settle, val, outcome, pnl):
    feats = {k: None for k in CB.FEATURE_KEYS}
    feats["Catalyst"] = val
    return {"case_id": "CASE-S%s" % i, "sim": True, "settle_date": settle,
            "outcome": outcome, "pnl_pct": pnl, "decision_version": "1.2.0",
            "features": feats}


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

    print("param_sweep 自检（全临时沙盒，真库零接触）：")
    sb = tempfile.mkdtemp(prefix="sw_sb_")
    cases = os.path.join(sb, "cases.jsonl")
    rows = []
    for i in range(20):
        hot = i % 2 == 0
        rows.append(_case(i, "2026-10-07", "A" if hot else "B",
                          "WIN" if (hot and i % 4 != 2) or (not hot and i % 8 == 1)
                          else "LOSS",
                          5.0 if hot else -2.0))
    with io.open(cases, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    fp_sb = os.path.join(sb, "research", "sweep", "sweep_facts.jsonl")

    r = sweep("2026-10-09", sim=True, field="Catalyst", values=["A", "B"],
              op="==", cases_path=cases, facts_path=fp_sb)
    recs = _rows_of(r["out_path"])
    T("A1 网格 2 格各产计数事实（A 组 10 笔 / B 组 10 笔）",
      len(recs) == 2 and {x["n"] for x in recs} == {10})
    T("A2 每格含 n/WIN 占比/平均/最差（事实四件套）",
      all(all(k in x for k in ("n", "win_rate", "avg_pnl_pct",
                               "worst_pnl_pct")) for x in recs))
    T("A3 零推荐：记录无 rank/best/建议 字段",
      all(not ({"rank", "best", "recommend"} & set(x)) for x in recs))
    r2 = sweep("2026-10-09", sim=True, field="Catalyst", values=["A", "B"],
               op="==", cases_path=cases, facts_path=fp_sb)
    T("A4 同日幂等不重写", r2["n_new"] == 0 and len(_rows_of(r2["out_path"])) == 2)
    try:
        sweep("2026-10-09", sim=True, field="outcome", values=["WIN"], op="==",
              cases_path=cases, facts_path=fp_sb)
        T("B1 结算后字段（outcome）→ 拒（防穿越）", False)
    except SweepError:
        T("B1 结算后字段（outcome）→ 拒（防穿越）", True)
    try:
        sweep("2026-10-09", sim=None, field="Catalyst", values=["A"], op="==",
              cases_path=cases, facts_path=fp_sb)
        T("B2 sim 不表态 → 拒", False)
    except SweepError:
        T("B2 sim 不表态 → 拒", True)
    T("C1 零 propose/queue 写入（事实表不进 0.9）",
      "propose(" not in _prod_src() and "calibration" not in _prod_src())
    T("C2 自检前后真库 sweep 事实文件逐字节未变",
      _md5f(SWEEP_PATH) == _DIR_MD5)
    print("param_sweep 自检：%d/%d" % (ok[0], ok[0] + bad[0]))
    return bad[0] == 0


_DIR_MD5 = None


def _md5f(p):
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
    print("param_sweep 纪律：")
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    prod = _prod_src()
    res = [
        ("① 写点静态可解析（sink 内联 facts_path or SWEEP_PATH）",
         "facts_path or SWEEP_PATH" in prod),
        ("② 零 params.json 写入（params.json 零候选）",
         "_write_params" not in prod and "params.json\"" not in prod),
        ("③ 零 propose/promote（验证在前提案在后）",
         "propose(" not in prod and "promote(" not in prod),
        ("④ 字段白名单防穿越", "field not in CB.FEATURE_KEYS" in prod),
        ("⑤ astwin 引用", bool(ASTW.func_table(ast.parse(src)))),
    ]
    for name, c in res:
        print("  [%s] %s" % ("✓" if c else "✗", name))
    print("param_sweep 纪律：%d/%d" % (sum(1 for _, c in res if c), len(res)))
    return all(c for _, c in res)


def _main(argv):
    global _DIR_MD5
    if "--selftest" in argv:
        _DIR_MD5 = _md5f(SWEEP_PATH)
        return 0 if _selftest() else 1
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    as_of = sim = field = op = None
    values = []
    if "--as-of" in argv:
        as_of = argv[argv.index("--as-of") + 1]
    if "--sim" in argv:
        sim = True
    if "--live" in argv:
        sim = False
    if "--field" in argv:
        field = argv[argv.index("--field") + 1]
    if "--op" in argv:
        op = argv[argv.index("--op") + 1]
    if "--values" in argv:
        raw = argv[argv.index("--values") + 1]
        values = [v for v in raw.split(",")]
        values = [float(v) if v.replace(".", "", 1).isdigit() else v
                  for v in values]
    if not (as_of and sim is not None and field and op and values):
        print("用法：param_sweep.py --as-of D --sim|--live --field <特征> "
              "--op >=|<=|== --values v1,v2,...")
        return 2
    r = OBS.run_observed(ACTOR, "param_sweep", STAGE,
                         lambda: sweep(as_of, sim=sim, field=field, values=values,
                                       op=op), as_of=as_of)
    v = r["value"]
    print("param_sweep: grid=%s new=%s -> %s" % (v["n_grid"], v["n_new"],
                                                 v["out_path"]))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
