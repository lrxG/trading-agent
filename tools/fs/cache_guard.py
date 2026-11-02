#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缓存新鲜度守卫（骨架件之外的纯工具 · 只读 · 零写点）

# 为什么需要这个模块（2026-10-22 真事故）

## 事故

`fa_rule.py` 读 `TEMP/mr_sub_vol.pkl`（2026-10-21 12:43 生成的老缓存），
我把财务覆盖率从 22.3% 补到 97.6% 之后重跑，**样本数一个没变（39403 → 39403）**，
得出「ROE 池内排序 WF 35.8%/夏普 1.76 = 唯一候选」。

换成全口径样本重跑后是 **WF −14.1% / 夏普 −0.24**。

**这不是数据变了，是「以为重跑了，其实读的是旧缓存」。**

## 现状盘点（项目里共 9 个 TEMP 缓存，约 1.1 GB，全无新鲜度检查）

| 缓存 | 消费者 |
|---|---|
| `mr_sub_vol.pkl` | fa_rule.py ← **出事的就是它** |
| `mr_full21.pkl` / `mr_samples.pkl` / `mr_2024h1.pkl` | fa_rule / mr_* |
| `alpha_panel.pkl` / `mkt_index.pkl` | alpha_matrix.py |
| `ind_med.pkl` | fa_study.py / g1_mktcap.py |
| `sf_stocks.pkl` / `sf_etfs.pkl` | strategy_family.py |

## 判据（M24）

**任何研究工具的缓存，加载时必须能回答三个问题：**
1. 这份缓存**什么时候**生成的？
2. 它覆盖的**日期范围**是什么？
3. 它依赖的**源数据**在这之后有没有更新过？

第 3 条是本次事故的盲点——缓存比源数据旧，却没人检查。

## 用法

    import cache_guard as CG

    data, meta = CG.load_cached(
        path,
        builder=build_fn,              # 缓存不存在/过期时的重建函数
        source_globs=["data/raw/bus/finance.csv"],   # 依赖的源数据
        label="mr_sub_vol",
    )

    # 或只检查已有缓存（不重建）
    rep = CG.inspect(path, source_globs=[...])
    CG.print_report(rep)

## 三条纪律

