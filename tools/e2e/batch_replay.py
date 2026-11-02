#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch_replay.py · 真实历史多日批量回放（任务 8.2 / 8.4 · v1.0 · 2026-10-10 建 · v1.2）

变更记录 v1.2（P0 逐笔审计修复 · 2026-10-10）：
  · **同日同码去重**（执行层缺陷修复）：同一 code 当日只评审一次。审计实证：A 路多
    事件（sh688825 双公告）/ B+A 同票时同日双开仓——sz000823 双开各 0.2（单票 40%）、
    sh688766 三开各 0.2（单票 60%），C5 每笔独立评估的 holdings 物化不含同日另一笔
    → 单票权益顶 39% 被穿透。去重闸在本 harness 选样层兜底；C5 组合视角
    （holdings 应含当日已批未成交 plan）为系统性缺口，另行立项待裁决。
  · pnl 结算语义随 pnl v1.2（outcome=最终盈亏结果 + exit_kind/exit_date 落账）。

用途：把单日真实回放（real_replay.py）扩展为**多日、可复现、可审计**的历史回放：
      旧包真实数据逐日投放 → 正式化 → C1 环境 → C2 候选 → C3 驱动（A 事件 / B 资金）
      → C4 三问 → C5 风控 → 模拟成交（口径 v1.1）→ 逐日结算 → 净值 → 统计报告。
      每个交易日的结果**只由当日真实历史输入驱动**；BUY/WAIT/NO 由机械规则 + 真实
      输入共同决定，不预设、不强造交易；亏损如实落账。

★ 执行口径 v1.1（与 settle v1.1 一致，前视修复）：
  决策 = D 盘后（信息截止 = D 日终：收盘行情 / 当日公告 / 龙虎榜 / 环境信号——
  假设 A1：LHB 与晚间公告当晚可得，采集痕迹 fetch_log 佐证不足处已标记）；
  成交 = D 后第一个交易日**开盘价**（限价 = D 收盘×1.02 容忍带，开盘 > 限价 → 不追价
  不成交；一字板 / 开盘涨停 → 不成交）；T+1：成交日买入最早次一交易日卖出；
  止损/目标自 T+1 起检查；交易成本 v1.2 起**正式入账**（pnl 行 fee_amount/net_amount，
  费率读 params.json cost_buy_rate/cost_sell_rate 正式参数——报告层不再自扣假设费用）。

★ 窗口（由 data_availability.py 统计选定，不默认 666 天全可回放）：
  W1 全链短窗 2026-09-15..09-30（11 交易日）：五域齐 → 真实 C2 五路扫描全开 +
  S1 事件评估 + LHB 机构榜（榜源）。
  W2 统计窗口 2026-06-01..09-30：quote+env+fundflow+涨停派生齐备；C2 只注入
  B 路事实（fundflow 主力净额 top5 → dragon_tiger 契约行，流源，转换规则在案），
  全五路扫描的消费者接入已由 W1 证明；公告/龙虎榜文件缺的日子 news/LHB=空集
  （空集≠数据为零，缺口逐日列于报告，不静默跳过）。

★ LLM 代位声明：C3 驱动链内容（evidence 文本 / inference / prediction 模板）由本
  harness 按固定机械模板生成——回放验证的是**机制链**（C3 判据 / C4 规则 / C5 闸），
  不是 LLM 判断质量；模板逐条在案，同输入同输出。

★ 防作弊红线：不改生存参数（params 只读）；不放宽 C5；不删亏损样本；judgements
  按结算时点可见数据逐次重算（行内落库值 = 窗口完成日全窗判定）；生产 data/+
  memory/ 前后指纹核验，data/raw 零生成。

用法：
  python tools/e2e/batch_replay.py                       # W1 + W2 全跑（沙盒）
  python tools/e2e/batch_replay.py --window w1           # 只跑 W1
  python tools/e2e/batch_replay.py --full                # 附全量 JSON
  python tools/e2e/batch_replay.py --calendar <csv>      # 注入 B3 交易日历（严格日状态闸）
  python tools/e2e/batch_replay.py --discipline

★ B3 日期闸接入（2026-10-13）：`--calendar` 给日历时 C2 走严格闸（非
  `TRADING_WITH_DATA` 不扫）；未给时显式 `on_unknown="allow"`（本 harness 即
  "无日历历史回放沙盒"这一唯一允许场景），此时唯一的替代保护是 loader `is_exact`
  硬闸 —— **不精确=零行可消费**，杜绝用前一日数据冒充目标日。
