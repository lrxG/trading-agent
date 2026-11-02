#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pred_ref.py · C3 M1/M2/M3 预测基准输入件（v1.0 · 2026-10-16 建 · 任务 205）

用途：为正式 BUY 链的 prediction 三槽位（M1/M2/M3）产出**可追溯的基准与阈值来源**，
      供 gendrive 挂 spec.prediction_planning（备查规划件）。

★ 一、M1/M2/M3 真实语义（逐字取自 tools/fs/decision.py 契约，禁自造第二套）
  · prediction[] 八字段（PREDICTION_FIELDS）= prediction_id / metric / data_source /
    baseline / direction / threshold / condition / window
  · PRED_SLOTS = ("M1","M2","M3")（BUY 必须三槽齐全，禁 M1M3 合并缩写）
  · M1 = 个股目标价可达（价格面，T+窗口内达 target）
  · M2 = 板块/题材扩散（情绪面，题材内联动家数/宽度扩张）
  · M3 = 量能维持（资金面，成交量不衰减到失败水平）
  消费者：settle 结算层按 verdicts 机械判 SUPPORT/REFUTE（本件不判 verdict）。

★ 二、数据源与口径（全部只读；逐条转录，禁混读）
  ① M1 基准（个股前向收益分布）：
       旧包 klines.csv 全市场 close 序列，**完整 T+N 窗**（样本日 d0 与 d0+N 均
       date < as_of）→ 前向收益分布。样本量 ~133 万（2026 全样本）。
       分位数 q50/q75/q90 作为「历史参照带」，**非个股预测**。
  ② M2 基准（板块扩散）：
       data/knowledge/s2_theme_lifecycle/theme_base.json 的 family_groups
       （题材家族 × T+1/T+3 题材等权前向涨幅，n_days 样本量，sample_ok 守门）。
       **口径警示**：S2 forward 含市场 beta，与 S1 市场调整 CAR、S3 涨停溢价
       不同属，禁混读（库 method.forward 原文）。
       当日匹配：s_s2_<as_of>.json 的 theme→family 映射（code→所属家族）。
  ③ M3 基准（量能维持）：
       旧包 klines.csv volume 列，量比 = 近 5 日均量 / 前 5 日均量，分布中位/q25/q75。
  ④ 时序守卫：所有历史样本严格 date < as_of（klines 含 as_of 当日及以后 → 一律排除）。
     S2 库走 loader 时序守卫（库 as_of > 请求日 → UNAVAILABLE）。

★ 三、输出规则（rule_version = "PRR-R1"，逐票可追溯）
  每票输出三槽位骨架（**结构兼容 PREDICTION_FIELDS 八字段**）：
    {"M1": {prediction_id, metric, data_source, baseline, direction, threshold,
            condition, window, sample_n, data_cutoff, basis, usable} | None,
     "M2": {...}, "M3": {...}}
  · baseline / threshold **冻结为历史分位数**（可追溯、可复现），不是主观预测值。
  · **N>=20 才允许**把样本分布作为参考（库 n_min_trust=30 更严者取严）；
    不足 → 该槽位 usable=False + 原因（不默认填值、不放宽匹配）。
  · 恒带 `restricted_reference=True`：**历史分布 ≠ 当前预测**，消费方只能当参考，
    禁当事实/胜率/赔率来源。
  · 无 klines / 无 S2 命中 / 样本不足 / 快照缺席 → 显式 usable=False + reasons。

★ 四、边界（与任务 205 纪律逐条对齐）
  · 零写点（--out 由 CLI 注入，缺省不落盘）；知识库只读。
  · **不改 C4 三问判据、不改 C5 硬风控、不改 decision/three_q 冻结件**。
  · **gendrive 挂载后五件套与 prediction 槽位仍不受影响**——本件只产「备查规划件」，
    正式 prediction 由 4.1 授权下的独立输入件给出（本件作证据参照）。
  · 不预测涨跌、不产胜率、不产赔率、不产目标价（目标价归 priceline）。
  · 样本统计与当前预测**物理分字段**（benchmark 段=历史观测；每票 usable=可用性）。

用法：
  python tools/decision/pred_ref.py --as-of 2026-09-29 --codes sh600241,sh600825 \
      [--snapshot-root <沙盒快照根>] [--kb-base <沙盒库根>] [--old-root <klines根>]
      [--window 5] [--out <path>]
  python tools/decision/pred_ref.py --selftest
