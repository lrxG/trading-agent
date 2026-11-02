#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""seed_real.py · P0 生产历史数据播种（旧系统包只读历史数据 → 生产 data/raw + data/snapshot）

用途：把「旧系统包」的只读历史数据目录，按其**已核验的字段映射**转成 0.2 契约的
      核心 5 键，经既有库函数落进生产数据面：
        data/raw/<date>/<契约文件> + _core.meta.json      （ingest.stage_core）
        data/snapshot/<date>/                              （ingest.promote_snapshot）

★ 边界（P0 授权范围，逐条落实）：
  · 源目录**只读**：全程只以 "r" 模式打开源文件；入口断言源根存在且不含本项目 data 目录。
  · **零重实现**：快照正式化 = 转发 `snapshot.write_snapshot`（经 ingest.promote_snapshot），
    投放 = 转发 `ingest.stage_core`。本件不实现快照/游标/结算逻辑，不推进游标、不触发决策。
  · 冲突即停止：目标已有非预期内容 → 报告并退出，**绝不覆盖**（不自动 replace）。
  · 失败可恢复：转换/校验失败**不留半份生产产物**（校验全部先于写盘；ingest 自身原子写）。
  · 不伪造时效性：`data_upto` = 源数据在目标日的实际覆盖截止（恒等于目标日）；历史日期
    如实保留，绝不把历史快照标成当前市场状态。
  · ★**未决口径自我拒绝**：每个契约字段在 `CALIBER` 登记「来源列 + 状态」。只要目标键含
    任一非 VERIFIED 字段，`--stage` / `--promote` 一律**拒绝执行并指名**，绝不写入。

数据源（旧包只读目录，全部路径在 _status() 中打印）：
  bus/klines.csv                     全市场个股日线（含 sh000001 指数行）
  bus/zt_pool/zt_<date>_<HHMM|hist>.csv   涨停池快照（lbc/fbt/fund/hybk/zbc）
  bus/notices/notice_<date>.csv      东财全市场公告流（art_code/time/code/name/column/title）
  bus/env_history.csv                市场宽度（westock market-overview 逐日归档）
  bus/lhb/lhb_<date>.md              龙虎榜存档（机构榜 / 活跃席位）

用法（默认 --check，全部只读）：
  python tools/e2e/seed_real.py --check           # 只读预检：源清单 + 映射矩阵 + 覆盖交集
  python tools/e2e/seed_real.py --plan            # 只读：内存转换 + 契约校验 + 待写文件清单
  python tools/e2e/seed_real.py --verify          # 隔离副本：幂等/冲突/失败安全自检
  python tools/e2e/seed_real.py --stage    --execute   # 写 data/raw（未决口径未清则拒绝）
  python tools/e2e/seed_real.py --promote  --execute   # 写 data/snapshot（同上）

