#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""s5.py · S5 资金筹码结构（任务9 · v1.0 · 2026-10-12 建）

用途：判**资金表现出什么结构/行为模式**——C3 B 类（资金是否构成当前上涨驱动）
/ C4 赔率背景 / E7 的供料器。**只供素材，零动作字段**（设计 2.5 批准锁死 4 条
全数落实，见 ★四）。S5 ≠ C3：C3 答「资金是否构成当前上涨驱动」，S5 答「已观察到的
参与者/流向/成交结构/筹码状态**呈现什么行为模式**」——S5 不回答「这票能买吗」。

★一、数据来源与口径（全部显式，禁事后改口）
  · 资金流 = 旧包 `data/raw/bus/fundflow.csv`（**921,382 行 / 181 日 / 5,238 码**）：
      date,code,close,main_net,main_in,main_out,jumbo_net,mid_net,small_net,block_net
      —— 主力/超大单/中单/小单/大单**净额**（单位元，正=净流入）。
      **覆盖实况如实标注**：2026-01-05..09-18 为全市场（每日约 5,190 行），
      09-19 起骤降至数百行（采集衰减）→ 快照 universe 报 n_flow_codes /
      coverage_ratio，历史库对覆盖弱日标 thin_coverage（**不掩盖、不补造**）。
  · 换手 = 旧包 `data/raw/bus/klines.csv` 的 `exchange` 列（S3 的 load_klines 视图
      不含该列，故本件另建 turnover 视图——**字段不同故非复制**；涨停/连板/位置
      原语仍单点复用 `s3`，本模块零复制涨停规则）。
  · 行情/涨停/连板/45 日位置 = `s3.load_klines` / `s3._sealed_codes` /
      `s3._board_n` / `s3._pos45`（与 S2/S3/real_replay 同源同口径）。
  · 筹码 = 旧包 `data/raw/bus/chip_daily.csv`（**3,256 行 / 75 日 / 1,032 码**，
      每日仅 5~70 码 = **关注池而非全市场**）→ 无该票时 chip_state="unknown"（如实）。
  · 龙虎榜 = 旧包 `data/raw/bus/lhb/lhb_<date>.md`（**仅 11 日**：09-15..09-30）：
      两段机器表格——「机构榜」（instBuyBranchCount/instBuyAmt/netBuyAmt）与
      「活跃席位」（席位 id/name/多股/买入额）。缺该日文件 → buyers=[] + 标缺（不猜）。
  · 股东结构 = 旧包 `data/generated/zhuanggu_shareholders.csv`（331 行 · **单时点快照**）
      → v1 **不参与判定**（无时序，"变化"不可得），仅作档案背景字段如实转载。

★二、行为模式（机械规则，阈值全部来自 params.s5_rules，本模块零字面量阈值）
  · behavior ∈ accumulation / relay / distribution / suspected_round_trip /
      neutral / unknown（设计 2.5 枚举）——**证据组合查表，不是总分打分**：
      每个行为 = 必要证据栏（全部命中）+ 判优顺序（params.behavior.priority，
      最"特指"者优先）；命中/未命中逐条进 behavior_reason.gates（可解释）。
  · 证据口径（窗口 W = flow.window，全因果：只用 ≤ D 数据）：
      net_r      = main_net / (main_in + main_out)            当日主力净占比
      cum_net_r  = 窗口内 Σmain_net / Σ(main_in + main_out)     窗口累计净占比
      dual       = min(main_in, main_out) / max(main_in, main_out)  双向度
      t_ratio    = 当日换手 / 前 (W−1) 日均换手                  换手放大倍
      pos_streak / neg_streak = 截至当日连续净流入/流出天数
      gain       = 窗口累计涨幅；pos = 45 日位置（0=最低 1=最高）
      zt_days    = 窗口内涨停天数（S3 精确价规则）；board = 当日连板数
    判优顺序（params.behavior.priority，实测分布对齐后定）：
      suspected_round_trip（dual≥0.90 且 |net_r|≤0.02 且 t_ratio≥1.5）
      → relay（zt_days≥1 且 net_r≥0 且 t_ratio≥1.0）
      → accumulation（cum_net_r≥0.03 且 pos_streak≥2 且 gain≤0.10）
      → distribution（cum_net_r≤−0.05 且 pos≥0.70 且 neg_streak≥2）
      → neutral（数据齐但无命中）；数据不足（有效日 < flow.min_days）→ unknown。
    **分布对齐非结果验证**：阈值锚点取自 2026-06-30（全市场 5,183 票）与 09-29
    实测分位（见施工方案 v3.40），仍 BASELINE / UNCALIBRATED。
  · chip_state ∈ 集中 / 分散 / 换手充分 / unknown——**三值互不推导**，各由独立指标判：
      集中 = concentration70 ≤ chip.conc_concentrated_max
      分散 = concentration70 ≥ chip.conc_dispersed_min
      换手充分 = t_ratio ≥ chip.turnover_sufficient_min 或 profitRate ≥ chip.profit_high_min
      （判优顺序 params.chip.priority；无筹码数据 → unknown，不用其他指标代理）

★三、历史库与席位档案（治理两期分离，同 S1/S3/S2 先例）
  · 行级明细 + 分组统计只进 **X1 提案报告**（data/generated/reports/s5_backfill/
    summary.json，与席位档案提案**同一写点**——保持写模式 ≤2）；人工核对后一次性
    晋升 data/knowledge/s5_capital_structure/capital_base.json + seat_archive.json
    + _registry.json 路由（治理动作，代码零知识库写通道）。
  · 分组 = behavior × chip_state 的 T+1/T+5 **个股涨幅累加**（含市场 beta，
    与 S1 市场调整 CAR、S3 涨停溢价、S2 题材等权涨幅**均不同属，禁混读**）；
    残窗不入库（窗口任一日缺行情 → 剔除该窗）。
  · 样本不足如实标记：格子 n < 30 → sample_ok=False（消费方按 INSUFFICIENT_SAMPLES
    处理，禁出统计结论）。
  · 席位档案 = **时点可见数据**（该席位出现日 + 参与票）→ 机械标签 → T+1/T+5
    结果回填（锁死 4：禁未来表现倒灌定义过去风格）。标签三值 + "样本不足"。

★四、批准锁死（施工方案 2.5）
  1. **behavior = 证据组合对应的行为模式分类，不是对资金真实意图的事实认定** ——
     输出恒带 evidence（指标值 + 判据命中），note 措辞中立（**禁**资金意图类
     断言字样——词表唯一来源 = 模块常量 _INTENT_WORDS，本注释不复述词形以免
     判据自指；由 _no_overreach 做单词级拦截）。
  2. **证据不足允许 unknown，禁止强行分类**（有效日不足 / 关键字段缺 → unknown，
     不落 neutral 凑数）。
  3. **buyers ≠ 真实资金主体** —— 席位类型 ≠ 最终资金主体（字段字面声明
     `seat_type_is_proxy: true`）；`style` **必须来自席位历史档案**（archive 缺该席位
     → style="unarchived"，**禁 LLM 临时判断、禁按 seat_type 顺推**）。
  4. **席位档案防样本污染** —— 只用 as_of 可见切片建档；档案 label 由历史**可观察
     统计**（涨停占比 / T+1 溢价中位）机械映射，不是意图认定。
  · S 只提供素材：输出零 action 字段 + _no_overreach 文本级双保险。
  · 时间纪律：日期一律字符串序数比较，零日期算术、零 wall-clock 进快照。

★五、落点与写点（landing 封闭）
  · 唯一快照写点 _write_snapshot（X3 豁免 data/generated/s_<cap>_<as_of>.json，
    文件名小写 s_s5_ 与 loader._sid 消费口径一致；fallback 字面量大写 S5 对齐
    注册表 id 的 cap_ref 判据——同 s1/s2/s3 先例）。
  · X1 提案报告 = data/generated/reports/s5_backfill/summary.json（X1 豁免）。
  · 知识库零写通道（★三）：已晋升档案只读（load_seat_archive）。

用法：
  python tools/analysis/s5.py --eval 2026-06-30 [--codes sz000001,sh600000] [--stdout]
  python tools/analysis/s5.py --history-build                   # 历史库 + 席位档案 X1 提案
  python tools/analysis/s5.py --history-check                   # 复算对账已晋升库
  python tools/analysis/s5.py --selftest                        # 内置验收（临时沙盒）
  python tools/analysis/s5.py --discipline                      # 纪律判据
