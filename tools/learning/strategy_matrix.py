#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""strategy_matrix.py · 策略 × 环境 效果矩阵（2026-10-23 建 · 用户令）

## 用户的原话与它推翻的东西

「**都要研究啊，所有投资策略都要研究啊，因为没有一种策略能长期持久赚钱，都是某一段时间
赚钱而已，所以都要研究，然后不同市场环境下用不同策略，对症下药，灵活调整**」

这推翻了两种常见做法（也包括我此前的一个错误建议）：
  ✗ 「哪条路胜率高就专攻它」——单条策略的胜率是**时段平均**，掩盖了它只在特定环境有效；
  ✗ 「A 路样本少就放弃 A 路」——少不等于无效，可能只是没在**适合它的环境**里被检验过。

正确的目标不是「选出最好的策略」，而是**建立一张对照表**：
        什么环境 × 用什么策略 × 期望胜率多少 × 样本够不够
环境变了就换打法，而不是死守一套。

## 本件做什么

把 Case 池按 `(Regime 环境) × (Catalyst 驱动类型)` 交叉切格，逐格算：
`n / WIN / LOSS / 胜率 / 相对基线偏离 / 是否达验证门槛`。

★ **只出事实，不出结论**（与 `param_sweep`/`obs_scanner` 同一纪律）：
  格子里胜率高不代表「该用这个策略」——样本可能不够、可能是多重比较的偶然。
  判据交给人 + `verify` 的统计确认，本件零推荐、零排序偏好、零「最优策略」字样。

★ **同时报覆盖率缺口**：哪些格样本不足、哪些格**根本没样本**（例如 C 类驱动
  在当前回测里从未被产出）。「没数据」和「数据差」是两件事，必须分开显示——
  否则会误以为「这个策略不行」，其实只是**没测过**。

## 为什么这件事对「长期进步」是关键

设计 §2 病根 4：「内核是相关不是因果，相关会过期」。
单条策略的胜率会随环境漂移，所以「永久最优策略」不存在；
但「**环境 → 策略**的对应关系」比单条策略稳定得多（环境本身是可判别的）。
本件就是这张对应表的机械数据底座。

## 用法

```
python tools/learning/strategy_matrix.py --sim                    # 当前池全量矩阵
python tools/learning/strategy_matrix.py --sim --min-n 5          # 只看样本≥5 的格
python tools/learning/strategy_matrix.py --sim --json out.json    # 附机读产物
```

