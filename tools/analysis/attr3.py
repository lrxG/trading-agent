# -*- coding: utf-8 -*-
"""attr3.py —— 逐笔三段归因（可审计恒等式）+ 失败模式分类 + 动作映射

存在的理由：系统里没有「成交账本 → 归因 → 反写规则」这条链路，
所有归因都停在报告文字里，进不了下一轮决策。本模块把它接上。

归因口径（针对真实成交，买价可非开盘价）：
    pre     = buy_price / prev_close - 1      买入前的跳空（别人的利润，你付的溢价）
    intra   = close[buy_day] / buy_price - 1  买入当日剩余段
    drift   = sell_price / close[buy_day] - 1 持有期（跨日）
    total   = sell_price / prev_close - 1
    恒等式：total ≈ (1+pre)(1+intra)(1+drift) - 1

失败模式（决定动作，不是决定评价）：
    GAP   型：pre > +2%  且 total < 0         → 动作：禁止在跳空高位买入，等回补
    INTRA 型：|pre| <= 2% 且 intra < -1%      → 动作：买点下移到均价/VWAP 下方
    DRIFT 型：intra >= 0 但 drift < -2%       → 动作：加持有期上限 / 移动止损
    WIN          ：total > 0                  → 记录成功归因（哪一段贡献最大）
    NOISE        ：各段均小，|total| < 1%     → 成本问题，不是选股问题

用法：
    python tools/analysis/attr3.py --list
    python tools/analysis/attr3.py --attr            # 全部已平仓
    python tools/analysis/attr3.py --open            # 未平仓，按最新价浮动归因
    python tools/analysis/attr3.py --add --code sh600172 --price 11.00 --date 2026-11-06
"""
import argparse
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KLINES = os.path.join(ROOT, "data", "raw", "bus", "klines.csv")
TRADES = os.path.join(ROOT, "data", "state", "trades.jsonl")

TH = {"GAP": 0.02, "INTRA": -0.01, "DRIFT": -0.02, "NOISE": 0.01}
MAXEXPECT = "2026-11-09"   # 手动维护：期望的K线最新日；落后即提示补数据


# ── 数据 ────────────────────────────────────────────────────────────
def load_klines():
    """返回 {code: [(date, open, close, high, low), ...]} 升序"""
    out = {}
    if not os.path.exists(KLINES):
        return out
    with io.open(KLINES, encoding="utf-8") as f:
        hdr = f.readline().rstrip("\n").split(",")
        ix = {k: i for i, k in enumerate(hdr)}
        for ln in f:
            p = ln.rstrip("\n").split(",")
            if len(p) < len(hdr):
                continue
            try:
                o, c = float(p[ix["open"]]), float(p[ix["close"]])
            except (ValueError, IndexError):
                continue
            if o <= 0 or c <= 0:
                continue
            try:
                h, l = float(p[ix["high"]]), float(p[ix["low"]])
            except (ValueError, IndexError):
                h = l = c
            out.setdefault(p[ix["code"]], []).append((p[ix["date"]], o, c, h, l))
    for v in out.values():
        v.sort()
    return out


