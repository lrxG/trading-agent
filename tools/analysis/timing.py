#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""timing.py · 市场择时闸（T 母题成果落地件 · 2026-10-28）

★ 成果来源（data/research/replay/PLAYBOOK.md · T 母题）：
  预设 6 规则三年分年验证，唯一三年全正且回撤砍半的是最笨的一条：
  **等权全A指数站上 MA20 → risk_on；否则 risk_off**。
  2024 +14.8%(回撤19% vs 基准30%) · 2025 +26.9%(9% vs 16%) · 2026 +2.0%(11% vs 21%)。
  加条件（涨停数/炸板率/量能）在 2024 更漂亮但 2026 更差 ⇒ 过拟合，不采用。

★ 定位：**市场级过滤器**，不是收益源。
  risk_off ⇒ 禁止新开仓（存量退出仍策略自主）；risk_on ⇒ 放行（不保证赚钱，只是不挡）。
  年化仅约 +2%（2026），覆盖不了个股摩擦成本 ⇒ 它的价值在于**避免负漂移期在场**。

★ 禁未来信息：信号只用 ≤D 收盘数据，作用于 D+1。

★ 前瞻验证（必做）：每次调用把 (date, risk_on, 偏离, 指数, MA20) 追加到
  data/state/timing_log.jsonl——3 个月后核对实盘记录与回测是否同向。
  回测只是必要条件，不是证据终点。

用法：
  python tools/analysis/timing.py --asof 2026-10-26      # 算指定日信号
  python tools/analysis/timing.py --latest --execute      # 最新日 + 落 state/log
  python tools/analysis/timing.py --selftest
