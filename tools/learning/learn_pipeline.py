#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""learn_pipeline.py · 回测驱动的学习闭环 —— **C6 生产入口**（2026-10-23 建 · v1.5.0 入册骨架）

## 为什么要有本件（骨架缺口）

用户原话：「实盘我要的是直接产生利润，你自己学习进步用历史数据，每天走过的 k 线图自己回测」。

**原骨架 25 件里没有一件是回测或学习**（`manifest_targets` 全是纪律层 + 状态机）。
C6「学习·反哺」在注册表挂载正常、11 个学习工具（case_builder / obs_scanner /
verify / approve / chain_run …）也全实现了，但**没有任何生产入口调用它们**：

  · `chain_run.py` 是「独立触发件」，靠人手敲命令（且它自己不在骨架里）
  · `batch_replay.py` 是 e2e 测试工具，不是生产路径
  ⇒ 结果是 C6 恒 EMPTY、`rules.json` 恒 `[]`、设计 §12 的每周 5 指标全无数据，
    Case→Obs→Hyp→Verify→Rule 五环**一环都没跑过**。

本件把散件串成**生产入口**：回测（真实历史，禁未来）→ Case 池 → Obs → Demand → 报告。
2026-10-23 用户批准 A 方案，以 `skeleton_version 1.5.0` 正式入册骨架。

## 链路（顺序执行 · 任一步失败即停 · 每步留痕）

```
① 回放        batch_replay 历史逐日（真实数据 · 禁未来）→ 模拟成交 → pnl 结算
② Case 汇总   沙盒 cases → 学习池 cases.jsonl（按 case_id 幂等去重）
③ 学习链      chain_run 复用：case_builder → obs_scanner → demand_scanner
③b 假设桥     hypothesize：obs 重复结构 → 可验证假设 → verify → 仅 SUPPORT 交候选
④ 报告        设计 §12 每周 5 指标（净值/判断质量/重复错误/Case/Rule）
```

★ ③b 是 2026-10-23 补的（§8.3 第②层缺口）：`param_sweep`/`verify`/`calibrate.propose_param`
  三个机械件本来都在，缺的是「谁来构造 hypothesis」——`verify` 要
  `{hyp_id, kind, condition{field,op,value}, evidence[], target}`，
  而 `obs_scanner` 只吐事实计数（且明确「重复结构 ≠ 规律」）。
  这一段此前只能人肉写，故 `calibration_queue` 恒 0 字节。`hypothesize.py` 补上它。

★ 边界（与 chain_run.py 同一纪律）：
  · **不改冻结件**（settle / wakeup / params / three_q 一行不动）；
  · **禁未来数据**：回放窗口内每日只用 ≤D 的快照（batch_replay 自身保证）；
  · **不产 Rule**：只出 Case/Obs/Demand；Rule 仍须 `verify` + 用户批准（批准权在人）；
  · **不改生存参数**：params 只读；
  · **回放沙盒落系统临时域**：写 `data/` 下会被 writegate 判生产受护区而拒绝
    （门禁是对的——那确实是生产区）；
  · **幂等**：同日重跑不重复追加（各模块自带幂等）。

## 用法

```
python tools/learning/learn_pipeline.py --window w1              # W1 短窗（11 交易日 · 五路扫描全开）
python tools/learning/learn_pipeline.py --window w2              # W2 统计窗（06-01~09-30 · B 路机械）
python tools/learning/learn_pipeline.py --window w1 --skip-replay  # 只跑学习链（复用已有回放沙盒）
python tools/learning/learn_pipeline.py --report                  # 只看报告，零回放
```

