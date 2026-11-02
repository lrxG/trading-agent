#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""three_q.py · 1.4 三问决策 C4（**协议层** · v1.0 · 2026-10-08 建）

用途：把 C3 的驱动链压成一个可执行动作（BUY / WAIT / NO）—— **神经链的收敛出口**。
      收敛器不是分析器：本模块不产新证据、不查行情、不算仓位、不下单。

★ 一、零重定义（本模块的第一纪律）
  三问涉及的**每一个字段名与枚举都取自上游真源**，本模块一个都不新造：
    · 动作       ACTIONS      ← `decision.ACTIONS`（同一对象，`is` 判定，非拷贝）
    · 驱动类别   DRIVERS      ← `decision.DRIVERS`（与 `candidates.DRIVER_TYPES` 同源）
    · 五件套键   FIVE_KEYS    ← `decision.FIVE_KEYS`（键名 = 设计方案原文）
    · 驱动链键   CHAIN_KEYS   ← `decision.CHAIN_KEYS`（evidence/inference/prediction/five）
    · 价线与仓位 fields       ← `decision.DECISION_FIELDS` 的子集，**原字面量**：
                                `entry_limit` / `stop_line` / `target` / `window` /
                                `position_request`
    · 条件结构   {"text","rule"} ← 0.5 `candidates` 的三条件同形（`rule.op ∈ candidates.OPS`）
  自检 `A 组` + `--discipline` 对此做机械断言：任何一处写错名字（例如把 `stop_line`
  写成 `stop`、把 `position_request` 写成 `position`）当场报错。
  ⇒ **协议层不许有同义字段**：「协议重新定义一套同义字段」是这一层最容易犯、也最难发现的病。

★ 二、证据不足机械分流（LLM 零自由裁量）
  施工 1.4 写死的分流，落成机械判据：
    ① `driver == "驱动不明"`（0.3 顶层字段）                     → **NO**（机械，不看其他问）
    ② 驱动问证据不足（五件套缺件 / 驱动不明）                    → 进③
       赔率问证据不足（entry / stop / target / window 缺件）      → 进③
       证伪问证据不足（stop_line 写不出）                        → 进③
    ③ 是否存在**明确、可验证的补全事件**（`completion_event`）？
         有 → **WAIT** + `next_trigger = completion_event`
         无 → **NO**  + 翻案条件（`overturn_event`，**必填**）
  三问全过（且 driver 明确）→ **BUY**，`position_request` 由调用方显式给出。
  本模块**不替 LLM 补判**：缺口、补全事件、翻案条件都是**入参**；缺了就是缺了，不给默认值。

★ 三、零模糊输出（硬验收的执行点）
  `verify_verdict()` 在**写盘之前**机械拦住三类模糊：
    · BUY 而五字段不齐 / `position_request <= 0`；
    · WAIT / NO 而价位非空（**有价位就是 BUY 的意图** —— 0.3 同款判据）；
    · WAIT / NO 而 `next_trigger`（WAIT=等什么事件 / NO=翻案条件）为空、
      或写的是空话（「再看看」「视情况」「研究研究」…）。
  空话黑名单是显式的、可审的（见 `VAGUE_WORDS`）—— 不做语义判断，只做字面拒绝。
  另：**价线方向自相矛盾**（`target <= entry_limit` 或 `entry_limit <= stop_line`）不是
  「答不出」，是调用方给错了数 —— 直接抛错（`ODDS_INVERTED`），不降级成 WAIT。

★ 四、边界（施工 1.4 原文，逐条落成不变量）
  · **C4 不执行交易**（→ C5 裁决 / 执行链）：本模块无任何下单接口，报告里 `orders_created`
    恒 0，且**不接受** `position_cap` 之类输入 —— 仓位是 4.1 授权下的自主请求，不是算出来的。
  · **C4 不结账**（→ C6）：结果一律进 `pnl_ledger`，本模块不写。
  · **C4 不产新文档**：唯一写动作 = 向 `decision_ledger` 追加一行 + 向候选池回填锚。
  · **不越权成 C5**：`position_request` 是本模块**原样带出**的请求，绝不裁剪、绝不上限。

★ 五、落盘：二段式（**这是 1.4 与 0.3 的接线口径**）
  段一 · 决策账：`record_decision()` → `decision.write_decision()`（账本唯一写入口）。
        三种动作**都记**（施工 1.4「全写 decision_ledger」= 三种动作都要留痕，
        不是只记 BUY）；WAIT / NO 时五字段为空、`position_request = 0`（0.3 硬校验兜底）。
  段二 · 观察锚：`anchor_pool()` → `candidates.backfill_candidate(next_trigger=...)`。
        WAIT 的「等什么事件」与 NO 的「翻案条件」是**同一件事**（下一步要看的事件），
        故共用 0.5 既存的同名字段 `next_trigger`，**不新造字段、不两套叫法**
        （施工 1.4 对 NO 的原文即「自动成观察锚**进候选池**」）。
        BUY 不落锚（候选使命完成）；池中无此 code → 返回 `NO_CANDIDATE`，
        **三问不代建候选**（建候选是 1.2 扫描器的职责，越权即侵入）。
  ⇒ 账本保证「此刻的决策已有出路」，候选池保证「出路本身可被后续步进消费」——
     合成 `commit()`；任何一半都算半环。

★ 六、本模块不碰文件
  没有任何 `open()` / `os.replace` 写点是本模块的（`--discipline` 在**生产区**断言）。
  两个落点全部经上游模块的写接口，落点审计（0.7）只认「账本 / 候选池」两个既存落点。

被谁调用：4.2 调度（QUALIFIED 候选 → 三问）、4.1 人格自主决策链、阶段 0 联调。
用法：
  python tools/fs/three_q.py                 # 三问契约总览（零写入）
  python tools/fs/three_q.py --selftest      # 内置验收（临时目录，不碰真数据）
  python tools/fs/three_q.py --discipline    # 纪律自检（零重定义 / 零写盘 / 零下单）
