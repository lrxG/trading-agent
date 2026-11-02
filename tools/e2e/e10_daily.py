# -*- coding: utf-8 -*-
"""E10 每日投研简报（2026-10-13 · 第一阶段补齐）。

职责（registry）：主线 / 异动 / 消息 / 今日看什么 → 挂载 C1 · 消费者 = 用户盘前阅读。
触发：盘前（E1 之后）。**边界：零 BUY/WAIT/NO、零仓位动作**（纯只读装配 + 报告落点）。

输入（全部只读；缺→ABSENT 如实，零编造）：
  · E1 盘前预案   data/generated/reports/e1_<as_of>.md（premarket 产出件）
  · S3 情绪       data/generated/s_s3_<as_of>.json
  · S2 题材       data/generated/s_s2_<as_of>.json
  · S4 拥挤       data/generated/s_s4_<as_of>.json
  · S5 资金       data/generated/s_s5_<as_of>.json
  · S7 财报节点   data/generated/s_s7_<as_of>.json（消息面：刚披露清单）
  · C2 候选池     data/state/candidates.json（state 分布概览）

范式 = E3 先例：单一 append-only jsonl（`data/generated/reports/e10_daily.jsonl`）
+ 行内 `report_md`（纯函数渲染）+ `content_hash` 幂等（同 as_of 同内容 → EXISTS，
异内容 → 拒——append-only 禁改写）。
"""
import hashlib
import io
import json
import os
import sys
import datetime as _dt

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
sys.path.insert(0, _FS)
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
from paths import ROOT  # noqa: E402

S_ID = "e10"
REPORT = os.path.join(ROOT, "data", "generated", "reports", "e10_daily.jsonl")
GEN = os.path.join(ROOT, "data", "generated")


class E10Error(Exception):
    """REASON_CODE 前缀错误。"""


def _chk_date(s):
    t = s.isoformat() if hasattr(s, "isoformat") else str(s or "")
    try:
        _dt.date.fromisoformat(t)
    except ValueError:
        raise E10Error("BAD_DATE: %r 非法（须 YYYY-MM-DD）" % (s,))
    return t


