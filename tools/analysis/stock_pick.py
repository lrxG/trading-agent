# -*- coding: utf-8 -*-
"""stock_pick.py —— 环境自适应选票

为什么改成这样：
    之前是「找一个固定的筛子一直用」，这是机械化。
    `ml_21_regime_pick.py` 实测：同一套策略在不同环境下方向完全不同 ——
        S1 低位反弹：多头·普涨 +1.79%(t=27.3) / 空头·分化 -0.19% / 空头·冰点 -0.29%
        S3 涨停追涨：多头·普涨 -1.23%(t=-10.3) / 空头·普涨 +0.85%
    混在一起统计会把有效策略平均成无效（这就是上一轮报「2026 全部归零」的原因）。

本模块做三件事：
    1. 先判环境（D 日收盘可得，无未来函数）
    2. 按环境查策略表
    3. 该环境没有正期望策略时，明确输出「空仓」，不硬选

环境 = 偏置(等权指数 vs MA20) × 广度(当日上涨家数占比)
    偏置 >0 多头 / <=0 空头；广度 <35% 冰点 / 35~55% 分化 / 55~75% 偏强 / >75% 普涨

⚠ 2026-11-09 降级声明（必读）：
    ml_24_cluster.py 实测：本文件所用 S1~S5 与 ml_21 环境矩阵**均未做交易日聚类**，
    同一天几十只票被当成独立样本，t 值被放大一个量级。
    对照实证：超跌形态④ 个股层面 alpha +1.49%/t=15.81，按日聚类后 −0.80%/t=−1.51，
    **符号翻转**。因此本文件 REGIME_MAP / MATRIX 中的一切数字降级为「待验」，
    不得作为下单依据；`--scan` 结果只作观察池，不作买点清单。
    修法：所有回测路径加 groupby(date) 聚合后重算，见 trade-rules §一e.6。

策略库（见 ml_18/19/21，数字待验）：
    S1 低位反弹   p20<-10% 且 当日 +3~7%
    S2 强势回踩   p20>+20% 且 当日 -5~-1%
    S3 涨停追涨   当日涨停
    S4 缩量回调   当日 -5~-1% 且 量比<0.8 且 p20>0
    S5 中期动量   p20>+20% 且 当日 0~3% 且 站上 MA20

用法：
    python tools/analysis/stock_pick.py --judge                 # 只判环境与该用哪个策略
    python tools/analysis/stock_pick.py --scan --date 2026-11-06
    python tools/analysis/stock_pick.py --backtest --strategy S1
    python tools/analysis/stock_pick.py --matrix                # 环境×策略矩阵
"""
import argparse
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KLINES = os.path.join(ROOT, "data", "raw", "bus", "klines.csv")
COST = 0.0015

# ── 策略表（来源 ml_21_regime_pick.py，2026 年净收益，只保留 >+0.20%）────
REGIME_MAP = {
    "多头·普涨": ("S1", "+0.91%", "59.2%", 2072),
    "多头·偏强": ("S1", "+3.09%", "70.2%", 2415),
    "多头·分化": ("S1", "+0.45%", "48.0%", 810),
    "空头·普涨": ("S1", "+0.49%", "56.7%", 7035),
    "多头·冰点": None,   # 2026 最优 S2 也只有 -0.05%
    "空头·偏强": None,   # 2026 最优 S2 仅 +0.16%，不足覆盖滑点
    "空头·分化": None,   # 2026 最优 S3 也是 -0.56%
    "空头·冰点": None,   # 2026 最优 S2 仅 +0.13%
}

# 全样本矩阵（ml_21，净收益 %）—— 证明同一策略在不同环境翻向
MATRIX = {
    "多头·普涨": (+1.79, -0.20, -1.23, +0.11),
    "多头·偏强": (+1.63, +0.06, +0.17, -0.06),
    "多头·分化": (+0.32, +0.39, -0.29, +0.16),
    "多头·冰点": (+0.16, +0.63, -0.60, +0.11),
    "空头·普涨": (+1.12, -0.73, +0.85, -0.16),
    "空头·偏强": (+0.70, +0.25, -0.23, -0.02),
    "空头·分化": (-0.19, -0.77, -0.91, -0.60),
    "空头·冰点": (-0.29, +0.07, -0.57, +0.04),
}


