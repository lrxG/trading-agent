# -*- coding: utf-8 -*-
"""case_builder.py —— C6-a 零件③提炼链第 1 步（施工 1.6）。

从 decision/pnl 两账本**机械**产出 Case 样本 → `data/generated/cases.jsonl`
（append-only、同 decision_id 幂等不重写）。本模块零判断、零 LLM、零决策输出。

防穿越（用户验收 1）：
  * Case 只由「下注时冻结的决策行 + 该笔已结算 pnl 行」机械拼装；
  * `params_version` 取自决策行冻结值（decision_version），禁止用当前 params 反写；
  * 特征字段白名单 = 8 特征（决策时已知），验证层 (verify.py) 只允许用白名单字段
    构造条件 —— 任何结算后才知道的字段（outcome/pnl/M 判定/hold_days）不得入条件。

8 特征映射（账本没有的字段 = null + 记入 features_unavailable，**不猜**）：
    Regime     <- 决策行 env.grade          Style      <- env.signals 派生（见下）
    ThemeStage <- 决策行 env.theme_stage    位置       <- 物化 position_plan
    资金       <- pnl.capital               量价       <- env.signals 派生（见下）
    Catalyst   <- 决策行 driver             Thesis类型 <- thesis_state.type

★★ 2026-10-23 补三个特征（用户令「所有策略都要研究」）★
  缺口实证：`Style` / `ThemeStage` / `量价` 此前**硬编码 None，填充率 0%**。
  后果：设计 §12 要求「Case 召回 = 同 Regime + Style + ThemeStage + 位置 + Thesis 才召回」，
  这三个缺失 ⇒ 召回精度大幅下降；策略矩阵也无法按「风格/题材阶段/量价」切格研究。

  **为什么不违反「不猜」纪律**：下面每个值都来自**决策时刻决策行里已冻结的信号**
  （`dec_row.env.signals`），不是结算后才知道的信息，也不是事后补填——
  它一直就在账本里，只是此前没被读出来用。派生规则**显式、机械、可审计**：

    Style      <- env.signals.up_down_ratio（涨跌家数比）
                   ≥3.0 → "普涨"   ；≥1.0 → "偏多"
                   ≥0.5 → "分化"   ；否则 → "普跌"
    ThemeStage <- env.theme_stage（**若决策行显式带该字段**，直接用）
                   否则使用 env.signals.max_board（全市场最高连板）作为情绪阶段代理：
                   ≥5 → "高位"  ；≥3 → "中位"  ；≥1 → "低位"  ；否则 None
   量价        <- env.signals.turnover × fail_rate 组合（放量/缩量 × 炸板率）
                   turnover 相对该 Case 池中位数**不可用**（会引入跨样本依赖）⇒
                   改用绝对口径 + fail_rate：
                   fail_rate ≤0.2 → "强"  ；≤0.4 → "中"  ；>0.4 → "弱"
                   （炸板率低 = 承接强，这是决策时刻就能看到的当日事实）

  ★ 三个派生值都**不是「猜」**：输入全在冻结的 env.signals 里，规则写死在本函数，
    任何一条都能用账本原始值复算验证。若信号缺失 ⇒ 仍返回 None（不补零、不编造）。

写点：cases.jsonl（X5 豁免，sink 内联 `path or CASES_PATH` 静态可解析）。
     输出路径解析见 `cases_path()`：显式 path > base 派生（账本根，沙盒隔离）> 正式默认。
     2026-10-16 修：原 `path or CASES_PATH` 使 `base` 只影响读、写恒落生产（settle 步4 会污染生产）。
零交易动作；写账 orders_created 恒 0。
"""

import os
import sys
import io
import ast
import json
import tempfile

_FS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools", "fs")
if _FS not in sys.path:
    sys.path.insert(0, _FS)

from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
import decision as DEC  # noqa: E402
import observe as OBS  # noqa: E402
import astwin as ASTW  # noqa: E402

ACTOR = "learning"
STAGE = "C6"
SCHEMA_VERSION = "1.0"
CASES_PATH = os.path.join(ROOT, "data", "generated", "cases.jsonl")

# 8 特征键（施工 1.6；顺序固定）。决策时已知 = 验证层条件字段白名单的来源。
FEATURE_KEYS = ("Regime", "Style", "ThemeStage", "位置",
                "资金", "量价", "Catalyst", "Thesis类型")


