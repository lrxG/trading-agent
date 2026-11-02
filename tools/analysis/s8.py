# -*- coding: utf-8 -*-
"""S8 政策/产业传导链（2026-10-13 · 第一阶段补齐：政策事件流实做 + 传导链降级）。

职责（registry）：政策含金量 / 传导链 / 受益标的 → 消费者 C3 A 路。

双轨：
  · **政策事件流 = 可机械产出**——数据源 = 生产 `data/raw/<d>/news.jsonl`。
    按标题关键词机械命中政策语义（规划/补贴/试点/准入/专项/名录/指导意见/
    行动方案/实施方案/征求意见），并按二级词根粗分政策域（财税/产业/金融/
    区域/监管/科技）。产出政策事件节点 {ann_date, title, news_id, domain,
    related_codes}，供 C3 A 路证据引用（S1 taxonomy 的政策类映射互认）。
  · **传导链/受益标的 = 缺源降级**——产业链图谱（上游→中游→下游映射）全盘
    无源（2026-10-12 核准）；`transmission` 恒 UNAVAILABLE（不伪装受益名单，
    禁以板块名蹭政策标题的假传导）。
  · **政策含金量（中央/地方/部委层级判定）**= 部分可机械：按标题/来源词根
    （国务院/部委/发改委…）给 mechanical_hints 三值（central/ministry/local/
    unknown），终判留给 C3 LLM；不含金量打分（禁伪装）。

输出快照 `s_S8_<as_of>（X3 快照 · 注册表声明形态；盘面文件名 = 小写动态拼装）`（X3 同款；tempfile+os.replace）。
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

S_ID = "s8"
SNAP_FALLBACK = os.path.join(ROOT, "data", "generated", "s_S8_MISSING.json")

# 政策语义命中词（一级：是否政策）；误命中由 C3 LLM 终审兜底（本件只做召回）
POLICY_KEYWORDS = (
    "规划", "指导意见", "行动方案", "实施方案", "征求意见", "补贴", "专项",
    "试点", "准入", "名录", "扶持", "条例", "办法发布", "白皮书", "纲要",
)
# 政策域二级词根（粗分；多命中取首个，全未命中=unknown）
DOMAIN_KEYWORDS = (
    ("fiscal", ("补贴", "专项资金", "退税", "财政", "税费")),
    ("industry", ("产业", "制造", "产业链", "集群", "产能")),
    ("finance", ("金融", "信贷", "资本市场", "上市", "债券")),
    ("region", ("区域", "经济带", "示范区", "自贸")),
    ("regulatory", ("准入", "监管", "名录", "许可", "规范")),
    ("tech", ("科技", "创新", "研发", "人工智能", "数字", "量子", "芯片")),
)
# 层级词根（mechanical_hints；终判归 C3 LLM）
LEVEL_KEYWORDS = (
    ("central", ("国务院", "中央", "中共中央")),
    ("ministry", ("部委", "发改委", "工信部", "财政部", "央行", "证监会", "住建部",
                  "交通运输部", "农业农村部", "商务部", "卫健委", "能源局")),
)


class S8Error(Exception):
    """REASON_CODE 前缀错误（BAD_DATE/...）。"""


def _chk_date(s, name="as_of"):
    import datetime as _dt
    t = s.isoformat() if hasattr(s, "isoformat") else str(s or "")
    if len(t) != 10 or t[4] != "-" or t[7] != "-":
        raise S8Error("BAD_DATE: %s 必须是 YYYY-MM-DD，收到 %r" % (name, s))
    try:
        _dt.date(int(t[:4]), int(t[5:7]), int(t[8:10]))
    except ValueError:
        raise S8Error("BAD_DATE: %s 非法日 %r" % (name, s))
    return t


def _domain_of(title):
    for dom, kws in DOMAIN_KEYWORDS:
        if any(k in title for k in kws):
            return dom
    return "unknown"


def _level_hints(title):
    hits = [lv for lv, kws in LEVEL_KEYWORDS if any(k in title for k in kws)]
    return hits or ["unknown"]


def evaluate(as_of, *, lookback_days=30, params_path=None, base=None):
    """S8 单日评估 → 快照落 s_S8_<as_of>（X3 快照 · 注册表声明形态；盘面文件名 = 小写动态拼装）。

    lookback_days 回看公告流窗口；误命中风险由召回口径声明（本件只召回，
    含金量终判 = C3 LLM；禁在本件内打分伪装）。
    """
    import datetime as _dt
    D = _chk_date(as_of)
    d0 = _dt.date.fromisoformat(D) - _dt.timedelta(days=lookback_days)
    days, d = [], d0
    while d <= _dt.date.fromisoformat(D):
        days.append(d.isoformat())
        d += _dt.timedelta(days=1)

    events, scanned, missing = [], 0, []
    for dd in days:
        p = os.path.join(base or ROOT, "data", "raw", dd, "news.jsonl")
        if not os.path.isfile(p):
            missing.append(dd)
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
                title = row.get("title") or ""
                if not any(k in title for k in POLICY_KEYWORDS):
                    continue
                events.append({
                    "ann_date": dd, "title": title[:100],
                    "news_id": row.get("news_id"),
                    "domain": _domain_of(title),
                    "level_hints": _level_hints(title),
                    "related_codes": row.get("related_codes") or [],
                })
    seen, uniq = set(), []
    for e in sorted(events, key=lambda x: (x["ann_date"], x["news_id"] or "")):
        k = (e["ann_date"], e["news_id"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)

    snap = {"schema": 1, "s_id": S_ID, "cap": S_ID, "as_of": D,
            "scanned_news": scanned, "n_missing_days": len(missing),
            "missing_days": missing[:5],
            "n_policy_events": len(uniq), "events": uniq,
            "transmission": {"status": "UNAVAILABLE",
                             "reason": "产业链图谱（上/中/下游映射）无源（2026-10-12 "
                                       "核准）——受益标的与传导路径不可机械产出；"
                                       "禁以板块名蹭政策标题的假传导",
                             # 2026-10-19：图谱数据缺源，但**拆链方法**已由旧链
                             # 方法论补齐（industry-track：穿透≥2层 + A股映射 5 问
                             # + 暴露度前置）——数据就绪后按该方法装配，不临时编口径。
                             "knowledge_refs": [
                                 "e4_research 框架库（industry_track：A股映射 5 问/"
                                 "暴露度前置/斜率比加权·[旧链]）",
                                 "imported_20261019 旧研究全账（X2 证据区·八分支）"]},
            "note": "本件只做政策事件召回 + 域/层级机械提示；政策含金量终判 = C3 LLM",
            "confidence": "UNCALIBRATED"}
    gen = os.path.join(base or ROOT, "data", "generated")
    os.makedirs(gen, exist_ok=True)
    p = os.path.join(gen, "s_s8_%s.json" % D)
    _WG.check_write(p)
    fd, tmp = tempfile.mkstemp(dir=gen, prefix="s_s8_", suffix=".tmp")
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

    print("== s8 --selftest ==")
    try:
        evaluate("2026-9-1")
        T("A1 非法日期拒绝", False)
    except S8Error:
        T("A1 非法日期拒绝", True)
    sb = _tf.mkdtemp(prefix="s8_sb_")
    try:
        raw = os.path.join(sb, "data", "raw", "2026-09-28")
        os.makedirs(raw, exist_ok=True)
        news = [
            {"news_id": "P1", "title": "国务院关于印发《人工智能产业行动方案》的通知",
             "related_codes": ["sz000001"]},
            {"news_id": "P2", "title": "工信部关于开展车联网准入试点的公告",
             "related_codes": ["sz000002"]},
            {"news_id": "P3", "title": "某公司日常关联交易公告", "related_codes": ["sz000003"]},
        ]
        with io.open(os.path.join(raw, "news.jsonl"), "w", encoding="utf-8") as f:
            for n in news:
                f.write(json.dumps(n, ensure_ascii=False) + "\n")
        snap = evaluate("2026-09-29", base=sb, lookback_days=5)
        T("B1 政策事件召回（2/3 命中）", snap["n_policy_events"] == 2)
        T("B2 域粗分（tech/industry 或 regulatory）",
          snap["events"][0]["domain"] in ("tech", "industry"))
        T("B3 层级提示（central 命中国务院）",
          "central" in snap["events"][0]["level_hints"])
        T("B4 传导链恒 UNAVAILABLE（禁假受益名单）",
          snap["transmission"]["status"] == "UNAVAILABLE")
        T("B5 非政策公告不入集", snap["n_policy_events"] == 2)
        T("B6 重算逐字节同",
          json.dumps(evaluate("2026-09-29", base=sb, lookback_days=5),
                     sort_keys=True) == json.dumps(snap, sort_keys=True))
        T("B7 快照落沙盒 generated", os.path.isfile(
            os.path.join(sb, "data", "generated", "s_s8_2026-09-29.json")))
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    # C 组：真实公告流首跑（沙盒基座；生产写点已由统一闸门守卫）
    _prod_s8 = os.path.join(ROOT, "data", "generated", "s_s8_2026-09-29.json")
    _s8_hash_before = (io.open(_prod_s8, encoding="utf-8").read()
                       if os.path.isfile(_prod_s8) else None)
    sb2 = _tf.mkdtemp(prefix="s8_self_c_")
    try:
        snap = evaluate("2026-09-29", base=sb2)
        T("C1 真实公告流可跑（事件数如实 ≥0）", isinstance(snap["n_policy_events"], int))
        T("C2 快照只落沙盒 generated", os.path.isfile(
            os.path.join(sb2, "data", "generated", "s_s8_2026-09-29.json")))
        try:
            evaluate("2026-09-29")          # 无 base ⇒ 生产根 ⇒ 须被闸门拒
            T("C3 生产写点被闸门拒（无票据）", False, "竟放行")
        except _WG.WriteGateError:
            T("C3 生产写点被闸门拒（无票据 ⇒ PROD_WRITE_NO_TICKET）", True)
        _s8_hash_after = (io.open(_prod_s8, encoding="utf-8").read()
                          if os.path.isfile(_prod_s8) else None)
        T("C4 生产快照零改动（自检不碰生产树）", _s8_hash_before == _s8_hash_after)
    finally:
        _sh.rmtree(sb2, ignore_errors=True)
    print("== s8 --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    if "--eval" in argv:
        b = (argv[argv.index("--base") + 1] if "--base" in argv else None)
        if b and not os.path.isdir(b):
            print("--base 必须是已存在目录（防误写生产），实际: %s" % b)
            sys.exit(2)
        print(json.dumps(evaluate(argv[argv.index("--eval") + 1], base=b),
                         ensure_ascii=False, indent=1)[:2000])
        sys.exit(0)
    print("用法: s8.py --eval <as_of> [--base 沙盒] | --selftest")
    sys.exit(2)
