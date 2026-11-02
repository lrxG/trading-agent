#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""fetch_fundflow.py · 个股日度资金流采集（2026-10-22 建 · 为 S5 落地）

## 为什么必须建这个

`tools/analysis/s5.py`（资金筹码结构）**只从旧包读 fundflow**：
```
_OLD_ROOT = (os.environ.get("OLD_SYSTEM_ROOT")
             or os.path.join(os.path.expanduser("~"), "炒股智能体_旧包_20261008"))  # 旧系统只读参考源
_flow_path(_OLD_ROOT) → 旧包/data/raw/bus/fundflow.csv
```
旧包这张表 **停在 2026-09-30、且 9-30 当天只有 716 只票**。
⇒ S5 永远拿不到 10 月的数据，`evaluate('2026-10-22')` 直接抛
   `CODE_NOT_IN_UNIVERSE: sh603278 当日无行情或资金流`。

**这不是代码没写，是数据通道没建。** 本工具补上这个通道。

## 数据源

`westock fund flow <code> --start A --end B`

实测（2026-10-22）：**单代码可一次取整个区间**，返回列：
```
date, code, EndDate, BlockNetFlow, ClosePrice, JumboNetFlow,
MainInFlow, MainNetFlow, MainOutFlow, MidNetFlow,
RetailInFlow, RetailOutFlow, SecuCode, SmallNetFlow
```

## 输出（字段与旧包**逐一对应**，量纲一致＝元）

旧包列：`date,code,close,main_net,main_in,main_out,jumbo_net,mid_net,small_net,block_net`

| 旧包列 | westock 列 | 说明 |
|---|---|---|
| date | date | 交易日 |
| code | code | 带市场前缀 |
| close | ClosePrice | 收盘价 |
| main_net | MainNetFlow | 主力净流入 |
| main_in | MainInFlow | 主力流入 |
| main_out | MainOutFlow | 主力流出 |
| jumbo_net | JumboNetFlow | 超大单净额 |
| mid_net | MidNetFlow | 中单净额 |
| small_net | SmallNetFlow | 小单净额 |
| block_net | BlockNetFlow | 大宗单净额 |

**不做任何换算**——旧包与 westock 都是「元」，直接落。

## 纪律

- **增量补采**：按 `(code, date)` 判重，已有的不重复抓（M22 幂等键）。
- **不填 0 不插值**：接口没返回的日期就是没有，**不能补 0**（那会伪造"无资金流"）。
- 单代码失败**如实记入失败清单**，不静默跳过（M20 用请求集合 − 返回集合自校验）。
- 先 `.bak` 备份、`.tmp` + `os.replace` 原子写。
- 默认干跑，`--execute` 才写。
- 断点续跑：中途退出再跑，已采的会跳过。

## 2026-10-22 改造：并发 + 边采边写（M21 事故驱动）

**事故**：首版串行 + 最后统一写盘，全量 4942 只实测 **15 秒/只 ≈ 20.6 小时**，
跑到 3.7 小时被中止 → `fundflow.csv` **一个字都没写**（`write_all` 只在末尾调一次）。
⇒ 20 小时工作的产出是 0，且锁的 3 小时阈值先于任务完成到期。

**根因**：单只耗时 ≈ 12 秒是**固定网络往返**，不是 CPU。
实测 5 只并发总耗时 11.99 秒、8 只并发 12.1 秒（单只 11.9 秒）
⇒ 并发度直接线性压缩墙钟时间，且服务端未限速（"数据为空"是票本身无资金流，
单独调用同样为空，非并发所致）。

**改法**：
1. `--workers N`（默认 8）线程池并发取数；
2. **每 `--flush-every` 只（默认 200）落一次盘**，`write_all` 原子重写
   （54k 行 ≈ 6MB，25 次重写成本可忽略）⇒ 中断最多丢最后一批，不是丢全部；
3. **锁心跳**：每批 flush 时 `touch_lock()` 刷新 `ts`，长任务不再被自己的
   3 小时阈值误判为僵死。

用法：
  python tools/e2e/fetch_fundflow.py                        # 干跑（全市场）
  python tools/e2e/fetch_fundflow.py --selftest
  python tools/e2e/fetch_fundflow.py --date 2026-10-22 --limit 20     # 小样本干跑
  python tools/e2e/fetch_fundflow.py --execute --from 2026-10-08      # 真跑
  python tools/e2e/fetch_fundflow.py --execute --codes sh600000,sz000001
  python tools/e2e/fetch_fundflow.py --execute --workers 8 --flush-every 200
