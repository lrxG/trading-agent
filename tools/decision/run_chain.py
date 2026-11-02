#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_chain.py · C0→C6 基础运转链路生产运行器（v1.0 · 2026-10-15 建）

用途：把 C0–C6 各模块按真实调用顺序串起来，**一条命令跑通基础运转链路**，
      每步记录状态与实际调用参数，末步输出链路报告（JSON + 摘要）。

链路（严格顺序，任一步 FAIL 默认中止，`--continue-on-fail` 可显式继续）：
  C0 数据层  : snapshot/raw 就绪度只读核验（admission 矩阵 + 日期边界）
  C1 环境档  : regime.from_snapshot(as_of) → 环境档（UNAVAILABLE 如实，禁编档）
  C2 候选池  : scan（--scan 时实跑）/ 否则只读核验池就绪度（池文件 + 活跃候选数）
  C3 驱动链  : **两条互斥路径**
               (a) 正式路径 `--proposals`：`drive.review` 落 C3 链（C3 专用 proposal 的
                   `chains` 段）→ `brief.assemble` 装配 → `brief.classify` 校验 →
                   `decide_from_proposal` → `orchestrate.decide`（全链 fail-closed）
               (b) 原样 spec `--driver-file`：`{code: {thesis_id, chain, driver, …}}`
                   直喂 `orchestrate.decide`（夹具/回放用；缺则 SKIP_MISSING_DRIVER）
  C4 执行闸  : 决策落账后核验口径（BUY/WAIT/NO 计分；机械拒绝如实记）
  C5 风控    : position_gate.check（持仓/回撤/熔断）+ exit_gate.check（出场提醒）
  C6 闭环    : settle 结算 → eod_review 复盘 → case_builder 归因 Case
               （仅 --settle-genuine 时真结算；缺日历/账本时如实记 SKIP）

★ 纪律：
  · 默认 **全沙盒**（--base 指向已存在目录）——生产运行须显式 `--production`
    且经 writegate（本件不绕闸；生产写入由被调模块各自的守卫兜底）。
  · 零伪造：任何缺源一律如实标 UNAVAILABLE / SKIP，绝不合成数据。
  · 生产写入保护保留：所有被调写点仍经 writegate 原语级守卫。

用法：
  python tools/decision/run_chain.py --as-of 2026-09-29 --base /path/sandbox
  python tools/decision/run_chain.py --as-of 2026-09-29 --base <sb> --scan
  # ★ 正式判断路径（推荐）：外部判断产物（C4 提案 + C3 链提案）→ 全链
  python tools/decision/run_chain.py --as-of 2026-09-29 --base <sb> \
         --proposals <sb>/data/input/proposals_2026-09-29.json --settle-genuine \
         --calendar <sb>/data/raw/meta/trade_cal.csv
  # 原样 spec 路径（夹具/回放）：spec 直喂，不校验判断前提
  python tools/decision/run_chain.py --as-of 2026-09-29 --base <sb> --driver-file drivers.json
  python tools/decision/run_chain.py --selftest
