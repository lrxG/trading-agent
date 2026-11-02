"""mr_study.py · 均值回归信号工程化研究（只读，纯计算，零落盘）。

目的：把「20 日跌幅超阈值」这个统计成立的信号，做成**能算出年化与最大回撤**的组合规则。

三层递进：
  L1 持有期优化：5/10/20/30/40 日，各自的笔均/日级 t/最大回撤
  L2 分散规则：每日命中多只时买几只（等权），组合层面日收益曲线
  L3 风控规则：止损（-8/-10/-15%）、极深跌幅上限、流动性门槛

关键口径（写死，防混淆）：
  · 信号只用 T 日及之前数据（20 日跌幅），T+1 开盘买入，零未来
  · 买入价 = T+1 open；若 T+1 一字（open==high==low==close）⇒ 视为买不进、剔除
  · 卖出价 = 买入后第 hold 个交易日 close
  · 止损：若持有期内某日 low 触及止损价 ⇒ 按止损价成交（保守：按 min(open, 止损价)）
  · 成本：买 0.125% / 卖 0.175%（同 backtest.params）
"""
import os
import sys
import io
import random
import statistics
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("analysis", "fs"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

from paths import ROOT           # noqa: E402
import backtest as BT            # noqa: E402


def load_trading_days(root=None, d1="2024-01-02", d2="2026-10-20"):
    """从 klines 读出真实交易日序列（升序）。

    ★ 这是资金模拟的正确时间轴：不能用「有候选的日期」当时间轴——
      2026 年每天都有候选、2024 年常有空档，混用会让「持有 20 日」错位成
      「持有 20 个候选日」，价格与日历脱钩（本件曾因此得出错误的逐年结论）。
    """
    import csv
    import io
    root = root or ROOT
    p = os.path.join(root, "data", "raw", "bus", "klines.csv")
    ds = set()
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            dt = r.get("date") or ""
            if d1 <= dt <= d2:
                ds.add(dt)
    return sorted(ds)


def load_mktcap(root=None):
    """逐日市值表 → {(code, date): 总市值(亿)}。

    来源 `data/raw/bus/mktcap.csv`（由 `fetch_mktcap.py` 用通达信逐日股本 × close 生成）。
    文件不存在时返回 {}，调用方须如实降级为「无市值特征」，不得伪造。
    """
    import csv
    root = root or ROOT
    p = os.path.join(root, "data", "raw", "bus", "mktcap.csv")
    if not os.path.isfile(p):
        return {}
    out = {}
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                out[(r["code"], r["date"])] = float(r["mktcap_yi"])
            except (TypeError, ValueError, KeyError):
                continue
    return out


def attach_mktcap(samples, cap_map):
    """给样本附加 `cap_yi`（决策日总市值，亿元）。缺失=none（不填中位数冒充）。"""
    n_miss = 0
    for s in samples:
        v = cap_map.get((s["code"], s["date"]))
        if v is None or v <= 0:
            s["cap_yi"] = None
            n_miss += 1
        else:
            s["cap_yi"] = round(v, 2)
    return n_miss


def load_industry_map(root=None):
    """个股 → 通达信一级行业（881xxx）。

    来源：`data/raw/meta/stock_industry.json`（由通达信 `get_stock_list_in_sector`
    逐板块抓取后固化）。128 个行业 · 5580 票 · 一股一行业无重叠 · 对 klines 覆盖 99.6%。

    缺失时返回 {}（调用方须如实降级为「无行业特征」，不得伪造）。
    """
    import json
    root = root or ROOT
    p = os.path.join(root, "data", "raw", "meta", "stock_industry.json")
    if not os.path.isfile(p):
        return {}
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)


def industry_rel_field(by_code, ind_map, min_members=5):
    """行业中相对偏离：个股 r20 − 同行业当日 r20 中位数（负 = 比行业跌更多）。

    → {date: {industry: median_r20}}
    ★ 零未来：只用当日及之前 20 日收盘。
    ★ 同业样本 < min_members 的行业当日不给基准（不拿 2 只票的中位数冒充行业）
    """
    import statistics
    agg = defaultdict(list)
    for code, rows in by_code.items():
        ind = ind_map.get(code)
        if not ind:
            continue
        for i in range(20, len(rows)):
            p0, p1 = rows[i - 20][4], rows[i][4]
            if p0 <= 0 or p1 <= 0:
                continue
            agg[rows[i][0]].append((ind, (p1 / p0 - 1.0) * 100))
    out = {}
    tmp = defaultdict(lambda: defaultdict(list))
    for d, pairs in agg.items():
        for ind, v in pairs:
            tmp[d][ind].append(v)
    for d, d2 in tmp.items():
        med = {}
        for ind, vs in d2.items():
            if len(vs) >= min_members:
                med[ind] = statistics.median(vs)
        if med:
            out[d] = med
    return out


