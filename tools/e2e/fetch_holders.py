#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fetch_holders.py · 股东户数采集（2026-10-30 建 · 信息能力升级 P0 · task #281）

★ 运行环境（重要）：**必须用装了 akshare 的那个 Python 解释器**（本机装在系统 Python，非 WorkBuddy 内嵌解释器），
  证书链：脚本会自动用 `certifi.where()` 补 `REQUESTS_CA_BUNDLE`（无需手工写死路径）
  （本机双 Python + 缺 CA 证书的两个坑）。

数据源：AKShare `stock_zh_a_gdhs`（东方财富数据中心-股东户数，季度频，全市场）。
输出：data/raw/bus/holder_num_<报告期>.csv
字段：代码/名称/股东户数-本次/上次/增减/增减比例/户均持股市值/公告日期

用法：
  <装有 akshare 的 python> tools/e2e/fetch_holders.py --period 20260930
"""
from __future__ import annotations

import argparse
import io
import os
import ssl
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", required=True, help="报告期 YYYYMMDD（如 20260930）")
    a = ap.parse_args()

    # 公开数据只读接口：本机 CA 链不全，关闭校验（requests 层用 REQUESTS_CA_BUNDLE 或此处兜底）
    try:
        import certifi  # noqa: F401
        # ★ 2026-11-02：不再写死本机路径。优先 certifi 包自报的 CA 路径，
        #   其次沿用调用方已设的 REQUESTS_CA_BUNDLE；都取不到则跳过（让 requests 用默认链）。
        if not os.environ.get("REQUESTS_CA_BUNDLE"):
            try:
                import certifi
                os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
            except Exception:                                    # noqa: BLE001
                pass
        if not os.path.isfile(os.environ["REQUESTS_CA_BUNDLE"]):
            raise FileNotFoundError
    except Exception:
        ssl._create_default_https_context = ssl._create_unverified_context

    import akshare as ak
    df = ak.stock_zh_a_gdhs(symbol=a.period)
    out = os.path.join(_ROOT, "data", "raw", "bus", "holder_num_%s.csv" % a.period)
    df.to_csv(out, index=False, encoding="utf-8-sig")
    dec = df[df["股东户数-增减比例"] <= -5]
    inc = df[df["股东户数-增减比例"] >= 5]
    print("[SAVED]", os.path.relpath(out, _ROOT), "· 行数", len(df))
    print("[筹码集中] 户数降≥5%%: %d 只 · 户数增≥5%%: %d 只" % (len(dec), len(inc)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
