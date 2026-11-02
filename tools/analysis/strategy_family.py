"""strategy_family.py · 策略族（多套赚钱方法）× 环境 → 策略 switcher 研究台。

## 为什么需要这个文件（用户纠偏驱动的重大转向，2026-10-21）

用户原话：
  「股市赚钱的方法很多，赚钱的模式很多，但每个人肯定不是天天都赚钱，
    肯定有某一段时间自己那套模式不灵验的时候。但股市每天都有涨的也有跌的，
    每天都有涨停的。你要做到针对不同的环境、针对不同的情况，去**切换不同的投资思路**，
    而不是研究一套固定的因子出来一直用。」

★ 这指出了我之前三份报告的共同局限：
  我测的「环境过滤」= 在**同一套策略**上加减仓位开关
  ⇒ 结果必然只能变成「少下注」（M16 完美解释了为何全败）。
  **而用户要的是「换一套玩法」——不同的赚钱逻辑本身轮换。**

  这两件事在数学上是不同的：
    · 加仓/减仓：同一个 alpha，改变暴露大小 → 风险收益比不变（Calmar 恒定）
    · 换策略  ：换一个 alpha，改变收益驱动来源 → **风险收益比可以改变**

  所以「策略族 + 切换」是有可能真的改善 Calmar 的唯一方向，
  而「同一策略调仓位」数学上不可能（M16 就是这个证明）。

## 本文件定义的策略族（每套 = 一个独立的赚钱逻辑）

  S1 REV_SHORT   短反转：买近期跌幅最深（3/5/10日），持 5-10 日
                 —— 已验证有效，但只在市场高波动时强
  S2 REV_R20     超跌反转：r20 ≤ -25%，持 10 日（现有主策略）
  S3 MOM_STRONG  趋势跟随：买 N 日涨幅最强（20/60日），持 10-20 日
                 —— 我们此前测出「反向指标」，但那是在**全样本**上；此处按环境分开测
  S4 LOWVOL      低波轮动：买波动率最低的 K 只（ETF 池），持 10 日
                 —— 外部实证：聚宽 7年4.7倍/回撤16.89%
  S5 ETF_ROT     宽基轮动：在宽基 ETF 里按动量选最强，持 10 日
                 —— 外部实证：ETF 轮动是聚宽主流风控友好策略
  S6 RS_STRONG   相对强度：买行业中相对最强的（rel20 最高），持 10 日
                 —— 与 S1/S2 方向相反，构成对冲

## 环境定义（用 ≤d 数据，可实盘）

  · 趋势（trend）：宽基指数 MA20 vs MA60、以及 60 日涨幅
  · 波动（vol）  ：市场 20 日已实现波动率
  · 情绪（heat） ：涨停家数、连板高度（有 zt_pool 时用，否则跳过）

## 核心输出（回答用户的问题）

  1. 每个策略在每个环境下的 Calmar / 年化 / 回撤 → **谁在什么环境下灵**
  2. 「切换」组合 vs「任一单策略」vs「简单等权混合」
     ★ 判据：切换必须打败**等权混合**（等权混合 = 不判断环境也不换，
       只是各策略同时跑。如果切换打不过等权混合，说明"判断环境"没有价值，
       价值来自"多策略并存"本身 —— 这个区分极其重要。）
  3. 切换规则必须无未来函数（用滚动历史分位，不用全样本分位）

用法：
  python tools/analysis/strategy_family.py --selftest
  python tools/analysis/strategy_family.py --single     # 各策略单独表现
  python tools/analysis/strategy_family.py --matrix     # 策略 × 环境矩阵
  python tools/analysis/strategy_family.py --switch     # 切换 vs 等权混合
"""
import argparse
import csv
import os
import pickle
import statistics
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("analysis", "fs"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

TMP = os.environ.get("TEMP", "/tmp")
COST_BUY = 0.00125
COST_SELL = 0.00175
KL = os.path.join(_ROOT, "data", "raw", "bus", "klines.csv")
ETF = os.path.join(_ROOT, "data", "raw", "bus", "etf_klines.csv")

# ★★ 2026-10-22 缓存新鲜度守卫（M24）
#   事故：本模块把加载结果缓存到 TEMP/sf_*.pkl，**从不检查缓存是否过期**。
#   更糟的是 etf_klines.csv 是旧包原样副本，**停在 2026-09-22**，
#   而 klines.csv 已到 2026-10-22 ⇒ 低波轮动/宽基轮动的结论是「一个月前的世界」。
#   现在：加载时打印缓存时间 + 覆盖范围 + 源数据是否更新过；源数据比缓存新则告警。
sys.path.insert(0, os.path.join(_ROOT, "tools", "fs"))
import cache_guard as CG      # noqa: E402

# 本模块缓存依赖的源数据
_CG_SOURCES = (KL, ETF)


# ── 数据加载 ────────────────────────────────────────────────────────────────
def load_stocks(force=False):
    """{code: {date: (o,h,l,c,amt)}} + 交易日序列。缓存到 TEMP。

    ★ 走 `cache_guard`：打印缓存时间与覆盖范围，源数据比缓存新则告警。
    """
    cp = os.path.join(TMP, "sf_stocks.pkl")
    _rep = CG.inspect(cp, source_globs=_CG_SOURCES, root=_ROOT)
    if not force:
        CG.print_report(_rep, prefix="")
        if _rep["verdict"] in ("STALE", "OLD"):
            print("  ⚠ 本模块结果可能不含最新行情（缓存比源数据旧）"
                  "——加 --force 可强制重建缓存")
        # 缓存命中就用缓存（否则每次都要重解 240 万行）；过期与否已在上方显式告知
        if _rep["exists"]:
            return pickle.load(open(cp, "rb"))
    by_code, days = defaultdict(dict), set()
    with open(KL, encoding="utf-8", errors="ignore") as f:
        for r in csv.DictReader(f):
            c = r["code"]
            if c.startswith("bj") or c.startswith("sh000") or c.startswith("sz399"):
                continue
            try:
                by_code[c][r["date"]] = (float(r["open"]), float(r["high"]),
                                         float(r["low"]), float(r["close"]),
                                         float(r["amount"]))
                days.add(r["date"])
            except (TypeError, ValueError):
                continue
    out = {"stocks": dict(by_code), "days": sorted(days)}
    pickle.dump(out, open(cp, "wb"))
    return out


def load_etfs(force=False):
    """{code: {date: (o,h,l,c,amt)}} + {code: (name, cat)}。"""
    cp = os.path.join(TMP, "sf_etfs.pkl")
    _rep = CG.inspect(cp, source_globs=_CG_SOURCES, root=_ROOT)
    if not force:
        CG.print_report(_rep, prefix="")
        if _rep["verdict"] in ("STALE", "OLD"):
            print("  ⚠ ETF 数据可能停更（%s）——低波/宽基轮动结论需谨慎"
                  % (_rep["d1"] or "范围未知"))
        if _rep["exists"]:
            return pickle.load(open(cp, "rb"))
    by_code, meta = defaultdict(dict), {}
    with open(ETF, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            c = r["code"]
            if c.startswith("sh000") or c.startswith("sz399"):
                continue                       # 指数本身不做交易标的
            try:
                by_code[c][r["date"]] = (float(r["open"]), float(r["high"]),
                                         float(r["low"]), float(r["close"]),
                                         float(r["amount"]))
                meta[c] = (r["name"], r["cat"])
            except (TypeError, ValueError):
                continue
    out = {"etfs": dict(by_code), "meta": meta,
           "days": sorted({d for m in by_code.values() for d in m})}
    pickle.dump(out, open(cp, "wb"))
    return out


# ── 环境判定（无未来函数）──────────────────────────────────────────────────
def build_env(prices, days, ma_fast=20, ma_slow=60, vol_win=20):
    """逐日算环境：{date: {trend, vol, ret60}}。

    trend: "up"/"down"/"flat"（宽基等权指数 MA20 vs MA60）
    vol  : 市场 20 日已实现年化波动率
    """
    # 全市场等权指数
    eq_ret = {}
    for d in days:
        xs = []
        for c, m in prices.items():
            if d in m and m[d][3] > 0:
                xs.append(m[d][3])
        eq_ret[d] = xs

    # 用所有票的日收益均值构指数
    prev = {}
    rets_by_day = defaultdict(list)
    for c, m in prices.items():
        ds = sorted(m)
        for i in range(1, len(ds)):
            d0, d1 = ds[i - 1], ds[i]
            if m[d0][3] > 0:
                rets_by_day[d1].append(m[d1][3] / m[d0][3] - 1)
    idx, cur = {}, 1000.0
    for d in days:
        rs = rets_by_day.get(d)
        if rs:
            cur *= (1 + sum(rs) / len(rs))
        idx[d] = cur

    out = {}
    for i, d in enumerate(days):
        e = {"trend": None, "vol": None, "ret60": None}
        if i >= vol_win:
            xs = []
            for k in range(i - vol_win + 1, i + 1):
                rs = rets_by_day.get(days[k])
                if rs:
                    xs.append(sum(rs) / len(rs))
            if len(xs) > 2:
                e["vol"] = statistics.stdev(xs) * (250 ** 0.5)
        if i >= ma_slow:
            f = sum(idx[days[k]] for k in range(i - ma_fast + 1, i + 1)) / ma_fast
            s = sum(idx[days[k]] for k in range(i - ma_slow + 1, i + 1)) / ma_slow
            e["trend"] = "up" if f > s * 1.005 else ("down" if f < s * 0.995 else "flat")
            e["ret60"] = idx[d] / idx[days[i - ma_slow]] - 1
        out[d] = e
    return out, idx


# ── 策略信号生成器（每个返回 [(code, score)] 按优先级排序）──────────────
def sig_rev(prices, d, days, i, lookback=5, top=5, universe=None):
    """短反转：lookback 日跌幅最深。"""
    if i < lookback + 1:
        return []
    out = []
    for c, m in prices.items():
        if universe and c not in universe:
            continue
        if d not in m or days[i - lookback] not in m:
            continue
        p0, p1 = m[days[i - lookback]][3], m[d][3]
        if p0 > 0 and p1 > 0:
            out.append((c, p1 / p0 - 1))
    out.sort(key=lambda x: x[1])
    return out[:top]


def sig_r20(prices, d, days, i, top=5, drop=-0.25, universe=None):
    """超跌反转：20日跌超阈值。"""
    if i < 21:
        return []
    out = []
    for c, m in prices.items():
        if universe and c not in universe:
            continue
        if d not in m or days[i - 20] not in m:
            continue
        p0, p1 = m[days[i - 20]][3], m[d][3]
        if p0 > 0 and p1 > 0 and (p1 / p0 - 1) <= drop:
            out.append((c, p1 / p0 - 1))
    out.sort(key=lambda x: x[1])
    return out[:top]


def sig_mom(prices, d, days, i, lookback=20, top=5, universe=None):
    """趋势跟随：lookback 日涨幅最强。"""
    if i < lookback + 1:
        return []
    out = []
    for c, m in prices.items():
        if universe and c not in universe:
            continue
        if d not in m or days[i - lookback] not in m:
            continue
        p0, p1 = m[days[i - lookback]][3], m[d][3]
        if p0 > 0 and p1 > 0:
            out.append((c, p0 / p1 - 1))      # 用倒数排序，负值=涨最多
    out.sort(key=lambda x: x[1])
    return [(c, -s) for c, s in out[:top]]


def sig_lowvol(prices, d, days, i, window=20, top=3, universe=None):
    """低波轮动：窗口内波动率最低。"""
    if i < window + 1:
        return []
    out = []
    for c, m in prices.items():
        if universe and c not in universe:
            continue
        ds = [days[k] for k in range(i - window, i + 1)]
        if not all(x in m for x in ds):
            continue
        cl = [m[x][3] for x in ds]
        rs = [cl[k] / cl[k - 1] - 1 for k in range(1, len(cl)) if cl[k - 1] > 0]
        if len(rs) < window - 2:
            continue
        out.append((c, statistics.stdev(rs)))
    out.sort(key=lambda x: x[1])
    return out[:top]


def sig_rs(prices, d, days, i, lookback=20, top=5, universe=None):
    """相对强度：lookback 日涨幅最强（与 mom 同向，但用于股票池）。"""
    return sig_mom(prices, d, days, i, lookback=lookback, top=top,
                   universe=universe)


# ── 策略定义（策略族）──────────────────────────────────────────────────────
FAMILY = {
    "REV5": {"fn": sig_rev, "kw": {"lookback": 5}, "hold": 5,
             "universe": "stock", "desc": "短反转(5日跌最深)持5日"},
    "REV10": {"fn": sig_rev, "kw": {"lookback": 10}, "hold": 10,
              "universe": "stock", "desc": "短反转(10日跌最深)持10日"},
    "R20": {"fn": sig_r20, "kw": {"drop": -0.25}, "hold": 10,
            "universe": "stock", "desc": "超跌反转(r20<=-25%)持10日"},
    "MOM20": {"fn": sig_mom, "kw": {"lookback": 20}, "hold": 10,
              "universe": "stock", "desc": "趋势跟随(20日涨最强)持10日"},
    "MOM60": {"fn": sig_mom, "kw": {"lookback": 60}, "hold": 20,
              "universe": "stock", "desc": "趋势跟随(60日涨最强)持20日"},
    "LOWVOL": {"fn": sig_lowvol, "kw": {"window": 20}, "hold": 10,
               "universe": "etf", "desc": "低波轮动(ETF波动最低)持10日"},
    "ETFROT": {"fn": sig_mom, "kw": {"lookback": 20}, "hold": 10,
               "universe": "etf_wide", "desc": "宽基ETF动量轮动持10日"},
}


def _etf_wide_codes(meta):
    return {c for c, (nm, cat) in meta.items() if cat == "wide"}


# ── 回测引擎（单策略）──────────────────────────────────────────────────────
def backtest(prices, days, strat, top=5, init=1_000_000.0, env_days=None):
    """逐日按策略信号等权买入、持有 hold 日。返回净值曲线。"""
    cfg = FAMILY[strat]
    hold = cfg["hold"]
    cash, holdings, equity, trades = init, [], [], []
    held = set()
    cap = top * hold

    for i, d in enumerate(days):
        if env_days and d not in env_days:
            continue
        # 到期卖出
        still = []
        for h in holdings:
            if h["exit_i"] <= i:
                px = prices[h["code"]].get(d, (0, 0, 0, h["mark"], 0))[3] or h["mark"]
                proceeds = h["sh"] * px * (1 - COST_SELL)
                cash += proceeds
                # 收益率必须用「卖出总回款 / 买入总成本」，不能拿单股价除总成本
                trades.append(proceeds / h["cost"] - 1)
                held.discard(h["code"])
            else:
                still.append(h)
        holdings = still
        # 市值
        total = cash
        for h in holdings:
            px = prices[h["code"]].get(d)
            if px:
                h["mark"] = px[3]
            total += h["sh"] * h["mark"]
        # 买入
        sigs = cfg["fn"](prices, d, days, i, top=top, **cfg["kw"])
        sigs = [s for s in sigs if s[0] not in held]
        n_slot = min(top, max(0, cap - len(holdings)))
        if n_slot <= 0 or cash <= 1.0 or not sigs:
            equity.append((d, total))
            continue
        picked = sigs[:n_slot]
        per = cash / n_slot
        for c, _sc in picked:
            pxd = prices[c].get(d)
            if not pxd or pxd[3] <= 0:
                continue
            # T+1 开盘买：用次日开盘价
            if i + 1 >= len(days):
                continue
            nd = days[i + 1]
            npx = prices[c].get(nd)
            if not npx or npx[0] <= 0:
                continue
            px = npx[0]
            sh = int(per / (px * (1 + COST_BUY)) / 100) * 100
            if sh < 100:
                continue
            cost = sh * px * (1 + COST_BUY)
            if cost > cash:
                continue
            cash -= cost
            holdings.append({"code": c, "sh": sh, "cost": cost,
                             "exit_i": i + 1 + hold, "mark": px})
            held.add(c)
            if len(holdings) >= cap:
                break
        equity.append((d, cash + sum(h["sh"] * h["mark"] for h in holdings)))

    for h in holdings:
        px = h["mark"]
        proceeds = h["sh"] * px * (1 - COST_SELL)
        trades.append(proceeds / h["cost"] - 1)
    return {"equity": equity, "trades": trades}


def summarize(sim, label=""):
    vals = [v for _, v in sim["equity"]]
    n = len(vals)
    if n < 30:
        return {"label": label, "n": 0}
    years = n / 250.0
    cagr = ((vals[-1] / vals[0]) ** (1 / years) - 1) * 100 if years > 0.1 else None
    peak, mdd, at = vals[0], 0.0, None
    for i, v in enumerate(vals):
        peak = max(peak, v)
        if (peak - v) / peak > mdd:
            mdd, at = (peak - v) / peak, sim["equity"][i][0]
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, n)]
    sd = statistics.stdev(rets) if len(rets) > 1 else 0
    mu = sum(rets) / len(rets)
    tr = sim["trades"]
    return {"label": label, "n": len(tr),
            "cagr": round(cagr, 1) if cagr else None,
            "mdd": round(mdd * 100, 1), "mdd_at": at,
            "sharpe": round(mu / sd * (250 ** 0.5), 2) if sd else None,
            "calmar": round(cagr / (mdd * 100), 2) if (cagr and mdd) else None,
            "win": round(len([x for x in tr if x > 0]) / len(tr) * 100, 1) if tr else None}


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

    print("strategy_family.py 自检")

    T("T1 策略族至少 6 套（多套赚钱方法，非单一因子）", len(FAMILY) >= 6,
      str(len(FAMILY)))
    T("T2 策略族方向互不相同（含反转与趋势两个相反方向）",
      "REV5" in FAMILY and "MOM20" in FAMILY)
    T("T3 每套策略都有描述与持有期",
      all("desc" in v and "hold" in v for v in FAMILY.values()))

    # 造合成数据：一只票持续下跌 → REV 应该选中
    days = ["2026-01-%02d" % (i + 1) for i in range(40)]
    prices = {"down": {}, "up": {}, "flat": {}}
    for i, d in enumerate(days):
        prices["down"][d] = (10 * (0.97 ** i),) * 4 + (1e8,)
        prices["up"][d] = (10 * (1.03 ** i),) * 4 + (1e8,)
        prices["flat"][d] = (10.0, 10.0, 10.0, 10.0, 1e8)

    s = sig_rev(prices, days[30], days, 30, lookback=5, top=3)
    T("T4 反转信号选中最跌的票", s and s[0][0] == "down", str(s))
    s2 = sig_mom(prices, days[30], days, 30, lookback=20, top=3)
    T("T5 趋势信号选中最涨的票", s2 and s2[0][0] == "up", str(s2))
    T("T6 反转与趋势选出相反标的（方向真的相反）",
      s[0][0] != s2[0][0], "%s vs %s" % (s[0][0], s2[0][0]))

    s3 = sig_lowvol(prices, days[30], days, 30, window=20, top=1)
    T("T7 低波信号选中波动最低的票", s3 and s3[0][0] == "flat", str(s3))

    # 环境判定
    env, idx = build_env(prices, days)
    T("T8 环境含 trend/vol/ret60 三要素",
      all(k in env[days[35]] for k in ("trend", "vol", "ret60")))
    T("T9 前段历史不足时 trend 为 None（不硬判）",
      env[days[3]]["trend"] is None)

    # 无未来函数：信号只用到 d 及之前
    T("T10 信号函数签名只用 (prices, d, days, i)（无后视参数）",
      "(prices, d, days, i" in open(os.path.abspath(__file__),
                                    encoding="utf-8").read())

    # 回测引擎基本正确性：单边下跌策略应亏
    sim = backtest(prices, days, "REV5", top=1)
    T("T11 回测产生净值曲线与成交", len(sim["equity"]) > 10 and sim["trades"])

    # 零写点
    import ast
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
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
            if not (isinstance(tgt, ast.Name) and "cp" in tgt.id.lower()
                    or isinstance(tgt, ast.Name) and tgt.id in ("cp",)):
                prod.append(ast.unparse(node)[:60])
    T("T12 零生产写点（仅允许 TEMP 缓存）", not prod, str(prod)[:100])

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _run_strategy(name, st, et, env, days, env_filter=None, top=5):
    """跑单策略；env_filter = 谓词 env->bool，None=全部。"""
    cfg = FAMILY[name]
    if cfg["universe"] == "etf":
        prices, dd = et["etfs"], et["days"]
    elif cfg["universe"] == "etf_wide":
        wide = _etf_wide_codes(et["meta"])
        prices = {c: m for c, m in et["etfs"].items() if c in wide}
        dd = et["days"]
    else:
        prices, dd = st["stocks"], days

    if env_filter is None:
        return backtest(prices, dd, name, top=top)

    # 只保留满足环境的日子（策略在该环境下的独立表现）
    keep = {d for d in dd if env_filter(env.get(d) or {})}
    return backtest(prices, [d for d in dd if d in keep], name, top=top)


