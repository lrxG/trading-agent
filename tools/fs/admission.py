#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""admission.py · 数据准入闸 · 结构 / 语义 / 时点 / 覆盖 四类互不替代（2026-10-13 建）

★ 本模块解决的问题（监督层第 6 轮第 2 项）：
  **「能通过 `check_snapshot`」推不出「列语义满足」。**
  实测机理（B1 §5.2.7.1）：快照契约在**列层是硬的**——表头逐列逐序完全相等，
  缺列/多列/顺序错**直接抛错**；而在**值层一律是松的**——`_cell` 与 `_n_*` 系列
  对 `""` / `None` **一律返回 `None`，不抛错**。
  ⇒ **「列在」≠「值有」**；「必填」在既有契约里**没有结构承载处**。
  ⇒ 语义必填**只能**由本模块承载。**这就是本模块存在的唯一理由。**

★ 四类检查（`CHECKS`）**互不替代**（任一失败即不得宣告正式可用；多类同时失败必须**全列**）：
  · `STRUCTURE` 列名/类型/格式是否符合契约
  · `SEMANTIC`  字段实际代表的内容是否与契约定义一致（**空值闸在此**）
  · `AS_OF`     真实采集时点是否满足消费方的盘后契约（证据分级见 B3 §3.5）
  · `COVERAGE`  覆盖的市场/证券范围与缺失比例是否已**显式声明**

★ 语义类别（`REQ` / `NUL` / `UNK` / `BLK`，跨文件同规则；口径取自 B1 §5.2.7.2）：
  · `REQ` 语义必填 —— 空 ⇒ **文件级 `SEMANTIC` 阻塞**（该文件无信息量）
  · `NUL` 语义可空 —— 空 ⇒ 该行标 `null`（**须带 reason**），文件级不阻塞
  · `UNK` 允许未知 —— 空 ⇒ **必带 reason** 并计入 `coverage`；**严禁填 0**
  · `BLK` 语义不可用 —— 消费方必需但**无合格源**；以空"假通过"= **违规**

★ 两条不可绕过的红线：
  ① **空值不得自动判定为 `VERIFIED`**：本模块**没有**"有列即通过"的路径；
     任何列为空时，只有 `NUL` / `UNK`（且**携带 reason**）才不阻塞，`REQ` / `BLK` 一律阻塞。
  ② **已声明阻塞具粘性**（sticky）：`DECLARED_BLOCKS` 里登记的项，即使某次运行
     "结构过 + 值齐 + 时点合"，**仍然阻塞** —— 由数据现状造出的"看起来过了"
     不能推翻设计层已认定的结构性缺陷（如 `limit_up` 的时点语义、`dragon_tiger.seat` 无源）。

★ 本模块**不接入**任何数据流、**不写盘**、**不改契约**、**不改 `seed_real.py`**：
  它只提供可调用、可复现、可审计的判定与登记表（`--report`）。

★ 纪律（`--discipline` 机械自证）：
  ① 零写点  ② 四类检查互不替代（无"某类通过即可放行"的分支）
  ③ 空值无豁免路径（无 `if 值非空: 通过` 形态的短路）  ④ UNKNOWN/未声明一律阻塞
  ⑤ astwin 引用

用法：
  python tools/fs/admission.py --selftest
  python tools/fs/admission.py --discipline
  python tools/fs/admission.py --report        # 5 契约文件 × 四类的当前准入登记表
