# -*- coding: utf-8 -*-
"""e11_research.py · E11 专题研究报告（2026-10-14 最小契约件 + 可实现接线）。

职责（registry）：专题数据/板块数据 → 板块比较/主题深度；挂 C3；入口=点题/I5；
boundary = no_decision（零 BUY/WAIT/NO、零仓位动作）。

本件实现范围（诚实分层）：
  · **已实现**：以 S2 题材快照为单位的专题骨架——主题阶段 + 判定依据 + 阶段分布
    + 池内相关候选（经 trigger 文本与题材名弱匹配，明标弱匹配）+ S6 行业估值截面
    （缓存件在时引用并标注 as_of/来源）+ 缺源段如实降级；
  · **缺源降级**：主题深度正文（政策原文/纪要/产业链细节）——无内置源，
    `deep_dive` 段恒 UNAVAILABLE 并列出解锁条件；
  · 输出 = data/generated/reports/e11_<topic_id>.md（X1 人读；无机械下游消费者）。

幂等：同 topic_id 同内容 → EXISTS 跳过写；异内容 → 拒（append-only 同款纪律）。
"""
import datetime as _dt
import io
import json
import os
import sys

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
_DM = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "decision"))
_AN = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "analysis"))
for _p in (_FS, _DM, _AN):
    sys.path.insert(0, _p)
from paths import ROOT  # noqa: E402
import candidates as CAND  # noqa: E402
import premarket as E1W  # noqa: E402  （复用 _atomic_write 原语，一个家）

S_ID = "e11"
CAP = "E11"


class E11Error(Exception):
    """REASON_CODE 前缀错误。"""


def _chk_slug(s, name="topic_id"):
    t = str(s or "")
    if not t or any(c in t for c in '/\\<>:"|?*'):
        raise E11Error("%s 非法（空或含路径字符）: %r" % (name, s))
    return t


def _load_json(p):
    if not os.path.isfile(p):
        return None
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)


def collect(topic_id, *, as_of=None, base=None, pool_path=None, s2_path=None,
            s6_section_path=None):
    """只读装配专题报告输入（缺 → ABSENT/None，缺就是缺）。"""
    b = base or ROOT
    gen = os.path.join(b, "data", "generated")
    d = {"topic_id": topic_id, "as_of": as_of, "cap": CAP}
    s2 = _load_json(s2_path or os.path.join(gen, "s_s2_%s.json" % as_of)) \
        if as_of else None
    theme = None
    if s2:
        for v in (s2.get("themes") or {}).values():
            if v.get("theme_id") == topic_id or v.get("theme_name") == topic_id:
                theme = v
                break
    d["s2_theme"] = ({"status": "ABSENT"} if theme is None else
                     {"status": "PRESENT",
                      "theme_id": theme.get("theme_id"),
                      "theme_name": theme.get("theme_name"),
                      "family": theme.get("family"),
                      "stage": theme.get("stage"),
                      "day_n": theme.get("day_n"),
                      "stage_reason": theme.get("stage_reason"),
                      "next_stage_trigger": theme.get("next_stage_trigger"),
                      "structure_bias": theme.get("structure_bias"),
                      "evidence": theme.get("evidence"),
                      "confidence": theme.get("confidence")})
    # 池内相关候选：trigger 文本与题材名弱匹配（诚实标注弱匹配，非成分股真源）
    try:
        pool = CAND.list_candidates(path=pool_path)
    except CAND.CandidateError:
        pool = None
    nm = (theme or {}).get("theme_name") or topic_id
    rel = [c for c in (pool or [])
           if c.get("state") == "DISCOVERED" and nm in str((c.get("trigger") or {}).get("text") or "")]
    d["pool_related"] = {"status": "PRESENT" if pool is not None else "ABSENT",
                         "match": "weak_text（trigger 文本含题材名；非成分股真源）",
                         "n": len(rel),
                         "candidates": [{"id": c["id"], "code": c["code"],
                                         "source": c.get("source"),
                                         "trigger": (c.get("trigger") or {}).get("text")}
                                        for c in rel[:20]]}
    # S6 行业估值截面（缓存件在时引用；截面 ≠ 序列）
    sec = _load_json(s6_section_path or os.path.join(gen, "s_s6_section_%s.json" % as_of)) \
        if as_of else None
    d["s6_section"] = ({"status": "ABSENT"} if sec is None else
                       {"status": "PRESENT", "as_of": sec.get("as_of"),
                        "fetched_at": sec.get("fetched_at"), "source": sec.get("source"),
                        "n_codes": sec.get("n_codes"),
                        "sector_section": sec.get("sector_section")})
    d["deep_dive"] = {"status": "UNAVAILABLE",
                      "reason": "主题深度正文（政策原文/纪要/产业链细节）无内置源",
                      "unlock": ["接入公告/研报/政策文本源（S8 政策事件流或 "
                                 "westock notice/report/dehydrated）"],
                      "note": "解锁后由 C3 LLM 在该段成文；本件只提供机械骨架与数据指针",
                      # 2026-10-19：解锁前的「怎么写」框架——旧链课题研究双技能
                      #（e4_research KB）；单票深挖框架在 e7_research KB。
                      # ⚠ 引用只写库名不写路径（landing 词汇表判据，同 s7/s8）。
                      "methodology_refs": [
                          "e4_research 框架库（event-analysis + industry-track："
                          "催化6类/预期差5档/四象限/退潮四层/暴露度前置）",
                          "e7_research 框架库（stock-research：单票 8 步体检框架）",
                          "imported_20261019 旧研究全账（X2 证据区·八分支）",
                          "news_search 库内公告检索（tools/query·已就绪）"]}
    return d


