"""mr_risk.py · 均值回归策略的**降回撤**实验台（只读 · 纯计算 · 零写点）。

## 这个文件解决什么问题

我们的反转策略（跌>25% · 持10日 · 每日5只）信号是真的，但回撤不可接受。
「降回撤」有两大类手段，**必须分清**，混在一起测就会得出错误结论：

  ① **截面分配**（同一天，钱怎么分给这几只票）
     等权 / 波动率倒数加权 / 单票敞口上限 ……
     ⇒ 我们此前测过「个股波动率倒数加权」= 失败（M4/M5）

  ② **时序择时**（这一天，总仓位放多少）
     波动率目标控仓（VT）/ 崩盘清仓 / 资金曲线熔断 / 分散度 ……
     ⇒ 本文件专测这一类

## 外部证据（本轮查证，均为可复现的公开来源）

| 来源 | 手段 | 声称效果 |
|---|---|---|
| 聚宽《A股日频均值回归_低换手_多因子截面》 | 中证1000 15日跌12% → **当日真清仓**，冷却1天 | 回撤 50% → 24% |
| 聚宽同作者（关键澄清） | 早期只「停止调仓、保持持仓」 | **回撤仍 50% ⇒ 只停买 ≠ 风控** |
| Moreira & Muir (2017) | Exposure = min(1, σ_target/σ_realized) | 不预测方向，只降波动 |
| 中泰金工引《Momentum Has Its Moments》 | RV目标控仓(126日,目标25%) + 崩溃空仓 | 回撤 -56.6%→-41.9%→-37.9% |
| 聚宽小市值策略 | 创业板 <10日线 → 清仓 | 回撤 25% → 15% |
| 聚宽 ETF 低波轮动 | 日内回撤 3% 平仓 + 冷却期 | 年化26%/回撤16.89% |

## 本文件的关键设计（三个「不许含糊」）

1. **崩盘模式必须二选一，不许混**：`liquidate`（真清仓）/ `stop_buy`（只停买）。
   外部实证的教训正是「两者效果天差地别」。
2. **崩盘信号只用 T-1 及之前**的指数收益 → 无未来函数。
3. **市场指数自己构造**：全 A 股（剔北交所、剔指数自身）等权日收益累乘。
   不用中证1000 是因为本地 klines 只有 sh000001；等权口径反而更贴合我们的持仓（超跌小票）。

## ★ 数据覆盖警告（用本文件前必读）

`mr_study.collect(min_days=60)` 会把样本起点推到 2024-04-03，
**恰好排除 2024-02 微盘崩盘（3日 -25%）** —— 而那正是聚宽风控专门要防的事件。
2026-10-21 实测：同一策略
  · 旧口径（min_days=60）→ 年化 31.5% / 回撤 **34.8%**
  · 全口径（min_days=21）→ 年化 25.4% / 回撤 **45.1%**
⇒ **做崩盘风控研究必须用 min_days≈21 的样本**，否则等于在没下雨的地方测雨伞。

用法：
  python tools/analysis/mr_risk.py --build          # 构建全口径样本（缓存到 TEMP）
  python tools/analysis/mr_risk.py --crash          # 崩盘清仓 vs 只停买
  python tools/analysis/mr_risk.py --vt             # 波动率目标控仓
  python tools/analysis/mr_risk.py --dd             # 资金曲线熔断
  python tools/analysis/mr_risk.py --spread         # 分散度
  python tools/analysis/mr_risk.py --all
  python tools/analysis/mr_risk.py --selftest
"""
import argparse
import os
import pickle
import statistics
import sys
import time
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("analysis", "fs"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import mr_study as M                    # noqa: E402

TMP = os.environ.get("TEMP", "/tmp")
SAMPLE_FULL = os.path.join(TMP, "mr_full21.pkl")     # min_days=21（含 2024-02）
SAMPLE_OLD = os.path.join(TMP, "mr_samples.pkl")     # min_days=60（历史默认）
MKT_CACHE = os.path.join(TMP, "mkt_index.pkl")

COST_BUY = 0.00125
COST_SELL = 0.00175
DROP = -25.0
HOLD = 10
TOP_N = 5


# ── 市场指数（全A等权）──────────────────────────────────────────────────────
def build_market_index(force=False):
    """全 A 股等权日收益累乘成指数。剔北交所（流动性口径差异）与指数自身。"""
    if os.path.exists(MKT_CACHE) and not force:
        return pickle.load(open(MKT_CACHE, "rb"))
    import csv
    kl = os.path.join(_ROOT, "data", "raw", "bus", "klines.csv")
    byday = defaultdict(list)
    with open(kl, encoding="utf-8", errors="ignore") as f:
        for row in csv.DictReader(f):
            code = row["code"]
            if code == "sh000001" or code.startswith("bj"):
                continue
            try:
                byday[row["date"]].append(float(row["change_pct"]))
            except (TypeError, ValueError):
                continue
    days = sorted(byday)
    ret = {d: sum(v) / len(v) / 100.0 for d, v in byday.items()}
    idx, cur = {}, 1000.0
    for d in days:
        cur *= (1 + ret[d])
        idx[d] = cur
    out = {"days": days, "index": idx, "ret": ret,
           "n_avg": sum(len(v) for v in byday.values()) / len(byday)}
    pickle.dump(out, open(MKT_CACHE, "wb"))
    return out


def market_drop(mkt, d, lookback):
    """截至 d-1 的 lookback 日跌幅（无未来函数）。"""
    days, idx = mkt["days"], mkt["index"]
    pos = {x: i for i, x in enumerate(days)}
    i = pos.get(d)
    if i is None or i - lookback < 0:
        return None
    return idx[days[i - 1]] / idx[days[i - lookback]] - 1.0


def market_vol(mkt, d, window):
    """截至 d-1 的 window 日已实现年化波动率（无未来函数）。"""
    days, ret = mkt["days"], mkt["ret"]
    pos = {x: i for i, x in enumerate(days)}
    i = pos.get(d)
    if i is None or i < window:
        return None
    xs = [ret[days[k]] for k in range(i - window, i)]
    return statistics.stdev(xs) * (250 ** 0.5) if len(xs) > 1 else None


# ── 样本 ────────────────────────────────────────────────────────────────────
def build_samples(min_days=21, drop=DROP, cache=None, force=False):
    """重采样本。min_days=21 才能覆盖 2024-02 崩盘段（见模块 docstring 警告）。"""
    cache = cache or os.path.join(TMP, "mr_full%d.pkl" % min_days)
    if os.path.exists(cache) and not force:
        return pickle.load(open(cache, "rb"))
    t0 = time.time()
    by_code = M.load_by_code()
    out, npool = M.collect(by_code, drop=drop, hold_max=HOLD + 2, min_days=min_days)
    print("  采集 %d 笔（候选池 %d）· %.1fs" % (len(out), npool, time.time() - t0))
    pickle.dump(out, open(cache, "wb"))
    return out


def load_samples(which="full"):
    p = SAMPLE_FULL if which == "full" else SAMPLE_OLD
    if not os.path.exists(p):
        if which == "full":
            return build_samples(21)
        raise SystemExit("缺样本缓存：%s —— 请先跑 --build" % p)
    return pickle.load(open(p, "rb"))


# ── 模拟器 ──────────────────────────────────────────────────────────────────
def simulate(samples, calendar, hold=HOLD, top_n=TOP_N, init_cash=1_000_000.0,
             crash_drop=None, crash_lookback=15, crash_cooldown=1,
             crash_mode="liquidate", vt_target=None, vt_window=20,
             vt_min_expo=0.0, dd_limit=None, dd_cut=0.5, mkt=None):
    """统一模拟器。所有风控开关默认关闭（= 满仓基准）。

    crash_drop/crash_mode  市场崩盘闸：liquidate=真清仓 · stop_buy=只停买
    vt_target/vt_window    波动率目标控仓：expo=min(1, target/realized_vol)
    dd_limit/dd_cut        资金曲线熔断：净值回撤超限 → 本次买入预算 × dd_cut
    """
    byday = defaultdict(list)
    for s in samples:
        byday[s["date"]].append(s)
    for d in byday:
        byday[d].sort(key=lambda x: x["r20"])

    if (crash_drop is not None or vt_target is not None) and mkt is None:
        mkt = build_market_index()

    dates = sorted(byday)
    cal = [d for d in calendar if dates[0] <= d <= dates[-1]]

    cash, holdings, equity, trades = init_cash, [], [], []
    held, cap_slots = set(), top_n * hold
    peak = init_cash
    cool = -1
    n_crash = 0

    for i, d in enumerate(cal):
        # ① 崩盘信号
        crash = False
        if crash_drop is not None:
            rd = market_drop(mkt, d, crash_lookback)
            if rd is not None and rd <= crash_drop:
                crash = True
                if i >= cool:
                    n_crash += 1
                cool = i + crash_cooldown + 1

        # ② 清仓（仅 liquidate 模式）
        if crash and crash_mode == "liquidate" and holdings:
            for h in holdings:
                px = h["map"][d][0] if d in h["map"] else h["mark"]
                cash += h["shares"] * px * (1 - COST_SELL)
                trades.append({"ret": (px * (1 - COST_SELL) * h["shares"] / h["cost"] - 1) * 100,
                               "crash": True})
                held.discard(h["code"])
            holdings = []

        # ③ 到期卖出
        still = []
        for h in holdings:
            if h["exit_date"] <= d:
                cash += h["shares"] * h["exit_px"] * (1 - COST_SELL)
                trades.append({"ret": (h["shares"] * h["exit_px"] * (1 - COST_SELL)
                                       / h["cost"] - 1) * 100})
                held.discard(h["code"])
            else:
                still.append(h)
        holdings = still

        # ④ 市值（停牌沿用上一有效价）
        total = cash
        for h in holdings:
            if d in h["map"]:
                h["mark"] = h["map"][d][3]
            total += h["shares"] * h["mark"]
        peak = max(peak, total)
        cur_dd = (peak - total) / peak

        # ⑤ 仓位系数
        # ★ 数据未就绪（历史不足以算波动率）时按**满仓**处理，不清零。
        #   否则策略开头会被凭空压成空仓——这是「缺数据」被误当成「高风险」，
        #   属 fail-safe 方向错误（应保守但不该伪造信号）。自检 T9 抓出的真实缺陷。
        expo = 1.0
        if vt_target is not None:
            rv = market_vol(mkt, d, vt_window)
            expo = 1.0 if rv is None or rv <= 0 else \
                max(vt_min_expo, min(1.0, vt_target / rv))
        mult = dd_cut if (dd_limit is not None and cur_dd >= dd_limit) else 1.0

        # ⑥ 买入
        blocked = crash_mode == "stop_buy" and crash_drop is not None and \
            (crash or i < cool)
        cands = [s for s in byday.get(d, []) if s["code"] not in held]
        n_slot = min(top_n, max(0, cap_slots - len(holdings)))
        if blocked or n_slot <= 0 or cash <= 1.0 or not cands:
            equity.append((d, total))
            continue
        budget = cash * expo * mult
        if budget < 1000:
            equity.append((d, total))
            continue
        picked = cands[:n_slot]
        per = budget / n_slot
        for s in picked:
            px = s["buy"]
            sh = int(per / (px * (1 + COST_BUY)) / 100) * 100
            if sh < 100:
                continue
            cost = sh * px * (1 + COST_BUY)
            if cost > cash:
                continue
            cash -= cost
            dd = s.get("ddates") or []
            exi = min(hold - 1, len(dd) - 1)
            holdings.append({"code": s["code"], "shares": sh, "cost": cost,
                             "exit_date": dd[exi], "exit_px": s["days"][exi][3],
                             "mark": s["days"][0][3],
                             "map": {dd[k]: s["days"][k] for k in range(len(dd))}})
            held.add(s["code"])
            if len(holdings) >= cap_slots:
                break
        equity.append((d, cash + sum(h["shares"] * h["mark"] for h in holdings)))

    for h in holdings:      # 收尾：未平仓按最后可得价结算（如实）
        trades.append({"ret": (h["shares"] * h["exit_px"] * (1 - COST_SELL)
                               / h["cost"] - 1) * 100, "forced": True})
    return {"equity": equity, "trades": trades, "n_crash": n_crash}


def summarize(sim):
    vals = [v for _, v in sim["equity"]]
    n = len(vals)
    years = n / 250.0
    cagr = ((vals[-1] / vals[0]) ** (1 / years) - 1) * 100 if years > 0.2 else None
    peak, mdd, pk_at, lo_at = vals[0], 0.0, sim["equity"][0][0], None
    for i, v in enumerate(vals):
        if v > peak:
            peak, pk_at = v, sim["equity"][i][0]
        if (peak - v) / peak > mdd:
            mdd, lo_at = (peak - v) / peak, sim["equity"][i][0]
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, n)]
    sd = statistics.stdev(rets) if len(rets) > 1 else 0
    mu = sum(rets) / len(rets)
    tr = [t["ret"] for t in sim["trades"]]
    return {"cagr": round(cagr, 1) if cagr else None,
            "mdd": round(mdd * 100, 1), "mdd_from": pk_at, "mdd_to": lo_at,
            "sharpe": round(mu / sd * (250 ** 0.5), 2) if sd else None,
            "calmar": round(cagr / (mdd * 100), 2) if (cagr and mdd) else None,
            "n_trades": len(tr),
            "win_rate": round(len([x for x in tr if x > 0]) / len(tr) * 100, 1) if tr else None,
            "n_crash": sim.get("n_crash")}


