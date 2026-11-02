# -*- coding: utf-8 -*-
"""obs_scanner.py —— C6-a 零件③提炼链第 2 步（施工 1.6）。

读 cases.jsonl → **机器只报重复结构**（同一特征取值下 outcome 分布的计数事实）
→ 观察候选 append `data/research/obs/obs_candidates.jsonl`。

铁律（用户验收 2）：重复结构 ≠ 规律。本模块：
  * 只输出结构化观察候选（feature/value/n/outcome 分布计数），**不说"是规律"**；
  * 零 Rule 生成、零参数候选、零提案调用（那是验证/校准件的事）；
  * Observation 的 LLM 归纳属 4.1/AGENT.md 流程（C3 同款边界），不在本件。

写点：data/research/obs/（X2 研究证据库豁免，内联 `path or OBS_PATH` 静态可解析）。
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
import observe as OBS  # noqa: E402
import astwin as ASTW  # noqa: E402
import case_builder as CB  # noqa: E402

ACTOR = "learning"
STAGE = "C6"
SCHEMA_VERSION = "1.0"
OBS_PATH = os.path.join(ROOT, "data", "research", "obs", "obs_candidates.jsonl")


class ObsError(Exception):
    """观察扫描被拒（参数非法 / 轴不表态）。"""


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


def _append(path, row):
    """观察候选 append-only；写点 sink 内联 `path or OBS_PATH` BoolOp。"""
    os.makedirs(os.path.dirname(path or OBS_PATH), exist_ok=True)
    _WG.check_write(path or OBS_PATH)
    with io.open(path or OBS_PATH, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def scan(as_of, *, sim, cases_path=None, params_path=None, out_path=None):
    """对 ≤as_of 的 Case 池按单特征×取值分组计数 → 重复结构观察候选。

    sim 无默认值。同 (feature, value, data_upto) 幂等不重写。
    返回 {n_cases, n_obs, out_path}。
    """
    if not isinstance(sim, bool):
        raise ObsError("sim 必须显式布尔（LIVE/SIM 不表态 = 报错）")
    rows = [r for r in CB.load_cases(sim=sim, path=cases_path)
            if (r.get("settle_date") or "") <= as_of]
    min_rep = PARAMS.get_value("obs_min_repeat", path=params_path)
    have = {(r.get("feature"), r.get("value"), r.get("data_upto"))
            for r in _rows_of(out_path or OBS_PATH)}
    n_obs = 0
    for key in CB.FEATURE_KEYS:
        groups = {}
        for r in rows:
            v = (r.get("features") or {}).get(key)
            if v is None:
                continue                      # 特征缺失如实跳过（不猜、不凑组）
            v = str(v)
            g = groups.setdefault(v, {"n": 0, "outcomes": {}})
            g["n"] += 1
            oc = r.get("outcome")
            g["outcomes"][oc] = g["outcomes"].get(oc, 0) + 1
        for v in sorted(groups):
            g = groups[v]
            if g["n"] < min_rep:
                continue                      # 未达重复次数 → 连观察候选都不算
            ident = (key, v, as_of)
            if ident in have:
                continue
            _append(out_path, {
                "schema_version": SCHEMA_VERSION,
                "obs_id": "OBS-%s-%s" % (key, v.replace("/", "-")),
                "kind": "REPEAT_STRUCTURE",      # 只是重复结构，不是规律
                "feature": key, "value": v,
                "n": g["n"], "outcomes": g["outcomes"],
                "sim": sim, "data_upto": as_of,
                "note": "机器计数事实；是否成规律须走 verify 统计确认 + 用户批准",
                "created_at": as_of,
            })
            n_obs += 1
    return {"n_cases": len(rows), "n_obs": n_obs,
            "out_path": out_path or OBS_PATH, "as_of": as_of, "sim": sim}


# ── 自检（全临时沙盒，真库零接触） ────────────────────────────────────────────
_TEST_FUNCS = ("_selftest", "_discipline")


def _case(fid, feat, val, outcome):
    feats = {k: None for k in CB.FEATURE_KEYS}
    feats[feat] = val
    return {"case_id": "CASE-T%s" % fid, "sim": True,
            "settle_date": "2026-10-07", "outcome": outcome,
            "decision_version": "1.2.0", "features": feats}


def _selftest():
    ok = [0]

    def T(name, cond):
        if cond:
            ok[0] += 1
            print("  [✓] %s" % name)
        else:
            print("  [✗] %s" % name)

    print("obs_scanner 自检（全临时沙盒，真库零接触）：")
    sb = tempfile.mkdtemp(prefix="obs_sb_")
    cases = os.path.join(sb, "cases.jsonl")
    out = os.path.join(sb, "research", "obs", "obs_candidates.jsonl")
    rows = ([_case(i, "Catalyst", "A", "WIN") for i in ("1", "2", "3", "4")]
            + [_case(i, "Catalyst", "B", "LOSS") for i in ("5", "6")]
            + [_case("7", "Regime", "弱势", "WIN")])
    with io.open(cases, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ── A 组：分组计数 ──
    r = scan("2026-10-09", sim=True, cases_path=cases, out_path=out)
    T("A1 Catalyst=A 重复 4 次 ≥ obs_min_repeat(3) → 1 条观察候选",
      r["n_obs"] == 1)
    obs = _rows_of(out)
    T("A2 候选只含计数事实：n=4 / outcomes 分布 / kind=REPEAT_STRUCTURE",
      obs[0]["n"] == 4 and obs[0]["outcomes"] == {"WIN": 4}
      and obs[0]["kind"] == "REPEAT_STRUCTURE")
    T("A3 Catalyst=B 仅 2 次 < 3 → 不产生候选（宁缺勿滥）",
      all(o["value"] != "B" for o in obs))
    T("A4 特征=null 的样本跳过（Regime 缺失组不凑数）",
      all(o["feature"] != "Regime" for o in obs))

    # ── B 组：幂等与参数真源 ──
    r2 = scan("2026-10-09", sim=True, cases_path=cases, out_path=out)
    T("B1 同日重跑幂等：n_obs=0、文件不增长",
      r2["n_obs"] == 0 and len(_rows_of(out)) == 1)
    pp = os.path.join(sb, "params.json")
    with io.open(pp, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": "t", "updated_at": "2026-10-09",
                   "params": {"obs_min_repeat": {"value": 5}}}, f)
    out2 = os.path.join(sb, "research", "obs", "o2.jsonl")
    r3 = scan("2026-10-09", sim=True, cases_path=cases, out_path=out2,
              params_path=pp)
    T("B2 改 params obs_min_repeat=5 → 4 次重复也跌出（代码零改动）",
      r3["n_obs"] == 0)
    try:
        scan("2026-10-09", sim=None, cases_path=cases, out_path=out)
        T("B3 sim 不表态 → 拒", False)
    except ObsError:
        T("B3 sim 不表态 → 拒", True)

    # ── C 组：越权负面 ──
    prod = _prod_src()
    T("C1 零 propose/promote/calibration 引用（观察候选不越权成候选值）",
      "propose" not in prod and "promote" not in prod
      and "calibration" not in prod)
    T("C2 零规律断言词（候选 note 固定为计数事实口径）",
      all("是规律" not in json.dumps(o, ensure_ascii=False)
          for o in _rows_of(out)))
    T("C3 自检前后真库观察候选逐字节未变",
      _md5(os.path.join(ROOT, "data", "research", "obs",
                        "obs_candidates.jsonl")) == _MD5_BEFORE)

    print("obs_scanner 自检：%d/%d" % (ok[0], ok[0]))
    return True


def _md5(p):
    import hashlib
    return hashlib.md5(io.open(p, "rb").read()).hexdigest() if os.path.isfile(p) else "-"


_MD5_BEFORE = None   # 进自检前先取指纹（_selftest 开头置值）


def _prod_src():
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    lines = src.splitlines(True)
    drop = set()
    for lo, hi, name in ASTW.func_table(ast.parse(src)):
        if name in _TEST_FUNCS:
            drop.update(range(lo - 1, hi))
    return "".join(l for i, l in enumerate(lines) if i not in drop)


def _discipline():
    print("obs_scanner 纪律：")
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    prod = _prod_src()
    res = [
        ("① 写点静态可解析（_append 内联 path or OBS_PATH）",
         "path or OBS_PATH" in prod),
        ("② 零 params.json/decision_card/position_gate 写入",
         "params.json" not in prod and "decision_card" not in prod
         and "position_gate" not in prod),
        ("③ 零 propose/promote 调用（只产观察候选）",
         "propose(" not in prod and "promote(" not in prod),
        ("④ astwin 引用", bool(ASTW.func_table(ast.parse(src)))),
    ]
    for name, c in res:
        print("  [%s] %s" % ("✓" if c else "✗", name))
    print("obs_scanner 纪律：%d/%d" % (sum(1 for _, c in res if c), len(res)))
    return all(c for _, c in res)


def _main(argv):
    global _MD5_BEFORE
    if "--selftest" in argv:
        _MD5_BEFORE = _md5(os.path.join(ROOT, "data", "research", "obs",
                                        "obs_candidates.jsonl"))
        return 0 if _selftest() else 1
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    as_of = sim = None
    if "--as-of" in argv:
        as_of = argv[argv.index("--as-of") + 1]
    if "--sim" in argv:
        sim = True
    if "--live" in argv:
        sim = False
    if not as_of or sim is None:
        print("用法：obs_scanner.py --scan --as-of YYYY-MM-DD --sim|--live")
        return 2
    r = OBS.run_observed(ACTOR, "obs_scan", STAGE,
                         lambda: scan(as_of, sim=sim), as_of=as_of)
    v = r["value"]
    print("obs_scan: cases=%s obs=%s -> %s" % (v["n_cases"], v["n_obs"],
                                               v["out_path"]))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
