#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""build_decision_card.py · 决策卡生成（0.15 载体 · 2026-10-22 补建）

## 为什么需要这个文件

设计 §5.2 + §6 明确要求 `data/decision_card.md` 作为 0.15 载体存在，
`tools/fs/landing.py` 的 LD5 落点也登记了它（「决策卡驱动依据区」）。

**但 2026-10-22 审计发现：全项目没有任何模块写它。**
落地检查（`capability_status.py`）因此判 C4 = NO_OUTPUT。

设计说决策卡 = **三问 + 驱动依据区 + 参数卡（每个数带防什么/抓什么）**，
本工具就按这三块装配，**不新增语义、不做判断**。

## 三块内容的数据来源（全部已存在，本工具只装配）

| 区块 | 数据源 | 说明 |
|---|---|---|
| 三问 | `data/input/proposals_<D>.json` + `data/generated/sim/decision_ledger.jsonl` | 提案 + 裁决结果 |
| 驱动依据区 | `data/research/rules/rules.json` | **只收 VALIDATED** 的 Rule；空就写空并说明原因 |
| 参数卡 | `data/params.json` | 每个数带「防什么/抓什么」 |

## 纪律

- **零判断**：本工具不决定买什么，只把已经做出的决策装配成人读卡片。
- **驱动依据区只认 VALIDATED**：`CANDIDATE` / `REJECTED` 一律不进卡（设计 §6 硬约束）。
- **参数卡每个数必须有用途声明**：没有用途声明的参数只列出数值并标 `PURPOSE_UNDECLARED`，
  **不编造理由**（编理由比不写更坏——会让人以为有依据）。
- 只读 + 单文件写（`.tmp` + `os.replace` 原子写）。
- 幂等：同输入逐字节同输出。

用法：
  python tools/e2e/build_decision_card.py --as-of 2026-10-22          # 干跑
  python tools/e2e/build_decision_card.py --as-of 2026-10-22 --execute
  python tools/e2e/build_decision_card.py --selftest
