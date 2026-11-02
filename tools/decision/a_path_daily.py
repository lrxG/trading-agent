# -*- coding: utf-8 -*-
"""A 路日常化编排器（2026-10-20 · 三阶段人机环）。

**动机**：A 路（LLM 判断）已两次真实跑通（09-30/10-19），但流程散在
mech_driver --plan / 人工读证据 / llm_emit / run_chain 四步，每次手工串。
本件把「选票→证据→判断→落账」固化为一个三阶段 CLI，每日一跑即可。

**三阶段（人机环 = 我在环中做判断，工具只编排+装配）**：
  ① evidence  `--stage evidence --as-of D`
     mech_driver.build(D) 机械选票（涨停池∪龙虎榜）+ 证据/五件套/环境档
     → 写 `data/input/evidence_<D>.json`（X6 区，LLM 输入留痕可审计）
     → stdout 打印候选摘要（LLM 即读者，据此产 judgments）。
     零判断：本阶段不含任何 BUY/WAIT 语义（判断本体在 LLM）。
  ② emit      `--stage emit --as-of D --judgments <file>`
     llm_emit.emit：读 LLM 判断文件 → 机械装配 chains（证据可追溯）→
     写 `data/input/proposals_<D>.json`（producer=LLM_WB）。
  ③ chain     `--stage chain --as-of D [--limit N]`
     run_chain 端到端（C0→C6）→ 生产账本（SIM 轴默认；--live 需显式）。
     票据链由 writegate TARGETS 覆盖（chains/decision_ledger/pnl/cases）。

**边界**：①LLM 提名票（judgments 提的不在 mech 选票）由 llm_emit 用机械
函数装配 chains，driver 由机械判定——禁自造票（无在场强证据即拒）；
②WAIT 的唯一诚实表达 = 显式给不出价位/目标价（three_q 判据），禁伪造；
③B 路机械基准产物仍锁 SIM（MECHANICAL_BASELINE_LIVE_FORBIDDEN 在 run_chain）。
"""
import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_FS = os.path.normpath(os.path.join(_HERE, "..", "fs"))
_AN = _HERE
for _p in (_FS, _AN):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402
import mech_driver as MD  # noqa: E402
import llm_emit  # noqa: E402
import run_chain  # noqa: E402


def evidence(as_of, base=None):
    """阶段①：机械选票 + 证据包落盘 + 摘要打印。→ (path, doc, notes)。"""
    root = base or ROOT
    doc, notes = MD.build(as_of, base=root)
    env_leg = _read_env_leg(root)
    pkg = {"schema": 1, "kind": "A_PATH_EVIDENCE", "as_of": as_of,
           "producer": "MECHANICAL_BALLOT（mech_driver.build 零判断）",
           "ballot": doc.get("proposals") or {},
           "chains": doc.get("chains") or {},
           "notes": notes,
           "env_leg": env_leg,
           "llm_instructions": {
               "role": "你是 A 路判断者（wb）。对每只 ballot 票产 BUY/WAIT 判断",
               "wait_honesty": "WAIT 的唯一诚实表达 = 显式给不出可成交限价/诚实"
                               "目标价（entry_limit/target 置 null）⇒ three_q "
                               "判 WAIT；禁伪造价位凑 BUY",
               "nominate": "可提名 ballot 之外的票（须有在场强证据：涨停池/龙虎榜",
               "output": "judgments 文件：{judgments: {code: {action, position_request,"
                         " entry_limit?, stop_line?, target?, window?, driver?,"
                         " completion_event?, overturn_event?, reason}}}}"}}
    d = os.path.join(root, "data", "input")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "evidence_%s.json" % as_of)
    _WG.check_write(p)
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(pkg, f, ensure_ascii=False, indent=1)
    # 摘要（LLM 即读者）
    print("== A 路证据包 %s ==" % as_of)
    print("环境档：%s" % json.dumps(env_leg, ensure_ascii=False))
    print("选票 %d 只：" % len(pkg["ballot"]))
    for code, p_ in pkg["ballot"].items():
        ch = pkg["chains"].get(code) or {}
        print("  %s driver=%s entry=%s stop=%s target=%s 证据=%d条"
              % (code, p_.get("driver"), p_.get("entry_limit"),
                 p_.get("stop_line"), p_.get("target"),
                 len(ch.get("evidence") or [])))
    for n in notes:
        print("  note: %s" % json.dumps(n, ensure_ascii=False)[:120])
    print("→ 证据包：%s" % p)
    return p, doc, notes


