#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scan.py · C2 发现扫描（施工方案 1.2 · v1.0 · 2026-10-08 建）

用途：覆盖全市场**产候选池、不决策**——五路机械扫描（只收不判）+ 事实→code 机械映射
      + 机械入池。**C2 = 发现；理解（驱动/next_trigger/keep_reason）归 C3，决策归 C4。**

★ 五路定义（source 值域，顺序固定；每路命中事实写入候选 trigger）：
  1. limit_up        快照 limit_up.csv 每行 → code 命中（涨停池成员 = 事实）。
                     炸板池**不存在**（快照无炸板列；fail_rate 是全市场单值、属 C1
                     环境信号）——本模块不越界造"炸板池"。
  2. sector_flow     ext 投放件 sector.jsonl（板块成员）+ capital_flow.jsonl（资金流）：
                     板块按 board_pct 降序取前 `sector_scan_top_n` 名（params）为"在榜"
                     → 其成员命中；资金 net_in > 0（符号事实）→ 命中。
  3. dragon_tiger    快照 dragon_tiger.csv 每行 → code 命中。
  4. news_hot        快照 news.jsonl（related_codes 非空 → 相关 code；空 → unmapped 不猜）
                     + ext 投放件 hot_search.jsonl（在榜 code）。
  5. mainline_recall mainline 台账（active+dormant 线成员）∩ 流通市值闸
                     （ext 投放件 float_mv.jsonl：float_mv ≤ `mainline_float_mv_gate`
                     才召回；**无市值数据 → 跳过该成员**——宁可漏召回，不猜）。

★ mainline 机械归并规则 v1（**七问逐条写死，全字符串拆分+相等匹配+日期比较，零 LLM**）：
  1. line_id 产生：对快照 limit_up.reason 按固定分隔符集 _SEPS 拆 token，strip 后
     长度 ≥ `mainline_token_min_len`（params）者成 token；**token 本身即线身份**，
     line_id = "L:" + token。不同义归并（"机器人"与"减速器"是两条线）——宁可漏召回。
  2. 成员进入：某 code 当日 reason 含该 token → 该 code 当日记为该线成员
     （first_seen/last_seen/hits 累计）。
  3. 状态转换（全日期驱动）：当日命中 → active（原非 active 则 day_n 从 1 重计）；
     active 线当日无命中 → miss_streak + 1，连续 miss ≥ `mainline_dormant_after_miss`
     （params）→ dormant；dormant 线再命中 → 复归 active（day_n 重计）。
  4. DayN：active 期内**有命中的交易日数**（dormant 期冻结，复归重计）。
  5. 一标的多线：多对多如实记录（同 code 可属 N 线），召回时按线各自过闸，不裁决。
  6. unmapped：reason 为空 / 拆后无合格 token → 计入报告 unmapped（**不入线、不猜**）。
  7. 零 LLM：归并全程无模型调用、无语义匹配；同输入两次构造逐字节相同（自检 B9）。
     台账 processed_upto 保证同日重跑幂等；as_of 倒流 → ScanError。

★ mainline 台账字段锁死（批准红线）：`mainline.json` 只允许"成员/状态/计数"机械账——
  lines[line_id] = {token, state(active|dormant), first_seen, last_hit, day_n,
  miss_streak, members{code: {first_seen, last_seen, hits}}}；
  **禁止出现强弱 / 看多看空 / 优先级 / 推荐 / 买点等任何判断字段。**

★ 数据边界（红线 4）：盘后全部经 `loader`（快照 5 文件 + load_ext 投放件），
  as_of ≤ T 物理截断；本模块**不直读快照文件、不绕 loader**。ext 投放件 schema
  （rows 均为对象行，本模块只读不写）：
    sector.jsonl {board, code, board_pct} / capital_flow.jsonl {code, net_in} /
    hot_search.jsonl {code, kw} / float_mv.jsonl {code, float_mv(亿元)}

★ fail-safe：某路取数 UNAVAILABLE → 该路记 unavailable、不产候选（不报错不编数据）；
  **五路全 UNAVAILABLE → scan 返回 None**（CLI 落 result=EMPTY，禁编池）。

★ **日期完整性闸（B3 · 2026-10-13 接入 · 本模块为 P1 首批落点）**：
  loader 的取数契约自带**回退语义**——目标日无快照时返回"最近一个 ≤ 目标日"的行集并置
  `is_exact=False`。若直接消费，即构成**「用前一日数据冒充目标日事实」**：
  会凭空产候选、把主线台账 `processed_upto` 推到缺口日、并让 DayN/miss_streak 少算。
  故本模块在**消费点单点加闸**，两道独立判据：
    ① **`is_exact` 硬闸（无需日历，恒生效）**：凡 `res.is_exact is not True` ⇒ 该路
       记 `stale_blocked`、**一律不产事实**（缺口日全路被阻断 ⇒ 落回既有 fail-safe
       "全路不可用 → None"，候选与台账状态**都不会被推进**）。
    ② **日状态闸（需日历）**：`dayclass.classify()` 四态判定；非
       `TRADING_WITH_DATA` 一律拒绝扫描。缺日历 ⇒ `UNKNOWN` ⇒ **默认拒绝**
       `on_unknown="block"`（fail-closed：无日历不许猜休市，也不许按周末推）。
       `on_unknown="allow"` 是**显式、可 grep 的例外**，只给"无日历的历史回放沙盒"用，
       且必须在报告里留 `day_status_basis` 证据。
    ③ **连续性闸**：`mainline_update` 增 `calendar`，`as_of` 必须恰为
       `processed_upto` 的**下一个交易日**；否则抛 `ScanError`（HALT）——
       缺口日**不得被静默跳过**（跳日会让 miss_streak 少算、dormant 迟迟不转）。
  注：本模块**不落日历文件**（`trading_calendar.json` 须单独授权）；日历由调用方注入
  （`Calendar` 对象或 `from_seed_csv` 只读路径），CLI 经 `--calendar` 传入。

★ **声明感知 · 已知缺陷日的显式标注（P1-3 · 用户 2026-10-13 裁定）**：
  裁定原文口径 = 09-23「**接受为可扫描日 + 显式标注**」（不是排除、不是跳过）。
  故本模块**不新增任何阻断**——缺陷日照常扫描、照常入池、照常推进台账；但必须做到
  「**结论与标注同时到达**」，否则下游读到 `processed_upto` 已越过缺陷日却无从知晓，
  即构成**静默污染**（比拒扫更坏）。三步落地：
    ① **读取**：缺陷日登记真源 = 投放区显式声明件 `_unresolved.decl.json` 的
       `known_gap_days` 段（文件名真源 = `ingest.DECL_FNAME`，零重定义）。
    ② **落账**：被标注日 ⇒ 台账增 `data_annotations[<date>] = {kind, decl, sha256}`
       —— **只存指针 + 摘要，不复制正文**（复制即造第二真源，声明件仍是唯一权威）。
       **未被标注的日期不落任何键**（缺席 ≠ 空：不许无条件写入空段）。
    ③ **留痕**：报告增 `data_annotation` 段；CLI 在扫描前打印一行可见标注。
  ★ `data_annotations` 是**出处/审计事实**，不是判断字段（`kind` 只有
    `DECLARED_KNOWN_GAP` 一个取值）——不违反「台账只允许成员/状态/计数」红线的本意，
    故台账 `version` 1 → 2 并在此处显式声明该扩展。

  ※ 读 `data/raw/` 是「不直读快照文件、不绕 loader」红线的**显式例外**，三条理由：
    Ⅰ 读的是**治理件**（记载"为什么空、谁授权"），非数据文件，loader 契约无从承载；
    Ⅱ `loader` 属**冻结件**（`C0.loader`），不得为读取声明件而改冻结契约；
    Ⅲ 纯只读、零写入，且缺失/损坏一律退化为"无标注"（fail-safe，见下）。
  ※ 取数口径 = **全量并集**（遍历全部 `data/raw/*/` 的声明件）：逐日件内
    `known_gap_days` 只登记"该批 dates"的缺陷日，**各件内容并不一致**，
    只读 as_of 当日件会**漏标**（fail-open）。并集方向 = 宁可多标，绝不漏标。

★ 入池（全机械）：每 code 汇总五路事实 → `candidates.upsert_candidate`
  （source="|".join(命中路)，trigger.text = 各路事实串；**next_trigger / invalid_condition
  一律不填——那是 C3 回填字段，C2 不猜**）。状态机迁移（DISCOVERED/WATCH/EXPIRED）
  全归骨架 0.5 `candidates.py`，本模块**零状态机逻辑**，只在扫描后调一次
  `advance_candidate` 让时间边界（超观察窗 → EXPIRED 留档）机械生效。

★ 留痕（0.17）：CLI 经 `observe.run_observed(actor="scan", stage="C2")` 包装；
  写点唯一目标 = `data/state/mainline.json`（候选写入全部发生在已登记的
  candidates.py 内，本模块自身对候选池零直写）。

用法：
  python tools/discovery/scan.py --eval --as-of 2026-10-08   # 盘后五路扫描 → 入池
  python tools/discovery/scan.py --eval --as-of 2026-10-08 --calendar <trade_cal.csv>
                                                             # 注入日历 → 严格日状态闸
  python tools/discovery/scan.py --selftest                  # 内置验收（全临时沙盒）
  python tools/discovery/scan.py --discipline

规则：本模块禁 LLM、禁排序打分语义、禁 BUY/WAIT/NO、禁动游标、禁写 brain、
     禁改候选池状态机；改阈值 = 改 params.json（校准）或改本文件 + 1.2 正文（口径）。
