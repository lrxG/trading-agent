#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""brief.py · C4 三问接入接缝（装配 + 校验；施工 1.4 C4 契约批 · v1.0 · 2026-10-12 建）

用途：C4 的**接缝件**——把内核 LLM 做三问所需的一切（brief）从真源装配出来，
      把 LLM 的三问输出（proposal）校验成 `three_q.answer` 的入参。
      **判断本体在 LLM（AGENT.md 内核 4.1）**：本件不判动作、不产新证据、不裁仓位；
      动作由 `three_q` 机械分流，落账由 `orchestrate.decide`。

★ 一、为什么存在 —— 施工 1.4 原文「LLM 判断流程（格式固定）」的那一半
  已落盘：`three_q.py`（机械裁决）+ `orchestrate.py`（幂等落账）。
  缺口：LLM 吃什么（输入装配）/ 吐什么（输出契约）/ 失败怎么办（安全行为）
  —— 三件都没有机械落点，故「C4 整体验收」一直挂着。本件 = 这三件的落点。

★ 二、环境装配（C1 env）：唯一状态载体 = `brain_state.md`「环境档」段
  `regime.update_brain` 写、本件**只读**读回（不导入 regime 模块，保持零跨模块写点面
  ——同 `drive.py` 消费 S1 的先例）。段缺失 / 档位 = `UNAVAILABLE` → 如实
  `source="UNAVAILABLE"`（**禁编档**；C1 是环境天花板，编一个假档 = 污染所有下游）。

★ 三、证据间（evidence_room）= LLM 唯一可引用的宇宙
  五类来源，各带引用命名空间，缺就缺（如实 None）：
    C1:ENV          ← brain_state 环境档（regime / 六信号）
    C2:CANDIDATE    ← 候选池（candidates.list_candidates）
    C3:CHAIN        ← 驱动链（drive.load_chain，仅认 as_of 当日链，旧链不冒充）
    S1:EFFECT       ← S1 效应库（loader.load_knowledge("s1")，32 类实证）
    S3:EMOTION      ← S3 情绪结构（loader.load_s_output("s3") + load_knowledge("s3")）
  proposal 的 basis 引用**必须落在本宇宙内**；引用不可追溯 → 拒（防编造证据）。

★ 四、失败安全（5 类，机械可判；前 4 类**不落账**）
  BRIEF_INCOMPLETE    brief 缺决策前提（无 thesis_id / 无驱动链）→ 不调用 LLM、不落账
  LLM_UNAVAILABLE     proposal 缺席（端点不可用 / 超时 / 未产出）→ 不落账
  PROPOSAL_MALFORMED  结构不合规（缺字段 / 类型错 / 空话 / 串号）→ 拒收，不落账
  EVIDENCE_UNRESOLVED basis 引用不可追溯（编造证据 / 引用缺席来源）→ 拒收，不落账
  证据不足但合规      → **放行** → three_q 机械分流 WAIT/NO（正常业务，不是失败）
  理由：记一个 LLM 没真正给出的决策 = 污染账本（C6 学习闭环读它）→ 比不决策更坏。
  「答不出就不下」在本层的落点 = **不落账**，不是落一个 NO 凑数。

★ 五、零写点：本件不写任何文件。唯一落账路径 = `orchestrate.decide` → 决策账本
  （已登记的骨架写点）。故本件不新增落点登记。

用法：
  python tools/decision/brief.py --assemble --as-of 2026-09-29 --code sh600000
  python tools/decision/brief.py --selftest
  python tools/decision/brief.py --discipline
