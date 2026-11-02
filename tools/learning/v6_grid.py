# -*- coding: utf-8 -*-
"""v6_grid.py —— 0.16 #5 新建（图纸「保留」纠偏，现场无旧文件）。

多维网格扫描：param_sweep 的组合版 —— 多字段 × 各自候选值做**笛卡尔积**，
逐组合做机械分组计数（n / WIN 占比 / 平均 pnl_pct / 最差单笔）→
`data/research/sweep/grid_<as_of>.jsonl`（X2 豁免，append-only）。

与 param_sweep 同一边界：事实表不进 0.9 候选队列（验证在前提案在后）、
零排序零推荐、字段白名单防穿越、零 LLM。组合样本 < obs_min_repeat 如实标
`thin_sample`（样本薄，不假装可比）。
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
import param_sweep as SW  # noqa: E402

ACTOR = "learning"
STAGE = "C6"
SCHEMA_VERSION = "1.0"
GRID_PATH = os.path.join(ROOT, "data", "research", "sweep",
                         "grid_facts.jsonl")


class GridError(Exception):
    """网格扫描被拒（维度非法 / 网格空）。"""


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


def grid(as_of, *, sim, dims, cases_path=None, params_path=None,
         facts_path=None):
    """dims = [(field, op, [values...]), ...]（≥1 维）→ 笛卡尔积逐组合计数。

    同 (组合键, data_upto) 幂等。零推荐——输出只供 verify/批准链消费。
    """
    if not isinstance(sim, bool):
        raise GridError("sim 必须显式布尔（LIVE/SIM 不表态 = 报错）")
    if not dims:
        raise GridError("dims 为空（至少 1 维）")
    for field, op, values in dims:
        if field not in CB.FEATURE_KEYS:
            raise GridError("字段 %r 越白名单（防穿越）" % field)
        if op not in SW.OPS:
            raise GridError("op 只许 %s" % "/".join(SW.OPS))
        if not values:
            raise GridError("维度 %r 网格为空" % field)
    thin = PARAMS.get_value("obs_min_repeat", path=params_path)
    rows = [r for r in CB.load_cases(sim=sim, path=cases_path)
            if (r.get("settle_date") or "") <= as_of
            and r.get("outcome") in ("WIN", "LOSS")]
    fp = facts_path or GRID_PATH
    keys = [(f, o, [str(v) for v in vs]) for f, o, vs in dims]
    have = {(r.get("combo_key"), r.get("data_upto")) for r in _rows_of(fp)}
    n_new, n_thin = 0, 0
    for combo in itertools.product(*[range(len(vs)) for _, _, vs in dims]):
        picked = [(dims[i][0], dims[i][1], dims[i][2][combo[i]])
                  for i in range(len(dims))]
        ckey = "|".join("%s%s%s" % (f, o, v) for f, o, v in picked)
        if (ckey, as_of) in have:
            continue
        hit = rows
        for f, o, v in picked:
            hit = [c for c in hit if SW._match(c, f, o, v)]
        st = SW._stats(hit)
        is_thin = st["n"] < thin
        n_thin += 1 if is_thin else 0
        rec = {"schema_version": SCHEMA_VERSION, "combo_key": ckey,
               "dims": [{"field": f, "op": o, "value": v} for f, o, v in picked],
               "n_total": len(rows), "sim": sim, "data_upto": as_of,
               "kind": "GRID_FACT", "thin_sample": is_thin,
               "note": "计数事实，无推荐无排序；采用须 verify+用户批准", **st}
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        _WG.check_write(facts_path or GRID_PATH)
        # ★ 写点 sink 内联 `facts_path or GRID_PATH` BoolOp（审计静态解析要求直达）
        with io.open(facts_path or GRID_PATH, "a", encoding="utf-8",
                     newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        n_new += 1
    total = 1
    for _, _, vs in dims:
        total *= len(vs)
    return {"n_grid": total, "n_new": n_new, "n_thin": n_thin,
            "out_path": fp, "as_of": as_of, "sim": sim}


# ── 自检 / 纪律 ──────────────────────────────────────────────────────────────
_TEST_FUNCS = ("_selftest", "_discipline")


def _case(i, settle, cat, reg, outcome, pnl):
    feats = {k: None for k in CB.FEATURE_KEYS}
    feats["Catalyst"], feats["Regime"] = cat, reg
    return {"case_id": "CASE-G%s" % i, "sim": True, "settle_date": settle,
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

    print("v6_grid 自检（全临时沙盒，真库零接触）：")
    sb = tempfile.mkdtemp(prefix="gr_sb_")
    cases = os.path.join(sb, "cases.jsonl")
    rows = []
    for i in range(24):
        cat = "A" if i % 2 == 0 else "B"
        reg = "强势" if i % 3 == 0 else "弱势"
        win = cat == "A" and i % 4 != 2
        rows.append(_case(i, "2026-10-07", cat, reg,
                          "WIN" if win else "LOSS", 6.0 if win else -2.0))
    with io.open(cases, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    fp_sb = os.path.join(sb, "research", "sweep", "grid_facts.jsonl")
    dims = [("Catalyst", "==", ["A", "B"]), ("Regime", "==", ["强势", "弱势"])]

    r = grid("2026-10-09", sim=True, dims=dims, cases_path=cases, facts_path=fp_sb)
    recs = _rows_of(r["out_path"])
    T("A1 2×2 网格 4 组合全产出", r["n_grid"] == 4 and r["n_new"] == 4
      and len(recs) == 4)
    T("A2 每组合 thin_sample 按计数如实标注（n≥obs_min_repeat 为 False）",
      all(recs[i]["thin_sample"] == (recs[i]["n"] < 3) for i in range(4)))
    T("A3 零推荐字段（rank/best/recommend 不存在）",
      all(not ({"rank", "best", "recommend"} & set(x)) for x in recs))
    r2 = grid("2026-10-09", sim=True, dims=dims, cases_path=cases, facts_path=fp_sb)
    T("A4 同日幂等不重写", r2["n_new"] == 0)
    try:
        grid("2026-10-09", sim=True, dims=[("outcome", "==", ["WIN"])],
             cases_path=cases, facts_path=fp_sb)
        T("B1 结算后字段 → 拒（防穿越）", False)
    except GridError:
        T("B1 结算后字段 → 拒（防穿越）", True)
    try:
        grid("2026-10-09", sim=None, dims=dims, cases_path=cases, facts_path=fp_sb)
        T("B2 sim 不表态 → 拒", False)
    except GridError:
        T("B2 sim 不表态 → 拒", True)
    T("C1 零提案调用（事实表不进 0.9）",
      "propose(" not in _prod_src() and "promote(" not in _prod_src())
    T("C2 自检前后真库 grid 事实文件逐字节未变",
      SW._md5f(GRID_PATH) == SW._DIR_MD5)
    print("v6_grid 自检：%d/%d" % (ok[0], ok[0] + bad[0]))
    return bad[0] == 0


def _prod_src():
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    lines = src.splitlines(True)
    drop = set()
    for lo, hi, name in ASTW.func_table(ast.parse(src)):
        if name in _TEST_FUNCS:
            drop.update(range(lo - 1, hi))
    return "".join(l for i, l in enumerate(lines) if i not in drop)


def _discipline():
    print("v6_grid 纪律：")
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    prod = _prod_src()
    res = [
        ("① 写点静态可解析（sink 内联 facts_path or GRID_PATH）",
         "facts_path or GRID_PATH" in prod),
        ("② 零 params.json 写入", "_write_params" not in prod
         and "params.json\"" not in prod),
        ("③ 零 propose/promote", "propose(" not in prod
         and "promote(" not in prod),
        ("④ 复用 param_sweep 白名单判据（不另写第二份）",
         "SW._match" in prod and "field not in CB.FEATURE_KEYS" in prod),
        ("⑤ astwin 引用", bool(ASTW.func_table(ast.parse(src)))),
    ]
    for name, c in res:
        print("  [%s] %s" % ("✓" if c else "✗", name))
    print("v6_grid 纪律：%d/%d" % (sum(1 for _, c in res if c), len(res)))
    return all(c for _, c in res)


def _main(argv):
    if "--selftest" in argv:
        SW._DIR_MD5 = SW._md5f(GRID_PATH)      # 自检前先取真库指纹
        return 0 if _selftest() else 1
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    print("用法：v6_grid.py --selftest | --discipline "
          "（grid 经 Python API 调用，dims=[(field, op, [values...]), ...]）")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