def _load_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def collect(as_of, *, base=None):
    """只读装配：聚合全部输入 → dict（缺→ABSENT，缺就是缺）。"""
    b = base or ROOT
    gen = os.path.join(b, "data", "generated")
    d = {"as_of": as_of}

    e1p = os.path.join(gen, "reports", "e1_%s.md" % as_of)
    d["e1"] = ({"status": "PRESENT",
                "rel": "data/generated/reports/e1_%s.md" % as_of}
               if os.path.isfile(e1p) else {"status": "ABSENT"})
    # S1 事件催化（2026-10-14 接线）：生产快照缺席 = ABSENT（不伪造事件）
    s1 = _load_json(os.path.join(gen, "s_s1_%s.json" % as_of))
    d["s1_events"] = ({"status": "ABSENT"} if s1 is None else
                      {"status": "PRESENT",
                       "n_events": len(s1.get("events") or []),
                       "top": [{"event_id": e.get("event_id"),
                                "strength": e.get("strength"),
                                "n_codes": len(e.get("related_codes") or [])}
                               for e in (s1.get("events") or [])[:8]]})
    s3 = _load_json(os.path.join(gen, "s_s3_%s.json" % as_of))
    d["s3_emotion"] = ({"status": "ABSENT"} if s3 is None else
                       {"status": "PRESENT", "temp": s3.get("temp"),
                        "stage": s3.get("stage"),
                        "leader": (s3.get("leader") or {}).get("name"),
                        "leader_boards": (s3.get("leader") or {}).get("boards")})
    s2 = _load_json(os.path.join(gen, "s_s2_%s.json" % as_of))
    if s2 is None:
        d["s2_theme"] = {"status": "ABSENT"}
    else:
        hist = {}
        for v in (s2.get("themes") or {}).values():
            hist[(v or {}).get("stage")] = hist.get((v or {}).get("stage"), 0) + 1
        # 阶段判定明细（2026-10-14 增强）：top 题材带 stage + stage_reason 依据
        # （不再只报计数；依据 = S2 当日判据原文，降级时整段 ABSENT）
        themes = list((s2.get("themes") or {}).values())
        top = sorted(themes, key=lambda v: (-int((v or {}).get("day_n") or 0),
                                           str((v or {}).get("theme_name") or "")))[:10]
        d["s2_theme"] = {"status": "PRESENT", "n_themes": s2.get("n_themes"),
                         "stage_histogram": hist,
                         "top": [{"theme_name": v.get("theme_name"),
                                  "stage": v.get("stage"),
                                  "day_n": v.get("day_n"),
                                  "stage_reason": (v.get("stage_reason") or [None])[0],
                                  "structure_bias": v.get("structure_bias")}
                                 for v in top]}
    s4 = _load_json(os.path.join(gen, "s_s4_%s.json" % as_of))
    d["s4_crowding"] = ({"status": "ABSENT"} if s4 is None else
                        {"status": "PRESENT", "n_codes": s4.get("n_codes"),
                         "n_high": s4.get("n_high"),
                         "froth": (s4.get("froth") or {}).get("status")})
    s5 = _load_json(os.path.join(gen, "s_s5_%s.json" % as_of))
    d["s5_capital"] = ({"status": "ABSENT"} if s5 is None else
                       {"status": "PRESENT",
                        "behavior_counts": s5.get("behavior_counts"),
                        "n_structured": s5.get("n_structured")})
    s7 = _load_json(os.path.join(gen, "s_s7_%s.json" % as_of))
    if s7 is None:
        d["s7_earnings"] = {"status": "ABSENT"}
        d["s7_consensus"] = {"status": "ABSENT"}
    else:
        fresh = [(e.get("code"), e.get("kind"), e.get("ann_date"))
                 for e in (json.loads(json.dumps(s7.get("by_code") or {}))
                           and [x for v in (s7.get("by_code") or {}).values()
                                for x in v]) if e.get("phase") == "just_disclosed"]
        d["s7_earnings"] = {"status": "PRESENT", "n_events": s7.get("n_events"),
                            "just_disclosed": fresh[:20]}
        # S7 一致预期截面（2026-10-14 接线）：当前预期截面；历史预期序列仍不可用
        eg = s7.get("expectation_gap") or {}
        if eg.get("status") == "PRESENT_SEGMENT":
            items = eg.get("items") or {}
            d["s7_consensus"] = {
                "status": "PRESENT_SEGMENT",
                "semantics": "当前预期截面（历史预期序列不可用）",
                "source": eg.get("source"), "fetched_at": eg.get("fetched_at"),
                "n_codes": eg.get("n_codes"), "n_covered": eg.get("n_covered"),
                # 摘要只取本体（eps/净利/营收+同比），不取派生 pe/pb/ps
                "top": {k: {"name": v.get("name"),
                            "target_price": v.get("target_price"),
                            "covered": v.get("has_coverage"),
                            "first_year": (v.get("years") or [None])[0]}
                        for k, v in sorted(items.items())[:20]}}
        else:
            d["s7_consensus"] = {"status": eg.get("status") or "ABSENT",
                                 "reason": eg.get("reason", "s7 快照无 expectation_gap 段")}
    pp = os.path.join(b, "data", "state", "candidates.json")
    if os.path.isfile(pp):
        try:
            cj = json.load(io.open(pp, encoding="utf-8"))
            dist = {}
            for c in cj.get("candidates") or []:
                dist[c.get("state")] = dist.get(c.get("state"), 0) + 1
            d["pool"] = {"status": "PRESENT", "n": len(cj.get("candidates") or []),
                         "state_dist": dist}
        except Exception:
            d["pool"] = {"status": "UNREADABLE"}
    else:
        d["pool"] = {"status": "ABSENT"}
    d["degradations"] = _degradation_list(d)
    return d