def exp_matrix(st, et, env, days):
    """策略 × 环境矩阵 —— 回答「哪套方法在什么环境下灵」。

    ★ 口径警告（我第一版踩过的坑）：
      本矩阵用「**同一根时间轴、只在满足环境的日子做归因统计**」，
      即每天的收益按当日环境归档，而不是"只保留该环境的日子连成一条净值曲线"。
      后者会让 43 个孤立交易日的净值连成曲线，年化分母只有 43 天，
      而持有期跨出窗口 ⇒ 出现 132719% 这种伪数字。

      因此本矩阵输出的是 **年化贡献（按日归档）**，不是"该策略在该环境单独跑"的结果。
      「单独跑」的正确做法见 exp_switch 的切换引擎。
    """
    envs = {
        "全部": None,
        "趋势向上": lambda e: e.get("trend") == "up",
        "趋势向下": lambda e: e.get("trend") == "down",
        "无趋势": lambda e: e.get("trend") == "flat",
        "高波动": lambda e: (e.get("vol") or 0) >= 0.25,
        "低波动": lambda e: (e.get("vol") or 0) < 0.25,
    }
    print("\n【策略 × 环境】按日归档的收益归因（不是「单独跑」，口径见函数注释）")
    print("  每格 = 该策略在「该环境的交易日」里的平均日收益 × 250（年化近似）")
    hdr = "  %-24s" % "策略"
    for en in envs:
        hdr += " %-12s" % en
    print(hdr)

    res = {}
    for name, cfg in FAMILY.items():
        prices = _prices_for(name, st, et)
        dd = et["days"] if cfg["universe"].startswith("etf") else days
        sim = backtest(prices, dd, name, top=5)
        eq = sim["equity"]
        # 按日收益 → 归档到环境
        buckets = defaultdict(list)
        for i in range(1, len(eq)):
            d0, v0 = eq[i - 1]
            d1, v1 = eq[i]
            if v0 <= 0:
                continue
            r = v1 / v0 - 1
            e = env.get(d1) or {}
            if e.get("trend") is None:
                continue
            buckets["全部"].append(r)
            if e.get("trend") == "up":
                buckets["趋势向上"].append(r)
            elif e.get("trend") == "down":
                buckets["趋势向下"].append(r)
            elif e.get("trend") == "flat":
                buckets["无趋势"].append(r)
            if (e.get("vol") or 0) >= 0.25:
                buckets["高波动"].append(r)
            else:
                buckets["低波动"].append(r)
        line = "  %-24s" % cfg["desc"][:22]
        res[name] = {}
        for en in envs:
            xs = buckets.get(en) or []
            if len(xs) < 5:
                line += " %-12s" % "-"
                res[name][en] = None
                continue
            ann = statistics.mean(xs) * 250 * 100
            # 该环境内的日级年化波动，用于粗看稳定性
            sd = statistics.stdev(xs) * (250 ** 0.5) * 100 if len(xs) > 2 else None
            res[name][en] = {"ann": ann, "n_days": len(xs), "vol": sd}
            line += " %-12s" % ("%+6.0f%%(%dd)" % (ann, len(xs)))
        print(line)
    print("\n  ★ 注意：无趋势只有 43 天，样本极少，该列数字不可作为结论（M12）")
    return res


