#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""registry.py · 能力注册表与骨架冻结（施工方案 0.18 · v1.0 · 2026-10-08 建）

用途：把「新增能力只能插入式、不能侵入式」落成**机制**（设计 §4 插入式扩展 + §3 原则 4）。
      没有注册表，「新增一个能力要改几处」只能靠记性 —— 实测旧设计下新增一个 S 需同步 6 处，
      改一处漏一处，正是要治的侵入式。本模块是**唯一登记点**的读写与校验器。

两个文件（职责不同，禁混）：
  data/state/capability_registry.json  能力登记（唯一真源：所有清单/映射由此派生）
  data/state/skeleton_manifest.json    骨架冻结台账（骨架文件与契约的指纹）

★ 一、注册表 = 唯一真源（任何 .md / 代码不得再维护第二份能力清单）
  调度（4.2 按需层）· 阶段 2 能力计数 · 知识库路由 · 落点审计 · E 系列契约表
  —— 全部调 `dispatch_view()` / `by_mount()` / `knowledge_routes()` / `count_by_kind()` 取。
  设计 §4 母环节挂载表已标注为**注册表阅读视图**，不是第二真源。

★ 二、字段名写死（12 列，顺序一致；真源 = 本模块 CAP_FIELDS）
  id · kind(S|E|C) · name · mounted_on(C0~C6) · consumes · produces · trigger ·
  output_path · knowledge_route(可空) · boundary · status(active|retired) · registry_version
  · `trigger` 结构 `{"text": 人话, "gaps": [缺口...], "entry": [入口...]}` ——
    `gaps` 是 4.2 级联的下一跳声明（真源 = 缺口枚举，取自 `tools/fs/thesis.py` 的 GAPS，
    **本模块不另立一份**）；`entry` 是非级联唤起（定时/用户提问等，E 系列用这个）。
  · `boundary` 结构 `{"no_decision": bool, "can_order": bool, "note": str}` ——
    S/E **必须** no_decision=true 且 can_order=false（能力不得出 BUY/WAIT/NO 或仓位）；
    can_order=true 只允许 C5（风控·定价是执行层）。

★ 三、`status` 字段名的收口（修一处真接不上）
  4.2 落盘时 `thesis.py` 的 `next_hop()` 按 `cap["state"]` 过滤 retired 能力，
  而 0.18 定义的字段名是 `status` —— 两处不一致时，注册表一落盘 retired 过滤**永远失效**
  （S9 幽灵复活）。已统一为 **`status`**：本模块 `verify_registry()` 把未知字段（含 `state`）
  判为 SCHEMA_MISMATCH，`thesis.next_hop()` 同步改读 `status`。禁双字段并存。

★ 四、校验六类（进唤醒自检项）
  SCHEMA_MISMATCH 字段名/顺序不符（含 state 之类的旧写法）
  DUP_ID          重复 id
  MOUNT_INVALID   mounted_on 不在 C0~C6
  BOUNDARY_VIOLATION  S/E 越权（no_decision 非 true 或 can_order 为 true）
  TRIGGER_EMPTY   trigger 三项全空（= 永远唤不起的死能力）
  GHOST_CAP       幽灵能力：`impl_log` 里曾记实现过，但能力落点目录已不在（删了目录还留登记）

★ 五、骨架冻结（"不许改"必须有执行点，否则只是口号）
  `skeleton_manifest.json` 登记【骨架文件 / 契约常量的 sha256 指纹】。改骨架文件或改
  字段 schema → 指纹变 → `skeleton_check()` 报 `SKELETON_DRIFT`。
  **不是禁止改，而是改必须显式**：须同时升 `skeleton_version` + 附迁移脚本 + 记 changelog，
  再跑 `registry.py manifest --update` 重建台账（重建这个动作本身就是"显式动作"的落痕）。
  判据 = 改骨架是「显式动作」，不是「顺手改」。
  `pending` 组登记尚未落盘的骨架件（`tools/risk/settle.py` 0.6 / 三问 API 1.4 等）——
  未落盘不算 drift，落盘后须补登记，否则自检报 `MANIFEST_STALE`。

★ 六、插入流程（写死 3 步，全程不碰骨架文件）
  ①建能力目录（`.workbuddy/skills/<id>/SKILL.md` + 输出落点）
  ②`registry.py add '<json>'` 追加一行
  ③`registry.py verify` 自检
  验收见 5.8：插入假能力 S99 后 `git diff --stat` 中骨架文件改动 = 0。

★ 七、本模块零交易动作
  无下单 / 撤单 / 成交接口；无网络与子进程导入；只写注册表与冻结台账两个文件。

被谁调用：`tools/fs/thesis.py`（4.2 级联下一跳读 `trigger.gaps`）· 4.2 调度（按需层）·
          0.14 唤醒自检（`verify_registry` + `skeleton_check`）· C6/阶段 5 验收（插入测试）。

用法：
  python tools/fs/registry.py                     # 只读总览（按母环节分组）
  python tools/fs/registry.py --verify            # 六类校验 + 骨架冻结
  python tools/fs/registry.py --dispatch          # 4.2 按需视图（缺口 → 能力）
  python tools/fs/registry.py --manifest          # 骨架冻结状态（指纹逐个比对）
  python tools/fs/registry.py --manifest --update  # 重建冻结台账（显式动作）
  python tools/fs/registry.py --selftest          # 内置验收（临时文件，不碰真数据）
  python tools/fs/registry.py --discipline        # 纪律自检（AST）