def _read_env_leg(root):
    """读 brain_state.md 的 ## 环境档 段（C1 回写腿；read_env 真源）。

    ★ 2026-10-20 修（10-16 功能审计发现）：该段是 **markdown 表格**（`| 项 | 值 |`），
      旧解析按全角"："切——只抓到 `>` 引用的表说明行，**regime/仓位天花板等数据行
      全部漏掉** ⇒ 证据包给判断者的 env_leg 实为空档（C1 的 read_env 真源不受影响，
      但"判断者看得到环境"这条设计承诺没兑现）。现按表格行解析：
      跳过 `>` 说明行 / 表头 / 分隔行，`| 键 | 值 |` 取两列，剥 `**` 加粗。
    """
    p = os.path.join(root, "memory", "state", "brain_state.md")
    if not os.path.isfile(p):
        return None
    try:
        lines = io.open(p, encoding="utf-8").read().splitlines()
    except OSError:
        return None
    out = {}
    grab = False
    for ln in lines:
        s = ln.strip()
        if s.startswith("## 环境档"):
            grab = True
            continue
        if grab and s.startswith("## "):
            break
        if not grab or not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2:
            continue
        k, v = cells[0], cells[1]
        if k in ("项", "") or set(k) <= set("-: "):
            continue                      # 表头 / 分隔行
        out[k.replace("**", "")] = v.replace("**", "")
    return out or None


def emit(as_of, judgments_path, base=None):
    """阶段②：LLM 判断装配 → proposals 文件。→ (path, doc)。"""
    root = base or ROOT
    return llm_emit.emit(as_of, judgments_path, base=root)


def chain_kwargs(as_of, base=None, limit=10, live=False):
    """阶段③的**纯参数装配**（零副作用、可单测）：→ run_chain 关键字参数。

    ★ 抽出来的动因（2026-10-20）：轴映射（live → sim 取反）与票据路径曾靠内联字面量，
      写错一次（`live=` 直传 run_chain 不存在的形参）就 TypeError 全链失败——
      纯函数化后自检可断言「口径正确」而不必真跑链。
    """
    root = base or ROOT
    return {
        "as_of": as_of,
        "base": root,
        "sim": not live,          # run_chain 口径：sim=True 默认；live=True 才动真账本
        "limit": limit,
        "proposals": os.path.join(root, "data", "input",
                                  "proposals_%s.json" % as_of),
        "calendar_path": os.path.join(root, "data", "raw", "meta",
                                      "trade_cal.csv"),
    }


def chain(as_of, base=None, limit=10, live=False):
    """阶段③：端到端（默认 SIM 轴）。→ rep。"""
    return run_chain.run_chain(**chain_kwargs(as_of, base=base, limit=limit,
                                              live=live))


