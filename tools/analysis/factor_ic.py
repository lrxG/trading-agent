#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""factor_ic.py · 横截面因素 IC 研究（2026-10-29 建 · 回应：行情再差也有涨的，研究涨跌背后因素）

## 方法

每月末 t：对全过滤池每只票计算 14 个因素值 → 与次月收益（winsorize −33%~+300%）算 Spearman 秩相关（IC）。
IC>0 = 因子值越大下月越涨。聚合：全样本 IC 均值/ICIR/胜率，并按环境分组（等权全A>MA20 的月 vs <）——
直接回答"什么因素在什么行情里驱动上涨"。

## 因子表（全部月末时点可得，零未来）

| 组 | 因子 | 定义 | 方向直觉 |
|---|---|---|---|
| 动量 | ret5/10/20/60 | 区间收益 | A股短期反转→预期负 |
| 量价 | vr5 | 量比=当日量/前5日均量 | 文献：温和放量有效 |
| 量价 | shrink_up | 近10日"价涨量缩"天数占比 | 缩量上涨=筹码锁定 |
| 量价 | expand_down | 近10日"放量下跌"天数占比 | 恐慌抛售 |
| 量价 | amp20 | 20日振幅 | 高波动=博弈烈度 |
| 位置 | pos60 | 收盘在60日区间的位置 | 低位置弹性 |
| 位置 | dist_high20 | 收盘/20日最高−1 | 接近新高=动量 |
| 位置 | boards10 | 近10日涨停次数 | 涨停基因 |
| 流动性 | turnover20 | 20日均换手率 | 低换手异象→预期负 |
| 流动性 | amt20 | 20日均成交额 | 流动性溢价 |
| 规模 | lfmv | ln(流通市值) | 小市值→预期负 |
| 风险 | vol20 | 20日收益波动率 | 低波异象→预期负 |

