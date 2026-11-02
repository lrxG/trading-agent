#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mainline_scan · **行业热度榜（展示层）**，不是线台账

★ 2026-11-02 语义定案（系统自查）：本件与 `data/state/mainline.json` 不是同一个东西，
  不是替代关系，禁止互相覆盖：
    · `data/state/mainline.json` = **线台账**（token 归并出的"线"：成员/状态/miss 计数），
      字段锁死，唯一写点 = `tools/discovery/scan.py::mainline_update`。**唯一真源。**
    · 本件 = **行业级七维热度排名**（124 行业中谁在走强），只出 md 报告，不写台账。
  两者粒度不同：线（题材/事件 token）vs 行业（申万风格分类）。要"线成员"查台账，
  要"哪个行业热"看本榜。

原 docstring 如下（保留供考古）：
mainline_scan.py · 工序2 主线识别 v1.0（2026-10-29 建 · 选股中心化重构 #280）

## 方法论来源（.workbuddy/skills/trade-rules/SKILL.md §一/§六）

旧系统 7 维主线判据的机械化子集（5/7 维可机械产出）+ 题材七态证据供给：

| 维 | 来源 | 机械判据 |
|---|---|---|
| D1 板块涨幅 | westock sector ranking | 当日涨跌幅（归一 0~6%） |
| D2 涨家数广度 | 同上 upCount | 涨/总占比 |
| D3 资金回流持续性 | 同上 mainNetInflow 1D/5D | 双正=1.0 / 仅1D正=0.5 / 仅5D正=0.4 / 双负=0 |
| D4 龙头辨识度 | 同上 leader(涨幅) | 归一 0~10% |
| D5 成交额集中度 | 同上 turnover | 占全市场比 |
| D6 连板高度梯队 | 盘中扫描 csv + sector constituent | 行业∩涨停/连板桶（梯队名单） |
| D7 催化强度 | 新闻/公告（info_feeder/Agent 层） | **本件不出**，报告留 Agent 作答位 |

综合分 = 0.25·D1 + 0.25·D2 + 0.25·D3 + 0.15·D4 + 0.10·D5（权重写死可复现；改动须递增 SCHEMA 并记 PLAYBOOK）。

## 口径纪律（红线）

1. **零 BUY/WAIT/NO、零"是不是主线"结论**——本件只产事实榜+证据；主线认定与七态阶段判定归 Agent（D7 缺机械源，本就是判断层）。
2. 涨停判定沿用盘中扫描 csv 的 ceiling 字段（不自算口径）；连板用 boards 字段。
3. 全市场成交额=ranking 124 行业 turnover 之和（近似，北交所/停牌覆盖差如实标注）。
4. 落点：data/generated/reports/mainline_scan_<date>_<hhmm>.md；不写落点 1~5。

## 用法

