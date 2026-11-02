#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mech_driver.py · 机械基准判断器（C3 链提案 + C4 提案的**机械化生成**）

════════════════════════════════════════════════════════════════════════════
★ 定位（用户 2026-10-19 批准「先 B 后 A」中的 B）：
  本件是**基准，不是判断**。它把「判断」这一环用**可复现的机械规则**填上，
  让 C3→C4→C5 端到端跑通，从而在外部 LLM 判断器就位（A 路）之前，系统能
  产出**带明确标注的基准决策**。所有产物一律带 `MECHANICAL_BASELINE` 标记，
  **永不冒充** LLM 判断；A 路就位后一行参数切回，本件可整体停用。

★ 为什么不是「编数据」：
  本件**只读真实真源**（当日快照 / 候选池 / params），所有 evidence 的 ref 都能
  被 `drive._validate_evidence` 机械解析到（解析不到即 DriveError，fail-closed）；
  inference 只陈述数据关系，不含预测性叙事；prediction 由数据机械冻结。
  ⇒ 判断的「质量」是基准级的，但「可追溯性」与 LLM 判断**同一标准**。

★ 机械口径（全部可在本文件内审计，改口径 = 改本文件的常量段）：
  ① 选票    ：当日涨停池（按封单额降序）∪ 龙虎榜净买入>0（按净买入降序），取 Top N；
              两者皆无 ⇒ 零票（不凑数，不退回全市场瞎选）
  ② driver  ：在涨停池 + 在龙虎榜 → `复合`；仅涨停池 → `C 情绪扩散`；
              仅龙虎榜 → `B 资金抢占`；都不在 → 不选（驱动不明者不进候选）
              判据口径直接引 params.driver_dict 的 verify 三条（不另立标准）
  ③ 价位    ：entry_limit = 当日收盘；stop_line = 收盘 ×(1−5%)；
              target = 收盘 ×(1+10%)；window = 3 交易日
              （RR 恒 = 2.0；这两个百分比是**基准常量**，非最优参数，见 CONST）
  ④ 仓位    ：position_request = params.trial_position / 100（试仓，不越过用户参数）
  ⑤ 五件套  ：五项**全部**由当日数据机械填出（每条都带可核字段），不用空话；
              填不出的项一律 None ⇒ 三问会判 WAIT/NO（缺件即降级，绝不硬凑）
  ⑥ Thesis  ：ttype = driver 判定的 A/B/C（params.driver_dict 枚举内）；
              claim = 「[MECH] <code> 因 <驱动名>（<机械判据摘要>）具备短期动量延续条件」
              幂等：该 candidate 已有 OPEN thesis ⇒ 复用，不重复建

★ 安全锁（三道，缺一不可）：
  L1 **SIM 轴锁**：产物带 `axis: "SIM_ONLY"` + `producer: "MECHANICAL_BASELINE"`；
     `run_chain` 读到 producer=MECHANICAL_BASELINE 且非 sim ⇒ 直接 ChainError。
  L2 **零生产写入**：只写 `<base>/data/input/mech_proposals_<as_of>.json` 一个文件
     （经 `writegate.check_write`）；thesis 走 `thesis.create_thesis`（其写点已在案）。
  L3 **不碰冻结件**：不改 brief/drive/three_q/decision/orchestrate/settle 任何一行。

用法：
  --as-of <date> [--base <sandbox>] [--top N] [--plan]   生成（--plan 只打印不写）
  --selftest                                            合成夹具自检（零生产接触）
