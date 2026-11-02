# -*- coding: utf-8 -*-
"""E5 持仓诊断（2026-10-13 · 第一阶段补齐）。

职责（registry）：单票诊断「逻辑在不在 / 走不走 / 动作」→ 挂载 C4。
触发：持仓异动 / 盘后。**边界：零 BUY/WAIT/NO、零仓位动作**（no_decision/can_order=false）。

**持仓四类分清（2026-10-12 核准，不得互相冒充）**：
  · 本系统真实/模拟持仓 = C5 `position_gate.derive_holdings` 唯一真源
    （决策账本 EXECUTION 物化，已剔除已结算退出票）。当前为空 ⇒ **空仓无需诊断**
    （契约原文），如实输出空态——禁读旧包手工 positions.md 冒充当前持仓。
  · 分析段（S4 拥挤 / S5 筹码）只对**在仓票**装配；快照缺席 → ABSENT 如实。

**硬风控与分析解释严格分槽**：
  · C5 硬风控段 = exit_gate.check 的机械结论原样引用；当前缺实时价/止损线
    （无盘中源）→ 该段显式 UNAVAILABLE，**不得以收盘近似冒充硬检查**。
  · S4/S5 段标注「分析解释，非风控指令」。

输出 `data/generated/reports/e5_<as_of>.md`（X1；写点复用 premarket._atomic_write
——已登记，零新增写点）。幂等：同输入同字节 ⇒ 跳过写。
"""
import io
import json
import os
import sys
import datetime as _dt

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
_RK = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "risk"))
_DM = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "decision"))
for p in (_FS, _RK, _DM):
    sys.path.insert(0, p)
from paths import ROOT  # noqa: E402
import position_gate as PG      # noqa: E402  C5 硬风控唯一真源
import premarket as E1          # noqa: E402  复用已登记写点 _atomic_write（零新增写点）

S_ID = "e5"


def _load_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def collect(as_of, *, sim=False, base=None):
    """只读装配。持仓=C5 真源；分析段=S4/S5 快照（缺席如实）。"""
    b = base or ROOT
    gen = os.path.join(b, "data", "generated")
    holdings = PG.derive_holdings(sim, base=base)          # {} = 空仓（真源口径）
    d = {"as_of": as_of, "axis": "SIM" if sim else "LIVE",
         "holdings": holdings, "n_positions": len(holdings),
         "status": ("EMPTY" if not holdings else "HOLDINGS"),
         "exit_gate": {"status": "UNAVAILABLE",
                       "reason": "缺实时价/止损线（无盘中源）——硬检查不得以收盘近似冒充；"
                                 "数据就绪后经 exit_gate.check 机械执行"},
         }
    s5 = _load_json(os.path.join(gen, "s_s5_%s.json" % as_of))
    codes = s5.get("codes") or {} if isinstance(s5, dict) else {}
    s4 = _load_json(os.path.join(gen, "s_s4_%s.json" % as_of))
    s4c = s4.get("codes") or {} if isinstance(s4, dict) else {}
    per = {}
    for code in holdings:
        ent = codes.get(code)
        if ent is None and isinstance(codes, dict):
            ent = next((v for v in codes.values()
                        if isinstance(v, dict) and v.get("code") == code), None)
        c4 = s4c.get(code) if isinstance(s4c, dict) else None
        per[code] = {
            "position_ratio": holdings[code],
            "s5_capital": ({"behavior": ent.get("behavior"),
                            "chip_state": ent.get("chip_state")}
                           if isinstance(ent, dict) else
                           {"status": "ABSENT（S5 结构化集未覆盖该票——缺就是缺）"}),
            "s4_crowding": ({"crowding": c4.get("crowding"),
                             "evidence": c4.get("evidence")}
                            if isinstance(c4, dict) else
                            {"status": "ABSENT（S4 快照未覆盖该票）"}),
            "thesis": "UNAVAILABLE（持仓 Thesis 载体未接——登记缺口，不编逻辑在不在）",
        }
    d["per_position"] = per
    d["degradations"] = []
    if not holdings:
        d["degradations"].append("空仓（C5 真源口径 0 在仓）——无单票可诊断（契约：空仓无需诊断）")
    d["degradations"].append("C5 退出闸硬检查 UNAVAILABLE（缺实时价/止损线）")
    d["degradations"].append("持仓 Thesis 载体未接——「逻辑在不在」暂不可机械产出")
    if s5 is None:
        d["degradations"].append("S5 资金快照缺席（分析解释段缺）")
    if s4 is None:
        d["degradations"].append("S4 拥挤快照缺席（分析解释段缺）")
    return d


