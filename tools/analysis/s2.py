#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""s2.py · S2 题材生命周期定位（任务9 · v1.0 · 2026-10-12 建）

用途：给**题材/板块**定位生命周期段落——C2 板块侧重 / C3 延续机制 / E10 简报的供料器。
**只供素材，零动作字段**（设计 2.2 批准锁死 4 条全数落实，见 ★四）。S2 ≠ S3：
S3 看**全市场情绪结构**（面），S2 看**单个题材处于哪一段**（点），两者可同时调用、互不替代。

★一、数据来源与口径（全部显式，禁事后改口）
  · 题材宇宙 = 旧包 `data/raw/meta/tdx_sector_constituent.csv`（sector_type=概念）——
    269 个概念板块及其成分股。**成员 = 静态快照，非逐日快照** → v1 口径局限如实标注
    （近窗偏差有限；长历史回演有成分漂移偏差，本库窗口与 S3 对齐只取 2026-06 起）。
  · 题材行情 = 成分股 klines **等权聚合**（真源 = 旧包 `data/raw/bus/klines.csv`，
    与 S3 / real_replay 同源，原语经 `s3` 单点复用、本模块零复制涨停规则）：
      pct        = 成分股当日 change_pct 等权均值（**等权代理，非官方板块指数**）
      breadth    = 上涨成分数 / 有行情成分数
      zt_count   = 成分股当日涨停家数（精确价规则，同 S3）
      max_board  = 成分涨停股最高连板数；n_lb2/n_first/first_share = 梯队结构
      amount     = 成分成交额合计（量能代理）；amount_ma = 前 N 日均额（不含当日）
      leader     = 最高连板成分股（并列取成交额大者）
  · 当日题材截面交叉证据 = 旧包 `data/raw/meta/concept_radar_<date>.csv`（09-11 起 12 日）：
    按**名称精确命中**附 `radar_ref`（主力资金 main1d/main5d、宽度 up_count、龙头 leader）
    ——未命中即 None，不猜、不模糊匹配。
  · 任何指标当日取不到 → None，不补造；依赖它的证据门按未命中处理（如实）。

★二、生命周期六段（机械规则，阈值全部来自 params.s2_rules，本模块零字面量阈值）
  · stage ∈ 发现/发酵/扩散/加速/分歧/尾声（设计 2.2 六段）——**证据门三栏，不是总分打分**：
      每阶段 = 必要证据 / 辅助证据 / 排除反向证据；全命中必要且零反向 → 入档。
      判优顺序固定：尾声>分歧>加速>扩散>发酵>发现（相邻歧义由顺序消解，
      stage_reason 记录全部六门命中/未命中值——判定可解释"为什么是这个阶段
      而不是相邻阶段"）。
  · 周期语义 = 阶段是**状态**、门是**转换**：转换门未命中且紧邻前一日有阶段 →
    状态延续（维持前值）；序列起点/断链 → UNRESOLVED（不强行归档、不跨缺口外推）。
  · **day_n 与 stage 双轨独立、互不推导**（锁死 3）：day_n = **时间状态**（该题材自
    「本轮活跃首现日」起第 N 个交易日，首现判据 = 涨停数由 < theme.active_zt_min →
    ≥ active_zt_min，机械可复现）；stage = **结构状态**。序列窗内找不到首现锚 →
    day_n=None（不猜、不外推）。
  · next_stage_trigger = **可验证事件**（结构化 conditions + 人话 text），
    禁"板块继续走强则进入加速"这类无法机械求值的写法。
  · family = 八族字典（params.b7_families 关键词种子；无命中 → UNKNOWN **不硬归**，
    同 B7「归类不上才新增」）。

★三、历史库（生命周期段 × 族 × T+1/T+3 题材前向涨幅；治理两期分离，同 S1/S3 先例）
  · 行级明细 + 分组统计只进 **X1 提案报告**（data/generated/reports/s2_backfill/），
    人工核对后一次性晋升 data/knowledge/s2_theme_lifecycle/theme_base.json
    + _registry.json 路由（治理动作，代码零知识库写通道）。
  · --history-check 复算已晋升库对账（只读，reproduced 标记）。
  · 题材前向涨幅 = 题材等权涨幅的 T+1/T+3 累加（含市场 beta，与 S1 市场调整 CAR、
    S3 涨停溢价口径均不同属，禁混读）；残窗不入库（窗口任一日缺行情 → 该题材剔除该窗）。
  · 样本不足如实标记：格子 n_days < 30 → sample_ok=False（消费方按
    INSUFFICIENT_SAMPLES 处理，禁出统计结论）。

★四、批准锁死（施工方案 2.2）
  1. 六阶段=证据门不是总分打分；判定必须能解释"为什么是这个阶段而不是相邻阶段"（三栏可查）。
  2. next_stage_trigger 必须是可验证事件（结构化条件），禁自然语言愿望。
  3. DayN 与 stage 双轨独立、互不推导（本模块：day_n 由首现锚定，不读 stage；stage 不读 day_n）。
  4. 历史库"成功/夭折"= 结果标签不是事前规则（v1 只落实际前向涨幅，结果标签由 C6 回填）。
  · S 只提供素材：输出零 action 字段 + _assert_neutral 文本级双保险，不产生任何买卖建议。
  · 时间纪律：日期一律字符串序数比较，零日期算术、零 wall-clock 进快照
    （同输入逐字节同输出，回放确定性）。

★五、落点与写点（landing 封闭）
  · 唯一快照写点 _write_snapshot（X3 豁免 data/generated/s_<cap>_<as_of>.json，
    文件名小写 s_s2_ 与 loader._sid 消费口径一致；fallback 字面量大写 S2 对齐
    注册表 id 的 cap_ref 判据——同 s1.py/s3.py 先例）。
  · X1 提案报告 = data/generated/reports/s2_backfill/summary.json（X1 豁免）。
  · 知识库零写通道（★三）。

用法：
  python tools/analysis/s2.py --eval 2026-09-29 [--stdout]      # 单日题材生命周期快照
  python tools/analysis/s2.py --history-build                   # 历史库 X1 提案
  python tools/analysis/s2.py --history-check                   # 复算对账已晋升库
  python tools/analysis/s2.py --selftest                        # 内置验收（临时沙盒）
  python tools/analysis/s2.py --discipline                      # 纪律判据
