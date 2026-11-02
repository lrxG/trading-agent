# -*- coding: utf-8 -*-
"""demand_scanner.py —— C6-a 零件②：需求发现（施工 1.6，正式版扫描器）。

读 Case 池 → 机械扫判断质量分布 / 重复错误 / 胜利但判断错 → 学习缺口 →
append `data/learning/demand_queue.jsonl`（**唯一队列**，与 0.6 settle 步5 的
SETTLE_MINIMAL 同一落点、行 `scanner` 字段区分；本件只走 CLI/API，**不接结算
主链**——切换属骨架契约统一批，用户拍板 3）。

阈值真源 = params：`demand_repeat_wrong_min` / `demand_min_settled` /
`demand_support_floor`（对齐 settle 内置默认 2 / 3 / 1/3 口径）。同日同缺口幂等。

信号升格验证（1.6 低频档）：`signal_promotion_check` 对 C1 只记录的 3 信号
（成交额 / 涨跌家数比 / 指数20日线，来自 case.env.signals）做「信号在记录 vs
判断质量/结果」的分组计数对比 —— **只报计数事实**（观察候选），升格路径 =
观察 → verify 统计确认 → 用户批准（与普通过链同一条，无特权后门）。

零 LLM、零决策输出、零提案调用。写点：data/learning/（X5 豁免）+
data/research/（X2 豁免），均内联默认静态可解析。
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
DEMAND_PATH = os.path.join(ROOT, "data", "learning", "demand_queue.jsonl")
SIG_OBS_PATH = os.path.join(ROOT, "data", "research", "obs",
                            "signal_promotion.jsonl")
# C1 只记录的 3 信号（1.1 三个记录信号；键名 = env.signals 的 snake_case 约定）
RECORDED_SIGNALS = ("turnover", "up_down_ratio", "index_ma20")


class DemandError(Exception):
    """需求扫描被拒（轴不表态 / 参数非法）。"""


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


def _append_demand(row, path=None):
    """学习队列 append-only；写点 sink 内联 `path or DEMAND_PATH` BoolOp。"""
    os.makedirs(os.path.dirname(path or DEMAND_PATH), exist_ok=True)
    _WG.check_write(path or DEMAND_PATH)
    with io.open(path or DEMAND_PATH, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _append_sig(row, path=None):
    """信号观察候选 append-only；写点 sink 内联 `path or SIG_OBS_PATH` BoolOp。"""
    os.makedirs(os.path.dirname(path or SIG_OBS_PATH), exist_ok=True)
    _WG.check_write(path or SIG_OBS_PATH)
    with io.open(path or SIG_OBS_PATH, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def scan(as_of, *, sim, cases_path=None, params_path=None, demand_path=None):
    """正式需求扫描：缺口 → demand_queue（kind=GAP / SCAN_NOOP），同日幂等。"""
    if not isinstance(sim, bool):
        raise DemandError("sim 必须显式布尔（LIVE/SIM 不表态 = 报错）")
    rows = [r for r in CB.load_cases(sim=sim, path=cases_path)
            if (r.get("settle_date") or "") <= as_of
            and r.get("outcome") in ("WIN", "LOSS")]
    n = len(rows)
    rep_min = PARAMS.get_value("demand_repeat_wrong_min", path=params_path)
    n_min = PARAMS.get_value("demand_min_settled", path=params_path)
    floor = PARAMS.get_value("demand_support_floor", path=params_path)
    gaps = []
    wrong_l, wrong_3, win_but_wrong = {}, {}, 0
    verd = {"SUPPORT": 0, "MIXED": 0, "REFUTE": 0}
    for r in rows:
        for k, v in (r.get("attribution_L1L5") or {}).items():
            if v == "WRONG":
                wrong_l[k] = wrong_l.get(k, 0) + 1
        for k, v in (r.get("attribution_三问") or {}).items():
            if v == "WRONG":
                wrong_3[k] = wrong_3.get(k, 0) + 1
        ms = [r.get(k) for k in ("M1", "M2", "M3")]
        for v in ms:
            if v in verd:
                verd[v] += 1
        if r.get("outcome") == "WIN" and ms and all(
                m == "REFUTE" for m in ms if m is not None) \
                and any(m is not None for m in ms):
            win_but_wrong += 1
    if n:
        for k in sorted(wrong_l):
            if wrong_l[k] >= rep_min:
                gaps.append({"kind": "REPEAT_ERROR", "where": "L1L5.%s" % k,
                             "count": wrong_l[k]})
        for k in sorted(wrong_3):
            if wrong_3[k] >= rep_min:
                gaps.append({"kind": "REPEAT_ERROR", "where": "三问.%s" % k,
                             "count": wrong_3[k]})
        if win_but_wrong:
            gaps.append({"kind": "WIN_BUT_WRONG", "where": "M1/M2/M3 全 REFUTE",
                         "count": win_but_wrong})
        tot = sum(verd.values())
        sr = (verd["SUPPORT"] / tot) if tot else None
        if sr is not None and n >= n_min and sr < floor:
            gaps.append({"kind": "JUDGEMENT_QUALITY_LOW", "where": "SUPPORT 占比",
                         "ratio": round(sr, 4)})
    row = {"as_of": as_of, "schema_version": SCHEMA_VERSION,
           "kind": "GAP" if gaps else "SCAN_NOOP", "gaps": gaps,
           "n_cases": n, "scanner": "DEMAND_SCANNER",
           "params_version": PARAMS.load_params(path=params_path).get("version"),
           "created_at": as_of}
    dp = demand_path or DEMAND_PATH
    same = [r for r in _rows_of(dp)
            if r.get("as_of") == as_of and r.get("kind") == row["kind"]
            and r.get("gaps") == row["gaps"]
            and r.get("scanner") == "DEMAND_SCANNER"]
    if not same:
        _append_demand(row, dp)   # ★ 判重与写入同一目标文件（dp）——漏传即写真库
    return {"gaps": gaps, "kind": row["kind"], "n_cases": n,
            "appended": not same, "demand_path": dp, "as_of": as_of, "sim": sim}


def signal_promotion_check(as_of, *, sim, cases_path=None, out_path=None):
    """低频档：C1 记录信号 vs 判断质量/结果的分组计数（观察候选，零升格动作）。"""
    if not isinstance(sim, bool):
        raise DemandError("sim 必须显式布尔（LIVE/SIM 不表态 = 报错）")
    rows = [r for r in CB.load_cases(sim=sim, path=cases_path)
            if (r.get("settle_date") or "") <= as_of
            and r.get("outcome") in ("WIN", "LOSS")]
    out = []
    for sig in RECORDED_SIGNALS:
        has, hasnt = [], []
        for r in rows:
            v = ((r.get("env") or {}).get("signals") or {}).get(sig)
            (has if v is not None else hasnt).append(r)
        def wr(group):
            done = [r for r in group if r.get("outcome")]
            return {"n": len(done),
                    "win_rate": (sum(1 for r in done
                                     if r["outcome"] == "WIN") / len(done))
                    if done else None}
        out.append({"schema_version": SCHEMA_VERSION, "signal": sig,
                    "recorded": wr(has), "not_recorded": wr(hasnt),
                    "kind": "SIGNAL_OBSERVATION",
                    "note": "计数事实；升格须走 观察→verify 统计确认→用户批准，"
                            "驱动依据区正式入口待治理（挂账）",
                    "data_upto": as_of, "created_at": as_of})
    op = out_path or SIG_OBS_PATH
    have = {(r.get("signal"), r.get("data_upto"))
            for r in _rows_of(op)}
    n_new = 0
    for r in out:
        if (r["signal"], r["data_upto"]) in have:
            continue
        _append_sig(r, op)        # ★ 判重与写入同一目标文件（op）——漏传即写真库
        n_new += 1
    return {"n_signals": len(RECORDED_SIGNALS), "n_new": n_new,
            "out_path": op, "as_of": as_of, "sim": sim}


# ── 自检（全临时沙盒，真库零接触） ────────────────────────────────────────────
_TEST_FUNCS = ("_selftest", "_discipline")


def _case(i, settle, outcome, l1wrong=False, win_but_wrong=False, sig=None,
          m="SUPPORT"):
    feats = {k: None for k in CB.FEATURE_KEYS}
    return {"case_id": "CASE-D%s" % i, "sim": True, "settle_date": settle,
            "outcome": outcome, "decision_version": "1.2.0", "features": feats,
            "attribution_L1L5": {"L3": "WRONG" if l1wrong else "OK"},
            "attribution_三问": {"驱动": "OK"},
            "M1": "REFUTE" if win_but_wrong else m,
            "M2": "REFUTE" if win_but_wrong else m,
            "M3": "REFUTE" if win_but_wrong else m,
            "env": {"signals": ({"turnover": 1.1} if sig else {})}}


def _write(path, rows):
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

    print("demand_scanner 自检（全临时沙盒，真库零接触）：")
    sb = tempfile.mkdtemp(prefix="dm_sb_")
    cases = os.path.join(sb, "cases.jsonl")
    demand = os.path.join(sb, "learning", "demand_queue.jsonl")
    pp = os.path.join(sb, "params.json")
    with io.open(pp, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": "t", "updated_at": "2026-10-09", "params": {
            "demand_repeat_wrong_min": {"value": 2},
            "demand_min_settled": {"value": 3},
            "demand_support_floor": {"value": 0.34}}}, f)

    # ── A 组：缺口识别 ──
    rows = [_case(i, "2026-10-07", "LOSS", l1wrong=True, m="REFUTE")
            for i in range(3)]
    rows += [_case("w1", "2026-10-07", "WIN", win_but_wrong=True)]
    rows += [_case("g1", "2026-10-07", "WIN")]
    _write(cases, rows)
    r = scan("2026-10-09", sim=True, cases_path=cases, params_path=pp,
             demand_path=demand)
    T("A1 L3 连续错 3 次 ≥ 2 → REPEAT_ERROR 缺口",
      any(g["kind"] == "REPEAT_ERROR" and g["where"] == "L1L5.L3"
          for g in r["gaps"]))
    T("A2 胜利但判断错 → WIN_BUT_WRONG 缺口",
      any(g["kind"] == "WIN_BUT_WRONG" for g in r["gaps"]))
    T("A3 SUPPORT 占比 3/15 < 0.34 → JUDGEMENT_QUALITY_LOW",
      any(g["kind"] == "JUDGEMENT_QUALITY_LOW" for g in r["gaps"]))
    T("A4 行带 scanner=DEMAND_SCANNER（与 SETTLE_MINIMAL 同队列可区分）",
      _rows_of(demand)[-1]["scanner"] == "DEMAND_SCANNER")

    # ── B 组：幂等 / 阈值真源 / 无缺口留痕 ──
    r2 = scan("2026-10-09", sim=True, cases_path=cases, params_path=pp,
              demand_path=demand)
    T("B1 同日同缺口幂等不重写", not r2["appended"]
      and len(_rows_of(demand)) == 1)
    _write(cases, [_case("g1", "2026-10-07", "WIN"),
                   _case("g2", "2026-10-07", "WIN")])
    r3 = scan("2026-10-08", sim=True, cases_path=cases, params_path=pp,
              demand_path=demand)
    T("B2 无缺口 → SCAN_NOOP 也留痕（空转可观测）",
      r3["kind"] == "SCAN_NOOP" and r3["appended"])
    pp2 = os.path.join(sb, "p2.json")
    with io.open(pp2, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": "t2", "updated_at": "2026-10-09", "params": {
            "demand_repeat_wrong_min": {"value": 5},
            "demand_min_settled": {"value": 3},
            "demand_support_floor": {"value": 0.01}}}, f)
    _write(cases, rows)
    r4 = scan("2026-10-09", sim=True, cases_path=cases, params_path=pp2,
              demand_path=demand)
    T("B3 改 params repeat_wrong_min=5 → L3 缺口消失（代码零改动）",
      not any(g.get("where") == "L1L5.L3" for g in r4["gaps"]))

    # ── C 组：信号升格观察（只报计数） ──
    sigp = os.path.join(sb, "research", "obs", "signal_promotion.jsonl")
    rs = signal_promotion_check("2026-10-09", sim=True, cases_path=cases,
                                out_path=sigp)
    T("C1 3 个记录信号各产 1 条观察候选",
      rs["n_signals"] == 3 and rs["n_new"] == 3)
    obs = _rows_of(sigp)
    T("C2 候选只含分组计数（recorded/not_recorded 的 n 与 win_rate）",
      all("recorded" in o and "not_recorded" in o for o in obs)
      and all(o["kind"] == "SIGNAL_OBSERVATION" for o in obs))
    rs2 = signal_promotion_check("2026-10-09", sim=True, cases_path=cases,
                                 out_path=sigp)
    T("C3 同日幂等不重写", rs2["n_new"] == 0)
    try:
        scan("2026-10-09", sim=None, cases_path=cases, demand_path=demand)
        T("C4 sim 不表态 → 拒", False)
    except DemandError:
        T("C4 sim 不表态 → 拒", True)

    # ── D 组：越权与真库 ──
    prod = _prod_src()
    T("D1 零 propose/promote（观察不越权成候选）",
      "propose(" not in prod and "promote(" not in prod)
    T("D2 零 params.json/决策卡/position_gate 写入",
      "params.json\"" not in prod and "decision_card" not in prod
      and "position_gate" not in prod)
    T("D3 自检前后真库 demand 队列逐字节未变",
      _md5(DEMAND_PATH) == _M_DEMAND)
    print("demand_scanner 自检：%d/%d" % (ok[0], ok[0] + bad[0]))
    return bad[0] == 0


def _md5(p):
    import hashlib
    return hashlib.md5(io.open(p, "rb").read()).hexdigest() if os.path.isfile(p) else "-"


_M_DEMAND = None


def _prod_src():
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    lines = src.splitlines(True)
    drop = set()
    for lo, hi, name in ASTW.func_table(ast.parse(src)):
        if name in _TEST_FUNCS:
            drop.update(range(lo - 1, hi))
    return "".join(l for i, l in enumerate(lines) if i not in drop)


def _discipline():
    print("demand_scanner 纪律：")
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    prod = _prod_src()
    res = [
        ("① 写点静态可解析（demand/obs 内联默认）",
         "demand_path or DEMAND_PATH" in prod and "out_path or SIG_OBS_PATH" in prod),
        ("② 阈值只读 params（零字面量比较）",
         "demand_repeat_wrong_min" in prod and "demand_support_floor" in prod),
        ("③ 零 propose/promote（观察不越权）",
         "propose(" not in prod and "promote(" not in prod),
        ("④ 不接结算主链（本件无 settle 导入）",
         "import settle" not in prod),
        ("⑤ astwin 引用", bool(ASTW.func_table(ast.parse(src)))),
    ]
    for name, c in res:
        print("  [%s] %s" % ("✓" if c else "✗", name))
    print("demand_scanner 纪律：%d/%d" % (sum(1 for _, c in res if c), len(res)))
    return all(c for _, c in res)


def _main(argv):
    global _M_DEMAND
    if "--selftest" in argv:
        _M_DEMAND = _md5(DEMAND_PATH)
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
        print("用法：demand_scanner.py --scan|--signal-check --as-of "
              "YYYY-MM-DD --sim|--live")
        return 2
    if "--scan" in argv:
        r = OBS.run_observed(ACTOR, "demand_scan", STAGE,
                             lambda: scan(as_of, sim=sim), as_of=as_of)
        v = r["value"]
        print("demand_scan: gaps=%s n=%s -> %s" % (len(v["gaps"]), v["n_cases"],
                                                   v["demand_path"]))
        return 0
    if "--signal-check" in argv:
        r = OBS.run_observed(ACTOR, "signal_promotion", STAGE,
                             lambda: signal_promotion_check(as_of, sim=sim),
                             as_of=as_of)
        v = r["value"]
        print("signal_check: signals=%s new=%s -> %s" % (v["n_signals"],
                                                         v["n_new"], v["out_path"]))
        return 0
    print("未知参数"); return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
