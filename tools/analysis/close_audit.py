#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""close_audit.py · 收盘体检 + 板块异动归因待办（2026-11-05）

★ 为什么有这个文件（两起实证失效，不是预防性设计）：

  失效一 · 板块异动失明（2026-11-03 光伏，晚了 2 个交易日）
    11-03 美国中期选举投票日，永臻股份当日涨停启动，11-03/04/05 三连板。
    系统在 11-05 用户直接追问"光伏为什么涨"时才去找原因。
    根因不是"预测不到"——中期选举是日程表上写了几年的确定事件；
    根因是**每天收盘后没有一个固定动作去看"今天哪些板块动了"**。
    → 本文件 A 段：涨幅榜 + 涨停密度榜 + 新晋榜，三个口径取并集，
      每条留 "原因=?" 空位。填不上就是功课没做完，不许进第二天的预案。

  失效二 · 不复权价污染跌幅（2026-11-05 瑞达期货）
    本地 klines.close 是**不复权价**，但 change_pct 是**真实涨跌幅**。
    实测：sz002961 瑞达期货 60 日跌 -41.74%（不复权）/ -9.94%（复权），差 31.8 个点。
    sz001309 德明利 -45.06% / -28.02%，差 17 个点。
    ⇒ 按"60 日跌幅榜"选票时，除权票会虚高排到前面。
    → 本文件 B 段：用 change_pct 累乘重建复权序列，标出所有失真 >5 点的票。
      **任何用 close 直接相除算涨跌幅的地方都应改走 --adj。**

  失效三 · 择时闸阈值口径漂移（2026-11-05）
    盘前预案写 "MA20≈4136"，那是**截至昨日**的 MA20；当日含今日的 MA20 是 4125.26。
    差 10.75 点，足以改变"站上/站不上"的结论。
    → 本文件 C 段：MA20 一律双值并列，禁单报。

★ 定位：**只读诊断件**，不写任何生产数据，不改状态。可每天收盘后裸跑，也可接日链。
  stdout 是给人看的三段体检；--json 落 data/generated/close_audit_<D>.json 供编排消费。

用法：
  python tools/analysis/close_audit.py --date 2026-11-04
  python tools/analysis/close_audit.py --date 2026-11-04 --json
  python tools/analysis/close_audit.py --adj --limit 30      # 复权修正后的跌幅榜
  python tools/analysis/close_audit.py --selftest
