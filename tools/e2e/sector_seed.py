# -*- coding: utf-8 -*-
"""sector_seed.py · 板块估值/行情采集器（S4/S6 数据面 · 2026-10-20）。

实证基础（2026-10-20 本机实测）：
  · `sector ranking`——124 个申万行业板块当日截面（涨幅/主力净流入/换手/upCount 涨跌家数/
    龙头）⇒ M2 板块扩散的权威依据（同板块涨家数）+ S4 拥挤度的资金面；
  · `sector valuation <code[,code...]>`——板块估值（**PeTTM + PeTTMPct=PE 历史百分位**/
    PbLF + PbLFPct=PB 分位 / DivTTM 股息率 / PcfTTM / PsTTM + PrevM/Q/W/Y 历史值）
    ⇒ S4 froth（估值泡沫）与 S6 行业估值锚的核心判据；**支持逗号批量 + --date 历史模式**；
  · `sector forecast <code...>`——板块盈利预测（一致预期的板块级近似，S4 第三源）。

本件 = 采集 + 落盘（投放区新键 sector_ranking / sector_valuation / sector_forecast）。
0.2 契约扩展：新键须同步 ingest.CONTRACT + landing 词汇表（本件自带 --selftest 覆盖契约形态）。

用法：
  python tools/e2e/sector_seed.py --snapshot --date 2026-10-19            # 当日截面（ranking+valuation 全板块）
  python tools/e2e/sector_seed.py --forecast --date 2026-10-19            # 盈利预测（当日）
  python tools/e2e/sector_seed.py --history --from D1 --to D2             # valuation 逐日序列（S6）
  python tools/e2e/sector_seed.py --selftest                             # 合成夹具自检（零生产）
"""
import argparse
import csv
import io
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("fs", "analysis", "decision", "risk", "learning", "e2e"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import writegate as _WG          # noqa: E402

SOURCE_LABEL = "westock_cli"
VAL_COLS = ["code", "name", "end_date", "pe_ttm", "pe_ttm_pct", "pb_lf", "pb_lf_pct",
            "div_ttm", "pcf_ttm", "ps_ttm"]
RANK_COLS = ["code", "name", "change_pct", "main_net_inflow", "main_net_inflow_5d",
             "turnover", "turnover_rate", "up_count", "leader"]
FC_COLS = ["code", "name", "pe_forecast", "net_profit_yoy", "revenue_yoy",
           "analyst_count", "rating_avg"]


def _run(args, timeout=90):
    import subprocess
    p = subprocess.run(["westock"] + args, capture_output=True, text=True,
                       timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError("westock %s 失败：%s" % (" ".join(args),
                                                  (p.stderr or "")[:200]))
    return p.stdout


def _parse_md(txt):
    """markdown 表 → list[dict]（跳过分隔行）。"""
    rows = []
    for ln in txt.splitlines():
        ln = ln.strip()
        if not ln.startswith("|") or set(ln) <= set("|- "):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if cells and cells[0] in ("code", ""):
            continue
        rows.append(cells)
    return rows


def _num(v):
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def fetch_ranking():
    """当日板块行情榜 → rows（RANK_COLS 形态）。"""
    txt = _run(["sector", "ranking"])
    out = []
    for c in _parse_md(txt):
        if len(c) < 9:
            continue
        out.append({"code": c[0], "name": c[1], "change_pct": _num(c[2]),
                    "main_net_inflow": _num(c[3]), "main_net_inflow_5d": _num(c[4]),
                    "turnover": _num(c[5]), "turnover_rate": _num(c[6]),
                    "up_count": c[7], "leader": c[8] if len(c) > 8 else None})
    return out


def fetch_valuation(codes, date=None):
    """板块估值（批量）→ rows（VAL_COLS 形态）。codes 超长时自动分批（80/批）。"""
    out = []
    for i in range(0, len(codes), 80):
        chunk = codes[i:i + 80]
        args = ["sector", "valuation", ",".join(chunk)]
        if date:
            args += ["--date", date]
        txt = _run(args)
        for c in _parse_md(txt):
            if len(c) < 10:
                continue
            out.append({"code": c[0], "name": c[1], "end_date": c[2],
                        "pe_ttm": _num(c[20]) if len(c) > 20 else None,
                        "pe_ttm_pct": _num(c[21]) if len(c) > 21 else None,
                        "pb_lf": _num(c[9]) if len(c) > 9 else None,
                        "pb_lf_pct": _num(c[10]) if len(c) > 10 else None,
                        "div_ttm": _num(c[3]) if len(c) > 3 else None,
                        "pcf_ttm": _num(c[15]) if len(c) > 15 else None,
                        "ps_ttm": _num(c[25]) if len(c) > 25 else None})
        if i + 80 < len(codes):
            time.sleep(2.5)
    return out


def _forecast_one(code, date=None):
    """单板块 forecast 解析（批量失败时的降级路径 + 无效代码隔离）。"""
    args = ["sector", "forecast", code]
    if date:
        args += ["--date", date]
    txt = _run(args, timeout=180)
    cur = None
    for ln in txt.splitlines():
        s = ln.strip()
        m = re.match(r"^\*\*(.+?)\*\*（申万[二三级]*，(pt\d+)", s)
        if m:
            cur = (m.group(2), m.group(1))
            continue
        if not s.startswith("|") or set(s) <= set("|- "):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if cells[0] in ("revenue", "") or cur is None:
            continue
        if len(cells) < 11:
            continue
        return {"code": cur[0], "name": cur[1],
                "pe_forecast": _num(cells[6]), "net_profit_yoy": _num(cells[4]),
                "revenue_yoy": _num(cells[3]),
                "analyst_count": None, "rating_avg": None,
                "_extra": {"cagr2y": _num(cells[5]), "roe": _num(cells[9]),
                           "peg": _num(cells[10]), "pb": _num(cells[7])}}
    return None


def fetch_forecast(codes, date=None):
    """板块未来盈利预测（申万）→ rows（FC_COLS 形态）。逗号批量（20/批）。

    输出为"未来数期预测"（表体多行=多期）⇒ 每板块取**首行=最近预测期**；
    PEG = PE/增速（S4 froth 经典判据）；netProfitYoy = 盈利增速（S4 第二源）。
    ★ 无效代码隔离（2026-10-20）：批量中一个 forecast 不认的 code（ranking 有、
    forecast 无）⇒ CLI 整批拒「未找到板块代码」⇒ 自动降级逐个调用，
    无效代码记 skipped（如实，不阻塞其余板块）。
    """
    out = []
    seen = set()
    skipped = []
    for i in range(0, len(codes), 20):
        chunk = codes[i:i + 20]
        args = ["sector", "forecast", ",".join(chunk)]
        if date:
            args += ["--date", date]
        txt = _run(args, timeout=180)
        # 批量中断特征（实测 2026-10-20）：①「未找到板块代码」（无效 code）
        # ②「当日无盈利预测数据」——CLI 遇批内首个无数据板块即停输出
        # ⇒ 两者都触发逐个降级（隔离无效/无数据代码，不阻塞其余板块）
        has_table = any(ln.strip().startswith("|") and "---" not in ln
                        for ln in txt.splitlines())
        if "未找到板块代码" in txt or "无盈利预测数据" in txt or not has_table:
            # 批量被拒 ⇒ 逐个降级（隔离无效代码）
            for c in chunk:
                r = _forecast_one(c, date=date)
                if r is None:
                    skipped.append(c)
                elif r["code"] not in seen:
                    seen.add(r["code"])
                    out.append(r)
            continue
        cur = None
        for ln in txt.splitlines():
            s = ln.strip()
            m = re.match(r"^\*\*(.+?)\*\*（申万[二三级]*，(pt\d+)", s)
            if m:
                cur = (m.group(2), m.group(1))
                continue
            if not s.startswith("|") or set(s) <= set("|- "):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if cells[0] in ("revenue", "") or cur is None or cur[0] in seen:
                continue
            if len(cells) < 11:
                continue
            seen.add(cur[0])
            out.append({"code": cur[0], "name": cur[1],
                        "pe_forecast": _num(cells[6]), "net_profit_yoy": _num(cells[4]),
                        "revenue_yoy": _num(cells[3]),
                        "analyst_count": None, "rating_avg": None,
                        "_extra": {"cagr2y": _num(cells[5]), "roe": _num(cells[9]),
                                   "peg": _num(cells[10]), "pb": _num(cells[7])}})
        if i + 20 < len(codes):
            time.sleep(2.5)
    for r in out:                     # _extra 只作诊断，不进 CSV（列契约固定）
        r.pop("_extra", None)
    if skipped:
        print("  ⚠ forecast 无效板块代码 %d 个（ranking 有/forecast 无）：%s"
              % (len(skipped), ",".join(skipped[:8]) + ("..." if len(skipped) > 8 else "")))
    return out


def _write_csv(path, cols, rows, date, source_note):
    if not os.path.isfile(os.path.join(os.path.dirname(path), "daily_quote.csv")) \
            and not os.path.isdir(os.path.dirname(path)):
        raise RuntimeError("投放日目录不存在：%s" % os.path.dirname(path))
    _WG.check_write(path)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
    os.replace(tmp, path)


def snapshot(date, base=None):
    root = base or _ROOT
    d = os.path.join(root, "data", "raw", date)
    if not os.path.isdir(d):
        raise RuntimeError("投放日目录不存在（先跑 live_seed 采当日）：%s" % d)
    rank = fetch_ranking()
    _write_csv(os.path.join(d, "sector_ranking.csv"), RANK_COLS, rank, date,
               SOURCE_LABEL)
    codes = [r["code"] for r in rank]
    val = fetch_valuation(codes)
    _write_csv(os.path.join(d, "sector_valuation.csv"), VAL_COLS, val, date,
               SOURCE_LABEL)
    return {"ranking": len(rank), "valuation": len(val)}


def history(d1, d2, base=None):
    """估值序列补采 → `data/raw/meta/sector_valuation_history.csv`（累积 append）。

    序列语义 = 一个文件多日期（区别于按日截面 sector_valuation.csv）；
    只采交易日历内的交易日（westock 无数据的日期跳过并记 0 行）；
    按"今日板块代码集"采历史（板块调整导致的历史缺失留空，不补造）。
    """
    root = base or _ROOT
    import dayclass as DC
    cal = DC.Calendar.from_seed_csv(
        os.path.join(root, "data", "raw", "meta", "trade_cal.csv"), source="sector_seed")
    days = list(cal.sessions_between(d1, d2))
    codes = [r["code"] for r in fetch_ranking()]
    out_p = os.path.join(root, "data", "raw", "meta",
                         "sector_valuation_history.csv")
    # 已采日期集合（幂等：不重复采同日）
    have = set()
    if os.path.isfile(out_p):
        with io.open(out_p, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("date"):
                    have.add(r["date"])
    new_rows = []
    for day in days:
        if day in have:
            continue
        try:
            val = fetch_valuation(codes, date=day)
        except Exception as e:                               # noqa: BLE001
            print("  %s 采空（%s）——跳过" % (day, str(e)[:80]))
            continue
        for v in val:
            v["date"] = day
            new_rows.append(v)
        time.sleep(1.0)
    if new_rows:
        exists = os.path.isfile(out_p)
        _WG.check_write(out_p)
        cols = ["date"] + VAL_COLS
        with io.open(out_p, "a", encoding="utf-8", newline="\n") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            if not exists:
                w.writeheader()
            for r in new_rows:
                w.writerow({k: ("" if r.get(k) is None else r.get(k))
                            for k in cols})
    return {"days_target": len(days), "days_have": len(have),
            "rows_new": len(new_rows), "path": out_p}


def _selftest():
    import tempfile
    ok = fail = 0

    def T(name, cond, extra=""):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s %s" % ("✓" if cond else "✗", name, extra if not cond else ""))

    print("== sector_seed --selftest ==")
    T("A1 RANK_COLS 九列齐", len(RANK_COLS) == 9)
    T("A2 VAL_COLS 十列齐（含分位两列）", len(VAL_COLS) == 10
      and "pe_ttm_pct" in VAL_COLS and "pb_lf_pct" in VAL_COLS)
    md = ("| code | name | changePct | mainNetInflow | mainNetInflow5d | turnover "
          "| turnoverRate | upCount | leader |\n| --- | --- | --- | --- | --- | --- "
          "| --- | --- | --- |\n| pt01801085 | 消费电子 | 2.17 | 238748.76 | -73753.58 "
          "| 4069027 | 1.68 | 78/91 | 长盈精密(19.99) |\n")
    rows = _parse_md(md)
    T("A3 markdown 表解析（跳过分隔行）", len(rows) == 1 and rows[0][0] == "pt01801085")
    T("A4 _num 空值与逗号", _num("") is None and _num("-") is None
      and _num("1,234.5") == 1234.5)
    sb = tempfile.mkdtemp(prefix="sector_seed_")
    try:
        os.makedirs(os.path.join(sb, "data", "raw", "2026-10-19"))
        with io.open(os.path.join(sb, "data", "raw", "2026-10-19",
                                  "daily_quote.csv"), "w", encoding="utf-8") as f:
            f.write("code\n")
        rank = [{"code": "pt1", "name": "测试板块", "change_pct": 1.5,
                 "main_net_inflow": 100.0, "main_net_inflow_5d": 200.0,
                 "turnover": 300.0, "turnover_rate": 1.1, "up_count": "78/91",
                 "leader": "某股(9.99)"}]
        _write_csv(os.path.join(sb, "data", "raw", "2026-10-19",
                                "sector_ranking.csv"), RANK_COLS, rank,
                   "2026-10-19", SOURCE_LABEL)
        back = list(csv.DictReader(io.open(
            os.path.join(sb, "data", "raw", "2026-10-19", "sector_ranking.csv"),
            encoding="utf-8")))
        T("B1 写读往返（沙盒路径不受闸）", len(back) == 1
          and back[0]["up_count"] == "78/91")
        try:
            _write_csv(os.path.join(sb, "data", "raw", "2099-01-01", "x.csv"),
                       RANK_COLS, rank, "2099-01-01", SOURCE_LABEL)
            T("B2 缺日目录拒绝（不静默建）", False)
        except RuntimeError:
            T("B2 缺日目录拒绝（不静默建）", True)
    finally:
        import shutil
        shutil.rmtree(sb, ignore_errors=True)
    print("== sector_seed --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def _main(argv):
    ap = argparse.ArgumentParser(description="板块估值/行情采集器（S4/S6 数据面）")
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--forecast", action="store_true")
    ap.add_argument("--history", action="store_true")
    ap.add_argument("--date")
    ap.add_argument("--from", dest="d1")
    ap.add_argument("--to", dest="d2")
    ap.add_argument("--base", default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if a.snapshot:
        if not a.date:
            print("⛔ --snapshot 需 --date", file=sys.stderr)
            return 2
        r = snapshot(a.date, base=a.base)
        print("✅ %s 板块截面：ranking %d · valuation %d"
              % (a.date, r["ranking"], r["valuation"]))
        return 0
    if a.forecast:
        if not a.date:
            print("⛔ --forecast 需 --date", file=sys.stderr)
            return 2
        d = os.path.join(a.base or _ROOT, "data", "raw", a.date)
        if not os.path.isdir(d):
            raise RuntimeError("投放日目录不存在（先跑 live_seed 采当日）：%s" % d)
        codes = [r["code"] for r in fetch_ranking()]
        fc = fetch_forecast(codes, date=a.date)
        _write_csv(os.path.join(d, "sector_forecast.csv"), FC_COLS, fc, a.date,
                   SOURCE_LABEL)
        print("✅ %s 板块盈利预测：%d 个" % (a.date, len(fc)))
        return 0
    if a.history:
        if not (a.d1 and a.d2):
            print("⛔ --history 需 --from/--to", file=sys.stderr)
            return 2
        r = history(a.d1, a.d2, base=a.base)
        print("✅ 序列补采：目标 %d 日 · 已有 %d 日 · 新增 %d 行 → %s"
              % (r["days_target"], r["days_have"], r["rows_new"], r["path"]))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
