#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zibei_thermo.py —— 名称族温度计（钱今天聚在哪个壳）

设计立场（2026-11-09 定，源自「羊字辈」那次纠错的实锤）
------------------------------------------------------
字辈/名称族**不是因子，是正在被轮换的载体**。所以本工具三条铁律：

1. **不人工指定族。** 族一律由「当日涨停名单的名称 n-gram 富集度」自动挖出。
   人工指定（「我觉得洋字辈好」）等于用结论找证据，必然 p-hacking。
2. **温度只看封板率，不看中位涨幅。** 中位涨幅会被族内少数大票带偏；
   封板率 = 族内涨停数 ÷ 族内票数，量的是「钱真封死了多少比例」，方向不可伪造。
3. **只报状态，不预测下一个壳。** 输出四件：族内票数 / 涨停只数 / 封板率 / 最高几板，
   外加与前 N 日的封板率变化（在聚 or 在散）。**没有历史胜率支撑的族不出现在推荐位。**

数据源（两套，自动适配）
------------------------
- 盘后：data/snapshot/<date>/daily_quote.csv（code,name,pct,close）
        + data/snapshot/<date>/limit_up.csv（code,name,boards,seal_amount,reason）
- 盘中：data/raw/intraday/<date>/scan_*.csv（code,name,pct,price,ceiling,boards,amount）

用法
----
  python tools/analysis/zibei_thermo.py --date 2026-11-06
  python tools/analysis/zibei_thermo.py --intraday data/raw/intraday/2026-11-09/scan_1109.csv
  python tools/analysis/zibei_thermo.py --series 东方 --date 2026-11-06
  python tools/analysis/zibei_thermo.py --verify         # 历史前瞻性检验

★ 已验证的负面结论（2026-11-09 实测，别再用它当买点）
-----------------------------------------------------
`--verify` 跑 2026-09-01 ~ 11-06（43 日 / 252 个族-日）：
「当日封板率 − 前日封板率 > +2pp（在聚）」这个信号**没有前瞻性**。

| 族范围 | T+1 超额 | T+1 日胜率 | T+1 按日聚类 t | T+3 超额 | T+3 日胜率 |
|---|---|---|---|---|---|
| 全部族（含单字） | +0.290% | 45.0% | **−0.15** | +0.347% | 44.7% |
| 仅双字族 | +0.228% | 42.4% | **−0.33** | **−0.011%** | 38.7% |

超额看着有，但 t 值贴近 0、日胜率不过半 → **统计上等于零，不能用**。
根因：自动挖出的"族"里混了大量行业词（健康/制药/医药/光电/传媒），
名称族与行业族在程序层面没有可靠分离手段（试过 stock_industry.json 的行业代码，
粒度是三级细分，"东方"27 只跨 24 个代码，无区分度 —— 已放弃自动判别，改为原样报行业名让人看）。