"""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "..", ".."))
for _p in ("fs", "analysis", "decision", "discovery"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import decision as DEC        # noqa: E402
import three_q as TQ          # noqa: E402
import candidates as CAND     # noqa: E402
import thesis as TH           # noqa: E402
import loader as LD           # noqa: E402
import drive as DRV           # noqa: E402
import orchestrate as ORCH    # noqa: E402

SCHEMA_VERSION = 1

BRAIN_PATH = os.path.join(ROOT := DEC.ROOT, "memory", "state", "brain_state.md")

# ── 零重定义：全部取上游真源（is 判定见自检 A 组） ─────────────────────────────
DRIVERS = DEC.DRIVERS                    # ("A","B","C","复合","驱动不明")
ACTIONS = DEC.ACTIONS                    # ("BUY","WAIT","NO")
DRIVER_UNKNOWN = "驱动不明"
COND_KEYS = TQ.COND_KEYS                 # ("text","rule")  0.5 同形，禁第二套条件语言
COND_OPS = TQ.COND_OPS
VAGUE_WORDS = TQ.VAGUE_WORDS             # 空话黑名单（1.4 零模糊输出）

# proposal 的字段（LLM 输出）—— 每条都是 answer() 的入参或审计对照量
PROPOSAL_KEYS = ("schema_version", "as_of", "code", "thesis_id", "driver",
                 "entry_limit", "stop_line", "target", "window", "position_request",
                 "completion_event", "overturn_event", "action_proposed",
                 "basis", "missing", "confidence")
# answer() 只吃这些（driver 与四价位 + 两事件）；其余为 brief 侧或审计侧
ANSWER_KEYS = ("driver", "entry_limit", "stop_line", "target", "window",
               "position_request", "completion_event", "overturn_event")

# 引用命名空间（evidence_room 的键 ↔ basis.ref 前缀）
REF_NS = {"C1:ENV": "env", "C2:CANDIDATE": "candidate", "C3:CHAIN": "chain",
          "S1:EFFECT": "s1_effects", "S3:EMOTION": "s3_emotion",
          "S4:CROWDING": "s4_crowding", "S8:POLICY": "s8_policy",
          "S5:CAPITAL": "s5_capital", "S2:THEME": "s2_theme",
          "S6:VALUATION": "s6_valuation"}

# S8 单票最多带几条事件进 brief（防淹没：单票 != 全市场政策清单）
_S8_MAX_EVENTS = 10
# S2 单票最多带几个题材（一只票可属数十个概念板块，全带会淹没判断）
_S2_MAX_THEMES = 8
# 旧包只读外部输入根（S2 成分股映射来源；同 s1/s2/s3/s5 先例，禁写禁搬）
_OLD_PKG_ROOT = "C:\\Users\\G\\炒股智能体_旧包_20261008"

# 失败安全状态（**不落账**的四类 + 通过）
ST_BRIEF_INCOMPLETE = "BRIEF_INCOMPLETE"
ST_LLM_ABSENT = "LLM_UNAVAILABLE"
ST_MALFORMED = "PROPOSAL_MALFORMED"
ST_EVIDENCE = "EVIDENCE_UNRESOLVED"
ST_OK = "OK"

# 决策前提（缺任一 = 无决策主体，不该调用 LLM、也不该落账）
PREREQ_KEYS = ("thesis_id", "chain")


class BriefError(Exception):
    """装配失败（真源结构异常）。str 以 REASON_CODE: 开头——每类可机械断言。"""


# ── C1 环境档读回（纯文件只读；不导入 regime） ────────────────────────────────
_ENV_SEC = re.compile(r"^## 环境档[^\n]*\n(.*?)(?=^## )", re.S | re.M)
_MD_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*$", re.M)
_SIG_MAP = (("涨停家数", "limit_up_count", int), ("连板高度", "max_board", int),
            ("涨跌家数比（只记录）", "up_down_ratio", float),
            ("指数20日线（只记录）", "index_ma20", float))


def _env_unavailable():
    return {"regime": None, "position_cap": None, "signals": {}, "leg": "EOD",
            "source": "UNAVAILABLE", "as_of": None}


def _num(v, cast):
    try:
        return cast(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def read_env(brain_path=None, base=None):
    """`brain_state.md` 环境档段 → C1 env（1.1 契约形态）。缺段 / 值 UNAVAILABLE → 如实报缺。

    只读解析，零编造：解析不出的信号**不填 0**、不猜档。

    **路径解析（禁跨环境静默回退）**：显式 `brain_path` > `base` 派生
    `base/memory/state/brain_state.md`（沙盒隔离）> `BRAIN_PATH`（正式默认，行为不变）。
    ★ 2026-10-19 修：原实现无 `base`，调用方给了 `base` 仍读到**生产** `brain_state.md`。

    **返回字段**：`regime` / `position_cap` / `signals` / `leg` / `source` / `as_of`。
    `as_of` = 环境档自身时点（源文件 `as_of / source` 行的左半），用于下游做
    **环境锚时点核验**（如 `mainline_radar` 核对环境档是否与报告 as_of 同日）。
    源文件没有该行 / 左半不是 `YYYY-MM-DD` → `None`（**缺就是缺，不猜、不回退**）。

    ⚠️ **`as_of` 不进账本契约**：写决策时走 `env_ledger()` 投影（恰四字段
    `regime/signals/leg/source`），`as_of` 不会到达 `decision._norm_env`（闭集校验）
    也不进 `orchestrate.info_set_hash`（只取 `regime`+`signals`）——
    故本字段是**纯读回增强，零指纹影响、零写入面变更**。
    """
    p = brain_path or (os.path.join(base, "memory", "state", "brain_state.md")
                       if base else BRAIN_PATH)
    if not os.path.isfile(p):
        return _env_unavailable()
    text = io.open(p, encoding="utf-8").read()
    m = _ENV_SEC.search(text)
    if not m:
        return _env_unavailable()
    rows = {}
    for k, v in _MD_ROW.findall(m.group(1)):
        rows[k.strip()] = v.strip()
    grade_raw = rows.get("档位", "")
    if "UNAVAILABLE" in grade_raw or not grade_raw:
        return _env_unavailable()
    grade = grade_raw.strip("*` ").strip()
    sig = {}
    for label, key, cast in _SIG_MAP:
        val = _num(rows.get(label), cast)
        if val is not None:
            sig[key] = val
    fr = rows.get("炸板率")
    if fr is not None:
        v = _num(fr.rstrip("%"), float)
        if v is not None:
            sig["fail_rate"] = v / 100.0
    tv = rows.get("成交额（只记录）")
    if tv is not None:
        v = _num(tv.split("万亿")[0], float)
        if v is not None:
            sig["turnover"] = v * 1e12
    cap_v = _num(rows.get("仓位天花板（成）"), float)
    # `as_of / source` 行 → (as_of, source)。source 语义与拆分方式**与既有实现逐字一致**
    # （取最后一个 `/` 段），仅在左侧多读出一个 as_of。
    asof_src = rows.get("as_of / source", "")
    parts = asof_src.split("/")
    src = parts[-1].strip() if len(parts) > 1 else "UNKNOWN"
    left = parts[0].strip() if len(parts) > 1 else ""
    env_asof = left if _DATE_ONLY.match(left) else None
    return {"regime": grade, "position_cap": cap_v, "signals": sig, "leg": "EOD",
            "source": src or "UNKNOWN", "as_of": env_asof}


# ── S1 / S3 消费（经 loader 上游产物入口；缺口如实 None） ──────────────────────
def _load_s1_room(as_of, base=None):
    rec = LD.load_knowledge("s1", as_of, base=base)
    if rec is LD.UNAVAILABLE:
        return None
    cats = rec.get("cats") or rec.get("categories") or {}
    names = sorted(cats.keys()) if isinstance(cats, dict) else sorted(cats or [])
    return {"kb_version": rec.get("version"), "kb_as_of": rec.get("as_of")
            or rec.get("data_end"), "categories": names, "n_categories": len(names)}


def _load_s4_room(as_of, code=None, base=None):
    """S4 泡沫/拥挤度（**本票视角**）——2026-10-23 接入。

    ★ 为什么先接 S4 而不是 S2/S8：
      S4 快照的 `codes` 是 **code → {crowding, evidence, reasons}** 的字典
      （实测 10-22 有 38 只、n_high=11），**天生带个股维度**，可直接按 code 取；
      S8 事件只有 `related_codes`（要反查）；S2 只有板块维度、无成分股映射
      （题材行情是成分股等权聚合，但快照不回吐成员表）⇒ 单票决策拿不到。

    ★ 只取本票，不把全市场 38 只都塞进 brief：
      LLM 判断的是「这一只」，给他别票的拥挤度只会诱导横向比较与串号。
      `market` 只留一个聚合计数（当日 high 档共几只）作为背景量，不含别票标识。

    ★ `froth_stock` 恒 UNAVAILABLE（个股级估值源未工程化）——
      如实透传，不补 0、不拿环境级冒充个股级（M-「不伪装」纪律）。
    """
    rec = LD.load_s_output("s4", as_of, base=base)
    if rec is LD.UNAVAILABLE:
        return None
    doc = dict(rec)
    codes = doc.get("codes") or {}
    mine = None
    if code and isinstance(codes, dict):
        hit = codes.get(code)
        if isinstance(hit, dict):
            mine = {"code": code,
                    "crowding": hit.get("crowding"),
                    "evidence": hit.get("evidence"),
                    "reasons": hit.get("reasons")}
    return {"as_of": doc.get("as_of"), "schema": doc.get("schema"),
            "this_code": mine,
            "in_universe": bool(mine),
            "market": {"n_codes": doc.get("n_codes"), "n_high": doc.get("n_high")},
            "froth_stock": doc.get("froth_stock")}


def _load_s8_room(as_of, code=None, base=None):
    """S8 政策/产业传导链（**本票相关政策事件**）——2026-10-23 接入。

    ★ 与 S4 的差异：S8 快照是**事件流**（`events` 列表），没有 code 索引，
      要靠事件的 `related_codes` 反查本票。实测 10-22：77 个事件、带 related_codes。

    ★ 只留命中本票的事件（最多 `_S8_MAX_EVENTS` 条，按公告日降序）：
      把 77 条全塞进 brief 会淹没判断（单票决策关心的是「这只票有没有政策驱动」，
      不是全市场政策清单）。条数上限是**防淹没**，不是掩盖——超出部分报在
      `n_matched` 里，可回查快照。
    """
    rec = LD.load_s_output("s8", as_of, base=base)
    if rec is LD.UNAVAILABLE:
        return None
    doc = dict(rec)
    evs = doc.get("events") or []
    mine = []
    if code:
        for e in evs:
            if not isinstance(e, dict):
                continue
            rcs = e.get("related_codes") or []
            if code in rcs:
                mine.append({"news_id": e.get("news_id"),
                             "ann_date": e.get("ann_date"),
                             "title": (e.get("title") or "")[:120],
                             "domain": e.get("domain"),
                             "level_hints": e.get("level_hints")})
    mine.sort(key=lambda x: (x.get("ann_date") or "", x.get("news_id") or ""),
              reverse=True)
    return {"as_of": doc.get("as_of"),
            "this_code": mine[:_S8_MAX_EVENTS],
            "n_matched": len(mine),
            "n_truncated": max(0, len(mine) - _S8_MAX_EVENTS),
            "in_universe": bool(mine),
            # transmission 段常为 UNAVAILABLE（产业链图谱无源）——如实透传
            "transmission": doc.get("transmission"),
            "market": {"n_policy_events": doc.get("n_policy_events"),
                       "scanned_news": doc.get("scanned_news")}}


def _load_s5_room(as_of, code=None, base=None):
    """S5 资金筹码结构（**本票视角**）——2026-10-23 接入。

    ★ S5 快照 `codes` 是 code → {behavior, behavior_reason{gates...}, ...}，
      天生带个股维度（实测 09-29 有 17 只结构化票、behavior_counts 全市场分布）。
      与 S4 同构，同样**只取本票**，只留一个全市场分布作背景。

    ★ `behavior=unknown` 是**合法结论**（源覆盖不足），不是缺数据 ——
      如实透传，由判断层自己决定要不要采信（覆盖 16% 时「unknown 占多数」是事实）。
    """
    rec = LD.load_s_output("s5", as_of, base=base)
    if rec is LD.UNAVAILABLE:
        return None
    doc = dict(rec)
    codes = doc.get("codes") or {}
    mine = None
    if code and isinstance(codes, dict):
        hit = codes.get(code)
        if isinstance(hit, dict):
            gates = ((hit.get("behavior_reason") or {}).get("gates")) or {}
            mine = {"code": code,
                    "behavior": hit.get("behavior"),
                    # 只带**命中**的闸（ok=True），未命中闸全塞进去会淹没有效证据
                    "gates_hit": {k: v for k, v in gates.items()
                                  if isinstance(v, list)
                                  and any(g.get("ok") for g in v
                                          if isinstance(g, dict))},
                    "priority": (hit.get("behavior_reason") or {}).get("priority")}
    return {"as_of": doc.get("as_of"),
            "this_code": mine,
            "in_universe": bool(mine),
            "market": {"behavior_counts": doc.get("behavior_counts"),
                       "n_codes": doc.get("n_codes"),
                       "coverage_ratio": (doc.get("universe") or {}).get("coverage_ratio")}}


_S2_CONST = os.path.join(_OLD_PKG_ROOT, "data", "raw", "meta",
                         "tdx_sector_constituent.csv")


def _s2_code_norm(c):
    """`000008.SZ` → `sz000008`（与本项目 codes 口径统一）。认不出 → None 不猜。"""
    s = str(c or "").strip().upper()
    if "." not in s:
        return None
    num, mkt = s.split(".", 1)
    mkt = mkt.strip()
    if mkt not in ("SH", "SZ", "BJ"):
        return None
    return mkt.lower() + num.strip()


def _s2_members_by_code():
    """→ {本项目code: [(theme_id, theme_name), ...]}。读不到 → {}（不猜、不炸）。

    用途：S2 快照只有**板块维度**（4 个题材），没有成分股表 ⇒ 单票决策拿不到。
    成分股映射在旧包 `tdx_sector_constituent.csv`（实测 269 板块 / 46832 成员）。
    这是 S2 唯一能把「题材阶段」落到「这只票」的桥。
    """
    if not os.path.isfile(_S2_CONST):
        return {}
    out = {}
    with io.open(_S2_CONST, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            code = _s2_code_norm(r.get("code"))
            tid = (r.get("sector_code") or "").strip()
            if not code or not tid:
                continue
            out.setdefault(code, []).append((tid, (r.get("sector_name") or "").strip()))
    return out


def _load_s2_room(as_of, code=None, base=None):
    """S2 题材生命周期（**本票所属题材的阶段**）——2026-10-23 接入。

    ★ 这是 S 系里最绕的一个：快照只给板块（`themes`: 4 个），不给成分股。
      桥 = 旧包 `tdx_sector_constituent.csv`（板块→成员），把本票能挂上的题材
      逐个取出阶段/DayN/族。
    ★ 一只票可能属于**很多**题材（实测一带一路 784 只成员），全塞进去会淹没判断
      ⇒ 只取快照 `themes` 里**确实被算出阶段**的那几个（通常 4 个以内），
      并限制在 `_S2_MAX_THEMES`。命不中任何题材 → `themes=[]` 且 `in_universe=False`
      （「这只票不属于当前在途题材」本身就是有效信息）。
    """
    rec = LD.load_s_output("s2", as_of, base=base)
    if rec is LD.UNAVAILABLE:
        return None
    doc = dict(rec)
    themes = doc.get("themes") or {}
    if not isinstance(themes, dict) or not code:
        return {"as_of": doc.get("as_of"), "themes": [], "in_universe": False,
                "n_themes_total": len(themes) if isinstance(themes, dict) else 0}

    mine_ids = set(tid for tid, _n in (_s2_members_by_code().get(code) or []))
    hit = []
    for tid, t in themes.items():
        if tid not in mine_ids or not isinstance(t, dict):
            continue
        hit.append({"theme_id": tid, "theme_name": t.get("theme_name"),
                    "family": t.get("family"), "stage": t.get("stage"),
                    "day_n": t.get("day_n"), "day_n_anchor": t.get("day_n_anchor"),
                    "stage_reason": (t.get("stage_reason") or [])[:3],
                    "confidence": t.get("confidence")})
    # 阶段越靠前越有信息量（发酵>扩散>加速>分歧>尾声），同阶段按 DayN 升序
    _ORD = {"发酵": 0, "扩散": 1, "加速": 2, "分歧": 3, "尾声": 4}
    hit.sort(key=lambda x: (_ORD.get(x.get("stage"), 9), x.get("day_n") or 999,
                            x.get("theme_id") or ""))
    return {"as_of": doc.get("as_of"),
            "themes": hit[:_S2_MAX_THEMES],
            "n_matched": len(hit),
            "n_truncated": max(0, len(hit) - _S2_MAX_THEMES),
            "in_universe": bool(hit),
            "market": {"n_themes_total": len(themes),
                       "n_unresolved": doc.get("n_unresolved")}}


def _load_s6_room(as_of, code=None, base=None):
    """S6 估值定价（**本票 PE/PB/股息/市值**）——2026-10-23 接入。

    ★ 数据在 `stock_section`（code → {pe_ratio, pe_fwd, pb_ratio,
      dividend_ratio_ttm, total_market_cap, ...}），实测 10-19 有 5 只个股 + 129 只
      板块。**个股只有 5 只**（S6 只算候选池的票，不是全市场）⇒ 命中率天然低，
      命不中如实 `in_universe=False`。

    ★ `semantics` 必带：快照是**当前截面**（非 120 日序列），估值「贵不贵」只能
      横向比，不能看自身历史分位。这个限制必须让判断层看到，否则会误当序列用。
    """
    rec = LD.load_s_output("s6_section", as_of, base=base)
    if rec is LD.UNAVAILABLE:
        rec = LD.load_s_output("s6", as_of, base=base)   # 兼容两种文件名
    if rec is LD.UNAVAILABLE:
        return None
    doc = dict(rec)
    ss = doc.get("stock_section") or {}
    mine = None
    if code and isinstance(ss, dict) and isinstance(ss.get(code), dict):
        v = ss[code]
        mine = {"code": code, "name": v.get("name"),
                "quote_date": v.get("quote_date"),
                "pe_ttm": v.get("pe_ratio"), "pe_fwd": v.get("pe_fwd"),
                "pb": v.get("pb_ratio"),
                "dividend_ttm": v.get("dividend_ratio_ttm"),
                "mkt_cap_yi": v.get("total_market_cap")}
    return {"as_of": doc.get("as_of"),
            "this_code": mine,
            "in_universe": bool(mine),
            # 截面非序列：显式透传，禁让判断层当历史分位用
            "semantics": doc.get("semantics"),
            "market": {"n_codes": doc.get("n_codes"),
                       "n_stock_section": len(ss) if isinstance(ss, dict) else 0}}


def _load_s3_room(as_of, s3_path=None, base=None):
    """S3 快照（当日结构）+ 知识库（历史实证分组）。两者缺任一 → 各自字段为 None。"""
    snap = None
    if s3_path:
        if os.path.isfile(s3_path):
            with io.open(s3_path, encoding="utf-8-sig") as f:
                snap = json.load(f)
    else:
        rec = LD.load_s_output("s3", as_of, base=base)
        if rec is not LD.UNAVAILABLE:
            snap = dict(rec)
    kb = LD.load_knowledge("s3", as_of, base=base)
    kb_info = None
    if kb is not LD.UNAVAILABLE:
        kb_info = {"kb_version": kb.get("version"), "kb_as_of": kb.get("as_of"),
                   "n_days_total": kb.get("n_days_total"),
                   "n_unresolved": kb.get("n_unresolved")}
    if snap is None and kb_info is None:
        return None
    out = {"snapshot": None, "kb": kb_info}
    if snap is not None:
        out["snapshot"] = {"as_of": snap.get("as_of"), "stage": snap.get("stage"),
                           "temp": snap.get("temp"), "leader": snap.get("leader"),
                           "mid": snap.get("mid"), "catchup": snap.get("catchup"),
                           "confidence": snap.get("confidence")}
    return out


# ── 装配 ─────────────────────────────────────────────────────────────────────
# ── 环境投影（C1 输出形态 → 账本 env 契约） ──────────────────────────────────
# ★ 真实契约差异（本件必须吸收的一处形态不一致）：
#   C1 `regime.evaluate()` 输出 {regime, position_cap, signals, as_of, source}，
#   而 0.3 账本 `_norm_env()` 只收 {regime, signals, leg, source}——多 position_cap 即拒。
#   brief 对 LLM 呈现**完整 C1 输出**（判断要看仓位天花板），落账只用契约四字段。
#   source 亦需映射：C1 自报来源（SNAPSHOT / LIVE…）→ 账本三值域（LOADER/EXPLICIT/UNAVAILABLE）。
_SRC_TO_LEDGER = {"SNAPSHOT": "LOADER", "LOADER": "LOADER", "LIVE": "EXPLICIT",
                  "INTRADAY": "EXPLICIT", "EXPLICIT": "EXPLICIT",
                  "UNAVAILABLE": "UNAVAILABLE"}


def env_ledger(env):
    """C1 env → 账本契约 env（四字段 + 枚举映射）。UNAVAILABLE 时 regime/signals 必空。"""
    src = _SRC_TO_LEDGER.get(env.get("source"), "EXPLICIT")
    if src == "UNAVAILABLE" or env.get("regime") is None:
        return {"regime": None, "signals": {}, "leg": env.get("leg") or "EOD",
                "source": "UNAVAILABLE"}
    return {"regime": env["regime"], "signals": env.get("signals") or {},
            "leg": env.get("leg") or "EOD", "source": src}


def _unwrap_chain(obj):
    """C3 链载荷解包 → (4 键链, 信封 | None)。

    `drive.review` 落盘行是**信封**：
      {schema_version, chain_id, as_of, code, thesis_id, driver_proposed,
       driver_effective, driver_reason, chain:{evidence,inference,prediction,five},
       evidence_meta, prediction_slots, session, data_fingerprint, [context]}
    而决策层 `decision._norm_chain` 只接受**恰 4 键**（evidence/inference/prediction/five）。

    ★ 2026-10-19 修复：原 `assemble` 直接 `chain = row`（信封），下游 `_norm_chain`
      必拒（实测「多=['as_of','chain_id','code','thesis_id',…]」）——即**任何真实
      proposal 经 review 落链后都无法进决策**，属读取侧未解包的结构性断链。
      此处解包，信封另存供 chain_id / thesis_id 取值；已是 4 键的调用原样通过。
    """
    if not isinstance(obj, dict):
        return None, None
    inner = obj.get("chain")
    if isinstance(inner, dict):
        return dict(inner), dict(obj)
    return dict(obj), None


def assemble(as_of, code, *, thesis_id=None, chain=None, thesis_state=None,
             brain_path=None, params_path=None, s1_path=None, s3_path=None,
             pool_path=None, thesis_path=None, chains_path=None, base=None):
    """真源 → brief。**确定性**（同输入同 brief，零 wall-clock / 零随机量）。

    自动解析：thesis_id ← 参数 > 当日 C3 链 > Thesis 池（code 匹配）；
    chain ← 参数 > `drive.load_chain(code, as_of)`。解析不到 → missing 记缺口，照常产 brief
    （LLM 看得到缺什么 = 证据不足时应给 WAIT/NO 而不是编）。
    """
    d = DEC._check_date(as_of)
    c = DEC._check_code(code)

    env = read_env(brain_path, base=base)
    missing = []

    # C3 链：**只认 as_of 当日**（旧链不冒充当日事实）
    chain_row = None
    if chain is None:
        row = DRV.load_chain(code=c, as_of=d, path=chains_path)
        if row is not None:
            chain, chain_row = _unwrap_chain(row)
    else:
        chain, chain_row = _unwrap_chain(chain)
    ch_id = (chain_row or {}).get("chain_id")
    if chain is None:
        missing.append("C3:CHAIN")

    # thesis_id：显式 > 链自报 > Thesis 池
    #   ★ 链自报须取**信封**的 thesis_id：`drive.review` 落盘行是信封
    #     `{chain_id, as_of, code, thesis_id, chain:{evidence,inference,prediction,five}}`，
    #     真链在 `row["chain"]`；解包后 4 键链里没有 thesis_id。
    th_id = (thesis_id or (chain_row or {}).get("thesis_id")
             or (chain or {}).get("thesis_id"))
    if th_id is None:
        for it in TH.list_thesis(code=c, path=thesis_path) or []:
            th_id = it.get("id")
    if th_id is None:
        missing.append("THESIS_ID")

    # C2 候选
    cands = CAND.list_candidates(code=c, path=pool_path)
    candidate = None
    if cands:
        candidate = {"id": cands[-1].get("id"), "state": cands[-1].get("state"),
                     "created_at": cands[-1].get("created_at"),
                     "expiry": cands[-1].get("expiry")}
    else:
        missing.append("C2:CANDIDATE")

    if env.get("source") == "UNAVAILABLE":
        missing.append("C1:ENV")

    s1_room = _load_s1_room(d, base=base)
    if s1_room is None:
        missing.append("S1:EFFECT")
    s3_room = _load_s3_room(d, s3_path=s3_path, base=base)
    if s3_room is None:
        missing.append("S3:EMOTION")
    # S4 拥挤度：不在缺口中计（它是**增强证据**，不是决策前提；
    #   S1/S3 缺了 brief 就不完整，S4 缺了只是少一个判据，不该拦 LLM 调用）
    s4_room = _load_s4_room(d, code=c, base=base)
    # S8 政策事件：同样只在缺口中不计（增强证据，非决策前提）
    s8_room = _load_s8_room(d, code=c, base=base)
    # S5 资金筹码：同上，增强证据不计缺口
    s5_room = _load_s5_room(d, code=c, base=base)
    # S2 题材阶段：同上，增强证据不计缺口
    s2_room = _load_s2_room(d, code=c, base=base)
    # S6 估值：同上，增强证据不计缺口
    s6_room = _load_s6_room(d, code=c, base=base)

    pver = ORCH._params_version(params_path)
    e_led = env_ledger(env)
    try:
        h = ORCH.info_set_hash(d, c, thesis_id=th_id or "TH-ABSENT", chain=chain or {},
                               thesis_state=thesis_state, env=e_led,
                               params_path=params_path)
    except Exception:                                    # 链非法时不阻断装配（如实标）
        h = "INFO_SET_UNRESOLVED"

    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": d, "code": c, "thesis_id": th_id,
        "params_version": pver,
        "env": env,
        "env_ledger": e_led,
        "candidate": candidate,
        "chain": chain,
        "chain_id": ch_id,          # ← 信封 chain_id（可追溯；4 键链内不许带，见 _norm_chain）
        "evidence_room": {"env": env, "candidate": candidate, "chain": chain,
                          "s1_effects": s1_room, "s3_emotion": s3_room,
                          "s4_crowding": s4_room, "s8_policy": s8_room,
                          "s5_capital": s5_room, "s2_theme": s2_room,
                          "s6_valuation": s6_room},
        "missing": missing,
        "info_set_hash": h,
    }


# ── 校验闸 ───────────────────────────────────────────────────────────────────
def _check_event(ev, name, problems):
    """事件 = 0.5 同形 `{text,rule}`；空话一律拒（零模糊输出的落点）。"""
    if ev is None:
        return
    if not isinstance(ev, dict):
        problems.append("%s 必须是 {text,rule} 对象" % name)
        return
    if set(ev) - set(COND_KEYS):
        problems.append("%s 含非契约键 %s（条件结构复用 0.5，禁第二套语言）"
                        % (name, sorted(set(ev) - set(COND_KEYS))))
    txt = ev.get("text")
    if not isinstance(txt, str) or not txt.strip():
        problems.append("%s.text 必填非空" % name)
    else:
        for w in VAGUE_WORDS:
            if w in txt:
                problems.append("%s.text 含空话「%s」——零模糊输出不达标" % (name, w))
    rule = ev.get("rule")
    if rule is not None:
        if not isinstance(rule, dict) or set(rule) != {"field", "op", "value"}:
            problems.append("%s.rule 必须是 {field,op,value}" % name)
        elif rule.get("op") not in COND_OPS:
            problems.append("%s.rule.op 必须是 %s" % (name, " / ".join(COND_OPS)))


def review_proposal(proposal, brief):
    """LLM proposal × brief → {ok, problems, answer_kwargs}。**不判动作、不猜意图**。

    校验三层：①结构（字段/类型/值域）②恒等（as_of/code/thesis_id 必须与 brief 一致
    ——防串号，把 A 票的判断记到 B 票头上）③证据引用（basis 必须落在 brief.evidence_room）。
    动作裁决**不在此**（three_q 机械分流），本闸只保证「喂给机械裁决的输入合法」。
    """
    problems = []
    if not isinstance(proposal, dict):
        return {"ok": False, "problems": ["proposal 必须是 dict"], "answer_kwargs": None}

    extra = set(proposal) - set(PROPOSAL_KEYS)
    if extra:
        problems.append("proposal 含契约外键 %s" % sorted(extra))
    for k in ("schema_version", "as_of", "code", "thesis_id", "driver", "basis"):
        if k not in proposal:
            problems.append("proposal 缺必填键 %s" % k)
    if problems:
        return {"ok": False, "problems": problems, "answer_kwargs": None}

    # ② 恒等（防串号）
    if brief.get("thesis_id") is None:
        problems.append("brief 缺 thesis_id（无决策主体）——先补 C3 链 / Thesis 再决策")
    for k in ("as_of", "code", "thesis_id"):
        if brief.get(k) is not None and proposal.get(k) != brief.get(k):
            problems.append("%s 不一致：proposal=%r brief=%r（串号拒收）"
                            % (k, proposal.get(k), brief.get(k)))

    # ① 值域与类型
    if proposal["driver"] not in DRIVERS:
        problems.append("driver 必须是 %s，实际 %r" % (" / ".join(DRIVERS),
                                                       proposal["driver"]))
    for k in ("entry_limit", "stop_line", "target"):
        v = proposal.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))
                              or v <= 0):
            problems.append("%s 必须是正数或 null，实际 %r" % (k, v))
    w = proposal.get("window")
    if w is not None and (isinstance(w, bool) or not isinstance(w, int) or w < 1):
        problems.append("window 必须是 >=1 的整数或 null，实际 %r" % (w,))
    pr = proposal.get("position_request")
    if pr is not None and (isinstance(pr, bool) or not isinstance(pr, (int, float))
                           or pr < 0 or pr > 1):
        problems.append("position_request 必须在 [0,1] 或 null，实际 %r" % (pr,))
    ap = proposal.get("action_proposed")
    if ap is not None and ap not in ACTIONS:
        problems.append("action_proposed 必须是 %s 或 null（审计对照量，不参与裁决）"
                        % " / ".join(ACTIONS))
    cf = proposal.get("confidence")
    if cf is not None and (isinstance(cf, bool) or not isinstance(cf, (int, float))
                           or not (0 <= cf <= 1)):
        problems.append("confidence 必须在 [0,1] 或 null，实际 %r" % (cf,))
    _check_event(proposal.get("completion_event"), "completion_event", problems)
    _check_event(proposal.get("overturn_event"), "overturn_event", problems)

    # ③ 证据引用（防编造）
    room = brief.get("evidence_room") or {}
    basis = proposal.get("basis")
    if not isinstance(basis, list) or not basis:
        problems.append("basis 必须是非空列表（无证据引用 = 无依据的判断）")
    else:
        for i, b in enumerate(basis):
            if not isinstance(b, dict) or not b.get("ref"):
                problems.append("basis[%d] 必须是 {ref,claim}" % i)
                continue
            ref = b["ref"]
            ns = next((k for k in REF_NS if ref == k or ref.startswith(k + ":")), None)
            if ns is None:
                problems.append("basis[%d].ref=%r 不在引用命名空间 %s"
                                % (i, ref, sorted(REF_NS)))
            elif room.get(REF_NS[ns]) is None:
                problems.append("basis[%d].ref=%r 引用缺席来源（brief 该来源为 None）"
                                % (i, ref))

    if problems:
        return {"ok": False, "problems": problems, "answer_kwargs": None}

    kw = {k: proposal.get(k) for k in ANSWER_KEYS}
    return {"ok": True, "problems": [], "answer_kwargs": kw}


# ── 收口：proposal → 决策落账（失败安全在此显形） ──────────────────────────────
def brief_ready(brief):
    """决策前提是否齐（身份 + 驱动链）。缺 → **不调用 LLM、不落账**，返回缺口。

    与 LLM 失败区分开：这是「输入装配缺口」（要补数据/链），不是「LLM 没答」。
    无 Thesis = 无决策主体；无驱动链 = 无判断素材——两者都不构成一次可失败的判断。
    """
    miss = [k for k in PREREQ_KEYS if brief.get(k) is None]
    return (not miss), miss


def classify(proposal, brief):
    """proposal × brief → 失败分类（不落账的 4 类 + 通过）。**纯函数，零副作用**。

    顺序：先查决策前提（无前提则谈不上调用 LLM），再查 LLM 产出，最后查输出与证据。
    """
    ready, miss = brief_ready(brief)
    if not ready:
        return ST_BRIEF_INCOMPLETE, {"reason": "brief 缺决策前提（无决策主体/无判断素材）"
                                               "——不调用 LLM、不落账", "missing": miss}
    if proposal is None:
        return ST_LLM_ABSENT, {"reason": "LLM 未产出 proposal（端点不可用/超时/未返回）"
                                        "——不落账，当日该 code 无决策"}
    r = review_proposal(proposal, brief)
    if not r["ok"]:
        ev_only = all(("basis" in p or "引用" in p) for p in r["problems"])
        st = ST_EVIDENCE if ev_only else ST_MALFORMED
        return st, {"reason": "proposal 拒收", "problems": r["problems"]}
    return ST_OK, {"answer_kwargs": r["answer_kwargs"]}


def decide_from_proposal(proposal, brief, *, sim, chain=None, thesis_state=None,
                         env=None, pool_path=None, thesis_path=None, params_path=None,
                         snapshot_root=None, base=None):
    """proposal → 校验 → orchestrate.decide。失败安全：前 3 类**不落账**，只返回状态。

    返回 {status, ...}：失败三态原样透出；通过则 orchestrate 的
    OK / REUSED / HALF_COMMIT / REUSED_LEDGER。
    """
    st, info = classify(proposal, brief)
    if st != ST_OK:
        return {"status": st, "decision_id": None, "action": None, "detail": info}
    kw = info["answer_kwargs"]
    rep = ORCH.decide(as_of=brief["as_of"], code=brief["code"],
                      thesis_id=brief["thesis_id"],
                      chain=chain if chain is not None else brief.get("chain"),
                      sim=sim, thesis_state=thesis_state,
                      env=env if env is not None else brief.get("env_ledger", {}),
                      pool_path=pool_path, thesis_path=thesis_path,
                      params_path=params_path, snapshot_root=snapshot_root,
                      base=base, **kw)
    return rep


# ── 自检（全沙盒） ───────────────────────────────────────────────────────────
def _tree_fp(root):
    """目录树指纹（相对路径 + 内容；缺失目录记 ABSENT）。真库守卫用。"""
    if not os.path.isdir(root):
        return "ABSENT"
    h = hashlib.md5()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root).replace("\\", "/")
            h.update(rel.encode("utf-8"))
            with io.open(p, "rb") as f:
                h.update(f.read())
    return h.hexdigest()


def _selftest():
    import shutil
    import tempfile

    bad = [0]
    # ★ 真库守卫（承接本模块首版自检的真实教训：write 型接缝漏传 base= 会把测试行
    #   打進真实账本——首版实测污染 data/generated/sim/ 1 行，已清除）。守卫 = 铁律。
    _guard_dirs = [os.path.join(DEC.ROOT, "data"), os.path.join(DEC.ROOT, "memory")]
    _before = {d: _tree_fp(d) for d in _guard_dirs}

    def T(name, cond, extra=""):
        print(("  [ok] " if cond else "  [!!] ") + name + ("" if cond else " | %s" % extra))
        if not cond:
            bad[0] += 1

    print("== A. 零重定义 ==")
    T("A1 DRIVERS is DEC.DRIVERS", DRIVERS is DEC.DRIVERS)
    T("A2 ACTIONS is DEC.ACTIONS", ACTIONS is DEC.ACTIONS)
    T("A3 COND_KEYS is TQ.COND_KEYS", COND_KEYS is TQ.COND_KEYS)
    T("A4 VAGUE_WORDS is TQ.VAGUE_WORDS", VAGUE_WORDS is TQ.VAGUE_WORDS)
    _ans_kw = next(kw for nm, _pos, kw in TQ.THREE_Q_API if nm == "answer")
    T("A5 ANSWER_KEYS ⊆ answer() 关键字参数", set(ANSWER_KEYS) <= set(_ans_kw),
      sorted(set(ANSWER_KEYS) - set(_ans_kw)))

    print("== B. 环境档读回 ==")
    tmp = tempfile.mkdtemp(prefix="ta_brief_")
    try:
        brain = os.path.join(tmp, "brain_state.md")
        with io.open(brain, "w", encoding="utf-8", newline="\n") as f:
            f.write("# 当前状态\n\n## 环境档（← C1 产出）\n\n| 项 | 值 |\n| --- | --- |\n"
                    "| 档位 | **强势** |\n| 仓位天花板（成） | 5 |\n"
                    "| as_of / source | 2026-09-29 / SNAPSHOT |\n| 涨停家数 | 60 |\n"
                    "| 连板高度 | 4 |\n| 炸板率 | 20.0% |\n"
                    "| 成交额（只记录） | 1.20 万亿 |\n| 涨跌家数比（只记录） | 1.50 |\n"
                    "| 指数20日线（只记录） | 3200.0 |\n\n## 下一段\n")
        e = read_env(brain)
        T("B1 档位解析", e["regime"] == "强势", e)
        T("B2 仓位天花板", e["position_cap"] == 5.0, e)
        T("B3 三信号（涨停/高度/炸板率）",
          e["signals"].get("limit_up_count") == 60 and e["signals"].get("max_board") == 4
          and abs(e["signals"].get("fail_rate", 0) - 0.20) < 1e-9, e["signals"])
        T("B4 只记录三信号（turnover/up_down/index_ma20）",
          abs(e["signals"].get("turnover", 0) - 1.2e12) < 1
          and e["signals"].get("up_down_ratio") == 1.5
          and e["signals"].get("index_ma20") == 3200.0, e["signals"])
        T("B5 source 解析", e["source"] == "SNAPSHOT", e)
        # T3 收尾：环境档自身时点（源行 `as_of / source` 左半）——纯读回增强
        T("B5b 环境档 as_of 读出", e["as_of"] == "2026-09-29", e)
        T("B5c as_of 不进账本契约（env_ledger 恰四字段）",
          set(env_ledger(e)) == {"regime", "signals", "leg", "source"}, env_ledger(e))
        # UNAVAILABLE 表 → 不编档
        b2 = os.path.join(tmp, "brain_na.md")
        with io.open(b2, "w", encoding="utf-8", newline="\n") as f:
            f.write("# x\n\n## 环境档（← C1 产出）\n\n| 项 | 值 |\n| --- | --- |\n"
                    "| 档位 | `UNAVAILABLE`（取数报缺，禁编档） |\n"
                    "| 仓位天花板 | `UNAVAILABLE` |\n| 关键信号 | `UNAVAILABLE` |\n\n## n\n")
        e2 = read_env(b2)
        T("B6 UNAVAILABLE 如实报缺（零编档）",
          e2["regime"] is None and e2["source"] == "UNAVAILABLE"
          and not e2["signals"], e2)
        T("B6b UNAVAILABLE 时 as_of 亦为 None（缺就是缺）", e2["as_of"] is None, e2)
        T("B7 文件缺失 → 报缺", read_env(os.path.join(tmp, "nope.md"))["source"]
          == "UNAVAILABLE")
        T("B7b 文件缺失 → as_of 亦为 None",
          read_env(os.path.join(tmp, "nope.md"))["as_of"] is None)
        # 有档位但缺 `as_of / source` 行 → as_of 不猜
        b3 = os.path.join(tmp, "brain_norow.md")
        with io.open(b3, "w", encoding="utf-8", newline="\n") as f:
            f.write("# x\n\n## 环境档（← C1 产出）\n\n| 项 | 值 |\n| --- | --- |\n"
                    "| 档位 | **强势** |\n| 涨停家数 | 60 |\n\n## n\n")
        e3 = read_env(b3)
        T("B7c 缺 as_of/source 行 → as_of=None 且 source=UNKNOWN（不猜）",
          e3["as_of"] is None and e3["source"] == "UNKNOWN", e3)
        # 左半非法日期 → as_of 不猜（source 仍按既有语义取末段）
        b4 = os.path.join(tmp, "brain_badasof.md")
        with io.open(b4, "w", encoding="utf-8", newline="\n") as f:
            f.write("# x\n\n## 环境档（← C1 产出）\n\n| 项 | 值 |\n| --- | --- |\n"
                    "| 档位 | **强势** |\n| as_of / source | 昨日盘后 / snapshot |\n\n## n\n")
        e4 = read_env(b4)
        T("B7d 左半非 YYYY-MM-DD → as_of=None（source 仍解析）",
          e4["as_of"] is None and e4["source"] == "snapshot", e4)

        print("== C. 装配（真源 + 沙盒） ==")
        # ★ 候选池用**沙盒空池**（与生产同构）：自检不得依赖生产 data/state 的 schema 版本，
        #   否则 A4 口径升级（candidates SCHEMA_VERSION 1→2）会让本自检随生产库状态而红。
        pool_f = os.path.join(tmp, "candidates.json")
        with io.open(pool_f, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"version": CAND.SCHEMA_VERSION, "updated_at": "2026-09-29",
                       "candidates": []}, f, ensure_ascii=False, indent=2)
        b = assemble("2026-09-29", "sh600000", brain_path=brain, pool_path=pool_f)
        T("C1 as_of/code 规范化", b["as_of"] == "2026-09-29" and b["code"] == "sh600000")
        T("C2 evidence_room 十来源键齐（2026-10-23 加 s2/s4/s5/s6/s8）",
          set(b["evidence_room"]) == {"env", "candidate", "chain", "s1_effects",
                                      "s3_emotion", "s4_crowding", "s8_policy",
                                      "s5_capital", "s2_theme", "s6_valuation"})
        _GAP_NAMES = set(REF_NS) | {"THESIS_ID", "PARAMS_VERSION"}
        T("C3 缺什么记什么（missing 全为已知缺口名）",
          bool(b["missing"]) and all(x in _GAP_NAMES for x in b["missing"]),
          b["missing"])
        T("C3b 真源在场 → S1/S3 不入 missing（真数据消费实证）",
          "S1:EFFECT" not in b["missing"] and "S3:EMOTION" not in b["missing"],
          b["missing"])

        # ── C6/C7 S4·S8 装配（2026-10-23 接入 S 系消费） ──
        # 上面 b 用真库（S4/S8 可能真有数据）；隔离性必须单独验：
        # 用**空沙盒 base** 装配 → S4/S8 必须 None（读不到快照），且不污染 missing。
        empty_base = os.path.join(tmp, "empty_base")
        os.makedirs(os.path.join(empty_base, "data", "generated"), exist_ok=True)
        b_iso = assemble("2026-09-29", "sh600000", brain_path=brain,
                         pool_path=pool_f, base=empty_base)
        T("C6 空沙盒 → S4/S8 房间为 None（缺快照不炸、不假装有数据）",
          b_iso["evidence_room"].get("s4_crowding") is None
          and b_iso["evidence_room"].get("s8_policy") is None
          and b_iso["evidence_room"].get("s5_capital") is None
          and b_iso["evidence_room"].get("s2_theme") is None
          and b_iso["evidence_room"].get("s6_valuation") is None,
          "s4=%r s8=%r" % (b_iso["evidence_room"].get("s4_crowding"),
                           b_iso["evidence_room"].get("s8_policy")))
        T("C6b S4/S8 缺席不进 missing（增强证据 ≠ 决策前提）",
          not [x for x in b_iso["missing"] if x.startswith(("S4", "S8"))],
          b_iso["missing"])
        T("C7 S4·S8 命名空间已登记（LLM 可引用）",
          REF_NS.get("S4:CROWDING") == "s4_crowding"
          and REF_NS.get("S8:POLICY") == "s8_policy")
        T("C7b S4 只带本票、不夹带别票（防串号）",
          (b["evidence_room"].get("s4_crowding") or {}).get("this_code") is None
          or (b["evidence_room"]["s4_crowding"]["this_code"] or {}).get("code")
          == "sh600000",
          str((b["evidence_room"].get("s4_crowding") or {}).get("this_code"))[:80])
        T("C4 确定性：两次装配逐字节同",
          json.dumps(assemble("2026-09-29", "sh600000", brain_path=brain,
                              pool_path=pool_f),
                     ensure_ascii=False, sort_keys=True)
          == json.dumps(b, ensure_ascii=False, sort_keys=True))
        T("C5 零写点：装配不改 brain 文件",
          io.open(brain, encoding="utf-8").read().count("## 环境档") == 1)

        print("== D. 校验闸 ==")
        # 校验闸只校「输出」，身份由 brief 给——补一个齐身份 brief 测（真实装配无 thesis）
        b_d = dict(b, thesis_id="TH-TEST")
        base_prop = {"schema_version": 1, "as_of": b["as_of"], "code": b["code"],
                     "thesis_id": "TH-TEST", "driver": "C",
                     "entry_limit": 10.0, "stop_line": 9.0, "target": 12.0,
                     "window": 3, "position_request": 0.2,
                     "completion_event": None, "overturn_event": None,
                     "action_proposed": "BUY",
                     "basis": [{"ref": "S3:EMOTION", "claim": "情绪发酵（真数据）"}],
                     "missing": [], "confidence": 0.6}
        r = review_proposal(base_prop, b_d)
        T("D1 合规 proposal 通过（引用真 S3 证据）", r["ok"], r["problems"])
        T("D2 answer_kwargs 恰为 ANSWER_KEYS", set(r["answer_kwargs"]) == set(ANSWER_KEYS))
        T("D3 driver 值域拒", not review_proposal(dict(base_prop, driver="X"), b_d)["ok"])
        T("D4 串号拒（as_of 不一致）",
          any("串号" in x for x in
              review_proposal(dict(base_prop, as_of="2026-01-01"), b_d)["problems"]))
        T("D5 空 basis 拒（无依据的判断）",
          any("basis" in x for x in review_proposal(dict(base_prop, basis=[]), b_d)["problems"]))
        T("D6 引用缺席来源拒（C3:CHAIN 在真实 brief 为 None）",
          any("缺席" in x for x in review_proposal(
              dict(base_prop, basis=[{"ref": "C3:CHAIN", "claim": "链"}]), b_d)["problems"]))
        T("D7 命名空间外拒（编造证据）",
          any("命名空间" in x for x in review_proposal(
              dict(base_prop, basis=[{"ref": "X9:FAKE", "claim": "编的"}]), b_d)["problems"]))
        T("D8 空话拒（零模糊输出）",
          any("空话" in x for x in review_proposal(
              dict(base_prop, completion_event={"text": "再看看情况", "rule": None}),
              b_d)["problems"]))
        T("D9 合规事件通过", review_proposal(
            dict(base_prop, completion_event={"text": "放量站回10元",
                                              "rule": {"field": "close", "op": ">",
                                                       "value": 10}}), b_d)["ok"])
        T("D10 window<1 拒", not review_proposal(dict(base_prop, window=0), b_d)["ok"])
        T("D11 position_request>1 拒",
          not review_proposal(dict(base_prop, position_request=1.5), b_d)["ok"])
        T("D12 契约外键拒", any("契约外键" in x for x in
                                review_proposal(dict(base_prop, extra_key=1), b_d)["problems"]))
        T("D13 brief 缺 thesis_id 时明确报「无决策主体」（非笼统串号）",
          any("无决策主体" in x for x in review_proposal(base_prop, b)["problems"]))

        print("== E. 失败安全分类 ==")
        _ready, _miss = brief_ready(b)
        T("E0 真实装配 brief 缺前提可机械识别",
          (not _ready) and set(_miss) == set(PREREQ_KEYS), _miss)
        T("E0b 前提缺 → BRIEF_INCOMPLETE（不落账）",
          classify(base_prop, b)[0] == ST_BRIEF_INCOMPLETE)
        b_full = dict(b, thesis_id="TH-TEST",
                      chain={"evidence": ["C1:ENV"], "inference": [], "prediction": [],
                             "five": {k: "有" for k in DEC.FIVE_KEYS}})
        T("E1 proposal 缺席 → LLM_UNAVAILABLE", classify(None, b_full)[0] == ST_LLM_ABSENT)
        T("E2 结构不合规 → PROPOSAL_MALFORMED",
          classify(dict(base_prop, driver="X"), b_full)[0] == ST_MALFORMED)
        T("E3 证据不可追溯 → EVIDENCE_UNRESOLVED",
          classify(dict(base_prop, basis=[{"ref": "X9:F", "claim": "c"}]), b_full)[0]
          == ST_EVIDENCE)
        T("E4 合规 → OK", classify(base_prop, b_full)[0] == ST_OK)
        T("E5 失败四态互斥且非 OK",
          len({ST_BRIEF_INCOMPLETE, ST_LLM_ABSENT, ST_MALFORMED, ST_EVIDENCE}) == 4
          and ST_OK not in (ST_BRIEF_INCOMPLETE, ST_LLM_ABSENT, ST_MALFORMED, ST_EVIDENCE))

        print("== F. 贯通（沙盒：proposal → decide → 账本） ==")
        proj = os.path.join(tmp, "proj")
        _seed_proj(proj)
        ps = dict(params_path=os.path.join(proj, "data", "params.json"),
                  pool_path=os.path.join(proj, "data", "state", "candidates.json"))
        TH.init_file(os.path.join(proj, "thesis.md"))
        th = TH.create_thesis("2026-09-29", "C", "测试",
                              code="sh600000", path=os.path.join(proj, "thesis.md"))
        th_id = th["id"]
        chain = {"evidence": ["C1:ENV 环境强势"], "inference": ["驱动成立"],
                 "prediction": [{"prediction_id": k, "metric": "5日超额",
                                 "data_source": "snapshot", "baseline": "0",
                                 "direction": "UP", "threshold": "3",
                                 "condition": "放量不破5日线", "window": 3}
                                for k in TQ.PRED_SLOTS],
                 "five": {k: "有" for k in DEC.FIVE_KEYS}}
        b2 = assemble("2026-09-29", "sh600000", thesis_id=th_id, chain=chain,
                      brain_path=os.path.join(proj, "brain_state.md"),
                      thesis_path=os.path.join(proj, "thesis.md"), **ps)
        prop = dict(base_prop, thesis_id=th_id,
                    basis=[{"ref": "C1:ENV", "claim": "环境强势"}])
        th_path = os.path.join(proj, "thesis.md")
        led_base = os.path.join(proj, "data", "generated")   # base = 账本根目录
        th_state = TH.read_thesis(th_id, th_path)
        rep = decide_from_proposal(prop, b2, sim=True, thesis_state=th_state,
                                   thesis_path=th_path, base=led_base,
                                   snapshot_root=os.path.join(proj, "snap"), **ps)
        T("F1 合规 proposal → 落账 OK", rep.get("status") == "OK", rep)
        T("F2 动作由机械分流（driver=C 且三件齐 → BUY）",
          rep.get("action") == "BUY", rep)
        rep2 = decide_from_proposal(None, b2, sim=True, base=led_base,
                                    thesis_path=th_path,
                                    snapshot_root=os.path.join(proj, "snap"), **ps)
        T("F3 LLM 缺席 → 不落账（状态透出，无 decision_id）",
          rep2["status"] == ST_LLM_ABSENT and rep2["decision_id"] is None, rep2)
        led = os.path.join(proj, "data", "generated", "sim", "decision_ledger.jsonl")
        n1 = len([x for x in io.open(led, encoding="utf-8") if x.strip()]) \
            if os.path.isfile(led) else 0
        T("F4 沙盒账本恰 1 行（缺席未追加）", n1 == 1, n1)
        T("F5 同 proposal 重放 → REUSED 不新增",
          decide_from_proposal(prop, b2, sim=True, thesis_state=th_state,
                               thesis_path=th_path, base=led_base,
                               snapshot_root=os.path.join(proj, "snap"),
                               **ps).get("status") == "REUSED")
        T("F6 重放后沙盒账本仍 1 行",
          len([x for x in io.open(led, encoding="utf-8") if x.strip()]) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        _purge_pycache()
    _after = {d: _tree_fp(d) for d in _guard_dirs}
    _drift = [os.path.relpath(d, DEC.ROOT) for d in _guard_dirs
              if _before[d] != _after[d]]
    T("G1 真库零污染（data/ 与 memory/ 前后指纹一致）", not _drift, _drift)
    print("\n自检：%s（%d 失败）" % ("全部通过 ✅" if not bad[0] else "有失败 ❌", bad[0]))
    return bad[0]


def _seed_proj(proj):
    """最小沙盒：params 真文件副本 + brain + 空 snap + **空候选池**（与真库同构）。复制真源，不造第二套。"""
    import shutil
    os.makedirs(os.path.join(proj, "data", "generated", "sim"), exist_ok=True)
    os.makedirs(os.path.join(proj, "data", "snapshot"), exist_ok=True)
    os.makedirs(os.path.join(proj, "data", "state"), exist_ok=True)
    shutil.copy(os.path.join(DEC.ROOT, "data", "params.json"),
                os.path.join(proj, "data", "params.json"))
    # ★ 候选池版本取 `candidates.SCHEMA_VERSION`（**不硬编码**）：A4 口径升级 1→2 后，
    #   硬编码的夹具会让本自检随生产库版本而红；此处与真库同构且版本同源。
    with io.open(os.path.join(proj, "data", "state", "candidates.json"), "w",
                 encoding="utf-8", newline="\n") as f:
        json.dump({"version": CAND.SCHEMA_VERSION, "updated_at": "2026-09-29",
                   "candidates": []}, f, ensure_ascii=False, indent=2)
    with io.open(os.path.join(proj, "brain_state.md"), "w", encoding="utf-8",
                 newline="\n") as f:
        f.write("# x\n\n## 环境档（← C1 产出）\n\n| 项 | 值 |\n| --- | --- |\n"
                "| 档位 | **强势** |\n| 仓位天花板（成） | 5 |\n"
                "| as_of / source | 2026-09-29 / SNAPSHOT |\n| 涨停家数 | 60 |\n"
                "| 连板高度 | 4 |\n| 炸板率 | 20.0% |\n\n## n\n")


def _purge_pycache():
    here = os.path.dirname(os.path.abspath(__file__))
    pc = os.path.join(here, "__pycache__")
    if os.path.isdir(pc):
        import shutil
        shutil.rmtree(pc, ignore_errors=True)


# ── 纪律（静态扫描生产区） ───────────────────────────────────────────────────
def _prod_source():
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    i = src.index("def _selftest")
    return src[:i]


def _discipline(verbose=True):
    """静态纪律：**只扫生产区 AST token**（注释/文档字符串里的说明不算违例——
    首版用正则扫文本，被自身注释里的示例字面量误报，现按 AST 判据重写）。"""
    import ast
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    prod_n = src[:src.index("def _selftest")].count("\n") + 1
    nodes = [n for n in ast.walk(tree) if getattr(n, "lineno", 0) <= prod_n]
    ids = set()
    for n in nodes:
        if isinstance(n, ast.Name):
            ids.add(n.id)
        elif isinstance(n, ast.Attribute):
            ids.add(n.attr)
    ok = []

    # ① 零写点（本件是纯只读接缝）：写型 open / os.replace / makedirs / write sink
    wmodes = ("w", "a", "w+", "a+", "wb", "ab", "x")
    sinks = ("replace", "makedirs", "remove", "unlink", "rename", "rmtree")
    hits = []
    for n in nodes:
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        nm = getattr(f, "id", None) or getattr(f, "attr", None)
        if nm == "open":
            if any(isinstance(a, ast.Constant) and a.value in wmodes for a in n.args):
                hits.append("open(%s)" % ",".join(
                    str(a.value) for a in n.args
                    if isinstance(a, ast.Constant) and a.value in wmodes))
        elif nm in sinks:
            # 只认 os./shutil. 模块级写型调用；**不认 str.replace 这类方法链**
            # （首版无差别匹配 `replace` 被 str.replace 误报，现按调用者限定）
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                    and f.value.id in ("os", "shutil"):
                hits.append("%s.%s" % (f.value.id, nm))
        elif nm in ("write", "writelines"):
            hits.append(nm)
    ok.append(("① 零写点（生产区无文件写）", not hits, hits))

    # ② 零下单 / 零风控闸调用（接缝不得越权触碰执行与风控）
    forb = ("order" + "_create", "place" + "_order", "position" + "_gate",
            "exit" + "_gate", "append" + "_" + "risk", "settle")
    hits = sorted(ids & set(forb))
    ok.append(("② 零下单 / 零风控闸调用", not hits, hits))

    # ③ 零日期算术（确定性：同输入同 brief，不回放漂移）
    hits = sorted(ids & {"timedelta", "now", "today", "time"})
    ok.append(("③ 零日期算术 / 零 wall-clock", not hits, hits))

    # ④ 零参数正式值写入 / 零 promote（参数只走候选→批准链）
    hits = sorted(ids & {"propose", "promote", "_write_params"})
    ok.append(("④ 零参数写入 / 零 promote", not hits, hits))

    # ⑤ 零重定义：生产区不得出现第二套动作/驱动字面量表（AST Tuple 判据）
    ACTS = set(TQ.ACTIONS)
    hit = []
    for n in nodes:
        if isinstance(n, (ast.Tuple, ast.List, ast.Set)):
            vals = {e.value for e in n.elts if isinstance(e, ast.Constant)}
            if ACTS and ACTS <= vals:
                hit.append(ast.dump(n)[:60])
    ok.append(("⑤ 零重定义（无第二套动作字面量表）", not hit, hit))

    # ⑥ 判断本体在 LLM：本件不得自造动作裁决（无 answer/verdict 构造）
    hits = sorted(ids & {"answer"})
    ok.append(("⑥ 零动作裁决（动作只由 three_q 机械分流）", not hits, hits))

    if verbose:
        for name, good, extra in ok:
            print(("  [ok] " if good else "  [!!] ") + name
                  + ("" if good else " | %s" % extra))
    return 0 if all(g for _, g, _ in ok) else 1


def _main(argv):
    ap = argparse.ArgumentParser(description="C4 三问接入接缝（装配 + 校验）")
    ap.add_argument("--assemble", action="store_true", help="装配 brief 并打印")
    ap.add_argument("--as-of")
    ap.add_argument("--code")
    ap.add_argument("--brain")
    ap.add_argument("--params")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--discipline", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if a.discipline:
        return _discipline()
    if a.assemble:
        if not (a.as_of and a.code):
            ap.error("--assemble 需 --as-of 与 --code")
        b = assemble(a.as_of, a.code, brain_path=a.brain, params_path=a.params)
        print(json.dumps(b, ensure_ascii=False, indent=1))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
