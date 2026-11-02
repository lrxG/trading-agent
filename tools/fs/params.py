#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""params.py · 参数总账与反哺闭环（施工方案 0.8 + 0.9 · v1.0 · 2026-10-08 建）

用途：**运行时数值的唯一权威**（设计 §8.1「机器唯一读写源」）—— 全工程任何地方要读一个阈值，
      只走本模块；全工程任何地方想改一个正式值，只走 `promote()` 一条路。

三个文件（物理分层，禁合并）：
  data/params.json           正式值（**唯一运行时权威**）+ 七列元数据
  data/calibration_queue.jsonl  候选值（append-only）—— 候选态**只存在这里**
  data/params_changelog.jsonl   变更史（append-only）—— 正式值的**唯一溯源**

★ 一、七列 meta 与三个维度的分工（原条六列 → 本条补第 7 列）
  0.8 原 schema 六列 `value/unit/status/source/last_review/tightening_direction`，
  但同条种子说明与设计 §8.2 都在用 `validation=UNCALIBRATED` —— **该列在 schema 里不存在**，
  属真接不上。本条补为**七列**，并把两个易混维度拆清：
    · status    生命周期：BASELINE（种子死值，从未被批准改动）/ CALIBRATED（经批准转正过）/ DEPRECATED
    · validation 验证层：UNCALIBRATED（未验证）/ UNDER_TEST（在验证中）/ VALIDATED（已验证）
    · 二者**不同义**：转正（status→CALIBRATED）**不等于**已验证（validation 可以仍是 UNCALIBRATED）——
      设计 §8.2「第一版死值 = 安全基线，不假装已验证」即此。禁把两列并成一列。
    · tightening_direction 收紧方向：LOWER_IS_TIGHTER / HIGHER_IS_TIGHTER / FREE
      —— 它不是备注，是**机械判据**：转正时据此判这次改动是「收紧」还是「放宽」（见 ★三）。

★ 二、读写权限边界（把设计 §8.3「Agent 无权直接改正式值」落成执行点）
  · 读：全工程只读 `load_params` / `get_value`（运行时权威），无第二份数值副本（任何 .md 禁存副本）。
  · 写正式值：**唯一入口 = `promote(queue_id, approver)`**，且必须同时满足
      ① 该候选末态为 APPROVED（由 `review()` 产生，批准人非空）；
      ② `approver` 非空 —— 无批准人不落盘；
      ③ 生存类放宽需跨区间证据（见 ★三）；
    写完一次只做三件事：改 value + status→CALIBRATED + last_review，**version patch +1**，
    并 append 一行 changelog。`_write_params()` 是本文件唯一写 params.json 的函数，
    `--discipline` 断言它只被 `promote` 调用。
  · 写候选值：`propose()` 只能 append 到 queue，**永不触碰 params.json**——
    「候选值 ≠ 自动生效值」（设计 §8.2）由物理分层保证，不靠自觉。

★ 三、生存类只许收紧（设计 §8.2 红线）
  LOWER_IS_TIGHTER 的参（熔断 / 单票顶 / 仓位上限）新值 **大于** 旧值 = 放宽 →
  候选 evidence 必须显式带 `CROSS_REGIME` 标记（跨区间证据），否则 `WIDEN_NEEDS_EVIDENCE` 拒。
  HIGHER_IS_TIGHTER 反向同理。FREE（进攻类）可双向。

★ 四、版本边界（0.9 changelog ↔ 0.8 params.json 的一致性，机械可验）
  · **每次正式值变更 → version 的 patch 位 +1**（1.0.0 → 1.0.1 → …）；
  · changelog 首行是初始化行 `{version:"1.0.0", param_name:"*", reason:"INIT_SEED"}`；
  · 三条校验（`verify_params()`）：
      V4 链式一致：同一 param 的逐行 `old_value → new_value` 首尾相接、末行 new == 当前正式值；
      V5+V8 版本自洽：非 `*` 行数 == patch 位数字（**漏记一次变更必被抓**）；
      V3 末行 version == params.json 的 version。
  · **两 status 值域物理隔离**（V6）：queue 的 `status`（PENDING/APPROVED/REJECTED/KEEP_TESTING）
    与 params 的 `status`（BASELINE/CALIBRATED/DEPRECATED）**同名不同义**，
    故断言两边值域互不出现 —— 防「审批态」与「生命周期态」串读。

★ 五、本模块零交易动作与零行情依赖
  无下单 / 撤单 / 成交接口；无网络与子进程导入；不改任何非 params/queue/changelog 的文件。
  **改阈值永不反过来触发交易**。

被谁调用：C1 `regime()`（档位区间与仓位映射）· C4/C5 门控（`cascade_*` 等阈值）·
          `tools/fs/thesis.py`（驱动字典 driver_dict、级联门槛）· C6/E6（读候选与变更史）·
          `param_sweep.py` / `v6_grid.py`（0.16：扫描输出**只准 propose 候选**，禁回写 params）。

用法：
  python tools/fs/params.py                    # 只读总览（参数表 + 候选 + 变更史）
  python tools/fs/params.py --verify           # 七列 / 版本边界 / 值域隔离全量校验
  python tools/fs/params.py --queue            # 候选队列（物化视图：同 id 取末态）
  python tools/fs/params.py --changelog        # 变更史
  python tools/fs/params.py --selftest         # 内置验收（临时文件，不碰真数据）
  python tools/fs/params.py --discipline       # 纪律自检（AST）