```
python tools/research/mainline_scan.py                 # 干跑（用最新盘中扫描 csv）
python tools/research/mainline_scan.py --execute       # 落盘报告
python tools/research/mainline_scan.py --top 5         # 梯队聚合行业数（默认5）
python tools/research/mainline_scan.py --selftest
"""
from __future__ import annotations

import argparse
import csv
import datetime
import glob
import io
import os
import re
import subprocess
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
INTRA_DIR = os.path.join(_ROOT, "data", "raw", "intraday")
OUT_DIR = os.path.join(_ROOT, "data", "generated", "reports")
SCHEMA = "mainline_scan_v1"
WEIGHTS = {"D1": 0.25, "D2": 0.25, "D3": 0.25, "D4": 0.15, "D5": 0.10}


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _run(cmd_args, timeout=90):
    try:
        p = subprocess.run(["westock"] + cmd_args, capture_output=True, timeout=timeout)
        text = p.stdout.decode("utf-8", "replace")
        return (p.returncode == 0 and text.strip()), text.strip()
    except Exception as e:
        return False, repr(e)


def latest_scan_csv():
    """最新盘中扫描 csv；无则 None。"""
    cands = sorted(glob.glob(os.path.join(INTRA_DIR, "*", "scan_*.csv")))
    return cands[-1] if cands else None


def parse_ranking(text):
    """解析 sector ranking 表 → [{code,name,pct,in1d,in5d,in20d,turnover,up,uptotal,leader,leader_pct}]"""
    out = []
    for ln in text.splitlines():
        m = re.match(r"^\|\s*(pt\d+)\s*\|([^|]+)\|", ln)
        if not m:
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 12 or cells[0] == "code":
            continue
        try:
            up, tot = cells[8].split("/")
            lm = re.match(r"(.+)\(([-\d.]+)\)", cells[11])
            out.append({
                "code": cells[0], "name": cells[1], "pct": float(cells[2]),
                "in1d": float(cells[3]), "in5d": float(cells[4]), "in20d": float(cells[5]),
                "turnover": float(cells[6]), "up": int(up), "tot": int(tot),
                "leader": lm.group(1) if lm else cells[11],
                "leader_pct": float(lm.group(2)) if lm else 0.0,
            })
        except (ValueError, IndexError):
            continue
    return out


def fund_signal(in1d, in5d):
    if in1d > 0 and in5d > 0:
        return 1.0, "双正"
    if in1d > 0:
        return 0.5, "仅当日正"
    if in5d > 0:
        return 0.4, "仅5日正"
    return 0.0, "双负"


def score_sector(sec, total_turnover):
    """机械五维加权分（0~1）。证据字段原样保留。"""
    d1 = max(0.0, min(sec["pct"], 6.0)) / 6.0
    d2 = (sec["up"] / sec["tot"]) if sec["tot"] else 0.0
    d3, d3_label = fund_signal(sec["in1d"], sec["in5d"])
    d4 = max(0.0, min(sec["leader_pct"], 10.0)) / 10.0
    d5 = (sec["turnover"] / total_turnover) if total_turnover else 0.0
    score = (WEIGHTS["D1"] * d1 + WEIGHTS["D2"] * d2 + WEIGHTS["D3"] * d3
             + WEIGHTS["D4"] * d4 + WEIGHTS["D5"] * min(1.0, d5 * 10))
    sec.update({"d1": d1, "d2": d2, "d3": d3, "d3_label": d3_label, "d4": d4,
                "d5": d5, "score": round(score, 4), "fund_label": d3_label})
    return sec


def parse_constituent_codes(text):
    return set(re.findall(r"\|\s*(s[hz]\d{6})\s*\|", text))


def build_md(secs, tiers, scan_src, data_date, n_top):
    L = []
    A = L.append
    A("# 主线识别榜 · %s（数据 %s · %s）" % (_now()[:16], data_date, SCHEMA))
    A("")
    A("> **零主线结论**：本件=五维机械事实榜（D1涨幅/D2广度/D3资金回流/D4龙头/D5成交额集中度）+ 梯队证据。")
    A("> 主线认定、题材七态（T0-T7）阶段、催化强度（D7）归 Agent 作答；BUY/WAIT/NO 归 C4。")
    A("")
    A("盘中扫描源：`%s`" % scan_src)
    A("")
    A("## 主线候选榜（按机械综合分降序，Top %d）" % len(secs))
    A("")
    A("| # | 行业 | 涨幅% | 涨/总 | 主力1D(万) | 5D(万) | 资金信号 | 龙头(幅%) | 成交占比% | 综合分 |")
    A("|---|---|---|---|---|---|---|---|---|---|")
    for i, s in enumerate(secs, 1):
        A("| %d | %s | %.2f | %d/%d | %.0f | %.0f | %s | %s(%.2f) | %.2f | %.3f |" % (
            i, s["name"], s["pct"], s["up"], s["tot"], s["in1d"], s["in5d"],
            s["fund_label"], s["leader"], s["leader_pct"], s["d5"] * 100, s["score"]))
    A("")
    for s in tiers:
        A("## 梯队证据 · %s（成份股 %d 只 ∩ 盘中涨停桶）" % (s["name"], s["n_const"]))
        A("")
        if s["tier"]:
            A("| 连板 | 代码 | 名称 | 现价涨幅% | 委比 |")
            A("|---|---|---|---|---|")
            for t in s["tier"][:8]:
                A("| %d板 | %s | %s | %.2f | %.0f |" % (t["boards"], t["code"], t["name"], t["pct"], t.get("wb", 0)))
        else:
            A("（该行业成份内盘中无涨停/连板股——涨幅榜与梯队背离，警惕脉冲）")
        A("")
    A("## Agent 作答位（七态判定）")
    A("")
    A("对 Top3 各答：①是否主线（D7 催化强度+叙事纯度）②七态阶段 T0-T7（证据：涨停家数环比/高度/龙头轮换）③今日该看谁。")
    A("")
    A("## 数据缺口（如实）")
    A("")
    A("- D7 催化强度无机械源（需 news/公告语义层）")
    A("- 行业口径=申万二级(pt码)，与旧系统通达信881行业存在映射差")
    A("- 连板数为盘中截面递推值（boards_src 单列口径见盘中扫描件）")
    return "\n".join(L)


def _selftest():
    n = 0
    ok = []
    def chk(name, cond):
        nonlocal n
        ok.append((name, cond))
        n += 1
    rank_text = """| code | name | changePct | mainNetInflow | mainNetInflow5d | mainNetInflow20d | turnover | turnoverRate | upCount | mainInflow | mainOutflow | leader |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| pt01 | 半导体 | 3.0 | 100.0 | 200.0 | 300.0 | 50000.0 | 2.0 | 90/189 | 400.0 | 300.0 | 拓荆科技(8.5) |
| pt02 | 白酒 | -1.0 | -50.0 | -80.0 | -90.0 | 20000.0 | 1.0 | 2/19 | 10.0 | 60.0 | 舍得酒业(2.6) |
"""
    secs = parse_ranking(rank_text)
    chk("ranking解析2行", len(secs) == 2 and secs[0]["name"] == "半导体")
    chk("leader解析", secs[1]["leader"] == "舍得酒业" and abs(secs[1]["leader_pct"] - 2.6) < 1e-9)
    tt = sum(s["turnover"] for s in secs)
    s0 = score_sector(dict(secs[0]), tt)
    s1 = score_sector(dict(secs[1]), tt)
    chk("资金信号双正>双负", s0["d3"] == 1.0 and s1["d3"] == 0.0 and s0["score"] > s1["score"])
    chk("综合分界内", 0.0 <= s0["score"] <= 1.0)
    cons = parse_constituent_codes("| sh688072 | 拓荆科技 |\n| sz300053 | 航宇微 |")
    chk("成份解析", cons == {"sh688072", "sz300053"})
    md = build_md([s0], [], "test.csv", "2026-10-29", 1)
    chk("报告含零结论纪律", "零主线结论" in md and "Agent 作答位" in md)
    fails = [k for k, v in ok if not v]
    print("== mainline_scan --selftest: %d/%d 通过 ==" % (n - len(fails), n))
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser(description="工序2 主线识别（五维事实榜+梯队证据）")
    ap.add_argument("--top", type=int, default=5, help="梯队聚合行业数")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()

    csvp = latest_scan_csv()
    if not csvp:
        print("[ABORT] 无盘中扫描 csv——先跑 tools/e2e/intraday_scan.py --execute")
        return 2
    rows = []
    with io.open(csvp, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({"code": r["code"], "name": r["name"], "pct": float(r["pct"] or 0),
                             "ceiling": float(r["ceiling"] or 0), "boards": int(float(r["boards"] or 0)),
                             "wb": float(r["wb_ratio"] or 0)})
            except (ValueError, KeyError):
                continue
    zt_pool = [x for x in rows if x["ceiling"] > 0 and x["pct"] >= 9.7]  # 近似涨停桶（ceiling 判定详见盘中件）
    zb_pool = sorted([x for x in rows if x["boards"] >= 2], key=lambda x: -x["boards"])
    data_date = os.path.basename(os.path.dirname(csvp))

    ok, rtext = _run(["sector", "ranking"], timeout=120)
    if not ok:
        print("[ABORT] sector ranking 通道失败")
        return 2
    secs = parse_ranking(rtext)
    total_turnover = sum(s["turnover"] for s in secs)
    secs = sorted((score_sector(s, total_turnover) for s in secs), key=lambda x: -x["score"])

    tiers = []
    for s in secs[:args.top]:
        okc, ctext = _run(["sector", "constituent", s["code"]], timeout=60)
        codes = parse_constituent_codes(ctext) if okc else set()
        tier = sorted([x for x in zt_pool if x["code"] in codes], key=lambda x: -x["boards"])
        s["n_const"] = len(codes)
        tiers.append(dict(s, tier=tier))

    md = build_md(secs[:max(10, args.top)], tiers, os.path.relpath(csvp, _ROOT), data_date, args.top)
    if args.execute:
        os.makedirs(OUT_DIR, exist_ok=True)
        fn = os.path.join(OUT_DIR, "mainline_scan_%s_%s.md" % (
            data_date, datetime.datetime.now().strftime("%H%M")))
        io.open(fn, "w", encoding="utf-8", newline="\n").write(md)
        print("[SAVED]", fn)
    else:
        print(md)
    print("[STATS] 行业 %d · 全市场涨停桶 %d · 连板≥2 %d · 榜首 %s(%.3f)" % (
        len(secs), len(zt_pool), len(zb_pool), secs[0]["name"], secs[0]["score"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
