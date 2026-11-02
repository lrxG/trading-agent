# -*- coding: utf-8 -*-
"""S6 估值定价判断（2026-10-13 · 第一阶段补齐：契约 + 降级，核心待数据源）。

职责（registry）：估值法 / 市场交易什么预期 / 重估风险 → 消费者 C5 赔率定价。

**缺源核准（2026-10-12，不得假装可产出）**：
  · 行业/个股 PE·PB·PS 估值序列：全盘无源（data/raw 实测无财务/估值字段）
  · 一致预期（分析师预期）：无源
  · 因此 S6 的三项核心产出（估值法判定 / 交易预期识别 / 重估风险）**当前不可机械产出**。

本件提供：
  1. `availability(as_of)` —— 消费者（C5/E7）统一入口：如实返回
     {ready: False, gaps: [...], wait_for: [...]}，缺什么列什么，零编造；
  2. `evaluate(as_of)` —— 判定入口：数据未就绪时返回 UNAVAILABLE 判定包
     （显式 gap 清单 + 解锁条件），**不产出伪装估值**；源接入后按同一契约
     填充 verdicts（估值分位/预期差/重估触发），接口与落盘契约一次定死；
  3. 落盘契约（源接入后启用）：`data/generated/s_s6_<as_of>.json`（X3 同款；
     当前**不写**——每日产 UNAVAILABLE 快照属噪音，消费者以 availability 为准）。

设计红线：禁以价格涨幅冒充估值；禁以"市值/随意口径利润"编造 PE。
"""
import io
import json
import os
import sys
import tempfile

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
sys.path.insert(0, _FS)
from paths import ROOT  # noqa: E402
import cli_codes as CC  # noqa: E402  ★ CLI 代码格式单真源（2026-11-02）
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）

_LEGACY_ROOT = (os.environ.get("OLD_SYSTEM_ROOT")
                or os.path.join(os.path.expanduser("~"), "炒股智能体_旧包_20261008"))  # 旧系统只读参考源；换机器时设 OLD_SYSTEM_ROOT 或放家目录

S_ID = "s6"
OLD_ROOT = _LEGACY_ROOT
_OLD_PKG = OLD_ROOT

# 数据源就绪判据（逐项实测口径；接入新源时在此登记，不散落调用方）
# ★ 2026-10-20 数据层贯通：估值序列已采（sector_seed --history →
#   `data/raw/meta/sector_valuation_history.csv`，78 交易日 × 124 板块），
#   但 westock sector valuation 历史模式**硬边界 = 2026-06-24 起**（实测更早无数据）
#   ⇒ 深度 78 < 120 契约 ⇒ 仍不就绪，且如实标注「源深度不足」而非「未采集」。
SOURCE_CHECKS = (
    ("valuation_series", "个股/行业 PE·PB·PS 估值序列",
     ("data/raw/meta/", "sector_valuation_history")),
    ("consensus_estimates", "一致预期（分析师盈利预测）",
     ("data/raw/bus/", "consensus|estimate")),
)
SERIES_MIN_DAYS = 120          # ≥120 交易日契约（本文件顶部 docstring 同款）


class S6Error(Exception):
    """REASON_CODE 前缀错误。"""