def _degradation_list(d):
    """如实列出本简报的降级/缺席项（用户须知，禁静默）。"""
    out = []
    if d["e1"]["status"] != "PRESENT":
        out.append("E1 盘前预案缺席（未产出）——定调段缺失")
    if d["s1_events"]["status"] != "PRESENT":
        out.append("S1 事件催化快照缺席——事件段缺失（运行 s1.py --eval 生成）")
    if d["s3_emotion"]["status"] != "PRESENT":
        out.append("S3 情绪快照缺席——情绪段缺失")
    if d["s2_theme"]["status"] != "PRESENT":
        out.append("S2 题材快照缺席——题材段缺失")
    if d["s4_crowding"].get("status") != "PRESENT":
        out.append("S4 拥挤快照缺席——拥挤段缺失")
    if d.get("s4_crowding", {}).get("froth") == "UNAVAILABLE":
        out.append("S4 froth（泡沫）缺源锁死 UNAVAILABLE（估值源缺失）")
    if d["s5_capital"]["status"] != "PRESENT":
        out.append("S5 资金快照缺席——资金段缺失")
    if d["s7_earnings"]["status"] != "PRESENT":
        out.append("S7 财报节点缺席——消息面段缺失")
    else:
        s7c = d.get("s7_consensus") or {}
        if s7c.get("status") == "PRESENT_SEGMENT":
            out.append("S7 一致预期 = 当前截面（历史预期序列不可用；回溯式预期差 UNAVAILABLE）")
        else:
            out.append("S7 一致预期截面缺席——运行 s7.py --fetch-consensus <codes> 生成缓存"
                       "（历史预期序列仍不可用）")
    if d["pool"]["status"] != "PRESENT":
        out.append("候选池不可读——候选概览缺失")
    return out


def render_md(d):
    """纯函数渲染（零 LLM 零手工）。"""
    L = ["# E10 每日投研简报 · %s" % d["as_of"], ""]
    L.append("- 定调（E1）：%s" % ("已产出（%s）" % d["e1"]["rel"]
                                  if d["e1"]["status"] == "PRESENT" else "ABSENT"))
    s1 = d["s1_events"]
    L.append("- 事件（S1）：%s" % (
        "%d 事件 · top=%s" % (s1.get("n_events"),
                             [e["event_id"] for e in (s1.get("top") or [])[:5]])
        if s1["status"] == "PRESENT" else "ABSENT"))
    s3 = d["s3_emotion"]
    L.append("- 情绪（S3）：%s" % (
        "%s / %s / 龙头=%s(%s板)" % (s3.get("temp"), s3.get("stage"),
                                    s3.get("leader"), s3.get("leader_boards"))
        if s3["status"] == "PRESENT" else "ABSENT"))
    s2 = d["s2_theme"]
    if s2["status"] == "PRESENT":
        L.append("- 题材（S2）：%d 题材 · 阶段分布 %s" % (s2.get("n_themes"),
                                                       s2.get("stage_histogram")))
        for t in (s2.get("top") or [])[:5]:
            L.append("  - %s：%s（day_n=%s）依据=%s"
                     % (t["theme_name"], t["stage"], t["day_n"],
                        (t.get("stage_reason") or "-")[:60]))
    else:
        L.append("- 题材（S2）：ABSENT")
    s4 = d["s4_crowding"]
    L.append("- 拥挤（S4）：%s" % (
        "观察 %d 票 · 拥挤高 %d · froth=%s" % (s4.get("n_codes"), s4.get("n_high"),
                                              s4.get("froth"))
        if s4["status"] == "PRESENT" else "ABSENT"))
    s5 = d["s5_capital"]
    L.append("- 资金（S5）：%s" % (
        "结构化 %d 票 · 行为分布 %s" % (s5.get("n_structured"),
                                       s5.get("behavior_counts"))
        if s5["status"] == "PRESENT" else "ABSENT"))
    s7 = d["s7_earnings"]
    L.append("- 财报消息（S7）：%s" % (
        "%d 事件 · 刚披露 %s" % (s7.get("n_events"),
                                [x[0] for x in (s7.get("just_disclosed") or [])[:8]]
                                if s7.get("just_disclosed") else "[]")
        if s7["status"] == "PRESENT" else "ABSENT"))
    s7c = d.get("s7_consensus") or {}
    if s7c.get("status") == "PRESENT_SEGMENT":
        L.append("- 一致预期截面（S7）：%d 码 / 覆盖 %d 码 · 源=%s · 取数=%s"
                 % (s7c.get("n_codes") or 0, s7c.get("n_covered") or 0,
                    s7c.get("source"), s7c.get("fetched_at")))
        L.append("  - 语义：当前预期截面（历史预期序列不可用——回溯式预期差仍 UNAVAILABLE）")
    else:
        L.append("- 一致预期截面（S7）：%s" % s7c.get("status"))
    pl = d["pool"]
    L.append("- 候选池（C2）：%s" % (
        "%d 只 · %s" % (pl.get("n"), pl.get("state_dist"))
        if pl["status"] == "PRESENT" else pl["status"]))
    L.append("")
    L.append("## 降级与缺席（如实）")
    for x in d["degradations"]:
        L.append("- %s" % x)
    L.append("")
    L.append("> 边界：本简报零 BUY/WAIT/NO、零仓位动作；判断本体 = C4 三问（LLM）。")
    return "\n".join(L)