def attach_rel(samples, ind_map, ind_med):
    """给样本附加 `rel` 字段（相对行业偏离）。缺基准的样本 rel=None（不填 0）。"""
    n_miss = 0
    for s in samples:
        ind = ind_map.get(s["code"])
        m = (ind_med.get(s["date"]) or {}).get(ind) if ind else None
        if m is None:
            s["rel"] = None
            n_miss += 1
        else:
            s["rel"] = round(s["r20"] - m, 2)
    return n_miss


def load_by_code(root=None, d1="2024-01-02", d2="2026-10-20"):
    """单遍读 klines → {code: [(date, o, h, l, c, amt), ...]} 按日期升序。

    比 by_date 组织快得多（只遍历一次全表），是 240 万行能算得动的前提。
    """
    import csv
    import io
    root = root or ROOT
    p = os.path.join(root, "data", "raw", "bus", "klines.csv")
    by_code = defaultdict(list)
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            dt = r.get("date") or ""
            if not (d1 <= dt <= d2):
                continue
            try:
                row = (dt, float(r["open"]), float(r["high"]),
                       float(r["low"]), float(r["close"]), float(r.get("amount") or 0))
            except (TypeError, ValueError, KeyError):
                continue
            if row[1] <= 0 or row[4] <= 0:
                continue
            by_code[r["code"]].append(row)
    for c in by_code:
        by_code[c].sort(key=lambda x: x[0])
    return by_code


def collect(by_code, drop=-0.15, hold_max=45, min_days=60):
    """采集所有满足「20日跌幅 <= drop」的样本，含完整持有期路径。

    → [{'date','code','r20','buy','days':[(o,h,l,c),...], 'amt5'}]
    `days` 从 T+1（买入口）起，长度 ≥ hold_max。
    一字买不进（open==high==low==close）直接剔除——不假装买到。
    """
    out = []
    n_pool = 0
    for code, rows in by_code.items():
        n = len(rows)
        if n < min_days + hold_max + 2:
            continue
        for i in range(min_days, n - hold_max - 1):
            dt, _o, _h, _l, px, _a = rows[i]
            p20 = rows[i - 20][4]
            if p20 <= 0:
                continue
            r20 = px / p20 - 1.0
            n_pool += 1
            if r20 > drop:
                continue
            nxt = rows[i + 1]
            bo = nxt[1]
            if bo <= 0:
                continue
            if nxt[1] == nxt[2] == nxt[3] == nxt[4]:      # 一字买不进
                continue
            days = [(rows[i + k][1], rows[i + k][2], rows[i + k][3], rows[i + k][4])
                    for k in range(1, hold_max + 1)]
            ddates = [rows[i + k][0] for k in range(1, hold_max + 1)]
            amt5 = sum(rows[j][5] for j in range(i - 4, i + 1)) / 5.0
            out.append({"date": dt, "code": code, "r20": round(r20 * 100, 2),
                        "buy": bo, "days": days, "ddates": ddates, "amt5": amt5})
    out.sort(key=lambda x: (x["date"], x["r20"]))
    return out, n_pool


def trade_ret(s, hold, stop_loss=None):
    """单笔净收益（%）。stop_loss=-0.10 表示跌破买入价 10% 止损。"""
    buy = s["buy"]
    days = s["days"]
    if hold > len(days):
        return None
    exit_px = days[hold - 1][3]
    if stop_loss is not None:
        floor = buy * (1 + stop_loss)
        for k in range(hold):
            o, h, l, c = days[k]
            if l <= floor:
                exit_px = min(o, floor) if o < floor else floor
                break
    net = ((exit_px * (1 - BT.COST_SELL)) / (buy * (1 + BT.COST_BUY)) - 1) * 100
    return round(net, 3)


