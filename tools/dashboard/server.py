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
   - 环境档            ← `memory/state/brain_state.md`（解析"环境档"表）
   - 持仓              ← `memory/state/user_positions.md`（唯一真源，解析失败则显原文）
   - 族温度计          ← 复用 `tools/analysis/zibei_thermo.py`
3. **不许骗人**：解析失败 / 数据缺口一律在 UI 上显式标出，不静默补 0。

用法
----
  python tools/dashboard/server.py                    # 默认 http://127.0.0.1:8770
  python tools/dashboard/server.py --port 8771
  python tools/dashboard/server.py --open             # 起服务并打开浏览器

接口
----
  GET /                     面板页面
  GET /api/meta             可用日期、各文件 as_of、数据缺口
  GET /api/overview?date=   全市场宽度 / 环境档 / 连板梯度 / 板块资金流
  GET /api/kline?code=&n=   个股 OHLC + MA5/10/20/60
  GET /api/positions        用户持仓（成本/现价/浮盈/纪律线距离）
  GET /api/thermo?date=     族温度计（双字族 + 单字族；族内票数/涨停/封板率/最高板/日变化）
  GET /api/search?q=        代码/名称模糊查（自选加票用）
  GET /api/intraday?date=   最近一份盘中扫描快照（宽度/封板数/连板梯度）
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
SNAP = os.path.join(ROOT, "data", "snapshot")
STATE = os.path.join(ROOT, "memory", "state")

KLINES = os.path.join(BUS, "klines.csv")


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


_DATES_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def trading_dates():
    """所有存在 daily_quote.csv 的日期（升序）。"""
    out = []
    if not os.path.isdir(RAW):
        return out
    for d in os.listdir(RAW):
        if _DATES_RE.match(d) and os.path.exists(os.path.join(RAW, d, "daily_quote.csv")):
            out.append(d)
    return sorted(out)


def latest_date():
    ds = trading_dates()
    return ds[-1] if ds else None


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
                "boards": int(_f(r.get("boards"), 1) or 1),
                "seal": _f(r.get("seal_amount")),
                "first_seal": r.get("first_seal_time") or "",
                "reason": r.get("reason") or "",
            })
    return out


def load_sectors(date, limit=14):
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

def breadth(rows):
    pcts = [r["pct"] for r in rows]
    up = sum(1 for x in pcts if x > 0)
    dn = sum(1 for x in pcts if x < 0)
    flat = len(pcts) - up - dn
    buckets = collections.OrderedDict([
        ("≤-9", 0), ("-9~-7", 0), ("-7~-5", 0), ("-5~-3", 0), ("-3~-1", 0),
        ("-1~0", 0), ("0~1", 0), ("1~3", 0), ("3~5", 0), ("5~7", 0),
        ("7~9", 0), ("≥9", 0),
    ])

    def bucket(x):
        if x <= -9:
            return "≤-9"
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

    for x in pcts:
        buckets[bucket(x)] += 1

    return {
        "n": len(pcts),
        "up": up,
        "down": dn,
        "flat": flat,
        "ratio": (up / dn) if dn else None,
        "median": statistics.median(pcts) if pcts else None,
        "mean": (sum(pcts) / len(pcts)) if pcts else None,
        "dist": [{"k": k, "v": v} for k, v in buckets.items()],
        "le3": sum(1 for x in pcts if x <= -3),
        "ge5": sum(1 for x in pcts if x >= 5),
    }


def ladder(limit_rows):
    c = collections.Counter(r["boards"] for r in limit_rows)
    total = sum(c.values())
    n1 = c.get(1, 0)
    multi = total - n1
    return {
        "total": total,
        "first": n1,
        "first_share": (100.0 * n1 / total) if total else None,
        "multi": multi,
        "max_board": max(c) if c else 0,
        "grad": [{"boards": b, "n": c[b]} for b in sorted(c.keys(), reverse=True)],
        "top": sorted(limit_rows, key=lambda r: (-r["boards"], -(r["seal"] or 0)))[:20],
    }


