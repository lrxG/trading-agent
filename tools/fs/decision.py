#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""decision.py · 决策快照（施工方案 0.3 · v1.0 · 2026-10-08 建）

用途：**决策快照层**的唯一管理者 —— 只记「当时为什么这么做」，append-only，不可回改。
      「后来实际发生了什么」在结算层 `tools/fs/pnl.py`（0.4）。两层不合并（设计 §395）。

落点（施工 0.3 · 0.7 落点⑤类）：
  LIVE → data/generated/decision_ledger.jsonl
  SIM  → data/generated/sim/decision_ledger.jsonl      ← SIM/LIVE **物理分轴**

★ 一、append-only 是结构保证，不是纪律口号
  本模块对账本只有「读」与「追加一行」两种动作 —— **代码里不存在任何 update / delete /
  覆盖写路径**（open 模式只有 "r" / "a"；"w" 仅出现在自检里写临时文件）。
  后续产物（C5 裁决仓 / 实际成交）**不改写原 DECISION 行**，一律向同一账本追加不同
  `record_type` 的事件（施工 0.3「同账本追加事件」）。

★ 二、三类事件 + 物化视图
  DECISION   决策时冻结：action / thesis / thesis_state / env / params / persona /
             C4 请求仓 position_request / 价线 / 信息集指纹 / call_chain
  RISK_PLAN  C5 裁决后追加：position_plan（**只允许 ≤ position_request**，C5 是硬闸）
  EXECUTION  成交后追加：actual_position（**只允许 ≤ position_plan**）+ execution_result
  `load_decision(id)` 按 decision_id 把三类物化成一行（三层仓位并列），**底层历史行零改动**
  —— 归因据此拆「C4 判断错 / C5 因风险降仓 / 执行偏差」三层。

★ 三、不可变 = 深拷贝定格 + 指纹自证
  `thesis` / `thesis_state` 写入时 json 深拷贝定格，并记录：
    · thesis_fingerprint  sha256(规范化 JSON) —— 覆盖 thesis + thesis_state + history 长度
    · thesis_history_len  定格当时的演化史条数
  `verify_decision(id)` 重算指纹 → INTACT / TAMPERED（防有人手改历史行）；
  `diff_thesis(id)`     拿定格对象与 `memory/state/thesis.md` **现值**比对 → IDENTICAL /
                        EVOLVED_SINCE（**只报不写**）。
  ⇒ **后续 Thesis 演化永不反向污染历史决策**；历史决策打开看到的一定是「当时的 Thesis」。

★ 四、SIM / LIVE 物理可区分（防 C6 统计混轴）
  · 两个轴 = 两个不同文件（SIM 在 data/generated/sim/ 子目录），按文件统计天然不混；
  · `write_decision` 的 `sim` 参数**无默认值** —— 不表态就报错，杜绝"默认进 LIVE"；
  · id 带轴前缀：LIVE = DEC-YYYYMMDD-NNN，SIM = SIM-DEC-YYYYMMDD-NNN，**跨轴永不撞号**；
  · 读接口 `iter_decisions(sim)` 同样强制表态；`load_decision(id)` 从 id 前缀判轴，
    若显式传入的 sim 与前缀不符 → AXIS_MISMATCH 拒（复制粘贴串轴的执行点）。

★ 五、本模块零交易动作（0.3 验收判据）
  无任何下单 / 撤单 / 成交接口，无网络与子进程导入；`write_*` 报告里 `orders_created` 恒 0。
  **写账永不反过来触发交易** —— 账本只有一个方向：事实 → 落盘。

★ 六、缺值 = 显式缺口，禁猜（与 0.2 / 0.10 同口径）
  · env 结构必填，但允许 `source="UNAVAILABLE"`（当时确实没有环境数据 = 事实，不造数）；
  · params.json 未落盘 → params_version="BUILTIN_DEFAULT" + params_source 标明来源；
  · persona.md 未落盘（4.1 未施工）→ persona_version=None + persona_source="PERSONA_ABSENT"。
  · `outcome` 在 DECISION 行**恒为 None**（结算结果一律进 pnl_ledger，不回填本行）。

被谁调用：C4 三问（下注定格）、C5 风控（append_risk_plan）、执行链（append_execution）、
          tools/risk/settle.py（0.6 结算读账）、C6 / E3（归因读当时的链）。

用法：
  python tools/fs/decision.py                              # 只读总览（两轴分开列）
  python tools/fs/decision.py --show DEC-20261008-001       # 物化视图 + 指纹自证
  python tools/fs/decision.py --selftest                    # 内置验收（临时文件，不碰真数据）
  from tools.fs.decision import write_decision, load_decision, append_risk_plan