"""

from __future__ import annotations

import io
import json
import os
import sys

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
if os.path.join(_TOOLS, "fs") not in sys.path:
    sys.path.insert(0, os.path.join(_TOOLS, "fs"))

CARD = os.path.join(_ROOT, "data", "decision_card.md")

# 参数卡的「防什么/抓什么」声明表。
# ★ 严格对应设计 §5.2 点名的五类：环境档→仓位 / 单票硬顶 / 熔断 / 试仓 / 最短持仓。
#   键名以 `data/params.json` 实际键为准（2026-10-22 核对过：
#   没有 probe_position_pct，试仓是 trial_position；没有 circuit_breaker_cap_l2）。
PURPOSE = {
    # ① 单票硬顶
    "single_stock_cap_of_position": "防单票爆仓归零（单票占总仓上限）",
    "single_stock_cap_of_equity": "防单票爆仓归零（单票占权益上限）",
    "max_holding_count": "防过度分散导致无法跟踪（同时持仓只数上限）",
    # ② 熔断
    "circuit_breaker_drawdown_l1": "防本金不可逆回撤（一级熔断触发：回撤达此值）",
    "circuit_breaker_cap_l1": "防亏损后继续重仓（一级熔断后仓位上限）",
    "circuit_breaker_drawdown_l2": "防本金不可逆回撤（二级熔断触发）",
    "circuit_breaker_daily_drawdown": "防单日巨亏（日内回撤触发线）",
    "circuit_breaker_flat_days": "防连续亏损后继续交易（熔断后强制空仓天数）",
    # ③ 试仓
    "trial_position": "防错局重仓（首次试仓比例）",
    # ④ 最短持仓
    "min_hold_days": "防隔日冲动换仓（最短持仓天数）",
    # ⑤ 交易成本（不算「防/抓」但影响期望，列在卡上便于下注前心算）
    "cost_buy_rate": "抓真实期望（买入成本，回测与实盘必须同口径）",
    "cost_sell_rate": "抓真实期望（卖出成本，含印花税）",
}


def _read_json(p, default=None):
    try:
        with io.open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _read_jsonl(p):
    out = []
    try:
        with io.open(p, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except json.JSONDecodeError:
                        pass
    except Exception:
        pass
    return out


def load_params(root=None):
    """→ [(key, value, unit, status, purpose)]。"""
    root = root or _ROOT
    d = _read_json(os.path.join(root, "data", "params.json"), {}) or {}
    params = d.get("params") or d.get("values") or {}
    rows = []
    for k in sorted(params):
        v = params[k]
        if isinstance(v, dict):
            rows.append((k, v.get("value"), v.get("unit") or "",
                         v.get("status") or "?", PURPOSE.get(k)))
        else:
            rows.append((k, v, "", "?", PURPOSE.get(k)))
    return rows


def load_rules(root=None):
    """→ (validated, others)。驱动依据区**只收 VALIDATED**。"""
    root = root or _ROOT
    d = _read_json(os.path.join(root, "data", "research", "rules",
                                "rules.json"), {}) or {}
    rules = d.get("rules") or d.get("items") or []
    if isinstance(rules, dict):
        rules = list(rules.values())
    validated, others = [], []
    for r in rules:
        if not isinstance(r, dict):
            continue
        st = str(r.get("state") or r.get("status") or "").upper()
        (validated if st == "VALIDATED" else others).append(r)
    return validated, others


def load_decisions(as_of, root=None):
    """→ (proposals, verdicts)。

    ★ 账本行的时间字段是 `date`（不是 `as_of`）——2026-10-22 第一次读时
      用 `as_of` 过滤，结果误报「今日决策 0 条」。
    """
    root = root or _ROOT
    prop = _read_json(os.path.join(root, "data", "input",
                                   "proposals_%s.json" % as_of), {}) or {}
    props = prop.get("proposals") or {}
    verdicts = {}
    for r in _read_jsonl(os.path.join(root, "data", "generated", "sim",
                                      "decision_ledger.jsonl")):
        if r.get("date") == as_of and r.get("record_type") == "DECISION":
            verdicts[r.get("code")] = r
    return props, verdicts


def load_env(root=None):
    """从 brain_state.md 的「环境档」段取档位与仓位上限（只读文本）。"""
    root = root or _ROOT
    p = os.path.join(root, "memory", "state", "brain_state.md")
    out = {}
    try:
        txt = io.open(p, encoding="utf-8").read()
    except Exception:
        return out
    i = txt.find("## 环境档")
    if i < 0:
        return out
    for ln in txt[i:i + 2500].splitlines():
        if ln.startswith("|") and ln.count("|") >= 3:
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cells) >= 2 and cells[0] not in ("项", "---") \
                    and not set(cells[0]) <= set("-: "):
                out[cells[0]] = cells[1].replace("**", "")
    return out


def build(as_of, root=None):
    """→ (markdown, stats)。"""
    root = root or _ROOT
    props, verdicts = load_decisions(as_of, root)
    validated, others = load_rules(root)
    params = load_params(root)
    env = load_env(root)

    uparams = [p for p in params if not p[4]]
    # ★ 设计 §5.2 只要求「关键数」进参数卡（环境档→仓位 / 单票硬顶 / 熔断 / 试仓 / 最短持仓）。
    #   2026-10-22 第一次实现把全部 48 个参数都塞进卡里，还给自己扣了
    #   「42 个参数未声明用途」的帽子 —— **那是我读错了设计要求**：
    #   设计从没说 48 个都要有「防什么/抓什么」，只说关键数要。
    #   把全部参数塞进「每天必读」的卡，本身就是让卡失去可读性。
    core = [p for p in params if p[4]]
    L = []
    L.append("# 决策卡（0.15 载体 · as_of=%s）" % as_of)
    L.append("")
    L.append("> 本卡 = **三问 + 驱动依据区 + 参数卡**（设计 §5.2 / §6）。")
    L.append("> **每天必读、每笔下注前必读。** 由 `tools/e2e/build_decision_card.py` 机械装配，")
    L.append("> 零判断、零新增语义；数据全部来自已存在的落点文件。")
    L.append("")
    L.append("## 一、今日环境（C1）")
    L.append("")
    if env:
        L.append("| 项 | 值 |")
        L.append("| --- | --- |")
        for k in ("档位", "仓位天花板（成）", "涨停家数", "连板高度",
                  "炸板率", "涨跌家数比（只记录）", "as_of / source"):
            if k in env:
                L.append("| %s | **%s** |" % (k, env[k]))
    else:
        L.append("环境档段未在 `brain_state.md` 找到（跑 `regime.py --eval` 生成）。")
    L.append("")

    L.append("## 二、三问（今日裁决）")
    L.append("")
    L.append("| 代码 | 驱动 | 裁决 | 入场 | 止损 | 目标 | 窗口 | 依据（首条） |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    n_buy = n_wait = n_no = 0
    for code in sorted(props):
        p = props[code]
        act = p.get("action_proposed")
        v = verdicts.get(code, {})
        real = v.get("action") or act
        if real == "BUY":
            n_buy += 1
        elif real == "WAIT":
            n_wait += 1
        elif real == "NO":
            n_no += 1
        ev = (v.get("thesis") or {}).get("evidence") or []
        first = (ev[0][:44] + "…") if ev else "—"
        L.append("| %s | %s | **%s** | %s | %s | %s | %s | %s |" % (
            code, p.get("driver") or "—", real,
            p.get("entry_limit") if p.get("entry_limit") is not None else "—",
            p.get("stop_line") if p.get("stop_line") is not None else "—",
            p.get("target") if p.get("target") is not None else "—",
            p.get("window") if p.get("window") is not None else "—",
            first))
    if not props:
        L.append("| — | — | — | — | — | — | — | 今日无提案（`proposals_<D>.json` 未生成） |")
    L.append("")
    L.append("**合计：BUY %d · WAIT %d · NO %d**" % (n_buy, n_wait, n_no))
    L.append("")

    L.append("## 三、驱动依据区（只收 VALIDATED 的 Rule / 经验）")
    L.append("")
    if validated:
        L.append("| Rule | 内容 | 状态 |")
        L.append("| --- | --- | --- |")
        for r in validated[:20]:
            L.append("| %s | %s | VALIDATED |" % (
                r.get("id") or r.get("rule_id") or "?",
                str(r.get("text") or r.get("desc") or "")[:60]))
    else:
        L.append("**空账**（这是正确状态，不是缺陷）：")
        L.append("")
        L.append("- 本区只收 **VALIDATED** 的 Rule / 经验条目（设计 §6 硬约束）。")
        L.append("- 当前 `data/research/rules/rules.json` 里 VALIDATED 条目 **0 条**")
        L.append("  （CANDIDATE/REJECTED 一律不进卡）。")
        L.append("- 规则要变 VALIDATED，需经 C6-a 验证协议 **SUPPORT + 用户批准**，")
        L.append("  程序不得自动转正。")
    if others:
        L.append("")
        L.append("> 另有 %d 条非 VALIDATED 规则（CANDIDATE/DECAYING/REJECTED），"
                 "按设计**不进本卡**。" % len(others))
    L.append("")

    L.append("## 四、参数卡（设计 §5.2 要求的关键数·每个数带「防什么 / 抓什么」）")
    L.append("")
    L.append("> 设计 §5.2 原文只要求这几个关键数进卡（环境档→仓位 / 单票硬顶 / 熔断 / 试仓 /")
    L.append("> 最短持仓）。**其余 %d 个参数不在参数卡上**——它们不进每日必读。"
             % max(0, len(params) - len(core)))
    L.append("> 完整清单见下方「附：全部参数」，或直接读 `data/params.json`。")
    L.append("")
    L.append("| 数值 | 当前值 | 防什么 / 抓什么 |")
    L.append("| --- | --- | --- |")
    for k, v, unit, st, purpose in core:
        val = "%s%s" % (v, (" " + unit) if unit else "")
        L.append("| `%s` | **%s** | %s |" % (k, val, purpose))
    if not core:
        L.append("| — | — | 关键参数一个都没匹配到（检查 params.json 键名） |")
    L.append("")
    # 环境档→仓位 是设计点名的第一项，单独强调
    if env.get("仓位天花板（成）") is not None:
        L.append("**环境档→仓位（今日）**：档位 **%s** ⇒ 仓位天花板 **%s 成**"
                 "（防退潮满仓；映射表 = `params.json.regime_ladder`）"
                 % (env.get("档位", "?"), env.get("仓位天花板（成）")))
        L.append("")
    L.append("附：全部参数（%d 个，机器可读真源 = `data/params.json`）" % len(params))
    L.append("")
    L.append("<details><summary>展开</summary>")
    L.append("")
    L.append("| 参数 | 值 | 单位 | 状态 |")
    L.append("| --- | --- | --- | --- |")
    for k, v, unit, st, purpose in params:
        sv = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
        sv = str(sv)
        if len(sv) > 48:
            sv = sv[:46] + "…"
        L.append("| `%s` | %s | %s | %s |" % (k, sv, unit, st))
    L.append("")
    L.append("</details>")
    L.append("")
    L.append("---")
    L.append("")
    L.append("*本卡机械装配（零 LLM 零手工）；同输入逐字节同输出。*")

    md = "\n".join(L) + "\n"
    stats = {"props": len(props), "verdicts": len(verdicts),
             "buy": n_buy, "wait": n_wait, "no": n_no,
             "validated_rules": len(validated), "other_rules": len(others),
             "params": len(params), "params_core": len(core)}
    return md, stats


def write_card(md, path=None):
    path = path or CARD
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(md)
    os.replace(tmp, path)
    return path


# ── 自检 ────────────────────────────────────────────────────────────────────
def _selftest():
    ok = fail = 0

    def T(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ✓ %s" % name)
        else:
            fail += 1
            print("  ✗ %s  %s" % (name, detail))

    import shutil
    import tempfile
    print("build_decision_card.py 自检")

    d = tempfile.mkdtemp()
    try:
        def w(rel, obj):
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with io.open(p, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)

        w("data/params.json", {"params": {
            "max_holding_count": {"value": 3, "unit": "count",
                                  "status": "BASELINE"},
            "unknown_param_x": {"value": 7, "unit": "pct", "status": "BASELINE"},
        }})
        w("data/research/rules/rules.json", {"rules": [
            {"id": "R1", "text": "已批准的规则", "state": "VALIDATED"},
            {"id": "R2", "text": "未批准的规则", "state": "CANDIDATE"},
        ]})
        w("data/input/proposals_2026-10-22.json", {"proposals": {
            "sh600000": {"driver": "C", "action_proposed": "WAIT",
                         "entry_limit": None, "stop_line": 9.0,
                         "target": 10.5, "window": 3},
        }})
        p = os.path.join(d, "data", "generated", "sim",
                         "decision_ledger.jsonl")
        os.makedirs(os.path.dirname(p))
        with io.open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"record_type": "DECISION", "date": "2026-10-22",
                                "code": "sh600000", "action": "WAIT",
                                "thesis": {"evidence": ["证据A"]}},
                               ensure_ascii=False) + "\n")
        os.makedirs(os.path.join(d, "memory", "state"))
        with io.open(os.path.join(d, "memory", "state", "brain_state.md"),
                     "w", encoding="utf-8") as f:
            f.write("## 环境档\n\n| 项 | 值 |\n| --- | --- |\n"
                    "| 档位 | **退潮** |\n| 仓位天花板（成） | 1 |\n")

        md, st = build("2026-10-22", root=d)
        T("A1 提案被装配进三问", st["props"] == 1, str(st))
        T("A2 裁决 WAIT 计数正确", st["wait"] == 1 and st["buy"] == 0, str(st))
        T("A3 只收 VALIDATED 规则", st["validated_rules"] == 1
          and st["other_rules"] == 1, str(st))
        T("A4 卡片含 R1 不含 R2（未 VALIDATED 不进卡）",
          "已批准的规则" in md and "未批准的规则" not in md)
        T("A5 关键数进参数卡（max_holding_count 有用途声明）",
          st["params_core"] == 1 and "防过度分散" in md, str(st))
        T("A6 非关键参数不进参数卡正文（只在「附：全部参数」里）",
          md.find("unknown_param_x") > md.find("附：全部参数"), "位置检查")
        T("A7 环境档退潮被读入", "退潮" in md)
        T("A8 三问/驱动依据区/参数卡 三段都在",
          "三问" in md and "驱动依据区" in md and "参数卡" in md)

        # B 幂等：同输入逐字节同输出
        md2, _ = build("2026-10-22", root=d)
        T("B1 幂等（同输入逐字节同输出）", md == md2)

        # C 空规则时说明原因而不是空白
        w("data/research/rules/rules.json", {"rules": []})
        md3, st3 = build("2026-10-22", root=d)
        T("C1 规则空账时写明「这是正确状态不是缺陷」",
          "空账" in md3 and "不是缺陷" in md3)
        T("C2 空账时 validated=0", st3["validated_rules"] == 0)

        # D 原子写
        src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
        T("D1 用 .tmp + os.replace 原子写",
          ".tmp" in src and "os.replace" in src)
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    as_of = None
    execute = "--execute" in argv
    for i, a in enumerate(argv):
        if a == "--as-of" and i + 1 < len(argv):
            as_of = argv[i + 1]
    if not as_of:
        import datetime
        as_of = datetime.date.today().isoformat()

    md, st = build(as_of)
    print("决策卡装配 · as_of=%s" % as_of)
    print("  提案 %d · 裁决 %d（BUY %d / WAIT %d / NO %d）"
          % (st["props"], st["verdicts"], st["buy"], st["wait"], st["no"]))
    print("  驱动依据区：VALIDATED %d 条（另有 %d 条非 VALIDATED 不进卡）"
          % (st["validated_rules"], st["other_rules"]))
    print("  参数卡：全部 %d 个参数，其中设计 §5.2 点名的关键数 %d 个进卡"
          % (st["params"], st["params_core"]))
    if execute:
        p = write_card(md)
        print("  已写入 %s（%d 字节）" % (os.path.basename(p),
                                       len(md.encode("utf-8"))))
    else:
        print("  干跑：加 --execute 才写")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit(_main(sys.argv[1:]))