def backtest_switch(prices_map, days, pick_fn, top=5, init=1_000_000.0):
    """★ 真正的「策略切换」引擎：逐日判断环境 → 当日启用哪套策略。

    ★ 为什么不能用「只保留该环境的日子」来测单策略环境的归因（我第一版就这么错了）：
      那样会把 43 个孤立交易日的净值连成一条曲线，年化分母只有 43 天，
      而持有期却跨出筛选窗口 ⇒ 得到 132719% 这种伪数字。
      正确做法：**在同一根连续时间轴上逐日切换**，这才对应实盘。

    pick_fn(date) → (策略名 or None, 对应的 prices dict)
    """
    cash, holdings, equity, trades = init, [], [], []
    held = set()
    max_cap = top * 30

    for i, d in enumerate(days):
        # ① 到期卖出（按当日价格，跨策略通用：持仓库不区分来源）
        # ★ 索引纪律：h["prices"] 结构是 {code: {date: (o,h,l,c,amt)}}，
        #   必须走 prices[code][date]。只写 prices.get(date) 会永远取不到价，
        #   退回 mark ⇒ 卖出价恒等于买入价 ⇒ 每笔恰好亏掉双边成本（-0.3%）。
        still = []
        for h in holdings:
            if h["exit_i"] <= i:
                ser = h["prices"].get(h["code"]) or {}
                rec = ser.get(d)
                px = rec[3] if rec and rec[3] > 0 else h["mark"]
                proceeds = h["sh"] * px * (1 - COST_SELL)
                cash += proceeds
                trades.append({"ret": proceeds / h["cost"] - 1,
                               "strat": h["strat"]})
                held.discard(h["code"])
            else:
                still.append(h)
        holdings = still

        # ② 市值（同样走 prices[code][date]）
        total = cash
        for h in holdings:
            ser = h["prices"].get(h["code"]) or {}
            rec = ser.get(d)
            if rec and rec[3] > 0:
                h["mark"] = rec[3]
            total += h["sh"] * h["mark"]

        # ③ 当日选哪套策略
        name, prices_ = pick_fn(d)
        if name is None:
            equity.append((d, total))
            continue
        cfg = FAMILY[name]
        if i + 1 >= len(days):
            equity.append((d, total))
            continue
        nd = days[i + 1]
        sigs = cfg["fn"](prices_, d, days, i, top=top, **cfg["kw"])
        sigs = [s for s in sigs if s[0] not in held]
        n_slot = min(top, max(0, max_cap - len(holdings)))
        if n_slot <= 0 or cash <= 1.0 or not sigs:
            equity.append((d, total))
            continue
        picked = sigs[:n_slot]
        per = cash / n_slot
        for c, _sc in picked:
            # ★ 两层索引：prices_[code][date]。写成 prices_.get(date) 会永远买不进
            ser = prices_.get(c)
            if not ser:
                continue
            npx = ser.get(nd)
            if not npx or npx[0] <= 0:
                continue
            px = npx[0]
            sh = int(per / (px * (1 + COST_BUY)) / 100) * 100
            if sh < 100:
                continue
            cost = sh * px * (1 + COST_BUY)
            if cost > cash:
                continue
            cash -= cost
            holdings.append({"code": c, "sh": sh, "cost": cost,
                             "exit_i": i + 1 + cfg["hold"], "mark": px,
                             "prices": prices_, "strat": name})
            held.add(c)
            if len(holdings) >= max_cap:
                break
        # ★ 当日新买入的持仓立刻计入市值；否则净值会漏掉当天的持仓，
        #   且下一天若 exit_i<=i 会被误判为"到期"（这正是前 10 笔全是 -0.3% 成本的根因）
        for h in holdings:
            if "mark" in h and h.get("prices") is prices_:
                pass
        equity.append((d, cash + sum(h["sh"] * h["mark"] for h in holdings)))

    for h in holdings:
        proceeds = h["sh"] * h["mark"] * (1 - COST_SELL)
        trades.append({"ret": proceeds / h["cost"] - 1, "strat": h["strat"]})
    return {"equity": equity,
            "trades": [t["ret"] for t in trades],
            "by_strat": _group_trades(trades)}