def ladder_history(date, n=3):
    """近 n 个交易日的连板结构，用于看「首板占比」是在升还是在降。"""
    ds = trading_dates()
    if date in ds:
        ds = ds[:ds.index(date) + 1]
    out = []
    for d in ds[-n:]:
        lu = load_limit_up(d)
        if not lu:
            continue
        lad = ladder(lu)
        out.append({"date": d, "total": lad["total"], "first_share": lad["first_share"],
                    "multi": lad["multi"], "max_board": lad["max_board"]})
    return out


# --------------------------------------------------------------------------
# K 线（全表一次扫，常驻内存，只留最近 N 根）
# --------------------------------------------------------------------------

_KL = {
    "ready": False,
    "lock": threading.Lock(),
    "key": None,
    "bars": {},     # code -> dict(dates=array('i'), o/h/l/c=array('f'), v=array('f'))
    "name": {},
    "err": None,
    "build_sec": None,
    "n_rows": 0,
}
KEEP_BARS = 260


def _build_kline(days=KEEP_BARS):
    t0 = time.time()
    bars, names = {}, {}
    n = 0
    # 单遍扫描：只保留每只票最近 days 根，用 array 压内存
    tmp = {}  # code -> list[(dateint, o,h,l,c,v)] 为控制内存，先按 260 截断
    with open(KLINES, encoding="utf-8-sig") as fh:
        r = csv.reader(fh)
        next(r, None)
        for row in r:
            n += 1
            if len(row) < 9:
                continue
            code = row[0]
            d = row[2]
            o, c, h, l = _f(row[3]), _f(row[4]), _f(row[5]), _f(row[6])
            v = _f(row[7], 0.0) or 0.0
            # ⚠ volume 单位跨时段不一致（实测：2024 年比对 0.1、2025-09~2026-09 为手 ≈100、
            #   2026-10 起为股 ≈1）。amount 全程都是元 → 量能一律用 amount，volume 只留备查。
            amt = _f(row[8], 0.0) or 0.0
            if o is None or c is None or h is None or l is None:
                continue
            names[code] = row[1]
            di = int(d.replace("-", ""))
            lst = tmp.get(code)
            if lst is None:
                lst = tmp[code] = []
            lst.append((di, o, h, l, c, v, amt))
    for code, lst in tmp.items():
        if len(lst) > days:
            lst = lst[-days:]
        di = array("i", [x[0] for x in lst])
        o = array("f", [x[1] for x in lst])
        h = array("f", [x[2] for x in lst])
        l = array("f", [x[3] for x in lst])
        c = array("f", [x[4] for x in lst])
        v = array("f", [x[5] for x in lst])
        a = array("d", [x[6] for x in lst])   # 成交额额值较大，用 double 防精度损失
        bars[code] = {"d": di, "o": o, "h": h, "l": l, "c": c, "v": v, "a": a}
    return bars, names, n, time.time() - t0


def ensure_kline():
    key = _mtime(KLINES)
    with _KL["lock"]:
        if _KL["ready"] and _KL["key"] == key:
            return True
        try:
            bars, names, n, sec = _build_kline()
        except Exception as e:  # noqa: BLE001
            _KL["err"] = "%s: %s" % (type(e).__name__, e)
            return False
        _KL.update(bars=bars, name=names, n_rows=n, build_sec=round(sec, 1),
                   ready=True, key=key, err=None)
        return True


def _ma(vals, i, n):
    if i + 1 < n:
        return None
    seg = vals[i + 1 - n:i + 1]
    return sum(seg) / n


