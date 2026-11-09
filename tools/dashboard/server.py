"""dashboard_server.py —— 交易系统可视化面板（只读数据服务）

设计原则
--------
1. **只读**：本服务不写任何文件（不落缓存、不写日志到盘），因此不影响
   `tools/fs/landing.py` 的落点封闭纪律。所有数字实时从既有落盘文件读。
2. **单一真源**：所有数字来自既有落盘产物，绝不自行推算市场级指标：
   - 全市场涨跌/中位  ← `data/raw/<date>/daily_quote.csv`
   - 涨停/连板/炸板率  ← `data/raw/<date>/limit_up.csv` + `env_metrics.json`
   - 板块资金流        ← `data/raw/<date>/sector_ranking.csv`
   - 个股 K 线          ← `data/raw/bus/klines.csv`
   - 盘中截面          ← `data/raw/intraday/<date>/scan_HHMM.csv`
   - 环境档            ← `memory/state/brain_state.md`（解析"环境档"表）
   - 持仓              ← `memory/state/user_positions.md`（唯一真源，解析失败则显原文）
   - 族温度计          ← 复用 `tools/analysis/zibei_thermo.py`
3. **不许骗人**：解析失败 / 数据缺口一律在 UI 上显式标出，不静默补 0。

盘中口径（2026-11-09 修）
------------------------
盘中（09:15~15:05）时今日 `daily_quote.csv` 还没落盘，若用"有 daily_quote 的最新日"
当"今天"，面板会把**昨天收盘**当成实时数据展示 —— 这正是旧版最致命的坑。
现改为三源取"今天"：daily_quote / intraday / 当前日期三者交叉，并把
`quote_today=false` 明确透出，由 UI 挂黄条告知"此刻看到的是 <上一交易日> 收盘"。

用法
----
  python tools/dashboard/server.py                    # 默认 http://127.0.0.1:8770
  python tools/dashboard/server.py --port 8771
  python tools/dashboard/server.py --open --warm

接口
----
  GET /                     面板页面
  GET /api/meta             今日判定、可用日期、各文件 as_of、数据缺口
  GET /api/overview?date=   宽度 / 环境档 / 连板梯度 / 板块资金流
  GET /api/kline?code=&n=   个股 OHLC + MA + 昨收 + 涨跌停价
  GET /api/quotes?codes=    批量报价（自选列表一次取回）
  GET /api/positions        用户持仓（当前仓 / 已了结 分组）
  GET /api/thermo?date=     族温度计
  GET /api/search?q=        代码/名称模糊查
  GET /api/intraday?date=   今日各截面（宽度/涨停/封板演变）+ 最近一帧明细
  GET /api/limitup?date=    当日涨停全量
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import re
import statistics
import sys
import threading
import time
import webbrowser
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))

RAW = os.path.join(ROOT, "data", "raw")
BUS = os.path.join(RAW, "bus")
STATE = os.path.join(ROOT, "memory", "state")

KLINES = os.path.join(BUS, "klines.csv")
INTRADAY = os.path.join(RAW, "intraday")

KEEP_BARS = 260


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _f(x, default=None):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _i(x, default=0):
    v = _f(x)
    return int(v) if v is not None else default


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return ""


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def _hhmm(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else None


_DATES_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def today_str():
    return time.strftime("%Y-%m-%d")


def quote_dates():
    """所有存在 daily_quote.csv 的日期（升序）。"""
    out = []
    if not os.path.isdir(RAW):
        return out
    for d in os.listdir(RAW):
        if _DATES_RE.match(d) and os.path.exists(os.path.join(RAW, d, "daily_quote.csv")):
            out.append(d)
    return sorted(out)


def intraday_dates():
    """所有存在 scan_*.csv 的盘中目录（升序）。"""
    out = []
    if not os.path.isdir(INTRADAY):
        return out
    for d in os.listdir(INTRADAY):
        p = os.path.join(INTRADAY, d)
        if not (_DATES_RE.match(d) and os.path.isdir(p)):
            continue
        if any(f.startswith("scan_") and f.endswith(".csv") for f in os.listdir(p)):
            out.append(d)
    return sorted(out)


def latest_quote_date():
    ds = quote_dates()
    return ds[-1] if ds else None


# 兼容旧调用名
def latest_date():
    return latest_quote_date()


def is_trading_hours(now=None):
    t = now or time.localtime()
    if t.tm_wday >= 5:
        return False
    hm = t.tm_hour * 60 + t.tm_min
    return (9 * 60 + 15) <= hm <= (15 * 60 + 5)


# --------------------------------------------------------------------------
# daily_quote.csv（带 mtime 缓存）
# --------------------------------------------------------------------------

_Q_CACHE = {"key": None, "rows": None, "by_code": None, "lock": threading.Lock()}


def load_quote(date):
    path = os.path.join(RAW, date, "daily_quote.csv")
    key = (path, _mtime(path))
    with _Q_CACHE["lock"]:
        if _Q_CACHE["key"] == key:
            return _Q_CACHE["rows"], _Q_CACHE["by_code"]
        rows, by_code = [], {}
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    pct = _f(r.get("pct"))
                    if pct is None:
                        continue
                    row = {
                        "code": r.get("code", ""),
                        "name": r.get("name", ""),
                        "open": _f(r.get("open")),
                        "high": _f(r.get("high")),
                        "low": _f(r.get("low")),
                        "close": _f(r.get("close")),
                        "prev_close": _f(r.get("prev_close")),
                        "pct": pct,
                        "vol": _f(r.get("vol")),
                        "amount": _f(r.get("amount")),
                    }
                    rows.append(row)
                    by_code[row["code"]] = row
        _Q_CACHE.update(key=key, rows=rows, by_code=by_code)
        return rows, by_code


# --------------------------------------------------------------------------
# limit_up.csv / env_metrics.json / sector_ranking.csv
# --------------------------------------------------------------------------

def load_limit_up(date):
    path = os.path.join(RAW, date, "limit_up.csv")
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if not r.get("code"):
                continue
            out.append({
                "code": r["code"],
                "name": r.get("name", ""),
                "boards": _i(r.get("boards"), 1) or 1,
                "seal": _f(r.get("seal_amount")),
                "first_seal": (r.get("first_seal_time") or "").strip(),
                "reason": (r.get("reason") or "").strip(),
            })
    return out


def load_sectors(date):
    path = os.path.join(RAW, date, "sector_ranking.csv")
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            pct = _f(r.get("change_pct"))
            if pct is None:
                continue
            updn = r.get("up_count") or ""
            up_n = dn_n = None
            if "/" in updn:
                a, b = updn.split("/", 1)
                up_n, dn_n = _f(a), _f(b)
            out.append({
                "code": r.get("code", ""),
                "name": r.get("name", ""),
                "pct": pct,
                # ⚠ 源文件单位是「万元」，不是「元」：元件 377531.93 → 37.75 亿。
                #   此处统一换算成元，避免前端二次踩坑（2026-11-09 实测踩过：显示成 38 万）。
                "net1d": (_f(r.get("main_net_inflow")) or 0.0) * 1e4,
                "net5d": (_f(r.get("main_net_inflow_5d")) or 0.0) * 1e4,
                "width": (100.0 * up_n / dn_n) if (up_n and dn_n) else None,
                "up": up_n,
                "total": dn_n,
                # ⚠ 源字段名虽叫 leader，实测是数值（不是股票名），故改名防误用
                "leader_num": _f(r.get("leader")),
            })
    out.sort(key=lambda x: -(x["net1d"] or 0))
    return out


def env_block():
    """解析 brain_state.md 的「环境档」表。解析不到就返回 None，由前端显式标缺口。"""
    txt = _read_text(os.path.join(STATE, "brain_state.md"))
    if not txt:
        return None
    grab = {}
    keys = {
        "档位": "regime",
        "仓位天花板（成）": "cap",
        "as_of / source": "as_of",
        "涨停家数": "limit_up",
        "连板高度": "max_board",
        "炸板率": "fail_rate",
        "成交额（只记录）": "turnover",
        "涨跌家数比（只记录）": "updown",
        "指数20日线（只记录）": "ma20",
    }
    for line in txt.split("\n"):
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        k = cells[0].strip("* ")
        if k in keys:
            grab[keys[k]] = cells[1].strip("* ")
    if "regime" not in grab:
        return None
    grab["cap"] = _f(re.sub(r"[^\d.]", "", grab.get("cap", "") or ""))
    return grab


def brain_headline():
    """brain_state §〇 的一句话判断（原文，不改写）。"""
    txt = _read_text(os.path.join(STATE, "brain_state.md"))
    if not txt:
        return None
    m = re.search(r"## 〇、一句话判断[^\n]*\n+(.+?)\n\n", txt, re.S)
    if not m:
        return None
    return m.group(1).strip().strip("*").strip()


# --------------------------------------------------------------------------
# 全市场宽度 + 连板梯度
# --------------------------------------------------------------------------

# 首桶用 ASCII 的 "<-9"：中文环境下 "≤" 在部分等宽字体缺字形，canvas 会吞掉
_BUCKETS = ["<-9", "-9~-7", "-7~-5", "-5~-3", "-3~-1", "-1~0",
            "0~1", "1~3", "3~5", "5~7", "7~9", "≥9"]


def _bucket(x):
    if x <= -9:
        return "<-9"
    if x <= -7:
        return "-9~-7"
    if x <= -5:
        return "-7~-5"
    if x <= -3:
        return "-5~-3"
    if x <= -1:
        return "-3~-1"
    if x <= 0:
        return "-1~0"
    if x <= 1:
        return "0~1"
    if x <= 3:
        return "1~3"
    if x <= 5:
        return "3~5"
    if x <= 7:
        return "5~7"
    if x <= 9:
        return "7~9"
    return "≥9"


def breadth(rows):
    pcts = [r["pct"] for r in rows]
    up = sum(1 for x in pcts if x > 0)
    dn = sum(1 for x in pcts if x < 0)
    flat = len(pcts) - up - dn
    cnt = collections.Counter(_bucket(x) for x in pcts)
    return {
        "n": len(pcts),
        "up": up,
        "down": dn,
        "flat": flat,
        "ratio": (up / dn) if dn else None,
        "median": statistics.median(pcts) if pcts else None,
        "mean": (sum(pcts) / len(pcts)) if pcts else None,
        "dist": [{"k": k, "v": cnt.get(k, 0)} for k in _BUCKETS],
        "le3": sum(1 for x in pcts if x <= -3),
        "ge5": sum(1 for x in pcts if x >= 5),
    }


def ladder(limit_rows):
    c = collections.Counter(r["boards"] for r in limit_rows)
    total = sum(c.values())
    n1 = c.get(1, 0)
    return {
        "total": total,
        "first": n1,
        "first_share": (100.0 * n1 / total) if total else None,
        "multi": total - n1,
        "max_board": max(c) if c else 0,
        "grad": [{"boards": b, "n": c[b]} for b in sorted(c.keys(), reverse=True)],
        "top": sorted(limit_rows, key=lambda r: (-r["boards"], -(r["seal"] or 0)))[:80],
    }


def ladder_history(date, n=4):
    """近 n 个交易日的连板结构（**含当日**，前端须去重）。"""
    ds = quote_dates()
    if date in ds:
        ds = ds[:ds.index(date) + 1]
    out = []
    for d in ds[-n:]:
        lu = load_limit_up(d)
        if not lu:
            continue
        lad = ladder(lu)
        out.append({"date": d, "total": lad["total"], "first": lad["first"],
                    "first_share": lad["first_share"], "multi": lad["multi"],
                    "max_board": lad["max_board"]})
    return out


# --------------------------------------------------------------------------
# K 线（单遍扫描 + array 增量，峰值内存约 80MB；旧版用元组全量暂存，峰值 700MB+）
# --------------------------------------------------------------------------

_KL = {
    "snap": None,
    "key": None,
    "lock": threading.Lock(),
    "err": None,
    "building": False,
}


def _build_kline():
    """单遍扫描 klines.csv，边读边写入 array，最后整体截断到 KEEP_BARS 根。

    ★ 旧实现先把 248 万行塞进 list[tuple]，再统一截断 —— 峰值内存 700MB+，
      在低配机上直接卡死/触发换页，表现为"面板点了没反应"。这里改成增量 array：
      峰值约 248万×(4×5+8+4)B ≈ 80MB，建库 2 秒内完成。
    """
    t0 = time.time()
    bars, names = {}, {}
    n = 0
    # 内层循环刻意内联 float()/int() 而不是走 _f()：248 万行 × 8 次函数调用 ≈ 2000 万次
    # 调用，实测白扔掉 2~3 秒。csv.reader 也换成 split（本文件无引号，实测快 0.5 秒）。
    with open(KLINES, encoding="utf-8-sig") as fh:
        next(fh, None)
        for line in fh:
            row = line.split(",")
            n += 1
            if len(row) < 9:
                continue
            code = row[0]
            try:
                o = float(row[3])
                c = float(row[4])
                h = float(row[5])
                l = float(row[6])
                # ⚠ volume 单位跨时段不一致（实测：2024 年比对 0.1、2025-09~2026-09 为手 ≈100、
                #   2026-10 起为股 ≈1）。amount 全程都是元 → 量能一律用 amount，volume 只留备查。
                v = float(row[7]) if row[7] else 0.0
                amt = float(row[8]) if row[8] else 0.0
                d = row[2]
                di = int(d[0:4] + d[5:7] + d[8:10])
            except (ValueError, IndexError):
                continue
            b = bars.get(code)
            if b is None:
                b = bars[code] = {"d": array("i"), "o": array("f"), "h": array("f"),
                                  "l": array("f"), "c": array("f"),
                                  "v": array("d"), "a": array("d")}
                names[code] = row[1]
            b["d"].append(di)
            b["o"].append(o)
            b["h"].append(h)
            b["l"].append(l)
            b["c"].append(c)
            b["v"].append(v)
            b["a"].append(amt)
    # 截断 + 保证升序（源文件按 code 分组，组内一般升序，但不做假设）
    for code, b in bars.items():
        dl = b["d"]
        if len(dl) > KEEP_BARS:
            cut = len(dl) - KEEP_BARS
            for k in b:
                del b[k][:cut]
        # 逆序检测：只在确实乱序时才全量重排
        rev = False
        for i in range(1, len(dl)):
            if dl[i] < dl[i - 1]:
                rev = True
                break
        if rev:
            order = sorted(range(len(dl)), key=lambda j: dl[j])
            for k in b:
                arr = b[k]
                b[k] = array(arr.typecode, [arr[j] for j in order])
    return {"bars": bars, "name": names, "n_rows": n, "build_sec": round(time.time() - t0, 1)}


def _kick_build():
    """后台建库（幂等）。"""
    with _KL["lock"]:
        if _KL["building"]:
            return
        _KL["building"] = True

    def run():
        try:
            snap = _build_kline()
            _KL["snap"] = snap
            _KL["key"] = _mtime(KLINES)
            _KL["err"] = None
        except Exception as e:  # noqa: BLE001
            _KL["err"] = "%s: %s" % (type(e).__name__, e)
        finally:
            _KL["building"] = False

    threading.Thread(target=run, daemon=True).start()


def ensure_kline(block=True):
    key = _mtime(KLINES)
    snap = _KL["snap"]
    if snap is not None and _KL["key"] == key:
        return True
    if not block:
        _kick_build()
        return False
    with _KL["lock"]:
        if _KL["snap"] is not None and _KL["key"] == key:
            return True
        try:
            snap = _build_kline()
        except Exception as e:  # noqa: BLE001
            _KL["err"] = "%s: %s" % (type(e).__name__, e)
            return False
        _KL["snap"] = snap          # 整体替换引用 → 读方不会读到半成品
        _KL["key"] = key
        _KL["err"] = None
        return True


_PENDING = {"ok": False, "pending": True,
            "error": "K线库首次加载中（约 10 秒，一次性），稍后自动重试"}


def _ma(vals, i, n):
    if i + 1 < n:
        return None
    return sum(vals[i + 1 - n:i + 1]) / n


def kline_payload(code, n=160):
    if not ensure_kline(block=False):
        return dict(_PENDING) if not _KL["err"] else {"ok": False, "error": _KL["err"]}
    snap = _KL["snap"]
    b = snap["bars"].get(code)
    if not b:
        return {"ok": False, "error": "K线库中无此代码：%s" % code}
    total = len(b["d"])
    n = max(2, min(int(n or 160), KEEP_BARS))
    start = max(0, total - n)
    idx = list(range(start, total))
    closes, highs, lows, amts = b["c"], b["h"], b["l"], b["a"]
    bars = []
    for i in idx:
        prev = closes[i - 1] if i > 0 else b["o"][i]
        bars.append({
            "d": "%04d-%02d-%02d" % (b["d"][i] // 10000, b["d"][i] // 100 % 100, b["d"][i] % 100),
            "o": round(b["o"][i], 3),
            "h": round(highs[i], 3),
            "l": round(lows[i], 3),
            "c": round(closes[i], 3),
            "a": round(amts[i], 0),      # 成交额（元）——量能图一律用它，不用 volume
            "v": round(b["v"][i], 0),    # 原始 volume：单位跨时段不一致，仅备查
            "p": round(100.0 * (closes[i] / prev - 1.0), 2) if prev else None,
        })
    mas = {}
    for w in (5, 10, 20, 60):
        mas["ma%d" % w] = [
            (round(_ma(closes, i, w), 3) if _ma(closes, i, w) is not None else None)
            for i in idx
        ]
    last_i = idx[-1]
    prev_close = closes[last_i - 1] if last_i > 0 else None
    seg_close = [closes[i] for i in idx]
    last = seg_close[-1]
    prev5 = [amts[i] for i in idx][-6:-1]
    amt_ratio = None
    if len(prev5) == 5:
        avg = sum(prev5) / 5.0
        if avg > 0:
            amt_ratio = round(amts[last_i] / avg, 2)
    return {
        "ok": True,
        "code": code,
        "name": snap["name"].get(code, code),
        "bars": bars,
        "ma": mas,
        "stat": {
            "last": round(last, 3),
            "prev": round(prev_close, 3) if prev_close else None,
            "pct_today": round(100.0 * (last / prev_close - 1.0), 2) if prev_close else None,
            "chg_n": round(100.0 * (last / seg_close[0] - 1.0), 2) if seg_close[0] else None,
            "high": round(max(highs[i] for i in idx), 3),
            "low": round(min(lows[i] for i in idx), 3),
            "ma20": mas["ma20"][-1],
            "ma60": mas["ma60"][-1],
            "amt": round(amts[last_i], 0),
            "amt_ratio": amt_ratio,
            "last_date": "%04d-%02d-%02d" % (b["d"][last_i] // 10000,
                                             b["d"][last_i] // 100 % 100, b["d"][last_i] % 100),
        },
    }


def quotes_payload(codes):
    """批量报价：自选列表一次取回，避免前端串行 N 次请求。"""
    if not ensure_kline(block=False):
        if _KL["err"]:
            return {"ok": False, "error": _KL["err"], "items": []}
        return dict(_PENDING, items=[])
    snap = _KL["snap"]
    items, missing = [], []
    for code in codes:
        b = snap["bars"].get(code)
        if not b or not b["c"]:
            missing.append(code)
            continue
        i = len(b["c"]) - 1
        last = b["c"][i]
        prev = b["c"][i - 1] if i > 0 else None
        items.append({
            "code": code,
            "name": snap["name"].get(code, code),
            "close": round(last, 3),
            "pct": round(100.0 * (last / prev - 1.0), 2) if prev else None,
            "ma20": (round(_ma(b["c"], i, 20), 3) if _ma(b["c"], i, 20) is not None else None),
            "ma5": (round(_ma(b["c"], i, 5), 3) if _ma(b["c"], i, 5) is not None else None),
            "amt": round(b["a"][i], 0),
            "date": "%04d-%02d-%02d" % (b["d"][i] // 10000, b["d"][i] // 100 % 100, b["d"][i] % 100),
        })
    return {"ok": True, "items": items, "missing": missing}


# --------------------------------------------------------------------------
# 持仓（唯一真源 user_positions.md，解析失败显原文）
# --------------------------------------------------------------------------

_CODE_RE = re.compile(r"\b([a-z]{2}\d{6})\b")
_COST_RE = re.compile(r"成本\s*\**\s*([0-9]+(?:\.[0-9]+)?)")
_STOP_RE = re.compile(r"破\s*\**\s*([0-9]+(?:\.[0-9]+)?)\s*\**\s*(?:就|则)")
_STOP_RE2 = re.compile(r"破\s*\**\s*([0-9]+(?:\.[0-9]+)?)")
_REAL_RE = re.compile(r"实得\s*\**\s*([+\-]?[0-9]+(?:\.[0-9]+)?)\s*%")
_SELL_RE = re.compile(r"卖出\s*@\s*([0-9]+(?:\.[0-9]+)?)")


def positions(date):
    path = os.path.join(STATE, "user_positions.md")
    txt = _read_text(path)
    _, by_code = load_quote(date) if date else ({}, {})
    items, warns = [], []
    if not txt:
        return {"ok": False, "error": "读不到 memory/state/user_positions.md", "items": []}
    for line in txt.split("\n"):
        if not line.startswith("|"):
            continue
        if "---" in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        m = _CODE_RE.search(cells[0])
        if not m:
            continue
        code = m.group(1)
        raw_name = re.sub(r"[*~]", "", cells[1]).strip()
        joined = " ".join(cells)
        closed = ("已清仓" in joined) or ("已了结" in joined)
        cost = None
        mc = _COST_RE.search(cells[2]) or _COST_RE.search(joined)
        if mc:
            cost = _f(mc.group(1))
        stop = None
        for rx in (_STOP_RE, _STOP_RE2):
            ms = rx.search(cells[4]) if len(cells) > 4 else None
            if ms:
                stop = _f(ms.group(1))
                break
        q = by_code.get(code)
        cur = q["close"] if q else None
        # ★ 已了结的票不许用收盘价算浮盈（那是「没卖的价格」，不是实得）。
        pnl, pnl_kind, sell_px = None, "float", None
        if closed:
            mr = _REAL_RE.search(joined)
            ms = _SELL_RE.search(joined)
            if ms:
                sell_px = _f(ms.group(1))
            if mr:
                pnl = _f(mr.group(1))
                pnl_kind = "realized"
        elif cur and cost:
            pnl = 100.0 * (cur / cost - 1.0)
        dist = 100.0 * (cur / stop - 1.0) if (cur and stop) else None
        # 告警只针对「当前持仓」——历史已了结票没有实得是本来的事，不该刷屏
        if not closed:
            if cost is None:
                warns.append("%s 成本解析失败，浮盈不可算" % raw_name)
            if q is None:
                warns.append("%s 当日无行情（停牌或数据缺口）" % raw_name)
        items.append({
            "code": code,
            "name": raw_name,
            "active": not closed,
            "cost": cost,
            "close": round(cur, 3) if cur else None,
            "pct": round(q["pct"], 2) if q else None,
            "high": q["high"] if q else None,
            "low": q["low"] if q else None,
            "amount": q["amount"] if q else None,
            "pnl": round(pnl, 2) if pnl is not None else None,
            "pnl_kind": pnl_kind,
            "sell_px": sell_px,
            "stop": stop,
            "stop_dist": round(dist, 2) if dist is not None else None,
            "closed": closed,
            "raw_note": re.sub(r"\s+", " ", cells[3])[:240] if len(cells) > 3 else "",
            "raw_rule": re.sub(r"\s+", " ", cells[4])[:240] if len(cells) > 4 else "",
        })
    # 当前仓在前（按浮盈降序），已了结在后（按原顺序）
    active = [x for x in items if x["active"]]
    done = [x for x in items if not x["active"]]
    active.sort(key=lambda x: (x["pnl"] is None, -(x["pnl"] or 0)))
    return {"ok": True, "as_of": date, "items": active + done,
            "n_active": len(active), "n_closed": len(done),
            "warns": warns,
            "src": "memory/state/user_positions.md",
            "src_label": "用户实盘（口述记录 · 非券商导入）"}


# --------------------------------------------------------------------------
# 族温度计（复用 zibei_thermo）
# --------------------------------------------------------------------------

def _import_thermo():
    p = os.path.join(ROOT, "tools", "analysis")
    if p not in sys.path:
        sys.path.insert(0, p)
    import zibei_thermo  # noqa: PLC0415
    return zibei_thermo


_THERMO_CACHE = {}


def _thermo_rows(date, topk, minlen, prev_date=None):
    zt = _import_thermo()
    rows, lm = zt.load_snapshot(date)
    rows = zt.clean(rows)
    fam, M, L = zt.families(rows, lm, topk=topk, minlen=minlen)
    prev_rate = {}
    if prev_date:
        try:
            prows, plm = zt.load_snapshot(prev_date)
            prows = zt.clean(prows)
            pfam, _, _ = zt.families(prows, plm, topk=max(topk * 4, 40), minlen=minlen)
            prev_rate = {c["gram"]: c["seal_rate"] for c in pfam}
        except Exception:
            prev_rate = {}
    out = []
    for c in fam:
        pr = prev_rate.get(c["gram"])
        out.append({
            "gram": c["gram"],
            "m": c["m"],
            "gl": c["gl"],
            "seal_rate": round(100.0 * c["seal_rate"], 1),
            "enrich": round(c["enrich"], 2),
            "max_boards": int(c["max_boards"]),
            "prev_seal": round(100.0 * pr, 1) if pr is not None else None,
            "delta": round(100.0 * (c["seal_rate"] - pr), 1) if pr is not None else None,
            "names": c["names"][:8],
            "industries": c.get("industries", [])[:5],
            "boards": [int(x) for x in c["boards"]][:10],
            "n_ind": len(set(c.get("industries", []))),
        })
    return out, M, L


def thermo(date, topk=8, singlek=6):
    """双字族（可信）+ 单字族（慎用，但「洋/阳」这类真实抱团就藏在里面）。"""
    key = (date, topk, singlek, "v2")
    if key in _THERMO_CACHE:
        return _THERMO_CACHE[key]
    try:
        zt = _import_thermo()
        ds = [d for d in zt.dates() if d < date]
        prev = ds[-1] if ds else None
        fam, M, L = _thermo_rows(date, topk, 2, prev)
        singles, _, _ = _thermo_rows(date, singlek, 1, prev)
        dup = {c["gram"] for c in fam}
        singles = [c for c in singles if c["gram"] not in dup][:singlek]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                "families": [], "singles": []}
    payload = {"ok": True, "date": date, "n_market": M, "n_limit": L,
               "prev_date": prev, "families": fam, "singles": singles,
               "note": "族封板率在聚 = 当日状态描述；前瞻性检验 t=−0.15、日胜率 45%，不是买点信号（trade-rules §一g.10）"}
    _THERMO_CACHE[key] = payload
    return payload


# --------------------------------------------------------------------------
# 盘中截面
# --------------------------------------------------------------------------

def _scan_files(date):
    folder = os.path.join(INTRADAY, date)
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder)
                  if f.startswith("scan_") and f.endswith(".csv"))


def _read_scan(date, fn):
    rows = []
    with open(os.path.join(INTRADAY, date, fn), encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            pct = _f(r.get("pct"))
            if pct is None:
                continue
            price, ceil, high = _f(r.get("price")), _f(r.get("ceiling")), _f(r.get("high"))
            sealed = bool(price is not None and ceil and price >= ceil - 0.005)
            touched = bool(high is not None and ceil and high >= ceil - 0.005)
            rows.append({
                "code": r.get("code", ""),
                "name": r.get("name", ""),
                "pct": pct,
                "price": price,
                "high": high,
                "prev_close": _f(r.get("prev_close")),
                "amount": _f(r.get("amount")) or 0.0,
                "boards": _i(r.get("boards"), 0),
                "sealed": sealed,
                "touched": touched,       # 曾摸到涨停价（含已炸板）
                "ceiling": ceil,
                "turnover_rate": _f(r.get("turnover_rate")),
                "volume_ratio": _f(r.get("volume_ratio")),
            })
    return rows


def intraday_latest(date=None):
    """最近一份盘中扫描快照 + 今日各截面的宽度演变。

    ★ 为什么要有 frames：单帧只能看"此刻多宽"，看不出"在聚还是在散"。
      09:39 / 10:06 / 11:09 三个截面连起来，才看得出钱是往里涌还是往外撤。
      原始扫描是「截面」不是逐笔 —— 不画分时曲线（raw 里没有逐分钟行情）。
    """
    days = intraday_dates()
    if not days:
        return {"ok": False, "error": "无盘中扫描记录（data/raw/intraday 为空）"}
    d = date if date in days else days[-1]
    files = _scan_files(d)
    if not files:
        return {"ok": False, "error": "%s 无 scan_*.csv" % d}
    fn = files[-1]
    hhmm = fn[len("scan_"):-4]
    rows = _read_scan(d, fn)
    if not rows:
        return {"ok": False, "error": "%s 为空" % fn}
    non_st = [r for r in rows if "ST" not in r["name"]]
    b = breadth(non_st)
    sealed_rows = [r for r in non_st if r["sealed"]]
    for r in sealed_rows:
        if r["boards"] <= 0:
            r["boards"] = 1
    lad = ladder([{"boards": r["boards"], "seal": r["amount"], "code": r["code"],
                   "name": r["name"], "reason": "", "first_seal": ""} for r in sealed_rows])
    # 今日各截面演变
    frames = []
    for f in files[-8:]:
        fr = _read_scan(d, f)
        nst = [r for r in fr if "ST" not in r["name"]]
        if not nst:
            continue
        ups = sum(1 for r in nst if r["pct"] > 0)
        dns = sum(1 for r in nst if r["pct"] < 0)
        sl = [r for r in nst if r["sealed"]]
        frames.append({
            "hhmm": f[len("scan_"):-4],
            "up": ups,
            "down": dns,
            "ratio": round(ups / dns, 2) if dns else None,
            "sealed": len(sl),
            "multi": sum(1 for r in sl if (r["boards"] or 1) >= 2),
            "median": round(statistics.median(r["pct"] for r in nst), 2),
        })
    # 盘中炸板率 / 全市场成交额：收盘口径来自 env_metrics.json，盘中只能自己算
    touched = [r for r in non_st if r["touched"]]
    fail_rate = None
    if touched:
        fail_rate = round(1.0 - len(sealed_rows) / len(touched), 6)
    turnover = round(sum(r["amount"] for r in non_st), 0)
    return {
        "ok": True,
        "date": d,
        "as_of": "%s %s:%s" % (d, hhmm[:2], hhmm[2:]),
        "hhmm": hhmm,
        "file": "data/raw/intraday/%s/%s" % (d, fn),
        "n": len(non_st),
        "breadth": b,
        "sealed": len(sealed_rows),
        "touched": len(touched),
        "fail_rate": fail_rate,
        "turnover": turnover or None,
        "ladder": lad,
        "frames": frames,
        "n_frames": len(files),
        "is_today": d == today_str(),
        "is_latest_quote_day": d == latest_quote_date(),
    }


# --------------------------------------------------------------------------
# 涨停全量
# --------------------------------------------------------------------------

def limitup_payload(date=None):
    ds = quote_dates()
    if not date or date not in ds:
        date = ds[-1] if ds else None
    if not date:
        return {"ok": False, "error": "无任何 daily_quote.csv 落盘", "items": []}
    lu = load_limit_up(date)
    _, by_code = load_quote(date)
    items = []
    for r in lu:
        q = by_code.get(r["code"]) or {}
        items.append({
            "code": r["code"],
            "name": r["name"],
            "boards": r["boards"],
            "seal": r["seal"],
            "first_seal": r["first_seal"],
            "reason": r["reason"],
            "pct": q.get("pct"),
            "amount": q.get("amount"),
            "turnover_rate": q.get("turnover_rate"),
        })
    items.sort(key=lambda x: (-x["boards"], -(x["seal"] or 0)))
    # 缺口按「实际有值的条数」判，不能只看第一条
    n_seal = sum(1 for x in items if x["seal"])
    n_time = sum(1 for x in items if x["first_seal"])
    gap = []
    if items and n_seal < len(items) * 0.5:
        gap.append("seal_amount 仅 %d/%d 有值（封单额大面积缺失）" % (n_seal, len(items)))
    if items and n_time == 0:
        gap.append("first_seal_time 全空（源未落盘封板时间）")
    return {"ok": True, "date": date, "is_today_quote": date == today_str(),
            "n": len(items), "items": items, "gap": gap}


# --------------------------------------------------------------------------
# 搜索
# --------------------------------------------------------------------------

def search_stocks(q, limit=20):
    """代码或名称模糊查（只用内存里的 K 线索引，不读盘）。"""
    q = (q or "").strip()
    if not q:
        return {"ok": True, "items": []}
    if not ensure_kline(block=False):
        if _KL["err"]:
            return {"ok": False, "error": _KL["err"], "items": []}
        return dict(_PENDING, items=[])
    ql = q.lower()
    exact, starts, contains = [], [], []
    for code, name in _KL["snap"]["name"].items():
        if code == ql or name == q:
            exact.append((code, name))
        elif code.startswith(ql) or name.startswith(q):
            starts.append((code, name))
        elif ql in code or q in name:
            contains.append((code, name))
        # ★ 不再提前 break：旧版命中数一超限就跳出，遍历顺序是 dict 插入序，
        #   导致"黄河"这类常见字有时查得到有时查不到。
    starts.sort(key=lambda x: (len(x[1]), x[1]))
    contains.sort(key=lambda x: (len(x[1]), x[1]))
    out = (exact + starts + contains)[:limit]
    return {"ok": True, "items": [{"code": c, "name": n} for c, n in out]}


# --------------------------------------------------------------------------
# 汇总接口
# --------------------------------------------------------------------------

_META_FILES = ("daily_quote.csv", "limit_up.csv", "env_metrics.json",
               "sector_ranking.csv", "dragon_tiger.csv", "news.jsonl",
               "regulatory.jsonl", "holder_behavior.jsonl")


def meta_payload():
    t = today_str()
    qd = quote_dates()
    idt = intraday_dates()
    d = qd[-1] if qd else None
    files = {}
    if d:
        for fn in _META_FILES:
            p = os.path.join(RAW, d, fn)
            ex = os.path.exists(p)
            files[fn] = {"bytes": os.path.getsize(p) if ex else 0,
                         "as_of": _hhmm(_mtime(p)) if ex else None}
    warnings = []
    if d:
        for fn, info in files.items():
            if info["bytes"] == 0:
                warnings.append("%s 为 0 字节（落盘缺口）" % fn)
        if files.get("dragon_tiger.csv", {}).get("bytes", 0) < 100:
            warnings.append("dragon_tiger.csv 仅表头（今日无龙虎榜落盘）")
        if not files.get("news.jsonl", {}).get("bytes"):
            warnings.append("news.jsonl 缺失（消息面断供）")
    frames_today = len(_scan_files(t)) if t in idt else 0
    snap = _KL["snap"]
    return {
        "ok": True,
        "today": t,
        "latest_quote": d,
        "quote_today": t in qd,
        "intraday_today": t in idt,
        "intraday_frames": frames_today,
        "intraday_last": (lambda fl: ("%s:%s" % (fl[-1][5:7], fl[-1][7:9])) if fl else None)(_scan_files(t)),
        "is_trading_hours": is_trading_hours(),
        "dates": qd[-40:],
        "n_dates": len(qd),
        "files": files,
        "warnings": warnings,
        "kline": {"ready": snap is not None, "rows": snap["n_rows"] if snap else 0,
                  "build_sec": snap["build_sec"] if snap else None, "err": _KL["err"],
                  "as_of": _hhmm(_mtime(KLINES))},
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def overview_payload(date=None):
    ds = quote_dates()
    if not date or date not in ds:
        date = ds[-1] if ds else None
    if not date:
        return {"ok": False, "error": "无任何 daily_quote.csv 落盘"}
    rows, _ = load_quote(date)
    lu = load_limit_up(date)
    envj = _read_json(os.path.join(RAW, date, "env_metrics.json")) or {}
    sec = load_sectors(date)
    warn = list(meta_payload()["warnings"])
    if date != today_str():
        warn.insert(0, "此页展示的是 %s 收盘数据，不是今天" % date)
    return {
        "ok": True,
        "date": date,
        "is_today_quote": date == today_str(),
        "breadth": breadth(rows),
        "ladder": ladder(lu),
        "ladder_hist": ladder_history(date, 4),
        "env_file": envj,
        "env": env_block(),
        "headline": brain_headline(),
        "sectors_hot": sec[:12],
        "sectors_cold": sec[-6:][::-1],
        "n_sectors": len(sec),
        "warns": warn,
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

INDEX = os.path.join(_HERE, "index.html")


class Handler(BaseHTTPRequestHandler):
    server_version = "TradingDash/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, body, ctype="application/json; charset=utf-8", code=200):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path

        def q(k, default=None):
            v = qs.get(k)
            return v[0] if v else default

        try:
            if path in ("/", "/index.html"):
                html = _read_text(INDEX)
                if not html:
                    return self._send({"ok": False, "error": "index.html 缺失"}, code=500)
                return self._send(html, "text/html; charset=utf-8")
            if path == "/api/meta":
                return self._send(meta_payload())
            if path == "/api/overview":
                return self._send(overview_payload(q("date")))
            if path == "/api/kline":
                code = (q("code") or "").strip()
                if not code:
                    return self._send({"ok": False, "error": "缺 code"}, code=400)
                return self._send(kline_payload(code, _i(q("n"), 160)))
            if path == "/api/quotes":
                codes = [c.strip() for c in (q("codes") or "").split(",") if c.strip()]
                return self._send(quotes_payload(codes[:60]))
            if path == "/api/positions":
                return self._send(positions(q("date") or latest_quote_date()))
            if path == "/api/thermo":
                return self._send(thermo(q("date") or latest_quote_date()))
            if path == "/api/search":
                return self._send(search_stocks(q("q")))
            if path == "/api/intraday":
                return self._send(intraday_latest(q("date")))
            if path == "/api/limitup":
                return self._send(limitup_payload(q("date")))
            return self._send({"ok": False, "error": "未知路径 %s" % path}, code=404)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            return self._send({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, code=500)


def main(argv=None):
    ap = argparse.ArgumentParser(description="交易系统可视化面板（只读）")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", action="store_true", help="起服务后自动打开浏览器")
    ap.add_argument("--warm", action="store_true", help="启动即预热 K 线缓存")
    a = ap.parse_args(argv)

    print("工作区根：%s" % ROOT)
    print("最新收盘：%s ｜ 今天：%s ｜ 今日收盘已落盘：%s"
          % (latest_quote_date() or "无", today_str(), today_str() in quote_dates()))
    if a.warm:
        t0 = time.time()
        ok = ensure_kline(block=True)
        print("K线缓存预热：%s（%s 行 / %.1fs）"
              % (ok, _KL["snap"]["n_rows"] if ok else 0, time.time() - t0))
    else:
        _kick_build()   # 后台建库，建库期间接口返回 pending 而不是卡住

    # 端口被占（常见于上一个面板实例没关）自动顺延，而不是抛异常让黑窗一闪而过
    srv, port = None, a.port
    for i in range(12):
        try:
            srv = ThreadingHTTPServer((a.host, a.port + i), Handler)
            port = a.port + i
            break
        except OSError:
            continue
    if srv is None:
        print("端口 %d~%d 全被占用，请关掉旧面板再试" % (a.port, a.port + 11))
        return 1
    srv.daemon_threads = True
    url = "http://%s:%d/" % (a.host, port)
    if port != a.port:
        print("端口 %d 被占用，已改用 %d" % (a.port, port))
    print("面板地址：%s   （Ctrl+C 停止）" % url)
    if a.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
