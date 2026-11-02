#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dayclass.py · B3 日期完整性 · 交易日与数据可用状态判定（最小可行实现 · 2026-10-13 建）

★ 本模块只做一件事：把「某一天到底能不能用当日事实」变成一个**显式、可机械复算、
  永不静默回退**的判定结果。它是 B3 设计件 v4 §3 三轴分离中「轴 A（交易日状态）+
  轴 B（数据完整性）」的**纯函数实现**，零写盘、零状态、零外部依赖。

★ 四态（唯一值域，禁止第三种说法）：
  · `TRADING_WITH_DATA`  确认交易日 ∧ 目标日数据齐备（`exact=True`，唯一可放行态）
  · `TRADING_NO_DATA`    确认交易日 ∧ 目标日数据缺失（**不得**用旧日期数据顶替）
  · `NON_TRADING`        **有可靠日历证据**确认非交易日
  · `UNKNOWN`            证据不足或冲突（无日历 / 超出日历覆盖 / 参数非法）
  ⇒ **`UNKNOWN` 与 `TRADING_NO_DATA` 必须可区分**：前者连"是不是交易日"都不知道，
    后者知道是交易日但没数据。混为一谈 = 要么把缺口当休市（漏记缺口），
    要么把休市当缺口（凭空造缺口）。本模块用两个常量与不同 `reasons` 强制区分。

★ fail-closed 三条铁律（本模块的**存在理由**）：
  ① **无日历 ⇒ `UNKNOWN`**，绝不按周末推算、绝不由目录缺失反推休市；
  ② `anchor`（允许使用的数据日）**只在 `TRADING_WITH_DATA` 时非 None**；
     其余三态一律 `None` —— 调用方拿不到"可以顶替的日期"，静默回退在接口层就断了；
  ③ `data_date` 字段**仅作信息展示**（"最近一日数据落在哪天"），**禁止**当作可用数据源；
     本模块不给任何"回退取数"的返回值。

★ 时间轴正确性（A4 候选到期日同源）：`add_sessions` / `sessions_between` / `prev_session`
  一律**以日历交易日序列**为轴，不用"工作日近似"、不用"有数据日序列"。
  「有数据日序列」会**静默压缩时间轴**（缺口日凭空消失 → DayN 少算、expiry 提前）；
  「日历」才是唯一正确轴。二者不可互换：M 类问题（第 N 个交易日）走日历，
  D 类问题（该日事实是否可用）走 `classify`。→ B3 设计件 v4 §5.2.7。

★ 日历来源（**只读消费，本模块不落盘、不刷新、不接入**）：
  `Calendar.from_seed_csv(path)` 读 `date,is_trade` 两列 CSV（旧包
  `data/raw/meta/trade_cal.csv` 同构）。落地与刷新策略属 B3 §5.2 日历接入契约，
  须单独授权；本模块只提供**纯内存构造 + 只读种子解析**两种入口。

★ 纪律（`--discipline` 机械自证）：
  ① 零写点（生产区无写模式 open / 无 makedirs / 无 remove / 无 replace / 无 rename）
  ② 零"目录缺失 ⇒ 休市"推断（生产区无把文件/目录缺失直接判 NON_TRADING 的分支）
  ③ 四态常量封闭（值域恰为 4 个，禁第五态）
  ④ UNKNOWN 是默认失败态（无日历 / 超范围 ⇒ UNKNOWN，而非 NON_TRADING）
  ⑤ astwin 引用（生产函数表经 astwin 枚举，判据与自检共享同一窗口实现）

用法：
  python tools/fs/dayclass.py --selftest
  python tools/fs/dayclass.py --discipline
  python tools/fs/dayclass.py --calendar <trade_cal.csv> --as-of 2026-09-25
