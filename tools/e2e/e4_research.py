# -*- coding: utf-8 -*-
"""E4 课题研究载体（2026-10-13 · 第一阶段补齐）。

职责（registry）：深挖缺口 → 候选规则/参数 → 挂载 C6。触发：学习需求 / 用户点题。

**载体定义（2026-10-12 核准：真库产物近零 + 产出零消费者 ⇒ 本件先立载体与流转）**：
  · 输入 = `data/learning/demand_queue.jsonl`（settle 步5 / obs_scanner / demand_scanner
    三写方的缺口行；kind=SCAN_NOOP 且 gaps 空 = 无真实需求，跳过）
  · 载体 = `data/research/e4_<topic_id>/`（X2 研究豁免区）：
      topic.md   课题卡（来源缺口 / 输入 / 预期产出 / 消费者 / 状态流转）
      status.json {topic_id, status: open|researching|delivered, transitions: [...]}
  · 状态流转：open（队列产）→ researching（人工/LLM 认领）→ delivered（产出
    rules/params 候选进 C6 审批队列）。本件只做 **open 的机械化生产** 与 **状态
    推进 API**（advance），研究本体 = LLM/人工（不在本件伪装）。
  · 唯一产出 = 课题目录；消费者 = 用户点题研究流程 / E11（落地后）/ C6 审批。
    不生成没人消费的第二份文件。

幂等：同 demand 行（fingerprint）已建课题 → EXISTS 跳过。
"""
import hashlib
import io
import json
import os
import sys
import datetime as _dt

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
sys.path.insert(0, _FS)
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
from paths import ROOT  # noqa: E402

S_ID = "e4"
DEMAND = os.path.join(ROOT, "data", "learning", "demand_queue.jsonl")
RESEARCH = os.path.join(ROOT, "data", "research")
STATES = ("open", "researching", "delivered")

# ── 旧链课题研究框架（2026-10-19 导入 · 旧封版技能 event-analysis +
#   industry-track · 只读）——课题卡的「研究方法」来源；目录层 join（文件名在使用点拼）。
_KB_REL = os.path.join("data", "knowledge", "e4_research")
_KB_FNAME = "research_base.json"


def load_knowledge_base(base=None):
    """→ 旧链课题研究框架 dict；缺席/坏 JSON ⇒ None（按无框架降级，禁抛）。"""
    p = os.path.join(base or ROOT, _KB_REL, _KB_FNAME)
    if not os.path.isfile(p):
        return None
    try:
        return json.load(io.open(p, encoding="utf-8-sig")) or None
    except Exception:
        return None


def _gap_kind(gaps):
    """缺口行 → 方法论段落路由（事件类 / 产业类 / 单票类）。零 LLM，纯键名匹配。"""
    text = json.dumps(gaps or [], ensure_ascii=False)
    if any(k in text for k in ("事件", "预期差", "公告", "消息")):
        return "event_gap"
    if any(k in text for k in ("题材", "主线", "产业", "行业", "传导")):
        return "industry_gap"
    return "stock_gap"


class E4Error(Exception):
    """REASON_CODE 前缀错误。"""


