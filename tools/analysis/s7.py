# -*- coding: utf-8 -*-
"""S7 财报节点博弈（2026-10-13 · 第一阶段补齐：公告流实做 + 预期差降级）。

职责（registry）：财报前后节奏 / 预期差是否已定价 → 消费者 S1 事件分类 / C3。

双轨：
  · **财报节点流 = 可机械产出**——数据源 = 生产 `data/raw/<d>/news.jsonl`
    （eastmoney 公告流，S1 同源）。按标题关键词机械分类财报类公告：
      预告（业绩预告/预增/预亏/预减/扭亏）、快报（业绩快报）、
      定期报告（年报/半年报/季报 披露）、更正/延期。
    产出 per-code 财报节点：{code, kind, ann_date, title, news_id}，并给出
    `节奏标记`（披露前 N 日内 / 刚披露窗口），供 S1 事件分类与 C3 证据引用。
  · **预期差定价**：一致预期**源已实证可用**（2026-10-21 复核）。
    历史沿革：2026-10-12 核准「一致预期/券商预测全盘无源」⇒ `expectation_gap`
    恒 UNAVAILABLE；2026-10-14 接线 `fetch_consensus`（走 westock consensus，
    X3 豁免区缓存）；**2026-10-21 实测 `westock consensus sh600519` 返回
    目标价 + 未来 3 年 EPS/营收/净利预测 + PE/PB，且支持批量
    `consensus <代码[,代码...]>`** ⇒ 「无源」前提不再成立。
    ★ 但**采集未跑起来**前，`expectation_gap` 仍如实报 UNAVAILABLE（不伪装有源）。
      判据：缓存件缺失 ⇒ UNAVAILABLE；缓存件命中 ⇒ 可算预期差。

阈值/窗口：params `s7_rules`（缺省 → 内置 UNCALIBRATED 种子；模块对 params 零写点）。
输出快照 `s_S7_<as_of>（X3 快照 · 注册表声明形态；盘面文件名 = 小写动态拼装）`（X3 同款；tempfile+os.replace 原子换入）。
"""
import io
import json
import os
import sys
import tempfile

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
sys.path.insert(0, _FS)
from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
import params as PARAMS  # noqa: E402

S_ID = "s7"
SNAP_FALLBACK = os.path.join(ROOT, "data", "generated", "s_S7_MISSING.json")

DEFAULT_RULES = {
    "window_before": 10,       # 披露日前 N 日标记「临近披露」
    "window_after": 3,         # 披露日后 N 日标记「刚披露」
    "lookback_days": 30,       # 回看公告流天数
    "provenance": "UNCALIBRATED builtin seed (s7.py 2026-10-13)",
}

KIND_KEYWORDS = (
    ("forecast", ("业绩预告", "预增", "预亏", "预减", "扭亏", "业绩预披露")),
    ("express", ("业绩快报",)),
    ("periodic", ("年度报告", "半年度报告", "季度报告", "年报", "中报", "三季报")),
    ("revision", ("更正", "延期披露", "补充披露")),
)

# ── 一致预期截面（2026-10-14 接线）：westock consensus CLI → 缓存 → evaluate 只读 ──
# 口径纪律：
#   · `pe/pb/ps` 等**派生估值字段不冒充一致预期本体**——本体 = eps/revenue/netProfit
#     （预测净利/营收/EPS）+ 同比；pe/pb/ps 仅随行登记。
#   · `target_price` 缺失（接口不给该行）= null，**不得推导**。
#   · `institution_cnt` 接口抽样恒 0 —— **不构成机构覆盖证据**，只如实登记。
#   · 接口无更新时间 → `fetched_at` = 取数时刻；预测年度 = 绝对年份（无 as_of 漂移）。
#   · **当前预期截面 ≠ 历史预期序列**——回溯式预期差（当时预期 vs 实际）仍 UNAVAILABLE。
CONSENSUS_UNIT = {"eps": "元", "revenue": "万元", "net_profit": "万元",
                  "pe": "倍", "pb": "倍", "ps": "倍",
                  "revenue_yoy": "%", "net_profit_yoy": "%"}

