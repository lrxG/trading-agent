#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""intraday_scan.py · 盘中实时扫描（E2 数据腿 · 2026-10-26 建 · v1.0.0）

## 为什么有本件

`e2_intraday.py` 判 **DEGRADED**，理由是 `SOURCE_CHECKS` 里 `intraday_feed`
（盘中行情腿）ready=false。2026-10-26 盘中实证推翻了「缺源」这个前提：

| 通道 | 实测 | 结论 |
|---|---|---|
| `westock quote <codes>` | 4942 只 / 3 批 / **11.4 秒** 全返回，38 列，`time=2026-10-26` | ✅ 实时行情腿**存在** |
| `westock sector ranking` | 124 个行业：涨跌幅 / 主力净流入 1D·5D·20D / 涨家数 / 龙头股 | ✅ 板块腿**存在** |
| `westock changedist --date D` | **`--date` 被忽略**：10-26 与 10-23 返回同一份实时数（成交额两次调用差 2.66 亿＝正在走） | ⚠️ 只能取当日、不能取历史（本件只用于当日交叉校验） |

**旧版 `skills/intraday/` 要跑 neodata 4 路 + 东财 zt_pool + near_board_scan +
ranking 三路 + oversold_scan 五个脚本；本件 4 个调用 ~15 秒出同一批事实。**

## 比旧版强在哪（不是照搬）

| 旧版 | 本件 | 差别 |
|---|---|---|
| 炸板池「不存在」（快照无炸板列，`scan.py` 明写） | **实时三态分桶**：`price≥ceiling`=封板 / `high≥ceiling 且 price<ceiling`=炸板 / 未触板 | quote 直给 `high` + `price_ceiling`，同一截面可判，无需等 14:57 定格 |
| `near_board_scan.py --min 4.5` 单独脚本 | 同一次 quote 里直接分桶（**0 额外取数**） | 半路板层与涨停池同一时刻截面，不错位 |
| 环境 4 档「**不设硬数字阈值**」（`market-environment.md` 原文） | **8 维打分 −16..+16 → 硬阈值定档** | 旧版不可复现、不可回测；本件同输入必然同输出（自检 B3） |
| 单指标一票否决（`regime.py` failback：炸板率≥40% ⇒ 直接退潮 cap 1 成） | 8 维加总，**任一维极端不单独否决** | 10-23 涨停 73 / 涨跌比 4.80 / 连板 6 的普涨日被炸板率 41.1% 一票打成「退潮」，是缺陷不是稳健 |
| 封单质量靠 zt_pool 封单额（盘后定格） | `wb_ratio`（委比）+ `high/ceiling` 关系 → 封稳/回封（疑似）/炸板 | 盘中可得，且能区分「一直封」与「封了又弱」 |
| 量比 Top5 只取 API 返回的前 N | 全市场 `volume_ratio` 排序（4942 只全量） | 无 top-N 截断 |
| 超跌反弹要跑 `oversold_scan.py` | `chg_20d ≤ -35%` 字段直给 | 0 额外取数 |

## 口径纪律（红线）

1. **零 LLM、零判断越界**：本件只产**事实层**（分桶/计数/榜单）与**环境档**（机械打分）。
   **不出 BUY/WAIT/NO**——买卖结论归 C4 三问，归 C5 定价（`AGENT.md §3`、E2 boundary）。
2. **涨停用 `price_ceiling` 判，不自算**：自算要处理 ST 5% / 创业板科创 20% / 北交所 30%，
   quote 已给足字段，自算＝引入第二套口径。`ceiling ≤ 0`（指数/ETF/无涨跌停）**不入池**。
3. **`changedist` 只做当日交叉校验**，绝不作为历史源（`--date` 实测无效）。
4. **连板数递推**：`boards = 前一日 limit_up.boards + 1`，两日皆封才递推；
   前日无记录（新股/前日未封）＝ 记 1 板并单列 `boards_src`，**不猜**。
5. **封板质量只到「疑似」**：区分「一直封」与「炸后回封」需分钟腿；
   `wb_ratio < 90` 记 `回封(疑似)`，不写死。
6. **指数单列**：`price_ceiling ≤ 0` 的行不参与涨跌停判定，只取涨跌幅/成交额。
7. **落点**：快照 `data/raw/intraday/<date>/`（X5 运行数据，非知识）；
   报告 `intraday_scan_<date>_<hhmm>.md` 落 `data/generated/reports/`（X1；**禁 e2_ 前缀**，防污染 E2 产物命名空间）。**不写任何落点 1~5**。

## 用法

