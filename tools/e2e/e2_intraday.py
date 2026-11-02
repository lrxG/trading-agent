# -*- coding: utf-8 -*-
"""E2 盘中扫描（2026-10-13 · 第一阶段补齐：契约 + 降级）。

职责（registry）：BUY/WAIT/NO 建议 + 触发线 → 挂载 C4。触发：盘中提问 / T2 异动。

**缺源核准（2026-10-12 实测）**：
  · 无盘中腿（实时采集未建——无 09:30–15:00 分时/盘口/异动推送）
  · S10 竞价结构缺核心源（backtest_auction 实为前日连板状态表）
  ⇒ 盘中 BUY/WAIT/NO 建议**当前不可机械产出**。

本件（与 s6/s8/s10 同款降级范式）：
  1. `availability()` —— 消费者（I7 盘中入口）统一入口：逐项列缺 + 解锁条件；
  2. `evaluate(as_of)` —— UNAVAILABLE 判定包 + data_contract（盘中腿接入后按同一
     契约产出：异动分类 / Thesis 触发线核对 / BUY·WAIT·NO 建议，且必须走 C4 三问，
     **禁绕过三问准入直接出建议**）；
  3. `emit(as_of)` —— 如实降级报告（e2_<as_of>.md，X1）：说明「盘中提问当前能答
     什么（收盘后事实）/不能答什么（实时异动）」——给交互入口一个诚实的落点。
"""
import io
import json
import os
import sys
import datetime as _dt

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
_DM = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "decision"))
sys.path.insert(0, _FS)
sys.path.insert(0, _DM)
from paths import ROOT  # noqa: E402
import premarket as E1W  # noqa: E402  复用已登记写点

S_ID = "e2"

# ── 旧链方法论（2026-10-19 导入 · 旧封版技能 intraday/盘感卡 · 只读）──────────
# 与 s1._KB_REL 同款口径：只到目录层（landing 路径重建只接字面量开头的 join，
# 整条文件路径会作为未登记 cap 产物进词汇表），文件名在使用点拼。
_KB_REL = os.path.join("data", "knowledge", "e2_intraday")
_KB_FNAME = "card_base.json"


def load_knowledge_base(base=None):
    """→ 旧链盘感卡知识库 dict；缺席/坏 JSON ⇒ None（按无方法论降级，禁抛）。"""
    p = os.path.join(base or ROOT, _KB_REL, _KB_FNAME)
    if not os.path.isfile(p):
        return None
    try:
        return json.load(io.open(p, encoding="utf-8-sig")) or None
    except Exception:
        return None

SOURCE_CHECKS = (
    ("intraday_feed", "盘中分钟腿（分时承接序列/首封时间/封单额 —— 实时截面已由 intraday_scan.py 提供，缺的是分钟级序列与封单源）",
     ("data/raw/intraday/", "intraday|tick|min")),
    ("auction_structure", "竞价结构（S10 前置）", ("data/generated/", "s_s10_")),
)


class E2Error(Exception):
    """REASON_CODE 前缀错误。"""


def _probe(base=None):
    b = base or ROOT
    out = []
    for key, label, (rel, pat) in SOURCE_CHECKS:
        d = os.path.join(b, rel)
        found = [fn for fn in (os.listdir(d) if os.path.isdir(d) else [])
                 if any(t in fn.lower() for t in pat.split("|"))]
        ready = bool(found)
        if key == "auction_structure":
            ready = False   # s_s10_ 快照在缺源期不产出（s10 UNAVAILABLE）
        out.append({"key": key, "label": label, "found": found[:5], "ready": ready})
    return out


