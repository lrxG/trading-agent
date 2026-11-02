#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""loader.py · 数据统一入口（施工方案 0.10 · v1.0 · 2026-10-08 建）

用途：全系统**唯一取数出口**。C3/C4 判断与 S/E 插件一律调本模块，**禁自连数据源**
      （0.10 铁律）。本模块自己也不连数据源 —— 它只读磁盘上已落好的文件，
      所以每个数据源都成了可插拔的投放区，而不是焊死在代码里的调用。

三条腿（三个时态，互不混用）：
  · 盘后腿  历史/步进/回测/复盘 —— 走 data/snapshot/<date>/，天然满足 as_of ≤ T
  · 盘中腿  实时决策 —— 走 data/raw/_live/，独立接口、**零快照写入**、不推进游标
  · 盘前腿  第三时态 —— 「≤T-1 收盘快照 + 当日盘前投放」，**禁取当日收盘快照**
                        （当日收盘快照收盘后才生成，盘前取到 = 未来泄漏窗口）

★ 时序纪律（2.10 铁律，本模块逐条落实）：**所有 `load_*` 签名第 1 位恒为时序参数 `as_of`**
  。调用者永远先声明"我站在哪一天"，杜绝「有的接口日期在前、有的在后」导致的调用事故。
  `as_of` 只约束**时态**（能看到多新），不要求那天恰好有数据 —— 落在非交易日 / 未采集日
  会回退到最近一个 ≤as_of 的快照。**实际取到哪一天，由返回值自带的 `.date` 告知**
  （契约③：返回结构写死，调用者不许猜；想先核一日用 `resolve()`）。

★ 目录参数只有一个：`base`（默认 = 项目根 ROOT）。四个数据根一律由它派生 ——
  `base/data/{snapshot,raw,generated,knowledge}`。**禁一个参数承载多种含义**
  （v1.0 初稿曾让 `root` 在不同函数里分别指快照根 / 投放根，直接串目录，已根治）：
  好处是回测沙盒 / 自检只需把 base 指到一份临时 data 树，全系统照跑。

返回值约定（契约③）：
  · 列表类 → `Series`（list 子类）：list 用法不变，额外带 `.date` / `.is_exact`
  · 字典类 → `Record`（dict 子类）：键名与 0.2 契约**完全一致**（属性不进 keys），额外带 `.date` / `.is_exact`
  · 数据缺口 → `UNAVAILABLE`（与 0.2 同一哨兵对象：`x is UNAVAILABLE` 为真、布尔为假）
  · `load_quote` / `load_live` 特例：**源整个缺席 → UNAVAILABLE；源在但无该票 → None**（含义不同，别混）

数据缺口处理纪律（0.10 ③）：调用方遇 `UNAVAILABLE` 必须按缺口处理（标缺口 / 降置信 /
  拒绝出结论），**禁静默降级成空值走完流程**，**禁 LLM 补数**。要知道缺在哪 → `diagnose()`。

插入式扩展点（用户令 2026-10-08：骨架先定、插件后插）：
  · 新增数据类型 = 往 `data/raw/<date>/` 放 `<type>.json|jsonl` → 调 `load_ext("<type>", as_of)`
    **本模块零改动**（10 个具名接口只是薄封装，供契约固定；新类型走 load_ext）
  · 接真实采集器 / 实时通道 = 采集器往投放区写文件 → **本模块零改动**
  · 禁在本模块内直连数据源、禁用 LLM 补数、禁自造字段（`--discipline` 可自证）

落点：`tools/fs/loader.py`（与 0.1 cursor.py / 0.2 snapshot.py 同目录；0.10 原写 `tools/data/`
      为笔误，与 0.1/0.2 及「全项目文件结构总表」打架，已在施工 v3.5 收口）

用法：
  python tools/fs/loader.py                 # 数据源现状（只读）
  python tools/fs/loader.py --selftest      # 内置验收（临时 data 树，不碰真数据）
  python tools/fs/loader.py --discipline    # 纪律自检：全库有无直连数据源 / LLM 补数
  from loader import load_quote, load_asof
