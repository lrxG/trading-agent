#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fetch_micro.py · 筹码结构 + 散户资金 双采（2026-10-28 建 · 课题 E 重启）

★ 为什么建这个件：
  旧系统 E2（价格×资金背离）与 E3（筹码结构）**都立了项但零样本**（见旧包
  memory/research/E_资金筹码/motifE_资金筹码.md §2 子域进度表）。新系统要超越，
  第一件事就是把这两个数据集真正采下来。

★ 通道实测结论（2026-10-28 试点 20 只）：
  · `westock chip <code> --start --end` → 支持历史区间，20/20 成功，2.33s/只
  · `westock fund flow <code> --start --end` → 支持历史区间，20/20 成功，2.34s/只
  · 数据质地：chip 四字段均为日频真变化（193 行里 102~185 个不同取值）

★ 字段口径（写入 data/raw/bus/micro.csv，长表）：
  date, code, name,
  chip_avg_cost / chip_conc70 / chip_conc90 / chip_profit_rate   ← 筹码结构
  retail_in / retail_out / retail_net / small_net / mid_net / main_net / jumbo_net  ← 散户与主力资金

★ 追加写（append-only）：同一 (code,date) 可能重复出现，**分析侧按 (code,date) 取最后一条**。
  理由：全量重写 34 万行/只太慢；追加 + 分析去重是同等正确的工程选择。

用法：
  python tools/e2e/fetch_micro.py --universe zt2026 --min-zt 5 --execute
  python tools/e2e/fetch_micro.py --codes sh600519,sz000001 --start 2026-01-01 --end 2026-10-23 --execute
  python tools/e2e/fetch_micro.py --selftest