def _probe_sources(old_root=None):
    """逐项探测数据源是否就绪（只读文件系统，零网络）。

    活库优先（2026-10-20 同 s3 口径）：本库 data/ > 旧包 fallback。
    valuation_series 附加**序列深度校验**——文件在场但 <120 日 ⇒ 不就绪
    （reason 如实区分「未采集」与「源深度不足」，禁把 78 日当 120 日用）。
    """
    import csv as _csv
    import io as _io
    roots = [ROOT] + ([] if old_root else [_OLD_PKG])
    if old_root:
        roots = [old_root]
    out = []
    for key, label, (rel, pat) in SOURCE_CHECKS:
        found = []
        hit_root = None
        for root in roots:
            d = os.path.join(root, rel)
            if os.path.isdir(d):
                for fn in os.listdir(d):
                    low = fn.lower()
                    if any(t in low for t in pat.split("|")) and low.endswith(".csv"):
                        found.append(fn)
                        hit_root = root
        item = {"key": key, "label": label, "ready": bool(found),
                "found": found[:5]}
        if key == "valuation_series" and found:
            # 序列深度校验（读 date 列唯一值）
            days = set()
            try:
                with _io.open(os.path.join(hit_root, rel, found[0]),
                              encoding="utf-8-sig") as f:
                    for r in _csv.DictReader(f):
                        if r.get("date"):
                            days.add(r["date"])
            except Exception:                             # noqa: BLE001
                pass
            item["series_days"] = len(days)
            if len(days) < SERIES_MIN_DAYS:
                item["ready"] = False
                item["reason"] = ("序列深度不足：%d/%d 交易日——westock sector "
                                  "valuation 历史模式硬边界 2026-06-24 起"
                                  "（非采集遗漏，补不到更早）" % (len(days),
                                                                SERIES_MIN_DAYS))
        out.append(item)
    return out


def availability(as_of, *, old_root=None):
    """消费者统一入口：S6 当前能否产出真实判定。零编造。"""
    src = _probe_sources(old_root)
    gaps = [x["label"] for x in src if not x["ready"]]
    return {"s_id": S_ID, "as_of": as_of, "ready": not gaps,
            "sources": src, "gaps": gaps,
            # 2026-10-19 通道实证（westock CLI 本机畅通）：「等一个未知源」改为
            # 「等一个已实证命令的采集工程化」——消费者据此知道阻塞的性质。
            # 2026-10-20 更新：估值序列已采 78 日（sector_valuation_history.csv），
            # 但源硬边界 2026-06-24 起补不到 120 ⇒ 阻塞性质 = 源深度不足（非未采）。
            "wait_for": ["估值序列深度：已采 %d/120 交易日（sector_seed --history · "
                         "源硬边界 2026-06-24 起，westock 更早无数据）⇒ 需历史更深的"
                         "估值源或下调契约（改契约=显式治理动作）"
                         % (src[0].get("series_days") or 0),
                         "一致预期源：westock sector forecast（通道已实证，待采集）"],
            "unlocked_outputs": [] if gaps else
            ["valuation_percentile", "expectation_priced_in", "repricing_risk"]}


def evaluate(as_of, *, old_root=None, params_path=None, base=None):
    """判定入口：缺源 ⇒ 返回 UNAVAILABLE 判定包（零伪装、零快照落盘）。

    2026-10-14 截面接线（不影响 ≥120 日序列契约）：
      读缓存件 `s_s6_section_<as_of>.json`（westock quote 个股截面 + sector
      valuation 行业截面，人触发 fetch）→ 输出段 `valuation_section`：
      PRESENT_SEGMENT（截面语义 + 数据时间/来源/缺失逐项）或 ABSENT。
      `valuation_series`（≥120 日）契约**恒保留**——单日截面不得伪装序列。"""
    from s4 import _chk_date  # 同款日期契约（一个家，不复制）
    D = _chk_date(as_of)
    av = availability(D, old_root=old_root)
    sec = _load_section(D, base=base)
    section = ({"status": "ABSENT",
                "reason": "截面缓存未生成——运行 s6.py --fetch-section <codes[,pt_codes]> 生成"}
               if sec is None else
              {"status": "PRESENT_SEGMENT",
               "semantics": "当前截面（非序列；≥120 日估值序列契约仍未满足）",
               "source": sec.get("source"), "fetched_at": sec.get("fetched_at"),
               "stock_section": sec.get("stock_section"),
               "sector_section": sec.get("sector_section"),
               "missing": sec.get("missing")})
    if av["ready"]:
        raise S6Error("SOURCE_READY: 估值序列源已接入——按 data_contract 填充 verdicts "
                      "（本分支待源接入后实现，禁以占位逻辑冒充估值判断）")
    return {"schema": 1, "s_id": S_ID, "cap": S_ID, "as_of": D,
            "status": "UNAVAILABLE", "gaps": av["gaps"],
            "sources": av["sources"], "wait_for": av["wait_for"],
            "valuation_section": section,
            "data_contract": {
                "verdicts": ["valuation_percentile（行业内估值分位）",
                             "expectation_priced_in（市场交易的预期识别）",
                             "repricing_risk（重估触发条件与方向）"],
                "required_inputs": ["pe_ttm/pb 序列（≥120 交易日）",
                                    "行业分类映射", "一致预期 EPS"],
                "snapshot": "s_s6_<as_of>.json（快照契约名；源就绪后落 X3 同款 generated 目录）"},
            "confidence": "N/A"}


