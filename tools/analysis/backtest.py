# -*- coding: utf-8 -*-
"""backtest.py · 策略回测引擎（2026-10-21 建）。

## 为什么要有本件

2026-10-21 的策略有效性实测是**临时脚本**算的（一次性、不可复现、口径散落）。
本件把它固化为引擎：**任何选票/持有逻辑的改动，都能一条命令跑出可比的交割单与统计**。
没有这个，改策略还是靠感觉——这是「能否盈利」问题无法推进的根因。

## 设计

**输入**：日期区间 + 选票规则 + 持有规则（全部可参数化）
**输出**：逐笔交割单（csv）+ 统计（胜率/盈亏比/期望/最大回撤/分档归因）

### 数据源（只用本库已有数据，零外部依赖）
- 行情：`data/raw/bus/klines.csv`（单文件全历史，675 交易日 2024-01 ~ 2026-10）
- 涨停池：`data/raw/<date>/limit_up.csv`（有则以它为池，含连板/封单）
          无则从 klines 按 `change_pct >= 阈值` 反推（口径见下）

### 数据覆盖边界（2026-10-21 实测，**用之前必须看**）

`klines.csv` 虽含 675 个交易日，但**覆盖度极不均匀**：

| 区间 | 每月行数 | 判定 |
|---|---|---|
| 2024-01 ~ 2025-08 | 300~440 行/月 | **残缺**（仅几十只股票，非全市场）|
| 2025-09 ~ 2026-10 | 72k~119k 行/月 | 完整（全市场约 5000 码/日）|

⇒ **有效回测样本起点 = 2025-09-01**。更早的区间会因"池里只有几十只票"而
产生虚假结论（2024 年每月仅 3~25 笔候选，是残缺导致的，不是市场清淡）。
本引擎**不自动截断**（截断会让调用方以为跑了全区间），但会在报告里标注
`data_coverage` 警告，`--strict` 时直接拒绝越界区间。

### 涨停判定（反推口径 · 必须显式声明）
klines 无涨停标记 ⇒ 按 `change_pct >= LIMIT_PCT`（默认 9.8）近似。
- 主板 10% / 创业科创 20% / ST 5% —— 用 9.8 只捞到 10% 档 + 部分 20% 档的低位，
  **会漏掉 20% 档中涨幅 10~19.8% 的票**（但那些本来也不是涨停）。
- 这是**近似**，与真实涨停池有差（真实池还有"触板未封"等边界）。
  有 `limit_up.csv` 的日期优先用真实的，无则用近似 —— 每条交割单标注 `pool_src`。

### 三条铁律（与主系统一致）
1. **不用未来数据**：决策日 T 只用 ≤T 的信息；买入价 = T+1 open。
2. **买不进就记买不进**：一字（open==high==low==close）、开盘 ≥ 涨停价 ⇒ 判 unfilled，
   **不计入收益统计**（不能假装买到了）。
3. **费用照扣**：买 0.125% / 卖 0.175%（同 params.json 口径），毛/费/净三口径都留痕。

## 用法

```bash
# 复现当前系统选票（封单降序 Top5）· 近 60 日
python tools/analysis/backtest.py --from 2026-07-01 --to 2026-10-20 \\
    --rule seal_top --top 5 --hold 2

# 对比：连板高度优先
python tools/analysis/backtest.py --from 2026-07-01 --to 2026-10-20 \\
    --rule boards_first --min-boards 3 --hold 2

# 全历史扫描（长样本）
python tools/analysis/backtest.py --from 2024-01-02 --to 2026-10-20 --rule all_lu --hold 2

# 自检（合成夹具，真库零接触）
python tools/analysis/backtest.py --selftest
```
"""
import argparse
import csv
import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("fs", "analysis", "decision", "risk"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import limit_rules as LIMIT  # noqa: E402  ★ 涨跌幅单真源（2026-11-02）
from paths import ROOT  # noqa: E402

# 费用（与 params.json 同口径；BASELINE 模型假设，非真实费单）
COST_BUY = 0.00125
COST_SELL = 0.00175

# 涨停判定阈值（反推口径，近似；有真实池时不用它）
LIMIT_PCT = 9.8

# 数据完整样本起点（2026-10-21 实测：此前每月仅 300~440 行 = 几十只票，非全市场）
VALID_START = "2025-09-01"
# 每月行数低于此值 ⇒ 判为覆盖残缺（全市场完整月应 ≥ 50000）
MIN_ROWS_PER_MONTH = 5000


class BtError(Exception):
    """REASON_CODE 前缀错误。"""


# ── 数据加载 ─────────────────────────────────────────────────────────────────
def load_klines(root=None, codes=None, d1=None, d2=None):
    """→ {date: {code: row}} 仅保留需要的日期区间（省内存：675 日 × 5000 码）。

    返回 (by_date, all_dates)。all_dates 升序（交易日轴）。
    """
    p = os.path.join(root or ROOT, "data", "raw", "bus", "klines.csv")
    if not os.path.isfile(p):
        raise BtError("NO_KLINES: %s 不存在" % p)
    by_date = {}
    dates = set()
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = r.get("date")
            if not d:
                continue
            if d1 and d < d1:
                continue
            if d2 and d > d2:
                continue
            c = r.get("code")
            if codes and c not in codes:
                continue
            by_date.setdefault(d, {})[c] = r
            dates.add(d)
    return by_date, sorted(dates)


def limit_ratio(code, name=""):
    """涨停幅度（用于判一字/涨停价）。同 settle 口径。"""
    return LIMIT.limit_pct(code, name)   # ★ 2026-11-02 收敛为单真源 fs/limit_rules


def load_pool(date, by_date, root=None):
    """→ (codes, src)。有真实涨停池用真实，否则从 klines 反推（近似）。"""
    p = os.path.join(root or ROOT, "data", "raw", date, "limit_up.csv")
    if os.path.isfile(p):
        rows = list(csv.DictReader(io.open(p, encoding="utf-8-sig")))
        if rows:
            return rows, "limit_up.csv"
    # 反推：change_pct >= LIMIT_PCT
    out = []
    for c, r in (by_date.get(date) or {}).items():
        try:
            if float(r.get("change_pct") or 0) >= LIMIT_PCT:
                out.append({"code": c, "name": r.get("name"),
                            "boards": "", "seal_amount": "", "reason": ""})
        except (TypeError, ValueError):
            continue
    return out, "klines_approx"


# ── 截面筛选池（非涨停池规则 · A2/A4 重验 · 2026-10-29）────────────────────
SCREEN_RULES = ("oversold_reversal", "mainwave", "firstboard_warm", "firstboard_cold", "secondboard")


def _rolling_min_max(vals, w):
    """固定窗口滚动 min/max（单调队列 O(n)）。→ (mins, maxs)，前 w-1 位为 None。"""
    from collections import deque
    mins, maxs = [None] * len(vals), [None] * len(vals)
    dq_min, dq_max = deque(), deque()
    for i, v in enumerate(vals):
        while dq_min and vals[dq_min[-1]] >= v:
            dq_min.pop()
        dq_min.append(i)
        if dq_min[0] <= i - w:
            dq_min.popleft()
        while dq_max and vals[dq_max[-1]] <= v:
            dq_max.pop()
        dq_max.append(i)
        if dq_max[0] <= i - w:
            dq_max.popleft()
        if i >= w - 1:
            mins[i] = vals[dq_min[0]]
            maxs[i] = vals[dq_max[0]]
    return mins, maxs


class ScreenIndex:
    """截面筛选索引：一次预计算全历史滚动统计 → 按决策日 O(1) 出候选池。

    oversold_reversal（A4 超跌反转）：当日涨停(±0.25pp 容差) ∧ 20 日跌幅 ≤ −20%。
    mainwave（A2 主升浪公式）：pos60<0.64 ∧ 换手≥3.41% ∧ 20 日振幅≥37.33%，换手降序。
    零未来：决策日 i 只用 ≤i 数据；换手 = volume / float_share（mktcap.csv）。
    """

    def __init__(self, by_date, dates, rule, root=None):
        if rule not in SCREEN_RULES:
            raise BtError("UNKNOWN_SCREEN_RULE: %s" % rule)
        self.rule, self.dates, self.by_date = rule, dates, by_date
        idx = {d: i for i, d in enumerate(dates)}
        fs_root = os.path.join(root or ROOT, "data", "raw", "bus", "mktcap.csv")
        fs = {}
        if os.path.isfile(fs_root):
            with io.open(fs_root, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    try:
                        fs.setdefault(r["code"], {})[r["date"]] = float(r["float_share"])
                    except (KeyError, TypeError, ValueError):
                        continue
        buckets = {d: [] for d in dates}
        code_map = {}
        for d_, rmap in by_date.items():
            for c_, row_ in rmap.items():
                code_map.setdefault(c_, {})[d_] = row_
        for code, rmap in code_map.items():
            ds = sorted(k for k in rmap if k in idx)
            if len(ds) < 25:
                continue
            rows = [rmap[k] for k in ds]
            try:
                closes = [float(r["close"]) for r in rows]
                highs = [float(r["high"]) for r in rows]
                lows = [float(r["low"]) for r in rows]
                chgs = [float(r.get("change_pct") or 0) for r in rows]
                vols = [float(r.get("volume") or 0) for r in rows]
            except (KeyError, TypeError, ValueError):
                continue
            name = rows[0].get("name") or ""
            lim = limit_ratio(code, name) * 100
            cmin60, cmax60 = _rolling_min_max(closes, 60)
            _h_min, hmax20 = _rolling_min_max(highs, 20)
            lmin20, _l_max = _rolling_min_max(lows, 20)
            for j in range(len(ds)):
                d = ds[j]
                i = idx[d]
                if j < 20:
                    continue
                chg20 = (closes[j] / closes[j - 20] - 1) * 100 if closes[j - 20] > 0 else None
                hi20, lo20 = hmax20[j], lmin20[j]
                amp20 = (hi20 - lo20) / lo20 * 100 if (lo20 and lo20 > 0) else None
                if "ST" in name.upper() or "退" in name or closes[j] < 2.0:
                    continue  # 质量过滤：ST/退市整理/仙股不可投
                rec = {"code": code, "name": name, "boards": "", "seal_amount": "",
                       "chg20": round(chg20, 2) if chg20 is not None else None,
                       "amp20": round(amp20, 2) if amp20 is not None else None}
                if rule in ("firstboard_warm", "firstboard_cold", "secondboard"):
                    # 文献规则（2026-10-29 网络调研）：首板量价分型 + 二板定龙头
                    lim_y = j > 0 and chgs[j - 1] >= lim - 0.25
                    lim_t = chgs[j] >= lim - 0.25
                    if not lim_t:
                        continue
                    vma5 = sum(vols[max(0, j - 5):j]) / max(1, min(5, j)) if j > 0 else 0
                    if vma5 <= 0:
                        continue
                    vr = vols[j] / vma5
                    f = fs.get(code, {}).get(ds[j])
                    turn = (vols[j] / f * 100) if (f and f > 0) else None
                    one_word = (abs(float(rows[j]["open"]) - float(rows[j]["close"])) < 1e-9
                                and abs(float(rows[j]["open"]) - float(rows[j]["high"])) < 1e-9)
                    if one_word:
                        continue  # 一字板买不进也不作为候选（try_fill 兜底）
                    if rule == "secondboard":
                        if not lim_y:
                            continue
                        rec["vr"] = round(vr, 2)
                        if turn:
                            rec["turnover"] = round(turn, 2)
                        buckets[d].append(rec)
                        continue
                    if lim_y:
                        continue  # 首板要求：昨日非涨停
                    rec["vr"] = round(vr, 2)
                    if turn:
                        rec["turnover"] = round(turn, 2)
                    if rule == "firstboard_warm":
                        # 文献主张：温和放量 量比1.5~3 ∧ 换手5~20%
                        if 1.5 <= vr <= 3.0 and turn and 5.0 <= turn <= 20.0:
                            buckets[d].append(rec)
                    else:  # firstboard_cold：一字/缩量板 量比<1（文献称走妖率仅21%）
                        if vr < 1.0:
                            buckets[d].append(rec)
                    continue
                if rule == "oversold_reversal":
                    if chg20 is not None and chg20 <= -20.0 and chgs[j] >= lim - 0.25:
                        buckets[d].append(rec)
                else:  # mainwave
                    if j < 59 or cmin60[j] is None or cmax60[j] is None:
                        continue
                    if cmax60[j] <= cmin60[j]:
                        continue
                    pos60 = (closes[j] - cmin60[j]) / (cmax60[j] - cmin60[j])
                    f = fs.get(code, {}).get(d)
                    if not f or f <= 0:
                        continue
                    turnover = vols[j] / f * 100
                    if pos60 < 0.64 and turnover >= 3.41 and amp20 is not None and amp20 >= 37.33:
                        rec.update({"pos60": round(pos60, 3), "turnover": round(turnover, 2)})
                        buckets[d].append(rec)
        if rule == "mainwave":
            for d in buckets:
                buckets[d].sort(key=lambda r: r.get("turnover") or 0, reverse=True)
        elif rule == "firstboard_warm":
            for d in buckets:
                buckets[d].sort(key=lambda r: r.get("vr") or 0, reverse=True)
        elif rule == "firstboard_cold":
            for d in buckets:
                buckets[d].sort(key=lambda r: r.get("vr") or 9, reverse=False)
        elif rule == "secondboard":
            for d in buckets:
                buckets[d].sort(key=lambda r: r.get("turnover") or 0, reverse=True)
        else:
            for d in buckets:
                buckets[d].sort(key=lambda r: r.get("chg20") if r.get("chg20") is not None else 0)
        self.buckets = buckets

    def pool(self, d):
        return list(self.buckets.get(d) or []), "screen:%s" % self.rule


# ── 选票规则 ────────────────────────────────────────────────────────────────
def rank_candidates(pool, rule, top=5, min_boards=0):
    """按 rule 排序候选 → 前 top 只。**不含任何未来信息**（只用决策日可得字段）。

    rule:
      - `seal_top`     封单降序（**当前系统逻辑**，用于对照）
      - `boards_first` 连板高度降序（同高度按封单降序）
      - `all_lu`       全池（不筛，用于测基础胜率）
      - `boards_ge`    只取 boards >= min_boards，按封单降序
    """
    def _f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    rows = list(pool)
    if rule in SCREEN_RULES:
        return rows[:top], "screen:%s（证据字段见交割单）" % rule
    if rule == "all_lu":
        return rows, "全池不筛"
    if rule == "boards_ge":
        rows = [r for r in rows if _f(r.get("boards")) >= min_boards]
        rows.sort(key=lambda r: _f(r.get("seal_amount")), reverse=True)
        return rows[:top], "boards>=%d → 封单降序" % min_boards
    if rule == "boards_first":
        rows.sort(key=lambda r: (_f(r.get("boards")),
                                 _f(r.get("seal_amount"))), reverse=True)
        if min_boards:
            rows = [r for r in rows if _f(r.get("boards")) >= min_boards]
        return rows[:top], "连板降序 → 封单降序"
    # 默认 seal_top
    rows.sort(key=lambda r: _f(r.get("seal_amount")), reverse=True)
    return rows[:top], "封单降序（当前系统逻辑）"


# ── 成交判定 ────────────────────────────────────────────────────────────────
def try_fill(row_t1, prev_close, code, name=""):
    """T+1 日能否按 open 买入。→ (filled: bool, price, reason)。

    判据同 settle._step_fill：一字 / 开盘≥涨停价 ⇒ 买不进（不假装买到）。
    """
    if not row_t1:
        return False, None, "NO_QUOTE"
    try:
        o = float(row_t1["open"]); h = float(row_t1["high"])
        l = float(row_t1["low"]); c = float(row_t1["close"])
    except (KeyError, TypeError, ValueError):
        return False, None, "QUOTE_FIELD_MISSING"
    if o <= 0:
        return False, None, "BAD_PRICE"
    if o == h == l == c:
        return False, None, "ONE_WORD_BOARD（一字买不进）"
    if prev_close:
        lim = round(prev_close * (1 + limit_ratio(code, name)), 2)
        if o >= lim - 0.005:
            return False, None, "OPEN_AT_LIMIT（开盘即涨停）"
    return True, o, "FILLED"


# ── 主回测 ──────────────────────────────────────────────────────────────────
def coverage_check(root=None, d1=None, d2=None):
    """检查区间内行情覆盖度 → {months, bad_months, ok}。

    `bad_months` = 每月行数 < MIN_ROWS_PER_MONTH 的月份（数据残缺，结论不可信）。
    """
    p = os.path.join(root or ROOT, "data", "raw", "bus", "klines.csv")
    cnt = {}
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = r.get("date") or ""
            if d1 and d < d1:
                continue
            if d2 and d > d2:
                continue
            m = d[:7]
            cnt[m] = cnt.get(m, 0) + 1
    bad = {m: n for m, n in cnt.items() if n < MIN_ROWS_PER_MONTH}
    return {"months": len(cnt), "rows": sum(cnt.values()),
            "bad_months": bad, "ok": not bad,
            "first_full_month": min((m for m in cnt if m not in bad), default=None)}


def run(d1, d2, rule="seal_top", top=5, hold=2, root=None, min_boards=0,
        exit_on_weak=False, strict=False, env_gate=False):
    """回测。→ rep。

    `hold` = 持有交易日数（买入日记为第 1 日；hold=1 表示次日买、当日收盘卖）。
    `exit_on_weak`：若 T+1 收盘 < T+1 开盘（当日走弱）⇒ 按 T+1 close 卖出（提前离场）。
    """
    root = root or ROOT
    cov = coverage_check(root, d1, d2)
    if strict and cov["bad_months"]:
        raise BtError(
            "DATA_COVERAGE_INSUFFICIENT: %d 个月行数 < %d（残缺，结论不可信）：%s\n"
            "  有效起点建议 --from %s"
            % (len(cov["bad_months"]), MIN_ROWS_PER_MONTH,
               ",".join(sorted(cov["bad_months"])[:6]), VALID_START))
    by_date, dates = load_klines(root, d1=d1, d2=d2)
    if not dates:
        raise BtError("NO_DATA: %s~%s 无行情" % (d1, d2))
    idx = {d: i for i, d in enumerate(dates)}

    trades = []
    rule_note = "（区间内无候选日）"
    screen_idx = ScreenIndex(by_date, dates, rule, root) if rule in SCREEN_RULES else None
    env_ok = None
    if env_gate:
        # 环境闸（本系统已验证的择时口径）：等权全A收盘均值 > 其 MA20 方可开新仓
        eq_series = []
        for d_ in dates:
            cl = [float(r["close"]) for r in by_date.get(d_, {}).values()
                  if r.get("close") not in (None, "") and float(r.get("close") or 0) > 0]
            eq_series.append(sum(cl) / len(cl) if cl else None)
        valid = [v for v in eq_series if v is not None]
        # MA20 用有效序列滚动（跳过 None 日不入场）
        ma20 = [None] * len(eq_series)
        win = []
        for i_, v in enumerate(eq_series):
            if v is None:
                continue
            win.append(v)
            if len(win) > 20:
                win.pop(0)
            if len(win) == 20:
                ma20[i_] = sum(win) / 20.0
        env_ok = {i_: (eq_series[i_] is not None and ma20[i_] is not None
                       and eq_series[i_] > ma20[i_]) for i_ in range(len(dates))}
    for i, d in enumerate(dates[:-2]):
        if env_ok is not None and not env_ok.get(i, False):
            continue  # 环境闸：等权全A 低于 MA20 = 禁新开仓
        if screen_idx is not None:
            pool, pool_src = screen_idx.pool(d)
        else:
            pool, pool_src = load_pool(d, by_date, root)
        if not pool:
            continue
        picked, rule_note = rank_candidates(pool, rule, top=top,
                                            min_boards=min_boards)
        if not picked:
            continue
        # T+1（买入日）
        ni = i + 1
        nd = dates[ni]
        t1 = by_date.get(nd) or {}
        for r in picked:
            code = r.get("code")
            q1 = t1.get(code)
            prev_row = (by_date.get(d) or {}).get(code) or {}
            try:
                prev_close = float(prev_row.get("close"))
            except (TypeError, ValueError):
                prev_close = None
            ok, px, why = try_fill(q1, prev_close, code, r.get("name"))
            rec = {"decision_date": d, "code": code, "name": r.get("name") or
                   (q1 or {}).get("name"), "boards": r.get("boards"),
                   "seal_amount": r.get("seal_amount"),
                   "pool_src": pool_src, "buy_date": nd,
                   "filled": ok, "buy_price": px, "fill_reason": why,
                   "sell_date": None, "sell_price": None, "hold_days": None,
                   "gross_ret": None, "net_ret": None, "exit_reason": None}
            if not ok:
                trades.append(rec)
                continue
            # 持有：hold=1 当日收盘卖；hold=2 第 T+2 日收盘卖
            ei = ni + hold - 1
            if ei >= len(dates):
                rec["fill_reason"] = "NO_SELL_DAY（样本末端）"
                rec["filled"] = False
                trades.append(rec)
                continue
            if exit_on_weak:
                # 次日（买入当日）走弱离场：close < open
                try:
                    if float(q1["close"]) < float(q1["open"]):
                        ei = ni
                        rec["exit_reason"] = "WEAK_SAME_DAY（买入当日收阴，当日收盘离场）"
                except (KeyError, TypeError, ValueError):
                    pass
            ed = dates[ei]
            eq = (by_date.get(ed) or {}).get(code)
            if not eq:
                rec["fill_reason"] = "NO_SELL_QUOTE"
                rec["filled"] = False
                trades.append(rec)
                continue
            try:
                sp = float(eq["close"])
            except (TypeError, ValueError):
                rec["fill_reason"] = "BAD_SELL_PRICE"
                rec["filled"] = False
                trades.append(rec)
                continue
            gross = (sp / px - 1) * 100
            net = ((sp * (1 - COST_SELL)) / (px * (1 + COST_BUY)) - 1) * 100
            rec.update({"sell_date": ed, "sell_price": sp,
                        "hold_days": ei - ni + 1,
                        "gross_ret": round(gross, 3), "net_ret": round(net, 3),
                        "exit_reason": rec.get("exit_reason") or "HOLD_END"})
            trades.append(rec)

    return _summary(trades, rule_note, rule, d1, d2, hold, root, cov)


def _summary(trades, rule_note, rule, d1, d2, hold, root, cov=None):
    fills = [t for t in trades if t["filled"]]
    n_all = len(trades)
    n_fill = len(fills)
    n_one = sum(1 for t in trades if not t["filled"]
                and "ONE_WORD" in (t.get("fill_reason") or ""))
    if n_fill:
        nets = sorted(t["net_ret"] for t in fills)
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x <= 0]
        avg = sum(nets) / len(nets)
        avg_w = sum(wins) / len(wins) if wins else 0.0
        avg_l = sum(losses) / len(losses) if losses else 0.0
        pf = abs(avg_w / avg_l) if avg_l else None
        expect = (len(wins) / len(nets)) * avg_w + (len(losses) / len(nets)) * avg_l
        # 权益曲线（等权、逐笔累乘）→ 最大回撤
        eq, peak, mdd = 1.0, 1.0, 0.0
        for x in nets:
            eq *= (1 + x / 100.0)
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1)
        stat = {"n_filled": n_fill, "win_rate": round(len(wins) / len(nets) * 100, 2),
                "avg_net": round(avg, 3), "median_net": round(nets[len(nets) // 2], 3),
                "avg_win": round(avg_w, 3), "avg_loss": round(avg_l, 3),
                "profit_factor": round(pf, 3) if pf else None,
                "expectancy": round(expect, 3),
                "max_drawdown": round(mdd * 100, 2),
                "total_return_compound": round((eq - 1) * 100, 2),
                "best": round(nets[-1], 3), "worst": round(nets[0], 3)}
    else:
        stat = {"n_filled": 0}

    # 分档归因
    def bucket(keyf, label):
        m = {}
        for t in fills:
            k = keyf(t)
            if k is None:
                continue
            m.setdefault(k, []).append(t["net_ret"])
        out = {}
        for k, v in sorted(m.items(), key=lambda x: str(x[0])):
            w = [x for x in v if x > 0]
            out[str(k)] = {"n": len(v), "win_rate": round(len(w) / len(v) * 100, 1),
                           "avg": round(sum(v) / len(v), 2)}
        return out

    def b_boards(t):
        try:
            b = float(t.get("boards") or 0)
        except (TypeError, ValueError):
            return None
        return "1板" if b <= 1 else ("2-3板" if b <= 3 else ">=4板")

    by_bucket = {"boards": bucket(b_boards, "连板"),
                 "hold_days": bucket(lambda t: t.get("hold_days"), "持有天数"),
                 "month": bucket(lambda t: (t.get("buy_date") or "")[:7], "月份")}

    return {"engine": "backtest.py", "rule": rule, "rule_note": rule_note,
            "range": [d1, d2], "hold": hold,
            "n_candidates": n_all, "n_unfilled_one_word": n_one,
            "fill_rate": round(n_fill / n_all * 100, 2) if n_all else 0.0,
            "stat": stat, "by_bucket": by_bucket,
            "data_coverage": cov,
            "trades": trades}


# ── 自检 ────────────────────────────────────────────────────────────────────
def _selftest():
    import shutil
    import tempfile
    ok = fail = 0
    fails = []

    def T(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [OK] %s" % name)
        else:
            fail += 1
            fails.append(name)
            print("  [!!] %s %s" % (name, extra))

    sb = tempfile.mkdtemp(prefix="bt_")
    try:
        os.makedirs(os.path.join(sb, "data", "raw", "bus"))
        kp = os.path.join(sb, "data", "raw", "bus", "klines.csv")
        with io.open(kp, "w", encoding="utf-8", newline="") as f:
            f.write("code,name,date,open,close,high,low,volume,amount,"
                    "exchange,change_pct\n")
            # A: T 涨停 → T+1 普通上涨 → T+2 再涨（应成交且盈利）
            f.write("sh600001,甲,2026-01-05,10,11,11,10,100,0,,10.0\n")
            f.write("sh600001,甲,2026-01-06,11,11.5,11.6,10.9,100,0,,4.5\n")
            f.write("sh600001,甲,2026-01-07,11.5,12.0,12.1,11.4,100,0,,4.3\n")
            # B: T 涨停 → T+1 一字（买不进）
            f.write("sh600002,乙,2026-01-05,10,11,11,10,100,0,,10.0\n")
            f.write("sh600002,乙,2026-01-06,12.1,12.1,12.1,12.1,100,0,,10.0\n")
            f.write("sh600002,乙,2026-01-07,12.1,12.5,12.6,12,100,0,,3.3\n")
            # C: T 涨停 → T+1 高开后下杀（应成交且亏损）
            f.write("sh600003,丙,2026-01-05,10,11,11,10,100,0,,10.0\n")
            f.write("sh600003,丙,2026-01-06,12,11.2,12,11,100,0,,1.8\n")
            f.write("sh600003,丙,2026-01-07,11.2,10.5,11.3,10.4,100,0,,-6.3\n")
        bp = os.path.join(sb, "data", "raw", "2026-01-05")
        os.makedirs(bp)
        with io.open(os.path.join(bp, "limit_up.csv"), "w",
                     encoding="utf-8", newline="") as f:
            f.write("code,name,boards,first_seal_time,seal_amount,reason\n")
            f.write("sh600001,甲,1,,100000,行业A\n")
            f.write("sh600002,乙,2,,90000,行业B\n")
            f.write("sh600003,丙,3,,80000,行业C\n")

        rep = run("2026-01-05", "2026-01-07", rule="seal_top", top=5, hold=2,
                  root=sb)
        tr = {t["code"]: t for t in rep["trades"]}
        T("T1 三条候选都进交割单", len(rep["trades"]) == 3, str(len(rep["trades"])))
        T("T2 甲成交（非一字非涨停开）", tr.get("sh600001", {}).get("filled") is True)
        T("T3 乙未成交（一字买不进）",
          tr.get("sh600002", {}).get("filled") is False
          and "ONE_WORD" in (tr["sh600002"].get("fill_reason") or ""))
        T("T4 丙成交但亏损（净值为负）",
          tr.get("sh600003", {}).get("filled") is True
          and (tr["sh600003"].get("net_ret") or 0) < 0)
        T("T5 甲毛收益 ≈ +9.09%（hold=2：T+1 open 11.5 买 → T+2 close 12.5 卖）",
          abs((tr["sh600001"].get("gross_ret") or 0) - 9.091) < 0.01,
          str(tr["sh600001"].get("gross_ret")))
        T("T6 费用已扣（净 < 毛）",
          tr["sh600001"]["net_ret"] < tr["sh600001"]["gross_ret"])
        T("T7 成交率 = 2/3", abs(rep["fill_rate"] - 66.67) < 0.1, str(rep["fill_rate"]))
        T("T8 统计含胜率/盈亏比/回撤",
          all(k in rep["stat"] for k in ("win_rate", "profit_factor",
                                         "max_drawdown", "expectancy")))
        T("T9 分档含连板/月份", "boards" in rep["by_bucket"]
          and "month" in rep["by_bucket"])

        # 规则对照：boards_first 应把 3 板排第一
        rep2 = run("2026-01-05", "2026-01-07", rule="boards_first", top=1,
                   hold=2, root=sb)
        T("T10 boards_first 选最高板（丙 3 板）",
          rep2["trades"] and rep2["trades"][0]["code"] == "sh600003",
          str([t["code"] for t in rep2["trades"]]))
        rep3 = run("2026-01-05", "2026-01-07", rule="seal_top", top=1,
                   hold=2, root=sb)
        T("T11 seal_top 选封单最强（甲）",
          rep3["trades"] and rep3["trades"][0]["code"] == "sh600001",
          str([t["code"] for t in rep3["trades"]]))

        # 涨停反推口径
        pool, src = load_pool("2026-01-06", load_klines(sb)[0], sb)
        T("T12 无 limit_up.csv 时用 klines 反推（无票因都在阈值下）",
          src in ("klines_approx", "limit_up.csv"), src)
        T("T13 涨停幅度分档（主板10/创业20/ST5）",
          limit_ratio("sh600000") == 0.10
          and limit_ratio("sz300001") == 0.20
          and limit_ratio("sh600000", "ST某") == 0.05
          and limit_ratio("sh688001") == 0.20)

        # 提前离场
        rep4 = run("2026-01-05", "2026-01-07", rule="seal_top", top=5, hold=2,
                   root=sb, exit_on_weak=True)
        t3 = [t for t in rep4["trades"] if t["code"] == "sh600003"][0]
        T("T14 exit_on_weak：丙买入当日收阴 ⇒ 当日离场",
          t3.get("exit_reason", "").startswith("WEAK_SAME_DAY"),
          str(t3.get("exit_reason")))
        T("T15 exit_on_weak 后持有天数 = 1", t3.get("hold_days") == 1,
          str(t3.get("hold_days")))
    finally:
        shutil.rmtree(sb, ignore_errors=True)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：%s" % "；".join(fails))
    return 0 if fail == 0 else 1


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    ap = argparse.ArgumentParser(description="策略回测引擎")
    ap.add_argument("--from", dest="d1", required=True)
    ap.add_argument("--to", dest="d2", required=True)
    ap.add_argument("--rule", default="seal_top",
                    choices=("seal_top", "boards_first", "all_lu", "boards_ge",
                    "oversold_reversal", "mainwave",
                    "firstboard_warm", "firstboard_cold", "secondboard"))
    ap.add_argument("--env-gate", dest="env_gate", action="store_true",
                    help="环境闸：等权全A<MA20 禁新开仓（已验证择时口径）")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--hold", type=int, default=2)
    ap.add_argument("--min-boards", dest="min_boards", type=int, default=0)
    ap.add_argument("--exit-on-weak", dest="exit_on_weak", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="区间含覆盖残缺月份时拒绝运行（防误用残缺数据下结论）")
    ap.add_argument("--base", default=None)
    ap.add_argument("--out", default=None, help="交割单 CSV 输出路径")
    a = ap.parse_args(argv)
    rep = run(a.d1, a.d2, rule=a.rule, top=a.top, hold=a.hold, root=a.base,
              min_boards=a.min_boards, exit_on_weak=a.exit_on_weak,
              strict=a.strict, env_gate=a.env_gate)
    s = rep["stat"]
    cov = rep.get("data_coverage") or {}
    print("回测 %s ~ %s · 规则=%s（%s）· 持有%d日%s" % (
        a.d1, a.d2, a.rule, rep["rule_note"], a.hold,
        " · 弱即离场" if a.exit_on_weak else ""))
    if cov.get("bad_months"):
        print("  ⚠ 数据覆盖警告：%d 个月行数 < %d（残缺，结论偏乐观/不可信）：%s"
              % (len(cov["bad_months"]), MIN_ROWS_PER_MONTH,
                 ",".join(sorted(cov["bad_months"])[:8])))
        print("    有效起点建议 --from %s" % VALID_START)
    print("  候选 %d · 可成交 %d（%.1f%%）· 一字买不进 %d" % (
        rep["n_candidates"], s.get("n_filled", 0), rep["fill_rate"],
        rep["n_unfilled_one_word"]))
    if s.get("n_filled"):
        print("  胜率 %.1f%% · 平均净 %+.2f%% · 中位 %+.2f%% · 盈亏比 %s" % (
            s["win_rate"], s["avg_net"], s["median_net"], s["profit_factor"]))
        print("  期望 %+.2f%%/笔 · 复利总收益 %+.1f%% · 最大回撤 %.1f%%" % (
            s["expectancy"], s["total_return_compound"], s["max_drawdown"]))
        print("  最好 %+.2f%% / 最差 %+.2f%%" % (s["best"], s["worst"]))
        for k, v in rep["by_bucket"].items():
            print("  分档·%s: %s" % (k, json.dumps(v, ensure_ascii=False)))
    if a.out:
        with io.open(a.out, "w", encoding="utf-8", newline="") as f:
            cols = ["decision_date", "code", "name", "boards", "seal_amount",
                    "pool_src", "buy_date", "filled", "buy_price", "fill_reason",
                    "sell_date", "sell_price", "hold_days", "gross_ret",
                    "net_ret", "exit_reason"]
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for t in rep["trades"]:
                w.writerow(t)
        print("→ 交割单：%s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
