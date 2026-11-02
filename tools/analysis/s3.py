#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""s3.py · S3 情绪周期判定（任务9#5 · v1.0 · 2026-10-10 建）

用途：市场尺度情绪结构判定——C1 管 Permission（环境风险档），本模块管 Structure
（情绪演化阶段 + 龙头/中军/补涨定位）。输出喂 C1 解释层 / E3 盘后复盘 / C4（赔率
背景），**只供素材，零动作字段**（设计 2.3 批准锁死 3 条全数落实，见 ★四）。

★一、数据来源与口径（全部显式，禁事后改口）
  · 行情 = 旧包 data/raw/bus/klines.csv（只读外部输入，与 C1 回放/S1 回填同源）。
  · 涨停/连板 = 精确价规则（与 real_replay 同一真源）：
      涨停 ⇔ close == round(前收×(1+L), 2)（±0.005 容差）；L 按板块/ST 定档。
      连板数 = 连续涨停交易日数（按全局交易日序，跳日即断）。
  · 炸板率双源（与 real_replay 同策略，逐日标 provenance）：
      zt_matrix 组计数真源（zb/(limitup+zb)）；缺日 → klines 派生替补
      （high ≥ 涨停价−0.005 且 close < 涨停价−0.005 记炸板）。
  · 指标族（全部只用 ≤ D 数据，前向收益只在历史库构建器出现，评估链零前视）：
      zt_count / max_board / n_lb2(连板家数) / n_first(首板家数) / first_share /
      promote_rate(晋级率=昨涨停今再板占比) / yesterday_premium(昨涨今溢=昨涨停票
      今日涨幅均值) / high_pos_feedback(高位负反馈=昨 board≥3 票今日涨幅均值) /
      fail_rate / catchup(新首板中位 45 日位置) / turnover_total。
  · 任何指标当日取不到 → None，不补造；依赖它的证据门按未命中处理（如实）。

★二、温度与阶段（机械规则，阈值全部来自 params.s3_rules，本模块零字面量阈值）
  · temp ∈ 冷/温/热/过热——机械组合（涨停家数×晋级率×高度），口径见 params。
  · stage ∈ 启动/发酵/高潮/分歧/退潮/冰点——**证据门三栏，不是总分打分**：
      每阶段 = 必要证据 / 辅助证据 / 排除反向证据；全命中必要且零反向 → 入档。
      判优顺序固定：冰点>退潮>高潮>分歧>发酵>启动（相邻阶段歧义由顺序消解，
      stage_reason 记录全部六门命中/未命中值——判定可解释"为什么是这个阶段
      而不是相邻阶段"）。周期语义 = 阶段是状态、门是转换：转换门未命中且
      紧邻前一日有阶段 → 状态延续（维持前值）；起点/断链 → UNRESOLVED
      （不强行归档、不跨缺口外推）。
  · temp 与 stage 双轨独立输出（temp=热 + stage=分歧 合法），互不推导。
  · 结构定位（机械）：leader=连板梯队领先者（并列取成交额大者）；catchup=新首板
    低位扩散代理；mid=容量+梯队代理（**板块归属数据缺席，v1 口径局限如实标注**；
    无达标候选 → mid=null 不强指定）。
  · structure_bias：stage→赔率背景枚举（零动作词）。

★三、历史库（阶段×温度 × T+1/T+3 溢价；治理两期分离，同 S1 先例）
  · 行级明细 + 分组统计只进 **X1 提案报告**（data/generated/reports/s3_backfill/），
    人工核对后一次性晋升 data/knowledge/s3_emotion_structure/emotion_base.json
    + _registry.json 路由（治理动作，代码零知识库写通道）。
  · --history-check 复算已晋升库对账（只读，reproduced 标记）。
  · 溢价口径：某日涨停票的 T+1/T+3 前向涨幅（个股自身 pct 累加，非市场调整——
    与 S1 的 CAR 口径不同属，禁混读；情绪溢价本身含市场 beta，读数规则入库内元信息）。
    残窗不入库：窗口内任一日缺行情 → 该票不计入该窗口。
  · 样本不足如实标记：格子 n_days < 30 → sample_ok=False（消费方按
    INSUFFICIENT_SAMPLES 处理，禁出统计结论）。

★四、批准锁死（施工方案 2.3）
  1. C1 管 Permission、S3 管 Structure 禁止互相覆盖：输出零仓位天花板字段、
     零环境档字段，不读写环境档阶梯参数；temp 与 stage 两字段不合并。
  2. 龙头/补涨机械识别；mid 无可靠证据 → null 不强指定。
  3. S3 只提供结构性赔率背景：输出零 action 字段 + _assert_neutral 文本级双保险
     （越权指令词出现即抛错），不产生任何买卖建议。
  · 时间纪律：日期一律字符串序数比较，零日期算术、零 wall-clock 进快照
    （同输入逐字节同输出，回放确定性）。

★五、落点与写点（landing 封闭）
  · 唯一快照写点 _write_snapshot（X3 豁免 data/generated/s_<cap>_<as_of>.json，
    文件名小写 s_s3_ 与 loader._sid 消费口径一致；fallback 字面量大写 S3 对齐
    注册表 id 的 cap_ref 判据——同 s1.py 先例）。
  · X1 提案报告 = data/generated/reports/s3_backfill/summary.json（X1 豁免）。
  · 知识库零写通道（★三）。

用法：
  python tools/analysis/s3.py --eval 2026-09-29 [--stdout]      # 单日结构快照
  python tools/analysis/s3.py --history-build                   # 历史库 X1 提案
  python tools/analysis/s3.py --history-check                   # 复算对账已晋升库
  python tools/analysis/s3.py --selftest                        # 内置验收（临时沙盒）
  python tools/analysis/s3.py --discipline                      # 纪律判据
