#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""候选池非个股清理 —— 一次性治理工具（2026-10-22）

## 背景

`data/state/candidates.json` 4650 条里混了 **355 条非个股**（全部 EXPIRED）：

| 类型 | 数量 | 说明 |
|---|---|---|
| BOND | 180 | 可转债（sh110/111/113、sz11x/12x） |
| BSE_MISFIXED | 171 | **北交所股但前缀被错标成 sz**（如 sz920033 实为 bj920033 康普化学） |
| OTHER | 3 | sh246107 上交所回购 / sz148002 深市债券 / sz810014 新三板 |
| INDEX | 1 | 指数 |

根因：五路采纳**一条都没做代码类型校验**（`scan.add()` 原先裸收），
而判据散落四处口径不一，最松的那个决定了实际行为。
已由 `tools/fs/codes.py`（全项目唯一真源）+ `scan.add()` 类型闸根治。

## 本工具只做一件事

把 `is_stock()==False` 的历史条目**从候选池移除**，先备份、原子写、幂等。

## 纪律

- **只删 EXPIRED**。若有非 EXPIRED 的非个股，**拒绝执行并报错**——
  那说明有活跃污染，需要人看，不该被机械清掉。
- 先 `.bak` 备份（带时间戳），`.tmp` + `os.replace` 原子写。
- 默认干跑，`--execute` 才写。
- 幂等：再跑一次 0 条可清。
- AST 断言锁死：本工具**不修改**任何候选的业务字段（状态/触发/时间），只做整体删除。

用法：
  python tools/e2e/purge_nonstock_candidates.py            # 干跑
  python tools/e2e/purge_nonstock_candidates.py --selftest
  python tools/e2e/purge_nonstock_candidates.py --execute
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import time

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
_FS = os.path.join(_TOOLS, "fs")
if _FS not in sys.path:
    sys.path.insert(0, _FS)

import codes as CD  # noqa: E402

POOL = os.path.join(_ROOT, "data", "state", "candidates.json")


def scan_pool(path=None):
    """→ (total, removable, blocked, kinds)

    removable：EXPIRED 的非个股（可机械清）
    blocked  ：**非** EXPIRED 的非个股（拒绝清理，须人看）
    """
    path = path or POOL
    with io.open(path, encoding="utf-8") as f:
        d = json.load(f)
    cands = d.get("candidates") or []
    removable, blocked, kinds = [], [], {}
    for x in cands:
        if not isinstance(x, dict):
            continue
        code = x.get("code") or ""
        if CD.is_stock(code):
            continue
        k = CD.classify(code)
        kinds[k] = kinds.get(k, 0) + 1
        (removable if x.get("state") == "EXPIRED" else blocked).append(x)
    return len(cands), removable, blocked, kinds


def purge(path=None, execute=False, backup=True):
    """→ 报告 dict。干跑只算不写。"""
    path = path or POOL
    total, removable, blocked, kinds = scan_pool(path)

    rep = {"total": total, "removable": len(removable),
           "blocked": len(blocked), "kinds": dict(sorted(kinds.items())),
           "executed": False, "backup": None, "after": None}

    if blocked:
        # 活跃污染：拒绝执行。**不静默清理**——那会把"有活候选是转债"这种事埋掉。
        rep["error"] = ("存在 %d 条**非 EXPIRED** 的非个股（活跃污染），拒绝清理。"
                        "样例：%s" % (len(blocked),
                                     [x.get("code") for x in blocked[:5]]))
        return rep

    if not removable:
        rep["note"] = "无待清理项（幂等）"
        rep["after"] = total
        return rep

    if not execute:
        rep["note"] = "干跑：加 --execute 才写"
        return rep

    # 备份
    if backup:
        bak = path + ".bak.%d" % int(time.time())
        shutil.copy2(path, bak)
        rep["backup"] = os.path.basename(bak)

    with io.open(path, encoding="utf-8") as f:
        d = json.load(f)
    # ★ 只按「是否个股」过滤一次。**不要**再拿 scan_pool 的对象 id 去匹配——
    #   那是另一次 json.load 的临时对象，id 必然不同（2026-10-22 第一版就这么
    #   写出「两次过滤结果不一致 2 vs 1」的死循环）。幂等性由 `is_stock` 保证：
    #   同一个判据跑两遍结果必然相同。
    kept = [x for x in (d.get("candidates") or [])
            if not isinstance(x, dict) or CD.is_stock(x.get("code") or "")]

    d["candidates"] = kept
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)

    rep["executed"] = True
    rep["after"] = len(kept)
    return rep


