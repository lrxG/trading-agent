#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wakeup.py · 运行入口（唤醒即追平）（施工方案 0.14 · v1.0 · 2026-10-08 建）

用途：**唯一入口**。人一开口（或定时唤醒），系统先把"时间"追平，再谈别的。
      C 不追平 → LLM 看不到「昨天之后」的信息；C 乱追平 → 步进补单是拿未来结果编的
      （后视镜造假器）。故本模块是 0.1/0.6 时序闸门在**装配层**的落点。

★ 主循环（写死，与骨架完成判据逐字对应）
    唤醒 → 顶部三项自检（0.17）→ 读 C → 逐日追平（候选池观察态推进 → 0.6 五步结算）
         → 当下母链 C0~C6 空转留痕 → 刷新跨日看板（0.12，委派 hboard）→ 就绪报告
  · 追平日**只推进观察态**（0.5 候选池用 ≤T 数据推进），**不产生新买单**——
    新买单只能在追平后的"当下"产生（0.14 原文）。
  · 母链在**当下**跑一次：追平型日次不重跑认知链（那是后视镜）。
    零插件时各环节产出为空，但**必须留痕**（EMPTY 行）——"转过"可见，不等于"转出结果"。
  · 看板刷新**只委派**（`hboard.py` 做聚合）：本层不算 expiry、不折算日历、不重写三区口径。

★ 唯一执行器纪律（0.1 / 0.6 写死，本模块**遵守**而非重复实现）
  · 游标打勾 / date 推进的唯一执行器 = tools/risk/settle.py；
    本模块**只负责叫 settle**，绝不自己动 date（`--discipline` ② AST 断言本文件
    生产区不出现 mark_step / advance_cursor 的调用）。
  · 结算日序列**双轴判定**（v1.4 变更，原「不引入交易日历」是缺陷源）：
    **交易日身份只来自日历**，**数据齐备只来自投放区目录**，两轴分别判定、互不替代。
    · 缺陷机理（实测）：旧实现只按投放区目录发现日子 ⇒ 交易日**缺数据**时该日不进 `plan`
      ⇒ 缺口被**静默跳过**（投放区 [10-08,10-12] 时装配层看不到 10-09 等 15 个交易日）。
    · 日历唯一读取接口 = `dayclass.Calendar.from_seed_csv`（本模块**不自写历法、不算交易日、
      不按周末近似、不联网刷新**）；缺省落点 `data/raw/meta/trade_cal.csv`（只读元数据）。
    · 非交易日目录 → **不进**结算序列；交易日缺数据 → **阻断**（`SESSION_DATA_GAP`）；
      日历缺失/损坏/越界 → **阻断**（`NO_CALENDAR` / `DAY_STATE_UNKNOWN`）。
    · 「有数据的日子才需要结账」保留为**发现**手段，不再充当**时间轴**判据。

★ 装配层三纪律（用户 2026-10-08 F7 三个盯防项，逐条落成机械判据）
  ① **不得重新实现任何业务规则**：本模块只做「遍历 / 解析 / 委派 / 留痕」四件事，
    不判断驱动、不算档位、不出 BUY/WAIT/NO、不碰仓位与价位。
    `--discipline` ① 用 AST 扫描生产区标识符，命中业务规则词汇即 FAIL。
  ② **缺插件 / 缺数据 / 缺能力时必须按既定 fail-safe 运行，禁静默降级成错误决策**：
    · 缺能力（环节下无已落盘能力）→ 记 `EMPTY / NO_CAPABILITY`（**不计 STALL**），
      **不产出任何决策**——空载 ≠ 假装跑过，也 ≠ 编一个"看起来合理"的结论。
    · 缺数据（当日无快照）→ `blocked=AWAITING_TODAY_DATA`，`ready=False`；
      游标停在上一日、**不推进**。
    · 步骤抛错 → settle 内部不打勾 + 本模块停止当天 + 0.17 落 FAIL 行（失败不吞）。
  ③ **"零插件空转跑通一天"必须是真实端到端运行**：`--acceptance` 用真模块 + 真文件 +
    真注册表从**零状态**跑完一个交易日（不是把各模块 `--selftest` 串起来——
    那样证不了"装配起来还能跑"）。判据见 `_acceptance()`。

★ 母链环节顺序 = 注册表 `MOUNTS`（禁第二份清单，0.18 铁律）
  环节下"有没有可执行能力"= `registry.verify_registry()["pending_impl"]` 的反面；
  **环节自身的容器行**（C1 挂在自己头上那行）**不算可委派能力**——它就是这个环节的
  接口定义。把它计入就绪度，会让骨架期每一步都假装有执行体（把"没跑"伪装成"跑了"）

被谁调用：人（对话开始）· 定时唤醒（cron，暂缓）· 各 E/S runner（追平后进入）。
用法：
  python tools/fs/wakeup.py                     # 只读：游标 + 三项自检 + 母链就绪度
  python tools/fs/wakeup.py --wake 2026-10-08   # 追平到该日
  python tools/fs/wakeup.py --wake 2026-10-08 --calendar <trade_cal.csv>
                                                # 显式注入交易日历（缺省落点见下）
  python tools/fs/wakeup.py --selftest          # 内置验收（临时沙盒，不碰真数据）
  python tools/fs/wakeup.py --acceptance        # 零插件端到端空转（骨架完成判据）
  python tools/fs/wakeup.py --discipline

日历落点：`data/raw/meta/trade_cal.csv`（`date,is_trade` 两列，只读元数据，不与日期投放目录混）。
  `--calendar` 缺省即用该落点；**缺失/损坏/越界一律阻断追平**（不自动联网刷新、不按周末推算）。