规则：禁在别处内联本账本读写；字段增删必须升 SCHEMA_VERSION 并同步施工方案 0.3。
"""
import copy
import datetime as _dt
import hashlib
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.normpath(os.path.dirname(os.path.abspath(__file__))))
from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
import thesis as TH  # noqa: E402
import astwin as ASTW  # noqa: E402   ← AST 窗口（尾界）唯一实现，禁本模块自写

SCHEMA_VERSION = 1
LEDGER_NAME = "decision_ledger.jsonl"
SIM_DIR = "sim"

# ── 事件类型（施工 0.3：三类事件同账本，靠 record_type 区分） ────────────────────
REC_DECISION = "DECISION"
REC_RISK_PLAN = "RISK_PLAN"
REC_EXECUTION = "EXECUTION"
REC_TYPES = (REC_DECISION, REC_RISK_PLAN, REC_EXECUTION)

# ── 枚举（改枚举 = 改契约，须升 SCHEMA_VERSION） ────────────────────────────────
ACTIONS = ("BUY", "WAIT", "NO")                 # 三问结论，零模糊输出（无「再看看」）
DRIVERS = ("A", "B", "C", "复合", "驱动不明")     # 与 candidates.DRIVER_TYPES 同源
EXEC_RESULTS = ("filled", "partially_filled", "unfilled")
ENV_LEGS = ("INTRADAY", "EOD")                   # 盘中档 / 盘后档（1.1）
ENV_SOURCES = ("LOADER", "EXPLICIT", "UNAVAILABLE")

# ── DECISION 行字段（施工 0.3 写死；顺序 = 写盘顺序） ───────────────────────────
DECISION_FIELDS = (
    "record_type", "schema_version", "id", "date", "code", "action",
    "thesis_id", "thesis", "thesis_state", "thesis_fingerprint", "thesis_history_len",
    "driver", "entry_limit", "stop_line", "target", "window", "position_request",
    "sim", "env", "params_version", "params_source",
    "persona_version", "persona_snapshot", "persona_source",
    "frozen_at", "info_set_hash", "call_chain", "outcome",
)
RISK_PLAN_FIELDS = ("record_type", "schema_version", "decision_id", "as_of",
                    "position_plan", "gates", "note", "recorded_at")
EXECUTION_FIELDS = ("record_type", "schema_version", "decision_id", "as_of",
                    "actual_position", "execution_result", "avg_price", "note",
                    "recorded_at")

# C3 驱动链四件（事实/推断物理分开 —— C3 批准锁死，禁合并）
CHAIN_KEYS = ("evidence", "inference", "prediction", "five")
# prediction[] 七字段（施工 0.3；下注前冻结验证定义，结算时才能机械判）
PREDICTION_FIELDS = ("prediction_id", "metric", "data_source", "baseline",
                     "direction", "threshold", "condition", "window")
# M1/M2/M3 = 固定三槽位（**禁 M1M3 合并缩写**，M2=板块扩散不能漏）
PRED_SLOTS = ("M1", "M2", "M3")
# 五件套（施工 1.3 ④；键名 = 设计方案原文，禁造第二套叫法）
FIVE_KEYS = ("驱动存在", "当前证据", "延续机制", "失效条件", "剩余预期")

_ID_RE = re.compile(r"^DEC-\d{8}-\d{3}$")
_SIM_ID_RE = re.compile(r"^SIM-DEC-\d{8}-\d{3}$")
_TH_ID_RE = re.compile(r"^TH-\d{8}-\d{3}$")
_CODE_RE = re.compile(r"^(sh|sz|bj)?\d{6}$", re.I)

DEFAULT_PARAMS_VERSION = "BUILTIN_DEFAULT"
POS_TOL = 1e-6


class DecisionError(Exception):
    """决策账本操作被拒（非法字段 / 非法状态 / 篡改 / 串轴）——显式抛错，不静默降级。"""


# ── 基础校验 ──────────────────────────────────────────────────────────────────
def _check_date(date, name="date"):
    s = date.isoformat() if hasattr(date, "isoformat") else str(date)
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise DecisionError("%s 必须是 YYYY-MM-DD，收到: %r" % (name, date))
    try:
        _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        raise DecisionError("%s 非法: %r" % (name, date))
    return s


def _check_text(s, name, allow_empty=False):
    if not isinstance(s, str):
        raise DecisionError("%s 必须是字符串，收到: %r" % (name, s))
    if not allow_empty and not s.strip():
        raise DecisionError("%s 不得为空（缺值 = 显式 null，禁空串占位）" % name)
    return s


def _check_enum(v, allowed, name):
    if v not in allowed:
        raise DecisionError("%s 只能是 %s，收到: %r" % (name, " / ".join(allowed), v))
    return v


def _check_num(v, name, allow_none=False, positive=False, non_negative=False):
    if v is None:
        if allow_none:
            return None
        raise DecisionError("%s 必填（缺值 = 显式 null 由调用方声明，本字段不可缺）" % name)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise DecisionError("%s 必须是数值，收到: %r" % (name, v))
    if positive and v <= 0:
        raise DecisionError("%s 必须 > 0，收到: %r" % (name, v))
    if non_negative and v < 0:
        raise DecisionError("%s 必须 >= 0，收到: %r" % (name, v))
    return float(v)


def _check_ratio(v, name, allow_none=False):
    v = _check_num(v, name, allow_none=allow_none, non_negative=True)
    if v is not None and v > 1 + POS_TOL:
        raise DecisionError("%s 是仓位比例（0~1），收到: %r（疑似写成百分数）" % (name, v))
    return v


def _check_code(code):
    c = _check_text(code, "code")
    if not _CODE_RE.match(c):
        raise DecisionError("code 形如 sh600000 / 600000（字符串，防丢前导零），收到: %r" % (code,))
    return c


def _check_thesis_id(th_id):
    t = _check_text(th_id, "thesis_id")
    if not _TH_ID_RE.match(t):
        raise DecisionError("thesis_id 必须是 Thesis 实例身份 TH-YYYYMMDD-NNN（不是 code、不是日期），"
                            "收到: %r" % (th_id,))
    return t


def _now():
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ── 轴（SIM / LIVE 物理分轴） ──────────────────────────────────────────────────
def ledger_root(base=None):
    """base → 账本根目录（`data/generated`）。**两种调用口径归一**。

    ★ 2026-10-20 修（A 路日常化实证的路径错位）：本模块的 `base` 契约历来做「**账本根**
      （默认 `data/generated`）」，而编排层（`run_chain` / `proposal_input` / `orchestrate`）
      的 `base` 是「**项目根**」——两者混用导致 `base=<项目根>` 时账本被写到
      `<项目根>/sim/decision_ledger.jsonl`（误建目录），真账本 `data/generated/sim/`
      一行不动 ⇒ 决策「报告成功但没落账」。判据取**结构而非猜测**：
      `<base>/data/generated` 存在 ⇒ base 是项目根，下钻一级；否则 base 即账本根
      （沙盒/单测直接指 `.../generated` 的旧用法继续有效）。

    **判据顺序（2026-10-20 二次修正，eod_review 夹具实证）**：先看「账本文件是否就躺在
    base 下（任一轴）」——**在场证据优先于目录结构**。因为沙盒账本根可能同时内含
    `data/generated`（报告区），只看目录结构会把它误判成项目根，把夹具刚写的账本读成空。
    """
    if not base:
        return os.path.join(ROOT, "data", "generated")
    if _looks_like_ledger_root(base):
        return base
    if os.path.isdir(os.path.join(base, "data")):
        # ★ 项目根的**结构性**特征 = 有 data/ 目录（编排层沙盒即使尚未跑过任何
        #   写点，也必有 snapshot/state/input）——比"看 data/generated 是否已建"
        #   强：空账本根不会自带 data/ 子目录，而项目根必带（10-20 沙盒实测）。
        return os.path.join(base, "data", "generated")
    return base


def _looks_like_ledger_root(p):
    """p（或其 sim/）下已有**任一轴的账本文件** ⇒ p 是账本根（在场证据优先）。

    认 `*_ledger.jsonl` 家族而非单个文件名：决策账本在场、盈亏账本尚未创建时，
    同一次运行的后续调用仍应认得出「这是账本根」，不能因文件名不同而改判。
    """
    for d in (p, os.path.join(p, SIM_DIR)):
        if not os.path.isdir(d):
            continue
        try:
            for fn in os.listdir(d):
                if fn.endswith("_ledger.jsonl"):
                    return True
        except OSError:
            pass
    return False


def ledger_path(sim, base=None):
    """轴 → 文件路径。sim 必须显式布尔（无默认值：不表态 = 报错，不猜轴）。

    base 可为账本**根目录**（默认 data/generated）**或项目根**（`<proj>/data/generated`
    存在时自动下钻，见 `ledger_root`）—— 两轴自动分流到 base/ 与 base/sim/，
    调用方无需关心某个 id 属哪条轴（写 SIM 也不必手工拼路径）。
    """
    if not isinstance(sim, bool):
        raise DecisionError("sim 必须显式布尔（False=LIVE / True=SIM），收到: %r" % (sim,))
    root = ledger_root(base)
    return os.path.join(root, SIM_DIR, LEDGER_NAME) if sim else os.path.join(root, LEDGER_NAME)


def axis_of(decision_id):
    """由 id 前缀判轴（SIM-DEC-… = SIM；DEC-… = LIVE）；两个正则都不匹配 → 拒。"""
    d = _check_text(decision_id, "decision_id")
    if _SIM_ID_RE.match(d):
        return True
    if _ID_RE.match(d):
        return False
    raise DecisionError("decision_id 非法（LIVE=DEC-YYYYMMDD-NNN / SIM=SIM-DEC-YYYYMMDD-NNN），"
                        "收到: %r" % (decision_id,))


def axis_name(sim):
    return "SIM" if sim else "LIVE"


# ── 账本读写（只有「全量读」与「追加一行」两种动作） ────────────────────────────
def _read_rows(sim, base=None):
    """读该轴全部行（只读）。文件不存在 = 空账本（合法初始态）。坏行 → 报错不静默跳过。"""
    p = ledger_path(sim, base)
    if not os.path.exists(p):
        return []
    rows = []
    with io.open(p, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except ValueError as e:
                raise DecisionError("账本第 %d 行不是合法 JSON（%s）：%s" % (i, e, p))
            if not isinstance(obj, dict):
                raise DecisionError("账本第 %d 行必须是 JSON 对象：%s" % (i, p))
            rows.append(obj)
    return rows


def _append(row, sim, base=None):
    """追加一行 —— 本模块唯一的写动作（模式 "a"）。父目录按需创建。"""
    p = ledger_path(sim, base)
    _WG.check_write(p)
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    with io.open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")
    return p


def _new_id(sim, as_of, base=None):
    """生成该轴下一个决策 id（轴内单调，带轴前缀，跨轴永不撞号）。"""
    pre = "SIM-DEC-" if sim else "DEC-"
    key = pre + as_of.replace("-", "") + "-"
    used = {r.get("id") for r in _read_rows(sim, base)}
    n = 1
    while ("%s%03d" % (key, n)) in used:
        n += 1
    return "%s%03d" % (key, n)


# ── 指纹（不可变自证） ────────────────────────────────────────────────────────
def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def thesis_fingerprint(thesis_id, thesis, thesis_state):
    """定格内容指纹 —— 覆盖 Thesis 身份 + C3 驱动链 + Thesis 对象（含完整演化史）。"""
    return _sha(_canon({"thesis_id": thesis_id, "thesis": thesis, "thesis_state": thesis_state}))


# ── 结构校验：C3 驱动链 / Thesis 对象 / env / persona ─────────────────────────
def _norm_chain_prediction(p, idx):
    if not isinstance(p, dict):
        raise DecisionError("prediction[%d] 必须是对象" % idx)
    extra = set(p) - set(PREDICTION_FIELDS)
    miss = set(PREDICTION_FIELDS) - set(p)
    if extra or miss:
        raise DecisionError("prediction[%d] 字段不符（多=%s 缺=%s）—— 施工 0.3 写死七字段"
                            % (idx, sorted(extra), sorted(miss)))
    _check_enum(p["prediction_id"], PRED_SLOTS, "prediction[%d].prediction_id" % idx)
    for k in ("metric", "data_source", "condition"):
        _check_text(p[k], "prediction[%d].%s" % (idx, k))
    _check_enum(p["direction"], ("UP", "DOWN", "FLAT"), "prediction[%d].direction" % idx)
    if p["baseline"] is None:
        raise DecisionError("prediction[%d].baseline 不得为空（下注前必须冻结基准）" % idx)
    if p["threshold"] is None:
        raise DecisionError("prediction[%d].threshold 不得为空（下注前必须冻结阈值）" % idx)
    if p["window"] is None:
        raise DecisionError("prediction[%d].window 不得为空（下注前必须冻结窗口）" % idx)
    return dict(p)


def _norm_chain(thesis, action):
    """C3 驱动链快照（evidence[] / inference[] / prediction[] / five）结构校验。

    分工（同 F3 口径）：机械件**只校结构、不取数、不判语义** —— 链的内容是 C3 的判断。
    prediction 槽位：BUY **必须三槽位齐全**（M1/M2/M3，禁 M1M3 缩写）；
                      WAIT/NO 允许空（未下注 = 没有冻结的验证定义，结算层也不结账）。
    """
    if not isinstance(thesis, dict):
        raise DecisionError("thesis 必须是 C3 驱动链对象")
    extra = set(thesis) - set(CHAIN_KEYS)
    miss = set(CHAIN_KEYS) - set(thesis)
    if extra or miss:
        raise DecisionError("thesis 字段不符（多=%s 缺=%s）—— 只允许 %s（事实/推断物理分开）"
                            % (sorted(extra), sorted(miss), " / ".join(CHAIN_KEYS)))
    out = {}
    for k in ("evidence", "inference"):
        v = thesis[k]
        if not isinstance(v, list):
            raise DecisionError("thesis.%s 必须是列表" % k)
        out[k] = [_check_text(x, "thesis.%s[]" % k) for x in v]
    v = thesis["prediction"]
    if not isinstance(v, list):
        raise DecisionError("thesis.prediction 必须是列表")
    out["prediction"] = [_norm_chain_prediction(p, i) for i, p in enumerate(v)]
    slots = [p["prediction_id"] for p in out["prediction"]]
    if len(set(slots)) != len(slots):
        raise DecisionError("prediction 槽位重复（%s）—— 每个槽位只允许一项" % slots)
    if action == "BUY":
        if set(slots) != set(PRED_SLOTS):
            raise DecisionError("BUY 的 prediction 必须三槽位齐全 %s（禁 M1M3 合并缩写，"
                                "M2=板块扩散不能漏），收到: %s" % (list(PRED_SLOTS), slots))
    five = thesis["five"]
    if not isinstance(five, dict) or set(five) != set(FIVE_KEYS):
        raise DecisionError("thesis.five 必须恰含五件套 %s（键名 = 设计方案原文）"
                            % list(FIVE_KEYS))
    out["five"] = {k: (None if five[k] is None else _check_text(five[k], "five.%s" % k))
                   for k in FIVE_KEYS}
    return out


def _norm_thesis_state(st, thesis_id):
    """F3 Thesis 对象结构校验（深拷贝定格用）—— 字段集合以 thesis.py 的 FIELD_ORDER 为真源。"""
    if not isinstance(st, dict):
        raise DecisionError("thesis_state 必须是 Thesis 对象")
    expect = set(TH.FIELD_ORDER) | {"id", "history"}
    if set(st.keys()) != expect:
        raise DecisionError("thesis_state 字段不符：期望 %s，收到 %s（真源 = thesis.FIELD_ORDER）"
                            % (sorted(expect), sorted(st.keys())))
    if st["id"] != thesis_id:
        raise DecisionError("thesis_state.id=%r 与 thesis_id=%r 不一致" % (st["id"], thesis_id))
    _check_thesis_id(st["id"])
    _check_enum(st["state"], TH.STATES, "thesis_state.state")
    _check_date(st["created_at"], "thesis_state.created_at")
    _check_date(st["updated_at"], "thesis_state.updated_at")
    if not isinstance(st["history"], list):
        raise DecisionError("thesis_state.history 必须是列表（演化史 append-only）")
    for i, e in enumerate(st["history"]):
        if not isinstance(e, dict) or set(e) != {"date", "kind", "text", "evidence"}:
            raise DecisionError("thesis_state.history[%d] 字段不符（须 date/kind/text/evidence）" % i)
    return copy.deepcopy(st)


def _norm_env(env):
    """当时环境（归因必读）—— 结构必填，允许显式 UNAVAILABLE（不造数）。"""
    if not isinstance(env, dict):
        raise DecisionError("env 必填（当时环境档 + 关键信号）")
    extra = set(env) - {"regime", "signals", "leg", "source"}
    miss = {"leg", "source"} - set(env)
    if extra or miss:
        raise DecisionError("env 字段不符（多=%s 缺=%s）—— 允许 regime/signals/leg/source"
                            % (sorted(extra), sorted(miss)))
    e = {"regime": env.get("regime"), "signals": env.get("signals") or {},
         "leg": _check_enum(env["leg"], ENV_LEGS, "env.leg"),
         "source": _check_enum(env["source"], ENV_SOURCES, "env.source")}
    if e["regime"] is not None:
        _check_text(e["regime"], "env.regime")
    if not isinstance(e["signals"], dict):
        raise DecisionError("env.signals 必须是对象（键名 = params 的 snake_case 信号名）")
    if e["source"] == "UNAVAILABLE" and (e["regime"] is not None or e["signals"]):
        raise DecisionError("env.source=UNAVAILABLE 时不得携带 regime/signals（缺就是缺，不造数）")
    return e


def _read_params_version(params_path=None):
    """params.json 的版本号（0.8 未落盘 → BUILTIN_DEFAULT + 标来源，不猜版本）。"""
    p = params_path or os.path.join(ROOT, "data", "params.json")
    if not os.path.exists(p):
        return DEFAULT_PARAMS_VERSION, "PARAMS_ABSENT"
    try:
        with io.open(p, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
    except (ValueError, OSError):
        raise DecisionError("params.json 存在但不可解析（拒绝静默降级）：%s" % p)
    return str(d.get("version") or DEFAULT_PARAMS_VERSION), "PARAMS_JSON"


def _read_persona(persona_path=None):
    """人格版本（4.1 未施工 → PERSONA_ABSENT；**不写"稳健"之类档位占位**）。"""
    p = persona_path or os.path.join(ROOT, "memory", "state", "persona.md")
    if not os.path.exists(p):
        return None, None, "PERSONA_ABSENT"
    with io.open(p, "r", encoding="utf-8") as f:
        txt = f.read()
    m = re.search(r"^-\s*version:\s*(\S+)\s*$", txt, re.M)
    if not m:
        raise DecisionError("persona.md 存在但缺 version 行（拒绝静默降级）；格式见施工 4.1")
    return m.group(1), txt, "PERSONA_MD"


def _norm_call_chain(cc, thesis_id, thesis_path=None):
    if cc is None:
        cc = TH.call_chain(thesis_id, thesis_path)
    if not isinstance(cc, list):
        raise DecisionError("call_chain 必须是能力 id 列表（神经链实际调用路径）")
    return [_check_text(x, "call_chain[]") for x in cc]


# ── 写入：DECISION（冻结） ────────────────────────────────────────────────────
def write_decision(as_of, code, action, thesis_id, thesis, *, sim, driver=None,
                   thesis_state=None, entry_limit=None, stop_line=None, target=None,
                   window=None, position_request=0.0, env=None, persona_version=None,
                   persona_snapshot=None, info_set_hash=None, call_chain=None,
                   frozen_at=None, allow_duplicate=False, base=None,
                   thesis_path=None, params_path=None, persona_path=None):
    """写一条 DECISION（不可变）。返回写入行 + 审计信息。

    必填：as_of / code / action / thesis_id / thesis（C3 驱动链）/ sim（**显式表态**）/ env。
    幂等：同轴内 (date, code, action, thesis_id) 已存在 → 返回 EXISTS，**不新增、不改旧行**
          （重跑同一决策不重复记账）；确需同日同 Thesis 二次决策 → allow_duplicate=True。
    硬校验（机械可判部分，防半环）：
      · BUY   → driver != "驱动不明"（驱动不明还下注 = 强行归因）；
                 entry_limit/stop_line/target/window 全必填且 target > entry_limit > stop_line；
                position_request > 0；prediction 三槽位齐全。
      · WAIT/NO → 上述四项价位与 window **必须为空**、position_request **必须为 0**
                （有价位就是 BUY 的意图 —— 零模糊输出）。
      · outcome **恒为 None** —— 结果一律进 pnl_ledger（两层不合并）。
    """
    d = _check_date(as_of, "as_of")
    a = _check_enum(action, ACTIONS, "action")
    c = _check_code(code)
    th_id = _check_thesis_id(thesis_id)
    chain = _norm_chain(thesis, a)

    if thesis_state is None:
        try:
            thesis_state = TH.read_thesis(th_id, thesis_path)
        except TH.ThesisError:
            raise DecisionError("Thesis %s 不存在于 thesis.md —— 决策不得挂悬空 thesis_id"
                                "（thesis_id 是实例身份，不是 code）" % th_id)
    st = _norm_thesis_state(thesis_state, th_id)

    dv = driver if driver is not None else st["type"]
    _check_enum(dv, DRIVERS, "driver")
    if a == "BUY" and dv == "驱动不明":
        raise DecisionError("action=BUY 但 driver=驱动不明 —— 驱动不明是合法结果，但不是下注理由（1.4）")

    e_lmt = _check_num(entry_limit, "entry_limit", allow_none=True, positive=True)
    s_lmt = _check_num(stop_line, "stop_line", allow_none=True, positive=True)
    tgt = _check_num(target, "target", allow_none=True, positive=True)
    win = window
    if win is not None:
        if isinstance(win, bool) or not isinstance(win, int) or win < 1:
            raise DecisionError("window 是持有窗口（交易日，整数 >= 1），收到: %r" % (window,))
    pr = _check_ratio(position_request, "position_request")

    if a == "BUY":
        for nm, v in (("entry_limit", e_lmt), ("stop_line", s_lmt),
                      ("target", tgt), ("window", win)):
            if v is None:
                raise DecisionError("action=BUY 时 %s 必填（赔率三件齐才能下注）" % nm)
        if not (tgt > e_lmt > s_lmt):
            raise DecisionError("价位方向必须 target(%s) > entry_limit(%s) > stop_line(%s)"
                                % (tgt, e_lmt, s_lmt))
        if pr is None or pr <= 0:
            raise DecisionError("action=BUY 时 position_request 必须 > 0（C4 请求仓）")
    else:
        for nm, v in (("entry_limit", e_lmt), ("stop_line", s_lmt),
                      ("target", tgt), ("window", win)):
            if v is not None:
                raise DecisionError("action=%s 时 %s 必须为 null —— 有价位就是 BUY 的意图" % (a, nm))
        if pr not in (0, 0.0, None) and pr > POS_TOL:
            raise DecisionError("action=%s 时 position_request 必须为 0（不下注）" % a)
        pr = 0.0

    env_n = _norm_env(env)
    pv, ps, psrc = _read_persona(persona_path)
    if persona_version is not None:
        pv, ps, psrc = _check_text(persona_version, "persona_version"), persona_snapshot, "EXPLICIT"
    if (pv is None) != (ps is None):
        raise DecisionError("persona_version 与 persona_snapshot 必须同时有或同时为 null"
                            "（只有版本没内容 = 归因拿不到当时人格）")
    pver, psrc2 = _read_params_version(params_path)

    # 幂等去重（重跑不重复记账）
    if not allow_duplicate:
        for r in _read_rows(sim, base):
            if (r.get("record_type") == REC_DECISION and r.get("date") == d
                    and r.get("code") == c and r.get("action") == a
                    and r.get("thesis_id") == th_id):
                return {"id": r["id"], "created": False, "reused": True,
                        "reason": "EXISTS", "row": copy.deepcopy(r),
                        "orders_created": 0,
                        "ledger": ledger_path(sim, base), "axis": axis_name(sim)}

    did = _new_id(sim, d, base)
    row = {
        "record_type": REC_DECISION, "schema_version": SCHEMA_VERSION, "id": did,
        "date": d, "code": c, "action": a,
        "thesis_id": th_id, "thesis": chain, "thesis_state": st,
        "thesis_fingerprint": thesis_fingerprint(th_id, chain, st),
        "thesis_history_len": len(st["history"]),
        "driver": dv,
        "entry_limit": e_lmt, "stop_line": s_lmt, "target": tgt, "window": win,
        "position_request": pr,
        "sim": bool(sim), "env": env_n,
        "params_version": pver, "params_source": psrc2,
        "persona_version": pv, "persona_snapshot": ps, "persona_source": psrc,
        "frozen_at": _check_text(frozen_at, "frozen_at") if frozen_at else _now(),
        "info_set_hash": _check_text(info_set_hash, "info_set_hash") if info_set_hash else "UNAVAILABLE",
        "call_chain": _norm_call_chain(call_chain, th_id, thesis_path),
        "outcome": None,          # ← 恒 null：结果进 pnl_ledger，不回填本行
    }
    _assert_fields(row, DECISION_FIELDS, "DECISION")
    _append(row, sim, base)
    return {"id": did, "created": True, "reused": False, "row": copy.deepcopy(row),
            "orders_created": 0, "ledger": ledger_path(sim, base), "axis": axis_name(sim)}


def _assert_fields(row, fields, name):
    if tuple(row.keys()) != tuple(fields):
        raise DecisionError("%s 行字段不符：期望 %s，收到 %s"
                            % (name, list(fields), list(row.keys())))


# ── 追加事件：C5 裁决 / 实际成交 ──────────────────────────────────────────────
def _find_decision(decision_id, base=None):
    sim = axis_of(decision_id)
    rows = _read_rows(sim, base)
    hits = [r for r in rows if r.get("record_type") == REC_DECISION and r.get("id") == decision_id]
    if not hits:
        raise DecisionError("决策不存在: %s（轴=%s）" % (decision_id, axis_name(sim)))
    if len(hits) > 1:
        raise DecisionError("同 id 出现 %d 条 DECISION（账本被篡改）: %s" % (len(hits), decision_id))
    return hits[0], rows, sim


def _existing_event(rows, decision_id, rec):
    return [r for r in rows if r.get("record_type") == rec and r.get("decision_id") == decision_id]


def append_risk_plan(decision_id, position_plan, *, as_of=None, gates=None, note=None,
                     recorded_at=None, base=None):
    """追加 RISK_PLAN（C5 裁决仓）—— **只允许 ≤ position_request**（C5 是硬闸，不得加仓）。

    一次决策只允许一条 RISK_PLAN：重复追加同内容 → EXISTS 幂等；不同内容 → 拒
    （允许二次改写 = 给"事后改账"开后门）。确需重判 → 走新决策（加仓即新决策）。
    """
    dec, rows, sim = _find_decision(decision_id, base)
    pp = _check_ratio(position_plan, "position_plan")
    if pp is None:
        raise DecisionError("position_plan 必填（C5 裁决结果）")
    pr = dec["position_request"]
    if pp > pr + POS_TOL:
        raise DecisionError("C5 裁决仓 %s 不得大于 C4 请求仓 %s —— C5 是硬闸，只能降不能加"
                            % (pp, pr))
    a = _check_date(as_of, "as_of") if as_of else dec["date"]
    if a < dec["date"]:
        raise DecisionError("RISK_PLAN.as_of=%s 早于决策日 %s（时间不得倒流）" % (a, dec["date"]))
    if gates is not None and not isinstance(gates, dict):
        raise DecisionError("gates 必须是对象（结构化裁决依据），收到: %r" % (gates,))
    row = {"record_type": REC_RISK_PLAN, "schema_version": SCHEMA_VERSION,
           "decision_id": decision_id, "as_of": a, "position_plan": pp,
           "gates": gates or {}, "note": note,
           "recorded_at": recorded_at or _now()}
    return _append_event(row, RISK_PLAN_FIELDS, REC_RISK_PLAN, sim, rows, note, base)


def append_execution(decision_id, actual_position, execution_result, *, as_of=None,
                     avg_price=None, note=None, recorded_at=None, base=None):
    """追加 EXECUTION（实际成交）—— **只允许 ≤ position_plan**（执行偏差只能减，不得超裁决）。

    execution_result：filled / partially_filled / unfilled；unfilled → actual_position 必须 = 0。
    一次决策只允许一条 EXECUTION（未成交即 actual_position=0，不是再追加一条）。
    """
    dec, rows, sim = _find_decision(decision_id, base)
    ap = _check_ratio(actual_position, "actual_position")
    if ap is None:
        raise DecisionError("actual_position 必填（实际成交仓，未成交填 0）")
    res = _check_enum(execution_result, EXEC_RESULTS, "execution_result")
    caps = _existing_event(rows, decision_id, REC_RISK_PLAN)
    cap = caps[-1]["position_plan"] if caps else dec["position_request"]
    cap_src = "position_plan(C5)" if caps else "position_request(C4)"
    if ap > cap + POS_TOL:
        raise DecisionError("实际成交仓 %s 不得大于 %s %s（执行只能 ≤ 裁决）" % (ap, cap_src, cap))
    if res == "unfilled" and ap > POS_TOL:
        raise DecisionError("execution_result=unfilled 时 actual_position 必须为 0")
    if res == "filled" and ap <= POS_TOL:
        raise DecisionError("execution_result=filled 时 actual_position 必须 > 0")
    a = _check_date(as_of, "as_of") if as_of else dec["date"]
    if a < dec["date"]:
        raise DecisionError("EXECUTION.as_of=%s 早于决策日 %s（时间不得倒流）" % (a, dec["date"]))
    row = {"record_type": REC_EXECUTION, "schema_version": SCHEMA_VERSION,
           "decision_id": decision_id, "as_of": a, "actual_position": ap,
           "execution_result": res,
           "avg_price": _check_num(avg_price, "avg_price", allow_none=True, positive=True),
           "note": note, "recorded_at": recorded_at or _now()}
    return _append_event(row, EXECUTION_FIELDS, REC_EXECUTION, sim, rows, note, base)


def _append_event(row, fields, rec, sim, rows, note, base):
    _assert_fields(row, fields, rec)
    for old in _existing_event(rows, row["decision_id"], rec):
        same = all(old.get(k) == row.get(k) for k in fields
                   if k not in ("recorded_at", "note"))
        if same:
            return {"created": False, "reused": True, "reason": "EXISTS",
                    "event": copy.deepcopy(old), "row": copy.deepcopy(old),
                    "orders_created": 0}
        raise DecisionError("决策 %s 已有 %s 事件且内容不同 —— 一次裁决/一次成交各只允许一条；"
                            "确需重判走新决策（append-only 不给改账开后门）"
                            % (row["decision_id"], rec))
    _append(row, sim, base)
    return {"created": True, "reused": False, "event": copy.deepcopy(row),
            "row": copy.deepcopy(row), "orders_created": 0}


# ── 读取：物化视图 / 指纹自证 / 定格 vs 现值 ──────────────────────────────────
def read_ledger(sim, base=None):
    """该轴原始行（只读副本）—— 审计用。sim 必须显式表态。"""
    return copy.deepcopy(_read_rows(sim, base))


def load_decision(decision_id, sim=None, base=None):
    """物化视图：DECISION + RISK_PLAN + EXECUTION → 一行（**底层历史行零改动**）。

    三层仓位并列 = 归因拆「C4 判断错 / C5 因风险降仓 / 执行偏差」。
    轴由 id 前缀判定；显式传入的 sim 与前缀不符 → AXIS_MISMATCH 拒（防串轴）。
    """
    prefix_sim = axis_of(decision_id)
    if sim is not None and bool(sim) != prefix_sim:
        raise DecisionError("AXIS_MISMATCH：id=%s 属 %s 轴，但显式 sim=%s —— 拒绝跨轴读取"
                            "（SIM/LIVE 物理分离，混轴会污染 C6 统计）"
                            % (decision_id, axis_name(prefix_sim), axis_name(bool(sim))))
    dec, rows, _ = _find_decision(decision_id, base)
    view = copy.deepcopy(dec)
    rp = _existing_event(rows, decision_id, REC_RISK_PLAN)
    ex = _existing_event(rows, decision_id, REC_EXECUTION)
    view["position_plan"] = copy.deepcopy(rp[-1]["position_plan"]) if rp else None
    view["risk_gates"] = copy.deepcopy(rp[-1]["gates"]) if rp else None
    view["risk_note"] = copy.deepcopy(rp[-1]["note"]) if rp else None
    view["actual_position"] = copy.deepcopy(ex[-1]["actual_position"]) if ex else None
    view["execution_result"] = copy.deepcopy(ex[-1]["execution_result"]) if ex else None
    view["avg_price"] = copy.deepcopy(ex[-1]["avg_price"]) if ex else None
    view["exec_note"] = copy.deepcopy(ex[-1]["note"]) if ex else None
    view["events"] = 1 + len(rp) + len(ex)
    view["axis"] = axis_name(prefix_sim)
    view["integrity"] = verify_decision(decision_id, base=base)["integrity"]
    return view


def verify_decision(decision_id, base=None):
    """指纹自证：重算定格指纹与账本里存的是否一致 → INTACT / TAMPERED。"""
    dec, _, _ = _find_decision(decision_id, base)
    recomputed = thesis_fingerprint(dec.get("thesis_id"), dec.get("thesis"),
                                    dec.get("thesis_state"))
    stored = dec.get("thesis_fingerprint")
    return {"id": decision_id, "integrity": "INTACT" if stored == recomputed else "TAMPERED",
            "fingerprint_stored": stored, "fingerprint_recomputed": recomputed,
            "thesis_history_len": dec.get("thesis_history_len")}


def iter_decisions(sim, as_of=None, code=None, action=None, base=None):
    """按轴遍历 DECISION（**sim 必填**：统计接口无默认轴，从接口层杜绝混轴）。"""
    if not isinstance(sim, bool):
        raise DecisionError("iter_decisions 的 sim 必须显式布尔（不做默认轴，防混轴统计）")
    out = []
    for r in _read_rows(sim, base):
        if r.get("record_type") != REC_DECISION:
            continue
        if as_of and r.get("date") != _check_date(as_of, "as_of"):
            continue
        if code and r.get("code") != code:
            continue
        if action and r.get("action") != action:
            continue
        out.append(copy.deepcopy(r))
    return out


def diff_thesis(decision_id, thesis_path=None, base=None):
    """定格 vs 现值：**只报不写**（历史决策永不回填，这里是"污染"的可观测证据）。

    返回 IDENTICAL / EVOLVED_SINCE（附新增演化史条数）/ NOT_FOUND（Thesis 已归档或删）。
    """
    dec, _, _ = _find_decision(decision_id, base)
    frozen = dec["thesis_state"]
    try:
        live = TH.read_thesis(dec["thesis_id"], thesis_path)
    except TH.ThesisError:
        return {"id": decision_id, "thesis_id": dec["thesis_id"], "verdict": "NOT_FOUND",
                "frozen_history_len": dec["thesis_history_len"], "deltas": []}
    deltas = []
    if len(live["history"]) != len(frozen["history"]):
        deltas.append("history %d → %d（%+d 条）"
                      % (len(frozen["history"]), len(live["history"]),
                         len(live["history"]) - len(frozen["history"])))
    for k in ("state", "gap", "updated_at", "claim", "verdict"):
        if live.get(k) != frozen.get(k):
            deltas.append("%s %r → %r" % (k, frozen.get(k), live.get(k)))
    return {"id": decision_id, "thesis_id": dec["thesis_id"],
            "verdict": "IDENTICAL" if not deltas else "EVOLVED_SINCE",
            "frozen_history_len": dec["thesis_history_len"],
            "live_history_len": len(live["history"]), "deltas": deltas}


# ── 纪律自检（静态扫描 · 不执行任何业务逻辑） ──────────────────────────────────
def _scan_source():
    """静态扫描本文件：返回 (imported, open_modes, defs)。open_modes 项 = (mode, lineno, fn)。"""
    import ast
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = ASTW.func_table(tree)         # 函数区间表 = astwin 唯一实现（尾界含多行尾部）
    imported = set()
    modes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
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
    return imported, modes, [nm for _, _, nm in funcs]


def _discipline(verbose=True):
    """纪律自检：①无网络/子进程导入 ②无下单类接口 ③open 无覆盖写（"w" 仅自检内）。"""
    banned_imp = {"requests", "urllib", "http", "socket", "subprocess", "openai"}
    banned_def = ("order", "place_", "submit", "trade", "buy", "sell", "cancel")
    imported, modes, defs = _scan_source()
    bad_imp = sorted(imported & banned_imp)
    bad_def = [d for d in defs if any(b in d.lower() for b in banned_def)]
    bad_mode = [(m, ln, f) for m, ln, f in modes if m in ("w", "w+", "r+", "a+") and f != "_selftest"]
    if verbose:
        print("纪律自检 · decision.py")
        print("  ① 网络/子进程导入      : %s" % (bad_imp or "无 [OK]"))
        print("  ② 下单/交易类接口定义   : %s" % (bad_def or "无 [OK]（写账不产生交易动作）"))
        print("  ③ 覆盖写（w/w+/r+/a+） : %s"
              % (["行%d@%s" % (l, f) for _, l, f in bad_mode] or "仅自检内 [OK]"))
        print("  ④ 账本写模式            : %s"
              % sorted({str(m) for m, _, f in modes if f != "_selftest"}))
    return 0 if not (bad_imp or bad_def or bad_mode) else 1


def _selftest():
    import shutil
    import tempfile
    ok = fail = 0
    fails = []

    def T(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [OK] %s" % name)
        else:
            fail += 1
            fails.append(name)
            print("  [!!] %s" % name)

    def raises(fn, *a, **kw):
        try:
            fn(*a, **kw)
            return False
        except (DecisionError, TH.ThesisError, TypeError):
            return True

    def _fp(p):
        if not os.path.exists(p):
            return None
        return hashlib.md5(io.open(p, "rb").read()).hexdigest()

    real = (_fp(ledger_path(False)), _fp(ledger_path(True)))
    tmp = tempfile.mkdtemp(prefix="ta_decision_selftest_")
    base = os.path.join(tmp, "generated")
    th_p = os.path.join(tmp, "thesis.md")
    D1, D2, D3 = "2026-10-08", "2026-10-09", "2026-10-12"

    def pred(slot, metric):
        return {"prediction_id": slot, "metric": metric, "data_source": "snapshot.daily_quote",
                "baseline": "T 日收盘", "direction": "UP", "threshold": 3.0,
                "condition": "T+3 内累计涨幅 >= 3%", "window": "3D"}

    FIVE = {"驱动存在": "板块级订单落地", "当前证据": "公告 + 资金同步流入",
            "延续机制": "订单排产 → 业绩预期", "失效条件": "公告被澄清",
            "剩余预期": "3 日内板块扩散"}

    def chain(three=True):
        return {"evidence": ["公告：公司获批量订单"],
                "inference": ["产业催化 → A 类驱动"],
                "prediction": [pred("M1", "个股 T+3 涨幅"), pred("M2", "板块扩散家数"),
                               pred("M3", "成交额维持")] if three else [],
                "five": dict(FIVE)}

    ENV = {"regime": "过热", "signals": {"limit_up_count": 62}, "leg": "INTRADAY",
           "source": "EXPLICIT"}
    TH.init_file(th_p)
    t1 = TH.create_thesis(D1, "A", "政策催化：板块级订单落地", code="sh600000",
                          candidate_id="CD-20261008-001", path=th_p)
    TH_ID = t1["id"]
    try:
        print("== A. 契约与字段 ==")
        T("A1 DECISION 字段 28 项", len(DECISION_FIELDS) == 28)
        T("A2 三类事件字段表齐备",
          len(RISK_PLAN_FIELDS) == 8 and len(EXECUTION_FIELDS) == 9)
        T("A3 action 三选一无「再看看」", ACTIONS == ("BUY", "WAIT", "NO"))
        T("A4 prediction 槽位 = M1/M2/M3（禁 M1M3 合并缩写）", PRED_SLOTS == ("M1", "M2", "M3"))
        T("A5 five 五件套键 = 设计方案原文", FIVE_KEYS == ("驱动存在", "当前证据", "延续机制",
                                                          "失效条件", "剩余预期"))
        T("A6 轴分流：LIVE 落 base/、SIM 落 base/sim/",
          ledger_path(False, base) == os.path.join(base, LEDGER_NAME)
          and ledger_path(True, base) == os.path.join(base, SIM_DIR, LEDGER_NAME))

        print("== B. 写入与冻结 ==")
        r1 = write_decision(D1, "sh600000", "BUY", TH_ID, chain(), sim=False, base=base,
                            thesis_path=th_p, entry_limit=10.0, stop_line=9.4, target=11.5,
                            window=3, position_request=0.2, env=ENV, info_set_hash="sha256:abc",
                            frozen_at="2026-10-08T09:35:10",
                            params_path=os.path.join(tmp, "no_params.json"),
                            persona_path=os.path.join(tmp, "no_persona.md"))
        DID = r1["id"]
        FROZEN = copy.deepcopy(r1["row"])
        T("B1 首写 created=True 且 id = DEC-YYYYMMDD-001",
          r1["created"] is True and DID == "DEC-20261008-001")
        T("B2 outcome 恒 None（结果进 pnl_ledger，不回填本行）", r1["row"]["outcome"] is None)
        T("B3 指纹非空且定格演化史条数 = 1",
          r1["row"]["thesis_fingerprint"].startswith("sha256:")
          and r1["row"]["thesis_history_len"] == 1)
        T("B4 call_chain = 该 Thesis 实际调用链", isinstance(r1["row"]["call_chain"], list))
        T("B5 params 未落盘 → BUILTIN_DEFAULT + PARAMS_ABSENT",
          r1["row"]["params_version"] == DEFAULT_PARAMS_VERSION
          and r1["row"]["params_source"] == "PARAMS_ABSENT")
        T("B6 persona 未施工 → None + PERSONA_ABSENT（不写档位占位）",
          r1["row"]["persona_version"] is None
          and r1["row"]["persona_source"] == "PERSONA_ABSENT")
        n0 = len(read_ledger(False, base))
        r1b = write_decision(D1, "sh600000", "BUY", TH_ID, chain(), sim=False, base=base,
                             thesis_path=th_p, entry_limit=10.0, stop_line=9.4, target=11.5,
                             window=3, position_request=0.2, env=ENV)
        T("B7 重复写同决策 → EXISTS 幂等（重跑不重复记账）",
          r1b["created"] is False and r1b["reason"] == "EXISTS"
          and len(read_ledger(False, base)) == n0)
        T("B8 env=UNAVAILABLE 且带 signals → 拒（缺就是缺不造数）",
          raises(write_decision, D1, "sh600000", "WAIT", TH_ID, chain(False), sim=False,
                 base=base, thesis_path=th_p, env={"leg": "EOD", "source": "UNAVAILABLE",
                                                   "signals": {"x": 1}}))

        print("== C. 硬校验（把半环收成闭环） ==")
        T("C1 BUY + driver=驱动不明 → 拒（驱动不明是合法结果但不是下注理由）",
          raises(write_decision, D1, "sh600001", "BUY", TH_ID, chain(), sim=False, base=base,
                 thesis_path=th_p, driver="驱动不明", entry_limit=10.0, stop_line=9.4,
                 target=11.5, window=3, position_request=0.2, env=ENV))
        T("C2 BUY 缺 stop_line → 拒",
          raises(write_decision, D1, "sh600002", "BUY", TH_ID, chain(), sim=False, base=base,
                 thesis_path=th_p, entry_limit=10.0, target=11.5, window=3,
                 position_request=0.2, env=ENV))
        T("C3 价位方向 target<entry → 拒",
          raises(write_decision, D1, "sh600003", "BUY", TH_ID, chain(), sim=False, base=base,
                 thesis_path=th_p, entry_limit=10.0, stop_line=9.4, target=9.9, window=3,
                 position_request=0.2, env=ENV))
        T("C4 BUY 缺 M2 槽位 → 拒（板块扩散不能漏）",
          raises(write_decision, D1, "sh600004", "BUY", TH_ID,
                 {"evidence": ["x"], "inference": ["y"], "five": dict(FIVE),
                  "prediction": [pred("M1", "a"), pred("M3", "c")]},
                 sim=False, base=base, thesis_path=th_p, entry_limit=10.0, stop_line=9.4,
                 target=11.5, window=3, position_request=0.2, env=ENV))
        T("C5 prediction 字段增删 → 拒",
          raises(write_decision, D1, "sh600005", "BUY", TH_ID,
                 {"evidence": [], "inference": [], "five": dict(FIVE),
                  "prediction": [dict(pred("M1", "a"), extra=1), pred("M2", "b"),
                                 pred("M3", "c")]},
                 sim=False, base=base, thesis_path=th_p, entry_limit=10.0, stop_line=9.4,
                 target=11.5, window=3, position_request=0.2, env=ENV))
        T("C6 WAIT 带 entry_limit → 拒（有价位就是 BUY 的意图）",
          raises(write_decision, D1, "sh600006", "WAIT", TH_ID, chain(False), sim=False,
                 base=base, thesis_path=th_p, entry_limit=10.0, env=ENV))
        T("C7 WAIT 带 position_request>0 → 拒",
          raises(write_decision, D1, "sh600007", "WAIT", TH_ID, chain(False), sim=False,
                 base=base, thesis_path=th_p, position_request=0.1, env=ENV))
        T("C8 thesis_id 传 code → 拒（必须是实例身份不是 code）",
          raises(write_decision, D1, "sh600008", "WAIT", "600000", chain(False), sim=False,
                 base=base, thesis_path=th_p, env=ENV))
        T("C9 悬空 thesis_id → 拒",
          raises(write_decision, D1, "sh600009", "WAIT", "TH-20261008-009", chain(False),
                 sim=False, base=base, thesis_path=th_p, env=ENV))
        T("C10 action 写「再看看」→ 拒（零模糊输出）",
          raises(write_decision, D1, "sh600010", "再看看", TH_ID, chain(False), sim=False,
                 base=base, thesis_path=th_p, env=ENV))
        T("C11 sim 不表态 → 拒（不猜轴）",
          raises(write_decision, D1, "sh600011", "WAIT", TH_ID, chain(False),
                 thesis_path=th_p, env=ENV))

        print("== D. 同账本追加事件 + 三层仓位物化 ==")
        before = io.open(ledger_path(False, base), "rb").read()
        dec_line0 = before.split(b"\n")[0]
        rp = append_risk_plan(DID, 0.15, as_of=D1, gates={"P2": "单板块上限"}, base=base)
        T("D1 RISK_PLAN 追加成功（新行，不动原行）", rp["created"] is True)
        T("D2 C5 加仓（plan>request）→ 拒（C5 是硬闸只能降）",
          raises(append_risk_plan, DID, 0.25, as_of=D1, base=base))
        T("D3 重复同内容 RISK_PLAN → EXISTS 幂等",
          append_risk_plan(DID, 0.15, as_of=D1, gates={"P2": "单板块上限"},
                           base=base)["created"] is False)
        T("D4 改内容再追 → 拒（不给事后改账开后门）",
          raises(append_risk_plan, DID, 0.10, as_of=D1, base=base))
        ex = append_execution(DID, 0.13, "partially_filled", as_of=D2, avg_price=10.12, base=base)
        T("D5 EXECUTION 追加成功", ex["created"] is True)
        T("D6 实际成交 > 裁决仓 → 拒（执行只能 ≤ 裁决）",
          raises(append_execution, DID, 0.20, "filled", as_of=D2, base=base))
        T("D7 unfilled 带仓 → 拒",
          raises(append_execution, DID, 0.1, "unfilled", as_of=D2, base=base))
        v = load_decision(DID, base=base)
        T("D8 物化三层仓位并列（request/plan/actual）",
          v["position_request"] == 0.2 and v["position_plan"] == 0.15
          and v["actual_position"] == 0.13 and v["events"] == 3)
        T("D9 追加事件后原 DECISION 行逐字节未变",
          io.open(ledger_path(False, base), "rb").read().split(b"\n")[0] == dec_line0)

        print("== E. 不可变：Thesis 演化不污染历史决策 ==")
        TH.update_thesis(TH_ID, D1, "SUPPORT", "证据加强：龙虎榜席位净买", ["seat"], path=th_p)
        TH.update_thesis(TH_ID, D2, "SUPPORT", "板块扩散出现（M2 兑现中）", path=th_p)
        v2 = load_decision(DID, base=base)
        live = TH.read_thesis(TH_ID, path=th_p)
        T("E1 定格演化史条数不变（仍 = 注入时）",
          len(v2["thesis_state"]["history"]) == 1 and v2["thesis_history_len"] == 1)
        T("E2 而 thesis.md 现值已演化（%d 条）→ 历史未被回填"
          % len(live["history"]), len(live["history"]) == 3)
        T("E3 物化视图的 thesis_state 与注入时定格逐字段相同（深拷贝，非引用现值）",
          v2["thesis_state"] == FROZEN["thesis_state"]
          and v2["thesis"] == FROZEN["thesis"]
          and v2["thesis_state"] is not FROZEN["thesis_state"])
        T("E4 verify_decision → INTACT（指纹自证未被手改）",
          verify_decision(DID, base=base)["integrity"] == "INTACT")
        dd = diff_thesis(DID, thesis_path=th_p, base=base)
        T("E5 diff_thesis → EVOLVED_SINCE 且只报不写",
          dd["verdict"] == "EVOLVED_SINCE" and dd["frozen_history_len"] == 1
          and dd["live_history_len"] == 3)
        T("E6 演化后决策行仍逐字节未变",
          io.open(ledger_path(False, base), "rb").read().split(b"\n")[0] == dec_line0)
        r2 = write_decision(D2, "sz000001", "WAIT", TH_ID, chain(False), sim=False, base=base,
                            thesis_path=th_p, env=ENV)
        lp = ledger_path(False, base)
        lines = io.open(lp, encoding="utf-8").read().splitlines()
        idx = next(i for i, s in enumerate(lines)
                   if json.loads(s).get("record_type") == REC_DECISION
                   and json.loads(s).get("id") == r2["id"])
        tampered = json.loads(lines[idx])
        tampered["thesis_state"]["claim"] = "被篡改"
        lines[idx] = json.dumps(tampered, ensure_ascii=False, separators=(",", ":"))
        io.open(lp, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
        T("E7 手改历史行 → verify 报 TAMPERED（篡改可被发现）",
          verify_decision(r2["id"], base=base)["integrity"] == "TAMPERED")
        T("E8 篡改后物化视图自带 integrity=TAMPERED",
          load_decision(r2["id"], base=base)["integrity"] == "TAMPERED")

        print("== F. SIM / LIVE 物理分轴 ==")
        sr = write_decision(D1, "sh600000", "BUY", TH_ID, chain(), sim=True, base=base,
                            thesis_path=th_p, entry_limit=10.0, stop_line=9.4, target=11.5,
                            window=3, position_request=0.2, env=ENV)
        T("F1 SIM id 带轴前缀 SIM-DEC-", sr["id"] == "SIM-DEC-20261008-001")
        T("F2 两轴落到两个不同文件",
          ledger_path(False, base) != ledger_path(True, base)
          and os.path.exists(ledger_path(False, base)) and os.path.exists(ledger_path(True, base)))
        T("F3 同 code 两轴各一条，轴内互不可见",
          len(iter_decisions(False, base=base)) == 2
          and len(iter_decisions(True, base=base)) == 1)
        T("F4 统计接口必须显式表态轴（sim=None → 拒）", raises(iter_decisions, None, base=base))
        T("F5 load_decision 由 id 前缀判轴 = SIM",
          load_decision(sr["id"], base=base)["axis"] == "SIM")
        T("F6 跨轴显式 sim 不符 → AXIS_MISMATCH 拒",
          raises(load_decision, sr["id"], False, base=base))
        T("F7 SIM 轴决策未被 LIVE 统计混入",
          all(d["id"].startswith("DEC-") for d in iter_decisions(False, base=base)))

        print("== G. 纪律与零污染 ==")
        imported, modes, defs = _scan_source()
        T("G1 无网络/子进程导入",
          not (imported & {"requests", "urllib", "http", "socket", "subprocess", "openai"}))
        T("G2 无下单/交易类接口定义（写账永不触发交易）",
          not [d for d in defs if any(b in d.lower() for b in
                                      ("order", "place_", "submit", "trade", "buy", "sell"))])
        T("G3 覆盖写模式只出现在自检内（账本零 update/delete 路径）",
          all(f == "_selftest" for m, _, f in modes if m in ("w", "w+", "r+", "a+")))
        T("G4 write/append 报告 orders_created 恒 0",
          r1["orders_created"] == 0 and rp["orders_created"] == 0 and ex["orders_created"] == 0)
        T("G5 真账本未被自检触碰（两轴指纹不变）",
          (_fp(ledger_path(False)), _fp(ledger_path(True))) == real)
        T("G6 坏 JSON 行 → 报错不静默跳过",
          _bad_row_case(base, tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：%s" % " / ".join(fails))
    return 1 if fail else 0


def _bad_row_case(base, tmp):
    p = os.path.join(tmp, "bad", LEDGER_NAME)
    os.makedirs(os.path.dirname(p))
    io.open(p, "a", encoding="utf-8", newline="\n").write("{不是 json}\n")
    try:
        _read_rows(False, os.path.join(tmp, "bad"))
        return False
    except DecisionError:
        return True


def _main(argv):
    if "--selftest" in argv:
        print("decision.py 自检（SCHEMA_VERSION=%d · 临时文件运行）" % SCHEMA_VERSION)
        return _selftest()
    if "--discipline" in argv:
        return _discipline()
    if "--show" in argv:
        did = argv[argv.index("--show") + 1]
        v = load_decision(did)
        print("决策物化视图 · %s（轴=%s · 事件 %d）" % (did, v["axis"], v["events"]))
        print("  code/action : %s %s    date=%s" % (v["code"], v["action"], v["date"]))
        print("  仓位三层    : request=%s  plan=%s  actual=%s（%s）"
              % (v["position_request"], v["position_plan"], v["actual_position"],
                 v["execution_result"]))
        print("  thesis_id   : %s（定格演化史 %d 条）" % (v["thesis_id"], v["thesis_history_len"]))
        print("  指纹自证    : %s" % v["integrity"])
        dd = diff_thesis(did)
        print("  定格 vs 现值: %s  %s" % (dd["verdict"], " / ".join(dd["deltas"]) or ""))
        return 0
    print("决策账本（0.3 · append-only · SIM/LIVE 物理分轴）")
    for sim in (False, True):
        p = ledger_path(sim, None)
        rows = _read_rows(sim, None) if os.path.exists(p) else []
        dec = [r for r in rows if r.get("record_type") == REC_DECISION]
        rp = sum(1 for r in rows if r.get("record_type") == REC_RISK_PLAN)
        ex = sum(1 for r in rows if r.get("record_type") == REC_EXECUTION)
        print("  [%s] %s" % (axis_name(sim), p))
        print("       DECISION=%d  RISK_PLAN=%d  EXECUTION=%d  %s"
              % (len(dec), rp, ex, "" if os.path.exists(p) else "（尚未创建）"))
        for r in dec[-5:]:
            print("         %s  %-10s %-4s req=%-5s sim=%s"
                  % (r["id"], r["code"], r["action"], r["position_request"], r["sim"]))
    print("  纪律    : append-only（无 update/delete）；写账恒不产生交易动作（orders_created=0）")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