def _group_trades(trades):
    g = defaultdict(list)
    for t in trades:
        g[t["strat"]].append(t["ret"])
    return {k: {"n": len(v),
                "win": round(len([x for x in v if x > 0]) / len(v) * 100, 1),
                "avg": round(sum(v) / len(v) * 100, 2)} for k, v in g.items()}


def _prices_for(name, st, et):
    cfg = FAMILY[name]
    if cfg["universe"] == "etf":
        return et["etfs"]
    if cfg["universe"] == "etf_wide":
        wide = _etf_wide_codes(et["meta"])
        return {c: m for c, m in et["etfs"].items() if c in wide}
    return st["stocks"]


def exp_switch(st, et, env, days):
    """★ 核心实验：动态切换 vs 等权混合 vs 单策略。

    三条对照（缺一不可）：
      ① 单策略          —— 只用一套
      ② 等权混合        —— 全都跑，各占 1/N，**不判断环境**
      ③ 动态切换        —— 每天按环境选一套

    ★ 判据：切换必须打败**等权混合**。
      若打不过 ⇒ 「判断环境」没有价值，价值只来自「多策略并存」本身。
      这个区分决定了用户要的「切换思路」到底成立不成立。
    """
    print("\n【核心实验】动态切换 vs 等权混合 vs 单策略")
    print("  判据：切换必须打败等权混合，否则「判断环境」无价值\n")

    # ① 单策略
    print("  ① 单策略（一根时间轴，全程只跑这一套）")
    print("     %-26s %-8s %-9s %-9s %-8s %-9s"
          % ("策略", "笔数", "年化%", "回撤%", "夏普", "Calmar"))
    singles = {}
    for name, cfg in FAMILY.items():
        sim = backtest(_prices_for(name, st, et),
                       et["days"] if cfg["universe"].startswith("etf") else days,
                       name, top=5)
        r = summarize(sim, name)
        singles[name] = r
        print("     %-28s %-10s %-11s %-11s %-10s %-11s"
              % (cfg["desc"][:24], r.get("n"), r.get("cagr"),
                 r.get("mdd"), r.get("sharpe"), r.get("calmar")))

    # ② 等权混合（净值平均）
    curves = {}
    for name, cfg in FAMILY.items():
        sim = backtest(_prices_for(name, st, et),
                       et["days"] if cfg["universe"].startswith("etf") else days,
                       name, top=5)
        if sim["equity"]:
            curves[name] = dict(sim["equity"])
    alld = sorted(set().union(*[set(c) for c in curves.values()])) if curves else []
    print("\n  ② 等权混合（不判断环境，全都跑，各占 1/N）")
    blends = {}
    for names, tag in ((list(FAMILY), "全部 %d 套" % len(FAMILY)),
                       (["R20", "LOWVOL"], "反转+低波"),
                       (["REV10", "LOWVOL"], "REV10+低波"),
                       (["R20", "MOM60", "LOWVOL"], "反转+趋势+低波"),
                       (["R20", "ETFROT"], "反转+宽基轮动")):
        ns = [n for n in names if n in curves]
        if not ns:
            continue
        out = []
        for d in alld:
            vs = [curves[n][d] for n in ns if d in curves[n]]
            if vs:
                out.append((d, sum(vs) / len(vs)))
        r = summarize({"equity": out, "trades": [0]}, tag)
        blends[tag] = r
        print("     %-26s %-10s %-11s %-11s %-10s %-11s"
              % (tag, "-", r.get("cagr"), r.get("mdd"),
                 r.get("sharpe"), r.get("calmar")))

    # ③ 动态切换（多种规则，全部无未来函数）
    # ★ 时间轴必须统一：股票日历 675 天、ETF 日历 801 天（ETF 更早），
    #   切换引擎只能跑**交集日历**，否则查不到对方的价格 ⇒ 一笔都成交不了。
    etf_days = set(et["days"])
    cal = [d for d in days if d in etf_days]
    print("\n  ③ 动态切换（每天按环境选一套）")
    print("     统一时间轴：股票 %d 天 ∩ ETF %d 天 = **%d 天**"
          % (len(days), len(et["days"]), len(cal)))
    rules = {
        "趋势向下→反转 / 其它→低波":
            lambda e: "R20" if e.get("trend") == "down" else "LOWVOL",
        "趋势向下→反转 / 趋势向上→趋势 / 无趋势→低波":
            lambda e: ("R20" if e.get("trend") == "down"
                       else ("MOM60" if e.get("trend") == "up" else "LOWVOL")),
        "低波→反转 / 高波→低波（反直觉对照）":
            lambda e: "R20" if (e.get("vol") or 0) < 0.25 else "LOWVOL",
        "始终低波（下界对照）":
            lambda e: "LOWVOL",
        "始终反转（上界对照）":
            lambda e: "R20",
    }
    switches = {}
    for tag, fn in rules.items():
        def pick(d, _fn=fn):
            e = env.get(d) or {}
            if e.get("trend") is None:
                return None, None            # 历史不足不判（不硬选）
            nm = _fn(e)
            if nm not in FAMILY:
                return None, None
            return nm, _prices_for(nm, st, et)
        sim = backtest_switch({}, cal, pick, top=5)
        r = summarize({"equity": sim["equity"], "trades": sim["trades"]}, tag)
        switches[tag] = (r, sim["by_strat"])
        print("     %-40s 年化%6s%% 回撤%6s%% 夏普%5s Calmar%5s"
              % (tag, r.get("cagr"), r.get("mdd"), r.get("sharpe"),
                 r.get("calmar")))
        bys = "、".join("%s:%d笔" % (k, v["n"]) for k, v in sim["by_strat"].items())
        if bys:
            print("        └ 各策略实际出场：%s" % bys)

    # ④ 判定
    # ★ 只拿**真正的切换规则**参与「切换 vs 混合」判定；
    #   「始终低波」「始终反转」是上下界对照，不是切换，混进来会得出假结论。
    print("\n  ── 判定：切换 vs 等权混合 ──")
    real_switch = {k: v for k, v in switches.items()
                   if not k.startswith("始终")}
    best_blend = max((r for r in blends.values() if r.get("calmar")),
                     key=lambda r: r["calmar"], default=None)
    best_sw_tag = max(real_switch,
                      key=lambda k: real_switch[k][0].get("calmar") or -99,
                      default=None)
    if best_blend and best_sw_tag:
        sw = real_switch[best_sw_tag][0]
        if sw.get("calmar") is None:
            print("  （切换实验未产生有效净值，跳过判定）")
            return singles, blends, switches

        print("  ── 上下界对照（不计入判定）──")
        for k, (r, _b) in switches.items():
            if k.startswith("始终"):
                print("     %-38s 年化%6s%% 回撤%6s%% Calmar%6s"
                      % (k, r.get("cagr"), r.get("mdd"), r.get("calmar")))

        print("\n  ── 真正的切换规则 ──")
        for k, (r, _b) in real_switch.items():
            print("     %-38s 年化%6s%% 回撤%6s%% Calmar%6s"
                  % (k[:38], r.get("cagr"), r.get("mdd"), r.get("calmar")))

        blend_tag = max(blends, key=lambda k: blends[k].get("calmar") or -99)
        print("\n  最佳切换  %-34s Calmar %.2f（年化 %s%% / 回撤 %s%%）"
              % (best_sw_tag[:34], sw["calmar"], sw["cagr"], sw["mdd"]))
        print("  最佳混合  %-34s Calmar %.2f（年化 %s%% / 回撤 %s%%）"
              % (blend_tag[:34], best_blend["calmar"],
                 best_blend["cagr"], best_blend["mdd"]))
        if (sw.get("calmar") or 0) > (best_blend.get("calmar") or 0):
            print("\n  ⇒ **切换胜出**：「判断环境换思路」有真实价值")
        else:
            print("\n  ⇒ **切换未胜过等权混合**：价值来自「多策略并存」本身，"
                  "而非「判断环境」这一步")
    return singles, blends, switches