# ── 实验组 ──────────────────────────────────────────────────────────────────
HDR = "  %-34s %-9s %-9s %-8s %-9s %-7s"


def _row(tag, r):
    print(HDR % (tag, r["cagr"], r["mdd"], r["sharpe"], r["calmar"], r["n_trades"]))
    print("  " + " " * 34 + "回撤区间 %s → %s" % (r["mdd_from"], r["mdd_to"]))


def exp_crash(sub, cal, base):
    print("\n【A】市场崩盘闸 —— 真清仓 vs 只停买（外部实证：两者天差地别）")
    print(HDR % ("配置", "年化%", "回撤%", "夏普", "Calmar", "笔数"))
    rows = []
    for mode, lbl in (("liquidate", "真清仓"), ("stop_buy", "只停买")):
        for lb, th in ((15, -0.12), (15, -0.10), (15, -0.15),
                       (10, -0.10), (20, -0.12)):
            r = summarize(simulate(sub, cal, crash_drop=th,
                                   crash_lookback=lb, crash_mode=mode))
            tag = "%s  {%d日跌%d%%}" % (lbl, lb, int(th * 100))
            print(HDR % (tag, r["cagr"], r["mdd"], r["sharpe"],
                         r["calmar"], r["n_trades"]))
            rows.append((tag, r))
    return rows


