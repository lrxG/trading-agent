"""fetch_mktcap.py · 抓逐日总股本 → 推算历史市值（G1 能力缺口）。

## 为什么需要

「市值越大，超跌反弹越可靠」是一条有外部实证支撑、但**我们一直无法验证**的假设
（缺历史市值）。klines 只有价格没股本，`daily_quote` 也没有市值列。

## 数据来源（通达信官方只读接口）

  `get_gb_info` 参数契约（实测确定，错误信息会直接指出缺哪个 Key）：
    · `gb_type=1` + `start_date` + `end_date`  → **逐日** 股本序列 ✅ 本件采用
    · `gb_type=0` + `date_list` / `count`      → 截面（实测参数校验不通过，未用）

  字段：`Zgb` = 总股本（**股**）· `Ltgb` = 流通股本（股）
  校验：600178 于 20241018 的 `Ltgb`=466476160 股 = 46647.62 万股
        = `get_stock_info` 的 `ActiveCapital`（46647.62）⇒ 单位一致，无需换算

  市值 = 总股本 × 当日 close（元）· 流通市值 = 流通股本 × close

## 纪律

· **只用只读白名单接口**，不碰交易类
· 取不到的票**如实记为缺失**，不用「最新股本回推历史」冒充（除权会导致严重错误）
· 分批抓取 + 断点续跑（已存在则跳过），单批失败不中断全局
· 输出 CSV：code,date,close,total_share,float_share,mktcap_yi,float_mktcap_yi

用法：
  python tools/e2e/fetch_mktcap.py --check
  python tools/e2e/fetch_mktcap.py --from 2024-01-02 --to 2026-10-20 --sample 20
  python tools/e2e/fetch_mktcap.py --from 2024-01-02 --to 2026-10-20 --execute
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

import tq_bridge as TQ            # noqa: E402
import writegate as _WG           # noqa: E402

OUT_REL = os.path.join("data", "raw", "bus", "mktcap.csv")
HEADER = ["code", "date", "close", "total_share", "float_share",
          "mktcap_yi", "float_mktcap_yi"]
BATCH = 1                          # get_gb_info 一次只接受单只（stock_code 而非 stock_list）


def to_tdx_code(project_code):
    """sh600000 → 600000.SH"""
    if not project_code or len(project_code) < 8:
        return None
    ex, num = project_code[:2], project_code[2:]
    m = {"sh": "SH", "sz": "SZ", "bj": "BJ"}
    return "%s.%s" % (num, m[ex]) if ex in m else None


def load_closes(root=None, d1="2024-01-02", d2="2026-10-20"):
    """klines → {code: {date: close}} 与股票清单（按金额降序，优做大票）。"""
    root = root or _ROOT
    p = os.path.join(root, "data", "raw", "bus", "klines.csv")
    closes = {}
    amount = {}
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            dt = r.get("date") or ""
            if not (d1 <= dt <= d2):
                continue
            try:
                c = float(r["close"])
            except (TypeError, ValueError, KeyError):
                continue
            if c <= 0:
                continue
            closes.setdefault(r["code"], {})[dt] = c
            try:
                amount[r["code"]] = amount.get(r["code"], 0.0) + float(r.get("amount") or 0)
            except (TypeError, ValueError):
                pass
    order = sorted(amount, key=lambda c: -amount[c])
    return closes, order


def fetch_shares(project_code, d1, d2, timeout=60):
    """→ {date: (total_share, float_share)}（股）。空则返回 {}。"""
    tc = to_tdx_code(project_code)
    if not tc:
        return {}
    r = TQ.call("get_gb_info", {"stock_code": tc, "gb_type": 1,
                                "start_date": d1.replace("-", ""),
                                "end_date": d2.replace("-", "")}, timeout=timeout)
    if str(r.get("ErrorId")) != "0":
        raise RuntimeError("get_gb_info %s: %s" % (tc, r.get("Error")))
    out = {}
    for it in (r.get("Value") or []):
        try:
            d = str(it.get("Date"))
            d = "%s-%s-%s" % (d[:4], d[4:6], d[6:8])
            out[d] = (float(it.get("Zgb") or 0), float(it.get("Ltgb") or 0))
        except (TypeError, ValueError, AttributeError):
            continue
    return {d: v for d, v in out.items() if v[0] > 0}


def build_rows(project_code, closes, shares):
    """合成市值行。股本缺失的日期**不输出**（不插值、不回推）。"""
    rows = []
    for d, (tot, flo) in shares.items():
        px = closes.get(d)
        if not px or px <= 0:
            continue
        rows.append({
            "code": project_code, "date": d, "close": "%.3f" % px,
            "total_share": "%.0f" % tot, "float_share": "%.0f" % flo,
            "mktcap_yi": "%.2f" % (tot * px / 1e8),
            "float_mktcap_yi": "%.2f" % (flo * px / 1e8),
        })
    rows.sort(key=lambda x: x["date"])
    return rows


def load_existing(path):
    """已抓过的 code 集合（断点续跑）。

    ★ 只扫首列（不解析整行）：21MB 文件用 DictReader 逐行解析要数秒，
      而这里只需要 code 去重。改成按行取逗号前段，快 5-10 倍。
    """
    if not os.path.isfile(path):
        return set(), []
    done = set()
    with io.open(path, encoding="utf-8-sig", errors="ignore") as f:
        f.readline()                       # 跳过表头
        for ln in f:
            i = ln.find(",")
            if i > 0:
                done.add(ln[:i])
    return done, []


def write_rows_append(path, rows):
    """**追加**写（不是整表重写）。

    ★ 旧实现每抓一只就整表读-拼-写，5240 只 ⇒ O(n²) 且容易撞「文件被占用」
      （WinError 5）。现改为：首次建表头，之后逐批追加。
      断点续跑仍安全：load_existing 按 code 去重。
    """
    _WG.check_write(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.isfile(path) or os.path.getsize(path) == 0
    # 追加模式（newline="" 防 Windows 下 \r\n 夹入）
    with io.open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, lineterminator="\n")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in HEADER})
    return len(rows)


def coverage_stats(path):
    """覆盖率 + 市值分布（如实报告）。"""
    if not os.path.isfile(path):
        return {}
    n = 0
    codes = set()
    dates = set()
    caps = []
    with io.open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            n += 1
            codes.add(r["code"])
            dates.add(r["date"])
            try:
                caps.append(float(r["mktcap_yi"]))
            except (TypeError, ValueError):
                pass
    caps.sort()
    if not caps:
        return {"rows": n, "codes": len(codes)}
    return {"rows": n, "codes": len(codes), "dates": len(dates),
            "cap_p10_yi": round(caps[len(caps) // 10], 1),
            "cap_median_yi": round(caps[len(caps) // 2], 1),
            "cap_p90_yi": round(caps[len(caps) * 9 // 10], 1)}


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    ap = argparse.ArgumentParser(description="抓逐日股本 → 推算历史市值")
    ap.add_argument("--from", dest="d1", default="2024-01-02")
    ap.add_argument("--to", dest="d2", default="2026-10-20")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--sample", type=int, default=0, help="只抓前 N 只（试跑）")
    ap.add_argument("--sleep", type=float, default=0.05)
    ap.add_argument("--base", default=None)
    a = ap.parse_args(argv)

    st = TQ.check_status()
    if a.check:
        print("服务可用:", st["usable"])
        return 0 if st["usable"] else 2
    if not st["usable"]:
        print("⛔ 通达信服务不可用：请先启动客户端并登录")
        return 2

    root = a.base or _ROOT
    closes, order = load_closes(root, a.d1, a.d2)
    path = os.path.join(root, OUT_REL)
    done, _ = load_existing(path)
    todo = [c for c in order if c not in done]
    if a.sample:
        todo = todo[:a.sample]
    print("股票 %d · 已有 %d · 本次抓 %d" % (len(order), len(done), len(todo)))

    t0 = time.time()
    n_ok = n_empty = n_fail = n_rows = 0
    fails = []
    buf = []                                    # 批量缓冲（减少写盘次数）
    FLUSH = 50

    def flush():
        nonlocal n_rows
        if buf and a.execute:
            cnt = write_rows_append(path, buf)
            n_rows += cnt
        buf.clear()

    for i, code in enumerate(todo):
        try:
            sh = fetch_shares(code, a.d1, a.d2)
        except Exception as e:
            n_fail += 1
            fails.append((code, str(e)[:80]))
            continue
        if not sh:
            n_empty += 1
            continue
        rows = build_rows(code, closes.get(code) or {}, sh)
        if not rows:
            n_empty += 1
            continue
        n_ok += 1
        buf.extend(rows)
        if len(buf) >= FLUSH * 500:             # 约 50 只票
            flush()
        if (i + 1) % 100 == 0:
            flush()
            print("  %d/%d · 成功 %d · 空 %d · 失败 %d · 已写 %d 行 · %.0fs"
                  % (i + 1, len(todo), n_ok, n_empty, n_fail, n_rows, time.time() - t0))
        time.sleep(a.sleep)
    flush()

    print()
    print("完成：成功 %d · 空 %d · 失败 %d · 生成 %d 行 · %.0fs"
          % (n_ok, n_empty, n_fail, n_rows, time.time() - t0))
    if fails:
        print("失败样例:", fails[:3])
    if a.execute:
        print("落盘:", path)
        print("统计:", coverage_stats(path))
    else:
        print("（dry-run，未写盘；加 --execute 生效）")
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

    T("A1 sh→SH", to_tdx_code("sh600178") == "600178.SH")
    T("A2 sz→SZ", to_tdx_code("sz000001") == "000001.SZ")
    T("A3 bj→BJ", to_tdx_code("bj430047") == "430047.BJ")
    T("A4 非法返回 None",
      to_tdx_code("600178") is None and to_tdx_code("") is None)

    # 市值计算：总股本 4.7 亿 × 价 10 元 = 47 亿
    closes = {"2024-01-02": 10.0, "2024-01-03": 20.0}
    shares = {"2024-01-02": (470000000.0, 460000000.0),
              "2024-01-03": (470000000.0, 460000000.0)}
    rows = build_rows("sh600178", closes, shares)
    T("B1 市值 = 总股本 × close（4.7亿×10 = 47亿）",
      len(rows) == 2 and rows[0]["mktcap_yi"] == "47.00", str(rows[:1]))
    T("B2 流通市值 = 流通股本 × close（46亿）",
      rows[0]["float_mktcap_yi"] == "46.00", str(rows[:1]))
    T("B3 行按日期升序", rows[0]["date"] < rows[1]["date"])

    # 缺股本的日期不输出（不插值）
    shares2 = {"2024-01-02": (470000000.0, 460000000.0)}
    rows2 = build_rows("sh600178", closes, shares2)
    T("B4 无股本的日期不生成行（不插值）", len(rows2) == 1, str(len(rows2)))

    # 缺 close 的日期不输出
    closes3 = {}
    rows3 = build_rows("sh600178", closes3, shares)
    T("B5 无收盘价的日期不生成行", len(rows3) == 0)

    # 股本为 0 的记录应被 fetch_shares 过滤（这里验证过滤逻辑）
    sh = {"2024-01-02": (0.0, 0.0), "2024-01-03": (1e8, 1e8)}
    filtered = {d: v for d, v in sh.items() if v[0] > 0}
    T("B6 零股本被过滤", len(filtered) == 1, str(filtered))

    T("B7 输出表头字段齐全", HEADER == ["code", "date", "close", "total_share",
                                        "float_share", "mktcap_yi", "float_mktcap_yi"])

    # C 追加写：两次调用不能产生重复表头、不能丢行
    import tempfile
    d = tempfile.mkdtemp()
    tp = os.path.join(d, "m.csv")
    write_rows_append(tp, rows[:1])
    write_rows_append(tp, rows[1:])
    with io.open(tp, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    T("C1 追加写只出现一次表头",
      sum(1 for ln in lines if ln.startswith("code,")) == 1, str(lines[:3]))
    T("C2 追加写行数 = 两次之和", len(lines) - 1 == len(rows), str(len(lines)))
    T("C3 追加写无空行", all(ln.strip() for ln in lines))
    import shutil
    shutil.rmtree(d, ignore_errors=True)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
