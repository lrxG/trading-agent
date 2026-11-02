#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""earnings_research.py · 业绩/产业研究工作台（v1.0 · 2026-10-29 建）

## 为什么有本件（外部评估 3/10 处方 · 任务 #277/#278）

评估结论：系统"不会推理业绩和研究趋势"，根因不是数据缺失而是**通道到研究结论之间整段缺失**。
本件把最短核心链路做成可执行件：**一条命令拉齐证据包（每条带来源+日期）→ 强制七字段研究契约 → 预测入 H 账本到期机械结算**。

## 分工纪律（红线）

1. **本件只产事实层证据包 + 契约骨架，零推理**——因果判断归 Agent（LLM），与 intraday_scan 同范式。
2. **零伪造**：任一数据通道失败，如实标 UNAVAILABLE，绝不合成；证据每条带来源命令+采集时间。
3. **契约强制**：`--register` 缺 prediction 数字、缺验证日期、缺证伪线 → 拒绝入账（研究不合格就明说不足，禁空话补齐）。
4. 落点：`data/research/earnings/`（研究工作区，**不写落点 1~5**，不经 writegate 生产闸）。

## 用法

```
python tools/research/earnings_research.py --code sh688388                    # 证据包（打印）
python tools/research/earnings_research.py --code sh688388 --execute          # 证据包落盘
python tools/research/earnings_research.py --code sh688388 --sector 锂电池     # 附行业榜定位
python tools/research/earnings_research.py --register H-ER1029a --claim "..." \
       --metric "..." --window-to 2027-04-30 --invalidate-if "..."            # 预测入 H 账本
python tools/research/earnings_research.py --selftest
```

## 证据包四腿（公司级）