"""
import ast
import datetime as _dt
import io
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from paths import ROOT  # noqa: E402
import snapshot as S  # noqa: E402   （复用 0.2：快照读写 + 契约，禁再抄一份）

SCHEMA_VERSION = 1

LIVE_DIRNAME = "_live"                  # 盘中投放区（无日期，按文件新鲜度判）
LIVE_TTL_SECONDS = 180                  # 盘中数据最长容忍年龄（秒）；超龄 = 数据缺口

# 单一哨兵：直接复用 0.2 的对象 —— 全系统只有一种"数据缺口"判据，禁第二个哨兵
UNAVAILABLE = S.UNAVAILABLE
SnapshotError = S.SnapshotError

# 核心 5 文件已占用的 type 名（扩展投放不得撞名，防两套口径）
_CORE_KEYS = tuple(spec["key"] for spec in S.CONTRACT.values())
_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_CODE_RE = re.compile(r"^(sh|sz|bj)?\d{6}$", re.I)

# 0.10 具名数据类型契约（薄封装；新类型请走 load_ext，勿在本模块加函数）
EXT_TYPES = ("sector", "capital_flow", "hot_search", "financial", "consensus",
             "governance", "chip", "auction", "intraday_history", "policy")


# ── 目录派生（唯一入口：一个 base 派生四根，禁各函数自己拼路径） ───────────────
def roots(base=None):
    """→ Record{snapshot,raw,generated,knowledge}。base 默认项目根（回测沙盒只需换 base）。"""
    b = base or ROOT
    return Record({
        "snapshot": os.path.join(b, "data", "snapshot"),
        "raw": os.path.join(b, "data", "raw"),
        "generated": os.path.join(b, "data", "generated"),
        "knowledge": os.path.join(b, "data", "knowledge"),
    }, kind="roots")


# ── 返回结构（契约③：带出处，调用者不许猜） ──────────────────────────────────
class Series(list):
    """列表类返回：list 的全部用法不变，额外携带出处。"""
    __slots__ = ("date", "is_exact", "kind", "note")

    def __init__(self, items=(), date=None, is_exact=None, kind=None, note=None):
        list.__init__(self, items)
        self.date, self.is_exact, self.kind, self.note = date, is_exact, kind, note

    @property
    def source_date(self):
        return self.date


class Record(dict):
    """字典类返回：键名严格等于契约键（属性不进 keys），额外携带出处。"""
    __slots__ = ("date", "is_exact", "kind", "note")

    def __init__(self, *a, **kw):
        date = kw.pop("date", None)
        is_exact = kw.pop("is_exact", None)
        kind = kw.pop("kind", None)
        note = kw.pop("note", None)
        dict.__init__(self, *a, **kw)
        self.date, self.is_exact, self.kind, self.note = date, is_exact, kind, note

    @property
    def source_date(self):
        return self.date


def _wrap(obj, date, is_exact, kind=None, note=None):
    """按容器类型套出处（list→Series / dict→Record；其它原样返回）。"""
    if isinstance(obj, list):
        return Series(obj, date=date, is_exact=is_exact, kind=kind, note=note)
    if isinstance(obj, dict):
        return Record(obj, date=date, is_exact=is_exact, kind=kind, note=note)
    return obj


# ── 小工具 ───────────────────────────────────────────────────────────────────
def _chk_date(as_of):
    """日期硬校验（YYYY-MM-DD）→ 规范字符串；非法即抛错（禁静默按字符串比较）。"""
    s = as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of or "")
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise SnapshotError("as_of 必须是 YYYY-MM-DD，收到: %r" % (as_of,))
    try:
        _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        raise SnapshotError("as_of 日期非法: %r" % (as_of,))
    return s


def _norm_code(code):
    """代码归一：去空白、前缀小写；非法即抛错（防静默查不到）。"""
    s = "" if code is None else str(code).strip()
    if not _CODE_RE.match(s):
        raise SnapshotError("股票代码非法（须 6 位数字，可带 sh/sz/bj 前缀）: %r" % (code,))
    return s.lower()


def _same_code(a, b):
    """代码比较：忽略前缀差异（sh600000 == 600000）。"""
    if a is None or b is None:
        return False
    x, y = str(a).strip().lower(), str(b).strip().lower()
    if x == y:
        return True
    return len(x) >= 6 and len(y) >= 6 and x[-6:] == y[-6:]


def _check_type(type_name):
    t = str(type_name or "").strip()
    if not _TYPE_RE.match(t):
        raise SnapshotError("数据类型名非法（须小写 snake_case）: %r" % (type_name,))
    if t in _CORE_KEYS:
        raise SnapshotError("数据类型名 %r 与 0.2 核心契约键撞名 —— 防两套口径，禁复用" % t)
    return t


def _raw_dates(base=None, upto=None):
    """投放区已存在的日期目录（升序）；upto 给定则只取 ≤upto（= 物理截断）。"""
    r = roots(base)["raw"]
    if not os.path.isdir(r):
        return []
    out = []
    for n in os.listdir(r):
        if len(n) == 10 and n[4] == "-" and n[7] == "-" and os.path.isdir(os.path.join(r, n)):
            if upto is None or n <= upto:
                out.append(n)
    return sorted(out)


def _ext_file(type_name, date, base=None):
    """某日投放区里该类型的文件路径（.json 优先）；无 → None。"""
    d = os.path.join(roots(base)["raw"], date)
    for ext in (".json", ".jsonl"):
        p = os.path.join(d, type_name + ext)
        if os.path.isfile(p):
            return p
    return None


# ── 盘后腿（走快照，天然 ≤ as_of） ────────────────────────────────────────────
def resolve(as_of, base=None):
    """本次 as_of 实际会命中哪一日快照 → Record{date,is_exact}；无 → UNAVAILABLE。"""
    snap = S.load_asof(as_of, root=roots(base)["snapshot"])
    if snap is UNAVAILABLE:
        return UNAVAILABLE
    return Record({"date": snap["date"], "is_exact": snap.get("is_exact")}, kind="resolve")


def load_snapshot(as_of, base=None):
    """≤as_of 的最新一日全量快照 → Record{5 契约键 + date + is_exact}；无 → UNAVAILABLE。"""
    snap = S.load_asof(as_of, root=roots(base)["snapshot"])
    if snap is UNAVAILABLE:
        return UNAVAILABLE
    return Record(snap, date=snap["date"], is_exact=snap.get("is_exact"), kind="snapshot")


def load_quote(as_of, code, base=None):
    """单票日线 → Record{code,name,open,high,low,close,pct,vol,amount}。

    **快照整个缺席 → UNAVAILABLE（数据缺口）；快照在但该票不在内 → None（该日无此票）**。
    """
    c = _norm_code(code)
    snap = S.load_asof(as_of, root=roots(base)["snapshot"])
    if snap is UNAVAILABLE:
        return UNAVAILABLE
    for row in snap["daily_quote"]:
        if _same_code(row.get("code"), c):
            return Record(row, date=snap["date"], is_exact=snap.get("is_exact"), kind="quote")
    return None


def load_limit_up(as_of, base=None):
    """涨停/连板清单 → Series[{code,name,boards,first_seal_time,seal_amount,reason}]。

    该日真实无涨停 → 空 Series（长度 0）；快照缺席 → UNAVAILABLE。两者含义不同。
    """
    snap = S.load_asof(as_of, root=roots(base)["snapshot"])
    if snap is UNAVAILABLE:
        return UNAVAILABLE
    return Series(snap["limit_up"], date=snap["date"], is_exact=snap.get("is_exact"), kind="limit_up")


def load_dragon_tiger(as_of, base=None):
    """龙虎榜 → Series[{code,name,seat,buy,sell,net}]。"""
    snap = S.load_asof(as_of, root=roots(base)["snapshot"])
    if snap is UNAVAILABLE:
        return UNAVAILABLE
    return Series(snap["dragon_tiger"], date=snap["date"], is_exact=snap.get("is_exact"),
                  kind="dragon_tiger")


def load_news(as_of, base=None):
    """消息快照 → Series[{news_id,ts,source,title,body,related_codes}]（盘后/历史口径）。

    盘前"当日消息"不在快照里（快照盘后才生成）—— 请走 `load_premarket()`。
    """
    snap = S.load_asof(as_of, root=roots(base)["snapshot"])
    if snap is UNAVAILABLE:
        return UNAVAILABLE
    return Series(snap["news"], date=snap["date"], is_exact=snap.get("is_exact"), kind="news")


def load_env_metrics(as_of, base=None):
    """环境指标 → Record{limit_up_count,max_board,turnover,up_down_ratio,fail_rate,index_ma20}。

    键名严格等于 0.2 契约（判断类消费方直接当 regime 入参，禁再改名）。
    """
    snap = S.load_asof(as_of, root=roots(base)["snapshot"])
    if snap is UNAVAILABLE:
        return UNAVAILABLE
    return Record(snap["env_metrics"], date=snap["date"], is_exact=snap.get("is_exact"),
                  kind="env_metrics")


def load_premarket(as_of, base=None):
    """★ 盘前腿（第三时态）→ Record{close_date,is_exact_close,snapshot,env_metrics,news,auction}。

    组成 =「≤as_of-1 的最近收盘快照」+「as_of 当日的盘前投放（消息 / 竞价）」，逐个只在场才算数：
      · snapshot / env_metrics 缺席 → 骨架不在 → 整个调用返回 UNAVAILABLE
      · news(当日盘前) / auction(当日竞价) 缺席 → 该键单独为 UNAVAILABLE（不拖累其余）

    ★ 硬闸：收盘快照日必须 < as_of —— 盘前读当日收盘快照 = 未来泄漏（2.10 写死禁项）。
      若 ≤as_of-1 无快照、却存在 as_of 当日快照，本函数**不取它**，返回 UNAVAILABLE。
    """
    d = _chk_date(as_of)
    prev = (_dt.date(int(d[:4]), int(d[5:7]), int(d[8:10])) - _dt.timedelta(days=1)).isoformat()
    snap = S.load_asof(prev, root=roots(base)["snapshot"])
    if snap is UNAVAILABLE:
        return UNAVAILABLE
    if snap["date"] >= d:                     # 物理硬闸（截断是锁①，不是约定）
        raise SnapshotError(
            "盘前腿取到了非盘前数据：as_of=%s，快照日=%s（盘前只准用 ≤%s）" % (d, snap["date"], prev))
    rec = Record({
        "close_date": snap["date"],
        "is_exact_close": snap.get("is_exact"),
        "snapshot": Record(snap, date=snap["date"], is_exact=snap.get("is_exact"), kind="snapshot"),
        "env_metrics": _wrap(snap["env_metrics"], snap["date"], snap.get("is_exact"), "env_metrics"),
        "news": _raw_ext("news", d, base=base),        # 当日盘前消息（投放区）
        "auction": _raw_ext("auction", d, base=base),  # 当日竞价（投放区）
    }, kind="premarket")
    rec.date, rec.is_exact = snap["date"], snap.get("is_exact")
    return rec


# ── 盘中腿（实时；零快照写入） ────────────────────────────────────────────────
def _live_read(fname, ttl=None, base=None):
    """读盘中投放文件并校验新鲜度 → (payload, mtime_iso) 或 UNAVAILABLE。

    新鲜度判据：文件 mtime 距今 > ttl 秒即判超龄（ttl 传负值 = 强制视为超龄，供自检/停机）。
    超龄绝不拿旧读数当实时（防"静默空转当有数"）。
    """
    p = os.path.join(roots(base)["raw"], LIVE_DIRNAME, fname)
    if not os.path.isfile(p):
        return UNAVAILABLE
    ttl = LIVE_TTL_SECONDS if ttl is None else int(ttl)
    age = _dt.datetime.now().timestamp() - os.path.getmtime(p)
    if age > ttl:
        return UNAVAILABLE
    try:
        with io.open(p, encoding="utf-8-sig") as f:
            payload = json.load(f)
    except Exception as e:
        raise SnapshotError("盘中投放文件读取失败 %s：%s" % (p, e))
    ts = _dt.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M:%S")
    return payload, ts


def load_live(code, ttl=None, base=None):
    """实时单票 → Record（`data/raw/_live/quotes.json`，形如 {code:{...}} 或 [{code:...}]）。

    无投放 / 超龄 → UNAVAILABLE；源在但无该票 → None。
    **只读，绝不写快照**（盘中数据不得混入历史结算，0.10 写死）。
    """
    c = _norm_code(code)
    got = _live_read("quotes.json", ttl=ttl, base=base)
    if got is UNAVAILABLE:
        return UNAVAILABLE
    payload, ts = got
    row = None
    if isinstance(payload, dict):
        for k, v in payload.items():
            if _same_code(k, c):
                row = v
                break
    elif isinstance(payload, list):
        for v in payload:
            if isinstance(v, dict) and _same_code(v.get("code"), c):
                row = v
                break
    if row is None:
        return None                       # 同 load_quote：源在但无此票 ≠ 源缺席
    if not isinstance(row, dict):
        raise SnapshotError("盘中单票结构非法（须对象）: %r" % (row,))
    rec = Record(row, kind="live")
    rec.date, rec.is_exact, rec.note = ts[:10], True, "live ts=%s" % ts
    return rec


def load_live_market(ttl=None, base=None):
    """实时全市场 → Record（`data/raw/_live/market.json`）。无投放 / 超龄 → UNAVAILABLE。"""
    got = _live_read("market.json", ttl=ttl, base=base)
    if got is UNAVAILABLE:
        return UNAVAILABLE
    payload, ts = got
    if not isinstance(payload, dict):
        raise SnapshotError("盘中全市场结构非法（须对象）: %r" % (type(payload),))
    rec = Record(payload, kind="live_market")
    rec.date, rec.is_exact, rec.note = ts[:10], True, "live ts=%s" % ts
    return rec


# ── 扩展数据类型（插入式：新类型只放文件，本模块零改动） ──────────────────────
def _raw_ext(type_name, date, base=None):
    """精确读某日投放区 `<type>.json|jsonl`；不存在 → UNAVAILABLE。"""
    p = _ext_file(type_name, date, base)
    if not p:
        return UNAVAILABLE
    try:
        if p.endswith(".json"):
            with io.open(p, encoding="utf-8-sig") as f:
                return _wrap(json.load(f), date, True, type_name)
        rows = []
        with io.open(p, encoding="utf-8-sig") as f:
            for i, line in enumerate(f, 1):
                s = line.strip()
                if not s:
                    continue
                try:
                    rows.append(json.loads(s))
                except Exception as e:
                    raise SnapshotError("投放文件 %s 第 %d 行非法 JSON：%s" % (p, i, e))
        return Series(rows, date=date, is_exact=True, kind=type_name)
    except SnapshotError:
        raise
    except Exception as e:
        raise SnapshotError("投放文件读取失败 %s：%s" % (p, e))


def load_ext(type_name, as_of, code=None, base=None):
    """★ 插入式扩展点：按类型名找「≤as_of 的最近一日」投放文件，无需改本模块。

    `data/raw/<date>/<type>.json|jsonl`；带 code 则按 code 过滤/取值。
    新增数据类型 = 采集器往投放区放文件 → 调 `load_ext("<type>", as_of)`，**loader.py 零改动**。
    """
    t = _check_type(type_name)
    d0 = _chk_date(as_of)
    for d in sorted(_raw_dates(base, upto=d0), reverse=True):
        got = _raw_ext(t, d, base=base)
        if got is UNAVAILABLE:
            continue
        exact = (d == d0)
        if code is None:
            got.is_exact = exact
            return got
        c = _norm_code(code)
        if isinstance(got, dict):
            for k, v in got.items():
                if _same_code(k, c):
                    return _wrap(v, d, exact, t)
            return None
        rows = [r for r in got if isinstance(r, dict) and _same_code(r.get("code"), c)]
        return Series(rows, date=d, is_exact=exact, kind=t)
    return UNAVAILABLE


def list_ext_types(as_of, base=None):
    """列出 ≤as_of 已投放的扩展数据类型 → Record{type: 最近投放日}（诊断/缺口盘点用）。"""
    seen = {}
    r = roots(base)["raw"]
    for d in _raw_dates(base, upto=_chk_date(as_of)):
        for n in os.listdir(os.path.join(r, d)):
            stem, ext = os.path.splitext(n)
            if ext in (".json", ".jsonl") and os.path.isfile(os.path.join(r, d, n)):
                if stem not in _CORE_KEYS:
                    seen[stem] = max(seen.get(stem, ""), d)
    return Record(seen, kind="ext_index")


# 0.10 具名契约：薄封装（签名即冻结契约，一律 as_of 第 1 位）
def load_sector(as_of, code=None, base=None):
    """板块行情与梯队。源未接 → UNAVAILABLE。"""
    return load_ext("sector", as_of, code=code, base=base)


def load_capital_flow(as_of, code=None, base=None):
    """资金流与龙虎榜席位。源未接 → UNAVAILABLE。"""
    return load_ext("capital_flow", as_of, code=code, base=base)


def load_hot_search(as_of, code=None, base=None):
    """热搜 / 人气。源未接 → UNAVAILABLE。"""
    return load_ext("hot_search", as_of, code=code, base=base)


def load_financial(as_of, code=None, base=None):
    """财务与报表。源未接 → UNAVAILABLE。"""
    return load_ext("financial", as_of, code=code, base=base)


def load_consensus(as_of, code=None, base=None):
    """一致预期。源未接 → UNAVAILABLE。"""
    return load_ext("consensus", as_of, code=code, base=base)


def load_governance(as_of, code=None, base=None):
    """公司治理。源未接 → UNAVAILABLE。"""
    return load_ext("governance", as_of, code=code, base=base)


def load_chip(as_of, code=None, base=None):
    """筹码结构。源未接 → UNAVAILABLE。"""
    return load_ext("chip", as_of, code=code, base=base)


def load_auction(as_of, code=None, base=None):
    """竞价历史。源未接 → UNAVAILABLE（当日竞价见 load_premarket）。"""
    return load_ext("auction", as_of, code=code, base=base)


def load_intraday_history(as_of, code=None, base=None):
    """分时历史。源未接 → UNAVAILABLE。"""
    return load_ext("intraday_history", as_of, code=code, base=base)


def load_policy(as_of, code=None, base=None):
    """政策与产业传导素材。源未接 → UNAVAILABLE。"""
    return load_ext("policy", as_of, code=code, base=base)


# ── 上游产物（2.10 统一收口：同为 point-in-time 取数接口） ─────────────────────
def load_s_output(s_id, as_of, base=None):
    """S 输出快照 → `data/generated/s_<id>_<as_of>.json`（2.10 唯一键 = as_of）。

    精确命中：同 as_of 幂等覆盖、跨 as_of 各自独立；缺 → UNAVAILABLE。
    """
    sid = _sid(s_id)
    d = _chk_date(as_of)
    p = os.path.join(roots(base)["generated"], "s_%s_%s.json" % (sid, d))
    if not os.path.isfile(p):
        return UNAVAILABLE
    with io.open(p, encoding="utf-8-sig") as f:
        obj = json.load(f)
    rec = _wrap(obj, d, True, "s_output")
    if isinstance(rec, (Series, Record)):
        rec.note = "s_%s" % sid
    return rec


def load_knowledge(s_id, as_of, base=None):
    """S 知识库 → 经 `data/knowledge/_registry.json` 路由（2.10）。

    时序守卫：库文件自报 `as_of` 晚于请求 → UNAVAILABLE（禁历史重演读到来后才改的知识）。
    注册表 / 条目 / 文件缺席 → UNAVAILABLE。
    """
    sid = _sid(s_id)
    d = _chk_date(as_of)
    b = base or ROOT
    reg_p = os.path.join(roots(base)["knowledge"], "_registry.json")
    if not os.path.isfile(reg_p):
        return UNAVAILABLE
    with io.open(reg_p, encoding="utf-8-sig") as f:
        reg = json.load(f) or {}
    entry = reg.get(sid)
    if not entry:
        return UNAVAILABLE
    rel = entry.get("path") if isinstance(entry, dict) else entry
    if not rel:
        return UNAVAILABLE
    p = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(b, rel))
    if not os.path.isfile(p):
        return UNAVAILABLE
    ver = entry.get("version") if isinstance(entry, dict) else None
    if p.endswith(".json"):
        with io.open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
        k_asof = data.get("as_of") if isinstance(data, dict) else None
        if k_asof and str(k_asof) > d:
            return UNAVAILABLE                     # 未来版本，禁读
        rec = _wrap(data, d, True, "knowledge")
    else:
        with io.open(p, encoding="utf-8-sig") as f:
            text = f.read()
        m = re.search(r"as_of\s*[:：]\s*(\d{4}-\d{2}-\d{2})", text[:4000])
        k_asof = m.group(1) if m else None
        if k_asof and k_asof > d:
            return UNAVAILABLE
        rec = Record({"s_id": sid, "path": rel, "as_of": k_asof or d, "version": ver,
                      "text": text}, date=d, is_exact=True, kind="knowledge")
    rec.note = "s_%s @%s" % (sid, rel)
    return rec


def _sid(s_id):
    s = str(s_id or "").strip().lower()
    if not _TYPE_RE.match(s):
        raise SnapshotError("S 标识非法: %r" % (s_id,))
    return s


# ── 诊断（区分"从未有源"与"那几天没数据"） ───────────────────────────────────
def diagnose(name, as_of=None, base=None):
    """数据源健康诊断 → Record{state,detail,as_of}。

    state: OK / NO_SOURCE（从未投放该类型）/ DATE_GAP（有投放、但 ≤as_of 没有）/ STALE（盘中超龄）
    —— 数据缺口处置必须先知道是哪种；本函数是唯一诊断入口，不改数据路径的单值性。
    说明：判 NO_SOURCE / DATE_GAP 时只看投放区**文件名**（不读未来文件内容），不构成未来数据泄漏。
    """
    out = {"state": "NO_SOURCE", "detail": "", "as_of": as_of}
    if name in ("live", "live_market"):
        fname = "market.json" if name == "live_market" else "quotes.json"
        p = os.path.join(roots(base)["raw"], LIVE_DIRNAME, fname)
        if not os.path.isfile(p):
            out["detail"] = "盘中投放不存在：%s" % p
        else:
            age = _dt.datetime.now().timestamp() - os.path.getmtime(p)
            if age > LIVE_TTL_SECONDS:
                out["state"] = "STALE"
                out["detail"] = "投放已 %.0f 秒未更新（上限 %d）" % (age, LIVE_TTL_SECONDS)
            else:
                out["state"], out["detail"] = "OK", "投放 %.0f 秒前更新" % age
        return Record(out, kind="diagnose")

    all_dates = _raw_dates(base)
    if name in _CORE_KEYS:
        fname = [f for f, spec in S.CONTRACT.items() if spec["key"] == name][0]
        hit = [d for d in all_dates if os.path.isfile(os.path.join(roots(base)["raw"], d, fname))]
        snap = S.load_asof(as_of, root=roots(base)["snapshot"]) if as_of else UNAVAILABLE
        if snap is not UNAVAILABLE:
            out["state"], out["detail"] = "OK", "命中快照日 %s" % snap["date"]
        elif hit:
            out["state"] = "DATE_GAP"
            out["detail"] = "投放区有 %s（%s），但快照未生成 → 查 0.2 ingest 是否执行" % (fname, max(hit))
        else:
            out["detail"] = "核心契约文件 %s 从未投放" % fname
        return Record(out, kind="diagnose")

    if not all_dates:
        out["detail"] = "投放区无任何日期目录"
        return Record(out, kind="diagnose")
    with_type = [d for d in all_dates if _ext_file(name, d, base)]
    if not with_type:
        out["state"], out["detail"] = "NO_SOURCE", "从未投放该类型（已投放日：%s）" % ", ".join(all_dates)
    else:
        ok = [d for d in with_type if as_of is None or d <= as_of]
        if ok:
            out["state"], out["detail"] = "OK", "最近投放日 %s" % max(ok)
        else:
            out["state"] = "DATE_GAP"
            out["detail"] = "只有 %s 的投放，均晚于 as_of=%s（截断生效）" % (max(with_type), as_of)
    return Record(out, kind="diagnose")


# ── 纪律自检：全库有无直连数据源 / LLM 补数路径（0.10 验收硬项） ────────────────
# ★ 用 AST 而非文本匹配：注释 / 字符串里出现这些字样不算违规（真违规必须是真导入、真调用）
_FORBIDDEN_IMPORTS = ("requests", "urllib", "urllib2", "urllib3", "http", "httpx", "aiohttp",
                      "socket", "subprocess", "telnetlib", "ftplib", "smtplib", "openai",
                      "anthropic", "zhipuai", "dashscope", "websocket", "websockets")
_FORBIDDEN_ATTRS = ("system", "popen", "spawn", "spawnl", "spawnv", "execv", "execvp", "execve")
_SCAN_DIRS = ("tools", os.path.join(".workbuddy", "skills"))


def check_discipline(project_root=None):
    """扫全库 python 源码：有无「直连数据源 / 调 shell 取数 / LLM 补数」→ 违规清单（[] = 合规）。

    判据（AST 级，禁文本误判）：`import` 禁用模块 · 调用 `os.system/os.popen/os.spawn*/os.exec*`。
    本模块在扫描范围内（自查不过即有鬼）。
    """
    root = project_root or ROOT
    targets = []
    for sub in _SCAN_DIRS:
        base_dir = os.path.join(root, sub)
        if not os.path.isdir(base_dir):
            continue
        for dirpath, dirnames, filenames in os.walk(base_dir):
            dirnames[:] = [x for x in dirnames if x not in ("__pycache__", ".git")]
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    targets.append(os.path.join(dirpath, fn))
    bad = []
    for p in sorted(set(targets)):
        rel = os.path.relpath(p, root).replace("\\", "/")
        with io.open(p, encoding="utf-8-sig", errors="replace") as f:
            src = f.read()
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            bad.append((rel, getattr(e, "lineno", 0) or 0, "无法解析：%s" % e))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for al in node.names:
                    if al.name.split(".")[0] in _FORBIDDEN_IMPORTS:
                        bad.append((rel, node.lineno, "禁用导入：%s" % al.name))
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in _FORBIDDEN_IMPORTS:
                    bad.append((rel, node.lineno, "禁用导入：%s" % node.module))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in _FORBIDDEN_ATTRS:
                    bad.append((rel, node.lineno, "禁用调用：.%s()" % node.func.attr))
    return bad


# ── 只读现状视图 ─────────────────────────────────────────────────────────────
def _status():
    r = roots()
    print("数据统一入口 · loader.py v%s" % SCHEMA_VERSION)
    print("  ROOT        : %s" % ROOT)
    snap_dates = S.available_dates()
    print("  快照日      : %s" % (", ".join(snap_dates) if snap_dates
                                else "(无 —— 尚未接采集器，属预期状态)"))
    rd = _raw_dates()
    print("  投放日      : %s" % (", ".join(rd) if rd else "(无)"))
    live_p = os.path.join(r["raw"], LIVE_DIRNAME)
    print("  盘中投放区  : %s%s" % (live_p, "" if os.path.isdir(live_p) else "  (不存在)"))
    ext = list_ext_types("9999-12-31")
    print("  扩展类型    : %s" % (", ".join("%s@%s" % (k, v) for k, v in sorted(ext.items())) or "(无)"))
    pub = sorted(n for n in dir(sys.modules[__name__]) if n.startswith("load_"))
    print("  公开接口    : %d 个 load_*  + resolve()/diagnose()" % len(pub))
    print("                %s" % ", ".join(pub))
    bad = check_discipline()
    print("  纪律自检    : %s" % ("✅ 无直连数据源 / 无 LLM 补数" if not bad
                                else "⚠️ %d 条违规：%s" % (len(bad), bad[:3])))


# ── 自检（--selftest）：全部在临时 data 树跑，不碰真数据 ───────────────────────
def _selftest():
    import shutil
    import tempfile

    n = [0, 0]

    def T(name, cond):
        n[0] += 1
        if cond:
            print("  ✅ %s" % name)
        else:
            n[1] += 1
            print("  ❌ %s" % name)

    real_before = _tree_fingerprint(os.path.join(ROOT, "data"))
    tmp = tempfile.mkdtemp(prefix="loader_st_")
    B = os.path.join(tmp, "proj")                     # 沙盒项目根（base）
    D_ = roots(B)
    os.makedirs(B, exist_ok=True)
    for k in ("raw", "generated", "knowledge"):
        os.makedirs(D_[k], exist_ok=True)

    D1, D2, D3 = "2026-10-08", "2026-10-09", "2026-10-12"    # D3 留空 = 未采集
    PAY = {"daily_quote": [], "limit_up": [], "dragon_tiger": [], "news": [],
           "env_metrics": {"limit_up_count": 41, "max_board": 4, "turnover": 8123.5,
                           "up_down_ratio": 1.8, "fail_rate": 0.21, "index_ma20": 3312.4}}

    def pay(day, code="sh600000", boards=1, news=False):
        p = json.loads(json.dumps(PAY))
        p["daily_quote"] = [{"code": code, "name": "浦发银行", "open": 10.0, "high": 10.9,
                             "low": 9.9, "close": 10.8, "pct": 5.88, "vol": 1.2e8, "amount": 1.29e9}]
        p["limit_up"] = [{"code": code, "name": "浦发银行", "boards": boards,
                          "first_seal_time": "09:42", "seal_amount": 3.1e8, "reason": "银行"}]
        p["news"] = ([{"news_id": "N1", "ts": day + " 09:10", "source": "cls",
                       "title": "测试消息", "body": "...", "related_codes": [code]}] if news else [])
        return p

    def wjson(path, obj):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False)

    def wline(path, obj):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    try:
        # ── A 哨兵 / 契约 / 目录派生（9） ───────────────────────────────
        T("A1 UNAVAILABLE 与 0.2 是同一哨兵对象（全系统一种缺口判据）", UNAVAILABLE is S.UNAVAILABLE)
        T("A2 UNAVAILABLE 布尔为假", not UNAVAILABLE)
        T("A3 无快照时 load_quote → UNAVAILABLE（源缺席）", load_quote(D1, "sh600000", base=B) is UNAVAILABLE)
        T("A4 无快照时 load_snapshot → UNAVAILABLE", load_snapshot(D1, base=B) is UNAVAILABLE)
        T("A5 非法代码抛错（不静默查不到）", _raises(lambda: load_quote(D1, "60000", base=B)))
        T("A6 非法数据类型名抛错", _raises(lambda: load_ext("Bad-Name", D1, base=B)))
        T("A7 与核心契约撞名的类型被拒", _raises(lambda: load_ext("daily_quote", D1, base=B)))
        T("A8 非法 as_of 抛错（禁按字符串静默比较）", _raises(lambda: load_snapshot("2026/10/08", base=B)))
        T("A9 roots() 由 base 派生四根（单参数不承多义）",
          roots(B)["snapshot"].startswith(B) and roots(B)["raw"].startswith(B))

        # ── B 盘后腿（6） ────────────────────────────────────────────────
        S.write_snapshot(D1, pay(D1), root=D_["snapshot"])
        q = load_quote(D1, "600000", base=B)          # 无前缀应命中 sh600000
        T("B1 load_quote 忽略前缀差异命中", isinstance(q, Record) and q["close"] == 10.8)
        T("B2 返回自带出处 .date / .is_exact", q.date == D1 and q.is_exact is True)
        T("B3 快照在但无该票 → None（≠ 源缺席）", load_quote(D1, "sz300001", base=B) is None)
        lu = load_limit_up(D1, base=B)
        T("B4 列表类返回 Series 且带 date", isinstance(lu, list) and lu.date == D1 and len(lu) == 1)
        env = load_env_metrics(D1, base=B)
        T("B5 env 键名严格等于 0.2 契约",
          set(env.keys()) == set(p for p, _ in S.CONTRACT["env_metrics.json"]["cols"]))
        ns = load_news(D1, base=B)
        T("B6 真实空列表是 Series 不是 UNAVAILABLE（空 ≠ 缺口）",
          isinstance(ns, list) and len(ns) == 0 and ns is not UNAVAILABLE)

        # ── C 锁① 截断（6） ─────────────────────────────────────────────
        S.write_snapshot(D2, pay(D2, code="sz300001", boards=3), root=D_["snapshot"])
        T("C1 写完 D2 后，as_of=D1 仍只拿 D1", load_limit_up(D1, base=B).date == D1)
        T("C2 拿不到 D2 才有的票", load_quote(D1, "sz300001", base=B) is None)
        T("C3 as_of=D2 精确命中且 is_exact=True", load_quote(D2, "sz300001", base=B).is_exact is True)
        T("C4 落在 D3（未采集）→ 回退 D2 且 is_exact=False",
          load_snapshot(D3, base=B).date == D2 and load_snapshot(D3, base=B).is_exact is False)
        T("C5 resolve() 报真实命中日", resolve(D3, base=B)["date"] == D2)
        T("C6 早于全部快照 → UNAVAILABLE", load_snapshot("2020-01-01", base=B) is UNAVAILABLE)

        # ── D 盘前腿（8） ───────────────────────────────────────────────
        wline(os.path.join(D_["raw"], D2, "news.jsonl"),
              {"news_id": "P1", "ts": D2 + " 08:05", "source": "cls",
               "title": "盘前消息", "body": "x", "related_codes": []})
        pm = load_premarket(D2, base=B)
        T("D1 盘前腿收盘腿 = ≤T-1（D1）", isinstance(pm, Record) and pm["close_date"] == D1)
        T("D2 盘前腿日期出处 = 收盘腿日", pm.date == D1 and pm["is_exact_close"] is True)
        T("D3 盘前腿拿到当日盘前消息（投放区口径，非快照）",
          isinstance(pm["news"], list) and len(pm["news"]) == 1 and pm["news"][0]["news_id"] == "P1")
        T("D4 当日竞价未投放 → 该键单独 UNAVAILABLE（不拖累其余）", pm["auction"] is UNAVAILABLE)
        T("D5 环境数据来自收盘腿且可用", pm["env_metrics"]["max_board"] == 4)
        T("D6 无 ≤T-1 快照 → 整个盘前腿 UNAVAILABLE", load_premarket(D1, base=B) is UNAVAILABLE)
        T("D7 ★硬闸：D2 快照存在，盘前腿仍只认 ≤T-1（绝不取当日收盘快照）",
          load_premarket(D2, base=B)["close_date"] < D2)
        T("D8 盘前腿未消费当日快照内容（拿到 D1 的票，非 D2 的 sz300001）",
          load_premarket(D2, base=B)["snapshot"]["daily_quote"][0]["code"] == "sh600000")

        # ── E 盘中腿（7） ───────────────────────────────────────────────
        T("E1 无盘中投放 → UNAVAILABLE", load_live("sh600000", base=B) is UNAVAILABLE)
        T("E2 无盘中投放 → load_live_market UNAVAILABLE", load_live_market(base=B) is UNAVAILABLE)
        live = os.path.join(D_["raw"], LIVE_DIRNAME)
        wjson(os.path.join(live, "quotes.json"),
              {"sh600000": {"code": "sh600000", "last": 11.02, "pct": 2.03, "vol_ratio": 1.4}})
        wjson(os.path.join(live, "market.json"), {"up": 3120, "down": 1850, "limit_up": 44})
        lv = load_live("600000", base=B)
        T("E3 新鲜投放 → 命中且带 ts", isinstance(lv, Record) and lv["last"] == 11.02 and "ts=" in (lv.note or ""))
        T("E4 实时源在但无该票 → None", load_live("sz000001", base=B) is None)
        T("E5 超龄投放 → UNAVAILABLE（旧读数不当实时）",
          load_live("sh600000", ttl=-1, base=B) is UNAVAILABLE
          and load_live_market(ttl=-1, base=B) is UNAVAILABLE)
        T("E6 load_live_market 命中", load_live_market(base=B)["limit_up"] == 44)
        before = _tree_fingerprint(D_["snapshot"])
        for _ in range(3):
            load_live("sh600000", base=B)
            load_live_market(base=B)
        T("E7 ★盘中腿零快照写入（调用前后快照目录指纹不变）",
          _tree_fingerprint(D_["snapshot"]) == before)

        # ── F 扩展类型：插入式（7） ─────────────────────────────────────
        T("F1 未接的具名数据类型 → UNAVAILABLE", load_sector(D2, base=B) is UNAVAILABLE)
        T("F2 10 个具名类型全返回 UNAVAILABLE（源未接不造假）",
          all(fn(D2, base=B) is UNAVAILABLE for fn in
              (load_sector, load_capital_flow, load_hot_search, load_financial, load_consensus,
               load_governance, load_chip, load_auction, load_intraday_history, load_policy)))
        wjson(os.path.join(D_["raw"], D2, "sector.json"),
              [{"name": "银行", "pct": 2.1}, {"name": "地产", "pct": -0.8}])
        sec = load_sector(D2, base=B)
        T("F3 放文件即生效：load_sector 拿到投放数据", isinstance(sec, list) and len(sec) == 2)
        T("F4 扩展类型同样 ≤as_of 截断（as_of=D1 拿不到 D2 的投放）", load_sector(D1, base=B) is UNAVAILABLE)
        wline(os.path.join(D_["raw"], D2, "brand_new_type.jsonl"), {"code": "sh600000", "score": 9})
        bn = load_ext("brand_new_type", D2, base=B)
        T("F5 全新类型零代码改动可读（load_ext）", isinstance(bn, list) and bn[0]["score"] == 9)
        T("F6 带 code 的扩展查询按 code 取到",
          isinstance(load_ext("brand_new_type", D2, code="600000", base=B), list))
        T("F7 list_ext_types 排除核心契约键（news 属核心 5 文件，不得混入扩展清单）",
          set(list_ext_types(D2, base=B).keys()) == {"sector", "brand_new_type"})

        # ── G 上游产物（7） ─────────────────────────────────────────────
        T("G1 无 S 输出 → UNAVAILABLE", load_s_output("s3", D2, base=B) is UNAVAILABLE)
        wjson(os.path.join(D_["generated"], "s_s3_%s.json" % D2),
              {"s_id": "S3", "as_of": D2, "evidence": [], "confidence": 0.62})
        so = load_s_output("s3", D2, base=B)
        T("G2 S 输出按 as_of 精确命中", isinstance(so, Record) and so["confidence"] == 0.62)
        T("G3 S 输出跨 as_of 独立（D1 无该文件 → UNAVAILABLE）",
          load_s_output("s3", D1, base=B) is UNAVAILABLE)
        T("G4 无知识库注册表 → UNAVAILABLE", load_knowledge("s3", D2, base=B) is UNAVAILABLE)
        wjson(os.path.join(D_["knowledge"], "_registry.json"),
              {"s3": {"path": "data/knowledge/s3_emotion_structure/kb.json", "version": 1}})
        T("G5 注册表有但文件缺 → UNAVAILABLE（不假装有）", load_knowledge("s3", D2, base=B) is UNAVAILABLE)
        kb = os.path.join(D_["knowledge"], "s3_emotion_structure", "kb.json")
        wjson(kb, {"as_of": "2026-12-31", "rows": []})                  # 未来版本
        T("G6 ★知识库未来版本被挡（禁历史重演读到来后才改的知识）",
          load_knowledge("s3", D2, base=B) is UNAVAILABLE)
        wjson(kb, {"as_of": D1, "rows": [1, 2]})                        # 历史版本
        T("G7 知识库历史版本可读且带出处", isinstance(load_knowledge("s3", D2, base=B), Record))

        # ── H 纪律自检（5） ─────────────────────────────────────────────
        T("H1 本库源码零违规（loader 自查也过）", check_discipline(B) == [])
        os.makedirs(os.path.join(B, "tools"), exist_ok=True)
        with io.open(os.path.join(B, "tools", "bad_tool.py"), "w", encoding="utf-8", newline="\n") as f:
            f.write("import requests\n\n\ndef get():\n    return requests.get('http://x')\n")
        v1 = check_discipline(B)
        T("H2 直连数据源被查出（import requests）", any("requests" in x[2] for x in v1))
        with io.open(os.path.join(B, "tools", "bad2.py"), "w", encoding="utf-8", newline="\n") as f:
            f.write("import os\n\n\ndef run():\n    os.system('westock quote sh600000')\n")
        v2 = check_discipline(B)
        T("H3 调 shell 取数被查出（os.system）", any(".system" in x[2] for x in v2))
        T("H4 违规项带 文件+行号（可定位）", all(len(x) == 3 and isinstance(x[1], int) for x in v2))
        with io.open(os.path.join(B, "tools", "comment_only.py"), "w", encoding="utf-8", newline="\n") as f:
            f.write("# 本模块禁 import requests；也不得调 os.system\n"
                    "MSG = 'requests 与 os.system 均不可用'\n")
        T("H5 注释/字符串里的字样不误判（AST 判据）",
          all(x[0] != "tools/comment_only.py" for x in check_discipline(B)))

        # ── I 诊断（5） ─────────────────────────────────────────────────
        T("I1 diagnose=NO_SOURCE：从未投放的类型",
          diagnose("hot_search", D2, base=B)["state"] == "NO_SOURCE")
        T("I2 diagnose=OK：已投放的类型", diagnose("sector", D2, base=B)["state"] == "OK")
        T("I3 diagnose=DATE_GAP：只有晚于 as_of 的投放",
          diagnose("sector", D1, base=B)["state"] == "DATE_GAP")
        T("I4 diagnose 核心键走快照口径（报命中快照日）",
          diagnose("env_metrics", D2, base=B)["detail"].startswith("命中快照日"))
        T("I5 diagnose 盘中有投放且新鲜 → OK", diagnose("live", base=B)["state"] == "OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(os.path.join(_HERE, "__pycache__"), ignore_errors=True)

    T("J1 ★真数据零污染（自检后 ROOT/data 指纹不变）",
      _tree_fingerprint(os.path.join(ROOT, "data")) == real_before)
    T("J2 临时沙盒已清除", not os.path.exists(tmp))

    print("\n结果：%d 通过 / %d 失败" % (n[0] - n[1], n[1]))
    return 1 if n[1] else 0


def _raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


def _tree_fingerprint(path):
    """目录指纹（相对路径 + 大小 + mtime），用于"零写入 / 零污染"证明。"""
    if not os.path.isdir(path):
        return ()
    out = []
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for fn in sorted(filenames):
            p = os.path.join(dirpath, fn)
            st = os.stat(p)
            out.append((os.path.relpath(p, path), st.st_size, int(st.st_mtime)))
    return tuple(out)


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    if "--discipline" in argv:
        bad = check_discipline()
        npy = sum(1 for d, _, fs in os.walk(os.path.join(ROOT, "tools"))
                  for f in fs if f.endswith(".py"))
        if not bad:
            print("✅ 全库无直连数据源 / 无 LLM 补数路径（扫 %d 个 python 文件）" % npy)
            return 0
        print("⚠️ 发现 %d 条违规：" % len(bad))
        for rel, ln, what in bad:
            print("  %s:%d  %s" % (rel, ln, what))
        return 1
    _status()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
