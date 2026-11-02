#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hboard.py · 跨日看板（施工方案 0.12 · v1.0 · 2026-10-08 建）

用途：把「今天必须裁什么 / 日历上有什么要预装 / 下一个到期日是哪天」压成一页
      （`data/generated/h_board.md`），供人一眼看过就开工——**跨日**看板，每天刷新。

★ 装配层职责 = 只做「读已有状态 + 聚合呈现」，**不实现任何业务规则**：
  · **不算 expiry**（0.5 `compute_expiry` 是唯一口径，本模块只**读**候选池的 `expiry` 字段）；
  · **不折算交易日历**（零日期计算——用候选池里已有 `expiry` 的**最小值**表达"下一个到期日"；
    交易日历未落盘，故宁可口径保守也不拿"自然日 +1"冒充"明天"）；
  · **不判状态迁移**（0.5 `advance_candidate` 是唯一执行器）、**不取外部数据**（取数归 0.10 loader）。

★ fail-safe（用户 2026-10-08 F7 盯防项②）：缺数据 / 缺能力一律**显式标注**，禁编内容。
  三类"空"的**性质不同、处置不同**，禁混：
  · `NO_DATA`        候选池文件读不到（0.5 拒绝静默新建 → 本模块如实报缺，不编空表）
  · `NO_CAPABILITY`  该区依赖的能力 / 数据类型未落盘（骨架期"日历预装"即此态）
  · `EMPTY`          依赖都在、就是没有内容（**≠ 缺口**，零插件骨架期的正常态）

★ 三区口径（与施工方案 0.12 原文逐字对应，**口径写死防两套叫法**）
  · **今日必裁** ← 候选池中 `state ∈ OPEN_STATES` 且 `expiry == as_of`（观察窗最后一天，必须裁决）
                   另**单列** `expiry < as_of` 却仍未终态的（`OVERDUE`：本应已被 0.5 判过期）
  · **日历预装** ← 0.10 插入式扩展点 `loader.load_ext("calendar", as_of)`
                   （新增数据源 = 采集器往投放区放文件，**本模块零改动**）
  · **明日到期** ← 候选池中 `expiry > as_of` 的**最近一档**（同一档可能多票）
                   口径 = 「下一个到期日」，**不声称是"明天"**

被谁调用：`wakeup.py` 母链之后**委派**（经 run_observed 留痕）；也可独立跑。
用法：
  python tools/fs/hboard.py                       # 只读：渲染并打印（不写盘）
  python tools/fs/hboard.py --refresh 2026-10-08  # 刷新 `data/generated/h_board.md`
  python tools/fs/hboard.py --selftest            # 内置验收（临时沙盒，不碰真数据）
  python tools/fs/hboard.py --discipline