# ── 估值截面（2026-10-14 接线）：westock quote/sector valuation → 缓存 → evaluate 只读 ──
# 口径纪律：截面 = 单日快照（pe/pe_fwd/pe_lyr/pb/股息率/双市值；行业含历史百分位），
# **不是 ≥120 日序列**——序列契约原样保留、不得因截面在场宣称满足。
_STOCK_FIELDS = ("price", "pe_ratio", "pe_fwd", "pe_lyr", "pb_ratio",
                 "dividend_ratio_ttm", "total_market_cap", "circulating_market_cap")
# 真实列名（2026-10-14 实测；TTA 类本体 + Pct 历史百分位 + PrevM/Q/W/Y 多期对照）
_SECTOR_FIELDS = ("EndDate",
                  "PeTTM", "PeTTMPct", "PeTTMPrevM", "PeTTMPrevQ", "PeTTMPrevW", "PeTTMPrevY",
                  "PbLF", "PbLFPct", "PbLFPrevM", "PbLFPrevQ", "PbLFPrevW", "PbLFPrevY",
                  "PsTTM", "PsTTMPct", "PsTTMPrevM", "PsTTMPrevQ", "PsTTMPrevW", "PsTTMPrevY",
                  "PcfTTM", "PcfTTMPct", "PcfTTMPrevM", "PcfTTMPrevQ", "PcfTTMPrevW", "PcfTTMPrevY",
                  "DivTTM", "DivTTMPct", "DivTTMPrevM", "DivTTMPrevQ", "DivTTMPrevW", "DivTTMPrevY")


def _parse_quote_md(text):
    """westock quote markdown 表 → [{code, name, ...}]（单行 CSV 解析）。"""
    rows = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln.startswith("|") or ln.startswith("| ---") or ln.startswith("| code"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) < 10:
            continue
        rows.append(cells)
    return rows


