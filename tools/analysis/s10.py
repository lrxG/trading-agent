# -*- coding: utf-8 -*-
"""S10 竞价/一字板结构（2026-10-13 第一阶段补齐 · 2026-10-19 接旧链 G 研究知识库）。

职责（registry）：竞价强弱 / 当日·次日溢价 / 封板·炸板·撬板 → 消费者 C4 入场时点。

**缺源核准（实测登记，不得伪装）**：
  · 旧包 `backtest_auction` 实为**前日连板状态表**（无竞价量/委比/分时）
  · zt_pool `封板资金(fund)` 字段脏值率高（09-29 实测含 '93054' 类脏时间戳）
  · ⇒ 竞价强弱、封单结构、撬板序列当前**不可机械产出**。

本件提供（与 s6 同款降级范式）：
  1. `availability()` —— 消费者统一入口：逐项列出数据源就绪情况与缺口 + 知识库状态；
  2. `evaluate(as_of)` —— UNAVAILABLE 判定包 + data_contract + **旧链知识**（红线段/
     实测/边界——降级态也载知识，不是光秃秃的「没数据」）；
  3. 缺源期**不落快照**（消费者以 availability 为准；禁每日噪音快照）。

★ 2026-10-19 旧链接入（母题 G 研究 → data/knowledge/s10_auction/ 知识库）：
  · G-RL-1 红线（26 万样本）：竞价价格结构**单测**次日溢价 ≈ 硬币翻转
    （胜率 44~49%、期望≈0）⇒ 源接入后 verdicts **不得**是纯竞价价格结构的
    单变量预测；缺消息/事件层（S1）叠加时禁给方向性结论；
  · G-RL-2：竞价 = 事件/情绪/个股位置的**放大器**，非独立预测源；
  · G-RL-3：消息三档 S/W/N 口径（价值 = 首次发布当天；>7 天 = N；日历当天 = N）；
  · G-M-1 实测：恒强（跳空高走×排除高位×量比<2）→ H=10 中位 +2.7%（可作判据）；
  · G-H-1/G-H-2：n=1 / 未测假设——**禁当结论**（只可立 memory/hypotheses）。

★ 2026-10-20 GAP-ATTR 接入（SYS-GAP-ATTR 跳空归因层 · 母题 G §SYS-GAP-ATTR）：
  · G-RL-4 红线：异动(3.0x)/风险提示(1.8x)公告 = 『果』非『因』（涨疯→被要求发公告），
    **禁用**作高开/跳空的事件理由（反向因果陷阱）；
  · G-M-2 实测：真前瞻类=业绩预告/订单 1.5x（叠加竞价高开可接力）；回购/分红/质押/减持
    0.6~0.7x（撑不起跳空，见高开不构成信号）；
  · G-M-3 实测（**带 caveat**）：隔夜腿核心=昨缩量封死板→T 日开盘卖 +3.18%/71%
    （n=3,331）——**未过闸**（逐月未拆 / 卖出撮合未建模 / 装配冲突），禁当稳定判据；
  · gap_attr.structure：跳空主因在腿外（β 18% + 题材共振 82%），当日公告仅 12%。
"""
import os
import sys

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
sys.path.insert(0, _FS)
from paths import ROOT  # noqa: E402

_LEGACY_ROOT = (os.environ.get("OLD_SYSTEM_ROOT")
                or os.path.join(os.path.expanduser("~"), "炒股智能体_旧包_20261008"))  # 旧系统只读参考源；换机器时设 OLD_SYSTEM_ROOT 或放家目录

S_ID = "s10"
OLD_ROOT = _LEGACY_ROOT

# ── 旧链知识库（母题 G 研究结构化 · 2026-10-19 导入 · 只读）─────────────────
# base 相对解析（回测沙盒整树可替换；与 s1._KB_REL 同款口径：**只到目录层**，
# 文件名在使用点拼——landing 路径重建只接字面量开头的 join，整条文件路径会作为
# 未登记 cap 产物进词汇表（s10_auction ≠ 注册表 id），目录层则与 s1 等既有库一致）。
_KB_REL = os.path.join("data", "knowledge", "s10_auction")
_KB_FNAME = "auction_base.json"