def load_klines():
    out = {}
    with io.open(KLINES, encoding="utf-8") as f:
        hdr = f.readline().rstrip("\n").split(",")
        ix = {k: i for i, k in enumerate(hdr)}
        for ln in f:
            p = ln.rstrip("\n").split(",")
            if len(p) < len(hdr):
                continue
            try:
                o, c, h, l = (float(p[ix["open"]]), float(p[ix["close"]]),
                              float(p[ix["high"]]), float(p[ix["low"]]))
                v, a = float(p[ix["volume"]]), float(p[ix["amount"]])
                pct = float(p[ix["change_pct"]])
            except (ValueError, IndexError):
                continue
            if o <= 0 or c <= 0 or v <= 0:
                continue
            out.setdefault(p[ix["code"]], []).append(
                {"date": p[ix["date"]], "name": p[ix["name"]], "o": o, "c": c,
                 "h": h, "l": l, "v": v, "a": a, "pct": pct})
    for k in out:
        out[k].sort(key=lambda x: x["date"])
    return out


def market_env(kl):
    """逐日环境序列（只用当日及之前）：breadth / dev / mom20"""
    dates = set()
    up, tot, sp = {}, {}, {}
    for v in kl.values():
        for b in v:
            d = b["date"]
            dates.add(d)
            tot[d] = tot.get(d, 0) + 1
            sp[d] = sp.get(d, 0) + b["pct"]
            if b["pct"] > 0:
                up[d] = up.get(d, 0) + 1
    seq = []
    eq = 1.0
    for d in sorted(dates):
        if not tot.get(d):
            continue
        breadth = up.get(d, 0) / tot[d]
        mean_pct = sp[d] / tot[d] / 100.0
        eq *= (1 + mean_pct)
        seq.append({"date": d, "breadth": breadth, "eq": eq})
    eqs = [x["eq"] for x in seq]
    for i, x in enumerate(seq):
        if i >= 20:
            x["dev"] = x["eq"] / (sum(eqs[i - 19:i + 1]) / 20.0) - 1
            x["mom20"] = eqs[i] / eqs[i - 20] - 1
        else:
            x["dev"] = 0.0
            x["mom20"] = 0.0
        x["env"] = env_label(x["dev"], x["breadth"])
    return seq


def env_label(dev, breadth):
    side = "多头" if dev > 0 else "空头"
    if breadth < 0.35:
        lvl = "冰点"
    elif breadth < 0.55:
        lvl = "分化"
    elif breadth < 0.75:
        lvl = "偏强"
    else:
        lvl = "普涨"
    return side + "·" + lvl


def feats(series, i):
    if i < 21:
        return None
    b = series[i]
    c20 = series[i - 20]["c"]
    if c20 <= 0:
        return None
    win = series[max(0, i - 5):i]
    v5 = sum(x["v"] for x in win) / len(win) if win else 0
    win60 = series[max(0, i - 59):i + 1]
    hi60 = max(x["h"] for x in win60)
    ma20 = sum(x["c"] for x in series[i - 19:i + 1]) / 20.0
    return {"p20": b["c"] / c20 - 1, "dd60": b["c"] / hi60 - 1,
            "ma20dev": b["c"] / ma20 - 1,
            "vr": b["v"] / v5 if v5 > 0 else None,
            "pct": b["pct"]}


def cond(strategy, f):
    if f is None:
        return False
    if strategy == "S1":
        return f["p20"] < -0.10 and 3 <= f["pct"] <= 7
    if strategy == "S2":
        return f["p20"] > 0.20 and -5 <= f["pct"] <= -1
    if strategy == "S3":
        return f["pct"] >= 9.5
    if strategy == "S4":
        return (-5 <= f["pct"] <= -1) and f["vr"] is not None and f["vr"] < 0.8 and f["p20"] > 0
    if strategy == "S5":
        return f["p20"] > 0.20 and 0 <= f["pct"] <= 3 and f["ma20dev"] > 0
    return False


def score(f):
    """四维分开打分，不合成总分（避免同源叠加）"""
    P = (1 if f["p20"] <= -0.20 else 0) + (1 if f["dd60"] <= -0.30 else 0)
    F = 0
    if f["vr"] and f["vr"] >= 2.0:
        F += 1
    if f["ma20dev"] > -0.05:
        F += 1
    return P, F


def recent_perf(kl, seq, days=60):
    """近 N 个交易日重估：当前环境下各策略的净收益。
    存在的理由：环境→策略 的映射本身逐年翻转（S1 在多头·偏强：
    2024 -2.84% → 2025 +0.60% → 2026 +3.09%），所以映射表不能用三年前的数据定，
    必须用近端数据滚动重估。"""
    win = [x["date"] for x in seq[-days:]]
    wset = set(win)
    cur = seq[-1]["env"]
    envmap = {x["date"]: x["env"] for x in seq}
    buckets = {}
    for code, ser in kl.items():
        for i in range(21, len(ser) - 6):
            d = ser[i]["date"]
            if d not in wset:
                continue
            f = feats(ser, i)
            if f is None:
                continue
            buy = ser[i + 1]["o"]
            if buy <= 0:
                continue
            r = ser[i + 2]["c"] / buy - 1
            e = envmap.get(d, "?")
            for st in ("S1", "S2", "S3", "S4", "S5"):
                if cond(st, f):
                    buckets.setdefault((e, st), []).append(r)
                    buckets.setdefault((e, st, "cur"), []).append(r) if e == cur else None
    return buckets, cur, win[0], win[-1]


