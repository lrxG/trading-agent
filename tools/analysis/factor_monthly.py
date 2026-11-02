#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""factor_monthly.py · 月频因子回测引擎（2026-10-29 建 · 外部经验验证批次 #1）

## 来源与改造（学习反哺流程：调研→提炼思路→自建口径→回测→立账结算）

调研来源（2026-10-29，关键词：小市值/短期反转/低波/低换手）：
- 果仁网微盘轮动（5年472%）· 兴业证券微盘择时（宽度+边际资金，2013-2026 年化43.5%）
- 源达金工规模因子十年（小市值+盈利/估值门槛）· 海通动量反转研究（A股无动量、短期反转显著）
- 开源交易行为因子月报 · 雪球/腾讯社区低波低换手多源 · GitHub quant_v9.1（周频小盘反转+广度择时）

**改造点（非照抄）**：①口径全部用本系统数据自建（klines+mktcap）②统一质量/流动性过滤
③叠加本系统已验证的择时闸（等权全A vs MA20）做环境过滤变体 ④输出与 backtest.py 同族统计。

## 因子（月末截面）

- smallcap   流通市值最小（close×float_share 升序）
- reversal20 20 日收益最低（A股短期反转：弱者逆袭，方向=升序取最小）
- lowvol20   20 日日收益标准差最低（低波异象）

## 统一过滤（月末时点可得，零未来）

非 ST/退 · 收盘≥2元 · 20日均成交额≥3000万 · 上市历史≥60交易日 · float_share>0

## 持有与成本

月末收盘选股等权 20 只 → 持有至下月末收盘（close-to-close）→ 换手部分计 COST_BUY/COST_SELL（与 backtest.py 同源）。
基准 = 同过滤池等权月均（分离因子 alpha 与市场 β）。

## 用法

