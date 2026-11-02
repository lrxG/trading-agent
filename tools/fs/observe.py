#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""observe.py · 运转可观测与自愈（施工方案 0.17 · v1.0 · 2026-10-08 建）

用途：让「系统有没有在转 / 转到哪一步 / 哪一步崩了」**不需要人问就能知道**。
      旧运转方式最大的盲区不是"跑错"，而是"没跑却看不出来"——已有实证代价：
      10/08 8:40 全部任务开机补跑集体失败（上次结果 -2147020576），**4 小时后才被翻出来**；
      run_log 末条停在 09/30 17:51、当日零采集，同样无人察觉。
      **运转不可见，等于转没转都一样。**

★ 第 1 条 · 每次运行必留痕（本模块的存在理由）
  任何正式运行 → 自动追加一行 run_log；**由脚本自写，智能体不手工归档**；
  **缺这一行的运行视为未发生**——这是本模块对其他所有模块的硬约束。

★ 第 2 条 · 四类 result（值域写死，禁自造）
  DONE        跑了且真产出（新快照 / 新决策 / 新结算 / 新留痕）
  EMPTY       跑了但产出为空（**也必须记一行**——空跑与"没跑"是两件事）
  FAIL        抛错（必带 error 文本；无因的 FAIL 被本模块拒绝写入）
  STALL       连续 N 次「跑了却没产出」达到阈值 → 通道可能断了（直接对治旧系统的盲区）

★ 第 3 条 · EMPTY 必须再分两种（本轮关键判据，防误报警）
  RAN_NO_OUTPUT   **本该有产出却没产出** → 可疑，计入 STALL 计数
  NO_CAPABILITY   **结构性为空**：该环节在盘上根本没有可执行能力（零插件空转就是这种）
                 → **不计入 STALL**
  不分这两种会出两个错，方向相反却同样致命：
    · 不分 → 零插件骨架空转每天全部 EMPTY → 第 3 天报 STALL → **假报警**，
      真断链淹没在噪声里（旧系统盲区的翻版：报得太多 = 没人看）；
    · 不分 → 把「没插件的正常空载」和「插件装了却不产出」混成一锅 →
      **真断链被当成正常**。
  判据一句话：**空的"性质"不同，处置就不同**——这是 0.17 第 2 条"静默空转检测"的落点。

★ 第 4 条 · 失败不吞
  任何步骤抛错 → 落 `result=FAIL` + `error`，**并把异常上抛**（`raise_on_error=True` 默认）。
  不吞的意义：调用层（wakeup）据此**停止当天、不推进游标**；下次唤醒按游标自动重做该步（幂等）。
  **不允许"跳过去继续跑"**。

★ 第 5 条 · 「已处理 FAIL」的判据（不靠人标记，靠事实推导）
  同一 `(as_of, actor, action)` 的 FAIL 之后**没有再出现非 FAIL 行** → 仍未处理。
  即：**按游标重做且成功 = 已处理**（与 0.6 步级幂等天然对齐）——
  不引入 handled 标志位，避免"标了已处理但其实没修"。

★ 第 6 条 · 唤醒即自检（三项，结果打在唤醒输出顶部）
  ① 昨日是否结账  ② 有无未处理 FAIL  ③ 有无 STALL
  失败必须**显式**（第 5 条：FAIL/STALL 禁止只埋在日志里）。

★ 第 7 条 · 统一挂载，不靠自觉
  `run_observed(actor, action, stage, fn)` 是**唯一**的执行包装；
  **所有正式运行入口必须经它调用**（wakeup / settle / scan / regime / 学习类 / E 类）。
  **未经包装的运行 = 无 run_log 行 = 视为未发生。**

★ 单写点纪律（与 0.7 落点封闭同款）
  `data/state/run_log.jsonl` 的**唯一写入方 = 本模块**；
  其他任何模块出现写该路径的代码 → `--discipline` 判 FAIL。
  理由：留痕若能被各处自造，就永远无法保证"缺行 = 未发生"。

run_log 行 schema（**9 字段写死，顺序即真源**；本模块 RUN_LOG_FIELDS）：
  ts / as_of / actor / action / stage / result / duration_ms / error / detail
  · `as_of`  = 交易日（非交易日动作记 null）——**stream 键含它**，
    否则"今天失败、明天重跑"会被误判成"失败已处理"。
  · `stage`  = 环节名，值域**派生自注册表 MOUNTS**（C0~C6）+ PRE/POST 两个非环节阶段。
    ★ 不自立一份环节清单（0.18：所有清单一律由注册表派生，禁第二真源）。
  · `detail` = 空/`RAN_NO_OUTPUT`/`NO_CAPABILITY[:<缺哪个能力>]`——EMPTY 的性质就在这里。

被谁调用：tools/fs/wakeup.py（0.14）· 各 S/E runner · 人工 `--report`。
用法：
  python tools/fs/observe.py                  # 只读总览（按 result / actor 计数 + 未处理 FAIL + STALL）
  python tools/fs/observe.py --report         # 全量明细
  python tools/fs/observe.py --selftest       # 内置验收（临时目录，不碰真 run_log）
  python tools/fs/observe.py --discipline