def render_md(d):
    L = ["# E5 持仓诊断 · %s（%s 轴）" % (d["as_of"], d["axis"]), ""]
    L.append("- 在仓（C5 真源）：%s" % (
        "空仓" if d["status"] == "EMPTY"
        else "；".join("%s=%.2f%%" % (c, r * 100) for c, r in sorted(d["holdings"].items()))))
    L.append("- C5 硬风控（退出闸）：%s（%s）" % (d["exit_gate"]["status"],
                                                d["exit_gate"]["reason"]))
    L.append("")
    if d["status"] != "EMPTY":
        L.append("## 单票（分析解释，非风控指令）")
        for code, v in sorted(d["per_position"].items()):
            L.append("### %s（仓位 %.2f%%）" % (code, v["position_ratio"] * 100))
            L.append("- S5 资金：analysis=%s" % json.dumps(v["s5_capital"],
                                                           ensure_ascii=False))
            L.append("- S4 拥挤：analysis=%s" % json.dumps(v["s4_crowding"],
                                                           ensure_ascii=False))
            L.append("- Thesis：analysis=%s" % v["thesis"])
            L.append("")
    if d["status"] == "EMPTY":
        L.append("## 账本语义声明（空仓态必读）")
        L.append("- 当前账本无记录或账本文件缺席（C5 真源口径 0 在仓）。")
        L.append("- 本报告不能证明真实账户当前持仓为空。")
        L.append("- 不得据此验证真实持仓决策。")
        L.append("")
    L.append("## 降级与缺席（如实）")
    for x in d["degradations"]:
        L.append("- %s" % x)
    L.append("")
    L.append("> 边界：本诊断零 BUY/WAIT/NO、零仓位动作；「走不走」的机械判定 = "
             "C5 退出闸（数据就绪后），「逻辑在不在」终判 = C4（LLM）。")
    return "\n".join(L)


def emit(as_of, *, sim=False, base=None, out_dir=None):
    """装配 → 渲染 → 原子写（幂等：同输入同字节 ⇒ 跳过写）。"""
    import datetime as _dd
    d = collect(as_of, sim=sim, base=base)
    md = render_md(d)
    p = os.path.join(base or ROOT, "data", "generated", "reports",
                     "e5_%s.md" % as_of)
    if os.path.isfile(p) and io.open(p, encoding="utf-8").read() == md:
        return {"created": False, "reason": "EXISTS", "path": p}
    E1._atomic_write(p, md)
    return {"created": True, "path": p,
            "generated_at": _dd.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}


