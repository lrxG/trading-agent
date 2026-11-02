# -*- coding: utf-8 -*-
"""backfill_klines_tq.py · 用通达信 TQ 补全历史 K 线（2026-10-21 建）。

## 为什么需要

`data/raw/bus/klines.csv` 覆盖度极不均匀（2026-10-21 实测）：

| 区间 | 每月行数 | 判定 |
|---|---|---|
| 2024-01 ~ 2025-08 | 300~440 行/月 | **残缺**（仅几十只票，非全市场）|
| 2025-09 ~ 2026-10 | 72k~119k 行/月 | 完整 |

⇒ 有效回测样本只有 13 个月。策略验证的样本量因此不足（`oversold` 只有 950 笔、
日级样本仅 90 天）。本件用通达信本地 K 线库补 2024-01~2025-08。

## 数据源

通达信 TQ 本地服务（`tq_bridge.py`，只读，**禁调任何交易接口**）：
`get_market_data(stock_list, period='1d', start_time, end_time, dividend_type)`。
通达信返回 `Value: {code: {Date/Open/High/Low/Close/Volume/Amount/ForwardFactor}}`。

## 口径（与既有 klines.csv 逐列对齐）

| klines.csv 列 | TQ 字段 | 换算 |
|---|---|---|
| code | key（`600000.SH`） | 转契约格式 `sh600000` |
| name | — | 从现有 klines / 当日 quote 取；取不到留空 |
| date | `Date`（`20240102`） | 转 `2024-01-02` |
| open/high/low/close | 同名字段（字符串） | float |
| volume | `Volume`（股） | float |
| amount | `Amount`（**千元**） | ×1000 转元（通达信 Amount 单位=千元，实测校验） |
| exchange | — | 空串（S 系不读） |
| change_pct | 由 close 与前一日 close 算 | **优先用 TQ 自带**；无则自算 |

## 纪律

- **只补缺口**：已有 (code,date) 一律不覆盖（幂等，按 `(code,date)` 去重）。
- **不猜**：取不到的日期/字段留空或跳过，不插值、不外推。
- **写点唯一**：`data/raw/bus/klines.csv`（append + 原子替换）。
- **可回滚**：写前留 `data/research/obs/klines_backup_<ts>.csv.gz` 快照。

## 用法

```bash
python tools/e2e/backfill_klines_tq.py --check                     # 通道+覆盖度（只读）
python tools/e2e/backfill_klines_tq.py --from 2024-01-02 --to 2025-08-29 --dry-run
python tools/e2e/backfill_klines_tq.py --from 2024-01-02 --to 2025-08-29 --execute
```
"""
import argparse
import csv
import gzip
import io
import json
import os
import shutil
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("fs", "analysis", "decision", "risk"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

from paths import ROOT  # noqa: E402
import tq_bridge as TQ  # noqa: E402

BUS = os.path.join("data", "raw", "bus", "klines.csv")
COLS = ("code,name,date,open,close,high,low,volume,amount,exchange,"
        "change_pct").split(",")
BATCH = 80          # 每批股票数（TQ 单次请求上限实测 ~100，留余量）


class BfError(Exception):
    """REASON_CODE 前缀错误。"""


def to_tq_code(c):
    """契约格式 sh600000 → TQ 格式 600000.SH。"""
    c = (c or "").lower()
    if c.startswith("sh"):
        return c[2:] + ".SH"
    if c.startswith("sz"):
        return c[2:] + ".SZ"
    if c.startswith("bj"):
        return c[2:] + ".BJ"
    return c


def from_tq_code(c):
    """600000.SH → sh600000。"""
    if "." not in c:
        return c
    num, mk = c.split(".", 1)
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(mk.upper(), "") + num


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_existing(root=None):
    """→ (rows, keys)。rows 是全部现有行（保序），keys 是 {(code,date)}。"""
    p = os.path.join(root or ROOT, BUS)
    if not os.path.isfile(p):
        return [], set()
    rows, keys = [], set()
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append(r)
            keys.add((r.get("code"), r.get("date")))
    return rows, keys


def coverage(root=None, d1=None, d2=None):
    """逐月行数（判断残缺）。"""
    p = os.path.join(root or ROOT, BUS)
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
    bad = {m: n for m, n in cnt.items() if n < 5000}
    return {"months": len(cnt), "rows": sum(cnt.values()), "bad_months": bad}


def pick_universe(root=None, sample=0):
    """要补的股票池 → list[契约 code]。

    用现有 klines 里出现过的全部 A 股代码（**不自造新宇宙**）；
    `sample>0` 时随机取该数量（试跑用）。
    """
    p = os.path.join(root or ROOT, BUS)
    codes = set()
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            c = r.get("code") or ""
            if len(c) == 8 and c[:2] in ("sh", "sz", "bj") and c[2:].isdigit():
                codes.add(c)
    out = sorted(codes)
    if sample and sample < len(out):
        import random
        random.Random(42).shuffle(out)
        out = sorted(out[:sample])
    return out


def fetch_klines(codes, d1, d2, timeout=300):
    """批量取 K 线 → list[dict]（契约列格式）。失败抛错，不静默。"""
    tq_codes = [to_tq_code(c) for c in codes]
    r = TQ.call("get_market_data", {
        "stock_list": tq_codes, "period": "1d",
        "start_time": d1.replace("-", ""), "end_time": d2.replace("-", ""),
        "dividend_type": "none"}, timeout=timeout)
    val = (r or {}).get("Value") or {}
    out = []
    for tq_code, d in val.items():
        code = from_tq_code(tq_code)
        dates = d.get("Date") or []
        op = d.get("Open") or []
        hi = d.get("High") or []
        lo = d.get("Low") or []
        cl = d.get("Close") or []
        vo = d.get("Volume") or []
        am = d.get("Amount") or []
        n = min(len(dates), len(op), len(hi), len(lo), len(cl))
        prev_close = None
        for i in range(n):
            dt = dates[i]
            if len(dt) != 8:
                continue
            iso = "%s-%s-%s" % (dt[:4], dt[4:6], dt[6:8])
            c_close = _f(cl[i])
            if c_close is None or c_close <= 0:
                continue
            pct = None
            if prev_close and prev_close > 0:
                pct = round((c_close / prev_close - 1) * 100, 4)
            prev_close = c_close
            amt = _f(am[i]) if i < len(am) else None
            out.append({
                "code": code, "name": "",
                "date": iso,
                "open": _f(op[i]), "close": c_close,
                "high": _f(hi[i]), "low": _f(lo[i]),
                "volume": _f(vo[i]) if i < len(vo) else None,
                # 通达信 Amount 单位 = 千元 ⇒ ×1000 转元（与 klines.csv 对齐）
                "amount": (amt * 1000.0) if amt is not None else None,
                "exchange": "",
                "change_pct": pct,
            })
    return out


def merge(rows, keys, new_rows, name_map=None):
    """把新行并入（跳过已存在 (code,date)）。→ (rows, n_added, n_skip)。"""
    added = skip = 0
    for r in new_rows:
        k = (r["code"], r["date"])
        if k in keys:
            skip += 1
            continue
        if name_map and not r.get("name"):
            r["name"] = name_map.get(r["code"], "")
        rows.append(r)
        keys.add(k)
        added += 1
    return rows, added, skip


def write_out(rows, root=None, backup=True):
    """原子写回 klines.csv（按 date,code 排序）。"""
    p = os.path.join(root or ROOT, BUS)
    if backup:
        bdir = os.path.join(root or ROOT, "data", "research", "obs")
        os.makedirs(bdir, exist_ok=True)
        ts = time.strftime("%Y%m%dT%H%M%S")
        bp = os.path.join(bdir, "klines_backup_%s.csv.gz" % ts)
        with io.open(p, "rb") as fin, gzip.open(bp, "wb") as fout:
            shutil.copyfileobj(fin, fout)
    rows.sort(key=lambda r: (r.get("date") or "", r.get("code") or ""))
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (r.get(k) if r.get(k) is not None else "") for k in COLS})
    os.replace(tmp, p)
    return p


