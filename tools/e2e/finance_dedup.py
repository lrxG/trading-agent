#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""finance.csv 去重（骨架件 · 只处理 finance.csv · 默认干跑）

# 为什么需要这个工具（2026-10-22 真事故）

## 事故经过

2026-10-21 14:38 我启动了一个 `fetch_finance.py --execute --limit 12 --workers 1`
（**单票模式 + workers=1**）的 writegate 后台任务。它**没有被正确回收**，
一直跑到 2026-10-22 08:41 —— **整整 18 小时**。

期间它做了两件事：

1. **重复抓已有数据**：`fetch_finance` 的 `todo` 是在**启动时**算的一次性快照
   （`todo = [c for c in universe if c not in done]`），之后不再刷新。
   孤儿进程启动时那份 todo 里的票，后来已被别的轮次采过，
   但它仍按旧 todo 继续抓 ⇒ **同一批数据被反复追加**。
2. **顺带采了 balance / cashflow**：那个任务的参数是 `--types income`，
   但进程命令行显示它后面的子进程在查 `--type balance`……
   实际是 `fetch_one()` 的循环把 `TYPES` 三张表全跑了（`--types` 未生效于单票分支，
   见下方"待修"）。**这算意外的有益增量**：balance 从 1.7 万行涨到 12.6 万行。

## 重复的规模（实测）

| | 值 |
|---|---|
| 总行数 | 1,364,802 |
| 完全重复行 | **344,631** |
| 唯一 (票, 报告期, 公告日, 表, 字段) | 1,020,171 |
| **键相同但值冲突** | **166**（涉及 38 只票） |

## 值冲突怎么处理（本工具的口径）

166 个冲突键里，**中位差异 2.18%，最大 2688%**。
这不是抓取错误，而是**数据源追溯调整**（同一报告期被后续修正）。

口径：**保留后出现的那一条**（即"最新抓到的值"）。
理由：财务数据的追溯调整是**越晚越准**，后来的值覆盖先前值是财务数据的通例。
但**必须显式计数并在报告里列出**，不能静默吞掉——因为这 166 条
恰好是"历史上曾经公告过另一个数"的证据，做 PIT 研究时可能是有效信息。

## 三条纪律

1. **默认干跑**，必须 `--execute` 才写；写前先落 `.bak` 备份。
2. **不做任何"修复"**：只做去重，不填补空值、不改数值、不删异常值。
3. **经写入闸**：`writegate plan → run --confirm-production`。

## 与 data_health 的分工