def _content_hash(rev):
    body = {k: v for k, v in rev.items() if k != "generated_at"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8")).hexdigest()


def emit(as_of, *, base=None):
    """装配 → 渲染 → append-only 幂等落盘。同 as_of 同内容 → EXISTS；异内容 → 拒。"""
    D = _chk_date(as_of)
    d = collect(D, base=base)
    rev = {"schema": 1, "s_id": S_ID, "cap": S_ID, "as_of": D, "inputs": d,
           "report_md": render_md(d)}
    rev["generated_at"] = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    h = _content_hash(rev)
    rep = os.path.join(base or ROOT, "data", "generated", "reports",
                       "e10_daily.jsonl")
    if os.path.isfile(rep):
        with io.open(rep, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                r = json.loads(ln)
                if r.get("as_of") == D:
                    if r.get("content_hash") == h:
                        return {"created": False, "reason": "EXISTS", "path": rep}
                    raise E10Error("简报 %s 已存在且内容不同——append-only 禁改写"
                                   "（上游快照变化须新 as_of 或显式治理动作）" % D)
    row = dict(rev)
    row["content_hash"] = h
    os.makedirs(os.path.dirname(rep), exist_ok=True)
    _WG.check_write(rep)
    with io.open(rep, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {"created": True, "path": rep, "content_hash": h}


def _selftest():
    import tempfile as _tf
    import shutil as _sh
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== e10 --selftest ==")
    try:
        emit("2026-9-1")
        T("A1 非法日期拒绝", False)
    except E10Error:
        T("A1 非法日期拒绝", True)
    sb = _tf.mkdtemp(prefix="e10_sb_")
    try:
        gen = os.path.join(sb, "data", "generated")
        os.makedirs(os.path.join(gen, "reports"), exist_ok=True)
        # B1 全缺席态：输入全缺 → 简报仍可产出且降级清单如实
        r1 = emit("2026-09-29", base=sb)
        t1 = json.loads(io.open(r1["path"], encoding="utf-8").read().strip()
                        .splitlines()[-1])
        T("B1 全缺席态可产出（不崩、不编造）", r1["created"] is True
          and len(t1["inputs"]["degradations"]) >= 5)
        T("B2 降级清单点名全部缺席段",
          any("E1" in x for x in t1["inputs"]["degradations"])
          and any("S3" in x for x in t1["inputs"]["degradations"]))
        T("B3 幂等：同输入重跑 EXISTS", emit("2026-09-29", base=sb)["reason"] == "EXISTS")
        # B4 上游到场：注入 S3/S4 快照 → PRESENT 且降级清单收窄
        io.open(os.path.join(gen, "s_s3_2026-09-30.json"), "w", encoding="utf-8").write(
            json.dumps({"temp": "温", "stage": "发酵", "leader": {"name": "新华传媒",
                                                                "boards": 6}}))
        io.open(os.path.join(gen, "s_s4_2026-09-30.json"), "w", encoding="utf-8").write(
            json.dumps({"n_codes": 2, "n_high": 1,
                        "froth": {"status": "UNAVAILABLE"}}))
        # B4 上游注入后内容变化 → 拒（append-only 异内容禁改写）
        io.open(os.path.join(gen, "s_s3_2026-09-29.json"), "w", encoding="utf-8").write(
            json.dumps({"temp": "热", "stage": "加速", "leader": {"name": "X",
                                                                "boards": 9}}))
        try:
            r2 = emit("2026-09-29", base=sb)
            T("B4 上游注入后内容变化 → 拒（append-only 异内容禁改写）",
              r2.get("created") is None and "内容不同" in r2.get("reason", ""))
        except E10Error as e:
            T("B4 上游注入后内容变化 → 拒（append-only 异内容禁改写）",
              "内容不同" in str(e))
        r3 = emit("2026-09-30", base=sb)
        T("B5 新 as_of 正常落盘（逐日独立）", r3["created"] is True)
        t3 = json.loads(io.open(r3["path"], encoding="utf-8").read().strip()
                        .splitlines()[-1])
        T("B6 注入后 S3/S4 段 PRESENT 且 froth 降级仍如实",
          t3["inputs"]["s3_emotion"]["status"] == "PRESENT"
          and any("froth" in x for x in t3["inputs"]["degradations"]))
        T("B7 report_md 渲染含边界声明", "零 BUY" in t3["report_md"])
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    # C 组：真实链路（沙盒 base 复刻 X1 报告落点形态；禁自检写真库——生产写入
    # 须持票，自检不可持票；2026-10-20 修：原 C 组直写生产 reports 区导致自检
    # 必挂 PROD_WRITE_NO_TICKET，且 sentinel 硬编码日用尽后被 append-only 拒）。
    import dayclass as _DC10
    _cal = _DC10.Calendar.from_seed_csv(
        os.path.join(ROOT, "data", "raw", "meta", "trade_cal.csv"), source="e10_selftest")
    _used = set()
    _e10p = os.path.join(ROOT, "data", "generated", "reports", "e10_daily.jsonl")
    if os.path.isfile(_e10p):
        for ln in io.open(_e10p, encoding="utf-8"):
            if ln.strip():
                try:
                    _used.add(json.loads(ln).get("as_of"))
                except ValueError:
                    pass
    C_AS_OF = None
    for _d in _cal.sessions_between("2026-10-16", "2027-06-30"):
        if _d not in _used:
            C_AS_OF = _d
            break
    if C_AS_OF is None:
        C_AS_OF = "2027-07-01"        # 理论上不可达（日历外）
    sb2 = _tf.mkdtemp(prefix="e10_c_")
    try:
        os.makedirs(os.path.join(sb2, "data", "generated", "reports"),
                    exist_ok=True)
        r = emit(C_AS_OF, base=sb2)
        T("C1 装配落盘（沙盒复刻 X1 落点形态）| %s"
          % str(r)[:80],
          r["created"] is True and os.path.basename(r["path"]) == "e10_daily.jsonl")
        r2 = emit(C_AS_OF, base=sb2)
        T("C2 链路幂等 | %s" % str(r2)[:80], r2["reason"] == "EXISTS")
    finally:
        _sh.rmtree(sb2, ignore_errors=True)
    print("== e10 --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    if "--eval" in argv:
        # 运转留痕（E 系列留痕欠账收口 · 2026-10-14）：stage=POST / actor=ext:e10。
        base = argv[argv.index("--base") + 1] if "--base" in argv else None
        if base and not os.path.isdir(base):
            print("--base 必须是已存在目录（留痕 log_path 与数据根同源），实际: %s" % base)
            sys.exit(2)
        import observe as _OBS
        _logp = (os.path.join(base, "data", "state", "run_log.jsonl")
                 if base else None)
        _obs = _OBS.run_observed(
            "ext:e10", "e10_daily", "POST",
            lambda: emit(argv[argv.index("--eval") + 1], base=base),
            as_of=argv[argv.index("--eval") + 1],
            detail="E10 每日投研简报（ext 外延 · 人触发盘后入口）",
            raise_on_error=False, log_path=_logp)
        if _obs["error"]:
            print("失败: %s" % _obs["error"])
            sys.exit(1)
        print(json.dumps(_obs["value"], ensure_ascii=False, indent=1))
        sys.exit(0)
    print("用法: e10_daily.py --eval <as_of> [--base <沙盒根>] | --selftest")
    sys.exit(2)