python tools/analysis/factor_monthly.py --factor smallcap [--env-gate] [--n 20]
python tools/analysis/factor_monthly.py --selftest
"""
from __future__ import annotations

import argparse
import csv
import datetime
import io
import math
import os
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
COST_BUY = 0.001   # 与 backtest.py 同源口径（params 缺省时用此保守值）
COST_SELL = 0.0015
FACTORS = ("smallcap", "reversal20", "lowvol20", "expanddn10")


def _load(root=None):
    """→ (dates 升序, px{code:{date:(close,amount,high,low)}}, fs{code:{date:float_share}}, names{code:name})"""
    root = root or _ROOT
    px, fs, names = {}, {}, {}
    with io.open(os.path.join(root, "data", "raw", "bus", "klines.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d, c = r.get("date"), r.get("code")
            if not d or not c:
                continue
            try:
                px.setdefault(c, {})[d] = (float(r["close"]), float(r.get("amount") or 0),
                                           float(r["high"]), float(r["low"]), float(r.get("volume") or 0))
            except (TypeError, ValueError):
                continue
            names.setdefault(c, r.get("name") or "")
    p2 = os.path.join(root, "data", "raw", "bus", "mktcap.csv")
    if os.path.isfile(p2):
        with io.open(p2, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                try:
                    fs.setdefault(r["code"], {})[r["date"]] = float(r["float_share"])
                except (KeyError, TypeError, ValueError):
                    continue
    dates = sorted({d for m in px.values() for d in m})
    return dates, px, fs, names


def month_ends(dates):
    """每月最后一个交易日 → [(month, date)]"""
    out = {}
    for d in dates:
        out[d[:7]] = d
    return sorted(out.items())


def build_snapshots(px, dates, fs):
    """月末截面：对每个月末 t，算每只票的因子与过滤字段（零未来：只用 ≤t 数据）。"""
    idx = {d: i for i, d in enumerate(dates)}
    snaps = []
    for m, d in month_ends(dates):
        i = idx[d]
        if i < 60:
            continue
        recs = []
        for c, m_ in px.items():
            row = m_.get(d)
            if not row:
                continue
            close, amount, high, low, vol_today = row
            name = ""
            if close < 2.0 or "ST" in name.upper() or "退" in name:
                continue
            fshare = fs.get(c, {}).get(d)
            if not fshare or fshare <= 0:
                continue
            # 历史 20 日（该股自身交易日）
            hist = sorted(k for k in m_ if k <= d)[-21:]
            if len(hist) < 21:
                continue
            closes = [m_[k][0] for k in hist]
            amts = [m_[k][1] for k in hist]
            vv = [m_[k][4] for k in hist]
            c0 = closes[0]
            if c0 <= 0:
                continue
            ret20 = (close / c0 - 1) * 100
            rets = [(closes[k] / closes[k - 1] - 1) for k in range(1, len(closes)) if closes[k - 1] > 0]
            if len(rets) < 15:
                continue
            mu = sum(rets) / len(rets)
            vol20 = math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) * 100
            amt20 = sum(amts[1:]) / (len(amts) - 1)
            if amt20 < 3e7:
                continue
            ed = 0
            for k in range(len(closes) - 10, len(closes)):
                if k == 0:
                    continue
                if closes[k] < closes[k - 1] and vv[k] > vv[k - 1]:
                    ed += 1
            recs.append({"code": c, "close": close, "ret20": ret20, "vol20": vol20,
                         "fmv": close * fshare, "amt20": amt20,
                         "expanddn10": ed / 10 * 100})
        snaps.append({"month": m, "date": d, "recs": recs})
    return snaps


def pick(sn, factor, n=20):
    key = {"smallcap": lambda r: r["fmv"],
           "reversal20": lambda r: r["ret20"],
           "lowvol20": lambda r: r["vol20"],
           "expanddn10": lambda r: -r.get("expanddn10", 0)}[factor]
    return sorted(sn["recs"], key=key)[:n]


def run(factor, n=20, env_gate=False, root=None):
    dates, px, fs, names = _load(root)
    snaps = build_snapshots(px, dates, fs)
    # 环境闸序列：等权全A收盘均值 vs MA20
    env = None
    if env_gate:
        eq = []
        for d in dates:
            cl = [v[0] for v in px_all_day(px, d)]
            eq.append(sum(cl) / len(cl) if cl else None)
        win = []
        ma20 = {}
        for i, v in enumerate(eq):
            if v is None:
                continue
            win.append(v)
            if len(win) > 20:
                win.pop(0)
            if len(win) == 20:
                ma20[dates[i]] = sum(win) / 20.0
        env = {d: (eq[i] is not None and ma20.get(d) is not None and eq[i] > ma20[d])
               for i, d in enumerate(dates)}
    trades, months = [], []
    held = []
    for k in range(len(snaps) - 1):
        sn, nx = snaps[k], snaps[k + 1]
        if env is not None and not env.get(sn["date"], False):
            months.append({"month": nx["month"], "port": 0.0, "bench": _bench(px, sn, nx),
                           "hold": 0, "note": "ENV_GATE_OFF"})
            held = []
            continue
        tgt = {r["code"]: r for r in pick(sn, factor, n)}
        # 组合月收益（close-to-close，换手计费）；基准=全过滤池等权
        port = 0.0
        w = 1.0 / max(1, len(tgt))
        cnt = 0
        for c, r in tgt.items():
            nx_row = px.get(c, {}).get(nx["date"])
            old_row = px.get(c, {}).get(sn["date"])
            if not nx_row or not old_row or old_row[0] <= 0:
                continue
            gross = nx_row[0] / old_row[0] - 1
            if gross < -0.33 or gross > 3.0:
                continue  # 不复权数据除权缺口/坏行，winsorize 剔除（报告已注）
            turnover = 0 if c in held else 1
            cost = (COST_BUY + COST_SELL) if turnover else 0.0
            port += w * ((1 + gross) * (1 - cost) - 1)
            cnt += 1
        bench = _bench(px, sn, nx)
        trades.append({"month": nx["month"], "date": nx["date"], "codes": list(tgt)[:n],
                       "port": round(port * 100, 3), "bench": round(bench, 3)})
        months.append({"month": nx["month"], "port": round(port * 100, 3),
                       "bench": round(bench, 3), "hold": cnt, "note": ""})
        held = list(tgt)
    return _stats(factor, months, n, env_gate), trades, months


def px_all_day(px, d):
    for c, m_ in px.items():
        v = m_.get(d)
        if v:
            yield v


def _bench(px, sn, nx):
    """基准：同过滤池等权月收益（close-to-close）。"""
    tot = cnt = 0
    for r in sn["recs"]:
        a = px.get(r["code"], {}).get(sn["date"])
        b = px.get(r["code"], {}).get(nx["date"])
        g = b[0] / a[0] - 1 if (a and b and a[0] > 0) else None
        if g is None or g < -0.33 or g > 3.0:
            continue  # 同上 winsorize
        tot += g
        cnt += 1
    return round(tot / cnt * 100, 3) if cnt else 0.0


def _stats(factor, months, n, env_gate):
    ports = [m["port"] for m in months]
    benchs = [m["bench"] for m in months]
    wins = sum(1 for p, b in zip(ports, benchs) if p > b)
    eq = 1.0
    peak, mdd = 1.0, 0.0
    for p in ports:
        eq *= (1 + p / 100)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    ym = max(1, len(ports) / 12)
    ann = (eq ** (1 / ym) - 1) * 100 if eq > 0 else -100.0
    beq = 1.0
    for b in benchs:
        beq *= (1 + b / 100)
    return {"factor": factor, "n_months": len(ports), "n_hold": n, "env_gate": env_gate,
            "win_vs_bench": round(wins / len(ports) * 100, 1) if ports else 0,
            "avg_month": round(sum(ports) / len(ports), 3) if ports else 0,
            "total": round((eq - 1) * 100, 1), "bench_total": round((beq - 1) * 100, 1),
            "ann": round(ann, 1), "mdd": round(mdd * 100, 1)}


def _selftest():
    assert month_ends(["2024-01-02", "2024-01-31", "2024-02-01", "2024-02-28"]) == \
        [("2024-01", "2024-01-31"), ("2024-02", "2024-02-28")]
    print("factor_monthly selftest OK")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--factor", choices=FACTORS)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--env-gate", dest="env_gate", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if not a.factor:
        ap.error("--factor 必填（或 --selftest）")
    global _PX
    st, trades, months = run(a.factor, n=a.n, env_gate=a.env_gate)
    print("因子=%s · 持仓%d · 月数%d · 环境闸=%s" % (a.factor, a.n, st["n_months"], a.env_gate))
    print("月均 %+.2f%% · 总收益 %+.1f%%（基准 %+.1f%%）· 年化 %+.1f%% · 月胜率(超基准) %.0f%% · MDD %.1f%%"
          % (st["avg_month"], st["total"], st["bench_total"], st["ann"], st["win_vs_bench"], st["mdd"]))
    for m in months:
        print("%s 组合 %+.2f%% 基准 %+.2f%% 持仓 %d %s" % (m["month"], m["port"], m["bench"], m["hold"], m["note"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