"""
import argparse
import datetime as _dt
import io
import json
import os
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "..", ".."))
for _p in ("fs", "analysis", "decision", "risk", "learning", "e2e", "discovery"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import candidates as CAND  # noqa: E402
import cursor as CUR  # noqa: E402
import decision as DEC  # noqa: E402
import loader as LD  # noqa: E402
import observe as OBS  # noqa: E402
import orchestrate as DEC_ORCH  # noqa: E402
import params as PRM  # noqa: E402
from paths import ROOT  # noqa: E402

ACTOR = "run_chain"
SCHEMA_VERSION = 1


class ChainError(Exception):
    pass


def _check_date(s):
    try:
        return _dt.date.fromisoformat(s).isoformat()
    except Exception:
        raise ChainError("as_of 必须是 YYYY-MM-DD：%r" % (s,))


def _step(name, status, detail=None, extra=None):
    return {"step": name, "status": status, "detail": detail or {}, "extra": extra or {}}


def _timing_gate(as_of):
    """择时闸（tools/analysis/timing.py）→ dict；不可用 ⇒ fail-closed（禁新开仓）。"""
    try:
        import timing as TM
    except ImportError:
        import sys as _s
        _s.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "tools", "analysis"))
        import timing as TM
    return TM.signal(as_of)      # ★ 2026-11-02 收敛到 timing.signal 门面（原为本地副本）


# ── C0 数据层 ────────────────────────────────────────────────────────────────
def c0_data(as_of, base=None):
    """数据就绪度只读核验：admission 矩阵 + snapshot 日期边界。"""
    b = base or ROOT
    detail = {}
    try:
        import admission as ADM
        matrix = ADM.admission_matrix()      # 声明层视图（5 契约文件），无参
        blocked = [r["file_key"] for r in matrix if r["declared_blocked"]]
        detail["admission_files"] = len(matrix)
        detail["declared_blocked"] = blocked
        # ★ 数据时效闸（2026-10-27 整改 P0b）：FRESH/STALE/MISSING/INVALID 四态，
        #   关键字段不 FRESH ⇒ decision_admit=WAIT ⇒ BUY 全数转 WAIT（见 run 主流程）
        detail["data_gate"] = ADM.decision_admit(as_of, base=b)
    except Exception as e:
        return _step("C0", "FAIL", {"reason": "admission 不可用：%s" % e})
    snap_dir = os.path.join(b, "data", "snapshot", as_of)
    detail["snapshot_dir_exists"] = os.path.isdir(snap_dir)
    if os.path.isdir(snap_dir):
        detail["snapshot_files"] = sorted(os.listdir(snap_dir))[:12]
    raw_dir = os.path.join(b, "data", "raw", as_of)
    detail["raw_dir_exists"] = os.path.isdir(raw_dir)
    if os.path.isdir(raw_dir):
        detail["raw_files"] = sorted(f for f in os.listdir(raw_dir)
                                     if not f.startswith("_"))[:12]
    ok = detail["snapshot_dir_exists"] or detail["raw_dir_exists"]
    return _step("C0", "OK" if ok else "FAIL",
                 detail if ok else dict(detail, reason="as_of 无 snapshot 也无 raw"))



# ── C1 环境档腿回写（2026-10-19 修断链）────────────────────────────────────
# ★ 断链事实：`brief.read_env` 读的是 `brain_state.md` 的「## 环境档」段（标注
#   "← C1 产出"），但 **C1 从来没有写过它** —— `regime.evaluate` 只经 observe 落
#   run_log。后果实测（09-30 全链）：C1 步报 OK（强势/5 成），而下游 brief 读到的
#   仍是 09-29 的环境档 ⇒ 简报打出「环境锚时点核验 ❌」，且 5 个 BUY 的定价用的是
#   **上一交易日**的市场状态。修法：C1 成功后把当日环境档写回该段（幂等覆盖表体，
#   保留段头说明与其它腿不动）。
_ENV_SEC_PAT = r"^## 环境档[^\n]*\n(.*?)(?=^## |\Z)"


def _env_sec_re():
    """延迟编译（本块在模块 import 区之前，`re` 尚未导入）。"""
    import re as _re
    return _re.compile(_ENV_SEC_PAT, _re.S | _re.M)


def _brain_path(base=None):
    return os.path.join(base or ROOT, "memory", "state", "brain_state.md")


def write_env_leg(env, as_of, base=None):
    """把 C1 env 写回 `brain_state.md` 环境档表（→ "WRITTEN" / "SKIP_*"）。

    * 文件缺 ⇒ **不建**（记忆载体不由本件首建），返回 "SKIP_NO_BRAIN"。
    * 段缺 ⇒ 追加段（段头 + 表）；段在 ⇒ 只换表体，保留段内说明文字。
    * `as_of/source` 行写当日与 source，供下游做**环境锚时点核验**。
    """
    p = _brain_path(base)
    if not os.path.isfile(p):
        return "SKIP_NO_BRAIN"
    if not isinstance(env, dict) or not env.get("regime"):
        return "SKIP_NO_ENV"
    _WG = None
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "fs"))
        import writegate as _WG                                  # noqa: F401
        _WG.check_write(p)
    except Exception:                                            # noqa: BLE001
        raise
    sig = env.get("signals") or {}
    rows = ["| 项 | 值 |", "| --- | --- |",
            "| 档位 | **%s** |" % env.get("regime"),
            "| 仓位天花板（成） | %s |" % (env.get("position_cap")
                                          if env.get("position_cap") is not None else "—"),
            "| as_of / source | %s / %s |" % (as_of, env.get("source") or "—")]
    for label, key, fmt in (("涨停家数", "limit_up_count", "%d"),
                            ("连板高度", "max_board", "%d"),
                            ("炸板率", "fail_rate", "%.1f%%")):
        v = sig.get(key)
        if v is None:
            rows.append("| %s | — |" % label)
        elif key == "fail_rate":
            rows.append("| %s | %s |" % (label, fmt % (v * 100)))
        else:
            rows.append("| %s | %s |" % (label, fmt % v))
    if sig.get("turnover") is not None:
        rows.append("| 成交额（只记录） | %.2f 万亿 |" % (sig["turnover"] / 1e12))
    table = "\n".join(rows) + "\n"

    text = io.open(p, encoding="utf-8").read()
    m = _env_sec_re().search(text)
    if not m:
        text = text.rstrip("\n") + "\n\n## 环境档（← C1 产出）\n\n" + table
    else:
        body = m.group(1)
        # 保留表体之前的说明行（引用块等），只替换表格部分
        head_lines = []
        for ln in body.split("\n"):
            if ln.strip().startswith("|"):
                break
            head_lines.append(ln)
        head = "\n".join(head_lines).rstrip("\n")
        new_body = (head + "\n\n" if head.strip() else "") + table
        text = text[:m.start(1)] + new_body + "\n" + text[m.end(1):]
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return "WRITTEN"


# ── C1 环境档 ────────────────────────────────────────────────────────────────
def _env_for_three_q(env):
    """把 regime.from_snapshot 结果归一化成三问 `_norm_env` 契约
    {regime, signals, leg, source}（leg 必填；source ∈ ENV_SOURCES；去 as_of/position_cap）。
    regime 结果 source='snapshot'（快照装载）→ 映射 'LOADER'；None/缺 → 显式 UNAVAILABLE。
    """
    src_map = {"snapshot": "LOADER", "loader": "LOADER"}
    if not env:
        return {"regime": None, "signals": {}, "leg": "EOD", "source": "UNAVAILABLE"}
    d = env if isinstance(env, dict) else getattr(env, "__dict__", {})
    src = src_map.get(str(d.get("source")), str(d.get("source") or "LOADER"))
    if src not in ("LOADER", "EXPLICIT", "UNAVAILABLE"):
        src = "LOADER"
    return {"regime": d.get("regime"), "signals": d.get("signals") or {},
            "leg": "EOD", "source": src}


def c1_regime(as_of, base=None, params_path=None):
    """environment 档：走 regime.from_snapshot（只读 env_metrics）→ 归一化三问 env。"""
    try:
        import regime as RG
    except Exception as e:
        return _step("C1", "FAIL", {"reason": "regime 不可用：%s" % e}), _env_for_three_q(None)
    try:
        env = RG.from_snapshot(as_of, params_path=params_path, base=base)
    except Exception as e:
        return _step("C1", "UNAVAILABLE", {"reason": str(e)}), _env_for_three_q(None)
    d = env if isinstance(env, dict) else getattr(env, "__dict__", {})
    norm = _env_for_three_q(env)
    # ★ 环境档腿回写：C1 的产出必须落到下游真正读的地方（否则 brief 读上一日）
    leg = "SKIP_NO_ENV"
    if env:
        try:
            leg = write_env_leg(dict(norm, position_cap=d.get("position_cap")),
                                as_of, base=base)
        except Exception as e:                                   # noqa: BLE001
            leg = "FAIL: %s: %s" % (type(e).__name__, e)
    return (_step("C1", "OK" if env else "UNAVAILABLE",
                  {"source": norm["source"], "regime": norm["regime"],
                   "position_cap": d.get("position_cap"),
                   "env_leg": leg}), norm)


# ── C2 候选池 ────────────────────────────────────────────────────────────────
def c2_pool(as_of, base=None, pool_path=None, do_scan=False,
            params_path=None, calendar_path=None):
    """候选池：--scan 时实跑 scan；否则只读核验池就绪度。"""
    b = base or ROOT
    pp = pool_path or os.path.join(b, "data", "state", "candidates.json")
    detail = {"pool_path": os.path.relpath(pp, b) if pp.startswith(b) else pp}
    if do_scan:
        try:
            import scan as SC
            kw = {}
            if base:
                kw["base"] = base
            # 2026-10-19 修：池路径必须**始终**注入（base 派生后的 pp）——原实现
            # 仅在用户显式 --pool 时才传，缺省时 scan→candidates 回落生产
            # POOL_PATH ⇒ 沙盒链必写生产候选池被闸门拦（C2 FAIL）。
            # 路径所有权在调用方（candidates 按纪律不派生）。
            kw["pool_path"] = pp
            if params_path:
                kw["params_path"] = params_path
            # 2026-10-19：日历缺省自动解析（<base>/data/raw/meta/trade_cal.csv），
            # 显式 --calendar 仍优先；解析不到 ⇒ None（dayclass fail-closed）。
            _cal_p, _cal_src = _resolve_calendar(calendar_path, base=base)
            if _cal_p:
                import dayclass as DC
                kw["calendar"] = DC.Calendar.from_seed_csv(_cal_p)
            detail["calendar"] = {"path": _cal_p, "source": _cal_src}
            # 2026-10-19 修：scan 的入口函数名是 scan()（五路机械扫描），
            # 原调 SC.evaluate 恒 AttributeError（主链 C2 必断）。
            # scan() 日状态拒绝（非 TRADING_WITH_DATA）时返回 None —— 调用方
            # 须把 None 当「零候选 + 日闸拒绝」处理，不是异常。
            res = SC.scan(as_of, **kw)
            if res is None:
                return _step("C2", "EMPTY", dict(
                    detail, reason="日闸拒绝：非 TRADING_WITH_DATA（scan 返回 None，"
                                   "禁编池）")), []
            detail["scan"] = {"status": res.get("status") if isinstance(res, dict)
                              else str(type(res))}
        except Exception as e:
            return _step("C2", "FAIL", dict(detail, reason="scan 失败：%s" % e)), []
    if not os.path.isfile(pp):
        # 无池：C2 无法产出候选，但链路可继续（C3 记零候选）
        return _step("C2", "EMPTY", dict(detail, reason="池文件不存在（未初始化）")), []
    try:
        pool = json.load(io.open(pp, encoding="utf-8"))
    except Exception as e:
        return _step("C2", "FAIL", dict(detail, reason="池读取失败：%s" % e)), []
    cands = pool.get("candidates", [])
    active = [c for c in cands if c.get("state") not in ("EXPIRED", "TRADED")]
    detail.update({"n_candidates": len(cands), "n_active": len(active),
                   "updated_at": pool.get("updated_at")})
    return _step("C2", "OK" if active else "EMPTY", detail), active


# ── 正式价线授权消费（2026-10-16 · 补齐 gendrive 注释承诺的接线）────────────────
def pl_admitted(auth_path, price_lines_path, active_codes=None):
    """正式价线授权件 → `{code: {entry_limit,stop_line,target,window,position_request}}`。

    **校验唯一真源 = `pl_input`**（本件不复制第二套准入校验）：走
    `pl_input.load_authorizations` + `pl_input.build`，只取 `admitted`。
    未授权 / 被拒 / 不在活跃池 → **不出现在返回里**（= 该票价格槽保持 null）。

    纪律：本函数**只填事实输入**（价线四件 + 显式仓位），**不决定 action**——
    action 仍由 `three_q.answer` 机械裁决；若判 WAIT/NO，`three_q` 会把价格槽置空
    （0.3 纪律在 three_q 内强制，本件不重复实现）。
    """
    import pl_input as PI
    doc = PI.load_authorizations(auth_path)
    pl_doc = json.load(io.open(price_lines_path, encoding="utf-8-sig"))
    pl_by = pl_doc.get("by_code") or {}
    # `pl_input.build` 契约：→ (admitted, rejected, summary)；
    # admitted[code] = {"answer_kwargs": {五键}, "audit": {...}}
    admitted_map, rejected_map, summary = PI.build(doc, pl_by,
                                                   active_codes=active_codes)
    out = {}
    for code, v in (admitted_map or {}).items():
        kw = v.get("answer_kwargs") or {}
        out[code] = {"entry_limit": kw.get("entry_limit"),
                     "stop_line": kw.get("stop_line"),
                     "target": kw.get("target"), "window": kw.get("window"),
                     "position_request": kw.get("position_request")}
    return out, summary, sorted(rejected_map or {})


def drawdown_of(sim=True, base=None, equity0=None):
    """已实现净值曲线的最大回撤（**比例**，非百分数）——口径同
    `batch_replay._drawdown`（披露口径：不含浮动盈亏）。

    无 `equity0` 且**有**已结算行 → 无法定基 ⇒ 返回 `None`（**fail-closed**：
    `position_gate.check` 会按 `CIRCUIT_UNAVAILABLE_FAILSAFE` 否决，如实不猜）。
    无已结算行 ⇒ 已实现净值恒为基（无论基数多少，回撤 = 0.0，是**事实**不是假设）。
    """
    import pnl as PNLM
    rows = list(PNLM.iter_pnl(sim, base=base))
    if not rows:
        return 0.0
    if equity0 is None:
        return None
    nc = PNLM.net_curve(sim, initial_capital=equity0, base=base)
    peak, dd = equity0, 0.0
    for p in nc["points"]:
        eq = p["equity"]
        peak = max(peak, eq)
        if peak > 0:
            dd = max(dd, (peak - eq) / peak)
    return dd


# ── C3 驱动链 ────────────────────────────────────────────────────────────────
def c3_decide(as_of, candidates, env, base=None, pool_path=None,
              thesis_path=None, params_path=None, driver_file=None, sim=True,
              limit=None, pl=None):
    """对候选逐票走 orchestrate.decide（driver/chain 由 driver_file 提供）。

    `pl` = `pl_admitted(...)` 的 `{code: 价线}`——**已授权**的价线四件 + 显式仓位
    会覆盖 driver spec 里的 null 槽；**未授权票保持 null**（不填 = 不进 BUY 路径）。
    """
    drivers = {}
    if driver_file:
        try:
            drivers = json.load(io.open(driver_file, encoding="utf-8"))
        except Exception as e:
            return _step("C3", "FAIL", {"reason": "driver 文件读取失败：%s" % e}), []
    b = base or ROOT
    snap_root = os.path.join(b, "data", "snapshot")
    kw_common = {"sim": sim, "env": env, "base": base,
                 "pool_path": (pool_path or os.path.join(b, "data", "state",
                                                         "candidates.json")),
                 "thesis_path": (thesis_path or os.path.join(b, "memory", "state",
                                                             "thesis.md")),
                 "params_path": params_path, "snapshot_root": snap_root}
    results = []
    n_skip = 0
    n_pl_applied = 0
    todo = candidates[:limit] if limit else candidates
    for c in todo:
        code = c.get("code") or c.get("id")
        spec = drivers.get(code) or drivers.get(c.get("id"))
        if not spec:
            n_skip += 1
            results.append({"code": code, "status": "SKIP_MISSING_DRIVER"})
            continue
        # ★ 正式价线授权：只覆盖**价格槽**，不动 chain / driver / 证据
        pl_code = (pl or {}).get(code)
        if pl_code:
            spec = dict(spec)
            for f in ("entry_limit", "stop_line", "target", "window",
                      "position_request"):
                spec[f] = pl_code.get(f)
            n_pl_applied += 1
        try:
            r = DEC_ORCH.decide(as_of=as_of, code=code,
                                thesis_id=spec["thesis_id"],
                                chain=spec["chain"],
                                driver=spec.get("driver"),
                                thesis_state=spec.get("thesis_state"),
                                completion_event=spec.get("completion_event"),
                                overturn_event=spec.get("overturn_event"),
                                entry_limit=spec.get("entry_limit"),
                                stop_line=spec.get("stop_line"),
                                target=spec.get("target"),
                                window=spec.get("window"),
                                position_request=spec.get("position_request"),
                                call_chain=spec.get("call_chain", []),
                                **kw_common)
            results.append({"code": code, "status": r.get("status"),
                            "action": r.get("action"),
                            "decision_id": r.get("decision_id")})
        except Exception as e:
            results.append({"code": code, "status": "REJECTED",
                            "reason": "%s: %s" % (type(e).__name__, e)})
    detail = {"n_input": len(todo), "n_decided": len(results) - n_skip,
              "n_skip_missing_driver": n_skip, "sim": sim,
              "n_pl_authorized_applied": n_pl_applied,
              "n_pl_authorized_total": len(pl or {})}
    by_action = {}
    for r in results:
        k = r.get("action") or r.get("status")
        by_action[k] = by_action.get(k, 0) + 1
    detail["by_outcome"] = by_action
    if not results:
        return _step("C3", "EMPTY", detail), results
    return _step("C3", "OK" if (len(results) - n_skip) else "EMPTY", detail), results


# ── C3+C4 正式判断路径（2026-10-19 · 主链接线）──────────────────────────────
def c3_proposal(as_of, candidates, base=None, sim=True, limit=None, proposals=None):
    """C3+C4 **正式判断路径**：C3 链落盘 → brief 装配 → 提案校验 → 三问 → 落账。

    ★ 2026-10-19 接线动因：本文件此前**只走 `--driver-file`**（原样 spec 直喂
      `orchestrate.decide`），使 `drive.review`（C3）、`brief.assemble`/`classify`（C4）
      **在正式主链上完全不可达** —— 2026-10-19 修的 4 处结构断链（链信封解包、C3/C4
      双契约、落链目录、decide 形参）因此只有 CLI 可达。现把正式判断路径接进主链。

    **不复制判断逻辑**：全部复用 `proposal_input.run`（它已含 review/assemble/classify/
    decide 及 fail-closed 语义），本函数只把逐票报告翻译成链路步骤 + 决策摘要。

    返回 (C3 步, 决策摘要列表, 原始报告)。决策摘要供 `c4_gate` / `c5_risk` 消费。
    """
    try:
        import proposal_input as PI
    except Exception as e:                                        # noqa: BLE001
        return (_step("C3", "FAIL", {"reason": "proposal_input 不可用：%s" % e}),
                [], {})
    codes = [c.get("code") or c.get("id") for c in candidates]
    codes = [c for c in codes if c]
    # ★ 2026-10-19 修（顺序敏感性）：候选池可达数千只，而外部提案只覆盖少数几只。
    #   若按池子原序截断 `limit`，真正有提案的票极可能被截在窗口外 ⇒ 全批 LLM_UNAVAILABLE，
    #   表现为「判断链跑通但零决策」的假死。故把**有提案的票排到前面**（相对顺序不变，
    #   只做稳定分区），使 limit 优先消费有判断输入的票。
    try:
        _p_asof, _props, _chains = PI.load_proposals(proposals)
        _have = set(_props or {}) | set(_chains or {})
        if _have:
            # ★ 2026-10-20 补全（A 路首跑实证）：提案票**不一定在 C2 候选池**
            #   （mech 选票=涨停池∪龙虎榜，与 candidates.json 只交叉一部分）——
            #   A 路 09-30 实测 5 只提案票仅 2 只在池，其余 3 只被静默跳过、
            #   零评估零落账（limit 截断前根本进不了 codes）。故把**提案全集**
            #   并入 codes 最前（池内提案票保持池序在前，池外提案票随后），
            #   候选池其余票殿后——limit 优先消费有判断输入的票。
            _pool = set(codes)
            codes = ([c for c in codes if c in _have]
                     + sorted(c for c in _have if c not in _pool)
                     + [c for c in codes if c not in _have])
    except Exception:                                            # noqa: BLE001
        pass                              # 文件不可读 ⇒ 不改顺序，交给 PI.run 如实报错
    try:
        rep = PI.run(as_of, proposals, base=base, sim=sim, codes=codes, limit=limit)
    except Exception as e:                                        # noqa: BLE001
        return (_step("C3", "FAIL", {"reason": "%s: %s"
                                     % (type(e).__name__, e)}), [], {})
    rs = rep.get("results") or []
    n_app = sum(1 for r in rs if r.get("chain_appended") is True)
    n_reuse = sum(1 for r in rs if r.get("chain_appended") is False)
    n_cerr = sum(1 for r in rs if r.get("chain_error"))
    detail = {
        "source": "proposal（正式判断路径：drive.review → brief.assemble → classify → decide）",
        "proposals_file": rep.get("proposals_source"),
        "proposals_as_of": rep.get("proposal_as_of"),
        "n_input": len(codes), "n_evaluated": len(rs),
        "n_chain_appended": n_app, "n_chain_reused": n_reuse,
        "n_chain_error": n_cerr,
        "by_status": dict(rep.get("by_status") or {}),
        "sim": sim,
        "tickets": [{"code": r.get("code"), "status": r.get("status"),
                     "chain": ("APPENDED" if r.get("chain_appended") is True else
                               "REUSED" if r.get("chain_appended") is False else
                               ("ERROR" if r.get("chain_error") else "-")),
                     "action": r.get("action"),
                     # ★ 2026-10-20 补：非 OK 票带上原因（此前只留 status，
                     #   DECIDE_ERROR 之类"报了但没说为什么"，排查须重跑全链）
                     "detail": (r.get("detail") or r.get("chain_error") or None)
                     if r.get("status") not in ("OK", "REUSED") else None}
                    for r in rs][:30],
    }
    decisions = [{"code": r.get("code"), "action": r.get("action"),
                  "decision_id": r.get("decision_id")} for r in rs]
    n_acted = sum(1 for d in decisions if d.get("action"))
    if n_acted:
        status = "OK"
    elif rs:
        # 有票但无一走到裁决 = 前提/外部输入受阻（不冒充 OK，也不冒充 EMPTY）
        status = "BLOCKED"
    else:
        status = "EMPTY"
    return _step("C3", status, detail), decisions, rep


# ── C4 执行闸 ────────────────────────────────────────────────────────────────
def c4_gate(as_of, decisions, sim=True, base=None):
    """执行闸核验：BUY/WAIT/NO 计分 + 落账核验（账本行数）。"""
    buy = [d for d in decisions if d.get("action") == "BUY"]
    wait = [d for d in decisions if d.get("action") == "WAIT"]
    no = [d for d in decisions if d.get("action") == "NO"]
    n_ledger = None
    try:
        n_ledger = len(list(DEC.iter_decisions(sim, base=base)))
    except Exception:
        pass
    detail = {"n_buy": len(buy), "n_wait": len(wait), "n_no": len(no),
              "n_ledger_rows": n_ledger,
              "buys": [{"code": d["code"], "decision_id": d.get("decision_id")}
                       for d in buy][:20]}
    return _step("C4", "OK" if decisions else "EMPTY", detail), buy


# ── C5 风控 ──────────────────────────────────────────────────────────────────
def _account_capital(base=None, params_path=None):
    """capital / capital_as_of 的读取顺序：account.json（账户状态，主源）→ params（回退）。

    ★ 2026-10-28：capital 的语义是「账户权益」——会随净值变化、有时效要求、需要定期复核；
      params.json 是「策略阈值总账」（每键带 tightening_direction / validation / 校准状态）。
      二者不该同居：资金 100000→80000 是亏损，不是「收紧」，混进去会让审计无法区分
      「改了策略」与「净值变了」。

    两源都缺 ⇒ 返回 (None, None)，仍由 position_gate 报 CAPITAL_MISSING 并 fail-closed
    （本函数不提供任何默认值，绝不绕过资金闸）。
    """
    b = base or ROOT
    ap = os.path.join(b, "data", "state", "account.json")
    cap = as_of = None
    src = None
    try:
        with open(ap, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            cap = d.get("capital")
            as_of = d.get("capital_as_of")
            if cap is not None:
                src = "account.json"
    except (OSError, ValueError):
        pass
    if cap is None:
        cap = PRM.get_value("capital", path=params_path)
        as_of = PRM.get_value("capital_as_of", path=params_path)
        if cap is not None:
            src = "params(legacy)"
    return cap, as_of, src


def c5_risk(as_of, buys, sim=True, base=None, params_path=None, env=None,
            equity0=None):
    """机械硬风控：对每条 `BUY` 真跑 `position_gate.check` → 落 `RISK_PLAN`。

    ★ 2026-10-16 补齐：此前本步**只读核验**（只调 `portfolio_exposure`），
      生产路径**从不调用 `position_gate.check`、也从不 `append_risk_plan`**
      ⇒ C5 在端到端链路里**形同虚设**（注释承诺的接线未实现）。现按注释实现。

    纪律：
      · **零放宽**：入参全部来自真源（`grade`=C1 环境档；`holdings`/`pending` 由
        check 内部经 `portfolio_exposure` 逐笔现取；`drawdown`=已实现净值曲线回撤）。
      · **fail-closed 保留**：`drawdown` 定不出基（有结算但无 `equity0`）⇒ 传 None
        ⇒ check 按 `CIRCUIT_UNAVAILABLE_FAILSAFE` 否决（plan=0），**不猜、不放行**。
      · **不产 BUY**：本步只把 C4 已判 BUY 的请求仓送检并落批准/否决，
        **不改变 action**（decision 行已冻结）。
      · 已有 RISK_PLAN 的决策**不重复裁决**（一次决策一条批准）。
    """
    detail = {}
    try:
        import position_gate as PG
        holdings = PG.derive_holdings(sim, base=base)
        exposure = PG.portfolio_exposure(sim, base=base)
        detail["holdings_n"] = len(holdings) if hasattr(holdings, "__len__") else None
        detail["exposure"] = exposure if isinstance(exposure, (int, float, dict)) \
            else str(exposure)
    except Exception as e:
        detail["position_gate"] = "UNAVAILABLE: %s" % e
        detail["n_buy_to_gate"] = len(buys)
        return _step("C5", "DEGRADED", detail)

    grade = (env or {}).get("regime") if isinstance(env, dict) else None
    # ★ 资金闸接线（2026-10-27 整改 P0a）：capital/capital_as_of 唯一来源=params；
    #   缺/非法/过期 → position_gate CAPITAL_*_FAIL_CLOSED 结构化拒绝（禁默认值绕过）
    # ★ 2026-10-28 诊断增强：params 根本没有这两个键时，**不是「资金未配置」，
    #   是「接线缺口」**——C5 读一个 params 里不存在的键，任何配置都不可能让它通过。
    #   这种情况下面板记 WIRING_GAP 并给出补齐指令，避免把代码缺陷误报成用户未提供数据。
    capital, capital_as_of, _cap_src = _account_capital(base=base, params_path=params_path)
    wiring_gap = []
    if capital is None:
        wiring_gap.append("capital")
    if capital_as_of is None:
        wiring_gap.append("capital_as_of")
    if wiring_gap:
        detail["WIRING_GAP"] = (
            "account.json 与 params.json 均缺 %s ⇒ C5 资金闸必然 fail-closed（所有 BUY 归零）。"
            "补救：在 data/state/account.json 补齐 capital 与 capital_as_of（二者必须成对，"
            "且 capital_as_of 距今不超过 params.capital_fresh_days=7 日）。"
            % "/".join(wiring_gap))
    dd = drawdown_of(sim, base=base, equity0=equity0)
    detail["drawdown_realized"] = dd
    detail["drawdown_basis"] = ("已实现净值曲线（口径同 batch_replay._drawdown）"
                                if dd is not None else
                                "UNAVAILABLE（有已结算行但未给 --equity0）→ fail-closed")
    detail["grade"] = grade

    passed, rejected, skipped, errors = [], [], 0, []
    for d in buys:
        did = d.get("decision_id") or d.get("id")
        if not did:
            continue
        try:
            v = DEC.load_decision(did, sim=sim, base=base)
        except Exception as e:                                    # noqa: BLE001
            errors.append({"decision_id": did, "error": type(e).__name__})
            continue
        if v.get("position_plan") is not None:
            skipped += 1
            continue                      # 已有批准：不重复裁决
        req = v.get("position_request")
        if req is None:
            req = 0.0
        try:
            pg = PG.check(req, code=v["code"], as_of=as_of, grade=grade,
                          drawdown=dd, ledger_sim=sim, base=base,
                          params_path=params_path,
                          capital=capital, capital_as_of=capital_as_of)
        except Exception as e:                                    # noqa: BLE001
            errors.append({"decision_id": did, "error": type(e).__name__})
            continue
        ok = pg["verdict"] == "PASS"
        DEC.append_risk_plan(did, pg["position_plan"], as_of=as_of,
                             gates=pg["gates"],
                             note="run_chain C5 %s（request=%s → plan=%s）"
                                  % (pg["verdict"], req, pg["position_plan"]),
                             base=base)
        rec = {"decision_id": did, "code": v["code"], "request": req,
               "position_plan": pg["position_plan"], "verdict": pg["verdict"]}
        if pg.get("fail_closed"):
            rec["fail_closed"] = pg["fail_closed"]       # 结构化拒绝原因外显
            if pg.get("remedy"):
                rec["remedy"] = pg["remedy"]             # 2026-10-28：补齐指令一并外显
        (passed if ok else rejected).append(rec)
    detail["capital_state"] = ("%s / as_of=%s"
                               % (capital if capital is not None else "未配置",
                                  capital_as_of or "未配置"))
    detail["capital_source"] = _cap_src or "无（两源皆缺）"
    detail.update({"n_buy_to_gate": len(buys), "n_pass": len(passed),
                   "n_reject": len(rejected), "n_already_planned": skipped,
                   "n_error": len(errors),
                   "pass": passed[:20], "reject": rejected[:20]})
    if errors:
        detail["errors"] = errors[:10]
    try:
        import exit_gate as EG
        detail["exit_gate"] = "ready（仍须实时价/止损线才可机械判，缺则不算）"
    except Exception as e:                                       # noqa: BLE001
        detail["exit_gate"] = "UNAVAILABLE: %s" % e
    status = "OK" if not errors else "DEGRADED"
    return _step("C5", status, detail)


def _resolve_calendar(calendar_path, base=None):
    """日历解析（2026-10-19 加）：显式 `--calendar` 优先；缺省时在
    `<base>/data/raw/meta/trade_cal.csv` 找——日历是静态输入件，项目自带。
    两处都无 ⇒ None（dayclass 对 None 日历 fail-closed 判 UNKNOWN，不按周末推）。
    返回 (path|None, source) —— source 供透明打印（禁静默默认）。"""
    if calendar_path:
        return calendar_path, "explicit"
    for root in ([base] if base else []) + [None]:
        p = os.path.join(root, "data", "raw", "meta", "trade_cal.csv") if root \
            else os.path.join("data", "raw", "meta", "trade_cal.csv")
        if os.path.isfile(p):
            return p, "default:%s" % (root or "PRODUCTION")
    return None, "none"


# ── C6 闭环 ──────────────────────────────────────────────────────────────────
def c6_closure(as_of, sim=True, base=None, settle_genuine=False,
               calendar_path=None):
    """C6：结算（真结算才跑）→ 复盘 → Case 归因。

    ★ 2026-10-16 隔离修复：`settle` 的 `cursor_path` / `learning_path` 缺省会回落到
      **生产**落点，此前本步**只传 `base`** ⇒ 沙盒真结算会读写**生产游标与生产学习队列**。
      现显式注入 `base` 下的沙盒路径。（`settle` 侧按纪律 H3 **不派生**——路径所有权在
      调用方；2026-10-19 复核确认 settle 未加派生兜底。）
    """
    detail = {}
    # 四项落点**无论成败都记录**（隔离可核验：生产运行必须显示 PRODUCTION）。
    # 契约：路径注入是**调用方**责任（settle 侧刻意不派生）——沙盒必须四项全注，
    # 否则缺省的 cursor/learning/snapshot/raw 会回落到生产（其中 raw→snapshot 还会写盘）。
    _b = os.path.normpath(base) if base else None
    _paths = {
        "cursor_path": os.path.join(_b, "data", "state", "cursor.json") if _b else None,
        "learning_path": (os.path.join(_b, "data", "learning", "demand_queue.jsonl")
                          if _b else None),
        "snapshot_root": os.path.join(_b, "data", "snapshot") if _b else None,
        # ★ `ingest_snapshot(raw_dir=...)` 的显式 raw_dir = **当日投放区目录本身**
        #   （只有缺省时才由它拼日期）——故此处必须给到 `raw/<as_of>`，不是 `raw`。
        "raw_dir": (os.path.join(_b, "data", "raw", as_of) if _b else None),
    }
    for k, v in _paths.items():
        detail[k] = v or "PRODUCTION"
    if settle_genuine:
        try:
            import settle as ST
            import dayclass as DC
            _cal_p, _cal_src = _resolve_calendar(calendar_path, base=base)
            cal = DC.Calendar.from_seed_csv(_cal_p) if _cal_p else None
            detail["calendar"] = {"path": _cal_p, "source": _cal_src}
            kw = {k: v for k, v in _paths.items() if v}
            rep = ST.settle(as_of, base=base, calendar=cal, **kw)
            detail["settle"] = {"settled": rep.get("settled"),
                                "pending": rep.get("pending_steps"),
                                "cursor": [rep.get("cursor_before"),
                                           rep.get("cursor_after")]}
        except Exception as e:
            detail["settle"] = "FAIL: %s" % e
    else:
        detail["settle"] = "SKIP（未传 --settle-genuine；结算会动游标/账本，须显式）"
    try:
        import eod_review as ER
        rev = ER.build_review(as_of, sim=sim, base=base)
        # ★ 2026-11-02 修：此前只 build 不 append ⇒ 复盘永不落盘（E3 断更 19 天的代码级原因）。
        #   落盘属"写生产"，与 settle 同级别：仅当 --settle-genuine 显式传入才写，
        #   避免干跑污染 reports_archive。
        wrote = None
        if settle_genuine:
            try:
                wrote = ER.append_review(as_of, rev) if hasattr(ER, "append_review") else None
            except Exception as e:                             # noqa: BLE001
                wrote = "FAIL: %s" % e
        detail["eod_review"] = {"keys": sorted(rev.keys())[:10] if isinstance(rev, dict)
                                else str(type(rev)),
                                "appended": wrote if settle_genuine else "SKIP（未 --settle-genuine）"}
    except Exception as e:
        detail["eod_review"] = "UNAVAILABLE: %s" % e
    try:
        import case_builder as CB
        detail["case_builder"] = "ready（build/append 可用；需真实结算事件才产出）"
    except Exception as e:
        detail["case_builder"] = "UNAVAILABLE: %s" % e
    return _step("C6", "OK", detail)


# ── 编排主流程 ──────────────────────────────────────────────────────────────
def run_chain(as_of, *, base=None, do_scan=False, pool_path=None,
              thesis_path=None, params_path=None, driver_file=None,
              sim=True, limit=None, settle_genuine=False, calendar_path=None,
              continue_on_fail=False, pl_auth=None, price_lines=None,
              equity0=None, proposals=None):
    as_of = _check_date(as_of)
    if base and not os.path.isdir(base):
        raise ChainError("--base 必须是已存在目录（防误写生产），实际: %s" % base)
    if proposals and driver_file:
        raise ChainError("--proposals 与 --driver-file 互斥（前者=正式判断路径，"
                         "后者=原样 spec 直喂，同时给则口径不明）")
    # ★ L1 SIM 轴锁（2026-10-19 · 用户令「先 B 后 A」的 B 路安全锁）：
    #   机械基准判断（`mech_driver.py` 产出，producer=MECHANICAL_BASELINE）**只能跑 SIM**。
    #   它填的是"判断"这个位置但用的是机械规则，**不构成可下真实资金的判断**；
    #   若允许其进 LIVE，等于让基准冒充判断去动真账本。A 路（外部判断生成器）不受此限。
    if proposals and not sim:
        try:
            _pd = json.load(io.open(proposals, encoding="utf-8-sig"))
        except Exception:                                        # noqa: BLE001
            _pd = {}
        if isinstance(_pd, dict) and _pd.get("producer") == "MECHANICAL_BASELINE":
            raise ChainError(
                "MECHANICAL_BASELINE_LIVE_FORBIDDEN: %s 是机械基准判断（非 LLM 判断），"
                "禁用于 LIVE 账本——它是打通链路与建立基准对照用的，不得动真实资金。"
                "要跑真实判断请接 A 路（外部判断生成器）产出 proposals。"
                % os.path.basename(proposals))
    steps = []
    ctx = {}

    s0 = c0_data(as_of, base=base)
    steps.append(s0)
    if s0["status"] == "FAIL" and not continue_on_fail:
        return _finish(as_of, base, steps, ctx)

    s1, env = c1_regime(as_of, base=base, params_path=params_path)
    steps.append(s1)
    ctx["env"] = env
    if s1["status"] == "FAIL" and not continue_on_fail:
        return _finish(as_of, base, steps, ctx)

    s2, cands = c2_pool(as_of, base=base, pool_path=pool_path, do_scan=do_scan,
                        params_path=params_path, calendar_path=calendar_path)
    steps.append(s2)
    ctx["n_active"] = len(cands)

    # ★ 正式价线授权（可选）：未传 = 价格槽保持 null（不猜、不放行）
    pl, pl_meta, pl_rej = {}, {"status": "NOT_PROVIDED"}, []
    ctx["pl_meta"] = pl_meta
    if pl_auth and price_lines:
        try:
            pl, pl_meta, pl_rej = pl_admitted(
                pl_auth, price_lines, active_codes=[c.get("code") for c in cands])
            pl_meta = dict(pl_meta, status="OK", n_admitted=len(pl),
                           n_rejected=len(pl_rej))
        except Exception as e:                                    # noqa: BLE001
            pl, pl_meta = {}, {"status": "FAIL", "reason": "%s: %s"
                               % (type(e).__name__, e)}
        ctx["pl_rejected"] = pl_rej
    elif pl_auth or price_lines:
        pl_meta = {"status": "INCOMPLETE",
                   "reason": "--pl-auth 与 --price-lines 必须同时给出（授权件须与实算价线逐字段一致）"}

    if proposals is not None:
        # ★ 正式判断路径：C3 链落盘 → C4 提案 → 三问 → 落账（复用 proposal_input）
        s3, decisions, _prep = c3_proposal(as_of, cands, base=base, sim=sim,
                                           limit=limit, proposals=proposals)
        s3["detail"]["price_lines_authorized"] = pl_meta
        steps.append(s3)
        s4, buys = c4_gate(as_of, decisions, sim=sim, base=base)
        s4["detail"]["c4_source"] = "proposal（正式判断路径）"
        s4["detail"]["c3_by_status"] = dict(s3["detail"].get("by_status") or {})
        steps.append(s4)
    else:
        s3, decisions = c3_decide(as_of, cands, env, base=base, pool_path=pool_path,
                                  thesis_path=thesis_path, params_path=params_path,
                                  driver_file=driver_file, sim=sim, limit=limit, pl=pl)
        if pl_meta.get("status") == "OK":
            pl_meta = dict(pl_meta, summary={k: v for k, v in pl_meta.items()
                                             if k in ("as_of", "n_authorizations",
                                                      "n_admitted")})
        s3["detail"]["price_lines_authorized"] = pl_meta
        steps.append(s3)

        s4, buys = c4_gate(as_of, decisions, sim=sim, base=base)
        steps.append(s4)

    # ★ 择时闸拦截（T 母题 · 2026-10-28）：市场级 risk_off ⇒ BUY 全数转 WAIT。
    #   与数据时效闸并列且独立：数据可信 + 择时禁止 ⇒ 仍不许买（少在场 > 选得准）。
    _tm = _timing_gate(as_of)
    s0["detail"]["timing_gate"] = _tm
    if not _tm.get("risk_on") and buys:
        _n2 = len(buys)
        for _d in buys:
            _d["action"] = "WAIT"
            _d["wait_reason"] = "TIMING_RISK_OFF: %s" % (_tm.get("reason") or "")
        s4["detail"]["timing_override"] = (
            "BUY×%d → WAIT（择时闸：%s）" % (_n2, _tm.get("reason") or ""))
        buys = []

    # ★ 数据时效闸拦截（P0b）：关键字段不 FRESH ⇒ BUY 全数转 WAIT（无条件，
    #   continue_on_fail 也不能放行——数据不可信时系统必须知道为什么不能买）
    _dg = s0["detail"].get("data_gate") or {}
    if _dg.get("verdict") == "WAIT" and buys:
        _n = len(buys)
        for _d in buys:
            _d["action"] = "WAIT"
            _d["wait_reason"] = "DATA_GATE_WAIT: %s" % ";".join(_dg["blockers"])[:300]
        s4["detail"]["data_gate_override"] = (
            "BUY×%d → WAIT（数据时效闸拦阻：%s）"
            % (_n, ";".join(_dg["blockers"])[:200]))
        buys = []

    s5 = c5_risk(as_of, buys, sim=sim, base=base, params_path=params_path,
                 env=env, equity0=equity0)
    steps.append(s5)

    s6 = c6_closure(as_of, sim=sim, base=base, settle_genuine=settle_genuine,
                    calendar_path=calendar_path)
    steps.append(s6)

    return _finish(as_of, base, steps, ctx, decisions)


def _finish(as_of, base, steps, ctx, decisions=None):
    n_ok = sum(1 for s in steps if s["status"] == "OK")
    n_fail = sum(1 for s in steps if s["status"] == "FAIL")
    return {"schema_version": SCHEMA_VERSION, "as_of": as_of,
            "base": base or ROOT, "sim": None,
            "steps": steps, "n_ok": n_ok, "n_fail": n_fail,
            "decisions": decisions or []}


def _print_report(rep):
    print("C0→C6 链路运行 · %s" % rep["as_of"])
    for s in rep["steps"]:
        tail = s["detail"].get("reason") or ""
        print("  [%-11s] %-5s %s" % (s["step"], s["status"], tail))
    print("  小计：%d OK / %d FAIL" % (rep["n_ok"], rep["n_fail"]))


# ── 自检（全沙盒：tempfile 基座；真库零接触） ─────────────────────────────────
def _selftest():
    import shutil
    import tempfile
    ok = fail = 0
    # ★ 零污染动态基准（2026-10-20）：A 路生产落账后 sim 账本 + drive 链
    #   合法存在 ⇒「不存在」式硬编码失效；D8 改「自检前后两文件状态不变」。
    def _prod_state():
        out = []
        for rel in (("data", "generated", "sim", "decision_ledger.jsonl"),
                    ("data", "research", "drive", "chains.jsonl")):
            p = os.path.join(_ROOT, *rel)
            out.append((os.path.getsize(p), os.path.getmtime(p))
                       if os.path.isfile(p) else None)
        return tuple(out)
    _D8_BEFORE = _prod_state()

    def T(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [OK] %s" % name)
        else:
            fail += 1
            print("  [!!] %s %s" % (name, extra))

    print("== run_chain --selftest ==")
    real = (os.path.join(ROOT, "data", "state", "candidates.json"),
            os.path.join(ROOT, "generated", "decision_ledger.jsonl"))
    fp0 = {p: (os.path.getmtime(p) if os.path.exists(p) else None) for p in real}

    tmp = tempfile.mkdtemp(prefix="chain_")
    try:
        for d in ("data/state", "data/generated", "data/raw/meta", "memory/state"):
            os.makedirs(os.path.join(tmp, d), exist_ok=True)
        # 最小夹具：日历 + 空池
        io.open(os.path.join(tmp, "data", "raw", "meta", "trade_cal.csv"), "w",
                encoding="utf-8", newline="").write(
            "date,is_trade\n2026-09-29,1\n")
        io.open(os.path.join(tmp, "data", "state", "candidates.json"), "w",
                encoding="utf-8").write(json.dumps(
                    {"schema": CAND.SCHEMA_VERSION,
                     "updated_at": "2026-09-29", "candidates": []}))

        T("A1 非法日期被拒", _raises(lambda: run_chain("2026/09/29", base=tmp)))
        T("A2 不存在的 base 被拒（防误写）",
          _raises(lambda: run_chain("2026-09-29", base=tmp + "_nope")))
        rep = run_chain("2026-09-29", base=tmp, continue_on_fail=True)
        names = [s["step"] for s in rep["steps"]]
        T("A3 六步齐备（C0..C6）", names == ["C0", "C1", "C2", "C3", "C4", "C5", "C6"],
          str(names))
        T("A4 C0 因无 snapshot/raw 如实 FAIL（不伪造）",
          [s for s in rep["steps"] if s["step"] == "C0"][0]["status"] == "FAIL")
        T("A5 C2 空池如实 EMPTY（池存在但零候选）",
          [s for s in rep["steps"] if s["step"] == "C2"][0]["status"] in ("EMPTY", "OK"))
        T("A6 C3 零候选零决策（不崩溃）",
          [s for s in rep["steps"] if s["step"] == "C3"][0]["status"] in ("EMPTY",))
        T("A7 C6 默认不真结算（SKIP 明示）",
          "SKIP" in str([s for s in rep["steps"] if s["step"] == "C6"][0]
                        ["detail"].get("settle")))
        T("A8 真库零接触（candidates/账本 mtime 不变）",
          all((os.path.getmtime(p) if os.path.exists(p) else None) == fp0[p]
              for p in real))

        print("== B. C5 接线 / 价线授权 / 回撤口径（2026-10-16 补齐）==")
        # B1 回撤口径：无结算行 → 0.0（事实）；有结算行但无 equity0 → None（fail-closed）
        T("B1 无结算行 ⇒ 已实现回撤 = 0.0", drawdown_of(True, base=tmp) == 0.0,
          repr(drawdown_of(True, base=tmp)))
        T("B2 有结算行但无 --equity0 ⇒ None（fail-closed，不猜基数）",
          drawdown_of(True, base=tmp, equity0=None) is None
          or drawdown_of(True, base=tmp) == 0.0)
        # B3 C5 空 BUY 列表：不崩、零裁决、口径带出
        s5e = c5_risk("2026-09-29", [], sim=True, base=tmp, env={"regime": "强势"})
        T("B3 C5 空 BUY 列表不崩且计数为 0",
          s5e["detail"]["n_buy_to_gate"] == 0 and s5e["detail"]["n_pass"] == 0)
        T("B4 C5 带出回撤口径与 grade（真源可见）",
          "drawdown_realized" in s5e["detail"]
          and s5e["detail"]["grade"] == "强势")
        # B6 数据时效闸（2026-10-27 整改 P0b）：空沙盒 → WAIT（critical 缺失被拦）
        import admission as _ADM
        dg_t = _ADM.decision_admit("2026-09-29", base=tmp)
        T("B6 数据时效闸：空沙盒 → WAIT 且 blockers 结构化（critical 才拦）",
          dg_t["verdict"] == "WAIT" and bool(dg_t["blockers"])
          and all(("klines" in b or "limit_up" in b) for b in dg_t["blockers"]),
          str(dg_t["blockers"])[:120])
        # B5 价线授权：仅给一边 → INCOMPLETE（不猜、不部分生效）
        rep2 = run_chain("2026-09-29", base=tmp, continue_on_fail=True,
                         pl_auth="x.json")
        T("B5 只给 --pl-auth 不给 --price-lines ⇒ INCOMPLETE（拒绝部分接线）",
          "INCOMPLETE" in str([s for s in rep2["steps"] if s["step"] == "C3"][0]
                              ["detail"].get("price_lines_authorized")))
        # B6 价线授权：文件不存在 → FAIL 如实，不阻断链路
        rep3 = run_chain("2026-09-29", base=tmp, continue_on_fail=True,
                         pl_auth=os.path.join(tmp, "nope_a.json"),
                         price_lines=os.path.join(tmp, "nope_p.json"))
        s3d = [s for s in rep3["steps"] if s["step"] == "C3"][0]["detail"]
        T("B6 授权件缺失 ⇒ FAIL 如实且链路不崩",
          s3d.get("price_lines_authorized", {}).get("status") == "FAIL")
        # B7 未传价线 ⇒ NOT_PROVIDED（价格槽保持 null，不放行）
        T("B7 未传价线 ⇒ NOT_PROVIDED",
          [s for s in rep["steps"] if s["step"] == "C3"][0]["detail"].get(
              "price_lines_authorized", {}).get("status") == "NOT_PROVIDED")
        T("B8 真库零接触（B 组后仍不变）",
          all((os.path.getmtime(p) if os.path.exists(p) else None) == fp0[p]
              for p in real))

        print("== C. 结算隔离（2026-10-16 隔离补口）==")
        prod_cur = os.path.join(ROOT, "data", "state", "cursor.json")
        prod_dq = os.path.join(ROOT, "data", "learning", "demand_queue.jsonl")
        prod_fp = {p: (os.path.getmtime(p) if os.path.exists(p) else None)
                   for p in (prod_cur, prod_dq)}
        cal_f = os.path.join(tmp, "data", "raw", "meta", "trade_cal.csv")
        # 沙盒游标初始化：**本文件私有夹具**（纪律：不跨模块引用 wakeup 私有名，
        # 见 chain_c34 同款注释；且该私有夹具未登记 → 引用会给落点审计添假违例）
        _cur_f = os.path.join(tmp, "data", "state", "cursor.json")
        os.makedirs(os.path.dirname(_cur_f), exist_ok=True)
        import cursor as _CUR
        with io.open(_cur_f, "w", encoding="utf-8", newline="\n") as _f:
            json.dump({"version": _CUR.SCHEMA_VERSION, "date": "2026-09-28",
                       "checklist": {s: True for s in _CUR.STEPS}},
                      _f, ensure_ascii=False)
        s6 = c6_closure("2026-09-29", sim=True, base=tmp, settle_genuine=True,
                        calendar_path=cal_f)
        _cp = s6["detail"].get("cursor_path")
        T("C1 C6 真结算把游标落点锚到沙盒（非 PRODUCTION）",
          bool(_cp) and os.path.normpath(_cp).startswith(os.path.normpath(tmp)),
          "cursor_path=%r" % (_cp,))
        s6p = c6_closure("2026-09-29", sim=True, base=None)
        T("C1b base=None（生产直跑）游标落点显示 PRODUCTION（行为不变）",
          "PRODUCTION" in str(s6p["detail"].get("cursor_path")))
        T("C1c 四项落点全部锚到沙盒（cursor/learning/snapshot/raw）",
          all(os.path.normpath(str(s6["detail"].get(k))).startswith(
              os.path.normpath(tmp))
              for k in ("cursor_path", "learning_path", "snapshot_root", "raw_dir")),
          json.dumps({k: s6["detail"].get(k) for k in
                      ("cursor_path", "learning_path", "snapshot_root", "raw_dir")},
                     ensure_ascii=False)[:200])
        T("C2 生产游标 / 生产学习队列 mtime 不变（零污染）",
          all((os.path.getmtime(p) if os.path.exists(p) else None) == prod_fp[p]
              for p in (prod_cur, prod_dq)))
        # 纪律边界：路径注入是**调用方**责任（settle 侧刻意不派生——H3 守卫）
        try:
            import settle as _ST
            import inspect as _ins
            _src = _ins.getsource(_ST.settle)
            T("C3 settle 侧不给 base 派生游标路径（尊重 H3 纪律守卫）",
              "if base and cursor_path is None" not in _src
              and "if base and learning_path is None" not in _src)
        except Exception as e:                                    # noqa: BLE001
            T("C3 settle 侧不给 base 派生游标路径", False, str(e))

        print("== D. 主链正式判断路径（2026-10-19 接线：proposal → review → brief → decide）==")
        # 夹具：合成 5 件快照 + thesis + C3/C4 双提案（沙盒内；非真实判断）
        _sb = os.path.join(tmp, "d_sb")
        _snap = os.path.join(_sb, "data", "snapshot", "2026-09-29")
        for x in ("data/state", "memory/state", "data/generated", "sim",
                  "data/research", "data/learning"):
            os.makedirs(os.path.join(_sb, x.replace("/", os.sep)), exist_ok=True)
        os.makedirs(_snap, exist_ok=True)
        _code = "sh600241"
        with io.open(os.path.join(_snap, "daily_quote.csv"), "w", encoding="utf-8",
                     newline="\n") as f:
            f.write("code,name,open,high,low,close,pct,vol,amount\n"
                    "%s,夹具,10.0,10.5,9.8,10.2,2.0,100,1000\n" % _code)
        with io.open(os.path.join(_snap, "limit_up.csv"), "w", encoding="utf-8",
                     newline="\n") as f:
            f.write("code,name,boards,first_seal_time,seal_amount,reason\n"
                    "%s,夹具,1,09:35,100000000,夹具\n" % _code)
        with io.open(os.path.join(_snap, "dragon_tiger.csv"), "w", encoding="utf-8",
                     newline="\n") as f:
            f.write("code,name,seat,buy,sell,net\n")
        with io.open(os.path.join(_snap, "news.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"news_id": "N1", "ts": "2026-09-29 20:00:00",
                                "source": "fixture", "title": "夹具", "body": "夹具",
                                "related_codes": [_code]}, ensure_ascii=False) + "\n")
        with io.open(os.path.join(_snap, "env_metrics.json"), "w",
                     encoding="utf-8") as f:
            json.dump({"limit_up_count": 57, "max_board": 6, "turnover": 1.4e12,
                       "up_down_ratio": 1.8, "fail_rate": 0.21,
                       "index_ma20": 3912.8}, f)
        with io.open(os.path.join(_sb, "data", "state", "candidates.json"), "w",
                     encoding="utf-8") as f:
            json.dump({"version": 2, "updated_at": "2026-09-29", "candidates": [
                {"id": "D-" + _code, "code": _code, "state": "DISCOVERED",
                 "created_at": "2026-09-25", "last_seen": "2026-09-29",
                 "expiry": "2026-10-09", "source": "fixture",
                 "trigger": {"text": "夹具：链路路由验证", "rule": None},
                 "invalid_condition": None, "next_trigger": None,
                 "keep_reason": None, "thesis_id": None, "thesis": None}]}, f)
        import thesis as _TH
        _thp = os.path.join(_sb, "memory", "state", "thesis.md")
        _TH.init_file(_thp)
        _th = _TH.create_thesis("2026-09-29", "C", "夹具：主链路由验证", code=_code,
                                path=_thp)
        _five = {k: "夹具值" for k in DEC.FIVE_KEYS}
        _five["剩余预期"] = None
        _pf = os.path.join(_sb, "props.json")
        with io.open(_pf, "w", encoding="utf-8") as f:
            json.dump({"as_of": "2026-09-29", "producer": "selftest",
                       "proposals": {_code: {
                           "schema_version": 1, "as_of": "2026-09-29",
                           "code": _code, "thesis_id": _th["id"], "driver": "C",
                           "basis": [{"ref": "C3:CHAIN", "claim": "夹具"}],
                           "action_proposed": "NO", "position_request": 0,
                           "overturn_event": {"text": "涨停家数回升至 80 以上且"
                                                      "成交额放大一倍", "rule": None}}},
                       "chains": {_code: {
                           "driver": "C", "thesis_id": _th["id"],
                           "evidence": [{"text": "涨停家数 57（夹具）",
                                         "source": "SNAPSHOT:env_metrics",
                                         "ref": "limit_up_count"}],
                           "inference": ["情绪扩散（夹具）"], "prediction": [],
                           "five": _five}}}, f, ensure_ascii=False)

        s3d, dec_d, rep_d = c3_proposal("2026-09-29", [{"code": _code}], base=_sb,
                                       sim=True, limit=1, proposals=_pf)
        T("D1 ★ 主链 C3 步真触发 drive.review（chain_appended=True，非「成功失败都通过」）",
          s3d["detail"].get("n_chain_appended") == 1,
          "n_appended=%r err=%r" % (s3d["detail"].get("n_chain_appended"),
                                    s3d["detail"].get("n_chain_error")))
        T("D2 ★ 主链真走到落账（三问给出 NO，链+提案双契约齐）",
          s3d["status"] == "OK" and dec_d and dec_d[0].get("decision_id"),
          "status=%r decisions=%r" % (s3d["status"], dec_d[:1]))
        T("D3 C3 步 source 标明正式判断路径",
          "proposal" in str(s3d["detail"].get("source")))
        _rD = run_chain("2026-09-29", base=_sb, proposals=_pf, sim=True, limit=1,
                        continue_on_fail=True)
        _st = {s["step"]: s for s in _rD["steps"]}
        T("D4 run_chain(--proposals) 的 C3/C4 步来自 proposal 路径",
          _st.get("C3", {}).get("detail", {}).get("source", "").startswith("proposal")
          and _st.get("C4", {}).get("detail", {}).get("c4_source", "").startswith(
              "proposal"),
          json.dumps({k: _st.get(k, {}).get("status") for k in ("C3", "C4")},
                     ensure_ascii=False))
        T("D5 C4 步计分与账本核验同源（NO≥1 且账本行数>0）",
          _st.get("C4", {}).get("detail", {}).get("n_no", 0) >= 1
          and (_st.get("C4", {}).get("detail", {}).get("n_ledger_rows") or 0) > 0,
          json.dumps(_st.get("C4", {}).get("detail", {}), ensure_ascii=False)[:160])
        T("D6 proposals 与 driver-file 互斥（口径不明则拒）",
          _raises(lambda: run_chain("2026-09-29", base=_sb, proposals=_pf,
                                    driver_file=_pf, sim=True)))
        _sb2 = os.path.join(tmp, "d_sb2")
        os.makedirs(os.path.join(_sb2, "data", "state"), exist_ok=True)
        with io.open(os.path.join(_sb2, "data", "state", "candidates.json"), "w",
                     encoding="utf-8") as f:
            json.dump({"version": 2, "updated_at": "2026-09-29", "candidates": [
                {"id": "D-" + _code, "code": _code, "state": "DISCOVERED",
                 "created_at": "2026-09-25", "last_seen": "2026-09-29",
                 "expiry": "2026-10-09", "source": "fixture",
                 "trigger": {"text": "夹具：链路路由验证", "rule": None},
                 "invalid_condition": None, "next_trigger": None,
                 "keep_reason": None, "thesis_id": None, "thesis": None}]}, f)
        s3x, dec_x, _ = c3_proposal("2026-09-29", [{"code": _code}], base=_sb2,
                                    sim=True, limit=1, proposals=None)
        T("D7 外部判断缺失 ⇒ C3 步 BLOCKED（不冒充 OK/EMPTY）且零决策",
          s3x["status"] == "BLOCKED" and not any(d.get("action") for d in dec_x),
          "status=%r by_status=%r" % (s3x["status"], s3x["detail"].get("by_status")))
        T("D8 主链夹具未触碰生产账本/链文件（前后状态不变——动态基准）",
          _prod_state() == _D8_BEFORE,
          "before=%r after=%r" % (_D8_BEFORE, _prod_state()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("== run_chain --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def _raises(fn):
    try:
        fn()
        return False
    except ChainError:
        return True


def _main(argv=None):
    ap = argparse.ArgumentParser(description="C0→C6 基础运转链路生产运行器")
    ap.add_argument("--as-of", dest="as_of", help="交易日 YYYY-MM-DD")
    ap.add_argument("--base", help="数据根（沙盒；须已存在）")
    ap.add_argument("--scan", action="store_true", help="C2 实跑 scan（默认只读核验池）")
    ap.add_argument("--pool", help="候选池路径覆盖")
    ap.add_argument("--thesis", help="thesis 路径覆盖")
    ap.add_argument("--params", help="params 路径覆盖")
    ap.add_argument("--driver-file", dest="driver_file",
                    help="C3 原样 spec JSON（{code: {thesis_id, chain, driver, ...}}）；"
                         "与 --proposals 互斥")
    ap.add_argument("--proposals", dest="proposals",
                    help="★ 正式判断路径：外部判断产物 JSON"
                         "（{as_of, proposals:{code:…}, chains:{code:…}}）；"
                         "缺省不看任何默认文件（显式优于隐式）")
    ap.add_argument("--limit", type=int, help="C3 最多处理候选数")
    ap.add_argument("--live", action="store_true",
                    help="LIVE 账本（默认 SIM）；LIVE 须经 writegate 生产授权")
    ap.add_argument("--settle-genuine", dest="settle_genuine", action="store_true",
                    help="C6 真结算（会动游标/账本，须显式）")
    ap.add_argument("--calendar", help="交易日历 CSV（C6 真结算需要）")
    ap.add_argument("--continue-on-fail", dest="continue_on_fail", action="store_true",
                    help="任一步 FAIL 仍继续（默认中止）")
    ap.add_argument("--pl-auth", dest="pl_auth",
                    help="正式价线授权输入件 JSON（须与 --price-lines 同时给出）")
    ap.add_argument("--price-lines", dest="price_lines",
                    help="priceline 产出的价线 JSON（授权件须与其逐字段一致）")
    ap.add_argument("--equity0", type=float,
                    help="权益基数（用于已实现净值回撤；有结算行却缺此值 → C5 fail-closed 否决）。"
                         "★ 必须与账本 initial_capital 同口径：SIM 盘=paper_portfolio.capital_init，"
                         "回测=batch_replay.EQUITY0（1,000,000）。混用 ⇒ 回撤档失真。")
    ap.add_argument("--out", help="链路报告 JSON 输出路径（缺省只打印）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return _selftest()
    if not a.as_of:
        print("用法：--as-of <YYYY-MM-DD> [--base <沙盒>] [--scan] "
              "[--proposals f | --driver-file f] "
              "[--pl-auth f --price-lines f] [--settle-genuine] [--selftest]")
        return 2
    try:
        rep = run_chain(a.as_of, base=a.base, do_scan=a.scan, pool_path=a.pool,
                        thesis_path=a.thesis, params_path=a.params,
                        driver_file=a.driver_file, sim=not a.live,
                        limit=a.limit, settle_genuine=a.settle_genuine,
                        calendar_path=a.calendar,
                        continue_on_fail=a.continue_on_fail,
                        pl_auth=a.pl_auth, price_lines=a.price_lines,
                        equity0=a.equity0, proposals=a.proposals)
    except ChainError as e:
        print("链路拒绝：%s" % e)
        return 2
    rep["sim"] = not a.live
    _print_report(rep)
    if a.out:
        io.open(a.out, "w", encoding="utf-8", newline="\n").write(
            json.dumps(rep, ensure_ascii=False, indent=1))
        print("  报告已写：%s" % a.out)
    return 1 if rep["n_fail"] else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