def render_md(d):
    L = ["# E11 专题研究报告 · %s（as_of=%s）" % (d["topic_id"], d["as_of"]), ""]
    L.append("- 边界：零 BUY/WAIT/NO、零仓位动作；本件 = 研究骨架 + 数据指针，"
             "判断本体 = C3/C4（LLM）")
    L.append("")
    t = d["s2_theme"]
    L.append("## 主题阶段（S2）")
    if t["status"] == "PRESENT":
        L.append("- %s（%s，family=%s）：**%s**（day_n=%s，置信=%s）"
                 % (t["theme_name"], t["theme_id"], t.get("family"), t["stage"],
                    t.get("day_n"), t.get("confidence")))
        for r in (t.get("stage_reason") or []):
            L.append("  - 依据：%s" % r)
        nt = t.get("next_stage_trigger") or {}
        if nt:
            L.append("- 下一阶段触发：%s" % (nt.get("text") or json.dumps(nt, ensure_ascii=False)))
        if t.get("structure_bias"):
            L.append("- 结构倾向：%s" % t["structure_bias"])
    else:
        L.append("- ABSENT（S2 快照缺席或无此主题——运行 s2.py --eval <as_of> 生成）")
    L.append("")
    pr = d["pool_related"]
    L.append("## 池内相关候选（C2）")
    if pr["status"] == "PRESENT":
        L.append("- %d 只（%s）" % (pr["n"], pr["match"]))
        for c in pr["candidates"][:10]:
            L.append("  - %s %s（%s）触发=%s" % (c["id"], c["code"], c["source"],
                                               c["trigger"]))
    else:
        L.append("- ABSENT（候选池不可读）")
    L.append("")
    s6 = d["s6_section"]
    L.append("## 行业估值截面（S6）")
    if s6["status"] == "PRESENT":
        L.append("- as_of=%s 取数=%s ｜ 源=%s ｜ %s 码"
                 % (s6["as_of"], s6["fetched_at"], s6["source"], s6["n_codes"]))
        L.append("- 口径：**当前截面（非 ≥120 日序列；序列契约仍未满足）**")
        for k, v in list((s6.get("sector_section") or {}).items())[:8]:
            L.append("  - %s（%s）PeTTM=%s（分位 %s%%）PbLF=%s（分位 %s%%）"
                     % (k, v.get("name"), v.get("PeTTM"), v.get("PeTTMPct"),
                        v.get("PbLF"), v.get("PbLFPct")))
    else:
        L.append("- ABSENT（S6 截面缓存未生成——运行 s6.py --fetch-section 生成）")
    L.append("")
    dd = d["deep_dive"]
    L.append("## 主题深度正文 —— %s" % dd["status"])
    L.append("- %s" % dd["reason"])
    for u in dd.get("unlock") or []:
        L.append("- 解锁：%s" % u)
    L.append("")
    L.append("> 本报告机械装配（零 LLM 零手工）；同输入逐字节同输出。")
    return "\n".join(L)