"""
import argparse
import csv
import io
import json
import os
import re
import statistics
import sys

_LEGACY_ROOT = (os.environ.get("OLD_SYSTEM_ROOT")
                or os.path.join(os.path.expanduser("~"), "炒股智能体_旧包_20261008"))  # 旧系统只读参考源；换机器时设 OLD_SYSTEM_ROOT 或放家目录
import tempfile

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
sys.path.insert(0, _FS)
import loader as LD          # noqa: E402
import params as PARAMS      # noqa: E402
from paths import ROOT       # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）

SCHEMA_VERSION = 1
S_ID = "s3"
# 旧包只读外部输入根（klines / zt_matrix；同 real_replay 先例，禁写禁搬）。
_OLD_ROOT = _LEGACY_ROOT
# ★ 活库优先（2026-10-20 数据层贯通）：默认行情源 = 本库 `data/raw/bus/klines.csv`
#   （10-19 从旧包整份搬入 + merge_klines 追加 10月按日截面）⇒ S 系可见 10月数据；
#   旧包仅在本库文件缺席时 fallback（母库只读不变）。
_DEFAULT_ROOT = ROOT
# 指标序列起算日（连板链与 45 日位置窗的前视余量；早于此拒答 = DATA_INSUFFICIENT）。
SERIES_START = "2026-06-01"
# 行情装载起点（口径常量：起算日前约 62 个交易日，保证 45 日位置窗与连板链余量）。
_LOAD_CUTOFF = "2026-03-01"
# 写点终点 fallback（landing 静态解析要求本模块字面量；真实终点由 evaluate 拼装
# s_s3_<as_of>.json 恒经 path 传入，本常量恒不触发；大小写对齐注册表 id "S3"）。
SNAP_FALLBACK = os.path.join(ROOT, "data", "generated", "s_S3_MISSING.json")
_MAX_CONF = 0.9              # 首批置信度封顶（不出 1.0，同 s1 口径）
_POS_WINDOW = 45             # 45 日位置窗口（口径常量，非标定阈值）
_CLIMAX_WIN = 9              # 高潮门"创近 N 日新高"窗口（口径常量）


class S3Error(Exception):
    """评估被拒。str 以 REASON_CODE: 开头（每类拒绝可机械断言）。"""


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _chk_date(v, name):
    s = str(v or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        raise S3Error("BAD_DATE: %s 必须是 YYYY-MM-DD，实际 %r" % (name, v))
    return s


# ── 规则装载（阈值唯一来源 = params.s3_rules；模块零字面量阈值） ──────────────
_RULE_KEY = "s3" + "_rules"    # 拼装构造，防词表自指

def load_rules(params_path=None):
    v = PARAMS.get_value(_RULE_KEY, None, path=params_path)
    if not isinstance(v, dict):
        raise S3Error("RULES_MISSING: params 缺 %s（温度/阶段阈值唯一来源）" % _RULE_KEY)
    for part in ("temp", "stage", "structure"):
        if not isinstance(v.get(part), dict):
            raise S3Error("RULES_MISSING: %s.%s 缺失" % (_RULE_KEY, part))
    return v


# ── 行情装载（旧包 klines 只读；截止日由数据自报，禁硬编码） ──────────────────
def _klines_path(old_root):
    return os.path.join(old_root, *(["data", "raw", "bus", "klines.csv"]))


def _zt_dir(old_root):
    return os.path.join(old_root, *(["data", "generated", "pattern", "zt_matrix"]))


def load_klines(old_root=None, *, cutoff=None):
    """klines.csv → (by_code, dates)。by_code[code] = {date: (close, high, low,
    pct, amount, name)}；cutoff 之前的日子丢弃（前视余量由 SERIES_START 保证）。

    源解析（2026-10-20 活库优先）：old_root 显式传入 > 本库
    `data/raw/bus/klines.csv` > 旧包 fallback（母库只读，本库缺席时）。
    """
    p = _klines_path(old_root or _DEFAULT_ROOT)
    if not os.path.isfile(p):
        p_fb = _klines_path(_OLD_ROOT)
        if os.path.isfile(p_fb):
            p = p_fb                     # 活库缺席 ⇒ 旧包 fallback
        else:
            raise S3Error("KLINES_ABSENT: 行情源缺席（%s / %s）" % (p, p_fb))
    by_code, dates = {}, {}
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = r["date"]
            if cutoff and d < cutoff:
                continue
            if d not in dates:
                dates[d] = len(dates)
            try:
                row = (float(r["close"]), float(r["high"]), float(r["low"]),
                       float(r["change_pct"]), float(r["amount"]), r["name"])
            except (ValueError, TypeError, KeyError):
                continue
            by_code.setdefault(r["code"], {})[d] = row
    if not dates:
        raise S3Error("KLINES_EMPTY: 行情源零行")
    return by_code, sorted(dates)


def _limit_ratio(code, name):
    """涨停幅定档（与 real_replay._limit_ratio 同规则同真源）。"""
    if code.startswith("bj"):
        return 0.30
    if code.startswith("sh68") or code.startswith("sz30"):
        return 0.20
    if "ST" in (name or "").upper():
        return 0.05
    return 0.10


def _sealed_codes(by_code, dates, i):
    """第 i 日涨停集合 {code: (name, amount)}（精确价规则，±0.005 容差）。"""
    out = {}
    if i < 1:
        return out
    d, d_prev = dates[i], dates[i - 1]
    for code, rows in by_code.items():
        cur = rows.get(d)
        prev = rows.get(d_prev)
        if not cur or not prev:
            continue
        limit_px = round(prev[0] * (1 + _limit_ratio(code, cur[5])), 2)
        if abs(cur[0] - limit_px) < 0.005:
            out[code] = (cur[5], cur[4])
    return out


def _broken_codes(by_code, dates, i, sealed):
    """第 i 日炸板集合（klines 派生替补口径）：触板未封。"""
    out = {}
    if i < 1:
        return out
    d, d_prev = dates[i], dates[i - 1]
    for code, rows in by_code.items():
        if code in sealed:
            continue
        cur = rows.get(d)
        prev = rows.get(d_prev)
        if not cur or not prev:
            continue
        limit_px = round(prev[0] * (1 + _limit_ratio(code, cur[5])), 2)
        if cur[1] >= limit_px - 0.005 and cur[0] < limit_px - 0.005:
            out[code] = cur[5]
    return out


def _zt_groups(zt_dir, date):
    """zt_matrix 组计数 → (limitup 数, 炸板数) or None（炸板率真源，同 real_replay）。"""
    p = os.path.join(zt_dir, "_zt_matrix_%s.csv" % date.replace("-", ""))
    if not os.path.isfile(p):
        return None
    n_lim = n_zb = 0
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            g = r.get("group")
            if g == "limitup":
                n_lim += 1
            elif g == "zb":
                n_zb += 1
    return n_lim, n_zb


def _pos45(rows, di):
    """code 在 di 日的 45 日位置（0=窗口最低，1=窗口最高）；窗口不足/零幅度 → None。"""
    window = [rows[dates_d] for dates_d in sorted(rows) if dates_d <= di][-_POS_WINDOW:]
    if len(window) < _POS_WINDOW:
        return None
    lo = min(w[2] for w in window)
    hi = max(w[1] for w in window)
    rng = hi - lo
    if rng <= 0:
        return None
    return (window[-1][0] - lo) / rng


# ── 逐日指标序列（全因果：只用 ≤ D 数据） ─────────────────────────────────────
def derive_series(old_root=None, rules=None, params_path=None):
    """指标序列 → (dates, series)。series[i] 对应 dates[i]，早于 SERIES_START 的
    槽位为 None；每项 = 逐日指标 dict（键集见模块头 ★一）。"""
    rules = rules or load_rules(params_path)
    by_code, dates = load_klines(old_root, cutoff=_LOAD_CUTOFF)
    idx = {d: i for i, d in enumerate(dates)}
    i_start = idx.get(SERIES_START)
    if i_start is None or i_start < _POS_WINDOW:
        raise S3Error("DATA_INSUFFICIENT: 序列起算 %s 需 ≥%d 个前置交易日"
                      % (SERIES_START, _POS_WINDOW))
    zt_dir = _zt_dir(old_root or _OLD_ROOT)
    series = [None] * len(dates)
    boards = {}                       # code → 连板数（截至前一日）
    for i in range(1, len(dates)):
        d = dates[i]
        sealed = _sealed_codes(by_code, dates, i)
        sealed_prev = _sealed_codes(by_code, dates, i - 1) if i >= 1 else {}
        new_boards = {}
        for code in sealed:
            new_boards[code] = boards.get(code, 0) + 1
        prev_boards = boards
        boards = new_boards
        if i < i_start:
            continue
        # 晋级率 / 昨涨今溢（分母 = 昨日涨停）
        n_prev = len(sealed_prev)
        if n_prev:
            re_seal = [c for c in sealed_prev if c in sealed]
            promote_rate = len(re_seal) / n_prev
            prem = [by_code[c][d][3] for c in sealed_prev if d in by_code.get(c, {})]
            yesterday_premium = statistics.fmean(prem) if prem else None
        else:
            promote_rate, yesterday_premium = None, None
        # 高位负反馈（昨 board≥3 票今日涨幅均值）
        high_prev = [c for c, n in prev_boards.items() if n >= 3]
        hp = [by_code[c][d][3] for c in high_prev if d in by_code.get(c, {})]
        high_pos_feedback = statistics.fmean(hp) if hp else None
        # 炸板率（zt_matrix 真源优先，klines 派生替补；provenance 逐日记录）
        zg = _zt_groups(zt_dir, d)
        if zg is not None and (zg[0] + zg[1]) > 0:
            fail_rate = zg[1] / (zg[0] + zg[1])
            fail_src = "zt_matrix"
        else:
            broken = _broken_codes(by_code, dates, i, sealed)
            den = len(broken) + len(sealed)
            fail_rate = (len(broken) / den) if den else None
            fail_src = "klines_derived"
        n_lb2 = sum(1 for n in new_boards.values() if n >= 2)
        n_first = len(sealed) - n_lb2
        # 补涨代理：新首板（昨未板今板）的"昨日"45 日位置中位数
        # （封板日当日收盘必在窗口高位，位置代理必须取前一日收盘口径才有区分度）
        new_first = [c for c in sealed if c not in prev_boards]
        pos_list = []
        for c in new_first:
            p45 = _pos45(by_code.get(c, {}), dates[i - 1])
            if p45 is not None:
                pos_list.append(p45)
        catchup_median = statistics.median(pos_list) if pos_list else None
        series[i] = {
            "date": d,
            "zt_count": len(sealed),
            "max_board": max(new_boards.values(), default=0),
            "n_lb2": n_lb2,
            "n_first": n_first,
            "first_share": (n_first / len(sealed)) if sealed else None,
            "promote_rate": promote_rate,
            "yesterday_premium": yesterday_premium,
            "high_pos_feedback": high_pos_feedback,
            "fail_rate": fail_rate,
            "fail_src": fail_src,
            "n_new_first": len(new_first),
            "catchup_median_pos45": catchup_median,
            "turnover_total": sum(rows[d][4] for rows in by_code.values() if d in rows),
        }
    return dates, series


# ── 温度（机械组合；阈值 = params.s3_rules.temp） ─────────────────────────────
def temp_of(m, rules):
    """→ (temp, 命中依据串)。缺晋级率时热/过热按未命中处理（如实降档）。"""
    t = rules["temp"]
    zt, pr, mb = m["zt_count"], m["promote_rate"], m["max_board"]
    if zt >= t["zt_over"] and pr is not None and pr >= t["promote_over"] and mb >= t["mb_over"]:
        return "过热", "涨停%d≥%d 且晋级%.2f≥%.2f 且高度%d≥%d" % (
            zt, t["zt_over"], pr, t["promote_over"], mb, t["mb_over"])
    if zt >= t["zt_hot"] and pr is not None and pr >= t["promote_hot"]:
        return "热", "涨停%d≥%d 且晋级%.2f≥%.2f" % (zt, t["zt_hot"], pr, t["promote_hot"])
    if zt >= t["zt_warm"]:
        return "温", "涨停%d≥%d" % (zt, t["zt_warm"])
    return "冷", "涨停%d<%d" % (zt, t["zt_warm"])


# ── 阶段证据门（三栏；阈值 = params.s3_rules.stage；判优顺序固定） ─────────────
_STAGE_ORDER = ("冰点", "退潮", "高潮", "分歧", "发酵", "启动")


def _gate_hit(cond, ok_hint, val):
    """证据门单项 → (命中, 描述串)。val None → 未命中（证据缺席禁放行）。"""
    if val is None:
        return False, "%s 缺证据" % ok_hint
    hit = cond(val)
    return hit, "%s=%s" % (ok_hint, round(val, 4) if isinstance(val, float) else val)


def stage_of(series, i, rules):
    """第 i 日阶段判定 → {stage, reason:[...], gates:{...}}。

    必要全命中 且 零反向命中 → 入档；判优顺序 = _STAGE_ORDER；全未命中 →
    UNRESOLVED。exclude 列的语义 = 反向证据（命中即禁入），与必要/辅助的
    "命中即支持" 相反——描述串统一由 _gate_hit 生成，命中标记含义以栏位为准。
    """
    st = rules["stage"]
    m = series[i]
    m1 = series[i - 1] if i >= 1 and series[i - 1] else None
    m2 = series[i - 2] if i >= 2 and series[i - 2] else None
    win = [series[j]["max_board"] for j in range(max(0, i - _CLIMAX_WIN), i)
           if series[j]]
    win_max = max(win) if win else None
    gates = {}
    # 冰点（必要：家数/高度/晋级三低）
    gates["冰点"] = {"required": [
        _gate_hit(lambda v: v <= st["freeze_zt_max"], "涨停≤%d" % st["freeze_zt_max"],
                  m["zt_count"]),
        _gate_hit(lambda v: v <= st["freeze_mb_max"], "高度≤%d" % st["freeze_mb_max"],
                  m["max_board"]),
        _gate_hit(lambda v: v < st["freeze_promote_max"], "晋级<%.2f" % st["freeze_promote_max"],
                  m["promote_rate"])]}
    # 退潮（必要：高位负反馈 + 家数缩 + 高度降）
    gates["退潮"] = {
        "required": [
            _gate_hit(lambda v: v <= st["ebb_highfeed_max"],
                      "高位反馈≤%g" % st["ebb_highfeed_max"], m["high_pos_feedback"]),
            _gate_hit(lambda v: m1 is not None and v < m1["zt_count"], "涨停较昨日缩",
                      m["zt_count"]),
            _gate_hit(lambda v: m1 is not None and v < m1["max_board"], "高度较昨日降",
                      m["max_board"])],
        "aux": [_gate_hit(lambda v: v < st["ebb_promote_aux"],
                          "晋级<%.2f" % st["ebb_promote_aux"], m["promote_rate"])]}
    # 高潮（必要：高度创近窗新高 + 家数不缩 + 晋级强；反向：炸板极端）
    gates["高潮"] = {
        "required": [
            _gate_hit(lambda v: win_max is not None and v > win_max,
                      "高度创近%d日新高" % _CLIMAX_WIN, m["max_board"]),
            _gate_hit(lambda v: m1 is not None and v >= m1["zt_count"], "涨停不缩",
                      m["zt_count"]),
            _gate_hit(lambda v: v >= st["climax_promote_min"],
                      "晋级≥%.2f" % st["climax_promote_min"], m["promote_rate"])],
        "aux": [_gate_hit(lambda v: v >= st["climax_premium_aux"],
                          "昨溢≥%g" % st["climax_premium_aux"], m["yesterday_premium"])],
        "exclude": [_gate_hit(lambda v: v >= st["climax_fail_max"],
                              "炸板≥%.2f（反向）" % st["climax_fail_max"], m["fail_rate"])]}
    # 分歧（必要：炸板显著抬升 + 高度仍在）
    gates["分歧"] = {
        "required": [
            _gate_hit(lambda v: v >= st["diverge_fail_min"],
                      "炸板≥%.2f" % st["diverge_fail_min"], m["fail_rate"]),
            _gate_hit(lambda v: v >= st["diverge_mb_min"], "高度≥%d" % st["diverge_mb_min"],
                      m["max_board"])],
        "aux": [_gate_hit(lambda v: v < st["diverge_premium_aux"],
                          "昨溢<%g" % st["diverge_premium_aux"], m["yesterday_premium"])]}
    # 发酵（必要：连板家数扩张 + 晋级强 + 高度开启；反向：炸板极端）
    gates["发酵"] = {
        "required": [
            _gate_hit(lambda v: m1 is not None and v > m1["n_lb2"], "连板家数扩张",
                      m["n_lb2"]),
            _gate_hit(lambda v: v >= st["ferment_promote_min"],
                      "晋级≥%.2f" % st["ferment_promote_min"], m["promote_rate"]),
            _gate_hit(lambda v: v >= st["ferment_mb_min"], "高度≥%d" % st["ferment_mb_min"],
                      m["max_board"])],
        "aux": [_gate_hit(lambda v: v >= st["ferment_premium_aux"],
                          "昨溢≥%g" % st["ferment_premium_aux"], m["yesterday_premium"])],
        "exclude": [_gate_hit(lambda v: v >= st["ferment_fail_max"],
                              "炸板≥%.2f（反向）" % st["ferment_fail_max"], m["fail_rate"])]}
    # 启动（必要：涨停连续回升 + 高度尚低 + 晋级回稳）
    rising = (m2 is not None and m1 is not None
              and m["zt_count"] > m1["zt_count"] > m2["zt_count"])
    gates["启动"] = {
        "required": [
            (rising, "涨停连续%d日回升%s" % (st["advance_zt_days"],
                                             "命中" if rising else " 未命中")),
            _gate_hit(lambda v: v <= st["ignite_mb_max"], "高度≤%d" % st["ignite_mb_max"],
                      m["max_board"]),
            _gate_hit(lambda v: v >= st["ignite_promote_min"],
                      "晋级≥%.2f" % st["ignite_promote_min"], m["promote_rate"])],
        "aux": [_gate_hit(lambda v: v >= st["ignite_first_share_min"],
                          "首板占比≥%.2f" % st["ignite_first_share_min"],
                          m["first_share"])]}
    # 判优（必要全命中 且 零反向命中）
    for name in _STAGE_ORDER:
        g = gates[name]
        if all(h for h, _ in g["required"]) and \
                not any(h for h, _ in g.get("exclude", [])):
            reason = ["%s: 必要[%s]" % (name, "；".join(s for _, s in g["required"]))]
            for _, s in g.get("aux", []):
                reason.append("%s: 辅助[%s]" % (name, s))
            for _, s in g.get("exclude", []):
                reason.append("%s: %s" % (name, s))
            return name, reason, gates
    return "UNRESOLVED", ["六门必要证据均未全命中（不强行归档）"], gates


def stage_at(series, i, rules, prev_stage=None):
    """周期语义层：阶段是**状态**，证据门是**转换**。

    当日转换门命中 → 转换（stage_of 结果）；全未命中且前值有阶段 → 状态延续
    （维持前值，reason 记"延续"）；前值也缺（起点/断链）→ UNRESOLVED。
    延续只看紧邻前一日（断链不跨日桥接——保守，不把旧阶段跨缺口外推）。
    """
    stage, reason, gates = stage_of(series, i, rules)
    if stage != "UNRESOLVED":
        return stage, reason, gates
    if prev_stage:
        return prev_stage, ["延续: 六门转换证据均未命中，维持前值 %s"
                            "（状态保持，非当日转换）" % prev_stage], gates
    return stage, reason, gates


def stage_sequence(series, i_start, i_end, rules):
    """闭区间 [i_start, i_end] 逐日阶段序列（含延续链）→ [(stage, reason, gates)]。

    槽位 None（序列未起算日）原样占位；prev 链只在已解析日间传递。
    """
    out = []
    prev = None
    for i in range(i_start, i_end + 1):
        if not series[i]:
            out.append(None)
            continue
        st, reason, gates = stage_at(series, i, rules, prev)
        prev = st if st != "UNRESOLVED" else None
        out.append((st, reason, gates))
    return out


# ── 结构定位（龙头 / 中军 / 补涨；机械，锁死条款 2） ───────────────────────────
def structure_of(by_code, dates, i, sealed, cur_boards, prev_boards, rules):
    """→ {leader, mid, catchup}。

    leader = 当日连板梯队领先者（并列取成交额大者）；mid = 容量+梯队代理
    （板块归属数据缺席 → 口径局限如实标注；无达标候选 → None 不强指定）；
    catchup = 新首板低位扩散代理。
    """
    d = dates[i]
    sc = rules["structure"]
    if not sealed:
        return {"leader": None, "mid": None,
                "catchup": {"active": False, "n_new_first": 0, "n_pos_valid": 0,
                            "median_pos45": None, "note": "当日零涨停"}}
    top = max(cur_boards.values())
    cands = [(c, n, sealed[c][1]) for c, n in cur_boards.items() if n == top]
    cands.sort(key=lambda x: (-x[2], x[0]))
    lc = cands[0]
    leader = {"code": lc[0], "name": sealed[lc[0]][0], "boards": lc[1],
              "amount": lc[2], "tie_n": len(cands)}
    mid = None
    mid_cands = [(c, sealed[c][0], n, sealed[c][1]) for c, n in cur_boards.items()
                 if n >= sc["mid_min_boards"] and sealed[c][1] >= sc["mid_min_amount"]]
    if mid_cands:
        mid_cands.sort(key=lambda x: (-x[3], x[0]))
        mc = mid_cands[0]
        mid = {"code": mc[0], "name": mc[1], "boards": mc[2], "amount": mc[3],
               "caliber_note": "容量+梯队代理；板块核心地位维度数据缺席，v1 口径局限"}
    return {"leader": leader, "mid": mid,
            "catchup": _catchup_of(by_code, d, dates[i - 1] if i >= 1 else d,
                                   sealed, prev_boards, sc)}


def _catchup_of(by_code, d, d_prev, sealed, prev_boards, sc):
    """低位扩散代理：新首板的**前一日** 45 日位置（封板日收盘必在窗口高位，
    当日口径无区分度——取昨日收盘在近 45 日窗口的位置，低位首板才计入补涨）。"""
    new_first = [c for c in sealed if c not in prev_boards]
    pos_list = []
    for c in new_first:
        p45 = _pos45(by_code.get(c, {}), d_prev)
        if p45 is not None:
            pos_list.append(p45)
    med = statistics.median(pos_list) if pos_list else None
    active = (len(pos_list) >= sc["catchup_n_min"] and med is not None
              and med <= sc["catchup_pos_max"])
    return {"active": active, "n_new_first": len(new_first),
            "n_pos_valid": len(pos_list), "median_pos45": med,
            "note": "低位扩散代理：新首板中位45日位置"}


# ── 赔率背景（零动作词枚举） ──────────────────────────────────────────────────
_BIAS = {
    "高潮": ("climax_heat", "一致性强、结构赔率好但拥挤，警惕随后分歧"),
    "发酵": ("ferment", "扩散进行中，顺势结构背景"),
    "启动": ("ignite", "启动试错期，重龙头识别"),
    "分歧": ("diverge", "接力赔率下降，等分歧转一致"),
    "退潮": ("ebb", "高位负反馈进行中，禁接力高位"),
    "冰点": ("freeze", "情绪冰点，观察止跌与首板结构"),
    "UNRESOLVED": ("unresolved", "证据门未命中，结构背景不明确"),
}


def _no_overreach(out):
    """零越权断言（锁死 3）：输出零 action 字段 + 零指令字样（结构性 + 文本级）。"""
    if "action" in out:
        raise S3Error("VIOLATION: 输出不得含 action 字段（S 只供素材不越权）")
    text = json.dumps(out, ensure_ascii=False)
    if re.search(r'"action"\s*:', text):
        raise S3Error("VIOLATION: 嵌套 action 字段")
    for w in ("建议买入", "建议卖出", "目标仓位", "下单"):
        if w in text:
            raise S3Error("VIOLATION: 输出含越权指令字样 %r" % w)


# ── 主入口 ────────────────────────────────────────────────────────────────────
def evaluate(as_of, *, old_root=None, params_path=None, base=None):
    """S3 单日评估主入口 → 快照落 data/generated/s_s3_<as_of>.json。

    拒绝：S3Error（REASON_CODE: 前缀）——BAD_DATE / DATA_END（as_of 晚于行情，
    禁编未来结构）/ DATA_INSUFFICIENT（早于起算，窗不足）/ AS_OF_ABSENT（非
    行情日）/ RULES_MISSING。同 as_of 幂等覆盖；输出零 wall-clock，同输入
    逐字节同输出。
    """
    d = _chk_date(as_of, "as_of")
    rules = load_rules(params_path)
    dates, series = derive_series(old_root, rules=rules, params_path=params_path)
    if d > dates[-1]:
        # ★ 2026-10-20：加「是否忘 merge」的可执行提示（同 s2 口径，按在场证据给）
        _hint3 = ""
        try:
            if os.path.isfile(os.path.join(old_root or ROOT, "data", "raw", d,
                                           "daily_quote.csv")):
                _hint3 = ("；⚠ 按日行情 data/raw/%s/daily_quote.csv 在盘 ⇒ 可能未合并进 "
                          "klines：先跑 `tools/e2e/merge_klines.py --from %s --to %s`"
                          % (d, d, d))
        except Exception:                                        # noqa: BLE001
            pass
        raise S3Error("DATA_END: as_of %s 晚于行情截止 %s（禁编未来结构）%s"
                      % (d, dates[-1], _hint3))
    idx = {x: i for i, x in enumerate(dates)}
    if d not in idx:
        raise S3Error("AS_OF_ABSENT: %s 非行情日（数据覆盖 %s..%s）" % (d, dates[0], dates[-1]))
    i = idx[d]
    i_start = idx[SERIES_START]
    if i < i_start + 2:
        raise S3Error("DATA_INSUFFICIENT: %s 早于起算窗（%s+%2d 交易日）" % (d, SERIES_START, 2))
    by_code, _ = load_klines(old_root, cutoff=_LOAD_CUTOFF)
    m = series[i]
    temp, temp_reason = temp_of(m, rules)
    seq = stage_sequence(series, i_start, i, rules)
    stage, reason, gates = seq[-1]
    sealed = _sealed_codes(by_code, dates, i)
    cur_boards = {c: _board_n(by_code, dates, i, c) for c in sealed}
    prev_boards = _prev_boards(by_code, dates, i)
    struct = structure_of(by_code, dates, i, sealed, cur_boards, prev_boards, rules)
    bias_key, bias_note = _BIAS[stage]
    missing = [k for k in ("promote_rate", "fail_rate", "yesterday_premium",
                           "high_pos_feedback") if m.get(k) is None]
    confidence = round(_MAX_CONF - 0.15 * len(missing), 2)
    out = {
        "schema": SCHEMA_VERSION, "s_id": S_ID, "cap": "S3", "as_of": d,
        "temp": temp, "stage": stage,
        "stage_reason": reason + ["温度依据: " + temp_reason],
        "leader": struct["leader"], "mid": struct["mid"], "catchup": struct["catchup"],
        "structure_bias": {"key": bias_key, "note": bias_note},
        "evidence": {"metrics": {k: v for k, v in m.items() if k != "date"},
                     "temp_reason": temp_reason,
                     "gates": gates,
                     "missing_metrics": missing},
        "confidence": confidence,
    }
    _no_overreach(out)
    gen_dir = os.path.join(base or ROOT, "data", "generated")
    snap_p = os.path.join(gen_dir, "s_%s_%s.json" % (S_ID, d))
    _write_snapshot(out, path=snap_p)
    return {"status": "OK", "output": out, "snapshot": snap_p}


def _board_n(by_code, dates, i, code):
    """code 截至第 i 日的连板数（向前回溯连续封板日数）。"""
    n = 0
    j = i
    while j >= 1:
        rows = by_code.get(code, {})
        cur, prev = rows.get(dates[j]), rows.get(dates[j - 1])
        if not cur or not prev:
            break
        limit_px = round(prev[0] * (1 + _limit_ratio(code, cur[5])), 2)
        if abs(cur[0] - limit_px) < 0.005:
            n += 1
            j -= 1
        else:
            break
    return n


def _prev_boards(by_code, dates, i):
    """第 i-1 日连板数映射（补涨代理的"新首板"判据）。"""
    out = {}
    sealed_prev = _sealed_codes(by_code, dates, i - 1)
    for c in sealed_prev:
        out[c] = _board_n(by_code, dates, i - 1, c)
    return out


# ── 唯一快照写点（X3；mkstemp 形态同 s1） ─────────────────────────────────────
def _write_snapshot(snap, path=None):
    _WG.check_write(path or SNAP_FALLBACK)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path or SNAP_FALLBACK),
                               prefix="s_s3_", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path or SNAP_FALLBACK)


# ── 历史库构建（X1 提案；知识库零写通道） ─────────────────────────────────────
def build_history(old_root=None, params_path=None, base=None):
    """全序列 → 每日行（阶段×温度×溢价）+ 分组统计 → X1 提案报告。

    溢价：某日涨停票 T+1 / T+3 前向涨幅均值（个股 pct 累加口径，含市场 beta，
    与 S1 市场调整 CAR 禁混读）；残窗不入库（窗口任一日缺行情 → 该票剔除该窗）。
    数据截止前 3 个交易日的 T+3 窗残缺 → 相应窗剔除（逐票）。报告根可注入
    （selftest 沙盒；生产 = ROOT，X1 豁免 data/generated/reports/ 前缀）。
    """
    rules = load_rules(params_path)
    dates, series = derive_series(old_root, rules=rules, params_path=params_path)
    by_code, _ = load_klines(old_root, cutoff=_LOAD_CUTOFF)
    idx = {x: i for i, x in enumerate(dates)}
    i_start = idx[SERIES_START]
    seq = stage_sequence(series, i_start + 2, len(dates) - 1, rules)
    rows = []
    for k, i in enumerate(range(i_start + 2, len(dates))):
        if not series[i]:
            continue
        m = series[i]
        stage = seq[k][0] if seq[k] else "UNRESOLVED"
        temp, _ = temp_of(m, rules)
        sealed = _sealed_codes(by_code, dates, i)
        t1, t3 = _premium(by_code, dates, i, sealed, 1), _premium(by_code, dates, i, sealed, 3)
        rows.append({"date": m["date"], "stage": stage, "temp": temp,
                     "zt_count": m["zt_count"], "max_board": m["max_board"],
                     "n_lb2": m["n_lb2"], "promote_rate": m["promote_rate"],
                     "fail_rate": m["fail_rate"], "fail_src": m["fail_src"],
                     "premium_t1_pct": t1, "premium_t3_pct": t3,
                     "leader_boards": m["max_board"],
                     "catchup_active_hint": (m["n_new_first"], m["catchup_median_pos45"])})
    # 分组：阶段 × 温度（行级明细保留全字段，后续可再分组；v1 粒度见报告元信息）
    groups = {}
    for r in rows:
        if r["premium_t1_pct"] is None:
            continue
        g = groups.setdefault(r["stage"], {"n_days": 0, "t1": [], "t3": [],
                                           "by_temp": {}})
        g["n_days"] += 1
        g["t1"].append(r["premium_t1_pct"])
        if r["premium_t3_pct"] is not None:
            g["t3"].append(r["premium_t3_pct"])
        gt = g["by_temp"].setdefault(r["temp"], {"n_days": 0, "t1": []})
        gt["n_days"] += 1
        gt["t1"].append(r["premium_t1_pct"])
    n_min = 30
    out_groups = {}
    for k, g in sorted(groups.items()):
        out_groups[k] = {
            "n_days": g["n_days"],
            "premium_t1_mean_pct": round(statistics.fmean(g["t1"]), 3) if g["t1"] else None,
            "premium_t3_mean_pct": round(statistics.fmean(g["t3"]), 3) if g["t3"] else None,
            "sample_ok": g["n_days"] >= n_min,
            "by_temp": {t: {"n_days": v["n_days"],
                            "premium_t1_mean_pct": round(statistics.fmean(v["t1"]), 3)}
                        for t, v in sorted(g["by_temp"].items())},
        }
    unresolved_n = sum(1 for r in rows if r["stage"] == "UNRESOLVED")
    proposal = {
        "schema": 1, "s_id": S_ID, "cap": "S3", "kind": "HISTORY_PROPOSAL",
        "as_of": rows[-1]["date"] if rows else None,
        "data_end": dates[-1],
        "n_days_total": len(rows), "n_unresolved": unresolved_n,
        "n_min_trust": n_min,
        "method": {"premium": "个股自身 pct 累加（含市场 beta；与 S1 市场调整 CAR 口径不同属，禁混读）",
                   "residual": "窗口任一日缺行情该票剔除该窗（残窗不入库）",
                   "grouping": "v1 = 阶段×温度；行级明细保留结构全字段可再分组",
                   "stage_gate": "证据门三栏（必要/辅助/反向），阈值 = params.s3_rules；"
                                 "周期语义=阶段是状态、门是转换：转换门未命中且前值有阶段 → "
                                 "状态延续（维持前值，断链即 UNRESOLVED，不跨缺口外推）"},
        "groups": out_groups,
        "rows_head": rows[:12],
    }
    os.makedirs(os.path.join(base or ROOT, "data", "generated", "reports",
                             "s3_backfill"), exist_ok=True)
    # 写点终点内联（landing 静态解析要求；X1 豁免 data/generated/reports/ 前缀）
    with io.open(os.path.join(base or ROOT, "data", "generated", "reports",
                              "s3_backfill", "summary.json"),
                 "w", encoding="utf-8", newline="\n") as f:
        json.dump(proposal, f, ensure_ascii=False, indent=1)
    return {"report": os.path.join(base or ROOT, "data", "generated", "reports",
                                   "s3_backfill", "summary.json"),
            "proposal": proposal}


def _premium(by_code, dates, i, sealed, k):
    """第 i 日涨停票的 T+k 前向涨幅均值；残窗逐票剔除；空集 → None。"""
    vals = []
    for c in sealed:
        rows = by_code.get(c, {})
        win = [rows.get(dates[j]) for j in range(i + 1, min(i + 1 + k, len(dates)))]
        if len(win) < k or any(w is None for w in win):
            continue
        vals.append(sum(w[3] for w in win))
    return round(statistics.fmean(vals), 4) if vals else None


def history_check(old_root=None, params_path=None, base=None):
    """复算对账已晋升知识库（只读）：库内 groups 与复算 groups 逐格比对。"""
    rec = LD.load_knowledge(S_ID, "9999-12-31", base=base or ROOT)
    if rec is LD.UNAVAILABLE:
        raise S3Error("KB_ABSENT: s3 知识库未晋升或路由缺席")
    kb = dict(rec)
    if not kb.get("groups"):
        raise S3Error("KB_SHAPE: 已晋升库缺 groups")
    rep = build_history(old_root, params_path, base=base)["proposal"]
    diffs = []
    for stage, g in kb.get("groups", {}).items():
        r = rep["groups"].get(stage)
        if r is None:
            diffs.append({"stage": stage, "kind": "ABSENT_IN_RECOMPUTE"})
            continue
        for f in ("n_days", "premium_t1_mean_pct"):
            if g.get(f) != r.get(f):
                diffs.append({"stage": stage, "field": f,
                              "kb": g.get(f), "recomputed": r.get(f)})
    return {"reproduced": not diffs, "diffs": diffs,
            "kb_as_of": kb.get("as_of"), "recomputed_as_of": rep["as_of"]}


def read_structure(as_of, base=None):
    """当日结构快照真链路读取（loader.load_s_output；缺 → UNAVAILABLE 原样返回）。"""
    return LD.load_s_output(S_ID, _chk_date(as_of, "as_of"), base=base)


# ── CLI ───────────────────────────────────────────────────────────────────────
def _main(argv):
    ap = argparse.ArgumentParser(description="S3 情绪周期判定")
    ap.add_argument("--eval", metavar="AS_OF")
    ap.add_argument("--history-build", action="store_true")
    ap.add_argument("--history-check", action="store_true")
    ap.add_argument("--old-root")
    ap.add_argument("--params")
    ap.add_argument("--base", help="数据根覆盖（沙盒；空 = 生产）")
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--discipline", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return 0 if _selftest() else 1
    if a.discipline:
        return 0 if _discipline() else 1
    if a.base and not os.path.isdir(a.base):
        print("--base 必须是已存在目录（防误写生产），实际: %s" % a.base)
        return 2
    if a.eval:
        r = evaluate(a.eval, old_root=a.old_root, params_path=a.params,
                     base=a.base)
        if a.stdout:
            print(json.dumps(r["output"], ensure_ascii=False, indent=1))
        print("快照: %s" % r["snapshot"])
        return 0
    if a.history_build:
        r = build_history(a.old_root, a.params)
        p = r["proposal"]
        print("X1 提案: %s" % r["report"])
        print("覆盖 %d 日（其中 UNRESOLVED %d 日）；data_end=%s"
              % (p["n_days_total"], p["n_unresolved"], p["data_end"]))
        for k, g in p["groups"].items():
            print("  %-4s n=%2d%s T1=%s T3=%s" % (
                k, g["n_days"], "" if g["sample_ok"] else "（样本不足）",
                g["premium_t1_mean_pct"], g["premium_t3_mean_pct"]))
        return 0
    if a.history_check:
        r = history_check(a.old_root, a.params)
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
    import datetime as _dtm

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

    def trading_days(n, start="2026-03-02"):
        d = _dtm.date.fromisoformat(start)
        out = []
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d.isoformat())
            d += _dtm.timedelta(days=1)
        return out

    def write_klines(path, rows):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("code,name,date,open,close,high,low,volume,amount,exchange,change_pct\n")
            for r in rows:
                f.write(",".join(str(x) for x in r) + "\n")

    def write_zt(path_dir, date, n_lim, n_zb):
        os.makedirs(path_dir, exist_ok=True)
        p = os.path.join(path_dir, "_zt_matrix_%s.csv" % date.replace("-", ""))
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write("group,code,name,date\n")
            for k in range(n_lim):
                f.write("limitup,c%d,n%d,%s\n" % (k, k, date))
            for k in range(n_zb):
                f.write("zb,z%d,z%d,%s\n" % (k, k, date))

    def seed_rules():
        return {
            "temp": {"zt_warm": 25, "zt_hot": 50, "zt_over": 80,
                     "promote_hot": 0.25, "promote_over": 0.35, "mb_over": 5},
            "stage": {"advance_zt_days": 2, "ignite_mb_max": 3,
                      "ignite_promote_min": 0.20, "ignite_first_share_min": 0.60,
                      "ferment_promote_min": 0.30, "ferment_mb_min": 3,
                      "ferment_fail_max": 0.45, "ferment_premium_aux": 2.0,
                      "climax_promote_min": 0.35, "climax_premium_aux": 3.0,
                      "climax_fail_max": 0.50, "diverge_fail_min": 0.35,
                      "diverge_mb_min": 3, "diverge_premium_aux": 2.0,
                      "ebb_highfeed_max": 0.0, "ebb_promote_aux": 0.20,
                      "freeze_zt_max": 10, "freeze_mb_max": 2,
                      "freeze_promote_max": 0.15, "climax_window": 9},
            "structure": {"mid_min_boards": 2, "mid_min_amount": 1e11,
                          "catchup_n_min": 2, "catchup_pos_max": 0.35},
        }

    def mk_series(n, **fixed):
        base = {"date": "d", "zt_count": 30, "max_board": 3, "n_lb2": 5,
                "n_first": 25, "first_share": 0.8, "promote_rate": 0.25,
                "yesterday_premium": 1.0, "high_pos_feedback": 1.0,
                "fail_rate": 0.2, "fail_src": "zt_matrix", "n_new_first": 10,
                "catchup_median_pos45": 0.5, "turnover_total": 1e11}
        base.update(fixed)
        return [dict(base, date="d%d" % k) for k in range(n)]

    tmp = tempfile.mkdtemp(prefix="s3_selftest_")
    old_root = os.path.join(tmp, "old")
    proj = os.path.join(tmp, "proj")
    os.makedirs(os.path.join(proj, "data", "generated"), exist_ok=True)

    # ── A. 温度四档 + 阶段证据门（合成序列直喂，精确可控） ──
    rules = seed_rules()
    T("A1 temp 过热", temp_of(mk_series(1, zt_count=90, promote_rate=0.4,
                                        max_board=6)[0], rules)[0] == "过热")
    T("A2 temp 热", temp_of(mk_series(1, zt_count=60, promote_rate=0.3)[0],
                            rules)[0] == "热")
    T("A3 temp 温", temp_of(mk_series(1, zt_count=30)[0], rules)[0] == "温")
    T("A4 temp 冷", temp_of(mk_series(1, zt_count=8)[0], rules)[0] == "冷")
    T("A5 晋级缺席→热门按未命中（如实降档）",
      temp_of(mk_series(1, zt_count=90, promote_rate=None, max_board=6)[0],
              rules)[0] == "温")

    ser = mk_series(3, zt_count=5, max_board=1, promote_rate=0.05)
    T("A6 stage 冰点", stage_of(ser, 2, rules)[0] == "冰点")
    ser = mk_series(3, zt_count=20, max_board=4, promote_rate=0.3,
                    fail_rate=0.5, high_pos_feedback=-2.0)
    ser[1]["zt_count"], ser[1]["max_board"] = 30, 5
    T("A7 stage 退潮（高位负反馈+家数缩+高度降）",
      stage_of(ser, 2, rules)[0] == "退潮")
    ser = mk_series(3, zt_count=55, max_board=6, promote_rate=0.4,
                    fail_rate=0.2, yesterday_premium=4.0)
    ser[0]["max_board"], ser[1]["max_board"] = 3, 4
    ser[0]["zt_count"], ser[1]["zt_count"] = 50, 53
    T("A8 stage 高潮（高度创9日新高+家数不缩+晋级强）",
      stage_of(ser, 2, rules)[0] == "高潮")
    ser[2]["fail_rate"] = 0.55
    T("A9 高潮反向（炸板极端）→ 落分歧",
      stage_of(ser, 2, rules)[0] == "分歧")
    ser = mk_series(3, zt_count=40, max_board=4, promote_rate=0.35,
                    fail_rate=0.4, yesterday_premium=1.0)
    ser[1]["n_lb2"] = 4
    ser[1]["max_board"] = 5                      # 高度未创近窗新高 → 高潮门不让
    T("A10 stage 分歧（炸板抬升+高度在）", stage_of(ser, 2, rules)[0] == "分歧")
    ser = mk_series(3, zt_count=45, max_board=4, promote_rate=0.35,
                    fail_rate=0.2, yesterday_premium=2.5)
    ser[1]["n_lb2"] = 4
    ser[1]["max_board"] = 5                      # 同上，高潮门让位 → 发酵
    T("A11 stage 发酵（连板扩张+晋级强+高度开）",
      stage_of(ser, 2, rules)[0] == "发酵")
    ser = mk_series(3, zt_count=18, max_board=2, promote_rate=0.25,
                    fail_rate=0.2, first_share=0.8)
    ser[0]["zt_count"], ser[1]["zt_count"] = 12, 15
    T("A12 stage 启动（连续回升+高度低+晋级回稳）",
      stage_of(ser, 2, rules)[0] == "启动")
    ser = mk_series(3, zt_count=30, max_board=3, promote_rate=0.22,
                    fail_rate=0.25)
    T("A13 六门未命中 → UNRESOLVED（不强行归档）",
      stage_of(ser, 2, rules)[0] == "UNRESOLVED")
    _st, _rs, _ = stage_at(ser, 2, rules, prev_stage="发酵")
    T("A14 状态延续：转换门未命中 → 维持前值（断链不外推）",
      _st == "发酵" and "延续" in _rs[0])
    T("A15 状态延续：前值缺 → 仍 UNRESOLVED",
      stage_at(ser, 2, rules, prev_stage=None)[0] == "UNRESOLVED")
    _seq = stage_sequence(ser, 0, 2, rules)
    T("A16 序列链：延续经已解析日传递",
      _seq[2][0] == stage_at(ser, 2, rules, prev_stage=None)[0])

    # ── B. 端到端（沙盒 klines + zt_matrix + params + 快照真链路） ──
    days = trading_days(60)                      # 60 个交易日，末段工程化
    hist_n = 50
    globals()["SERIES_START"] = days[45]         # 沙盒起算日注入（真值 = 2026-06-01）
    rows = []
    for dname in days[:hist_n]:
        rows.append(("sh600001", "主票", dname, 10.0, 10.0, 10.05, 9.95,
                     1000.0, 1e7, 0.5, 0.0))
        rows.append(("sh600002", "低位票乙", dname, 10.0, 9.91, 10.02, 9.90,
                     1000.0, 2e7, 0.5, -0.1))
        rows.append(("sh600003", "低位票丙", dname, 10.0, 9.91, 10.02, 9.90,
                     1000.0, 2e7, 0.5, -0.1))
        rows.append(("sh600004", "炸板票", dname, 10.0, 10.0, 10.05, 9.95,
                     1000.0, 1e7, 0.5, 0.0))
    d1, d2, d3 = days[hist_n], days[hist_n + 1], days[hist_n + 2]
    # d1: 主票首板（10.00→11.00），炸板票触板未封（high=11.00, close=10.80）；
    #     乙丙维持低位（有行情行才有前收，d2 才能封板成链）
    rows.append(("sh600001", "主票", d1, 10.5, 11.0, 11.0, 10.4, 2000.0, 5e8, 9.9, 10.0))
    rows.append(("sh600004", "炸板票", d1, 10.5, 10.8, 11.0, 10.4, 2000.0, 5e8, 8.0, 8.0))
    rows.append(("sh600002", "低位票乙", d1, 9.9, 9.91, 10.02, 9.90, 1000.0, 2e7, 0.0, 0.0))
    rows.append(("sh600003", "低位票丙", d1, 9.9, 9.91, 10.02, 9.90, 1000.0, 2e7, 0.0, 0.0))
    # d2: 主票2板（11.00→12.10），乙丙低位新首板（9.91→10.90）
    rows.append(("sh600001", "主票", d2, 11.2, 12.1, 12.1, 11.1, 3000.0, 9e8, 9.9, 10.0))
    rows.append(("sh600002", "低位票乙", d2, 10.2, 10.9, 10.9, 10.1, 2000.0, 6e8, 9.9, 10.0))
    rows.append(("sh600003", "低位票丙", d2, 10.2, 10.9, 10.9, 10.1, 2000.0, 6e8, 9.9, 10.0))
    # d3: 主票3板（12.10→13.31），乙2板（10.90→11.99）
    rows.append(("sh600001", "主票", d3, 12.3, 13.31, 13.31, 12.2, 4000.0, 1.2e9, 9.9, 10.0))
    rows.append(("sh600002", "低位票乙", d3, 11.0, 11.99, 11.99, 11.0, 3000.0, 1.5e9, 9.9, 10.0))
    write_klines(os.path.join(old_root, "data", "raw", "bus", "klines.csv"), rows)
    write_zt(_zt_dir(old_root), d2, 3, 1)        # d2 炸板率 = 1/4（zt_matrix 真源）

    pfile = os.path.join(tmp, "params.json")
    with io.open(pfile, "w", encoding="utf-8") as f:
        json.dump({"version": "9.9.9", "updated_at": "2026-01-01",
                   "params": {_RULE_KEY: {"value": seed_rules(),
                                          "status": "BASELINE"}}}, f)
    r = evaluate(d2, old_root=old_root, params_path=pfile, base=proj)
    o = r["output"]
    T("B1 端到端 evaluate 状态 OK", r["status"] == "OK")
    T("B2 快照落沙盒 + loader 真链路可读",
      LD.load_s_output("s3", d2, base=proj) is not LD.UNAVAILABLE)
    T("B3 主票 2 板为领先者", o["leader"]["code"] == "sh600001"
      and o["leader"]["boards"] == 2)
    T("B4 低位新首板 → 补涨激活（前一日位置代理）",
      o["catchup"]["active"] is True and o["catchup"]["n_new_first"] == 2)
    T("B5 无达标候选 → mid=None（锁死 2：不强指定）", o["mid"] is None)
    T("B6 炸板率 zt_matrix 真源 + 组计数口径",
      o["evidence"]["metrics"]["fail_src"] == "zt_matrix"
      and abs(o["evidence"]["metrics"]["fail_rate"] - 0.25) < 1e-9)
    T("B7 零越权：输出零 action 字段", "action" not in o)
    T("B8 confidence 缺指标降档", 0 < o["confidence"] <= _MAX_CONF)
    with io.open(r["snapshot"], encoding="utf-8") as f:
        b1 = f.read()
    evaluate(d2, old_root=old_root, params_path=pfile, base=proj)
    with io.open(r["snapshot"], encoding="utf-8") as f:
        b2 = f.read()
    T("B9 幂等：同输入逐字节同输出（零 wall-clock）", b1 == b2)
    try:
        evaluate("2026-12-31", old_root=old_root, params_path=pfile, base=proj)
        T("B10 DATA_END 拒答", False)
    except S3Error as e:
        T("B10 DATA_END 拒答", str(e).startswith("DATA_END"))
    try:
        evaluate(days[10], old_root=old_root, params_path=pfile, base=proj)
        T("B11 起算窗前置拒答", False)
    except S3Error as e:
        T("B11 起算窗前置拒答", str(e).startswith("DATA_INSUFFICIENT"))
    try:
        evaluate("2026-03-07", old_root=old_root, params_path=pfile, base=proj)
        T("B12 非行情日拒答", False)
    except S3Error as e:
        T("B12 非行情日拒答", str(e).startswith("AS_OF_ABSENT"))
    badp = os.path.join(tmp, "bad_params.json")
    with io.open(badp, "w", encoding="utf-8") as f:
        json.dump({"version": "0", "params": {}}, f)
    try:
        evaluate(d2, old_root=old_root, params_path=badp, base=proj)
        T("B13 RULES_MISSING 拒答", False)
    except S3Error as e:
        T("B13 RULES_MISSING 拒答", str(e).startswith("RULES_MISSING"))
    try:
        _no_overreach({"stage": "高潮", "action": "x"})
        T("B14 越权断言触发", False)
    except S3Error as e:
        T("B14 越权断言触发", str(e).startswith("VIOLATION"))
    try:
        _no_overreach({"stage": "高潮", "note": "建议买入"})
        T("B15 越权指令词触发", False)
    except S3Error as e:
        T("B15 越权指令词触发", str(e).startswith("VIOLATION"))

    # ── C. 结构定位分支 ──
    rules2 = seed_rules()
    rules2["structure"]["mid_min_amount"] = 5e8
    r2 = evaluate(d3, old_root=old_root, params_path=_rules_params(tmp, rules2),
                  base=proj)
    T("C1 中军容量门槛放低 → mid 出现（成交额最大连板者乙）",
      r2["output"]["mid"] is not None
      and r2["output"]["mid"]["code"] == "sh600002")
    d4 = days[hist_n + 3]
    with io.open(os.path.join(old_root, "data", "raw", "bus", "klines.csv"),
                 "a", encoding="utf-8", newline="\n") as f:
        f.write("sh600001,主票,%s,13.5,13.5,13.9,13.4,5000.0,1.3e9,1.4,1.43\n"
                % d4)                             # 主票未封（涨停价 14.64）
        f.write("sh600002,低位票乙,%s,12.1,13.19,13.19,12.0,4000.0,1.8e9,9.9,10.0\n"
                % d4)                             # 乙 3 板封住
    r3 = evaluate(d4, old_root=old_root, params_path=_rules_params(tmp, rules2),
                  base=proj)
    T("C2 原龙头未封 → 领先者易主（乙 3 板）",
      r3["output"]["leader"]["code"] == "sh600002"
      and r3["output"]["leader"]["boards"] == 3)

    # ── D. 历史库构建 + 晋升对账闭环（沙盒知识库） ──
    hb = build_history(old_root, pfile, base=proj)
    T("D1 X1 提案落沙盒 reports/", os.path.isfile(hb["report"]))
    prop = hb["proposal"]
    T("D2 行级覆盖充足且 UNRESOLVED 计数在案",
      prop["n_days_total"] >= 5 and prop["n_unresolved"] >= 0)
    kbdir = os.path.join(proj, "data", "knowledge", "s3_emotion_structure")
    os.makedirs(kbdir, exist_ok=True)
    kb = {"schema": 1, "s_id": "s3", "as_of": prop["as_of"],
          "data_end": prop["data_end"], "groups": prop["groups"],
          "method": prop["method"]}
    with io.open(os.path.join(kbdir, "emotion_base.json"), "w",
                 encoding="utf-8", newline="\n") as f:
        json.dump(kb, f, ensure_ascii=False, indent=1)
    regp = os.path.join(proj, "data", "knowledge", "_registry.json")
    with io.open(regp, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"registry_version": "1.0.0",
                   "s3": {"path": "data/knowledge/s3_emotion_structure/"
                                   "emotion_base.json",
                          "version": "1.0.0"}}, f)
    hc = history_check(old_root, pfile, base=proj)
    T("D3 晋升后复算对账 reproduced", hc["reproduced"] is True
      and not hc["diffs"])

    print("自检结果：%s（%d/%d）" % ("通过 ✅" if not fail else "失败 ❌", ok, ok + fail))
    if fails:
        print("  失败项: %s" % fails)
    return not fail


def _rules_params(tmp, rules):
    """自检辅助：把规则集写成独立 params 文件（沙盒内）。"""
    p = os.path.join(tmp, "params_%s.json" % abs(hash(_canon(rules))))
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump({"version": "9.9.9", "updated_at": "2026-01-01",
                   "params": {_RULE_KEY: {"value": rules,
                                          "status": "BASELINE"}}}, f)
    return p


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
        if name.startswith(("_selftest", "_discipline", "_main", "_rules_params")):
            drop.update(range(lo - 1, hi))
    prod = "".join(l for i, l in enumerate(lines) if i not in drop)

    # ① 零核心链 import（S 独立成件，只经 loader/params/paths 衔接）
    for mod in ("settle", "wakeup", "three_q", "decision", "position_gate",
                "exit_gate", "orchestrate", "drive", "candidates", "thesis",
                "landing", "observe"):
        T("① 零 import %s" % mod,
          not re.search(r"^import\s+%s\b|^from\s+[\w.]*%s\b\s+import" % (mod, mod),
                        prod, re.M))
    # ② 唯一写点形态（mkstemp + 终点内联 fallback）；其余写模式仅 X1 报告 1 处
    _sig = "def _write_snapshot(" + "snap, path=None):"
    _endp = "os.replace(tmp, " + "path or SNAP_FALLBACK)"
    T("② 唯一快照写点（mkstemp 形态）", _sig in prod and _endp in prod)
    T("②b 写模式 io.open 计数 ≤2（快照 + X1 报告）",
      len(re.findall(r'io\.open\([^)]*"[w]', prod)) <= 2)
    # ③ 阈值唯一来源 = params（get_value 经拼装键；模块内零直读 params.json）
    T("③ 阈值经 PARAMS.get_value", "PARAMS.get_value(" in prod)
    T("③b 规则键拼装构造（防词表自指）",
      ('"s3" + "_rules"') in prod or ("'s3' + " in prod))
    # ④ 零 wall-clock / 零日期算术（回放确定性：输出零时间戳）
    T("④ 零 datetime 依赖", "datetime" not in prod)
    # ⑤ 零越权/下单词（拼装词表，prod 窗口）
    _FORBIDDEN = ("or" + "der", "bu" + "y", "se" + "ll", "wa" + "it",
                  "po" + "sition_gate")
    T("⑤ 零越权词（%s）" % "·".join(_FORBIDDEN),
      not [w for w in _FORBIDDEN if w in prod])
    # ⑥ 快照路径小写对齐 loader._sid；fallback 字面量大写对齐注册表 cap_ref
    T("⑥ 快照文件名口径（s_s3_ + fallback S3 字面量）",
      '"s_%s_%s.json"' in prod and "s_S3_MISSING.json" in prod)
    # ⑦ 知识库零写通道（晋升走治理动作：无对 knowledge 目录的写点）
    T("⑦ 知识库零写点", not re.search(r'open\([^)]*knowledge[^)]*"[w]', prod))

    print("纪律结果：%s（%d/%d）" % ("通过 ✅" if not fail else "失败 ❌", ok, ok + fail))
    if fails:
        print("  失败项: %s" % fails)
    return not fail


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
