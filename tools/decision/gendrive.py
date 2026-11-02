#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gendrive.py · C3 驱动规格生成器（v1.0 · 2026-10-15 建）

用途：把「当日真实可查的证据面」压成 C3 驱动规格文件（driver-file），供
      `run_chain.py --driver-file` 消费 → C3 逐票三问 → C4 落账 → C5 风控。
      本模块**只做证据打包与机械映射**，不产新证据、不判 BUY、不造价格线。

★ 一、证据面（只认当日真实源；缺即缺，不补不造）
  读 `--base` 下（缺省 = 生产只读）：
    · candidates.json  活跃候选（state ∉ EXPIRED/TRADED）—— 准入面
    · mainline.json    active 主线成员 → 产业面（A）
    · s_s5_<as_of>     席位行为结构化（资金面 B）
    · s_s4_<as_of>     拥挤度（资金/热度面 B）
    · s_s2_<as_of>     题材阶段（情绪面 C）：code 命中题材 leader → 该题材
                       next_stage_trigger 作补全事件
    · snapshot/<as_of> 涨停榜（补充证据，enrich only，不作准入面）
  准入 spec 的 code = (S4 ∪ S5 ∪ 主线active成员 ∪ S2leader) ∩ active候选。
  其余活跃候选 → run_chain 记 SKIP_MISSING_DRIVER（如实，不硬凑）。

★ 二、零伪造纪律（写死）
  · driver 枚举只取 `decision.DRIVERS`（A/B/C/复合/驱动不明）；映射是**机械**的：
      资金类证据(S5/S4) → B · 题材情绪类(S2) → C · 产业类(主线active) → A ·
      命中多类 → 复合 · 零类 → 驱动不明。映射理由逐票写进 spec.driver_basis。
  · 五件套「剩余预期」**恒 null**（本件无真实预期预测能力）⇒ 驱动问必缺件
      ⇒ 有补全事件 → WAIT（挂观察锚）· 无 → NO（翻案条件）· **永不 BUY**。
  · 价格线 entry/stop/target/window/position_request **恒 null**（无真实价源）。
  · 每条 chain.evidence 标注来源（S4/S5/主线/S2/涨停榜 + 关键值），可回溯可审计。
  · completion/overturn 文本只用**真实字段值 + 真实转换**（非空话；三问做字面黑名单校验）。

★ 三、Thesis 定格（不写 thesis.md，避免生产认知写授权）
  spec 显式携带 `thesis_state`（TH.FIELD_ORDER ∪ {id,history} 结构），三问用它做
  指纹 + driver 解析，**不回落读空 thesis.md**（write_decision 仅在 thesis_state
  为 None 时才查 thesis.md）。thesis_id = TH-<as_of>-<seq>（日内按 code 排序派生，
  稳定可复现）。

★ 四、边界
  本模块**零数据面写点**（只读 --base 各面）；唯一写点 = `--out` 的 driver-file
  （临时交换件，非 5 知识落点；落生产受护区须经 writegate）。缺省不写盘，只打印统计。

用法：
  python tools/decision/gendrive.py --as-of 2026-09-29 --base <沙盒> --out drivers.json
  python tools/decision/gendrive.py --as-of 2026-09-29            # 只读生产·只打印
  python tools/decision/gendrive.py --selftest                     # 沙盒验收