"""
import copy
import inspect
import io
import json
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.dirname(os.path.abspath(__file__))))
import decision as DEC      # noqa: E402   ← 字段名与枚举的唯一真源
import candidates as CAND   # noqa: E402   ← 条件结构（{"text","rule"}）的真源
import thesis as TH        # noqa: E402
import astwin as ASTW      # noqa: E402   ← AST 窗口（尾界）唯一实现，禁本模块自写

SCHEMA_VERSION = 1

# ── 零重定义：以下全部是**别名**（同一对象 / 同源常量），不是第二份定义 ─────────────
ACTIONS = DEC.ACTIONS                       # ("BUY","WAIT","NO")
DRIVERS = DEC.DRIVERS                       # 与 CAND.DRIVER_TYPES 同源
FIVE_KEYS = DEC.FIVE_KEYS
CHAIN_KEYS = DEC.CHAIN_KEYS
PRED_SLOTS = DEC.PRED_SLOTS
COND_KEYS = ("text", "rule")                # 0.5 三条件同形（见 candidates._norm_cond）
COND_OPS = CAND.OPS

ACT_BUY, ACT_WAIT, ACT_NO = DEC.ACTIONS[0], DEC.ACTIONS[1], DEC.ACTIONS[2]
DRIVER_UNKNOWN = "驱动不明"
POS_TOL = 1e-9                              # 与 decision 同口径的浮点零容差

# 本模块会写进账本的字段（**必须**是 DECISION_FIELDS 的子集，A4 断言）
LEDGER_FIELDS = ("entry_limit", "stop_line", "target", "window", "position_request")

# ── 三问 ─────────────────────────────────────────────────────────────────────
Q_DRIVER, Q_ODDS, Q_FALSIFY = "驱动", "赔率", "证伪"
QUESTIONS = (Q_DRIVER, Q_ODDS, Q_FALSIFY)
Q_PASS, Q_FAIL = "PASS", "FAIL"

# 证据不足的缺口名（**不是**字段名，只是诊断标签；字段名一律取自 DECISION_FIELDS）
GAP_DRIVER_UNKNOWN = "DRIVER_UNKNOWN"
GAP_MISSING_FIVE = "MISSING_FIVE"
GAP_MISSING_ODDS = "MISSING_ODDS"
GAP_MISSING_FALSIFY = "MISSING_FALSIFY"
GAPS = (GAP_DRIVER_UNKNOWN, GAP_MISSING_FIVE, GAP_MISSING_ODDS, GAP_MISSING_FALSIFY)

# 空话黑名单（施工 1.4「零模糊输出，无"再看看"」—— 只做字面拒绝，不判语义）
VAGUE_WORDS = ("再看看", "看情况", "视情况", "酌情", "酌定", "待定", "观察一下",
               "研究研究", "先放着", "大概率", "可能会", "不太确定")

# ── 三问 API 签名（骨架冻结件：`const:tools/fs/three_q.py:THREE_Q_API`）──────────
# 结构 = (函数名, 必填位置参数, 仅关键字参数)。自检 A6 用 inspect 逐字比对实现；
# 改签名 = 改骨架 = 显式动作（升 SCHEMA_VERSION + 重建冻结台账），不能顺手改。
THREE_Q_API = (
    ("answer", ("as_of", "code", "thesis_id", "thesis"),
     ("driver", "thesis_state", "env", "entry_limit", "stop_line", "target",
      "window", "position_request", "completion_event", "overturn_event",
      "thesis_path", "params_path")),
    ("verify_verdict", ("verdict",), ()),
    ("record_decision", ("verdict",),
     ("sim", "env", "thesis_state", "info_set_hash", "call_chain",
      "base", "thesis_path", "params_path", "persona_path", "frozen_at")),
    ("anchor_pool", ("verdict",), ("pool_path",)),
    ("commit", ("verdict",),
     ("sim", "pool_path", "base", "env", "thesis_state", "info_set_hash",
      "call_chain", "thesis_path", "params_path", "persona_path", "frozen_at")),
)


class ThreeQError(Exception):
    """三问协议自身的错（入参矛盾 / 零模糊输出未达标）—— 显式抛错，不静默降级。"""


# ── 入参规范化（只做机械可判的部分）───────────────────────────────────────────
def _check_text(s, name, allow_empty=False):
    if not isinstance(s, str) or (not allow_empty and not s.strip()):
        raise ThreeQError("%s 必须是非空字符串，收到: %r" % (name, s))
    return s.strip()


def _norm_event(ev, name, required=True):
    """把事件规范成 0.5 同形的 `{"text":..,"rule":..}`。**不新造第二套条件结构。**

    `rule` 可为 null（人话条件），非空时必须是 `{field,op,value}` 且 `op ∈ candidates.OPS`。
    空话（`VAGUE_WORDS`）一律拒 —— 「零模糊输出」的落点。
    """
    if ev is None:
        if required:
            raise ThreeQError("%s 必填（1.4：WAIT 无事件 / NO 无翻案条件 = 模糊输出）" % name)
        return None
    if isinstance(ev, str):
        ev = {"text": ev, "rule": None}
    if not isinstance(ev, dict):
        raise ThreeQError("%s 必须是字符串或 {text,rule} 对象，收到: %r" % (name, ev))
    extra = set(ev) - set(COND_KEYS)
    if extra:
        raise ThreeQError("%s 只允许 %s（与 0.5 三条件同形），多出: %s"
                          % (name, " / ".join(COND_KEYS), sorted(extra)))
    text = _check_text(ev.get("text"), "%s.text" % name)
    for w in VAGUE_WORDS:
        if w in text:
            raise ThreeQError("%s 含模糊表述 %r —— 1.4 零模糊输出：必须写可验证事件"
                              "（收到: %r）" % (name, w, text))
    rule = ev.get("rule")
    if rule is not None:
        if not isinstance(rule, dict):
            raise ThreeQError("%s.rule 必须是对象或 null" % name)
        r_extra = set(rule) - {"field", "op", "value"}
        if r_extra or not {"field", "op"} <= set(rule):
            raise ThreeQError("%s.rule 必须恰含 field/op/value（多=%s 缺=%s）"
                              % (name, sorted(r_extra),
                                 sorted({"field", "op"} - set(rule))))
        _check_text(rule["field"], "%s.rule.field" % name)
        if rule["op"] not in COND_OPS:
            raise ThreeQError("%s.rule.op 只能是 %s（与 candidates.OPS 同源），实际: %r"
                              % (name, " / ".join(COND_OPS), rule["op"]))
    return {"text": text, "rule": rule}


def _read_five(thesis):
    """只**读**五件套，不重复校验（结构校验的唯一真源是 `decision._norm_chain`）。"""
    five = (thesis or {}).get("five")
    if not isinstance(five, dict):
        return None, list(FIVE_KEYS)
    missing = [k for k in FIVE_KEYS if five.get(k) is None or not str(five.get(k)).strip()]
    return five, missing


def _resolve_driver(driver, thesis_state, thesis_id, thesis_path):
    """驱动类别：显式 > thesis_state.type > thesis.md 现值。三者都没有 → 抛错（不猜）。"""
    if driver is not None:
        return _check_text(driver, "driver")
    if isinstance(thesis_state, dict) and thesis_state.get("type"):
        return _check_text(thesis_state["type"], "thesis_state.type")
    try:
        st = TH.read_thesis(thesis_id, thesis_path)
        if st.get("type"):
            return _check_text(st["type"], "thesis.type")
    except TH.ThesisError:
        pass
    raise ThreeQError("无法确定 driver（显式未给、thesis_state 无 type、thesis.md 也取不到）"
                      " —— 1.4 禁猜：驱动不明请显式传 %r" % DRIVER_UNKNOWN)


def _check_odds_number(v, name, allow_none=True):
    if v is None:
        if allow_none:
            return None
        raise ThreeQError("%s 必填" % name)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ThreeQError("%s 必须是数字，收到: %r" % (name, v))
    v = float(v)
    if v <= 0:
        raise ThreeQError("%s 必须 > 0（价线），收到: %r" % (name, v))
    return v


# ── 三问逐问（每问：过 / 不过 + 缺口标签）─────────────────────────────────────
def _ask_driver(driver, five, missing_five):
    if driver == DRIVER_UNKNOWN:
        return {Q_PASS: False, "gap": GAP_DRIVER_UNKNOWN,
                "why": "driver=%s（0.3 顶层字段）—— 施工 1.4：机械 NO" % DRIVER_UNKNOWN,
                "five_missing": missing_five}
    if missing_five:
        return {Q_PASS: False, "gap": GAP_MISSING_FIVE,
                "why": "五件套缺件 %s —— 驱动答不出（答不出不下）" % missing_five,
                "five_missing": missing_five}
    return {Q_PASS: True, "gap": None,
            "why": "五件套齐全 + driver=%s" % driver, "five_missing": []}


def _ask_odds(entry_limit, stop_line, target, window):
    miss = [n for n, v in (("entry_limit", entry_limit), ("stop_line", stop_line),
                           ("target", target), ("window", window)) if v is None]
    if miss:
        return {Q_PASS: False, "gap": GAP_MISSING_ODDS,
                "why": "赔率三件/窗口缺件 %s（答不出不下）" % miss, "odds": None}
    if not (target > entry_limit > stop_line):
        raise ThreeQError(
            "ODDS_INVERTED：价位方向必须 target(%s) > entry_limit(%s) > stop_line(%s) —— "
            "这是调用方给错数，不是「答不出」（不降级成 WAIT）"
            % (target, entry_limit, stop_line))
    odds = (target - entry_limit) / (entry_limit - stop_line)
    return {Q_PASS: True, "gap": None,
            "why": "(target-entry)/(entry-stop) = (%.4f-%.4f)/(%.4f-%.4f)"
                   % (target, entry_limit, entry_limit, stop_line), "odds": odds}


def _ask_falsify(stop_line):
    if stop_line is None:
        return {Q_PASS: False, "gap": GAP_MISSING_FALSIFY,
                "why": "stop_line 写不出 —— 证伪线是机械价线，不是心理止损（答不出不下）"}
    return {Q_PASS: True, "gap": None, "why": "stop_line=%.4f 可机械校验" % stop_line}


# ── 主接口 ①：answer（纯判断，零写入）────────────────────────────────────────
def answer(as_of, code, thesis_id, thesis, *, driver=None, thesis_state=None,
           env=None, entry_limit=None, stop_line=None, target=None, window=None,
           position_request=None, completion_event=None, overturn_event=None,
           thesis_path=None, params_path=None):
    """三问裁决 → verdict（**不做任何写入**；落盘见 record_decision / commit）。

    逐问映射（施工 1.4）：
      ① 驱动问 —— 读 `thesis.five`（五件套齐不齐）+ `driver`（驱动不明？）
      ② 赔率问 —— `(target - entry_limit) / (entry_limit - stop_line)`，三件 + window 齐？
      ③ 证伪问 —— `stop_line` 写得出吗
    任一问「答不出」→ 进机械分流：有 `completion_event` → WAIT + next_trigger；
    无 → NO + 翻案条件（`overturn_event`）。`driver == 驱动不明` → 直接 NO。
    全过且 driver 明确 → BUY（`position_request` 必须由调用方显式给出，见边界）。
    """
    a_as_of = DEC._check_date(as_of, "as_of")
    a_code = DEC._check_code(code)
    th_id = DEC._check_thesis_id(thesis_id)
    if not isinstance(thesis, dict):
        raise ThreeQError("thesis 必须是 C3 驱动链对象（evidence/inference/prediction/five）")

    dv = _resolve_driver(driver, thesis_state, th_id, thesis_path)
    if dv not in DRIVERS:
        raise ThreeQError("driver 只能是 %s，实际: %r" % (" / ".join(DRIVERS), dv))

    five, missing_five = _read_five(thesis)
    e = _check_odds_number(entry_limit, "entry_limit")
    s = _check_odds_number(stop_line, "stop_line")
    t = _check_odds_number(target, "target")
    w = window
    if w is not None and (isinstance(w, bool) or not isinstance(w, int) or w < 1):
        raise ThreeQError("window 是持有窗口（交易日，整数 >= 1），收到: %r" % (window,))

    q1 = _ask_driver(dv, five, missing_five)
    q2 = _ask_odds(e, s, t, w)
    q3 = _ask_falsify(s)
    questions = {Q_DRIVER: q1, Q_ODDS: q2, Q_FALSIFY: q3}
    gaps = [q["gap"] for q in (q1, q2, q3) if q["gap"]]
    reasons = ["%s：%s" % (k, questions[k]["why"]) for k in QUESTIONS]

    next_trigger = None
    if dv == DRIVER_UNKNOWN:
        action = ACT_NO
        reasons.append("驱动不明 → 机械 NO（施工 1.4，不看其余两问）")
        next_trigger = _norm_event(overturn_event, "overturn_event")
    elif gaps:
        if completion_event is not None:
            action = ACT_WAIT
            next_trigger = _norm_event(completion_event, "completion_event")
            reasons.append("证据不足 %s，但存在可验证补全事件 → WAIT" % gaps)
        else:
            action = ACT_NO
            next_trigger = _norm_event(overturn_event, "overturn_event")
            reasons.append("证据不足 %s 且无补全事件 → NO（翻案条件必填）" % gaps)
    else:
        action = ACT_BUY
        pr = _check_odds_number(position_request, "position_request", allow_none=False)
        reasons.append("三问齐过 → BUY")
        position_request = pr

    pr_out = 0.0 if action in (ACT_WAIT, ACT_NO) else float(position_request)
    verdict = {
        "schema_version": SCHEMA_VERSION,
        "as_of": a_as_of, "code": a_code, "thesis_id": th_id,
        "action": action, "driver": dv,
        "questions": questions, "gaps": gaps, "reasons": reasons,
        "odds": q2["odds"],
        "thesis": copy.deepcopy(thesis),
        "thesis_state": copy.deepcopy(thesis_state),
        "env": copy.deepcopy(env),
        # 字段名 = DECISION_FIELDS 的原字面量（A4 断言），WAIT/NO 一律置空
        "fields": {"entry_limit": e if action == ACT_BUY else None,
                   "stop_line": s if action == ACT_BUY else None,
                   "target": t if action == ACT_BUY else None,
                   "window": w if action == ACT_BUY else None,
                   "position_request": pr_out},
        "next_trigger": next_trigger,
        "orders_created": 0,
    }
    return verdict


# ── 主接口 ②：verify_verdict（零模糊输出的机械校验）──────────────────────────
def verify_verdict(verdict):
    """返回问题清单（空 = 达标）。**写盘前必须过**；不做任何修正、不猜意图。"""
    p = []
    if not isinstance(verdict, dict):
        return ["verdict 必须是 answer() 的返回对象"]
    action = verdict.get("action")
    if action not in ACTIONS:
        p.append("action 必须是 %s，实际: %r" % (" / ".join(ACTIONS), action))
        return p
    f = verdict.get("fields") or {}
    extra = set(f) - set(LEDGER_FIELDS)
    if extra:
        p.append("fields 含非账本字段 %s —— 1.4 协议不得自造字段名" % sorted(extra))
    miss_names = set(LEDGER_FIELDS) - set(f)
    if miss_names:
        p.append("fields 缺 %s（五字段必须齐键，空值写 null 而非省略）" % sorted(miss_names))

    e, s, t, w = (f.get("entry_limit"), f.get("stop_line"),
                  f.get("target"), f.get("window"))
    pr = f.get("position_request")
    nt = verdict.get("next_trigger")

    if action == ACT_BUY:
        for nm, v in (("entry_limit", e), ("stop_line", s), ("target", t), ("window", w)):
            if v is None:
                p.append("BUY 时 %s 必填（赔率三件 + 窗口齐才能下注）" % nm)
        if e is not None and s is not None and t is not None and not (t > e > s):
            p.append("BUY 价位方向必须 target > entry_limit > stop_line")
        if pr is None or pr <= POS_TOL:
            p.append("BUY 时 position_request 必须 > 0（C4 请求仓，4.1 授权内自主提出）")
        if nt is not None:
            p.append("BUY 时不得带 next_trigger（下注了就不需要观察锚）")
        if verdict.get("odds") is None:
            p.append("BUY 时 odds 必须已算出")
    else:
        for nm, v in (("entry_limit", e), ("stop_line", s), ("target", t), ("window", w)):
            if v is not None:
                p.append("%s 时 %s 必须为 null —— 有价位就是 BUY 的意图" % (action, nm))
        if pr not in (0, 0.0, None):
            p.append("%s 时 position_request 必须为 0（不下注）" % action)
        if nt is None:
            p.append("%s 缺 next_trigger（WAIT=等什么事件 / NO=翻案条件）—— 零模糊输出不达标"
                     % action)
        else:
            try:
                _norm_event(nt, "next_trigger")
            except ThreeQError as ex:
                p.append("next_trigger 不合规：%s" % ex)
        if not verdict.get("gaps"):
            p.append("%s 必须记录证据缺口（gaps 空 = 看不出为什么不下）" % action)
        if not verdict.get("reasons"):
            p.append("%s 必须留 reasons（决策依据不可空）" % action)
    return p


# ── 主接口 ③：record_decision（段一 · 决策账）────────────────────────────────
def record_decision(verdict, *, sim, env=None, thesis_state=None, info_set_hash=None,
                    call_chain=None, base=None, thesis_path=None, params_path=None,
                    persona_path=None, frozen_at=None):
    """把 verdict 落成一条 DECISION（**唯一写入口 = decision.write_decision**）。

    `sim` 无默认值 —— 与 0.3 同纪律：不表态即报错，杜绝「默认进 LIVE」。
    零模糊输出未达标 → 抛 ThreeQError，**不写盘**（先在内存里拦住）。
    """
    problems = verify_verdict(verdict)
    if problems:
        raise ThreeQError("零模糊输出未达标，拒绝落盘：\n  - %s" % "\n  - ".join(problems))
    if sim is None:
        raise ThreeQError("sim 必填（True=SIM / False=LIVE）—— 无默认值，不表态即报错")
    f = verdict["fields"]
    r = DEC.write_decision(
        verdict["as_of"], verdict["code"], verdict["action"], verdict["thesis_id"],
        verdict["thesis"], sim=bool(sim), driver=verdict["driver"],
        thesis_state=thesis_state if thesis_state is not None else verdict.get("thesis_state"),
        entry_limit=f["entry_limit"], stop_line=f["stop_line"], target=f["target"],
        window=f["window"], position_request=f["position_request"],
        env=env if env is not None else verdict.get("env"),
        info_set_hash=info_set_hash, call_chain=call_chain, frozen_at=frozen_at,
        base=base, thesis_path=thesis_path, params_path=params_path,
        persona_path=persona_path)
    out = {"decision_id": r["id"], "created": r["created"], "reused": r.get("reused", False),
           "axis": r["axis"], "ledger": r["ledger"], "action": verdict["action"],
           "orders_created": 0}
    return out


# ── 主接口 ④：anchor_pool（段二 · 观察锚）───────────────────────────────────
def anchor_pool(verdict, *, pool_path=None):
    """WAIT / NO 的出路回填候选池（**0.5 既存字段 `next_trigger`**，不新造字段）。

    · BUY → 不落锚（候选使命完成，TRADED 由 0.5 步进处理）；
    · 池中无此 code → `NO_CANDIDATE`，**三问不代建候选**（建候选 = 1.2 扫描器的职责）；
    · 候选已终态 → `TERMINAL`（0.5 拒绝回填，如实上报，不绕过）。
    """
    action = verdict.get("action")
    if action == ACT_BUY:
        return {"anchored": False, "reason": "BUY 无需观察锚", "candidate_id": None}
    try:
        cs = [c for c in CAND.list_candidates(code=verdict["code"], path=pool_path)
              if c["state"] not in CAND.TERMINAL_STATES]
    except CAND.CandidateError as ex:
        return {"anchored": False, "reason": "POOL_ERROR:%s" % ex, "candidate_id": None}
    if not cs:
        return {"anchored": False, "reason": "NO_CANDIDATE", "candidate_id": None,
                "note": "三问不代建候选（建候选 = 1.2 扫描器职责）"}
    cid = sorted(cs, key=lambda c: (c["created_at"], c["id"]))[-1]["id"]
    try:
        c = CAND.backfill_candidate(
            cid, verdict["as_of"], next_trigger=verdict["next_trigger"],
            keep_reason="C4 三问 %s：%s" % (action, " / ".join(verdict.get("gaps") or ["-"])),
            path=pool_path)
    except CAND.CandidateError as ex:
        return {"anchored": False, "reason": "TERMINAL:%s" % ex, "candidate_id": cid}
    return {"anchored": True, "candidate_id": cid, "state": c["state"],
            "next_trigger": c["next_trigger"], "reason": None}


# ── 主接口 ⑤：commit（两段一起落 —— 半环留痕）───────────────────────────────
def commit(verdict, *, sim, pool_path=None, base=None, env=None, thesis_state=None,
           info_set_hash=None, call_chain=None, thesis_path=None, params_path=None,
           persona_path=None, frozen_at=None):
    """段一 + 段二。返回合并报告；`orders_created` 恒 0（C4 不执行交易）。"""
    dec = record_decision(verdict, sim=sim, env=env, thesis_state=thesis_state,
                          info_set_hash=info_set_hash, call_chain=call_chain, base=base,
                          thesis_path=thesis_path, params_path=params_path,
                          persona_path=persona_path, frozen_at=frozen_at)
    anc = anchor_pool(verdict, pool_path=pool_path)
    return {"action": verdict["action"], "decision": dec, "anchor": anc,
            "orders_created": 0}


# ── 自检 ─────────────────────────────────────────────────────────────────────
def _five(text="x"):
    return {k: (text if k != "剩余预期" else text) for k in FIVE_KEYS}


def _chain(five=None, slots=None):
    return {"evidence": ["e1"], "inference": ["i1"],
            "prediction": [{"prediction_id": k, "metric": "m", "data_source": "d",
                            "baseline": "b", "direction": "UP", "threshold": "t",
                            "condition": "c", "window": 3}
                           for k in (slots or PRED_SLOTS)],
            "five": _five() if five is None else five}


def _selftest():
    import shutil
    import tempfile
    import landing as L  # noqa: F401  （确认 0.7 审计器在，并复用其审计结论）
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

    def raises(fn, *a, **kw):
        try:
            fn(*a, **kw)
            return None
        except Exception as ex:      # noqa: BLE001
            return str(ex)

    tmp = tempfile.mkdtemp(prefix="ta_threeq_selftest_")
    try:
        print("== A. 契约：零重定义（字段名/枚举全部取自上游真源） ==")
        T("A1 ACTIONS 与 decision.ACTIONS 是**同一对象**（非拷贝）", ACTIONS is DEC.ACTIONS)
        T("A2 DRIVERS == candidates.DRIVER_TYPES（驱动类别同源）",
          DRIVERS == CAND.DRIVER_TYPES)
        T("A3 FIVE_KEYS / CHAIN_KEYS / PRED_SLOTS 同源",
          FIVE_KEYS is DEC.FIVE_KEYS and CHAIN_KEYS is DEC.CHAIN_KEYS
          and PRED_SLOTS is DEC.PRED_SLOTS)
        T("A4 本模块写进账本的字段名 ⊆ decision.DECISION_FIELDS",
          set(LEDGER_FIELDS) <= set(DEC.DECISION_FIELDS),
          "多出: %s" % sorted(set(LEDGER_FIELDS) - set(DEC.DECISION_FIELDS)))
        T("A5 条件结构复用 0.5 的 {text,rule}，且 op ⊆ candidates.OPS",
          COND_KEYS == ("text", "rule") and COND_OPS == CAND.OPS)
        sigs = {n: inspect.signature(f) for n, f in (
            ("answer", answer), ("verify_verdict", verify_verdict),
            ("record_decision", record_decision), ("anchor_pool", anchor_pool),
            ("commit", commit))}
        bad = []
        for nm, pos, kw in THREE_Q_API:
            s = sigs[nm]
            gp = tuple(p.name for p in s.parameters.values()
                       if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
            gk = tuple(p.name for p in s.parameters.values() if p.kind == p.KEYWORD_ONLY)
            if gp != pos or gk != kw:
                bad.append("%s: 实=%s/%s 签=%s/%s" % (nm, gp, gk, pos, kw))
        T("A6 THREE_Q_API 冻结签名与实现逐字一致（改签名必须重建台账）", not bad, bad)
        T("A7 本模块不新造价线/仓位同义词（源码无 stop/target_price/entry_price 等别名）",
          not _synonym_leaks())

        print("== B. 三问逐问与机械分流 ==")
        v = answer("2026-10-08", "sh600000", "TH-20261008-001", _chain(),
                   driver="A", entry_limit=10.0, stop_line=9.0, target=12.0,
                   window=3, position_request=0.2)
        T("B1 三问齐过 + driver 明确 → BUY", v["action"] == "BUY"
          and all(v["questions"][q][Q_PASS] for q in QUESTIONS))
        T("B2 赔率算式 = (target−entry)/(entry−stop)", abs(v["odds"] - 2.0) < 1e-9,
          v["odds"])
        v2 = answer("2026-10-08", "sh600000", "TH-20261008-001", _chain(),
                    driver=DRIVER_UNKNOWN, overturn_event="新公告出现且驱动可命名")
        T("B3 driver=驱动不明 → 机械 NO（不必给价线）", v2["action"] == "NO"
          and GAP_DRIVER_UNKNOWN in v2["gaps"])
        T("B4 驱动不明优先于其余两问（即便有价线也不 BUY）",
          v2["fields"]["entry_limit"] is None and v2["fields"]["target"] is None)
        v3 = answer("2026-10-08", "sh600000", "TH-20261008-001", _chain(),
                    driver="A", entry_limit=10.0, stop_line=9.0, window=3,
                    completion_event={"text": "出中报确认订单", "rule":
                                      {"field": "order_amount", "op": ">=", "value": 1e8}})
        T("B5 缺 target + 有补全事件 → WAIT + next_trigger",
          v3["action"] == "WAIT" and v3["next_trigger"]["text"] == "出中报确认订单"
          and GAP_MISSING_ODDS in v3["gaps"])
        v4 = answer("2026-10-08", "sh600000", "TH-20261008-001", _chain(),
                    driver="A", entry_limit=10.0, stop_line=9.0, window=3,
                    overturn_event="板块出现第二个涨停")
        T("B6 缺 target + 无补全事件 → NO + 翻案条件",
          v4["action"] == "NO" and v4["next_trigger"]["text"] == "板块出现第二个涨停")
        T("B7 缺 stop_line → 证伪问缺口 → 分流（不是「降级成 WAIT」）",
          GAP_MISSING_FALSIFY in answer(
              "2026-10-08", "sh600000", "TH-20261008-001", _chain(), driver="A",
              entry_limit=10.0, target=12.0, window=3,
              overturn_event="出现新催化")["gaps"])
        v5 = answer("2026-10-08", "sh600000", "TH-20261008-001",
                    _chain(five={k: (None if k == "剩余预期" else "x") for k in FIVE_KEYS}),
                    driver="A", entry_limit=10.0, stop_line=9.0, target=12.0, window=3,
                    overturn_event="补齐剩余预期")
        T("B8 五件套缺件 → 驱动问缺口 → 分流为 NO（留缺口名）",
          v5["action"] == "NO" and GAP_MISSING_FIVE in v5["gaps"])
        T("B9 价位方向矛盾 → 抛 ODDS_INVERTED（不降级成 WAIT）",
          "ODDS_INVERTED" in (raises(answer, "2026-10-08", "sh600000", "TH-20261008-001",
                                    _chain(), driver="A", entry_limit=12.0, stop_line=9.0,
                                    target=10.0, window=3) or ""))
        T("B10 三问全过但未给 position_request → 拒绝（C4 不自算仓位）",
          raises(answer, "2026-10-08", "sh600000", "TH-20261008-001", _chain(),
                 driver="A", entry_limit=10.0, stop_line=9.0, target=12.0, window=3)
          is not None)

        print("== C. 零模糊输出（写盘前机械拦住） ==")
        T("C1 BUY 全字段齐 → verify 无问题", verify_verdict(v) == [])
        T("C2 WAIT 缺 next_trigger → 报错并拦住落盘",
          raises(record_decision, dict(v3, next_trigger=None), sim=True) is not None
          and any("next_trigger" in x for x in verify_verdict(dict(v3, next_trigger=None))))
        T("C3 NO 缺翻案条件 → 在 answer 阶段就抛（不给默认）",
          raises(answer, "2026-10-08", "sh600000", "TH-20261008-001", _chain(),
                 driver="A", entry_limit=10.0, stop_line=9.0, window=3) is not None)
        T("C4 空话表述被拒（『再看看』类）",
          "模糊" in (raises(answer, "2026-10-08", "sh600000", "TH-20261008-001", _chain(),
                            driver="A", entry_limit=10.0, stop_line=9.0, window=3,
                            completion_event="再看看") or ""))
        T("C5 rule.op 越界被拒（与 candidates.OPS 同源）",
          raises(_norm_event, {"text": "x", "rule": {"field": "f", "op": "~", "value": 1}},
                 "ev") is not None)
        T("C6 WAIT/NO 带价位 → verify 报「有价位就是 BUY 的意图」",
          any("必须为 null" in x for x in verify_verdict(
              dict(v4, fields=dict(v4["fields"], target=12.0)))))
        T("C7 verify 不做修正：问题清单只报不改（输入 verdict 未被改动）",
          (lambda d: (verify_verdict(d), d["action"] == "BUY"))(dict(v))[1])
        T("C8 BUY 带 next_trigger → 被拒", any(
            "不得带 next_trigger" in x for x in verify_verdict(
                dict(v, next_trigger={"text": "x", "rule": None}))))

        print("== D. 落盘：账本一行（唯一写入口 = decision.write_decision） ==")
        led = os.path.join(tmp, "gen")
        pool = os.path.join(tmp, "candidates.json")
        tpath = os.path.join(tmp, "thesis.md")
        with io.open(pool, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"version": CAND.SCHEMA_VERSION, "updated_at": "2026-10-08",
                       "candidates": []}, f)
        TH.init_file(tpath)
        th = TH.create_thesis("2026-10-08", "A", "驱动链测试：龙头拥挤度突破",
                              code="sh600000", path=tpath)
        T("D0 用**真** Thesis 对象做定格（thesis_state 真源 = thesis.FIELD_ORDER）",
          th["id"] == "TH-20261008-001")
        env = {"leg": "EOD", "source": "UNAVAILABLE"}
        r = record_decision(v, sim=False, base=led, thesis_state=th, env=env,
                            thesis_path=tpath)
        rows = DEC.iter_decisions(False, base=led)
        T("D1 BUY 落一行 DECISION（LIVE 轴）", len(rows) == 1 and rows[0]["action"] == "BUY"
          and r["axis"] == "LIVE")
        T("D2 落盘行的价线五字段与 verdict 逐个相同",
          all(rows[0][k] == v["fields"][k] for k in LEDGER_FIELDS))
        T("D3 orders_created 恒 0（C4 不执行交易）",
          r["orders_created"] == 0 and v["orders_created"] == 0)
        r_wait = record_decision(v3, sim=True, base=led, thesis_state=th, env=env,
                                 thesis_path=tpath)
        wrows = DEC.iter_decisions(True, base=led)
        T("D4 WAIT 也记进账本（施工「全写 decision_ledger」= 三动作都留痕）",
          len(wrows) == 1 and wrows[0]["action"] == "WAIT")
        T("D5 WAIT 行五字段全 null、position_request=0（0.3 硬校验同款判据）",
          all(wrows[0][k] is None for k in ("entry_limit", "stop_line", "target", "window"))
          and wrows[0]["position_request"] == 0)
        T("D6 SIM 与 LIVE 物理分轴（两个文件，轴前缀不同）",
          r_wait["axis"] == "SIM" and r_wait["ledger"] != r["ledger"]
          and r_wait["decision_id"].startswith("SIM-DEC-"))
        T("D7 重复提交同一条 → EXISTS 幂等，不重复记账",
          record_decision(v, sim=False, base=led, thesis_state=th, env=env,
                          thesis_path=tpath)["reused"] is True
          and len(DEC.iter_decisions(False, base=led)) == 1)
        T("D8 本模块生产区零文件写点（落盘全部经上游接口）", not _prod_write_calls())

        print("== E. 观察锚：WAIT/NO 的出路回填 0.5 既存字段 ==")
        CAND.upsert_candidate("sh600000", "2026-10-07", "S3 高潮识别",
                              "龙头拥挤度 > 阈值", path=pool)
        a1 = anchor_pool(v3, pool_path=pool)
        T("E1 WAIT → 回填候选池既存字段 next_trigger（无新字段）",
          a1["anchored"] and a1["next_trigger"]["text"] == "出中报确认订单")
        a2 = anchor_pool(v, pool_path=pool)
        T("E2 BUY → 不落锚（候选使命完成）", a2["anchored"] is False)
        a3 = anchor_pool(dict(v4, code="sz000001"), pool_path=pool)
        T("E3 池中无此 code → NO_CANDIDATE，三问不代建候选", a3["reason"] == "NO_CANDIDATE")
        T("E4 锚点确实写进了候选池文件（读回可见）",
          CAND.list_candidates(code="sh600000", path=pool)[0]["next_trigger"]["text"]
          == "出中报确认订单")
        T("E5 字段名就是 0.5 的 next_trigger（不是本模块的第二个叫法）",
          "next_trigger" in CAND.FIELDS)
        c1 = commit(v3, sim=False, base=led, pool_path=pool, thesis_state=th, env=env,
                    thesis_path=tpath)
        T("E6 commit = 账本 + 锚两段齐落（半环即不合格）",
          c1["decision"]["action"] == "WAIT" and c1["anchor"]["anchored"] is True
          and c1["orders_created"] == 0)

        print("== F. 纪律 ==")
        T("F1 无网络/子进程导入", not _bad_imports())
        T("F2 生产区无写盘/改名/删除调用（第三段 F2 同源判据）",
          not _prod_write_calls())
        T("F3 不越权成 C5：生产区无仓位上限标识符（position_cap 等）",
          not _ident_hits(_CAP_IDENTS), _ident_hits(_CAP_IDENTS))
        T("F4 不越权成 C5：生产区无下单/撤单标识符（place_order / cancel_order …）",
          not _ident_hits(_ORDER_IDENTS), _ident_hits(_ORDER_IDENTS))
        T("F4b 判据作用于代码而非文档（docstring 里的纪律声明不算违例）",
          "position" + "_cap" not in _prod_tokens()[0]
          and "position" + "_cap" in _prod_source())
        T("F5 落点审计对真库零违例（新增本模块后仍封闭）",
          L.audit()["ok"], L.audit()["violations"][:3])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：%s" % " / ".join(fails))
    return 1 if fail else 0


# ── 纪律辅助 ─────────────────────────────────────────────────────────────────
def _prod_source():
    """本文件生产区源码（剔除 `_selftest` 整段）—— 判据只作用于生产区。"""
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    lines = src.splitlines()
    import ast
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_selftest":
            for i in ASTW.line_set(n, clamp=len(lines)):
                lines[i - 1] = ""
    return "\n".join(lines)


def _bad_imports():
    import re
    return re.findall(r"^\s*(?:import|from)\s+(requests|urllib|socket|subprocess|openai)\b",
                      io.open(os.path.abspath(__file__), encoding="utf-8").read(), re.M)


def _prod_write_calls():
    """生产区不得出现写盘形态（本模块不碰文件：落盘全部经上游写接口）。"""
    import re
    src = _prod_source()
    pats = (r"open\([^\)]*['\"][wax]", r"os\.(replace|rename|remove|rmdir|makedirs|unlink)\(",
            r"shutil\.(copy|copy2|copyfile|move|rmtree)\(")
    return [p for p in pats if re.search(p, src)]


# 同义别名黑名单：**词语本身拼装构造**，避免清单自己命中自己
# （同 settle.py 里 `"cursor" + ".json"` 的做法：断言用的字面量不得出现在被断言的源码里）
_SYNONYM_WORDS = ("st" + "op", "tgt" + "_price", "ent" + "ry_price",
                  "position" + "_size", "exit" + "_price", "hold" + "_window")
# 越权标识符黑名单（C4 边界：不算仓位、不交易）—— 拼装构造，避免自指
_CAP_IDENTS = ("position" + "_cap", "position" + "_limit")
_ORDER_IDENTS = ("place" + "_order", "submit" + "_order", "cancel" + "_order",
                 "send" + "_order", "trade" + "_api")
_POWER_GRAB_IDENTS = _CAP_IDENTS + _ORDER_IDENTS


def _prod_tokens():
    """生产区的**标识符集合 + 非文档字符串集合**（注释被 AST 天然丢弃）。

    判据必须作用在「代码」上，而不是文档说明文字上 —— 文档里写「本模块不接受
    position_cap」是纪律声明，把声明本身判成违例属假判据（0.7 已犯过一次同类错）。
    """
    import ast as _ast
    tree = _ast.parse(_prod_source())
    doc_nodes = set()
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Module, _ast.FunctionDef, _ast.AsyncFunctionDef,
                             _ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], _ast.Expr)
                    and isinstance(body[0].value, _ast.Constant)
                    and isinstance(body[0].value.value, str)):
                doc_nodes.add(id(body[0].value))
    idents, strings = set(), set()
    for n in _ast.walk(tree):
        if isinstance(n, _ast.Name):
            idents.add(n.id)
        elif isinstance(n, _ast.Attribute):
            idents.add(n.attr)
        elif isinstance(n, _ast.alias):
            idents.add(n.name.split(".")[-1])
        elif (isinstance(n, _ast.Constant) and isinstance(n.value, str)
                and id(n) not in doc_nodes):
            strings.add(n.value)
    return idents, strings


def _synonym_leaks():
    """生产区不得出现价线/仓位的**同义别名**（这是协议层最容易犯的病）。

    真源字段名只有 `entry_limit` / `stop_line` / `target` / `window` / `position_request`
    （见 `LEDGER_FIELDS`）。任何「同一个意思换个叫法」都属重定义，一律拒。
    """
    idents, strings = _prod_tokens()
    return sorted({w for w in _SYNONYM_WORDS if w in idents or w in strings})


def _ident_hits(words):
    """给定词表在**生产区标识符**中的命中（文档里的说明文字不算）。"""
    idents, _s = _prod_tokens()
    return sorted({w for w in words if w in idents})


def _power_grabs():
    """生产区不得出现仓位上限 / 下单撤单类**标识符**（C4 边界：不算仓位、不交易）。"""
    return _ident_hits(_POWER_GRAB_IDENTS)



def _discipline(verbose=True):
    """纪律自检：零重定义 / 零写盘 / 零下单 / 落点封闭。"""
    checks = {
        "① 零重定义 · ACTIONS 同源": ACTIONS is DEC.ACTIONS,
        "② 零重定义 · DRIVERS 同源": DRIVERS == CAND.DRIVER_TYPES,
        "③ 零重定义 · 账本字段 ⊆ DECISION_FIELDS":
            set(LEDGER_FIELDS) <= set(DEC.DECISION_FIELDS),
        "④ 零重定义 · 无同义别名": not _synonym_leaks(),
        "⑤ 零写盘 · 生产区无文件写点": not _prod_write_calls(),
        "⑥ 零越权 · 无仓位/下单标识符": not _power_grabs(),
        "⑦ 零联外 · 无网络/子进程导入": not _bad_imports(),
        "⑧ 落点封闭 · 真库零违例": _audit_ok(),
    }
    if verbose:
        print("纪律自检 · three_q.py（1.4 三问协议层）")
        for k, v_ in checks.items():
            print("  %-34s %s" % (k, "OK" if v_ else "[!!]"))
        print("  三问 API 签名（冻结件 const:THREE_Q_API）：")
        for nm, pos, kw in THREE_Q_API:
            print("    %-16s (%s, *, %s)" % (nm, ", ".join(pos), ", ".join(kw)))
    return 0 if all(checks.values()) else 1


def _audit_ok():
    try:
        import landing as L
        return L.audit()["ok"]
    except ImportError:
        return None


def _main(argv):
    if "--selftest" in argv:
        print("three_q.py 自检（1.4 三问 · SCHEMA_VERSION=%d · 临时目录）" % SCHEMA_VERSION)
        return _selftest()
    if "--discipline" in argv:
        return _discipline()
    print("三问决策 C4（1.4）· 收敛器不是分析器：答不出不下，零模糊输出")
    print("\n  三问 → 动作：")
    print("    ① 驱动问：读 five（五件套齐？）+ driver（驱动不明？）")
    print("    ② 赔率问：(target − entry_limit) / (entry_limit − stop_line)，三件 + window 齐？")
    print("    ③ 证伪问：stop_line 写得出吗（机械价线，非心理止损）")
    print("\n  证据不足机械分流（LLM 零自由裁量）：")
    print("    driver == \"驱动不明\"        → NO（机械）")
    print("    缺 target/stop/五件套        → 有明确可验证补全事件 ? WAIT + next_trigger")
    print("                                   └ 无 → NO + 翻案条件（必填）")
    print("    三问齐过 + driver 明确        → BUY（position_request 由调用方显式给出）")
    print("\n  落点（二段式）：")
    print("    段一 决策账：decision.write_decision()  →  data/generated[|sim]/decision_ledger.jsonl")
    print("    段二 观察锚：candidates.backfill_candidate(next_trigger=…)  →  data/state/candidates.json")
    print("    （WAIT 的「等什么事件」与 NO 的「翻案条件」共用 0.5 既存字段 next_trigger，零新字段）")
    print("\n  字段名全部取自上游真源，本模块零重定义：")
    print("    action ← decision.ACTIONS %s" % (ACTIONS,))
    print("    driver ← decision.DRIVERS %s" % (DRIVERS,))
    print("    价线/仓位 ← decision.DECISION_FIELDS 子集 %s" % (LEDGER_FIELDS,))
    print("    条件结构 ← candidates {\"text\",\"rule\"} · op ∈ %s" % (COND_OPS,))
    print("\n  纪律：python tools/fs/three_q.py --discipline")
    print("  验收：python tools/fs/three_q.py --selftest")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
