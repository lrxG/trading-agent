"""mr_run.py · 均值回归信号工程化分析（只读 · 纯计算 · 零落盘生产区）。

从缓存的样本集出发，回答四个问题：
  Q1 持有期哪个最优（5/10/20/30/40 日）
  Q2 跌幅阈值怎么定（-10/-15/-20/-25/-30/-35%）
  Q3 止损要不要设、设多少（无/-8/-10/-15/-20%）
  Q4 组合层面（每日最多买 N 只、等权）→ 年化、最大回撤、月度路径

用法：
  python tools/analysis/mr_run.py --all
  python tools/analysis/mr_run.py --hold
"""
import os
import sys
import pickle

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("analysis", "fs"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import statistics                                    # noqa: E402
from collections import defaultdict                  # noqa: E402
import mr_study as M                                 # noqa: E402

CACHE = os.path.join(os.environ.get("TEMP", "/tmp"), "mr_samples.pkl")


def load_samples():
    if not os.path.isfile(CACHE):
        raise SystemExit("缺样本缓存，先跑采集：python -c \"import mr_study...\"")
    with open(CACHE, "rb") as f:
        return pickle.load(f)


def q1_hold(samples, drops=(-0.15, -0.25), holds=(5, 10, 20, 30, 40)):
    print("=" * 74)
    print("Q1 持有期优化")
    print("%-10s %-8s %-7s %-6s %-8s %-9s %-8s %-8s" % (
        "跌幅阈值", "持有", "n", "胜率", "平均净", "日级日均", "日级t", "最差"))
    print("-" * 74)
    out = []
    for dr in drops:
        sub = [s for s in samples if s["r20"] <= dr * 100]
        for h in holds:
            vals = [M.trade_ret(s, h) for s in sub]
            vals = [v for v in vals if v is not None]
            if len(vals) < 100:
                continue
            st = M.stat_block(vals)
            dt = M.daily_t(sub, h)
            print("%-10s %-8s %-7d %-6.1f %+8.2f%% %+9.3f%% %-8s %+8.2f%%" % (
                "%d%%" % (dr * 100), "%d日" % h, st["n"], st["win_rate"],
                st["avg"], dt["avg"] or 0, dt["t"], st["worst"]))
            out.append({"drop": dr, "hold": h, "stat": st, "daily": dt})
        print()
    return out


def q2_drop(samples, hold=20, drops=(10, 15, 20, 25, 30, 35, 40)):
    print("=" * 74)
    print("Q2 跌幅阈值（持有 %d 日）" % hold)
    print("%-8s %-7s %-7s %-6s %-9s %-9s %-8s" % (
        "跌幅", "n", "占比", "胜率", "平均净", "日级日均", "日级t"))
    print("-" * 74)
    tot = len(samples)
    out = []
    for d in drops:
        sub = [s for s in samples if s["r20"] <= -d]
        vals = [M.trade_ret(s, hold) for s in sub]
        vals = [v for v in vals if v is not None]
        if len(vals) < 100:
            continue
        st = M.stat_block(vals)
        dt = M.daily_t(sub, hold)
        print("%-8s %-7d %-6.2f%% %-6.1f %+9.2f%% %+9.3f%% %-8s" % (
            "跌>%d%%" % d, st["n"], st["n"] / tot * 100, st["win_rate"],
            st["avg"], dt["avg"] or 0, dt["t"]))
        out.append({"drop": -d, "stat": st, "daily": dt})
    print()
    return out


def q3_stop(samples, drop=-0.25, holds=(10, 20)):
    print("=" * 74)
    print("Q3 止损规则（跌幅>%d%%）" % (-drop * 100))
    print("%-8s %-8s %-7s %-6s %-9s %-9s %-8s %-8s" % (
        "持有", "止损", "n", "胜率", "平均净", "日级t", "最差", "盈亏比"))
    print("-" * 74)
    sub = [s for s in samples if s["r20"] <= drop * 100]
    out = []
    for h in holds:
        for sl, lab in ((None, "无"), (-0.08, "-8%"), (-0.10, "-10%"),
                        (-0.15, "-15%"), (-0.20, "-20%")):
            vals = [M.trade_ret(s, h, sl) for s in sub]
            vals = [v for v in vals if v is not None]
            if len(vals) < 100:
                continue
            st = M.stat_block(vals)
            dt = M.daily_t(sub, h, sl)
            print("%-8s %-8s %-7d %-6.1f %+9.2f%% %-9s %+8.2f%% %-8s" % (
                "%d日" % h, lab, st["n"], st["win_rate"], st["avg"],
                dt["t"], st["worst"], st["pf"]))
            out.append({"hold": h, "stop": sl, "stat": st, "daily": dt})
        print()
    return out


def q4_portfolio(samples, drop=-0.25, hold=20, stop=None,
                 top_ns=(3, 5, 10, 20), min_amts=(None, 5e7, 1e8)):
    print("=" * 74)
    print("Q4 组合层面（跌幅>%d%% · 持有%d日 · 止损%s）" % (
        -drop * 100, hold, "%d%%" % (stop * 100) if stop else "无"))
    print("%-6s %-10s %-8s %-10s %-9s %-8s %-10s" % (
        "每日只数", "成交额门槛", "交易日", "日均收益", "t", "累计", "最大回撤"))
    print("-" * 74)
    sub = [s for s in samples if s["r20"] <= drop * 100]
    out = []
    for tn in top_ns:
        for ma, lab in ((a, ("无" if a is None else "%.0f万" % (a / 1e4)))
                        for a in min_amts):
            pf = M.portfolio(sub, hold, top_n=tn, stop_loss=stop, min_amt=ma)
            if not pf.get("n_days"):
                continue
            print("%-6d %-10s %-8d %+9.2f%% %-9s %+9.1f%% %-10s" % (
                tn, lab, pf["n_days"], pf["avg_daily"], pf["t"],
                pf["eq_total"], "%.1f%%" % pf["mdd"]))
            out.append({"top_n": tn, "min_amt": ma, **pf})
        print()
    return out


def q5_monthly(samples, drop=-0.25, hold=20, stop=None, top_n=5, min_amt=None):
    """选定规则的月度路径（看连续亏损期）。"""
    sub = [s for s in samples if s["r20"] <= drop * 100]
    bym = defaultdict(list)
    byd = defaultdict(list)
    for s in sub:
        if min_amt is not None and s["amt5"] < min_amt:
            continue
        byd[s["date"]].append(s)
    for d, lst in byd.items():
        lst.sort(key=lambda x: x["r20"])
        rets = [M.trade_ret(s, hold, stop) for s in lst[:top_n]]
        rets = [r for r in rets if r is not None]
        if rets:
            bym[d[:7]].append(sum(rets) / len(rets))
    print("=" * 74)
    print("Q5 月度路径（跌>%d%% · 持%d日 · 每日%d只 · 止损%s）" % (
        -drop * 100, hold, top_n, "%d%%" % (stop * 100) if stop else "无"))
    pos = 0
    tot = []
    for k in sorted(bym):
        v = bym[k]
        a = sum(v) / len(v)
        tot.append(a)
        if a > 0:
            pos += 1
        flag = " ⚠" if a < -3 else ""
        print("  %s: %2d日 日均 %+7.2f%%%s" % (k, len(v), a, flag))
    print("  ⇒ 正月份 %d / %d · 月度最高 %+.2f%% · 最低 %+.2f%%" % (
        pos, len(bym), max(tot), min(tot)))
    # 最长连续亏损月
    run = mx = 0
    for a in tot:
        if a < 0:
            run += 1
            mx = max(mx, run)
        else:
            run = 0
    print("  ⇒ 最长连续亏损月数: %d" % mx)
    return bym


def _main(argv):
    if "--selftest" in argv:
        return M._selftest()
    s = load_samples()
    print("样本 %d 笔（跌幅 >10%%，2024-01~2026-10）\n" % len(s))
    which = [a for a in argv if a.startswith("--")]
    do_all = "--all" in which or not which
    if do_all or "--hold" in which:
        q1_hold(s)
    if do_all or "--drop" in which:
        q2_drop(s)
    if do_all or "--stop" in which:
        q3_stop(s)
    if do_all or "--port" in which:
        q4_portfolio(s)
    if do_all or "--monthly" in which:
        q5_monthly(s)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