"""
import argparse
import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_FS = os.path.normpath(os.path.join(_HERE, "..", "fs"))
if _FS not in sys.path:
    sys.path.insert(0, _FS)

from paths import ROOT                                              # noqa: E402
import candidates as CAND                                           # noqa: E402
import decision as DEC                                              # noqa: E402
import snapshot as SNAP                                             # noqa: E402
import thesis as TH                                                 # noqa: E402
import writegate as _WG                                             # noqa: E402

PRODUCER = "MECHANICAL_BASELINE"
TAG = "[MECH]"
SCHEMA_VERSION = 1

# ── 基准常量（改这里 = 改口径，必须同步台账） ────────────────────────────────
STOP_PCT = 0.05          # 止损 = 收盘 ×(1−5%)
TARGET_PCT = 0.10        # 目标 = 收盘 ×(1+10%)
WINDOW_DAYS = 3          # 持有窗口（交易日）
DEFAULT_TOP = 5          # 默认取票数（Top N）
VOL_KEEP_RATIO = 0.8     # 延续条件：次日成交额 ≥ 当日 × 0.8
_DRIVER_NAME = {"A": "产业催化", "B": "资金抢占", "C": "情绪扩散", "复合": "复合驱动"}
# Thesis.type 值域 = params.driver_dict 的单字母键（A/B/C）——"复合" 不是合法 type。
# 复合驱动落池时取 **B（资金抢占）**：龙虎榜净买入是比连板更硬的资金证据，
# 且「复合」的机械前提本就要求 B/C 证据同时在场（drive._driver_effective 会复核）。
# ★ C4 提案的 driver 仍写"复合"（three_q.DRIVERS 接受），本映射只用于 Thesis 池登记。
_THESIS_TYPE = {"A": "A", "B": "B", "C": "C", "复合": "B"}

_S_INPUT = ("data", "input")
_S_PARAMS = ("data", "params.json")
_S_POOL = ("data", "state", "candidates.json")
_S_THESIS = ("memory", "state", "thesis.md")


class MechDriverError(Exception):
    """本件入参/契约违规。"""


def _check_date(s):
    if not isinstance(s, str) or len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise MechDriverError("as_of 必须是 YYYY-MM-DD，实际 %r" % (s,))
    for i in (0, 1, 2, 3, 5, 6, 8, 9):
        if not s[i].isdigit():
            raise MechDriverError("as_of 非法：%r" % (s,))
    return s


def out_path(as_of, base=None):
    """产物落点：`<base>/data/input/mech_proposals_<as_of>.json`（X6 外部判断输入区）。

    与 LLM 产物（`data/input/proposals_<as_of>.json`）**分文件存放**——
    绝不让机械基准占用正式判断通道的文件名（防"基准被误当判断"）。
    两步拼装：先常量目录（landing 可归类），再拼动态文件名。
    """
    d = os.path.join(base or ROOT, *_S_INPUT)
    return os.path.join(d, "mech_proposals_%s.json" % as_of)


def _param(base, name, default):
    p = os.path.join(base or ROOT, *_S_PARAMS)
    try:
        with io.open(p, encoding="utf-8-sig") as f:
            d = json.load(f)
        v = ((d.get("params") or {}).get(name) or {}).get("value")
        return default if v is None else v
    except Exception:                                           # noqa: BLE001
        return default


# ── ① 选票（机械优先序，不凑数） ──────────────────────────────────────────────
def pick_codes(snap, top=DEFAULT_TOP):
    """→ [(code, why)]。涨停池 ∪ 龙虎榜净买入>0，按各自的强度降序，去重取 Top N。

    两者皆无 ⇒ 空列表（当日没有可机械判断的强票；**不退回全市场瞎选**）。
    """
    if snap is SNAP.UNAVAILABLE:
        return []
    lu = {r.get("code"): r for r in (snap.get("limit_up") or []) if r.get("code")}
    dt = {r.get("code"): r for r in (snap.get("dragon_tiger") or []) if r.get("code")}
    lu_rank = sorted(lu.items(),
                     key=lambda kv: (-(kv[1].get("seal_amount") or 0),
                                     -(kv[1].get("boards") or 0), kv[0]))
    dt_net = {c: _num(r.get("net")) for c, r in dt.items()}
    dt_rank = sorted([(c, n) for c, n in dt_net.items() if n is not None and n > 0],
                     key=lambda x: (-x[1], x[0]))
    picked, seen = [], set()
    for c, _r in lu_rank:
        if len(picked) >= top:
            break
        picked.append((c, "涨停池"))
        seen.add(c)
    for c, _n in dt_rank:
        if len(picked) >= top:
            break
        if c in seen:
            continue
        picked.append((c, "龙虎榜净买入"))
        seen.add(c)
    return picked


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── ② 证据与驱动判定 ─────────────────────────────────────────────────────────
def build_evidence(code, snap, cand):
    """→ (evidence 列表, 事实摘要 dict, driver)。

    evidence 的 (source, ref) **全部**能在当日快照/候选池解析到（drive 会机械复核）：
      SNAPSHOT:daily_quote   ref=<code>
      SNAPSHOT:limit_up      ref=<code>      （在该池时）
      SNAPSHOT:dragon_tiger  ref=<code>      （在榜且净买入>0 时）
      SNAPSHOT:env_metrics   ref=<字段名>    （取最高连板字段作为环境锚）
      C2:CANDIDATE           ref=<候选 id>   （候选在场时）
    """
    q = next((r for r in (snap.get("daily_quote") or [])
              if r.get("code") == code), None)
    if q is None:
        raise MechDriverError("EVIDENCE_UNRESOLVED: %s 在 as_of 快照的 daily_quote 中不存在"
                              "（禁编造证据）" % code)
    lu = next((r for r in (snap.get("limit_up") or [])
               if r.get("code") == code), None)
    dt = next((r for r in (snap.get("dragon_tiger") or [])
               if r.get("code") == code), None)
    env = snap.get("env_metrics") or {}

    ev, facts = [], {}
    close = _num(q.get("close"))
    pct = _num(q.get("pct"))
    amt = _num(q.get("amount"))
    facts.update({"name": q.get("name"), "close": close, "pct": pct, "amount": amt,
                  "high": _num(q.get("high")), "low": _num(q.get("low"))})
    ev.append({"text": "%s(%s) 收盘 %.2f 元 涨幅 %+.2f%% 成交额 %.0f 万元"
                       % (q.get("name") or "", code, close or 0, pct or 0,
                          (amt or 0) / 1e4),
               "source": "SNAPSHOT:daily_quote", "ref": code})

    if lu is not None:
        facts["boards"] = lu.get("boards")
        facts["seal_amount"] = _num(lu.get("seal_amount"))
        facts["first_seal_time"] = lu.get("first_seal_time")
        facts["reason"] = lu.get("reason")
        parts = ["涨停 %s 连板" % (lu.get("boards") or 1)]
        if lu.get("first_seal_time"):
            parts.append("首封 %s" % lu["first_seal_time"])
        if facts["seal_amount"]:
            parts.append("封单 %.0f 万元" % (facts["seal_amount"] / 1e4))
        _rsn = (lu.get("reason") or "").strip()
        if _rsn:
            parts.append("涨停理由「%s」" % _rsn)
            # M2 板块扩散槽的依据：**同题材涨停家数**（题材标签 = 涨停池自带字段，
            # 不自造行业分类）。同一 reason 在当日涨停池里的家数即扩散强度。
            _same = [r for r in (snap.get("limit_up") or [])
                     if (r.get("reason") or "").strip() == _rsn]
            facts["sector_tag"] = _rsn
            facts["sector_same_count"] = len(_same)
        ev.append({"text": "%s %s" % (code, " / ".join(parts)),
                   "source": "SNAPSHOT:limit_up", "ref": code})

    net = _num((dt or {}).get("net"))
    if dt is not None:
        facts["dt_net"] = net
        facts["dt_buy"] = _num(dt.get("buy"))
        facts["dt_sell"] = _num(dt.get("sell"))
    if dt is not None and net is not None and net > 0:
        ev.append({"text": "%s 龙虎榜净买入 %.0f 万元（买 %.0f 万 / 卖 %.0f 万）"
                           % (code, net / 1e4, (facts["dt_buy"] or 0) / 1e4,
                              (facts["dt_sell"] or 0) / 1e4),
                   "source": "SNAPSHOT:dragon_tiger", "ref": code})

    if env.get("max_board") is not None:
        facts["env_max_board"] = env["max_board"]
        ev.append({"text": "全市场最高连板 %s 板 / 涨停 %s 家 / 炸板率 %s"
                           % (env.get("max_board"), env.get("limit_up_count"),
                              env.get("fail_rate")),
                   "source": "SNAPSHOT:env_metrics", "ref": "max_board"})

    if cand is not None:
        facts["candidate_id"] = cand.get("id")
        ev.append({"text": "候选 %s 于 %s 由 %s 路命中（当前状态 %s）"
                           % (cand.get("id"), cand.get("created_at"),
                              cand.get("source"), cand.get("state")),
                   "source": "C2:CANDIDATE", "ref": cand.get("id")})

    # ── 驱动判定（2026-10-23 加**强度门槛**）──────────────────────────────
    # ★ 原判据过松（实测致三问 0% 过滤）：
    #   原逻辑 = 在涨停池就算 driver="C"，于是回放 41/101 笔**100% 全 BUY**，
    #   三问一道题都没拦下。根因：把「今天涨停」当成了「驱动存在且还在」。
    #   设计 §5.1 原文要的是「哪种驱动，**它还在不在**」——「涨停」只说明今天有人买，
    #   不说明明天还有人接。
    #
    # ★ 新判据（每条都可机械核验，且全部读快照事实，零主观）：
    #   C 路（题材/情绪）必须**至少一条强度确认**：
    #     ① 连板 ≥ 2（首板不算「趋势延续」）
    #     ② 同题材涨停家数 ≥ 3（板块扩散，不是孤票）
    #     ③ 龙虎榜净买入 > 0（有资金背书）
    #   B 路（资金）必须**同时**满足：
    #     ① 龙虎榜净买入 > 0
    #     ② 涨停 或 同题材扩散 ≥3（纯资金买入但没有题材共识 → 不追）
    #   都不满足 ⇒ driver=None（不产判断，调用方跳过）——这是「答不出不下」的机械落点。
    #
    # ★ 为什么不直接改 three_q：three_q 是骨架冻结件，且它的逻辑本来是对的
    #   （缺件就 WAIT/NO）。病灶在**上游把不合格的票喂进来了**，修上游才对。
    has_lu = lu is not None
    has_dt = dt is not None and net is not None and net > 0
    boards = facts.get("boards") or 1
    same_n = facts.get("sector_same_count") or 0

    c_confirm = []
    if has_lu:
        if boards >= 2:
            c_confirm.append("连板%d" % boards)
        if same_n >= 3:
            c_confirm.append("题材扩散%d家" % same_n)
        if has_dt:
            c_confirm.append("龙虎榜净买%.0f万" % ((net or 0) / 1e4))

    if has_lu and has_dt and c_confirm:
        driver = "复合"
    elif has_lu and c_confirm:
        driver = "C"
    elif has_dt and (has_lu or same_n >= 3):
        driver = "B"
    else:
        driver = None                    # 强度不足 ⇒ 不产判断（调用方跳过）
    facts["has_lu"], facts["has_dt"] = has_lu, has_dt
    facts["c_confirm"] = c_confirm
    facts["driver_strength"] = ("确认[%s]" % "+".join(c_confirm)) if c_confirm else "无强度确认"
    return ev, facts, driver


def driver_basis(driver, facts):
    """驱动的**机械判据摘要**——直接对应 params.driver_dict 的 verify 三条。

    2026-10-23 加强度确认段：原文只写「涨停 N 板」，读不出「驱动还在不在」；
    现补 `确认[...]` 段，把通过门槛的具体证据列出来（连板/扩散/资金），
    让「为什么这算驱动明确」可复核。
    """
    _st = facts.get("driver_strength") or ""
    if driver == "B":
        return ("席位/资金流向可查（龙虎榜净买入 %.0f 万元）%s"
                % ((facts.get("dt_net") or 0) / 1e4, _st))
    if driver == "C":
        return ("题材发酵阶段可定（涨停 %s 连板，封单 %.0f 万元）%s"
                % (facts.get("boards") or 1,
                   (facts.get("seal_amount") or 0) / 1e4, _st))
    if driver == "复合":
        return ("资金流向可查（龙虎榜净买入 %.0f 万元）+ 连板 %s 板封单 %.0f 万元%s"
                % ((facts.get("dt_net") or 0) / 1e4, facts.get("boards") or 1,
                   (facts.get("seal_amount") or 0) / 1e4, _st))
    return "证据不足（%s）" % (_st or "无涨停池/龙虎榜强证据")


# ── ③ 价位与五件套（全机械，填不出即 None） ──────────────────────────────────
def prices(close):
    """→ dict(entry, stop, target, window)。收盘缺失 ⇒ 全 None（三问会判缺件）。"""
    if not close or close <= 0:
        return {"entry_limit": None, "stop_line": None, "target": None,
                "window": None}
    entry = round(close, 2)
    stop = round(close * (1 - STOP_PCT), 2)
    target = round(close * (1 + TARGET_PCT), 2)
    # 防御：四舍五入后必须保持 target > entry > stop（否则三问会抛 ODDS_INVERTED）
    if not (target > entry > stop):
        raise MechDriverError("ODDS_GEOMETRY: 机械价位退化 entry=%r stop=%r target=%r"
                              % (entry, stop, target))
    return {"entry_limit": entry, "stop_line": stop, "target": target,
            "window": WINDOW_DAYS}


def five_pieces(driver, facts, px):
    """五件套：**五项都由数据机械填**；任一无法填 ⇒ None（则 Q1 缺口 → WAIT/NO）。"""
    dname = _DRIVER_NAME.get(driver)
    if dname is None:
        return {k: None for k in DEC.FIVE_KEYS}
    basis = driver_basis(driver, facts)
    amt = facts.get("amount")
    out = {
        "驱动存在": "%s驱动：%s" % (TAG, basis) if basis else None,
        "当前证据": ("%s %s 收盘 %.2f 涨幅 %+.2f%% 成交额 %.0f 万元"
                     % (TAG, facts.get("name") or "", facts.get("close") or 0,
                        facts.get("pct") or 0, (amt or 0) / 1e4)
                     if facts.get("close") else None),
        "延续机制": ("%s 次日成交额 ≥ 当日 %.0f 万元的 %.0f%% 且收盘不破 %.2f 元"
                     % (TAG, (amt or 0) / 1e4, VOL_KEEP_RATIO * 100,
                        px["entry_limit"]) if amt and px["entry_limit"] else None),
        "失效条件": ("%s 收盘价跌破 %.2f 元（收盘 ×(1−%.0f%%)）"
                     % (TAG, px["stop_line"], STOP_PCT * 100)
                     if px["stop_line"] else None),
        "剩余预期": ("%s 目标 %.2f 元，距当日收盘 %.2f 元约 %+.1f%%（%d 个交易日内）"
                     % (TAG, px["target"], px["entry_limit"], TARGET_PCT * 100,
                        WINDOW_DAYS) if px["target"] and px["entry_limit"] else None),
    }
    return out


# ── ④ C3 链提案 / C4 提案 ────────────────────────────────────────────────────
def chain_proposal(code, driver, facts, ev, px, five, as_of, thesis_id):
    """C3 链提案（drive.review 的入参契约：driver/evidence/inference/prediction/five）。"""
    inf = ["%s 机械判定驱动=%s（%s）" % (TAG, driver, driver_basis(driver, facts))]
    if facts.get("boards"):
        inf.append("%s 连板高度 %s 板，属当日涨停梯队成员"
                   % (TAG, facts["boards"]))
    if facts.get("dt_net"):
        inf.append("%s 龙虎榜净买入为正（%.0f 万元），存在主动承接资金"
                   % (TAG, (facts["dt_net"] or 0) / 1e4))
    inf.append("%s 价位按基准常量机械设定：止损 −%.0f%% / 目标 +%.0f%% / 窗口 %d 日"
               % (TAG, STOP_PCT * 100, TARGET_PCT * 100, WINDOW_DAYS))

    # ★ 三槽位语义（decision.PRED_SLOTS 的设计口径，禁 M1M3 合并）：
    #   M1 = 个股涨幅 / M2 = **板块扩散家数** / M3 = 成交额维持
    #   M2 无依据（涨停理由缺失）⇒ 该票**不产 BUY 级提案**，由 build() 直接跳过，
    #   绝不用「全市场涨停家数」冒充板块扩散（那是偷换口径）。
    pred = [
        {"prediction_id": "M1", "metric": "个股 T+3 收盘价相对当日收盘价",
         "data_source": "SNAPSHOT:daily_quote", "direction": "UP",
         "condition": "%s 个股 3 个交易日内收盘涨幅 ≥ %.0f%%" % (TAG, 2.0),
         "baseline": facts.get("close"),
         "threshold": (round(facts["close"] * 1.02, 2) if facts.get("close") else None),
         "window": 3},
        {"prediction_id": "M2", "metric": "同题材（涨停理由）涨停家数",
         "data_source": "SNAPSHOT:limit_up", "direction": "FLAT",
         "condition": "%s 次日同题材「%s」涨停家数 ≥ %s 家"
                      % (TAG, facts.get("sector_tag"), facts.get("sector_same_count")),
         "baseline": facts.get("sector_same_count"),
         "threshold": facts.get("sector_same_count"),
         "window": 1},
        {"prediction_id": "M3", "metric": "次日成交额相对当日成交额之比",
         "data_source": "SNAPSHOT:daily_quote", "direction": "FLAT",
         "condition": "%s 次日成交额 ≥ 当日 × %.0f%%" % (TAG, VOL_KEEP_RATIO * 100),
         "baseline": facts.get("amount"),
         "threshold": VOL_KEEP_RATIO,
         "window": 1},
    ]
    return {"thesis_id": thesis_id, "driver": driver, "evidence": ev,
            "inference": inf, "prediction": pred, "five": five}


def c4_proposal(code, driver, facts, px, as_of, thesis_id, cand, env_room):
    """C4 提案（brief.PROPOSAL_KEYS 契约）。basis 只用**确定在场**的来源。"""
    basis = []
    if cand is not None:
        basis.append({"ref": "C2:CANDIDATE:%s" % cand.get("id"),
                      "claim": "该票由 C2 %s 路于 %s 命中" % (cand.get("source"),
                                                             cand.get("created_at"))})
    if facts.get("env_max_board") is not None:
        basis.append({"ref": "C1:ENV:max_board",
                      "claim": "全市场最高连板 %s 板（环境档在场）"
                               % facts.get("env_max_board")})
    basis.append({"ref": "C3:CHAIN:mech", "claim": "C3 机械链已落盘（producer=%s）"
                                                % PRODUCER})
    pos = _param(None, "trial_position", 10)
    return {
        "schema_version": 1, "as_of": as_of, "code": code, "thesis_id": thesis_id,
        "driver": driver,
        "entry_limit": px["entry_limit"], "stop_line": px["stop_line"],
        "target": px["target"], "window": px["window"],
        "position_request": (round(float(pos) / 100.0, 4) if pos else None),
        "completion_event": None,
        "overturn_event": ({"text": "%s 收盘价跌破 %.2f 元即翻案（机械止损线）"
                                   % (TAG, px["stop_line"]),
                            "rule": {"field": "close", "op": "<",
                                     "value": px["stop_line"]}}
                           if px["stop_line"] else None),
        "action_proposed": "BUY",
        "basis": basis,
        "missing": [],
        "confidence": None,
    }


# ── ⑤ 装配 ───────────────────────────────────────────────────────────────────
def _cand_for(code, pool):
    rows = [c for c in (pool or []) if c.get("code") == code
            and c.get("state") not in ("EXPIRED", "TRADED")]
    return rows[-1] if rows else None


def ensure_thesis(code, cand, driver, facts, as_of, base=None):
    """→ (thesis_id, created)。幂等：该票已有 OPEN thesis ⇒ 复用。

    ★ 不代建「外部判断」：claim 是机械判据的直述（带 [MECH] 前缀），不是叙事。
    """
    path = os.path.join(base or ROOT, *_S_THESIS)
    if not os.path.isfile(path):
        # Thesis 池缺席 ⇒ **显式**初始化（`thesis.py` 的纪律是「不得静默新建」，
        # 这里的初始化是本件明写的一步，不是隐式副作用；沙盒与首跑都用它）
        TH.init_file(path)
    for it in (TH.list_thesis(code=code, path=path) or []):
        if it.get("state") == TH.STATE_OPEN:
            return it.get("id"), False
    claim = ("%s %s 因 %s（%s）具备短期动量延续条件"
             % (TAG, code, _DRIVER_NAME.get(driver),
                driver_basis(driver, facts)))
    th = TH.create_thesis(as_of, _THESIS_TYPE.get(driver, driver), claim, code=code,
                          candidate_id=(cand or {}).get("id"), path=path)
    return th.get("id"), True


def build(as_of, base=None, top=DEFAULT_TOP, want_thesis=True):
    """→ (doc, notes)。doc 即 proposals 文件内容（proposals + chains 双层）。

    **不写盘**（写盘见 `emit`）——便于自检与 `--plan` 复用同一路径。
    """
    d = _check_date(as_of)
    snap = SNAP.load_snapshot(d, root=os.path.join(base or ROOT, "data", "snapshot"))
    if snap is SNAP.UNAVAILABLE:
        raise MechDriverError("DATA_UNAVAILABLE: as_of=%s 无快照（机械基准不得无据产出）"
                              % d)
    pool_p = os.path.join(base or ROOT, *_S_POOL)
    try:
        with io.open(pool_p, encoding="utf-8-sig") as f:
            pool = (json.load(f) or {}).get("candidates") or []
    except Exception:                                           # noqa: BLE001
        pool = []

    picked = pick_codes(snap, top=top)
    proposals, chains, notes = {}, {}, []
    for code, why in picked:
        cand = _cand_for(code, pool)
        try:
            ev, facts, driver = build_evidence(code, snap, cand)
        except MechDriverError as e:
            notes.append({"code": code, "skipped": str(e)})
            continue
        if driver is None:
            notes.append({"code": code, "skipped": "驱动不明（无涨停池/龙虎榜强证据）"})
            continue
        if not facts.get("sector_same_count"):
            notes.append({"code": code,
                          "skipped": "涨停理由(reason)缺失 ⇒ M2 板块扩散槽无依据，"
                                     "不产判断（禁以全市场涨停家数冒充板块扩散）"})
            continue
        px = prices(facts.get("close"))
        five = five_pieces(driver, facts, px)
        if want_thesis:
            th_id, created = ensure_thesis(code, cand, driver, facts, d, base=base)
        else:
            th_id, created = "TH-MECH-PLACEHOLDER", False
        proposals[code] = c4_proposal(code, driver, facts, px, d, th_id, cand,
                                      snap.get("env_metrics"))
        chains[code] = chain_proposal(code, driver, facts, ev, px, five, d, th_id)
        notes.append({"code": code, "pick": why, "driver": driver,
                      "thesis_id": th_id, "thesis_created": created,
                      "entry": px["entry_limit"], "stop": px["stop_line"],
                      "target": px["target"]})

    doc = {"schema_version": SCHEMA_VERSION, "as_of": d,
           "producer": PRODUCER, "axis": "SIM_ONLY",
           "note": ("机械基准判断（用户 2026-10-19 批准「先 B 后 A」的 B 路）。"
                    "非 LLM 判断，产物仅供打通 C3→C4→C5 链路与建立基准对照；"
                    "A 路（外部判断生成器）就位后应停用本件。口径见 "
                    "tools/decision/mech_driver.py 头部常量段。"),
           "proposals": proposals, "chains": chains,
           "mech_meta": {"top": top, "n_picked": len(picked),
                         "n_proposed": len(proposals), "notes": notes}}
    return doc, notes


def emit(as_of, base=None, top=DEFAULT_TOP):
    """写盘（唯一写点，经 writegate.check_write）。→ (path, doc, notes)"""
    doc, notes = build(as_of, base=base, top=top)
    p = out_path(as_of, base=base)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    _WG.check_write(p)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return p, doc, notes


# ── CLI ──────────────────────────────────────────────────────────────────────
def _main(argv):
    ap = argparse.ArgumentParser(description="机械基准判断器（C3 链 + C4 提案生成）")
    ap.add_argument("--as-of", dest="as_of")
    ap.add_argument("--base", default=None)
    ap.add_argument("--top", type=int, default=DEFAULT_TOP)
    ap.add_argument("--plan", action="store_true", help="只打印，不写盘")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if not a.as_of:
        ap.print_help()
        return 2
    if a.base and not os.path.isdir(a.base):
        print("--base 必须是已存在目录", file=sys.stderr)
        return 2
    if a.plan:
        doc, notes = build(a.as_of, base=a.base, top=a.top)
        print("机械基准计划 as_of=%s top=%d" % (a.as_of, a.top))
        for n in notes:
            print("  %s" % json.dumps(n, ensure_ascii=False))
        print("产物将写入：%s" % out_path(a.as_of, base=a.base).replace("\\", "/"))
        print("（--plan 零写入；thesis 亦未创建）")
        return 0
    p, doc, notes = emit(a.as_of, base=a.base, top=a.top)
    print("已生成 %s" % p.replace("\\", "/"))
    for n in notes:
        print("  %s" % json.dumps(n, ensure_ascii=False))
    print("proposals=%d chains=%d producer=%s axis=SIM_ONLY"
          % (len(doc["proposals"]), len(doc["chains"]), doc["producer"]))
    return 0


# ── 自检（合成夹具 · 零生产写） ──────────────────────────────────────────────
def _selftest():
    import shutil as _sh
    import tempfile
    ok = fail = 0

    def T(name, cond, extra=None):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s%s" % ("✓" if cond else "✗", name,
                               "" if cond else "  ← %s" % (extra,)))

    print("== mech_driver --selftest ==")
    sb = tempfile.mkdtemp(prefix="mech_sb_")
    try:
        for x in ("data/snapshot", "data/state", "data/input",
                  "memory/state", "data/generated"):
            os.makedirs(os.path.join(sb, x.replace("/", os.sep)), exist_ok=True)
        # params 与候选池从生产只读复制（契约形态真实），snapshot 用合成夹具
        _sh.copy(os.path.join(ROOT, "data", "params.json"),
                 os.path.join(sb, "data", "params.json"))
        def _cand(i, code, src):
            return {"id": "CD-20260930-%03d" % i, "code": code,
                    "state": "DISCOVERED", "created_at": "2026-09-30",
                    "last_seen": "2026-09-30", "expiry": "2026-10-09",
                    "source": src,
                    "trigger": {"text": "C2 命中 2026-09-30: %s 路" % src,
                                "rule": None},
                    "invalid_condition": None, "next_trigger": None,
                    "keep_reason": None, "thesis_id": None, "thesis": None}
        pool = {"version": 2, "updated_at": "2026-09-30", "candidates": [
            _cand(1, "sh600001", "limit_up"),
            _cand(2, "sh600002", "news_hot"),
            _cand(3, "sh600003", "limit_up"),
        ]}
        with io.open(os.path.join(sb, "data", "state", "candidates.json"),
                     "w", encoding="utf-8") as f:
            json.dump(pool, f, ensure_ascii=False)
        pay = {"daily_quote": [
                   {"code": "sh600001", "name": "甲", "open": 10, "high": 11,
                    "low": 9.8, "close": 11.0, "pct": 10.0, "vol": 100000,
                    "amount": 10000000.0},
                   {"code": "sh600002", "name": "乙", "open": 10, "high": 10.5,
                    "low": 9.9, "close": 10.2, "pct": 2.0, "vol": 100000,
                    "amount": 5000000.0},
                   {"code": "sh600003", "name": "丙", "open": 10, "high": 12,
                    "low": 10, "close": 12.0, "pct": 9.99, "vol": 100000,
                    "amount": 8000000.0},
               ],
               "limit_up": [
                   {"code": "sh600001", "name": "甲", "boards": 3,
                    "first_seal_time": "09:31:00", "seal_amount": 50000000.0,
                    "reason": "电池"},
                   {"code": "sh600003", "name": "丙", "boards": 2,
                    "first_seal_time": None, "seal_amount": None, "reason": None},
               ],
               "dragon_tiger": [
                   {"code": "sh600001", "name": "甲", "seat": "机构专用",
                    "buy": 80000000.0, "sell": 30000000.0, "net": 50000000.0},
                   {"code": "sh600002", "name": "乙", "seat": "机构专用",
                    "buy": 20000000.0, "sell": 50000000.0, "net": -30000000.0},
               ],
               "news": [],
               "env_metrics": {"limit_up_count": 40, "max_board": 5,
                               "turnover": 1000000000000.0, "up_down_ratio": 1.2,
                               "fail_rate": 0.2, "index_ma20": 3900.0}}
        _wr = SNAP.write_snapshot("2026-09-30", pay,
                                  root=os.path.join(sb, "data", "snapshot"))
        T("A0 夹具快照落盘成功（WRITTEN，非 EXISTS）", _wr == "WRITTEN", _wr)

        # A 组：入参
        for bad in ("2026/09/30", "20260930", None):
            try:
                build(bad, base=sb)
                T("A1 非法日期拒绝 %r" % (bad,), False)
            except (MechDriverError, Exception):
                T("A1 非法日期拒绝 %r" % (bad,), True)

        # B 组：选票与驱动判定
        s2 = SNAP.load_snapshot("2026-09-30",
                               root=os.path.join(sb, "data", "snapshot"))
        picked = pick_codes(s2, top=5)
        T("B1 选票 = 涨停池(封单降序)∪龙虎榜净买入>0（乙净卖出被排除）",
          [c for c, _w in picked] == ["sh600001", "sh600003"], picked)
        _ev, f1, dr1 = build_evidence("sh600001", s2,
                                      _cand_for("sh600001", pool["candidates"]))
        T("B2 涨停+龙虎榜 ⇒ 复合驱动", dr1 == "复合", dr1)
        _ev, f3, dr3 = build_evidence("sh600003", s2, None)
        T("B3 仅涨停 ⇒ C 情绪扩散", dr3 == "C", dr3)
        _ev, f2, dr2 = build_evidence("sh600002", s2, None)
        T("B4 仅净卖出龙虎榜 ⇒ 驱动不明（None，不产判断）", dr2 is None, dr2)

        # C 组：证据可解析性（drive 的机械校验会复核 ref）
        _AN = os.path.normpath(os.path.join(_HERE, "..", "analysis"))
        if _AN not in sys.path:
            sys.path.insert(0, _AN)
        import drive as DRV
        ev1, _fx, _dx = build_evidence("sh600001", s2,
                                      _cand_for("sh600001", pool["candidates"]))
        T("C1 证据 source 全在 drive.SOURCES 枚举内 + daily_quote ref 即票码",
          all(e["source"] in DRV.SOURCES for e in ev1)
          and any(e["source"] == "SNAPSHOT:daily_quote" and e["ref"] == "sh600001"
                  for e in ev1), [e["source"] for e in ev1])

        # D 组：价位与五件套
        px = prices(11.0)
        T("D1 价位几何合法（target>entry>stop）",
          px["target"] > px["entry_limit"] > px["stop_line"], px)
        T("D2 止损/目标 = 基准常量 −5%%/+10%%",
          abs(px["stop_line"] - 10.45) < 1e-9 and abs(px["target"] - 12.1) < 1e-9, px)
        fv = five_pieces("C", f1, px)
        T("D3 五件套五项全非空（数据够时不留空话）",
          all(isinstance(fv[k], str) and fv[k].strip() for k in DEC.FIVE_KEYS), fv)
        T("D4 五件套键 = decision.FIVE_KEYS（零重定义）",
          tuple(fv.keys()) == tuple(DEC.FIVE_KEYS))

        # E 组：端到端装配（thesis + proposals + chains）
        doc, notes = build("2026-09-30", base=sb, top=5)
        T("E1 仅产出有题材依据的票（甲），producer/axis 标记齐",
          set(doc["proposals"]) == {"sh600001"} and doc["producer"] == PRODUCER
          and doc["axis"] == "SIM_ONLY", doc.get("mech_meta"))
        T("E1b 涨停理由缺失的丙被跳过（M2 无依据 ⇒ 不产 BUY 级判断，不偷换口径）",
          any(n.get("code") == "sh600003" and "M2" in str(n.get("skipped"))
              for n in notes), doc.get("mech_meta", {}).get("notes"))
        T("E2 chains 与 proposals 同票同 thesis_id",
          set(doc["chains"]) == set(doc["proposals"])
          and all(doc["chains"][c]["thesis_id"] == doc["proposals"][c]["thesis_id"]
                  for c in doc["chains"]))
        th = TH.list_thesis(code="sh600001",
                            path=os.path.join(sb, "memory", "state", "thesis.md"))
        T("E3 Thesis 已建且 type 落在 driver_dict 枚举内",
          th and th[-1].get("type") in ("A", "B", "C"), th)
        doc2, _n2 = build("2026-09-30", base=sb, top=5)
        th2 = TH.list_thesis(code="sh600001",
                             path=os.path.join(sb, "memory", "state", "thesis.md"))
        T("E4 幂等：重跑不重复建 Thesis", len(th2) == len(th),
          (len(th), len(th2)))

        # F 组：与真实校验器对齐（drive.review 与 brief.review_proposal 都过）
        c3 = doc["chains"]["sh600001"]
        os.makedirs(os.path.join(sb, "data", "research", "drive"), exist_ok=True)
        rv = DRV.review(c3, as_of="2026-09-30", code="sh600001",
                        path=os.path.join(sb, "data", "research", "drive",
                                          "chains.jsonl"),
                        snapshot_root=os.path.join(sb, "data", "snapshot"),
                        pool_path=os.path.join(sb, "data", "state", "candidates.json"),
                        brain_state_path=None)
        T("F1 drive.review 接受机械链提案（证据全部解析到）",
          rv.get("appended") is True, rv)
        import brief as BR
        b = BR.assemble("2026-09-30", "sh600001",
                        pool_path=os.path.join(sb, "data", "state", "candidates.json"),
                        thesis_path=os.path.join(sb, "memory", "state", "thesis.md"),
                        chains_path=os.path.join(sb, "data", "research", "drive",
                                                 "chains.jsonl"),
                        base=sb, params_path=os.path.join(sb, "data", "params.json"),
                        brain_path=os.path.join(sb, "memory", "state", "brain_state.md"))
        st, info = BR.classify(doc["proposals"]["sh600001"], b)
        T("F2 brief.classify 接受机械 C4 提案（basis 全部在场）",
          st == BR.ST_OK, (st, info))

        # G 组：安全锁
        p, doc3, _n3 = emit("2026-09-30", base=sb, top=5)
        T("G1 产物落 data/input/mech_proposals_<as_of>.json（不与 LLM 产物同名）",
          p.replace("\\", "/").endswith("data/input/mech_proposals_2026-09-30.json"), p)
        with io.open(p, encoding="utf-8") as f:
            raw = json.load(f)
        T("G2 产物自带 producer/axis/note 三标记（可被下游识别与阻止 LIVE）",
          raw.get("producer") == PRODUCER and raw.get("axis") == "SIM_ONLY"
          and "非 LLM 判断" in (raw.get("note") or ""))
        # 只看**模块顶层**（自检内的局部 import 是为验证"外部校验器能接受"而临时加载，
        # 不构成本件的运行期依赖面）
        src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
        head = src.split("def _selftest", 1)[0]
        _bad_imports = [m for m in ("import brief", "import orchestrate",
                                    "import settle", "import three_q",
                                    "import registry", "import brief as")
                        if m in head]
        T("G3 本件顶层不 import 判断/冻结件（零改冻结面）",
          not _bad_imports, _bad_imports)
    finally:
        _sh.rmtree(sb, ignore_errors=True)

    # ── H 驱动强度门槛（2026-10-23 修三问 0% 过滤）──
    # 原判据「在涨停池 ⇒ driver=C」致回放 100% 全 BUY；下面盯死五类边界。
    def _mk(lu=None, dt=None, net=None, code="sh600000"):
        """造最小快照（**必须含 daily_quote**，否则 build_evidence 判 EVIDENCE_UNRESOLVED）。

        这是设计正确的硬闸：无当日行情就不许造证据。自检也走同一闸。
        """
        snap = {"daily_quote": [{"code": code, "close": 10.0, "pct": 10.0}]}
        if lu is not None:
            snap["limit_up"] = lu if isinstance(lu, list) else [lu]
        if dt is not None:
            snap["dragon_tiger"] = [dt]
        return snap

    def _driver(snap, code="sh600000"):
        ev, facts, drv = build_evidence(code, snap, None)
        return drv, facts

    T("H1 首板 + 无扩散 + 无龙虎榜 → driver=None（首板不算趋势延续）",
      _driver(_mk(lu={"code": "sh600000", "boards": 1,
                      "reason": "独苗题材"}))[0] is None)
    T("H2 首板 + 同题材扩散≥3 → driver=C（板块共识成立）",
      _driver(_mk(lu=[{"code": "sh600000", "boards": 1, "reason": "AI"},
                      {"code": "sh600001", "boards": 1, "reason": "AI"},
                      {"code": "sh600002", "boards": 1, "reason": "AI"},
                      {"code": "sh600003", "boards": 1, "reason": "AI"}]))[0] == "C")
    T("H3 连板≥2 → driver=C（趋势延续成立）",
      _driver(_mk(lu={"code": "sh600000", "boards": 2,
                      "reason": "孤票"}))[0] == "C")
    T("H4 龙虎榜净买>0 但既不涨停也无扩散 → driver=None（纯资金不追）",
      _driver(_mk(dt={"code": "sh600000", "net": 1e7}))[0] is None)
    T("H5 龙虎榜净买>0 + 涨停 → driver=复合（资金+题材双确认）",
      _driver(_mk(lu={"code": "sh600000", "boards": 2, "reason": "x"},
                  dt={"code": "sh600000", "net": 1e7}))[0] == "复合")
    T("H6 强度确认文案进 driver_strength（可复核为什么算驱动明确）",
      "连板2" in (_driver(_mk(lu={"code": "sh600000", "boards": 2,
                                  "reason": "x"}))[1].get("driver_strength") or ""))

    print("== mech_driver --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