def judge(seq, date=None):
    if date:
        hit = [x for x in seq if x["date"] == date]
        x = hit[0] if hit else seq[-1]
    else:
        x = seq[-1]
    rec = REGIME_MAP.get(x["env"])
    print("环境判定（%s 收盘，D+1 开盘执行）" % x["date"])
    print("  上涨占比 %.1f%% ｜ 等权距MA20 %+.2f%% ｜ 20日动量 %+.2f%%"
          % (x["breadth"] * 100, x["dev"] * 100, x["mom20"] * 100))
    print("  → 环境 = %s" % x["env"])
    if rec is None:
        print("  → 策略：空仓（该环境 2026 无正期望策略，最优者净收益 < +0.20%）")
    else:
        print("  → 策略：%s（2026 净 %s，胜率 %s，样本 %d）" % rec)
    print("")
    print("对照：该环境下各策略全样本净收益")
    print("  %-12s %9s %9s %9s %9s" % ("环境", "S1低位", "S2回踩", "S3涨停", "S4缩量"))
    for k, v in MATRIX.items():
        mark = "  <当前" if k == x["env"] else ""
        print("  %-12s %+8.2f%% %+8.2f%% %+8.2f%% %+8.2f%%%s" % (k, v[0], v[1], v[2], v[3], mark))
    return x, rec


def judge_recent(kl, seq, days=60):
    """近端重估：不用三年前的映射，用最近 N 天决定当前该用哪个策略"""
    buckets, cur, d0, d1 = recent_perf(kl, seq, days)
    print("")
    print("近 %d 个交易日重估（%s ~ %s）· 当前环境 %s" % (days, d0, d1, cur))
    print("  %-8s %8s %9s %8s" % ("策略", "样本", "净收益", "胜率"))
    best = None
    for st in ("S1", "S2", "S3", "S4", "S5"):
        v = buckets.get((cur, st), [])
        if len(v) < 20:
            continue
        m = sum(v) / len(v)
        w = sum(1 for z in v if z > 0) / len(v)
        print("  %-8s %8d %+8.2f%% %7.1f%%" % (st, len(v), (m - COST) * 100, w * 100))
        if best is None or m > best[1]:
            best = (st, m)
    if best is None:
        print("  近端该环境下各策略样本均不足 20，无法重估 → 按空仓处理")
    else:
        ok = (best[1] - COST) > 0.002
        print("  → 近端最优 %s 净 %+.2f%% → %s"
              % (best[0], (best[1] - COST) * 100,
                 "可用" if ok else "仍不足覆盖成本，空仓"))


def scan(kl, seq, date, recent=60):
    """先判环境，再用「近端重估」定策略（不用三年前的死表）"""
    env = [x for x in seq if x["date"] == date]
    if not env:
        print("环境序列中没有 %s（最新 %s）" % (date, seq[-1]["date"]))
        return
    e = env[0]
    cur = e["env"]
    rec = REGIME_MAP.get(cur)
    print("环境 %s（上涨占比 %.1f%%，距MA20 %+.2f%%）"
          % (cur, e["breadth"] * 100, e["dev"] * 100))

    # 近端重估：把 seq 截到 date，用最近 N 日该环境的表现定策略
    sub_seq = [x for x in seq if x["date"] <= date]
    buckets, _, d0, d1 = recent_perf(kl, sub_seq, recent)
    best = None
    for st0 in ("S1", "S2", "S3", "S4", "S5"):
        v = buckets.get((cur, st0), [])
        if len(v) >= 20:
            m = sum(v) / len(v)
            if best is None or m > best[1]:
                best = (st0, m, len(v))
    print("  固定表（2026全年）：%s ｜ 近%d日重估（%s~%s）：%s"
          % (rec[0] if rec else "空仓", recent, d0, d1,
             ("%s 净%+.2f%%(n=%d)" % (best[0], (best[1] - COST) * 100, best[2]))
             if best else "样本不足"))
    if best is None or (best[1] - COST) <= 0.002:
        print("  → 无可用策略，不出候选（近端最优也不足覆盖成本）")
        return
    st = best[0]
    if rec is None:
        print("  -> 固定表为空仓，但近端重估给出 %s，以近端为准" % st)
    elif rec[0] != st:
        print("  → 取近端结论 %s（与固定表 %s 冲突，以近端为准）" % (st, rec[0]))
    else:
        print("  → 策略 %s（固定表与近端一致）" % st)
    hits = []
    for code, ser in kl.items():
        idx = None
        for i in range(len(ser) - 1, -1, -1):
            if ser[i]["date"] == date:
                idx = i
                break
        if idx is None:
            continue
        f = feats(ser, idx)
        if not cond(st, f):
            continue
        P, F = score(f)
        hits.append((code, ser[idx]["name"], f, P, F))
    if not hits:
        print("%s 在 %s 下无符合标的" % (date, st))
        return
    hits.sort(key=lambda x: (-(x[3] + x[4]), x[2]["p20"]))
    print("%s 入选 %d 只\n" % (date, len(hits)))
    print("%-10s %-9s %8s %7s %6s %7s %6s %3s %3s" %
          ("code", "name", "p20", "今日", "量比", "距MA20", "距60高", "P", "F"))
    for code, name, f, P, F in hits[:15]:
        print("%-10s %-9s %+7.1f%% %+6.1f%% %6.2f %+6.1f%% %+5.1f%% %3d %3d" %
              (code, name[:8], f["p20"] * 100, f["pct"], f["vr"] or 0,
               f["ma20dev"] * 100, f["dd60"] * 100, P, F))