1. **只读**：本模块不写任何文件，只做检查与报告（重建由调用方 builder 负责）。
2. **不静默**：过期/未知一律打印警告，禁止 `warnings.filterwarnings("ignore")` 式静默。
3. **可失败**：`strict=True` 时过期直接抛 `StaleCacheError`，让调用方显式决定用不用。
"""

import glob
import gzip
import io
import os
import pickle
import sys
import time

# 默认源数据（缓存重建依赖的落点）。相对项目根。
DEFAULT_SOURCES = (
    "data/raw/bus/klines.csv",
    "data/raw/bus/finance.csv",
    "data/raw/bus/mktcap.csv",
    "data/raw/meta/stock_industry.json",
)

# 超过这个年龄（秒）就至少告警。默认 1 天。
DEFAULT_MAX_AGE = 24 * 3600


class StaleCacheError(Exception):
    """缓存过期/无法判定，且调用方要求严格模式。"""


def _newest_mtime(globs, root=None):
    """源数据里最新的 mtime（None=一个都不存在）。"""
    root = root or os.getcwd()
    newest = None
    newest_p = None
    for g in globs:
        pat = g if os.path.isabs(g) else os.path.join(root, g)
        for p in glob.glob(pat):
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if newest is None or m > newest:
                newest, newest_p = m, p
    return newest, newest_p


def _peek_range(obj):
    """尽力猜缓存的日期范围（不假定结构）。

    → (d0, d1, n) ；猜不出就 (None, None, len 或 None)
    """
    n = None
    d0 = d1 = None
    try:
        if isinstance(obj, dict):
            # {code: {date: ...}} 形态
            n = len(obj)
            ds = []
            for v in obj.values():
                if isinstance(v, dict):
                    ds.extend(list(v.keys())[:1])
                    ds.extend(list(v.keys())[-1:])
            if ds and all(isinstance(x, str) and len(x) == 10 for x in ds[:4]):
                d0, d1 = min(ds), max(ds)
        elif isinstance(obj, (list, tuple)):
            n = len(obj)
            ds = [x.get("date") for x in obj
                  if isinstance(x, dict) and isinstance(x.get("date"), str)]
            if ds:
                d0, d1 = min(ds), max(ds)
    except Exception:
        pass
    return d0, d1, n


def inspect(path, source_globs=None, max_age=DEFAULT_MAX_AGE, root=None):
    """检查单个缓存。→ dict（只读，不重建）。"""
    source_globs = tuple(source_globs or DEFAULT_SOURCES)
    rep = {
        "path": path,
        "exists": os.path.exists(path),
        "mtime": None,
        "age_sec": None,
        "size_mb": None,
        "d0": None, "d1": None, "n": None,
        "src_newest": None, "src_newest_path": None,
        "verdict": "UNKNOWN",
        "reasons": [],
    }
    if not rep["exists"]:
        rep["verdict"] = "MISSING"
        rep["reasons"].append("缓存文件不存在（首次运行需重建）")
        return rep

    st = os.stat(path)
    rep["mtime"] = st.st_mtime
    rep["age_sec"] = time.time() - st.st_mtime
    rep["size_mb"] = st.st_size / 1e6

    # 源数据新鲜度
    sm, sp = _newest_mtime(source_globs, root=root)
    rep["src_newest"] = sm
    rep["src_newest_path"] = sp

    # 读内容猜范围（可能很大，失败不影响判定）
    try:
        with open(path, "rb") as f:
            head = f.read(2)
        if head == b"\x1f\x8b":
            with gzip.open(path, "rb") as f:
                obj = pickle.load(f)
        else:
            with open(path, "rb") as f:
                obj = pickle.load(f)
        rep["d0"], rep["d1"], rep["n"] = _peek_range(obj)
        del obj
    except Exception as e:
        rep["reasons"].append("内容无法解析：%s" % str(e)[:60])

    # 判定
    stale_by_src = (sm is not None and sm > rep["mtime"])
    stale_by_age = (rep["age_sec"] is not None and rep["age_sec"] > max_age)

    if stale_by_src and stale_by_age:
        rep["verdict"] = "STALE"
        rep["reasons"].append(
            "**源数据已更新**（%s 比缓存新 %.1f 小时）"
            % (os.path.basename(rep["src_newest_path"] or "?"),
               (sm - rep["mtime"]) / 3600))
        rep["reasons"].append("缓存本身也已超过 %.0f 小时" % (max_age / 3600))
    elif stale_by_src:
        rep["verdict"] = "STALE"
        rep["reasons"].append(
            "**源数据已更新**（%s 比缓存新 %.1f 小时）⇒ 缓存必然不含新数据"
            % (os.path.basename(rep["src_newest_path"] or "?"),
               (sm - rep["mtime"]) / 3600))
    elif stale_by_age:
        rep["verdict"] = "OLD"
        rep["reasons"].append("缓存已超过 %.0f 小时（源数据未变，风险较低）"
                              % (max_age / 3600))
    else:
        rep["verdict"] = "FRESH"
    return rep


def print_report(rep, prefix="  "):
    """打印一份可读报告。**必须调用**——不打印等于没检查。"""
    name = os.path.basename(rep["path"])
    if not rep["exists"]:
        print("%s[缓存] %-22s ✗ 不存在（将重建）" % (prefix, name))
        return
    mt = time.strftime("%m-%d %H:%M", time.localtime(rep["mtime"]))
    rng = ("%s ~ %s" % (rep["d0"], rep["d1"])) if rep["d0"] else "范围未知"
    n = ("%d 条" % rep["n"]) if rep["n"] is not None else "条数未知"
    flag = {"FRESH": "✓", "OLD": "·", "STALE": "⚠", "UNKNOWN": "?"}.get(
        rep["verdict"], "?")
    print("%s[缓存] %s %-22s %6.1f MB · %s · %s · %s"
          % (prefix, flag, name, rep["size_mb"] or 0, mt, rng, n))
    for r in rep["reasons"]:
        print("%s        %s" % (prefix, r))


def load_cached(path, builder, source_globs=None, max_age=DEFAULT_MAX_AGE,
                root=None, strict=False, force=False, label=None, quiet=False):
    """加载缓存（不存在/过期则重建）。→ (data, meta)

    `meta` 里含 verdict / mtime / 日期范围 / 是否重建，**调用方应打出来**。

    strict=True 且缓存过期 ⇒ 抛 StaleCacheError（不静默用旧数据）。
    """
    rep = inspect(path, source_globs=source_globs, max_age=max_age, root=root)
    rep["label"] = label or os.path.basename(path)

    need_build = force or not rep["exists"]
    if rep["verdict"] == "STALE" and strict and not force:
        raise StaleCacheError(
            "缓存 %s 已过期：%s（如确认要用旧数据，加 force=True）"
            % (rep["label"], "；".join(rep["reasons"])))

    if not quiet:
        print_report(rep)

    if need_build:
        t0 = time.time()
        data = builder()
        if not quiet:
            print("        ↑ 已重建（%.1fs）" % (time.time() - t0))
        rep["rebuilt"] = True
        # 重建后重读一次元信息
        rep2 = inspect(path, source_globs=source_globs, max_age=max_age,
                       root=root)
        rep2["rebuilt"] = True
        rep2["label"] = rep["label"]
        return data, rep2

    with open(path, "rb") as f:
        head = f.read(2)
    if head == b"\x1f\x8b":
        with gzip.open(path, "rb") as f:
            data = pickle.load(f)
    else:
        with open(path, "rb") as f:
            data = pickle.load(f)
    rep["rebuilt"] = False
    if rep["verdict"] in ("STALE", "OLD") and not quiet:
        print("        ⚠ 仍在使用%s缓存（如需最新数据请加 force=True 重建）"
              % ("过期" if rep["verdict"] == "STALE" else "较旧"))
    return data, rep


def scan(patterns, source_globs=None, max_age=DEFAULT_MAX_AGE):
    """扫一批缓存文件，返回全部报告（用于体检）。"""
    out = []
    for pat in patterns:
        for p in glob.glob(pat):
            out.append(inspect(p, source_globs=source_globs, max_age=max_age))
    return out


# ── 自检 ────────────────────────────────────────────────────────────────────

def _selftest():
    import tempfile
    import shutil
    ok = fail = 0

    def T(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ✓ %s" % name)
        else:
            fail += 1
            print("  ✗ %s  %s" % (name, extra))

    d = tempfile.mkdtemp()
    try:
        src = os.path.join(d, "src.csv")
        cache = os.path.join(d, "c.pkl")

        # A 缓存不存在
        rep = inspect(cache, source_globs=[src], root=d)
        T("A1 缓存不存在 → MISSING", rep["verdict"] == "MISSING", rep["verdict"])

        # B 缓存与源同时间 → FRESH
        io.open(src, "w", encoding="utf-8").write("a\n")
        pickle.dump([{"date": "2026-10-01"}, {"date": "2026-10-20"}],
                    open(cache, "wb"))
        now = time.time()
        os.utime(src, (now, now))
        os.utime(cache, (now, now))
        rep = inspect(cache, source_globs=[src], root=d)
        T("B1 源与缓存同时间 → FRESH", rep["verdict"] == "FRESH", rep["verdict"])
        T("B2 能猜出日期范围", rep["d0"] == "2026-10-01" and rep["d1"] == "2026-10-20",
          "%s~%s" % (rep["d0"], rep["d1"]))
        T("B3 能猜出条数", rep["n"] == 2, str(rep["n"]))

        # C 源比缓存新 → STALE（**本次事故的核心判据**）
        os.utime(src, (now + 7200, now + 7200))     # 源晚 2 小时
        rep = inspect(cache, source_globs=[src], root=d)
        T("C1 **源比缓存新 → STALE**（事故判据）",
          rep["verdict"] == "STALE", rep["verdict"])
        T("C2 STALE 理由提到源数据已更新",
          any("源数据已更新" in r for r in rep["reasons"]), str(rep["reasons"]))

        # D 只有缓存老、源没变 → OLD（不是 STALE，风险低）
        #   ★ 必须先把源时间设回「早于缓存」，否则残留的上一步 mtime 会误判 STALE
        os.utime(cache, (now - 3 * 24 * 3600, now - 3 * 24 * 3600))
        os.utime(src, (now - 4 * 24 * 3600, now - 4 * 24 * 3600))  # 源更早
        rep = inspect(cache, source_globs=[src], root=d, max_age=3600)
        T("D1 源未变但缓存老 → OLD（不与 STALE 混为一谈）",
          rep["verdict"] == "OLD", "%s / %s" % (rep["verdict"], rep["reasons"]))

        # D2 反向验证：源比缓存新时必须判 STALE，不能因为「缓存老」就降级成 OLD
        os.utime(src, (now + 7200, now + 7200))
        rep = inspect(cache, source_globs=[src], root=d, max_age=3600)
        T("D2 源新+缓存老 ⇒ STALE 优先于 OLD", rep["verdict"] == "STALE",
          rep["verdict"])

        # E strict 模式对 STALE 抛错
        os.utime(src, (now + 7200, now + 7200))
        try:
            load_cached(cache, builder=lambda: [], source_globs=[src],
                        root=d, strict=True, quiet=True)
            T("E1 strict 对 STALE 抛 StaleCacheError", False, "没抛")
        except StaleCacheError:
            T("E1 strict 对 STALE 抛 StaleCacheError", True)

        # F force 重建
        built = {"built": True}
        data, meta = load_cached(cache, builder=lambda: built,
                                 source_globs=[src], root=d,
                                 force=True, quiet=True)
        T("F1 force=True 会调 builder 重建", data is built and meta["rebuilt"])

        # G 不存在时自动重建
        c2 = os.path.join(d, "c2.pkl")
        data2, meta2 = load_cached(c2, builder=lambda: built,
                                   source_globs=[src], root=d, quiet=True)
        T("G1 缓存不存在会自动重建", data2 is built and meta2["rebuilt"])

        # H 内容坏了不崩
        bad = os.path.join(d, "bad.pkl")
        io.open(bad, "wb").write(b"not a pickle")
        rep = inspect(bad, source_globs=[src], root=d)
        T("H1 坏缓存不崩且记原因",
          rep["exists"] and any("无法解析" in r for r in rep["reasons"]),
          str(rep["reasons"])[:60])

        # I gzip 缓存可读
        gzc = os.path.join(d, "c.pkl.gz")
        with gzip.open(gzc, "wb") as f:
            pickle.dump([{"date": "2026-09-01"}], f)
        rep = inspect(gzc, source_globs=[src], root=d)
        T("I1 gzip 缓存可解析", rep["d0"] == "2026-09-01", str(rep["d0"]))

        # J 只读自证：**生产代码区**（_selftest 之前）不得有写调用。
        #   自检自己要造临时夹具，天然有写；判据必须只作用于生产段，否则自指。
        src_txt = io.open(os.path.abspath(__file__), encoding="utf-8").read()
        prod_txt = src_txt.split("def _selftest(")[0]
        import ast
        tree = ast.parse(prod_txt)
        writes = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                nm = getattr(fn, "id", "") or getattr(fn, "attr", "")
                if nm in ("remove", "unlink", "replace", "rename", "rmtree"):
                    writes.append(nm)
                if nm == "open" and len(node.args) > 1:
                    a = node.args[1]
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                            and any(c in a.value for c in "wax+"):
                        writes.append("open(%s)" % a.value)
        T("J1 生产段零写点（只读检查器；自检夹具不算）", not writes, str(writes))

        # K 三个问题必须都能回答
        rep = inspect(cache, source_globs=[src], root=d)
        T("K1 能答『缓存何时生成』", rep["mtime"] is not None)
        T("K2 能答『覆盖什么范围』", rep["d0"] is not None or rep["n"] is not None)
        T("K3 能答『源数据是否更新过』", rep["src_newest"] is not None)
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    if "--selftest" in argv or not argv:
        return _selftest()
    if "--scan" in argv:
        tmp = os.environ.get("TEMP", "/tmp")
        reps = scan([os.path.join(tmp, "*.pkl"),
                     os.path.join(tmp, "*.pkl.gz")])
        print("TEMP 缓存体检（共 %d 个）" % len(reps))
        print("=" * 78)
        total = 0.0
        for r in sorted(reps, key=lambda x: -(x["size_mb"] or 0)):
            print_report(r)
            total += r["size_mb"] or 0
        print("=" * 78)
        bad = [r for r in reps if r["verdict"] in ("STALE", "OLD")]
        print("合计 %.0f MB · 过期/较旧 %d 个" % (total, len(bad)))
        return 0
    print("用法：--selftest | --scan")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