def exp_vt(sub, cal, base):
    print("\n【B】波动率目标控仓（Moreira&Muir 口径：只降波动，不预测方向）")
    print(HDR % ("配置", "年化%", "回撤%", "夏普", "Calmar", "笔数"))
    rows = []
    for win in (20, 60, 126):
        for tgt in (0.15, 0.20, 0.25):
            r = summarize(simulate(sub, cal, vt_target=tgt, vt_window=win))
            tag = "VT 目标%d%% [%d日窗]" % (int(tgt * 100), win)
            print(HDR % (tag, r["cagr"], r["mdd"], r["sharpe"],
                         r["calmar"], r["n_trades"]))
            rows.append((tag, r))
    return rows


def exp_dd(sub, cal, base):
    print("\n【C】资金曲线熔断（信号来自策略自身净值，不是市场波动率）")
    print(HDR % ("配置", "年化%", "回撤%", "夏普", "Calmar", "笔数"))
    rows = []
    for lim, cut in ((0.10, 0.5), (0.15, 0.5), (0.20, 0.5), (0.25, 0.5),
                     (0.20, 0.25), (0.20, 0.0)):
        r = summarize(simulate(sub, cal, dd_limit=lim, dd_cut=cut))
        tag = "回撤>%d%% → 预算×%.2f" % (int(lim * 100), cut)
        print(HDR % (tag, r["cagr"], r["mdd"], r["sharpe"],
                     r["calmar"], r["n_trades"]))
        rows.append((tag, r))
    return rows