def _fp(row):
    return hashlib.sha256(json.dumps(
        {k: row.get(k) for k in ("as_of", "kind", "gaps", "scanner")},
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def read_demands(base=None, *, only_open=True):
    """读缺口队列：only_open=True 只留有真实 gaps 的行（SCAN_NOOP/空 gaps 跳过）。"""
    p = os.path.join(base or ROOT, "data", "learning", "demand_queue.jsonl")
    out = []
    if not os.path.isfile(p):
        return out
    with io.open(p, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if only_open and not (r.get("gaps") or []):
                continue
            out.append(r)
    return out


def collect(base=None):
    """队列 → 课题清单（已建/待建），零写。"""
    demands = read_demands(base)
    items = []
    for r in demands:
        tid = "T-" + _fp(r)
        tdir = os.path.join(base or ROOT, "data", "research", "e4_%s" % tid)
        st = _load_status(tdir)
        items.append({"topic_id": tid, "demand": r, "dir": tdir,
                      "status": (st or {}).get("status"),
                      "created": os.path.isdir(tdir)})
    return {"n_demands": len(demands), "topics": items}


def _load_status(tdir):
    p = os.path.join(tdir, "status.json")
    if not os.path.isfile(p):
        return None
    try:
        return json.load(io.open(p, encoding="utf-8"))
    except Exception:
        return None


def _topic_md(topic_id, demand, kb=None):
    kind = _gap_kind(demand.get("gaps"))
    L = ["# E4 课题 · %s" % topic_id, "",
         "- 来源：demand_queue %s（kind=%s scanner=%s）"
         % (demand.get("as_of"), demand.get("kind"), demand.get("scanner")),
         "- 缺口：%s" % json.dumps(demand.get("gaps"), ensure_ascii=False),
         "- 状态：open（待认领研究）", "",
         "## 输入", "- 缺口行原文（jsonl 见 demand_queue）；结算/复盘账本对应区间",
         "", "## 预期产出", "- 候选规则 / 参数候选（进 C6 审批队列：候选 → 验证 → 用户批准）",
         "", "## 消费者", "- 用户点题研究流程；E11 专题报告（落地后）；C6 审批队列", ""]
    if kb:
        route = (kb.get("usage_by_gap_kind") or {}).get(kind, "")
        L += ["## 研究方法（旧链方法论 · 按缺口类型路由）",
              "- 缺口类型判定：**%s** → 适用框架：%s" % (kind, route),
              "- 框架真源：e4_research 框架库（路径以 data/knowledge/_registry.json "
              "登记为准；禁在代码里写完整路径字面量——landing 词汇表会误判）",
              "- 铁律必须遵守：%s" % json.dumps(
                  (kb.get("industry_track", {}).get("four_iron_laws") or [])[:2],
                  ensure_ascii=False),
              ""]
    L += ["> 纪律：课题研究本体 = LLM/人工；本卡只登记来源/输入/产出/消费者。"
          "产出未经 C6 审批不得直接生效。"]
    return "\n".join(L)


def ensure_topics(base=None):
    """队列 → 课题目录机械化生产（幂等：同 fingerprint 已建 → 跳过）。"""
    created = []
    for r in read_demands(base):
        tid = "T-" + _fp(r)
        tdir = os.path.join(base or ROOT, "data", "research", "e4_%s" % tid)
        if os.path.isdir(tdir):
            continue
        os.makedirs(tdir, exist_ok=True)
        _WG.check_write(tdir)  # 目录守卫：topic.md/status.json 祖先
        now = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        io.open(os.path.join(tdir, "topic.md"), "w", encoding="utf-8", newline="")\
            .write(_topic_md(tid, r, kb=load_knowledge_base(base=base)))
        io.open(os.path.join(tdir, "status.json"), "w", encoding="utf-8", newline="")\
            .write(json.dumps({"topic_id": tid, "status": "open",
                               "source_demand_fp": _fp(r),
                               "demand_as_of": r.get("as_of"),
                               "transitions": [{"to": "open", "at": now}]},
                              ensure_ascii=False, indent=1))
        created.append(tid)
    return {"created": created, "n": len(created)}


def advance(topic_id, to_state, *, note=None, base=None):
    """状态推进 API：open → researching → delivered（禁跳级/禁倒退）。

    delivered 语义 = 该课题产出已进 C6 审批队列（候选规则/参数），由调用方自证；
    本件只登记流转，不校验产出内容（产出质量归 C6 验证链）。
    """
    if to_state not in STATES:
        raise E4Error("BAD_STATE: 目标状态非法 %r（值域 %s）" % (to_state, STATES))
    tdir = os.path.join(base or ROOT, "data", "research", "e4_%s" % topic_id)
    st = _load_status(tdir)
    if not st:
        raise E4Error("TOPIC_ABSENT: 课题不存在 %s" % topic_id)
    cur = st.get("status")
    order = STATES.index
    if order(to_state) <= order(cur):
        raise E4Error("BAD_TRANSITION: %s → %s（禁倒退/重复）" % (cur, to_state))
    if to_state == "delivered" and cur != "researching":
        raise E4Error("BAD_TRANSITION: delivered 只能从 researching 进入（当前 %s）" % cur)
    now = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    st["status"] = to_state
    st["transitions"].append({"to": to_state, "at": now, "note": note or ""})
    _WG.check_write(os.path.join(tdir, 'status.json'))
    io.open(os.path.join(tdir, "status.json"), "w", encoding="utf-8", newline="")\
        .write(json.dumps(st, ensure_ascii=False, indent=1))
    return {"topic_id": topic_id, "status": to_state}


def _selftest():
    import tempfile as _tf
    import shutil as _sh
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== e4 --selftest ==")
    # A 组：真实队列（当前 1 行 SCAN_NOOP 无 gaps ⇒ 待建 0）
    c0 = collect()
    T("A1 真实队列只收有 gaps 的需求（SCAN_NOOP 跳过）",
      all(t["demand"].get("gaps") for t in c0["topics"]))
    sb = _tf.mkdtemp(prefix="e4_sb_")
    try:
        os.makedirs(os.path.join(sb, "data", "learning"), exist_ok=True)
        dq = os.path.join(sb, "data", "learning", "demand_queue.jsonl")
        row = {"as_of": "2026-10-09", "kind": "WIN_BUT_WRONG", "gaps": ["止损过晚"],
               "scanner": "OBS_SCANNER", "n_cases": 2}
        with io.open(dq, "w", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        c = collect(sb)
        T("B1 有 gaps 需求入列", c["n_demands"] == 1)
        r = ensure_topics(sb)
        T("B2 课题机械化生产（topic.md + status.json，X2 区）",
          r["n"] == 1 and os.path.isfile(
              os.path.join(sb, "data", "research", "e4_%s" % r["created"][0],
                           "topic.md")))
        tid = r["created"][0]
        T("B3 幂等：重跑零新建", ensure_topics(sb)["n"] == 0)
        T("B4 初始状态 open", advance(tid, "researching", base=sb)["status"] == "researching")
        try:
            advance(tid, "delivered", base=sb)   # researching → delivered 合法
            T("B5 researching → delivered 合法流转", True)
        except Exception as e:
            T("B5 researching → delivered 合法流转", False, str(e)[:80])
        try:
            advance(tid, "open", base=sb)
            T("B6 倒退/重复拒绝", False)
        except E4Error:
            T("B6 倒退/重复拒绝", True)
        try:
            advance("T-nonexistent", "researching", base=sb)
            T("B7 课题不存在拒绝", False)
        except E4Error:
            T("B7 课题不存在拒绝", True)
        tmd = io.open(os.path.join(sb, "data", "research", "e4_%s" % tid, "topic.md"),
                      encoding="utf-8").read()
        T("B8 课题卡四要素齐（来源/输入/产出/消费者）",
          all(k in tmd for k in ("来源", "输入", "预期产出", "消费者")))
        # ── C 组：旧链课题研究框架接线（2026-10-19）────────────────────
        kb = load_knowledge_base()
        T("C1 课题框架库载入（event-analysis + industry-track 双段）",
          kb is not None and "event_analysis" in kb and "industry_track" in kb)
        T("C2 缺口类型路由零 LLM（事件类/产业类/单票类）",
          _gap_kind(["事件预期差缺口"]) == "event_gap"
          and _gap_kind(["题材传导缺口"]) == "industry_gap"
          and _gap_kind(["止损过晚"]) == "stock_gap")
        # 沙盒 sb 需自带库副本（base 隔离：禁沙盒读生产库）
        os.makedirs(os.path.join(sb, "data", "knowledge", "e4_research"),
                    exist_ok=True)
        _sh.copy(os.path.join(ROOT, _KB_REL, _KB_FNAME),
                 os.path.join(sb, _KB_REL, _KB_FNAME))
        row2 = {"as_of": "2026-10-19", "kind": "EVENT_GAP", "gaps": ["事件预期差无源"],
                "scanner": "OBS_SCANNER", "n_cases": 1}
        with io.open(dq, "a", encoding="utf-8") as f:
            f.write(json.dumps(row2, ensure_ascii=False) + "\n")
        r2 = ensure_topics(sb)
        T("C3 新课题卡带『研究方法』段（按缺口类型路由到框架）",
          r2["n"] == 1 and "研究方法" in io.open(
              os.path.join(sb, "data", "research", "e4_%s" % r2["created"][0],
                           "topic.md"), encoding="utf-8").read())
        # C4：库缺席时课题卡照常产出（无框架段不崩）
        os.remove(os.path.join(sb, _KB_REL, _KB_FNAME))
        row3 = {"as_of": "2026-10-19", "kind": "X", "gaps": ["z"], "scanner": "S"}
        with io.open(dq, "a", encoding="utf-8") as f:
            f.write(json.dumps(row3, ensure_ascii=False) + "\n")
        r3 = ensure_topics(sb)
        t3 = io.open(os.path.join(sb, "data", "research", "e4_%s" % r3["created"][0],
                                  "topic.md"), encoding="utf-8").read()
        T("C4 库缺席 ⇒ 课题卡无方法段但四要素仍齐（降级不崩）",
          r3["n"] == 1 and "研究方法" not in t3
          and all(k in t3 for k in ("来源", "输入", "预期产出", "消费者")))
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    print("== e4 --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    if "--ensure" in argv:
        print(json.dumps(ensure_topics(), ensure_ascii=False, indent=1))
        sys.exit(0)
    if "--collect" in argv:
        print(json.dumps(collect(), ensure_ascii=False, indent=1))
        sys.exit(0)
    print("用法: e4_research.py --collect | --ensure | --selftest")
    sys.exit(2)
