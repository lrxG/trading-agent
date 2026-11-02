#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chain_c34.py · C3→C4→C5→结算→Case 贯穿 E2E（C3/C4/E2E-b 批 · v1.0 · 2026-10-09 建）

用途：证明 C3 驱动链 → C4 三问 → C5 风控 → 结算 → 归因 Case **同一条模拟业务链**
      真实贯通。全部写入临时沙盒（tempfile），正式游标/正式账本/真库前后指纹一致。

★ 四场景矩阵（用户裁决 2026-10-09；动作全部由规则+输入共同决定，**不预设**）：
  S1 有效 BUY：C3 证据链（B 资金抢占）→ C4 机械 BUY → C5 PASS → append_risk_plan
     → 结算成交（entry 落在当日 [low,high]）→ pnl → case_builder 出 Case。
  S2 无效 BUY：五件套缺件且无补全事件 → C4 机械 NO（缺项可指认）→ 零执行记录。
  S3 C5 拒绝：C4 BUY 但熔断档回撤触发 → C5 REJECT/position_plan=0 → RISK_PLAN=0
     → 结算判 unfilled（绝不成交）→ 无虚假盈亏。
  S4 WAIT 零执行：等补全事件（锚回填候选池、内容可验证）；与 S2 的 NO 一样无
     EXECUTION，且结算成交步确实运行过（fills 含 S1/S3、不含 S2/S4）。

★ 限制如实声明：真库无任何交易日快照（data/snapshot 空）——本 E2E 使用**契约形态
  沙盒夹具**（清楚标记），只证数据链贯通与机械规则正确，不证真实行情、不证策略盈利。
  C1 参与方式 = env 显式传参（source 如实标注）；C1/C2 模块级接线属骨架契约统一批。

用法：
  python tools/e2e/chain_c34.py            # 运行贯穿 E2E（全沙盒，零真库写入）
  python tools/e2e/chain_c34.py --discipline