def load_knowledge_base(base=None):
    """→ 旧链知识库 dict；缺席/坏 JSON ⇒ None（消费者按无知识降级，禁抛）。"""
    import json as _json
    import io as _io
    p = os.path.join(base or ROOT, _KB_REL, _KB_FNAME)
    if not os.path.isfile(p):
        return None
    try:
        return _json.load(_io.open(p, encoding="utf-8-sig")) or None
    except Exception:
        return None

SOURCE_CHECKS = (
    ("auction_feed", "集合竞价行情（量/委比/强弱分时）",
     ("data/raw/backtest_auction/", "auction|竞价")),
    ("seal_order_feed", "封单/撬板序列（可信封单额）",
     ("data/raw/bus/zt_pool/", "zt_pool")),
)


class S10Error(Exception):
    """REASON_CODE 前缀错误。"""


def _probe_sources(old_root=None, base=None, as_of=None):
    """→ [{key,label,found,ready,note}]。

    ★ 2026-10-22 补：**auction_feed 现在真的可用了**。
      `tools/e2e/fetch_auction.py` 从 `westock quote` 的 `pre_quote_*` 系列字段
      采到了本项目 `data/raw/bus/auction_<date>.csv`（14 列：竞价价/涨幅/量/额/
      换手/委比/涨停价/跌停价/开盘/收盘/全天涨幅）。
      此前 `_probe_sources` **只查旧包目录**（`data/raw/backtest_auction/`、
      `data/raw/bus/zt_pool/`），从不看本项目的新采文件 ⇒ 永远 ready=false。
      这也是本函数自己 `wait_for` 里早就写着的「westock quote pre_quote_* 结果字段可用」——
      **设计者知道有源，只是没人去接。**
    """
    root = old_root or OLD_ROOT
    out = []
    for key, label, (rel, pat) in SOURCE_CHECKS:
        d = os.path.join(root, rel)
        found = []
        if os.path.isdir(d):
            for fn in os.listdir(d):
                if any(t in fn.lower() for t in pat.split("|")):
                    found.append(fn)
        ready = bool(found)
        if key == "auction_feed" and found:
            # 旧包命名命中 ≠ 内容合格：backtest_auction 是前日连板状态表，
            # 无竞价量/委比字段 ⇒ 旧源仍判不可用（保持原来的怀疑）
            ready = False
        if key == "seal_order_feed" and found:
            ready = False   # zt_pool 封单额脏值率高 → 不作为可信封单源
        out.append({"key": key, "label": label, "found": found[:5],
                    "ready": ready})

    # ★ 本项目自采的竞价文件：内容级核验（列齐 + 有行）才判 ready
    if as_of:
        base_root = base or os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        p = os.path.join(base_root, "data", "raw", "bus",
                         "auction_%s.csv" % as_of)
        ok, why, n = _check_auction_file(p)
        for x in out:
            if x["key"] == "auction_feed":
                if ok:
                    x["ready"] = True
                    x["found"] = list(x["found"]) + [os.path.basename(p)]
                    x["note"] = "本项目自采（westock quote pre_quote_*）· %d 行" % n
                else:
                    x["note"] = why
    return out


# 竞价文件必须有的列（缺任一 ⇒ 内容不合格，不因"文件存在"就判可用）
_AUCTION_COLS = ("date", "code", "auction_price", "auction_pct",
                 "auction_volume", "wb_ratio", "price_ceiling")


def _check_auction_file(p):
    """→ (ok, why, n_rows)。内容级核验，不看文件是否存在。"""
    import csv as _csv
    import io as _io
    if not os.path.isfile(p):
        return False, "未采集（跑 tools/e2e/fetch_auction.py --date <D> --execute）", 0
    try:
        with _io.open(p, encoding="utf-8", newline="") as f:
            rd = _csv.DictReader(f)
            cols = set(rd.fieldnames or [])
            miss = [c for c in _AUCTION_COLS if c not in cols]
            if miss:
                return False, "列不全，缺 %s" % miss, 0
            n = sum(1 for _ in rd)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, str(e)[:60]), 0
    if n == 0:
        return False, "文件在但 0 行（空壳不算有数据）", 0
    return True, "", n