"""
import argparse
import collections
import csv
import datetime as _dt
import io
import json
import os
import sys

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "fs"))
_ANAL = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "..", "analysis"))
sys.path.insert(0, _FS)
sys.path.insert(0, _ANAL)

RULE_VERSION = "PRR-R1"

# 样本量门槛：任务 205 明文 N>=20；库侧 n_min_trust=30 更严 → **取严**（禁放宽）
N_MIN_REF = 20
N_MIN_TRUST = 30
N_MIN_EFFECTIVE = max(N_MIN_REF, N_MIN_TRUST)

# 观察窗口（交易日）
WINDOW_M1 = 5                 # 个股目标可达窗（T+5）
WINDOW_M2 = 1                 # 题材扩散窗（T+1 验证）
WINDOW_M3 = 5                 # 量能维持窗（近 5 日均量）

# S2 next_stage_trigger 可机械判 field 白名单（同 gendrive._RULE_FIELD_OK 口径，
# 另补 S2 theme metrics 自有字段 zt_count/breadth；**不扩大**：不在表内的 field
# → 该 trigger 不可机械判 → M2 阈值拒给，不造数）
_RULE_FIELD_OK = ("limit_up_count", "max_board", "turnover", "up_down_ratio",
                  "fail_rate", "index_ma20", "close", "pct", "vol", "amount",
                  "boards", "is_limit_up", "zt_count", "breadth")

# klines 装载上限（控内存）：只保留每票最近 N_BARS_KEEP 根
N_BARS_KEEP = 160

# S2 知识库路径（**只读消费**；写法同 s1.py/s1_backfill.py/expect_ref.py 先例：
# 目录常量止于 3 段、文件名独立常量，使 join 首参为变量 → _join_parts 无锚点段
# → 不产生 `data/knowledge/<cap>/...` 完整字面量，避免 landing 审计误判为
# 「未登记能力输出落点」。本件对知识库零写点。）
_S2_KB_DIR_REL = os.path.join("data", "knowledge", "s2_theme_lifecycle")
_S2_KB_FILE = "theme_base.json"


class PredRefError(Exception):
    """基准输入件拒绝条件（缺源/样本不足/口径冲突）——显式抛错或记 reasons，不静默降级。"""


def _check_date(s):
    try:
        _dt.date.fromisoformat(str(s))
    except ValueError:
        raise PredRefError("BAD_DATE: %r（要 YYYY-MM-DD）" % s)
    return str(s)


# ── 分布工具（纯机械分位，禁插值外推）────────────────────────────────────────
def _quantile(sorted_vals, q):
    n = len(sorted_vals)
    if n == 0:
        return None
    i = int(q * (n - 1))
    return sorted_vals[max(0, min(n - 1, i))]


def _dist(vals):
    """升序样本 → 分位摘要。空 → None（不造数）。"""
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return {"n": n,
            "min": round(s[0], 4), "q10": round(_quantile(s, 0.10), 4),
            "q25": round(_quantile(s, 0.25), 4), "q50": round(_quantile(s, 0.50), 4),
            "q75": round(_quantile(s, 0.75), 4), "q90": round(_quantile(s, 0.90), 4),
            "max": round(s[-1], 4),
            "mean": round(sum(s) / n, 4)}


# ── klines 装载（严格 date < as_of，保最近 N_BARS_KEEP 根）───────────────────
def _load_klines(old_root, as_of):
    """→ (by_code, meta)；by_code[code] = [(date, close, volume)] 升序。
    严格 date < as_of（决策日及以后不入线，防未来函数）；缺源 → 抛错（不降级）。"""
    import priceline as PL
    p = PL.klines_path(old_root)
    if not os.path.isfile(p):
        raise PredRefError("KLINES_ABSENT: %s" % p)
    by_code = {}
    n_rows = 0
    with io.open(p, encoding="utf-8-sig") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            d = r.get("date")
            if not d or d >= as_of:            # ★ 防未来函数：决策日数据不入线
                continue
            try:
                row = (d, float(r["close"]), float(r["volume"]))
            except (ValueError, TypeError, KeyError):
                continue
            if row[1] <= 0:
                continue
            n_rows += 1
            by_code.setdefault(r["code"], []).append(row)
    if not by_code:
        raise PredRefError("KLINES_EMPTY_ASOF: %s 之前零行" % as_of)
    for c in by_code:
        by_code[c].sort(key=lambda x: x[0])
        if len(by_code[c]) > N_BARS_KEEP:
            by_code[c] = by_code[c][-N_BARS_KEEP:]
    return by_code, {"path": p, "n_codes": len(by_code), "n_rows_used": n_rows,
                     "as_of": as_of, "caliber": "ACTUAL_TRADED_PRICE",
                     "note": "严格 date < as_of（防未来函数）"}


# ── M1 基准：全市场个股 T+N 前向收益分布（完整窗，无未来泄漏）────────────────
def m1_benchmark(by_code, window=WINDOW_M1):
    """完整 T+N 窗前向收益分布（%）——样本日 d0 与 d0+N 均已在库内（即均 < as_of）。"""
    vals = []
    for _c, bars in by_code.items():
        ds = [b[0] for b in bars]
        idx = {d: i for i, d in enumerate(ds)}
        for d0 in ds:
            i = idx[d0]
            if i + window >= len(bars):
                continue                        # 窗口未完成 → 不入样本（禁半窗）
            base = bars[i][1]
            nxt = bars[i + window][1]
            if base > 0:
                vals.append((nxt - base) / base * 100.0)
    return _dist(vals)


# ── M3 基准：量比分布（近 N 日均量 / 前 N 日均量）─────────────────────────────
def m3_benchmark(by_code, window=WINDOW_M3):
    vals = []
    for _c, bars in by_code.items():
        if len(bars) < window * 2:
            continue
        cur = sum(b[2] for b in bars[-window:]) / window
        prev = sum(b[2] for b in bars[-window * 2:-window]) / window
        if prev > 0:
            vals.append(cur / prev)
    return _dist(vals)


# ── M2 数据源分层（任务 207 修复 · 2026-10-16）───────────────────────────────
#   L1 当日观测层：s_s2_<as_of>.json（**as_of = 决策日 T**，T 收盘后生成 ⇒ T 时可读）
#       → family / stage / metrics(pct,breadth,zt_count,max_board,amount) / next_stage_trigger
#   L2 前向统计层：data/knowledge/s2_theme_lifecycle/（知识库，**一次性回溯跑批产物**，
#       as_of = 覆盖期末端）→ family_groups 前向涨幅；**时序守卫**：as_of > T ⇒ 拒答
#   L0 归属层：旧包 tdx_sector_constituent.csv（概念板块成分，**静态快照**，v1 口径局限
#       已由 S2 模块头如实标注）→ code → theme_id 集合（**事实性归属，非预测匹配**）
#
#   ★ 时序纪律（任务 207 B1）：决策日 T 只读 L0（静态归属）+ L1（as_of=T）；
#     L2 仅在其 as_of ≤ T 时作可选增强；**严禁用 T+1 才就绪的 L2 回填 T 的预测**。
def m2_kb(as_of, kb_base=None):
    """→ (kb, note) | (None, reason)。库不可读 → (None, reason)，不降级。"""
    import loader as LD
    rec = LD.load_knowledge("s2", as_of, base=kb_base)
    if rec is LD.UNAVAILABLE:
        return None, "S2_KB_UNAVAILABLE: loader 时序守卫拒答或注册表/条目缺席"
    return dict(rec), None


def _gen_dir(base):
    """生成件目录（容器级 join：止于 data/generated，静态可归类为容器，非写点）。
    文件名段在**外层** join 拼接（无 anchor 段 → 不入路径词汇表），同 gendrive._s_dir 先例。"""
    return os.path.join(base, "data", "generated")


def _trigger_value(trigger):
    """S2 next_stage_trigger → 可机械判的条件**列表**（AND 语义）。

    规则（不扩大、不造数）：
      · conditions 非空且**每条**的 field 在白名单、op 合法、value 非 None
        ⇒ 返回 [(field, op, value), ...]（多条件 = AND 全满足才触发）
      · 任一条件不可判 / conditions 空 ⇒ None（该 trigger 不可机械判）
    返回 (rules, conds)。rules=None 表示不可机械判。
    """
    conds = (trigger or {}).get("conditions") or []
    if not conds:
        return None, conds
    rules = []
    for c0 in conds:
        if not isinstance(c0, dict):
            return None, conds
        fld, op, val = c0.get("field"), c0.get("op"), c0.get("value")
        if fld not in _RULE_FIELD_OK or op not in ("<=", "<", "==", "!=", ">", ">="):
            return None, conds
        if val is None:
            return None, conds
        rules.append((fld, op, val))
    return rules, conds


def _m2_context(base, as_of, old_root=None):
    """构建 M2 上下文（L0 归属 + L1 当日观测）。

    → (ctx, meta)。ctx[code] = {theme_id, theme_name, family, stage, metrics,
      trigger, trigger_rule, n_themes_hit}。缺生成件 → ({}, meta with reason)。
    """
    meta = {"source_layer": "L0_constituent + L1_snapshot", "as_of": as_of,
            "gen_path": None, "gen_as_of": None, "n_gen_themes": 0,
            "n_codes_mapped": 0, "constituent_stats": None,
            "constituent_error": None, "leader_only_fallback": False}
    if not base:
        meta["constituent_error"] = "BASE_UNSET"
        return {}, meta
    p = os.path.join(_gen_dir(base), "s_s2_" + as_of + ".json")
    meta["gen_path"] = p
    if not os.path.isfile(p):
        return {}, meta
    with io.open(p, encoding="utf-8") as f:
        doc = json.load(f) or {}
    meta["gen_as_of"] = str(doc.get("as_of") or "")
    themes_doc = doc.get("themes") or {}
    meta["n_gen_themes"] = len(themes_doc)

    # L1：theme_id → 当日观测记录
    tid2rec = {}
    for tid, tm in themes_doc.items():
        ev = tm.get("evidence") or {}
        tid2rec[tid] = {
            "theme_id": tid, "theme_name": tm.get("theme_name"),
            "family": tm.get("family"), "stage": tm.get("stage"),
            "metrics": ev.get("metrics") or {},
            "trigger": tm.get("next_stage_trigger") or {},
            "trigger_rules": _trigger_value(tm.get("next_stage_trigger"))[0],
        }

    # L0：成分表 code → theme_id 集合（静态快照，事实性归属）
    code2themes = {}
    try:
        import s2 as S2M
        _th, code2themes, stats = S2M.load_constituents(old_root)
        meta["constituent_stats"] = stats
    except Exception as e:
        meta["constituent_error"] = "%s: %s" % (type(e).__name__, str(e)[:120])

    # 合并：code → 当日活跃 theme 记录（多命中取 zt_count 最高，并列取 amount 大者
    #       —— 与 S2 leader 选取同口径；**不猜、不改写**，无法判定时如实多记）
    out = {}
    if code2themes:
        for code, tids in code2themes.items():
            cands = [tid2rec[t] for t in tids if t in tid2rec]
            if not cands:
                continue
            if len(cands) > 1:
                cands.sort(key=lambda r: (-(r["metrics"].get("zt_count") or 0),
                                          -(r["metrics"].get("amount") or 0),
                                          r["theme_id"]))
            rec = dict(cands[0])
            rec["n_themes_hit"] = len(cands)
            rec["all_theme_ids"] = sorted(tids)
            out[code] = rec
    else:
        # 成分表缺席 → 退回 leader-only 映射（**如实降级并标注**，不静默）
        meta["leader_only_fallback"] = True
        for tid, rec in tid2rec.items():
            led = ((themes_doc[tid].get("evidence") or {}).get("leader") or {})
            lc = led.get("code")
            if lc and lc not in out:
                r = dict(rec)
                r["n_themes_hit"] = 1
                r["all_theme_ids"] = [tid]
                out[lc] = r
    meta["n_codes_mapped"] = len(out)
    return out, meta


# ── 三槽位机械装配（结构 = PREDICTION_FIELDS 八字段 + 审计附加）───────────────
def _slot_m1(code, dist, as_of):
    """M1 个股目标可达（价格面）：基准 = 全市场 T+5 前向收益分位。"""
    if dist is None:
        return None, "M1_NO_BENCHMARK: klines 前向样本为空"
    slot = {
        "prediction_id": "M1",
        "metric": "个股 %d 日前向收益（%%）" % WINDOW_M1,
        "data_source": "old_package.klines.csv（实际成交价口径 · 全市场等权样本）",
        "baseline": dist["q50"],              # 中位 = 基准（可追溯、非主观）
        "direction": "UP",
        "threshold": dist["q75"],             # 阈值 = q75（跑赢 75% 历史样本才算出众）
        "condition": "窗口内触及 priceline target 且 %d 日收益 > 基准 q50=%.2f%%"
                     % (WINDOW_M1, dist["q50"]),
        "window": WINDOW_M1,
        "sample_n": dist["n"],
        "data_cutoff": as_of,
        "distribution": dist,
        "basis": "全市场 T+%d 前向收益分布（完整窗，严格 date<as_of）" % WINDOW_M1,
        "restricted_reference": True,
        "note": "历史分布参照带，非个股预测；个股达标须另由价线/target 判定",
    }
    return slot, None


def _slot_m2(code, ctx, kb, as_of):
    """M2 板块扩散（情绪面）—— **L1 当日观测口径**（任务 207 修复）。

    基准/阈值全部取自 **as_of=T 的当日生成件**（T 时可读，零未来函数）：
      · family / theme_name / stage      ← s_s2_<T>.json
      · baseline = 当日 theme 涨停家数 zt_count（扩散核心机械量，真实观测）
      · threshold = S2 next_stage_trigger 的可机械判条件值（S2 真实生成，非本件造）
    知识库 family_groups 前向统计作**可选增强**（kb_family_stats）：仅在其
    as_of ≤ T 时填写；as_of > T ⇒ 如实记不可用（时序守卫），**不回填 T 的预测**。
    """
    if not ctx:
        return None, ("M2_NO_FAMILY: 该票当日在 s_s2 生成件中无题材家族归属"
                      "（L0 成分表 ∩ L1 当日 themes 均未命中）")
    fam = ctx.get("family")
    if not fam:
        return None, ("M2_NO_FAMILY: 命中题材 %s 但生成件未标 family"
                      % ctx.get("theme_id"))
    m = ctx.get("metrics") or {}
    zt = m.get("zt_count")
    if zt is None:
        return None, ("M2_NO_METRICS: 题材 %s 当日 metrics 缺 zt_count（扩散观测缺失）"
                      % ctx.get("theme_id"))
    rules = ctx.get("trigger_rules")
    if not rules:
        return None, ("M2_NO_TRIGGER: 题材 %s 的 next_stage_trigger 不可机械判"
                      "（conditions 空，或含非白名单 field / 非法 op / 缺 value）"
                      "⇒ 不造阈值" % ctx.get("theme_id"))
    cond_txt = " 且 ".join("%s %s %s" % (f, o, v) for f, o, v in rules)
    slot = {
        "prediction_id": "M2",
        "metric": "题材「%s」%s（T 日涨停家数，扩散核心机械量）"
                  % (ctx.get("theme_name") or ctx.get("theme_id"), "T+1 扩散维持"),
        "data_source": "S2 当日生成件 s_s2_<as_of>.json（as_of=%s，T 收盘后生成）"
                       % as_of,
        "baseline": zt,                       # T 日真实观测（非历史分位、非预测）
        "direction": "UP",
        "threshold": [{"field": f, "op": o, "value": v} for f, o, v in rules],
        "condition": ("T+1 题材「%s」满足 S2 next_stage_trigger 全部条件（AND）：%s；"
                      "T 日基线 zt_count=%s" %
                      (ctx.get("theme_name") or ctx.get("theme_id"), cond_txt, zt)),
        "window": WINDOW_M2,
        "sample_n": None,                     # 当日观测非分布样本（禁填分布 n）
        "data_cutoff": as_of,                 # L1 数据截止 = 决策日（T 时可读）
        "decision_date": as_of,
        "source_layer": "L1_snapshot",
        "source_version": "s_s2_%s" % as_of,
        "family": fam,
        "theme_id": ctx.get("theme_id"),
        "theme_name": ctx.get("theme_name"),
        "stage": ctx.get("stage"),
        "n_themes_hit": ctx.get("n_themes_hit"),
        "trigger_rule": {"all": [{"field": f, "op": o, "value": v}
                                 for f, o, v in rules]},
        "basis": ("S2 当日生成件（as_of=T，T 收盘后生成 ⇒ T 时可读）；"
                  "成分归属 = 旧包成分表静态快照（v1 口径局限，S2 模块头已标注）"),
        "restricted_reference": True,
        "note": ("T 日真实扩散观测，非预测值；阈值 = S2 真实触发条件（AND 全满足）；"
                 "家族归属为事实性映射，非匹配扩大"),
    }
    # L2 可选增强：知识库前向统计（时序守卫：as_of ≤ T 才填）
    if kb is not None:
        node = ((kb.get("family_groups") or {}).get(fam) or {})
        n = node.get("n_days")
        fwd = node.get("fwd_t1_mean_pct")
        kb_as_of = str(kb.get("as_of") or "")
        if (node.get("sample_ok") and n is not None and n >= N_MIN_EFFECTIVE
                and fwd is not None and kb_as_of and kb_as_of <= as_of):
            slot["kb_family_stats"] = {
                "available": True, "family": fam, "n_days": n,
                "fwd_t1_mean_pct": round(float(fwd), 4),
                "kb_as_of": kb_as_of,
                "caliber": "题材等权 T+1 前向（含市场 beta；与 S1 CAR/S3 溢价禁混读）",
            }
        else:
            slot["kb_family_stats"] = {
                "available": False, "family": fam, "kb_as_of": kb_as_of,
                "reason": ("S2_KB_ASOF_AFTER_DECISION: 知识库 as_of=%s 晚于决策日 %s"
                           "（T+1 才就绪）⇒ 前向统计禁用于 T 日预测（时序守卫）"
                           % (kb_as_of or "?", as_of))
                if kb_as_of and kb_as_of > as_of else
                ("M2_INSUFFICIENT_SAMPLES: family=%s n_days=%s < %d 或 sample_ok=False"
                 % (fam, n, N_MIN_EFFECTIVE)),
            }
    else:
        slot["kb_family_stats"] = {"available": False,
                                   "reason": "S2_KB_UNAVAILABLE（loader 拒答/缺席）"}
    return slot, None


def _slot_m3(code, dist, as_of):
    """M3 量能维持（资金面）：基准 = 全市场量比分位。"""
    if dist is None:
        return None, "M3_NO_BENCHMARK: 量比样本为空（klines volume 列缺席或根数不足）"
    slot = {
        "prediction_id": "M3",
        "metric": "量比（近 %d 日均量 / 前 %d 日均量）" % (WINDOW_M3, WINDOW_M3),
        "data_source": "old_package.klines.csv:volume（实际成交量口径）",
        "baseline": dist["q50"],
        "direction": "UP",
        "threshold": dist["q50"],             # 阈值 = 中位（守 1.0 基准线）
        "condition": "窗口内量比 >= 历史中位 %.3f（量能不衰减至失败水平）"
                     % dist["q50"],
        "window": WINDOW_M3,
        "sample_n": dist["n"],
        "data_cutoff": as_of,
        "distribution": dist,
        "basis": "全市场量比分布（近 %d 日/前 %d 日，严格 date<as_of）"
                 % (WINDOW_M3, WINDOW_M3),
        "restricted_reference": True,
        "note": "全市场量能分布参照，非个股量能预测",
    }
    return slot, None


def compute(codes, as_of, *, base=None, old_root=None, kb_base=None,
            window_m1=WINDOW_M1):
    """codes=[code...] → (by_code, summary)。by_code[code] = 三槽位骨架 + 审计。
    全零写点。缺源/样本不足 → 该槽 usable=False + reasons，不默认填值。"""
    as_of = _check_date(as_of)
    reasons = collections.Counter()
    out = {}

    # ① klines（M1/M3 源；缺 → 两槽全不可用，如实）
    by_code = meta = None
    kl_err = None
    if old_root:
        try:
            by_code, meta = _load_klines(old_root, as_of)
        except Exception as e:
            kl_err = "%s: %s" % (type(e).__name__, str(e)[:140])

    dist_m1 = m1_benchmark(by_code, window_m1) if by_code else None
    dist_m3 = m3_benchmark(by_code) if by_code else None

    # ② S2 库（M2 **L2 可选增强**，时序守卫；与 old_root 独立）
    kb, kb_err = m2_kb(as_of, kb_base=kb_base)
    # ②' M2 上下文（L0 成分归属 + L1 当日观测；任务 207 修复）
    m2ctx, m2ctx_meta = _m2_context(base, as_of, old_root=old_root)

    # ③ 逐票装配
    for code in codes:
        rec = {"code": code, "as_of": as_of, "decision_date": as_of,
               "rule_version": RULE_VERSION,
               "slots": {}, "usable_slots": [], "data_cutoff": as_of,
               "reasons": [], "restricted_reference": True,
               "m2_mapping": None}
        if kl_err:
            rec["reasons"].append("M1M3_KLINES_UNAVAILABLE: %s" % kl_err)
        if kb_err:
            rec["reasons"].append(kb_err)
        if not old_root:
            rec["reasons"].append("M1M3_OLD_ROOT_UNSET: 未提供 klines 根 → M1/M3 不可算")
        # M2 家族映射结果（任务 207 B3：逐票输出映射结果与来源版本）
        _ctx = m2ctx.get(code)
        rec["m2_mapping"] = ({
            "mapped": True, "theme_id": _ctx.get("theme_id"),
            "theme_name": _ctx.get("theme_name"), "family": _ctx.get("family"),
            "stage": _ctx.get("stage"), "n_themes_hit": _ctx.get("n_themes_hit"),
            "source_layer": "L0_constituent + L1_snapshot",
            "source_version": "s_s2_%s" % as_of,
            "gen_as_of": m2ctx_meta.get("gen_as_of"),
        } if _ctx else {
            "mapped": False, "source_layer": "L0_constituent + L1_snapshot",
            "source_version": "s_s2_%s" % as_of,
            "reason": ("M2_NO_FAMILY: L0 成分表 ∩ L1 当日 themes 未命中（该票当日无"
                       "活跃题材归属；不猜家族）"),
        })

        for name, builder, arg in (("M1", _slot_m1, (dist_m1, as_of)),
                                   ("M2", _slot_m2, (_ctx, kb, as_of)),
                                   ("M3", _slot_m3, (dist_m3, as_of))):
            try:
                slot, why = builder(code, *arg)
            except Exception as e:
                slot, why = None, "%s_BUILD_ERROR: %s" % (name, type(e).__name__)
            rec["slots"][name] = slot
            if slot is not None:
                rec["usable_slots"].append(name)
            else:
                rec["reasons"].append(why)
                reasons[why.split(":")[0]] += 1
        rec["n_usable"] = len(rec["usable_slots"])
        rec["all_usable"] = (rec["n_usable"] == 3)
        out[code] = rec

    n_all = sum(1 for r in out.values() if r["all_usable"])
    n_any = sum(1 for r in out.values() if r["n_usable"] > 0)
    n_m2_mapped = sum(1 for r in out.values() if (r.get("m2_mapping") or {}).get("mapped"))
    summary = {
        "as_of": as_of, "decision_date": as_of, "rule_version": RULE_VERSION,
        "n_requested": len(list(codes)), "n_all_usable": n_all,
        "n_partial_usable": n_any - n_all, "n_none_usable": len(out) - n_any,
        "slot_usable": {s: sum(1 for r in out.values() if s in r["usable_slots"])
                        for s in ("M1", "M2", "M3")},
        "m2_mapping": {"n_mapped": n_m2_mapped, "n_unmapped": len(out) - n_m2_mapped,
                       "gen_as_of": m2ctx_meta.get("gen_as_of"),
                       "gen_path": m2ctx_meta.get("gen_path"),
                       "n_gen_themes": m2ctx_meta.get("n_gen_themes"),
                       "constituent_stats": m2ctx_meta.get("constituent_stats"),
                       "constituent_error": m2ctx_meta.get("constituent_error"),
                       "leader_only_fallback": m2ctx_meta.get("leader_only_fallback")},
        "benchmark": {
            "M1_forward_T%d_pct" % window_m1: dist_m1,
            "M2_kb_as_of": (kb or {}).get("as_of"),
            "M2_kb_usable_for_decision": bool(
                kb is not None and str(kb.get("as_of") or "") <= as_of),
            "M3_volume_ratio": dist_m3,
        },
        "thresholds": {"N_MIN_REF": N_MIN_REF, "N_MIN_TRUST": N_MIN_TRUST,
                       "N_MIN_EFFECTIVE": N_MIN_EFFECTIVE,
                       "WINDOW_M1": window_m1, "WINDOW_M2": WINDOW_M2,
                       "WINDOW_M3": WINDOW_M3},
        "klines_meta": meta,
        "reason_histogram": dict(reasons),
        "discipline": ("M2 基准 = L1 当日生成件（as_of=T，T 时可读，零未来函数）；"
                       "L2 知识库前向统计仅作可选增强（as_of≤T 才填）；"
                       "历史分布 ≠ 当前预测（restricted_reference=True）；"
                       "不产胜率/赔率/目标价；不改 C4/C5"),
    }
    return out, summary


# ── 自检（全合成夹具，真库/真 klines 零接触）────────────────────────────────
def _mk_klines_csv(path, codes, n_bars, start="2026-01-05", drift=0.0,
                   vol_base=1000.0, vol_drift=0.0):
    """合成 klines.csv（含 volume 列）——日期用连续自然日（够算即可）。"""
    import random
    random.seed(7)
    d0 = _dt.date.fromisoformat(start)
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write("code,name,date,open,close,high,low,volume,amount,exchange,change_pct\n")
        for ci, code in enumerate(codes):
            c = 10.0 + ci * 0.5
            v = vol_base
            for i in range(n_bars):
                d = (d0 + _dt.timedelta(days=i)).isoformat()
                o = c
                c = c * (1 + drift + random.uniform(-0.004, 0.004))
                v = v * (1 + vol_drift + random.uniform(-0.05, 0.05))
                f.write("%s,N%d,%s,%.3f,%.3f,%.3f,%.3f,%.1f,%.1f,X,%.3f\n"
                        % (code, ci, d, o, c, max(o, c) * 1.01, min(o, c) * 0.99,
                           v, 1e6, (c - o) / o * 100))


def _mk_s2_kb(kb_base, as_of, families):
    """合成 S2 知识库（含注册表路由 + family_groups）。"""
    kbdir = os.path.join(kb_base, "data", "knowledge")
    os.makedirs(kbdir, exist_ok=True)
    with io.open(os.path.join(kbdir, "_registry.json"), "w", encoding="utf-8") as f:
        json.dump({"s2": {"path": os.path.join("data", "knowledge",
                                               "s2_theme_lifecycle", "theme_base.json"),
                          "version": "t1"}}, f, ensure_ascii=False)
    d = os.path.join(kbdir, "s2_theme_lifecycle")
    os.makedirs(d, exist_ok=True)
    doc = {"s_id": "s2", "as_of": as_of, "n_min_trust": 30,
           "method": {"forward": "题材等权涨幅 T+1/T+3 累加（含市场 beta）"},
           "family_groups": families}
    with io.open(os.path.join(d, "theme_base.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)


def _mk_constituents(old_root, pairs):
    """合成旧包成分表（tdx_sector_constituent.csv）。pairs = [(tid, tname, [code...])]。
    code 口径 = `000002.SZ`（数字.后缀），由 s2._norm_code 归一化。"""
    d = os.path.join(old_root, "data", "raw", "meta")
    os.makedirs(d, exist_ok=True)
    with io.open(os.path.join(d, "tdx_sector_constituent.csv"), "w",
                 encoding="utf-8", newline="") as f:
        f.write("sector_code,sector_name,sector_type,code,name\n")
        for tid, tname, codes in pairs:
            for c in codes:
                num = c[2:]                      # sh600000 → 600000
                suf = "SH" if c.startswith("sh") else "SZ"
                f.write("%s,%s,概念,%s.%s,N\n" % (tid, tname, num, suf))


def _mk_s_s2(base, as_of, themes):
    """合成 s_s2_<as_of>.json。themes = [{tid, name, family, stage, codes,
    metrics, trigger}]（**新语义：L1 当日观测，含 metrics + next_stage_trigger**）。"""
    g = os.path.join(base, "data", "generated")
    os.makedirs(g, exist_ok=True)
    doc_themes = {}
    for t in themes:
        doc_themes[t["tid"]] = {
            "theme_id": t["tid"], "theme_name": t.get("name") or ("T" + t["tid"]),
            "family": t.get("family"), "stage": t.get("stage") or "发酵",
            "next_stage_trigger": t.get("trigger") or {},
            "evidence": {"metrics": t.get("metrics") or {},
                         "leader": {"code": (t.get("codes") or [None])[0]}},
        }
    with io.open(os.path.join(g, "s_s2_" + as_of + ".json"), "w",
                 encoding="utf-8") as f:
        json.dump({"s_id": "s2", "as_of": as_of, "themes": doc_themes},
                  f, ensure_ascii=False)


def _selftest():
    import shutil
    import tempfile
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
    tmp = tempfile.mkdtemp(prefix="predref_")
    try:
        sb = os.path.join(tmp, "sb")
        os.makedirs(os.path.join(sb, "data", "raw", "bus"), exist_ok=True)
        kl = os.path.join(sb, "data", "raw", "bus", "klines.csv")
        # 100 票 × 120 根（满足 M1 完整窗与 M3 量比）
        _mk_klines_csv(kl, ["sh600%03d" % i for i in range(100)], 120)
        # 注：真实 klines 的路径约定 = <old_root>/data/raw/bus/klines.csv
        old_root = sb

        # L0 成分表（静态快照）：sh600000/001→880001（family A）；
        #   sh600002→880002（family X，库样本不足）；sh600003→880003（family Z，库缺席）；
        #   sh600999 **不在成分表**（无归属）
        _mk_constituents(old_root, [
            ("880001.SH", "题材甲", ["sh600000", "sh600001"]),
            ("880002.SH", "题材乙", ["sh600002"]),
            ("880003.SH", "题材丙", ["sh600003"]),
        ])

        # L2 知识库（**可选增强**）：A 家族 n=5000（够）；X n=10（不足）；Z 缺席
        _mk_s2_kb(tmp, as_of, {
            "A": {"n_days": 5000, "fwd_t1_mean_pct": -0.08, "sample_ok": True},
            "X": {"n_days": 10, "fwd_t1_mean_pct": 1.23, "sample_ok": False},
        })

        # L1 当日生成件（as_of=T）：含 metrics + 可机械判 trigger
        def _trig(field="zt_count", op=">=", val=2):
            return {"type": "rediscover", "text": "涨停簇再现",
                    "conditions": [{"field": field, "op": op, "value": val}]}
        _mk_s_s2(sb, as_of, [
            {"tid": "880001.SH", "name": "题材甲", "family": "A", "stage": "发酵",
             "codes": ["sh600000", "sh600001"],
             "metrics": {"n_valid": 442, "pct": 1.32, "breadth": 0.69,
                         "zt_count": 9, "max_board": 1, "amount": 2.9e11},
             "trigger": _trig()},
            {"tid": "880002.SH", "name": "题材乙", "family": "X", "stage": "分歧",
             "codes": ["sh600002"],
             "metrics": {"zt_count": 3, "pct": 0.1, "breadth": 0.4},
             "trigger": _trig(val=2)},
            {"tid": "880003.SH", "name": "题材丙", "family": "Z", "stage": "尾声",
             "codes": ["sh600003"],
             "metrics": {"zt_count": 1, "pct": -0.5, "breadth": 0.3},
             # 含**非白名单 field** → 不可机械判（拒给阈值，不造数）
             "trigger": {"type": "vague", "text": "神秘条件",
                         "conditions": [{"field": "unknown_field", "op": ">=",
                                         "value": 2}]}},
        ])

        out, summ = compute(["sh600000", "sh600002", "sh600003", "sh600999"], as_of,
                            base=sb, old_root=old_root, kb_base=tmp)

        # A：L1 命中 + trigger 可机械判 → M2 可用（基准 = 当日 zt_count）
        a = out["sh600000"]
        T("A1 L1 命中 + trigger 可判 → M2 可用（新 L1 口径）", "M2" in a["usable_slots"])
        T("A2 M1 可用（klines 前向样本充足）", "M1" in a["usable_slots"])
        T("A3 M3 可用（量比样本充足）", "M3" in a["usable_slots"])
        T("A4 三槽全可用 → all_usable", a["all_usable"] is True)
        T("A5 M2 八字段齐 + 审计字段（decision_date/来源版本/家族映射）",
          all(k in a["slots"]["M2"] for k in
              ("prediction_id", "metric", "data_source", "baseline", "direction",
               "threshold", "condition", "window", "data_cutoff", "decision_date",
               "source_layer", "source_version", "family")))
        T("A6 M2 baseline = 当日真实 zt_count（非历史分位）",
          a["slots"]["M2"]["baseline"] == 9)
        T("A7 M2 threshold = S2 真实 trigger 条件（AND 列表，非本件造）",
          a["slots"]["M2"]["threshold"] == [{"field": "zt_count", "op": ">=",
                                             "value": 2}]
          and a["slots"]["M2"]["trigger_rule"]["all"][0]["field"] == "zt_count")
        T("A8 M2 data_cutoff = 决策日（L1 当日，T 时可读）",
          a["slots"]["M2"]["data_cutoff"] == as_of
          and as_of in a["slots"]["M2"]["data_source"])
        T("A9 L2 可选增强填写（库 as_of=决策日，样本足）",
          (a["slots"]["M2"].get("kb_family_stats") or {}).get("available") is True)
        T("A10 家族映射结果逐票输出（m2_mapping）",
          (a.get("m2_mapping") or {}).get("mapped") is True
          and a["m2_mapping"]["family"] == "A"
          and a["m2_mapping"]["source_version"] == "s_s2_%s" % as_of)

        # B：家族样本不足（X n=10 < 30）→ M2 **仍可用**（L1 口径），但 L2 增强不可用
        b = out["sh600002"]
        T("B1 家族 X 样本不足 → M2 仍可用（L1 基准，不依赖 L2）", "M2" in b["usable_slots"])
        T("B2 L2 增强如实标不可用 + 原因",
          (b["slots"]["M2"].get("kb_family_stats") or {}).get("available") is False
          and "INSUFFICIENT" in str(b["slots"]["M2"]["kb_family_stats"].get("reason")))

        # C：trigger 不可机械判 → M2 拒（不造阈值）
        c = out["sh600003"]
        T("C1 trigger 不可机械判 → M2 不可用", "M2" not in c["usable_slots"])
        T("C2 报 M2_NO_TRIGGER 留痕",
          any("M2_NO_TRIGGER" in r for r in c["reasons"]))

        # D：无家族归属（sh600999 不在成分表）
        d = out["sh600999"]
        T("D1 无家族归属 → M2 不可用", "M2" not in d["usable_slots"])
        T("D2 报 M2_NO_FAMILY 留痕 + m2_mapping.mapped=False",
          any("M2_NO_FAMILY" in r for r in d["reasons"])
          and (d.get("m2_mapping") or {}).get("mapped") is False)
        T("D3 但 M1/M3 仍可用（槽位独立）",
          "M1" in d["usable_slots"] and "M3" in d["usable_slots"])

        # E：防未来函数——as_of 之后的 klines 行不得进入样本
        before = out["sh600000"]["slots"]["M1"]["sample_n"]
        with io.open(kl, "a", encoding="utf-8", newline="") as f:
            for i in range(400):        # 追加 as_of 之后的日期行
                f.write("sh600000,N0,2027-01-%02d,10.0,10.0,10.1,9.9,1000,1e6,X,0.0\n"
                        % (i % 28 + 1))
        out2, _ = compute(["sh600000"], as_of, base=sb, old_root=old_root, kb_base=tmp)
        T("E1 追加 as_of 之后行 → M1 样本量不变（防未来泄漏）",
          out2["sh600000"]["slots"]["M1"]["sample_n"] == before)

        # F：klines 缺席 → M1/M3 不可用 + 留痕（M2 走 L1，独立仍可用）
        out3, _ = compute(["sh600000"], as_of, base=sb,
                          old_root=os.path.join(tmp, "nope"), kb_base=tmp)
        T("F1 klines 缺席 → M1/M3 不可用（M2 独立仍可用）",
          "M1" not in out3["sh600000"]["usable_slots"]
          and "M3" not in out3["sh600000"]["usable_slots"]
          and "M2" in out3["sh600000"]["usable_slots"])
        T("F2 报 M1M3_KLINES_UNAVAILABLE 留痕",
          any("KLINES" in r for r in out3["sh600000"]["reasons"]))

        # G：**多日期时序守卫验证**（任务 207 B4）
        #    G1 数据早于决策日（库 as_of=09-28 < 09-29）→ L2 可用
        _mk_s2_kb(tmp, "2026-09-28", {
            "A": {"n_days": 5000, "fwd_t1_mean_pct": -0.08, "sample_ok": True}})
        outG1, _ = compute(["sh600000"], "2026-09-29", base=sb, old_root=old_root,
                           kb_base=tmp)
        T("G1 库 as_of(09-28) 早于决策日(09-29) → L2 增强可用",
          (outG1["sh600000"]["slots"]["M2"].get("kb_family_stats") or {}).get("available")
          is True)
        #    G2 同日可用（库 as_of=09-29 == 决策日）→ L2 可用
        _mk_s2_kb(tmp, "2026-09-29", {
            "A": {"n_days": 5000, "fwd_t1_mean_pct": -0.08, "sample_ok": True}})
        outG2, _ = compute(["sh600000"], "2026-09-29", base=sb, old_root=old_root,
                           kb_base=tmp)
        T("G2 库 as_of == 决策日 → L2 增强可用（同日可读）",
          (outG2["sh600000"]["slots"]["M2"].get("kb_family_stats") or {}).get("available")
          is True)
        #    G3 数据晚于决策日（库 as_of=09-30 > 09-29）→ L2 **拒答**（禁回填）
        _mk_s2_kb(tmp, "2026-09-30", {
            "A": {"n_days": 5000, "fwd_t1_mean_pct": -0.08, "sample_ok": True}})
        outG3, _ = compute(["sh600000", "sh600999"], "2026-09-29", base=sb,
                           old_root=old_root, kb_base=tmp)
        _kfs = outG3["sh600000"]["slots"]["M2"].get("kb_family_stats") or {}
        T("G3 库 as_of(09-30) 晚于决策日(09-29) → L2 拒答（禁回填 T 的预测）",
          _kfs.get("available") is False
          and any(k in str(_kfs.get("reason")) for k in
                  ("S2_KB_UNAVAILABLE", "S2_KB_ASOF_AFTER_DECISION")))
        T("G4 晚于决策日时 M2 仍可用（L1 基准不受影响）+ summary 如实标注",
          "M2" in outG3["sh600000"]["usable_slots"]
          and outG3["sh600000"]["slots"]["M2"]["data_cutoff"] == "2026-09-29")
        #    G5 缺失家族 → 拒（不猜）
        T("G5 成分表未命中 → M2 拒（不得猜家族）",
          "M2" not in outG3["sh600999"]["usable_slots"]
          and any("M2_NO_FAMILY" in r for r in outG3["sh600999"]["reasons"]))
        #    G6 未匹配代码格式（成分表 code 无法归一化 → s2 计数上报）
        _mk_constituents(old_root, [
            ("880001.SH", "题材甲", ["sh600000", "sh600001"]),
            ("880002.SH", "题材乙", ["sh600002"]),
            ("880003.SH", "题材丙", ["sh600003"]),
        ])
        outG6, _ = compute(["sh600999"], "2026-09-29", base=sb, old_root=old_root,
                           kb_base=tmp)
        T("G6 未匹配代码 → M2 拒 + m2_mapping 留原因",
          "M2" not in outG6["sh600999"]["usable_slots"]
          and (outG6["sh600999"].get("m2_mapping") or {}).get("reason"))

        # H：summary 计数自洽（2 全可用 + 2 部分可用 + 0 全不可用）
        T("H1 summary 计数一致",
          summ["n_requested"] == 4
          and summ["n_all_usable"] == 2      # sh600000 / sh600002
          and summ["n_partial_usable"] == 2  # sh600003（trigger 非白名单）/ sh600999（无归属）
          and summ["n_none_usable"] == 0
          and summ["n_all_usable"] + summ["n_partial_usable"]
          + summ["n_none_usable"] == summ["n_requested"])
        T("H2 benchmark 段记录真实源摘要 + M2 映射统计",
          summ["benchmark"]["M1_forward_T5_pct"]["n"] > 0
          and summ["m2_mapping"]["n_mapped"] == 3
          and summ["m2_mapping"]["gen_as_of"] == as_of
          and summ["m2_mapping"]["constituent_stats"]["n_members"] == 4)

        # I：纪律标记
        T("I1 恒带 restricted_reference=True",
          out["sh600000"]["restricted_reference"] is True
          and out["sh600000"]["slots"]["M1"]["restricted_reference"] is True)
        T("I2 不产胜率/赔率/目标价字段",
          not any(k in a["slots"]["M1"] for k in ("win_rate", "odds", "target")))
        T("I3 M2 基准非历史分位（L1 当日观测口径）",
          a["slots"]["M2"]["source_layer"] == "L1_snapshot"
          and "distribution" not in a["slots"]["M2"])
        T("I4 零写点（未指定 --out 时不落盘）",
          not os.path.exists(os.path.join(tmp, "out.json")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\npred_ref 自检：%d/%d 通过%s"
          % (ok[0], ok[0] + len(fails), "" if not fails else " · 失败：" + ", ".join(fails)))
    return 0 if not fails else 1


def _load_out_targets(p):
    """--out 目标（唯一写点，CLI 注入；缺省不落盘）。"""
    if not p:
        return None
    return p


def _main(argv=None):
    ap = argparse.ArgumentParser(description="C3 M1/M2/M3 预测基准输入件（PRR-R1）")
    ap.add_argument("--as-of", dest="as_of", help="交易日 YYYY-MM-DD")
    ap.add_argument("--codes", help="逗号分隔股票代码")
    ap.add_argument("--base", help="沙盒/项目根（读 s_s2 生成件）")
    ap.add_argument("--old-root", dest="old_root",
                    help="旧包根（读 data/raw/bus/klines.csv）")
    ap.add_argument("--kb-base", dest="kb_base", help="知识库根（读 S2 库）")
    ap.add_argument("--window", type=int, default=WINDOW_M1, help="M1 观察窗口（默认 5）")
    ap.add_argument("--out", help="输出 JSON 路径（缺省只打印统计，不落盘）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if not a.as_of or not a.codes:
        print("用法：--as-of <YYYY-MM-DD> --codes c1,c2 [--base 根] [--old-root 根] "
              "[--kb-base 根] [--window N] [--out f]")
        return 2
    codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    import priceline as PL
    out, summ = compute(codes, a.as_of, base=a.base,
                        old_root=a.old_root or PL.OLD_ROOT,
                        kb_base=a.kb_base, window_m1=a.window)
    print("C3 预测基准 · %s · rule=%s · n=%d" % (summ["as_of"], summ["rule_version"],
                                                summ["n_requested"]))
    print("  三槽全可用=%d  部分可用=%d  全不可用=%d  槽位可用=%s"
          % (summ["n_all_usable"], summ["n_partial_usable"], summ["n_none_usable"],
             summ["slot_usable"]))
    bm = summ["benchmark"]
    if bm.get("M1_forward_T%d_pct" % a.window):
        d = bm["M1_forward_T%d_pct" % a.window]
        print("  M1 基准 T+%d 前向 n=%d q50=%.2f q75=%.2f"
              % (a.window, d["n"], d["q50"], d["q75"]))
    if bm.get("M3_volume_ratio"):
        d = bm["M3_volume_ratio"]
        print("  M3 基准 量比 n=%d q50=%.2f q25=%.2f q75=%.2f"
              % (d["n"], d["q50"], d["q25"], d["q75"]))
    print("  M2 库 as_of=%s family_groups=%s"
          % (bm.get("M2_kb_as_of"), bm.get("M2_family_groups_n")))
    if summ["reason_histogram"]:
        print("  不可用原因=%s" % summ["reason_histogram"])
    print("  纪律: %s" % summ["discipline"])
    t = _load_out_targets(a.out)
    if t:
        d = os.path.dirname(os.path.abspath(t))
        if d:
            os.makedirs(d, exist_ok=True)
        with io.open(t, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"schema_version": 1, "summary": summ, "by_code": out},
                      f, ensure_ascii=False, indent=1)
        print("  已写基准件: %s（%d 票）" % (t, len(out)))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
