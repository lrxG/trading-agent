#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eod_review.py · E3 盘后复盘报告生产者（任务9#3 · v1.0 · 2026-10-10 建）

用途：把**某一天的**决策 / 风控 / 成交 / 结算 / 缺口 / 判断比分 / 「什么变了」
汇成一份**可复现、确定性生成**的复盘记录（数据全部来自账本与参数变更史，
零 LLM、零手工编辑），落 `data/generated/reports/e3_daily.jsonl`（append-only，
一行 = 一个 (axis, as_of) 的复盘；v3.18 E6 先例：按日命名文件改单一
append-only 文件 + 日期字段，registry E3 output_path 的 e3_<as_of>.md 由此
收口为 e3_daily.jsonl——md 渲染内嵌在行的 `report_md` 字段）。

★ 一、报告必须回答「什么变了」（用户 2026-10-10 令），不是文字汇总：
  · 参数变化 = params_changelog 当日（及自上一复盘日以来）全部变更行
    （param old→new · approver · reason）——参数改动必有痕；
  · 判断面变化 = 当日 M1/M2/M3 判定分布 vs 上一复盘日累计分布（判定本身是
    与下注冻结预测比的，出现 REFUTE/WRONG 即「判断发生了什么」的落点）；
  · 数字变化 = 累计毛/费/净、胜率、已实现回撤、在途/未解出笔数 的逐日增量；
  · 环境档位变化 = 决策行 env 字段逐日对比（无决策日如实 UNKNOWN）。
  · 教训落点 = 当日 REFUTE / 三问 WRONG / LOSS 笔的 L1-L5 归因汇总 →
    **只列「待人工复核」清单，零自动改参**（E3 boundary：no_decision）。

★ 二、可复现性（机械判据）：
  · 全部输入 = 决策账本 + pnl 账本 + params_changelog + 游标 —— 无外部时变源；
  · 行幂等：同 (axis, as_of) 重跑，内容 hash（剔除 generated_at）一致 → EXISTS
    跳过；不一致 → 拒（**append-only 不改写**，复盘记录禁篡改）；
  · md 渲染 = 纯函数，同 review 逐字节同输出。

★ 三、边界：零交易动作、零参数写入、零自动改判；缺数据段如实 UNKNOWN/缺口
  显式化（在途未成交 / 已成交未解出 / 环境无源），不造数。

被谁调用：结算步内的定时触发（接线挂账下一批；本批交付生产者 + CLI 真跑）。
用法（**沙盒隔离同 E1/E2/E5/E9 先例：--base 一改，读写全落沙盒**）：
  python tools/e2e/eod_review.py --as-of 2026-09-29 --sim --base <沙盒根> [--stdout]
  python tools/e2e/eod_review.py --as-of 2026-09-29 --sim --stdout   # 生产（写入须过 writegate）
  python tools/e2e/eod_review.py --selftest
产物：`<base>/data/generated/reports/e3_daily.jsonl`（append-only 机器账本）
      + `<base>/data/generated/reports/e3_<as_of>.md`（人读报告，幂等覆写）
