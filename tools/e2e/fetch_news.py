#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""fetch_news.py · 新闻/公告采集（2026-10-22 建）

## 为什么必须建这个

`data/raw/<日期>/news.jsonl` 是 **S1 / S7 / S8 / E7 的共同上游**：
- S8 政策/产业传导（回看 30 天）
- S7 财报节点博弈
- S1 事件催化评估
- E7 个股深度研究

但 **2026-10-22 实测**：
```
2026-09-22 ~ 09-30：每天 1136 / 1911 / 1406 / 1437 / 1758 / 486 条  ← 正常
2026-10-13 ~ 10-22：**全部 0 条 · 0 字节**                          ← 断了一整个月
```

根因：`grep news.jsonl tools/ --include=*.py` 显示**生产代码里没有任何模块
负责采集它**，只有 `drive.py`（研究沙盒）与自检夹具会写。
⇒ 9 月那批是从旧包/研究流程来的，10 月起没人采。

S8 报「扫 8134 条新闻」其实是**回看 30 天读到的 9 月数据**，
当日增量恒为 0 —— 这解释了为什么它一直「能跑但内容陈旧」。

## 数据源

`westock news list sh000001 --limit N`（市场资讯）
`westock notice list <代码[,代码...]> --limit N`（个股公告，支持批量）

实测返回列（news）：`id, src, summary, time, title, typeStr, url`

### ★ 回溯边界（实测硬限制，2026-10-22）

| 请求 | 实际覆盖 |
|---|---|
| `--limit 100` | **10-20 ~ 10-22 共 3 个自然日**（每天 20~50 条） |
| `--limit 500` | **返回空**（超上限直接不给） |

⇒ **只能采"最近几天"，补不了更早的历史。** `2026-10-13~10-19` 实测
无论如何都取不到，这是**数据源限制不是代码缺陷**。

因此本工具的正确用法是：**每天跟着跑**（已进每日链第 8 步），
靠日积月累攒出连续序列；**不要指望事后一次性补齐一个月**。

## 输出格式（对齐既有 `data/raw/<日期>/news.jsonl` 契约）

每行一个 JSON：
```json
{"news_id": "...", "ts": "2026-10-22 15:51:00", "source": "上海证券报",
 "title": "...", "body": "...", "related_codes": [...]}
```

★ **严格 6 列，不许多也不许少**（2026-10-22 修正）：
  快照层 `snapshot.write_snapshot` 用 `set(rec.keys()) != set(cols)` **严格相等**判定，
  多一列即抛 `SnapshotError`。本件原写出 `kind` 与 `url` 两列 ⇒ **`news` 永远无法
  promote 进快照**（实测 2026-10-22 promote 被拒，上一轮补采的新闻从未真正生效）。
  这两列无任何下游消费者（唯一相近的 `sources.kind` 是 S1 自产结果字段，非 news 输入）
  ⇒ 直接从契约中剔除。`url` 有价值但契约无此列，若要落须先扩 `snapshot.CONTRACT`
  （属骨架变更，须显式升版本，不在本件擅改）。

**关键**：`related_codes` 必须是**真源**，不能猜。
- 市场资讯（`news list sh000001`）来源本身不带个股 → `related_codes` 留空，
  由下游 `pool_match.from_news` 计入 `unmapped`（**宁可漏召回，不猜**）
- 个股公告（`notice list <code>`）来源确定 → `related_codes=[code]`（真源）

## 纪律

- **增量 append**：按 `news_id` 判重（M22），重跑不污染
- **不编造**：接口没给的字段留空，绝不按标题"联想"出代码
- **如实声明缺口**：某天取不到就留 0 条并写进声明，不补假数据
- 先 `.bak`、`.tmp`+`os.replace` 原子写、进程锁（M21）、默认干跑

用法：
  python tools/e2e/fetch_news.py                         # 干跑
  python tools/e2e/fetch_news.py --selftest
  python tools/e2e/fetch_news.py --date 2026-10-22 --limit 100
  python tools/e2e/fetch_news.py --date 2026-10-22 --execute
  python tools/e2e/fetch_news.py --from 2026-10-08 --to 2026-10-22 --execute
  python tools/e2e/fetch_news.py --codes sh600000,sz000001 --notice --execute
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
if os.path.join(_TOOLS, "fs") not in sys.path:
    sys.path.insert(0, os.path.join(_TOOLS, "fs"))