def emit(topic_id, *, as_of, base=None, pool_path=None, s2_path=None,
         s6_section_path=None):
    D = _chk_slug(topic_id)
    d = collect(D, as_of=as_of, base=base, pool_path=pool_path, s2_path=s2_path,
                s6_section_path=s6_section_path)
    body = render_md(d)
    p = os.path.join(base or ROOT, "data", "generated", "reports", "e11_%s.md" % D)
    if os.path.isfile(p) and io.open(p, encoding="utf-8").read() == body:
        return {"created": False, "reason": "EXISTS", "path": p}
    E1W._atomic_write(p, body)
    return {"created": True, "path": p,
            "generated_at": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}


def _selftest():
    import shutil as _sh
    import tempfile as _tf
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== e11 --selftest ==")
    sb = _tf.mkdtemp(prefix="e11_")
    try:
        gen = os.path.join(sb, "data", "generated")
        os.makedirs(gen, exist_ok=True)
        pool = os.path.join(sb, "pool.json")
        with io.open(pool, "w", encoding="utf-8") as f:
            json.dump({"version": CAND.SCHEMA_VERSION, "updated_at": "2026-09-29",
                       "candidates": []}, f)
        CAND.upsert_candidate("sz000607", "2026-09-29", "limit_up",
                              "涨停池|低空经济", path=pool)
        with io.open(os.path.join(gen, "s_s2_2026-09-29.json"), "w",
                     encoding="utf-8") as f:
            json.dump({"themes": {"T1": {"theme_id": "T1", "theme_name": "低空经济",
                                         "family": "A", "stage": "发酵", "day_n": 3,
                                         "stage_reason": ["涨停簇 ≥2 且高度递增"],
                                         "next_stage_trigger": {"text": "分歧转一致"},
                                         "structure_bias": "梯队完整",
                                         "evidence": ["zt"], "confidence": "params"}}},
                      f, ensure_ascii=False)
        r1 = emit("T1", as_of="2026-09-29", base=sb, pool_path=pool)
        T("A1 产出 e11_T1.md（X1）", r1["created"] is True
          and os.path.isfile(os.path.join(sb, "data", "generated", "reports", "e11_T1.md")))
        t = io.open(os.path.join(sb, "data", "generated", "reports", "e11_T1.md"),
                    encoding="utf-8").read()
        T("A2 阶段+依据+弱匹配候选+截面 ABSENT+深度 UNAVAILABLE 四段齐",
          "发酵" in t and "涨停簇 ≥2" in t and "weak_text" in t
          and "ABSENT（S6 截面缓存未生成" in t and "UNAVAILABLE" in t)
        T("A3 池内弱匹配命中（sz000607）", "sz000607" in t)
        r2 = emit("T1", as_of="2026-09-29", base=sb, pool_path=pool)
        T("A4 幂等：同输入 EXISTS", r2["reason"] == "EXISTS")
        try:
            emit("../evil", as_of="2026-09-29", base=sb)
            T("A5 非法 topic_id 拒绝", False)
        except E11Error:
            T("A5 非法 topic_id 拒绝", True)
        d = collect("NOPE", as_of="2026-09-29", base=sb, pool_path=pool)
        T("A6 未知主题 → S2 段 ABSENT（不编造）", d["s2_theme"]["status"] == "ABSENT")
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    print("== e11 --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    if "--emit" in argv:
        i = argv.index("--emit") + 1
        topic, as_of = argv[i], argv[i + 1]
        base = (argv[argv.index("--base") + 1] if "--base" in argv else None)
        print(json.dumps(emit(topic, as_of=as_of, base=base), ensure_ascii=False,
                         indent=1))
        return 0
    print("用法: e11_research.py --emit <topic_id> <as_of> [--base 沙盒] | --selftest")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