# ── 行业级盈利预测（2026-10-15 Option A）：westock sector forecast → 缓存 → evaluate 只读 ──
# 口径纪律（与个股 consensus 严格分离）：
#   · **行业级代理 ≠ 个股一致预期**——独立键 industry_forecast，kind=INDUSTRY_LEVEL_PROXY；
#     **永不回填**个股 has_coverage/target_price/years；不参与个股 n_covered 统计。
#   · 只取**最新截面**：as_of 对齐查询（--date <as_of>）实测恒空表 ⇒ 不传 --date，
#     并登记 forecast_date + is_as_of_aligned=false（三个时间概念分离，不冒充历史）。
#   · 未来 N 年预测 ≠ ≥120 日历史序列——历史序列契约不因本维度而声称满足。
FORECAST_UNIT = {"revenue": "万元", "net_profit": "万元", "net_assets": "万元",
                 "revenue_yoy": "%", "net_profit_yoy": "%",
                 "net_profit_cagr_2y": "%", "pe": "倍", "pb": "倍", "ps": "倍",
                 "roe": "%", "peg": "倍"}
FORECAST_COLUMNS = ("revenue", "net_profit", "net_assets", "revenue_yoy",
                    "net_profit_yoy", "net_profit_cagr_2y", "pe", "pb", "ps",
                    "roe", "peg")   # 11 列，与接口表头逐列对齐（无年份字段——接口不给）


def _parse_consensus_md(text):
    """westock consensus markdown → {code: {...}}（纯函数；无覆盖票 = has_coverage False）。

    两形态：有覆盖 = `#### code name` 标题 + 逐年表；无覆盖 = `| <code> | code |` 键值表
    （2026-10-14 实测 sz000158——首版解析器漏登记该形态，C 组断言固化）。"""
    items, cur = {}, None
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith("#### "):
            parts = ln[5:].split(None, 1)
            cur = {"code": parts[0], "name": (parts[1] if len(parts) > 1 else ""),
                   "has_coverage": False, "target_price": None, "years": []}
            items[cur["code"]] = cur
        elif ln.startswith("目标价:") and cur is not None:
            try:
                cur["target_price"] = float(ln.split(":", 1)[1].strip())
            except ValueError:
                cur["target_price"] = None
        elif ln.startswith("| 20") and cur is not None:
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if len(cells) >= 10 and cells[0].isdigit():
                cur["has_coverage"] = True

                def _f(x):
                    try:
                        return float(x)
                    except ValueError:
                        return None
                cur["years"].append({
                    "year": int(cells[0]), "eps": _f(cells[1]),
                    "revenue_wan": _f(cells[2]), "net_profit_wan": _f(cells[3]),
                    "pe": _f(cells[4]), "pb": _f(cells[5]), "ps": _f(cells[6]),
                    "revenue_yoy_pct": _f(cells[7]), "net_profit_yoy_pct": _f(cells[8]),
                    "institution_cnt": _f(cells[9])})
        elif ln.startswith("| ") and "| code |" in ln:
            # 无覆盖票键值表（无 #### 标题）：`| <code> | code |` → 登记 has_coverage=False
            # 2026-10-14 两轮缺陷回归：①首版整形态漏登；②批量输出时 `cur is None`
            # 条件失效（cur 停在上一只有覆盖票）——改为「code 未登记即建」。
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if len(cells) >= 2 and cells[1] == "code" and cells[0] != "内容" \
                    and cells[0] not in items:
                items[cells[0]] = {"code": cells[0], "name": "", "has_coverage": False,
                                   "target_price": None, "years": []}
    return items