"""

import ast
import csv
import io
import os
import sys
from datetime import date, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import astwin as ASTW  # noqa: E402

_LEGACY_ROOT = (os.environ.get("OLD_SYSTEM_ROOT")
                or os.path.join(os.path.expanduser("~"), "炒股智能体_旧包_20261008"))  # 旧系统只读参考源；换机器时设 OLD_SYSTEM_ROOT 或放家目录

__all__ = ("DayError", "Calendar", "classify", "require_exact", "gaps",
           "next_session_of", "add_sessions", "sessions_between", "is_contiguous",
           "STATUSES", "STATUS_TRADING_WITH_DATA", "STATUS_TRADING_NO_DATA",
           "STATUS_NON_TRADING", "STATUS_UNKNOWN")

# ── 四态值域（封闭；禁止第五态、禁止同义改写） ────────────────────────────────
STATUS_TRADING_WITH_DATA = "TRADING_WITH_DATA"
STATUS_TRADING_NO_DATA = "TRADING_NO_DATA"
STATUS_NON_TRADING = "NON_TRADING"
STATUS_UNKNOWN = "UNKNOWN"
STATUSES = (STATUS_TRADING_WITH_DATA, STATUS_TRADING_NO_DATA,
            STATUS_NON_TRADING, STATUS_UNKNOWN)

# reasons 值域（机读；人读文案不进 this 常量，防判据漂移）
R_NO_CALENDAR = "NO_CALENDAR"
R_OUT_OF_RANGE = "OUT_OF_CALENDAR_RANGE"
R_CALENDAR_NON_SESSION = "CALENDAR_SAYS_NON_SESSION"
R_NO_SNAPSHOT = "NO_SNAPSHOT_FOR_SESSION"
R_INCOMPLETE = "DATA_INCOMPLETE"
R_BAD_DATE = "BAD_DATE"

_DATE_LEN = 10          # 'YYYY-MM-DD'


class DayError(Exception):
    """日期语义错误（入参非法 / 需要 exact 却拿到缺口）。**响失败，禁静默降级**。"""


# ── 日历（纯内存；只读消费，本模块不落盘、不刷新） ────────────────────────────
class Calendar(object):
    """交易日历（**纯数据对象**）。`sessions` = 已知交易日升序序列。

    `covers(d)` 的语义：d 落在日历**已知区间内**（区间 = [首日, 末日] 闭区间）。
    区间外一律视为「不知道」⇒ `classify` 返回 `UNKNOWN`（**不是** NON_TRADING）。
    """

    __slots__ = ("sessions", "_set", "source", "version", "as_of")

    def __init__(self, sessions, source=None, version=None, as_of=None):
        got = []
        for s in sessions:
            got.append(_norm_date(s))
        self.sessions = tuple(sorted(set(got)))
        self._set = frozenset(self.sessions)
        self.source = source
        self.version = version
        self.as_of = as_of

    # —— 基本查询 ——
    def __len__(self):
        return len(self.sessions)

    @property
    def span(self):
        if not self.sessions:
            return (None, None)
        return (self.sessions[0], self.sessions[-1])

    def covers(self, d):
        """d 是否在日历已知区间内（含端点）。空日历 ⇒ 什么都不覆盖。"""
        d = _norm_date(d)
        if not self.sessions:
            return False
        lo, hi = self.span
        return lo <= d <= hi

    def is_session(self, d):
        return _norm_date(d) in self._set

    def basis(self):
        """日历出处（进 verdict，供审计）。无 source 也要如实说"无"）。"""
        if not self.sessions:
            return "EMPTY"
        return "SEED:%s@%s" % (self.source or "UNSPECIFIED",
                               self.version or "UNSPECIFIED")

    # —— 时间轴（**唯一正确轴**：日历交易日序列） ——
    def prev_session(self, d):
        """严格早于 d 的最后一个交易日；无 ⇒ None（不猜、不按周末推）。"""
        d = _norm_date(d)
        lo = None
        for s in self.sessions:
            if s < d:
                lo = s
            else:
                break
        return lo

    def next_session(self, d):
        """严格晚于 d 的第一个交易日；无 ⇒ None。"""
        d = _norm_date(d)
        for s in self.sessions:
            if s > d:
                return s
        return None

    def sessions_between(self, a, b):
        """闭区间 [a, b] 内的交易日（升序）。两端非交易日也照常返回区间内交易日。"""
        a, b = _norm_date(a), _norm_date(b)
        if b < a:
            return ()
        return tuple(s for s in self.sessions if a <= s <= b)

    def add_sessions(self, d, n):
        """**从 d 次日起**数第 n 个交易日（n ≥ 1）。

        与冻结件 `candidates._add_trading_days` **同口径**（次日起、数第 n 个），
        唯一差异 = 本实现走**日历交易日**，而 `_add_trading_days` 只跳周末
        —— 后者在跨休市窗口会**提前一日**（09-22 + 5 → 09-29，正确应为 09-30）。
        区间外 ⇒ None（不按周末近似、不外推）。
        """
        d = _norm_date(d)
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise DayError("add_sessions 的 n 须 >= 1（次日起数第 n 个），收到 %r" % (n,))
        i = None
        for k, s in enumerate(self.sessions):
            if s > d:
                i = k
                break
        if i is None:
            return None
        j = i + n - 1
        if j >= len(self.sessions):
            return None
        return self.sessions[j]

    # —— 构造入口（只读） ——
    @classmethod
    def from_seed_rows(cls, rows, source=None, version=None, as_of=None):
        """[{'date': 'YYYY-MM-DD', 'is_trade': 1|'1'|True}, ...] → 日历。"""
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            if _truthy(r.get("is_trade")):
                out.append(r.get("date"))
        return cls(out, source=source, version=version, as_of=as_of)

    @classmethod
    def from_seed_csv(cls, path, source=None, version=None, as_of=None):
        """只读解析 `date,is_trade` 两列 CSV（旧包 `meta/trade_cal.csv` 同构）。

        **只读**：本模块不对其做任何写入/修复/补全。
        """
        with io.open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            raise DayError("日历种子为空：%s" % path)
        cols = set(rows[0].keys())
        if "date" not in cols or "is_trade" not in cols:
            raise DayError("日历种子缺列（须 date,is_trade）：%s" % sorted(cols))
        return cls.from_seed_rows(rows, source=source or os.path.basename(path),
                                  version=version, as_of=as_of)


def _truthy(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip() in ("1", "true", "True", "TRUE", "yes", "Y")
    return False


def _norm_date(d):
    """'YYYY-MM-DD' 归一 + 校验（严进：非法即抛，禁静默接受）。"""
    if isinstance(d, date):
        return d.isoformat()
    if not isinstance(d, str):
        raise DayError("日期必须是 'YYYY-MM-DD' 字符串或 date，收到 %r" % (type(d).__name__,))
    s = d.strip()
    if len(s) != _DATE_LEN:
        raise DayError("日期长度非法（须 YYYY-MM-DD）：%r" % (d,))
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise DayError("日期非法（无法解析）：%r" % (d,))


# ── 核心判定：某一天能不能用「当日事实」 ──────────────────────────────────────
def classify(as_of, *, calendar=None, data_dates=(), required_keys=None,
             present_keys=None):
    """→ verdict dict（唯一判定入口；**永不返回"可回退日期"**）。

    参数：
      calendar       `Calendar` | None。None ⇒ `UNKNOWN`（fail-closed，不按周末推）。
      data_dates     该数据源**已有数据的日期集合**（如快照目录名）。
                     **只用于"目标日在不在"这一判断**，不用于任何时间轴推算。
      required_keys  可选：该日快照**必需键集**（如 0.2 契约 5 键）。
      present_keys   可选：该日实际存在的键集。与 required_keys 齐备性比对，
                     缺键 ⇒ 降级为 `TRADING_NO_DATA`（**缺就是缺，不静默当齐**）。

    返回（键集封闭）：
      {date, status, exact, anchor, data_date, reasons, missing_keys, calendar_basis}
      · `exact`  = (status == TRADING_WITH_DATA)
      · `anchor` = as_of 仅当 exact，否则 **None**（禁止回退取数）
      · `data_date` = 最近一个 < as_of 的数据日（**仅信息展示**，禁止当数据源）
    """
    d = _norm_date(as_of)
    have = frozenset(_norm_date(x) for x in data_dates)

    reasons = []
    missing = []

    if calendar is None or not isinstance(calendar, Calendar) or not len(calendar):
        reasons.append(R_NO_CALENDAR)
        status = STATUS_UNKNOWN
    elif not calendar.covers(d):
        reasons.append(R_OUT_OF_RANGE)
        status = STATUS_UNKNOWN
    elif not calendar.is_session(d):
        reasons.append(R_CALENDAR_NON_SESSION)
        status = STATUS_NON_TRADING
    elif d not in have:
        reasons.append(R_NO_SNAPSHOT)
        status = STATUS_TRADING_NO_DATA
    else:
        status = STATUS_TRADING_WITH_DATA
        if required_keys is not None:
            need = set(required_keys)
            got = set(present_keys or ())
            missing = sorted(need - got)
            if missing:
                reasons.append(R_INCOMPLETE)
                status = STATUS_TRADING_NO_DATA

    exact = status == STATUS_TRADING_WITH_DATA
    prior = [x for x in have if x < d]
    return {
        "date": d,
        "status": status,
        "exact": exact,
        "anchor": d if exact else None,
        "data_date": max(prior) if prior else None,
        "reasons": reasons,
        "missing_keys": missing,
        "calendar_basis": (calendar.basis() if isinstance(calendar, Calendar)
                           else "NONE"),
    }


def require_exact(verdict):
    """exact 闸门（**单点收口**）：不满足 ⇒ 抛 `DayError`，不返回"次优日期"。

    供消费者在写入任何「目标日事实」之前调用（B3 §4 / §3.6 L1 层）。
    """
    if not isinstance(verdict, dict) or "status" not in verdict:
        raise DayError("require_exact 需要 classify() 的 verdict，收到 %r" % (verdict,))
    if verdict["status"] != STATUS_TRADING_WITH_DATA:
        raise DayError(
            "日期不可用：%s status=%s reasons=%s（data_date=%s 仅信息展示，"
            "禁止用作回退数据源）"
            % (verdict.get("date"), verdict["status"], verdict.get("reasons"),
               verdict.get("data_date")))
    return verdict


# ── 缺口与连续性（日历轴；**禁止用"有数据日序列"顶替**） ──────────────────────
def gaps(calendar, data_dates, start, end):
    """[start, end] 内**是交易日但没有数据**的日期（升序）= 显式缺口清单。

    无日历 ⇒ 抛 `DayError`（挂日历才谈得上"缺口"；不挂就是"未知"，不能编缺口）。
    """
    if not isinstance(calendar, Calendar) or not len(calendar):
        raise DayError("gaps 需要日历（无日历只能说 UNKNOWN，不能编缺口）")
    have = frozenset(_norm_date(x) for x in data_dates)
    return tuple(s for s in calendar.sessions_between(start, end) if s not in have)


def next_session_of(calendar, d):
    """d 之后第一个交易日；无日历 / 越界 ⇒ None。"""
    if not isinstance(calendar, Calendar) or not len(calendar):
        return None
    return calendar.next_session(d)


def add_sessions(calendar, d, n):
    """**日历轴**：d 次日起第 n 个交易日；无日历 ⇒ DayError（禁止工作日近似）。

    与 `candidates._add_trading_days` 同口径（次日起数第 n 个），差异见 `Calendar.add_sessions`。
    """
    if not isinstance(calendar, Calendar) or not len(calendar):
        raise DayError("add_sessions 需要日历（工作日近似会静默压缩时间轴）")
    return calendar.add_sessions(d, n)


def sessions_between(calendar, a, b):
    """闭区间内交易日数；无日历 ⇒ DayError。"""
    if not isinstance(calendar, Calendar) or not len(calendar):
        raise DayError("sessions_between 需要日历")
    return calendar.sessions_between(a, b)


def is_contiguous(calendar, prev_session, as_of):
    """`as_of` 是否恰为 `prev` 的**下一个交易日**（连续处理推进的唯一判据）。

    返回 (ok, next_expected)。`prev_session` 为 None（首日）⇒ (True, as_of)。
    ⇒ 缺口日**不得**被静默跳过：不连续即 `ok=False`，调用方须 HALT 或显式补数。
    """
    if not isinstance(calendar, Calendar) or not len(calendar):
        raise DayError("is_contiguous 需要日历（否则无法区分「休市」与「缺口」）")
    if not prev_session:
        return (True, _norm_date(as_of))
    nxt = calendar.next_session(prev_session)
    return (nxt == _norm_date(as_of), nxt)


# ── CLI ──────────────────────────────────────────────────────────────────────
def _main(argv):
    if "--selftest" in argv:
        return 0 if _selftest() else 1
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    if "--calendar" in argv:
        i = argv.index("--calendar")
        if i + 1 >= len(argv):
            print("--calendar 需要 CSV 路径")
            return 2
        cal = Calendar.from_seed_csv(argv[i + 1])
        lo, hi = cal.span
        print("日历：source=%s version=%s 交易日=%d 区间=%s ~ %s"
              % (cal.source, cal.version, len(cal), lo, hi))
        if "--as-of" in argv:
            j = argv.index("--as-of")
            v = classify(argv[j + 1], calendar=cal, data_dates=[])
            print("判定 %s → status=%s exact=%s reasons=%s anchor=%s"
                  % (v["date"], v["status"], v["exact"], v["reasons"], v["anchor"]))
        else:
            for d in cal.sessions[-5:]:
                print("  末 5 个交易日之一：%s" % d)
        return 0
    print(__doc__)
    return 2


# ── 自检（纯内存 + 临时目录；生产 data/ 零接触） ────────────────────────────
_TEST_FUNCS = ("_selftest", "_discipline", "_prod_source", "_mk_cal",
               "_fixture_rows", "_write_tmp_cal", "_opt_real_cal")


def _mk_cal(*extra, **kw):
    """沙盒日历：2026-09-16 ~ 2026-09-30 的交易日（**09-25 非交易日**，同旧包实测）。"""
    base = ["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22",
            "2026-09-23", "2026-09-24", "2026-09-28", "2026-09-29", "2026-09-30"]
    return Calendar(list(base) + list(extra), **kw)


def _fixture_rows():
    """合成种子行（含非交易日 09-25 / 周末），用于验证 from_seed_rows 只取 is_trade。"""
    rows = []
    for d in ("2026-09-24", "2026-09-25", "2026-09-26", "2026-09-28"):
        rows.append({"date": d, "is_trade": 0 if d in ("2026-09-25", "2026-09-26") else 1})
    return rows


def _write_tmp_cal(tmp):
    """把合成种子写成临时 CSV（**仅在自检内**，不碰生产目录）。"""
    p = os.path.join(tmp, "trade_cal_fixture.csv")
    with io.open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "is_trade"])
        for r in _fixture_rows():
            w.writerow([r["date"], r["is_trade"]])
    return p


def _opt_real_cal():
    """可选：只读旧包真日历（不存在 ⇒ None，自检跳过并如实标注）。"""
    p = os.path.join(_LEGACY_ROOT, "data/raw/meta/trade_cal.csv")
    return p if os.path.isfile(p) else None


def _selftest():
    import tempfile
    fail = [0]

    def T(name, cond):
        if cond:
            print("  [✓] %s" % name)
        else:
            fail[0] += 1
            print("  [✗] %s" % name)

    cal = _mk_cal(source="FIXTURE", version="2026.09")
    DATA_FULL = ("2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21",
                 "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-28",
                 "2026-09-29", "2026-09-30")
    print("dayclass 自检（纯内存 + 临时目录；生产 data/ 零接触）：")

    # ── A 组：四态判定 ──
    v = classify("2026-09-29", calendar=cal, data_dates=DATA_FULL)
    T("A1 交易日 ∧ 有数据 → TRADING_WITH_DATA / exact=True / anchor=当日",
      v["status"] == STATUS_TRADING_WITH_DATA and v["exact"] is True
      and v["anchor"] == "2026-09-29" and v["reasons"] == [])

    v = classify("2026-09-25", calendar=cal, data_dates=DATA_FULL)
    T("A2 日历判非交易日 → NON_TRADING（**不是**缺口，reasons 指名日历证据）",
      v["status"] == STATUS_NON_TRADING and v["exact"] is False
      and v["anchor"] is None and v["reasons"] == [R_CALENDAR_NON_SESSION])

    v = classify("2026-09-23", calendar=cal, data_dates=("2026-09-16", "2026-09-22"))
    T("A3 交易日 ∧ 目标日无数据 → TRADING_NO_DATA / anchor=None（禁回退）",
      v["status"] == STATUS_TRADING_NO_DATA and v["exact"] is False
      and v["anchor"] is None and v["reasons"] == [R_NO_SNAPSHOT]
      and v["data_date"] == "2026-09-22")          # 仅信息展示

    v = classify("2026-09-29", calendar=None, data_dates=DATA_FULL)
    T("A4 **无日历 ⇒ UNKNOWN**（即使当日有数据也不放行；不按周末推算）",
      v["status"] == STATUS_UNKNOWN and v["exact"] is False
      and v["reasons"] == [R_NO_CALENDAR])

    v = classify("2026-12-31", calendar=cal, data_dates=DATA_FULL)
    T("A5 超出日历覆盖 ⇒ UNKNOWN（不得由「目录里没有」推成休市）",
      v["status"] == STATUS_UNKNOWN and v["reasons"] == [R_OUT_OF_RANGE])

    v = classify("2026-09-29", calendar=cal, data_dates=DATA_FULL,
                 required_keys=("daily_quote", "limit_up", "news",
                                "dragon_tiger", "env_metrics"),
                 present_keys=("daily_quote", "limit_up", "news"))
    T("A6 数据日存在但缺键 → 降级 TRADING_NO_DATA 且列出缺键（缺就是缺）",
      v["status"] == STATUS_TRADING_NO_DATA and v["exact"] is False
      and v["missing_keys"] == ["dragon_tiger", "env_metrics"])

    v = classify("2026-09-29", calendar=cal, data_dates=DATA_FULL,
                 required_keys=("daily_quote",), present_keys=("daily_quote",))
    T("A7 必需键齐备 → 仍 TRADING_WITH_DATA（缺键闸不误伤）",
      v["status"] == STATUS_TRADING_WITH_DATA and v["missing_keys"] == [])

    v = classify("2026-09-24", calendar=Calendar([]), data_dates=DATA_FULL)
    T("A8 空日历 ⇒ UNKNOWN（空日历 ≠ 全休市）",
      v["status"] == STATUS_UNKNOWN and v["reasons"] == [R_NO_CALENDAR])

    T("A9 四态值域封闭（恰 4 个，无第五态）",
      len(STATUSES) == 4 and set(STATUSES) == {
          "TRADING_WITH_DATA", "TRADING_NO_DATA", "NON_TRADING", "UNKNOWN"})

    # ── B 组：exact 闸门 ──
    ok = True
    try:
        require_exact(classify("2026-09-29", calendar=cal, data_dates=DATA_FULL))
    except DayError:
        ok = False
    T("B1 require_exact 对 WITH_DATA 放行（不抛）", ok)
    for d, why in (("2026-09-25", "NON_TRADING"), ("2026-09-23", "NO_DATA")):
        raised = False
        try:
            require_exact(classify(d, calendar=cal, data_dates=("2026-09-16",)))
        except DayError as e:
            raised = "data_date=" in str(e) and "禁止用作回退数据源" in str(e)
        T("B2 require_exact 对 %s 抛错且文案禁回退" % why, raised)
    raised = False
    try:
        require_exact(classify("2026-09-29", calendar=None, data_dates=DATA_FULL))
    except DayError:
        raised = True
    T("B3 require_exact 对 UNKNOWN 抛错（fail-closed 到底）", raised)
    raised = False
    try:
        require_exact({"oops": 1})
    except DayError:
        raised = True
    T("B4 require_exact 拒收非 verdict（防误用）", raised)

    # ── C 组：时间轴（日历轴 vs 工作日近似） ──
    T("C1 prev_session 跨休市：09-28 的前一交易日 = 09-24（**非** 09-25/09-27）",
      cal.prev_session("2026-09-28") == "2026-09-24")
    T("C2 next_session 跨休市：09-24 的后一交易日 = 09-28（跳过 09-25）",
      cal.next_session("2026-09-24") == "2026-09-28")
    T("C3 add_sessions(09-24, 3) = 09-30 —— 周末近似会错算成 09-29（提前一日）",
      cal.add_sessions("2026-09-24", 3) == "2026-09-30"
      and cal.add_sessions("2026-09-24", 2) == "2026-09-29"
      and cal.add_sessions("2026-09-24", 1) == "2026-09-28")
    T("C4 add_sessions(09-22, 5) = 09-30（窗口跨 09-25 休市；周末近似得 09-29）",
      cal.add_sessions("2026-09-22", 5) == "2026-09-30")
    T("C5 add_sessions 越界 ⇒ None（不近似、不外推）",
      cal.add_sessions("2026-09-30", 1) is None)
    T("C6 sessions_between(09-22, 09-30) 恰 6 个交易日（休市不进轴）",
      len(cal.sessions_between("2026-09-22", "2026-09-30")) == 6)
    T("C7 sessions_between 反转区间 ⇒ 空（不静默取绝对值）",
      cal.sessions_between("2026-09-30", "2026-09-22") == ())

    # ── D 组：缺口与连续性（禁止压缩时间轴） ──
    g = gaps(cal, ("2026-09-16", "2026-09-17", "2026-09-21", "2026-09-22",
                   "2026-09-23", "2026-09-24", "2026-09-28", "2026-09-29",
                   "2026-09-30"), "2026-09-16", "2026-09-30")
    T("D1 缺口清单恰含 09-18（交易日 ∧ 无数据），且**不含** 09-25（非交易日）",
      g == ("2026-09-18",))
    ok, nxt = is_contiguous(cal, "2026-09-17", "2026-09-18")
    T("D2 连续（09-17 → 09-18）判 ok", ok is True and nxt == "2026-09-18")
    ok, nxt = is_contiguous(cal, "2026-09-17", "2026-09-21")
    T("D3 跨缺口跳日（09-17 → 09-21）判**不连续**，期望日 = 09-18（禁静默跳日）",
      ok is False and nxt == "2026-09-18")
    ok, nxt = is_contiguous(cal, "2026-09-24", "2026-09-28")
    T("D4 跨休市（09-24 → 09-28）判**连续**（休市不占位，故不算缺口）",
      ok is True and nxt == "2026-09-28")
    ok, nxt = is_contiguous(cal, None, "2026-09-16")
    T("D5 首日（prev=None）判连续（唯一豁免）", ok is True and nxt == "2026-09-16")

    raised = False
    try:
        gaps(None, (), "2026-09-16", "2026-09-30")
    except DayError:
        raised = True
    T("D6 无日历调 gaps ⇒ 抛错（无日历只能说 UNKNOWN，不能编缺口）", raised)
    for fn, args in (("add_sessions", (None, "2026-09-24", 3)),
                     ("sessions_between", (None, "a", "b")),
                     ("is_contiguous", (None, "2026-09-24", "2026-09-28"))):
        raised = False
        try:
            globals()[fn](*args)
        except DayError:
            raised = True
        T("D7 无日历调 %s ⇒ 抛错（禁用工作日/有数据日近似）" % fn, raised)

    # ── E 组：种子解析（只读）与入参严进 ──
    c2 = Calendar.from_seed_rows(_fixture_rows())
    T("E1 from_seed_rows 只收 is_trade 真值（09-24/09-28 两日）",
      c2.sessions == ("2026-09-24", "2026-09-28"))
    tmp = tempfile.mkdtemp(prefix="ta_dayclass_")
    p = _write_tmp_cal(tmp)
    c3 = Calendar.from_seed_csv(p)
    T("E2 from_seed_csv 只读解析临时 CSV（含非交易日行不入轴）",
      c3.sessions == ("2026-09-24", "2026-09-28") and c3.source.endswith(".csv"))
    raised = False
    try:
        classify("2026/09/29", calendar=cal, data_dates=())
    except DayError:
        raised = True
    T("E3 非法日期格式 ⇒ DayError（严进；不猜、不补零）", raised)
    raised = False
    try:
        cal.add_sessions("2026-09-24", 0)
    except DayError:
        raised = True
    T("E4 n=0 ⇒ DayError（次日起数第 n 个，n 须 ≥1）", raised)

    # ── F 组：可选真日历交叉核验（只读；缺失则如实报 SKIP） ──
    rp = _opt_real_cal()
    if rp:
        rc = Calendar.from_seed_csv(rp, source="旧包 trade_cal.csv")
        T("F1 真日历 2026 全年交易日数 = 242",
          len(rc.sessions_between("2026-01-01", "2026-12-31")) == 242)
        T("F2 真日历判 2026-09-25 = NON_TRADING（与 klines 轴零矛盾）",
          classify("2026-09-25", calendar=rc, data_dates=())["status"]
          == STATUS_NON_TRADING)
        T("F3 真日历判 2026-09-24 的后一交易日 = 2026-09-28",
          rc.next_session("2026-09-24") == "2026-09-28")
        T("F4 真日历 add_sessions(2026-09-24, 3) = 2026-09-30（A4 到期日口径）",
          rc.add_sessions("2026-09-24", 3) == "2026-09-30")
        T("F5 真日历判 2026-10-01（国庆） = NON_TRADING",
          classify("2026-10-01", calendar=rc, data_dates=())["status"]
          == STATUS_NON_TRADING)
    else:
        print("  [--] F 组跳过：旧包真日历不存在（只读核验未执行，如实标注）")

    # ── G 组：纪律 ──
    T("G1 --discipline 全绿", _discipline())

    print("dayclass 自检：%d 失败" % fail[0])
    return fail[0] == 0


def _prod_source():
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    segs = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name not in _TEST_FUNCS:
            segs.append(ast.get_source_segment(src, n))
        if isinstance(n, ast.ClassDef):
            segs.append(ast.get_source_segment(src, n))
    return src, tree, "\n".join(segs)


def _discipline():
    ok = [0]

    def T(name, cond):
        if cond:
            ok[0] += 1
            print("  [✓] %s" % name)
        else:
            print("  [✗] %s" % name)

    src, tree, prod_src = _prod_source()
    print("dayclass 纪律自证：")

    # ① 零写点
    writes = [m for m in ('"w"', "'w'", '"a"', "'a'", '"x"', "'x'",
                          "makedirs", "os.remove", "os.replace", "os.rename",
                          "shutil", "json.dump") if m in prod_src]
    T("① 零写点（生产区无写模式 open / makedirs / remove / replace / rename）",
      not writes)
    # ② 精确判据：生产区不得出现任何「文件系统存在性探测」，且
    #    `STATUS_NON_TRADING` 唯一赋值点的守卫必须是日历判定（is_session）
    fs_probes = [m for m in ("os.path.isdir", "os.path.isfile", "os.path.exists",
                             "listdir", "glob.", "os.walk", "os.scandir")
                 if m in prod_src]
    lines = prod_src.split("\n")
    idx = [i for i, s in enumerate(lines) if "= STATUS_NON_TRADING" in s]
    guarded = bool(idx) and any(
        "is_session" in lines[i - 1] or "is_session" in lines[i - 2]
        for i in idx if i >= 2)
    T("② 零「目录缺失 ⇒ 休市」推断（无 fs 探测 ∧ NON_TRADING 只由日历判定赋值）",
      not fs_probes and len(idx) == 1 and guarded)
    T("③ 四态值域封闭（恰 4 个常量）",
      STATUSES == ("TRADING_WITH_DATA", "TRADING_NO_DATA", "NON_TRADING",
                   "UNKNOWN"))
    T("④ UNKNOWN 是缺省失败态（无日历 / 超范围分支均赋 UNKNOWN）",
      prod_src.count("status = STATUS_UNKNOWN") >= 2
      and "status = STATUS_NON_TRADING" in prod_src)
    T("⑤ astwin 引用（生产函数表经 astwin 枚举）",
      bool(ASTW.func_table(ast.parse(src)))
      and len(ASTW.func_table(ast.parse(src))) > 8)
    return ok[0] == 5


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