规则：本件禁 import 任何数据源客户端（网络/子进程）；禁 LLM 补数；禁自造字段。
"""
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
from decimal import Decimal, ROUND_HALF_UP

_HERE = os.path.dirname(os.path.abspath(__file__))
_FS = os.path.normpath(os.path.join(_HERE, "..", "fs"))
if _FS not in sys.path:
    sys.path.insert(0, _FS)
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
from paths import ROOT  # noqa: E402
import snapshot as S  # noqa: E402   （契约 / 校验 / 落盘 唯一真源）
import ingest as ING  # noqa: E402   （投放 + 快照正式化 转发件）
import loader as LD  # noqa: E402    （目录派生 roots(base)）

_LEGACY_ROOT = (os.environ.get("OLD_SYSTEM_ROOT")
                or os.path.join(os.path.expanduser("~"), "炒股智能体_旧包_20261008"))  # 旧系统只读参考源；换机器时设 OLD_SYSTEM_ROOT 或放家目录

SCHEMA_VERSION = 1

# ── 源：旧系统包只读历史数据目录 ──────────────────────────────────────────────
# 源根：**旧包归档**（只读）。
#
# ★★ 2026-10-22 修正一处**过时注释**（原文误导了我，也误导过数据体检）：
#   旧注释写「本项目 data/raw/bus/ 另存一份『能力运行用』的辅助数据
#   （fundflow/筹码/竞价快照/涨停池等，供 S4/S5/S10 读取）」——
#   **这句话已不成立**。实际核对：
#     · s4.py / s5.py 的 `_OLD_ROOT` 都指向旧包，读的是旧包那份
#     · real_replay / batch_replay 的 env_history/env_labels 也走 `old_root`
#   ⇒ 本项目 `data/raw/bus/` 下那批同名文件**一个消费者都没有**，
#     且都是旧包原样副本（cmp 逐字节一致），停在各自搬到本项目那天。
#   危害：它们让 `data_health` 天天报 FRESH 告警，也让"有数据"看起来是真的。
#   处置：已全部隔离到 `data/retired/raw_bus_copies_20261022/`（117MB，9 个文件），
#        不是删除，随时可取回。
#
# 下方 `_assert_readonly_src` 明确禁止源根落在本项目 data/ 内（禁自读自写）。
SRC_ROOT = os.path.join(_LEGACY_ROOT, "data/raw")
SRC_BUS = os.path.join(SRC_ROOT, "bus")
SRC_KLINES = os.path.join(SRC_BUS, "klines.csv")
SRC_ZT = os.path.join(SRC_BUS, "zt_pool")
SRC_NOTICE = os.path.join(SRC_BUS, "notices")
SRC_ENV = os.path.join(SRC_BUS, "env_history.csv")
SRC_LHB = os.path.join(SRC_BUS, "lhb")
SOURCE_LABEL = "oldpkg:20261008/bus"

# ── 「显式声明阻塞后播种」通道（2026-10-13 用户裁定路线） ──────────────────────
# 真源 = ingest（零重定义）；声明件与 `_core.meta.json` 并列，同属投放区（data/raw/<date>/）
DECL_FNAME = ING.DECL_FNAME
# 已知缺口日（P0 B2）：**显式登记，不跳过**（跳过 = 静默把前一日池标成当日事实）。
KNOWN_GAP_DAYS = {
    "2026-09-23": "旧包归档该日量额与价格**均不可恢复**（B2 根因已定性）。按裁定表"
                  "『对该日显式登记（不是跳过）』**保留原样投放**；禁止缩放 / 插值 / 反推。"
                  "下游须按本登记把该日排除在完整性验收之外。"
                  "★用户 2026-10-13 裁定：该日**接受为可扫描日 + 显式标注**（不是排除、不是跳过）。"
                  "故 C1/C2 照常消费，但**必须**连同本标注一并读："
                  "① `turnover` 为盘中快照量额（R-1：低约 28%），**不得**用于任何量能比较或趋势判断；"
                  "② `fail_rate` 的自算源 `high`/`close` 未见异常（涨停自算数落在合理区间），"
                  "**量额缺陷不影响涨停类指标**；③ 该日候选/档位结论**不参与完整性验收**。"
                  "★★2026-10-13 实测**修正**：09-23 的 `close` **亦受污染**（非仅量额）——"
                  "拿 zt_pool 权威名单逐票核验，09-23 仅 **41/51** 判为封板（其余日 100%）。"
                  "故本轮推翻裁定表旧结论「09-23 close/high 未见异常」。",
    "2026-09-24": "**★前收污染传导日（2026-10-13 新发现）**：该日涨停价基准 = 09-23 收盘价，"
                  "而 09-23 收盘价受污染（见上条）⇒ 例 `sz000498`：09-23 close=5.12（真值≈5.14）、"
                  "09-24 close=5.65，算出 **+10.35%（主板不可能）**。"
                  "逐票核验：该日 zt_pool 权威名单 53 只中仅 **18 只**判为封板（其余日 100%）；"
                  "自洽样本退化至 **22/5224** ⇒ 触发内部不变量守卫"
                  "（触板总数 22 < 权威涨停家数 53）⇒ **`fail_rate` 判定为「不可算」并置 None**"
                  "（fail-closed：禁以退化样本冒充实测炸板率），审计登记于 `audit.fail_rate_uncomputable`。"
                  "`limit_up_count`(zt_pool 末档 53) 与 `max_board`(5) **不受影响**（不依赖前收）。"
                  "按用户 09-23 同款裁定原则「**接受为可扫描日 + 显式标注**」：该日照常消费，"
                  "但 `fail_rate` 缺失 ⇒ `regime()` 对该日**如实拒产**（不得静默降级）。"
                  "★此日新事实**须用户补裁**（是否扩展 09-23 的处置口径至 09-24、或改走补数路线）。",
}
# AS_OF 声明：只声明**已被 CALIBER 证据实测到**的那一项，其余一律留「未检查」
# （未检查 ⇒ 如实阻塞，不伪装成已声明；禁为了让写入通过而编造可过的档位）
DECLARED_AS_OF = {
    "limit_up.csv": {
        "as_of_label": "intraday_snapshot",
        "evidence_level": "E1",     # 档名内时点（zt_<date>_<HHMM>）= E1 弱证据
        "why": "CALIBER.limit_up.risk 实测：09-17~09-30 末档多为 14:57，非 15:00 收盘定格。"
               "按 `AS_OF_MIN_EVIDENCE`，intraday_snapshot 天然不满足盘后契约 ⇒ 恒阻塞；"
               "limit_up 已在 `DECLARED_BLOCKS` 登记 AS_OF ⇒ 不会因此拒写，但**原因被如实记录**"
               "（优于留成「未检查」）。",
    },
}

# 生产写入窗口（只读预检据此断言：窗口必须落在全部「可产出键」的覆盖交集内）
DEFAULT_FROM = "2026-09-16"
DEFAULT_TO = "2026-09-29"

_ZTHIST_RE = re.compile(r"^zt_(\d{4}-\d{2}-\d{2})_([0-9]{4}|hist)\.csv$")
_NOTICE_RE = re.compile(r"^notice_(\d{4}-\d{2}-\d{2})\.csv$")
_LHB_RE = re.compile(r"^lhb_(\d{4}-\d{2}-\d{2})\.md$")
_INDEX_CODE = "sh000001"                       # 指数行（旧包 klines 设计上随库携带）
_ZERO = (0.0, None, "")

# ── ★ 口径登记表：每个契约字段一行「来源列 + 状态」；非 VERIFIED 阻塞其所在键 ──────
# 状态 ENUM：VERIFIED（已按源数据机械验证，可直接转）/ UNRESOLVED（口径未决，须裁定）
CALIBER = {
    "daily_quote": {
        "status": "VERIFIED",
        "src": "bus/klines.csv",
        "note": "code/name/open/high/low/close 同名直取；pct←change_pct（已是百分数）；"
                "amount←amount(元)；vol←volume×100（手→股），★科创板 sh688*/sh689* 的 "
                "volume 本就是股（×1）——按板块分档；剔除指数行 sh000001",
        "evidence": "VWAP=amount/(vol×unit) ∈ [low,high] 逐行机械断言："
                    "2026-09-16~09-30 每日越界 ≤3 行（≤0.06%），非正量行 ≈11~13/日（停牌，转 null）",
    },
    "limit_up": {
        "status": "VERIFIED",
        "src": "bus/zt_pool/zt_<date>_<HHMM|hist>.csv（取当日最晚档）",
        "note": "boards←lbc；first_seal_time←fbt（5/6 位 HHMMSS → HH:MM:SS）；"
                "seal_amount←fund(元)；reason←hybk(行业板块)；code/name 直取",
        "evidence": "旧包 DATA-HUB 自述 zt_pool 为涨停/连板「唯一权威·直给 lbc/fund/zbc/ltsz/fbt」；"
                    "21 个交易日全部档位 fbt/fund 无空值",
        "risk": "09-17~09-30 仅盘中快照，末档多为 14:57（非 15:00 收盘定格）；仅 09-21(15:00)/"
                "09-22(15:01) 为收盘后档。14:57→15:00 间封板/开板变动无法反映。",
    },
    "news": {
        "status": "VERIFIED",
        "src": "bus/notices/notice_<date>.csv",
        "note": "news_id←art_code；ts←time；title←title；related_codes←[code]；"
                "source←'eastmoney_notice'（该文件由旧包 tools/data/notice_archive.py 自东财 "
                "np-anotice-stock 接口归档，docstring 自述）；★body 源未提供 → 显式 null；"
                "★源 code 列非合约形态（实测为 A 股**发行/审核**类公告的 IPO 发行代码，"
                "如 'szA25199'＝未上市标的）⇒ 该行 related_codes=[]（不猜不修码），公告保留、"
                "计数入报告，下游 from_news 计入 unmapped（可见而非静默）",
        "evidence": "10 个可用日 13,758 行，art_code 无重复、ts 无空值；"
                    "notice_2026-08-05.csv 有表头 0 行（空档，不播种）；"
                    "非合约 code 行合计 77/13,758（0.56%），全部为 IPO 审核/招股类公告",
        "risk": "09-30 档仅 486 行（当日 17:10 半程抓取）＝**部分日**，晚于 17:47 的公告缺失。",
    },
    "dragon_tiger": {
        "status": "UNRESOLVED",
        "src": "bus/lhb/lhb_<date>.md",
        "note": "契约须 (code,name,seat,buy,sell,net)；源为两张 markdown 表：机构榜＝按 code 聚合"
                "（instBuyAmt=机构席位净买额、netBuyAmt=全席位净买额，**无 buy/sell 拆分**、"
                "seat 为聚合概念）；活跃席位＝按 seat 聚合（buyAmt 为席位买入额，code 列可能是"
                "分号分隔多票，**无 sell/net**）。→ 不存在忠实的 1:1 映射。",
        "evidence": "11 日：机构榜 370 行；活跃席位 1,670 行（单代码 1,399 / 多代码 271）；"
                    "机构榜 tdDays ∈ {1,3}（278/92），同日同 code 存在 1 日榜与 3 日榜两行需去重；"
                    "旧包 DATA-HUB 另载「机构榜 instBuyRate 实为当日涨跌幅，非机构买入占比」"
                    "——按表头字面映射必错。",
    },
    "env_metrics": {
        "status": "VERIFIED",
        "src": "bus/zt_pool（末档）+ bus/klines.csv（全市场 + sh000001）+ bus/env_history.csv",
        "note": "**6 键全实现**（口径 = 用户 2026-10-13 裁定「自算口径+显式标注」；方案取裁定表"
                "§5.1 **甲·同源自洽优先**）："
                "limit_up_count←**zt_pool 末档行数**（甲：与 limit_up.csv 同源 ⇒ 同一快照不再出现"
                "两个涨停家数，自动消解 R-9）；max_board←max(zt_pool.lbc)；"
                "fail_rate←**klines 精确限价自算** = 触板未封 ÷ (封板 + 触板未封)，"
                "触板⇔high ≥ 涨停价、封板⇔close ≥ 涨停价，涨停价 = round(前收 × (1+板块上限), 2)；"
                "turnover←**klines 全市场 amount 求和**（剔除指数行 sh000001）；"
                "up_down_ratio←env_history.ratio_ud；index_ma20←MA20(sh000001 close)。",
        "evidence": "裁定表 §5 实证：09-29 fail_rate 精确限价 **29.5%**（旧包记录 29.7%，差 0.2pp，"
                    "为三口径中唯一逼近者；hunter 24.3% / rebuild 33.0% 各差 5.4pp / 3.3pp）；"
                    "turnover 09-29 = 1.4029 万亿（复盘 1.41 万亿，差 0.5%），只数覆盖 93.6%，"
                    "与 westock money_ratio20 偏差 −1.78~+0.89pp。★R-4：旧包「64/27」实现未留档、"
                    "**不可复现**，不得作验收基准。",
        "label": {
            "limit_up_count": "**zt_pool 末档行数**口径 —— 末档多为 **14:57 盘中快照**（非 15:00 "
                              "收盘定格）、**不含 ST**、**含北交所**；14:57→15:00 的封/开板变动不可反映。",
            "fail_rate": "**独立于 limit_up_count 的口径**（跨源唯一字段，理由：zt_pool 的 zbc 是"
                         "「回封次数」≠「炸板未封」）；**日线粒度**，无法识别触板次数与回封；"
                         "**不含北交所**（klines 该窗口覆盖 0）；无前收（新股/停牌）与除权行**不参与自算**。",
            "turnover": "**klines 求和口径 · 缺北交所 · 恒定低估 ~1%**；非两市真实成交额权威值；"
                        "**只记录、不参与分档**（升格须走 C6 需求发现验证，禁拍「成交额萎缩=退潮」）。",
            "up_down_ratio": "**westock 原字段直取**，内部定义（时点 / 含否 ST·北交所）未知；只记录。",
            "index_ma20": "MA20(sh000001 close)，含当日、前 19 日；只记录。",
            "max_board": "**zt_pool 末档 max(lbc)**；末档非收盘定格（同 limit_up_count）。",
        },
        "residual": "R-2 klines 北交所覆盖 0（该窗口）· R-5 westock zt/ratio_ud 内部定义未知（已改用 "
                    "zt_pool，不再取 zt）· R-6 zt_pool 历史仅 21 个交易日 · R-7 fail_rate 跨源"
                    "（快照元数据 + regime 报告显式标注）· R-3 change_pct 与自算 32/5225 不一致"
                    "（除权行，已由本件 mechanically 剔除并计数）。",
        "risk": "★裁定表 §5.2 实证 1：fail_rate 口径**直接决定档位**（09-29 精确限价 29.5%⇒强势档"
                "position_cap=5；rebuild 33.0%⇒震荡档 cap=3）。本件取精确限价（唯一逼近旧记录者）并"
                "**逐日明示该标注**——档位结论须连同标注一并读，禁剥离口径单独引用。",
    },
}

# 契约键 → 文件（真源 = snapshot.CONTRACT，零重定义）
_FNAME_OF = {spec["key"]: f for f, spec in S.CONTRACT.items()}

# 涨停/自算口径参数（仅用于 --check 的只读复算与报告，不进入任何生产写入路径）
_BOARD_LIMIT = (("bj", 0.30), ("sh688", 0.20), ("sh689", 0.20),
                ("sz300", 0.20), ("sz301", 0.20))


class SeedError(Exception):
    """播种被拒（源缺失 / 口径未决 / 冲突）——一律显式抛错，不静默降级。"""


# ── 源侧只读工具 ─────────────────────────────────────────────────────────────
def _assert_readonly():
    """入口纪律断言：源根存在、是目录、且**不在本项目 data/ 内**（防自读自写）。"""
    if not os.path.isdir(SRC_BUS):
        raise SeedError("源数据目录不存在：%s" % SRC_BUS)
    proj_data = os.path.normcase(os.path.join(ROOT, "data"))
    if os.path.normcase(os.path.abspath(SRC_ROOT)).startswith(proj_data):
        raise SeedError("源根落在本项目 data/ 内（禁自读自写）：%s" % SRC_ROOT)


def _rd(path):
    """源文件只读打开（UTF-8-BOM 容错）——全模块唯一的源读取入口。"""
    return io.open(path, encoding="utf-8-sig", newline="")


def sha256_file(path, limit=None):
    """流式 sha256（≤limit 字节；None = 全文件）。只读。"""
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            n += len(b)
            if limit is not None and n >= limit:
                break
    return h.hexdigest()


def _rows(path):
    with _rd(path) as f:
        return list(csv.DictReader(f))


# ── 源清单 ───────────────────────────────────────────────────────────────────
def zt_snapshots():
    """→ {date: [(tag, filename), ...]}（升序）；tag ∈ HHMM | 'hist'。"""
    out = {}
    if not os.path.isdir(SRC_ZT):
        return out
    for n in sorted(os.listdir(SRC_ZT)):
        m = _ZTHIST_RE.match(n)
        if m:
            out.setdefault(m.group(1), []).append((m.group(2), n))
    for d in out:
        out[d].sort()
    return out


def zt_final(date):
    """当日最晚档文件路径；无 → None。'hist' 视为收盘档（回补通道），排在 HHMM 之前。"""
    snaps = zt_snapshots().get(date)
    if not snaps:
        return None
    return os.path.join(SRC_ZT, snaps[-1][1])


def notice_dates():
    out = {}
    if not os.path.isdir(SRC_NOTICE):
        return out
    for n in sorted(os.listdir(SRC_NOTICE)):
        m = _NOTICE_RE.match(n)
        if m:
            out[m.group(1)] = os.path.join(SRC_NOTICE, n)
    return out


def lhb_dates():
    out = {}
    if not os.path.isdir(SRC_LHB):
        return out
    for n in sorted(os.listdir(SRC_LHB)):
        m = _LHB_RE.match(n)
        if m:
            out[m.group(1)] = os.path.join(SRC_LHB, n)
    return out


def env_history():
    """→ {date: row}。"""
    if not os.path.isfile(SRC_ENV):
        return {}
    return {r["date"]: r for r in _rows(SRC_ENV) if r.get("date")}


# ── 键 1 · daily_quote ← klines.csv ─────────────────────────────────────────
def _vol_unit(code):
    """成交量单位换算因子（手→股）＝100；科创板本就是股。"""
    c = code.lower()
    for pre, _lim in _BOARD_LIMIT:
        if pre in ("sh688", "sh689") and c.startswith(pre):
            return 1.0
    return 100.0


def board_limit(code, name):
    """板块涨跌幅上限（旧包 DATA-HUB 口径：全市场含 bj+ST，按板块上限逐票判）。"""
    c = code.lower()
    for pre, lim in _BOARD_LIMIT:
        if c.startswith(pre):
            return lim
    nm = (name or "").upper()
    return 0.05 if ("ST" in nm) else 0.10


def _fnum(v):
    s = "" if v is None else str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        raise SeedError("数值列无法解析：%r" % (v,))


def daily_quote_rows(dates):
    """一次性流式读 klines.csv → {date: [契约行]}（仅合成 dates 内）。

    行保留原字段以保证 VWAP 断言可复算；契约行同时产出。
    """
    want = set(dates)
    out = {d: [] for d in dates}
    audit = {"rows": 0, "vwap_bad": 0, "nonpos": 0, "index_skipped": 0, "no_pct": 0}
    with _rd(SRC_KLINES) as f:
        for r in csv.DictReader(f):
            d = (r.get("date") or "").strip()
            if d not in want:
                continue
            audit["rows"] += 1
            code = (r.get("code") or "").strip().lower()
            if code == _INDEX_CODE:
                audit["index_skipped"] += 1
                continue
            name = r.get("name") or ""
            vol_raw = _fnum(r.get("volume"))
            amount = _fnum(r.get("amount"))
            lo, hi = _fnum(r.get("low")), _fnum(r.get("high"))
            unit = _vol_unit(code)
            vol = None
            if vol_raw is not None and vol_raw > 0:
                vol = vol_raw * unit
            else:
                audit["nonpos"] += 1
            if amount is not None and amount <= 0:
                amount = None
                audit["nonpos"] += 1
            if vol and amount and lo and hi:
                vwap = amount / vol
                if not (lo - 1e-6 <= vwap <= hi + 1e-6):
                    audit["vwap_bad"] += 1
            pct = _fnum(r.get("change_pct"))
            if pct is None:
                audit["no_pct"] += 1
            out[d].append({"code": code, "name": name,
                           "open": _fnum(r.get("open")), "high": hi, "low": lo,
                           "close": _fnum(r.get("close")), "pct": pct,
                           "vol": vol, "amount": amount})
    return out, audit


# ── 键 2 · limit_up ← zt_pool ───────────────────────────────────────────────
def _fbt_to_time(v):
    """封板时间 5/6 位整数串（92500 / 93006）→ 'HH:MM:SS'；空 → None。"""
    s = "" if v is None else str(v).strip()
    if not s:
        return None
    if not s.isdigit() or len(s) > 6:
        raise SeedError("fbt 形态非法：%r" % (v,))
    s = s.zfill(6)
    hh, mm, ss = s[:2], s[2:4], s[4:]
    if int(hh) > 23 or int(mm) > 59 or int(ss) > 59:
        raise SeedError("fbt 时间越界：%r" % (v,))
    return "%s:%s:%s" % (hh, mm, ss)


def limit_up_rows(date):
    """→ (rows, source_file, snap_tag)；无源 → (None, None, None)。

    ★ 只取当日**最晚档**（hist 优先视为收盘档；盘中档 = 该日可得的最晚时点）。
    """
    p = zt_final(date)
    if not p:
        return None, None, None
    tag = os.path.basename(p).rsplit("_", 1)[1][:-4]
    rows, seen = [], set()
    for r in _rows(p):
        code = (r.get("code") or "").strip().lower()
        if not code or code in seen:
            continue
        seen.add(code)
        rows.append({"code": code, "name": r.get("name") or "",
                     "boards": int(float(r["lbc"])),
                     "first_seal_time": _fbt_to_time(r.get("fbt")),
                     "seal_amount": _fnum(r.get("fund")),
                     "reason": (r.get("hybk") or "").strip() or None})
    return rows, p, tag


# ── 键 4 · news ← notices ──────────────────────────────────────────────────
def news_rows(date):
    """→ (rows, source_file, audit)；无源/空档 → (None, None, None)。

    ★ 源 code 列非合约形态（已实测为 A 股**发行/审核类**公告的 IPO 发行代码，如 'szA25199'
    ＝未上市标的，无 6 位证券代码）⇒ 该行 `related_codes=[]`（不猜、不修码），
    公告本体照常保留；计数入 audit 并在报告呈现（下游 `pool_match.from_news` 会把它计入
    unmapped，可见而非静默）。
    """
    p = notice_dates().get(date)
    if not p:
        return None, None, None
    rows, seen = [], set()
    audit = {"rows": 0, "unmapped_code": 0, "dup_id": 0, "empty_ts": 0}
    for r in _rows(p):
        audit["rows"] += 1
        aid = (r.get("art_code") or "").strip()
        code = (r.get("code") or "").strip().lower()
        if not aid:
            continue
        if aid in seen:
            audit["dup_id"] += 1
            continue
        seen.add(aid)
        ok = bool(code) and bool(S._CODES_RE.match(code))
        if code and not ok:
            audit["unmapped_code"] += 1
        ts = (r.get("time") or "").strip()
        if not ts:
            audit["empty_ts"] += 1
        rows.append({"news_id": aid, "ts": ts, "source": "eastmoney_notice",
                     "title": (r.get("title") or "").strip() or None,
                     "body": None, "related_codes": [code] if ok else []})
    return rows, p, audit


# ── 键 5 · env_metrics（6 键全实现 · 裁定表 §5 方案甲；含机械剔除与逐日审计） ──
def index_close_series():
    """sh000001 全行（含 name 为空的历史段）→ {date: close}。只读一次。"""
    ser = {}
    with _rd(SRC_KLINES) as f:
        for r in csv.DictReader(f):
            if r.get("code") == _INDEX_CODE:
                c = _fnum(r.get("close"))
                if c:
                    ser[(r.get("date") or "").strip()] = c
    return ser


def market_day_rows(dates):
    """一次性流式读 klines.csv → ({date: {code: {name,high,close,prev,amount}}}, audit)。

    ★ 逐 code 按日期**升序**滚动 `last` ⇒ 同一遍扫描即得**前收**（涨停价必需），
      无需二次读盘、无需全量驻留。内存有界：只留目标日的行 + 每 code 最近一根收盘。

    剔除指数行 `sh000001`（turnover 不得含指数）。
    """
    want = set(dates)
    last = {}
    out = {d: {} for d in dates}
    audit = {"rows": 0, "index_skipped": 0, "bj_rows": 0, "no_name": 0, "no_prev": 0,
             "no_amount": 0}
    with _rd(SRC_KLINES) as f:
        for r in csv.DictReader(f):
            code = (r.get("code") or "").strip()
            d = (r.get("date") or "").strip()
            if not code or not d:
                continue
            cl = _fnum(r.get("close"))
            if code == _INDEX_CODE:
                audit["index_skipped"] += 1
                if cl:
                    last[code] = cl
                continue
            if d in want:
                amt = _fnum(r.get("amount"))
                if not amt:
                    audit["no_amount"] += 1
                nm = (r.get("name") or "").strip()
                if not nm:
                    audit["no_name"] += 1
                if code.lower().startswith("bj"):
                    audit["bj_rows"] += 1
                out[d][code] = {"name": nm, "high": _fnum(r.get("high")), "close": cl,
                                "low": _fnum(r.get("low")),
                                "prev": last.get(code), "amount": amt}
                audit["rows"] += 1
            if cl:
                last[code] = cl
    for d in dates:
        audit["no_prev"] += sum(1 for v in out[d].values() if not v.get("prev"))
    return out, audit


_ENV_KEYS = tuple(c for c, _ in S.CONTRACT["env_metrics.json"]["cols"])


def limit_prices_cents(prev, lim):
    """→ `(涨停价_分, 跌停价_分)`，**四舍五入（half-up）到分** —— 与交易所口径一致。

    ⚠ **禁用内置 `round()`**：Python 是**银行家舍入**（half-to-even），
      `prev=3.75` 时 `round(4.125, 2) = 4.12`，而交易所为 **4.13**
      （实测 09-29 `sz000607`：`close=4.13` 恰为涨停价，却被误判 `EXDIV`）。
      前收末位为 5 时即触发 ⇒ 约 10% 的涨停票受影响。
    ★ 全走**整数分**运算，避免浮点误差（`int(x*100+0.5)` 对 4.125 亦会失败）。
    """
    pct = int(round(lim * 100))                  # 5 / 10 / 20 / 30
    c = int(round(prev * 100))                   # 前收（分）
    return (c * (100 + pct) + 50) // 100, (c * (100 - pct) + 50) // 100


def classify_limit(pv, hi, lo, cl, lim):
    """单票触板判定（纯函数）→ `'NOPREV' | 'EXDIV' | 'NONE' | 'SEALED' | 'BROKEN'`。

    ★ **除权/坏数据判据 = 价格越出涨跌停带**（`hi > 涨停价` 或 `lo < 跌停价`），
      **不是**裸涨幅比 —— 涨停价须四舍五入到分，故 `cl/pv − 1` 会**合法地**略超板块上限
      （实测 09-29 sh603200：prev=36.97 → lp=40.67，比值 0.10008 > 0.10）。
      用比值判除权会把真涨停误剔（本件首版缺陷；V20e 已固化为防回归断言）。
    ★ 比较全部在**整数分**域进行（含 half-up 舍入），杜绝浮点等值误判。
    """
    if not pv or pv <= 0 or hi is None or cl is None:
        return "NOPREV"
    up, dn = limit_prices_cents(pv, lim)
    hic, clc = int(round(hi * 100)), int(round(cl * 100))
    loc = int(round(lo * 100)) if lo is not None else None
    if hic > up or (loc is not None and loc < dn):
        return "EXDIV"
    if hic < up:
        return "NONE"
    return "SEALED" if clc >= up else "BROKEN"


def env_metrics_rows(dates):
    """→ ({date: {field: value}}, audit)。**6 键全产出**（口径 = 裁定表 §5 方案甲）。

      · `limit_up_count` ← **zt_pool 末档行数**（甲：与 `limit_up.csv` 同源）
      · `max_board`      ← `max(zt_pool.lbc)`
      · `fail_rate`      ← klines 精确限价自算：触板未封 ÷ (封板 + 触板未封)
      · `turnover`       ← klines 全市场 amount 求和（剔除指数行）
      · `up_down_ratio`  ← `env_history.ratio_ud`；**该源缺席日**（旧包 env_history
        止于 09-29）⇒ klines 同源自算（涨家数÷跌家数，登记 `audit.ud_derived`，
        与 fail_rate 同款显式标注先例；实测偏差 −5.2%~+7.6%，宇宙差已知）
      · `index_ma20`     ← MA20(sh000001 close)

     ★ fail_rate 的**机械剔除**（fail-closed，不猜）：无前收（新股首日 / 停牌）与
      **除权行**（价格**越出涨跌停带**：`high > 涨停价` 或 `low < 跌停价`，即无复权前收
      ⇒ 涨停价不可算）**不参与自算**，仅计数登记 —— 否则会把除权当「炸板」污染口径。
      ⚠ 判据**必须**用价格带而非裸涨幅比：涨停价需**四舍五入到分**，故 `close/prev-1`
      会合法地略超板块上限（实测 sh603200 prev=36.97→lp=40.67，比值 0.10008 > 0.10），
      用比值判除权会把 31 只真涨停误剔（本件首版缺陷，V20e 已固化防回归）。
    """
    hist = env_history()
    zts = zt_snapshots()
    idx = index_close_series()
    iseq = sorted(idx)
    mkt, maudit = market_day_rows(dates)
    out, audit = {}, {"ma20_missing": [], "hist_missing": [], "zt_missing": [],
                      "zt_rows": {}, "fail_counts": {}, "exdiv_rows": {}, "no_prev_rows": {},
                      "turnover_universe": {}, "klines": maudit,
                      "ud_derived": {}, "ud_uncomputable": []}
    for d in dates:
        row = {k: None for k in _ENV_KEYS}
        h = hist.get(d)
        if h:
            row["up_down_ratio"] = _fnum(h.get("ratio_ud"))
        else:
            audit["hist_missing"].append(d)
            # 2026-10-19 补位通道：env_history 缺席日（旧包两个 env_history 均止于
            # 2026-09-29）⇒ 由当日 klines 同源自算（上涨家数 ÷ 下跌家数）——与
            # fail_rate 同款「自算口径 + 显式标注」先例（用户 2026-10-13 裁定表 §5）。
            # 实测对账（12 个官方在场日）：派生值与官方偏差 −5.2% ~ +7.6%
            # （宇宙差：klines 无北交所行）⇒ 同 caliber 近似，非等价。
            # 派生值逐日登记 audit.ud_derived，下游须随附读取，禁剥离标注单独引用。
            mrows = mkt.get(d) or {}
            up = sum(1 for v in mrows.values()
                     if v.get("close") and v.get("prev") and v["close"] > v["prev"])
            dn = sum(1 for v in mrows.values()
                     if v.get("close") and v.get("prev") and v["close"] < v["prev"])
            if dn > 0:
                row["up_down_ratio"] = round(up / dn, 4)
                audit["ud_derived"][d] = {"up": up, "down": dn,
                                          "value": row["up_down_ratio"],
                                          "caliber": "klines_derived"}
            else:
                audit["ud_uncomputable"].append(d)
        if d in zts:
            rows = _rows(zt_final(d))
            boards = []
            for r in rows:
                try:
                    boards.append(int(float(r["lbc"])))
                except (KeyError, TypeError, ValueError):
                    pass
            row["limit_up_count"] = len(rows)              # 方案甲：末档行数（非 env_history.zt）
            row["max_board"] = max(boards) if boards else None
            audit["zt_rows"][d] = len(rows)
        else:
            audit["zt_missing"].append(d)
        if d in idx:
            i = iseq.index(d)
            win = iseq[max(0, i - 19):i + 1]
            if len(win) == 20:
                row["index_ma20"] = sum(idx[x] for x in win) / 20.0
        if row["index_ma20"] is None:
            audit["ma20_missing"].append(d)
        # ── turnover / fail_rate ← klines 自算 ────────────────────────────────
        rows = mkt.get(d) or {}
        tot, sealed, broken, exdiv, noprev = 0.0, 0, 0, 0, 0
        for code, v in rows.items():
            amt = v.get("amount")
            if amt and amt > 0:
                tot += amt
            cls = classify_limit(v.get("prev"), v.get("high"), v.get("low"), v.get("close"),
                                 board_limit(code, v.get("name")))
            if cls == "NOPREV":
                noprev += 1
            elif cls == "EXDIV":
                exdiv += 1
            elif cls == "SEALED":
                sealed += 1
            elif cls == "BROKEN":
                broken += 1
        row["turnover"] = tot if tot > 0 else None
        # ── ★ 内部不变量守卫：触板总数（封板+炸板）必须 ≥ 当日权威涨停家数 ──
        #    每只涨停票按定义都是「触板」票 ⇒ sealed+broken ≥ limit_up_count 恒成立。
        #    违反 ⇒ 前收基准被污染（如 2026-09-24 的前收 = 已污染的 09-23 收盘）
        #    ⇒ 样本退化（09-24 仅 22/5224 参与）⇒ **fail_rate 不可算**，fail-closed 置 None。
        ztn = row["limit_up_count"]
        den = sealed + broken
        if ztn and den < ztn:
            row["fail_rate"] = None
            audit.setdefault("fail_rate_uncomputable", {})[d] = (
                "触板总数 %d < 权威涨停家数 %d（内部不变量违反）⇒ 前收基准受污染、"
                "自洽样本退化（本日仅 %d/%d 只参与）⇒ 炸板率不可算，禁以退化样本冒充实测值"
                % (den, ztn, den, len(rows)))
        else:
            row["fail_rate"] = (broken / den) if den else None
        audit["fail_counts"][d] = {"sealed": sealed, "broken": broken, "den": den}
        audit["exdiv_rows"][d] = exdiv
        audit["no_prev_rows"][d] = noprev
        audit["turnover_universe"][d] = {"amount_sum_yuan": tot, "n_codes": len(rows)}
        out[d] = row
    return out, audit


# ── 口径闸：未决字段所在键一律不得写入 ───────────────────────────────────────
def blocked_keys(dates=None):
    """→ 未决键清单。传 dates 时**按日期**判 news（2026-10-19 修）：

    news 的源（notices）是**按日归档**——`CALIBER.status=VERIFIED` 只说明「有转换器且口径已定」，
    不说明「该日有归档」。旧实现只看 CALIBER ⇒ 9 月上旬（公告归档自 9/16 起）无法声明空、
    promote 又因 CORE_INCOMPLETE 拒收，形成死结。现：窗口内任一日期无公告归档 ⇒ news 进未决集，
    由声明通道显式登记为空（口径不伪造，只是承认该日无源）。
    """
    bl = {k for k, v in CALIBER.items() if v["status"] != "VERIFIED"}
    if dates:
        nd = notice_dates()
        lack = [d for d in dates if d not in nd]
        if lack:
            bl.add("news")
    return sorted(bl)


def _assert_calibers(key):
    v = CALIBER.get(key)
    if not v or v["status"] != "VERIFIED":
        raise SeedError("键 %s 口径未决（CALIBER.status=%s）——拒绝写入；须先裁定来源列与口径"
                        % (key, (v or {}).get("status", "缺失")))


# ── 转换入口（只产出可产出键；未决键显式缺席） ────────────────────────────────
def _check_declaration(declare, dates=None):
    """机械核验：声明集合必须**逐项等于**未决键集合（防漏声明 / 防多声明 / 防静默播种）。

    `dates` 传入时按日期判 news 可产性（见 `blocked_keys`）。"""
    bl, dv = set(blocked_keys(dates)), set(declare or ())
    if dv != bl:
        miss, extra = sorted(bl - dv), sorted(dv - bl)
        raise SeedError(
            "声明与未决键不一致 —— 拒绝写入。未决键=%s / 已声明=%s%s%s"
            % (sorted(bl), sorted(dv),
               ("；**漏声明**（不得静默播种未声明的未决键）：%s" % miss) if miss else "",
               ("；**多声明**（声明了非未决键，属伪造口径）：%s" % extra) if extra else ""))
    return sorted(dv)


def declaration_artifact(dates, declare):
    """→ 「显式声明阻塞后播种」声明件（落 `_core.decl.json` 同目录）。

    只声明**为什么空**与**谁授权**，**不承载任何数据值**；并随 `promote_snapshot`
    进 A9 判决 sidecar（`provenance` 字段）—— 声明不得在正式化环节被丢掉。
    """
    return {
        "schema": 1,
        "kind": "EXPLICIT_DECLARATION_SEED",
        "authorized_by": "用户 2026-10-13 裁定：P0 口径未决路线 = 「显式声明阻塞后播种」",
        "declared_unresolved_keys": sorted(declare),
        "key_declarations": {
            k: {"status": CALIBER[k]["status"], "src": CALIBER[k]["src"],
                "note": CALIBER[k]["note"], "risk": CALIBER[k].get("risk", ""),
                "empty_form": ("0 行（源存在但无忠实 1:1 映射）" if k == "dragon_tiger"
                               else "未登记形态（CALIBER 新增未决键时须同步本映射）")}
            for k in sorted(declare)},
        "known_gap_days": {d: KNOWN_GAP_DAYS[d] for d in sorted(dates) if d in KNOWN_GAP_DAYS},
        "provenance": {k: {"as_of_label": v["as_of_label"],
                           "evidence_level": v["evidence_level"]}
                       for k, v in DECLARED_AS_OF.items()},
        "as_of_undeclared": "除上列以外，本批**未对 AS_OF / COVERAGE 作出声明** ⇒ A9 判决记"
                            "「未检查」（未判定 ≠ 通过），不得据此认定时点已合格。",
        "verdict_authority": "逐日**四类判决的唯一权威 = 快照目录内 `_admission.json`**"
                             "（A9 层①/② 写入并由层②复算校验）。本件只承载「为什么空」与"
                             "「谁授权」，**不复制判决**（禁第二份真源）。"
                             "消费前断言走 `snapshot.assert_admissible(date, need=...)`。",
        "rule": "空值是**声明出来的**，不是省略出来的。消费方禁把空读成「当日无此事」；"
                "须以 A9 判决 sidecar / 本件为准（`assert_admissible(date, need=...)`）。",
    }


def build(dates, declare=()):
    """→ (payload_by_date, sidecar)。

    `declare` = **已显式声明**的未决键（调用方须先用 `_check_declaration` 机械核验）。
    已声明键以「**显式空值**」投放 —— 空值是声明出来的，不是省略出来的：
      · `dragon_tiger` → **0 行**（契约有 code/name/seat/buy/sell/net，源为两张异维度聚合表，
        无忠实 1:1 映射 ⇒ 不猜不拼；空行**不等于**"该日无龙虎榜"，故须由声明件区分）

    ⚠ 未声明的未决键**一律不投**（缺席 ≠ 空），由 loaders 如实 UNAVAILABLE。
    ⚠ `env_metrics` 自 2026-10-13 裁定后**已 VERIFIED**（6 键全产出，不再走声明通道）。
    """
    dq, dq_audit = daily_quote_rows(dates)
    env, env_audit = env_metrics_rows(dates)
    decl_keys = set(declare or ())
    payload, side = {}, {"daily_quote": dq_audit, "env_metrics": env_audit, "per_date": {}}
    for d in dates:
        p, s = {}, {}
        if CALIBER["daily_quote"]["status"] == "VERIFIED":
            p["daily_quote"] = dq[d]
            s["daily_quote_n"] = len(dq[d])
        if CALIBER["limit_up"]["status"] == "VERIFIED":
            lu, path, tag = limit_up_rows(d)
            if lu is not None:
                p["limit_up"] = lu
                s["limit_up_n"] = len(lu)
                s["limit_up_src"] = os.path.basename(path)
                s["limit_up_tag"] = tag
        if CALIBER["news"]["status"] == "VERIFIED":
            nw, path, n_audit = news_rows(d)
            if nw is not None:
                p["news"] = nw
                s["news_n"] = len(nw)
                s["news_src"] = os.path.basename(path) if path else None
                s["news_unmapped_code"] = n_audit["unmapped_code"]
            elif "news" in decl_keys:
                # 该日无公告归档且已声明 ⇒ 显式空（0 行），由声明件区分「空」与「无源」
                p["news"] = []
                s["news_n"] = 0
                s["news_src"] = None
                s["news_declared_empty"] = True
        if CALIBER["dragon_tiger"]["status"] == "VERIFIED":
            raise SeedError("dragon_tiger 口径已标记 VERIFIED 但本件无对应转换器（禁止占位实现）")
        if "dragon_tiger" in decl_keys:
            p["dragon_tiger"] = []                    # 显式空：0 行，由声明件区分「空」与「无」
            s["dragon_tiger_declared_empty"] = True
        if CALIBER["env_metrics"]["status"] == "VERIFIED":
            p["env_metrics"] = env[d]                 # 6 键全产出（裁定表 §5 方案甲）
            s["env_metrics_n"] = len(env[d])
            s["env_metrics_null"] = sorted(k for k, v in env[d].items() if v is None)
        elif "env_metrics" in decl_keys:
            p["env_metrics"] = env[d]
            s["env_metrics_declared_null"] = sorted(k for k, v in env[d].items() if v is None)
        s["env_metrics_partial"] = env[d]
        payload[d], side["per_date"][d] = p, s
    return payload, side


def _validate(payload_by_date, declare=(), side=None):
    """写盘前契约校验（真源 = snapshot._normalize；纯函数，不碰盘）→ 问题清单。

    另核验「显式声明」的形态：已声明键必须**在场且确为显式空**（禁以"缺席"冒充"声明为空"）。
    ★ `side` = `build()` 的审计段 —— env_metrics 的「不可算」登记据此核验：
      只允许**有审计留痕**的 `fail_rate=None`（前收污染致样本退化），禁静默空值。
    """
    probs = []
    decl = set(declare or ())
    side_audit = ((side or {}).get("env_metrics") or {})
    for d, p in payload_by_date.items():
        if not p:
            probs.append("%s: 无任何可产出键" % d)
            continue
        for k, rows in p.items():
            if k == "daily_quote" and not rows:
                probs.append("%s: daily_quote 为空" % d)
        for k in sorted(decl):
            if k not in p:
                probs.append("%s: 已声明键 %s 缺席（'缺席'≠'声明为空'，禁混淆）" % (d, k))
        if "dragon_tiger" in decl and p.get("dragon_tiger") not in ([], None):
            probs.append("%s: 已声明 dragon_tiger 必须为 0 行显式空，实得 %d 行（禁拼凑假映射）"
                         % (d, len(p.get("dragon_tiger") or [])))
        if "env_metrics" in decl:
            em = p.get("env_metrics") or {}
            nulls = sorted(k for k, v in em.items() if v is None)
            if nulls != ["fail_rate", "turnover"]:
                probs.append("%s: 已声明 env_metrics 的显式 null 应为 "
                             "['fail_rate','turnover']，实得 %s" % (d, nulls))
        # env_metrics 自裁定后已 VERIFIED：6 键必须为真值 —— 但**经审计登记**的
        # 「不可算」日（前收污染致样本退化）允许 fail_rate 为 None，且须在审计留痕。
        if CALIBER["env_metrics"]["status"] == "VERIFIED" and "env_metrics" in p:
            em = p.get("env_metrics") or {}
            nulls = sorted(k for k, v in em.items() if v is None)
            uncalc = (side_audit.get("fail_rate_uncomputable") or {})
            ok_null = uncalc.get(d)
            if nulls and not (nulls == ["fail_rate"] and ok_null):
                probs.append("%s: env_metrics 出现 null %s 但**无审计登记**（禁静默空值；"
                             "若源确缺须显式登记）" % (d, nulls))
            if set(em) != set(_ENV_KEYS):
                probs.append("%s: env_metrics 键集 ≠ 契约 %s，实得 %s"
                             % (d, sorted(_ENV_KEYS), sorted(em)))
        # 用完整 5 键骨架跑 _normalize，仅校验**已产出键**的列结构
        try:
            _normalize_partial(p, d)
        except S.SnapshotError as e:
            probs.append("%s: %s" % (d, e))
    return probs


def _normalize_partial(payload, date):
    """仅校验已产出键的列结构（不伪造缺失键）——复用 snapshot 的列规格与归一化器。"""
    for key, rows in payload.items():
        fname = _FNAME_OF[key]
        spec = S.CONTRACT[fname]
        cols = [c for c, _ in spec["cols"]]
        if spec["fmt"] == "json":
            if set(rows.keys()) != set(cols):
                raise S.SnapshotError("%s 键集不符契约：期望 %s，实际 %s"
                                      % (fname, cols, sorted(rows.keys())))
            for c, k in spec["cols"]:
                S._NORM[k](rows.get(c), "%s.%s" % (fname, c))
            continue
        if not isinstance(rows, list):
            raise S.SnapshotError("%s 必须是记录列表" % fname)
        for i, rec in enumerate(rows):
            if set(rec.keys()) != set(cols):
                raise S.SnapshotError("%s 第 %d 行字段集不符契约：期望 %s，实际 %s"
                                      % (fname, i, cols, sorted(rec.keys())))
            for c, k in spec["cols"]:
                S._NORM[k](rec.get(c), "%s[%d].%s" % (fname, i, c))


# ── 待写清单（--plan）：用**真实写入器**在临时 base 算字节与哈希 ────────────
def manifest(payload_by_date, base=None):
    """→ [ {path, rows, bytes, sha256} ]；只写临时目录，绝不碰生产目标。

    ★ 2026-10-19 修：原实现用 `snapshot._dump` 算预览哈希，但 `--stage` 实际
    经 `ingest.stage_core` 落盘（CRLF 行尾 + compact JSON）⇒ 计划哈希与实写
    字节系统性不符（daily_quote 每行 +1 B、env JSON 差缩进）——计划自称「实测
    字节」却不实测。现改为**同一写入器**（stage_core → 临时 base），只对 5 个
    契约文件取哈希（_core.meta.json 含时间戳、非确定性，不入清单）。
    """
    tmp = tempfile.mkdtemp(prefix="seed_manifest_")
    out = []
    try:
        for d in sorted(payload_by_date):
            ING.stage_core(d, payload_by_date[d], source=SOURCE_LABEL,
                           base=tmp, data_upto=d)
            r_root = LD.roots(tmp)["raw"]
            for key in sorted(payload_by_date[d]):
                fname = _FNAME_OF[key]
                p = os.path.join(r_root, d, fname)
                st = os.stat(p)
                h = hashlib.sha256(io.open(p, "rb").read()).hexdigest()[:16]
                rows = payload_by_date[d][key]
                out.append({"date": d, "key": key,
                            "raw_path": os.path.join("data", "raw", d, fname)
                            .replace("\\", "/"),
                            "rows": len(rows) if isinstance(rows, list) else 1,
                            "bytes": st.st_size,
                            "sha256": h})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


# ── 写入路径（--stage / --promote）；未决口径 ⇒ 拒绝 ─────────────────────────
def do_stage(dates, execute=False, declare=()):
    """经 ingest.stage_core 写 data/raw/<date>/。声明不齐 / 无 --execute ⇒ 拒绝。

    ★ 不改判「未决键」这一事实：只把「一律拒绝」换成「**声明集合须逐项等于未决键集合**」。
      未声明 ⇒ 仍拒绝；声明齐 ⇒ 放行，但未决键以**显式空**投放 + 落声明件。
    """
    declared = _check_declaration(declare, dates)
    if not execute:
        raise SeedError("拒绝写入：缺少 --execute（本件默认只读）")
    _assert_readonly()
    payload, side = build(dates, declared)
    probs = _validate(payload, declared, side)
    if probs:
        raise SeedError("契约校验未通过，零写入：\n  " + "\n  ".join(probs[:10]))
    r_root = LD.roots()["raw"]
    # ★ 冲突判据 = **日期目录**（真源 = loader._raw_dates），非裸 isdir ——
    #   否则 `data/raw/meta/`（交易日历只读落点，非投放）会被误判为"已有投放"而拒播。
    existing = LD._raw_dates()
    if existing:
        raise SeedError("目标投放区已有投放日期目录 %s —— 冲突即停止，不覆盖；请人工裁定"
                        % existing[:5])
    decl = declaration_artifact(dates, declared)
    done = []
    for d in sorted(payload):
        r = ING.stage_core(d, payload[d], source=SOURCE_LABEL, data_upto=d, declaration=decl)
        done.append((d, r["staged"], r["warnings"]))
    return done, side


def do_promote(dates, execute=False, declare=()):
    """经 ingest.promote_snapshot 写 data/snapshot/<date>/（核心键不齐 ⇒ 该件自身拒绝）。

    声明件里登记的 `provenance` 由 `promote_snapshot` 自动继承进 A9 判决 sidecar。
    """
    _check_declaration(declare, dates)
    if not execute:
        raise SeedError("拒绝写入：缺少 --execute（本件默认只读）")
    out = []
    for d in sorted(dates):
        out.append((d, ING.promote_snapshot(d)))
    return out


RESEED_ROOT = os.path.join(ROOT, "data", "state", "reseeds")


def _already_replaced(dates, raw_root):
    """→ 该投放区中**已处于 `replace=true`** 的日期清单（防重入闸判据；纯只读）。

    ★ 2026-10-13 事故：`--reseed` 被连续执行两次，第二次的备份捕获到的是第一次
      replace **之后**的状态 ⇒ `changelog` 的 `before` 失真（45 个数据文件的变更被漏记）。
      故二次重播种必须显式 `--allow-rerun`。
    """
    seen = []
    for d in sorted(dates):
        mp = os.path.join(raw_root, d, "_core.meta.json")
        if os.path.isfile(mp):
            try:
                if (json.load(io.open(mp, encoding="utf-8")) or {}).get("replace"):
                    seen.append(d)
            except Exception:
                pass
    return seen


def _mk_meta(rdir, replace):
    """自检助手：在给定投放区写一份最小的 `_core.meta.json`（合成，不碰生产）。"""
    os.makedirs(rdir, exist_ok=True)
    with io.open(os.path.join(rdir, "_core.meta.json"), "w", encoding="utf-8",
                 newline="\n") as f:
        f.write(json.dumps({"schema": 1, "replace": bool(replace)}, sort_keys=True))
    return os.path.join(rdir, "_core.meta.json")


def do_reseed(dates, execute=False, declare=(), reason=None, allow_rerun=False):
    """**口径/数据修正后的重播种**：`replace` 重投放 + `force` 重正式化。

    走的是 `ingest` 设计内的补数通道（`stage_core(replace=True)` = 覆盖 + `meta.replace=true`
    留痕；`write_snapshot(force=True)` = 「仅用于数据修正后的重跑」，旧目录先改名再换入、
    中途失败自动回滚）。**本函数只暴露该通道，不新造覆盖语义。**

    ★ 四重闸：
      ① 须 `--execute`（默认只读）；
      ② 须 `--reason "<非空说明>"` —— 覆盖生产即须留痕，禁静默改写；
      ③ **防重入**（2026-10-13 事故后新增）：目标 `_core.meta.json` 若已 `replace=true`
         ⇒ **拒绝**。理由：此时备份捕获的是**上一次 replace 之后**的状态，`changelog` 的
         `before` 失真（实测事故：命令被执行两次 ⇒ 45 个数据文件的变更被漏记）。
         确需二次重播种须显式 `--allow-rerun`，且备份目录会标 `RERUN` 前缀以便区分。
      ④ 先**整目录备份**（raw + snapshot 各自 `copytree`）到 `data/state/reseeds/<TS>/`，
         再写 changelog `RESEED.json`（逐文件 before/after sha256 + added/removed 清单）。
    """
    declared = _check_declaration(declare, dates)
    if not execute:
        raise SeedError("拒绝写入：缺少 --execute（本件默认只读）")
    if not reason or not str(reason).strip():
        raise SeedError('拒绝覆盖：重播种属**数据修正**，须显式给出 --reason "<说明>" 留痕')
    _assert_readonly()
    r_root, s_root = LD.roots()["raw"], LD.roots()["snapshot"]
    if not allow_rerun:                                       # ③ 防重入
        seen = _already_replaced(dates, r_root)
        if seen:
            raise SeedError(
                "拒绝重播种：%s 已处于 `replace=true` 状态（已被重播种过一次）——"
                "此时备份将是**上一次 replace 之后**的状态，changelog 的 before 会失真。"
                "确需二次重播种请显式加 --allow-rerun（备份将标 RERUN）。" % seen[:5])
    payload, side = build(dates, declared)
    probs = _validate(payload, declared, side)
    if probs:
        raise SeedError("契约校验未通过，零写入：\n  " + "\n  ".join(probs[:10]))
    ts = ("RERUN-" if allow_rerun else "") + time.strftime("%Y%m%dT%H%M%S")
    box = os.path.join(RESEED_ROOT, ts)
    _WG.check_write(os.path.join(RESEED_ROOT, ts))  # 备份/变更清单落点（生产 state）
    os.makedirs(box, exist_ok=True)
    before = {}
    for d in sorted(dates):                                   # ① 备份（先于任何覆盖）
        for root, tag in ((r_root, "raw"), (s_root, "snapshot")):
            src = os.path.join(root, d)
            if not os.path.isdir(src):
                continue
            shutil.copytree(src, os.path.join(box, tag, d))
            for f in sorted(os.listdir(src)):
                if os.path.isfile(os.path.join(src, f)):
                    before["%s/%s/%s" % (tag, d, f)] = sha256_file(os.path.join(src, f))
    decl = declaration_artifact(dates, declared)
    staged = []
    for d in sorted(payload):                                 # ② replace 重投放
        ING.stage_core(d, payload[d], source=SOURCE_LABEL, data_upto=d,
                       declaration=decl, replace=True)
        staged.append(d)
    promoted = [(d, ING.promote_snapshot(d, force=True)) for d in sorted(payload)]   # ③ force
    after = {}
    for d in sorted(dates):
        for root, tag in ((r_root, "raw"), (s_root, "snapshot")):
            src = os.path.join(root, d)
            if not os.path.isdir(src):
                continue
            for f in sorted(os.listdir(src)):
                if os.path.isfile(os.path.join(src, f)):
                    after["%s/%s/%s" % (tag, d, f)] = sha256_file(os.path.join(src, f))
    chg = {"schema": 1, "kind": "RESEED", "at": ING._now(), "reason": str(reason).strip(),
           "dates": sorted(dates), "declared_unresolved_keys": list(declared),
           "backup_dir": box, "n_files_before": len(before), "n_files_after": len(after),
           "changed": sorted(k for k in after if before.get(k) != after[k]),
           "added": sorted(k for k in after if k not in before),
           "removed": sorted(k for k in before if k not in after)}
    with io.open(os.path.join(box, "RESEED.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(chg, ensure_ascii=False, indent=1, sort_keys=True))
    return staged, promoted, box, chg


# ── 报告：只读预检 ───────────────────────────────────────────────────────────
def _status(argv):
    _assert_readonly()
    print("seed_real.py v%d · P0 生产历史数据播种（只读预检）" % SCHEMA_VERSION)
    print("  项目根   : %s" % ROOT)
    print("  源（只读）: %s" % SRC_ROOT)
    print("  目标      : %s" % LD.roots()["raw"])
    print("              %s" % LD.roots()["snapshot"])
    print("  目标现状  : data/raw exists=%s · data/snapshot exists=%s"
          % (os.path.isdir(LD.roots()["raw"]), os.path.isdir(LD.roots()["snapshot"])))
    print()
    print("  ── 源文件清单 ──")
    for p in (SRC_KLINES, SRC_ENV):
        print("    %-46s %12d B  sha256=%s" %
              (os.path.relpath(p, SRC_ROOT), os.path.getsize(p), sha256_file(p)[:16]))
    zs = zt_snapshots()
    print("    zt_pool/  %d 文件 / %d 交易日 %s ~ %s"
          % (sum(len(v) for v in zs.values()), len(zs),
             min(zs) if zs else "-", max(zs) if zs else "-"))
    nd = notice_dates()
    print("    notices/  %d 文件 %s ~ %s" % (len(nd), min(nd) if nd else "-", max(nd) if nd else "-"))
    ld = lhb_dates()
    print("    lhb/      %d 文件 %s ~ %s" % (len(ld), min(ld) if ld else "-", max(ld) if ld else "-"))
    print()
    print("  ── 契约 5 键 · 口径矩阵 ──")
    for k in ("daily_quote", "limit_up", "dragon_tiger", "news", "env_metrics"):
        v = CALIBER[k]
        mark = "✅ VERIFIED " if v["status"] == "VERIFIED" else "⛔ UNRESOLVED"
        print("    %s %-13s ← %s" % (mark, k, v["src"]))
        print("        口径: %s" % v["note"])
        if v.get("evidence"):
            print("        证据: %s" % v["evidence"])
        if v.get("risk"):
            print("        ⚠ 风险: %s" % v["risk"])
    bl = blocked_keys()
    print()
    print("  ── 结论 ──")
    print("    可产出键 : %s" % (" / ".join(k for k in CALIBER if k not in bl) or "(无)"))
    print("    未决键   : %s" % (" / ".join(bl) or "(无)"))
    print("    默认路径 : 未决键未清前 **不得播种、不得正式化**（--stage/--promote 拒绝）")
    print("    声明通道 : `--declare-unresolved %s`（须**逐项相等**，漏声明/多声明均拒）"
          % ",".join(bl))
    print("               ⇒ 未决键以**显式空**投放 + 落 `%s`；空值是声明出来的，"
          "不是省略出来的" % DECL_FNAME)
    print("    已知缺口日（显式登记，不跳过）: %s" % (" / ".join(sorted(KNOWN_GAP_DAYS)) or "(无)"))
    return 0


def _plan(argv):
    dates = _arg_dates(argv)
    raw = _arg_declare(argv)
    declared = _check_declaration(raw) if raw else []      # 只读预览：无声明不强制
    payload, side = build(dates, declared)
    print("--plan（只读：内存转换 + 契约校验 + 待写清单；不落盘）")
    print("  窗口 = %s ~ %s（%d 个日期）" % (dates[0], dates[-1], len(dates)))
    print("  声明 = %s" % (" / ".join(declared) or "(无 → 仅列可产出键；未决键不投)"))
    probs = _validate(payload, declared, side)
    for d in dates:
        s = side["per_date"][d]
        print("  %s  daily_quote=%-5s limit_up=%-4s(%s) news=%-5s(%s, 空关联%s)  env6=%s"
              % (d, s.get("daily_quote_n", "-"), s.get("limit_up_n", "-"),
                 s.get("limit_up_tag", "-"), s.get("news_n", "-"),
                 s.get("news_src", "-"), s.get("news_unmapped_code", 0),
                 {k: v for k, v in s["env_metrics_partial"].items() if v is not None}))
    print("  契约校验 : %s" % ("✅ 通过（已产出键列结构全合规）" if not probs else "❌ %d 项" % len(probs)))
    for p in probs[:10]:
        print("     - %s" % p)
    mf = manifest(payload)
    print("  待写清单（--stage 生效时会创建的文件；经**同一写入器** stage_core "
          "在临时 base 实测字节，与实写逐字节一致）:")
    for m in mf[:6]:
        print("    %s  %d 行 %8d B  sha256=%s" % (m["raw_path"], m["rows"], m["bytes"], m["sha256"]))
    if len(mf) > 6:
        print("    … 共 %d 个文件（%d 日期 × 已产出键）" % (len(mf), len(dates)))
    print("  已知缺口日 : %s" % (" / ".join(d for d in sorted(KNOWN_GAP_DAYS) if d in dates)
                              or "(窗口内无)"))
    return 0


def _arg_dates(argv):
    a = argv[argv.index("--from") + 1] if "--from" in argv else DEFAULT_FROM
    b = argv[argv.index("--to") + 1] if "--to" in argv else DEFAULT_TO
    days = sorted(d for d in zt_snapshots() if a <= d <= b)
    if not days:
        raise SeedError("窗口 %s~%s 内无源数据（zt_pool 覆盖 = %s）"
                        % (a, b, "%s~%s" % (min(zt_snapshots()), max(zt_snapshots()))
                           if zt_snapshots() else "无"))
    return days


# ── 隔离副本自检（--verify）：幂等 / 冲突 / 失败安全 / 历史边界 ────────────────
def _verify(argv):
    dates = _arg_dates(argv)[:3]
    payload, side = build(dates)
    ok = []
    tmp = tempfile.mkdtemp(prefix="seed_verify_")

    def T(name, cond, extra=""):
        ok.append(bool(cond))
        print(("  [OK] " if cond else "  [!!] ") + name + (("  " + extra) if extra and not cond else ""))

    print("--verify（隔离副本 %s：不碰生产 data/raw、data/snapshot）" % tmp)
    real = (LD.roots()["raw"], LD.roots()["snapshot"])
    pre = (os.path.exists(real[0]), os.path.exists(real[1]))
    try:
        # V1 契约校验
        T("V1 已产出键过 snapshot 列规格校验", not _validate(payload, side=side))
        # V2 源只读：转换前后源文件哈希不变
        h0 = sha256_file(SRC_KLINES)
        build(dates)
        T("V2 源文件零改写（转换前后源哈希一致）", sha256_file(SRC_KLINES) == h0)
        # V3 投放落点只认 base
        for d in dates:
            ING.stage_core(d, payload[d], source=SOURCE_LABEL, base=tmp, data_upto=d)
        raw_t = LD.roots(tmp)["raw"]
        T("V3 stage_core 只写 base 派生投放区",
          os.path.isdir(raw_t) and
          sorted(os.listdir(raw_t)) == dates)
        T("V4 生产 data/raw 状态零变化（自检前后同态）",
          (os.path.exists(real[0]), os.path.exists(real[1])) == pre)
        # V5 幂等：重复投放被拒（STAGED_EXISTS），且既有文件字节不变
        f = os.path.join(raw_t, dates[0], "daily_quote.csv")
        b0 = sha256_file(f)
        try:
            ING.stage_core(dates[0], payload[dates[0]], source=SOURCE_LABEL, base=tmp,
                           data_upto=dates[0])
            T("V5 重复投放被拒（防覆盖）", False, "未抛错")
        except ING.IngestError as e:
            T("V5 重复投放被拒 STAGED_EXISTS", "STAGED_EXISTS" in str(e))
        T("V6 被拒后既有文件逐字节不变", sha256_file(f) == b0)
        # V7 核心键不齐 → promote 拒绝，且不留快照
        try:
            ING.promote_snapshot(dates[0], base=tmp)
            T("V7 核心键不齐 promote 拒绝", False, "竟成功")
        except ING.IngestError as e:
            T("V7 核心键不齐 promote 拒绝 CORE_INCOMPLETE", "CORE_INCOMPLETE" in str(e))
        T("V8 被拒后零快照目录残留", not os.path.exists(os.path.join(LD.roots(tmp)["snapshot"], dates[0])))
        # V9 历史截止边界：data_upto 恒等于目标日，拒绝未来数据
        try:
            ING.stage_core("2026-10-01", payload[dates[0]], source=SOURCE_LABEL, base=tmp,
                           data_upto="2026-10-02")
            T("V9 未来数据被拒 FUTURE_DATA", False, "未抛错")
        except ING.IngestError as e:
            T("V9 未来数据被拒 FUTURE_DATA", "FUTURE_DATA" in str(e))
        # V10 未决口径闸
        blocked = blocked_keys()
        try:
            do_stage(dates, execute=True)
            T("V10 未决口径下 --stage 拒绝", False, "竟通过")
        except SeedError as e:
            T("V10 未决口径下 --stage 被拒并指名", all(b in str(e) for b in blocked),
              str(e)[:70])
        # V11 缺 --execute 拒绝
        try:
            do_promote(dates, execute=False)
            T("V11 缺 --execute 拒绝", False, "竟通过")
        except SeedError:
            T("V11 缺 --execute 拒绝", True)
        # V12 生产目标零变化
        T("V12 生产 data/raw、data/snapshot 全程状态不变（隔离副本未外溢）",
          (os.path.exists(real[0]), os.path.exists(real[1])) == pre)

        # ── V13~V18 显式声明通道（P0「显式声明阻塞后播种」） ──
        bl = blocked_keys()
        try:
            do_stage(dates, execute=True, declare=())
            T("V13 漏声明未决键 ⇒ 拒绝（禁静默播种未声明键）", False, "竟通过")
        except SeedError as e:
            T("V13 漏声明未决键 ⇒ 拒绝（禁静默播种未声明键）", "漏声明" in str(e), str(e)[:70])
        try:
            do_stage(dates, execute=True, declare=tuple(bl) + ("news",))
            T("V14 声明非未决键 ⇒ 拒绝（属伪造口径）", False, "竟通过")
        except SeedError as e:
            T("V14 声明非未决键 ⇒ 拒绝（属伪造口径）", "多声明" in str(e), str(e)[:70])

        sb2 = tempfile.mkdtemp(prefix="seed_decl_")
        try:
            pay2, side2 = build(dates, bl)
            T("V15a 声明齐备后契约校验通过（显式空形态合规）",
              not _validate(pay2, bl, side2), str(_validate(pay2, bl, side2)[:2]))
            dcl = declaration_artifact(dates, bl)
            for d in dates:
                ING.stage_core(d, pay2[d], source=SOURCE_LABEL, base=sb2, data_upto=d,
                               declaration=dcl)
            d0 = dates[0]
            r1 = LD.roots(sb2)["raw"]
            T("V15b 投放区落声明件（非契约文件，与 _core.meta.json 并列）",
              os.path.isfile(os.path.join(r1, d0, DECL_FNAME))
              and os.path.isfile(os.path.join(r1, d0, "_core.meta.json")))
            T("V15c dragon_tiger = 0 行显式空（不拼凑假映射）", pay2[d0]["dragon_tiger"] == [])
            _em = pay2[d0]["env_metrics"]
            _uncalc2 = side2["env_metrics"].get("fail_rate_uncomputable") or {}
            T("V15d env_metrics 已裁定 ⇒ 6 键齐备；null 仅允许**有审计登记**的 fail_rate",
              sorted(_em) == sorted(_ENV_KEYS)
              and all(_em[k] is not None for k in _ENV_KEYS if k != "fail_rate")
              and (_em["fail_rate"] is not None or d0 in _uncalc2),
              "nulls=%s uncalc=%s" % (sorted(k for k, v in _em.items() if v is None),
                                      sorted(_uncalc2)))
            st = ING.promote_snapshot(d0, base=sb2)
            sroot = LD.roots(sb2)["snapshot"]
            adm = json.load(io.open(os.path.join(sroot, d0, "_admission.json"), encoding="utf-8"))
            T("V16a 正式化成功且 A9 判决 sidecar 在场（%s）" % st, st == "WRITTEN")
            T("V16b ★声明随数据同行：limit_up AS_OF=intraday_snapshot 被真判并如实阻塞",
              "AS_OF" in adm["files"]["limit_up.csv"]["blocked_classes"])
            _emcl = adm["files"]["env_metrics.json"]["blocked_classes"]
            T("V16c dragon_tiger 含 SEMANTIC（未决键）；env_metrics 裁定后 SEMANTIC 已解除，"
              "仅余 AS_OF/COVERAGE（未检查 + 残留覆盖阻塞）",
              "SEMANTIC" in adm["files"]["dragon_tiger.csv"]["blocked_classes"]
              and "SEMANTIC" not in _emcl and "COVERAGE" in _emcl,
              "dt=%s em=%s" % (adm["files"]["dragon_tiger.csv"]["blocked_classes"], _emcl))
            try:
                S.assert_admissible(d0, root=sroot, need=("SEMANTIC",))
                T("V16d 层③ need=(SEMANTIC,) 对含未决键的快照必拦", False, "竟放行")
            except S.AdmissionRejected:
                T("V16d 层③ need=(SEMANTIC,) 对含未决键的快照必拦", True)
            alld = _arg_dates(argv)
            # 注：`dcl` 由**截断的 3 日**窗口构造（自检提速），故缺口日核验须用全窗口件
            T("V17 已知缺口日 2026-09-23 在窗口内且**显式登记**（登记 ≠ 跳过）",
              alld[0] <= "2026-09-23" <= alld[-1]
              and "2026-09-23" in declaration_artifact(alld, bl)["known_gap_days"]
              and "2026-09-23" in KNOWN_GAP_DAYS)
            T("V18 空值不得被读成「当日无此事」：声明件载明规则且列明键级理由",
              "空值是**声明出来的**" in dcl["rule"]
              and set(dcl["declared_unresolved_keys"]) == set(bl)
              and all(dcl["key_declarations"][k]["note"] for k in bl))
            # V19 声明件刷新**只动声明件**（数据文件逐字节不变；且拒造第二份真源）
            fp5 = {f: sha256_file(os.path.join(r1, d0, f)) for f in S._FNAMES}
            ING.declare_unresolved(d0, dcl, base=sb2)
            T("V19a 声明件刷新只动声明件：5 契约文件逐字节不变",
              all(sha256_file(os.path.join(r1, d0, f)) == fp5[f] for f in S._FNAMES))
            try:
                ING.declare_unresolved("2026-12-31", dcl, base=sb2)
                T("V19b 无投放区 ⇒ 拒写声明件（禁凭空造声明）", False, "竟通过")
            except ING.IngestError as e:
                T("V19b 无投放区 ⇒ 拒写声明件（禁凭空造声明）", "NOT_STAGED" in str(e), str(e)[:60])
            try:
                do_refresh_decl([d0], execute=True, declare=tuple(bl), base=sb2)
                T("V19c 声明件 provenance 与 sidecar 一致 ⇒ 刷新放行", True)
            except SeedError as e:
                T("V19c 声明件 provenance 与 sidecar 一致 ⇒ 刷新放行", False, str(e)[:90])
            # ── V20~V21 env_metrics 口径落地（2026-10-13 裁定：自算口径+显式标注） ──
            ea = side["env_metrics"]
            T("V20a limit_up_count = zt_pool 末档**行数**（方案甲：与 limit_up.csv 同源）",
              all(payload[d]["env_metrics"]["limit_up_count"] == ea["zt_rows"][d] for d in dates),
              "%s vs %s" % ([payload[d]["env_metrics"]["limit_up_count"] for d in dates],
                            [ea["zt_rows"][d] for d in dates]))
            T("V20b turnover = klines 全市场 amount 求和（剔除指数行）",
              all(payload[d]["env_metrics"]["turnover"]
                  == ea["turnover_universe"][d]["amount_sum_yuan"] for d in dates))
            T("V20c fail_rate = 触板未封 ÷ (封板 + 触板未封)（由审计计数复算）",
              all(abs(payload[d]["env_metrics"]["fail_rate"]
                      - ea["fail_counts"][d]["broken"]
                      / float(ea["fail_counts"][d]["sealed"] + ea["fail_counts"][d]["broken"])) < 1e-12
                  for d in dates
                  if (ea["fail_counts"][d]["sealed"] + ea["fail_counts"][d]["broken"]) > 0))
            T("V20d 机械剔除通道在跑：无前收 / 除权行**逐日计数**（禁把除权当炸板）",
              all(d in ea["exdiv_rows"] and d in ea["no_prev_rows"] for d in dates),
              "exdiv=%s noprev=%s" % (ea["exdiv_rows"], ea["no_prev_rows"]))
            T("V21 口径标注齐备：CALIBER.env_metrics.label 覆盖全部 6 键（口径变更必带标注）",
              set(CALIBER["env_metrics"].get("label") or {}) == set(_ENV_KEYS),
              "label=%s" % sorted(CALIBER["env_metrics"].get("label") or {}))
            T("V21b 裁定后未决键集合收窄为 ['dragon_tiger']",
              blocked_keys() == ["dragon_tiger"], "%s" % blocked_keys())
            # V22 防重入闸（2026-10-13 事故后新增）：已 replace=true ⇒ 拒绝二次重播种。
            #     用**含合成 meta 的临时投放区**单测判据纯函数，不碰生产。
            sb3 = tempfile.mkdtemp(prefix="seed_rerun_")
            try:
                _mk_meta(os.path.join(LD.roots(sb3)["raw"], "2026-09-16"), replace=False)
                T("V22a 未 replace 的投放区 ⇒ 判据返回空（可正常重播种）",
                  _already_replaced(["2026-09-16"], LD.roots(sb3)["raw"]) == [])
                _mk_meta(os.path.join(LD.roots(sb3)["raw"], "2026-09-17"), replace=True)
                T("V22b 已 replace 的投放区 ⇒ 判据指名（防 before 备份失真）",
                  _already_replaced(["2026-09-16", "2026-09-17"],
                                    LD.roots(sb3)["raw"]) == ["2026-09-17"])
            finally:
                shutil.rmtree(sb3, ignore_errors=True)
            # V20e 防回归：除权判据必须是**价格带**而非裸涨幅比（首版缺陷：误剔 31 只真涨停）
            T("V20e 除权判据含舍入：prev=36.97/close=high=40.67 的**真涨停**不得被判 EXDIV",
              classify_limit(36.97, 40.67, 36.97, 40.67, 0.10) == "SEALED")
            T("V20e2 越出涨停带（hi 11.05 > lp 11.00）确实判 EXDIV；无前收判 NOPREV",
              classify_limit(10.0, 11.05, 9.0, 9.5, 0.10) == "EXDIV"
              and classify_limit(None, 11.0, 9.0, 10.0, 0.10) == "NOPREV")
            # V20f 防回归：涨停价必须 half-up（银行家舍入会漏判；实测 sz000607 09-29）
            T("V20f 涨停价 half-up：prev=3.75 ⇒ 4.13（非 round() 的 4.12）；close=4.13 判 SEALED",
              limit_prices_cents(3.75, 0.10) == (413, 338)
              and classify_limit(3.75, 4.13, 3.50, 4.13, 0.10) == "SEALED")
            T("V20f2 四档上限 half-up 全对：5%/10%/20%/30% 的涨停·跌停价（分）",
              limit_prices_cents(10.00, 0.05) == (1050, 950)
              and limit_prices_cents(10.00, 0.10) == (1100, 900)
              and limit_prices_cents(10.00, 0.20) == (1200, 800)
              and limit_prices_cents(10.00, 0.30) == (1300, 700))
        finally:
            shutil.rmtree(sb2, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(os.path.join(_HERE, "__pycache__"), ignore_errors=True)
    # V23 --klines 路径安全（全局污染自恢复）
    def _peq(a, b):
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    _prev = SRC_KLINES
    _bad = False
    try:
        _set_klines(os.path.join(ROOT, "data", "raw", "meta", "trade_cal.csv"))
    except SeedError:
        _bad = True
    _restored = _peq(SRC_KLINES, _prev)
    _ok_ext = False
    try:
        # ★ 2026-11-02 修：原测试写死本机残留路径（C:/tmp/wb_a9/...），换机器必挂。
        #   `_set_klines` 只要求「存在 + 是文件 + 不在本项目 data/ 内」⇒ 直接借用
        #   仓库内既有的非 data/ 文件，**零临时件、零新写点**（造临时文件会引入
        #   未登记写点，被 landing 审计判 UNDECLARED_WRITE）。
        _ext_kl = os.path.join(ROOT, "AGENT.md")
        _old = _set_klines(_ext_kl)
        _ok_ext = _peq(SRC_KLINES, os.path.abspath(_ext_kl))
        _set_klines(_old)          # 经助手恢复全局（禁在 _verify 内直接赋值）
    except SeedError:
        pass
    T("V23 --klines 路径安全：data/ 内拒绝、外部文件接受、全局可恢复",
      _bad and _restored and _ok_ext and _peq(SRC_KLINES, _prev))
    # V24 --reseed 日期契约（纯函数沙盒测试，零写入）
    def _rd_err(argv_):
        try:
            _arg_reseed_dates(argv_)
            return None
        except SeedError as e:
            return str(e)
    _d3, _src3 = _arg_reseed_dates(
        ["--reseed", "2026-09-16,2026-09-23,2026-09-24", "--execute"])
    T("V24a 显式日期清单（逗号）精确解析为 09-16/09-23/09-24",
      _d3 == ["2026-09-16", "2026-09-23", "2026-09-24"] and _src3 == "显式日期清单")
    _d3b, _ = _arg_reseed_dates(
        ["--reseed", "2026-09-16", "2026-09-23", "2026-09-24"])
    T("V24b 空格分隔多 token 同样精确解析", _d3b == _d3)
    T("V24c 非法日（09-31）拒绝", _rd_err(
        ["--reseed", "2026-09-31"]) is not None)
    T("V24d 格式非法（20260916）拒绝", _rd_err(
        ["--reseed", "20260916"]) is not None)
    T("V24e 重复日期（冲突）拒绝", _rd_err(
        ["--reseed", "2026-09-16,2026-09-16"]) is not None)
    T("V24f 空解析（无日期无窗口）拒绝——严禁静默回退默认全量",
      _rd_err(["--reseed", "--execute"]) is not None)
    T("V24g 日期清单与 --from/--to 同时给出（冲突）拒绝",
      _rd_err(["--reseed", "2026-09-16", "--from", "2026-09-16",
               "--to", "2026-09-16"]) is not None)
    _w, _wsrc = _arg_reseed_dates(
        ["--reseed", "--from", "2026-09-23", "--to", "2026-09-24"])
    T("V24h 显式 --from/--to 窗口路径可用（恰 2 日）",
      _w == ["2026-09-23", "2026-09-24"] and _wsrc == "--from/--to 窗口")
    T("V24i 不在源覆盖内的日期拒绝",
      _rd_err(["--reseed", "2020-01-01"]) is not None)
    n_fail = sum(1 for c in ok if not c)
    print("\n== seed_real --verify: %d/%d 通过 ==" % (len(ok) - n_fail, len(ok)))
    return 1 if n_fail else 0


def do_refresh_decl(dates, execute=False, declare=(), base=None):
    """刷新已投放日的显式声明件（**只写声明件，绝不碰数据文件**）。

    用于授权文案 / 已知缺口登记变更后，把落盘声明件与当前代码一致化（幂等可重跑）。

    ★ 闸：若本件 `provenance` 与快照判决 sidecar（`_admission.json`）里记录的不一致 ⇒
      **拒绝**。改 `provenance` 会改变 A9 判决，必须重新正式化（`--promote --execute`）；
      只刷声明件会在"投放区声明"与"快照判决"之间造成**第二份真源**。
    ★ base：2026-10-19 修——原实现无 base 参数，内部 `admission_of` /
      `declare_unresolved` 双打生产根（沙盒不可用 + 自检 V19c 变相写生产被闸门
      拦崩）。现贯通：None = 生产（默认，经 writegate），有值 = 沙盒根。
    """
    declared = _check_declaration(declare, dates)
    if not execute:
        raise SeedError("拒绝写入：缺少 --execute（本件默认只读）")
    dcl = declaration_artifact(dates, declared)
    prov = dcl.get("provenance") or {}
    drift = []
    for d in sorted(dates):
        try:
            adm = S.admission_of(d, root=base)
        except Exception:                                   # 无快照 / 未正式化 ⇒ 跳过一致性闸
            continue
        if isinstance(adm, dict) and not adm.get("missing"):
            if (adm.get("provenance") or {}) != prov:
                drift.append(d)
    if drift:
        raise SeedError("声明件 provenance 与快照判决 sidecar 不一致 %s —— 改 provenance 必须"
                        "重新正式化（--promote --execute），禁只刷声明件造第二份真源" % drift[:5])
    return [(d, ING.declare_unresolved(d, dcl, base=base)) for d in sorted(dates)]


def _arg_declare(argv):
    """解析 `--declare-unresolved k1,k2`（无此参数 ⇒ 空元组 ⇒ 走原「一律拒绝」路径）。"""
    if "--declare-unresolved" not in argv:
        return ()
    i = argv.index("--declare-unresolved")
    if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
        raise SeedError("--declare-unresolved 需跟键名清单，"
                        "例：--declare-unresolved dragon_tiger")
    return tuple(x.strip() for x in argv[i + 1].split(",") if x.strip())


def _set_klines(path):
    """`--klines` 输入源覆盖：**只换行情读取来源**（SRC_KLINES 的 3 个读取点全部
    是输入侧），不触碰任何业务逻辑。

    ★ 路径安全断言（与 `_assert_readonly` 同纪律）：必须存在、是文件、且
    **不在本项目 data/ 内**（防自读自写）。返回旧值以便调用方恢复。
    """
    global SRC_KLINES
    p = os.path.abspath(os.path.expanduser(str(path or "")))
    if not os.path.isfile(p):
        raise SeedError("--klines 文件不存在：%s" % p)
    proj_data = os.path.normcase(os.path.join(ROOT, "data"))
    if os.path.normcase(p).startswith(proj_data):
        raise SeedError("--klines 禁指向本项目 data/ 内（防自读自写）：%s" % p)
    prev = SRC_KLINES
    SRC_KLINES = p
    return prev


def _arg_reseed_dates(argv):
    """`--reseed` 的日期参数契约（2026-10-13 执行范围事故后加固）：

    事故：`--reseed` 后面的日期清单曾被静默忽略，日期由 `--from/--to` 默认窗口
    决定 ⇒ 授权 3 日、实际执行 9 日。本契约修复为 **fail-closed**：
      ① 显式日期清单：`--reseed` 之后、下一个 `--` 旗标之前的全部 token
        （逗号或空格分隔均可），严格解析；
      ② 或显式 `--from/--to` 窗口；
      ③ 两者同时给出 ⇒ **冲突拒绝**；都不给 ⇒ **拒绝**（严禁静默回退默认全量日期）；
      ④ 格式非法 / 日期无效 / 空解析 / 重复日期 / 不在源数据覆盖内 ⇒ 一律拒绝。
    返回 (排序后日期集, 来源说明)。调用方须在执行**前**打印该集合。
    """
    i = argv.index("--reseed")
    toks = []
    for t in argv[i + 1:]:
        if t.startswith("--"):
            break
        toks.append(t)
    dates = []
    for t in toks:
        dates.extend(x.strip() for x in t.split(",") if x.strip())
    has_window = "--from" in argv or "--to" in argv
    if dates and has_window:
        raise SeedError("日期参数冲突：--reseed 后的显式日期清单与 --from/--to 窗口"
                        "不可同时给出（防两套口径各说各话）")
    if not dates and not has_window:
        raise SeedError("--reseed 需要显式日期清单（例：--reseed 2026-09-16,2026-09-23,"
                        "2026-09-24）或显式 --from/--to 窗口——**严禁静默回退默认全量日期**"
                        "（2026-10-13 执行范围事故）")
    if not dates:
        return _arg_dates(argv), "--from/--to 窗口"
    import datetime as _dt
    for d in dates:
        if len(d) != 10 or d[4] != "-" or d[7] != "-":
            raise SeedError("日期格式非法（须 YYYY-MM-DD）：%r" % d)
        try:
            _dt.date(int(d[:4]), int(d[5:7]), int(d[8:10]))
        except ValueError:
            raise SeedError("日期无效：%r" % d)
    if len(set(dates)) != len(dates):
        raise SeedError("日期清单含重复项（冲突）：%s" % sorted(dates))
    out = sorted(dates)
    avail = zt_snapshots()
    if not avail:
        raise SeedError("源数据覆盖为空（zt_pool 无档），无法校验日期范围")
    miss = [d for d in out if d not in set(avail)]
    if miss:
        raise SeedError("日期不在源数据覆盖内（zt_pool %s~%s）：%s"
                        % (min(avail), max(avail), miss))
    return out, "显式日期清单"


def _main(argv):
    try:
        if "--verify" in argv:
            return _verify(argv)
        if "--plan" in argv:
            return _plan(argv)
        if "--stage" in argv:
            done, _ = do_stage(_arg_dates(argv), execute="--execute" in argv,
                               declare=_arg_declare(argv))
            for d, staged, warn in done:
                print("  %s staged=%s %s" % (d, staged, ("WARN " + "; ".join(warn)) if warn else ""))
            return 0
        if "--reseed" in argv:
            if "--klines" in argv:
                ki = argv.index("--klines")
                kp = argv[ki + 1] if ki + 1 < len(argv) else None
                _set_klines(kp)
                print("  行情输入源  : %s（--klines 覆盖 · 路径安全断言已过）" % SRC_KLINES)
            rdates, rsrc = _arg_reseed_dates(argv)      # fail-closed 日期契约
            print("  目标日期    : %s（来源=%s · 执行前确认）" % (",".join(rdates), rsrc))
            i = argv.index("--reason") if "--reason" in argv else -1
            rsn = argv[i + 1] if i >= 0 and i + 1 < len(argv) else None
            staged, promoted, box, chg = do_reseed(
                rdates, execute="--execute" in argv,
                declare=_arg_declare(argv), reason=rsn,
                allow_rerun="--allow-rerun" in argv)
            print("  备份+留痕 : %s" % box)
            print("  重投放    : %d 日（replace 通道）" % len(staged))
            print("  重正式化  : %s" % ", ".join("%s=%s" % (d, st) for d, st in promoted))
            print("  变更文件  : %d 处（changed）· 新增 %d · 移除 %d"
                  % (len(chg["changed"]), len(chg["added"]), len(chg["removed"])))
            for k in chg["changed"][:12]:
                print("     Δ %s" % k)
            return 0
        if "--refresh-decl" in argv:
            for d, p in do_refresh_decl(_arg_dates(argv), execute="--execute" in argv,
                                        declare=_arg_declare(argv)):
                print("  %s decl=%s" % (d, p))
            return 0
        if "--promote" in argv:
            for d, st in do_promote(_arg_dates(argv), execute="--execute" in argv,
                                    declare=_arg_declare(argv)):
                print("  %s promote=%s" % (d, st))
            return 0
        return _status(argv)
    except SeedError as e:
        print("⛔ %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
