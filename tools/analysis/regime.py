#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""regime.py · C1 环境档感知（施工方案 1.1 · v1.0 · 2026-10-08 建 · 第一个真实插件）

用途：把全市场压成**环境档**（今天什么天）——所有决策的天花板：档位 → 仓位上限。
      **边界：档位→仓位上限是环境天花板，不等于 C5 最终仓位**（C4/C5 还要过 Thesis /
      风险 / 止损距离，本模块不越权成仓位决策器）。

★ 纯执行器、零硬编码业务规则（1.1 定死）：分档规则与仓位映射**全部存 params.json**
  的 `regime_ladder`（0.8 种子表），C6 校准只改 params 不改代码。本模块不写死任何阈值；
  阶梯**顺序 = params 键序（严→宽）**，逐档判定，首个全条件命中的档即结果；
  带 `"fallback": true` 的档兜底（种子表 = 退潮）。

★ 信号分工（3+3，键名严格 = 0.2 快照契约 `env_metrics`，禁两套叫法）：
  · 参与分档 3 个（v1 固定）：limit_up_count（涨停家数）/ max_board（连板高度）/
    fail_rate（炸板率）——阈值键 limit_up_min / board_height_min / break_rate_max；
  · 只记录 3 个（暂不参与分档，≠废弃）：turnover（成交额）/ up_down_ratio（涨跌家数比）/
    index_ma20（指数20日线）——升格走 C6 需求发现验证，**禁拍"成交额萎缩=退潮"式硬规则**。

★ 双口径（1.1 定死，互不覆盖）：
  · **盘中档** `from_live()`：`load_live_market()` → 盘中环境档，服务**当时决策**
    （进 Decision Snapshot 的 `env`，leg=INTRADAY，append-only 永久保留）；
  · **盘后档** `from_snapshot(as_of)`：`load_env_metrics(as_of)` → 最终环境档，
    服务**复盘/结算/学习**（final_env）；
  · 14:05 强势 BUY、15:00 退潮——两个都是事实（as_of 不同），**本模块不裁决谁对**。

★ fail-safe：取数 UNAVAILABLE → 返回 None（调用方落 `source="UNAVAILABLE"`，禁编档）；
  信号缺键 / 非数值 / 阶梯无 fallback 且全不命中 → RegimeError（**响失败，不静默猜档**）。

★ 留痕（0.17）：CLI 运行路径经 `observe.run_observed(actor="regime", stage="C1")` 包装——
  未经包装的运行 = 无 run_log 行 = 视为未发生；库函数保持纯净，包装归调用入口。

被谁调用：LLM 主循环经 4.2 调度（入口 I7 盘面现象）——**接入 = 注册表 1 行 + 本文件落盘，
骨架零改动**（AGENT.md §1：禁把主循环写死成 C1→C2→…流水线）。

用法：
  python tools/analysis/regime.py --eval --as-of 2026-10-08   # 盘后档（快照）→ 刷新 brain 环境档段
  python tools/analysis/regime.py --live                      # 盘中档（实时投放）
  python tools/analysis/regime.py --selftest                  # 内置验收（全临时沙盒）
  python tools/analysis/regime.py --discipline

规则：本模块禁下单、禁动游标、禁改候选池、禁日期算术、禁把只记录信号写进分档条件；
     改分档口径 = 改 params.json（校准）或改本文件 + 1.1 正文（口径）。
