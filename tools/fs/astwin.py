#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""astwin —— AST 静态扫描的**公共窗口工具（唯一实现）**（骨架基础设施 · 零业务）

★ 为什么有本文件（施工方案「插件期开工前置清单」P1 · 用户 2026-10-08 令）：
  「取 AST 节点尾界」这条判据曾在本项目 **8 个模块复制 23 处**，且**已发生版本漂移**：
    · 同一判据两种写法并存 —— 旧的 `max([x.lineno ...])` 只取到多行语句的**起始行**
      （没用 `end_lineno`）→ 尾部 `)` `}` 等续行**漏在窗口之外**；
    · **同一文件内两种写法并存**（`observe.py` 3 处旧 + 1 处新）= 漂移最刺眼的证据；
    · 连变量名都不统一（`lines` / `ls`、`funcs` / `fns`）。
  缺口的**方向是"过严"而非"漏报"**：窗口偏短 → 尾部被误当生产区 → 只会误报。
  F7 收口时它已显形为一次**真缺陷**（置空残留 `}` → `IndentationError`）。
  用户判定 = **插件期开工前的基础设施债**：先把判据收成唯一实现，再开策略插件，
  否则插件越多、这份复制漂移越扩散（每插一个插件就多一处要同步的副本）。

★ 边界（本模块**只算窗口，不判违规**）：
  · 只做「节点覆盖哪些行」的计算；**不含任何判据 / 业务规则 / 路径 / 写盘**；
  · 调用方各自决定"落在生产区还是测试区""算不算违例"。

★ 影响面（必须写明，防将来误判本模块风险）：
  全部 23 处调用点**都在审计侧**（`--discipline` / `--selftest` 的静态扫描），
  故本模块**不改变任何生产行为**，只改变审计窗口的**准确度**。

★ 纪律：本模块是**唯一实现**——禁止任何模块再自写尾界判定；
  新增需要窗口计算的地方**一律引用本模块**（施工纪律 6「一个知识点只有一个家」）。
