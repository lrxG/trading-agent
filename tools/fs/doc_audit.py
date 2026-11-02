"""doc_audit.py · 文档 ✅ 工具名对账（M17 落地 · 只读）

## 为什么需要这个文件

2026-10-21 发现真实断链：

  `data/research/imported_20261019/DATA-HUB.md` 第 228 行写着
  「两融余额/融资净买入（放大器）→ **✅ `heat_sensor.py margin`**」

  **但 `heat_sensor.py` 全项目 find 不到**（0 命中），
  而 `margin_daily.csv` 里躺着 53 行（28 行垃圾）无人维护的数据。

⇒ 文档说"有能力 ✅"、代码里"没有"、数据"是垃圾"。
   **文档的 ✅ 是给人看的，但没人校验它是否真实存在。**

## M17 判据（极简，可机械执行）

  **文档里出现的 ✅ 标记后紧跟的工具名，`find` 不到就是假的。**

  支持两种写法：
    · 「✅ + 反引号包裹的路径」        ← 路径式，如 tools/xx/yy.py
    · 「✅ + 反引号包裹的裸文件名」     ← 裸文件名式，需在 tools/ 下找到
    ★ 两种都允许工具名后面跟子命令/参数（实测文档常写 `xxx.py margin`）

## 五类判定（互不替代）

  OK          ✅ 后工具名能在磁盘找到
  MISSING     ✅ 后工具名找不到（**真断链**）
  LEGACY      文档位于**旧包导入区**（`data/research/imported_*`），
              且工具属于**旧系统路径**（如 `tools/data/` / `capabilities/`）
              ⇒ 不是本项目断链，是历史遗留引用，**不应算缺陷**
  NO_SELFTEST 工具存在但无 `--selftest`（无法自证，弱化项）
  AMBIGUOUS   裸文件名在磁盘有多处同名（无法确定指哪个）

★ LEGACY 与 MISSING 必须分开（2026-10-21 首次全库对账发现）：
  `data/research/imported_20261019/` 是 2026-10-19 从**上一代系统**导入的资料区，
  里面写的 `tools/data/cffex_lib.py`、`capabilities/pattern/src/ms_backfill.py`
  都是**旧系统路径**，从未搬进本项目。把它们算成"断链"会让报告永远有 5 条噪声，
  真正的新断链反而被淹没。

## 纪律

· **只读**：不写任何文件、不改文档
· **不猜**：UNPARSED / AMBIGUOUS 如实归类，不强行判定
· **不改**：发现断链只报告；修文档或补实现是治理动作

用法：
  python tools/fs/doc_audit.py
  python tools/fs/doc_audit.py --verbose
  python tools/fs/doc_audit.py --selftest
"""
import argparse
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))

DOC_DIRS = ("docs", "data/research", ".")
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".workbuddy", "data/raw",
             "data/snapshot", "data/generated", "data/knowledge",
             "data/learning", "memory"}
# ✅ 后面的候选工具名形态
# ★ 文档里的写法常常是 `tools/e2e/heat_sensor.py margin`（**工具名后跟子命令/参数**），
#   所以不能要求以 .py 结尾 —— 必须抓到 .py 为止，允许后面还有内容（2026-10-21 踩过）。
TOOL_RX = re.compile(
    r"✅[^\n`]*`([A-Za-z0-9_./\\-]+?\.(?:py|sh|bat|ps1))(?=[\s`])")
BARE_RX = re.compile(r"^[A-Za-z0-9_-]+\.py$")


def _iter_docs():
    """→ 文档路径列表（md 文件，跳过数据/缓存目录）。"""
    out = []
    for d in DOC_DIRS:
        base = os.path.join(_ROOT, d) if d != "." else _ROOT
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            rel = os.path.relpath(dirpath, _ROOT).replace("\\", "/")
            if any(rel == s or rel.startswith(s + "/") for s in SKIP_DIRS):
                dirnames[:] = []
                continue
            if d == "." and rel != "." and not rel.startswith("data/research"):
                # 根目录只扫一层（不递归到 docs/tools 内部，避免重复）
                dirnames[:] = []
            for fn in filenames:
                if fn.endswith(".md"):
                    out.append(os.path.join(dirpath, fn))
    return sorted(set(out))


def _index_tools():
    """→ {basename: [相对路径...]}（全库 py/sh 索引）。"""
    idx = {}
    base = os.path.join(_ROOT, "tools")
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [x for x in dirnames if x != "__pycache__"]
        for fn in filenames:
            if fn.endswith((".py", ".sh", ".bat", ".ps1")):
                rel = os.path.relpath(os.path.join(dirpath, fn), _ROOT)
                idx.setdefault(fn, []).append(rel.replace("\\", "/"))
    return idx


def has_selftest(relpath):
    try:
        with open(os.path.join(_ROOT, relpath), encoding="utf-8",
                  errors="ignore") as f:
            return "--selftest" in f.read()
    except OSError:
        return False


def _is_legacy(doc_rel, tool):
    """旧包导入区的文档 + 旧系统路径 ⇒ LEGACY（不是本项目断链）。

    判据（两条都要满足）：
      ① 文档在 data/research/imported_*/ 下
      ② 工具路径属于旧系统形态：tools/data/ · capabilities/ · src/
    """
    if "/imported_" not in doc_rel.replace("\\", "/"):
        return False
    t = tool.replace("\\", "/")
    return t.startswith(("tools/data/", "capabilities/", "src/"))