def fetch_valuation_section(codes, *, sectors=None, as_of=None, base=None):
    """westock quote（个股）+ sector valuation（行业）→ 缓存件（X3 同款）。

    evaluate 只读缓存零网络；本函数是唯一网络触点（人触发 CLI）。"""
    import datetime as _dt
    import subprocess as _sp
    from s4 import _chk_date
    D = _chk_date(as_of) if as_of else _dt.date.today().isoformat()
    cl = CC.to_cli_csv([c.strip() for c in codes if c.strip()])
    if not cl:
        raise S6Error("codes 为空（逗号分隔代码清单）")
    pr = _sp.run(["westock", "quote", cl], capture_output=True, text=True,
                 encoding="utf-8", timeout=180)
    if pr.returncode != 0:
        raise S6Error("westock quote 失败 rc=%d: %s" % (pr.returncode, pr.stderr[-200:]))
    # 列头定位（字段顺序以表头为准，不硬编码下标）
    hdr = None
    for ln in pr.stdout.splitlines():
        if ln.strip().startswith("| code"):
            hdr = [c.strip() for c in ln.strip().strip("|").split("|")]
            break
    if hdr is None:
        raise S6Error("westock quote 输出无表头（schema 漂移？）")
    body = [r for r in _parse_quote_md(pr.stdout)]
    stock = {}
    for cells in body:
        d = dict(zip(hdr, cells))
        stock[d["code"]] = {
            "name": d.get("name"), "quote_date": d.get("time"),
            "price": d.get("price"),
            **{k: d.get(k) for k in _STOCK_FIELDS}}
    sector = {}
    missing = [c for c in cl.split(",") if c not in stock]
    if sectors:
        sl = ",".join(s.strip() for s in sectors if s.strip())
        pr2 = _sp.run(["westock", "sector", "valuation", sl], capture_output=True,
                      text=True, encoding="utf-8", timeout=180)
        if pr2.returncode == 0:
            hdr2 = None
            for ln in pr2.stdout.splitlines():
                if ln.strip().startswith("| code"):
                    hdr2 = [c.strip() for c in ln.strip().strip("|").split("|")]
                    break
            for cells in _parse_quote_md(pr2.stdout):
                d = dict(zip(hdr2 or [], cells))
                sector[d["code"]] = {"name": d.get("name"), **{k: d.get(k)
                                     for k in _SECTOR_FIELDS if k in d}}
        else:
            missing.append("sector_valuation rc=%d" % pr2.returncode)
    gen = os.path.join(base or ROOT, "data", "generated")
    os.makedirs(gen, exist_ok=True)
    cache = {"schema": 1, "as_of": D, "fetched_at": _dt.datetime.now().isoformat(timespec="seconds"),
             "source": "westock quote CLI（个股截面）+ westock sector valuation（行业截面，"
                       "含历史百分位；接口不披露更新时间）",
             "semantics": "当前截面（非序列）——≥120 日估值序列契约仍未满足",
             "stock_section": stock, "sector_section": sector,
             "n_codes": len(stock) + len(sector), "missing": missing}
    p = os.path.join(gen, "s_s6_section_%s.json" % D)
    _WG.check_write(p)
    fd, tmp = tempfile.mkstemp(dir=gen, prefix="s_s6_section_", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return {"path": p, "n_codes": cache["n_codes"], "missing": missing,
            "fetched_at": cache["fetched_at"]}


def _load_section(as_of, base=None):
    # 两步拼装（同 evaluate 纪律：完整路径字面不进源码词汇）
    gen = os.path.join(base or ROOT, "data", "generated")
    p = os.path.join(gen, "s_s6_section_%s.json" % as_of)
    if not os.path.isfile(p):
        return None
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)