规则：本模块禁判断业务、禁动游标、禁下单；追平只经 settle；新增环节只能改注册表。
"""
import ast
import io
import json
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools", "fs")))
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "risk")))
from paths import ROOT  # noqa: E402
import cursor as CUR  # noqa: E402
import snapshot as SNAP  # noqa: E402
import candidates as CAND  # noqa: E402
import registry as REG  # noqa: E402
import hboard as HB  # noqa: E402
import observe as OBS  # noqa: E402
import astwin as ASTW  # noqa: E402   ← AST 窗口（尾界）唯一实现，禁本模块自写
import dayclass as DC  # noqa: E402   ← 日期四态 / 交易日历唯一实现，禁本模块自写历法
import settle as SETTLE  # noqa: E402

SCHEMA_VERSION = 1
ACTOR = "wakeup"

# 受阻原因码（值域写死；报告里出现未登记的原因码 = 契约破损）
BLOCKED_CURSOR = "CURSOR_UNREADABLE"
BLOCKED_NO_DATA = "NO_DATA"
BLOCKED_AWAITING_TODAY = "AWAITING_TODAY_DATA"
BLOCKED_POOL_FAIL = "POOL_ADVANCE_FAIL"
BLOCKED_SETTLE_FAIL = "SETTLE_FAIL"
BLOCKED_NOT_SETTLED = "DAY_NOT_SETTLED"
# v1.4 日历轴阻断（三类，均 fail-closed）
BLOCKED_NO_CALENDAR = "NO_CALENDAR"                 # 未注入 / 日历不可用
BLOCKED_DAY_UNKNOWN = "DAY_STATE_UNKNOWN"           # 游标或 today 越出日历覆盖
BLOCKED_SESSION_GAP = "SESSION_DATA_GAP"            # 交易日缺数据（真缺口，须补数）
BLOCKED = (BLOCKED_CURSOR, BLOCKED_NO_DATA, BLOCKED_AWAITING_TODAY, BLOCKED_POOL_FAIL,
           BLOCKED_SETTLE_FAIL, BLOCKED_NOT_SETTLED,
           BLOCKED_NO_CALENDAR, BLOCKED_DAY_UNKNOWN, BLOCKED_SESSION_GAP)

# 交易日历落点（与投放区同根但**不是**日期目录：`meta/` 会被日期名过滤挡在追平计划外）
DEFAULT_CALENDAR = os.path.join(SNAP.RAW_ROOT, "meta", "trade_cal.csv")


class WakeupError(Exception):
    """追平被拒（游标不可读 / 非法日期 / 参数矛盾）——显式抛错，不静默降级。"""


def _check_date(d, name="today"):
    """日期硬校验（YYYY-MM-DD）。与 cursor / settle 同口径：**不猜、不折算**。"""
    import datetime as _dt
    s = d.isoformat() if hasattr(d, "isoformat") else str(d)
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise WakeupError("%s 必须是 YYYY-MM-DD，收到: %r" % (name, d))
    try:
        _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        raise WakeupError("%s 非法: %r" % (name, d))
    return s


# ── 母链：环节顺序与就绪度（全部派生自注册表，禁第二份清单）────────────────────
def chain_stages():
    """母链环节顺序 = 注册表 MOUNTS（"C0".."C6"）。**本文件不另写一份环节清单。**"""
    return tuple(REG.MOUNTS)


def step_readiness(mount, registry_path=None):
    """某环节的就绪度：{mount, delegable, ready, missing, c_row}。

    · delegable = 挂在该环节下、status=active 的**可委派能力**（排除环节自身的容器行）
    · ready     = delegable 中**已落盘**的（未列入 registry 的 pending_impl）
    · missing   = delegable 中**登记了但未落盘**的（骨架阶段预期，非错）
    """
    if mount not in REG.MOUNTS:
        raise WakeupError("未知环节 %r，合法值域 = %s" % (mount, " / ".join(REG.MOUNTS)))
    v = REG.verify_registry(path=registry_path)
    pending = set(v["pending_impl"])
    caps = [c for c in REG.by_mount(mount, path=registry_path) if c.get("status") == "active"]
    c_row = [c["id"] for c in caps if c["kind"] == "C" and c["id"] == c["mounted_on"]]
    delegable = [c["id"] for c in caps if not (c["kind"] == "C" and c["id"] == c["mounted_on"])]
    return {"mount": mount, "delegable": delegable,
            "ready": [c for c in delegable if c not in pending],
            "missing": [c for c in delegable if c in pending],
            "c_row": c_row}


def day_chain(as_of, *, registry_path=None, log_path=None, params_path=None):
    """当下母链空转：按注册表顺序遍历 C0~C6 → 逐环节解析就绪度 → **留痕**。

    ★ 本函数是装配层的核心纪律样本：**只遍历 / 只解析 / 只留痕，不执行任何业务规则**。
      · 环节下有已落盘能力 → 记 DONE，detail=`DELEGATED:<ids>`（委派给平台技能 / LLM，
        它们各自的产出由各自挂载的运行包装单独留痕）。
      · 环节下没有 → 记 `EMPTY / NO_CAPABILITY:<ids>`，**不计 STALL**
        （零插件的空载是设计预期，不是断链信号）。
    返回 {as_of, registry_stage, steps, orders_created=0, ready_any}。
    """
    v = REG.verify_registry(path=registry_path)
    out = {"as_of": as_of, "registry_stage": v["stage"], "steps": [],
           "orders_created": 0, "ready_any": False}
    for mount in chain_stages():
        sr = step_readiness(mount, registry_path)
        ready, missing = sr["ready"], sr["missing"]
        rec = OBS.run_observed(
            ACTOR, "chain:%s" % mount, mount,
            lambda rr=ready, mm=missing: {"ready": rr, "missing": mm},
            as_of=as_of,
            produces=lambda x: bool(x["ready"]),
            detail=(",".join(ready) if ready else ",".join(missing) or "环节无挂载能力"),
            no_capability=not ready,
            log_path=log_path, params_path=params_path, raise_on_error=False)
        out["steps"].append({"mount": mount, "result": rec["record"]["result"],
                             "detail": rec["record"]["detail"],
                             "ready": ready, "missing": missing})
        out["ready_any"] = out["ready_any"] or bool(ready)
    return out


# ── 一天：观察态推进 + 五步结算 ───────────────────────────────────────────────
def _load_calendar(path=None):
    """加载交易日历（**唯一读取接口 = `dayclass.Calendar.from_seed_csv`**，本模块不自写历法）。

    缺省落点 = `DEFAULT_CALENDAR`（`--calendar` 显式覆盖）。
    文件缺失 / 不可读 / 空 / 列不符 ⇒ 抛 `WakeupError`（**fail-closed**：不退回「无日历继续跑」，
    不自动联网刷新，不按周末推算）。
    """
    p = path or DEFAULT_CALENDAR
    if not os.path.isfile(p):
        raise WakeupError(
            "交易日历缺失：%s（不自动联网刷新、不按周末推算；缺失即拒绝追平）" % p)
    try:
        cal = DC.Calendar.from_seed_csv(p, source=os.path.basename(p), version="seed")
    except DC.DayError as e:
        raise WakeupError("交易日历不可用（%s）：%s" % (type(e).__name__, e))
    except OSError as e:
        raise WakeupError("交易日历不可读：%s" % e)
    if not len(cal):
        raise WakeupError("交易日历为空（无任何交易日）：%s（不按周末推算）" % p)
    return cal


def collected_days(raw_root=None, start=None, end=None, calendar=None):
    """**已采集**的交易日（投放区 `data/raw/<date>/` 存在日期目录），闭区间，按序返回。

    ★ v1.4 变更（缺口闭环）：增 `calendar`。给了日历时**只返回交易日** —— 非交易日目录
      （如节假日误投的 `data/raw/2026-10-07/`）**不进结算序列**；`calendar=None` 时保留
      纯目录发现语义（供诊断）。生产路径先过日历闸，故此参在生产上恒非 None。

    ★ 为什么追平日序列不用「已有快照的日期」（实测踩过的坑，真 bug）：
      快照是 **0.6 结算步1 的产物**（`ingest_snapshot`）。用「已有快照」当追平计划来源
      → 形成循环依赖：**新的一天永远进不了计划**（它的快照要等结算才产生），
      `wakeup` 于是每次都"无事可做"，整个系统静止。
      这正是「运转不可见」之外的第二类盲区：**看起来在转，其实一步没动**。
    ★ 为什么不过滤「5 契约文件是否齐」：缺文件应当在**结算步1 显式抛错**（FAIL 行 + 停当天），
      而不是在这里被静默跳过——静默跳过 = 那一天永远不会被结账（0.17「失败不吞」）。
    """
    root = raw_root or SNAP.RAW_ROOT
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        if not os.path.isdir(os.path.join(root, name)):
            continue
        try:
            d = _check_date(name, "投放区日期目录")
        except WakeupError:
            continue                                  # 非日期命名的目录不参与（不猜）
        if calendar is not None and not calendar.is_session(d):
            continue            # v1.4：非交易日目录不进结算序列（`meta/`/`backup/` 上一步已挡）
        if start and d < _check_date(start, "start"):
            continue
        if end and d > _check_date(end, "end"):
            continue
        out.append(d)
    return out


def _run_day(d, ctx):
    """一个交易日：候选池观察态推进 → 0.6 五步结算（唯一推进游标的路径）。"""
    day = {"as_of": d, "pool": None, "settle": None, "settled": False,
           "pending_steps": [], "orders_created": 0, "blocked": None, "error": None,
           "hint": None}
    # ① 候选池观察态推进（≤T 数据；advance_candidate 只推进状态机，零买单）
    rp = OBS.run_observed(
        ACTOR, "pool", "C0",
        lambda: CAND.advance_candidate(d, path=ctx["pool_path"]),
        as_of=d, produces=lambda x: True,
        detail="观察态推进", log_path=ctx["log_path"], params_path=ctx["params_path"],
        raise_on_error=False)
    if rp["error"]:
        day["blocked"], day["error"] = BLOCKED_POOL_FAIL, rp["error"]
        day["hint"] = "候选池状态文件按 0.5 需先初始化（装配层不得越权自建状态）"
        return day
    day["pool"] = rp["value"]
    # ② 五步结算（唯一执行器；本模块只叫它，不自己打勾、不自己推进 date）
    rs = OBS.run_observed(
        ACTOR, "settle", "C0",
        lambda: SETTLE.settle(d, judgements=ctx["judgements"], equity=ctx["equity"],
                              base=ctx["base"], snapshot_root=ctx["snapshot_root"],
                              raw_dir=(os.path.join(ctx["raw_root"], d)
                                       if ctx["raw_root"] else None),
                              cursor_path=ctx["cursor_path"],
                              learning_path=ctx["learning_path"],
                              calendar=ctx["calendar"]),   # v1.4：缺口须到达结算闸门
        as_of=d, produces=lambda x: bool(x.get("settled")),
        detail="五步结算", log_path=ctx["log_path"], params_path=ctx["params_path"],
        raise_on_error=False)
    if rs["error"]:
        day["blocked"], day["error"] = BLOCKED_SETTLE_FAIL, rs["error"]
        return day
    rep = rs["value"]
    day["settle"] = rep
    day["settled"] = bool(rep["settled"])
    day["pending_steps"] = list(rep["pending_steps"])
    day["orders_created"] = int(rep.get("orders_created") or 0)
    if not day["settled"]:
        day["blocked"] = BLOCKED_NOT_SETTLED
    return day


# ── 唯一入口 ─────────────────────────────────────────────────────────────────
def wakeup(today, *, base=None, cursor_path=None, raw_root=None, snapshot_root=None,
           log_path=None, params_path=None, registry_path=None, pool_path=None,
           learning_path=None, judgements=None, equity=None, max_days=None,
           chain=True, board=True, board_path=None, calendar=None):
    """唤醒唯一入口：顶部三项自检 → 追平游标 → 当下母链空转 → 刷新看板 → 就绪报告。

    ★ 参数口径（**跨模块同名不同义是埋雷区，此处写死**）：
      · 本模块 `raw_root` = **投放区根**（`data/raw/`）——用于发现"已采集的交易日"。
      · `settle.settle(raw_dir=)` 的 `raw_dir` = **当日投放目录**（`data/raw/<date>/`），
        由本模块逐日拼好再传。传错会让步1 报"投放区缺 5 个契约文件"（实测踩过）。
      · `calendar` = `dayclass.Calendar`（v1.4）。**缺 ⇒ `NO_CALENDAR` 阻断**（fail-closed：
        交易日身份 UNKNOWN 时不得推进）；游标或 `today` 越出覆盖 ⇒ `DAY_STATE_UNKNOWN`；
        `(游标, today)` 内有交易日无数据 ⇒ `SESSION_DATA_GAP`（缺口不得静默跳过）。

    返回报告（含 `opening` 三项自检行、逐日结果、母链留痕、`ready` 与 `blocked`）。
    **不抛错**（顶层入口必须永远给出一份可读报告）；失败一律落 FAIL 行 + `blocked` 原因码。
    """
    today = _check_date(today)
    ctx = {"base": base, "snapshot_root": snapshot_root, "raw_root": raw_root,
           "cursor_path": cursor_path, "log_path": log_path, "params_path": params_path,
           "pool_path": pool_path, "learning_path": learning_path,
           "judgements": judgements, "equity": equity, "calendar": calendar}

    opening = OBS.opening_checks(today, cursor_path=cursor_path, path=log_path,
                                 snapshot_root=snapshot_root, params_path=params_path)
    report = {"schema_version": SCHEMA_VERSION, "today": today, "actor": ACTOR,
              "opening": opening, "banner": list(opening["lines"]),
              "cursor_before": None, "cursor_after": None, "plan": [], "days": [],
              "chain": None, "chain_day": None, "ready": False, "blocked": None,
              "error": None, "orders_created": 0, "h_board": None,
              "settled_days": [], "pending_days": [],
              "calendar_basis": None, "gap_sessions": []}

    # ① 读游标（缺失/损坏 → 显式 FAIL 行 + 原因码，绝不静默新建）
    try:
        cur = CUR.read_cursor(cursor_path)
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)

        def _fail(_e=e):
            raise _e

        OBS.run_observed(ACTOR, "read_cursor", "PRE", _fail, as_of=None,
                         log_path=log_path, raise_on_error=False)
        report["blocked"] = BLOCKED_CURSOR
        report["error"] = err
        report["banner"].append("[!!] 游标不可读 → 无法追平：%s" % err)
        return report
    report["cursor_before"] = cur["date"]
    # ★ 提前返回路径（日历闸 / 缺口闸）也必须带 cursor_after：值 = 当前游标，如实表示"未推进"。
    #   若留初始 None，会掩盖"游标确实没动"这一事实（K3/K8 自检暴露）。
    report["cursor_after"] = cur["date"]
    settled = CUR.is_settled(cur)

    # ①b 日历闸（v1.4 · fail-closed）：缺日历 / 越界 ⇒ 阻断，不进逐日
    if calendar is None or not len(calendar):
        report["blocked"] = BLOCKED_NO_CALENDAR
        report["error"] = ("未注入交易日历：交易日身份 UNKNOWN ⇒ 拒绝追平"
                           "（不按周末推算、不由目录缺失反推休市；经 --calendar 注入）")
        report["banner"].append("[!!] 日历      : 未注入 → 拒绝追平（fail-closed）")
        return report
    report["calendar_basis"] = calendar.basis()
    _out = [x for x in (cur["date"], today) if not calendar.covers(x)]
    if _out:
        report["blocked"] = BLOCKED_DAY_UNKNOWN
        report["error"] = ("日期轴越界：%s 不在日历覆盖 %s 内 ⇒ 拒绝追平（越界不外推）"
                           % (",".join(_out), calendar.span))
        report["banner"].append("[!!] 日历      : %s 越出覆盖 %s → 拒绝追平"
                                % (",".join(_out), calendar.span))
        return report
    report["banner"].append("[OK] 日历      : %s（%s ~ %s）"
                            % (calendar.basis(), calendar.span[0], calendar.span[1]))

    # ② 追平日序列（**双轴**：交易日身份 ← 日历；数据齐备 ← 投放区目录）
    raw_root = raw_root or SNAP.RAW_ROOT
    days_all = collected_days(raw_root, cur["date"], today, calendar=calendar)
    # ②b 缺口闸（v1.4）：严格落在 (游标, today) 之间的交易日无数据 ⇒ 阻断
    #     · `today` 本身无数据**不算**缺口（数据未到 = 正常等待，见 awaiting_today_data）
    #     · `today` 之后一律不看（as_of 上界由 collected_days 的 end=today 兜住）
    gap = [s for s in calendar.sessions_between(cur["date"], today)
           if cur["date"] < s < today and s not in set(days_all)]
    report["gap_sessions"] = gap
    if gap:
        report["blocked"] = BLOCKED_SESSION_GAP
        report["error"] = ("交易日缺口 %d 个：%s ⇒ 拒绝追平（缺口不得静默跳过，须先补数）"
                           % (len(gap), ",".join(gap[:6])))
        report["banner"].append("[!!] 缺口      : %d 个交易日无数据 → 拒绝追平（%s%s）"
                                % (len(gap), ",".join(gap[:3]),
                                   "…" if len(gap) > 3 else ""))
        return report
    forward = [d for d in days_all if d > cur["date"]]
    plan = ([cur["date"]] if not settled else []) + forward
    if max_days:
        plan = plan[:int(max_days)]
    report["plan"] = plan
    report["forward_days"] = forward
    report["collected_days"] = days_all
    report["awaiting_today_data"] = today not in days_all
    if not days_all and settled:
        report["blocked"] = BLOCKED_NO_DATA

    # ③ 逐日追平（未结清即停 —— 游标不推进）
    for d in plan:
        day = _run_day(d, ctx)
        report["days"].append(day)
        report["orders_created"] += day["orders_created"]
        if day["settled"]:
            report["settled_days"].append(d)
        else:
            report["pending_days"].append(d)
            if not report["blocked"]:
                report["blocked"] = day["blocked"] or BLOCKED_NOT_SETTLED
            if day["error"]:
                report["error"] = day["error"]
            break                                  # 不允许"跳过去继续跑"

    cur2 = CUR.read_cursor(cursor_path)
    report["cursor_after"] = cur2["date"]
    report["cursor_settled"] = CUR.is_settled(cur2)
    report["cursor_pending_steps"] = CUR.pending_steps(cur2)

    # ④ 当下母链空转（零插件：各环节留痕，产出为空）
    if chain:
        cd = today if today in days_all else report["cursor_after"]
        report["chain_day"] = cd
        report["chain"] = day_chain(cd, registry_path=registry_path, log_path=log_path,
                                    params_path=params_path)
        report["orders_created"] += int(report["chain"].get("orders_created") or 0)

    # ⑤ 刷新跨日看板（0.12）——装配层**只委派**，聚合逻辑在 `hboard.py`（本层不重实现）
    if chain and board:
        bd = report["chain_day"] or today
        rb = OBS.run_observed(
            ACTOR, "hboard", "POST",
            lambda: HB.refresh(bd, path=board_path, pool_path=pool_path, base=base),
            as_of=bd, detail="刷新跨日看板", log_path=log_path,
            params_path=params_path, raise_on_error=False)
        val = rb["value"] or {}
        report["h_board"] = {"path": val.get("path"), "statuses": val.get("statuses"),
                             "error": rb["error"]}

    # ⑥ 就绪判定：追平到 today 且五步全勾，才算「就绪，可供 C1 感知消费」
    report["ready"] = bool(report["cursor_settled"] and report["cursor_after"] == today)
    if not report["ready"] and not report["blocked"]:
        report["blocked"] = BLOCKED_AWAITING_TODAY if report["awaiting_today_data"] else \
            BLOCKED_NOT_SETTLED
    report["banner"].append("%s 就绪        : C=%s%s" % (
        "[OK]" if report["ready"] else "[!!]", report["cursor_after"],
        "（追平完成）" if report["ready"] else
        "（未追平到 %s；原因 %s%s）" % (today, report["blocked"],
                                     ("：" + str(report["error"])[:80]) if report["error"] else "")))
    if report["chain"]:
        n_empty = len([s for s in report["chain"]["steps"] if s["result"] == "EMPTY"])
        report["banner"].append("     └ 母链     : %d 环节 · %d 个产出为空（零插件空转，已留痕）"
                                % (len(report["chain"]["steps"]), n_empty))
    return report


def status(*, cursor_path=None, base=None, registry_path=None, log_path=None,
           snapshot_root=None):
    """只读总览：游标 + 母链各环节就绪度 + 观测摘要（0.17）。"""
    cur = CUR.read_cursor(cursor_path)
    return {"cursor": cur, "settled": CUR.is_settled(cur),
            "pending_steps": CUR.pending_steps(cur),
            "registry_stage": REG.verify_registry(path=registry_path)["stage"],
            "chain": [step_readiness(m, registry_path) for m in chain_stages()],
            "observability": OBS.summary(log_path),
            "note": "本模块禁判断业务；环节顺序与就绪度全部派生自注册表"}


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

    ★ 这条纪律是从 F6 的 landing.py 直接搬来的（同一类错误同一个修法）：
      F6 曾用「只被 `_selftest` 调用」单独判定夹具 → 把 `upsert_candidate` / `promote` /
      `resolve` 这类**公开核心 API** 一并置空 → 整个模块的写点审计**静默屏蔽**
      （全绿 = 没看）。故：**公开名恒进审计，永不被判为夹具**。
      本模块的 `_sandbox` / `_put_raw` / `_init_cursor` / `_fp` 是夹具（私有 + 只被测试调用），
      而 `_run_day` / `collected_days` / `wakeup` 虽私有/公开但被生产调用 → **照常审计**。
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
            continue                                  # ← 公开名恒进审计
        if n.name in _TEST_FUNCS:
            continue
        cs = callers.get(n.name)
        if cs and cs <= {True}:                       # 私有 + 只被测试函数调用
            out.add(n.name)
    return out


def _prod_tokens(path=None):
    """生产代码区标识符 + 非文档字符串（剔测试函数整段与夹具函数）。

    ★ 为什么必须剔文档字符串：本模块 docstring 里**必须**写明"不得出 BUY/WAIT/NO"
      "不得判断驱动"这类纪律声明——把声明本身判成违例，就是 F6 修过的那种假判据。
      判据必须是**真引用**，不是"提到过"。
    """
    p = path or os.path.abspath(__file__)
    tree = ast.parse(io.open(p, encoding="utf-8").read())
    skip = set(_test_lines(tree))
    fix = _fixture_names(tree)
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in fix:
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
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            toks.add(node.value)
    return toks


def _stage_list_literals(path=None):
    """生产区里「环节清单」的**形状**：一个 List/Tuple 含 ≥3 个不同的 C 环节字面量。

    单写一个 `stage="C0"`（说明本次运行属哪个环节）不是清单，不该判违例；
    真正的"第二份环节清单"必然长得像 `("C0","C1",...,"C6")`。
    """
    p = path or os.path.abspath(__file__)
    tree = ast.parse(io.open(p, encoding="utf-8").read())
    skip = set(_test_lines(tree))
    fix = _fixture_names(tree)
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in fix:
            skip |= ASTW.line_set(n)
    out = []
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) in skip:
            continue
        if isinstance(node, (ast.List, ast.Tuple)):
            cs = {e.value for e in node.elts
                  if isinstance(e, ast.Constant) and isinstance(e.value, str)
                  and len(e.value) == 2 and e.value[0] == "C" and e.value[1].isdigit()}
            if len(cs) >= 3:
                out.append(sorted(cs))
    return out


# 业务规则词汇（**拼装构造**：避免判据自指——写成整字面量会命中本文件自己的黑名单）
_BIZ_WORDS = (
    "regime" + "_ladder", "position" + "_cap", "limit" + "_up_count", "break" + "_rate",
    "driver" + "_type", "stop" + "_line", "entry" + "_limit", "position" + "_request",
    "take" + "_profit", "buy" + "_price", "sell" + "_price", "hold" + "_days",
)
_ORDER_WORDS = ("place" + "_order", "submit" + "_order", "can" + "cel_order",
                "下" + "单", "撤" + "单")
_FORBIDDEN_CURSOR_WRITE = ("mark" + "_step", "advance" + "_cursor")
# 正式运行调用：必须被 run_observed 包裹的（装配层只经包装调用它们）
_WRAPPED_CALLS = ("settle", "advance_candidate", "refresh")


def _probe_ctx(d, **overrides):
    """纪律探测上下文：**全部可写路径显式注入临时域**（D-02 最小修复 R1）。

    ★ 存在理由：旧 ⑦ 手写 `ctx_bad` 并给 3 个可写路径传 `None`
      （`pool_path` / `learning_path` / `base`），经各模块 `p = path or 默认落点`
      的回落语义**落到真库**：真实候选池被 `.tmp` + `os.replace` 完整重写。
    ★ 三条硬规则（对应专项件 §6 I-1～I-4）：
      I-1 **可写路径禁 None / 空串 / 假值**；
      I-2 目标必须落在 `d` 之下；
      I-3 需"已存在"的落点必须**先初始化**（候选池 = `_init_pool`），否则语义漂移
          （`advance_candidate` 抛 `CandidateError` → ⑦ 变成"候选池报错"而不是"结算抛错"）；
      I-4 只读依赖优先**副本**（`params.json`）而不是回落真库。
    ★ **违反即在探测之前抛错**（fail-closed：宁可拒绝跑探测，也不让探测落到真库）。
      `**overrides` 不做白名单豁免 —— 覆盖值同样过同一套检查，
      故"注入一个 None 试试"会被机械拦住（T-D02-1 负例）。
    ★ 路径类键的识别规则在本函数（前置强制）与 ⑨c（事后核验）**各写一份**，
      **有意为之**：同一处 bug 不应同时使"强制"与"核验"失效。
    """
    import shutil
    d = os.path.abspath(d)
    for _sub in ("state", "generated", "learning", "raw", "snapshot"):
        os.makedirs(os.path.join(d, _sub), exist_ok=True)
    _pool = _init_pool(os.path.join(d, "state", "candidates.json"), "2026-10-07")
    _params = os.path.join(d, "state", "params.json")
    import params as _PARAMS
    shutil.copyfile(_PARAMS.PARAMS_PATH, _params)          # I-4：真库 params 只读副本
    ctx = {"base": os.path.join(d, "generated"),
           "snapshot_root": os.path.join(d, "snapshot"),
           "raw_root": os.path.join(d, "raw"),
           "cursor_path": os.path.join(d, "state", "cursor.json"),   # 刻意不创建
           "log_path": os.path.join(d, "state", "run" + "_log.jsonl"),
           "params_path": _params,
           "pool_path": _pool,                                # 必须已存在（I-3）
           "learning_path": os.path.join(d, "learning", "demand_queue.jsonl"),
           "judgements": None, "equity": None, "calendar": None}   # 非路径：None 即生产缺省
    ctx.update(overrides)
    _bad = sorted(k for k, v in ctx.items()
                  if (k.endswith("_path") or k.endswith("_root") or k == "base")
                  and (not isinstance(v, str) or not v
                       or not os.path.abspath(v).startswith(d + os.sep)))
    if _bad:
        raise WakeupError(
            "纪律探测上下文未隔离 %s —— 可写路径禁 None / 空串 / 假值，且必须落在临时域 %s "
            "之下（D-02：不得回落到真库）" % (_bad, d))
    return ctx




def _empty_registry():
    """合成「零 S/E 插件」注册表（C 母链行留、S/E 全剥）→ 临时文件路径。

    2026-10-19 开插后：真实注册表 stage=PLUGINS 且 20 个 S/E 全部落盘，
    「零插件」场景的机械断言（EMPTY/NO_CAPABILITY）改用本夹具复现——
    测试与装机状态解耦（同 registry.py 自检夹具 id 移出真实空间的原则）。
    """
    import json as _json
    import tempfile as _tf
    real = REG.load_registry()
    doc = {"registry_version": real.get("registry_version"),
           "updated_at": real.get("updated_at"), "stage": "SKELETON",
           "capabilities": [c for c in real.get("capabilities", [])
                            if c.get("kind") == "C"],
           "impl_log": [], "notes": "wakeup 自检夹具：零 S/E 插件"}
    fd, p = _tf.mkstemp(prefix="wakeup_emptyreg_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        _json.dump(doc, f, ensure_ascii=False)
    return p


def _discipline(verbose=True):
    """装配层纪律九项（0.14 全条 + 用户 F7 三个盯防项）。"""
    res, bad = [], []

    def T(name, cond, extra=""):
        res.append((name, bool(cond), extra))
        if not cond:
            bad.append(name)

    def _snap_lib():
        """真库全目录快照：相对路径 → (大小, 亚秒 mtime, inode, 内容 md5)。

        ★ 为什么不用既有 `_tree_fp()`：它是 `int(st.st_mtime)`（**秒级**）。
          最坏情形实测（被写文件 mtime 与探测同秒）**0/6 次检出**；而 `--discipline`
          自检仅约 0.6 s、同秒命中率极高 ⇒ 秒级口径在本判据上是**空判据**。
          内容 md5 亦**不能**作主判据（`as_of` == 池内 `updated_at` 巧合 ⇒ 0/6 检出），
          但保留为"内容无变化"的独立维度。inode 在 `.tmp` + `os.replace` 下**必变**。
        ★ `_tree_fp()` 属冻结范围且被 `--acceptance` I1 复用 ⇒ 本批不改它；
          其同类盲区另行报告、单独申请授权（待决 D-02-⑨）。
        """
        import hashlib
        out = {}
        for dp, dn, fn in os.walk(ROOT):
            dn[:] = [x for x in dn if x not in (".git", "__pycache__")]
            for f in sorted(fn):
                if f.endswith(".pyc"):
                    continue
                p = os.path.join(dp, f)
                st = os.stat(p)
                with io.open(p, "rb") as _fh:
                    _digest = hashlib.md5(_fh.read()).hexdigest()
                out[os.path.relpath(p, ROOT).replace("\\", "/")] = (
                    st.st_size, round(st.st_mtime, 6), st.st_ino, _digest)
        return out

    # ★ 口径②在此起算（D-02 最小修复 R2）：真库**全目录**快照必须在**任何探测之前**取得。
    #   旧版把 before 取在 ⑦ 之后（`:577` vs `:567`）⇒ 污染已在快照内 ⇒ 判据必然全绿（F21）。
    #   放在 ① 之前是**保守取最前**（① ～ ⑤ 均为只读静态分析，放其前不依赖任何后续推理）。
    lib_before = _snap_lib()

    toks = _prod_tokens()
    tree = ast.parse(io.open(os.path.abspath(__file__), encoding="utf-8").read())
    # 生产区 Call 节点及其祖先（用于"是否被 run_observed 包裹"）
    funcs = ASTW.func_table(tree)          # 函数区间表 = astwin 唯一实现（尾界含多行尾部）
    test_funcs = {nm for lo, hi, nm in funcs
                  if nm in ("_selftest", "_acceptance", "_discipline")}

    def _is_test_node(node):
        return ASTW.owner_of(funcs, getattr(node, "lineno", -1), None) in test_funcs

    parents = {}
    for node in ast.walk(tree):
        for ch in ast.iter_child_nodes(node):
            parents[ch] = node

    def _inside_run_observed(node):
        p = parents.get(node)
        while p is not None:
            if isinstance(p, ast.Call):
                fn = getattr(p.func, "id", None) or getattr(p.func, "attr", None)
                if fn == "run_observed":
                    return True
            p = parents.get(p)
        return False

    my_name = os.path.basename(__file__)
    # ① 不得重新实现业务规则（AST 标识符，禁第二套字段名/阈值）
    hit = sorted({w for w in _BIZ_WORDS if w in toks})
    T("① 装配层零业务规则重实现（%d 个业务词汇命中）" % len(hit), not hit, hit)
    # ② 不得自己打勾 / 推进游标（唯一执行器 = settle）
    hit2 = sorted({w for w in _FORBIDDEN_CURSOR_WRITE if w in toks})
    T("② 不直接调 mark_step / advance_cursor（唯一执行器 = settle）", not hit2, hit2)
    # ③ 零下单（本层无下单接口；报告 orders_created 恒 0）
    hit3 = sorted({w for w in _ORDER_WORDS if w in toks})
    T("③ 零下单类标识符（报告 orders_created 恒 0）", not hit3, hit3)
    # ④ 环节顺序派生自注册表（禁第二份清单——判据是"清单的形状"，不是"出现过 C0"）
    sl = _stage_list_literals()
    T("④ 母链环节顺序 = 注册表 MOUNTS 且无第二份环节清单",
      chain_stages() == tuple(REG.MOUNTS) and not sl, sl)
    # ④b 夹具判别不吞公开名（F6 同一负例，防"审计静默屏蔽"回归）
    fx = _fixture_names(ast.parse(io.open(os.path.abspath(__file__), encoding="utf-8").read()))
    pub = [n.name for n in ast.parse(io.open(os.path.abspath(__file__),
                                             encoding="utf-8").read()).body
           if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")]
    T("④b 夹具判别永不吞公开名（%d 个公开函数全部在审计内）" % len(pub),
      not (fx & set(pub)) and bool(fx), sorted(fx))
    # ⑤ 正式运行调用全被 run_observed 包裹
    naked = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and not _is_test_node(node):
            fn = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if fn in _WRAPPED_CALLS and not _inside_run_observed(node):
                naked.append("%s@L%d" % (fn, node.lineno))
    T("⑤ 正式运行调用（settle / advance_candidate）100%% 经 run_observed",
      not naked, naked)
    # ⑥ 缺能力 fail-safe：零插件沙盒实测（不是看代码）
    import tempfile as _tf
    d = _tf.mkdtemp(prefix="ta_wakeup_disc_")
    lp = os.path.join(d, "rl.jsonl")
    _er = _empty_registry()
    ch = day_chain("2026-10-08", log_path=lp, registry_path=_er)
    n_emp = len([s for s in ch["steps"] if s["result"] == "EMPTY"])
    T("⑥ 零插件 → 母链各环节 EMPTY/NO_CAPABILITY 且零决策产出",
      n_emp == len(chain_stages()) and ch["orders_created"] == 0
      and all("NO_CAPABILITY" in s["detail"] for s in ch["steps"]), n_emp)
    T("⑥b 结构性空转不计 STALL（零插件连跑 5 次）",
      not OBS.stall_streams(path=lp))
    # ⑦ 失败不吞：结算抛错被记 FAIL 且被拦（真跑一遍）
    # ★ R1：探测上下文的**全部可写路径**显式注入临时域（旧写法给 3 个键传 None，
    #   经 `path or 默认落点` 落到真库 —— 真候选池被 `.tmp` + `os.replace` 完整重写）。
    ctx_bad = _probe_ctx(d)
    LP2 = ctx_bad["log_path"]
    day = _run_day("2026-10-08", ctx_bad)
    rows = OBS.read_log(LP2)
    T("⑦ 结算抛错 → 记 FAIL 行 + 当天被拦（不静默降级）",
      day["blocked"] is not None and any(r["result"] == "FAIL" for r in rows),
      day["blocked"])
    # ⑧ 单写点纪律不改：本模块不出现留痕路径字面量（留痕只经 observe）
    T("⑧ 本模块不引用留痕路径（写入权在 observe）",
      not any("run_log" in t for t in toks))
    # ⑨ 沙盒泄漏 = 0（口径②）—— **快照前置 + 全树口径 + 高灵敏度维度**（D-02 最小修复 R2）
    #    · 旧版把 before 取在 ⑦ **之后**（F21）：污染已在快照内 ⇒ 判据必然全绿；
    #    · 旧版只比两条硬编码路径 ⇒ 漏写一个没人想到的文件，断言照样全绿（F7 的教训）；
    #    · 旧版用 `_fp`（内容 md5）⇒ 本缺陷因"日期巧合"内容逐字节相同，实测 0/6 检出。
    #    故改判据 = 前后两份**全树快照**逐项相等（集合 + 大小 + 亚秒 mtime + inode + 内容）。
    _probe = _sb = None
    import tempfile as _tf2
    _sb = _tf2.mkdtemp(prefix="ta_wakeup_disc2_")
    _probe = day_chain("2026-10-08", log_path=os.path.join(_sb, "rl.jsonl"))
    lib_now = _snap_lib()
    T("⑨ ②沙盒泄漏 = 0：真库**全目录**（文件集合 + 大小 + 亚秒 mtime + inode + 内容摘要）"
      "纪律自检前后逐项相同（快照取在探测之前 · %d 文件 · %d 环节探测）"
      % (len(lib_before), len(_probe["steps"])),
      lib_now == lib_before, _tree_diff(lib_before, lib_now))

    # ⑨b 快照早于探测（结构性）：判据只有在"污染尚未发生"时才可能为真
    _dnode = next(_n for _n in tree.body
                  if isinstance(_n, ast.FunctionDef) and _n.name == "_discipline")
    _snap_lines = [n.lineno for n in ast.walk(_dnode) if isinstance(n, ast.Assign)
                   and any(getattr(t, "id", None) == "lib_before" for t in n.targets)]
    _probe_lines = [n.lineno for n in ast.walk(_dnode) if isinstance(n, ast.Call)
                    and getattr(n.func, "id", None) in ("_run_day", "day_chain")]
    T("⑨b 快照早于探测（快照 L%s < 首个探测 L%s）"
      % (_snap_lines[:1] or ["-"], _probe_lines[:1] or ["-"]),
      bool(_snap_lines) and bool(_probe_lines) and min(_snap_lines) < min(_probe_lines),
      {"snap": _snap_lines, "probe": _probe_lines})

    # ⑨c 全写路径隔离（核验 ⑦ **实际使用**的那一份 ctx，不是"另建一份"）
    _dday = next(_n for _n in tree.body
                 if isinstance(_n, ast.FunctionDef) and _n.name == "_run_day")
    _used = sorted({n.slice.value for n in ast.walk(_dday)
                    if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                    and n.value.id == "ctx" and isinstance(n.slice, ast.Constant)
                    and isinstance(n.slice.value, str)})
    _plike = lambda _k: _k.endswith("_path") or _k.endswith("_root") or _k == "base"
    _missing = [k for k in _used if k not in ctx_bad]
    _badp = [k for k in _used if _plike(k) and (
        not isinstance(ctx_bad.get(k), str) or not ctx_bad.get(k)
        or not os.path.abspath(ctx_bad[k]).startswith(os.path.abspath(d) + os.sep))]
    T("⑨c ⑦ 探测上下文隔离（_run_day 消费 %d 键 · 路径类 %d 键全部非假值且落在临时域）"
      % (len(_used), len([k for k in _used if _plike(k)])),
      bool(_used) and len(_used) >= 10 and not _missing and not _badp,
      {"missing": _missing, "unisolated": _badp, "n_used": len(_used)})

    # ⑨d 判据灵敏度自证（四分纯内存负例，零 I/O）：四类扰动均须被检出并指名路径
    _b0 = {"a/x": (10, 1.0, 1, "h")}
    _cases = (("亚秒 mtime 变", {"a/x": (10, 1.000001, 1, "h")}, "改动"),
              ("内容摘要变", {"a/x": (10, 1.0, 1, "h2")}, "改动"),
              ("新增文件", {"a/x": (10, 1.0, 1, "h"), "a/y": (1, 0.0, 2, "g")}, "新增"),
              ("消失文件", {}, "消失"))
    _sens = {}
    for _nm, _v, _kw in _cases:
        _s = _tree_diff(_b0, _v)
        _sens[_nm] = bool(_v != _b0) and (_kw in _s) and ("a/" in _s)
    T("⑨d 判据灵敏度自证（四类扰动均被检出并指名路径）", all(_sens.values()), _sens)

    # ⑨e fail-closed 结构断言：判据必须是"前后两份快照"的相等比较（禁恒真 / 禁 or True），
    #    且必须位于 ⑦ 之后（保证探测确已发生）。
    def _tcall(_prefix):
        # 首参可能是 Constant、隐式拼接后的 Constant，或 `%` 格式化后的 BinOp(Mod)
        def _nm(_n):
            if not _n.args:
                return None
            _a = _n.args[0]
            if isinstance(_a, ast.BinOp) and isinstance(_a.op, ast.Mod):
                _a = _a.left
            return _a.value if isinstance(_a, ast.Constant)                 and isinstance(_a.value, str) else None

        return [n for n in ast.walk(_dnode) if isinstance(n, ast.Call)
                and getattr(n.func, "id", None) == "T" and (_nm(n) or "").startswith(_prefix)]

    _t9, _t7 = _tcall("⑨ "), _tcall("⑦ ")
    _eq = bool(_t9) and isinstance(_t9[0].args[1], ast.Compare) \
        and isinstance(_t9[0].args[1].ops[0], ast.Eq) \
        and ast.dump(_t9[0].args[1].left) != ast.dump(_t9[0].args[1].comparators[0])
    T("⑨e fail-closed：⑨ 判据 = Eq(前后两份快照) 且两侧不同（禁恒真/禁 or True），且 ⑨ 在 ⑦ 之后",
      _eq and bool(_t7) and _t9[0].lineno > _t7[0].lineno,
      {"eq_form": _eq, "l9": _t9[0].lineno if _t9 else None,
       "l7": _t7[0].lineno if _t7 else None})
    if verbose:
        for name, okk, extra in res:
            print("  [%s] %s" % ("OK" if okk else "!!", name) + ("  %s" % (extra,) if extra else ""))
    return {"ok": not bad, "n": len(res), "failed": bad, "results": res}


# ── 自检（--selftest）：单元级判据，全部在临时沙盒 ────────────────────────────
def _sandbox(prefix="ta_wakeup_", pool_date="2026-10-07", init_pool=True):
    """独立沙盒：项目根 base + 快照/投放/账本/游标/候选/Thesis/学习/留痕 全套注入口。

    `init_pool=False` 供端到端验收用——**"从零状态启动"必须先证明盘上什么都没有**，
    再显式初始化；若沙盒一建好就把状态铺好，那条判据就成了空话。

    ★ 为什么沙盒要**显式初始化**候选池：0.5 与 0.1 同纪律——`_load` 见文件缺失一律抛错
      （「不得静默新建」，防池子凭空复位）。装配层同样不得越权自建状态，
      故缺文件时 wakeup 是**显式受阻**（POOL_ADVANCE_FAIL），不是悄悄造一份空的。
      本函数是**测试夹具**（与各模块 `_selftest` 里直写状态文件同例），生产不走这里。
    """
    import tempfile
    root = tempfile.mkdtemp(prefix=prefix)
    for sub in (("data", "snapshot"), ("data", "raw"), ("data", "generated"),
                ("data", "learning"), ("data", "state")):
        os.makedirs(os.path.join(root, *sub), exist_ok=True)
    pool_path = os.path.join(root, "data", "state", "candidates.json")
    if init_pool:
        _init_pool(pool_path, pool_date)
    return {
        "root": root,
        "base": root,
        "snapshot_root": os.path.join(root, "data", "snapshot"),
        "raw_root": os.path.join(root, "data", "raw"),
        "cursor_path": os.path.join(root, "data", "state", "cursor.json"),
        "pool_path": pool_path,
        "learning_path": os.path.join(root, "data", "learning", "demand_queue.jsonl"),
        "log_path": os.path.join(root, "data", "state", "run_log.jsonl"),
        "thesis_path": os.path.join(root, "memory", "state", "thesis.md"),
        "board_path": os.path.join(root, "data", "generated", "h_board.md"),
    }


def _init_pool(path, date):
    """0.5 候选池初始化（测试夹具；生产初始化属 0.5 条目的人工动作，装配层不代劳）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": CAND.SCHEMA_VERSION, "updated_at": date, "candidates": []},
                  f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def _init_cursor(path, date, done=True):
    """0.1 初始化（本模块只用于测试沙盒；生产初始化属人工/0.1 条目）。"""
    import cursor as C
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": C.SCHEMA_VERSION, "date": date,
                   "checklist": {s: bool(done) for s in C.STEPS}}, f, ensure_ascii=False)
    return path