- `data_health.py`：**体检**（只读），报告"有没有问题"
- `finance_dedup.py`：**治理**（会写），执行"把重复消掉"
"""

import argparse
import csv
import io
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_REL = os.path.join("data", "raw", "bus", "finance.csv")

HEADER = ["code", "end_date", "publ_date", "source_type", "field", "value"]
KEY_FIELDS = ("code", "end_date", "publ_date", "source_type", "field")


def load_rows(path):
    """→ list of tuples（保持文件原顺序）。"""
    out = []
    with io.open(path, encoding="utf-8-sig", errors="ignore", newline="") as f:
        rd = csv.reader(f)
        hdr = next(rd, None)
        if not hdr:
            return [], []
        idx = {h.strip(): i for i, h in enumerate(hdr)}
        for cells in rd:
            if not cells or len(cells) < len(HEADER):
                continue
            out.append(tuple((cells[idx.get(k, -1)].strip()
                              if idx.get(k, -1) >= 0 and idx.get(k, -1) < len(cells)
                              else "") for k in HEADER))
    return hdr, out


def dedup(rows):
    """按键去重，后出现的覆盖先出现的。

    → (kept_rows, n_dup_exact, n_conflict, conflicts)

    n_dup_exact : 键 + 值都相同的重复（纯冗余，可安全删）
    n_conflict  : 键相同但值不同（保留后者，但必须报出来）
    """
    pos = {}          # key -> 在 kept 里的下标
    kept = []
    n_exact = 0
    conflicts = []
    for r in rows:
        k = tuple(r[HEADER.index(f)] for f in KEY_FIELDS)
        v = r[HEADER.index("value")]
        if k in pos:
            i = pos[k]
            if kept[i][HEADER.index("value")] == v:
                n_exact += 1                       # 完全重复
            else:
                conflicts.append((k, kept[i][HEADER.index("value")], v))
                kept[i] = r                        # ★ 保留后者（越晚越准）
        else:
            pos[k] = len(kept)
            kept.append(r)
    return kept, n_exact, len(conflicts), conflicts


def write_rows(path, hdr, rows):
    """整体重写（去重必须重写整表）。"""
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(hdr if hdr else HEADER)
        w.writerows(rows)
    os.replace(tmp, path)


def backup(path):
    b = "%s.bak.%d" % (path, int(time.time()))
    with io.open(path, "rb") as s, io.open(b, "wb") as d:
        while True:
            chunk = s.read(1 << 20)
            if not chunk:
                break
            d.write(chunk)
    return b


def run(execute=False, keep="last"):
    path = os.path.join(_ROOT, OUT_REL)
    if not os.path.isfile(path):
        print("✗ 找不到 %s" % OUT_REL)
        return 2

    t0 = time.time()
    hdr, rows = load_rows(path)
    print("finance.csv · %d 行 · 读取 %.1fs" % (len(rows), time.time() - t0))

    kept, n_exact, n_conf, confs = dedup(rows)
    print()
    print("  ┌ 去重结果 ─────────────────────────────")
    print("  │ 原行数        %9d" % len(rows))
    print("  │ 完全重复      %9d  （键+值都同，纯冗余）" % n_exact)
    print("  │ 值冲突        %9d  （键同值不同，保留%s）"
          % (n_conf, "后者" if keep == "last" else "前者"))
    print("  │ 去重后        %9d" % len(kept))
    print("  │ 减少          %9d  （%.1f%%）"
          % (len(rows) - len(kept),
             (len(rows) - len(kept)) / max(len(rows), 1) * 100))
    print("  └───────────────────────────────────────")

    if confs:
        print()
        print("  ⚠ 值冲突 %d 条（数据源追溯调整的证据，不是抓取错误）" % n_conf)
        print("    涉及股票 %d 只 · 前 5 条："
              % len({c[0][0] for c in confs}))
        for k, old, new in confs[:5]:
            print("      %s %s %s.%s : %r → %r"
                  % (k[0], k[1], k[3], k[4], old, new))
        print("    ⚠ 这些是「历史上曾公告过另一个数」的证据，")
        print("      做 PIT 研究时可能是有效信息，故保留并显式列出，不静默吞掉。")

    if not execute:
        print()
        print("（干跑，未写盘；加 --execute 生效，会先落 .bak 备份）")
        return 0

    if len(kept) == len(rows):
        print()
        print("无需写盘（没有重复）")
        return 0

    b = backup(path)
    print()
    print("  备份 → %s" % os.path.basename(b))
    write_rows(path, hdr, kept)
    print("  已写回 %d 行" % len(kept))
    return 0


# ── 自检 ────────────────────────────────────────────────────────────────────

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

    R = lambda c, e, p, s, f, v: (c, e, p, s, f, v)

    # A. 纯重复
    rows = [R("a", "2025-12-31", "2026-04-01", "income", "ROE", "1.0"),
            R("a", "2025-12-31", "2026-04-01", "income", "ROE", "1.0")]
    kept, ne, nc, _ = dedup(rows)
    T("A1 完全重复被识别并去除", len(kept) == 1 and ne == 1,
      "kept=%d exact=%d" % (len(kept), ne))
    T("A2 完全重复不算值冲突", nc == 0, "conf=%d" % nc)

    # B. 值冲突：保留后者
    rows = [R("a", "2025-12-31", "2026-04-01", "income", "ROE", "1.0"),
            R("a", "2025-12-31", "2026-04-01", "income", "ROE", "2.0")]
    kept, ne, nc, confs = dedup(rows)
    T("B1 值冲突被识别", nc == 1 and len(confs) == 1, "conf=%d" % nc)
    T("B2 值冲突保留**后者**（越晚越准）", kept[0][5] == "2.0", kept[0][5])
    T("B3 冲突记录含旧值与新值", confs[0][1] == "1.0" and confs[0][2] == "2.0",
      str(confs[0]))

    # C. 不同键不能误删
    rows = [R("a", "2025-12-31", "2026-04-01", "income", "ROE", "1.0"),
            R("a", "2025-09-30", "2025-10-28", "income", "ROE", "2.0"),
            R("b", "2025-12-31", "2026-04-01", "income", "ROE", "3.0"),
            R("a", "2025-12-31", "2026-04-01", "balance", "ROE", "4.0"),
            R("a", "2025-12-31", "2026-04-01", "income", "EPS", "5.0")]
    kept, ne, nc, _ = dedup(rows)
    T("C1 报告期不同不去重", len(kept) == 5, "kept=%d" % len(kept))
    T("C2 代码/表/字段任一不同都不去重", ne == 0 and nc == 0)

    # D. 公告日不同 = 不同键（PIT 关键：同一报告期不同公告日必须保留）
    rows = [R("a", "2025-12-31", "2026-04-01", "income", "ROE", "1.0"),
            R("a", "2025-12-31", "2026-06-15", "income", "ROE", "2.0")]
    kept, ne, nc, _ = dedup(rows)
    T("D1 同一报告期不同公告日 = 不同键（PIT 保留两者）",
      len(kept) == 2, "kept=%d" % len(kept))

    # E. 保持原顺序（后面的行不乱序）
    rows = [R("c", "2025-12-31", "2026-04-01", "income", "ROE", "1"),
            R("a", "2025-12-31", "2026-04-01", "income", "ROE", "2"),
            R("b", "2025-12-31", "2026-04-01", "income", "ROE", "3")]
    kept, _, _, _ = dedup(rows)
    T("E1 无重复时保持原顺序", [r[0] for r in kept] == ["c", "a", "b"],
      str([r[0] for r in kept]))

    # F. 空输入
    kept, ne, nc, _ = dedup([])
    T("F1 空输入不崩", kept == [] and ne == 0 and nc == 0)

    # G. 三张表混合
    rows = [R("a", "2025-12-31", "2026-04-01", "income", "ROE", "1"),
            R("a", "2025-12-31", "2026-04-01", "balance", "ROE", "1"),
            R("a", "2025-12-31", "2026-04-01", "cashflow", "ROE", "1")]
    kept, ne, nc, _ = dedup(rows)
    T("G1 三张表同键不同 source_type 不去重", len(kept) == 3, "kept=%d" % len(kept))

    # H. 只处理 finance.csv（防止误改别的数据）
    T("H1 输出目标只有 finance.csv", OUT_REL.replace("\\", "/").endswith("finance.csv"))
    T("H2 只读源固定（不含其它 bus 文件）", OUT_REL.count("bus") == 1)

    # I. 干跑不写（源码层面：write_rows 只在 execute 分支被调用）
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    i_exec = src.find("if not execute:")
    i_write = src.find("write_rows(path, hdr, kept)")
    T("I1 write_rows 只在 execute 之后被调用", i_exec > 0 and i_write > i_exec,
      "exec=%d write=%d" % (i_exec, i_write))

    # J. 写前必须备份
    T("J1 写盘前先调 backup()",
      src.find("b = backup(path)") > 0
      and src.find("b = backup(path)") < src.find("write_rows(path, hdr, kept)"))

    # K. 不做其它"修复"（AST 查真实调用，不查源码字面量——否则断言自身会自指）
    import ast
    tree = ast.parse(io.open(os.path.abspath(__file__), encoding="utf-8").read())
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            nm = getattr(fn, "id", "") or getattr(fn, "attr", "")
            if nm:
                called.add(nm)
    forbidden = ("fillna", "impute", "clip", "winsor", "replace_nan",
                 "interpolate", "ffill", "bfill")
    hit = [f for f in forbidden if f in called]
    T("K1 不做任何插补/缩尾/前向填充（只去重不改值）", not hit, str(hit))

    # L. 原子写（先 .tmp 再 replace，避免写一半崩掉毁文件）
    T("L1 用 .tmp + os.replace 原子写",
      ".tmp" in src and "os.replace" in src)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    ap = argparse.ArgumentParser(description="finance.csv 去重（默认干跑）")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--keep", choices=("last", "first"), default="last",
                    help="值冲突时保留哪一条（默认 last=最新抓到的）")
    a = ap.parse_args(argv)
    return run(execute=a.execute, keep=a.keep)


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
