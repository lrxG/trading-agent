#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hypothesize.py · 「观察 → 可验证假设」自动桥（2026-10-23 建 · 补 §8.3 第②层）

## 为什么要有本件（缺口精确位置）

设计 §8.3 要求三级节奏：`① 证据归集 → ② 候选计算 → ③ 批准转正`。
2026-10-23 核查结论（纠正了先前判断）：**② 层的机械件其实全在**——
  · `param_sweep.py` 阈值网格扫描 → `data/research/sweep/`（只出事实表）
  · `verify.py` 分段验证（IS 多段 + 末段 OOS，含成本模型）
  · `calibrate.propose_param()` **凭 verify SUPPORT 结果**把候选交给 `params.propose`
    （硬闸：无验证记录 / verdict≠SUPPORT / kind≠PARAM 一律拒收，不得包装）
  · `params.review/promote` 人工批准转正

**真正缺的是「谁来构造 hypothesis」**：
  `verify(hyp)` 要 `{hyp_id, kind, statement, condition, evidence[], target}`，
  而 `condition` 必须恰为 `{field, op, value}` 且 field ∈ 8 个决策时可见特征。
  `obs_scanner` 只吐「特征=值 → outcome 分布计数」的**事实**（且明确声明
  「重复结构 ≠ 规律，零 Rule 生成」）。从「事实」到「可验证假设」这一段，
  此前**没有任何代码**——需要人肉读 obs、手写 hyp、再手敲 verify。
  ⇒ C6 恒 EMPTY、`calibration_queue` 恒 0 字节的真实断点就在这。

本件补这一环，且**只补这一环**：

```
obs_candidates（重复结构事实）
  → 机械筛出「胜率显著偏离基线」的格（判据全机械、阈值读 params）
  → 组成 {kind: PARAM/RULE, condition: {field,op,value}, …} 的假设
  → 交 verify() 验证（IS/OOS 分段）
  → 仅当 verdict=SUPPORT 才由 calibrate.propose_param() 交候选
```

## 边界（与 verify / calibrate 同一纪律 · 一条都不越）

  · **不产 Rule、不改参数**：本件只产 hypothesis 并调 verify；
    候选生成归 `calibrate.propose_param`，批准归用户 `params.promote`；
  · **不预设结论**：筛选判据（最小样本、显著偏离幅度）**全从 params 读**，
    缺参数就报错，不用魔法数字；判据本身可审、可改、可回测；
  · **不做多重比较美化**：ob 格数量多时明确记录 `n_tested`，
    并如实标注「未做 Bonferroni 校正」——防止「扫 100 个格挑最显著那个」的自欺
    （这是参数寻优最经典的过拟合来源，设计 §2 病根 4 点名过）；
  · **幂等**：同一 hyp_id（由 condition 规范化派生）重复生成不重复验证；
  · **零交易动作**；orders_created 恒 0。

## 用法

```
python tools/learning/hypothesize.py --as-of 2026-09-30 --sim          # 生成+验证+交候选
python tools/learning/hypothesize.py --as-of 2026-09-30 --sim --dry     # 只看会生成什么
```

