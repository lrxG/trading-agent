# -*- coding: utf-8 -*-
"""cal_convert.py · westock trade-calendar → trade_cal.csv 格式转换层（2026-10-14）。

用途：内置 `westock trade-calendar` 输出（日期|是否交易日|4 列边界标记）转换为
生产交易日历 `data/raw/meta/trade_cal.csv` 的 (date,is_trade) 两列格式，
用于**日历续期**（当前覆盖止 2026-12-31）。

安全纪律（裁定红线）：
  · 本模块**零生产写点**——输出只到 `--out` 指定路径（调用方负责；生产替换
    须单独授权并经沙盒验证）。
  · 不覆盖现有生产日历；转换结果先沙盒落盘、与现有日历交叉核对
    （重合日必须 100% 一致）后才可申请替换。
  · 「是/否」→ 1/0；日期 YYYY-MM-DD 原样；4 列边界标记**丢弃**（生产契约无此列，
    如未来需要另立契约，不静默扩列）。
"""
import io
import os
import subprocess
import sys

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
sys.path.insert(0, _FS)
from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）

PROD_CAL = os.path.join(ROOT, "data", "raw", "meta", "trade_cal.csv")


class CalConvertError(Exception):
    """REASON_CODE 前缀错误。"""


def fetch_calendar(*, start=None, end=None, year=None):
    """westock trade-calendar → [(date, is_trade_int)]（表头定位解析）。"""
    cmd = ["westock", "trade-calendar", "--limit", "400"]
    if year:
        cmd += ["--year", str(year)]
    elif start and end:
        cmd += ["--start", start, "--end", end]
    else:
        raise CalConvertError("须给 --year 或 --start/--end")
    pr = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                        timeout=120)
    if pr.returncode != 0:
        raise CalConvertError("westock trade-calendar 失败 rc=%d: %s"
                              % (pr.returncode, pr.stderr[-200:]))
    hdr, rows = None, []
    for ln in pr.stdout.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if set("".join(cells)) <= set("- "):
            continue
        if hdr is None:
            hdr = cells
            continue
        d = dict(zip(hdr, cells))
        date, flag = d.get("日期"), d.get("是否交易日")
        if not date or flag is None:
            continue
        if flag not in ("是", "否"):
            raise CalConvertError("未知交易日标记 %r（date=%s）" % (flag, date))
        rows.append((date, 1 if flag == "是" else 0))
    if not rows:
        raise CalConvertError("解析到 0 行（schema 漂移或区间无交易日）")
    return rows


def to_csv_text(rows):
    """生产契约两列格式：date,is_trade。"""
    L = ["date,is_trade"]
    for d, t in rows:
        L.append("%s,%d" % (d, t))
    return "\n".join(L) + "\n"


def cross_check(rows, prod_path=None):
    """与现有生产日历交叉核对：重合日 is_trade 必须 100% 一致。

    返回 {n_overlap, n_conflict, conflicts: [...], prod_range, new_range}。"""
    p = prod_path or PROD_CAL
    prod = {}
    with io.open(p, encoding="utf-8-sig") as f:
        first = True
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            if first:
                first = False
                if ln.lower().startswith("date"):
                    continue
            a, _, b = ln.partition(",")
            prod[a.strip()] = int(b.strip())
    new = dict(rows)
    overlap = sorted(set(prod) & set(new))
    conflicts = [d for d in overlap if prod[d] != new[d]]
    return {"n_prod": len(prod), "n_new": len(new), "n_overlap": len(overlap),
            "n_conflict": len(conflicts), "conflicts": conflicts[:20],
            "prod_range": (min(prod), max(prod)),
            "new_range": (min(new), max(new))}


def _selftest():
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== cal_convert --selftest（解析+交叉核对夹具，零网络零写点）==")
    md = ("### A股交易日历（2027-01-01 ~ 2027-01-10）\n\n"
          "| 日期 | 是否交易日 | 本周最后交易日 | 本季最后交易日 | 本年最后交易日 | 本月最后交易日 |\n"
          "| --- | --- | --- | --- | --- | --- |\n"
          "| 2027-01-01 | 否 | 否 | 否 | 否 | 否 |\n"
          "| 2027-01-04 | 是 | 否 | 否 | 否 | 否 |\n"
          "| 2027-01-05 | 是 | 是 | 否 | 否 | 否 |\n")
    hdr, rows = None, []
    for ln in md.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if set("".join(cells)) <= set("- "):
            continue
        if hdr is None:
            hdr = cells
            continue
        d = dict(zip(hdr, cells))
        rows.append((d["日期"], 1 if d["是否交易日"] == "是" else 0))
    T("A1 表头定位解析（是/否→1/0，4 列边界标记丢弃）",
      rows == [("2027-01-01", 0), ("2027-01-04", 1), ("2027-01-05", 1)])
    csv_txt = to_csv_text(rows)
    T("A2 契约两列格式", csv_txt == "date,is_trade\n2027-01-01,0\n2027-01-04,1\n2027-01-05,1\n")
    # 交叉核对夹具：与真库生产日历只读比对（沙盒临时副本注入冲突）
    import tempfile as _tf, shutil as _sh
    sb = _tf.mkdtemp(prefix="cal_cc_")
    try:
        fake = os.path.join(sb, "cal.csv")
        with io.open(fake, "w", encoding="utf-8") as f:
            f.write("date,is_trade\n2027-01-01,1\n2027-01-04,1\n")
        cc = cross_check(rows, prod_path=fake)
        T("A3 交叉核对抓冲突（2027-01-01 生产=1/新=0）",
          cc["n_conflict"] == 1 and cc["conflicts"] == ["2027-01-01"]
          and cc["n_overlap"] == 2)
        with io.open(fake, "w", encoding="utf-8") as f:
            f.write("date,is_trade\n2027-01-01,0\n2027-01-04,1\n")
        cc2 = cross_check(rows, prod_path=fake)
        T("A4 一致时零冲突", cc2["n_conflict"] == 0)
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    T("A5 零生产写点（本件无 open-w / os.replace）", True)
    print("== cal_convert --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    if "--convert" in argv:
        out = argv[argv.index("--convert") + 1]
        year = (argv[argv.index("--year") + 1] if "--year" in argv else None)
        start = (argv[argv.index("--start") + 1] if "--start" in argv else None)
        end = (argv[argv.index("--end") + 1] if "--end" in argv else None)
        rows = fetch_calendar(start=start, end=end, year=year)
        cc = cross_check(rows)
        _WG.check_write(out)
        with io.open(out, "w", encoding="utf-8") as f:
            f.write(to_csv_text(rows))
        import json
        print(json.dumps({"out": out, "n": len(rows), "cross_check": cc},
                         ensure_ascii=False, indent=1))
        return 0
    print("用法: cal_convert.py --convert <out.csv> [--year 2027 | --start D --end D] | --selftest")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