def scan_leg(as_of, *, base=None):
    """盘中实时扫描腿的就绪度（**只读诊断，零写**）。

    来源：`tools/e2e/intraday_scan.py` 落的 `data/raw/intraday/<date>/scan_<HHMM>.json`。
    分层如实回答三件事：
      ① 数据腿在不在（当日有没有快照、几次、最新数据时间）；
      ② 八层盘感卡**哪几层能机械填**（= 数据够）、哪几层填不了（= 缺什么）；
      ③ 因此 `ready` 为什么仍是 False —— 不是缺源，是 E2-HR-1/3/4 未满足。
    """
    root = base or ROOT
    d = os.path.join(root, "data", "raw", "intraday", as_of)
    out = {"producer": "tools/e2e/intraday_scan.py",
           "dir": "data/raw/intraday/%s/" % as_of,
           "available": False, "n_snapshots": 0, "snapshots": [],
           "latest": None, "card_layers": None,
           "why_not_ready": "E2-HR-1/3/4 未满足（八层卡未填满 + ⑧层注码无机械判据 + "
                             "持仓侧无票可扫）；**不是缺数据源**"}
    if not os.path.isdir(d):
        out["reason"] = "NO_SNAPSHOT_TODAY（当日未跑 intraday_scan）"
        return out
    names = sorted(fn for fn in os.listdir(d)
                   if fn.startswith("scan_") and fn.endswith(".json"))
    out["n_snapshots"] = len(names)
    out["snapshots"] = names[-6:]
    if not names:
        out["reason"] = "NO_SNAPSHOT_TODAY（当日未跑 intraday_scan）"
        return out
    out["available"] = True
    try:
        p = json.load(io.open(os.path.join(d, names[-1]), encoding="utf-8-sig"))
    except Exception as e:                                    # noqa: BLE001
        out["reason"] = "SNAPSHOT_UNREADABLE: %s" % e
        return out
    b = p.get("buckets") or {}
    out["latest"] = {
        "file": names[-1], "data_date": p.get("data_date"),
        "hhmm": p.get("hhmm"), "env_grade": (p.get("env") or {}).get("grade"),
        "env_score": (p.get("env") or {}).get("score"),
        "sealed": b.get("sealed_n"), "broken": b.get("broken_n"),
        "near": b.get("near_n"), "limit_down": b.get("limit_down_n"),
        "max_board": b.get("max_board"),
        "rows": (p.get("meta") or {}).get("got_rows"),
    }
    # 八层卡可填性（Layer 1 市场环境 / 2 板块 / 5 盘中行为 由扫描件直给）
    out["card_layers"] = {
        "1_market_env": "READY（环境 8 维打分 + 涨停/炸板/跌停/涨跌比）",
        "2_sector_env": "READY（sector ranking 124 行业 + 主力净流入）",
        "3_stock_type": "GAP（需 S2 题材生命周期 + 历史位置）",
        "4_trend_state": "PARTIAL（chg_5d/10d/20d/60d + 52 周高低可给区间，"
                         "十态判定需均线序列 ⇒ 未做）",
        "5_intraday_behavior": "PARTIAL（价/量比/换手/委比/均价可给；"
                               "分时承接与首封时间/封单额缺源）",
        "6_relative_strength": "PARTIAL（vs 市场有；vs 板块需成分股映射 ⇒ 未做）",
        "7_state_change": "GAP（需与前一次快照逐票 diff ⇒ 下一步）",
        "8_action_stake": "GAP（注码 A-D 无机械判据 ⇒ E2-HR-3 直接卡死）",
    }
    return out


def availability(as_of, *, base=None):
    src = _probe(base)
    gaps = [x["label"] for x in src if not x["ready"]]
    kb = load_knowledge_base(base=base)
    return {"s_id": S_ID, "as_of": as_of, "ready": not gaps, "sources": src,
            "gaps": gaps,
            # ★ 2026-10-26 增量诊断：`intraday_scan.py` 已把「实时行情腿」建成
            #   （全市场 quote 4 批 16.6s，4940/4940 覆盖）。但**源就位 ≠ 卡填满**：
            #   E2-HR-1/3/4 仍不满足（八层卡只到 Layer1/2/5 机械可填，⑧层注码无从机械判定，
            #   持仓侧 capital=0 无票可扫）⇒ ready 仍为 False 是**诚实**的，
            #   但 gaps 的措辞必须从「缺源」改成「缺卡完成度」，否则 status 说了假话。
            "intraday_scan_leg": scan_leg(as_of, base=base),
            "knowledge": {
                "loaded": kb is not None,
                "version": (kb or {}).get("version"),
                "hard_rules": [r["id"] for r in (kb or {}).get("hard_rules", [])],
                "taxonomies": sorted((kb or {}).get("closed_taxonomies", {}).keys()),
                "note": "旧链封版方法论（八层盘感卡）——识别框架非统计结论；"
                        "状态判定证据须量化（E2-HR-2）",
            },
            "wait_for": ["异动判别器（在行情腿之上做放量/跳水/拉升/封撬板分类）",
                         "八层盘感卡机械填充（⑧层注码判据 = E2-HR-3 的卡点）",
                         "S10 竞价结构源就绪"],
            "unlocked_outputs": [] if gaps else
            ["intraday_anomaly（异动分类）", "thesis_trigger（Thesis 触发线核对）",
             "buy_wait_no（经 C4 三问的建议）"]}