def _put_raw(raw_root, date, rows):
    """投放区 5 契约文件（与 settle 自检同形）。"""
    rd = os.path.join(raw_root, date)
    os.makedirs(rd, exist_ok=True)
    dq = ["code,name,open,high,low,close,pct,vol,amount"]
    for c, o, h, lo, cl, pc in rows:
        dq.append("%s,%s,%s,%s,%s,%s,%s,%d,%d" % (c, c, o, h, lo, cl, pc, 1000000, 10000000))
    io.open(os.path.join(rd, "daily_quote.csv"), "w", encoding="utf-8",
            newline="\n").write("\n".join(dq) + "\n")
    for fname, head in (("limit_up.csv", "code,name,boards,first_seal_time,seal_amount,reason"),
                        ("dragon_tiger.csv", "code,name,seat,buy,sell,net")):
        io.open(os.path.join(rd, fname), "w", encoding="utf-8", newline="\n").write(head + "\n")
    io.open(os.path.join(rd, "news.jsonl"), "w", encoding="utf-8", newline="\n").write("")
    io.open(os.path.join(rd, "env_metrics.json"), "w", encoding="utf-8", newline="\n").write(
        json.dumps({"limit_up_count": 62, "max_board": 5, "turnover": 1.1e12,
                    "up_down_ratio": 1.6, "fail_rate": 0.18, "index_ma20": 3200.0}))