def _selftest():
    ok = fail = 0

    def T(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ✓ %s" % name)
        else:
            fail += 1
            print("  ✗ %s  %s" % (name, detail))

    import tempfile
    print("purge_nonstock_candidates.py 自检")

    def mk(items, d=None):
        """造临时池。items = [(code, state)]"""
        d = d or tempfile.mkdtemp()
        p = os.path.join(d, "candidates.json")
        obj = {"version": 1, "candidates": [
            {"id": "CD-%03d" % i, "code": c, "state": s,
             "created_at": "2026-09-16", "last_seen": "2026-09-24",
             "source": "news_hot", "trigger": {"text": "x", "rule": None}}
            for i, (c, s) in enumerate(items)]}
        with io.open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        return p, d

    # A 干跑不写
    p, d = mk([("sh600000", "WATCH"), ("sh110084", "EXPIRED")])
    try:
        before = io.open(p, encoding="utf-8").read()
        r = purge(p, execute=False)
        T("A1 干跑识别 1 条可清", r["removable"] == 1, str(r))
        T("A2 干跑不写盘", io.open(p, encoding="utf-8").read() == before)
        T("A3 干跑报错为空", "error" not in r)

        # B 执行 + 幂等
        r = purge(p, execute=True)
        T("B1 执行成功", r["executed"] is True, str(r))
        T("B2 清后 1 条（只剩个股）", r["after"] == 1, str(r["after"]))
        T("B3 生成了备份", bool(r["backup"]))
        r2 = purge(p, execute=True)
        T("B4 幂等：再跑 0 条可清", r2["removable"] == 0, str(r2))
        T("B5 幂等：执行标记为 False（无可做之事）", r2["executed"] is False)

        # C 活跃污染 → 拒绝
        p2, d2 = mk([("sh110084", "WATCH")])
        r3 = purge(p2, execute=True)
        T("C1 非 EXPIRED 的非个股 → 拒绝执行", "error" in r3 and not r3["executed"],
          str(r3))
        T("C2 拒绝时盘上未被改动",
          "sh110084" in io.open(p2, encoding="utf-8").read())

        # D 类型统计如实
        p3, d3 = mk([("sh110084", "EXPIRED"), ("sz920033", "EXPIRED"),
                     ("sh600000", "EXPIRED")])
        r4 = purge(p3, execute=False)
        T("D1 类型统计含 BOND 与 BSE_MISFIXED",
          r4["kinds"].get("BOND") == 1 and r4["kinds"].get("BSE_MISFIXED") == 1,
          str(r4["kinds"]))
        T("D2 个股不在可清列表", r4["removable"] == 2, str(r4["removable"]))

        # E 业务字段零改动（只整体删除，不改字段）
        p4, d4 = mk([("sh600000", "WATCH"), ("sh110084", "EXPIRED")])
        before_obj = json.load(io.open(p4, encoding="utf-8"))["candidates"][0]
        purge(p4, execute=True)
        after_obj = json.load(io.open(p4, encoding="utf-8"))["candidates"][0]
        T("E1 保留条目业务字段逐字段未变", before_obj == after_obj,
          "%s vs %s" % (before_obj, after_obj))

        # F 只读自证：本工具不当场改字段，只整体删除（AST 层面确认没有字段赋值）
        src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
        import ast
        assigns = []
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Subscript):
                        v = t.value
                        nm = getattr(v, "id", "") or getattr(v, "attr", "")
                        if nm in ("x", "cand", "candidate", "item"):
                            assigns.append(ast.dump(t)[:60])
        T("F1 不对候选条目做字段级赋值（只整体删除）", not assigns, str(assigns))
    finally:
        for dd in (d, d2, d3, d4):
            shutil.rmtree(dd, ignore_errors=True)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    execute = "--execute" in argv
    path = POOL
    for i, a in enumerate(argv):
        if a == "--path" and i + 1 < len(argv):
            path = argv[i + 1]
        elif a == "--file" and i + 1 < len(argv):
            path = argv[i + 1]

    r = purge(path, execute=execute)
    print("候选池非个股清理 · %s" % os.path.basename(path))
    print("  清理前 %d 条 · 可清 %d 条 · 活跃污染 %d 条"
          % (r["total"], r["removable"], r["blocked"]))
    if r["kinds"]:
        print("  类型分布：%s" % r["kinds"])
    if "error" in r:
        print("  ⛔ %s" % r["error"])
        return 2
    if r.get("note"):
        print("  %s" % r["note"])
    if r["executed"]:
        print("  已清理 → 剩 %d 条 · 备份 %s" % (r["after"], r["backup"]))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit(_main(sys.argv[1:]))