"""
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
for _p in ("fs", "analysis", "decision", "risk", "learning", "e2e", "discovery"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import decision as DEC          # noqa: E402
import three_q as TQ            # noqa: E402
import candidates as CAND       # noqa: E402
import cursor as CUR            # noqa: E402
import thesis as TH             # noqa: E402
import settle as SETTLE         # noqa: E402
import position_gate as PG      # noqa: E402
import case_builder as CB       # noqa: E402
import pnl as PNL               # noqa: E402
import params as PARAMS         # noqa: E402   ← 费率真源（v1.2 成本入账，报告层不再自扣）
import drive                    # noqa: E402
import orchestrate as ORCH      # noqa: E402
import s1 as S1MOD              # noqa: E402
import ingest as ING            # noqa: E402
import loader as LD             # noqa: E402
import regime as RG             # noqa: E402
import scan as SCAN             # noqa: E402
import real_replay as RR        # noqa: E402   ← 转换层单一真源（同规则复用）

_TEST_FUNCS = ("run_batch", "_discipline", "_main")   # 沙盒夹具自报（写目标全部=tempfile）
_OLD_ROOT = RR.OLD_ROOT
_TAX = RR._TAX

WINDOWS = {
    "w1": ("2026-09-15", "2026-09-30"),
    "w2": ("2026-06-01", "2026-09-30"),
    # ★ w3（2026-10-23 用户令「所有策略都要研究」）：**长窗 + 五路扫描全开**。
    #   为什么需要它：w1 只有 11 天（五路开但样本太少）；w2 有 4 个月但**只走 B 路机械**
    #   （不跑 scan）⇒ 策略矩阵里 C 情绪/趋势类**一格样本都没有**，A 路只有 6 笔。
    #   「没测过」不等于「无效」，要判断各策略在各环境的表现，必须让五路在长窗里跑起来。
    #   代价：五路扫描是逐日全市场扫（4000+ 只），w3 预计数小时——故独立窗口，按需跑。
    "w3": ("2026-06-01", "2026-09-30"),
}

# 哪些窗口跑**真实五路扫描**（其余只做 B 路机械直选）
SCAN_WINDOWS = ("w1", "w3")
EQUITY0 = 1_000_000
TOP_B = 3            # B 路每日机械选样数（主力净额降序 top3 → 全链评审）
TOP_C = 3            # C 路每日机械选样数（涨停池连板降序 top3，排除已被 B 路的）
B_TOP_N = 5          # fundflow → dragon_tiger 转换行每日条数（快照内真实子集）
REQ = 0.2            # 仓位请求（与单日回放同一模板）
LIMIT_TOL = 1.02     # 限价容忍带（口径 v1.1）


# ── 旧包真实数据（只读；扩展 real_replay 转换层） ────────────────────────────
def _klines_series(old_root, since):
    """扫 klines：逐日封板名单（含连板数）/ 触板数。

    文件事实（2026-10-10 实测）：行序既非按日期也非按代码全局有序（日期列第 236 行
    即乱序、代码交错、单码约 261/666 天）——**任何依赖文件顺序的流式分组都会算错**，
    必须显式按日收集后排序逐日计算。内存量级：since=2026-03-01 约 30 万行元组。

    返回 (series{date: {"sealed": {code:(name,boards)}, "touched": n}}, dates 升序)。
    派生规则与 real_replay._sealed_codes 同一规则（精确涨停价规则）。
    """
    rows_by_date = {}
    with io.open(os.path.join(old_root, "data", "raw", "bus", "klines.csv"),
                 encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = r["date"]
            if d < since:
                continue
            rows_by_date.setdefault(d, []).append(
                (r["code"], r.get("name") or "", r.get("close"), r.get("high")))
    dates = sorted(rows_by_date)
    series = {}
    prev_close = {}                                   # code → 最近已知收盘（跨日保留）
    boards = {}                                       # code → 连板数（未封板次日清零）
    for d in dates:
        sealed, touched, today_close = {}, 0, {}
        for code, name, cl, hi in rows_by_date[d]:
            today_close[code] = cl
            pc = prev_close.get(code)
            if not pc or not cl or not hi:
                continue
            try:
                lim = round(float(pc) * (1 + RR._limit_ratio(code, name)), 2)
                h, c = float(hi), float(cl)
            except (TypeError, ValueError):
                continue
            if h >= lim - 0.005:
                touched += 1
                if abs(c - lim) < 0.005:
                    sealed[code] = (name, boards.get(code, 0) + 1)
        boards = {code: nb for code, (_, nb) in sealed.items()}
        prev_close.update(today_close)
        series[d] = {"sealed": sealed, "touched": touched}
    return series, dates


def _fundflow_by_date(old_root):
    """fundflow.csv → {date: [(code, main_in, main_out, main_net)]}（一次入内存）。"""
    out = {}
    with io.open(os.path.join(old_root, "data", "raw", "bus", "fundflow.csv"),
                 encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                row = (r["code"], float(r["main_in"]), float(r["main_out"]),
                       float(r["main_net"]))
            except (TypeError, ValueError):
                continue
            out.setdefault(r["date"], []).append(row)
    return out


def _zt_pool_hist(old_root):
    """zt_pool _hist → {date: {code: (fbt, fund)}}（首封时间/封单额，缺 → None）。

    fbt 脏值守卫：不符合 HH:MM(:SS) 合同格式的一律按 None 落账（缺就是缺，
    不做格式解释性修复），并在返回值第二项给出逐日脏值计数（报告用）。
    """
    d = os.path.join(old_root, "data", "raw", "bus", "zt_pool")
    out, bad = {}, {}
    if not os.path.isdir(d):
        return out, bad
    for fn in sorted(os.listdir(d)):
        m = re.match(r"zt_(\d{4}-\d{2}-\d{2})_hist\.csv$", fn)
        if not m:
            continue
        day = {}
        n_bad = 0
        with io.open(os.path.join(d, fn), encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                fbt = (r.get("fbt") or "").strip() or None
                if fbt is not None and not _TIME_OK.match(fbt):
                    n_bad += 1
                    fbt = None
                try:
                    fund = float(r["fund"]) if (r.get("fund") or "").strip() else None
                except (TypeError, ValueError):
                    fund = None
                    n_bad += 1
                day[r["code"]] = (fbt, fund)
        out[m.group(1)] = day
        if n_bad:
            bad[m.group(1)] = n_bad
    return out, bad


_TIME_OK = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")


def _fundflow_dt_rows(flow_day):
    """fundflow 当日行 → dragon_tiger 契约行 top-N（主力净额降序，net>0 优先补足）。"""
    rows = sorted(flow_day or [], key=lambda x: -x[3])
    pos = [r for r in rows if r[3] > 0][:B_TOP_N]
    sel = pos
    if len(sel) < B_TOP_N:
        for r in rows:
            if len(sel) >= B_TOP_N:
                break
            if r[3] <= 0 and r not in sel:
                sel.append(r)
    return [{"code": c, "name": None, "seat": "主力资金（fundflow 机采 → 契约映射）",
             "buy": mi, "sell": mo, "net": net} for c, mi, mo, net in sel]


def _payload_for(old_root, D, kb_day, series_day, flow, ztp, env_cache):
    """一日真实数据 → 契约载荷（零补造；缺口=None/空集并在报告标注）。

    返回 (pay, meta)：meta 为 `_` 前缀元数据（报告用，不入 ingest 契约）。
    """
    pay = {}
    pay["daily_quote"] = [dict(kb_day[c], code=c) for c in sorted(kb_day)]
    zt_d = ztp.get(D) or {}
    lu = []
    for code, (name, nb) in sorted(series_day["sealed"].items(),
                                    key=lambda kv: (-kv[1][1], kv[0])):
        fbt, fund = zt_d.get(code, (None, None))
        lu.append({"code": code, "name": name, "boards": nb, "first_seal_time": fbt,
                   "seal_amount": fund, "reason": None})
    pay["limit_up"] = lu
    lhb = RR._legacy_lhb_inst(old_root, D)
    if lhb:
        pay["dragon_tiger"] = lhb
        pay["_dt_source"] = "lhb_inst"
    else:
        pay["dragon_tiger"] = _fundflow_dt_rows(flow.get(D))
        pay["_dt_source"] = "fundflow_top%d" % B_TOP_N
    news, skip_n = RR._legacy_news(old_root, D)
    pay["news"] = news
    pay["_news_skipped_non_a"] = skip_n
    lab, hist = RR._legacy_env(old_root, D)
    max_board = max((x["boards"] for x in lu), default=0)
    touched = series_day.get("touched") or 0
    sealed_n = len(lu)
    fail_derived = round((touched - sealed_n) / touched, 4) if touched else None
    zg = RR._legacy_zt_groups(os.path.join(old_root, "data", "generated", "pattern",
                                           "zt_matrix"), D)
    fail_rate = (round(zg[1] / (zg[0] + zg[1]), 4)
                 if zg and (zg[0] + zg[1]) else fail_derived)
    pay["env_metrics"] = {
        "limit_up_count": (int(float(lab["zt"])) if lab and lab.get("zt")
                           else sealed_n),
        "max_board": max_board,
        "turnover": None,
        "up_down_ratio": (float(hist["ratio_ud"]) if hist and hist.get("ratio_ud") else None),
        "fail_rate": fail_rate,
        "index_ma20": (float(lab["idx_sh"]) if lab and lab.get("idx_sh") else None)}
    pay["_env_src"] = {"limit_up_count": "env_labels" if lab and lab.get("zt") else "derived",
                       "fail_rate": "zt_matrix" if zg and (zg[0] + zg[1]) else
                                    ("klines_derived" if fail_derived is not None else None),
                       "has_label": bool(lab)}
    meta = {k: pay.pop(k) for k in list(pay) if k.startswith("_")}
    return pay, meta


# ── 沙盒夹具（_selftest_ 前缀约定；写目标全部 = 沙盒路径） ────────────────────
# 注意：必须是**同形函数副本**而非 `= RR._selftest_x` 赋值别名——landing._external_names
# 按「全库被调名 - 本模块定义名」判跨模块生产入口，赋值别名会让这些夹具名在全库失去
# 测试豁免（2026-10-10 全库 audit 实测 4 处 UNDECLARED_WRITE 误伤 chain_c34/raw_to_settle/
# real_replay 的同名夹具）。副本定义后名字在本模块有定义 → 不再进 ext。
def _selftest_init_pool(path, date):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": CAND.SCHEMA_VERSION, "updated_at": date,
                   "candidates": []}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def _selftest_init_cursor(path, date, done=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": CUR.SCHEMA_VERSION, "date": date,
                   "checklist": {s: bool(done) for s in CUR.STEPS}},
                  f, ensure_ascii=False)
    return path


def _selftest_copy_kb(base):
    kb_dst = os.path.join(base, "data", "knowledge", "s1_event_effects")
    os.makedirs(kb_dst, exist_ok=True)
    for _fn in ("effect_base.json", "taxonomy.json"):
        shutil.copy2(os.path.join(S1MOD.KB_DIR, _fn), os.path.join(kb_dst, _fn))
    shutil.copy2(os.path.join(S1MOD.KB_DIR, "..", "_registry.json"),
                 os.path.join(base, "data", "knowledge", "_registry.json"))


def _selftest_fp(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


def _selftest_tree_fp(roots):
    out = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                p = os.path.join(dirpath, fn)
                out.add((os.path.relpath(p, root), _selftest_fp(p)))
    return out


# harness 自报（与 real_replay 同理：全部写盘 = tempfile 沙盒，零生产落点）
_TEST_FUNCS = ("run_batch", "_second_pass", "_discipline", "_main")


# ── 机械判定与统计 ───────────────────────────────────────────────────────────
def _window_span(dates, d, w):
    """决策日 d 的（成交日, 窗口末日）索引；窗口未走完 → 末日=None。"""
    i = dates.index(d)
    if i + 1 >= len(dates):
        return None, None
    exec_i = i + 1
    end_i = i + w if i + w < len(dates) else None
    return exec_i, end_i


def _judgement_for(dec_id, dates, kb, dt_rows_by_day, as_of, base):
    """按 as_of 可见数据机械重算 M1/M2/M3 判定（零 LLM；窗口未完 → MIXED）。"""
    v = DEC.load_decision(dec_id, sim=True, base=base)
    d = v["date"]
    w = int(v["window"])
    exec_i, end_i = _window_span(dates, d, w)
    code = v["code"]
    stop = float(v["stop_line"])
    if d not in kb or (exec_i is not None and dates[exec_i] not in kb):
        return None
    rows = dt_rows_by_day.get(d) or []
    end_d = dates[end_i] if end_i is not None else None
    complete = end_i is not None and end_d <= as_of and end_d in kb
    peer = [x for x in rows if x["code"] != code and x["code"] in kb.get(d, {})
            and (end_d is not None and end_d in kb)]

    def _m1():
        if not complete or code not in kb.get(end_d, {}):
            return "MIXED", "窗口未完或末日缺行情（截至 %s）" % as_of
        pct = round((kb[end_d][code]["close"] / kb[d][code]["close"] - 1) * 100, 2)
        return ("SUPPORT" if pct >= 0 else "REFUTE"), "%s%%" % pct

    def _m2():
        if not peer:
            return "MIXED", "同榜股行情缺"
        if not complete:
            return "MIXED", "窗口未完（截至 %s）" % as_of
        rets = []
        for x in peer:
            if x["code"] not in kb.get(end_d, {}) or x["code"] not in kb.get(d, {}):
                continue                    # 同榜股窗口末日停牌/缺行 → 剔除不计（如实）
            rets.append(round((kb[end_d][x["code"]]["close"]
                               / kb[d][x["code"]]["close"] - 1) * 100, 2))
        if not rets:
            return "MIXED", "同榜股窗口行情缺"
        avg = round(sum(rets) / len(rets), 2)
        return ("SUPPORT" if avg >= 0 else "REFUTE"), "%s%%(n=%d)" % (avg, len(rets))

    def _m3():
        if not complete:
            return "MIXED", "窗口未完（截至 %s）" % as_of
        lows = [kb[dates[i]][code]["low"] for i in range(exec_i, end_i + 1)
                if dates[i] in kb and code in kb[dates[i]]]
        if not lows:
            return "MIXED", "窗口行情缺"
        lo = min(lows)
        return ("SUPPORT" if lo > stop else "REFUTE"), str(lo)

    m1, o1 = _m1()
    m2, o2 = _m2()
    m3, o3 = _m3()
    return {"verdicts": {
        "M1": {"metric": "窗口收盘涨跌幅", "data_source": "snapshot", "verdict": m1,
               "observed": o1, "note": "机械代入真实行情（截至 %s）" % as_of},
        "M2": {"metric": "同榜资金股窗口平均涨跌幅", "data_source": "snapshot",
               "verdict": m2, "observed": o2, "note": None},
        "M3": {"metric": "窗口最低价不破止损", "data_source": "snapshot", "verdict": m3,
               "observed": o3, "note": "止损=%s" % stop}},
        "attribution_L1L5": {k: "OK" for k in PNL.L1L5_KEYS},
        "attribution_三问": {"驱动": "OK", "赔率": "OK", "证伪": "NA"},
        "_harness_note": "归因为机械占位（非 LLM 判断）；判定按结算时点可见数据重算"}


def _open_positions(sim=True, base=None):
    """当前在场的 {code: weight}（v1.2.1：经 PG.portfolio_exposure executed 视图——
    EXECUTION>0 且 pnl 未落；与 position_gate.derive_holdings 同源，消除双真源）。"""
    return {c: e["executed"]
            for c, e in PG.portfolio_exposure(sim, base=base).items()
            if e["executed"] > 0}


def _drawdown(sim=True, base=None, equity0=EQUITY0):
    """已实现净值曲线的最大回撤（不含浮动盈亏——披露口径）。"""
    nc = PNL.net_curve(sim, initial_capital=equity0, base=base)
    peak = equity0
    dd = 0.0
    for p in nc["points"]:
        eq = p["equity"]
        peak = max(peak, eq)
        if peak > 0:
            dd = max(dd, (peak - eq) / peak)
    return dd


def _realized(sim=True, base=None):
    return round(sum(r["pnl_amount"] for r in PNL.iter_pnl(sim, base=base)), 2)


# ── 单窗回放主流程 ───────────────────────────────────────────────────────────
def run_batch(window="w1", start=None, end=None, old_root=_OLD_ROOT, sandbox=None,
              second_pass=True, verbose=True, calendar=None):
    """`calendar` = `dayclass.Calendar` | None（B3 日期闸）。

    给了日历 ⇒ scan 走**严格日状态闸**（非 `TRADING_WITH_DATA` 一律不扫）。
    未给 ⇒ scan 显式 `on_unknown="allow"`：本 harness 是**无日历的历史回放沙盒**，
    该例外是**显式且可 grep** 的（见 `SCAN.scan(..., on_unknown=...)` 调用点），
    其安全前提是 loader 的 `is_exact` 硬闸仍然生效（数据日 ≠ 目标日 ⇒ 零行可消费）。
    """
    rep = {"window": window, "equity0": EQUITY0,
           "assumptions": {
               "A1_信息截止": "决策=盘后；当日公告/LHB/环境信号当晚可得（假设，采集痕迹佐证不足处见报告）",
               "A2_成本": "v1.2 起费用正式入账（pnl 行 fee_amount/net_amount）：费率读正式参数 "
                          "params.json cost_buy_rate=%.3f%%/cost_sell_rate=%.3f%%（相对各自成交金额；"
                          "模型假设 BASELINE/UNCALIBRATED，非真实费单；不含额外滑点假设）"
                          % (PARAMS.get_value("cost_buy_rate") * 100,
                             PARAMS.get_value("cost_sell_rate") * 100),
               "A3_成交": "口径 v1.1：次日开盘成交，限价=收盘×1.02，一字板/涨停开盘不成交，T+1",
               "A4_LLM代位": "C3 链内容=固定机械模板；归因=机械占位；验证的是机制链非 LLM 判断"},
           "days": {}, "idempotency": None}
    tmp = sandbox or tempfile.mkdtemp(prefix="batch_replay_")
    cleanup = sandbox is None
    try:
        base = os.path.join(tmp, "gen")
        if not os.path.isdir(base):
            os.makedirs(base)
        w0, w1 = start or WINDOWS[window][0], end or WINDOWS[window][1]
        series, all_dates = _klines_series(old_root, "2026-03-01")
        dates = [d for d in all_dates if w0 <= d <= w1]
        need_kb = set()
        for d in dates:
            i = all_dates.index(d)
            for j in range(max(0, i - 1), min(len(all_dates), i + 4)):
                need_kb.add(all_dates[j])
        kb = _load_kb_days(old_root, need_kb)
        flow = _fundflow_by_date(old_root)
        ztp, ztp_bad = _zt_pool_hist(old_root)
        if ztp_bad:
            rep["zt_pool_bad_fields"] = ztp_bad   # 首封时间/封单额脏值逐日计数（如实报告）
        rep["range"] = [dates[0], dates[-1]]
        rep["n_days"] = len(dates)

        snap_root = LD.roots(base)["snapshot"]
        cursor_f = os.path.join(tmp, "cursor.json")
        pool_f = os.path.join(tmp, "candidates.json")
        chains_f = os.path.join(tmp, "chains.jsonl")
        cases_f = os.path.join(tmp, "cases.jsonl")
        dq_f = os.path.join(tmp, "demand_queue.jsonl")
        thesis_f = os.path.join(tmp, "thesis.md")
        ml_f = os.path.join(tmp, "mainline.json")
        TH.init_file(thesis_f)
        prev_i = all_dates.index(dates[0])
        _selftest_init_cursor(cursor_f, all_dates[prev_i - 1] if prev_i else dates[0], True)
        _selftest_init_pool(pool_f, dates[0])
        _selftest_copy_kb(base)

        dec_stats = {"BUY": 0, "WAIT": 0, "NO": 0, "c5_pass": 0, "c5_reject": 0}
        scan_fail = 0        # 五路扫描抛异常的天数（>=3 即中止）
        scan_none = 0        # 五路扫描被日状态闸合法拒绝的天数
        driver_cnt = {"A": 0, "B": 0, "C": 0, "复合": 0, "驱动不明": 0}
        day_recs = rep["days"]
        dt_cache = {}
        for D in dates:
            rec = {"date": D}
            day_recs[D] = rec
            pay, pmeta = _payload_for(old_root, D, kb[D], series[D], flow, ztp, None)
            dt_cache[D] = pay["dragon_tiger"]
            rec["dt_source"] = pmeta["_dt_source"]
            rec["news_rows"] = len(pay["news"])
            rec["news_skipped_non_a"] = pmeta["_news_skipped_non_a"]
            rec["fail_rate_src"] = pmeta["_env_src"]["fail_rate"]
            try:
                ING.stage_core(D, pay, source="manual:旧包真实回放 批量 %s" % window,
                               base=base, data_upto=D)
            except ING.IngestError as e:
                if "STAGED_EXISTS" not in str(e):
                    rec["stage_error"] = str(e)
                    print("  [stage-error] %s: %s" % (D, str(e)[:160]))
                    continue
                rec["stage"] = "EXISTS"
            pr = ING.promote_snapshot(D, base=base)
            rec["promote"] = pr

            # C1 环境档（真实信号；缺 → RegimeError → C1 UNAVAILABLE，当日不出决策）
            try:
                env_res = RG.evaluate(pay["env_metrics"], as_of=D, source="snapshot")
                env = {"regime": env_res["regime"], "signals": env_res["signals"],
                       "leg": "EOD", "source": "LOADER"}
                rec["regime"] = env_res["regime"]
                rec["position_cap"] = env_res["position_cap"]
            except RG.RegimeError as e:
                rec["regime"] = "UNAVAILABLE"
                rec["regime_error"] = str(e)
                env = None

            # C2 扫描：SCAN_WINDOWS = 真实五路 scan；其余 = B 路机械直选（同池机制，规则在案）
            scan_rep = None
            if window in SCAN_WINDOWS:
                try:
                    scan_rep = SCAN.scan(D, base=base, pool_path=pool_f, mainline_path=ml_f,
                                         calendar=calendar,
                                         on_unknown=("block" if calendar else "allow"))
                except Exception as e:  # noqa: BLE001
                    # ★★ 2026-10-23：原先这里只 `rec["scan_error"]=str(e)` 就放过，
                    #   而 rec 只在内存里（不落账本）、全流程**无人检查 scan_error**
                    #   ⇒ 五路扫描连崩 54 天、w3 白跑 462 条决策，直到我从沙盒
                    #   candidates.json 反查才发现。这正是 M42「伪装成正常空结果」
                    #   的另一面：**把异常记成一个没人读的字段**，等于静默吞掉。
                    #   现在：记错误 + 立即打日志 + 首日就失败即中止（禁带病跑整窗）。
                    rec["scan_error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
                    print("  [scan-error] %s: %s" % (D, rec["scan_error"]))
                    scan_fail += 1
                    if scan_fail >= 3:
                        raise RuntimeError(
                            "五路扫描连续失败 %d 天（首错 %s）——中止回测，"
                            "否则整窗决策会退化成单路 B 且外观正常（M42）"
                            % (scan_fail, rec["scan_error"])) from e
                if scan_rep is None and not rec.get("scan_error"):
                    # scan 合法返回 None = 日状态闸拒绝（非交易日/无数据），这不是错误，
                    # 但要计数并可查，否则「真跑过但没命中」与「根本没跑」无法区分。
                    scan_none += 1
                rec["scan"] = ({"created": scan_rep["created"],
                                "routes": {k: v.get("hit") for k, v in
                                           (scan_rep["routes"] or {}).items()}}
                               if scan_rep else None)
            dt_rows = pay["dragon_tiger"]
            selected = []
            for x in sorted([r for r in dt_rows if (r.get("net") or 0) > 0],
                            key=lambda r: -r["net"])[:TOP_B]:
                selected.append((x["code"], "B", x))
            weak_b = [x for x in sorted([r for r in dt_rows if (r.get("net") or 0) > 0],
                                        key=lambda r: -r["net"])[TOP_B:TOP_B + 2]]
            # A 路：当日公告 → 可映射分类 → S1 评估（真知识库）
            a_events = []
            col_of = {}
            np = os.path.join(old_root, "data", "raw", "bus", "notices",
                              "notice_%s.csv" % D)
            if os.path.isfile(np):
                for r in csv.DictReader(io.open(np, encoding="utf-8-sig")):
                    col_of[r["art_code"]] = r["column"].strip()
            for n in pay["news"]:
                for c in n["related_codes"]:
                    col = col_of.get(n["news_id"], "")
                    cat = RR._TAX["cat_map"].get(col)
                    if not cat or cat[0] == "_NEUTRAL":
                        continue
                    a_events.append((c, n, col))
            rec["a_events"] = len(a_events)
            for c, n, col in a_events[:3]:
                selected.append((c, "A", (n, col)))

            # C 路：涨停池情绪/连板（2026-10-23 补 · 此前**结构性缺失**）
            #
            # ★ 缺口实证：scan 一直真跑（w3 沙盒里 3352 条 limit_up 候选、 proclaimed
            #   `source=limit_up` 入池齐全），但**选样从不读候选池** —— `selected`
            #   只由「龙虎榜 top3（B）」+「公告事件 top3（A）」两处拼成，`scan_rep`
            #   仅被记进 rec 做统计。后果：C 类（情绪/连板驱动）决策恒为 0，
            #   策略矩阵 15 格里 14 格「从未测过」，用户令「所有策略都要研究」
            #   在 C 类上根本无法开始。
            #
            # 判据（机械，全用当日已冻结快照，禁未来）：
            #   · 只取当日 `limit_up` 来源的候选（scan 已过代码类型闸与日状态闸）；
            #   · **排除**已上龙虎榜净买>0 的票 —— 那些已由 B 路取走，
            #     重复选会在同日去重里被跳掉，白占名额；
            #   · 按连板数降序取前 TOP_C（连板越高情绪越强，与 C 类定义一致）。
            c_picks = []
            if scan_rep:
                dt_net = {r.get("code"): (r.get("net") or 0) for r in dt_rows}
                for cd_ in (scan_rep.get("codes") or []):
                    if dt_net.get(cd_, 0) > 0:
                        continue          # 已被 B 路覆盖（避免同日去重白占名额）
                    c_picks.append(cd_)
            # 连板数从当日涨停池取（pay["limit_up"] 行，boards 字段）
            boards_of = {}
            for r in (pay.get("limit_up") or []):
                try:
                    boards_of[r.get("code")] = int(r.get("boards") or 1)
                except (TypeError, ValueError):
                    boards_of[r.get("code")] = 1
            c_picks.sort(key=lambda x: -boards_of.get(x, 1))
            rec["c_picks"] = len(c_picks)
            for cd_ in c_picks[:TOP_C]:
                selected.append((cd_, "C", {"boards": boards_of.get(cd_, 1)}))

            rec["decisions"] = []
            open_pos = _open_positions(True, base)
            decided_today = set()   # v1.2 审计修复（2026-10-10 P0）：同日同码只评审一次
            for code, drv, meta in selected:
                if code in decided_today:
                    # 同票多事件/多路径去重：重复评审会产生同日双开仓，C5 每笔独立
                    # 评估时 holdings 不含同日另一笔（实测穿透单票权益顶 39%：
                    # sz000823 双开 40% / sh688766 三开 60%）——组合层缺口由本闸兜底。
                    rec["decisions"].append({"code": code, "driver": drv,
                                             "skip": "同日已评审（同票去重）"})
                    continue
                decided_today.add(code)
                if code not in kb.get(D, {}):
                    rec["decisions"].append({"code": code, "driver": drv,
                                             "skip": "当日无行情行"})
                    continue
                # ★★ 2026-10-23 加「次日可成交性」预检（回测侧护栏，不动 settle 骨架）：
                #   实测踩到：sz000004 在 2026-07-13 有行情、07-14 起**停牌无行**，
                #   但 07-13 仍产了 BUY ⇒ `settle._step_fill` 在成交日（07-14）
                #   找不到行情行 ⇒ `pending NO_QUOTE` ⇒ 该 reason **不在
                #   `CARRY_REASONS`** ⇒ 被判阻塞型 ⇒ 步2 不打勾 ⇒ 游标不推进 ⇒
                #   `SettleError: 上一日五步未走完` ⇒ **整个回测崩在 07-15**
                #   （w3 第二次白跑 190 条）。
                #
                #   为什么在回测侧修而不改 `settle`：`settle` 是**骨架冻结件**，
                #   且 D4/D6 自检明确锁定「无行情 ⇒ 阻塞、缺就是缺不猜」这一设计
                #   （那是对的：对**生产**而言，成交日缺行可能是数据没采到，
                #   贸然判「未成交」会掩盖采集事故）。但回测里「次日无行」在
                #   历史数据上**只可能意味着停牌**，是可判定事实，不是未知。
                #   ⇒ 回测不该产出一笔注定无法结算的决策：在决策时就把
                #   「次日无行情（停牌）」的票排除，记为 skip 并留痕可查。
                _nxt_i = all_dates.index(D) + 1 if D in all_dates else -1
                _nxt = all_dates[_nxt_i] if 0 <= _nxt_i < len(all_dates) else None
                if _nxt and code not in kb.get(_nxt, {}):
                    rec["decisions"].append({"code": code, "driver": drv,
                                             "skip": "次日无行情（停牌）→ 无法成交，不产判断"})
                    rec.setdefault("skip_suspended", []).append(
                        {"code": code, "next": _nxt})
                    continue
                if code in open_pos:
                    rec["decisions"].append({"code": code, "driver": drv,
                                             "skip": "已持仓不加仓"})
                    continue
                try:
                    # 2026-10-23：C 路补入 —— 原先只有 B/A 两分支，C 会误落
                    # news_hot（那是 A 路的 scanner 名），归因会串道。
                    if drv == "B":
                        _scan_name = "dragon_tiger"
                        _txt = "C2 命中 dragon_tiger: 龙虎榜:net=%s" % meta.get("net")
                    elif drv == "C":
                        _scan_name = "limit_up"
                        _txt = "C2 命中 limit_up: 涨停池:boards=%s" % meta["boards"]
                    else:
                        _scan_name = "news_hot"
                        _txt = "C2 命中 news_hot: 消息关联:news=%s" % meta[0]["news_id"]
                    CAND.upsert_candidate(code, D, _scan_name,
                                          {"text": _txt, "rule": None}, path=pool_f)
                except Exception as e:  # noqa: BLE001
                    rec["decisions"].append({"code": code, "driver": drv,
                                             "error": "入池失败:%s" % e})
                    continue
                q = kb[D][code]
                close_d = float(q["close"])
                odds = dict(entry_limit=round(close_d * LIMIT_TOL, 2),
                            stop_line=round(close_d * 0.95, 2),
                            target=round(close_d * 1.10, 2), window=2,
                            position_request=REQ)
                s1_snap_p = os.path.join(LD.roots(base)["generated"], "s_s1_%s.json" % D)
                if drv == "B":
                    th = TH.create_thesis(D, "B", "资金抢占：主力净买入（%s %s）"
                                          % (code, D), code=code, candidate_id=None,
                                          gap=TH.GAP_DRIVER, path=thesis_f)
                    payload = {"driver": "B", "thesis_id": th["id"],
                               "evidence": [{"text": "资金净买入 net=%.0f（快照 dragon_tiger 行，"
                                                     "来源=%s）" % (meta["net"],
                                                                  pmeta["_dt_source"]),
                                             "source": "SNAPSHOT:dragon_tiger",
                                             "ref": code}],
                               "inference": ["资金抢占 → 溢价兑现"],
                               "prediction": _preds(close_d), "five": _five()}
                    kw = {}
                elif drv == "C":
                    # C 路（情绪/连板）：证据只取**当日已冻结的 env_metrics**
                    # （`drive._driver_effective` 对 C 的硬要求是 `SNAPSHOT:env_metrics`），
                    # 不掺涨停池个股面以外的东西，避免把 B/A 的证据蹭进来。
                    em = pay.get("env_metrics") or {}
                    th = TH.create_thesis(D, "C", "情绪扩散：连板 %s 板（%s %s）"
                                          % (meta["boards"], code, D), code=code,
                                          candidate_id=None, gap=TH.GAP_DRIVER,
                                          path=thesis_f)
                    payload = {"driver": "C", "thesis_id": th["id"],
                               "evidence": [{"text": "连板 %s 板 + 炸板率 %s（快照 env_metrics，"
                                                     "来源=%s）"
                                                     % (meta["boards"],
                                                        em.get("fail_rate"),
                                                        pmeta["_env_src"]["fail_rate"]),
                                             "source": "SNAPSHOT:env_metrics",
                                             "ref": "fail_rate"}],
                               "inference": ["情绪扩散 → 连板接力溢价"],
                               "prediction": _preds(close_d), "five": _five()}
                    kw = {}
                else:
                    n, col = meta
                    try:
                        ev = {"event_id_src": n["news_id"], "title": n["title"],
                              "cols": col,
                              "sources": [{"source_id": "巨潮资讯（旧包机采归档）",
                                           "kind": "公告号", "ref": n["news_id"],
                                           "ts": n["ts"]}],
                              "first_seen_at": D, "announcement_at": D,
                              "related_codes": [code]}
                        sr = S1MOD.evaluate(ev, as_of=D, base=base)
                    except S1MOD.S1Error as e:
                        # 知识库版本晚于决策日（as_of=2026-09-21）→ S1 机制正确拒绝
                        # → 该日 A 路证据缺失，如实记录（不造证据、不提前用未来知识库）
                        rec.setdefault("s1_unavailable", []).append(
                            {"code": code, "col": col, "reason": str(e)[:120]})
                        continue
                    o = sr["output"]
                    rec.setdefault("s1", []).append(
                        {"code": code, "type": o["type"], "strength": o["strength"],
                         "event_id": sr["event_id"]})
                    th = TH.create_thesis(D, "A", "事件催化：%s（%s %s）"
                                          % (o["type"], code, D), code=code,
                                          candidate_id=None, gap=TH.GAP_DRIVER,
                                          path=thesis_f)
                    payload = {"driver": "A", "thesis_id": th["id"],
                               "evidence": [{"text": "%s 事件 S1 评估（%s，强度 %s）"
                                             % (o["type"], col, o["strength"]),
                                             "source": "S1:EVENT_EVAL",
                                             "ref": sr["event_id"]}],
                               "inference": ["事件催化 → 历史净效应可追溯"],
                               "prediction": _preds(close_d), "five": _five()}
                    kw = {"s1_path": s1_snap_p}
                try:
                    rv = drive.review(payload, as_of=D, code=code,
                                      snapshot_root=snap_root, pool_path=pool_f,
                                      path=chains_f, **kw)
                except Exception as e:  # noqa: BLE001
                    rec["decisions"].append({"code": code, "driver": drv,
                                             "error": "C3失败:%s" % e})
                    continue
                d1b = drive.load_chain(code=code, as_of=D, path=chains_f)
                if env is None:
                    rec["decisions"].append({"code": code, "driver": drv,
                                             "skip": "C1_UNAVAILABLE"})
                    continue
                decide_kw = dict(as_of=D, code=code, thesis_id=payload["thesis_id"],
                                 chain=d1b["chain"], sim=True,
                                 driver=rv["driver_effective"], env=env, base=base,
                                 pool_path=pool_f, thesis_path=thesis_f,
                                 snapshot_root=snap_root, **odds)
                if drv == "B" and meta in weak_b:
                    decide_kw["completion_event"] = {
                        "text": "次日仍在主力净买入榜（资金证据连续性确认）", "rule": None}
                r_buy = ORCH.decide(**decide_kw)
                act = r_buy["action"]
                dec_stats[act] = dec_stats.get(act, 0) + 1
                # 生效驱动分布（C 路此前恒 0，是因为选样根本没产 C —— 必须可见）
                _de = rv.get("driver_effective")
                driver_cnt[_de] = driver_cnt.get(_de, 0) + 1
                entry = {"code": code, "driver": drv, "action": act,
                         "decision_id": r_buy["decision_id"],
                         "status": r_buy["status"]}
                if act == "BUY":
                    # v1.2.1（C5 组合修复批）：省略 holdings → check 内部经
                    # portfolio_exposure 逐笔现取（executed+pending）——同批前序
                    # RISK_PLAN 落账后，后一笔检查立即可见（消除批内旧快照穿透）。
                    pg = PG.check(REQ, code=code, as_of=D, grade=env["regime"],
                                  drawdown=_drawdown(True, base),
                                  ledger_sim=True, base=base)
                    entry["c5"] = pg["verdict"]
                    entry["c5_plan"] = pg["position_plan"]
                    dec_stats["c5_pass" if pg["verdict"] == "PASS" else "c5_reject"] += 1
                    if pg["verdict"] == "PASS":
                        DEC.append_risk_plan(r_buy["decision_id"], pg["position_plan"],
                                             as_of=D, gates=pg["gates"],
                                             note="批量真实回放 %s" % window, base=base)
                    else:
                        DEC.append_risk_plan(r_buy["decision_id"], pg["position_plan"],
                                             as_of=D, gates=pg["gates"],
                                             note="批量真实回放 %s（C5 否决）" % window,
                                             base=base)
                rec["decisions"].append(entry)

            # 结算（判定按当日可见数据机械重算注入；落库值 = 窗口完成日全窗判定）
            #
            # ★★ 2026-10-23 修时序 bug：`iter_decisions(True, base=base)` **不带 as_of**，
            #   把整窗（含**未来日**）的 BUY 全塞给 settle ⇒ settle 试图为未来决策
            #   找成交记录，找不到 → 「五步未走完（待办：成交记录）」→ 游标卡死 →
            #   `CursorError` 让整个回测在 2026-07-15 崩掉（w3 白跑 190 条决策）。
            #   这同时是**未来函数**：结算用到了决策日之后才存在的决策行。
            #   正确口径：结算只认 `date <= D` 的决策（当日及以前），未来决策当日不可见。
            judgements = {}
            for dec in DEC.iter_decisions(True, base=base):
                if dec.get("action") != "BUY":
                    continue
                if (dec.get("date") or "") > D:
                    continue          # 禁未来：结算不得引用决策日之后的决策
                j = _judgement_for(dec["id"], all_dates, kb, dt_cache, D, base)
                if j:
                    judgements[dec["id"]] = j
            srep = SETTLE.settle(D, judgements=judgements,
                                 equity=EQUITY0 + _realized(True, base),
                                 base=base, snapshot_root=snap_root,
                                 raw_dir=os.path.join(LD.roots(base)["raw"], D),
                                 cursor_path=cursor_f, learning_path=dq_f,
                                 calendar=calendar)
            rec["settled"] = srep["settled"]
            rec["pending_steps"] = srep["pending_steps"]
            rec["pnl_written"] = srep["steps"][2].get("written", 0)
            # 当日新写 pnl 行的盈亏合计与胜负计数（8.4 分环境拆分用；重跑日 written=0 不重复计）
            _s = (srep["steps"][2].get("settlements") or [])
            rec["pnl_day_sum"] = round(sum(float(x.get("pnl_amount") or 0) for x in _s), 2)
            rec["pnl_day_win"] = sum(1 for x in _s if x.get("outcome") == "WIN")
            rec["pnl_day_loss"] = sum(1 for x in _s if x.get("outcome") == "LOSS")
            rec["pnl_day_filled"] = sum(1 for x in _s if x.get("outcome") not in
                                        ("NOT_FILLED", None))
            cb = CB.build(D, sim=True, base=base, path=cases_f)
            rec["cases_added"] = cb["added"]

        rep["decision_stats"] = dec_stats
        # 2026-10-23：驱动分布 + 扫描健康度必须进报告。
        # 起因：w3 五路扫描连崩，但报告只统计 action、不看 driver，
        # 结果「156 条 BUY 全是 B」这种结构性退化在报告上完全看不出来。
        rep["driver_effective"] = dict(driver_cnt)
        rep["scan_health"] = {"fail_days": scan_fail, "gated_days": scan_none,
                              "scan_window": window in SCAN_WINDOWS}
        rep["pnl_summary"] = PNL.summary(True, base=base)
        nc = PNL.net_curve(True, initial_capital=EQUITY0, base=base)
        rep["net_curve"] = {"total_pnl": nc["total_pnl"], "n": nc["n"],
                            "points": nc["points"]}
        rep["open_at_end"] = _open_positions(True, base)
        rep["unresolved_pnl"] = [
            {"decision_id": dec["id"], "date": dec["date"], "code": dec["code"],
             "reason": "窗口未完/数据缺（如实呈现）"}
            for dec in DEC.iter_decisions(True, base=base)
            if dec.get("action") == "BUY"
            and (DEC.load_decision(dec["id"], sim=True, base=base).get("actual_position")
                 or 0) > 0
            and PNL.read_pnl(dec["id"], True, base=base) is None]

        if second_pass:
            rep["idempotency"] = _second_pass(window, all_dates, old_root, kb, series,
                                              flow, ztp, tmp, base, cursor_f, pool_f,
                                              ml_f, thesis_f, chains_f, cases_f, dq_f,
                                              dt_cache, calendar=calendar)
        return rep
    finally:
        if cleanup:
            shutil.rmtree(tmp, ignore_errors=True)


def _second_pass(window, all_dates, old_root, kb, series, flow, ztp, tmp, base,
                 cursor_f, pool_f, ml_f, thesis_f, chains_f, cases_f, dq_f, dt_cache,
                 calendar=None):
    """重复执行 / 结算重跑：同沙盒整窗重放，全部层应幂等（账本零新增）。"""
    n_dec = len(DEC.read_ledger(True, base=base))
    n_pnl = len(PNL.iter_pnl(True, base=base))
    n_cases = _count_lines(cases_f)
    reused = {"stage_exists": 0, "promote_exists": 0, "scan_dup": 0, "settle_already": 0}
    for D in [d for d in all_dates if _in_win(window, d)]:
        pay, _meta = _payload_for(old_root, D, kb[D], series[D], flow, ztp, None)
        try:
            ING.stage_core(D, pay, source="manual:旧包真实回放 批量 %s" % window,
                           base=base, data_upto=D)
        except ING.IngestError as e:
            if "STAGED_EXISTS" in str(e):
                reused["stage_exists"] += 1
        if ING.promote_snapshot(D, base=base) == "EXISTS":
            reused["promote_exists"] += 1
        if window in SCAN_WINDOWS:
            try:
                sr = SCAN.scan(D, base=base, pool_path=pool_f, mainline_path=ml_f,
                               calendar=calendar,
                               on_unknown=("block" if calendar else "allow"))
                if sr and sr.get("duplicate"):
                    reused["scan_dup"] += sr["duplicate"]
            except SCAN.ScanError as e:
                # 幂等二跑重扫已处理日：主线台账「时间倒流，禁回写」拒绝 = 正确的
                # 防作弊行为，捕获并计数为证据（而非失败）。
                if "时间倒流" in str(e):
                    reused["time_travel_blocked"] = \
                        reused.get("time_travel_blocked", 0) + 1
                else:
                    raise
        judgements = {}
        for dec in DEC.iter_decisions(True, base=base):
            if dec.get("action") != "BUY":
                continue
            if (dec.get("date") or "") > D:
                continue          # 禁未来（同主循环 2026-10-23 修）
            j = _judgement_for(dec["id"], all_dates, kb, dt_cache, D, base)
            if j:
                judgements[dec["id"]] = j
        try:
            srep = SETTLE.settle(D, judgements=judgements,
                                 equity=EQUITY0 + _realized(True, base),
                                 base=base, snapshot_root=LD.roots(base)["snapshot"],
                                 raw_dir=os.path.join(LD.roots(base)["raw"], D),
                                 cursor_path=cursor_f, learning_path=dq_f,
                                 calendar=calendar)
        except SETTLE.SettleError as e:
            # 幂等二跑重放游标已越过的日子：「游标禁倒退」拒绝 = 正确的防回写行为，
            # 捕获并计数为证据（已结账日不允许重跑改历史；幂等证据由账本零新增给出）。
            if "游标禁倒退" in str(e):
                reused["settle_cursor_guarded"] = \
                    reused.get("settle_cursor_guarded", 0) + 1
                continue
            raise
        reused["settle_already"] += sum(1 for s in srep["steps"] if s["status"] == "ALREADY")
    return {"ledger_decisions_before": n_dec, "ledger_decisions_after":
            len(DEC.read_ledger(True, base=base)),
            "pnl_rows_before": n_pnl, "pnl_rows_after": len(PNL.iter_pnl(True, base=base)),
            "cases_before": n_cases, "cases_after": _count_lines(cases_f),
            "zero_new": (n_dec == len(DEC.read_ledger(True, base=base))
                         and n_pnl == len(PNL.iter_pnl(True, base=base))
                         and n_cases == _count_lines(cases_f)),
            **reused}


def _in_win(window, d):
    w0, w1 = WINDOWS[window]
    return w0 <= d <= w1


def _count_lines(p):
    if not os.path.isfile(p):
        return 0
    return sum(1 for _ in io.open(p, encoding="utf-8"))


def _load_kb_days(old_root, need):
    kb = {}
    with io.open(os.path.join(old_root, "data", "raw", "bus", "klines.csv"),
                 encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["date"] in need:
                kb.setdefault(r["date"], {})[r["code"]] = {
                    "name": r["name"], "open": float(r["open"]),
                    "high": float(r["high"]), "low": float(r["low"]),
                    "close": float(r["close"]), "pct": float(r["change_pct"]),
                    "vol": float(r["volume"]), "amount": float(r["amount"])}
    return kb


def _preds(close_d):
    return [
        {"prediction_id": "M1", "metric": "窗口收盘涨跌幅", "data_source": "snapshot",
         "baseline": "0", "direction": "UP", "threshold": "0",
         "condition": "驱动后资金认可", "window": 2},
        {"prediction_id": "M2", "metric": "同榜资金股窗口平均涨跌幅", "data_source": "snapshot",
         "baseline": "0", "direction": "UP", "threshold": "0",
         "condition": "同榜扩散", "window": 2},
        {"prediction_id": "M3", "metric": "窗口最低价不破止损", "data_source": "snapshot",
         "baseline": str(round(close_d * 0.95, 2)), "direction": "DOWN",
         "threshold": str(round(close_d * 0.95, 2)),
         "condition": "回撤不破 5% 止损", "window": 2}]


def _five():
    return {k: "%s（批量真实回放：机械模板，LLM 判断留待生产）" % k for k in TQ.FIVE_KEYS}


# ── 统计报告（8.4） ──────────────────────────────────────────────────────────
def _print_report(reps):
    print("=" * 76)
    print("真实历史批量回放报告（任务 8.2/8.4 · 口径 v1.1 · 沙盒零污染）")
    print("=" * 76)
    for rep in reps:
        ps = rep["pnl_summary"]
        print("\n■ 窗口 %s：%s → %s（%d 交易日）" % (rep["window"], rep["range"][0],
                                                    rep["range"][1], rep["n_days"]))
        print("  决策统计: %s" % json.dumps(rep["decision_stats"], ensure_ascii=False))
        # 2026-10-23 新增：驱动分布 + 扫描健康度（此前报告看不到这两项，
        # 导致「全部退化成 B 路」这种结构性缺陷静默存活整窗）
        if rep.get("driver_effective"):
            print("  生效驱动: %s" % json.dumps(rep["driver_effective"],
                                               ensure_ascii=False))
        sh = rep.get("scan_health") or {}
        if sh.get("scan_window"):
            print("  五路扫描: 异常 %d 天 · 日状态闸拒绝 %d 天"
                  % (sh.get("fail_days", 0), sh.get("gated_days", 0)))
        print("  结算行: n=%d 成交=%d 未成交=%d WIN=%d LOSS=%d 胜率=%s"
              % (ps["n"], ps["filled"], ps["not_filled"], ps["win"], ps["loss"],
                 ps["win_rate"]))
        print("  毛盈亏=%s 元 | 费用(正式入账)=%s 元 | 费后净盈亏=%s 元"
              % (round(ps["pnl_total"], 2), round(ps["fee_total"], 2),
                 round(ps["net_total"], 2)))
        nc = rep["net_curve"]
        peak, eq, mdd = EQUITY0, EQUITY0, 0.0
        for p in nc["points"]:
            eq = EQUITY0 + p["cum_pnl"]
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak * 100 if peak else 0)
        print("  净值(费后): 期末 %s 元（较期初 %+.2f%%）| 最大回撤(已实现费后口径) %.2f%%"
              % (round(EQUITY0 + nc["total_pnl"], 2),
                 (nc["total_pnl"] / EQUITY0 * 100), mdd))
        print("  期末未平仓: %s | 未解出 pnl: %d 笔"
              % (json.dumps(rep["open_at_end"], ensure_ascii=False),
                 len(rep["unresolved_pnl"])))
        regimes = {}
        for d, r in rep["days"].items():
            if r.get("regime"):
                regimes.setdefault(r["regime"], 0)
                regimes[r["regime"]] += 1
        print("  环境分布: %s" % json.dumps(regimes, ensure_ascii=False))
        ids = rep["idempotency"]
        if ids:
            print("  幂等重放: zero_new=%s（决策 %d→%d · pnl %d→%d · case %d→%d · "
                  "stage_exists=%d · promote_exists=%d）"
                  % (ids["zero_new"], ids["ledger_decisions_before"],
                     ids["ledger_decisions_after"], ids["pnl_rows_before"],
                     ids["pnl_rows_after"], ids["cases_before"], ids["cases_after"],
                     ids["stage_exists"], ids["promote_exists"]))
        skipped = [(d, r) for d, r in sorted(rep["days"].items())
                   if r.get("regime") == "UNAVAILABLE" or r.get("stage_error")
                   or r.get("scan_error")]
        if skipped:
            print("  跳过/异常日（不静默）: %s"
                  % json.dumps({d: {k: v for k, v in r.items()
                                    if k in ("regime", "stage_error", "scan_error")}
                                for d, r in skipped}, ensure_ascii=False))


def _discipline(verbose=True):
    import ast
    import astwin as ASTW
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    lines = src.splitlines(True)
    drop = set()
    for lo, hi, name in ASTW.func_table(ast.parse(src)):
        if name.startswith(("_selftest", "_discipline", "_main")):
            drop.update(range(lo - 1, hi))
    prod = "".join(l for i, l in enumerate(lines) if i not in drop)
    res = [("① 生产区零写盘原语（无 os.replace/mkstemp/.write(/json.dump(/写模式）",
            "os.replace" not in prod and "mkstemp" not in prod
            and ".write(" not in prod and "json.dump(" not in prod
            and '", "w"' not in prod and '", "a"' not in prod and "'w'" not in prod),
           ("② 真实数据声明在案（manual 第二类 · 零补造 · 不预设 BUY）",
            "零补造" in src and "不预设" in src),
           ("③ 落盘仅经系统接口（stage/promote/settle/scan/upsert/thesis）",
            "ING.stage_core(" in prod and "promote_snapshot(" in prod
            and "SETTLE.settle(" in prod and "CAND.upsert_candidate(" in prod),
           # ④ 2026-10-23 修：`_second_pass` 曾引用未定义的 `calendar` ⇒ 10 个交易日
           #    全部跑完后才在幂等复跑阶段 NameError 崩（整窗白跑，产物随沙盒删掉）。
           #    判据：`_second_pass` 的定义与调用都必须出现 calendar（防再退化为闭包漏传）。
           ("④ _second_pass 透传 calendar（防 NameError 白跑整窗）",
            "_second_pass" in src
            and src.count("calendar=calendar") >= 1
            and "dt_cache, calendar=calendar" in src.replace("\n", " ")
            .replace("  ", " ")),
           # ⑤ 沙盒持久化前提：传 sandbox= ⇒ cleanup=False（产物不删，学习链才吃得到）
           ("⑤ sandbox 传入即不清理（回放产物可供学习消费）",
            "cleanup = sandbox is None" in src),
           # ⑥ 2026-10-23：C 路必须在选样里（此前 selected 只由 B/A 拼成，
           #   scan 真跑但结果从不用于选样 ⇒ C 类决策恒 0、策略矩阵 14/15 格空）
           ("⑥ C 路进入选样（`selected.append((cd_, \"C\"` 在案）",
            'selected.append((cd_, "C"' in src),
           # ⑦ 扫捕异常不得静默：必须打日志 + 计数 + 连续失败即中止
           #   （原先只 `rec["scan_error"]=...`，rec 不落盘且无人读 ⇒ w3 白跑 462 条）
           ("⑦ 扫描异常可中止（scan_fail 计数 + RuntimeError）",
            "scan_fail += 1" in src and "五路扫描连续失败" in src),
           # ⑧ 报告必须暴露驱动分布与扫描健康度（否则退化不可见）
           ("⑧ 报告含 driver_effective / scan_health",
            'rep["driver_effective"]' in src and 'rep["scan_health"]' in src),
           # ⑨ 2026-10-23：结算禁引用未来决策。
           #   原先 `iter_decisions(True, base=base)` 无 as_of ⇒ 整窗（含未来日）
           #   BUY 全塞给 settle ⇒ 找不到成交记录 ⇒ 游标卡「五步未走完」⇒ 回测崩。
           #   这既是**死锁**也是**未来函数**（结算用到了决策日之后的行）。
           ("⑨ 结算只看 date<=D 的决策（禁未来 + 防游标死锁）",
            src.count('> D:') >= 2 or '禁未来：结算不得引用决策日之后的决策' in src),
           # ⑩ 2026-10-23：次日停牌票必须在决策时不产判断。
           #   否则 settle 在成交日找不到行情 ⇒ NO_QUOTE（阻塞型，不在 CARRY_REASONS）
           #   ⇒ 游标不推进 ⇒ SettleError 崩掉整个回测（实测崩在 07-15，白跑 190 条）。
           ("⑩ 次日无行情（停牌）票不产判断（防结算死锁）",
            "次日无行情（停牌）" in src)]
    ok = True
    for name, passed in res:
        if verbose:
            print(("  [ok] " if passed else "  [!!] ") + name)
        ok = ok and passed
    print("纪律结果：%s（%d/%d）" % ("通过 ✅" if ok else "失败 ❌",
                                    sum(1 for _, p in res if p), len(res)))
    return ok


def _main(argv):
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    if "--selftest" in argv:
        # ★ 2026-10-20 修：--selftest 曾被误当跑批指令（v1.4 日历强制后必挂）。
        #   本件验收 = --discipline + 显式 --calendar 真跑（与 audit_replay 同款处置）。
        print("batch_replay 无单元自检（验收 = --discipline + 显式 --calendar 真跑）；纪律检查：")
        return 0 if _discipline() else 1
    wins = ["w1"] if "--window" in argv and "w1" in argv else (
        ([_w] if (_w := next((a for a in argv if a in WINDOWS), None)) else ["w1", "w2"]))
    if "--calendar" in argv:
        import dayclass as DC                    # B3 日历：只读消费，本 harness 不落盘
        cal = DC.Calendar.from_seed_csv(argv[argv.index("--calendar") + 1],
                                        source="--calendar")
    else:
        # ★ 2026-10-20 修：v1.4 起 settle.open_day 对 calendar=None 一律拒绝开日
        #   ⇒ 不带 --calendar 的跑批必然全挂 ⇒ 默认注入真库交易日历（只读）。
        import dayclass as DC
        cal = DC.Calendar.from_seed_csv(
            os.path.join(_ROOT, "data", "raw", "meta", "trade_cal.csv"),
            source="default_trade_cal")
    reps = []
    for w in wins:
        reps.append(run_batch(w, calendar=cal))
    _print_report(reps)
    if "--full" in argv:
        print("\n== FULL_JSON_BEGIN ==")
        print(json.dumps(reps, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