"""
import argparse
import datetime as _dt
import hashlib
import io
import json
import os
import re
import sys

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
_AN = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "analysis"))
sys.path.insert(0, _FS)
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
sys.path.insert(0, _AN)
import decision as DEC        # noqa: E402
import loader as LD           # noqa: E402
import pnl as PNL            # noqa: E402
import params as PARAMS       # noqa: E402
import cursor as CUR          # noqa: E402
from paths import ROOT        # noqa: E402

REPORT = os.path.join(ROOT, "data", "generated", "reports", "e3_daily.jsonl")
# 落点说明：X1 reports/ + cap 前缀 e3_ → 注册表 E3（_CAP_REF_RULES (e[0-9]+)_ 命中，
# want=E3 ∈ reg_ids）。单一 append-only 文件（v3.18 教训：按日命名 = <dyn> 不可登记）。


def _report_path(base=None):
    """E3 双落点（**全部跟随 base**；缺省 = 生产）：
      ① e3_daily.jsonl —— append-only 机器账本（一行 = 一个 (axis, as_of) 复盘）
      ② e3_<as_of>.md  —— 人读报告（同内容 md 渲染；幂等覆写，非 append）
    ★ base 语义同 E1/E2/E5/E9 先例：沙盒根存在 → 读写全落沙盒；生产根写入须过
      writegate（无票即拒），**零旁路**。
    """
    b = base or ROOT
    return os.path.join(b, "data", "generated", "reports", "e3_daily.jsonl")


def _md_path(base, as_of):
    b = base or ROOT
    return os.path.join(b, "data", "generated", "reports", "e3_%s.md" % as_of)


SCHEMA_VERSION = "1.0"


class E3Error(Exception):
    """复盘记录被拒（幂等冲突 / 非法日期 / 篡改型重写）——显式抛错。"""


def _chk_date(date, name="as_of"):
    s = str(date)
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise E3Error("%s 必须是 YYYY-MM-DD，收到: %r" % (name, date))
    return s


# ── 取数（全部账本直读，确定性）──────────────────────────────────────────────
def _day_decisions(as_of, sim, base):
    out = []
    for row in DEC.iter_decisions(sim, as_of=as_of, base=base):
        v = DEC.load_decision(row["id"], sim=sim, base=base)
        out.append({
            "id": v["id"], "date": v.get("date"), "code": v["code"], "action": v["action"],
            "driver": v.get("driver"), "sim": v.get("sim"),
            "position_request": v.get("position_request"),
            "entry_limit": v.get("entry_limit"), "stop_line": v.get("stop_line"),
            "target": v.get("target"), "window": v.get("window"),
            "position_plan": v.get("position_plan"),
            "risk_gates": v.get("risk_gates"),
            "actual_position": v.get("actual_position"),
            "execution_result": v.get("execution_result"),
            "avg_price": v.get("avg_price"),
            "env": (v.get("env") or {}).get("regime") if isinstance(v.get("env"), dict) else None,
            "thesis_id": v.get("thesis_id"),
        })
    return sorted(out, key=lambda x: x["id"])


def _map_settlement(r):
    """pnl 行 → 复盘用映射（判定值取字符串；毛/费/净三口径齐）。"""
    return {"decision_id": r["decision_id"], "outcome": r["outcome"],
            "exit_kind": r.get("exit_kind"), "exit_date": r.get("exit_date"),
            "pnl_amount": r["pnl_amount"], "fee_amount": r.get("fee_amount"),
            "net_amount": r.get("net_amount"), "pnl_pct": r["pnl_pct"],
            "hold_days": r.get("hold_days"),
            "M1": (r.get("M1") or {}).get("verdict"),
            "M2": (r.get("M2") or {}).get("verdict"),
            "M3": (r.get("M3") or {}).get("verdict"),
            "attribution_L1L5": r.get("attribution_L1L5"),
            "attribution_三问": r.get("attribution_三问")}


def _day_settlements(as_of, sim, base):
    return sorted((_map_settlement(r) for r in PNL.iter_pnl(sim, as_of=as_of, base=base)),
                  key=lambda x: x["decision_id"])


def _score(settlements):
    """判断比分：M1/M2/M3 × SUPPORT/MIXED/REFUTE + L1L5 + 三问。"""
    slots = ("M1", "M2", "M3")
    dist = {s: {"SUPPORT": 0, "MIXED": 0, "REFUTE": 0, "NA": 0} for s in slots}
    for r in settlements:
        for s in slots:
            vd = r.get(s)
            dist[s][vd if vd in ("SUPPORT", "MIXED", "REFUTE") else "NA"] += 1
    l1 = {k: {"OK": 0, "WRONG": 0, "NA": 0} for k in ("L1", "L2", "L3", "L4", "L5")}
    three = {k: {"OK": 0, "WRONG": 0, "NA": 0} for k in ("驱动", "赔率", "证伪")}
    for r in settlements:
        for k in l1:
            v = (r.get("attribution_L1L5") or {}).get(k)
            l1[k][v if v in ("OK", "WRONG") else "NA"] += 1
        for k in three:
            v = (r.get("attribution_三问") or {}).get(k)
            three[k][v if v in ("OK", "WRONG") else "NA"] += 1
    return {"verdict_dist": dist, "attribution_L1L5": l1, "attribution_三问": three}


def _gaps(as_of, sim, base):
    """缺口显式化：在途未成交（有 plan 无 EXECUTION）+ 已成交未解出（有 EXECUTION 无 pnl）。"""
    settled_ids = {r["decision_id"] for r in PNL.iter_pnl(sim, base=base)}
    pending_plan, unsettled = [], []
    for d in DEC.iter_decisions(sim, base=base):
        if d.get("date", "") > as_of:
            continue
        v = DEC.load_decision(d["id"], sim=sim, base=base)
        if v.get("action") != "BUY":
            continue
        if (v.get("position_plan") or 0) > 0 and v.get("actual_position") is None:
            pending_plan.append({"decision_id": v["id"], "date": v["date"],
                                 "code": v["code"], "plan": v["position_plan"]})
        elif (v.get("actual_position") or 0) > 0 and v["id"] not in settled_ids:
            unsettled.append({"decision_id": v["id"], "date": v["date"],
                              "code": v["code"]})
    return {"pending_plan": sorted(pending_plan, key=lambda x: x["decision_id"]),
            "unsettled": sorted(unsettled, key=lambda x: x["decision_id"])}


def _cum(sim, base):
    """累计数字（费后口径，v1.3 net_curve）。"""
    s = PNL.summary(sim, base=base)
    nc = PNL.net_curve(sim, base=base)
    peak, dd = None, 0.0
    eq0 = None
    for p in nc["points"]:
        eq = p["cum_net"]
        eq0 = eq if eq0 is None else eq0
        peak = eq if peak is None else max(peak, eq)
        if peak is not None and peak > 0:
            dd = max(dd, peak - eq)
    return {"n_decisions": s["n"], "filled": s["filled"],
            "win": s["win"], "loss": s["loss"], "win_rate": s["win_rate"],
            "gross_total": s["pnl_total"], "fee_total": s["fee_total"],
            "net_total": s["net_total"],
            "max_realized_dd_net": round(dd, 6)}


def _param_changes(as_of, since):
    """params_changelog 当日（及自上一复盘日以来）全部行。"""
    out = []
    for r in PARAMS.read_changelog():
        d = r.get("date")
        if d == as_of or (since and since < d <= as_of):
            out.append({"date": d, "param": r.get("param_name"),
                        "old": r.get("old_value"), "new": r.get("new_value"),
                        "approver": r.get("approver"), "reason": r.get("reason")})
    return sorted(out, key=lambda x: (x["date"], x["param"]))


# ── 组装 + 渲染 ──────────────────────────────────────────────────────────────
def _s3_structure(as_of, base):
    """S3 情绪结构当日快照（任务9#5 接线：下游消费真链路）。

    在席 → 摘要字段；缺席 → ABSENT（不编造、不回退旧日快照——load_s_output
    本就只认精确 as_of）。
    """
    rec = LD.load_s_output("s3", as_of, base=base)
    if rec is LD.UNAVAILABLE:
        return {"status": "ABSENT", "as_of": as_of,
                "note": "当日未生成 S3 快照（不回退旧日、不编造）"}
    return {"status": "OK", "as_of": rec.get("as_of"),
            "stage": rec.get("stage"), "temp": rec.get("temp"),
            "leader": (rec.get("leader") or {}).get("name"),
            "mid": (rec.get("mid") or {}).get("name"),
            "catchup_active": (rec.get("catchup") or {}).get("active"),
            "structure_bias": (rec.get("structure_bias") or {}).get("key"),
            "confidence": rec.get("confidence")}


def _cursor_date(base):
    """游标日期（跟随 base；缺文件 → None，不阻塞复盘）。"""
    try:
        p = os.path.join(base or ROOT, "data", "state", "cursor.json")
        return (CUR.read_cursor(p) or {}).get("date")
    except Exception:
        return None


def _market_env(as_of, base):
    """当日市场与环境（任务 209 §1.4：市场环境段；缺项如实 UNKNOWN，不阻塞）。

    数据源（全部只读、跟随 base）：
      · env_metrics 当日快照（limit_up_count/max_board/turnover/up_down_ratio/
        fail_rate/index_ma20）——快照 5 面之一；
      · regime.from_snapshot 盘后档（source="snapshot"；非当日精确快照 → None）；
      · S3 情绪结构（load_s_output 精确 as_of）。
    """
    out = {"status": "OK", "env_metrics": None, "regime": None,
           "limit_up_top": [], "n_limit_up": None, "dragon_tiger_n": None,
           "notes": []}
    # ① env_metrics
    try:
        import snapshot as SNAP
        snap = SNAP.load_snapshot(as_of, root=(os.path.join(base, "data", "snapshot")
                                               if base else None))
    except Exception as e:
        snap = None
        out["notes"].append("快照读取失败: %s: %s" % (type(e).__name__, str(e)[:100]))
    if snap is not None and snap is not SNAP.UNAVAILABLE:
        em = snap.get("env_metrics") or {}
        if em:
            out["env_metrics"] = em
        else:
            out["notes"].append("env_metrics 面为空")
        lu = snap.get("limit_up") or []
        out["n_limit_up"] = len(lu)
        out["limit_up_top"] = sorted(
            lu, key=lambda r: (-(r.get("boards") or 0),
                               -(r.get("seal_amount") or 0)))[:10]
        out["dragon_tiger_n"] = len(snap.get("dragon_tiger") or [])
    else:
        out["notes"].append("当日快照缺席（%s）——市场面不可评估，不编造"
                            % as_of)
    # ② regime 盘后档
    try:
        import regime as RG
        env = RG.from_snapshot(as_of, base=base)
        out["regime"] = (env.get("regime") if isinstance(env, dict) else None)
        if out["regime"] is None:
            out["notes"].append("环境档不可用（非当日精确快照或 env 缺项）")
    except Exception as e:
        out["notes"].append("环境档读取失败: %s" % type(e).__name__)
    # ③ S3 结构（复用既有 _s3_structure）
    out["s3_structure"] = _s3_structure(as_of, base)
    return out


def _opportunities(as_of, base):
    """重点机会回顾（任务 209 §1.4：当日机会面；无候选池/无涨停 → 如实空）。

    数据源：候选池 candidates.json（C2 产物）+ 当日涨停榜（已在 market_env）。
    **只做事实统计，不重判、不排序推荐**（推荐归 C2/C3，E3 boundary=no_decision）。
    """
    out = {"status": "OK", "n_active": None, "n_with_next_trigger": None,
           "n_traded": None, "n_expired": None, "trigger_top": [], "notes": []}
    p = os.path.join(base or ROOT, "data", "state", "candidates.json")
    if not os.path.isfile(p):
        out["status"] = "ABSENT"
        out["notes"].append("候选池缺席（%s）——机会面不可评估" % p)
        return out
    try:
        with io.open(p, encoding="utf-8") as f:
            pool = json.load(f) or {}
    except Exception as e:
        out["status"] = "ABSENT"
        out["notes"].append("候选池读取失败: %s" % type(e).__name__)
        return out
    cands = pool.get("candidates") or []
    active = [c for c in cands if c.get("state") not in ("EXPIRED", "TRADED")]
    out["n_active"] = len(active)
    out["n_traded"] = sum(1 for c in cands if c.get("state") == "TRADED")
    out["n_expired"] = sum(1 for c in cands if c.get("state") == "EXPIRED")
    with_trig = [c for c in active if c.get("next_trigger")]
    out["n_with_next_trigger"] = len(with_trig)
    out["trigger_top"] = [
        {"code": c.get("code"), "state": c.get("state"),
         "trigger": (c.get("next_trigger") or {}).get("text")}
        for c in with_trig[:10]]
    if not with_trig:
        out["notes"].append("活跃候选均无 next_trigger（无出路可回顾）")
    return out


def _holdings(base):
    """持仓/模拟结果（任务 209 §1.4：**有数据才展示**；空仓如实写 0 笔）。

    数据源：memory/state/positions.md（持仓唯一真相源，本件只读引用不复制）。
    """
    out = {"status": "OK", "n_positions": 0, "rows": [], "updated": None,
           "notes": []}
    p = os.path.join(base or ROOT, "memory", "state", "positions.md")
    if not os.path.isfile(p):
        out["status"] = "ABSENT"
        out["notes"].append("持仓档缺席（%s）" % p)
        return out
    try:
        with io.open(p, encoding="utf-8") as f:
            txt = f.read()
    except Exception as e:
        out["status"] = "ABSENT"
        out["notes"].append("持仓档读取失败: %s" % type(e).__name__)
        return out
    m = re.search(r"-\s*\*\*更新时间\*\*：(\S+)", txt)
    if m:
        out["updated"] = m.group(1)
    m = re.search(r"-\s*\*\*持仓数\*\*：(\d+)", txt)
    if m:
        out["n_positions"] = int(m.group(1))
    # 解析持仓表行（| 代码 | 名称 | ... |；跳过表头/分隔线/占位行）
    for ln in txt.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 8:
            continue
        if cells[0] in ("代码",) or set(cells[0]) <= set("-— "):
            continue
        if cells[0] in ("—", "-", ""):
            continue
        out["rows"].append({"code": cells[0], "name": cells[1],
                            "cost": cells[3], "qty": cells[4],
                            "decision_id": cells[8] if len(cells) > 8 else None})
    else:
        out["n_positions"] = 0
    if out["n_positions"] == 0 and not out["rows"]:
        # 空仓 = 账实相符（无 BUY 行即无持仓）——主行已表达，不再重复 note
        return out
    if out["notes"]:
        pass
    return out


def _risk_events(mkt):
    """风险事件（任务 209 §1.4：有数据才展示）。"""
    out = {"status": "OK", "items": [], "notes": []}
    em = mkt.get("env_metrics") or {}
    if em:
        fr = em.get("fail_rate")
        if isinstance(fr, (int, float)) and fr >= 0.3:
            out["items"].append({"kind": "炸板率偏高",
                                 "detail": "fail_rate=%.1f%%（≥30%%）" % (fr * 100)})
        lu = em.get("limit_up_count")
        if isinstance(lu, int) and lu <= 10:
            out["items"].append({"kind": "涨停家数偏少",
                                 "detail": "limit_up_count=%d（≤10）" % lu})
    else:
        out["notes"].append("env_metrics 缺席 → 风险事件面不可评估")
    s3 = mkt.get("s3_structure") or {}
    if s3.get("status") == "OK" and s3.get("structure_bias"):
        out["items"].append({"kind": "S3 结构偏向",
                             "detail": "structure_bias=%s" % s3["structure_bias"]})
    if not out["items"] and not out["notes"]:
        out["notes"].append("当日无触发型风险事件（阈值未达）")
    return out


def _watch_items(rev, base):
    """后续观察事项（任务 209 §1.4）：WAIT 决策的出路 + 未解出在途（有才展示）。

    next_trigger 从候选池按 code 补（决策行本身不存出路文本；无 → 留空不编）。
    """
    out = {"status": "OK", "wait_triggers": [], "open_positions": [],
           "notes": []}
    trig_by_code = {}
    try:
        p = os.path.join(base or ROOT, "data", "state", "candidates.json")
        if os.path.isfile(p):
            with io.open(p, encoding="utf-8") as f:
                pool = json.load(f) or {}
            for c in (pool.get("candidates") or []):
                nt = (c.get("next_trigger") or {}).get("text")
                if nt:
                    trig_by_code[c.get("code")] = nt
    except Exception:
        pass
    for d in rev.get("decisions_today") or []:
        if d.get("action") == "WAIT":
            out["wait_triggers"].append({
                "decision_id": d.get("id"), "code": d.get("code"),
                "next_trigger": trig_by_code.get(d.get("code"))})
    for g in (rev.get("gaps") or {}).get("unsettled") or []:
        out["open_positions"].append(g)
    if not out["wait_triggers"]:
        out["notes"].append("当日无 WAIT 决策（无出路需跟踪）")
    if not out["open_positions"]:
        out["notes"].append("无已成交未解出笔数（无在途需跟踪）")
    return out


def build_review(as_of, sim=False, base=None):
    """某日复盘（确定性；同输入同输出）。"""
    as_of = _chk_date(as_of)
    settlements = _day_settlements(as_of, sim, base)
    prev = _prev_review_date(as_of, sim, base)
    rev = {
        "schema": SCHEMA_VERSION, "cap": "E3", "axis": PNL.axis_name(sim),
        "as_of": as_of, "prev_review_date": prev,
        "params_version": PARAMS.params_version(),
        "cursor": _cursor_date(base),
        "decisions_today": _day_decisions(as_of, sim, base),
        "settlements_today": settlements,
        "judgement_score_today": _score(settlements),
        "judgement_score_cum": _score(_map_settlement(r)
                                      for r in PNL.iter_pnl(sim, base=base)),
        "cumulative": _cum(sim, base),
        "param_changes": _param_changes(as_of, prev),
        "s3_structure": _s3_structure(as_of, base),
        "gaps": _gaps(as_of, sim, base),
        # ── 任务 209 新增数据段（市场/机会/持仓/风险/观察；全部只读 + 缺失如实）──
        "market_env": _market_env(as_of, base),
        "opportunities": _opportunities(as_of, base),
        "holdings": _holdings(base),
    }
    rev["risk_events"] = _risk_events(rev["market_env"])
    rev["watch_items"] = _watch_items(rev, base)
    rev["lessons_pending_review"] = _lessons(rev)
    rev["report_md"] = render_md(rev)
    return rev


def _lessons(rev):
    """教训落点：REFUTE / 三问 WRONG / LOSS 笔归因 → 待人工复核清单（零自动改参）。"""
    out = []
    for r in rev["settlements_today"]:
        for s in ("M1", "M2", "M3"):
            if r[s] == "REFUTE":
                out.append({"decision_id": r["decision_id"], "kind": "REFUTE", "slot": s})
        att = r.get("attribution_三问") or {}
        for k, v in att.items():
            if v == "WRONG" and isinstance(v, str):
                out.append({"decision_id": r["decision_id"], "kind": "三问_WRONG", "slot": k})
        if r["outcome"] == "LOSS":
            out.append({"decision_id": r["decision_id"], "kind": "LOSS",
                        "L1L5": r.get("attribution_L1L5")})
    return out


def _prev_review_date(as_of, sim, base):
    """上一复盘日 = 已有复盘行里同轴、< as_of 的最大日（无 → None）。"""
    best = None
    for row in _read_rows(sim, base):
        d = row.get("as_of")
        if d and d < as_of and (best is None or d > best):
            best = d
    return best


def render_md(rev):
    """纯函数渲染（同 review 逐字节同输出）。

    章节结构借鉴旧系统盘后复盘模板的成熟表现形式（L0 摘要卡 + 市场账定温表 +
    判断总账 + 沉淀层），**数据全部来自新系统账本/快照/参数**，旧指标与旧结论
    一律不沿用（任务 209 补充约束三：旧模板只借鉴表现形式）。
    """
    L = []
    L.append("# E3 盘后复盘 · %s（%s 轴）" % (rev["as_of"], rev["axis"]))
    L.append("")
    L.append("> 数据源：新系统决策账本 / pnl 账本 / params changelog / 当日快照 5 面 / "
             "候选池 / 持仓档（全部只读，时点 = %s 收盘）" % rev["as_of"])
    L.append("")
    # ── L0 摘要卡（旧模板结构：今日判断比分 + 一句话定性 + 数据源标注）─────────
    js_t = rev["judgement_score_today"]
    n_sup = sum(js_t["verdict_dist"][s]["SUPPORT"] for s in ("M1", "M2", "M3"))
    n_ref = sum(js_t["verdict_dist"][s]["REFUTE"] for s in ("M1", "M2", "M3"))
    n_mix = sum(js_t["verdict_dist"][s]["MIXED"] for s in ("M1", "M2", "M3"))
    n_na = sum(js_t["verdict_dist"][s]["NA"] for s in ("M1", "M2", "M3"))
    L.append("## L0 摘要卡")
    L.append("")
    L.append("- **今日判断比分**：%d✅ / %d🔶 / %d❌ / %d◻︎观（M1+M2+M3 合计）"
             % (n_sup, n_mix, n_ref, n_na))
    L.append("- **本日下注**：决策 %d 笔 ｜ 成交 %d 笔 ｜ 结算 %d 笔"
             % (len(rev["decisions_today"]),
                sum(1 for d in rev["decisions_today"]
                    if (d.get("actual_position") or 0) > 0),
                len(rev["settlements_today"])))
    L.append("- **一句话定性**：%s"
             % (_one_line_verdict(rev)))
    L.append("- 上一复盘日: %s ｜ 游标: %s"
             % (rev["prev_review_date"] or "无", rev.get("cursor") or "UNKNOWN"))
    L.append("")
    # ── 市场与环境（旧模板①环境定温表形式；新系统数据）──────────────────────
    L.append("## 一、当日市场与环境")
    mkt = rev.get("market_env") or {}
    em = mkt.get("env_metrics")
    if em:
        L.append("| 指标 | 收盘值 | 说明 |")
        L.append("|---|---|---|")
        L.append("| 涨停家数 | %s | 打板资金共识广度 |" % em.get("limit_up_count"))
        L.append("| 最高连板 | %s | 空间高度 |" % em.get("max_board"))
        L.append("| 两市成交额 | %s | 量能（万亿级=%.2f 万亿） |"
                 % (_fmt_amt(em.get("turnover")), (em.get("turnover") or 0) / 1e12))
        L.append("| 涨跌比 | %s | >1=上涨家数占优 |" % em.get("up_down_ratio"))
        L.append("| 炸板率 | %s | 打板失败占比 |" % _fmt_pct(em.get("fail_rate")))
        L.append("| 指数 MA20 | %s | 中期趋势参照 |" % em.get("index_ma20"))
    else:
        L.append("- 市场指标：**当日快照缺席，不可评估**（不编造、不回退旧日）")
    L.append("- 环境档位（regime 盘后档）: %s"
             % (mkt.get("regime") or "UNKNOWN（非当日精确快照或 env 缺项）"))
    s3 = rev.get("s3_structure") or {}
    if s3.get("status") == "OK":
        L.append("- 情绪结构（S3）: stage=%s ｜ temp=%s ｜ 领先=%s ｜ 赔率背景=%s"
                 % (s3.get("stage"), s3.get("temp"), s3.get("leader") or "无",
                    s3.get("structure_bias")))
    else:
        L.append("- 情绪结构（S3）: 当日快照缺席（未生成不编造）")
    if mkt.get("notes"):
        for n in mkt["notes"]:
            L.append("  - ⚠️ %s" % n)
    L.append("")
    # ── 重点机会回顾 ────────────────────────────────────────────────────────
    L.append("## 二、重点机会回顾（当日事实统计）")
    opp = rev.get("opportunities") or {}
    if opp.get("status") == "OK":
        L.append("- 候选池：活跃 %d ｜ 带出路(next_trigger) %d ｜ 已成交 %d ｜ 已过期 %d"
                 % (opp.get("n_active") or 0, opp.get("n_with_next_trigger") or 0,
                    opp.get("n_traded") or 0, opp.get("n_expired") or 0))
        if mkt.get("n_limit_up"):
            L.append("- 当日涨停 %d 家（龙虎榜 %d 条）"
                     % (mkt.get("n_limit_up"), mkt.get("dragon_tiger_n") or 0))
        if opp.get("trigger_top"):
            L.append("")
            L.append("| code | 状态 | 出路（next_trigger） |")
            L.append("|---|---|---|")
            for t in opp["trigger_top"]:
                L.append("| %s | %s | %s |"
                         % (t.get("code"), t.get("state"),
                            (t.get("trigger") or "")[:60]))
    else:
        L.append("- **候选池缺席，机会面不可评估**（不编造）")
    L.append("")
    # ── 决策与执行 ──────────────────────────────────────────────────────────
    L.append("## 三、决策与执行（当日）")
    if rev["decisions_today"]:
        L.append("| decision | code | 驱动 | req | plan | 成交 | 均价 | gates(简) |")
        L.append("|---|---|---|---|---|---|---|---|")
        for d in rev["decisions_today"]:
            g = d.get("risk_gates") or {}
            binding = [k for k in ("circuit", "single", "env")
                       if isinstance(g.get(k), dict) and g[k].get("binding")]
            L.append("| %s | %s | %s | %s | %s | %s | %s | %s |"
                     % (d["id"], d["code"], d.get("driver") or "-",
                        d.get("position_request"), d.get("position_plan"),
                        d.get("execution_result") or "未执行", d.get("avg_price") or "-",
                        ("binding:" + ",".join(binding)) if binding else "-"))
    else:
        L.append("（当日无决策）")
    L.append("")
    L.append("## 四、结算与盈亏（当日；毛/费/净三口径）")
    if rev["settlements_today"]:
        L.append("| decision | outcome | exit_kind | 毛 | 费 | 净 | pct | 持有日 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for r in rev["settlements_today"]:
            L.append("| %s | %s | %s | %s | %s | %s | %s%% | %s |"
                     % (r["decision_id"], r["outcome"], r.get("exit_kind") or "-",
                        round(r["pnl_amount"], 2), round(r.get("fee_amount") or 0, 2),
                        round(r.get("net_amount") or 0, 2), r["pnl_pct"],
                        r.get("hold_days")))
    else:
        L.append("（当日无结算）")
    L.append("")
    js_t, js_c = rev["judgement_score_today"], rev["judgement_score_cum"]
    L.append("## 五、判断比分（当日 / 累计）")
    for s in ("M1", "M2", "M3"):
        t, c = js_t["verdict_dist"][s], js_c["verdict_dist"][s]
        L.append("- %s: S/M/R/NA = %d/%d/%d/%d （累计 %d/%d/%d/%d）"
                 % (s, t["SUPPORT"], t["MIXED"], t["REFUTE"], t["NA"],
                    c["SUPPORT"], c["MIXED"], c["REFUTE"], c["NA"]))
    L.append("")
    cum = rev["cumulative"]
    L.append("## 六、累计数字（费后口径）")
    L.append("- 决策 %d ｜ 成交 %d ｜ 胜/负 %d/%d（胜率 %s）"
             % (cum["n_decisions"], cum["filled"], cum["win"], cum["loss"],
                cum["win_rate"]))
    L.append("- 毛 %s ｜ 费 %s ｜ 净 %s ｜ 已实现最大回撤(净) %s"
             % (round(cum["gross_total"], 2), round(cum["fee_total"], 2),
                round(cum["net_total"], 2), cum["max_realized_dd_net"]))
    L.append("")
    L.append("## 七、什么变了")
    if rev["param_changes"]:
        L.append("### 参数变更（changelog 有痕）")
        for p in rev["param_changes"]:
            L.append("- %s `%s`: %s → %s（%s · %s）"
                     % (p["date"], p["param"], p["old"], p["new"],
                        p["approver"], (p["reason"] or "")[:60]))
    else:
        L.append("- 参数：无变更（changelog 当日及区间内零行）")
    envs = sorted({d.get("env") for d in rev["decisions_today"] if d.get("env")})
    L.append("- 环境档位（当日决策 env 字段）: %s"
             % (envs if envs else "UNKNOWN（当日无决策或 env 无源）"))
    s3s = rev.get("s3_structure") or {}
    if s3s.get("status") == "OK":
        L.append("- 情绪结构（S3 当日快照）: stage=%s ｜ temp=%s ｜ 领先=%s ｜ "
                 "中军=%s ｜ 补涨激活=%s ｜ 赔率背景=%s ｜ 置信 %s"
                 % (s3s.get("stage"), s3s.get("temp"),
                    s3s.get("leader") or "无", s3s.get("mid") or "无",
                    s3s.get("catchup_active"), s3s.get("structure_bias"),
                    s3s.get("confidence")))
    else:
        L.append("- 情绪结构（S3）: 当日快照缺席（未生成不编造、不回退旧日）")
    L.append("")
    # ── 持仓/模拟结果（有数据才展示）────────────────────────────────────────
    L.append("## 八、持仓与模拟结果")
    hold = rev.get("holdings") or {}
    if hold.get("status") == "ABSENT":
        L.append("- **持仓档缺席，不可评估**（不编造）")
    elif hold.get("rows"):
        L.append("- 持仓 %d 笔（档更新时间 %s）"
                 % (hold.get("n_positions") or len(hold["rows"]),
                    hold.get("updated") or "未标注"))
        L.append("")
        L.append("| 代码 | 名称 | 成本 | 数量 | 对应 decision |")
        L.append("|---|---|---|---|---|")
        for r in hold["rows"]:
            L.append("| %s | %s | %s | %s | %s |"
                     % (r.get("code"), r.get("name"), r.get("cost") or "-",
                        r.get("qty") or "-", r.get("decision_id") or "-"))
    else:
        L.append("- 空仓（0 笔）——**账实相符**：无 BUY 行即无持仓（settle 步2/步3 机械对账）")
    if hold.get("notes"):
        for n in hold["notes"]:
            L.append("  - %s" % n)
    L.append("")
    # ── 风险事件（有数据才展示）─────────────────────────────────────────────
    L.append("## 九、风险事件")
    rk = rev.get("risk_events") or {}
    if rk.get("items"):
        for it in rk["items"]:
            L.append("- **%s**：%s" % (it["kind"], it["detail"]))
    else:
        L.append("- 当日无触发型风险事件（阈值未达或数据缺席）")
    for n in (rk.get("notes") or []):
        L.append("  - ⚠️ %s" % n)
    L.append("")
    # ── 后续观察事项 ────────────────────────────────────────────────────────
    L.append("## 十、后续观察事项")
    wi = rev.get("watch_items") or {}
    if wi.get("wait_triggers"):
        L.append("### WAIT 出路跟踪")
        L.append("| decision | code | 出路（next_trigger） |")
        L.append("|---|---|---|")
        for w in wi["wait_triggers"]:
            L.append("| %s | %s | %s |"
                     % (w.get("decision_id"), w.get("code"),
                        (w.get("next_trigger") or "（候选池无出路文本）")[:60]))
    if wi.get("open_positions"):
        L.append("### 在途未解出（已成交未结算）")
        for o in wi["open_positions"]:
            L.append("- %s %s（%s）" % (o.get("decision_id"), o.get("code"),
                                       o.get("date")))
    for n in (wi.get("notes") or []):
        L.append("- %s" % n)
    L.append("")
    L.append("## 十一、教训落点（待人工复核；零自动改参）")
    if rev["lessons_pending_review"]:
        for x in rev["lessons_pending_review"]:
            L.append("- %s %s%s" % (x["decision_id"], x["kind"],
                                    (" " + x["slot"]) if x.get("slot") else ""))
    else:
        L.append("（当日无 REFUTE / 三问 WRONG / LOSS）")
    L.append("")
    L.append("> 缺口显式化：在途未成交与未解出清单见 JSONL 行 gaps 字段；"
             "本报告由账本机械生成，同输入逐字节同输出。")
    return "\n".join(L)


# ── 渲染辅助（纯格式化，零判断）─────────────────────────────────────────────
def _fmt_amt(v):
    if not isinstance(v, (int, float)):
        return "-"
    if v >= 1e12:
        return "%.2f 万亿" % (v / 1e12)
    if v >= 1e8:
        return "%.1f 亿" % (v / 1e8)
    return "%.0f" % v


def _fmt_pct(v):
    if not isinstance(v, (int, float)):
        return "-"
    return "%.1f%%" % (v * 100)


def _one_line_verdict(rev):
    """一句话定性（**纯事实组装，零判断**：只把当日决策/结算/比分拼成一句）。"""
    parts = []
    js = rev["judgement_score_today"]
    n_ref = sum(js["verdict_dist"][s]["REFUTE"] for s in ("M1", "M2", "M3"))
    n_sup = sum(js["verdict_dist"][s]["SUPPORT"] for s in ("M1", "M2", "M3"))
    if n_sup or n_ref:
        parts.append("判断 %d✅/%d❌" % (n_sup, n_ref))
    n_dec = len(rev["decisions_today"])
    n_st = len(rev["settlements_today"])
    if n_st:
        net = sum(r.get("net_amount") or 0 for r in rev["settlements_today"])
        parts.append("结算 %d 笔净 %+.2f 元" % (n_st, net))
    elif n_dec:
        parts.append("决策 %d 笔、零结算（当日无可结账判断）" % n_dec)
    if not parts:
        parts.append("当日零决策零结算（无内容可定性）")
    return "；".join(parts)


# ── 落盘（append-only + 幂等）────────────────────────────────────────────────
def _read_rows(sim, base):
    """已有复盘行（同轴；**跟随 base**）。"""
    rp = _report_path(base)
    if not os.path.isfile(rp):
        return []
    axis = PNL.axis_name(sim)
    out = []
    with io.open(rp, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            r = json.loads(ln)
            if r.get("axis") == axis:
                out.append(r)
    return out


def _content_hash(rev):
    """内容指纹（剔除 generated_at——重跑时刻不同不算内容变化）。"""
    body = {k: v for k, v in rev.items() if k not in ("generated_at", "report_md")}
    body["report_md"] = rev.get("report_md")       # md 是纯函数产物，参与指纹
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8")).hexdigest()


def append_review(rev, base=None):
    """双落点（**全部跟随 base**）：
      ① e3_daily.jsonl —— append-only 幂等（同 (axis, as_of) 同内容 → EXISTS；
         异内容 → 拒，禁改写）；
      ② e3_<as_of>.md  —— 人读报告（**幂等覆写**：同内容跳过、异内容重写——
         md 是 jsonl 的渲染投影，非历史账本，故允许覆写保持一致）。
    生产根写入须过 writegate（无票即拒）；沙盒根零闸口（沙盒不在受护区）。
    """
    rev = dict(rev)
    rev["generated_at"] = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    h = _content_hash(rev)
    rp = _report_path(base)
    for row in _read_rows(rev["axis"] == "SIM", base):
        if row.get("as_of") == rev["as_of"]:
            if row.get("content_hash") == h:
                _write_md(rev, base)      # jsonl 已存在 → md 仍需保证一致（覆写幂等）
                return {"created": False, "reason": "EXISTS", "path": rp}
            raise E3Error("复盘记录 %s/%s 已存在且内容不同 —— append-only 禁改写"
                          % (rev["axis"], rev["as_of"]))
    row = dict(rev)
    row["content_hash"] = h
    d = os.path.dirname(rp)
    if d:
        os.makedirs(d, exist_ok=True)
    _WG.check_write(rp)
    with io.open(rp, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    _write_md(rev, base)
    return {"created": True, "path": rp, "content_hash": h}


def _write_md(rev, base=None):
    """md 报告幂等落盘（同内容跳过；异内容覆写——渲染投影非账本）。"""
    mp = _md_path(base, rev["as_of"])
    d = os.path.dirname(mp)
    if d:
        os.makedirs(d, exist_ok=True)
    txt = rev["report_md"]
    if os.path.isfile(mp):
        try:
            with io.open(mp, encoding="utf-8") as f:
                if f.read() == txt:
                    return mp
        except Exception:
            pass
    _WG.check_write(mp)
    with io.open(mp, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)
    return mp


# ── CLI ──────────────────────────────────────────────────────────────────────
def _main(argv):
    if "--selftest" in argv:
        return 0 if _selftest() else 1
    ap = argparse.ArgumentParser(
        description="E3 盘后复盘报告（零决策·机械生成·沙盒可用）")
    ap.add_argument("--as-of", dest="as_of", required=True,
                    help="复盘交易日（YYYY-MM-DD）")
    ap.add_argument("--sim", action="store_true", help="SIM 轴（缺省 = LIVE 轴）")
    ap.add_argument("--base", help="数据根覆盖（沙盒；空 = 生产，生产写入须过 writegate）")
    ap.add_argument("--stdout", action="store_true", help="同时打印 Markdown 报告")
    a = ap.parse_args(argv)
    if a.base and not os.path.isdir(a.base):
        print("失败: --base 必须是已存在目录，实际: %s" % a.base)
        return 2
    rev = build_review(a.as_of, sim=a.sim, base=a.base)
    r = append_review(rev, base=a.base)
    print("E3 复盘 %s/%s → %s（%s）"
          % (rev["axis"], rev["as_of"], r["path"],
             "新建" if r["created"] else "已存在（幂等 EXISTS）"))
    print("  Markdown 报告 → %s" % _md_path(a.base, rev["as_of"]))
    if a.stdout:
        print(rev["report_md"])
    return 0


# ── 自检（沙盒账本，不碰真库）───────────────────────────────────────────────
def _selftest(verbose=True):
    import shutil
    import tempfile
    import datetime as _dt

    def T(name, cond):
        if verbose:
            print("  [%s] %s" % ("✓" if cond else "!!", name))
        return bool(cond)

    ok = fail = 0

    def chk(cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1

    tmp = tempfile.mkdtemp(prefix="e3_selftest_")
    try:
        base = os.path.join(tmp, "gen")
        os.makedirs(base, exist_ok=True)
        # 夹具：两笔决策（一笔已结算 WIN 带费、一笔 plan>0 未成交在途）
        import thesis as TH
        th_p = os.path.join(base, "thesis_store.jsonl")
        TH.init_file(th_p)
        t0 = TH.create_thesis("2026-10-09", "A", "驱动：夹具", code="sh600000",
                              path=th_p)["id"]

        def pred(slot):
            return {"prediction_id": slot, "metric": "m_%s" % slot,
                    "data_source": "snapshot", "baseline": "T 日收盘",
                    "direction": "UP", "threshold": 3.0,
                    "condition": "T+3 累计涨幅 ≥3%", "window": "3D"}

        FIVE = {"驱动存在": "板块级订单落地", "当前证据": "公告+资金同步",
                "延续机制": "订单排产→业绩预期", "失效条件": "公告被澄清",
                "剩余预期": "3 日内扩散"}
        chain = {"evidence": ["公告"], "inference": ["催化→A"],
                 "prediction": [pred(s) for s in PNL.SLOTS], "five": dict(FIVE)}
        d1 = DEC.write_decision("2026-10-09", "sh600000", "BUY", t0, chain, sim=False,
                                base=base, thesis_path=th_p, driver="A",
                                entry_limit=10.0, stop_line=9.5, target=11.0,
                                window=3, position_request=0.2, env={"regime": "强势", "signals": {}, "leg": "INTRADAY", "source": "EXPLICIT"})
        DEC.append_risk_plan(d1["id"], 0.2, as_of="2026-10-09",
                             gates={"circuit": {"binding": False}},
                             base=base)
        DEC.append_execution(d1["id"], 0.2, "filled", avg_price=10.0,
                             as_of="2026-10-09", base=base)
        vd = lambda: {s: {"prediction_id": s, "metric": "m_" + s,                # noqa: E731
                          "data_source": "snapshot",
                          "verdict": "SUPPORT", "observed": None, "note": None}
                      for s in PNL.SLOTS}
        L1 = {"L1": "OK", "L2": "OK", "L3": "OK", "L4": "OK", "L5": "OK"}
        THQ = {"驱动": "OK", "赔率": "OK", "证伪": "OK"}
        PNL.write_pnl(d1["id"], "WIN", verdicts=vd(), attribution_L1L5=L1,
                      attribution_三问=THQ, entry_price=10.0, exit_price=11.0,
                      exit_kind="TARGET", exit_date="2026-10-10", capital=20000.0,
                      fee_amount=60.0, settle_date="2026-10-10", base=base)
        d2 = DEC.write_decision("2026-10-10", "sz000001", "BUY",
                                TH.create_thesis("2026-10-10", "A", "驱动：夹具2",
                                                 code="sz000001", path=th_p)["id"],
                                chain, sim=False, base=base, thesis_path=th_p,
                                driver="B", entry_limit=20.0, stop_line=19.0,
                                target=22.0, window=3, position_request=0.2,
                                env={"regime": "退潮", "signals": {}, "leg": "INTRADAY", "source": "EXPLICIT"})
        DEC.append_risk_plan(d2["id"], 0.1, as_of="2026-10-10", base=base)

        print("== A. 组装 ==")
        # S3 快照夹具（在席分支）：当日结构快照 → 复盘消费真链路
        s3_dir = os.path.join(base, "data", "generated")
        os.makedirs(s3_dir, exist_ok=True)
        s3_snap = {"schema": 1, "s_id": "s3", "cap": "S3", "as_of": "2026-10-10",
                   "temp": "热", "stage": "高潮",
                   "leader": {"code": "sh600000", "name": "夹具龙头", "boards": 5},
                   "mid": None, "catchup": {"active": True},
                   "structure_bias": {"key": "climax_heat"},
                   "confidence": 0.9}
        with io.open(os.path.join(s3_dir, "s_s3_2026-10-10.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(s3_snap, f, ensure_ascii=False, indent=1)
        rev = build_review("2026-10-10", sim=False, base=base)
        chk(T("A1 当日决策 1 笔（d2）；在途未成交 1（d2 plan 无 EXECUTION）；未解出 0",
              len(rev["decisions_today"]) == 1
              and len(rev["gaps"]["pending_plan"]) == 1
              and rev["gaps"]["pending_plan"][0]["decision_id"] == d2["id"]
              and len(rev["gaps"]["unsettled"]) == 0))
        chk(T("A2 当日结算 1 笔（d1）毛/费/净三口径齐",
              len(rev["settlements_today"]) == 1
              and rev["settlements_today"][0]["net_amount"]
              == rev["settlements_today"][0]["pnl_amount"] - 60.0))
        chk(T("A3 判断比分当日 SUPPORT=3", 
              rev["judgement_score_today"]["verdict_dist"]["M1"]["SUPPORT"] == 1
              and rev["judgement_score_cum"]["verdict_dist"]["M2"]["SUPPORT"] == 1))
        chk(T("A4 教训落点空（无 REFUTE/LOSS）",
              rev["lessons_pending_review"] == []))
        chk(T("A5 环境档位来自决策 env（退潮）",
              "退潮" in [d.get("env") for d in rev["decisions_today"]]))
        chk(T("A6 md 渲染纯函数（两次逐字节一致）",
              render_md(rev) == render_md(build_review("2026-10-10", sim=False,
                                                       base=base))))
        chk(T("A7 S3 快照在席 → 结构摘要入复盘（stage/领先/补涨）",
              rev["s3_structure"]["status"] == "OK"
              and rev["s3_structure"]["stage"] == "高潮"
              and rev["s3_structure"]["leader"] == "夹具龙头"
              and "情绪结构（S3 当日快照）" in rev["report_md"]))
        rev_prev = build_review("2026-10-09", sim=False, base=base)
        chk(T("A8 S3 快照缺席 → 如实缺席（不回退旧日/不编造）",
              rev_prev["s3_structure"]["status"] == "ABSENT"
              and "情绪结构（S3）: 当日快照缺席" in rev_prev["report_md"]))

        print("== B. 落盘幂等 ==")
        # ★ 全部显式带 base（沙盒隔离；**不 monkeypatch 全局路径**——
        #   _report_path(base) 后 REPORT 已是非常量路径函数，沙盒隔离靠参数）
        r1 = append_review(rev, base=base)
        chk(T("B1 首写 created", r1["created"]))
        r2 = append_review(build_review("2026-10-10", sim=False, base=base),
                           base=base)
        chk(T("B2 同内容重跑 → EXISTS（幂等）",
              not r2["created"] and r2["reason"] == "EXISTS"))
        chk(T("B2b md 报告落盘且与 jsonl 行 report_md 一致",
              os.path.isfile(_md_path(base, "2026-10-10"))
              and io.open(_md_path(base, "2026-10-10"), encoding="utf-8").read()
              == rev["report_md"]))
        tampered = dict(rev)
        tampered["cumulative"] = dict(rev["cumulative"], net_total=99999.0)
        try:
            append_review(tampered, base=base)
            chk(T("B3 同日异内容 → 拒（append-only 禁改写）", False))
        except E3Error:
            chk(T("B3 同日异内容 → 拒（append-only 禁改写）", True))
        chk(T("B4 上一复盘日=无（首日）", rev["prev_review_date"] is None))
        chk(T("B5 --base 隔离：生产 e3_daily.jsonl 未被触碰",
              os.path.abspath(_report_path(base)) != os.path.abspath(REPORT)
              or not os.path.exists(REPORT)))

        print("== C. 纪律 ==")
        import ast
        src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
        tree = ast.parse(src)
        bad_imp = {a2.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                   for a2 in n.names}
        chk(T("C1 零网络/子进程导入",
             not (bad_imp & {"requests", "socket", "subprocess", "urllib", "http"})))
        chk(T("C2 写点受控：jsonl 只 append + md 唯一 w 写点",
             src.count('io.open(rp, "a"') == 2      # 真写点 1 + 本断言字面量 1
             and src.count('io.open(mp, "w"') == 2))
        chk(T("C3 零自动改参：无 params 写调用",
             ("PARAMS.prop" + "ose(") not in src
             and ("_write_par" + "ams") not in src
             and ("PROM" + "OTE(") not in src))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return fail == 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