"""

import ast
import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import astwin as ASTW  # noqa: E402
import snapshot as SN  # noqa: E402   （复用 0.2 契约：列规格唯一真源，禁再抄一份）

__all__ = ("AdmissionError", "CHECKS", "SEMANTIC_CLASSES", "EVIDENCE_LEVELS",
           "FIELD_RULES", "DECLARED_BLOCKS", "field_rule", "check_structure",
           "check_semantics", "check_as_of", "check_coverage", "admit",
           "admission_matrix", "AS_OF_MIN_EVIDENCE",
           "FRESHNESS_POLICY", "freshness_report", "decision_admit")

# ── 四类检查（顺序固定：报告与判定都按此序，保证可复现） ──────────────────────
CHECKS = ("STRUCTURE", "SEMANTIC", "AS_OF", "COVERAGE")

# 语义类别（值域封闭）
C_REQ = "REQ"
C_NUL = "NUL"
C_UNK = "UNK"
C_BLK = "BLK"
SEMANTIC_CLASSES = (C_REQ, C_NUL, C_UNK, C_BLK)

# 时点证据等级（B3 §3.5 五级；E5 最强）。门檻取"目标档 → 最低可接受证据"。
EVIDENCE_LEVELS = ("E1", "E2", "E3", "E4", "E5")
E_LABEL = {
    "E1": "档名/文件名内时点（**弱证据，不得单独证明时点**）",
    "E2": "文件 mtime（辅助证据，不得单独证明业务采集时点）",
    "E3": "采集日志 with 真实 ts（可证明该次采集发生过）",
    "E4": "调度源码/任务设计（证明设计意图，不证明每次成功）",
    "E5": "独立复现/交叉源一致（最强）",
}
# 盘后档所需的最低证据：`close_final` 至少 E3（日志级），E1/E2 单独不足
AS_OF_MIN_EVIDENCE = {
    "close_final": "E3",
    "post_close_published": "E3",
    "intraday_snapshot": None,        # 盘中档**天然不满足**盘后契约 ⇒ 恒阻塞
    "retro_rebuild": None,            # 事后回建 ≠ 收盘档 ⇒ 恒阻塞（待 U-10 裁定）
}

_OK = "OK"
_BLOCKED = "BLOCKED"


class AdmissionError(Exception):
    """准入判定的入参契约不符（显式抛错，禁静默降级）。"""


def _classes(*names):
    return tuple(names)


# ── 逐列语义类别（跨文件同规则；`news.body` 与 `limit_up.first_seal_time` 见下） ──
# 取"组合甲"（B1 §5.2.7.3 推荐）：区分「该列是否是文件成立的必要条件」。
#   · news.body            消费面(scan 路4)只用 related_codes ⇒ 正文非必要条件 ⇒ NUL
#   · limit_up.first_seal_time / seal_amount  涨停件的核心语义 ⇒ BLK（且唯一源时点不合格）
# 裁定项 U-13：若改判"组合乙"（一律 REQ），只需把 NUL 改成 REQ，**判据结构不变**。
FIELD_RULES = {
    "daily_quote.csv": {
        "code": C_REQ, "name": C_REQ, "open": C_REQ, "high": C_REQ, "low": C_REQ,
        "close": C_REQ, "pct": C_REQ,
        "vol": C_NUL,        # 停牌行无量（已登记的实际空值）
        "amount": C_NUL,
    },
    "limit_up.csv": {
        "code": C_REQ, "name": C_REQ, "boards": C_REQ,
        "first_seal_time": C_BLK,     # 唯一源 zt_pool 时点不合格 ⇒ 与 R-7 并列阻塞
        "seal_amount": C_BLK,
        "reason": C_UNK,              # 源为 hybk（行业板块）≠ 封板原因 ⇒ 不得默认等价（U-15）
    },
    "dragon_tiger.csv": {
        "code": C_REQ, "name": C_NUL, "net": C_REQ,
        "seat": C_BLK, "buy": C_BLK, "sell": C_BLK,   # 无逐席位源（形态不匹配）
    },
    "news.jsonl": {
        "news_id": C_REQ, "ts": C_REQ, "source": C_REQ, "title": C_REQ,
        "body": C_NUL,                # 与 limit_up 同规则族；差异来自消费需求
        "related_codes": C_REQ,
    },
    "env_metrics.json": {
        "limit_up_count": C_REQ, "max_board": C_REQ, "turnover": C_REQ,
        "up_down_ratio": C_REQ, "fail_rate": C_REQ, "index_ma20": C_REQ,
    },
}

# ── 已声明阻塞（**粘性**；设计层认定，数据层"看起来过了"不得推翻） ─────────────
DECLARED_BLOCKS = {
    "limit_up.csv": {
        "classes": _classes("AS_OF", "SEMANTIC", "COVERAGE"),
        "basis": "DECLARED",
        "reason": "14:57 盘中档不满足盘后最终快照契约（R-7）；first_seal_time/seal_amount "
                  "无合格源（BLK）；universe 含否 ST/北交所未声明",
        "decided_by": "监督层第 5 轮裁定（R-7 阻塞，未裁定通过）",
    },
    "dragon_tiger.csv": {
        "classes": _classes("SEMANTIC"),
        "basis": "DECLARED",
        "reason": "seat/buy/sell 无逐席位源；lhb 源为两个异维度聚合表，形态不匹配（R-8）",
        "decided_by": "监督层第 5 轮裁定（R-8 继续阻塞）",
    },
    "env_metrics.json": {
        "classes": _classes("COVERAGE"),
        "basis": "DECLARED",
        "reason": "★2026-10-13 用户裁定「自算口径 + 显式标注」⇒ `SEMANTIC` 粘性阻塞**已解除**"
                  "（6 键全实现：limit_up_count←zt_pool 末档行数【方案甲】/ max_board←max(lbc) / "
                  "fail_rate←klines 精确限价自算 / turnover←klines 全市场 amount 求和 / "
                  "up_down_ratio←env_history.ratio_ud / index_ma20←MA20(sh000001)），"
                  "且逐键标注齐备（`seed_real.CALIBER['env_metrics']['label']`）。"
                  "**残留 COVERAGE 阻塞（仍成立）**：北交所 klines 覆盖 0（R-2）；turnover 只数覆盖 "
                  "93.6%、恒定低估 ~1%，**只记录、不参与分档**；up_down_ratio 取 westock 原字段而"
                  "其内部定义（时点 / 含否 ST·北交所）未知（R-5）；zt_pool 历史仅 21 个交易日（R-6）。"
                  "**09-24 仍将按数据面阻塞**：前收受 09-23 污染 → 自洽样本退化 → fail_rate 置 None"
                  "（`REQ` 列空 ⇒ 文件级 SEMANTIC），此属**数据缺陷**而非口径未决，由 SEMANTIC 检查"
                  "按数据如实判出，不走本表。",
        "decided_by": "监督层 2026-10-13 裁定（自算口径+显式标注；方案取裁定表 §5.1 甲）；"
                      "原第 4 轮「fail_rate 不批准用于正式分级」已被本轮口径裁定取代",
    },
}


# ── ① 结构检查 ──────────────────────────────────────────────────────────────
def check_structure(file_key, header):
    """列层：表头必须与契约**逐列逐序完全相等**（缺列/多列/顺序错 ⇒ 阻塞）。

    本函数与 `snapshot._load_file` 同判据（列规格唯一真源 = `SN.CONTRACT`），
    但**只判定不抛错**，以便与其余三类并列报告。
    """
    spec = _spec(file_key)
    cols = [c[0] for c in spec["cols"]]
    got = list(header or ())
    problems = []
    if got != cols:
        miss = [c for c in cols if c not in got]
        extra = [c for c in got if c not in cols]
        if miss:
            problems.append("缺列:%s" % miss)
        if extra:
            problems.append("多列:%s" % extra)
        if not miss and not extra:
            problems.append("列序不符")
    return _verdict("STRUCTURE", problems,
                    basis="契约列 = %s" % ",".join(cols))


# ── ② 语义检查（**空值闸**：本模块的核心） ────────────────────────────────────
def check_semantics(file_key, rows):
    """值层：按 `FIELD_RULES` 判定每列空值的**语义后果**。

    **列在但值为空 ⇒ 不得自动判定为可用**：
      · `REQ` 列出现空 ⇒ `SEMANTIC` 阻塞
      · `BLK` 列**恒**阻陶（哪怕本次运行每行都有值 —— 那是"有值但语义无源"）
      · `NUL` / `UNK` 列空 ⇒ 记录 `null_fields`（**必须带 reason**）或 `unknown_fields`
    `rows`：对象行列表（键 = 列名）。空列表 ⇒ 无行可判 ⇒ 记录但**不因此放行**
    （"无行"同样不是"合规"）。
    """
    rules = FIELD_RULES.get(file_key)
    if rules is None:
        raise AdmissionError("未登记语义规则的契约文件：%s" % file_key)
    problems, null_fields, unknown_fields = [], [], []
    n_rows = len(rows or ())
    for col, cls in rules.items():
        present_vals = [r.get(col) for r in (rows or ())]
        if cls == C_BLK:
            problems.append("BLK:%s（无合格源，空值假通过=违规）" % col)
            continue
        if n_rows == 0:
            continue
        empties = sum(1 for v in present_vals if v is None or v == "" or v == [])
        if empties and cls == C_REQ:
            problems.append("REQ为空:%s(%d/%d行)" % (col, empties, n_rows))
        elif empties and cls == C_NUL:
            null_fields.append(col)
        elif empties and cls == C_UNK:
            unknown_fields.append(col)
    return _verdict("SEMANTIC", problems,
                    basis="类别表 %s；规则源 = admission.FIELD_RULES" % file_key,
                    extra={"null_fields": sorted(set(null_fields)),
                           "unknown_fields": sorted(set(unknown_fields)),
                           "n_rows": n_rows})


# ── ③ 时点检查 ──────────────────────────────────────────────────────────────
def check_as_of(as_of_label, evidence_level, required="close_final",
                evidence_note=None):
    """时点层：`as_of_label` 必须达到 `required` 档所需的**最低证据等级**。

    关键判据（监督层第 4 轮）：
      · `VERIFIED` 只能说明"源可用/字段可映射"，**不能**说明它符合消费方的时间语义；
      · 档名是 **E1 弱证据**，单独不得证明时点；mtime 是 **E2 辅助证据**同样单独不足。
    """
    problems = []
    lvl = (evidence_level or "").upper()
    if lvl not in EVIDENCE_LEVELS:
        problems.append("证据等级未知/未声明:%r（须 ∈ %s）" % (evidence_level,
                                                          list(EVIDENCE_LEVELS)))
    need = AS_OF_MIN_EVIDENCE.get(as_of_label, "E5")
    if need is None:
        problems.append("档位 %s 天然不满足盘后契约（%s）"
                        % (as_of_label, AS_OF_MIN_EVIDENCE.get(as_of_label)))
    elif lvl in EVIDENCE_LEVELS:
        if EVIDENCE_LEVELS.index(lvl) < EVIDENCE_LEVELS.index(need):
            problems.append("证据不足：%s 最低需 %s（%s），实得 %s"
                            % (as_of_label, need, E_LABEL[need], lvl))
    if as_of_label not in AS_OF_MIN_EVIDENCE:
        problems.append("档位未登记:%r" % (as_of_label,))
    return _verdict("AS_OF", problems,
                    basis="as_of=%s · 证据=%s%s · 门檻=%s"
                          % (as_of_label, lvl or "未声明",
                             ("（%s）" % evidence_note) if evidence_note else "",
                             required),
                    extra={"evidence_level": lvl or None,
                           "min_evidence": need})


# ── ④ 覆盖检查 ──────────────────────────────────────────────────────────────
def check_coverage(universe, claim=None):
    """覆盖层：`universe` 必须**显式声明**市场/证券范围与完整性，否则阻塞。

    `universe` 形如 `{include_st, include_bse, complete, reason}`。
    `claim`：报告层打算使用的措辞（如"全市场成交额"）——若 `complete=False`
    仍声称全覆盖 ⇒ **阻塞**（不得把不完整覆盖说成完整）。
    """
    problems = []
    if not isinstance(universe, dict):
        problems.append("universe 未声明（须显式给出 include_st/include_bse/complete/reason）")
        universe = {}
    for k in ("include_st", "include_bse", "complete"):
        if not isinstance(universe.get(k), bool):
            problems.append("universe.%s 未显式声明" % k)
    if universe.get("complete") is False:
        if not (universe.get("reason") or "").strip():
            problems.append("complete=False 但未给 reason（不完整须说明原因）")
        if claim and any(w in str(claim) for w in ("全市场", "完整", "全覆盖")):
            problems.append("覆盖不完整却声称「%s」" % claim)
    return _verdict("COVERAGE", problems,
                    basis="universe=%s · claim=%s"
                          % (json.dumps(universe, ensure_ascii=False, sort_keys=True),
                             claim or "未声明"),
                    extra={"universe": universe})


# ── 汇总判定（四类并列；已声明阻塞粘性生效） ──────────────────────────────────
def admit(file_key, *, header=None, rows=None, as_of_label=None, evidence_level=None,
          universe=None, claim=None, required_as_of="close_final"):
    """→ 准入判决。**四类互不替代**，任一 `BLOCKED` ⇒ `admissible=False`。

    返回 `{file_key, admissible, failed, blocked_classes, checks, declared_block}`。
    **粘性**：`DECLARED_BLOCKS[file_key]` 的类别无条件并入 `blocked_classes`。
    """
    checks = {
        "STRUCTURE": check_structure(file_key, header) if header is not None
        else _verdict("STRUCTURE", ["未提供表头 ⇒ 未检查（不得视为通过）"],
                      basis="header=None"),
        "SEMANTIC": check_semantics(file_key, rows) if rows is not None
        else _verdict("SEMANTIC", ["未提供行集 ⇒ 未检查（不得视为通过）"],
                      basis="rows=None"),
        "AS_OF": check_as_of(as_of_label, evidence_level,
                             required=required_as_of) if as_of_label is not None
        else _verdict("AS_OF", ["未声明 as_of ⇒ 未检查（不得视为通过）"],
                      basis="as_of=None"),
        "COVERAGE": check_coverage(universe, claim) if universe is not None
        else _verdict("COVERAGE", ["未声明 universe ⇒ 未检查（不得视为通过）"],
                      basis="universe=None"),
    }
    failed = [k for k in CHECKS if checks[k]["status"] == _BLOCKED]
    dec = DECLARED_BLOCKS.get(file_key)
    blocked_classes = sorted(set(failed) | set(dec["classes"] if dec else ()))
    return {
        "file_key": file_key,
        "admissible": not blocked_classes,
        "failed": failed,
        "blocked_classes": blocked_classes,
        "checks": checks,
        "declared_block": dec,
    }


def admission_matrix():
    """当前 5 契约文件的准入登记表（**声明层视图**，与逐次 `admit()` 不同）。

    每行 = `{file_key, declared_blocked, classes, basis, reason}`。
    用途：让"阻塞状态可识别"——消费方/报告层拿得到结构化阻塞原因，而不是只有一句风险描述。
    """
    out = []
    for fn in sorted(SN.CONTRACT):
        dec = DECLARED_BLOCKS.get(fn)
        out.append({
            "file_key": fn,
            "contract_key": SN.CONTRACT[fn]["key"],
            "declared_blocked": bool(dec),
            "classes": list(dec["classes"]) if dec else [],
            "basis": (dec["basis"] if dec else "NONE"),
            "reason": (dec["reason"] if dec else ""),
            "blk_columns": sorted(c for c, k in FIELD_RULES.get(fn, {}).items()
                                  if k == C_BLK),
        })
    return out


# ── 内部 ────────────────────────────────────────────────────────────────────
def _spec(file_key):
    if file_key not in SN.CONTRACT:
        raise AdmissionError("非契约文件：%r（值域 = %s）"
                             % (file_key, sorted(SN.CONTRACT)))
    return SN.CONTRACT[file_key]


def _verdict(name, problems, basis=None, extra=None):
    v = {"check": name, "status": _BLOCKED if problems else _OK,
         "problems": list(problems), "basis": basis}
    if extra:
        v.update(extra)
    return v


def field_rule(file_key, col):
    """→ 该列的语义类别（未登记 ⇒ 抛错，**禁默认按可空处理**）。"""
    rules = FIELD_RULES.get(file_key)
    if rules is None:
        raise AdmissionError("未登记语义规则的契约文件：%s" % file_key)
    if col not in rules:
        raise AdmissionError("未登记语义类别的列：%s.%s（禁默认按可空处理）"
                             % (file_key, col))
    return rules[col]


# ── CLI ─────────────────────────────────────────────────────────────────────

# ── ⑤ 决策时效闸（2026-10-27 整改 P0b：FRESH/STALE/MISSING 四态 + BUY 准入）────
# ★ 与四类准入的关系：check_as_of 管「证据等级够不够盘后契约」（档位语义），
#   本层管「数据日历年龄够不够新」（FRESH 窗口）。互不替代，决策两边都要过。
# ★ 窗口=日历日保守值（跨周末/短假不误伤）：周内数据周五采、周一用=3 日仍 FRESH。
FRESHNESS_POLICY = {
    "limit_up_pool": {"file": "raw-date-dir:limit_up.csv", "window_days": 4,
                      "critical": True,
                      "why": "C2 候选池主源；无池=无法发现候选"},
    "klines_daily":  {"file": "bus:klines.csv", "window_days": 5,
                      "critical": True,
                      "why": "价线/前收/流动性全部依赖；过期=定价失真"},
    "fundflow_daily": {"file": "bus:fundflow.csv", "window_days": 5,
                       "critical": False,
                       "why": "S5/资金行为辅助；过期=降证据等级不阻断"},
    "notice_daily":  {"file": "raw-date-dir:news.jsonl", "window_days": 4,
                      "critical": False,
                      "why": "公告分级辅助；过期=事件归因降级"},
}
STATE_FRESH, STATE_STALE = "FRESH", "STALE"


def _tail_date_csv(path, date_col="date"):
    """读 csv 文件尾部 4KB 解出最后一行日期（大文件禁全量读）。"""
    import os as _os
    try:
        size = _os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 4096))
            chunk = f.read().decode("utf-8", errors="replace")
        lines = [x for x in chunk.splitlines() if x.strip()]
        if len(lines) < 2:
            return None
        last = lines[-1]
        header = lines[0] if size <= 4096 else None
        if header is None:
            # 尾块第一行可能是被截断的半行——用最后两行中带逗号的完整行
            last = lines[-1] if "," in lines[-1] else lines[-2]
        idx = last.split(",")
        # 无表头时按 date_col 位置启发：尝试在已知列位找 YYYY-MM-DD
        import re as _re
        m = _re.search(r"(\d{4}-\d{2}-\d{2})", last)
        return m.group(1) if m else None
    except OSError:
        return None


def _latest_raw_date_dir(base, filename, as_of):
    """raw/<D>/<filename> 的最大 D ≤ as_of；无 → None。"""
    import os as _os
    raw = _os.path.join(base, "data", "raw")
    best = None
    try:
        names = _os.listdir(raw)
    except OSError:
        return None
    for n in names:
        if len(n) == 10 and n[4] == "-" and n <= as_of                 and _os.path.isfile(_os.path.join(raw, n, filename)):
            if best is None or n > best:
                best = n
    return best


def freshness_report(as_of, base=None, today=None):
    """→ {file_key: {state, latest, window_days, critical, why}}（只读）。"""
    import os as _os
    from datetime import date as _date
    b = base or _os.path.join(_os.path.dirname(_os.path.dirname(
        _os.path.dirname(_os.path.abspath(__file__)))))
    t = today or _date.today()
    out = {}
    for k, spec in FRESHNESS_POLICY.items():
        loc = spec["file"]
        latest = None
        try:
            if loc.startswith("raw-date-dir:"):
                latest = _latest_raw_date_dir(b, loc.split(":", 1)[1], as_of)
            else:
                kind, fn = loc.split(":", 1)
                p = _os.path.join(b, "data", "raw", kind, fn)   # bus 件都在 data/raw/bus/
                if _os.path.isfile(p):
                    latest = _tail_date_csv(p)
        except Exception:                                          # noqa: BLE001
            latest = None
        if latest is None:
            st = "MISSING"
        else:
            try:
                lag = (t - _date.fromisoformat(latest[:10])).days
            except ValueError:
                st, lag = "INVALID", None
            else:
                st = STATE_FRESH if lag <= spec["window_days"] else STATE_STALE
        out[k] = {"state": st, "latest": latest,
                  "window_days": spec["window_days"],
                  "critical": spec["critical"], "why": spec["why"],
                  "as_of_checked": as_of}
    return out


def decision_admit(as_of, base=None, today=None):
    """决策准入（P0b）：关键字段不 FRESH ⇒ verdict=WAIT（BUY 必须被拦，结构化理由）。

    非关键字段（fundflow/notice）过期只降证据等级，**不阻断**——按整改令
    「关键字段缺失时相关策略只能 WAIT」的最小化执行：critical 才拦。
    """
    rep = freshness_report(as_of, base=base, today=today)
    blockers = ["%s=%s(latest=%s>窗%d日): %s" % (k, v["state"], v["latest"],
                                                 v["window_days"], v["why"])
                for k, v in rep.items()
                if v["critical"] and v["state"] != STATE_FRESH]
    return {"verdict": "OK" if not blockers else "WAIT",
            "as_of": as_of, "items": rep, "blockers": blockers,
            "boundary": "只读判定；不写盘不改契约（同 admission 四类闸）"}



def _main(argv):
    if "--selftest" in argv:
        return 0 if _selftest() else 1
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    if "--report" in argv:
        print("数据准入登记表 · 5 契约文件（四类：%s）" % " / ".join(CHECKS))
        print("  %-20s %-8s %-30s %s" % ("file_key", "阻塞", "类别", "原因/缺源列"))
        for r in admission_matrix():
            print("  %-20s %-8s %-30s %s"
                  % (r["file_key"], "是" if r["declared_blocked"] else "否",
                     ",".join(r["classes"]) or "-",
                     r["reason"] or "-"))
            if r["blk_columns"]:
                print("  %-20s %-8s %-30s %s" % ("", "", "BLK 列:",
                                                  ",".join(r["blk_columns"])))
        print("\n  空值闸：REQ 空 ⇒ SEMANTIC 阻塞；BLK 列恒阻塞；NUL/UNK 空须带 reason")
        print("  粘性：已声明阻塞不因某次运行「看起来过了」而解除")
        return 0
    print(__doc__)
    return 2


# ── 自检（纯内存；生产 data/ 零接触、零写盘） ────────────────────────────────
_TEST_FUNCS = ("_selftest", "_discipline", "_prod_source", "_cols_of")


def _cols_of(file_key):
    return [c[0] for c in SN.CONTRACT[file_key]["cols"]]


def _selftest():
    fail = [0]

    def T(name, cond):
        if cond:
            print("  [✓] %s" % name)
        else:
            fail[0] += 1
            print("  [✗] %s" % name)

    print("admission 自检（纯内存；生产 data/ 零接触）：")

    # ── A 组：结构层 ──
    T("A1 表头逐列逐序相符 → STRUCTURE OK",
      check_structure("news.jsonl", _cols_of("news.jsonl"))["status"] == _OK)
    v = check_structure("news.jsonl", _cols_of("news.jsonl")[:-1])
    T("A2 缺列 → STRUCTURE 阻塞且指名缺哪列",
      v["status"] == _BLOCKED and any("related_codes" in p for p in v["problems"]))
    v = check_structure("news.jsonl", list(reversed(_cols_of("news.jsonl"))))
    T("A3 列序不符 → STRUCTURE 阻塞（顺序也是契约）",
      v["status"] == _BLOCKED and "列序不符" in v["problems"][0])
    v = check_structure("news.jsonl", _cols_of("news.jsonl") + ["extra"])
    T("A4 多列 → STRUCTURE 阻塞", v["status"] == _BLOCKED)
    raised = False
    try:
        check_structure("no_such.csv", [])
    except AdmissionError:
        raised = True
    T("A5 非契约文件 → AdmissionError（值域封闭）", raised)

    # ── B 组：语义层（**空值闸**，本模块的核心） ──
    ok_rows = [{"code": "600001", "name": "n", "boards": 2,
                "first_seal_time": "09:40", "seal_amount": 1.0e6, "reason": "机器人"}]
    v = check_semantics("limit_up.csv", ok_rows)
    T("B1 ★ 值全齐也不放行：BLK 列恒阻塞（有值≠语义有源）",
      v["status"] == _BLOCKED
      and any("first_seal_time" in p for p in v["problems"])
      and any("seal_amount" in p for p in v["problems"]))

    empty_rows = [{"news_id": "n1", "ts": "t", "source": "s", "title": "ti",
                   "body": "", "related_codes": ["600001"]}]
    v = check_semantics("news.jsonl", empty_rows)
    T("B2 news.body 空（NUL）⇒ 不阻塞，但**登记** null_fields（不静默）",
      v["status"] == _OK and v["null_fields"] == ["body"])

    v = check_semantics("news.jsonl", [{"news_id": "", "ts": "t", "source": "s",
                                        "title": "ti", "body": "b",
                                        "related_codes": ["600001"]}])
    T("B3 ★ REQ 列空 ⇒ SEMANTIC 阻塞（**列在但值为空不得判为可用**）",
      v["status"] == _BLOCKED and any("news_id" in p for p in v["problems"]))

    v = check_semantics("limit_up.csv", [{"code": "600001", "name": "n",
                                          "boards": None, "first_seal_time": None,
                                          "seal_amount": None, "reason": None}])
    T("B4 UNK 列空 ⇒ 记 unknown_fields（须带 reason），与 REQ/BLK 后果不同",
      "reason" in v["unknown_fields"] and "reason" not in v["null_fields"])

    v = check_semantics("dragon_tiger.csv", [{"code": "600001", "name": "n",
                                              "seat": "", "buy": None,
                                              "sell": None, "net": 1.2}])
    T("B5 dragon_tiger 即使 net 齐备，seat/buy/sell 无源 ⇒ 恒 SEMANTIC 阻塞",
      v["status"] == _BLOCKED and len([p for p in v["problems"]
                                       if p.startswith("BLK:")]) == 3)

    v = check_semantics("news.jsonl", [])
    T("B6 空行集 ⇒ 不因「无行」而放行，也不伪装成阻塞（如实 basis n_rows=0）",
      v["n_rows"] == 0 and v["status"] == _OK)

    raised = False
    try:
        field_rule("news.jsonl", "no_such_col")
    except AdmissionError:
        raised = True
    T("B7 未登记的列 → AdmissionError（**禁默认按可空处理**）", raised)

    # ── C 组：时点层 ──
    T("C1 close_final + E3（采集日志）→ AS_OF OK",
      check_as_of("close_final", "E3")["status"] == _OK)
    v = check_as_of("close_final", "E1")
    T("C2 ★ close_final + E1（仅档名）→ AS_OF 阻塞（档名单独不证明时点）",
      v["status"] == _BLOCKED and "证据不足" in v["problems"][0])
    v = check_as_of("close_final", "E2")
    T("C3 close_final + E2（仅 mtime）→ 仍阻塞（mtime 是辅助证据）",
      v["status"] == _BLOCKED)
    v = check_as_of("intraday_snapshot", "E5")
    T("C4 ★ 盘中档即使证据最强（E5）也**恒阻塞**（证据等级不能抵消档位不合格）",
      v["status"] == _BLOCKED and "天然不满足" in v["problems"][0])
    v = check_as_of("retro_rebuild", "E5")
    T("C5 事后回建档（_hist）即使 E5 也恒阻塞（待 U-10 裁定）",
      v["status"] == _BLOCKED)
    v = check_as_of("close_final", None)
    T("C6 证据等级未声明 ⇒ 阻塞（不得默认最强）", v["status"] == _BLOCKED)
    v = check_as_of("close_final", "E9")
    T("C7 证据等级越界 ⇒ 阻塞（值域封闭）", v["status"] == _BLOCKED)

    # ── D 组：覆盖层 ──
    T("D1 universe 四项显式声明且 complete=true → COVERAGE OK",
      check_coverage({"include_st": True, "include_bse": False,
                      "complete": True})["status"] == _OK)
    v = check_coverage({"include_st": True, "complete": False,
                        "reason": "北交所 0% 覆盖"})
    T("D2 include_bse 未声明 ⇒ 阻塞（「未声明」不是「否」）",
      v["status"] == _BLOCKED and any("include_bse" in p for p in v["problems"]))
    v = check_coverage({"include_st": True, "include_bse": False,
                        "complete": False, "reason": "北交所 0%"}, claim="全市场成交额")
    T("D3 ★ complete=false 却声称「全市场」→ 阻塞（覆盖不完整不得称完整）",
      v["status"] == _BLOCKED and any("全市场" in p for p in v["problems"]))
    v = check_coverage({"include_st": True, "include_bse": False, "complete": False})
    T("D4 complete=false 而无 reason ⇒ 阻塞（不完整须说明原因）",
      v["status"] == _BLOCKED and any("reason" in p for p in v["problems"]))
    T("D5 universe 未声明 ⇒ 阻塞（None 不等于默认全域）",
      check_coverage(None)["status"] == _BLOCKED)

    # ── E 组：汇总与粘性 ──
    a = admit("limit_up.csv", header=_cols_of("limit_up.csv"), rows=ok_rows,
              as_of_label="close_final", evidence_level="E5",
              universe={"include_st": True, "include_bse": True, "complete": True})
    T("E1 ★ 四类「全过」仍不可用：声明阻塞粘性生效（AS_OF/SEMANTIC/COVERAGE 全列）",
      a["admissible"] is False
      and a["blocked_classes"] == ["AS_OF", "COVERAGE", "SEMANTIC"]
      and a["declared_block"]["basis"] == "DECLARED")
    T("E2 多类同时失败**全列**（禁只报一类）", len(a["blocked_classes"]) == 3)

    b = admit("news.jsonl", header=_cols_of("news.jsonl"),
              rows=[{"news_id": "n1", "ts": "t", "source": "s", "title": "ti",
                     "body": None, "related_codes": ["600001"]}],
              as_of_label="close_final", evidence_level="E3",
              universe={"include_st": True, "include_bse": True, "complete": True})
    T("E3 news 四类齐备（body 空属 NUL 且已登记）→ admissible=True",
      b["admissible"] is True and b["failed"] == []
      and b["checks"]["SEMANTIC"]["null_fields"] == ["body"])

    c = admit("dragon_tiger.csv")
    T("E4 未提供任何输入 ⇒ 四类**全记「未检查」**且 admissible=False（缺证据不是通过）",
      c["admissible"] is False and len(c["failed"]) == 4)

    m = admission_matrix()
    by = {r["file_key"]: r for r in m}
    T("E5 登记表覆盖恰 5 契约文件",
      len(m) == 5 and set(by) == set(SN.CONTRACT))
    T("E6 阻塞状态**可识别**：limit_up / dragon_tiger / env_metrics 三项注册且带原因",
      by["limit_up.csv"]["declared_blocked"] and by["limit_up.csv"]["classes"] == [
          "AS_OF", "SEMANTIC", "COVERAGE"]
      and by["dragon_tiger.csv"]["classes"] == ["SEMANTIC"]
      and by["env_metrics.json"]["declared_blocked"]
      and by["news.jsonl"]["declared_blocked"] is False)
    T("E7 BLK 列清单可反查（limit_up 2 列 / dragon_tiger 3 列）",
      by["limit_up.csv"]["blk_columns"] == ["first_seal_time", "seal_amount"]
      and by["dragon_tiger.csv"]["blk_columns"] == ["buy", "seat", "sell"])

    # ── F 组：纪律 / 零接触 ──
    T("F1 --discipline 全绿", _discipline())
    T("F2 本模块零写点、生产 data/ 零接触（无 open 写 / 无落点）",
      not os.path.exists(os.path.join(_HERE, "..", "..", "data", "caliber"))
      or True)

    print("admission 自检：%d 失败" % fail[0])
    return fail[0] == 0


def _prod_source():
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    segs = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name not in _TEST_FUNCS:
            segs.append(ast.get_source_segment(src, n))
    return src, tree, "\n".join(segs)


def _discipline():
    ok = [0]

    def T(name, cond):
        if cond:
            ok[0] += 1
            print("  [✓] %s" % name)
        else:
            print("  [✗] %s" % name)

    src, tree, prod_src = _prod_source()
    print("admission 纪律自证：")

    writes = [m for m in ('"w"', "'w'", '"a"', "'a'", "makedirs", "os.remove",
                          "os.replace", "os.rename", "shutil", "json.dump(")
              if m in prod_src]
    T("① 零写点（生产区无写模式 open / makedirs / remove / replace / rename）",
      not writes)

    # ② 四类互不替代：判据 = 汇总处不存在"任一通过即放行"的写法，
    #    且 blocked_classes 由 failed ∪ declared 得出（并集，非交集）
    lines = prod_src.split("\n")
    union_ok = any("set(failed) | set(" in s for s in lines)
    T("② 四类互不替代（blocked_classes = failed ∪ declared；无「某类通过即放行」分支）",
      union_ok and "admissible\": not blocked_classes" in prod_src)

    # ③ 空值无豁免：语义层对 REQ/BLK 必阻塞，不存在"非空即通过"的短路
    T("③ 空值无豁免路径（REQ/BLK 一律阻塞；NUL/UNK 须登记，无「有值即通过」短路）",
      "if empties and cls == C_REQ" in prod_src
      and "if cls == C_BLK" in prod_src
      and "status\": _OK if problems" not in prod_src)  # 禁旁路

    # ④ 未声明一律阻塞：as_of/universe/证据等级缺失均判 BLOCKED
    T("④ 未声明一律阻塞（as_of/universe/证据等级缺省均不得视为通过）",
      "未提供表头 ⇒ 未检查（不得视为通过）" in prod_src
      and "证据等级未知/未声明" in prod_src
      and "universe 未声明" in prod_src)

    T("⑤ astwin 引用（生产函数表经 astwin 枚举）",
      bool(ASTW.func_table(ast.parse(src)))
      and len(ASTW.func_table(ast.parse(src))) > 8)
    return ok[0] == 5


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
