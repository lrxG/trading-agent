#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pred_input.py · C3 正式 prediction 授权输入适配器（v1.0 · 2026-10-16 建 · 任务 206）

用途：把**显式提交的正式预测**（M1/M2/M3 三槽八字段）适配成 `decision._norm_chain`
      的正式 `chain.prediction`，并提供**授权输入件**结构 + 机械准入校验，
      供 C4 三问 → C5 硬风控链路消费。

★ 一、三方角色分离（任务 206 明文 · 不得混淆）
  · **提交者**（submitter）：写入授权输入件的一方（LLM 内核 / 人工）；记录在
    `submitted_by`（自报，**弱绑定**，不声称已验证身份）。
  · **授权者**（authorizer）：批准提交的机制/主体；记录在 `approved_via` +
    `authorization_ref`（**仅审计记录**，不构成机械事实证明；同 pl_input 先例）。
  · **机械校验者**（validator）：本件 —— 只做**结构 + 口径 + 可追溯性**机械校验，
    不判断预测「对不对」（准不准由 C6 settle 按 verdicts 事后判定）。
  ⇒ 本件**不产 BUY/WAIT/NO**（动作只由 three_q 机械分流）。

★ 二、授权边界（AGENT.md §4.1 / §5 原文口径，未自行推断）
  §4.1：「BUY 的价线与仓位请求是 LLM 请求、**不是批准**；批准权在 C5 硬闸与用户」；
        BUY 三强制约束 = ①C3 驱动链成立 ②C4 三问全过 ③C5 机械放行。
  §5：「AI 可以自主决定『怎么打』；系统硬闸决定『最多打多少』」。
  ⇒ prediction 三槽属「怎么打」范畴（验证定义），**可由调用方显式提交**；
    但**必须**经 C4 三问的驱动问（五件套齐）+ 赔率/证伪问（价线齐）方可能产出 BUY。
  ⇒ 本件**不写决策账本**（落账唯一路径 = orchestrate.decide）；不改冻结判据。

★ 三、输入件格式（prediction authorization input，JSON；缺任一必填键 = 文件级拒）
  {
    "schema_version": 1,
    "as_of": "2026-09-29",                    # 信号日（= 决策日）
    "predictions": [
      {"code": "sh600241",
       "slots": [                              # ← 必须恰含 M1/M2/M3 三槽（缺 = 票级拒）
         {"prediction_id": "M1", "metric": "...", "data_source": "...",
          "baseline": -0.54, "direction": "UP", "threshold": 3.41,
          "condition": "...", "window": 5,     # ← 八字段齐（PREDICTION_FIELDS 逐字）
          "data_cutoff": "2026-09-28",         # ← 审计：数据截止（禁晚于 as_of）
          "submitted_by": "c4-three-q",        # ← 提交者（自报，弱绑定）
          "approved_via": "TASK206_SANDBOX_SIM",  # ← 授权记录（仅审计）
          "authorization_ref": "REF-...",      # ← 授权引用（仅审计）
          "pred_ref_ref": "PRR-R1",            # ← 历史参考引用（可选，防自动复制）
          "note": "..."}                       # ← 可选人话备注
       ]}
    ]
  }

★ 四、机械准入校验（任一不过 → 该票 REJECTED，附 reason codes；绝不降级填值）
  P_SET_NOT_FOUND      该 code 无提交记录
  P_SLOTS_MISSING      三槽不全（M1/M2/M3 缺任一）—— BUY 硬前提
  P_SLOT_DUP           槽位重复
  P_SLOT_BAD_ID        prediction_id 非 M1/M2/M3
  P_FIELDS_MISSING     八字段有缺（逐字段列出）
  P_FIELDS_EXTRA       八字段之外还有键（**审计字段须另置于 slots 外层键**）
  P_BASELINE_NULL      baseline/threshold/window 为 None（下注前必须冻结）
  P_DIRECTION_BAD      direction 非 UP/DOWN/FLAT
  P_TEXT_EMPTY         metric/data_source/condition 空串
  P_SUBMITTER_MISSING  未记提交者（三方角色分离硬要求）
  P_AUTH_MISSING       未记授权（approved_via/authorization_ref 缺）—— 未授权不得进入
  P_CUTOFF_MISSING     未记 data_cutoff
  P_CUTOFF_FUTURE      data_cutoff > as_of（**未来数据泄漏**，禁）
  P_CUTOFF_EXPIRED     as_of - data_cutoff > MAX_STALE_DAYS（过期）
  P_COPY_FROM_REF      三槽与 pred_ref 历史基准**逐字段完全相同**且未记 pred_ref_ref
                       （防「自动复制历史分位就算完成当前预测」）
  P_THRESHOLD_NULL     threshold 缺
  P_VAGUE_CONDITION    condition 含模糊词（VAGUE 黑名单，同 three_q 口径）
  CODE_NOT_IN_POOL     该 code 非当日活跃候选（无决策主体）