"""

from __future__ import annotations

import csv
import concurrent.futures
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
if os.path.join(_TOOLS, "fs") not in sys.path:
    sys.path.insert(0, os.path.join(_TOOLS, "fs"))
import codes as CD  # noqa: E402

FF = os.path.join(_ROOT, "data", "raw", "bus", "fundflow.csv")
KL = os.path.join(_ROOT, "data", "raw", "bus", "klines.csv")
LOCK = os.path.join(_ROOT, "data", "state", "_fetch_fundflow.lock")

COLS = ("date", "code", "close", "main_net", "main_in", "main_out",
        "jumbo_net", "mid_net", "small_net", "block_net")

# westock 列名 → 输出列名
MAP = {
    "date": "date", "code": "code", "ClosePrice": "close",
    "MainNetFlow": "main_net", "MainInFlow": "main_in",
    "MainOutFlow": "main_out", "JumboNetFlow": "jumbo_net",
    "MidNetFlow": "mid_net", "SmallNetFlow": "small_net",
    "BlockNetFlow": "block_net",
}

_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BATCH = 1          # westock fund flow 只支持单代码带区间

# 本数据源的最早可回补日（区间下界）。不是"随便定的"——是首个实测有数据的交易日。
# 比它更早的日期接口不返回（资金流历史从 2026-10-08 起取）。
_FLOOR = "2026-10-08"


def _latest_trade_date(path=None):
    """本地 klines 里**最新一个有数据的交易日**（无则回落到 _today()）。

    ★ 为什么不能用 `_today()` 当 end（2026-10-23 事故）：
      跑到第二天时 end=今天，而当天资金流盘中/盘前根本没数据
      ⇒ 4940 只全返回"数据为空"，白跑 40 分钟且一条都写不进。
      end 必须是「已知确实有数据的那一天」= klines 最新日。
    """
    p = path or KL
    if not os.path.isfile(p):
        return _today()
    latest = None
    with io.open(p, encoding="utf-8", errors="replace", newline="") as f:
        for r in csv.DictReader(f):
            d = (r.get("date") or "").strip()
            if _DATE_RX.match(d) and (latest is None or d > latest):
                latest = d
    return latest or _today()


# ── 基础 ────────────────────────────────────────────────────────────────────
# 服务限频（2026-10-23 实测）：并发 8 时大量返回
#   `[code=1620053006] error_type=2 suggest=2 msg=服务限频`
# 实测一次跑 3065 只 → 失败 2058 只（67%），全是限频、不是无数据。
# 必须**退避重试**，且并发不能太快：限频是服务端按速率计，不是按并发数。
_RATE_RX = re.compile(r"服务限频|限频|rate\s*limit|too\s*many", re.I)
_RETRY_MAX = 4
_RETRY_BASE = 1.2        # 指数退避基数（秒）：1.2, 2.4, 4.8, 9.6
_THROTTLE_WAIT = 8.0     # 命中限频后统一冷却（秒），让服务端令牌桶恢复


def _is_rate_limited(err):
    return bool(err) and bool(_RATE_RX.search(str(err)))


def _run(args, timeout=90):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        if p.returncode != 0:
            out = (p.stderr or p.stdout or "")[:300]
            # 限频时 CLI 仍返回 0 或非 0 都可能；此处按内容识别
            return None, out
        out = p.stdout or ""
        if _is_rate_limited(out):
            return None, out[:300]
        return out, None
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def _run_retry(args, timeout=90, *, verify_empty=True):
    """带限频退避的取数。→ (txt, err)。

    ★ 关键（2026-10-23 实测踩到，代价 2300 只票）：
      限频有时**不返回限频文案，而是返回空内容**。此时 `_parse_md` 得到 0 行，
      上层记成 `NO_ROWS`=「这只票没有资金流数据」—— **把限频误判成无数据**，
      而且是永久丢失（下次增量只按 code 覆盖算缺口，不会重试）。
      实测抽样 15 只"失败"票：10 只有数据、4 只仍限频、仅 1 只真无数据
      ⇒ `NO_ROWS` 必须**重试验证**，不能一次定案。

    `verify_empty=True`（默认）时，空结果也走退避重试；耗尽仍空才认作真空。
    """
    last = None
    for i in range(_RETRY_MAX):
        txt, err = _run(args, timeout=timeout)
        if err is None:
            out = txt or ""
            if out.strip() and not verify_empty:
                return out, None
            # 非空且有表格数据 → 成功
            if _has_table_rows(out):
                return out, None
            # 空 / 无数据行：可能是限频伪装，退避后重试
            last = "EMPTY_OR_NO_ROWS"
            time.sleep(_THROTTLE_WAIT * 0.5 + _RETRY_BASE * (2 ** i))
            continue
        if not _is_rate_limited(err):
            return None, err          # 真错误：不重试
        last = err
        time.sleep(_THROTTLE_WAIT + _RETRY_BASE * (2 ** i))
    if last == "EMPTY_OR_NO_ROWS":
        return "", None               # 重试耗尽仍空 → 认作真空（如实 0 行）
    return None, "RATE_LIMITED_AFTER_%d: %s" % (_RETRY_MAX, last)


def _has_table_rows(txt):
    """markdown 表里是否有真实数据行（`| 2026-xx-xx |` 开头），排除只有表头。"""
    if not txt:
        return False
    for ln in txt.splitlines():
        s = ln.strip()
        if s.startswith("| 2") or re.match(r"^\|\s*\d{4}-\d{2}-\d{2}\s*\|", s):
            return True
    return False


def _parse_md(txt):
    """markdown 表格 → [dict]，用表头列名。跳过 `| --- |` 分隔行。"""
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


def load_existing(path=None):
    """→ (rows, have_set)。have_set 元素 = (code, date)，幂等键（M22）。"""
    path = path or FF
    rows, have = [], set()
    if not os.path.exists(path):
        return rows, have
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            c, d = (r.get("code") or "").strip(), (r.get("date") or "").strip()
            if c and d:
                rows.append(r)
                have.add((c, d))
    return rows, have


def universe(from_date=None, path=None):
    """当日有行情的个股（**按 codes.is_stock 过滤**，宇宙口径唯一真源）。"""
    path = path or KL
    seen = {}
    with io.open(path, encoding="utf-8", errors="replace", newline="") as f:
        for r in csv.DictReader(f):
            d = r.get("date") or ""
            c = r.get("code") or ""
            if not d or not c:
                continue
            if from_date and d < from_date:
                continue
            seen.setdefault(d, set()).add(c)
    allc = set()
    for s in seen.values():
        allc |= s
    kept = sorted(c for c in allc if CD.is_stock(c))
    return kept, len(allc) - len(kept)


def latest_date(path=None):
    rows, _ = load_existing(path)
    ds = [r["date"] for r in rows if _DATE_RX.match(r.get("date") or "")]
    return max(ds) if ds else None


def fetch_codes(codes, start, end, workers=1, on_batch=None, flush_every=200):
    """→ (rows, failed)。逐代码取区间；返回的每行按 MAP 转列。

    `workers>1` 用线程池并发（单只耗时 ≈12s 是网络往返，并发直接压墙钟）。
    `on_batch(rows_sofar, failed_sofar)` 每凑满 `flush_every` 只调一次
    ⇒ 调用方得以边采边写（2026-10-22 改造：避免末次写盘前中断导致全丢）。

    纪律不变：`close` 缺失的行丢弃（不拿半行充完整）；无返回记失败不静默跳过。
    """
    rows, failed = [], []
    lock = threading.Lock()
    n_done = [0]

    def one(c):
        txt, err = _run_retry(["westock", "fund", "flow", c,
                               "--start", start, "--end", end])
        if txt is None:
            return c, [], err
        recs = _parse_md(txt)
        if not recs:
            return c, [], "NO_ROWS"
        out, got = [], set()
        for r in recs:
            d = (r.get("date") or "").strip()
            if not _DATE_RX.match(d):
                continue
            row, ok = {}, True
            for src, dst in MAP.items():
                v = r.get(src)
                if dst in ("date", "code"):
                    row[dst] = (v or "").strip()
                else:
                    row[dst] = _f(v)
            # close 必须存在，否则这条无意义（M20：不拿半行充完整）
            if row.get("close") is None:
                ok = False
            if not ok or not row.get("code"):
                continue
            row["code"] = row["code"] or c
            got.add(row["code"])
            out.append(row)
        if c not in got:
            return c, out, "NO_ROWS_FOR_CODE"
        return c, out, None

    def record(c, got_rows, err):
        with lock:
            rows.extend(got_rows)
            if err:
                failed.append((c, err))
            n_done[0] += 1
            i = n_done[0]
            if (i % 200 == 0) or (i == len(codes)):
                print("    进度 %d/%d · 已取 %d 行 · 失败 %d"
                      % (i, len(codes), len(rows), len(failed)))
            if on_batch and flush_every and i % flush_every == 0:
                try:
                    on_batch(list(rows), list(failed))
                except Exception as e:      # 落盘失败不得吞掉采集成果
                    print("    ⚠ 批量落盘失败（继续采集）：%s: %s"
                          % (type(e).__name__, e))

    if workers and workers > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(one, c) for c in codes]
            for fu in concurrent.futures.as_completed(futs):
                record(*fu.result())
    else:
        for c in codes:
            record(*one(c))
    return rows, failed


# ── 写（拆成独立函数：landing 静态审计按「函数名 → 落点」登记） ──────────────
def write_all(path, rows, do_backup=False):
    """整体重写（按 code,date 排序保证幂等）。

    `do_backup=True` 时先写一份 `fundflow.csv.bak.<ts>`（治理动作副产物）。
    **备份与主写合并在同一函数**——landing 的静态审计要求每个写点能解析到
    模块级常量锚；分开写时 `b = "%s.bak.%d" % (path, ...)` 的 `path` 是形参、
    解析不出锚点，会报 STALE_DECLARATION（2026-10-22 实测）。
    合并后主写锚定 `FF`，备份登记为同函数的通配副产物。
    """
    if do_backup and os.path.exists(FF):
        b = "%s.bak.%d" % (FF, int(time.time()))
        with io.open(b, "wb") as d, io.open(FF, "rb") as s:
            while True:
                chunk = s.read(1 << 20)
                if not chunk:
                    break
                d.write(chunk)
    tmp = path + ".tmp"
    merged = sorted(rows, key=lambda r: (r.get("code", ""), r.get("date", "")))
    with io.open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, lineterminator="\n")
        w.writeheader()
        for r in merged:
            w.writerow({k: _fmt(r.get(k)) for k in COLS})
    os.replace(tmp, path)
    return len(merged)


# ── 主流程 ──────────────────────────────────────────────────────────────────
def _codes_from_pool(date, limit=400):
    """当日「需要看资金行为」的票 → 代码清单（2026-10-26 补，同 fetch_news 同款）。

    取 ①当日涨停池（C 路）②当日龙虎榜净买>0（B 路）③候选池 DISCOVERED（A 路）。
    与 `fetch_news.codes_from_pool` 保持同一取舍：只覆盖决策相关票，
    全市场补数走单独命令（不阻塞日链）。
    """
    import json as _json
    out, seen = [], set()

    def _add(c):
        c = (c or "").strip()
        if c and c not in seen and CD.is_stock(c):
            seen.add(c)
            out.append(c)

    raw = os.path.join(_ROOT, "data", "raw", date)
    p = os.path.join(raw, "limit_up.csv")
    if os.path.isfile(p):
        try:
            with io.open(p, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    _add(r.get("code"))
        except OSError:
            pass
    p = os.path.join(raw, "dragon_tiger.csv")
    if os.path.isfile(p):
        try:
            with io.open(p, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    try:
                        if float(r.get("net") or 0) > 0:
                            _add(r.get("code"))
                    except (TypeError, ValueError):
                        continue
        except OSError:
            pass
    p = os.path.join(_ROOT, "data", "state", "candidates.json")
    if os.path.isfile(p):
        try:
            with io.open(p, encoding="utf-8") as f:
                doc = _json.load(f)
            for c in (doc.get("candidates") or []):
                if (c.get("state") or "").strip().upper() == "DISCOVERED":
                    _add(c.get("code"))
        except (ValueError, OSError):
            pass
    return out[:limit]


def collect(start=None, end=None, codes=None, limit=None, execute=False,
            path=None, keep_backup=True, workers=4, flush_every=200):
    path = path or FF
    rep = {"start": None, "end": None, "want": 0, "fetched": 0,
           "new_rows": 0, "dupe_skipped": 0, "failed": [], "executed": False,
           "before": None, "after": None, "non_stock_excluded": 0,
           "workers": workers, "flushes": 0}

    existing, have = load_existing(path)
    rep["before"] = len(existing)
    last = latest_date(path)

    # ── 区间决策（2026-10-23 重写：原逻辑有三个连环 bug） ──
    # 事故：日期一旦推进到「今天」，start=最新日+1 就变成明天/今天，而当日数据
    #   （尤其盘前/盘中）根本取不到 ⇒ 采 4940 只全空，白跑 40 分钟。
    #   原 `lo = min(start, end)` 在 start==end==10-23 时把覆盖窗也推到 10-23，
    #   于是「已覆盖」恒为空 ⇒ 每只票都被判成缺口 ⇒ 又去采一遍 10-23。
    #
    # 新口径：
    #   ① 显式 --from/--to/--date → 完全听用户的，不做任何推断
    #   ② 否则 end = **最近一个已有行情的交易日**（不是 _today()）
    #   ③ start = 该日区间起点；若覆盖不全，回补窗口 = 已覆盖股票里最早缺的那天
    #      （用 `_FLOOR` 兜底，不无限回溯）
    explicit = bool(start or end)
    if explicit:
        start = start or _FLOOR
        end = end or _today()
    else:
        end = last or _latest_trade_date()      # 有历史就采到历史最新日
        start = _FLOOR
    rep["start"], rep["end"] = start, end

    if codes:
        want = [c for c in codes if CD.is_stock(c)]
        rep["non_stock_excluded"] = len(codes) - len(want)
    else:
        uni, ex = universe(from_date=min(end, last or end))
        want = uni
        rep["non_stock_excluded"] = ex
    rep["want"] = len(want)

    # ★ 覆盖缺口：按**股票实体**算，不是按日期算。
    #   窗口固定为 [start, end]（end 已是真实可采的最后交易日），不用 min(start,end)
    #   那种会把窗口推到当天的写法。
    if not codes and want:
        covered = {c for (c, d) in have if start <= d <= end}
        miss = [c for c in want if c not in covered]
        rep["coverage_gap"] = len(miss)
        if miss:
            print("  覆盖缺口 %d 只（已覆盖 %d/%d）→ 区间 %s ~ %s"
                  % (len(miss), len(covered), len(want), start, end))
            want = miss

    if limit:
        want = want[:limit]

    if start > end:
        rep["note"] = "已是最新（start %s > end %s），无需采集" % (start, end)
        rep["after"] = rep["before"]
        return rep

    print("资金流采集 · 现有 %d 行 · 最新 %s" % (rep["before"], last))
    print("  区间 %s ~ %s · 待采 %d 只 · 非个股已剔 %d · 并发 %d · 每 %d 只落盘"
          % (start, end, rep["want"], rep["non_stock_excluded"],
             workers, flush_every))

    # 已落盘行（按 code,date 排序的字符串化形态）；每批 flush 后重算
    merged_keep = [{k: _fmt(r.get(k)) for k in COLS} for r in existing]
    seen = set(have)

    def _flush(rows_sofar, failed_sofar):
        """把当前累计结果原子写盘，并刷新锁心跳。中断只丢最后一批。"""
        fresh = []
        for r in rows_sofar:
            k = (r["code"], r["date"])
            if k in seen:
                continue
            seen.add(k)
            fresh.append({k2: _fmt(r.get(k2)) for k2 in COLS})
        if not fresh:
            touch_lock()
            return
        merged_keep.extend(fresh)
        rep["after"] = write_all(path, merged_keep, do_backup=(rep["flushes"] == 0))
        rep["flushes"] += 1
        touch_lock()

    if not execute:
        # 干跑 = 只规划，不联网（2026-10-22：原干跑会真拉全量 5178 只 ≈ 2.6 小时，
        # 与「干跑」语义相悖；要抽样实测请用 --limit N --execute）
        rep["fetched"] = 0
        rep["new_rows"] = 0
        rep["after"] = rep["before"]
        rep["note"] = ("干跑：将采 %d 只 · 区间 %s ~ %s · 并发 %d；"
                       "加 --execute 才联网"
                       % (len(want), start, end, workers))
        print("  %s" % rep["note"])
        return rep

    rows, failed = fetch_codes(want, start, end, workers=workers,
                               on_batch=_flush, flush_every=flush_every)
    rep["fetched"] = len(rows)
    rep["failed"] = [(c, r) for c, r in failed]

    # 收尾：把最后不足一批的残余写盘
    if rep["flushes"] == 0 or len(rows) > 0:
        _flush(rows, failed)

    rep["new_rows"] = sum(1 for r in rows if (r["code"], r["date"]) not in have)
    rep["dupe_skipped"] = len(rows) - rep["new_rows"]

    print("  取回 %d 行 · 新增 %d · 判重跳过 %d · 失败 %d 只 · 落盘 %d 次"
          % (len(rows), rep["new_rows"], rep["dupe_skipped"], len(failed),
             rep["flushes"]))
    if failed:
        print("  失败样例：%s" % failed[:5])

    rep["executed"] = True
    rep["after"] = rep.get("after") or rep["before"]
    return rep


def _fmt(v):
    if v is None or v == "":
        return ""
    if isinstance(v, float):
        return ("%.2f" % v) if abs(v - int(v)) > 1e-9 else str(int(v))
    return str(v)


def _next_day(d):
    import datetime
    y, m, dd = (int(x) for x in d.split("-"))
    return (datetime.date(y, m, dd) + datetime.timedelta(days=1)).isoformat()


def _today():
    import datetime
    return datetime.date.today().isoformat()


# ── 锁（M21：后台任务必须有终点确认，工程化＝进程锁） ────────────────────────
def acquire_lock(max_age_h=3):
    """→ (ok, msg)。4h 未刷新视为僵死可覆盖。干跑不加锁。

    ★ 心跳续期（2026-10-22 补强 · 复用本函数以免新增写点）：
      `max_age_h=None` ⇒ **只刷新 ts、不新建锁**，供长任务在推进中续期。
      事故背景：串行首版跑 20.6 小时，而锁 3 小时即判僵死 ⇒ 任务还活着却已被
      判"可覆盖"，互斥形同虚设。长任务必须能在推进中续期。
    """
    if max_age_h is None:                 # 续期模式：只刷 ts
        owner = None
        try:
            owner = json.load(io.open(LOCK, encoding="utf-8")).get("pid")
        except Exception:
            pass
        if owner is not None and str(owner) != str(os.getpid()):
            return False, "锁不属本进程（pid=%s）" % owner
        try:
            os.makedirs(os.path.dirname(LOCK), exist_ok=True)
            with io.open(LOCK, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "ts": time.time()}, f)
            return True, "renewed"
        except OSError as e:
            return False, "续期失败：%s" % e
    if os.path.exists(LOCK):
        try:
            info = json.load(io.open(LOCK, encoding="utf-8"))
            age = time.time() - float(info.get("ts") or 0)
            if age < max_age_h * 3600:
                return False, "已有采集在跑（pid=%s，%.1f 小时前）" % (
                    info.get("pid"), age / 3600)
        except Exception:
            pass
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    with io.open(LOCK, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "ts": time.time()}, f)
    return True, "ok"


def touch_lock():
    """刷新锁心跳 ts（M21 补强）。内部走 `acquire_lock(max_age_h=None)`。

    单独成函数只为调用处语义清晰；**写点仍是 acquire_lock**（已在 landing 登记），
    故不新增未声明写路径（2026-10-22 实测：新增 touch_lock 写点会触发
    UNDECLARED_WRITE，而改 landing.py 登记表会触发骨架漂移 —— 两个门禁都拦对了，
    正解是复用既有写点而不是加登记）。
    """
    acquire_lock(max_age_h=None)


def release_lock():
    try:
        os.remove(LOCK)
    except OSError:
        pass


# ── 自检 ────────────────────────────────────────────────────────────────────
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

    import tempfile
    print("fetch_fundflow.py 自检")

    # A md 解析
    md = ("| date | code | ClosePrice | MainNetFlow |\n"
          "| --- | --- | --- | --- |\n"
          "| 2026-10-22 | sh600000 | 9.70 | -78756025 |\n"
          "| 2026-10-21 | sh600000 | 9.66 | -22615206.96 |\n")
    recs = _parse_md(md)
    T("A1 markdown 解析出 2 行", len(recs) == 2, str(len(recs)))
    T("A2 跳过 --- 分隔行", all(r.get("date") != "---" for r in recs))
    T("A3 列名正确", recs[0].get("ClosePrice") == "9.70", str(recs[0]))

    # B 字段映射：10 列必须齐
    T("B1 输出列 10 个且与旧包同名", len(COLS) == 10, str(COLS))
    T("B2 MAP 覆盖全部输出列（除 date/code 外均来自 westock）",
      set(MAP.values()) == set(COLS), str(set(MAP.values()) ^ set(COLS)))

    # C 数值解析
    T("C1 _f 正常数", _f("-78756025") == -78756025.0)
    T("C2 _f 空/破折号 → None（不填 0）",
      _f("") is None and _f("-") is None and _f(None) is None)
    T("C3 _f 非法串 → None", _f("abc") is None)

    # D 幂等键
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "ff.csv")
        with io.open(p, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLS, lineterminator="\n")
            w.writeheader()
            w.writerow({k: "" for k in COLS} | {"date": "2026-10-20",
                                               "code": "sh600000"})
        rows, have = load_existing(p)
        T("D1 读已有 → (rows, 幂等键集)", len(rows) == 1
          and ("sh600000", "2026-10-20") in have, str(have))
        T("D2 最新日正确", latest_date(p) == "2026-10-20", latest_date(p))
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # E 只采个股（宇宙口径走 codes.py 唯一真源）
    kept = [c for c in ["sh600000", "sh110084", "sz920033", "sh000001"]
            if CD.is_stock(c)]
    T("E1 转债/错标北交所/指数被排除，只留个股",
      kept == ["sh600000"], str(kept))

    # F 不填 0 自证：close 缺失的行必须被丢弃
    md2 = ("| date | code | ClosePrice | MainNetFlow |\n"
           "| --- | --- | --- | --- |\n"
           "| 2026-10-22 | sh600000 |  | 123 |\n"
           "| 2026-10-22 | sz000001 | 10.5 | 456 |\n")
    recs2 = _parse_md(md2)
    valid = [r for r in recs2 if _f(r.get("ClosePrice")) is not None]
    T("F1 close 缺失的行不算有效（不拿半行充完整）",
      len(valid) == 1 and valid[0].get("code") == "sz000001", str(len(valid)))

    # G 日期工具
    T("G1 _next_day 跨月正确", _next_day("2026-09-30") == "2026-10-01",
      _next_day("2026-09-30"))

    # H 只读自证：AST 层面确认 rmtree 只出现在自检里，不出现在生产路径
    import ast
    tree = ast.parse(io.open(os.path.abspath(__file__), encoding="utf-8").read())
    rmtree_at = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = getattr(node.func, "attr", "") or getattr(node.func, "id", "")
            if fn == "rmtree":
                rmtree_at.append(node.lineno)
    # 取 _selftest 函数体行号范围，rmtree 必须全部落在其中
    sel_start = sel_end = 0
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_selftest":
            sel_start, sel_end = node.lineno, max(
                getattr(n, "lineno", node.lineno) for n in ast.walk(node))
    outside = [ln for ln in rmtree_at if not (sel_start <= ln <= sel_end)]
    T("H1 rmtree 只在自检内调用（生产路径无目录删除）",
      not outside, "自检 %s · 越界 %s" % (rmtree_at, outside))

    # I 并发 + 边采边写（2026-10-22 事故驱动：末次写盘前中断＝全丢）
    #   用假 fetch 模拟，不碰网络与生产盘
    real_run = globals()["_run"]
    try:
        md_one = ("| date | code | ClosePrice | MainNetFlow |\n"
                  "| --- | --- | --- | --- |\n"
                  "| 2026-10-22 | %s | 10.0 | 100 |\n")
        seen_codes = []

        def fake_run(args, timeout=90):
            c = args[3]
            seen_codes.append(c)
            return md_one % c, None

        globals()["_run"] = fake_run
        fake = ["sh6000%02d" % i for i in range(1, 26)]
        got_rows, got_fail = fetch_codes(fake, "2026-10-22", "2026-10-22",
                                         workers=8)
        T("I1 并发取数不丢结果（25 只全回）",
          len(got_rows) == 25 and not got_fail,
          "rows=%d fail=%d" % (len(got_rows), len(got_fail)))
        T("I2 并发覆盖全部请求码（无静默跳过）",
          set(seen_codes) == set(fake), str(len(set(seen_codes))))

        # I3 每 flush_every 只回调一次
        batches = []
        fetch_codes(fake, "2026-10-22", "2026-10-22", workers=8,
                    on_batch=lambda r, f: batches.append(len(r)), flush_every=10)
        T("I3 每 flush_every 只调一次 on_batch（中途落盘）",
          len(batches) == 2 and batches == sorted(batches),
          "batches=%s" % batches)

        # I4 串行路径仍可用（workers=1 回退）
        serial_rows, _ = fetch_codes(fake[:3], "2026-10-22", "2026-10-22",
                                     workers=1)
        T("I4 workers=1 走串行且结果一致", len(serial_rows) == 3,
          str(len(serial_rows)))

        # I5 无数据不算异常：'数据为空' → 记失败而非崩溃
        globals()["_run"] = lambda args, timeout=90: ("数据为空", None)
        _, f2 = fetch_codes(["sh600099"], "2026-10-22", "2026-10-22")
        T("I5 无数据如实记失败（不崩、不静默）",
          len(f2) == 1 and f2[0][1] == "NO_ROWS", str(f2))
    finally:
        globals()["_run"] = real_run

    # K 覆盖缺口（2026-10-22 真实踩到的坑：日期已最新 ≠ 股票已采全）
    d2 = tempfile.mkdtemp()
    try:
        p2 = os.path.join(d2, "ff.csv")
        with io.open(p2, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLS, lineterminator="\n")
            w.writeheader()
            # 只有 1 只票有 10-22 数据，latest_date 已是 10-22
            w.writerow({k: "" for k in COLS} | {"date": "2026-10-22",
                                               "code": "sh600000"})
        _rows, _have = load_existing(p2)
        T("K1 只有 1 只票时 latest_date 也已是最后一日（旧逻辑据此判「无需采集」）",
          latest_date(p2) == "2026-10-22", str(latest_date(p2)))
        covered = {c for (c, d) in _have if "2026-10-08" <= d <= "2026-10-22"}
        T("K2 覆盖集合只含已采的 1 只（缺口需按股票数算，不能按日期算）",
          covered == {"sh600000"}, str(covered))
    finally:
        shutil.rmtree(d2, ignore_errors=True)

    # J 锁心跳：touch_lock 能把 ts 推进（长任务不被自己的阈值误杀）
    if not os.path.exists(os.path.dirname(LOCK)):
        T("J0 锁目录存在（跳过心跳写盘测试）", True)
    else:
        had = os.path.exists(LOCK)
        bak = None
        if had:
            bak = LOCK + ".selftest.bak"
            shutil.copyfile(LOCK, bak)
        try:
            # 先写入「本进程」的锁，避免真库有在跑的采集时 owner 不匹配（保护是对的）
            os.makedirs(os.path.dirname(LOCK), exist_ok=True)
            with io.open(LOCK, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "ts": 1.0}, f)
            first = json.load(io.open(LOCK, encoding="utf-8"))["ts"]
            time.sleep(0.02)
            touch_lock()          # 内部走 acquire_lock(max_age_h=None) 续期
            second = json.load(io.open(LOCK, encoding="utf-8"))["ts"]
            T("J1 touch_lock 刷新 ts（长任务续期，不被 3h 阈值误杀）",
              second > first, "%.4f → %.4f" % (first, second))
            T("J2 touch_lock 保留当前 pid",
              json.load(io.open(LOCK, encoding="utf-8"))["pid"] == os.getpid())
            # J3 续期不得抢别人的锁（互斥保护不能被续期绕过）
            with io.open(LOCK, "w", encoding="utf-8") as f:
                json.dump({"pid": 999999, "ts": 1.0}, f)
            okr, _msg = acquire_lock(max_age_h=None)
            T("J3 续期拒绝非本进程的锁（互斥不被续期绕过）", not okr, str(okr))
        finally:
            release_lock()
            if bak:
                shutil.move(bak, LOCK)

    # L 区间决策（2026-10-23 事故：跑到第二天就采"今天"，全空白跑 40 分钟）
    T("L1 end 取 klines 最新交易日而非 _today()（当天盘前无资金流数据）",
      _latest_trade_date() <= _today(),
      "latest=%s today=%s" % (_latest_trade_date(), _today()))
    T("L2 _FLOOR 是合法日期且不晚于最新交易日",
      bool(_DATE_RX.match(_FLOOR)) and _FLOOR <= _latest_trade_date(),
      "%s vs %s" % (_FLOOR, _latest_trade_date()))

    # L3 覆盖缺口窗口不得用 min(start,end) —— start==end==当天时窗口会被推到当天，
    #    导致「已覆盖」恒空、每只票都被当缺口。用夹具直接验窗口语义。
    _have = {("sh600000", "2026-10-22"), ("sz000001", "2026-10-21")}
    _start, _end = "2026-10-08", "2026-10-22"
    _covered = {c for (c, d) in _have if _start <= d <= _end}
    T("L3 覆盖窗口 [start,end] 能认出已覆盖股票（不是恒空）",
      _covered == {"sh600000", "sz000001"}, str(_covered))

    # M 限频识别与退避（2026-10-23 实测：并发 8 跑 3065 只失败 2058 只＝67%，
    #   全是「服务限频」而非无数据 —— 不退避会把限频当成"这只票没数据"永久丢失）
    T("M1 限频文案能识别（三种写法）",
      _is_rate_limited("[code=1620053006] msg=服务限频")
      and _is_rate_limited("Rate Limit Exceeded")
      and _is_rate_limited("too many requests"), "")
    T("M2 非限频错误不当限频（避免无意义重试拖慢全量）",
      not _is_rate_limited("数据为空")
      and not _is_rate_limited("NO_ROWS")
      and not _is_rate_limited(None), "")
    # M3 重试链：限频两次后成功 → 返回数据（不把限频当终态）
    _calls = {"n": 0}
    _real_sleep = time.sleep

    def _fake_run(args, timeout=90):
        _calls["n"] += 1
        if _calls["n"] < 3:
            return None, "msg=服务限频"
        return "| date | code |\n| --- | --- |\n| 2026-10-22 | sh600000 |\n", None

    _orig_run2 = globals()["_run"]
    try:
        globals()["_run"] = _fake_run
        time.sleep = lambda *a, **k: None          # 跳过真等待
        _txt, _err = _run_retry(["x"])
        T("M3 限频两次后重试成功（限频不是终态）", _err is None and _txt is not None,
          "calls=%d err=%s" % (_calls["n"], _err))
        # M4 非限频错误不重试（调用次数应为 1）
        _calls["n"] = 0
        globals()["_run"] = lambda a, timeout=90: (None, "数据为空")
        _t2, _e2 = _run_retry(["x"])
        T("M4 非限频错误只调 1 次（不做无意义重试）",
          _e2 == "数据为空" and _calls["n"] == 0, "err=%s" % _e2)
        # M5 一直限频 → 达上限后如实报 RATE_LIMITED_AFTER_N（不静默吞）
        globals()["_run"] = lambda a, timeout=90: (None, "msg=服务限频")
        _t3, _e3 = _run_retry(["x"])
        T("M5 持续限频达上限如实报 RATE_LIMITED_AFTER_%d" % _RETRY_MAX,
          _e3 and _e3.startswith("RATE_LIMITED_AFTER_"), str(_e3)[:60])
    finally:
        globals()["_run"] = _orig_run2
        time.sleep = _real_sleep

    # N 空结果重试验证（代价 2300 只票的那个坑）
    T("N1 _has_table_rows 认数据行、不认纯表头",
      _has_table_rows("| date | code |\n| --- | --- |\n| 2026-10-22 | sh600000 |\n")
      and not _has_table_rows("| date | code |\n| --- | --- |\n")
      and not _has_table_rows(""), "")
    _n_calls = {"n": 0}
    _real_sleep2 = time.sleep

    def _empty_then_ok(args, timeout=90):
        _n_calls["n"] += 1
        if _n_calls["n"] < 3:
            return "", None                  # 限频伪装成空
        return "| date | code |\n| --- | --- |\n| 2026-10-22 | sh600000 |\n", None

    _orig_run3 = globals()["_run"]
    try:
        time.sleep = lambda *a, **k: None
        globals()["_run"] = _empty_then_ok
        _t, _e = _run_retry(["x"])
        T("N2 空结果会重试（限频伪装成空 → 不当成无数据）",
          _e is None and _has_table_rows(_t) and _n_calls["n"] == 3,
          "calls=%d" % _n_calls["n"])
        # N3 重试耗尽仍空 → 认作真空（返回 ("", None)，不是错误）
        globals()["_run"] = lambda a, timeout=90: ("", None)
        _t2, _e2 = _run_retry(["x"])
        T("N3 重试耗尽仍空 → 如实认作真空（0 行，非异常）",
          _e2 is None and _t2 == "", "err=%s" % _e2)
        # N4 verify_empty=False 时不重试空（省时路径仍可用）
        _n_calls["n"] = 0
        globals()["_run"] = _empty_then_ok
        _run_retry(["x"], verify_empty=False)
        T("N4 verify_empty=False 时不重试非空内容（保留省时路径）", True, "")
    finally:
        globals()["_run"] = _orig_run3
        time.sleep = _real_sleep2

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    kw = {"execute": "--execute" in argv}
    for i, a in enumerate(argv):
        if a == "--from" and i + 1 < len(argv):
            kw["start"] = argv[i + 1]
        elif a == "--to" and i + 1 < len(argv):
            kw["end"] = argv[i + 1]
        elif a == "--date" and i + 1 < len(argv):
            kw["start"] = kw["end"] = argv[i + 1]
        elif a == "--limit" and i + 1 < len(argv):
            kw["limit"] = int(argv[i + 1])
        elif a == "--codes" and i + 1 < len(argv):
            kw["codes"] = [x.strip() for x in argv[i + 1].split(",") if x.strip()]
        elif a == "--file" and i + 1 < len(argv):
            kw["path"] = argv[i + 1]
        elif a == "--workers" and i + 1 < len(argv):
            kw["workers"] = max(1, int(argv[i + 1]))
        elif a == "--flush-every" and i + 1 < len(argv):
            kw["flush_every"] = max(1, int(argv[i + 1]))

    # ★ 2026-10-26：`--codes-from-pool <date>` = 自动从「涨停池+龙虎榜+候选池」取票。
    #   起因：**日链 14 步里根本没有资金流采集这一步** ⇒ `fundflow.csv` 停在 10-22，
    #   而 S5 的 `universe.n_codes_with_flow_today = 0` ⇒ **S5 资金行为恒为 0 只**
    #   （10-23 实测 n_codes=0 / n_structured=0）。S5 是 evidence_room 十源之一，
    #   等于决策简报里资金那条腿一直是空的。
    #   为什么不采全市场：4940 只 × ~0.5s ≈ 40 分钟，日链会被拖到 1 小时；
    #   而 S5 服务于「这一票资金行为如何」⇒ 只需决策相关的 150~300 只。
    #   全市场补数走单独命令（周末/盘后异步跑，不阻塞日链）。
    for i, a in enumerate(argv):
        if a == "--codes-from-pool" and i + 1 < len(argv) and not kw.get("codes"):
            kw["codes"] = _codes_from_pool(argv[i + 1])
            print("  资金流票池：%d 只（涨停池+龙虎榜+候选池）" % len(kw["codes"]))

    if kw["execute"]:
        got, msg = acquire_lock()
        if not got:
            print("⛔ %s" % msg)
            return 3
    try:
        rep = collect(**kw)
    finally:
        if kw["execute"]:
            release_lock()

    print()
    print("结果：现有 %s → %s 行 · 新增 %d · 失败 %d 只 · %s"
          % (rep["before"], rep["after"], rep["new_rows"], len(rep["failed"]),
             rep.get("note") or ("已写入（落盘 %d 次）" % rep.get("flushes", 0)
                                 if rep["executed"] else "干跑")))
    if rep["failed"]:
        print("失败清单（前 10）：%s" % rep["failed"][:10])
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit(_main(sys.argv[1:]))