def stat_block(vals):
    if not vals:
        return {"n": 0}
    w = [x for x in vals if x > 0]
    ls = [x for x in vals if x <= 0]
    aw = sum(w) / len(w) if w else 0.0
    al = sum(ls) / len(ls) if ls else 0.0
    s = sorted(vals)
    return {"n": len(vals), "win_rate": round(len(w) / len(vals) * 100, 1),
            "avg": round(sum(vals) / len(vals), 3),
            "median": round(s[len(s) // 2], 2),
            "pf": round(abs(aw / al), 2) if al else None,
            "worst": round(s[0], 2), "best": round(s[-1], 2)}


def daily_t(samples, hold, stop_loss=None):
    """日级：命中日日均收益 + t（vs 0）。"""
    byd = defaultdict(list)
    for s in samples:
        r = trade_ret(s, hold, stop_loss)
        if r is not None:
            byd[s["date"]].append(r)
    dm = [sum(v) / len(v) for v in byd.values()]
    if len(dm) < 3:
        return {"days": len(dm), "avg": None, "t": None}
    m = sum(dm) / len(dm)
    sd = statistics.stdev(dm)
    se = sd / len(dm) ** 0.5
    return {"days": len(dm), "avg": round(m, 3),
            "t": round(m / se, 2) if se else None,
            "pos_months": None}


def portfolio(samples, hold, top_n=5, stop_loss=None, max_drop=None,
              min_amt=None, capital_per=20000.0):
    """组合模拟：每日等权买入 top_n 只（按跌幅最深优先），持 hold 日。

    简化口径（如实声明）：按「每日新开仓 + 到期卖出」统计，同一资金不做复利再投，
    日收益 = 当日所有在场笔的加权净收益。用于比较规则优劣，不等于真实资金曲线。
    """
    byd = defaultdict(list)
    for s in samples:
        if max_drop is not None and s["r20"] < max_drop:
            continue
        if min_amt is not None and s["amt5"] < min_amt:
            continue
        byd[s["date"]].append(s)

    daily = []                      # (date, 当日所有在场笔平均收益)
    for d, lst in byd.items():
        lst.sort(key=lambda x: x["r20"])          # 跌最深优先
        picked = lst[:top_n]
        rets = [trade_ret(s, hold, stop_loss) for s in picked]
        rets = [r for r in rets if r is not None]
        if rets:
            daily.append((d, sum(rets) / len(rets), len(rets)))
    if not daily:
        return {"n_days": 0}
    daily.sort()
    rets = [x[1] for x in daily]
    m = sum(rets) / len(rets)
    sd = statistics.stdev(rets) if len(rets) > 1 else 0.0
    se = sd / len(rets) ** 0.5 if rets else 0.0
    # 净值曲线（每笔持有 hold 日，按日均收益累乘近似）
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1 + r / 100.0 * (1.0 / max(hold, 1)))   # 摊到每日
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    return {"n_days": len(daily), "avg_daily": round(m, 3),
            "t": round(m / se, 2) if se else None,
            "eq_total": round((eq - 1) * 100, 1),
            "mdd": round(mdd * 100, 1),
            "avg_holds": round(sum(x[2] for x in daily) / len(daily), 1)}


# ─────────────────────────── 自检 ───────────────────────────
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

    # 构造样本：买入 10 元，10 天后 11 元
    s = {"date": "2026-01-05", "code": "sh600000", "r20": -20.0, "buy": 10.0,
         "amt5": 1e8,
         "days": [(10.0, 10.5, 9.8, 10.2)] + [(10.2, 10.5, 10.0, 10.3)] * 9
                 + [(10.3, 11.2, 10.3, 11.0)] * 35}
    T("A1 hold=10 卖出价取第10日 close(10.3)",
      abs(trade_ret(s, 10) - ((10.3 * (1 - BT.COST_SELL) / (10.0 * (1 + BT.COST_BUY)) - 1) * 100)) < 0.01,
      str(trade_ret(s, 10)))
    T("A2 hold 超样本长度返回 None", trade_ret(s, 60) is None)

    # 止损：买入 10，第 3 日 low 8.9 触 -10% 线（9.0）
    s2 = {"date": "2026-01-05", "code": "x", "r20": -20.0, "buy": 10.0, "amt5": 1e8,
          "days": [(10.0, 10.1, 9.9, 10.0), (10.0, 10.1, 9.5, 9.6),
                   (9.6, 9.7, 8.9, 9.0)] + [(9.0, 9.1, 8.9, 9.0)] * 42}
    r = trade_ret(s2, 10, stop_loss=-0.10)
    # 第3日 open 9.6 > 9.0 ⇒ 按 9.0 成交
    T("A3 止损按 9.0 成交（open 高于止损价）",
      abs(r - ((9.0 * (1 - BT.COST_SELL) / (10.0 * (1 + BT.COST_BUY)) - 1) * 100)) < 0.01,
      str(r))
    # 跳空开在止损价下方 ⇒ 按 open 成交（更差）
    s3 = dict(s2)
    s3["days"] = [(10.0, 10.1, 9.9, 10.0), (8.5, 8.6, 8.4, 8.5)] + [(8.5, 8.6, 8.4, 8.5)] * 43
    r3 = trade_ret(s3, 10, stop_loss=-0.10)
    T("A4 跳空开低时按 open(8.5) 成交而非止损价",
      abs(r3 - ((8.5 * (1 - BT.COST_SELL) / (10.0 * (1 + BT.COST_BUY)) - 1) * 100)) < 0.01,
      str(r3))

    # 统计块
    st = stat_block([10.0, -5.0, 3.0, -1.0])
    T("B1 stat_block 胜率 50%", st["win_rate"] == 50.0, str(st))
    T("B2 stat_block pf = (10+3)/2 / (5+1)/2 = 2.17", st["pf"] == 2.17, str(st))

    # 组合：max_drop 过滤
    ss = [{"date": "d1", "code": "a", "r20": -30.0, "buy": 10.0, "amt5": 1e9,
           "days": [(10, 10, 10, 10)] * 45},
          {"date": "d1", "code": "b", "r20": -5.0, "buy": 10.0, "amt5": 1e9,
           "days": [(10, 10, 10, 10)] * 45}]
    pf = portfolio(ss, 10, top_n=5, max_drop=-20.0)
    T("C1 max_drop=-20 过滤掉 r20=-5 的样本", pf["n_days"] == 1, str(pf))

    # 一字买不进在 collect 层剔除
    dates = ["2026-%02d-%02d" % (1 + d // 28, 1 + d % 28) for d in range(130)]
    by_code = {"sh600001": [(d, 10.0, 10.0, 10.0, 10.0, 1e8) for d in dates]}
    got, n_pool = collect(by_code, drop=-0.15, hold_max=20)
    T("D1 横盘无跌幅 ⇒ 零样本（但池子有遍历）", len(got) == 0 and n_pool > 0,
      "%d/%d" % (len(got), n_pool))

    # 前 80 日 10.0，之后 7.0 ⇒ i=80 时 r20 = 7/10-1 = -30%（T=dates[80], T+1=dates[81]）
    def mk(px_at=None):
        seq = []
        for j, d in enumerate(dates):
            px = 10.0 if j < 80 else 7.0
            seq.append((d, px, px, px, px, 1e8))
        if px_at:
            k, (o, h, l, c) = px_at
            seq[k] = (dates[k], o, h, l, c, 1e8)
        return seq

    got2, _ = collect({"sh600002": mk((81, (7.7, 7.7, 7.7, 7.7)))},
                      drop=-0.15, hold_max=20)
    T("D2 命中但 T+1 一字 ⇒ 剔除（不假装买到）",
      all(s["date"] != dates[80] for s in got2), str(len(got2)))

    got3, _ = collect({"sh600003": mk((81, (7.05, 7.3, 7.0, 7.2)))},
                      drop=-0.15, hold_max=20)
    hit = [s for s in got3 if s["date"] == dates[80]]
    T("D3 正常可成交 ⇒ 命中且 buy=T+1 open",
      len(hit) == 1 and hit[0]["buy"] == 7.05, str(hit[:1]))

    # E 行业中相对偏离（G2 能力）
    imap = {"a": "881001.SH", "b": "881001.SH", "c": "881002.SH"}
    T("E1 load_industry_map 缺失时返回空 dict（不伪造）",
      isinstance(load_industry_map(root="Z:/nonexistent"), dict))
    # 造两只同行业票：a 跌 30%、b 跌 10% ⇒ 行业中位 -10% ⇒ a 的 rel = -20
    bd = {}
    dts = ["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28) for i in range(60)]
    for code, drop_from in (("a", 40), ("b", 50)):
        rows = []
        for j, d in enumerate(dts):
            px = 10.0 if j < drop_from else 7.0
            if code == "b" and j >= drop_from:
                px = 9.0
            rows.append((d, px, px, px, px, 1e8))
        bd[code] = rows
    med = industry_rel_field(bd, imap, min_members=2)
    last = dts[-1]
    T("E2 industry_rel_field 给出行业基准",
      last in med and "881001.SH" in med[last], str(med.get(last)))
    smp = [{"code": "a", "date": last, "r20": -30.0},
           {"code": "c", "date": last, "r20": -30.0}]
    n_miss = attach_rel(smp, imap, med)
    T("E3 attach_rel 计算 rel = r20 − 行业中位", smp[0]["rel"] is not None)
    T("E4 无行业基准的样本 rel=None（不填 0 冒充）",
      smp[1]["rel"] is None and n_miss == 1, str(smp[1]))

    # F 市值特征（G1 能力）
    T("F1 load_mktcap 缺失时返回空 dict（不伪造）",
      load_mktcap(root="Z:/nonexistent") == {})
    cm = {("sh600178", "2024-01-02"): 47.0}
    s1 = [{"code": "sh600178", "date": "2024-01-02"},
          {"code": "sh600178", "date": "2024-01-03"}]
    miss2 = attach_mktcap(s1, cm)
    T("F2 attach_mktcap 命中填 cap_yi", s1[0]["cap_yi"] == 47.0, str(s1[0]))
    T("F3 缺失样本 cap_yi=None 且计入缺失数",
      s1[1]["cap_yi"] is None and miss2 == 1, str(s1[1]))
    # 零/负市值视为缺失（不用 0 冒充「小盘」）
    s2 = [{"code": "x", "date": "d"}]
    attach_mktcap(s2, {("x", "d"): 0.0})
    T("F4 零市值当缺失处理（不冒充小盘）", s2[0]["cap_yi"] is None, str(s2[0]))

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
