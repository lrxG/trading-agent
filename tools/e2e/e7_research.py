# -*- coding: utf-8 -*-
"""E7 个股研究装配（2026-10-13 · 第一阶段补齐）。

职责（registry）：驱动 / 位置 / 赔率 / 风险 / 证伪线 → 挂载 C3。触发：点名 / QUALIFIED 深挖。
**边界：零 BUY/WAIT/NO、零仓位动作**（研究装配，判断本体 = C3/C4 LLM）。

输入（只读，缺→ABSENT 如实）：
  · S1 事件评估    data/generated/s_s1_<as_of>.json（该票事件 + strength）
  · S4 拥挤        data/generated/s_s4_<as_of>.json（该票 crowding；froth 缺源锁死）
  · S5 资金        data/generated/s_s5_<as_of>.json（该票 behavior/chip）
  · S2 题材        tdx 成分映射 × s_s2_<as_of>.json（该票所属题材阶段）
  · C3 驱动链      data/research/drive/chains.jsonl（该票最近一条 review 结论）
  · 快照           data/snapshot/<as_of>（价格事实：close/high/low——只引用不解读）

降级（缺源核准 2026-10-12）：
  · 赔率定价 = S6 UNAVAILABLE（估值源缺失）；基本面/质地 = E8 UNAVAILABLE（无财务源）；
  · 财报节点 = S7（预期差降级）；政策传导 = S8（传导链降级）——均在降级清单点名。

输出 `data/generated/reports/e7_<as_of>.md`（X1；写点复用 premarket._atomic_write）。
幂等：同输入同字节 ⇒ 跳过写。
"""
import io
import json
import os
import sys
import datetime as _dt

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
_DM = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "decision"))
_AN = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "analysis"))
for p in (_FS, _DM, _AN):
    sys.path.insert(0, p)
from paths import ROOT  # noqa: E402
import premarket as E1W  # noqa: E402  复用已登记写点（零新增写点）
import drive as DRV      # noqa: E402  S2 题材映射复用（_code_theme_ids）

_LEGACY_ROOT = (os.environ.get("OLD_SYSTEM_ROOT")
                or os.path.join(os.path.expanduser("~"), "炒股智能体_旧包_20261008"))  # 旧系统只读参考源；换机器时设 OLD_SYSTEM_ROOT 或放家目录

S_ID = "e7"
OLD_ROOT = _LEGACY_ROOT

# ── 旧链研究框架（2026-10-19 导入 · 旧封版技能 stock-research · 只读）─────────
# 目录层 join（landing 路径重建只接字面量开头的 join；文件名在使用点拼）。
_KB_REL = os.path.join("data", "knowledge", "e7_research")
_KB_FNAME = "research_base.json"


def load_knowledge_base(base=None):
    """→ 旧链研究框架 dict；缺席/坏 JSON ⇒ None（按无框架降级，禁抛）。"""
    p = os.path.join(base or ROOT, _KB_REL, _KB_FNAME)
    if not os.path.isfile(p):
        return None
    try:
        with io.open(p, encoding="utf-8-sig") as f:
            return json.load(f) or None
    except Exception:
        return None


