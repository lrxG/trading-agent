# -*- coding: utf-8 -*-
"""sector_scan.py · 板块启动扫描 + 主线内选股清单（盘后运行）

把 ml_01~ml_05 四层实证落成每日可跑的扫描器。**不含未来数据，只用当日收盘可见字段。**

挡位设计（每一条都对应实证，见 data/research/obs/ML_主线预判四层实证_20261106.md）：

  A. 位置闸：距 60 日高点 dd60 ≤ -20%
     实证：dd60<-30% 档 T+20 超额 +4.35%（主线率 43.5%，n=23）；-30~-20% 档 +2.43%（26.8%，n=153）
           全局启动事件基线 +1.09% / 主线率 ~20%
  B. 启动闸：当日超额 ≥2pp + 宽度 ≥0.65 + 绝对上涨
     实证：单看启动闸无 alpha（ml_02），但它是"试错的扳机"，不能不扣
  C. 选股闸：板块内取「前 20 日跌幅最大 20%」，**禁止买龙头**
     实证：L1低位 T+10 +1.00% / 中位 -0.43%；R1龙头 T+10 -0.57% / 中位 -2.05%（最差）
  D. 时点闸：**启动当日次日开盘即买，不等确认**
     实证：T+1 起算 +1.96pp，T+2 起算只剩 +0.28pp（等一根确认线，alpha 全没了）
  E. 风控：买错代价不对称 —— 低位组 T+1 走弱仅 -0.23%，龙头组走弱 -1.85%

用法：
  python tools/analysis/sector_scan.py                 # 扫最新一日
  python tools/analysis/sector_scan.py --date 2026-11-06
  python tools/analysis/sector_scan.py --refresh        # 强制重建面板（约 90s）
  python tools/analysis/sector_scan.py --top 3          # 每板块列 3 只低位票
"""
import argparse
import json
import os
import subprocess
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
OBS = os.path.join(_ROOT, "data", "research", "obs")
PANEL = os.path.join(OBS, "panel_sector.csv")
STOCK = os.path.join(OBS, "cache_stock.pkl")
NAMES = os.path.join(OBS, "sw_name.json")


def ensure_panel(refresh=False, want_date=None):
    need = True
    if not refresh and os.path.exists(PANEL) and os.path.exists(STOCK):
        try:
            last = pd.read_csv(PANEL, usecols=["date"])["date"].max()
            need = (want_date is not None and last < want_date)
            if not need:
                return last
        except Exception:
            need = True
    print("[面板] 重建中（约 90s）...", flush=True)
    rc = subprocess.call([sys.executable, os.path.join(OBS, "gen_sector_panel.py")], cwd=_ROOT)
    if rc != 0:
        raise SystemExit("面板重建失败，退出码 %d" % rc)
    return pd.read_csv(PANEL, usecols=["date"])["date"].max()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--top", type=int, default=3)
    a = ap.parse_args()

    last = ensure_panel(a.refresh, a.date)
    panel = pd.read_csv(PANEL)
    names = json.load(open(NAMES, encoding="utf-8"))
    panel["nm"] = panel["sw"].map(names).fillna(panel["sw"])
    panel = panel[panel["sw"] != "UNMAPPED"].copy()
    dates = sorted(panel["date"].unique())
    d = a.date or last
    if d not in dates:
        raise SystemExit("面板无此日期：%s（最新 %s）" % (d, last))

    cur = panel[panel["date"] == d].copy()
    print("=" * 104)
    print("板块启动扫描 · %s" % d)
    print("全市场中位涨幅 %+.2f%%（等权口径，klines 逐股 change_pct 汇总）"
          % cur["mkt_med"].iloc[0])
    print("=" * 104)

    # ---- 位置闸：超跌板块 ----
    dip = cur[cur["dd60"] <= -0.20].sort_values("dd60")
    print("\n【A】超跌板块（距 60 日高点 ≤ -20%%）：%d 个" % len(dip))
    if len(dip):
        print("%-10s %8s %8s %8s %8s %8s" % ("板块", "距高点", "当日超额", "宽度", "20日超额", "成分"))
        for _, r in dip.head(12).iterrows():
            print("%-10s %7.1f%% %+7.2f %7.1f%% %+7.1f %8d"
                  % (r["nm"], r["dd60"] * 100, r["excess"], r["breadth"] * 100,
                     r["past_ex20"], r["n"]))

    # ---- 启动闸 ----
    ign = cur[(cur["excess"] >= 2.0) & (cur["breadth"] >= 0.65) & (cur["med_pct"] > 0)].copy()
    ign = ign.sort_values("excess", ascending=False)
    print("\n【B】今日启动板块（超额≥2pp + 宽度≥65%% + 绝对上涨）：%d 个" % len(ign))

    if not len(ign):
        print("  无。→ 今天没有可动手的扳机，空仓等待不算错。")
        return

    st = pd.read_pickle(STOCK)
    st = st[~st["code"].str.startswith("bj")]
    sd = st[st["date"] == d]

    print("\n【C】启动板块明细 + 选股清单（板块内前 20 日跌幅最大者，禁买龙头）")
    for _, r in ign.iterrows():
        pos_txt = ("深度超跌（历史主线率 43.5%）" if r["dd60"] <= -0.30 else
                   "超跌（26.8%）" if r["dd60"] <= -0.20 else
                   "非低位（~19%，不建议新开仓）")
        print("\n  ── %s  超额 %+.2fpp  宽度 %.0f%%  距高点 %.1f%%  涨停 %d 只  [%s]"
              % (r["nm"], r["excess"], r["breadth"] * 100, r["dd60"] * 100, r["n_lim"], pos_txt))
        sub = sd[sd["sw"] == r["sw"]].copy()
        if len(sub) < 6:
            print("     成分不足，跳过")
            continue
        sub = sub.sort_values("past20")
        low = sub.head(a.top)
        lead = sub.sort_values("pct", ascending=False).head(2)
        for _, x in low.iterrows():
            print("     低位票  %-6s %-8s 收 %7.2f  当日 %+6.2f%%  20日 %+7.1f%%  额 %.1f亿"
                  % (x["code"], x["name"], x["close"], x["pct"], x["past20"],
                     x["amount"] / 1e8))
        print("     龙头(不推荐，历史 T+10 中位 -2.05%%)：%s"
              % " / ".join("%s(%+.1f%%)" % (x["name"], x["pct"]) for _, x in lead.iterrows()))

    # ---- 附：今日跌停/弱势提示 ----
    print("\n【E】执行提示（实证口径）")
    print("  · T+1 开盘买入，不等确认：T+1 起算 5 日 +1.96pp，T+2 起算只剩 +0.28pp")
    print("  · 买错代价：低位组 T+1 走弱 -0.23%，龙头组走弱 -1.85%（不对称 8 倍）")
    print("  · 一字板买不进的不追（回测已剔除，占 2.1%）")
    print("  · 本清单不含基本面/驱动源判定 —— 驱动闸仍须人工过，数据层无法替代")


if __name__ == "__main__":
    main()