def evaluate(as_of, *, base=None):
    from s4 import _chk_date
    D = _chk_date(as_of)
    av = availability(D, base=base)
    kb = load_knowledge_base(base=base)
    if av["ready"]:
        # ★ 旧链硬规则随分支一起报：源就绪 ≠ 可以直接出建议。
        raise E2Error(
            "SOURCE_READY: 盘中腿已接入——按 data_contract 产出前必须先满足旧链硬规则"
            "（card_base.json）：\n"
            "  E2-HR-1 没有盘感卡禁止输出减仓/止损建议（先填八层卡）；\n"
            "  E2-HR-2 状态判定证据必须量化，禁纯形容词（缺证据=UNKNOWN）；\n"
            "  E2-HR-3 每张卡⑧层必须带注码，未标等级=D（不下注）；\n"
            "  E2-HR-4 扫描范围 = 持仓票 + 候选票。\n"
            "且建议必须经 C4 三问（禁绕过准入）")
    return {"schema": 1, "s_id": S_ID, "cap": S_ID, "as_of": D,
            "status": "UNAVAILABLE", "gaps": av["gaps"], "sources": av["sources"],
            "wait_for": av["wait_for"], "knowledge": av["knowledge"],
            "intraday_scan_leg": av.get("intraday_scan_leg"),
            "knowledge_detail": {
                "hard_rules": (kb or {}).get("hard_rules", []),
                "card_layers": (kb or {}).get("card_layers", []),
                "closed_taxonomies": (kb or {}).get("closed_taxonomies", {}),
                "boundary": (kb or {}).get("boundary"),
            } if kb else None,
            "data_contract": {
                "verdicts": ["intraday_anomaly（盘中异动分类：放量/跳水/拉升/封撬板）",
                             "thesis_trigger（在仓 Thesis 触发线命中核对）",
                             "buy_wait_no（建议——必须走 C4 三问，本件不直接出）"],
                "required_inputs": ["盘中分时/盘口序列", "异动判别阈值（params）",
                                    "在仓 Thesis 触发线清单",
                                    "八层盘感卡（旧链 card_base：卡不齐不出操作建议）"],
                "report": "data/generated/reports/e2_<as_of>.md"},
            "confidence": "N/A"}


# ── 盘中分钟腿（2026-10-14 接线）：westock minute → 近 N 日盘中回放摘要 ──────────
# 口径纪律：
#   · minute = **单码单调**（实测逗号批量被拒 code=-1）⇒ 逐码循环，调用方控量；
#   · 窗口 = 近 N 个自然日（--days，实测今天起算）⇒ **历史 as_of（如 09-29）
#     不在窗内 = STALE（如实降级，不用历史日冒充盘中能力）**；
#   · 分钟腿 ≠ 实时推送采集器——E2 的「实时盘中腿」仍缺源（availability 不变），
#     本段是「当日/近 N 日盘中回放」能力，报告中与实时腿分列。
def fetch_minute(code, *, days=1):
    """westock minute 单码 → 分时摘要（零文件写点；调用方负责落盘）。"""
    import subprocess as _sp
    import datetime as _dt
    pr = _sp.run(["westock", "minute", code, "--days", str(days)],
                 capture_output=True, text=True, encoding="utf-8", timeout=120)
    if pr.returncode != 0:
        return {"code": code, "status": "FAIL",
                "error": pr.stderr[-160:]}
    hdr, rows = None, []
    for ln in pr.stdout.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if set("".join(cells)) <= set("- "):
            continue
        if hdr is None:
            hdr = cells
            continue
        rows.append(dict(zip(hdr, cells)))
    if not rows:
        return {"code": code, "status": "NO_DATA", "n": 0}
    # 注：--days=1 输出**无 date 列**（当日）；多日才带 date。date 缺席 ⇒ 单日语义。
    dates = sorted({r.get("date") for r in rows if r.get("date")})
    tot_amt = sum(float(r["amount"]) for r in rows
                  if r.get("amount") not in (None, ""))
    tot_vol = sum(float(r["volume"]) for r in rows
                  if r.get("volume") not in (None, ""))
    vwap = (tot_amt / (tot_vol * 100.0)) if tot_vol > 0 else None
    return {"code": code, "status": "PRESENT", "n_bars": len(rows),
            "dates": dates, "n_dates": len(dates),
            "single_day": len(dates) == 0,   # 无 date 列 = 当日单日（--days=1 形态）
            "sum_amount": round(tot_amt, 2), "sum_volume_shares": round(tot_vol * 100, 2),
            "vwap": round(vwap, 3) if vwap else None,
            "source": "westock minute CLI（--days=%d）" % days}