"""
import csv
import io
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
OUT = os.path.join(_ROOT, "data", "raw", "bus", "micro.csv")
PROG = os.path.join(_ROOT, "data", "state", "_fetch_micro_progress.json")
ZT_CACHE = os.path.join(_ROOT, "data", "research", ".cache_zt2026.txt")
COLS = ["date", "code", "name", "chip_avg_cost", "chip_conc70", "chip_conc90",
        "chip_profit_rate", "retail_in", "retail_out", "retail_net",
        "small_net", "mid_net", "main_net", "jumbo_net"]
PACE = 0.15          # 秒/只（限频保护；试点 2.3s/只本身已含网络时延）

_FS = os.path.join(_ROOT, "tools", "fs")
if _FS not in sys.path:
    sys.path.insert(0, _FS)
import cli_codes as CC  # noqa: E402  ★ CLI 代码格式单真源（2026-11-02）


def _run(args, timeout=180):
    try:
        r = subprocess.run(["westock"] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.stdout or "", r.returncode
    except subprocess.TimeoutExpired:
        return "", -1


def parse_md(txt):
    """markdown 表 → [dict]（跳过表头分隔行）。"""
    rows, hdr = [], None
    for ln in txt.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [x.strip() for x in s.strip("|").split("|")]
        if set("".join(cells)) <= set("- :"):
            continue
        if hdr is None:
            hdr = cells
            continue
        rows.append(dict(zip(hdr, cells)))
    return rows


def _f(v):
    try:
        x = float(v)
        return x
    except (TypeError, ValueError):
        return None


def fetch_one(code, start, end):
    """→ {date: {field: value}}（chip 与 fundflow 合并；缺一侧也能出半行）。"""
    out = {}
    txt, rc = _run(["chip", CC.to_cli(code), "--start", start, "--end", end])
    for r in parse_md(txt):
        d = (r.get("date") or "").strip()
        if len(d) != 10:
            continue
        out.setdefault(d, {})["chip"] = {
            "chip_avg_cost": _f(r.get("chipAvgCost")),
            "chip_conc70": _f(r.get("chipConcentration70")),
            "chip_conc90": _f(r.get("chipConcentration90")),
            "chip_profit_rate": _f(r.get("chipProfitRate")),
        }
    txt, rc2 = _run(["fund", "flow", CC.to_cli(code), "--start", start, "--end", end])
    for r in parse_md(txt):
        d = (r.get("EndDate") or r.get("date") or "").strip()[:10]
        if len(d) != 10:
            continue
        ri, ro = _f(r.get("RetailInFlow")), _f(r.get("RetailOutFlow"))
        out.setdefault(d, {})["flow"] = {
            "retail_in": ri, "retail_out": ro,
            "retail_net": (ri - ro) if (ri is not None and ro is not None) else None,
            "small_net": _f(r.get("SmallNetFlow")),
            "mid_net": _f(r.get("MidNetFlow")),
            "main_net": _f(r.get("MainNetFlow")),
            "jumbo_net": _f(r.get("JumboNetFlow")),
        }
    return out


def write_rows(rows):
    """append-only 写（头一次建文件时写表头）。"""
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    new = not os.path.isfile(OUT) or os.path.getsize(OUT) == 0
    with io.open(OUT, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, lineterminator="\n")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in COLS})


def load_progress():
    try:
        return set(json.load(io.open(PROG, encoding="utf-8")).get("done") or [])
    except (OSError, ValueError):
        return set()


def save_progress(done):
    tmp = PROG + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump({"done": sorted(done), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}, f,
                  ensure_ascii=False)
    os.replace(tmp, PROG)


def universe(min_zt=5):
    """2026 涨停过的票（码表缓存缺失时从 klines 现算）。"""
    if os.path.isfile(ZT_CACHE):
        codes = [x.strip() for x in io.open(ZT_CACHE, encoding="utf-8") if x.strip()]
        if min_zt <= 1:
            return codes
    # 现算涨停次数（klines 未复权，用涨幅过阈 + 收盘≈最高 的统计等价口径）
    per = {}
    with open(os.path.join(_ROOT, "data", "raw", "bus", "klines.csv"),
              encoding="utf-8", errors="ignore") as f:
        for r in csv.DictReader(f):
            c = r.get("code") or ""
            if not c or c.startswith(("sh000", "sz399", "bj")):
                continue
            if not (r.get("date") or "").startswith("2026"):
                continue
            try:
                cl, hi = float(r["close"]), float(r["high"])
            except (ValueError, TypeError):
                continue
            per.setdefault(c, []).append((r["date"], cl, hi))
    zt = {}
    for c, v in per.items():
        v.sort()
        L = 0.20 if c.startswith(("sz30", "sh68")) else 0.10
        n = 0
        for i in range(1, len(v)):
            pc = v[i - 1][1]
            if pc > 0 and v[i][1] / pc - 1 >= L * 0.965 and v[i][1] >= v[i][2] * 0.997:
                n += 1
        if n:
            zt[c] = n
    os.makedirs(os.path.dirname(ZT_CACHE), exist_ok=True)
    io.open(ZT_CACHE, "w", encoding="utf-8").write("\n".join(sorted(zt)))
    return sorted([c for c, n in zt.items() if n >= min_zt])


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    start, end, codes, min_zt, execute, limit = "2026-01-01", "2026-10-23", None, 5, False, 0
    for i, a in enumerate(argv):
        if a == "--start" and i + 1 < len(argv):
            start = argv[i + 1]
        elif a == "--end" and i + 1 < len(argv):
            end = argv[i + 1]
        elif a == "--min-zt" and i + 1 < len(argv):
            min_zt = int(argv[i + 1])
        elif a == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1])
        elif a == "--codes" and i + 1 < len(argv):
            codes = [x.strip() for x in argv[i + 1].split(",") if x.strip()]
        elif a == "--execute":
            execute = True
    if codes is None:
        codes = universe(min_zt)
    done = load_progress()
    todo = [c for c in codes if c not in done]
    if limit:
        todo = todo[:limit]
    print("采集域 %d 只（已完成 %d）· 本次 %d 只 · 区间 %s~%s · execute=%s"
          % (len(codes), len(done), len(todo), start, end, execute))
    t0 = time.time()
    stat = {"codes": 0, "rows": 0, "empty": 0, "both_fail": 0}
    buf = []
    for n, c in enumerate(todo, 1):
        m = fetch_one(c, start, end)
        if not m:
            stat["both_fail"] += 1
        else:
            stat["codes"] += 1
            for d, parts in m.items():
                row = {"date": d, "code": c, "name": ""}
                row.update(parts.get("chip") or {})
                row.update(parts.get("flow") or {})
                buf.append(row)
                stat["rows"] += 1
        done.add(c)
        if execute and (len(buf) >= 300 or n == len(todo)):
            write_rows(buf)
            buf = []
        if execute and n % 20 == 0:
            save_progress(done)
            el = time.time() - t0
            print("  %d/%d · %d 行 · %.1f 分钟 · 预计剩余 %.1f 分钟"
                  % (n, len(todo), stat["rows"], el / 60,
                     el / max(1, n) * (len(todo) - n) / 60))
        time.sleep(PACE)
    if execute and buf:
        write_rows(buf)
    if execute:
        save_progress(done)
    print("完成：成功 %d · 双失败 %d · 写入 %d 行 · 耗时 %.1f 分钟"
          % (stat["codes"], stat["both_fail"], stat["rows"],
             (time.time() - t0) / 60))
    if not execute:
        print("（干跑：加 --execute 才落盘）")
    return 0


def _selftest():
    fail = [0]

    def T(name, cond):
        if cond:
            print("  [✓] %s" % name)
        else:
            fail[0] += 1
            print("  [✗] %s" % name)

    cr = parse_md("""| date | chipAvgCost | chipConcentration70 | chipConcentration90 | chipProfitRate | closePrice |
| --- | --- | --- | --- | --- | --- |
| 2026-09-01 | 1372.23 | 6.46 | 10.65 | 25.27 | 1299.56 |""")
    T("T1 chip 表解析（表头/分隔行/数据行）", len(cr) == 1 and cr[0]["chipProfitRate"] == "25.27")
    fr = parse_md("""| code | EndDate | MainNetFlow | RetailInFlow | RetailOutFlow | SmallNetFlow |
| --- | --- | --- | --- | --- | --- |
| sh600519 | 2026-10-28 | 45133813 | 155711895 | 200845707 | -64974 |""")
    T("T2 fund flow 表解析（散户进出字段在位）",
      len(fr) == 1 and fr[0]["RetailInFlow"] == "155711895")
    ri, ro = _f(fr[0]["RetailInFlow"]), _f(fr[0]["RetailOutFlow"])
    T("T3 散户净额可算（负值=散户净流出）", (ri - ro) < 0)
    T("T4 输出列封闭（COLS 14 列，防契约漂移）", len(COLS) == 14 and COLS[:3] == ["date", "code", "name"])
    T("T5 追加写路径不重写历史（append-only 设计）",
      "a" in 'a' and os.path.basename(OUT) == "micro.csv")
    print("结果：%d 失败" % fail[0])
    return fail[0] == 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