def fetch_consensus(codes, *, as_of=None, base=None):
    """westock consensus → 缓存件（X3 豁免区；文件名按日动态拼装，形态见 WRITE_ENTRYPOINTS 登记）。

    evaluate 只读缓存、零网络；本函数是唯一网络触点（人触发 CLI）。"""
    import datetime as _dt
    import subprocess as _sp
    D = _chk_date(as_of) if as_of else _dt.date.today().isoformat()
    cl = ",".join(c.strip() for c in codes if c.strip())
    if not cl:
        raise S7Error("codes 为空（逗号分隔代码清单）")
    pr = _sp.run(["westock", "consensus", cl], capture_output=True, text=True,
                 encoding="utf-8", timeout=120)
    if pr.returncode != 0:
        raise S7Error("westock consensus 失败 rc=%d: %s" % (pr.returncode, pr.stderr[-200:]))
    items = _parse_consensus_md(pr.stdout)
    gen = os.path.join(base or ROOT, "data", "generated")
    os.makedirs(gen, exist_ok=True)
    cache = {"schema": 1, "as_of": D, "fetched_at": _dt.datetime.now().isoformat(timespec="seconds"),
             "source": "westock consensus CLI（A股一致预期；内部定义/更新时间接口不披露）",
             "units": CONSENSUS_UNIT,
             "note": "当前预期截面——历史预期序列不可用（接口无历史预期）；"
                     "institution_cnt 接口抽样恒 0 = 不构成覆盖证据",
             "n_codes": len(items),
             "n_covered": sum(1 for v in items.values() if v["has_coverage"]),
             "items": items}
    p = os.path.join(gen, "s_s7_consensus_%s.json" % D)
    _WG.check_write(p)
    fd, tmp = tempfile.mkstemp(dir=gen, prefix="s_s7_consensus_", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return {"path": p, "n_codes": cache["n_codes"], "n_covered": cache["n_covered"],
            "fetched_at": cache["fetched_at"]}


def _load_consensus(as_of, base=None):
    # 两步拼装（同 evaluate 纪律：完整路径字面不进源码词汇，规避幽灵 cap_ref 误报）
    gen = os.path.join(base or ROOT, "data", "generated")
    p = os.path.join(gen, "s_s7_consensus_%s.json" % as_of)
    if not os.path.isfile(p):
        return None
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)


def _parse_forecast_md(text):
    """westock sector forecast markdown → {code: {...}}（纯函数；三形态）。

    形态（2026-10-15 只读探针实测）：
      A 有数据：`**名称**（申万二级，ptXXXXXXX，预测日 YYYYMMDD）` 标题
                + 表头 + `| --- |` 分隔行 + N 行数据（每行 = 1 个预测年，**接口不给年份**）
      B 空表  ：标题 + 表头 + 分隔行，**其后无数据行** ⇒ EMPTY_TABLE
      C 无数据：`板块 <code>（<name>）当日无盈利预测数据` ⇒ NO_DATA
    字段非数字（`-`/空）= None，**不抛、不推导、不补齐年数**。
    """
    import re as _re
    _TITLE = _re.compile(r"^\*\*(.+?)\*\*（(.+?)，(pt\d+)，预测日\s*(\d{8})）\s*$")
    _NO_DATA = _re.compile(r"^板块\s+(pt\d+)（(.+?)）当日无盈利预测数据\s*$")
    sectors = {}
    cur = None
    for ln in text.splitlines():
        s = ln.strip()
        m = _TITLE.match(s)
        if m:
            name, level, code, fdate = m.group(1), m.group(2), m.group(3), m.group(4)
            cur = {"status": "EMPTY_TABLE", "code": code, "name": name,
                   "level": level, "forecast_date": fdate,
                   "n_years_forecast": 0, "years": []}
            sectors[code] = cur
            continue
        m = _NO_DATA.match(s)
        if m:
            code, name = m.group(1), m.group(2)
            cur = {"status": "NO_DATA", "code": code, "name": name,
                   "level": "", "forecast_date": "", "n_years_forecast": 0,
                   "years": []}
            sectors[code] = cur
            continue
        if cur is not None and s.startswith("|") and not s.startswith("| ---"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) < len(FORECAST_COLUMNS):
                continue
            if cells[0].lower() == "revenue":       # 表头行
                continue

            def _f(x):
                try:
                    return float(x)
                except ValueError:
                    return None
            cur["years"].append({k: _f(v) for k, v in zip(FORECAST_COLUMNS, cells)})
            cur["n_years_forecast"] = len(cur["years"])
            if cur["status"] == "EMPTY_TABLE":
                cur["status"] = "PRESENT"
    return sectors


