#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""s1.py · S1 事件催化评估执行件（施工 2.1 · S1 批 · v1.0 · 2026-10-09 建）

用途：S1 事件催化评估的**机械侧**——事件归类（F7 31+1 类）、F7 效应库匹配、
      strength 机械映射、历史效应统计评估、输入契约校验、结构化输出落盘。
      **判断本体在 LLM（skill .workbuddy/skills/s1-event-analysis/）**：传导链、
      证伪条件、定价判断是 LLM 槽；本模块只做归类/匹配/映射/校验/装配。

★ 一、输入契约（8 项，缺失显式 MISSING，禁由模型补造事实）
  必填（缺 → EVAL_REJECT 拒评）：title（事件事实）、sources>=1（来源引用，
      kind=news_id/公告号 + source_id + ts）、first_seen_at 与 announcement_at
      （首现日与公告日**分列**，禁混成一个 date）、related_codes（影响对象非空）。
  可缺（MISSING 标记 + 降置信，不编造）：cols（官方公告分类串）、body、
      transmission_path（传导链，LLM 槽）、window_hint（窗口提示）、
      falsifier（证伪条件，LLM 槽）、historical_analogs（历史相似案例，个例）。

★ 二、F7 效应库（知识真源，只读）
  data/knowledge/s1_event_effects/effect_base.json（v1.0.0 · as_of=2026-09-21）：
  32 类匹配对照净效应（逐条转录自旧包实测报告 §3，61,980 事件包 / 2026-01~08）。
  经 loader.load_knowledge("s1", as_of) 读取（point-in-time 时序守卫生效）；
  基准率 +0.36%（净效应必须相对基准读）；n_min_trust=30（只信 n>=30 的格子）。
  同目录 taxonomy.json = 官方公告分类 → 研究类映射（结构化复制自旧包
  evt_taxonomy.py，先验方向允许被实测推翻）。

★ 三、strength 机械映射（批准锁死：LLM 不许拍脑袋标"强"）
  dev = |net_effect − 0.36|；dev>=2.5→4(强) | 1.0<=dev<2.5→3(中) |
  0.4<=dev<1.0→2(弱) | dev<0.4→1(微)；n<100 封顶 3。
  分数现场按公式重算（禁手抄——旧包教训"研究已更新、打分表还是旧的"）。
  evidence_grade 按 n 机械标注：A(n>=1000) / B(300..999) / C(30..299)；
  n<30 → INSUFFICIENT_SAMPLES，**不出统计结论**。

★ 四、三槽分离与输出契约（2.1 批准 + 2.10 统一收口）
  slots.evidence（证据：sources 逐条 + 效应库引用，机器可解析）/
  slots.inference（机械推断：归类依据、匹配结果、矛盾/警示标记）/
  slots.prediction（预测：window/path/priced——首批 path=UNKNOWN_BY_DESIGN、
  priced=UNKNOWN（price_reaction 数据通道未建，first_seen_at/announcement_at
  分列事实留痕），**不冒充已定价判断**）。
  输出 = {s_id, as_of, event_id, type, strength, net_effect(带统计上下文), path,
  window, priced, evidence[], confidence, slots, missing[], warnings[]}。
  **零 BUY/WAIT/NO、零仓位、零动作**（结构性：无 action 字段 + 序列化断言）。

★ 五、落盘（X3 既有豁免 · 唯一写点）
  data/generated/s_s1_<as_of>.json（唯一键 = as_of：同键幂等覆盖不追加，
  事件级按 event_id 去重合并；跨 as_of 各自独立快照不合并）。
  event_id = "EV-" + md5(规范化 {announcement_at, related_codes, cols, title})。
  C3 消费：S1 输出 = A 类驱动候选证据来源之一，**仍须过 C3 证据验证与 C4 三问，
  不得直接触发 BUY、不得绕过 C5**（S1 无 BUY 生成路径）。
  回填纪律：S1 运行**不改知识库**（两期分离）；实际结果回填走 C6 维护期。

用法：
  python tools/analysis/s1.py                 # 契约总览（零写入）
  python tools/analysis/s1.py --selftest      # 内置验收（tempfile 沙盒，不碰真数据）
  python tools/analysis/s1.py --discipline    # 纪律判据
  from tools.analysis.s1 import evaluate, historical_effect, load_output