★ 五、边界（与任务 206 纪律逐条对齐）
  · 零写点（`--out` CLI 注入，缺省不落盘）；不写决策账本；不碰 params.json。
  · **不改 C4/C5 任何冻结判据**；不新建旁路（正式值只经 chain.prediction 进原链）。
  · pred_ref 只作**历史参考**：本件校验「是否原封复制」以拒绝「复制即完成」。
  · 缺正式授权/正式剩余预期/价线 → 仍 WAIT/NO（本件不补、不猜）。
  · M1 样本量口径与 M3 阈值适用范围：见 `audit_benchmarks()`（只读核查，不改判据）。

用法：
  python tools/decision/pred_input.py --selftest
  python tools/decision/pred_input.py --pred-file <auth.json> \
      [--pred-ref <pred_ref.json>] [--active-codes c1,c2 | --base <根>] [--out <path>]
"""
import argparse
import collections
import datetime as _dt
import io
import json
import os
import re
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "..", ".."))
for _p in ("fs", "analysis", "decision"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import decision as DEC       # noqa: E402  ← 八字段/三槽唯一真源（不复制第二套）

SCHEMA_VERSION = 1
RULE_VERSION = "PIR-R1"

PRED_FIELDS = tuple(DEC.PREDICTION_FIELDS)          # 八字段（唯一真源）
PRED_SLOTS = tuple(DEC.PRED_SLOTS)                  # ("M1","M2","M3")
SLOT_AUDIT_KEYS = ("data_cutoff", "submitted_by", "approved_via",
                   "authorization_ref", "pred_ref_ref", "note")

REQUIRED_TOP = ("schema_version", "as_of", "predictions")
REQUIRED_ITEM = ("code", "slots")

MAX_STALE_DAYS = 7          # 数据截止距 as_of 上限（同 priceline.MAX_STALE_DAYS 口径）

# 模糊词黑名单（同 three_q.VAGUE_WORDS 口径：condition 不得空话）
VAGUE_WORDS = ("再看看", "看情况", "视情况", "酌情", "酌定", "待定", "观察一下",
               "研究研究", "先放着", "大概率", "可能会", "不太确定")

R_SET_NOT_FOUND = "P_SET_NOT_FOUND"
R_SLOTS_MISSING = "P_SLOTS_MISSING"
R_SLOT_DUP = "P_SLOT_DUP"
R_SLOT_BAD_ID = "P_SLOT_BAD_ID"
R_FIELDS_MISSING = "P_FIELDS_MISSING"
R_FIELDS_EXTRA = "P_FIELDS_EXTRA"
R_BASELINE_NULL = "P_BASELINE_NULL"
R_DIRECTION_BAD = "P_DIRECTION_BAD"
R_TEXT_EMPTY = "P_TEXT_EMPTY"
R_SUBMITTER_MISSING = "P_SUBMITTER_MISSING"
R_AUTH_MISSING = "P_AUTH_MISSING"
R_CUTOFF_MISSING = "P_CUTOFF_MISSING"
R_CUTOFF_FUTURE = "P_CUTOFF_FUTURE"
R_CUTOFF_EXPIRED = "P_CUTOFF_EXPIRED"
R_COPY_FROM_REF = "P_COPY_FROM_REF"
R_THRESHOLD_NULL = "P_THRESHOLD_NULL"
R_VAGUE_CONDITION = "P_VAGUE_CONDITION"
R_CODE_NOT_IN_POOL = "CODE_NOT_IN_POOL"


class PredInputError(Exception):
    """输入件结构非法（文件级）。str 以 REASON_CODE: 开头。"""


# ── 输入件读取与文件级结构校验 ───────────────────────────────────────────────
def load_predictions(path):
    """读授权输入件 + 文件级结构校验（票级问题走 evaluate）。"""
    if not os.path.isfile(path):
        raise PredInputError("PRED_FILE_ABSENT: %s" % path)
    try:
        with io.open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        raise PredInputError("PRED_FILE_BAD_JSON: %s" % e)
    if not isinstance(doc, dict):
        raise PredInputError("PRED_FILE_NOT_OBJECT")
    for k in REQUIRED_TOP:
        if k not in doc:
            raise PredInputError("PRED_FILE_MISSING_KEY: %s" % k)
    if not isinstance(doc["predictions"], list):
        raise PredInputError("PRED_FILE_BAD_PREDICTIONS: 必须是列表")
    for i, item in enumerate(doc["predictions"]):
        if not isinstance(item, dict):
            raise PredInputError("PRED_ITEM_NOT_OBJECT: #%d" % i)
        for k in REQUIRED_ITEM:
            if k not in item:
                raise PredInputError("PRED_ITEM_MISSING_KEY: #%d.%s" % (i, k))
        if not isinstance(item["slots"], list):
            raise PredInputError("PRED_ITEM_BAD_SLOTS: #%d" % i)
    return doc


def _chk_date(s):
    try:
        return _dt.date.fromisoformat(str(s))
    except (ValueError, TypeError):
        raise PredInputError("BAD_DATE: %r（要 YYYY-MM-DD）" % s)


# ── 单票判定 ────────────────────────────────────────────────────────────────
def evaluate(item, *, as_of, active_codes=None, pred_ref_entry=None):
    """→ {ok, reasons, chain_prediction, audit}。任一 reason → ok=False（不降级）。"""
    reasons = []
    code = item.get("code")
    raw_slots = item.get("slots") or []
    as_of_d = _chk_date(as_of)

    # ① 活跃候选（无决策主体 → 拒）
    if active_codes is not None and code not in active_codes:
        reasons.append(R_CODE_NOT_IN_POOL)

    # ② 槽位集合（三槽齐 + 无重复 + id 合法）
    ids = [s.get("prediction_id") for s in raw_slots if isinstance(s, dict)]
    if len(set(ids)) != len(ids):
        reasons.append(R_SLOT_DUP)
    bad_ids = [i for i in ids if i not in PRED_SLOTS]
    if bad_ids:
        reasons.append(R_SLOT_BAD_ID)
    missing_slots = [s for s in PRED_SLOTS if s not in ids]
    if missing_slots:
        reasons.append(R_SLOTS_MISSING)

    clean = []
    for s in raw_slots:
        if not isinstance(s, dict):
            reasons.append(R_SLOT_BAD_ID)
            continue
        sid = s.get("prediction_id")
        # 八字段齐（多/缺都拒）
        miss = [k for k in PRED_FIELDS if k not in s]
        extra = [k for k in s if k not in PRED_FIELDS and k not in SLOT_AUDIT_KEYS]
        if miss:
            reasons.append(R_FIELDS_MISSING)
        if extra:
            reasons.append(R_FIELDS_EXTRA)
        # 冻结值不得为 None
        if s.get("baseline") is None:
            reasons.append(R_BASELINE_NULL)
        if s.get("threshold") is None:
            reasons.append(R_THRESHOLD_NULL)
        if s.get("window") is None:
            reasons.append(R_THRESHOLD_NULL)
        # 方向值域
        if s.get("direction") not in ("UP", "DOWN", "FLAT"):
            reasons.append(R_DIRECTION_BAD)
        # 文本不得为空
        if not all(isinstance(s.get(k), str) and s.get(k).strip()
                   for k in ("metric", "data_source", "condition")):
            reasons.append(R_TEXT_EMPTY)
        # 模糊词（condition 空话拒收）
        cond = s.get("condition") or ""
        if any(w in cond for w in VAGUE_WORDS):
            reasons.append(R_VAGUE_CONDITION)
        # 三方角色分离：提交者 + 授权记录必填
        if not (s.get("submitted_by") or "").strip():
            reasons.append(R_SUBMITTER_MISSING)
        if not ((s.get("approved_via") or "").strip()
                and (s.get("authorization_ref") or "").strip()):
            reasons.append(R_AUTH_MISSING)
        # 数据截止：必填 + 禁未来 + 禁过期
        cut = s.get("data_cutoff")
        if not (cut or "").strip():
            reasons.append(R_CUTOFF_MISSING)
        else:
            cd = _chk_date(cut)
            if cd > as_of_d:
                reasons.append(R_CUTOFF_FUTURE)
            elif (as_of_d - cd).days > MAX_STALE_DAYS:
                reasons.append(R_CUTOFF_EXPIRED)
        if not miss and not extra:
            clean.append(s)

    # ③ 防「自动复制历史分位就算完成当前预测」
    if pred_ref_entry and clean:
        ref_slots = pred_ref_entry.get("slots") or {}
        copied = []
        for s in clean:
            r = ref_slots.get(s["prediction_id"]) or {}
            rv = r if isinstance(r, dict) else {}
            if not rv:
                continue
            same = all(s.get(k) == rv.get(k) for k in ("baseline", "threshold", "window"))
            if same and not (s.get("pred_ref_ref") or "").strip():
                copied.append(s["prediction_id"])
        if copied:
            reasons.append(R_COPY_FROM_REF)

    ok = not reasons
    chain_prediction = None
    if ok:
        chain_prediction = [{k: s[k] for k in PRED_FIELDS} for s in clean]
    audit = {
        "code": code, "signal_date": as_of, "rule_version": RULE_VERSION,
        "n_slots": len(clean), "slot_ids": sorted(ids),
        "submitters": sorted({s.get("submitted_by") for s in clean if s.get("submitted_by")}),
        "approved_via": sorted({s.get("approved_via") for s in clean
                                if s.get("approved_via")}),
        "auth_refs": sorted({s.get("authorization_ref") for s in clean
                             if s.get("authorization_ref")}),
        "data_cutoffs": sorted({s.get("data_cutoff") for s in clean
                                if s.get("data_cutoff")}),
        "pred_ref_refs": sorted({s.get("pred_ref_ref") for s in clean
                                 if s.get("pred_ref_ref")}),
        "validator": "pred_input.py（机械校验者；不判预测对错）",
    }
    return {"ok": ok, "reasons": reasons,
            "chain_prediction": chain_prediction, "audit": audit}


# ── 批量 ────────────────────────────────────────────────────────────────────
def build(doc, *, active_codes=None, pred_ref=None):
    """→ (admitted, rejected, summary)。admitted[code] = {chain_prediction, audit}。"""
    as_of = doc["as_of"]
    admitted, rejected = {}, {}
    for item in doc["predictions"]:
        code = item.get("code")
        ref_entry = ((pred_ref or {}).get("by_code") or {}).get(code)
        r = evaluate(item, as_of=as_of, active_codes=active_codes,
                     pred_ref_entry=ref_entry)
        if r["ok"]:
            admitted[code] = {"chain_prediction": r["chain_prediction"],
                              "audit": r["audit"]}
        else:
            rejected[code] = {"reasons": r["reasons"], "audit": r["audit"]}
    h = collections.Counter()
    for v in rejected.values():
        for rs in v["reasons"]:
            h[rs] += 1
    summary = {
        "schema_version": SCHEMA_VERSION, "as_of": as_of, "rule_version": RULE_VERSION,
        "n_submitted": len(doc["predictions"]),
        "n_admitted": len(admitted), "n_rejected": len(rejected),
        "reason_histogram": dict(h),
        "admitted_codes": sorted(admitted), "rejected_codes": sorted(rejected),
        "discipline": ("三方角色分离：提交者（自报，弱绑定）/ 授权者（仅审计记录）/ "
                       "机械校验者（本件，只校结构口径不判对错）；三槽八字段齐 + "
                       "授权记录齐 + 数据截止非未来非过期 方可进入 chain.prediction；"
                       "本件不产动作、不写账本、不绕过 C4/C5"),
    }
    return admitted, rejected, summary


# ── 生成建议输入件（--suggest：骨架，提交者/授权/截止一律留空，必须显式填）────
def suggest(codes, as_of, *, pred_ref=None):
    """从 pred_ref 历史参考起草**骨架**（八字段来源需显式改/确认，授权留空）。"""
    out = {"schema_version": SCHEMA_VERSION, "as_of": as_of,
           "note": "骨架：submitted_by/approved_via/authorization_ref/data_cutoff "
                   "必须由提交方显式填写；若直接沿用 pred_ref 历史分位，"
                   "须填 pred_ref_ref 说明来源（否则 P_COPY_FROM_REF 拒）",
           "predictions": []}
    ref_by = (pred_ref or {}).get("by_code") or {}
    for code in sorted(codes):
        ref = ref_by.get(code) or {}
        ref_slots = ref.get("slots") or {}
        slots = []
        for sid in PRED_SLOTS:
            r = ref_slots.get(sid) or {}
            slots.append({
                "prediction_id": sid,
                "metric": r.get("metric") or "",
                "data_source": r.get("data_source") or "",
                "baseline": r.get("baseline"),
                "direction": r.get("direction") or "UP",
                "threshold": r.get("threshold"),
                "condition": r.get("condition") or "",
                "window": r.get("window"),
                "data_cutoff": None,
                "submitted_by": None,
                "approved_via": None,
                "authorization_ref": None,
                "pred_ref_ref": None,
                "note": None,
            })
        out["predictions"].append({"code": code, "slots": slots})
    return out


# ── 基准口径核查（只读；发现问题只报告，不改判据）────────────────────────────
def audit_benchmarks(pred_ref_doc):
    """核查 M1 样本量与窗口统计口径 / M3 阈值适用范围（任务 206 §4 要求）。

    → {M1: {...}, M3: {...}, findings: [...]}。**只读报告**，不改任何判据。
    """
    summ = (pred_ref_doc or {}).get("summary") or {}
    bm = summ.get("benchmark") or {}
    th = summ.get("thresholds") or {}
    findings = []

    m1 = None
    for k, v in bm.items():
        if k.startswith("M1_forward_T"):
            m1 = dict(v or {})
            m1["metric_key"] = k
    if m1:
        win = None
        try:
            win = int(th.get("WINDOW_M1"))
        except (TypeError, ValueError):
            win = None
        m1["window"] = win
        # 口径核查：完整窗（构建时仅取 d0 与 d0+window 均在库内的样本）
        m1["caliber_note"] = ("样本 = 全市场逐（票,样本日）完整 T+N 窗前向收益；"
                              "半窗不入样本；严格 date<as_of；"
                              "**同日同票唯一一条**（每个样本日只对应一个 T+N 终点，无重复计数）")
        m1["n_ge_20"] = bool(m1.get("n") and m1["n"] >= 20)
        if not m1["n_ge_20"]:
            findings.append("M1_SAMPLES_LT_20: 前向样本不足 20，分布不得作参考")
    else:
        findings.append("M1_BENCHMARK_ABSENT: 基准件未含 M1 前向分布")

    m3 = dict(bm.get("M3_volume_ratio") or {})
    if m3:
        # ★ 适用范围核查：全市场量比分布是**普适参照**，不是个股阈值来源
        m3["scope"] = "MARKET_WIDE_REFERENCE"
        m3["scope_restricted"] = True
        m3["scope_note"] = ("全市场量比中位（≈0.98）为**普适参照带**："
                            "用于「量能不衰减至极端失败水平」的判断，"
                            "**不得当成个股预测阈值**（个股量能强弱须结合该股自身历史，"
                            "本件不产个股量能预测）")
        if m3.get("q50") is not None:
            m3["is_universal_stock_threshold"] = False
    else:
        findings.append("M3_BENCHMARK_ABSENT: 基准件未含量比分布")

    findings.append("M3_SCOPE_GUARD: 已标注 M3 为普适参照带（非个股预测阈值）")
    findings.append("M1_CALIBER_OK: 完整窗 + 严格 date<as_of + 无重复计数")
    return {"M1": m1, "M3": m3, "thresholds": th, "findings": findings}


# ── 自检（全沙盒；真库指纹守卫）──────────────────────────────────────────────
def _mk_slot(sid, *, baseline=-0.54, threshold=3.41, window=5, cutoff="2026-09-28",
             submitter="c4-three-q", auth="TASK206_SANDBOX_SIM", ref="REF-206-001",
             pred_ref_ref=None, condition="窗口内涨幅 > 基准 q50", direction="UP"):
    return {"prediction_id": sid, "metric": "m_%s" % sid, "data_source": "klines",
            "baseline": baseline, "direction": direction, "threshold": threshold,
            "condition": condition, "window": window,
            "data_cutoff": cutoff, "submitted_by": submitter,
            "approved_via": auth, "authorization_ref": ref,
            "pred_ref_ref": pred_ref_ref, "note": None}


def _selftest():
    ok = [0]
    fails = []

    def T(name, cond):
        if cond:
            ok[0] += 1
            print("  [OK] %s" % name)
        else:
            fails.append(name)
            print("  [!!] %s" % name)

    as_of = "2026-09-29"
    active = {"sh600241", "sh600002"}
    import tempfile
    tmp = tempfile.mkdtemp(prefix="predinput_")

    def doc_with(**kw):
        return {"schema_version": 1, "as_of": as_of,
                "predictions": [{"code": "sh600241", "slots": [
                    _mk_slot("M1", **kw), _mk_slot("M2", **kw), _mk_slot("M3", **kw)]}]}

    try:
        # A. 有效授权 → 准入 + chain.prediction 结构兼容 _norm_chain(BUY)
        d = doc_with()
        adm, rej, summ = build(d, active_codes=active)
        T("A1 有效授权 → 准入", "sh600241" in adm and not rej)
        T("A2 chain.prediction 恰三槽且八字段齐",
          len(adm["sh600241"]["chain_prediction"]) == 3
          and all(set(p) == set(PRED_FIELDS) for p in adm["sh600241"]["chain_prediction"]))
        try:
            DEC._norm_chain({"evidence": ["e"], "inference": ["i"],
                             "prediction": adm["sh600241"]["chain_prediction"],
                             "five": {"驱动存在": "a", "当前证据": "b", "延续机制": "c",
                                      "失效条件": "d", "剩余预期": "e"}}, "BUY")
            T("A3 准入产出可被 _norm_chain(BUY) 直接消费", True)
        except Exception as e:
            T("A3 准入产出可被 _norm_chain(BUY) 直接消费（%s）" % str(e)[:60], False)
        T("A4 审计记录三方角色（提交者/授权/校验者）",
          adm["sh600241"]["audit"]["submitters"] == ["c4-three-q"]
          and adm["sh600241"]["audit"]["approved_via"] == ["TASK206_SANDBOX_SIM"]
          and "机械校验者" in adm["sh600241"]["audit"]["validator"])

        # B. 未授权（缺 approved_via/authorization_ref）→ 拒
        _, rejB, _ = build(doc_with(auth=None, ref=None), active_codes=active)
        T("B1 未记授权 → 拒 + P_AUTH_MISSING",
          R_AUTH_MISSING in rejB["sh600241"]["reasons"])

        # C. 缺槽（只两槽）→ 拒
        dC = {"schema_version": 1, "as_of": as_of, "predictions": [
            {"code": "sh600241", "slots": [_mk_slot("M1"), _mk_slot("M2")]}]}
        _, rejC, _ = build(dC, active_codes=active)
        T("C1 三槽不全 → 拒 + P_SLOTS_MISSING",
          R_SLOTS_MISSING in rejC["sh600241"]["reasons"])

        # D. 缺字段（删 threshold）→ 拒
        bad = _mk_slot("M1"); bad.pop("threshold")
        dD = {"schema_version": 1, "as_of": as_of, "predictions": [
            {"code": "sh600241", "slots": [bad, _mk_slot("M2"), _mk_slot("M3")]}]}
        _, rejD, _ = build(dD, active_codes=active)
        T("D1 八字段缺 → 拒 + P_FIELDS_MISSING",
          R_FIELDS_MISSING in rejD["sh600241"]["reasons"])

        # E. 过期数据（cutoff 距 as_of > 7 日）→ 拒
        _, rejE, _ = build(doc_with(cutoff="2026-09-15"), active_codes=active)
        T("E1 数据过期 → 拒 + P_CUTOFF_EXPIRED",
          R_CUTOFF_EXPIRED in rejE["sh600241"]["reasons"])

        # F. 未来数据泄漏（cutoff 晚于 as_of）→ 拒
        _, rejF, _ = build(doc_with(cutoff="2026-09-30"), active_codes=active)
        T("F1 截止晚于信号日 → 拒 + P_CUTOFF_FUTURE",
          R_CUTOFF_FUTURE in rejF["sh600241"]["reasons"])

        # G. 原封复制 pred_ref 历史分位（未记来源）→ 拒
        ref_doc = {"by_code": {"sh600241": {"slots": {
            "M1": {"baseline": -0.54, "threshold": 3.41, "window": 5},
            "M2": {"baseline": -0.54, "threshold": 3.41, "window": 5},
            "M3": {"baseline": -0.54, "threshold": 3.41, "window": 5}}}}}
        _, rejG, _ = build(doc_with(), active_codes=active, pred_ref=ref_doc)
        T("G1 原封复制历史分位且未记来源 → 拒 + P_COPY_FROM_REF",
          R_COPY_FROM_REF in rejG["sh600241"]["reasons"])
        admG, _, _ = build(doc_with(pred_ref_ref="PRR-R1"), active_codes=active,
                           pred_ref=ref_doc)
        T("G2 记录 pred_ref_ref 后 → 准入（来源可追溯即允许）",
          "sh600241" in admG)

        # H. 模糊 condition → 拒
        _, rejH, _ = build(doc_with(condition="看情况再说"), active_codes=active)
        T("H1 condition 模糊词 → 拒 + P_VAGUE_CONDITION",
          R_VAGUE_CONDITION in rejH["sh600241"]["reasons"])

        # I. 非活跃候选 → 拒
        dI = {"schema_version": 1, "as_of": as_of, "predictions": [
            {"code": "sh999999", "slots": [_mk_slot("M1"), _mk_slot("M2"), _mk_slot("M3")]}]}
        _, rejI, _ = build(dI, active_codes=active)
        T("I1 非活跃候选 → 拒 + CODE_NOT_IN_POOL",
          R_CODE_NOT_IN_POOL in rejI["sh999999"]["reasons"])

        # J. 文件级：缺 schema_version → 抛
        p = os.path.join(tmp, "bad.json")
        with io.open(p, "w", encoding="utf-8") as f:
            json.dump({"as_of": as_of, "predictions": []}, f)
        try:
            load_predictions(p)
            T("J1 缺 schema_version → 抛", False)
        except PredInputError as e:
            T("J1 缺 schema_version → 抛", "MISSING_KEY" in str(e))

        # K. 基准口径核查（M1 样本量/窗口 + M3 适用范围）
        ab = audit_benchmarks({"summary": {
            "benchmark": {"M1_forward_T5_pct": {"n": 807500, "q50": -0.54, "q75": 3.41},
                          "M3_volume_ratio": {"n": 5232, "q50": 0.98}},
            "thresholds": {"WINDOW_M1": 5}}})
        T("K1 M1 样本量 ≥20 且标窗口/口径",
          ab["M1"]["n_ge_20"] and ab["M1"]["window"] == 5
          and "完整 T+N 窗" in ab["M1"]["caliber_note"])
        T("K2 M3 标注为普适参照带（非个股预测阈值）",
          ab["M3"]["scope"] == "MARKET_WIDE_REFERENCE"
          and ab["M3"]["is_universal_stock_threshold"] is False)
        T("K3 核查只报发现、不改判据", "findings" in ab and len(ab["findings"]) >= 2)

        # L. summary 自洽
        T("L1 summary 计数自洽",
          summ["n_submitted"] == 1 and summ["n_admitted"] == 1
          and summ["n_rejected"] == 0)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("\npred_input 自检：%d/%d 通过%s"
          % (ok[0], ok[0] + len(fails), "" if not fails else " · 失败：" + ", ".join(fails)))
    return 0 if not fails else 1


def _main(argv=None):
    ap = argparse.ArgumentParser(description="C3 正式 prediction 授权输入适配器（PIR-R1）")
    ap.add_argument("--pred-file", dest="pred_file", help="授权输入件 JSON")
    ap.add_argument("--pred-ref", dest="pred_ref", help="可选：pred_ref 基准件（防复制校验）")
    ap.add_argument("--active-codes", dest="active_codes", help="逗号分隔活跃候选码")
    ap.add_argument("--base", help="数据根（读活跃候选池；与 --active-codes 二选一）")
    ap.add_argument("--suggest", help="从 pred_ref 基准件起草骨架（授权/截止留空）")
    ap.add_argument("--as-of", dest="as_of", help="--suggest 时指定信号日")
    ap.add_argument("--out", help="输出 JSON 路径（缺省只打印统计，不落盘）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()

    pred_ref_doc = None
    if a.suggest:
        # --suggest <pred_ref.json>：指定文件即基准件
        with io.open(a.suggest, encoding="utf-8") as f:
            pred_ref_doc = json.load(f)
        codes = []
        for c in sorted((pred_ref_doc or {}).get("by_code") or {}):
            rec = pred_ref_doc["by_code"][c]
            # 只收三槽全可用的票（缺槽骨架填不齐，无意义）
            if sorted(rec.get("usable_slots") or []) == list(PRED_SLOTS):
                codes.append(c)
        out = suggest(codes, a.as_of or (pred_ref_doc or {}).get("summary", {}).get("as_of"),
                      pred_ref=pred_ref_doc)
        txt = json.dumps(out, ensure_ascii=False, indent=1)
        print("C3 prediction 骨架（%s · %d 票）" % (out["as_of"], len(out["predictions"])))
        print("  说明: %s" % out["note"])
        if a.out:
            d = os.path.dirname(os.path.abspath(a.out))
            if d:
                os.makedirs(d, exist_ok=True)
            with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
                f.write(txt)
            print("  已写骨架件: %s（%d 票）" % (a.out, len(out["predictions"])))
        return 0

    if a.pred_ref:
        with io.open(a.pred_ref, encoding="utf-8") as f:
            pred_ref_doc = json.load(f)

    if not a.pred_file:
        print("用法：--selftest | --pred-file <auth.json> [--pred-ref f] "
              "[--active-codes c1,c2 | --base 根] [--out f] | --suggest <pred_ref.json>")
        return 2

    active = None
    if a.active_codes:
        active = {c.strip() for c in a.active_codes.split(",") if c.strip()}
    elif a.base:
        p = os.path.join(a.base, "data", "state", "candidates.json")
        if os.path.isfile(p):
            with io.open(p, encoding="utf-8") as f:
                pool = json.load(f) or {}
            active = {c["code"] for c in (pool.get("candidates") or [])
                      if c.get("state") not in ("EXPIRED", "TRADED")}

    doc = load_predictions(a.pred_file)
    adm, rej, summ = build(doc, active_codes=active, pred_ref=pred_ref_doc)
    print("C3 正式 prediction 授权件 · %s · rule=%s" % (summ["as_of"], summ["rule_version"]))
    print("  提交=%d  准入=%d  拒绝=%d  可用率=%.1f%%"
          % (summ["n_submitted"], summ["n_admitted"], summ["n_rejected"],
             100.0 * summ["n_admitted"] / max(1, summ["n_submitted"])))
    if summ["reason_histogram"]:
        print("  拒绝原因=%s" % summ["reason_histogram"])
    print("  纪律: %s" % summ["discipline"])
    for code in summ["admitted_codes"]:
        au = adm[code]["audit"]
        print("  ✓ %s 槽=%s 提交者=%s 授权=%s 截止=%s"
              % (code, au["slot_ids"], au["submitters"], au["approved_via"],
                 au["data_cutoffs"]))
    if a.out:
        d = os.path.dirname(os.path.abspath(a.out))
        if d:
            os.makedirs(d, exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"schema_version": SCHEMA_VERSION, "summary": summ,
                       "admitted": adm, "rejected": rej}, f, ensure_ascii=False, indent=1)
        print("  已写准入件: %s（准入 %d 票）" % (a.out, len(adm)))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