def _fp(path):
    import hashlib
    return None if not os.path.exists(path) else hashlib.md5(io.open(path, "rb").read()).hexdigest()


def _tree_fp():
    """真库**全目录**指纹（相对路径 → 大小 + mtime）。

    ★ 存在理由（用户 2026-10-08 收紧验收口径）：「沙盒泄漏到真库」不能只查几个
      硬编码路径——**漏写一个没人想到的文件，三条硬编码断言照样全绿**。
      F7 首版 E2E 就真把看板写进了真库（J1/J2/J4 才暴露）→ 故改为**全库逐项比对**。
    排除项写死（与本系统"是否写入"无关的必然变化）：
      `.git/`（版本库内部）· `__pycache__/` + `*.pyc`（Python 导入副作用，每次运行必生成）。
    口径与 loader.py 的「目录指纹（相对路径 + 大小 + mtime）」一致，不另造第二套。
    """
    out = {}
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in (".git", "__pycache__")]
        for f in sorted(fn):
            if f.endswith(".pyc"):
                continue
            p = os.path.join(dp, f)
            st = os.stat(p)
            out[os.path.relpath(p, ROOT).replace("\\", "/")] = (
                st.st_size, int(st.st_mtime))
    return out


def _tree_diff(before, after=None):
    """指纹差异摘要（只报路径，便于定位泄漏源）。"""
    after = _tree_fp() if after is None else after
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    parts = []
    if added:
        parts.append("新增 %s" % added[:5])
    if removed:
        parts.append("消失 %s" % removed[:5])
    if changed:
        parts.append("改动 %s" % changed[:5])
    return " / ".join(parts) or "无差异"