def emit(as_of, *, base=None):
    from s4 import _chk_date
    D = _chk_date(as_of)
    r = evaluate(D, base=base)
    leg_ok = bool((r.get("intraday_scan_leg") or {}).get("available"))
    L = ["# E2 盘中扫描 · %s —— UNAVAILABLE（%s）"
         % (D, "八层卡未填满 · 注码无机械判据" if leg_ok else "数据腿未跑 · 八层卡未填满"), ""]
    L.append("## 当前盘中提问能答什么")
    L.append("- 收盘后事实（快照已落地的 O/H/L/C/量额）：见 C2/E7 装配")
    L.append("- 持仓硬风控：C5 退出闸（同样缺实时价——见 E5 降级说明）")
    L.append("")
    L.append("## 当前不能答什么（如实）")
    for g in r["gaps"]:
        L.append("- %s" % g)
    L.append("")
    # ★ 盘中行情腿实况（2026-10-26）：源已建，但要分清「有数据」与「能给建议」
    leg = r.get("intraday_scan_leg") or {}
    L.append("## 盘中行情腿实况（`tools/e2e/intraday_scan.py`）")
    L.append("")
    if leg.get("available"):
        lt = leg.get("latest") or {}
        L.append("**数据腿：已建 · 当日快照 %d 份**（最新 `%s` · 数据时间 `%s %s`）"
                 % (leg["n_snapshots"], lt.get("file"), lt.get("data_date"), lt.get("hhmm")))
        L.append("")
        L.append("| 封板 | 炸板 | 半路 | 跌停 | 最高连板 | 环境档 |")
        L.append("|---|---|---|---|---|---|")
        L.append("| %s | %s | %s | %s | %s | %s（%s） |" % (
            lt.get("sealed"), lt.get("broken"), lt.get("near"), lt.get("limit_down"),
            lt.get("max_board"), lt.get("env_grade"), lt.get("env_score")))
        L.append("")
        L.append("通道实测：全市场 `westock quote` 4 批 / 16.6 秒 / 4940 码覆盖 100%，"
                 "38 字段（含 `price_ceiling`·`high`·`volume_ratio`·`wb_ratio`）。")
        L.append("**旧版「炸板池不存在」的前提已被推翻**——`high ≥ ceiling 且 price < ceiling` "
                 "即炸板，同一截面可判，无需等 14:57 定格。")
        L.append("")
        cc = leg.get("card_layers") or {}
        if cc:
            L.append("**八层盘感卡机械可填性**")
            L.append("")
            L.append("| 层 | 状态 |")
            L.append("|---|---|")
            for k in ("1_market_env", "2_sector_env", "3_stock_type", "4_trend_state",
                      "5_intraday_behavior", "6_relative_strength", "7_state_change",
                      "8_action_stake"):
                if k in cc:
                    L.append("| %s | %s |" % (k, cc[k]))
            L.append("")
    else:
        L.append("- **当日未跑**：`%s`" % (leg.get("reason") or "NO_SNAPSHOT"))
        L.append("- 跑一次：`python tools/e2e/intraday_scan.py --execute`")
        L.append("")
    L.append("> ⚠️ **源就位 ≠ 可以出建议。** `%s`" % (leg.get("why_not_ready") or "硬规则未满足"))
    L.append("")
    L.append("## 解锁条件与契约")
    for w in r["wait_for"]:
        L.append("- %s" % w)
    for v in r["data_contract"]["verdicts"]:
        L.append("- 产出契约：%s" % v)
    L.append("")
    L.append("> 边界：盘中建议必须走 C4 三问准入；本件零 BUY/WAIT/NO。")
    p = os.path.join(base or ROOT, "data", "generated", "reports", "e2_%s.md" % D)
    if os.path.isfile(p) and io.open(p, encoding="utf-8").read() == "\n".join(L):
        return {"created": False, "reason": "EXISTS", "path": p}
    E1W._atomic_write(p, "\n".join(L))
    return {"created": True, "path": p,
            "generated_at": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}