def kline_payload(code, n=160):
    if not ensure_kline():
        return {"ok": False, "error": _KL["err"] or "K线库未就绪"}
    b = _KL["bars"].get(code)
    if not b:
        return {"ok": False, "error": "K线库中无此代码：%s" % code}
    total = len(b["d"])
    start = max(0, total - n)
    idx = range(start, total)
    closes = b["c"]
    vols = b["v"]
    amts = b["a"]
    bars = []
    for i in idx:
        bars.append({
            "d": "%04d-%02d-%02d" % (b["d"][i] // 10000, b["d"][i] // 100 % 100, b["d"][i] % 100),
            "o": round(b["o"][i], 3),
            "h": round(b["h"][i], 3),
            "l": round(b["l"][i], 3),
            "c": round(closes[i], 3),
            "a": round(amts[i], 0),      # 成交额（元）——量能图一律用它，不用 volume
            "v": round(vols[i], 0),      # 原始 volume 字段：单位跨时段不一致，仅备查
        })
    mas = {}
    for w in (5, 10, 20, 60):
        mas["ma%d" % w] = [
            (round(_ma(closes, i, w), 3) if _ma(closes, i, w) is not None else None)
            for i in idx
        ]
    # 区间统计
    seg_close = [closes[i] for i in idx]
    high_seg = max(b["h"][i] for i in idx)
    low_seg = min(b["l"][i] for i in idx)
    last = seg_close[-1]
    # 额比 = 最新一日成交额 ÷ 前 5 日均额（不用 volume，避免单位陷阱）
    prev5 = [amts[i] for i in idx][-6:-1]
    amt_ratio = None
    if len(prev5) == 5:
        avg = sum(prev5) / 5.0
        if avg > 0:
            amt_ratio = round(amts[idx[-1]] / avg, 2)
    return {
        "ok": True,
        "code": code,
        "name": _KL["name"].get(code, code),
        "bars": bars,
        "ma": mas,
        "stat": {
            "last": round(last, 3),
            "chg_n": round(100.0 * (last / seg_close[0] - 1.0), 2) if seg_close[0] else None,
            "high": round(high_seg, 3),
            "low": round(low_seg, 3),
            "ma20": mas["ma20"][-1],
            "ma60": mas["ma60"][-1],
            "amt": round(amts[idx[-1]], 0),
            "amt_ratio": amt_ratio,
        },
    }


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
    _, by_code = load_quote(date)
    items, warns = [], []
    if not txt:
        return {"ok": False, "error": "读不到 memory/state/user_positions.md", "items": []}
    for line in txt.split("\n"):
        if not line.startswith("|"):
            continue
        if "---" in line or "代码" in line.split("|")[1]:
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
        #   实得从文本里抓「实得 +X.XX%」，抓不到就留空并告警。
        pnl = None
        pnl_kind = "float"
        sell_px = None
        if closed:
            mr = _REAL_RE.search(joined)
            ms = _SELL_RE.search(joined)
            if ms:
                sell_px = _f(ms.group(1))
            if mr:
                pnl = _f(mr.group(1))
                pnl_kind = "realized"
            else:
                warns.append("%s 已了结但未解析到「实得」收益率，不显示盈亏" % raw_name)
        elif cur and cost:
            pnl = 100.0 * (cur / cost - 1.0)
        dist = None
        if cur and stop:
            dist = 100.0 * (cur / stop - 1.0)
        if not closed and cost is None:
            warns.append("%s 成本解析失败，浮盈不可算" % raw_name)
        if not closed and q is None:
            warns.append("%s 当日无行情（停牌或数据缺口）" % raw_name)
        items.append({
            "code": code,
            "name": raw_name,
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
            "raw_note": re.sub(r"\s+", " ", cells[3])[:220] if len(cells) > 3 else "",
            "raw_rule": re.sub(r"\s+", " ", cells[4])[:220] if len(cells) > 4 else "",
        })
    return {"ok": True, "as_of": date, "items": items, "warns": warns,
            "src": "memory/state/user_positions.md"}


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
    """双字族（可信）+ 单字族（慎用，但「洋/阳」这类真实抱团就藏在里面）。

    ★ 为什么两种都出：11-09 之前我按 minlen=2 只出双字族，结果把用户真正观察到的
      「洋字辈」整族过滤掉了 —— 单字族有行业撞词风险，但不等于没有信息。
      故：双字族列前、单字族单列并标「慎用」，两个都摆出来由人判。
    """
    key = (date, topk, singlek, "v2")
    if key in _THERMO_CACHE:
        return _THERMO_CACHE[key]
    try:
        zt = _import_thermo()
        ds = [d for d in zt.dates() if d < date]
        prev = ds[-1] if ds else None
        fam, M, L = _thermo_rows(date, topk, 2, prev)
        singles, _, _ = _thermo_rows(date, singlek, 1, prev)
        # 单字表里剔掉与双字表重复的（不会重复，但保险）
        dup = {c["gram"] for c in fam}
        singles = [c for c in singles if c["gram"] not in dup][:singlek]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                "families": [], "singles": []}
    payload = {"ok": True, "date": date, "n_market": M, "n_limit": L,
               "prev_date": prev, "families": fam, "singles": singles,
               "note": "族封板率在聚 = 当日状态描述；前瞻性检验 t=-0.15、日胜率 45%，不是买点信号（trade-rules §一g.10）"}
    _THERMO_CACHE[key] = payload
    return payload


