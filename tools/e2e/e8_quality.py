# -*- coding: utf-8 -*-
"""E8 公司质地评估（2026-10-13 · 第一阶段补齐：契约 + 降级）。

职责（registry）：壁垒 / 成长质量 / 治理 / 估值水位 → 挂载 C3。触发：问「值不值得长期跟踪」。

**缺源核准（2026-10-12 实测）**：data/raw 全盘无结构化财务/估值源（无 PE/PB、
无财报序列、无股权/董事会治理数据）⇒ 三项核心产出（壁垒 / 成长质量 / 治理）
**当前不可机械产出**——与 s6/s10 同款降级范式：
  1. `availability()` 消费者统一入口：逐项列缺 + 解锁条件；
  2. `emit(as_of, code)` 产出**如实降级报告**（e8_<as_of>.md，X1）——报告本身
     就是交付物（回答「值不值得长期跟踪」的**前提核查清单**），不产出伪装质地分。
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

_LEGACY_ROOT = (os.environ.get("OLD_SYSTEM_ROOT")
                or os.path.join(os.path.expanduser("~"), "炒股智能体_旧包_20261008"))  # 旧系统只读参考源；换机器时设 OLD_SYSTEM_ROOT 或放家目录

S_ID = "e8"
OLD_ROOT = _LEGACY_ROOT

SOURCE_CHECKS = (
    ("financials", "财报序列（营收/利润/毛利率，≥8 季）", ("data/raw/bus/", "finance|income|report")),
    ("valuation", "估值序列（PE/PB/PS 日频）", ("data/raw/bus/", "valuation|pe_ttm|pb")),
    ("governance", "治理数据（股权/董事会/增减持/分红）", ("data/raw/bus/", "governance|holder|dividend")),
)


class E8Error(Exception):
    """REASON_CODE 前缀错误。"""


def _probe(old_root=None):
    root = old_root or OLD_ROOT
    out = []
    for key, label, (rel, pat) in SOURCE_CHECKS:
        d = os.path.join(root, rel)
        found = [fn for fn in (os.listdir(d) if os.path.isdir(d) else [])
                 if any(t in fn.lower() for t in pat.split("|")) and fn.endswith(".csv")]
        out.append({"key": key, "label": label, "ready": bool(found), "found": found[:5]})
    return out


def availability(as_of, *, old_root=None):
    src = _probe(old_root)
    gaps = [x["label"] for x in src if not x["ready"]]
    return {"s_id": S_ID, "as_of": as_of, "ready": not gaps, "sources": src,
            "gaps": gaps,
            "wait_for": ["财报数据源（季度营收/利润/毛利率，≥8 季）",
                         "治理数据源（股权结构/增减持/分红）",
                         "估值序列源（PE/PB/PS）"],
            "unlocked_outputs": [] if gaps else
            ["moat（壁垒）", "growth_quality（成长质量）", "governance（治理）",
             "valuation_water_level（估值水位）"]}


# ── 财报披露日历（2026-10-14 接线）：westock disclosure → 财报前置核查 ──────────
# 口径：disclosureDate = **预计披露日**（可为未来）；reportEndDate = 报告期。
# 日期口径纪律：as_of 为评估日；「披露日在 as_of 之后」= 未来披露（前瞻）；
# 「≤ as_of」= 已披露（对照 actual 用——本件无 actual 源，只登记预约）。
def fetch_disclosure(codes):
    """westock disclosure（支持逗号批量）→ 契约行列表（零文件写点）。"""
    import subprocess as _sp
    cl = ",".join(c.strip() for c in codes if c.strip())
    if not cl:
        raise E8Error("codes 为空（逗号分隔代码清单）")
    pr = _sp.run(["westock", "disclosure", cl], capture_output=True, text=True,
                 encoding="utf-8", timeout=120)
    if pr.returncode != 0:
        return {"status": "FAIL", "error": pr.stderr[-160:]}
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
        d = dict(zip(hdr, cells))
        rows.append({"code": d.get("code"), "name": d.get("name"),
                     "disclosure_date": d.get("disclosureDate"),
                     "desc": d.get("disclosureDesc"),
                     "disclosure_end_date": d.get("disclosureEndDate"),
                     "report_end_date": d.get("reportEndDate")})
    return {"status": "PRESENT" if rows else "NO_DATA", "n": len(rows), "rows": rows}


def evaluate(as_of, code, *, old_root=None, disclosure=None):
    from s4 import _chk_date
    D = _chk_date(as_of)
    av = availability(D, old_root=old_root)
    # disclosure 段：调用方传入 fetch_disclosure 结果（evaluate 零网络）
    if disclosure is None:
        dsec = {"status": "ABSENT", "reason": "未提供 disclosure（调用方可经 "
                "fetch_disclosure 注入；evaluate 零网络）"}
    else:
        dsec = dict(disclosure)
        if dsec.get("status") == "PRESENT":
            for r in dsec.get("rows") or []:
                dd = r.get("disclosure_date")
                r["phase"] = ("future" if (dd or "") > D else
                              "past_or_today" if dd else "unknown")
    if av["ready"]:
        raise E8Error("SOURCE_READY: 质地数据源已接入——按 data_contract 填充四段判定 "
                      "（本分支待源接入后实现，禁以占位逻辑冒充质地评估）")
    return {"schema": 1, "s_id": S_ID, "cap": S_ID, "as_of": D, "code": code,
            "status": "UNAVAILABLE", "gaps": av["gaps"], "sources": av["sources"],
            "wait_for": av["wait_for"],
            "disclosure_calendar": dsec,
            "data_contract": {
                "verdicts": ["moat（壁垒：竞争位/护城河证据）",
                             "growth_quality（成长质量：量利齐升 vs 增收不增利）",
                             "governance（治理：股权/增减持/分红记录）",
                             "valuation_water_level（估值水位：历史分位）"],
                "required_inputs": ["财报序列 ≥8 季", "治理事件流", "估值序列"],
                "report": "data/generated/reports/e8_<as_of>.md（UNAVAILABLE 态亦落盘："
                          "前提核查清单即交付物）"},
            "confidence": "N/A"}


def emit(as_of, code, *, base=None, disclosure=None):
    from s4 import _chk_date
    D = _chk_date(as_of)
    r = evaluate(D, code, disclosure=disclosure)
    L = ["# E8 公司质地评估 · %s（%s）—— UNAVAILABLE（缺源降级）" % (code, D), ""]
    L.append("## 数据缺口（如实）")
    for g in r["gaps"]:
        L.append("- %s" % g)
    L.append("")
    L.append("## 财报披露日历（内置 westock disclosure）")
    dsec = r["disclosure_calendar"]
    if dsec.get("status") == "PRESENT":
        for x in dsec["rows"]:
            L.append("- %s（%s）预计披露 %s（报告期 %s，%s）%s"
                     % (x["code"], x.get("name"), x.get("disclosure_date"),
                        x.get("report_end_date"), x.get("phase"),
                        ("| " + (x.get("desc") or "")[:60]) if x.get("desc") else ""))
    else:
        L.append("- %s：%s" % (dsec.get("status"),
                              dsec.get("reason") or dsec.get("error") or "无数据"))
    L.append("")
    L.append("## 解锁条件（wait_for）")
    for w in r["wait_for"]:
        L.append("- %s" % w)
    L.append("")
    L.append("## 源就绪后的判定契约（data_contract）")
    for v in r["data_contract"]["verdicts"]:
        L.append("- %s" % v)
    L.append("")
    L.append("## 前提核查清单（当前可回答的部分）")
    L.append("- 该票是否在你定义的关注域内：待用户/C3 LLM 判定（本件不越权）")
    L.append("- 行情事实（价格/量额）：见 E7 个股研究装配（本件不重复）")
    L.append("")
    L.append("> 边界：零 BUY/WAIT/NO；「值不值得长期跟踪」终判 = C3/C4（LLM），"
             "本件只提供质地数据前提。")
    p = os.path.join(base or ROOT, "data", "generated", "reports", "e8_%s.md" % D)
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

    print("== e8 --selftest ==")
    av = availability("2026-09-29")
    T("A1 availability ready=False 且逐项列缺", av["ready"] is False and len(av["gaps"]) == 3)
    r = evaluate("2026-09-29", "sh600519")
    T("A2 evaluate UNAVAILABLE + data_contract 四段判定",
      r["status"] == "UNAVAILABLE" and len(r["data_contract"]["verdicts"]) == 4)
    sb = _tf.mkdtemp(prefix="e8_sb_")
    try:
        r1 = emit("2026-09-29", "sh600519", base=sb)
        T("B1 降级报告落 X1（e8_<as_of>.md）",
          os.path.basename(r1["path"]) == "e8_2026-09-29.md")
        T("B2 报告含缺口/解锁/契约三段", all(
            k in io.open(r1["path"], encoding="utf-8").read()
            for k in ("数据缺口", "解锁条件", "判定契约")))
        T("B3 幂等", emit("2026-09-29", "sh600519", base=sb)["reason"] == "EXISTS")
        try:
            evaluate("2026-9-1", "sh600519")
            T("B4 非法日期拒绝", False)
        except __import__("s4").S4Error:
            T("B4 非法日期拒绝", True)
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    print("== e8 --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    if "--eval" in argv:
        import json as _j
        a = argv.index("--eval") + 1
        # disclosure 注入：--with-disclosure 时经唯一网络触点取数后传入（evaluate 零网络）
        dsec = (fetch_disclosure(argv[a + 1].split(",")) if "--with-disclosure" in argv
                else None)
        b = (argv[argv.index("--base") + 1] if "--base" in argv else None)
        print(_j.dumps(emit(argv[a], argv[a + 1], disclosure=dsec, base=b),
                       ensure_ascii=False, indent=1))
        sys.exit(0)
    print("用法: e8_quality.py --eval <as_of> <code[,code]> [--with-disclosure] [--base 沙盒] | --selftest")
    sys.exit(2)