"""
import argparse
import csv
import io
import json
import os
import sys
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
BUS = os.path.join(_ROOT, "data", "raw", "bus")
RAW = os.path.join(_ROOT, "data", "raw")
GEN = os.path.join(_ROOT, "data", "generated")
KL = os.path.join(BUS, "klines.csv")

# 失真阈值：不复权跌幅 vs 复权跌幅，超过这个点数就认为该票在窗口内除过权
DISTORT_PCT = 5.0
# 板块"动"的门槛：涨幅 >= 该值才算当日异动
MOVE_PCT = 3.0
# 涨停密度榜取前 N
TOP_N = 8


# ── 行情读取 ────────────────────────────────────────────────────────────────
def load_klines(path=KL, min_days=62):
    """→ {code: [(date, open, close, high, low, vol, amount, change_pct), ...]} 按日升序

    change_pct 与 close 的关系是**本文件的核心资产**：
      close 不复权（会因除权跳空），change_pct 是真涨跌幅（已复权）。
      两者背离的点 = 除权日。
    """
    per = defaultdict(list)
    with io.open(path, encoding="utf-8", errors="ignore") as f:
        for r in csv.DictReader(f):
            c = (r.get("code") or "").strip()
            if not c or c.startswith(("bj", "sh000", "sz399")):
                continue
            try:
                cl = float(r["close"])
                op = float(r["open"])
                hi = float(r["high"])
                lo = float(r["low"])
                vol = float(r["volume"] or 0)
                amt = float(r["amount"] or 0)
            except (ValueError, TypeError, KeyError):
                continue
            if cl <= 0:
                continue
            try:
                chg = float(r["change_pct"])
            except (ValueError, TypeError, KeyError):
                chg = float("nan")
            per[c].append((r["date"], op, cl, hi, lo, vol, amt, chg, r.get("name") or ""))
    out = {}
    for c, v in per.items():
        if len(v) >= min_days:
            v.sort(key=lambda x: x[0])
            out[c] = v
    return out


def adj_returns(seq, n):
    """用 change_pct 累乘得 n 日**复权**收益率（%）。NaN 跳过（缺失日不算 0）。"""
    f = 1.0
    used = 0
    for rec in seq[-n:]:
        chg = rec[7]
        if chg != chg:  # NaN
            continue
        f *= (1.0 + chg / 100.0)
        used += 1
    if used < n * 0.8:  # 缺失超两成则不做结论
        return None
    return (f - 1.0) * 100.0


def raw_returns(seq, n):
    """用不复权 close 直接相除得的 n 日收益率（%）。这里故意保留，用于对比失真。"""
    if len(seq) < n + 1:
        return None
    return (seq[-1][2] / seq[-1 - n][2] - 1.0) * 100.0


# ── A 段：板块异动归因待办 ──────────────────────────────────────────────────
def _find_width(row):
    """在整行里自适应定位"板块宽度"字段（形如 "181/183"）。

    ★ 为什么不能按列名取（2026-11-05 实证）：
      sector_ranking.csv 的**表头与数据错位一格**。实测 11-04 第一行：
        header: ...,turnover,turnover_rate,up_count,leader
        data:   ...,-4624146.65,26719824.0,4.73,181/183
      ⇒ 按名取到的 up_count 是 4.73（那是换手率），leader 是 181/183（那才是宽度），
        真正的领涨股字段上游根本没返回。
      任何"按名字取"的写法都会静默拿到换手率当宽度 —— 不会报错，只会结论错。
      ⇒ 改为扫行找唯一形态特征：含 "/" 且两侧都是数字。
    """
    for k in range(len(row) - 1, 1, -1):
        s = str(row[k] or "").strip()
        if "/" not in s:
            continue
        a, _, b = s.partition("/")
        try:
            a, b = float(a), float(b)
        except ValueError:
            continue
        if b > 0:
            return a / b
    return None


def _read_sector_ranking(date):
    """→ [(code, name, chg, main1, main5, up_ratio, leader), ...] 按涨幅降序

    字段口径（经 11-04 数据实证）：change_pct/主力两列位置正确；
    turnover 起的四列整体左移一格 ⇒ 宽度必须走 _find_width 而非按名取。
    """
    p = os.path.join(RAW, date, "sector_ranking.csv")
    if not os.path.exists(p):
        return None
    rows = []
    with io.open(p, encoding="utf-8", errors="ignore") as f:
        rd = csv.reader(f)
        header = next(rd, None) or []
        for row in rd:
            if len(row) < 3:
                continue
            try:
                chg = float(row[2] or 0)
            except ValueError:
                continue
            # 主力净流入两列（c[3]/c[4]）在错位前，可直接用；单位万元→亿元
            def _f(i):
                try:
                    return float(row[i] or 0) / 1e4
                except (ValueError, IndexError):
                    return 0.0
            m1, m5 = _f(3), _f(4)
            ratio = _find_width(row)
            rows.append((row[0], row[1], chg, m1, m5, ratio, ""))
    rows.sort(key=lambda x: -x[2])
    return rows


def _read_limit_up(date):
    """→ [(code, name, boards, reason), ...]"""
    p = os.path.join(RAW, date, "limit_up.csv")
    if not os.path.exists(p):
        return None
    rows = []
    with io.open(p, encoding="utf-8-sig", errors="ignore") as f:
        for r in csv.DictReader(f):
            code = (r.get("code") or "").strip()
            if not code:
                continue
            try:
                boards = int(float(r.get("boards") or 1))
            except ValueError:
                boards = 1
            rows.append((code, r.get("name") or "", boards, (r.get("reason") or "").strip()))
    return rows


def _classify(chg, m1, m5, ratio):
    """板块异动性质判定 —— 这是给人省时间的关键：先定性，再让人只去核那一类。"""
    if chg >= MOVE_PCT and m1 > 0 and (ratio is None or ratio >= 0.8):
        return "真实启动", "涨+钱进+普涨"
    if chg >= MOVE_PCT and m1 < 0:
        return "拉高派发", "涨但主力净流出 ⚠"
    if chg >= MOVE_PCT and m5 < 0 and m1 > 0:
        return "首日回流", "5日在流出，今天刚回"
    if chg >= MOVE_PCT:
        return "分歧上涨", "涨但宽度或资金不确认"
    return "—", ""


def sector_audit(date, top_n=TOP_N, prev_date=None):
    """A 段主函数。→ dict"""
    cur = _read_sector_ranking(date)
    zt = _read_limit_up(date)
    res = {"date": date, "sector_ok": cur is not None, "limit_up_ok": zt is not None,
           "top_gain": [], "top_zt": [], "reversals": [], "rank_jump": [],
           "alerts": [], "must_explain": []}

    if cur:
        for row in cur[:top_n]:
            code, name, chg, m1, m5, ratio, leader = row
            tag, why = _classify(chg, m1, m5, ratio)
            res["top_gain"].append({
                "code": code, "name": name, "chg": round(chg, 2),
                "main1": round(m1, 2), "main5": round(m5, 2),
                "width": round(ratio * 100, 1) if ratio is not None else None,
                "leader": leader, "tag": tag, "why": why, "reason": "?"})
        # 告警：涨但钱走 —— 这类最容易让人误以为板块启动
        for r in res["top_gain"]:
            if r["tag"] == "拉高派发":
                res["alerts"].append("%s 涨%.1f%% 但主力净流出 %.1f亿" %
                                     (r["name"], r["chg"], -r["main1"]))

    if zt:
        cnt = Counter(r[3] or "未标原因" for r in zt)
        high = Counter()
        for code, name, boards, reason in zt:
            if boards >= 2:
                high[reason or "未标原因"] += 1
        for reason, n in cnt.most_common(top_n):
            res["top_zt"].append({"reason": reason, "n": n, "n_2board_plus": high.get(reason, 0),
                                  "explain": "?"})
        # 涨停原因缺失率本身是个数据质量问题
        miss = sum(1 for r in zt if not r[3])
        if miss:
            res["alerts"].append("涨停池 %d/%d 条无 reason 字段（板块归因精度受损）" % (miss, len(zt)))

    # 排名跃升 / 深V反转：这才是风格切换的发生地
    #   实证（2026-11-03 → 11-04）：半导体 -5.42%（倒数第2）→ +11.41%（第1），
    #   元件 -7.66% → +9.14%，通信设备 -8.37% → +8.91%。
    #   一天之内完成风格切换；次日收盘才看涨幅榜就晚了，必须看"谁从坑里翻上来"。
    if prev_date:
        prev = _read_sector_ranking(prev_date)
        if prev and cur:
            prank = {r[0]: i for i, r in enumerate(prev)}
            n_all = len(prev)
            for i, row in enumerate(cur):
                code = row[0]
                if code not in prank:
                    continue
                j = prank[code]
                jump = j - i
                was_bottom = j >= n_all * 0.8      # 昨日在跌幅榜尾部 20%
                is_top = i < 10                     # 今日进涨幅榜前十
                if was_bottom and is_top:
                    res["reversals"].append({
                        "name": row[1], "chg": round(row[2], 2),
                        "prev_chg": round(prev[j][2], 2), "prev_rank": j + 1,
                        "rank": i + 1, "main1": round(row[3], 2), "reason": "?"})
                elif jump >= 30:
                    res["rank_jump"].append({
                        "name": row[1], "chg": round(row[2], 2),
                        "prev_chg": round(prev[j][2], 2),
                        "prev_rank": j + 1, "rank": i + 1,
                        "jump": jump, "main1": round(row[3], 2), "reason": "?"})
            res["rank_jump"].sort(key=lambda x: -x["jump"])
            res["rank_jump"] = res["rank_jump"][:8]
            # 反转同样要收敛：普涨日会有十几个板块同时从坑里翻上来，
            # 全列等于没列 ⇒ 只留涨幅最高的 8 个
            res["reversals"].sort(key=lambda x: -x["chg"])
            res["reversals"] = res["reversals"][:8]

    # 必须归因清单（并集，去重）：涨幅前 N 的板块名 + 涨停密度前 N 的原因
    seen = []
    for r in res["top_gain"]:
        if r["name"] not in seen:
            seen.append(r["name"])
    res["must_explain"] = seen
    return res


# ── B 段：复权失真体检 ──────────────────────────────────────────────────────
def adj_distortion(per, n=60, thresh=DISTORT_PCT, limit=None):
    """→ [(code, name, raw_pct, adj_pct, diff, last_date), ...] 按 |diff| 降序

    只在**窗口内真的除过权**时 raw 与 adj 才会背离，所以这张表等价于"除权污染清单"。
    """
    out = []
    for c, seq in per.items():
        if len(seq) < n + 1:
            continue
        raw = raw_returns(seq, n)
        adj = adj_returns(seq, n)
        if raw is None or adj is None:
            continue
        diff = raw - adj
        if abs(diff) >= thresh:
            out.append((c, seq[-1][8], raw, adj, diff, seq[-1][0]))
    out.sort(key=lambda x: -abs(x[4]))
    return out[:limit] if limit else out


# ── C 段：MA20 双值（禁单报）───────────────────────────────────────────────
_IDX_CACHE = os.path.join(_ROOT, "data", "research", "obs", "idx_hs300.csv")


def idx_from_cache(code="sh000300", asof=None):
    """从 data/research/obs/idx_hs300.csv 读指数并算 MA20 双值。

    该文件格式：date,open,close,high,low（与 klines 不同，故单独读）。
    ★ 陷阱一：缓存可能领先于体检日（体检 11-04 时缓存已含 11-05）
      ⇒ 必须按 asof 截断，否则会把 11-05 的 MA20 当成 11-04 的报出去——
        这正是 11-05 当天犯过的错（拿昨日值当今日值），方向反过来一样是错。
    ★ 陷阱二：该缓存可能由**盘中**生成（11-05 14:04 那份末尾 4089.81，
      当日收盘实为 4113.41）⇒ 返回值带上 last_date，收盘口径须与实时接口核对。
    """
    if not os.path.exists(_IDX_CACHE):
        return None
    rows = []
    with io.open(_IDX_CACHE, encoding="utf-8", errors="ignore") as f:
        for r in csv.DictReader(f):
            d = r.get("date") or ""
            if asof and d > asof:
                continue
            try:
                rows.append((d, float(r["close"])))
            except (ValueError, KeyError):
                continue
    if len(rows) < 21:
        return None
    rows.sort(key=lambda x: x[0])
    cl = [x[1] for x in rows]
    return {"ma20_today": round(sum(cl[-20:]) / 20.0, 2),
            "ma20_prev": round(sum(cl[-21:-1]) / 20.0, 2),
            "close": cl[-1], "last_date": rows[-1][0],
            "stale": bool(asof and rows[-1][0] != asof),
            "source": os.path.relpath(_IDX_CACHE, _ROOT)}


def idx_ma20_both(code, per):
    """返回 (含今日MA20, 截至昨日MA20, 最新收盘, 日期)。

    2026-11-05 事故：预案报 4136（=截至昨日），当日实为 4125.26，差 10.75 点。
    ⇒ 任何对外报 MA20 的地方都必须同时给出两个值。
    """
    seq = per.get(code)
    if not seq or len(seq) < 21:
        return None
    cl = [s[2] for s in seq]
    today = sum(cl[-20:]) / 20.0
    prev = sum(cl[-21:-1]) / 20.0
    return (today, prev, cl[-1], seq[-1][0])


# ── E 段：确定性日程（制度性日期，不依赖联网）────────────────────────────
def rule_events(date, horizon=7):
    """未来 horizon 个自然日内的**制度性**事件。→ [(日期, 事件, 影响)]

    ★ 为什么要有这一段（2026-11-03 光伏失明的直接教训）：
      美国中期选举是提前几年就写在日程上的确定事件，11-03 投票日当天
      永臻股份涨停启动，系统完全没反应。根因不是"预测不到"，
      是**收盘后没有任何一个动作会去问"接下来几天有什么确定会发生的事"**。
      外部事件（选举/战争/议息）无法在本机固化，但**制度性日期可以**：
      LPR 报价、期货期权交割、财报披露截止，年月日都是死的，算得出来。
      外部事件由 A 段的"必须归因清单"兜住；制度性日期由本段兜住。
    """
    import datetime as _dt
    try:
        d0 = _dt.date(*[int(x) for x in date.split("-")])
    except (ValueError, TypeError):
        return []
    out = []
    for k in range(0, horizon + 1):
        d = d0 + _dt.timedelta(days=k)
        ds = d.strftime("%Y-%m-%d")
        # LPR 报价：每月 20 日
        if d.day == 20:
            out.append((ds, "LPR 报价日", "利率敏感（银行/地产/高股息）"))
        # 股指期货/期权交割：每月第三个周五
        if d.weekday() == 4 and 15 <= d.day <= 21:
            out.append((ds, "股指期货/期权交割日", "尾盘异动、贴水收敛"))
        # ETF 期权交割：每月第四个周三
        if d.weekday() == 2 and 22 <= d.day <= 28:
            out.append((ds, "ETF 期权交割日", "权重股尾盘扰动"))
        # 定期报告披露截止
        if (d.month, d.day) in ((4, 30), (8, 31), (10, 31)):
            out.append((ds, "定期报告披露截止", "业绩暴雷/预增集中释放"))
        # 月初宏观数据窗（CPI/PPI 约 9-10 日，金融数据 10-15 日）
        if d.day == 9:
            out.append((ds, "CPI/PPI 发布窗开启", "通胀链、消费/资源"))
        if d.day == 10:
            out.append((ds, "金融数据发布窗开启", "社融/M2，银行地产"))
        # 季末资金面
        if (d.month, d.day) in ((3, 31), (6, 30), (9, 30), (12, 31)):
            out.append((ds, "季末最后一个交易日", "资金面紧张、机构调仓"))
    return out


# ── D 段：数据齐备度 ────────────────────────────────────────────────────────
_DATA_FILES = [
    ("daily_quote.csv", "当日行情", True),
    ("news.jsonl", "新闻", True),
    ("limit_up.csv", "涨停池", True),
    ("sector_ranking.csv", "板块排行", True),
    ("sector_valuation.csv", "板块估值", False),
    ("regulatory.jsonl", "监管/股东", False),
    ("env_metrics.json", "环境指标", True),
    ("dragon_tiger.csv", "龙虎榜", False),
]


def data_gaps(date):
    d = os.path.join(RAW, date)
    if not os.path.isdir(d):
        return [{"file": "-", "what": "整日目录不存在", "critical": True, "size": 0}]
    gaps = []
    for fn, what, crit in _DATA_FILES:
        p = os.path.join(d, fn)
        size = os.path.getsize(p) if os.path.exists(p) else 0
        if size == 0:
            gaps.append({"file": fn, "what": what, "critical": crit, "size": 0})
    return gaps


# ── 报告 ────────────────────────────────────────────────────────────────────
def _auto_prev_date(date):
    """自动取上一交易日：data/raw 下 < date 且含 sector_ranking.csv 的最大日期目录。

    日链调用时不该要求传第二个参数——多一个必填参数就多一处会漏的地方。
    """
    try:
        cands = []
        for fn in os.listdir(RAW):
            if not (len(fn) == 10 and fn[4] == "-" and fn < date):
                continue
            if os.path.isfile(os.path.join(RAW, fn, "sector_ranking.csv")):
                cands.append(fn)
        return max(cands) if cands else None
    except OSError:
        return None


def build(date, prev_date=None, limit=30):
    per = load_klines()
    if not prev_date:
        prev_date = _auto_prev_date(date)
    sect = sector_audit(date, prev_date=prev_date)
    dist = adj_distortion(per, limit=limit)
    gaps = data_gaps(date)
    # 指数不在 klines 里（klines 只有个股），单独走 obs 缓存；按体检日截断
    idx = {}
    ic = idx_from_cache(asof=date)
    if ic:
        idx["sh000300"] = ic
    return {"date": date, "sector": sect, "distortion": dist, "gaps": gaps,
            "index": idx, "events": rule_events(date)}


def render(rep):
    L = []
    A = L.append
    A("=" * 78)
    A("收盘体检  %s" % rep["date"])
    A("=" * 78)

    # ---- A 板块异动 ----
    s = rep["sector"]
    A("")
    A("【A · 板块异动待归因】涨幅前 %d（★ 每条必须能在明天预案里填上原因）" % TOP_N)
    if not s["sector_ok"]:
        A("  ✗ sector_ranking.csv 缺失，本段跳过")
    else:
        A("  %-14s %7s %9s %9s %7s  %-10s %s" %
          ("板块", "涨幅", "主力1日", "主力5日", "宽度", "性质", "原因"))
        A("  " + "-" * 74)
        for r in s["top_gain"]:
            A("  %-14s %+6.1f%% %+8.2f亿 %+8.2f亿 %6s%%  %-10s %s" % (
                r["name"][:12], r["chg"], r["main1"], r["main5"],
                ("%.0f" % r["width"]) if r["width"] is not None else "  -",
                r["tag"], r["reason"]))

    if s["top_zt"]:
        A("")
        A("  涨停密度（按涨停原因聚合）")
        A("  %-20s %5s %6s  %s" % ("原因", "涨停", "其中2板+", "解释"))
        A("  " + "-" * 60)
        for r in s["top_zt"]:
            A("  %-20s %5d %6d  %s" % (r["reason"][:18], r["n"], r["n_2board_plus"], r["explain"]))

    if s["reversals"]:
        A("")
        A("  ★★ 深V反转（昨日跌幅榜尾部 → 今日涨幅榜前十）—— 风格切换就在这类地方发生")
        A("     %-14s %8s %10s %10s %9s %s" % ("板块", "今日", "昨日", "昨日排名", "主力1日", "原因"))
        A("     " + "-" * 68)
        for r in s["reversals"]:
            A("     %-14s %+7.1f%% %+9.1f%% %9d %+8.2f亿 %s" % (
                r["name"][:12], r["chg"], r["prev_chg"], r["prev_rank"], r["main1"], r["reason"]))

    if s["rank_jump"]:
        A("")
        A("  排名跃升 ≥30 名（次要，同样要能说出原因）")
        A("  %-14s %8s %8s %10s %6s  %s" % ("板块", "今日", "昨日", "排名变化", "主力", "原因"))
        A("  " + "-" * 66)
        for r in s["rank_jump"]:
            A("  %-14s %+7.1f%% %+7.1f%% %4d→%-4d %+7.2f亿  %s" % (
                r["name"][:12], r["chg"], r["prev_chg"], r["prev_rank"], r["rank"],
                r["main1"], r["reason"]))

    if s["alerts"]:
        A("")
        A("  告警")
        for a in s["alerts"]:
            A("    ⚠ %s" % a)

    # ---- B 复权失真 ----
    A("")
    A("【B · 不复权失真】窗口内除过权的票（跌幅榜会被它们污染）")
    if not rep["distortion"]:
        A("  ✓ 无失真 ≥%.0f 点的票" % DISTORT_PCT)
    else:
        A("  %-10s %-10s %11s %11s %9s %s" % ("代码", "名称", "60日(不复权)", "60日(复权)", "差异", "最新日"))
        A("  " + "-" * 72)
        for c, nm, raw, adj, diff, d in rep["distortion"]:
            A("  %-10s %-10s %+10.1f%% %+10.1f%% %+8.1f点 %s" % (c, (nm or "")[:8], raw, adj, diff, d))
        A("  ⇒ 按跌幅选票时必须用 --adj 口径，否则这些票会虚高排到前面")

    # ---- C 指数 MA20 双值 ----
    A("")
    A("【C · 指数 MA20 双值】禁单报（11-05 曾把截至昨日值 4136 当成当日值）")
    if not rep["index"]:
        A("  ✗ 无 idx_hs300.csv 缓存 ⇒ 需实时接口取收盘价后重算")
    else:
        A("  %-10s %11s %11s %10s %10s" % ("指数", "MA20含今日", "MA20截至昨", "收盘", "距含今MA20"))
        A("  " + "-" * 62)
        for code, v in rep["index"].items():
            nm = {"sh000300": "沪深300"}.get(code, code)
            dev = (v["close"] / v["ma20_today"] - 1) * 100
            A("  %-10s %11.2f %11.2f %10.2f %+9.2f%%" % (
                nm, v["ma20_today"], v["ma20_prev"], v["close"], dev))
            A("     两值相差 %.2f 点；缓存末日 %s%s" % (
                abs(v["ma20_today"] - v["ma20_prev"]), v["last_date"],
                "（⚠ 缓存滞后于体检日，MA20 非当日值）" if v.get("stale") else
                "（若该日缓存为盘中生成，收盘价须与实时接口核对）"))

    # ---- D 数据缺口 ----
    A("")
    A("【D · 当日数据缺口】")
    if not rep["gaps"]:
        A("  ✓ 关键件齐全")
    else:
        for g in rep["gaps"]:
            A("  %s %-22s %s" % ("✗" if g["critical"] else "·", g["file"], g["what"]))

    # ---- E 确定性日程 ----
    A("")
    A("【E · 未来 7 日确定性日程】制度性日期（外部事件靠 A 段归因清单兜）")
    if not rep["events"]:
        A("  （未来 7 日无制度性事件）")
    else:
        for ds, ev, imp in rep["events"]:
            A("  %s  %-18s %s" % (ds, ev, imp))

    # ---- 待办 ----
    A("")
    A("【待办】今天必须能回答的：")
    n = 0
    for r in s.get("top_gain", []):
        n += 1
        A("  %d. %s 为什么涨 %.1f%%？（性质提示：%s）" % (n, r["name"], r["chg"], r["why"] or r["tag"]))
    for r in s.get("reversals", [])[:4]:
        n += 1
        A("  %d. %s 昨天 %.1f%%（排名第 %d）今天 %.1f%%，是什么事件让它一天翻过来？" % (
            n, r["name"], r["prev_chg"], r["prev_rank"], r["chg"]))
    if not n:
        A("  （板块数据缺失，无法生成待办）")
    return "\n".join(L)


def render_adj(per, limit=30):
    """--adj：复权修正后的跌幅榜（选票用这个，不用 close 相除）

    ★ 默认剔除退市/ST/低价 —— 否则榜首永远是"退市XX -97%"这种不能买的票，
      榜单看着严谨其实零信息量（第一版就跑出了这个结果）。
    """
    rows = []
    for c, seq in per.items():
        if len(seq) < 62:
            continue
        nm = (seq[-1][8] or "")
        if "退" in nm or "ST" in nm.upper():
            continue
        if seq[-1][2] < 1.5:          # 低于 1.5 元的票不进榜（博弈盘，非本口径目标）
            continue
        adj = adj_returns(seq, 60)
        raw = raw_returns(seq, 60)
        if adj is None:
            continue
        rows.append((c, nm, seq[-1][2], adj, raw, seq[-1][0]))
    rows.sort(key=lambda x: x[3])
    L = ["【复权口径 60 日跌幅榜】前 %d（已剔退市/ST/股价<1.5元）" % limit,
         "%-10s %-10s %9s %12s %12s %9s" % ("代码", "名称", "收盘", "60日(复权)", "60日(不复权)", "失真")]
    L.append("-" * 68)
    for c, nm, cl, adj, raw, d in rows[:limit]:
        L.append("%-10s %-10s %9.2f %+11.1f%% %+11.1f%% %+8.1f点" % (
            c, nm[:8], cl, adj, raw, raw - adj))
    # 顺手报一下这批里被除权污染了多少（有失真就没法直接比较）
    dirty = [r for r in rows if abs(r[4] - r[3]) >= DISTORT_PCT]
    if dirty:
        L.append("\n  ⚠ 榜内 %d 只窗口内含除权（失真≥%.0f 点），"
                 "不复权口径会把它们排得更靠前：%s" % (
                     len(dirty), DISTORT_PCT,
                     "、".join("%s(%+.0f点)" % (r[1][:6], r[4] - r[3]) for r in dirty[:5])))
    L.append("\n  ⚠ 本地 name 滞后（实测：联创光电已被 ST，本地仍是旧名）"
             "⇒ ST/退市过滤只作用于旧名，名单外发前必须用实时接口核一遍名称")
    return "\n".join(L)


# ── 自检 ────────────────────────────────────────────────────────────────────
def _selftest():
    ok = fail = 0

    def T(name, cond, extra=""):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s%s" % ("✓" if cond else "✗", name, ("  " + extra) if extra and not cond else ""))

    print("== close_audit --selftest ==")

    # T1 复权累乘：构造一次 2:1 拆股。不复权 close 从 20 跳到 10（-50%），
    #    但 change_pct=0（真实没跌）。复权收益必须是 0%，不复权必须是 -50%。
    seq = [("D%d" % i, 20.0, 20.0, 20.0, 20.0, 1.0, 1.0, 0.0, "X") for i in range(5)]
    seq += [("D5", 10.0, 10.0, 10.0, 10.0, 1.0, 1.0, 0.0, "X")]
    adj = adj_returns(seq, 1)
    raw = raw_returns(seq, 1)
    T("T1 拆股日：change_pct=0 → 复权收益 0%%", adj is not None and abs(adj) < 1e-9, str(adj))
    T("T2 拆股日：不复权收益 -50%%", raw is not None and abs(raw + 50) < 1e-6, str(raw))

    # T3 除权失真识别：把上面这只放进 per，应被 adj_distortion 抓到
    per = {"sh600000": seq + [("D6", 10.0, 10.0, 10.0, 10.0, 1.0, 1.0, 0.0, "拆股股")] * 0}
    per["sh600000"] = [("D%d" % i, 20.0, 20.0, 20.0, 20.0, 1.0, 1.0, 0.0, "拆股股") for i in range(60)]
    per["sh600000"].append(("D60", 10.0, 10.0, 10.0, 10.0, 1.0, 1.0, 0.0, "拆股股"))
    dist = adj_distortion(per, n=60, thresh=5.0)
    T("T3 除权票被失真体检抓到", len(dist) == 1 and dist[0][0] == "sh600000", str(len(dist)))
    T("T4 失真幅度约 -50 点", dist and abs(dist[0][4] + 50) < 1e-6, str(dist[0][4] if dist else None))

    # T5 无除权时不该报失真：纯上涨序列 raw 与 adj 应一致
    up = [("D%d" % i, 10.0, 10.0 * (1.01 ** i), 10.0 * (1.01 ** i), 10.0, 1.0, 1.0,
           0.0 if i == 0 else 1.0, "涨股") for i in range(61)]
    a2 = adj_returns(up, 60)
    r2 = raw_returns(up, 60)
    T("T5 无除权时两口径一致", a2 is not None and r2 is not None and abs(a2 - r2) < 1e-6,
      "%s vs %s" % (a2, r2))

    # T6 MA20 双值必须不同：单调上涨序列里含今日 > 截至昨日
    m = idx_ma20_both("sh600000", {})
    T("T6 空数据返回 None 不抛异常", m is None)
    rising = {"sh000300": [("D%d" % i, 1, 100.0 + i, 1, 1, 1, 1, 0.0, "idx") for i in range(25)]}
    mb = idx_ma20_both("sh000300", rising)
    T("T7 上涨序列：含今日MA20 > 截至昨日MA20", mb is not None and mb[0] > mb[1],
      "%s vs %s" % (mb[0], mb[1]) if mb else "")
    T("T8 两者不相等（禁单报的直接理由）", mb is not None and abs(mb[0] - mb[1]) > 1e-9)

    # T9 板块定性：涨3%+ 钱进 + 宽度90% → 真实启动
    T("T9 涨/钱进/宽度足 → 真实启动", _classify(4.0, 2.0, -1.0, 0.9)[0] == "真实启动")
    T("T10 涨但钱走 → 拉高派发", _classify(4.0, -2.0, -1.0, 0.9)[0] == "拉高派发")
    T("T11 涨/钱进/5日流出 → 首日回流", _classify(4.0, 2.0, -1.0, 0.5)[0] == "首日回流")
    T("T12 涨幅不足不算异动", _classify(1.0, 5.0, 5.0, 1.0)[0] == "—")

    # T13 NaN change_pct 不得污染累乘（缺失日跳过，不当 0 处理）
    nanseq = [("D%d" % i, 10.0, 10.0, 10.0, 10.0, 1.0, 1.0, float("nan"), "缺") for i in range(5)]
    T("T13 NaN 超两成 → 返回 None（不硬给结论）", adj_returns(nanseq, 5) is None)

    # T14~T16 列错位自愈（2026-11-05 实证：sector_ranking.csv 表头比数据少一列，
    #          按名取 up_count 会静默拿到换手率 4.73）
    real_row = ["pt01801081", "半导体", "11.41", "1939645.76", "-875552.69",
                "-4624146.65", "26719824.0", "4.73", "181/183"]
    r = _find_width(real_row)
    T("T14 错位真实行能取到宽度 181/183=98.9%", r is not None and abs(r - 181.0 / 183) < 1e-9,
      str(r))
    T("T15 不会把换手率 4.73 当宽度", r is not None and abs(r - 4.73) > 1e-6)
    aligned = ["pt01801085", "消费电子", "2.17", "238748.76", "-73753.58",
               "4069027", "1.68", "78/91", "长盈精密(19.99)"]
    r2 = _find_width(aligned)
    T("T16 对齐行同样能取到宽度 78/91",
      r2 is not None and abs(r2 - 78.0 / 91) < 1e-9, str(r2))
    T("T17 无宽度字段返回 None（不瞎猜）", _find_width(["a", "b", "1.0", "2.0"]) is None)

    # T18/T19 制度性日程：第三个周五 = 股指期货交割；每月 20 日 = LPR
    ev = rule_events("2026-11-18", horizon=7)
    T("T18 2026-11-20 识别为股指期货交割日",
      any(d == "2026-11-20" and "股指期货" in e for d, e, _ in ev), str([x[0] for x in ev]))
    ev2 = rule_events("2026-11-19", horizon=2)
    T("T19 2026-11-20 识别为 LPR 报价日",
      any(d == "2026-11-20" and "LPR" in e for d, e, _ in ev2), str([x[0] for x in ev2]))
    T("T20 非法日期返回空列表不抛异常", rule_events("bad-date") == [])

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return fail == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--prev-date", default=None)
    ap.add_argument("--adj", action="store_true", help="输出复权修正后的60日跌幅榜")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--json", action="store_true", help="另存 data/generated/close_audit_<D>.json")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return 0 if _selftest() else 1

    per = load_klines()

    if a.adj:
        print(render_adj(per, limit=a.limit))
        return 0

    if not a.date:
        # 默认取 klines 里最新日期
        dates = {s[-1][0] for s in per.values()}
        a.date = max(dates) if dates else None
        if not a.date:
            print("✗ 无行情数据")
            return 1

    rep = build(a.date, prev_date=a.prev_date, limit=a.limit)
    print(render(rep))

    if a.json:
        os.makedirs(GEN, exist_ok=True)
        p = os.path.join(GEN, "close_audit_%s.json" % a.date)
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rep, ensure_ascii=False, indent=2))
        print("\n[落盘] %s" % os.path.relpath(p, _ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
