#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""candidates.py · 候选池状态机（施工方案 0.5 · v1.0 · 2026-10-08 建）

用途：候选池的唯一实现——「机会生命周期」状态对象的建 / 查 / 步进。
      候选 = 发现层产出（有生命周期），**不是持仓、更不是买单**；
      过三问 + 风控才能交易，本模块不产生任何买单。

状态落点（唯一）：data/state/candidates.json
  {"version":1,"updated_at":"YYYY-MM-DD","candidates":[ {...}, ... ]}

候选字段（施工 0.5 写死 13 项，禁增删改名；改 schema 须升 SCHEMA_VERSION 并同步施工方案）：
  id / code / state / created_at / last_seen / expiry / source / trigger /
  invalid_condition / next_trigger / keep_reason / thesis_id / thesis

字段语义：
  · id              CD-YYYYMMDD-NNN（入池日 + 当日序号，本模块生成）
  · code            标的代码
  · state           DISCOVERED / WATCH / QUALIFIED / TRADED / EXPIRED
  · created_at      入池日
  · last_seen       最近一次**被扫描命中**日（由 upsert_candidate 更新，advance 不改）
  · expiry          过期日 —— 候选生命周期的唯一时间边界（本模块 compute_expiry 唯一算出）
  · source          哪路机械扫描命中（施工 1.2；必填，缺 = 不入池）
  · trigger         什么事实触发入池（施工 1.2；必填，缺 = 不入池）
  · invalid_condition 失效条件（C3 回填）
  · next_trigger    等什么事件（C3 回填）
  · keep_reason     为什么继续观察（C3 回填）
  · thesis_id       挂哪条 Thesis（4.2；一个候选可挂一条解释）
  · thesis          C3 回填的驱动链 + 演化史（下注时定格进 decision_ledger）

状态机（施工 0.5；**唯一迁移执行器 = advance_candidate**）：
  DISCOVERED --满足 trigger--> WATCH --命中 next_trigger--> QUALIFIED
             --过三问+风控--> TRADED
  任意非终态 --(expiry 到期 | invalid_condition 命中)--> EXPIRED
  · DISCOVERED = 已入池，未进观察
  · WATCH      = 进观察（触发事实已确认，等下一事件）
  · QUALIFIED  = 已具备下注资格（等 C4 三问 + C5 风控裁决）
  · TRADED     = 已下注（候选使命完成，后续由决策/持仓账本跟踪）
  · EXPIRED    = 过期或失效 —— **留档不删除**（负样本供 C6 用）

★ 时间边界唯一（施工 §Canonical 写死）：
  · 候选生命周期的时间边界**唯一由 expiry 承担**，状态迁移**唯一由 advance_candidate 决定**；
  · **禁任何模块自算「last_seen + N 日」** —— 要过期日就读本字段；
  · expiry 在入池时由 compute_expiry 唯一算出。

★ A4 口径升级（v1.4 变更 · 2026-10-13 授权施工）：**expiry 由「周末算术」改为「日历交易日」**
  · 旧口径（`weekend_only@v1`）：`created_at` 次日起、**只跳周六周日**、数第 window 个。
    跨法定休市窗口会**提前一日或多日**（09-22 + 5 → 09-29，应为 09-30）；而 `advance_candidate`
    据 `expiry` 机械判 `EXPIRED`（**终态不可逆**）⇒ 观察窗被**静默缩短**。B3 §2.2 缺陷 A4。
  · 新口径（`calendar_sessions@v2`）：委托 `dayclass.Calendar.add_sessions`（**交易日历唯一实现**）；
    本模块**不自写历法、不按周末近似、不联网刷新**。日历落点 = `data/raw/meta/trade_cal.csv`。
  · **日历覆盖内** → `calendar_sessions@v2`；**早于覆盖**（无日历的历史段）→ 维持 `weekend_only@v1`
    并在报告层**显式标注**（**非静默降级**，B3 §2.4.3b/RV-4）；
    **晚于覆盖**（日历未发布，如 2027）/ 日历缺失 / 损坏 / 空 → **拒绝产出 expiry**（fail-closed，
    绝不按周末推算兜底，B3 §2.4.4/RV-3）。
  · 新旧口径**不得共存于同一 SCHEMA_VERSION** ⇒ 本批 `SCHEMA_VERSION` 1 → 2；
    存量迁移走 `tools/migrate/expiry_rule_v2.py`（M0–M4 + RB-1…RB-4），**禁就地改口径**。

★ 条件求值分工（防机械件猜语义）：
  trigger / next_trigger / invalid_condition 三个字段的值形如
      {"text": "人话描述", "rule": null | {"field":..,"op":..,"value":..}}
  · 本模块**只校验结构、不自行求值、更不取数**：取数是 loader 的职责，语义是 LLM 的判断；
  · 调用方（C3 / 扫描器 / 上游能力）按 rule 或按语义求值后，把布尔结论通过 signals 传给
    advance_candidate —— **机械件只做「拿结论 → 迁移状态 → 校验合法性」**；
  · 无 signals 的候选只做 expiry 过期判，其余一律不动（绝不猜、不用 LLM 补判）。

★ 零买单铁律（0.5 验收判据）：本模块无任何下单/成交接口；advance_candidate 只推进观察态，
  返回报告里 orders_created 恒为 0 —— 步进永不产生新买单。

★ 一步一档（保守可审）：一次 advance 内每条候选**最多推进一档**；同日多信号命中时，
  已推进一档后剩余的未消费信号记入报告的 deferred，下次步进再处理（每档都留痕可追溯）。

被谁调用：tools/discovery/scan.py（1.2 机械扫描 · 入池）、C3 理解链（回填 thesis /
          next_trigger / keep_reason / invalid_condition）、tools/fs/wakeup.py（0.14 步进）。

用法：
  python tools/fs/candidates.py                # 只读查看候选池
  python tools/fs/candidates.py --selftest     # 内置验收（临时文件，不碰真数据）
  from tools.fs.candidates import upsert_candidate, advance_candidate, list_candidates

