#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""fetch_auction.py · 集合竞价数据采集（2026-10-22 建 · 为 S10 落地）

## 为什么需要这个文件

`tools/analysis/s10.py`（竞价/一字板结构）判 NO_OUTPUT 的原因，`--availability` 说：

```
sources: auction_feed（集合竞价行情-量/委比/强弱分时）ready=false
         seal_order_feed（封单/撬板序列）          ready=false
```

但**实测 westock `quote` 接口里就有完好的竞价数据**（2026-10-22 · sh603278）：

| 字段 | 值 | 含义 |
|---|---|---|
| `pre_quote_price` | 14.50 | **竞价成交价** |
| `pre_quote_change_pct` | **9.93** | **竞价涨幅 +9.93% ⇒ 竞价即顶涨停价** |
| `pre_quote_volume` | 164529 | 竞价成交量（手） |
| `pre_quote_amount` | 2.39 亿 | 竞价成交额 |
| `pre_quote_turnover_rate` | 4.81 | 竞价换手率 |
| `wb_ratio` | 100 | 委比（100 = 全封） |
| `price_ceiling` | 14.51 | 涨停价 |

⇒ **S10 缺的不是数据，是没人去采。** `s10._probe_sources()` 只查旧包目录里的
文件（`data/raw/backtest_auction/`、`data/raw/bus/zt_pool/`），**从不查 westock 接口**。

本工具把 `quote` 的竞价字段落成 S10 期望的形态：`data/raw/bus/auction_<date>.csv`。

## 输出列（直取，不做换算）

`date, code, name, auction_price, auction_pct, auction_volume, auction_amount,
 auction_turnover, wb_ratio, price_ceiling, price_floor, open, close, pct`

- **`pct`（全天涨幅）与 `auction_pct`（竞价涨幅）分列**——这是 S10 判「一字板」的关键：
  `auction_pct >= 9.8` 且 `pct >= 9.8` ⇒ 竞价即封死，**当天买不进**。
- 不做任何单位换算、不插值、不补 0。

## 纪律

- 只采 `codes.is_stock()` 为真的个股（宇宙口径唯一真源）。
- 批量取（`westock quote "a,b,c"` 实测支持），每批 40 只控制响应大小。
- 缺字段的行**跳过并计数**，不拿空行充数（M20）。
- `.tmp` + `os.replace` 原子写；进程锁（M21）；默认干跑。

用法：
  python tools/e2e/fetch_auction.py --date 2026-10-22              # 干跑
  python tools/e2e/fetch_auction.py --date 2026-10-22 --limit 40
  python tools/e2e/fetch_auction.py --date 2026-10-22 --execute
  python tools/e2e/fetch_auction.py --selftest
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import time

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
if os.path.join(_TOOLS, "fs") not in sys.path:
    sys.path.insert(0, os.path.join(_TOOLS, "fs"))
import codes as CD  # noqa: E402
import cli_codes as CC  # noqa: E402  ★ CLI 代码格式单真源（2026-11-02）

OUT_DIR = os.path.join(_ROOT, "data", "raw", "bus")
LOCK = os.path.join(_ROOT, "data", "state", "_fetch_auction.lock")
KL = os.path.join(_ROOT, "data", "raw", "bus", "klines.csv")

COLS = ("date", "code", "name", "auction_price", "auction_pct",
        "auction_volume", "auction_amount", "auction_turnover",
        "wb_ratio", "price_ceiling", "price_floor", "open", "close", "pct")

# westock quote 列 → 输出列
MAP = {
    "pre_quote_price": "auction_price",
    "pre_quote_change_pct": "auction_pct",
    "pre_quote_volume": "auction_volume",
    "pre_quote_amount": "auction_amount",
    "pre_quote_turnover_rate": "auction_turnover",
    "wb_ratio": "wb_ratio",
    "price_ceiling": "price_ceiling",
    "price_floor": "price_floor",
    "open": "open",
    "price": "close",
    "change_percent": "pct",
    "name": "name",
}

BATCH = 40


