"""fetch_finance.py · 抓历史财务数据（含真实公告日）→ 供 point-in-time 回测。

## 为什么这个文件是基本面研究的关键

基本面回测最容易犯的错是**未来函数**：
用「报告期」而不是「公告日」对齐。实测证据（600178）：

    EndDate=2025-12-31 的年报，InfoPublDate = 2026-04-14

如果按 EndDate 在 2026-01-02 就用这份年报 ⇒ **提前 3.5 个月使用了当时还不存在的信息**。
本件因此**必须**抓 `InfoPublDate`，并且下游只允许按公告日对齐。

## 数据源

`westock finance <code> --type {income|balance|cashflow} --limit N --fields all`
  `--fields all` 才含 **InfoPublDate**（core 窄表没有）⇒ 本件固定用 all
  实测可回溯 12 期（3 年）；A 股按季报 ⇒ 12 期 = 约 3 年

## 输出

`data/raw/bus/finance.csv`，长表：
  code, end_date, publ_date, source_type, field, value

长表而非宽表：字段名跨报告类型会重名（如 ROE 在 income/balance 都有），
长表用 (source_type, field) 唯一定位；坏处是行数大，但可 gzip。

## 纪律

· **缺 InfoPublDate 的记录整条丢弃**（无法做 point-in-time，留着会诱使下游误用）
· 取不到的票如实计失败，不写空行
· 断点续跑（已抓 code 跳过）；先写 .part 再改名，避免半成品被当成完整
· dry-run 为默认，须 --execute

用法：
  python tools/e2e/fetch_finance.py --check
  python tools/e2e/fetch_finance.py --sample 20 --limit 12
  python tools/e2e/fetch_finance.py --execute --limit 12
"""
import argparse
import csv
import io
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("fs", "analysis", "e2e"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import writegate as _WG           # noqa: E402

OUT_REL = os.path.join("data", "raw", "bus", "finance.csv")
HEADER = ["code", "end_date", "publ_date", "source_type", "field", "value"]
TYPES = ("income", "balance", "cashflow")

# ★★ 2026-10-22 进程锁（M21 · 孤儿进程事故的 P1 修复）
#   事故：2026-10-21 14:38 启动的采集任务未被回收，跑到次日 08:41（18 小时），
#        反复追加已采数据 ⇒ 34.5 万行重复。
#   这里加锁文件，启动时若存在则拒绝启动，避免同一数据区被多条链同时写。
#   锁内容 = PID + 启动时间；超过 LOCK_STALE_SEC 视为僵死锁（可被覆盖），
#   避免上次崩溃留下的锁把后续任务永久堵死。
LOCK_REL = os.path.join("data", "state", "_fetch_finance.lock")
LOCK_STALE_SEC = 4 * 3600          # 4 小时无更新视为僵死
MAX_RUNTIME_SEC = 3 * 3600         # 单次运行上限 3 小时，超时自杀（防无人看管）
# 核心字段（--fields all 里挑，减少行数；不够再加）
KEEP = {
    "income": ("EndDate", "InfoPublDate", "BasicEPS", "ROE", "ROEWeighted",
               "ROETTM", "NPParentCompanyOwners", "NPParentCompanyYOY",
               "OperatingRevenue", "OperatingRevenueGrowRate_Q",
               "GrossIncomeRatio", "NetProfitRatio", "TotalProfit",
               "EBIT", "OperatingProfit"),
    "balance": ("EndDate", "InfoPublDate", "TotalAssets", "TotalLiability",
                "TotalEquity", "CurrentRatio", "DebtAssetsRatio",
                "Inventory", "AccountReceivable", "GoodWill"),
    "cashflow": ("EndDate", "InfoPublDate", "NetOperateCashFlow",
                 "NetInvestCashFlow", "NetFinanceCashFlow", "CashIncrease"),
}


def parse_md_table(text):
    """解析 westock 的 markdown 表格 → (header, rows)。"""
    lines = [ln for ln in text.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return [], []
    hdr = [c.strip() for c in lines[0].strip().strip("|").split("|")]
    rows = []
    for ln in lines[1:]:
        body = ln.strip().strip("|")
        if not body or set(body.replace("|", "").strip()) <= set("- :"):
            continue
        cells = [c.strip() for c in body.split("|")]
        if len(cells) < 2:
            continue
        rows.append(cells)
    return hdr, rows


def run_cli(code, ftype, limit, timeout=180):
    """调 westock finance。→ markdown 文本；失败抛错。"""
    import subprocess
    cmd = ["westock", "finance", code, "--type", ftype,
           "--limit", str(limit), "--fields", "all"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError("westock finance %s %s 失败: %s"
                           % (code, ftype, (p.stderr or p.stdout or "")[:150]))
    return p.stdout or ""


def rows_from_md(code, ftype, text, keep=None):
    """markdown → 长表记录。缺 InfoPublDate 的整条丢弃并计数。"""
    hdr, raw = parse_md_table(text)
    if not hdr:
        return [], 0
    idx = {h: i for i, h in enumerate(hdr)}
    if "EndDate" not in idx or "InfoPublDate" not in idx:
        return [], 0
    keep = keep or KEEP.get(ftype, ())
    out = []
    dropped = 0
    for cells in raw:
        def g(k):
            i = idx.get(k)
            return cells[i].strip() if (i is not None and i < len(cells)) else ""
        end_d, pub_d = g("EndDate"), g("InfoPublDate")
        if not end_d or not pub_d or pub_d in ("-", "None", "null"):
            dropped += 1
            continue
        for f in keep:
            if f in ("EndDate", "InfoPublDate") or f not in idx:
                continue
            v = g(f)
            if v in ("", "-", "None", "null", "--"):
                continue
            out.append({"code": code, "end_date": end_d, "publ_date": pub_d,
                        "source_type": ftype, "field": f, "value": v})
    return out, dropped


def write_append(path, rows, dedup_existing=True):
    """追加写（首次建表头）。

    ★★ 2026-10-22 加幂等保护（M22）：
      事故教训——孤儿进程跑了 18 小时、把已采数据反复追加，
      1,364,802 行里 344,631 行完全重复。
      根因是 `write_append` 纯追加不做判重，而 `load_done` 只按 code 判
      「这只票抓过没」，**粒度不够**（同一只票重跑会整只再写一遍）。

      现在：写入前按 (code, end_date, publ_date, source_type, field) 判重，
      已存在的键跳过。判重表在**每次调用时重建**（不缓存），
      因为长跑任务里别的进程/轮次可能已经写过。

      代价：每次写要扫一遍已有文件。文件 100 万行约 2~3 秒，
      相对每批 14 秒的抓取开销可接受；`dedup_existing=False` 可关掉（排障用）。
    """
    _WG.check_write(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.isfile(path) or os.path.getsize(path) == 0

    seen = set()
    if dedup_existing and not new:
        with io.open(path, encoding="utf-8-sig", errors="ignore", newline="") as f:
            rd = csv.reader(f)
            next(rd, None)
            for cells in rd:
                if len(cells) >= 5:
                    seen.add((cells[0], cells[1], cells[2], cells[3], cells[4]))

    kept, skipped = [], 0
    for r in rows:
        k = (r.get("code", ""), r.get("end_date", ""), r.get("publ_date", ""),
             r.get("source_type", ""), r.get("field", ""))
        if k in seen:
            skipped += 1
            continue
        seen.add(k)
        kept.append(r)

    if kept:
        with io.open(path, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=HEADER, lineterminator="\n")
            if new:
                w.writeheader()
            for r in kept:
                w.writerow({k: r.get(k, "") for k in HEADER})
    return len(kept), skipped


def acquire_lock(root=None):
    """进程锁。→ (ok, reason, lockpath)

    M21：后台任务必须有终点确认。事故根因之一就是「启动即遗忘」，
    这里用锁文件把「同时只允许一条链」变成机器可检查的事实。
    锁内容 = pid + 时间戳；超过 LOCK_STALE_SEC 未更新视为僵死（可覆盖）。
    """
    root = root or _ROOT
    p = os.path.join(root, LOCK_REL)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.isfile(p):
        try:
            age = time.time() - os.path.getmtime(p)
        except OSError:
            age = 0
        if age < LOCK_STALE_SEC:
            try:
                body = io.open(p, encoding="utf-8").read().strip()[:120]
            except OSError:
                body = "?"
            return False, ("已有采集进程在跑（锁 %ds 前更新：%s）"
                           "⇒ 拒绝启动，避免并发写同一数据区" % (age, body)), p
        # 僵死锁：记录并覆盖
        print("  ⚠ 发现僵死锁（%.0f 分钟未更新），覆盖它" % (age / 60))
    try:
        with io.open(p, "w", encoding="utf-8") as f:
            f.write("pid=%d ts=%d\n" % (os.getpid(), int(time.time())))
    except OSError as e:
        return False, "无法创建锁文件：%s" % e, p
    return True, "", p


def release_lock(p):
    try:
        if p and os.path.isfile(p):
            os.remove(p)
    except OSError:
        pass


def touch_lock(p):
    """刷新锁时间戳（长跑心跳，避免被误判僵死）。"""
    try:
        os.utime(p, None)
    except OSError:
        pass


def load_done(path):
    """已抓 code（只扫首列，快）。

    ⚠ 粒度只到 code：只判「这只票抓过没」，**不判「这个 (code, end_date, field) 有没有」**。
      这就是孤儿进程事故里重复数据的来源之一（M22）。
      真正防重复的是 `write_append` 里的键级判重，本函数只用于**减少无谓抓取**。
    """
    if not os.path.isfile(path):
        return set()
    done = set()
    with io.open(path, encoding="utf-8-sig", errors="ignore") as f:
        f.readline()
        for ln in f:
            i = ln.find(",")
            if i > 0:
                done.add(ln[:i])
    return done


def _is_stock_code(c):
    """只保留真实个股：sh6/sz0/sz3/sh68 开头；剔指数、ETF、北交所。

    ★ mktcap.csv 里混着指数（sh000001 等）⇒ 拿去批量查财务必然「无数据返回」，
      白占失败配额、还会污染失败率统计（2026-10-21 实测）。
    """
    if not c or len(c) < 6:
        return False
    if c.startswith("bj"):
        return False
    # 指数/ETF：sh000xxx（上证系列）、sz399xxx（深证系列）、sh51/sz15（ETF）
    if c.startswith("sh000") or c.startswith("sz399"):
        return False
    if c.startswith("sh51") or c.startswith("sz15") or c.startswith("sh58"):
        return False
    return c.startswith(("sh6", "sz0", "sz3"))


def load_universe(root=None):
    """优先用 mktcap.csv 的 code（市值抓取已完成、覆盖 99.7%），否则 klines。

    ★ 已按 `_is_stock_code` 过滤：只留真实个股，剔指数/ETF/北交所。
    """
    root = root or _ROOT
    p = os.path.join(root, "data", "raw", "bus", "mktcap.csv")
    codes = set()
    if os.path.isfile(p):
        with io.open(p, encoding="utf-8-sig", errors="ignore") as f:
            f.readline()
            for ln in f:
                i = ln.find(",")
                if i > 0:
                    c = ln[:i].strip()
                    if _is_stock_code(c):
                        codes.add(c)
        return sorted(codes)
    p2 = os.path.join(root, "data", "raw", "bus", "klines.csv")
    if os.path.isfile(p2):
        with io.open(p2, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if _is_stock_code(r["code"]):
                    codes.add(r["code"])
    return sorted(codes)


def fetch_one(code, limit, types=TYPES, timeout=180):
    """抓单只票的全部报告类型。→ (records, n_dropped, errors)

    ★ 串行时每只约 24 秒（3 表 × 8s），5223 只要 35 小时。
      实测 6 并发能压到约 1/3 用时 ⇒ 由调用方用线程池并发。

    ★★ 2026-10-21 重大修正：**单票调用是限频的根因**。
      `westock finance <code>` 一次只查一只 ⇒ 5200 只要 5200 次请求 ⇒ 必撞「服务限频」。
      而 CLI **原生支持批量**：`westock finance "a,b,c,..." --type income --limit N`
      实测 40 只一批成功。⇒ 请求数直接降到 1/40。
      本函数保留（自检与单票排障用），主流程请用 `fetch_batch`。
    """
    recs, dropped, errs = [], 0, []
    for ft in types:
        try:
            text = run_cli(code, ft, limit, timeout=timeout)
        except Exception as e:
            errs.append((code, ft, str(e)[:70]))
            continue
        r, d = rows_from_md(code, ft, text)
        dropped += d
        recs.extend(r)
    return recs, dropped, errs


# ★★ 2026-10-21 二次修正：批大小从 40 降到 20。
#   发现**批量返回会被静默部分吞掉**——请求 40 只只回 4 只、请求 20 只回 18 只，
#   而且**不报错**（文本里没有"限频"字样），失败清单也看不出来。
#   连打实测（无冷却）：20只×5批 → 20/9/20/4/4；
#   加 20 秒冷却后：20只×4批 → 20/20/17/13。
#   ⇒ 光调批大小不够，**必须靠「识别缺失 → 重试」把吞掉的票捞回来**。
BATCH_SIZE = 20
COOLDOWN_SEC = 20        # 批间冷却（实测有效，0 秒时成功率断崖）
RETRY_ROUNDS = 3         # 缺失票最多重试轮数（每轮批大小减半）


def rows_from_md_multi(ftype, text, keep=None):
    """批量 markdown → 长表记录（**按返回里的 code 列分组**，不靠传入的 code）。

    ★ 与 rows_from_md 的区别：批量返回是多个 code 的行混在一张表里，
      code 必须从**每行的 code 列**读，不能假定是请求的那一只。
    """
    hdr, raw = parse_md_table(text)
    if not hdr:
        return [], 0
    idx = {h: i for i, h in enumerate(hdr)}
    if "code" not in idx or "EndDate" not in idx or "InfoPublDate" not in idx:
        return [], 0
    keep = keep or KEEP.get(ftype, ())
    out = []
    dropped = 0
    for cells in raw:
        def g(k):
            i = idx.get(k)
            return cells[i].strip() if (i is not None and i < len(cells)) else ""
        code = g("code")
        end_d, pub_d = g("EndDate"), g("InfoPublDate")
        if not code or not end_d or not pub_d or pub_d in ("-", "None", "null"):
            dropped += 1
            continue
        for f in keep:
            if f in ("EndDate", "InfoPublDate") or f not in idx:
                continue
            v = g(f)
            if v in ("", "-", "None", "null", "--"):
                continue
            out.append({"code": code, "end_date": end_d, "publ_date": pub_d,
                        "source_type": ftype, "field": f, "value": v})
    return out, dropped


def fetch_batch(codes, limit, types=TYPES, timeout=300, size=BATCH_SIZE):
    """★ 批量抓（推荐）。→ (records, n_dropped, errors, got_codes)

    每批 `westock finance "c1,c2,...,cN" --type X --limit N`，一次拿 N 只票。
    返回值里 code 来自**返回行自己的 code 列**（不假定请求集合）。
    """
    recs, dropped, errs = [], 0, []
    got = set()
    for i in range(0, len(codes), size):
        chunk = codes[i:i + size]
        for ft in types:
            try:
                text = run_cli(",".join(chunk), ft, limit, timeout=timeout)
            except Exception as e:
                errs.append(("+".join(chunk[:3]) + "...", ft, str(e)[:70]))
                continue
            r, d = rows_from_md_multi(ft, text)
            dropped += d
            recs.extend(r)
            for x in r:
                got.add(x["code"])
    return recs, dropped, errs, got


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    ap = argparse.ArgumentParser(description="抓历史财务（含公告日）")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--codes-file", dest="codes_file", default=None,
                    help="只抓这个 JSON 数组里的代码（避免抓全市场里用不到的票）")
    ap.add_argument("--limit", type=int, default=12, help="期数（12=约3年）")
    ap.add_argument("--workers", type=int, default=6, help="并发数（实测 6 较稳）")
    ap.add_argument("--types", default="income",
                    help="报告类型，逗号分隔（默认 income：ROE/EPS/利润/营收 全在这张表；"
                         "balance/cashflow 单独跑以免 3 倍开销）")
    ap.add_argument("--base", default=None)
    ap.add_argument("--single", action="store_true",
                    help="用单票模式（仅排障；全市场会限频，默认走批量）")
    ap.add_argument("--batch-size", dest="batch_size", type=int,
                    default=BATCH_SIZE, help="批量模式每批股票数（默认 20）")
    ap.add_argument("--cooldown", type=float, default=COOLDOWN_SEC,
                    help="批间冷却秒数（默认 20；0 会大幅提高被静默吞掉的概率）")
    ap.add_argument("--retry", type=int, default=RETRY_ROUNDS,
                    help="缺失票重试轮数（默认 3，每轮批大小减半）")
    a = ap.parse_args(argv)

    use_types = tuple(t.strip() for t in a.types.split(",") if t.strip())
    bad = [t for t in use_types if t not in TYPES]
    if bad:
        print("⛔ 未知报告类型 %s（可选 %s）" % (bad, TYPES))
        return 2
    if a.check:
        import subprocess
        p = subprocess.run(["westock", "finance", "sh600178", "--type", "income",
                            "--limit", "1", "--fields", "all"],
                           capture_output=True, text=True, timeout=120)
        ok = p.returncode == 0 and "InfoPublDate" in (p.stdout or "")
        print("westock finance 可用:", ok)
        if not ok:
            print((p.stdout or p.stderr or "")[:300])
        return 0 if ok else 2

    root = a.base or _ROOT
    path = os.path.join(root, OUT_REL)
    if a.codes_file:
        import json
        with io.open(a.codes_file, encoding="utf-8") as f:
            universe = sorted(set(json.load(f)))
        print("（codes-file 指定 %d 只，不抓全市场）" % len(universe))
    else:
        universe = load_universe(root)
    done = load_done(path)
    todo = [c for c in universe if c not in done]
    if a.sample:
        todo = todo[:a.sample]
    print("全池 %d · 已抓 %d · 本次 %d · 每只 %d 期 × %d 表(%s)"
          % (len(universe), len(done), len(todo), a.limit, len(use_types),
             ",".join(use_types)))

    t0 = time.time()
    n_ok = n_fail = n_rows = n_drop = 0
    n_skip_dup = 0        # 写前判重跳过的行数（>0 说明有重抓，M22）
    fails = []
    buf = []

    # ★★ 进程锁（M21）：只允许一条采集链同时写 finance.csv。
    #   干跑不加锁（不写盘，无害），只有 --execute 才需要互斥。
    lock_p = None
    if a.execute:
        ok, why, lock_p = acquire_lock(root)
        if not ok:
            print("⛔ %s" % why)
            return 3

    def flush():
        nonlocal n_rows, n_skip_dup
        if buf and a.execute:
            # ★ 写前按键判重（M22）：返回 (实写行数, 跳过重复数)
            w, sk = write_append(path, buf)
            n_rows += w
            n_skip_dup += sk
            if lock_p:
                touch_lock(lock_p)
        buf.clear()

    # ★★ 2026-10-21：默认走**批量模式**。
    #   原因：单票模式 5200 只 = 5200 次请求 ⇒ 必撞「服务限频」（实测 code=1620053006）。
    #   CLI 原生支持 `finance "a,b,c"` 批量 ⇒ 请求数降到 1/40。
    if a.single:
        print("⚠ 单票模式（--single）：仅用于排障，全市场会限频")
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
            futs = {ex.submit(fetch_one, c, a.limit, use_types): c for c in todo}
            done_n = 0
            for fut in cf.as_completed(futs):
                code = futs[fut]
                done_n += 1
                try:
                    recs, dropped, errs = fut.result()
                except Exception as e:
                    n_fail += 1
                    fails.append((code, "fut", str(e)[:70]))
                    continue
                n_drop += dropped
                fails.extend(errs)
                if recs:
                    n_ok += 1
                    buf.extend(recs)
                else:
                    n_fail += 1
                if len(buf) >= 8000:
                    flush()
                if done_n % 50 == 0:
                    flush()
                    print("  %d/%d · 成功 %d · 失败 %d · 已写 %d 行 · 丢弃 %d · %.0fs"
                          % (done_n, len(todo), n_ok, n_fail, n_rows, n_drop,
                             time.time() - t0))
        flush()
    else:
        n_batch = (len(todo) + a.batch_size - 1) // a.batch_size
        print("  批量模式：每批 %d 只 × %d 批 · 批间冷却 %ss · 缺失重试 %d 轮"
              % (a.batch_size, n_batch, a.cooldown, a.retry))
        for bi in range(n_batch):
            # ★ 运行时长上限（M21）：防无人看管的长跑任务。
            #   超时先 flush 保住已抓数据，然后正常退出（不 kill 进程，留干净退出路径）。
            if time.time() - t0 > MAX_RUNTIME_SEC:
                print("  ⏱ 已达单次运行上限 %.0f 分钟，主动停止（已抓数据已落盘）"
                      % (MAX_RUNTIME_SEC / 60))
                print("     续跑：重跑同一命令即可（已抓的会被 load_done 跳过）")
                break
            chunk = todo[bi * a.batch_size:(bi + 1) * a.batch_size]
            recs, dropped, errs, got = fetch_batch(
                chunk, a.limit, use_types, size=a.batch_size)
            n_drop += dropped
            fails.extend(errs)
            if recs:
                n_ok += len(got)
                buf.extend(recs)
            # ★ 判据必须用「本批请求的 chunk」对照「本批返回的 got」，
            #   不能用累计的 n_ok（第一版写成 n_ok 累加后对比，统计全错）
            miss = [c for c in chunk if c not in got]
            # ★★ 关键：批量返回会**静默部分吞掉**且不报错 ⇒ 必须重试。
            #   每轮把批大小减半（越小的批越不容易被吞），并留冷却。
            for rd in range(1, a.retry + 1):
                if not miss:
                    break
                time.sleep(a.cooldown)
                sub = max(1, a.batch_size >> rd)      # 20 → 10 → 5
                print("    重试 %d：缺失 %d 只（每批 %d 只）"
                      % (rd, len(miss), sub))
                r2, d2, e2, g2 = fetch_batch(miss, a.limit, use_types, size=sub)
                n_drop += d2
                fails.extend(e2)
                if r2:
                    n_ok += len(g2)
                    buf.extend(r2)
                miss = [c for c in miss if c not in g2]
            if miss:
                n_fail += len(miss)
                fails.append((miss[:3], "batch", "重试 %d 轮后仍缺(%d只)"
                              % (a.retry, len(miss))))
            if len(buf) >= 8000:
                flush()
            print("  批 %d/%d · 覆盖 %d 只 · 失败 %d 只 · 已写 %d 行 · 丢弃缺公告日 %d · %.0fs"
                  % (bi + 1, n_batch, n_ok, n_fail, n_rows, n_drop,
                     time.time() - t0))
            if bi + 1 < n_batch:
                time.sleep(a.cooldown)
        flush()

    print()
    print("完成：成功 %d · 失败 %d · 写入 %d 行 · 判重跳过 %d · 缺公告日丢弃 %d · %.0fs"
          % (n_ok, n_fail, n_rows, n_skip_dup, n_drop, time.time() - t0))
    if n_skip_dup:
        print("  ⚠ 判重跳过 %d 行：说明本轮抓到了已存在的数据（重抓/并发）。"
              % n_skip_dup)
        print("    已自动丢弃，未污染文件。若这个数字很大，说明 todo 快照过旧。")
    if fails:
        print("失败样例:", fails[:3])
    if a.execute:
        print("落盘:", path)
    else:
        print("（dry-run，未写盘；加 --execute 生效）")
    release_lock(lock_p)
    return 0


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

    md = """| EndDate | InfoPublDate | BasicEPS | ROE |
| --- | --- | --- | --- |
| 2025-12-31 | 2026-04-14 | 0.0267 | 0.4899 |
| 2025-09-30 | 2025-10-28 | -0.0093 | -0.1291 |
"""
    hdr, rows = parse_md_table(md)
    T("A1 解析表头", hdr[:2] == ["EndDate", "InfoPublDate"], str(hdr[:2]))
    T("A2 解析数据行数", len(rows) == 2, str(len(rows)))
    T("A3 跳过分隔行", all(r[0] != "---" for r in rows))

    recs, drop = rows_from_md("sh600178", "income", md,
                              keep=("EndDate", "InfoPublDate", "BasicEPS", "ROE"))
    T("B1 长表记录数 = 行×字段（2×2=4）", len(recs) == 4, str(len(recs)))
    T("B2 记录含真实公告日", recs[0]["publ_date"] == "2026-04-14", str(recs[0]))
    T("B3 公告日晚于报告期（年报 3.5 月后）",
      recs[0]["publ_date"] > recs[0]["end_date"])

    # 缺公告日必须整条丢弃
    md_bad = """| EndDate | InfoPublDate | BasicEPS |
| --- | --- | --- |
| 2025-12-31 |  | 0.5 |
| 2025-09-30 | 2025-10-28 | 0.3 |
"""
    r2, d2 = rows_from_md("x", "income", md_bad, keep=("BasicEPS",))
    T("C1 缺公告日的行被丢弃并计数", d2 == 1 and len(r2) == 1,
      "drop=%d rows=%d" % (d2, len(r2)))
    T("C2 保留行是公告日完整的", r2[0]["end_date"] == "2025-09-30", str(r2))

    # 空/异常输入不崩
    T("D1 空文本返回空", rows_from_md("x", "income", "")[0] == [])
    T("D2 无表头返回空", parse_md_table("no table") == ([], []))

    # 追加写不重复表头
    import tempfile
    import shutil
    d = tempfile.mkdtemp()
    tp = os.path.join(d, "f.csv")
    write_append(tp, recs[:2])
    write_append(tp, recs[2:])
    with io.open(tp, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    T("E1 追加写只一次表头",
      sum(1 for ln in lines if ln.startswith("code,")) == 1)
    T("E2 追加写行数 = 总和", len(lines) - 1 == len(recs), str(len(lines)))
    T("E3 load_done 能读回 code", "sh600178" in load_done(tp))

    # ★ E4~E6 幂等写（M22）：同一批数据写两遍，第二遍必须全跳过
    tp2 = os.path.join(d, "f2.csv")
    w1, s1 = write_append(tp2, recs)
    w2, s2 = write_append(tp2, recs)          # 原样再写一次
    with io.open(tp2, encoding="utf-8") as f:
        lines2 = [ln for ln in f.read().splitlines() if ln.strip()]
    T("E4 首次写入全落盘", w1 == len(recs) and s1 == 0,
      "w=%d skip=%d" % (w1, s1))
    T("E5 **重复写入被全部跳过**（幂等，防孤儿进程污染）",
      w2 == 0 and s2 == len(recs), "w=%d skip=%d" % (w2, s2))
    T("E6 文件行数不因重写增长", len(lines2) - 1 == len(recs),
      "%d vs %d" % (len(lines2) - 1, len(recs)))

    # E7：同 code 不同报告期必须能写进去（判重粒度不能太粗）
    one = dict(recs[0])
    alt = dict(one)
    alt["end_date"] = "1999-12-31"
    alt["publ_date"] = "2000-04-01"
    w3, s3 = write_append(tp2, [alt])
    T("E7 同票不同报告期不被误判为重复", w3 == 1 and s3 == 0,
      "w=%d skip=%d" % (w3, s3))

    # E8：可关闭判重（排障用）
    w4, s4 = write_append(tp2, recs[:1], dedup_existing=False)
    T("E8 dedup_existing=False 可强制写入（排障用）", w4 == 1 and s4 == 0,
      "w=%d skip=%d" % (w4, s4))

    shutil.rmtree(d, ignore_errors=True)

    T("F1 KEEP 表含三张表", set(TYPES) == set(KEEP))

    # ── G 组：进程锁（M21）──────────────────────────────────────────────
    d2 = tempfile.mkdtemp()
    lp = os.path.join(d2, "_lock")
    # G1 首次加锁成功
    os.makedirs(os.path.dirname(lp), exist_ok=True)
    try:
        with io.open(lp, "w", encoding="utf-8") as f:
            f.write("pid=test ts=%d\n" % int(time.time()))
        stale = (time.time() - os.path.getmtime(lp)) >= LOCK_STALE_SEC
        T("G1 新建锁未过期（视为活动锁）", not stale)
    finally:
        shutil.rmtree(d2, ignore_errors=True)

    _root2 = tempfile.mkdtemp()
    os.makedirs(os.path.join(_root2, "data", "state"), exist_ok=True)
    okA, whyA, pA = acquire_lock(_root2)
    T("G2 首次 acquire_lock 成功", okA and os.path.isfile(pA), whyA)
    okB, whyB, _ = acquire_lock(_root2)          # 第二次应被拒
    T("G3 **第二次 acquire 被拒**（防并发写同一数据区）",
      not okB and "已有采集进程" in whyB, whyB[:60])
    touch_lock(pA)
    T("G4 touch_lock 刷新时间戳不报错", os.path.isfile(pA))
    release_lock(pA)
    T("G5 release_lock 删掉锁文件", not os.path.isfile(pA))

    # G6 僵死锁可被覆盖
    okC, _, pC = acquire_lock(_root2)
    old = time.time() - LOCK_STALE_SEC - 10
    os.utime(pC, (old, old))
    okD, whyD, _ = acquire_lock(_root2)
    T("G6 **僵死锁可被覆盖**（上次崩溃不会永久堵死）", okD, whyD[:60])
    release_lock(pC)
    shutil.rmtree(_root2, ignore_errors=True)

    # G7 单次运行上限必须存在且有值
    T("G7 运行上限 > 0（防 18 小时无人看管）", MAX_RUNTIME_SEC > 0,
      str(MAX_RUNTIME_SEC))

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
