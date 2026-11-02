#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""settle.py · 五步结算（施工方案 0.6 · v1.1 · 2026-10-08 建 · v1.2 2026-10-10 · v1.3 2026-10-13）

变更记录 v1.3（日期轴缺口修复 · 2026-10-13 · 用户**定向授权：仅改本文件**）：
  ★ 缺陷（v1.2 及以前的实现）：本模块的日期轴**全部**取自 SNAP.available_dates()，
    即「实际存在的快照目录名」。该口径只有**一条轴**，无法区分两种截然不同的事：
      · 该日休市（按日历不是交易日）——没有快照是**正常**的；
      · 该日是交易日但快照未采集——没有快照是**缺口**。
    后果三条（均已在隔离测试中复现，见 §I 组）：
      ① **结算窗口静默延长**：`_resolve_exit` 用「快照日序列」当交易日序列，
         `len(days) < w` 一律记 PENDING_WINDOW；而 PENDING_WINDOW 属**待续型**（CARRY）
         ⇒ 该步照常打勾、游标照常推进 ⇒ 缺口**既不入账也不阻塞**，被永久吞掉。
         且 hold_days 计的是「有快照的天数」，不是交易日序号。
      ② **退出日期错位**：缺口事后补录 ⇒ 窗口末日/离场日**整体后移**
         （成交日也被推到更晚的日期，离场价取错日期）。
      ③ **游标错误推进**：`open_day` 防跳日只看「快照目录」，
         交易日无快照时该日不被识别 ⇒ 游标可直接跳过该交易日。
  ★ 修复（结构 = 双轴分离，**不新增任何落盘件、不改账本协议**）：
      · **交易日身份只来自日历**（复用 tools/fs/dayclass.py 四态判定，本模块不自写
        交易日算术）；**数据齐备只来自快照**。两轴**分别**判定，互不替代。
      · 交易日无数据 ⇒ 阻塞型缺口 `GAP_SESSION_NO_DATA`；无日历/越界（状态未知）
        ⇒ 阻塞型缺口 `UNKNOWN_DAY_STATE`（fail-closed）。二者**都不是** PENDING_WINDOW，
        该步不打勾、游标不推进，缺口补齐后重跑同日即完成。
      · 窗口末日改由**日历**锚定 ⇒ 补录数据只补价格、**不再移动**窗口末日/离场日。
      · `_day_range` 的成交日上界由 `"9999-12-31"` 收回为 **as_of**（禁读未来快照）。
      · `PENDING_DATA` 移出 CARRY —— 「窗口内缺该 code 行情 / 窗口末日缺收盘价」
        是**数据缺口**而非「时间未到」，旧口径把它当待续型 = 缺口被静默吞掉。
  ★ 日历从哪来：本模块**不自行发现、不落盘、不内置**任何历法（那会是第二个真源）。
    日历由调用方经 `calendar=` 注入（`--calendar <csv>` 由 CLI 注入）。
    **未注入日历 ⇒ 全链路 UNKNOWN ⇒ 结算被拒**（这才是「缺口不得静默跳过」）。
    生产侧目前**没有已授权的日历落盘件** ⇒ 本项为**待授权事项**，不在本次改动内自行补建。

变更记录 v1.2（任务9#1 交易成本建模 · 2026-10-10 · 用户授权施工）：
  · **费用进入正式结算账本**：盈亏结算步按正式费率参数（params.json 1.1.0
    `cost_buy_rate`=0.125% 买入侧 / `cost_sell_rate`=0.175% 卖出侧，均相对各自
    成交金额；BASELINE/UNCALIBRATED **模型假设，非真实费单**）计算每笔费用
    fee_amount = capital×买费率 + capital×(exit/entry)×卖费率，随结算行落账；
    pnl 行新增 fee_amount / net_amount（= 毛 − 费，SCHEMA 2→3）。
  · net_curve / summary 消费费后口径 —— 回放、结算、盈亏账本统一消费同一
    成本模型；报告层不再自扣假设费用（audit_replay 改为对账复核）。
  · pnl_amount 保持**毛盈亏**（纯价差）语义不变——费前/费后两口径都留痕。

变更记录 v1.1.2（C5 组合暴露修复批 · 2026-10-10 · 用户授权范围）：
  · **封堵 request 回退旁路**：`_step_fill` 旧口径「RISK_PLAN 缺失 → 按
    position_request 回退成交」= 未经 C5 审批的决策可在结算层全额成交
    （硬闸旁路向量）。改为 RISK_PLAN 缺失 → unfilled（拒绝成交，note 落痕）。
    全部调用链（batch_replay/chain_c34/raw_to_settle/real_replay）本就恒写
    RISK_PLAN → 回放数字零影响；自检夹具 mk_buy 显式落 RISK_PLAN 对齐。
  · 幂等安全：拒绝走现成 unfilled 终态通道（一决策一 EXECUTION，append-only）；
    已写入 EXECUTION 的决策重跑在读取层被跳过，不会被复检二次拒绝。

变更记录 v1.1.1（P0 逐笔审计裁决 · 2026-10-10）：
  · **outcome 语义统一为最终盈亏结果**：v1.1 中 STOP 分支固定 outcome=LOSS 是
    「事件语义」，与 WINDOW_END 分支的「结果语义」混合在同一字段（审计实证
    SIM-DEC-20260924-002：止损触发但 exit>entry → LOSS 配正 pct）。修正后
    outcome 一律按 sign(exit−entry) 对齐，退出原因改落 pnl 行新字段
    **exit_kind**（STOP/TARGET/WINDOW_END/NOT_FILLED）+ **exit_date**（退出日）——
    事件与结果分开记录（pnl SCHEMA_VERSION 1→2）。
  · 金额口径零变化：entry/exit/capital/pnl_amount 全部不变；仅 outcome 重标
    （止损触发但盈利的笔 LOSS→WIN）与新增两字段。

变更记录 v1.1（任务 8.3 前视偏差审计 · 2026-10-10）：
  v1.0 口径「成交日 = 决策日（T+0）」存在**结构性前视偏差**：决策使用盘后齐备的
  信息（收盘行情 / 盘后公告 / 龙虎榜 / 环境信号），却在决策日当天按当日价位成交——
  决策时点晚于当日全部盘中价格，当日盘中触发止损在因果上不可能。批量真实回放
  （任务 8）按 v1.0 口径复现出「决策日当天止损离场」即此缺陷的可观测形态。
  v1.1 修正（口径收紧、结果只会更差不会更好，非生存参数调整）：
    · 决策时点 = 决策日盘后（信息截止 = 决策日终）；
    · 成交日 = 决策日后**第一个有快照的交易日**，成交价 = 该日**开盘价**
      （限价语义：open > entry_limit → 不追价、不成交）；
    · 一字板（open==high==low==close）或开盘价触及涨停价 → 未成交（排板不保证）；
    · T+1 约束：成交日买入，**最早成交日后第一个交易日卖出**——止损/目标检查
      自成交日的下一快照日起；持有窗口 = 自成交日起 window 个交易日，
      窗口内未触发 → 窗口末日收盘价离场；hold_days = 卖出日序号（成交日 = 1）。
    · 跳空规则沿用：低开跌破止损 → 以当日 open 成交（更差）；高开越目标 → 以
      open 成交（更好）；同日双触发 → 止损优先（保守）。

用途：把一个交易日的账结完，并**唯一地**推进认知游标 C。
      C 是时间线地基：C 不推进，LLM 就看不到「昨天之后」的信息；C 乱推进，
      步进补单就是拿未来结果编的（后视镜造假器）。故本模块是全案时序的唯一闸门。

五步（顺序写死 = cursor.STEPS，禁第二套步名）：
  1 行情落定 → 2 成交记录 → 3 盈亏结算 → 4 复盘归因 → 5 学习反馈

★ 唯一执行器纪律（0.1 / 0.6 写死）：
  · checklist 打勾的唯一执行器 = 本文件；date 推进的唯一执行器 = 本文件
    （经 cursor.mark_step / cursor.advance_cursor，**不自行读写游标文件**）。
  · 本模块只经 DEC / PNL / SNAP / CUR 的公开接口落盘，**不直接碰任何账本文件**
    （`--discipline` 断言：文件内不出现游标文件名与两账本文件名）。

★ 原子性（用户 2026-10-08 点名审核项）：
  · 「推进」= 状态跃移，只在**五步全勾**后发生。五步未走完 → cursor.advance_cursor 抛错，
    C 停在前一日；本条另有「上一日未结账不得开新日」前置，双保险。
  · 步体先执行、后打勾：任一步抛错 → 该步不打勾 → 游标不推进，
    且失败**不吞**（抛 SettleError 到调用层，由 0.17 观测/告警）。
  · **步内有未清缺口 → 该步不算完成、不打勾**（`status=PENDING`）——
    「缺口」与「完成」是两件事：把 `PENDING_JUDGEMENT` 记成 `DONE` 等于把漏结算
    伪装成已结账，那一天永远等不到重试。缺口消失后重跑**即补齐**（步级幂等）。
  · 开日 = date 前移 + checklist 清零，由 cursor.advance_cursor **一次原子落盘**，
    不出现「日期变了、勾还在」的中间态。
  · 防跳日：开日时若旧日与目标日之间**存在已采集（有快照）但未结账的交易日** → 拒
    （SKIP_SETTLEMENT）。逐日结是唯一合法路径。

★ 幂等（0.6 验收「重复跑同日不重复记账」）：
  · 已打勾的步**整步跳过**（status=ALREADY），步体不再执行 → 同日重跑零副作用。
  · 步内兜底：快照 EXISTS / EXECUTION EXISTS / pnl EXISTS / 学习留痕按 as_of 去重，
    即使 checklist 被人为清空，重跑也不产生第二行（自检 G4 用字节级对比证明）。

★ 边界（F6 不改职责）：
  · 本模块**不产生交易动作**（无下单/撤单/成交接口，`orders_created` 恒 0）；
    「成交记录」是把可成交性判定结果**记账**，不是去下单。
  · 「盈亏结算」的资金部分机械派生（可手算复现），但**预测判定（M1/M2/M3）与归因
    （L1L5 / 三问）不自行编造**：由调用方经 `judgements` 注入，真源字段名一律沿用
    pnl.py（禁第二套同义字段）。未注入 → 记 PENDING 且**不写行**（漏结算可后补，
    写错的结算永远错）。
  · 只消费 F1~F5 已落件：游标 / 快照 / 决策账本 / 净值账本 / 参数总账。

被谁调用：tools/fs/wakeup.py（0.14 唤醒 → 追平游标）、人工 `--settle <date>`。

用法：
  python tools/risk/settle.py                  # 只读：游标状态 + 五步待办 + 账目计数
  python tools/risk/settle.py --selftest       # 内置验收（临时目录，不碰真数据）
  python tools/risk/settle.py --settle 2026-10-08
  python tools/risk/settle.py --discipline

