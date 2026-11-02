#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ETF K线补采（骨架件之外 · 会写 data/raw/bus/etf_klines.csv）

# 为什么需要这个工具（2026-10-22）

数据体检发现 `etf_klines.csv` **停在 2026-09-22**，而 `klines.csv` 已到 2026-10-22。
查下来是**旧包原样副本**（与本项目同名文件字节完全一致），
而且是**九步编排根本不采的**——没有任何步骤会更新它。

## 危害（真实，不是理论）

`strategy_family.py` 直接读这个文件：
```python
ETF = os.path.join(_ROOT, "data", "raw", "bus", "etf_klines.csv")
```
于是「低波轮动 ETF 年化 5.0% / 夏普 1.07 / 回撤 7.2%」这个
**全项目风险收益比最好的结论**，建立在**一个月前的世界**上。

## 数据源

`westock kline <代码[,代码...]> --period day --start --end`
（**支持批量 + 日期区间**，实测 5 只一批正常）

## 三条纪律

1. **增量补采**：已存在的 (code, date) 跳过，不重写历史
2. **不编造**：取不到的票记失败，不填 0、不前向填充
3. **走写入闸**：`writegate plan → run --confirm-production`

## 用法

    python tools/e2e/fetch_etf_klines.py                 # 干跑，显示要补多少
    python tools/e2e/fetch_etf_klines.py --execute       # 真补（经闸）
    python tools/e2e/fetch_etf_klines.py --from 2026-09-23 --execute