规则：禁在别处内联候选池读写；禁硬编码路径；schema 变更必须升 version 并同步施工方案 0.5。
"""
import io
import json
import os
import sys
import datetime as _dt

sys.path.insert(0, os.path.normpath(os.path.dirname(os.path.abspath(__file__))))
from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）

import dayclass as DC  # noqa: E402   ← 交易日历**唯一实现**；本模块禁自写历法

SCHEMA_VERSION = 2   # v2 = A4 口径升级（expiry_rule: calendar_sessions@v2）；v1 为周末算术，禁共存
POOL_PATH = os.path.join(ROOT, "data", "state", "candidates.json")

# ── 状态枚举（施工 0.5；改枚举 = 改契约，须升 version） ─────────────────────────
STATE_DISCOVERED = "DISCOVERED"
STATE_WATCH = "WATCH"
STATE_QUALIFIED = "QUALIFIED"
STATE_TRADED = "TRADED"
STATE_EXPIRED = "EXPIRED"
STATES = (STATE_DISCOVERED, STATE_WATCH, STATE_QUALIFIED, STATE_TRADED, STATE_EXPIRED)
OPEN_STATES = (STATE_DISCOVERED, STATE_WATCH, STATE_QUALIFIED)
TERMINAL_STATES = (STATE_TRADED, STATE_EXPIRED)

# 合法迁移表（唯一真源 = 施工 0.5 状态机；表外的迁移一律拒）
LEGAL_TRANSITIONS = frozenset({
    (STATE_DISCOVERED, STATE_WATCH),
    (STATE_WATCH, STATE_QUALIFIED),
    (STATE_QUALIFIED, STATE_TRADED),
})

# 驱动类型枚举（施工 1.4 读 thesis.driver_type 判「驱动不明」）
DRIVER_TYPES = ("A", "B", "C", "复合", "驱动不明")

# 候选字段（施工 0.5 写死 13 项）
FIELDS = ("id", "code", "state", "created_at", "last_seen", "expiry", "source",
          "trigger", "invalid_condition", "next_trigger", "keep_reason",
          "thesis_id", "thesis")

# 结构化判据允许的比较符
OPS = (">=", ">", "<=", "<", "==", "!=", "in", "not_in")

# 观察窗（交易日）—— **真源 = params.json 的 `candidate_watch_window_days`（0.8/F5）**；
# 未落盘 / 读不到时用本种子兜底并显式标 BUILTIN_DEFAULT（口径依据 = 施工 1.2「默认 5 交易日」）。
# ⚠ 术语碰撞提醒：本参数是**候选池过期**观察窗；设计 §5.6 纠错环的「观察窗 3 日」是另一个参数
#   （`correction_observe_days`），两者**不可混用**，故参数名必须全称、不可简写为 "观察窗"。
DEFAULT_WATCH_WINDOW = 5
PARAMS_PATH = os.path.join(ROOT, "data", "params.json")


def _watch_window(window=None, params_path=None):
    """观察窗交易日数 + 来源：显式传入 > params.json > 内置种子（绝不猜、来源标明）。"""
    if window is not None:
        return window, "EXPLICIT"
    p = PARAMS_PATH if params_path is None else params_path
    try:
        with io.open(p, encoding="utf-8-sig") as f:
            d = json.load(f)
        v = ((d.get("params") or {}).get("candidate_watch_window_days") or {}).get("value")
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v, "PARAMS"
    except Exception:
        pass                                        # 文件不存在 / 损坏 → 走种子兜底（来源已标）
    return DEFAULT_WATCH_WINDOW, "BUILTIN_DEFAULT"

# 迁移原因码
R_TRIGGER = "TRIGGER"
R_NEXT_TRIGGER = "NEXT_TRIGGER"
R_QUALIFIED = "QUALIFIED"
R_EXPIRY = "EXPIRY"
R_INVALID = "INVALID"


class CandidateError(Exception):
    """候选池操作被拒（非法状态 / 非法迁移 / 必备字段缺失 / 文件损坏）——显式抛错，不静默降级。"""


# ── 日期工具 ──────────────────────────────────────────────────────────────────
def _check_date(date):
    """日期硬校验（YYYY-MM-DD），防把时间戳或中文日期写进候选池。返回规范化字符串。"""
    s = date.isoformat() if hasattr(date, "isoformat") else str(date)
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise CandidateError("日期必须是 YYYY-MM-DD，收到: %r" % (date,))
    try:
        _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        raise CandidateError("日期非法: %r" % (date,))
    return s


def _parse(s):
    s = _check_date(s)
    return _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))


def _is_weekend(d):
    return d.weekday() >= 5


def _add_trading_days(start, n):
    """**旧口径 `weekend_only@v1`**：从 start **次日起**数第 n 个「非周末日」（只跳周末）。

    ⚠ A4 v2 起**生产路径不再走本函数**（`compute_expiry` 已改由日历驱动）。保留它的两个用途：
      ① 日历覆盖**之外的历史段**降级（调用方须**显式标注** rule，非静默）；
      ② `tools/migrate/expiry_rule_v2.py` 与 `tools/e2e/expiry_rule_diff.py` 的**新旧对照**。
    """
    if not isinstance(n, int) or n < 1:
        raise CandidateError("交易日窗必须 >= 1，收到: %r" % (n,))
    d = _parse(start)
    c = 0
    while c < n:
        d = d + _dt.timedelta(days=1)
        if not _is_weekend(d):
            c += 1
    return d.isoformat()


# ── 交易日历（A4 · v2）：读取接口唯一，委托 dayclass；本模块不自写历法 ──────────
RULE_CALENDAR = "calendar_sessions@v2"   # 新口径：日历交易日
RULE_WEEKEND = "weekend_only@v1"         # 旧口径：只跳周末（仅历史无日历段）
RULE_UNKNOWN = "CALENDAR_UNKNOWN"        # 日历未定 ⇒ 拒绝产出（fail-closed）

# 日历落点（与 0.14 wakeup 同源同落点：投放区同根下的只读元数据）
CALENDAR_PATH = os.path.join(ROOT, "data", "raw", "meta", "trade_cal.csv")


def load_calendar(path=None):
    """交易日历**唯一读取接口**（只读；委托 `dayclass.Calendar.from_seed_csv`）。

    缺失 / 不可读 / 损坏 / 空 ⇒ `CandidateError`（**fail-closed**：不按周末推算、不联网刷新）。
    """
    p = CALENDAR_PATH if path is None else path
    if not os.path.isfile(p):
        raise CandidateError("交易日历缺失：%s —— 拒绝产出 expiry"
                             "（不按周末推算、不自动联网刷新）" % p)
    try:
        cal = DC.Calendar.from_seed_csv(p, source=os.path.basename(p), version="seed")
    except DC.DayError as e:
        raise CandidateError("交易日历不可用（%s）：%s" % (type(e).__name__, e))
    except OSError as e:
        raise CandidateError("交易日历不可读：%s" % e)
    if not len(cal):
        raise CandidateError("交易日历为空（无任何交易日）：%s（不按周末推算）" % p)
    return cal


def expiry_rule_of(created_at, calendar=None, calendar_path=None):
    """`created_at` 适用的 expiry 口径标签（供**报告层 / 迁移**渲染；不改候选落盘字段）。

    `calendar_sessions@v2` = 日历覆盖内 · `weekend_only@v1` = 早于覆盖的历史段 ·
    `CALENDAR_UNKNOWN` = 晚于覆盖（日历未发布）或空日历。
    """
    d = _check_date(created_at)
    cal = calendar if calendar is not None else load_calendar(calendar_path)
    if len(cal) and cal.covers(d):
        return RULE_CALENDAR
    lo, _hi = cal.span
    if lo is not None and d < lo:
        return RULE_WEEKEND
    return RULE_UNKNOWN


def compute_expiry(created_at, window=None, params_path=None, calendar=None,
                   calendar_path=None):
    """入池时**唯一**计算过期日（施工 §Canonical：expiry 是候选生命周期的唯一时间边界）。

    口径（A4 v2 · 2026-10-13 授权升级）：`created_at` **次日起**数第 window 个**日历交易日**
    （委托 `dayclass.Calendar.add_sessions`——交易日历唯一实现；本模块不自写历法）。
      · **日历覆盖内** → 日历交易日（`calendar_sessions@v2`）；
      · **早于覆盖**（无日历的历史段）→ 旧口径 `weekend_only@v1`（报告层须**显式标注**，非静默）；
      · **晚于覆盖**（日历未发布）/ 日历缺失 / 损坏 / 空 / 剩余交易日不足 → **抛错拒绝产出**
        （**fail-closed**：绝不按周末近似兜底、绝不外推）。
    禁任何模块自算「last_seen + N 日」——要过期日读本函数写入的 expiry 字段。
    window 的真源 = params.json `candidate_watch_window_days`（F5 起生效）；未落盘用种子兜底。
    """
    w, _src = _watch_window(window, params_path)
    if not isinstance(w, int) or isinstance(w, bool) or w < 1:
        raise CandidateError("交易日窗必须 >= 1，收到: %r" % (w,))
    d = _check_date(created_at)
    cal = calendar if calendar is not None else load_calendar(calendar_path)
    if not len(cal):
        raise CandidateError("交易日历为空 ⇒ 拒绝产出 expiry（不按周末推算）")
    if cal.covers(d):
        out = cal.add_sessions(d, w)
        if out is None:
            raise CandidateError("交易日历覆盖不足：%s 起第 %d 个交易日越出日历跨度 %s "
                                 "⇒ 拒绝产出 expiry（越界不外推、不按周末兜底）"
                                 % (d, w, cal.span))
        return out
    lo, _hi = cal.span
    if lo is not None and d < lo:
        return _add_trading_days(d, w)          # 历史无日历段：旧口径（须显式标注 rule）
    raise CandidateError("交易日历未定：%s 晚于日历跨度 %s ⇒ 拒绝产出 expiry"
                         "（日历未发布，不按周末推算、不外推）" % (d, cal.span))


# ── 条件结构校验（只校结构，不求值） ──────────────────────────────────────────
def _norm_cond(cond, name, required):
    """把条件字段规范成 {'text':..,'rule':..}。None 且非必填 → 返回 None（未回填）。"""
    if cond is None:
        if required:
            raise CandidateError("%s 必填（施工 1.2：缺 = 不入池）" % name)
        return None
    if isinstance(cond, str):                      # 简写：只给一句人话
        cond = {"text": cond, "rule": None}
    if not isinstance(cond, dict) or set(cond.keys()) != {"text", "rule"}:
        raise CandidateError("%s 必须是 {'text':..,'rule':..} 结构，实际: %r" % (name, cond))
    if not isinstance(cond["text"], str) or not cond["text"].strip():
        raise CandidateError("%s.text 必须是非空字符串（否则候选无法解释为什么被观察/失效）" % name)
    r = cond["rule"]
    if r is not None:
        if not isinstance(r, dict) or set(r.keys()) != {"field", "op", "value"}:
            raise CandidateError("%s.rule 必须是 null 或 {'field','op','value'}，实际: %r" % (name, r))
        if not isinstance(r["field"], str) or not r["field"].strip():
            raise CandidateError("%s.rule.field 必须是非空字符串" % name)
        if r["op"] not in OPS:
            raise CandidateError("%s.rule.op 只能是 %s，实际: %r" % (name, " / ".join(OPS), r["op"]))
    return {"text": cond["text"], "rule": r}


def _norm_thesis(t):
    """thesis 字段：null 或 {'driver_type':.., 'history':[条目...]}（演化史 append-only）。"""
    if t is None:
        return None
    if not isinstance(t, dict) or set(t.keys()) != {"driver_type", "history"}:
        raise CandidateError("thesis 必须是 null 或 {'driver_type','history'}，实际: %r" % (t,))
    if t["driver_type"] is not None and t["driver_type"] not in DRIVER_TYPES:
        raise CandidateError("thesis.driver_type 只能是 %s，实际: %r"
                             % (" / ".join(DRIVER_TYPES), t["driver_type"]))
    h = t["history"]
    if not isinstance(h, list):
        raise CandidateError("thesis.history 必须是列表（append-only）")
    for e in h:
        _norm_history_entry(e)
    return {"driver_type": t["driver_type"], "history": [dict(e) for e in h]}


def _norm_history_entry(e):
    if not isinstance(e, dict) or set(e.keys()) != {"date", "kind", "text", "evidence"}:
        raise CandidateError("history 条目必须是 {'date','kind','text','evidence'}，实际: %r" % (e,))
    _check_date(e["date"])
    if not isinstance(e["kind"], str) or not e["kind"].strip():
        raise CandidateError("history.kind 必须是非空字符串")
    if not isinstance(e["text"], str) or not e["text"].strip():
        raise CandidateError("history.text 必须是非空字符串")
    if not isinstance(e["evidence"], list):
        raise CandidateError("history.evidence 必须是列表（可为空）")
    return e


# ── 读写 ─────────────────────────────────────────────────────────────────────
def _validate(d):
    """schema 强校验：版本、顶层字段、候选字段集合、状态枚举、日期、条件结构全写死。"""
    if not isinstance(d, dict):
        raise CandidateError("candidates.json 顶层必须是对象，实际: %r" % type(d).__name__)
    if set(d.keys()) != {"version", "updated_at", "candidates"}:
        raise CandidateError("顶层字段必须且只能是 version / updated_at / candidates，实际: %r"
                             % sorted(d.keys()))
    if d.get("version") != SCHEMA_VERSION:
        raise CandidateError("candidates.json version 不匹配（期望 %s，实际 %r）——"
                             "schema 变更须同步施工方案 0.5" % (SCHEMA_VERSION, d.get("version")))
    _check_date(d.get("updated_at"))
    cs = d.get("candidates")
    if not isinstance(cs, list):
        raise CandidateError("candidates 必须是列表，实际: %r" % type(cs).__name__)
    seen = set()
    for c in cs:
        if not isinstance(c, dict):
            raise CandidateError("候选条目必须是对象，实际: %r" % type(c).__name__)
        if set(c.keys()) != set(FIELDS):
            raise CandidateError("候选字段必须且只能是 %s，实际: %r"
                                 % (" / ".join(FIELDS), sorted(c.keys())))
        if c["state"] not in STATES:
            raise CandidateError("候选 state 只能是 %s，实际: %r" % (" / ".join(STATES), c["state"]))
        if not isinstance(c["id"], str) or not c["id"].strip():
            raise CandidateError("候选 id 必须是非空字符串")
        if c["id"] in seen:
            raise CandidateError("候选 id 重复: %s" % c["id"])
        seen.add(c["id"])
        if not isinstance(c["code"], str) or not c["code"].strip():
            raise CandidateError("候选 code 必须是非空字符串")
        if not isinstance(c["source"], str) or not c["source"].strip():
            raise CandidateError("候选 source 必填（哪路扫描命中）")
        for k in ("created_at", "last_seen", "expiry"):
            _check_date(c[k])
        if c["expiry"] <= c["created_at"]:
            raise CandidateError("候选 %s 的 expiry(%s) 必须晚于 created_at(%s)"
                                 % (c["id"], c["expiry"], c["created_at"]))
        _norm_cond(c["trigger"], "trigger", True)
        for k in ("invalid_condition", "next_trigger"):
            _norm_cond(c[k], k, False)
        _norm_thesis(c["thesis"])
        for k in ("keep_reason", "thesis_id"):
            if c[k] is not None and (not isinstance(c[k], str) or not c[k].strip()):
                raise CandidateError("候选 %s 的 %s 必须是非空字符串或 null" % (c["id"], k))
    return d


def _load(path=None):
    """读候选池（不写盘）。文件缺失/损坏一律抛错，绝不静默造一份（防候选池凭空复位）。"""
    p = path or POOL_PATH
    if not os.path.exists(p):
        raise CandidateError("候选池文件不存在：%s —— 需先按施工方案 0.5 初始化（不得静默新建）" % p)
    with io.open(p, encoding="utf-8") as f:
        return _validate(json.load(f))


def _save(d, path=None):
    """原子写：先写 .tmp 再 os.replace（中途崩溃不留半截文件）。"""
    p = path or POOL_PATH
    _WG.check_write(p)
    _validate(d)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def _new_id(items, as_of):
    """候选 id = CD-YYYYMMDD-NNN（入池日 + 当日序号），同日多候选自动递增序号。"""
    prefix = "CD-" + as_of.replace("-", "")
    taken = {c["id"] for c in items}
    n = 1
    while ("%s-%03d" % (prefix, n)) in taken:
        n += 1
    return "%s-%03d" % (prefix, n)


# ── 公开接口 ─────────────────────────────────────────────────────────────────
def list_candidates(state=None, code=None, path=None):
    """按条件列候选（state / code 过滤；None = 不过滤）。"""
    if state is not None and state not in STATES:
        raise CandidateError("未知 state %r，合法值：%s" % (state, " / ".join(STATES)))
    cs = _load(path)["candidates"]
    out = [c for c in cs
           if (state is None or c["state"] == state) and (code is None or c["code"] == code)]
    return sorted(out, key=lambda c: (c["created_at"], c["id"]))


def get_candidate(cid, path=None):
    """按 id 取候选；不在池中返回 None（不抛错）。"""
    for c in _load(path)["candidates"]:
        if c["id"] == cid:
            return c
    return None


def upsert_candidate(code, as_of, source, trigger, invalid_condition=None, next_trigger=None,
                     keep_reason=None, thesis_id=None, thesis=None, window=None,
                     created_at=None, path=None, calendar=None, calendar_path=None):
    """入池 / 去重（施工 1.2「入池全机械，LLM 不参与」）。

    规则（施工 1.2 写死）：
      · source + trigger **必带** —— 每条候选必答「哪路扫描命中 / 什么事实触发」，缺 = 不入池；
      · 同 code **已有非终态候选** → 只更新 last_seen（不重复建、不动其余字段）；
      · 同 code 只有终态候选（EXPIRED/TRADED） → 视为新一轮机会，新建一条；
      · expiry 由本模块 compute_expiry 唯一算出（入池时定，此后只读）。

    `calendar` / `calendar_path`（A4 v2）：注入交易日历；都不给则读默认落点
    `data/raw/meta/trade_cal.csv`。日历不可用 ⇒ 抛 `CandidateError`（缺 = 不入池）。

    返回 {"created": bool, "reason": str, "candidate": {...}}。
    """
    as_of = _check_date(as_of)
    ca = _check_date(created_at) if created_at else as_of
    if ca > as_of:
        raise CandidateError("created_at(%s) 不得晚于 as_of(%s)" % (ca, as_of))
    if not isinstance(code, str) or not code.strip():
        raise CandidateError("code 必填（非空字符串）")
    if not isinstance(source, str) or not source.strip():
        raise CandidateError("source 必填（施工 1.2：哪路扫描命中）—— 缺 = 不入池")
    tr = _norm_cond(trigger, "trigger", True)
    ic = _norm_cond(invalid_condition, "invalid_condition", False)
    nt = _norm_cond(next_trigger, "next_trigger", False)
    exp = compute_expiry(ca, window, calendar=calendar, calendar_path=calendar_path)

    d = _load(path)
    for c in d["candidates"]:
        if c["code"] == code and c["state"] in OPEN_STATES:
            c["last_seen"] = as_of                     # 去重：只更新 last_seen
            d["updated_at"] = as_of
            _save(d, path)
            return {"created": False, "reason": "DUPLICATE_OPEN", "candidate": c}

    cand = {
        "id": _new_id(d["candidates"], as_of),
        "code": code,
        "state": STATE_DISCOVERED,
        "created_at": ca,
        "last_seen": as_of,
        "expiry": exp,
        "source": source,
        "trigger": tr,
        "invalid_condition": ic,
        "next_trigger": nt,
        "keep_reason": keep_reason,
        "thesis_id": thesis_id,
        "thesis": _norm_thesis(thesis),
    }
    d["candidates"].append(cand)
    d["updated_at"] = as_of
    _save(d, path)
    return {"created": True, "reason": "CREATED", "candidate": cand}


def backfill_candidate(cid, as_of, driver_type=None, history_entry=None,
                       invalid_condition=None, next_trigger=None, keep_reason=None,
                       thesis_id=None, path=None):
    """C3 理解链回填（施工 1.3：驱动链 / 等什么事件 / 为什么继续观察 / 失效条件）。

    ★ 演化史 append-only：history_entry 只**追加**，本函数**不提供任何改写已存在条目的入口**。
    返回回填后的候选；候选不存在或已终态 → 抛错（终态候选不再回填）。
    """
    as_of = _check_date(as_of)
    if driver_type is not None and driver_type not in DRIVER_TYPES:
        raise CandidateError("driver_type 只能是 %s，实际: %r" % (" / ".join(DRIVER_TYPES), driver_type))
    if history_entry is not None:
        _norm_history_entry(history_entry)

    d = _load(path)
    for c in d["candidates"]:
        if c["id"] != cid:
            continue
        if c["state"] in TERMINAL_STATES:
            raise CandidateError("候选 %s 已终态（%s），不得回填" % (cid, c["state"]))
        if driver_type is not None:
            if c["thesis"] is None:
                c["thesis"] = {"driver_type": None, "history": []}
            c["thesis"]["driver_type"] = driver_type
        if history_entry is not None:
            if c["thesis"] is None:
                c["thesis"] = {"driver_type": None, "history": []}
            c["thesis"]["history"].append(dict(history_entry))     # 只追加
        if invalid_condition is not None:
            c["invalid_condition"] = _norm_cond(invalid_condition, "invalid_condition", True)
        if next_trigger is not None:
            c["next_trigger"] = _norm_cond(next_trigger, "next_trigger", True)
        if keep_reason is not None:
            c["keep_reason"] = keep_reason
        if thesis_id is not None:
            c["thesis_id"] = thesis_id
        c["last_seen"] = as_of
        d["updated_at"] = as_of
        _save(d, path)
        return c
    raise CandidateError("候选不存在: %s" % cid)


def advance_candidate(as_of, signals=None, path=None):
    """候选池步进（施工 0.5：**状态迁移唯一执行器**）。

    参数：
      as_of   —— 步进日（YYYY-MM-DD），恒第 1 位
      signals —— 外部求值结论，形如
                 {"<候选 id 或 code>": {"trigger":bool,"next_trigger":bool,
                                        "invalid":bool,"qualified":bool}}
                 未给的候选只做 expiry 过期判（不猜、不推进）。

    步进规则（对每条非终态候选，按序取第一条命中）：
      1. as_of > expiry            → EXPIRED（EXPIRY）—— 唯一时间边界，无条件强制
      2. signals.invalid 为真       → EXPIRED（INVALID）
      3. DISCOVERED 且 trigger      → WATCH（TRIGGER）
      4. WATCH 且 next_trigger      → QUALIFIED（NEXT_TRIGGER）
      5. QUALIFIED 且 qualified     → TRADED（QUALIFIED）
    ★ 一步一档：本条已推进一档后，同日剩余未消费信号记入 deferred，下次步进再处理。
    ★ 零买单：本函数**只推进观察态**，不产生任何买单。

    返回报告 {"as_of","checked","transitions","deferred","orders_created"}。
    """
    as_of = _check_date(as_of)
    sig = signals or {}
    if not isinstance(sig, dict):
        raise CandidateError("signals 必须是 dict，实际: %r" % type(sig).__name__)

    d = _load(path)
    transitions, deferred = [], []
    checked = 0
    for c in d["candidates"]:
        if c["state"] in TERMINAL_STATES:
            continue
        checked += 1
        s = sig.get(c["id"], sig.get(c["code"], {})) or {}
        if not isinstance(s, dict):
            raise CandidateError("signals[%s] 必须是 dict" % c["id"])

        from_state = c["state"]
        to_state = reason = None
        if as_of > c["expiry"]:
            to_state, reason = STATE_EXPIRED, R_EXPIRY          # 时间边界优先，无条件
        elif s.get("invalid"):
            to_state, reason = STATE_EXPIRED, R_INVALID
        elif from_state == STATE_DISCOVERED and s.get("trigger"):
            to_state, reason = STATE_WATCH, R_TRIGGER
        elif from_state == STATE_WATCH and s.get("next_trigger"):
            to_state, reason = STATE_QUALIFIED, R_NEXT_TRIGGER
        elif from_state == STATE_QUALIFIED and s.get("qualified"):
            to_state, reason = STATE_TRADED, R_QUALIFIED

        if to_state is None:
            continue
        if (from_state, to_state) not in LEGAL_TRANSITIONS and to_state != STATE_EXPIRED:
            raise CandidateError("非法迁移 %s → %s（候选 %s）—— 迁移只能走施工 0.5 状态机"
                                 % (from_state, to_state, c["id"]))
        c["state"] = to_state
        transitions.append({"id": c["id"], "code": c["code"], "from": from_state,
                            "to": to_state, "reason": reason})
        rest = [k for k in ("trigger", "next_trigger", "invalid", "qualified")
                if s.get(k) and not (to_state == STATE_EXPIRED and k == "invalid")]
        if rest:
            deferred.append({"id": c["id"], "state": to_state, "signals": rest,
                             "reason": "ONE_STEP_PER_DAY"})

    d["updated_at"] = as_of
    _save(d, path)
    return {"as_of": as_of, "checked": checked, "transitions": transitions,
            "deferred": deferred, "orders_created": 0}


# ── 自检（--selftest）：全部在临时文件跑，不碰真实数据 ─────────────────────────
def _selftest():
    import tempfile
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
        except CandidateError:
            return True

    def _fp(p):
        """真文件指纹（不存在 → None）—— 用于验证自检零污染。"""
        if not os.path.exists(p):
            return None
        import hashlib
        with io.open(p, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()

    real_fp = _fp(POOL_PATH)
    tmp = tempfile.mkdtemp(prefix="ta_candidates_selftest_")
    pool = os.path.join(tmp, "candidates.json")
    D1, D2 = "2026-10-08", "2026-10-09"          # 10-08 周四 / 10-09 周五
    empty = {"version": SCHEMA_VERSION, "updated_at": D1, "candidates": []}
    with io.open(pool, "w", encoding="utf-8", newline="\n") as f:
        json.dump(empty, f, ensure_ascii=False, indent=2)
    TRIG = "五路扫描命中：涨停池 + 板块资金同步流入"
    try:
        print("== A. schema 与初始态 ==")
        d = _load(pool)
        T("A1 初始池通过 schema 校验", d["version"] == SCHEMA_VERSION)
        T("A2 字段集合恰 13 项", len(FIELDS) == 13)
        T("A3 顶层字段 = version/updated_at/candidates",
          set(d.keys()) == {"version", "updated_at", "candidates"})
        T("A4 空池 candidates == []", d["candidates"] == [])

        print("== B. 入池与去重（施工 1.2 入池全机械） ==")
        r1 = upsert_candidate("sh600000", D1, "ZT_POOL", TRIG, path=pool)
        T("B1 首次入池 created=True", r1["created"] is True)
        T("B2 入池即 DISCOVERED", r1["candidate"]["state"] == STATE_DISCOVERED)
        T("B3 created_at = last_seen = 入池日",
          r1["candidate"]["created_at"] == D1 and r1["candidate"]["last_seen"] == D1)
        r2 = upsert_candidate("sh600000", D2, "ZT_POOL", TRIG, path=pool)
        T("B4 同 code 非终态去重（created=False）", r2["created"] is False)
        T("B5 去重只更新 last_seen、不新增条目",
          r2["candidate"]["last_seen"] == D2 and len(_load(pool)["candidates"]) == 1)
        T("B6 缺 source → 拒（缺=不入池）",
          raises(upsert_candidate, "sh600001", D1, "", TRIG, path=pool))
        T("B7 缺 trigger → 拒（缺=不入池）",
          raises(upsert_candidate, "sh600001", D1, "ZT_POOL", None, path=pool))
        T("B8 简写 trigger（只给一句人话）被规范化",
          _load(pool)["candidates"][0]["trigger"] == {"text": TRIG, "rule": None})

        print("== C. expiry 唯一来源（施工 §Canonical） ==")
        c0 = _load(pool)["candidates"][0]
        T("C1 expiry = compute_expiry(created_at, 默认窗)",
          c0["expiry"] == compute_expiry(D1, DEFAULT_WATCH_WINDOW))
        T("C2 expiry 晚于 created_at", c0["expiry"] > c0["created_at"])
        T("C3 expiry 非周末日", not _is_weekend(_parse(c0["expiry"])))
        # 独立实现互验：从次日数 5 个工作日（手工口径）
        n, cur = 0, _parse(D1)
        while n < 5:
            cur = cur + _dt.timedelta(days=1)
            if cur.weekday() < 5:
                n += 1
        T("C4 与独立实现互验一致", c0["expiry"] == cur.isoformat())
        T("C5 window=1 → 次一个工作日",
          compute_expiry("2026-10-08", 1) == "2026-10-09")
        T("C6 window=0 → 拒", raises(compute_expiry, "2026-10-08", 0))
        tp = os.path.join(tmp, "params_probe.json")
        T("C7 params 未落盘 → 种子兜底并标 BUILTIN_DEFAULT",
          _watch_window(None, tp) == (DEFAULT_WATCH_WINDOW, "BUILTIN_DEFAULT"))
        with io.open(tp, "w", encoding="utf-8") as f:
            json.dump({"params": {"candidate_watch_window_days": {"value": 7}}}, f)
        T("C8 params 落地后自动取 params 值（7）并标 PARAMS",
          _watch_window(None, tp) == (7, "PARAMS"))
        T("C9 显式 window 优先于 params（EXPLICIT，自检/J 回放可强制基线）",
          _watch_window(1, tp) == (1, "EXPLICIT"))
        T("C10 换 params 值 → 过期日随之变（消费方代码零改动）",
          compute_expiry("2026-10-08", None, tp) != compute_expiry("2026-10-08", None,
                                                                  os.path.join(tmp, "no.json")))
        T("C11 跨周末正确（周五入池 → 落下一周）",
          compute_expiry("2026-10-09", 1) == "2026-10-12")

        print("== J. A4 日历口径（v2 · expiry_rule · fail-closed） ==")
        try:
            CAL = load_calendar()
            _cal_err = None
        except CandidateError as _e:
            CAL, _cal_err = None, str(_e)
            print("  [!!] 交易日历不可用 → A4 组无法验证：%s" % _cal_err)
        T("J1 默认落点交易日历可载入且非空%s" % ("" if CAL is not None else "（%s）" % _cal_err),
          CAL is not None and len(CAL) > 0)
        if CAL is not None:
            def _n_sess(a, b):
                """(a, b] 内交易日个数（独立计数，不用 compute_expiry）。"""
                return len([s for s in CAL.sessions_between(a, b) if a < s <= b])

            # RV-1（B3 §2.4.4）：正常跨休市 09-22 + 5 → 09-30（旧口径 09-29）
            T("J2 RV-1 跨中秋休市：09-22+5 → 2026-09-30（设计值）",
              compute_expiry("2026-09-22", 5, calendar=CAL) == "2026-09-30")
            T("J3 RV-1 独立计数互验：(09-22, 09-30] 恰 5 个交易日 且 expiry 是交易日",
              _n_sess("2026-09-22", "2026-09-30") == 5 and CAL.is_session("2026-09-30"))
            T("J4 旧口径同日只到 09-29（证明新口径确实走日历，非周末近似）",
              _add_trading_days("2026-09-22", 5) == "2026-09-29")

            # RV-2（B3 §2.4.4）：跨国庆长休 09-30 + 5 → 落 10-09 之后的交易日
            _e2 = compute_expiry("2026-09-30", 5, calendar=CAL)
            T("J5 RV-2 跨国庆长休：09-30+5 → %s（须晚于 10-08）" % _e2,
              _e2 > "2026-10-08" and CAL.is_session(_e2) and _n_sess("2026-09-30", _e2) == 5)
            T("J6 RV-2 对照：旧口径同日落到 2026-10-07（**国庆休市日**——正是缺陷后果）",
              _add_trading_days("2026-09-30", 5) == "2026-10-07"
              and not CAL.is_session("2026-10-07"))

            # 口径标签（B3 §2.4.3b / RV-4）
            T("J7 覆盖内 ⇒ calendar_sessions@v2",
              expiry_rule_of("2026-09-22", calendar=CAL) == RULE_CALENDAR)
            T("J8 早于覆盖（2025 段）⇒ weekend_only@v1（旧口径，**须显式标注**，非静默）",
              expiry_rule_of("2025-06-01", calendar=CAL) == RULE_WEEKEND)
            T("J9 晚于覆盖（2027）⇒ CALENDAR_UNKNOWN",
              expiry_rule_of("2027-01-05", calendar=CAL) == RULE_UNKNOWN)
            T("J10 历史段 created_at 仍可算（旧口径），但**不冒充**日历口径",
              compute_expiry("2025-06-02", 5, calendar=CAL) == "2025-06-09"
              and expiry_rule_of("2025-06-02", calendar=CAL) == RULE_WEEKEND)
            # ★ 用**独立池**：D 组共用 pool，J11 若写进去会污染 D1「无 signals → 不动」
            pool_j = os.path.join(tmp, "candidates_j.json")
            with io.open(pool_j, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"version": SCHEMA_VERSION, "updated_at": "2026-09-22",
                           "candidates": []}, f, ensure_ascii=False, indent=2)
            T("J11 upsert 注入沙盒日历 → expiry 走日历口径",
              upsert_candidate("sh600002", "2026-09-30", "J组", TRIG,
                               created_at="2026-09-22", window=5,
                               path=pool_j, calendar=CAL)["candidate"]["expiry"]
              == "2026-09-30")

            # fail-closed 三态（RV-3）
            T("J12 RV-3 晚于覆盖 ⇒ 拒绝产出 expiry（**不按周末兜底**）",
              raises(compute_expiry, "2027-01-05", 5, None, CAL))
            T("J13 越界不足（12-25 起不足 5 个交易日）⇒ 拒绝产出",
              raises(compute_expiry, "2026-12-25", 5, None, CAL))
            T("J14 空日历 ⇒ 拒绝产出（不按周末推算）",
              raises(compute_expiry, "2026-10-08", 5, None, DC.Calendar([])))
            T("J15 空日历 ⇒ expiry_rule_of = CALENDAR_UNKNOWN",
              expiry_rule_of("2026-10-08", calendar=DC.Calendar([])) == RULE_UNKNOWN)

        # load_calendar 三态（真跑临时文件，非恒真断言）
        _c_miss = os.path.join(tmp, "no_cal.csv")
        _c_bad = os.path.join(tmp, "bad_cal.csv")
        _c_empty = os.path.join(tmp, "empty_cal.csv")
        with io.open(_c_bad, "w", encoding="utf-8", newline="\n") as f:
            f.write("foo,bar\n1,2\n")
        with io.open(_c_empty, "w", encoding="utf-8", newline="\n") as f:
            f.write("date,is_trade\n")
        T("J16 日历**缺失** ⇒ CandidateError（不自动联网刷新）",
          raises(load_calendar, _c_miss))
        T("J17 日历**损坏**（缺列）⇒ CandidateError（不静默降级）",
          raises(load_calendar, _c_bad))
        T("J18 日历**无交易日** ⇒ CandidateError",
          raises(load_calendar, _c_empty))
        T("J19 日历缺失时 compute_expiry 亦拒绝（缺 = 不入池）",
          raises(compute_expiry, "2026-10-08", 5, None, None, _c_miss))

        print("== D. 状态迁移（施工 0.5 状态机） ==")
        cid = c0["id"]
        T("D1 无 signals → 不动", advance_candidate(D1, path=pool)["transitions"] == [])
        rr = advance_candidate(D1, {cid: {"trigger": True}}, path=pool)
        T("D2 DISCOVERED --trigger--> WATCH",
          rr["transitions"][0]["to"] == STATE_WATCH and rr["transitions"][0]["reason"] == R_TRIGGER)
        T("D3 WATCH 态对 trigger 信号不误迁",
          advance_candidate(D1, {cid: {"trigger": True}}, path=pool)["transitions"] == [])
        rr = advance_candidate(D1, {cid: {"next_trigger": True}}, path=pool)
        T("D4 WATCH --next_trigger--> QUALIFIED",
          rr["transitions"][0]["to"] == STATE_QUALIFIED)
        rr = advance_candidate(D1, {cid: {"qualified": True}}, path=pool)
        T("D5 QUALIFIED --qualified--> TRADED",
          rr["transitions"][0]["to"] == STATE_TRADED)
        T("D6 TRADED 是终态，不再迁移",
          advance_candidate(D1, {cid: {"invalid": True}}, path=pool)["transitions"] == [])
        T("D7 步进后 checked 只数非终态",
          advance_candidate(D1, path=pool)["checked"] == 0)
        r_i = upsert_candidate("sz000001", D1, "DT_LIST", TRIG, path=pool)
        i_id = r_i["candidate"]["id"]
        rr = advance_candidate(D1, {i_id: {"invalid": True}}, path=pool)
        T("D8 invalid_condition 命中 → EXPIRED(INVALID)",
          rr["transitions"][0]["to"] == STATE_EXPIRED and rr["transitions"][0]["reason"] == R_INVALID)
        r_e = upsert_candidate("sz000002", D1, "HOT_SEARCH", TRIG,
                               created_at="2026-09-01", path=pool)
        e_id = r_e["candidate"]["id"]
        rr = advance_candidate(D1, {e_id: {"trigger": True}}, path=pool)
        T("D9 as_of > expiry → EXPIRED(EXPIRY)，且时间边界优先于 trigger",
          rr["transitions"][0]["to"] == STATE_EXPIRED and rr["transitions"][0]["reason"] == R_EXPIRY)
        T("D10 非法迁移被拒（表外迁移不存在）",
          (STATE_DISCOVERED, STATE_TRADED) not in LEGAL_TRANSITIONS)

        print("== E. 一步一档与报告 ==")
        r_m = upsert_candidate("sz000003", D1, "SECTOR", TRIG, path=pool)
        m_id = r_m["candidate"]["id"]
        rr = advance_candidate(D1, {m_id: {"trigger": True, "next_trigger": True}}, path=pool)
        T("E1 同日多信号只推进一档",
          len(rr["transitions"]) == 1 and rr["transitions"][0]["to"] == STATE_WATCH)
        T("E2 未消费信号记入 deferred",
          rr["deferred"] and "next_trigger" in rr["deferred"][0]["signals"])
        T("E3 报告 orders_created 恒为 0", rr["orders_created"] == 0)
        rr2 = advance_candidate(D1, {m_id: {"next_trigger": True}}, path=pool)
        T("E4 隔次步进把剩余信号补上", rr2["transitions"][0]["to"] == STATE_QUALIFIED)
        T("E5 transitions 带 from/to/reason",
          set(rr2["transitions"][0].keys()) == {"id", "code", "from", "to", "reason"})

        print("== F. C3 回填与演化史 append-only ==")
        b1 = backfill_candidate(m_id, D1, driver_type="B",
                                history_entry={"date": D1, "kind": "EVIDENCE",
                                               "text": "龙虎榜机构席位净买 4000 万",
                                               "evidence": ["DT20261008-003"]}, path=pool)
        T("F1 driver_type 回填生效", b1["thesis"]["driver_type"] == "B")
        T("F2 演化史追加 1 条", len(b1["thesis"]["history"]) == 1)
        b2 = backfill_candidate(m_id, D2, driver_type="B",
                                history_entry={"date": D2, "kind": "CONTRADICT",
                                               "text": "次日承接转弱，量能未续",
                                               "evidence": []}, path=pool)
        T("F3 再追加不破坏旧条目（append-only）",
          len(b2["thesis"]["history"]) == 2
          and b2["thesis"]["history"][0]["kind"] == "EVIDENCE")
        T("F4 回填不改 state", b2["state"] == STATE_QUALIFIED)
        T("F5 非法 driver_type → 拒",
          raises(backfill_candidate, m_id, D1, "Z", None, None, None, None, None, pool))
        try:
            backfill_candidate(i_id, D1, driver_type="A", path=pool)
            term_rejected = False
        except CandidateError:
            term_rejected = True
        T("F6 终态候选不得回填", term_rejected)

        print("== G. 零买单与纪律 ==")
        import ast
        src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
        tree = ast.parse(src)
        banned_call = ("place_order", "submit_order", "buy", "sell", "trade", "execute_order")
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called.add(node.func.id.lower())
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called.add(node.func.attr.lower())
        T("G1 无任何下单类调用", not (called & set(banned_call)))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        T("G2 无网络/子进程类导入（不直连数据源）",
          not (imported & {"requests", "urllib", "http", "socket", "subprocess", "openai"}))
        T("G3 真候选池未被自检触碰（指纹不变）", _fp(POOL_PATH) == real_fp)
        bad = _load(pool)
        bad["candidates"][0]["state"] = "BOUGHT"
        T("G4 非法 state 被 schema 拒",
          raises(lambda x: _validate(x), bad))
        bad2 = _load(pool)
        bad2["candidates"][0]["extra_field"] = 1
        T("G5 字段增删被 schema 拒", raises(lambda x: _validate(x), bad2))
        bad3 = _load(pool)
        bad3["candidates"][0]["expiry"] = bad3["candidates"][0]["created_at"]
        T("G6 expiry 不晚于 created_at 被拒", raises(lambda x: _validate(x), bad3))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：%s" % " / ".join(fails))
    return 1 if fail else 0


def _main(argv):
    if "--selftest" in argv:
        print("candidates.py 自检（SCHEMA_VERSION=%d · 临时文件运行）" % SCHEMA_VERSION)
        return _selftest()
    print("候选池 · %s" % POOL_PATH)
    if not os.path.exists(POOL_PATH):
        print("  状态 : 尚未初始化（需按施工方案 0.5 建 data/state/candidates.json）")
        return 1
    d = _load()
    cs = d["candidates"]
    print("  version : %s" % d["version"])
    print("  updated : %s" % d["updated_at"])
    print("  候选数  : %d" % len(cs))
    for st in STATES:
        n = sum(1 for c in cs if c["state"] == st)
        if n:
            print("    %-11s %d" % (st, n))
    for c in cs:
        print("    %s  %-10s %-11s expiry=%s  last_seen=%s  src=%s"
              % (c["id"], c["code"], c["state"], c["expiry"], c["last_seen"], c["source"]))
    print("  纪律    : 状态迁移唯一执行器 = advance_candidate；本模块零买单")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