## 写点

  无（纯只读分析）。要留证据请用 `--json <path>` 显式指定非受护区路径。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from collections import defaultdict

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
for _sub in ("fs", "learning"):
    _p = os.path.join(_TOOLS, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import case_builder as CB      # noqa: E402
import params as PARAMS        # noqa: E402

# 设计 §5.1 的三类驱动（字母 → 人话）
DRIVER_CN = {
    "A": "A 产业/事件（公告·政策·财报）",
    "B": "B 资金/筹码（龙虎榜·大单·换手）",
    "C": "C 情绪/趋势（连板·晋级率·赚钱效应）",
    "复合": "复合（多类同时成立）",
    "驱动不明": "驱动不明（应判 NO）",
}

REGIMES = ("主升", "强势", "震荡", "退潮")


def _min_n():
    """验证门槛：复用 params（不新增参数 = 零骨架侵入）。"""
    try:
        return int(PARAMS.get_value("verify_min_samples") or 5)
    except Exception:                                        # noqa: BLE001
        return 5


def build(cases, row_key="Regime", col_key="Catalyst"):
    """Case 列表 → 矩阵 dict。纯函数，零 IO。

    `row_key` / `col_key` 可指定**任意白名单特征**切格（2026-10-23 扩展）：
      默认 `Regime × Catalyst`（环境 × 驱动，最关心的主表）；
      也可换 `Style × 量价`、`ThemeStage × Catalyst` 等。
    ★ 为什么允许换维度：用户令「所有投资策略都要研究」——策略不只有"驱动类型"一种分法，
      「大盘风格 × 承接强度」「题材阶段 × 驱动」都是不同视角。硬编死一对维度
      等于预先决定了"什么算策略"，那是主观裁剪，不是研究。
    """
    baseline_n = len(cases)
    baseline_win = sum(1 for c in cases if c["outcome"] == "WIN")
    baseline = (baseline_win / baseline_n) if baseline_n else None

    grid = defaultdict(lambda: {"n": 0, "WIN": 0, "LOSS": 0, "pnl": 0.0})
    by_reg = defaultdict(lambda: {"n": 0, "WIN": 0, "LOSS": 0, "pnl": 0.0})
    by_drv = defaultdict(lambda: {"n": 0, "WIN": 0, "LOSS": 0, "pnl": 0.0})

    for c in cases:
        f = c.get("features") or {}
        rk = f.get(row_key)
        ck = f.get(col_key)
        pnl = c.get("pnl_pct") or 0.0
        for bucket in (grid[(rk, ck)], by_reg[rk], by_drv[ck]):
            bucket["n"] += 1
            bucket[c["outcome"]] += 1
            bucket["pnl"] += pnl

    def _finalize(d, baseline):
        out = {}
        for k, v in d.items():
            n, w, l = v["n"], v["WIN"], v["LOSS"]
            dec = w + l
            out[k] = {
                "n": n, "win": w, "loss": l,
                "win_rate": round(w / dec, 4) if dec else None,
                "edge": round(w / dec - baseline, 4) if dec and baseline is not None else None,
                "avg_pnl_pct": round(v["pnl"] / n, 4) if n else None,
            }
        return out

    return {"baseline": {"n": baseline_n, "win": baseline_win,
                         "win_rate": round(baseline, 4) if baseline else None},
            "row_key": row_key, "col_key": col_key,
            "grid": _finalize(grid, baseline),
            "by_regime": _finalize(by_reg, baseline),
            "by_driver": _finalize(by_drv, baseline)}


def print_matrix(rep, min_n):
    b = rep["baseline"]
    row_key = rep.get("row_key", "Regime")
    col_key = rep.get("col_key", "Catalyst")
    print("=" * 78)
    print("策略 × 环境 效果矩阵（回放口径 · 只出事实不出结论）")
    print("切格维度：行=%s × 列=%s" % (row_key, col_key))
    print("=" * 78)
    print("基线：%d 笔 · 胜 %d · 胜率 %s"
          % (b["n"], b["win"],
             ("%.1f%%" % (b["win_rate"] * 100)) if b["win_rate"] is not None else "—"))
    print("验证门槛：单格 n >= %d（params.verify_min_samples）" % min_n)
    print()

    rows_ = sorted({k[0] for k in rep["grid"]}, key=str)
    cols_ = sorted({k[1] for k in rep["grid"]}, key=str)
    print("【交叉矩阵】行=%s  列=%s" % (row_key, col_key))
    hdr = "%-10s" % row_key
    for c in cols_:
        hdr += "%-18s" % str(c)[:16]
    print(hdr)
    for rk in rows_:
        line = "%-10s" % str(rk)
        for ck in cols_:
            v = rep["grid"].get((rk, ck))
            if not v or not v["n"]:
                line += "%-18s" % "-"
            else:
                mark = "" if v["n"] >= min_n else " d"
                wr = ("%.0f%%" % (v["win_rate"] * 100)) if v["win_rate"] is not None else "-"
                line += "%-18s" % ("n=%d %s%s" % (v["n"], wr, mark))
        print(line)
    print("  (d = 样本未达验证门槛，数值仅作观察不可作依据)")
    print()

    for label, key, d in (("按 %s" % row_key, "by_regime", rep["by_regime"]),
                          ("按 %s" % col_key, "by_driver", rep["by_driver"])):
        print("【%s】" % label)
        for k, v in sorted(d.items(), key=lambda x: str(x[0])):
            if not v["n"]:
                continue
            e = ("%+.1fpp" % (v["edge"] * 100)) if v["edge"] is not None else "-"
            wr = ("%.1f%%" % (v["win_rate"] * 100)) if v["win_rate"] is not None else "-"
            ap = ("%.2f%%" % v["avg_pnl_pct"]) if v["avg_pnl_pct"] is not None else "-"
            print("  %-10s n=%-4d 胜率 %-7s 偏离 %-9s 均涨跌 %s"
                  % (str(k), v["n"], wr, e, ap))
        print()

    print("【覆盖率缺口】")
    holes, thin = [], []
    for (rk, ck), v in rep["grid"].items():
        if not v["n"]:
            holes.append((rk, ck))
        elif v["n"] < min_n:
            thin.append((rk, ck, v["n"]))
    if holes:
        print("  无样本 %d 格（没测过 != 无效）：" % len(holes))
        for rk, ck in holes:
            print("    - %s x %s" % (rk, ck))
    if thin:
        print("  样本不足（<%d）%d 格：" % (min_n, len(thin)))
        for rk, ck, n in thin:
            print("    - %s x %s（n=%d）" % (rk, ck, n))
    if not holes and not thin:
        print("  所有格均达验证门槛")
    print()
    print("=" * 78)
    print("* 本表只报事实：胜率高 != 该用该策略（样本/多重比较/环境漂移都可能是原因）。")
    print("* 「无样本」!=「该策略无效」—— 是没测过。要判定须先让它在该环境下产生样本。")
    print("=" * 78)


def run(*, sim=True, min_n=None, json_out=None, cases_path=None,
        row_key="Regime", col_key="Catalyst"):
    cases = [c for c in CB.load_cases(sim=sim, path=cases_path)
             if c.get("outcome") in ("WIN", "LOSS")]
    if not cases:
        print("无可用 Case（outcome ∈ WIN/LOSS 为空）→ 无矩阵可出。")
        return {"n": 0}
    rep = build(cases, row_key=row_key, col_key=col_key)
    rep["min_n"] = min_n or _min_n()
    print_matrix(rep, rep["min_n"])
    if json_out:
        _write_matrix_json(json_out, rep)
    return rep


def _write_matrix_json(json_out, rep):
    """机读产物（可选）。**锚定调用方显式给的路径**，默认不写任何文件。

    ★ 本件默认零写点（纯只读分析）。只有显式 `--json` 才落盘，
      且目标是调用方指定路径（研究证据，X2 豁免区或临时域）。
      landing 静态审计要求写点能解析到锚：这里用形参 `json_out` 属**调用方注入**，
      已在 `WRITE_ENTRYPOINTS` 登记为注入型写点（同 cursor._write 的 path 注入先例）。
    """
    p = json_out
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1,
                  default=lambda o: list(o) if isinstance(o, tuple) else str(o))
        f.write("\n")
    print("\n机读产物：%s" % p)


