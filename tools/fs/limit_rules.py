#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""limit_rules.py · A 股涨跌幅限制的**唯一真源**。

背景（2026-11-02 系统自查）：同一规则此前有 5 份实现、3 种语义 --
    settle._limit_ratio   bj30/创业科创20/ST5/主板10   ← 正确
    live_seed.limit_pct   ST5/创业科创20/主板10，**无 bj 分支 ⇒ 北证落 10%**  ← 错
    backtest.limit_ratio  ST5/**bj 并入 20%**/主板10                            ← 错
    timing.lim            **无 ST、无 bj** ⇒ 全按 20/10                           ← 错
    env_recalc            容差阈值（TH_ST/TH_20CM/TH_MAIN），语义不同但同源
口径（按交易所规则，判定顺序不可换）：
    1. 北交所 bj            → 30%
    2. 创业板 sz30* / 科创 sh68* → 20%（含这两板上的 ST 股，注册制下仍是 20%）
    3. 主板 ST/*ST          → 5%
    4. 其余主板             → 10%
新股上市首日等特殊情形不在本件范围（历史回测里已按日截面处理）。
"""
import os

BSE = 0.30
GROWTH = 0.20
ST = 0.05
MAIN = 0.10


def is_bse(code):
    return (code or "").lower().startswith("bj")


def is_growth(code):
    c = (code or "").lower()
    return c.startswith("sz30") or c.startswith("sh68")


def is_st(name):
    return "ST" in (name or "").upper()


def limit_pct(code, name=""):
    """→ 涨停幅度（小数）。顺序：北证 > 创业科创 > 主板 ST > 主板。"""
    if is_bse(code):
        return BSE
    if is_growth(code):
        return GROWTH
    if is_st(name):
        return ST
    return MAIN


def limit_price(prev_close, pct_or_code, name=""):
    """→ 涨停价（四舍五入到分）。pct_or_code 传幅度(小数)或代码皆可。"""
    if prev_close is None:
        return None
    from decimal import Decimal, ROUND_HALF_UP
    pct = pct_or_code if isinstance(pct_or_code, float) else limit_pct(pct_or_code, name)
    v = Decimal(str(prev_close)) * (Decimal(1) + Decimal(str(pct)))
    return float(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_limit_up(change_pct, code, name=""):
    """→ 当日涨幅是否达涨停（change_pct 为百分数，如 10.02）。留 0.3pp 容差。"""
    return change_pct is not None and change_pct >= limit_pct(code, name) * 100 - 0.3


def is_limit_down(change_pct, code, name=""):
    return change_pct is not None and change_pct <= -limit_pct(code, name) * 100 + 0.3


def _selftest():
    ok = fail = 0

    def T(cond, msg):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print("  [FAIL]", msg)

    T(limit_pct("bj920000") == 0.30, "A1 北证 30%")
    T(limit_pct("sz300750") == 0.20, "A2 创业板 20%")
    T(limit_pct("sh688388") == 0.20, "A3 科创 20%")
    T(limit_pct("sh600110") == 0.10, "A4 主板 10%")
    T(limit_pct("sh600110", "ST某某") == 0.05, "A5 主板 ST 5%")
    T(limit_pct("sz300001", "*ST创业") == 0.20, "A6 创业板 ST 仍 20%（顺序不可换）")
    T(limit_pct("bj920001", "ST北证") == 0.30, "A7 北证 ST 仍 30%")
    T(abs(limit_price(10.0, "sh600110") - 11.0) < 1e-9, "A8 主板涨停价 11.00")
    T(abs(limit_price(10.0, "sz300001") - 12.0) < 1e-9, "A9 创业板涨停价 12.00")
    T(abs(limit_price(9.87, "sh600110") - 10.86) < 1e-9, "A10 四舍五入到分（9.87→10.86）")
    T(is_limit_up(10.02, "sh600110"), "A11 主板 10.02% 判涨停")
    T(not is_limit_up(9.5, "sh600110"), "A12 主板 9.5% 非涨停")
    T(is_limit_down(-9.98, "sh600110"), "A13 主板 −9.98% 判跌停")
    T(is_limit_up(19.9, "bj920000") is False, "A14 北证 19.9% 非涨停（30% 档）")
    print("limit_rules --selftest: %d/%d 通过" % (ok, ok + fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(limit_pct.__doc__)