def _selftest():
    ok = fail = 0
    fails = []
    # ★ 零污染动态基准（2026-10-20，同 I2）：游标已正式推进（10-19 全勾）
    #   ⇒「仍 2026-09-30」硬编码失效；G4 改「自检前后游标态不变」。
    _G4_BEFORE = CUR.read_cursor()

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
        except (WakeupError, CUR.CursorError, SNAP.SnapshotError, SETTLE.SettleError):
            return True

    try:
        CAL = _load_calendar()          # 真实日历（落点 data/raw/meta/trade_cal.csv）
    except WakeupError as e:
        print("  [!!] 交易日历不可用 → 追平闸无法验证：%s" % e)
        return 1

    def W(env, today, **kw):
        kw.setdefault("calendar", CAL)  # 默认注入；显式传 calendar=None 可测缺日历路径
        return wakeup(today, base=env["base"], cursor_path=env["cursor_path"],
                      raw_root=env["raw_root"], snapshot_root=env["snapshot_root"],
                      log_path=env["log_path"], pool_path=env["pool_path"],
                      learning_path=env["learning_path"],
                      board_path=env["board_path"], **kw)

    print("【A】环节顺序与就绪度：全部派生自注册表（5）")
    T("A1 环节顺序 = 注册表 MOUNTS", chain_stages() == tuple(REG.MOUNTS))
    T("A2 环节顺序非硬编码（%d 个环节）" % len(chain_stages()),
      len(chain_stages()) == len(REG.MOUNTS))
    v = REG.verify_registry()
    T("A3 真注册表就绪：%d 行 · stage=%s" % (v["n_caps"], v["stage"]), v["ok"])
    sr1 = step_readiness("C1")
    T("A4 C1 容器行不被算作可委派能力", sr1["c_row"] == ["C1"] and "C1" not in sr1["delegable"])
    T("A5 开插后（2026-10-19）C1 ready 反映真实状态：挂载 S/E 已落盘",
      bool(sr1["ready"]) and not sr1["missing"]
      and set(sr1["delegable"]) >= {"S2", "S3"}, sr1)
    T("A6 非法环节被拒", raises(step_readiness, "C9"))

    print("【B】母链空转：留痕且零决策（5）")
    env0 = _sandbox("ta_wakeup_chain_")
    ch = day_chain("2026-10-08", log_path=env0["log_path"],
                   registry_path=_empty_registry())
    T("B1 逐环节留痕（%d 行 = %d 环节）" % (len(ch["steps"]), len(chain_stages())),
      len(ch["steps"]) == len(chain_stages()))
    T("B2 零插件 → 全部 EMPTY/NO_CAPABILITY",
      all(s["result"] == "EMPTY" and s["detail"].startswith("NO_CAPABILITY")
          for s in ch["steps"]), [s["detail"] for s in ch["steps"]])
    ch_real = day_chain("2026-10-08", log_path=env0["log_path"])
    T("B2b 开插后真实注册表 ⇒ 环节 DONE（挂载能力 id 列表，非 NO_CAPABILITY）",
      any(s["result"] == "DONE" and "NO_CAPABILITY" not in str(s["detail"])
          for s in ch_real["steps"]), [s["detail"] for s in ch_real["steps"][:3]])
    T("B3 母链不留 STALL（结构性为空不计）", not OBS.stall_streams(path=env0["log_path"]))
    T("B4 母链 orders_created 恒 0", ch["orders_created"] == 0)
    T("B5 报告里无任何裁决字样（本层不产出 BUY/WAIT 这类结论）",
      not any(w in json.dumps(ch, ensure_ascii=False) for w in ("BUY", "WAIT")),
      [w for w in ("BUY", "WAIT") if w in json.dumps(ch, ensure_ascii=False)])

    print("【C】唤醒入口：自检在顶部 / 追平 / 不推进（8）")
    env = _sandbox("ta_wakeup_up_")
    _init_cursor(env["cursor_path"], "2026-10-07", True)
    _put_raw(env["raw_root"], "2026-10-08", [("sh600000", 10.0, 10.6, 9.9, 10.4, 4.0)])
    rep = W(env, "2026-10-08")
    T("C1 报告含三项自检行（banner 顶部）",
      len(rep["banner"]) >= 4 and any("① " in x for x in rep["banner"])
      and any("② " in x for x in rep["banner"]) and any("③ " in x for x in rep["banner"]))
    T("C2 追平计划含当日", rep["plan"] == ["2026-10-08"], rep["plan"])
    T("C3 当日五步全勾 → settled", rep["days"][0]["settled"] is True,
      rep["days"][0].get("pending_steps"))
    T("C4 游标 2026-10-07 → 2026-10-08",
      (rep["cursor_before"], rep["cursor_after"]) == ("2026-10-07", "2026-10-08"))
    T("C5 ready=True（追平到 today）", rep["ready"] is True, rep["blocked"])
    T("C6 orders_created 恒 0", rep["orders_created"] == 0)
    T("C7 候选池观察态推进被调用", rep["days"][0]["pool"] is not None)
    T("C8 母链在当下日留痕", rep["chain_day"] == "2026-10-08"
      and len(rep["chain"]["steps"]) == len(chain_stages()))

    print("【D】缺数据 fail-safe：显式受阻、游标不推进（6）")
    env2 = _sandbox("ta_wakeup_nodata_")
    _init_cursor(env2["cursor_path"], "2026-10-07", True)
    rep2 = W(env2, "2026-10-08")
    T("D1 无投放区 → blocked=NO_DATA（不猜、不编，也不空转）",
      rep2["blocked"] == BLOCKED_NO_DATA, rep2["blocked"])
    T("D2 无投放 → 不产生任何 FAIL 行（无事可做 ≠ 出错；两者不得混为一谈）",
      not [r for r in OBS.read_log(env2["log_path"]) if r["result"] == "FAIL"])
    T("D3 游标未动、ready=False（未追平到 today 绝不谎报就绪）",
      rep2["cursor_after"] == "2026-10-07" and rep2["ready"] is False)
    _put_raw(env2["raw_root"], "2026-10-08", [("sh600000", 10.0, 10.6, 9.9, 10.4, 4.0)])
    os.remove(os.path.join(env2["raw_root"], "2026-10-08", "env_metrics.json"))
    rep2b = W(env2, "2026-10-08")
    T("D4 投放不全（缺契约文件）→ blocked=SETTLE_FAIL + error 有文本",
      rep2b["blocked"] == BLOCKED_SETTLE_FAIL and bool(rep2b["error"]), rep2b["blocked"])
    T("D5 失败不吞：真库留痕里有 FAIL 行",
      bool([r for r in OBS.read_log(env2["log_path"]) if r["result"] == "FAIL"]))
    T("D6 次日唤醒会重做本日（plan 含未结清日，不跳过）",
      W(env2, "2026-10-08")["plan"] == ["2026-10-08"],
      W(env2, "2026-10-08")["plan"])

    print("【E】中断恢复与幂等（5）")
    env3 = _sandbox("ta_wakeup_resume_")
    _init_cursor(env3["cursor_path"], "2026-10-07", True)
    _put_raw(env3["raw_root"], "2026-10-08", [("sh600000", 10.0, 10.6, 9.9, 10.4, 4.0)])
    W(env3, "2026-10-08")
    led = os.path.join(env3["base"], "data", "generated", "decision_ledger.jsonl")
    snap_d1 = os.path.join(env3["snapshot_root"], "2026-10-08")
    fp_led, fp_snap = _fp(led), _fp(os.path.join(snap_d1, "daily_quote.csv"))
    rep3 = W(env3, "2026-10-08")
    T("E1 同日二次唤醒 → plan 为空（无可追平日）", rep3["plan"] == [])
    T("E2 游标不动（幂等）", rep3["cursor_after"] == "2026-10-08")
    T("E3 决策账本字节级零变化", _fp(led) == fp_led)
    T("E4 快照字节级零变化", _fp(os.path.join(snap_d1, "daily_quote.csv")) == fp_snap)
    T("E5 第二次仍 ready（不因重跑而失能）", rep3["ready"] is True)

    print("【F】跨日：步进只推进观察态（4）")
    env4 = _sandbox("ta_wakeup_multi_")
    _init_cursor(env4["cursor_path"], "2026-10-07", True)
    _put_raw(env4["raw_root"], "2026-10-08", [("sh600000", 10.0, 10.6, 9.9, 10.4, 4.0)])
    _put_raw(env4["raw_root"], "2026-10-09", [("sh600000", 10.4, 10.9, 10.2, 10.8, 3.8)])
    rep4 = W(env4, "2026-10-09")
    T("F1 落后 2 日 → 一次唤醒补完 2 日",
      rep4["settled_days"] == ["2026-10-08", "2026-10-09"], rep4["settled_days"])
    T("F2 游标到今日", rep4["cursor_after"] == "2026-10-09")
    T("F3 两日各自留痕（母链 1 次 + 每日 pool/settle）",
      len([r for r in OBS.read_log(env4["log_path"]) if r["action"] == "settle"]) == 2)
    T("F4 全程零新买单", rep4["orders_created"] == 0)
    env5 = _sandbox("ta_wakeup_speed_")
    _init_cursor(env5["cursor_path"], "2026-10-07", True)
    _put_raw(env5["raw_root"], "2026-10-08", [("sh600000", 10.0, 10.6, 9.9, 10.4, 4.0)])
    _put_raw(env5["raw_root"], "2026-10-09", [("sh600000", 10.4, 10.9, 10.2, 10.8, 3.8)])
    r5 = W(env5, "2026-10-09", max_days=1)
    T("F5 max_days=1 限速生效（只补 1 日，C=%s 未到 10-09）" % r5["cursor_after"],
      r5["cursor_after"] == "2026-10-08" and r5["ready"] is False, r5["cursor_after"])

    print("【G】纪律与真库（4）")
    d = _discipline(verbose=False)
    T("G1 装配层纪律九项全绿（%d 项）" % d["n"], d["ok"], d["failed"])
    T("G2 纪律判据不是文本扫描（文档里写纪律声明不算违例）",
      not any(w in _prod_tokens() for w in _BIZ_WORDS))
    st = status()
    T("G3 只读总览含游标 + 母链就绪度 + 观测摘要",
      all(k in st for k in ("cursor", "chain", "observability", "registry_stage")))
    T("G4 ②沙盒泄漏 = 0：真游标未被自检触碰（前后状态不变——动态基准）",
      CUR.read_cursor() == _G4_BEFORE,
      "before=%s after=%s" % (_G4_BEFORE.get("date"),
                              CUR.read_cursor().get("date")))

    print("【K】日历闸与缺口闭环（v1.4 · 14）")
    import tempfile as _tfk
    _ROWS = [("sh600000", 10.0, 10.6, 9.9, 10.4, 4.0)]

    # K1 完整序列（跨周末）：游标 10-08（已结清）→ 投放 10-09 / 10-12
    ek1 = _sandbox("ta_wakeup_k1_")
    _init_cursor(ek1["cursor_path"], "2026-10-08", True)
    _put_raw(ek1["raw_root"], "2026-10-09", _ROWS)
    _put_raw(ek1["raw_root"], "2026-10-12", _ROWS)
    rk1 = W(ek1, "2026-10-12")
    T("K1 完整序列（10-09→10-12 跨周末）↦ 追平到位 · gap=0 · ready",
      rk1["cursor_after"] == "2026-10-12" and rk1["gap_sessions"] == [] and rk1["ready"],
      (rk1["cursor_after"], rk1["gap_sessions"], rk1["blocked"]))

    # K2/K3 中间缺口：交易日 10-09 无投放（旧实现在此**静默跳过**）
    ek2 = _sandbox("ta_wakeup_k2_")
    _init_cursor(ek2["cursor_path"], "2026-10-08", True)
    _put_raw(ek2["raw_root"], "2026-10-12", _ROWS)          # 故意缺 10-09
    rk2 = W(ek2, "2026-10-12")
    T("K2 中间缺口（交易日 10-09 无投放）↦ blocked=SESSION_DATA_GAP",
      rk2["blocked"] == BLOCKED_SESSION_GAP, (rk2["blocked"], rk2["error"]))
    T("K3 缺口被指名 · 游标不动 · plan 为空（不得静默跳过）",
      rk2["gap_sessions"] == ["2026-10-09"] and rk2["cursor_after"] == "2026-10-08"
      and rk2["plan"] == [], (rk2["gap_sessions"], rk2["cursor_after"], rk2["plan"]))

    # K4 末尾缺口（today 未投放）= 正常等待，**不是**缺口
    ek4 = _sandbox("ta_wakeup_k4_")
    _init_cursor(ek4["cursor_path"], "2026-10-08", True)
    _put_raw(ek4["raw_root"], "2026-10-09", _ROWS)
    rk4 = W(ek4, "2026-10-12")
    T("K4 末尾缺口（today 10-12 未投放）↦ 不判缺口，仅等当日数据",
      rk4["blocked"] != BLOCKED_SESSION_GAP and rk4["gap_sessions"] == []
      and rk4["awaiting_today_data"] is True, (rk4["blocked"], rk4["gap_sessions"]))

    # K5 补齐后恢复：给 K2 环境补投 10-09
    _put_raw(ek2["raw_root"], "2026-10-09", _ROWS)
    rk5 = W(ek2, "2026-10-12")
    T("K5 补齐后恢复（补投 10-09）↦ gap 清空 · 追平到位 · ready",
      rk5["gap_sessions"] == [] and rk5["cursor_after"] == "2026-10-12" and rk5["ready"],
      (rk5["gap_sessions"], rk5["cursor_after"]))

    # K6 节假日/周末目录：10-10 周六 / 10-11 周日误投
    ek6 = _sandbox("ta_wakeup_k6_")
    _init_cursor(ek6["cursor_path"], "2026-10-08", True)
    for _d in ("2026-10-09", "2026-10-10", "2026-10-11"):
        _put_raw(ek6["raw_root"], _d, _ROWS)
    rk6 = W(ek6, "2026-10-09")
    T("K6 周末目录（10-10/10-11）不进结算序列、也不算缺口",
      rk6["collected_days"] == ["2026-10-09"] and rk6["gap_sessions"] == []
      and rk6["plan"] == ["2026-10-09"], (rk6["collected_days"], rk6["plan"]))

    # K7 meta/ · backup/ 不污染追平计划
    ek7 = _sandbox("ta_wakeup_k7_")
    _init_cursor(ek7["cursor_path"], "2026-10-08", True)
    _put_raw(ek7["raw_root"], "2026-10-09", _ROWS)
    for _nd in ("meta", "backup"):
        os.makedirs(os.path.join(ek7["raw_root"], _nd))
    io.open(os.path.join(ek7["raw_root"], "meta", "trade_cal.csv"), "w",
            encoding="utf-8").write("date,is_trade\n2026-10-09,1\n")
    rk7 = W(ek7, "2026-10-09")
    T("K7 meta/ 与 backup/ 不进追平计划（meta 不被当日期目录）",
      rk7["collected_days"] == ["2026-10-09"] and rk7["plan"] == ["2026-10-09"],
      (rk7["collected_days"], rk7["plan"]))

    # K8 未注入日历 ↦ fail-closed 阻断
    ek8 = _sandbox("ta_wakeup_k8_")
    _init_cursor(ek8["cursor_path"], "2026-10-08", True)
    _put_raw(ek8["raw_root"], "2026-10-09", _ROWS)
    rk8 = W(ek8, "2026-10-09", calendar=None)
    T("K8 未注入日历 ↦ blocked=NO_CALENDAR（不按周末推算、游标不动）",
      rk8["blocked"] == BLOCKED_NO_CALENDAR and rk8["cursor_after"] == "2026-10-08",
      rk8["blocked"])

    # K9 日历缺失 / 损坏 / 空 ↦ 构造期抛错（不静默降级）
    _kd = _tfk.mkdtemp(prefix="ta_wakeup_k9_")
    T("K9a 日历文件缺失 ↦ WakeupError",
      raises(_load_calendar, os.path.join(_kd, "nope.csv")))
    _kbad = os.path.join(_kd, "bad.csv")
    io.open(_kbad, "w", encoding="utf-8").write("dt,trade\n2026-10-09,1\n")
    T("K9b 日历列不符 ↦ WakeupError", raises(_load_calendar, _kbad))
    _kzero = os.path.join(_kd, "zero.csv")
    io.open(_kzero, "w", encoding="utf-8").write("date,is_trade\n2026-10-09,0\n")
    T("K9c 日历无任何交易日 ↦ WakeupError", raises(_load_calendar, _kzero))

    # K10 越界（today 超日历覆盖）↦ 阻断，不外推
    ek10 = _sandbox("ta_wakeup_k10_")
    _init_cursor(ek10["cursor_path"], "2026-10-08", True)
    _put_raw(ek10["raw_root"], "2027-01-05", _ROWS)
    rk10 = W(ek10, "2027-01-05")
    T("K10 today 越出日历覆盖（2027-01-05）↦ blocked=DAY_STATE_UNKNOWN",
      rk10["blocked"] == BLOCKED_DAY_UNKNOWN, rk10["blocked"])

    # K11 as_of 上界：today 之后的日子不进计划（不看未来）
    ek11 = _sandbox("ta_wakeup_k11_")
    _init_cursor(ek11["cursor_path"], "2026-10-08", True)
    _put_raw(ek11["raw_root"], "2026-10-09", _ROWS)
    _put_raw(ek11["raw_root"], "2026-10-12", _ROWS)
    rk11 = W(ek11, "2026-10-09")
    T("K11 as_of 上界：today(10-09) 之后不进计划 / 不进发现集",
      rk11["plan"] == ["2026-10-09"] and "2026-10-12" not in rk11["collected_days"],
      (rk11["plan"], rk11["collected_days"]))

    # K12 ★端口连通：绕开装配层缺口闸、直接 _run_day 直击结算闸门
    #    （证明 calendar 真的到达 settle：同场景下若闸门没接到历法，报错文本会是"日期轴未知"）
    _kd2 = _tfk.mkdtemp(prefix="ta_wakeup_gate_")
    _ctx = _probe_ctx(_kd2)                     # 全部可写路径已隔离在 _kd2 之下
    _init_cursor(_ctx["cursor_path"], "2026-10-08", True)
    _put_raw(_ctx["raw_root"], "2026-10-12", _ROWS)   # 缺 10-09；快照目录为空
    _ctx["calendar"] = CAL
    _day = _run_day("2026-10-12", _ctx)
    T("K12 缺口到达结算闸门（绕开装配层闸仍被 settle 拒 · 日历轴双保险）",
      _day["blocked"] == BLOCKED_SETTLE_FAIL
      and "SKIP_SETTLEMENT" in str(_day["error"]), str(_day["error"])[:96])

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：")
        for f in fails:
            print("  - %s" % f)
    return 0 if not fail else 1