规则：五步顺序/步名不得增删改（改 = 改契约，须同步 0.1 checklist schema）；禁在别处打勾或推进游标。
"""
import copy
import datetime as _dt
import io
import json
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools", "fs")))
# ★ 2026-10-19：步4 的 `import case_builder`（tools/learning）此前**依赖调用方**把
#   tools/learning 放进 sys.path —— run_chain 有，直接 `settle()` 调用与 `--selftest` 没有
#   ⇒ Case 归因会静默降级为 case_info[0].error（实测 ModuleNotFoundError）。
#   这里显式入路径，使 C6 学习反馈与调用方无关。
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools", "learning")))
from paths import ROOT  # noqa: E402
import cursor as CUR  # noqa: E402
import snapshot as SNAP  # noqa: E402
import decision as DEC  # noqa: E402
import pnl as PNL  # noqa: E402
import limit_rules as LIMIT  # noqa: E402  ★ 涨跌幅单真源（2026-11-02）
import params as PARAMS  # noqa: E402
import astwin as ASTW  # noqa: E402   ← AST 窗口（尾界）唯一实现，禁本模块自写
import dayclass as DC  # noqa: E402   ← 日期四态（交易日 × 数据）唯一判定，禁本模块自写交易日算术

SCHEMA_VERSION = 1

# 步名与顺序 = 真源 cursor.STEPS（禁在此另写一份字面量）
STEP_MARKET = CUR.STEPS[0]      # 行情落定
STEP_FILL = CUR.STEPS[1]        # 成交记录
STEP_PNL = CUR.STEPS[2]         # 盈亏结算
STEP_ATTR = CUR.STEPS[3]        # 复盘归因
STEP_LEARN = CUR.STEPS[4]       # 学习反馈
STEPS = CUR.STEPS

LEARNING_PATH = os.path.join(ROOT, "data", "learning", "demand_queue.jsonl")
EPS = 1e-9

# 步5 缺口阈值（骨架阶段内置默认；1.6 demand_scanner 落盘后改读 params）
DEFAULT_REPEAT_WRONG_THRESHOLD = 2
DEFAULT_MIN_SETTLED_FOR_RATIO = 3
DEFAULT_SUPPORT_RATIO_FLOOR = 1.0 / 3.0

FILL_NOTE_PREFIX = "settle 推定："     # 成交推定口径必须写在行内（EXECUTION.note 是既有字段，未改 schema）

# 步内缺口分两类（施工 0.6 原子性；用户 2026-10-08 点名审核项）：
#  · **待续型 CARRY** = 按设计尚未到期，次日重跑自然继续 → 不算缺口、照常打勾。
#    若把这类也当阻塞，窗口跨日就永远结不了账（游标会卡死在入场日，窗口永远走不完）。
#  · **阻塞型** = 缺的是**外部输入**（判定 / 本金 / 当日行情），不补齐就等于当天该结的账
#    没结；一旦打勾，缺口会被永久吞掉（没有人会再回来补）。故视为未完成 → 不打勾、游标不推进。
#  未知理由一律按**阻塞**处理（fail-safe：新增缺口类型不可能被静默吞掉）。
# v1.1：EXECUTION_NOT_DUE（成交日快照尚未产生——时间未到，非数据缺口）入 CARRY。
# v1.3：**PENDING_DATA 移出 CARRY** —— 「窗口内缺该 code 行情 / 窗口末日缺收盘价」
#   是那一天的数据缺失（外部输入没到），不是"时间未到"。旧口径把它归入待续型
#   ⇒ 步照常打勾、游标照常推进 ⇒ 缺口被永久吞掉（与 ①②③ 同一类病）。
CARRY_REASONS = ("PENDING_WINDOW", "NO_EXECUTION", "EXECUTION_NOT_DUE")

# ── 日期轴双判（v1.3）：交易日身份只来自日历，数据齐备只来自快照 ───────────────────
#  铁律：**禁止**用「快照目录不存在」推「该日休市」；也禁止用「有快照」推「该日是交易日」。
#  两条轴分别判定、互不替代：日历说是不是交易日；快照说有没有数据。
#  两类新缺口一律**阻塞型**（不在 CARRY_REASONS 内 ⇒ `_blocking` 自动拦住，fail-safe）。
BLOCK_GAP_NO_DATA = "GAP_SESSION_NO_DATA"   # 交易日但无数据 ⇒ 缺口，须补数（不得当"未到期"）
BLOCK_DAY_UNKNOWN = "UNKNOWN_DAY_STATE"     # 无日历 / 越界 ⇒ 交易日身份未知 ⇒ fail-closed

# 纪律检查用的禁用子串——用拼接构造，避免「检查串自身出现在源码里」的自指误报
NAME_CURSOR_FILE = "cursor" + ".json"
NAME_CURSOR_CONST = "CURSOR" + "_PATH"
NAME_LEDGER_DEC = "decision" + "_ledger.jsonl"
NAME_LEDGER_PNL = "pnl" + "_ledger.jsonl"


class SettleError(Exception):
    """结算被拒（前置未满足 / 数据缺口 / 五步未走完 / 账目不一致）——显式抛错，不静默降级。"""


def _check_date(date):
    s = date.isoformat() if hasattr(date, "isoformat") else str(date)
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise SettleError("日期必须是 YYYY-MM-DD，收到: %r" % (date,))
    try:
        _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        raise SettleError("日期非法: %r" % (date,))
    return s


def _now():
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def _step_result(name, **kw):
    out = {"step": name, "status": None, "written": 0, "reused": 0, "pending": [], "notes": []}
    out.update(kw)
    return out


def _reason(p):
    """缺口条目 → 理由串（缺字段不猜，退回 '?'）。"""
    return str((p or {}).get("reason") or (p or {}).get("detail") or "?")


def _blocking(pending):
    """从 pending 里挑出**阻塞型**缺口（待续型 CARRY_REASONS 不算）。"""
    return [p for p in (pending or []) if _reason(p) not in CARRY_REASONS]


def _quote_of(snap, code):
    """从快照日线里取该 code 的行（找不到 → None，由调用方判数据缺口）。"""
    for r in (snap.get("daily_quote") or []):
        if str(r.get("code")) == str(code):
            return r
    return None


def _load_snapshot_or_raise(date, snapshot_root):
    snap = SNAP.load_snapshot(date, root=snapshot_root)
    if snap is SNAP.UNAVAILABLE:
        raise SettleError("快照缺失：%s（%s）—— 结算不得用无快照的日子编账"
                          % (date, snapshot_root or SNAP.SNAPSHOT_ROOT))
    return snap


# ── 日期轴 v1.3：交易日（日历）× 数据（快照）**分别**判定 ─────────────────────────
#  本节的四个函数是「日期」这件事在本模块的**唯一入口**；其它地方不得再用
#  `SNAP.available_dates()` 当交易日序列（那是 v1.2 缺陷的根因）。
#  ★ 本模块**不内置、不发现、不落盘**任何历法：calendar 由调用方注入；
#    未注入 ⇒ 一律 UNKNOWN ⇒ 阻塞（fail-closed），绝不用「工作日近似」顶替。
def data_days(snapshot_root):
    """快照数据日集合（只读）。只回答「该日有没有数据」，**不参与任何时间轴推算**。"""
    return frozenset(SNAP.available_dates(root=snapshot_root))


def day_verdict(d, *, calendar=None, dd=None):
    """单日四态判定的唯一入口（复用 dayclass，**禁**本模块自写交易日算术）。

    → {date, status, exact, anchor, ...}；status ∈ TRADING_WITH_DATA /
      TRADING_NO_DATA / NON_TRADING / UNKNOWN。日历判「是不是交易日」，
      `dd`（快照数据日）判「有没有数据」——两者**缺一不可、互不替代**。
    """
    return DC.classify(d, calendar=calendar, data_dates=tuple(dd or ()))


def _session_axis(dec_date, as_of, calendar, dd):
    """成交日轴 v1.3：决策日之后**第一个交易日**（日历轴），且不晚于 as_of。

    返回 `{state, date?, reason?}`，四态封闭：
      OK                     该交易日 ≤ as_of 且**有数据** ⇒ 可作成交日
      NOT_DUE                该交易日 > as_of（**时间未到**，待续型）
      GAP_SESSION_NO_DATA    该交易日 ≤ as_of 但**无快照** ⇒ **缺口，阻塞**
      UNKNOWN_DAY_STATE      无日历 / 日历未覆盖 / 尾界 ⇒ 身份未知 ⇒ **阻塞**
    ★ 分水岭：**NOT_DUE 与 GAP 必须分开**——旧实现把两者混成一个"没找到快照日"，
      于是"交易日缺数据"被当成"时间未到"，成交日被静默推到更晚的日期。
    """
    if calendar is None or not len(calendar):
        return {"state": BLOCK_DAY_UNKNOWN,
                "reason": "无交易日历：无法区分「休市」与「交易日但无数据」"}
    if not calendar.covers(dec_date) or not calendar.covers(as_of):
        return {"state": BLOCK_DAY_UNKNOWN,
                "reason": "日历（%s）未覆盖 %s ~ %s —— 越界不外推"
                          % (calendar.basis(), dec_date, as_of)}
    nxt = calendar.next_session(dec_date)
    if nxt is None:
        return {"state": BLOCK_DAY_UNKNOWN,
                "reason": "日历中 %s 之后无交易日（尾界）—— 不外推" % dec_date}
    if nxt > as_of:
        return {"state": "NOT_DUE", "date": nxt}
    if nxt not in dd:
        return {"state": BLOCK_GAP_NO_DATA, "date": nxt, "missing": [nxt],
                "reason": "成交日 %s 是交易日但没有快照（缺口，非时间未到）" % nxt}
    return {"state": "OK", "date": nxt}


def _window_axis(d0, as_of, n, calendar, dd):
    """持有窗口轴 v1.3：窗口 = 自成交日（= d0 之后第一个交易日）起的 n 个**交易日**。

    返回 `{state, days, need, complete, reason?}`：
      state = OK                  日历可用；`days` = 截至 as_of 已有的交易日（日历轴）
              GAP_SESSION_NO_DATA 窗口**需要**的某交易日无数据 ⇒ 阻塞
              UNKNOWN_DAY_STATE   无日历 / 未覆盖 / 尾界 ⇒ 阻塞
      complete = (len(days) >= n) 窗口是否已走满 —— **只**决定"未触发时算待续还是算末日离场"，
                 **不**用来提前短路：窗口没走满也可能已经触发止损/目标（须先扫价）。
    ★ 顺序纪律（旧实现的病）：**先查缺口、再看长度**；且"未走满"不得抹掉"已触发"。
      若把"没走满"当挡箭牌，缺口日就会被伪装成待续型（PENDING_WINDOW ⇒ 打勾 ⇒ 游标推进）。
    ★ `days` 恒为**日历交易日序列**，休市日**不进序列**（故 hold_days = 交易日序号，
      不再等于"有快照的天数"）。
    """
    if calendar is None or not len(calendar):
        return {"state": BLOCK_DAY_UNKNOWN,
                "reason": "无交易日历：无法区分「休市」与「交易日但无数据」"}
    if not calendar.covers(d0) or not calendar.covers(as_of):
        return {"state": BLOCK_DAY_UNKNOWN,
                "reason": "日历（%s）未覆盖 %s ~ %s —— 越界不外推"
                          % (calendar.basis(), d0, as_of)}
    nxt = calendar.next_session(d0)
    if nxt is None:
        return {"state": BLOCK_DAY_UNKNOWN,
                "reason": "日历中 %s 之后无交易日（尾界）—— 不外推" % d0}
    if nxt > as_of:
        return {"state": "OK", "days": (), "need": (), "complete": False,
                "reason": "成交日（%s）尚未到 as_of=%s（时间未到）" % (nxt, as_of)}
    days = tuple(s for s in calendar.sessions_between(d0, as_of) if s > d0)
    need = days[:n]
    missing = [s for s in need if s not in dd]
    if missing:
        return {"state": BLOCK_GAP_NO_DATA, "days": days, "need": need, "missing": missing,
                "reason": "窗口内交易日无快照（缺 %s）—— 缺口，非时间未到"
                          % " / ".join(missing)}
    return {"state": "OK", "days": days, "need": need, "complete": len(days) >= n}


def axis_report(calendar, snapshot_root=None):
    """日期轴留痕（进结算报告；供审计核对"当时用的是哪条轴"）。**零写盘**。"""
    if calendar is None or not len(calendar):
        return {"calendar": "NONE", "basis": "EMPTY", "sessions": 0,
                "note": "未注入交易日历 ⇒ 交易日身份 UNKNOWN ⇒ 结算被拒（fail-closed）"}
    span = calendar.span if hasattr(calendar, "span") else (None, None)
    out = {"calendar": "PROVIDED", "basis": calendar.basis(),
           "sessions": len(calendar), "span": [span[0], span[1]]}
    if snapshot_root is not None:
        out["data_days"] = len(data_days(snapshot_root))
    return out


# ── 开日（不是 checklist 五步之一：checklist schema 冻结为 5 个步名） ────────────
def open_day(as_of, *, cursor_path=None, snapshot_root=None, calendar=None):
    """把游标的「工作日」推进到 as_of，前置 = 上一日五步全勾（由 cursor 强制）+ 防跳日。

    开日的语义是「开始结账 as_of」：date 前移 + checklist 清零（原子）。
    as_of 只有在本日五步全勾后才会成为 C（read_cursor / pending_steps 口径，本模块不改）。

    v1.3 防跳日升级为**两条轴各查一遍**（旧实现只看快照目录 ⇒ 交易日无快照时可直接跳过）：
      · 快照轴：中间有**已采集（有快照）但未结账**的日期 → SKIP_SETTLEMENT（原有）。
      · 日历轴：中间有**是交易日但没有快照**的日期 → SKIP_SETTLEMENT（新增，缺口不得跳过）。
      · 无日历 / 日历未覆盖 → 拒（状态未知，不猜）。
    """
    d = _check_date(as_of)
    cur = CUR.read_cursor(cursor_path)
    old = cur["date"]
    if d == old:
        return {"opened": False, "reason": "ALREADY_OPENED", "cursor_before": old,
                "cursor_unsettled": not CUR.is_settled(cur)}
    if d < old:
        raise SettleError("游标禁倒退：当前 %s，请求 %s" % (old, d))
    if not CUR.is_settled(cur):
        raise SettleError("上一日 %s 五步未走完（待办：%s）—— 结账未完不得开新日"
                          "（游标只在五步全勾后推进）" % (old, " / ".join(CUR.pending_steps(cur))))
    root = snapshot_root or SNAP.SNAPSHOT_ROOT
    between = [x for x in SNAP.available_dates(old, d, root=root) if old < x < d]
    if between:
        raise SettleError("SKIP_SETTLEMENT：%s 与 %s 之间有 %d 个已采集交易日未结账（%s）"
                          "—— 逐日结是唯一合法路径，不得跳日"
                          % (old, d, len(between), " / ".join(between)))
    # v1.3 日历轴：两条轴各查一遍。快照轴只能拦住"有数据却没结"，
    # 拦不住"是交易日却根本没数据"（那正是被静默跳过的缺口日）。
    if calendar is None or not len(calendar):
        raise SettleError(
            "日期轴未知（未注入交易日历）：无法判定 %s ~ %s 之间是否漏结交易日 —— 拒绝开日"
            "（缺口不得静默跳过；缺的就是缺，不用「工作日近似」猜）" % (old, d))
    if not calendar.covers(old) or not calendar.covers(d):
        raise SettleError("SKIP_SETTLEMENT：日历（%s）未覆盖 %s ~ %s —— 越界不外推"
                          % (calendar.basis(), old, d))
    dd = data_days(root)
    miss = [s for s in calendar.sessions_between(old, d) if old < s < d and s not in dd]
    if miss:
        raise SettleError(
            "SKIP_SETTLEMENT（日历轴）：%s 与 %s 之间有 %d 个**交易日无快照**（%s）"
            "—— 缺口日不得被静默跳过；先补数、再逐日结"
            % (old, d, len(miss), " / ".join(miss)))
    moved = CUR.advance_cursor(d, cursor_path)      # 原子：date 前移 + checklist 清零
    return {"opened": bool(moved), "reason": "OPENED", "cursor_before": old,
            "cursor_unsettled": False}


# ── 步1 行情落定 ──────────────────────────────────────────────────────────────
def _step_market(as_of, *, snapshot_root, raw_dir, force_snapshot, **_):
    """快照先行 → 再判落定（0.2 写死先后依赖，防无快照空判）。

    投放区缺失/缺文件 → SnapshotUnavailable（数据缺口，禁编造）→ 包成 SettleError 上抛。
    """
    try:
        r = SNAP.ingest_snapshot(as_of, raw_dir=raw_dir, root=snapshot_root,
                                 force=bool(force_snapshot))
    except SNAP.SnapshotUnavailable as e:
        raise SettleError("步1 行情落定：%s" % e)
    probs = SNAP.check_snapshot(as_of, root=snapshot_root)
    if probs:
        raise SettleError("步1 行情落定：快照已存在但契约不符 → 不算落定：%s" % " / ".join(probs))
    return _step_result(STEP_MARKET, written=1 if r["status"] == "WRITTEN" else 0,
                        reused=0 if r["status"] == "WRITTEN" else 1,
                        snapshot=r["path"], snapshot_status=r["status"],
                        files=list(r["files"]),
                        notes=["快照先行、落定后判（0.2 先后依赖）"])


# ── 步2 成交记录 ──────────────────────────────────────────────────────────────
def _limit_ratio(code, name):
    """精确涨停幅度 —— 委托 `fs/limit_rules`（2026-11-02 收敛为单真源，禁再复制口径）。"""
    return LIMIT.limit_pct(code, name)


def _exec_date_of(dec, as_of, calendar, dd):
    """成交日 = 决策日后第一个**交易日**（日历轴，v1.3），且不晚于 as_of。

    只用于"已知成交日"的回读（`_day_range`）；判成交走 `_session_axis` 全四态。
    未达 OK（缺口 / 未知 / 未到）⇒ None（不返回任何"次优日期"）。
    """
    ax = _session_axis(dec["date"], as_of, calendar, dd)
    return ax.get("date") if ax["state"] == "OK" else None


def _step_fill(as_of, *, base, snapshot_root, calendar=None, **_):
    """为 action=BUY 且尚无 EXECUTION 的决策判可成交性并落痕（0.6 口径 · v1.1、v1.3 日期轴）。

    口径 v1.1（前视修复，见模块头变更记录）：
      · 决策时点 = 决策日盘后；成交日 = 决策日后第一个快照日；成交价 = 该日 open。
      · 限价语义：open > entry_limit → 不追价 → unfilled（放弃，不追高）。
      · 一字板（open==high==low==close）→ 未排到 → unfilled；
        开盘价 ≥ 涨停价（精确规则，前收 = 决策日快照收盘）→ 排板不保证 → unfilled。
      · 成交日快照未产生（时间未到）→ pending EXECUTION_NOT_DUE（待续型，次日自愈）。
      · 成交日快照在但该 code 无行情 / 缺价 → 记 pending，**不判成交**（缺就是缺，不猜）。
      · 裁决仓（position_plan）为 0 → C5 否决 → unfilled。
      · 已知近似：真实成交量需外部成交回报（插件阶段接入）；本步按「开盘价可成交即
        在计划仓全额成交」推定，avg_price = 成交日 open，推定口径写进 EXECUTION.note。

    v1.3 日期轴（成交日改由**日历**锚定，不再由"快照目录"锚定）：
      · 成交日 = 决策日后第一个**交易日**；该日 ≤ as_of 却**无快照** ⇒
        `GAP_SESSION_NO_DATA`（**阻塞**），不再是"没找到快照日 ⇒ EXECUTION_NOT_DUE"。
        旧口径据此把成交日推到更晚的日期（离场价/持有期随之全错）。
      · 该交易日 > as_of ⇒ EXECUTION_NOT_DUE（**时间未到**，待续型）——两者严格分开。
      · 无日历 / 越界 ⇒ `UNKNOWN_DAY_STATE`（阻塞，fail-closed）。
    """
    written = reused = 0
    pending, notes, detail = [], [], []
    dd = data_days(snapshot_root)
    for sim in (False, True):
        for dec in DEC.iter_decisions(sim, base=base):
            if dec.get("action") != "BUY":
                continue
            if dec["date"] > as_of:
                continue                                  # 锁①：未来决策不得提前执行
            v = DEC.load_decision(dec["id"], base=base)
            if v.get("actual_position") is not None:
                reused += 1                               # 已有 EXECUTION → 幂等不改历史
                continue
            ax = _session_axis(dec["date"], as_of, calendar, dd)
            if ax["state"] == "NOT_DUE":
                pending.append({"decision_id": dec["id"], "reason": "EXECUTION_NOT_DUE",
                                "date": dec["date"], "next_session": ax.get("date")})
                continue
            if ax["state"] != "OK":
                # v1.3：缺口 / 未知 ⇒ 阻塞型（`_blocking` 拦住 ⇒ 该步不打勾、游标不推进）
                pending.append({"decision_id": dec["id"], "reason": ax["state"],
                                "date": dec["date"], "detail": ax.get("reason"),
                                "missing": list(ax.get("missing") or ()),
                                "next_session": ax.get("date")})
                continue
            d_exec = ax["date"]
            snap = SNAP.load_snapshot(d_exec, root=snapshot_root)
            if snap is SNAP.UNAVAILABLE:
                pending.append({"decision_id": dec["id"], "reason": "NO_SNAPSHOT",
                                "date": d_exec})
                continue
            row = _quote_of(snap, dec["code"])
            if row is None:
                pending.append({"decision_id": dec["id"], "reason": "NO_QUOTE",
                                "date": d_exec})
                continue
            op = row.get("open")
            low, high = row.get("low"), row.get("high")
            if op is None or low is None or high is None:
                pending.append({"decision_id": dec["id"], "reason": "QUOTE_FIELD_MISSING",
                                "date": d_exec})
                continue
            one_board = (op == high == low == row.get("close"))
            limit_hit = False
            prev_snap = SNAP.load_snapshot(dec["date"], root=snapshot_root)
            if prev_snap is not SNAP.UNAVAILABLE:
                prev_row = _quote_of(prev_snap, dec["code"])
                if prev_row and prev_row.get("close") is not None:
                    lim_px = round(float(prev_row["close"])
                                   * (1 + _limit_ratio(dec["code"], row.get("name"))), 2)
                    limit_hit = op >= lim_px - 0.005
            plan = v.get("position_plan")
            if plan is None:
                # v1.1.2（C5 组合修复批）：无 RISK_PLAN = 未经 C5 审批，不得按
                # position_request 回退成交（旧回退 = 硬闸旁路向量）。
                ap, res, why = 0.0, "unfilled", "无 RISK_PLAN（未经 C5 审批，拒绝按 request 回退成交）"
            elif plan <= 0:
                ap, res, why = 0.0, "unfilled", "C5 裁决否决（position_plan=0）"
            elif one_board:
                ap, res, why = 0.0, "unfilled", "一字板未排到，判未成交（未在场）"
            elif limit_hit:
                ap, res, why = 0.0, "unfilled", "开盘价触及涨停价，排板不保证成交（未在场）"
            elif op > float(v["entry_limit"]) + EPS:
                ap, res, why = 0.0, "unfilled", ("开盘价 %s 高于限价 %s → 不追价不成交"
                                                 % (op, v["entry_limit"]))
            else:
                ap, res, why = plan, "filled", None
            r = DEC.append_execution(dec["id"], ap, res, as_of=d_exec,
                                     avg_price=(op if ap > 0 else None),
                                     note=_fill_note(why, d_exec, op), base=base)
            if r["created"]:
                written += 1
            else:
                reused += 1
            detail.append({"decision_id": dec["id"], "code": dec["code"],
                           "date": dec["date"], "exec_date": d_exec,
                           "result": res, "actual_position": ap, "fill_price": op,
                           "day_low": low, "day_high": high,
                           "one_board": bool(one_board), "limit_open": bool(limit_hit),
                           "reason": why})
    if pending:
        notes.append("未判成交 %d 笔：%s"
                     % (len(pending), " / ".join(sorted({_reason(p) for p in pending}))))
    return _step_result(STEP_FILL, written=written, reused=reused, pending=pending,
                        fills=detail, notes=notes)


def _fill_note(why, exec_date=None, fill_price=None):
    head = FILL_NOTE_PREFIX + "口径 v1.1：决策日后首快照日开盘成交"
    if exec_date:
        head += "（成交日 %s" % exec_date
        if fill_price is not None:
            head += " @open=%s" % fill_price
        head += "）"
    if why is None:
        return head + "；按计划仓全额成交（尚无成交回报）"
    return head + "；未在场：%s" % why


# ── 步3 盈亏结算 ──────────────────────────────────────────────────────────────
def _step_pnl(as_of, *, base, snapshot_root, judgements, equity, calendar=None, **_):
    """写 pnl 账本（append-only）。资金部分机械派生，判定部分**注入**，缺任一 → PENDING 不写行。

    资金机械派生（可手算复现 · v1.1 口径见模块头）：
      · 未成交（actual_position=0）→ NOT_FILLED，价 null、capital 0、hold_days 0。
      · 已成交 → 持有窗口 = 自成交日起 window 个交易日，T+1 自次一快照日起检查：
          止损/目标谁先触发：同日双触发 → **止损优先**（保守，禁事后择优）。
          跳空：低开跌破止损 → 以当日 open 成交（更差）；高开越过目标 → 以 open 成交（更好）。
          窗口内均未触发 → 以窗口末日**收盘价**离场。
        **只扫 ≤ as_of 的快照**（锁①：不拿未来数据结算）；窗口未走完 → PENDING_WINDOW。
      · entry = 实际成交均价（成交日 open）；capital = actual_position × equity
        （本金基数来自账户权益，0.12/F7 供；缺 → PENDING_CAPITAL，**不许随手编一个本金**）。
        judgements 里显式给 capital 则优先（用于与手算逐笔对齐）。
    """
    written = reused = 0
    pending, notes, detail = [], [], []
    jj = judgements or {}
    dd = data_days(snapshot_root)
    for sim in (False, True):
        for dec in DEC.iter_decisions(sim, base=base):
            if dec.get("action") != "BUY":
                continue
            if dec["date"] > as_of:
                continue
            if PNL.read_pnl(dec["id"], sim, base=base) is not None:
                reused += 1                                  # 已结算 → 幂等，不改结果
                continue
            v = DEC.load_decision(dec["id"], base=base)
            if v.get("actual_position") is None:
                pending.append({"decision_id": dec["id"], "reason": "NO_EXECUTION"})
                continue
            j = jj.get(dec["id"])
            ap = v["actual_position"] or 0.0
            if ap <= 0:
                # ★ v1.4（2026-10-19）：未成交 = **机械终态**——成交事实已由 EXECUTION
                #   机械确认，判定（M1/M2/M3 verdicts + L1L5/三问归因）是对「已成交笔结果」
                #   作出的，未成交笔**无结果可判** ⇒ **不再要求 judgements**。
                #   原实现在此之前就拦 PENDING_JUDGEMENT，导致 NOT_FILLED 永远卡住。
                #   兼容：调用方若仍给全三件判定，原样带上（旧行为不变）。
                oc, e, x, hd, exit_info = "NOT_FILLED", None, None, 0, {"kind": "NOT_FILLED"}
                xk, xd = "NOT_FILLED", None
                if not (isinstance(j, dict) and "verdicts" in j
                        and "attribution_L1L5" in j and "attribution_三问" in j):
                    j = None                  # 未给全 → 按「不判」落账（不占位、不编造）
            else:
                if not isinstance(j, dict) or "verdicts" not in j:
                    pending.append({"decision_id": dec["id"],
                                    "reason": "PENDING_JUDGEMENT"})
                    continue
                ex = _resolve_exit(v, as_of, snapshot_root, calendar, dd)
                # v1.3：白名单外一律记缺口（旧实现只枚举 PENDING_WINDOW/PENDING_DATA，
                # 新增的 GAP/UNKNOWN 会被当成终态结果继续往下写 = 静默吞掉）。
                if ex["kind"] not in ("STOP", "TARGET", "WINDOW_END"):
                    pending.append({"decision_id": dec["id"], "reason": ex["kind"],
                                    "detail": ex.get("reason"),
                                    "missing": list(ex.get("missing") or ()),
                                    "window_days": list(ex.get("days") or ())})
                    continue
                # v1.1：entry = 实际成交均价（成交日开盘价），非决策日参考限价
                oc, e, x, hd, exit_info = (ex["outcome"],
                                           v.get("avg_price") or v["entry_limit"],
                                           ex["exit_price"], ex["hold_days"], ex)
                xk, xd = ex["kind"], ex.get("exit_date")   # v1.1.1：退出原因/退出日落账
            cap = _capital_of(v, j or {}, equity)
            if oc != "NOT_FILLED" and cap is None:
                pending.append({"decision_id": dec["id"], "reason": "PENDING_CAPITAL"})
                continue
            dl, dh = _day_range(v, snapshot_root, as_of, calendar, dd)   # 成交日的 [low,high]
            # v1.2 费用正式入账：买入侧按投入金额、卖出侧按卖出成交金额，
            # 费率读正式参数（params.json cost_buy_rate/cost_sell_rate，BASELINE/UNCALIBRATED
            # 模型假设，非真实费单）；NOT_FILLED 无成交 → 费 0。
            fee = 0.0
            if oc != "NOT_FILLED" and cap:
                br = PARAMS.get_value("cost_buy_rate") or 0.0
                sr_ = PARAMS.get_value("cost_sell_rate") or 0.0
                fee = round(cap * br + cap * (x / e) * sr_, 6)
            r = PNL.write_pnl(dec["id"], oc,
                              # 未成交且未给全判定 ⇒ j 为 None ⇒ 三件传 None（pnl 按「不判」落账）
                              verdicts=(j.get("verdicts") if j else None),
                              attribution_L1L5=(j.get("attribution_L1L5") if j else None),
                              attribution_三问=(j.get("attribution_三问") if j else None),
                              entry_price=e, exit_price=x, exit_kind=xk, exit_date=xd,
                              capital=(cap or 0.0), fee_amount=fee,
                              hold_days=hd, settle_date=as_of, day_low=dl, day_high=dh,
                              base=base)
            if r["created"]:
                written += 1
            else:
                reused += 1
            detail.append({"decision_id": dec["id"], "outcome": oc, "entry": e, "exit": x,
                           "capital": cap or 0.0, "pnl_amount": r["row"]["pnl_amount"],
                           "fee_amount": r["row"]["fee_amount"],
                           "net_amount": r["row"]["net_amount"],
                           "hold_days": hd, "exit_info": exit_info, "axis": r["axis"]})
    if pending:
        notes.append("未结算 %d 笔（缺口显式化，不造数）：%s"
                     % (len(pending), " / ".join(sorted({p["reason"] for p in pending}))))
    return _step_result(STEP_PNL, written=written, reused=reused, pending=pending,
                        settlements=detail, notes=notes)


def _day_range(v, snapshot_root, as_of, calendar=None, dd=None):
    """成交日（决策日后第一个**交易日**）该 code 的 [low, high]——入口=open 必在区间内，
    保留校验作数据一致性哨兵；缺 → (None, None) 由 pnl 跳过校验。

    v1.3：成交日上界由 `"9999-12-31"` **收回为 as_of**。旧实现不限上界，
    会把成交日取到 as_of 之后并读入**尚未到期的未来快照**（同一笔账里混进未来数据）。
    """
    if dd is None:
        dd = data_days(snapshot_root)
    d_exec = _exec_date_of(v, as_of, calendar, dd)
    if d_exec is None:
        return None, None
    snap = SNAP.load_snapshot(d_exec, root=snapshot_root)
    if snap is SNAP.UNAVAILABLE:
        return None, None
    row = _quote_of(snap, v["code"])
    if row is None:
        return None, None
    return row.get("low"), row.get("high")


def _capital_of(v, j, equity):
    """本金基数：显式注入 > actual_position × equity > None（缺口）。"""
    if (v.get("actual_position") or 0.0) <= 0:
        return 0.0
    if j.get("capital") is not None:
        return round(float(j["capital"]), 2)
    if equity is None:
        return None
    return round(float(v["actual_position"]) * float(equity), 2)


def _resolve_exit(v, as_of, snapshot_root, calendar=None, dd=None):
    """机械解出离场（v1.1：成交日 = 决策日后首个交易日；T+1 自成交日次一交易日起可卖）。

    · 持有窗口 = 自成交日起 window 个**交易日**（成交日 = 第 1 日）；
      交易日身份来自**日历**（v1.3），不再来自"快照日序列"。
    · 交易日已过却无数据 ⇒ `GAP_SESSION_NO_DATA`（**阻塞**）；
      窗口未走满但无缺口 ⇒ `PENDING_WINDOW`（待续，不拿未来数据结算）；
      无日历/越界 ⇒ `UNKNOWN_DAY_STATE`（**阻塞**）。三者严格分开。
    · T+1：成交日当日不可卖 → 止损/目标检查自第 2 日起。
    · 同日双触发 → 止损优先（保守，禁事后择优）。
    · 跳空：低开跌破止损 → 以当日 open 成交（更差）；高开越过目标 → 以 open 成交（更好）。
    · 窗口内均未触发 → 窗口末日收盘价离场；hold_days = 卖出日序号（成交日 = 1）。
    · ★ 补录不变式：窗口末日由日历锚定 ⇒ 事后补录缺口**只补价格、不移动离场日**。
    """
    d0 = v["date"]
    try:
        w = int(v["window"])
    except (TypeError, ValueError):
        raise SettleError("决策 %s 的 window=%r 不是整数，无法机械结算"
                          % (v["id"], v.get("window")))
    if w < 1:
        raise SettleError("决策 %s 的 window=%r < 1（持有窗口至少含成交日）"
                          % (v["id"], v.get("window")))
    stop, tgt = v["stop_line"], v["target"]
    entry = v.get("avg_price") or v["entry_limit"]   # v1.1.1：符号判定基准（成交均价优先）
    if dd is None:
        dd = data_days(snapshot_root)
    ax = _window_axis(d0, as_of, w, calendar, dd)     # ← v1.3 唯一日期轴
    if ax["state"] != "OK":
        out = {"kind": ax["state"], "reason": ax.get("reason")}
        if ax.get("missing"):
            out["missing"] = list(ax["missing"])
        if ax.get("days"):
            out["days"] = list(ax["days"])
        return out
    days = list(ax["days"])                          # 截至 as_of 已有的**交易日**序列
    for i, d in enumerate(days[:w]):
        snap = SNAP.load_snapshot(d, root=snapshot_root)
        if snap is SNAP.UNAVAILABLE:
            return {"kind": "PENDING_DATA", "reason": "窗口内 %s 缺快照" % d}
        row = _quote_of(snap, v["code"])
        if row is None or row.get("low") is None or row.get("high") is None:
            return {"kind": "PENDING_DATA", "reason": "窗口内 %s 缺 %s 行情" % (d, v["code"])}
        low, high, op = row["low"], row["high"], row.get("open")
        if i == 0:
            continue                                 # T+1：成交日买入当日不可卖
        if low <= stop + EPS:                        # 止损优先（同日双触发也走这条）
            px = op if (op is not None and op < stop) else stop
            # v1.1.1：outcome=最终盈亏结果（按符号），退出原因=kind（事件分离）
            oc = "WIN" if px > entry + EPS else ("LOSS" if px < entry - EPS else "FLAT")
            return {"kind": "STOP", "outcome": oc, "exit_price": px, "exit_date": d,
                    "hold_days": i + 1, "day_low": low, "day_high": high}
        if high >= tgt - EPS:
            px = op if (op is not None and op > tgt) else tgt
            oc = "WIN" if px > entry + EPS else ("LOSS" if px < entry - EPS else "FLAT")
            return {"kind": "TARGET", "outcome": oc, "exit_price": px, "exit_date": d,
                    "hold_days": i + 1, "day_low": low, "day_high": high}
    if not ax["complete"]:
        return {"kind": "PENDING_WINDOW", "days": days,
                "reason": ax.get("reason") or
                          "窗口 %d 个交易日（自成交日起），截至 %s 只走过 %d 日"
                          % (w, as_of, len(days))}
    d = days[w - 1]                                  # 窗口末日（日历轴第 w 个交易日）
    snap = _load_snapshot_or_raise(d, snapshot_root)
    row = _quote_of(snap, v["code"])
    if row is None or row.get("close") is None:
        return {"kind": "PENDING_DATA", "reason": "窗口末日 %s 缺收盘价" % d}
    x = row["close"]
    oc = "WIN" if x > entry + EPS else ("LOSS" if x < entry - EPS else "FLAT")
    return {"kind": "WINDOW_END", "outcome": oc, "exit_price": x, "exit_date": d,
            "hold_days": w, "day_low": row.get("low"), "day_high": row.get("high")}


# ── 步4 复盘归因（只读：读回 + 结构断言 + 当日摘要） ────────────────────────────
def _step_attr(as_of, *, base, **_):
    """读回本日结算行 → 断言归因结构齐全 → 产出具可机械统计的当日归因摘要。**零写入**。"""
    rows_all = list(PNL.iter_pnl(False, as_of=as_of, base=base)) + \
        list(PNL.iter_pnl(True, as_of=as_of, base=base))
    # ★ v1.4（2026-10-19）：未成交（NOT_FILLED）是**机械终态**——没有成交价、没有盈亏、
    #   也没有 M/L1L5/三问判定（pnl v1.4 起存 None）。它**无归因可查**，故不进归因校验
    #   与归因统计；单独计数如实报出（不静默吞、也不算「归因不齐」）。
    rows = [r for r in rows_all if r.get("outcome") != "NOT_FILLED"]
    n_not_filled = len(rows_all) - len(rows)
    bad = []
    for r in rows:
        l1 = r.get("attribution_L1L5") or {}
        th = r.get("attribution_三问") or {}
        if set(PNL.L1L5_KEYS) - set(l1):
            bad.append("%s 缺 L1~L5" % r["decision_id"])
        if set(PNL.THREE_KEYS) - set(th):
            bad.append("%s 缺三问归因" % r["decision_id"])
        if [r.get(k) for k in PNL.SLOTS].count(None):
            bad.append("%s 缺 M1/M2/M3 槽位" % r["decision_id"])
    if bad:
        raise SettleError("步4 复盘归因：结算行归因结构不全 → 拒（归因不齐不得算结完）：%s"
                          % " / ".join(bad[:5]))
    outcome_c = {k: 0 for k in PNL.OUTCOMES}
    wrong_l = {k: 0 for k in PNL.L1L5_KEYS}
    wrong_3 = {k: 0 for k in PNL.THREE_KEYS}
    verd = {k: 0 for k in PNL.VERDICTS}
    win_but_wrong = 0
    for r in rows:
        outcome_c[r["outcome"]] = outcome_c.get(r["outcome"], 0) + 1
        for k in PNL.L1L5_KEYS:
            if r["attribution_L1L5"].get(k) == "WRONG":
                wrong_l[k] += 1
        for k in PNL.THREE_KEYS:
            if r["attribution_三问"].get(k) == "WRONG":
                wrong_3[k] += 1
        vs = [r[k]["verdict"] for k in PNL.SLOTS]
        for v in vs:
            verd[v] = verd.get(v, 0) + 1
        if r["outcome"] == "WIN" and all(v == "REFUTE" for v in vs):
            win_but_wrong += 1        # 赢了但三条预测全被否 → 最危险的样本（运气）
    n = len(rows)
    summary = {"as_of": as_of, "n_settled": len(rows_all), "n_attributable": n,
               "n_not_filled": n_not_filled, "outcomes": outcome_c,
               "wrong_by_L": wrong_l, "wrong_by_three": wrong_3, "verdicts": verd,
               "win_but_wrong": win_but_wrong,
               "support_ratio": (verd["SUPPORT"] / float(3 * n)) if n else None}
    # C6 闭环（2026-10-15 接入）：归因摘要齐全后，把本日已结算 BUY 笔落成 Case。
    # 只在有结算行时触发（零结算日零 Case）；落点 = case_builder.cases_path(base)：
    # base 给定 ⇒ base/cases.jsonl（沙盒隔离）；base 缺省 ⇒ 正式 CASES_PATH（生产默认不变）。
    # 幂等：case_builder 按 case_id 去重；失败不阻塞步4（Case 属派生留痕，可次日自愈）。
    case_info = None
    if rows_all:
        # ★ v1.4：有结算行就跑（未成交行会被 case_builder 跳过并计入 skipped_not_filled），
        #   这样「未成交但不进学习样本」是**可见事实**，而不是靠 n==0 恰好不触发。
        try:
            import case_builder as CB
            ci = []
            for axis in (False, True):
                rr = CB.build(as_of, sim=axis, base=base)
                if rr.get("added") or rr.get("skipped_not_filled"):
                    ci.append({"sim": axis, "added": rr["added"],
                               "skipped": rr["skipped"],
                               "skipped_not_filled": rr.get("skipped_not_filled", 0)})
            case_info = ci or [{"added": 0, "note": "本日无 BUY 结算笔 → 零 Case"}]
        except Exception as ex:
            case_info = [{"error": "%s: %s" % (type(ex).__name__, ex)}]
    return _step_result(STEP_ATTR, written=0, reused=0,
                        summary=dict(summary, cases=case_info),
                        notes=["只读归因：口径 = 判断质量 ≠ 盈亏（设计 §327）",
                               "Case 落盘（C6 闭环）：BUY 结算笔 → learning cases（幂等）"])


# ── 步5 学习反馈（最小机械扫 + 留痕；1.6 已落盘 → 阈值真源=params） ──────────────
def _learn_thresholds():
    """步5 缺口阈值唯一真源 = params 三键（0.6 内置默认仅缺档兜底）。

    契约统一批（2026-10-09）兑现本文件头部自诺的「1.6 demand_scanner 落盘后改读
    params」：与 tools/learning/demand_scanner 消费同三键
    （demand_repeat_wrong_min / demand_min_settled / demand_support_floor），
    消除同队列双写者的阈值双真源（内置 1/3 vs params 0.34 的既有分歧）。
    """
    try:
        th = {"repeat_wrong": PARAMS.get_value("demand_repeat_wrong_min"),
              "min_settled": PARAMS.get_value("demand_min_settled"),
              "support_floor": PARAMS.get_value("demand_support_floor")}
        if any(v is None for v in th.values()):
            raise KeyError([k for k, v in th.items() if v is None][0])
        return ({"repeat_wrong": int(th["repeat_wrong"]),
                 "min_settled": int(th["min_settled"]),
                 "support_floor": float(th["support_floor"])}, "params")
    except Exception:                                  # noqa: BLE001 —— 缺档兜底不猜
        return ({"repeat_wrong": DEFAULT_REPEAT_WRONG_THRESHOLD,
                 "min_settled": DEFAULT_MIN_SETTLED_FOR_RATIO,
                 "support_floor": DEFAULT_SUPPORT_RATIO_FLOOR}, "BUILTIN_DEFAULT")


def _step_learn(as_of, *, learning_path, summary, **_):
    """把归因摘要机械扫成「学习缺口」写进学习队列（append-only）；无缺口也留一行痕。

    边界（0.6 步5 契约，契约统一批后口径）：
      输入 = 步4 归因摘要（summary），零额外数据源；
      输出 = learning_path 恰一行（GAP / SCAN_NOOP），与 demand_scanner 行共存同队列；
      触发 = 结算日五步内、游标推进前；
      幂等 = 同日同缺口不重复 append（重复结算不得重复产生学习需求）；
      失败 = 次日重跑自愈（CARRY 语义，游标未推进则整日重结）；
      写边界 = learning_path 由调用方显式注入（生产=真库、测试必指沙盒）；
      行内 scanner=SETTLE_MINIMAL 与 DEMAND_SCANNER 以字段区分（不冒充对方）。
    """
    path = learning_path or LEARNING_PATH
    th, th_src = _learn_thresholds()
    s = summary or {}
    n = s.get("n_settled", 0)
    # ★ v1.4（2026-10-19）：判定质量的**样本口径只用「可归因」笔**（已成交且给了判定）。
    #   NOT_FILLED 没有 M/L1L5/三问，不得把它的行数算进 min_settled 门槛
    #   （否则未成交能把学习阈值「凑满」，等于拿无信息样本触发学习结论）。
    n_attr = s.get("n_attributable", n)
    gaps = []
    if n_attr:
        for k, c in sorted((s.get("wrong_by_L") or {}).items()):
            if c >= th["repeat_wrong"]:
                gaps.append({"kind": "REPEAT_ERROR", "where": "L1L5.%s" % k, "count": c})
        for k, c in sorted((s.get("wrong_by_three") or {}).items()):
            if c >= th["repeat_wrong"]:
                gaps.append({"kind": "REPEAT_ERROR", "where": "三问.%s" % k, "count": c})
        if s.get("win_but_wrong"):
            gaps.append({"kind": "WIN_BUT_WRONG", "where": "M1/M2/M3 全 REFUTE",
                         "count": s["win_but_wrong"]})
        sr = s.get("support_ratio")
        if sr is not None and n_attr >= th["min_settled"] \
                and sr < th["support_floor"]:
            gaps.append({"kind": "JUDGEMENT_QUALITY_LOW", "where": "SUPPORT 占比",
                         "ratio": round(sr, 4)})
    row = {"as_of": as_of, "schema_version": SCHEMA_VERSION,
           "kind": "GAP" if gaps else "SCAN_NOOP", "gaps": gaps, "n_settled": n,
           "n_attributable": n_attr,
           "scanner": "SETTLE_MINIMAL",
           "scanner_note": "1.6 demand_scanner 已落盘（C6-a）→ 0.6 仍为结算侧最小扫，"
                           "与 DEMAND_SCANNER 同队列共存、以 scanner 字段区分",
           "threshold_source": th_src, "params_version": _params_version(),
           "created_at": _now()}
    old = _read_jsonl(path)
    same = [r for r in old if r.get("as_of") == as_of and r.get("kind") == row["kind"]
            and r.get("gaps") == row["gaps"]]
    if same:
        return _step_result(STEP_LEARN, written=0, reused=1, gaps=gaps, learning=path,
                            notes=["同日同结果已留痕 → 幂等不重复 append"])
    _append_jsonl(path, row)
    return _step_result(STEP_LEARN, written=1, reused=0, gaps=gaps, learning=path,
                        notes=["学习队列 append-only；无缺口亦留痕（空转也要可观测）"])


def _params_version():
    try:
        return PARAMS.params_version()
    except Exception:
        return DEC.DEFAULT_PARAMS_VERSION


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    rows = []
    with io.open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                continue
            try:
                rows.append(json.loads(s))
            except ValueError:
                raise SettleError("学习队列第 %d 行不是合法 JSON：%s" % (ln, path))
    return rows


def _append_jsonl(path, rec):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")


# ── 主流程：开日 → 五步（步体成功后打勾）→ 报告 ──────────────────────────────
_STEP_FN = {STEP_MARKET: _step_market, STEP_FILL: _step_fill, STEP_PNL: _step_pnl,
            STEP_ATTR: _step_attr, STEP_LEARN: _step_learn}


def settle(as_of, *, judgements=None, equity=None, base=None, snapshot_root=None,
           raw_dir=None, cursor_path=None, learning_path=None, force_snapshot=False,
           calendar=None):
    """结完 as_of 这一天。返回报告（逐步状态、缺口、账目计数、游标前后值）。

    `judgements` = {decision_id: {"verdicts":…, "attribution_L1L5":…, "attribution_三问":…,
                                  "capital":可选}}；字段名真源 = pnl.py（禁第二套同义字段）。
    `equity` = 账户权益（本金基数来源）；缺 → 已成交笔记 PENDING_CAPITAL。
    `calendar` = 交易日历（`dayclass.Calendar`）。**未注入 ⇒ 交易日身份 UNKNOWN ⇒ 开日即拒**
      （v1.3：缺口不得静默跳过；本模块不内置历法，也不落盘历法）。
    """
    d = _check_date(as_of)
    opened = open_day(d, cursor_path=cursor_path, snapshot_root=snapshot_root,
                      calendar=calendar)
    report = {"as_of": d, "schema_version": SCHEMA_VERSION, "opened": opened,
              "steps": [], "orders_created": 0, "settled": False,
              "cursor_before": opened.get("cursor_before"), "cursor_after": None,
              "pending_steps": [],
              "date_axis": axis_report(calendar, snapshot_root)}
    ctx = {"base": base, "snapshot_root": snapshot_root, "raw_dir": raw_dir,
           "force_snapshot": force_snapshot, "judgements": judgements, "equity": equity,
           "learning_path": learning_path, "summary": None, "calendar": calendar}
    for step in STEPS:
        if CUR.read_cursor(cursor_path)["checklist"][step]:
            report["steps"].append(_step_result(step, status="ALREADY",
                                                notes=["已打勾 → 整步跳过（同日重跑零副作用）"]))
            continue
        res = _STEP_FN[step](d, **ctx)                 # ← 步体先执行
        blocking = _blocking(res.get("pending"))       # ← 只拦「阻塞型」缺口
        if blocking:
            res["status"] = "PENDING"
            res["blocking"] = blocking
            res["notes"] = list(res.get("notes") or []) + [
                "阻塞型缺口 %s —— 本步**不打勾**：游标不推进，缺口补齐后重跑同日即完成"
                % " / ".join(sorted({_reason(p) for p in blocking}))]
        else:
            CUR.mark_step(step, cursor_path)           # ← 无阻塞缺口才算完成（打勾）
            res["status"] = "DONE"
        report["steps"].append(res)
        if step == STEP_ATTR:
            ctx["summary"] = res.get("summary")        # 步5 的机械输入 = 步4 摘要
    cur = CUR.read_cursor(cursor_path)
    report["cursor_after"] = cur["date"]
    report["settled"] = CUR.is_settled(cur)
    report["pending_steps"] = CUR.pending_steps(cur)
    report["checklist"] = dict(cur["checklist"])
    return report


def last_settled(cursor_path=None):
    """最后一个已结账交易日：五步全勾时 = cursor.date；未全勾 → None（折算需交易日历，不猜）。"""
    cur = CUR.read_cursor(cursor_path)
    return cur["date"] if CUR.is_settled(cur) else None


def status(cursor_path=None, base=None):
    """只读总览：游标 + 五步待办 + 账目计数 + 待结算笔数。"""
    cur = CUR.read_cursor(cursor_path)
    pend = CUR.pending_steps(cur)
    counts, awaiting = {}, []
    for sim in (False, True):
        decs = [x for x in DEC.iter_decisions(sim, base=base) if x.get("action") == "BUY"]
        filled = settled = 0
        for x in decs:
            v = DEC.load_decision(x["id"], base=base)
            if v.get("actual_position") is not None:
                filled += 1
                if PNL.read_pnl(x["id"], sim, base=base) is None:
                    awaiting.append({"decision_id": x["id"], "reason": "PENDING"})
            if PNL.read_pnl(x["id"], sim, base=base) is not None:
                settled += 1
        counts[DEC.axis_name(sim)] = {"buy": len(decs), "with_execution": filled,
                                      "settled": settled}
    return {"cursor": cur, "settled": not pend, "pending_steps": pend,
            "open_day": cur["date"], "last_settled": cur["date"] if not pend else None,
            "counts": counts, "awaiting_settlement": awaiting,
            "note": "未结账时 C 的折算需交易日历，本模块不猜（cursor.py 同口径）"}


# ── 静态扫描（纪律自检用） ────────────────────────────────────────────────────
def _prod_source(path=None, name="_selftest"):
    """生产代码区（剔除自检函数整段）——纪律只约束生产代码，测试夹具不受约束。"""
    import ast
    p = path or os.path.abspath(__file__)
    src = io.open(p, encoding="utf-8").read()
    lines = src.splitlines()
    for n in ast.parse(src).body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            for i in ASTW.line_set(n, clamp=len(lines)):
                lines[i - 1] = ""
    return "\n".join(lines)


def _scan_source(path=None):
    """静态扫描本文件：返回 (imported, open_modes, defs, calls_by_fn)。"""
    import ast
    src = io.open(path or os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = ASTW.func_table(tree)         # 函数区间表 = astwin 唯一实现（尾界含多行尾部）
    imported, modes, calls = set(), [], {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        if isinstance(node, ast.FunctionDef):
            calls[node.name] = {(getattr(c.func, "id", None) or getattr(c.func, "attr", None))
                                for c in ast.walk(node) if isinstance(c, ast.Call)} - {None}
        if isinstance(node, ast.Call):
            fn = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if fn == "open":
                m = None
                for a in node.args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        m = a.value
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        m = kw.value.value
                owner = ASTW.owner_of(funcs, node.lineno)
                modes.append((m, node.lineno, owner))
    return imported, modes, [nm for _, _, nm in funcs], calls


# ── 自检（--selftest）：全部在临时目录跑，不碰真实游标/账本 ────────────────────
def _selftest():
    import hashlib
    import shutil
    import tempfile
    import thesis as TH

    ok = fail = 0
    fails = []
    skips = []

    def T(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [OK] %s" % name)
        else:
            fail += 1
            fails.append(name)
            print("  [!!] %s %s" % (name, extra))

    def SK(name, why):
        """显式跳过（**不**计为通过）：外部只读素材未挂载时如实标注。"""
        skips.append(name)
        print("  [--] %s（跳过：%s）" % (name, why))

    def tree_fp(root):
        """快照根内容指纹（相对路径 + 字节数）——证"只读 API 不改盘"。"""
        out = []
        for dp, _dn, fns in os.walk(root):
            for f in sorted(fns):
                p = os.path.join(dp, f)
                out.append((os.path.relpath(p, root), os.path.getsize(p)))
        return sorted(out)

    def raises(fn, *a, **kw):
        try:
            fn(*a, **kw)
            return False
        except (SettleError, CUR.CursorError, SNAP.SnapshotError, DEC.DecisionError,
                PNL.PnlError, TH.ThesisError, TypeError):
            return True

    def fp(p):
        return None if not os.path.exists(p) else \
            hashlib.md5(io.open(p, "rb").read()).hexdigest()

    tmp = tempfile.mkdtemp(prefix="ta_settle_selftest_")
    raw_root = os.path.join(tmp, "raw")

    # ── v1.3 夹具日历：**真实交易日历**（2026-09-01 ~ 12-31 共 82 个交易日） ─────────
    #  来源 = 旧包 data/raw/meta/trade_cal.csv 的**只读**派生字面量（该 CSV 2026 全年
    #  242 个交易日；此处只抄与夹具相关的一段）。**仅存在于自检内**——本模块生产代码
    #  不含任何历法真源、不发现也不落盘历法（历法由调用方经 `calendar=` 注入）。
    #  为什么必须换：v1.2 夹具里有 4 个日期其实是**非交易日**（2026-10-05 国庆、
    #  10-17/11-07 周六、11-08 周日），却把「下一个有数据的日子」当成「下一个交易日」——
    #  那正是被修复的这个缺陷在夹具里的翻版。换真历法后，夹具与生产同轴。
    CAL = DC.Calendar((
        "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08",
        "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16",
        "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24",
        "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09", "2026-10-12",
        "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20",
        "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28",
        "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05",
        "2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13",
        "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23",
        "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27", "2026-11-30", "2026-12-01",
        "2026-12-02", "2026-12-03", "2026-12-04", "2026-12-07", "2026-12-08", "2026-12-09",
        "2026-12-10", "2026-12-11", "2026-12-14", "2026-12-15", "2026-12-16", "2026-12-17",
        "2026-12-18", "2026-12-21", "2026-12-22", "2026-12-23", "2026-12-24", "2026-12-25",
        "2026-12-28", "2026-12-29", "2026-12-30", "2026-12-31",
    ), source="trade_cal.csv·旧包只读派生", version="fixture-sep~dec-2026")

    def put_raw(date, rows):
        """投放区 5 契约文件。rows = [(code, open, high, low, close, pct), ...]"""
        rd = os.path.join(raw_root, date)
        os.makedirs(rd, exist_ok=True)
        dq = ["code,name,open,high,low,close,pct,vol,amount"]
        for c, o, h, lo, cl, pc in rows:
            dq.append("%s,%s,%s,%s,%s,%s,%s,%d,%d" % (c, c, o, h, lo, cl, pc, 1000000, 10000000))
        io.open(os.path.join(rd, "daily_quote.csv"), "w", encoding="utf-8",
                newline="\n").write("\n".join(dq) + "\n")
        for fname, head in (("limit_up.csv", "code,name,boards,first_seal_time,seal_amount,reason"),
                            ("dragon_tiger.csv", "code,name,seat,buy,sell,net")):
            io.open(os.path.join(rd, fname), "w", encoding="utf-8",
                    newline="\n").write(head + "\n")
        io.open(os.path.join(rd, "news.jsonl"), "w", encoding="utf-8",
                newline="\n").write("")
        io.open(os.path.join(rd, "env_metrics.json"), "w", encoding="utf-8", newline="\n").write(
            json.dumps({"limit_up_count": 62, "max_board": 5, "turnover": 1.1e12,
                        "up_down_ratio": 1.6, "fail_rate": 0.18, "index_ma20": 3200.0}))

    def new_env(date0, steps_done=5):
        """独立的 游标 + 快照根 + 学习队列 + 账本根（各簇互不干扰）。"""
        d = tempfile.mkdtemp(prefix="ta_settle_env_", dir=tmp)
        cp = os.path.join(d, "cursor.json")
        io.open(cp, "w", encoding="utf-8", newline="\n").write(json.dumps(
            {"version": 1, "date": date0,
             "checklist": {s: (i < steps_done) for i, s in enumerate(STEPS)}},
            ensure_ascii=False, indent=2) + "\n")
        th = os.path.join(d, "thesis.md")
        TH.init_file(th)
        return {"dir": d, "cursor": cp, "snap": os.path.join(d, "snap"),
                "learn": os.path.join(d, "learning", "demand_queue.jsonl"),
                "base": os.path.join(d, "generated"), "thesis": th}

    def set_cursor(env, date0, steps_done=5):
        io.open(env["cursor"], "w", encoding="utf-8", newline="\n").write(json.dumps(
            {"version": 1, "date": date0,
             "checklist": {s: (i < steps_done) for i, s in enumerate(STEPS)}},
            ensure_ascii=False, indent=2) + "\n")

    def judgement(did, verdicts=("SUPPORT", "SUPPORT", "SUPPORT"), capital=None):
        v = {k: {"prediction_id": k, "metric": "m_%s" % k, "data_source": "snapshot",
                 "verdict": s} for k, s in zip(PNL.SLOTS, verdicts)}
        j = {"verdicts": v, "attribution_L1L5": {k: "OK" for k in PNL.L1L5_KEYS},
             "attribution_三问": {k: "OK" for k in PNL.THREE_KEYS}}
        if capital is not None:
            j["capital"] = capital
        return j

    def pred(slot):
        return {"prediction_id": slot, "metric": "m_%s" % slot, "data_source": "snapshot",
                "baseline": "T 日收盘", "direction": "UP", "threshold": 3.0,
                "condition": "T+3 累计涨幅 ≥3%", "window": "3D"}

    FIVE = {"驱动存在": "板块级订单落地", "当前证据": "公告 + 资金同步流入",
            "延续机制": "订单排产 → 业绩预期", "失效条件": "公告被澄清", "剩余预期": "3 日内扩散"}
    ENV = {"regime": "强势", "signals": {"limit_up_count": 62}, "leg": "INTRADAY",
           "source": "EXPLICIT"}

    def mk_buy(env, date, code, entry, stop, tgt, window=3, req=0.2, sim=False,
               plan=None, omit_plan=False):
        tid = TH.create_thesis(date, "A", "驱动：%s" % code, code=code,
                               path=env["thesis"])["id"]
        chain = {"evidence": ["公告"], "inference": ["催化 → A 类"],
                 "prediction": [pred(s) for s in PNL.SLOTS], "five": dict(FIVE)}
        d = DEC.write_decision(date, code, "BUY", tid, chain, sim=sim, base=env["base"],
                               thesis_path=env["thesis"], driver="A", entry_limit=entry,
                               stop_line=stop, target=tgt, window=window,
                               position_request=req, env=ENV)
        # v1.1.2：夹具显式落 RISK_PLAN（旧夹具依赖 settle 的 request 回退旁路，已封堵）。
        if not omit_plan:
            DEC.append_risk_plan(d["id"], req if plan is None else plan,
                                 as_of=date, base=env["base"], note="自检夹具 C5 裁决")
        return d

    def run(env, date, judgements=None, equity=1e6, **kw):
        return settle(date, base=env["base"], snapshot_root=env["snap"],
                      raw_dir=os.path.join(raw_root, date), cursor_path=env["cursor"],
                      learning_path=env["learn"], judgements=judgements, equity=equity,
                      calendar=CAL, **kw)

    def seed(env, *dates, row=("sh600300", 10.0, 10.1, 9.9, 10.05, 0.5)):
        """只把若干天的**快照**落盘（不经结算）—— 用于精确构造"数据日集合"。"""
        for d in dates:
            put_raw(d, [row])
            SNAP.ingest_snapshot(d, raw_dir=os.path.join(raw_root, d), root=env["snap"])

    def no_cal(env, date, judgements=None, **kw):
        """不注入日历地结账（测 fail-closed）。"""
        return settle(date, base=env["base"], snapshot_root=env["snap"],
                      raw_dir=os.path.join(raw_root, date), cursor_path=env["cursor"],
                      learning_path=env["learn"], judgements=judgements, equity=1e6, **kw)

    real_cursor = copy.deepcopy(CUR.read_cursor())
    real_files = (fp(DEC.ledger_path(False)), fp(DEC.ledger_path(True)),
                  fp(PNL.ledger_path(False)), fp(PNL.ledger_path(True)), fp(LEARNING_PATH))
    try:
        print("== A. 契约：步名/顺序/字段单一真源 ==")
        T("A1 步名与顺序 = cursor.STEPS（禁第二套字面量）",
          STEPS == CUR.STEPS
          and STEPS == ("行情落定", "成交记录", "盈亏结算", "复盘归因", "学习反馈"))
        T("A2 五步函数表与步名一一对应（无多余/缺失）",
          tuple(_STEP_FN) == STEPS and len(_STEP_FN) == 5)
        T("A3 结算层不自造字段：M1/M2/M3 槽位与 L1~L5 三问归因沿用 pnl 真源",
          DEC.PRED_SLOTS == PNL.SLOTS and PNL.L1L5_KEYS == ("L1", "L2", "L3", "L4", "L5")
          and PNL.THREE_KEYS == ("驱动", "赔率", "证伪"))
        e = new_env("2026-09-30", 5)
        # v1.3：原夹具用 2026-10-05，但它在**真实日历上是国庆休市日**；改用 10-08（真交易日）。
        put_raw("2026-10-08", [("sh600000", 10.0, 10.6, 9.9, 10.4, 4.0)])
        T("A4 报告恒含 orders_created=0（结算只记账、不产生交易动作）",
          run(e, "2026-10-08")["orders_created"] == 0
          and run(e, "2026-10-08")["date_axis"]["calendar"] == "PROVIDED")

        print("== B. 开日与游标推进权（原子性核心） ==")
        e = new_env("2026-09-30", 3)                      # 上一日只走 3 步
        put_raw("2026-10-08", [("sh600000", 10.0, 10.6, 9.9, 10.4, 4.0)])
        T("B1 上一日五步未满 → 开新日被拒（date 不动）",
          raises(open_day, "2026-10-08", cursor_path=e["cursor"], snapshot_root=e["snap"],
                 calendar=CAL)
          and CUR.read_cursor(e["cursor"])["date"] == "2026-09-30")
        set_cursor(e, "2026-09-30", 4)                    # 只差最后一步
        T("B2 四步全勾 → 整日结账被拒：checklist 不清零、五步仍在",
          raises(run, e, "2026-10-08")
          and CUR.read_cursor(e["cursor"])["date"] == "2026-09-30"
          and CUR.pending_steps(CUR.read_cursor(e["cursor"])) == [STEPS[4]])
        T("B3 拒绝发生在游标层（CursorError），非静默跳过",
          raises(CUR.advance_cursor, "2026-10-08", e["cursor"]))
        set_cursor(e, "2026-09-30", 5)
        od = open_day("2026-10-08", cursor_path=e["cursor"], snapshot_root=e["snap"],
                      calendar=CAL)
        T("B4 五步全勾 → 开日放行：date 前移 + checklist 清零（同一次原子落盘）",
          od["opened"] is True and CUR.read_cursor(e["cursor"])["date"] == "2026-10-08"
          and not any(CUR.read_cursor(e["cursor"])["checklist"].values()))
        T("B5 重复开同一日 → 幂等不二次推进、不清已打的勾",
          open_day("2026-10-08", cursor_path=e["cursor"], snapshot_root=e["snap"],
                   calendar=CAL)["reason"] == "ALREADY_OPENED")
        T("B6 禁倒退（请求早于游标 → 拒）",
          raises(open_day, "2026-09-30", cursor_path=e["cursor"], snapshot_root=e["snap"],
                 calendar=CAL))

        print("== C. 步1 行情落定 · 数据缺口不吞 ==")
        e = new_env("2026-10-08", 0)
        T("C1 无投放区 → 抛错（缺口不吞），该步不打勾、整日未 settled",
          raises(settle, "2026-10-08", base=e["base"], snapshot_root=e["snap"],
                 raw_dir=os.path.join(tmp, "no_raw"), cursor_path=e["cursor"],
                 learning_path=e["learn"], calendar=CAL)
          and CUR.read_cursor(e["cursor"])["checklist"][STEPS[0]] is False
          and last_settled(e["cursor"]) is None)
        put_raw("2026-10-08", [("sh600000", 10.0, 10.6, 9.9, 10.4, 4.0)])
        rep = run(e, "2026-10-08")
        T("C2 数据到位后重跑：步1 DONE 且快照目录恰 5 契约文件 + 1 准入判决 sidecar",
          rep["steps"][0]["status"] == "DONE"
          and rep["steps"][0]["snapshot_status"] == "WRITTEN"
          and len(os.listdir(os.path.join(e["snap"], "2026-10-08"))) == 6)
        T("C3 无 BUY 决策 → 全五步仍结完（空转留痕，各步产出可为空）",
          rep["settled"] is True and [s["status"] for s in rep["steps"]] == ["DONE"] * 5
          and rep["steps"][1]["written"] == 0 and rep["steps"][2]["written"] == 0)
        T("C4 重跑同日 → 五步全 ALREADY（整步跳过，步体不再执行）",
          [s["status"] for s in run(e, "2026-10-08")["steps"]] == ["ALREADY"] * 5)

        print("== D. 步2 成交记录（0.6 可成交性口径 · v1.1 次日开盘成交） ==")
        e = new_env("2026-09-30", 5)
        put_raw("2026-10-08", [("sh600000", 10.0, 10.6, 9.9, 10.4, 4.0),
                               ("sh600001", 11.0, 11.0, 11.0, 11.0, 10.0),
                               ("sh600002", 12.0, 12.9, 12.4, 12.8, 6.0),
                               ("sh600003", 13.0, 13.5, 12.9, 13.4, 5.0),
                               ("sh600004", 12.0, 12.5, 11.9, 12.2, 1.0)])
        a = mk_buy(e, "2026-10-08", "sh600000", 10.2, 9.7, 11.0)
        b = mk_buy(e, "2026-10-08", "sh600001", 11.0, 10.5, 11.8)
        c = mk_buy(e, "2026-10-08", "sh600002", 12.0, 11.5, 13.0)
        c2 = mk_buy(e, "2026-10-08", "sh600003", 13.2, 12.5, 14.0)
        c3 = mk_buy(e, "2026-10-08", "sh600004", 12.0, 11.5, 13.0)
        d = mk_buy(e, "2026-10-08", "sh600009", 9.0, 8.5, 10.0)     # 成交日无此 code 行情
        rep0 = run(e, "2026-10-08")
        T("D0 决策日结算：成交日快照未到 → EXECUTION_NOT_DUE 待续（步照常打勾，游标推进）",
          rep0["settled"] is True and CUR.read_cursor(e["cursor"])["date"] == "2026-10-08"
          and {"EXECUTION_NOT_DUE"} == {_reason(p)
                                        for p in rep0["steps"][1]["pending"]},
          rep0["steps"][1])
        # 成交日 2026-10-09：600000 开盘 10.2 ≤ 限价 → @open 成交；600001 一字板；
        # 600002 开盘 14.08 = 涨停价(12.8×1.10) → 排板不保证；600003 开盘 13.5 > 限价 13.2
        # → 不追价；600004 开盘 12.5 > 限价 12.0 → 不追价；600009 缺行情（阻塞缺口）
        put_raw("2026-10-09", [("sh600000", 10.2, 10.6, 9.9, 10.4, 0.0),
                               ("sh600001", 11.0, 11.0, 11.0, 11.0, 0.0),
                               ("sh600002", 14.08, 14.08, 13.9, 14.0, 9.4),
                               ("sh600003", 13.5, 13.6, 13.3, 13.4, 0.0),
                               ("sh600004", 12.5, 12.6, 12.3, 12.4, 1.6)])
        rep = run(e, "2026-10-09")
        fills = {f["decision_id"]: f for f in rep["steps"][1]["fills"]}
        T("D1 成交日开盘 ≤ 限价 → filled，实际成交价 = 开盘价（非决策日参考价）",
          fills[a["id"]]["result"] == "filled" and fills[a["id"]]["fill_price"] == 10.2
          and DEC.load_decision(a["id"], base=e["base"])["actual_position"] == 0.2
          and DEC.load_decision(a["id"], base=e["base"])["avg_price"] == 10.2)
        T("D2 一字板 → 未排到判未成交（未在场，actual=0）",
          fills[b["id"]]["result"] == "unfilled" and fills[b["id"]]["one_board"] is True
          and DEC.load_decision(b["id"], base=e["base"])["actual_position"] == 0.0)
        T("D3 开盘价触及涨停价 → 排板不保证成交（未在场）",
          fills[c["id"]]["result"] == "unfilled" and fills[c["id"]]["limit_open"] is True
          and "涨停" in fills[c["id"]]["reason"])
        T("D3b 开盘价高于限价 → 不追价不成交（放弃，不假装买到）",
          fills[c2["id"]]["result"] == "unfilled"
          and "不追价" in fills[c2["id"]]["reason"]
          and fills[c3["id"]]["result"] == "unfilled")
        T("D4 成交日该 code 无行情 → 记 pending NO_QUOTE，不判成交（缺就是缺）",
          any(p["decision_id"] == d["id"] and p["reason"] == "NO_QUOTE"
              for p in rep["steps"][1]["pending"])
          and DEC.load_decision(d["id"], base=e["base"])["actual_position"] is None)
        T("D5 成交推定口径写进 EXECUTION.note（含成交日与开盘价，未改 schema）",
          FILL_NOTE_PREFIX in (DEC.load_decision(a["id"], base=e["base"])["exec_note"] or "")
          and "2026-10-09" in (DEC.load_decision(a["id"], base=e["base"])["exec_note"] or ""))
        n_rows = len(DEC.read_ledger(False, base=e["base"]))
        # 600009 成交日缺行情 = 阻塞型缺口 → 步2 不打勾 → 整日未 settled、游标不得前移
        cur_after = CUR.read_cursor(e["cursor"])
        T("D6 阻塞型缺口（NO_QUOTE）→ 该步 PENDING 不打勾：游标未推进、整日未 settled",
          rep["steps"][1]["status"] == "PENDING"
          and "NO_QUOTE" in {_reason(p) for p in rep["steps"][1].get("blocking") or []}
          and rep["settled"] is False and STEPS[1] in rep["pending_steps"]
          and cur_after["checklist"][STEPS[1]] is False
          and raises(CUR.advance_cursor, "2026-10-10", e["cursor"]),
          "步2=%s settled=%s pending_steps=%s" % (rep["steps"][1]["status"], rep["settled"],
                                                 rep["pending_steps"]))
        done_before = {s["step"] for s in rep["steps"] if s["status"] == "DONE"}
        pend_before = {s["step"] for s in rep["steps"] if s["status"] == "PENDING"}
        rep2 = run(e, "2026-10-09")
        st2 = {s["step"]: s["status"] for s in rep2["steps"]}
        T("D7 同日重跑 → 已完成步整步跳过（ALREADY）、阻塞步仍 PENDING，账本零新增（幂等）",
          all(st2[s] == "ALREADY" for s in done_before)
          and all(st2[s] == "PENDING" for s in pend_before)
          and len(DEC.read_ledger(False, base=e["base"])) == n_rows,
          "重跑状态=%s（已完成=%s 阻塞=%s）" % (st2, sorted(done_before), sorted(pend_before)))
        put_raw("2026-10-09", [("sh600000", 10.2, 10.6, 9.9, 10.4, 0.0),
                               ("sh600001", 11.0, 11.0, 11.0, 11.0, 0.0),
                               ("sh600002", 14.08, 14.08, 13.9, 14.0, 9.4),
                               ("sh600003", 13.5, 13.6, 13.3, 13.4, 0.0),
                               ("sh600004", 12.5, 12.6, 12.3, 12.4, 1.6),
                               ("sh600009", 9.0, 9.4, 8.8, 9.2, 1.0)])   # 补齐缺口
        # 步1 已打勾 → 同日不会重采快照；补齐缺口的正确动作是「重投放 + force 重采」
        SNAP.ingest_snapshot("2026-10-09", raw_dir=os.path.join(raw_root, "2026-10-09"),
                             root=e["snap"], force=True)
        jj = {x["id"]: judgement(x["id"]) for x in (a, b, c, c2, c3, d)}
        rep3 = run(e, "2026-10-09", jj)
        st3 = [s["status"] for s in rep3["steps"]]
        # 决策账本是**事件行日志**（EXECUTION 是合法追加行）→ 幂等基准只能是「再跑一次零新增」，
        # 不能拿首轮行数比（补齐缺口那一轮必然要追加执行事件）。
        n_after_fill = len(DEC.read_ledger(False, base=e["base"]))
        rep4 = run(e, "2026-10-09", jj)
        T("D8 缺口补齐后同日重跑 → 阻塞步转 DONE、五步全勾、游标方可推进（缺口不被吞）",
          st3 == ["ALREADY", "DONE", "DONE", "ALREADY", "ALREADY"]
          and rep3["settled"] is True and rep3["pending_steps"] == []
          and CUR.is_settled(CUR.read_cursor(e["cursor"])) is True
          and [s["status"] for s in rep4["steps"]] == ["ALREADY"] * 5
          and len(DEC.read_ledger(False, base=e["base"])) == n_after_fill,
          "状态=%s settled=%s pending_steps=%s 再跑=%s" % (
              st3, rep3["settled"], rep3["pending_steps"],
              [s["status"] for s in rep4["steps"]]))
        T("D9 待续型缺口（PENDING_WINDOW）不打成阻塞：步3 照常打勾（未到期 ≠ 缺口）",
          rep3["steps"][2]["status"] == "DONE"
          and {_reason(p) for p in rep3["steps"][2]["pending"]} == {"PENDING_WINDOW"},
          "步3=%s pending=%s" % (rep3["steps"][2]["status"],
                                sorted({_reason(p) for p in rep3["steps"][2]["pending"]})))

        print("== E. 步3 盈亏结算（机械派生 + 判定注入 · v1.1 次日开盘成交 + T+1） ==")
        e = new_env("2026-09-30", 5)
        put_raw("2026-10-08", [("sh600000", 10.0, 10.6, 9.9, 10.4, 4.0)])
        put_raw("2026-10-09", [("sh600000", 10.2, 10.4, 9.9, 10.1, -2.9)])  # 成交日：未触发
        # v1.3：T+1 首卖日 = 成交日（10-09 周五）之后的**第一个交易日** = 10-12（周一）。
        # （原夹具写 10-10，但 10-10 是周六 ⇒ 真实日历上不存在该交易日；见 §I 组 I3/I9）
        put_raw("2026-10-12", [("sh600000", 10.0, 10.1, 9.7, 9.7, -4.0)])   # T+1 首卖日：跌破止损 9.8
        s1 = mk_buy(e, "2026-10-08", "sh600000", 10.2, 9.8, 11.0, window=3)
        rep = run(e, "2026-10-08")                     # 决策日：成交待续（EXECUTION_NOT_DUE）
        rep = run(e, "2026-10-09")                     # 成交日：已成交，判定未注入
        T("E1 未注入判定 → PENDING_JUDGEMENT 且不写行（不造数）",
          any(p["reason"] == "PENDING_JUDGEMENT" for p in rep["steps"][2]["pending"])
          and PNL.read_pnl(s1["id"], False, base=e["base"]) is None)
        rep = run(e, "2026-10-09", {s1["id"]: judgement(s1["id"])})
        T("E2 窗口未走完 → PENDING_WINDOW，不拿未来数据结算（T+1：成交日次日起才可卖）",
          any(p["reason"] == "PENDING_WINDOW" for p in rep["steps"][2]["pending"])
          and PNL.read_pnl(s1["id"], False, base=e["base"]) is None)
        rep = run(e, "2026-10-12", {s1["id"]: judgement(s1["id"],
                                                        ("REFUTE", "REFUTE", "REFUTE"))})
        row = PNL.read_pnl(s1["id"], False, base=e["base"])
        T("E3 止损触发 → LOSS，exit=stop 9.8；entry=成交日 open 10.2；capital=req×equity"
          "（v1.1.1：exit_kind=STOP · exit_date 落账；v1.3：离场日 = 日历第 2 个交易日 10-12）",
          row is not None and row["outcome"] == "LOSS" and row["exit_price"] == 9.8
          and row["entry_price"] == 10.2
          and row["exit_kind"] == "STOP" and row["exit_date"] == "2026-10-12"
          and row["capital"] == 200000.0 and row["hold_days"] == 2
          and abs(row["pnl_amount"] - 200000.0 * (9.8 - 10.2) / 10.2 * 100 / 100.0) < 0.01
          and row["settle_date"] == "2026-10-12")
        # v1.2 成本建模：fee 按正式费率参数（买侧 capital×rate + 卖侧卖出额×rate）
        exp_fee = (row["capital"] * PARAMS.get_value("cost_buy_rate")
                   + row["capital"] * (row["exit_price"] / row["entry_price"])
                   * PARAMS.get_value("cost_sell_rate"))
        T("E3b v1.2 费用入账：fee=买侧+卖侧（正式费率参数）、net=毛−费、毛盈亏不变",
          row["fee_amount"] > 0
          and abs(row["fee_amount"] - exp_fee) < 1e-6
          and abs(row["net_amount"] - (row["pnl_amount"] - row["fee_amount"])) < 1e-6
          and abs(row["pnl_amount"] - 200000.0 * (9.8 - 10.2) / 10.2 * 100 / 100.0) < 0.01)
        T("E4 判定按注入落库且三槽位不合并",
          all(row[k]["verdict"] == "REFUTE" for k in PNL.SLOTS))
        T("E5 止损/目标同日双触发 → 止损优先（保守，禁事后择优）",
          _dual_case(new_env, put_raw, mk_buy, run, judgement))
        T("E6 跳空低开跌破止损 → 以当日 open 成交（不假装按 stop 成交）",
          _gap_case(new_env, put_raw, mk_buy, run, judgement))
        T("E7 目标触发 → WIN，exit=target，hold_days 按交易日数计",
          _target_case(new_env, put_raw, mk_buy, run, judgement))
        T("E8 窗口内未触发 → 以窗口末日收盘价离场（WINDOW_END）",
          _window_end_case(new_env, put_raw, mk_buy, run, judgement))
        T("E9 equity 缺且未注入 capital → PENDING_CAPITAL（没有本金基数不许编）",
          _no_capital_case(new_env, put_raw, mk_buy, run, judgement))
        T("E10 显式注入 capital 优先于 equity 推算（与手算逐笔对齐）",
          _capital_override_case(new_env, put_raw, mk_buy, run, judgement))
        T("E11 已结算再跑同日 → 整步跳过、结算行数不变（幂等）",
          len(PNL.iter_pnl(False, base=e["base"])) == 1
          and run(e, "2026-10-12", {s1["id"]: judgement(s1["id"])})["steps"][2]["status"]
          == "ALREADY")
        T("E12 v1.1.1 止损触发但卖出价高于便宜入场 → outcome=WIN + exit_kind=STOP"
          "（事件与结果分离；v1.1 旧语义误标 LOSS 配正 pct）",
          _stop_win_case(new_env, put_raw, mk_buy, run, judgement))
        # v1.1.2：request 回退旁路封堵（独立环境，不污染后续用例游标）
        e2 = new_env("2026-10-13", 5)
        put_raw("2026-10-14", [("sh600130", 10.0, 10.2, 9.9, 10.1, 1.0)])
        put_raw("2026-10-15", [("sh600130", 10.0, 10.2, 9.9, 10.0, -1.0)])
        d_np = mk_buy(e2, "2026-10-14", "sh600130", 10.05, 9.5, 11.0,
                      window=3, omit_plan=True)
        run(e2, "2026-10-14", {d_np["id"]: judgement(d_np["id"])})
        r_np = run(e2, "2026-10-15", {d_np["id"]: judgement(d_np["id"])})
        ex_np = [x for x in r_np["steps"][1]["fills"]
                 if x["decision_id"] == d_np["id"]]
        row_np = DEC.load_decision(d_np["id"], base=e2["base"])
        T("E13 无 RISK_PLAN（未经 C5 审批）→ unfilled，不按 request 回退成交",
          ex_np and ex_np[0]["result"] == "unfilled"
          and "无 RISK_PLAN" in (ex_np[0]["reason"] or "")
          and row_np["actual_position"] == 0)
        r_np2 = run(e2, "2026-10-15", {d_np["id"]: judgement(d_np["id"])})
        exec_cnt = sum(1 for r in DEC.read_ledger(False, base=e2["base"])
                       if r.get("decision_id") == d_np["id"]
                       and r.get("record_type") == DEC.REC_EXECUTION)
        T("E14 拒绝后重跑：整步 ALREADY（同日重跑零副作用）、EXECUTION 仍恰 1 条且 actual=0",
          r_np2["steps"][1]["status"] == "ALREADY" and exec_cnt == 1
          and DEC.load_decision(d_np["id"], base=e2["base"])["actual_position"] == 0)

        print("== F. 步4 归因摘要 / 步5 学习留痕 ==")
        T("F1 步4 只读：产出归因摘要（outcome/错误环/判定分布）零写入",
          rep["steps"][3]["written"] == 0
          and set(rep["steps"][3]["summary"]["outcomes"]) == set(PNL.OUTCOMES)
          and "wrong_by_L" in rep["steps"][3]["summary"])
        wbw = _win_but_wrong_case(new_env, put_raw, mk_buy, run, judgement)
        T("F2 步4 抓「赢了但三槽位全 REFUTE」的危险样本（判断质量 ≠ 盈亏）",
          wbw["win_but_wrong"] >= 1)
        T("F3 步5 写出学习留痕一行：scanner=SETTLE_MINIMAL（与 DEMAND_SCANNER 同队列区分）"
          "+ 阈值真源=params（缺档才兜底 BUILTIN_DEFAULT）",
          rep["steps"][4]["written"] == 1 and os.path.exists(e["learn"])
          and _read_jsonl(e["learn"])[-1]["scanner"] == "SETTLE_MINIMAL"
          and _read_jsonl(e["learn"])[-1]["threshold_source"] == "params")
        T("F4 步5 缺口机械扫：三槽位全 REFUTE 的赢单 → 记 WIN_BUT_WRONG",
          "WIN_BUT_WRONG" in wbw["gap_kinds"])
        n_learn = len(_read_jsonl(e["learn"]))
        run(e, "2026-10-12", {s1["id"]: judgement(s1["id"])})
        T("F5 同日重跑 → 学习队列不重复 append（幂等）", len(_read_jsonl(e["learn"])) == n_learn)

        print("== N. NOT_FILLED 机械终态（v1.4 · 2026-10-19：未成交不依赖 judgements）==")
        n1 = new_env("2026-11-02", 5)
        put_raw("2026-11-03", [("sh603000", 10.0, 10.3, 9.9, 10.2, 2.0)])
        # 成交日一字板：open=high=low=close 且涨幅到顶 ⇒ 排不到，机械判未成交
        put_raw("2026-11-04", [("sh603000", 11.22, 11.22, 11.22, 11.22, 10.0)])
        nb = mk_buy(n1, "2026-11-03", "sh603000", 10.2, 9.8, 11.5, window=3)
        run(n1, "2026-11-03")                  # 决策日
        rn = run(n1, "2026-11-04")             # 成交日：一字板 ⇒ EXECUTION unfilled
        pv = DEC.load_decision(nb["id"], base=n1["base"])
        prow = PNL.read_pnl(nb["id"], False, base=n1["base"])
        T("N1 成交事实机械确认：EXECUTION actual_position=0（未成交）",
          pv["actual_position"] == 0, "actual=%r" % (pv["actual_position"],))
        T("N2 ★ 未注入 judgements 也能落 NOT_FILLED 终态（不再卡 PENDING_JUDGEMENT）",
          prow is not None and prow["outcome"] == "NOT_FILLED",
          "pnl=%s ｜ 步3=%s pending=%s"
          % ("None" if prow is None else prow["outcome"],
             rn["steps"][2]["status"], rn["steps"][2].get("pending")))
        T("N3 ★ 未成交不产生虚假盈亏：capital=0 / pnl_amount=0 / net=0 / 价与退出日全 null",
          prow and prow["capital"] == 0 and prow["pnl_amount"] == 0
          and prow["net_amount"] == 0 and prow["entry_price"] is None
          and prow["exit_price"] is None and prow["exit_date"] is None
          and prow["pnl_pct"] == 0,
          json.dumps({k: prow[k] for k in ("capital", "pnl_amount", "net_amount",
                                           "entry_price", "exit_price", "exit_date")},
                     ensure_ascii=False) if prow else "None")
        T("N4 ★ 未成交不产生判定：M1/M2/M3 与两份归因均为 None（无占位值）",
          prow and all(prow.get(k) is None for k in PNL.SLOTS)
          and prow.get("attribution_L1L5") is None
          and prow.get("attribution_三问") is None)
        sm = PNL.summary(False, base=n1["base"])
        T("N5 ★ 未成交不计入胜率：filled=0、win_rate=None、not_filled=1",
          sm["filled"] == 0 and sm["win_rate"] is None and sm["not_filled"] == 1,
          json.dumps({k: sm[k] for k in ("n", "filled", "win_rate", "not_filled")},
                     ensure_ascii=False))
        at = rn["steps"][3]["summary"]
        T("N6 步4 归因摘要：未成交单独计数（n_not_filled=1）且不因缺归因而拒",
          at.get("n_not_filled") == 1 and at.get("n_attributable") == 0,
          json.dumps({k: at.get(k) for k in ("n_settled", "n_attributable",
                                             "n_not_filled")}, ensure_ascii=False))
        T("N7 ★ 学习样本不掺未成交：cases 零新增、skipped_not_filled=1",
          (at.get("cases") or [{}])[0].get("added", 0) == 0
          and (at.get("cases") or [{}])[0].get("skipped_not_filled", 0) >= 1
          and not os.path.isfile(os.path.join(n1["base"], "cases.jsonl")),
          json.dumps(at.get("cases"), ensure_ascii=False)[:200])
        n_row = len([1 for r in _read_jsonl(PNL.ledger_path(False, n1["base"]))])
        rn2 = run(n1, "2026-11-05", None)
        n_row2 = len([1 for r in _read_jsonl(PNL.ledger_path(False, n1["base"]))])
        T("N8 幂等：再跑不新增 pnl 行（append-only，不改写已结算）",
          n_row == n_row2 == 1, "%d → %d" % (n_row, n_row2))
        def _live_read():
            return PNL.read_pnl(nb["id"], True, base=n1["base"])
        T("N9 SIM/LIVE 分轴不被绕过：按 LIVE 轴读 SIM 结算笔 ⇒ AXIS_MISMATCH 拒绝；"
          "LIVE 账本文件不存在",
          raises(_live_read) and not os.path.exists(PNL.ledger_path(True, n1["base"])))
        _lrows = [x for x in _read_jsonl(n1["learn"]) if x.get("as_of") == "2026-11-04"]
        lr = _lrows[-1] if _lrows else {}
        T("N10 学习留痕：n_settled=1 但 n_attributable=0（未成交不充判定质量样本）",
          lr.get("n_settled") == 1 and lr.get("n_attributable") == 0,
          json.dumps({k: lr.get(k) for k in ("as_of", "n_settled", "n_attributable",
                                             "gaps")}, ensure_ascii=False))

        print("== G. 端到端：逐日追平 + 字节级幂等 + 防跳日 ==")
        e = new_env("2026-11-30", 5)
        put_raw("2026-12-01", [("sh601000", 10.0, 10.5, 9.9, 10.3, 3.0)])
        put_raw("2026-12-02", [("sh601000", 10.1, 10.6, 10.0, 10.4, 1.0)])   # 成交日 @10.1
        put_raw("2026-12-03", [("sh601000", 10.4, 11.3, 10.3, 11.1, 7.0)])
        g1 = mk_buy(e, "2026-12-01", "sh601000", 10.2, 9.6, 11.0, window=3)
        jj = {g1["id"]: judgement(g1["id"])}
        r1 = run(e, "2026-12-01", jj)
        T("G1 settle(D1) → 五步全 DONE + settled=True + cursor.date=D1",
          [s["status"] for s in r1["steps"]] == ["DONE"] * 5 and r1["settled"] is True
          and r1["cursor_after"] == "2026-12-01" and last_settled(e["cursor"]) == "2026-12-01")
        r2 = run(e, "2026-12-02", jj)
        r3 = run(e, "2026-12-03", jj)
        T("G2 逐日追平 D1→D2→D3：游标逐日前移、每日五步全勾（0.14 唤醒即追平的前置）",
          last_settled(e["cursor"]) == "2026-12-03" and r2["cursor_before"] == "2026-12-01"
          and r3["cursor_before"] == "2026-12-02" and r3["settled"] is True)
        rows = PNL.iter_pnl(False, base=e["base"])
        T("G3 终态：结算行 M1/M2/M3 齐全且金额 == capital×pct/100（机械可复算）",
          rows and all(abs(r["pnl_amount"] - r["capital"] * r["pnl_pct"] / 100.0) < 1e-4
                       and all(r[k] is not None for k in PNL.SLOTS) for r in rows))
        before = (io.open(DEC.ledger_path(False, e["base"]), "rb").read(),
                  io.open(PNL.ledger_path(False, e["base"]), "rb").read(),
                  io.open(e["learn"], "rb").read(),
                  io.open(e["cursor"], "rb").read())
        run(e, "2026-12-03", jj)
        after = (io.open(DEC.ledger_path(False, e["base"]), "rb").read(),
                 io.open(PNL.ledger_path(False, e["base"]), "rb").read(),
                 io.open(e["learn"], "rb").read(),
                 io.open(e["cursor"], "rb").read())
        T("G4 全量重跑同日 → 决策账本/pnl/学习队列/游标 逐字节未变（幂等最强形式）",
          before == after)
        T("G5 防跳日：中间有已采集未结账的交易日 → SKIP_SETTLEMENT 拒",
          _skip_case(new_env, put_raw, run, raises, CAL))

        print("== I. 日期轴双判：交易日（日历）× 数据（快照）· 缺口阻断（v1.3 修复项） ==")

        def _real_cal_csv():
            """真实日历 CSV 路径：**复用** dayclass 既有的只读发现规则（不自造第二套路径）。"""
            f = getattr(DC, "_opt_real_cal", None)
            return f() if callable(f) else None

        _rc = _real_cal_csv()
        if _rc:
            _real = DC.Calendar.from_seed_csv(_rc, source="旧包trade_cal.csv")
            _seg = [d for d in _real.sessions if "2026-09-01" <= d <= "2026-12-31"]
            T("I0 夹具日历 == 真实日历（2026-09-01~12-31 逐日相等；跨国庆长假与周末）",
              _seg == list(CAL.sessions),
              "夹具 %d 日 vs 真实 %d 日" % (len(CAL.sessions), len(_seg)))
        else:
            SK("I0 夹具日历 == 真实日历（2026-09-01~12-31）", "旧包 trade_cal.csv 未挂载")

        # —— I1 完整窗口：窗口末日必须按**交易日**算 ——
        e = new_env("2026-09-30", 5)
        _oct = ("2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14")
        for _d in _oct:
            put_raw(_d, [("sh600300", 10.0, 10.1, 9.9, 10.05, 0.5)])
        d1 = mk_buy(e, "2026-09-30", "sh600300", 10.2, 9.5, 11.0, window=5)
        j1 = {d1["id"]: judgement(d1["id"])}
        for _d in _oct:
            rep1 = run(e, _d, j1)
        r1 = PNL.read_pnl(d1["id"], False, base=e["base"])
        T("I1 完整窗口（真实日历）：决策 09-30 + w=5 ⇒ 成交日 10-08、窗口末日 10-14；"
          "跨国庆 7 日休市，末日**不**被周末近似算成 10-10/10-13",
          r1 is not None and r1["exit_kind"] == "WINDOW_END"
          and r1["exit_date"] == "2026-10-14" and r1["hold_days"] == 5
          and r1["entry_price"] == 10.0
          and rep1["date_axis"]["calendar"] == "PROVIDED")
        _w1 = _window_axis("2026-09-30", "2026-10-14", 5, CAL, data_days(e["snap"]))
        T("I1b 窗口 = 日历交易日序列（10-08/09/12/13/14）；不含休市 10-01~10-07 与周末 10-10/11",
          _w1["state"] == "OK" and tuple(_w1["days"]) == _oct and _w1["complete"] is True)

        # —— I2 缺失交易日快照 ⇒ 阻塞（不是 PENDING_WINDOW） ——
        # 真实交易日序列（自成交日 10-09 起）：10-09(1) 10-12(2) 10-13(3) ⇒ 窗口末日应为 10-13。
        # 场景 = 同日重跑（游标已在 10-14，开日为 ALREADY_OPENED）而 10-12 的快照缺失。
        e = new_env("2026-10-14", 0)
        seed(e, "2026-10-08", "2026-10-09", "2026-10-13", "2026-10-14")     # 独缺 10-12
        d2 = mk_buy(e, "2026-10-08", "sh600300", 10.2, 9.5, 11.0, window=3)
        _rows0 = len(PNL.iter_pnl(False, base=e["base"]))
        rep2 = run(e, "2026-10-14", {d2["id"]: judgement(d2["id"])})
        _st2 = {s["step"]: s for s in rep2["steps"]}
        T("I2 窗口内交易日(10-12)无快照 ⇒ 步3 记 GAP_SESSION_NO_DATA（**阻塞**）："
          "不是 PENDING_WINDOW、不打勾、游标不推进、账本零新增",
          {_reason(p) for p in _st2[STEPS[2]]["pending"]} == {BLOCK_GAP_NO_DATA}
          and _st2[STEPS[2]]["status"] == "PENDING"
          and STEPS[2] in rep2["pending_steps"] and rep2["settled"] is False
          and rep2["cursor_after"] == "2026-10-14"
          and len(PNL.iter_pnl(False, base=e["base"])) == _rows0,
          "步3=%s pending=%s" % (_st2[STEPS[2]]["status"],
                                sorted({_reason(p) for p in _st2[STEPS[2]]["pending"]})))

        # —— I2b 旧决策日 + 数据起点更晚（生产实际形态） ⇒ 步2 阻塞 ——
        eb = new_env("2026-10-13", 0)
        seed(eb, "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13")
        d2b = mk_buy(eb, "2026-09-24", "sh600300", 10.2, 9.5, 11.0, window=5)
        rep2b = run(eb, "2026-10-13", {d2b["id"]: judgement(d2b["id"])})
        _st2b = {s["step"]: s for s in rep2b["steps"]}
        T("I2b 决策日 09-24 而 09-28/29/30 三个交易日无快照 ⇒ 步2 阻塞（旧口径："
          "快照日序列只有 4 日 < w=5 ⇒ PENDING_WINDOW ⇒ 打勾放行、缺口被吞）",
          _st2b[STEPS[1]]["status"] == "PENDING"
          and {_reason(p) for p in _st2b[STEPS[1]]["pending"]} == {BLOCK_GAP_NO_DATA}
          and rep2b["settled"] is False and rep2b["cursor_after"] == "2026-10-13"
          and PNL.read_pnl(d2b["id"], False, base=eb["base"]) is None)

        # —— I3 休市日不阻塞（防"把无数据一律当缺口"的过阻塞） ——
        e3 = new_env("2026-09-30", 5)
        for _d in ("2026-10-08", "2026-10-09"):
            put_raw(_d, [("sh600310", 10.0, 10.1, 9.9, 10.05, 0.5)])
        d3 = mk_buy(e3, "2026-09-30", "sh600310", 10.2, 9.5, 11.0, window=2)
        j3 = {d3["id"]: judgement(d3["id"])}
        run(e3, "2026-10-08", j3)
        rep3 = run(e3, "2026-10-09", j3)
        r3 = PNL.read_pnl(d3["id"], False, base=e3["base"])
        T("I3 休市日无数据**不**报缺口、不进窗口：10-01~10-07（国庆）+ 10-10/11（周末）"
          "全无数据仍按 2 个交易日结完",
          r3 is not None and r3["exit_kind"] == "WINDOW_END"
          and r3["exit_date"] == "2026-10-09" and r3["hold_days"] == 2
          and not [p for p in rep3["steps"][2]["pending"]
                   if _reason(p) in (BLOCK_GAP_NO_DATA, BLOCK_DAY_UNKNOWN)])

        # —— I4 缺失后补录 ⇒ 完成且离场日不移动 ——
        _w4 = _window_axis("2026-10-08", "2026-10-14", 3, CAL,
                           frozenset({"2026-10-08", "2026-10-09", "2026-10-13", "2026-10-14"}))
        _pred4 = _w4["need"][-1] if _w4.get("need") else None
        T("I4a 缺口状态下**仍能先算出**日历推定的窗口末日（= 10-13）—— 供补录前后对照",
          _w4["state"] == BLOCK_GAP_NO_DATA and _pred4 == "2026-10-13")
        seed(e, "2026-10-12")                       # e 仍是 I2 的环境：补录缺口日
        rep4 = run(e, "2026-10-14", {d2["id"]: judgement(d2["id"])})
        r4 = PNL.read_pnl(d2["id"], False, base=e["base"])
        T("I4b 补录后同日重跑 ⇒ 步3 转 DONE、结账；**离场日 == 补录前推定的窗口末日 10-13**"
          "（旧口径：快照日序列把末日推到 10-14 ⇒ 离场日错位一个交易日）",
          r4 is not None and r4["exit_date"] == _pred4 and r4["exit_date"] != "2026-10-14"
          and r4["hold_days"] == 3 and rep4["steps"][2]["status"] == "DONE"
          and rep4["settled"] is True)

        # —— I5 开日闸（日历轴）——
        eA = new_env("2026-10-09", 5)
        T("I5 开日走日历轴：游标 10-09 且 10-12 无快照 ⇒ open_day(10-13) 拒"
          "（旧实现只看快照目录 ⇒ 该交易日被静默跳过、游标前移）",
          raises(open_day, "2026-10-13", cursor_path=eA["cursor"],
                 snapshot_root=eA["snap"], calendar=CAL))
        eB = new_env("2026-10-09", 5)
        _od5 = open_day("2026-10-12", cursor_path=eB["cursor"], snapshot_root=eB["snap"],
                        calendar=CAL)
        T("I5b 相邻交易日放行：游标 10-09 → 开 10-12（目标是交易日即可；"
          "其自身数据由步1 负责，不要求预先有快照）",
          _od5["opened"] is True and CUR.read_cursor(eB["cursor"])["date"] == "2026-10-12")

        # —— I6 未知日历状态 ⇒ fail-closed ——
        eC = new_env("2026-10-09", 5)
        T("I6 未注入日历 = 状态未知 ⇒ fail-closed：open_day / settle 均拒"
          "（禁用'工作日近似'顶替交易日身份）",
          raises(open_day, "2026-10-12", cursor_path=eC["cursor"], snapshot_root=eC["snap"])
          and raises(no_cal, eC, "2026-10-12"))
        _dd6 = frozenset({"2026-10-09"})
        T("I6b 双轴 API 一致性：无日历 / 日历越界 ⇒ UNKNOWN_DAY_STATE（窗口轴与成交日轴同判）",
          _window_axis("2026-10-08", "2026-10-14", 3, None, _dd6)["state"] == BLOCK_DAY_UNKNOWN
          and _session_axis("2026-10-08", "2026-10-14", None, _dd6)["state"] == BLOCK_DAY_UNKNOWN
          and _window_axis("2026-10-08", "2027-03-01", 3, CAL, _dd6)["state"]
          == BLOCK_DAY_UNKNOWN
          and _session_axis("2026-10-08", "2027-03-01", CAL, _dd6)["state"]
          == BLOCK_DAY_UNKNOWN)

        # —— I7 as_of 上界（禁读未来快照） ——
        eD = new_env("2026-10-14", 0)
        seed(eD, "2026-10-08", "2026-10-09", "2026-10-13", "2026-10-14")
        d7 = mk_buy(eD, "2026-10-08", "sh600300", 10.2, 9.5, 11.0, window=3)
        v7 = DEC.load_decision(d7["id"], base=eD["base"])
        _d7dd = data_days(eD["snap"])
        T("I7 as_of 上界：as_of=10-08 时成交日（10-09）尚未到 ⇒ _day_range 返回 (None,None)，"
          "**不**越界读未来快照（旧实现传 '9999-12-31' ⇒ 取到 10-09 的 low/high）",
          _day_range(v7, eD["snap"], "2026-10-08", CAL, _d7dd) == (None, None)
          and _day_range(v7, eD["snap"], "2026-10-14", CAL, _d7dd) == (9.9, 10.1))
        T("I7b 窗口序列恒 ≤ as_of：as_of=10-13 时不含 10-14",
          "2026-10-14" not in tuple(
              _window_axis("2026-10-08", "2026-10-13", 3, CAL, _d7dd)["days"]))

        # —— I8 非交易日有快照（数据异常）⇒ 不进窗口 ——
        _dd8a = frozenset({"2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14"})
        _w8a = _window_axis("2026-09-30", "2026-10-14", 5, CAL, _dd8a)
        _w8b = _window_axis("2026-09-30", "2026-10-14", 5, CAL, _dd8a | {"2026-10-10"})
        T("I8 非交易日（10-10 周六）有快照 = 数据异常 ⇒ 仍不进窗口：与'该日无数据'"
          "**逐字相同**（旧实现把快照日当交易日 ⇒ 结果被污染）",
          tuple(_w8a["days"]) == tuple(_w8b["days"]) and _w8b["complete"] is True
          and tuple(_w8b["days"]) == _oct)

        # —— I9 两轴分离的显式判据 + 判别性 ——
        T("I9 四态齐备、两轴互不替代：交易日无数据=TRADING_NO_DATA；非交易日有数据="
          "NON_TRADING（**日历赢**，不看目录）；齐备=TRADING_WITH_DATA；无日历=UNKNOWN",
          day_verdict("2026-10-12", calendar=CAL, dd=set())["status"]
          == DC.STATUS_TRADING_NO_DATA
          and day_verdict("2026-10-10", calendar=CAL, dd={"2026-10-10"})["status"]
          == DC.STATUS_NON_TRADING
          and day_verdict("2026-10-12", calendar=CAL, dd={"2026-10-12"})["status"]
          == DC.STATUS_TRADING_WITH_DATA
          and day_verdict("2026-10-12", calendar=None, dd={"2026-10-12"})["status"]
          == DC.STATUS_UNKNOWN)
        ALT = DC.Calendar([d for d in CAL.sessions if d != "2026-10-12"] + ["2026-10-10"],
                          source="ALT-MUTANT", version="discriminate")
        T("I9b 判别性：同一份数据换成另一份日历 ⇒ 10-12/10-10 判定**翻转**"
          "（若判据来自目录而非日历，此断言必失败）",
          day_verdict("2026-10-12", calendar=ALT, dd={"2026-10-12"})["status"]
          == DC.STATUS_NON_TRADING
          and day_verdict("2026-10-10", calendar=ALT, dd={"2026-10-10"})["status"]
          == DC.STATUS_TRADING_WITH_DATA)

        # —— I10 日期轴 API 只读 ——
        _fp10 = tree_fp(eD["snap"])
        data_days(eD["snap"])
        _window_axis("2026-10-08", "2026-10-14", 3, CAL, data_days(eD["snap"]))
        _session_axis("2026-10-08", "2026-10-14", CAL, data_days(eD["snap"]))
        day_verdict("2026-10-12", calendar=CAL, dd=data_days(eD["snap"]))
        axis_report(CAL, eD["snap"])
        T("I10 日期轴 API 全程只读：快照根内容指纹逐字未变（零写点）",
          tree_fp(eD["snap"]) == _fp10)

        print("== H. 纪律与零污染 ==")
        imported, modes, defs, calls = _scan_source()
        src = _prod_source()
        T("H1 无网络/子进程导入",
          not (imported & {"requests", "urllib", "http", "socket", "subprocess", "openai"}))
        T("H2 无下单/撤单/成交类接口定义（结算不产生交易动作）",
          not [d for d in defs if any(b in d.lower() for b in
                                      ("order", "place_", "submit", "trade", "buy", "sell",
                                       "cancel", "execute_"))])
        T("H3 不自行读写游标文件（只经 mark_step / advance_cursor）",
          NAME_CURSOR_FILE not in src and NAME_CURSOR_CONST not in src
          and "mark_step" in calls.get("settle", set())
          and "advance_cursor" in calls.get("open_day", set()))
        T("H4 不直接碰账本文件（只经 DEC / PNL 接口）",
          NAME_LEDGER_DEC not in src and NAME_LEDGER_PNL not in src)
        T("H5 覆盖写模式只出现在自检内（生产只 append）",
          all(f == "_selftest" for m, _, f in modes if m in ("w", "w+", "r+", "a+")))
        T("H6 真游标/真账本/真学习队列未被自检触碰（指纹不变）",
          CUR.read_cursor() == real_cursor
          and (fp(DEC.ledger_path(False)), fp(DEC.ledger_path(True)),
               fp(PNL.ledger_path(False)), fp(PNL.ledger_path(True)),
               fp(LEARNING_PATH)) == real_files)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n结果：%d 通过 / %d 失败 / %d 跳过" % (ok, fail, len(skips)))
    if fails:
        print("失败项：%s" % " / ".join(fails))
    return 1 if fail else 0


# ── 自检用子场景（全部嵌套依赖在 _selftest 内传入的工厂，不碰真数据） ──────────
def _dual_case(new_env, put_raw, mk_buy, run, judgement):
    """止损与目标同日双触发 → 止损优先（v1.1：成交日次日 = 首个可卖日）。"""
    e = new_env("2026-10-30", 5)
    put_raw("2026-11-02", [("sh600100", 10.0, 10.3, 9.9, 10.2, 2.0)])
    put_raw("2026-11-03", [("sh600100", 10.1, 10.4, 9.9, 10.2, 0.0)])    # 成交日 @10.1
    put_raw("2026-11-04", [("sh600100", 10.0, 11.5, 9.0, 11.2, 12.0)])   # 同时越 stop 与 target
    did = mk_buy(e, "2026-11-02", "sh600100", 10.1, 9.5, 11.0, window=3)
    run(e, "2026-11-02", {did["id"]: judgement(did["id"])})
    run(e, "2026-11-03", {did["id"]: judgement(did["id"])})
    run(e, "2026-11-04", {did["id"]: judgement(did["id"])})
    row = PNL.read_pnl(did["id"], False, base=e["base"])
    return row is not None and row["outcome"] == "LOSS" and row["exit_price"] == 9.5


def _stop_win_case(new_env, put_raw, mk_buy, run, judgement):
    """v1.1.1：止损触发但卖出价高于便宜入场 → outcome=WIN + exit_kind=STOP（事件分离）。

    成交日低开 9.6 便宜成交（entry=9.6 < stop=9.8）；T+1 日 low 9.7 破止损但
    open 9.75 > entry → px=open 9.75，最终盈亏为正：v1.1 旧语义误标 LOSS 配正 pct，
    v1.1.1 起落 outcome=WIN + exit_kind=STOP（触发事件 ≠ 盈亏结果）。
    """
    e = new_env("2026-10-13", 5)
    put_raw("2026-10-14", [("sh600120", 10.0, 10.3, 9.9, 10.2, 2.0)])
    put_raw("2026-10-15", [("sh600120", 9.6, 10.0, 9.5, 9.9, -3.0)])    # 低开 9.6 ≤ 限价 → 成交
    put_raw("2026-10-16", [("sh600120", 9.75, 9.9, 9.7, 9.85, -0.5)])   # T+1：low 破 stop，@open 9.75 卖
    put_raw("2026-10-20", [("sh600120", 9.9, 10.0, 9.8, 9.95, 1.0)])    # v1.3：原为 10-17（周六）
    did = mk_buy(e, "2026-10-14", "sh600120", 10.05, 9.8, 11.0, window=3)
    run(e, "2026-10-14", {did["id"]: judgement(did["id"])})
    run(e, "2026-10-15", {did["id"]: judgement(did["id"])})
    run(e, "2026-10-16", {did["id"]: judgement(did["id"])})
    row = PNL.read_pnl(did["id"], False, base=e["base"])
    return (row is not None and row["outcome"] == "WIN" and row["exit_kind"] == "STOP"
            and row["exit_date"] == "2026-10-16" and row["exit_price"] == 9.75
            and row["entry_price"] == 9.6 and row["pnl_pct"] > 0 and row["hold_days"] == 2)


def _gap_case(new_env, put_raw, mk_buy, run, judgement):
    """跳空低开跌破止损 → 以 open 成交（更差）；T+1：成交日不可卖，次一交易日跳空出场。"""
    e = new_env("2026-10-13", 5)
    put_raw("2026-10-14", [("sh600110", 10.0, 10.2, 9.9, 10.1, 1.0)])
    put_raw("2026-10-15", [("sh600110", 10.0, 10.2, 9.9, 10.0, -1.0)])   # 成交日 @10.0
    put_raw("2026-10-16", [("sh600110", 8.9, 9.0, 8.7, 8.8, -12.0)])     # 低开 8.9 < stop 9.5
    did = mk_buy(e, "2026-10-14", "sh600110", 10.05, 9.5, 11.0, window=3)
    run(e, "2026-10-14", {did["id"]: judgement(did["id"])})
    run(e, "2026-10-15", {did["id"]: judgement(did["id"])})
    run(e, "2026-10-16", {did["id"]: judgement(did["id"])})
    row = PNL.read_pnl(did["id"], False, base=e["base"])
    return row is not None and row["outcome"] == "LOSS" and row["exit_price"] == 8.9


def _target_case(new_env, put_raw, mk_buy, run, judgement):
    """目标触发 → WIN，hold_days = 自成交日起的交易日序号。"""
    e = new_env("2026-10-19", 5)
    put_raw("2026-10-20", [("sz000001", 10.0, 10.3, 9.9, 10.1, 1.0)])
    put_raw("2026-10-21", [("sz000001", 10.1, 10.6, 10.0, 10.5, 4.0)])   # 成交日 @10.1
    put_raw("2026-10-22", [("sz000001", 10.5, 11.6, 10.4, 11.5, 9.0)])
    did = mk_buy(e, "2026-10-20", "sz000001", 10.2, 9.5, 11.0, window=3)
    run(e, "2026-10-20", {did["id"]: judgement(did["id"])})
    run(e, "2026-10-21", {did["id"]: judgement(did["id"])})
    run(e, "2026-10-22", {did["id"]: judgement(did["id"])})
    row = PNL.read_pnl(did["id"], False, base=e["base"])
    return row is not None and row["outcome"] == "WIN" and row["exit_price"] == 11.0 \
        and row["hold_days"] == 2


def _window_end_case(new_env, put_raw, mk_buy, run, judgement):
    """窗口内均未触发 → 以窗口末日收盘价离场；涨 → WIN（窗口自成交日起算）。"""
    e = new_env("2026-10-26", 5)
    for i, d in enumerate(("2026-10-27", "2026-10-28", "2026-10-29")):
        put_raw(d, [("sz000010", 10.0 + i * 0.05, 10.2 + i * 0.05, 9.95 + i * 0.05,
                     10.1 + i * 0.05, 1.0)])
    did = mk_buy(e, "2026-10-27", "sz000010", 10.05, 9.0, 12.0, window=2)
    for d in ("2026-10-27", "2026-10-28", "2026-10-29"):
        run(e, d, {did["id"]: judgement(did["id"])})
    row = PNL.read_pnl(did["id"], False, base=e["base"])
    return row is not None and row["outcome"] == "WIN" and row["hold_days"] == 2 \
        and row["exit_price"] == 10.2


def _no_capital_case(new_env, put_raw, mk_buy, run, judgement):
    """equity 缺 → PENDING_CAPITAL，不写行（窗口走完只剩本金缺口）。"""
    e = new_env("2026-11-03", 5)
    put_raw("2026-11-04", [("sz000020", 10.0, 10.4, 9.9, 10.3, 3.0)])
    put_raw("2026-11-05", [("sz000020", 10.0, 10.4, 9.9, 10.3, 0.0)])    # 成交日 @10.0
    put_raw("2026-11-06", [("sz000020", 10.1, 10.4, 10.0, 10.2, 0.0)])   # 窗口末日
    did = mk_buy(e, "2026-11-04", "sz000020", 10.2, 9.5, 12.0, window=2)
    run(e, "2026-11-04", {did["id"]: judgement(did["id"])})
    run(e, "2026-11-05", {did["id"]: judgement(did["id"])})          # 成交日
    rep = run(e, "2026-11-06", {did["id"]: judgement(did["id"])}, equity=None)
    return any(p["reason"] == "PENDING_CAPITAL" for p in rep["steps"][2]["pending"]) \
        and PNL.read_pnl(did["id"], False, base=e["base"]) is None


def _capital_override_case(new_env, put_raw, mk_buy, run, judgement):
    """显式注入 capital → 优先于 actual_position × equity（与手算逐笔对齐）。

    v1.3：原夹具用 11-07（周六）/11-08（周日）当交易日，真实日历上不存在；
    顺延到 11-09（周一）/11-10（周二）。
    """
    e = new_env("2026-11-05", 5)
    put_raw("2026-11-06", [("sz000030", 10.0, 10.4, 9.9, 10.3, 3.0)])
    put_raw("2026-11-09", [("sz000030", 10.0, 10.4, 9.9, 10.3, 0.0)])    # 成交日 @10.0
    put_raw("2026-11-10", [("sz000030", 10.1, 10.4, 10.0, 10.2, 0.0)])   # 窗口末日
    did = mk_buy(e, "2026-11-06", "sz000030", 10.2, 9.5, 12.0, window=2)
    run(e, "2026-11-06", {did["id"]: judgement(did["id"])})
    run(e, "2026-11-09", {did["id"]: judgement(did["id"])})          # 成交日
    run(e, "2026-11-10", {did["id"]: judgement(did["id"], capital=1234.56)})
    row = PNL.read_pnl(did["id"], False, base=e["base"])
    return row is not None and row["capital"] == 1234.56 \
        and abs(row["pnl_amount"] - 1234.56 * row["pnl_pct"] / 100.0) < 0.01


def _win_but_wrong_case(new_env, put_raw, mk_buy, run, judgement):
    """赢了但三槽位全 REFUTE → 步4 摘要 win_but_wrong ≥ 1，且步5 记 WIN_BUT_WRONG 缺口。"""
    e = new_env("2026-11-10", 5)
    put_raw("2026-11-11", [("sz000040", 10.0, 10.3, 9.9, 10.2, 1.0)])
    put_raw("2026-11-12", [("sz000040", 10.1, 10.4, 10.0, 10.2, 0.0)])   # 成交日 @10.1
    put_raw("2026-11-13", [("sz000040", 10.2, 11.4, 10.1, 11.3, 10.0)])  # 越目标 11.0
    did = mk_buy(e, "2026-11-11", "sz000040", 10.15, 9.5, 11.0, window=3)
    run(e, "2026-11-11", {did["id"]: judgement(did["id"])})
    run(e, "2026-11-12", {did["id"]: judgement(did["id"])})
    rep = run(e, "2026-11-13",
              {did["id"]: judgement(did["id"], ("REFUTE", "REFUTE", "REFUTE"))})
    gaps = [g for g in _read_jsonl(e["learn"])[-1]["gaps"]]
    return {"win_but_wrong": rep["steps"][3]["summary"]["win_but_wrong"],
            "gap_kinds": sorted({g["kind"] for g in gaps})}


def _skip_case(new_env, put_raw, run, raises, calendar):
    """防跳日：中间存在已采集（有快照）但未结账的交易日 → 拒。"""
    e = new_env("2026-12-07", 5)
    os.makedirs(os.path.join(e["snap"], "2026-12-08"), exist_ok=True)
    os.makedirs(os.path.join(e["snap"], "2026-12-09"), exist_ok=True)
    put_raw("2026-12-08", [("sh600000", 10.0, 10.1, 9.9, 10.0, 0.5)])
    put_raw("2026-12-09", [("sh600000", 10.0, 10.2, 9.9, 10.1, 1.0)])
    try:
        settle("2026-12-10", base=e["base"], snapshot_root=e["snap"], raw_dir=os.path.join(
            e["dir"], "raw"), cursor_path=e["cursor"], learning_path=e["learn"],
            calendar=calendar)
        return False
    except SettleError as ex:
        return "SKIP_SETTLEMENT" in str(ex)


def _discipline(verbose=True):
    """纪律自检：①无网络/子进程导入 ②无交易类接口 ③不自行碰游标/账本文件。"""
    banned_imp = {"requests", "urllib", "http", "socket", "subprocess", "openai"}
    banned_def = ("order", "place_", "submit", "trade", "buy", "sell", "cancel", "execute_")
    imported, modes, defs, calls = _scan_source()
    src = _prod_source()
    bad_imp = sorted(imported & banned_imp)
    bad_def = [d for d in defs if any(b in d.lower() for b in banned_def)]
    bad_mode = [(m, ln, f) for m, ln, f in modes
                if m in ("w", "w+", "r+", "a+") and f != "_selftest"]
    bad_cursor = (NAME_CURSOR_FILE in src) or (NAME_CURSOR_CONST in src)
    bad_ledger = (NAME_LEDGER_DEC in src) or (NAME_LEDGER_PNL in src)
    if verbose:
        print("纪律自检 · settle.py")
        print("  ① 网络/子进程导入        : %s" % (bad_imp or "无 [OK]"))
        print("  ② 下单/交易类接口定义     : %s" % (bad_def or "无 [OK]（结算不产生交易动作）"))
        print("  ③ 覆盖写（w/w+/r+/a+）    : %s"
              % (["行%d@%s" % (l, f) for _, l, f in bad_mode] or "仅自检内 [OK]"))
        print("  ④ 自行读写游标文件        : %s"
              % ("命中 [!!]" if bad_cursor else "无 [OK]（只经 mark_step / advance_cursor）"))
        print("  ⑤ 直接碰账本文件          : %s"
              % ("命中 [!!]" if bad_ledger else "无 [OK]（只经 DEC / PNL 接口）"))
        print("  ⑥ 生产写文件模式          : %s"
              % sorted({str(m) for m, _, f in modes if f != "_selftest"}))
    bad = bad_imp or bad_def or bad_mode or bad_cursor or bad_ledger
    return 0 if not bad else 1


def _main(argv):
    if "--selftest" in argv:
        print("settle.py 自检（0.6 五步结算 · 原子性/幂等 · 日期轴双判 v1.3 · "
              "SCHEMA_VERSION=%d · 临时目录）" % SCHEMA_VERSION)
        return _selftest()
    if "--discipline" in argv:
        return _discipline()
    cal = None
    if "--calendar" in argv:
        i = argv.index("--calendar")
        if i + 1 >= len(argv):
            print("--calendar 需要交易日历 CSV 路径（列：date,is_trade）")
            return 2
        cal = DC.Calendar.from_seed_csv(argv[i + 1])
        print("交易日历    : %s（%d 个交易日 · %s ~ %s）"
              % (cal.basis(), len(cal), cal.span[0], cal.span[1]))
    if "--settle" in argv:
        i = argv.index("--settle")
        d = _check_date(argv[i + 1])
        rep = settle(d, calendar=cal)
        print("五步结算 · %s（游标 %s → %s）" % (d, rep["cursor_before"], rep["cursor_after"]))
        print("  日期轴    : %s" % rep["date_axis"])
        for s in rep["steps"]:
            tail = ""
            if s["pending"]:
                tail = "  缺口 %d：%s" % (len(s["pending"]),
                                         " / ".join(sorted({p["reason"] for p in s["pending"]})))
            print("  [%-7s] %-8s 写 %d / 复用 %d%s"
                  % (s["status"], s["step"], s["written"], s["reused"], tail))
        print("  结账状态  : %s" % ("已结账 ✓" if rep["settled"]
                                    else "未结账（待办 %s）" % " / ".join(rep["pending_steps"])))
        print("  交易动作  : orders_created=%d（结算只记账，不产生交易动作）"
              % rep["orders_created"])
        return 0
    st = status()
    print("五步结算总览 · 0.6")
    print("  游标 C       : %s（%s）" % (st["open_day"], "已结账" if st["settled"] else "结账中"))
    print("  最后已结账日 : %s" % (st["last_settled"] or "（未全勾，折算需交易日历）"))
    print("  五步待办     : %s" % (" / ".join(st["pending_steps"]) or "无"))
    for axis, c in st["counts"].items():
        print("  %-12s : BUY %d · 已成交 %d · 已结算 %d"
              % (axis, c["buy"], c["with_execution"], c["settled"]))
    print("  待结算       : %d 笔" % len(st["awaiting_settlement"]))
    print("  交易日历     : %s"
          % ("%s（%d 交易日）" % (cal.basis(), len(cal)) if cal is not None and len(cal)
             else "未注入 ⇒ 结算将被拒（v1.3：交易日身份只来自日历；用 --calendar <csv> 注入）"))
    print("  学习队列     : %s" % LEARNING_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