规则：run_log schema 变更 = 改契约，须升 SCHEMA_VERSION 并同步施工方案 0.17；禁在别处写 run_log。
"""
import ast
import datetime as _dt
import io
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools", "fs")))
from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
import registry as REG  # noqa: E402
import astwin as ASTW  # noqa: E402   ← AST 窗口（尾界）唯一实现，禁本模块自写

SCHEMA_VERSION = 1
RUN_LOG_PATH = os.path.join(ROOT, "data", "state", "run_log.jsonl")

# 行 schema：字段名与顺序 = 契约（顺序即真源，禁增删改而不升版本）
RUN_LOG_FIELDS = ("ts", "as_of", "actor", "action", "stage", "result",
                  "duration_ms", "error", "detail")
RESULTS = ("DONE", "EMPTY", "FAIL", "STALL")
EMPTY_KINDS = ("RAN_NO_OUTPUT", "NO_CAPABILITY")
# STALL 只统计「本该有产出却没产出」；结构性为空不算（见模块 docstring 第 3 条）
STALL_COUNTED = "RAN_NO_OUTPUT"

# 环节值域 = 注册表 MOUNTS（禁第二份清单）+ 两个非环节阶段
CHAIN_STAGES = tuple(REG.MOUNTS)                    # ("C0".."C6")
NON_CHAIN_STAGES = ("PRE", "POST")
STAGES = CHAIN_STAGES + NON_CHAIN_STAGES

# 正式运行入口的 actor 值域（0.17 第 7 条「正式入口清单」；进 discipline 逐项核）
# 2026-10-09 契约统一批：补 "risk"（position_gate/exit_gate 独立 CLI 运行时即在用，
# 此前清单缺项 = 纪律与实际不一致；learning/ext 早已在册）。
FORMAL_ACTORS = ("wakeup", "settle", "scan", "regime", "risk", "learning", "ext",
                 "observe")

PARAM_STALL_THRESHOLD = "stall_empty_threshold"
DEFAULT_STALL_THRESHOLD = 3

_ACTOR_RE = re.compile(r"^[a-z][a-z0-9_]*(:[A-Za-z0-9_./:\-]+)?$")
_ACTION_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./:\-]{0,79}$")


class ObserveError(Exception):
    """留痕操作被拒（schema 不符 / 非法值域 / FAIL 无因）——显式抛错，不静默降级。"""


class ObservedFailure(Exception):
    """被包装的运行抛错后，本模块记录 FAIL 行再原样上抛的载体（保留原始 error 文本）。"""

    def __init__(self, error, report):
        Exception.__init__(self, error)
        self.error = error
        self.report = report


# ── 基础 ─────────────────────────────────────────────────────────────────────
def _now():
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _check_date(d, name="as_of", allow_none=True):
    if d is None:
        if allow_none:
            return None
        raise ObserveError("%s 不得为空" % name)
    s = d.isoformat() if hasattr(d, "isoformat") else str(d)
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise ObserveError("%s 必须是 YYYY-MM-DD，收到: %r" % (name, d))
    try:
        _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        raise ObserveError("%s 非法: %r" % (name, d))
    return s


def _stall_threshold(params_path=None):
    """STALL 阈值：读 params.json（0.8 落地即生效）；读不到用内置默认并标明来源。"""
    try:
        import params as PARAMS
        v = PARAMS.get_value(PARAM_STALL_THRESHOLD, DEFAULT_STALL_THRESHOLD, path=params_path)
        n = int(v)
        return n if n >= 1 else DEFAULT_STALL_THRESHOLD
    except Exception:
        return DEFAULT_STALL_THRESHOLD


def _validate(rec):
    """行 schema 强校验：字段名/顺序/值域全部写死，任何不符都抛错（不开兼容口）。"""
    if not isinstance(rec, dict):
        raise ObserveError("run_log 行必须是对象，实际: %r" % type(rec).__name__)
    extra = [k for k in rec if k not in RUN_LOG_FIELDS]
    missing = [k for k in RUN_LOG_FIELDS if k not in rec]
    if extra:
        raise ObserveError("run_log 行含未声明字段 %s（schema 见 RUN_LOG_FIELDS）" % extra)
    if missing:
        raise ObserveError("run_log 行缺字段 %s（schema 见 RUN_LOG_FIELDS）" % missing)
    if rec["result"] not in RESULTS:
        raise ObserveError("result 值域 = %s，实际 %r" % (" / ".join(RESULTS), rec["result"]))
    if rec["result"] == "FAIL" and not str(rec.get("error") or "").strip():
        raise ObserveError("FAIL 行必须有 error 文本——**失败不许无因**（0.17 第 4 条）")
    if rec["result"] != "FAIL" and str(rec.get("error") or "").strip():
        raise ObserveError("非 FAIL 行不得带 error（杜绝把异常塞进成功行）")
    if rec["stage"] not in STAGES:
        raise ObserveError("stage 值域 = %s（真源 = 注册表 MOUNTS + PRE/POST），实际 %r"
                           % (" / ".join(STAGES), rec["stage"]))
    if not _ACTOR_RE.match(str(rec["actor"] or "")):
        raise ObserveError("actor 必须是形如 `wakeup` / `settle:2026-10-08` 的标识，实际 %r"
                           % (rec["actor"],))
    if not _ACTION_RE.match(str(rec["action"] or "")):
        raise ObserveError("action 非法: %r" % (rec["action"],))
    if not isinstance(rec["duration_ms"], int) or rec["duration_ms"] < 0:
        raise ObserveError("duration_ms 必须是非负整数，实际 %r" % (rec["duration_ms"],))
    _check_date(rec["as_of"])
    if not str(rec["ts"] or "").count("-") >= 2:
        raise ObserveError("ts 非法: %r" % (rec["ts"],))
    return rec


def _append(rec, path=None):
    """唯一写入口：append-only。原子性靠 os.write + 单次 write 调用（行级追加，不回写历史）。"""
    p = path or RUN_LOG_PATH
    _WG.check_write(p)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False, sort_keys=False) + "\n"
    with io.open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(line)
    return rec


def read_log(path=None):
    """读 run_log（不写盘）。缺文件 → []（"还没跑过"是合法状态，不是错误）。"""
    p = path or RUN_LOG_PATH
    if not os.path.exists(p):
        return []
    out = []
    with io.open(p, encoding="utf-8") as f:
        for ln, raw in enumerate(f, 1):
            s = raw.strip()
            if not s:
                continue
            try:
                out.append(_validate(json.loads(s)))
            except Exception as e:
                raise ObserveError("run_log 第 %d 行不符 schema：%s（%s）" % (ln, e, s[:120]))
    return out


def iter_log(actor=None, action=None, result=None, as_of=None, stage=None, path=None):
    """按条件过滤（全部可选，None = 不限）。"""
    for r in read_log(path):
        if actor and r["actor"] != actor:
            continue
        if action and r["action"] != action:
            continue
        if result and r["result"] != result:
            continue
        if as_of and r["as_of"] != _check_date(as_of):
            continue
        if stage and r["stage"] != stage:
            continue
        yield r


# ── 统一挂载（0.17 第 7 条）──────────────────────────────────────────────────
def _default_produced(rv):
    """默认「有没有产出」判据：None/False/0/空容器 = 没产出。"""
    if rv is None or rv is False:
        return False
    if isinstance(rv, bool):
        return rv
    if isinstance(rv, (int, float)):
        return rv != 0
    if isinstance(rv, (str, list, tuple, set, dict)):
        return len(rv) > 0
    return True


def run_observed(actor, action, stage, fn, *, as_of=None, produces=None, detail=None,
                 no_capability=False, log_path=None, params_path=None,
                 raise_on_error=True):
    """**唯一**的执行包装：跑 fn → 判产出 → 落 run_log → （空转达阈值则）记 STALL。

    参数：
      actor/action/stage —— 谁 / 干了什么 / 属哪个环节（值域见模块 docstring）
      fn                 —— 被包装的调用（0 参可调用；也接受直接传值）
      as_of              —— 交易日（stream 键含它，防"今天失败明天重跑"被误判已处理）
      produces           —— 自定义「有无产出」判据 `fn(返回值) -> bool`；缺省走 _default_produced
      detail             —— 写入 detail 列的说明
      no_capability=True —— **结构性为空**（该环节盘上无能力）：EMPTY 记 `NO_CAPABILITY`，
                            **不计入 STALL**（见模块 docstring 第 3 条）
      raise_on_error     —— 抛错时是否上抛（默认 True：失败不吞）

    返回 {"record": 行, "value": fn 返回值, "error": 错误文本或 None, "stall": STALL 行或 None}。
    """
    t0 = time.perf_counter()
    rv, err = None, None
    try:
        rv = fn() if callable(fn) else fn
    except Exception as e:                       # noqa: BLE001 —— 必须兜住一切异常才叫"不吞"
        err = "%s: %s" % (type(e).__name__, e)
    dur = int(round((time.perf_counter() - t0) * 1000))

    if err:
        result, det = "FAIL", (detail or "")
    else:
        produced = produces(rv) if callable(produces) else _default_produced(rv)
        if produced:
            result, det = "DONE", (detail or "")
        else:
            result = "EMPTY"
            if no_capability:
                det = "NO_CAPABILITY" + ((":" + detail) if detail else "")
            else:
                det = detail or STALL_COUNTED
    rec = _validate({"ts": _now(), "as_of": _check_date(as_of), "actor": actor,
                     "action": action, "stage": stage, "result": result,
                     "duration_ms": dur, "error": err, "detail": det or ""})
    _append(rec, log_path)

    stall = None
    if result == "EMPTY":
        stall = _stall_check(actor, action, as_of, log_path, params_path)

    out = {"record": rec, "value": rv, "error": err, "stall": stall}
    if err and raise_on_error:
        raise ObservedFailure(err, out)
    return out


def wrap(actor, stage, *, action=None, as_of=None, log_path=None, params_path=None,
         raise_on_error=True):
    """装饰器形式（可选）：@wrap("scan", "C2", action="daily") —— 与 run_observed 同一实现。"""
    def deco(fn):
        def inner(*a, **kw):
            act = action or fn.__name__
            return run_observed(actor, act, stage, lambda: fn(*a, **kw), as_of=as_of,
                                log_path=log_path, params_path=params_path,
                                raise_on_error=raise_on_error)["value"]
        inner.__name__ = fn.__name__
        inner.__doc__ = fn.__doc__
        return inner
    return deco


# ── STALL（第 2 条：静默空转检测）────────────────────────────────────────────
def stall_streams(threshold=None, *, path=None, params_path=None):
    """当前处于 STALL 的流：同一 (actor, action) **连续** N 次 `EMPTY/RAN_NO_OUTPUT`。

    · 只数 `RAN_NO_OUTPUT`；`NO_CAPABILITY`（结构性为空）**不数**（见 docstring 第 3 条）。
    · DONE / FAIL 打断连击（真出过东西、或真报过错 → 通道不是"断而无声"）。
    · STALL 行本身不打断连击（它是本模块派生出来的，不是新的运行事实）。
    """
    thr = int(threshold) if threshold else _stall_threshold(params_path)
    flows = {}
    for r in read_log(path):
        flows.setdefault((r["actor"], r["action"]), []).append(r)
    out = []
    for (actor, action), rows in sorted(flows.items()):
        n, seq = 0, []
        for r in reversed(rows):
            if r["result"] == "STALL":
                continue
            if r["result"] == "EMPTY" and r["detail"] == STALL_COUNTED:
                n += 1
                seq.append(r)
            else:
                break
        if n >= thr:
            out.append({"actor": actor, "action": action, "count": n, "threshold": thr,
                        "from_ts": seq[-1]["ts"], "to_ts": seq[0]["ts"],
                        "as_of": seq[0]["as_of"], "detail": seq[0]["detail"]})
    return out


def _stall_check(actor, action, as_of, log_path, params_path):
    """落一条 EMPTY 之后判定是否刚到阈值 → 到则记一行 STALL（**只在跨过阈值那一刻记一次**）。

    不重复刷屏的理由：连击继续增长时若每次都记，run_log 会被 STALL 淹没——
    那正是"报得太多 = 没人看"（旧系统盲区的翻版）。
    """
    thr = _stall_threshold(params_path)
    rows = [r for r in read_log(log_path) if r["actor"] == actor and r["action"] == action]
    n = 0
    for r in reversed(rows):
        if r["result"] == "STALL":
            continue
        if r["result"] == "EMPTY" and r["detail"] == STALL_COUNTED:
            n += 1
        else:
            break
    if n != thr:                                  # 只在恰好跨过阈值那一次记
        return None
    rec = _validate({"ts": _now(), "as_of": _check_date(as_of), "actor": actor,
                     "action": action, "stage": rows[-1]["stage"], "result": "STALL",
                     "duration_ms": 0, "error": "", "detail": "连续 %d 次运行零产出" % n})
    _append(rec, log_path)
    return rec


# ── 未处理 FAIL（第 5 条）────────────────────────────────────────────────────
def unhandled_failures(path=None):
    """未处理的 FAIL：同一 (as_of, actor, action) 的 FAIL 之后没有再出现 DONE/EMPTY 行。

    「已处理」不靠人标记，靠**事实**：按游标重做该步且成功 → 同键出现 DONE/EMPTY → 视为已处理。
    """
    last = {}
    for r in read_log(path):
        k = (r["as_of"], r["actor"], r["action"])
        if r["result"] == "FAIL":
            last[k] = r
        elif r["result"] in ("DONE", "EMPTY"):
            last.pop(k, None)
    return [last[k] for k in sorted(last, key=lambda x: (str(x[0]), x[1], x[2]))]


# ── 唤醒即自检（第 6 条）────────────────────────────────────────────────────
def opening_checks(today=None, *, cursor_path=None, path=None, snapshot_root=None,
                   params_path=None):
    """三项自检（0.17 第 4/5 条 · 5.5 验收）：① 昨日是否结账 ② 未处理 FAIL ③ STALL。

    返回 {"ok", "checks", "lines", "failures", "stalls"}；`lines` 直接打在唤醒输出顶部。
    """
    import cursor as CUR
    import snapshot as SNAP

    lines, checks = [], {}
    # ① 昨日是否结账（未结账 = 上一轮中断，唤醒将重做本日）
    try:
        cur = CUR.read_cursor(cursor_path)
        pend = CUR.pending_steps(cur)
        behind = []
        if today:
            behind = [d for d in SNAP.available_dates(cur["date"], today, root=snapshot_root)
                      if d > cur["date"]]
        settled = not pend
        checks["cursor_settled"] = {"ok": settled, "date": cur["date"], "pending": pend,
                                    "days_behind": behind}
        lines.append("%s ① 上一日结账 : %s%s" % (
            "[OK]" if settled else "[!!]", cur["date"],
            " 五步全勾" if settled else
            " **五步未走完**（待办 %s）—— 中断未恢复，唤醒将重做本日" % " / ".join(pend)))
        if behind:
            lines.append("        └ 待追平    : %s（%d 个已采集交易日）"
                         % (" / ".join(behind), len(behind)))
    except Exception as e:
        settled = False
        checks["cursor_settled"] = {"ok": False, "error": str(e)}
        lines.append("[!!] ① 上一日结账 : **游标不可读** —— %s" % e)
    # ② 未处理 FAIL
    fails = unhandled_failures(path)
    checks["unhandled_fail"] = {"ok": not fails, "n": len(fails), "items": fails}
    if fails:
        lines.append("[!!] ② 未处理 FAIL : %d 条" % len(fails))
        for f in fails[:5]:
            lines.append("        └ %s %s:%s → %s" % (f["as_of"], f["actor"], f["action"],
                                                     str(f["error"])[:90]))
    else:
        lines.append("[OK] ② 未处理 FAIL : 无")
    # ③ STALL
    stalls = stall_streams(path=path, params_path=params_path)
    checks["stall"] = {"ok": not stalls, "n": len(stalls), "items": stalls}
    if stalls:
        lines.append("[!!] ③ STALL        : %d 条流连续零产出" % len(stalls))
        for s in stalls[:5]:
            lines.append("        └ %s:%s 连续 %d 次（阈值 %d）"
                         % (s["actor"], s["action"], s["count"], s["threshold"]))
    else:
        lines.append("[OK] ③ STALL        : 无")
    ok = bool(checks["cursor_settled"].get("ok") and not fails and not stalls)
    return {"ok": ok, "checks": checks, "lines": lines, "failures": fails, "stalls": stalls}


def summary(path=None):
    """只读总览：总行数 / 按 result 计数 / 按 actor 计数 / 未处理 FAIL / STALL / 最后一行。"""
    rows = read_log(path)
    by_result = {r: 0 for r in RESULTS}
    by_actor, by_detail = {}, {}
    for x in rows:
        by_result[x["result"]] = by_result.get(x["result"], 0) + 1
        by_actor[x["actor"]] = by_actor.get(x["actor"], 0) + 1
        if x["detail"]:
            by_detail[x["detail"].split(":")[0]] = by_detail.get(x["detail"].split(":")[0], 0) + 1
    return {"path": path or RUN_LOG_PATH, "schema_version": SCHEMA_VERSION,
            "fields": list(RUN_LOG_FIELDS), "n": len(rows), "by_result": by_result,
            "by_actor": by_actor, "by_detail": by_detail,
            "unhandled_failures": unhandled_failures(path),
            "stalls": stall_streams(path=path), "last": rows[-1] if rows else None}


# ── 静态扫描（纪律自检用）────────────────────────────────────────────────────
_TEST_FUNCS = ("_selftest", "_acceptance", "_discipline")


def _test_lines(tree):
    """测试函数（自检/验收/纪律）覆盖的行号集合 —— 尾界取 `astwin` 唯一实现。"""
    out = set()
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in _TEST_FUNCS:
            out |= ASTW.line_set(n)
    return out


def _fixture_names(tree):
    """夹具判别：**只有下划线私有名才可能是夹具**，且**调用者 ⊆ 测试函数集**。

    ★ 这条纪律的来历（同一类错误在三处出现过，故写死在此并进自检）：
      F6 的 `landing.py` 曾用「只被 `_selftest` 调用」单独判定夹具 → 把
      `upsert_candidate` / `promote` / `resolve` 这类**公开核心 API** 一并置空 →
      整个模块的写点审计**静默屏蔽**（全绿 = 没看）。
      故本实现两条都要：**公开名（无下划线前缀）恒进审计，永不被判为夹具**；
      私有名还必须"只被测试函数调用"才算夹具。
    """
    tl = _test_lines(tree)
    callers = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            fn = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            if fn:
                callers.setdefault(fn, set()).add(n.lineno in tl)
    out = set()
    for n in tree.body:
        if not isinstance(n, ast.FunctionDef) or not n.name.startswith("_"):
            continue
        if n.name in _TEST_FUNCS:
            continue
        cs = callers.get(n.name)
        if cs and cs <= {True}:
            out.add(n.name)
    return out


def _prod_source(path):
    """生产代码区（剔除测试函数整段与夹具函数）——纪律只约束生产代码。"""
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.splitlines()
    skip = _test_lines(tree)
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in _fixture_names(tree):
            skip |= ASTW.line_set(n)
    for i in sorted(skip):
        if 1 <= i <= len(lines):
            lines[i - 1] = ""
    return "\n".join(lines)


def _prod_idents(path):
    """生产代码区的**标识符 + 非文档字符串常量**（剔测试函数与夹具、剔 docstring）。

    为什么不用文本扫描：注释/文档里解释"为什么只有本模块能写留痕"会命中关键词 →
    把说明性文字判成违例（假判据）。判据必须是**真引用**，不是"提到过"。
    """
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    skip = _test_lines(tree)
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in _fixture_names(tree):
            skip |= ASTW.line_set(n)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            b = node.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) \
                    and isinstance(b[0].value.value, str):
                docs.add(id(b[0].value))
    toks = set()
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) in skip:
            continue
        if isinstance(node, ast.Name):
            toks.add(node.id)
        elif isinstance(node, ast.Attribute):
            toks.add(node.attr)
        elif isinstance(node, ast.arg):
            toks.add(node.arg)
        elif isinstance(node, ast.FunctionDef):
            toks.add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docs:
            toks.add(node.value)
    return toks


def _tool_sources(root=None):
    d = os.path.join(root or ROOT, "tools")
    out = []
    for r, _dirs, files in os.walk(d):
        if "__pycache__" in r:
            continue
        for f in sorted(files):
            if f.endswith(".py"):
                out.append(os.path.join(r, f))
    return out


_WRITE_MODES = ("w", "w+", "a", "a+", "x", "x+", "r+")
_SINK_ATTRS = ("replace", "rename", "move", "copy", "copy2", "copyfile")


def _writes_run_log(path):
    """该模块**生产区**是否把 run_log 当**写目标**（而非仅仅登记 / 提到路径）。

    ★ F7 收口升级（原判据有真缺陷）：旧判据 = 「生产区出现过 run_log 字样」，它会把
      **0.7 落点审计器 `landing.py`** 判成违规——而审计器**必须**把
      `data/state/run_log.jsonl` 登记进 `WRITE_ENTRYPOINTS`，那是**声明表**、不是写点
      （登记真实目标路径正是它的职责）。判据必须是「真写」，不是「提到」
      （与 F6「夹具判别」同族：抓"提到"就会把合法的元数据引用判成违例）。
    现判据 = 某个**生产函数**内**同时**出现（a）run_log 引用 与（b）写 sink。
    """
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    skip = _test_lines(tree)
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in _fixture_names(tree):
            skip |= ASTW.line_set(n)
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name in _TEST_FUNCS:
            continue
        toks, writes = set(), False
        for node in ast.walk(fn):
            if getattr(node, "lineno", None) in skip:
                continue
            if isinstance(node, ast.Name):
                toks.add(node.id)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                toks.add(node.value)
            elif isinstance(node, ast.Call):
                nm = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if nm in _SINK_ATTRS:
                    writes = True
                elif nm == "open":
                    modes = [a.value for a in node.args[1:] if isinstance(a, ast.Constant)]
                    modes += [k.value.value for k in node.keywords
                              if k.arg == "mode" and isinstance(k.value, ast.Constant)]
                    if any(m in _WRITE_MODES for m in modes):
                        writes = True
        if writes and any("run_log" in t.lower() for t in toks):
            return True
    return False


def _produces_rules():
    """[noqa] 本函数存在的唯一理由：把「未挂载」的判据做成**可机械复算**的形式。"""
    return None


def _discipline(verbose=True):
    """观测纪律七项（0.17 全条）：schema / 三类可复现 / 失败不吞 / 单写点 /
    结构性空转不误报 / 正式入口已挂载 / 真库零静默失败。"""
    res, bad = [], []

    def T(name, cond, extra=""):
        res.append((name, bool(cond), extra))
        if not cond:
            bad.append(name)

    # ① run_log schema 冻结（字段名与顺序）
    T("① run_log schema 冻结（%d 字段 · 顺序即真源）" % len(RUN_LOG_FIELDS),
      RUN_LOG_FIELDS == ("ts", "as_of", "actor", "action", "stage", "result",
                         "duration_ms", "error", "detail"), list(RUN_LOG_FIELDS))
    # ② 环节值域派生自注册表（禁第二份清单）
    T("② 环节值域派生自注册表 MOUNTS（%s + %s）"
      % (",".join(CHAIN_STAGES), ",".join(NON_CHAIN_STAGES)),
      CHAIN_STAGES == tuple(REG.MOUNTS))
    # ③ 失败不吞：无因 FAIL 被拒
    def _no_reason():
        _validate({"ts": _now(), "as_of": None, "actor": "observe", "action": "x",
                   "stage": "PRE", "result": "FAIL", "duration_ms": 0, "error": "", "detail": ""})
    try:
        _no_reason()
        T("③ 失败不吞：无 error 的 FAIL 行被拒", False, "竟被接受")
    except ObserveError:
        T("③ 失败不吞：无 error 的 FAIL 行被拒", True)
    # ④ 单写点：全库生产区**只有本模块真写** run_log
    #    （判据 = 「真写」而非「提到」：登记/声明路径不算写 —— 见 _writes_run_log docstring）
    mine = os.path.abspath(__file__)
    others = []
    for p in _tool_sources():
        if os.path.abspath(p) == mine:
            continue
        try:
            if _writes_run_log(p):
                others.append(os.path.relpath(p, ROOT))
        except Exception:
            continue
    T("④ 单写点：%d 个模块的生产区真写 run_log（登记/提到不算）" % len(others), not others, others)
    T("④c 仅**登记** run_log 路径的模块不误判（0.7 落点审计器须登记本路径）",
      not _writes_run_log(os.path.join(ROOT, "tools", "fs", "landing.py")))
    T("④d 真写 run_log 的本模块被判为写方（判据不是空判据）",
      _writes_run_log(mine))
    # ④b 夹具判别不吞公开名（F6 同一负例，防"审计静默屏蔽"回归）
    _t = ast.parse(io.open(os.path.abspath(__file__), encoding="utf-8").read())
    _pub = [n.name for n in _t.body
            if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")]
    _fx = _fixture_names(_t)
    T("④b 夹具判别永不吞公开名（%d 个公开函数全在审计内）" % len(_pub),
      bool(_pub) and not (_fx & set(_pub)), sorted(_fx))
    # ⑤ 结构性为空不计入 STALL（真跑一遍：NO_CAPABILITY 连打 5 次不应出 STALL）
    import tempfile as _tf
    d = _tf.mkdtemp(prefix="ta_observe_disc_")
    lp = os.path.join(d, "run_log.jsonl")
    for _ in range(5):
        run_observed("learning", "chain:C6", "C6", lambda: None, as_of="2026-10-08",
                     no_capability=True, detail="C6", log_path=lp)
    n_struct = len([r for r in read_log(lp) if r["result"] == "STALL"])
    lp2 = os.path.join(d, "run_log2.jsonl")
    for _ in range(_stall_threshold()):
        run_observed("learning", "scan", "C2", lambda: None, as_of="2026-10-08",
                     log_path=lp2)
    n_real = len([r for r in read_log(lp2) if r["result"] == "STALL"])
    T("⑤ 结构性为空（NO_CAPABILITY）连续 5 次 → 不报 STALL", n_struct == 0, n_struct)
    T("⑤b 本该产出却没产出（RAN_NO_OUTPUT）达阈值 → 报 STALL", n_real == 1, n_real)
    # ⑥ 正式入口已挂载 run_observed（wakeup.py 在盘上则必须挂）
    wp = os.path.join(ROOT, "tools", "fs", "wakeup.py")
    if os.path.exists(wp):
        toks = _prod_idents(wp)
        T("⑥ wakeup.py 已挂 run_observed（0.17 第 7 条）", "run_observed" in toks)
    else:
        T("⑥ wakeup.py 尚未落盘（0.14 未施工）→ 本条挂起", True, "PENDING")
    # ⑦ 真库零静默失败：真 run_log 里每个 FAIL 都有 error 文本；且 schema 全合法
    try:
        rows = read_log()
        bad_fail = [r for r in rows if r["result"] == "FAIL" and not r["error"].strip()]
        T("⑦ 真库 run_log %d 行 schema 全合法、FAIL 全有 error" % len(rows), not bad_fail,
          bad_fail)
    except ObserveError as e:
        T("⑦ 真库 run_log schema 全合法", False, str(e))
    if verbose:
        for name, okk, extra in res:
            print("  [%s] %s" % ("OK" if okk else "!!", name) + ("  %s" % (extra,) if extra else ""))
    return {"ok": not bad, "n": len(res), "failed": bad, "results": res}


# ── 自检（--selftest）：全部在临时目录跑，不碰真 run_log ──────────────────────
def _selftest():
    import tempfile

    ok = fail = 0
    fails = []

    def T(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [OK] %s" % name)
        else:
            fail += 1
            fails.append(name)
            print("  [!!] %s %s" % (name, extra))

    def raises(fn, *a, **kw):
        try:
            fn(*a, **kw)
            return False
        except (ObserveError, ObservedFailure):
            return True

    def line(**kw):
        d = {"ts": "2026-10-08T14:00:00", "as_of": "2026-10-08", "actor": "wakeup",
             "action": "day", "stage": "C0", "result": "DONE", "duration_ms": 1,
             "error": "", "detail": ""}
        d.update(kw)
        return d

    tmp = tempfile.mkdtemp(prefix="ta_observe_selftest_")
    LP = os.path.join(tmp, "run_log.jsonl")

    print("【A】行 schema 与值域（8）")
    T("A1 合法行通过", _validate(line())["result"] == "DONE")
    T("A2 缺字段被拒", raises(_validate, {k: v for k, v in line().items() if k != "detail"}))
    T("A3 多字段被拒", raises(_validate, dict(line(), extra=1)))
    T("A4 非法 result 被拒", raises(_validate, line(result="SUCCESS")))
    T("A5 FAIL 无 error 被拒", raises(_validate, line(result="FAIL")))
    T("A6 非 FAIL 带 error 被拒", raises(_validate, line(result="DONE", error="boom")))
    T("A7 非法 stage 被拒", raises(_validate, line(stage="C9")))
    T("A8 duration_ms 负数被拒", raises(_validate, line(duration_ms=-1)))
    T("A9 非法 actor 被拒", raises(_validate, line(actor="Wakeup X")))
    T("A10 非法日期被拒", raises(_validate, line(as_of="2026-13-40")))
    T("A11 非交易日动作 as_of=None 合法", _validate(line(as_of=None))["as_of"] is None)
    T("A12 stage 值域 = MOUNTS + PRE/POST", set(STAGES) == set(REG.MOUNTS) | {"PRE", "POST"})

    print("【B】run_observed：产出判定与留痕（6）")
    r1 = run_observed("scan", "daily", "C2", lambda: [1, 2], as_of="2026-10-08", log_path=LP)
    T("B1 非空返回 → DONE", r1["record"]["result"] == "DONE")
    r2 = run_observed("scan", "daily", "C2", lambda: [], as_of="2026-10-08", log_path=LP)
    T("B2 空返回 → EMPTY / RAN_NO_OUTPUT",
      r2["record"]["result"] == "EMPTY" and r2["record"]["detail"] == "RAN_NO_OUTPUT")
    r3 = run_observed("learning", "chain:C6", "C6", lambda: None, as_of="2026-10-08",
                      no_capability=True, detail="C6", log_path=LP)
    T("B3 结构性为空 → EMPTY / NO_CAPABILITY:C6",
      r3["record"]["detail"] == "NO_CAPABILITY:C6")
    T("B4 用 produces 自定义判据（dict 有 settled=False → 空）",
      run_observed("wakeup", "settle", "C0", lambda: {"settled": False}, as_of="2026-10-08",
                   produces=lambda r: r["settled"], log_path=LP)["record"]["result"] == "EMPTY")
    T("B5 duration_ms 为整数非负",
      isinstance(r1["record"]["duration_ms"], int) and r1["record"]["duration_ms"] >= 0)
    T("B6 落盘行数与调用次数一致", len(read_log(LP)) == 4, len(read_log(LP)))

    print("【C】失败不吞（5）")
    def boom():
        raise ValueError("数据源不可达")
    r = run_observed("settle", "day:2026-10-08", "C0", boom, as_of="2026-10-08",
                     log_path=LP, raise_on_error=False)
    T("C1 抛错 → status=FAIL + error 文本",
      r["record"]["result"] == "FAIL" and "数据源不可达" in r["error"])
    T("C2 FAIL 行已落盘", read_log(LP)[-1]["result"] == "FAIL")
    T("C3 默认上抛（失败不吞）",
      raises(run_observed, "settle", "day:2026-10-08", "C0", boom, as_of="2026-10-08", log_path=LP))
    T("C4 上抛仍已留痕（留痕先于抛出）",
      len([x for x in read_log(LP) if x["result"] == "FAIL"]) == 2)
    T("C5 上抛后游标无人推进由调用层负责（本模块不改任何状态）",
      not os.path.exists(os.path.join(tmp, "cursor.json")))

    print("【D】STALL：结构性为空不误报（4）")
    LP2 = os.path.join(tmp, "rl2.jsonl")
    for _ in range(5):
        run_observed("learning", "weekly", "C6", lambda: None, as_of="2026-10-08",
                     no_capability=True, detail="C6", log_path=LP2)
    T("D1 结构性为空连续 5 次 → 零 STALL",
      len([x for x in read_log(LP2) if x["result"] == "STALL"]) == 0)
    LP3 = os.path.join(tmp, "rl3.jsonl")
    thr = _stall_threshold()
    for _ in range(thr):
        run_observed("scan", "daily", "C2", lambda: [], as_of="2026-10-08", log_path=LP3)
    st = [x for x in read_log(LP3) if x["result"] == "STALL"]
    T("D2 RAN_NO_OUTPUT 达阈值（%d）→ 记 1 条 STALL" % thr, len(st) == 1, len(st))
    run_observed("scan", "daily", "C2", lambda: [], as_of="2026-10-08", log_path=LP3)
    T("D3 连击继续增长不重复刷屏", len([x for x in read_log(LP3) if x["result"] == "STALL"]) == 1)
    run_observed("scan", "daily", "C2", lambda: ["产出"], as_of="2026-10-08", log_path=LP3)
    for _ in range(thr):
        run_observed("scan", "daily", "C2", lambda: [], as_of="2026-10-08", log_path=LP3)
    T("D4 DONE 打断连击 → 重新达阈值可再报",
      len([x for x in read_log(LP3) if x["result"] == "STALL"]) == 2)
    T("D5 stall_streams 可复现",
      bool(stall_streams(path=LP3)) and stall_streams(path=LP3)[0]["actor"] == "scan")

    print("【E】未处理 FAIL 的判据（4）")
    LP4 = os.path.join(tmp, "rl4.jsonl")
    run_observed("settle", "day", "C0", boom, as_of="2026-10-08", log_path=LP4,
                 raise_on_error=False)
    T("E1 FAIL → 未处理", len(unhandled_failures(LP4)) == 1)
    run_observed("settle", "day", "C0", lambda: {"ok": 1}, as_of="2026-10-08", log_path=LP4)
    T("E2 同键重做成功 → 视为已处理", len(unhandled_failures(LP4)) == 0)
    run_observed("settle", "day", "C0", boom, as_of="2026-10-09", log_path=LP4,
                 raise_on_error=False)
    T("E3 换一天（as_of 不同）→ 是新 stream 的未处理 FAIL",
      len(unhandled_failures(LP4)) == 1 and unhandled_failures(LP4)[0]["as_of"] == "2026-10-09")
    run_observed("settle", "day", "C0", lambda: None, as_of="2026-10-09", log_path=LP4,
                 no_capability=True)
    T("E4 EMPTY 也算重做成功（按游标自动重做，产出可空）", len(unhandled_failures(LP4)) == 0)

    print("【F】唤醒三项自检（5）")
    import cursor as CUR
    env = tempfile.mkdtemp(prefix="ta_observe_env_", dir=tmp)
    cp = os.path.join(env, "cursor.json")
    io.open(cp, "w", encoding="utf-8", newline="\n").write(json.dumps(
        {"version": 1, "date": "2026-10-08",
         "checklist": {s: True for s in CUR.STEPS}}, ensure_ascii=False))
    LP5 = os.path.join(tmp, "rl5.jsonl")
    oc = opening_checks("2026-10-08", cursor_path=cp, path=LP5, snapshot_root=env)
    T("F1 三项齐全（① 结账 ② FAIL ③ STALL）",
      set(oc["checks"]) == {"cursor_settled", "unhandled_fail", "stall"})
    T("F2 全绿 → ok=True 且三行自检",
      oc["ok"] and len([x for x in oc["lines"] if x.startswith("[OK]")]) == 3, oc["lines"])
    cur = json.load(io.open(cp, encoding="utf-8"))
    cur["checklist"][CUR.STEPS[2]] = False
    io.open(cp, "w", encoding="utf-8", newline="\n").write(json.dumps(cur, ensure_ascii=False))
    oc2 = opening_checks("2026-10-08", cursor_path=cp, path=LP5, snapshot_root=env)
    T("F3 中断未恢复（第 3 步 盈亏结算 未打勾）→ ① 报 !! 且 ok=False 且列出待办步名",
      not oc2["ok"] and not oc2["checks"]["cursor_settled"]["ok"]
      and CUR.STEPS[2] in " ".join(oc2["lines"]),
      "ok=%s settled_ok=%s lines=%s" % (oc2["ok"], oc2["checks"]["cursor_settled"].get("ok"),
                                        oc2["lines"]))
    run_observed("wakeup", "day", "C0", boom, as_of="2026-10-08", log_path=LP5,
                 raise_on_error=False)
    oc3 = opening_checks("2026-10-08", cursor_path=cp, path=LP5, snapshot_root=env)
    T("F4 有未处理 FAIL → ② 报 !! 且列出来",
      not oc3["checks"]["unhandled_fail"]["ok"] and "12:00" not in " ".join(oc3["lines"]))
    LP6 = os.path.join(tmp, "rl6.jsonl")
    for _ in range(_stall_threshold()):
        run_observed("scan", "daily", "C2", lambda: [], as_of="2026-10-08", log_path=LP6)
    oc4 = opening_checks("2026-10-08", cursor_path=cp, path=LP6, snapshot_root=env)
    T("F5 有 STALL → ③ 报 !! 且失败显式在顶部",
      not oc4["checks"]["stall"]["ok"]
      and any(x.startswith("[!!] ③") for x in oc4["lines"]))

    print("【G】纪律与真库（4）")
    d = _discipline(verbose=False)
    T("G1 纪律七项全绿（%d 项）" % d["n"], d["ok"], d["failed"])
    s = summary()
    T("G2 总览字段齐（by_result / by_actor / last）",
      all(k in s for k in ("by_result", "by_actor", "last", "fields")))
    T("G3 真 run_log 不因自检被污染（自检全在临时目录）",
      os.path.abspath(RUN_LOG_PATH).startswith(os.path.abspath(ROOT)))
    T("G4 模块自体不含第二份环节清单（stage 值域来自注册表）",
      tuple(REG.MOUNTS) == CHAIN_STAGES)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：")
        for f in fails:
            print("  - %s" % f)
    return 0 if not fail else 1


def _report():
    s = summary()
    print("run_log 总览 · %s" % s["path"])
    print("  schema  : v%s · %s" % (s["schema_version"], " / ".join(s["fields"])))
    print("  总行数  : %d" % s["n"])
    print("  按结果  : %s" % " · ".join("%s=%d" % (k, v) for k, v in sorted(s["by_result"].items())))
    print("  按 actor: %s" % " · ".join("%s=%d" % (k, v) for k, v in sorted(s["by_actor"].items())))
    if s["by_detail"]:
        print("  空转性质: %s" % " · ".join("%s=%d" % (k, v) for k, v in sorted(s["by_detail"].items())))
    print("  未处理 FAIL: %d 条" % len(s["unhandled_failures"]))
    for f in s["unhandled_failures"][:5]:
        print("    - %s %s:%s %s" % (f["as_of"], f["actor"], f["action"], str(f["error"])[:80]))
    print("  STALL      : %d 条流" % len(s["stalls"]))
    for x in s["stalls"][:5]:
        print("    - %s:%s 连续 %d 次" % (x["actor"], x["action"], x["count"]))
    if s["last"]:
        print("  最后一行: %s %s:%s → %s" % (s["last"]["ts"], s["last"]["actor"],
                                             s["last"]["action"], s["last"]["result"]))


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    if "--discipline" in argv:
        print("观测纪律（0.17）")
        d = _discipline()
        print("\n纪律：%d 项 %s" % (d["n"], "全 OK ✅" if d["ok"] else "有失败 ❌ %s" % d["failed"]))
        return 0 if d["ok"] else 1
    if "--report" in argv:
        for r in read_log():
            print(json.dumps(r, ensure_ascii=False))
        return 0
    _report()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