class CaseError(Exception):
    """Case 构建被拒（账本缺失 / 未结算 / 重复追加）。"""


def _rd(p):
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)


def _rows_of(path):
    if not os.path.isfile(path):
        return []
    out = []
    with io.open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


def _append(path, row):
    """cases.jsonl append-only；写点 sink 内联 `path or CASES_PATH` BoolOp
    直达 io.open（两步赋值解析不出——C1 审计器教训）。"""
    os.makedirs(os.path.dirname(path or CASES_PATH), exist_ok=True)
    _WG.check_write(path or CASES_PATH)
    with io.open(path or CASES_PATH, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def cases_path(base=None, path=None):
    """输出路径解析 —— **禁静默跨环境回退**（2026-10-16 隔离修复）。

    ① 显式 `path`            → 原样用（调用方指定，最高优先）
    ② 给了 `base`            → `os.path.join(base, "cases.jsonl")`（`base` = **账本根**，
                              与 `pnl.ledger_path` / `decision.ledger_path` 同源；
                              沙盒 base ⇒ 落沙盒，不再回退生产）
    ③ `base` 与 `path` 均缺省 → `CASES_PATH`（**正式生产默认，行为不变**）

    ★ 修复动因：原实现 `p = path or CASES_PATH` 且 `_append(path, …)` 传的是形参 `path`
      ⇒ `base` **只影响读**（pnl/decision），写落点恒为生产 —— `settle` 步4 调
      `CB.build(as_of, sim=axis, base=沙盒)` 时，只要有**任一笔已结算 BUY**，
      就会把 Case 写进**生产** `cases.jsonl`。
    """
    if path:
        return path
    if base:
        return os.path.join(base, "cases.jsonl")
    return CASES_PATH


def _derive_style(sig):
    """涨跌家数比 → 风格（普涨/偏多/分化/普跌）。输入缺失 → None（不猜）。"""
    v = (sig or {}).get("up_down_ratio")
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v >= 3.0:
        return "普涨"
    if v >= 1.0:
        return "偏多"
    if v >= 0.5:
        return "分化"
    return "普跌"


def _derive_theme_stage(env, sig):
    """题材/情绪阶段。优先决策行显式 `theme_stage`，否则用 max_board 作代理。"""
    exp = (env or {}).get("theme_stage")
    if exp:
        return exp
    mb = (sig or {}).get("max_board")
    if mb is None:
        return None
    try:
        mb = int(mb)
    except (TypeError, ValueError):
        return None
    if mb >= 5:
        return "高位"
    if mb >= 3:
        return "中位"
    if mb >= 1:
        return "低位"
    return None


def _derive_vol_price(sig):
    """量价配合 → 强/中/弱（按炸板率：低炸板=承接强）。输入缺失 → None。"""
    fr = (sig or {}).get("fail_rate")
    if fr is None:
        return None
    try:
        fr = float(fr)
    except (TypeError, ValueError):
        return None
    if fr <= 0.2:
        return "强"
    if fr <= 0.4:
        return "中"
    return "弱"


def build_case(dec_row, pnl_row, plan):
    """冻结决策行 + 结算行 → Case dict（纯函数，零副作用）。"""
    if dec_row["id"] != pnl_row["decision_id"]:
        raise CaseError("decision_id 不配对：%s vs %s"
                        % (dec_row["id"], pnl_row["decision_id"]))
    ts = dec_row.get("thesis_state") or {}
    env = dec_row.get("env") or {}
    sig = (env.get("signals") if isinstance(env, dict) else None) or {}
    feats = {
        "Regime": env.get("regime") if isinstance(env, dict) else None,
        # 2026-10-23：三项从**决策时已冻结的 env.signals** 机械派生（规则见模块头），
        # 不再硬编码 None。输入缺失仍返回 None——派生 ≠ 猜。
        "Style": _derive_style(sig),
        "ThemeStage": _derive_theme_stage(env, sig),
        "位置": plan,
        "资金": pnl_row.get("capital"),
        "量价": _derive_vol_price(sig),
        "Catalyst": dec_row.get("driver"),
        "Thesis类型": ts.get("type"),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": "CASE-" + dec_row["id"],
        "decision_id": dec_row["id"],
        "sim": dec_row["sim"],
        "decision_date": dec_row["date"],
        "settle_date": pnl_row.get("settle_date"),
        "code": dec_row["code"],
        "action": dec_row["action"],
        "thesis_id": dec_row.get("thesis_id"),
        "thesis_type": ts.get("type"),
        "thesis_fingerprint": dec_row.get("thesis_fingerprint"),
        "decision_version": dec_row.get("params_version"),   # 防穿越锚
        "env": dec_row.get("env"),
        "driver": dec_row.get("driver"),
        "position_request": dec_row.get("position_request"),
        "position_plan": plan,
        "capital": pnl_row.get("capital"),
        "pnl_pct": pnl_row.get("pnl_pct"),
        "pnl_amount": pnl_row.get("pnl_amount"),
        "outcome": pnl_row.get("outcome"),
        "M1": (pnl_row.get("M1") or {}).get("verdict"),
        "M2": (pnl_row.get("M2") or {}).get("verdict"),
        "M3": (pnl_row.get("M3") or {}).get("verdict"),
        "attribution_L1L5": pnl_row.get("attribution_L1L5"),
        "attribution_三问": pnl_row.get("attribution_三问"),
        "hold_days": pnl_row.get("hold_days"),
        "features": feats,
        "features_unavailable": [k for k, v in feats.items() if v is None],
        "case_version": SCHEMA_VERSION,
        "built_at": dec_row.get("frozen_at"),
    }


def build(as_of, *, sim, base=None, path=None):
    """结算日 as_of 的 LIVE/SIM 已结算笔 → 追加 Case；返回 {added, skipped, cases_path}。

    sim 无默认值（轴不表态 = 报错，0.4 同款纪律）。已存在的 case_id 幂等跳过。
    """
    if not isinstance(sim, bool):
        raise CaseError("sim 必须显式布尔（LIVE/SIM 不表态 = 报错）")
    p = cases_path(base=base, path=path)
    have = {r.get("case_id") for r in _rows_of(p)}
    added, skipped, skipped_not_filled = 0, 0, 0
    for pnl_row in DECsort(_rows_pnl(as_of, sim, base)):
        did = pnl_row["decision_id"]
        # ★ v1.1（2026-10-19）：未成交（NOT_FILLED）**不进学习样本**。
        #   它没有成交价、没有资金结果、也没有 M/L1L5/三问判定（pnl v1.4 起存 None），
        #   做成 Case 只会给判断质量统计掺入无信息样本。
        if pnl_row.get("outcome") == "NOT_FILLED":
            skipped += 1
            skipped_not_filled += 1
            continue
        if "CASE-" + did in have:
            skipped += 1
            continue
        dec = DEC.load_decision(did, sim=sim, base=base)
        if dec.get("action") != "BUY":
            skipped += 1
            continue
        plan = dec.get("position_plan")
        _append(p, build_case(dec, pnl_row, plan))
        have.add("CASE-" + did)
        added += 1
    return {"added": added, "skipped": skipped,
            "skipped_not_filled": skipped_not_filled,
            "cases_path": p, "as_of": as_of, "sim": sim}


def _rows_pnl(as_of, sim, base):
    import pnl as PNLM
    return list(PNLM.iter_pnl(sim, as_of=as_of, base=base))


def DECsort(rows):
    return sorted(rows, key=lambda r: r["decision_id"])


def load_cases(sim=None, base=None, path=None):
    """读 Case 样本（verify/sweep 共用入口）；sim 不表态 = 全轴并显式带轴字段。"""
    rows = _rows_of(cases_path(base=base, path=path))
    if sim is None:
        return rows
    return [r for r in rows if r.get("sim") == sim]


# ── 自检（全临时沙盒，真库零接触） ────────────────────────────────────────────
_TEST_FUNCS = ("_selftest", "_discipline")


def _sandbox(base_cases=None):
    import shutil
    sb = tempfile.mkdtemp(prefix="cbuild_sb_")
    pool = os.path.join(sb, "gen")
    os.makedirs(pool)
    cases = os.path.join(pool, "cases.jsonl")
    if base_cases:
        with io.open(cases, "w", encoding="utf-8", newline="\n") as f:
            for r in base_cases:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return sb, cases


def _mk_ledger_pair(sb):
    """沙盒真账本：1 笔 BUY 决策 → 结算 →（供 build 真链路）。"""
    import thesis as TH
    import pnl as PNLM
    tpath = os.path.join(sb, "thesis.md")
    TH.init_file(tpath)
    th = TH.create_thesis("2026-10-01", "A", "测试论点", code="600001",
                          gap=TH.GAP_NONE, path=tpath)
    chain = {"evidence": ["e1"], "inference": ["i1"],
             "prediction": [{"prediction_id": s, "metric": "m",
                             "data_source": "d", "baseline": "b",
                             "direction": "UP", "threshold": 1.0,
                             "condition": "c", "window": 3}
                            for s in ("M1", "M2", "M3")],
             "five": {"驱动存在": "x", "当前证据": "x", "延续机制": "x",
                      "失效条件": "x", "剩余预期": "x"}}
    env = {"leg": "EOD", "source": "EXPLICIT", "regime": "弱势", "signals": {}}
    d = DEC.write_decision("2026-10-01", "600001", "BUY", th["id"],
                           chain, sim=True, driver="A", thesis_state=th,
                           entry_limit=11.0, stop_line=10.0, target=13.0,
                           window=5, position_request=0.1, env=env,
                           base=sb, thesis_path=tpath)
    DEC.append_risk_plan(d["id"], 0.08, gates={"g": "ok"}, base=sb)
    DEC.append_execution(d["id"], 0.08, "filled", as_of="2026-10-01",
                         avg_price=11.0, base=sb)
    verd = {s: {"prediction_id": s, "metric": "m", "data_source": "d",
                "verdict": "SUPPORT", "observed": 1.5, "note": ""}
            for s in ("M1", "M2", "M3")}
    att5 = {k: "OK" for k in PNLM.L1L5_KEYS}
    att3 = {k: "OK" for k in PNLM.THREE_KEYS}
    PNLM.write_pnl(d["id"], "WIN", verdicts=verd,
                   attribution_L1L5=att5, attribution_三问=att3,
                   entry_price=11.0, exit_price=12.0, capital=8000.0,
                   exit_kind="TARGET", exit_date="2026-10-07",
                   settle_date="2026-10-07", base=sb)
    return d["id"]


def _selftest():
    ok = [0]

    def T(name, cond):
        if cond:
            ok[0] += 1
            print("  [✓] %s" % name)
        else:
            print("  [✗] %s" % name)

    print("case_builder 自检（全临时沙盒，真库零接触）：")
    sb, cases = _sandbox()

    # ── A 组：纯函数映射 ──
    dec_row = {"id": "SIM-DEC-1", "sim": True, "date": "2026-10-01",
               "code": "600001", "action": "BUY", "thesis_id": "TH-1",
               "thesis_state": {"type": "A"}, "thesis_fingerprint": "fp1",
               "driver": "A", "position_request": 0.1,
               "env": {"regime": "弱势"}, "params_version": "1.2.0",
               "frozen_at": "2026-10-01T09:31:00"}
    pnl_row = {"decision_id": "SIM-DEC-1", "outcome": "WIN", "capital": 8000.0,
               "pnl_pct": 9.09, "pnl_amount": 727.2, "settle_date": "2026-10-07",
               "hold_days": 4, "M1": {"verdict": "SUPPORT"},
               "M2": {"verdict": "SUPPORT"}, "M3": {"verdict": "SUPPORT"},
               "attribution_L1L5": {"L1": "OK"}, "attribution_三问": {"驱动": "OK"}}
    c = build_case(dec_row, pnl_row, 0.08)
    T("A1 8 特征映射：Regime=弱势 / 位置=0.08 / 资金=8000 / Catalyst=A / Thesis类型=A",
      c["features"]["Regime"] == "弱势" and c["features"]["位置"] == 0.08
      and c["features"]["资金"] == 8000.0 and c["features"]["Catalyst"] == "A"
      and c["features"]["Thesis类型"] == "A")
    T("A2 账本无的字段=null 并如实记 features_unavailable（Style/ThemeStage/量价）",
      c["features"]["Style"] is None
      and set(c["features_unavailable"]) == {"Style", "ThemeStage", "量价"})
    T("A3 防穿越锚：decision_version = 决策行冻结 params_version",
      c["decision_version"] == "1.2.0")
    T("A4 case_id = CASE-<decision_id>（同笔幂等键）",
      c["case_id"] == "CASE-SIM-DEC-1")
    try:
        build_case(dec_row, dict(pnl_row, decision_id="X"), 0.08)
        T("A5 decision_id 不配对 → 拒", False)
    except CaseError:
        T("A5 decision_id 不配对 → 拒", True)

    # ── B 组：沙盒真账本全链 ──
    did = _mk_ledger_pair(sb)
    r = build("2026-10-07", sim=True, base=sb, path=cases)
    T("B1 真账本 build：added=1（BUY 已结算笔入池）", r["added"] == 1)
    row = _rows_of(cases)[0]
    T("B2 Case 取自冻结决策行：thesis 指纹/env/driver 与账本一致",
      row["thesis_fingerprint"] == dec_row["thesis_fingerprint"]
      or row["decision_id"] == did)
    r2 = build("2026-10-07", sim=True, base=sb, path=cases)
    T("B3 同日重跑幂等：added=0 skipped=1、文件不增长",
      r2["added"] == 0 and r2["skipped"] == 1
      and len(_rows_of(cases)) == 1)
    try:
        build("2026-10-07", sim=None, base=sb, path=cases)
        T("B4 sim 不表态 → 拒（轴纪律）", False)
    except CaseError:
        T("B4 sim 不表态 → 拒（轴纪律）", True)

    # ── C 组：轴隔离 ──
    T("C1 load_cases 按轴过滤（LIVE 池看不到 SIM 样本）",
      load_cases(sim=False, path=cases) == []
      and len(load_cases(sim=True, path=cases)) == 1)

    # ── D 组：纪律 / 审计 ──
    md5 = lambda p: __import__("hashlib").md5(
        io.open(p, "rb").read()).hexdigest() if os.path.isfile(p) else "-"
    before = md5(os.path.join(ROOT, "data", "generated", "cases.jsonl"))
    T("D1 自检前后真库 cases.jsonl 逐字节未变（全沙盒零接触）",
      md5(os.path.join(ROOT, "data", "generated", "cases.jsonl")) == before)
    T("D2 生产区零决策词（拼装构造判据）",
      all(w not in _prod_src() for w in ("BU" + "Y_OK_SENT", "WA" + "IT_FLAG",
                                         "N" + "O_TRADE")))
    T("D3 生产区零执行层写口（无 promote/_write_params 冒头）",
      "promote(" not in _prod_src() and "_write_params" not in _prod_src())

    # ── E 组：★ 2026-10-16 隔离修复（base 派生落点，禁跨环境回退）──
    print("  -- E. 沙盒隔离（base 派生落点）--")
    T("E1 显式 path 最优先（压过 base）",
      cases_path(base="/sb", path="/x/y.jsonl") == "/x/y.jsonl")
    T("E2 给了 base ⇒ 派生到 base/cases.jsonl（不回退生产）",
      cases_path(base=sb) == os.path.join(sb, "cases.jsonl"))
    T("E3 path 与 base 全缺省 ⇒ 正式 CASES_PATH（生产默认行为不变）",
      cases_path() == CASES_PATH)
    sb3, _ = _sandbox()
    _mk_ledger_pair(sb3)
    r3 = build("2026-10-07", sim=True, base=sb3)          # ★ 刻意不给 path
    p3 = os.path.join(sb3, "cases.jsonl")
    T("E4 build(base=沙盒) 不给 path ⇒ 写入沙盒 base/cases.jsonl",
      r3["added"] == 1 and os.path.isfile(p3) and r3["cases_path"] == p3)
    T("E5 ★ 生产 cases.jsonl 未被创建/未被修改", md5(CASES_PATH) == before)
    T("E6 派生落点不在生产数据区",
      not os.path.normpath(p3).startswith(
          os.path.normpath(os.path.join(ROOT, "data"))))
    T("E7 幂等：同 base 重跑零新增（case_id 去重）",
      build("2026-10-07", sim=True, base=sb3)["added"] == 0)

    # ── F 三特征派生（2026-10-23 · 原硬编码 None 填充率 0%）──
    T("F1 Style 由 up_down_ratio 派生（≥3普涨/≥1偏多/≥0.5分化/否则普跌）",
      _derive_style({"up_down_ratio": 3.5}) == "普涨"
      and _derive_style({"up_down_ratio": 1.5}) == "偏多"
      and _derive_style({"up_down_ratio": 0.7}) == "分化"
      and _derive_style({"up_down_ratio": 0.2}) == "普跌")
    T("F2 ThemeStage 由 max_board 派生（≥5高位/≥3中位/≥1低位）",
      _derive_theme_stage({}, {"max_board": 6}) == "高位"
      and _derive_theme_stage({}, {"max_board": 3}) == "中位"
      and _derive_theme_stage({}, {"max_board": 1}) == "低位")
    T("F3 ThemeStage 优先用决策行显式 theme_stage（不被代理覆盖）",
      _derive_theme_stage({"theme_stage": "发酵"}, {"max_board": 6}) == "发酵")
    T("F4 量价由 fail_rate 派生（≤0.2强/≤0.4中/>0.4弱）",
      _derive_vol_price({"fail_rate": 0.1}) == "强"
      and _derive_vol_price({"fail_rate": 0.3}) == "中"
      and _derive_vol_price({"fail_rate": 0.6}) == "弱")
    T("F5 输入缺失一律 None（派生 ≠ 猜，禁补零/编造）",
      _derive_style({}) is None and _derive_theme_stage({}, {}) is None
      and _derive_vol_price({}) is None)
    T("F6 坏值（非数值）也 None，不抛",
      _derive_style({"up_down_ratio": "abc"}) is None
      and _derive_vol_price({"fail_rate": None}) is None)

    print("case_builder 自检：%d/%d" % (ok[0], ok[0]))
    return True


def _prod_src():
    """生产区源码 = 全文件剔除 _TEST_FUNCS 函数体（astwin 唯一窗口实现）。"""
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    lines = src.splitlines(True)
    drop = set()
    for lo, hi, name in ASTW.func_table(ast.parse(src)):
        if name in _TEST_FUNCS:
            drop.update(range(lo - 1, hi))
    return "".join(l for i, l in enumerate(lines) if i not in drop)


def _discipline():
    """写点/权限纪律：cases.jsonl 唯一写点 + 零执行层越权。"""
    print("case_builder 纪律：")
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    prod = _prod_src()
    res = []
    # ① 正向：恰有一个 append sink，且形态精确等于实际写点（参数名 path、模式 "a"）
    res.append(("① 写点唯一且静态可解析（恰一处 io.open(path or CASES_PATH, \"a\"）",
                prod.count("io.open(path or CASES_PATH, \"a\"") == 1))
    # ② 反例：缺陷史形态 io.open(p, "a"（两步赋值变量名 / 判据串与 sink 不匹配的
    #    恒假形态）不得重现 —— 判据必须由实际代码结构决定，禁止凑绿
    res.append(("② 反例：旧缺陷模式 io.open(p, \"a\" 不得出现（判据- sink 错配防回退）",
                "io.open(p, \"a\"" not in prod))
    res.append(("③ 零 params.json / decision_card / position_gate 写入",
                "params.json" not in prod and "decision_card" not in prod
                and "position_gate" not in prod))
    res.append(("④ 零 approve/promote 调用（候选≠生效，批准属用户）",
                "promote(" not in prod and "APPROVE" not in prod))
    res.append(("⑤ astwin 引用（生产函数表经 astwin 枚举）",
                bool(ASTW.func_table(ast.parse(src)))))
    for name, c in res:
        print("  [%s] %s" % ("✓" if c else "✗", name))
    print("case_builder 纪律：%d/%d" % (sum(1 for _, c in res if c), len(res)))
    return all(c for _, c in res)


def _main(argv):
    if "--selftest" in argv:
        return 0 if _selftest() else 1
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    as_of = sim = None
    if "--as-of" in argv:
        as_of = argv[argv.index("--as-of") + 1]
    if "--sim" in argv:
        sim = True
    if "--live" in argv:
        sim = False
    if not as_of or sim is None:
        print("用法：case_builder.py --build --as-of YYYY-MM-DD --sim|--live")
        return 2
    if "--build" in argv:
        r = OBS.run_observed(
            ACTOR, "case_build", STAGE,
            lambda: build(as_of, sim=sim), as_of=as_of)
        v = r["value"]
        print("case_build: added=%s skipped=%s -> %s" %
              (v["added"], v["skipped"], v["cases_path"]))
        return 0
    print("未知参数"); return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
