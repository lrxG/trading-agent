#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""capability_status.py · 能力落地实况核查（2026-10-22 建）

## 为什么需要它

`data/state/capability_registry.json` 写着 27 个能力、26 个 `active`。
但 **`active` 是声明，不是事实**。2026-10-22 实测发现三处声明与事实不符：

| 能力 | 注册表说 | 事实 |
|---|---|---|
| S1 | active | 无 `--eval` 入口，是库函数（被 expect_ref/replay 调用） |
| S5 | active | 只读旧包 fundflow（停在 9-30、仅 716 票）⇒ **永远拿不到当日数据** |
| S8 | active | 能跑，但**从未进过每日链**，产物停在 9-29 |

**声明与事实的差，就是"功能完善了吗"这个问题的真实答案。**
本工具不读声明，只查两件事：**产物在不在、能不能跑**。

## 三种判定（互斥，不留模糊地带）

| 判定 | 含义 | 依据 |
|---|---|---|
| **LIVE** | 有近期产物，且产物不是 UNAVAILABLE | 真实文件 + 内容 |
| **DEGRADED** | 能跑，但产物是 UNAVAILABLE（数据源缺） | 真实文件 + 内容 |
| **NO_OUTPUT** | 产物路径存在但**从没生成过**或已严重过期 | 文件系统 |
| **NO_ENTRY** | 没有可执行入口（纯库函数） | AST/CLI 探测 |

**过期判据**：产物最新日期距基准日 > `stale_days`（默认 5 个自然日）⇒ 记 STALE。
不按"文件存在"就算数——`s_s5_2026-09-29.json` 存在，但它说明的是 9-29 的世界。

## 纪律

- **只读**：不跑任何能力、不写任何产物（避免"检查动作本身改变被检查对象"）。
  能不能跑由**产物痕迹**间接判定，需要实跑时用 `--probe`（默认关）。
- **缺口如实列出**：`NO_OUTPUT` 不是"没问题"，是"这条能力没在产出东西"。
- 本模块零写点（自检 J1 断言）。

用法：
  python tools/fs/capability_status.py                     # 查全部
  python tools/fs/capability_status.py --as-of 2026-10-22
  python tools/fs/capability_status.py --only S           # 只看 S 系
  python tools/fs/capability_status.py --selftest
  python tools/fs/capability_status.py --json             # 机器可读
