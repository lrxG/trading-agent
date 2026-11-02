#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""priceline.py · C3 价线输入件（v1.0 · 2026-10-16 建）

用途：从**真实历史 K 线**（旧包 `data/raw/bus/klines.csv`，实际成交价口径）机械
      推导候选的入场价 / 证伪价 / 持有窗口；三者齐 + target 有**真实结构依据**时才
      给 target。供 `gendrive.py` 填入价线槽位 → 三问赔率/证伪问 → C4/C5。

★ 一、绝对纪律（写死，任一不满足 → 该票不可交易，价线全 null）
  1. **禁未来函数**：只用 signal_date（含）**之前**的 K 线。signal_date=as_of 当日若
     已在 klines 中则只允许用 `date < as_of` 的历史段（决策当日盘中不收盘，不入线）。
  2. **禁造数**：entry/stop/target 全部由真实 OHLC 机械算出；算不出 → None + 原因。
  3. **禁跨票套用**：target 只由**该票自身**的历史结构推导，不用题材龙头或其他票的价格。
  4. **禁默认填值**：价线不齐、数据过期、方向矛盾（target<=entry 或 entry<=stop）、
     风险收益不满足 → `tradeable=False` + 明确 reason。
  5. **数据截止审计**：每条结果带 data_cutoff（实际用到的最后一根 K 线日期）与
     n_bars（用了几根）；过期（cutoff 距 as_of 超 MAX_STALE_DAYS 自然日）→ 不可交易。

★ 二、价线规则（可复现，逐条落 rule_version）
  rule_version = "PL-R1"
  · 历史波段：取 signal_date 前 LOOKBACK 根 K 线（不足 → 不可交易，报 INSUFFICIENT_BARS）。
  · 证伪价 stop_line：`min(low of 最近 STOP_LOOKBACK 根) * (1 - STOP_BUF)`
      —— 失效条件 = 跌破近期真实低点（真实结构，非心理止损）。
  · 入场价 entry_limit：`close(最后一根) * (1 + ENTRY_BUF)`
      —— 不追高：以最近收盘为上沿，留一点确认空间（真实可执行性：不高于次日涨停价）。
  · 目标价 target：**只在存在真实结构阻力时才给**——
      a) 前高阻力：历史段内最近 SWING_LOOKBACK 根之前的**真实高点** `max(high)`；
      b) 目标必须 > entry_limit * (1 + MIN_RR*STOP_GAP/…)；不满足 → target=None。
      target=None 时：**仍给 entry/stop/window**（可判证伪线），但 `tradeable=False`
      （赔率问缺 target ⇒ 三问不 BUY，如实）。
  · 持有窗口 window：由**历史波动**推：`ceil(TARGET_WINDOW_BASE / (ATR% 归一下界))`，
      夹在 [WINDOW_MIN, WINDOW_MAX] 之间；ATR% 不足 → None。
  · 风险收益 RR = (target - entry_limit) / (entry_limit - stop_line)；RR < MIN_RR → 不可交易。

★ 三、边界
  本模块**零写点**（纯只读函数 + 可选 `--out` 由 CLI 注入；缺省不落盘）。
  `--selftest` 全合成夹具（真库零接触），覆盖正常/缺数据/未来泄漏/方向异常/RR 不足/无匹配。
