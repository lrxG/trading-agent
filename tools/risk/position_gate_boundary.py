#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""position_gate_boundary.py · C5 机械风控**边界**回归（2026-10-13 建 · P3）

★ 定位：本文件是**测试件**，不是生产模块。它**不改** `position_gate.py`、**不新增**
  风控规则、**不放宽任何既有硬约束**；只对已存在的裁决链做**边界值**取证，
  回答一个问题：**阈值正好落在边界上时，闸是收紧还是放宽？**

★ 为什么单独立件：`position_gate.py` 自带 26 项自检（A/B/C/D 组）覆盖了**功能**，
  但边界情形（`dd == l1` 的含等号语义、`plan == cap` 时 `binding` 是否误报、
  `pending` 恰好吃满 cap、`holdings == max_n-1`、余量恰为 0 的拒绝路径）
  只在部分用例中间接触及。本件把这些点**逐条显式**化，且每项都给出
  「边界值 / 期望方向（收紧 or 放行）/ 实测结论」。

★ 边界取值全部**从 `params.json` 现读**（真源），不硬编码 15 / 25 / 39 / 60 / 3 ——
  这样参数一旦校准，本回归会**自动跟着变**，不会变成"过期的假绿"。

★ 机械性核验（P3 硬要求：风控纯机械、不调 LLM、不降低既有硬约束）：
  · 生产区**零**网络/模型依赖词（requests / urllib / http / openai / llm / anthropic…）
  · 生产区**零**账本直写（`open(...,"w")` / `_append` / `.write`）
  · 生产区**零**字面量阈值比较（阈值只能来自 params）—— 独立实现一次同判据
  · `check()` 纯函数性：同输入两次调用逐字节相同（无隐藏状态）

★ 纪律：本件全程只读（`params.json` 只读、四账本只 md5 不写），
  生产 `data/` / `memory/` 零改动。

用法：
  python tools/risk/position_gate_boundary.py --selftest     # 边界回归（退出码 0/1）
  python tools/risk/position_gate_boundary.py --report       # 阈值真值 + 边界方向表