# --------------------------------------------------------------------------
# 盘中快照（data/raw/intraday/<date>/scan_HHMM.csv）
# --------------------------------------------------------------------------

def intraday_latest(date=None):
    """取最近一份盘中扫描快照，算宽度 + 封板 + 连板梯度。

    ★ 为什么要有这块：收盘数据只能复盘，盘中要决策。原始扫描是「截面」，
      不是逐笔，所以这里只给「此刻的宽度与封板结构」，不给分时曲线
      （整个 raw 里没有逐分钟行情，不许凭空画分时）。
    """
    base = os.path.join(RAW, "intraday")
    if not os.path.isdir(base):
        return {"ok": False, "error": "无 data/raw/intraday 目录"}
    days = sorted(d for d in os.listdir(base)
                  if _DATES_RE.match(d) and os.path.isdir(os.path.join(base, d)))
    if not days:
        return {"ok": False, "error": "无盘中扫描记录"}
    d = date if date in days else days[-1]
    folder = os.path.join(base, d)
    files = sorted(f for f in os.listdir(folder)
                   if f.startswith("scan_") and f.endswith(".csv"))
    if not files:
        return {"ok": False, "error": "%s 无 scan_*.csv" % d}
    fn = files[-1]
    hhmm = fn[len("scan_"):-4]
    rows = []
    with open(os.path.join(folder, fn), encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            pct = _f(r.get("pct"))
            if pct is None:
                continue
            price, ceil = _f(r.get("price")), _f(r.get("ceiling"))
            sealed = bool(price is not None and ceil is not None and price >= ceil - 0.005)
            rows.append({
                "code": r.get("code", ""),
                "name": r.get("name", ""),
                "pct": pct,
                "price": price,
                "amount": _f(r.get("amount")) or 0.0,
                "boards": int(_f(r.get("boards"), 0) or 0),
                "sealed": sealed,
                "ceiling": ceil,
                "turnover_rate": _f(r.get("turnover_rate")),
                "volume_ratio": _f(r.get("volume_ratio")),
            })
    if not rows:
        return {"ok": False, "error": "%s 为空" % fn}
    non_st = [r for r in rows if "ST" not in r["name"]]
    b = breadth(non_st)
    sealed_rows = [r for r in non_st if r["sealed"]]
    for r in sealed_rows:
        if r["boards"] <= 0:
            r["boards"] = 1
    lad = ladder([{"boards": r["boards"], "seal": r["amount"],
                   "code": r["code"], "name": r["name"], "reason": ""} for r in sealed_rows])
    return {
        "ok": True,
        "date": d,
        "as_of": "%s %s:%s" % (d, hhmm[:2], hhmm[2:]),
        "hhmm": hhmm,
        "file": "data/raw/intraday/%s/%s" % (d, fn),
        "n": len(non_st),
        "breadth": b,
        "sealed": len(sealed_rows),
        "ladder": lad,
        "is_today": d == latest_date(),
    }


def search_stocks(q, limit=20):
    """代码或名称模糊查（只用内存里的 K 线索引，不读盘）。"""
    q = (q or "").strip()
    if not q:
        return {"ok": True, "items": []}
    if not ensure_kline():
        return {"ok": False, "error": _KL["err"] or "K线库未就绪", "items": []}
    ql = q.lower()
    exact, starts, contains = [], [], []
    for code, name in _KL["name"].items():
        if code == ql:
            exact.append((code, name))
        elif code.startswith(ql) or name.startswith(q):
            starts.append((code, name))
        elif ql in code or q in name:
            contains.append((code, name))
        if len(exact) + len(starts) + len(contains) > limit * 6:
            break
    out = (exact + starts + contains)[:limit]
    return {"ok": True, "items": [{"code": c, "name": n} for c, n in out]}


# --------------------------------------------------------------------------
# 汇总接口
# --------------------------------------------------------------------------

def meta_payload():
    ds = trading_dates()
    d = ds[-1] if ds else None
    files = {}
    if d:
        for fn in ("daily_quote.csv", "limit_up.csv", "env_metrics.json",
                   "sector_ranking.csv", "dragon_tiger.csv", "news.jsonl",
                   "regulatory.jsonl", "holder_behavior.jsonl"):
            p = os.path.join(RAW, d, fn)
            files[fn] = {"bytes": os.path.getsize(p) if os.path.exists(p) else 0,
                         "as_of": time.strftime("%m-%d %H:%M", time.localtime(_mtime(p)))
                         if os.path.exists(p) else None}
    warnings = []
    if d:
        for fn, info in files.items():
            if info["bytes"] == 0:
                warnings.append("%s 为 0 字节（落盘缺口）" % fn)
        if files.get("dragon_tiger.csv", {}).get("bytes", 0) < 100:
            warnings.append("dragon_tiger.csv 仅表头（今日无龙虎榜落盘）")
    return {
        "ok": True,
        "latest": d,
        "dates": ds[-30:],
        "n_dates": len(ds),
        "files": files,
        "warnings": warnings,
        "kline": {"ready": _KL["ready"], "rows": _KL["n_rows"],
                  "build_sec": _KL["build_sec"], "err": _KL["err"],
                  "as_of": time.strftime("%m-%d %H:%M", time.localtime(_mtime(KLINES)))},
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def overview_payload(date=None):
    ds = trading_dates()
    if not date or date not in ds:
        date = ds[-1] if ds else None
    if not date:
        return {"ok": False, "error": "无任何 daily_quote.csv 落盘"}
    rows, _ = load_quote(date)
    lu = load_limit_up(date)
    envj = _read_json(os.path.join(RAW, date, "env_metrics.json")) or {}
    sec = load_sectors(date)
    return {
        "ok": True,
        "date": date,
        "breadth": breadth(rows),
        "ladder": ladder(lu),
        "ladder_hist": ladder_history(date, 3),
        "env_file": envj,
        "env": env_block(),
        "headline": brain_headline(),
        "sectors_hot": sec[:10],
        "sectors_cold": sec[-8:][::-1],
        "warns": [w for w in meta_payload()["warnings"]],
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

INDEX = os.path.join(_HERE, "index.html")


class Handler(BaseHTTPRequestHandler):
    server_version = "TradingDash/1.0"

    def log_message(self, fmt, *args):  # 不写日志文件，只在终端精简输出
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
                n = int(_f(q("n"), 160) or 160)
                if not code:
                    return self._send({"ok": False, "error": "缺 code"}, code=400)
                return self._send(kline_payload(code, max(30, min(n, 260))))
            if path == "/api/positions":
                d = q("date") or latest_date()
                return self._send(positions(d))
            if path == "/api/thermo":
                d = q("date") or latest_date()
                return self._send(thermo(d))
            if path == "/api/search":
                return self._send(search_stocks(q("q")))
            if path == "/api/intraday":
                return self._send(intraday_latest(q("date")))
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
    ap.add_argument("--warm", action="store_true", help="启动即预热 K 线缓存（约 3~5 秒）")
    a = ap.parse_args(argv)

    print("工作区根：%s" % ROOT)
    print("最新交易日：%s" % (latest_date() or "无"))
    if a.warm:
        t0 = time.time()
        ok = ensure_kline()
        print("K线缓存预热：%s（%s 行 / %.1fs）" % (ok, _KL["n_rows"], time.time() - t0))
    else:
        threading.Thread(target=ensure_kline, daemon=True).start()

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = "http://%s:%d/" % (a.host, a.port)
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