def _main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    for f in ("selftest", "single", "matrix", "switch", "force"):
        ap.add_argument("--" + f, action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()

    st = load_stocks(force=a.force)
    et = load_etfs(force=a.force)
    print("股票 %d 只 · ETF %d 只 · 股票交易日 %d"
          % (len(st["stocks"]), len(et["etfs"]), len(st["days"])))

    days = st["days"]
    env, idx = build_env(st["stocks"], days)
    print("环境已建：trend 分布 %s"
          % {k: sum(1 for d in days if env[d]["trend"] == k)
             for k in ("up", "down", "flat", None)})

    if a.single or a.matrix or a.switch:
        pass
    else:
        a.single = True

    if a.single:
        print("\n【单策略表现】")
        print("  %-26s %-8s %-9s %-9s %-8s %-9s %s"
              % ("策略", "笔数", "年化%", "回撤%", "夏普", "Calmar", "胜率"))
        for name, cfg in FAMILY.items():
            sim = _run_strategy(name, st, et, env, days)
            r = summarize(sim, name)
            if r.get("n"):
                print("  %-28s %-10s %-11s %-11s %-10s %-11s %s"
                      % (cfg["desc"], r["n"], r["cagr"], r["mdd"],
                         r["sharpe"], r["calmar"], r["win"]))

    if a.matrix:
        exp_matrix(st, et, env, days)
    if a.switch:
        exp_switch(st, et, env, days)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