"""
import argparse
import ast
import csv
import io
import json
import os
import re
import shutil
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
import s3 as S3              # noqa: E402  共享行情/涨停原语（一个知识点一个家）

_LEGACY_ROOT = (os.environ.get("OLD_SYSTEM_ROOT")
                or os.path.join(os.path.expanduser("~"), "炒股智能体_旧包_20261008"))  # 旧系统只读参考源；换机器时设 OLD_SYSTEM_ROOT 或放家目录

SCHEMA_VERSION = 1
S_ID = "s5"
# 旧包只读外部输入根（fundflow / chip_daily / lhb / klines；同 s1/s2/s3 先例，禁写禁搬）。
_OLD_ROOT = _LEGACY_ROOT
# 指标序列起算日（与 S2/S3 对齐；早于此拒答 = DATA_INSUFFICIENT）。
SERIES_START = "2026-06-01"
# 行情装载起点（口径常量：起算日前约 62 个交易日，保证窗口余量）。
_LOAD_CUTOFF = "2026-03-01"
SNAP_FALLBACK = os.path.join(ROOT, "data", "generated", "s_S5_MISSING.json")
_MAX_CONF = 0.9              # 首批置信度封顶（不出 1.0，同 s1/s2/s3 口径）
_CONF_FLOOR = 0.3
_BEHAVIORS = ("accumulation", "relay", "distribution", "suspected_round_trip",
              "neutral", "unknown")
# "有结构信息"的行为（四类）——全市场扫描快照只落这些票的明细；neutral/unknown
# 的信息量＝"无显著模式"，其**计数完整上报**（behavior_counts / n_neutral /
# n_unknown），明细不落盘（否则单日快照 11.6MB，无消费场景且污染版本库）。
_STRUCTURED = ("accumulation", "relay", "distribution", "suspected_round_trip")
_CHIP_STATES = ("集中", "分散", "换手充分", "unknown")
# 意图断言词（锁死 1）：命中即拒——S5 不得宣称资金主体的真实意图。
# **拼装构造**（同 s2._FORBIDDEN 先例）：防"检查串自身出现在源码里"的自指误报。
_INTENT_WORDS = ("吸" + "筹", "正在" + "建仓", "在" + "建仓", "出" + "货",
                 "派发" + "筹码", "主力" + "意图", "在" + "洗盘", "锁" + "仓")


class S5Error(Exception):
    """评估被拒。str 以 REASON_CODE: 开头（每类拒绝可机械断言）。"""


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _chk_date(v, name):
    s = str(v or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        raise S5Error("BAD_DATE: %s 必须是 YYYY-MM-DD，实际 %r" % (name, v))
    return s


# 行情/资金/筹码/龙虎榜四处 code 口径统一 = `<市场前缀小写><6位>`（如 sz000002）。
# 与 S2 的 `000002.SZ`（成分表口径）不同源——本件输入已是目标格式，故只**校验**
# 不转换；不识别即计次上报，**禁静默丢行**（否则全市场覆盖被偏置）。
_CODE_RE = re.compile(r"^(sh|sz|bj)\d{6}$")
# 指数码口径常量（**非可调阈值**，故不入 params；同 s3._POS_WINDOW 先例）：
# 旧包 fundflow/klines 含 `sh000001`（上证指数）——若按个股处理会产出"指数的资金
# 行为"行并污染历史库分组 → 显式排除 + 计次上报（禁静默丢，覆盖数须自洽）。
# 注意 `sz000001`（平安银行）是**个股**，故只按前缀 `sh000` / `sz399` 判指数。
_INDEX_PREFIX = ("sh000", "sz399")


def is_index(code):
    """指数码判定（前缀口径）。"""
    return str(code or "").startswith(_INDEX_PREFIX)


def _chk_code(raw):
    s = str(raw or "").strip().lower()
    return s if _CODE_RE.match(s) else None


# ── 规则装载（阈值唯一来源 = params.s5_rules / 席位词表 = params.s5_seats） ──────
_RULE_KEY = "s5" + "_rules"          # 拼装构造，防词表自指
_SEAT_KEY = "s5" + "_seats"


def load_rules(params_path=None):
    v = PARAMS.get_value(_RULE_KEY, None, path=params_path)
    if not isinstance(v, dict):
        raise S5Error("RULES_MISSING: params 缺 %s（行为/筹码阈值唯一来源）" % _RULE_KEY)
    for part in ("flow", "behavior", "chip", "seat"):
        if not isinstance(v.get(part), dict):
            raise S5Error("RULES_MISSING: %s.%s 缺失" % (_RULE_KEY, part))
    if not v["behavior"].get("priority"):
        raise S5Error("RULES_MISSING: %s.behavior.priority 缺失（判优顺序须外置）"
                      % _RULE_KEY)
    return v


def load_seat_vocab(params_path=None):
    v = PARAMS.get_value(_SEAT_KEY, None, path=params_path)
    if not isinstance(v, dict) or not isinstance(v.get("types"), dict):
        raise S5Error("RULES_MISSING: params 缺 %s（席位类型词表唯一来源）" % _SEAT_KEY)
    if not v.get("priority"):
        raise S5Error("RULES_MISSING: %s.priority 缺失（分类顺序须外置）" % _SEAT_KEY)
    return v


# ── 输入装载（旧包只读） ──────────────────────────────────────────────────────
def _flow_path(old_root):
    return os.path.join(old_root, *(["data", "raw", "bus", "fundflow.csv"]))


def _chip_path(old_root):
    return os.path.join(old_root, *(["data", "raw", "bus", "chip_daily.csv"]))


def _turn_path(old_root):
    return os.path.join(old_root, *(["data", "raw", "bus", "klines.csv"]))


def _seat_path(old_root):
    return os.path.join(old_root, *(["data", "generated", "zhuanggu_shareholders.csv"]))


def _lhb_path(old_root, date):
    return os.path.join(old_root, *(["data", "raw", "bus", "lhb", "lhb_%s.md" % date]))


def _f(r, key):
    try:
        return float(r[key])
    except (ValueError, TypeError, KeyError):
        return None


def _local_flow_path(base=None):
    """本项目自采资金流（`fetch_fundflow.py` 写入，2026-10-22 起）。

    与旧包**互补不重叠**（实测）：
      旧包   2026-01-05 ~ 2026-09-30 · 5238 码 · 921,382 行
      本项目 2026-10-08 ~ 至今       · 增量采集中
    ⇒ 双源合并后才有完整区间；只读旧包会让 10 月全部 MISSING（原 STALE 根因）。

    ★ `base`：本项目的根（默认 ROOT）。
      注意**不能**用 `old_root` 沙盒基址来推导它 —— 两者相对布局相同
      （都是 `<root>/data/raw/bus/fundflow.csv`），传同一个 base 会让两个源
      指向同一文件、双源退化成单源（2026-10-23 自检踩到：A13 三个条件全乱）。
      沙盒隔离要显式传不同的 base。
    """
    return os.path.join(base or ROOT, "data", "raw", "bus", "fundflow.csv")


def _read_flow_file(p, cutoff, out, stat):
    """读单个 fundflow 文件并入 `out`（就地累加 stat）。返回本文件行数。

    抽成独立函数是为了双源共用同一套校验口径（缺列/坏值/指数/未识别码都计次），
    禁两套解析（否则两个源的行会被不同标准过滤，统计不可比）。
    """
    n = 0
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = str(r.get("date") or "").strip()
            if cutoff and d < cutoff:
                continue
            code = _chk_code(r.get("code"))
            if code is None:
                stat["n_code_bad"] += 1
                continue
            if is_index(code):
                stat["n_index_excluded"] += 1
                continue
            vals = tuple(_f(r, k) for k in ("close", "main_net", "main_in", "main_out",
                                            "jumbo_net", "mid_net", "small_net",
                                            "block_net"))
            if any(v is None for v in vals):
                stat["n_val_bad"] += 1
                continue
            out.setdefault(code, {})[d] = vals
            n += 1
    return n


def load_fundflow(old_root=None, *, cutoff=None, sources="dual", local_root=None):
    """fundflow.csv → (by_code, stats)。by_code[code][date] = 8 元组：
    (close, main_net, main_in, main_out, jumbo_net, mid_net, small_net, block_net)。
    缺列/坏值行跳过并计次；code 不识别 → 计次上报（禁静默丢）。

    `sources`（2026-10-23 双源改造）：
      · "dual"  （默认）= 旧包历史 + 本项目自采，**同日以本项目为准**
        （自采是最新口径重取，旧包同日在除权票上可能有复权基准漂移）
      · "old"   = 只读旧包（旧行为，保留以便对照与回归）
      · "local" = 只读本项目（用于验证采集本身）
    两源都缺席才报 FLOW_ABSENT；只有一源缺 → 用另一源并如实记 `src_missing`，
    不静默吞掉（否则「少读一个源」看起来和「数据本来就少」一模一样）。

    `local_root`：本项目自采根（默认 ROOT）。**与 `old_root` 独立** ——
    两者相对布局虽相同，但语义不同，混用一个参数会让双源退化成单源。
    """
    stat = {"n_rows": 0, "n_codes": 0, "n_code_bad": 0, "n_val_bad": 0,
            "n_index_excluded": 0, "src": [], "src_missing": [], "n_overlap_rows": 0}

    cand = []
    if sources in ("dual", "old"):
        cand.append(("old", _flow_path(old_root or _OLD_ROOT)))
    if sources in ("dual", "local"):
        cand.append(("local", _local_flow_path(local_root)))

    out = {}
    for tag, p in cand:
        if not os.path.isfile(p):
            stat["src_missing"].append("%s:%s" % (tag, p))
            continue
        # 记录读前快照大小，用于算「同日覆盖」量（不改变合并语义，仅审计用）
        before = sum(len(v) for v in out.values())
        n = _read_flow_file(p, cutoff, out, stat)
        stat["src"].append({"tag": tag, "path": p, "n_rows_file": n,
                            "n_rows_added": sum(len(v) for v in out.values()) - before})
        stat["n_rows"] += n

    if not out:
        raise S5Error("FLOW_ABSENT: 资金流源全缺席（sources=%s · 缺 %s）"
                      % (sources, stat["src_missing"] or "无可用文件"))
    stat["n_codes"] = len(out)
    return out, stat


def load_turnover(old_root=None, *, cutoff=None):
    """klines.csv 的 `exchange`（换手率）→ {code: {date: pct}}。"""
    p = _turn_path(old_root or _OLD_ROOT)
    if not os.path.isfile(p):
        raise S5Error("TURNOVER_ABSENT: 行情源缺席（%s）" % p)
    out, bad, n_idx = {}, 0, 0
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = str(r.get("date") or "").strip()
            if cutoff and d < cutoff:
                continue
            code = _chk_code(r.get("code"))
            v = _f(r, "exchange")
            if code is None or v is None:
                bad += 1
                continue
            if is_index(code):
                n_idx += 1
                continue
            out.setdefault(code, {})[d] = v
    return out, {"n_codes": len(out), "n_bad": bad, "n_index_excluded": n_idx, "src": p}


def load_chip(old_root=None):
    """chip_daily.csv → (by_code, stats)。by_code[code][date] = (avg_cost, conc70,
    conc90, profit_rate, close)。**样本池=关注池**（每日 5~70 码），非全市场。"""
    p = _chip_path(old_root or _OLD_ROOT)
    out, bad_code, bad_val, n, n_idx = {}, 0, 0, 0, 0
    if not os.path.isfile(p):
        return out, {"n_rows": 0, "n_codes": 0, "n_code_bad": 0, "n_val_bad": 0,
                     "n_index_excluded": 0, "src": p, "absent": True}
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = str(r.get("date") or "").strip()
            code = _chk_code(r.get("code"))
            if code is None:
                bad_code += 1
                continue
            if is_index(code):
                n_idx += 1
                continue
            vals = tuple(_f(r, k) for k in ("chipAvgCost", "chipConcentration70",
                                            "chipConcentration90", "chipProfitRate",
                                            "closePrice"))
            if any(v is None for v in vals):
                bad_val += 1
                continue
            out.setdefault(code, {})[d] = vals
            n += 1
    return out, {"n_rows": n, "n_codes": len(out), "n_code_bad": bad_code,
                 "n_val_bad": bad_val, "n_index_excluded": n_idx, "src": p,
                 "absent": False}


def load_shareholders(old_root=None):
    """股东结构（单时点快照）→ {code: (natural_persons, has_inst, top1_pct)}。
    v1 只作档案背景字段如实转载，**不参与判定**（无时序，"变化"不可得）。"""
    p = _seat_path(old_root or _OLD_ROOT)
    out = {}
    if not os.path.isfile(p):
        return out
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            code = _chk_code(r.get("code"))
            if code is None:
                continue
            out[code] = (_f(r, "natural_persons"), _f(r, "has_inst"), _f(r, "top1_pct"))
    return out


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def load_lhb(old_root, date):
    """解析 lhb_<date>.md → {"file","institutions":[...],"seats":[...]}（缺 → file=None）。
    只认机器表格两段（机构榜 / 活跃席位）；解析不到即空，**不猜不补**。"""
    p = _lhb_path(old_root or _OLD_ROOT, date)
    res = {"file": p if os.path.isfile(p) else None, "institutions": [], "seats": []}
    if not res["file"]:
        return res
    seg = None
    with io.open(p, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s.startswith("**机构榜**"):
                seg = "inst"
                continue
            if s.startswith("**活跃席位**"):
                seg = "seat"
                continue
            if not s.startswith("|") or seg is None:
                continue
            cs = _cells(s)
            if not cs or cs[0] in ("rank", "id") or set(cs[0]) <= set("-"):
                continue
            if seg == "inst" and len(cs) >= 10 and _chk_code(cs[1]):
                res["institutions"].append({
                    "code": _chk_code(cs[1]), "name": cs[2],
                    "branch_count": _f({"v": cs[4]}, "v"),
                    "inst_buy_amt": _f({"v": cs[5]}, "v"),
                    "total_buy_amt": _f({"v": cs[7]}, "v"),
                    "net_buy_amt": _f({"v": cs[8]}, "v")})
            elif seg == "seat" and len(cs) >= 5:
                codes = [_chk_code(x) for x in cs[2].split(";")]
                res["seats"].append({
                    "seat_id": cs[0], "name": cs[1],
                    "codes": [c for c in codes if c],
                    "buy_amt": _f({"v": cs[4]}, "v")})
    return res


def load_inputs(old_root=None, *, cutoff=None, params_path=None, sources="dual",
                local_root=None):
    """一次性装载全部只读输入（evaluate / build_history 共用）。

    `sources` / `local_root` 透传 `load_fundflow`。自检传 sources="old" 以隔离夹具。
    """
    rules = load_rules(params_path)
    vocab = load_seat_vocab(params_path)
    by_code, dates = S3.load_klines(old_root, cutoff=cutoff)
    n_idx_kl = 0
    for c in [c for c in by_code if is_index(c)]:      # 指数不入个股域（口径常量）
        by_code.pop(c)
        n_idx_kl += 1
    flow, fstat = load_fundflow(old_root, cutoff=cutoff, sources=sources,
                                local_root=local_root)
    turn, tstat = load_turnover(old_root, cutoff=cutoff)
    chip, cstat = load_chip(old_root)
    return {"rules": rules, "vocab": vocab, "by_code": by_code, "dates": dates,
            "flow": flow, "flow_stats": fstat, "turn": turn, "turn_stats": tstat,
            "chip": chip, "chip_stats": cstat, "index_excluded_klines": n_idx_kl,
            "hold": load_shareholders(old_root)}


# ── 指标与行为判定（证据组合查表） ────────────────────────────────────────────
def _sealed_by_day(by_code, dates, js):
    """窗口各日涨停集合（每日只算一次，避免按票重复全市场遍历）。"""
    return {j: S3._sealed_codes(by_code, dates, j) for j in js}


def flow_metrics(inp, i, code, sealed_cache=None):
    """窗口指标（全因果：只用 ≤ D）→ (metrics, missing)。有效日 < flow.min_days
    → (None, [FLOW_INSUFFICIENT])。sealed_cache（{j: 涨停集}）由调用方按日预计算
    ——否则逐票重复全市场遍历（全市场扫描下是 5 倍冗余）。"""
    rules = inp["rules"]
    W = int(rules["flow"]["window"])
    min_days = int(rules["flow"]["min_days"])
    dates, flow, turn = inp["dates"], inp["flow"], inp["turn"]
    seq = flow.get(code)
    if not seq:
        return None, ["FLOW_ABSENT"]
    d = dates[i]
    js = list(range(max(0, i - W + 1), i + 1))
    win = [dates[j] for j in js]
    ok = [seq[x] for x in win if x in seq]
    if len(ok) < min_days:
        return None, ["FLOW_INSUFFICIENT:%d/%d" % (len(ok), min_days)]
    cur = seq.get(d)
    if cur is None or (cur[2] + cur[3]) <= 0:
        return None, ["FLOW_DAY_MISSING"]
    tot = cur[2] + cur[3]
    numer, denom = 0.0, 0.0
    for r in ok:
        numer += r[1]
        denom += r[2] + r[3]
    cum_net_r = (numer / denom) if denom > 0 else None
    dual = (min(cur[2], cur[3]) / max(cur[2], cur[3])) if max(cur[2], cur[3]) > 0 else None
    # 连续净流入/流出（严格交替即断）
    pos_streak = neg_streak = 0
    sign = 0
    for j in range(i, max(-1, i - 60), -1):
        r = seq.get(dates[j])
        if r is None or r[1] == 0:
            break
        s = 1 if r[1] > 0 else -1
        if sign == 0:
            sign = s
        elif s != sign:
            break
        if s > 0:
            pos_streak += 1
        else:
            neg_streak += 1
    # 换手放大（当日 / 窗口前段均值）
    tv = [turn.get(code, {}).get(x) for x in win]
    tv = [x for x in tv if x is not None]
    t_ratio = None
    if len(tv) >= 2 and tv[-1] is not None:
        base = [x for x in tv[:-1]]
        if base and statistics.fmean(base) > 0:
            t_ratio = tv[-1] / statistics.fmean(base)
    # 窗口涨幅（收盘）+ 45 日位置
    c0 = inp["by_code"].get(code, {}).get(win[0])
    c1 = inp["by_code"].get(code, {}).get(d)
    gain = (c1[0] / c0[0] - 1) if (c0 and c1 and c0[0] > 0) else None
    pos = S3._pos45(inp["by_code"].get(code, {}), d)
    # 窗口涨停天数 / 当日连板
    js2 = [j for j in js if j >= 1]
    sealed = (sealed_cache if sealed_cache is not None
              else _sealed_by_day(inp["by_code"], dates, js2))
    zt_days = sum(1 for j in js2 if code in sealed.get(j, {}))
    board = S3._board_n(inp["by_code"], dates, i, code)
    m = {"net_r": round(cur[1] / tot, 5), "cum_net_r": (round(cum_net_r, 5)
                                                       if cum_net_r is not None else None),
         "dual": (round(dual, 5) if dual is not None else None),
         "t_ratio": (round(t_ratio, 4) if t_ratio is not None else None),
         "pos_streak": pos_streak, "neg_streak": neg_streak,
         "gain": (round(gain, 5) if gain is not None else None),
         "pos": (round(pos, 4) if pos is not None else None),
         "zt_days": zt_days, "board": board,
         "main_net": cur[1], "jumbo_net": cur[4], "mid_net": cur[5],
         "small_net": cur[6], "block_net": cur[7], "close": cur[0]}
    missing = [k for k in ("cum_net_r", "dual", "t_ratio", "gain", "pos")
               if m[k] is None]
    return m, missing


def _hit(text, ok, value):
    return {"text": text, "ok": bool(ok), "value": value}


def behavior_of(m, rules):
    """证据组合 → (behavior, reason)。判优顺序 = rules.behavior.priority（外置）；
    全部必要条件命中才入档；无命中 → neutral。"""
    b = rules["behavior"]

    def _rt():
        return [_hit("双向度≥%.2f" % b["rt_dual_min"],
                     m["dual"] is not None and m["dual"] >= b["rt_dual_min"], m["dual"]),
                _hit("|主力净占比|≤%.3f" % b["rt_net_abs_max"],
                     m["net_r"] is not None and abs(m["net_r"]) <= b["rt_net_abs_max"],
                     m["net_r"]),
                _hit("换手放大≥%.2f×" % b["rt_turnover_min"],
                     m["t_ratio"] is not None and m["t_ratio"] >= b["rt_turnover_min"],
                     m["t_ratio"])]

    def _relay():
        return [_hit("窗口涨停≥%d 日" % b["relay_zt_min"],
                     m["zt_days"] >= b["relay_zt_min"], m["zt_days"]),
                _hit("主力净占比≥%.3f" % b["relay_net_min"],
                     m["net_r"] is not None and m["net_r"] >= b["relay_net_min"],
                     m["net_r"]),
                _hit("换手放大≥%.2f×" % b["relay_turnover_min"],
                     m["t_ratio"] is not None and m["t_ratio"] >= b["relay_turnover_min"],
                     m["t_ratio"])]

    def _acc():
        return [_hit("窗口累计净占比≥%.3f" % b["acc_cum_min"],
                     m["cum_net_r"] is not None and m["cum_net_r"] >= b["acc_cum_min"],
                     m["cum_net_r"]),
                _hit("连续净流入≥%d 日" % b["acc_streak_min"],
                     m["pos_streak"] >= b["acc_streak_min"], m["pos_streak"]),
                _hit("窗口涨幅≤%.3f" % b["acc_gain_max"],
                     m["gain"] is not None and m["gain"] <= b["acc_gain_max"], m["gain"])]

    def _dist():
        return [_hit("窗口累计净占比≤%.3f" % b["dist_cum_max"],
                     m["cum_net_r"] is not None and m["cum_net_r"] <= b["dist_cum_max"],
                     m["cum_net_r"]),
                _hit("45 日位置≥%.2f" % b["dist_pos_min"],
                     m["pos"] is not None and m["pos"] >= b["dist_pos_min"], m["pos"]),
                _hit("连续净流出≥%d 日" % b["dist_streak_min"],
                     m["neg_streak"] >= b["dist_streak_min"], m["neg_streak"])]

    _G = {"suspected_round_trip": _rt, "relay": _relay,
          "accumulation": _acc, "distribution": _dist}
    gates = {}
    for name in b["priority"]:
        if name not in _G:
            raise S5Error("RULE_BAD: behavior.priority 含未定义行为 %r" % name)
        gates[name] = _G[name]()
    hit = None
    for name in b["priority"]:
        if all(x["ok"] for x in gates[name]):
            hit = name
            break
    reason = {"priority": list(b["priority"]), "gates": gates,
              "matched": hit, "note": "证据组合分类，非资金意图认定"}
    return (hit or "neutral"), reason


def chip_state_of(inp, i, code, t_ratio):
    """筹码状态（三值互不推导，各由独立指标判；无筹码数据 → unknown）。"""
    r = inp["rules"]["chip"]
    seq = inp["chip"].get(code)
    d = inp["dates"][i]
    if not seq:
        return "unknown", {"available": False, "reason": "CHIP_ABSENT"}
    row = seq.get(d)
    if row is None:                     # 筹码为关注池采集：允许取 ≤D 最近一行（标实际日）
        cand = [x for x in sorted(seq) if x <= d]
        if not cand:
            return "unknown", {"available": False, "reason": "CHIP_NOT_YET"}
        d_use = cand[-1]
        row = seq[d_use]
        stale = True
    else:
        d_use, stale = d, False
    avg, c70, c90, profit, close = row
    cands = {}
    if c70 is not None and c70 <= r["conc_concentrated_max"]:
        cands["集中"] = _hit("70%%筹码区间宽度≤%.2f" % r["conc_concentrated_max"],
                             True, c70)
    if c70 is not None and c70 >= r["conc_dispersed_min"]:
        cands["分散"] = _hit("70%%筹码区间宽度≥%.2f" % r["conc_dispersed_min"],
                             True, c70)
    if ((t_ratio is not None and t_ratio >= r["turnover_sufficient_min"])
            or (profit is not None and profit >= r["profit_high_min"])):
        cands["换手充分"] = _hit("换手放大≥%.2f 或获利盘≥%.1f" % (
            r["turnover_sufficient_min"], r["profit_high_min"]), True,
            {"t_ratio": t_ratio, "profit_rate": profit})
    state = "unknown"
    for name in r["priority"]:
        if name in cands:
            state = name
            break
    ev = {"available": True, "as_of_chip": d_use, "stale": stale,
          "concentration70": c70, "concentration90": c90,
          "profit_rate": profit, "avg_cost": avg, "close": close,
          "gates": cands, "priority": list(r["priority"])}
    return state, ev


# ── 席位识别与 buyers（锁死 3：类型是代理、style 必须来自档案） ──────────────
def seat_type_of(seat_id, name, vocab):
    """席位类型（机械关键词分类；无命中 → unknown）。**不是真实资金主体认定**。"""
    text = "%s %s" % (seat_id or "", name or "")
    for st in vocab["priority"]:
        for kw in vocab["types"].get(st, []):
            if kw and kw in text:
                return st
    return "unknown"


def classify_buyers(lhb, vocab, archive):
    """龙虎榜 → buyers 列表。style 只从 archive 取（缺 → "unarchived"，禁顺推）。"""
    out = []
    for it in lhb.get("institutions", []):
        key = "inst:%s" % it["code"]
        st = seat_type_of(key, "机构专用", vocab)
        out.append({"seat_type": st, "seat_key": key,
                    "name": it.get("name"),
                    "style": archive.get(key, {}).get("label", "unarchived"),
                    "net": it.get("net_buy_amt"),
                    "amt_caliber": "龙虎榜机构净买入额（元）"})
    for s in lhb.get("seats", []):
        st = seat_type_of(s["seat_id"], s["name"], vocab)
        out.append({"seat_type": st, "seat_key": s["seat_id"], "name": s["name"],
                    "style": archive.get(s["seat_id"], {}).get("label", "unarchived"),
                    "net": s.get("buy_amt"),
                    "amt_caliber": "龙虎榜买入额（元·非净额）"})
    return out


def load_seat_archive(base=None):
    """已晋升席位档案（只读；知识库零写通道）。缺 → {}。"""
    rec = LD.load_knowledge(S_ID, "9999-12-31", base=base or ROOT)
    if rec is LD.UNAVAILABLE:
        return {}
    kb = dict(rec)
    return {k: v for k, v in (kb.get("seat_archive") or {}).items()}


# ── 零越权断言（锁死：S 只供素材） ────────────────────────────────────────────
def _no_overreach(out):
    if "action" in out:
        raise S5Error("VIOLATION: 输出不得含 action 字段（S 只供素材不越权）")
    text = json.dumps(out, ensure_ascii=False)
    if re.search(r'"action"\s*:', text):
        raise S5Error("VIOLATION: 嵌套 action 字段")
    for w in ("建议买入", "建议卖出", "目标仓位", "下单"):
        if w in text:
            raise S5Error("VIOLATION: 输出含越权指令字样 %r" % w)
    for w in _INTENT_WORDS:                # 锁死 1：禁资金意图断言
        if w in text:
            raise S5Error("VIOLATION: 输出含资金意图断言字样 %r" % w)


# ── 主入口 ────────────────────────────────────────────────────────────────────
def evaluate(as_of, *, codes=None, old_root=None, params_path=None, base=None):
    """S5 单日评估主入口 → 快照落 data/generated/s_s5_<as_of>.json。

    codes=None → 全市场（当日有行情且有资金流的票）；codes=[...] → 限定。
    """
    D = _chk_date(as_of, "as_of")
    if D < SERIES_START:
        raise S5Error("DATA_INSUFFICIENT: %s 早于序列起算日 %s" % (D, SERIES_START))
    inp = load_inputs(old_root, cutoff=_LOAD_CUTOFF, params_path=params_path)
    dates = inp["dates"]
    if D not in dates:
        raise S5Error("DATA_INSUFFICIENT: %s 不是行情序列交易日" % D)
    i = dates.index(D)
    archive = load_seat_archive(base)
    lhb = load_lhb(old_root, D)
    want = None
    if codes:
        want = []
        for c in codes:
            cc = _chk_code(c)
            if cc is None:
                raise S5Error("BAD_CODE: %r 不是合法行情码（sh/sz/bj + 6 位）" % c)
            want.append(cc)
    # 全市场域 = 当日有 klines 且有资金流的票
    pool = []
    for code in inp["by_code"]:
        if D in inp["by_code"][code] and code in inp["flow"] and D in inp["flow"][code]:
            pool.append(code)
    pool.sort()
    if want is not None:
        pool = [c for c in pool if c in set(want)]
        for c in want:
            if c not in pool:
                raise S5Error("CODE_NOT_IN_UNIVERSE: %s 当日无行情或资金流" % c)
    out_codes, n_unknown, n_neutral = {}, 0, 0
    beh_cnt = {}
    _W = int(inp["rules"]["flow"]["window"])
    sealed_cache = {j: S3._sealed_codes(inp["by_code"], dates, j)
                    for j in range(max(1, i - _W + 1), i + 1)}
    for code in pool:
        m, missing = flow_metrics(inp, i, code, sealed_cache)
        if m is None:
            beh, reason = "unknown", {"priority": list(inp["rules"]["behavior"]["priority"]),
                                      "gates": {}, "matched": None,
                                      "note": "证据不足不强行分类", "why": missing}
            chip_state, chip_ev = "unknown", {"available": False, "reason": "NO_FLOW"}
            conf = _CONF_FLOOR
        else:
            beh, reason = behavior_of(m, inp["rules"])
            chip_state, chip_ev = chip_state_of(inp, i, code, m["t_ratio"])
            conf = round(max(_CONF_FLOOR, _MAX_CONF - 0.05 * len(missing)), 2)
        buyers = []
        if lhb.get("file"):
            all_b = classify_buyers(lhb, inp["vocab"], archive)
            buyers = [b for b in all_b
                      if b["seat_key"] == "inst:%s" % code or code in
                      [c for s in lhb.get("seats", []) if s["seat_id"] == b["seat_key"]
                       for c in s.get("codes", [])]]
        if beh == "unknown":
            n_unknown += 1
        elif beh == "neutral":
            n_neutral += 1
        beh_cnt[beh] = beh_cnt.get(beh, 0) + 1
        # 全市场扫描：只落结构化票明细（neutral/unknown 只计数不落盘）；
        # 显式 --codes 请求时全部落盘（那是调用方明确要的票）。
        if want is None and beh not in _STRUCTURED:
            continue
        ev = {"metrics": {k: v for k, v in m.items()} if m else {},
              "missing": missing,
              "lhb_ref": lhb.get("file"), "chip": chip_ev,
              "shareholders": inp["hold"].get(code)}
        out_codes[code] = {
            "code": code, "behavior": beh, "behavior_reason": reason,
            "chip_state": chip_state,
            "buyers": buyers, "buyers_are_proxy": True,
            "evidence": ev, "confidence": conf}
        if beh not in _BEHAVIORS:
            raise S5Error("VIOLATION: 输出行为越界 %r" % beh)
        if chip_state not in _CHIP_STATES:
            raise S5Error("VIOLATION: 筹码状态越界 %r" % chip_state)
    n_flow_total = len(inp["flow"])
    out = {
        "schema": SCHEMA_VERSION, "s_id": S_ID, "cap": "S5", "as_of": D,
        "n_codes": len(out_codes), "n_codes_total": len(pool),
        "n_structured": sum(c for b, c in beh_cnt.items() if b in _STRUCTURED),
        "n_unknown": n_unknown, "n_neutral": n_neutral,
        "behavior_counts": beh_cnt,
        "codes_filter": ("全市场扫描只落结构化票（behavior ∈ %s）；neutral/unknown "
                         "明细不落盘但计数完整（behavior_counts/n_neutral/n_unknown）"
                         "——显式 --codes 请求时全部落盘" % (list(_STRUCTURED),)),
        "universe": {
            "n_flow_codes": n_flow_total,
            "n_codes_with_flow_today": sum(1 for c in inp["flow"] if D in inp["flow"][c]),
            "n_codes_klines": len(inp["by_code"]),
            "n_covered": len(pool),
            "coverage_ratio": round(len(pool) / n_flow_total, 4) if n_flow_total else 0.0,
            "n_index_excluded": (inp["flow_stats"].get("n_index_excluded", 0)
                                 + inp["index_excluded_klines"]),
            "flow_window": int(inp["rules"]["flow"]["window"]),
            "series_start": SERIES_START,
            "caliber": "资金流=旧包 fundflow.csv（主力/超大/中/小/大单净额，元）；"
                       "换手=klines.exchange；涨停/连板/位置=复用 s3 原语；"
                       "筹码=chip_daily.csv（关注池，非全市场）",
            "thin_coverage": bool(len(pool) < 0.5 * n_flow_total),
        },
        "codes": out_codes,
        "confidence": round(_MAX_CONF - (0.05 if not lhb.get("file") else 0), 2),
    }
    _no_overreach(out)
    gen_dir = os.path.join(base or ROOT, "data", "generated")
    snap_p = os.path.join(gen_dir, "s_%s_%s.json" % (S_ID, D))
    _write_snapshot(out, path=snap_p)
    return {"status": "OK", "output": out, "snapshot": snap_p}


# ── 唯一快照写点（X3；mkstemp 形态同 s1/s2/s3） ───────────────────────────────
def _write_snapshot(snap, path=None):
    _WG.check_write(path or SNAP_FALLBACK)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path or SNAP_FALLBACK),
                               prefix="s_s5_", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path or SNAP_FALLBACK)


# ── 历史库构建（X1 提案；知识库零写通道） ─────────────────────────────────────
def _fwd_pct(by_code, dates, i, code, k):
    """个股 T+1..T+k 涨幅累加（残窗剔除）；缺 → None。"""
    vals = []
    for j in range(i + 1, min(i + 1 + k, len(dates))):
        rows = by_code.get(code)
        if not rows:
            return None
        cur, prev = rows.get(dates[j]), rows.get(dates[j - 1])
        if not cur or not prev or prev[0] <= 0:
            return None
        vals.append(cur[0] / prev[0] - 1)
    return round(100 * sum(vals), 4) if len(vals) == k else None


def _seat_stats(inp, archive_days):
    """席位档案提案：**只用 as_of 可见切片**（锁死 4）——按席位聚合其出现日与参与票，
    标签由可观察统计机械映射（涨停占比 / T+1 溢价中位），非意图认定。"""
    dates = inp["dates"]
    idx = {d: j for j, d in enumerate(dates)}
    rules = inp["rules"]["seat"]
    seats = {}
    for d in archive_days:
        if d not in idx:
            continue
        i = idx[d]
        lhb = load_lhb(None, d)
        for it in lhb.get("institutions", []):
            key = "inst:%s" % it["code"]
            s = seats.setdefault(key, {"name": it.get("name"), "seat_type":
                                       seat_type_of(key, "机构专用", inp["vocab"]),
                                       "days": set(), "codes": set(), "t1": [], "zt": []})
            s["days"].add(d)
            s["codes"].add(it["code"])
            t1 = _fwd_pct(inp["by_code"], dates, i, it["code"], 1)
            if t1 is not None:
                s["t1"].append(t1)
            s["zt"].append(1 if it["code"] in S3._sealed_codes(inp["by_code"], dates, i)
                           else 0)
        for seat in lhb.get("seats", []):
            s = seats.setdefault(seat["seat_id"], {
                "name": seat["name"],
                "seat_type": seat_type_of(seat["seat_id"], seat["name"], inp["vocab"]),
                "days": set(), "codes": set(), "t1": [], "zt": []})
            s["days"].add(d)
            for c in seat.get("codes", []):
                s["codes"].add(c)
                t1 = _fwd_pct(inp["by_code"], dates, i, c, 1)
                if t1 is not None:
                    s["t1"].append(t1)
                s["zt"].append(1 if c in S3._sealed_codes(inp["by_code"], dates, i) else 0)
    out = {}
    for key, s in sorted(seats.items()):
        n_days, n_codes = len(s["days"]), len(s["codes"])
        zt_share = (statistics.fmean(s["zt"]) if s["zt"] else None)
        t1_med = (statistics.median(s["t1"]) if s["t1"] else None)
        ok = n_days >= int(rules["min_days"]) and n_codes >= int(rules["min_codes"])
        if not ok:
            label = "样本不足"
        elif zt_share is not None and zt_share >= rules["zt_share_min"]:
            label = "涨停簇参与型"
        elif t1_med is not None and t1_med >= rules["t1_med_min"]:
            label = "接力跟随型"
        else:
            label = "常规参与型"
        out[key] = {"name": s["name"], "seat_type": s["seat_type"],
                    "n_days": n_days, "n_codes": n_codes,
                    "zt_share": (round(zt_share, 4) if zt_share is not None else None),
                    "t1_med_pct": (round(t1_med, 4) if t1_med is not None else None),
                    "label": label, "sample_ok": bool(ok),
                    "days_visible": sorted(s["days"])}
    return out


def build_history(old_root=None, params_path=None, base=None, *, sample_min=30):
    """全序列 → 每(票,日)行 + behavior×chip_state 分组统计 + 席位档案提案 → X1 报告。"""
    inp = load_inputs(old_root, cutoff=_LOAD_CUTOFF, params_path=params_path)
    dates, by_code = inp["dates"], inp["by_code"]
    if SERIES_START not in dates:
        raise S5Error("DATA_INSUFFICIENT: 序列起算日 %s 不在行情序列" % SERIES_START)
    i_start = dates.index(SERIES_START)
    rows, groups, chip_groups = [], {}, {}
    _W = int(inp["rules"]["flow"]["window"])
    for i in range(i_start, len(dates)):
        d = dates[i]
        sealed_cache = {j: S3._sealed_codes(by_code, dates, j)
                        for j in range(max(1, i - _W + 1), i + 1)}
        for code in sorted(inp["flow"]):
            if d not in inp["flow"][code] or d not in by_code.get(code, {}):
                continue
            m, missing = flow_metrics(inp, i, code, sealed_cache)
            if m is None:
                continue
            beh, _ = behavior_of(m, inp["rules"])
            cs, _ = chip_state_of(inp, i, code, m["t_ratio"])
            t1 = _fwd_pct(by_code, dates, i, code, 1)
            t5 = _fwd_pct(by_code, dates, i, code, 5)
            rows.append({"date": d, "code": code, "behavior": beh, "chip_state": cs,
                         "t1": t1, "t5": t5, "cum_net_r": m["cum_net_r"],
                         "pos_streak": m["pos_streak"], "neg_streak": m["neg_streak"],
                         "zt_days": m["zt_days"], "t_ratio": m["t_ratio"]})
    def _agg(g):
        t1 = [x["t1"] for x in g if x.get("t1") is not None]
        t5 = [x["t5"] for x in g if x.get("t5") is not None]
        return {"n_days": len(g),
                "fwd_t1_mean_pct": round(statistics.fmean(t1), 4) if t1 else None,
                "fwd_t5_mean_pct": round(statistics.fmean(t5), 4) if t5 else None,
                "sample_ok": len(g) >= sample_min}
    by_b = {}
    by_bc = {}
    for r in rows:
        by_b.setdefault(r["behavior"], []).append(r)
        by_bc.setdefault("%s|%s" % (r["behavior"], r["chip_state"]), []).append(r)
    for k, g in by_b.items():
        groups[k] = _agg(g)
    for k, g in by_bc.items():
        chip_groups[k] = _agg(g)
    seat_days = sorted({os.path.basename(x)[4:-3] for x in
                        [(_lhb_path(old_root or _OLD_ROOT, d)) for d in dates]
                        if os.path.isfile(x)})
    seat_archive = _seat_stats(inp, seat_days)
    proposal = {
        "schema": SCHEMA_VERSION, "s_id": S_ID, "cap": "S5", "as_of": dates[-1],
        "data_end": dates[-1], "series_start": SERIES_START,
        "n_rows_total": len(rows), "n_codes": len(inp["flow"]),
        "n_min_trust": sample_min,
        "method": "behavior = 证据组合查表（阈值 params.s5_rules，分布对齐非结果验证）；"
                  "T+1/T+5 = 个股涨幅累加（含 beta，与 S1 CAR / S3 涨停溢价 / S2 题材 "
                  "等权均不同属，禁混读）；残窗剔除",
        "universe": {"n_flow_codes": inp["flow_stats"]["n_codes"],
                     "n_flow_rows": inp["flow_stats"]["n_rows"],
                     "n_chip_rows": inp["chip_stats"]["n_rows"],
                     "coverage_note": "fundflow 09-19 起每日行数骤降（采集衰减）→ "
                                      "覆盖弱日的分组样本须按 n_days 判读"},
        "groups": groups, "chip_groups": chip_groups,
        "seat_archive": seat_archive,
        "seat_universe": {"n_seats": len(seat_archive), "days": seat_days},
        "rows_head": rows[:12],
    }
    os.makedirs(os.path.join(base or ROOT, "data", "generated", "reports",
                             "s5_backfill"), exist_ok=True)
    with io.open(os.path.join(base or ROOT, "data", "generated", "reports",
                              "s5_backfill", "summary.json"),
                 "w", encoding="utf-8", newline="\n") as f:
        json.dump(proposal, f, ensure_ascii=False, indent=1)
    return {"report": os.path.join(base or ROOT, "data", "generated", "reports",
                                   "s5_backfill", "summary.json"),
            "proposal": proposal}


def history_check(old_root=None, params_path=None, base=None):
    """复算对账已晋升知识库（只读）：库内 groups 与复算逐格比对。"""
    rec = LD.load_knowledge(S_ID, "9999-12-31", base=base or ROOT)
    if rec is LD.UNAVAILABLE:
        raise S5Error("KB_ABSENT: s5 知识库未晋升或路由缺席")
    kb = dict(rec)
    if not kb.get("groups"):
        raise S5Error("KB_SHAPE: 已晋升库缺 groups")
    rep = build_history(old_root, params_path, base=base)["proposal"]
    diffs = []
    for beh, g in kb.get("groups", {}).items():
        r = rep["groups"].get(beh)
        if r is None:
            diffs.append({"behavior": beh, "kind": "ABSENT_IN_RECOMPUTE"})
            continue
        for f in ("n_days", "fwd_t1_mean_pct", "fwd_t5_mean_pct"):
            if g.get(f) != r.get(f):
                diffs.append({"behavior": beh, "field": f, "kb": g.get(f),
                              "recomputed": r.get(f)})
    return {"reproduced": not diffs, "diffs": diffs, "kb_as_of": kb.get("as_of"),
            "recomputed_as_of": rep["as_of"]}


def read_structure(as_of, base=None):
    """当日资金结构快照真链路读取（loader.load_s_output；缺 → UNAVAILABLE 原样返回）。"""
    return LD.load_s_output(S_ID, _chk_date(as_of, "as_of"), base=base)


# ── CLI ───────────────────────────────────────────────────────────────────────
def _main(argv):
    ap = argparse.ArgumentParser(description="S5 资金筹码结构")
    ap.add_argument("--eval", metavar="AS_OF")
    ap.add_argument("--codes", help="逗号分隔行情码（缺省=全市场）")
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
        cs = [x.strip() for x in a.codes.split(",")] if a.codes else None
        r = evaluate(a.eval, codes=cs, old_root=a.old_root, params_path=a.params,
                     base=a.base)
        o = r["output"]
        if a.stdout:
            print(json.dumps(o, ensure_ascii=False, indent=1))
        print("快照: %s" % r["snapshot"])
        print("覆盖 %d 票（资金流池 %d · coverage %.3f · thin=%s）· 结构化 %d · 落盘 %d"
              % (o["n_codes_total"], o["universe"]["n_flow_codes"],
                 o["universe"]["coverage_ratio"], o["universe"]["thin_coverage"],
                 o["n_structured"], o["n_codes"]))
        print("行为分布: %s" % json.dumps(o["behavior_counts"], ensure_ascii=False))
        return 0
    if a.history_build:
        r = build_history(a.old_root, a.params)
        p = r["proposal"]
        print("X1 提案: %s" % r["report"])
        print("行 %d · behavior 组 %d · behavior×chip 组 %d · 席位 %d"
              % (p["n_rows_total"], len(p["groups"]), len(p["chip_groups"]),
                 p["seat_universe"]["n_seats"]))
        for k, v in sorted(p["groups"].items()):
            print("  %-24s n=%-6d T+1 %-10s T+5 %-10s sample_ok=%s"
                  % (k, v["n_days"], v["fwd_t1_mean_pct"], v["fwd_t5_mean_pct"],
                     v["sample_ok"]))
        return 0
    if a.history_check:
        r = history_check(a.old_root, a.params)
        print("复算对账: %s（diffs=%d · kb_as_of=%s）"
              % ("reproduced ✅" if r["reproduced"] else "不一致 ❌",
                 len(r["diffs"]), r["kb_as_of"]))
        for d in r["diffs"][:10]:
            print("  %s" % json.dumps(d, ensure_ascii=False))
        return 0 if r["reproduced"] else 1
    ap.print_help()
    return 2


# ── 自检（临时沙盒：夹具只读源 → 断言；真库零污染） ──────────────────────────
_FIX_DATES = (["2026-04-%02d" % d for d in range(1, 23)]
              + ["2026-05-%02d" % d for d in range(1, 21)]
              + ["2026-06-%02d" % d for d in range(1, 9)])   # 50 个交易日（≥45 日位置窗口）


def _mk_fixtures(root):
    """沙盒夹具：klines / fundflow / chip_daily / lhb 四源（目录结构与旧包一致）。
    五票各承载一个行为门（sz000005 仅末日 → unknown）：证据门在夹具上真正可达。"""
    bus = os.path.join(root, "data", "raw", "bus")
    os.makedirs(os.path.join(bus, "lhb"), exist_ok=True)
    os.makedirs(os.path.join(root, "data", "generated"), exist_ok=True)
    n = len(_FIX_DATES)
    px = {"sz000001": 10.0, "sz000002": 10.0, "sz000003": 20.0,
          "sz000004": 30.0, "sz000005": 5.0}
    kl, ff = [], []
    for di, d in enumerate(_FIX_DATES):
        last = (di == n - 1)
        for code in ("sz000001", "sz000002", "sz000003", "sz000004", "sz000005"):
            if code == "sz000005" and not last:
                continue
            prev = px[code]
            if code == "sz000002":            # 前段横盘、末两日涨停 → relay
                close = round(prev * (1.10 if di >= n - 2 else 1.0), 2)
                turn = 8.0 if di >= n - 3 else 3.0
            elif code == "sz000001":          # 缓涨（5 日 ≈ +2%）→ accumulation
                close, turn = round(prev * 1.004, 2), 2.0
            elif code == "sz000004":          # 持续上涨至 45 日高位 → distribution
                close, turn = round(prev * 1.010, 2), 6.0
            elif code == "sz000003":          # 横盘 + 末日换手放大 → round_trip
                close, turn = round(prev, 2), (20.0 if last else 12.0)
            else:
                close, turn = round(prev, 2), 3.0
            high, low = round(close * 1.01, 2), round(close * 0.99, 2)
            kl.append([code, "N%s" % code[-2:], d, round(prev, 2), close, high, low,
                       10000.0, 1000000.0 + di * 1000, turn,
                       round((close / prev - 1) * 100, 2)])
            px[code] = close
            inamt = {"sz000001": 10e6, "sz000002": 20e6, "sz000003": 50e6,
                     "sz000004": 8e6, "sz000005": 5e6}[code]
            if code == "sz000001":            # 连续净流入、量级温和
                outamt, net = inamt * 0.8, inamt * 0.2
            elif code == "sz000002":          # 涨停日大额净流入
                outamt, net = ((inamt * 0.5, inamt * 0.5) if di >= n - 2
                               else (inamt * 0.9, inamt * 0.1))
            elif code == "sz000003":          # 双向都大、净额极小
                outamt, net = inamt * 0.99, inamt * 0.01
            elif code == "sz000004":          # 持续净流出
                outamt, net = inamt * 1.5, -inamt * 0.5
            else:
                outamt, net = inamt * 0.9, inamt * 0.1
            ff.append([d, code, close, net, inamt, outamt, net * 0.6, net * 0.2,
                       -net * 0.1, net * 0.3])
        # 指数行（sh000001 上证指数）——验证"指数不入个股域"的排除与计次口径
        kl.append(["sh000001", "上证指数", d, 3000.0, 3010.0, 3020.0, 2990.0,
                   1000000000.0, 500000000000.0, 0.0, 0.33])
        ff.append([d, "sh000001", 3010.0, 1e8, 5e8, 4e8, 5e7, 2e7, -3e7, 1e7])
    with io.open(os.path.join(bus, "klines.csv"), "w", encoding="utf-8", newline="\n") as f:
        w = csv.writer(f)
        w.writerow(["code", "name", "date", "open", "close", "high", "low",
                    "volume", "amount", "exchange", "change_pct"])
        w.writerows(kl)
    with io.open(os.path.join(bus, "fundflow.csv"), "w", encoding="utf-8",
                 newline="\n") as f:
        w = csv.writer(f)
        w.writerow(["date", "code", "close", "main_net", "main_in", "main_out",
                    "jumbo_net", "mid_net", "small_net", "block_net"])
        w.writerows(ff)
    # chip_daily（只给 sz000001 一只 → 其余 unknown）
    with io.open(os.path.join(bus, "chip_daily.csv"), "w", encoding="utf-8",
                 newline="\n") as f:
        w = csv.writer(f)
        w.writerow(["date", "code", "name", "chipAvgCost", "chipConcentration70",
                    "chipConcentration90", "chipProfitRate", "closePrice"])
        for d in _FIX_DATES:
            w.writerow([d, "sz000001", "N01", 9.5, 8.0, 15.0, 88.0, 10.0])
    # lhb（仅最后一日）
    lhb = os.path.join(bus, "lhb", "lhb_%s.md" % _FIX_DATES[-1])
    with io.open(lhb, "w", encoding="utf-8", newline="\n") as f:
        f.write("# 龙虎榜全市场存档 %s（夹具）\n\n**机构榜**（1只）\n\n" % _FIX_DATES[-1])
        f.write("| rank | code | name | tdDays | instBuyBranchCount | instBuyAmt | "
                "instBuyRate | totalBuyAmt | netBuyAmt | netBuyRate |\n")
        f.write("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n")
        f.write("| 1 | sz000002 | N02 | 1 | 3 | 30000000 | 5.0 | 50000000 | "
                "20000000 | 4.0 |\n\n**活跃席位**（2个）\n\n")
        f.write("| id | name | code | stockName | buyAmt |\n")
        f.write("| --- | --- | --- | --- | --- |\n")
        f.write("| T000000001 | 中信证券西安朱雀大街 | sz000002;sz000003 | "
                "N02;N03 | 12000000 |\n")
        f.write("| sgt | 深股通专用 | sz000001 | N01 | 8000000 |\n")
    return root


def _rules_params(path, extra=None, drop=()):
    """夹具 params：读真 params，替换 s5_rules/s5_seats 子树（保留真实阈值）。"""
    doc = json.loads(io.open(path, encoding="utf-8").read())
    for k in drop:
        doc["params"].pop(k, None)
    for k, v in (extra or {}).items():
        doc["params"][k] = v
    tmp = tempfile.mkdtemp(prefix="s5_prm_")
    p = os.path.join(tmp, "params.json")
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    return p


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
    # 真库产物指纹（运行前取，G 段比对 —— 沙盒全程不得改动真库）
    def _fp(p):
        return os.path.getsize(p) if os.path.isfile(p) else None
    _fp0 = {p: _fp(p) for p in (
        os.path.join(ROOT, "data", "generated", "s_s5_%s.json" % _FIX_DATES[-1]),
        os.path.join(ROOT, "data", "generated", "reports", "s5_backfill",
                     "summary.json"))}
    PARAM_P = os.path.join(ROOT, "data", "params.json")
    rules = load_rules(PARAM_P)
    vocab = load_seat_vocab(PARAM_P)
    box = tempfile.mkdtemp(prefix="s5_box_")
    _mk_fixtures(box)
    # ★ `sources="old"`：自检只锁沙盒单一源。
    #   默认 dual 会把沙盒里的 local 源也读进来（若存在），使「指数排除计数」
    #   随夹具外的文件变化 ⇒ 断言不稳。双源行为由 A11 单独覆盖。
    inp = load_inputs(box, cutoff=None, params_path=PARAM_P, sources="old")

    # ── A 输入装载与口径 ──
    T("A1 klines 装载（复用 s3 原语）", len(inp["by_code"]) == 5
      and len(inp["dates"]) == len(_FIX_DATES))
    T("A2 fundflow 装载与 8 元组字段", len(inp["flow"]) == 5
      and len(inp["flow"]["sz000001"]) == len(_FIX_DATES))
    T("A3 换手视图独立于 s3 视图", "exchange" not in (inp["by_code"]["sz000001"] and
                                                      ["x"]) and
      inp["turn"]["sz000003"][_FIX_DATES[0]] == 12.0)
    T("A4 坏码计次上报（禁静默丢）", "n_code_bad" in inp["flow_stats"]
      and inp["flow_stats"]["n_code_bad"] == 0)
    T("A5 chip 装载（关注池）", len(inp["chip"]) == 1
      and len(inp["chip"]["sz000001"]) == len(_FIX_DATES))
    T("A6 股东结构单时点（缺文件不抛）", isinstance(inp["hold"], dict))
    lhb = load_lhb(box, _FIX_DATES[-1])
    T("A7 lhb 机构榜解析", len(lhb["institutions"]) == 1
      and lhb["institutions"][0]["code"] == "sz000002")
    T("A8 lhb 活跃席位解析（多股分号切分）", len(lhb["seats"]) == 2
      and lhb["seats"][0]["codes"] == ["sz000002", "sz000003"])
    T("A9 lhb 缺日 → file=None 不猜", load_lhb(box, "2026-01-01")["file"] is None)
    T("A10 指数码排除且计次上报（禁混入个股域）",
      "sh000001" not in inp["by_code"] and "sh000001" not in inp["flow"]
      and inp["flow_stats"]["n_index_excluded"] == len(_FIX_DATES)
      and inp["turn_stats"]["n_index_excluded"] == len(_FIX_DATES)
      and inp["index_excluded_klines"] == 1)

    # ── A11~A14 双源（2026-10-23：旧包历史 + 本项目自采） ──
    # 两个**独立**沙盒：老源夹具在 box，「本项目」在 lbox。
    # 不能共用一个 base —— 相对布局相同（都是 <root>/data/raw/bus/fundflow.csv），
    # 共用会让双源指向同一文件、退化成单源。
    FCOLS = ("date", "code", "close", "main_net", "main_in", "main_out",
             "jumbo_net", "mid_net", "small_net", "block_net")
    lbox = tempfile.mkdtemp(prefix="s5_lbox_")
    localp = _local_flow_path(lbox)
    os.makedirs(os.path.dirname(localp), exist_ok=True)
    with io.open(localp, "w", encoding="utf-8", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=FCOLS, lineterminator="\n")
        wr.writeheader()
        for dd, cl in (("2099-01-02", "99.0"), (_FIX_DATES[0], "777.0")):
            wr.writerow({"date": dd, "code": "sz000001", "close": cl,
                         "main_net": "1.0", "main_in": "1.0", "main_out": "1.0",
                         "jumbo_net": "1.0", "mid_net": "1.0",
                         "small_net": "1.0", "block_net": "1.0"})
    try:
        f_dual, st_dual = load_fundflow(box, sources="dual", local_root=lbox)
        f_old, st_old = load_fundflow(box, sources="old", local_root=lbox)
        f_loc, st_loc = load_fundflow(box, sources="local", local_root=lbox)
        T("A11 dual 合并两源（旧包码数不变 + 自采新日期并进来）",
          len(f_dual) == len(f_old) and "2099-01-02" in f_dual["sz000001"],
          "dual码=%d old码=%d" % (len(f_dual), len(f_old)))
        T("A12 同日冲突以本项目为准（自采=最新口径，非旧包）",
          f_dual["sz000001"][_FIX_DATES[0]][0] == 777.0,
          str(f_dual["sz000001"][_FIX_DATES[0]][0]))
        T("A13 sources 可单源（old 无自采日 / local 仅自采 1 码）",
          "2099-01-02" not in f_old["sz000001"]
          and len(f_loc) == 1 and "2099-01-02" in f_loc["sz000001"],
          "old含2099=%s loc码=%d" % ("2099-01-02" in f_old["sz000001"],
                                     len(f_loc)))
        T("A14 源登记完整（dual 两源在案且无缺失）",
          len(st_dual["src"]) == 2 and not st_dual["src_missing"],
          "src=%s missing=%s" % ([s["tag"] for s in st_dual["src"]],
                                 st_dual["src_missing"]))
        # A15 缺源不静默：指向空目录时 local 缺席须进 src_missing，但仍用 old 出数
        empty = tempfile.mkdtemp(prefix="s5_empty_")
        try:
            f_neg, st_neg = load_fundflow(box, sources="dual", local_root=empty)
            T("A15 单源缺席进 src_missing 且不阻断（用另一源出数）",
              len(st_neg["src_missing"]) == 1 and len(st_neg["src"]) == 1
              and len(f_neg) == len(f_old),
              "missing=%d src=%d" % (len(st_neg["src_missing"]),
                                     len(st_neg["src"])))
        finally:
            shutil.rmtree(empty, ignore_errors=True)
    finally:
        shutil.rmtree(lbox, ignore_errors=True)

    # ── B 指标与行为判定 ──
    i_last = len(inp["dates"]) - 1
    m1, _ = flow_metrics(inp, i_last, "sz000001")
    T("B1 accumulation 指标（连续净流入+累计净占比）", m1["pos_streak"] >= 3
      and m1["cum_net_r"] > 0)
    b1, r1 = behavior_of(m1, rules)
    T("B2 accumulation 判定", b1 == "accumulation", extra="got=%s" % b1)
    T("B2b 判定带证据三元（hit/ok/value）", all("ok" in x and "value" in x and "text" in x
                                                for g in r1["gates"].values() for x in g))
    m2, _ = flow_metrics(inp, i_last, "sz000002")
    b2, _ = behavior_of(m2, rules)
    T("B3 relay 判定（窗口涨停+净流入）", b2 == "relay", extra="got=%s" % b2)
    m3, _ = flow_metrics(inp, i_last, "sz000003")
    b3, _ = behavior_of(m3, rules)
    T("B4 round_trip 判定（双向度+窄净额+换手放大）", b3 == "suspected_round_trip",
      extra="got=%s" % b3)
    m4, _ = flow_metrics(inp, i_last, "sz000004")
    b4, _ = behavior_of(m4, rules)
    T("B5 distribution 判定（净流出+高位）", b4 == "distribution", extra="got=%s" % b4)
    T("B6 判优顺序外置且被遵守", r1["priority"] == list(rules["behavior"]["priority"]))
    b6, _ = behavior_of({"dual": 0.95, "net_r": 0.001, "t_ratio": 2.0, "zt_days": 3,
                         "cum_net_r": 0.10, "pos_streak": 5, "gain": 0.02,
                         "neg_streak": 0, "pos": 0.9}, rules)
    T("B7 多门同命中时取判优顺序首位", b6 == "suspected_round_trip", extra="got=%s" % b6)
    b7, _ = behavior_of({"dual": 0.5, "net_r": 0.0, "t_ratio": 0.5, "zt_days": 0,
                         "cum_net_r": 0.0, "pos_streak": 0, "gain": None,
                         "neg_streak": 0, "pos": None}, rules)
    T("B8 无命中 → neutral（不硬塞四类）", b7 == "neutral")
    T("B9 有效日不足 → unknown（锁死 2）",
      flow_metrics(inp, 1, "sz000001")[0] is None
      and "FLOW_INSUFFICIENT" in flow_metrics(inp, 1, "sz000001")[1][0])
    T("B10 无资金流票 → FLOW_ABSENT", flow_metrics(inp, i_last, "sz999999")[1]
      == ["FLOW_ABSENT"])
    cs, cev = chip_state_of(inp, i_last, "sz000001", m1["t_ratio"])
    T("B11 chip_state 有数据可判（集中/分散/换手充分）", cs in ("集中", "分散", "换手充分"),
      extra="got=%s" % cs)
    T("B12 chip_state 无数据 → unknown 不用其他指标代理",
      chip_state_of(inp, i_last, "sz000002", None)[0] == "unknown")

    # ── C 席位（锁死 3） ──
    T("C1 席位类型：机构榜 → 机构", seat_type_of("inst:sz000002", "机构专用", vocab) == "机构")
    T("C2 席位类型：股通 → 北向", seat_type_of("sgt", "深股通专用", vocab) == "北向")
    T("C3 席位类型：拉萨 → 拉萨系", seat_type_of("T1", "东方财富证券拉萨团结路", vocab) == "拉萨系")
    T("C4 席位类型无命中 → unknown（不硬分类）",
      seat_type_of("x", "某某", vocab) == "unknown")
    buyers = classify_buyers(lhb, vocab, {})
    T("C5 buyers 无档案 → style=unarchived（禁顺推）",
      all(b["style"] == "unarchived" for b in buyers) and len(buyers) == 3)
    T("C5b buyers 声明为代理（非真实资金主体）",
      all("seat_key" in b and b.get("amt_caliber") for b in buyers))
    buyers2 = classify_buyers(lhb, vocab, {"sgt": {"label": "常规参与型"}})
    T("C6 style 来自档案（有档案即取档案标签）",
      [b["style"] for b in buyers2 if b["seat_key"] == "sgt"] == ["常规参与型"])

    # ── D 输出契约与零越权 ──
    r = evaluate(_FIX_DATES[-1], old_root=box, params_path=PARAM_P, base=box)
    o = r["output"]
    T("D1 快照落盘 s_s5_<as_of>.json",
      os.path.basename(r["snapshot"]) == "s_s5_%s.json" % _FIX_DATES[-1])
    T("D2 全市场域（klines∩flow）", o["n_codes_total"] == 5)
    T("D3 行为/筹码枚举恒在值域",
      all(v["behavior"] in _BEHAVIORS and v["chip_state"] in _CHIP_STATES
          for v in o["codes"].values()))
    T("D4 零 action 字段（S 只供素材）",
      not any("action" in v for v in o["codes"].values()) and "action" not in o)
    T("D5 每条带 evidence + confidence",
      all(v["evidence"] and 0 < v["confidence"] <= _MAX_CONF
          for v in o["codes"].values()))
    T("D6 unknown 计数一致", o["n_unknown"] == o["behavior_counts"].get("unknown", 0) == 1)
    T("D11 全市场快照只落结构化票（计数完整不丢信息）",
      all(v["behavior"] in _STRUCTURED for v in o["codes"].values())
      and o["n_codes"] == o["n_structured"]
      and (o["n_structured"] + o["n_neutral"] + o["n_unknown"]) == o["n_codes_total"]
      and "codes_filter" in o)
    T("D7 大票带 lhb buyers、且 amount 口径声明",
      len(o["codes"]["sz000002"]["buyers"]) >= 1
      and o["codes"]["sz000002"]["buyers"][0]["amt_caliber"])
    T("D8 universe 报覆盖率与 thin 标记",
      "coverage_ratio" in o["universe"] and "thin_coverage" in o["universe"])
    T("D9 loader 真链路可回读（load_s_output）",
      LD.load_s_output(S_ID, _FIX_DATES[-1], base=box) is not LD.UNAVAILABLE)
    T("D10 --codes 限定生效", evaluate(_FIX_DATES[-1], codes=["sz000001"],
                                        old_root=box, params_path=PARAM_P,
                                        base=box)["output"]["n_codes"] == 1)

    # ── E 拒绝路径 ──
    def _raises(fn, frag):
        try:
            fn()
        except S5Error as e:
            return frag in str(e)
        return False
    T("E1 早于起算日 → DATA_INSUFFICIENT",
      _raises(lambda: evaluate("2026-05-01", old_root=box, params_path=PARAM_P,
                               base=box), "DATA_INSUFFICIENT"))
    T("E2 非法日期 → BAD_DATE",
      _raises(lambda: evaluate("2026/06/01", old_root=box, params_path=PARAM_P,
                               base=box), "BAD_DATE"))
    T("E3 非法码 → BAD_CODE",
      _raises(lambda: evaluate(_FIX_DATES[-1], codes=["600000"], old_root=box,
                               params_path=PARAM_P, base=box), "BAD_CODE"))
    T("E4 域外码 → CODE_NOT_IN_UNIVERSE",
      _raises(lambda: evaluate(_FIX_DATES[-1], codes=["sh600000"], old_root=box,
                               params_path=PARAM_P, base=box), "CODE_NOT_IN_UNIVERSE"))
    p_no = _rules_params(PARAM_P, drop=[_RULE_KEY])
    T("E5 缺 s5_rules → RULES_MISSING",
      _raises(lambda: load_rules(p_no), "RULES_MISSING"))
    _item = json.loads(io.open(PARAM_P, encoding="utf-8").read())["params"][_RULE_KEY]
    _v = json.loads(json.dumps(_item["value"]))
    _v["behavior"].pop("priority", None)
    p_pri = _rules_params(PARAM_P, extra={_RULE_KEY: dict(_item, value=_v)})
    T("E5b 缺判优顺序 → RULES_MISSING",
      _raises(lambda: load_rules(p_pri), "RULES_MISSING"))
    T("E6 越权文案 → VIOLATION",
      _raises(lambda: _no_overreach({"note": "机构正在吸筹"}), "VIOLATION"))
    T("E6b action 字段 → VIOLATION",
      _raises(lambda: _no_overreach({"action": "BUY"}), "VIOLATION"))
    _v2 = json.loads(json.dumps(_item["value"]))
    _v2["behavior"]["priority"] = ["no_such_behavior"]
    p_bad = _rules_params(PARAM_P, extra={_RULE_KEY: dict(_item, value=_v2)})
    T("E7 判优顺序含未定义行为 → RULE_BAD",
      _raises(lambda: behavior_of(m1, load_rules(p_bad)), "RULE_BAD"))

    # ── F 历史库与席位档案（沙盒） ──
    hb = build_history(box, PARAM_P, base=box)
    prop = hb["proposal"]
    T("F1 X1 提案落 data/generated/reports/s5_backfill/summary.json",
      os.path.isfile(os.path.join(box, "data", "generated", "reports",
                                  "s5_backfill", "summary.json")))
    T("F2 分组含 behavior 与 behavior×chip 两视图",
      bool(prop["groups"]) and bool(prop["chip_groups"]))
    T("F3 T+1/T+5 口径为个股涨幅累加且残窗剔除",
      all(("fwd_t1_mean_pct" in v and "fwd_t5_mean_pct" in v)
          for v in prop["groups"].values()))
    T("F4 样本不足标 sample_ok=False（禁出统计结论）",
      all(v["sample_ok"] == (v["n_days"] >= 30) for v in prop["groups"].values()))
    T("F5 席位档案只用可见切片 + 标签机械映射",
      all(("days_visible" in v and v["label"] in
           ("样本不足", "涨停簇参与型", "接力跟随型", "常规参与型"))
          for v in prop["seat_archive"].values()))
    T("F6 席位样本不足 → label=样本不足 + sample_ok=False",
      all((not v["sample_ok"]) == (v["label"] == "样本不足")
          for v in prop["seat_archive"].values()))
    T("F7 知识库未晋升 → history_check 报 KB_ABSENT",
      _raises(lambda: history_check(box, PARAM_P, base=box), "KB_ABSENT"))
    T("F8 read_structure 缺快照 → UNAVAILABLE 原样返回",
      read_structure("2026-01-02", base=box) is LD.UNAVAILABLE)

    # ── G 真库零污染（夹具全程在沙盒；真库产物指纹前后比对） ──
    T("G1 沙盒运行不改写真库产物（快照 + X1 报告 指纹不变）",
      all(_fp(p) == _fp0[p] for p in _fp0))
    T("G2 沙盒快照落沙盒（不越界写真库）",
      os.path.isfile(os.path.join(box, "data", "generated",
                                  "s_s5_%s.json" % _FIX_DATES[-1])))

    print("自检结果：%s（%d/%d）" % ("通过 ✅" if not fail else "失败 ❌", ok, ok + fail))
    if fails:
        print("  失败项: %s" % fails)
    return not fail


def _discipline(verbose=True):
    """纪律判据（AST/文本机械断言；生产区窗口 = 去掉 _selftest/_discipline/_main）。"""
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
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name.startswith(
                ("_selftest", "_discipline", "_main", "_mk_fixtures", "_rules_params")):
            drop.update(range(node.lineno - 1, node.end_lineno))
    prod = "".join(l for i, l in enumerate(lines) if i not in drop)

    for mod in ("settle", "wakeup", "three_q", "decision", "position_gate",
                "exit_gate", "orchestrate", "drive", "candidates", "thesis",
                "landing", "observe", "pool_match", "scan", "regime"):
        T("① 零 import %s" % mod,
          not re.search(r"^import\s+%s\b|^from\s+[\w.]*%s\b\s+import" % (mod, mod),
                        prod, re.M))
    T("①b 复用 s3 原语（零复制涨停规则）",
      "import s3 as S3" in prod and "S3._sealed_codes(" in prod
      and "S3._board_n(" in prod and "round(prev[0]" not in prod)
    T("② 唯一快照写点（mkstemp 形态）",
      ("def _write_snapshot(" + "snap, path=None):") in prod
      and ("os.replace(tmp, " + "path or SNAP_FALLBACK)") in prod)
    T("②b 写模式 io.open 计数 ≤2（快照 + X1 报告）",
      len(re.findall(r'io\.open\([^)]*"[w]', prod)) <= 2)
    T("③ 阈值经 PARAMS.get_value", "PARAMS.get_value(" in prod)
    T("③b 规则键拼装构造（防词表自指）",
      ('"s5" + "_rules"') in prod and ('"s5" + "_seats"') in prod)
    T("④ 零 datetime 依赖", "datetime" not in prod)
    _FORBIDDEN = ("or" + "der", "bu" + "y", "se" + "ll", "wa" + "it",
                  "po" + "sition_gate")
    # 词边界匹配：S5 领域词（buyers / buy_amt / net_buy_amt）不得被误判为越权词
    T("⑤ 零越权词（%s）" % "·".join(_FORBIDDEN),
      not [w for w in _FORBIDDEN
           if re.search(r"\b%s\b" % re.escape(w), prod)])
    T("⑤b 零资金意图断言词（锁死 1，拼装词表）",
      not [w for w in _INTENT_WORDS if w in prod])
    T("⑥ 快照名口径（s_s5_ + fallback S5 字面量）",
      '"s_%s_%s.json"' in prod and "s_S5_MISSING.json" in prod)
    T("⑦ 知识库零写点", not re.search(r'open\([^)]*knowledge[^)]*"[w]', prod))
    T("⑦b 席位档案只经 loader 读（零直读文件写通道）",
      "def load_seat_archive(" in prod and "LD.load_knowledge(S_ID" in prod)
    T("⑧ 行为枚举与筹码枚举封闭（值域安全网）",
      "_BEHAVIORS = (" in prod and "_CHIP_STATES = (" in prod
      and "not in _BEHAVIORS" in prod and "not in _CHIP_STATES" in prod)
    T("⑧b 行为判定单调优顺序（priority 外置，禁硬编码分支）",
      'b["priority"]' in prod and 'gates[name]' in prod)
    T("⑨ buyers 声明为代理（非真实资金主体，锁死 3）",
      "buyers_are_proxy" in prod and "unarchived" in prod)
    T("⑩ 筹码三值互不推导（各由独立指标判）",
      'cands["集中"]' in prod and 'cands["分散"]' in prod
      and 'cands["换手充分"]' in prod)

    print("纪律结果：%s（%d/%d）" % ("通过 ✅" if not fail else "失败 ❌", ok, ok + fail))
    if fails:
        print("  失败项: %s" % fails)
    return not fail


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
