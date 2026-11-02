#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""raw_to_settle.py · 手动投放模板 → 全链消费贯穿 E2E（数据链验收批 · v1.0 · 2026-10-09 建）

用途：证明 **投放区数据（ingest.stage_core，source 标记在案）→ promote_snapshot 正式化
      → loader 消费 → C1 env / C2 候选 → C3 驱动链（B 资金 + A 事件 S1 供料双路）
      → C4 三问 → C5 风控 → 逐日结算 → pnl → Case** 同一条链真实贯通。
      这就是「手动投放激活全链真实验收」的可执行预演：真实历史数据按本 E2E 的
      同一入口（stage_core）投放即可复现全程。

★ 数据性质声明（用户裁决 2026-10-09 任务 2-④）：本 E2E 使用**合成沙盒夹具**
  （契约形态、清楚标注"模板示例"），验证的是链路与机械规则，**不是实时行情验收、
  不是策略有效性证明**。人工整理的历史数据导入后属第二类（人工历史），实时采集
  属第三类——三类界限以 meta.source 与投放通道为准，禁止混称。

用法：
  python tools/e2e/raw_to_settle.py            # 运行贯穿 E2E（全沙盒，零真库写入）
  python tools/e2e/raw_to_settle.py --discipline
"""
import ast
import hashlib
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
for _p in ("fs", "analysis", "decision", "risk", "learning", "e2e"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import decision as DEC  # noqa: E402
import three_q as TQ  # noqa: E402
import candidates as CAND  # noqa: E402
import cursor as CUR  # noqa: E402
import thesis as TH  # noqa: E402
import settle as SETTLE  # noqa: E402
import position_gate as PG  # noqa: E402
import case_builder as CB  # noqa: E402
import pnl as PNL  # noqa: E402
import drive  # noqa: E402
import orchestrate as ORCH  # noqa: E402
import s1 as S1MOD  # noqa: E402
import ingest as ING  # noqa: E402
import loader as LD  # noqa: E402
import dayclass as DC  # noqa: E402   ← 交易日历唯一读取接口（禁本文件自写历法）

# v1.4：基座改为**真实连续交易日**。原 10-07（国庆休市）/ 10-10（周六）均非交易日，
# 在日历闸下不再成立；10-10 / 10-11 为周末，故 10-09 → 10-12 是相邻交易日。
D0, D1, D2, D3 = "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"
# 日历落点（与 wakeup 同源同落点；只读，不刷新）
CAL_PATH = os.path.join(DEC.ROOT, "data", "raw", "meta", "trade_cal.csv")
CODE = "sh600000"
EQUITY = 1_000_000
ENV_OK = {"regime": "强势", "signals": {"limit_up_count": 58}, "leg": "EOD",
          "source": "EXPLICIT"}

FIVE_FULL = {k: "%s 内容" % k for k in TQ.FIVE_KEYS}
PRED = [{"prediction_id": k, "metric": "5日超额", "data_source": "snapshot",
         "baseline": "0", "direction": "UP", "threshold": "3",
         "condition": "放量不破5日线", "window": 3} for k in TQ.PRED_SLOTS]
ODDS = dict(entry_limit=10.2, stop_line=9.8, target=11.5, window=5,
            position_request=0.2)


def _selftest_fp(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


def _tree_selftest_fp(roots):
    out = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                p = os.path.join(dirpath, fn)
                out.add((os.path.relpath(p, root), _selftest_fp(p)))
    return out


def _selftest_payload(day, close=10.4, pct=3.18):
    """模板投放包（manual 来源、契约形态；数字为模板示例非真实行情）。"""
    return {
        "daily_quote": [{"code": CODE, "name": "模板票", "open": 10.0,
                         "high": 10.5, "low": 9.9, "close": close, "pct": pct,
                         "vol": 100, "amount": 127000000.0}],
        "limit_up": [{"code": CODE, "name": "模板票", "boards": 2,
                      "first_seal_time": "09:35:12", "seal_amount": 82000000.0,
                      "reason": "回购预案+订单（模板示例）"}],
        "dragon_tiger": [{"code": CODE, "name": "模板票", "seat": "机构席位A",
                          "buy": 90000000.0, "sell": 10000000.0, "net": 80000000.0}],
        "news": [{"news_id": "N-M-%s" % day, "ts": day + " 08:45:00",
                  "source": "巨潮资讯（模板示例）", "title": "回购预案公告（模板示例）",
                  "body": "模板示例正文", "related_codes": [CODE]}],
        "env_metrics": {"limit_up_count": 58, "max_board": 5, "turnover": 1.42e11,
                        "up_down_ratio": 2.8, "fail_rate": 24.0, "index_ma20": 3412.6},
    }


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


def _selftest():
    bad = [0]

    def T(name, cond, extra=""):
        print(("  [ok] " if cond else "  [!!] ") + name
              + ((" :: " + str(extra)) if (extra and not cond) else ""))
        if not cond:
            bad[0] += 1

    print("E2E-c · 投放→正式化→loader→C3(A/B)→C4→C5→结算→Case 贯穿（全沙盒）")
    real_fp = _tree_selftest_fp([os.path.join(DEC.ROOT, "data"),
                        os.path.join(DEC.ROOT, "memory")])
    raw_prod = os.path.join(DEC.ROOT, "data", "raw")
    # v1.5：P0 生产播种（2026-10-13 授权）后生产 data/raw **已有**授权投放日期目录 ⇒
    #       判据不能是"为空"，须是**自检前后同态**（真零污染不变量，与 Z2 全树指纹一致）
    def _prod_dated():
        return sorted(x for x in (os.listdir(raw_prod) if os.path.isdir(raw_prod) else [])
                      if len(x) == 10 and x[4] == "-" and x[7] == "-"
                      and x.replace("-", "").isdigit())
    _PROD_DATED_BEFORE = _prod_dated()
    CAL = DC.Calendar.from_seed_csv(CAL_PATH, source="data/raw/meta/trade_cal.csv",
                                    version="seed")   # v1.4：结算闸必需，缺失即拒
    tmp = tempfile.mkdtemp(prefix="raw2settle_")
    try:
        base = os.path.join(tmp, "gen")
        os.makedirs(base)
        snap_root = os.path.join(LD.roots(base)["snapshot"])
        brain_f = os.path.join(tmp, "brain_state.md")
        open(brain_f, "w", encoding="utf-8", newline="\n").write(
            "# 当前状态（沙盒）\n- **更新时间**：%s\n" % D1)
        cursor_f = os.path.join(tmp, "cursor.json")
        _selftest_init_cursor(cursor_f, D0, True)
        pool_f = os.path.join(tmp, "candidates.json")
        _selftest_init_pool(pool_f, D0)
        CAND.upsert_candidate(CODE, D0, "五路扫描",
                              {"text": "龙虎榜净买入（模板）", "rule": None}, path=pool_f)
        chains_f = os.path.join(tmp, "chains.jsonl")
        cases_f = os.path.join(tmp, "cases.jsonl")
        dq_f = os.path.join(tmp, "demand_queue.jsonl")
        thesis_f = os.path.join(tmp, "thesis.md")
        TH.init_file(thesis_f)

        print("R组 投放把关（ingest.stage_core · 来源标记在案）")
        r1 = ING.stage_core(D1, _selftest_payload(D1), source="manual:模板验收",
                            base=base, data_upto=D1)
        T("R1 D1 整包投放 staged=6（5 数据+meta）", len(r1["staged"]) == 6, r1)
        T("R2 meta source=manual:模板验收", json.load(open(
            os.path.join(LD.roots(base)["raw"], D1, "_core.meta.json"),
            encoding="utf-8"))["source"] == "manual:模板验收")
        try:
            ING.stage_core("2026-10-11", _selftest_payload("2026-10-11"), source="m",
                           base=base, data_upto="2026-10-12")
            T("R3 未来数据拒", False)
        except ING.IngestError as e:
            T("R3 FUTURE_DATA 未来数据拒", "FUTURE_DATA" in str(e))
        try:
            ING.stage_core(D1, _selftest_payload(D1), source="m", base=base)
            T("R4 重复投放拒", False)
        except ING.IngestError as e:
            T("R4 STAGED_EXISTS 重复拒", "STAGED_EXISTS" in str(e))
        r4b = ING.stage_core(D1, _selftest_payload(D1), source="manual:模板验收补数",
                             base=base, replace=True)
        T("R5 replace=True 补数通道留痕",
          any("REPLACED" in w for w in r4b["warnings"]), r4b["warnings"])
        ING.stage_core(D2, _selftest_payload(D2, close=11.0, pct=5.8), source="manual:模板验收",
                       base=base, data_upto=D2)
        ING.stage_core(D0, {"daily_quote": _selftest_payload(D0)["daily_quote"]},
                       source="m", base=base)
        try:
            ING.promote_snapshot("2026-10-10", base=base)
            T("R6 未投放日正式化拒", False)
        except Exception as e:  # noqa: BLE001  （缺目录 → SnapshotError/IngestError 均算拒）
            T("R6 未投放日正式化拒（%s）" % type(e).__name__, True)

        print("P组 正式化与 loader 消费（核心 5 键齐才可快照）")
        pr1 = ING.promote_snapshot(D1, base=base)
        T("P1 D1 正式化 WRITTEN", pr1 == "WRITTEN", pr1)
        pr1b = ING.promote_snapshot(D1, base=base)
        T("P2 重复正式化 EXISTS（幂等）", pr1b == "EXISTS", pr1b)
        try:
            ING.promote_snapshot(D0, base=base)
            T("P3 部分键日拒", False)
        except ING.IngestError as e:
            T("P3 CORE_INCOMPLETE 部分键日拒（D0 只投 1 键）", "CORE_INCOMPLETE" in str(e))
        q = LD.load_quote(D1, CODE, base=base)
        T("P4 load_quote 消费成功（close=10.4）", q is not None and q["close"] == 10.4)
        n = LD.load_news(D1, base=base)
        T("P5 load_news 消费成功", len(n) == 1 and n[0]["news_id"] == "N-M-" + D1)
        snap = LD.load_snapshot(D1, base=base)
        T("P6 load_snapshot 全键可用", snap is not LD.UNAVAILABLE
          and len(snap["dragon_tiger"]) == 1 and snap["limit_up"] is not LD.UNAVAILABLE)

        print("C组 C3 驱动链（B 资金 + A 事件 S1 供料）→ C4 → C5")
        th_b = TH.create_thesis(D1, "B", "资金抢占主线（模板）", code=CODE,
                                candidate_id=None, gap=TH.GAP_DRIVER,
                                path=thesis_f)["id"]
        rv_b = drive.review({"driver": "B", "thesis_id": th_b,
                             "evidence": [{"text": "席位净买入 8000 万",
                                           "source": "SNAPSHOT:dragon_tiger", "ref": CODE}],
                             "inference": ["资金抢占 → 溢价兑现"],
                             "prediction": PRED, "five": FIVE_FULL},
                            as_of=D1, code=CODE, snapshot_root=snap_root,
                            pool_path=pool_f, brain_state_path=brain_f, path=chains_f)
        T("C1 B 链经**投放正式化快照**成立", rv_b["appended"] is True
          and rv_b["driver_effective"] == "B")

        # A 路：S1 评估（真知识库副本入沙盒 + 沙盒 base）→ S1 快照 → drive A 驱动
        kb_dst = os.path.join(base, "data", "knowledge", "s1_event_effects")
        os.makedirs(kb_dst, exist_ok=True)
        for _fn in ("effect_base.json", "taxonomy.json"):
            shutil.copy2(os.path.join(S1MOD.KB_DIR, _fn), os.path.join(kb_dst, _fn))
        shutil.copy2(os.path.join(S1MOD.KB_DIR, "..", "_registry.json"),
                     os.path.join(base, "data", "knowledge", "_registry.json"))
        ev = {"event_id_src": "ann", "title": "回购预案（模板示例）",
              "cols": "回购预案",
              "sources": [{"source_id": "巨潮资讯", "kind": "公告号",
                           "ref": "ANN-2026-1008-001", "ts": D1 + " 08:45:00"}],
              "first_seen_at": D1, "announcement_at": D1,
              "related_codes": [CODE], "digest": "模板示例事件"}
        s1r = S1MOD.evaluate(ev, as_of=D1, base=base)
        T("C2 S1 评估真知识库出统计结论（回购 n=3448）",
          s1r["output"]["type"] == "回购"
          and str(s1r["output"]["net_effect"].get("status") or "OK") == "OK"
          and s1r["output"]["net_effect"].get("sample_size") == 3448
          and s1r["output"]["strength"] in ("强", "中", "弱", "微"),
          {k: s1r["output"].get(k) for k in ("type", "strength")})
        s1_snap_p = os.path.join(LD.roots(base)["generated"], "s_s1_%s.json" % D1)
        th_a = TH.create_thesis(D1, "A", "事件催化：回购（模板）", code=CODE,
                                candidate_id=None, gap=TH.GAP_DRIVER,
                                path=thesis_f)["id"]
        rv_a = drive.review({"driver": "A", "thesis_id": th_a,
                             "evidence": [{"text": "回购事件历史净效应可追溯",
                                           "source": "S1:EVENT_EVAL",
                                           "ref": s1r["event_id"]}],
                             "inference": ["事件催化 → 历史净效应为正"],
                             "prediction": PRED, "five": FIVE_FULL},
                            as_of=D1, code=CODE, snapshot_root=snap_root,
                            pool_path=pool_f, brain_state_path=brain_f,
                            path=chains_f, s1_path=s1_snap_p)
        T("C3 A 链经 S1 快照成立（S1→C3 供料闭环）",
          rv_a["driver_effective"] == "A" and rv_a["driver_reason"] is None)

        d1b = drive.load_chain(code=CODE, as_of=D1, path=chains_f)
        r_buy = ORCH.decide(as_of=D1, code=CODE, thesis_id=th_b, chain=d1b["chain"],
                            sim=True, driver=d1b["driver_effective"], env=ENV_OK,
                            base=base, pool_path=pool_f, thesis_path=thesis_f,
                            snapshot_root=snap_root, **ODDS)
        T("C4 C4 机械 BUY（未预设：规则+输入共同决定）",
          r_buy["status"] == "OK" and r_buy["action"] == "BUY", r_buy.get("action"))
        res = PG.check(0.2, code=CODE, as_of=D1, grade="强势", holdings={},
                       drawdown=0.0, ledger_sim=True, base=base)
        T("C5 C5 PASS", res["verdict"] == "PASS" and res["position_plan"] == 0.2)
        DEC.append_risk_plan(r_buy["decision_id"], res["position_plan"], as_of=D1,
                             gates=res["gates"], note="E2E-c", base=base)

        print("S组 逐日结算 D1→D2→D3（口径 v1.1：D2 成交日开盘成交；D3 越目标价 → 盈亏为正）")
        # D2 补数重投（replace）+ 重新正式化：成交日开盘价 ≤ 限价（open 10.1 ≤ 10.2）
        p2 = _selftest_payload(D2, close=10.8, pct=3.8)
        p2["daily_quote"][0].update(open=10.1, high=10.9, low=9.9)
        ING.stage_core(D2, p2, source="manual:模板验收", base=base, replace=True,
                       data_upto=D2)
        T("S0 D2 补数重投+正式化（成交日）", ING.promote_snapshot(D2, base=base) == "WRITTEN")
        # D3（T+1 首卖日）：高开越目标价 11.5 → 目标离场
        p3 = _selftest_payload(D3, close=11.7, pct=8.3)
        p3["daily_quote"][0].update(open=11.6, high=11.8, low=11.4)
        ING.stage_core(D3, p3, source="manual:模板验收", base=base, replace=True,
                       data_upto=D3)
        T("S0b D3 投放+正式化（目标离场日）", ING.promote_snapshot(D3, base=base) == "WRITTEN")

        def _judge():
            return {"verdicts": {k: {"metric": "5日超额", "data_source": "snapshot",
                                     "verdict": "SUPPORT", "observed": "达标（模板）",
                                     "note": None} for k in TQ.PRED_SLOTS},
                    "attribution_L1L5": {"L1": "OK", "L2": "OK", "L3": "OK",
                                         "L4": "OK", "L5": "OK"},
                    "attribution_三问": {"驱动": "OK", "赔率": "OK", "证伪": "NA"}}
        judgements = {r_buy["decision_id"]: _judge()}
        rep1 = SETTLE.settle(D1, judgements=judgements, equity=EQUITY, base=base,
                             snapshot_root=snap_root,
                             raw_dir=os.path.join(LD.roots(base)["raw"], D1),
                             cursor_path=cursor_f, learning_path=dq_f, calendar=CAL)
        rep2 = SETTLE.settle(D2, judgements=judgements, equity=EQUITY, base=base,
                             snapshot_root=snap_root,
                             raw_dir=os.path.join(LD.roots(base)["raw"], D2),
                             cursor_path=cursor_f, learning_path=dq_f, calendar=CAL)
        rep3 = SETTLE.settle(D3, judgements=judgements, equity=EQUITY, base=base,
                             snapshot_root=snap_root,
                             raw_dir=os.path.join(LD.roots(base)["raw"], D3),
                             cursor_path=cursor_f, learning_path=dq_f, calendar=CAL)
        T("S1 逐日结算全勾（D0→D1→D2→D3）", rep1["settled"] is True
          and rep1["cursor_after"] == D1 and rep2["settled"] is True
          and rep2["cursor_after"] == D2 and rep3["settled"] is True
          and rep3["cursor_after"] == D3,
          (rep1.get("pending_steps"), rep2.get("pending_steps"),
           rep3.get("pending_steps")))
        dec = DEC.load_decision(r_buy["decision_id"], sim=True, base=base)
        T("S2 EXECUTION 已成交（D2 开盘价 10.1 ≤ 限价 10.2 → filled @10.1）",
          dec["actual_position"] == 0.2 and dec["execution_result"] == "filled"
          and dec["avg_price"] == 10.1,
          (dec.get("execution_result"), dec.get("actual_position"), dec.get("avg_price")))
        _p = PNL.read_pnl(r_buy["decision_id"], True, base=base)
        T("S3 pnl 盈亏为正（D3 高开越目标价离场）",
          _p is not None and float(_p.get("pnl_amount") or 0) > 0,
          _p and {k: _p.get(k) for k in ("outcome", "exit_price", "pnl_amount")})
        cb = CB.build(D3, sim=True, base=base, path=cases_f)
        T("S4 Case 生成（同一条业务链）", cb["added"] == 1, cb)

        print("Z组 真库零污染")
        # v1.4：日历落点 `data/raw/meta/trade_cal.csv` 是**只读元数据**，不是投放日期目录。
        # v1.5：判据 = 前后同态（播种后生产已有授权投放目录；`meta/` 为只读日历元数据，非投放）
        _prod_dated_now = _prod_dated()
        T("Z1 生产 data/raw 投放日期目录集**自检前后不变**（before=%d after=%d；投放全在沙盒）"
          % (len(_PROD_DATED_BEFORE), len(_prod_dated_now)),
          _prod_dated_now == _PROD_DATED_BEFORE,
          "%s → %s" % (_PROD_DATED_BEFORE, _prod_dated_now))
        T("Z2 正式 data/ 与 memory/ 前后指纹一致",
          real_fp == _tree_selftest_fp([os.path.join(DEC.ROOT, "data"),
                               os.path.join(DEC.ROOT, "memory")]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("E2E-c 结果：%s" % ("全部通过 ✅" if bad[0] == 0 else "%d 项失败 ❌" % bad[0]))
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
    res = []
    res.append(("① 生产区零写盘（无 open w/a 与 os.replace）",
                "open(" not in prod and "os.replace" not in prod
                and "io.open" not in prod))
    res.append(("② 零 settle/params/position_gate 直写调用",
                "append_risk_plan" not in prod and "set_value" not in prod
                and "PARAMS" not in prod))
    res.append(("③ 合成数据声明在案", "合成沙盒夹具" in src or "模板示例" in src))
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
        return 0 if _selftest() else 1
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