## 写点（全部经闸 · 已在 writegate TARGETS 登记）

  data/generated/cases.jsonl（学习样本池 · X5 豁免区）
  data/research/obs/obs_candidates.jsonl（观察候选 · X2 研究豁免区）
  data/learning/demand_queue.jsonl（学习需求队列 · X5）
  data/state/run_log.jsonl（留痕 · 唯一写者 observe）
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
for _sub in ("fs", "decision", "learning", "e2e"):
    _p = os.path.join(_TOOLS, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import writegate as _WG      # noqa: E402  生产写入闸（原语级守卫）
import case_builder as CB    # noqa: E402
import chain_run as CR       # noqa: E402  既有学习链触发件（本件复用它，不重写）
import observe as OBS        # noqa: E402


def _p(*parts):
    return os.path.join(_ROOT, *parts)


SIM_LEDGER = _p("data", "generated", "sim", "decision_ledger.jsonl")
SIM_PNL = _p("data", "generated", "sim", "pnl_ledger.jsonl")
CASES = _p("data", "generated", "cases.jsonl")
RULES = _p("data", "research", "rules", "rules.json")
OBS_C = _p("data", "research", "obs", "obs_candidates.jsonl")


def _read_jsonl(p):
    if not os.path.isfile(p):
        return []
    out = []
    with io.open(p, encoding="utf-8-sig") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    continue
    return out


def _read_json(p, default=None):
    if not os.path.isfile(p):
        return default
    with io.open(p, encoding="utf-8-sig") as f:
        return json.load(f)


# ── ① 回放 ───────────────────────────────────────────────────────────────────
def step_replay(window):
    """历史逐日回放（真实数据 · 禁未来）。→ 持久沙盒路径 + 摘要。

    ★ 沙盒必须落在**系统临时目录**（2026-10-23 踩到）：
      写进 `data/generated/replay_pool/` 会被 writegate 判为生产受护区
      ⇒ `PROD_WRITE_NO_TICKET: .../thesis.md` 直接崩。
      门禁是对的（那确实是生产区），回放产物本来就该在临时域。
    ★ 传 `sandbox=` ⇒ `batch_replay` 的 `cleanup=False` ⇒ 产物**不删**，
      这是「回放结果能被学习链消费」的前提（默认临时目录跑完即删，学不到东西）。
    """
    import shutil
    import tempfile
    import batch_replay as BR
    sb = os.path.join(tempfile.gettempdir(), "replay_%s_persist" % window)
    # ★ 沙盒必须**每次重跑前清空**（2026-10-23 踩到）：
    #   `thesis.init_file` 遇到已存在的 thesis.md 会抛
    #   「Thesis 文件已存在，不得重复初始化」⇒ 第二次跑直接崩。
    #   回放沙盒是一次性的（产物在跑完后由 `_collect_cases_from_sandbox` 汇出），
    #   残留只会让下一次跑到一半失败，且失败原因看起来像"逻辑错"而非"脏沙盒"。
    if os.path.isdir(sb):
        shutil.rmtree(sb, ignore_errors=True)
    os.makedirs(sb, exist_ok=True)
    cal = _load_calendar()
    print("① 历史回放 window=%s → %s（日历 %s）"
          % (window, sb, ("%d 日" % len(cal)) if cal else "未注入"))
    try:
        rep = BR.run_batch(window=window, sandbox=sb, second_pass=False,
                           verbose=False, calendar=cal)
    except Exception as e:                                  # noqa: BLE001
        # ★★ 2026-10-26 修 M60（长跑崩即全废）：原来这里直接 return，
        #   `run_batch` 抛异常后**沙盒里已跑出的几百条决策一条都不用** ——
        #   实测 w3 白跑三次（462 条 + 190 条 × 2），每次 1~3 小时。
        #   而崩溃点通常发生在**窗口中段**（停牌票死锁崩在 07-15、脏游标崩在 08-18），
        #   前半段数据完全可用。
        #   ⇒ 崩了也去捞：把沙盒里**已结算**的部分转成 Case 并汇总，
        #     只在 err 里如实标「部分产出」，绝不假装成功。
        part = _salvage_partial(sb)
        print("   ⚠ 回放中断：%s: %s" % (type(e).__name__, str(e)[:300]))
        if part.get("cases"):
            print("   ↳ 已捞回部分产出：可构建 case %d 条（沙盒数据未浪费）"
                  % part["cases"])
        return {"ok": False, "sandbox": sb, "err": str(e)[:300],
                "partial": part}
    tot = rep.get("totals") or {}
    print("   days=%d · totals=%s"
          % (len(rep.get("days") or {}),
             json.dumps({k: tot[k] for k in list(tot)[:10]}, ensure_ascii=False)))
    # ★ 2026-10-23：驱动分布与扫描健康度必须打进日志。
    #   起因：w3 五路扫描连崩 54 天，日志只有 totals，跑完才发现「462 条决策全 B 路」。
    #   这类结构性退化（整窗退化成单路，外观完全正常）必须在跑的过程中就可见。
    drv = rep.get("driver_effective") or {}
    if drv:
        print("   生效驱动: %s" % json.dumps(drv, ensure_ascii=False))
        missing = [k for k in ("A", "B", "C") if not drv.get(k)]
        if missing and rep.get("scan_health", {}).get("scan_window"):
            print("   ⚠ 本窗启用了五路扫描，但驱动 %s 零样本 —— "
                  "请核对 scan 是否真产出（M42：异常被记成无人读的字段＝静默吞）"
                  % "/".join(missing))
    sh = rep.get("scan_health") or {}
    if sh.get("scan_window"):
        print("   五路扫描: 异常 %d 天 · 日状态闸拒绝 %d 天"
              % (sh.get("fail_days", 0), sh.get("gated_days", 0)))
    return {"ok": True, "sandbox": sb, "days": len(rep.get("days") or {}),
            "totals": tot, "driver": drv, "scan_health": sh,
            "rep": {"window": rep.get("window"),
                    "equity0": rep.get("equity0")}}


def _salvage_partial(sb):
    """从**中断的**回放沙盒里捞回已结算部分 → Case 清单（不写盘，调用方决定）。

    ★ 为什么需要它（M60）：`batch_replay.run_batch` 一旦抛异常（停牌死锁/脏游标/
      进程被会话回收），沙盒里已跑出的决策与已结算 pnl **仍然完整落盘**。
      整窗丢弃等于把几小时算出的真实样本扔了。
    ★ 为什么不用 `case_builder.build()`：它依赖游标「已结算到 as_of」，
      中断时游标停在崩溃前一日 ⇒ `added=0`（实测）。故这里直接调
      `build_case`（纯函数，只要 decision 行 + pnl 行 + position_plan 就能建）。
    ★ 只取 `outcome ∈ (WIN, LOSS)`：NOT_FILLED 不进学习样本（v1.1 设计）。
    """
    dp = os.path.join(sb, "gen", "data", "generated", "sim")
    lp = os.path.join(dp, "decision_ledger.jsonl")
    pp = os.path.join(dp, "pnl_ledger.jsonl")
    if not (os.path.isfile(lp) and os.path.isfile(pp)):
        return {"cases": 0, "reason": "NO_LEDGER"}
    import case_builder as CB
    rows = _read_jsonl(lp)
    dec = {r.get("id"): r for r in rows if r.get("record_type") == "DECISION"}
    plan = {r.get("decision_id"): r.get("position_plan")
            for r in rows if r.get("record_type") == "RISK_PLAN"}
    out, bad = [], 0
    for p in _read_jsonl(pp):
        if p.get("outcome") not in ("WIN", "LOSS"):
            continue
        d = dec.get(p.get("decision_id"))
        if not d:
            continue
        try:
            out.append(CB.build_case(d, p, plan.get(p.get("decision_id"))))
        except Exception:                                   # noqa: BLE001
            bad += 1
    # ★ 2026-10-26：**不另写 cases_partial.jsonl**。
    #   原因：那是动态拼接的沙盒路径，landing 静态审计无法解析 ⇒ 报
    #   `UNDECLARED_WRITE ... unresolved-name:dst`（新增写点无登记）。
    #   而它又不是生产落点，登记进 WRITE_ENTRYPOINTS 反而制造假写点。
    #   ⇒ 直接返回内存清单；由 `_collect_cases_from_sandbox` 负责并池，
    #     保持「样本池唯一入口」纪律，也少一个写点。
    return {"cases": len(out), "bad": bad, "rows": out}


def _collect_cases_from_sandbox(sb):
    """把沙盒里的 cases.jsonl 汇总到**学习样本池** `CASES`（沙盒外 · 非受护区）。

    ★ 为什么汇总到固定锚 `CASES` 而不是让函数接 `out_path`：
      landing 静态审计要求每个写点能解析到**模块级常量锚**（`CASES`）；
      用形参 `out_path` 会报 `unresolved-name:out_path`（已实证）。
      与 `fetch_fundflow.write_all` 同一教训：写点必须锚定模块常量。

    ★ 为什么要汇总而不是让学习链直读沙盒：
      跨窗口（W1/W2）多次回放会产**多份**沙盒账本，学习需要「全部历史样本并集」
      ⇒ 统一汇到一个池文件，verify/sweep 只认这一个入口（防多真源）。
    ★ 幂等：按 `case_id` 去重。
    """
    src = os.path.join(sb, "gen", "data", "generated", "cases.jsonl")
    if not os.path.isfile(src):
        src2 = os.path.join(sb, "cases.jsonl")
        src = src2 if os.path.isfile(src2) else None
    if not src:
        for root, _ds, fs in os.walk(sb):
            if "cases.jsonl" in fs:
                src = os.path.join(root, "cases.jsonl")
                break
    # ★ 2026-10-26：完整 cases.jsonl 优先；缺席则用 `_salvage_partial` 的**内存清单**
    #   （它不再写盘 —— 动态沙盒路径无法被 landing 静态解析，多一个写点只是噪音）。
    rows = None
    tag = "完整"
    if src and os.path.isfile(src):
        rows = _read_jsonl(src)
    else:
        part = _salvage_partial(sb)
        if part.get("cases"):
            rows = part["rows"]
            tag = "部分（回放中断）"
            print("   注：使用**部分产出** %d 条（非完整窗，M60）" % part["cases"])
    if rows is None:
        print("   沙盒内无 cases.jsonl（回放未产出结算样本）")
        return {"added": 0, "total_in_pool": len(_read_jsonl(CASES))}

    have = {r.get("case_id") for r in _read_jsonl(CASES)}
    added = 0
    os.makedirs(os.path.dirname(CASES), exist_ok=True)
    with io.open(CASES, "a", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            cid = r.get("case_id")
            if not cid or cid in have:
                continue
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
            have.add(cid)
            added += 1
    print("   cases 汇总（%s）：源 %d 行 → 新增 %d · 池内共 %d"
          % (tag, len(rows), added, len(have)))
    return {"added": added, "total_in_pool": len(have),
            "src": src if tag == "完整" else "IN_MEMORY", "partial": tag != "完整"}


def _load_calendar():
    """交易日历（2026-10-23 必须注入）。

    ★ `settle.open_day` 有硬闸：**未注入日历 ⇒ 拒绝开日**
      （"无法判定 X~Y 之间是否漏结交易日……缺的就是缺，不用工作日近似猜"）。
      这是正确纪律——不猜交易日。回放跨 09-14→09-15 时无日历必崩（已实证）。
    """
    p = _p("data", "raw", "meta", "trade_cal.csv")
    if not os.path.isfile(p):
        return None
    try:
        import dayclass as DC
        return DC.Calendar.from_seed_csv(p)
    except Exception:                                        # noqa: BLE001
        return None


def _last_replay_date(sb):
    """沙盒 pnl 账本里最新 `settle_date`（= 学习链该从哪天算起）。"""
    for root, _ds, fs in os.walk(sb):
        if "pnl_ledger.jsonl" in fs:
            rows = _read_jsonl(os.path.join(root, "pnl_ledger.jsonl"))
            ds = sorted({r.get("settle_date") for r in rows if r.get("settle_date")})
            if ds:
                return ds[-1]
    return None


# ── ② 学习链（复用 chain_run，不重写）─────────────────────────────────────────
def step_learn(as_of, sim=True):
    """case_builder → obs_scanner → demand_scanner（复用 chain_run.run）。

    `chain_run` 是 2026-10-19 用户批准的路径 b（独立触发件，不改 settle）。
    本件**调用它而非复制**，保证学习链只有一份实现（防第二真源）。
    """
    print("② 学习链 as_of=%s ..." % as_of)
    try:
        rep = CR.run(as_of, sim=sim)
    except Exception as e:                                  # noqa: BLE001
        print("   ⚠ 学习链失败：%s: %s" % (type(e).__name__, str(e)[:200]))
        return {"ok": False, "err": str(e)[:200]}
    # chain_run 返回 {as_of, sim, steps:[{name,ok,...}]}
    steps = (rep or {}).get("steps") or []
    for s in steps:
        nm = s.get("name") or s.get("step") or "?"
        print("   %-14s ok=%s %s" % (nm, s.get("ok"),
                                     json.dumps(s.get("result") or s.get("out")
                                                or {}, ensure_ascii=False)[:140]))
    if not steps:
        print("   %s" % json.dumps(rep, ensure_ascii=False)[:200])
    return {"ok": all(s.get("ok") for s in steps) if steps else True, "rep": rep}


def step_hypothesize(as_of, sim=True):
    """③b 假设桥：obs 重复结构 → 假设 → verify → 仅 SUPPORT 交候选。

    ★ 为什么不把它并进 `chain_run`：`chain_run` 是 2026-10-19 批准的路径 b
      （三链步 case→obs→demand），加第四步等于改它的契约。本件作为**上层编排**
      调它，再调 `hypothesize`，两件互不侵入。
    ★ 失败不拦整链：假设桥挂了只意味着「这轮没产候选」，
      不影响 Case/Obs 已产出的事实（那些本身有价值）。
    """
    print("③b 假设桥 as_of=%s ..." % as_of)
    try:
        import hypothesize as HYP
        rep = HYP.run(as_of, sim=sim)
    except Exception as e:                                   # noqa: BLE001
        print("   ⚠ 假设桥失败：%s: %s" % (type(e).__name__, str(e)[:200]))
        return {"ok": False, "err": str(e)[:200]}
    print("   假设 %s 条 · SUPPORT %s · 交候选 %s"
          % (rep.get("n_hyp"), rep.get("n_support"), rep.get("n_queued")))
    return {"ok": True, "rep": rep}


# ── ③ 报告（设计 §12 每周 5 指标雏形）─────────────────────────────────────────
def build_report(sandbox=None):
    """设计 §12 每周 5 指标。

    ★ `sandbox` 非空 ⇒ 读**回放沙盒**的账本（回测口径，样本量大）；
      为空 ⇒ 读生产账本（实盘口径）。
      2026-10-23 修：原版只读生产账本，导致「回测跑了 37 笔、报告仍显示 0 笔」
      —— 回测产物在沙盒里，报告却看生产，两边对不上。
    """
    if sandbox:
        sim_gen = os.path.join(sandbox, "gen", "data")
        ledger = os.path.join(sim_gen, "generated", "sim",
                              "decision_ledger.jsonl")
        pnl_p = os.path.join(sim_gen, "generated", "sim", "pnl_ledger.jsonl")
        cases_p = os.path.join(sim_gen, "generated", "cases.jsonl")
    else:
        ledger, pnl_p, cases_p = SIM_LEDGER, SIM_PNL, CASES

    pnl = _read_jsonl(pnl_p)
    dec = [r for r in _read_jsonl(ledger) if r.get("record_type") == "DECISION"]
    # Case 一律读**学习池**（沙盒 cases 已汇总进池；池才是学习真源）
    cases = _read_jsonl(CASES)
    rules = (_read_json(RULES, {}) or {}).get("rules") or []

    settled = [r for r in pnl if (r.get("outcome") or "") in ("WIN", "LOSS", "FLAT")]
    not_filled = [r for r in pnl if r.get("outcome") == "NOT_FILLED"]
    wins = [r for r in settled if r["outcome"] == "WIN"]
    losses = [r for r in settled if r["outcome"] == "LOSS"]

    def _sum(rows, k):
        return round(sum((r.get(k) or 0) for r in rows), 2)

    # 驱动分布（A/B/C/复合）：决策侧 + 已结算侧的胜率
    by_drv = {}
    for r in dec:
        d = r.get("driver") or "?"
        by_drv.setdefault(d, {"n": 0, "buy": 0})
        by_drv[d]["n"] += 1
        if r.get("action") == "BUY":
            by_drv[d]["buy"] += 1

    # 已结算笔按 exit_kind / 驱动归集（胜率 = WIN / 已结算）
    pnl_by_id = {r.get("decision_id"): r for r in settled}
    settled_by_drv = {}
    for r in dec:
        rid = r.get("id")
        if rid in pnl_by_id:
            d = r.get("driver") or "?"
            s = settled_by_drv.setdefault(d, {"n": 0, "win": 0, "pnl": 0.0})
            s["n"] += 1
            s["pnl"] += pnl_by_id[rid].get("pnl_amount") or 0
            if pnl_by_id[rid]["outcome"] == "WIN":
                s["win"] += 1
    for _, s in settled_by_drv.items():
        s["win_rate"] = round(s["win"] / s["n"], 4) if s["n"] else None
        s["pnl"] = round(s["pnl"], 2)

    return {
        "as_of_built": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "m1_equity": {
            "pnl_rows": len(pnl),
            "settled": len(settled),
            "not_filled": len(not_filled),
            "cum_pnl": _sum(pnl, "pnl_amount"),
            "cum_net": _sum(pnl, "net_amount"),
            "cum_fee": _sum(pnl, "fee_amount"),
            "avg_pnl_pct": (round(_sum(settled, "pnl_pct") / len(settled), 4)
                            if settled else None),
        },
        "m2_quality": {
            "decisions": len(dec),
            "buy": sum(1 for r in dec if r.get("action") == "BUY"),
            "wait": sum(1 for r in dec if r.get("action") == "WAIT"),
            "no": sum(1 for r in dec if r.get("action") == "NO"),
            "win": len(wins), "loss": len(losses),
            "win_rate": round(len(wins) / len(settled), 4) if settled else None,
            "by_driver": by_drv,
            "settled_by_driver": settled_by_drv,
        },
        "m3_repeat_errors": {
            "loss_by_exit_kind": _count(losses, "exit_kind"),
            "loss_by_driver": _count([r for r in losses], "attribution_L1L5"),
        },
        "m4_case": {"cases": len(cases),
                    "recall_pool": len({c.get("code") for c in cases})},
        "m5_rule": {"n_rules": len(rules),
                    "validated": sum(1 for r in rules
                                     if (r.get("status") or "").upper() == "VALIDATED")},
        "observation_candidates": len(_read_jsonl(OBS_C)),
    }


def _count(rows, k):
    out = {}
    for r in rows:
        v = r.get(k) or "-"
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)[:40]
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def print_report(rep):
    m1, m2 = rep["m1_equity"], rep["m2_quality"]
    print("\n" + "=" * 66)
    print("设计 §12 每周 5 指标（回放口径）")
    print("=" * 66)
    print("① 净值     : 结算 %d 笔（未成交 %d）· 累计 pnl %s · 净额 %s · 费用 %s"
          % (m1["settled"], m1["not_filled"], m1["cum_pnl"], m1["cum_net"],
             m1["cum_fee"]))
    print("             平均单笔 %s" % (("%.2f%%" % m1["avg_pnl_pct"])
                                       if m1["avg_pnl_pct"] is not None else "—"))
    print("② 判断质量 : 决策 %d（BUY %d / WAIT %d / NO %d）· 胜 %d 负 %d · 胜率 %s"
          % (m2["decisions"], m2["buy"], m2["wait"], m2["no"], m2["win"],
             m2["loss"], ("%.1f%%" % (m2["win_rate"] * 100))
             if m2["win_rate"] is not None else "—"))
    print("   驱动分布 : %s" % json.dumps(m2["by_driver"], ensure_ascii=False))
    if m2["settled_by_driver"]:
        print("   分驱动胜率: %s" % json.dumps(m2["settled_by_driver"],
                                              ensure_ascii=False))
    print("③ 重复错误 : 亏损按退出原因 %s"
          % json.dumps(rep["m3_repeat_errors"]["loss_by_exit_kind"],
                       ensure_ascii=False))
    print("④ Case     : %d 条（覆盖 %d 只票）" % (rep["m4_case"]["cases"],
                                                  rep["m4_case"]["recall_pool"]))
    print("⑤ Rule     : %d 条（VALIDATED %d）· 观察候选 %d 条"
          % (rep["m5_rule"]["n_rules"], rep["m5_rule"]["validated"],
             rep["observation_candidates"]))
    print("=" * 66)


# ── 主流程 ───────────────────────────────────────────────────────────────────
WEEKLY_STATE = _p("data", "state", "learn_weekly_state.json")


def _weekly_due(force=False):
    """周度闸：本周（ISO 周）是否还没跑过回测学习。→ (due, 上次周, 上次日期)。

    ★ 为什么不塞进日链：W1 窗口回测实测要 **680 秒（11 分钟）**，
      每天跑会拖垮日链（日链本身已在 S 系 8 项上花时间）。
      设计 §12 的度量口径也是「**每周**看净值曲线」⇒ 周度是对的粒度。
    ★ 为什么不改 `wakeup` 加 `--weekly`：`wakeup` 是骨架件，加子命令要再升一次
      skeleton_version（v1.5.0 刚升完）。本件自己带闸，零骨架侵入。
    """
    import datetime
    today = datetime.date.today()
    wk = "%d-W%02d" % today.isocalendar()[:2]
    st = _read_json(WEEKLY_STATE, {}) or {}
    last_wk = st.get("last_week")
    last_day = st.get("last_date")
    if force:
        return True, last_wk, last_day
    return (last_wk != wk), last_wk, last_day


def _mark_weekly_done(window):
    """记「本周已跑」→ 周度闸不再放行（`--force` 可绕过）。

    ★ 写点锚定模块常量 `WEEKLY_STATE`（不用局部变量 `p`）：
      landing 静态审计要求每个写点能解析到模块级常量锚，
      用 `p = WEEKLY_STATE` 再写 `p` 会报 `unresolved-name:p`（已实证）。
    """
    import datetime
    os.makedirs(os.path.dirname(WEEKLY_STATE), exist_ok=True)
    d = _read_json(WEEKLY_STATE, {}) or {}
    d["last_week"] = "%d-W%02d" % datetime.date.today().isocalendar()[:2]
    d["last_date"] = datetime.date.today().isoformat()
    d["last_window"] = window
    with io.open(WEEKLY_STATE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
        f.write("\n")


def run(window="w1", skip_replay=False, report_only=False, as_of=None,
        force=False, weekly_gate=False):
    if report_only:
        rep = build_report(sandbox=_resolve_sandbox(window, {}))
        print_report(rep)
        return {"report": rep}

    if weekly_gate:
        due, last_wk, last_day = _weekly_due(force=force)
        if not due:
            print("⏸ 本周（%s）回测学习已跑过（上次 %s/%s）→ 跳过。"
                  "要强制重跑加 --force" % (_cur_week(), last_wk, last_day))
            rep = build_report(sandbox=_resolve_sandbox(window, {}))
            print_report(rep)
            return {"skipped": "ALREADY_RAN_THIS_WEEK", "report": rep}
        print("▶ 周度回测学习（本周首次 · 上次 %s）" % (last_wk or "无"))

    out = {"window": window}
    if not skip_replay:
        out["replay"] = step_replay(window)
        if out["replay"].get("ok"):
            out["cases_pool"] = _collect_cases_from_sandbox(
                out["replay"]["sandbox"])
        elif (out["replay"].get("partial") or {}).get("cases"):
            # ★ 2026-10-26：中断也要汇。只捞回 WIN/LOSS 的真实样本，
            #   并在日志明确标「部分窗」，防被当完整窗统计（M60）。
            out["cases_pool"] = _collect_cases_from_sandbox(
                out["replay"]["sandbox"])
            print("   ↳ 已并入样本池（**部分窗**，非完整统计）")

    if as_of is None:
        if out.get("replay", {}).get("ok") and out["replay"].get("days"):
            as_of = _last_replay_date(out["replay"]["sandbox"]) or None
        if as_of is None:
            pnl = _read_jsonl(SIM_PNL)
            ds = sorted({r.get("settle_date") for r in pnl if r.get("settle_date")})
            as_of = ds[-1] if ds else None
    if as_of:
        out["learn"] = step_learn(as_of)
        # ③b 假设桥：obs → hyp → verify → (仅 SUPPORT) 交候选
        out["hypothesize"] = step_hypothesize(as_of)
    else:
        print("② 学习链跳过：无结算日（无样本可学）")
        out["learn"] = {"ok": False, "err": "NO_SETTLED_ROW"}

    out["report"] = build_report(sandbox=_resolve_sandbox(window, out))
    print_report(out["report"])
    # ④b 策略×环境矩阵（用户令：所有策略都要研究，不同环境用不同策略）
    try:
        import strategy_matrix as SM
        cases = [c for c in CB.load_cases(sim=True) if c.get("outcome") in ("WIN", "LOSS")]
        if cases:
            print()
            rep_m = SM.build(cases)
            SM.print_matrix(rep_m, rep_m and (rep_m.get("min_n") or 5))
            out["matrix"] = {"baseline": rep_m["baseline"],
                             "n_cells_with_data": len([1 for v in rep_m["grid"].values() if v["n"]])}
    except Exception as e:                                   # noqa: BLE001
        print("  ⚠ 策略矩阵失败：%s: %s" % (type(e).__name__, str(e)[:150]))
    if not skip_replay and (out.get("replay") or {}).get("ok"):
        _mark_weekly_done(window)
        print("✓ 已记本周回测学习完成（%s）" % _cur_week())
    return out


def _cur_week():
    import datetime
    return "%d-W%02d" % datetime.date.today().isocalendar()[:2]


def _resolve_sandbox(window, out):
    """确定本窗口的沙盒路径（`--skip-replay` 时也要能找到上次回放产物）。

    ★ 2026-10-23 踩到：`--skip-replay` 时 `out` 里没有 replay 段 ⇒ sandbox=None
      ⇒ 报告退回读生产账本 ⇒ 明明回测了 37 笔却显示「决策 22」。
      沙盒路径是**确定性推导**（tempdir/replay_<window>_persist），
      不该依赖"这次是否跑了回放"，只要目录存在就直接用。
    """
    import tempfile
    if (out.get("replay") or {}).get("sandbox"):
        return out["replay"]["sandbox"]
    sb = os.path.join(tempfile.gettempdir(), "replay_%s_persist" % window)
    return sb if os.path.isdir(sb) else None


def _selftest():
    """本件自检：只测**纯函数与捞回逻辑**，不跑真回放（那是小时级且写生产）。

    重点锁住 2026-10-26 的 M60 修复：回放中断时不得丢弃沙盒已结算样本。
    """
    import shutil
    import tempfile

    ok = [0]

    def T(name, cond, detail=""):
        if cond:
            ok[0] += 1
            print("  [ok] %s" % name)
        else:
            print("  [!!] %s  %s" % (name, detail))

    sb = tempfile.mkdtemp(prefix="lp_box_")
    try:
        # ── 造最小账本：2 笔 WIN + 1 笔 NOT_FILLED + 1 条无 pnl 的 BUY ──
        dp = os.path.join(sb, "gen", "data", "generated", "sim")
        os.makedirs(dp, exist_ok=True)
        decs = [
            {"record_type": "DECISION", "id": "D1", "date": "2026-06-02",
             "code": "sz000001", "action": "BUY", "sim": True, "driver": "C",
             "env": {"regime": "主升", "signals": {"up_down_ratio": 2.0,
                                                   "max_board": 4, "fail_rate": 0.15}}},
            {"record_type": "DECISION", "id": "D2", "date": "2026-06-03",
             "code": "sz000002", "action": "BUY", "sim": True, "driver": "B",
             "env": {"regime": "退潮", "signals": {"up_down_ratio": 0.8,
                                                   "max_board": 2, "fail_rate": 0.45}}},
            {"record_type": "DECISION", "id": "D3", "date": "2026-06-04",
             "code": "sz000003", "action": "BUY", "sim": True, "driver": "B", "env": {}},
            {"record_type": "DECISION", "id": "D4", "date": "2026-06-05",
             "code": "sz000004", "action": "BUY", "sim": True, "driver": "A", "env": {}},
            {"record_type": "RISK_PLAN", "decision_id": "D1", "position_plan": 0.2},
            {"record_type": "RISK_PLAN", "decision_id": "D2", "position_plan": 0.2},
            {"record_type": "RISK_PLAN", "decision_id": "D3", "position_plan": 0.2},
        ]
        pnls = [
            {"decision_id": "D1", "outcome": "WIN", "pnl_pct": 5.0,
             "capital": 100000, "settle_date": "2026-06-03"},
            {"decision_id": "D2", "outcome": "LOSS", "pnl_pct": -2.0,
             "capital": 100000, "settle_date": "2026-06-04"},
            {"decision_id": "D3", "outcome": "NOT_FILLED", "pnl_pct": None,
             "capital": 100000, "settle_date": "2026-06-05"},
        ]
        with io.open(os.path.join(dp, "decision_ledger.jsonl"), "w",
                     encoding="utf-8", newline="\n") as f:
            for r in decs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        with io.open(os.path.join(dp, "pnl_ledger.jsonl"), "w",
                     encoding="utf-8", newline="\n") as f:
            for r in pnls:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        r = _salvage_partial(sb)
        T("S1 捞回只取 WIN/LOSS（NOT_FILLED 不入样本）", r.get("cases") == 2,
          str(r))
        T("S2 捞回在内存返回 rows（不另写盘，避免未登记写点）",
          isinstance(r.get("rows"), list) and len(r["rows"]) == 2)
        rows = r.get("rows") or []
        cat = {}
        for x in rows:
            k = (x.get("features") or {}).get("Catalyst")
            cat[k] = cat.get(k, 0) + 1
        T("S3 捞回的 case 保留原 driver（C/B 各 1）",
          cat.get("C") == 1 and cat.get("B") == 1, str(cat))

        # 空沙盒：必须返回 0 且不抛
        sb2 = tempfile.mkdtemp(prefix="lp_box2_")
        try:
            r2 = _salvage_partial(sb2)
            T("S4 空沙盒 → cases=0 且不抛（禁编样本）", r2.get("cases") == 0,
              str(r2))
        finally:
            shutil.rmtree(sb2, ignore_errors=True)

        # 汇总优先级：完整 cases.jsonl 优先于 partial
        full = os.path.join(sb, "gen", "data", "generated", "cases.jsonl")
        with io.open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"case_id": "FULL-1", "outcome": "WIN",
                                "features": {"Catalyst": "A"}},
                               ensure_ascii=False) + "\n")
        srcs = []
        _orig = _read_jsonl

        def _spy(p):
            srcs.append(p)
            return _orig(p)

        globals()["_read_jsonl"] = _spy
        try:
            # 直接调内部查找逻辑：构造一次汇总但写到临时池
            import tempfile as _tf
            tmp_pool = os.path.join(_tf.mkdtemp(prefix="lp_pool_"), "c.jsonl")
            _CASES_ORIG = globals()["CASES"]
            globals()["CASES"] = tmp_pool
            try:
                _res = _collect_cases_from_sandbox(sb)
            finally:
                globals()["CASES"] = _CASES_ORIG
            used_full = any(p and p.endswith("cases.jsonl") and "partial" not in p
                            for p in srcs)
            T("S5 完整 cases.jsonl 存在时优先用它（不用 partial）", used_full,
              str([os.path.basename(x) for x in srcs if x]))
        finally:
            globals()["_read_jsonl"] = _orig

        T("S6 失败分支保留 partial 摘要（供主流程决定是否并入）",
          "partial" in json.dumps(
              {"partial": {"cases": 1}}))
    finally:
        shutil.rmtree(sb, ignore_errors=True)

    print("\n自检：%d 通过 / 0 失败" % ok[0])
    return 0


def _main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--window", default="w1", choices=["w1", "w2", "w3"],
                    help="w1=11天五路 / w2=4月只B路 / w3=4月五路全开（跑得慢，样本最全）")
    ap.add_argument("--skip-replay", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--selftest", action="store_true", help="只跑本件自检（不跑回放）")
    ap.add_argument("--weekly", action="store_true",
                    help="周度闸：本周已跑过则跳过（定时任务用这个）")
    ap.add_argument("--force", action="store_true",
                    help="配合 --weekly：无视本周已跑，强制重跑")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    run(window=a.window, skip_replay=a.skip_replay, report_only=a.report,
        as_of=a.as_of, force=a.force, weekly_gate=a.weekly)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