def build_name_map(rows):
    m = {}
    for r in rows:
        c, n = r.get("code"), r.get("name")
        if c and n and c not in m:
            m[c] = n
    return m


def run(d1, d2, dry_run=True, sample=0, root=None, chunk_sleep=1.0):
    root = root or ROOT
    if not TQ.port_open():
        raise BfError("SERVICE_DOWN: 通达信 TQ 服务未就绪（%d 端口）——"
                      "请先启动通达信客户端" % TQ.TQ_PORT)
    rows, keys = load_existing(root)
    names = build_name_map(rows)
    codes = pick_universe(root, sample=sample)
    if not codes:
        raise BfError("NO_UNIVERSE: 现有 klines 无可用 A 股代码")
    print("股票池 %d 只 · 目标区间 %s ~ %s" % (len(codes), d1, d2))
    before = coverage(root, d1, d2)
    print("补前覆盖：%d 月 · %d 行 · 残缺 %d 月"
          % (before["months"], before["rows"], len(before["bad_months"])))

    total_new = total_skip = 0
    for i in range(0, len(codes), BATCH):
        chunk = codes[i:i + BATCH]
        try:
            nr = fetch_klines(chunk, d1, d2)
        except Exception as e:                                  # noqa: BLE001
            print("  ⛔ 批次 %d-%d 失败：%s —— 跳过该批（不补零）"
                  % (i + 1, i + len(chunk), str(e)[:120]))
            continue
        rows, a, s = merge(rows, keys, nr, name_map=names)
        total_new += a
        total_skip += s
        print("  批次 %d/%d：取 %d 行 → 新增 %d 跳过 %d"
              % (i // BATCH + 1, (len(codes) + BATCH - 1) // BATCH,
                 len(nr), a, s))
        if i + BATCH < len(codes):
            time.sleep(chunk_sleep)

    print("\n合计新增 %d 行 · 跳过(已存在) %d 行" % (total_new, total_skip))
    if dry_run:
        print("（dry-run：未写盘）")
        return {"new": total_new, "skip": total_skip, "written": False}
    p = write_out(rows, root)
    after = coverage(root, d1, d2)
    print("→ 已写回 %s" % p)
    print("补后覆盖：%d 月 · %d 行 · 残缺 %d 月"
          % (after["months"], after["rows"], len(after["bad_months"])))
    return {"new": total_new, "skip": total_skip, "written": True, "path": p}