def _selftest():
    import tempfile as _tf
    import shutil as _sh
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== e5 --selftest ==")
    sb = _tf.mkdtemp(prefix="e5_sb_")
    try:
        os.makedirs(os.path.join(sb, "data", "generated"), exist_ok=True)
        d = collect("2026-10-13", sim=True, base=sb)
        T("B1 空仓如实输出（C5 真源口径 EMPTY）", d["status"] == "EMPTY"
          and d["n_positions"] == 0)
        r1 = emit("2026-10-13", sim=True, base=sb)
        t1 = io.open(r1["path"], encoding="utf-8").read()
        T("B2 空仓态报告含「空仓无需诊断」契约语与边界声明",
          "空仓" in t1 and "零 BUY" in t1)
        T("B3 幂等：重跑 EXISTS", emit("2026-10-13", sim=True, base=sb)["reason"] == "EXISTS")
        T("B4 降级清单点名硬检查/Thesis 缺口",
          any("退出闸" in x for x in d["degradations"])
          and any("Thesis" in x for x in d["degradations"]))
        # C 组：有仓态（经 SIM 决策账本真实入账 → derive_holdings 非空）
        # 夹具 = settle.py 自检同款合法 BUY 契约（write_decision + RISK_PLAN + EXECUTION）
        import decision as DEC
        import thesis as TH
        import pnl as PNLM
        th = os.path.join(sb, "thesis.md")
        TH.init_file(th)

        def pred(slot):
            return {"prediction_id": slot, "metric": "m_%s" % slot,
                    "data_source": "snapshot", "baseline": "T 日收盘",
                    "direction": "UP", "threshold": 3.0,
                    "condition": "T+3 累计涨幅 ≥3%", "window": "3D"}

        FIVE = {"驱动存在": "板块级订单落地", "当前证据": "公告 + 资金同步流入",
                "延续机制": "订单排产 → 业绩预期", "失效条件": "公告被澄清",
                "剩余预期": "3 日内扩散"}
        ENV = {"regime": "强势", "signals": {"limit_up_count": 62},
               "leg": "INTRADAY", "source": "EXPLICIT"}
        tid = TH.create_thesis("2026-10-06", "A", "驱动：sz000001", code="sz000001",
                               path=th)["id"]
        chain = {"evidence": ["公告"], "inference": ["催化 → A 类"],
                 "prediction": [pred(s) for s in PNLM.SLOTS], "five": dict(FIVE)}
        d = DEC.write_decision("2026-10-06", "sz000001", "BUY", tid, chain, sim=True,
                               base=sb, thesis_path=th, driver="A", entry_limit=10.0,
                               stop_line=9.5, target=11.0, window=3,
                               position_request=0.05, env=ENV)
        DEC.append_risk_plan(d["id"], 0.05, as_of="2026-10-06", base=sb)
        DEC.append_execution(d["id"], 0.05, "filled", as_of="2026-10-07",
                             avg_price=10.0, base=sb)
        d2 = collect("2026-10-08", sim=True, base=sb)
        T("C1 有仓态：derive_holdings 经 EXECUTION 物化（真实 C5 链）",
          d2["status"] == "HOLDINGS" and "sz000001" in d2["holdings"])
        r2 = emit("2026-10-08", sim=True, base=sb)
        t2 = io.open(r2["path"], encoding="utf-8").read()
        T("C2 单票段含 S4/S5 缺席如实标注（快照未覆盖）",
          t2.count("ABSENT") >= 2 and "非风控指令" in t2)
        T("C3 输出落 X1 报告区（e5_<as_of>.md）",
          os.path.basename(r2["path"]) == "e5_2026-10-08.md")
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    # D 组：真实生产态（空仓如实）
    d = collect("2026-10-13", sim=False)
    T("D1 生产真实态 = 空仓（无账本/无 EXECUTION）——不得虚构持仓",
      d["status"] == "EMPTY")
    print("== e5 --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    if "--eval" in argv:
        # 运转留痕（E 系列留痕欠账收口 · 2026-10-14）：stage=POST / actor=ext:e5。
        as_of = argv[argv.index("--eval") + 1]
        sim = "--sim" in argv
        base = argv[argv.index("--base") + 1] if "--base" in argv else None
        if base and not os.path.isdir(base):
            print("--base 必须是已存在目录（留痕 log_path 与数据根同源），实际: %s" % base)
            sys.exit(2)
        import observe as _OBS
        _logp = (os.path.join(base, "data", "state", "run_log.jsonl")
                 if base else None)
        _obs = _OBS.run_observed(
            "ext:e5", "e5_position", "POST",
            lambda: emit(as_of, sim=sim, base=base), as_of=as_of,
            detail="E5 持仓诊断（ext 外延 · 盘后人触发；sim=%s）" % sim,
            raise_on_error=False, log_path=_logp)
        if _obs["error"]:
            print("失败: %s" % _obs["error"])
            sys.exit(1)
        import json as _j
        print(_j.dumps(_obs["value"], ensure_ascii=False, indent=1))
        sys.exit(0)
    print("用法: e5_position.py --eval <as_of> [--sim] [--base <沙盒根>] | --selftest")
    sys.exit(2)