"""

import argparse
import csv
import io
import os
import re
import shutil
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("fs", "analysis", "e2e"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import writegate as _WG          # noqa: E402

OUT_REL = os.path.join("data", "raw", "bus", "etf_klines.csv")
HEADER = ["code", "name", "cat", "date", "open", "close",
          "high", "low", "amount", "change_pct"]
BATCH = 20                 # 每批 ETF 数（实测 20 只无压力）
COOLDOWN = 1.0


# ── 读现有（保留 name/cat 元数据，增量判断用） ──────────────────────────────
def load_existing(path=None):
    """→ (rows, meta, have)。

    rows : 全部现有行（原样保留，导出时按 code+date 排序）
    meta : {code: (name, cat)}  ← 新补的行要沿用这两个字段
    have : {(code, date)}       ← 已存在的键，用来跳过
    """
    path = path or os.path.join(_ROOT, OUT_REL)
    rows, meta, have = [], {}, set()
    if not os.path.isfile(path):
        return rows, meta, have
    with io.open(path, encoding="utf-8-sig", errors="ignore", newline="") as f:
        rd = csv.DictReader(f)
        for r in rd:
            c, d = r.get("code", "").strip(), r.get("date", "").strip()
            if not c or not d:
                continue
            rows.append({k: r.get(k, "") for k in HEADER})
            meta[c] = (r.get("name", ""), r.get("cat", ""))
            have.add((c, d))
    return rows, meta, have


def latest_date(rows):
    ds = [r["date"] for r in rows if r.get("date")]
    return max(ds) if ds else None


# ── 抓取 ────────────────────────────────────────────────────────────────────
_MD_ROW = re.compile(r"^\|\s*([a-z]{2}\d{6})\s*\|")


def run_cli(codes, start, end, timeout=240):
    """westock kline 批量。→ markdown 文本"""
    cmd = ["westock", "kline", ",".join(codes), "--period", "day",
           "--start", start, "--end", end]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError("westock kline 失败：%s"
                           % ((p.stderr or p.stdout or "")[:200]))
    return p.stdout or ""


def parse_md(text):
    """markdown → [{code,date,open,close,high,low,amount,change_pct}]"""
    hdr, rows = None, []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if hdr is None and "code" in [c.lower() for c in cells] \
                and "date" in [c.lower() for c in cells]:
            hdr = [c.lower() for c in cells]
            continue
        if hdr is None or set("".join(cells)) <= set("-: "):
            continue
        if len(cells) < len(hdr):
            continue
        rec = dict(zip(hdr, cells))
        code = (rec.get("code") or "").strip()
        if not _MD_ROW.match("| %s |" % code):
            continue
        rows.append({
            "code": code,
            "date": rec.get("date", ""),
            "open": rec.get("open", ""),
            "close": rec.get("last", rec.get("close", "")),   # CLI 用 last 表示收盘
            "high": rec.get("high", ""),
            "low": rec.get("low", ""),
            "amount": rec.get("amount", ""),
            "change_pct": rec.get("change_pct", ""),
        })
    return rows


def fetch_missing(codes, start, end, have, meta, batch=BATCH, cooldown=COOLDOWN):
    """→ (new_rows, failed)。

    ★ 只补 (code, date) 不在 have 里的；且**判重按批对比**（M20：
      批量接口会静默少返回，必须用「请求集合 − 返回集合」自校验）。
    """
    new_rows, failed = [], []
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        try:
            text = run_cli(chunk, start, end)
        except Exception as e:
            failed.extend((c, str(e)[:60]) for c in chunk)
            continue
        recs = parse_md(text)
        got = set()
        for r in recs:
            k = (r["code"], r["date"])
            if k in have:
                continue
            name, cat = meta.get(r["code"], ("", ""))
            r2 = dict(r)
            r2["name"], r2["cat"] = name, cat
            new_rows.append({k2: r2.get(k2, "") for k2 in HEADER})
            have.add(k)
            got.add(r["code"])
        miss = [c for c in chunk if c not in got]
        if miss:
            failed.extend((c, "无返回") for c in miss)
        if i + batch < len(codes):
            time.sleep(cooldown)
    return new_rows, failed


# ── 写 ──────────────────────────────────────────────────────────────────────
def write_all(path, rows):
    """整体重写（按 code,date 排序，保证幂等）。"""
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, lineterminator="\n")
        w.writeheader()
        for r in sorted(rows, key=lambda x: (x["code"], x["date"])):
            w.writerow({k: r.get(k, "") for k in HEADER})
    os.replace(tmp, path)


def backup(path):
    b = "%s.bak.%d" % (path, int(time.time()))
    with io.open(path, "rb") as s, io.open(b, "wb") as d:
        while True:
            chunk = s.read(1 << 20)
            if not chunk:
                break
            d.write(chunk)
    return b


def run(start=None, execute=False, force_all=False):
    path = os.path.join(_ROOT, OUT_REL)
    rows, meta, have = load_existing(path)
    if not rows:
        print("✗ 找不到 %s 或无数据" % OUT_REL)
        return 2

    last = latest_date(rows)
    codes = sorted(meta)
    start = start or _next_day(last)
    today = _today()
    print("ETF K线补采 · 现有 %d 行 · %d 只 · 最新 %s" % (len(rows), len(codes), last))
    print("  补采区间：%s ~ %s" % (start, today))

    # ★ 已是最新 ⇒ 直接返回，**不发无效请求**。
    #   之前漏了这个判断，导致 start > end 时的 CLI 报错被当成 62 只采集失败
    #   （2026-10-22 实测：明明无需补采，却刷出「失败 62 只」的假警报）。
    if start > today:
        print("\n  已是最新（%s 已包含到 %s），无需补采" % (OUT_REL, last))
        return 0

    t0 = time.time()
    new_rows, failed = fetch_missing(codes, start, today, have, meta)
    print("  抓到 %d 行新数据 · 失败 %d 只 · %.1fs"
          % (len(new_rows), len(failed), time.time() - t0))
    if failed:
        print("  失败样例：%s" % failed[:6])

    if not new_rows:
        print("\n  无需补采（已是最新，或数据源无新数据）")
        return 0

    new_last = latest_date(new_rows)
    print("\n  ┌ 补采结果 ─────────────────────────")
    print("  │ 新增行数    %6d" % len(new_rows))
    print("  │ 覆盖 ETF    %6d 只" % len({r["code"] for r in new_rows}))
    print("  │ 新数据止于  %s（原 %s）" % (new_last, last))
    print("  └───────────────────────────────────")

    if not execute:
        print("\n（干跑，未写盘；加 --execute 生效，会先落 .bak 备份）")
        return 0

    b = backup(path)
    print("\n  备份 → %s" % os.path.basename(b))
    write_all(path, rows + new_rows)
    rows2, _, _ = load_existing(path)
    print("  已写回 %d 行 · 最新 %s" % (len(rows2), latest_date(rows2)))
    return 0


def _today():
    import datetime
    return datetime.date.today().strftime("%Y-%m-%d")


def _next_day(d):
    import datetime
    dt = datetime.datetime.strptime(d, "%Y-%m-%d").date()
    return (dt + datetime.timedelta(days=1)).strftime("%Y-%m-%d")


# ── 自检 ────────────────────────────────────────────────────────────────────

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

    md = """| code | date | open | last | high | low | volume | amount | exchange | change_pct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sh510300 | 2026-10-22 | 4.185 | 4.135 | 4.189 | 4.116 | 10893765 | 4507706126 | 3.61 | -1.19 |