def backtest(kl, seq, strategy, by_env=True):
    envmap = {x["date"]: x["env"] for x in seq}
    base = {}
    for code, ser in kl.items():
        for i in range(21, len(ser) - 6):
            o = ser[i + 1]["o"]
            if o <= 0:
                continue
            base.setdefault(ser[i]["date"], []).append(ser[i + 2]["c"] / o - 1)
    rows = []
    for code, ser in kl.items():
        for i in range(21, len(ser) - 6):
            f = feats(ser, i)
            if not cond(strategy, f):
                continue
            buy = ser[i + 1]["o"]
            if buy <= 0:
                continue
            rows.append({"year": ser[i]["date"][:4],
                         "env": envmap.get(ser[i]["date"], "?"),
                         "r1": ser[i + 2]["c"] / buy - 1})
    if not rows:
        print("无样本")
        return
    print("%s 共 %d 笔\n" % (strategy, len(rows)))
    if by_env:
        print("%-12s %-6s %7s %9s %9s %8s" % ("环境", "年份", "样本", "实得", "净收益", "胜率"))
        for e in sorted(set(r["env"] for r in rows)):
            for y in ["2024", "2025", "2026", "ALL"]:
                sub = [r for r in rows if r["env"] == e and (y == "ALL" or r["year"] == y)]
                if len(sub) < 40:
                    continue
                v = [r["r1"] for r in sub]
                m = sum(v) / len(v)
                print("%-12s %-6s %7d %+8.2f%% %+8.2f%% %7.1f%%" %
                      (e, y, len(v), m * 100, (m - COST) * 100,
                       sum(1 for x in v if x > 0) / len(v) * 100))
    else:
        for y in ["2024", "2025", "2026", "ALL"]:
            sub = rows if y == "ALL" else [r for r in rows if r["year"] == y]
            if not sub:
                continue
            v = [r["r1"] for r in sub]
            m = sum(v) / len(v)
            print("%-6s n=%5d 实得%+7.2f%% 净%+7.2f%% 胜率%5.1f%%" %
                  (y, len(v), m * 100, (m - COST) * 100,
                   sum(1 for x in v if x > 0) / len(v) * 100))


def print_matrix():
    print("%-12s %9s %9s %9s %9s %8s" %
          ("环境", "S1低位", "S2回踩", "S3涨停", "S4缩量", "2026该用"))
    for k, v in MATRIX.items():
        rec = REGIME_MAP.get(k)
        print("%-12s %+8.2f%% %+8.2f%% %+8.2f%% %+8.2f%% %8s" %
              (k, v[0], v[1], v[2], v[3], rec[0] if rec else "空仓"))
    print("")
    print("左四列 = 全样本(2024-2026)净收益；最后一列 = 2026 单年结论。")
    print("两者不一致说明市场结构在变 —— 用 2026 列，不用全样本列。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--matrix", action="store_true")
    ap.add_argument("--date")
    ap.add_argument("--strategy", default="S1")
    ap.add_argument("--mix", action="store_true", help="回测时不按环境拆分")
    ap.add_argument("--recent", type=int, default=60, help="近端重估窗口，默认 60 日")
    a = ap.parse_args()
    kl = load_klines()
    seq = market_env(kl)
    if a.matrix:
        print_matrix()
    elif a.judge:
        judge(seq, a.date)
        judge_recent(kl, seq, a.recent)
    elif a.scan:
        if not a.date:
            print("需要 --date（最新交易日：%s）" % seq[-1]["date"])
            return 2
        scan(kl, seq, a.date, a.recent)
    elif a.backtest:
        backtest(kl, seq, a.strategy, by_env=not a.mix)
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