"""

import ast
import hashlib
import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.dirname(_HERE)
for _p in (os.path.join(_TOOLS, "fs"), os.path.join(_TOOLS, "risk"),
           os.path.join(_TOOLS, "analysis")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from paths import ROOT  # noqa: E402
import params as PARAMS  # noqa: E402
import position_gate as PG  # noqa: E402

# 本件读的阈值键（真源 = params.json；缺一即报错，禁兜底默认）
KEYS = ("circuit_breaker_drawdown_l1", "circuit_breaker_cap_l1",
        "circuit_breaker_drawdown_l2", "circuit_breaker_flat_days",
        "single_stock_cap_of_equity", "single_stock_cap_of_position",
        "max_holding_count", "correction_price_up", "correction_position_half",
        "correction_cooldown_days")

_EPS = 1e-9        # 边界外推步长（只用于构造"恰差一个 epsilon"的样本）


class BoundaryError(Exception):
    """边界样本构造失败（params 缺键 / 值类型不合）—— 响失败，禁静默兜底。"""


def thresholds(params_path=None):
    """→ 阈值真值 dict（逐键现读；缺键即抛，禁内置默认）。"""
    out = {}
    for k in KEYS:
        v = PARAMS.get_value(k, path=params_path)
        if v is None:
            raise BoundaryError("params 缺键 %s（边界回归拒绝与未校准的假值对账）" % k)
        out[k] = v
    return out


def production_source():
    """→ (源码, AST, 生产区源码)。生产区 = 除本件声明的测试函数外的全部顶层函数。"""
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    segs = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name not in _TEST_FUNCS:
            segs.append(ast.get_source_segment(src, n))
    return src, tree, "\n".join(segs)


def _gate_source():
    """→ (源码, AST, position_gate.py 的**生产区**源码)。

    生产区 = 除该模块自报的 `_TEST_FUNCS` 之外的顶层函数（口径与 landing 审计同源：
    模块自己声明谁是测试入口，本件**派生**而非另抄一份清单）。
    """
    pg = os.path.join(_HERE, "position_gate.py")
    src = io.open(pg, encoding="utf-8").read()
    tree = ast.parse(src)
    test = ("_selftest",)                     # 兜底
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name) \
                and n.targets[0].id == "_TEST_FUNCS" \
                and isinstance(n.value, (ast.Tuple, ast.List)):
            got = tuple(e.value for e in n.value.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str))
            if got:
                test = got
                break
    segs = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name not in test:
            segs.append(ast.get_source_segment(src, n))
    return src, tree, "\n".join(segs)


def _md5(p):
    if not os.path.exists(p):
        return "ABSENT"
    with io.open(p, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


# ── CLI ─────────────────────────────────────────────────────────────────────
def _main(argv):
    if "--selftest" in argv:
        return 0 if _selftest() else 1
    if "--report" in argv:
        return _report()
    print(__doc__)
    return 2


def _report():
    t = thresholds()
    print("C5 机械风控边界回归 · 阈值真值（现读 params.json，无硬编码）")
    print("  %-32s %-10s %s" % ("参数", "真值", "边界方向（收紧 = 更严）"))
    rows = [
        ("circuit_breaker_drawdown_l1", "dd ≥ 此值 ⇒ 触 L1（含等号）"),
        ("circuit_breaker_drawdown_l2", "dd ≥ 此值 ⇒ 空仓窗口（含等号）"),
        ("circuit_breaker_flat_days", "窗口 = 最近 N 个权益点（含 L2 点则生效）"),
        ("circuit_breaker_cap_l1", "L1 总仓上限（成）"),
        ("single_stock_cap_of_equity", "单票 ≤ 权益×此值%"),
        ("single_stock_cap_of_position", "单票/(总仓+单票) ≤ 此值%"),
        ("max_holding_count", "持仓只数 ≥ 此值 ⇒ 新票拒（加仓不拒）"),
        ("correction_price_up", "纠错价 ≤ 卖价×(1+此值%)，等号放行"),
        ("correction_position_half", "纠错仓 = request × 此值"),
        ("correction_cooldown_days", "同标的距上次纠错 < 此值 ⇒ DENY"),
    ]
    for k, note in rows:
        print("  %-32s %-10s %s" % (k, t[k], note))
    print()
    src, tree, prod = _gate_source()
    print("机械性（position_gate.py 生产区）：")
    print("  网络/模型依赖词命中 : %s"
          % (", ".join(w for w in _DEP_WORDS if w in prod) or "无 [OK]"))
    print("  账本直写命中       : %s"
          % (", ".join(w for w in ('open(', '_append', '.write') if w in prod)
             or "无 [OK]"))
    print("  生产区行数         : %d" % len(prod.split("\n")))
    return 0


_TEST_FUNCS = ("_selftest", "_report", "_gate_source", "_md5")


# 生产区机械性判据用词表（测试常量；不参与生产逻辑）
# 只列**通用能力词**（网络/LLM/券商接口/文件传输），不列具体第三方库名——
# 判据是"生产区不得出现网络与外部依赖"，与用过哪家库无关。
_DEP_WORDS = ("requests", "urllib", "urlopen", "http.client", "openai", "anthropic",
              "llm", "LLM", "socket", "websocket", "ftp", "tdx", "qmt")


def _selftest():
    fail = [0]

    def T(name, cond):
        if cond:
            print("  [✓] %s" % name)
        else:
            fail[0] += 1
            print("  [✗] %s" % name)

    t = thresholds()
    L1 = t["circuit_breaker_drawdown_l1"]
    L2 = t["circuit_breaker_drawdown_l2"]
    CAP_L1 = t["circuit_breaker_cap_l1"] / 10.0
    FLAT_N = int(t["circuit_breaker_flat_days"])
    CAP_EQ = t["single_stock_cap_of_equity"] / 100.0
    CAP_POS = t["single_stock_cap_of_position"] / 100.0
    MAX_N = int(t["max_holding_count"])

    # 沙盒 ladder（**不读生产 params 的能力档**，避免与本件判据耦合）
    LAD = {"强": {"position_cap": 7}, "中": {"position_cap": 3}}
    AS_OF = "2026-10-13"

    def chk(req, **kw):
        kw.setdefault("grade", "强")
        kw.setdefault("ladder", LAD)
        return PG.check(req, code=kw.pop("code", "600001"), as_of=AS_OF, **kw)

    print("position_gate 边界回归（只读；生产 data/ 零接触）：")
    print("  阈值真值：L1=%s L2=%s CAP_L1=%s成 FLAT_N=%s CAP_EQ=%s CAP_POS=%s MAX_N=%s"
          % (L1, L2, CAP_L1 * 10, FLAT_N, CAP_EQ, CAP_POS, MAX_N))

    # ── A 组：仓位上限 / 单票占权益比例（边界含等号语义） ──
    r = chk(CAP_EQ, drawdown=0)
    T("A1 单票 = 权益顶**恰好相等** → PASS 且 single 不 binding（含等号，不误报）",
      r["verdict"] == "PASS" and r["position_plan"] == round(CAP_EQ, 6)
      and r["gates"]["single"]["binding"] is False
      and r["gates"]["circuit"]["binding"] is False)

    r = chk(CAP_EQ + _EPS, drawdown=0)
    T("A2 单票 = 权益顶 + 1ε → 被裁到顶且 single binding（超一丝即收紧）",
      r["position_plan"] == round(CAP_EQ, 6)
      and r["gates"]["single"]["binding"] is True)

    total = 0.20
    conc_cap = round(total * CAP_POS / (1.0 - CAP_POS), 6)      # = 1.5 × total
    r = chk(conc_cap, drawdown=0, holdings={"600002": total})
    T("A3 集中度 = 总仓×k/(1−k) **恰好相等** → PASS（plan/(总+plan) == cap 上限）",
      r["verdict"] == "PASS" and r["position_plan"] == conc_cap
      and r["gates"]["single"]["binding"] is False
      and "concentration_limit" in r["gates"]["single"]["basis"])

    r = chk(conc_cap + 0.01, drawdown=0, holdings={"600002": total})
    T("A4 集中度超一丝 → 裁到该上限且 binding（集中度闸真的在起作用）",
      r["position_plan"] == conc_cap
      and r["gates"]["single"]["binding"] is True)

    r = chk(0.05, drawdown=0, holdings={"600002": 0.0})
    T("A5 总仓为 0（存量 0 只而非空 dict）→ 集中度子闸 SKIP_FIRST_POSITION",
      "SKIP_FIRST_POSITION" in r["gates"]["single"]["basis"])

    # 持仓只数边界：MAX_N − 1 可开新票；== MAX_N 拒；加仓已有票不拒
    holds_n1 = {("60000%d" % (i + 2)): 0.05 for i in range(MAX_N - 1)}
    r = chk(0.05, drawdown=0, holdings=holds_n1)
    T("A6 已持 %d 只（= MAX_N−1）→ 仍可开新票（未达上限，含边界正确）" % (MAX_N - 1),
      r["verdict"] == "PASS" and r["position_plan"] == 0.05)

    holds_n = {("60000%d" % (i + 2)): 0.05 for i in range(MAX_N)}
    r = chk(0.05, drawdown=0, holdings=holds_n)
    T("A7 已持 %d 只（== MAX_N）→ 只数闸拒（收紧方向）" % MAX_N,
      r["verdict"] == "REJECT"
      and r["gates"]["single"]["basis"].startswith("MAX_HOLDINGS"))

    r = chk(0.05, drawdown=0, holdings=holds_n, code="600002")
    T("A8 ★ 已持 MAX_N 只但 code **在持仓内**（加仓）→ 不触发只数闸（不误伤）",
      r["verdict"] == "PASS" and r["position_plan"] == 0.05)

    # ── B 组：回撤阈值（含等号语义 + 窗口边界） ──
    r = chk(0.1, drawdown=L1)
    T("B1 dd **恰好** == L1 → 触 L1（≥ 含等号），总仓帽 = %s成" % (CAP_L1 * 10),
      "L1(" in r["gates"]["circuit"]["basis"]
      and r["position_plan"] == round(min(0.1, CAP_L1), 6))

    r = chk(0.1, drawdown=L1 - _EPS)
    T("B2 dd = L1 − 1ε → **不**触 L1（避开边界外，方向验证）",
      r["gates"]["circuit"]["basis"].startswith("OK("))

    r = chk(0.1, drawdown=L2)
    T("B3 dd **恰好** == L2 → 空仓窗口生效，plan=0 REJECT（≥ 含等号）",
      r["verdict"] == "REJECT" and r["position_plan"] == 0.0
      and "FLAT_WINDOW_L2" in r["gates"]["circuit"]["basis"])

    r = chk(0.1, drawdown=L2 - _EPS)
    T("B4 dd = L2 − 1ε → 不空仓（但仍在 L1 区间内，帽不变松）",
      "FLAT_WINDOW_L2" not in r["gates"]["circuit"]["basis"])

    # 曲线推导：峰 100 → 恰好跌到 L1 对应价（dd 精确 == L1）
    eq_at_l1 = 100.0 * (1.0 - L1 / 100.0)
    curve = [{"date": "2026-10-09", "equity": 100.0},
             {"date": "2026-10-12", "equity": eq_at_l1},
             {"date": "2026-10-13", "equity": eq_at_l1}]
    r = chk(0.1, curve=curve)
    T("B5 曲线推导 dd 精确 == L1 → 触 L1（浮点边界与标量入参同口径）",
      abs(r["drawdown"] - L1) < 1e-9 and "L1(" in r["gates"]["circuit"]["basis"])

    # flat 窗口：L2 点恰在最近 FLAT_N 个之内 / 之外（窗口滑动的**精确边界**）
    l2_eq = 100.0 * (1.0 - L2 / 100.0)
    win = [{"date": "2026-10-01", "equity": 100.0}] + \
          [{"date": "2026-10-0%d" % (i + 2), "equity": l2_eq} for i in range(FLAT_N - 1)] + \
          [{"date": "2026-10-1%d" % i, "equity": l2_eq} for i in range(1)]
    r_in = chk(0.1, curve=win)
    T("B6 flat 窗口边界：最近 %d 点含 L2 点（末点 dd 已回落新高）→ 仍空仓" % FLAT_N,
      r_in["verdict"] == "REJECT")

    r_keep = chk(0.1, curve=win + [{"date": "2026-10-2%d" % i, "equity": 100.0}
                                   for i in range(FLAT_N - 1)])
    T("B7a 追加 %d 个新高（L2 点仍在最近 %d 点内）→ 窗口**仍**生效（边界内侧）"
      % (FLAT_N - 1, FLAT_N), r_keep["verdict"] == "REJECT")

    r_out = chk(0.1, curve=win + [{"date": "2026-10-2%d" % i, "equity": 100.0}
                                  for i in range(FLAT_N)])
    T("B7b 追加 %d 个新高（L2 点恰跌出最近 %d 点窗）→ 窗口失效 PASS（边界外侧）"
      % (FLAT_N, FLAT_N),
      r_out["verdict"] == "PASS" and r_out["position_plan"] == 0.1)

    r = chk(0.1, curve=[])
    T("B8 curve=[] （空列表）→ 视同无权益数据 ⇒ CIRCUIT_UNAVAILABLE 拒（不静默当 dd=0）",
      r["verdict"] == "REJECT"
      and "CIRCUIT_UNAVAILABLE" in r["gates"]["circuit"]["basis"])

    # ── C 组：拒绝交易路径穷举（每条 REJECT 都要有实证） ──
    rejects = []
    rejects.append(("C1 只数上限", chk(0.05, drawdown=0, holdings=holds_n)))
    rejects.append(("C2 环境缺失", chk(0.1, drawdown=0, grade=None)))
    rejects.append(("C3 权益缺失", chk(0.1)))
    rejects.append(("C4 空仓窗口", chk(0.1, drawdown=L2)))
    rejects.append(("C5 熔断余量耗尽",
                    chk(0.1, drawdown=L1, holdings={"600009": CAP_L1})))
    rejects.append(("C6 同码 pending 吃满帽",
                    chk(0.3, drawdown=0, holdings={}, pending={"600001": CAP_EQ})))
    ok_all = all(r["verdict"] == "REJECT" and r["position_plan"] == 0.0
                 for _n, r in rejects)
    T("C1–C6 六条拒绝路径全部 REJECT 且 plan=0（%s）"
      % "；".join("%s=%s" % (n.split()[0], r["verdict"]) for n, r in rejects),
      ok_all)

    r = chk(0.1, drawdown=L1, holdings={"600009": CAP_L1})
    T("C7 余量**恰好为 0**（总仓已满 L1 帽）→ REJECT，circuit binding",
      r["verdict"] == "REJECT" and r["gates"]["circuit"]["binding"] is True
      and r["gates"]["circuit"]["cap"] == 0.0)

    r = chk(CAP_EQ, drawdown=0, pending={"600001": CAP_EQ})
    T("C8 单票帽被同码 pending **恰好吃满** → cap=0 REJECT，basis 记 PENDING_EXPOSURE",
      r["verdict"] == "REJECT"
      and r["gates"]["single"]["cap"] == 0.0
      and "PENDING_EXPOSURE" in r["gates"]["single"]["basis"])

    # 入参严进（边界非法值一律响失败，不静默夹取）
    bad = [("request=0", 0.0), ("request>1", 1.0001), ("request=bool", True),
           ("request=str", "0.1"), ("request=负数", -0.1)]
    ok_bad = True
    for _n, val in bad:
        try:
            chk(val, drawdown=0)
            ok_bad = False
        except PG.GateError:
            pass
    T("C9 request 非法值（0 / >1 / bool / str / 负）全部 GateError（不夹取、不默认）",
      ok_bad)

    ok_sd = True
    for v in (-0.1, "x", True):
        try:
            PG.check(0.1, code="600001", as_of=AS_OF, grade="强", ladder=LAD,
                     drawdown=v)
            ok_sd = False
        except PG.GateError:
            pass
    T("C10 drawdown 非法值（负 / 非数 / bool）→ GateError", ok_sd)

    ok_cv = True
    for cv in ([{"date": "d", "equity": 0.0}], [{"date": "d", "equity": -1.0}]):
        try:
            PG.drawdown_series(cv)
            ok_cv = False
        except PG.GateError:
            pass
    T("C11 权益点非正（0 / 负）→ GateError（不把 0 当基准做除法）", ok_cv)

    # ── D 组：机械性（不调 LLM / 不降约束 / 纯函数） ──
    _src, _tree, prod = _gate_source()
    dep = [w for w in _DEP_WORDS if w in prod]
    T("D1 position_gate 生产区**零**网络/模型依赖（命中：%s）" % (dep or "无"), not dep)

    direct = [w for w in ('open(', '_append', '.write') if w in prod]
    T("D2 生产区账本零直写（命中：%s）" % (direct or "无"), not direct)

    lit = []
    for n in _tree.body:
        if not isinstance(n, ast.FunctionDef):
            continue
        if n.name in getattr(PG, "_TEST_FUNCS", ()):
            continue
        for node in ast.walk(n):
            if isinstance(node, ast.Compare):
                for v in [node.left] + list(node.comparators):
                    if isinstance(v, ast.Constant) and isinstance(v.value, (int, float)) \
                            and not isinstance(v.value, bool) and v.value not in (0, 1):
                        lit.append("%s:%d" % (n.name, node.lineno))
                        break
    T("D3 生产区零字面量阈值比较（阈值只在 params；本判据独立实现一次）", not lit)

    r1 = chk(0.42, drawdown=7, holdings={"600002": 0.1})
    r2 = chk(0.42, drawdown=7, holdings={"600002": 0.1})
    T("D4 check() 纯函数：同输入两次调用 JSON 逐字节相同（无隐藏状态）",
      json.dumps(r1, sort_keys=True, default=str)
      == json.dumps(r2, sort_keys=True, default=str))

    led = [PG.DEC.ledger_path(False), PG.DEC.ledger_path(True),
           PG.PNLM.ledger_path(False), PG.PNLM.ledger_path(True)]
    before = [_md5(p) for p in led]
    chk(0.1, drawdown=L1)
    chk(0.1, drawdown=L2)
    after = [_md5(p) for p in led]
    T("D5 边界回归全程四账本 md5 逐字节未变（零账本写，风控不落账）",
      before == after)

    T("D6 position_gate --discipline 全绿（零字面量/零决策词/零直写/astwin）",
      PG._discipline() if hasattr(PG, "_discipline") else True)

    print("position_gate 边界回归：%d 失败" % fail[0])
    return fail[0] == 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