def _main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--min-n", type=int, default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--row", default="Regime",
                    help="行维度（白名单特征：Regime/Style/ThemeStage/位置/Catalyst/量价…）")
    ap.add_argument("--col", default="Catalyst", help="列维度")
    ap.add_argument("--all-pairs", action="store_true",
                    help="遍历所有特征两两组合（用户令「所有策略都要研究」）")
    a = ap.parse_args(argv)
    if not a.sim and not a.live:
        print("必须显式 --sim 或 --live（两轴物理隔离，不表态 = 报错）")
        return 2
    if a.all_pairs:
        import itertools
        keys = [k for k in CB.FEATURE_KEYS]
        for rk, ck in itertools.combinations(keys, 2):
            cases = [c for c in CB.load_cases(sim=a.sim)
                     if c.get("outcome") in ("WIN", "LOSS")]
            if not cases:
                print("无可用 Case")
                return 1
            rep = build(cases, row_key=rk, col_key=ck)
            # 只打印有数据的组合，避免刷屏
            if any(v["n"] >= (a.min_n or _min_n()) for v in rep["grid"].values()):
                print()
                print_matrix(rep, a.min_n or _min_n())
        return 0
    run(sim=a.sim, min_n=a.min_n, json_out=a.json, row_key=a.row, col_key=a.col)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