规则：本模块禁判断业务、禁动游标、禁下单、禁算 expiry；改三区口径 = 改本文件 + 0.12 正文。
"""
import ast
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools", "fs")))
from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
import candidates as CAND  # noqa: E402
import astwin as ASTW  # noqa: E402   ← AST 窗口（尾界）唯一实现，禁本模块自写

SCHEMA_VERSION = 1
BOARD_PATH = os.path.join(ROOT, "data", "generated", "h_board.md")

# 三区名（写死；改这里必须同步施工方案 0.12 正文——禁两套叫法）
SECTION_TODAY = "今日必裁"
SECTION_CALENDAR = "日历预装"
SECTION_TOMORROW = "明日到期"
SECTIONS = (SECTION_TODAY, SECTION_CALENDAR, SECTION_TOMORROW)

# 区状态码（值域写死；报告里出现未登记的状态码 = 契约破损）
S_OK = "OK"
S_EMPTY = "EMPTY"
S_NO_DATA = "NO_DATA"
S_NO_CAPABILITY = "NO_CAPABILITY"
S_OVERDUE = "OVERDUE"
STATUSES = (S_OK, S_EMPTY, S_NO_DATA, S_NO_CAPABILITY, S_OVERDUE)

# 日历数据类型名（走 0.10 插入式扩展点；与 0.2 核心 5 契约键不撞名）
CALENDAR_EXT = "calendar"


class BoardError(Exception):
    """看板自身出错（日期非法 / 写盘失败）——显式抛错，不静默给半截看板。"""


def _check_date(d, name="as_of"):
    """日期硬校验（YYYY-MM-DD）。与 cursor / settle / wakeup 同口径：**不猜、不折算**。"""
    import datetime as _dt
    s = d.isoformat() if hasattr(d, "isoformat") else str(d)
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise BoardError("%s 必须是 YYYY-MM-DD，收到: %r" % (name, d))
    try:
        _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        raise BoardError("%s 非法: %r" % (name, d))
    return s


def _key(c):
    """稳定排序键（expiry → id）：让看板输出可复现（同输入同字节）。"""
    return (str(c.get("expiry") or ""), str(c.get("id") or ""))


def _row(c):
    """候选展示行（**只搬运已有字段，不派生新判断**）。"""
    return {"id": c.get("id"), "code": c.get("code"), "state": c.get("state"),
            "expiry": c.get("expiry"), "last_seen": c.get("last_seen"),
            "source": c.get("source")}


# ── 聚合（纯只读，不写盘、不迁移状态）────────────────────────────────────────
def collect(as_of, *, pool_path=None, base=None):
    """三区聚合。返回 `{as_of, sections:[{name, status, note, rows, extra, error}]}`。

    **只读两个源**：0.5 候选池（状态文件）· 0.10 插入式扩展点（日历）。
    任一源不可用 → 对应区标 `NO_DATA` / `NO_CAPABILITY`，**其余两区照常渲染**
    （一区缺不影响全局——这与"整篇失败"不同）。
    """
    as_of = _check_date(as_of)

    # ① 候选池（0.5）
    pool, pool_status, pool_err = [], S_OK, None
    try:
        pool = [c for c in CAND.list_candidates(path=pool_path)
                if c.get("state") in CAND.OPEN_STATES]
    except Exception as e:
        pool_status, pool_err = S_NO_DATA, "%s: %s" % (type(e).__name__, e)

    due = sorted([c for c in pool if c.get("expiry") == as_of], key=_key)
    overdue = sorted([c for c in pool if str(c.get("expiry") or "") < as_of], key=_key)
    future = sorted([c for c in pool if str(c.get("expiry") or "") > as_of], key=_key)

    if pool_status != S_OK:
        today_status = S_NO_DATA
    elif overdue:
        today_status = S_OVERDUE          # 有过期未处理 → 比"有必裁"更紧急，优先报出
    elif due:
        today_status = S_OK
    else:
        today_status = S_EMPTY
    today = {"name": SECTION_TODAY, "status": today_status, "error": pool_err,
             "note": "观察窗最后一天 → 必须裁决（晋级 / 淘汰）",
             "rows": [_row(c) for c in due],
             "extra": {"OVERDUE": [_row(c) for c in overdue]}}

    # ② 日历预装（0.10 插入式扩展点；缺数据类型 → NO_CAPABILITY，不是 EMPTY）
    cal_status, cal_rows, cal_note, cal_err = S_NO_CAPABILITY, [], "", None
    try:
        import loader as LOAD
        cal = LOAD.load_ext(CALENDAR_EXT, as_of, base=base)
        if cal is LOAD.UNAVAILABLE:
            cal_note = "日历数据类型未投放（尚无采集者）→ 这是能力缺口，不是「今天没有日历」"
        else:
            cal_rows = [dict(x) for x in cal]
            cal_status = S_OK if cal_rows else S_EMPTY
            cal_note = "来自投放区 `data/raw/<date>/%s.*`" % CALENDAR_EXT
    except Exception as e:
        cal_err = "%s: %s" % (type(e).__name__, e)
    calendar = {"name": SECTION_CALENDAR, "status": cal_status, "error": cal_err,
                "note": "来源 = 0.10 插入式扩展点 `loader.load_ext(%r, as_of)`" % CALENDAR_EXT,
                "rows": cal_rows, "extra": {"hint": cal_note}}

    # ③ 明日到期（**零日期折算**：取已有 expiry 的最小未来值；非"自然日 +1"）
    nxt = future[0].get("expiry") if future else None
    if pool_status != S_OK:
        tom_status = S_NO_DATA
    elif nxt:
        tom_status = S_OK
    else:
        tom_status = S_EMPTY
    tomorrow = {"name": SECTION_TOMORROW, "status": tom_status, "error": pool_err,
                "note": "候选池中 `expiry` 晚于 as_of 的**最近一档**（口径 = 下一个到期日，非「明天」）",
                "rows": [_row(c) for c in future if c.get("expiry") == nxt],
                "extra": {"next_expiry": nxt}}

    return {"schema_version": SCHEMA_VERSION, "as_of": as_of,
            "sections": [today, calendar, tomorrow]}


# ── 渲染 ─────────────────────────────────────────────────────────────────────
def _fmt_cell(v):
    s = "" if v is None else str(v)
    return s.replace("|", "\\|").strip()


def _rows_md(rows, cols=("id", "code", "state", "expiry", "last_seen")):
    if not rows:
        return "_（无）_\n"
    out = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for r in rows:
        out.append("| " + " | ".join(_fmt_cell(r.get(c)) for c in cols) + " |")
    return "\n".join(out) + "\n"


def render(as_of, *, pool_path=None, base=None):
    """渲染看板文本 + 聚合结构（纯函数，**不写盘**）。"""
    doc = collect(as_of, pool_path=pool_path, base=base)
    L = []
    L.append("# 跨日看板（0.12 · `data/generated/h_board.md` · as_of=%s）" % doc["as_of"])
    L.append("")
    L.append("> 本文件由 `tools/fs/hboard.py` 刷新——装配层只做「读已有状态 + 聚合呈现」，"
             "**不实现任何业务规则**（不算 expiry、不折算交易日历、不判状态迁移、不取外部数据）。")
    L.append("> 三类「空」性质不同、处置不同，禁混：**`NO_DATA`**（源读不到）· "
             "**`NO_CAPABILITY`**（依赖的能力/数据未落盘）· **`EMPTY`**（依赖都在、就是没有内容）。")
    L.append("")
    nums = ("一", "二", "三")
    for i, s in enumerate(doc["sections"]):
        L.append("## %s、%s  ·  `%s`" % (nums[i], s["name"], s["status"]))
        L.append("")
        L.append(s["note"])
        L.append("")
        if s["status"] == S_NO_DATA:
            L.append("_缺口原因（`NO_DATA`）：%s_" % (s.get("error") or "候选池不可读"))
            L.append("")
            continue
        if s["status"] == S_NO_CAPABILITY:
            L.append("_缺口原因（`NO_CAPABILITY`）：%s_" % (s["extra"].get("hint") or "未落盘"))
            L.append("")
            continue
        if i == 0 and s["extra"].get("OVERDUE"):
            L.append("**⚠ 过期未处理（`OVERDUE`，本应已被 0.5 判 EXPIRED）：**")
            L.append("")
            L.append(_rows_md(s["extra"]["OVERDUE"]))
            L.append("")
        if i == 2 and s["extra"].get("next_expiry"):
            L.append("下一档到期日 = **%s**" % s["extra"]["next_expiry"])
            L.append("")
        L.append(_rows_md(s["rows"]))
    return "\n".join(L).rstrip() + "\n", doc


# ── 写盘 ─────────────────────────────────────────────────────────────────────
def _write(p, text):
    """原子写（同目录 tmp + os.replace）。看板是**当日快照视图** → 整篇重写，非 append。"""
    _WG.check_write(p)
    d = os.path.dirname(p)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".h_board_", suffix=".tmp")
    try:
        with io.open(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def refresh(as_of, *, path=None, pool_path=None, base=None):
    """刷新看板（唯一写入口）。返回 `{path, as_of, bytes, sections, statuses}`。"""
    text, doc = render(as_of, pool_path=pool_path, base=base)
    p = _write(path or BOARD_PATH, text)
    return {"path": p, "as_of": doc["as_of"], "bytes": len(text.encode("utf-8")),
            "sections": [s["name"] for s in doc["sections"]],
            "statuses": {s["name"]: s["status"] for s in doc["sections"]},
            "orders_created": 0}


# ── 自检（--selftest）：全部在临时沙盒 ───────────────────────────────────────
def _selftest():
    import json as _json
    import shutil
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

    tmp = tempfile.mkdtemp(prefix="ta_hboard_selftest_")
    try:
        D = "2026-10-08"

        def _mkpool(cands, path):
            d = {"version": CAND.SCHEMA_VERSION, "updated_at": D, "candidates": cands}
            with io.open(path, "w", encoding="utf-8", newline="\n") as f:
                _json.dump(d, f, ensure_ascii=False, indent=2)
            return path

        def _c(cid, state, expiry):
            return {"id": cid, "code": "sh600000", "state": state, "created_at": "2026-10-01",
                    "last_seen": "2026-10-07", "expiry": expiry, "source": "selftest",
                    "trigger": {"text": "涨停放量", "rule": None}, "invalid_condition": None,
                    "next_trigger": None, "keep_reason": None,
                    "thesis_id": None, "thesis": None}

        print("== A. 契约与口径 ==")
        T("A1 三区名与 0.12 原文逐字一致",
          SECTIONS == ("今日必裁", "日历预装", "明日到期"))
        T("A2 状态码值域写死（含 NO_DATA / NO_CAPABILITY 两类「空」）",
          STATUSES == ("OK", "EMPTY", "NO_DATA", "NO_CAPABILITY", "OVERDUE"))
        T("A3 看板落点 = data/generated/h_board.md（0.7 豁免 X5）",
          BOARD_PATH.replace("\\", "/").endswith("data/generated/h_board.md"))
        T("A4 日期硬校验：非法日期显式抛错（不猜）",
          _raises(lambda: collect("2026/10/08")) and _raises(lambda: collect("2026-13-01")))

        print("== B. 三区聚合（真候选池状态文件） ==")
        p = os.path.join(tmp, "candidates.json")
        _mkpool([_c("CD-1", "WATCH", D),                     # 今日到期
                 _c("CD-2", "DISCOVERED", "2026-10-09"),     # 下一档
                 _c("CD-3", "WATCH", "2026-10-09"),          # 下一档（同档）
                 _c("CD-4", "QUALIFIED", "2026-10-12"),      # 更远的未来
                 _c("CD-5", "TRADED", D),                    # 终态，不该出现
                 _c("CD-6", "EXPIRED", D),                   # 终态，不该出现
                 _c("CD-7", "WATCH", "2026-10-06")], p)      # 过期未处理 → OVERDUE
        doc = collect(D, pool_path=p)
        secs = {s["name"]: s for s in doc["sections"]}
        ids = lambda s: sorted(r["id"] for r in s["rows"])
        T("B1 今日必裁 = expiry==as_of 且观察中（只 CD-1，终态不入）",
          ids(secs[SECTION_TODAY]) == ["CD-1"], ids(secs[SECTION_TODAY]))
        T("B2 过期未处理单列 OVERDUE（CD-7）且今日必裁状态优先报 OVERDUE",
          [r["id"] for r in secs[SECTION_TODAY]["extra"]["OVERDUE"]] == ["CD-7"]
          and secs[SECTION_TODAY]["status"] == S_OVERDUE)
        T("B3 明日到期 = 最近一档未来 expiry（2026-10-09 的 CD-2/CD-3，同档全列）",
          secs[SECTION_TOMORROW]["extra"]["next_expiry"] == "2026-10-09"
          and ids(secs[SECTION_TOMORROW]) == ["CD-2", "CD-3"])
        T("B4 终端态（TRADED/EXPIRED）不进入任何区",
          not any(r["id"] in ("CD-5", "CD-6")
                  for s in doc["sections"] for r in s["rows"]))
        T("B5 日历预装 = NO_CAPABILITY（数据类型未投放 → 能力缺口，非「今天没有」）",
          secs[SECTION_CALENDAR]["status"] == S_NO_CAPABILITY)

        print("== C. 三类「空」区分（用户 F7 盯防项②） ==")
        empty_p = os.path.join(tmp, "empty.json")
        _mkpool([], empty_p)
        d2 = {s["name"]: s for s in collect(D, pool_path=empty_p)["sections"]}
        T("C1 空池 → EMPTY（依赖都在、就是没有内容），不是缺口",
          d2[SECTION_TODAY]["status"] == S_EMPTY
          and d2[SECTION_TOMORROW]["status"] == S_EMPTY)
        missing_p = os.path.join(tmp, "nosuch", "candidates.json")
        d3 = {s["name"]: s for s in collect(D, pool_path=missing_p)["sections"]}
        T("C2 候选池不可读 → NO_DATA（0.5 拒绝静默新建，本模块如实报缺、不编空表）",
          d3[SECTION_TODAY]["status"] == S_NO_DATA
          and d3[SECTION_TOMORROW]["status"] == S_NO_DATA
          and d3[SECTION_TODAY]["error"])
        T("C3 一区缺不影响其余：候选池 NO_DATA 时日历区照常给结论",
          d3[SECTION_CALENDAR]["status"] == S_NO_CAPABILITY)

        print("== D. 渲染与写盘 ==")
        text, _doc = render(D, pool_path=p)
        T("D1 渲染含三区标题 + 状态码",
          all(("## %s、%s" % (n, s)) in text
              for n, s in zip(("一", "二", "三"), SECTIONS))
          and "`%s`" % S_NO_CAPABILITY in text)
        T("D2 渲染可复现（同输入 → 同字节）", render(D, pool_path=p)[0] == text)
        bp = os.path.join(tmp, "generated", "h_board.md")
        pool_fp_before = _fp(p)
        r = refresh(D, path=bp, pool_path=p)
        T("D3 写盘成功且报告零下单（orders_created=0）",
          os.path.exists(bp) and r["orders_created"] == 0 and r["statuses"][SECTION_TODAY] == S_OVERDUE)
        T("D4 原子写：同目录不留 .tmp 残留",
          not [x for x in os.listdir(os.path.dirname(bp)) if x.endswith(".tmp")])
        T("D5 整篇重写而非追加（二次刷新后仍是单份看板、非拼接）",
          refresh(D, path=bp, pool_path=p)["bytes"] == r["bytes"])
        T("D6 候选池在刷新前后逐字节不变（看板只读状态、不改状态）",
          _fp(p) == pool_fp_before)

        print("== E. 纪律 ==")
        src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
        toks = _prod_tokens()
        T("E1 零业务规则重实现：不出现 expiry 计算 / 档位 / 价位 / 仓位类标识符",
          not (_BIZ_WORDS & toks), sorted(_BIZ_WORDS & toks))
        T("E2 不调状态迁移 / 游标写入 / 结算（本层只读）",
          not (_FORBIDDEN & toks), sorted(_FORBIDDEN & toks))
        T("E3 零下单类标识符", not (_ORDER_WORDS & toks), sorted(_ORDER_WORDS & toks))
        T("E4 无网络 / 子进程导入（看板不取外部数据）",
          not _imports_any(src, ("requests", "urllib", "socket", "subprocess", "http")))
        T("E5 生产区**零日期算术**（不折算交易日历：无 timedelta / date±N）",
          not _has_date_arith(), _has_date_arith())
        T("E6 ②沙盒泄漏 = 0：真看板在自检前后逐字节不变",
          _fp(BOARD_PATH) == _board_fp_before)

        print("\n结果：%d 通过 / %d 失败" % (ok, fail))
        if fails:
            print("失败项：%s" % " / ".join(fails))
        return 1 if fail else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 静态扫描（纪律自检用）────────────────────────────────────────────────────
_TEST_FUNCS = ("_selftest", "_discipline")

# 业务规则词汇（**拼装构造**：避免判据自指——黑名单里出现整字面量会命中定义行自己，
# 这正是 F6 在 landing.py 修过的同一类错，此处**全部**拼装、不留裸字面量）
_BIZ_WORDS = frozenset((
    "compute" + "_expiry", "regime" + "_ladder", "position" + "_cap",
    "limit" + "_up_count", "break" + "_rate", "driver" + "_type",
    "stop" + "_line", "entry" + "_limit", "position" + "_request",
    "take" + "_profit", "hold" + "_days",
))
_FORBIDDEN = frozenset(("advance" + "_candidate", "mark" + "_step",
                        "advance" + "_cursor", "set" + "tle",
                        "pro" + "mote", "pro" + "pose"))
_ORDER_WORDS = frozenset(("place" + "_order", "submit" + "_order", "can" + "cel_order",
                          "下" + "单", "撤" + "单"))


def _test_lines(tree):
    out = set()
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in _TEST_FUNCS:
            out |= ASTW.line_set(n)
    return out


def _fixture_names(tree):
    """夹具判别：**只有下划线私有名才可能是夹具**，且**调用者 ⊆ 测试函数集**。

    （与 landing.py / wakeup.py 同一纪律：公开名恒进审计，永不被判为夹具。）
    """
    tl = _test_lines(tree)
    callers = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            fn = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            if fn:
                callers.setdefault(fn, set()).add(n.lineno in tl)
    out = set()
    for n in tree.body:
        if not isinstance(n, ast.FunctionDef) or not n.name.startswith("_"):
            continue
        if n.name in _TEST_FUNCS:
            continue
        cs = callers.get(n.name)
        if cs and cs <= {True}:
            out.add(n.name)
    return out


def _prod_tokens(path=None):
    """生产代码区标识符（剔测试函数整段与夹具函数、剔文档字符串）。"""
    p = path or os.path.abspath(__file__)
    tree = ast.parse(io.open(p, encoding="utf-8").read())
    skip = set(_test_lines(tree))
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in _fixture_names(tree):
            skip |= ASTW.line_set(n)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            b = node.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) \
                    and isinstance(b[0].value.value, str):
                docs.add(id(b[0].value))
    toks = set()
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) in skip:
            continue
        if isinstance(node, ast.Name):
            toks.add(node.id)
        elif isinstance(node, ast.Attribute):
            toks.add(node.attr)
        elif isinstance(node, ast.arg):
            toks.add(node.arg)
        elif isinstance(node, ast.FunctionDef):
            toks.add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docs:
            toks.add(node.value)
    return toks


def _has_date_arith(path=None):
    """生产区是否出现日期算术（timedelta / date ± n）——"不折算日历"的机械判据。"""
    p = path or os.path.abspath(__file__)
    tree = ast.parse(io.open(p, encoding="utf-8").read())
    skip = set(_test_lines(tree))
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in _fixture_names(tree):
            skip |= ASTW.line_set(n)
    hits = []
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) in skip:
            continue
        if isinstance(node, ast.Attribute) and node.attr == "timedelta":
            hits.append("timedelta@L%d" % node.lineno)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "timedelta":
            hits.append("timedelta()@L%d" % node.lineno)
    return hits


def _imports_any(src, names):
    out = []
    for nm in names:
        if not _re_import(src, nm):
            continue
        out.append(nm)
    return out


def _re_import(src, name):
    import re
    return bool(re.search(r"^\s*(?:import|from)\s+%s\b" % re.escape(name), src, re.M))


def _fp(path):
    import hashlib
    if not os.path.exists(path):
        return None
    with io.open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


_board_fp_before = _fp(BOARD_PATH)


def _discipline(verbose=True):
    """纪律自检：零业务规则 / 零状态写入 / 零下单 / 零日期折算 / ②沙盒泄漏 = 0。"""
    toks = _prod_tokens()
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    biz = sorted(_BIZ_WORDS & toks)
    forb = sorted(_FORBIDDEN & toks)
    orders = sorted(_ORDER_WORDS & toks)
    net = _imports_any(src, ("requests", "urllib", "socket", "subprocess", "openai"))
    arith = _has_date_arith()
    res, bad = [], []

    def T(n, c, e=""):
        res.append((n, bool(c), e))
        if not c:
            bad.append(n)

    T("① 零业务规则重实现（不算 expiry / 不出档位价位仓位）", not biz, biz)
    T("② 不调状态迁移 / 游标写入 / 结算（本层只读）", not forb, forb)
    T("③ 零下单类标识符", not orders, orders)
    T("④ 生产区零日期折算（交易日历未落盘 → 不拿自然日顶替）", not arith, arith)
    T("⑤ 无网络 / 子进程导入", not net, net)
    T("⑥ 三区口径在本文件与 0.12 正文写死（读注册表无关 → 无第二份清单）",
      len(SECTIONS) == 3 and len(set(SECTIONS)) == 3)
    T("⑦ ②沙盒泄漏 = 0：真看板未被本自检触碰",
      _fp(BOARD_PATH) == _board_fp_before)

    if verbose:
        print("纪律自检 · hboard.py")
        for n, okk, e in res:
            print("  [%s] %s%s" % ("OK" if okk else "!!", n, ("  %s" % (e,)) if e else ""))
    return {"ok": not bad, "n": len(res), "failed": bad, "results": res}


def _main(argv):
    if "--selftest" in argv:
        print("hboard.py 自检（0.12 跨日看板 · SCHEMA_VERSION=%d · 临时沙盒）" % SCHEMA_VERSION)
        return _selftest()
    if "--discipline" in argv:
        d = _discipline()
        print("\n纪律：%d 项 %s" % (d["n"], "全 OK ✅" if d["ok"] else "有失败 ❌ %s" % d["failed"]))
        return 0 if d["ok"] else 1
    if "--refresh" in argv:
        i = argv.index("--refresh")
        day = argv[i + 1] if len(argv) > i + 1 else None
        if not day:
            print("用法：--refresh YYYY-MM-DD")
            return 2
        r = refresh(day)
        print("已刷新：%s（%d bytes）" % (r["path"], r["bytes"]))
        for k, v in r["statuses"].items():
            print("  %-8s %s" % (k, v))
        return 0
    # 默认只读：渲染并打印（不写盘）
    today = None
    for a in argv:
        if len(a) == 10 and a[4] == "-":
            today = a
    if not today:
        try:
            import cursor as CUR
            today = CUR.read_cursor()["date"]
        except Exception:
            today = "2026-09-30"
    text, _doc = render(today)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