**所以本工具只做「当日状态描述」，不做「买点信号」**：
回答"钱今天聚在哪个壳、在聚还是散"，不回答"该买哪个字"。
把它的输出当买入依据 = 重犯 2026-11-09「洋字辈」那次的错。
"""

from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import os
import statistics
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
SNAP = os.path.join(ROOT, "data", "snapshot")
INTRADAY = os.path.join(ROOT, "data", "raw", "intraday")

# 族的一字 n-gram 常见虚词/无区分度的字，直接排除（否则「股」「科」「新」会占满）
STOP1 = set("科新技股集国中*ＳＳＴST")
# 族规模阈值：太少没统计意义，太多没有区分度
M_MIN, M_MAX = 8, 260
# 富集倍数门槛
ENRICH_MIN = 2.5
# 族内涨停只数门槛
GL_MIN = 2


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


def load_snapshot(date: str):
    """盘后快照 → (rows, limit_map)。rows: 全市场；limit_map: code -> (boards, seal)"""
    q = os.path.join(SNAP, date, "daily_quote.csv")
    if not os.path.exists(q):
        return None, None
    rows = []
    with open(q, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            pct = _f(r.get("pct"))
            if pct is None:
                continue
            rows.append({"code": r["code"], "name": r["name"], "pct": pct})
    limit_map = {}
    lp = os.path.join(SNAP, date, "limit_up.csv")
    if os.path.exists(lp):
        with open(lp, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if not r.get("code"):
                    continue
                limit_map[r["code"]] = (_f(r.get("boards")) or 1.0,
                                        _f(r.get("seal_amount")),
                                        (r.get("reason") or "").strip())
    return rows, limit_map


def load_intraday(path: str):
    """盘中扫描快照 → (rows, limit_map)。封板口径 price >= ceiling。"""
    rows, limit_map = [], {}
    with open(path, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            pct = _f(r.get("pct"))
            if pct is None or not r.get("name"):
                continue
            px, ceil = _f(r.get("price")), _f(r.get("ceiling"))
            sealed = px is not None and ceil is not None and px >= ceil - 0.005
            rows.append({"code": r["code"], "name": r["name"], "pct": pct})
            if sealed:
                limit_map[r["code"]] = (_f(r.get("boards")) or 1.0, _f(r.get("amount")), "")
    return rows, limit_map


def clean(rows):
    """剔 ST / 退市 / 停牌无成交。ST 是另一套定价机制，混进族会污染封板率。"""
    out = []
    for r in rows:
        n = r["name"]
        if "ST" in n.upper() or "退" in n:
            continue
        out.append(r)
    return out


def ngrams(name: str):
    """名称的 1-gram 与 2-gram（跳过市场前缀、跳过 STOP1、跳过空白）。"""
    s = "".join(ch for ch in name.strip() if not ch.isspace())
    out = []
    for i in range(len(s)):
        g = s[i]
        if g not in STOP1:
            out.append(g)
    for i in range(len(s) - 1):
        out.append(s[i:i + 2])
    return set(out)


def families(rows, limit_map, topk=8, minlen=1):
    """自动挖族：按 (族内涨停占比 ÷ 族内票数占比) 排序。minlen=2 只出双字族。"""
    M = len(rows)
    L = len([r for r in rows if r["code"] in limit_map])
    if M == 0 or L == 0:
        return [], M, L
    by_gram = collections.defaultdict(list)
    for r in rows:
        for g in ngrams(r["name"]):
            by_gram[g].append(r)

    cands = []
    for g, members in by_gram.items():
        if len(g) < minlen or not g.strip():
            continue
        m = len(members)
        if m < M_MIN or m > M_MAX:
            continue
        hit = [r for r in members if r["code"] in limit_map]
        gl = len(hit)
        if gl < GL_MIN:
            continue
        enrich = (gl / L) / (m / M)
        if enrich < ENRICH_MIN:
            continue
        boards = [limit_map[r["code"]][0] for r in hit]
        seals = [limit_map[r["code"]][1] or 0 for r in hit]
        reasons = [limit_map[r["code"]][2] if len(limit_map[r["code"]]) > 2 else "" for r in hit]
        rs = [x for x in reasons if x]
        # ★跨行业数 = 名称族 vs 行业族的唯一可靠判据（方向不能反）
        ind = industry_map()
        all_inds = {ind.get(x["code"]) for x in members if ind.get(x["code"])}
        n_ind = len(all_inds)
        cands.append({
            "gram": g,
            "m": m,
            "gl": gl,
            "enrich": enrich,
            "seal_rate": gl / m,
            "max_boards": max(boards) if boards else 0,
            "sum_seal": sum(seals),
            "boards": sorted(boards, reverse=True),
            "names": [r["name"] for r in hit],
            "n_ind": n_ind,
            "industries": sorted(set(rs)),
            "is_1char": len(g) == 1,
        })
    cands.sort(key=lambda c: (-c["enrich"], -c["gl"]))
    # 去重：若某 1-gram 已被某 2-gram 完全覆盖且后者更强，丢掉 1-gram
    kept, seen = [], set()
    for c in cands:
        if c["gram"] in seen:
            continue
        kept.append(c)
        seen.add(c["gram"])
    return kept[:topk], M, L


def dates():
    if not os.path.isdir(SNAP):
        return []
    ds = [d for d in os.listdir(SNAP) if os.path.isdir(os.path.join(SNAP, d))]
    return sorted(ds)


_IND = None


def industry_map():
    """全市场 code → 行业代码（data/raw/meta/stock_industry.json，5580 条）。

    用途：判别「名称族」与「行业族」。判据方向不能搞反 ——
    名称抱团的本质是**跨行业**（「洋」跨了医药/农业/汽零/通信，所以只能是名字驱动）；
    若一个族的成员全落在同一行业，它只是行业的别名（「健康」「制药」）。
    """
    global _IND
    if _IND is None:
        p = os.path.join(ROOT, "data", "raw", "meta", "stock_industry.json")
        try:
            with open(p, encoding="utf-8") as fh:
                _IND = json.load(fh)
        except Exception:
            _IND = {}
    return _IND


def series(gram: str, ds=None):
    """某族逐日封板率序列（族成员以「当日名称含该 gram」动态重算，不用固定名单）。"""
    ds = ds or dates()
    out = []
    for d in ds:
        rows, lm = load_snapshot(d)
        if not rows:
            continue
        rows = clean(rows)
        members = [r for r in rows if gram in r["name"]]
        if not members:
            continue
        gl = len([r for r in members if r["code"] in lm])
        boards = [lm[r["code"]][0] for r in members if r["code"] in lm]
        out.append({
            "date": d, "m": len(members), "gl": gl,
            "seal_rate": gl / len(members),
            "max_boards": max(boards) if boards else 0,
        })
    return out


def cmd_day(date: str, intraday_path: str, topk: int, minlen: int = 1):
    if intraday_path:
        rows, lm = load_intraday(intraday_path)
        src = intraday_path
    else:
        rows, lm = load_snapshot(date)
        src = os.path.join(SNAP, date, "daily_quote.csv")
    if not rows:
        print("无数据源：%s" % src)
        return 2
    rows = clean(rows)
    med = statistics.median(r["pct"] for r in rows)
    fam, M, L = families(rows, lm, topk=topk, minlen=minlen)

    print("=" * 78)
    print("名称族温度计  %s" % (date or intraday_path))
    print("数据源 %s" % src)
    print("全市场 %d 只（剔 ST）｜中位 %+.2f%%｜涨停/封板 %d 只（封板率 %.1f%%）"
          % (M, med, L, 100.0 * L / M if M else 0))
    print("=" * 78)
    if not fam:
        print("今日无满足门槛的族（富集≥%.1f 且 族内涨停≥%d）—— 钱没有聚在任何名称壳上。"
              % (ENRICH_MIN, GL_MIN))
        return 0

    hdr = "%-6s %5s %5s %8s %9s %6s %10s  %s" % (
        "族", "票数", "涨停", "封板率", "富集倍数", "最高板", "封单合计", "族内涨停票行业（自辨名称/行业）")
    print(hdr)
    print("-" * 100)
    for c in fam:
        if c["industries"]:
            inds = "、".join(c["industries"][:5])
        elif intraday_path:
            inds = "（盘中无行业分类，盘后补）"
        else:
            inds = "—"
        print("%-6s %5d %5d %7.1f%% %8.1fx %6.0f %9.2f亿  %s"
              % (c["gram"], c["m"], c["gl"], 100 * c["seal_rate"],
                 c["enrich"], c["max_boards"], c["sum_seal"] / 1e8, inds))
    print()
    for c in fam[:4]:
        tail = "  [单字族·仅参考]" if c["is_1char"] else ""
        n_ind = len(c["industries"])
        if n_ind >= 3:
            tail += "  ← %d 个不同行业，跨行业分布 = 名称驱动" % n_ind
        elif n_ind == 1:
            tail += "  ← 单一行业，可能只是行业别名"
        print("  [%s] 板梯 %s | %s%s"
              % (c["gram"], [int(x) for x in c["boards"]],
                 "、".join(c["names"][:8]), tail))
    if intraday_path:
        print("\n⚠ 盘中模式的缺口：本快照无行业分类字段，无法提示「这个族是不是只是行业」。")
        print("   自辨方法：看族内名字——若涨停票名称都只共享一个字（东方/洋），基本是名称驱动；")
        print("   若族名本身就是行业词（通信/医药/光电），那它大概率就是行业，不是壳。")

    # 与前一交易日对比（只在有历史快照时）
    prev = [d for d in dates() if d < date] if date else []
    if prev:
        p = prev[-1]
        prow, plm = load_snapshot(p)
        if prow:
            prow = clean(prow)
            print("\n封板率变化（vs %s）：" % p)
            for c in fam[:5]:
                pm = [r for r in prow if c["gram"] in r["name"]]
                pgl = len([r for r in pm if r["code"] in plm]) if pm else 0
                pr = pgl / len(pm) if pm else 0.0
                delta = c["seal_rate"] - pr
                tag = "在聚 ↑" if delta > 0.02 else ("在散 ↓" if delta < -0.02 else "平 →")
                print("  %-6s 今日 %5.1f%% ← 前日 %5.1f%%  %+6.1fpp  %s  (前日 %d/%d)"
                      % (c["gram"], 100 * c["seal_rate"], 100 * pr,
                         100 * delta, tag, pgl, len(pm)))
    return 0


def cmd_series(gram: str, upto: str):
    ds = [d for d in dates() if not upto or d <= upto][-20:]
    s = series(gram, ds)
    if not s:
        print("无序列")
        return 1
    print("族「%s」近 %d 个交易日封板率序列" % (gram, len(s)))
    print("%-12s %5s %5s %8s %6s" % ("日期", "票数", "涨停", "封板率", "最高板"))
    for x in s:
        bar = "█" * int(round(x["seal_rate"] * 50))
        print("%-12s %5d %5d %7.1f%% %6.0f  %s"
              % (x["date"], x["m"], x["gl"], 100 * x["seal_rate"], x["max_boards"], bar))
    return 0


def cmd_verify(upto: str = None, minlen: int = 1):
    """前瞻性检验：族的「封板率在聚」能不能预测次日/次三日？

    口径（写死，防事后调整）：
    - 样本单位 = (日期, 族)。族由当日自动挖（富集≥ENRICH_MIN、涨停≥GL_MIN）。
    - 信号 = 当日封板率 − 前一日同族封板率 > +2pp（「在聚」）。
    - 收益 = 次日起 1/3 个交易日，族内**等权**收益的累乘；基准 = 同期全市场等权。
    - 显著性按**交易日聚类**（同一天多个族不独立），报 t 但明确标注 df。
    """
    ds = [d for d in dates() if not upto or d <= upto]
    if len(ds) < 8:
        print("历史快照不足 8 日，无法检验")
        return 1
    idx = {d: i for i, d in enumerate(ds)}
    cache = {}
    for d in ds:
        rows, lm = load_snapshot(d)
        if rows:
            cache[d] = (clean(rows), lm)

    recs = {"1": [], "3": []}
    base = {"1": [], "3": []}
    n_fam = 0
    for i, d in enumerate(ds):
        if d not in cache:
            continue
        rows, lm = cache[d]
        fam, M, L = families(rows, lm, topk=6, minlen=minlen)
        if not fam:
            continue
        n_fam += len(fam)
        # 前一日封板率
        prevd = ds[i - 1] if i > 0 else None
        for c in fam:
            if not prevd or prevd not in cache:
                continue
            prow, plm = cache[prevd]
            pm = [r for r in prow if c["gram"] in r["name"]]
            if not pm:
                continue
            pr = len([r for r in pm if r["code"] in plm]) / len(pm)
            if c["seal_rate"] - pr <= 0.02:
                continue
            for hold in ("1", "3"):
                k = int(hold)
                if i + k >= len(ds):
                    continue
                rets, brets = [], []
                for j in range(1, k + 1):
                    dd = ds[i + j]
                    if dd not in cache:
                        rets, brets = [], []
                        break
                    frows, _ = cache[dd]
                    cur = [r["pct"] for r in frows if c["gram"] in r["name"]]
                    if cur:
                        rets.append(statistics.mean(cur) / 100.0)
                    brets.append(statistics.mean(r["pct"] for r in frows) / 100.0)
                if len(rets) == k and len(brets) == k:
                    v = 1.0
                    for x in rets:
                        v *= (1 + x)
                    recs[hold].append((d, c["gram"], (v - 1) * 100))
                    b = 1.0
                    for x in brets:
                        b *= (1 + x)
                    base[hold].append((b - 1) * 100)

    print("=" * 78)
    print("族温度前瞻性检验（%s ~ %s，%d 个交易日）" % (ds[0], ds[-1], len(ds)))
    print("信号定义：当日封板率 − 前日同族封板率 > +2pp（「在聚」）"
          "｜族长度 ≥%d 字" % minlen)
    print("样本单位 = (日期, 族)；共识别 %d 个族-日" % n_fam)
    print("=" * 78)
    for hold in ("1", "3"):
        xs = recs[hold]
        if not xs:
            print("T+%s：无样本" % hold)
            continue
        # 按日聚类：先算每日的族均值，再对日序列做 t
        byday = collections.defaultdict(list)
        for d, g, r in xs:
            byday[d].append(r)
        daymean = [statistics.mean(v) for v in byday.values()]
        allv = [r for _, _, r in xs]
        bm = statistics.mean(base[hold]) if base[hold] else 0.0
        sd = statistics.stdev(daymean) if len(daymean) > 1 else 0.0
        t = (statistics.mean(daymean) / (sd / (len(daymean) ** 0.5))) if sd else 0.0
        win = 100.0 * len([x for x in daymean if x > 0]) / len(daymean)
        print("\nT+%s（持有 %s 日）" % (hold, hold))
        print("  族-日样本 %d 个，聚合到 %d 个交易日" % (len(xs), len(daymean)))
        print("  日聚合均值 %+.3f%%/次  ｜ 逐样本均值 %+.3f%%" % (statistics.mean(daymean), statistics.mean(allv)))
        print("  基准（全市场同期等权）%+.3f%%  ｜ 超额 %+.3f%%" % (bm, statistics.mean(daymean) - bm))
        print("  日胜率 %.1f%%  ｜ 按日聚类 t = %.2f（df=%d，|t|>2.1 才算过）" % (win, t, len(daymean) - 1))
        print("  样本族：%s" % "、".join(sorted({g for _, g, _ in xs})[:12]))
    print("\n⚠ 边界（读结论前必须看）：")
    print("   1. 族由当日数据挖出后再看次日收益，含「当日已知信息」成分；")
    print("   2. 2026-11-09 实测口径下 t 值贴 0、日胜率不过半 → 该信号**统计上等于零**，")
    print("      本检验的结论是「在聚的族次日并不显著强于全市场」，不是「在聚=可买」；")
    print("   3. 它不证明能预测下一个壳。族温度计只能做当日状态描述，不能做买点信号。")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--intraday", default=None)
    ap.add_argument("--series", default=None)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--topk", type=int, default=8)
    ap.add_argument("--minlen", type=int, default=1, help="族最小字数，2=只出双字族")
    a = ap.parse_args()
    if a.verify:
        return cmd_verify(a.date, a.minlen)
    if a.series:
        return cmd_series(a.series, a.date)
    return cmd_day(a.date, a.intraday, a.topk, a.minlen)


if __name__ == "__main__":
    sys.exit(main())