## 用法
python tools/analysis/factor_ic.py            # 全表
python tools/analysis/factor_ic.py --selftest
"""
from __future__ import annotations

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from factor_monthly import _load, month_ends  # noqa: E402

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

FACTORS = ("ret5", "ret10", "ret20", "ret60", "vr5", "shrink_up", "expand_down",
           "amp20", "pos60", "dist_high20", "boards10", "turnover20", "amt20", "lfmv", "vol20")


def _spearman(xs, ys):
    """秩相关（含并列取平均秩）。"""
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if vx == 0 or vy == 0:
        return 0.0
    return cov / (vx * vy)


def build(px, vols_by, fs, dates, names):
    """px: {code:{date:(close,amount,high,low)}}; vols_by: {code:{date:volume}}"""
    idx = {d: i for i, d in enumerate(dates)}
    me = month_ends(dates)
    out = []
    for k in range(len(me) - 1):
        m, d = me[k]
        nm, nd = me[k + 1]
        i = idx[d]
        if i < 60:
            continue
        recs = []
        for c, m_ in px.items():
            row = m_.get(d)
            if not row:
                continue
            close, amount, high, low = row
            nm_ = names.get(c, "")
            if close < 2.0 or "ST" in nm_.upper() or "退" in nm_:
                continue
            fshare = fs.get(c, {}).get(d)
            if not fshare or fshare <= 0:
                continue
            hist = sorted(k2 for k2 in m_ if k2 <= d)
            if len(hist) < 61:
                continue
            closes = [m_[k2][0] for k2 in hist]
            amts = [m_[k2][1] for k2 in hist]
            highs = [m_[k2][2] for k2 in hist]
            lows = [m_[k2][3] for k2 in hist]
            vols = [vols_by.get(c, {}).get(k2, 0) for k2 in hist]
            if min(closes[-21:]) <= 0 or min(vols[-11:]) <= 0:
                continue
            nxrow = px.get(c, {}).get(nd)
            if not nxrow or nxrow[0] <= 0:
                continue
            fwd = nxrow[0] / close - 1
            if fwd < -0.33 or fwd > 3.0:
                continue

            def ret(n):
                return (closes[-1] / closes[-1 - n] - 1) * 100 if closes[-1 - n] > 0 else None

            vma5 = sum(vols[-6:-1]) / 5
            vr5 = vols[-1] / vma5 if vma5 > 0 else None
            su = ed = 0
            for j in range(len(closes) - 10, len(closes)):
                if j == 0:
                    continue
                up = closes[j] > closes[j - 1]
                dv = vols[j] > vols[j - 1]
                if up and not dv:
                    su += 1
                if (not up) and dv:
                    ed += 1
            hi20, lo20 = max(highs[-20:]), min(lows[-20:])
            hi60, lo60 = max(closes[-60:]), min(closes[-60:])
            rets = [(closes[k] / closes[k - 1] - 1) for k in range(len(closes) - 20, len(closes)) if closes[k - 1] > 0]
            mu = sum(rets) / len(rets)
            vol20 = math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) * 100
            amt20 = sum(amts[-20:]) / 20
            boards10 = sum(1 for j in range(len(closes) - 10, len(closes))
                           if closes[j] / closes[j - 1] - 1 >= (0.05 if "ST" in nm_.upper() else 0.095) - 0.005)
            recs.append({
                "code": c, "fwd": fwd * 100,
                "ret5": ret(5), "ret10": ret(10), "ret20": ret(20), "ret60": ret(60),
                "vr5": vr5,
                "shrink_up": su / 10 * 100, "expand_down": ed / 10 * 100,
                "amp20": (hi20 - lo20) / lo20 * 100 if lo20 > 0 else None,
                "pos60": (close - lo60) / (hi60 - lo60) * 100 if hi60 > lo60 else None,
                "dist_high20": (close / hi20 - 1) * 100,
                "boards10": boards10,
                "turnover20": (sum(vols[-20:]) / 20) / fshare * 100,
                "amt20": amt20, "lfmv": math.log(close * fshare),
                "vol20": vol20,
            })
        out.append({"month": m, "date": d, "next": nd, "recs": recs})
    return out


def aggregate(snaps, env_series, dates):
    """→ {factor: {all/ON/OFF: (ic_mean, icir, win, n)}}；环境=月末等权全A vs MA20。"""
    eq = []
    px_by = {}
    acc = {f: {"all": [], "ON": [], "OFF": []} for f in FACTORS}
    for si, sn in enumerate(snaps):
        if len(sn["recs"]) < 200:
            continue
        env_on = env_series.get(sn["date"], None)
        for f in FACTORS:
            xs, ys = [], []
            for r in sn["recs"]:
                v = r.get(f)
                if v is None:
                    continue
                xs.append(v)
                ys.append(r["fwd"])
            if len(xs) < 200:
                continue
            ic = _spearman(xs, ys)
            acc[f]["all"].append(ic)
            if env_on is not None:
                acc[f]["ON" if env_on else "OFF"].append(ic)
    res = {}
    for f in FACTORS:
        res[f] = {}
        for g in ("all", "ON", "OFF"):
            v = acc[f][g]
            if not v:
                res[f][g] = None
                continue
            mu = sum(v) / len(v)
            sd = math.sqrt(sum((x - mu) ** 2 for x in v) / (len(v) - 1)) if len(v) > 1 else 0
            res[f][g] = {"ic": round(mu, 4), "icir": round(mu / sd, 2) if sd else 0,
                         "win": round(sum(1 for x in v if x > 0) / len(v) * 100, 1), "n": len(v)}
    return res


def _selftest():
    assert abs(_spearman([1, 2, 3, 4], [10, 20, 30, 40]) - 1.0) < 1e-9
    assert abs(_spearman([1, 2, 3, 4], [40, 30, 20, 10]) + 1.0) < 1e-9
    print("factor_ic selftest OK")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    dates, px, fs, names = _load()
    vols_by = {}
    import csv as _csv
    import io as _io
    with _io.open(os.path.join(_ROOT, "data", "raw", "bus", "klines.csv"), encoding="utf-8-sig") as f:
        for r in _csv.DictReader(f):
            try:
                vols_by.setdefault(r["code"], {})[r["date"]] = float(r["volume"])
            except (KeyError, TypeError, ValueError):
                continue
    # 环境序列：等权全A收盘 vs MA20
    eq = []
    for d in dates:
        cl = [v[0] for v in px_all(px, d)]
        eq.append(sum(cl) / len(cl) if cl else None)
    win, ma20 = [], {}
    for i, v in enumerate(eq):
        if v is None:
            continue
        win.append(v)
        if len(win) > 20:
            win.pop(0)
        if len(win) == 20:
            ma20[dates[i]] = sum(win) / 20.0
    env_series = {d: (eq[i] is not None and ma20.get(d) is not None and eq[i] > ma20[d])
                  for i, d in enumerate(dates)}
    snaps = build(px, vols_by, fs, dates, names)
    res = aggregate(snaps, env_series, dates)
    print("因素IC研究 · %d 个月末截面（%s ~ %s）· 环境分组：ON=等权全A>MA20 月末" % (
        len(snaps), snaps[0]["month"], snaps[-1]["month"]))
    print()
    print("| 因子 | 全期IC | ICIR | 胜率% | ON月IC(n) | OFF月IC(n) | 解读 |")
    print("|---|---|---|---|---|---|---|")
    for f in FACTORS:
        r = res[f]
        if not r.get("all"):
            continue
        a, on, off = r["all"], r.get("ON"), r.get("OFF")
        note = "涨" if a["ic"] > 0.02 else ("跌" if a["ic"] < -0.02 else "平")
        print("| %s | %+.4f | %.2f | %.0f | %s | %s | %s |" % (
            f, a["ic"], a["icir"], a["win"],
            ("%.4f(%d)" % (on["ic"], on["n"])) if on else "—",
            ("%.4f(%d)" % (off["ic"], off["n"])) if off else "—", note))
    return 0


def px_all(px, d):
    for c, m_ in px.items():
        v = m_.get(d)
        if v:
            yield v


if __name__ == "__main__":
    sys.exit(main())