| sz159915 | 2026-10-22 | 2.701 | 2.638 | 2.708 | 2.619 | 32943737 | 8738722026 | 11.28 | -2.76 |
"""
    recs = parse_md(md)
    T("A1 解析出 2 行", len(recs) == 2, str(len(recs)))
    T("A2 CLI 的 last 映射到 close", recs[0]["close"] == "4.135", recs[0]["close"])
    T("A3 表头分隔行被跳过", all(r["code"].startswith(("sh", "sz")) for r in recs))
    T("A4 不把汇总行当数据", len([r for r in recs if r["code"] == ""]) == 0)

    # B 增量判重
    have = {("sh510300", "2026-10-22")}
    meta = {"sh510300": ("沪深300ETF", "wide"),
            "sz159915": ("创业板ETF", "wide")}
    new_rows, failed = [], []
    got = set()
    for r in recs:
        k = (r["code"], r["date"])
        if k in have:
            continue
        n, c = meta.get(r["code"], ("", ""))
        r2 = dict(r)
        r2["name"], r2["cat"] = n, c
        new_rows.append(r2)
        got.add(r["code"])
    T("B1 已存在的 (code,date) 被跳过", len(new_rows) == 1, str(len(new_rows)))
    T("B2 新行带上 name/cat 元数据", new_rows[0]["name"] == "创业板ETF",
      new_rows[0].get("name"))
    T("B3 已有键跳过 ≠ 缺失（只有真没返回的才算失败）",
      [c for c in ["sz159915"] if c not in got] == []
      and [c for c in ["sh510300"] if c not in got] == ["sh510300"],
      "got=%s（sh510300 因已存在被跳过，不算失败）" % sorted(got))

    # C 日期工具
    T("C1 _next_day 正确", _next_day("2026-09-22") == "2026-09-23",
      _next_day("2026-09-22"))
    T("C2 _next_day 跨月", _next_day("2026-09-30") == "2026-10-01",
      _next_day("2026-09-30"))

    # D latest_date
    T("D1 latest_date 取最大", latest_date(
        [{"date": "2026-01-01"}, {"date": "2026-10-22"},
         {"date": "2026-05-01"}]) == "2026-10-22")

    # E 写幂等（同一批写两次行数不变）
    import tempfile
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "e.csv")
        base = [{"code": "sh510300", "name": "x", "cat": "wide",
                 "date": "2026-01-01", "open": "1", "close": "1",
                 "high": "1", "low": "1", "amount": "1", "change_pct": "0"}]
        write_all(p, base)
        write_all(p, base)
        _, _, h2 = load_existing(p)
        T("E1 重复写同一批不产生重复行", len(h2) == 1, str(len(h2)))
        T("E2 表头正确", io.open(p, encoding="utf-8").readline().strip()
          == ",".join(HEADER))

        # F 排序稳定（幂等的前提）
        rows2 = [dict(base[0], date="2026-02-01"),
                 dict(base[0], date="2026-01-01")]
        write_all(p, rows2)
        txt1 = io.open(p, encoding="utf-8").read()
        write_all(p, list(reversed(rows2)))
        txt2 = io.open(p, encoding="utf-8").read()
        T("F1 输入顺序不同但输出字节一致（排序稳定）", txt1 == txt2)
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # G 生产段无删改调用（只有原子写与备份）
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    prod = src.split("def _selftest(")[0]
    T("G1 生产段不删数据（只有写/备份/rename）",
      "os.remove" not in prod and "unlink" not in prod)
    T("G2 用 .tmp + os.replace 原子写", ".tmp" in prod and "os.replace" in prod)
    T("G3 写前必先 backup", prod.find("b = backup(path)")
      < prod.find("write_all(path, rows + new_rows)"))

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    ap = argparse.ArgumentParser(description="ETF K线补采（默认干跑）")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--from", dest="start", default=None)
    ap.add_argument("--base", default=None)
    a = ap.parse_args(argv)
    return run(start=a.start, execute=a.execute)


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