"""
import ast
import hashlib
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
for _p in ("fs", "analysis", "decision", "risk", "learning"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import decision as DEC  # noqa: E402
import three_q as TQ  # noqa: E402
import candidates as CAND  # noqa: E402
import cursor as CUR  # noqa: E402
import snapshot as SNAP  # noqa: E402  (供 info_set_hash 语义；E2E 快照经参数注入)
import thesis as TH  # noqa: E402
import settle as SETTLE  # noqa: E402
import params as PARAMS  # noqa: E402
import position_gate as PG  # noqa: E402
import case_builder as CB  # noqa: E402
import pnl as PNL  # noqa: E402
import drive  # noqa: E402
import orchestrate as ORCH  # noqa: E402
# 注：不引用 wakeup 的私有夹具（_init_pool/_init_cursor）——跨模块引用私有名会被
# landing 审计永久视为生产入口（防静默屏蔽设计），故本文件自带同形夹具副本（下方）。
import drive  # noqa: E402
import orchestrate as ORCH  # noqa: E402

D0, D1 = "2026-10-07", "2026-10-08"
CODE = "sh600000"
EQUITY = 1_000_000
ENV = {"regime": None, "signals": {}, "leg": "EOD", "source": "UNAVAILABLE"}
ENV_OK = {"regime": "强势", "signals": {"limit_up_count": 60}, "leg": "EOD",
          "source": "EXPLICIT"}  # C1 档位显式注入（regime_ladder 既有枚举）

FIVE_FULL = {k: "%s 内容" % k for k in TQ.FIVE_KEYS}
FIVE_PART = dict(FIVE_FULL, 剩余预期=None)
PRED = [{"prediction_id": k, "metric": "5日超额", "data_source": "snapshot",
         "baseline": "0", "direction": "UP", "threshold": "3",
         "condition": "放量不破5日线", "window": 3} for k in TQ.PRED_SLOTS]
CHAIN_B = {"evidence": ["席位净买入 8000 万（龙虎榜）", "涨停题材归属可查"],
           "inference": ["资金抢占 → 溢价兑现"], "prediction": PRED, "five": FIVE_FULL}
CHAIN_PART = {"evidence": ["涨停题材归属可查"], "inference": ["扩散中"],
              "prediction": [], "five": FIVE_PART}
ODDS = dict(entry_limit=10.2, stop_line=9.8, target=11.5, window=5,
            position_request=0.2)


def _selftest_md5(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


def _selftest_tree_fp(roots):
    out = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                p = os.path.join(dirpath, fn)
                out.add((os.path.relpath(p, root), _selftest_md5(p)))
    return out


def _selftest_mk_snapshot(root, date, quote=None):
    """契约形态沙盒夹具快照（同 snapshot.CONTRACT；非真实行情）。

    经 `SNAP.write_snapshot` 唯一写入口构造（A9 起自动生成可复现的
    `_admission.json` sidecar；缺 sidecar 的快照在 settle 步1 会被拒）。
    `quote` = daily_quote 记录列表（dict）；None = 夹具默认单日行情。"""
    import snapshot as _SNAP
    qrecs = quote if quote is not None else [{"code": CODE, "name": "夹具X",
                                              "open": 10.0, "high": 10.5, "low": 9.9,
                                              "close": 10.4, "pct": 3.18, "vol": 100,
                                              "amount": 127000000.0}]
    payload = _SNAP._normalize({
        "daily_quote": qrecs,
        "limit_up": [{"code": CODE, "name": "夹具X", "boards": 2,
                      "first_seal_time": "09:35:12", "seal_amount": 82000000.0,
                      "reason": "夹具题材"}],
        "dragon_tiger": [{"code": CODE, "name": "夹具X", "seat": "夹具席位",
                          "buy": 90000000.0, "sell": 10000000.0,
                          "net": 80000000.0}],
        "news": [{"news_id": "N-1", "ts": date + " 09:12:00", "source": "夹具时报",
                  "title": "t", "body": "b", "related_codes": [CODE]}],
        "env_metrics": {"limit_up_count": 60, "max_board": 6, "turnover": 1.5e11,
                        "up_down_ratio": 3.2, "fail_rate": 21.0,
                        "index_ma20": 3450.5},
    })
    _SNAP.write_snapshot(date, payload, root=root, force=True)


def _selftest_init_pool(path, date):
    """0.5 候选池初始化夹具（与 wakeup._init_pool 同形；本文件私有，不外借）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": CAND.SCHEMA_VERSION, "updated_at": date,
                   "candidates": []}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def _selftest_init_cursor(path, date, done=True):
    """0.1 游标初始化夹具（与 wakeup._init_cursor 同形；本文件私有）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": CUR.SCHEMA_VERSION, "date": date,
                   "checklist": {s: bool(done) for s in CUR.STEPS}},
                  f, ensure_ascii=False)
    return path


def _selftest():
    bad = [0]

    def T(name, cond, extra=""):
        print(("  [ok] " if cond else "  [!!] ") + name
              + ((" :: " + str(extra)) if (extra and not cond) else ""))
        if not cond:
            bad[0] += 1

    print("E2E-b · C3→C4→C5→结算→Case 贯穿（全沙盒 · 夹具快照 · 零真库写入）")
    real_fp = _selftest_tree_fp([os.path.join(DEC.ROOT, "data"),
                        os.path.join(DEC.ROOT, "memory")])
    tmp = tempfile.mkdtemp(prefix="chain34_")
    try:
        base = os.path.join(tmp, "gen")
        os.makedirs(base)
        snap_root = os.path.join(tmp, "snapshot")
        _selftest_mk_snapshot(snap_root, D1)
        # 窗口内快照（口径 v1.1：d2=成交日 open 10.0 ≤ 限价 → 成交；d3 高开越目标价
        # 11.5 → 目标离场；D4..D7 常规）——供 pnl 结算
        _base = __import__("datetime").date(2026, 10, 8)
        _d2 = (_base + __import__("datetime").timedelta(days=2)).isoformat()
        for i in range(1, 7):
            _d = (_base + __import__("datetime").timedelta(days=i)).isoformat()
            if _d == _d2:
                # d2 = 成交日（open 11.6 > 限价? 否——open 11.6 越 11.5 目标 → 目标离场）
                _selftest_mk_snapshot(snap_root, _d,
                                      [{"code": CODE, "name": "夹具X", "open": 11.6,
                                        "high": 11.8, "low": 11.4, "close": 11.7,
                                        "pct": 12.5, "vol": 100,
                                        "amount": 180000000.0}])
            else:
                _selftest_mk_snapshot(snap_root, _d)
        raw_root = os.path.join(tmp, "raw")
        import snapshot as _SNAP_RAW
        for _d in sorted(os.listdir(snap_root)):
            _selftest_mk_snapshot(raw_root, _d,
                                  _SNAP_RAW.load_snapshot(_d, root=snap_root)["daily_quote"])
        brain_f = os.path.join(tmp, "brain_state.md")
        open(brain_f, "w", encoding="utf-8", newline="\n").write(
            "# 当前状态（沙盒夹具）\n- **更新时间**：%s\n" % D1)
        cursor_f = os.path.join(tmp, "cursor.json")
        _selftest_init_cursor(cursor_f, D0, True)
        # settle v1.3 日历轴：夹具日历（合成 10-07~10-10 全交易日，只供日期轴判定）
        import dayclass as _DC
        cal_f = os.path.join(tmp, "trade_cal.csv")
        open(cal_f, "w", encoding="utf-8", newline="\n").write(
            "date,is_trade\n2026-10-07,1\n2026-10-08,1\n2026-10-09,1\n2026-10-10,1\n")
        cal = _DC.Calendar.from_seed_csv(cal_f)
        pool_f = os.path.join(tmp, "candidates.json")
        _selftest_init_pool(pool_f, D0)
        CAND.upsert_candidate(CODE, D0, "五路扫描",
                              {"text": "龙虎榜净买入（夹具）", "rule": None}, path=pool_f)
        raw_dir = os.path.join(tmp, "raw", D1)
        chains_f = os.path.join(tmp, "chains.jsonl")
        cases_f = os.path.join(tmp, "cases.jsonl")
        dq_f = os.path.join(tmp, "demand_queue.jsonl")
        thesis_f = os.path.join(tmp, "thesis.md")
        TH.init_file(thesis_f)

        def new_th(ttype, claim):
            return TH.create_thesis(D1, ttype, claim, code=CODE, candidate_id=None,
                                    gap=TH.GAP_DRIVER, path=thesis_f)["id"]

        def decide_kw(th_id, chain, **kw):
            base_kw = dict(as_of=D1, code=CODE, thesis_id=th_id, chain=chain,
                           sim=True, thesis_state=TH.read_thesis(th_id, thesis_f),
                           env=ENV, base=base, pool_path=pool_f,
                           thesis_path=thesis_f, snapshot_root=snap_root)
            base_kw.update(kw)
            return base_kw

        th1 = new_th("B", "资金抢占主线")
        th2 = new_th("C", "情绪观察（证据不足）")
        th3 = new_th("C", "情绪扩散（等晋级）")
        th4 = new_th("B", "资金主线（熔断期）")

        print("S1 有效 BUY：C3 链 → C4 BUY → C5 PASS → （结算在后）")
        rv = drive.review({"driver": "B", "thesis_id": th1,
                           "evidence": [{"text": "席位净买入 8000 万",
                                         "source": "SNAPSHOT:dragon_tiger", "ref": CODE},
                                        {"text": "涨停题材归属可查",
                                         "source": "SNAPSHOT:limit_up", "ref": CODE}],
                           "inference": ["资金抢占 → 溢价兑现"],
                           "prediction": PRED, "five": FIVE_FULL},
                          as_of=D1, code=CODE, snapshot_root=snap_root,
                          pool_path=pool_f, brain_state_path=brain_f, path=chains_f)
        T("S1.0 C3 链落盘且 thesis_id 锁定", rv["appended"] is True
          and rv["thesis_id"] == th1 and rv["driver_effective"] == "B")
        d1 = drive.load_chain(code=CODE, as_of=D1, path=chains_f)
        T("S1.0b C4 从盘上取回同一份链", d1 is not None
          and d1["data_fingerprint"] == rv["data_fingerprint"])
        r1 = ORCH.decide(**decide_kw(th1, d1["chain"], driver=d1["driver_effective"],
                                     env=ENV_OK, **ODDS))
        T("S1.1 C4 机械 BUY（规则+输入共同决定，未预设）",
          r1["status"] == "OK" and r1["action"] == "BUY")
        res1 = PG.check(0.2, code=CODE, as_of=D1, grade="强势", holdings={},
                        drawdown=0.0, ledger_sim=True, base=base)
        T("S1.2 C5 PASS 且裁决仓=请求仓",
          res1["verdict"] == "PASS" and res1["position_plan"] == 0.2, res1["gates"])
        DEC.append_risk_plan(r1["decision_id"], res1["position_plan"], as_of=D1,
                             gates=res1["gates"], note="E2E S1", base=base)

        print("S2 无效 BUY：三问机械拒（明确缺项）")
        r2 = ORCH.decide(**decide_kw(th2, CHAIN_PART, driver="C", env=ENV_OK,
                                     overturn_event={"text": "题材证伪：龙头断板且炸板率失控",
                                                     "rule": None}))
        T("S2.1 五件套缺件且无补全事件 → 机械 NO", r2["action"] == "NO")
        v2 = TQ.answer(D1, CODE, th2, CHAIN_PART, driver="C",
                       thesis_state=TH.read_thesis(th2, thesis_f), env=ENV_OK,
                       thesis_path=thesis_f,
                       overturn_event={"text": "题材证伪：龙头断板且炸板率失控",
                                       "rule": None})
        T("S2.2 缺项原因可指认（gaps 含五件套缺口）",
          any("五件套" in g or "FIVE" in g.upper() for g in v2.get("gaps", [])),
          v2.get("gaps"))

        print("S3 C5 拒绝：熔断档 → REJECT（结算在后判 unfilled）")
        r3 = ORCH.decide(**decide_kw(th4, CHAIN_B, driver="B", env=ENV_OK, **ODDS))
        T("S3.1 C4 仍机械 BUY（C4 不越权成 C5）", r3["action"] == "BUY")
        l2 = float(PARAMS.get_value("circuit_breaker_drawdown_l2"))
        res3 = PG.check(0.2, code=CODE, as_of=D1, grade="强势", holdings={},
                        drawdown=l2 + 0.01, ledger_sim=True, base=base)
        T("S3.2 C5 REJECT（熔断 binding，plan=0）",
          res3["verdict"] == "REJECT" and res3["position_plan"] == 0.0, res3["gates"])
        DEC.append_risk_plan(r3["decision_id"], 0.0, as_of=D1, gates=res3["gates"],
                             note="E2E S3 熔断拒绝", base=base)

        print("S4 WAIT：等补全事件，锚回填候选池")
        r4 = ORCH.decide(**decide_kw(th3, CHAIN_PART, driver="C", env=ENV_OK,
                                     completion_event={"text": "龙头晋级 4 板",
                                                       "rule": None}))
        T("S4.1 WAIT 落账且锚回填（next_trigger 可验证）",
          r4["action"] == "WAIT" and r4["anchor"].get("anchored") is True)

        print("SETTLE 逐日结算 D1→D2（窗口型缺口随时间自愈：D1 只能判 NOT_FILLED，D2 目标价离场）")

        def _judge():
            return {"verdicts": {k: {"metric": "5日超额", "data_source": "snapshot",
                                     "verdict": "SUPPORT", "observed": "达标（夹具）",
                                     "note": None} for k in TQ.PRED_SLOTS},
                    "attribution_L1L5": {"L1": "OK", "L2": "OK", "L3": "OK",
                                         "L4": "OK", "L5": "OK"},
                    "attribution_三问": {"驱动": "OK", "赔率": "OK", "证伪": "NA"}}
        judgements = {r1["decision_id"]: _judge(), r3["decision_id"]: _judge()}
        import datetime as _dtmod
        d2 = (_dtmod.date(2026, 10, 8) + _dtmod.timedelta(days=1)).isoformat()
        d3 = (_dtmod.date(2026, 10, 8) + _dtmod.timedelta(days=2)).isoformat()
        rep = SETTLE.settle(D1, judgements=judgements, equity=EQUITY, base=base,
                            snapshot_root=snap_root, raw_dir=os.path.join(raw_root, D1),
                            cursor_path=cursor_f, learning_path=dq_f, calendar=cal)
        rep2 = SETTLE.settle(d2, judgements=judgements, equity=EQUITY, base=base,
                             snapshot_root=snap_root, raw_dir=os.path.join(raw_root, d2),
                             cursor_path=cursor_f, learning_path=dq_f, calendar=cal)
        rep3 = SETTLE.settle(d3, judgements=judgements, equity=EQUITY, base=base,
                             snapshot_root=snap_root, raw_dir=os.path.join(raw_root, d3),
                             cursor_path=cursor_f, learning_path=dq_f, calendar=cal)
        T("ST.1 逐日结算全勾（游标 D0 → D1 → D2 → D3；口径 v1.1：成交在 D2、出场在 D3）",
          rep["settled"] is True and rep["cursor_after"] == D1
          and rep2["settled"] is True and rep2["cursor_after"] == d2
          and rep3["settled"] is True and rep3["cursor_after"] == d3,
          (rep.get("pending_steps"), rep2.get("pending_steps"), rep3.get("pending_steps")))
        dec1 = DEC.load_decision(r1["decision_id"], sim=True, base=base)
        T("S1.3 EXECUTION 已成交（plan 0.2 全额 · 成交价 = D2 开盘价）",
          dec1["actual_position"] == 0.2 and dec1["execution_result"] == "filled"
          and dec1["avg_price"] == 10.0,
          (dec1.get("execution_result"), dec1.get("actual_position"), dec1.get("avg_price")))
        _p1 = PNL.read_pnl(r1["decision_id"], True, base=base)
        _p3 = PNL.read_pnl(r3["decision_id"], True, base=base)
        T("S1.4 pnl 账本有该笔盈亏（D3 高开越目标价离场 · 盈亏为正）",
          _p1 is not None and float(_p1.get("pnl_amount") or 0) > 0,
          {"p1": _p1 and {k: _p1.get(k) for k in ("outcome", "exit_price", "pnl_amount")},
           "p3": _p3 and _p3.get("outcome")})
        pnl_rows = [_p1]
        cb = CB.build(d3, sim=True, base=base, path=cases_f)
        T("S1.5 case_builder 出 Case（同一条业务链）", cb["added"] == 1, cb)
        dec3 = DEC.load_decision(r3["decision_id"], sim=True, base=base)
        T("S3.3 EXECUTION = unfilled（C5 否决绝对阻断成交）",
          dec3["actual_position"] == 0.0 and dec3["execution_result"] == "unfilled",
          (dec3.get("execution_result"), dec3.get("note")))
        T("S3.4 无虚假盈亏（该笔 pnl=NOT_FILLED · pnl_amount=0）",
          (PNL.read_pnl(r3["decision_id"], True, base=base) or {}).get("pnl_amount", 0) == 0)
        for rid, act in ((r2["decision_id"], "NO"), (r4["decision_id"], "WAIT")):
            dv = DEC.load_decision(rid, sim=True, base=base)
            T("S4.2 %s 决策无 EXECUTION（actual_position=None）" % act,
              dv.get("actual_position") is None)
        fills = []
        for st in rep["steps"] + rep2["steps"] + rep3["steps"]:
            if st.get("step") == "成交记录":
                fills.extend(st.get("fills") or [])
        fill_ids = {f.get("decision_id") for f in fills}
        T("S4.3 结算成交步确实运行（含 S1/S3 两笔 BUY）",
          {r1["decision_id"], r3["decision_id"]} <= fill_ids, fill_ids)
        T("S4.4 WAIT/NO 不在执行列表",
          r2["decision_id"] not in fill_ids and r4["decision_id"] not in fill_ids)

        print("收尾：真库指纹一致")
        T("Z1 正式 data/ 与 memory/ 前后指纹完全一致",
          real_fp == _selftest_tree_fp([os.path.join(DEC.ROOT, "data"),
                               os.path.join(DEC.ROOT, "memory")]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("E2E 结果：%s" % ("全部通过 ✅" if bad[0] == 0 else "%d 项失败 ❌" % bad[0]))
    return bad[0] == 0


def _discipline(verbose=True):
    import astwin as ASTW
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    lines = src.splitlines(True)
    drop = set()
    for lo, hi, name in ASTW.func_table(ast.parse(src)):
        if name.startswith(("_selftest", "_discipline", "_main")):
            drop.update(range(lo - 1, hi))
    prod = "".join(l for i, l in enumerate(lines) if i not in drop)
    res = [("① 生产区零写点（打开-写/落盘调用只存在于 _selftest 夹具区）",
            all(tok not in prod for tok in ("open(", "json.dump", "os.makedirs",
                                            ".write("))),
           ("② 不触正式游标（无 advance_cursor/mark_step 生产调用）",
            "advance_cursor" not in prod and "mark_step" not in prod),
           ("③ astwin 可枚举函数表", bool(ASTW.func_table(ast.parse(src))))]
    ok = True
    for name, passed in res:
        if verbose:
            print(("  [ok] " if passed else "  [!!] ") + name)
        ok = ok and passed
    print("纪律结果：%s" % ("通过 ✅" if ok else "失败 ❌"))
    return ok


def _main(argv):
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    return 0 if _selftest() else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