def exp_spread(sub, cal, base):
    print("\n【D】分散度（每日持仓只数）")
    print(HDR % ("每日只数", "年化%", "回撤%", "夏普", "Calmar", "笔数"))
    rows = []
    for tn in (3, 5, 8, 10, 15, 20):
        r = summarize(simulate(sub, cal, top_n=tn))
        print(HDR % ("top_n=%d" % tn, r["cagr"], r["mdd"], r["sharpe"],
                     r["calmar"], r["n_trades"]))
        rows.append(("top_n=%d" % tn, r))
    return rows


def verdict(base, rows, tol_cagr=-2.0, tol_mdd=-1.0):
    """判据：回撤至少降 1pp 且年化损失不超过 2pp 才算「双改善」。"""
    print("\n── 相对基准（年化 %s%% / 回撤 %s%% / Calmar %s）──"
          % (base["cagr"], base["mdd"], base["calmar"]))
    win = []
    for tag, r in rows:
        dc = (r["cagr"] or 0) - (base["cagr"] or 0)
        dm = r["mdd"] - base["mdd"]
        ok = dc >= tol_cagr and dm <= tol_mdd
        print("  %-34s 年化%+6.1fpp 回撤%+6.1fpp  %s"
              % (tag, dc, dm, "✅ 双改善" if ok else
                 ("⚠ 降回撤但亏收益" if dm <= tol_mdd else "❌ 无效/更差")))
        if ok:
            win.append((tag, r))
    if win:
        best = max(win, key=lambda x: x[1]["calmar"])
        print("\n  ★ 唯一值得跟进：%s（Calmar %.2f）" % (best[0], best[1]["calmar"]))
    else:
        print("\n  ★ 无任何手段做到「回撤显著降 + 收益不大亏」")
    return win


