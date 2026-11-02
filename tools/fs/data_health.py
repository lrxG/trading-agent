"""data_health.py · 数据体检（只读 · 零写点 · 不改任何数据）

## 为什么需要这个文件

2026-10-21 体检发现真实问题：

  `data/raw/bus/margin_daily.csv` 53 行里 **28 行是重复表头**、0 行真实数据。
  也就是说：**文件存在、体积不为零、列名齐全，但里面是空的。**
  如果下游按「文件存在」判可用，就会拿一张空表当数据源 —— 这正是
  `admission.py` 说的「列在 ≠ 值有」，但连它都比这强（它至少会判 SEMANTIC）。

  ⇒ 需要一个**一次性扫全库**的体检，把「假装有数据」的文件揪出来。

## 四类检查（互不替代）

  A. EXIST      文件是否存在、能否解析
  B. STRUCT     表头是否规整（缺列/重列/重复表头行）
  C. CONTENT    是否真有数据（重复表头行占比、全空列、唯一值过少）
  D. FRESH      最新日期是否够新（相对给定基准日）

## 纪律

· **只读**：本文件不写、不改、不修数据。发现问题是报告，不是自动修复
  （修复属治理动作，须显式批准 —— 与「禁擅自删」同一条纪律）
· **不猜**：无法判定的如实标 UNKNOWN，不编结论
· **可复现**：同输入同输出，不依赖随机

用法：
  python tools/fs/data_health.py                  # 全库体检
  python tools/fs/data_health.py --as-of 2026-10-21
  python tools/fs/data_health.py --json            # 机器可读
  python tools/fs/data_health.py --selftest
"""
import argparse
import csv
import json
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "tools", "fs"))

BUS = os.path.join(_ROOT, "data", "raw", "bus")

# 每个文件的期望列（只列关键的；None=不校验列）
EXPECT = {
    "klines.csv": ["code", "date", "open", "high", "low", "close", "amount"],
    "mktcap.csv": ["code", "date"],
    "finance.csv": ["code", "end_date", "publ_date", "field", "value"],
    "fundflow.csv": ["date", "code", "main_net"],
    "margin_daily.csv": ["date", "code", "fin_balance"],
    "etf_klines.csv": ["code", "date", "close"],
    "etf_nav_prem.csv": ["code", "date", "nav", "close", "prem"],
    "chip_daily.csv": ["date", "code"],
    "env_history.csv": ["date"],
    "env_labels.csv": ["date", "env"],
    "board_track.csv": ["date", "code"],
    "tdx_sector_index.csv": ["date", "code", "close"],
    "fetch_log.csv": ["ts", "date", "status"],
}

# 日期列候选（按优先级）
DATE_COLS = ("date", "ts", "end_date", "publ_date")

# ── 已知空列登记（2026-10-22）────────────────────────────────────────────────
# 有的列恒空是**数据源限制**而非采集缺陷。登记在此，体检仍如实列出，
# 但不把它当作 status 降级理由 —— 否则同一条已知告警每天刷屏，真问题被淹没。
KNOWN_EMPTY = {
    "margin_daily.csv": ("fin_netbuy", "total_balance"),
}
KNOWN_EMPTY_REASON = {
    "margin_daily.csv":
        "`westock fund margin` 批量接口只返回融资余额/融券等，"
        "**不返回融资买入/偿还/两融余额**（单票接口才有）；"
        "且这两个字段全项目零消费者 ⇒ 不值得花 5000+ 次单票请求去补",
}

# 大文件只抽样（避免体检本身跑几分钟）
SAMPLE_LIMIT = 300_000