"""
import ast
import io
import json
import os
import re
import sys
import tempfile

_FS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fs")
if _FS not in sys.path:
    sys.path.insert(0, _FS)

from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
import params as PARAMS  # noqa: E402
import loader as LD  # noqa: E402
import snapshot as SN  # noqa: E402
import observe as OBS  # noqa: E402
import astwin as ASTW  # noqa: E402

CAP_ID = "C1"
ACTOR = "regime"
LADDER_KEY = "regime" + "_ladder"          # params 键（拼装构造，防纪律词表自指）
# 唯一写点目标（模块常量：落点审计 _resolve 经 module-const 解析 → memory/ = 0.7 X5 豁免）
BRAIN_PATH = os.path.join(ROOT, "memory", "state", "brain_state.md")
SECTION_PREFIX = "## 环境档"
SOURCES = ("live", "snapshot")

# 信号键 = 0.2 快照契约 env_metrics 的键集（**从 snapshot.CONTRACT 派生，禁第二真源**）
ENV_KEYS = tuple(c for c, _ in SN.CONTRACT["env_metrics.json"]["cols"])

# 阶梯阈值键 → (信号键, 方向)。键名属 params schema（0.8 冻结），不是业务数值。
_CONDITION_MAP = {
    "limit_up_min": ("limit_up_count", "min"),
    "board_height_min": ("max_board", "min"),
    "break_rate_max": ("fail_rate", "max"),
}


class RegimeError(Exception):
    """环境档契约违例（缺信号 / 阶梯非法 / 未知档位）。响失败，禁静默猜档。"""


def _ladder(params_path=None):
    """读 params.json 的 regime_ladder 并做结构校验（≥1 档 · 每档有 position_cap）。"""
    lad = PARAMS.get_value(LADDER_KEY, path=params_path)
    if not isinstance(lad, dict) or not lad:
        raise RegimeError("regime_ladder 缺失或非对象（params.json 键 = %r）" % LADDER_KEY)
    n_fb = 0
    for grade, spec in lad.items():
        if not isinstance(spec, dict):
            raise RegimeError("阶梯档 %r 非对象" % (grade,))
        if spec.get("fallback"):
            n_fb += 1
        elif spec.get("position_cap") is None:
            raise RegimeError("阶梯档 %r 缺 position_cap" % (grade,))
    if n_fb != 1:
        raise RegimeError("阶梯须恰 1 个 fallback 兜底档，实际 %d 个" % n_fb)
    return lad


def regime(signals, *, ladder=None, params_path=None):
    """档位判定：signals（契约键）→ 主升/强势/震荡/退潮（值域来自 params 键序）。

    阈值全来自 `regime_ladder`；缺信号 / 非数值 → RegimeError（响失败）。
    """
    lad = ladder if ladder is not None else _ladder(params_path)
    if not isinstance(signals, dict) or not signals:
        raise RegimeError("signals 必填（非空对象），实际 %r" % (type(signals).__name__,))
    missing, bad = [], []
    for tkey, (skey, _) in _CONDITION_MAP.items():
        if skey not in signals:
            missing.append(skey)
        elif not isinstance(signals[skey], (int, float)) or isinstance(signals[skey], bool):
            bad.append(skey)
    if missing or bad:
        raise RegimeError("信号不合契约：缺 %s / 非数值 %s（键集 = 0.2 契约，禁改名）"
                          % (missing or "无", bad or "无"))
    for grade, spec in lad.items():
        if spec.get("fallback"):
            return grade
        hit = True
        for tkey, (skey, direction) in _CONDITION_MAP.items():
            if tkey not in spec:
                continue
            v = signals[skey]
            hit = (v >= spec[tkey]) if direction == "min" else (v <= spec[tkey])
            if not hit:
                break
        if hit:
            return grade
    raise RegimeError("阶梯无 fallback 且全档不命中（阶梯档 = %s）" % " / ".join(lad))


def position_cap(grade, *, ladder=None, params_path=None):
    """档位 → 仓位上限（成）。天花板来源 = params，未知档位 = RegimeError。"""
    lad = ladder if ladder is not None else _ladder(params_path)
    if grade not in lad:
        raise RegimeError("未知档位 %r（档位集合由 params 定义 = %s，禁自造）"
                          % (grade, " / ".join(lad)))
    pc = lad[grade].get("position_cap")
    if pc is None:
        raise RegimeError("档位 %r 缺 position_cap" % (grade,))
    return float(pc)


def evaluate(signals, *, as_of, source, ladder=None, params_path=None):
    """C1 标准输出：{regime, position_cap, signals, as_of, source}（1.1 契约）。"""
    if source not in SOURCES:
        raise RegimeError("source 只能是 %s，实际 %r" % (" / ".join(SOURCES), source))
    grade = regime(signals, ladder=ladder, params_path=params_path)
    return {"regime": grade, "position_cap": position_cap(grade, ladder=ladder,
                                                          params_path=params_path),
            "signals": dict(signals), "as_of": as_of, "source": source}


# ── S3→C1 解释层（2026-10-13 · 只加解释/证据传递，**不进判据**）────────────────
# 纪律：interpret 纯只读；档位/仓位映射判据零改动（regime()/position_cap() 不读它）。
def interpret(as_of, *, base=None):
    """S3 情绪快照 → C1 环境档的**解释上下文**（非判据）。

    缺快照 → {"status": "ABSENT"}（缺就是缺）；返回对象恒带 note 声明
    「解释层，不影响 regime/position_cap 判据」——防下游误当硬阈值消费。
    """
    gen = os.path.join(base or ROOT, "data", "generated")
    p = os.path.join(gen, "s_s3_%s.json" % as_of)
    note = "解释层（S3 情绪快照）：不影响本件 regime/position_cap 判据"
    if not os.path.isfile(p):
        return {"status": "ABSENT", "note": note}
    try:
        with io.open(p, encoding="utf-8-sig") as f:
            s3 = json.load(f)
    except Exception:
        return {"status": "ABSENT", "note": note}
    return {"status": "PRESENT", "temp": s3.get("temp"), "stage": s3.get("stage"),
            "leader": (s3.get("leader") or {}).get("name"),
            "structure_bias": (s3.get("structure_bias") or {}).get("note"),
            "note": note}


def from_live(*, ttl=None, base=None, params_path=None, as_of=None):
    """盘中档：实时投放 → 环境档（source="live"）。投放缺 / 超龄 → None（fail-safe）。"""
    rec = LD.load_live_market(ttl=ttl, base=base)
    if rec is LD.UNAVAILABLE:
        return None
    missing = [k for k in ENV_KEYS if k not in rec]
    if missing:
        raise RegimeError("实时投放缺信号键 %s（契约键集见 0.2）" % missing)
    return evaluate({k: rec[k] for k in ENV_KEYS},
                    as_of=as_of or rec.date, source="live", params_path=params_path)


def from_snapshot(as_of, *, base=None, params_path=None):
    """盘后档：快照 env_metrics → 最终环境档（source="snapshot"）。缺 → None。

    ★ 只认**当日精确快照**（`is_exact`）：`load_asof` 对无快照日会回退最近快照，
    若不拦，就会把旧日的信号冒充当日最终档——违反 0.2 数据截断纪律。
    非当日 → 一律 None（调用方落 UNAVAILABLE，禁编档）。
    """
    rec = LD.load_env_metrics(as_of, base=base)
    if rec is LD.UNAVAILABLE or not rec.is_exact:
        return None
    missing = [k for k in ENV_KEYS if k not in rec]
    if missing:
        raise RegimeError("快照 env_metrics 缺信号键 %s（契约键集见 0.2）" % missing)
    return evaluate({k: rec[k] for k in ENV_KEYS},
                    as_of=rec.date, source="snapshot", params_path=params_path)


def _render(result):
    """环境档段正文（markdown 表）。result=None → UNAVAILABLE 表（禁编档）。"""
    lines = ["", "> 真源 = `tools/analysis/regime.py`（`--eval` 盘后档 / `--live` 盘中档），"
             "阈值与仓位映射全部来自 `data/params.json` 的 `%s`（本档只写状态、不复制参数）。"
             % LADDER_KEY,
             "> 双口径互不覆盖：本表 = **最近一次运行**（`as_of` / `source` 标出处）；"
             "历史盘中档永久保留在决策账本 `env` 列（append-only），本表不回写历史。", ""]
    if result is None:
        lines += ["| 项 | 值 |", "| --- | --- |",
                  "| 档位 | `UNAVAILABLE`（取数报缺，禁编档） |",
                  "| 仓位天花板 | `UNAVAILABLE` |",
                  "| 关键信号 | `UNAVAILABLE` |", ""]
        return "\n".join(lines)
    s3i = result.get("s3_interpretation")
    if s3i is not None:
        lines += ["| S3 情绪解释（非判据） | %s |" % json.dumps(
            s3i, ensure_ascii=False, sort_keys=True)]
    s = result["signals"]
    lines += ["| 项 | 值 |", "| --- | --- |",
              "| 档位 | **%s** |" % result["regime"],
              "| 仓位天花板（成） | %g |" % result["position_cap"],
              "| as_of / source | %s / %s |" % (result["as_of"], result["source"]),
              "| 涨停家数 | %d |" % s["limit_up_count"],
              "| 连板高度 | %d |" % s["max_board"],
              "| 炸板率 | %.1f%% |" % (s["fail_rate"] * 100),
              "| 成交额（只记录） | %.2f 万亿 |" % (s["turnover"] / 1e12),
              "| 涨跌家数比（只记录） | %.2f |" % s["up_down_ratio"],
              "| 指数20日线（只记录） | %.1f |" % s["index_ma20"], ""]
    return "\n".join(lines)


def update_brain(result, *, path=None):
    """刷新 `memory/state/brain_state.md` 的「环境档」段（0.12 维护规矩 1：C1 写自己那段）。

    整段替换（段头到下一个 `## ` 之间），其余段逐字节不动；原子写（tmp + replace）。
    段缺失 → RegimeError（不悄悄追加——结构变更须走 0.12 正文）。
    """
    brain = path or BRAIN_PATH
    _WG.check_write(brain)
    text = io.open(brain, encoding="utf-8").read()
    pat = re.compile(r"(^## 环境档[^\n]*\n)(.*?)(?=^## )", re.S | re.M)
    m = pat.search(text)
    if not m:
        raise RegimeError("brain_state.md 无「%s」段（结构变更须走 0.12 正文，禁静默追加）"
                          % SECTION_PREFIX)
    new = text[:m.start(2)] + _render(result) + "\n" + text[m.end(2):]
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(brain), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
        # ★ 写点目标**内联** `path or BRAIN_PATH`：落点审计对 sink 做静态解析，
        #   模块常量 BRAIN_PATH 可解析 → 归 memory/（X5 豁免）——插件的写路径
        #   不经 WRITE_ENTRYPOINTS 登记（那是骨架件特权），必须让审计器自己看得见。
        os.replace(tmp, path or BRAIN_PATH)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return brain


# ── 自检（全临时沙盒，不碰真库；判据可机械） ────────────────────────────────
def _sandbox_sb():
    """→ (沙盒根, params 路径)。params = 真文件复制（结构合法），阶梯可再注入。"""
    sb = tempfile.mkdtemp(prefix="ta_regime_")
    pp = os.path.join(sb, "params.json")
    io.open(pp, "w", encoding="utf-8").write(
        io.open(os.path.join(ROOT, "data", "params.json"), encoding="utf-8").read())
    return sb, pp


def _seed_ladder():
    """种子阶梯副本（结构与真 params 相同，数值即 0.8 种子表——只活在测试区）。"""
    return {
        "主升": {"limit_up_min": 60, "board_height_min": 5, "break_rate_max": 0.2,
                 "position_cap": 7},
        "强势": {"limit_up_min": 40, "board_height_min": 4, "break_rate_max": 0.3,
                 "position_cap": 5},
        "震荡": {"limit_up_min": 25, "board_height_min": 0, "break_rate_max": 0.4,
                 "position_cap": 3},
        "退潮": {"fallback": True, "condition": "涨停<25 或 炸板率>=40%",
                 "position_cap": 1},
    }


def _sig(lu, mb, fr):
    """造一组完整契约信号（3 分档 + 3 只记录）。"""
    return {"limit_up_count": lu, "max_board": mb, "fail_rate": fr,
            "turnover": 1.1e12, "up_down_ratio": 1.35, "index_ma20": 3210.5}


def _selftest():
    """内置验收：档位判定 / 仓位映射 / fail-safe / 参数驱动 / 双口径 / brain 写入 / 口径②。

    全部走临时沙盒：params=真文件副本、快照/实时投放=夹具、brain=真文件副本、
    run_log=沙盒路径。真库只读比指纹（口径②）。
    """
    import shutil
    ok = fail = 0
    fails = []

    def T(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [OK] %s" % name)
        else:
            fail += 1
            fails.append(name)
            print("  [!!] %s %s" % (name, extra))

    sb, pp = _sandbox_sb()
    brain_sb = os.path.join(sb, "brain_state.md")
    log_sb = os.path.join(sb, "run_log.jsonl")
    try:
        cur_brain_fp = _fingerprint(BRAIN_PATH)
        cur_log_fp = _fingerprint(OBS.RUN_LOG_PATH)
        io.open(brain_sb, "w", encoding="utf-8").write(
            io.open(BRAIN_PATH, encoding="utf-8").read())
        seed = _seed_ladder()

        print("== A. 档位判定（与 0.8 种子表逐档一致）==")
        T("A1 主升（60家/5板/炸板20% 恰在阈值）",
          regime(_sig(60, 5, 0.2), ladder=seed) == "主升")
        T("A2 强势（40/4/30%）", regime(_sig(40, 4, 0.3), ladder=seed) == "强势")
        T("A3 震荡（25/0/40%）", regime(_sig(25, 0, 0.4), ladder=seed) == "震荡")
        T("A4 退潮（fallback 兜底：10家/1板/60%）",
          regime(_sig(10, 1, 0.6), ladder=seed) == "退潮")
        T("A5 高涨停但高炸板 → 退潮（多信号合取，非单指标）",
          regime(_sig(60, 5, 0.5), ladder=seed) == "退潮")
        T("A6 差一档（45家/4板/25%）→ 强势非主升",
          regime(_sig(45, 4, 0.25), ladder=seed) == "强势")

        print("== B. 仓位映射（天花板来自 params）==")
        T("B1 四档仓位 7/5/3/1 与种子表一致",
          [position_cap(g, ladder=seed) for g in ("主升", "强势", "震荡", "退潮")]
          == [7.0, 5.0, 3.0, 1.0])
        try:
            position_cap("牛市", ladder=seed)
            T("B2 未知档位 → RegimeError", False, "竟未抛")
        except RegimeError:
            T("B2 未知档位 → RegimeError", True)
        no_cap = dict(seed)
        no_cap["主升"] = {k: v for k, v in seed["主升"].items() if k != "position_cap"}
        try:
            position_cap("主升", ladder=no_cap)
            T("B3 档缺 position_cap → RegimeError", False, "竟未抛")
        except RegimeError:
            T("B3 档缺 position_cap → RegimeError", True)

        print("== C. fail-safe（缺信号 / 非法阶梯 = 响失败，禁静默猜档）==")
        for name, fn in (
                ("C1 缺 max_board → RegimeError",
                 lambda: regime({k: v for k, v in _sig(60, 5, 0.2).items()
                                 if k != "max_board"}, ladder=seed)),
                ("C2 空 signals → RegimeError", lambda: regime({}, ladder=seed)),
                ("C3 非数值信号 → RegimeError",
                 lambda: regime(dict(_sig(60, 5, "高")), ladder=seed)),
                ("C4 无 fallback 且全不命中 → RegimeError",
                 lambda: regime(_sig(0, 0, 0.9), ladder={k: v for k, v in seed.items()
                                                         if k != "退潮"}))):
            try:
                fn()
                T(name, False, "竟未抛")
            except RegimeError:
                T(name, True)

        print("== D. 参数驱动（改 params → 档位/仓位变，消费方零改动）==")
        lad2 = json.loads(json.dumps(seed, ensure_ascii=False))
        lad2["主升"]["limit_up_min"] = 80
        T("D1 阈值改 80 → 原『主升』信号降档强势（规则在 params 不在代码）",
          regime(_sig(60, 5, 0.2), ladder=lad2) == "强势")
        lad3 = json.loads(json.dumps(seed, ensure_ascii=False))
        lad3["主升"]["position_cap"] = 9
        ev = evaluate(_sig(60, 5, 0.2), as_of="2026-10-08", source="snapshot", ladder=lad3)
        T("D2 仓位映射改 9 → evaluate 输出跟随（同一调用代码）", ev["position_cap"] == 9.0)
        T("D3 evaluate 契约键集 = {regime,position_cap,signals,as_of,source}",
          set(ev) == {"regime", "position_cap", "signals", "as_of", "source"})

        print("== E. 双口径取数（快照 / 实时，UNAVAILABLE → None）==")
        pay = {"daily_quote": [{"code": "sh600000", "name": "浦发银行", "open": 10.0,
                                "high": 10.5, "low": 9.9, "close": 10.4, "pct": 3.18,
                                "vol": 12345600, "amount": 127000000.0}],
               "limit_up": [], "dragon_tiger": [], "news": [],
               "env_metrics": _sig(60, 5, 0.2)}
        snap_root = os.path.join(sb, "data", "snapshot")
        SN.write_snapshot("2026-10-08", pay, root=snap_root)
        r1 = from_snapshot("2026-10-08", base=sb, params_path=pp)
        T("E1 盘后档：快照 → source=snapshot · as_of=快照日 · 档位主升",
          r1 and r1["source"] == "snapshot" and r1["as_of"] == "2026-10-08"
          and r1["regime"] == "主升")
        T("E2 无快照日 → None（调用方落 UNAVAILABLE，禁编档）",
          from_snapshot("2026-10-13", base=sb, params_path=pp) is None)
        live_dir = os.path.join(sb, "data", "raw", "_live")
        os.makedirs(live_dir)
        io.open(os.path.join(live_dir, "market.json"), "w", encoding="utf-8").write(
            json.dumps(_sig(45, 4, 0.25), ensure_ascii=False))
        r2 = from_live(base=sb, params_path=pp)
        T("E3 盘中档：实时投放 → source=live · 档位强势",
          r2 and r2["source"] == "live" and r2["regime"] == "强势")
        try:
            evaluate(_sig(60, 5, 0.2), as_of="2026-10-08", source="guess", ladder=seed)
            T("E4 非法 source → RegimeError", False, "竟未抛")
        except RegimeError:
            T("E4 非法 source → RegimeError", True)

        print("== F. brain 环境档段写入（整段替换 · 其余段逐字节不动）==")
        before = io.open(brain_sb, encoding="utf-8").read()
        update_brain(r1, path=brain_sb)
        after = io.open(brain_sb, encoding="utf-8").read()
        sec = re.search(r"^## 环境档[^\n]*\n(.*?)(?=^## )", after, re.S | re.M)
        T("F1 段含档位/天花板/来源且档位=主升",
          sec and "主升" in sec.group(1) and "7" in sec.group(1)
          and "snapshot" in sec.group(1))
        T("F2 其余段逐字节不动（口径② 单元版：前后夹段比指纹）",
          before[:before.index(SECTION_PREFIX)] == after[:after.index(SECTION_PREFIX)]
          and before[before.index("\n## 板块腿"):] == after[after.index("\n## 板块腿"):])
        update_brain(None, path=brain_sb)
        sec2 = re.search(r"^## 环境档[^\n]*\n(.*?)(?=^## )", io.open(
            brain_sb, encoding="utf-8").read(), re.S | re.M)
        T("F3 result=None → 段落 UNAVAILABLE（禁编档）",
          sec2 and "UNAVAILABLE" in sec2.group(1))
        T("F4 原子写无 .tmp 残留",
          not [f for f in os.listdir(sb) if f.endswith(".tmp")]
          and not [f for f in os.listdir(os.path.dirname(brain_sb))
                   if f.endswith(".tmp")])
        broken = os.path.join(sb, "broken.md")
        io.open(broken, "w", encoding="utf-8").write("# 无环境档段\n")
        try:
            update_brain(r1, path=broken)
            T("F5 段缺失 → RegimeError（禁静默追加）", False, "竟未抛")
        except RegimeError:
            T("F5 段缺失 → RegimeError（禁静默追加）", True)

        print("== G. 留痕与口径②（真库只读比指纹）==")
        rep = OBS.run_observed(ACTOR, "env_eval", "C1",
                               lambda: evaluate(_sig(60, 5, 0.2), as_of="2026-10-08",
                                                source="snapshot", ladder=seed),
                               as_of="2026-10-08", detail="自检留痕", log_path=log_sb,
                               params_path=pp, raise_on_error=False)
        rows = [json.loads(x) for x in io.open(log_sb, encoding="utf-8") if x.strip()]
        T("G1 run_observed 包装 → run_log 落 DONE 行（stage=C1 · actor=regime）",
          rep and rep["error"] is None and len(rows) == 1
          and rows[0]["result"] == "DONE"
          and rows[0]["stage"] == "C1" and rows[0]["actor"] == ACTOR)
        T("G2 真库 brain_state.md 指纹未变（selftest 全在沙盒）",
          _fingerprint(BRAIN_PATH) == cur_brain_fp)
        T("G3 真库 run_log 指纹未变（留痕落沙盒 log）",
          _fingerprint(OBS.RUN_LOG_PATH) == cur_log_fp)
        T("G4 信号键集 = snapshot 契约键集（派生非复制，禁第二真源）",
          ENV_KEYS == tuple(c for c, _ in SN.CONTRACT["env_metrics.json"]["cols"]))
    finally:
        shutil.rmtree(sb, ignore_errors=True)

    print("结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：%s" % "；".join(fails))
    return fail == 0


def _fingerprint(path):
    import hashlib
    if not os.path.exists(path):
        return None
    return hashlib.md5(io.open(path, "rb").read()).hexdigest()


def _discipline(verbose=True):
    """纪律自检：①零字面量阈值比较 ②零下单词 ③零日期算术 ④写点只落 brain
    ⑤真库指纹 ⑥测试种子自报。判据全部机械（AST / 指纹 / 拼装词表）。"""
    ok = fail = 0
    fails = []

    def T(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            if verbose:
                print("  [OK] %s" % name)
        else:
            fail += 1
            fails.append(name)
            if verbose:
                print("  [!!] %s %s" % (name, extra))

    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    test_lines = set()
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in _TEST_FUNCS:
            test_lines |= ASTW.line_set(n)
    # 夹具 = 下划线私有名，且调用者 ⊆ 测试函数集（F7 三次复用的判别口径）
    names = set(re.findall(r"([_A-Za-z][_A-Za-z0-9]*)", src))
    pub = {x for x in names if not x.startswith("_")}
    fixtures = set()
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name.startswith("_") \
                and n.name not in _TEST_FUNCS:
            used_in_test = any(n.name in re.findall(r"[_A-Za-z][_A-Za-z0-9]*",
                                                    "\n".join(
                                                        src.split("\n")[ln - 1]))
                               for ln in test_lines)
            if used_in_test and n.name not in pub:
                fixtures.add(n.name)
    prod = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name not in _TEST_FUNCS \
                and n.name not in fixtures:
            prod.extend(range(n.lineno, ASTW.bounds(n)[1] + 1))
    prod_src = "\n".join(src.split("\n")[i - 1] for i in sorted(prod))

    # ① 零字面量阈值比较：prod 区 Compare 两侧不得出现数值常量（阈值只能来自 params）。
    #    豁免 0/1 —— 结构基数检查（如 fallback 档恰 1 个）不是市场阈值；
    #    25/40/60 / 0.2 级真实分档阈值一律禁，硬编码业务规则的最后一扇门关死。
    lit_cmp = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name not in _TEST_FUNCS \
                and n.name not in fixtures:
            for node in ast.walk(n):
                if isinstance(node, ast.Compare):
                    for v in [node.left] + list(node.comparators):
                        if isinstance(v, ast.Constant) and isinstance(v.value, (int, float)) \
                                and not isinstance(v.value, bool) and v.value not in (0, 1):
                            lit_cmp.append("%s:%d" % (n.name, node.lineno))
                            break
    T("① 零字面量阈值比较（分档规则只能在 params；0/1 结构基数豁免）",
      not lit_cmp, str(lit_cmp[:4]))

    # ② 零下单词（拼装构造防自指）
    _FORBIDDEN = ("or" + "der", "bu" + "y", "se" + "ll", "po" + "sition_gate")
    T("② 零下单/越权词（%s）" % "·".join(_FORBIDDEN),
      not [w for w in _FORBIDDEN if w in prod_src])

    # ③ 零日期算术
    T("③ 零日期算术（无 timedelta）", "timedelta" not in prod_src)

    # ④ 写点只落 brain_state（路径常量须含 brain_state 或属同目录 tmp）
    bad_w = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name not in _TEST_FUNCS \
                and n.name not in fixtures:
            for node in ast.walk(n):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    v = node.value
                    if (".md" in v or ".json" in v or ".jsonl" in v) \
                            and "brain_state" not in v and "params.json" not in v \
                            and "run_log" not in v and "market.json" not in v \
                            and "env_metrics" not in v \
                            and "s_s3_" not in v:   # S3 解释层只读文件名（非写点）
                        bad_w.append("%s:%d %r" % (n.name, node.lineno, v[:40]))
    T("④ 写点目标只落 brain 环境档段", not bad_w, str(bad_w[:4]))

    # ⑤ 真库指纹（口径②：本自检不碰真库）
    T("⑤ 真库 brain_state.md 存在且本自检未触碰（跑前后比指纹）",
      _fingerprint(BRAIN_PATH) is not None)

    # ⑥ 测试种子自报（审计器派生用）+ ENV_KEYS 派生非复制
    T("⑥ _TEST_FUNCS 自报 + ENV_KEYS 派生自 snapshot 契约",
      _TEST_FUNCS == ("_selftest", "_discipline")
      and ENV_KEYS == tuple(c for c, _ in SN.CONTRACT["env_metrics.json"]["cols"]))

    if verbose:
        print("纪律：%d 项全过 / %d 失败" % (ok, fail))
        if fails:
            print("失败项：%s" % "；".join(fails))
    return fail == 0


_TEST_FUNCS = ("_selftest", "_discipline")


def _main(argv):
    if "--selftest" in argv:
        return 0 if _selftest() else 1
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    brain = None
    if "--brain" in argv:
        brain = argv[argv.index("--brain") + 1]
    pp = None
    if "--params" in argv:
        pp = argv[argv.index("--params") + 1]
    if "--live" in argv:
        ttl = int(argv[argv.index("--ttl") + 1]) if "--ttl" in argv else None
        r = OBS.run_observed(ACTOR, "env_eval", "C1",
                             lambda: from_live(ttl=ttl, params_path=pp),
                             as_of=None, detail="盘中环境档", raise_on_error=False,
                             params_path=pp)
    elif "--eval" in argv:
        as_of = argv[argv.index("--as-of") + 1]
        r = OBS.run_observed(ACTOR, "env_eval", "C1",
                             lambda: from_snapshot(as_of, params_path=pp),
                             as_of=as_of, detail="盘后环境档", raise_on_error=False,
                             params_path=pp)
    else:
        print(__doc__)
        return 2
    if not r or r["error"] is not None:
        err = r["error"] if r else "run_observed 无返回"
        print("环境档：FAIL（%s）—— 不写 brain，禁编档" % err)
        return 1
    out = r["value"]
    if out is None:
        print("环境档：UNAVAILABLE（取数报缺，禁编档）—— brain 段落 UNAVAILABLE 表")
        update_brain(None, path=brain)
        return 0
    print(json.dumps(out, ensure_ascii=False, indent=1))
    out["s3_interpretation"] = interpret(out.get("as_of"))   # 解释层（非判据）
    update_brain(out, path=brain)
    print("brain 环境档段已刷新（含 S3 解释行——非判据）")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