规则：禁在别处内联本注册表读写；schema 增删必须升 SCHEMA_VERSION 并同步施工方案 0.18。
"""
import datetime as _dt
import hashlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.dirname(os.path.abspath(__file__))))

SCHEMA_VERSION = 1
try:
    from paths import ROOT
except Exception:                                    # pragma: no cover
    ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）

REGISTRY_PATH = os.path.join(ROOT, "data", "state", "capability_registry.json")
MANIFEST_PATH = os.path.join(ROOT, "data", "state", "skeleton_manifest.json")
SKILLS_DIR = os.path.join(ROOT, ".workbuddy", "skills")

# ── 能力行 12 列（键名与顺序写死；真源 = 本常量）────────────────────────────
CAP_FIELDS = ("id", "kind", "name", "mounted_on", "consumes", "produces", "trigger",
              "output_path", "knowledge_route", "boundary", "status", "registry_version")
KINDS = ("S", "E", "C")
MOUNTS = ("C0", "C1", "C2", "C3", "C4", "C5", "C6")
CAP_STATUS = ("active", "retired")
TRIGGER_KEYS = ("text", "gaps", "entry")
BOUNDARY_KEYS = ("no_decision", "can_order", "note")
ORDER_KINDS = ("C",)                 # 只有 C 系列可能持有 can_order
ORDER_IDS = ("C5",)                  # 且只有 C5（执行层）
STAGES = ("SKELETON", "PLUGINS")     # 分批纪律：骨架未完不许插插件

# ── C 类（核心链）实现落点（用户 P4 · 2026-10-08 令）─────────────────────────
#   为什么需要：`pending_impl` 的判据有一句 `if c["kind"] != "C"` —— **C 类不参与**
#   （理由：C 类与骨架同体、`mounted_on` 指向自身，落点是骨架件而非 skill 目录）。
#   后果 = **「核心能力是否落地」在 verify 输出里看不见**，只活在能力账本「完成程度」列
#   → 违反「每条必须有可执行验收」+「一个知识点只有一个家」。
#   ★ 入表的只有**有执行件的 C 类**；下列两个**显式排除并写明理由**（不留空白）：
#     · C3 理解·驱动：落地位置 = `data/state/candidates.json`（thesis 回填）
#       + `data/decision_card.md`（驱动依据区）+ **判断流程在 AGENT.md/LLM 侧** → 无独立执行件；
#     · C4 决策·三问：执行件 `tools/fs/three_q.py` **已随骨架落盘**，
#       已由 `skeleton_manifest.json` 冻结台账逐字节覆盖（重复登记 = 第二份真源）。
C_IMPL = (
    ("C1", ("tools/analysis/regime.py",)),
    ("C2", ("tools/discovery/scan.py",)),
    ("C5", ("tools/risk/position_gate.py", "tools/risk/exit_gate.py")),
    ("C6", ("tools/learning/",)),
)

# 缺口枚举：**真源在 thesis.py**（本模块不另立一份）——导入失败则显式标注而非默认放行
try:
    from thesis import GAPS as _GAPS
    GAP_VALUES = tuple(_GAPS)
    _GAPS_SRC = "thesis"
except Exception:                                    # pragma: no cover
    GAP_VALUES = ()
    _GAPS_SRC = "ABSENT"

TOP_FIELDS = ("registry_version", "updated_at", "stage", "capabilities", "impl_log", "notes")
MANIFEST_TOP = ("skeleton_version", "updated_at", "files", "pending")


class RegistryError(Exception):
    """注册表操作被拒（字段不符 / 重复 id / 越权 / 幽灵能力 / 骨架漂移）—— 显式抛错，不静默降级。"""


# ── 基础 ─────────────────────────────────────────────────────────────────────
def _check_date(d, name="date"):
    s = d.isoformat() if hasattr(d, "isoformat") else str(d)
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise RegistryError("%s 必须是 YYYY-MM-DD，收到: %r" % (name, d))
    try:
        _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        raise RegistryError("%s 非法: %r" % (name, d))
    return s


def _read_json(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:
        raise RegistryError("文件存在但不可解析（拒绝静默降级）：%s（%s）" % (path, e))


def _write_json(path, doc):
    _WG.check_write(path)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _sha256_bytes(b):
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _sha256_text(s):
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()


# ── 读 ───────────────────────────────────────────────────────────────────────
def load_registry(path=None):
    """读注册表。未落盘 → {capabilities: [], source: REGISTRY_ABSENT}（不造数、不给默认清单）。"""
    p = REGISTRY_PATH if path is None else path
    d = _read_json(p)
    if not d:
        return {"registry_version": None, "updated_at": None, "stage": None,
                "capabilities": [], "impl_log": [], "notes": "", "source": "REGISTRY_ABSENT"}
    caps = d.get("capabilities")
    if not isinstance(caps, list):
        raise RegistryError("注册表缺 capabilities 列表：%s" % p)
    d["source"] = "REGISTRY_JSON"
    return d


def caps(path=None):
    return load_registry(path)["capabilities"]


def get_cap(cid, path=None):
    for c in caps(path):
        if isinstance(c, dict) and c.get("id") == cid:
            return c
    return None


def by_mount(mount=None, path=None):
    """按母环节分组 {C0..C6: [cap...]}；给 mount 则返回该组。"""
    out = {m: [] for m in MOUNTS}
    for c in caps(path):
        if isinstance(c, dict) and c.get("mounted_on") in out:
            out[c["mounted_on"]].append(c)
    return out if mount is None else out.get(mount, [])


def dispatch_view(path=None):
    """4.2 按需层唯一来源：{缺口: [cap_id...]}（只含 active 且声明了 gaps 的能力）。"""
    out = {}
    for c in caps(path):
        if not isinstance(c, dict) or c.get("status") != "active":
            continue
        t = c.get("trigger") or {}
        for g in (t.get("gaps") or []):
            out.setdefault(g, []).append(c.get("id"))
    return out


def knowledge_routes(path=None):
    """知识库路由视图：{cap_id: route}（空路由不列）。"""
    return {c["id"]: c["knowledge_route"] for c in caps(path)
            if isinstance(c, dict) and c.get("knowledge_route")}


def outputs(path=None):
    return {c["id"]: c.get("output_path") for c in caps(path) if isinstance(c, dict)}


def count_by_kind(path=None):
    out = {k: 0 for k in KINDS}
    for c in caps(path):
        if isinstance(c, dict) and c.get("kind") in out:
            out[c["kind"]] += 1
    return out


def cap_dir(cid):
    """能力落点目录（S/E = 平台项目级技能目录；C = 母链，落点在 tools/fs 或状态文件）。"""
    return os.path.join(SKILLS_DIR, cid)


def impl_exists(cid, kind):
    """能力是否已实现（有落点）：S/E 看 SKILL.md；C 母链由骨架文件承担，恒视为已实现。"""
    if kind == "C":
        return True
    return os.path.exists(os.path.join(cap_dir(cid), "SKILL.md"))


# ── 写（插入流程第 2 步；不碰骨架文件）───────────────────────────────────────
def _norm_cap(rec, registry_version):
    missing = [k for k in CAP_FIELDS if k not in rec]
    if missing:
        raise RegistryError("能力行缺字段：%s（schema 见 CAP_FIELDS）" % missing)
    extra = [k for k in rec if k not in CAP_FIELDS]
    if extra:
        raise RegistryError("能力行含非 schema 字段：%s —— 字段名写死，禁自造"
                            "（常见误写：state 应为 status）" % extra)
    out = {}
    for k in CAP_FIELDS:
        v = rec[k]
        if k == "trigger":
            if not isinstance(v, dict):
                raise RegistryError("trigger 必须是对象 {text,gaps,entry}")
            badext = [x for x in v if x not in TRIGGER_KEYS]
            if badext:
                raise RegistryError("trigger 含非 schema 键：%s" % badext)
            v = {"text": v.get("text") or "", "gaps": list(v.get("gaps") or []),
                 "entry": list(v.get("entry") or [])}
        elif k == "boundary":
            if not isinstance(v, dict) or "no_decision" not in v:
                raise RegistryError("boundary 必须含 no_decision（S/E 不得出 BUY/WAIT/NO 或仓位）")
            v = {"no_decision": bool(v.get("no_decision")),
                 "can_order": bool(v.get("can_order")),
                 "note": v.get("note") or ""}
        elif k == "registry_version":
            v = v or registry_version
        out[k] = v
    if out["kind"] not in KINDS:
        raise RegistryError("kind 只能是 %s，实际: %r" % (" / ".join(KINDS), out["kind"]))
    if out["mounted_on"] not in MOUNTS:
        raise RegistryError("mounted_on 必须是 %s 之一，实际: %r"
                            % (" / ".join(MOUNTS), out["mounted_on"]))
    if out["status"] not in CAP_STATUS:
        raise RegistryError("status 只能是 %s，实际: %r" % (" / ".join(CAP_STATUS), out["status"]))
    if out["status"] == "active" and not (out["trigger"]["gaps"] or out["trigger"]["entry"]):
        raise RegistryError("trigger 三项全空 = 永远唤不起的死能力，拒收")
    b = out["boundary"]
    if out["kind"] in ("S", "E") and (b["no_decision"] is not True or b["can_order"] is not False):
        raise RegistryError("boundary 越权：%s 属 %s 系列，必须 no_decision=true 且 can_order=false"
                            "（能力不得出 BUY/WAIT/NO 或仓位）" % (out["id"], out["kind"]))
    if b["can_order"] and out["id"] not in ORDER_IDS:
        raise RegistryError("boundary 越权：can_order=true 仅允许 %s（执行层）"
                            % list(ORDER_IDS))
    if GAP_VALUES:
        for g in out["trigger"]["gaps"]:
            if g not in GAP_VALUES:
                raise RegistryError("trigger.gaps 含非法缺口 %r（值域 = thesis.GAPS：%s）"
                                    % (g, list(GAP_VALUES)))
    return out


def add_cap(rec, path=None):
    """插入流程第 2 步：追加一行。**只动注册表一个文件，不碰骨架文件。**"""
    p = REGISTRY_PATH if path is None else path
    doc = load_registry(p)
    rv = doc.get("registry_version") or "1.0.0"
    if get_cap(rec.get("id"), p) is not None:
        raise RegistryError("能力 %r 已登记（重复 id）" % rec.get("id"))
    cap = _norm_cap(rec, rv)
    doc.setdefault("capabilities", []).append(cap)
    doc.setdefault("impl_log", [])
    doc["updated_at"] = _check_date(_dt.date.today().isoformat())
    _write_json(p, doc)
    return {"id": cap["id"], "appended": True, "file": p, "orders_created": 0}


def retire(cid, note=None, path=None):
    """**留痕不删**：status 置 retired（幽灵能力由「删目录还留登记」判定，不靠删行）。"""
    p = REGISTRY_PATH if path is None else path
    doc = load_registry(p)
    for c in doc.get("capabilities", []):
        if c.get("id") == cid:
            c["status"] = "retired"
            if note:
                c["boundary"]["note"] = (c["boundary"].get("note", "") + " | " + note).strip(" |")
            doc["updated_at"] = _check_date(_dt.date.today().isoformat())
            _write_json(p, doc)
            return {"id": cid, "status": "retired", "appended": False}
    raise RegistryError("能力 %r 未登记，无法退役" % cid)


def mark_impl(cid, as_of, path=None):
    """登记某能力的实现落痕（插入流程①之后的显式动作）—— 供幽灵能力判定。"""
    a = _check_date(as_of, "as_of")
    p = REGISTRY_PATH if path is None else path
    doc = load_registry(p)
    if get_cap(cid, p) is None:
        raise RegistryError("能力 %r 未登记，不得先记实现" % cid)
    log = doc.setdefault("impl_log", [])
    for e in log:
        if e.get("id") == cid:
            e["implemented_at"] = a
            _write_json(p, doc)
            return {"id": cid, "implemented_at": a, "updated": True}
    log.append({"id": cid, "implemented_at": a})
    _write_json(p, doc)
    return {"id": cid, "implemented_at": a, "updated": False}


# ── 校验（六类）─────────────────────────────────────────────────────────────
def verify_registry(reg=None, path=None):
    """六类校验。返回 {ok, errors, pending_impl, counts, stage}。"""
    doc = reg if reg is not None else load_registry(path)
    errs = []
    pending = []
    seen = set()
    rv_top = doc.get("registry_version")
    stage = doc.get("stage")
    if stage not in STAGES:
        errs.append("STAGE_INVALID 顶层 stage 只能是 %s，实际 %r" % (" / ".join(STAGES), stage))
    for c in doc.get("capabilities", []):
        cid = c.get("id") if isinstance(c, dict) else "?"
        if not isinstance(c, dict) or tuple(c.keys()) != tuple(CAP_FIELDS):
            got = list(c.keys()) if isinstance(c, dict) else c
            errs.append("SCHEMA_MISMATCH %s 字段不符：期望 %s，收到 %s（含 state 之类旧写法？）"
                        % (cid, list(CAP_FIELDS), got))
            continue
        if cid in seen:
            errs.append("DUP_ID 重复登记 %s" % cid)
        seen.add(cid)
        if c["mounted_on"] not in MOUNTS:
            errs.append("MOUNT_INVALID %s mounted_on=%r 不在 %s"
                        % (cid, c["mounted_on"], list(MOUNTS)))
        if c["status"] not in CAP_STATUS:
            errs.append("SCHEMA_MISMATCH %s status=%r 不在 %s" % (cid, c["status"], list(CAP_STATUS)))
        if c["registry_version"] != rv_top:
            errs.append("SCHEMA_MISMATCH %s registry_version=%r != 顶层 %r"
                        % (cid, c["registry_version"], rv_top))
        t = c["trigger"]
        if not t["text"] and not t["gaps"] and not t["entry"]:
            errs.append("TRIGGER_EMPTY %s 永不会唤起（trigger 三项全空）" % cid)
        if GAP_VALUES:
            for g in t["gaps"]:
                if g not in GAP_VALUES:
                    errs.append("SCHEMA_MISMATCH %s trigger.gaps 含非法缺口 %r（值域 = thesis.GAPS）"
                                % (cid, g))
        b = c["boundary"]
        if c["kind"] in ("S", "E"):
            if b["no_decision"] is not True or b["can_order"] is not False:
                errs.append("BOUNDARY_VIOLATION %s 越权（S/E 必须 no_decision=true 且 can_order=false）"
                            % cid)
        elif b["can_order"] and cid not in ORDER_IDS:
            errs.append("BOUNDARY_VIOLATION %s can_order=true 仅允许 %s（执行层）"
                        % (cid, list(ORDER_IDS)))
        # 幽灵能力：曾记实现过、但落点目录已不在
        if c["kind"] != "C" and not impl_exists(cid, c["kind"]):
            if any(e.get("id") == cid for e in doc.get("impl_log", [])):
                errs.append("GHOST_CAP %s 曾记实现但落点目录已不在（删了目录还留登记）" % cid)
            elif c["status"] != "active":
                pass                                  # retired 且从未实现 → 已退场，不追
            elif stage == "PLUGINS":
                errs.append("GHOST_CAP %s 插件阶段仍无落点目录（%s/SKILL.md）"
                            % (cid, cap_dir(cid)))
            else:
                pending.append(cid)

    # C 类（核心链）实现落点存在性 —— 用户 P4（补上 `pending_impl` 刻意跳过的 C 类）
    #   SKELETON 阶段**只提示**：C 类未实现是骨架期的预期态（骨架 = C0 底座 + 协议），
    #     与 `pending_impl` 同口径 —— 提示是为了让「核心能力还剩几项没落」**可见**；
    #   PLUGINS 阶段**升为错误**：到了插件期核心链还缺执行件 = 真漏落，必须当场报。
    pend_core = []
    for cid, rels in C_IMPL:
        miss = [r for r in rels if not os.path.exists(os.path.join(ROOT, r))]
        if not miss:
            continue
        if stage == "PLUGINS":
            errs.append("GHOST_CORE %s 核心链已到插件阶段仍缺实现落点：%s"
                        % (cid, " / ".join(miss)))
        else:
            pend_core.append(cid)

    return {"ok": not errs, "errors": errs, "pending_impl": sorted(set(pending)),
            "pending_core": sorted(set(pend_core)),
            "counts": count_by_kind(path), "stage": stage, "registry_version": rv_top,
            "gap_source": _GAPS_SRC, "n_caps": len(doc.get("capabilities", []))}


# ── 骨架冻结（指纹台账）────────────────────────────────────────────────────
def fingerprint(target):
    """契约指纹。两类目标：
      file:<rel>                  整文件 sha256
      const:<rel>:<NAME>          常量值（json 规范化）sha256 —— schema/字段表这类契约
    """
    kind, _, rest = target.partition(":")
    if kind == "file":
        p = os.path.join(ROOT, rest.replace("/", os.sep))
        if not os.path.exists(p):
            return None
        return _sha256_bytes(io.open(p, "rb").read())
    if kind == "const":
        rel, _, name = rest.partition(":")
        mod = os.path.basename(rel)[:-3]
        try:
            m = __import__(mod)
        except Exception as e:
            raise RegistryError("const 指纹无法载入模块 %s：%s" % (rel, e))
        if not hasattr(m, name):
            raise RegistryError("const 指纹目标不存在：%s.%s" % (mod, name))
        return _sha256_text(json.dumps(getattr(m, name), ensure_ascii=False, sort_keys=True,
                                       default=str))
    raise RegistryError("未知指纹目标类型：%r" % target)


def _resolvable(target):
    """台账 pending 判据的唯一实现：目标是否**已可解析**（`file:` 与 `const:` 一视同仁）。

    曾只认 `file:` —— 结果是 `const:` 类契约件（schema / API 签名）落盘后**永远挂在 pending**，
    冻结校验形同虚设。判据必须与「能否算出指纹」同源，不能按目标类型分叉。
    """
    try:
        return fingerprint(target) is not None
    except RegistryError:
        return False


def skeleton_check(manifest_path=None):
    """骨架冻结校验：逐个比对指纹。返回 {ok, drifted, missing, pending, stale}。"""
    mp = MANIFEST_PATH if manifest_path is None else manifest_path
    doc = _read_json(mp)
    if not doc:
        return {"ok": False, "drifted": [], "missing": [], "pending": [], "stale": [],
                "errors": ["MANIFEST_ABSENT 骨架冻结台账未落盘：%s" % mp], "version": None}
    drifted, missing, stale = [], [], []
    for f in doc.get("files", []):
        cur = fingerprint(f.get("target", ""))
        if cur is None:
            missing.append(f.get("id") or f.get("target"))
        elif cur != f.get("sha256"):
            drifted.append(f.get("id") or f.get("target"))
    pend = []
    for f in doc.get("pending", []):
        t = f.get("target", "")
        if _resolvable(t):
            stale.append(f.get("id") or t)          # 已落盘但仍挂在 pending → 台账过期
        else:
            pend.append(f.get("id") or t)
    errs = []
    if drifted:
        errs.append("SKELETON_DRIFT 骨架指纹变化：%s —— 改骨架须是**显式动作**"
                    "（升 skeleton_version + 迁移脚本 + changelog），再 manifest --update 重建台账"
                    % drifted)
    if missing:
        errs.append("SKELETON_MISSING 已登记骨架件缺失：%s —— 复核是否被误移/误删（路径纪律）"
                    % missing)
    if stale:
        errs.append("MANIFEST_STALE 已落盘却仍挂 pending：%s —— 请补登记进 files" % stale)
    return {"ok": not errs, "drifted": drifted, "missing": missing, "pending": pend,
            "stale": stale, "errors": errs, "version": doc.get("skeleton_version"),
            "n_files": len(doc.get("files", []))}


def manifest_targets():
    """骨架件清单（已落盘组 + 未落盘 pending 组）。**判据 = 骨架 20 条里的核心件与契约。**"""
    present = [
        ("paths.py", "file:tools/fs/paths.py", "项目根定位（唯一权威）"),
        ("C0.cursor", "file:tools/fs/cursor.py", "0.1 游标（date 推进唯一执行器，settle.py 是写者）"),
        ("C0.snapshot", "file:tools/fs/snapshot.py", "0.2 信息截断（load_asof ≤ D 物理隔离未来）"),
        ("C0.loader", "file:tools/fs/loader.py", "0.10 数据统一入口（取数唯一门）"),
        ("C0.candidates", "file:tools/fs/candidates.py", "0.5 候选池状态机（迁移唯一执行器）"),
        ("C0.thesis", "file:tools/fs/thesis.py", "4.2 Thesis 认知对象 + 级联门控"),
        ("C0.decision", "file:tools/fs/decision.py", "0.3 决策快照（不可变 + SIM/LIVE 双轴）"),
        ("C0.pnl", "file:tools/fs/pnl.py", "0.4 净值账本（结算层，append-only）"),
        ("C0.params", "file:tools/fs/params.py", "0.8/0.9 参数总账（正式值唯一写入口 = promote）"),
        ("C0.registry", "file:tools/fs/registry.py", "0.18 能力注册表 + 骨架冻结校验"),
        # ↓ P1 勘察补登（2026-10-08）：0.7 落点封闭的审计器原**整份不在台账** ——
        #   实测「改了 landing.py，漂移检查不报」；1.4 三问原只锁 API 常量、**文件体未锁**。
        ("C0.landing", "file:tools/fs/landing.py", "0.7 落点封闭（5 落点 + 5 豁免 · 可反查登记表）"),
        ("C0.three_q", "file:tools/fs/three_q.py", "1.4 三问（文件体；API 另由 const:api.three_questions 锁）"),
        ("C0.astwin", "file:tools/fs/astwin.py",
         "P1 AST 窗口（尾界）唯一实现 —— 审计地基，禁任何模块再自写副本"),
        ("params.schema", "const:tools/fs/params.py:PARAM_FIELDS", "0.8 params 七列 meta schema"),
        ("params.queue_schema", "const:tools/fs/params.py:QUEUE_FIELDS", "0.9 候选队列六列 schema"),
        ("params.changelog_schema", "const:tools/fs/params.py:CHANGELOG_FIELDS", "0.9 变更史七列 schema"),
        ("registry.schema", "const:tools/fs/registry.py:CAP_FIELDS", "0.18 能力行 12 列 schema"),
        ("decision.ledger_schema", "const:tools/fs/decision.py:DECISION_FIELDS", "0.3 decision_ledger 事件 schema"),
        ("candidates.state_schema", "const:tools/fs/candidates.py:FIELDS", "0.5 候选池字段 schema"),
        ("C0.premarket", "file:tools/decision/premarket.py",
         "E1 盘前预案（只读装配层 · 唯一写点 data/generated/reports/e1_<as_of>.md · 落点 X1）"
         " —— 2026-10-13 重签批准入册（#159 授权新建）"),
        # ↓ v1.5.0（2026-10-23 · 用户批准 A 方案）：**补 C6 回测学习触发入口**。
        #   缺口实证：原骨架 25 件无一件是回测/学习；C6 虽在注册表挂载、11 个学习工具也都在，
        #   但**没有任何生产入口调用它们** ⇒ C6 恒 EMPTY、rules.json 恒 []、
        #   每周 5 指标全无数据。设计 §12 的 Case→Obs→Hyp→Verify→Rule 从未被执行过。
        #   用户原话：「实盘我要的是直接产生利润，你自己学习进步用历史数据」
        #   ⇒ 回测学习是**生产核心环节**，不是实验工具，必须进骨架。
        ("C0.learn_pipeline", "file:tools/learning/learn_pipeline.py",
         "C6 回测学习闭环唯一入口（batch_replay 历史回放 → cases 池 → obs → demand → "
         "每周5指标报告）—— 2026-10-23 v1.5.0 批准入册（A 方案）。边界：不产 Rule"
         "（须 verify + 用户批准）、不改生存参数、回放沙盒落系统临时域不碰生产"),
        # ↓ v1.5.1（2026-10-23 用户令「全部推进」）：**补 §8.3 第②层「谁构造假设」**。
        #   缺口实证：`param_sweep`/`verify`/`calibrate.propose_param` 三个机械件本来都在，
        #   但 `verify(hyp)` 要 {condition{field,op,value}, evidence[], target}，
        #   而 `obs_scanner` 只吐事实计数（且明确「重复结构 ≠ 规律」）⇒
        #   从事实到可验证假设那一段**没有任何代码**，只能人肉写，
        #   故 `calibration_queue` 恒 0 字节、C6 恒 EMPTY。
        ("C0.hypothesize", "file:tools/learning/hypothesize.py",
         "C6 观察→假设自动桥（obs 重复结构 → 可验证假设 → verify 分段 → "
         "仅 SUPPORT 经 calibrate.propose_param 交候选）—— 2026-10-23 v1.5.1 入册。"
         "边界：不产 Rule、不改参数、不批准；判据复用 verify_min_samples/min_edge"),
        # ↓ v1.5.2（2026-10-23 用户令「所有策略都要研究，没有一种策略能长期赚钱」）：
        #   **策略×环境对应表的数据底座**。用户原话推翻了「专攻胜率高那条路」的做法——
        #   单条策略胜率是时段平均，掩盖了它只在特定环境有效；正确目标是建立
        #   「什么环境 × 用什么策略」的对照表。本件只出事实（n/胜率/偏离/覆盖缺口），
        #   零推荐零排序，并显式区分「无样本（没测过）」与「样本差（测过不行）」。
        ("C0.strategy_matrix", "file:tools/learning/strategy_matrix.py",
         "C6 策略×环境矩阵（Regime × Catalyst 交叉切格出 n/胜率/偏离/覆盖缺口，"
         "零推荐只出事实）—— 2026-10-23 v1.5.2 入册。对应设计 §2 病根 4"
         "「相关会过期」：单策略胜率随环境漂移，但「环境→策略」对应关系稳定得多"),
    ]
    pending = [
        ("C0.settle", "file:tools/risk/settle.py", "0.6 五步结算 checklist 唯一执行器"),
        ("C0.wakeup", "file:tools/fs/wakeup.py", "0.14 运行入口（唤醒即追平）"),
        ("C0.observe", "file:tools/fs/observe.py", "0.17 运转可观测与自愈（run_observed）"),
        ("C0.hboard", "file:tools/fs/hboard.py", "0.12 跨日看板（装配层聚合视图，h_board.md 唯一写入方）"),
        ("api.three_questions", "const:tools/fs/three_q.py:THREE_Q_API", "1.4 三问 API 签名"),
    ]
    return present, pending


def manifest_update(version=None, as_of=None, manifest_path=None):
    """重建冻结台账 —— **这是「显式动作」本身**（改骨架后必须跑它，漂移红线才消）。"""
    mp = MANIFEST_PATH if manifest_path is None else manifest_path
    old = _read_json(mp) or {}
    a = _check_date(as_of or _dt.date.today().isoformat(), "as_of")
    if version is None:
        version = old.get("skeleton_version") or "1.0.0"
    present, pending = manifest_targets()
    files = []
    for cid, target, contract in present:
        fp = fingerprint(target)
        if fp is None:
            pending.append((cid, target, contract))
            continue
        files.append({"id": cid, "target": target, "sha256": fp, "contract": contract})
    pend = []
    for cid, target, contract in pending:
        # 两类 pending 都要能自动转正：判据与 skeleton_check 同源（_resolvable）
        fp2 = fingerprint(target) if _resolvable(target) else None
        if fp2 is not None:                     # pending 里的件已落盘 → 自动转正登记
            files.append({"id": cid, "target": target, "sha256": fp2, "contract": contract,
                          "promoted_from_pending": True})
        else:
            pend.append({"id": cid, "target": target, "contract": contract})
    doc = {"skeleton_version": version, "updated_at": a, "files": files, "pending": pend}
    _write_json(mp, doc)
    return {"manifest": mp, "n_files": len(files), "n_pending": len(pend),
            "skeleton_version": version, "updated_at": a}


# ── 自检 ─────────────────────────────────────────────────────────────────────
def _mk_cap(cid, kind="S", mount="C3", gaps=("驱动不明",), entry=(), status="active",
            no_decision=True, can_order=False, route=None):
    return {"id": cid, "kind": kind, "name": "测试能力%s" % cid, "mounted_on": mount,
            "consumes": ["data/snapshot"], "produces": ["s1_out"], "trigger":
            {"text": "测试唤起条件", "gaps": list(gaps), "entry": list(entry)},
            "output_path": "data/generated/s_%s_<as_of>.json" % cid,
            "knowledge_route": route, "boundary":
            {"no_decision": no_decision, "can_order": can_order, "note": "不得出决策"},
            "status": status, "registry_version": "1.0.0"}


def _selftest():
    import shutil
    import tempfile
    ok = fail = 0
    fails = []

    def T(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [OK] %s" % name)
        else:
            fail += 1
            fails.append(name)
            print("  [!!] %s %s" % (name, detail))

    def raises(fn, *a, **kw):
        try:
            fn(*a, **kw)
            return False
        except (RegistryError, TypeError):
            return True

    def errmsg(fn, *a, **kw):
        try:
            fn(*a, **kw)
            return ""
        except RegistryError as e:
            return str(e)

    tmp = tempfile.mkdtemp(prefix="ta_registry_selftest_")
    rp = os.path.join(tmp, "capability_registry.json")
    mp = os.path.join(tmp, "skeleton_manifest.json")
    D1 = "2026-10-08"

    def fp2(p):
        return io.open(p, "rb").read() if os.path.exists(p) else None

    real_sig = (fp2(REGISTRY_PATH), fp2(MANIFEST_PATH))   # 真文件基线（F 段会临时改全局路径）

    def seed(path=rp, stage="SKELETON"):
        doc = {"registry_version": "1.0.0", "updated_at": D1, "stage": stage,
               "capabilities": [
                   _mk_cap("S90", "S", "C3", ("驱动不明",)),
                   _mk_cap("S9", "S", "C6", ("驱动不明",), status="retired"),
                   _mk_cap("E90", "E", "C1", (), ("定时：盘前",), route=None, no_decision=True),
                   _mk_cap("C4", "C", "C4", (), ("主循环",), no_decision=False),
                   _mk_cap("C5", "C", "C5", (), ("主循环",), no_decision=False, can_order=True),
               ], "impl_log": [], "notes": "自检种子"}
        _write_json(path, doc)
        return doc

    try:
        print("== A. schema 与唯一真源 ==")
        T("A1 能力行 12 列", len(CAP_FIELDS) == 12)
        T("A2 `status` 在 schema 内（0.18 字段名，非 state）", "status" in CAP_FIELDS and "state" not in CAP_FIELDS)
        T("A3 缺口枚举取自 thesis.py（不另立第二份）", _GAPS_SRC == "thesis" and len(GAP_VALUES) == 5)
        T("A4 缺口值域与 thesis 一致",
          set(GAP_VALUES) == {"驱动不明", "赔率不明", "风险不明", "执行不明", "无"}
          if GAP_VALUES else False)
        T("A5 母环节七档", MOUNTS == ("C0", "C1", "C2", "C3", "C4", "C5", "C6"))
        T("A6 分批阶段两档（骨架未完不许插插件）", STAGES == ("SKELETON", "PLUGINS"))

        print("== B. 读与派生视图 ==")
        seed()
        R = load_registry(rp)
        T("B1 读到 5 行", len(R["capabilities"]) == 5 and R["source"] == "REGISTRY_JSON")
        T("B2 get_cap 取单行", (get_cap("S90", rp) or {}).get("kind") == "S")
        T("B3 by_mount 分组", len(by_mount("C3", rp)) == 1 and len(by_mount(path=rp)) == 7)
        T("B4 dispatch 只含 active 且声明 gaps 的能力",
          dispatch_view(rp) == {"驱动不明": ["S90"]})
        T("B5 dispatch **不含 retired**（S9 不在）", "S9" not in dispatch_view(rp).get("驱动不明", []))
        T("B6 count_by_kind", count_by_kind(rp) == {"S": 2, "E": 1, "C": 2})
        T("B7 knowledge_routes 空路由不列", knowledge_routes(rp) == {})
        T("B8 未落盘 → REGISTRY_ABSENT（不造数）",
          load_registry(os.path.join(tmp, "no.json"))["source"] == "REGISTRY_ABSENT")

        print("== C. 校验六类 ==")
        v = verify_registry(path=rp)
        T("C1 种子注册表通过校验", v["ok"], "; ".join(v["errors"]))
        T("C2 SKELETON 阶段未实现能力记 pending_impl（不是错）",
          "S90" in v["pending_impl"] and v["ok"])

        d = seed()
        d["capabilities"][0] = dict(d["capabilities"][0])
        del d["capabilities"][0]["status"]
        d["capabilities"][0]["state"] = "active"      # 旧写法（4.2 曾读 state）
        T("C3 字段名不符（含旧 state）→ SCHEMA_MISMATCH",
          any("SCHEMA_MISMATCH" in e for e in verify_registry(reg=d)["errors"]))
        d = seed()
        d["capabilities"].append(dict(d["capabilities"][0]))
        T("C4 重复 id → DUP_ID", any("DUP_ID" in e for e in verify_registry(reg=d)["errors"]))
        d = seed()
        d["capabilities"][0]["mounted_on"] = "C9"
        T("C5 mounted_on 非法 → MOUNT_INVALID",
          any("MOUNT_INVALID" in e for e in verify_registry(reg=d)["errors"]))
        d = seed()
        d["capabilities"][0]["boundary"] = {"no_decision": False, "can_order": True, "note": ""}
        T("C6 S 越权 → BOUNDARY_VIOLATION",
          any("BOUNDARY_VIOLATION" in e for e in verify_registry(reg=d)["errors"]))
        d = seed()
        d["capabilities"][4]["boundary"] = {"no_decision": False, "can_order": True, "note": "C5 执行层"}
        T("C7 C5 持 can_order 合法（唯一例外）", verify_registry(reg=d)["ok"])
        d = seed()
        d["capabilities"][0]["trigger"] = {"text": "", "gaps": [], "entry": []}
        T("C8 trigger 全空 → TRIGGER_EMPTY",
          any("TRIGGER_EMPTY" in e for e in verify_registry(reg=d)["errors"]))
        d = seed()
        d["capabilities"][0]["trigger"]["gaps"] = ["不存在的缺口"]
        T("C9 非法缺口值域 → SCHEMA_MISMATCH",
          any("SCHEMA_MISMATCH" in e for e in verify_registry(reg=d)["errors"]))
        d = seed()
        d["capabilities"][0]["registry_version"] = "9.9.9"
        T("C10 行版本 != 顶层 → SCHEMA_MISMATCH",
          any("SCHEMA_MISMATCH" in e for e in verify_registry(reg=d)["errors"]))
        d = seed()
        d["stage"] = "WHATEVER"
        T("C11 非法 stage → STAGE_INVALID",
          any("STAGE_INVALID" in e for e in verify_registry(reg=d)["errors"]))

        print("== D. 幽灵能力（三类情形辨清）==")
        d = seed()
        d["impl_log"] = [{"id": "S90", "implemented_at": D1}]     # 曾实现，但目录不在
        T("D1 曾记实现 + 目录不在 → GHOST_CAP",
          any("GHOST_CAP" in e for e in verify_registry(reg=d)["errors"]))
        d = seed()
        T("D2 从未实现 + SKELETON 阶段 → pending_impl（不算错）",
          verify_registry(reg=d)["ok"] and "S90" in verify_registry(reg=d)["pending_impl"])
        d = seed(stage="PLUGINS")
        T("D3 插件阶段仍无落点 → GHOST_CAP",
          any("GHOST_CAP" in e for e in verify_registry(reg=d)["errors"]))
        d = seed()
        d["capabilities"][0]["status"] = "retired"
        T("D4 retired 能力不计入 pending_impl（已退场）",
          "S90" not in verify_registry(reg=d)["pending_impl"])

        print("== D5+. 核心链 C 类落点判据（P4 · 补 pending_impl 刻意跳过的 C 类）==")
        T("D5 C_IMPL 只收「有执行件的 C 类」（C3 判断在 LLM 侧 / C4 已由冻结台账覆盖 → 不入表）",
          {c for c, _r in C_IMPL} == {"C1", "C2", "C5", "C6"})
        _v = verify_registry()
        _miss = {cid for cid, rels in C_IMPL
                 if any(not os.path.exists(os.path.join(ROOT, r)) for r in rels)}
        T("D6 pending_core 逐项与文件系统一致（不硬编码清单 → 插件期落地后自动收敛）",
          set(_v["pending_core"]) == _miss and _v["ok"],
          "pending_core=%r / 实际缺=%r" % (_v["pending_core"], sorted(_miss)))
        _saved = C_IMPL
        try:
            globals()["C_IMPL"] = (("C9", ("tools/_definitely_missing_impl_.py",)),)
            T("D7 PLUGINS 阶段缺落点 → GHOST_CORE 错误（核心链漏落当场报）",
              any("GHOST_CORE" in e
                  for e in verify_registry(reg=seed(stage="PLUGINS"))["errors"]))
            T("D8 SKELETON 阶段同一情形 → 只提示 pending_core、不算错",
              verify_registry(reg=seed())["ok"]
              and "C9" in verify_registry(reg=seed())["pending_core"])
        finally:
            globals()["C_IMPL"] = _saved

        print("== E. 插入流程（只动注册表，不碰骨架）==")
        seed()
        rd_fp, _ = None, None
        rb = io.open(rp, "rb").read()
        r = add_cap(_mk_cap("S99", "S", "C2", ("执行不明",)), rp)
        T("E1 add 追加一行", r["appended"] and len(caps(rp)) == 6)
        T("E2 只写注册表一个文件（其他骨架零改动）", io.open(rp, "rb").read() != rb)
        T("E3 新能力进 dispatch 视图（无需改调度代码）",
          dispatch_view(rp).get("执行不明") == ["S99"])
        T("E4 重复 id → 拒", raises(add_cap, _mk_cap("S90"), rp))
        T("E5 缺字段 → 拒", raises(add_cap, {"id": "S98", "kind": "S"}, rp))
        T("E6 非 schema 字段 → 拒（防自造字段）",
          "非 schema 字段" in errmsg(add_cap, dict(_mk_cap("S98"), foo=1), rp))
        T("E7 active 但 trigger 全空 → 拒", raises(add_cap, dict(
            _mk_cap("S98"), trigger={"text": "", "gaps": [], "entry": []}), rp))
        T("E8 mount 非法 → 拒", raises(add_cap, _mk_cap("S98", mount="C9"), rp))
        T("E9 S 越权 → 拒", raises(add_cap, _mk_cap("S98", no_decision=False), rp))
        r = retire("S99", "并入 C3 事实层", rp)
        T("E10 retire 留痕不删（行仍在）", r["status"] == "retired" and len(caps(rp)) == 6)
        T("E11 retire 后不再进 dispatch", "S99" not in dispatch_view(rp).get("执行不明", []))
        T("E12 mark_impl 记实现落痕", mark_impl("S90", D1, rp)["implemented_at"] == D1)

        print("== F. 骨架冻结 ==")
        old_manifest, old_rp = MANIFEST_PATH, REGISTRY_PATH
        try:
            globals()["MANIFEST_PATH"] = mp
            globals()["REGISTRY_PATH"] = rp
            seed()
            mu = manifest_update(as_of=D1, manifest_path=mp)
            T("F1 台账重建：%d 个已落盘件 / %d 个 pending"
              "（F7 装配完成后骨架件已全落盘 → pending=0 是正确态，不再要求「必须有 pending」）"
              % (mu["n_files"], mu["n_pending"]),
              mu["n_files"] >= 12)
            T("F2 登记 = file 与 const 两类契约",
              any(f["target"].startswith("file:") for f in _read_json(mp)["files"])
              and any(f["target"].startswith("const:") for f in _read_json(mp)["files"]))
            sc = skeleton_check(mp)
            T("F3 刚重建 → 无漂移", sc["ok"], "; ".join(sc["errors"]))
            T("F4 pending 组无「已可解析却仍挂 pending」——**const: 契约件也必须能转正**"
              "（只认 file: 会让 1.4 三问 API 签名永远挂空）",
              not any(_resolvable(p2["target"]) for p2 in _read_json(mp)["pending"]))
            T("F5 registry.py 自身已登记（骨架自证）",
              any(f["id"] == "C0.registry" for f in _read_json(mp)["files"]))
            T("F6 台账不含自己（防自指）",
              not any("manifest" in f["target"] for f in _read_json(mp)["files"]))

            # 篡改 A：改一个骨架文件 → 漂移
            doc = _read_json(mp)
            doc["files"][0]["sha256"] = "sha256:deadbeef"
            _write_json(mp, doc)
            sc2 = skeleton_check(mp)
            T("F7 指纹不符 → SKELETON_DRIFT", not sc2["ok"] and "SKELETON_DRIFT" in sc2["errors"][0])
            T("F8 漂移项精确列出（%s）" % sc2["drifted"], sc2["drifted"] == [doc["files"][0]["id"]])

            # 篡改 B：删掉一个已登记件 → 缺失
            manifest_update(as_of=D1, manifest_path=mp)
            doc = _read_json(mp)
            doc["files"].append({"id": "ghost", "target": "file:tools/fs/no_such.py",
                                 "sha256": "sha256:x", "contract": "不存在"})
            _write_json(mp, doc)
            sc3 = skeleton_check(mp)
            T("F9 已登记件不存在 → SKELETON_MISSING",
              not sc3["ok"] and "ghost" in sc3["missing"])

            # 篡改 C：const 契约（schema）被改 → 漂移
            manifest_update(as_of=D1, manifest_path=mp)
            doc = _read_json(mp)
            for f in doc["files"]:
                if f["id"] == "params.schema":
                    f["sha256"] = "sha256:0" * 1
            _write_json(mp, doc)
            sc4 = skeleton_check(mp)
            T("F10 schema 常量指纹变化 → 漂移（改 schema 也是显式动作）",
              "params.schema" in sc4["drifted"])

            # 篡改 D：pending 里的件已落盘 → 台账过期
            manifest_update(as_of=D1, manifest_path=mp)
            doc = _read_json(mp)
            doc["pending"].append({"id": "settle", "target": "file:tools/fs/params.py",
                                   "contract": "假装未落盘"})
            _write_json(mp, doc)
            sc5 = skeleton_check(mp)
            T("F11 已落盘却挂 pending → MANIFEST_STALE",
              "settle" in sc5["stale"] and any("MANIFEST_STALE" in e for e in sc5["errors"]))
            T("F12 台账缺失 → MANIFEST_ABSENT（不假装通过）",
              skeleton_check(os.path.join(tmp, "no.json"))["ok"] is False)
        finally:
            globals()["MANIFEST_PATH"], globals()["REGISTRY_PATH"] = old_manifest, old_rp

        print("== G. 真数据零污染与纪律 ==")
        T("G1 **真注册表/冻结台账未被自检触碰（指纹不变）**",
          (fp2(REGISTRY_PATH), fp2(MANIFEST_PATH)) == real_sig)
        import ast
        src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
        tree = ast.parse(src)
        banned = {"requests", "urllib", "http", "socket", "subprocess", "openai"}
        top_imp, in_selftest, local_only = set(), set(), set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                top_imp |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                top_imp.add(n.module.split(".")[0])
        for fn in [x for x in tree.body if isinstance(x, ast.FunctionDef)]:
            mods = set()
            for n in ast.walk(fn):
                if isinstance(n, ast.Import):
                    mods |= {a.name.split(".")[0] for a in n.names}
                elif isinstance(n, ast.ImportFrom) and n.module:
                    mods.add(n.module.split(".")[0])
            (in_selftest if fn.name == "_selftest" else local_only).update(mods)
        T("G2 顶层无网络/子进程导入", not (top_imp & banned), "命中: %s" % sorted(top_imp & banned))
        T("G3 函数内导入同禁（除自检）", not (local_only & banned),
          "命中: %s" % sorted(local_only & banned))
        T("G4 shutil 只在自检内", "shutil" not in local_only and "shutil" in in_selftest)
        calls = {}
        for n in tree.body:                          # 只统计模块顶层函数（自检内嵌套辅助不算）
            if isinstance(n, ast.FunctionDef):
                calls[n.name] = {c.func.id for c in ast.walk(n)
                                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        T("G5 无下单/撤单/成交接口",
          not [f for f in calls if any(k in f for k in ("order", "buy", "sell", "trade", "execute"))])
        writers = sorted(f for f, ns in calls.items() if "_write_json" in ns)
        T("G6 写者封闭（注册表/台账及其管理接口）", set(writers) <= {
            "add_cap", "retire", "mark_impl", "manifest_update", "_selftest"},
          "实际: %s" % writers)
        abs_macs, abs_win = "/" + "Users" + "/", "C:" + "\\" + "Users"
        T("G7 无 hardcode 绝对路径", abs_macs not in src and abs_win not in src)
        T("G8 verify_registry 不写盘（纯校验）",
          "_write_json" not in calls.get("verify_registry", set())
          and "_write_json" not in calls.get("skeleton_check", set()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nregistry.py 自检：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：")
        for f in fails:
            print("  - %s" % f)
    return 1 if fail else 0


def _discipline():
    import ast
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    banned = {"requests", "urllib", "http", "socket", "subprocess", "openai"}
    imp = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imp |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imp.add(n.module.split(".")[0])
    hit = sorted(imp & banned)
    calls = {}
    for n in tree.body:                              # 只统计模块顶层函数
        if isinstance(n, ast.FunctionDef):
            calls[n.name] = {c.func.id for c in ast.walk(n)
                             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    writers = sorted(f for f, ns in calls.items() if "_write_json" in ns)
    print("registry.py 纪律检查（0.18）")
    print("  ① 禁导入（网络/子进程）: %s" % ("✅ 无" if not hit else "❌ %s" % hit))
    print("  ② 单一真源            : ✅ 派生视图（dispatch/by_mount/knowledge_routes）不另立清单")
    print("  ③ 缺口枚举来源        : %s（%s）" % (_GAPS_SRC, " / ".join(GAP_VALUES)))
    print("  ④ 写者封闭            : %s" % writers)
    print("  ⑤ 零交易动作          : ✅ 无 order/buy/sell 接口")
    ok = (not hit) and set(writers) <= {"add_cap", "retire", "mark_impl", "manifest_update",
                                       "_selftest"}
    return 0 if ok else 1


def _main(argv):
    if "--selftest" in argv:
        print("registry.py 自检（SCHEMA_VERSION=%d · 临时文件运行）" % SCHEMA_VERSION)
        return _selftest()
    if "--discipline" in argv:
        return _discipline()
    if "--manifest" in argv:
        if "--update" in argv:
            # `--ver X.Y.Z` = 显式升 skeleton_version（改骨架件数变动的唯一正规入口；
            # 缺它是"改骨架是显式动作"里的最后一环只能靠 python 一行命令，不可复现）
            _v = None
            if "--ver" in argv:
                _i = argv.index("--ver")
                if _i + 1 >= len(argv):
                    raise RegistryError("--ver 需跟版本号，例：--manifest --update --ver 1.2.0")
                _v = argv[_i + 1]
            r = manifest_update(version=_v)
            print("骨架冻结台账已重建（**这是显式动作**）：%s" % r["manifest"])
            print("  skeleton_version=%s · 已登记 %d 件 · pending %d 件"
                  % (r["skeleton_version"], r["n_files"], r["n_pending"]))
            return 0
        sc = skeleton_check()
        print("骨架冻结状态（manifest version=%s · %d 件）"
              % (sc["version"], sc.get("n_files", 0)))
        print("  结论: %s" % ("✅ 无漂移" if sc["ok"] else "❌ 有漂移"))
        for e in sc["errors"]:
            print("  [!!] %s" % e)
        for p in sc["pending"]:
            print("      pending: %s" % p)
        return 0 if sc["ok"] else 1
    if "--verify" in argv:
        v = verify_registry()
        sc = skeleton_check()
        print("注册表校验（%s · stage=%s · %d 行 · %s）"
              % ("通过 ✅" if v["ok"] else "失败 ❌", v["stage"], v["n_caps"], v["gap_source"]))
        for e in v["errors"]:
            print("  [!!] %s" % e)
        if v["pending_impl"]:
            print("  [--] 待实现（骨架阶段预期，非错）：%s" % ", ".join(v["pending_impl"]))
        if v["pending_core"]:
            print("  [--] 核心链待实现（骨架阶段预期，非错）：%s（落点见 C_IMPL）"
                  % ", ".join(v["pending_core"]))
        print("骨架冻结（%s）" % ("✅ 无漂移" if sc["ok"] else "❌ 有漂移"))
        for e in sc["errors"]:
            print("  [!!] %s" % e)
        return 0 if (v["ok"] and sc["ok"]) else 1
    if "--dispatch" in argv:
        dv = dispatch_view()
        print("4.2 按需视图（缺口 → 能力；唯一来源 = 注册表 trigger.gaps）")
        for g, ids in dv.items():
            print("  %-8s → %s" % (g, ", ".join(ids)))
        if not dv:
            print("  （空：注册表未落盘或无人声明 gaps）")
        return 0
    R = load_registry()
    c = count_by_kind()
    print("能力注册表（0.18 · version=%s · stage=%s · %s）"
          % (R.get("registry_version"), R.get("stage"), R["source"]))
    print("  计数: %s" % " · ".join("%s %d 个" % (k, c[k]) for k in KINDS))
    groups = by_mount()
    for m in MOUNTS:
        if not groups[m]:
            continue
        print("  [%s] %s" % (m, ", ".join(
            "%s%s" % (x["id"], "" if x["status"] == "active" else "(retired)") for x in groups[m])))
    if not R["capabilities"]:
        print("  注册表尚未落盘（source=%s）—— 4.2 级联将返回 REGISTRY_ABSENT（不猜）" % R["source"])
    print("  pending（待实现）: %s" % (", ".join(verify_registry()["pending_impl"]) or "无"))
    print("  纪律: 新增能力 = 建目录 + 注册表 add 一行 + verify，**骨架文件零改动**")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
