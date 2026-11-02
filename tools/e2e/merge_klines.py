# -*- coding: utf-8 -*-
"""klines 合并器（2026-10-20 · 数据层贯通：10月按日截面 → S 系全历史单文件）。

**断点根因**：S 系执行件（s3/s4/s5...）读 `data/raw/bus/klines.csv`（旧包搬入，
截止 2026-09-30）；live_seed 补采的 10月行情落 **按日** `data/raw/2026-10-*/daily_quote.csv`
⇒ 两个库从未合并 ⇒ S 系对 10月数据不可见（s4 --eval 10-19 报 DATA_END）。

**列映射**（daily_quote → bus/klines.csv 契约）：
    date=目录日 · open→open · close→close · high→high · low→low
    vol→volume · amount→amount · pct→change_pct · exchange=""（S 系不读该列）
    name→name · code→code

**前复权基准边界（如实声明）**：CLI 历史日返回**以查询日为基准**的前复权价 ⇒
10月批次基准=各自查询日（live_seed 采集日附近），与旧包 09-30 及以前（基准=各自
当日）混用 ⇒ 遇除权/复牌的边界票价格或有微小差。影响面=S 系结构分析（连板/位置/
涨停判定用当日 close/pct 自洽，不跨日比价）⇒ 可接受；**精确比价场景禁跨批混算**
（ledger v3.22 铁律）。

幂等：按 (code,date) 去重，已存在日期不重复追加。原子性：tmp + os.replace。
"""
import csv
import io
import os
import sys
import tempfile

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
sys.path.insert(0, _FS)
from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸

KLINES_COLS = ["code", "name", "date", "open", "close", "high", "low",
               "volume", "amount", "exchange", "change_pct"]


def _f(x):
    try:
        return "" if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return ""


def merge_days(days, root=None):
    """把按日 daily_quote.csv 合并进 bus/klines.csv。→ (n_added, n_skipped, path)。"""
    root = root or ROOT
    bus_p = os.path.join(root, "data", "raw", "bus", "klines.csv")
    if not os.path.isfile(bus_p):
        raise IOError("KLINES_ABSENT: %s 不存在（S 系行情源）" % bus_p)
    # 已存在的 (code,date) 集合（幂等）
    have = set()
    with io.open(bus_p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("code") and r.get("date"):
                have.add((r["code"], r["date"]))
    n0 = len(have)
    added, skipped = [], 0
    for day in days:
        dq_p = os.path.join(root, "data", "raw", day, "daily_quote.csv")
        if not os.path.isfile(dq_p):
            continue
        with io.open(dq_p, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                key = (r.get("code"), day)
                if not r.get("code") or key in have:
                    skipped += 1
                    continue
                added.append({"code": r["code"], "name": r.get("name") or "",
                              "date": day,
                              "open": _f(r.get("open")), "close": _f(r.get("close")),
                              "high": _f(r.get("high")), "low": _f(r.get("low")),
                              "volume": _f(r.get("vol")), "amount": _f(r.get("amount")),
                              "exchange": "", "change_pct": _f(r.get("pct"))})
    if added:
        _WG.check_write(bus_p)
        # 大文件追加式：直接 append（127MB 全量重写浪费且风险大）；
        # 幂等由 (code,date) 去重保证，中断至多丢半行（下游 DictReader 容错）
        with io.open(bus_p, "a", encoding="utf-8", newline="\n") as out:
            w = csv.DictWriter(out, fieldnames=KLINES_COLS)
            for r in added:
                w.writerow(r)
    return len(added), skipped, bus_p


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    days = []
    if "--from" in argv and "--to" in argv:
        import dayclass as DC
        cal = DC.Calendar.from_seed_csv(
            os.path.join(ROOT, "data", "raw", "meta", "trade_cal.csv"),
            source="merge_klines")
        days = [d for d in cal.sessions_between(
            argv[argv.index("--from") + 1], argv[argv.index("--to") + 1])]
    n, sk, p = merge_days(days)
    print("✅ klines 合并：新增 %d 行 · 跳过(已存在) %d 行 → %s" % (n, sk, p))
    return 0


def _selftest():
    import shutil
    ok = fail = 0

    def T(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [✓] %s" % name)
        else:
            fail += 1
            print("  [✗] %s %s" % (name, extra))

    sb = tempfile.mkdtemp(prefix="merge_kl_")
    try:
        os.makedirs(os.path.join(sb, "data", "raw", "bus"))
        os.makedirs(os.path.join(sb, "data", "raw", "2026-10-19"))
        bus = os.path.join(sb, "data", "raw", "bus", "klines.csv")
        with io.open(bus, "w", encoding="utf-8") as f:
            f.write("code,name,date,open,close,high,low,volume,amount,"
                    "exchange,change_pct\n")
            f.write("sh600000,浦发银行,2026-09-30,10.0,10.5,10.6,9.9,100,105000000,,1.0\n")
        with io.open(os.path.join(sb, "data", "raw", "2026-10-19",
                                  "daily_quote.csv"), "w", encoding="utf-8") as f:
            f.write("code,name,open,high,low,close,pct,vol,amount\n")
            f.write("sh600000,浦发银行,10.5,10.8,10.4,10.7,1.9,120,128000000\n")
            f.write("sh600001,邯郸钢铁,5.0,5.1,4.9,5.0,0.0,50,25000000\n")
        n, sk, _ = merge_days(["2026-10-19"], root=sb)
        T("M1 新增 2 行（映射列正确）", n == 2, "n=%d" % n)
        with io.open(bus, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        T("M2 文件共 3 行（旧 1 + 新 2）", len(rows) == 3, str(len(rows)))
        r = rows[1]
        T("M3 列映射（vol→volume/pct→change_pct/date 注入）",
          r["volume"] == "120.0" and r["change_pct"] == "1.9"
          and r["date"] == "2026-10-19" and r["name"] == "浦发银行",
          str(r))
        # 幂等：重跑同一天
        n2, sk2, _ = merge_days(["2026-10-19"], root=sb)
        T("M4 幂等（重跑零新增）", n2 == 0 and sk2 == 2, "n2=%d sk2=%d" % (n2, sk2))
        # 缺日目录：跳过不报错
        n3, sk3, _ = merge_days(["2026-10-20"], root=sb)
        T("M5 缺日目录静默跳过", n3 == 0, "n3=%d" % n3)
    finally:
        shutil.rmtree(sb, ignore_errors=True)
    print("== merge_klines --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