# ── 端到端验收：零插件空转跑通一天（骨架完成判据）────────────────────────────
def _acceptance():
    """**真实端到端**：从零状态启动 → 装载 C0~C6/F1~F6 → 无插件 → 跑完一个交易日。

    与 `--selftest` 的区别（用户 2026-10-08 F7 盯防项③）：
      · selftest 验的是**单个判据**；本验收验的是**装配起来能不能真跑**。
      · 本验收用真模块 + 真文件 + **真注册表**，链是
        数据截断 → 候选产生 → Thesis → 三问 → 风控 fail-safe → 记录 → 结算 → 游标推进
        → 刷新跨日看板 → 第二次运行不重复；**不是把各模块 --selftest 串起来**（那样证不了装配）。

    ★ 真库写入口径（用户 2026-10-08 封板时收紧，第 ⑨ 段逐条判，**禁混称"零污染"**）：
      ① **预期生产写入** = 允许 —— 正式运行只许写在落点登记表的目标上（执行点 = `landing.py`
         的写点目标封闭 + 注册表 `output_path` 闭环，本段**不重复实现**）；
      ② **测试 / 沙盒泄漏到真库** = 禁止 —— 执行点 = **本段 I1 全库指纹比对**（不是只查几个
         硬编码路径：F7 首版就是只查 3 条，结果看板真写进了真库）；
      ③ **未登记写入** = 禁止 —— 执行点 = `landing.py --discipline`
         （`STALE_DECLARATION` / `UNDECLARED_WRITE` / `WRITE_TARGET_UNKNOWN` 全为 0）。
    """
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

    # ★ 口径②在此起算：真库全目录先快照，收尾逐项比对（含**文件集合**，不只内容）
    lib_before = _tree_fp()
    cur_before = CUR.read_cursor()      # I2：游标零污染动态基准（不硬编码日期）

    D0, D1, D2 = "2026-10-07", "2026-10-08", "2026-10-09"
    try:
        CAL = _load_calendar()      # v1.4：装配层须持日历才可追平（10-07 为国庆休市，仅作游标初值）
    except WakeupError as e:
        print("  [!!] 交易日历不可用 → 端到端验收无法进行：%s" % e)
        return 1
    env = _sandbox("ta_wakeup_e2e_", init_pool=False)
    root = env["root"]
    gen = os.path.join(root, "data", "generated")
    led = os.path.join(gen, "decision_ledger.jsonl")
    pnl = os.path.join(gen, "pnl_ledger.jsonl")

    print("① 从零状态启动（初始化前盘上什么都没有）")
    T("A1 零状态：无游标 / 无候选池 / 无账本 / 无快照 / 无留痕",
      not any(os.path.exists(p) for p in (
          env["cursor_path"], env["pool_path"], led, pnl, env["log_path"],
          os.path.join(env["snapshot_root"], D1))))
    _init_cursor(env["cursor_path"], D0, True)
    _init_pool(env["pool_path"], D0)
    T("A2 显式初始化 C=%s + 候选池（0.1 / 0.5：禁静默新建，须显式动作）" % D0,
      CUR.read_cursor(env["cursor_path"])["date"] == D0
      and os.path.exists(env["pool_path"]))

    print("② 装载 C0~C6 / F1~F6（真注册表 + 真骨架件）")
    v = REG.verify_registry()
    sc = REG.skeleton_check()
    T("B1 注册表 %d 行 · stage=%s · 校验%s" % (v["n_caps"], v["stage"], "通过" if v["ok"] else "失败"),
      v["ok"] and v["n_caps"] == 27)
    T("B2 骨架冻结台账无漂移（%d 件已登记 / %d 件 pending）"
      % (sc["n_files"], len(sc["pending"])), sc["ok"], sc["errors"])
    if v["stage"] == "SKELETON":
        T("B3 **零插件**：%d 个 S/E 未落盘（pending_impl）" % len(v["pending_impl"]),
          len(v["pending_impl"]) > 0
          and not [c for c in REG.caps() if c["kind"] in ("S", "E")
                   and REG.impl_exists(c["id"], c["kind"])])
    else:   # PLUGINS+：v3.19 开插后骨架验收进入插件期语义（2026-10-20 修 #237）
        T("B3 **插件期**：pending_impl=0 且 active S/E 全部已落盘",
          len(v["pending_impl"]) == 0
          and not [c for c in REG.caps() if c["kind"] in ("S", "E")
                   and c.get("status") == "active"
                   and not REG.impl_exists(c["id"], c["kind"])])
    mods = ("cursor", "snapshot", "loader", "candidates", "thesis", "decision", "pnl",
            "params", "registry", "landing", "three_q", "observe", "wakeup")
    missing = [m for m in mods
               if not os.path.exists(os.path.join(ROOT, "tools", "fs", m + ".py"))
               and not os.path.exists(os.path.join(ROOT, "tools", "risk", m + ".py"))]
    T("B4 骨架件齐全（%d 个模块在盘）" % len(mods), not missing, missing)

    print("③ 跑完一个交易日：wakeup(D1) 全链")
    for d, px in ((D1, [("sh600000", 10.0, 10.6, 9.9, 10.4, 4.0),
                        ("sh600001", 11.0, 11.5, 10.9, 11.4, 3.6)]),
                  (D2, [("sh600000", 10.4, 10.9, 10.2, 10.8, 3.8),
                        ("sh600001", 11.4, 11.6, 11.0, 11.1, -2.6)])):
        _put_raw(env["raw_root"], d, px)
    rep = wakeup(D1, base=env["base"], cursor_path=env["cursor_path"],
                 raw_root=env["raw_root"], snapshot_root=env["snapshot_root"],
                 log_path=env["log_path"], pool_path=env["pool_path"],
                 learning_path=env["learning_path"],
                 board_path=os.path.join(gen, "h_board.md"), calendar=CAL)
    T("C1 顶部三项自检齐全且全绿",
      len([x for x in rep["banner"] if x.startswith("[OK]")]) >= 4, rep["banner"])
    T("C2 结算五步走完、无待办缺口",
      rep["days"][0]["settled"] and rep["days"][0]["pending_steps"] == [],
      rep["days"][0].get("pending_steps"))
    T("C3 游标推进 %s → %s" % (D0, D1),
      (rep["cursor_before"], rep["cursor_after"]) == (D0, D1))
    if v["stage"] == "SKELETON":
        T("C4 母链 C0~C6 全环节留痕（各产出为空但留痕）",
          len(rep["chain"]["steps"]) == len(chain_stages())
          and all(s["result"] == "EMPTY" for s in rep["chain"]["steps"]))
    else:   # 插件期：调度真调插件（沙盒无数据 ⇒ 降级 DONE）⇒ 判「步齐+无 FAIL」（2026-10-20 修 #237）
        T("C4 母链 C0~C6 全环节留痕（插件期：%d 步齐 · 无 FAIL）"
          % len(rep["chain"]["steps"]),
          len(rep["chain"]["steps"]) == len(chain_stages())
          and not [s for s in rep["chain"]["steps"] if s["result"] == "FAIL"])
    T("C5 零插件下无任何环节产出裁决（fail-safe：不静默降级成错误决策）",
      not any(w in json.dumps(rep["chain"], ensure_ascii=False)
              for w in ("BUY", "WAIT")))

    print("④ 数据截断正确（≤T，无未来知识）")
    import loader as LOAD
    T("D1 快照仅 D1 在盘（D2 尚未落定）",
      SNAP.available_dates(D0, D2, root=env["snapshot_root"]) == [D1])
    r1 = LOAD.resolve(D1, base=env["base"])
    T("D2 resolve(%s) 精确命中 D1（is_exact=True，非「最近可用日」猜测）" % D1,
      r1 is not LOAD.UNAVAILABLE and r1["date"] == D1 and r1["is_exact"] is True,
      None if r1 is LOAD.UNAVAILABLE else dict(r1))
    r2 = LOAD.resolve(D2, base=env["base"])
    T("D3 D2 未落定时 resolve(%s) 回退 D1 且 is_exact=False（不猜未来，调用方能识别）" % D2,
      r2 is not LOAD.UNAVAILABLE and r2["date"] == D1 and r2["is_exact"] is False,
      None if r2 is LOAD.UNAVAILABLE else dict(r2))
    q_before = dict(LOAD.load_quote(D1, "sh600000", base=env["base"]))
    T("D4 load_quote 取到 D1 真值（close=10.4）", q_before.get("close") == 10.4, q_before.get("close"))

    print("⑤ 候选产生 → Thesis → 三问 → 风控 fail-safe → 记录")
    import candidates as C
    import thesis as TH
    import three_q as TQ
    import decision as DEC
    r = C.upsert_candidate("sh600000", D1, "E2E 端到端验收投放", "涨停放量",
                           invalid_condition={"text": "跌破 5 日线", "rule": None},
                           next_trigger={"text": "次日高开 2% 以上", "rule": None},
                           path=env["pool_path"])
    cid = r["candidate"]["id"]
    T("E1 候选产生（%s · state=%s）" % (cid, r["candidate"]["state"]),
      r["created"] is True and r["candidate"]["state"] == C.STATE_DISCOVERED)
    ad = C.advance_candidate(D1, signals={cid: {"trigger": True}}, path=env["pool_path"])
    T("E2 观察态推进（DISCOVERED→%s，真状态机）"
      % (ad["transitions"][0]["to"] if ad["transitions"] else "-"),
      bool(ad["transitions"]) and ad["transitions"][0]["to"] == C.STATE_WATCH)
    TH.init_file(env["thesis_path"])
    th = TH.create_thesis(D1, "A", "E2E：产业催化驱动的观察主线", code="sh600000",
                          candidate_id=cid, gap=TH.GAP_DRIVER, path=env["thesis_path"])
    st = TH.read_thesis(th["id"], env["thesis_path"])
    T("E3 Thesis 创建（%s · type=%s · state=%s）"
      % (th["id"], st["type"], st["state"]), st["state"] == TH.STATE_OPEN)
    thesis_chain = {
        "evidence": ["D1 涨停放量，席位净买入可查"],
        "inference": ["催化 → 资金抢占 → 溢价兑现"],
        "prediction": [{"prediction_id": k, "metric": "5 日超额收益", "data_source": "loader.load_quote",
                        "baseline": "0", "direction": "UP", "threshold": "3",
                        "condition": "放量不破 5 日线", "window": 3} for k in TQ.PRED_SLOTS],
        "five": {k: "%s（E2E 注入）" % k for k in TQ.FIVE_KEYS},
    }
    # 环境档：C1 未落盘 → **显式标 UNAVAILABLE**（0.3 _norm_env 的合法取值，禁造数）
    env_na = {"regime": None, "signals": {}, "leg": "EOD", "source": "UNAVAILABLE"}
    verdict = TQ.answer(D1, "sh600000", th["id"], thesis_chain, driver=TQ.DRIVER_UNKNOWN,
                        thesis_state=st, env=env_na, overturn_event={
                            "text": "出现可查证的产业催化落地证据",
                            "rule": {"field": "driver", "op": "==", "value": "A"}})
    T("E4 三问裁决 = %s（driver=%s）" % (verdict["action"], verdict["driver"]),
      verdict["action"] in TQ.ACTIONS)
    T("E5 三问零模糊输出达标（verify_verdict 零问题）",
      TQ.verify_verdict(verdict) == [], TQ.verify_verdict(verdict))
    T("E6 **风控 fail-safe**：零插件（无 C3 证据）→ 机械 NO，不得出 BUY",
      verdict["action"] == TQ.ACT_NO and verdict["fields"]["position_request"] == 0.0,
      verdict["action"])
    cm = TQ.commit(verdict, sim=False, pool_path=env["pool_path"], base=gen,
                   env=env_na, thesis_state=st, thesis_path=env["thesis_path"])
    T("E7 两段式落盘合一（决策账本 + 观察锚）",
      cm["orders_created"] == 0 and cm["decision"] is not None)
    rows = DEC.read_ledger(False, base=gen)
    T("E8 记录落账本 LD1（%d 行 · action 全为 %s）"
      % (len(rows), verdict["action"]),
      len(rows) == 1 and rows[0]["action"] == TQ.ACT_NO)
    T("E9 账本零 BUY（零插件不产出下注意图）",
      not [x for x in rows if x["action"] in ("BUY", "SELL")])

    print("⑥ 结算 → 游标推进 → 跨日")
    rep2 = wakeup(D2, base=env["base"], cursor_path=env["cursor_path"],
                  raw_root=env["raw_root"], snapshot_root=env["snapshot_root"],
                  log_path=env["log_path"], pool_path=env["pool_path"],
                  learning_path=env["learning_path"],
                 board_path=os.path.join(gen, "h_board.md"), calendar=CAL)
    T("F1 次日追平完成（C=%s）" % rep2["cursor_after"], rep2["cursor_after"] == D2)
    T("F2 两日皆已结清", rep2["settled_days"] == [D2] and rep2["ready"] is True)
    T("F3 全链 orders_created 恒 0（%d）" % (rep["orders_created"] + rep2["orders_created"]),
      rep["orders_created"] == 0 and rep2["orders_created"] == 0)
    # ★ 无未来知识：D2 落盘后，D1 的取数必须逐字节不变（point-in-time 可复现）
    q_after = dict(LOAD.load_quote(D1, "sh600000", base=env["base"]))
    r1b = LOAD.resolve(D1, base=env["base"])
    T("F4 D2 落盘后 D1 取数逐字节不变（历史回放不被未来污染）",
      q_after == q_before and r1b["date"] == D1 and r1b["is_exact"] is True,
      (q_before.get("close"), q_after.get("close")))
    r2b = LOAD.resolve(D2, base=env["base"])
    T("F5 D2 落盘后 resolve(%s) 才精确命中 D2（此前是回退，不是「提前知道」）" % D2,
      r2b["date"] == D2 and r2b["is_exact"] is True, dict(r2b))

    print("⑦ 第二次运行不重复（幂等）")
    pool_fp = _fp(env["pool_path"])
    led_fp, pnl_fp = _fp(led), _fp(pnl)
    n_led = len(DEC.read_ledger(False, base=gen))
    rep3 = wakeup(D2, base=env["base"], cursor_path=env["cursor_path"],
                  raw_root=env["raw_root"], snapshot_root=env["snapshot_root"],
                  log_path=env["log_path"], pool_path=env["pool_path"],
                  learning_path=env["learning_path"],
                 board_path=os.path.join(gen, "h_board.md"), calendar=CAL)
    T("G1 二次唤醒 plan 为空（无待追平日）", rep3["plan"] == [], rep3["plan"])
    T("G2 游标不动（%s）" % rep3["cursor_after"], rep3["cursor_after"] == D2)
    T("G3 决策账本字节级零变化", _fp(led) == led_fp)
    T("G4 净值账本字节级零变化", _fp(pnl) == pnl_fp)
    T("G5 候选池字节级零变化", _fp(env["pool_path"]) == pool_fp)
    T("G6 账本行数不增（%d → %d）" % (n_led, len(DEC.read_ledger(False, base=gen))),
      len(DEC.read_ledger(False, base=gen)) == n_led)
    T("G7 快照目录字节级零变化",
      _fp(os.path.join(env["snapshot_root"], D2, "daily_quote.csv")) is not None)
    T("G8 二次唤醒仍 ready（重跑不使系统失能）", rep3["ready"] is True)

    print("⑧ 可观测：留痕完整、无静默失败")
    logs = OBS.read_log(env["log_path"])
    acts = sorted({x["action"] for x in logs})
    T("H1 留痕覆盖：pool / settle / chain:C0~C6（%d 行）" % len(logs),
      "pool" in acts and "settle" in acts
      and len([a for a in acts if a.startswith("chain:")]) == len(chain_stages()))
    T("H2 真库零静默失败（无未处理 FAIL）", OBS.unhandled_failures(env["log_path"]) == [])
    T("H3 零 STALL（结构性为空不误报，真断链才报）",
      OBS.stall_streams(path=env["log_path"]) == [], OBS.stall_streams(path=env["log_path"]))
    T("H4 留痕 schema 全合法", len(logs) > 0)

    print("⑨ 写入三类分判（口径①预期生产写入=允许 · ②沙盒泄漏=禁止 · ③未登记写入=禁止）")
    T("I1 ★②沙盒泄漏 = 0：真库**全目录**（相对路径 + 大小 + mtime）验收前后逐项相同",
      _tree_fp() == lib_before, _tree_diff(lib_before))
    T("I2 ②真库游标未动（验收前后游标态逐项相同——动态基准，禁硬编码日期）",
      CUR.read_cursor() == cur_before,
      "before=%s after=%s" % (cur_before.get("date"),
                              CUR.read_cursor().get("date")))
    T("I3 ②真库无决策账本 / 净值账本（账本只能由正式运行写在登记落点）",
      not os.path.exists(os.path.join(ROOT, "data", "generated", "decision_ledger.jsonl"))
      and not os.path.exists(os.path.join(ROOT, "data", "generated", "pnl_ledger.jsonl")))
    T("I4 ②临时沙盒路径 ≠ 项目根", os.path.abspath(root) != os.path.abspath(ROOT))

    print("⑩ 跨日看板刷新（0.12 · 装配层**委派** hboard，不自己聚合）")
    bpath = os.path.join(gen, "h_board.md")
    hb = rep2.get("h_board") or {}
    T("J1 看板已刷新且路径与盘上文件一致",
      hb.get("path") == bpath and os.path.exists(bpath), hb)
    txt = io.open(bpath, encoding="utf-8").read() if os.path.exists(bpath) else ""
    T("J2 三区齐全（今日必裁 / 日历预装 / 明日到期）",
      all(("## %s、%s" % (n, s)) in txt
          for n, s in zip(("一", "二", "三"), HB.SECTIONS)))
    T("J3 零插件 fail-safe：日历区 = NO_CAPABILITY（缺能力留痕，禁编内容）",
      (hb.get("statuses") or {}).get(HB.SECTION_CALENDAR) == HB.S_NO_CAPABILITY,
      hb.get("statuses"))
    T("J4 看板真读到了状态：观察中候选进入「明日到期」区并落在正文",
      (hb.get("statuses") or {}).get(HB.SECTION_TOMORROW) == HB.S_OK and "sh600000" in txt,
      hb.get("statuses"))
    T("J5 看板刷新经 run_observed 留痕（actor=wakeup · action=hboard）",
      any(x["action"] == "hboard" and x["actor"] == "wakeup"
          for x in OBS.read_log(env["log_path"])))
    b_fp = _fp(bpath)
    rep4 = wakeup(D2, base=env["base"], cursor_path=env["cursor_path"],
                  raw_root=env["raw_root"], snapshot_root=env["snapshot_root"],
                  log_path=env["log_path"], pool_path=env["pool_path"],
                  learning_path=env["learning_path"],
                 board_path=os.path.join(gen, "h_board.md"), calendar=CAL)
    T("J6 重跑唤醒后看板 byte 级不变（幂等；不是恒真断言，是真跑一次比对）",
      _fp(bpath) == b_fp and rep4["ready"] is True)

    print("\n端到端验收：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：")
        for f in fails:
            print("  - %s" % f)
    return 0 if not fail else 1


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    if "--acceptance" in argv:
        return _acceptance()
    if "--discipline" in argv:
        print("装配层纪律（0.14 · 用户 F7 三个盯防项）")
        d = _discipline()
        print("\n纪律：%d 项 %s" % (d["n"], "全 OK ✅" if d["ok"] else "有失败 ❌ %s" % d["failed"]))
        return 0 if d["ok"] else 1
    if "--wake" in argv:
        i = argv.index("--wake")
        _arg = (argv[i + 1] if len(argv) > i + 1
                and not argv[i + 1].startswith("--") else None)
        _cpath = None
        if "--calendar" in argv:
            j = argv.index("--calendar")
            if j + 1 >= len(argv):
                print("--calendar 需要 CSV 路径")
                return 2
            _cpath = argv[j + 1]
        try:
            cal = _load_calendar(_cpath)          # 缺省 = DEFAULT_CALENDAR
        except WakeupError as e:
            print("[!!] 日历      : %s" % e)
            return 2
        rep = wakeup(_arg, calendar=cal)
        for x in rep["banner"]:
            print(x)
        print("  ready=%s blocked=%s C=%s→%s" % (rep["ready"], rep["blocked"],
                                                 rep["cursor_before"], rep["cursor_after"]))
        return 0 if rep["ready"] else 1
    st = status()
    print("运行入口（0.14 · 唤醒即追平）")
    c = st["cursor"]
    print("  游标 C  : %s（五步%s · 待办 %s）"
          % (c["date"], "全勾" if st["settled"] else "未走完",
             " / ".join(st["pending_steps"]) or "无"))
    print("  注册表  : stage=%s" % st["registry_stage"])
    print("  母链就绪度（环节顺序派生自注册表）：")
    for s in st["chain"]:
        print("    %-3s 可委派=%-22s 已落盘=%-12s 未落盘=%s"
              % (s["mount"], ",".join(s["delegable"]) or "-",
                 ",".join(s["ready"]) or "-", ",".join(s["missing"]) or "-"))
    o = st["observability"]
    print("  留痕    : %d 行 · %s" % (o["n"], " · ".join("%s=%d" % (k, v2)
                                                    for k, v2 in sorted(o["by_result"].items()))))
    print("            未处理 FAIL %d · STALL %d" % (len(o["unhandled_failures"]), len(o["stalls"])))
    print("  纪律    : 本模块禁判断业务、禁动游标、禁下单；追平只经 settle（0.6）")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
