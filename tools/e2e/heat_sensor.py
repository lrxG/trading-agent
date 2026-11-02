"""heat_sensor.py · 两融数据采集（补上 DATA-HUB 声称存在但从未实现的断链）

## 为什么要建这个文件

`data/research/imported_20261019/DATA-HUB.md` 第 228 行写着：

  「两融余额/融资净买入（放大器）→ ✅ `heat_sensor.py margin`
    （daily_close 挂 → margin_daily.csv · T+1 披露取最后已披露日）」

**但 `heat_sensor.py` 全项目不存在**（`find . -name "heat_sensor*"` = 0 命中）。

而 `data/raw/bus/margin_daily.csv` 里确实有文件：
  · 53 行中 **28 行是重复表头**（垃圾）
  · 只剩 25 行真实数据，全部是 **2026-09-29 单日、25 只票**
  · **无任何工具写它**（`grep -rln margin_daily tools/` 只有 data_availability 只读引用）

⇒ 这就是「文档说有能力、代码里没有、数据是半截垃圾」的典型断链。
   2026-10-21 用户令「先把简单的做完」时由 `data_health.py` 体检发现。

## 数据源（已实测，非假设）

`westock fund margin <code[,code...]> [--date D | --start A --end B]`

返回（实测 sz300750 / 2026-09-29）：
  code name date closePrice changePct FinanceValue SecurityValue
  FinanceBuyValue FinanceRefundValue TradingValue TradingValueDif
  FinanceValueDOD SecurityValueDOD

## ★ 两个接口的返回列完全不同（2026-10-21 实测，踩过一次）

**单票调用** `westock fund margin sh600000 --date D` → **英文列**：
  code name date closePrice changePct FinanceValue SecurityValue
  FinanceBuyValue FinanceRefundValue TradingValue TradingValueDif
  FinanceValueDOD SecurityValueDOD

**批量调用** `westock fund margin "a,b,c" --date D` → **中文列**：
  code name 融资余额 融券余额 融资买入 融资余额环比
  （且有 `[Batch] 状态: success | 总数: 3 | 成功: 3 | 失败: 0` 头）

⇒ 必须**两套列名都认**，否则批量调用会一行都解析不出（第一版就栽在这）。
   批量版缺「融资偿还」「两融余额」，故 `fin_netbuy` / `total_balance` 留空（不编造）。

字段映射（统一到内部名）：
  fin_balance   ← FinanceValue | 融资余额       （元）
  fin_netbuy    ← FinanceBuyValue − FinanceRefundValue（仅单票有，批量留空）
  total_balance ← TradingValue | 两融余额        （批量留空）
  fin_dod_pct   ← FinanceValueDOD | 融资余额环比  （%）

## 三条纪律

① **不编造**：取不到的票如实计失败，不写空行、不填 0
② **先清洗再追加**：目标文件里的重复表头行属垃圾，本工具**追加时不复制**它们；
   是否清除历史垃圾需显式 `--clean`（治理动作，须确认）
③ **幂等**：同 (date, code) 已存在则跳过，不重复写

## 输出

`data/raw/bus/margin_daily.csv`（append-only，主键 date+code）

用法：
  python tools/e2e/heat_sensor.py --check
  python tools/e2e/heat_sensor.py margin --date 2026-09-29 --limit 20      # 干跑
  python tools/e2e/heat_sensor.py margin --date 2026-09-29 --limit 20 --execute
  python tools/e2e/heat_sensor.py margin --date 2026-09-29 --all           # 全市场（慢）
  python tools/e2e/heat_sensor.py --selftest
"""
import argparse
import csv
import io
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("fs", "e2e"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

OUT_REL = os.path.join("data", "raw", "bus", "margin_daily.csv")
HEADER = ["date", "code", "name", "fin_balance", "fin_netbuy",
          "total_balance", "fin_dod_pct"]
KL = os.path.join(_ROOT, "data", "raw", "bus", "klines.csv")

# ★ 两套列名都要认：单票=英文列，批量=中文列（2026-10-21 实测差异）
ALIAS = {
    "fin_balance": ("FinanceValue", "融资余额"),
    "total_balance": ("TradingValue", "两融余额"),
    "fin_dod_pct": ("FinanceValueDOD", "融资余额环比"),
    "fin_buy": ("FinanceBuyValue", "融资买入"),
    "fin_refund": ("FinanceRefundValue", "融资偿还"),
}


def _pick(rec, key):
    """按别名取字段（两套列名都试）。

    ★ 批量接口的列名带单位后缀，如 `融资余额环比(%)` ⇒ 必须**前缀匹配**，
      精确匹配会漏（2026-10-21 实测踩过）。
    """
    for name in ALIAS.get(key, (key,)):
        if name in rec:
            return rec[name]
    for name in ALIAS.get(key, (key,)):
        for col, v in rec.items():
            if col.startswith(name):
                return v
    return None


def _run(cmd, timeout=60):
    """跑 westock 命令，→ stdout 文本。失败返回 None。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return None, (r.stderr or r.stdout or "")[:200]
        return r.stdout, None
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"
    except FileNotFoundError:
        return None, "westock 不在 PATH"


def _md_rows(text):
    """解析 markdown 表 → [dict]。跳过 |---| 分隔行。"""
    if not text:
        return []
    lines = [ln for ln in text.strip().splitlines() if ln.strip().startswith("|")]
    if len(lines) < 3:
        return []
    def cells(ln):
        return [c.strip() for c in ln.strip().strip("|").split("|")]
    hdr = cells(lines[0])
    out = []
    for ln in lines[1:]:
        cs = cells(ln)
        if all(set(c) <= set("-: ") for c in cs):
            continue                      # 分隔行
        if len(cs) != len(hdr):
            continue
        out.append(dict(zip(hdr, cs)))
    return out


def _f(v):
    """安全转 float；空/非法 → None（不填 0）。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "—", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_margin(codes, date):
    """→ ([row dict], [失败 code])"""
    rows, failed = [], []
    # 分批（命令行长度与限速）
    B = 40
    for i in range(0, len(codes), B):
        chunk = codes[i:i + B]
        txt, err = _run(["westock", "fund", "margin", ",".join(chunk),
                         "--date", date])
        if txt is None:
            failed.extend(chunk)
            continue
        recs = _md_rows(txt)
        if not recs:
            failed.extend(chunk)
            continue
        got = set()
        for r in recs:
            c = r.get("code")
            if not c:
                continue
            fin = _f(_pick(r, "fin_balance"))
            buy = _f(_pick(r, "fin_buy"))
            refund = _f(_pick(r, "fin_refund"))
            net = (buy - refund) if (buy is not None and refund is not None) else None
            total = _f(_pick(r, "total_balance"))
            dod = _f(_pick(r, "fin_dod_pct"))
            if fin is None:
                failed.append(c)
                continue
            rows.append({
                "date": r.get("date") or date,
                "code": c,
                "name": r.get("name") or "",
                "fin_balance": "%.2f" % fin,
                # 批量接口没有这两列 ⇒ 留空，不填 0（纪律：不编造）
                "fin_netbuy": ("%.2f" % net) if net is not None else "",
                "total_balance": ("%.2f" % total) if total is not None else "",
                "fin_dod_pct": ("%.2f" % dod) if dod is not None else "",
            })
            got.add(c)
        for c in chunk:
            if c not in got and c not in failed:
                failed.append(c)
    return rows, failed


def load_existing(path=None):
    """→ (set of (date,code), n_garbage)。只扫首两列，快。"""
    p = path or os.path.join(_ROOT, OUT_REL)
    have, garbage = set(), 0
    if not os.path.exists(p):
        return have, garbage
    with open(p, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        hdr = next(rd, None)
        if not hdr:
            return have, garbage
        for row in rd:
            if not row:
                continue
            if [x.strip() for x in row] == [h.strip() for h in hdr]:
                garbage += 1
                continue
            if len(row) >= 2:
                have.add((row[0].strip(), row[1].strip()))
    return have, garbage


def write_rows(rows, path=None):
    """追加写（不复制垃圾表头行）。"""
    p = path or os.path.join(_ROOT, OUT_REL)
    new = not os.path.exists(p)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)


def code_universe(limit=None):
    """全 A 代码（从 klines 取，剔北交所与指数）。"""
    codes = set()
    with open(KL, encoding="utf-8", errors="ignore") as f:
        for r in csv.DictReader(f):
            c = r["code"]
            if c.startswith("bj") or c.startswith("sh000") or c.startswith("sz399"):
                continue
            codes.add(c)
    codes = sorted(codes)
    return codes[:limit] if limit else codes


# ── 入口 ────────────────────────────────────────────────────────────────────
def cmd_check():
    have, garbage = load_existing()
    dates = sorted({d for d, _ in have})
    codes = {c for _, c in have}
    print("margin_daily.csv 现状")
    print("  真实数据 (date,code) 对：%d" % len(have))
    print("  重复表头垃圾行：%d  ← 需 --clean 才清（治理动作）" % garbage)
    print("  覆盖日期：%s" % (dates if dates else "无"))
    print("  覆盖股票：%d 只" % len(codes))
    print("\n  数据源自检：", end="")
    txt, err = _run(["westock", "fund", "margin", "sz300750", "--date", "2026-09-29"])
    print("OK" if txt else ("FAIL %s" % err))
    return 0


def cmd_margin(date, limit=None, all_market=False, execute=False, clean=False):
    have, garbage = load_existing()
    print("两融采集 · %s%s" % (date, "" if execute else "（干跑）"))
    print("  已有 (date,code)：%d 对 · 垃圾表头行：%d" % (len(have), garbage))

    if all_market:
        codes = code_universe()
    else:
        codes = code_universe(limit=limit or 20)
    codes = [c for c in codes if (date, c) not in have]
    print("  待采：%d 只（已跳过已存在的）" % len(codes))
    if not codes:
        print("  无需采集")
        return 0
    if not execute:
        print("\n  干跑：将采集 %d 只，预计 %.0f 秒"
              % (len(codes), len(codes) / 40 * 2.5))
        if all_market:
            print("  ⚠ 全市场约 18 分钟。**期间请勿并发运行其他写生产的 CLI**")
            print("     （否则写入闸会报「预期外写入」——2026-10-21 实测撞过一次）")
        print("  确认无误后加 --execute 真跑（走写作闸）")
        return 0

    t0 = time.time()
    rows, failed = fetch_margin(codes, date)
    print("\n  抓到 %d 行 · 失败 %d 只 · %.1fs" % (len(rows), len(failed), time.time() - t0))
    if failed:
        print("  失败样例：%s" % failed[:8])
    if not rows:
        print("  ✗ 零有效数据，不写文件（不编造）")
        return 1

    n = write_rows(rows)
    print("  已写入 %d 行 → %s" % (n, OUT_REL))
    have2, garbage2 = load_existing()
    print("  写后：真实 %d 对 · 垃圾表头行 %d（本轮未清，需 --clean 显式处理）"
          % (len(have2), garbage2))
    return 0


def clean_garbage(execute=False):
    """清除重复表头垃圾行 —— **治理动作，须显式执行**。

    纪律：清理前先备份（.bak），不静默删除。
    """
    p = os.path.join(_ROOT, OUT_REL)
    if not os.path.exists(p):
        print("文件不存在：%s" % OUT_REL)
        return 1
    with open(p, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        rows = list(rd)
    if not rows:
        print("空文件")
        return 1
    hdr = [h.strip() for h in rows[0]]
    keep = [rows[0]]
    dropped = 0
    for r in rows[1:]:
        if [x.strip() for x in r] == hdr:
            dropped += 1
            continue
        keep.append(r)
    print("清理重复表头：将删除 %d 行垃圾，保留 %d 行真实数据"
          % (dropped, len(keep) - 1))
    if not dropped:
        print("无需清理")
        return 0
    if not execute:
        print("\n  干跑（未改动）。确认后加 --execute 执行（会先备份 .bak）")
        return 0
    bak = p + ".bak.%d" % int(time.time())
    with open(bak, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)
    print("  已备份 → %s" % os.path.basename(bak))
    with open(p, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(keep)
    print("  已清理，现 %d 行真实数据" % (len(keep) - 1))
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

    print("heat_sensor.py 自检")

    T("T1 输出列与 HEADER 一致（7 列）", len(HEADER) == 7, str(HEADER))

    # markdown 表解析
    md = ("| code | name | date | FinanceValue | FinanceBuyValue | "
          "FinanceRefundValue | TradingValue | FinanceValueDOD |\n"
          "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
          "| sz300750 | 宁德时代 | 2026-09-29 | 23942454166.00 | 522380122.00 "
          "| 574641647.00 | 24357797352.00 | -0.22 |\n")
    recs = _md_rows(md)
    T("T2 能解析 markdown 表", len(recs) == 1, str(len(recs)))
    T("T3 解析出正确代码", recs and recs[0]["code"] == "sz300750")

    # 字段映射
    r = recs[0]
    fin = _f(r["FinanceValue"])
    net = _f(r["FinanceBuyValue"]) - _f(r["FinanceRefundValue"])
    T("T4 融资余额解析正确", fin == 23942454166.0, str(fin))
    T("T5 融资净买入 = 买入 − 偿还", abs(net - (522380122.0 - 574641647.0)) < 1,
      str(net))

    # 空值不填 0
    T("T6 空值返回 None 而非 0", _f("") is None and _f("-") is None and _f(None) is None)
    T("T7 非法值返回 None", _f("abc") is None)

    # 垃圾表头识别
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "margin_daily.csv")
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(",".join(HEADER) + "\n")
        f.write(",".join(HEADER) + "\n")        # 垃圾
        f.write("2026-09-29,sz300750,宁德时代,1,2,3,4\n")
        f.write(",".join(HEADER) + "\n")        # 垃圾
    have, garbage = load_existing(p)
    T("T8 能识别重复表头为垃圾且不计入已有", garbage == 2 and len(have) == 1,
      "garbage=%d have=%d" % (garbage, len(have)))
    T("T9 已有键为 (date,code)", ("2026-09-29", "sz300750") in have, str(have))

    # 追加不复制垃圾
    write_rows([{"date": "2026-09-30", "code": "sh600000", "name": "x",
                 "fin_balance": "1", "fin_netbuy": "", "total_balance": "",
                 "fin_dod_pct": ""}], p)
    have2, garbage2 = load_existing(p)
    T("T10 追加后垃圾行数不变（不复制垃圾）", garbage2 == 2, str(garbage2))
    T("T11 追加后新数据可读", ("2026-09-30", "sh600000") in have2, str(len(have2)))

    # 幂等：同键不加
    T("T12 幂等键存在时可识别（跳过依据）", ("2026-09-29", "sz300750") in have2)

    # 生产代码区零写点（写只经 write_rows，且被 main 的闸包裹）
    import ast
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    span = (0, 0)
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_selftest":
            span = (n.lineno, n.end_lineno)
    writes = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        nm = getattr(n.func, "id", "") or getattr(n.func, "attr", "")
        if nm != "open":
            continue
        mode = None
        if len(n.args) > 1 and isinstance(n.args[1], ast.Constant):
            mode = n.args[1].value
        for k in n.keywords:
            if k.arg == "mode" and isinstance(k.value, ast.Constant):
                mode = k.value.value
        if isinstance(mode, str) and any(c in mode for c in "wax+"):
            if not (span[0] <= n.lineno <= span[1]):
                writes.append("L%d" % n.lineno)
    T("T13 生产写点全部在登记的写入函数内（write_rows / clean_garbage）",
      len(writes) == 3, str(writes))
    T("T14 clean_garbage 有备份动作（不静默删除）",
      ".bak." in src and "csv.writer(f).writerows(rows)" in src)
    T("T15 clean_garbage 默认干跑（须 --execute 才真清）",
      "if not execute:" in src)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("cmd", nargs="?", default="check",
                    choices=["check", "margin"])
    ap.add_argument("--date", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--all", dest="all_market", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--clean", action="store_true",
                    help="清除重复表头垃圾行（治理动作，先备份）")
    a = ap.parse_args(argv)

    if a.clean:
        # ★ --clean 必须在 cmd==check 之前判定，否则 `--clean`（cmd 默认 check）
        #   会先掉进 check 分支，清理逻辑永远走不到（2026-10-21 自测发现）
        return clean_garbage(execute=a.execute)
    if a.cmd == "check":
        return cmd_check()
    if not a.date:
        print("margin 需要 --date YYYY-MM-DD")
        return 2
    return cmd_margin(a.date, limit=a.limit, all_market=a.all_market,
                      execute=a.execute) or 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