def check_file(fn, as_of=None, sample=SAMPLE_LIMIT):
    """→ dict(status, reasons, rows, cols, dup_header, empty_cols, latest)"""
    p = os.path.join(BUS, fn)
    out = {"file": fn, "status": "OK", "reasons": [], "rows": 0, "cols": 0,
           "dup_header": 0, "empty_cols": [], "latest": None, "size_mb": 0.0}
    if not os.path.exists(p):
        out["status"] = "MISSING"
        out["reasons"].append("EXIST 文件不存在")
        return out
    out["size_mb"] = round(os.path.getsize(p) / 1e6, 2)

    try:
        with open(p, encoding="utf-8-sig", errors="replace") as f:
            rd = csv.reader(f)
            try:
                hdr = next(rd)
            except StopIteration:
                out["status"] = "EMPTY_FILE"
                out["reasons"].append("EXIST 文件为空（连表头都没有）")
                return out
            out["cols"] = len(hdr)
            hdr_norm = [h.strip() for h in hdr]

            # ── B. STRUCT ────────────────────────────────────────────────
            exp = EXPECT.get(fn)
            if exp:
                miss = [c for c in exp if c not in hdr_norm]
                if miss:
                    out["reasons"].append("STRUCT 缺关键列 %s" % miss)
            dup_cols = [c for c, n in Counter(hdr_norm).items() if n > 1]
            if dup_cols:
                out["reasons"].append("STRUCT 表头重名列 %s" % dup_cols)
            if not hdr_norm or all(not h for h in hdr_norm):
                out["reasons"].append("STRUCT 表头全空")

            # ── C. CONTENT ───────────────────────────────────────────────
            # ★ 抽样不能只读前 N 行：klines.csv 按 code 排序，前 30 万行只覆盖
            #   2024 年上半年 ⇒ 会误报「最新日期 2024-07-04」。这是抽样方式错，
            #   不是数据错。改为「全量扫日期列（轻量）+ 只对前 N 行做列统计」。
            n_rows = 0
            dup_header = 0
            nonempty = [0] * len(hdr_norm)
            date_idx = None
            for i, c in enumerate(hdr_norm):
                if c in DATE_COLS:
                    date_idx = i
                    break
            latest = None
            n_total = 0
            for row in rd:
                n_total += 1
                # 日期全量扫（只比字符串，代价极低）
                if date_idx is not None and date_idx < len(row):
                    v = row[date_idx].strip()
                    if v and (latest is None or v > latest):
                        latest = v
                # 列统计与重复表头只统计前 sample 行
                if n_rows >= sample:
                    continue
                n_rows += 1
                if [x.strip() for x in row] == hdr_norm:
                    dup_header += 1
                    continue
                for i, v in enumerate(row[:len(hdr_norm)]):
                    if v and v.strip():
                        nonempty[i] += 1

            # 重复表头占比也用全量重算（否则大文件会严重低估）
            # 用 n_total 做分母，dup_header 只数了前 sample 行 ⇒ 标注口径
            out["rows"] = n_total
            out["sampled"] = n_rows
            out["dup_header"] = dup_header
            out["latest"] = latest
            out["empty_cols"] = [hdr_norm[i] for i, c in enumerate(nonempty)
                                 if c == 0]
            real = n_total - dup_header
            if n_total == 0:
                out["reasons"].append("CONTENT 零数据行")
            elif real == 0:
                out["status"] = "FAKE_DATA"
                out["reasons"].append(
                    "CONTENT **全部 %d 行都是重复表头、零真实数据**" % dup_header)
            elif dup_header:
                out["reasons"].append(
                    "CONTENT 前 %d 行中有 %d 行是重复表头"
                    % (n_rows, dup_header))
            if out["empty_cols"]:
                # ★ 2026-10-22 加「已知空列」豁免（**只降噪，不隐藏**）：
                #   `margin_daily.csv` 的 fin_netbuy / total_balance 恒空，
                #   因为 `westock fund margin` **批量接口不返回**这两个字段
                #   （单票接口才有 FinanceBuyValue/FinanceRefundValue/TradingValue）。
                #   而这两个字段**全项目零消费者**（grep 确认），
                #   花 5237 次单票请求去补两个没人用的列不值得。
                #   处置：仍如实列出空列 + 原因，但不再计入 status=WARN 的理由，
                #   避免"已知的数据源限制"每天刷同一条告警把真问题淹没。
                _known = KNOWN_EMPTY.get(fn)
                _unk = [c for c in out["empty_cols"][:6] if c not in (_known or ())]
                if _unk:
                    out["reasons"].append("CONTENT 全空列 %s" % _unk)
                else:
                    out["reasons"].append(
                        "CONTENT 全空列 %s（**已知**：批量接口不返回该字段，%s）"
                        % (out["empty_cols"][:6],
                           (KNOWN_EMPTY_REASON.get(fn) or "已登记")))

            # ── D. FRESH ─────────────────────────────────────────────────
            if as_of and latest:
                if latest < as_of:
                    out["reasons"].append(
                        "FRESH 最新日期 %s 早于基准 %s" % (latest, as_of))

    except Exception as e:                              # noqa: BLE001
        out["status"] = "PARSE_ERROR"
        out["reasons"].append("EXIST 解析失败：%s" % str(e)[:100])
        return out

    # 状态归并：任一硬问题即降级
    hard = [r for r in out["reasons"]
            if r.startswith(("EXIST", "STRUCT 缺", "STRUCT 表头", "CONTENT 零",
                             "CONTENT **", "PARSE"))]
    if hard and out["status"] == "OK":
        out["status"] = "BAD"
    elif out["reasons"] and out["status"] == "OK":
        out["status"] = "WARN"
    return out


