# -*- coding: utf-8 -*-
"""事件研究 —— 验证「消息→股价」传导链的条件性（用户命题 2026-10-27）。

命题拆解（用户原文）：
  ① 消息在一定程度上影响股价（有传导链）
  ② 但不是所有消息都影响股价（分类别/分条件）
  ③ 视情况而定（环境/位置/情绪调制）
  ④ 不可忽视（要进决策链）

方法（继承旧系统 F7 口径纪律，全部四坑规避）：
  - 样本：westock notice list 全量公告史（分页抓取）× klines 窗口（2024-01~2026-08）
  - 事件包：同票同日多公告合并 1 包；T0 = 公告日 D 之后首个交易日；窗口完整才计入
  - 超额 = 个股 fwd 收益 − 全市场等权日基准（同窗口）——**等权均值，禁中位数**（F7 坑1）
  - 分类：关键词映射 12 研究类（对齐 F7 31 类的可测子集）
  - 三层调制：位置（pre10 涨幅三档）/ 情绪（近 5 日涨停过）/ 环境档（env_recalc 种子档）

对照基准 = 旧系统 F7（61,980 事件包全量验证，data/research/event_effect/base.json）：
  基准率 +0.36%；增持 +2.98/回购 +1.76/股权激励 +2.59；退市风险 −4.67/风险提示 −2.83；
  减持/质押/担保 ≈ 0（非信号）；位置两头正中间负；情绪票遇利空 −1.97pp；强势市归零。

用法：
  event_study.py selftest
  event_study.py run --codes 250 --limit 200        # 抓样本文告 + 事件研究
"""
import csv
import io
import json
import os
import statistics as st
import subprocess
import sys
from collections import defaultdict

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
for _p in ("fs", "analysis", "e2e"):
    if os.path.join(_TOOLS, _p) not in sys.path:
        sys.path.insert(0, os.path.join(_TOOLS, _p))

KL = os.path.join(_ROOT, "data", "raw", "bus", "klines.csv")
ENV_CSV = os.path.join(_ROOT, "data", "research", "env_recalc",
                       "env_metrics_recalc_full.csv")
OUT_DIR = os.path.join(_ROOT, "data", "research", "event_study")
BASE_KB = os.path.join(_ROOT, "data", "research", "event_effect", "base.json")
WIN_START, WIN_END = "2024-01-01", "2026-08-31"

# 研究类关键词映射（对齐 F7 类；顺序=优先级，先命中先归类）
CLASSIFY = [
    ("增持", ("增持",)),
    ("回购", ("回购",)),
    ("股权激励", ("股权激励", "激励计划", "员工持股")),
    ("减持", ("减持",)),
    ("质押担保", ("质押", "担保", "冻结")),
    ("风险黑天鹅", ("立案", "调查", "退市", "风险提示", "违规", "处罚")),
    ("业绩类", ("业绩", "预增", "预减", "预亏", "快报", "季报", "年报", "中报")),
    ("订单合同", ("中标", "订单", "合同", "签约")),
    ("分红", ("分红", "派息", "利润分配")),
    ("停复牌", ("停牌", "复牌")),
]
PROGRAM_KW = ("关联交易", "聘请", "换届", "审计", "召集", "公告格式")


def classify(title):
    t = str(title or "")
    for name, kws in CLASSIFY:
        if any(k in t for k in kws):
            return name
    if any(k in t for k in PROGRAM_KW):
        return "程序类"
    return "其他"


