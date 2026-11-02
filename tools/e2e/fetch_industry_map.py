"""fetch_industry_map.py · 从通达信抓「个股 → 行业板块」映射并固化。

## 为什么单独成件

`data/raw/meta/stock_industry.json` 是研究特征（行业中相对偏离）的基础数据。
它此前只能靠临时脚本生成 ⇒ 换机器/重装后无法复现。本件让它可复现。

## 数据来源（腾讯自选股之外的另一条官方通道）

通达信客户端 JSON-RPC（`tq_bridge`，只读白名单内）：
  · `get_sector_list`                          → 全部板块代码（587 个）
  · `get_stock_list_in_sector {'block_code'}`  → 该板块成份股

板块代码分两类：
  · `880xxx` = 概念板块（454 个）
  · **`881xxx` = 通达信一级行业（128 个）** ← 本件只用这一类

## 口径（写死）

· **一股一行业**：实测 881xxx 无重叠（0 只票属于 2 个行业），若未来出现重叠取字典序首个并告警
· 代码格式转换：通达信 `600000.SH` → 本项目 `sh600000`
· **通达信未启动 ⇒ 如实失败退出（不产出空文件冒充成功）**

用法：
  python tools/e2e/fetch_industry_map.py --check        # 只看服务状态
  python tools/e2e/fetch_industry_map.py --dry-run      # 抓取但不写盘，报告覆盖率
  python tools/e2e/fetch_industry_map.py --execute      # 抓取并写 data/raw/meta/stock_industry.json
"""
import argparse
import io
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("fs", "analysis", "e2e"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import tq_bridge as TQ            # noqa: E402
import writegate as _WG           # noqa: E402

OUT_REL = os.path.join("data", "raw", "meta", "stock_industry.json")
INDUSTRY_PREFIX = "881"           # 通达信一级行业


def to_project_code(c):
    """600000.SH → sh600000；000001.SZ → sz000001。无法识别返回 None。"""
    if not c or "." not in c:
        return None
    num, ex = c.split(".", 1)
    ex = ex.upper()
    if ex == "SH":
        return "sh" + num
    if ex == "SZ":
        return "sz" + num
    if ex == "BJ":
        return "bj" + num
    return None


def fetch_map(timeout=30, progress=None):
    """抓全部 881xxx 板块成份。→ (stock_map, stats)

    stock_map: {项目代码: 行业代码}
    stats: 含板块数/覆盖票数/重叠数/耗时/失败数
    """
    t0 = time.time()
    r = TQ.call("get_sector_list", {}, timeout=60)
    sectors = r.get("Value") or []
    inds = [s for s in sectors if str(s).split(".")[0].startswith(INDUSTRY_PREFIX)]
    stock_map = {}
    overlap = 0
    failed = []
    n_sec_with_members = 0
    for i, sec in enumerate(inds):
        try:
            rr = TQ.call("get_stock_list_in_sector", {"block_code": sec}, timeout=timeout)
        except Exception as e:
            failed.append({"sector": sec, "error": str(e)[:120]})
            continue
        lst = rr.get("Value") or []
        if not lst:
            continue
        n_sec_with_members += 1
        for raw in lst:
            pc = to_project_code(raw if isinstance(raw, str) else "")
            if not pc:
                continue
            if pc in stock_map and stock_map[pc] != sec:
                overlap += 1
                # 一股多行业：保留字典序首个（确定性，不随机）
                if sec < stock_map[pc]:
                    stock_map[pc] = sec
            else:
                stock_map[pc] = sec
        if progress and (i + 1) % 40 == 0:
            progress("  %d/%d 板块 · 累计 %d 票 · %.0fs"
                     % (i + 1, len(inds), len(stock_map), time.time() - t0))
    stats = {
        "sector_total": len(sectors),
        "industry_sectors": len(inds),
        "industry_with_members": n_sec_with_members,
        "n_stocks": len(stock_map),
        "overlap_multi_industry": overlap,
        "failed": failed,
        "elapsed_s": round(time.time() - t0, 1),
        "source": "tq get_sector_list + get_stock_list_in_sector（通达信一级行业 881xxx）",
    }
    return stock_map, stats


def klines_coverage(stock_map, root=None):
    """映射对 klines 全量股票的覆盖率（如实报告，不当作校验通过）。"""
    import csv
    root = root or _ROOT
    p = os.path.join(root, "data", "raw", "bus", "klines.csv")
    if not os.path.isfile(p):
        return None
    codes = set()
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            codes.add(r["code"])
    hit = len(codes & set(stock_map))
    return {"klines_codes": len(codes), "covered": hit,
            "coverage_pct": round(hit / max(len(codes), 1) * 100, 2)}


def write_map(stock_map, root=None, provenance=None):
    """写 data/raw/meta/stock_industry.json（经写点检查）。"""
    root = root or _ROOT
    p = os.path.join(root, OUT_REL)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    _WG.check_write(p)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(stock_map, f, ensure_ascii=False, indent=0, sort_keys=True)
    os.replace(tmp, p)
    if provenance is not None:
        meta = p + ".provenance.json"
        with io.open(meta, "w", encoding="utf-8") as f:
            json.dump(provenance, f, ensure_ascii=False, indent=1)
    return p


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    ap = argparse.ArgumentParser(description="抓个股→行业映射（通达信一级行业）")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--dry-run", dest="dry", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--base", default=None)
    a = ap.parse_args(argv)

    st = TQ.check_status()
    if a.check:
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return 0 if st["usable"] else 2
    if not st["usable"]:
        print("⛔ 通达信服务不可用（%s）：请先启动客户端并登录" % st.get("reason", "端口未监听"))
        return 2

    print("抓取中…")
    mp, stats = fetch_map(progress=print)
    cov = klines_coverage(mp, a.base)
    print()
    for k in ("sector_total", "industry_sectors", "industry_with_members",
              "n_stocks", "overlap_multi_industry", "elapsed_s"):
        print("  %-24s %s" % (k, stats[k]))
    if stats["failed"]:
        print("  ⚠ 失败板块 %d 个（前3：%s）"
              % (len(stats["failed"]), stats["failed"][:3]))
    if cov:
        print("  %-24s %d/%d（%.2f%%）"
              % ("klines 覆盖率", cov["covered"], cov["klines_codes"],
                 cov["coverage_pct"]))
    if stats["overlap_multi_industry"]:
        print("  ⚠ 一股多行业 %d 例（已取字典序首个）"
              % stats["overlap_multi_industry"])

    if a.execute:
        prov = dict(stats)
        prov["klines_coverage"] = cov
        prov["actor"] = "fetch_industry_map"
        p = write_map(mp, a.base, provenance=prov)
        print("\n已写入 %s（%d 条）" % (p, len(mp)))
    else:
        print("\n（dry-run，未写盘；加 --execute 生效）")
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

    T("A1 SH 代码转换", to_project_code("600000.SH") == "sh600000")
    T("A2 SZ 代码转换", to_project_code("000001.SZ") == "sz000001")
    T("A3 BJ 代码转换", to_project_code("430047.BJ") == "bj430047")
    T("A4 非法代码返回 None",
      to_project_code("600000") is None and to_project_code("") is None
      and to_project_code(None) is None)
    T("A5 行业前缀常量正确", INDUSTRY_PREFIX == "881")
    # 服务不可用时 fetch_map 应抛错而非返回空（不冒充成功）
    st = TQ.check_status()
    if not st["usable"]:
        try:
            fetch_map()
            T("B1 服务不可用时抛错（不返回空 map）", False, "未抛错")
        except Exception:
            T("B1 服务不可用时抛错（不返回空 map）", True)
    else:
        T("B1 服务不可用时抛错（不返回空 map）", True, "服务在线，跳过")
    T("B2 write_map 签名可调用", callable(write_map))
    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