def availability(as_of, *, old_root=None, base=None):
    src = _probe_sources(old_root, base=base, as_of=as_of)
    gaps = [x["label"] for x in src if not x["ready"]]
    kb = load_knowledge_base(base=base)
    return {"s_id": S_ID, "as_of": as_of, "ready": not gaps,
            "sources": src, "gaps": gaps,
            "knowledge": {
                "loaded": kb is not None,
                "version": (kb or {}).get("version"),
                "as_of": (kb or {}).get("as_of"),
                "red_lines": [r["id"] for r in (kb or {}).get("red_lines", [])],
                "measured": [m["id"] for m in (kb or {}).get("measured", [])],
                "hypotheses": [h["id"] for h in (kb or {}).get("hypotheses", [])],
                "gap_attr": bool((kb or {}).get("gap_attr")),
                "note": "旧链知识（母题 G 研究）——红线为硬约束；measured 可作判据；"
                        "hypotheses 禁当结论；gap_attr=SYS-GAP-ATTR 跳空归因层（含 DRAFT 隔夜腿）",
            },
            "wait_for": ["~~集合竞价行情接入~~（2026-10-22 已接："
                         "`tools/e2e/fetch_auction.py` 采 westock quote pre_quote_* → "
                         "`data/raw/bus/auction_<D>.csv`。注意 09:25 逐笔撮合明细仍是"
                         "伪缺口，非必需）",
                         "可信封单额源（Level-1/2 或交易所披露口径）",
                         "消息/事件层标签工程化（G×F 联动前提，26 万样本无此标签）"],
            "unlocked_outputs": [] if gaps else
            ["auction_strength", "overnight_premium", "seal_structure", "pry_sequence"]}


def evaluate(as_of, *, old_root=None, params_path=None, base=None):
    from s4 import _chk_date
    D = _chk_date(as_of)
    av = availability(D, old_root=old_root, base=base)
    kb = load_knowledge_base(base=base)
    if av["ready"]:
        # ★ G-RL-1/G-RL-2 硬约束随分支一起报：源就绪 ≠ 可以产单变量预测。
        # 缺 S1 事件层叠加时，verdicts 不得给方向性结论（26 万样本已证伪）。
        raise S10Error(
            "SOURCE_READY: 竞价源已接入——按 data_contract 填充 verdicts 前必须先满足"
            "旧链红线（auction_base.json）：\n"
            "  G-RL-1 竞价价格结构单测次日溢价≈硬币翻转（26 万样本）⇒ verdicts 不得是"
            "纯竞价价格结构的单变量预测；\n"
            "  G-RL-2 竞价=放大器 ⇒ 缺 S1 事件层叠加时禁给方向性结论；\n"
            "  G-RL-3 消息三档口径（价值=首日；>7 天=N；日历当天=N）。\n"
            "（本分支待源接入后实现，禁以连板状态表冒充竞价结构）")
    return {"schema": 1, "s_id": S_ID, "cap": S_ID, "as_of": D,
            "status": "UNAVAILABLE", "gaps": av["gaps"], "sources": av["sources"],
            "wait_for": av["wait_for"], "knowledge": av["knowledge"],
            "knowledge_detail": {
                "red_lines": (kb or {}).get("red_lines", []),
                "measured": (kb or {}).get("measured", []),
                "hypotheses": (kb or {}).get("hypotheses", []),
                "gap_attr": (kb or {}).get("gap_attr"),
                "boundary": (kb or {}).get("boundary"),
            } if kb else None,
            "data_contract": {
                "verdicts": ["auction_strength（竞价强弱档）",
                             "overnight_premium（隔夜/次日溢价）",
                             "seal_structure（封单结构）",
                             "pry_sequence（撬板序列）"],
                "required_inputs": ["09:15–09:25 竞价量价序列（westock pre_quote_* 结果字段）",
                                    "可信封单额",
                                    "S1 事件层叠加（G-RL-2：无叠加禁方向性结论）",
                                    "连板梯队状态表（已有：backtest_auction 可作辅）"],
                "red_line_constraints": ["G-RL-1 单变量预测禁令",
                                         "G-RL-2 事件层叠加要求",
                                         "G-RL-3 消息三档口径"],
                "snapshot": "s_s10_<as_of>.json（快照契约名；源就绪后落 X3 同款 generated 目录）"},
            "confidence": "N/A"}