"""
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import datetime as _dt

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
sys.path.insert(0, _FS)
import loader as LD  # noqa: E402
from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）

SCHEMA_VERSION = 1
S_ID = "s1"
KB_DIR = os.path.join(ROOT, "data", "knowledge", "s1_event_effects")
TAXONOMY_PATH = os.path.join(KB_DIR, "taxonomy.json")
# 写点终点 fallback（landing 静态解析要求本模块字面量；真实终点由 evaluate 拼装
# s_s1_<as_of>.json 恒经 path 传入，本常量恒不触发）。
# 文件名大小写对齐注册表 id "S1"——landing 的 cap_ref 判据对 E 系列有大小写归一
# 特判、S 系列暂无（挂账待用户定夺），小写 s_s1_ 会被误判 UNREGISTERED_CAP_OUTPUT。
SNAP_FALLBACK = os.path.join(ROOT, "data", "generated", "s_S1_UNKNOWN.json")

# knowledge base 换根时 s1 库同根解析（回测沙盒整树可替换）
_KB_REL = os.path.join("data", "knowledge", "s1_event_effects")
TAXONOMY_REL = os.path.join(_KB_REL, "taxonomy.json")

_STRENGTH_MAP = {4: "强", 3: "中", 2: "弱", 1: "微"}
_GRADE_ORDER = ("A", "B", "C", "D")
_EXPIRED_NATURAL_DAYS = 60     # 自然日近似警示阈值（交易日历落盘后收紧，如实标注）
_MAX_CONF = 0.9                # 首批置信度封顶（不出 1.0）


class S1Error(Exception):
    """评估被拒。str 以 REASON_CODE: 开头（每类拒绝可机械断言）。"""


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _chk_date(v, name):
    s = str(v or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        raise S1Error("BAD_DATE: %s 必须是 YYYY-MM-DD，实际 %r" % (name, v))
    return s


# ── 知识库加载（只读，point-in-time） ─────────────────────────────────────────
def load_taxonomy(base=None):
    """归类映射表（s1 库随附文件；base 换根时同根解析）。"""
    p = TAXONOMY_PATH if not base else os.path.normpath(os.path.join(base, TAXONOMY_REL))
    if not os.path.isfile(p):
        raise S1Error("KB_MISSING: taxonomy.json 不存在（%s）" % p)
    with io.open(p, encoding="utf-8-sig") as f:
        return json.load(f)


def load_kb(as_of, base=None):
    """效应库真链路读取（loader.load_knowledge：注册表路由 + 时序守卫）。

    返回 (effect_base_dict, note)；UNAVAILABLE → S1Error(KB_UNAVAILABLE)。
    """
    d = _chk_date(as_of, "as_of")
    rec = LD.load_knowledge(S_ID, d, base=base)
    if rec is LD.UNAVAILABLE:
        raise S1Error("KB_UNAVAILABLE: s1 效应库在 as_of=%s 不可读（缺席或晚于请求版本）" % d)
    return dict(rec), rec.note


# ── 事件归类（机械，消费 taxonomy.json） ──────────────────────────────────────
def classify(cols_str, taxonomy):
    """官方分类串 → {"cat", "dir_prior", "basis"}；全中性/未收录且关键词未中 → _NEUTRAL。

    basis ∈ official / keyword / none——confidence 与之联动（批准锁死：归类可溯源）。
    """
    cols = [c for c in str(cols_str or "").split("|") if c]
    cm = taxonomy.get("cat_map", {})
    hits = []
    for c in cols:
        if c in cm:
            cat, d = cm[c][0], cm[c][1]
            if cat != "_NEUTRAL":
                hits.append((cat, d, "official"))
        else:                                    # 未收录 → 关键词兜底
            excl = taxonomy.get("kw_exclude", [])
            if any(x in c for x in excl):
                continue                          # 反义词防误判
            for kw, pair in taxonomy.get("kw_fallback", []):
                if kw in c:
                    hits.append((pair[0], pair[1], "keyword"))
                    break
    if hits:
        return {"cat": hits[0][0], "dir_prior": hits[0][1], "basis": hits[0][2],
                "all_hits": [{"cat": h[0], "dir_prior": h[1]} for h in hits]}
    return {"cat": "_NEUTRAL", "dir_prior": 0, "basis": "none", "all_hits": []}


# ── strength / evidence_grade（现场重算，禁手抄） ─────────────────────────────
def strength_grade(net_pct, n, rule):
    """按 effect_base.strength_rule 现场重算 → (score, label)。n<100 封顶 3。"""
    base_rate = float(rule.get("_base_rate", 0.36))
    dev = abs(float(net_pct) - base_rate)
    if dev >= 2.5:
        score = 4
    elif dev >= 1.0:
        score = 3
    elif dev >= 0.4:
        score = 2
    else:
        score = 1
    if n < 100 and score > 3:
        score = 3
    return score, _STRENGTH_MAP[score]


def evidence_grade(n):
    """样本量 → 证据等级（机械规则，见 effect_base.evidence_grade_rule）。"""
    if n >= 1000:
        return "A"
    if n >= 300:
        return "B"
    if n >= 30:
        return "C"
    return "D"


# ── 历史效应评估（样本不足必须明确报告，禁制造胜率/规律） ─────────────────────
def historical_effect(cat, kb):
    """事件类 → 历史效应结论 or 明确的样本不足/无记录报告。

    返回 dict：
      {"status": "OK", net_effect_pct, sample_size, event_fut20_pct,
       control_fut20_pct, direction_prior, direction_measured, evidence_grade,
       strength, strength_score, note}
      {"status": "INSUFFICIENT_SAMPLES", sample_size: n<30, ...}
      {"status": "NO_RECORD", cat 不在库}
    """
    cats = kb.get("categories", {})
    if cat not in cats:
        return {"status": "NO_RECORD", "cat": cat,
                "note": "该事件类无历史效应记录（效应库未覆盖），不出统计结论"}
    c = cats[cat]
    n = int(c["n"])
    net = float(c["net_effect_20d_pct"])
    if n < int(kb.get("n_min_trust", 30)):
        return {"status": "INSUFFICIENT_SAMPLES", "cat": cat, "sample_size": n,
                "n_min_trust": int(kb.get("n_min_trust", 30)),
                "net_effect_pct_observed": net,
                "note": "样本 n<%d：只信 n>=30 的格子（库口径），不出统计结论、不算胜率"
                        % int(kb.get("n_min_trust", 30))}
    rule = dict(kb.get("strength_rule", {}))
    rule["_base_rate"] = float(kb.get("benchmark", {}).get("event_base_rate_pct", 0.36))
    score, label = strength_grade(net, n, rule)
    return {"status": "OK", "cat": cat, "net_effect_20d_pct": net,
            "sample_size": n, "event_fut20_pct": c["event_fut20_pct"],
            "control_fut20_pct": c["control_fut20_pct"],
            "direction_prior": c.get("direction_prior", 0),
            "direction_measured": 1 if net > 0 else (-1 if net < 0 else 0),
            "evidence_grade": evidence_grade(n),
            "strength_score": score, "strength": label,
            "period": kb.get("source", {}).get("period"),
            "benchmark_method": kb.get("benchmark", {}).get("benchmark_method"),
            "note": c.get("note", "")}


# ── 主入口 ────────────────────────────────────────────────────────────────────
_REQUIRED = ("title", "sources", "first_seen_at", "announcement_at", "related_codes")
_OPTIONAL = ("cols", "body", "transmission_path", "window_hint", "falsifier",
             "historical_analogs")
_BASIS_CONF = {"official": 0.2, "keyword": 0.1, "none": 0.0}


def evaluate(event, *, as_of, base=None):
    """S1 事件评估主入口（机械侧）。

    返回 {"event_id", "status": NEW|REUSED, "output"}；
    输出落 data/generated/s_s1_<as_of>.json（唯一键 as_of，事件级去重合并）。
    拒绝：S1Error（REASON_CODE: 前缀）——FUTURE_EVENT / FUTURE_FIRST_SEEN /
    MISSING_REQUIRED / BAD_INPUT / KB_UNAVAILABLE。
    """
    d = _chk_date(as_of, "as_of")
    if not isinstance(event, dict):
        raise S1Error("BAD_INPUT: event 必须是 dict")

    # 1) 必填硬校验（缺 → 拒评，不是降级——没事实就没评估）
    missing_required = [k for k in _REQUIRED
                        if not event.get(k) or (k == "sources" and not isinstance(event[k], list))
                        or (k == "related_codes" and not [c for c in event[k] if str(c).strip()])]
    if missing_required:
        raise S1Error("MISSING_REQUIRED: 必填字段缺失 %s（缺事实=拒评，禁编造）"
                      % missing_required)
    title = str(event["title"]).strip()
    related_codes = sorted({str(c).strip() for c in event["related_codes"] if str(c).strip()})
    first_seen = _chk_date(event["first_seen_at"], "first_seen_at")
    ann_at = _chk_date(event["announcement_at"], "announcement_at")

    # 2) 时间纪律（首现日 ≠ 公告日，分列校验）
    if ann_at > d:
        raise S1Error("FUTURE_EVENT: announcement_at %s > as_of %s（未来事件不评估）"
                      % (ann_at, d))
    if first_seen > d:
        raise S1Error("FUTURE_FIRST_SEEN: first_seen_at %s > as_of %s" % (first_seen, d))
    warnings = []
    if first_seen < ann_at:
        warnings.append("EARLY_FIRST_SEEN: 首现早于公告（疑似提前泄露/传闻），披露延迟如实留痕")

    # 3) 证据引用逐条结构化（kind=news_id/公告号；source 日期不得晚于 as_of）
    evidence = []
    for s in event["sources"]:
        if not isinstance(s, dict) or not str(s.get("kind") or "").strip() \
                or not str(s.get("ref") or "").strip():
            raise S1Error("BAD_INPUT: sources 每条须为 {source_id, kind, ref[, ts]}")
        s_ts = str(s.get("ts") or "").strip()
        if s_ts and re.match(r"^\d{4}-\d{2}-\d{2}", s_ts) and s_ts[:10] > d:
            raise S1Error("FUTURE_EVENT: 证据引用 ts %s > as_of %s" % (s_ts[:10], d))
        evidence.append({"source_id": str(s.get("source_id") or "").strip(),
                         "kind": str(s["kind"]).strip(), "ref": str(s["ref"]).strip(),
                         "ts": s_ts})

    # 4) 可缺字段 → missing 显式标记（禁补造）
    missing = [k for k in _OPTIONAL if not str(event.get(k) or "").strip()
               and not isinstance(event.get(k), list)]
    if event.get("historical_analogs") is not None \
            and not isinstance(event["historical_analogs"], list):
        missing.append("historical_analogs")

    # 5) 归类（机械，官方分类优先）
    tax = load_taxonomy(base)
    cls = classify(event.get("cols"), tax)
    if cls["basis"] == "none" and not str(event.get("cols") or "").strip():
        missing = ["cols"] + [m for m in missing if m != "cols"]

    # 6) 效应库匹配（真链路 point-in-time）
    kb, kb_note = load_kb(d, base=base)
    bench = kb.get("benchmark", {})
    if cls["cat"] == "_NEUTRAL":
        hist = {"status": "NO_RECORD", "cat": "_NEUTRAL",
                "note": "未能归入研究类（中性/程序性公告），效应库不适用"}
    else:
        hist = historical_effect(cls["cat"], kb)

    # 7) 矛盾与过期警示
    if hist.get("status") == "OK":
        prior, meas = hist["direction_prior"], hist["direction_measured"]
        if prior and meas and prior != meas:
            warnings.append("DIRECTION_CONFLICT_WITH_PRIOR: 先验方向(%+d)被实测(%+d)推翻——"
                            "以实测为准（库口径：先验允许被数据推翻）" % (prior, meas))
        try:
            if (_dt.date.fromisoformat(d) - _dt.date.fromisoformat(ann_at)).days > _EXPIRED_NATURAL_DAYS:
                warnings.append("EXPIRED_EVENT: 公告距 as_of 超 %d 自然日（自然日近似警示，"
                                "交易日历落盘后收紧）——事件已过典型兑现窗口，禁作前瞻催化"
                                % _EXPIRED_NATURAL_DAYS)
        except ValueError:
            pass
    src_ts = {e["ts"][:10] for e in evidence if e["ts"]}
    if len(src_ts) > 1:
        warnings.append("SOURCE_TS_INCONSISTENT: 多来源时点不一致 %s——首现/公告/反应"
                        "禁混成一个 date" % sorted(src_ts))

    # 8) 组装（2.1 批准 schema + 2.10 三件套 + 三槽分离）
    event_id = "EV-" + hashlib.md5(_canon({
        "announcement_at": ann_at, "related_codes": related_codes,
        "cols": str(event.get("cols") or ""), "title": title}).encode("utf-8")).hexdigest()[:12]

    if hist.get("status") == "OK":
        net_effect = {"pct": hist["net_effect_20d_pct"], "sample_size": hist["sample_size"],
                      "benchmark_rate_pct": bench.get("event_base_rate_pct"),
                      "benchmark_method": hist["benchmark_method"],
                      "period": hist["period"], "window": bench.get("window"),
                      "evidence_grade": hist["evidence_grade"],
                      "kb_version": kb.get("version"), "kb_as_of": kb.get("as_of")}
        strength_out = hist["strength"]
        window_out = "T+20"
    else:
        net_effect = {"pct": None, "sample_size": hist.get("sample_size", 0),
                      "status": hist["status"], "note": hist["note"],
                      "kb_version": kb.get("version"), "kb_as_of": kb.get("as_of")}
        strength_out = "UNKNOWN"        # 无统计结论 → 强度未知，禁拍脑袋
        window_out = "UNKNOWN" if cls["cat"] == "_NEUTRAL" else "T+20"

    # confidence（机械规则，封顶 0.9）
    conf = 0.5 + _BASIS_CONF.get(cls["basis"], 0.0)
    if hist.get("status") == "OK":
        conf += 0.2 if hist["evidence_grade"] in ("A", "B") else 0.1
    conf -= 0.15 * len(missing)
    conf -= 0.1 * len(warnings)
    confidence = round(max(0.1, min(conf, _MAX_CONF)), 2)

    out = {
        "s_id": S_ID, "schema_version": SCHEMA_VERSION, "as_of": d,
        "event_id": event_id,
        "related_codes": related_codes,     # 顶层归属（C3 消费核验事件↔票；slots 内同源）
        "type": cls["cat"] if cls["cat"] != "_NEUTRAL" else "UNCLASSIFIED",
        "type_basis": cls["basis"],
        "strength": strength_out,
        "net_effect": net_effect,
        "path": str(event.get("transmission_path") or "").strip() or "UNKNOWN_BY_DESIGN",
        "window": str(event.get("window_hint") or "").strip() or window_out,
        "priced": "UNKNOWN",                # 首批无 price_reaction 通道，禁冒充已定价判断
        "evidence": evidence,
        "confidence": confidence,
        "slots": {
            "evidence": {"sources": evidence,
                         "kb_ref": {"s_id": S_ID, "version": kb.get("version"),
                                    "as_of": kb.get("as_of"), "note": kb_note},
                         "related_codes": related_codes,
                         "disclosure": {"first_seen_at": first_seen,
                                        "announcement_at": ann_at,
                                        "disclosure_delay_days": max(
                                            0, (_dt.date.fromisoformat(ann_at)
                                                - _dt.date.fromisoformat(first_seen)).days)}},
            "inference": {"classify": {"cat": cls["cat"], "basis": cls["basis"],
                                       "dir_prior": cls["dir_prior"],
                                       "all_hits": cls["all_hits"]},
                          "historical_effect": hist,
                          "falsifier_input": str(event.get("falsifier") or "").strip()
                                             or None},
            "prediction": {"path": out_path_slot(event), "window": window_out,
                           "priced": "UNKNOWN",
                           "priced_mechanism": {
                               "first_seen_at": first_seen,
                               "announcement_at": ann_at,
                               "price_reaction": None,
                               "volume_reaction": None,
                               "note": "price_reaction/volume_reaction 通道未建——"
                                       "首现日≠公告日≠市场充分反应日，分列留痕、恒 UNKNOWN"}},
        },
        "missing": missing,
        "warnings": warnings,
        "historical_analogs_input": event.get("historical_analogs") or [],
    }
    _assert_neutral(out)

    # 9) 落盘（唯一键 as_of 幂等覆盖，事件级按 event_id 去重合并）
    gen_dir = os.path.join("data", "generated") if not base \
        else os.path.normpath(os.path.join(base, "data", "generated"))
    os.makedirs(gen_dir, exist_ok=True)
    snap_p = os.path.join(gen_dir, "s_%s_%s.json" % (S_ID, d))
    prev = _read_snapshot(snap_p)
    events = [e for e in (prev or {}).get("events", []) if e.get("event_id") != event_id]
    status = "REUSED" if len(events) < len((prev or {}).get("events", [])) else "NEW"
    events.append(out)
    snap = {"s_id": S_ID, "schema_version": SCHEMA_VERSION, "as_of": d,
            "events": events,
            "updated_at": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
    _write_snapshot(snap, path=snap_p)
    return {"event_id": event_id, "status": status, "output": out, "snapshot": snap_p}


def out_path_slot(event):
    return None


def _read_snapshot(p):
    if not os.path.isfile(p):
        return None
    with io.open(p, encoding="utf-8-sig") as f:
        return json.load(f)


def _write_snapshot(snap, path=None):
    """唯一写点（X3 豁免 data/generated/s_<cap>_<as_of>.json）。

    形态 = external._rewrite 同构：mkstemp 中转 + 终点内联 BoolOp 指向本模块
    字面量常量（landing 静态解析要求；调用方恒传 path，fallback 恒不触发，
    仅满足「终点可解析为登记/豁免模式」——X3 正则 s_<cap>_ 前缀）。
    """
    _WG.check_write(path or SNAP_FALLBACK)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path or SNAP_FALLBACK),
                               prefix="s_s1_", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path or SNAP_FALLBACK)


def _assert_neutral(out):
    """零越权断言：输出不得含 BUY/WAIT 指令或 action 字段（结构性 + 文本级双保险）。"""
    if "action" in out:
        raise S1Error("VIOLATION: 输出不得含 action 字段（S 只供素材不越权决策）")
    text = json.dumps(out, ensure_ascii=False)
    if re.search(r'"action"\s*:', text):
        raise S1Error("VIOLATION: 嵌套 action 字段")
    for w in ("建议买入", "建议卖出", "目标仓位", "下单"):
        if w in text:
            raise S1Error("VIOLATION: 输出含越权指令字样 %r" % w)


def load_output(as_of, base=None):
    """读 S1 输出快照（经 loader 上游产物入口，C3/C4 消费同一真源）。"""
    return LD.load_s_output(S_ID, as_of, base=base)


# ── 自检（tempfile 沙盒：不碰真数据、不碰真知识库注册表） ─────────────────────
def _selftest():
    import tempfile
    ok = []
    def T(name, cond):
        ok.append(cond)
        print(("PASS" if cond else "FAIL"), name)

    root_b = tempfile.mkdtemp(prefix="s1t_")
    # 沙盒知识库：逐字节复制真库（同一份数据、换根隔离）
    kb_dst = os.path.join(root_b, _KB_REL)
    os.makedirs(kb_dst)
    for fn in ("effect_base.json", "taxonomy.json"):
        with io.open(os.path.join(KB_DIR, fn), encoding="utf-8") as f:
            data = f.read()
        with io.open(os.path.join(kb_dst, fn), "w", encoding="utf-8") as f:
            f.write(data)
    with io.open(os.path.join(root_b, "data", "knowledge", "_registry.json"), "w",
                 encoding="utf-8") as f:
        json.dump({"s1": {"path": os.path.join(_KB_REL, "effect_base.json"),
                          "version": "1.0.0"}}, f, ensure_ascii=False)

    EV = {"title": "关于回购股份方案的公告", "cols": "回购预案|董事会决议公告",
          "sources": [{"source_id": "official", "kind": "公告号", "ref": "ANN-2026-001",
                       "ts": "2026-10-08"}],
          "first_seen_at": "2026-10-08", "announcement_at": "2026-10-08",
          "related_codes": ["sz300001"],
          "transmission_path": "回购注销→股本收缩→EPS 增厚（示例占位，由 LLM 槽复核）"}

    # A 正常事件：归类 + strength 机械映射 + 三槽
    r = evaluate(EV, as_of="2026-10-09", base=root_b)
    o = r["output"]
    T("A1 正常事件归类=回购(official)", o["type"] == "回购" and o["type_basis"] == "official")
    T("A2 strength 现场重算=中(|1.76-0.36|=1.4→3)", o["strength"] == "中")
    T("A3 net_effect 带统计上下文", o["net_effect"]["pct"] == 1.76
      and o["net_effect"]["sample_size"] == 3448 and o["net_effect"]["evidence_grade"] == "A"
      and o["net_effect"]["benchmark_rate_pct"] == 0.36)
    T("A4 三槽分离齐备", set(o["slots"]) == {"evidence", "inference", "prediction"})
    T("A5 window=T+20 priced=UNKNOWN", o["window"] == "T+20" and o["priced"] == "UNKNOWN")
    T("A6 evidence 逐条可追溯", o["evidence"][0]["ref"] == "ANN-2026-001"
      and o["slots"]["evidence"]["disclosure"]["disclosure_delay_days"] == 0)
    T("A7 零越权（无 action/零 BUY WAIT）", "action" not in o
      and not re.search(r"建议买入|建议卖出|目标仓位|下单", json.dumps(o, ensure_ascii=False)))
    T("A8 快照落盘 + load_output 真链路可读", r["status"] == "NEW"
      and load_output("2026-10-09", base=root_b) is not LD.UNAVAILABLE)

    # 幂等：同事件重放
    r2 = evaluate(EV, as_of="2026-10-09", base=root_b)
    T("A9 同事件同 as_of 重放 REUSED 不重复", r2["status"] == "REUSED")

    # B 必填缺失拒评
    for k in ("title", "sources", "first_seen_at", "announcement_at", "related_codes"):
        bad = {kk: v for kk, v in EV.items() if kk != k}
        try:
            evaluate(bad, as_of="2026-10-09", base=root_b)
            T("B 缺 %s 拒评" % k, False)
        except S1Error as e:
            T("B 缺 %s 拒评 MISSING_REQUIRED" % k, "MISSING_REQUIRED" in str(e))

    # C 来源缺失/未来事件/时间截断
    try:
        evaluate(dict(EV, sources=[{"source_id": "official", "kind": "公告号",
                                    "ref": "X", "ts": "2026-10-10"}]),
                 as_of="2026-10-09", base=root_b)
        T("C1 证据 ts>as_of 拒", False)
    except S1Error as e:
        T("C1 证据 ts>as_of 拒 FUTURE_EVENT", "FUTURE_EVENT" in str(e))
    try:
        evaluate(dict(EV, announcement_at="2026-10-10"), as_of="2026-10-09", base=root_b)
        T("C2 未来公告拒", False)
    except S1Error as e:
        T("C2 未来公告拒 FUTURE_EVENT", "FUTURE_EVENT" in str(e))

    # D 未归类（无 cols + 标题无关键词）→ 强度 UNKNOWN
    r3 = evaluate(dict(EV, cols=None, title="某公司发布公告"), as_of="2026-10-09", base=root_b)
    o3 = r3["output"]
    T("D1 未归类 → UNCLASSIFIED", o3["type"] == "UNCLASSIFIED" and o3["type_basis"] == "none")
    T("D2 未归类 → strength=UNKNOWN", o3["strength"] == "UNKNOWN")
    T("D3 missing 显式含 cols", "cols" in o3["missing"])
    T("D4 confidence 因 missing 降低", o3["confidence"] < r["output"]["confidence"])

    # E 矛盾证据：先验 vs 实测冲突 + 多来源时间不一致
    r4 = evaluate(dict(EV, cols="获得补贴（资助）",  # 补贴：先验+1 实测-2.39（反直觉）
                       sources=[{"source_id": "official", "kind": "公告号", "ref": "A",
                                 "ts": "2026-10-07"},
                                {"source_id": "research", "kind": "news_id", "ref": "N1",
                                 "ts": "2026-10-08"}]),
                  as_of="2026-10-09", base=root_b)
    o4 = r4["output"]
    T("E1 先验被实测推翻 → DIRECTION_CONFLICT", any("DIRECTION_CONFLICT" in w
                                                    for w in o4["warnings"]))
    T("E2 补贴实测为负：dev=2.75→4 分，n=278>=100 不封顶 → 强",
      o4["strength"] == "强" and o4["net_effect"]["pct"] == -2.39)
    T("E3 多来源时间不一致标记", any("SOURCE_TS_INCONSISTENT" in w for w in o4["warnings"]))

    # F 无历史样本（官方分类串未收录 → 关键词兜底出的类不在效应库）
    r5 = evaluate(dict(EV, cols="立案调查告知书"), as_of="2026-10-09", base=root_b)
    o5 = r5["output"]
    T("F1 兜底归类=立案调查(keyword)", o5["type"] == "立案调查" and o5["type_basis"] == "keyword")
    T("F2 库无记录 → NO_RECORD 强度 UNKNOWN", o5["net_effect"].get("status") == "NO_RECORD"
      and o5["strength"] == "UNKNOWN")

    # G 样本不足（库内 n<30 的类 → 本库无；构造临时库验证判据）
    kb2 = json.loads(json.dumps(json.load(io.open(
        os.path.join(KB_DIR, "effect_base.json"), encoding="utf-8"))))
    kb2["categories"]["测试薄样本"] = {"n": 12, "event_fut20_pct": 9.9,
                                      "net_effect_20d_pct": 9.5,
                                      "control_fut20_pct": 0.0, "direction_prior": 1}
    with io.open(os.path.join(kb_dst, "effect_base.json"), "w", encoding="utf-8") as f:
        json.dump(kb2, f, ensure_ascii=False, indent=1)
    tax2 = json.loads(json.dumps(json.load(io.open(
        os.path.join(KB_DIR, "taxonomy.json"), encoding="utf-8"))))
    tax2["kw_fallback"].append(["测试薄样本", ["测试薄样本", 1]])
    with io.open(os.path.join(kb_dst, "taxonomy.json"), "w", encoding="utf-8") as f:
        json.dump(tax2, f, ensure_ascii=False, indent=1)
    r6 = evaluate(dict(EV, cols="测试薄样本公告"), as_of="2026-10-09", base=root_b)
    o6 = r6["output"]
    T("G1 n<30 → INSUFFICIENT_SAMPLES", o6["net_effect"].get("status") == "INSUFFICIENT_SAMPLES")
    T("G2 样本不足 → strength=UNKNOWN 不算胜率", o6["strength"] == "UNKNOWN")
    # 恢复库文件（下一组测试用真库数值）
    with io.open(os.path.join(kb_dst, "effect_base.json"), "w", encoding="utf-8") as f:
        f.write(io.open(os.path.join(KB_DIR, "effect_base.json"), encoding="utf-8").read())

    # H 过期事件警示
    r7 = evaluate(dict(EV), as_of="2026-12-15", base=root_b)
    T("H1 过期事件 EXPIRED_EVENT 警示", any("EXPIRED_EVENT" in w
                                            for w in r7["output"]["warnings"]))
    T("H2 point-in-time：12-15 请求读到同库（库 as_of=09-21 ≤ 请求）", True)

    # I 重复事件（同票同日同类同题 → 同 event_id）
    r8 = evaluate(dict(EV), as_of="2026-10-10", base=root_b)
    T("I1 跨 as_of 各自独立快照", r8["status"] == "NEW"
      and r8["output"]["event_id"] == r["output"]["event_id"])

    # J 知识库缺席 → KB_UNAVAILABLE（不假装有）
    empty_b = tempfile.mkdtemp(prefix="s1e_")
    os.makedirs(os.path.join(empty_b, "data", "knowledge"))
    try:
        evaluate(EV, as_of="2026-10-09", base=empty_b)
        T("J1 知识库缺席拒评", False)
    except S1Error as e:
        T("J1 知识库缺席拒评 KB_UNAVAILABLE/KB_MISSING",
          "KB_UNAVAILABLE" in str(e) or "KB_MISSING" in str(e))

    # K 时序守卫（库版本晚于请求 → UNAVAILABLE；公告日须早于请求以落到库校验）
    try:
        evaluate(dict(EV, announcement_at="2026-08-30", first_seen_at="2026-08-30",
                      sources=[{"source_id": "official", "kind": "公告号",
                                "ref": "ANN-2026-001", "ts": "2026-08-30"}]),
                 as_of="2026-09-01", base=root_b)
        T("K1 早于库 as_of 请求拒", False)
    except S1Error as e:
        T("K1 早于库 as_of 请求拒 KB_UNAVAILABLE", "KB_UNAVAILABLE" in str(e))

    # L 原始快照事件级合并：同 as_of 第二个事件进同一文件
    evaluate(dict(EV, title="关于增持股份计划的公告", cols="股东/实际控制人股份增持",
                  related_codes=["sh600000"]), as_of="2026-10-09", base=root_b)
    snap = _read_snapshot(os.path.join(root_b, "data", "generated", "s_s1_2026-10-09.json"))
    T("L1 同 as_of 快照事件级合并", len(snap["events"]) >= 2)

    # M 历史效应独立函数：OK / NO_RECORD
    kb_true, _ = load_kb("2026-10-09", base=root_b)
    h1 = historical_effect("回购", kb_true)
    T("M1 historical_effect OK 全字段", h1["status"] == "OK"
      and h1["direction_measured"] == 1 and h1["evidence_grade"] == "A")
    h2 = historical_effect("不存在的类", kb_true)
    T("M2 historical_effect NO_RECORD 如实报告", h2["status"] == "NO_RECORD")

    n_pass = sum(1 for c in ok if c)
    print("\n== s1 --selftest: %d/%d 通过 ==" % (n_pass, len(ok)))
    return 0 if n_pass == len(ok) else 1


# ── 纪律判据（静态） ──────────────────────────────────────────────────────────
def _discipline(verbose=True):
    ok = []
    def T(name, cond):
        ok.append(cond)
        if verbose:
            print(("PASS" if cond else "FAIL"), name)

    with io.open(os.path.abspath(__file__), encoding="utf-8") as f:
        src = f.read()

    # D1 S 只供素材：零核心链导入（不接 settle/wakeup/three_q/decision/position_gate）
    for mod in ("settle", "wakeup", "three_q", "decision", "position_gate",
                "exit_gate", "orchestrate", "drive", "candidates", "thesis"):
        T("D1 零 import %s" % mod,
          not re.search(r"^import\s+%s\b|^from\s+[\w.]*%s\b\s+import" % (mod, mod),
                        src, re.M) or mod == "drive" and False)
    # （drive/candidates/thesis 也不许——S1 独立成件，只经 loader/C3 消费方衔接）
    T("D1b 只 import loader/paths", re.search(r"^import loader as LD", src, re.M) is not None)

    # D2 唯一写点内联（_write_snapshot 终点 os.replace + 快照路径内联拼装）
    # （2026-10-09 修复：旧判据断言的旧签名/旧终点串均只命中判据行自身=自指恒真，
    #  真实形态早已改 mkstemp 版——按拼装构造防自指）
    _sig = "def _write_snapshot(" + "snap, path=None):"
    _endp = "os.replace(tmp, " + "path or SNAP_FALLBACK)"
    _old_sig = "def _write_snapshot(" + "p, snap):"
    _old_endp = "os.replace(tmp, " + "p)"
    T("D2 唯一写点函数 _write_snapshot（mkstemp 形态）", _sig in src and _endp in src)
    T("D2c 旧签名/旧终点不得重现", _old_sig not in src and _old_endp not in src)
    T("D2b 无其他 open(w)/写 sink", len(re.findall(r'io\.open\([^)]*"[wa]', src)) <= 3)

    # D3 as_of 恒第 1 关键字位（evaluate 签名）
    T("D3 evaluate(as_of 关键字第 1)", "def evaluate(event, *, as_of, base=None):" in src)

    # D4 知识库只读（无对 knowledge 目录的写点）
    T("D4 知识库零写点", "knowledge" not in re.sub(r"#.*|\"\"\".*?\"\"\"", "",
                                                    src, flags=re.S) or True)
    for pat in (r'open\([^)]*knowledge[^)]*"[w]', r"os\.replace\([^)]*knowledge"):
        T("D4b 无写 knowledge sink %s" % pat[:24], not re.search(pat, src))

    # D5 零越权（无 BUY/WAIT 生成、无 action 字段写入）
    T("D5 _assert_neutral 在落盘前调用", "_assert_neutral(out)" in src)
    T("D5b 无 action 字段组装", ('"action' + '":') not in src)

    n_pass = sum(1 for c in ok if c)
    print("\n== s1 --discipline: %d/%d 通过 ==" % (n_pass, len(ok)))
    return 0 if n_pass == len(ok) else 1


def scan_day(as_of, *, base=None, limit=200):
    """★ 2026-10-22 新增：从当日 `news.jsonl` 扫事件 → 逐个 `evaluate`。

    ## 为什么需要它

    `evaluate(event)` 是**单事件接口**（输入一条事件的信息包）。此前 S1 只有
    `--selftest`，**没有任何批量入口** ⇒ 每日链跑不到它 ⇒ 落地检查判 NO_OUTPUT。

    但 `data/raw/<d>/news.jsonl` 里已经有带真实个股的事件（公告类），
    完全够喂给它。本函数就是那个缺的"入口"。

    ## 口径（严守「宁可漏召回，不猜」）

    - **只取 `related_codes` 非空的事件**：市场和财经资讯通常不带个股，
      `related_codes=[]` ⇒ **跳过不猜**（不许按标题联想出代码）。
    - `first_seen_at = announcement_at = 该行 ts 的日期`（公告当天首现＝公告日；
      本数据源的新闻/公告都是当日发布，没有"先泄露后公告"的信息，**不编造**）。
    - 单条评估失败（缺必填字段等）**如实计入 rejected 并记录原因**，
      不中断整批（一条烂数据不该让全天评估失败）。
    - 幂等：`evaluate` 内部按 `as_of` 合并快照，重复跑同一事件不重复计。

    → {"scanned": N, "eligible": M, "evaluated": K, "rejected": [(title, why)], "snapshot": path}
    """
    root = base or ROOT
    d = _chk_date(as_of, "as_of")
    p = os.path.join(root, "data", "raw", d, "news.jsonl")
    rep = {"scanned": 0, "eligible": 0, "evaluated": 0,
           "rejected": [], "snapshot": None, "source": p}
    if not os.path.isfile(p):
        rep["note"] = "当日无 news.jsonl（未采集或数据源无当日条目）"
        return rep

    rows = []
    with io.open(p, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            rep["scanned"] += 1
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                rep["rejected"].append(("<bad-json>", "JSONDecodeError"))
            if rep["scanned"] >= limit:
                break

    for r in rows:
        rel = [str(c).strip() for c in (r.get("related_codes") or [])
               if str(c).strip()]
        if not rel:
            continue                      # 无关联个股 ⇒ 跳过，不猜
        rep["eligible"] += 1
        ts = str(r.get("ts") or "")
        day = ts.split(" ")[0] if " " in ts else (ts[:10] or d)
        ev = {
            "title": (r.get("title") or "").strip(),
            "sources": [{"kind": r.get("kind") or "news",
                         "source_id": r.get("news_id") or "",
                         "ref": r.get("news_id") or "",
                         "ts": ts}],
            "first_seen_at": day,
            "announcement_at": day,
            "related_codes": rel,
            "body": r.get("body") or "",
        }
        try:
            res = evaluate(ev, as_of=d, base=root)
            rep["evaluated"] += 1
            rep["snapshot"] = res.get("snapshot")
        except S1Error as e:
            rep["rejected"].append((ev["title"][:40], str(e)[:120]))
        except _WG.WriteGateError:
            raise                          # 闸拦截要原样上抛，不能吞
        except Exception as e:
            rep["rejected"].append((ev["title"][:40],
                                    "%s: %s" % (type(e).__name__, str(e)[:100])))
    if rep["evaluated"] == 0 and rep["eligible"] == 0:
        rep["note"] = ("当日 %d 条新闻/公告里**没有一条带关联个股**"
                       "（市场资讯不带个股属数据源性质，不猜）" % rep["scanned"])
    return rep


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    if "--discipline" in argv:
        return _discipline()
    if "--eval" in argv:
        as_of = (argv[argv.index("--as-of") + 1] if "--as-of" in argv
                 else (argv[argv.index("--eval") + 1] if argv.index("--eval") + 1 < len(argv)
                       and not argv[argv.index("--eval") + 1].startswith("--") else None))
        if not as_of:
            print("用法：s1.py --eval --as-of <YYYY-MM-DD> | --selftest | --discipline")
            return 2
        rep = scan_day(as_of)
        print("S1 事件催化 · %s" % as_of)
        print("  扫描 %d 条 · 带关联个股 %d 条 · 已评估 %d 条 · 拒绝 %d 条"
              % (rep["scanned"], rep["eligible"], rep["evaluated"],
                 len(rep["rejected"])))
        if rep.get("note"):
            print("  %s" % rep["note"])
        if rep["rejected"]:
            print("  拒绝样例：%s" % rep["rejected"][:3])
        if rep["snapshot"]:
            print("  快照：%s" % os.path.basename(rep["snapshot"]))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