def _selftest():
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== s6 --selftest ==")

    def _prod_s6_fp():
        """生产 generated 目录 s_s6_* 文件指纹（文件名+大小）。
        2026-10-19 修：B1 原断言「目录无 s_s6_* 文件」写于缺源期零写点纪律，
        但 10-14 估值截面缓存落地后 landing.py 已登记
        `fetch_valuation_section → data/generated/s_s6_section_<as_of>.json`
        为合法写点（E6/E7/E11 消费）⇒ 断言改为「本次自检零写」指纹比对，
        既守住缺源期 evaluate 不落盘，又不与已登记写点矛盾。"""
        d = os.path.join(ROOT, "data", "generated")
        return sorted((f, os.path.getsize(os.path.join(d, f)))
                      for f in os.listdir(d) if f.startswith("s_s6_"))

    _s6_before = _prod_s6_fp()
    av = availability("2026-09-29")
    T("A1 availability 如实 ready=False 且列明缺口", av["ready"] is False
      and len(av["gaps"]) >= 1 and av["sources"])
    r = evaluate("2026-09-29")
    T("A2 evaluate 返回 UNAVAILABLE 判定包（不伪装估值）",
      r["status"] == "UNAVAILABLE" and r["data_contract"]["verdicts"]
      and "N/A" == r["confidence"])
    T("A3 不落快照（缺源期零写点）",
      not os.path.exists(os.path.join(ROOT, "data", "generated", "s_s6_2026-09-29.json")))
    try:
        evaluate("2026-9-1")
        T("A4 非法日期拒绝", False)
    except (S6Error, __import__("s4").S4Error):
        T("A4 非法日期拒绝", True)
    T("B1 真库零写（本次自检未改生产 generated 的 s_s6_* 文件）",
      _prod_s6_fp() == _s6_before)
    # ── C 组：估值截面（2026-10-14）——解析器 + 沙盒缓存双态 + 序列契约不伪装 ──
    import tempfile as _tf
    import shutil as _sh
    sb = _tf.mkdtemp(prefix="s6_sec_")
    try:
        md = ("| code | name | time | price | pe_ratio | pb_ratio | total_market_cap |\n"
              "| --- | --- | --- | --- | --- | --- | --- |\n"
              "| sh600519 | 贵州茅台 | 2026-10-14 | 1283.61 | 19.7 | 6.39 | 1604600000000 |\n")
        hdr = None
        for ln in md.splitlines():
            if ln.strip().startswith("| code"):
                hdr = [c.strip() for c in ln.strip().strip("|").split("|")]
        cells = [c.strip() for c in md.splitlines()[-1].strip().strip("|").split("|")]
        d1 = dict(zip(hdr, cells))
        T("C1 quote 表头定位解析（字段名对齐，不硬编码下标）",
          d1["code"] == "sh600519" and d1["pe_ratio"] == "19.7")
        gen = os.path.join(sb, "data", "generated")
        os.makedirs(gen, exist_ok=True)
        r0 = evaluate("2026-09-29", base=sb)
        T("C2 缓存缺席 → 截面 ABSENT（unlock 指向 fetch-section）",
          r0["valuation_section"]["status"] == "ABSENT"
          and "fetch-section" in r0["valuation_section"]["reason"])
        cache = {"schema": 1, "as_of": "2026-09-29", "fetched_at": "T",
                 "source": "westock quote CLI", "semantics": "当前截面（非序列）",
                 "stock_section": {"sh600519": d1}, "sector_section": {},
                 "n_codes": 1, "missing": []}
        with io.open(os.path.join(gen, "s_s6_section_2026-09-29.json"), "w",
                     encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        r1 = evaluate("2026-09-29", base=sb)
        T("C3 缓存在场 → PRESENT_SEGMENT（含来源/时间/缺失）",
          r1["valuation_section"]["status"] == "PRESENT_SEGMENT"
          and r1["valuation_section"]["source"] == "westock quote CLI")
        T("C4 截面不冒充序列：status 仍 UNAVAILABLE + 120 日契约原样",
          r1["status"] == "UNAVAILABLE"
          and "≥120 交易日" in r1["data_contract"]["required_inputs"][0])
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    print("== s6 --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    if "--availability" in argv:
        d = argv[argv.index("--availability") + 1] if len(argv) > 2 else "9999-12-31"
        print(json.dumps(availability(d), ensure_ascii=False, indent=1))
        sys.exit(0)
    if "--fetch-section" in argv:
        codes = argv[argv.index("--fetch-section") + 1].split(",")
        sectors = (argv[argv.index("--sectors") + 1].split(",")
                   if "--sectors" in argv else None)
        as_of = (argv[argv.index("--as-of") + 1] if "--as-of" in argv else None)
        base = (argv[argv.index("--base") + 1] if "--base" in argv else None)
        print(json.dumps(fetch_valuation_section(codes, sectors=sectors,
                                                 as_of=as_of, base=base),
                         ensure_ascii=False, indent=1))
        sys.exit(0)
    print("用法: s6.py --availability | --fetch-section <codes> [--sectors pt...] "
          "[--as-of D] [--base 沙盒] | --selftest")
    sys.exit(2)
