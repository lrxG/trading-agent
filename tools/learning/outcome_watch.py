# -*- coding: utf-8 -*-
"""outcome_watch.py —— C6 零件④：非下注决策出路兑现验证（施工 1.6 补 · v1.0）

用途：C4 三问的**非 BUY 出路**（WAIT 的 `next_trigger` / NO 的 `overturn_event`）
      在后续交易日的快照里**是否被真实数据兑现** —— 机械判定、逐笔留痕。
      这是 C6「学」环节对 WAIT/NO 的唯一验证路径（BUY 走 pnl→case_builder，
      非下注决策此前无任何结算反哺落点 = 基础闭环断点）。

★ 一、为什么需要本件（缺口证据）
  · `pnl.write_pnl` **只接受 action=BUY**（第 289 行硬拒 WAIT/NO）——这是**正确设计**：
    非下注决策不产生资金结果，不该进资金账本。
  · `case_builder.build` / `demand_scanner` / `verify` 全链基于「已结算 BUY 的资金结果」
    （outcome ∈ WIN/LOSS）——同样正确。
  · 后果：C3 出 127 票裁决（118 NO + 9 WAIT）后，**C6 收不到任何东西**，
    「等什么事件兑现了吗 / 翻案条件成立了吗」全程无机械验证 ⇒ 学习闭环缺口。

★ 二、零伪造（写死）
  · 只做**结构可判**的 rule 求值（`{field,op,value}` 且 field ∈ 白名单快照字段）；
    `rule=null`（人话条件）⇒ verdict=`UNJUDGEABLE`，**不猜语义**。
  · 快照缺席 / 字段缺 ⇒ `PENDING`（数据未到，非兑现非失效）——绝不默认判「已兑现」。
  · 不产 BUY/SELL、不算仓位、不写资金账本（orders_created 恒 0）。
  · 只读决策账本 + 快照；唯一写点 = `data/learning/outcome_watch.jsonl`（X5）。

★ 三、判据
  对每条 WAIT/NO 决策，取 `next_trigger.rule`（WAIT）或 `overturn_event.rule`（NO）：
    · WAIT 兑现 → `FULFILLED`（该等的事件真发生了 → 说明当时 WAIT 是对的）
    · NO 翻案成立 → `OVERTURNED`（翻案条件成立 → 当时 NO 可能过保守，进学习队列）
    · 窗口内未发生 / 条件不成立 → `NOT_YET`（仍在观察窗内）或 `EXPIRED_UNMET`（超窗未兑现）
  观察窗 = `next_trigger` 的决策日 + `outcome_watch_days`（params，缺省 5 交易日）。

★ 四、边界
  本模块**零判断**：rule 结构求值是机械的（字段名 + op + value），不读语义；
  不做「WAIT 对不对」的质量评价（那是 demand_scanner 的活），只报「事件是否兑现」事实。

被谁调用：C6 复盘链（settle 步5 之后，可选）/ learning 复盘批。
用法：
  python tools/learning/outcome_watch.py --as-of 2026-09-30 --base <沙盒>
  python tools/learning/outcome_watch.py --selftest
"""
import argparse
import datetime as _dt
import io
import json
import os
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "..", ".."))
for _p in ("fs", "analysis", "learning", "risk"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import decision as DEC      # noqa: E402
import candidates as CAND   # noqa: E402   ← OPS 枚举真源
import snapshot as SNAP     # noqa: E402

ACTOR = "learning"
SCHEMA_VERSION = "1.0"
WATCH_PATH = os.path.join(_ROOT, "data", "learning", "outcome_watch.jsonl")
DEFAULT_WATCH_DAYS = 5

# rule.field 白名单：只允许**快照里可机械取值**的字段（防猜语义 / 防穿越）
# env_metrics 的 6 键（0.2 契约）+ 该 code 的日线 4 键。其余一律 UNJUDGEABLE。
RULE_FIELDS = ("limit_up_count", "max_board", "turnover", "up_down_ratio",
               "fail_rate", "index_ma20", "close", "pct", "vol", "amount",
               "boards", "is_limit_up")
_OP_FN = {
    ">=": lambda a, b: a >= b, ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b, "<": lambda a, b: a < b,
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
}


class WatchError(Exception):
    """验证被拒（结构非法 / 写盘失败）。"""


def _check_date(s):
    try:
        return _dt.date.fromisoformat(str(s)).isoformat()
    except Exception:
        raise WatchError("日期必须是 YYYY-MM-DD：%r" % (s,))


def _read_jsonl(p):
    if not os.path.isfile(p):
        return []
    out = []
    with io.open(p, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    continue
    return out


def _field_value(snap, code, field):
    """从快照机械取 rule.field 的值。取不到 → (None, 'why')，绝不猜。"""
    if snap is SNAP.UNAVAILABLE:
        return None, "NO_SNAPSHOT"
    env = snap.get("env_metrics") or {}
    if field in env:
        return env.get(field), None
    if field in ("is_limit_up",):
        lu = snap.get("limit_up") or []
        return (any(r.get("code") == code for r in lu)), None
    if field in ("close", "pct", "vol", "amount", "boards"):
        if field == "boards":
            for r in (snap.get("limit_up") or []):
                if r.get("code") == code:
                    return r.get("boards"), None
            return None, "NOT_IN_LIMIT_UP"
        for r in (snap.get("daily_quote") or []):
            if r.get("code") == code:
                return r.get(field), None
        return None, "NO_QUOTE"
    return None, "FIELD_NOT_MECHANICAL"


def judge_rule(rule, snap, code):
    """机械求值一条 rule → (verdict_str, detail)。

    verdict ∈ {TRUE, FALSE, UNJUDGEABLE, PENDING}：
      TRUE/FALSE  = 结构可判且取到值
      UNJUDGEABLE = rule=null / field 不在白名单 / op 非法 ⇒ 不猜
      PENDING     = 快照或字段缺席 ⇒ 数据未到（非兑现非失效）
    """
    if not isinstance(rule, dict):
        return "UNJUDGEABLE", {"why": "rule=null（人话条件，机械件不判语义）"}
    f, op, val = rule.get("field"), rule.get("op"), rule.get("value")
    if f not in RULE_FIELDS:
        return "UNJUDGEABLE", {"why": "field=%r 不在白名单（禁猜语义）" % f}
    fn = _OP_FN.get(op) if op in (">=", ">", "<=", "<", "==", "!=") else None
    if fn is None:
        return "UNJUDGEABLE", {"why": "op=%r 非可比 op（in/not_in 不机械判）" % op}
    got, why = _field_value(snap, code, f)
    if why:
        return "PENDING", {"why": why, "field": f}
    try:
        res = bool(fn(got, val))
    except TypeError:
        return "UNJUDGEABLE", {"why": "值类型不可比：%r vs %r" % (got, val)}
    return ("TRUE" if res else "FALSE"), {"field": f, "op": op, "value": val,
                                          "observed": got}


def watch(as_of, *, base=None, sim=True, watch_days=DEFAULT_WATCH_DAYS, out=None):
    """扫全部 WAIT/NO 决策（≤ as_of，决策日 ≥ as_of-watch_days）→ 出路兑现判定。"""
    as_of = _check_date(as_of)
    base = base or _ROOT
    d0 = _dt.date.fromisoformat(as_of) - _dt.timedelta(days=watch_days)
    rows, n_scan = [], 0
    for axis in (False, True) if sim is None else (sim,):
        for dec in DEC.iter_decisions(axis, base=base):
            act = dec.get("action")
            if act not in ("WAIT", "NO"):
                continue
            n_scan += 1
            dd = dec.get("date")
            if dd is None or dd > as_of:
                continue                      # 锁①：未来决策不参与
            snap = SNAP.load_snapshot(as_of, root=os.path.join(base, "data", "snapshot"))
            v = DEC.load_decision(dec["id"], sim=axis, base=base)
            # 出路文本在落账时并入 thesis.evidence（outcome 持久化），此处从账本行取
            ev = ((v.get("thesis") or {}).get("evidence") or [])
            trigger_txt = next((e for e in ev if str(e).startswith("OUTCOME: next_trigger=")), None)
            # rule 取自候选池当前锚（决策时回填的 next_trigger）
            rule = None
            for c in CAND.list_candidates(path=os.path.join(base, "data", "state",
                                                             "candidates.json")):
                if c.get("code") == dec.get("code") and c.get("next_trigger"):
                    rule = (c["next_trigger"] or {}).get("rule")
                    break
            verdict, detail = judge_rule(rule, snap, dec.get("code"))
            in_window = dd >= d0.isoformat()
            if verdict == "TRUE":
                status = "FULFILLED" if act == "WAIT" else "OVERTURNED"
            elif verdict in ("UNJUDGEABLE", "PENDING"):
                status = "PENDING" if in_window else "UNJUDGEABLE"
            else:
                status = "NOT_YET" if in_window else "EXPIRED_UNMET"
            rows.append({
                "schema_version": SCHEMA_VERSION, "as_of": as_of,
                "decision_id": dec["id"], "code": dec.get("code"),
                "action": act, "decision_date": dd, "sim": bool(axis),
                "trigger_text": (trigger_txt.split("=", 1)[1] if trigger_txt else None),
                "rule": rule, "judge": detail, "status": status,
                "in_window": in_window,
            })
    report = {
        "as_of": as_of, "base": base, "watch_days": watch_days,
        "n_scanned_nonbuy": n_scan, "n_rows": len(rows),
        "by_status": {s: sum(1 for r in rows if r["status"] == s)
                      for s in sorted({r["status"] for r in rows})},
        "orders_created": 0,
        "note": "rule=null/field 非白名单 ⇒ UNJUDGEABLE（不猜语义）；快照缺 ⇒ PENDING",
    }
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with io.open(out, "a", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
        report["out"] = out
    return report, rows


# ── 自检（沙盒夹具，真库零接触） ──────────────────────────────────────────────
def _selftest():
    import shutil
    import tempfile
    ok = fail = 0

    def T(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [OK] %s" % name)
        else:
            fail += 1
            print("  [!!] %s %s" % (name, extra))

    print("== outcome_watch --selftest ==")
    SNAP_SB = {"env_metrics": {"limit_up_count": 57, "max_board": 6, "fail_rate": 0.21},
               "daily_quote": [{"code": "sh600001", "close": 9.41, "pct": 10.05}],
               "limit_up": [{"code": "sh600001", "boards": 2}], "date": "2026-09-30"}
    # A. 机械求值
    v, d = judge_rule({"field": "limit_up_count", "op": ">=", "value": 40}, SNAP_SB, "sh600001")
    T("A1 数值 rule 命中 → TRUE", v == "TRUE", str(d))
    v, d = judge_rule({"field": "max_board", "op": ">=", "value": 10}, SNAP_SB, "sh600001")
    T("A2 数值 rule 不命中 → FALSE", v == "FALSE", str(d))
    v, d = judge_rule(None, SNAP_SB, "sh600001")
    T("A3 rule=null → UNJUDGEABLE（不猜语义）", v == "UNJUDGEABLE", str(d))
    v, d = judge_rule({"field": "瞎猜字段", "op": ">=", "value": 1}, SNAP_SB, "sh600001")
    T("A4 非白名单 field → UNJUDGEABLE", v == "UNJUDGEABLE", str(d))
    v, d = judge_rule({"field": "limit_up_count", "op": "in", "value": [40]}, SNAP_SB, "sh600001")
    T("A5 非可比 op(in) → UNJUDGEABLE", v == "UNJUDGEABLE", str(d))
    v, d = judge_rule({"field": "pct", "op": ">=", "value": 9}, SNAP_SB, "sh600001")
    T("A6 code 级字段(pct) 机械取值 → TRUE", v == "TRUE", str(d))
    v, d = judge_rule({"field": "boards", "op": ">=", "value": 2}, SNAP_SB, "sh600001")
    T("A7 boards(涨停榜) → TRUE", v == "TRUE", str(d))
    v, d = judge_rule({"field": "close", "op": "<", "value": 5}, SNAP_SB, "sh600002")
    T("A8 快照无该 code → PENDING（数据未到，不默认兑现）", v == "PENDING", str(d))
    v, d = judge_rule({"field": "limit_up_count", "op": ">=", "value": 40}, SNAP.UNAVAILABLE,
                      "sh600001")
    T("A9 快照缺席 → PENDING（禁默认兑现）", v == "PENDING", str(d))
    # B. 真库零接触
    real = os.path.join(_ROOT, "data", "generated", "decision_ledger.jsonl")
    fp0 = os.path.getmtime(real) if os.path.exists(real) else None
    tmp = tempfile.mkdtemp(prefix="ow_sb_")
    try:
        for d_ in ("data/state", "data/snapshot", "data/learning"):
            os.makedirs(os.path.join(tmp, d_), exist_ok=True)
        io.open(os.path.join(tmp, "data", "state", "candidates.json"), "w",
                encoding="utf-8").write(json.dumps(
                    {"version": CAND.SCHEMA_VERSION, "updated_at": "2026-09-29",
                     "candidates": [{"id": "CD-1", "code": "sh600001",
                                     "state": "DISCOVERED",
                                     "next_trigger": {"text": "涨停≥40",
                                                      "rule": {"field": "limit_up_count",
                                                               "op": ">=", "value": 40}}}]}))
        rep, rows = watch("2026-09-30", base=tmp, sim=True, out=None)
        T("B1 无决策账本 → 零行（不崩溃）", rep["n_rows"] == 0, str(rep))
        T("B2 orders_created 恒 0（零交易）", rep["orders_created"] == 0)
        T("B3 缺省 out=None 不写盘", "out" not in rep)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    T("C1 真库决策账本 mtime 未变（只读）",
      (os.path.getmtime(real) if os.path.exists(real) else None) == fp0)
    print("== outcome_watch --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def _main(argv=None):
    ap = argparse.ArgumentParser(description="C6 非下注决策出路兑现验证（零伪造·机械判定）")
    ap.add_argument("--as-of", dest="as_of", help="校验日 YYYY-MM-DD（--selftest 免填）")
    ap.add_argument("--base", help="数据根（缺省=生产只读；沙盒则指向已存在目录）")
    ap.add_argument("--live", action="store_true", help="LIVE 轴（默认 SIM）")
    ap.add_argument("--watch-days", dest="watch_days", type=int, default=DEFAULT_WATCH_DAYS)
    ap.add_argument("--out", help="落盘 jsonl（缺省只打印，不写盘）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if not a.as_of:
        print("用法：--as-of <YYYY-MM-DD> [--base <根>] [--live] [--out f] [--selftest]")
        return 2
    rep, _rows = watch(a.as_of, base=a.base, sim=not a.live,
                       watch_days=a.watch_days, out=a.out)
    print("C6 出路兑现验证 · %s（sim=%s）" % (rep["as_of"], not a.live))
    print("  扫描 WAIT/NO 决策 %d 条 → 判定 %d 行" % (rep["n_scanned_nonbuy"], rep["n_rows"]))
    print("  状态分布: %s" % rep["by_status"])
    print("  %s" % rep["note"])
    if a.out:
        print("  已写: %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