```
python tools/e2e/intraday_scan.py                       # 干跑：只打印统计，不写盘
python tools/e2e/intraday_scan.py --execute             # 写快照 + 报告（当日）
python tools/e2e/intraday_scan.py --execute --limit 800 # 小样本联调
python tools/e2e/intraday_scan.py --selftest            # 内置验收（临时目录）
```
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import sys
import time

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
for _p in (os.path.join(_TOOLS, "fs"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import codes as CD  # noqa: E402
import cli_codes as CC  # noqa: E402  ★ CLI 代码格式单真源（2026-11-02）

RAW = os.path.join(_ROOT, "data", "raw")
INTRADAY = os.path.join(RAW, "intraday")
REPORTS = os.path.join(_ROOT, "data", "generated", "reports")
LOCK = os.path.join(_ROOT, "data", "state", "_intraday_scan.lock")

BATCH = 1500          # Windows 命令行 32767 字符上限；1500×9=13500 安全
NEAR_PCT = 4.5        # 半路板门槛（旧版 near_board_scan 同值）
OVERSOLD_20D = -35.0  # 超跌门槛（旧包 oversold_scan 深档口径）
BREADTH_REB = 65.0     # 反弹日闸（旧包 oversold_scan BREADTH_REB 原值）：
                       # 上涨占比 < 65% 时超跌腿**不开**——旧包 659 天实测：
                       # 单超跌 T+5 +0.23pp(t=1.58) 零超额、单反弹日零超额；
                       # 只有「深超跌×反弹日×当日涨>3%」才有 +1.63pp(t=4.11)。

IDX = ("sh000001", "sz399001", "sz399006")

# quote → 内部字段（只取用得上的；不搬 38 列全量）
FIELDS = {
    "code": "code", "name": "name", "price": "price",
    "prev_close": "prev_close", "open": "open", "high": "high", "low": "low",
    "volume": "volume", "amount": "amount", "change_percent": "pct",
    "turnover_rate": "turnover_rate", "volume_ratio": "volume_ratio",
    "avg_price": "avg_price", "wb_ratio": "wb_ratio",
    "price_ceiling": "ceiling", "price_floor": "floor",
    "chg_5d": "chg_5d", "chg_10d": "chg_10d", "chg_20d": "chg_20d",
    "chg_60d": "chg_60d",
    "float_shares": "float_shares", "total_shares": "total_shares",
    "circulating_market_cap": "circ_mv", "total_market_cap": "total_mv",
    "inner_volume": "inner_vol", "outer_volume": "outer_vol",
    "high_52week": "high_52w", "low_52week": "low_52w",
    "pre_quote_change_pct": "auction_pct", "pre_quote_price": "auction_price",
    "time": "data_date",
}


# ── 基础 IO ────────────────────────────────────────────────────────────────
def _run(args, timeout=180):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        if p.returncode != 0:
            return None, (p.stderr or p.stdout or "")[:200]
        return p.stdout, None
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"
    except Exception as e:                                    # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


def _parse_md(txt):
    """markdown 表格 → list[dict]（与 fetch_auction 同款，禁第二套解析）。"""
    if not txt:
        return []
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip().startswith("|")]

    def cells(ln):
        return [c.strip() for c in ln.strip().strip("|").split("|")]

    if not lines:
        return []
    hdr = cells(lines[0])
    out = []
    for ln in lines[1:]:
        cs = cells(ln)
        if all(set(c) <= set("-: ") for c in cs):
            continue
        if len(cs) != len(hdr):
            continue
        out.append(dict(zip(hdr, cs)))
    return out


def _f(v):
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in ("-", "--", "None", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ── 取数 ──────────────────────────────────────────────────────────────────
def prev_trade_date(date, base=None):
    """取 `< date` 的最近一个有基线产物的交易日（有 env_metrics 或 limit_up 的才认）。"""
    root = os.path.join(base or _ROOT, "data", "raw")
    if not os.path.isdir(root):
        return None
    cands = []
    for fn in os.listdir(root):
        if len(fn) == 10 and fn[4] == "-" and fn < date:
            d = os.path.join(root, fn)
            if os.path.isfile(os.path.join(d, "limit_up.csv")) or \
               os.path.isfile(os.path.join(d, "env_metrics.json")):
                cands.append(fn)
    return max(cands) if cands else None


def universe(date, base=None, limit=None):
    """宇宙：`<= date` 的最近一份日截面里的个股（codes.is_stock 唯一真源）。"""
    root = os.path.join(base or _ROOT, "data", "raw")
    d = date
    if not os.path.isfile(os.path.join(root, d, "daily_quote.csv")):
        p = prev_trade_date(date, base=base)
        if not p:
            return []
        d = p
    path = os.path.join(root, d, "daily_quote.csv")
    seen = []
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            c = (r.get("code") or "").strip()
            if c and CD.is_stock(c) and c not in seen:
                seen.append(c)
    return seen[:limit] if limit else seen


def fetch_quotes(codes, on_progress=None):
    """→ (rows, meta)。分批 quote；分批失败不拖垮全局，如实记。"""
    rows, batches, failed = [], [], []
    t0 = time.time()
    for i in range(0, len(codes), BATCH):
        chunk = codes[i:i + BATCH]
        ts = time.time()
        txt, err = _run(["westock", "quote", CC.to_cli_csv(chunk)])
        if txt is None:
            failed.append({"n": len(chunk), "err": err, "at": i})
            batches.append({"req": len(chunk), "got": 0, "sec": round(time.time() - ts, 1)})
            continue
        recs = _parse_md(txt)
        got = 0
        for r in recs:
            c = (r.get("code") or "").strip()
            if not c or not CD.is_stock(c):
                continue
            row = {}
            for src, dst in FIELDS.items():
                v = r.get(src)
                row[dst] = (v or "").strip() if dst in ("code", "name", "data_date") else _f(v)
            if row.get("price") is None or row.get("data_date") is None:
                continue
            rows.append(row)
            got += 1
        batches.append({"req": len(chunk), "got": got, "sec": round(time.time() - ts, 1)})
        if on_progress:
            on_progress(i + len(chunk), len(codes), len(rows))
    meta = {"req_codes": len(codes), "got_rows": len(rows),
            "sec": round(time.time() - t0, 1), "batches": batches,
            "failed_batches": failed,
            "coverage": round(len(rows) / len(codes), 4) if codes else 0.0}
    return rows, meta


def fetch_index():
    txt, err = _run(["westock", "quote", CC.to_cli_csv(IDX)])
    out = {}
    if txt is None:
        return {"status": "UNAVAILABLE", "error": err}
    for r in _parse_md(txt):
        c = (r.get("code") or "").strip()
        if c in IDX:
            out[c] = {"name": (r.get("name") or "").strip(),
                      "price": _f(r.get("price")),
                      "pct": _f(r.get("change_percent")),
                      "amount": _f(r.get("amount"))}
    return {"status": "OK" if out else "EMPTY", "index": out}


def fetch_sectors():
    txt, err = _run(["westock", "sector", "ranking"])
    if txt is None:
        return {"status": "UNAVAILABLE", "error": err, "rows": []}
    rows = []
    for r in _parse_md(txt):
        rows.append({
            "code": (r.get("code") or "").strip(),
            "name": (r.get("name") or "").strip(),
            "pct": _f(r.get("changePct")),
            "net_in_1d": _f(r.get("mainNetInflow")),
            "net_in_5d": _f(r.get("mainNetInflow5d")),
            "net_in_20d": _f(r.get("mainNetInflow20d")),
            "up_count": (r.get("upCount") or "").strip(),
            "turnover": _f(r.get("turnover")),
            "turnover_rate": _f(r.get("turnoverRate")),
            "leader": (r.get("leader") or "").strip(),
        })
    rows = [x for x in rows if x["pct"] is not None]
    rows.sort(key=lambda x: -x["pct"])
    return {"status": "OK" if rows else "EMPTY", "rows": rows, "n": len(rows)}


def fetch_changedist():
    """当日涨跌分布（**仅当日**：--date 实测被忽略，禁用于历史）。"""
    txt, err = _run(["westock", "changedist"])
    if txt is None:
        return {"status": "UNAVAILABLE", "error": err}
    d = {"status": "OK"}
    for r in _parse_md(txt):
        k = list(r.keys())
        if "上涨" in k and "下跌" in k:
            d.update({"up": _f(r.get("上涨")), "down": _f(r.get("下跌")),
                      "flat": _f(r.get("平盘")), "halt": _f(r.get("停牌")),
                      "limit_up": _f(r.get("涨停")), "limit_down": _f(r.get("跌停")),
                      "up_ratio_pct": (r.get("上涨占比") or "").strip()})
    # ★ 同期量能对比：原文形如「两市成交额：7985.78亿（较上日 -245.14亿）」
    #   这是**同一时刻 vs 上日同一时刻**，比「折全日」可靠得多（早盘成交额非线性）。
    m = re.search(r"成交额[：:]\s*([\d.]+)\s*亿\s*[（(]\s*较上日\s*([+-]?[\d.]+)\s*亿", txt)
    if m:
        today_amt, delta = float(m.group(1)), float(m.group(2))
        prev_same = today_amt - delta
        d.update({"amount_today_yi": today_amt, "amount_delta_yi": delta,
                  "amount_prev_same_yi": round(prev_same, 2),
                  "amount_ratio_same": round(today_amt / prev_same, 4) if prev_same else None})
    return d


# ── 前日基线（连板递推 + 量能对比）────────────────────────────────────────
def prev_baseline(date, base=None):
    p = prev_trade_date(date, base=base)
    out = {"prev_date": p, "boards": {}, "names": {}, "limit_up_n": None, "env": None}
    if not p:
        return out
    root = os.path.join(base or _ROOT, "data", "raw")
    lp = os.path.join(root, p, "limit_up.csv")
    if os.path.isfile(lp):
        with io.open(lp, encoding="utf-8-sig", newline="") as f:
            rs = list(csv.DictReader(f))
        for r in rs:
            c = (r.get("code") or "").strip()
            if c:
                out["boards"][c] = int(_f(r.get("boards")) or 1)
                out["names"][c] = (r.get("name") or "").strip()
        out["limit_up_n"] = len(rs)
    ep = os.path.join(root, p, "env_metrics.json")
    if os.path.isfile(ep):
        try:
            out["env"] = json.load(io.open(ep, encoding="utf-8-sig"))
        except Exception:                                     # noqa: BLE001
            out["env"] = None
    return out


# ── 分桶（事实层 · 零判断）────────────────────────────────────────────────
def classify(rows, prev):
    """→ buckets。三态判据只依赖 quote 截面字段。

    封板 : price ≥ ceiling（ceiling>0 才有意义）
    炸板 : high ≥ ceiling 且 price < ceiling
    未触板: high < ceiling
    """
    sealed, broken, near, down_limit, plain = [], [], [], [], []
    prev_b = prev.get("boards") or {}
    for r in rows:
        ce, fl = r.get("ceiling"), r.get("floor")
        p, h, pct = r.get("price"), r.get("high"), r.get("pct")
        if p is None or pct is None:
            continue
        code = r["code"]
        if ce and ce > 0 and h is not None:
            if p >= ce - 1e-6:
                b = prev_b.get(code)
                r["boards"] = (b + 1) if b else 1
                r["boards_src"] = "prev+1" if b else "new_or_unsealed"
                wb = r.get("wb_ratio")
                r["chain"] = "封稳" if (wb is not None and wb >= 90) else "回封(疑似)"
                sealed.append(r)
                continue
            if h >= ce - 1e-6:
                r["chain"] = "炸板"
                r["broken_after"] = round((ce - p) / ce * 100, 2)
                broken.append(r)
                continue
            if pct >= NEAR_PCT:
                r["chain"] = "未触板"
                r["gap_to_board"] = round((ce - p) / p * 100, 2)
                near.append(r)
                continue
        if fl and fl > 0 and p <= fl + 1e-6:
            r["chain"] = "跌停"
            down_limit.append(r)
            continue
        plain.append(r)
    return {"sealed": sealed, "broken": broken, "near": near,
            "limit_down": down_limit, "plain": plain}


def boards_hist(sealed):
    from collections import Counter
    c = Counter(int(x.get("boards") or 1) for x in sealed)
    return {str(k): c[k] for k in sorted(c)}


# ── 观察池：持仓 + 候选（E2-HR-4「扫描范围 = 持仓票 + 候选票」）──────────────
def is_tradable(code, name):
    """可交易判定（**只用于「能不能买」，不用于「是不是事实」**）。

    2026-10-26 从旧包 `capabilities/pattern/src/e2v11_scan.py` 继承的教训：
    原只判 `'ST' in name`，退市整理期票名形如「退市创兴」（不含 ST）⇒ 全部漏网，
    实测后果是深跌反弹回测里单笔 **−93.29%**（买入 2.53 → 退市整理一天 −91.24%）。
    同源缺陷在旧包 ms_scan / range_scan 一直有 '退' 判断，唯该文件漏 ⇒ 本函数对齐。
    """
    nm = name or ""
    if "ST" in nm.upper() or "退" in nm:
        return False
    if code.startswith("bj"):
        return False
    return True


def load_candidates(date, base=None):
    """读候选池 OPEN 态（DISCOVERED / WATCH / QUALIFIED 都算在池）。

    → {code: {state, expiry, source, last_seen, trigger_text}}。
    池缺席/坏 JSON ⇒ {}（按无候选降级，禁抛）。
    """
    p = os.path.join(base or _ROOT, "data", "state", "candidates.json")
    if not os.path.isfile(p):
        return {}, "NO_FILE"
    try:
        d = json.load(io.open(p, encoding="utf-8-sig"))
    except Exception as e:                                    # noqa: BLE001
        return {}, "UNREADABLE: %s" % e
    open_states = ("DISCOVERED", "WATCH", "QUALIFIED")
    out = {}
    for c in d.get("candidates") or []:
        if c.get("state") not in open_states:
            continue
        code = (c.get("code") or "").strip()
        if not code:
            continue
        out[code] = {
            "state": c.get("state"), "expiry": c.get("expiry"),
            "source": c.get("source"), "last_seen": c.get("last_seen"),
            "trigger_text": ((c.get("trigger") or {}).get("text") or "")[:80],
            "next_trigger": c.get("next_trigger"),
        }
    return out, "OK"


def load_positions(base=None):
    """读 `memory/state/positions.md` 表格 → {code: {name, cost, qty}}。

    该文件是持仓唯一真相源（0.12），本函数只读表格行，不解析正文说明。
    持仓为 0 ⇒ 返回空 dict（这是「账实相符」，不是「数据缺失」）。
    """
    p = os.path.join(base or _ROOT, "memory", "state", "positions.md")
    if not os.path.isfile(p):
        return {}, "NO_FILE"
    out = {}
    try:
        txt = io.open(p, encoding="utf-8").read()
    except Exception as e:                                    # noqa: BLE001
        return {}, "UNREADABLE: %s" % e
    for ln in txt.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 5:
            continue
        code = cells[0]
        if not re.match(r"^(sh|sz)\d{6}$", code):
            continue
        def _num(v):
            try:
                return float(re.sub(r"[^\d.\-]", "", v))
            except Exception:                                 # noqa: BLE001
                return None
        out[code] = {"name": cells[1], "cost": _num(cells[3]),
                     "qty": _num(cells[4]) if len(cells) > 4 else None}
    return out, "OK"


def watch_pool(rows, cand, pos, date):
    """池内票今日实时表现（**这是盘中扫描的决策核心**）。

    → list[dict]，按「异动强度」降序。异动判定（机械）：
      涨跌幅绝对值 ≥5% / 在封板或炸板池 / 在跌停池 / 半路板(≥4.5%)。
    池外票涨得再好也不进本表 —— 那属「发现」，归 discoveries()。
    """
    idx = {r["code"]: r for r in rows}
    out = []
    for code, m in cand.items():
        r = idx.get(code)
        if r is None:
            continue
        pct = r.get("pct")
        if pct is None:
            continue
        chain = r.get("chain") or ""
        hot = (abs(pct) >= 5.0 or chain in ("封稳", "回封(疑似)", "炸板", "未触板", "跌停"))
        if not hot:
            continue
        exp = m.get("expiry")
        out.append({
            "code": code, "name": r.get("name") or "",
            "price": r.get("price"), "pct": pct, "chain": chain or "普通",
            "volume_ratio": r.get("volume_ratio"), "turnover_rate": r.get("turnover_rate"),
            "amount": r.get("amount"), "boards": r.get("boards"),
            "in_pool": True, "state": m.get("state"), "source": m.get("source"),
            "expiry": exp, "last_seen": m.get("last_seen"),
            "days_left": _days_left(date, exp),
            "tradable": is_tradable(code, r.get("name")),
            "held": code in pos,
        })
    out.sort(key=lambda x: -abs(x["pct"]))
    return out


def _days_left(date, expiry):
    """到期前剩余自然日；解析失败 ⇒ None（禁猜）。"""
    import datetime as _dt
    if not expiry:
        return None
    try:
        d0 = _dt.date(*[int(x) for x in date.split("-")])
        d1 = _dt.date(*[int(x) for x in str(expiry).split("-")])
    except Exception:                                         # noqa: BLE001
        return None
    return (d1 - d0).days


def discoveries(rows, cand, sectors, diff, date):
    """发现项（**对照旧包 `tools/data/intraday_scan.py` 三类发现**，本件扩展为五类）。

    旧版原文（2026-09-16 用户「实盘眼睛是瞎的」补的）：
      A 柴堆点火：昨日沉默强链/加速中 概念 今日进实时榜 TOP
      B 新集群：  今日概念涨幅TOP 且 涨停家数≥3 且 昨日不在榜
      C 高位异动：热榜票当日 -7% 以下（核按钮预警）
      「不做的事：买卖建议、推送、任何市场判断。**记录本身就是发现**。」
    本件继承这个定位（零判断、只记事实），补两类按现有数据可得的：
      D 池内晋级：候选池票从普通 → 半路/封板
      E 封板恶化：封稳 → 炸板（封板链条断裂的第一信号）
    """
    out = []
    by_code = {r["code"]: r for r in rows}
    # A/B：板块点火 —— 今日涨幅 TOP 但 5 日主力净流入为负（= 新钱刚进、趋势未确认）
    for s in (sectors.get("rows") or [])[:8]:
        if s.get("pct") is None or s.get("net_in_5d") is None:
            continue
        if s["pct"] >= 1.5 and s["net_in_5d"] < 0:
            out.append({"kind": "点火", "mark": "🔥",
                        "who": "%s(%s)" % (s["name"], s["code"]),
                        "fact": "今日 %+.2f%% 但 5日主力净流出 %.2f 亿 ⇒ 新钱进、趋势未确认"
                                % (s["pct"], s["net_in_5d"] / 1e4)})
    # C：核按钮 —— 池内票 -7% 以下
    for code in cand:
        r = by_code.get(code)
        if r and (r.get("pct") or 0) <= -7.0:
            out.append({"kind": "核按钮", "mark": "⚠️",
                        "who": "%s %s" % (code, (r.get("name") or "")[:6]),
                        "fact": "%+.2f%% 量比 %s（候选池内）"
                                % (r["pct"], _s(r.get("volume_ratio")))})
    # D/E：与上一份快照的状态迁移
    NM = {"sealed": "封板", "broken": "炸板", "near": "半路", "limit_down": "跌停",
          "plain": "普通"}
    for it in (diff.get("items") or []):
        if it["from"] == "sealed" and it["to"] == "broken":
            out.append({"kind": "封板恶化", "mark": "💔",
                        "who": "%s %s" % (it["code"], it["name"]),
                        "fact": "封稳→炸板，现 %+.2f%%%s"
                                % (it["pct"] or 0, "（候选池内）" if it["in_pool"] else "")})
        elif it["to"] in ("sealed", "near") and it["from"] in ("plain", "broken"):
            if it["in_pool"]:
                out.append({"kind": "池内晋级", "mark": "⬆️",
                            "who": "%s %s" % (it["code"], it["name"]),
                            "fact": "%s→%s，现 %+.2f%%"
                                    % (NM[it["from"]], NM[it["to"]], it["pct"] or 0)})
    return out


def diff_snapshots(prev, cur_rows, cand):
    """两份快照的逐票状态迁移（层 7 · 用全市场 CSV 口径，不依赖分桶子集）。

    → list[dict]：只列**发生变化**的票，按方向分组。
    口径：状态 = 分桶名（sealed/broken/near/limit_down/普通）。
    """
    def state_of(code, buckets):
        for grp in ("sealed", "broken", "near", "limit_down"):
            if code in (buckets.get(grp) or {}):
                return grp
        return "plain"
    if not prev:
        return {"available": False, "reason": "无上一份快照（当日首轮）", "items": []}
    pst = {}
    for grp in ("sealed", "broken", "near", "limit_down"):
        for c in (prev.get(grp) or {}):
            pst[c] = grp
    now = {r["code"]: r for r in cur_rows}
    items = []
    for c, r in now.items():
        a = pst.get(c, "plain")
        b = r.get("chain") or ""
        b = {"封稳": "sealed", "回封(疑似)": "sealed", "炸板": "broken",
             "未触板": "near", "跌停": "limit_down"}.get(b, "plain")
        if a == b:
            continue
        if a == "plain" and b == "plain":
            continue
        items.append({"code": c, "name": (r.get("name") or "")[:6],
                      "from": a, "to": b, "pct": r.get("pct"),
                      "in_pool": c in cand})
    return {"available": True, "items": items,
            "prev_file": prev.get("_file"), "prev_hhmm": prev.get("_hhmm"),
            "prev_env": prev.get("_env_grade")}


def top_by(rows, key, n=10, reverse=True):
    xs = [r for r in rows if r.get(key) is not None]
    xs.sort(key=lambda x: x[key], reverse=reverse)
    return xs[:n]


# ── 环境定档（8 维打分 · 硬阈值 · 可回测）──────────────────────────────────
def _band(v, cuts, scores):
    """cuts 升序阈值，scores 对应区间分（len = len(cuts)+1）。"""
    if v is None:
        return 0, None
    for i, c in enumerate(cuts):
        if v < c:
            return scores[i], i
    return scores[-1], len(cuts)


def env_score(*, sealed_n, broken_n, near_n, limit_down_n, max_board,
              up, down, idx_pct, amount_yuan, prev_amount, progress,
              amount_ratio_same=None):
    """→ {score, grade, dims[]}。**8 维加总，禁止单维一票否决。**

    量能口径（2026-10-26 实测修正）：**优先用 changedist 的「今日 vs 上日同期」比值**
    `amount_ratio_same`。原来的「盘中额 ÷ 时间进度 → 折全日」在早盘严重高估
    （10:39 折算 26466 亿 vs 前日全天 18304 亿 → 假的 +2 分，把退潮日拉回震荡），
    因为成交额日内分布是 U 型而非线性。折全日只保留为参考值，**不参与打分**。
    """
    dims = []

    def add(name, raw, score, note=""):
        dims.append({"dim": name, "raw": raw, "score": score, "note": note})

    # 1 广度：涨跌比
    ratio = (up / down) if (up and down) else None
    s, _ = _band(ratio, [0.4, 0.8, 1.5, 3.0], [-2, -1, 0, 1, 2])
    add("广度·涨跌比", round(ratio, 3) if ratio is not None else None, s,
        "上涨%d/下跌%d" % (up or 0, down or 0))

    # 2 强度：涨停数
    s, _ = _band(sealed_n, [15, 25, 40, 60], [-2, -1, 0, 1, 2])
    add("强度·涨停数", sealed_n, s, "")

    # 3 亏钱效应：跌停数（越小越好 ⇒ 反向）
    s, _ = _band(limit_down_n, [3, 6, 13], [1, 0, -1, -2])
    add("亏钱·跌停数", limit_down_n, s, "")

    # 4 封板质量：封板率
    touch = (sealed_n or 0) + (broken_n or 0)
    seal_rate = (sealed_n / touch) if touch else None
    s, _ = _band(seal_rate, [0.35, 0.5, 0.65, 0.8], [-2, -1, 0, 1, 2])
    add("质量·封板率", round(seal_rate, 4) if seal_rate is not None else None, s,
        "封%d/炸%d" % (sealed_n or 0, broken_n or 0))

    # 5 高度：连板
    s, _ = _band(max_board, [2, 3, 4, 5], [-2, -1, 0, 1, 2])
    add("高度·最高连板", max_board, s, "")

    # 6 追封比 = 半路 / 封板（**越小越好**）
    #   2026-10-26 实测修正：原来是「半路越多越强」，但大跌日 70 只半路 vs 33 只封板
    #   恰恰是「一堆冲高封不住」，给 +2 会把杀跌日拉成震荡。封得干脆 = 半路少。
    chase = (near_n / sealed_n) if sealed_n else None
    if chase is not None and sealed_n < 15:
        # 封板太少时分母失真（12封/5半路 ⇒ 比值"好"，但绝对数说明市场根本没起来）
        # ⇒ 信息不足给 0，不让弱日在质量维上白捡分
        s, note = 0, "半路%d/封板%d（封板<15，比值失真 ⇒ 不给分）" % (near_n or 0, sealed_n)
    else:
        s, _ = _band(chase, [0.3, 0.6, 1.0, 1.5], [2, 1, 0, -1, -2])
        note = "半路%d/封板%d（越小=封得越干脆）" % (near_n or 0, sealed_n or 0)
    add("质量·追封比", round(chase, 3) if chase is not None else None, s, note)

    # 7 指数
    s, _ = _band(idx_pct, [-1.0, -0.3, 0.3, 1.0], [-2, -1, 0, 1, 2])
    add("指数·三指均值", round(idx_pct, 3) if idx_pct is not None else None, s, "")

    # 8 量能：同期比（changedist）优先；折全日仅参考
    full = None
    if amount_yuan and progress and progress > 0:
        full = amount_yuan / progress
    r = amount_ratio_same
    if r is not None:
        note = "今日/上日**同期** %.2f（changedist 直给，同口径）" % r
    else:
        rr = (full / prev_amount) if (full and prev_amount) else None
        r = rr
        note = ("折全日%.0f亿 / 前日%.0f亿（**线性折算，早盘高估，仅参考**）"
                % ((full or 0) / 1e8, (prev_amount or 0) / 1e8)) if rr is not None \
            else "量能无可比源"
    s, _ = _band(r, [0.6, 0.85, 1.1, 1.35], [-2, -1, 0, 1, 2])
    add("量能·同期比", round(r, 3) if r is not None else None, s, note)

    total = sum(d["score"] for d in dims)
    if total >= 7:
        grade = "主升"
    elif total >= 3:
        grade = "强势"
    elif total >= -2:
        grade = "震荡"
    else:
        grade = "退潮"
    return {"score": total, "grade": grade, "dims": dims,
            "range": [-16, 16], "rule": "8维加总；主升≥7 / 强势3~6 / 震荡-2~2 / 退潮≤-3"}


def progress_of(date, hhmm):
    """交易时间进度 0..1（240 分钟：9:30-11:30 + 13:00-15:00）。"""
    try:
        hh, mm = int(hhmm[:2]), int(hhmm[2:])
    except Exception:                                        # noqa: BLE001
        return None
    t = hh * 60 + mm
    if t <= 9 * 60 + 30:
        return 0.0
    if t <= 11 * 60 + 30:
        return min(1.0, (t - (9 * 60 + 30)) / 240.0)
    if t < 13 * 60:
        return 120 / 240.0
    if t <= 15 * 60:
        return min(1.0, (120 + (t - 13 * 60)) / 240.0)
    return 1.0


# ── 快照落盘 ──────────────────────────────────────────────────────────────
def alerts_path(date, base=None):
    return os.path.join(base or _ROOT, "data", "raw", "intraday", date, "alerts.md")


def append_alerts(date, hhmm, discoveries, pool, base=None):
    """盘中发现流水（**append-only · 按时点分节 · 只记增量**）。

    这是旧包 `tools/data/intraday_scan.py` 的原始形态（2026-09-16 用户「实盘眼睛是
    瞎的」补的），原文：

        输出：追加 data/generated/pattern/intraday_alerts.md —— 三类发现
        A 柴堆点火 / B 新集群 / C 高位异动
        不做的事：买卖建议、推送、任何市场判断。**记录本身就是发现。**

    旧版实例长这样（一行一条，极简）：
        ## 09-23 11:30
        - 🔥柴堆点火: 肿瘤疫苗(pt02GN2448) 昨在观察池，今日实时榜 2.52
        ## 09-22 11:30
        - ⚠️实时概念榜取空（接口异常），本档无效      ← 取数失败也如实记，不静默

    本件继承三点纪律：
      ① **append-only**：绝不覆盖/重写历史档；
      ② **按时点分节**：同一 HHMM 重跑 ⇒ 幂等跳过（不重复追加同一档）；
      ③ **零判断**：只写事实，不写「该不该买」；建议归盘前预案，扫描件不写进流水。

    → dict{path, appended(bool), n_items, reason}
    """
    p = alerts_path(date, base=base)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    head = "# 盘中发现流水（intraday_scan 自动 · 只追加）"
    prev = ""
    if os.path.isfile(p):
        prev = io.open(p, encoding="utf-8").read()
    # 幂等：该时点已记过 => 跳过
    if ("## %s:%s" % (hhmm[:2], hhmm[2:])) in prev:
        return {"path": p, "appended": False, "n_items": 0,
                "reason": "SAME_HHMM_ALREADY_LOGGED"}
    lines = []
    if not prev.strip():
        lines.append(head)
    lines.append("")
    lines.append("## %s:%s" % (hhmm[:2], hhmm[2:]))
    n = 0
    # ★ 同类聚合：点火/核按钮一多就不是「发现」而是「常态」——
    #   2026-10-26 实测一档出 7 条点火 + 24 条核按钮，读起来等于没读。
    #   纪律：≤3 条逐条列；>3 条折一条汇总（保留信息量：数量 + 最极端几只）。
    groups = {}
    for d in discoveries or []:
        groups.setdefault(d["kind"], []).append(d)
    for kind, ds in groups.items():
        if len(ds) <= 3:
            for d in ds:
                lines.append("- %s%s: %s —— %s" % (d["mark"], kind, d["who"], d["fact"]))
                n += 1
        else:
            mark = ds[0]["mark"]
            who = "、".join(d["who"] for d in ds[:5])
            lines.append("- %s%s **%d 条**（同类聚合）：%s%s" % (
                mark, kind, len(ds), who, " 等" if len(ds) > 5 else ""))
            n += 1
    # 池内晋级/恶化即使 discoveries 已含，也再给一行池内汇总（旧版「对比思维」要求）
    if pool:
        ups = [r for r in pool if (r.get("pct") or 0) >= 5.0]
        dns = [r for r in pool if (r.get("pct") or 0) <= -5.0]
        lines.append("- 📊池内: 走强 %d · 走弱 %d / 池内异动共 %d 条"
                     % (len(ups), len(dns), len(pool)))
        n += 1
    if n == 0:
        lines.append("- （本档无新发现）")
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(prev + "\n".join(lines) + "\n")
    os.replace(tmp, p)
    return {"path": p, "appended": True, "n_items": n, "reason": "OK"}


def live_flash(limit=12, timeout=90):
    """盘中直拉市场快讯（westock news list）。返回 [{time,title,src}]，失败返回 []。

    ★ 2026-11-02 增：info_feeder 快照只在日链（收盘）刷新，盘中用它=拿昨天的消息
      做今天的归因。本函数让扫描件自带实时消息腿，不依赖收盘后补数。
    """
    import subprocess
    try:
        r = subprocess.run(["westock", "news", "list", "sh000001", "--type", "2",
                            "--limit", str(limit)],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not r.stdout:
            return []
        rows = []
        for ln in r.stdout.splitlines():
            if not ln.startswith("|") or "---" in ln:
                continue
            c = [x.strip() for x in ln.strip().strip("|").split("|")]
            if len(c) < 5 or c[0] in ("id", "SN"):
                continue
            if not c[0].startswith("SN"):
                continue
            rows.append({"time": c[3], "src": c[1], "title": c[4]})
        return rows
    except Exception:                                            # noqa: BLE001
        return []


def _last_snapshot(date, exclude=None, base=None):
    """取当日**上一份**快照（分桶子集，用于状态迁移 diff）。

    → dict{sealed:[codes], broken:[...], near:[...], limit_down:[...]} 或 None。
    只读分桶键（不含全量 rows），体积小；**掉出池的票去向由 CSV 侧判定**，
    本函数只负责「上一轮谁在哪个桶」。
    """
    d = os.path.join(base or _ROOT, "data", "raw", "intraday", date)
    if not os.path.isdir(d):
        return None
    names = sorted(fn for fn in os.listdir(d)
                   if fn.startswith("scan_") and fn.endswith(".json")
                   and (exclude is None or fn != "scan_%s.json" % exclude))
    if not names:
        return None
    try:
        p = json.load(io.open(os.path.join(d, names[-1]), encoding="utf-8-sig"))
    except Exception:                                         # noqa: BLE001
        return None
    out = {"_file": names[-1], "_hhmm": p.get("hhmm"),
           "_env_grade": (p.get("env") or {}).get("grade")}
    for grp in ("sealed", "broken", "near", "limit_down"):
        out[grp] = {r["code"]: r for r in (p.get(grp) or [])}
    return out


def snapshot_paths(date, hhmm, base=None):
    root = base or _ROOT
    d = os.path.join(root, "data", "raw", "intraday", date)
    return (os.path.join(d, "scan_%s.json" % hhmm),
            os.path.join(d, "scan_%s.csv" % hhmm))


CSV_COLS = ("code", "name", "price", "pct", "high", "low", "open", "prev_close",
            "amount", "volume", "turnover_rate", "volume_ratio", "wb_ratio",
            "ceiling", "floor", "chain", "boards", "boards_src",
            "chg_5d", "chg_10d", "chg_20d", "chg_60d", "circ_mv", "data_date")


def write_snapshot(date, hhmm, rows, payload, base=None):
    jp, cp = snapshot_paths(date, hhmm, base=base)
    os.makedirs(os.path.dirname(jp), exist_ok=True)
    tmp = jp + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, jp)
    tmpc = cp + ".tmp"
    with io.open(tmpc, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, lineterminator="\n",
                           extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda x: x.get("code", "")):
            w.writerow(r)
    os.replace(tmpc, cp)
    return jp, cp


# ── 报告（X1 · 一屏扫描件骨架，事实层；买卖结论不在此出）──────────────────
def render_card(date, hhmm, payload):
    """盘中扫描件（**旧包 skills/intraday/OUTPUT-TEMPLATE.md 的 ①~⑧ 号体系**）。

    旧版定位原文：「唯一权威输出格式 = 扫描件式（一屏，每条=数据+含义+动作）。
    与盘前预案同号体系，盘中=对盘前预案的实时续写。一条铁律：每一行 =
    数据 + 含义 + 动作（缺一即该行不合格）；以「某票不错/某链有逻辑」结尾 = 未转化。」

    2026-10-26 修正（用户质问「你知道实盘扫描是干啥的不？」）：本件此前输出的是
    **市场统计报告**（涨停多少/板块涨幅/量比榜），方向错了——那是「日报」不是「扫描」。
    实盘扫描的对象是**我的持仓 + 我的候选池**，全市场数据只是背景板。
    旧包 tools/data/intraday_scan.py 原文亦写明：「不做买卖建议、任何市场判断，
    记录本身就是发现」——**发现层零判断，预案层才给动作，两层不许混**。
    """
    L = []
    A = L.append
    env, m, sec, cd, idx = (payload["env"], payload["meta"], payload["sectors"],
                            payload["changedist"], payload["index"])
    b = payload["buckets"]
    pool = payload.get("pool") or []
    diff = payload.get("diff") or {}
    disc = payload.get("discoveries") or []
    candm = payload.get("candidates_meta") or {}
    posm = payload.get("positions_meta") or {}

    A("# 盘中扫描 · %s %s:%s（数据时间 %s）" % (
        date, hhmm[:2], hhmm[2:], payload["data_date"] or "?"))
    A("")
    A("> **%s ｜ 驱动=%s ｜ 第一优先级=%s ｜ 风险锚=%s**" % (
        env["grade"], _driver_text(env, payload), _first_priority(payload),
        _risk_text(env, payload)))
    A(">")
    A("> 与盘前卡同号 ①~⑧；本件零 BUY/WAIT/NO，买卖走 C4 三问（E2 boundary）。")
    A("")

    A("## ① 实时定调")
    A("")
    prevf = payload.get("prev_snapshot")
    if prevf and diff.get("available"):
        A("vs 上一档 `%s`：状态迁移 **%d 票**（明细见 ⑦）" % (
            prevf, len(diff.get("items") or [])))
    else:
        A("当日首档（无可比前值）")
    A("")
    A("| 维度 | 实测 | 得分 | 备注 |")
    A("|---|---|---|---|")
    for d in env["dims"]:
        A("| %s | `%s` | %+d | %s |" % (d["dim"], _s(d["raw"]), d["score"], d["note"]))
    A("| **合计** | — | **%+d** | %s |" % (env["score"], env["rule"]))
    A("")
    # ★ 择时闸（T 母题 · 2026-10-28）：市场级开关，与 8 维环境分两层
    try:
        import sys as _sys2, os as _os2
        _sys2.path.insert(0, _os2.path.join(_os2.path.dirname(_os2.path.dirname(
            _os2.path.dirname(_os2.path.abspath(__file__)))), "tools", "analysis"))
        import timing as _TM
        _sig = _TM.signal(date)      # ★ 2026-11-02 收敛到 timing.signal 门面（原为本地副本）
        A("**择时闸（市场级）**：%s —— %s" % (
            "RISK_ON 允许开仓" if _sig.get("risk_on") else "**RISK_OFF 禁新开仓**",
            _sig.get("reason") or ""))
        A("")
    except Exception as _e:                                      # noqa: BLE001
        A("**择时闸**：不可用（%s）⇒ fail-closed，禁新开仓" % type(_e).__name__)
        A("")

    A("## ② 盘中消息面（实时快讯 · 陈旧自动降级）")
    A("")
    try:
        import info_feeder as IF
        _snap = IF.latest_snapshot(date)
    except Exception:
        _snap = None
    _snap_fresh = bool(_snap) and (_snap.get("date") == date)
    _news_rendered = False
    if not _snap_fresh:
        _live = live_flash()
        if _live:
            A("**实时快讯（盘中直拉 %s · 共 %d 条）**" % (_live[0].get("time", "")[:16], len(_live)))
            A("")
            A("| 时间 | 快讯 | 来源 |")
            A("|---|---|---|")
            for r in _live[:8]:
                A("| `%s` | %s | %s |" % ((r.get("time") or "")[11:16],
                                         r.get("title"), r.get("src")))
            A("")
            A("> info_feeder 快照非当日（%s）⇒ 已降级为盘中实时直拉；收盘日链会补全量归档。"
              % ((_snap or {}).get("date") or "无"))
            A("")
            _news_rendered = True
    if _snap and (_snap.get("date") == date) and ((_snap.get("flash") or {}).get("rows")
                                                  or (_snap.get("redlight") or {}).get("hits")):
        _rl = (_snap.get("redlight") or {}).get("hits") or []
        if _rl:
            A("**红灯（热搜×我的池，%d 命中）**：%s" % (
                len(_rl), "、".join("%s %s" % (h.get("code"), h.get("name"))
                                    for h in _rl[:8])))
            A("")
        A("| 时间 | 快讯（Top6） | 来源 |")
        A("|---|---|---|")
        for r in ((_snap.get("flash") or {}).get("rows") or [])[:6]:
            A("| `%s` | %s | %s |" % ((r.get("time") or "")[11:16],
                                     r.get("title"), r.get("src")))
        _na = _snap.get("notices_a") or []
        if _na:
            A("")
            A("公告 A 级 %d 条：%s" % (len(_na), "；".join(
                "%s %s" % ((r.get("code") or r.get("symbol") or "?"), str(r.get("title"))[:24]) for r in _na[:4])))
        A("")
        A("> 采集/解读分离：归因判断走 S1/event-analysis；快照 `%s_%s`。" % (
            _snap.get("date"), _snap.get("hhmm")))
        _news_rendered = True
    if not _news_rendered:
        A("| 类型 | 内容 | 影响→动作 |")
        A("|---|---|---|")
        A("| — | **消息腿不可用**（快照非当日且实时快讯拉取失败） | "
          "消息驱动票盘中无法归因，只看价量；不编造 |")
    A("")

    A("## ③ 主线光谱")
    A("")
    rows_s = sec.get("rows") or []
    if rows_s:
        A("| 层 | 板块 | 今日 | 主力净入1D/5D(亿) | 今日看什么 | 证伪线 |")
        A("|---|---|---|---|---|---|")
        cont = [x for x in rows_s[:6] if (x.get("net_in_5d") or 0) > 0][:2]
        for x in cont:
            A("| 🔥续强 | %s | %+.2f%% | %+.2f / %+.2f | 涨家数 %s，龙头 %s | "
              "龙头开板且涨家数回落 |" % (
                  x["name"], x["pct"], (x["net_in_1d"] or 0) / 1e4,
                  (x["net_in_5d"] or 0) / 1e4, x["up_count"], x["leader"]))
        fire = [x for x in rows_s[:6] if (x.get("net_in_5d") or 0) <= 0][:2]
        for x in fire:
            A("| 🌱点火 | %s | %+.2f%% | %+.2f / %+.2f | "
              "5日仍净流出=新钱刚进，看能否连续两日 | 次日主力转出即熄火 |" % (
                  x["name"], x["pct"], (x["net_in_1d"] or 0) / 1e4,
                  (x["net_in_5d"] or 0) / 1e4))
        for x in rows_s[-2:]:
            A("| 💀溃败 | %s | %+.2f%% | %+.2f / %+.2f | "
              "今日回避；持仓若在此链视作负反馈 | 收复今日跌幅一半才算止跌 |" % (
                  x["name"], x["pct"], (x["net_in_1d"] or 0) / 1e4,
                  (x["net_in_5d"] or 0) / 1e4))
    else:
        A("- SECTOR_UNAVAILABLE（%s）" % sec.get("error"))
    A("")

    A("## ④ 持仓决策")
    A("")
    if posm.get("n"):
        A("| 代码 | 名称 | 现价 | 涨幅 | 浮盈亏 | 链态 | 今日动作 |")
        A("|---|---|---|---|---|---|---|")
        byc = {r["code"]: r for r in payload["rows"]}
        for c in posm.get("codes") or []:
            r = byc.get(c) or {}
            A("| %s | %s | %s | %s%% | 见 positions.md | %s | "
              "按 C5 退出闸与逻辑失效位判定 |" % (
                  c, (r.get("name") or "?"), _s(r.get("price")), _s(r.get("pct")),
                  r.get("chain") or "普通"))
    else:
        A("**空仓（0 笔持仓）** —— 无决策项。")
        A("")
        A("> 这是**账实相符**不是数据缺失：`memory/state/positions.md` 是持仓唯一真相源，"
          "`decision_ledger.jsonl` 无 `action=BUY` 行 ⇒ 持仓必然为 0。"
          "另：`pnl` 账本 `capital=0`（初始资金未给）⇒ `drawdown_of()` 返 None ⇒ "
          "熔断闸 FAILSAFE 归零，**即使有候选也进不了 C5**。这是全链最硬的一处断点。")
    A("")

    A("## ⑤ 机会与候选")
    A("")
    A("候选池 OPEN 态 **%d 条**（读数：%s）· 今日盘中异动（|涨跌|≥5%% 或在涨停链）"
      "**%d 条**" % (candm.get("n_open") or 0, candm.get("src") or "?",
                     len(pool)))
    A("")
    if pool:
        # ★ 池内结构先给一行：今天这批候选**到底几只真能动手**（数据+含义+动作）
        from collections import Counter as _C
        act = _C(_actionable(r) for r in pool)
        n_up = sum(1 for r in pool if r["pct"] >= 5.0)
        n_dn = sum(1 for r in pool if r["pct"] <= -5.0)
        A("`可挂单 %d · 已封死 %d · 已炸板 %d · 跌停 %d · 可观察 %d · ST/退市 %d`"
          " —— 走强 **%d** / 走弱 **%d**" % (
              act.get("可挂单", 0), act.get("已封死", 0), act.get("已炸板", 0),
              act.get("跌停", 0), act.get("可观察", 0), act.get("ST/退市", 0),
              n_up, n_dn))
        A("")
        if n_dn > n_up:
            A("> ⚠️ **池内走弱（%d）多于走强（%d）**：这批候选以涨停池/龙虎榜入池 = "
              "追高型载体，退潮日集中反噬。**这意味着现在不是「池子给不给机会」的问题，"
              "是「池子本身在当前环境里就是错的载体」** —— 动作：本档只核不动，"
              "等环境分回到 ≥-2（震荡）再谈池内开仓。" % (n_dn, n_up))
        else:
            A("> 池内走强（%d）多于走弱（%d），载体与环境同向。"
              "动作：可挂单层（%d 只）逐个过 C4 三问，其余已封死/已炸板的不追。"
              % (n_up, n_dn, act.get("可挂单", 0)))
        A("")
    if pool:
        up_pool = [r for r in pool if r["pct"] >= 5.0]
        dn_pool = [r for r in pool if r["pct"] <= -5.0]
        mid_pool = [r for r in pool if -5.0 < r["pct"] < 5.0]

        def _tbl(items, n, note):
            A("**%s**（%d 条%s）" % (note[0], len(items),
                                    "，列前 %d" % n if len(items) > n else ""))
            A("")
            A("| 代码 | 名称 | 现价 | 涨幅 | 链态 | 量比 | 来源 | 到期 | 剩余 | 可执行 |")
            A("|---|---|---|---|---|---|---|---|---|---|")
            for r in items[:n]:
                dl = r.get("days_left")
                A("| %s | %s | %s | %+.2f%% | %s | %s | %s | %s | %s | %s |" % (
                    r["code"], (r["name"] or "")[:5], _s(r.get("price")), r["pct"],
                    r["chain"], _s(r.get("volume_ratio")),
                    (r.get("source") or "-").replace("limit_up", "涨停池")
                    .replace("dragon_tiger", "龙虎榜").replace("|", "+"),
                    r.get("expiry") or "-",
                    ("%d天" % dl) if dl is not None else "-",
                    _actionable(r)))
            A("")

        # ★ 按方向拆表：涨幅向=机会侧，跌幅向=风险侧，中间=涨停链但幅度不大
        #   （2026-10-26 修：原按 |pct| 混排会把跌停票排到「机会」第一行，方向感全失）
        if up_pool:
            _tbl(up_pool, 12, ("涨幅向（机会侧：池内今天在走强）",))
        if mid_pool:
            _tbl(mid_pool, 6, ("涨停链内但幅度居中（多为炸板回落，⚠️分歧）",))
        if dn_pool:
            _tbl(dn_pool, 8, ("跌幅向（风险侧：池内今天在走弱，勿碰）",))
        A("> **本表只说「它在池内、今天在动」，不构成买入依据。**"
          "要买须过 C4 三问（驱动/赔率/证伪）+ C5 定价。"
          "链态 `未触板`=半路可挂单层 · `炸板`=触板回落（分歧或出货）· "
          "`封稳`=已封死（当日买不进，只能排次日）。")
    else:
        A("**今日无池内异动。** 候选池 %d 条今日均未达异动线（|涨跌|≥5%% 或涨停链）。"
          % (candm.get("n_open") or 0))
        A("")
        A("> 候选池空/无异动是环境的结果不是失职——今天环境 %s，不出手本身就是动作。"
          % env["grade"])
    A("")

    A("## ⑥ 风险与不做")
    A("")
    A("| 等级 | 风险 | 动作 |")
    A("|---|---|---|")
    seal_rate = (b["sealed_n"] / max(1, b["sealed_n"] + b["broken_n"])) * 100
    r_ratio = payload.get("ratio")
    red = (b["limit_down_n"] >= 20) or (r_ratio is not None and r_ratio < 0.4)
    A("| %s | 跌停 **%d** 家 · 涨跌比 **%s** · 封板率 **%.1f%%** | %s |" % (
        "🔴" if red else "🟡", b["limit_down_n"], _s(r_ratio), seal_rate,
        "空仓等待，禁抄跌停、禁追高" if red else "控制仓位，只做池内确认票"))
    fires = [d for d in disc if d["kind"] == "点火"]
    others = [d for d in disc if d["kind"] != "点火"]
    for d in fires[:2]:
        A("| 🟡 | %s %s：%s | 记录待察，不动作 |" % (d["mark"], d["who"], d["fact"]))
    if len(fires) > 2:
        A("| 🟡 | 🔥 另有 **%d 个板块**同形态（今日涨但 5 日主力净流出） | "
          "同属「新钱刚进」，一并观察不单独动作 |" % (len(fires) - 2))
    for d in others[:6]:
        A("| 🟡 | %s %s：%s | 记录待察，不动作 |" % (d["mark"], d["who"], d["fact"]))
    expiring = [r for r in pool
                if (r.get("days_left") is not None and r["days_left"] <= 0)]
    if expiring:
        A("| 🟡 | 候选临期 %d 条：%s | 今日不处理则自动 EXPIRED（到期出池） |" % (
            len(expiring), "、".join(
                "%s %s" % (x["code"], (x["name"] or "")[:4]) for x in expiring[:5])))
    A("")
    A("**不做清单**（机械禁令）")
    A("- 不抄今日跌停（%d 家，含 %s）" % (
        b["limit_down_n"],
        "、".join((x.get("name") or "")[:5]
                 for x in (payload.get("limit_down") or [])[:3]) or "—"))
    A("- 不追 `半路` 层已涨 >7% 的票（距板 <3% 时追高即接最后一棒）")
    A("- 不做池外票（未过 C2 入池 + C3 驱动，无 Thesis ⇒ 无法给证伪线）")
    A("")

    A("## ⑦ 假设与变化")
    A("")
    A("**本档未产新假设**（假设归 `thesis.py` / `hypothesize.py`，盘中只读不立账）。")
    A("")
    if diff.get("available"):
        NM = {"sealed": "封板", "broken": "炸板", "near": "半路",
              "limit_down": "跌停", "plain": "普通"}
        items = diff.get("items") or []
        if items:
            A("**vs `%s` 逐票状态迁移（%d 票）**" % (
                diff.get("prev_file") or prevf, len(items)))
            A("")
            A("| 票 | 名称 | 迁移 | 现涨幅 | 在池 |")
            A("|---|---|---|---|---|")
            order = {"sealed": 0, "broken": 1, "near": 2, "limit_down": 3, "plain": 4}
            for it in sorted(items, key=lambda x: (order.get(x["from"], 9),
                                                   order.get(x["to"], 9)))[:14]:
                A("| %s | %s | %s→%s | %s%% | %s |" % (
                    it["code"], it["name"], NM[it["from"]], NM[it["to"]],
                    _s(it["pct"]), "✓" if it["in_pool"] else ""))
        else:
            A("本档无状态迁移（与上一档完全一致）")
    else:
        A("- 无上一档可比（当日首轮）")
    A("")

    A("## ⑧ 盲区对照")
    A("")
    A("- **消息面**：优先当日快照，非当日则盘中直拉快讯；两者皆失败才记空白 ⇒ 若由盘中午间消息驱动，"
      "本件**归因不出**，只能看到价量结果。这是今天最大的盲区。")
    A("- **封板链态只到疑似**：区分「一直封」与「炸后回封」需分钟腿；"
      "`委比<90` 记 `回封(疑似)`。")
    A("- **首封时间/封单额无源**：quote 不给，旧版靠东财 zt_pool 14:57 定格；"
      "本件以委比近似。")
    if payload.get("oversold_gate"):
        g = payload["oversold_gate"]
        A("- **超跌腿已关**：%s（本可入选 %d 只，按旧包口径全部抑制）" % (
            g["detail"], g["suppressed_n"]))
    A("- **持仓侧无法验证**：空仓 + capital=0 ⇒ ④段只能写「无决策项」，"
      "这套卡在持仓场景下的有效性**尚未被检验过一次**。")
    A("")
    A("---")
    A("")
    A("```")
    A("取数 %d 码 → %d 行 · 覆盖 %.1f%% · %s 秒 · %d 批 %s" % (
        m["req_codes"], m["got_rows"], m["coverage"] * 100, m["sec"],
        len(m["batches"]),
        "全成功" if not m["failed_batches"]
        else "失败 %d 批" % len(m["failed_batches"])))
    A("指数：%s" % " / ".join(
        "%s %+.2f%%" % (v["name"], v["pct"])
        for v in idx.get("index", {}).values() if v.get("pct") is not None))
    A("前日基线：%s（涨停 %s · 成交额 %.0f 亿）" % (
        payload["prev_date"], payload["prev_limit_up_n"],
        (payload["prev_amount"] or 0) / 1e8))
    A("交叉校验：本件涨停 %d / changedist %s · 跌停 %d / %s · 成交额同期比 %s" % (
        b["sealed_n"], _s(cd.get("limit_up")), b["limit_down_n"],
        _s(cd.get("limit_down")), _s(cd.get("amount_ratio_same"))))
    A("```")
    A("")
    return "\n".join(L)


def _actionable(r):
    """可执行性（**机械四态，禁形容词**）。

    `封稳` / `回封(疑似)` = 已封死，当日买不进（只能排次日）；
    `跌停` = 卖不出也买不进；`炸板` = 已回落，追它等于接最后一棒；
    只有 `未触板`（半路）才是真可挂单层。
    """
    if not r.get("tradable"):
        return "ST/退市"
    ch = r.get("chain") or ""
    if ch in ("封稳", "回封(疑似)"):
        return "已封死"
    if ch == "跌停":
        return "跌停"
    if ch == "炸板":
        return "已炸板"
    if ch == "未触板":
        return "可挂单"
    return "可观察"


def _first_priority(payload):
    """第一优先级：按持仓/候选/环境三态机械给（不给形容词）。"""
    posm = payload.get("positions_meta") or {}
    pool = payload.get("pool") or []
    if posm.get("n"):
        return "先处理持仓（%d 笔），候选靠后" % posm["n"]
    if pool:
        return "空仓 + 池内 %d 票异动，只核对池内，禁开池外" % len(pool)
    return "空仓且池内无异动 ⇒ 等环境，今日无动作"


def _s(v):
    return "-" if v is None else (("%g" % v) if isinstance(v, float) else str(v))


def _d(a, b):
    if a is None or b is None:
        return "-"
    return "%+d" % (int(a) - int(b))


def _driver_text(env, payload):
    b = payload["buckets"]
    if env["grade"] in ("主升", "强势"):
        return "封板率%s / 涨停%d / 半路%d" % (
            _s(round(b["sealed_n"] / max(1, b["sealed_n"] + b["broken_n"]), 2)),
            b["sealed_n"], b["near_n"])
    if env["grade"] == "退潮":
        return "跌停%d / 涨跌比%s / 封板率%s" % (
            b["limit_down_n"], _s(payload["ratio"]),
            _s(round(b["sealed_n"] / max(1, b["sealed_n"] + b["broken_n"]), 2)))
    return "涨停%d / 半路%d / 涨跌比%s" % (b["sealed_n"], b["near_n"],
                                      _s(payload["ratio"]))


def _risk_text(env, payload):
    b = payload["buckets"]
    worst = sorted(env["dims"], key=lambda d: d["score"])[:2]
    return "；".join("%s %+d" % (d["dim"], d["score"]) for d in worst)


# ── 主流程 ────────────────────────────────────────────────────────────────
def scan(date=None, hhmm=None, limit=None, base=None, on_progress=None):
    import datetime as _dt
    now = _dt.datetime.now()
    date = date or now.strftime("%Y-%m-%d")
    hhmm = hhmm or now.strftime("%H%M")
    codes = universe(date, base=base, limit=limit)
    if not codes:
        return {"status": "NO_UNIVERSE", "date": date}
    rows, meta = fetch_quotes(codes, on_progress=on_progress)
    if not rows:
        return {"status": "NO_QUOTE", "date": date, "meta": meta}
    prev = prev_baseline(date, base=base)
    buckets = classify(rows, prev)
    sealed, broken, near = buckets["sealed"], buckets["broken"], buckets["near"]
    max_board = max([int(r.get("boards") or 1) for r in sealed], default=None)
    up_n = sum(1 for r in rows if (r.get("pct") or 0) > 0)
    down_n = sum(1 for r in rows if (r.get("pct") or 0) < 0)
    amount = sum(r.get("amount") or 0 for r in rows)
    idx = fetch_index()
    ipcts = [v["pct"] for v in idx.get("index", {}).values() if v.get("pct") is not None]
    idx_pct = (sum(ipcts) / len(ipcts)) if ipcts else None
    prev_amt = (prev.get("env") or {}).get("turnover")
    prog = progress_of(date, hhmm)
    sec = fetch_sectors()
    cd = fetch_changedist()
    env = env_score(sealed_n=len(sealed), broken_n=len(broken), near_n=len(near),
                    limit_down_n=len(buckets["limit_down"]), max_board=max_board,
                    up=up_n, down=down_n, idx_pct=idx_pct, amount_yuan=amount,
                    prev_amount=prev_amt, progress=prog,
                    amount_ratio_same=cd.get("amount_ratio_same"))
    oversold = [r for r in rows
                if (r.get("chg_20d") is not None and r["chg_20d"] <= OVERSOLD_20D
                    and (r.get("pct") or 0) > 3.0)]
    oversold.sort(key=lambda x: -x["chg_20d"])
    # ── 观察池（E2-HR-4：扫描范围 = 持仓票 + 候选票）──────────────────────
    cand, cand_src = load_candidates(date, base=base)
    pos, pos_src = load_positions(base=base)
    prev_snap = _last_snapshot(date, exclude=hhmm, base=base)
    diff = diff_snapshots(prev_snap, rows, cand)
    pool = watch_pool(rows, cand, pos, date)
    disc = discoveries(rows, cand, sec, diff, date)
    # ── 超跌腿（旧包 oversold_scan 口径：**反弹日闸**必过）──────────────────
    #   旧包实测（3,539,535 行 / 5,222 只 / 659 天）：单「超跌」T+5 超额 +0.23pp(t=1.58)
    #   零超额；单「反弹日」零超额；「深超跌 × 反弹日 × 当天涨>3%」T+5 **+1.63pp(t=4.11)**。
    #   ⇒ 上涨占比 < BREADTH_REB(65%) 时这条腿**根本不开**——不是「池子小」，是前提不成立。
    breadth = (up_n / len(rows) * 100.0) if rows else None
    oversold_gate = None
    if breadth is not None and breadth < BREADTH_REB:
        oversold_gate = {
            "code": "BREADTH_TOO_LOW",
            "detail": "上涨占比 %.1f%% < %.0f%% ⇒ 非反弹日；旧包实测该腿零超额，不开"
                      % (breadth, BREADTH_REB),
            "suppressed_n": len(oversold)}
        oversold = []
    data_date = next((r.get("data_date") for r in rows if r.get("data_date")), None)
    payload = {
        "schema": 1, "kind": "intraday_scan", "date": date, "hhmm": hhmm,
        "data_date": data_date, "progress": prog,
        "meta": meta, "prev_date": prev.get("prev_date"),
        "prev_limit_up_n": prev.get("limit_up_n"), "prev_amount": prev_amt,
        "up_n": up_n, "down_n": down_n, "ratio": round(up_n / down_n, 4) if down_n else None,
        "amount_yuan": amount,
        "env": env, "sectors": sec, "changedist": cd, "index": idx,
        "buckets": {"sealed_n": len(sealed), "broken_n": len(broken),
                    "near_n": len(near), "limit_down_n": len(buckets["limit_down"]),
                    "max_board": max_board, "boards_hist": boards_hist(sealed)},
        "sealed": sealed, "broken": broken, "near": near,
        "limit_down": buckets["limit_down"],
        "oversold": oversold, "rows": rows,
        "oversold_gate": oversold_gate, "breadth_pct": round(breadth, 2) if breadth else None,
        "pool": pool, "diff": diff, "discoveries": disc,
        "candidates_meta": {"n_open": len(cand), "src": cand_src},
        "positions_meta": {"n": len(pos), "src": pos_src,
                           "codes": sorted(pos.keys())},
        "prev_snapshot": (prev_snap or {}).get("_file"),
        "boundary": "事实层 + 环境档 + 池内异动；零 BUY/WAIT/NO（须走 C4 三问）",
    }
    payload["status"] = "OK"
    return payload


LOCK_STALE_H = 1.0   # 扫描是分钟级任务；锁超 1h 未刷新视为僵死可覆盖（M21）


def acquire_lock(max_age_h=LOCK_STALE_H):
    """→ (ok, msg)。盘中扫描互斥：防两次扫描同写一个 hhmm 快照。

    超 `max_age_h` 未刷新视为僵死可覆盖 —— 否则崩溃后锁永久堵死（M21 事故）。
    """
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    if os.path.exists(LOCK):
        try:
            info = json.load(io.open(LOCK, encoding="utf-8"))
            age_h = time.time() - float(info.get("ts") or 0)
            if age_h < (max_age_h or 0) * 3600:
                return False, "已有扫描在跑（pid=%s，%.2f 小时前）" % (
                    info.get("pid"), age_h)
        except Exception:                                    # noqa: BLE001
            pass          # 锁件坏 ⇒ 当僵死处理
    with io.open(LOCK, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "ts": time.time()}, f)
    return True, "ok"


def release_lock():
    try:
        if os.path.exists(LOCK):
            os.remove(LOCK)
    except Exception:                                        # noqa: BLE001
        pass


def _selftest():
    import tempfile as _tf
    import shutil as _sh
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== intraday_scan --selftest ==")
    # A 解析
    md = ("| code | name | price | change_percent |\n| --- | --- | --- | --- |\n"
          "| sh600000 | 浦发 | 10.5 | 1.2 |\n")
    T("A1 markdown 表格解析", _parse_md(md)[0]["code"] == "sh600000"
      and _f(_parse_md(md)[0]["price"]) == 10.5)
    T("A2 _f 空/异常值 → None", _f("") is None and _f("-") is None and _f("x") is None)
    # B 分桶三态
    prev = {"boards": {"sh600001": 2}}
    rows = [
        {"code": "sh600001", "price": 11.0, "high": 11.0, "pct": 10.0,
         "ceiling": 11.0, "floor": 9.0, "wb_ratio": 100.0, "amount": 1e8},
        {"code": "sh600002", "price": 10.2, "high": 11.0, "pct": 2.0,
         "ceiling": 11.0, "floor": 9.0, "wb_ratio": 20.0, "amount": 1e8},
        {"code": "sh600003", "price": 10.6, "high": 10.7, "pct": 6.0,
         "ceiling": 11.0, "floor": 9.0, "wb_ratio": 30.0, "amount": 1e8},
        {"code": "sh600004", "price": 9.0, "high": 9.8, "pct": -10.0,
         "ceiling": 11.0, "floor": 9.0, "wb_ratio": -100.0, "amount": 1e8},
        {"code": "sh600005", "price": 5.0, "high": 5.1, "pct": 0.5,
         "ceiling": 5.5, "floor": 4.5, "wb_ratio": 0.0, "amount": 1e8},
    ]
    b = classify(rows, prev)
    T("B1 封板（price≥ceiling）且连板递推 prev+1",
      len(b["sealed"]) == 1 and b["sealed"][0]["boards"] == 3
      and b["sealed"][0]["boards_src"] == "prev+1")
    T("B2 炸板（high≥ceiling 且 price<ceiling）", len(b["broken"]) == 1
      and b["broken"][0]["code"] == "sh600002")
    T("B3 半路板（pct≥4.5 且 high<ceiling）", len(b["near"]) == 1
      and b["near"][0]["code"] == "sh600003")
    T("B4 跌停（price≤floor）", len(b["limit_down"]) == 1
      and b["limit_down"][0]["code"] == "sh600004")
    T("B5 普通票不入任何池", len(b["plain"]) == 1)
    # C 封板链态
    rows2 = [dict(rows[0], code="sh600010", wb_ratio=50.0),
             dict(rows[0], code="sh600011", wb_ratio=100.0)]
    b2 = classify(rows2, {"boards": {}})
    T("C1 委比<90 → 回封(疑似)；≥90 → 封稳",
      b2["sealed"][0]["chain"] == "回封(疑似)" and b2["sealed"][1]["chain"] == "封稳")
    T("C2 前日无记录 → boards=1 且标 new_or_unsealed",
      all(x["boards"] == 1 and x["boards_src"] == "new_or_unsealed" for x in b2["sealed"]))
    # D 环境打分（可复现 + 单调）
    e1 = env_score(sealed_n=70, broken_n=10, near_n=45, limit_down_n=0, max_board=6,
                   up=4200, down=600, idx_pct=1.2, amount_yuan=1.9e12,
                   prev_amount=1.83e12, progress=1.0, amount_ratio_same=1.05)
    e2 = env_score(sealed_n=12, broken_n=30, near_n=5, limit_down_n=25, max_board=1,
                   up=600, down=4200, idx_pct=-2.0, amount_yuan=6e11,
                   prev_amount=1.83e12, progress=1.0, amount_ratio_same=0.5)
    T("D1 极强环境 → 主升（≥7）", e1["grade"] == "主升" and e1["score"] >= 7)
    T("D2 极弱环境 → 退潮（≤-3）", e2["grade"] == "退潮" and e2["score"] <= -3)
    T("D3 8 维齐 + 同输入同输出（可回测）",
      len(e1["dims"]) == 8
      and json.dumps(e1, sort_keys=True) == json.dumps(
          env_score(sealed_n=70, broken_n=10, near_n=45, limit_down_n=0, max_board=6,
                    up=4200, down=600, idx_pct=1.2, amount_yuan=1.9e12,
                    prev_amount=1.83e12, progress=1.0, amount_ratio_same=1.05),
          sort_keys=True))
    # D4：单维极端不得一票否决（旧 regime failback 缺陷回归）
    e3 = env_score(sealed_n=73, broken_n=51, near_n=40, limit_down_n=1, max_board=6,
                   up=4200, down=800, idx_pct=0.5, amount_yuan=1.83e12,
                   prev_amount=1.8e12, progress=1.0, amount_ratio_same=1.0)
    T("D4 炸板率高（41%%）但其余强 ⇒ 不得单独判退潮",
      e3["grade"] != "退潮" and e3["score"] > 0)
    # D5：追封比口径（封板少时不给分；半路远多于封板 ⇒ 负分）
    e4 = env_score(sealed_n=33, broken_n=24, near_n=70, limit_down_n=19, max_board=4,
                   up=1270, down=3538, idx_pct=-1.72, amount_yuan=7.5e11,
                   prev_amount=1.83e12, progress=0.29, amount_ratio_same=0.97)
    chase_dim = [d for d in e4["dims"] if d["dim"] == "质量·追封比"][0]
    T("D5 半路70/封板33=2.12 ⇒ 追封比 -2（封不住），环境判退潮",
      chase_dim["score"] == -2 and e4["grade"] == "退潮")
    # D6：线性折算不得冒充同期比（早盘高估回归）
    e5 = env_score(sealed_n=33, broken_n=24, near_n=70, limit_down_n=19, max_board=4,
                   up=1270, down=3538, idx_pct=-1.72, amount_yuan=7.5e11,
                   prev_amount=1.83e12, progress=0.29, amount_ratio_same=None)
    amt_dim = [d for d in e5["dims"] if d["dim"] == "量能·同期比"][0]
    T("D6 无同期比时 note 明确标注「线性折算，早盘高估」",
      "线性折算" in amt_dim["note"] and "高估" in amt_dim["note"])
    # E 时间进度
    T("E1 progress_of 边界（09:30=0 / 10:30=0.25 / 11:30=0.5 / 13:00=0.5 / 15:00=1）",
      progress_of("x", "0930") == 0.0 and abs(progress_of("x", "1030") - 0.25) < 1e-9
      and abs(progress_of("x", "1130") - 0.5) < 1e-9
      and abs(progress_of("x", "1300") - 0.5) < 1e-9
      and progress_of("x", "1500") == 1.0)
    # F 落盘 + 报告
    sb = _tf.mkdtemp(prefix="iscan_")
    try:
        p = {
            "schema": 1, "date": "2026-10-26", "hhmm": "1030",
            "data_date": "2026-10-26", "progress": 0.25,
            "meta": {"req_codes": 5, "got_rows": 5, "coverage": 1.0, "sec": 0.1,
                     "batches": [{"req": 5, "got": 5, "sec": 0.1}], "failed_batches": []},
            "prev_date": "2026-10-23", "prev_limit_up_n": 73, "prev_amount": 1.83e12,
            "up_n": 3, "down_n": 2, "ratio": 1.5, "amount_yuan": 1e11,
            "env": {"score": 1, "grade": "震荡", "range": [-16, 16], "rule": "r",
                    "dims": [{"dim": "广度·涨跌比", "raw": 1.5, "score": 1, "note": ""}]},
            "sectors": {"status": "OK", "rows": [
                {"name": "医药", "pct": 2.0, "net_in_1d": 1e4, "net_in_5d": 1e4,
                 "up_count": "28/32", "leader": "百洋医药"}]},
            "changedist": {"status": "OK", "limit_up": 5, "limit_down": 1, "up": 3, "down": 2},
            "index": {"status": "OK", "index": {"sh000001": {"name": "上证指数", "pct": 0.3}}},
            "buckets": {"sealed_n": 1, "broken_n": 1, "near_n": 1, "limit_down_n": 1,
                        "max_board": 3, "boards_hist": {"3": 1}},
            "sealed": b["sealed"], "broken": b["broken"], "near": b["near"],
            "limit_down": b["limit_down"], "oversold": [], "rows": rows,
        }
        jp, cp = write_snapshot("2026-10-26", "1030", rows, p, base=sb)
        T("F1 快照双落（json+csv）",
          os.path.isfile(jp) and os.path.isfile(cp)
          and os.path.basename(jp) == "scan_1030.json")
        T("F2 json 可回读且 status 无关字段齐",
          json.load(io.open(jp, encoding="utf-8"))["buckets"]["sealed_n"] == 1)
        md = render_card("2026-10-26", "1030", p)
        T("F3 扫描件 ①~⑧ 八段齐（旧包 OUTPUT-TEMPLATE 号体系）且显式零买卖结论",
          all(k in md for k in ("## ① 实时定调", "## ② 盘中消息面", "## ③ 主线光谱",
                                "## ④ 持仓决策", "## ⑤ 机会与候选", "## ⑥ 风险与不做",
                                "## ⑦ 假设与变化", "## ⑧ 盲区对照"))
          and "零 BUY/WAIT/NO" in md)
        T("F4 报告无越界结论词（买入/卖出建议/加仓/清仓）",
          not any(w in md for w in ("建议买入", "建议卖出", "清仓", "满仓")))
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    # ── G 组：观察池 / 状态迁移 / 超跌闸 / 可执行性 / 发现流水 ─────────────
    T("G1 is_tradable：ST 与退市整理（名称含「退」）与北交所一律 False",
      is_tradable("sh600001", "ST浦发") is False
      and is_tradable("sh600193", "退市创兴") is False
      and is_tradable("bj920033", "康普化学") is False
      and is_tradable("sh600001", "浦发银行") is True)
    T("G2 _actionable 机械四态（封稳/回封=已封死，未触板=可挂单，炸板/跌停各归其位）",
      _actionable({"chain": "封稳", "tradable": True}) == "已封死"
      and _actionable({"chain": "回封(疑似)", "tradable": True}) == "已封死"
      and _actionable({"chain": "未触板", "tradable": True}) == "可挂单"
      and _actionable({"chain": "炸板", "tradable": True}) == "已炸板"
      and _actionable({"chain": "跌停", "tradable": True}) == "跌停"
      and _actionable({"chain": "", "tradable": False}) == "ST/退市")
    sb2 = _tf.mkdtemp(prefix="iscan_pool_")
    try:
        os.makedirs(os.path.join(sb2, "data", "state"), exist_ok=True)
        os.makedirs(os.path.join(sb2, "memory", "state"), exist_ok=True)
        io.open(os.path.join(sb2, "data", "state", "candidates.json"), "w",
                encoding="utf-8").write(json.dumps({"candidates": [
                    {"code": "sh600001", "state": "DISCOVERED", "expiry": "2026-10-30",
                     "source": "limit_up", "last_seen": "2026-10-23",
                     "trigger": {"text": "涨停池命中"}},
                    {"code": "sh600002", "state": "EXPIRED", "expiry": "2026-10-01",
                     "source": "limit_up"}]}))
        cand, csrc = load_candidates("2026-10-26", base=sb2)
        T("G3 候选池只收 OPEN 态（EXPIRED 不进池）",
          csrc == "OK" and list(cand) == ["sh600001"]
          and cand["sh600001"]["days_left" if False else "expiry"] == "2026-10-30")
        pos, psrc = load_positions(base=sb2)
        T("G4 positions.md 缺席 ⇒ 空持仓且不抛（空仓=账实相符不是缺数据）",
          psrc == "NO_FILE" and pos == {})
        io.open(os.path.join(sb2, "memory", "state", "positions.md"), "w",
                encoding="utf-8").write(
            "| 代码 | 名称 | 建仓日 | 成本 | 数量 |\n| --- | --- | --- | --- | --- |\n"
            "| sh600519 | 贵州茅台 | 2026-10-20 | 1335.5 | 100 |\n")
        pos2, psrc2 = load_positions(base=sb2)
        T("G5 positions.md 表格解析（成本/数量取数）",
          psrc2 == "OK" and pos2.get("sh600519", {}).get("cost") == 1335.5
          and pos2["sh600519"]["qty"] == 100)
        # G6 watch_pool 只收异动
        rows2 = [
            {"code": "sh600001", "name": "甲", "price": 11.0, "pct": 10.0,
             "chain": "封稳", "volume_ratio": 3.0},
            {"code": "sh600003", "name": "丙", "price": 10.2, "pct": 2.0,
             "chain": "", "volume_ratio": 1.0},
        ]
        pool2 = watch_pool(rows2, cand, {}, "2026-10-26")
        T("G7 watch_pool 只收异动票（+2% 不进表），并带剩余天数",
          len(pool2) == 1 and pool2[0]["code"] == "sh600001"
          and pool2[0]["days_left"] == 4 and pool2[0]["held"] is False)
        # G8 diff_snapshots 状态迁移
        prev2 = {"_file": "scan_1000.json", "_hhmm": "1000", "_env_grade": "震荡",
                 "sealed": {"sh600001": {}}, "broken": {}, "near": {},
                 "limit_down": {}}
        cur2 = [{"code": "sh600001", "name": "甲", "pct": 3.0, "chain": "炸板"},
                {"code": "sh600009", "name": "王", "pct": 6.0, "chain": "未触板"}]
        d2 = diff_snapshots(prev2, cur2, {})
        T("G9 diff_snapshots 抓到 封板→炸板 与 普通→半路",
          d2["available"] is True and d2["prev_file"] == "scan_1000.json"
          and {(x["code"], x["from"], x["to"]) for x in d2["items"]}
          == {("sh600001", "sealed", "broken"), ("sh600009", "plain", "near")})
        T("G10 diff_snapshots 无前值 ⇒ available=False（当日首轮）",
          diff_snapshots(None, cur2, {})["available"] is False)
        # G11 append_alerts：追加不覆盖 + 同时点幂等
        d3 = os.path.join(sb2, "data", "raw", "intraday", "2026-10-26")
        os.makedirs(d3, exist_ok=True)
        a1 = append_alerts("2026-10-26", "1030",
                           [{"kind": "点火", "mark": "🔥", "who": "X(pt1)",
                             "fact": "涨 2%"}], [], base=sb2)
        a2 = append_alerts("2026-10-26", "1030",
                           [{"kind": "点火", "mark": "🔥", "who": "X(pt1)",
                             "fact": "涨 2%"}], [], base=sb2)
        a3 = append_alerts("2026-10-26", "1100",
                           [{"kind": "核按钮", "mark": "⚠️", "who": "Y",
                             "fact": "-8%"}], [], base=sb2)
        txt = io.open(a1["path"], encoding="utf-8").read()
        T("G11 append_alerts 同时点幂等 + 不同时点追加（历史档不被覆盖）",
          a1["appended"] is True and a2["appended"] is False
          and a2["reason"] == "SAME_HHMM_ALREADY_LOGGED"
          and a3["appended"] is True
          and "## 10:30" in txt and "## 11:00" in txt
          and "本档无新发现" not in txt)
        a4 = append_alerts("2026-10-26", "1200", [], [], base=sb2)
        T("G12 无发现也记一档（「本档无新发现」，不静默跳过）",
          a4["appended"] is True
          and "本档无新发现" in io.open(a1["path"], encoding="utf-8").read())
    finally:
        _sh.rmtree(sb2, ignore_errors=True)
    # G13：超跌腿反弹日闸（旧包 oversold_scan BREADTH_REB=65 口径）
    import types as _types
    _orig = globals()["fetch_quotes"]

    def _stub_q(codes, on_progress=None):
        # 1 只深超跌且上涨（本可入超跌池）+ 9 只下跌 ⇒ 上涨占比 10% < 65% ⇒ 闸必须拦
        rows3 = [{"code": "sh600001", "name": "甲", "price": 10.0, "pct": 5.0,
                  "ceiling": 11.0, "floor": 9.0, "high": 10.0, "wb_ratio": 50.0,
                  "amount": 1e8, "chg_20d": -40.0, "data_date": "2026-10-26"}]
        for i in range(9):
            rows3.append({"code": "sh6001%02d" % i, "name": "跌%d" % i,
                          "price": 9.8, "pct": -2.0, "ceiling": 11.0, "floor": 9.0,
                          "high": 10.0, "wb_ratio": 0.0, "amount": 1e7,
                          "chg_20d": -5.0, "data_date": "2026-10-26"})
        return rows3, {"req_codes": 10, "got_rows": 10, "sec": 0.0, "coverage": 1.0,
                       "batches": [], "failed_batches": []}
    try:
        globals()["fetch_quotes"] = _stub_q
        globals()["fetch_index"] = lambda: {"status": "EMPTY", "index": {}}
        globals()["fetch_sectors"] = lambda: {"status": "EMPTY", "rows": []}
        globals()["fetch_changedist"] = lambda: {"status": "EMPTY"}
        globals()["universe"] = lambda *a, **k: ["sh600001"] + ["sh6001%02d" % i for i in range(9)]
        globals()["prev_baseline"] = lambda *a, **k: {"prev_date": None, "boards": {},
                                                     "limit_up_n": None, "env": None}
        globals()["load_candidates"] = lambda *a, **k: ({}, "STUB")
        globals()["load_positions"] = lambda *a, **k: ({}, "STUB")
        globals()["_last_snapshot"] = lambda *a, **k: None
        sb3 = _tf.mkdtemp(prefix="iscan_gate_")
        try:
            p3 = scan(date="2026-10-26", hhmm="1030", base=sb3)
            T("G13 ★超跌腿反弹日闸：上涨占比 <65% ⇒ 不出超跌池并记 suppressed_n",
              p3.get("oversold") == []
              and (p3.get("oversold_gate") or {}).get("code") == "BREADTH_TOO_LOW"
              and p3["oversold_gate"]["suppressed_n"] >= 1)
        finally:
            _sh.rmtree(sb3, ignore_errors=True)
    finally:
        globals()["fetch_quotes"] = _orig
    print("== intraday_scan --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    date = argv[argv.index("--date") + 1] if "--date" in argv else None
    hhmm = argv[argv.index("--hhmm") + 1] if "--hhmm" in argv else None
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None
    execute = "--execute" in argv
    got_lock, msg = acquire_lock()
    if execute and not got_lock:
        print("⛔ 另一个扫描进程持锁（%s）：%s" % (LOCK, msg), file=sys.stderr)
        return 3
    try:
        def prog(done, total, got):
            print("    进度 %d/%d · 已取 %d 行" % (done, total, got), file=sys.stderr)
        p = scan(date=date, hhmm=hhmm, limit=limit, on_progress=prog)
        if p.get("status") != "OK":
            print("⛔ %s" % p.get("status"), file=sys.stderr)
            return 2
        print("扫描 %s %s · 数据时间 %s · %d 行 / %d 码 · 覆盖 %.1f%% · %.1fs"
              % (p["date"], p["hhmm"], p["data_date"], p["meta"]["got_rows"],
                 p["meta"]["req_codes"], p["meta"]["coverage"] * 100, p["meta"]["sec"]))
        b = p["buckets"]
        print("封板 %d · 炸板 %d · 半路 %d · 跌停 %d · 最高连板 %s"
              % (b["sealed_n"], b["broken_n"], b["near_n"], b["limit_down_n"],
                 b["max_board"]))
        print("环境 %s（%+d/%d）" % (p["env"]["grade"], p["env"]["score"],
                                  p["env"]["range"][1]))
        for d in p["env"]["dims"]:
            print("  %-14s %-10s %+d  %s" % (d["dim"], _s(d["raw"]), d["score"], d["note"]))
        md = render_card(p["date"], p["hhmm"], p)
        if not execute:
            print("\n--- 干跑（未写盘）。预览前 40 行 ---")
            print("\n".join(md.splitlines()[:40]))
            return 0
        out = {"snapshot": None, "csv": None, "report": None}
        jp, cp = write_snapshot(p["date"], p["hhmm"], p["rows"],
                               {k: v for k, v in p.items() if k != "rows"})
        out["snapshot"], out["csv"] = jp, cp
        # ★ 文件名**不得以 e2_ 开头**：`e2_<as_of>.md` 是 registry 登记的 E2 产物路径，
        #   capability_status 按 `e2_*.md` 取最新件判 LIVE/DEGRADED。实测污染：
        #   e2_scan_2026-10-26_1041.md 字符序比 e2_2026-10-26.md 大 ⇒ 被判成
        #   「最新 E2 产物」⇒ 正文无 UNAVAILABLE ⇒ E2 假 LIVE（实际报告写着 UNAVAILABLE）。
        rp = os.path.join(REPORTS, "intraday_scan_%s_%s.md" % (p["date"], p["hhmm"]))
        os.makedirs(os.path.dirname(rp), exist_ok=True)
        tmp = rp + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            f.write(md)
        os.replace(tmp, rp)
        out["report"] = rp
        al = append_alerts(p["date"], p["hhmm"], p.get("discoveries"),
                           p.get("pool"), base=None)
        out["alerts"] = al["path"]
        out["alerts_appended"] = al["appended"]
        print("\n✅ 已写：" + json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
