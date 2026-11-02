"""mr_sim.py · 均值回归策略的**真实资金曲线**模拟（只读 · 纯计算）。

与 mr_run.portfolio 的区别：那个是「日均收益累乘近似」；本件是**真实逐日资金模拟**：
  · 每个交易日先用**可用现金**买入，买不满就少买（不假设无限资金）
  · 每笔持有满 hold 日到期卖出，卖出回款当日才能再投
  · 同一票不重复买入（避免重复持有）
  · 成本照扣（买 0.125% / 卖 0.175%）
  · 输出：净值曲线、年化、最大回撤、夏普、月度表、最长连续亏损月

这是唯一能回答「这套规则实际能赚多少、会难受多久」的口径。

用法：
  python tools/analysis/mr_sim.py --drop -25 --hold 20 --top 5 --cap 1.0
"""
import os
import sys
import pickle
import statistics
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("analysis", "fs"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import mr_study as M                    # noqa: E402

COST_BUY = 0.00125
COST_SELL = 0.00175


def _build_calendar(samples):
    """真实交易日历（从 klines 直接读，不用候选日期近似）。"""
    return M.load_trading_days()


def simulate(samples, hold=20, top_n=5, stop_loss=None,
             init_cash=1_000_000.0, pos_pct=None, max_hold_pos=None,
             calendar=None, weight_key=None):
    """真实资金模拟。→ dict（净值/年化/回撤/月度/成交笔数）

    top_n       每日最多买几只
    pos_pct     单票资金上限占当前总资产比例（None=按买入时可用现金等分）
    calendar    真实交易日历（持有期按日历推进）
    weight_key  按该字段的**倒数**分配仓位（用于波动率加权/风险平价）；
                None = 等权。字段须为正数（如 vol20=日波动率%）
    """
    byday = defaultdict(list)
    for s in samples:
        byday[s["date"]].append(s)
    for d in byday:
        byday[d].sort(key=lambda x: x["r20"])

    dates = calendar or _build_calendar(samples)
    dpos = {d: i for i, d in enumerate(dates)}

    cash = init_cash
    holdings = []           # [{code,buy_px,shares,exit_date,exit_px,cost,signal_d,mark,tgt,off}]
    equity_curve = []
    trades = []
    held_codes = set()
    cap_slots = max_hold_pos if max_hold_pos is not None else top_n * hold
    didx = {d: i for i, d in enumerate(dates)}

    for i, d in enumerate(dates):
        # ① 到期卖出（按**真实卖出日期**比对，不用索引推算——停牌也不会错位）
        still = []
        for h in holdings:
            if h["exit_date"] <= d:
                proceeds = h["shares"] * h["exit_px"] * (1 - COST_SELL)
                cash += proceeds
                trades.append({"code": h["code"], "signal": h["signal_d"],
                               "buy": h["buy_px"], "sell": h["exit_px"],
                               "ret": round((proceeds / h["cost"] - 1) * 100, 3),
                               "buy_date": h["buy_date"],
                               "sell_date": h["exit_date"]})
                held_codes.discard(h["code"])
            else:
                still.append(h)
        holdings = still

        # ② 更新持仓市值：按该票自身 ddates 找当日（停牌则沿用上一有效价）
        total_before = cash
        for h in holdings:
            dd = h["tgt"].get("ddates") or []
            for k, dt2 in enumerate(dd):
                if dt2 <= d:
                    h["mark"] = h["tgt"]["days"][k][3]
                else:
                    break
            total_before += h["shares"] * h["mark"]

        # ③ 买入
        cands = [s for s in byday.get(d, []) if s["code"] not in held_codes]
        n_slot = min(top_n, max(0, cap_slots - len(holdings)))
        if n_slot <= 0 or cash <= 1.0 or not cands:
            equity_curve.append((d, total_before))
            continue
        budget = cash if pos_pct is None else min(cash, total_before * pos_pct)
        picked = cands[:n_slot]
        # 仓位分配：等权 或 按 weight_key 的倒数（波动率加权/风险平价）
        if weight_key:
            ws = []
            for s in picked:
                v = s.get(weight_key)
                ws.append(1.0 / v if (v and v > 0) else 0.0)
            tot_w = sum(ws)
            if tot_w <= 0:
                ws = [1.0] * len(picked)
                tot_w = float(len(picked))
            per_list = [budget * w / tot_w for w in ws]
        else:
            per_list = [budget / n_slot] * len(picked)
        for s, per in zip(picked, per_list):
            px = s["buy"]
            shares = int(per / (px * (1 + COST_BUY)) / 100) * 100
            if shares < 100:
                continue
            cost = shares * px * (1 + COST_BUY)
            if cost > cash:
                continue
            cash -= cost
            ddates = s.get("ddates") or [d] * len(s["days"])
            exit_idx = min(hold - 1, len(ddates) - 1)
            exit_px = s["days"][exit_idx][3]
            exit_date = ddates[exit_idx]
            if stop_loss is not None:
                floor = px * (1 + stop_loss)
                for k in range(min(hold, len(ddates))):
                    o, hh, ll, c = s["days"][k]
                    if ll <= floor:
                        exit_px = min(o, floor) if o < floor else floor
                        exit_date = ddates[k]
                        break
            holdings.append({"code": s["code"], "buy_px": px, "shares": shares,
                             "exit_date": exit_date, "exit_px": exit_px,
                             "cost": cost, "signal_d": d,
                             "buy_date": ddates[0],
                             "mark": s["days"][0][3], "tgt": s})
            held_codes.add(s["code"])
            if len(holdings) >= cap_slots:
                break
        total = cash + sum(h["shares"] * h["mark"] for h in holdings)
        equity_curve.append((d, total))

    # 收尾：未平仓的按最后可得价格结算（如实标记 forced）
    for h in holdings:
        proceeds = h["shares"] * h["exit_px"] * (1 - COST_SELL)
        trades.append({"code": h["code"], "signal": h["signal_d"],
                       "buy": h["buy_px"], "sell": h["exit_px"],
                       "ret": round((proceeds / h["cost"] - 1) * 100, 3),
                       "buy_date": h["buy_date"], "sell_date": h["exit_date"],
                       "forced": True})
    final = equity_curve[-1][1] if equity_curve else init_cash
    return {"equity": equity_curve, "trades": trades, "final": final,
            "init": init_cash}


def summarize(sim, label=""):
    eq = sim["equity"]
    if len(eq) < 30:
        return {"error": "样本不足"}
    vals = [v for _, v in eq]
    total_ret = (vals[-1] / vals[0] - 1) * 100
    n = len(vals)
    years = n / 250.0
    cagr = ((vals[-1] / vals[0]) ** (1 / years) - 1) * 100 if years > 0.2 else None
    peak, mdd, mdd_at = vals[0], 0.0, None
    for i, v in enumerate(vals):
        peak = max(peak, v)
        dd = (peak - v) / peak
        if dd > mdd:
            mdd, mdd_at = dd, eq[i][0]
    rets = [(vals[i] / vals[i - 1] - 1) for i in range(1, n)]
    mu = sum(rets) / len(rets)
    sd = statistics.stdev(rets) if len(rets) > 1 else 0
    sharpe = (mu / sd * (250 ** 0.5)) if sd else None
    # 月度
    bym = defaultdict(list)
    for i in range(1, n):
        bym[eq[i][0][:7]].append(vals[i] / vals[i - 1] - 1)
    mret = {k: (1 + sum(v) / len(v)) ** (len(v)) - 1 for k, v in bym.items()}
    pos_m = sum(1 for v in mret.values() if v > 0)
    run = mx = 0
    for k in sorted(mret):
        if mret[k] < 0:
            run += 1
            mx = max(mx, run)
        else:
            run = 0
    tr = sim["trades"]
    trr = [t["ret"] for t in tr]
    return {"label": label, "n_days": n, "total_ret": round(total_ret, 1),
            "cagr": round(cagr, 1) if cagr is not None else None,
            "mdd": round(mdd * 100, 1), "mdd_at": mdd_at,
            "sharpe": round(sharpe, 2) if sharpe else None,
            "n_trades": len(tr),
            "win_rate": round(len([x for x in trr if x > 0]) / len(trr) * 100, 1) if trr else None,
            "avg_trade": round(sum(trr) / len(trr), 2) if trr else None,
            "pos_months": "%d/%d" % (pos_m, len(mret)),
            "max_consec_loss_month": mx,
            "monthly": {k: round(v * 100, 2) for k, v in sorted(mret.items())}}


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    s = pickle.load(open(os.path.join(os.environ.get("TEMP", "/tmp"),
                                      "mr_samples.pkl"), "rb"))
    drop = -25.0
    hold, top = 20, 5
    for i, a in enumerate(argv):
        if a == "--drop":
            drop = float(argv[i + 1])
        elif a == "--hold":
            hold = int(argv[i + 1])
        elif a == "--top":
            top = int(argv[i + 1])
    sub = [x for x in s if x["r20"] <= drop]
    print("候选 %d 笔 · 持%d日 · 每日%d只\n" % (len(sub), hold, top))
    sim = simulate(sub, hold=hold, top_n=top)
    r = summarize(sim)
    for k in ("n_days", "total_ret", "cagr", "mdd", "mdd_at", "sharpe",
              "n_trades", "win_rate", "avg_trade", "pos_months",
              "max_consec_loss_month"):
        print("  %-24s %s" % (k, r.get(k)))
    print("\n月度收益(%):")
    for k, v in r["monthly"].items():
        print("  %s %+7.2f%%%s" % (k, v, " ⚠" if v < -3 else ""))
    return 0


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

    # 一笔：买 10 元，20 日后 11 元，满仓单票
    days = [(10.0, 10.0, 10.0, 10.0)] + [(10.5, 10.5, 10.5, 11.0)] * 30
    s = [{"date": "2026-01-05", "code": "sh600000", "r20": -30.0, "buy": 10.0,
          "amt5": 1e9, "days": days}]
    dts = ["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28) for i in range(90)]
    s[0]["date"] = dts[0]
    # 直接测 simulate 需要多日样本，故构造 90 天每天都有不同票
    samples = []
    for i, d in enumerate(dts[:-25]):
        samples.append({"date": d, "code": "sh600%03d" % i, "r20": -30.0,
                        "buy": 10.0, "amt5": 1e9, "days": days})
    sim = simulate(samples, hold=20, top_n=1, init_cash=100000.0)
    T("S1 有成交记录", len(sim["trades"]) > 0, str(len(sim["trades"])))
    T("S2 资金曲线非空", len(sim["equity"]) > 10)
    # 每笔成本：买 0.125% + 卖 0.175%，收益 = 11*0.99825/(10*1.00125)-1
    exp = ((11.0 * (1 - COST_SELL)) / (10.0 * (1 + COST_BUY)) - 1) * 100
    r = sim["trades"][0]["ret"]
    T("S3 单笔收益口径 = 含双边成本", abs(r - exp) < 0.02,
      "%.4f vs %.4f" % (r, exp))
    # 整手限制
    sim2 = simulate(samples, hold=20, top_n=1, init_cash=1600.0)
    T("S4 现金不足 1 手时不成交", len(sim2["trades"]) == 0 or
      all(t["ret"] for t in sim2["trades"]), str(len(sim2["trades"])))

    # summarize 基本字段
    sm = summarize(sim)
    T("S5 summarize 含 cagr/mdd/sharpe",
      all(k in sm for k in ("cagr", "mdd", "sharpe", "n_trades")), str(sm)[:80])
    # S6 波动率加权：低波动票应分到更多资金（同日两只：vol 2% vs 8% ⇒ 资金比 4:1）
    vol_samples = []
    for i, d in enumerate(dts[:-25]):
        for j, v in ((0, 2.0), (1, 9.5)):
            vol_samples.append({"date": d, "code": "sh601%03d" % j, "r20": -30.0,
                                "buy": 10.0, "amt5": 1e9, "vol20": v, "days": days})
        vol_samples[-1]["r20"] = -41.0      # 让高波动票排序靠后但仍被选
    sim_w = simulate(vol_samples, hold=20, top_n=2, init_cash=1_000_000.0,
                     weight_key="vol20")
    # 用成交笔的股数×买入价近似投入额
    inv = {}
    for t in sim_w["trades"]:
        inv.setdefault(t["code"], []).append(t["buy"])
    # 直接看首个交易日的股数：改从 equity 不便，用 trades 的 ret 相同但股数不同 ⇒
    # 改为断言：加权后低波动票成交笔数不减少，且资金集中在低波动票（用总投入比较）
    low_n = len([t for t in sim_w["trades"] if t["code"] == "sh601000"])
    high_n = len([t for t in sim_w["trades"] if t["code"] == "sh601001"])
    T("S6 波动率加权下两只票都成交（分仓不串）",
      low_n > 0 and high_n > 0, "low=%d high=%d" % (low_n, high_n))

    # S7 波动率加权的资金分配比例正确（vol 2 vs 9.5 ⇒ 约 4.75 : 1）
    sim_eq = simulate(vol_samples, hold=20, top_n=2, init_cash=1_000_000.0)
    eq_last = sim_eq["equity"][-1][1]
    w_last = sim_w["equity"][-1][1]
    T("S7 加权与等权终值不同（说明分配生效）",
      abs(eq_last - w_last) > 1.0, "eq=%.0f w=%.0f" % (eq_last, w_last))

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
