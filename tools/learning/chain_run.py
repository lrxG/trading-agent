# -*- coding: utf-8 -*-
"""chain_run.py · C6 学习链独立触发件（2026-10-19 · D2 用户批准路径 b）。

**为什么是「独立触发件」而不是改 settle**：C6 中段（case_builder → obs_scanner →
demand_scanner）「已实现但未接线」；改 `settle._step_learn` 调正式模块 = **动冻结件
（骨架升级）**，与「settle 唯一执行器 / wakeup 唯一入口」纪律冲突。用户 2026-10-19
批准路径 b：**非冻结面新增独立触发件**，由调度层（wakeup / 人工 / 定时）显式调用，
**不与 settle 的 SETTLE_MINIMAL 行并行双写同一账本**（settle 的极简行带
`scanner=SETTLE_MINIMAL` 字段区分，本件写 `scanner=DEMAND_SCANNER`，以字段区分不冒充）。

## 链路（顺序执行 · 任一步失败即停）

```
case_builder.build(as_of)      → data/generated/cases.jsonl（append，幂等）
obs_scanner.scan(as_of)        → data/generated/observations.jsonl（append）
demand_scanner.scan(as_of)     → data/learning/demand_queue.jsonl（append）
```

每步经 `observe.run_observed(actor="learning", stage="C6")` 留痕（**唯一留痕通道**）。

## 边界

- **不改任何冻结件**（settle / wakeup / params / registry 一行不动）；
- **幂等**：同 `as_of` 重跑不产生重复行（各模块自带幂等；本件仅按序调用）；
- **失败停链**：任一步抛错 ⇒ 记 FAIL 留痕 + 返回非零，**不跳步续跑**（防半链状态）；
- **零决策**：本件不产 BUY/WAIT/NO、不动候选池、不下单；
- **轴显式**：`--sim` / `--live` 必选其一（无默认——两轴物理隔离，不表态不许跑）。

## 用法

```
python tools/learning/chain_run.py --as-of 2026-09-30 --sim          # 跑链（SIM）
python tools/learning/chain_run.py --as-of 2026-09-30 --live         # 跑链（LIVE）
python tools/learning/chain_run.py --as-of 2026-09-30 --sim --dry    # 只检查不执行
python tools/learning/chain_run.py --selftest
```
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_FS = os.path.normpath(os.path.join(_HERE, "..", "fs"))
for _p in (_FS, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import observe as OBS          # noqa: E402
import paths as P              # noqa: E402
import case_builder as CB      # noqa: E402
import demand_scanner as DS    # noqa: E402
import obs_scanner as OS       # noqa: E402

ROOT = P.ROOT
ACTOR = "learning"
STAGE = "C6"
STEPS = ("case_builder", "obs_scanner", "demand_scanner")


class ChainError(Exception):
    """REASON_CODE 前缀错误。"""


def _step(name, fn, as_of, sim, base, log_path):
    """单步：经 run_observed 留痕（唯一通道）；失败抛 ChainError 并保留留痕。"""
    r = OBS.run_observed(ACTOR, name, STAGE, fn, as_of=as_of,
                         detail="C6 链步 %s（sim=%s）" % (name, sim),
                         log_path=log_path, raise_on_error=False)
    if not r or r.get("error") is not None:
        raise ChainError("STEP_FAIL:%s → %s" % (name, (r or {}).get("error")))
    return r.get("value")


def run(as_of, *, sim, base=None, dry=False, log_path=None,
        cases_path=None, demand_path=None):
    """按序跑三链步；任一步失败即停（不跳步）。→ {steps: [...], as_of, sim}"""
    if sim is None:
        raise ChainError("AXIS_REQUIRED: 必须显式 --sim 或 --live（两轴物理隔离）")
    b = base or ROOT
    cp = cases_path or os.path.join(b, "data", "generated", "cases.jsonl")
    dp = demand_path or os.path.join(b, "data", "learning", "demand_queue.jsonl")
    out = {"as_of": as_of, "sim": bool(sim), "steps": [], "dry": bool(dry)}
    if dry:
        out["plan"] = list(STEPS)
        return out
    fns = {
        "case_builder": lambda: CB.build(as_of, sim=sim, base=base, path=cp),
        "obs_scanner": lambda: OS.scan(as_of, sim=sim, cases_path=cp),
        "demand_scanner": lambda: DS.scan(as_of, sim=sim, cases_path=cp,
                                          demand_path=dp),
    }
    for name in STEPS:
        v = _step(name, fns[name], as_of, sim, base, log_path)
        out["steps"].append({"step": name, "ok": True,
                             "result": v if isinstance(v, (int, str, dict, list))
                             else str(type(v))})
    return out


def _selftest():
    import io
    import json
    import shutil
    import tempfile
    ok = fail = 0

    def T(name, cond, extra=None):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s%s" % ("✓" if cond else "✗", name,
                               "" if cond else "  ← %s" % (extra,)))

    print("== chain_run --selftest（全沙盒 · 零生产写）==")
    sb = tempfile.mkdtemp(prefix="c6_chain_")
    try:
        for d in ("data/generated", "data/learning", "data/state", "data/generated/sim"):
            os.makedirs(os.path.join(sb, d.replace("/", os.sep)), exist_ok=True)
        lp = os.path.join(sb, "data", "state", "run_log.jsonl")
        T("A1 轴必选：不给 sim/live ⇒ AXIS_REQUIRED",
          _raises(ChainError, lambda: run("2026-09-30", sim=None, base=sb)))
        r = run("2026-09-30", sim=True, base=sb, log_path=lp)
        T("A2 空账本也能跑通三链步（0 case 是合法状态）",
          [s["step"] for s in r["steps"]] == list(STEPS))
        rows = [json.loads(x) for x in io.open(lp, encoding="utf-8")
                if x.strip()] if os.path.isfile(lp) else []
        T("A3 三步各留痕一行（actor=learning · stage=C6）",
          sum(1 for x in rows if x.get("actor") == ACTOR
              and x.get("stage") == STAGE) == 3,
          [x.get("action") for x in rows])
        T("A4 留痕结果非 FAIL（空账本=空转合法）",
          all(x.get("result") in ("DONE", "EMPTY") for x in rows))
        # A5 幂等：再跑一次不新增留痕（各模块自带幂等）——留痕允许新增，但产物行数不变
        def _count(p):
            return sum(1 for _ in io.open(p, encoding="utf-8")) if os.path.isfile(p) else 0
        dp = os.path.join(sb, "data", "learning", "demand_queue.jsonl")
        n1 = _count(dp)
        run("2026-09-30", sim=True, base=sb, log_path=lp)
        T("A5 幂等：demand_queue 行数不变（%d → %d）" % (n1, _count(dp)),
          _count(dp) == n1)
        T("A6 dry 只出计划不执行",
          run("2026-09-30", sim=True, base=sb, dry=True)["plan"] == list(STEPS))
    finally:
        shutil.rmtree(sb, ignore_errors=True)
    print("== chain_run --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def _raises(exc, fn):
    import contextlib
    try:
        fn()
    except exc:
        return True
    except Exception:                                          # noqa: BLE001
        return False
    return False


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    ap = argparse.ArgumentParser(description="C6 学习链独立触发件")
    ap.add_argument("--as-of", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--sim", action="store_true", help="SIM 轴")
    g.add_argument("--live", action="store_true", help="LIVE 轴")
    ap.add_argument("--dry", action="store_true", help="只出计划")
    ap.add_argument("--base", default=None)
    a = ap.parse_args(argv)
    try:
        r = run(a.as_of, sim=a.sim, base=a.base, dry=a.dry)
    except ChainError as e:
        print("⛔ %s" % e, file=sys.stderr)
        return 2
    print("C6 链 %s（sim=%s）：%s" % (r["as_of"], r["sim"],
                                    " · ".join(s["step"] for s in r["steps"])))
    for s in r["steps"]:
        print("  %-16s ok=%s" % (s["step"], s["ok"]))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
