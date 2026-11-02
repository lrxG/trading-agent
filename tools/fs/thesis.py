#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""thesis.py · Thesis 认知对象与级联门控（施工方案 4.2 · v1.0 · 2026-10-08 建）

用途：Thesis（认知主线）的**机械部分**——状态汇流 + 级联门控 + 下一跳选择。
      **不做超级中心模块**：判断（"我认为现在发生什么"）在 LLM，本模块只管
      状态载体的读写、append-only 约束、以及「能不能唤起下一跳」的门控裁决。

状态落点（唯一）：memory/state/thesis.md（人 / LLM 可读；**由本模块唯一维护**）
  · 手改本文件会在下次写入时被规范化 —— 要写内容走本模块 API，不要手改历史行。
  · 格式（固定，可机械解析）：
      ## TH-YYYYMMDD-NNN
      - state: OPEN | CONVERGED | ARCHIVED
      - type: A | B | C            （驱动字典枚举；真源 = params.json，缺则用种子）
      - code / candidate_id / created_at / updated_at / gap / verdict / frozen_decision_id
      - claim: 当前认知（我认为现在发生什么）
      ### 演化史（append-only）
      - 2026-10-08 [CREATE] 入口 I2 资金异动触发起建 | ev:-
      - 2026-10-08 [HOP] 唤起 C4 三问（缺口=赔率不明） | ev:-

Thesis 字段（施工 4.2 / 设计 §4）：
  id / state / type / code / candidate_id / created_at / updated_at /
  gap（当前缺口，决定下一跳）/ verdict（BUY/WAIT/NO，收敛时写）/
  frozen_decision_id（下注定格 → decision_ledger 的 id）/ claim / history[]

生命周期（施工 4.2，唯一迁移执行器 = 本模块）：
  创建（←任意合法入口 I1~I7 触发）→ 验证/更新（←C/S 输出，**append-only**）
  → 收敛（←BUY/WAIT/NO）→ 归档（←结算进 Case）
  · 先 CONVERGED 才能 ARCHIVED（禁跳步）
  · **演化史 append-only**（设计 §4 硬约束 1）：本模块不提供任何改写 / 删除历史条目的入口；
    只有「下注时刻的定格」有决策效力（frozen_decision_id）。

★ 门控（施工 4.2「神经链级联门控」）：`cascade_gate()` 四判据写死
  ① Thesis 必须 OPEN ② 上游必须**显著**（|delta| ≥ 门槛）③ 未超链长上限
  ④ 还有未闭合缺口 ⑤ 剔除已访问节点（**防死循环**）。
  **允许级联、禁止裸调用**：门控放行才唤起下一跳，不满足即截止。
  `cascade_gate()` 是**纯裁决、不写盘**；真唤起后由调用方调 `record_hop()` 落痕，
  call_chain 由此可回放（供 C6 复盘最短路径 / 常见路径）。

★ 零硬编码映射（施工 4.2 写死）：缺口 → 能力的映射**不写在代码里**，一律读
  `data/state/capability_registry.json`（0.18）里各能力的 `trigger.gaps` 声明——
  新增 / 删除 S 只改注册表，**本文件零改动**。注册表尚未落盘（0.18 属 F5）时，
  `next_hop()` 返回空列表并显式标 `source=REGISTRY_ABSENT`（不猜、不降级成默认清单）。
  · **状态字段名 = `status`**（0.18 schema 写死；F3 落盘时本文件曾读 `state`，
    F5 落盘注册表时收口统一为 `status`——否则 retired 过滤永远失效、S9 幽灵复活）。
    遇到旧写法（含 `state` 键而无 `status`）的行**不静默当 active 放过**：
    跳过并标 `source=REGISTRY_WITH_LEGACY_FIELD`，把问题摆到台面上。

★ 参数来源：显著门槛 `cascade_significance` / 链长上限 `cascade_max_hops` 真源在
  `data/params.json`（0.8 属 F5）；读不到时用内置默认值并显式标 `source=BUILTIN_DEFAULT`。

被谁调用：tools/fs/wakeup.py（0.14 唤醒）、C3/C4 判断流程（经 AGENT.md 内核）、
          tools/fs/candidates.py 的上游（候选挂 thesis_id）。

用法：
  python tools/fs/thesis.py                 # 只读查看认知主线
  python tools/fs/thesis.py --selftest      # 内置验收（临时文件，不碰真数据）
  from tools.fs.thesis import create_thesis, update_thesis, cascade_gate, next_hop

规则：禁在别处内联 thesis.md 读写；禁在代码里写「缺口 → 能力」映射表；
      schema 变更必须升 SCHEMA_VERSION 并同步施工方案 4.2。