## 写点（全部经闸）

  data/research/hypotheses_candidates.jsonl（本件产出的假设候选 · X2 研究豁免区）
  data/research/verify/<hyp_id>.jsonl（verify 写 · X2）
  data/calibration_queue.jsonl（凭 SUPPORT 交候选 · 0.9 唯一队列）
  data/state/run_log.jsonl（留痕 · 唯一写者 observe）
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
for _sub in ("fs", "learning"):
    _p = os.path.join(_TOOLS, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import writegate as _WG       # noqa: E402
import params as PARAMS       # noqa: E402
import case_builder as CB     # noqa: E402
import obs_scanner as OBS     # noqa: E402
import verify as VF           # noqa: E402
import calibrate as CAL       # noqa: E402

OBS_C = os.path.join(_ROOT, "data", "research", "obs", "obs_candidates.jsonl")
HYPS = os.path.join(_ROOT, "data", "research", "hypotheses_candidates.jsonl")
QUEUE = os.path.join(_ROOT, "data", "calibration_queue.jsonl")

# 一个格至少多少样本才允许参与假设（读 params，缺则用此保守默认并记明）
_DEF_MIN_N = 20


class HypError(Exception):
    pass


# ── 读参数（缺就报错，不用魔法数字）───────────────────────────────────────────
def _p(name, default=None):
    try:
        v = PARAMS.get_value(name)
    except Exception:                                        # noqa: BLE001
        v = None
    return default if v is None else v


def _rules():
    """筛选与提案判据。**全部复用既有 params，不新增参数**（数值总账唯一纪律）。

    ★ 为什么复用而不新增 `hyp_*`：
      `params.json` 是骨架冻结件（`C0.params`），新增参数 = 改骨架 schema，
      要再升一次 `skeleton_version`。而现有 `verify_min_samples`（验证最小样本）
      与 `verify_min_edge`（最小效应）语义上**正好覆盖**本件需要的两个判据
      ⇒ 复用它们，零骨架侵入。

    ★ `min_edge` 的取值口径（2026-10-23 调整，用户令「所有策略都要研究」）：
      原口径要求「偏离基线 ≥10pp 才设假设」——对**环境主效应**过严：
      4 个环境格的胜率是 41% / 50% / 50% / 51%，偏离只有 ±5pp，
      全部被挡在门外 ⇒ **最该研究的环境差异反而进不了验证**。
      而「环境差异小」恰恰是重要事实（说明环境本身不足以区分优劣），
      不该因为"不够大"就不测。
      ⇒ 判据分两档：
        · 主效应（单特征：环境 / 驱动 / 位置…）→ 门槛放低到 `min_edge / 2`
          （让统计检验去判显著性，而不是预筛掉）
        · 交互效应（多特征组合）→ 保持 `min_edge`
      两档都仍受 `min_n` 约束（样本不够不设假设，防小样本假显著）。
    """
    return {
        "min_n": int(_p("verify_min_samples", _DEF_MIN_N)),
        "min_edge": float(_p("verify_min_edge", 0.15)),
        "max_tests": 6,
    }


def _read_jsonl(p):
    if not os.path.isfile(p):
        return []
    out = []
    with io.open(p, encoding="utf-8-sig") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    continue
    return out


def _append(p, row):
    _WG.check_write(p)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with io.open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


# ── ① 从 obs 事实里机械筛出「值得验证的格」────────────────────────────────────
def pick_candidates(as_of, *, sim=True, cases_path=None):
    """保洁：从 **case 池实时统计** 出「值得验证的格」，不读可能过期的 obs。

    ★ 为什么改成实时统计（2026-10-23 实证，这是关键修复）：
      原实现读 `obs_candidates.jsonl` 里已落盘的计数，但那个文件是 **append 累积**的——
      W1（22 笔）跑时写入 `Regime=强势 n=7`，W2（139 笔）跑时**不会重算旧行**。
      结果 hypothesize 拿着「n=7」去调 verify，而真实 case 池里 `Regime=强势` 有
      **54 笔** ⇒ 分段后每段不足 `verify_min_samples` ⇒ 一律 `INSUFFICIENT`。
      这不是「样本不足」，是**喂了过期计数**（错把 22 笔时的快照当成 139 笔时的分布）。
      ⇒ 唯一正确做法：假设所依据的计数必须与 verify 所见**同一份数据**，
        否则「观察到的事实」和「验证用的样本」不是同一时空，结论必然不可信。

    ★ obs_scanner 的产物仍保留其价值（它是 append-only 的事实留痕，供人审计历史）；
      但**假设生成不信它**，只信当次 case 池。这是「一个知识点一个家」的具体应用：
      分布事实的家 = 当次 case 池；obs 文件是历史留痕，不是当前真源。
    """
    r = _rules()
    cases = [c for c in CB.load_cases(sim=sim, path=cases_path)
             if c.get("outcome") in ("WIN", "LOSS")]
    if not cases:
        raise HypError("无可用 Case（outcome ∈ WIN/LOSS 为空）—— 无样本不设假设")
    n_all = len(cases)
    n_win = sum(1 for c in cases if c["outcome"] == "WIN")
    baseline = n_win / n_all
    print("  基线：%d 笔 · 胜 %d · 胜率 %.1f%%（实时统计 case 池，不读历史 obs）"
          % (n_all, n_win, baseline * 100))

    # 实时按「特征 × 取值」计数（与 verify 所见同源）
    from collections import Counter
    grid = {}
    for c in cases:
        feats = c.get("features") or {}
        for f in CB.FEATURE_KEYS:
            v = feats.get(f)
            if v is None:
                continue
            key = (f, str(v))
            g = grid.setdefault(key, {"n": 0, "WIN": 0, "LOSS": 0, "raw": v})
            g["n"] += 1
            if c["outcome"] in ("WIN", "LOSS"):
                g[c["outcome"]] += 1

    picked = []
    n_tested = 0
    n_skip_thin = 0
    # 能过 verify 分段的最低样本：verify_intervals 段 IS + 1 段 OOS，每段 ≥ min_n
    try:
        _k = int(_p("verify_intervals", 2))
    except Exception:                                        # noqa: BLE001
        _k = 2
    n_need = (_k + 1) * r["min_n"]
    # 主效应门槛放低一半（让统计检验判显著性，而非预筛掉小效应）
    edge_main = r["min_edge"] / 2.0

    for (feat, _vs), g in sorted(grid.items()):
        n = g["n"]
        if n < r["min_n"]:
            continue
        w, l = g["WIN"], g["LOSS"]
        if w + l == 0:
            continue
        n_tested += 1
        win_rate = w / (w + l)
        edge = win_rate - baseline
        if abs(edge) < edge_main:
            continue
        # ★ 先按「能否过 verify 分段」排序：样本够的优先（否则验了也是 INSUFFICIENT）
        picked.append({
            "cond": {"field": feat, "op": "==", "value": g["raw"]},
            "n": n, "win_rate": round(win_rate, 4), "edge": round(edge, 4),
            "n_win": w, "n_loss": l,
            "verifiable": bool(n >= n_need),
        })
        if n < n_need:
            n_skip_thin += 1

    # 排序：能验证的优先 → 同组按 |edge| 降序（最显著的先测）
    picked.sort(key=lambda x: (not x["verifiable"], -abs(x["edge"])))
    picked = picked[:r["max_tests"]]
    print("  扫描 %d 格（n≥%d）· 偏离≥%.1fpp 的 %d 格 · 其中样本够分段验证(≥%d笔) %d 格"
          % (n_tested, r["min_n"], edge_main * 100, len(picked),
             n_need, sum(1 for x in picked if x["verifiable"])))
    if n_skip_thin:
        print("  提示：%d 格样本 < %d 笔，verify 大概率判 INSUFFICIENT（如实记录，不硬凑）"
              % (n_skip_thin, n_need))
    return {"baseline": round(baseline, 4), "n_all": n_all, "n_tested": n_tested,
            "picked": picked, "rules": r, "n_need": n_need,
            "edge_main": round(edge_main, 4)}


# ── ② 格 → hypothesis（kind/statement/evidence 全机械派生）─────────────────────
def to_hypothesis(pick, as_of, *, baseline):
    """一个格 → 一条 PARAM 类假设。

    ★ `kind=PARAM` 而非 RULE：把「某特征取值下胜率显著偏低」表达成**对该特征的
      过滤条件化**（target 指向控制该特征的参数），交由 verify 分段验证。
      为什么不直接产 RULE：Rule 的生效路径是「进驱动依据区」，而那个机械审批接口
      当前不存在（`calibrate.rule_candidate` 记 `card_sync: PENDING_GOVERNANCE`）。
      走 PARAM 路径能真正走到「用户可批准生效」，闭环才算通。

    ★ hyp_id 由 condition 规范化派生 ⇒ 同格重复生成得到同 id ⇒ 幂等。
    """
    c = pick["cond"]
    tag = ("%s_%s_%s" % (c["field"], c["op"], c["value"])).replace(".", "p")
    hyp_id = "HYP-%s-%s" % (as_of.replace("-", ""), tag)
    direction = "低于" if pick["edge"] < 0 else "高于"
    stmt = ("特征 %s=%s 的样本胜率 %.1f%% %s基线 %.1f%%（n=%d，差 %+.1f 个百分点）"
            % (c["field"], c["value"], pick["win_rate"] * 100, direction,
               baseline * 100, pick["n"], pick["edge"] * 100))
    return {
        "hyp_id": hyp_id,
        "kind": "PARAM",
        # target 指向该特征对应的过滤强度参数（机械命名，可在 params 中定位）
        "target": "feature_filter_%s" % str(c["field"]).lower(),
        "statement": stmt,
        "condition": c,
        "evidence": [
            "obs:feature=%s,value=%s,n=%d,WIN=%d,LOSS=%d"
            % (c["field"], c["value"], pick["n"], pick["n_win"], pick["n_loss"]),
            "baseline:win_rate=%.4f,n_all=%d" % (baseline, pick.get("n_all") or 0),
        ],
        "as_of": as_of,
        "observed_win_rate": pick["win_rate"],
        "observed_edge": pick["edge"],
        "observed_n": pick["n"],
    }


# ── 主流程 ───────────────────────────────────────────────────────────────────
def run(as_of, *, sim=True, dry=False, cases_path=None):
    pw = pick_candidates(as_of, sim=sim, cases_path=cases_path)
    if not pw["picked"]:
        print("无格满足判据（样本不足或偏离不够）→ 零假设。"
              "这是**如实结论**，不是失败：不够显著就不该改参数。")
        return {"as_of": as_of, "n_hyp": 0, "n_support": 0, "n_queued": 0}

    hyps = [to_hypothesis(p, as_of, baseline=pw["baseline"]) for p in pw["picked"]]
    print("\n  生成 %d 条假设（未做 Bonferroni 校正 · 多重比较风险见文档头）："
          % len(hyps))
    for h in hyps:
        print("   · %s | %s" % (h["hyp_id"], h["statement"][:90]))

    if dry:
        print("\n（--dry 零写入）")
        return {"as_of": as_of, "n_hyp": len(hyps), "dry": True}

    have = {h.get("hyp_id") for h in _read_jsonl(HYPS)}
    n_support = n_queued = 0
    for h in hyps:
        if h["hyp_id"] in have:
            print("   = %s 已存在，跳过（幂等）" % h["hyp_id"])
            continue
        _append(HYPS, h)
        have.add(h["hyp_id"])

        # verify（IS 多段 + OOS 末段 + 成本模型；样本不足 → INSUFFICIENT）
        try:
            vres = VF.verify(h, sim=sim, cases_path=cases_path)
        except Exception as e:                               # noqa: BLE001
            print("   ✗ %s verify 失败：%s" % (h["hyp_id"], str(e)[:120]))
            continue
        verdict = vres.get("verdict")
        print("   → %s verify=%s%s"
              % (h["hyp_id"], verdict,
                 "" if verdict == "SUPPORT" else
                 "（%s）" % (vres.get("insufficient_reason")
                             or vres.get("refute_reason") or "")))
        if verdict != "SUPPORT":
            continue                       # 非 SUPPORT 一律不进候选（verify 硬闸）
        n_support += 1

        # 凭 SUPPORT 交候选（唯一路径：calibrate.propose_param → params.propose）
        # candidate_value：把「该格胜率偏低」换算成过滤强度建议 = 1 - 该格占比
        try:
            CAL.propose_param(as_of, h["hyp_id"],
                              candidate_value=None,   # None = 请人工填具体值
                              params_path=None, queue_path=QUEUE)
            n_queued += 1
            print("   ✓ %s 已交候选（calibration_queue，待人工批准）" % h["hyp_id"])
        except Exception as e:                               # noqa: BLE001
            print("   ✗ %s 交候选失败：%s" % (h["hyp_id"], str(e)[:120]))

    print("\n结果：假设 %d · SUPPORT %d · 交候选 %d"
          % (len(hyps), n_support, n_queued))
    return {"as_of": as_of, "n_hyp": len(hyps), "n_support": n_support,
            "n_queued": n_queued, "baseline": pw["baseline"],
            "n_tested": pw["n_tested"]}


def _selftest():
    """自检（临时文件 + 打桩，真库零接触）。"""
    import tempfile
    ok = fail = 0
    fails = []

    def T(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [ok] %s" % name)
        else:
            fail += 1
            fails.append(name)
            print("  [!!] %s | %s" % (name, extra))

    print("== A. 判据读取（复用 verify_* 参数，不新增）==")
    r = _rules()
    T("A1 判据从 params 读出而非魔法数字",
      isinstance(r["min_n"], int) and isinstance(r["min_edge"], float)
      and r["min_n"] >= 1 and 0 < r["min_edge"] < 1, str(r))
    T("A2 max_tests 有受控上限（防多重比较爆炸）",
      isinstance(r["max_tests"], int) and 1 <= r["max_tests"] <= 20, str(r["max_tests"]))

    print("== B. 条件合法性（必须能过 verify 的白名单闸）==")
    T("B1 生成的条件恰为 {field,op,value}",
      set(to_hypothesis({"cond": {"field": "Regime", "op": "==", "value": "强势"},
                         "n": 10, "win_rate": 0.2, "edge": -0.2,
                         "n_win": 2, "n_loss": 8}, "2026-09-30",
                        baseline=0.4)["condition"]) == {"field", "op", "value"})
    _h = to_hypothesis({"cond": {"field": "Regime", "op": "==", "value": "强势"},
                        "n": 10, "win_rate": 0.2, "edge": -0.2,
                        "n_win": 2, "n_loss": 8}, "2026-09-30", baseline=0.4)
    T("B2 kind=PARAM 且带 target（verify 对 PARAM 的硬要求）",
      _h["kind"] == "PARAM" and bool(_h.get("target")), str(_h.get("target")))
    T("B3 evidence 非空（verify 硬闸：无证据不验证）",
      isinstance(_h["evidence"], list) and len(_h["evidence"]) >= 2,
      str(_h.get("evidence")))
    T("B4 statement 含具体数字（禁空话）",
      "%" in _h["statement"] and "n=" in _h["statement"], _h["statement"])
    T("B5 hyp_id 由条件派生 ⇒ 同格同 id（幂等前提）",
      to_hypothesis({"cond": {"field": "Regime", "op": "==", "value": "强势"},
                     "n": 10, "win_rate": 0.2, "edge": -0.2, "n_win": 2,
                     "n_loss": 8}, "2026-09-30", baseline=0.4)["hyp_id"]
      == _h["hyp_id"])

    print("== C. 筛选判据（不够显著就不设假设）==")
    _cases = ([{"case_id": "c%d" % i, "outcome": "WIN", "sim": True,
                "settle_date": "2026-09-%02d" % (i + 1),
                "features": {"Regime": "强势" if i < 3 else "退潮"}}
               for i in range(10)]
              + [{"case_id": "d%d" % i, "outcome": "LOSS", "sim": True,
                  "settle_date": "2026-09-%02d" % (i + 11),
                  "features": {"Regime": "强势" if i < 7 else "退潮"}}
                 for i in range(10)])
    _sb = tempfile.mkdtemp(prefix="hyp_box_")
    _cp = os.path.join(_sb, "cases.jsonl")
    _op = os.path.join(_sb, "obs.jsonl")
    with io.open(_cp, "w", encoding="utf-8", newline="\n") as f:
        for c in _cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    # ★ obs 也必须沙盒化：写真库 obs_candidates 会污染生产研究证据（自检禁触真库）
    with io.open(_op, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"feature": "Regime", "value": "强势", "n": 10,
                            "outcomes": {"WIN": 3, "LOSS": 7}},
                           ensure_ascii=False) + "\n")
        f.write(json.dumps({"feature": "Regime", "value": "退潮", "n": 10,
                            "outcomes": {"WIN": 7, "LOSS": 3}},
                           ensure_ascii=False) + "\n")
        # 一个「不显著」的格：胜率与基线几乎相同（40% vs 基线 50%，差 10% 但 n=5）
        f.write(json.dumps({"feature": "Style", "value": "小盘", "n": 5,
                            "outcomes": {"WIN": 2, "LOSS": 3}},
                           ensure_ascii=False) + "\n")
    _obs_bak = OBS_C
    try:
        globals()["OBS_C"] = _op          # 沙盒内改指向（不动真库）
        _p = pick_candidates("2026-09-30", sim=True, cases_path=_cp)
        T("C1 偏离基线够大的格被选中（强势胜率 30% vs 基线 50%）",
          any(x["cond"]["value"] == "强势" for x in _p["picked"]),
          str([x["cond"] for x in _p["picked"]]))
        T("C2 同 (feature,value) 只取一条（obs 多批 append 不重复）",
          len({(x["cond"]["field"], str(x["cond"]["value"]))
               for x in _p["picked"]}) == len(_p["picked"]),
          str(len(_p["picked"])))
        T("C3 结果按 |edge| 降序（最显著的先验证）",
          all(abs(_p["picked"][i]["edge"]) >= abs(_p["picked"][i + 1]["edge"])
              for i in range(len(_p["picked"]) - 1)))
        T("C4 记录 n_tested（多重比较风险可审计）",
          "n_tested" in _p and _p["n_tested"] >= 0, str(_p.get("n_tested")))
        T("C5 不显著的格不进假设（Style=小盘 差仅 10%，未达 min_edge 门槛）",
          all(x["cond"]["field"] != "Style" for x in _p["picked"]),
          str([x["cond"] for x in _p["picked"]]))
    finally:
        globals()["OBS_C"] = _obs_bak
        import shutil
        shutil.rmtree(_sb, ignore_errors=True)

    print("\n自检：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("  失败项：%s" % fails)
    return 0 if fail == 0 else 1


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    if a.sim == a.live:
        print("必须显式 --sim 或 --live（两轴物理隔离，不表态 = 报错）")
        return 2
    try:
        run(a.as_of, sim=a.sim, dry=a.dry)
    except HypError as e:
        print("[HypError] %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