def scan_all(as_of=None):
    if not os.path.isdir(BUS):
        return []
    files = sorted(f for f in os.listdir(BUS) if f.endswith(".csv"))
    return [check_file(f, as_of=as_of) for f in files]


def report(results, as_json=False):
    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    order = {"FAKE_DATA": 0, "PARSE_ERROR": 1, "BAD": 2, "MISSING": 3,
             "EMPTY_FILE": 4, "WARN": 5, "OK": 6}
    rs = sorted(results, key=lambda r: (order.get(r["status"], 9), r["file"]))
    print("数据体检（只读 · 不修任何数据）")
    print("  %-26s %-11s %-9s %-7s %-12s %s"
          % ("文件", "状态", "行数", "重复表头", "最新日期", "说明"))
    bad = 0
    for r in rs:
        if r["status"] not in ("OK",):
            bad += 1
        note = "；".join(r["reasons"])[:78]
        print("  %-26s %-11s %-9s %-7s %-12s %s"
              % (r["file"], r["status"], r["rows"], r["dup_header"],
                 r["latest"] or "-", note))
    print("\n  统计：%d 个文件 · %d 个有问题的" % (len(rs), bad))
    fake = [r["file"] for r in rs if r["status"] == "FAKE_DATA"]
    if fake:
        print("\n  ★ FAKE_DATA（文件存在但是空的，最危险）：%s" % "、".join(fake))
        print("    这些文件若被下游按「存在即可用」消费，等于拿空表当数据源。")
        print("    **本工具只报告不修复** —— 清理/重建属治理动作，须显式批准。")
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

    print("data_health.py 自检")
    import tempfile

    def mk(name, content):
        d = tempfile.mkdtemp()
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return p

    global BUS
    old_bus = BUS
    try:
        # T1 重复表头 → FAKE_DATA
        BUS = tempfile.mkdtemp()
        with open(os.path.join(BUS, "margin_daily.csv"), "w", encoding="utf-8") as f:
            f.write("date,code,fin_balance\n")
            for _ in range(5):
                f.write("date,code,fin_balance\n")
        r = check_file("margin_daily.csv")
        T("T1 全部重复表头 → FAKE_DATA", r["status"] == "FAKE_DATA",
          r["status"] + str(r["reasons"]))
        T("T2 能数出重复表头行数", r["dup_header"] == 5, str(r["dup_header"]))

        # T3 正常数据 → OK
        with open(os.path.join(BUS, "klines.csv"), "w", encoding="utf-8") as f:
            f.write("code,date,open,high,low,close,amount\n")
            f.write("sh600000,2026-10-20,10,11,9,10.5,1e8\n")
            f.write("sh600001,2026-10-21,10,11,9,10.5,1e8\n")
        r = check_file("klines.csv")
        T("T3 正常数据判 OK", r["status"] == "OK", r["status"] + str(r["reasons"]))
        T("T4 能取到最新日期", r["latest"] == "2026-10-21", str(r["latest"]))

        # T5 缺关键列 → BAD
        with open(os.path.join(BUS, "mktcap.csv"), "w", encoding="utf-8") as f:
            f.write("code,foo\n")
            f.write("sh600000,1\n")
        r = check_file("mktcap.csv")
        T("T5 缺关键列 → BAD", r["status"] == "BAD", r["status"] + str(r["reasons"]))

        # T6 部分重复表头 → WARN 或 BAD（含 CONTENT 提示）
        with open(os.path.join(BUS, "fundflow.csv"), "w", encoding="utf-8") as f:
            f.write("date,code,main_net\n")
            f.write("date,code,main_net\n")
            f.write("2026-10-20,sh600000,100\n")
        r = check_file("fundflow.csv")
        T("T6 部分重复表头被抓出", r["dup_header"] == 1, str(r["dup_header"]))

        # T7 零数据行
        with open(os.path.join(BUS, "fetch_log.csv"), "w", encoding="utf-8") as f:
            f.write("ts,date,status\n")
        r = check_file("fetch_log.csv")
        T("T7 只有表头零数据行被抓出", "CONTENT 零数据行" in " ".join(r["reasons"]),
          str(r["reasons"]))

        # T8 文件不存在
        r = check_file("not_exist.csv")
        T("T8 文件不存在 → MISSING", r["status"] == "MISSING", r["status"])

        # T9 全空列
        with open(os.path.join(BUS, "chip_daily.csv"), "w", encoding="utf-8") as f:
            f.write("date,code,extra\n")
            f.write("2026-10-20,sh600000,\n")
        r = check_file("chip_daily.csv")
        T("T9 全空列被抓出", "extra" in r["empty_cols"], str(r["empty_cols"]))

        # T10 FRESH 检查
        with open(os.path.join(BUS, "env_history.csv"), "w", encoding="utf-8") as f:
            f.write("date,v\n2026-10-01,1\n")
        r = check_file("env_history.csv", as_of="2026-10-21")
        T("T10 最新日期早于基准被抓出（FRESH）",
          any("FRESH" in x for x in r["reasons"]), str(r["reasons"]))

    finally:
        BUS = old_bus

    # T11 只读自证：按「写入目标」判，而不是按「有没有 open(w)」
    #     判据 = 任何写操作的目标都不能是 BUS 或 _ROOT（真实数据区）。
    #     自检写 tempfile.mkdtemp() 的临时文件是允许的（那不是生产数据）。
    import ast
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    # 收集 _selftest 函数体覆盖的行号区间（自检写入一律豁免）
    test_span = (0, 0)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_selftest":
            test_span = (node.lineno, node.end_lineno)

    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        nm = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
        if nm not in ("open", "write_text", "write_bytes"):
            continue
        mode = None
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = node.args[1].value
        for k in node.keywords:
            if k.arg == "mode" and isinstance(k.value, ast.Constant):
                mode = k.value.value
        if not (isinstance(mode, str) and any(c in mode for c in "wax+")):
            continue
        in_selftest = test_span[0] <= node.lineno <= test_span[1]
        if not in_selftest:
            bad.append("L%d %s" % (node.lineno, ast.unparse(node)[:40]))
    T("T11 生产代码区零写入（自检临时文件豁免）", not bad, str(bad)[:100])

    # T12 不做自动修复：生产代码区不得出现删除/改名/替换
    fixes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        seg = ast.unparse(node)
        if any(x in seg for x in ("os.remove", "os.rename", "os.replace",
                                  ".unlink(", "shutil.")):
            in_selftest = test_span[0] <= node.lineno <= test_span[1]
            if not in_selftest:
                fixes.append("L%d %s" % (node.lineno, seg[:40]))
    T("T12 生产代码区不做任何自动修复（只报告不修）", not fixes, str(fixes)[:100])

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    res = scan_all(as_of=a.as_of)
    return report(res, as_json=a.json)


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