def _selftest():
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== s10 --selftest ==")
    av = availability("2026-09-29")
    T("A1 availability ready=False 且逐项列缺", av["ready"] is False and av["gaps"])
    T("A2 auction_feed 判不合格（目录仅状态/日历文件，无竞价序列——防冒充）",
      not next(x for x in av["sources"] if x["key"] == "auction_feed")["ready"])
    r = evaluate("2026-09-29")
    T("A3 evaluate 返回 UNAVAILABLE 判定包（含 data_contract）",
      r["status"] == "UNAVAILABLE" and r["data_contract"]["verdicts"])
    T("A4 缺源期零快照落盘",
      not [f for f in os.listdir(os.path.join(ROOT, "data", "generated"))
           if f.startswith("s_s10_")])
    try:
        evaluate("2026-9-1")
        T("A5 非法日期拒绝", False)
    except (__import__("s4").S4Error, S10Error):
        T("A5 非法日期拒绝", True)
    # ── B 组：旧链知识库接线（2026-10-19 · 母题 G 研究导入）──────────────
    kb = load_knowledge_base()
    T("B1 知识库载入且四条红线段齐（G-RL-1 单变量/G-RL-2 放大器/G-RL-3 消息口径/G-RL-4 反向因果陷阱）",
      kb is not None and [r["id"] for r in kb.get("red_lines", [])]
      == ["G-RL-1", "G-RL-2", "G-RL-3", "G-RL-4"])
    r2 = evaluate("2026-09-29")
    T("B2 降级判定包载旧链知识（红线+实测+边界，不是光秃秃的没数据）",
      r2["status"] == "UNAVAILABLE" and r2["knowledge"]["loaded"] is True
      and len(r2["knowledge_detail"]["red_lines"]) == 4
      and r2["knowledge_detail"]["boundary"])
    T("B3 实测与假设分层（measured 可作判据 / hypotheses 禁当结论）",
      any(m["id"] == "G-M-1" and m["status"] == "MEASURED"
          for m in r2["knowledge_detail"]["measured"])
      and all(h["status"] == "HYPOTHESIS"
              for h in r2["knowledge_detail"]["hypotheses"]))
    # B4：源就绪分支的报错必须带红线硬约束（stub 源就绪 → 捕获 S10Error 验字样）
    _orig_probe = _probe_sources

    def _stub_ready(old_root=None, base=None, as_of=None):
        return [{"key": "auction_feed", "label": "x", "found": ["f"], "ready": True},
                {"key": "seal_order_feed", "label": "y", "found": ["f"], "ready": True}]

    try:
        globals()["_probe_sources"] = _stub_ready
        try:
            evaluate("2026-09-29")
            T("B4 源就绪 ⇒ SOURCE_READY 拒绝（不静默填充 verdicts）", False, "竟放行")
        except S10Error as e:
            T("B4 源就绪分支报错带红线硬约束（G-RL-1/2/3 + 硬币翻转字样）",
              all(t in str(e) for t in ("G-RL-1", "G-RL-2", "G-RL-3", "硬币翻转")))
    finally:
        globals()["_probe_sources"] = _orig_probe
    # B5：base 相对解析——沙盒根下的库副本可载入、生产根不受影响
    import tempfile as _tf
    import shutil as _sh
    sb = _tf.mkdtemp(prefix="s10_kb_")
    try:
        os.makedirs(os.path.join(sb, "data", "knowledge", "s10_auction"),
                    exist_ok=True)
        _sh.copy(os.path.join(ROOT, _KB_REL, _KB_FNAME),
                 os.path.join(sb, _KB_REL, _KB_FNAME))
        kb_sb = load_knowledge_base(base=sb)
        T("B5a 沙盒根知识库可载入（base 相对解析生效）", kb_sb is not None)
        os.remove(os.path.join(sb, _KB_REL, _KB_FNAME))
        T("B5b 库缺席 ⇒ None 降级不抛（消费者按无知识走）",
          load_knowledge_base(base=sb) is None)
        av_abs = availability("2026-09-29", base=sb)
        T("B5c 库缺席时 availability.knowledge.loaded=False 且不崩",
          av_abs["knowledge"]["loaded"] is False and av_abs["ready"] is False)
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    # B6：provenance 在场（旧链结论引用须可溯源）
    T("B6 provenance 载明旧包来源与『旧链』状态",
      kb.get("provenance", {}).get("chain") == "旧链（旧系统回测验证产物）——引用须标 [旧链] + 样本量"
      and "G_竞价日内" in str(kb["provenance"].get("source_files")))
    # ── B7 组：SYS-GAP-ATTR 跳空归因层（2026-10-20 接入 · 母题 G §SYS-GAP-ATTR）──
    ga = kb.get("gap_attr")
    T("B7a 知识库载 GAP-ATTR 段且三子段齐（structure/announcement_lift/overnight_leg）",
      isinstance(ga, dict)
      and all(k in ga for k in ("structure", "announcement_lift", "overnight_leg")))
    T("B7b 降级判定包透出 gap_attr（消费者可读归因层，不是只进库不进包）",
      r2["knowledge_detail"].get("gap_attr") is not None
      and r2["knowledge_detail"]["gap_attr"]["id"] == "SYS-GAP-ATTR")
    T("B7c G-RL-4 反向因果陷阱红线在案（异动/风险提示禁用作高开理由）",
      any(r["id"] == "G-RL-4" and "异动" in r["rule"]
          and "禁" in r["rule"] and "因" in r["rule"]
          for r in kb["red_lines"]))
    T("B7d 公告提升度分层正确（真前瞻 1.5x / 反向因果 3.0x·1.8x / 无信号 0.6~0.7x）",
      ga["announcement_lift"]["forward_looking"].get("业绩预告") == "1.5x"
      and ga["announcement_lift"]["reverse_causal_forbidden"].get("异动公告") == "3.0x"
      and ga["announcement_lift"]["no_signal"].get("分红") == "0.6x")
    T("B7e 隔夜腿实测带 caveat（未过闸 ⇒ 不得当稳定判据）",
      any(m["id"] == "G-M-3" and m.get("caveat") and "未过闸" in m["caveat"]
          for m in kb["measured"]))
    T("B7f availability.knowledge 报 gap_attr=True",
      availability("2026-09-29")["knowledge"].get("gap_attr") is True)
    print("== s10 --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    if "--availability" in argv:
        import json as _j
        import glob as _glob
        import re as _re
        # ★ 2026-10-22：原来传死日期 "9999-12-31" 当占位，竞价文件被拼成
        #   `auction_9999-12-31.csv` ⇒ 永远「未采集」。可用性检查应该看
        #   **最近一个真实的竞价文件**，不是看一个不存在的日期。
        _base = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        _hits = sorted(_glob.glob(os.path.join(
            _base, "data", "raw", "bus", "auction_*.csv")))
        _as_of = "9999-12-31"
        for _h in reversed(_hits):
            _m = _re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(_h))
            if _m:
                _as_of = _m.group(1)
                break
        _av = availability(_as_of)
        _av["probe_as_of"] = _as_of
        _av["note"] = ("可用性检查用**最近一个真实竞价文件**的日期（%s），"
                       "不是拿死日期占位" % _as_of)
        print(_j.dumps(_av, ensure_ascii=False, indent=1))
        sys.exit(0)
    print("用法: s10.py --availability | --selftest")
    sys.exit(2)