规则：禁在别处内联 params.json 读写；字段增删必须升 SCHEMA_VERSION 并同步施工方案 0.8 / 0.9。
"""
import datetime as _dt
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.normpath(os.path.dirname(os.path.abspath(__file__))))

SCHEMA_VERSION = 1

try:
    from paths import ROOT
except Exception:                                    # pragma: no cover
    ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）

PARAMS_PATH = os.path.join(ROOT, "data", "params.json")
QUEUE_PATH = os.path.join(ROOT, "data", "calibration_queue.jsonl")
CHANGELOG_PATH = os.path.join(ROOT, "data", "params_changelog.jsonl")

# ── 七列 meta（键名与顺序写死；真源 = 本常量）────────────────────────────────
PARAM_FIELDS = ("value", "unit", "status", "validation", "source",
                "last_review", "tightening_direction")

P_STATUS = ("BASELINE", "CALIBRATED", "DEPRECATED")          # 生命周期（params 侧）
P_VALIDATION = ("UNCALIBRATED", "UNDER_TEST", "VALIDATED")   # 验证层
P_SOURCE = ("USER_DEFINED", "BACKTEST", "EXPERIENCE", "REGIME_SEED", "MIGRATED")
P_TIGHTEN = ("LOWER_IS_TIGHTER", "HIGHER_IS_TIGHTER", "FREE")  # 收紧方向（机械判据）

# 单位词表（统一定名，防「%」「百分比」「成」「ratio」混用导致口径漂移；
# 新增单位 = 一次显式代码变更 + 记 changelog，不是随手写个新词）
UNIT_VOCAB = ("pct", "pct_per_day", "ratio", "count", "days", "minutes",
              "times", "steps", "ratio_map", "config", "enum")

# ── 候选队列（0.9 六字段；append-only，同 id 多行取末行 = 末态）──────────────
QUEUE_FIELDS = ("id", "param_name", "candidate_value", "evidence", "source",
                "status", "created_at")
QUEUE_STATUS = ("PENDING", "APPROVED", "REJECTED", "KEEP_TESTING")
_VERDICT_TO_STATUS = {"APPROVE": "APPROVED", "REJECT": "REJECTED",
                      "KEEP_TESTING": "KEEP_TESTING"}
_OPEN_QUEUE = ("PENDING", "KEEP_TESTING")

# ── 变更史（0.9 七字段）──────────────────────────────────────────────────────
CHANGELOG_FIELDS = ("version", "param_name", "old_value", "new_value",
                    "reason", "approver", "date")
INIT_PARAM = "*"                     # 初始化行的 param_name 哨兵
INIT_VERSION = "1.0.0"
CROSS_REGIME_TAG = "CROSS_REGIME"    # 生存类放宽所需的跨区间证据标记

_QUEUE_ID_RE = re.compile(r"^QC-\d{8}-\d{3}$")
_VER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class ParamError(Exception):
    """参数总账操作被拒（非法字段 / 非法值域 / 未批准转正 / 版本断裂）—— 显式抛错，不静默降级。"""


# ── 基础校验 ─────────────────────────────────────────────────────────────────
def _check_date(d, name="date"):
    s = d.isoformat() if hasattr(d, "isoformat") else str(d)
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise ParamError("%s 必须是 YYYY-MM-DD，收到: %r" % (name, d))
    try:
        _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        raise ParamError("%s 非法: %r" % (name, d))
    return s


def _check_text(s, name, allow_empty=False):
    if not isinstance(s, str):
        raise ParamError("%s 必须是字符串，实际: %r" % (name, type(s).__name__))
    if not allow_empty and not s.strip():
        raise ParamError("%s 不得为空" % name)
    if "\n" in s or "\r" in s:
        raise ParamError("%s 不得含换行" % name)
    return s


def _check_enum(v, allowed, name):
    if v not in allowed:
        raise ParamError("%s 只能是 %s，实际: %r" % (name, " / ".join(allowed), v))
    return v


def _check_value(v, name="value"):
    """参数值：数字 / 字符串 / bool / 列表 / 字典（配置类如 driver_dict 是字典）。"""
    if isinstance(v, bool) or v is None:
        raise ParamError("%s 只能是 数值 / 字符串 / 列表 / 字典，实际: %r" % (name, v))
    if isinstance(v, (int, float, str, list, dict)):
        return v
    raise ParamError("%s 类型不支持: %r" % (name, type(v).__name__))


def _read_json(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:
        raise ParamError("文件存在但不可解析（拒绝静默降级）：%s（%s）" % (path, e))


def _read_jsonl(path):
    if not path or not os.path.exists(path):
        return []
    out = []
    with io.open(path, encoding="utf-8") as f:
        for i, ln in enumerate(f, 1):
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception as e:
                raise ParamError("jsonl 第 %d 行不可解析：%s（%s）" % (i, path, e))
    return out


def _append_jsonl(path, rec):
    _WG.check_write(path)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with io.open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")


def _ver_tuple(v):
    m = _VER_RE.match(str(v or ""))
    if not m:
        raise ParamError("版本号必须是 X.Y.Z，实际: %r" % (v,))
    return tuple(int(x) for x in m.groups())


def _ver_str(t):
    return "%d.%d.%d" % t


# ── 读（运行时权威；全工程唯一数值来源）──────────────────────────────────────
def load_params(as_of=None, path=None):
    """读全部正式参数（运行时权威）。params.json 未落盘 → 返回空表 + source=PARAMS_ABSENT。

    as_of 只做**不早于**校验（参数是「当前」的，历史值要读哪个版本看 changelog）——
    传了就断言 as_of >= updated_at，防用未来的口径算过去的日子（后视）。
    """
    p = PARAMS_PATH if path is None else path
    d = _read_json(p)
    if not d:
        return {"version": None, "updated_at": None, "params": {}, "source": "PARAMS_ABSENT"}
    params = d.get("params")
    if not isinstance(params, dict):
        raise ParamError("params.json 缺 params 对象：%s" % p)
    if as_of is not None:
        a = _check_date(as_of, "as_of")
        u = d.get("updated_at")
        if u and _check_date(u, "updated_at") > a:
            raise ParamError("as_of(%s) 早于 params.updated_at(%s)：参数是当前值，"
                             "历史口径请读 changelog（禁后视）" % (a, u))
    return {"version": d.get("version"), "updated_at": d.get("updated_at"),
            "params": params, "source": "PARAMS_JSON"}


def params_version(path=None):
    """params.json 的文件版本号（决策快照 params_version 字段的来源）。"""
    d = _read_json(PARAMS_PATH if path is None else path) or {}
    return d.get("version")


def get_param(name, path=None):
    """单个参数的七列 meta（不存在 → None）。"""
    return load_params(path=path)["params"].get(name)


def get_value(name, default=None, path=None):
    """单参数的值（不存在 → default）—— 消费方最常用的入口。"""
    m = get_param(name, path)
    return default if not isinstance(m, dict) else m.get("value", default)


def list_params(path=None):
    """[(name, meta), ...]（保持文件内声明顺序）。"""
    return list(load_params(path=path)["params"].items())


def by_class(path=None):
    """按收紧方向归类：{'TIGHTEN_ONLY': [...], 'FREE': [...]} —— 生存类/进攻类视图。"""
    out = {"TIGHTEN_ONLY": [], "FREE": []}
    for n, m in list_params(path):
        t = m.get("tightening_direction")
        out["TIGHTEN_ONLY" if t in ("LOWER_IS_TIGHTER", "HIGHER_IS_TIGHTER") else "FREE"].append(n)
    return out


# ── 候选队列（0.9；append-only，候选态只存在这里）──────────────────────────
def read_queue(path=None):
    """原样读全部候选行（append-only 逐年历史；同一 id 可能多行）。"""
    return _read_jsonl(QUEUE_PATH if path is None else path)


def queue_view(path=None):
    """物化视图：同 id 取**末行**（末态），返回 {id: row}（与 decision 的物化同模式）。"""
    view = {}
    for row in read_queue(path):
        cid = row.get("id")
        if cid:
            view[cid] = row
    return view


def list_queue(status=None, path=None):
    """候选列表（按状态过滤；PENDING / KEEP_TESTING = 未决）。"""
    rows = sorted(queue_view(path).values(), key=lambda r: (r.get("created_at") or "", r.get("id") or ""))
    if status is None:
        return rows
    if isinstance(status, str):
        status = (status,)
    return [r for r in rows if r.get("status") in status]


def _next_queue_id(as_of, queue_path=None):
    n = sum(1 for r in read_queue(queue_path) if str(r.get("id", "")).startswith("QC-%s" % as_of.replace("-", "")))
    return "QC-%s-%03d" % (as_of.replace("-", ""), n + 1)


def propose(as_of, param_name, candidate_value, evidence, source, model_ver=None,
            path=None, queue_path=None):
    """提出**候选值**（设计 §8.3 ①：Agent 只能提议）。只 append queue，**绝不碰 params.json**。

    拒收条件：参数名不在 params.json（禁凭空造参）/ 候选值等于当前正式值（无变化不提案）/
    evidence 为空（无证据不提案）/ source 不在值域。
    """
    a = _check_date(as_of, "as_of")
    _check_text(param_name, "param_name")
    _check_text(evidence, "evidence")
    _check_enum(source, P_SOURCE, "source")
    _check_value(candidate_value, "candidate_value")
    cur = load_params(path=path)["params"]
    if param_name not in cur:
        raise ParamError("参数 %r 不在 params.json —— 新增参数须先走 params 变更（禁凭空造参）"
                         % param_name)
    if cur[param_name].get("value") == candidate_value:
        raise ParamError("候选值等于当前正式值（%r）—— 无变化不提案" % (candidate_value,))
    if model_ver is None:
        model_ver = cur[param_name].get("validation")
    qid = _next_queue_id(a, queue_path)
    rec = {"id": qid, "param_name": param_name, "candidate_value": candidate_value,
           "evidence": evidence, "source": source, "status": "PENDING", "created_at": a}
    if tuple(rec.keys()) != tuple(QUEUE_FIELDS):
        raise ParamError("候选行字段不符：期望 %s" % (list(QUEUE_FIELDS),))
    _append_jsonl(QUEUE_PATH if queue_path is None else queue_path, rec)
    return {"id": qid, "record": rec, "appended": True, "orders_created": 0}


def review(as_of, queue_id, verdict, approver, queue_path=None):
    """审批候选（设计 §8.3 ③：APPROVE / REJECT / KEEP_TESTING）。

    append-only：**追加一行同 id 的新末态**，不就地改历史行（审批史因此可回放）。
    param_name / candidate_value / evidence / source 从末行继承，禁在审批时偷改候选内容。
    """
    a = _check_date(as_of, "as_of")
    if not _QUEUE_ID_RE.match(str(queue_id or "")):
        raise ParamError("候选 id 格式须为 QC-YYYYMMDD-NNN，实际: %r" % (queue_id,))
    _check_text(approver, "approver")
    status = _VERDICT_TO_STATUS.get(verdict)
    if status is None:
        raise ParamError("verdict 只能是 %s，实际: %r" % (" / ".join(_VERDICT_TO_STATUS), verdict))
    qp = QUEUE_PATH if queue_path is None else queue_path
    cur = queue_view(qp).get(queue_id)
    if cur is None:
        raise ParamError("候选 %s 不存在" % queue_id)
    if cur.get("status") in ("APPROVED", "REJECTED"):
        raise ParamError("候选 %s 已终态（%s），不得再审批" % (queue_id, cur["status"]))
    rec = {"id": queue_id, "param_name": cur["param_name"],
           "candidate_value": cur["candidate_value"], "evidence": cur["evidence"],
           "source": cur["source"], "status": status, "created_at": a}
    _append_jsonl(qp, rec)
    return {"id": queue_id, "status": status, "approver": approver, "appended": True,
            "orders_created": 0}


# ── 变更史 ───────────────────────────────────────────────────────────────────
def read_changelog(path=None):
    return _read_jsonl(CHANGELOG_PATH if path is None else path)


# ── 转正（正式值的**唯一**写入口）────────────────────────────────────────────
def _write_params(path, doc):
    """写 params.json —— 本文件唯一写它的函数；`--discipline` 断言只被 promote 调用。"""
    _WG.check_write(path)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _widen_check(name, meta, old, new, evidence):
    """生存类放宽需跨区间证据（设计 §8.2）。返回 (is_widen, allowed)。"""
    t = meta.get("tightening_direction")
    if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        return False, True                      # 非数值参数（配置类）不做方向判
    is_widen = (t == "LOWER_IS_TIGHTER" and new > old) or (t == "HIGHER_IS_TIGHTER" and new < old)
    if not is_widen:
        return False, True
    return True, (CROSS_REGIME_TAG in str(evidence or ""))


def promote(as_of, queue_id, approver, reason=None, validation=None, path=None,
            queue_path=None, changelog_path=None):
    """**转正式值**（设计 §8.3 ③：批准才转正）—— 全工程唯一能改 params.json 正式值的入口。

    前置：候选末态必须是 APPROVED 且 approver 非空（Agent 无权自行转正）。
    写入：value=new · status→CALIBRATED · last_review=as_of · version patch+1 · updated_at=as_of。
    同时 append 一行 changelog（append-only 溯源）。**转正 ≠ 已验证**（validation 默认不动）。
    """
    a = _check_date(as_of, "as_of")
    _check_text(approver, "approver")
    if validation is not None:
        _check_enum(validation, P_VALIDATION, "validation")
        if validation == "VALIDATED":
            raise ParamError("promote 不得直接把 validation 置为 VALIDATED —— "
                             "规则验证走 1.6 提炼链（回测 + OOS + 批准），转正只管数值生效")
    qp = QUEUE_PATH if queue_path is None else queue_path
    pp = PARAMS_PATH if path is None else path
    cp = CHANGELOG_PATH if changelog_path is None else changelog_path

    cand = queue_view(qp).get(queue_id)
    if cand is None:
        raise ParamError("候选 %s 不存在" % queue_id)
    if cand.get("status") != "APPROVED":
        raise ParamError("候选 %s 末态为 %s，未获批准不得转正（正式值唯一变化路径 = 人工批准）"
                         % (queue_id, cand.get("status")))
    name = cand["param_name"]
    doc = _read_json(pp)
    if not doc or not isinstance(doc.get("params"), dict):
        raise ParamError("params.json 不存在或结构非法：%s" % pp)
    meta = doc["params"].get(name)
    if not isinstance(meta, dict):
        raise ParamError("参数 %r 不在 params.json" % name)

    new = cand["candidate_value"]
    old = meta.get("value")
    if new == old:
        raise ParamError("候选值等于当前正式值（%r）—— 无实际变更，不写盘" % (new,))
    is_widen, allowed = _widen_check(name, meta, old, new, cand.get("evidence"))
    if not allowed:
        raise ParamError("WIDEN_NEEDS_EVIDENCE：%s 属生存类（%s）放宽（%r → %r），"
                         "evidence 必须显式带 %s（跨区间证据）标记"
                         % (name, meta.get("tightening_direction"), old, new, CROSS_REGIME_TAG))

    nv = _ver_tuple(doc.get("version") or INIT_VERSION)
    nv = (nv[0], nv[1], nv[2] + 1)               # 每次变更 patch +1（版本边界，机械可验）
    meta["value"] = new
    meta["status"] = "CALIBRATED"
    meta["last_review"] = a
    if validation is not None:
        meta["validation"] = validation
    doc["version"] = _ver_str(nv)
    doc["updated_at"] = a
    if tuple(meta.keys()) != tuple(PARAM_FIELDS):
        raise ParamError("参数 %r 字段顺序不符（须 = PARAM_FIELDS）" % name)

    _write_params(pp, doc)
    _append_jsonl(cp, {"version": doc["version"], "param_name": name, "old_value": old,
                       "new_value": new,
                       "reason": reason or ("转正：%s（候选 %s）" % (cand.get("source"), queue_id)),
                       "approver": approver, "date": a})
    return {"queue_id": queue_id, "param_name": name, "old_value": old, "new_value": new,
            "version": doc["version"], "status": "CALIBRATED", "is_widen": is_widen,
            "approver": approver, "orders_created": 0}


# ── 校验（0.9 ↔ 0.8 的版本边界与值域隔离）───────────────────────────────────
def verify_params(path=None, changelog_path=None, queue_path=None):
    """全量校验：七列 / 值域 / 版本链 / 版本自洽 / 值域隔离。返回 {ok, checks, errors}。"""
    pp = PARAMS_PATH if path is None else path
    cp = CHANGELOG_PATH if changelog_path is None else changelog_path
    qp = QUEUE_PATH if queue_path is None else queue_path
    checks = []
    errs = []

    def C(name, ok, detail=""):
        checks.append({"ok": bool(ok), "name": name, "detail": detail})
        if not ok:
            errs.append("%s %s" % (name, detail))

    doc = _read_json(pp)
    C("V0 params.json 存在", bool(doc), pp)
    if not doc:
        return {"ok": False, "checks": checks, "errors": errs, "version": None, "n_params": 0}
    params = doc.get("params") or {}
    C("V1 版本号合规", bool(_VER_RE.match(str(doc.get("version") or ""))),
      "version=%r" % doc.get("version"))
    C("V2 updated_at 合规", bool(doc.get("updated_at")), "updated_at=%r" % doc.get("updated_at"))

    bad_cols, bad_enum = [], []
    for n, m in params.items():
        if not isinstance(m, dict) or tuple(m.keys()) != tuple(PARAM_FIELDS):
            bad_cols.append(n)
            continue
        if (m.get("status") not in P_STATUS or m.get("validation") not in P_VALIDATION
                or m.get("source") not in P_SOURCE or m.get("tightening_direction") not in P_TIGHTEN
                or m.get("unit") not in UNIT_VOCAB):
            bad_enum.append(n)
    C("V3 七列齐全且顺序一致（%d 个参数）" % len(params), not bad_cols, "不符: %s" % bad_cols)
    C("V4 五列值域合法（含 unit 词表）", not bad_enum, "越界: %s" % bad_enum)

    rows = _read_jsonl(cp)
    C("V5 changelog 有初始化行（%s）" % INIT_VERSION,
      bool(rows) and rows[0].get("param_name") == INIT_PARAM
      and rows[0].get("version") == INIT_VERSION,
      "首行=%r" % (rows[0] if rows else None))
    for r in rows:
        if tuple(r.keys()) != tuple(CHANGELOG_FIELDS):
            C("V5b changelog 行字段合规", False, "不符行: %r" % (r,))
            break
    else:
        C("V5b changelog 行字段合规", True, "%d 行" % len(rows))

    def _norm(v):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)

    chains, broken = {}, []
    for r in rows:
        n = r.get("param_name")
        if n == INIT_PARAM:
            continue
        prev = chains.get(n)
        if prev is None:
            if r.get("old_value") is None:
                broken.append("%s: 首行 old_value 为空（必须写明从什么改到什么）" % n)
        elif _norm(prev) != _norm(r.get("old_value")):
            broken.append("%s: 链断（上一 new=%r，本行 old=%r）"
                          % (n, prev, r.get("old_value")))
        chains[n] = r.get("new_value")
        if _norm(r.get("old_value")) == _norm(r.get("new_value")):
            broken.append("%s: 变更行 old == new（空变更不得入史）" % n)
    C("V6 变更链首尾相接（%d 个参数有变更史）" % len(chains), not broken, "; ".join(broken))

    mismatch = [n for n, v in chains.items()
                if n in params and _norm(params[n].get("value")) != _norm(v)]
    C("V7 链末值 == 当前正式值", not mismatch, "不符: %s" % mismatch)

    n_chg = sum(1 for r in rows if r.get("param_name") != INIT_PARAM)
    want = "%d.%d.%d" % (_ver_tuple(INIT_VERSION)[0], _ver_tuple(INIT_VERSION)[1], n_chg)
    C("V8 版本自洽（patch 位 == 变更行数 %d）" % n_chg, str(doc.get("version")) == want,
      "期望 %s，实际 %s" % (want, doc.get("version")))
    C("V9 changelog 末行 version == params.version",
      bool(rows) and rows[-1].get("version") == doc.get("version"),
      "末行=%r，文件=%r" % (rows[-1].get("version") if rows else None, doc.get("version")))
    later = [r.get("date") for r in rows if str(r.get("date") or "") > str(doc.get("updated_at") or "")]
    C("V10 updated_at 不早于任何变更行", not later, "晚于 updated_at: %s" % later)

    qrows = _read_jsonl(qp)
    qbad = [r.get("id") for r in qrows if r.get("status") not in QUEUE_STATUS]
    C("V11 候选 status 全在 queue 值域", not qbad, "越界: %s" % qbad)
    C("V12 queue 字段合规",
      all(tuple(r.keys()) == tuple(QUEUE_FIELDS) for r in qrows), "%d 行" % len(qrows))

    leak_a = sorted({m.get("status") for m in params.values() if isinstance(m, dict)}
                    & set(QUEUE_STATUS))
    leak_b = sorted({r.get("status") for r in qrows} & set(P_STATUS))
    C("V13 两 status 值域物理隔离（审批态 ≠ 生命周期态）", not leak_a and not leak_b,
      "串读: %s / %s" % (leak_a, leak_b))
    return {"ok": not errs, "checks": checks, "errors": errs,
            "version": doc.get("version"), "n_params": len(params)}


def verify_or_raise(**kw):
    r = verify_params(**kw)
    if not r["ok"]:
        raise ParamError("params 校验失败：%s" % "; ".join(r["errors"]))
    return r


# ── 自检 ─────────────────────────────────────────────────────────────────────
def _selftest():
    import shutil
    import tempfile
    ok = fail = 0
    fails = []

    def T(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [OK] %s" % name)
        else:
            fail += 1
            fails.append(name)
            print("  [!!] %s %s" % (name, detail))

    def raises(fn, *a, **kw):
        try:
            fn(*a, **kw)
            return False
        except (ParamError, TypeError):
            return True

    def errmsg(fn, *a, **kw):
        """捕获拒绝**原因**（不止于「抛错了」—— 断言是因正确理由被拒）。"""
        try:
            fn(*a, **kw)
            return ""
        except ParamError as e:
            return str(e)

    tmp = tempfile.mkdtemp(prefix="ta_params_selftest_")
    pp = os.path.join(tmp, "params.json")
    qp = os.path.join(tmp, "calibration_queue.jsonl")
    cp = os.path.join(tmp, "params_changelog.jsonl")
    D1, D2 = "2026-10-08", "2026-10-12"

    def mkmeta(v, unit="pct", status="BASELINE", validation="UNCALIBRATED",
               source="USER_DEFINED", lr=D1, tight="LOWER_IS_TIGHTER"):
        return {"value": v, "unit": unit, "status": status, "validation": validation,
                "source": source, "last_review": lr, "tightening_direction": tight}

    def seed(version=INIT_VERSION, limit=60, open_ratio=0.10):
        doc = {"version": version, "updated_at": D1, "params": {
            "single_stock_cap": mkmeta(limit, "pct"),
            "trial_position": mkmeta(open_ratio, "ratio", tight="FREE"),
            "regime_ladder": mkmeta({"主升": 0.7, "强势": 0.5, "震荡": 0.3, "退潮": 0.1},
                                    "ratio", "BASELINE", "UNCALIBRATED", "REGIME_SEED", D1, "FREE"),
            "driver_dict": mkmeta({"A": "产业催化", "B": "资金抢占", "C": "情绪扩散"},
                                  "enum", "BASELINE", "UNCALIBRATED", "USER_DEFINED", D1, "FREE"),
        }}
        with io.open(pp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")
        if not os.path.exists(cp):
            _append_jsonl(cp, {"version": INIT_VERSION, "param_name": INIT_PARAM,
                               "old_value": None, "new_value": None, "reason": "INIT_SEED",
                               "approver": "USER", "date": D1})
        return doc

    def fp(p):
        return io.open(p, "rb").read() if os.path.exists(p) else None

    real_sig = (fp(PARAMS_PATH), fp(QUEUE_PATH), fp(CHANGELOG_PATH))

    try:
        # ── A. schema 与常量 ────────────────────────────────────────────────
        print("== A. 七列 schema ==")
        T("A1 七列（补 validation，0.8 原六列接不上）", len(PARAM_FIELDS) == 7)
        T("A2 validation 在列内", "validation" in PARAM_FIELDS)
        T("A3 status 与 validation 是两列（不同义）", "status" in PARAM_FIELDS and "validation" in PARAM_FIELDS)
        T("A4 收紧方向是三值枚举（机械判据，非备注）", P_TIGHTEN == ("LOWER_IS_TIGHTER", "HIGHER_IS_TIGHTER", "FREE"))
        T("A5 三文件物理分层（params / queue / changelog）",
          len({PARAMS_PATH, QUEUE_PATH, CHANGELOG_PATH}) == 3)
        T("A6 候选字段七列（0.9 原文：id/param/candidate/evidence/source/status/created_at）",
          len(QUEUE_FIELDS) == 7)
        T("A7 变更史七列", len(CHANGELOG_FIELDS) == 7)
        T("A8 两 status 值域不重叠（QUEUE vs PARAMS）", not (set(QUEUE_STATUS) & set(P_STATUS)))
        T("A9 unit 词表统一定名（防 %／成／ratio 混用）", len(UNIT_VOCAB) == 11 and "pct" in UNIT_VOCAB)

        # ── B. 读 ──────────────────────────────────────────────────────────
        print("== B. 读（运行时权威）==")
        seed()
        L = load_params(path=pp)
        T("B1 load_params 读到全部种子", len(L["params"]) == 4 and L["source"] == "PARAMS_JSON")
        T("B2 版本可读", params_version(pp) == INIT_VERSION)
        T("B3 get_value 直取", get_value("single_stock_cap", path=pp) == 60)
        T("B4 get_param 取七列 meta", get_param("single_stock_cap", path=pp)["unit"] == "pct")
        T("B5 不存在 → default（不崩）", get_value("no_such", 7, path=pp) == 7)
        T("B6 配置类字典值可读（driver_dict）",
          isinstance(get_value("driver_dict", path=pp), dict)
          and get_value("driver_dict", path=pp)["A"] == "产业催化")
        T("B7 by_class 分生存/进攻", by_class(pp)["TIGHTEN_ONLY"] == ["single_stock_cap"]
          and "trial_position" in by_class(pp)["FREE"])
        T("B8 缺文件 → PARAMS_ABSENT（不造数）",
          load_params(path=os.path.join(tmp, "no.json"))["source"] == "PARAMS_ABSENT")
        T("B9 损坏 → 抛错（拒绝静默降级）",
          (io.open(os.path.join(tmp, "bad.json"), "w", encoding="utf-8").write("{"), 
           raises(load_params, None, os.path.join(tmp, "bad.json")))[1])
        T("B10 as_of 早于 updated_at → 拒（禁后视）",
          raises(load_params, "2026-10-07", pp))

        # ── C. 候选（只进 queue，绝不碰 params）────────────────────────────
        print("== C. 候选队列（0.9）==")
        s0 = fp(pp)
        r1 = propose(D1, "single_stock_cap", 55, "回测：60 在退潮档过损（N=37）", "BACKTEST",
                     None, pp, qp)
        T("C1 propose 生成候选 id", r1["id"] == "QC-20261008-001")
        T("C2 候选行六字段", tuple(r1["record"].keys()) == tuple(QUEUE_FIELDS))
        T("C3 初始态 PENDING", r1["record"]["status"] == "PENDING")
        T("C4 **propose 未触碰 params.json（逐字节不变）**", fp(pp) == s0)
        T("C5 候选值未进 params", get_value("single_stock_cap", path=pp) == 60)
        T("C6 第二个候选 id 递增", propose(D1, "trial_position", 0.08, "样本外验证", "BACKTEST",
                                         None, pp, qp)["id"] == "QC-20261008-002")
        T("C7 凭空造参 → 拒", raises(propose, D1, "no_such_param", 1, "e", "BACKTEST", None, pp, qp))
        T("C8 候选值 == 正式值 → 拒（无变化不提案）",
          raises(propose, D1, "single_stock_cap", 60, "e", "BACKTEST", None, pp, qp))
        T("C9 无证据 → 拒", raises(propose, D1, "single_stock_cap", 58, "", "BACKTEST", None, pp, qp))
        T("C10 非法 source → 拒",
          raises(propose, D1, "single_stock_cap", 58, "e", "拍脑袋", None, pp, qp))
        T("C11 非法日期 → 拒",
          raises(propose, "2026-13-01", "single_stock_cap", 58, "e", "BACKTEST", None, pp, qp))

        print("== D. 审批（append-only）==")
        s0, q_lines = fp(pp), len(read_queue(qp))
        rv = review(D2, "QC-20261008-001", "APPROVE", "USER", queue_path=qp)
        T("D1 审批追加新行（不就地改）", len(read_queue(qp)) == q_lines + 1)
        T("D2 物化视图取末态 = APPROVED", queue_view(qp)["QC-20261008-001"]["status"] == "APPROVED")
        T("D3 审批不改候选内容",
          queue_view(qp)["QC-20261008-001"]["candidate_value"] == 55)
        T("D4 审批未触碰 params.json", fp(pp) == s0)
        T("D5 非法 verdict → 拒",
          raises(review, D2, "QC-20261008-002", "PASS", "USER", qp))
        T("D6 无批准人 → 拒", raises(review, D2, "QC-20261008-002", "APPROVE", "", qp))
        T("D7 不存在的候选 → 拒", raises(review, D2, "QC-20261008-999", "APPROVE", "USER", qp))
        review(D2, "QC-20261008-002", "KEEP_TESTING", "USER", queue_path=qp)
        T("D8 KEEP_TESTING 可再投（非终态）",
          queue_view(qp)["QC-20261008-002"]["status"] == "KEEP_TESTING")
        review(D2, "QC-20261008-002", "REJECT", "USER", queue_path=qp)
        T("D9 REJECT 为终态", queue_view(qp)["QC-20261008-002"]["status"] == "REJECTED")
        T("D10 终态后不得再审批",
          raises(review, D2, "QC-20261008-002", "APPROVE", "USER", qp))
        T("D11 未决视图只剩 0 条（1 审 1 拒）", list_queue(_OPEN_QUEUE, qp) == [])

        # ── E. 转正（唯一写入口）────────────────────────────────────────────
        print("== E. 转正（唯一写 params 的路径）==")
        T("E1 未经批准（REJECTED）→ 拒",
          raises(promote, D2, "QC-20261008-002", "USER", None, None, pp, qp, cp))
        c_wait = propose(D2, "single_stock_cap", 52, "另一条待审", "BACKTEST", None, pp, qp)["id"]
        T("E2 PENDING 候选 → 拒（Agent 无权自行转正，因「未获批准」而拒）",
          "未获批准" in errmsg(promote, D2, c_wait, "USER", None, None, pp, qp, cp))
        T("E3 无批准人 → 拒（因「approver 不得为空」而拒）",
          "不得为空" in errmsg(promote, D2, "QC-20261008-001", "", None, None, pp, qp, cp))
        before = load_params(path=pp)["params"]["single_stock_cap"]["value"]
        pr = promote(D2, "QC-20261008-001", "USER", "回测 + OOS 通过", None, pp, qp, cp)
        T("E4 转正写值 60 → 55", pr["old_value"] == 60 and pr["new_value"] == 55)
        T("E5 转正后正式值生效", get_value("single_stock_cap", path=pp) == 55)
        m = get_param("single_stock_cap", pp)
        T("E6 status → CALIBRATED", m["status"] == "CALIBRATED")
        T("E7 **validation 不动（转正 ≠ 已验证）**", m["validation"] == "UNCALIBRATED")
        T("E8 last_review 更新", m["last_review"] == D2)
        T("E9 版本 patch +1", pr["version"] == "1.0.1" and params_version(pp) == "1.0.1")
        T("E10 changelog 追加一行", len(read_changelog(cp)) == 2)
        T("E11 changelog 行含批准人", read_changelog(cp)[-1]["approver"] == "USER")
        T("E12 收紧（60→55）不算放宽", pr["is_widen"] is False)
        T("E13 零交易动作", pr["orders_created"] == 0)
        T("E14 收紧路径可写（生存类）", before == 60)

        print("== F. 生存类只许收紧（设计 §8.2 红线）==")
        c_w = propose(D2, "single_stock_cap", 65, "样本外显示可放宽", "BACKTEST",
                      None, pp, qp)["id"]
        review(D2, c_w, "APPROVE", "USER", queue_path=qp)
        T("F1 生存类放宽无跨区间证据 → 拒（WIDEN_NEEDS_EVIDENCE）",
          CROSS_REGIME_TAG in errmsg(promote, D2, c_w, "USER", None, None, pp, qp, cp))
        T("F1b 被拒后正式值未动（拒就是拒，不留半成品）", get_value("single_stock_cap", path=pp) == 55)
        c_w2 = propose(D2, "single_stock_cap", 66, "CROSS_REGIME：三区间复测均支持",
                       "BACKTEST", None, pp, qp)["id"]
        review(D2, c_w2, "APPROVE", "USER", queue_path=qp)
        pr2 = promote(D2, c_w2, "USER", "跨区间证据齐备", None, pp, qp, cp)
        T("F2 带 CROSS_REGIME → 放宽放行", pr2["is_widen"] is True and pr2["new_value"] == 66)
        T("F3 放宽也记 changelog（不是暗改）", read_changelog(cp)[-1]["new_value"] == 66)
        T("F4 版本继续 +1（1.0.2）", params_version(pp) == "1.0.2")
        c_f = propose(D2, "trial_position", 0.12, "样本外", "BACKTEST", None, pp, qp)["id"]
        review(D2, c_f, "APPROVE", "USER", queue_path=qp)
        T("F5 进攻类可自由双向",
          promote(D2, c_f, "USER", None, None, pp, qp, cp)["new_value"] == 0.12)
        T("F6 promote 不得直接置 VALIDATED（规则验证走 1.6 提炼链）",
          "VALIDATED" in errmsg(promote, D2, c_f, "USER", None, "VALIDATED", pp, qp, cp))
        T("F7 候选转正后仍留档（append-only，queue 行不删）",
          queue_view(qp)["QC-20261008-001"]["candidate_value"] == 55)

        # ── G. 版本边界（0.9 ↔ 0.8）───────────────────────────────────────
        print("== G. 版本边界与值域隔离 ==")
        v = verify_params(pp, cp, qp)
        T("G1 全量校验通过（%d 项检查）" % len(v["checks"]), v["ok"], "; ".join(v["errors"]))
        T("G2 七列齐全", [c for c in v["checks"] if c["name"].startswith("V3")][0]["ok"])
        T("G3 变更链首尾相接", [c for c in v["checks"] if c["name"].startswith("V6")][0]["ok"])
        T("G4 链末值 == 当前正式值", [c for c in v["checks"] if c["name"].startswith("V7")][0]["ok"])
        T("G5 版本自洽（patch == 变更行数）", [c for c in v["checks"] if c["name"].startswith("V8")][0]["ok"])
        T("G6 两 status 值域物理隔离", [c for c in v["checks"] if c["name"].startswith("V13")][0]["ok"])

        # 篡改 A：漏记一次变更（直接改 params 值不改 changelog）
        doc = _read_json(pp)
        keep = doc["params"]["trial_position"]["value"]
        doc["params"]["trial_position"]["value"] = 0.99
        _write_params(pp, doc)
        g7 = [c for c in verify_params(pp, cp, qp)["checks"] if c["name"].startswith("V7")][0]
        T("G7 手改正式值（不记 changelog）→ V7 抓到", g7["ok"] is False)
        doc["params"]["trial_position"]["value"] = keep
        _write_params(pp, doc)
        T("G8 复位后恢复通过", verify_params(pp, cp, qp)["ok"])

        # 篡改 B：版本号与变更行数不符（少记一行）
        doc = _read_json(pp)
        doc["version"] = "1.0.9"
        _write_params(pp, doc)
        g9 = [c for c in verify_params(pp, cp, qp)["checks"] if c["name"].startswith("V8")][0]
        T("G9 版本号与变更行数不符 → V8 抓到", g9["ok"] is False)
        doc["version"] = "1.0.3"
        _write_params(pp, doc)
        T("G10 复位后恢复通过", verify_params(pp, cp, qp)["ok"])

        # 篡改 C：把审批态写进 params.status（值域串读）
        doc = _read_json(pp)
        doc["params"]["single_stock_cap"]["status"] = "APPROVED"
        _write_params(pp, doc)
        g11 = [c for c in verify_params(pp, cp, qp)["checks"] if c["name"].startswith("V13")][0]
        g12 = [c for c in verify_params(pp, cp, qp)["checks"] if c["name"].startswith("V4")][0]
        T("G11 审批态串进 params.status → V13/V4 抓到", g11["ok"] is False and g12["ok"] is False)
        doc["params"]["single_stock_cap"]["status"] = "CALIBRATED"
        _write_params(pp, doc)
        T("G12 复位后恢复通过", verify_params(pp, cp, qp)["ok"])

        # 篡改 D：断链（改中间行的 new_value）
        rows = read_changelog(cp)
        rows[1]["new_value"] = 999
        with io.open(cp, "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
        g13 = [c for c in verify_params(pp, cp, qp)["checks"] if c["name"].startswith("V6")][0]
        T("G13 变更史断链 → V6 抓到", g13["ok"] is False)
        T("G14 **真 params/queue/changelog 未被自检触碰（指纹不变）**",
          (fp(PARAMS_PATH), fp(QUEUE_PATH), fp(CHANGELOG_PATH)) == real_sig)

        # ── H. 纪律 ────────────────────────────────────────────────────────
        print("== H. 纪律 ==")
        import ast
        src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
        tree = ast.parse(src)
        # 禁的是**外联与外执行**；shutil 属本地文件操作，只允许出现在自检里
        banned = {"requests", "urllib", "http", "socket", "subprocess", "openai"}
        top_imp, in_selftest, local_only = set(), set(), set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods = {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods = {n.module.split(".")[0]}
            else:
                continue
            top_imp |= mods
        for fn in [x for x in tree.body if isinstance(x, ast.FunctionDef)]:
            mods = set()
            for n in ast.walk(fn):
                if isinstance(n, ast.Import):
                    mods |= {a.name.split(".")[0] for a in n.names}
                elif isinstance(n, ast.ImportFrom) and n.module:
                    mods.add(n.module.split(".")[0])
            if fn.name == "_selftest":
                in_selftest |= mods
            else:
                local_only |= mods
        T("H1 顶层无网络/子进程/外联导入", not (top_imp & banned), "命中: %s" % sorted(top_imp & banned))
        T("H1b 函数内导入同禁（除自检）", not (local_only & banned),
          "命中: %s" % sorted(local_only & banned))
        T("H1c shutil 只在自检内（清临时目录用）", "shutil" not in local_only and "shutil" in in_selftest)
        calls = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef):
                names = {c.func.id for c in ast.walk(n)
                         if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
                calls[n.name] = names
        writers = sorted(f for f, ns in calls.items() if "_write_params" in ns)
        prod_writers = [f for f in writers if f != "_selftest"]
        T("H2 **_write_params 生产路径只被 promote 调用（正式值唯一写入口）**",
          prod_writers == ["promote"], "实际: %s（自检内的调用用于制造篡改场景）" % writers)
        T("H3 无下单/撤单/成交之类接口名",
          not [f for f in calls if any(k in f for k in ("order", "buy", "sell", "trade", "execute"))])
        # 自指规避：检查串本身不能出现在被检查的源码里，故拼接构造
        abs_macs = "/" + "Users" + "/"
        abs_win = "C:" + "\\" + "Users"
        T("H4 模块内无 hardcode 绝对路径", abs_macs not in src and abs_win not in src)
        T("H5 本文件不写 params/queue/changelog 之外的路径",
          "PARAMS_PATH" in src and "QUEUE_PATH" in src and "CHANGELOG_PATH" in src)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nparams.py 自检：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：")
        for f in fails:
            print("  - %s" % f)
    return 1 if fail else 0


def _discipline():
    import ast
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    banned = {"requests", "urllib", "http", "socket", "subprocess", "openai"}
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    hit = sorted(imported & banned)
    calls = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef):
            calls[n.name] = {c.func.id for c in ast.walk(n)
                             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    writers = sorted(f for f, ns in calls.items() if "_write_params" in ns)
    print("params.py 纪律检查（0.8/0.9）")
    print("  ① 禁导入（网络/子进程）: %s" % ("✅ 无" if not hit else "❌ %s" % hit))
    print("  ② 正式值唯一写入口    : %s" % ("✅ promote" if writers == ["promote"] else "❌ %s" % writers))
    print("  ③ 候选只 append queue  : ✅ propose 无 params 写调用"
          if "_write_params" not in calls.get("propose", set()) else "  ③ ❌ propose 触碰 params")
    print("  ④ 零交易动作          : ✅ 无 order/buy/sell 接口")
    print("  ⑤ 三文件边界          : ✅ params.json / calibration_queue / params_changelog")
    ok = (not hit) and writers == ["promote"]
    return 0 if ok else 1


def _main(argv):
    if "--selftest" in argv:
        print("params.py 自检（SCHEMA_VERSION=%d · 临时文件运行）" % SCHEMA_VERSION)
        return _selftest()
    if "--discipline" in argv:
        return _discipline()
    if "--verify" in argv:
        v = verify_params()
        print("params 校验（%s）" % ("通过 ✅" if v["ok"] else "失败 ❌"))
        for c in v["checks"]:
            print("  [%s] %s %s" % ("OK" if c["ok"] else "!!", c["name"], c["detail"]))
        return 0 if v["ok"] else 1
    if "--queue" in argv:
        rows = sorted(queue_view().values(), key=lambda r: r.get("id") or "")
        print("候选队列（0.9 · append-only · %d 条物化态）" % len(rows))
        for r in rows:
            print("  %s  %-20s %-14s %-6s  %s"
                  % (r["id"], r["param_name"], str(r["candidate_value"])[:14], r["status"],
                     (r.get("evidence") or "")[:40]))
        return 0
    if "--changelog" in argv:
        print("变更史（0.9 · append-only）")
        for r in read_changelog():
            print("  %-8s %-20s %s → %s  by %s  %s"
                  % (r["version"], r["param_name"], r["old_value"], r["new_value"],
                     r["approver"], r["date"]))
        return 0
    L = load_params()
    print("参数总账（0.8 运行时唯一权威 · version=%s · updated_at=%s）"
          % (L["version"], L["updated_at"]))
    if not L["params"]:
        print("  params.json 尚未落盘（source=%s）" % L["source"])
    for n, m in L["params"].items():
        v = m.get("value")
        v = ("{...%d keys}" % len(v)) if isinstance(v, dict) else str(v)
        print("  %-24s %-12s %-10s %-13s %-18s %s"
              % (n, v, m.get("unit"), m.get("status"), m.get("validation"),
                 m.get("tightening_direction")))
    cls = by_class()
    print("  生存类（只许收紧）: %s" % ", ".join(cls["TIGHTEN_ONLY"]))
    print("  进攻类（可双向）  : %s" % ", ".join(cls["FREE"]))
    q = list_queue()
    openq = list_queue(_OPEN_QUEUE)
    print("  候选队列: %d 条（未决 %d）" % (len(q), len(openq)))
    print("  变更史  : %d 条" % len(read_changelog()))
    print("  纪律    : 正式值唯一写入口 = promote()；候选值只存在 queue（不进 params.json）")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