def _load_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def collect(as_of, code, *, base=None):
    b = base or ROOT
    gen = os.path.join(b, "data", "generated")
    d = {"as_of": as_of, "code": code, "sections": {}, "degradations": []}

    s1 = _load_json(os.path.join(gen, "s_s1_%s.json" % as_of))
    evs = []
    if isinstance(s1, dict):
        for e in s1.get("events") or []:
            if e.get("code") == code:
                evs.append({"event_id": e.get("event_id"), "kind": e.get("kind"),
                            "strength": e.get("strength"), "net_effect": e.get("net_effect")})
    d["sections"]["s1_events"] = {"status": ("PRESENT" if evs else
                                             ("PRESENT_EMPTY" if s1 else "ABSENT")),
                                  "events": evs[:10]}
    if s1 is None:
        d["degradations"].append("S1 事件快照缺席——事件段缺失")

    s4 = _load_json(os.path.join(gen, "s_s4_%s.json" % as_of))
    c4 = (s4.get("codes") or {}).get(code) if isinstance(s4, dict) else None
    d["sections"]["s4_crowding"] = (
        {"status": "PRESENT", "crowding": c4.get("crowding"),
         "evidence": c4.get("evidence")} if isinstance(c4, dict)
        else {"status": "ABSENT"})
    d["sections"]["froth"] = {"status": "UNAVAILABLE",
                              "reason": "S4 froth 缺源锁死（估值源缺失）——泡沫度不产出"}
    if s4 is None:
        d["degradations"].append("S4 拥挤快照缺席")
    # S6/S7 单票截面（2026-10-14 接线）：缓存件在 → 个股截面/一致预期当前截面；
    # 序列契约（≥120 日）/历史预期序列仍 UNAVAILABLE（不伪装）
    s6c = _load_json(os.path.join(gen, "s_s6_section_%s.json" % as_of))
    s6row = (s6c.get("stock_section") or {}).get(code) if isinstance(s6c, dict) else None
    d["sections"]["s6_valuation"] = (
        {"status": "PRESENT_SEGMENT",
         "quote_date": s6row.get("quote_date"),
         "pe_ttm": s6row.get("pe_ratio"), "pe_fwd": s6row.get("pe_fwd"),
         "pe_lyr": s6row.get("pe_lyr"), "pb": s6row.get("pb_ratio"),
         "dividend_ttm_pct": s6row.get("dividend_ratio_ttm"),
         "total_market_cap": s6row.get("total_market_cap"),
         "source": s6c.get("source"), "fetched_at": s6c.get("fetched_at")}
        if isinstance(s6row, dict) else
        {"status": "ABSENT", "reason": "S6 截面缓存未生成或该码无行"
                                      "（运行 s6.py --fetch-section <codes>）"})
    if not isinstance(s6row, dict):
        d["degradations"].append("S6 个股估值截面缺席（当前截面；≥120 日序列仍不可用）")
    s7c = _load_json(os.path.join(gen, "s_s7_consensus_%s.json" % as_of))
    s7row = (s7c.get("items") or {}).get(code) if isinstance(s7c, dict) else None
    d["sections"]["s7_consensus"] = (
        {"status": "PRESENT_SEGMENT",
         "covered": s7row.get("has_coverage"),
         "target_price": s7row.get("target_price"),
         "first_year": (s7row.get("years") or [None])[0],
         "source": s7c.get("source"), "fetched_at": s7c.get("fetched_at")}
        if isinstance(s7row, dict) else
        {"status": "ABSENT", "reason": "S7 一致预期缓存未生成或该码无覆盖"
                                      "（运行 s7.py --fetch-consensus <codes>）"})
    if not isinstance(s7row, dict):
        d["degradations"].append("S7 一致预期截面缺席（当前截面；历史预期序列不可用）")

    s5 = _load_json(os.path.join(gen, "s_s5_%s.json" % as_of))
    ent = (s5.get("codes") or {}).get(code) if isinstance(s5, dict) else None
    d["sections"]["s5_capital"] = (
        {"status": "PRESENT", "behavior": ent.get("behavior"),
         "chip_state": ent.get("chip_state")} if isinstance(ent, dict)
        else {"status": "ABSENT"})
    if s5 is None:
        d["degradations"].append("S5 资金快照缺席")

    s2 = _load_json(os.path.join(gen, "s_s2_%s.json" % as_of))
    if isinstance(s2, dict):
        tids = DRV._code_theme_ids(code, OLD_ROOT)
        th = s2.get("themes") or {}
        d["sections"]["s2_themes"] = {
            "status": "PRESENT",
            "themes": [{"theme_id": t, "theme_name": (th.get(t) or {}).get("theme_name"),
                        "stage": (th.get(t) or {}).get("stage")}
                       for t in tids if t in th]}
    else:
        d["sections"]["s2_themes"] = {"status": "ABSENT"}
    d["degradations"].append("S7 预期差 UNAVAILABLE（一致预期无源）；S8 传导链 UNAVAILABLE（产业链图谱无源）")
    d["degradations"].append("E8 公司质地 UNAVAILABLE（无财务/治理源）——质地段缺失")

    # 旧链研究框架（2026-10-19）：装配的顺序与每步的来源件——降级段据此说明
    # 「完整研究长什么样、当前缺哪几步」，而不是只列一堆 ABSENT。
    kb = load_knowledge_base(base=base)
    if kb:
        d["methodology"] = {
            "source": "旧链封版技能 stock-research（结构化导入 · [旧链]）",
            "logic_8_steps": kb.get("framework", {}).get("logic_8_steps", []),
            "section_source_map": kb.get("section_source_map", {}),
            "boundary": kb.get("boundary"),
        }
        for step, src in (kb.get("section_source_map") or {}).items():
            if "UNAVAILABLE" in str(src):
                d["degradations"].append(
                    "框架步 %s 的来源件降级（%s）——该步按旧链框架登记为待解锁"
                    % (step, src))

    snap = _load_json(os.path.join(b, "data", "snapshot", as_of, "daily_quote.csv"))
    if snap is None and os.path.isfile(os.path.join(b, "data", "snapshot", as_of,
                                                    "daily_quote.csv")):
        import csv as _csv
        with io.open(os.path.join(b, "data", "snapshot", as_of, "daily_quote.csv"),
                     encoding="utf-8") as f:
            row = next((r for r in _csv.DictReader(f) if r.get("code") == code), None)
        d["sections"]["price_fact"] = ({"status": "PRESENT",
                                        "close": row.get("close"),
                                        "high": row.get("high"), "low": row.get("low")}
                                       if row else {"status": "ABSENT（该票当日无行情行）"})
    else:
        d["sections"]["price_fact"] = {"status": "ABSENT（快照缺席）"}
    chains = os.path.join(b, "data", "research", "drive", "chains.jsonl")
    last = None
    if os.path.isfile(chains):
        with io.open(chains, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and json.loads(ln).get("code") == code:
                    last = json.loads(ln)
    d["sections"]["c3_chain"] = (
        {"status": "PRESENT", "chain_id": last.get("chain_id"),
         "driver_effective": last.get("driver_effective"),
         "as_of": last.get("as_of")} if last else
        {"status": "ABSENT（该票无 C3 驱动链 review 记录）"})
    return d


def render_md(d):
    L = ["# E7 个股研究 · %s（%s）" % (d["code"], d["as_of"]), ""]
    for k, label in (("s1_events", "S1 事件"), ("s4_crowding", "S4 拥挤"),
                     ("froth", "泡沫（S4 froth）"), ("s5_capital", "S5 资金"),
                     ("s2_themes", "S2 题材"), ("price_fact", "价格事实"),
                     ("s6_valuation", "S6 估值截面"), ("s7_consensus", "S7 一致预期"),
                     ("c3_chain", "C3 驱动链")):
        v = d["sections"][k]
        body = json.dumps({kk: vv for kk, vv in v.items() if kk != "status"},
                          ensure_ascii=False)
        L.append("- %s：%s %s" % (label, v["status"], body if body != "{}" else ""))
    L.append("")
    L.append("## 降级与缺席（如实）")
    for x in d["degradations"]:
        L.append("- %s" % x)
    L.append("")
    m = d.get("methodology")
    if m:
        L.append("## 研究框架（旧链 stock-research · 8 步装配顺序）")
        L.append("")
        L.append("> %s" % m["boundary"])
        L.append("")
        L.append("| # | 步骤 | 来源件 |")
        L.append("|---|---|---|")
        for i, step in enumerate(m["logic_8_steps"], 1):
            src = ""
            for k, v in (m["section_source_map"] or {}).items():
                if step.split("（")[0] in str(k) or str(k) in step:
                    src = v
                    break
            L.append("| %d | %s | %s |" % (i, step, src or "（见来源件映射）"))
        L.append("")
    L.append("> 边界：本件是研究装配（事实 + 分析段），不是结论；驱动/推断/预测终判 = "
             "C3（LLM 提案 + 机械校验），决策 = C4 三问。零 BUY/WAIT/NO。")
    return "\n".join(L)


def emit(as_of, code, *, base=None):
    d = collect(as_of, code, base=base)
    md = render_md(d)
    p = os.path.join(base or ROOT, "data", "generated", "reports",
                     "e7_%s.md" % as_of)
    if os.path.isfile(p) and io.open(p, encoding="utf-8").read() == md:
        return {"created": False, "reason": "EXISTS", "path": p}
    E1W._atomic_write(p, md)
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

    print("== e7 --selftest ==")
    sb = _tf.mkdtemp(prefix="e7_sb_")
    try:
        gen = os.path.join(sb, "data", "generated")
        os.makedirs(gen, exist_ok=True)
        r1 = emit("2026-09-29", "sh600825", base=sb)
        t1 = io.open(r1["path"], encoding="utf-8").read()
        T("B1 全缺源态可产出（不崩、ABSENT 如实）",
          r1["created"] is True and t1.count("ABSENT") >= 5)
        T("B2 降级清单点名 S6/S7/S8/E8",
          all(k in t1 for k in ("S6", "S7", "S8", "E8")))
        # B3 注入 S4/S5 快照 → 该票段 PRESENT
        io.open(os.path.join(gen, "s_s4_2026-09-29.json"), "w", encoding="utf-8").write(
            json.dumps({"codes": {"sh600825": {"crowding": "high",
                                               "evidence": {"boards_n": 6}}}}))
        io.open(os.path.join(gen, "s_s5_2026-09-29.json"), "w", encoding="utf-8").write(
            json.dumps({"codes": {"sh600825": {"code": "sh600825",
                                               "behavior": "relay"}}}))
        r2 = emit("2026-09-29", "sh600825", base=sb)
        T("B3 注入后 S4/S5 段 PRESENT（high/relay）",
          r2["created"] is True and "high" in io.open(r2["path"], encoding="utf-8").read()
          and "relay" in io.open(r2["path"], encoding="utf-8").read())
        T("B4 幂等：重跑 EXISTS", emit("2026-09-29", "sh600825", base=sb)["reason"] == "EXISTS")
        T("B5 X1 落点 e7_<as_of>.md", os.path.basename(r2["path"]) == "e7_2026-09-29.md")
        # ── C 组：旧链研究框架接线（2026-10-19）────────────────────────
        kb = load_knowledge_base()
        T("C1 研究框架库载入且 8 步齐（投资逻辑 8 步）",
          kb is not None and len(kb.get("framework", {}).get("logic_8_steps", [])) == 8)
        # 沙盒 sb 需自带库副本（base 隔离：禁沙盒读生产库）
        os.makedirs(os.path.join(sb, "data", "knowledge", "e7_research"),
                    exist_ok=True)
        _sh.copy(os.path.join(ROOT, _KB_REL, _KB_FNAME),
                 os.path.join(sb, _KB_REL, _KB_FNAME))
        d = collect("2026-09-29", "sh600825", base=sb)
        T("C2 collect 带 methodology（框架+来源件映射）",
          d.get("methodology", {}).get("logic_8_steps")
          and d["methodology"]["section_source_map"])
        r2b = emit("2026-09-29", "sh600825", base=sb)
        t3 = io.open(r2b["path"], encoding="utf-8").read()
        T("C3 报告渲染『研究框架』段（8 步装配顺序表）",
          "研究框架" in t3 and "赛道（空间+格局）" in t3)
        # C4：来源件降级的步骤被逐条登记（不是只列 ABSENT）
        T("C4 降级清单含框架步待解锁登记（E8/S6 来源件）",
          any("框架步" in x for x in d["degradations"]))
        # C5：base 相对解析 + 缺席降级
        sb2 = _tf.mkdtemp(prefix="e7_kb_")
        try:
            os.makedirs(os.path.join(sb2, "data", "knowledge", "e7_research"),
                        exist_ok=True)
            import shutil as _sh2
            _sh2.copy(os.path.join(ROOT, _KB_REL, _KB_FNAME),
                      os.path.join(sb2, _KB_REL, _KB_FNAME))
            T("C5a 沙盒根框架库可载入", load_knowledge_base(base=sb2) is not None)
            os.remove(os.path.join(sb2, _KB_REL, _KB_FNAME))
            T("C5b 缺席 ⇒ None 降级不抛", load_knowledge_base(base=sb2) is None)
        finally:
            _sh.rmtree(sb2, ignore_errors=True)
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    # C 组：真实数据（09-29 生产快照在库；sh600825 = S3 龙头）
    # —— 自检纪律（2026-10-15 修）：C 组**只读消费生产快照**（collect 不落盘），
    #    产出验证走沙盒基座；生产写点须被闸门拒（无票据）。这样自检永不写生产。
    d = collect("2026-09-29", "sh600825")          # 只读：读生产快照，不落盘
    _e7_mtime0 = _mtime(os.path.join(ROOT, "data", "generated", "reports",
                                     "e7_2026-09-29.md"))
    T("C1 真实装配：S4 段来自生产快照（PRESENT）；S5 该票缺席如实标 ABSENT",
      d["sections"]["s4_crowding"]["status"] == "PRESENT"
      and d["sections"]["s5_capital"]["status"] == "ABSENT")
    T("C2 真实装配降级清单如实（S1/S7/S8/E8 点名）",
      all(any(k in x for x in d["degradations"])
          for k in ("S1", "S7", "S8", "E8")))
    sb2 = _tf.mkdtemp(prefix="e7_sb2_")
    try:
        os.makedirs(os.path.join(sb2, "data", "generated", "reports"),
                    exist_ok=True)
        # 沙盒基座：注入 S4/S5 夹具以复现 PRESENT 装配并验证落盘
        gen2 = os.path.join(sb2, "data", "generated")
        io.open(os.path.join(gen2, "s_s4_2026-09-29.json"), "w",
                encoding="utf-8").write(json.dumps(
                    {"codes": {"sh600825": {"crowding": "high",
                                            "evidence": {"boards_n": 6}}}}))
        io.open(os.path.join(gen2, "s_s5_2026-09-29.json"), "w",
                encoding="utf-8").write(json.dumps(
                    {"codes": {"sh600825": {"code": "sh600825",
                                            "behavior": "relay"}}}))
        r = emit("2026-09-29", "sh600825", base=sb2)
        T("C3 沙盒基座落盘成功且为 X1 落点",
          r["created"] is True
          and os.path.basename(r["path"]) == "e7_2026-09-29.md"
          and os.path.abspath(r["path"]).startswith(
              os.path.abspath(sb2)))
        try:
            emit("2026-09-29", "sh600825")      # 无 base ⇒ 生产根 ⇒ 须被闸门拒
            T("C4 生产写点被闸门拒（无票据）", False)
        except Exception as e:
            T("C4 生产写点被闸门拒（%s）" % type(e).__name__,
              "PROD_WRITE_NO_TICKET" in str(e))
        T("C5 拒绝后生产 e7 件 mtime 未变（零写）",
          _mtime(os.path.join(ROOT, "data", "generated", "reports",
                              "e7_2026-09-29.md")) == _e7_mtime0)
    finally:
        _sh.rmtree(sb2, ignore_errors=True)
    print("== e7 --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def _mtime(p):
    return os.path.getmtime(p) if os.path.exists(p) else None


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    if "--eval" in argv:
        import json as _j
        a = argv.index("--eval") + 1
        # --base 沙盒根（缺省 None = 生产；给值必须已存在目录，防误写生产）
        b = (argv[argv.index("--base") + 1] if "--base" in argv else None)
        if b and not os.path.isdir(b):
            print("--base 必须是已存在目录（防误写生产），实际: %s" % b)
            sys.exit(2)
        print(_j.dumps(emit(argv[a], argv[a + 1], base=b), ensure_ascii=False, indent=1))
        sys.exit(0)
    print("用法: e7_research.py --eval <as_of> <code> [--base 沙盒] | --selftest")
    sys.exit(2)
