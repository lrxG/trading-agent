#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""drive.py · C3 驱动理解判据件（施工 1.3 · C3-a 批 · v1.0 · 2026-10-09 建）

用途：C3 判断的**机械侧**——把 LLM 的驱动理解提案（proposal）校验成结构化
      Thesis 驱动链（thesis_chain），证据可追溯、三槽物理分离、落盘可被 C4 消费。
      **判断本身在 LLM（AGENT.md 内核 4.1）**：本模块只做结构/追溯/一致性校验，
      不产新证据、不判语义、不决定 BUY/WAIT/NO（那是 three_q/C4）。

★ 一、证据四来源（真实 schema，逐一可解析；不得假定数据存在）
  SNAPSHOT:daily_quote   ref=code      ← snap["daily_quote"] 行内 code
  SNAPSHOT:limit_up      ref=code      ← snap["limit_up"] 行内 code
  SNAPSHOT:dragon_tiger  ref=code      ← snap["dragon_tiger"] 行内 code
  SNAPSHOT:env_metrics   ref=指标名     ← snap["env_metrics"] 键
  SNAPSHOT:news          ref=news_id   ← snap["news"] 行内 news_id（ts 须为 as_of 当日）
  C2:CANDIDATE           ref=候选 id   ← 候选池存在、code 匹配、created_at ≤ as_of
  C1:BRAIN_STATE         ref=原文锚点   ← brain_state.md 含该锚点，且更新时间 = as_of
  快照缺失 → DATA_UNAVAILABLE（缺就是缺，禁编造）。快照是收盘后最终日线 →
  本件输出恒标 session="EOD_ONLY"（首批只支持收盘后决策，不冒充盘中判断）。

★ 二、三槽物理分离（真源 = decision.CHAIN_KEYS，零重定义）
  evidence（证据，逐条带来源+引用，机器可解析）/ inference（推断，纯字符串）/
  prediction（预测，M1/M2/M3 槽七字段结构）/ five（五件套恰键）。
  传入 C4 的 chain 里 evidence 为字符串（decision._norm_chain 口径），
  机器可追溯元组单独存本模块落盘行的 evidence_meta——两份同源，链指纹锁定。

★ 三、驱动判定机械规则（v1.1 · S1 接入后，用户裁决 2026-10-09）
  A 产业催化 → 需 ≥1 条 S1:EVENT_EVAL 证据且该 S1 事件有统计结论
  （historical_effect.status=OK 且 strength ∈ 强/中/弱/微）→ 生效；
  否则 UNKNOWN（reason=A_NO_RELIABLE_SOURCE 无 S1 引用 / A_S1_INSUFFICIENT
  引用了但 n<30 或无记录或强度未知——**禁造胜率，薄样本不算可靠来源**）。
  S1 快照 as_of ≠ 本件 as_of → EVIDENCE_EXPIRED（旧知识不得冒充当日事实）。
  B 资金抢占 → 至少 1 条 SNAPSHOT:dragon_tiger 证据，否则 UNKNOWN。
  C 情绪扩散 → 至少 1 条 SNAPSHOT:env_metrics 或 SNAPSHOT:limit_up，否则 UNKNOWN。
  复合 → 同时满足 B 与 C 的最低证据，否则 UNKNOWN。
  UNKNOWN 落盘后交 C4 → three_q 机械分流（驱动不明 → NO），链路自然安全。

★ 四、落盘：单一 append-only 文件 + 幂等指纹
  data/research/drive/chains.jsonl（landing X2 既有声明模式 data/research/<id>/）。
  幂等键 data_fingerprint = md5(规范化 {as_of, code, driver_proposed, chain,
  evidence_meta})——不含运行时间/随机量；同输入重放 → 返回原行（appended=False），
  不追加第二行。同 (code, as_of) 不同内容 = 新指纹 = 合法新链（查重归 C4-a 调度件）。

★ 五、边界（纪律判据写死）
  零参数正式区 / 决策卡 / 仓位闸写入；零转正与批准调用；不接结算主链（无该模块导入）；
  唯一写点 = chains.jsonl（内联 path or CHAINS_PATH）。
  冻结文件零改动：thesis.py / three_q.py / decision.py / candidates.py / snapshot.py
  只被调用不被修改。

用法：
  python tools/analysis/drive.py                 # 契约总览（零写入）
  python tools/analysis/drive.py --selftest      # 内置验收（沙盒，不碰真数据）
  python tools/analysis/drive.py --discipline    # 纪律判据
  from tools.analysis.drive import review, load_chain, availability