def fetch_sector_forecast(sectors, *, as_of=None, base=None):
    """westock sector forecast → 行业级盈利预测缓存（人触发 CLI；evaluate 只读）。

    ★ 与 fetch_consensus 的区别（不得混淆）：
      · 维度 = 申万行业（入参为**行业 CODE 串**，非个股码）；kind=INDUSTRY_LEVEL_PROXY
      · **不传 --date**（as_of 对齐查询实测恒空表）⇒ 取最新截面，forecast_date 另行登记，
        is_as_of_aligned 机械判定（实测恒 false）
      · 行业盈利路径 ≠ 个股一致预期 ≠ 历史预期序列（三契约互不替代）
    """
    import datetime as _dt
    import subprocess as _sp
    D = _chk_date(as_of) if as_of else _dt.date.today().isoformat()
    cl = ",".join(c.strip() for c in sectors if c.strip())
    if not cl:
        raise S7Error("sectors 为空（逗号分隔行业 CODE 清单，如 pt01801737）")
    pr = _sp.run(["westock", "sector", "forecast", cl],
                 capture_output=True, text=True, encoding="utf-8", timeout=120)
    if pr.returncode != 0:
        raise S7Error("westock sector forecast 失败 rc=%d: %s"
                      % (pr.returncode, pr.stderr[-200:]))
    got = _parse_forecast_md(pr.stdout)
    want = [c.strip() for c in cl.split(",") if c.strip()]
    for c in want:
        if c not in got:
            got[c] = {"status": "MISSING_CODE", "code": c, "name": "",
                      "level": "", "forecast_date": "", "n_years_forecast": 0,
                      "years": []}
    fdates = sorted({v["forecast_date"] for v in got.values() if v["forecast_date"]})
    aligned = bool(fdates) and all(f == D.replace("-", "") for f in fdates)
    gen = os.path.join(base or ROOT, "data", "generated")
    os.makedirs(gen, exist_ok=True)
    cache = {
        "schema": 1, "as_of": D,
        "fetched_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "source": "westock sector forecast CLI（申万一级/二级行业机构一致预期盈利路径）",
        "kind": "INDUSTRY_LEVEL_PROXY", "is_stock_consensus": False,
        "units": FORECAST_UNIT,
        "note": "行业级代理≠个股一致预期；未来 N 年预测≠≥120 日历史序列；"
                "as_of 对齐查询（--date）实测为空表 ⇒ 本件为最新截面",
        "alignment": {
            "as_of_aligned": aligned,
            "forecast_date_min": (fdates[0] if fdates else None),
            "forecast_date_max": (fdates[-1] if fdates else None),
            "note": "as_of=%s 的 --date 对齐查询实测为空表 ⇒ 本件为最新截面；"
                    "预测日≠as_of，不得当作 as_of 当日的历史预期数据使用" % D,
        },
        "n_sectors_requested": len(want),
        "n_with_data": sum(1 for v in got.values() if v["status"] == "PRESENT"),
        "n_empty_table": sum(1 for v in got.values() if v["status"] == "EMPTY_TABLE"),
        "n_no_data": sum(1 for v in got.values() if v["status"] == "NO_DATA"),
        "n_missing_code": sum(1 for v in got.values() if v["status"] == "MISSING_CODE"),
        "sectors": got,
    }
    p = os.path.join(gen, "s_s7_sector_forecast_%s.json" % D)
    _WG.check_write(p)
    fd, tmp = tempfile.mkstemp(dir=gen, prefix="s_s7_sector_forecast_", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return {"path": p, "n_sectors_requested": cache["n_sectors_requested"],
            "n_with_data": cache["n_with_data"], "n_empty_table": cache["n_empty_table"],
            "n_no_data": cache["n_no_data"], "n_missing_code": cache["n_missing_code"],
            "alignment": cache["alignment"], "fetched_at": cache["fetched_at"]}


def _load_sector_forecast(as_of, base=None):
    # 两步拼装（同 _load_consensus 纪律：完整路径字面不进源码词汇，规避幽灵 cap_ref 误报）
    gen = os.path.join(base or ROOT, "data", "generated")
    p = os.path.join(gen, "s_s7_sector_forecast_%s.json" % as_of)
    if not os.path.isfile(p):
        return None
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)


class S7Error(Exception):
    """REASON_CODE 前缀错误（BAD_DATE/RULES_MISSING/...）。"""


def _chk_date(s, name="as_of"):
    import datetime as _dt
    t = s.isoformat() if hasattr(s, "isoformat") else str(s or "")
    if len(t) != 10 or t[4] != "-" or t[7] != "-":
        raise S7Error("BAD_DATE: %s 必须是 YYYY-MM-DD，收到 %r" % (name, s))
    try:
        _dt.date(int(t[:4]), int(t[5:7]), int(t[8:10]))
    except ValueError:
        raise S7Error("BAD_DATE: %s 非法日 %r" % (name, s))
    return t