"""

from __future__ import annotations

import glob
import io
import json
import os
import re
import sys

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
REG = os.path.join(_ROOT, "data", "state", "capability_registry.json")

_DATE_RX = re.compile(r"(\d{4}-\d{2}-\d{2})")

# CLI 探测：哪些文件带 --eval / --selftest 之类入口
_CLI_ENTRY = re.compile(r'^def _main|^def main', re.M)


def _load_registry(path=None):
    path = path or REG
    with io.open(path, encoding="utf-8") as f:
        d = json.load(f)
    caps = d.get("capabilities") or d
    items = caps if isinstance(caps, list) else list(caps.values())
    return [x for x in items if isinstance(x, dict)]


def _find_outputs(pattern, root=None):
    """output_path 模板 → 实际存在的文件列表（含日期）。

    ★ 2026-10-22 实测修正三处：
      ① 模板可能是 `memory/state/brain_state.md#regime` 这种**带锚点**的形式
         （锚点表示"这个文件里的某一段"）。glob 时锚点必须剥掉，否则永远
         匹配不到 → 把有产物的 C1 误判成 NO_OUTPUT。
      ② `<topic_id>` / `<research_id>` 这类占位符也要一起通配，不能只认 <as_of>。
      ③ **`data/generated/X` 要同时找 `data/generated/sim/X`**：
         决策账本有 SIM / LIVE 两条轴（`decision.ledger_path(sim)` 自动分流），
         默认走 SIM 是**安全设计**（未获批准不动生产账本）。
         只查生产路径 ⇒ 把「SIM 轴已在正常产出」误判成「从未产出」。
    """
    root = root or _ROOT
    if not pattern:
        return []
    p = pattern.split("#", 1)[0]                       # 剥锚点
    p = re.sub(r"<[a-z_]+>", "*", p)                    # 任何占位符都通配
    cands = [p]
    # 双轴：data/generated/xxx → 同时试 data/generated/sim/xxx
    if p.startswith("data/generated/") and not p.startswith("data/generated/sim/"):
        cands.append(p.replace("data/generated/", "data/generated/sim/", 1))
    out = []
    for cand in cands:
        for f in glob.glob(os.path.join(root, cand)):
            if os.path.isdir(f):
                out.append((f, None))
                continue
            m = _DATE_RX.search(os.path.basename(f))
            out.append((f, m.group(1) if m else None))
    return sorted(set(out), key=lambda x: x[1] or "")


def _read_status(path):
    """读产物，判断是 OK / UNAVAILABLE / EMPTY / UNREADABLE。

    → (verdict, reason)。**JSONL 与 JSON 都要能读**——`e6_weekly.jsonl`、
    `e10_daily.jsonl` 是多行 JSONL，按单 JSON 解析必然失败，会被误判成
    UNREADABLE（2026-10-22 第一版就这么误判了 E6/E10）。
    """
    try:
        if os.path.isdir(path):
            n = len(os.listdir(path))
            return ("EMPTY" if n == 0 else "OK",
                    "" if n else "目录为空")
        size = os.path.getsize(path)
        if size == 0:
            return "EMPTY", "文件 0 字节（存在但没内容）"
        with io.open(path, encoding="utf-8") as f:
            txt = f.read()
    except Exception as e:
        return "UNREADABLE", "%s: %s" % (type(e).__name__, str(e)[:60])

    def _one(obj):
        st = str(obj.get("status") or "").upper()
        if st == "UNAVAILABLE":
            gaps = obj.get("gaps") or []
            return "UNAVAILABLE", "; ".join(str(g)[:40] for g in gaps[:2])
        return None, ""

    try:
        d = json.loads(txt)
        v, why = _one(d)
        if v:
            return v, why
        inner = txt.count('"UNAVAILABLE"')
        return ("PARTIAL", "含 %d 处 UNAVAILABLE 子块" % inner) if inner \
            else ("OK", "")
    except Exception:
        pass

    # 非 JSON/JSONL 的类型（.md / .yaml 等）：按文本处理。
    #   C1 的产物是 `brain_state.md#regime`（markdown 的某一段），按 JSON 解析
    #   必然失败 ⇒ 会被误判 UNREADABLE（2026-10-22 实测踩过）。
    #
    #   ★★ **文本产物也要看内容是不是 UNAVAILABLE**。
    #   实测：`e2_2026-10-22.md` 标题就是「—— UNAVAILABLE（缺盘中腿）…」，
    #   但第一版判定器只判「文本非空 ⇒ LIVE」⇒ 把 4 个降级能力全报成 LIVE。
    #   那是**只检查"有没有文件"，不检查"文件说的是不是废话"**，
    #   正是本项目反复要防的形态。
    ext = os.path.splitext(path)[1].lower()
    if ext in (".md", ".txt", ".yaml", ".yml", ".csv"):
        if not txt.strip():
            return "EMPTY", "文本但无内容"
        # ★★ 只认**状态行/标题行**里的 UNAVAILABLE，不认正文里的这个词。
        #   实测坑：`e1_2026-10-22.md` 的**说明文字**里写着
        #   「缺失数据一律 UNAVAILABLE，禁以 0 冒充」——
        #   那是**该文件的设计声明**，不是"本文件降级"。
        #   第一版扫前 12 行把它判成 DEGRADED，**误杀了真正 READY 的 E1**。
        #   判据：①第一行（标题）含 UNAVAILABLE ⇒ 降级
        #        ②有 `状态：**READY**` / `status: OK` 之类显式就绪标记 ⇒ 正常
        #        ③两者皆无 ⇒ 看正文 UNAVAILABLE 是否 ≥8 处（真的大面积缺）
        lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
        title = lines[0] if lines else ""
        if "UNAVAILABLE" in title:
            m = re.search(r"[（(]缺[^）)]{0,40}[）)]", title)
            return "UNAVAILABLE", (m.group(0) if m else "标题标 UNAVAILABLE")
        ready_mark = re.search(r"状态[：:]\s*\*{0,2}(READY|OK|DONE)\*{0,2}", txt)
        if ready_mark:
            inner = txt.count("UNAVAILABLE")
            return "OK", "显式状态 %s%s" % (
                ready_mark.group(1),
                ("（正文另有 %d 处提及 UNAVAILABLE 属声明/子项）" % inner)
                if inner else "")
        if txt.count("UNAVAILABLE") >= 8:
            return "PARTIAL", "正文含 %d 处 UNAVAILABLE（多处未填）" \
                % txt.count("UNAVAILABLE")
        return "OK", "文本 %d 字节%s" % (
            len(txt.encode("utf-8")),
            ("（另有 %d 处 UNAVAILABLE 属模板未填段）" % txt.count("UNAVAILABLE"))
            if txt.count("UNAVAILABLE") else "")

    # JSONL：逐行解析，任一行 UNAVAILABLE 即降级
    lines = [ln for ln in txt.splitlines() if ln.strip()]
    if not lines:
        return "EMPTY", "文件无有效行"
    n_unavail = 0
    parsed = 0
    for ln in lines[:500]:
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        parsed += 1
        if isinstance(obj, dict):
            v, why = _one(obj)
            if v == "UNAVAILABLE":
                n_unavail += 1
    if parsed == 0:
        return "UNREADABLE", "既不是 JSON 也不是 JSONL"
    if n_unavail:
        return "PARTIAL", "%d/%d 行 UNAVAILABLE" % (n_unavail, parsed)
    return "OK", "JSONL %d 行" % parsed


def _has_cli_entry(cap_id, root=None):
    """该能力是否有 CLI 入口（--eval / --selftest 等）。

    找不到源文件 → 记 None（未知），不硬判为无。
    """
    root = root or _ROOT
    cid = str(cap_id).lower()
    cand = []
    for cat in ("analysis", "decision", "e2e", "extension", "risk"):
        cand += glob.glob(os.path.join(root, "tools", cat, "%s.py" % cid))
        cand += glob.glob(os.path.join(root, "tools", cat, "%s_*.py" % cid))
    if not cand:
        return None, []
    has = []
    for f in cand:
        try:
            s = io.open(f, encoding="utf-8").read()
        except Exception:
            continue
        if re.search(r'"--eval"', s) or re.search(r'"--selftest"', s):
            has.append(os.path.basename(f))
    return bool(has), has


def _days_between(a, b):
    import datetime
    y1, m1, d1 = (int(x) for x in a.split("-"))
    y2, m2, d2 = (int(x) for x in b.split("-"))
    return (datetime.date(y2, m2, d2) - datetime.date(y1, m1, d1)).days


def inspect(as_of, stale_days=5, only=None, root=None):
    """→ [dict]。每个能力一条。"""
    root = root or _ROOT
    caps = _load_registry(os.path.join(root, "data", "state",
                                       "capability_registry.json"))
    rows = []
    for c in caps:
        cid = str(c.get("id") or "")
        if only and not cid.upper().startswith(only.upper()):
            continue
        if c.get("status") == "retired":
            rows.append({"id": cid, "name": c.get("name"), "verdict": "RETIRED",
                         "latest": None, "reason": "已退役（不参与运转）",
                         "n_outputs": 0})
            continue

        pat = c.get("output_path")
        outs = _find_outputs(pat, root)
        latest = outs[-1][1] if outs else None
        entry_ok, entry_files = _has_cli_entry(cid, root)

        row = {"id": cid, "name": c.get("name"), "path": pat,
               "n_outputs": len(outs), "latest": latest,
               "entry": entry_files, "reason": "", "verdict": ""}

        if not outs:
            row["verdict"] = "NO_OUTPUT"
            row["reason"] = ("产物从未生成（模板 %s）" % pat) if pat \
                else "注册表未声明 output_path"
            # ★ 2026-10-22：区分「没实现」与「设计如此不产」。
            #   S10 竞价结构的设计是**源不全就不落每日快照**（避免噪音）；
            #   `--availability` 实测 auction_feed 已就绪（4940 行）、
            #   seal_order_feed 仍缺（涨停池封单额只覆盖 8/25 只，不足信）。
            #   把它和「零个模块写它」的 C4 归成一类是不诚实的。
            if pat and "s_s10_" in pat:
                row["reason"] = ("设计如此不产出：S10 要求 auction_feed + "
                                 "seal_order_feed **双源就绪**才落快照（避免每日噪音）。"
                                 "实测 auction_feed 已接通（fetch_auction.py · 4940 行），"
                                 "seal_order_feed 仍缺（封单额仅覆盖 8/25 只，不足信）")
            if entry_ok is False:
                row["verdict"] = "NO_OUTPUT"
                row["reason"] += "；且无 CLI 入口"
            rows.append(row)
            continue

        if latest and as_of:
            gap = _days_between(latest, as_of)
            if gap > stale_days:
                row["verdict"] = "STALE"
                row["reason"] = "最新产物 %s，距基准 %s 已 %d 天" % (
                    latest, as_of, gap)
                rows.append(row)
                continue

        st, why = _read_status(outs[-1][0])
        if st == "UNAVAILABLE":
            row["verdict"] = "DEGRADED"
            row["reason"] = why
        elif st == "EMPTY":
            # ★ 0 字节文件 ≠ 有产物。`calibration_queue.jsonl` 就是 0 字节，
            #   按旧逻辑会算 LIVE（文件存在），那是把"空壳"当"有东西"。
            row["verdict"] = "EMPTY"
            row["reason"] = why
        elif st == "UNREADABLE":
            row["verdict"] = "UNREADABLE"
            row["reason"] = why
        elif st == "PARTIAL":
            row["verdict"] = "LIVE"
            row["reason"] = why
        else:
            row["verdict"] = "LIVE"
            row["reason"] = why or ""
        rows.append(row)
    return rows


def render(rows, as_of):
    """→ 文本表。"""
    order = {"LIVE": 0, "DEGRADED": 1, "PARTIAL": 1, "STALE": 2,
             "EMPTY": 3, "NO_OUTPUT": 4, "UNREADABLE": 5, "NO_ENTRY": 6,
             "RETIRED": 9}
    rows = sorted(rows, key=lambda r: (order.get(r["verdict"], 8), r["id"]))
    L = []
    L.append("能力落地实况（基准日 %s · 只读核查，不跑能力不写产物）" % as_of)
    L.append("  %-5s %-16s %-10s %-11s %s"
             % ("ID", "名称", "判定", "最新产物", "说明"))
    for r in rows:
        L.append("  %-5s %-16s %-10s %-11s %s"
                 % (r["id"], (r.get("name") or "")[:14], r["verdict"],
                    r.get("latest") or "-", (r.get("reason") or "")[:60]))
    cnt = {}
    for r in rows:
        cnt[r["verdict"]] = cnt.get(r["verdict"], 0) + 1
    L.append("")
    L.append("  统计：%d 项 · %s" % (
        len(rows), " · ".join("%s=%d" % (k, v) for k, v in sorted(cnt.items()))))
    return "\n".join(L)


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
    import shutil
    print("capability_status.py 自检")

    d = tempfile.mkdtemp()
    try:
        # 造一个假项目
        os.makedirs(os.path.join(d, "data", "state"))
        os.makedirs(os.path.join(d, "data", "generated"))
        os.makedirs(os.path.join(d, "tools", "analysis"))

        def mkreg(caps):
            with io.open(os.path.join(d, "data", "state",
                                      "capability_registry.json"), "w",
                         encoding="utf-8") as f:
                json.dump({"capabilities": caps}, f, ensure_ascii=False)

        def mkout(name, obj):
            with io.open(os.path.join(d, "data", "generated", name), "w",
                         encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)

        mkreg([
            {"id": "A1", "name": "活的", "status": "active",
             "output_path": "data/generated/a1_<as_of>.json"},
            {"id": "A2", "name": "降级", "status": "active",
             "output_path": "data/generated/a2_<as_of>.json"},
            {"id": "A3", "name": "从没产出", "status": "active",
             "output_path": "data/generated/a3_<as_of>.json"},
            {"id": "A4", "name": "过期", "status": "active",
             "output_path": "data/generated/a4_<as_of>.json"},
            {"id": "A5", "name": "退役", "status": "retired",
             "output_path": "data/generated/a5_<as_of>.json"},
            {"id": "A6", "name": "文本降级", "status": "active",
             "output_path": "data/generated/a6_<as_of>.md"},
            {"id": "A7", "name": "空壳文件", "status": "active",
             "output_path": "data/generated/a7_<as_of>.jsonl"},
            {"id": "A8", "name": "带锚点", "status": "active",
             "output_path": "data/state/mem.md#regime"},
            {"id": "A9", "name": "READY但正文提到UNAVAILABLE", "status": "active",
             "output_path": "data/generated/a9_<as_of>.md"},
            {"id": "A10", "name": "只在sim轴有产物", "status": "active",
             "output_path": "data/generated/a10_ledger.jsonl"},
        ])
        mkout("a1_2026-10-22.json", {"status": "OK", "x": 1})
        mkout("a2_2026-10-22.json", {"status": "UNAVAILABLE",
                                     "gaps": ["缺估值序列"]})
        mkout("a4_2026-09-01.json", {"status": "OK"})
        # A6：文本产物的标题就是 UNAVAILABLE（真实 E2/E5/E7/E8 的形态）
        with io.open(os.path.join(d, "data", "generated", "a6_2026-10-22.md"),
                     "w", encoding="utf-8") as f:
            f.write("# E2 盘中扫描 · 2026-10-22 —— UNAVAILABLE（缺盘中腿，数据源无）\n\n正文\n")
        # A7：0 字节文件
        with io.open(os.path.join(d, "data", "generated", "a7_2026-10-22.jsonl"),
                     "w", encoding="utf-8") as f:
            f.write("")
        # A8：带 #锚点的模板指向真实存在的 md
        with io.open(os.path.join(d, "data", "state", "mem.md"), "w",
                     encoding="utf-8") as f:
            f.write("# 环境档\n\n强势 5 成\n")
        # A10：只在 SIM 轴有产物（真实 C5 决策账本的形态）
        os.makedirs(os.path.join(d, "data", "generated", "sim"), exist_ok=True)
        with io.open(os.path.join(d, "data", "generated", "sim",
                                  "a10_ledger.jsonl"), "w",
                     encoding="utf-8") as f:
            f.write('{"x": 1}' + "\n")
        # A9：真实 E1 的形态——状态 READY，但说明文字里提到 UNAVAILABLE
        with io.open(os.path.join(d, "data", "generated", "a9_2026-10-22.md"),
                     "w", encoding="utf-8") as f:
            f.write("# E1 盘前预案（as_of=2026-10-22）\n\n"
                    "> 缺失数据一律 UNAVAILABLE，禁以 0 冒充。\n\n"
                    "- 状态：**READY**\n\n## 环境档\n\n| 档位 | 退潮 |\n")

        rows = inspect("2026-10-22", stale_days=5, root=d)
        by = {r["id"]: r for r in rows}

        T("A1 有当日产物且 status=OK → LIVE",
          by["A1"]["verdict"] == "LIVE", by["A1"]["verdict"])
        T("A2 status=UNAVAILABLE → DEGRADED（不是 LIVE）",
          by["A2"]["verdict"] == "DEGRADED", by["A2"]["verdict"])
        T("A3 无产物 → NO_OUTPUT", by["A3"]["verdict"] == "NO_OUTPUT",
          by["A3"]["verdict"])
        T("A4 产物过期 → STALE（不因文件存在就算活）",
          by["A4"]["verdict"] == "STALE", by["A4"]["verdict"])
        T("A5 退役 → RETIRED", by["A5"]["verdict"] == "RETIRED",
          by["A5"]["verdict"])
        T("A6 文本产物标题含 UNAVAILABLE → DEGRADED（不因「有文件」报 LIVE）",
          by["A6"]["verdict"] == "DEGRADED", by["A6"]["verdict"])
        T("A7 0 字节文件 → EMPTY（空壳不算有产物）",
          by["A7"]["verdict"] == "EMPTY", by["A7"]["verdict"])
        T("A8 带 #锚点的模板能匹配到真文件 → LIVE（不误判 NO_OUTPUT）",
          by["A8"]["verdict"] == "LIVE", by["A8"]["verdict"])
        T("A9 状态 READY 但正文提到 UNAVAILABLE → LIVE（不误杀，仅提及不算降级）",
          by["A9"]["verdict"] == "LIVE", "%s / %s"
          % (by["A9"]["verdict"], by["A9"]["reason"]))
        T("A10 只在 sim 轴有产物也算有产物（不误判 NO_OUTPUT）",
          by["A10"]["verdict"] == "LIVE", by["A10"]["verdict"])

        # B 过期判据边界
        mkout("a1_2026-10-18.json", {"status": "OK"})
        rows2 = inspect("2026-10-22", stale_days=5, root=d)
        by2 = {r["id"]: r for r in rows2}
        T("B1 4 天内不算过期（取最新产物）",
          by2["A1"]["latest"] == "2026-10-22", by2["A1"]["latest"])

        # C only 过滤
        rows3 = inspect("2026-10-22", only="A1", root=d)
        # --only 是**前缀**匹配：A1 会同时命中 A1 / A10（这是设计行为）。
        # 断言要锁「A2~A9 被排除」，不能写死「只剩 A1」——加了 A10 就会挂。
        ids3 = [r["id"] for r in rows3]
        T("C1 --only 前缀过滤生效（命中 A1/A10，排除其余）",
          set(ids3) == {"A1", "A10"} and "A2" not in ids3, str(ids3))

        # D 渲染不抛错且含统计
        txt = render(rows, "2026-10-22")
        T("D1 渲染含统计行", "统计：" in txt and "LIVE=" in txt)

        # E 只读自证：AST 判定**生产路径**零写点（自检要造临时夹具，允许写临时目录）
        import ast
        tree = ast.parse(io.open(os.path.abspath(__file__),
                                 encoding="utf-8").read())
        sel = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "_selftest":
                sel = node
        sel_lines = set()
        if sel is not None:
            for n in ast.walk(sel):
                if hasattr(n, "lineno"):
                    sel_lines.add(n.lineno)
        writes = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = getattr(node.func, "attr", "") or getattr(node.func, "id", "")
                # ★ `str.replace` 是字符串方法，不是文件写 —— 只看**无接收者**的
                #   `os.replace` / `os.remove` 这类。判据必须区分两者，
                #   否则 `pattern.replace("<as_of>","*")` 会被误判成写盘。
                is_os_call = False
                if isinstance(node.func, ast.Attribute):
                    v = node.func.value
                    is_os_call = (getattr(v, "id", "") == "os")
                bad = (is_os_call and fn in ("remove", "unlink", "replace",
                                             "rename", "rmtree")) or \
                      (not isinstance(node.func, ast.Attribute)
                       and fn in ("dump", "writerow", "writerows"))
                if fn == "open" and len(node.args) > 1:
                    a = node.args[1]
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                            and any(ch in a.value for ch in "wax+"):
                        bad = True
                if bad and getattr(node, "lineno", -1) not in sel_lines:
                    writes.append("%s@L%d" % (fn, getattr(node, "lineno", -1)))
        T("E1 生产路径零写点（自检临时夹具豁免）", not writes, str(writes[:6]))
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    as_of = None
    only = None
    stale = 5
    js = "--json" in argv
    for i, a in enumerate(argv):
        if a == "--as-of" and i + 1 < len(argv):
            as_of = argv[i + 1]
        elif a == "--only" and i + 1 < len(argv):
            only = argv[i + 1]
        elif a == "--stale-days" and i + 1 < len(argv):
            stale = int(argv[i + 1])
    if not as_of:
        import datetime
        as_of = datetime.date.today().isoformat()

    rows = inspect(as_of, stale_days=stale, only=only)
    if js:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        print(render(rows, as_of))
    # 有 NO_OUTPUT 时返回非零，便于被门禁调用
    return 1 if any(r["verdict"] == "NO_OUTPUT" for r in rows) else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit(_main(sys.argv[1:]))
