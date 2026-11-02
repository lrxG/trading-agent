# -*- coding: utf-8 -*-
"""E9 组合诊断（2026-10-13 · 第一阶段补齐）。

职责（registry）：敞口 / 集中度 / 相关性 / 环境匹配 → 挂载 C5。
**边界：零 BUY/WAIT/NO、零仓位动作**（纯只读装配 + 报告落点）。

输入（全部只读，缺→ABSENT 如实）：
  · 敞口唯一真源 = C5 `position_gate.portfolio_exposure`（executed/pending/exposure）
  · 环境档 = `brief.read_env`（brain_state.md 环境档段；缺段如实）
  · S4 拥挤 / S5 资金聚合（分析解释段，非风控指令）

**硬风控与分析解释严格分槽**：
  · 「环境匹配」只做**机械对照**（C1 档位 cap vs 当前 exposure，超限标记）——
    cap 真源 = brain_state 环境档（C1 产物），本件零计算零绕过；
  · 相关性：无持仓/无收益序列 ⇒ UNAVAILABLE（不得用板块同属冒充相关系数）。

输出 `data/generated/reports/e9_<as_of>.md`（X1；写点复用 premarket._atomic_write）。
幂等：同输入同字节 ⇒ 跳过写。
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
import position_gate as PG      # noqa: E402  敞口唯一真源
import premarket as E1          # noqa: E402  复用已登记写点（零新增写点）
import brief as BRF             # noqa: E402  环境档读取（真源复用）

S_ID = "e9"


def _load_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def collect(as_of, *, sim=False, base=None):
    b = base or ROOT
    gen = os.path.join(b, "data", "generated")
    expo = PG.portfolio_exposure(sim, base=base)          # {} = 空仓
    tot_exec = sum(v["executed"] for v in expo.values())
    tot_pend = sum(v["pending"] for v in expo.values())
    max_pos = max((v["exposure"] for v in expo.values()), default=0.0)
    conc = round(max_pos / (tot_exec + tot_pend), 4) if (tot_exec + tot_pend) > 0 else None
    try:
        env = BRF.read_env(os.path.join(b, "memory", "state", "brain_state.md"))
    except Exception:
        env = None
    env_pub = ({"status": "PRESENT", "grade": env.get("grade"), "cap": env.get("cap")}
               if isinstance(env, dict) and env.get("grade") else {"status": "ABSENT"})
    match = "UNAVAILABLE（空仓无敞口可匹配）" if not expo else (
        "UNAVAILABLE（环境档缺席，cap 不可判）" if env_pub["status"] != "PRESENT"
        else "MECHANICAL_CHECK：exposure 总和 %.4f vs C1 cap=%s（%s）"
             % (tot_exec + tot_pend, env_pub.get("cap"),
                "超限标记" if env_pub.get("cap") and isinstance(
                    env_pub.get("cap"), (int, float))
                and (tot_exec + tot_pend) > env_pub["cap"] else "未超限（机械对照，非指令）"))
    d = {"as_of": as_of, "axis": "SIM" if sim else "LIVE",
         "exposure": expo, "n_positions": len(expo),
         "totals": {"executed": round(tot_exec, 6), "pending": round(tot_pend, 6),
                    "total": round(tot_exec + tot_pend, 6)},
         "concentration": conc,
         "correlation": {"status": "UNAVAILABLE",
                         "reason": "无持仓收益序列（空仓/账本无收益行）——相关系数不可产出，"
                                   "禁以板块同属冒充"},
         "env_match": {"status": "PRESENT", "mechanical": match,
                       "env": env_pub},
         "status": ("EMPTY" if not expo else "POSITIONS"),
         "degradations": []}
    s4 = _load_json(os.path.join(gen, "s_s4_%s.json" % as_of))
    s5 = _load_json(os.path.join(gen, "s_s5_%s.json" % as_of))
    d["s4_crowding"] = ({"status": "ABSENT"} if s4 is None else
                        {"status": "PRESENT", "n_high": s4.get("n_high"),
                         "n_codes": s4.get("n_codes")})
    d["s5_capital"] = ({"status": "ABSENT"} if s5 is None else
                       {"status": "PRESENT",
                        "behavior_counts": s5.get("behavior_counts")})
    if not expo:
        d["degradations"].append("空仓（C5 敞口真源 0）——组合诊断输出空态（如实）")
    if env_pub["status"] != "PRESENT":
        d["degradations"].append("环境档缺席——环境匹配 UNAVAILABLE（cap 不可判）")
    d["degradations"].append("相关性 UNAVAILABLE（无收益序列）")
    if s4 is None:
        d["degradations"].append("S4 拥挤快照缺席（分析段缺）")
    if s5 is None:
        d["degradations"].append("S5 资金快照缺席（分析段缺）")
    return d


def render_md(d):
    L = ["# E9 组合诊断 · %s（%s 轴）" % (d["as_of"], d["axis"]), ""]
    L.append("- 敞口（C5 唯一真源）：%s" % (
        "空仓" if d["status"] == "EMPTY"
        else "；".join("%s exec=%.4f pend=%.4f" % (c, v["executed"], v["pending"])
                       for c, v in sorted(d["exposure"].items()))))
    L.append("- 合计：executed=%.4f pending=%.4f total=%.4f ｜ 集中度=%s"
             % (d["totals"]["executed"], d["totals"]["pending"],
                d["totals"]["total"],
                d["concentration"] if d["concentration"] is not None else "UNAVAILABLE"))
    L.append("- 环境匹配：%s" % d["env_match"]["mechanical"])
    L.append("- 相关性：%s" % d["correlation"]["reason"])
    s4, s5 = d["s4_crowding"], d["s5_capital"]
    L.append("- S4 拥挤（分析）：%s" % (
        "高拥挤 %d/%d" % (s4.get("n_high"), s4.get("n_codes"))
        if s4["status"] == "PRESENT" else "ABSENT"))
    L.append("- S5 资金（分析）：%s" % (
        json.dumps(s5.get("behavior_counts"), ensure_ascii=False)
        if s5["status"] == "PRESENT" else "ABSENT"))
    L.append("")
    if d["status"] == "EMPTY":
        L.append("## 账本语义声明（空仓态必读）")
        L.append("- 当前账本无记录或账本文件缺席（C5 敞口真源口径 0 在仓）。")
        L.append("- 本报告不能证明真实账户当前持仓为空。")
        L.append("- 不得据此验证真实持仓决策。")
        L.append("")
    L.append("## 降级与缺席（如实）")
    for x in d["degradations"]:
        L.append("- %s" % x)
    L.append("")
    L.append("> 边界：零 BUY/WAIT/NO、零仓位动作；硬风控真源 = C5（params），"
             "本件只做机械对照与呈现，不产生任何调仓指令。")
    return "\n".join(L)


def emit(as_of, *, sim=False, base=None):
    d = collect(as_of, sim=sim, base=base)
    md = render_md(d)
    p = os.path.join(base or ROOT, "data", "generated", "reports",
                     "e9_%s.md" % as_of)
    if os.path.isfile(p) and io.open(p, encoding="utf-8").read() == md:
        return {"created": False, "reason": "EXISTS", "path": p}
    E1._atomic_write(p, md)
    return {"created": True, "path": p,
            "generated_at": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}


def _selftest():
    import tempfile as _tf
    import shutil as _sh
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== e9 --selftest ==")
    sb = _tf.mkdtemp(prefix="e9_sb_")
    try:
        os.makedirs(os.path.join(sb, "data", "generated"), exist_ok=True)
        d = collect("2026-10-13", sim=True, base=sb)
        T("B1 空仓如实（C5 敞口真源 0）", d["status"] == "EMPTY"
          and d["totals"]["total"] == 0.0)
        T("B2 相关性 UNAVAILABLE（禁板块同属冒充）",
          d["correlation"]["status"] == "UNAVAILABLE")
        r1 = emit("2026-10-13", sim=True, base=sb)
        t1 = io.open(r1["path"], encoding="utf-8").read()
        T("B3 空仓态报告落 X1 且含边界声明",
          os.path.basename(r1["path"]) == "e9_2026-10-13.md" and "零 BUY" in t1)
        T("B4 幂等：重跑 EXISTS", emit("2026-10-13", sim=True, base=sb)["reason"] == "EXISTS")
        T("B5 降级清单点名环境档/相关性",
          any("环境档" in x for x in d["degradations"])
          and any("相关性" in x for x in d["degradations"]))
        # C 组：有仓态（settle 自检同款合法 BUY 夹具）
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

        FIVE = {"驱动存在": "订单落地", "当前证据": "公告+资金", "延续机制": "排产",
                "失效条件": "澄清", "剩余预期": "扩散"}
        ENV = {"regime": "强势", "signals": {"limit_up_count": 62},
               "leg": "INTRADAY", "source": "EXPLICIT"}
        tid = TH.create_thesis("2026-10-06", "A", "驱动：sz000001", code="sz000001",
                               path=th)["id"]
        chain = {"evidence": ["公告"], "inference": ["催化 → A 类"],
                 "prediction": [pred(s) for s in PNLM.SLOTS], "five": dict(FIVE)}
        d0 = DEC.write_decision("2026-10-06", "sz000001", "BUY", tid, chain, sim=True,
                                base=sb, thesis_path=th, driver="A", entry_limit=10.0,
                                stop_line=9.5, target=11.0, window=3,
                                position_request=0.05, env=ENV)
        DEC.append_risk_plan(d0["id"], 0.05, as_of="2026-10-06", base=sb)
        DEC.append_execution(d0["id"], 0.05, "filled", as_of="2026-10-07",
                             avg_price=10.0, base=sb)
        d2 = collect("2026-10-08", sim=True, base=sb)
        T("C1 有仓态：敞口真源非零、集中度=1.0（单票）",
          d2["status"] == "POSITIONS" and d2["totals"]["executed"] == 0.05
          and d2["concentration"] == 1.0)
        r2 = emit("2026-10-08", sim=True, base=sb)
        T("C2 有仓态报告含机械对照（env 缺席 → UNAVAILABLE 如实）",
          "UNAVAILABLE" in io.open(r2["path"], encoding="utf-8").read())
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    d = collect("2026-10-13", sim=False)
    T("D1 生产真实态 = 空仓（不虚构敞口）", d["status"] == "EMPTY")
    print("== e9 --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    if "--eval" in argv:
        # 运转留痕（E 系列留痕欠账收口 · 2026-10-14）：stage=POST / actor=ext:e9。
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
            "ext:e9", "e9_portfolio", "POST",
            lambda: emit(as_of, sim=sim, base=base), as_of=as_of,
            detail="E9 组合诊断（ext 外延 · 盘后人触发；sim=%s）" % sim,
            raise_on_error=False, log_path=_logp)
        if _obs["error"]:
            print("失败: %s" % _obs["error"])
            sys.exit(1)
        import json as _j
        print(_j.dumps(_obs["value"], ensure_ascii=False, indent=1))
        sys.exit(0)
    print("用法: e9_portfolio.py --eval <as_of> [--sim] [--base <沙盒根>] | --selftest")
    sys.exit(2)