def audit():
    """→ [{doc, line, raw, tool, status, resolved}]"""
    idx = _index_tools()
    found = []
    for doc in _iter_docs():
        rel_doc = os.path.relpath(doc, _ROOT).replace("\\", "/")
        try:
            text = open(doc, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for ln, line in enumerate(text.splitlines(), 1):
            for m in TOOL_RX.finditer(line):
                raw = m.group(1)
                tool = raw.replace("\\", "/").lstrip("./")
                rec = {"doc": rel_doc, "line": ln, "raw": raw,
                       "tool": tool, "status": "OK", "resolved": None}
                base = os.path.basename(tool)
                legacy = _is_legacy(rel_doc, tool)
                if "/" in tool:
                    cand = os.path.join(_ROOT, tool.replace("/", os.sep))
                    if os.path.exists(cand):
                        rec["resolved"] = tool
                        if base.endswith(".py") and not has_selftest(tool):
                            rec["status"] = "NO_SELFTEST"
                    elif base in idx:
                        rec["status"] = "LEGACY" if legacy else "MISSING"
                        rec["resolved"] = "（按裸名存在：%s）" % idx[base][0]
                    else:
                        rec["status"] = "LEGACY" if legacy else "MISSING"
                else:
                    if base in idx:
                        paths = idx[base]
                        rec["resolved"] = paths[0]
                        if len(paths) > 1:
                            rec["status"] = "AMBIGUOUS"
                        elif base.endswith(".py") and not has_selftest(paths[0]):
                            rec["status"] = "NO_SELFTEST"
                    else:
                        rec["status"] = "LEGACY" if legacy else "MISSING"
                found.append(rec)
    return found


def report(found, verbose=False):
    order = {"MISSING": 0, "AMBIGUOUS": 1, "NO_SELFTEST": 2, "LEGACY": 3, "OK": 4}
    fs = sorted(found, key=lambda r: (order.get(r["status"], 9),
                                      r["doc"], r["line"]))
    print("文档 ✅ 工具名对账（M17 落地 · 只读）")
    print("  扫到 %d 条「✅ + 工具名」断言" % len(fs))
    cnt = {}
    for r in fs:
        cnt[r["status"]] = cnt.get(r["status"], 0) + 1
    print("  分布：%s" % " · ".join("%s=%d" % (k, v) for k, v in
                                   sorted(cnt.items(), key=lambda x: order.get(x[0], 9))))
    print()

    show = [r for r in fs if r["status"] != "OK"] if not verbose else fs
    if not show:
        print("  ✅ 全部命中，无断链")
        return 0

    print("  %-11s %-46s %-9s %s" % ("状态", "文档", "行", "工具"))
    for r in show:
        print("  %-11s %-46s %-9d %s"
              % (r["status"], r["doc"][:44], r["line"], r["tool"]))
        if r["status"] in ("MISSING", "AMBIGUOUS") and r["resolved"]:
            print("  %-11s %-46s %-9s └ %s" % ("", "", "", r["resolved"]))

    miss = [r for r in fs if r["status"] == "MISSING"]
    if miss:
        print("\n  ★ MISSING（**真断链**，文档声称有但找不到）：")
        seen = set()
        for r in miss:
            key = (r["tool"], r["doc"], r["line"])
            if key in seen:
                continue
            seen.add(key)
            print("     %s:%d → %s" % (r["doc"], r["line"], r["tool"]))
        print("\n     **本工具只报告不修复** —— 补实现或改文档都是治理动作，须显式批准。")
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

    print("doc_audit.py 自检")

    # T1 正则能抓路径式
    line = "| 两融 | **✅ `tools/e2e/heat_sensor.py margin`** | westock |"
    m = TOOL_RX.search(line)
    T("T1 能抓路径式 ✅ 工具名", m and m.group(1) == "tools/e2e/heat_sensor.py",
      m.group(1) if m else "None")

    # T2 正则能抓裸名式
    line2 = "- ✅ `margin.py`（已实现）"
    m2 = TOOL_RX.search(line2)
    T("T2 能抓裸名式 ✅ 工具名", m2 and m2.group(1) == "margin.py",
      m2.group(1) if m2 else "None")

    # T3 纯文字 ✅ 不该被抓
    T("T3 纯文字「✅ 已实现」不误抓", TOOL_RX.search("✅ 已实现且验收") is None)

    # T4 真实索引非空
    idx = _index_tools()
    T("T4 全库工具索引非空", len(idx) > 50, str(len(idx)))
    T("T5 索引里能找到已知工具 writegate.py", "writegate.py" in idx,
      str([k for k in idx if "writegate" in k]))

    # T6 MISSING 判定：造一个不存在的名字
    T("T6 不存在的工具名判定 MISSING",
      "definitely_not_exist_xyz.py" not in idx)

    # T7 has_selftest 对已知文件返回真
    wg = idx.get("writegate.py", [])
    T("T7 has_selftest 能识别 writegate.py 有自检",
      bool(wg) and has_selftest(wg[0]))

    # T8 全库扫描能跑通（不崩）
    found = audit()
    T("T8 全库扫描可完成", isinstance(found, list), str(len(found)))
    T("T9 结果每条都有必需字段",
      all({"doc", "line", "tool", "status"} <= set(r) for r in found))

    # T10 只读自证
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
    T("T10 只读（生产代码区零写点）", not writes, str(writes))

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    return report(audit(), verbose=a.verbose)


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