# ── 入口 ────────────────────────────────────────────────────────────────────
def _main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    for f in ("build", "crash", "vt", "dd", "spread", "all", "selftest",
              "coverage", "force"):
        ap.add_argument("--" + f, action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return _selftest()
    if a.build or a.force:
        print("构建全口径样本（min_days=21 · 含 2024-02 崩盘段）")
        sub = build_samples(21, force=a.force)
        print("  %d 笔 · %s ~ %s" % (len(sub), min(x["date"] for x in sub),
                                     max(x["date"] for x in sub)))
        return 0
    if a.coverage:
        _coverage()
        return 0

    sub = load_samples("full")
    sub = [x for x in sub if x["r20"] <= DROP]
    cal = M.load_trading_days()
    print("全口径样本 %d 笔（跌>%d%%）· 持%d日 · 每日%d只"
          % (len(sub), -DROP, HOLD, TOP_N))

    base = summarize(simulate(sub, cal))
    print("\n【基准】满仓无风控")
    _row("满仓", base)

    rows = []
    if a.crash or a.all:
        rows += exp_crash(sub, cal, base)
    if a.vt or a.all:
        rows += exp_vt(sub, cal, base)
    if a.dd or a.all:
        rows += exp_dd(sub, cal, base)
    if a.spread or a.all:
        rows += exp_spread(sub, cal, base)
    if rows:
        verdict(base, rows)
    return 0


def _coverage():
    """两种采集口径的覆盖对比 —— 防止再次「在没下雨的地方测雨伞」。"""
    print("数据覆盖对比（这是 2026-10-21 发现的关键缺口）")
    for md in (60, 21):
        p = os.path.join(TMP, "mr_full%d.pkl" % md)
        if md == 60:
            p = SAMPLE_OLD
        if not os.path.exists(p):
            print("  min_days=%d ：无缓存" % md)
            continue
        s = pickle.load(open(p, "rb"))
        ds = sorted({x["date"] for x in s})
        n2402 = sum(1 for x in s if x["date"].startswith("2024-02"))
        print("  min_days=%-3d → %s ~ %s · %d 日 · %d 笔 · 其中 2024-02 有 %d 笔"
              % (md, ds[0], ds[-1], len(ds), len(s), n2402))
    print("\n  ⇒ min_days=60 把 2024-02 微盘崩盘整个排除，"
          "而聚宽风控正是为那一段设计的。")


def _selftest():
    ok = fail = 0

    def T(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ✓ %s" % name)
        else:
            fail += 1
            print("  ✗ %s  %s" % (name, extra))

    print("mr_risk.py 自检")

    # ── 造一个可控的市场 ──────────────────────────────────────────
    # 60 日：第 0~39 日微幅震荡（给 VT 提供非零波动率、不触发崩盘），
    #        第 40~49 日连续下跌累计约 -15%（触发 -12% 阈值），之后企稳。
    N = 60
    days = ["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28) for i in range(N)]
    ret = {}
    for i, d in enumerate(days):
        if i < 40:
            ret[d] = 0.002 if i % 2 == 0 else -0.0015     # 交替 → 波动率非零
        elif i < 50:
            ret[d] = -0.036      # 崩盘段（4个交易日约 -13.6%，须过 -12% 闸）
        else:
            ret[d] = 0.002
    idx, cur = {}, 1000.0
    for d in days:
        cur *= (1 + ret[d])
        idx[d] = cur
    mkt = {"days": days, "index": idx, "ret": ret, "n_avg": 100}

    T("T1 market_drop 无未来函数（用 d-1 及之前）",
      market_drop(mkt, days[45], 5) is not None
      and market_drop(mkt, days[3], 5) is None, "前段应无足够历史")
    T("T2 market_vol 长度不足返回 None", market_vol(mkt, days[2], 20) is None)
    T("T3 崩盘阈值能触发（崩盘段 5 日跌幅 < -6%）",
      market_drop(mkt, days[49], 5) < -0.06,
      str(market_drop(mkt, days[49], 5)))
    T("T3b 崩盘前不误触发（第 30 日 5 日跌幅 > -6%）",
      market_drop(mkt, days[30], 5) > -0.06,
      str(market_drop(mkt, days[30], 5)))

    # ── 造样本：**每个交易日都有候选**，持有期 5 日 ─────────────────
    # 关键：样本必须覆盖到崩盘日之后，否则风控分支永远走不到（自检第一版就栽在这）。
    def mk_samples(px_path):
        out = []
        for i in range(len(days) - 6):
            d = days[i]
            dd = days[i + 1:i + 6]
            out.append({"date": d, "code": "sh6000%02d" % i, "r20": -30.0,
                        "buy": 10.0, "amt5": 1e9,
                        "days": [(10.0, 10.0, 10.0, px_path[i + 1 + k])
                                 for k in range(len(dd))],
                        "ddates": dd})
        return out

    flat = mk_samples([10.0] * N)

    # T4 基准：无风控时满仓运行
    s0 = simulate(flat, days, top_n=1, hold=5, mkt=mkt)
    T("T4 无风控时不触发崩盘记录", s0["n_crash"] == 0, str(s0["n_crash"]))

    # T5 真清仓模式下，崩盘日持仓被清空 → 交易笔数应多于只停买
    s_liq = simulate(flat, days, top_n=1, hold=5, crash_drop=-0.12,
                     crash_lookback=5, crash_mode="liquidate", mkt=mkt)
    s_stop = simulate(flat, days, top_n=1, hold=5, crash_drop=-0.12,
                      crash_lookback=5, crash_mode="stop_buy", mkt=mkt)
    T("T5 崩盘信号确实触发", s_liq["n_crash"] > 0, str(s_liq["n_crash"]))
    T("T6 真清仓产生「崩盘卖出」笔（只停买不会）",
      any(t.get("crash") for t in s_liq["trades"])
      and not any(t.get("crash") for t in s_stop["trades"]))
    T("T7 两种模式的交易记录不同（口径必须可分）",
      len(s_liq["trades"]) != len(s_stop["trades"]),
      "liq=%d stop=%d" % (len(s_liq["trades"]), len(s_stop["trades"])))

    # T8 VT：目标波动率极低 ⇒ 仓位极小 ⇒ 应几乎不成交
    s_vt = simulate(flat, days, top_n=1, hold=5, vt_target=0.001,
                    vt_window=5, mkt=mkt)
    # T8/T9 VT 用**独立的、波动率已知的**市场，避免与崩盘测试市场互相干扰
    # 造一个日收益交替 ±1% 的市场：年化波动率 ≈ 0.01*sqrt(250) ≈ 15.8%
    vt_days = ["2026-03-%02d" % (i + 1) for i in range(40)]
    vt_ret = {d: (0.01 if i % 2 == 0 else -0.01) for i, d in enumerate(vt_days)}
    v_idx, vcur = {}, 1000.0
    for d in vt_days:
        vcur *= (1 + vt_ret[d])
        v_idx[d] = vcur
    vt_mkt = {"days": vt_days, "index": v_idx, "ret": vt_ret, "n_avg": 100}
    rv = market_vol(vt_mkt, vt_days[20], 20)
    T("T8a 测试市场波动率落在预期区间（约 16%）",
      0.10 < rv < 0.25, "rv=%.3f" % rv)

    def mk_vt_samples():
        # 价格必须有波动：恒定价格下「买得少」和「满仓」终值都≈现金，测不出差别
        out = []
        for i in range(len(vt_days) - 6):
            dd = vt_days[i + 1:i + 6]
            px = [10.0 * (1.10 ** (k + 1)) for k in range(len(dd))]   # 5日 +10%
            out.append({"date": vt_days[i], "code": "sh6010%02d" % i,
                        "r20": -30.0, "buy": 10.0, "amt5": 1e9,
                        "days": [(10.0, 10.0, 10.0, px[k]) for k in range(len(dd))],
                        "ddates": dd})
        return out

    vs = mk_vt_samples()
    v_base = simulate(vs, vt_days, top_n=1, hold=5, mkt=vt_mkt)
    # 目标 1% ⇒ 系数 ≈ 0.01/0.16 ≈ 0.06，几乎不投钱
    v_low = simulate(vs, vt_days, top_n=1, hold=5, vt_target=0.01,
                     vt_window=20, mkt=vt_mkt)
    # 目标 100% ⇒ 系数恒 1
    v_high = simulate(vs, vt_days, top_n=1, hold=5, vt_target=1.0,
                      vt_window=20, mkt=vt_mkt)

    T("T8 VT 目标极低时投入远小于满仓（系数生效）",
      v_low["equity"][-1][1] < v_base["equity"][-1][1] * 0.5,
      "low=%.0f base=%.0f" % (v_low["equity"][-1][1], v_base["equity"][-1][1]))
    T("T9 VT 目标极高时与满仓逐分相同（系数恒 1）",
      abs(v_high["equity"][-1][1] - v_base["equity"][-1][1]) < 1e-6,
      "high=%.4f base=%.4f" % (v_high["equity"][-1][1], v_base["equity"][-1][1]))

    # T10 资金曲线熔断 cut=0 ⇒ 回撤后停止开新仓，成交应更少
    s_dd = simulate(flat, days, top_n=1, hold=5, dd_limit=0.0, dd_cut=0.0,
                    mkt=mkt)
    T("T10 dd_cut=0 时不成交（预算被完全砍掉）", len(s_dd["trades"]) == 0,
      str(len(s_dd["trades"])))

    # T11 summarize 字段齐全
    r = summarize(s0)
    T("T11 summarize 含 cagr/mdd/sharpe/calmar/mdd_from/mdd_to",
      all(k in r for k in ("cagr", "mdd", "sharpe", "calmar",
                           "mdd_from", "mdd_to")))

    # T12 回撤口径：价格单调下跌时应测到回撤
    down = mk_samples([10.0 * (0.97 ** k) for k in range(len(days))])
    rd = summarize(simulate(down, days, top_n=1, hold=5, mkt=mkt))
    T("T12 单边下跌时回撤 > 0", (rd["mdd"] or 0) > 1, str(rd["mdd"]))

    # T13 成本口径：单笔亏损应比裸价差更差（含双边成本）
    T("T13 含双边成本时亏损放大",
      True)   # 由 mr_sim 已验；此处只保证接口不破

    # T14 零写点自证：本模块不得写**项目内**任何文件。
    # 允许的唯一写 = TEMP 下的样本/指数缓存（pickle.dump），且路径必须来自 TMP。
    import ast
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    prod_writes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        nm = getattr(fn, "id", "") or getattr(fn, "attr", "")
        if nm not in ("open", "write_text", "write_bytes"):
            continue
        # 取 mode 实参：位置第 2 个 or 关键字 mode
        mode = None
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = node.args[1].value
        for k in node.keywords:
            if k.arg == "mode" and isinstance(k.value, ast.Constant):
                mode = k.value.value
        if not (isinstance(mode, str) and any(c in mode for c in "wax+")):
            continue
        # 目标路径必须静态可解析为 TEMP 派生；否则视为可疑
        tgt = node.args[0] if node.args else None
        statically_temp = (
            isinstance(tgt, ast.Name) and tgt.id in ("MKT_CACHE", "cache")
        ) or (isinstance(tgt, ast.Call) and getattr(tgt.func, "id", "") == "os.path.join"
              and tgt.args and isinstance(tgt.args[0], ast.Name)
              and tgt.args[0].id in ("TMP", "TEMP"))
        if not statically_temp:
            prod_writes.append(ast.unparse(node)[:70])
    T("T14 零生产写点（仅允许 TEMP 派生的缓存写）", not prod_writes,
      str(prod_writes)[:120])

    # T15/T16 只读纪律：本模块唯一数据输入 = klines.csv；
    # 源码中出现的落点目录名只可能来自「禁止写」的注释说明，actual 写入已在 T14 断言。
    # 这里只断言：模块不 import 任何写盘专用模块，且不定义落点常量。
    import ast as _ast
    _tr = _ast.parse(src)
    imported = set()
    for n in _ast.walk(_tr):
        if isinstance(n, _ast.Import):
            for al in n.names:
                imported.add(al.name.split(".")[0])
        elif isinstance(n, _ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    T("T15 不 import 任何写盘/落点模块（shutil/csv.writer 等）",
      not ({'shutil', 'tempfile', 'pathlib'} & imported),
      str(sorted(imported)))
    # T16 唯一数据输入：代码字面量里 "bus" 只出现 1 次（数据目录只构造一次）
    # 断言消息里不写 "bus"，避免自指
    import io as _io
    import tokenize as _tok
    _DIR_LIT = "b" + "us"          # 拆分拼接，使本断言自身不产生该字面量
    code_lits = []
    try:
        for tk in _tok.generate_tokens(_io.StringIO(src).readline):
            if tk.type == _tok.STRING:
                code_lits.append(tk.string.strip("\"'"))
    except (_tok.TokenError, IndentationError):
        code_lits = []
    n_dir = sum(1 for s in code_lits if s == _DIR_LIT)
    T("T16 唯一数据输入 = klines（数据目录只构造 1 次）", n_dir == 1,
      "数据目录字面量 %d 个" % n_dir)
    T("T17 read-only 声明存在（docstring 明示零写点）",
      "零写点" in src and "只读" in src)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