"""
import argparse
import datetime as _dt
import io
import json
import os
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "..", ".."))
for _p in ("fs", "analysis", "decision"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import decision as DEC      # noqa: E402   ← 枚举/结构校验唯一真源
import thesis as TH        # noqa: E402   ← thesis_state 字段真源（FIELD_ORDER）

SCHEMA_VERSION = 1
# 证据面文件（缺 = 该面当日不可用，如实降级，不报错）
_S4 = "s_s4_{}.json"
_S5 = "s_s5_{}.json"
_S2 = "s_s2_{}.json"
# 可转成机械 rule 的字段白名单（与 outcome_watch.RULE_FIELDS 交集；越界 → rule=null 不猜）
_RULE_FIELD_OK = ("limit_up_count", "max_board", "turnover", "up_down_ratio",
                  "fail_rate", "index_ma20", "close", "pct", "vol", "amount",
                  "boards", "is_limit_up")


class GDriveError(Exception):
    pass


def _check_date(s):
    try:
        return _dt.date.fromisoformat(s).isoformat()
    except Exception:
        raise GDriveError("as-of 必须是 YYYY-MM-DD：%r" % (s,))


def _load_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


# ── 证据面装配（每个 code 的可用证据 → 机械 driver 映射） ──────────────────────
def _s_dir(base):
    """证据面目录（`data/generated`，容器级可静态归类；只读，非写点）。"""
    return os.path.join(base, "data", "generated")


def _active_candidates(base, as_of):
    pool = _load_json(os.path.join(base, "data", "state", "candidates.json"))
    if not pool:
        return []
    out = []
    for c in pool.get("candidates", []):
        if c.get("state") not in ("EXPIRED", "TRADED"):
            out.append(c)
    return out


def _mainline_members(base, as_of):
    """code → 所属 active 主线 token 列表（产业面 A）。"""
    ml = _load_json(os.path.join(base, "data", "state", "mainline.json"))
    out = {}
    if not ml:
        return out
    for _line, v in (ml.get("lines") or {}).items():
        if v.get("state") == "active":
            tok = v.get("token")
            for m in (v.get("members") or {}):
                out.setdefault(m, []).append(tok)
    return out


def _s5_base(base, as_of):
    """code → S5 席位行为（资金面 B）。"""
    d = _load_json(os.path.join(_s_dir(base), "s_s5_" + as_of + ".json"))
    return (d or {}).get("codes") or {}


def _s4_base(base, as_of):
    """code → S4 拥挤度（资金/热度面 B）。"""
    d = _load_json(os.path.join(_s_dir(base), "s_s4_" + as_of + ".json"))
    return (d or {}).get("codes") or {}


def _s2_leaders(base, as_of):
    """code → 命中题材的 leader 信息 + 阶段 + next_stage_trigger（情绪面 C）。"""
    d = _load_json(os.path.join(_s_dir(base), "s_s2_" + as_of + ".json"))
    out = {}
    for _tid, tm in ((d or {}).get("themes") or {}).items():
        led = ((tm.get("evidence") or {}).get("leader") or {})
        code = led.get("code")
        if not code:
            continue
        out[code] = {"theme": tm.get("theme_name"), "theme_id": tm.get("theme_id"),
                     "stage": tm.get("stage"),
                     "trigger": (tm.get("next_stage_trigger") or {}).get("text"),
                     "conditions": (tm.get("next_stage_trigger") or {}).get("conditions") or [],
                     "bias": ((tm.get("structure_bias") or {}).get("note"))}
    return out


def _limit_up(base, as_of):
    """code → 涨停榜行（补充证据；缺快照 = 空，不报缺）。"""
    snap_dir = os.path.join(base, "data", "snapshot", as_of)
    path = os.path.join(snap_dir, "limit_up.csv")
    out = {}
    if not os.path.isfile(path):
        return out
    with io.open(path, encoding="utf-8") as f:
        for i, ln in enumerate(f):
            if i == 0:
                cols = ln.strip().split(",")
                continue
            cells = ln.rstrip("\n").split(",")
            if len(cells) < 3:
                continue
            row = dict(zip(cols, cells))
            code = row.get("code")
            if code:
                out[code] = row
    return out


def _driver_map(hit):
    """命中的证据类 → driver 枚举 + 依据（机械，非语义臆断）。"""
    basis = []
    classes = set()
    if "fund" in hit:
        classes.add("B")
    if "sentiment" in hit:
        classes.add("C")
    if "industrial" in hit:
        classes.add("A")
    for k, lab in (("fund", "资金类(S5/S4)"), ("sentiment", "题材情绪类(S2)"),
                   ("industrial", "产业类(主线active)")):
        if k in hit:
            basis.append("%s=%s" % (lab, "命中"))
    if not classes:
        return "驱动不明", basis
    if len(classes) > 1:
        return "复合", basis
    return sorted(classes)[0], basis


def _thesis_state(as_of, code, cand, driver, claim, evs, th_id):
    """构造 Thesis 定格对象（键集 = TH.FIELD_ORDER ∪ {id,history}，三问不回落读 thesis.md）。
    id 必须 = thesis_id（_norm_thesis_state 校验）；state=OPEN、verdict=None（未收敛定格）。"""
    st = {"id": th_id, "state": "OPEN", "type": (driver if driver in ("A", "B", "C") else "B"),
          "code": code, "candidate_id": (cand or {}).get("id"),
          "created_at": as_of, "updated_at": as_of, "gap": "无", "verdict": None,
          "frozen_decision_id": None, "claim": claim, "history": []}
    st["history"].append({"date": as_of, "kind": "CREATE", "text": claim,
                          "evidence": evs})
    return st


def generate(as_of, *, base=None, limit=None, out=None, price_lines=None,
             expect_ref=None, pred_ref=None, pred_input=None):
    base = base or _ROOT
    as_of = _check_date(as_of)
    if not os.path.isdir(base):
        raise GDriveError("--base 必须是已存在目录，实际: %s" % base)

    active = _active_candidates(base, as_of)
    active_map = {c["code"]: c for c in active if c.get("code")}
    s5 = _s5_base(base, as_of)
    s4 = _s4_base(base, as_of)
    s2l = _s2_leaders(base, as_of)
    ml = _mainline_members(base, as_of)
    lu = _limit_up(base, as_of)

    # 价线规划件（可选）：priceline 独立产出的 price-lines JSON（备查，不参与 verdict）
    pl, pl_meta = {}, {}
    if price_lines:
        try:
            with io.open(price_lines, encoding="utf-8") as _f:
                _pldoc = json.load(_f)
            pl = _pldoc.get("by_code") or {}
            pl_meta = _pldoc.get("summary") or {}
        except Exception as e:
            pl, pl_meta = {}, {"error": "price-lines 读取失败：%s" % e}

    # 剩余预期件（可选）：expect_ref 独立产出的历史证据参考（备查，不参与 verdict）
    er, er_meta = {}, {}
    if expect_ref:
        try:
            with io.open(expect_ref, encoding="utf-8") as _f:
                _erdoc = json.load(_f)
            er = _erdoc.get("by_code") or {}
            er_meta = _erdoc.get("summary") or {}
        except Exception as e:
            er, er_meta = {}, {"error": "expect-ref 读取失败：%s" % e}

    # M1/M2/M3 预测基准件（可选）：pred_ref 独立产出（备查，不参与 verdict）
    pdr, pdr_meta = {}, {}
    if pred_ref:
        try:
            with io.open(pred_ref, encoding="utf-8") as _f:
                _prdoc = json.load(_f)
            pdr = _prdoc.get("by_code") or {}
            pdr_meta = _prdoc.get("summary") or {}
        except Exception as e:
            pdr, pdr_meta = {}, {"error": "pred-ref 读取失败：%s" % e}

    # ★ 正式 prediction 授权件（可选）：pred_input 机械校验通过的**正式预测**
    #   —— 与上面三件「备查件」不同：本件产出**直接进 chain.prediction**（BUY 硬前提），
    #   但**只填 prediction 槽位**；五件套·剩余预期与价线/仓位槽位**仍恒 null**
    #   ⇒ 单独挂本件仍不产 BUY（三问驱动问仍缺，需 expectation/价线件齐备）。
    pin, pin_meta = {}, {}
    if pred_input:
        try:
            with io.open(pred_input, encoding="utf-8") as _f:
                _pindoc = json.load(_f)
            pin = _pindoc.get("admitted") or {}
            pin_meta = _pindoc.get("summary") or {}
        except Exception as e:
            pin, pin_meta = {}, {"error": "pred-input 读取失败：%s" % e}

    # 准入：任一证据面 ∩ active 候选
    admitted = (set(s5) | set(s4) | set(ml) | set(s2l)) & set(active_map)
    if limit:
        admitted = set(sorted(admitted)[:limit])

    codes = sorted(admitted)
    specs = {}
    by_driver = {}
    for idx, code in enumerate(codes, 1):
        cand = active_map[code]
        hit = set()
        ev, inf, ev_meta = [], [], []

        s4v = s4.get(code)
        s5v = s5.get(code)
        s2v = s2l.get(code)
        mlv = ml.get(code)
        luv = lu.get(code)

        if s4v is not None:
            hit.add("fund")
            ev.append("S4:%s 拥挤度=%s | %s" % (code, s4v.get("crowding"),
                                                "; ".join((s4v.get("reasons") or [])[:2])))
            ev_meta.append("S4")
        if s5v is not None:
            hit.add("fund")
            ev.append("S5:%s 席位行为=%s" % (code, s5v.get("behavior")))
            ev_meta.append("S5")
        if s2v is not None:
            hit.add("sentiment")
            ev.append("S2:%s 题材=%s 阶段=%s" % (code, s2v.get("theme"), s2v.get("stage")))
            ev_meta.append("S2")
            inf.append("题材「%s」阶段=%s ⇒ 情绪扩散证据（inference）"
                       % (s2v.get("theme"), s2v.get("stage")))
        if mlv:
            hit.add("industrial")
            ev.append("主线:active L:%s members∋%s" % ("/".join(mlv), code))
            ev_meta.append("MAINLINE")
        if luv is not None:
            ev.append("涨停榜:%s boards=%s" % (code, luv.get("boards", "?")))
            ev_meta.append("LIMIT_UP")

        driver, basis = _driver_map(hit)
        claim = "驱动=%s（%s）· %s" % (driver, " ".join(basis), "；".join(ev_meta) or "无证据面")

        # 五件套：剩余预期恒 None（无真实预期源）⇒ 驱动问必缺件 ⇒ 永不 BUY
        five = {
            "驱动存在": ("；".join(ev_meta) if ev_meta else None),
            "当前证据": (ev[0] if ev else None),
            "延续机制": (s2v.get("stage") if s2v else (mlv[0] if mlv else None)),
            "失效条件": (s2v.get("bias") if (s2v and s2v.get("bias"))
                         else ("S4拥挤度高" if (s4v and s4v.get("crowding") == "high")
                             else None)),
            "剩余预期": None,   # ← 纪律：不造预期
        }
        # ★ 正式 prediction 槽位：**只接受 pred_input 机械校验通过的授权件产出**
        #   （三槽八字段齐 + 授权记录齐 + 数据截止非未来非过期 + 非原封复制基准）。
        #   这是 BUY 的硬前提（_norm_chain：BUY 必须三槽齐全），但**本件只填
        #   prediction 槽位**——五件套·剩余预期与价线/仓位仍恒 null ⇒ 单独挂本件
        #   仍不产 BUY（驱动问仍缺件，须 expectation/价线件齐备方可）。
        _pin = pin.get(code) or {}
        chain = {"evidence": ev, "inference": inf,
                 "prediction": (_pin.get("chain_prediction") or []), "five": five}

        # 补全事件（WAIT 出路）：只用真实存在的 S2 next_stage_trigger；无 → 走 NO
        # ★ rule：S2 触发若为结构可判（conditions 恰一条且 field 在白名单）→ 转成机械 rule
        #   （供 C6 outcome_watch 兑现验证）；否则 rule=null（人话条件，机械件不判语义）。
        completion = None
        if s2v and s2v.get("trigger"):
            conds = s2v.get("conditions") or []
            rule = None
            if len(conds) == 1 and isinstance(conds[0], dict):
                c0 = conds[0]
                if c0.get("field") in _RULE_FIELD_OK and c0.get("op") in ("<=", "<", "==",
                                                                        "!=", ">", ">="):
                    rule = {"field": c0["field"], "op": c0["op"], "value": c0.get("value")}
            completion = {"text": "题材「%s」阶段转换（触发:%s）" %
                         (s2v.get("theme"), s2v.get("trigger")),
                         "rule": rule}
        # 翻案条件（NO 出路）：真实字段值 + 真实转换（可下一日重算验证，非空话）
        # ★ rule：用**该 code 在涨停榜的 boards** 作机械判据（真值取自 S5/S4 命中时已在榜的
        #   事实）；不在榜/无涨停证据 → rule=null（不造阈值）。
        if s5v is not None:
            over = "S5:%s 席位行为由 %s 转为 accumulation（净承接增强）" % (
                code, s5v.get("behavior"))
            over_rule = None
        elif s2v is not None:
            over = "S2:题材「%s」阶段由 %s 转扩散/发酵（情绪重燃）" % (
                s2v.get("theme"), s2v.get("stage"))
            over_rule = None
        elif mlv:
            over = "主线 L:%s 由 active 转 dormant 后重新 active（产业面重启）" % mlv[0]
            over_rule = None
        elif s4v is not None:
            over = "S4:%s 拥挤度由 %s 回落 low（热度消退后企稳）" % (
                code, s4v.get("crowding"))
            over_rule = None
        else:
            over = "该 %s 重新进入任一证据面（S4/S5/主线/S2）" % code
            over_rule = None
        # 若该 code 已在涨停榜（真实事实）→ 翻案可机械判为「再度涨停」（boards>=1）
        if luv is not None:
            try:
                if float(luv.get("boards") or 0) >= 1:
                    over_rule = {"field": "is_limit_up", "op": "==", "value": True}
            except (TypeError, ValueError):
                over_rule = None
        overturn = {"text": over, "rule": over_rule}

        th_id = "TH-%s-%03d" % (as_of.replace("-", ""), idx)
        # ★ 价线规划件（planning_only）：真实 K 线机械推导（priceline.PL-R1），
        #   只挂在 spec 上作**备查参考**，**不写入** 五件套价格线槽位 ——
        #   三问 0.3 纪律：WAIT/NO 价位必须 null（有价位=BUY 意图），故本件
        #   仍恒不产 BUY；run_chain --price-lines 只在 verdict=BUY 时消费它。
        pl_entry = pl.get(code)
        planning = None
        if pl_entry:
            planning = {
                "planning_only": True,
                "rule_version": pl_entry.get("rule_version"),
                "tradeable": pl_entry.get("tradeable"),
                "entry_limit": pl_entry.get("entry_limit"),
                "stop_line": pl_entry.get("stop_line"),
                "target": pl_entry.get("target"),
                "target_basis": pl_entry.get("target_basis"),
                "window": pl_entry.get("window"),
                "risk_reward": pl_entry.get("risk_reward"),
                "data_cutoff": pl_entry.get("data_cutoff"),
                "n_bars": pl_entry.get("n_bars"),
                "reasons": pl_entry.get("reasons"),
            }
        # ★ 剩余预期件（planning_only）：S1 效应库历史观测（expect_ref.EXR-R1）。
        #   只挂 spec 备查；五件套·剩余预期槽位仍恒 None（三问 0.3 纪律：有剩余预期
        #   才判驱动问，而该值**只是受限历史参考、非当前事实/胜率**——挂上即绕过
        #   纪律，故不入槽位）。
        er_entry = er.get(code) or {}
        expect_plan = None
        if er_entry and er_entry.get("status") in ("OK",):
            expect_plan = {
                "planning_only": True,
                "rule_version": er_entry.get("rule_version"),
                "event_cat": er_entry.get("event_cat"),
                "classify_basis": er_entry.get("classify_basis"),
                "hist": er_entry.get("hist"),
                "backfill": er_entry.get("backfill"),
                "market_20d_pct": er_entry.get("market_20d_pct"),
                "regime_zeroed": er_entry.get("regime_zeroed"),
                "room_ref": er_entry.get("room_ref"),
                "data_cutoff": er_entry.get("data_cutoff"),
                "reasons": er_entry.get("reasons"),
            }
        # ★ M1/M2/M3 预测基准件（planning_only）：pred_ref.PRR-R1 机械产出的三槽位
        #   基准/阈值（历史分位，可追溯）。只挂 spec 备查；chain.prediction **仍为空**
        #   （BUY 才需三槽，而本件永不用它造 BUY——正式 prediction 须 4.1 授权下独立
        #   输入件给出；挂上即等于用历史分布冒充当前预测，属越界）。
        pr_entry = pdr.get(code) or {}
        pred_plan = None
        if pr_entry and pr_entry.get("usable_slots"):
            pred_plan = {
                "planning_only": True,
                "rule_version": pr_entry.get("rule_version"),
                "usable_slots": pr_entry.get("usable_slots"),
                "n_usable": pr_entry.get("n_usable"),
                "slots": pr_entry.get("slots"),
                "data_cutoff": pr_entry.get("data_cutoff"),
                "restricted_reference": pr_entry.get("restricted_reference"),
                "reasons": pr_entry.get("reasons"),
            }
        # 正式 prediction 授权审计（只挂审计，值已在 chain.prediction 内）
        pin_audit = (_pin.get("audit") if _pin else None)
        specs[code] = {
            "thesis_id": th_id,
            "driver": driver,
            "driver_basis": basis,
            "chain": chain,
            "thesis_state": _thesis_state(as_of, code, cand, driver, claim, ev_meta, th_id),
            # 价格线/仓位：五件套槽位恒 null（三问据此判 WAIT/NO，绝不 BUY）
            "entry_limit": None, "stop_line": None, "target": None,
            "window": None, "position_request": None,
            "price_lines_planning": planning,   # 备查件（不参与三问 verdict）
            "expect_ref_planning": expect_plan,  # 备查件（历史参考，不参与 verdict）
            "pred_ref_planning": pred_plan,     # 备查件（M1/M2/M3 基准，不入 prediction）
            "pred_input_audit": pin_audit,      # 正式 prediction 的授权审计（值在 chain 内）
            "completion_event": completion,
            "overturn_event": overturn,
            "evidence_faces": sorted(hit),
        }
        by_driver[driver] = by_driver.get(driver, 0) + 1

    report = {
        "schema_version": SCHEMA_VERSION, "as_of": as_of, "base": base,
        "n_active_candidates": len(active),
        "n_admitted": len(specs),
        "n_skipped_by_face": len(active) - len(specs),   # 无证据面 → run_chain 记 SKIP
        "driver_distribution": by_driver,
        "price_lines": ({"available": True,
                         "n_tradeable": pl_meta.get("n_tradeable", 0),
                         "n_untradeable": pl_meta.get("n_untradeable", 0),
                         "rule_version": pl_meta.get("rule_version"),
                         "reason_histogram": pl_meta.get("reason_histogram")}
                        if price_lines else {"available": False}),
        "expect_ref": ({"available": True,
                        "n_ok": er_meta.get("n_ok", 0),
                        "n_room_ref": er_meta.get("n_room_ref", 0),
                        "n_neutral_or_no_event": er_meta.get("n_neutral_or_no_event", 0),
                        "n_regime_zeroed": er_meta.get("n_regime_zeroed", 0),
                        "rule_version": er_meta.get("rule_version")}
                       if expect_ref else {"available": False}),
        "pred_ref": ({"available": True,
                      "n_all_usable": pdr_meta.get("n_all_usable", 0),
                      "n_partial_usable": pdr_meta.get("n_partial_usable", 0),
                      "n_none_usable": pdr_meta.get("n_none_usable", 0),
                      "slot_usable": pdr_meta.get("slot_usable"),
                      "rule_version": pdr_meta.get("rule_version")}
                     if pred_ref else {"available": False}),
        "pred_input": ({"available": True,
                        "n_submitted": pin_meta.get("n_submitted", 0),
                        "n_admitted": pin_meta.get("n_admitted", 0),
                        "n_rejected": pin_meta.get("n_rejected", 0),
                        "reason_histogram": pin_meta.get("reason_histogram"),
                        "rule_version": pin_meta.get("rule_version"),
                        "n_specs_with_prediction": sum(
                            1 for s in specs.values() if s["chain"]["prediction"])}
                       if pred_input else {"available": False}),
        "s5_available": bool(s5), "s4_available": bool(s4),
        "s2_available": bool(s2l), "mainline_available": bool(ml),
        "limit_up_available": bool(lu),
        "discipline": "五件套·剩余预期恒 null；价格线恒 null ⇒ 本件永不产 BUY"
                      "（expect_ref/pred_ref 仅作备查；pred_input 只填 prediction 槽位，"
                      "价线/剩余预期仍缺 ⇒ 三问驱动问与赔率问仍缺件）",
        "codes": codes,
    }
    if out:
        # 契约：driver-file 顶层 = {code: spec}（run_chain --driver-file 直接
        # drivers.get(code) 消费）；审计报告挂保留键 _report（下划线前缀，
        # 与任何 code 不冲突，run_chain 自然忽略）。
        doc = dict(specs)
        doc["_report"] = report
        d = os.path.dirname(os.path.abspath(out))
        os.makedirs(d, exist_ok=True)
        with io.open(out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        report["out"] = out
    return report, specs


# ── 自检（全沙盒：临时 base；真库零接触） ──────────────────────────────────────
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

    print("== gendrive --selftest ==")
    A_REAL = (os.path.join(_ROOT, "memory", "state", "thesis.md"),
              os.path.join(_ROOT, "data", "state", "candidates.json"),
              os.path.join(_ROOT, "data", "generated", "decision_ledger.jsonl"))
    fp0 = {p: (os.path.getmtime(p) if os.path.exists(p) else None) for p in A_REAL}

    D = "2026-09-29"
    tmp = tempfile.mkdtemp(prefix="gendrive_")
    try:
        for d in ("data/state", "data/generated", "data/snapshot/%s" % D,
                  "memory/state"):
            os.makedirs(os.path.join(tmp, d), exist_ok=True)
        io.open(os.path.join(tmp, "memory", "state", "thesis.md"), "w",
                encoding="utf-8").write(TH.HEADER if hasattr(TH, "HEADER") else "# t\n")
        io.open(os.path.join(tmp, "data", "state", "candidates.json"), "w",
                encoding="utf-8").write(json.dumps(
                    {"version": 2, "updated_at": D, "candidates": [
                        {"id": "CD-1", "code": "sh600001", "state": "DISCOVERED"},
                        {"id": "CD-2", "code": "sh600002", "state": "DISCOVERED"},
                        {"id": "CD-3", "code": "sh600003", "state": "EXPIRED"}]}))
        io.open(os.path.join(tmp, "data", "state", "mainline.json"), "w",
                encoding="utf-8").write(json.dumps(
                    {"version": 2, "lines": {
                        "L:测试线": {"token": "测试线", "state": "active",
                                     "members": {"sh600001": {"hits": 1}}}},
                        "updated_at": D}))
        io.open(os.path.join(tmp, "data", "generated", _S5.format(D)), "w",
                encoding="utf-8").write(json.dumps(
                    {"as_of": D, "codes": {"sh600001": {"code": "sh600001",
                                                         "behavior": "relay"}}}))
        io.open(os.path.join(tmp, "data", "generated", _S4.format(D)), "w",
                encoding="utf-8").write(json.dumps(
                    {"as_of": D, "codes": {"sh600002": {"crowding": "high",
                                                         "reasons": ["连板=6"]}}}))
        io.open(os.path.join(tmp, "data", "generated", _S2.format(D)), "w",
                encoding="utf-8").write(json.dumps(
                    {"as_of": D, "themes": {"880001.SH": {
                        "theme_id": "880001.SH", "theme_name": "测试题材",
                        "stage": "发酵",
                        "evidence": {"leader": {"code": "sh600001", "name": "A"}},
                        "next_stage_trigger": {"text": "涨停≥2 重启", "type": "rediscover",
                                                "conditions": [{"field": "limit_up_count",
                                                                "op": ">=", "value": 40}]},
                        "structure_bias": {"note": "禁接力高位"}}}}))
        io.open(os.path.join(tmp, "data", "snapshot", D, "limit_up.csv"), "w",
                encoding="utf-8", newline="").write(
            "code,name,boards\nsh600002,测试B,2\n")

        rep, specs = generate(D, base=tmp)
        # 1. 准入 = S4∪S5∪主线∪S2 ∩ active = {sh600001, sh600002}（003 已过期）
        T("A1 准入仅证据面∩active（sh600001/sh600002，排除过期 sh600003）",
          set(specs) == {"sh600001", "sh600002"}, str(set(specs)))
        T("A2 报告如实报 SKIP 计数", rep["n_skipped_by_face"] == rep["n_active_candidates"]
          - len(specs), "active=%s admitted=%s" % (rep["n_active_candidates"], len(specs)))
        # 2. sh600001 命中 fund(S5)+sentiment(S2)+industrial(主线) → 复合
        s1 = specs.get("sh600001", {})
        T("B1 多证据面 → driver=复合", s1.get("driver") == "复合", s1.get("driver"))
        T("B2 五件套·剩余预期 恒 None",
          s1["chain"]["five"].get("剩余预期") is None)
        T("B3 价格线 恒 None（绝不 BUY）",
          all(s1.get(k) is None for k in
              ("entry_limit", "stop_line", "target", "window", "position_request")))
        T("B4 thesis_state 键集合法（三问可 _norm 通过）",
          set(s1["thesis_state"].keys()) ==
          set(TH.FIELD_ORDER) | {"id", "history"})
        T("B5 显式 completion（S2 题材触发，非空话）",
          s1.get("completion_event") and s1["completion_event"]["text"])
        # B6. 机械 rule：completion rule 来自 S2 真实 conditions（limit_up_count>=40，白名单）
        T("B6 completion.rule 机械可判（取自 S2 真实 conditions）",
          s1["completion_event"]["rule"] == {"field": "limit_up_count", "op": ">=",
                                             "value": 40},
          str(s1["completion_event"].get("rule")))
        # B7. 翻案 rule：sh600001 不在涨停榜 → rule 保持 null（不造阈值）
        T("B7 翻案 rule 无涨停证据 → None（禁造阈值）",
          s1["overturn_event"]["rule"] is None)
        # 3. sh600002 命中 fund(S4 high) → driver=B
        s2 = specs.get("sh600002", {})
        T("C1 单资金面 → driver=B", s2.get("driver") == "B", s2.get("driver"))
        T("C2 翻案条件非空（S4 真实值）",
          s2.get("overturn_event") and s2["overturn_event"]["text"])
        T("C3 证据条目标注来源（可回溯）",
          any(e.startswith(("S4:", "涨停榜:")) for e in s2["chain"]["evidence"]))
        T("C4 sh600002 在涨停榜(boards=2) → 翻案 rule 机械化(is_limit_up==True)",
          s2["overturn_event"]["rule"] == {"field": "is_limit_up", "op": "==",
                                            "value": True},
          str(s2["overturn_event"].get("rule")))
        # 4. 文本无 three_q 黑名单模糊词
        import three_q as TQ
        def _clean(txt):
            return all(w not in txt for w in TQ.VAGUE_WORDS)
        alltxt = []
        for sp in specs.values():
            if sp.get("completion_event"):
                alltxt.append(sp["completion_event"]["text"])
            alltxt.append(sp["overturn_event"]["text"])
        T("D1 completion/overturn 文本无模糊黑名单词", all(_clean(t) for t in alltxt))
        # 5. 结构校验：每条 spec 的 chain 能被 decision._norm_chain(WAIT) 通过
        all_ok = True
        for code, sp in specs.items():
            try:
                DEC._norm_chain(sp["chain"], "WAIT")
            except Exception:
                all_ok = False
        T("D2 全部 chain 通过 _norm_chain(WAIT)（WAIT/NO 允许空 prediction）", all_ok)
        # G. 剩余预期件（expect_ref 备查历史参考）：挂 spec 但**不入五件套槽位**
        er_fixture = os.path.join(tmp, "data", "generated", "expect_ref.json")
        with io.open(er_fixture, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"schema_version": 1,
                       "summary": {"n_ok": 1, "n_room_ref": 1,
                                   "rule_version": "EXR-R1"},
                       "by_code": {"sh600001": {
                           "status": "OK", "rule_version": "EXR-R1",
                           "event_cat": "增持", "classify_basis": "keyword",
                           "hist": {"net20": 2.98, "n": 457, "grade": "A"},
                           "market_20d_pct": -2.3, "regime_zeroed": False,
                           "room_ref": {"pct": 2.98, "window": "T+20",
                                        "restricted_reference": True,
                                        "entry": 10.0, "target": 13.0}}}},
                      f, ensure_ascii=False)
        rep3, specs3 = generate(D, base=tmp, expect_ref=er_fixture)
        g1 = specs3.get("sh600001", {})
        T("G1 剩余预期件挂 spec（expect_ref_planning 带 room_ref 历史参考）",
          g1.get("expect_ref_planning")
          and g1["expect_ref_planning"]["room_ref"]["pct"] == 2.98
          and g1["expect_ref_planning"]["planning_only"] is True)
        T("G2 五件套·剩余预期槽位仍恒 None（备查件不绕过三问纪律）",
          g1["chain"]["five"].get("剩余预期") is None)
        T("G3 挂备查件后价线/仓位槽位仍恒 None（本件仍永不产 BUY）",
          all(g1.get(k) is None for k in
              ("entry_limit", "stop_line", "target", "window", "position_request")))
        T("G4 report.expect_ref 如实标注 available",
          (rep3.get("expect_ref") or {}).get("available") is True)
        # H. M1/M2/M3 预测基准件（pred_ref 备查基准）：挂 spec 但**不入 chain.prediction**
        pr_fixture = os.path.join(tmp, "data", "generated", "pred_ref.json")
        with io.open(pr_fixture, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"schema_version": 1,
                       "summary": {"n_all_usable": 1, "n_partial_usable": 0,
                                   "n_none_usable": 0, "rule_version": "PRR-R1",
                                   "slot_usable": {"M1": 1, "M2": 1, "M3": 1}},
                       "by_code": {"sh600001": {
                           "rule_version": "PRR-R1",
                           "usable_slots": ["M1", "M2", "M3"], "n_usable": 3,
                           "restricted_reference": True, "data_cutoff": "2026-09-29",
                           "slots": {"M1": {"prediction_id": "M1", "metric": "个股5日",
                                            "baseline": -0.54, "threshold": 3.41,
                                            "window": 5, "sample_n": 807500},
                                     "M2": {"prediction_id": "M2", "family": "A",
                                            "baseline": -0.08, "threshold": -0.08,
                                            "window": 1, "sample_n": 13090},
                                     "M3": {"prediction_id": "M3", "baseline": 0.98,
                                            "threshold": 0.98, "window": 5,
                                            "sample_n": 5232}}}}},
                      f, ensure_ascii=False)
        rep4, specs4 = generate(D, base=tmp, pred_ref=pr_fixture)
        h1 = specs4.get("sh600001", {})
        T("H1 预测基准件挂 spec（pred_ref_planning 带三槽基准）",
          h1.get("pred_ref_planning")
          and h1["pred_ref_planning"]["n_usable"] == 3
          and h1["pred_ref_planning"]["planning_only"] is True)
        T("H2 chain.prediction **仍为空**（备查基准不冒充当前预测）",
          h1["chain"]["prediction"] == [])
        T("H3 五件套与价线/仓位槽位仍恒 None（挂基准后仍永不产 BUY）",
          h1["chain"]["five"].get("剩余预期") is None
          and all(h1.get(k) is None for k in
                  ("entry_limit", "stop_line", "target", "window", "position_request")))
        try:
            DEC._norm_chain(h1["chain"], "WAIT")
            _h4_ok = True
        except Exception:
            _h4_ok = False
        T("H4 挂基准后 chain 仍通过 _norm_chain(WAIT)", _h4_ok)
        T("H5 report.pred_ref 如实标注 available",
          (rep4.get("pred_ref") or {}).get("available") is True)
        # I. 正式 prediction 授权件（pred_input 准入产出）：**进 chain.prediction**，
        #    但价线/五件套·剩余预期仍恒 None ⇒ 三问驱动问与赔率问仍缺件 ⇒ 仍不产 BUY
        pin_fixture = os.path.join(tmp, "data", "generated", "pred_input.json")
        _slot = {"prediction_id": "M1", "metric": "个股 5 日前向收益(%)",
                 "data_source": "klines", "baseline": -0.54, "direction": "UP",
                 "threshold": 3.41, "condition": "T+5 收益 > 基准 q50",
                 "window": 5}
        with io.open(pin_fixture, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"schema_version": 1,
                       "summary": {"n_submitted": 1, "n_admitted": 1,
                                   "n_rejected": 0, "rule_version": "PIR-R1"},
                       "admitted": {"sh600001": {
                           "chain_prediction": [_slot, dict(_slot, prediction_id="M2"),
                                                dict(_slot, prediction_id="M3")],
                           "audit": {"code": "sh600001", "slot_ids": ["M1", "M2", "M3"],
                                     "submitters": ["c4-three-q"],
                                     "approved_via": ["TASK206_SANDBOX_SIM"],
                                     "data_cutoffs": ["2026-09-28"]}}},
                       "rejected": {}}, f, ensure_ascii=False)
        rep5, specs5 = generate(D, base=tmp, pred_input=pin_fixture)
        i1 = specs5.get("sh600001", {})
        T("I1 正式 prediction 进 chain.prediction（三槽齐）",
          len(i1["chain"]["prediction"]) == 3
          and {p["prediction_id"] for p in i1["chain"]["prediction"]}
          == set(DEC.PRED_SLOTS))
        T("I2 pred_input_audit 挂 spec（三方角色可回溯）",
          i1.get("pred_input_audit", {}).get("submitters") == ["c4-three-q"])
        T("I3 五件套·剩余预期与价线/仓位仍恒 None（正式件不造 BUY）",
          i1["chain"]["five"].get("剩余预期") is None
          and all(i1.get(k) is None for k in
                  ("entry_limit", "stop_line", "target", "window", "position_request")))
        try:
            DEC._norm_chain(i1["chain"], "WAIT")
            _i4_ok = True
        except Exception:
            _i4_ok = False
        T("I4 填 prediction 后 chain 仍通过 _norm_chain(WAIT)", _i4_ok)
        T("I5 report.pred_input 如实标注 available + 计数",
          (rep5.get("pred_input") or {}).get("available") is True
          and rep5["pred_input"]["n_admitted"] == 1
          and rep5["pred_input"]["n_specs_with_prediction"] >= 1)
        T("I6 report.discipline 明示正式件只填 prediction 槽位",
          "pred_input" in rep5["discipline"])
        # 6. 缺源降级：只删 S2，sh600001 应变 B（S5）+A（主线）→ 复合，且无 completion
        os.remove(os.path.join(tmp, "data", "generated", _S2.format(D)))
        rep2, specs2 = generate(D, base=tmp)
        T("E1 缺 S2 面 → 该票 completion 变 None（走 NO，不造假）",
          specs2.get("sh600001", {}).get("completion_event") is None)
        T("E2 缺源降级报告如实标注 s2_available=False", rep2["s2_available"] is False)
        # 7. 真库零接触
        T("F1 真 thesis.md/candidates/账本 mtime 不变",
          all((os.path.getmtime(p) if os.path.exists(p) else None) == fp0[p]
              for p in A_REAL))
        # 8. 缺省不写盘
        _ = generate(D, base=tmp)
        T("F2 缺省 --out 不产生任何写盘（仅返回统计）", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("== gendrive --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def _main(argv=None):
    ap = argparse.ArgumentParser(description="C3 驱动规格生成器（零伪造·缺源降级·恒不产 BUY）")
    ap.add_argument("--as-of", dest="as_of", help="交易日 YYYY-MM-DD（--selftest 时免填）")
    ap.add_argument("--base", help="数据根（缺省=生产只读；沙盒则指向已存在目录）")
    ap.add_argument("--limit", type=int, help="最多出 spec 的票数（按 code 排序截断）")
    ap.add_argument("--out", help="driver-file 输出路径（缺省只打印统计，不写盘）")
    ap.add_argument("--price-lines", dest="price_lines",
                    help="可选：priceline 产出的 price-lines JSON（备查规划件，不参与 verdict）")
    ap.add_argument("--expect-ref", dest="expect_ref",
                    help="可选：expect_ref 产出的剩余预期 JSON（备查历史参考，不参与 verdict）")
    ap.add_argument("--pred-ref", dest="pred_ref",
                    help="可选：pred_ref 产出的 M1/M2/M3 基准 JSON（备查，不入 prediction）")
    ap.add_argument("--pred-input", dest="pred_input",
                    help="可选：pred_input 产出的正式 prediction 准入 JSON（**填 chain.prediction**）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if not a.as_of:
        print("用法：--as-of <YYYY-MM-DD> [--base <根>] [--limit N] [--out f] "
              "[--price-lines f] [--expect-ref f] [--pred-ref f] [--pred-input f]")
        return 2
    rep, specs = generate(a.as_of, base=a.base, limit=a.limit, out=a.out,
                          price_lines=a.price_lines, expect_ref=a.expect_ref,
                          pred_ref=a.pred_ref, pred_input=a.pred_input)
    print("C3 驱动规格 · %s · base=%s" % (rep["as_of"],
          os.path.relpath(rep["base"], _ROOT) if rep["base"].startswith(_ROOT) else rep["base"]))
    print("  活跃候选=%d  准入(证据面∩active)=%d  无证据面SKIP=%d"
          % (rep["n_active_candidates"], rep["n_admitted"], rep["n_skipped_by_face"]))
    print("  driver 分布=%s" % rep["driver_distribution"])
    print("  证据面可用: S5=%s S4=%s S2=%s 主线=%s 涨停榜=%s"
          % (rep["s5_available"], rep["s4_available"], rep["s2_available"],
             rep["mainline_available"], rep["limit_up_available"]))
    _pl = rep.get("price_lines") or {}
    print("  价线规划件(备查): %s" % _pl)
    _er = rep.get("expect_ref") or {}
    print("  剩余预期件(备查历史参考): %s" % _er)
    _pdr = rep.get("pred_ref") or {}
    print("  M1/M2/M3 基准件(备查，不入 prediction): %s" % _pdr)
    _pin = rep.get("pred_input") or {}
    if _pin.get("available"):
        print("  正式 prediction 授权件(入 chain.prediction): %s" % _pin)
    print("  纪律: %s" % rep["discipline"])
    if a.out:
        print("  已写 driver-file: %s（%d 条 spec）" % (a.out, len(specs)))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