| 腿 | 命令 | 供什么 |
|---|---|---|
| 利润表 | westock finance --type income --limit 3 | 营收/净利/ROE 多期（趋势+拐点） |
| 现金流表 | westock finance --type cashflow --limit 2 | 经营现金流 vs 净利润背离（会计表象检查） |
| 披露日历 | westock disclosure | 预约披露日 → 研究契约⑦验证日期 |
| 资金/价格 | westock fund flow + westock quote | 主力 5 日流向（定价核查）+ 价格截面 |
"""
from __future__ import annotations

import argparse
import datetime
import io
import os
import re
import subprocess
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "tools", "decision"))
import h_ledger  # noqa: E402

OUT_DIR = os.path.join(_ROOT, "data", "research", "earnings")
_UNAVAIL = "UNAVAILABLE（通道失败，零伪造不合成）"


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _run(cmd_args):
    """跑 westock CLI，返回 (ok, text)。失败如实返回 ok=False。"""
    try:
        p = subprocess.run(["westock"] + cmd_args, capture_output=True, timeout=90)
        text = p.stdout.decode("utf-8", "replace")
        if p.returncode != 0 or not text.strip():
            return False, (p.stderr.decode("utf-8", "replace") or "empty output")
        return True, text.strip()
    except Exception as e:  # 通道异常如实记录
        return False, repr(e)


def collect_company(code):
    """公司级四腿证据采集。返回 [(腿名, ok, 源标注, 内容)]"""
    legs = [
        ("利润表·近3期", ["finance", code, "--type", "income", "--limit", "3"]),
        ("现金流·近2期", ["finance", code, "--type", "cashflow", "--limit", "2"]),
        ("披露日历（验证日期来源）", ["disclosure", code]),
        ("个股资金流（主力5日）", ["fund", "flow", code]),
        ("实时行情截面", ["quote", code]),
    ]
    out = []
    for name, args in legs:
        ok, text = _run(args)
        src = "westock %s · 采集 %s" % (" ".join(args), _now())
        out.append((name, ok, src, text if ok else _UNAVAIL + " :: " + text[:200]))
    return out


def extract_disclosure_date(text):
    """从披露日历输出提取下一个预约披露日。返回 (日期|None, 是否回退)。"""
    today = datetime.date.today()
    dates = re.findall(r"20\d{2}-\d{2}-\d{2}", text)
    future = sorted(d for d in dates if d > today.isoformat())
    if future:
        return future[0], False
    fallback = sorted(d for d in dates if d >= today.isoformat())
    return (fallback[0], True) if fallback else (None, True)


def build_md(code, legs, sector_name):
    lines = []
    A = lines.append
    A("# 业绩研究证据包 · %s（%s 采集）" % (code, _now()))
    A("")
    A("> 本件=事实层证据包（零推理）。研究结论由 Agent 按模板 `templates/业绩研究模板_v1.md` 七字段作答，")
    A("> 每条证据必须标注下方来源与采集时间；任一腿 UNAVAILABLE 时研究必须声明对应字段「不足以判断」。")
    A("")
    for name, ok, src, text in legs:
        A("## %s %s" % (name, "✅" if ok else "❌"))
        A("")
        A("来源：`%s`" % src)
        A("")
        A("```")
        for ln in text.splitlines()[:60]:
            A(ln)
        A("```")
        A("")
    dd, dd_fb = extract_disclosure_date("\n".join(t for _, ok, _, t in legs if ok))
    A("## 契约骨架（Agent 作答区，七字段缺一不合格）")
    A("")
    if sector_name:
        A("0. 产业补充字段：产业链位置 / 生命周期阶段 / 供需四件套 / 需求拉动 vs 供给收缩（模板产业段）")
    A("1. 结论（方向+幅度+期限，增长性质四选一）")
    A("2. 证据（引用上表，逐条带来源+日期）")
    A("3. 推理（因果链：业务驱动→收入/毛利率/费用/现金流怎么变→为什么）")
    A("4. 反证（什么证据推翻本结论；已考虑的反方理由）")
    A("5. 预测（数字+区间+截止报告期）")
    A("6. 置信度（高/中/低+依据）")
    if dd and not dd_fb:
        A("7. 验证（机械可判指标 + 到期日，披露日历给出预约披露日候选 **%s**）" % dd)
    elif dd:
        A("7. 验证（机械可判指标 + 到期日；⚠️ 日历未见未来披露日，%s 为回退值，**须人工核定**下一期预约日）" % dd)
    else:
        A("7. 验证（机械可判指标 + 到期日；⚠️ 披露日历未解析到日期，须人工核定）")
    A("")
    A("## 不足即写不足")
    A("")
    A("任一腿 ❌ 或证据不足以支撑预测时，对应字段明写「不足以判断」，禁空话补齐（模板红线）。")
    return "\n".join(lines), dd


def cmd_register(args):
    """预测入 H 账本，契约强制：claim/metric/window_to 非空 + metric 含数字 + window_to 日期形。"""
    errs = []
    if not args.claim or not args.claim.strip():
        errs.append("缺 --claim（预测内容）")
    if not args.metric or not args.metric.strip():
        errs.append("缺 --metric（验证指标/证伪线）")
    elif not re.search(r"\d", args.metric):
        errs.append("--metric 必须含数字（预测无数字=不合格，模板⑤）")
    if not args.window_to or not re.match(r"20\d{2}-\d{2}-\d{2}", args.window_to):
        errs.append("缺/错 --window-to（必须 YYYY-MM-DD，模板⑦）")
    if errs:
        print("[REJECTED] 研究契约不合格，拒绝入账：")
        for e in errs:
            print("  -", e)
        return 2
    h_ledger.create(args.register, args.claim.strip(), args.metric.strip(),
                    args.window_from or "2026-10-29", args.window_to,
                    args.invalidate_if or "—", "earnings_research.py 预测入账")
    st = h_ledger.status(args.register)
    print("[REGISTERED] %s → %s" % (args.register, st.get("kind")))
    return 0


def _selftest():
    """临时目录验收：契约拒绝路径 + 证据包装配。不联网。"""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="er_selftest_")
    lp = os.path.join(tmp, "h_ledger.jsonl")
    n_fail = 0

    def chk(name, cond):
        nonlocal n_fail
        print(("  [✓] " if cond else "  [✗] ") + name)
        if not cond:
            n_fail += 1

    # 契约拒绝：缺 metric 数字
    class A1:
        register = "H-TEST1"; claim = "x"; metric = "环比改善"; window_to = "2027-04-30"
        invalidate_if = ""; window_from = ""
    chk("缺数字 metric 被拒", cmd_register(A1) == 2)
    # 契约拒绝：window_to 非日期
    class A2(A1):
        metric = "净利>=3亿"; window_to = "年报"
    chk("非日期 window_to 被拒", cmd_register(A2) == 2)
    # 合格入账（走临时账本）
    orig = h_ledger.create
    def fake_create(hid, claim, metric, wf, wt, inv, src, path=None):
        return orig(hid, claim, metric, wf, wt, inv, src, path=lp)
    h_ledger.create = fake_create
    class A3(A2):
        metric = "Q4单季净利1.2~1.8亿"; window_to = "2027-04-30"
    chk("合格预测可入账", cmd_register(A3) == 0)
    chk("账本状态 ACTIVE", h_ledger.status("H-TEST1", path=lp).get("kind") == "ACTIVE")
    h_ledger.create = orig
    # 证据包装配（含 UNAVAILABLE 腿）
    legs = [("利润表·近3期", True, "westock finance · 采集 T", "营收 100 亿"),
            ("披露日历", False, "westock disclosure · 采集 T", _UNAVAIL)]
    md, dd = build_md("shTEST", legs, None)
    chk("证据包含 UNAVAILABLE 腿标注", "UNAVAILABLE" in md and "❌" in md)
    chk("披露腿失败→验证日期须人工核定提示", "人工核定" in md and dd is None)
    md2, dd2 = build_md("shTEST", legs, "锂电池")
    chk("sector 参数触发产业字段行", "产业补充字段" in md2)
    print("== earnings_research --selftest: %s ==" % ("全部通过" if n_fail == 0 else "%d 失败" % n_fail))
    return 0 if n_fail == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="业绩/产业研究工作台（证据包+契约+入账）")
    ap.add_argument("--code", help="标的代码 sh688388")
    ap.add_argument("--sector", default=None, help="所属行业名（用于产业字段提示）")
    ap.add_argument("--execute", action="store_true", help="证据包落盘 data/research/earnings/")
    ap.add_argument("--register", help="HID（H-ER<MMDD><序号>），预测入 H 账本")
    ap.add_argument("--claim", default="")
    ap.add_argument("--metric", default="")
    ap.add_argument("--window-from", default="")
    ap.add_argument("--window-to", default="")
    ap.add_argument("--invalidate-if", default="")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()
    if args.register:
        return cmd_register(args)
    if not args.code:
        ap.print_help()
        return 2
    legs = collect_company(args.code)
    md, _ = build_md(args.code, legs, args.sector)
    if args.execute:
        os.makedirs(OUT_DIR, exist_ok=True)
        fn = os.path.join(OUT_DIR, "evidence_%s_%s.md" % (
            args.code.replace(":", "_"), datetime.datetime.now().strftime("%Y%m%d_%H%M")))
        io.open(fn, "w", encoding="utf-8", newline="\n").write(md)
        print("[SAVED]", fn)
        n_ok = sum(1 for _, ok, _, _ in legs if ok)
        print("[LEGS] %d/%d 通道正常" % (n_ok, len(legs)))
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