"""

import ast

__all__ = ["bounds", "line_set", "func_table", "owner_of"]

# 测试入口自报（0.7 落点审计按此派生测试区种子，勿删）
_TEST_FUNCS = ("_selftest",)


# 历史旧写法（**拼装构造**：写成整字面量会命中本文件自己的反查判据；文档里允许出现，
# 判据只查代码区——与「真引用不是提到过」同口径）
_OLD_PATTERNS = (
    ("max([" + "x." + "lineno", "①内联 max(子节点.lineno)"),
    ("max(lines or [" + "n.lineno" + "])", "②函数区间表上界（变量 lines）"),
    ("max(ls or [" + "n.lineno" + "])", "②函数区间表变体（变量 ls）"),
)


def bounds(node):
    """返回 `(起始行, 尾行)`；尾行 = 后代（含自身）`end_lineno` 的最大值。

    ★ 唯一正确写法 = `getattr(x, "end_lineno", None) or x.lineno`。
      写成 `x.lineno` 会漏掉多行表达式尾部（如结尾的多行 `return {...}` / 多行调用
      的 `}` `)` 续行）——F7 的 `IndentationError` 正是出在这里。
      `end_lineno` 在 Python 3.8+ 才提供，故用 `or x.lineno` 兜底（本机 3.13）。
    """
    lo = node.lineno
    hi = getattr(node, "end_lineno", None) or lo
    for x in ast.walk(node):
        e = getattr(x, "end_lineno", None) or getattr(x, "lineno", None)
        if e is not None and e > hi:
            hi = e
    return lo, hi


def line_set(node, *, clamp=None):
    """节点覆盖的 **1-based** 行号集合；`clamp` = 源码总行数（越界截断，防 IndexError）。

    置空/跳过源码时用本函数，例：`for i in line_set(fn, clamp=len(lines)): lines[i-1] = ""`
    """
    lo, hi = bounds(node)
    if clamp is not None:
        hi = min(hi, clamp)
    return set(range(lo, hi + 1))


def func_table(tree):
    """顶层函数的 `(起始行, 尾行, 函数名)` 表（按行号升序）。

    owner 归属判定的**唯一来源** —— 别再在调用方各自 `max(lines or [n.lineno])`。
    """
    out = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef):
            lo, hi = bounds(n)
            out.append((lo, hi, n.name))
    return sorted(out)


def owner_of(funcs, lineno, default="?"):
    """行号落在哪个函数区间内；无归属返回 `default`（调用方可传 `None`）。"""
    for lo, hi, nm in funcs:
        if lo <= lineno <= hi:
            return nm
    return default


# ── 自检（--selftest）：纯 AST 计算，不碰文件系统 ────────────────────────────
def _selftest():
    import io
    import os

    ok = fail = 0
    fails = []

    def T(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [OK] %s" % name)
        else:
            fail += 1
            fails.append(name)
            print("  [!!] %s %s" % (name, extra))

    print("astwin 自检 —— AST 窗口唯一实现（%s）" % os.path.basename(__file__))

    # ① 单行节点：起止同为一行
    t = ast.parse("x = 1\n")
    n = t.body[0]
    T("A1 单行语句 bounds = (1, 1)", bounds(n) == (1, 1))

    # ② 多行表达式：**收尾括号独占一行**才是真区分用例——
    #    若 `}` 与内容同行（`{"b": 2}`），则内容常量本身就在尾行，
    #    旧写法取 max(lineno) 也能拿到同一尾界 → **测不出差异**。
    #    真正的缺口 = 尾行上**没有任何 AST 节点**（只有 `}` / `)`）。
    src2 = 'def f():\n    return {\n        "a": 1,\n    }\n'
    f2 = ast.parse(src2).body[0]
    T("A2 多行 return（`}` 独占一行）尾界到该行（= 4）",
      bounds(f2)[1] == 4, "实际 %r" % (bounds(f2),))

    # ③ 反证：旧写法（只取子节点 lineno 的 max）确实漏掉尾部 —— 证明这条判据有实质差异
    #    写成两行（先取列表再 max）以避开本模块 C1 的"无旧写法"自证（防判据自指）
    _old_lines = [x.lineno for x in ast.walk(f2) if hasattr(x, "lineno")]
    old_end = max(_old_lines or [f2.lineno])
    T("A3 旧写法(max lineno)=3 < 新写法(end_lineno)=4（证差异真实存在）",
      old_end == 3 and bounds(f2)[1] == 4, "旧 %d / 新 %d" % (old_end, bounds(f2)[1]))

    # ④ 多行调用尾部（F7 的 IndentationError 现场形态：`)` 独占一行）
    src3 = "def g():\n    h.write(\n        'x',\n    )\n"
    g = ast.parse(src3).body[0]
    T("A4 多行调用（`)` 独占一行）尾界到该行（= 4）",
      bounds(g)[1] == 4, "实际 %r" % (bounds(g),))

    # ⑤ line_set 覆盖 + clamp 截断
    T("A5 line_set 无 clamp 覆盖 1..4", line_set(f2) == {1, 2, 3, 4})
    T("A6 line_set clamp=2 截断为 1..2（防越界）", line_set(f2, clamp=2) == {1, 2})

    # ⑥ 函数表与 owner 归属
    src4 = ("def a():\n    pass\n\n\n"
            "def b():\n    return {\n        'k': 1,\n    }\n")
    tbl = func_table(ast.parse(src4))
    T("B1 func_table 两项且按行升序", [x[2] for x in tbl] == ["a", "b"], "实际 %r" % (tbl,))
    T("B2 a 区间 = (1, 2)", (tbl[0][0], tbl[0][1]) == (1, 2))
    T("B3 b 区间尾界到多行 return 的 `}` 行（= 8）", tbl[1][1] == 8, "实际 %r" % (tbl[1],))
    T("B4 owner_of 第 7 行属 b", owner_of(tbl, 7) == "b")
    T("B5 owner_of 空行无归属 → default", owner_of(tbl, 4) == "?", "实际 %r" % (owner_of(tbl, 4),))
    T("B6 owner_of 可自定义 default=None", owner_of(tbl, 4, None) is None)

    # ⑦ 唯一性自证：本模块**代码区**不得再出现旧写法（防"修完又漂回去"）
    #    ★ 两条陷阱都要避（F7 各栽过一次）：
    #      (a) **判据自指** → 针必须是**拼装构造**的，写成整字面量会命中判据自己的定义行；
    #      (b) **说明不算违例** → 本文件 docstring 里必须写明"旧写法长什么样"才能讲清来历，
    #          所以判据只查**代码区**（剔掉字符串字面量/文档字符串），与"真引用不是提到过"同口径。
    me = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    _needle = "max([" + "x." + "lineno"
    _blob = me
    for _nd in ast.walk(ast.parse(me)):
        if isinstance(_nd, ast.Constant) and isinstance(_nd.value, str):
            _blob = _blob.replace(_nd.value, "")
    T("C1 代码区无旧写法（判据拼装构造 + 剔文档字符串，双防自指）",
      _needle not in _blob, "命中位置 = 代码区，须改引用 astwin")
    T("C2 本模块生产区无写盘调用（纯计算）",
      not any(k in me.split("def _selftest")[0] for k in ("open(", ".write(", "os.replace")))

    # ⑧ 全库唯一性（跨模块 · P1 的真验收）：除本模块外，任何模块的**代码区**都不得再自写尾界判定
    #    ★ 这是"一个知识点只有一个家"的机械判据；缺了它，"唯一实现"只是口头承诺。
    tools_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hits = []
    for dp, _dn, fns in os.walk(tools_dir):
        if "__pycache__" in dp:
            continue
        for f in sorted(fns):
            if not f.endswith(".py"):
                continue
            p = os.path.join(dp, f)
            if os.path.abspath(p) == os.path.abspath(__file__):
                continue
            txt = io.open(p, encoding="utf-8").read()
            try:
                tr = ast.parse(txt)
            except SyntaxError as e:
                hits.append((os.path.relpath(p, tools_dir), "PARSE_ERROR %s" % e))
                continue
            blob = txt
            for nd in ast.walk(tr):
                if isinstance(nd, ast.Constant) and isinstance(nd.value, str):
                    blob = blob.replace(nd.value, "")     # 剔字符串/文档字符串（说明不算违例）
            for needle, label in _OLD_PATTERNS:
                if needle in blob:
                    hits.append((os.path.relpath(p, tools_dir), label))
    T("D1 全库尾界判据唯一（除本模块外 0 处自写副本）",
      not hits, "命中 = %r" % (hits[:6],))

    n_imp = 0
    for dp, _dn, fns in os.walk(tools_dir):
        if "__pycache__" in dp:
            continue
        for f in fns:
            if f.endswith(".py"):
                if "import astwin as ASTW" in io.open(os.path.join(dp, f), encoding="utf-8").read():
                    n_imp += 1
    T("D2 至少 8 个模块已改为引用本模块（证收拢真发生、非只删不接）",
      n_imp >= 8, "实际 %d 个" % n_imp)

    print("结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：%s" % ", ".join(fails))
    return 1 if fail else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
    print("用法：python astwin.py --selftest")
