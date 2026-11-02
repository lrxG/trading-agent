# -*- coding: utf-8 -*-
"""h_ledger.py · 盘前假设 H 账本（append-only · 最小闭环 · 2026-10-29 建）

## 背景（用户裁决 2026-10-29）

盘前预案的 H 假设此前只活在 markdown 里，盘中可被无痕改写 ⇒ 归因失真。
用户定性三个真实缺口：H 无结构化立账 / 盘中失效处理 / 盘后结算。
本件实现三者共用的最小闭环（同一 H ID 贯穿），不重构 Thesis/hypothesize（另体系，不合并），
不重写 premarket.py，不加预案章节。

## 设计（用户裁决边界）

- **append-only**：账本 JSONL 只追加，禁覆盖/禁改写（与 thesis.py 同纪律，独立文件不共用状态机）
- **状态机（单向）**：CREATED → INVALIDATED（盘中触发失效）→ （可选）SETTLED；
  或 CREATED → SETTLED（时间窗到出结论）。SETTLED 后终结，任何再追加被拒。
- **失效 ≠ 卖出**：INVALIDATED 仅含义 = 「撤销依赖该 H 的新开仓依据 + 重评持仓」；
  是否减仓/退出仍由交易决策与 C5 硬风控决定。本件不产生任何交易指令。
- **H ID 全局格式**：H-YYYYMMDD<seq>（如 H-20261029a），三环节共用同一 ID。

## 记录类型（JSONL 每行一条）

- H_CREATED   {hid, ts, claim, metric, window_from, window_to, invalidate_if, source_plan}
- H_INVALIDATED {hid, ts, evidence, by}
- H_SETTLED   {hid, ts, verdict(SUPPORTED/REFUTED/INCONCLUSIVE), evidence, actual}

## 用法

    python tools/decision/h_ledger.py --create-from-plan 盘前预案_2026-10-29.md   # 解析⑥节H表立账
    python tools/decision/h_ledger.py --invalidate H-20261029b --evidence "通信高开低走" --by intraday
    python tools/decision/h_ledger.py --settle H-20261029a --verdict SUPPORTED --evidence "..." --actual "..."
    python tools/decision/h_ledger.py --status [HID]                              # 查状态
    python tools/decision/h_ledger.py --selftest                                  # 沙盒自检

## 已知边界（如实）

- 盘中失效的「触发」目前是人工/半自动（盘中扫描后由会话判定调用 --invalidate），
  自动触发器未实现——最小闭环先立账本与状态机，触发自动化挂账。
- assert_tradeable() 原语已备（INVALIDATED 的 H 依赖开仓会被拒），尚未接入 run_chain 决策链。
"""
import argparse
import json
import os
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
LEDGER = os.path.join(_ROOT, "data", "state", "h_ledger.jsonl")

VALID_RECORD = {"H_CREATED", "H_INVALIDATED", "H_SETTLED"}
VALID_VERDICT = {"SUPPORTED", "REFUTED", "INCONCLUSIVE"}


class LedgerError(Exception):
    pass