"""
import ast
import hashlib
import io
import json
import os
import sys
import tempfile

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FS = os.path.join(_TOOLS, "fs")
if _FS not in sys.path:
    sys.path.insert(0, _FS)

from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
import params as PARAMS  # noqa: E402
import loader as LD  # noqa: E402
import candidates as CAND  # noqa: E402
import observe as OBS  # noqa: E402
import astwin as ASTW  # noqa: E402
import snapshot as SN  # noqa: E402
import dayclass as DC  # noqa: E402   （B3 日期完整性：四态判定 + exact 闸门）
import ingest as ING  # noqa: E402   （P1-3 声明件文件名真源 DECL_FNAME，零重定义）

_QUERY = os.path.join(_TOOLS, "query")
if _QUERY not in sys.path:
    sys.path.insert(0, _QUERY)
import pool_match as PM  # noqa: E402
import codes as CD  # noqa: E402  ★ 代码类型判据唯一真源（2026-10-22）

CAP_ID = "C2"
ACTOR = "scan"

# 五路 source 值域（顺序固定：source 联合与报告都按此序，保证可复现）
ROUTES = ("limit_up", "sector_flow", "dragon_tiger", "news_hot", "mainline_recall")

# params 键（拼装构造，防纪律词表自指；阈值真源 = params.json，代码零硬编码）
K_TOP_N = "sector_scan" + "_top_n"            # 板块榜"在榜"前 N（count）
K_MIN_LEN = "mainline_token" + "_min_len"     # reason token 最短长度（chars）
K_DORMANT = "mainline_dormant" + "_after_miss"  # active 连续 miss 几日转 dormant（days）
K_GATE = "mainline_float_mv" + "_gate"        # 主线召回流通市值闸上限（亿元）

# 唯一写点目标（模块常量：落点审计 _resolve 经 BoolOp 解析 → data/state 预期生产写入）
MAINLINE_PATH = os.path.join(ROOT, "data", "state", "mainline.json")

# reason 拆分分隔符集（机械、写死、无语义）
_SEPS = ("+", "/", "、", "，", ",", "；", ";", " ", "\t")

_LINE_PREFIX = "L:"
_STATE_ACTIVE = "active"
_STATE_DORMANT = "dormant"
_STATES = (_STATE_ACTIVE, _STATE_DORMANT)

# 台账 schema 版本（2 = 增 `data_annotations` 出处段；见模块头「声明感知」★）
LEDGER_VERSION = 2

# ── 声明感知常量（P1-3）────────────────────────────────────────────────────
_ANN_SECTION = "data_annotations"      # 台账内标注段（只存指针+摘要，不复制正文）
_ANN_KIND = "DECLARED_KNOWN_GAP"       # kind 值域**唯一**取值（值域封闭，禁自由文本）
_GAP_SEG = "known_gap_days"            # 声明件内的缺陷日登记段名


class ScanError(Exception):
    """扫描契约违例（日期倒流 / params 缺键 / 台账结构非法）。响失败，禁静默猜。"""


# ── mainline 台账（机械账：成员/状态/计数 + 出处标注，别无他物） ─────────────
def _new_ledger():
    """空骨架。**不预置 `data_annotations` 键**——缺席 ≠ 空：无缺陷日就不该有该段。"""
    return {"version": LEDGER_VERSION, "updated_at": None, "processed_upto": None,
            "lines": {}}


def load_mainline(path=None):
    """读台账；文件不存在 → 空骨架（不抛错——首次扫描合法起点）。

    **版本迁移**：磁盘上 `version < LEDGER_VERSION` 的旧账（1 = 无 `data_annotations`
    段）在**读入时即升版**，不另写文件——升版只放宽可选段，旧账语义零变化，
    `lines` 一字不动。降版**不做**（未来版本文件照读，不擅自回退）。
    """
    p = path or MAINLINE_PATH
    _WG.check_write(p)
    if not os.path.exists(p):
        return _new_ledger()
    with io.open(p, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict) or not isinstance(d.get("lines"), dict):
        raise ScanError("mainline 台账结构非法（lines 缺失或非对象）: %s" % p)
    v = d.get("version")
    if not isinstance(v, int) or isinstance(v, bool) or v < LEDGER_VERSION:
        d["version"] = LEDGER_VERSION
    return d


def _save_mainline(d, path=None):
    """原子写（tmp + replace）；写点目标内联 `path or MAINLINE_PATH` 供落点审计静态解析。"""
    p = path or MAINLINE_PATH
    d["updated_at"] = d.get("processed_upto") or d.get("updated_at")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path or MAINLINE_PATH)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def tokens_from_reason(reason, min_len):
    """reason 文本 → 合格 token 列表（固定分隔符拆分 + strip + 最短长度；全机械）。"""
    if not isinstance(reason, str) or not reason.strip():
        return []
    out = []
    for tok in _split_reason(reason):
        tok = tok.strip()
        if len(tok) >= min_len:
            out.append(tok)
    return out


def _split_reason(reason):
    """按 _SEPS 逐个替换为统一分隔符后拆分（不引入任何语义）。"""
    s = reason
    for sep in _SEPS:
        s = s.replace(sep, _SEPS[0])
    return s.split(_SEPS[0])


def _param(name, params_path=None):
    v = PARAMS.get_value(name, path=params_path)
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ScanError("params 键 %s 缺失或非数值（阈值只能来自 params，禁代码硬编码）" % name)
    return v


# ── 声明感知：已知缺陷日的读取与判定（P1-3 · 只读；零阻断）─────────────────────
def _rel(p, base=None):
    """绝对路径 → **数据根相对** POSIX 路径（台账内指针形态固定：`data/raw/<d>/…`）。

    以 `base`（缺省项目根）为锚 ⇒ 生产与沙盒（tempfile）落到**同一种指针形态**，
    台账不因"跑在哪个根下"而变形；落点审计与下游复核都只认这一种写法。
    """
    return os.path.relpath(p, base or ROOT).replace(os.sep, "/")


def decl_files(base=None):
    """→ 投放区内**全部**声明件绝对路径（目录名升序，确定性）；无边目录 ⇒ 空列表。"""
    r = LD.roots(base)["raw"]
    if not os.path.isdir(r):
        return []
    out = []
    for name in sorted(os.listdir(r)):
        p = os.path.join(r, name, ING.DECL_FNAME)
        if os.path.isfile(p):
            out.append(p)
    return out


def known_gap_days(base=None):
    """→ ({date: note}, [声明件绝对路径…])：全库声明件 `known_gap_days` **并集**。

    fail-safe 三条（声明件是留痕件，**单件损坏不得阻断全链扫描**）：
      · 文件缺失 / 非法 JSON / 根非对象 ⇒ **跳过该件**，不进证据表、不抛错；
      · 段 `known_gap_days` 非对象 ⇒ 同样跳过；
      · 同一日期多件登记 ⇒ **首件（目录序）胜**（取首个遇到的 note，保持确定性）。
    """
    out, ev = {}, []
    for p in decl_files(base=base):
        try:
            with io.open(p, encoding="utf-8") as f:
                o = json.load(f)
        except Exception:
            continue
        kg = o.get(_GAP_SEG) if isinstance(o, dict) else None
        if not isinstance(kg, dict):
            continue
        ev.append(p)
        for d, note in kg.items():
            if isinstance(d, str) and d not in out:
                out[d] = note
    return out, ev


def _sha256(path):
    h = hashlib.sha256()
    with io.open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def declared_annotation(as_of, base=None):
    """→ 该日的数据标注（**纯只读**）。未被标注 ⇒ `known_gap=False` 且零指针。

    返回 `{known_gap, kind, decl, decl_sha256, n_decl_files}`。
    `decl_sha256` 让标注**可机械复核**（台账只存指针+摘要 ⇒ 声明件仍是唯一真源）。
    证据指针优先取 as_of 当日件；并集命中但当日无件 ⇒ 退回首件（仍如实指向真源）。
    """
    as_of = CAND._check_date(as_of)
    gaps, ev = known_gap_days(base=base)
    info = {"known_gap": False, "kind": None, "decl": None, "decl_sha256": None,
            "n_decl_files": len(ev)}
    if as_of not in gaps or not ev:
        return info
    same = [q for q in ev if os.path.basename(os.path.dirname(q)) == as_of]
    p = same[0] if same else ev[0]
    info.update({"known_gap": True, "kind": _ANN_KIND,
                 "decl": _rel(p, base), "decl_sha256": _sha256(p)})
    return info


def _ann_entry(annotation):
    """标注 → 台账条目（**字段锁死**：kind / decl / sha256 三键，别无他物）。"""
    return {"kind": annotation.get("kind") or _ANN_KIND,
            "decl": annotation.get("decl"),
            "sha256": annotation.get("decl_sha256")}


def mainline_update(as_of, lu_rows, path=None, params_path=None, calendar=None,
                    annotation=None):
    """把当日涨停池事实机械归并进台账。

    lu_rows：当日 load_limit_up 行集（None = 当日数据缺失 → 不做任何归并与 miss 计，
    宁可漏，不把数据缺失日当"没命中"）。返回 (ledger, {"unmapped": n, "new_lines": n})。
    幂等：processed_upto == as_of → 原样返回；as_of < processed_upto → ScanError。

    **连续性闸（B3 P1）**：给了 `calendar` 时，`as_of` 必须恰为 `processed_upto` 的
    **下一个交易日**，否则抛 `ScanError`（HALT）。理由：缺口日被静默跳过会让
    active 线的 `miss_streak` 少算、dormant 迟迟不转，等于**悄悄压缩时间轴**。
    未给日历 ⇒ 只保留原有"不倒流"判据（缺口不可判 ⇒ 不得凭空判定）。

    **声明感知（P1-3）**：`annotation = declared_annotation(as_of)` 的结果。仅当
    `known_gap` 为真时，在台账 `data_annotations[as_of]` 落一条**出处指针**（kind/decl/
    sha256 三键）。未标注日**不落任何键** —— 不许无条件写空段（缺席 ≠ 空）。
    标注与 `processed_upto` **同一次写入**完成（原子性由 `_save_mainline` 保证）：
    要么"台账已越过该日且标注在册"，要么两者都不发生。
    """
    as_of = CAND._check_date(as_of)
    min_len = _param(K_MIN_LEN, params_path)
    dormant_after = _param(K_DORMANT, params_path)
    d = load_mainline(path)
    upto = d.get("processed_upto")
    if upto:
        if as_of < upto:
            raise ScanError("as_of(%s) 早于台账已处理日(%s)——时间倒流，禁回写" % (as_of, upto))
        if as_of == upto:
            return d, {"unmapped": 0, "new_lines": 0, "idempotent": True}
        if calendar is not None:
            ok, expect = DC.is_contiguous(calendar, upto, as_of)
            if not ok:
                raise ScanError(
                    "台账连续性中断：已处理至 %s，其下一交易日为 %s，而本次 as_of=%s —— "
                    "缺口日不得被静默跳过（HALT；补数须显式 --replay-from）"
                    % (upto, expect, as_of))

    today = {}          # token -> [code,...]（按行序）
    unmapped = 0
    if lu_rows is not None:
        for r in lu_rows:
            code = r.get("code")
            toks = tokens_from_reason(r.get("reason"), min_len)
            if not toks or not isinstance(code, str) or not code.strip():
                unmapped += 1
                continue
            code = code.strip()
            for tok in toks:
                today.setdefault(tok, [])
                if code not in today[tok]:
                    today[tok].append(code)

    new_lines = 0
    for lid, ln in d["lines"].items():
        tok = ln["token"]
        if tok in today:
            if ln["state"] != _STATE_ACTIVE:
                ln["state"] = _STATE_ACTIVE
                ln["day_n"] = 1                       # 复归 / 新活跃：DayN 重计
            else:
                ln["day_n"] = ln["day_n"] + 1
            ln["miss_streak"] = 0
            ln["last_hit"] = as_of
            for code in today[tok]:
                m = ln["members"].setdefault(
                    code, {"first_seen": as_of, "last_seen": as_of, "hits": 0})
                m["last_seen"] = as_of
                m["hits"] = m["hits"] + 1
        elif ln["state"] == _STATE_ACTIVE:
            ln["miss_streak"] = ln["miss_streak"] + 1
            if ln["miss_streak"] >= dormant_after:
                ln["state"] = _STATE_DORMANT          # 只转态，绝不删行（历史留档）
                ln["miss_streak"] = 0

    for tok, codes in today.items():
        lid = _LINE_PREFIX + tok
        if lid in d["lines"]:
            continue
        d["lines"][lid] = {"token": tok, "state": _STATE_ACTIVE,
                           "first_seen": as_of, "last_hit": as_of,
                           "day_n": 1, "miss_streak": 0, "members": {}}
        new_lines += 1
        for code in codes:
            d["lines"][lid]["members"][code] = {
                "first_seen": as_of, "last_seen": as_of, "hits": 1}

    if annotation and annotation.get("known_gap"):
        d.setdefault(_ANN_SECTION, {})[as_of] = _ann_entry(annotation)

    d["processed_upto"] = as_of
    _save_mainline(d, path)
    return d, {"unmapped": unmapped, "new_lines": new_lines}


# ── 日期完整性闸（B3 P1）────────────────────────────────────────────────────
# 日状态允许扫描的唯一值（其余三态一律拒绝；常量取自 dayclass，禁在本模块另写字面量）
_SCAN_OK_STATUS = DC.STATUS_TRADING_WITH_DATA
_ON_UNKNOWN = ("block", "allow")     # 值域封闭：显式二选一，禁隐式默认


def snapshot_keys(as_of, base=None):
    """目标日快照目录内**实际存在**的契约键集（只读列举；目录不存在 → 空集）。

    仅用于 `dayclass.classify` 的缺键判据（缺键 ⇒ TRADING_NO_DATA），
    本函数**不判断可用性**、不做任何回退（那是 classify 的职责）。
    """
    d = os.path.join(LD.roots(base)["snapshot"], as_of)
    if not os.path.isdir(d):
        return ()
    names = set(os.listdir(d))
    return tuple(spec["key"] for fn, spec in SN.CONTRACT.items() if fn in names)


def day_verdict(as_of, *, base=None, calendar=None, required_keys=None):
    """→ `dayclass.classify` 判定结果（数据日期集 = 快照目录名；键齐备性一并入判）。

    `required_keys` 缺省 = 0.2 契约全 5 键（缺键 ⇒ 降级 `TRADING_NO_DATA`）；
    传空元组 = 只判日期、不判键齐备。
    """
    keys = (tuple(spec["key"] for spec in SN.CONTRACT.values())
            if required_keys is None else tuple(required_keys))
    dates = SN.available_dates(root=LD.roots(base)["snapshot"])
    return DC.classify(as_of, calendar=calendar, data_dates=dates,
                       required_keys=keys,
                       present_keys=snapshot_keys(as_of, base=base))


def _date_guard(res):
    """取数结果 → (rows|None, info)。**B3 核心闸：数据日 ≠ 目标日即阻断**。

    `res.is_exact` 非 True（含 None/False）一律视为不可用 ⇒ rows=None。
    这正是「前一日数据回退后被当作目标日事实」的单点封堵：消费方拿不到行集，
    因而**不可能**用它产候选、推进台账或压缩连续交易日计数。
    """
    if res is LD.UNAVAILABLE or res is None:
        return None, {"unavailable": True, "stale_blocked": False, "data_date": None}
    if not isinstance(res, (list, tuple)):
        raise ScanError("loader 返回非行集: %r" % (type(res).__name__,))
    data_date = getattr(res, "date", None)
    if getattr(res, "is_exact", None) is not True:
        return None, {"unavailable": True, "stale_blocked": True,
                      "data_date": data_date}
    return ([r for r in res if isinstance(r, dict)],
            {"unavailable": False, "stale_blocked": False, "data_date": data_date})


def _rows_of(res):
    """loader 返回 Series → 对象行列表；UNAVAILABLE → None。非 UNAVAILABLE 的非列表 → 错。

    保留本函数 = 兼容既有调用（自检夹具/外部）；**生产取数一律走 `_date_guard`**。
    """
    if res is LD.UNAVAILABLE or res is None:
        return None
    if not isinstance(res, (list, tuple)):
        raise ScanError("loader 返回非行集: %r" % (type(res).__name__,))
    return [r for r in res if isinstance(r, dict)]


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _row_by_code(rows, code):
    """按 strip 后 code 找首行（PM 输出的 code 已 strip；找不到 → None，宁可漏）。"""
    for r in rows:
        c = r.get("code")
        if isinstance(c, str) and c.strip() == code:
            return r
    return None


def scan(as_of, *, base=None, params_path=None, pool_path=None, mainline_path=None,
         calendar=None, on_unknown="block"):
    """五路机械扫描 → 机械入池。返回报告 dict；五路全 UNAVAILABLE → None（禁编池）。

    日期闸（B3 P1）：非 `TRADING_WITH_DATA` 一律返回 None（不产候选、不推台账）；
    `on_unknown` 只在**无日历**（status=UNKNOWN）时生效，且必须显式二选一。
    """
    as_of = CAND._check_date(as_of)
    if on_unknown not in _ON_UNKNOWN:
        raise ScanError("on_unknown 值域封闭为 %s，收到 %r" % (list(_ON_UNKNOWN), on_unknown))
    top_n = _param(K_TOP_N, params_path)
    gate = _param(K_GATE, params_path)

    verdict = day_verdict(as_of, base=base, calendar=calendar)
    if not (verdict["status"] == DC.STATUS_UNKNOWN and on_unknown == "allow"):
        if verdict["status"] != _SCAN_OK_STATUS:
            return None   # 日状态拒绝：NON_TRADING / TRADING_NO_DATA / UNKNOWN(block)

    report = {"as_of": as_of, "routes": {}, "created": 0, "duplicate": 0,
              "skipped_no_mv": 0, "skipped_over_gate": 0, "mainline_unmapped": 0,
              "codes": [],
              "day_status": verdict["status"], "day_exact": verdict["exact"],
              "day_reasons": verdict["reasons"],
              "day_status_basis": verdict["calendar_basis"],
              "day_anchor": verdict["anchor"]}
    # P1-3 声明感知：**先判定再消费**——标注与本次扫描的结论同批产出（禁事后补标）
    annotation = declared_annotation(as_of, base=base)
    report["data_annotation"] = annotation
    facts = {}   # code -> {route: 事实文本}

    # 被剔的非个股计数：`{类型: 数量}`。**必须如实进报告**，
    # 否则"少了 181 条"会变成又一个看不见的静默丢弃。
    _nonstock = {}

    def add(code, route, text):
        code = code.strip()
        if not code:
            return
        # ★★ 2026-10-22 加入代码类型闸 —— 此前**五条采纳路全部不做类型校验**，
        #   导致 181 条可转债/指数（来源 news_hot、2026-09 遗留）进了候选池。
        #   根因是判据散落四处且口径不一（s5 的 `^(sh|sz|bj)\d{6}$` 放行转债、
        #   ingest 的 `_CODE_RE` 也放行），最松的那个（无校验）决定了实际行为。
        #   现在统一到 `codes.is_stock`（全项目唯一真源）。
        if not CD.is_stock(code):
            k = CD.classify(code)
            _nonstock[k] = _nonstock.get(k, 0) + 1
            return
        d = facts.setdefault(code, {})
        if route not in d:
            d[route] = text

    # 路1 limit_up + 主线归并输入（**经 _date_guard：数据日≠目标日 ⇒ 无行可消费**）
    lu, lu_info = _date_guard(LD.load_limit_up(as_of, base=base))

    # ★★ 2026-10-22 记录 A9 层③ 判决（**默认只记录，不拦**）
    #
    #   背景：`snapshot.assert_admissible()` 全项目只有自检在调用，
    #   2026-10-14 ~ 2026-10-22 每个快照 `admitted` 都是 false 却没人受影响 ——
    #   准入闸建了但没接进消费链。
    #
    #   本次把它**接进来但设为记录态**，不设拦截，理由（都是实测，不是猜）：
    #   ① 历史 7 天快照判决全为 `unchecked=5 / admitted=false`，
    #      它们写在 provenance 修复**之前**（那时 AS_OF/COVERAGE 一律"未检查"）。
    #      硬拦 ⇒ 7 天好数据瞬间全废。
    #   ② 自检沙盒夹具复用了生产 sidecar，但夹具票名/行集是构造的，
    #      SEMANTIC 必报假缺陷；硬拦会直接把 `scan --selftest` 打断
    #      （2026-10-22 试了两次，两次都挂）。
    #   ③ sidecar 里 `not_checked`（没声明所以没判）与 `blocked`（判了且不过）
    #      本是两个字段，混为一谈正是本次要修的病。
    #
    #   ⇒ 现在**只把判决原文记进报告**（`report["admission"]["limit_up"]`），
    #     要拦的时候由上层显式读该字段决定。**这比"假装接入"诚实**：
    #     闸有判决、报告有记录、但拦不拦是一个显式决策，不是隐式副作用。
    if lu is not None:
        try:
            _adm = SN.admission_of(as_of, root=LD.roots(base)["snapshot"])
            if _adm.get("missing"):
                report.setdefault("admission", {})["limit_up"] = {
                    "verdict": "NO_SIDECAR", "admitted": False,
                    "note": "该日无准入判决 —— 未判定 ≠ 通过"}
            else:
                _files = _adm.get("files") or {}
                _hard = sorted({c for _v in _files.values()
                                for c in (_v.get("new_defects") or ())
                                if c in ("STRUCTURE", "SEMANTIC")})
                report.setdefault("admission", {})["limit_up"] = {
                    "verdict": ("HARD_DEFECT" if _hard
                                else ("UNCHECKED" if any(
                                    _v.get("not_checked") for _v in _files.values())
                                    else "OK")),
                    "admitted": bool(_adm.get("admitted")),
                    "hard_defects": _hard,
                    "source": "%s/_admission.json" % as_of,
                }
        except Exception as e:
            report.setdefault("admission", {})["limit_up"] = {
                "verdict": "ERROR", "error": "%s: %s"
                % (type(e).__name__, str(e)[:120])}
    report["routes"]["limit_up"] = {"hit": 0, "unavailable": lu is None,
                                    "stale_blocked": lu_info["stale_blocked"],
                                    "data_date": lu_info["data_date"],
                                    "admission_rejected":
                                        lu_info.get("admission_rejected", False)}
    if lu is not None:
        for r in lu:
            code = r.get("code")
            if isinstance(code, str) and code.strip():
                add(code, "limit_up", "涨停池:boards=%s,reason=%s"
                    % (r.get("boards"), r.get("reason") or ""))
                report["routes"]["limit_up"]["hit"] += 1

    # 路5 前置：mainline 台账机械归并（唯一输入 = 当日涨停池 reason；缺数据日不归并不计 miss）
    ml_report = {"unmapped": 0, "new_lines": 0}
    ledger = None
    # 2026-10-19 修：mainline_path 缺省原回落生产常量 MAINLINE_PATH ⇒ 沙盒链
    # （run_chain --base）必写生产 mainline.json 被闸门拦（C2 FAIL）。按 base 派生，
    # 与 pool_path 等同类入参的派生口径一致；显式传入仍优先。
    ml_path = mainline_path or os.path.join(base or ROOT, "data", "state",
                                            "mainline.json")
    if lu is not None:
        ledger, ml_report = mainline_update(as_of, lu, path=ml_path,
                                            params_path=params_path,
                                            calendar=calendar,
                                            annotation=annotation)

    # 路2 sector_flow
    sec, sec_info = _date_guard(LD.load_sector(as_of, base=base))
    cap, cap_info = _date_guard(LD.load_capital_flow(as_of, base=base))
    hit2 = 0
    report["routes"]["sector_flow"] = {
        "hit": 0, "unavailable": sec is None and cap is None,
        "stale_blocked": bool(sec_info["stale_blocked"] or cap_info["stale_blocked"]),
        "data_date": sec_info["data_date"] or cap_info["data_date"]}
    if sec is not None:
        board_pct = {}
        for r in sec:
            b, p = r.get("board"), r.get("board_pct")
            if isinstance(b, str) and b.strip() and _num(p):
                board_pct.setdefault(b.strip(), p)
        ranked = sorted(board_pct.items(), key=lambda kv: (-kv[1], kv[0]))
        top_boards = frozenset(b for b, _p in ranked[:int(top_n)])
        got = PM.from_sector(sec, top_boards)
        for code in got["codes"]:
            add(code, "sector_flow", "板块榜:%s" % ",".join(sorted(top_boards)))
            hit2 += 1
    if cap is not None:
        inflow = [r for r in cap if _num(r.get("net_in")) and r.get("net_in") > 0]
        got = PM.from_rows(inflow)
        for code in got["codes"]:
            r = _row_by_code(inflow, code)
            if r is None:
                continue
            add(code, "sector_flow", "资金净流入:%s" % r.get("net_in"))
            hit2 += 1
    report["routes"]["sector_flow"]["hit"] = hit2

    # 路3 dragon_tiger
    dt, dt_info = _date_guard(LD.load_dragon_tiger(as_of, base=base))
    report["routes"]["dragon_tiger"] = {"hit": 0, "unavailable": dt is None,
                                        "stale_blocked": dt_info["stale_blocked"],
                                        "data_date": dt_info["data_date"]}
    if dt is not None:
        got = PM.from_rows(dt)
        for code in got["codes"]:
            r = _row_by_code(dt, code)
            if r is None:
                continue
            add(code, "dragon_tiger", "龙虎榜:net=%s" % r.get("net"))
            report["routes"]["dragon_tiger"]["hit"] += 1

    # 路4 news_hot
    nw, nw_info = _date_guard(LD.load_news(as_of, base=base))
    hs, hs_info = _date_guard(LD.load_hot_search(as_of, base=base))
    hit4, unmapped4 = 0, 0
    report["routes"]["news_hot"] = {
        "hit": 0, "unavailable": nw is None and hs is None,
        "stale_blocked": bool(nw_info["stale_blocked"] or hs_info["stale_blocked"]),
        "data_date": nw_info["data_date"] or hs_info["data_date"]}
    if nw is not None:
        got = PM.from_news(nw)
        unmapped4 += got["unmapped"]
        for code in got["codes"]:
            add(code, "news_hot", "消息关联:news=%s" % _news_ids(nw, code))
            hit4 += 1
    if hs is not None:
        got = PM.from_rows(hs)
        unmapped4 += got["unmapped"]
        for code in got["codes"]:
            add(code, "news_hot", "热搜在榜")
            hit4 += 1
    report["routes"]["news_hot"]["hit"] = hit4
    report["routes"]["news_hot"]["unmapped"] = unmapped4

    # 路5 mainline_recall（台账成员 ∩ 市值闸；无市值数据 → 跳过该成员，宁可漏召回）
    hit5 = 0
    if ledger is not None:
        got = PM.from_mainline(ledger["lines"], states=_STATES)
        fm, fm_info = _date_guard(LD.load_ext("float_mv", as_of, base=base))
        mv_map = {}
        if fm is not None:
            for r in fm:
                if isinstance(r.get("code"), str) and _num(r.get("float_mv")):
                    mv_map[r["code"].strip()] = r["float_mv"]
        line_of = {}
        for lid, ln in ledger["lines"].items():
            if ln["state"] in _STATES:
                for code in ln["members"]:
                    line_of.setdefault(code, []).append(lid)
        for code in got["codes"]:
            mv = mv_map.get(code)
            if mv is None:
                report["skipped_no_mv"] += 1
                continue
            if mv > gate:
                report["skipped_over_gate"] += 1
                continue
            add(code, "mainline_recall", "主线召回:%s,mv=%s亿"
                % (",".join(sorted(line_of.get(code, []))), mv))
            hit5 += 1
    else:
        fm_info = {"stale_blocked": False}
    report["routes"]["mainline_recall"] = {
        "hit": hit5, "unavailable": ledger is None,
        "stale_blocked": bool(fm_info["stale_blocked"]),
        "unmapped": ml_report.get("unmapped", 0), "new_lines": ml_report.get("new_lines", 0)}
    report["mainline_unmapped"] = ml_report.get("unmapped", 0)

    # 全路 UNAVAILABLE → None（fail-safe，禁编池）
    if all(report["routes"][r]["unavailable"] for r in ROUTES):
        return None

    # 机械入池（code 字母序，确定性；状态机全归骨架 0.5）
    for code in sorted(facts):
        routes_hit = [r for r in ROUTES if r in facts[code]]
        source = "|".join(routes_hit)
        trig = {"text": "C2 命中 %s: %s" % (as_of, "; ".join(
            facts[code][r] for r in routes_hit)), "rule": None}
        res = CAND.upsert_candidate(code, as_of, source, trig, path=pool_path)
        if res["created"]:
            report["created"] += 1
        else:
            report["duplicate"] += 1
        report["codes"].append(code)

    # 时间边界机械生效：超观察窗 → EXPIRED（留档不删，骨架 0.5 唯一时间边界）
    CAND.advance_candidate(as_of, path=pool_path)

    # ★ 非个股剔除必须可见：把「被类型闸挡掉的」如实写进报告。
    #   不写就是静默丢弃 —— 那是本项目反复吃过亏的形态（M14/M22）。
    report["non_stock_dropped"] = dict(sorted(_nonstock.items()))
    if _nonstock:
        print("  代码类型闸：剔除非个股 %d 条 %s（可转债/指数/ETF 不属候选宇宙）"
              % (sum(_nonstock.values()), report["non_stock_dropped"]))
    return report


def _news_ids(rows, code):
    return ",".join(sorted(r["news_id"] for r in rows
                           if isinstance(r.get("news_id"), str)
                           and isinstance(r.get("related_codes"), list)
                           and code in [str(c).strip() for c in r["related_codes"]]))


# ── 自检（全临时沙盒，不碰真库；判据可机械） ─────────────────────────────────
_TEST_FUNCS = ("_selftest", "_sandbox_sb", "_feed_raw", "_pay", "_env_row",
               "_quote_row", "_lu_row", "_dt_row", "_news_row", "_discipline",
               "_cal_sb", "_damage_snapshot", "_ml_state", "_feed_decl",
               "_decl_fp", "_snapshot_rejects_four_keys")


# 沙盒日历（合成；**不读生产文件、不落盘**）：10-08/10-09 与 10-12 相邻交易日，
# 10-09 可被 exclude 掉用于"休市日"场景
_SB_SESSIONS = ("2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06",
                "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12",
                "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
                "2026-10-19", "2026-10-20")


def _cal_sb(exclude=()):
    """沙盒日历（来源标注 SANDBOX；仅供自检与隔离测试）。"""
    return DC.Calendar([d for d in _SB_SESSIONS if d not in exclude],
                       source="SANDBOX", version="test")


def _damage_snapshot(sb, date, fname):
    """把已写好的沙盒快照**删掉一个契约文件**（模拟"目录在但键不全"的残缺快照）。

    注：`snapshot.write_snapshot` 在写入前就强制恰 5 键（结构闸），
    故残缺态**只能**从磁盘侧制造 —— 这本身即"结构闸 ≠ 完整性闸"的实证。
    """
    p = os.path.join(sb, "data", "snapshot", date, fname)
    if os.path.exists(p):
        os.unlink(p)
    return p


def _sandbox_sb():
    """→ (沙盒根, params 路径, pool 路径, mainline 路径)。params = 真文件复制。"""
    sb = tempfile.mkdtemp(prefix="ta_scan_")
    pp = os.path.join(sb, "params.json")
    io.open(pp, "w", encoding="utf-8").write(
        io.open(os.path.join(ROOT, "data", "params.json"), encoding="utf-8").read())
    pool = os.path.join(sb, "pool", "candidates.json")
    mainl = os.path.join(sb, "pool", "mainline.json")
    os.makedirs(os.path.dirname(pool))
    # 0.5 语义：候选池禁静默新建 —— 沙盒先落合法空池（与真库同构）
    io.open(pool, "w", encoding="utf-8").write(
        json.dumps({"version": CAND.SCHEMA_VERSION, "updated_at": "2026-10-08",
                    "candidates": []}))
    return sb, pp, pool, mainl


def _pay(codes_lu, news, dt):
    return {"daily_quote": [_quote_row(c) for c in
                            ("300001", "600002", "600519", "600003", "600519")],
            "limit_up": codes_lu, "dragon_tiger": dt,
            "news": news,
            "env_metrics": _env_row()}


def _quote_row(code):
    return {"code": code, "name": "x" + code, "open": 1.0, "high": 1.1, "low": 0.9,
            "close": 1.05, "pct": 5.0, "vol": 100.0, "amount": 200.0}


def _env_row():
    return {"limit_up_count": 45, "max_board": 4, "turnover": 1.1e12,
            "up_down_ratio": 2.0, "fail_rate": 0.25, "index_ma20": 3000.0}


def _lu_row(code, boards, reason, seal=1.0e6):
    return {"code": code, "name": "n" + code, "boards": boards,
            "first_seal_time": "09:40", "seal_amount": seal, "reason": reason}


def _dt_row(code, net=1.2):
    return {"code": code, "name": "n" + code, "seat": "机构专用",
            "buy": net + 0.2, "sell": 0.2, "net": net}


def _news_row(nid, codes):
    return {"news_id": nid, "ts": "2026-10-08 08:00:00", "source": "测试源",
            "title": "t", "body": "b", "related_codes": codes}


def _feed_raw(sb, date, sector, capital, hot, fmv):
    """ext 投放件落沙盒 raw 区（loader.load_ext 经 base=sb 读取）。"""
    d = os.path.join(sb, "data", "raw", date)
    os.makedirs(d)
    _dump_jsonl(os.path.join(d, "sector.jsonl"), sector)
    _dump_jsonl(os.path.join(d, "capital_flow.jsonl"), capital)
    _dump_jsonl(os.path.join(d, "hot_search.jsonl"), hot)
    _dump_jsonl(os.path.join(d, "float_mv.jsonl"), fmv)


def _dump_jsonl(p, rows):
    with io.open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _feed_decl(sb, date, gaps, extra_raw=None):
    """在沙盒投放区落一份**合成声明件**（自检用，非生产）。

    `extra_raw` 非 None ⇒ 原样写入该文本（用于制造"非法 JSON"负样本）。
    """
    d = os.path.join(sb, "data", "raw", date)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, ING.DECL_FNAME)
    txt = extra_raw if extra_raw is not None else json.dumps(
        {"schema": 1, "kind": "EXPLICIT_DECLARATION_SEED", _GAP_SEG: gaps},
        ensure_ascii=False)
    io.open(p, "w", encoding="utf-8").write(txt)
    return p


def _selftest():
    fails = [0]

    def T(name, cond):
        if cond:
            print("  [✓] %s" % name)
        else:
            fails[0] += 1
            print("  [✗] %s" % name)

    print("scan 自检（全临时沙盒，真库零接触）：")
    sb, pp, pool, mainl = _sandbox_sb()

    D1, D2 = "2026-10-08", "2026-10-09"

    # ── B 组：mainline 机械归并 ──
    mlp = os.path.join(sb, "ml", "mainline.json")
    os.makedirs(os.path.dirname(mlp))
    lu1 = [_lu_row("300001", 3, "机器人+减速器"),
           _lu_row("600519", 1, "白酒"),
           _lu_row("000001", 1, "车")]              # 1 字 token → unmapped
    lg, mr = mainline_update(D1, lu1, path=mlp, params_path=pp)
    T("B1 新线建立 L:机器人 / L:减速器 / L:白酒，DayN=1 active",
      set(lg["lines"]) == {"L:机器人", "L:减速器", "L:白酒"}
      and lg["lines"]["L:机器人"]["state"] == "active"
      and lg["lines"]["L:机器人"]["day_n"] == 1)
    T("B2 短 token 计 unmapped、不成线（min_len=2 来自 params）",
      mr["unmapped"] == 1 and "L:车" not in lg["lines"])
    T("B3 成员进入 hits=1 first_seen",
      lg["lines"]["L:机器人"]["members"]["300001"]["hits"] == 1
      and lg["lines"]["L:机器人"]["members"]["300001"]["first_seen"] == D1)
    T("B4 一标的多线如实记录（300001 ∈ 机器人+减速器）",
      "300001" in lg["lines"]["L:机器人"]["members"]
      and "300001" in lg["lines"]["L:减速器"]["members"])
    T("B5 reason 空 → unmapped 不入线（000001 无线）",
      not any("000001" in ln["members"] for ln in lg["lines"].values()))

    lg, mr = mainline_update(D1, lu1, path=mlp, params_path=pp)
    T("B6 同日重跑幂等（processed_upto 拦截，台账不重复计数）",
      mr.get("idempotent") and lg["lines"]["L:机器人"]["day_n"] == 1)

    import copy as _copy
    snap1 = _copy.deepcopy(json.load(io.open(mlp, encoding="utf-8")))
    mainline_update(D2, lu1, path=mlp, params_path=pp)
    lg2 = json.load(io.open(mlp, encoding="utf-8"))
    T("B7 次日命中 DayN=2 / hits=2 / first_seen 不变",
      lg2["lines"]["L:机器人"]["day_n"] == 2
      and lg2["lines"]["L:机器人"]["members"]["300001"]["hits"] == 2
      and lg2["lines"]["L:机器人"]["members"]["300001"]["first_seen"] == D1)
    T("B8 新增线落账、旧线不动（增量合并）",
      set(lg2["lines"]) == set(snap1["lines"]))

    # B9 dormant：改 params mainline_dormant_after_miss=1 → 一日 miss 即转 dormant（行为级双证）
    _set_param(pp, K_DORMANT, 1)
    lg, _m = mainline_update("2026-10-12", [_lu_row("600519", 1, "白酒")],
                             path=mlp, params_path=pp)
    T("B9 改 params dormant_after_miss=1 → 机器人线一日 miss 即 dormant（代码零改动）",
      lg["lines"]["L:机器人"]["state"] == "dormant"
      and lg["lines"]["L:白酒"]["state"] == "active")
    lg, _m = mainline_update("2026-10-13", lu1, path=mlp, params_path=pp)
    T("B10 dormant 线再命中复归 active 且 DayN 重计=1（历史 members 保留）",
      lg["lines"]["L:机器人"]["state"] == "active"
      and lg["lines"]["L:机器人"]["day_n"] == 1
      and lg["lines"]["L:机器人"]["members"]["300001"]["hits"] == 3)
    _set_param(pp, K_DORMANT, 2)      # 还原种子值

    ml_txt = io.open(mlp, encoding="utf-8").read()
    T("B11 台账纯度：只有成员/状态/计数，无判断字段（含 dormant 线不删行）",
      _has_no_judgment_words(ml_txt)
      and lg["lines"]["L:机器人"]["members"]["300001"]["first_seen"] == D1)

    # ── C 组：五路扫描全链（沙盒快照 + ext 投放件） ──
    sb2, pp2, pool2, _m2 = _sandbox_sb()
    CAL = _cal_sb()                       # B3：沙盒日历（供日状态闸；不落盘）
    _set_param(pp2, K_GATE, 200)      # 显式种子（亿元）
    lu = [_lu_row("300001", 3, "机器人+减速器"),      # 市值 86.5 ≤ 200 → 召回
          _lu_row("600003", 2, "机器人"),            # 市值 300 > 200 → 超闸漏召回
          _lu_row("600519", 1, "白酒")]              # 无市值数据 → 漏召回
    news = [_news_row("n1", ["300001"]), _news_row("n2", [])]
    dt = [_dt_row("600519", 1.2)]
    SN.write_snapshot(D1, _pay(lu, news, dt),
                      root=os.path.join(sb2, "data", "snapshot"))
    _feed_raw(sb2, D1,
              sector=[{"board": "机器人", "code": "300001", "board_pct": 3.1},
                      {"board": "芯片", "code": "600002", "board_pct": 5.0},
                      {"board": "白酒", "code": "600519", "board_pct": 2.0}],
              capital=[{"code": "300001", "net_in": 1.5},
                       {"code": "600002", "net_in": -0.5}],
              hot=[{"code": "600002", "kw": "芯片"}],
              fmv=[{"code": "300001", "float_mv": 86.5},
                   {"code": "600003", "float_mv": 300.0}])

    rep = scan(D1, base=sb2, params_path=pp2, pool_path=pool2, mainline_path=_m2,
               calendar=CAL)
    T("C1 五路全可用 → 报告非空、报告含五路键",
      rep is not None and set(rep["routes"]) == set(ROUTES))
    T("C2 300001 五路事实聚合（limit_up+sector_flow+news_hot+mainline_recall）",
      "300001" in rep["codes"]
      and _pool_source(pool2, "300001") == "limit_up|sector_flow|news_hot|mainline_recall")
    T("C3 候选 DISCOVERED + source/trigger/created_at 必带（锁死条款①）",
      _pool_field(pool2, "300001", "state") == "DISCOVERED"
      and bool(_pool_field(pool2, "300001", "source"))
      and bool(_pool_field(pool2, "300001", "trigger")["text"])
      and _pool_field(pool2, "300001", "created_at") == D1)
    T("C4 600519 命中 limit_up+sector_flow(白酒在榜)+dragon_tiger（龙虎榜 net 事实入 trigger）",
      _pool_source(pool2, "600519") == "limit_up|sector_flow|dragon_tiger"
      and "龙虎榜" in _pool_field(pool2, "600519", "trigger")["text"])
    T("C5 600002 在榜+热搜命中，但 net_in<0 不产资金事实（负净流入非事实命中）",
      _pool_source(pool2, "600002") == "sector_flow|news_hot"
      and "资金净流入" not in _pool_field(pool2, "600002", "trigger")["text"])
    T("C6 市值闸：600003 超 200 亿 → mainline_recall 跳过（skipped_over_gate=1）",
      rep["skipped_over_gate"] == 1
      and "mainline_recall" not in _pool_source(pool2, "600003"))
    T("C7 无市值数据 → 跳过漏召回（600519 skipped_no_mv，宁可漏不猜）",
      rep["skipped_no_mv"] == 1
      and "mainline_recall" not in _pool_source(pool2, "600519"))
    T("C8 台账 mainline 归并随扫描发生（L:机器人 成员含 300001/600003）",
      set(json.load(io.open(_m2, encoding="utf-8"))["lines"]["L:机器人"]["members"])
      == {"300001", "600003"})

    rep2 = scan(D1, base=sb2, params_path=pp2, pool_path=pool2, mainline_path=_m2,
                calendar=CAL)
    T("C9 同日重跑：候选不重复建（DUPLICATE 只更 last_seen），created 不变",
      rep2["created"] == 0 and rep2["duplicate"] == rep["created"])

    r_adv = CAND.advance_candidate("2026-10-16", path=pool2)   # 超 5 交易日观察窗
    T("C10 超观察窗 → EXPIRED 且行保留（锁死条款③负样本留档）",
      _pool_field(pool2, "300001", "state") == "EXPIRED"
      and _pool_count(pool2) == rep["created"])

    T("C11 完全无数据沙盒 → scan 返回 None（fail-safe 禁编池）",
      _scan_empty_is_none(pp2, pool2, _m2, CAL))

    # C12 top_n 行为级：sector_scan_top_n=1 → 只有 board_pct 最高的芯片在榜
    sb3, pp3, pool3, ml3 = _sandbox_sb()
    _set_param(pp3, K_TOP_N, 1)
    lu3 = [_lu_row("300001", 1, "机器人")]
    SN.write_snapshot(D1, _pay(lu3, [], []),
                      root=os.path.join(sb3, "data", "snapshot"))
    _feed_raw(sb3, D1,
              sector=[{"board": "机器人", "code": "300001", "board_pct": 3.1},
                      {"board": "芯片", "code": "600002", "board_pct": 5.0}],
              capital=[], hot=[], fmv=[])
    rep3 = scan(D1, base=sb3, params_path=pp3, pool_path=pool3, mainline_path=ml3,
                calendar=_cal_sb())
    T("C12 改 params sector_scan_top_n=1 → 机器人(3.1)跌出在榜，300001 不经 sector 路（代码零改动）",
      _pool_source(pool3, "300001") == "limit_up"
      and rep3["routes"]["sector_flow"]["hit"] == 1)   # 600002 仍经芯片在榜命中

    # ── E 组：日期完整性闸（B3 P1 · 本轮新增） ──
    # E1 报告留痕：日状态与 anchor 可审计
    T("E1 报告含 day_status/day_exact/day_anchor（anchor=当日，可机械复核）",
      rep["day_status"] == "TRADING_WITH_DATA" and rep["day_exact"] is True
      and rep["day_anchor"] == D1 and rep["day_status_basis"].endswith("@test"))

    # E2 **核心负样本**：目标日无快照 ⇒ 即便"允许越过日状态闸"也不得回退取前一日
    sb4, pp4, pool4, ml4 = _sandbox_sb()
    _set_param(pp4, K_GATE, 200)
    SN.write_snapshot(D1, _pay([_lu_row("300001", 3, "机器人")], [], []),
                      root=os.path.join(sb4, "data", "snapshot"))
    _feed_raw(sb4, D1, sector=[], capital=[], hot=[], fmv=[])
    ml4_before = _ml_state(ml4)
    rep4 = scan(D2, base=sb4, params_path=pp4, pool_path=pool4, mainline_path=ml4,
                calendar=None, on_unknown="allow")
    T("E2 无日历例外下 D2 无快照 → None（**不拿 D1 行集冒充 D2**）",
      rep4 is None)
    T("E3 缺口日**零副作用**：候选池未增 ∧ 台账 processed_upto 零推进",
      _pool_count(pool4) == 0 and _ml_state(ml4) == ml4_before)
    v4 = day_verdict(D2, base=sb4, calendar=None)
    T("E4 day_verdict 对无日历目标日给 UNKNOWN + NO_CALENDAR（不推成休市）",
      v4["status"] == "UNKNOWN" and v4["reasons"] == ["NO_CALENDAR"]
      and v4["anchor"] is None and v4["data_date"] == D1)
    T("E5 带日历调 scan(D2) → None（TRADING_NO_DATA，日状态闸独立生效）",
      scan(D2, base=sb4, params_path=pp4, pool_path=pool4, mainline_path=ml4,
           calendar=_cal_sb()) is None)
    T("E6 带日历调 scan(D2) 后仍零副作用（候选 0 / 台账未动）",
      _pool_count(pool4) == 0 and _ml_state(ml4) == ml4_before)

    # E7 非交易日（日历证据）：10-09 移出日历 → NON_TRADING，且**不产生缺口**语义
    cal_hol = _cal_sb(exclude=("2026-10-09",))
    v7 = day_verdict("2026-10-09", base=sb4, calendar=cal_hol)
    T("E7 日历判非交易日 → NON_TRADING + anchor=None（与缺口可区分）",
      v7["status"] == "NON_TRADING" and v7["anchor"] is None
      and v7["reasons"] == ["CALENDAR_SAYS_NON_SESSION"])
    T("E8 非交易日调 scan → None（有数据也不放行，fail-closed）",
      scan("2026-10-09", base=sb4, params_path=pp4, pool_path=pool4,
           mainline_path=ml4, calendar=cal_hol) is None)

    # E9 缺键闸：数据日存在但只有 4 键（缺 env_metrics）→ TRADING_NO_DATA
    sb5, pp5, pool5, ml5 = _sandbox_sb()
    _set_param(pp5, K_GATE, 200)
    SN.write_snapshot(D1, _pay([_lu_row("300001", 3, "机器人")], [], []),
                      root=os.path.join(sb5, "data", "snapshot"))
    _feed_raw(sb5, D1, sector=[], capital=[], hot=[], fmv=[])
    _damage_snapshot(sb5, D1, "env_metrics.json")
    v9 = day_verdict(D1, base=sb5, calendar=_cal_sb())
    T("E9 数据日存在但缺键 → 降级 TRADING_NO_DATA + 列出缺键（缺就是缺）",
      v9["status"] == "TRADING_NO_DATA" and v9["missing_keys"] == ["env_metrics"]
      and v9["anchor"] is None)
    T("E10 缺键日调 scan → None（不做形式上的完整快照）",
      scan(D1, base=sb5, params_path=pp5, pool_path=pool5, mainline_path=ml5,
           calendar=_cal_sb()) is None)
    T("E10b `write_snapshot` 自身即拒 4 键负载（结构闸在写入前生效）",
      _snapshot_rejects_four_keys())

    # E11 台账连续性闸：缺口日不得被静默跳过
    mlp2 = os.path.join(sb4, "ml", "mainline2.json")
    os.makedirs(os.path.dirname(mlp2))
    mainline_update("2026-10-08", [_lu_row("300001", 1, "机器人")],
                    path=mlp2, params_path=pp4)
    ok_cont = True
    try:
        mainline_update("2026-10-13", [_lu_row("300001", 1, "机器人")],
                        path=mlp2, params_path=pp4, calendar=_cal_sb())
        ok_cont = False
    except ScanError as e:
        ok_cont = "连续性中断" in str(e) and "2026-10-09" in str(e)
    T("E11 跨缺口跳日（10-08 → 10-13）→ HALT（期望日 10-09 留在文案里）", ok_cont)
    ok_hol = True
    try:
        mainline_update("2026-10-09", [_lu_row("300001", 1, "机器人")],
                        path=mlp2, params_path=pp4, calendar=_cal_sb())
    except ScanError:
        ok_hol = False
    T("E12 相邻交易日（10-08 → 10-09）→ 照常归并（连续性闸不误伤）", ok_hol)
    mlp3 = os.path.join(sb4, "ml", "mainline3.json")
    mainline_update("2026-10-08", [_lu_row("300001", 1, "机器人")],
                    path=mlp3, params_path=pp4)
    ok_skip_hol = True
    try:
        mainline_update("2026-10-12", [_lu_row("300001", 1, "机器人")],
                        path=mlp3, params_path=pp4,
                        calendar=_cal_sb(exclude=("2026-10-09",)))
    except ScanError:
        ok_skip_hol = False
    T("E13 跨休市（10-08 → 10-12，10-09 休市）→ 判连续，不算缺口（不误伤）",
      ok_skip_hol)

    # E14 _date_guard 直测（is_exact 三态；None 亦阻断 = fail-closed）
    g_false = _date_guard(LD.Series([{"code": "x"}], date=D1, is_exact=False))
    g_true = _date_guard(LD.Series([{"code": "x"}], date=D1, is_exact=True))
    g_none = _date_guard(LD.Series([{"code": "x"}], date=D1, is_exact=None))
    g_un = _date_guard(LD.UNAVAILABLE)
    T("E14 _date_guard：is_exact=False/None 一律阻断，True 放行，UNAVAILABLE 记缺",
      g_false[0] is None and g_false[1]["stale_blocked"] is True
      and g_true[0] == [{"code": "x"}] and g_true[1]["data_date"] == D1
      and g_none[0] is None and g_none[1]["stale_blocked"] is True
      and g_un[0] is None and g_un[1]["unavailable"] is True)

    # E15 值域封闭：on_unknown 非法值 ⇒ 抛错（禁隐式默认）
    raised_ou = False
    try:
        scan(D1, base=sb4, params_path=pp4, pool_path=pool4, mainline_path=ml4,
             calendar=None, on_unknown="maybe")
    except ScanError:
        raised_ou = True
    T("E15 on_unknown 值域封闭（非法值 ⇒ ScanError，禁隐式默认）", raised_ou)

    # E16 沙盒日历零落盘：真库无 trading_calendar / data/caliber
    T("E16 本模块零落盘：真库无 trading_calendar.json / data/caliber",
      not os.path.exists(os.path.join(ROOT, "data", "state",
                                      "trading_calendar.json"))
      and not os.path.exists(os.path.join(ROOT, "data", "caliber")))

    # ── F 组：声明感知 · 已知缺陷日的显式标注（P1-3 · 本轮新增） ──
    # 用户 2026-10-13 裁定：缺陷日「**接受为可扫描日 + 显式标注**」——
    # 故本组全部断言都在验证「**照常扫描**」与「**标注同时到达**」两件事同时成立。
    sb6, pp6, pool6, ml6 = _sandbox_sb()
    _set_param(pp6, K_GATE, 200)
    CAL6 = _cal_sb()
    T("F1 无声明件的沙盒 → 零缺陷日 + 零证据（缺失退化为「无标注」，不抛错不阻断）",
      known_gap_days(base=sb6) == ({}, [])
      and declared_annotation(D1, base=sb6) == {
          "known_gap": False, "kind": None, "decl": None, "decl_sha256": None,
          "n_decl_files": 0})

    for _d in (D1, D2):
        SN.write_snapshot(_d, _pay([_lu_row("300001", 3, "机器人")], [], []),
                          root=os.path.join(sb6, "data", "snapshot"))
        _feed_raw(sb6, _d, sector=[], capital=[], hot=[], fmv=[])
    pdecl = _feed_decl(sb6, D1, {D1: "沙盒合成缺陷日（自检用，非生产）"})
    rep6 = scan(D1, base=sb6, params_path=pp6, pool_path=pool6,
                mainline_path=ml6, calendar=CAL6)
    ann6 = rep6["data_annotation"]
    T("F2 被标注日**照常扫描照常入池**（不阻断），报告带 known_gap/kind/指针",
      ann6["known_gap"] is True and ann6["kind"] == "DECLARED_KNOWN_GAP"
      and ann6["decl"] == "data/raw/2026-10-08/" + ING.DECL_FNAME
      and len(ann6["decl_sha256"]) == 64 and rep6["created"] == 1
      and rep6["routes"]["limit_up"]["hit"] == 1)

    led6 = json.load(io.open(ml6, encoding="utf-8"))
    T("F3 标注与 processed_upto **同一次写入**（原子）：越过该日 ⇔ 标注在册，"
      "二者不可只发生其一",
      led6["processed_upto"] == D1
      and set(led6[_ANN_SECTION][D1]) == {"kind", "decl", "sha256"}
      and led6["version"] == LEDGER_VERSION)

    ml6_txt = io.open(ml6, encoding="utf-8").read()
    T("F4 台账只存**指针 + 摘要**（禁复制正文 ⇒ 禁第二真源）："
      "sha256 == 声明件实算，正文不入账",
      led6[_ANN_SECTION][D1]["sha256"] == _sha256(pdecl)
      and _sha256(pdecl) == ann6["decl_sha256"]
      and "沙盒合成缺陷日" not in ml6_txt)
    T("F4b 标注段过台账纯度检查（出处事实 ≠ 判断字段；kind 值域封闭单值）",
      _has_no_judgment_words(ml6_txt)
      and {v["kind"] for v in led6[_ANN_SECTION].values()} == {"DECLARED_KNOWN_GAP"})

    _feed_decl(sb6, D2, {}, extra_raw="{ 这不是合法 JSON")
    rep7 = scan(D2, base=sb6, params_path=pp6, pool_path=pool6,
                mainline_path=ml6, calendar=CAL6)
    led7 = json.load(io.open(ml6, encoding="utf-8"))
    T("F5 未标注日**不落任何键**（缺席 ≠ 空：禁无条件写空段），"
      "且已标注日**不被抹掉**（标注只增不减）",
      rep7["data_annotation"]["known_gap"] is False
      and sorted(led7[_ANN_SECTION]) == [D1] and led7["processed_upto"] == D2)

    T("F6 损坏声明件 fail-safe：跳过该件、不抛错，其余件照读"
      "（单件损坏不得阻断全链扫描）",
      known_gap_days(base=sb6)[0] == {D1: "沙盒合成缺陷日（自检用，非生产）"}
      and len(known_gap_days(base=sb6)[1]) == 1)

    # F7 旧账升版：v1（无 data_annotations 段）读入即升 v2 —— 不静默扩 schema
    mlp7 = os.path.join(sb6, "ml", "mainline_v1.json")
    os.makedirs(os.path.dirname(mlp7))
    io.open(mlp7, "w", encoding="utf-8").write(json.dumps(
        {"version": 1, "updated_at": None, "processed_upto": None, "lines": {}}))
    mainline_update(D1, [_lu_row("300001", 3, "机器人")], path=mlp7,
                    params_path=pp6,
                    annotation=declared_annotation(D1, base=sb6))
    led_v1 = json.load(io.open(mlp7, encoding="utf-8"))
    T("F7 旧账 version=1 读入即升 2 且 lines 一字不动（升版只放宽可选段）",
      led_v1["version"] == LEDGER_VERSION
      and set(led_v1["lines"]) == {"L:机器人"}
      and led_v1["processed_upto"] == D1)

    # ── L 组：run_log 沙盒重定向（2026-10-13 泄漏修复的防回归） ──
    T("L1 无重定向标志 ⇒ None（真生产跑走 observe 生产默认）",
      _log_path_for(["--eval", "--as-of", "2026-09-29"]) is None)
    T("L2 --mainline 重定向 ⇒ 日志落同目录（跟随状态件）",
      _log_path_for(["--eval", "--mainline", "/tmp/x/mainline.json"])
      == os.path.join(os.path.abspath("/tmp/x"), "run_log.jsonl"))
    _SBX = os.path.join(os.path.abspath(os.sep), "sb")   # 平台无关沙盒根（禁写死盘符）
    T("L3 仅 --base ⇒ <base>/data/state/run_log.jsonl",
      _log_path_for(["--eval", "--base", _SBX])
      == os.path.join(_SBX, "data", "state", "run_log.jsonl"))
    T("L4 优先级 --mainline > --pool > --base（状态件语义一致）",
      _log_path_for(["--pool", "/p/pool.json", "--base", _SBX])
      == os.path.join(os.path.abspath("/p"), "run_log.jsonl"))

    # CLI 级集成：沙盒执行只写沙盒三件（pool/mainline/run_log），生产零接触
    import contextlib
    sb8, pp8, pool8, ml8 = _sandbox_sb()
    _set_param(pp8, K_GATE, 200)
    lu8 = [_lu_row("300001", 3, "机器人")]
    SN.write_snapshot(D1, _pay(lu8, [], []),
                      root=os.path.join(sb8, "data", "snapshot"))
    _feed_raw(sb8, D1,
              sector=[{"board": "机器人", "code": "300001", "board_pct": 3.1}],
              capital=[], hot=[],
              fmv=[{"code": "300001", "float_mv": 86.5}])
    calcsv8 = os.path.join(sb8, "trade_cal.csv")
    with io.open(calcsv8, "w", encoding="utf-8", newline="\n") as f:
        f.write("date,is_trade\n")
        for d in _SB_SESSIONS:
            f.write("%s,1\n" % d)
    buf8 = io.StringIO()
    with contextlib.redirect_stdout(buf8):
        rc8 = _main(["--eval", "--as-of", D1, "--base", sb8, "--params", pp8,
                     "--pool", pool8, "--mainline", ml8, "--calendar", calcsv8])
    slog = os.path.join(sb8, "pool", "run_log.jsonl")
    ok_log = False
    if os.path.isfile(slog):
        rows8 = [json.loads(x) for x in
                 io.open(slog, encoding="utf-8").read().strip().splitlines()]
        ok_log = (len(rows8) == 1 and rows8[0]["actor"] == ACTOR
                  and rows8[0]["as_of"] == D1 and rows8[0]["result"] == "DONE")
    T("L5 CLI 沙盒执行：run_log 落沙盒（恰 1 行 actor=scan/DONE）且 rc=0"
      "（rc=%s slog_in_sb=%s）" % (rc8, os.path.isfile(slog)),
      rc8 == 0 and ok_log)
    T("L6 CLI 沙盒执行：pool/mainline 落沙盒（pool 有候选、台账推进）",
      _pool_count(pool8) >= 1
      and json.load(io.open(ml8, encoding="utf-8"))["processed_upto"] == D1)

    # ── D 组：纪律 / 审计 ──
    T("D1 自检前后真库候选池/台账逐字节未变（全沙盒零接触）",
      _md5(os.path.join(ROOT, "data", "state", "candidates.json")) == _MD5_POOL
      and _md5(os.path.join(ROOT, "data", "state", "mainline.json")) == _MD5_ML)
    T("D2 真库无沙盒泄漏（data/raw 目录集合与 import 时一致）",
      _no_new_dirs())
    ml_txt2 = io.open(_m2, encoding="utf-8").read()
    T("D3 沙盒台账纯度：只含成员/状态/计数机械字段，无判断字段",
      _has_no_judgment_words(ml_txt2)
      and '"members"' in ml_txt2 and '"day_n"' in ml_txt2 and '"hits"' in ml_txt2)
    T("D4 _TEST_FUNCS 自报（纪律判据只扫生产区）",
      isinstance(_TEST_FUNCS, tuple) and "_selftest" in _TEST_FUNCS)
    T("D5 声明感知**纯只读**：自检前后真库全部声明件逐字节未变",
      _decl_fp(ROOT) == _DECL_FP_AT_IMPORT)
    T("D6 ★run_log 泄漏防回归：自检期生产 run_log 逐字节未变（零追加）",
      _md5(os.path.join(ROOT, "data", "state", "run_log.jsonl")) == _MD5_RLOG)

    total = _SELFTEST_N
    print("scan 自检：%d/%d" % (total - fails[0], total))
    return fails[0] == 0


_SELFTEST_N = 59          # B 11 + C 12 + E 17 + F 7 + L 6 + D 6


def _scan_empty_is_none(pp, pool, ml, calendar=None):
    """全新空沙盒（无快照无投放）→ 五路全 UNAVAILABLE → None。"""
    sb = tempfile.mkdtemp(prefix="ta_scan_empty_")
    return scan("2026-10-08", base=sb, params_path=pp, pool_path=pool,
                mainline_path=ml, calendar=calendar) is None


def _ml_state(path):
    """台账状态指纹（不存在 → None）：用于断言"零推进"（含未创建）。"""
    if not os.path.exists(path):
        return None
    d = json.load(io.open(path, encoding="utf-8"))
    return (d.get("processed_upto"), sorted(d.get("lines") or {}))


def _snapshot_rejects_four_keys():
    """结构闸实证：`write_snapshot` 拒收 4 键负载（故残缺态只能从磁盘侧制造）。"""
    sb = tempfile.mkdtemp(prefix="ta_scan_keys_")
    pay = _pay([], [], [])
    pay.pop("env_metrics")
    try:
        SN.write_snapshot("2026-10-08", pay,
                          root=os.path.join(sb, "data", "snapshot"))
    except SN.SnapshotError:
        return True
    return False


_MD5_POOL = None
_MD5_ML = None
_RAW_DIRS_AT_IMPORT = None


def _md5(path):
    import hashlib
    if not os.path.exists(path):
        return None
    return hashlib.md5(io.open(path, "rb").read()).hexdigest()


def _decl_fp(base):
    """全部声明件 (相对路径, sha256) 指纹（**只读**；无件 ⇒ 空元组）。

    D5 用：证明「声明感知」整轮自检**没有写过投放区**（声明件是留痕件，只许读）。
    """
    return tuple((_rel(p, base), _sha256(p)) for p in decl_files(base=base))


_DECL_FP_AT_IMPORT = _decl_fp(ROOT)


def _no_new_dirs():
    r = os.path.join(ROOT, "data", "raw")
    if not os.path.isdir(r):
        return True
    return sorted(os.listdir(r)) == _RAW_DIRS_AT_IMPORT


_RAW_DIRS_AT_IMPORT = sorted(os.listdir(os.path.join(ROOT, "data", "raw"))) \
    if os.path.isdir(os.path.join(ROOT, "data", "raw")) else []
_MD5_POOL = _md5(os.path.join(ROOT, "data", "state", "candidates.json"))
_MD5_ML = _md5(os.path.join(ROOT, "data", "state", "mainline.json"))
_MD5_RLOG = _md5(os.path.join(ROOT, "data", "state", "run_log.jsonl"))


def _pool_source(pool_path, code):
    return _pool_field(pool_path, code, "source")


def _pool_field(pool_path, code, field):
    d = json.load(io.open(pool_path, encoding="utf-8"))
    for c in d["candidates"]:
        if c["code"] == code:
            return c[field]
    return None


def _pool_count(pool_path):
    d = json.load(io.open(pool_path, encoding="utf-8"))
    return len(d["candidates"])


def _set_param(pp, key, val):
    d = json.load(io.open(pp, encoding="utf-8"))
    d["params"][key] = {"value": val, "unit": "count", "status": "BASELINE",
                        "validation": "UNCALIBRATED", "source": "TEST",
                        "last_review": "2026-10-08", "tightening_direction": "FREE"}
    io.open(pp, "w", encoding="utf-8").write(
        json.dumps(d, ensure_ascii=False, indent=2))


def _has_no_judgment_words(text):
    return not any(w in text for w in _JUDGMENT_WORDS)


_JUDGMENT_WORDS = ("看多", "看空", "推荐", "买点", "优先", "强" + "弱")


def _code_only(src):
    """去掉字符串字面量与注释后的**代码词流**（供纪律② 精确判据）。

    原判据在裸文本上做子串匹配，会被**契约术语**误伤：`TRADING_NO_DATA` /
    `NON_TRADING` 里含 "NO"，但它们是日期口径名，不是决策动作。
    故判据改为「去 STRING/COMMENT 后按词边界匹配」，判的是**代码里出现的动作词**。
    """
    import io as _io
    import tokenize
    parts = []
    for tok in tokenize.generate_tokens(_io.StringIO(src).readline):
        if tok.type in (tokenize.STRING, tokenize.COMMENT):
            continue
        parts.append(tok.string)
    return " ".join(parts)


# ── 纪律自检（生产区判据；结构沿用 C1 regime） ───────────────────────────────
def _discipline(verbose=True):
    src_path = os.path.abspath(__file__)
    src = io.open(src_path, encoding="utf-8").read()
    tree = ast.parse(src)
    fixtures = ("_lu_row", "_dt_row", "_news_row", "_quote_row", "_env_row",
                "_pay", "_feed_raw", "_dump_jsonl", "_set_param", "_sandbox_sb",
                "_feed_decl")
    prod = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name not in _TEST_FUNCS \
                and n.name not in fixtures:
            prod.extend(range(n.lineno, ASTW.bounds(n)[1] + 1))
    prod_src = "\n".join(src.split("\n")[i - 1] for i in sorted(prod))
    res = [0]

    def T(name, cond):
        if cond:
            res[0] += 1
            if verbose:
                print("  [✓] %s" % name)
        elif verbose:
            print("  [✗] %s" % name)

    lit = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and (n.name in _TEST_FUNCS or n.name in fixtures):
            continue
        for node in ast.walk(n):
            if isinstance(node, ast.Compare):
                for v in [node.left] + list(node.comparators):
                    if isinstance(v, ast.Constant) and isinstance(v.value, (int, float)) \
                            and not isinstance(v.value, bool) and v.value not in (0, 1):
                        lit.append("%s:%d" % (n.name, node.lineno))
                        break
    T("① 零字面量阈值比较（阈值只能在 params；0/1 结构基数豁免）", not lit)
    import re as _re
    code_src = _code_only(prod_src)
    acts = [w for w in ("BU" + "Y", "WA" + "IT", "N" + "O")
            if _re.search(r"\b%s\b" % w, code_src)]
    T("② 零决策词（代码区按词边界判，无 BUY/WAIT/NO；口径名不算动作）", not acts)
    T("③ 零判断字段写台账（台账词表 = 成员/状态/计数 + 出处标注（指针+摘要），别无他物）",
      _has_no_judgment_words(" ".join(_LEDGER_WORDS)))
    T("④ 五路 source 值域封闭（ROUTES 常量 = 1.2 定义）", ROUTES == (
        "limit_up", "sector_flow", "dragon_tiger", "news_hot", "mainline_recall"))
    T("⑤ 写点唯一：候选池零直写（生产区无 candidates._save 调用）",
      "_save" not in prod_src.replace("_save_mainline", ""))
    return not lit and res[0] == 5


_LEDGER_WORDS = ("token", "state", "first_seen", "last_seen", "last_hit",
                 "day_n", "miss_streak", "members", "hits", "lines",
                 "version", "updated_at", "processed_upto",
                 _ANN_SECTION, _ANN_KIND, "kind", "decl", "sha256")


def _log_path_for(argv):
    """run_log 沙盒重定向判据（纯函数 · 2026-10-13 泄漏修复）。

    ★ 根因：CLI 把 `--pool/--mainline/--base` 传给了 `scan()`，但 `run_observed`
      未传 `log_path` ⇒ 观测日志**恒写生产** `data/state/run_log.jsonl`
      （干跑 4 日在沙盒池/台账隔离下仍向生产追加了 4 行）。
    ★ 规则：run_log **跟随状态文件**——`--mainline` / `--pool` 被重定向 ⇒ 日志落
      同目录（与 pool/mainline 同为状态件，语义一致）；仅 `--base` 重定向 ⇒
      落 `<base>/data/state/`。都未重定向（真生产跑）⇒ None ⇒ observe 生产默认。
      反向推论也成立：pool/mainline 仍指生产时，日志也指生产（同生共死）。
    """
    for flag in ("--mainline", "--pool"):
        if flag in argv:
            d = os.path.dirname(os.path.abspath(argv[argv.index(flag) + 1]))
            return os.path.join(d, "run_log.jsonl")
    if "--base" in argv:
        return os.path.join(argv[argv.index("--base") + 1],
                            "data", "state", "run_log.jsonl")
    return None


def _main(argv):
    if "--selftest" in argv:
        return 0 if _selftest() else 1
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    if "--eval" in argv:
        as_of = argv[argv.index("--as-of") + 1]
        kw = {}
        if "--base" in argv:
            kw["base"] = argv[argv.index("--base") + 1]
        if "--params" in argv:
            kw["params_path"] = argv[argv.index("--params") + 1]
        if "--pool" in argv:
            kw["pool_path"] = argv[argv.index("--pool") + 1]
        if "--mainline" in argv:
            kw["mainline_path"] = argv[argv.index("--mainline") + 1]
        cal = None
        if "--calendar" in argv:
            cal = DC.Calendar.from_seed_csv(argv[argv.index("--calendar") + 1])
            kw["calendar"] = cal
        if "--allow-unknown" in argv:
            kw["on_unknown"] = "allow"        # 显式例外（无日历历史回放沙盒）
        # 日状态**先于**扫描单独判定并留痕：让 CLI 能精确报"为什么没扫"
        v = day_verdict(as_of, base=kw.get("base"), calendar=cal)
        print("日状态: %s（exact=%s anchor=%s 依据=%s reasons=%s）"
              % (v["status"], v["exact"], v["anchor"], v["calendar_basis"],
                 v["reasons"]))
        # P1-3 声明感知：标注**先于**扫描打印（与 scan 内部同源，同一次只读求值）
        ann = declared_annotation(as_of, base=kw.get("base"))
        if ann["known_gap"]:
            print("数据标注: %s 系**声明件明文登记的已知缺陷日**（kind=%s）—— "
                  "照常扫描，但该日结论**不参与完整性验收**；"
                  "下游读台账须连同 `%s` 一并读。" % (as_of, ann["kind"], ann["decl"]))
            print("          证据 = %s（sha256 %s…；声明件共 %d 件，判据取全量并集）"
                  % (ann["decl"], (ann["decl_sha256"] or "")[:16], ann["n_decl_files"]))
        else:
            print("数据标注: 无（%s 未被任何声明件登记为已知缺陷日；"
                  "已核 %d 件声明件）" % (as_of, ann["n_decl_files"]))
        if v["status"] != "TRADING_WITH_DATA" and not (
                v["status"] == "UNKNOWN" and kw.get("on_unknown") == "allow"):
            print("SKIP：日状态非 TRADING_WITH_DATA —— 不扫描、不产候选、不推进台账")
            if v["data_date"]:
                print("      （注意：本地最近一日数据为 %s，**禁止**用它顶替 %s）"
                      % (v["data_date"], as_of))
            return 0
        r = OBS.run_observed(ACTOR, "scan", "C2",
                             lambda: scan(as_of, **kw), as_of=as_of,
                             detail="五路发现扫描", raise_on_error=False,
                             log_path=_log_path_for(argv))
        if not r or r["error"] is not None:
            err = r["error"] if r else "run_observed 无返回"
            print("扫描：FAIL（%s）—— 不写池，禁编数据" % err)
            return 1
        if r["value"] is None:
            print("EMPTY（五路全取数报缺——禁编池；fail-safe 端到端实证）")
            return 0
        print(json.dumps(r["value"], ensure_ascii=False, indent=2))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