"""
import csv
import io
import json
import os
import sys
from collections import defaultdict

_FS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
if _FS_DIR not in sys.path:
    sys.path.insert(0, _FS_DIR)
import limit_rules as LIMIT  # noqa: E402  ★ 涨跌幅单真源（2026-11-02）
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
KL = os.path.join(_ROOT, "data", "raw", "bus", "klines.csv")
STATE = os.path.join(_ROOT, "data", "state", "timing_state.json")
LOG = os.path.join(_ROOT, "data", "state", "timing_log.jsonl")
MA_N = 20


def _per_stock(path=KL):
    per = defaultdict(list)
    with open(path, encoding="utf-8", errors="ignore") as f:
        for r in csv.DictReader(f):
            c = r.get("code") or ""
            if not c or c.startswith(("sh000", "sz399", "bj")):
                continue
            try:
                cl, hi = float(r["close"]), float(r["high"])
            except (ValueError, TypeError):
                continue
            if cl <= 0:
                continue
            per[c].append([r["date"], cl, hi])
    for c in per:
        per[c].sort(key=lambda x: x[0])
    return per


def market_series(per):
    """→ (days, curve{date: idx}, ma{date: ma}, stat{date: {zt,bk,ret,n}})。"""
    agg = defaultdict(lambda: {"n": 0, "s": 0.0, "zt": 0, "bk": 0})

    def lim(c):
        return LIMIT.limit_pct(c)        # ★ 单真源；涨停判定不含 ST 名单信息，按板别取档

    for c, v in per.items():
        L = lim(c)
        for i in range(1, len(v)):
            d, cl, hi = v[i]
            pc = v[i - 1][1]
            if pc <= 0:
                continue
            a = agg[d]
            a["n"] += 1
            a["s"] += cl / pc - 1
            ceil_ = round(pc * (1 + L), 2)
            if cl >= ceil_ - 1e-9:
                a["zt"] += 1
            elif hi >= ceil_ - 1e-9:
                a["bk"] += 1
    days = sorted(agg)
    curve, eq = {}, 1.0
    for d in days:
        r = agg[d]["s"] / agg[d]["n"] if agg[d]["n"] else 0.0
        eq *= (1 + r)
        curve[d] = eq
    ma = {}
    for i, d in enumerate(days):
        if i >= MA_N - 1:
            ma[d] = sum(curve[x] for x in days[i - MA_N + 1:i + 1]) / MA_N
    stat = {d: {"zt": agg[d]["zt"], "bk": agg[d]["bk"],
                "ret": (agg[d]["s"] / agg[d]["n"] if agg[d]["n"] else 0.0),
                "n": agg[d]["n"]} for d in days}
    return days, curve, ma, stat


def signal_at(curve, ma, date):
    """→ dict{risk_on, index, ma20, dev_pct, reason}。MA 不足 → None（fail-closed）。"""
    if date not in curve or date not in ma:
        return None
    idx, m = curve[date], ma[date]
    on = idx > m
    return {"date": date, "risk_on": bool(on), "index": round(idx, 6),
            "ma20": round(m, 6), "dev_pct": round((idx / m - 1) * 100, 2),
            "rule": "eq_weight_allA > MA20 (V0 · PLAYBOOK T 母题)",
            "reason": ("等权全A 站上 MA20（偏离 %+.2f%%）允许开仓" % ((idx / m - 1) * 100)
                       if on else
                       "等权全A 低于 MA20（偏离 %+.2f%%）禁新开仓" % ((idx / m - 1) * 100))}


def signal(as_of, path=KL):
    """择时闸门面（★ 2026-11-02 收敛：此前 run_chain / premarket_card / intraday_scan
    各抄一份逐行相同的 fail-closed 封装，三份漂移风险）。

    返回 {risk_on, reason, date, ...}；任何异常 ⇒ fail-closed（risk_on=False），
    与"不可用即禁新开仓"的纪律一致，绝不因取不到数据而放行。
    """
    try:
        per = _per_stock(path)
        days, curve, ma, _st = market_series(per)
        if not days:
            return {"risk_on": False, "reason": "MA20 历史为空 ⇒ fail-closed", "date": None}
        use = as_of if as_of in curve else days[-1]
        r = signal_at(curve, ma, use)
        if not r:
            return {"risk_on": False, "reason": "MA20 数据不足 ⇒ fail-closed", "date": use}
        r = dict(r)
        r.setdefault("date", use)
        return r
    except Exception as e:                                       # noqa: BLE001
        return {"risk_on": False,
                "reason": "择时闸不可用（%s）⇒ fail-closed" % type(e).__name__,
                "date": None}


def write_state(doc):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def append_log(doc):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with io.open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False) + "\n")


def _selftest():
    fail = [0]

    def T(n, c):
        if c:
            print("  [✓] %s" % n)
        else:
            fail[0] += 1
            print("  [✗] %s" % n)

    # 手算夹具：3 只票 × 25 日，指数单调上行 → MA20 下方永真/上方永假可验证
    import tempfile
    sb = tempfile.mkdtemp(prefix="tmg_")
    p = os.path.join(sb, "kl.csv")
    rows = []
    days = ["2026-01-%02d" % d for d in range(1, 26)]
    price = {c: 10.0 for c in ("sh600001", "sh600002", "sz300001")}
    for i, d in enumerate(days):
        for c in price:
            price[c] *= 1.02 if i < 22 else 0.90     # 前 22 天涨、后 3 天深跌（跌穿 MA20）
            rows.append({"code": c, "date": d, "close": "%.4f" % price[c],
                         "high": "%.4f" % (price[c] * 1.01)})
    with io.open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["code", "date", "close", "high"])
        w.writeheader()
        w.writerows(rows)
    per = _per_stock(p)
    ds, curve, ma, stat = market_series(per)
    # 夹具 25 个日期，首日无涨跌幅不进序列 ⇒ 序列 24 日、MA20 可用 5 日
    T("T1 市场序列构建（24 日 · MA20 可用 5 日）",
      len(ds) == 24 and len(ma) == 5)
    T("T2 上行段 risk_on（末段连跌 3 日后转 risk_off）",
      signal_at(curve, ma, ds[20])["risk_on"] is True
      and signal_at(curve, ma, ds[-1])["risk_on"] is False)
    T("T3 数据不足 → None（fail-closed，不猜）", signal_at(curve, ma, "2026-01-01") is None)
    s = signal_at(curve, ma, ds[20])
    T("T4 信号含可审计字段（date/index/ma20/dev_pct/rule/reason）",
      all(k in s for k in ("date", "index", "ma20", "dev_pct", "rule", "reason")))
    T("T5 信号只依赖 ≤D 数据（MA20 用当日及之前 20 日）",
      abs(ma[ds[19]] - sum(curve[d] for d in ds[0:20]) / 20) < 1e-9)
    import shutil
    shutil.rmtree(sb, ignore_errors=True)
    print("结果：%d 失败" % fail[0])
    return fail[0] == 0


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    asof, latest, execute = None, False, False
    for i, a in enumerate(argv):
        if a == "--asof" and i + 1 < len(argv):
            asof = argv[i + 1]
        elif a == "--latest":
            latest = True
        elif a == "--execute":
            execute = True
    per = _per_stock()
    days, curve, ma, stat = market_series(per)
    if latest or not asof:
        asof = days[-1]
    sig = signal_at(curve, ma, asof)
    if sig is None:
        print("⛔ %s 无法判定（MA20 数据不足）⇒ fail-closed：禁新开仓" % asof)
        return 2
    sig["computed_at"] = __import__("time").strftime("%Y-%m-%dT%H:%M:%S")
    sig["market"] = stat.get(asof)
    print("[择时闸] %s → %s" % (sig["date"], "RISK_ON" if sig["risk_on"] else "RISK_OFF"))
    print("  等权指数 %.6f · MA20 %.6f · 偏离 %+.2f%%" % (sig["index"], sig["ma20"], sig["dev_pct"]))
    print("  当日：涨停 %d · 炸板 %d · 等权 %+.2f%%"
          % (sig["market"]["zt"], sig["market"]["bk"], sig["market"]["ret"] * 100))
    print("  规则：%s" % sig["rule"])
    if execute:
        write_state(sig)
        append_log(sig)
        print("  已落 state + 追加 timing_log.jsonl（前瞻验证用）")
    else:
        print("  （干跑：加 --execute 才落盘）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
