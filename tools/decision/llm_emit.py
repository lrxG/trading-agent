# -*- coding: utf-8 -*-
"""llm_emit.py · A 路判断生成器（LLM 提案装配 + 写盘 · 2026-10-20）。

职责分界（同 brief.py 范式——**代码不含 LLM 客户端**）：
  判断本体在 LLM（wb / 运行环境），本件只做：
  ① 读 LLM 判断文件（--judgments，位于非受护区）
  ② 装配 chains（证据层 = mech_driver.build 机械装配，全部可追溯）+ 基准对照
  ③ 按 judgments 覆写 proposals 层 → 写 data/input/proposals_<as_of>.json

producer = "LLM_WB" ≠ MECHANICAL_BASELINE ⇒ run_chain 的
MECHANICAL_BASELINE_LIVE_FORBIDDEN 锁对此文件不触发（A 路 = LIVE 合法判断源）。

纪律（与 c4-three-q skill 同源）：
  · 只允许对 mech_driver.build 选出的票做判断（禁自造票 / 禁引缺席证据）；
  · LLM 判断缺席的票**不保留机械判断冒充**（如实丢弃并记录）；
  · 价位沿用机械基准四件（entry/stop/target/window，RR=2.0 基准）——保证与
    B 路可比；LLM 的增量判断在 action / position_request / completion_event。

用法：
  python tools/decision/llm_emit.py --as-of 2026-09-30 --judgments <json路径>
judgments 文件格式：
  {"note": "...", "judgments": {"<code>": {"action": "BUY|WAIT",
     "position_request": 0.1, "driver": "C",
     "completion_event": {...}|null, "overturn_event": {...}|null,
     "reason": "一句话"}}}
"""
import argparse
import io
import json
import os
import sys
import time

_D = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
for _p in ("fs", "decision", "analysis"):
    sys.path.insert(0, os.path.join(_D, "tools", _p))

import mech_driver as MD            # noqa: E402
import writegate as _WG             # noqa: E402

PRODUCER = "LLM_WB"


def _today():
    return time.strftime("%Y-%m-%d")


def _apply(p, j, today):
    """把 LLM 判断覆写到基准 proposal 上（action / 仓位 / 事件 / 价位 / basis）。"""
    act = (j.get("action") or "").upper()
    if act not in ("BUY", "WAIT"):
        return None, "action 非法（%s）——LLM 无 NO 提案权（NO 由机械经 driver/事件分流）" % act
    p["action_proposed"] = act
    p["position_request"] = round(float(j.get("position_request") or 0), 4)
    p["completion_event"] = j.get("completion_event")
    p["overturn_event"] = j.get("overturn_event")
    if j.get("driver"):
        p["driver"] = j["driver"]
    # ★ 价位四件 optional 覆写（LLM 诚实「答不出」的唯一通道）：
    #   three_q 的 WAIT 判据 = 三问某问真的答不出（赔率问 gap = 四件任一 None）。
    #   LLM 若判断「这个位置/流动性给不出可成交限价/诚实目标价」，
    #   显式置 null ⇒ 机械判 WAIT（带 completion_event），而非伪造价位凑 BUY。
    #   未提供的键 = 沿用机械基准四件（保证与 B 路可比）。
    for k in ("entry_limit", "stop_line", "target", "window"):
        if k in j:
            p[k] = j[k]
    basis = list(p.get("basis") or [])
    basis.append({"ref": "C3:CHAIN:mech",
                  "claim": "LLM 判断（wb %s）：%s" % (today, (j.get("reason") or "")[:120])})
    p["basis"] = basis
    return p, None