def load_trades():
    if not os.path.exists(TRADES):
        return []
    rows = []
    with io.open(TRADES, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    pass
    return rows


def save_trade(t):
    os.makedirs(os.path.dirname(TRADES), exist_ok=True)
    with io.open(TRADES, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(t, ensure_ascii=False) + "\n")


def next_tid(rows):
    n = sum(1 for r in rows if r.get("side") == "BUY") + 1
    return "T-%04d" % n


# ── 归因核心 ────────────────────────────────────────────────────────
def _bar(series, date):
    for i, b in enumerate(series):
        if b[0] == date:
            return i, b
    return None, None


def attribute_one(trade, sells, kl, today, overrides=None):
    """trade = BUY 记录；sells = 该 tid 的 SELL 记录列表（可空=持仓中）"""
    code = trade["code"]
    series = kl.get(code)
    if not series:
        return {"error": "无K线数据 code=%s" % code}

    i, bar = _bar(series, trade["date"])
    if bar is None:
        return {"error": "买入日 %s 不在K线中" % trade["date"]}
    if i == 0:
        return {"error": "买入日无前一日收盘，无法算 pre"}
    prev_close = series[i - 1][2]
    buy = float(trade["price"])
    close_buy_day = bar[2]

    sell = None
    sell_date = None
    if sells:
        s = sorted(sells, key=lambda x: x["date"])[0]
        sell_date = s["date"]
        sell = float(s["price"])
        closed = True
    elif overrides and code in overrides:
        sell = float(overrides[code])
        sell_date = "实时"
        closed = False
    else:
        sell_date = today
        # 用买入日之后最近一根K线收盘（today 可能不在K线里，说明还没收盘）
        j = None
        for k in range(len(series) - 1, i - 1, -1):
            if series[k][0] <= today:
                j = k
                break
        if j is None:
            return {"error": "买入日之后无K线"}
        sell = series[j][2]
        sell_date = series[j][0]
        closed = False

    pre = buy / prev_close - 1.0
    intra = close_buy_day / buy - 1.0
    drift = sell / close_buy_day - 1.0
    total = sell / prev_close - 1.0
    chk = abs((1 + pre) * (1 + intra) * (1 + drift) - 1 - total)

    # 实得 = 从买入价算起的真实盈亏（用户的钱）；total 是从前收盘算起的全段
    got = sell / buy - 1.0

    # 模式分类：先用「买入位置」分低吸/追高，再用「买入后实得」分对错
    if abs(got) < TH["NOISE"] and abs(pre) < TH["GAP"]:
        mode = "NOISE"
    elif pre <= -TH["GAP"] and got >= 0:
        mode = "SCOOP"      # 低吸成功：买在前收盘下方，且买后为正
    elif pre <= -TH["GAP"] and got < 0:
        mode = "STUCK"      # 低吸了还亏 → 选股/择时错，不是买点错
    elif pre >= TH["GAP"] and got < 0:
        mode = "CHASE"      # 追高失败 → 买点错
    elif pre >= TH["GAP"] and got >= 0:
        mode = "RIDE"       # 追高成功
    elif got >= 0 and drift < TH["DRIFT"]:
        mode = "DRIFT"      # 曾经赚过、拿着又还回去了
    elif got < 0 and intra < TH["INTRA"]:
        mode = "INTRA"      # 平开买入当天就跌
    else:
        mode = "WIN" if got > 0 else "STUCK"

    return {
        "tid": trade.get("tid"), "code": code, "name": trade.get("name", ""),
        "buy_date": trade["date"], "buy": buy, "sell_date": sell_date, "sell": sell,
        "closed": closed, "prev_close": prev_close,
        "pre": pre, "intra": intra, "drift": drift, "total": total, "got": got,
        "identity_err": chk, "mode": mode,
    }


ACTION = {
    "SCOOP": "买入位置对（低吸）。复制这套：不追高、挂在前收盘下方等回补",
    "STUCK": "买点没错、票错了 → 低吸了还亏，问题在选股/择时，不在成交价",
    "CHASE": "买入位置错 → 禁止跳空>2%追入，等回补到前收盘±1%",
    "RIDE": "追高但赚了 → 记样本，不可当规则；追高的统计期望仍为负",
    "DRIFT": "赚过又还回去 → 过夜前必须设移动止损（回落1.5%即走）",
    "INTRA": "平开买入当天即跌 → 买点后移到当日均价/VWAP 下方",
    "WIN": "记录贡献最大的一段，做成可复制模板",
    "NOISE": "各段皆小 → 亏在成本，减少频次，不是选股问题",
}


def pairing(rows):
    """BUY 与其 SELL（按 tid 或按 code 先进先出）配对"""
    buys, sells = [], {}
    for r in rows:
        if r.get("side", "BUY") == "SELL":
            sells.setdefault(r.get("tid") or r.get("code"), []).append(r)
        else:
            buys.append(r)
    out = []
    for b in buys:
        key = b.get("tid") or b.get("code")
        out.append((b, sells.get(key, [])))
    return out


def latest_date(kl):
    d = ""
    for v in kl.values():
        if v and v[-1][0] > d:
            d = v[-1][0]
    return d


# ── CLI ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--attr", action="store_true")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--add", action="store_true")
    ap.add_argument("--sell", action="store_true")
    ap.add_argument("--code")
    ap.add_argument("--name", default="")
    ap.add_argument("--price", type=float)
    ap.add_argument("--date")
    ap.add_argument("--shares", type=float, default=None)
    ap.add_argument("--tid")
    ap.add_argument("--note", default="")
    ap.add_argument("--last", action="append", default=[],
                    help="覆盖最新价，格式 code=price，可多次")
    args = ap.parse_args()

    rows = load_trades()

    if args.add or args.sell:
        if not args.code or args.price is None or not args.date:
            print("需要 --code --price --date")
            return 2
        tid = args.tid or next_tid(rows)
        t = {"tid": tid, "code": args.code, "name": args.name,
             "side": "SELL" if args.sell else "BUY",
             "date": args.date, "price": args.price,
             "shares": args.shares, "source": "MANUAL", "note": args.note}
        save_trade(t)
        print("已记录 %s %s %s @ %.2f" % (tid, t["side"], args.code, args.price))
        return 0

    if args.list or not (args.attr or args.open):
        if not rows:
            print("trades.jsonl 为空。用 --add 登记第一笔。")
            return 0
        print("%-10s %-4s %-10s %-11s %9s %8s %s" %
              ("tid", "side", "code", "date", "price", "shares", "note"))
        for r in rows:
            print("%-10s %-4s %-10s %-11s %9.3f %8s %s" %
                  (r.get("tid", ""), r.get("side", "BUY")[:4], r.get("code", ""),
                   r.get("date", ""), float(r.get("price") or 0),
                   r.get("shares") if r.get("shares") is not None else "-",
                   r.get("note", "")[:24]))
        return 0

    kl = load_klines()
    today = latest_date(kl)
    ov = {}
    for item in args.last:
        if "=" in item:
            k, v = item.split("=", 1)
            try:
                ov[k.strip()] = float(v)
            except ValueError:
                pass
    if ov:
        today = MAXEXPECT
    res = []
    for b, s in pairing(rows):
        r = attribute_one(b, s, kl, today, ov)
        if "error" in r:
            print("! %s %s" % (b.get("code"), r["error"]))
            continue
        if args.open and r["closed"]:
            continue
        if args.attr and not r["closed"]:
            continue
        res.append(r)
    if not res:
        print("没有符合条件的交易记录。")
        return 0

    print("K线数据截止 %s%s" % (today, "" if today >= MAXEXPECT else "  ⚠ 非最新交易日"))
    print("%-9s %-9s %-8s %8s %8s %8s %8s %8s %6s %s" %
          ("tid", "code", "name", "买入位置", "当日", "持有", "实得", "全段", "状态", "模式"))
    for r in res:
        print("%-9s %-9s %-8s %+7.2f%% %+7.2f%% %+7.2f%% %+7.2f%% %+7.2f%% %6s %s" %
              (r["tid"], r["code"], r["name"][:8], r["pre"] * 100, r["intra"] * 100,
               r["drift"] * 100, r["got"] * 100, r["total"] * 100,
               "已平" if r["closed"] else "持仓", r["mode"]))
    print("\n买入位置=买价/前收-1（负=低吸，正=追高）｜实得=卖出价/买价-1（你的钱）")

    bad = max(r["identity_err"] for r in res)
    print("\n恒等式校验最大偏差 = %.2e → %s" % (bad, "通过" if bad < 1e-9 else "不通过"))

    print("\n失败模式分布：")
    cnt = {}
    for r in res:
        cnt[r["mode"]] = cnt.get(r["mode"], 0) + 1
    for m, n in sorted(cnt.items(), key=lambda x: -x[1]):
        print("  %-6s %d 笔 → %s" % (m, n, ACTION.get(m, "")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