def _now():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _append(rec, path=None):
    """追加一行（append-only 唯一写入口）。path=None → 生产账本。"""
    line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    with open(path or LEDGER, "a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


def load(path=None):
    """重放账本 → {hid: [rec, ...]}（保持顺序）。"""
    path = path or LEDGER
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", newline="") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError as e:
                raise LedgerError("账本第 %d 行非合法 JSON：%s" % (ln, e))
            if r.get("type") not in VALID_RECORD:
                raise LedgerError("账本第 %d 行 type 非法：%r" % (ln, r.get("type")))
            out.setdefault(r["hid"], []).append(r)
    return out


def status(hid, path=None):
    """→ dict(kind, detail)。kind ∈ ACTIVE/INVALIDATED/SETTLED/UNKNOWN"""
    recs = load(path).get(hid, [])
    if not recs:
        return {"kind": "UNKNOWN", "detail": "账本无此 HID"}
    created = [r for r in recs if r["type"] == "H_CREATED"]
    if not created:
        return {"kind": "UNKNOWN", "detail": "无 H_CREATED（首条=%s）" % recs[0]["type"]}
    inv = [r for r in recs if r["type"] == "H_INVALIDATED"]
    stl = [r for r in recs if r["type"] == "H_SETTLED"]
    if stl:
        return {"kind": "SETTLED", "detail": stl[-1],
                "was_invalidated": bool(inv), "created": created[0]}
    if inv:
        return {"kind": "INVALIDATED", "detail": inv[-1], "created": created[0]}
    return {"kind": "ACTIVE", "detail": created[0]}


def create(hid, claim, metric, window_from, window_to, invalidate_if, source_plan, path=None):
    """立账。重复 CREATED 拒绝（幂等防护）。"""
    st = status(hid, path)
    if st["kind"] != "UNKNOWN":
        raise LedgerError("%s 已立账（kind=%s），禁止重复创建" % (hid, st["kind"]))
    rec = {"type": "H_CREATED", "hid": hid, "ts": _now(),
           "claim": claim, "metric": metric,
           "window_from": str(window_from), "window_to": str(window_to),
           "invalidate_if": invalidate_if, "source_plan": source_plan}
    _append(rec, path)
    return rec


def invalidate(hid, evidence, by, path=None):
    """盘中失效。前提：ACTIVE。SETTLED 后拒绝。重复 INVALIDATED 拒绝（幂等）。"""
    st = status(hid, path)
    if st["kind"] == "UNKNOWN":
        raise LedgerError("%s 未立账，不可失效" % hid)
    if st["kind"] == "SETTLED":
        raise LedgerError("%s 已结算，不可再失效" % hid)
    if st["kind"] == "INVALIDATED":
        raise LedgerError("%s 已是 INVALIDATED（首次=%s），不可重复" % (hid, st["detail"].get("ts")))
    rec = {"type": "H_INVALIDATED", "hid": hid, "ts": _now(),
           "evidence": evidence, "by": by}
    _append(rec, path)
    return rec


def settle(hid, verdict, evidence, actual, path=None):
    """盘后结算。三态 verdict；ACTIVE/INVALIDATED 均可结算（失效后仍可归因），
    已 SETTLED 拒绝（结论不可改，防事后修正污染归因）。"""
    if verdict not in VALID_VERDICT:
        raise LedgerError("verdict 只能是 %s，实际 %r" % ("/".join(sorted(VALID_VERDICT)), verdict))
    st = status(hid, path)
    if st["kind"] == "UNKNOWN":
        raise LedgerError("%s 未立账，不可结算" % hid)
    if st["kind"] == "SETTLED":
        raise LedgerError("%s 已结算（verdict=%s），结论不可改" % (
            hid, st["detail"].get("verdict")))
    rec = {"type": "H_SETTLED", "hid": hid, "ts": _now(), "verdict": verdict,
           "evidence": evidence, "actual": actual,
           "was_invalidated": st["kind"] == "INVALIDATED"}
    _append(rec, path)
    return rec


def assert_tradeable(hid, path=None):
    """决策链原语：依赖某 H 开仓前调用。INVALIDATED/UNKNOWN → 拒绝。
    （尚未接入 run_chain，先作为可测试原语存在。）"""
    st = status(hid, path)
    if st["kind"] == "INVALIDATED":
        raise LedgerError("%s 已失效（%s），禁止作为新开仓依据；如需继续依赖须重新评估并立新 H" % (
            hid, st["detail"].get("evidence", "")[:80]))
    if st["kind"] == "UNKNOWN":
        raise LedgerError("%s 未立账，禁止作为开仓依据" % hid)
    return st


def _parse_plan_h_table(plan_path):
    """解析盘前预案 md ⑥节 H 表（五列：编号|预测内容|验证指标|有效时间窗|失效条件）。
    只在「## ⑥」节内解析（其他节的表格可能引用 H 编号，严禁误立账）；行首必须为 H 编号。"""
    import re
    if not os.path.exists(plan_path):
        raise LedgerError("预案文件不存在：%s" % plan_path)
    src = os.path.basename(plan_path)
    mdate = re.search(r"(\d{4})-(\d{2})-(\d{2})", src)
    year = mdate.group(1) if mdate else "????"
    lines = open(plan_path, encoding="utf-8").read().splitlines()
    # 定位 ⑥ 节
    in_sec = False
    sec_lines = []
    for line in lines:
        if re.match(r"^#{2,3}\s+⑥", line):
            in_sec = True
            continue
        if in_sec and re.match(r"^#{2,3}\s+", line):
            break
        if in_sec:
            sec_lines.append(line)
    rows = []
    for line in sec_lines:
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        m = re.match(r"^H-(\d{4})([a-z]+)$", cells[0])
        if not m:
            continue
        full = "H-%s%s%s" % (year, m.group(1), m.group(2))
        rows.append((full, cells[0], cells[1], cells[2], cells[3], cells[4]))
    return rows, year


def create_from_plan(plan_path, path=None):
    """从预案 md ⑥节 H 表批量立账。返回 [hid, ...]（新建的）。"""
    rows, prefix = _parse_plan_h_table(plan_path)
    if not rows:
        raise LedgerError("预案⑥节未解析到 H 表行（检查五列表格）：%s" % plan_path)
    made = []
    for full, short, claim, metric, win, inv in rows:
        create(full, claim, metric,
               win, win, inv, os.path.basename(plan_path), path)
        made.append("%s(预案内 %s)" % (full, short))
    return made


# ────────────────────────── CLI ──────────────────────────

def _cli():
    ap = argparse.ArgumentParser(description="盘前假设 H 账本（append-only）")
    ap.add_argument("--create-from-plan", metavar="PLAN_MD")
    ap.add_argument("--invalidate", metavar="HID")
    ap.add_argument("--settle", metavar="HID")
    ap.add_argument("--status", metavar="HID", nargs="?", const="__ALL__")
    ap.add_argument("--evidence", default="")
    ap.add_argument("--actual", default="")
    ap.add_argument("--verdict", default="")
    ap.add_argument("--by", default="manual")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        ok, fail = _selftest()
        print("selftest: %d 通过 / %d 失败" % (ok, fail))
        sys.exit(0 if fail == 0 else 1)
    if a.create_from_plan:
        made = create_from_plan(a.create_from_plan)
        print("立账 %d 条：" % len(made))
        for m_ in made:
            print("  " + m_)
        return
    if a.invalidate:
        r = invalidate(a.invalidate, a.evidence, a.by)
        print(json.dumps(r, ensure_ascii=False))
        return
    if a.settle:
        r = settle(a.settle, a.verdict, a.evidence, a.actual)
        print(json.dumps(r, ensure_ascii=False))
        return
    if a.status:
        if a.status == "__ALL__":
            for hid in sorted(load()):
                st = status(hid)
                print("%s  %s  %s" % (hid, st["kind"],
                                      json.dumps(st.get("detail", {}), ensure_ascii=False)[:120]))
        else:
            print(json.dumps(status(a.status), ensure_ascii=False, indent=2))
        return
    ap.print_help()


# ────────────────────────── 自检 ──────────────────────────

def _selftest():
    """沙盒账本自检：状态机/幂等/append-only/解析。生产账本零接触。"""
    import tempfile
    ok = fail = 0

    def chk(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print("  [FAIL] %s" % name)

    tmp = os.path.join(tempfile.mkdtemp(), "h_ledger_test.jsonl")

    # 1 创建
    create("H-20260101a", "测试预测A", "指标X", "20260101", "20260101", "若Y则失效",
           "test_plan.md", path=tmp)
    st = status("H-20260101a", tmp)
    chk("创建后 ACTIVE", st["kind"] == "ACTIVE")
    # 2 重复创建拒绝
    try:
        create("H-20260101a", "again", "m", "w", "w", "i", "p", path=tmp)
        chk("重复创建被拒", False)
    except LedgerError:
        chk("重复创建被拒", True)
    # 3 未立账失效拒绝
    try:
        invalidate("H-20260101z", "e", "t", path=tmp)
        chk("未立账失效被拒", False)
    except LedgerError:
        chk("未立账失效被拒", True)
    # 4 失效
    invalidate("H-20260101a", "触发失效条件Y", "intraday", path=tmp)
    chk("失效后 INVALIDATED", status("H-20260101a", tmp)["kind"] == "INVALIDATED")
    # 5 失效后依赖开仓被拒
    try:
        assert_tradeable("H-20260101a", tmp)
        chk("失效H禁止开仓依据", False)
    except LedgerError:
        chk("失效H禁止开仓依据", True)
    # 6 重复失效拒绝
    try:
        invalidate("H-20260101a", "again", "t", path=tmp)
        chk("重复失效被拒", False)
    except LedgerError:
        chk("重复失效被拒", True)
    # 7 失效后仍可结算（归因不丢）
    settle("H-20260101a", "REFUTED", "实际数据", "actual=-5%", path=tmp)
    st = status("H-20260101a", tmp)
    chk("失效后结算=SETTLED+was_invalidated", st["kind"] == "SETTLED" and st["was_invalidated"])
    # 8 结论不可改
    try:
        settle("H-20260101a", "SUPPORTED", "改口", "x", path=tmp)
        chk("结算后改结论被拒", False)
    except LedgerError:
        chk("结算后改结论被拒", True)
    # 9 非法 verdict 拒绝
    try:
        settle("H-20260101b", "MAYBE", "e", "a", path=tmp)
        chk("非法verdict被拒", False)
    except LedgerError:
        chk("非法verdict被拒", True)
    # 10 ACTIVE 可直接结算
    create("H-20260101c", "预测C", "m", "w", "w", "i", "p", path=tmp)
    settle("H-20260101c", "SUPPORTED", "e", "a", path=tmp)
    chk("ACTIVE直接结算", status("H-20260101c", tmp)["kind"] == "SETTLED")
    # 11 非法 verdict 值未被写入文件
    lines = open(tmp, encoding="utf-8").read().strip().split("\n")
    chk("append-only 行数=合法追加数(5)", len(lines) == 5)
    # 12 独立性：沙盒测试绝不写生产账本（生产账本存在时须不含测试标记）
    if os.path.exists(LEDGER):
        chk("生产账本未被触碰", all("test_plan.md" not in l
                                    for l in open(LEDGER, encoding="utf-8")))
    else:
        chk("生产账本未被触碰", True)
    return ok, fail


if __name__ == "__main__":
    _cli()