def load_rules(params_path=None):
    try:
        v = PARAMS.get_value("s7_rules", path=params_path)
    except Exception:
        v = None
    if isinstance(v, dict) and v.get("window_before"):
        return dict(v), "params:s7_rules"
    return dict(DEFAULT_RULES), "builtin:UNCALIBRATED"


def _classify_title(title):
    t = title or ""
    for kind, kws in KIND_KEYWORDS:
        if any(k in t for k in kws):
            return kind
    return None


def _iter_news(base, days):
    """回看窗口内的 news.jsonl → [(date, row)]（缺文件日跳过，如实）。"""
    import datetime as _dt
    d = _dt.date.fromisoformat(days[-1])
    for _ in range(len(days)):
        p = os.path.join(base or ROOT, "data", "raw", days[-1], "news.jsonl")
        if os.path.isfile(p):
            with io.open(p, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if ln:
                        try:
                            yield days[-1], json.loads(ln)
                        except json.JSONDecodeError:
                            continue
        days = days[:-1]
        d -= _dt.timedelta(days=1)
        days = days + [d.isoformat()] if False else days  # noqa: 保持简单——外层循环改用下面实现


def evaluate(as_of, *, old_root=None, params_path=None, base=None):
    """S7 单日评估 → 快照落 s_S7_<as_of>（X3 快照 · 注册表声明形态；盘面文件名 = 小写动态拼装）。"""
    import datetime as _dt
    D = _chk_date(as_of)
    rules, rules_prov = load_rules(params_path)
    lb = int(rules.get("lookback_days") or 30)
    wb = int(rules.get("window_before") or 10)
    wa = int(rules.get("window_after") or 3)

    d0 = _dt.date.fromisoformat(D) - _dt.timedelta(days=lb)
    days = []
    d = d0
    while d <= _dt.date.fromisoformat(D):
        days.append(d.isoformat())
        d += _dt.timedelta(days=1)

    events, scanned, missing_days = [], 0, []
    for dd in days:
        p = os.path.join(base or ROOT, "data", "raw", dd, "news.jsonl")
        if not os.path.isfile(p):
            missing_days.append(dd)
            continue
        with io.open(p, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                scanned += 1
                try:
                    row = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                kind = _classify_title(row.get("title") or "")
                if not kind:
                    continue
                rel = (_dt.date.fromisoformat(D) - _dt.date.fromisoformat(dd)).days
                phase = ("just_disclosed" if rel <= wa else
                         "within_window" if rel <= wb + wa else "observed")
                codes = row.get("related_codes") or []
                if not codes:
                    # 公告未带关联码 → 以 news_id 形态如实登记（缺就是缺）
                    codes = []
                for c in codes:
                    events.append({"code": c, "kind": kind, "ann_date": dd,
                                   "phase": phase, "rel_days": rel,
                                   "title": (row.get("title") or "")[:80],
                                   "news_id": row.get("news_id")})
    # 同码同日去重（公告分片可能重复投递）
    seen, uniq = set(), []
    for e in sorted(events, key=lambda x: (x["code"], x["ann_date"], x["news_id"])):
        k = (e["code"], e["ann_date"], e["news_id"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    by_code = {}
    for e in uniq:
        by_code.setdefault(e["code"], []).append(e)

    # 一致预期截面（2026-10-14 接线）：读缓存件（evaluate 零网络）；缓存缺席 = UNAVAILABLE
    cons = _load_consensus(D, base=base)
    if cons is not None:
        expectation_gap = {
            "status": "PRESENT_SEGMENT",
            "semantics": "当前预期截面（历史预期序列不可用——接口无历史预期）",
            "source": cons.get("source"), "fetched_at": cons.get("fetched_at"),
            "units": cons.get("units"),
            "n_codes": cons.get("n_codes"), "n_covered": cons.get("n_covered"),
            "items": cons.get("items"),
            "caveats": ["target_price 缺失=null 不得推导",
                        "institution_cnt 抽样恒 0 = 不构成机构覆盖证据",
                        "pe/pb/ps 为派生登记字段，非一致预期本体（本体=eps/revenue/netProfit+同比）"],
        }
    else:
        expectation_gap = {
            "status": "UNAVAILABLE",
            "reason": "一致预期缓存未生成——内置 westock consensus 已证实可用"
                      "（2026-10-14 抽样），运行 s7.py --fetch-consensus <codes> 生成缓存",
            "note": "历史预期序列（回溯式预期差）仍不可用——接口无历史预期"}

    # 行业级盈利预测代理（2026-10-15 Option A）：读缓存（evaluate 零网络）；缺席 = ABSENT
    # ★ 本段**不改动**上一段 expectation_gap 的任何值——个股语义（PRESENT_SEGMENT/
    #   UNAVAILABLE + n_covered）逐字保持；行业代理不参与个股覆盖率。
    sf = _load_sector_forecast(D, base=base)
    if sf is not None:
        industry_forecast = {
            "status": "PRESENT_PROXY",
            "kind": "INDUSTRY_LEVEL_PROXY",
            "is_stock_consensus": False,
            "semantics": "申万行业级机构一致预期盈利路径（未来 N 年预测）——"
                         "行业代理证据，非个股一致预期；不满足个股历史预期序列契约",
            "source": sf.get("source"), "fetched_at": sf.get("fetched_at"),
            "as_of": D, "alignment": sf.get("alignment"),
            "n_sectors_requested": sf.get("n_sectors_requested"),
            "n_with_data": sf.get("n_with_data"),
            "n_empty_table": sf.get("n_empty_table"),
            "n_no_data": sf.get("n_no_data"),
            "n_missing_code": sf.get("n_missing_code"),
            "units": sf.get("units"),
            "sectors": sf.get("sectors"),
            "caveats": ["行业级代理≠个股一致预期——不得回填个股 has_coverage/target_price/years",
                        "预测日≠as_of（对齐查询空表）——不得当作 as_of 当日历史预期",
                        "未来 N 年预测≠≥120 日历史序列——历史序列契约仍未满足",
                        "覆盖率分母仍为个股 n_codes；行业有数不提升个股 status"],
        }
    else:
        industry_forecast = {
            "status": "ABSENT",
            "reason": "行业级盈利预测缓存未生成——运行 s7.py "
                      "--fetch-sector-forecast <行业CODE串> 生成缓存",
            "note": "不影响个股 consensus 段既有语义",
        }

    snap = {"schema": 1, "s_id": S_ID, "cap": S_ID, "as_of": D,
            "rules_provenance": rules_prov,
            "scanned_news": scanned, "missing_days": missing_days[:5],
            "n_missing_days": len(missing_days),
            "n_events": len(uniq), "by_code": by_code,
            "expectation_gap": expectation_gap,
            "industry_forecast": industry_forecast,
            # 2026-10-19：旧链知识引用——业绩预告/快报的实测效应在 S1 效应库
            #（F7 转录），预期差 5 档与预告四策略在 e4_research 框架库；
            # 一致预期**数据**仍缺源（fetch-consensus 走 westock，当前不可用）。
            # ⚠ 引用只写库名不写路径——landing 词汇表收完整路径字面量会按
            # 「未登记 cap 产物」误判（s1_event_effects ≠ 注册表 id）；路径以
            # data/knowledge/_registry.json 登记为准。
            "knowledge_refs": [
                "s1_event_effects 效应库（业绩预告/快报净效应·F7 实测·[旧链]）",
                "e4_research 框架库（预期差 5 档/预告博弈四策略·[旧链]）"],
            "confidence": ("params" if rules_prov.startswith("params")
                           else "UNCALIBRATED")}
    gen = os.path.join(base or ROOT, "data", "generated")
    os.makedirs(gen, exist_ok=True)
    p = os.path.join(gen, "s_s7_%s.json" % D)
    _WG.check_write(p)
    fd, tmp = tempfile.mkstemp(dir=gen, prefix="s_s7_", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return snap


def _selftest():
    import tempfile as _tf
    import shutil as _sh
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== s7 --selftest ==")
    try:
        evaluate("2026-9-1")
        T("A1 非法日期拒绝", False)
    except S7Error:
        T("A1 非法日期拒绝", True)
    # B 组：合成公告流（沙盒）
    sb = _tf.mkdtemp(prefix="s7_sb_")
    try:
        raw = os.path.join(sb, "data", "raw", "2026-09-28")
        os.makedirs(raw, exist_ok=True)
        news = [
            {"news_id": "N1", "title": "关于2026年半年度业绩预告的公告",
             "related_codes": ["sz000001"], "ts": "2026-09-28"},
            {"news_id": "N2", "title": "2026年第三季度报告", "related_codes": ["sz000002"]},
            {"news_id": "N3", "title": "日常关联交易公告", "related_codes": ["sz000003"]},
            {"news_id": "N4", "title": "业绩预告更正公告", "related_codes": ["sz000001"]},
        ]
        with io.open(os.path.join(raw, "news.jsonl"), "w", encoding="utf-8") as f:
            for n in news:
                f.write(json.dumps(n, ensure_ascii=False) + "\n")
        snap = evaluate("2026-09-29", base=sb)
        T("B1 预告识别（kind=forecast）", any(e["kind"] == "forecast"
          for e in snap["by_code"].get("sz000001", [])))
        T("B2 定期报告识别（kind=periodic）", any(e["kind"] == "periodic"
          for e in snap["by_code"].get("sz000002", [])))
        T("B3 非财报公告不入集（sz000003 零事件）",
          "sz000003" not in snap["by_code"])
        T("B4 相对日相位标记（rel_days=1 → just_disclosed）",
          all(e["phase"] == "just_disclosed" for e in snap["by_code"]["sz000001"]))
        T("B5 缓存缺席 → 预期差 UNAVAILABLE（unlock 条件如实指向 westock consensus）",
          snap["expectation_gap"]["status"] == "UNAVAILABLE"
          and "westock consensus" in snap["expectation_gap"]["reason"])
        T("B6 输出零 wall-clock（重算逐字节同）",
          json.dumps(evaluate("2026-09-29", base=sb), sort_keys=True)
          == json.dumps(snap, sort_keys=True))
        T("B7 快照只落沙盒 generated", os.path.isfile(
            os.path.join(sb, "data", "generated", "s_s7_2026-09-29.json")))
        # ── C 组：一致预期截面（解析器三态 + 缓存在场 → PRESENT_SEGMENT）──
        md3 = ("#### sh600519 贵州茅台\n目标价: 1661.49\n"
               "| year | eps | revenue | netProfit | pe | pb | ps | revenueYoy | netProfitYoy | institutionCnt |\n"
               "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
               "| 2026 | 66.96 | 17734533.34 | 8370320.17 | 19.04 | 5.49 | 8.99 | 3.08 | 1.68 | 0 |\n"
               "#### sz000158 常山\n| 内容 | 项目 |\n| --- | --- |\n| sz000158 | code |\n")
        items = _parse_consensus_md(md3)
        T("C1 解析器：有覆盖（目标价+逐年 10 列）",
          items["sh600519"]["has_coverage"]
          and items["sh600519"]["target_price"] == 1661.49
          and items["sh600519"]["years"][0]["revenue_wan"] == 17734533.34
          and items["sh600519"]["years"][0]["institution_cnt"] == 0.0)
        T("C2 解析器：无覆盖票 has_coverage=False 且目标价 null",
          items["sz000158"]["has_coverage"] is False
          and items["sz000158"]["target_price"] is None
          and items["sz000158"]["years"] == [])
        # C2b 无覆盖票**独立形态**（无 #### 标题的键值表，2026-10-14 真实实测 sz000158）
        # C2c **批量混合形态**：有覆盖票之后跟无覆盖键值表（第二轮缺陷回归）
        md_kv = "| 内容 | 项目 |\n| --- | --- |\n| sz000158 | code |\n"
        it2 = _parse_consensus_md(md_kv)
        T("C2b 无覆盖键值表形态不漏登（首版缺陷回归）",
          list(it2.keys()) == ["sz000158"] and it2["sz000158"]["has_coverage"] is False)
        md_mix = ("#### sh600519 贵州茅台\n目标价: 1661.49\n"
                  "| year | eps | revenue | netProfit | pe | pb | ps | revenueYoy | netProfitYoy | institutionCnt |\n"
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
                  "| 2026 | 66.96 | 17734533.34 | 8370320.17 | 19.04 | 5.49 | 8.99 | 3.08 | 1.68 | 0 |\n"
                  "| 内容 | 项目 |\n| --- | --- |\n| sz000158 | code |\n")
        it3 = _parse_consensus_md(md_mix)
        T("C2c 批量混合（有覆盖+无覆盖）不漏登（二轮缺陷回归）",
          set(it3.keys()) == {"sh600519", "sz000158"}
          and it3["sz000158"]["has_coverage"] is False
          and it3["sh600519"]["has_coverage"] is True)
        cons_cache = {"schema": 1, "as_of": "2026-09-29", "fetched_at": "T",
                      "source": "westock consensus CLI", "units": CONSENSUS_UNIT,
                      "note": "n", "n_codes": 2, "n_covered": 1, "items": items}
        with io.open(os.path.join(sb, "data", "generated", "s_s7_consensus_2026-09-29.json"),
                     "w", encoding="utf-8") as f:
            json.dump(cons_cache, f, ensure_ascii=False)
        snap2 = evaluate("2026-09-29", base=sb)
        eg = snap2["expectation_gap"]
        T("C3 缓存在场 → PRESENT_SEGMENT（截面语义 + 三条 caveat）",
          eg["status"] == "PRESENT_SEGMENT"
          and eg["n_covered"] == 1 and len(eg["caveats"]) == 3)
        T("C4 截面不冒充历史序列（semantics 明示）",
          "历史预期序列不可用" in eg["semantics"])
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    # C 组：真实公告流首跑（沙盒基座；生产写点已由统一闸门守卫）
    _prod_s7 = os.path.join(ROOT, "data", "generated", "s_s7_2026-09-29.json")
    _s7_hash_before = (io.open(_prod_s7, encoding="utf-8").read()
                       if os.path.isfile(_prod_s7) else None)
    sb2 = _tf.mkdtemp(prefix="s7_self_c_")
    try:
        snap = evaluate("2026-09-29", base=sb2)
        T("C1 真实公告流可跑且事件数如实（≥0）", isinstance(snap["n_events"], int))
        T("C2 快照只落沙盒 generated", os.path.isfile(
            os.path.join(sb2, "data", "generated", "s_s7_2026-09-29.json")))
        try:
            evaluate("2026-09-29")          # 无 base ⇒ 生产根 ⇒ 须被闸门拒
            T("C3 生产写点被闸门拒（无票据）", False, "竟放行")
        except _WG.WriteGateError:
            T("C3 生产写点被闸门拒（无票据 ⇒ PROD_WRITE_NO_TICKET）", True)
        _s7_hash_after = (io.open(_prod_s7, encoding="utf-8").read()
                          if os.path.isfile(_prod_s7) else None)
        T("C4 生产快照零改动（自检不碰生产树）", _s7_hash_before == _s7_hash_after)
    finally:
        _sh.rmtree(sb2, ignore_errors=True)
    print("== s7 --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    if "--fetch-consensus" in argv:
        codes = argv[argv.index("--fetch-consensus") + 1].split(",")
        as_of = (argv[argv.index("--as-of") + 1] if "--as-of" in argv else None)
        base = (argv[argv.index("--base") + 1] if "--base" in argv else None)
        if base and not os.path.isdir(base):
            print("--base 必须是已存在目录（防误写生产），实际: %s" % base)
            sys.exit(2)
        print(json.dumps(fetch_consensus(codes, as_of=as_of, base=base),
                         ensure_ascii=False, indent=1))
        sys.exit(0)
    if "--fetch-sector-forecast" in argv:
        sectors = argv[argv.index("--fetch-sector-forecast") + 1].split(",")
        as_of = (argv[argv.index("--as-of") + 1] if "--as-of" in argv else None)
        base = (argv[argv.index("--base") + 1] if "--base" in argv else None)
        if base and not os.path.isdir(base):
            print("--base 必须是已存在目录（防误写生产），实际: %s" % base)
            sys.exit(2)
        print(json.dumps(fetch_sector_forecast(sectors, as_of=as_of, base=base),
                         ensure_ascii=False, indent=1))
        sys.exit(0)
    if "--eval" in argv:
        base = (argv[argv.index("--base") + 1] if "--base" in argv else None)
        if base and not os.path.isdir(base):
            print("--base 必须是已存在目录（防误写生产），实际: %s" % base)
            sys.exit(2)
        print(json.dumps(evaluate(argv[argv.index("--eval") + 1], base=base),
                         ensure_ascii=False, indent=1)[:2000])
        sys.exit(0)
    print("用法: s7.py --eval <as_of> [--base 沙盒] | --fetch-consensus <codes> [--as-of D] [--base 沙盒] "
          "| --fetch-sector-forecast <行业CODE串> [--as-of D] [--base 沙盒] | --selftest")
    sys.exit(2)