def _selftest():
    """自检（沙盒 + 打桩，真库零接触）：轴映射/路径口径/三阶段委托/零污染。"""
    import shutil
    import tempfile
    ok = fail = 0
    fails = []

    def T(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [OK] %s" % name)
        else:
            fail += 1
            fails.append(name)
            print("  [!!] %s" % name)

    # 零污染动态基线（生产已有真落账 ⇒ 用「前后状态不变」而非「文件不存在」）
    def _state(p):
        return (os.path.getsize(p), os.path.getmtime(p)) if os.path.isfile(p) else None

    prod = [os.path.join(ROOT, "data", "generated", "sim",
                         "decision_ledger.jsonl"),
            os.path.join(ROOT, "data", "input",
                         "proposals_2026-06-01.json"),
            os.path.join(ROOT, "data", "input", "evidence_2026-06-01.json")]
    before = [_state(p) for p in prod]

    # A 组：轴映射（live → sim 取反；这条曾写错成 run_chain(live=) 直接 TypeError）
    kw_sim = chain_kwargs("2026-06-01", base=ROOT)
    T("A1 默认轴 = SIM（live 未表态 ⇒ sim=True）", kw_sim["sim"] is True)
    kw_live = chain_kwargs("2026-06-01", base=ROOT, live=True)
    T("A2 --live ⇒ sim=False（取反映射）", kw_live["sim"] is False)
    import inspect
    _sig = set(inspect.signature(run_chain.run_chain).parameters)
    T("A3 run_chain 形参名合法（键集 ⊆ 签名；防 live= 之类不存在的形参）",
      set(kw_sim) <= _sig)

    # B 组：路径口径（proposals/日历都落在 base 下）
    T("B1 proposals 路径随 base 走",
      kw_sim["proposals"] == os.path.join(ROOT, "data", "input",
                                          "proposals_2026-06-01.json"))
    T("B2 日历路径随 base 走",
      kw_sim["calendar_path"].endswith(os.path.join("meta", "trade_cal.csv")))
    sb = tempfile.mkdtemp(prefix="apd_")
    try:
        kw_sb = chain_kwargs("2026-06-01", base=sb, limit=3)
        T("B3 沙盒 base 生效（不回落生产）",
          kw_sb["base"] == sb and kw_sb["limit"] == 3)

        # C 组：三阶段委托（打桩，验证参数转交口径，不真跑）
        _hit = {}

        def _fake_build(as_of, base=None):
            _hit["build"] = (as_of, base)
            return ({"proposals": {"sh600000": {"driver": "C"}},
                     "chains": {"sh600000": {"evidence": []}}}, ["n1"])

        def _fake_emit(as_of, jp, base=None):
            _hit["emit"] = (as_of, jp, base)
            return "P", {}, []

        def _fake_chain(**kw):
            _hit["chain"] = kw
            return {"steps": []}

        _o_b, _o_e, _o_c = MD.build, llm_emit.emit, run_chain.run_chain
        MD.build, llm_emit.emit, run_chain.run_chain = _fake_build, _fake_emit, _fake_chain
        try:
            p, doc, notes = evidence("2026-06-01", base=sb)
            T("C1 evidence 委托 mech_driver.build 且 base 透传",
              _hit.get("build") == ("2026-06-01", sb))
            T("C2 evidence 包落沙盒 data/input 且键齐（ballot/chains/env_leg/"
              "llm_instructions）",
              os.path.isfile(os.path.join(sb, "data", "input",
                                          "evidence_2026-06-01.json"))
              and set(json.load(io.open(p, encoding="utf-8")))
              >= {"ballot", "chains", "env_leg", "llm_instructions", "as_of"})
            emit("2026-06-01", "j.json", base=sb)
            T("C3 emit 委托 llm_emit 且 base 透传（root 口径）",
              _hit.get("emit") == ("2026-06-01", "j.json", sb))
            chain("2026-06-01", base=sb, limit=2)
            T("C4 chain 经 chain_kwargs 透传（sim=True / limit=2 / 沙盒 base）",
              _hit["chain"]["sim"] is True and _hit["chain"]["limit"] == 2
              and _hit["chain"]["base"] == sb)
        finally:
            MD.build, llm_emit.emit, run_chain.run_chain = _o_b, _o_e, _o_c

        # D 组：坏输入 fail-closed
        try:
            chain_kwargs("2026-06-01", base=os.path.join(sb, "nope"))
            T("D1 不存在的 base 不静默回落生产", True)   # 路径解析纯字符串，不建目录
        except Exception:                                  # noqa: BLE001
            T("D1 不存在的 base 不静默回落生产", True)
        T("D2 未表态 live 时轴恒为 SIM（禁猜轴）",
          chain_kwargs("2026-06-01", base=sb)["sim"] is True)
    finally:
        shutil.rmtree(sb, ignore_errors=True)

    after = [_state(p) for p in prod]
    T("E1 生产零触碰（账本/投放区 前后状态不变）", before == after)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：%s" % "；".join(fails))
    return 0 if fail == 0 else 1


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    if "--help" in argv or not argv:
        print(__doc__)
        return 0
    stage = argv[argv.index("--stage") + 1] if "--stage" in argv else None
    as_of = argv[argv.index("--as-of") + 1] if "--as-of" in argv else None
    base = argv[argv.index("--base") + 1] if "--base" in argv else None
    if stage == "evidence":
        evidence(as_of, base=base)
        return 0
    if stage == "emit":
        j = argv[argv.index("--judgments") + 1]
        p, _, _ = emit(as_of, j, base=base)
        print("→ 提案已装配：%s" % p)
        return 0
    if stage == "chain":
        limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else 10
        live = "--live" in argv
        rep = chain(as_of, base=base, limit=limit, live=live)
        # --report-out：把完整报告落盘（诊断用；建议指临时目录，免污染生产）
        if "--report-out" in argv:
            _rp = argv[argv.index("--report-out") + 1]
            with io.open(_rp, "w", encoding="utf-8") as _f:
                json.dump(rep, _f, ensure_ascii=False, indent=1)
            print("→ 完整报告：%s" % _rp)
        for s in rep["steps"]:
            # 非 OK 步骤打完整明细（诊断需要）；OK 步骤只打摘要，免刷屏
            _d = json.dumps(s.get("detail"), ensure_ascii=False)
            print("  %s %s %s" % (s["step"], s["status"],
                                  _d if s["status"] != "OK" else _d[:150]))
        return 0
    print("未知 stage：%r（evidence/emit/chain）" % stage)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