"""
import ast
import hashlib
import io
import json
import os
import csv
import re
import shutil
import sys
import tempfile
import datetime as _dt

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
sys.path.insert(0, _FS)
from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
import decision as DEC  # noqa: E402
import candidates as CAND  # noqa: E402
import snapshot as SNAP  # noqa: E402

_LEGACY_ROOT = (os.environ.get("OLD_SYSTEM_ROOT")
                or os.path.join(os.path.expanduser("~"), "炒股智能体_旧包_20261008"))  # 旧系统只读参考源；换机器时设 OLD_SYSTEM_ROOT 或放家目录
# S1 消费为纯文件只读（_load_s1_out），不导入 s1 模块——保持本件零跨模块写点面。

SCHEMA_VERSION = 1
CHAINS_PATH = os.path.join(ROOT, "data", "research", "drive", "chains.jsonl")
BRAIN_STATE_PATH = os.path.join(ROOT, "memory", "state", "brain_state.md")
S1_OUT_DIR = os.path.join(ROOT, "data", "generated")   # S1 快照目录（X3 豁免落点）

# ── 零重定义：全部取上游真源（is/同源判定见自检 A 组） ─────────────────────────
CHAIN_KEYS = DEC.CHAIN_KEYS              # ("evidence","inference","prediction","five")
FIVE_KEYS = DEC.FIVE_KEYS                # 五件套键名 = 设计方案原文
PRED_SLOTS = DEC.PRED_SLOTS              # ("M1","M2","M3")
PREDICTION_FIELDS = DEC.PREDICTION_FIELDS
DRIVER_TYPES = CAND.DRIVER_TYPES         # ("A","B","C","复合","驱动不明")
DRIVER_UNKNOWN = "驱动不明"
OPS = CAND.OPS                           # 结构化判据比较符（falsifier 用）

SOURCES = ("SNAPSHOT:daily_quote", "SNAPSHOT:limit_up", "SNAPSHOT:dragon_tiger",
           "SNAPSHOT:env_metrics", "SNAPSHOT:news", "C2:CANDIDATE", "C1:BRAIN_STATE",
           "S1:EVENT_EVAL")
_SNAP_SRC = ("SNAPSHOT:daily_quote", "SNAPSHOT:limit_up", "SNAPSHOT:dragon_tiger",
             "SNAPSHOT:env_metrics", "SNAPSHOT:news")
_B_REQUIRE = ("SNAPSHOT:dragon_tiger",)
_C_REQUIRE = ("SNAPSHOT:env_metrics", "SNAPSHOT:limit_up")
_A_REQUIRE = ("S1:EVENT_EVAL",)
# S1 结论可用 = historical_effect.status=="OK"（有可追溯统计结论）；
# UNKNOWN / NO_RECORD / INSUFFICIENT_SAMPLES（n<30）一律不算可靠来源（禁造胜率）。
_S1_STRENGTHS = ("强", "中", "弱", "微")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


class DriveError(Exception):
    """校验失败。str 以 REASON_CODE: 开头（每类拒绝都可机械断言）。"""


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(obj):
    return "md5:" + hashlib.md5(_canon(obj).encode("utf-8")).hexdigest()


def _check_text(s, name):
    if not isinstance(s, str) or not s.strip():
        raise DriveError("BAD_TEXT: %s 必须是非空字符串，收到 %r" % (name, s))
    return s


# ── 输入汇集（缺就是缺，显式上报） ────────────────────────────────────────────
def availability(as_of, *, snapshot_root=None, pool_path=None,
                 brain_state_path=None):
    """四来源在 as_of 的可用性概览（供 C4-a 调度前置检查；零写入）。"""
    d = DEC._check_date(as_of)
    snap = SNAP.load_snapshot(d, root=snapshot_root)
    out = {"as_of": d, "session": "EOD_ONLY",
           "snapshot": "ABSENT" if snap is SNAP.UNAVAILABLE else "PRESENT"}
    out["s1"] = "ABSENT" if _load_s1_out(d) is None else "PRESENT"
    if snap is not SNAP.UNAVAILABLE:
        out["snapshot_counts"] = {k: len(snap.get(k) or [])
                                  for k in ("daily_quote", "limit_up",
                                            "dragon_tiger", "news")}
        out["env_metrics_keys"] = sorted((snap.get("env_metrics") or {}).keys())
    cands = [c for c in CAND.list_candidates(path=pool_path)
             if c.get("state") == "QUALIFIED"]
    out["candidates_qualified"] = len(cands)
    bp = brain_state_path or BRAIN_STATE_PATH
    if os.path.isfile(bp):
        txt = io.open(bp, encoding="utf-8").read()
        m = re.search(r"\*\*更新时间\*\*：(\d{4}-\d{2}-\d{2})", txt)
        out["brain_state_upto"] = m.group(1) if m else "UNKNOWN"
    else:
        out["brain_state_upto"] = "ABSENT"
    return out


# ── 证据逐条校验（可追溯 = ref 必须真解析得到） ────────────────────────────────
def _load_s1_out(as_of, s1_path=None):
    """S1 输出快照只读（缺→None，缺就是缺）。s1_path 供沙盒注入。

    本地 io.open 读（brain_text 同款先例）：不走 S1 模块读函数，保持本件
    静态落点封闭（landing 只读审计可解析）。路径两步拼装：先常量目录
    （可归类），再动态文件名（无锚点不入词汇表）——规避 data/generated/<dyn>
    假落点（landing ⑥ 判据形态，非真实写点）。
    """
    p = s1_path or os.path.join(S1_OUT_DIR, "s_s1_%s.json" % as_of)
    if not os.path.isfile(p):
        return None
    with io.open(p, encoding="utf-8-sig") as f:
        return json.load(f)


# ── S2/S3/S5 上下文只读接线（2026-10-13 · 第一阶段 C3 补线）──────────────────
# 纪律与 _load_s1_out 同款：纯文件只读、缺→ABSENT、零编造、零跨模块导入
# （不 import s2/s3/s5 重机器，避免读取面放大）。S2 的 code→题材映射读旧包
# 静态成分表（tdx_sector_constituent，s2 同款宇宙），缺文件→空列表如实。
SX_ROOT = S1_OUT_DIR                     # 同一 X3 豁免目录
_OLD_ROOT = _LEGACY_ROOT
_CONSTITUENT_CACHE = None                # {code_lower: [sector_code, ...]}（惰性单遍）


def _load_sx_out(as_of, sid, root=None):
    """S2/S3/S5 当日快照只读（缺/坏→None，缺就是缺）。root 供沙盒注入。"""
    p = os.path.join(root or SX_ROOT, "s_%s_%s.json" % (sid, as_of))
    if not os.path.isfile(p):
        return None
    try:
        with io.open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def _code_theme_ids(code, old_root=None):
    """code（sh600477 形态）→ 所属概念板块 id 列表（tdx 静态快照；缺文件→[]）。

    CSV code 形态 `000002.SZ`/`600519.SH` → 本系统 `sz000002`/`sh600519`；
    全表 46832 行惰性加载一次后缓存（只读，无写点）。
    """
    global _CONSTITUENT_CACHE
    key = code.strip().lower()
    if _CONSTITUENT_CACHE is None:
        m = {}
        p = os.path.join(old_root or _OLD_ROOT, "data", "raw", "meta",
                         "tdx_sector_constituent.csv")
        try:
            with io.open(p, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    if (r.get("sector_type") or "") != "概念":
                        continue
                    raw = (r.get("code") or "").strip()
                    suf = raw[-2:].upper() if len(raw) > 2 else ""
                    num = raw[:-3] if suf in ("SZ", "SH") else raw
                    if suf == "SZ":
                        cc = "sz" + num
                    elif suf == "SH":
                        cc = "sh" + num
                    else:
                        cc = raw
                    m.setdefault(cc, []).append((r.get("sector_code") or "").strip())
        except OSError:
            m = {}
        _CONSTITUENT_CACHE = m
    return _CONSTITUENT_CACHE.get(key, [])


def _sx_context(as_of, code, sx_root=None, old_root=None):
    """S2/S3/S5 → C3 提案评审的**非阻断上下文**（研究技能输出，不设硬门）。

    每路 status=PRESENT/ABSENT 如实；ABSENT = 快照未产出（缺就是缺，禁编造）。
    """
    ctx = {}
    s2 = _load_sx_out(as_of, "s2", sx_root)
    if s2 is None:
        ctx["s2_theme"] = {"status": "ABSENT"}
    else:
        th = s2.get("themes") or {}
        hist = {}
        for v in th.values():
            st = (v or {}).get("stage")
            hist[st] = hist.get(st, 0) + 1
        tids = _code_theme_ids(code, old_root)
        mine = [{"theme_id": t,
                 "theme_name": (th.get(t) or {}).get("theme_name"),
                 "stage": (th.get(t) or {}).get("stage"),
                 "family": (th.get(t) or {}).get("family")}
                for t in tids if t in th]
        ctx["s2_theme"] = {"status": "PRESENT", "n_themes": s2.get("n_themes"),
                           "stage_histogram": hist, "code_themes": mine,
                           "n_code_themes": len(mine)}
    s3 = _load_sx_out(as_of, "s3", sx_root)
    ctx["s3_emotion"] = ({"status": "ABSENT"} if s3 is None else
                         {"status": "PRESENT", "temp": s3.get("temp"),
                          "stage": s3.get("stage"), "leader": s3.get("leader"),
                          "structure_bias": s3.get("structure_bias")})
    s5 = _load_sx_out(as_of, "s5", sx_root)
    if s5 is None:
        ctx["s5_capital"] = {"status": "ABSENT"}
    else:
        codes = s5.get("codes") or {}
        ent = codes.get(code) if isinstance(codes, dict) else None
        if ent is None and isinstance(codes, dict):
            for v in codes.values():
                if isinstance(v, dict) and v.get("code") == code:
                    ent = v
                    break
        ctx["s5_capital"] = {
            "status": "PRESENT", "n_structured": s5.get("n_structured"),
            "n_unknown": s5.get("n_unknown"),
            "code": ({"behavior": ent.get("behavior"),
                      "chip_state": ent.get("chip_state")}
                     if isinstance(ent, dict) else
                     {"behavior": "not_in_structured_set"})}
    return ctx


def _s1_usable(ev_out):
    """S1 事件输出 → 是否构成 A 类可靠证据（有统计结论才算）。

    真实产物键 = s1.evaluate 输出：net_effect（OK 态无 status 键；
    NO_RECORD / INSUFFICIENT_SAMPLES 显式 status）+ strength（UNKNOWN 或档位）。
    夹具必须与真实产物同形（第一轮教训：按臆造键名写的测试全绿但对接失灵）。
    """
    he = (ev_out or {}).get("net_effect") or {}
    if str(he.get("status") or "OK") != "OK":
        return False, str(he.get("status") or "EMPTY_NET_EFFECT")
    if ev_out.get("strength") not in _S1_STRENGTHS:
        return False, "STRENGTH_UNKNOWN"
    return True, None


def _validate_evidence(items, *, as_of, code, snap, pool, brain_text, s1_out=None):
    """返回 evidence_meta（机器可追溯元组）；任何一条不合格 → DriveError(REASON)。"""
    if not isinstance(items, list) or not items:
        raise DriveError("EVIDENCE_EMPTY: evidence 必须是非空列表（无证据即无 C3 结论）")
    meta, seen = [], set()
    for i, it in enumerate(items):
        if not isinstance(it, dict) or set(it) != {"text", "source", "ref"}:
            raise DriveError(
                "EVIDENCE_NOT_OBJECT: evidence[%d] 必须恰含 {text,source,ref}，"
                "收到 %r（模型生成的判断不得混入证据槽）" % (i, it))
        _check_text(it["text"], "evidence[%d].text" % i)
        src, ref = it["source"], it["ref"]
        _check_text(ref, "evidence[%d].ref" % i)
        if src not in SOURCES:
            raise DriveError("EVIDENCE_BAD_SOURCE: evidence[%d].source=%r 不在枚举 %s"
                             "（来源必须可追溯，模型自述不合法）" % (i, src, list(SOURCES)))
        key = (src, ref)
        if key in seen:
            raise DriveError("EVIDENCE_DUPLICATE_REF: evidence[%d] 重复引用 %s（自相矛盾/重复挂证）"
                             % (i, key))
        seen.add(key)
        if src in _SNAP_SRC:
            if snap is SNAP.UNAVAILABLE:
                raise DriveError("DATA_UNAVAILABLE: as_of=%s 无快照（禁编造证据）" % as_of)
            if src == "SNAPSHOT:daily_quote":
                ok = any(q.get("code") == ref for q in snap["daily_quote"])
            elif src == "SNAPSHOT:limit_up":
                ok = any(q.get("code") == ref for q in snap["limit_up"])
            elif src == "SNAPSHOT:dragon_tiger":
                ok = any(q.get("code") == ref for q in snap["dragon_tiger"])
            elif src == "SNAPSHOT:env_metrics":
                ok = ref in (snap.get("env_metrics") or {})
            else:  # SNAPSHOT:news
                row = next((n for n in snap.get("news") or []
                            if n.get("news_id") == ref), None)
                ok = row is not None and str(row.get("ts", "")).startswith(as_of)
                if row is not None and not ok:
                    raise DriveError("EVIDENCE_EXPIRED: news %s 时间 %s 非当日 %s"
                                     % (ref, row.get("ts"), as_of))
            if not ok:
                raise DriveError("EVIDENCE_REF_UNRESOLVED: evidence[%d] %s ref=%r "
                                 "在 as_of=%s 快照中解析不到" % (i, src, ref, as_of))
            meta.append({"index": i, "source": src, "ref": ref, "as_of": as_of})
        elif src == "C2:CANDIDATE":
            c = next((x for x in pool if x.get("id") == ref), None)
            if c is None:
                raise DriveError("EVIDENCE_REF_UNRESOLVED: evidence[%d] 候选 %r 不存在"
                                 % (i, ref))
            if c.get("code") != code:
                raise DriveError("EVIDENCE_REF_UNRESOLVED: evidence[%d] 候选 %s 属 %s ≠ %s"
                                 % (i, ref, c.get("code"), code))
            if str(c.get("created_at", "")) > as_of:
                raise DriveError("EVIDENCE_EXPIRED: 候选 %s created_at=%s > as_of=%s"
                                 % (ref, c.get("created_at"), as_of))
            meta.append({"index": i, "source": src, "ref": ref, "as_of": c["created_at"]})
        elif src == "S1:EVENT_EVAL":
            if not isinstance(s1_out, dict):
                raise DriveError("DATA_UNAVAILABLE: evidence[%d] S1 输出快照不可读"
                                 "（as_of=%s 无 s_s1_<as_of>.json，缺就是缺禁编造）"
                                 % (i, as_of))
            if str(s1_out.get("as_of") or "") != as_of:
                raise DriveError("EVIDENCE_EXPIRED: evidence[%d] S1 快照 as_of=%s ≠ %s"
                                 "（旧知识不得冒充当日事实）"
                                 % (i, s1_out.get("as_of"), as_of))
            ev = next((e for e in s1_out.get("events") or []
                       if e.get("event_id") == ref), None)
            if ev is None:
                raise DriveError("EVIDENCE_REF_UNRESOLVED: evidence[%d] S1 事件 %r "
                                 "不在 as_of=%s 快照中" % (i, ref, as_of))
            if code not in (ev.get("related_codes") or []):
                raise DriveError("EVIDENCE_REF_UNRESOLVED: evidence[%d] S1 事件 %s "
                                 "related_codes 不含 %s" % (i, ref, code))
            usable, why = _s1_usable(ev)
            meta.append({"index": i, "source": src, "ref": ref, "as_of": as_of,
                         "s1_usable": usable, "s1_blocker": why,
                         "s1_strength": ev.get("strength")})
        elif src == "C1:BRAIN_STATE":
            if brain_text is None:
                raise DriveError("BRAIN_STATE_ABSENT: brain_state.md 不可读（禁编造）")
            if ref not in brain_text:
                raise DriveError("EVIDENCE_REF_UNRESOLVED: brain_state 中找不到锚点 %r" % ref)
            m = re.search(r"\*\*更新时间\*\*：(\d{4}-\d{2}-\d{2})", brain_text)
            upto = m.group(1) if m else None
            if upto != as_of:
                raise DriveError("BRAIN_STATE_EXPIRED: 环境档更新至 %s ≠ as_of=%s"
                                 % (upto, as_of))
            meta.append({"index": i, "source": src, "ref": ref, "as_of": as_of})
        else:
            raise DriveError("EVIDENCE_BAD_SOURCE: evidence[%d].source=%r 内部路由缺失"
                             % (i, src))
    return meta


def _driver_effective(proposed, meta):
    """驱动判定机械规则（首批边界）。返回 (effective, reason)。"""
    if proposed not in DRIVER_TYPES:
        raise DriveError("DRIVER_BAD: driver 只能是 %s，收到 %r" % (list(DRIVER_TYPES), proposed))
    srcs = [m["source"] for m in meta]
    if proposed == "A":
        s1_metas = [m for m in meta if m["source"] in _A_REQUIRE]
        if not s1_metas:
            return DRIVER_UNKNOWN, "A_NO_RELIABLE_SOURCE"
        if not any(m.get("s1_usable") for m in s1_metas):
            return DRIVER_UNKNOWN, "A_S1_INSUFFICIENT"
        return proposed, None
    has_b = any(s in _B_REQUIRE for s in srcs)
    has_c = any(s in _C_REQUIRE for s in srcs)
    if proposed == "B" and not has_b:
        return DRIVER_UNKNOWN, "B_INSUFFICIENT_EVIDENCE"
    if proposed == "C" and not has_c:
        return DRIVER_UNKNOWN, "C_INSUFFICIENT_EVIDENCE"
    if proposed == "复合" and not (has_b and has_c):
        return DRIVER_UNKNOWN, "COMPOSITE_INSUFFICIENT_EVIDENCE"
    return proposed, None


def _validate_prediction(items):
    out = []
    seen = set()
    for i, p in enumerate(items or []):
        if not isinstance(p, dict):
            raise DriveError("PREDICTION_INVALID: prediction[%d] 必须是对象" % i)
        extra = set(p) - set(PREDICTION_FIELDS)
        miss = set(PREDICTION_FIELDS) - set(p)
        if extra or miss:
            raise DriveError("PREDICTION_INVALID: prediction[%d] 字段不符（多=%s 缺=%s）"
                             % (i, sorted(extra), sorted(miss)))
        if p["prediction_id"] not in PRED_SLOTS:
            raise DriveError("PREDICTION_INVALID: prediction_id 只许 %s" % list(PRED_SLOTS))
        if p["prediction_id"] in seen:
            raise DriveError("PREDICTION_INVALID: 槽位 %s 重复" % p["prediction_id"])
        seen.add(p["prediction_id"])
        if p["direction"] not in ("UP", "DOWN", "FLAT"):
            raise DriveError("PREDICTION_INVALID: direction 只许 UP/DOWN/FLAT")
        for k in ("metric", "data_source", "condition"):
            _check_text(p[k], "prediction[%d].%s" % (i, k))
        for k in ("baseline", "threshold", "window"):
            if p[k] is None:
                raise DriveError("PREDICTION_INVALID: prediction[%d].%s 不得为空"
                                 "（下注前必须冻结）" % (i, k))
        out.append(dict(p))
    return out


# ── 核心（纯函数：输入已加载，便于测试与复用） ────────────────────────────────
def _review_core(proposal, *, as_of, code, snap, pool, brain_text, s1_out=None,
                 sx_ctx=None):
    if not isinstance(proposal, dict):
        raise DriveError("PROPOSAL_INVALID: proposal 必须是对象")
    extra = set(proposal) - {"driver", "evidence", "inference", "prediction", "five",
                             "thesis_id"}
    miss = {"driver", "evidence", "inference", "prediction", "five"} - set(proposal)
    if extra or miss:
        raise DriveError("PROPOSAL_INVALID: 字段不符（多=%s 缺=%s）"
                         % (sorted(extra), sorted(miss)))
    th_id = proposal.get("thesis_id")
    if th_id is not None:
        th_id = DEC._check_thesis_id(th_id)
    meta = _validate_evidence(proposal["evidence"], as_of=as_of, code=code,
                              snap=snap, pool=pool, brain_text=brain_text,
                              s1_out=s1_out)
    driver, reason = _driver_effective(proposal["driver"], meta)
    inf = proposal["inference"]
    if not isinstance(inf, list):
        raise DriveError("INFERENCE_INVALID: inference 必须是列表（与证据物理分槽）")
    for i, x in enumerate(inf):
        _check_text(x, "inference[%d]" % i)
    pred = _validate_prediction(proposal["prediction"])
    five = proposal["five"]
    if not isinstance(five, dict) or set(five) != set(FIVE_KEYS):
        raise DriveError("FIVE_INVALID: five 必须恰含五件套 %s" % list(FIVE_KEYS))
    five = {k: (None if five[k] is None else _check_text(five[k], "five.%s" % k))
            for k in FIVE_KEYS}
    chain = {
        "evidence": ["%s（来源：%s 引用：%s）" % (it["text"], it["source"], it["ref"])
                     for it in proposal["evidence"]],
        "inference": list(inf),
        "prediction": pred,
        "five": five,
    }
    fp = _fingerprint({"as_of": as_of, "code": code, "driver_proposed": proposal["driver"],
                       "chain": chain, "evidence_meta": meta})
    row = {"schema_version": SCHEMA_VERSION, "chain_id": "DRV-" + fp[4:16],
           "as_of": as_of, "code": code, "thesis_id": th_id,
           "driver_proposed": proposal["driver"], "driver_effective": driver,
           "driver_reason": reason, "chain": chain, "evidence_meta": meta,
           "prediction_slots": [p["prediction_id"] for p in pred],
           "session": "EOD_ONLY", "data_fingerprint": fp}
    if sx_ctx is not None:
        row["context"] = sx_ctx      # S2/S3/S5 研究上下文（非阻断 · ABSENT 如实）
    return row


def _load_rows(path):
    p = path or CHAINS_PATH
    if not os.path.isfile(p):
        return []
    return [json.loads(x) for x in io.open(p, encoding="utf-8") if x.strip()]


# ── 主接口 ───────────────────────────────────────────────────────────────────
def review(proposal, *, as_of, code, snapshot_root=None, pool_path=None,
           brain_state_path=None, path=None, s1_path=None, sx_root=None,
           old_root=None):
    """LLM 提案 → 校验 → 落盘（幂等）。返回 row（含 appended 标志）。

    s1_path：S1 输出快照路径（沙盒注入）；缺省读 data/generated/s_s1_<as_of>.json。
    sx_root/old_root：S2/S3/S5 快照与题材成分表根（沙盒注入）；缺省生产路径。
    """
    d = DEC._check_date(as_of)
    c = DEC._check_code(code)
    snap = SNAP.load_snapshot(d, root=snapshot_root)
    bp = brain_state_path or BRAIN_STATE_PATH
    brain_text = io.open(bp, encoding="utf-8").read() if os.path.isfile(bp) else None
    pool = CAND.list_candidates(path=pool_path)
    s1_out = _load_s1_out(d, s1_path)
    sx_ctx = _sx_context(d, c, sx_root=sx_root, old_root=old_root)
    row = _review_core(proposal, as_of=d, code=c, snap=snap, pool=pool,
                       brain_text=brain_text, s1_out=s1_out, sx_ctx=sx_ctx)
    for old in _load_rows(path):
        if old.get("data_fingerprint") == row["data_fingerprint"]:
            row = dict(old)
            row["appended"] = False
            return row
    _WG.check_write(path or CHAINS_PATH)
    with io.open(path or CHAINS_PATH, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    row["appended"] = True
    return row


def load_chain(chain_id=None, *, code=None, as_of=None, path=None):
    """C4 消费入口：按 chain_id 或 (code, as_of) 取最近一条；无 → None。"""
    rows = _load_rows(path)
    if chain_id is not None:
        rows = [r for r in rows if r.get("chain_id") == chain_id]
    else:
        if code is not None:
            rows = [r for r in rows if r.get("code") == code]
        if as_of is not None:
            rows = [r for r in rows if r.get("as_of") == as_of]
        rows = sorted(rows, key=lambda r: (r["as_of"], r["chain_id"]))
    return dict(rows[-1]) if rows else None


# ── 自检（全部沙盒，不碰真数据） ─────────────────────────────────────────────
def _selftest():
    bad = [0]
    real_before = (hashlib.md5(io.open(CHAINS_PATH, "rb").read()).hexdigest()
                   if os.path.isfile(CHAINS_PATH) else "ABSENT")

    def T(name, cond):
        print(("  [ok] " if cond else "  [!!] ") + name)
        if not cond:
            bad[0] += 1

    import three_q as TQ
    D1 = "2026-10-08"

    print("A组 零重定义（同源 is 判定）")
    T("A1 CHAIN_KEYS is DEC.CHAIN_KEYS", CHAIN_KEYS is DEC.CHAIN_KEYS)
    T("A2 FIVE_KEYS is DEC.FIVE_KEYS", FIVE_KEYS is DEC.FIVE_KEYS)
    T("A3 PREDICTION_FIELDS is DEC.PREDICTION_FIELDS", PREDICTION_FIELDS is DEC.PREDICTION_FIELDS)
    T("A4 DRIVER_TYPES is CAND.DRIVER_TYPES", DRIVER_TYPES is CAND.DRIVER_TYPES)
    T("A5 OPS is CAND.OPS", OPS is CAND.OPS)
    T("A6 SOURCES 全部带真源前缀", all(s.startswith(("SNAPSHOT:", "C2:", "C1:", "S1:")) for s in SOURCES))

    # 沙盒夹具（契约形态，清楚标记为夹具——只证结构，不证行情）
    CODE = "sh600000"
    snap = {"date": D1,
            "daily_quote": [{"code": CODE, "name": "X", "open": 10.0, "high": 10.5,
                             "low": 9.9, "close": 10.4, "pct": 3.18, "vol": 1.0,
                             "amount": 1.27e8}],
            "limit_up": [{"code": CODE, "name": "X", "boards": 2,
                          "first_seal_time": "09:35:12", "seal_amount": 8.2e7,
                          "reason": "题材"}],
            "dragon_tiger": [{"code": CODE, "name": "X", "seat": "某席位",
                              "buy": 9e7, "sell": 1e7, "net": 8e7}],
            "news": [{"news_id": "N-1", "ts": D1 + " 09:12:00", "source": "证券时报",
                      "title": "t", "body": "b", "related_codes": CODE}],
            "env_metrics": {"limit_up_count": 60, "fail_rate": 21.0,
                            "turnover": 1.5e11, "up_down_ratio": 3.2,
                            "index_ma20": 3450.5, "max_board": 6}}
    pool = [{"id": "C-1", "code": CODE, "state": "QUALIFIED", "created_at": D1,
             "last_seen": D1, "expiry": "2026-10-13", "source": "五路扫描",
             "trigger": {"text": "t", "rule": None}, "invalid_condition": None,
             "next_trigger": None, "keep_reason": None, "thesis_id": None,
             "thesis": None}]
    brain = "# 当前状态\n- **更新时间**：%s\n" % D1
    EV = [{"text": "席位净买入 8000 万", "source": "SNAPSHOT:dragon_tiger", "ref": CODE},
          {"text": "涨停题材归属可查", "source": "SNAPSHOT:limit_up", "ref": CODE}]
    P5 = {k: "%s 内容" % k for k in FIVE_KEYS}
    good = {"driver": "B", "evidence": EV, "inference": ["资金抢占 → 溢价兑现"],
            "prediction": [], "five": P5, "thesis_id": None}

    print("B组 正样本（沙盒夹具）")
    row = _review_core(good, as_of=D1, code=CODE, snap=snap, pool=pool, brain_text=brain)
    T("B1 B 驱动成立（dragon_tiger 证据在）", row["driver_effective"] == "B")
    T("B2 chain 恰四键", set(row["chain"]) == set(CHAIN_KEYS))
    T("B3 evidence_meta 逐条带来源+as_of",
      len(row["evidence_meta"]) == 2 and all(m["as_of"] == D1 for m in row["evidence_meta"]))
    cgood = {"driver": "C",
             "evidence": [{"text": "炸板率 21%", "source": "SNAPSHOT:env_metrics",
                           "ref": "fail_rate"}],
             "inference": ["情绪扩散中"], "prediction": [], "five": P5, "thesis_id": None}
    rc = _review_core(cgood, as_of=D1, code=CODE, snap=snap, pool=pool, brain_text=brain)
    T("B4 C 驱动成立（env_metrics 证据在）", rc["driver_effective"] == "C")
    pa = {"prediction_id": "M1", "metric": "5日超额", "data_source": "snapshot",
          "baseline": "0", "direction": "UP", "threshold": "3",
          "condition": "放量不破5日线", "window": 3}
    pa2 = dict(pa, prediction_id="M2")
    pa3 = dict(pa, prediction_id="M3")
    rp = _review_core(dict(good, prediction=[pa, pa2, pa3]), as_of=D1, code=CODE,
                      snap=snap, pool=pool, brain_text=brain)
    T("B5 prediction 三槽齐被接受", rp["prediction_slots"] == ["M1", "M2", "M3"])

    print("C组 幂等与落盘 → C4 消费同一份结果（真 three_q 读盘）")
    tmp = tempfile.mkdtemp(prefix="drive_sb_")
    sbpath = os.path.join(tmp, "chains.jsonl")
    # 真 IO：沙盒 brain_state 文件 + 真快照根目录（契约文件，清楚标记为夹具）
    sroot = os.path.join(tmp, "snap", D1)
    os.makedirs(sroot)
    io.open(os.path.join(sroot, "daily_quote.csv"), "w", encoding="utf-8").write(
        "code,name,open,high,low,close,pct,vol,amount\nsh600000,X,10.0,10.5,9.9,10.4,3.18,1.0,127000000.0\n")
    io.open(os.path.join(sroot, "limit_up.csv"), "w", encoding="utf-8").write(
        "code,name,boards,first_seal_time,seal_amount,reason\nsh600000,X,2,09:35:12,82000000.0,题材\n")
    io.open(os.path.join(sroot, "dragon_tiger.csv"), "w", encoding="utf-8").write(
        "code,name,seat,buy,sell,net\nsh600000,X,某席位,90000000.0,10000000.0,80000000.0\n")
    io.open(os.path.join(sroot, "news.jsonl"), "w", encoding="utf-8").write(
        json.dumps({"news_id": "N-1", "ts": D1 + " 09:12:00", "source": "证券时报",
                    "title": "t", "body": "b", "related_codes": [CODE]},
                   ensure_ascii=False) + "\n")
    io.open(os.path.join(sroot, "env_metrics.json"), "w", encoding="utf-8").write(
        json.dumps(snap["env_metrics"], ensure_ascii=False))
    bpath = os.path.join(tmp, "brain_state.md")
    io.open(bpath, "w", encoding="utf-8").write(brain)
    pool_f = os.path.join(tmp, "candidates.json")
    io.open(pool_f, "w", encoding="utf-8").write(json.dumps(
        {"version": CAND.SCHEMA_VERSION, "updated_at": D1, "candidates": pool},
        ensure_ascii=False))
    w1 = review(good, as_of=D1, code=CODE, snapshot_root=os.path.join(tmp, "snap"),
                pool_path=pool_f, brain_state_path=bpath, path=sbpath)
    T("C1 review 真 IO 落盘成功", w1.get("appended") is True and w1["chain_id"] == row["chain_id"])
    w2 = review(good, as_of=D1, code=CODE, snapshot_root=os.path.join(tmp, "snap"),
                pool_path=pool_f, brain_state_path=bpath, path=sbpath)
    T("C2 幂等重放：同指纹不追加", w2.get("appended") is False
      and len(_load_rows(sbpath)) == 1)
    got = load_chain(code=CODE, as_of=D1, path=sbpath)
    T("C3 load_chain 取回同一链", got is not None and got["chain_id"] == row["chain_id"])
    verdict = TQ.answer(D1, CODE, "TH-20261008-001", got["chain"], driver="B",
                        env={"regime": None, "signals": {}, "leg": "EOD",
                             "source": "UNAVAILABLE"},
                        entry_limit=10.2, stop_line=9.8, target=11.5, window=5,
                        position_request=0.2)
    T("C4 C4 消费落盘链 → 机械 BUY（规则+输入共同决定）", verdict["action"] == "BUY")
    v2 = TQ.answer(D1, CODE, "TH-20261008-001", got["chain"], driver=TQ.DRIVER_UNKNOWN,
                   env={"regime": None, "signals": {}, "leg": "EOD",
                        "source": "UNAVAILABLE"},
                   overturn_event={"text": "资金回流可查证", "rule": None})
    T("C5 驱动不明 → 机械 NO（UNKNOWN 不产生 BUY）", v2["action"] == "NO")
    av = availability(D1, snapshot_root=os.path.join(tmp, "snap"), pool_path=pool_f,
                      brain_state_path=bpath)
    T("C6 availability 四来源如实上报",
      av["snapshot"] == "PRESENT" and av["candidates_qualified"] == 1
      and av["brain_state_upto"] == D1)

    print("D组 坏样本拒绝（每类拒绝原因可断言）")

    def rejects(name, fn, reason):
        try:
            fn()
            T(name + "（应拒未拒）", False)
        except DriveError as ex:
            T("%s -> %s" % (name, str(ex).split(":", 1)[0]),
              str(ex).startswith(reason))
        except Exception as ex:  # noqa: BLE001
            T("%s（异常类型错：%r）" % (name, ex), False)

    rejects("D1 证据缺来源",
            lambda: _review_core(dict(good, evidence=[{"text": "x", "ref": CODE}]),
                                 as_of=D1, code=CODE, snap=snap, pool=pool,
                                 brain_text=brain), "EVIDENCE_NOT_OBJECT")
    rejects("D2 证据来源不在枚举（模型自述不合法）",
            lambda: _review_core(dict(good, evidence=[{"text": "我觉得", "source": "SELF",
                                                       "ref": "x"}]),
                                 as_of=D1, code=CODE, snap=snap, pool=pool,
                                 brain_text=brain), "EVIDENCE_BAD_SOURCE")
    rejects("D3 ref 解析不到（快照无此 code）",
            lambda: _review_core(dict(good, evidence=[{"text": "x",
                                                       "source": "SNAPSHOT:daily_quote",
                                                       "ref": "sz000001"}]),
                                 as_of=D1, code=CODE, snap=snap, pool=pool,
                                 brain_text=brain), "EVIDENCE_REF_UNRESOLVED")
    rejects("D4 过期：news 非当日",
            lambda: _review_core(dict(good, evidence=[{"text": "x",
                                                       "source": "SNAPSHOT:news",
                                                       "ref": "N-1"}]),
                                 as_of="2026-10-09", code=CODE, snap=snap, pool=pool,
                                 brain_text=brain), "EVIDENCE_EXPIRED")
    rejects("D5 过期：环境档未更新到当日",
            lambda: _review_core(dict(good, evidence=[{"text": "x",
                                                       "source": "C1:BRAIN_STATE",
                                                       "ref": "更新时间"}]),
                                 as_of=D1, code=CODE, snap=snap, pool=pool,
                                 brain_text="# 状态\n- **更新时间**：2026-10-07\n"),
            "BRAIN_STATE_EXPIRED")
    rejects("D6 矛盾：同源同引用重复挂证",
            lambda: _review_core(dict(good, evidence=EV + [EV[0]]),
                                 as_of=D1, code=CODE, snap=snap, pool=pool,
                                 brain_text=brain), "EVIDENCE_DUPLICATE_REF")
    rejects("D7 无快照禁编造",
            lambda: _review_core(good, as_of=D1, code=CODE, snap=SNAP.UNAVAILABLE,
                                 pool=pool, brain_text=brain), "DATA_UNAVAILABLE")
    T("D8 A 类 → 恒 UNKNOWN（首批弱代理不得拼产业链）",
      _review_core(dict(good, driver="A"), as_of=D1, code=CODE, snap=snap,
                   pool=pool, brain_text=brain)["driver_effective"] == DRIVER_UNKNOWN)
    T("D9 A 类 UNKNOWN 带原因码",
      _review_core(dict(good, driver="A"), as_of=D1, code=CODE, snap=snap,
                   pool=pool, brain_text=brain)["driver_reason"] == "A_NO_RELIABLE_SOURCE")
    T("D10 B 类证据不足 → UNKNOWN",
      _review_core(dict(good, evidence=[{"text": "x", "source": "SNAPSHOT:limit_up",
                                         "ref": CODE}]),
                   as_of=D1, code=CODE, snap=snap, pool=pool,
                   brain_text=brain)["driver_effective"] == DRIVER_UNKNOWN)
    rejects("D11 five 缺键",
            lambda: _review_core(dict(good, five={"驱动存在": "x"}),
                                 as_of=D1, code=CODE, snap=snap, pool=pool,
                                 brain_text=brain), "FIVE_INVALID")
    rejects("D12 prediction 缺冻结字段",
            lambda: _review_core(dict(good, prediction=[dict(pa, baseline=None)]),
                                 as_of=D1, code=CODE, snap=snap, pool=pool,
                                 brain_text=brain), "PREDICTION_INVALID")
    rejects("D13 驱动枚举外",
            lambda: _review_core(dict(good, driver="D"), as_of=D1, code=CODE,
                                 snap=snap, pool=pool, brain_text=brain), "DRIVER_BAD")

    print("F组 S1:EVENT_EVAL 接入（A 类驱动供料 · v1.1）")
    s1_ok = {"s_id": "s1", "as_of": D1, "events": [{
        "event_id": "EV-1", "related_codes": [CODE], "type": "回购", "strength": "中",
        "net_effect": {"pct": 1.76, "sample_size": 3448,
                       "evidence_grade": "A", "kb_version": "1.0.0"}}]}
    s1_thin = {"s_id": "s1", "as_of": D1, "events": [{
        "event_id": "EV-2", "related_codes": [CODE], "type": "测试薄样本",
        "strength": "UNKNOWN",
        "net_effect": {"status": "INSUFFICIENT_SAMPLES", "sample_size": 12}}]}
    ev_s1 = {"text": "回购事件历史净效应 +1.76%", "source": "S1:EVENT_EVAL", "ref": "EV-1"}
    agood = dict(good, driver="A", evidence=[ev_s1])
    ra = _review_core(agood, as_of=D1, code=CODE, snap=snap, pool=pool,
                      brain_text=brain, s1_out=s1_ok)
    T("F1 A + S1 有统计结论 → 生效", ra["driver_effective"] == "A"
      and ra["driver_reason"] is None)
    rthin = _review_core(dict(agood, evidence=[dict(ev_s1, ref="EV-2")]),
                         as_of=D1, code=CODE, snap=snap, pool=pool,
                         brain_text=brain, s1_out=s1_thin)
    T("F2 S1 薄样本(n<30) → UNKNOWN/A_S1_INSUFFICIENT（禁造胜率）",
      rthin["driver_effective"] == DRIVER_UNKNOWN
      and rthin["driver_reason"] == "A_S1_INSUFFICIENT")
    rejects("F3 S1 快照缺失 → DATA_UNAVAILABLE（缺就是缺）",
            lambda: _review_core(agood, as_of=D1, code=CODE, snap=snap, pool=pool,
                                 brain_text=brain, s1_out=None), "DATA_UNAVAILABLE")
    rejects("F4 S1 快照 as_of 错位 → EVIDENCE_EXPIRED（旧知识不冒充当日）",
            lambda: _review_core(agood, as_of=D1, code=CODE, snap=snap, pool=pool,
                                 brain_text=brain,
                                 s1_out=dict(s1_ok, as_of="2026-10-07")),
            "EVIDENCE_EXPIRED")
    rejects("F5 事件 related_codes 不含本票 → REF_UNRESOLVED",
            lambda: _review_core(agood, as_of=D1, code="sz000001", snap=snap,
                                 pool=pool, brain_text=brain, s1_out=s1_ok),
            "EVIDENCE_REF_UNRESOLVED")
    rejects("F6 事件 id 不在快照 → REF_UNRESOLVED",
            lambda: _review_core(dict(agood, evidence=[dict(ev_s1, ref="EV-404")]),
                                 as_of=D1, code=CODE, snap=snap, pool=pool,
                                 brain_text=brain, s1_out=s1_ok),
            "EVIDENCE_REF_UNRESOLVED")
    # 真 IO：review 经 s1_path 消费 S1 快照文件
    s1_f = os.path.join(tmp, "s_s1_%s.json" % D1)
    io.open(s1_f, "w", encoding="utf-8").write(json.dumps(s1_ok, ensure_ascii=False))
    wa = review(agood, as_of=D1, code=CODE, snapshot_root=os.path.join(tmp, "snap"),
                pool_path=pool_f, brain_state_path=bpath, path=sbpath, s1_path=s1_f)
    T("F7 review 真 IO：A 链落盘且 evidence 含 S1 来源",
      wa["driver_effective"] == "A"
      and any("S1:EVENT_EVAL" in e for e in wa["chain"]["evidence"]))
    wa2 = review(agood, as_of=D1, code=CODE, snapshot_root=os.path.join(tmp, "snap"),
                 pool_path=pool_f, brain_state_path=bpath, path=sbpath, s1_path=s1_f)
    T("F8 A 链幂等重放", wa2.get("appended") is False)
    T("F9 availability s1 如实上报（沙盒真路径无 S1 → ABSENT）",
      availability(D1, snapshot_root=os.path.join(tmp, "snap"), pool_path=pool_f,
                   brain_state_path=bpath)["s1"] == "ABSENT")

    print("E组 真库零污染")
    real_after = (hashlib.md5(io.open(CHAINS_PATH, "rb").read()).hexdigest()
                  if os.path.isfile(CHAINS_PATH) else "ABSENT")
    T("E1 自检全程未写真库", real_before == real_after)
    shutil.rmtree(tmp, ignore_errors=True)

    print("自检结果：%s" % ("全部通过 ✅" if bad[0] == 0 else "%d 项失败 ❌" % bad[0]))
    return bad[0] == 0


# ── 纪律判据（生产区源码结构断言） ────────────────────────────────────────────
def _discipline(verbose=True):
    import astwin as ASTW
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    lines = src.splitlines(True)
    drop = set()
    for lo, hi, name in ASTW.func_table(ast.parse(src)):
        if name.startswith(("_selftest", "_discipline", "_main")):
            drop.update(range(lo - 1, hi))
    prod = "".join(l for i, l in enumerate(lines) if i not in drop)
    table = ASTW.func_table(ast.parse(src))
    res = []
    res.append(("① 写点唯一且静态可解析（内联 path or CHAINS_PATH）",
                prod.count('io.open(path or CHAINS_PATH, "a"') == 1))
    res.append(('② 反例判据：两步赋值坏形态（io.open(p, "a"）不得重现',
                'io.open(p, "a"' not in prod))
    res.append(("③ 零 params.json / decision_card / position_gate 写入",
                "params.json" not in prod and "decision_card" not in prod
                and "position_gate" not in prod))
    res.append(("④ 零 approve/promote 调用 + 不接结算主链（无 settle 导入）",
                "promote(" not in prod and "APPROVE" not in prod
                and "settle" not in prod))
    res.append(("⑤ astwin 引用（生产函数表经 astwin 枚举）", bool(table)))
    ok = True
    for name, passed in res:
        if verbose:
            print(("  [ok] " if passed else "  [!!] ") + name)
        ok = ok and passed
    print("纪律结果：%s（%d/%d）" % ("通过 ✅" if ok else "失败 ❌",
                                    sum(1 for _, p in res if p), len(res)))
    return ok


def _main(argv):
    if "--selftest" in argv:
        return 0 if _selftest() else 1
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