def _selftest():
    """自检（合成夹具 + 打桩，真库零接触）。"""
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

    # A 代码格式双向转换
    for c, t in (("sh600000", "600000.SH"), ("sz000001", "000001.SZ"),
                 ("bj920346", "920346.BJ")):
        T("A 契约→TQ %s" % c, to_tq_code(c) == t, to_tq_code(c))
        T("A 反解 TQ→契约 %s" % t, from_tq_code(t) == c, from_tq_code(t))

    sb = tempfile.mkdtemp(prefix="bfk_")
    try:
        os.makedirs(os.path.join(sb, "data", "raw", "bus"))
        kp = os.path.join(sb, "data", "raw", "bus", "klines.csv")
        with io.open(kp, "w", encoding="utf-8", newline="") as f:
            f.write(",".join(COLS) + "\n")
            f.write("sh600000,浦发银行,2024-01-02,6.63,6.60,6.65,6.60,"
                    "22066700,14606630,,0\n")
            f.write("sh600000,浦发银行,2024-01-03,6.59,6.64,6.65,6.59,"
                    "18203654,12063970,,0.61\n")

        # B 读现有
        rows, keys = load_existing(sb)
        T("B1 读到 2 行", len(rows) == 2, str(len(rows)))
        T("B2 key 集合正确",
          ("sh600000", "2024-01-02") in keys and len(keys) == 2)

        # C 合并去重（关键：不覆盖已有）
        new = [
            {"code": "sh600000", "date": "2024-01-02", "close": 99.0,
             "name": "", "open": 1, "high": 1, "low": 1, "volume": 1,
             "amount": 1, "exchange": "", "change_pct": 0},   # 已存在 ⇒ 跳过
            {"code": "sh600000", "date": "2024-01-04", "close": 6.62,
             "name": "", "open": 6.64, "high": 6.67, "low": 6.55, "volume": 1,
             "amount": 1, "exchange": "", "change_pct": -0.3},  # 新 ⇒ 并入
        ]
        rows2, a, s = merge(rows, keys, new, name_map={"sh600000": "浦发银行"})
        T("C1 新增 1 · 跳过 1", a == 1 and s == 1, "a=%d s=%d" % (a, s))
        T("C2 已有行未被覆盖（close 仍是 6.6 不是 99）",
          [r for r in rows2 if r["date"] == "2024-01-02"][0]["close"] in
          ("6.60", 6.6), "被覆盖了")
        T("C3 新行补了 name", 
          [r for r in rows2 if r["date"] == "2024-01-04"][0]["name"] == "浦发银行")

        # D 写回保序 + 备份
        p = write_out(rows2, sb, backup=True)
        T("D1 写回成功", os.path.isfile(p))
        rows3, keys3 = load_existing(sb)
        T("D2 行数 = 3", len(rows3) == 3, str(len(rows3)))
        T("D3 按 date,code 排序",
          [r["date"] for r in rows3] == sorted(r["date"] for r in rows3))
        bdir = os.path.join(sb, "data", "research", "obs")
        T("D4 留了备份", os.path.isdir(bdir) and
          any(f.startswith("klines_backup_") for f in os.listdir(bdir)))

        # E 覆盖度统计
        cov = coverage(sb, "2024-01-01", "2024-01-31")
        T("E1 覆盖度识别残缺月", "2024-01" in cov["bad_months"],
          str(cov["bad_months"].keys()))

        # F 股票池（只取真 A 股代码）
        T("F1 池含 sh600000", "sh600000" in pick_universe(sb))
        T("F2 池无非法代码",
          all(c[:2] in ("sh", "sz", "bj") and c[2:].isdigit()
              for c in pick_universe(sb)))

        # G 服务未起 ⇒ 明确报错（不静默）
        if not TQ.port_open():
            try:
                run("2024-01-02", "2024-01-05", dry_run=True, root=sb)
                T("G 服务未起报 SERVICE_DOWN", False)
            except BfError as e:
                T("G 服务未起报 SERVICE_DOWN", "SERVICE_DOWN" in str(e))
        else:
            T("G 服务未起报 SERVICE_DOWN（服务在跑，跳过）", True)

        # H amount 单位换算（千元 → 元）
        import unittest.mock as mock
        fake = {"Value": {"600000.SH": {
            "Date": ["20240102"], "Open": ["6.63"], "High": ["6.65"],
            "Low": ["6.60"], "Close": ["6.60"], "Volume": ["22066700"],
            "Amount": ["14606.63"]}}}
        with mock.patch.object(TQ, "call", return_value=fake):
            got = fetch_klines(["sh600000"], "2024-01-02", "2024-01-02")
        T("H1 解析出 1 行", len(got) == 1, str(len(got)))
        if got:
            T("H2 date 转 ISO 格式", got[0]["date"] == "2024-01-02",
              got[0]["date"])
            T("H3 amount 千元→元（×1000）",
              abs((got[0]["amount"] or 0) - 14606630.0) < 1.0,
              str(got[0]["amount"]))
            T("H4 code 转契约格式", got[0]["code"] == "sh600000", got[0]["code"])
    finally:
        shutil.rmtree(sb, ignore_errors=True)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：%s" % "；".join(fails))
    return 0 if fail == 0 else 1


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    if "--check" in argv:
        st = TQ.check_status()
        st["coverage"] = coverage()
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return 0 if st["usable"] else 2
    ap = argparse.ArgumentParser(description="用通达信补历史 K 线")
    ap.add_argument("--from", dest="d1", required=True)
    ap.add_argument("--to", dest="d2", required=True)
    ap.add_argument("--sample", type=int, default=0, help="只取 N 只票试跑")
    ap.add_argument("--base", default=None)
    ap.add_argument("--execute", action="store_true", help="真写盘（默认 dry-run）")
    a = ap.parse_args(argv)
    r = run(a.d1, a.d2, dry_run=not a.execute, sample=a.sample, root=a.base)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