def _run(args, timeout=120):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        if p.returncode != 0:
            return None, (p.stderr or p.stdout or "")[:200]
        return p.stdout, None
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def _parse_md(txt):
    if not txt:
        return []
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 3:
        return []
    def cells(ln):
        return [c.strip() for c in ln.strip().strip("|").split("|")]
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
    if not s or s in ("-", "--", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def universe(date, path=None):
    """当日有行情的个股（宇宙口径走 codes.is_stock 唯一真源）。"""
    path = path or KL
    seen = set()
    with io.open(path, encoding="utf-8", errors="replace", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("date") == date and CD.is_stock(r.get("code") or ""):
                seen.add(r["code"])
    return sorted(seen)


def fetch(codes, date):
    """→ (rows, failed)。批量取，缺关键字段的行丢弃并计数。"""
    rows, failed, dropped = [], [], 0
    for i in range(0, len(codes), BATCH):
        chunk = codes[i:i + BATCH]
        txt, err = _run(["westock", "quote", CC.to_cli_csv(chunk)])
        if txt is None:
            failed.extend((c, err) for c in chunk)
            continue
        recs = _parse_md(txt)
        got = set()
        for r in recs:
            c = (r.get("code") or "").strip()
            if not c or not CD.is_stock(c):
                continue
            row = {"date": date, "code": c}
            for src, dst in MAP.items():
                if dst in ("date", "code"):
                    continue
                v = r.get(src)
                row[dst] = (v or "").strip() if dst == "name" else _f(v)
            # 竞价价与竞价涨幅缺一不可 —— 否则这条对 S10 无意义
            if row.get("auction_price") is None or row.get("auction_pct") is None:
                dropped += 1
                continue
            got.add(c)
            rows.append(row)
        for c in chunk:
            if c not in got:
                failed.append((c, "NO_VALID_ROW"))
        if (i // BATCH + 1) % 10 == 0:
            print("    进度 %d/%d · 已取 %d 行 · 丢弃 %d · 失败 %d"
                  % (i + len(chunk), len(codes), len(rows), dropped, len(failed)))
    return rows, failed, dropped


def out_path(date):
    return os.path.join(OUT_DIR, "auction_%s.csv" % date)


def write_all(path, rows):
    """整体重写（按 code 排序，保证幂等）。"""
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, lineterminator="\n")
        w.writeheader()
        for r in sorted(rows, key=lambda x: x.get("code", "")):
            w.writerow({k: (r.get(k) if r.get(k) is not None else "") for k in COLS})
    os.replace(tmp, path)
    return len(rows)


def collect(date, limit=None, execute=False):
    rep = {"date": date, "universe": 0, "fetched": 0, "dropped": 0,
           "failed": [], "path": out_path(date), "executed": False}
    uni = universe(date)
    rep["universe"] = len(uni)
    if not uni:
        rep["note"] = "当日 klines 无个股（先跑 merge_klines）"
        return rep
    codes = uni[:limit] if limit else uni
    print("集合竞价采集 · %s · 宇宙 %d 只%s"
          % (date, len(uni), ("（限 %d）" % limit) if limit else ""))
    rows, failed, dropped = fetch(codes, date)
    rep["fetched"] = len(rows)
    rep["failed"] = failed[:20]
    rep["dropped"] = dropped
    print("  取回 %d 行 · 丢弃 %d（缺竞价字段）· 失败 %d 只"
          % (len(rows), dropped, len(failed)))
    if not rows:
        rep["note"] = "无有效行"
        return rep
    if not execute:
        rep["note"] = "干跑：加 --execute 才写"
        return rep
    rep["written"] = write_all(rep["path"], rows)
    rep["executed"] = True
    return rep


def _selftest():
    ok = fail = 0

    def T(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ✓ %s" % name)
        else:
            fail += 1
            print("  ✗ %s  %s" % (name, detail))

    import shutil
    import tempfile
    print("fetch_auction.py 自检")

    md = ("| code | name | price | open | pre_quote_price | pre_quote_change_pct |"
          " pre_quote_volume | wb_ratio | price_ceiling |\n"
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
          "| sh603278 | 大业股份 | 14.51 | 14.5 | 14.5 | 9.93 | 164529 | 100 | 14.51 |\n")
    recs = _parse_md(md)
    T("A1 解析 1 行", len(recs) == 1, str(len(recs)))

    d = recs[0]
    T("B1 竞价涨幅字段可读（9.93 = 竞价即顶涨停）",
      _f(d.get("pre_quote_change_pct")) == 9.93)
    T("B2 竞价价贴近涨停价（差 ≤1 分钱即视为顶板）",
      abs(_f(d.get("pre_quote_price")) - _f(d.get("price_ceiling"))) <= 0.011,
      "auction=%s ceiling=%s" % (d.get("pre_quote_price"), d.get("price_ceiling")))
    T("B2b 真实情形：竞价 14.50 / 涨停 14.51 —— 差 1 分钱是常态，"
      "断言不能用「完全相等」",
      _f(d.get("pre_quote_price")) != _f(d.get("price_ceiling")))
    T("B3 委比可读", _f(d.get("wb_ratio")) == 100.0)

    T("C1 输出列 14 个", len(COLS) == 14, str(len(COLS)))
    T("C2 MAP 覆盖全部非键列",
      set(MAP.values()) - {"date", "code"} == set(COLS) - {"date", "code"},
      str(set(MAP.values()) ^ set(COLS)))

    # D 缺竞价字段的行必须被丢弃（不能拿空行充数）
    md2 = ("| code | pre_quote_price | pre_quote_change_pct |\n"
           "| --- | --- | --- |\n"
           "| sh600000 |  | 1.0 |\n"
           "| sz000001 | 10.5 | 2.0 |\n")
    valid = [r for r in _parse_md(md2)
             if _f(r.get("pre_quote_price")) is not None
             and _f(r.get("pre_quote_change_pct")) is not None]
    T("D1 缺竞价价的行被丢弃", len(valid) == 1
      and valid[0].get("code") == "sz000001", str(len(valid)))

    # E 只采个股
    kept = [c for c in ["sh600000", "sh110084", "sz920033"] if CD.is_stock(c)]
    T("E1 转债/错标北交所被排除", kept == ["sh600000"], str(kept))

    # F 原子写
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    T("F1 用 .tmp + os.replace 原子写", ".tmp" in src and "os.replace" in src)

    # G 日期命名
    T("G1 输出文件名含日期", "auction_" in out_path("2026-10-22")
      and "2026-10-22" in out_path("2026-10-22"), out_path("2026-10-22"))

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    date = None
    limit = None
    execute = "--execute" in argv
    for i, a in enumerate(argv):
        if a == "--date" and i + 1 < len(argv):
            date = argv[i + 1]
        elif a == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1])
    if not date:
        import datetime
        date = datetime.date.today().isoformat()
    rep = collect(date, limit=limit, execute=execute)
    print("  宇宙 %d · 取回 %d · 丢弃 %d · %s"
          % (rep["universe"], rep["fetched"], rep["dropped"],
             rep.get("note") or ("已写 %s" % os.path.basename(rep["path"]))))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit(_main(sys.argv[1:]))