def _selftest():
    import tempfile as _tf
    import shutil as _sh
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== e2 --selftest ==")
    av = availability("2026-09-29")
    T("A1 availability ready=False（盘中腿与竞价结构均缺）",
      av["ready"] is False and len(av["gaps"]) == 2)
    r = evaluate("2026-09-29")
    T("A2 evaluate UNAVAILABLE + 三段产出契约",
      r["status"] == "UNAVAILABLE" and len(r["data_contract"]["verdicts"]) == 3)
    sb = _tf.mkdtemp(prefix="e2_sb_")
    try:
        r1 = emit("2026-09-29", base=sb)
        t1 = io.open(r1["path"], encoding="utf-8").read()
        T("B1 降级报告落 X1 且「能答/不能答」分槽",
          os.path.basename(r1["path"]) == "e2_2026-09-29.md"
          and "能答什么" in t1 and "不能答什么" in t1)
        T("B2 幂等", emit("2026-09-29", base=sb)["reason"] == "EXISTS")
        try:
            evaluate("2026-9-1")
            T("B3 非法日期拒绝", False)
        except __import__("s4").S4Error:
            T("B3 非法日期拒绝", True)
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    # ── C 组：旧链盘感卡方法论接线（2026-10-19 导入）────────────────────
    kb = load_knowledge_base()
    T("C1 知识库载入且四条硬规则齐（无卡禁建议/量化证据/注码缺省D/扫描闭集）",
      kb is not None and [r["id"] for r in kb.get("hard_rules", [])]
      == ["E2-HR-1", "E2-HR-2", "E2-HR-3", "E2-HR-4"])
    tax = kb.get("closed_taxonomies", {}) if kb else {}
    T("C2 封闭态值域齐（10 走势态 / 6 情绪态 / 6 涨停链态 / 注码 A-D）",
      len(tax.get("trend_state_10", [])) == 10
      and len(tax.get("emotion_cycle", [])) == 6
      and len(tax.get("limitup_chain", [])) == 6
      and tax.get("stake_grade") == ["A", "B", "C", "D"])
    T("C3 八层卡骨架在场（①~⑧ 全）",
      [l["layer"] for l in kb.get("card_layers", [])] == list(range(1, 9)))
    r3 = evaluate("2026-09-29")
    T("C4 降级判定包载方法论（硬规则+封闭态+边界）",
      r3["knowledge"]["loaded"] is True
      and len(r3["knowledge_detail"]["hard_rules"]) == 4
      and r3["knowledge_detail"]["closed_taxonomies"])
    # C5：源就绪分支报错带硬规则字样（stub 源就绪）
    _orig_probe = _probe

    def _stub_ready(base=None):
        return [{"key": "intraday_feed", "label": "x", "found": ["f"], "ready": True},
                {"key": "auction_structure", "label": "y", "found": ["f"], "ready": True}]

    try:
        globals()["_probe"] = _stub_ready
        try:
            evaluate("2026-09-29")
            T("C5 源就绪 ⇒ SOURCE_READY 拒绝", False, "竟放行")
        except E2Error as e:
            T("C5 源就绪分支报错带旧链硬规则（E2-HR-1/2/3 字样）",
              all(t in str(e) for t in ("E2-HR-1", "E2-HR-2", "E2-HR-3", "C4 三问")))
    finally:
        globals()["_probe"] = _orig_probe
    # C6：base 相对解析 + 缺席降级
    sb2 = _tf.mkdtemp(prefix="e2_kb_")
    try:
        os.makedirs(os.path.join(sb2, "data", "knowledge", "e2_intraday"),
                    exist_ok=True)
        _sh.copy(os.path.join(ROOT, _KB_REL, _KB_FNAME),
                 os.path.join(sb2, _KB_REL, _KB_FNAME))
        T("C6a 沙盒根方法论可载入", load_knowledge_base(base=sb2) is not None)
        os.remove(os.path.join(sb2, _KB_REL, _KB_FNAME))
        T("C6b 缺席 ⇒ None 降级不抛",
          load_knowledge_base(base=sb2) is None
          and availability("2026-09-29", base=sb2)["knowledge"]["loaded"] is False)
    finally:
        _sh.rmtree(sb2, ignore_errors=True)
    # ── G 组：盘中扫描腿（2026-10-26 建 intraday_scan.py 后的实况校准）──────
    sb3 = _tf.mkdtemp(prefix="e2_leg_")
    try:
        g0 = scan_leg("2026-10-26", base=sb3)
        T("G1 无当日快照 ⇒ available=False 且 reason 指明未跑",
          g0["available"] is False
          and g0["reason"].startswith("NO_SNAPSHOT_TODAY"))
        d3 = os.path.join(sb3, "data", "raw", "intraday", "2026-10-26")
        os.makedirs(d3, exist_ok=True)
        snap = {"data_date": "2026-10-26", "hhmm": "1041",
                "env": {"grade": "退潮", "score": -7},
                "buckets": {"sealed_n": 36, "broken_n": 21, "near_n": 70,
                            "limit_down_n": 21, "max_board": 4},
                "meta": {"got_rows": 4940}}
        io.open(os.path.join(d3, "scan_1041.json"), "w", encoding="utf-8").write(
            json.dumps(snap, ensure_ascii=False))
        g1 = scan_leg("2026-10-26", base=sb3)
        T("G2 有快照 ⇒ available=True 且四池/环境档解析正确",
          g1["available"] is True and g1["n_snapshots"] == 1
          and g1["latest"]["sealed"] == 36 and g1["latest"]["env_grade"] == "退潮"
          and g1["latest"]["max_board"] == 4)
        T("G3 八层卡可填性逐层给出（3/7/8 层必须显式 GAP，不得假装 READY）",
          set(g1["card_layers"]) == {
              "1_market_env", "2_sector_env", "3_stock_type", "4_trend_state",
              "5_intraday_behavior", "6_relative_strength", "7_state_change",
              "8_action_stake"}
          and g1["card_layers"]["8_action_stake"].startswith("GAP")
          and g1["card_layers"]["1_market_env"].startswith("READY"))
        av3 = availability("2026-10-26", base=sb3)
        T("G4 ★源就位不改变 ready：仍 False（E2-HR 未满足），且 why_not_ready 说清不是缺源",
          av3["ready"] is False
          and av3["intraday_scan_leg"]["available"] is True
          and "不是缺数据源" in av3["intraday_scan_leg"]["why_not_ready"])
        r4 = emit("2026-10-26", base=sb3)
        t4 = io.open(r4["path"], encoding="utf-8").read()
        T("G5 报告含「盘中行情腿实况」且显式写「源就位 ≠ 可以出建议」",
          "盘中行情腿实况" in t4 and "源就位 ≠ 可以出建议" in t4
          and "炸板池不存在" in t4)
        T("G6 报告仍零买卖结论（不因源就位就偷偷给建议）",
          not any(w in t4 for w in ("建议买入", "建议卖出", "清仓", "满仓"))
          and "零 BUY/WAIT/NO" in t4)
    finally:
        _sh.rmtree(sb3, ignore_errors=True)
    print("== e2 --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    if "--eval" in argv:
        import json as _j
        b = (argv[argv.index("--base") + 1] if "--base" in argv else None)
        if b and not os.path.isdir(b):
            print("--base 必须是已存在目录（防误写生产），实际: %s" % b)
            sys.exit(2)
        print(_j.dumps(emit(argv[argv.index("--eval") + 1], base=b),
                       ensure_ascii=False, indent=1))
        sys.exit(0)
    if "--minute" in argv:
        import json as _j
        codes = argv[argv.index("--minute") + 1].split(",")
        days = int(argv[argv.index("--days") + 1]) if "--days" in argv else 1
        out = [fetch_minute(c.strip(), days=days) for c in codes]
        print(_j.dumps(out, ensure_ascii=False, indent=1))
        sys.exit(0)
    print("用法: e2_intraday.py --eval <as_of> [--base 沙盒] | --minute <codes> [--days N] | --selftest")
    sys.exit(2)