def emit(as_of, judgments_path, base=None):
    """→ (path, doc, out)。LLM 判断装配 → 写 data/input/proposals_<as_of>.json。"""
    jd = json.load(io.open(judgments_path, encoding="utf-8-sig"))
    jmap = jd.get("judgments")
    if not isinstance(jmap, dict) or not jmap:
        raise ValueError("judgments 文件缺 judgments 映射（LLM 判断不得为空）")
    doc, notes = MD.build(as_of, base=base)      # chains/证据/Thesis（幂等）
    base_props = doc.get("proposals") or {}
    proposals, out = {}, []

    # ── ① mech 选票：按 judgments 覆写（判断层归 LLM）──────────────────────
    for code, base_p in base_props.items():
        j = jmap.get(code)
        if j is None:
            out.append({"code": code, "skipped": "LLM 判断缺席——不保留机械判断冒充"})
            continue
        p, err = _apply(dict(base_p), j, _today())
        if err:
            out.append({"code": code, "skipped": err})
            continue
        proposals[code] = p
        out.append({"code": code, "action": p["action_proposed"],
                    "position_request": p["position_request"],
                    "reason": (j.get("reason") or "")[:120]})

    # ── ② LLM 提名票（judgments 有而 mech 选票无）────────────────────────────
    #   2026-10-20：westock CLI 无涨停池命令 ⇒ 10月数据 reason 恒空 ⇒ mech 的 M2
    #   （sector_same_count）门槛把全部涨停票跳过（选票 0）。A 路选票权归 LLM：
    #   提名票的 chains 仍用**机械函数装配**（证据客观可追溯），driver 由
    #   build_evidence 机械判定（涨停池在场 ⇒ C，与 reason 无关），不经过 mech 的
    #   M2 门槛；M2 预测槽缺失由 judgments 如实处理，不伪造扩散依据。
    import candidates as CAND
    import snapshot as SNAP
    snap = SNAP.load_snapshot(as_of,
                              root=os.path.join(base or MD.ROOT, "data", "snapshot"))
    pool_p = os.path.join(base or MD.ROOT, "data", "state", "candidates.json")
    try:
        with io.open(pool_p, encoding="utf-8-sig") as f:
            pool = (json.load(f) or {}).get("candidates") or []
    except Exception:                                           # noqa: BLE001
        pool = []
    if snap is SNAP.UNAVAILABLE or snap is None:
        for code in jmap:
            if code not in proposals:
                out.append({"code": code, "skipped": "as_of 无快照——提名票无法装配证据链"})
    else:
        for code, j in jmap.items():
            if code in proposals:
                continue
            cand = MD._cand_for(code, pool)
            try:
                ev, facts, driver = MD.build_evidence(code, snap, cand)
            except Exception as e:                               # noqa: BLE001
                out.append({"code": code, "skipped": "提名票无在场证据：%s" % e})
                continue
            if driver is None:
                out.append({"code": code,
                            "skipped": "提名票无强证据（无涨停池/龙虎榜净买入）——机械门槛"})
                continue
            px = MD.prices(facts.get("close"))
            five = MD.five_pieces(driver, facts, px)
            th_id, _ = MD.ensure_thesis(code, cand, driver, facts, as_of, base=base)
            chains = doc.get("chains") or {}
            chains[code] = MD.chain_proposal(code, driver, facts, ev, px, five,
                                             as_of, th_id)
            doc["chains"] = chains
            p, err = _apply(MD.c4_proposal(code, driver, facts, px, as_of, th_id,
                                           cand, snap.get("env_metrics")), j, _today())
            if err:
                out.append({"code": code, "skipped": err})
                continue
            proposals[code] = p
            out.append({"code": code, "nominated": True, "driver": driver,
                        "action": p["action_proposed"],
                        "position_request": p["position_request"],
                        "reason": (j.get("reason") or "")[:120]})
    # （judgments 里非基准选票的 code 已由上方「LLM 提名票」路径处理：
    #   装配成功的进 proposals，无在场证据/无强证据的在该路径 out 记录——
    #   此处不再重复记「禁自造票」，避免同一 code 双记录。）
    doc2 = {"schema_version": 1, "as_of": as_of, "producer": PRODUCER,
            "axis": "SIM",
            "note": (jd.get("note") or
                     "A 路 LLM 判断（证据=机械装配可追溯；判断=wb）"),
            "proposals": proposals, "chains": doc.get("chains") or {},
            "llm_meta": {"judgments_source": os.path.basename(judgments_path),
                         "n_proposed": len(proposals), "notes": out,
                         "base_compare": {c: (b or {}).get("action_proposed")
                                          for c, b in base_props.items()}}}
    dst = os.path.join(base or MD.ROOT, "data", "input",
                       "proposals_%s.json" % as_of)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    _WG.check_write(dst)
    tmp = dst + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc2, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, dst)
    return dst, doc2, out


def _main(argv):
    ap = argparse.ArgumentParser(description="A 路判断生成器（LLM 提案装配+写盘）")
    ap.add_argument("--as-of", dest="as_of", required=True)
    ap.add_argument("--judgments", required=True)
    ap.add_argument("--base", default=None)
    a = ap.parse_args(argv)
    p, doc, out = emit(a.as_of, a.judgments, base=a.base)
    print("已生成 %s" % p.replace("\\", "/"))
    print("proposals=%d chains=%d producer=%s axis=%s"
          % (len(doc["proposals"]), len(doc["chains"]), doc["producer"], doc["axis"]))
    for n in out:
        print("  %s" % json.dumps(n, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