"""
import io
import json
import os
import re
import sys
import datetime as _dt

sys.path.insert(0, os.path.normpath(os.path.dirname(os.path.abspath(__file__))))
from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）

SCHEMA_VERSION = 1
THESIS_PATH = os.path.join(ROOT, "memory", "state", "thesis.md")
REGISTRY_PATH = os.path.join(ROOT, "data", "state", "capability_registry.json")
PARAMS_PATH = os.path.join(ROOT, "data", "params.json")

# ── 枚举（施工 4.2 / 设计 §4） ────────────────────────────────────────────────
STATE_OPEN = "OPEN"
STATE_CONVERGED = "CONVERGED"
STATE_ARCHIVED = "ARCHIVED"
STATES = (STATE_OPEN, STATE_CONVERGED, STATE_ARCHIVED)

GAP_DRIVER = "驱动不明"
GAP_ODDS = "赔率不明"
GAP_RISK = "风险不明"
GAP_EXEC = "执行不明"
GAP_NONE = "无"
GAPS = (GAP_DRIVER, GAP_ODDS, GAP_RISK, GAP_EXEC, GAP_NONE)

VERDICTS = ("BUY", "WAIT", "NO")

# Thesis 类型种子（政策催化 / 资金抢占 / 情绪扩散）；**真源 = params.json 驱动字典**
TYPES_FALLBACK = ("A", "B", "C")
TYPE_LABEL = {"A": "政策催化", "B": "资金抢占", "C": "情绪扩散"}

# 演化史条目类型：本模块自用 5 个 + LLM 可自由加（正则约束，非白名单）
KIND_CREATE = "CREATE"
KIND_HOP = "HOP"
KIND_CONVERGE = "CONVERGE"
KIND_ARCHIVE = "ARCHIVE"
KIND_NOTE = "NOTE"
_KIND_RE = re.compile(r"^[A-Z][A-Z_]{1,15}$")

FIELD_ORDER = ("state", "type", "code", "candidate_id", "created_at", "updated_at",
               "gap", "verdict", "frozen_decision_id", "claim")

# 门控默认值（真源 = params.json，缺则用此并标 BUILTIN_DEFAULT）
DEFAULT_CASCADE_THRESHOLD = 0.03
DEFAULT_MAX_HOPS = 4

_HEAD_RE = re.compile(r"^##\s+(TH-\S+)\s*$")
_FIELD_RE = re.compile(r"^-\s+([a-z_]+):\s*(.*)$")
_HIST_HEAD = "### 演化史（append-only）"
_HIST_RE = re.compile(r"^-\s+(\d{4}-\d{2}-\d{2})\s+\[([A-Z][A-Z_]*)\]\s+(.*)$")

HEADER = (
    "# Thesis 认知主线（施工方案 4.2 · 认知对象状态）\n"
    "\n"
    "> 本文件由 `tools/fs/thesis.py` 唯一维护（SCHEMA_VERSION=%d）。\n"
    "> **演化史 append-only**：新增只追加，禁手改 / 删除历史行；\n"
    "> 只有「下注时刻的定格」（frozen_decision_id）才有决策效力。\n"
    "> 字段口径见施工方案 4.2 / 核心能力设计方案 §4。\n"
) % SCHEMA_VERSION


class ThesisError(Exception):
    """Thesis 操作被拒（非法状态 / 非法迁移 / 格式不符 / 文件缺失）——显式抛错，不静默降级。"""


# ── 基础校验 ─────────────────────────────────────────────────────────────────
def _check_date(date):
    s = date.isoformat() if hasattr(date, "isoformat") else str(date)
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise ThesisError("日期必须是 YYYY-MM-DD，收到: %r" % (date,))
    try:
        _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        raise ThesisError("日期非法: %r" % (date,))
    return s


def _check_text(s, name, allow_empty=False):
    """单行文本校验：禁换行、禁 ' | ev:'（会破坏演化史行的机械解析）。"""
    if not isinstance(s, str):
        raise ThesisError("%s 必须是字符串，实际: %r" % (name, type(s).__name__))
    if not allow_empty and not s.strip():
        raise ThesisError("%s 不得为空" % name)
    if "\n" in s or "\r" in s:
        raise ThesisError("%s 不得含换行（本文件每字段一行）" % name)
    if " | ev:" in s:
        raise ThesisError("%s 不得含 ' | ev:'（会破坏演化史行的机械解析）" % name)
    return s


def _check_verdict(v):
    if v is not None and v not in VERDICTS:
        raise ThesisError("verdict 只能是 %s 或 null，实际: %r" % (" / ".join(VERDICTS), v))
    return v


def _check_gap(g):
    if g not in GAPS:
        raise ThesisError("gap 只能是 %s，实际: %r" % (" / ".join(GAPS), g))
    return g


def _types(params_path=None):
    """Thesis 类型枚举：优先读 params.json 驱动字典（0.8），缺失则用种子 + 标来源。"""
    d = _read_json(PARAMS_PATH if params_path is None else params_path)
    if d:
        dd = (d.get("params") or {}).get("driver_dict")
        v = dd.get("value") if isinstance(dd, dict) else None
        if isinstance(v, dict):
            keys = tuple(k for k in v.keys() if isinstance(k, str) and len(k) == 1)
            if keys:
                return keys, "PARAMS"
    return TYPES_FALLBACK, "BUILTIN_DEFAULT"


def _read_json(path):
    """读 json；不存在 / 损坏 → 返回 None（调用方决定是抛错还是标来源缺省）。"""
    if not path or not os.path.exists(path):
        return None
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def _read_param(name, default, params_path=None):
    """读 params.json 里的 params.<name>.value；读不到 → (default, 'BUILTIN_DEFAULT')。"""
    d = _read_json(PARAMS_PATH if params_path is None else params_path)
    if d:
        p = (d.get("params") or {}).get(name)
        if isinstance(p, dict) and p.get("value") is not None:
            return p["value"], "PARAMS"
    return default, "BUILTIN_DEFAULT"


# ── 文件解析 / 渲染（本模块是唯一写者） ───────────────────────────────────────
def _parse(text):
    """解析 thesis.md → [thesis dict]（保序）。格式不符即抛错，不静默丢弃条目。"""
    items = []
    cur = None
    hist = False
    for ln, line in enumerate(text.split("\n"), 1):
        s = line.rstrip("\r").strip()
        if not s:
            continue
        m = _HEAD_RE.match(s)
        if m:
            cur = {"id": m.group(1), "history": []}
            for k in FIELD_ORDER:
                cur.setdefault(k, None)
            items.append(cur)
            hist = False
            continue
        if s == _HIST_HEAD:
            if cur is None:
                raise ThesisError("演化史区出现在任何 Thesis 之前（第 %d 行）" % ln)
            hist = True
            continue
        if cur is None:
            continue                                        # 文件头注释
        if hist:
            m = _HIST_RE.match(s)
            if not m:
                raise ThesisError("演化史行格式不符（第 %d 行）：%r —— 应为 "
                                  "'- YYYY-MM-DD [KIND] 文本 | ev:...'" % (ln, s))
            date, kind, body = m.group(1), m.group(2), m.group(3)
            if " | ev:" in body:
                body, evs = body.rsplit(" | ev:", 1)
            else:
                evs = "-"
            cur["history"].append({
                "date": _check_date(date),
                "kind": kind,
                "text": body.strip(),
                "evidence": [e for e in evs.split(",") if e and e.strip() and e.strip() != "-"],
            })
            continue
        m = _FIELD_RE.match(s)
        if not m:
            raise ThesisError("字段行格式不符（第 %d 行）：%r" % (ln, s))
        k, v = m.group(1), m.group(2).strip()
        if k not in FIELD_ORDER:
            raise ThesisError("未知字段 %r（第 %d 行）—— 合法字段：%s"
                              % (k, ln, " / ".join(FIELD_ORDER)))
        cur[k] = None if v in ("", "-") else v
    for c in items:
        _validate_item(c)
    ids = [c["id"] for c in items]
    if len(ids) != len(set(ids)):
        raise ThesisError("Thesis id 重复：%s" % ids)
    return items


def _validate_item(c):
    if c.get("state") not in STATES:
        raise ThesisError("Thesis %s 的 state 非法: %r" % (c.get("id"), c.get("state")))
    _check_date(c.get("created_at"))
    _check_date(c.get("updated_at"))
    _check_gap(c.get("gap"))
    _check_verdict(c.get("verdict"))
    _check_text(c.get("claim"), "claim")
    if not isinstance(c.get("history"), list) or not c["history"]:
        raise ThesisError("Thesis %s 的演化史不得为空（至少一条 CREATE）" % c.get("id"))
    for e in c["history"]:
        if not _KIND_RE.match(e["kind"]):
            raise ThesisError("演化史 kind 非法: %r（须为大写词，如 EVIDENCE / CONTRADICT）" % e["kind"])
        _check_text(e["text"], "history.text")
    if c["state"] == STATE_ARCHIVED and c.get("verdict") is None:
        raise ThesisError("Thesis %s 已归档但无 verdict —— 归档前必须收敛" % c["id"])
    return c


def _render(items):
    out = [HEADER]
    for c in items:
        out.append("")
        out.append("## %s" % c["id"])
        out.append("")
        for k in FIELD_ORDER:
            v = c.get(k)
            out.append("- %s: %s" % (k, "-" if v is None else v))
        out.append("")
        out.append(_HIST_HEAD)
        out.append("")
        for e in c["history"]:
            ev = ",".join(e["evidence"]) if e["evidence"] else "-"
            out.append("- %s [%s] %s | ev:%s" % (e["date"], e["kind"], e["text"], ev))
    out.append("")
    return "\n".join(out)


def _load(path=None):
    """读认知主线。文件缺失 → 抛错（需先 init_file 初始化，不静默新建）。"""
    p = path or THESIS_PATH
    if not os.path.exists(p):
        raise ThesisError("Thesis 文件不存在：%s —— 需先初始化（不得静默新建）" % p)
    with io.open(p, encoding="utf-8") as f:
        return _parse(f.read())


def _save(items, path=None):
    p = path or THESIS_PATH
    _WG.check_write(p)
    for c in items:
        _validate_item(c)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(_render(items))
    os.replace(tmp, p)


def init_file(path=None):
    """初始化认知主线（空文件：仅文件头）。已存在 → 抛错（防覆盖已有认知）。"""
    p = path or THESIS_PATH
    if os.path.exists(p):
        raise ThesisError("Thesis 文件已存在，不得重复初始化：%s" % p)
    _save([], p)
    return p


def _new_id(items, as_of):
    prefix = "TH-" + as_of.replace("-", "")
    taken = {c["id"] for c in items}
    n = 1
    while ("%s-%03d" % (prefix, n)) in taken:
        n += 1
    return "%s-%03d" % (prefix, n)


# ── 生命周期（创建 → 更新 → 收敛 → 归档） ────────────────────────────────────
def create_thesis(as_of, ttype, claim, code=None, candidate_id=None, gap=GAP_DRIVER,
                  evidence=None, path=None, params_path=None):
    """创建 Thesis（←任意合法入口 I1~I7 触发）。

    规则：一个候选（candidate_id）同时只能挂**一条未收敛**的 Thesis（设计 §4 不合并不打架）；
    type 必须落在驱动字典枚举内（真源 params.json）；gap 决定后续下一跳。
    """
    as_of = _check_date(as_of)
    types, tsrc = _types(params_path)
    if ttype not in types:
        raise ThesisError("Thesis 类型只能是 %s（来源=%s），实际: %r"
                          % (" / ".join(types), tsrc, ttype))
    _check_gap(gap)
    _check_text(claim, "claim")
    items = _load(path)
    if candidate_id:
        for c in items:
            if c.get("candidate_id") == candidate_id and c["state"] == STATE_OPEN:
                raise ThesisError("候选 %s 已挂未收敛的 Thesis %s —— 先收敛再起新条"
                                  % (candidate_id, c["id"]))
    th = {"id": _new_id(items, as_of), "state": STATE_OPEN, "type": ttype,
          "code": code, "candidate_id": candidate_id, "created_at": as_of,
          "updated_at": as_of, "gap": gap, "verdict": None,
          "frozen_decision_id": None, "claim": claim, "history": []}
    th["history"].append(_hist(as_of, KIND_CREATE, "入口触发创建：%s" % claim, evidence))
    items.append(th)
    _save(items, path)
    return th


def _hist(date, kind, text, evidence):
    if not _KIND_RE.match(kind):
        raise ThesisError("演化史 kind 非法: %r" % (kind,))
    _check_text(text, "history.text")
    ev = []
    for e in (evidence or []):
        ev.append(_check_text(e, "evidence 条目"))
    return {"date": _check_date(date), "kind": kind, "text": text, "evidence": ev}


def _find(items, th_id):
    for c in items:
        if c["id"] == th_id:
            return c
    raise ThesisError("Thesis 不存在: %s" % th_id)


def update_thesis(th_id, as_of, kind, text, evidence=None, gap=None, path=None):
    """更新 Thesis（←C/S 输出：新证据支持 → 加强；矛盾 → 质疑/重构）。

    ★ 演化史 append-only：只追加一条，**本函数不提供任何改写/删除历史条目的入口**。
    gap 可选更新（决定下一跳）；state 不变（收敛走 converge_thesis）。
    """
    as_of = _check_date(as_of)
    if gap is not None:
        _check_gap(gap)
    entry = _hist(as_of, kind, text, evidence)
    items = _load(path)
    c = _find(items, th_id)
    if c["state"] != STATE_OPEN:
        raise ThesisError("Thesis %s 状态为 %s，已不再更新（演化史只对 OPEN 追加）"
                          % (th_id, c["state"]))
    c["history"].append(entry)
    if gap is not None:
        c["gap"] = gap
    c["updated_at"] = as_of
    _save(items, path)
    return c


def converge_thesis(th_id, as_of, verdict, frozen_decision_id=None, note=None, path=None):
    """收敛（←三问结论 BUY / WAIT / NO）。收敛即定格：frozen_decision_id 指向决策快照。

    · 收敛后不再接受 update_thesis（演化史停止追加）；
    · WAIT/NO 也可收敛（停在这一轮认知的终点），后续起新条。
    """
    as_of = _check_date(as_of)
    _check_verdict(verdict)
    if verdict is None:
        raise ThesisError("收敛必须给 verdict（BUY / WAIT / NO）")
    items = _load(path)
    c = _find(items, th_id)
    if c["state"] != STATE_OPEN:
        raise ThesisError("Thesis %s 状态为 %s，不得重复收敛" % (th_id, c["state"]))
    if frozen_decision_id is not None:
        _check_text(frozen_decision_id, "frozen_decision_id")
    c["state"] = STATE_CONVERGED
    c["verdict"] = verdict
    c["frozen_decision_id"] = frozen_decision_id
    c["updated_at"] = as_of
    txt = "收敛为 %s" % verdict
    if frozen_decision_id:
        txt += "（定格 decision=%s）" % frozen_decision_id
    if note:
        txt += "：" + _check_text(note, "note")
    c["history"].append(_hist(as_of, KIND_CONVERGE, txt, None))
    _save(items, path)
    return c


def archive_thesis(th_id, as_of, case_id=None, note=None, path=None):
    """归档（←结算进 Case）。**必须先 CONVERGED**（禁跳步）；归档后本文件只读不回改。"""
    as_of = _check_date(as_of)
    items = _load(path)
    c = _find(items, th_id)
    if c["state"] != STATE_CONVERGED:
        raise ThesisError("Thesis %s 状态为 %s —— 归档前必须先收敛（CONVERGED）"
                          % (th_id, c["state"]))
    c["state"] = STATE_ARCHIVED
    c["updated_at"] = as_of
    txt = "归档" + ("（case=%s）" % case_id if case_id else "")
    if note:
        txt += "：" + _check_text(note, "note")
    c["history"].append(_hist(as_of, KIND_ARCHIVE, txt, None))
    _save(items, path)
    return c


def read_thesis(th_id=None, path=None):
    """读一条（给 id）或全部（不给 id）。"""
    items = _load(path)
    if th_id is None:
        return items
    return _find(items, th_id)


def list_thesis(state=None, gap=None, code=None, path=None):
    """按条件列 Thesis。"""
    if state is not None and state not in STATES:
        raise ThesisError("未知 state %r，合法值：%s" % (state, " / ".join(STATES)))
    items = _load(path)
    out = [c for c in items
           if (state is None or c["state"] == state)
           and (gap is None or c["gap"] == gap)
           and (code is None or c.get("code") == code)]
    return out


# ── 调度层：下一跳选择 + 级联门控 ─────────────────────────────────────────────
def next_hop(gap, registry_path=None):
    """按**当前缺口**从能力注册表选下一跳（施工 4.2：读注册表 trigger，禁硬编码映射表）。

    匹配口径：注册表 `capabilities[].trigger.gaps`（列表）或 `trigger.gap`（单值）
    含当前 gap 的能力 —— **映射关系声明在注册表里，本文件零硬编码**。
    状态字段读 `status`（0.18 schema；旧写法 `state` 的行被跳过并显式标注，不静默放过）。
    注册表未落盘（0.18 属 F5）→ 返回空列表 + source=REGISTRY_ABSENT（不猜、不给默认清单）。
    """
    _check_gap(gap)
    if gap == GAP_NONE:
        return {"gap": gap, "next": [], "source": "NO_GAP", "note": "无缺口 → 不唤起"}
    reg = _read_json(REGISTRY_PATH if registry_path is None else registry_path)
    if not reg:
        return {"gap": gap, "next": [], "source": "REGISTRY_ABSENT",
                "note": "能力注册表（0.18）尚未落盘 —— 属预期：F5 落地后自动生效"}
    caps = reg.get("capabilities")
    if not isinstance(caps, list):
        return {"gap": gap, "next": [], "source": "REGISTRY_MALFORMED",
                "note": "注册表缺 capabilities 列表"}
    out, legacy = [], []
    for cap in caps:
        if not isinstance(cap, dict):
            continue
        if "status" not in cap and "state" in cap:
            legacy.append(cap.get("id"))              # 旧字段写法：不静默当 active
            continue
        if cap.get("status") in ("retired", "RETIRED", "disabled"):
            continue
        trig = cap.get("trigger")
        if not isinstance(trig, dict):
            continue
        gaps = trig.get("gaps")
        if gaps is None and trig.get("gap"):
            gaps = [trig["gap"]]
        if isinstance(gaps, list) and gap in gaps:
            out.append(cap.get("id"))
    if legacy:
        return {"gap": gap, "next": [i for i in out if i],
                "source": "REGISTRY_WITH_LEGACY_FIELD",
                "note": "含旧字段 state 的行被跳过：%s（0.18 schema 用 status）" % legacy}
    return {"gap": gap, "next": [i for i in out if i], "source": "REGISTRY", "note": ""}


def cascade_gate(th_id, upstream_delta, as_of, threshold=None, max_hops=None, visited=None,
                 path=None, registry_path=None, params_path=None):
    """级联门控（施工 4.2）：**允许级联、禁止裸调用**。纯裁决、不写盘、可反复调用。

    放行条件（全部满足才 allow）：
      ① Thesis 存在且 state == OPEN
      ② gap != 无（还有未闭合缺口）
      ③ 上游**显著**：abs(upstream_delta) >= threshold（门槛真源 = params.json）
      ④ 未超链长上限（已 HOP 次数 < max_hops）
      ⑤ 剔除 visited 后仍有可唤起能力（防死循环 / 防重复裸转）
    放行后由调用方真唤起，再调 record_hop() 落痕（call_chain 由此可回放）。
    """
    as_of = _check_date(as_of)
    th = _find(_load(path), th_id)
    t_val, t_src = _read_param("cascade_significance", DEFAULT_CASCADE_THRESHOLD, params_path)
    h_val, h_src = _read_param("cascade_max_hops", DEFAULT_MAX_HOPS, params_path)
    th_v = threshold if threshold is not None else t_val
    hops = sum(1 for e in th["history"] if e["kind"] == KIND_HOP)
    mx = max_hops if max_hops is not None else int(h_val)
    base = {"thesis_id": th_id, "as_of": as_of, "gap": th["gap"], "hops": hops,
            "max_hops": mx, "threshold": th_v, "registry_source": None,
            "threshold_source": t_src if threshold is None else "EXPLICIT",
            "hops_source": h_src if max_hops is None else "EXPLICIT"}

    if th["state"] != STATE_OPEN:
        base.update({"allow": False, "reason": "THESIS_NOT_OPEN", "next": []})
        return base
    if th["gap"] == GAP_NONE:
        base.update({"allow": False, "reason": "NO_GAP", "next": []})
        return base
    if abs(float(upstream_delta)) < float(th_v):
        base.update({"allow": False, "reason": "NOT_SIGNIFICANT", "next": []})
        return base
    if hops >= mx:
        base.update({"allow": False, "reason": "MAX_HOPS", "next": []})
        return base
    hop = next_hop(th["gap"], registry_path)
    seen = set(visited or [])
    cand = [i for i in hop["next"] if i not in seen]
    base["registry_source"] = hop["source"]
    if not cand:
        base.update({"allow": False, "next": [],
                     "reason": "ALL_VISITED" if hop["next"] else "NO_ELIGIBLE_NEXT"})
        return base
    base.update({"allow": True, "reason": "ALLOWED", "next": cand})
    return base


def record_hop(th_id, as_of, ability_id, gap=None, note=None, path=None):
    """记录一次**实际发生**的唤起（call_chain 由此可回放，供 C6 复盘）。

    ★ 与门控分离：cascade_gate 只裁决，真唤起后才落痕；裸调用无痕 = 事后可审计。
    """
    as_of = _check_date(as_of)
    _check_text(ability_id, "ability_id")
    txt = "唤起 %s" % ability_id
    if gap:
        txt += "（缺口=%s）" % _check_gap(gap)
    if note:
        txt += "：" + _check_text(note, "note")
    items = _load(path)
    c = _find(items, th_id)
    if c["state"] != STATE_OPEN:
        raise ThesisError("Thesis %s 状态为 %s，不得再记录唤起" % (th_id, c["state"]))
    c["history"].append(_hist(as_of, KIND_HOP, txt, None))
    c["updated_at"] = as_of
    _save(items, path)
    return c


def call_chain(th_id, path=None):
    """实际走过的链（按演化史顺序取 HOP 落痕），供 0.3 决策快照的 call_chain 字段使用。"""
    th = _find(_load(path), th_id)
    chain = []
    for e in th["history"]:
        if e["kind"] == KIND_HOP:
            m = re.match(r"^唤起\s+(\S+?)(?:（|$)", e["text"])
            if m:
                chain.append(m.group(1))
    return chain


# ── 自检（--selftest）：全部在临时文件跑，不碰真实数据 ─────────────────────────
def _selftest():
    import tempfile
    import shutil
    ok = fail = 0
    fails = []

    def T(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [OK] %s" % name)
        else:
            fail += 1
            fails.append(name)
            print("  [!!] %s" % name)

    def raises(fn, *a, **kw):
        try:
            fn(*a, **kw)
            return False
        except ThesisError:
            return True

    def _fp(p):
        """真文件指纹（不存在 → None）—— 用于验证自检零污染。"""
        if not os.path.exists(p):
            return None
        import hashlib
        with io.open(p, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()

    real_fp = _fp(THESIS_PATH)
    tmp = tempfile.mkdtemp(prefix="ta_thesis_selftest_")
    th_p = os.path.join(tmp, "thesis.md")
    reg_p = os.path.join(tmp, "registry.json")
    par_p = os.path.join(tmp, "params.json")
    D1, D2 = "2026-10-08", "2026-10-09"
    try:
        print("== A. 初始化与格式 ==")
        init_file(th_p)
        T("A1 初始化后空文件可解析", _load(th_p) == [])
        T("A2 重复初始化被拒", raises(init_file, th_p))
        T("A3 文件不存在时读 → 拒（不静默新建）",
          raises(_load, os.path.join(tmp, "nope.md")))
        T("A4 文件头含 SCHEMA_VERSION", str(SCHEMA_VERSION) in io.open(
            th_p, encoding="utf-8").read())
        T("A5 演化史区标题固定", _HIST_HEAD == "### 演化史（append-only）")

        print("== B. 创建（←任意合法入口） ==")
        t1 = create_thesis(D1, "B", "机构席位在承接，判断为资金抢占型", code="sh600000",
                           candidate_id="CD-20261008-001", gap=GAP_ODDS, path=th_p)
        T("B1 id = TH-YYYYMMDD-NNN", t1["id"] == "TH-20261008-001")
        T("B2 初始 state = OPEN", t1["state"] == STATE_OPEN)
        T("B3 初始 gap 生效", t1["gap"] == GAP_ODDS)
        T("B4 演化史自动落 CREATE 一条",
          len(t1["history"]) == 1 and t1["history"][0]["kind"] == KIND_CREATE)
        T("B5 磁盘可解析回同一 id", _load(th_p)[0]["id"] == t1["id"])
        T("B6 非法类型 → 拒",
          raises(create_thesis, D1, "Z", "x", None, None, GAP_DRIVER, None, th_p))
        T("B7 同候选再起未收敛条 → 拒（一个候选一条 Thesis）",
          raises(create_thesis, D1, "B", "x", "sh600000", "CD-20261008-001",
                 GAP_DRIVER, None, th_p))
        T("B8 gap 非法 → 拒", raises(create_thesis, D1, "B", "x", None, None, "瞎猜", None, th_p))

        print("== C. 演化史 append-only ==")
        t1 = update_thesis(t1["id"], D1, "EVIDENCE", "龙虎榜机构净买 4000 万",
                           ["DT20261008-003"], path=th_p)
        T("C1 追加一条证据", len(t1["history"]) == 2)
        t1 = update_thesis(t1["id"], D2, "CONTRADICT", "次日承接转弱，量能未续",
                           gap=GAP_RISK, path=th_p)
        T("C2 追加矛盾不破坏旧条目",
          len(t1["history"]) == 3 and t1["history"][0]["kind"] == KIND_CREATE)
        T("C3 更新 gap 生效", t1["gap"] == GAP_RISK)
        T("C4 更新不改 state", t1["state"] == STATE_OPEN)
        T("C5 evidence 往返一致（写盘后读回）",
          _load(th_p)[0]["history"][1]["evidence"] == ["DT20261008-003"])
        T("C6 模块无删除/改写历史条目的入口",
          not [n for n in ("del_history", "edit_history", "rewrite", "set_history")
               if hasattr(sys.modules[__name__], n)])
        T("C7 非法 kind → 拒",
          raises(update_thesis, t1["id"], D2, "bad", "x", None, None, th_p))
        T("C8 含 ' | ev:' 的文本 → 拒（保解析安全）",
          raises(update_thesis, t1["id"], D2, "NOTE", "坏行 | ev:x", None, None, th_p))

        print("== D. 生命周期（收敛 → 归档） ==")
        T("D1 verdict 非法 → 拒",
          raises(converge_thesis, t1["id"], D2, "HOLD", None, None, th_p))
        t1 = converge_thesis(t1["id"], D2, "BUY", frozen_decision_id="DEC-20261009-001",
                             note="三问答齐", path=th_p)
        T("D2 收敛后 state = CONVERGED", t1["state"] == STATE_CONVERGED)
        T("D3 定格 decision 写入", t1["frozen_decision_id"] == "DEC-20261009-001")
        T("D4 收敛后不得再更新演化史",
          raises(update_thesis, t1["id"], D2, "NOTE", "x", None, None, th_p))
        T("D5 收敛后不得重复收敛",
          raises(converge_thesis, t1["id"], D2, "NO", None, None, th_p))
        t1 = archive_thesis(t1["id"], D2, case_id="CASE-20261009-001", path=th_p)
        T("D6 归档后 state = ARCHIVED", t1["state"] == STATE_ARCHIVED)
        t2 = create_thesis(D2, "A", "政策落地传导至板块", code="sz000001", path=th_p)
        T("D7 未收敛直接归档 → 拒（禁跳步）",
          raises(archive_thesis, t2["id"], D2, None, None, th_p))
        T("D8 收敛后同候选可再起新条（旧条已收敛）",
          create_thesis(D2, "B", "第二轮", "sh600000", "CD-20261008-001",
                        GAP_DRIVER, None, th_p)["id"] == "TH-20261009-002")

        print("== E. 级联门控（施工 4.2 四判据） ==")

        def _reg(mode="status"):
            """注册表种子：mode='legacy' 造 0.18 之前的旧 `state` 写法（测不静默放过）。"""
            if mode == "legacy":
                c = [{"id": "S4", "state": "active", "trigger": {"gaps": [GAP_RISK]}}]
            else:
                c = [
                    {"id": "S4", "status": "active", "trigger": {"gaps": [GAP_RISK]}},
                    {"id": "S3", "status": "active", "trigger": {"gaps": [GAP_ODDS]}},
                    {"id": "S9", "status": "retired", "trigger": {"gaps": [GAP_DRIVER]}},
                ]
            with io.open(reg_p, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "capabilities": c}, f, ensure_ascii=False)

        _reg()
        t3 = create_thesis(D2, "B", "门控测试", path=th_p)
        g = cascade_gate(t3["id"], 0.05, D2, visited=None, path=th_p, registry_path=reg_p)
        T("E1 缺口语义：gap=驱动不明 → 命中 S9 但 retired 被剔 → 无合格项",
          g["allow"] is False and g["reason"] == "NO_ELIGIBLE_NEXT")
        nh1 = next_hop(GAP_RISK, reg_p)
        T("E1b 状态字段读 `status`（0.18 schema）：active 行被认出",
          nh1["source"] == "REGISTRY" and nh1["next"] == ["S4"])
        nh0 = next_hop(GAP_DRIVER, reg_p)
        T("E1c retired 行被正确剔除（S9 不复活）", nh0["next"] == [])
        _reg("legacy")
        nh2 = next_hop(GAP_RISK, reg_p)
        T("E1d 旧写法 state 不静默当 active（显式标 legacy，不猜）",
          nh2["source"] == "REGISTRY_WITH_LEGACY_FIELD" and nh2["next"] == []
          and "state" in nh2["note"])
        _reg()
        t5 = create_thesis(D2, "A", "无缺口测试（当前认知无需再验证）", gap=GAP_NONE, path=th_p)
        g = cascade_gate(t5["id"], 0.05, D2, visited=None, path=th_p, registry_path=reg_p)
        T("E2 gap 无 → 不放行", g["allow"] is False and g["reason"] == "NO_GAP")
        t4 = create_thesis(D2, "B", "门控测试2", gap=GAP_RISK, path=th_p)
        g = cascade_gate(t4["id"], 0.001, D2, path=th_p, registry_path=reg_p)
        T("E3 上游不显著 → 截止（NOT_SIGNIFICANT）",
          g["allow"] is False and g["reason"] == "NOT_SIGNIFICANT")
        g = cascade_gate(t4["id"], 0.06, D2, path=th_p, registry_path=reg_p)
        T("E4 显著 + 有缺口 → 放行且给出下一跳",
          g["allow"] is True and "S4" in g["next"])
        T("E5 params 缺失 → 门槛标 BUILTIN_DEFAULT 且用内置默认（不猜、来源显式）",
          cascade_gate(t4["id"], 0.06, D2, path=th_p, registry_path=reg_p,
                       params_path=os.path.join(tmp, "no_params.json"))["threshold_source"]
          == "BUILTIN_DEFAULT")
        T("E5b params 落盘状态与来源标注一致（%s）"
          % ("PARAMS" if os.path.exists(PARAMS_PATH) else "BUILTIN_DEFAULT"),
          g["threshold_source"] == ("PARAMS" if os.path.exists(PARAMS_PATH)
                                    else "BUILTIN_DEFAULT"))
        g2 = cascade_gate(t4["id"], 0.06, D2, visited=["S4"], path=th_p, registry_path=reg_p)
        T("E6 visited 可防重复唤起（ALL_VISITED）",
          g2["allow"] is False and g2["reason"] == "ALL_VISITED")
        record_hop(t4["id"], D2, "S4", gap=GAP_RISK, path=th_p)
        T("E7 record_hop 落痕、call_chain 可回放", call_chain(t4["id"], th_p) == ["S4"])
        g3 = cascade_gate(t4["id"], 0.06, D2, max_hops=1, path=th_p, registry_path=reg_p)
        T("E8 超链长上限 → 截止（MAX_HOPS）",
          g3["allow"] is False and g3["reason"] == "MAX_HOPS")
        g4 = cascade_gate(t1["id"], 0.06, D2, path=th_p, registry_path=reg_p)
        T("E9 已归档 Thesis → 不放行", g4["allow"] is False
          and g4["reason"] == "THESIS_NOT_OPEN")
        T("E10 cascade_gate 纯裁决（未写盘：history 条数不变）",
          len(_find(_load(th_p), t3["id"])["history"]) == 1)

        print("== F. 下一跳读注册表（禁硬编码映射） ==")
        n = next_hop(GAP_RISK, reg_p)
        T("F1 读注册表 trigger.gaps 命中", n["next"] == ["S4"] and n["source"] == "REGISTRY")
        T("F2 gap=无 → 不唤起", next_hop(GAP_NONE, reg_p)["source"] == "NO_GAP")
        T("F3 注册表缺失 → 空 + REGISTRY_ABSENT（不猜）",
          next_hop(GAP_RISK, os.path.join(tmp, "no.json"))["source"] == "REGISTRY_ABSENT")
        T("F4 无效 gap → 拒", raises(next_hop, "瞎猜", reg_p))
        with io.open(par_p, "w", encoding="utf-8") as f:
            json.dump({"version": "1.0.0", "params": {
                "cascade_significance": {"value": 0.10},
                "driver_dict": {"value": {"A": {}, "B": {}, "C": {}, "D": {}}}}}, f,
                ensure_ascii=False)
        g5 = cascade_gate(t4["id"], 0.06, D2, path=th_p, registry_path=reg_p, params_path=par_p)
        T("F5 params 落地后门槛自动生效（0.06 < 0.10 → 截止）",
          g5["allow"] is False and g5["reason"] == "NOT_SIGNIFICANT"
          and g5["threshold_source"] == "PARAMS")
        types, src = _types(par_p)
        T("F6 驱动字典扩展类型（含 D）被识别", "D" in types and src == "PARAMS")
        T("F7 新类型可建条（新增类型 = 参数变更，不改方法论）",
          create_thesis(D2, "D", "新增类型试建", path=th_p, params_path=par_p)["type"] == "D")

        print("== G. 纪律 ==")
        import ast

        def _lits_in(t, fname):
            """函数体内出现的「能力代号」字符串常量（形如 S4 / C3 / E7）——空 = 无硬编码清单。"""
            fn = next((n for n in ast.walk(t)
                       if isinstance(n, ast.FunctionDef) and n.name == fname), None)
            if fn is None:
                return ["<no %s>" % fname]
            return [n.value for n in ast.walk(fn)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and re.match(r"^[SCE]\d+$", n.value)]

        src_txt = io.open(os.path.abspath(__file__), encoding="utf-8").read()
        tree = ast.parse(src_txt)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        T("G1 无网络/子进程类导入（不直连）",
          not (imported & {"requests", "urllib", "http", "socket", "subprocess", "openai"}))
        T("G2 next_hop 内无「缺口 → 能力名」硬编码（映射一律走注册表）",
          not _lits_in(tree, "next_hop"))
        T("G3 真 thesis.md 未被自检触碰（指纹不变）", _fp(THESIS_PATH) == real_fp)
        bad = _load(th_p)
        bad[0]["state"] = "DONE"
        T("G4 非法 state 被解析校验拒", raises(lambda x: _validate_item(x), bad[0]))
        bad2 = {"id": "TH-X", "state": STATE_OPEN, "created_at": D1, "updated_at": D1,
                "gap": GAP_NONE, "verdict": None, "claim": "x", "history": []}
        T("G5 空演化史被拒", raises(lambda x: _validate_item(x), bad2))
        bad3 = dict(bad2)
        bad3["history"] = [{"date": D1, "kind": "bad", "text": "x", "evidence": []}]
        T("G6 演化史 kind 格式非法被拒", raises(lambda x: _validate_item(x), bad3))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：%s" % " / ".join(fails))
    return 1 if fail else 0


def _main(argv):
    if "--selftest" in argv:
        print("thesis.py 自检（SCHEMA_VERSION=%d · 临时文件运行）" % SCHEMA_VERSION)
        return _selftest()
    print("Thesis 认知主线 · %s" % THESIS_PATH)
    if not os.path.exists(THESIS_PATH):
        print("  状态 : 尚未初始化（需按施工方案 4.2 建 memory/state/thesis.md）")
        return 1
    items = _load()
    print("  条数 : %d" % len(items))
    for st in STATES:
        n = sum(1 for c in items if c["state"] == st)
        if n:
            print("    %-10s %d" % (st, n))
    for c in items:
        tlabel = TYPE_LABEL.get(c["type"], c["type"])
        print("    %s  %-10s type=%s(%s)  gap=%s  verdict=%s  hist=%d"
              % (c["id"], c["state"], c["type"], tlabel, c["gap"],
                 c["verdict"] or "-", len(c["history"])))
    r = next_hop(GAP_DRIVER)
    print("  调度 : 下一跳来源 = %s（%d 项）—— %s"
          % (r["source"], len(r["next"]), r["note"] or "已读注册表"))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