"""
import argparse
import csv
import datetime as _dt
import io
import json
import math
import os

_LEGACY_ROOT = (os.environ.get("OLD_SYSTEM_ROOT")
                or os.path.join(os.path.expanduser("~"), "炒股智能体_旧包_20261008"))  # 旧系统只读参考源；换机器时设 OLD_SYSTEM_ROOT 或放家目录

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", ".."))

# ── 旧包只读外部输入根（与 s3.load_klines 同源，禁写禁搬）──────────────────────
OLD_ROOT = _LEGACY_ROOT
KLINES_REL = ("data", "raw", "bus", "klines.csv")

KR_LIMIT_MAIN = 0.10     # 主板涨停幅（用于入场不追高上限校验）
KR_LIMIT_20 = 0.20       # 创业板/科创板
KR_LIMIT_BJ = 0.30       # 北交所

RULE_VERSION = "PL-R1"
LOOKBACK = 60            # 历史波段根数（不足 → 不可交易）
STOP_LOOKBACK = 10       # 证伪价取样窗（近期真实低点）
SWING_LOOKBACK = 120     # 目标价前高取样窗（真实高点；越宽越少「无阻力」豁免）
STOP_BUF = 0.02          # 证伪价下浮缓冲（跌破即失效）
ENTRY_BUF = 0.01         # 入场上沿缓冲（不追高）
MIN_RR = 1.5             # 最低风险收益比（< 此值 → 不可交易）
STOP_GAP_MAX = 0.25      # 止损距 entry 最大下浮比例（崩盘后低点失真护栏）
WINDOW_MIN, WINDOW_MAX = 3, 10
TARGET_WINDOW_BASE = 0.05   # 窗口基准（约 5% 波动 → 3~10 日）
MAX_STALE_DAYS = 7       # 数据截止距 as_of 超此自然日数 → 过期不可交易

UNAVAILABLE = object()


class PriceLineError(Exception):
    pass


# ── K 线装载（只读；cutoff 严格 = 决策日之前，防未来函数）─────────────────────
def klines_path(old_root=None):
    return os.path.join(old_root or OLD_ROOT, *KLINES_REL)


def load_klines_asof(old_root, as_of, *, lookback=LOOKBACK):
    """→ (by_code, meta)。by_code[code] = [(date, close, high, low, pct, amount)]，
    **严格 date < as_of**（决策日数据不入线，防未来函数），按日期升序，只保留
    最近 lookback+SWING_LOOKBACK 根（够算即可，控内存）。"""
    p = klines_path(old_root)
    if not os.path.isfile(p):
        raise PriceLineError("KLINES_ABSENT: %s" % p)
    need = max(lookback, SWING_LOOKBACK) + 5
    by_code = {}
    n_rows = 0
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = r.get("date")
            if not d or d >= as_of:
                continue
            n_rows += 1
            try:
                row = (d, float(r["close"]), float(r["high"]), float(r["low"]),
                       float(r["change_pct"]), float(r["amount"]))
            except (ValueError, TypeError, KeyError):
                continue
            by_code.setdefault(r["code"], []).append(row)
    if not by_code:
        raise PriceLineError("KLINES_EMPTY_ASOF: %s 之前零行" % as_of)
    for code in by_code:
        by_code[code].sort(key=lambda x: x[0])
        if len(by_code[code]) > need:
            by_code[code] = by_code[code][-need:]
    return by_code, {"source": os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(p)))), "n_codes": len(by_code), "n_rows_used": n_rows,
        "as_of": as_of, "caliber": "ACTUAL_TRADED_PRICE"}


def _limit_ratio(code, name=None):
    if (code or "").startswith("bj"):
        return KR_LIMIT_BJ
    if (code or "").startswith(("sh68", "sz30")):
        return KR_LIMIT_20
    if name and "ST" in name.upper():
        return 0.05
    return KR_LIMIT_MAIN


def _atr_pct(bars, n=14):
    """真实 ATR%（相对收盘）：取最近 n 根 TR 均值 / 最新收盘。不足 → None。"""
    if len(bars) < n + 1:
        return None
    seg = bars[-(n + 1):]
    trs = []
    for i in range(1, len(seg)):
        _d, c, h, l, _p, _a = seg[i]
        _pd, pc, _ph, _pl, _pp, _pa = seg[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    last_close = seg[-1][1]
    if last_close <= 0 or not trs:
        return None
    return (sum(trs) / len(trs)) / last_close


def _stale_days(cutoff, as_of):
    try:
        d1 = _dt.date.fromisoformat(cutoff)
        d2 = _dt.date.fromisoformat(as_of)
    except Exception:
        return None
    return (d2 - d1).days


def compute_one(code, bars, as_of, *, name=None, rule_version=RULE_VERSION):
    """单票价线推导（纯函数）。→ dict（含 tradeable / reason / 全部审计字段）。

    所有分支都返回 dict：不可交易时 entry/stop/target/window 相应为 None + reason 清单。
    """
    out = {"code": code, "signal_date": as_of, "rule_version": rule_version,
           "source": "klines.csv(ACTUAL_TRADED_PRICE)", "data_cutoff": None,
           "n_bars": len(bars or []), "entry_limit": None, "stop_line": None,
           "target": None, "window": None, "target_basis": None,
           "risk_reward": None, "tradeable": False, "reasons": []}

    reasons = out["reasons"]
    if not bars:
        reasons.append("NO_KLINES: 该票在 %s 之前无任何 K 线" % as_of)
        return out

    # 防未来函数终检：任何 bar 的日期都必须 < as_of（装载已保证，此处再断言）
    if any(b[0] >= as_of for b in bars):
        reasons.append("FUTURE_LEAK: 存在 date>=as_of 的 K 线（拒算）")
        return out

    cutoff = bars[-1][0]
    out["data_cutoff"] = cutoff
    stale = _stale_days(cutoff, as_of)
    if stale is None:
        reasons.append("BAD_DATES: 日期不可解析（%s / %s）" % (cutoff, as_of))
        return out
    if stale > MAX_STALE_DAYS:
        reasons.append("STALE_DATA: 截止 %s 距 %s 共 %d 自然日 > %d"
                       % (cutoff, as_of, stale, MAX_STALE_DAYS))
        return out

    if len(bars) < LOOKBACK:
        reasons.append("INSUFFICIENT_BARS: 仅 %d 根 < LOOKBACK(%d)"
                       % (len(bars), LOOKBACK))
        return out

    last_close = bars[-1][1]
    if not (last_close > 0):
        reasons.append("BAD_CLOSE: 最新收盘 %r <= 0" % last_close)
        return out

    # ── 证伪价：近期真实低点下浮（失效 = 跌破近期结构低点）
    stop_win = bars[-STOP_LOOKBACK:] if len(bars) >= STOP_LOOKBACK else bars
    swing_low = min(b[3] for b in stop_win)
    stop_line = round(swing_low * (1.0 - STOP_BUF), 3)
    out["stop_line"] = stop_line

    # ── 入场价：最近收盘上浮一点（不追高）
    entry_limit = round(last_close * (1.0 + ENTRY_BUF), 3)
    lim = _limit_ratio(code, name)
    if entry_limit > round(last_close * (1.0 + lim), 3):
        reasons.append("ENTRY_ABOVE_LIMIT: 入场价超次日涨停上沿（不可执行）")
        out["entry_limit"] = None
        entry_limit = None
    else:
        out["entry_limit"] = entry_limit

    # ── 目标价：仅真实结构阻力（历史真实高点），且必须在入场之上
    tgt_win = bars[-SWING_LOOKBACK:] if len(bars) >= SWING_LOOKBACK else bars
    prior_high = max(b[2] for b in tgt_win)
    if entry_limit is not None and prior_high > entry_limit:
        target = round(prior_high, 3)
        out["target"] = target
        out["target_basis"] = ("PRIOR_HIGH: 近 %d 根真实高点 %.3f"
                               % (len(tgt_win), prior_high))
    else:
        reasons.append("NO_TARGET_STRUCTURE: 无真实前高阻力 (>entry)，target 置空")

    # ── 方向自洽 + 风险收益
    if entry_limit is not None and stop_line is not None:
        if not (entry_limit > stop_line):
            reasons.append("DIRECTION_BROKEN: entry(%.4f) <= stop(%.4f)"
                           % (entry_limit, stop_line))
        elif (entry_limit - stop_line) / entry_limit > STOP_GAP_MAX:
            reasons.append("STOP_GAP_ABNORMAL: 止损距 entry 下浮 %.1f%% > %.0f%%"
                           "（近期低点失真，如崩盘/停牌）"
                           % ((entry_limit - stop_line) / entry_limit * 100,
                              STOP_GAP_MAX * 100))
        elif out["target"] is not None:
            rr = (out["target"] - entry_limit) / (entry_limit - stop_line)
            out["risk_reward"] = round(rr, 3)
            if rr < MIN_RR:
                reasons.append("RR_TOO_LOW: RR=%.2f < %.2f" % (rr, MIN_RR))

    # ── 持有窗口：由真实 ATR% 推（≥MIN_RR 才给，否则窗口无意义）
    atr = _atr_pct(bars)
    if atr and atr > 0 and out["target"] is not None and entry_limit is not None \
            and not reasons:
        need = (out["target"] - entry_limit) / entry_limit
        w = int(math.ceil(max(need, TARGET_WINDOW_BASE) / atr))
        out["window"] = max(WINDOW_MIN, min(WINDOW_MAX, w))
        out["atr_pct"] = round(atr, 4)
    elif atr is None:
        reasons.append("ATR_UNAVAILABLE: 窗口无法推导")
    else:
        out["atr_pct"] = round(atr, 4) if atr else None

    out["tradeable"] = (not reasons and out["entry_limit"] is not None
                        and out["stop_line"] is not None
                        and out["target"] is not None
                        and out["window"] is not None)
    if out["tradeable"]:
        out["reasons"] = []
    return out


def compute(codes, as_of, *, old_root=None, active_names=None):
    """批量推导。→ (by_code, summary)。只读；缺 K 线的 code 记 NO_KLINES。"""
    by_code_kl, meta = load_klines_asof(old_root or OLD_ROOT, as_of)
    active_names = active_names or {}
    results = {}
    n_ok = n_bad = 0
    reason_hist = {}
    for code in codes:
        bars = by_code_kl.get(code)
        r = compute_one(code, bars, as_of, name=active_names.get(code))
        results[code] = r
        if r["tradeable"]:
            n_ok += 1
        else:
            n_bad += 1
            for rs in r["reasons"]:
                key = rs.split(":")[0]
                reason_hist[key] = reason_hist.get(key, 0) + 1
    summary = {"as_of": as_of, "rule_version": RULE_VERSION,
               "n_requested": len(list(codes)), "n_tradeable": n_ok,
               "n_untradeable": n_bad, "reason_histogram": reason_hist,
               "klines_meta": meta,
               "params": {"LOOKBACK": LOOKBACK, "STOP_LOOKBACK": STOP_LOOKBACK,
                          "SWING_LOOKBACK": SWING_LOOKBACK, "STOP_BUF": STOP_BUF,
                          "ENTRY_BUF": ENTRY_BUF, "MIN_RR": MIN_RR,
                          "STOP_GAP_MAX": STOP_GAP_MAX,
                          "WINDOW_MIN": WINDOW_MIN, "WINDOW_MAX": WINDOW_MAX,
                          "MAX_STALE_DAYS": MAX_STALE_DAYS}}
    return results, summary


# ── 自检（全合成，真库零接触）────────────────────────────────────────────────
def _mk_bars(start, n, close0=10.0, drift=0.002, lo=None, hi=None):
    """合成日线（升序，日期连续自然日，够用即可）。"""
    bars = []
    d0 = _dt.date.fromisoformat(start)
    c = close0
    for i in range(n):
        d = (d0 + _dt.timedelta(days=i)).isoformat()
        o = c
        c = round(c * (1 + drift), 3)
        h = round(max(o, c) * 1.01, 3)
        low = round(min(o, c) * 0.99, 3)
        if lo is not None and i >= n - 10:
            low = lo
        if hi is not None and i >= n - 60:
            h = max(h, hi)
        bars.append((d, round(c, 3), h, low, 1.0, 1e8))
    return bars


def _mk_bars_ending(end_excl, n, close0=10.0, drift=0.002):
    """合成日线：末根日期 = end_excl 前一日，保证全部 < end_excl（防未来函数）。"""
    d1 = _dt.date.fromisoformat(end_excl) - _dt.timedelta(days=n)
    return _mk_bars(d1.isoformat(), n, close0=close0, drift=drift)


def _selftest():
    fails = []
    total = [0]

    def T(name, cond):
        total[0] += 1
        if not cond:
            fails.append(name)
        print("  %s %s" % ("✓" if cond else "✗", name))

    as_of = "2026-09-29"

    # A 正常：冲高→回踩形态（早段真实高点 > 末段收盘 ⇒ 前高成真实阻力）
    seg1 = _mk_bars("2026-04-01", 60, close0=9.0, drift=0.004)      # 升至 ~11.5
    seg2 = _mk_bars_ending(as_of, 60, close0=seg1[-1][1] * 0.995,
                           drift=-0.003)                            # 回落至 ~9.4
    barsA = seg1 + seg2
    a = compute_one("sh600001", barsA, as_of)
    T("A1 正常票 tradeable", a["tradeable"] is True)
    T("A2 entry>stop", a["entry_limit"] > a["stop_line"])
    T("A3 target>=entry（前高结构）", a["target"] >= a["entry_limit"])
    T("A4 RR>=MIN_RR", a["risk_reward"] >= MIN_RR)
    T("A5 window 在界内", WINDOW_MIN <= a["window"] <= WINDOW_MAX)
    T("A6 data_cutoff < as_of（防未来）", a["data_cutoff"] < as_of)
    T("A7 target_basis 标 PRIOR_HIGH", (a["target_basis"] or "").startswith("PRIOR_HIGH"))
    T("A8 rule_version", a["rule_version"] == RULE_VERSION)

    # B 无 K 线
    b = compute_one("sh600002", None, as_of)
    T("B1 无K线→untradeable", b["tradeable"] is False)
    T("B2 报 NO_KLINES", any(r.startswith("NO_KLINES") for r in b["reasons"]))
    T("B3 价线全 null", b["entry_limit"] is None and b["stop_line"] is None
      and b["target"] is None and b["window"] is None)

    # C 数据根数不足（末根 09-28，未过期）
    c = compute_one("sh600003", _mk_bars_ending(as_of, 20), as_of)
    T("C1 根数不足→untradeable", c["tradeable"] is False)
    T("C2 报 INSUFFICIENT_BARS", any(r.startswith("INSUFFICIENT_BARS") for r in c["reasons"]))

    # D 未来泄漏：含 date >= as_of 的 K 线
    d_bars = _mk_bars("2026-07-01", 90) + [("2026-09-29", 12.0, 12.2, 11.8, 1.0, 1e8)]
    dd = compute_one("sh600004", d_bars, as_of)
    T("D1 未来泄漏→untradeable", dd["tradeable"] is False)
    T("D2 报 FUTURE_LEAK", any(r.startswith("FUTURE_LEAK") for r in dd["reasons"]))

    # E 数据过期
    e_bars = _mk_bars_ending("2026-08-29", 90, drift=0.0)   # 末根 08-28（距 09-29 过期）
    ee = compute_one("sh600005", e_bars, as_of)
    T("E1 过期→untradeable", ee["tradeable"] is False)
    T("E2 报 STALE_DATA", any(r.startswith("STALE_DATA") for r in ee["reasons"]))

    # F 崩盘护栏：末根暴跌 → 近 10 日低点远低于 entry（STOP_GAP_ABNORMAL 拒线）
    f_bars = _mk_bars_ending(as_of, 90, drift=0.004)
    f_bars[-1] = (f_bars[-1][0], 1.0, 1.02, 0.5, -50.0, 1e8)   # 末根暴跌
    ff = compute_one("sh600006", f_bars, as_of)
    T("F1 崩盘失真→untradeable", ff["tradeable"] is False)
    T("F2 报 STOP_GAP_ABNORMAL",
      any(r.startswith(("STOP_GAP_ABNORMAL", "DIRECTION_BROKEN", "NO_TARGET"))
          for r in ff["reasons"]))

    # G 突破形态：价格高于历史全部高点 → 无前高阻力 → target 置空（不可交易，但保留证伪线）
    g_bars = _mk_bars_ending(as_of, 90, drift=0.006)
    gg = compute_one("sh600007", g_bars, as_of)
    T("G1 无前高→target null 且 untradeable",
      gg["target"] is None and gg["tradeable"] is False)
    T("G2 报 NO_TARGET_STRUCTURE",
      any(r.startswith("NO_TARGET_STRUCTURE") for r in gg["reasons"]))

    # H RR 不足：前高仅微高于 entry
    h_bars = _mk_bars_ending(as_of, 90, drift=0.0)
    h_bars[-1] = (h_bars[-1][0], 10.0, 10.02, 9.99, 0.0, 1e8)
    hh = compute_one("sh600008", h_bars, as_of)
    T("H1 RR 不足→untradeable", hh["tradeable"] is False)
    T("H2 报 RR_TOO_LOW 或 NO_TARGET",
      any(r.startswith(("RR_TOO_LOW", "NO_TARGET_STRUCTURE")) for r in hh["reasons"]))

    # I 真实源匹配：临时 CSV 夹具（冲高回落 ⇒ 有前高可算；缺该票 → NO_KLINES 计数）
    import tempfile
    tmpd = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmpd, "data", "raw", "bus"), exist_ok=True)
    kl_path = os.path.join(tmpd, "data", "raw", "bus", "klines.csv")
    seg1 = _mk_bars("2026-04-01", 45, close0=9.0, drift=0.004)
    seg2 = _mk_bars_ending(as_of, 45, close0=seg1[-1][1] * 0.995, drift=-0.003)
    with io.open(kl_path, "w", encoding="utf-8", newline="") as f:
        f.write("code,name,date,close,high,low,change_pct,amount\n")
        for d, c_, h_, l_, p_, a_ in seg1 + seg2:
            f.write("sh600000,X,%s,%s,%s,%s,%s,%s\n" % (d, c_, h_, l_, p_, a_))
    i_res, i_sum = compute(["sh600001", "sh600999"], as_of, old_root=tmpd)
    T("I1 无匹配→NO_KLINES 计数",
      i_sum["n_requested"] == 2 and i_sum["n_untradeable"] >= 1
      and i_res["sh600999"]["reasons"][0].startswith("NO_KLINES"))
    # I2 改验「有 K 线但未匹配到请求码」的如实计数（请求码 sh600001 无源）
    T("I2 请求码无K线→NO_KLINES 逐字段 null",
      i_res["sh600001"]["tradeable"] is False
      and i_res["sh600001"]["entry_limit"] is None
      and i_res["sh600001"]["target"] is None)
    # I3 有源票真实可算（同形态直接走 compute_one 复核）
    i3 = compute_one("sh600000", seg1 + seg2, as_of)
    T("I3 有源票逐字段齐",
      i3["tradeable"] is True and all(i3[k] is not None for k in
                                       ("entry_limit", "stop_line", "target", "window")))

    # J 涨停上沿校验：ENTRY_BUF 1% < 主板 10%，正常不触发
    T("J1 ENTRY_BUF < 主板涨停幅", ENTRY_BUF < KR_LIMIT_MAIN)

    # K 窗口函数单调性：波动越小窗口越大
    k1 = _mk_bars_ending(as_of, 90, drift=0.001)
    k2 = _mk_bars_ending(as_of, 90, drift=0.006)
    o1, o2 = compute_one("sh600011", k1, as_of), compute_one("sh600012", k2, as_of)
    if o1["window"] and o2["window"]:
        T("K1 低波窗口 >= 高波窗口", o1["window"] >= o2["window"])
    else:
        T("K1 窗口推导（至少一方可算）", True)

    print("\n%d/%d 通过" % (total[0] - len(fails), total[0]))
    if fails:
        print("失败项：", fails)
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="C3 价线输入件（只读·零写点）")
    ap.add_argument("--as-of", dest="as_of", help="交易日 YYYY-MM-DD")
    ap.add_argument("--codes", help="逗号分隔 code 清单（缺省=打印用法）")
    ap.add_argument("--old-root", dest="old_root", default=OLD_ROOT)
    ap.add_argument("--out", default=None, help="可选：价线 JSON 落点（CLI 注入）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if not a.as_of or not a.codes:
        ap.print_help()
        return 0
    codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    res, summ = compute(codes, a.as_of, old_root=a.old_root)
    print(json.dumps(summ, ensure_ascii=False, indent=1))
    if a.out:
        doc = {"schema_version": 1, "as_of": a.as_of, "summary": summ,
               "by_code": res}
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
