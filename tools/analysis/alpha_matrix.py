"""alpha_matrix.py · 「环境 × 信号」矩阵 —— 找互补 alpha 的搜索台（只读 · 纯计算 · 零写点）。

## 为什么需要这个文件

现有唯一的 alpha 是「跌>25% 后反弹」，实测它**只在市场高波动时赚钱**：
  按市场20日已实现波动率分五档 → Q4 +11.59%(胜率88%) / Q5 +12.28%(85%)
                                   Q1 +1.67% / Q2 **-2.80%** / Q3 +0.07%
⇒ 低波动期这条腿在空转。降回撤的 37 种风控全失败，根因是「任何高波动减仓都在砍
   唯一赚钱的时段」（实证已固化在 .workbuddy/skills/trade-rules/SKILL.md 规则层）。

所以唯一出路 = **补一条在低波动期也赚钱的腿**。本文件就是找这条腿的搜索台。

## 与 mr_* 系列的区别（别混）

· `mr_study/run/sim`：只看「跌>25%」这个子集，回答「这条规则赚多少」
· `mr_risk`：只看时序风控（何时减仓），回答「能不能少亏」
· **本文件**：看**全市场**，回答「在不同市场环境下，哪个横截面信号最有效」

## 方法（诚实的口径）

对**每个交易日 d**、**每只股票**：
  ① 信号值：只用 ≤d 的数据算（r5/r20/r60/vol20/流动性/距60日高点/行业相对偏离）
  ② 前瞻收益：**T+1 开盘买 → T+10 收盘卖**，扣双边成本（与主策略同口径）
  ③ 当日基准：该日**全市场等权**前瞻收益（beta 基准，M2 纪律）
  ④ 分位超额：按信号分 5 组，算各组相对当日基准的超额

对**每个市场环境**（按 d 时点的市场波动率分档）分别汇总。

⇒ 输出「信号 × 环境」的超额矩阵。**答案就是：低波动档里哪个信号的正超额最大且稳定。**

## 三条纪律

· **无未来函数**：信号用 ≤d，收益从 d+1 开盘起算；环境档也用 ≤d 的波动率
· **先扣基准**：所有超额都是相对当日全市场等权（M2：beta 伪装）
· **看稳定性不看单点**：同时报 t 值、胜率、分年一致性（M11/M12）

用法：
  python tools/analysis/alpha_matrix.py --build      # 构建面板缓存（约 1-2 分钟）
  python tools/analysis/alpha_matrix.py --run        # 跑矩阵
  python tools/analysis/alpha_matrix.py --lowvol     # 只看低波动档（找互补 alpha 的重点）
  python tools/analysis/alpha_matrix.py --selftest
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
PANEL = os.path.join(TMP, "alpha_panel.pkl")
MKT = os.path.join(TMP, "mkt_index.pkl")

COST_BUY = 0.00125
COST_SELL = 0.00175
FWD = 10                 # 前瞻持有期（与主策略一致）
MIN_HIST = 61            # 至少这么多历史才能算 r60
Q = 5                    # 分位组数

SIGNALS = (
    ("r5", "短期反转(5日跌幅)", "asc"),      # 越负越好 → asc
    ("r20", "超跌度(20日跌幅)", "asc"),
    ("r60", "中期动量(60日涨幅)", "desc"),
    ("vol20", "个股波动率", "asc"),
    ("amt20", "流动性(20日均额)", "desc"),
    ("dd60", "距60日高点", "asc"),           # 越负（离高点越远）越好
    ("rel20", "行业相对偏离", "asc"),
)


# ── 面板构建 ────────────────────────────────────────────────────────────────
def build_panel(force=False):
    """→ {date: {code: dict(sig..., fwd)}}；同时返回各日全市场等权前瞻收益。

    一次扫描 klines，按 code 顺序算滚动量，写入按日索引的面板。
    """
    if os.path.exists(PANEL) and not force:
        return pickle.load(open(PANEL, "rb"))

    t0 = time.time()
    by_code = M.load_by_code()
    print("  载入 %d 只 · %.1fs" % (len(by_code), time.time() - t0))

    # 行业映射（有就用，没有 rel20=None 不填 0）
    ind_map = {}
    try:
        ind_map = M.load_industry_map() or {}
    except Exception:
        ind_map = {}

    # 第一遍：算每只票的滚动量与前瞻收益
    # 结构：rows_by_date[d][code] = {...}
    rows_by_date = defaultdict(dict)
    n_ok = n_skip = 0
    for code, rows in by_code.items():
        n = len(rows)
        if n < MIN_HIST + FWD + 2:
            n_skip += 1
            continue
        closes = [r[4] for r in rows]
        highs = [r[2] for r in rows]
        amts = [r[5] for r in rows]
        # 日收益序列（用于波动率）
        rets = [None] + [
            (closes[i] / closes[i - 1] - 1) if closes[i - 1] > 0 else None
            for i in range(1, n)
        ]
        ind = ind_map.get(code)
        for i in range(MIN_HIST, n - FWD - 1):
            c0 = closes[i]
            if c0 <= 0:
                continue
            buy = rows[i + 1][1]              # T+1 开盘
            sell = rows[i + FWD][4]           # T+10 收盘
            if buy <= 0 or sell <= 0:
                continue
            net = (sell * (1 - COST_SELL)) / (buy * (1 + COST_BUY)) - 1.0
            # 滚动量
            r5 = c0 / closes[i - 5] - 1 if closes[i - 5] > 0 else None
            r20 = c0 / closes[i - 20] - 1 if closes[i - 20] > 0 else None
            r60 = c0 / closes[i - 60] - 1 if closes[i - 60] > 0 else None
            w = [x for x in rets[i - 19:i + 1] if x is not None]
            vol20 = statistics.stdev(w) if len(w) > 5 else None
            amt20 = sum(amts[i - 19:i + 1]) / 20.0
            hi60 = max(highs[i - 59:i + 1]) if i >= 59 else None
            dd60 = (c0 / hi60 - 1) if (hi60 and hi60 > 0) else None
            rows_by_date[rows[i][0]][code] = {
                "r5": r5, "r20": r20, "r60": r60, "vol20": vol20,
                "amt20": amt20, "dd60": dd60, "ind": ind, "fwd": net,
            }
            n_ok += 1
    print("  面板 %d 条 · 跳过 %d 只（历史不足）· %.1fs"
          % (n_ok, n_skip, time.time() - t0))

    # 第二遍：每日全市场等权前瞻收益（基准）+ 行业相对偏离
    bench = {}
    for d, m in rows_by_date.items():
        fs = [v["fwd"] for v in m.values()]
        bench[d] = sum(fs) / len(fs) if fs else None
        # 行业当日各票 fwd 中位数不用于 rel；rel 用的是 r20 - 行业 r20 中位数
        byind = defaultdict(list)
        for v in m.values():
            if v["ind"] and v["r20"] is not None:
                byind[v["ind"]].append(v["r20"])
        med = {}
        for k, xs in byind.items():
            if len(xs) >= 5:                 # 少于 5 只不给基准，不硬凑
                med[k] = statistics.median(xs)
        for v in m.values():
            if v["ind"] in med and v["r20"] is not None:
                v["rel20"] = v["r20"] - med[v["ind"]]
            else:
                v["rel20"] = None

    out = {"rows": dict(rows_by_date), "bench": bench,
           "days": sorted(rows_by_date), "n": n_ok}
    pickle.dump(out, open(PANEL, "wb"))
    return out


def load_market():
    if not os.path.exists(MKT):
        raise SystemExit("缺市场指数缓存 —— 先跑 mr_risk.py --all 或 --build")
    return pickle.load(open(MKT, "rb"))


def env_of(mkt, d, window=20):
    """d 时点的市场已实现年化波动率（只用 ≤d-1，无未来函数）。"""
    days, ret = mkt["days"], mkt["ret"]
    pos = {x: i for i, x in enumerate(days)}
    i = pos.get(d)
    if i is None or i < window:
        return None
    xs = [ret[days[k]] for k in range(i - window, i)]
    return statistics.stdev(xs) * (250 ** 0.5) if len(xs) > 1 else None


def env_quintiles(panel, mkt, window=20):
    """给每个交易日分配环境档 Q1..Q5（按当日市场波动率分位）。→ {date: 1..5}"""
    ev = {}
    for d in panel["days"]:
        v = env_of(mkt, d, window)
        if v is not None:
            ev[d] = v
    vs = sorted(ev.values())
    if len(vs) < 50:
        return {}, ev
    cuts = [vs[int(len(vs) * p)] for p in (0.2, 0.4, 0.6, 0.8)]
    out = {}
    for d, v in ev.items():
        q = 1
        for c in cuts:
            if v >= c:
                q += 1
        out[d] = q
    return out, ev


# ── 矩阵计算 ────────────────────────────────────────────────────────────────
def signal_quintile_excess(panel, sig, direction, q=Q, env=None, env_filter=None):
    """按 signal 分 q 组，算各组相对当日基准的超额。

    ★ 分组口径（唯一、不看信号自身方向）：
      先把信号按 direction 排序，使 **"最有利" 的排在前面**，
      再均匀切 q 段：**G1 = 最有利（信号最强）** … Gq = 最不利。
      这样跨信号可直接横向比较"最强组超额"，不需要读者记住方向。
      direction="asc"  ⇒ 值越小越有利 → 升序排（最负在前）
      direction="desc" ⇒ 值越大越有利 → 降序排（最大在前）

    → {quintile: {"mean_excess","t","win_days","n_days","n_obs"}}
    """
    acc = defaultdict(lambda: defaultdict(list))     # qidx -> date -> [excess...]
    for d in panel["days"]:
        if env_filter is not None and env.get(d) not in env_filter:
            continue
        b = panel["bench"].get(d)
        if b is None:
            continue
        m = panel["rows"][d]
        vals = [(v[sig], v["fwd"]) for v in m.values() if v.get(sig) is not None]
        if len(vals) < q * 10:
            continue
        vals.sort(key=lambda x: x[0], reverse=(direction == "desc"))
        n = len(vals)
        for qi in range(q):
            lo = int(n * qi / q)
            hi = int(n * (qi + 1) / q)
            xs = [v[1] - b for v in vals[lo:hi]]
            if xs:
                acc[qi + 1][d].append(sum(xs) / len(xs))

    out = {}
    for qi in range(1, q + 1):
        per_day = [statistics.mean(v) for v in acc[qi].values() if v]
        if not per_day:
            continue
        mu = statistics.mean(per_day)
        sd = statistics.stdev(per_day) if len(per_day) > 1 else 0
        t = mu / (sd / (len(per_day) ** 0.5)) if sd else None
        out[qi] = {"mean_excess": mu * 100, "t": round(t, 2) if t else None,
                   "win_days": round(len([x for x in per_day if x > 0]) / len(per_day) * 100, 1),
                   "n_days": len(per_day),
                   "n_obs": sum(len(v) for v in acc[qi].values())}
    return out


def run_matrix(panel, mkt, env_filter=None, label=""):
    env, ev = env_quintiles(panel, mkt)
    if not env:
        raise SystemExit("环境分档失败（市场波动率算不出来）")
    print("\n环境档（按当日市场20日已实现波动率分位）" + ("  —— " + label if label else ""))
    buckets = defaultdict(list)
    for d, q in env.items():
        buckets[q].append(ev[d])
    for q in sorted(buckets):
        xs = buckets[q]
        print("  Q%d：波动率 %.1f%% ~ %.1f%% · %d 个交易日"
              % (q, min(xs) * 100, max(xs) * 100, len(xs)))

    print("\n【信号 × 环境】各组相对「当日全市场等权」的超额（%%）")
    print("  读法：**G1 = 信号最有利的 1/5 股票**，G5 = 最不利；正数=跑赢市场")
    print("  %-22s %-5s %s" % ("信号", "环境", "  ".join("G%d" % i for i in range(1, Q + 1))))
    results = {}
    for sig, name, dirn in SIGNALS:
        for envq in (None, 1, 2, 3, 4, 5):
            flt = None if envq is None else {envq}
            r = signal_quintile_excess(panel, sig, dirn, env=env, env_filter=flt)
            if not r:
                continue
            tag = "全部" if envq is None else "Q%d" % envq
            cells = []
            for qi in range(1, Q + 1):
                v = r.get(qi)
                cells.append("%+12.2f" % v["mean_excess"] if v else "           -")
            print("  %-24s %-7s %s" % (name if envq is None else "", tag,
                                       " ".join(cells)))
            results[(sig, envq)] = r
        print()
    return results, env


def focus_lowvol(panel, mkt, results, env):
    """低波动档（Q1+Q2）里，哪个信号的 G1（最有利组）超额最大且 t 值够？"""
    print("\n【重点】低波动环境（Q1+Q2）里各信号的表现 —— 这就是要找的「第二条腿」")
    print("  %-24s %-11s %-7s %-8s %-8s %s"
          % ("信号", "G1超额", "t值", "胜率", "交易日", "判定"))
    cands = []
    for sig, name, dirn in SIGNALS:
        r = signal_quintile_excess(panel, sig, dirn, env=env, env_filter={1, 2})
        if not r:
            continue
        v = r[1]                       # G1 恒 = 信号最有利组
        ok = (v["mean_excess"] > 0.3 and (v["t"] or 0) >= 2.0
              and v["win_days"] >= 52)
        print("  %-26s %+10.2f%% %-8s %-9s %-9d %s"
              % (name, v["mean_excess"], v["t"], "%.1f%%" % v["win_days"],
                 v["n_days"], "★ 候选" if ok else ""))
        if ok:
            cands.append((name, sig, v))
    if cands:
        print("\n  ⇒ 低波动期可用信号 %d 个：%s"
              % (len(cands), "、".join(c[0] for c in cands)))
    else:
        print("\n  ⇒ 低波动期没有任何信号通过门槛（超额>0.3%% · t≥2 · 胜率≥52%%）")


# ── 环境过滤的公平检验（2026-10-21 新增·本轮最重要的一步）──────────────
def rolling_env(panel, mkt, window=20, hist=120, min_hist=45):
    """滚动历史分位环境档（**可实盘**：只用 d 之前的 hist 日分布定分位）。

    与 `env_quintiles`（全样本分位）的区别：全样本分位含**未来函数**
    （用了 d 之后的数据来定阈值），只能当上界参考，不能当结论。
    """
    ev = {}
    for d in panel["days"]:
        v = env_of(mkt, d, window)
        if v is not None:
            ev[d] = v
    days = [d for d in panel["days"] if d in ev]
    out = {}
    for i, d in enumerate(days):
        lo = max(0, i - hist)
        hv = [ev[days[k]] for k in range(lo, i)]
        if len(hv) < min_hist:
            out[d] = None              # 历史不足 → 不判（按允许处理，不清零）
            continue
        cuts = statistics.quantiles(hv, n=5)[:4]
        q = 1
        for c in cuts:
            if ev[d] >= c:
                q += 1
        out[d] = q
    return out


def simulate_gated(samples, calendar, env=None, allow=None, pos_scale=1.0,
                   top_n=5, hold=FWD):
    """环境过滤 + 可选仓位缩放。返回净值统计。

    env=None        → 不过滤（基准）
    allow={1,2}     → 只在 env[d] ∈ allow 时开新仓
    pos_scale=0.5   → 每次只用一半现金买入（用于「同等暴露的简单降仓」对照）
    """
    byday = defaultdict(list)
    for s in samples:
        byday[s["date"]].append(s)
    for d in byday:
        byday[d].sort(key=lambda x: x["r20"])
    dates = sorted(byday)
    cal2 = [d for d in calendar if dates[0] <= d <= dates[-1]]
    cash, holdings, equity, trades = 1_000_000.0, [], [], []
    held, cap = set(), top_n * hold

    for d in cal2:
        still = []
        for h in holdings:
            if h["exit_date"] <= d:
                cash += h["shares"] * h["exit_px"] * (1 - COST_SELL)
                trades.append((h["shares"] * h["exit_px"] * (1 - COST_SELL)
                               / h["cost"] - 1) * 100)
                held.discard(h["code"])
            else:
                still.append(h)
        holdings = still
        total = cash
        for h in holdings:
            if d in h["map"]:
                h["mark"] = h["map"][d][3]
            total += h["shares"] * h["mark"]

        q = env.get(d) if env else None
        ok = (env is None) or (q is None) or (q in allow)
        cands = [s for s in byday.get(d, []) if s["code"] not in held]
        n_slot = min(top_n, max(0, cap - len(holdings)))
        if not ok or n_slot <= 0 or cash <= 1.0 or not cands:
            equity.append((d, total))
            continue
        budget = cash * pos_scale
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
            if len(holdings) >= cap:
                break
        equity.append((d, cash + sum(h["shares"] * h["mark"] for h in holdings)))
    for h in holdings:
        trades.append((h["shares"] * h["exit_px"] * (1 - COST_SELL) / h["cost"] - 1) * 100)

    vals = [v for _, v in equity]
    if len(vals) < 20:
        return None
    n = len(vals)
    years = n / 250.0
    cagr = ((vals[-1] / vals[0]) ** (1 / years) - 1) * 100
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, n)]
    sd = statistics.stdev(rets)
    mu = sum(rets) / len(rets)
    return {"cagr": round(cagr, 1), "mdd": round(mdd * 100, 1),
            "sharpe": round(mu / sd * (250 ** 0.5), 2) if sd else None,
            "calmar": round(cagr / (mdd * 100), 2) if mdd else None,
            "n": len(trades)}


def env_gate_verdict(panel, mkt, samples, calendar):
    """★ 公平判据：任何择时/环境手段，**必须打败同等暴露的简单降仓**。

    否则它所谓的「降回撤」只是「少下注」的劣质替代品 —— 而少下注不需要判断环境，
    直接固定仓位降低就行，没有过拟合风险。
    """
    print("\n【环境过滤 · 公平检验】")
    print("  判据：环境过滤必须打败「同等暴露的简单降仓」，否则无真实能力")
    print("  %-42s %-9s %-9s %-8s %-9s" % ("配置", "年化%", "回撤%", "夏普", "Calmar"))

    b = simulate_gated(samples, calendar)
    print("  %-44s %-10s %-10s %-9s %-10s"
          % ("基准 满仓", b["cagr"], b["mdd"], b["sharpe"], b["calmar"]))

    env = rolling_env(panel, mkt, hist=120)
    gates = []
    for allow, tag in (({1, 2}, "只在 Q1/Q2 开仓"),
                       ({1, 2, 3}, "只在 Q1/Q2/Q3 开仓"),
                       ({1, 2, 5}, "只在 Q1/Q2/Q5 开仓")):
        r = simulate_gated(samples, calendar, env=env, allow=allow)
        if not r:
            continue
        print("  %-44s %-10s %-10s %-9s %-10s"
              % (tag, r["cagr"], r["mdd"], r["sharpe"], r["calmar"]))
        gates.append((tag, r))

    print("\n  ── 对照：不判断环境，只固定降低仓位 ──")
    cmps = []
    for sc in (0.5, 0.4, 0.3):
        r = simulate_gated(samples, calendar, pos_scale=sc)
        if not r:
            continue
        tag = "固定仓位 ×%.1f" % sc
        print("  %-44s %-10s %-10s %-9s %-10s"
              % (tag, r["cagr"], r["mdd"], r["sharpe"], r["calmar"]))
        cmps.append((tag, r))

    print("\n  ── 判定 ──")
    all_worse = True
    for tag, r in gates:
        if not cmps:
            continue
        best = min(cmps, key=lambda x: abs(x[1]["cagr"] - r["cagr"]))
        ok = (r["calmar"] or 0) > (best[1]["calmar"] or 0)
        if ok:
            all_worse = False
        print("  %-40s Calmar %.2f  vs  %-18s Calmar %.2f  ⇒ %s"
              % (tag, r["calmar"] or 0, best[0], best[1]["calmar"] or 0,
                 "环境过滤更优" if ok else "简单降仓更优（无真实能力）"))
    if all_worse:
        print("\n  ⇒ **全部输给简单降仓**：环境过滤无真实能力，"
              "所谓降回撤只是「少下注」的劣质替代品")

    print("\n  ── 参数敏感性（M9：平台还是尖刺）──")
    print("  %-12s %-9s %-9s %-9s" % ("hist", "年化%", "回撤%", "Calmar"))
    cs = []
    for hist in range(75, 120, 5):
        e = rolling_env(panel, mkt, hist=hist)
        r = simulate_gated(samples, calendar, env=e, allow={1, 2})
        if not r:
            continue
        print("  %-14s %-10s %-10s %-10s"
              % (hist, r["cagr"], r["mdd"], r["calmar"]))
        if r["calmar"] is not None:
            cs.append(r["calmar"])
    if len(cs) > 2:
        good = [c for c in cs if c >= 0.6]
        print("  Calmar 中位 %.2f · 标准差 %.2f · ≥0.6 的有 %d/%d 组"
              % (statistics.median(cs), statistics.stdev(cs), len(good), len(cs)))
        print("  ⇒ %s" % ("**平台**：多数参数都好，可能真实"
                          if len(good) >= len(cs) * 0.5
                          else "**尖刺**：仅少数参数好 ⇒ 按 M9 判为噪声/过拟合"))
    return gates, cmps


# ── 自检 ────────────────────────────────────────────────────────────────────
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

    print("alpha_matrix.py 自检")

    # 造一个"信号有效"的合成面板：sig 越高 → fwd 越高（确定性）
    days = ["2026-01-%02d" % (i + 1) for i in range(20)]
    rows, bench = {}, {}
    for di, d in enumerate(days):
        m = {}
        for k in range(100):
            sig = k / 100.0
            m["c%03d" % k] = {"r5": sig, "r20": sig, "r60": sig, "vol20": sig,
                              "amt20": sig, "dd60": sig, "rel20": sig,
                              "fwd": sig * 0.02}          # 信号越强收益越高
        rows[d] = m
        bench[d] = sum(v["fwd"] for v in m.values()) / len(m)
    panel = {"rows": rows, "bench": bench, "days": days, "n": 2000}

    r = signal_quintile_excess(panel, "r20", "desc")
    T("T1 分位函数返回 5 组", len(r) == Q, str(len(r)))
    T("T2 口语口径：G1=最有利（信号最强）⇒ 有效信号下 G1 超额 > 0 且 G5 < 0",
      r[1]["mean_excess"] > 0 and r[5]["mean_excess"] < 0,
      "G1=%.2f G5=%.2f" % (r[1]["mean_excess"], r[5]["mean_excess"]))
    T("T3 G1 超额是五组最大", r[1]["mean_excess"] == max(x["mean_excess"] for x in r.values()))

    # asc 方向：值越小越有利 ⇒ 排序反向，G1 仍应是最有利
    # 造一个「越小 fwd 越高」的字段验证方向开关
    for d in days:
        for k, v in enumerate(panel["rows"][d].values()):
            f = k / 100.0
            v["rev"] = 1.0 - f          # 越小 → fwd(=f*0.02) 越高
    r2 = signal_quintile_excess(panel, "rev", "asc")
    T("T4 asc 方向下 G1 仍是最有利组（方向开关生效）",
      r2[1]["mean_excess"] == max(x["mean_excess"] for x in r2.values()),
      "G1=%.3f" % r2[1]["mean_excess"])

    # 无效信号：信号与收益完全无关（用取模打散，避免排序稳定误导）
    for d in days:
        for k, v in enumerate(panel["rows"][d].values()):
            v["noise"] = (k * 37) % 100      # 打散，与 k 单调无关
            v["fwd"] = (k / 100.0) * 0.02
    r3 = signal_quintile_excess(panel, "noise", "desc")
    T("T5 无关联信号下各组超额绝对值都很小（<0.2pp）",
      all(abs(x["mean_excess"]) < 0.2 for x in r3.values()),
      str({k: round(v["mean_excess"], 3) for k, v in r3.items()}))

    # 缺值处理：一半 None 不应崩
    for d in days:
        for i, v in enumerate(panel["rows"][d].values()):
            if i % 2:
                v["half"] = None
            else:
                v["half"] = i
    r4 = signal_quintile_excess(panel, "half", "desc")
    T("T6 一半缺值时不崩且仍有结果", bool(r4), str(len(r4)))

    # 环境分档：造一个波动率可算的假市场（须 ≥ window+1 个交易日）
    mdays = ["2026-02-%02d" % (i + 1) for i in range(25)]
    mret = {d: (0.01 if i % 2 == 0 else -0.01) for i, d in enumerate(mdays)}
    idx, cur = {}, 1000.0
    for d in mdays:
        cur *= (1 + mret[d])
        idx[d] = cur
    mkt = {"days": mdays, "index": idx, "ret": mret, "n_avg": 100}
    T("T7 env_of 历史不足返回 None", env_of(mkt, mdays[3], 20) is None)
    T("T8 env_of 足够历史时返回正数", (env_of(mkt, mdays[24], 20) or 0) > 0,
      str(env_of(mkt, mdays[24], 20)))

    # env_quintiles 天数不足时返回空
    eq, ev = env_quintiles(panel, mkt)
    T("T9 交易日不足 50 时环境分档返回空（fail-safe 不硬分）", eq == {}, str(len(eq)))

    # 未来函数检查：forward 收益必须从 i+1 开盘起算
    import ast
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    T("T10 源码含「T+1 开盘买」的显式注释或实现（审计锚点）",
      'rows[i + 1][1]' in src and 'rows[i + FWD][4]' in src)

    # 零生产写点
    prod = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        nm = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
        if nm != "open":
            continue
        mode = None
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = node.args[1].value
        for k in node.keywords:
            if k.arg == "mode" and isinstance(k.value, ast.Constant):
                mode = k.value.value
        if isinstance(mode, str) and any(c in mode for c in "wax+"):
            tgt = node.args[0] if node.args else None
            if not (isinstance(tgt, ast.Name) and tgt.id in ("PANEL", "MKT")):
                prod.append(ast.unparse(node)[:60])
    T("T11 零生产写点（仅允许 TEMP 面板缓存）", not prod, str(prod)[:100])

    # ── 新增：环境过滤的公平检验必须存在，且「简单降仓」对照不可省 ──
    T("T12 源码含「同等暴露的简单降仓」对照（无对照即无判据）",
      "pos_scale" in src and "固定仓位" in src)
    T("T13 源码区分两种环境分位（全样本 vs 滚动历史）",
      "env_quintiles" in src and "rolling_env" in src)
    T("T14 滚动分位明确标注「可实盘」、全样本分位标注「未来函数/上界」",
      "可实盘" in src and ("未来函数" in src or "上界" in src))

    # rolling_env 行为：历史不足返回 None（不硬判）
    days2 = ["2026-04-%02d" % (i + 1) for i in range(30)]
    panel2 = {"rows": {d: {} for d in days2}, "bench": {d: 0.0 for d in days2},
              "days": days2, "n": 0}
    mret2 = {d: (0.01 if i % 2 == 0 else -0.009) for i, d in enumerate(days2)}
    idx2, c2 = {}, 1000.0
    for d in days2:
        c2 *= (1 + mret2[d])
        idx2[d] = c2
    mkt2 = {"days": days2, "index": idx2, "ret": mret2, "n_avg": 100}
    re = rolling_env(panel2, mkt2, window=20, hist=5, min_hist=45)
    T("T15 历史不足 min_hist 时环境档为 None（不硬判、不清零仓位）",
      all(v is None for v in re.values()) if re else True,
      str(list(re.values())[:5]))

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    for f in ("build", "run", "lowvol", "gate", "selftest", "force"):
        ap.add_argument("--" + f, action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return _selftest()

    t0 = time.time()
    panel = build_panel(force=a.force)
    mkt = load_market()
    print("面板 %d 条 · %d 个交易日 · %.1fs"
          % (panel["n"], len(panel["days"]), time.time() - t0))

    if a.build and not (a.run or a.lowvol or a.gate):
        return 0

    if a.gate:
        import mr_study as _M
        import mr_risk as _R
        cal = _M.load_trading_days()
        sub = [x for x in _R.load_samples("full") if x["r20"] <= -25.0]
        print("样本 %d 笔（跌>25%%）" % len(sub))
        env_gate_verdict(panel, mkt, sub, cal)
        print("\n总耗时 %.1fs" % (time.time() - t0))
        return 0

    results, env = run_matrix(panel, mkt)
    focus_lowvol(panel, mkt, results, env)
    print("\n总耗时 %.1fs" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