"""
import argparse
import csv
import io
import json
import os

_LEGACY_ROOT = (os.environ.get("OLD_SYSTEM_ROOT")
                or os.path.join(os.path.expanduser("~"), "炒股智能体_旧包_20261008"))  # 旧系统只读参考源；换机器时设 OLD_SYSTEM_ROOT 或放家目录
import re
import statistics
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_FS = os.path.normpath(os.path.join(_HERE, "..", "fs"))
for _p in (_FS, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import loader as LD          # noqa: E402
import params as PARAMS      # noqa: E402
from paths import ROOT       # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
import s3 as S3              # noqa: E402  共享行情/涨停原语（一个知识点一个家，本模块零复制）

SCHEMA_VERSION = 1
S_ID = "s2"
# 旧包只读外部输入根（klines / 板块成分 / 概念雷达；同 s1.py/s3.py 先例，禁写禁搬）。
_OLD_ROOT = _LEGACY_ROOT
# 指标序列起算日（与 S3 对齐；早于此拒答 = DATA_INSUFFICIENT）。
SERIES_START = "2026-06-01"
# 行情装载起点（口径常量：起算日前约 62 个交易日，保证窗口余量与 day_n 首现锚余量）。
_LOAD_CUTOFF = "2026-03-01"
# 写点终点 fallback（landing 静态解析要求本模块字面量；真实终点由 evaluate 拼装
# s_s2_<as_of>.json 恒经 path 传入，本常量恒不触发；大小写对齐注册表 id "S2"）。
SNAP_FALLBACK = os.path.join(ROOT, "data", "generated", "s_S2_MISSING.json")
_MAX_CONF = 0.9              # 首批置信度封顶（不出 1.0，同 s1/s3 口径）
# 判优顺序（最"终局"优先）：相邻阶段歧义由顺序消解
_STAGE_ORDER = ("尾声", "分歧", "加速", "扩散", "发酵", "发现")


class S2Error(Exception):
    """评估被拒。str 以 REASON_CODE: 开头（每类拒绝可机械断言）。"""


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _chk_date(v, name):
    s = str(v or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        raise S2Error("BAD_DATE: %s 必须是 YYYY-MM-DD，实际 %r" % (name, v))
    return s


# 成分表 code 口径 = `000002.SZ`（数字.后缀），行情码口径 = `sz000002`（前缀小写）。
# 两套命名空间必须显式归一化——不识别即计次上报，**禁静默丢成员**（否则题材聚合被偏置）。
_CODE_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")


def _norm_code(raw):
    """成分表 code → 行情码；格式不识别 → None（调用方计次上报，不猜不改写）。"""
    m = _CODE_RE.match(str(raw or "").strip().upper())
    return (m.group(2).lower() + m.group(1)) if m else None


# ── 规则装载（阈值唯一来源 = params.s2_rules / 八族 = params.b7_families） ─────────
_RULE_KEY = "s2" + "_rules"          # 拼装构造，防词表自指
_FAM_KEY = "b7" + "_families"


def load_rules(params_path=None):
    v = PARAMS.get_value(_RULE_KEY, None, path=params_path)
    if not isinstance(v, dict):
        raise S2Error("RULES_MISSING: params 缺 %s（阶段阈值/题材口径唯一来源）" % _RULE_KEY)
    for part in ("stage", "theme"):
        if not isinstance(v.get(part), dict):
            raise S2Error("RULES_MISSING: %s.%s 缺失" % (_RULE_KEY, part))
    return v


def load_families(params_path=None):
    v = PARAMS.get_value(_FAM_KEY, None, path=params_path)
    if not isinstance(v, dict) or not isinstance(v.get("keywords"), dict):
        raise S2Error("RULES_MISSING: params 缺 %s（八族字典唯一来源）" % _FAM_KEY)
    return v


def family_of(theme_name, fam):
    """题材名 → 八族码（关键词包含；无命中 → UNKNOWN，不硬归）。"""
    name = theme_name or ""
    for code, kws in sorted(fam["keywords"].items()):
        for kw in kws:
            if kw and kw in name:
                return code
    return "UNKNOWN"


# ── 输入装载（旧包只读） ──────────────────────────────────────────────────────
def _constituent_path(old_root):
    return os.path.join(old_root, *(["data", "raw", "meta", "tdx_sector_constituent.csv"]))


def _radar_path(old_root, date):
    return os.path.join(old_root, *(["data", "raw", "meta",
                                     "concept_radar_%s.csv" % date]))


def load_constituents(old_root=None):
    """概念板块成分 → (themes, code2themes, stats)。

    themes[tcode] = {"name": 板块名, "codes": [行情码...]}（仅 sector_type=概念）。
    成员为**静态快照**（v1 口径局限，见模块头 ★一）。成分码经 _norm_code 归一化，
    不识别格式的行计入 stats["n_code_bad"]（如实上报，不静默丢）。
    """
    p = _constituent_path(old_root or _OLD_ROOT)
    if not os.path.isfile(p):
        raise S2Error("CONSTITUENT_ABSENT: 板块成分表缺席（%s）" % p)
    themes, code2themes = {}, {}
    n_members = n_bad = 0
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if (r.get("sector_type") or "").strip() != "概念":
                continue
            tcode = (r.get("sector_code") or "").strip()
            if not tcode:
                continue
            n_members += 1
            code = _norm_code(r.get("code"))
            if not code:
                n_bad += 1
                continue
            t = themes.setdefault(tcode, {"name": (r.get("sector_name") or "").strip(),
                                          "codes": []})
            t["codes"].append(code)
            code2themes.setdefault(code, []).append(tcode)
    if not themes:
        raise S2Error("CONSTITUENT_EMPTY: 板块成分表零概念板块")
    return themes, code2themes, {"n_members": n_members, "n_code_bad": n_bad}


def load_radar(old_root=None, date=None):
    """概念雷达当日截面 → {名称: {pct, main1d, main5d, up_count, leader}}；缺 → {}。"""
    p = _radar_path(old_root or _OLD_ROOT, date)
    if not os.path.isfile(p):
        return {}
    out = {}
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            nm = (r.get("name") or "").strip()
            if not nm:
                continue
            out[nm] = {"pct": r.get("pct"), "main1d": r.get("main1d"),
                       "main5d": r.get("main5d"), "up_count": r.get("up_count"),
                       "leader": r.get("leader")}
    return out


# ── 题材序列派生（全因果：只用 ≤ D 数据） ──────────────────────────────────────
def derive_theme_series(old_root=None, rules=None, params_path=None):
    """→ (dates, themes, tseries, by_code, stats)。

    tseries[tcode] = 与 dates 对齐的列表；索引 i 处 = 该题材第 i 日指标 dict
    （早于 _LOAD_CUTOFF 的槽位 None）。指标键集见模块头 ★一。
    stats = 成分口径诊断（成员数 / 未识别码数 / 有行情成员数）——如实上报不静默丢。
    """
    rules = rules or load_rules(params_path)
    th = rules["theme"]
    themes, code2themes, cstats = load_constituents(old_root)
    by_code, dates = S3.load_klines(old_root, cutoff=_LOAD_CUTOFF)  # 活库优先（旧包仅 fallback）
    stats = dict(cstats)
    stats["n_members_with_klines"] = sum(1 for c in code2themes if c in by_code)
    idx = {d: i for i, d in enumerate(dates)}
    i_start = idx.get(SERIES_START)
    if i_start is None or i_start < 1:
        raise S2Error("DATA_INSUFFICIENT: 序列起算 %s 不在行情覆盖内" % SERIES_START)
    i_cut = 0
    tseries = {t: [None] * len(dates) for t in themes}
    for i in range(1, len(dates)):
        d = dates[i]
        sealed = S3._sealed_codes(by_code, dates, i)
        agg = {}                       # tcode -> [n_valid, n_up, sum_pct, sum_amount, {sealed}]
        for code, rows in by_code.items():
            row = rows.get(d)
            if row is None:
                continue
            ts = code2themes.get(code)
            if not ts:
                continue
            for t in ts:
                a = agg.get(t)
                if a is None:
                    a = agg[t] = [0, 0, 0.0, 0.0, {}]
                a[0] += 1
                if row[3] > 0:
                    a[1] += 1
                a[2] += row[3]
                a[3] += row[4]
                if code in sealed:
                    a[4][code] = sealed[code]
        for t, a in agg.items():
            n_valid, n_up, s_pct, s_amt, sl = a
            boards = {c: S3._board_n(by_code, dates, i, c) for c in sl}
            n_lb2 = sum(1 for n in boards.values() if n >= 2)
            n_first = len(sl) - n_lb2
            leader = None
            if sl:
                top = max(boards.values())
                cands = sorted(((c, n, sl[c][1]) for c, n in boards.items() if n == top),
                               key=lambda x: (-x[2], x[0]))
                leader = {"code": cands[0][0], "name": sl[cands[0][0]][0],
                          "boards": cands[0][1], "amount": cands[0][2],
                          "tie_n": len(cands)}
            prev_amts = [tseries[t][j]["amount"] for j in
                         range(max(i_cut, i - th["amount_ma_window"]), i)
                         if tseries[t][j]]
            tseries[t][i] = {
                "date": d,
                "n_valid": n_valid,
                "pct": (s_pct / n_valid) if n_valid else None,
                "breadth": (n_up / n_valid) if n_valid else None,
                "zt_count": len(sl),
                "max_board": max(boards.values(), default=0),
                "n_lb2": n_lb2,
                "n_first": n_first,
                "first_share": (n_first / len(sl)) if sl else None,
                "amount": s_amt,
                "amount_ma": (statistics.fmean(prev_amts) if prev_amts else None),
                "leader": leader,
            }
    if not any(x for t in tseries for x in tseries[t]):
        raise S2Error("SERIES_EMPTY: 题材序列零行（成分与行情对不上）")
    return dates, themes, tseries, by_code, stats


# ── 阶段证据门（三栏；阈值 = params.s2_rules.stage；判优顺序固定） ───────────────
def _gate_hit(cond, ok_hint, val):
    """证据门单项 → (命中, 描述串)。val None → 未命中（证据缺席禁放行）。"""
    if val is None:
        return False, "%s 缺证据" % ok_hint
    hit = cond(val)
    return hit, "%s=%s" % (ok_hint, round(val, 4) if isinstance(val, float) else val)


def _bool_hit(flag, desc):
    return bool(flag), "%s%s" % (desc, "命中" if flag else " 未命中")


def stage_of(tseq, i, rules):
    """第 i 日阶段判定 → {stage, reason:[...], gates:{...}}（六门三栏 + 判优）。"""
    st = rules["stage"]
    m = tseq[i]
    m1 = tseq[i - 1] if i >= 1 else None
    m2 = tseq[i - 2] if i >= 2 else None
    win = [x["max_board"] for x in
           tseq[max(0, i - rules["theme"]["max_board_window"]):i] if x]
    win_max = max(win) if win else None
    amt_ratio = None
    if m["amount"] is not None and m["amount_ma"]:
        amt_ratio = m["amount"] / m["amount_ma"]
    gates = {}
    # 发现（必要：涨停出现 + 昨日零涨停（首现） + 板块显著上涨）
    gates["发现"] = {
        "required": [
            _gate_hit(lambda v: v >= st["discover_zt_min"],
                      "涨停≥%d" % st["discover_zt_min"], m["zt_count"]),
            _bool_hit(m1 is not None and m1["zt_count"] == 0, "昨日零涨停（首现）"),
            _gate_hit(lambda v: v >= st["discover_pct_min"],
                      "板块等权涨幅≥%.2f%%" % st["discover_pct_min"], m["pct"])],
        "aux": [_gate_hit(lambda v: v >= st["discover_first_aux"],
                          "首板占比≥%.2f" % st["discover_first_aux"], m["first_share"])]}
    # 发酵（必要：涨停扩至门槛 + 家数扩张 + 高度开启）
    gates["发酵"] = {
        "required": [
            _gate_hit(lambda v: v >= st["ferment_zt_min"],
                      "涨停≥%d" % st["ferment_zt_min"], m["zt_count"]),
            _bool_hit(m1 is not None and m["zt_count"] > m1["zt_count"], "涨停家数扩张"),
            _gate_hit(lambda v: v >= st["ferment_mb_min"],
                      "最高连板≥%d" % st["ferment_mb_min"], m["max_board"])],
        "aux": [_gate_hit(lambda v: v >= st["ferment_breadth_aux"],
                          "宽度≥%.2f" % st["ferment_breadth_aux"], m["breadth"])]}
    # 扩散（必要：参与个股数连续 2 日增加 + 首板占比高）
    gates["扩散"] = {
        "required": [
            _gate_hit(lambda v: v >= st["diffuse_zt_min"],
                      "涨停≥%d" % st["diffuse_zt_min"], m["zt_count"]),
            _bool_hit(m1 is not None and m2 is not None
                      and m["zt_count"] > m1["zt_count"] > m2["zt_count"],
                      "参与个股数连续%d日增加" % st["diffuse_expand_days"]),
            _gate_hit(lambda v: v >= st["diffuse_first_min"],
                      "首板占比≥%.2f" % st["diffuse_first_min"], m["first_share"])],
        "aux": [_gate_hit(lambda v: v >= st["diffuse_breadth_aux"],
                          "宽度≥%.2f" % st["diffuse_breadth_aux"], m["breadth"])]}
    # 加速（必要：涨幅加速 + 高度在 + 涨停簇成规模 + 量能放大；反向：宽度塌陷=高位分化）
    gates["加速"] = {
        "required": [
            _gate_hit(lambda v: v >= st["accel_pct_min"],
                      "板块等权涨幅≥%.2f%%" % st["accel_pct_min"], m["pct"]),
            _gate_hit(lambda v: v >= st["accel_mb_min"],
                      "最高连板≥%d" % st["accel_mb_min"], m["max_board"]),
            _gate_hit(lambda v: v >= st["accel_zt_min"],
                      "涨停≥%d" % st["accel_zt_min"], m["zt_count"]),
            _gate_hit(lambda v: v >= st["accel_amount_ratio_min"],
                      "量能≥%.2f×前均" % st["accel_amount_ratio_min"], amt_ratio)],
        "aux": [_bool_hit(m1 is not None and m["zt_count"] >= m1["zt_count"],
                          "涨停家数不缩")],
        "exclude": [_gate_hit(lambda v: v <= st["accel_narrow_max"],
                              "宽度≤%.2f（反向：高位分化）" % st["accel_narrow_max"],
                              m["breadth"])]}
    # 分歧（必要：涨停家数收缩 + 高度仍在）
    gates["分歧"] = {
        "required": [
            _bool_hit(m1 is not None and m["zt_count"] < m1["zt_count"], "涨停家数收缩"),
            _gate_hit(lambda v: v >= st["diverge_mb_min"],
                      "最高连板≥%d" % st["diverge_mb_min"], m["max_board"])],
        "aux": [_gate_hit(lambda v: v <= st["diverge_breadth_aux"],
                          "宽度≤%.2f" % st["diverge_breadth_aux"], m["breadth"])]}
    # 尾声（必要：高度降 + 涨停簇缩到低位 + 宽度衰落）
    gates["尾声"] = {
        "required": [
            _bool_hit(m1 is not None and m["max_board"] < m1["max_board"], "最高连板较昨日降"),
            _gate_hit(lambda v: v <= st["ending_zt_max"],
                      "涨停≤%d" % st["ending_zt_max"], m["zt_count"]),
            _gate_hit(lambda v: v <= st["ending_breadth_max"],
                      "宽度≤%.2f" % st["ending_breadth_max"], m["breadth"])]}
    for name in _STAGE_ORDER:
        g = gates[name]
        if all(h for h, _ in g["required"]) and not any(h for h, _ in g.get("exclude", [])):
            reason = ["%s: 必要[%s]" % (name, "；".join(s for _, s in g["required"]))]
            for _, s in g.get("aux", []):
                reason.append("%s: 辅助[%s]" % (name, s))
            for _, s in g.get("exclude", []):
                reason.append("%s: %s" % (name, s))
            return name, reason, gates
    return "UNRESOLVED", ["六门必要证据均未全命中（不强行归档）"], gates


def stage_at(tseq, i, rules, prev_stage=None):
    """周期语义层：阶段是**状态**，证据门是**转换**（同 S3 语义）。"""
    stage, reason, gates = stage_of(tseq, i, rules)
    if stage != "UNRESOLVED":
        return stage, reason, gates
    if prev_stage:
        return prev_stage, ["延续: 六门转换证据均未命中，维持前值 %s（状态保持，非当日转换）"
                            % prev_stage], gates
    return stage, reason, gates


def stage_sequence(tseq, i_start, i_end, rules):
    """闭区间逐日阶段序列（含延续链）→ [(stage, reason, gates)]；None 槽位原样占位。"""
    out, prev = [], None
    for i in range(i_start, i_end + 1):
        if not tseq[i]:
            out.append(None)
            continue
        st, reason, gates = stage_at(tseq, i, rules, prev)
        prev = st if st != "UNRESOLVED" else None
        out.append((st, reason, gates))
    return out


# ── DayN（时间状态；独立于 stage，锁死 3） ─────────────────────────────────────
def day_n_of(tseq, i, zt_min):
    """→ (day_n, anchor_date)。首现锚 = 最近一次「涨停数由 < zt_min → ≥ zt_min」
    = **本轮活跃首现日**（zt_min = 盘面存在感门槛，与快照入册口径同源）。
    窗内找不到锚 → (None, None)——不猜、不外推。

    v1 真实数据修正：原锚「由 0 → ≥1」对成员多的大题材几乎不触发（实测 174 只成员的
    题材常年 ≥1 家涨停 → DayN 恒为百余日、或整窗无锚恒 None，"第几天"失去信息量）。
    改锚到存在感门槛后 DayN = 「本波活跃第 N 日」，与 B7 台账口径（这条线第几天）一致；
    阈值仍全外置（本函数只吃参数，不读结构侧——双轨独立不变）。
    """
    if not tseq[i]:
        return None, None
    j = i
    while j >= 1:
        cur, prev = tseq[j], tseq[j - 1]
        if cur is None or prev is None:
            break
        if cur["zt_count"] >= zt_min and prev["zt_count"] < zt_min:
            return (i - j + 1), cur["date"]
        j -= 1
    return None, None


# ── next_stage_trigger（可验证事件；结构化条件 + 人话） ─────────────────────────
def next_trigger_of(stage, rules):
    st = rules["stage"]
    if stage == "发现":
        return {"type": "ferment_confirm",
                "conditions": [{"field": "zt_count", "op": ">=",
                                "value": st["ferment_zt_min"]},
                               {"field": "max_board", "op": ">=",
                                "value": st["ferment_mb_min"]}],
                "text": "题材涨停家数扩至≥%d 且最高连板≥%d → 转发酵"
                        % (st["ferment_zt_min"], st["ferment_mb_min"])}
    if stage == "发酵":
        return {"type": "diffusion_expand",
                "conditions": [{"field": "zt_count", "op": ">=", "value": st["diffuse_zt_min"]},
                               {"field": "first_share", "op": ">=",
                                "value": st["diffuse_first_min"]}],
                "text": "参与个股数连续%d 日增加且首板占比≥%.2f → 转扩散"
                        % (st["diffuse_expand_days"], st["diffuse_first_min"])}
    if stage == "扩散":
        return {"type": "accel_confirm",
                "conditions": [{"field": "pct", "op": ">=", "value": st["accel_pct_min"]},
                               {"field": "max_board", "op": ">=", "value": st["accel_mb_min"]},
                               {"field": "zt_count", "op": ">=",
                                "value": st["accel_zt_min"]}],
                "text": "板块等权涨幅≥%.2f%% 且最高连板≥%d 且涨停≥%d → 转加速"
                        % (st["accel_pct_min"], st["accel_mb_min"], st["accel_zt_min"])}
    if stage == "加速":
        return {"type": "diverge_or_ending",
                "conditions": [{"field": "zt_count", "op": "shrink", "value": None}],
                "text": "涨停家数较昨日收缩：高度仍在（≥%d）→ 分歧；最高连板亦降 → 尾声"
                        % st["diverge_mb_min"]}
    if stage == "分歧":
        return {"type": "ending_or_resume",
                "conditions": [{"field": "max_board", "op": "decrease", "value": None}],
                "text": "最高连板较昨日下降且涨停数≤%d → 尾声；涨停家数回升且高度不减 → 重新发酵"
                        % st["ending_zt_max"]}
    if stage == "尾声":
        return {"type": "rediscover",
                "conditions": [{"field": "zt_count", "op": ">=",
                                "value": st["discover_zt_min"]}],
                "text": "涨停簇再现（涨停≥%d 且首日零涨停后重启）→ 新周期发现"
                        % st["discover_zt_min"]}
    return None


# ── 赔率背景（零动作词枚举） ──────────────────────────────────────────────────
_BIAS = {
    "发现": ("theme_discover", "题材首现，仅作观察起点，梯队未成形"),
    "发酵": ("theme_ferment", "扩散进行中，顺势结构背景"),
    "扩散": ("theme_diffuse", "宽度扩张，跟风参与度上升"),
    "加速": ("theme_accel", "加速段，弹性最好同时拥挤度上升"),
    "分歧": ("theme_diverge", "接力赔率下降，等分歧转一致"),
    "尾声": ("theme_ending", "涨停簇衰竭，禁接力高位"),
    "UNRESOLVED": ("theme_unresolved", "证据门未命中，题材段落不明确"),
}


def _no_overreach(out):
    """零越权断言（锁死）：输出零 action 字段 + 零指令字样。"""
    if "action" in out:
        raise S2Error("VIOLATION: 输出不得含 action 字段（S 只供素材不越权）")
    text = json.dumps(out, ensure_ascii=False)
    if re.search(r'"action"\s*:', text):
        raise S2Error("VIOLATION: 嵌套 action 字段")
    for w in ("建议买入", "建议卖出", "目标仓位", "下单"):
        if w in text:
            raise S2Error("VIOLATION: 输出含越权指令字样 %r" % w)


# ── 主入口 ────────────────────────────────────────────────────────────────────
def evaluate(as_of, *, old_root=None, params_path=None, base=None):
    """S2 单日评估主入口 → 快照落 data/generated/s_s2_<as_of>.json。

    只收录当日 **active**（涨停数 ≥ s2_rules.theme.active_zt_min）的题材——
    无盘面存在感的题材不入快照（不补造、不列全量）。拒绝码：BAD_DATE /
    DATA_END / DATA_INSUFFICIENT / AS_OF_ABSENT / RULES_MISSING。
    同 as_of 幂等覆盖；输出零 wall-clock，同输入逐字节同输出。
    """
    d = _chk_date(as_of, "as_of")
    rules = load_rules(params_path)
    fam = load_families(params_path)
    dates, themes, tseries, _, cstats = derive_theme_series(old_root, rules=rules,
                                                            params_path=params_path)
    if d > dates[-1]:
        # ★ 2026-10-20 加可执行提示（首次当日全链实证：S 系读 bus/klines.csv 单文件，
        #   当日补采落按日 data/raw/<D>/daily_quote.csv ⇒ 忘 merge 时全系报 DATA_END，
        #   但原报错只说"截止较晚"，看不出去哪修。提示按**在场证据**给，不猜）
        _hint = ""
        try:
            _day_q = os.path.join(old_root or ROOT, "data", "raw", d,
                                  "daily_quote.csv")
            if os.path.isfile(_day_q):
                _hint = ("；⚠ 已发现按日行情 data/raw/%s/daily_quote.csv 在盘 ⇒ "
                         "极可能是**未合并进 klines 库**：先跑 "
                         "`tools/e2e/merge_klines.py --from %s --to %s`（须走 writegate），"
                         "再重跑本件" % (d, d, d))
        except Exception:                                        # noqa: BLE001
            pass
        raise S2Error("DATA_END: as_of %s 晚于行情截止 %s（禁编未来结构）%s"
                      % (d, dates[-1], _hint))
    idx = {x: i for i, x in enumerate(dates)}
    if d not in idx:
        raise S2Error("AS_OF_ABSENT: %s 非行情日（数据覆盖 %s..%s）" % (d, dates[0], dates[-1]))
    i = idx[d]
    i_start = idx[SERIES_START]
    if i < i_start + 2:
        raise S2Error("DATA_INSUFFICIENT: %s 早于起算窗（%s+2 交易日）" % (d, SERIES_START))
    act_min = rules["theme"]["active_zt_min"]
    radar = load_radar(old_root, d)
    out_themes, n_unresolved, n_fam_unknown, radar_hit = {}, 0, 0, 0
    for tcode in sorted(themes):
        tseq = tseries[tcode]
        m = tseq[i]
        if not m or m["zt_count"] < act_min:
            continue
        seq = stage_sequence(tseq, i_start, i, rules)
        stage, reason, gates = seq[-1]
        if stage == "UNRESOLVED":
            n_unresolved += 1
        f = family_of(themes[tcode]["name"], fam)
        if f == "UNKNOWN":
            n_fam_unknown += 1
        dn, anchor = day_n_of(tseq, i, act_min)
        missing = [k for k in ("pct", "breadth", "first_share", "amount_ma")
                   if m.get(k) is None]
        conf = round(_MAX_CONF - 0.1 * len(missing), 2)
        ref = radar.get(themes[tcode]["name"])
        if ref:
            radar_hit += 1
        bias_key, bias_note = _BIAS[stage]
        out_themes[tcode] = {
            "theme_id": tcode, "theme_name": themes[tcode]["name"], "family": f,
            "stage": stage, "day_n": dn, "day_n_anchor": anchor,
            "stage_reason": reason,
            "next_stage_trigger": next_trigger_of(stage, rules),
            "structure_bias": {"key": bias_key, "note": bias_note},
            "evidence": {
                "metrics": {k: v for k, v in m.items() if k not in ("date", "leader")},
                "leader": m["leader"],
                "n_constituents": len(themes[tcode]["codes"]),
                "gates": gates,
                "missing_metrics": missing,
                "radar_ref": ref,
            },
            "confidence": conf,
        }
    out = {
        "schema": SCHEMA_VERSION, "s_id": S_ID, "cap": "S2", "as_of": d,
        "n_themes": len(out_themes), "n_unresolved": n_unresolved,
        "n_family_unknown": n_fam_unknown,
        "n_radar_matched": radar_hit,
        "universe": {"n_concept_sectors": len(themes),
                     "n_members": cstats["n_members"],
                     "n_members_with_klines": cstats["n_members_with_klines"],
                     "n_code_bad": cstats["n_code_bad"],
                     "active_zt_min": act_min,
                     "series_start": SERIES_START,
                     "caliber": "题材行情 = 成分股等权聚合（非官方板块指数；成分=静态快照）"},
        "themes": out_themes,
        "confidence": round(_MAX_CONF - 0.05 * (1 if not radar else 0), 2),
    }
    _no_overreach(out)
    gen_dir = os.path.join(base or ROOT, "data", "generated")
    snap_p = os.path.join(gen_dir, "s_%s_%s.json" % (S_ID, d))
    _write_snapshot(out, path=snap_p)
    return {"status": "OK", "output": out, "snapshot": snap_p}


# ── 唯一快照写点（X3；mkstemp 形态同 s1/s3） ─────────────────────────────────
def _write_snapshot(snap, path=None):
    _WG.check_write(path or SNAP_FALLBACK)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path or SNAP_FALLBACK),
                               prefix="s_s2_", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path or SNAP_FALLBACK)


# ── 历史库构建（X1 提案；知识库零写通道） ─────────────────────────────────────
def _theme_fwd(tseq, i, k):
    """题材等权涨幅 T+k 累加（残窗剔除）；缺 → None。"""
    vals = []
    for j in range(i + 1, min(i + 1 + k, len(tseq))):
        m = tseq[j]
        if m is None or m["pct"] is None:
            return None
        vals.append(m["pct"])
    return round(sum(vals), 4) if len(vals) == k else None


def build_history(old_root=None, params_path=None, base=None):
    """全序列 → 每(题材,日)行 + 阶段×族分组统计 → X1 提案报告。"""
    rules = load_rules(params_path)
    fam = load_families(params_path)
    dates, themes, tseries, _, cstats = derive_theme_series(old_root, rules=rules,
                                                            params_path=params_path)
    idx = {x: i for i, x in enumerate(dates)}
    i_start = idx[SERIES_START]
    rows = []
    for tcode in sorted(themes):
        tseq = tseries[tcode]
        if not tseq[i_start]:
            continue
        seq = stage_sequence(tseq, i_start, len(dates) - 1, rules)
        f = family_of(themes[tcode]["name"], fam)
        for k, i in enumerate(range(i_start, len(dates))):
            m = tseq[i]
            if not m or seq[k] is None:
                continue
            stage = seq[k][0]
            rows.append({"date": m["date"], "theme_id": tcode,
                         "theme_name": themes[tcode]["name"], "family": f,
                         "stage": stage, "zt_count": m["zt_count"],
                         "max_board": m["max_board"], "pct": m["pct"],
                         "breadth": m["breadth"],
                         "fwd_t1_pct": _theme_fwd(tseq, i, 1),
                         "fwd_t3_pct": _theme_fwd(tseq, i, 3)})
    groups, fam_groups = {}, {}
    for r in rows:
        if r["fwd_t1_pct"] is None:
            continue
        g = groups.setdefault(r["stage"], {"n_days": 0, "t1": [], "t3": []})
        g["n_days"] += 1
        g["t1"].append(r["fwd_t1_pct"])
        if r["fwd_t3_pct"] is not None:
            g["t3"].append(r["fwd_t3_pct"])
        fg = fam_groups.setdefault(r["family"], {"n_days": 0, "t1": [], "by_stage": {}})
        fg["n_days"] += 1
        fg["t1"].append(r["fwd_t1_pct"])
        bs = fg["by_stage"].setdefault(r["stage"], {"n_days": 0, "t1": []})
        bs["n_days"] += 1
        bs["t1"].append(r["fwd_t1_pct"])
    n_min = 30

    def _agg(g):
        # 阶段分组有 t1/t3 两列；族×阶段子视图只留 t1（缺列按缺席处理，不造数）
        t1 = g.get("t1") or []
        t3 = g.get("t3") or []
        return {"n_days": g["n_days"],
                "fwd_t1_mean_pct": round(statistics.fmean(t1), 3) if t1 else None,
                "fwd_t3_mean_pct": round(statistics.fmean(t3), 3) if t3 else None,
                "sample_ok": g["n_days"] >= n_min}

    out_groups = {k: _agg(g) for k, g in sorted(groups.items())}
    out_fam = {}
    for k, g in sorted(fam_groups.items()):
        d = _agg(g)
        d["by_stage"] = {s: _agg(v) for s, v in sorted(g["by_stage"].items())}
        out_fam[k] = d
    proposal = {
        "schema": 1, "s_id": S_ID, "cap": "S2", "kind": "HISTORY_PROPOSAL",
        "as_of": rows[-1]["date"] if rows else None, "data_end": dates[-1],
        "n_rows": len(rows),
        "n_unresolved": sum(1 for r in rows if r["stage"] == "UNRESOLVED"),
        "n_min_trust": n_min,
        "method": {
            "theme_series": "成分股等权聚合（等权涨幅/宽度/涨停数/连板高度/成交额）；"
                            "成分=静态快照（v1 局限）",
            "forward": "题材等权涨幅 T+1/T+3 累加（含市场 beta；与 S1 市场调整 CAR、"
                       "S3 涨停溢价口径均不同属，禁混读）",
            "residual": "窗口任一日缺行情该题材剔除该窗（残窗不入库）",
            "stage_gate": "证据门三栏（必要/辅助/反向），阈值 = params.s2_rules；"
                          "周期语义=阶段是状态、门是转换：转换门未命中且前值有阶段 → "
                          "状态延续（断链即 UNRESOLVED，不跨缺口外推）",
            "grouping": "v1 = 阶段 与 族×阶段 两视图；行级明细保留全字段可再分组"},
        "universe": {"n_concept_sectors": len(themes),
                     "n_members": cstats["n_members"],
                     "n_members_with_klines": cstats["n_members_with_klines"],
                     "n_code_bad": cstats["n_code_bad"]},
        "groups": out_groups, "family_groups": out_fam,
        "rows_head": rows[:12],
    }
    os.makedirs(os.path.join(base or ROOT, "data", "generated", "reports",
                             "s2_backfill"), exist_ok=True)
    # 写点终点内联（landing 静态解析要求；X1 豁免 data/generated/reports/ 前缀）
    # ★ 2026-10-19 修：原实现漏过 writegate——`data/` 全境受护（PROTECTED），
    #   landing 的 X1 是落点分类、不是写闸豁免；生产根写入一律 check_write。
    #   ⚠ 路径必须保持**字面量内联**（禁提变量）——landing 静态分析只认内联终点。
    _WG.check_write(os.path.join(base or ROOT, "data", "generated", "reports",
                                 "s2_backfill", "summary.json"))
    with io.open(os.path.join(base or ROOT, "data", "generated", "reports",
                              "s2_backfill", "summary.json"),
                 "w", encoding="utf-8", newline="\n") as f:
        json.dump(proposal, f, ensure_ascii=False, indent=1)
    return {"report": os.path.join(base or ROOT, "data", "generated", "reports",
                                   "s2_backfill", "summary.json"),
            "proposal": proposal}


def history_check(old_root=None, params_path=None, base=None):
    """复算对账已晋升知识库（只读）：库内 groups 与复算 groups 逐格比对。"""
    rec = LD.load_knowledge(S_ID, "9999-12-31", base=base or ROOT)
    if rec is LD.UNAVAILABLE:
        raise S2Error("KB_ABSENT: s2 知识库未晋升或路由缺席")
    kb = dict(rec)
    if not kb.get("groups"):
        raise S2Error("KB_SHAPE: 已晋升库缺 groups")
    rep = build_history(old_root, params_path, base=base)["proposal"]
    diffs = []
    for stage, g in kb.get("groups", {}).items():
        r = rep["groups"].get(stage)
        if r is None:
            diffs.append({"stage": stage, "kind": "ABSENT_IN_RECOMPUTE"})
            continue
        for f in ("n_days", "fwd_t1_mean_pct"):
            if g.get(f) != r.get(f):
                diffs.append({"stage": stage, "field": f,
                              "kb": g.get(f), "recomputed": r.get(f)})
    return {"reproduced": not diffs, "diffs": diffs,
            "kb_as_of": kb.get("as_of"), "recomputed_as_of": rep["as_of"]}


def read_themes(as_of, base=None):
    """当日题材快照真链路读取（loader.load_s_output；缺 → UNAVAILABLE 原样返回）。"""
    return LD.load_s_output(S_ID, _chk_date(as_of, "as_of"), base=base)


# ── CLI ───────────────────────────────────────────────────────────────────────
def _main(argv):
    ap = argparse.ArgumentParser(description="S2 题材生命周期定位")
    ap.add_argument("--eval", metavar="AS_OF")
    ap.add_argument("--history-build", action="store_true")
    ap.add_argument("--history-check", action="store_true")
    ap.add_argument("--old-root")
    ap.add_argument("--params")
    ap.add_argument("--base", help="数据根覆盖（沙盒；空 = 生产，生产写入须过 writegate）")
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--discipline", action="store_true")
    a = ap.parse_args(argv)
    if a.base and not os.path.isdir(a.base):
        print("--base 必须是已存在目录（防误写生产），实际: %s" % a.base)
        return 2
    if a.selftest:
        return 0 if _selftest() else 1
    if a.discipline:
        return 0 if _discipline() else 1
    if a.eval:
        r = evaluate(a.eval, old_root=a.old_root, params_path=a.params,
                     base=a.base)
        o = r["output"]
        if a.stdout:
            print(json.dumps(o, ensure_ascii=False, indent=1))
        print("快照: %s" % r["snapshot"])
        print("题材 %d 个（UNRESOLVED %d · 族未归 %d · 雷达命中 %d）"
              % (o["n_themes"], o["n_unresolved"], o["n_family_unknown"],
                 o["n_radar_matched"]))
        for t, v in sorted(o["themes"].items(),
                           key=lambda kv: (-kv[1]["evidence"]["metrics"]["zt_count"],
                                           kv[0]))[:10]:
            print("  %-10s %-8s %-6s DayN=%-4s zt=%-3d 高度=%d 涨幅=%s"
                  % (v["theme_id"], v["theme_name"], v["stage"], v["day_n"],
                     v["evidence"]["metrics"]["zt_count"],
                     v["evidence"]["metrics"]["max_board"],
                     round(v["evidence"]["metrics"]["pct"] or 0, 2)))
        return 0
    if a.history_build:
        r = build_history(a.old_root, a.params, base=a.base)
        p = r["proposal"]
        print("X1 提案: %s" % r["report"])
        print("覆盖 %d 行（UNRESOLVED %d）；data_end=%s"
              % (p["n_rows"], p["n_unresolved"], p["data_end"]))
        for k, g in p["groups"].items():
            print("  %-10s n=%3d%s T1=%s T3=%s" % (
                k, g["n_days"], "" if g["sample_ok"] else "（样本不足）",
                g["fwd_t1_mean_pct"], g["fwd_t3_mean_pct"]))
        return 0
    if a.history_check:
        r = history_check(a.old_root, a.params, base=a.base)
        print("对账: %s（kb_as_of=%s, 复算 as_of=%s, diffs=%d）"
              % ("reproduced ✅" if r["reproduced"] else "有差异 ❌",
                 r["kb_as_of"], r["recomputed_as_of"], len(r["diffs"])))
        for x in r["diffs"][:8]:
            print("  ", x)
        return 0 if r["reproduced"] else 1
    ap.print_help()
    return 2


# ── 自检（临时沙盒，不碰真库/生产区） ─────────────────────────────────────────
def _selftest(verbose=True):
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

    def seed_rules():
        return {
            "stage": {"discover_zt_min": 1, "discover_pct_min": 1.0,
                      "discover_first_aux": 0.6,
                      "ferment_zt_min": 3, "ferment_mb_min": 2, "ferment_breadth_aux": 0.5,
                      "diffuse_zt_min": 5, "diffuse_expand_days": 2, "diffuse_first_min": 0.5,
                      "diffuse_breadth_aux": 0.6,
                      "accel_pct_min": 3.0, "accel_mb_min": 3, "accel_zt_min": 5,
                      "accel_amount_ratio_min": 1.1, "accel_narrow_max": 0.35,
                      "diverge_mb_min": 3, "diverge_breadth_aux": 0.5,
                      "ending_zt_max": 2, "ending_breadth_max": 0.35},
            "theme": {"active_zt_min": 1, "max_board_window": 20,
                      "amount_ma_window": 5},
        }

    def seed_fam():
        return {"keywords": {"A": ["科技", "半导体", "元件"], "C": ["地产", "房地产"],
                             "D": ["军工"]},
                "families": {"A": {"name": "产业0→1"}, "C": {"name": "政策救助"},
                             "D": {"name": "海外映射"}},
                "note_scope": "s2 自检夹具"}

    def mk(n, **fixed):
        base = {"date": "d", "n_valid": 10, "pct": 0.5, "breadth": 0.4,
                "zt_count": 0, "max_board": 0, "n_lb2": 0, "n_first": 0,
                "first_share": None, "amount": 1e9, "amount_ma": 1e9, "leader": None}
        base.update(fixed)
        return [dict(base, date="d%d" % k) for k in range(n)]

    tmp = tempfile.mkdtemp(prefix="s2_selftest_")
    old_root = os.path.join(tmp, "old")
    proj = os.path.join(tmp, "proj")
    os.makedirs(os.path.join(proj, "data", "generated"), exist_ok=True)
    os.makedirs(os.path.join(old_root, "data", "raw", "meta"), exist_ok=True)
    os.makedirs(os.path.join(old_root, "data", "raw", "bus"), exist_ok=True)

    # ── A. 阶段证据门 + day_n + next_trigger（合成序列直喂，精确可控） ──
    rules = seed_rules()
    T("A1 发现（首现+显著上涨）",
      stage_of(mk(3, zt_count=0)[:2] + mk(1, zt_count=2, pct=2.5, max_board=1,
                                         first_share=0.8), 2, rules)[0] == "发现")
    ser = mk(3, zt_count=4, max_board=2, pct=1.0, breadth=0.6,
             n_lb2=1, n_first=3, first_share=0.75)
    ser[1]["zt_count"] = 2
    ser[1]["max_board"] = 1
    T("A2 发酵（扩至门槛+扩张+高度）", stage_of(ser, 2, rules)[0] == "发酵")
    ser = mk(3, zt_count=6, max_board=2, pct=1.0, breadth=0.7,
             n_lb2=1, n_first=5, first_share=0.85)
    ser[0]["zt_count"], ser[1]["zt_count"] = 3, 5
    T("A3 扩散（连增2日+首板占比高）", stage_of(ser, 2, rules)[0] == "扩散")
    ser = mk(3, zt_count=6, max_board=3, pct=4.0, breadth=0.8,
             amount=1.5e9, amount_ma=1e9, n_lb2=2, n_first=4, first_share=0.7)
    ser[0]["zt_count"], ser[1]["zt_count"] = 5, 5
    T("A4 加速（涨幅+高度+量能）", stage_of(ser, 2, rules)[0] == "加速")
    ser = mk(3, zt_count=2, max_board=3, pct=4.0, breadth=0.8,
             amount=1.5e9, amount_ma=1e9)
    T("A5 加速反向（涨停簇不足）→ 不落加速",
      stage_of(ser, 2, rules)[0] != "加速")
    ser = mk(3, zt_count=3, max_board=4, pct=1.0, breadth=0.45)
    ser[1]["zt_count"] = 6
    ser[1]["max_board"] = 4
    T("A6 分歧（家数缩+高度仍在）", stage_of(ser, 2, rules)[0] == "分歧")
    ser = mk(3, zt_count=1, max_board=1, pct=-2.0, breadth=0.2)
    ser[1]["zt_count"], ser[1]["max_board"] = 4, 4
    T("A7 尾声（高度降+簇缩+宽度衰）", stage_of(ser, 2, rules)[0] == "尾声")
    ser = mk(3, zt_count=2, max_board=1, pct=0.1, breadth=0.45)
    T("A8 六门未命中 → UNRESOLVED", stage_of(ser, 2, rules)[0] == "UNRESOLVED")
    _st, _rs, _ = stage_at(ser, 2, rules, prev_stage="发酵")
    T("A9 状态延续（转换门未命中→维持前值）",
      _st == "发酵" and "延续" in _rs[0])
    T("A10 状态延续（前值缺→仍 UNRESOLVED）",
      stage_at(ser, 2, rules, prev_stage=None)[0] == "UNRESOLVED")

    # day_n：首现锚
    ser = mk(6, zt_count=0)
    ser[2]["zt_count"] = 1
    ser[3]["zt_count"] = 2
    ser[4]["zt_count"] = 0
    ser[5]["zt_count"] = 3
    T("A11 day_n 首现锚（取最近一次 0→≥1，非最早一次）",
      day_n_of(ser, 5, 1) == (1, "d5") and day_n_of(ser, 3, 1) == (2, "d2"))
    T("A12 day_n 锚在窗前 → None（不猜）", day_n_of(ser, 2, 1)[0] is not None
      and day_n_of(mk(3, zt_count=1), 2, 1)[0] is None)
    # 双轨独立：day_n 不读 stage、stage 不读 day_n（同序列改 stage 不影响 day_n）
    ser2 = [dict(x) for x in ser]
    T("A13 day_n 与 stage 双轨独立（day_n 不吃 stage）",
      day_n_of(ser2, 5, 1)[0] == day_n_of(ser, 5, 1)[0])
    # 锚 = 存在感再进入（阈值可配；真实数据修正：0→≥1 对大题材失真）
    ser = mk(6, zt_count=0)
    ser[2]["zt_count"], ser[3]["zt_count"] = 6, 7
    ser[4]["zt_count"] = 2                 # 掉出存在感
    ser[5]["zt_count"] = 5                 # 重新进入
    T("A17 day_n 锚 = 存在感再进入（zt_min 可配，与 stage 无关）",
      day_n_of(ser, 5, 5) == (1, "d5") and day_n_of(ser, 3, 5) == (2, "d2")
      and day_n_of(ser, 5, 1) == (4, "d2"))
    tr = next_trigger_of("扩散", rules)
    T("A14 next_stage_trigger 结构化可验证（conditions 非空）",
      tr and tr["type"] == "accel_confirm" and all(
          set(c) >= {"field", "op", "value"} for c in tr["conditions"]))
    T("A15 next_stage_trigger UNRESOLVED → None",
      next_trigger_of("UNRESOLVED", rules) is None)
    ser = mk(3, zt_count=6, max_board=3, pct=4.0, breadth=0.20,
             amount=1.5e9, amount_ma=1e9)
    ser[0]["zt_count"], ser[1]["zt_count"] = 5, 5
    T("A16 加速反向栏（宽度塌陷=高位分化）→ 不落加速",
      stage_of(ser, 2, rules)[0] != "加速")

    # ── B. 端到端（沙盒 klines + 成分 + params + 快照真链路） ──
    import datetime as _dtm
    days = []
    dd = _dtm.date.fromisoformat("2026-03-02")
    while len(days) < 60:
        if dd.weekday() < 5:
            days.append(dd.isoformat())
        dd += _dtm.timedelta(days=1)
    globals()["SERIES_START"] = days[45]          # 沙盒起算日注入（真值 = 2026-06-01）
    codes = ["sh600001", "sh600002", "sh600003", "sh600004"]

    def _ext(c):                      # 行情码 → 成分表真源口径（'600001.SH'）
        return c[-6:] + "." + c[:2].upper()

    with io.open(os.path.join(old_root, "data", "raw", "meta",
                              "tdx_sector_constituent.csv"), "w",
                 encoding="utf-8", newline="\n") as f:
        f.write("sector_code,sector_name,sector_type,code,name\n")
        for c in codes[:3]:
            f.write("880001.SH,科技题材,概念,%s,票%s\n" % (_ext(c), c[-1]))
        f.write("880002.SH,地产题材,概念,%s,票4\n" % _ext(codes[3]))
        f.write("881001.SH,某行业,行业,%s,票1\n" % _ext(codes[0]))
        f.write("880001.SH,科技题材,概念,BOGUS,脏码\n")   # 未识别格式 → 计次上报
    rows = []
    for dname in days[:50]:
        for c in codes:
            rows.append("%s,票%s,%s,10.0,10.0,10.05,9.95,1000,1e7,sz,0.0"
                        % (c, c[-1], dname))
    d1, d2, d3, d4 = days[50], days[51], days[52], days[53]
    # d1：三只成分各板（首现） d2：两只二板 + 一只近板未封 d3：高度 3 板
    rows.append("sh600001,票1,%s,10.5,11.0,11.0,10.4,2000,5e8,sz,10.0" % d1)
    rows.append("sh600002,票2,%s,10.5,11.0,11.0,10.4,2000,5e8,sz,10.0" % d1)
    rows.append("sh600003,票3,%s,10.5,11.0,11.0,10.4,2000,5e8,sz,10.0" % d1)
    rows.append("sh600004,票4,%s,9.9,9.91,10.02,9.90,1000,2e7,sz,0.0" % d1)
    rows.append("sh600001,票1,%s,11.2,12.1,12.1,11.1,3000,9e8,sz,10.0" % d2)
    rows.append("sh600002,票2,%s,11.2,12.1,12.1,11.1,3000,9e8,sz,10.0" % d2)
    rows.append("sh600003,票3,%s,10.2,10.9,10.9,10.1,2000,6e8,sz,9.9" % d2)
    rows.append("sh600004,票4,%s,9.9,9.91,10.02,9.90,1000,2e7,sz,0.0" % d2)
    rows.append("sh600001,票1,%s,12.3,13.31,13.31,12.2,4000,1.4e9,sz,10.0" % d3)
    rows.append("sh600002,票2,%s,12.3,13.31,13.31,12.2,4000,1.2e9,sz,10.0" % d3)
    rows.append("sh600003,票3,%s,11.0,11.99,11.99,11.0,3000,1.5e9,sz,9.9" % d3)
    rows.append("sh600004,票4,%s,9.9,9.91,10.02,9.90,1000,2e7,sz,0.0" % d3)
    rows.append("sh600001,票1,%s,13.5,13.5,13.9,13.4,5000,1.3e9,sz,1.43" % d4)
    rows.append("sh600002,票2,%s,13.4,14.64,14.64,13.3,5000,9e8,sz,10.0" % d4)
    rows.append("sh600003,票3,%s,12.1,13.19,13.19,12.0,4000,1.1e9,sz,10.0" % d4)
    rows.append("sh600004,票4,%s,9.9,9.91,10.02,9.90,1000,2e7,sz,0.0" % d4)
    with io.open(os.path.join(old_root, "data", "raw", "bus", "klines.csv"), "w",
                 encoding="utf-8", newline="\n") as f:
        f.write("code,name,date,open,close,high,low,volume,amount,exchange,change_pct\n")
        for r in rows:
            f.write(r + "\n")
    pfile = os.path.join(tmp, "params.json")
    with io.open(pfile, "w", encoding="utf-8") as f:
        json.dump({"version": "9.9.9", "updated_at": "2026-01-01",
                   "params": {_RULE_KEY: {"value": seed_rules(), "status": "BASELINE"},
                              _FAM_KEY: {"value": seed_fam(), "status": "BASELINE"}}}, f)
    r = evaluate(d2, old_root=old_root, params_path=pfile, base=proj)
    o = r["output"]
    T("B1 端到端 evaluate 状态 OK", r["status"] == "OK")
    T("B2 快照落沙盒 + loader 真链路可读",
      LD.load_s_output("s2", d2, base=proj) is not LD.UNAVAILABLE)
    T("B3 科技题材在册（涨停 2 家：001/002 二板，003 近板未封）",
      o["themes"].get("880001.SH", {}).get("evidence", {}).get("metrics", {})
      .get("zt_count") == 2)
    th = o["themes"].get("880001.SH", {})
    T("B4 龙头 = 最高连板成分股", th.get("evidence", {}).get("leader", {})
      and th["evidence"]["leader"]["code"] in ("sh600001", "sh600002"))
    T("B5 族映射（科技题材→A）", th.get("family") == "A")
    T("B18 族字典零命中 → UNKNOWN（不硬归）",
      family_of("某不存在的题材名xyz", seed_fam()) == "UNKNOWN"
      and family_of("", seed_fam()) == "UNKNOWN")
    T("B6 day_n 首现锚 = d1（第 2 个活跃日）", th.get("day_n") == 2)
    T("B7 地产题材（成分当日零涨停）不入册", "880002.SH" not in o["themes"])
    T("B8 零越权：输出零 action 字段", "action" not in o
      and "action" not in json.dumps(o, ensure_ascii=False))
    T("B9 confidence 封顶", 0 < o["confidence"] <= _MAX_CONF)
    T("B17 成分码归一化（.SZ/.SH → sz/sh）+ 脏码如实上报",
      o["universe"]["n_members_with_klines"] == 4 and o["universe"]["n_code_bad"] == 1)
    with io.open(r["snapshot"], encoding="utf-8") as f:
        b1 = f.read()
    evaluate(d2, old_root=old_root, params_path=pfile, base=proj)
    with io.open(r["snapshot"], encoding="utf-8") as f:
        b2 = f.read()
    T("B10 幂等：同输入逐字节同输出（零 wall-clock）", b1 == b2)
    try:
        evaluate("2026-12-31", old_root=old_root, params_path=pfile, base=proj)
        T("B11 DATA_END 拒答", False)
    except S2Error as e:
        T("B11 DATA_END 拒答", str(e).startswith("DATA_END"))
    try:
        evaluate(days[10], old_root=old_root, params_path=pfile, base=proj)
        T("B12 起算窗前拒答", False)
    except S2Error as e:
        T("B12 起算窗前拒答", str(e).startswith("DATA_INSUFFICIENT"))
    try:
        evaluate("2026-03-07", old_root=old_root, params_path=pfile, base=proj)
        T("B13 非行情日拒答", False)
    except S2Error as e:
        T("B13 非行情日拒答", str(e).startswith("AS_OF_ABSENT"))
    badp = os.path.join(tmp, "bad_params.json")
    with io.open(badp, "w", encoding="utf-8") as f:
        json.dump({"version": "0", "params": {}}, f)
    try:
        evaluate(d2, old_root=old_root, params_path=badp, base=proj)
        T("B14 RULES_MISSING 拒答", False)
    except S2Error as e:
        T("B14 RULES_MISSING 拒答", str(e).startswith("RULES_MISSING"))
    try:
        evaluate(d2, old_root=old_root, params_path=pfile, base=proj)
        _no_overreach({"stage": "加速", "action": "x"})
        T("B15 越权断言触发", False)
    except S2Error as e:
        T("B15 越权断言触发", str(e).startswith("VIOLATION"))
    try:
        _no_overreach({"stage": "加速", "note": "建议买入"})
        T("B16 越权指令词触发", False)
    except S2Error as e:
        T("B16 越权指令词触发", str(e).startswith("VIOLATION"))

    # ── C. day_n / 阶段推进（跨日一致） ──
    r3 = evaluate(d3, old_root=old_root, params_path=pfile, base=proj)
    th3 = r3["output"]["themes"]["880001.SH"]
    T("C1 阶段连续推进（d3 高度更高、段落入档非 UNRESOLVED）",
      th3["stage"] != "UNRESOLVED")
    T("C2 day_n 随日推进 +1", th3["day_n"] == 3)
    T("C3 stage_reason 可解释（六门三栏可查）",
      bool(th3["stage_reason"]) and isinstance(th3["evidence"]["gates"], dict)
      and len(th3["evidence"]["gates"]) == 6)

    # ── D. 历史库构建 + 晋升对账闭环（沙盒知识库） ──
    hb = build_history(old_root, pfile, base=proj)
    T("D1 X1 提案落沙盒 reports/", os.path.isfile(hb["report"]))
    prop = hb["proposal"]
    T("D2 行级覆盖充足", prop["n_rows"] >= 5)
    kbdir = os.path.join(proj, "data", "knowledge", "s2_theme_lifecycle")
    os.makedirs(kbdir, exist_ok=True)
    kb = {"schema": 1, "s_id": "s2", "as_of": prop["as_of"], "data_end": prop["data_end"],
          "groups": prop["groups"], "method": prop["method"]}
    with io.open(os.path.join(kbdir, "theme_base.json"), "w",
                 encoding="utf-8", newline="\n") as f:
        json.dump(kb, f, ensure_ascii=False, indent=1)
    regp = os.path.join(proj, "data", "knowledge", "_registry.json")
    with io.open(regp, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"registry_version": "1.0.0",
                   "s2": {"path": "data/knowledge/s2_theme_lifecycle/theme_base.json",
                          "version": "1.0.0"}}, f)
    hc = history_check(old_root, pfile, base=proj)
    T("D3 晋升后复算对账 reproduced", hc["reproduced"] is True and not hc["diffs"])

    print("自检结果：%s（%d/%d）" % ("通过 ✅" if not fail else "失败 ❌", ok, ok + fail))
    if fails:
        print("  失败项: %s" % fails)
    return not fail


# ── 纪律判据（静态；prod 窗口剔除 _selftest/_discipline/_main） ────────────────
def _discipline(verbose=True):
    import ast
    import astwin as ASTW
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
    lines = src.splitlines(True)
    drop = set()
    for lo, hi, name in ASTW.func_table(ast.parse(src)):
        if name.startswith(("_selftest", "_discipline", "_main")):
            drop.update(range(lo - 1, hi))
    prod = "".join(l for i, l in enumerate(lines) if i not in drop)

    # ① 零核心链 import（S 独立成件；共享原语只许来自 s3，禁碰 C 链与状态对象）
    for mod in ("settle", "wakeup", "three_q", "decision", "position_gate",
                "exit_gate", "orchestrate", "drive", "candidates", "thesis",
                "landing", "observe", "pool_match", "scan", "regime"):
        T("① 零 import %s" % mod,
          not re.search(r"^import\s+%s\b|^from\s+[\w.]*%s\b\s+import" % (mod, mod),
                        prod, re.M))
    # ①b 涨停/行情原语单点复用（不许在本模块复制第二份）
    T("①b 复用 s3 原语（零复制涨停规则）",
      "import s3 as S3" in prod and "S3._sealed_codes(" in prod
      and "round(prev[0]" not in prod)
    # ② 唯一写点形态（mkstemp + 终点内联 fallback）；其余写模式仅 X1 报告 1 处
    _sig = "def _write_snapshot(" + "snap, path=None):"
    _endp = "os.replace(tmp, " + "path or SNAP_FALLBACK)"
    T("② 唯一快照写点（mkstemp 形态）", _sig in prod and _endp in prod)
    T("②b 写模式 io.open 计数 ≤2（快照 + X1 报告）",
      len(re.findall(r'io\.open\([^)]*"[w]', prod)) <= 2)
    # ③ 阈值唯一来源 = params（get_value 经拼装键；模块内零直读 params.json）
    T("③ 阈值经 PARAMS.get_value", "PARAMS.get_value(" in prod)
    T("③b 规则键拼装构造（防词表自指）",
      ('"s2" + "_rules"') in prod and ('"b7" + "_families"') in prod)
    # ④ 零 wall-clock / 零日期算术（回放确定性：输出零时间戳）
    T("④ 零 datetime 依赖", "datetime" not in prod)
    # ⑤ 零越权/下单词（拼装词表，prod 窗口）
    _FORBIDDEN = ("or" + "der", "bu" + "y", "se" + "ll", "wa" + "it",
                  "po" + "sition_gate")
    T("⑤ 零越权词（%s）" % "·".join(_FORBIDDEN),
      not [w for w in _FORBIDDEN if w in prod])
    # ⑥ 快照路径小写对齐 loader._sid；fallback 字面量大写对齐注册表 cap_ref
    T("⑥ 快照文件名口径（s_s2_ + fallback S2 字面量）",
      '"s_%s_%s.json"' in prod and "s_S2_MISSING.json" in prod)
    # ⑦ 知识库零写通道（晋升走治理动作：无对 knowledge 目录的写点）
    T("⑦ 知识库零写点", not re.search(r'open\([^)]*knowledge[^)]*"[w]', prod))
    # ⑧ 双轨独立机械判据：stage 判定函数体内不得读 day_n；day_n 函数体内不得读 stage
    tbl = {n: (lo, hi) for lo, hi, n in ASTW.func_table(ast.parse(src))}
    def _body(fn):
        return "".join(lines[tbl[fn][0] - 1:tbl[fn][1]])
    T("⑧ day_n 不吃 stage（双轨独立）", "stage" not in _body("day_n_of"))
    T("⑧b stage 判定不吃 day_n（双轨独立）",
      "day_n" not in _body("stage_of") and "day_n" not in _body("stage_at"))

    print("纪律结果：%s（%d/%d）" % ("通过 ✅" if not fail else "失败 ❌", ok, ok + fail))
    if fails:
        print("  失败项: %s" % fails)
    return not fail


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