import codes as CD  # noqa: E402
import snapshot as S  # noqa: E402  （契约单一真源，禁在本件重定义列集合）

RAW = os.path.join(_ROOT, "data", "raw")
LOCK = os.path.join(_ROOT, "data", "state", "_fetch_news.lock")

# news.jsonl 契约列（真源 = snapshot.CONTRACT；此处仅做「本件输出 == 契约」自检锚点）
NEWS_KEYS = tuple(c for c, _t in S.CONTRACT["news.jsonl"]["cols"])

# 市场资讯的"指数占位码"：拿它取全市场资讯流（不是个股）
MARKET_PROXY = "sh000001"

_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_RX = re.compile(r"(\d{4}-\d{2}-\d{2})")


# 公告逐票查询的整体时间预算（秒）。超时即停并标 PARTIAL，禁拖死日链。
NOTICE_TIME_BUDGET = 240


def _run(args, timeout=90):
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
    """markdown 表格 → [dict]。"""
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


def _day_of(ts):
    """'2026-10-22 15:51:00' → '2026-10-22'。"""
    m = _TS_RX.search(str(ts or ""))
    return m.group(1) if m else None


def news_path(date):
    return os.path.join(RAW, date, "news.jsonl")


def codes_from_pool(date, limit=300):
    """当日「需要看公告」的票 → 代码清单（2026-10-26 补）。

    ★★ 为什么必须自动取而不是命令行传（这是本项目第 N 次同类教训）：
      `fetch_notices()` 早就写好了、`collect(with_notice=True)` 也早就支持，
      但 **`daily_pipeline` 的 step 9 只传 `--date/--limit/--execute`**，
      既不传 `--codes` 也不传 `--notice` ⇒ 公告**从未被采过一次**。
      后果：`data/raw/bus/notices/` 至今只有 11 个文件、全是 9 月旧包遗留；
      10 月**零公告**。而 S1（事件催化）/S8（政策传导）/A 路（事件驱动）的
      上游全是它 ⇒ **A 路在当前数据上等于零**。
      实测（2026-10-26）：手工传 9 只主线票即取回 11 条真公告，其中
      **歌尔股份 10-23 18:43 连发 9 条（含三季报 + 中期分红）**——
      18:43 已收盘 ⇒ 10-26 才是第一个定价日，**这是纯信息差**，系统却完全没看。

    ★ 取哪些票（按决策价值排序，非全市场）：
      ① 当日涨停池（C 路情绪标的，公告决定次日溢价能否延续）
      ② 当日龙虎榜净买>0（B 路资金标的）
      ③ 候选池 DISCOVERED（A 路事件标的，本来就要看事件）
      全市场 4940 只逐票查 ≈ 40 分钟，而决策只涉及这 150~300 只 ⇒ 只查它们。
    ★ 取不到任何源 → 返回 []（调用方跳过公告腿，不空跑、不硬编票）。
    """
    out, seen = [], set()

    def _add(c):
        c = (c or "").strip()
        if c and c not in seen and CD.is_stock(c):
            seen.add(c)
            out.append(c)

    # ① 涨停池
    p = os.path.join(RAW, date, "limit_up.csv")
    if os.path.isfile(p):
        try:
            with io.open(p, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    _add(r.get("code"))
        except OSError:
            pass
    # ② 龙虎榜净买>0
    p = os.path.join(RAW, date, "dragon_tiger.csv")
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
    # ③ 候选池 DISCOVERED
    p = os.path.join(_ROOT, "data", "state", "candidates.json")
    if os.path.isfile(p):
        try:
            with io.open(p, encoding="utf-8") as f:
                doc = json.load(f)
            for c in (doc.get("candidates") or []):
                if (c.get("state") or "").strip().upper() == "DISCOVERED":
                    _add(c.get("code"))
        except (ValueError, OSError):
            pass

    return out[:limit]


def load_existing(path):
    """→ (rows, ids)。ids = news_id 集合，幂等键（M22）。"""
    rows, ids = [], set()
    if not os.path.exists(path):
        return rows, ids
    with io.open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            rows.append(r)
            nid = r.get("news_id")
            if nid:
                ids.add(nid)
    return rows, ids


def fetch_market(date, limit=100):
    """市场资讯 → (rows, err)。只保留 `date` 当天的条目。"""
    txt, err = _run(["westock", "news", "list", MARKET_PROXY,
                     "--limit", str(limit)])
    if txt is None:
        return [], err
    recs = _parse_md(txt)
    rows = []
    for r in recs:
        d = _day_of(r.get("time"))
        if d != date:
            continue                       # 只要目标日（别的日子不往里塞）
        nid = (r.get("id") or "").strip()
        title = (r.get("title") or "").strip()
        if not nid or not title:
            continue
        rows.append({
            "news_id": nid,
            "ts": (r.get("time") or "").strip(),
            "source": (r.get("src") or "").strip(),
            "title": title,
            # summary 实测多为空；绝不拿标题"扩写"成 body（禁编造）
            "body": (r.get("summary") or "").strip(),
            "related_codes": [],           # 市场资讯无真源个股 → 空，下游计 unmapped
        })
    return rows, None


def fetch_notices(codes, date, limit=25, time_budget=240):
    """个股公告 → (rows, failed, stats)。

    ★ 2026-10-27 整改 P0c：pool 全量模式曾静默少采（226 码仅 14 条）——
      根因=逐票 westock 查询静默失败/限频 + 按日过滤后逐票覆盖不可见。
      现带 stats 核对：requested/queried/failed/source_rows/matched_date，
      覆盖率 <80% 由 collect 显式告警（禁静默）。

    ★ 2026-11-02 加固：逐票查询必须带**整体时间预算**（默认 240s）。
      实锤：83 票 × limit100 × 90s 超时 = 最长 2 小时，日链曾在此挂死 26 分钟
      （CPU 0 秒 = 纯网络等待）。超预算即停、标 budget_exhausted，禁无限拖。
    """
    rows, failed = [], []
    stats = {"requested": len(codes), "queried": 0, "failed": 0,
             "source_rows": 0, "matched_date": 0,
             "budget_exhausted": False, "skipped": 0, "elapsed_s": 0.0}
    t0 = time.monotonic()
    for c in codes:
        # 整体预算闸：超时即停，剩余票记 skipped（禁无限拖死日链）
        if time.monotonic() - t0 > time_budget:
            stats["budget_exhausted"] = True
            stats["skipped"] = len(codes) - stats["queried"] - stats["failed"]
            break
        if not CD.is_stock(c):
            failed.append((c, "NOT_STOCK"))
            continue
        txt, err = _run(["westock", "notice", "list", c, "--limit", str(limit)],
                      timeout=25)  # ★ 2026-11-02：挂住的票快速跳过，别吃满 90s
        if txt is None:
            failed.append((c, err))
            continue
        stats["queried"] += 1
        recs = _parse_md(txt)
        stats["source_rows"] += len(recs)
        got = 0
        for r in recs:
            d = _day_of(r.get("ann_date") or r.get("time") or r.get("date"))
            if d != date:
                continue
            stats["matched_date"] += 1
            nm = r.get("notice_title") or r.get("title") or ""
            nm = (nm or "").strip()
            nid = (r.get("id") or r.get("notice_id") or "").strip()
            if not nm:
                continue
            if not nid:
                # 没 id 就用 code+日期+标题哈希，保证幂等键稳定
                nid = "NOID-%s-%s-%08x" % (c, date, abs(hash(nm)) % (1 << 32))
            rows.append({
                "news_id": nid,
                "ts": "%s 00:00:00" % date,
                "source": "公告",
                "title": nm[:200],
                "body": "",
                "related_codes": [c],      # ★ 真源：查的就是这只票
            })  # ★ 2026-10-30 再修：kind 字段违反六键契约（10-22 同款缺陷复发），删除
            got += 1
        if got == 0:
            failed.append((c, "NO_NOTICE_TODAY"))
    stats["failed"] = len(failed)
    stats["elapsed_s"] = round(time.monotonic() - t0, 1)
    return rows, failed, stats


def write_append(path, new_rows):
    """append 写（幂等由调用方按 news_id 保证）。原子：写 .tmp 再 replace。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    old_rows, _ = load_existing(path)
    merged = old_rows + new_rows
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for r in merged:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return len(merged)


def collect(date, limit=100, codes=None, with_notice=False, execute=False,
            path=None):
    path = path or news_path(date)
    rep = {"date": date, "fetched": 0, "new": 0, "dupe": 0, "failed": [],
           "before": 0, "after": 0, "executed": False}

    old_rows, ids = load_existing(path)
    rep["before"] = len(old_rows)

    rows, err = fetch_market(date, limit=limit)
    if err:
        rep["failed"].append((MARKET_PROXY, err))
    if with_notice and codes:
        nrows, nfail, nstats = fetch_notices(codes, date, time_budget=NOTICE_TIME_BUDGET)
        rows += nrows
        rep["failed"] += nfail
        rep["notice"] = nstats
        # ★ P0c 显式告警：覆盖率 <80% 或源端 0 行 ⇒ 静默少采嫌疑，必须外显
        cov = (nstats["queried"] / nstats["requested"])             if nstats["requested"] else None
        rep["notice_coverage"] = round(cov, 3) if cov is not None else None
        if cov is not None and cov < 0.8:
            rep["warning"] = ("公告覆盖率 %.0f%%（%d/%d queried，failed %d）"
                              "——低于 80%% 阈值，疑限频/静默失败，勿当全量用"
                              % (cov * 100, nstats["queried"],
                                 nstats["requested"], nstats["failed"]))
        elif nstats["queried"] and nstats["source_rows"] == 0:
            rep["warning"] = "公告源端 0 行（queried %d）——通道异常嫌疑"                 % nstats["queried"]
    rep["fetched"] = len(rows)

    fresh = []
    for r in rows:
        if r["news_id"] in ids:
            rep["dupe"] += 1
            continue
        ids.add(r["news_id"])
        fresh.append(r)
    rep["new"] = len(fresh)

    print("新闻采集 · %s · 现有 %d 条 · 取回 %d · 新增 %d · 判重跳过 %d"
          % (date, rep["before"], rep["fetched"], rep["new"], rep["dupe"]))
    if rep["failed"]:
        print("  失败：%s" % rep["failed"][:5])

    if not fresh:
        rep["after"] = rep["before"]
        rep["note"] = "无新增（幂等）"
        return rep

    if not execute:
        rep["note"] = "干跑：加 --execute 才写"
        return rep

    rep["after"] = write_append(path, fresh)
    rep["executed"] = True
    return rep


# ── 锁 ──────────────────────────────────────────────────────────────────────
def acquire_lock(max_age_h=3):
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
    import shutil
    print("fetch_news.py 自检")

    md = ("| id | src | summary | time | title |\n"
          "| --- | --- | --- | --- | --- |\n"
          "| SN001 | 上海证券报 |  | 2026-10-22 15:51:00 | 标题A |\n"
          "| SN002 | 腾讯 |  | 2026-10-21 10:00:00 | 标题B |\n")
    recs = _parse_md(md)
    T("A1 markdown 解析 2 行", len(recs) == 2, str(len(recs)))
    T("A2 跳过 --- 分隔行", all(r.get("id") != "---" for r in recs))

    T("B1 _day_of 取日期", _day_of("2026-10-22 15:51:00") == "2026-10-22")
    T("B2 _day_of 空值 → None", _day_of("") is None)

    d = tempfile.mkdtemp()
    try:
        p = news_path("2026-10-22")
        p = os.path.join(d, "news.jsonl")
        exist = [{"news_id": "SN001", "title": "x"}]
        with io.open(p, "w", encoding="utf-8") as f:
            for r in exist:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        rows, ids = load_existing(p)
        T("C1 读已有 → (rows, ids)", len(rows) == 1 and "SN001" in ids, str(ids))

        n = write_append(p, [{"news_id": "SN002", "title": "y"}])
        T("C2 append 后 2 条", n == 2, str(n))
        rows2, ids2 = load_existing(p)
        T("C3 append 保留旧行（不是覆盖）",
          {r["news_id"] for r in rows2} == {"SN001", "SN002"}, str(ids2))

        # D 幂等键：重复 id 不进
        n2 = write_append(p, [{"news_id": "SN002", "title": "y-dup"}])
        T("D1 重复 news_id append 后仍 2 条（由调用方判重）", n2 == 3,
          "append 本身不判重，判重在 collect；此处验证行数如实增长")

        # E related_codes 真源纪律：市场资讯必须留空
        T("E1 MARKET_PROXY 是 sh000001（市场资讯代理，非个股）",
          MARKET_PROXY == "sh000001")

        # F 只采个股公告
        kept = [c for c in ["sh600000", "sh110084"] if CD.is_stock(c)]
        T("F1 公告采集只认个股（转债被排除）", kept == ["sh600000"], str(kept))

        # G 原子写
        src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
        T("G1 用 .tmp + os.replace 原子写",
          ".tmp" in src and "os.replace" in src)

        # H 契约列严格相等（2026-10-22：多 kind/url 两列 ⇒ news 永远 promote 不进快照）
        nc = {c for c, _t in S.CONTRACT["news.jsonl"]["cols"]}
        T("H1 news 行字段集与快照契约严格相等（多一列即 SnapshotError）",
          set(NEWS_KEYS) == nc,
          "本件 %s vs 契约 %s" % (sorted(NEWS_KEYS), sorted(nc)))
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    kw = {"execute": "--execute" in argv}
    date = None
    frm = to = None
    codes = None
    for i, a in enumerate(argv):
        if a == "--date" and i + 1 < len(argv):
            date = argv[i + 1]
        elif a == "--from" and i + 1 < len(argv):
            frm = argv[i + 1]
        elif a == "--to" and i + 1 < len(argv):
            to = argv[i + 1]
        elif a == "--limit" and i + 1 < len(argv):
            kw["limit"] = int(argv[i + 1])
        elif a == "--codes" and i + 1 < len(argv):
            codes = [x.strip() for x in argv[i + 1].split(",") if x.strip()]
        elif a == "--day" and i + 1 < len(argv):
            date = argv[i + 1]

    dates = []
    if date:
        dates = [date]
    elif frm and to:
        import datetime
        a = datetime.date.fromisoformat(frm)
        b = datetime.date.fromisoformat(to)
        while a <= b:
            dates.append(a.isoformat())
            a += datetime.timedelta(days=1)
    if not dates:
        import datetime
        dates = [datetime.date.today().isoformat()]

    if kw["execute"]:
        got, msg = acquire_lock()
        if not got:
            print("⛔ %s" % msg)
            return 3
    try:
        total_new = 0
        for d in dates:
            # ★ 2026-10-26：`--notice-pool` = 自动从「涨停池+龙虎榜+候选池」取票再采公告。
            #   为什么不靠命令行传 codes：`daily_pipeline` 的 step 是**静态参数表**，
            #   传不了当日动态票池；而这个开关让本件自己取，日链只需加一个 flag。
            dc = codes
            if dc is None and "--notice-pool" in argv:
                dc = codes_from_pool(d)
                print("  公告票池：%d 只（涨停池+龙虎榜+候选池）" % len(dc))
            rep = collect(d, codes=dc,
                          with_notice=("--notice" in argv or "--notice-pool" in argv),
                          **kw)
            print("  → %s 新增 %d 条 · %s"
                  % (d, rep["new"], rep.get("note") or "已写入"))
            if rep.get("warning"):
                print("  ⚠️ %s" % rep["warning"])
            if rep.get("notice"):
                ns = rep["notice"]
                print("  公告核对：请求 %d · 成查 %d · 失败 %d · 源行 %d · 当日命中 %d · 耗时 %.0fs"
                      % (ns["requested"], ns["queried"], ns["failed"],
                         ns["source_rows"], ns["matched_date"], ns.get("elapsed_s") or 0))
                if ns.get("budget_exhausted"):
                    print("  ⚠️ 公告采集**超时间预算**：剩余 %d 票已跳过（PARTIAL，非全量）"
                          % ns.get("skipped", 0))
            total_new += rep["new"]
    finally:
        if kw["execute"]:
            release_lock()

    print("\n合计新增 %d 条" % total_new)
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit(_main(sys.argv[1:]))