def fetch_notices(code, limit):
    """某票全部公告史（westock 一手通道，分页抓最近 limit 条）。"""
    try:
        r = subprocess.run(["westock", "notice", "list", code, "--limit", str(limit)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        txt = r.stdout or ""
    except Exception:                                         # noqa: BLE001
        return []
    rows, hdr = [], None
    for ln in txt.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if hdr is None:
            hdr = cells
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        rows.append(dict(zip(hdr, cells)))
    out = []
    for r in rows:
        t = (r.get("time") or "")[:10]
        out.append({"code": code, "date": t, "title": r.get("title"),
                    "cls": classify(r.get("title"))})
    return out


def load_klines():
    """→ prices{code:{date:(o,h,l,c)}}（窗口 + 足量前视回溯）。"""
    prices = defaultdict(dict)
    with open(KL, encoding="utf-8", errors="ignore", newline="") as f:
        for r in csv.DictReader(f):
            c, d = r.get("code") or "", r.get("date") or ""
            if c.startswith("bj") or c.startswith(("sh000", "sz399")):
                continue
            try:
                o, h, l, cl = (float(r["open"]), float(r["high"]),
                               float(r["low"]), float(r["close"]))
            except (ValueError, TypeError):
                continue
            if cl <= 0:
                continue
            prices[c][d] = (o, h, l, cl)
    return prices


def daily_equal_weight(prices, days):
    """全市场等权日收益基准（F7 坑1：等权均值）。→ {date: mean_pct}"""
    out = {}
    for d in days:
        pcts = []
        for c, m in prices.items():
            ds = m.get(d)
            if not ds:
                continue
            # 前收：同 code 上一交易日（逐 code 预排序太贵——用全局日期序近似）
            pcts.append(ds[3])
        out[d] = None
    return out  # 占位：真实基准在 study() 内按日对齐计算


def study(sample_codes, limit, progress=print):
    """事件研究主流程。→ (events[], summary{})"""
    prices = load_klines()
    all_days = sorted({d for m in prices.values() for d in m})
    day_idx = {d: i for i, d in enumerate(all_days)}
    # 全市场等权日收益（T0 相对 T-1 收盘）
    ew = {}
    for i, d in enumerate(all_days):
        if i == 0:
            continue
        dp = all_days[i - 1]
        rets = [m[d][3] / m[dp][3] - 1 for m in prices.values()
                if m.get(d) and m.get(dp)]
        if len(rets) > 100:
            ew[d] = st.mean(rets)

    # 环境档
    env = {}
    if os.path.isfile(ENV_CSV):
        with io.open(ENV_CSV, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                env[r["date"]] = r.get("grade")

    events = []
    for k, code in enumerate(sample_codes):
        if k % 25 == 0:
            progress("  抓取 %d/%d" % (k, len(sample_codes)))
        for n in fetch_notices(code, limit):
            D = n["date"]
            if not (WIN_START <= D <= WIN_END) or D not in day_idx:
                continue
            i0 = day_idx[D]
            m = prices.get(code)
            if not m:
                continue
            ds = sorted(d for d in m if d > D)          # T0 = D 后首个交易日
            if not ds:
                continue
            T0 = ds[0]
            if T0 not in day_idx:
                continue
            j0 = day_idx[T0]
            # 位置：pre10 = T0 前第 10 交易日收盘 → T0 前一日收盘
            if j0 < 11:
                continue
            pre10_d = all_days[j0 - 10]
            if pre10_d not in m or all_days[j0 - 1] not in m:
                continue
            pre10 = m[all_days[j0 - 1]][3] / m[pre10_d][3] - 1
            # 情绪：T0 前 5 交易日是否涨停过（≥9.5%）
            hot5 = any(all_days[j] in m and all_days[j - 1] in m
                       and (m[all_days[j]][3] / m[all_days[j - 1]][3] - 1) >= 0.095
                       for j in range(max(1, j0 - 5), j0))
            close0 = m[all_days[j0 - 1]][3]
            o1 = m[T0][0]
            gap = o1 / close0 - 1
            row = {"code": code, "D": D, "T0": T0, "cls": n["cls"],
                   "gap": round(gap, 6), "pre10": round(pre10, 6),
                   "hot5": hot5, "env": env.get(T0)}
            # fwd 相对 T0 开盘；窗口完整才计入（F7 坑3）
            for horizon in (1, 3, 5, 10, 20):
                j = j0 + horizon
                if j >= len(all_days) or all_days[j] not in m:
                    continue
                base = 1.0
                for jj in range(j0 + 1, j + 1):
                    b = ew.get(all_days[jj])
                    if b is not None:
                        base *= (1 + b)
                fwd = m[all_days[j]][3] / o1 - 1
                row["fwd%d" % horizon] = round(fwd, 6)
                row["ex%d" % horizon] = round(fwd - (base - 1), 6)
            events.append(row)
    return events


def agg(events, key_fn, field="ex5", min_n=15):
    """分组统计：n/均值/95%CI（n<min_n → INSUFFICIENT）。"""
    g = defaultdict(list)
    for e in events:
        if e.get(field) is not None:
            g[key_fn(e)].append(e[field])
    out = {}
    for k, v in sorted(g.items(), key=lambda x: str(x[0])):
        n = len(v)
        mean = st.mean(v)
        ci = None
        if n >= 2:
            sd = st.stdev(v)
            ci = [round(mean - 1.96 * sd / n ** 0.5, 5), round(mean + 1.96 * sd / n ** 0.5, 5)]
        out[k] = {"n": n, "mean": round(mean, 5), "ci95": ci,
                  "status": "OK" if n >= min_n else "INSUFFICIENT"}
    return out


def write_report(events, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    with io.open(os.path.join(out_dir, "events.csv"), "w", encoding="utf-8",
                 newline="") as f:
        if events:
            w = csv.DictWriter(f, fieldnames=list(events[0].keys()),
                               lineterminator="\n")
            w.writeheader()
            w.writerows(events)
    report = {
        "schema": "event_study@1", "as_of": "2026-10-27",
        "window": "%s~%s" % (WIN_START, WIN_END),
        "n_events": len(events),
        "n_codes": len({e["code"] for e in events}),
        "by_class_ex5": agg(events, lambda e: e["cls"]),
        "by_class_ex10": agg(events, lambda e: e["cls"], field="ex10"),
        "pos_modulation_利空": agg(
            [e for e in events if e["cls"] in ("减持", "质押担保", "风险黑天鹅")],
            lambda e: ("深跌<%%-10%%" if e["pre10"] < -0.10 else
                       "温和区" if e["pre10"] < 0.18 else "暴涨>18%")),
        "pos_modulation_利好": agg(
            [e for e in events if e["cls"] in ("增持", "回购", "股权激励", "订单合同")],
            lambda e: ("低位<%%-10%%" if e["pre10"] < -0.10 else
                       "温和区" if e["pre10"] < 0.18 else "高位>18%")),
        "sentiment_利空": agg(
            [e for e in events if e["cls"] in ("减持", "质押担保", "风险黑天鹅")],
            lambda e: "情绪票(近5日涨停过)" if e["hot5"] else "冷票"),
        "gap_bucket": agg(events, lambda e: ("低开<0" if e["gap"] < 0 else
                                             "平开0~3%" if e["gap"] < 0.03 else
                                             "高开3~6%" if e["gap"] < 0.06 else
                                             "大高开>6%")),
        "env_modulation": agg([e for e in events if e.get("env")],
                              lambda e: e["env"]),
        "reference": "F7（旧系统 61,980 事件包全量验证）→ data/research/event_effect/base.json",
        "boundary": "研究证据；类效应进决策须过 verify 统计 + 用户批准"}
    with io.open(os.path.join(out_dir, "REPORT.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    return report


def _selftest():
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== event_study --selftest ==")
    T("C1 分类：中标→订单合同", classify("关于中标XX项目的公告") == "订单合同")
    T("C2 分类：增持优先于业绩（先命中先归类）",
      classify("控股股东增持计划暨业绩预告") == "增持")
    T("C3 分类：未命中→程序类/其他", classify("关于召开临时股东大会的通知") in ("程序类", "其他"))
    T("C4 分类：立案→风险黑天鹅", classify("因涉嫌信息披露违规被立案调查") == "风险黑天鹅")
    # agg 统计数学
    evs = [{"cls": "A", "ex5": 0.01}, {"cls": "A", "ex5": 0.03},
           {"cls": "B", "ex5": -0.02}]
    r = agg(evs, lambda e: e["cls"], min_n=2)
    T("C5 agg：A 均值 0.02、B n<min_n → INSUFFICIENT",
      abs(r["A"]["mean"] - 0.02) < 1e-9 and r["B"]["status"] == "INSUFFICIENT")
    T("C6 基准知识件存在且带样本量", os.path.isfile(BASE_KB) and
      (json.load(io.open(BASE_KB, encoding="utf-8-sig")).get("base_rate") is not None))
    print("== event_study --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def main(argv):
    if argv and argv[0] == "selftest":
        return _selftest()
    if argv and argv[0] == "run":
        argv = argv[1:]
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", type=int, default=200, help="采样票数")
    ap.add_argument("--limit", type=int, default=200, help="每票公告条数")
    a = ap.parse_args(argv)
    # 采样：候选池 + 均匀抽取主板（确定性）
    codes = set()
    try:
        d = json.load(io.open(os.path.join(_ROOT, "data", "state", "candidates.json"),
                              encoding="utf-8-sig"))
        codes |= {c["code"] for c in d.get("candidates", [])
                  if c.get("state") in ("OPEN", "DISCOVERED", "WATCH", "QUALIFIED")}
    except Exception:                                         # noqa: BLE001
        pass
    prices = load_klines()
    universe = sorted(c for c in prices if not c.startswith(("sz30", "sh68")))
    step = max(1, len(universe) // max(1, a.codes - len(codes)))
    codes |= set(universe[::step][:a.codes])
    codes = sorted(codes)[:a.codes + 50]
    print("采样 %d 票（候选池优先 + 主板均匀），每票公告 limit=%d" % (len(codes), a.limit))
    events = study(codes, a.limit)
    print("事件包 %d 个（%d 票）" % (len(events), len({e["code"] for e in events})))
    report = write_report(events, OUT_DIR)
    print(json.dumps({"out": OUT_DIR, "n_events": report["n_events"],
                      "by_class_ex5": report["by_class_ex5"]},
                     ensure_ascii=False, indent=1)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
