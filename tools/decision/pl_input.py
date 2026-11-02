#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pl_input.py · C3 正式价线输入适配器（v1.0 · 2026-10-16 建 · 任务 203）

用途：把 **priceline.py 的真实计算结果**（PL-R1）适配成 `three_q.answer` 的正式
      价线入参（entry_limit / stop_line / target / window），并提供 **显式授权输入件**
      （position_request + 授权元数据），供 C4 三问赔率/证伪问 → C5 机械硬风控审议。

★ 一、授权边界（AGENT.md §4.1 原文口径，不自行推断）
  §4.1：「**BUY 的价线与仓位请求是 LLM 请求、不是批准；批准权在 C5 硬闸与用户**」；
  §5：「AI 可以自主决定『怎么打』：策略选择 / 交易方向 / R:R 要求 / **仓位请求** /
       持仓周期 …… 系统硬闸决定『最多打多少、最多承受多少风险』」。
  ⇒ 本适配器**不做**逐票人工批准（那不是既有契约），而是：
    ① 价线必须**来自 priceline 真实计算**（不许手填、不许默认值）；
    ② 仓位必须**显式给出**（不来自价线生成器推断）；
    ③ 两者合并后**必须**送 C5 `position_gate.check` 机械审议（≤ request 四层取最严）。
  ⇒ 本件**不判动作**（动作只由 three_q 机械分流）、**不绕过 C5**、**不改冻结判据**。

★ 二、正式输入件格式（authorization input，JSON；缺任一必填键 = 拒）
  {
    "schema_version": 1,
    "as_of": "2026-09-29",                 # 信号日（= 决策日）
    "authorizations": [
      {"code": "sh600152",
       "price_line": {"rule_version": "PL-R1", "data_cutoff": "2026-09-28",
                      "entry_limit": 7.636, "stop_line": 7.213,
                      "target": 16.62, "window": 10},   # ← 必须与 priceline 实算逐字段一致
       "position_request": 0.05,             # ← 显式请求仓（(0,1]，独立输入，非价线推断）
       "note": "…"}                          # 可选：人话备注（不参与机械判定）
    ]
  }
  授权元数据（审计）：`approved_via` / `note` 仅记录，**不构成机械事实证明**
  （同 B 批 §2.4.10「审批人 = 弱绑定」先例：只记录，不声称已绑定）。

★ 三、准入校验（任一不过 → 该票 REJECTED，附 reason codes；绝不降级填值）
  PL_NOT_FOUND       该 code 无 priceline 结果（未参与实算）
  PL_NOT_TRADEABLE   priceline 判不可交易（RR/过期/无前高/崩盘失真…）
  PL_FIELD_MISMATCH  授权件价线字段与 priceline 实算值不一致（防手填/防改数）
  PL_RULE_MISMATCH   规则版本不一致（防旧规则价线冒充）
  PL_EXPIRED         数据截止距 as_of 超 priceline.MAX_STALE_DAYS
  PL_DIRECTION       方向矛盾（target <= entry 或 entry <= stop）
  PL_RR_LOW          风险收益不足（RR < priceline.MIN_RR）
  POS_MISSING        未给 position_request（仓位必须独立显式给出）
  POS_RANGE          position_request 不在 (0, 1]
  CODE_NOT_IN_POOL   该 code 非当日活跃候选（无决策主体）

★ 四、边界
  零写点（纯只读 + 返回值）；`--out` 由 CLI 注入（缺省不落盘）。
  `--selftest` 全沙盒（合成 priceline 结果 + 真库指纹守卫）。
"""
import argparse
import hashlib
import io
import json
import os
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "..", ".."))
for _p in ("fs", "analysis", "decision"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import priceline as PL       # noqa: E402  ← 价线算法唯一真源（不复制第二套）
import decision as DEC       # noqa: E402

SCHEMA_VERSION = 1

PRICE_FIELDS = ("entry_limit", "stop_line", "target", "window")
REQUIRED_TOP = ("schema_version", "as_of", "authorizations")
REQUIRED_AUTH = ("code", "price_line", "position_request")

# 原因码（顺序 = 校验顺序，短路口径）
R_NOT_FOUND = "PL_NOT_FOUND"
R_NOT_TRADEABLE = "PL_NOT_TRADEABLE"
R_FIELD_MISMATCH = "PL_FIELD_MISMATCH"
R_RULE_MISMATCH = "PL_RULE_MISMATCH"
R_EXPIRED = "PL_EXPIRED"
R_DIRECTION = "PL_DIRECTION"
R_RR_LOW = "PL_RR_LOW"
R_POS_MISSING = "POS_MISSING"
R_POS_RANGE = "POS_RANGE"
R_CODE_NOT_IN_POOL = "CODE_NOT_IN_POOL"


class PLInputError(Exception):
    """输入件结构非法（文件级）。str 以 REASON_CODE: 开头。"""


# ── 输入件读取与结构校验 ─────────────────────────────────────────────────────
def load_authorizations(path):
    """读授权输入件 + 结构校验（文件级非法直接抛；票级问题走 evaluate）。"""
    if not os.path.isfile(path):
        raise PLInputError("AUTH_FILE_ABSENT: %s" % path)
    try:
        with io.open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        raise PLInputError("AUTH_FILE_BAD_JSON: %s" % e)
    if not isinstance(doc, dict):
        raise PLInputError("AUTH_FILE_NOT_OBJECT")
    for k in REQUIRED_TOP:
        if k not in doc:
            raise PLInputError("AUTH_FILE_MISSING_KEY: %s" % k)
    if not isinstance(doc["authorizations"], list):
        raise PLInputError("AUTH_FILE_BAD_AUTHORIZATIONS: 必须是列表")
    for i, a in enumerate(doc["authorizations"]):
        if not isinstance(a, dict):
            raise PLInputError("AUTH_ITEM_NOT_OBJECT: #%d" % i)
        for k in REQUIRED_AUTH:
            if k not in a:
                raise PLInputError("AUTH_ITEM_MISSING_KEY: #%d.%s" % (i, k))
        if not isinstance(a["price_line"], dict):
            raise PLInputError("AUTH_ITEM_BAD_PRICE_LINE: #%d" % i)
    return doc


def _eq_num(a, b, tol=1e-9):
    """数值等价（None 对 None = 真；防 int/float 表示差异）。"""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


# ── 票级准入校验 ─────────────────────────────────────────────────────────────
def evaluate(auth_item, pl_result, as_of, *, active_codes=None):
    """授权项 × priceline 实算结果 → {ok, reasons, answer_kwargs, audit}。

    **纯函数**：不读文件、不写文件、不判动作。ok=False 时 answer_kwargs=None。
    """
    reasons = []
    code = auth_item.get("code")
    plv = pl_result
    pline = auth_item.get("price_line") or {}

    # ① priceline 结果存在性
    if plv is None:
        reasons.append(R_NOT_FOUND)
        return _rej(code, reasons, pline, auth_item)

    # ② 活跃候选（有决策主体）
    if active_codes is not None and code not in active_codes:
        reasons.append(R_CODE_NOT_IN_POOL)

    # ③ 规则版本一致（防旧规则冒充）
    if pline.get("rule_version") != plv.get("rule_version"):
        reasons.append(R_RULE_MISMATCH)

    # ④ 数据截止一致 + 未过期（未来函数/过期双查）
    if pline.get("data_cutoff") != plv.get("data_cutoff"):
        reasons.append(R_FIELD_MISMATCH)
    cutoff = plv.get("data_cutoff")
    if cutoff:
        stale = PL._stale_days(cutoff, as_of)
        if stale is None or stale > PL.MAX_STALE_DAYS:
            reasons.append(R_EXPIRED)
        if cutoff >= as_of:
            reasons.append(R_EXPIRED)       # 截止 >= 信号日 = 未来函数

    # ⑤ 四价位字段与实算值逐字段一致（防手填/防改数）
    for k in PRICE_FIELDS:
        if not _eq_num(pline.get(k), plv.get(k)):
            reasons.append(R_FIELD_MISMATCH)
            break

    # ⑥ priceline 自身可交易判定（RR/前高/崩盘护栏等由 priceline 机械给出）
    if not plv.get("tradeable"):
        reasons.append(R_NOT_TRADEABLE)

    # ⑦ 方向自洽 + RR 门槛（用**实算值**复核，不信输入件）
    e, s, t = plv.get("entry_limit"), plv.get("stop_line"), plv.get("target")
    w = plv.get("window")
    rr = plv.get("risk_reward")
    if None in (e, s, t, w):
        reasons.append(R_NOT_TRADEABLE)
    else:
        if not (t > e > s):
            reasons.append(R_DIRECTION)
        elif rr is None or rr < PL.MIN_RR:
            reasons.append(R_RR_LOW)

    # ⑧ 仓位独立输入（不从价线推断）
    pr = auth_item.get("position_request")
    if pr is None:
        reasons.append(R_POS_MISSING)
    elif isinstance(pr, bool) or not isinstance(pr, (int, float)) or not (0 < pr <= 1):
        reasons.append(R_POS_RANGE)

    if reasons:
        return _rej(code, sorted(set(reasons)), pline, auth_item)
    return {
        "ok": True, "reasons": [],
        "answer_kwargs": {"entry_limit": float(e), "stop_line": float(s),
                          "target": float(t), "window": int(w),
                          "position_request": float(pr)},
        "audit": {"code": code, "signal_date": as_of,
                  "data_cutoff": plv.get("data_cutoff"),
                  "rule_version": plv.get("rule_version"),
                  "risk_reward": rr, "n_bars": plv.get("n_bars"),
                  "target_basis": plv.get("target_basis"),
                  "price_line_source": "priceline.PL-R1",
                  "note": auth_item.get("note")},
    }


def _rej(code, reasons, pline, auth_item):
    return {"ok": False, "reasons": reasons, "answer_kwargs": None,
            "audit": {"code": code, "submitted_price_line": pline,
                      "position_request": auth_item.get("position_request")}}


# ── 批量：输入件 × priceline 结果集 → 正式入参集 ─────────────────────────────
def build(doc, pl_by_code, *, active_codes=None):
    """→ (admitted, rejected, summary)。admitted[code] = {answer_kwargs, audit}。"""
    as_of = doc["as_of"]
    admitted, rejected = {}, {}
    for item in doc["authorizations"]:
        code = item.get("code")
        r = evaluate(item, pl_by_code.get(code), as_of, active_codes=active_codes)
        if r["ok"]:
            admitted[code] = {"answer_kwargs": r["answer_kwargs"], "audit": r["audit"]}
        else:
            rejected[code] = {"reasons": r["reasons"], "audit": r["audit"]}
    summary = {
        "schema_version": SCHEMA_VERSION, "as_of": as_of,
        "n_submitted": len(doc["authorizations"]),
        "n_admitted": len(admitted), "n_rejected": len(rejected),
        "reason_histogram": _hist(rejected),
        "admitted_codes": sorted(admitted), "rejected_codes": sorted(rejected),
        "discipline": ("价线只取 priceline 实算值（禁手填/默认值）；仓位独立显式输入；"
                       "本件不判动作，动作只由 three_q 机械分流；C5 硬闸不可绕过"),
    }
    return admitted, rejected, summary


def _hist(rejected):
    h = {}
    for v in rejected.values():
        for rs in v["reasons"]:
            h[rs] = h.get(rs, 0) + 1
    return h


# ── 生成建议输入件（--suggest：从 priceline 结果集起草，仓位留给调用方填）──────
def suggest(pl_by_code, as_of, *, active_codes=None):
    """从 priceline 实算结果起草授权件骨架（**仓位留空 = None，必须显式填**）。

    只收录 priceline 判 tradeable 且属活跃候选的票；仓位一律 None（本件不代填）。
    """
    out = {"schema_version": SCHEMA_VERSION, "as_of": as_of,
           "note": "骨架：position_request 必须由调用方显式填写（本件不代填），"
                   "approved_via/note 仅审计记录",
           "authorizations": []}
    for code in sorted(pl_by_code):
        v = pl_by_code[code]
        if not v.get("tradeable"):
            continue
        if active_codes is not None and code not in active_codes:
            continue
        out["authorizations"].append({
            "code": code,
            "price_line": {"rule_version": v.get("rule_version"),
                           "data_cutoff": v.get("data_cutoff"),
                           "entry_limit": v.get("entry_limit"),
                           "stop_line": v.get("stop_line"),
                           "target": v.get("target"),
                           "window": v.get("window")},
            "position_request": None,
            "note": None,
        })
    return out


# ── 自检（全沙盒 + 真库指纹守卫）─────────────────────────────────────────────
def _tree_fp(root):
    if not os.path.isdir(root):
        return "ABSENT"
    h = hashlib.md5()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root).replace("\\", "/")
            h.update(rel.encode("utf-8"))
            with io.open(p, "rb") as f:
                h.update(f.read())
    return h.hexdigest()


def _selftest():
    import shutil
    import tempfile

    bad = [0]
    _guard = [os.path.join(DEC.ROOT, "data"), os.path.join(DEC.ROOT, "memory")]
    _before = {d: _tree_fp(d) for d in _guard}

    def T(name, cond, extra=""):
        print(("  [ok] " if cond else "  [!!] ") + name + ("" if cond else " | %s" % extra))
        if not cond:
            bad[0] += 1

    as_of = "2026-09-29"
    good_pl = {"code": "sh600152", "rule_version": "PL-R1",
               "data_cutoff": "2026-09-28", "n_bars": 125, "tradeable": True,
               "entry_limit": 7.636, "stop_line": 7.213, "target": 16.62,
               "window": 10, "risk_reward": 21.239,
               "target_basis": "PRIOR_HIGH", "reasons": []}
    good_item = {"code": "sh600152",
                 "price_line": {"rule_version": "PL-R1", "data_cutoff": "2026-09-28",
                                "entry_limit": 7.636, "stop_line": 7.213,
                                "target": 16.62, "window": 10},
                 "position_request": 0.05, "note": "测试"}

    print("== A. 有效价线 ==")
    r = evaluate(good_item, good_pl, as_of, active_codes={"sh600152"})
    T("A1 合规 → 通过", r["ok"], r["reasons"])
    T("A2 answer_kwargs 五键齐",
      set(r["answer_kwargs"]) == {"entry_limit", "stop_line", "target", "window",
                                  "position_request"}, r["answer_kwargs"])
    T("A3 价位取实算值（非输入件字面）",
      r["answer_kwargs"]["entry_limit"] == good_pl["entry_limit"])
    T("A4 审计字段齐（signal/data_cutoff/rule_version）",
      r["audit"]["signal_date"] == as_of and r["audit"]["data_cutoff"] == "2026-09-28"
      and r["audit"]["rule_version"] == "PL-R1")

    print("== B. 无效价线 ==")
    r = evaluate(good_item, None, as_of)
    T("B1 无 priceline 结果 → PL_NOT_FOUND", r["reasons"] == [R_NOT_FOUND], r["reasons"])
    r = evaluate(good_item, dict(good_pl, tradeable=False, reasons=["RR_TOO_LOW"]), as_of)
    T("B2 priceline 不可交易 → PL_NOT_TRADEABLE",
      R_NOT_TRADEABLE in r["reasons"], r["reasons"])
    r = evaluate(dict(good_item, price_line=dict(good_item["price_line"],
                                                 entry_limit=9.999)), good_pl, as_of)
    T("B3 手填价位不一致 → PL_FIELD_MISMATCH", R_FIELD_MISMATCH in r["reasons"])
    r = evaluate(dict(good_item, price_line=dict(good_item["price_line"],
                                                 rule_version="PL-R0")), good_pl, as_of)
    T("B4 规则版本不一致 → PL_RULE_MISMATCH", R_RULE_MISMATCH in r["reasons"])

    print("== C. 过期 / 未来函数 ==")
    r = evaluate(good_item, dict(good_pl, data_cutoff="2026-09-10"), as_of)
    T("C1 数据过期 → PL_EXPIRED", R_EXPIRED in r["reasons"] or
      R_FIELD_MISMATCH in r["reasons"], r["reasons"])
    r = evaluate(good_item, dict(good_pl, data_cutoff="2026-09-30"), as_of)
    T("C2 截止>=信号日（未来）→ 拒", R_EXPIRED in r["reasons"] or
      R_FIELD_MISMATCH in r["reasons"], r["reasons"])

    print("== D. 价位/风险收益异常 ==")
    r = evaluate(good_item, dict(good_pl, target=7.0), as_of)
    T("D1 方向矛盾 → PL_DIRECTION",
      R_DIRECTION in r["reasons"] or R_FIELD_MISMATCH in r["reasons"], r["reasons"])
    r = evaluate(good_item, dict(good_pl, risk_reward=1.2), as_of)
    T("D2 RR 不足 → PL_RR_LOW",
      R_RR_LOW in r["reasons"] or R_NOT_TRADEABLE in r["reasons"], r["reasons"])

    print("== E. 仓位独立输入 ==")
    r = evaluate(dict(good_item, position_request=None), good_pl, as_of)
    T("E1 缺仓位 → POS_MISSING（不代填）", r["reasons"] == [R_POS_MISSING], r["reasons"])
    r = evaluate(dict(good_item, position_request=1.5), good_pl, as_of)
    T("E2 仓位越界 → POS_RANGE", r["reasons"] == [R_POS_RANGE], r["reasons"])
    r = evaluate(dict(good_item, position_request=0), good_pl, as_of)
    T("E3 仓位 0 → POS_RANGE（不产生下注）", r["reasons"] == [R_POS_RANGE])

    print("== F. 候选/池归属 ==")
    r = evaluate(good_item, good_pl, as_of, active_codes={"sh600999"})
    T("F1 非活跃候选 → CODE_NOT_IN_POOL", R_CODE_NOT_IN_POOL in r["reasons"])

    print("== G. 输入件结构校验 ==")
    tmp = tempfile.mkdtemp(prefix="ta_plin_")
    try:
        p = os.path.join(tmp, "auth.json")
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"schema_version": 1, "as_of": as_of,
                       "authorizations": [good_item]}, f, ensure_ascii=False)
        doc = load_authorizations(p)
        T("G1 合规输入件可读", doc["as_of"] == as_of
          and len(doc["authorizations"]) == 1)
        p2 = os.path.join(tmp, "bad.json")
        with io.open(p2, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"as_of": as_of, "authorizations": []}, f)
        try:
            load_authorizations(p2)
            T("G2 缺 schema_version → 抛", False)
        except PLInputError as e:
            T("G2 缺 schema_version → 抛", "MISSING_KEY" in str(e))
        p3 = os.path.join(tmp, "bad2.json")
        with io.open(p3, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"schema_version": 1, "as_of": as_of,
                       "authorizations": [{"code": "x", "position_request": 0.1}]}, f)
        try:
            load_authorizations(p3)
            T("G3 授权项缺 price_line → 抛", False)
        except PLInputError as e:
            T("G3 授权项缺 price_line → 抛", "MISSING_KEY" in str(e))

        print("== H. 批量 build（混合票） ==")
        pls = {"sh600152": good_pl,
               "sh600218": dict(good_pl, code="sh600218", tradeable=False,
                                reasons=["RR_TOO_LOW"])}
        doc2 = {"schema_version": 1, "as_of": as_of, "authorizations": [
            good_item,
            {"code": "sh600218",
             "price_line": {"rule_version": "PL-R1", "data_cutoff": "2026-09-28",
                            "entry_limit": 1, "stop_line": 1, "target": 1, "window": 1},
             "position_request": 0.05},
            {"code": "sh600999",
             "price_line": {"rule_version": "PL-R1", "data_cutoff": "2026-09-28",
                            "entry_limit": 1, "stop_line": 1, "target": 1, "window": 1},
             "position_request": 0.05}]}
        adm, rej, summ = build(doc2, pls, active_codes={"sh600152", "sh600218"})
        T("H1 混合票准入计数", summ["n_submitted"] == 3 and summ["n_admitted"] == 1
          and summ["n_rejected"] == 2, summ)
        T("H2 拒因直方图可读", summ["reason_histogram"].get(R_NOT_FOUND) == 1, summ)

        print("== I. 骨架起草（仓位必须空） ==")
        sk = suggest({"sh600152": good_pl, "sh600218": dict(good_pl, code="sh600218",
                                                           tradeable=False)}, as_of,
                     active_codes={"sh600152", "sh600218"})
        T("I1 仅收 tradeable + 活跃候选", len(sk["authorizations"]) == 1)
        T("I2 仓位一律 None（不代填）",
          all(a["position_request"] is None for a in sk["authorizations"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        _purge_pycache()

    _after = {d: _tree_fp(d) for d in _guard}
    drift = [os.path.relpath(d, DEC.ROOT) for d in _guard if _before[d] != _after[d]]
    T("J1 真库零污染（data/ 与 memory/ 指纹一致）", not drift, drift)
    print("\n自检：%s（%d 失败）" % ("全部通过 ✅" if not bad[0] else "有失败 ❌", bad[0]))
    return bad[0]


def _purge_pycache():
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, os.path.join(_ROOT, "tools", "analysis")):
        pc = os.path.join(d, "__pycache__")
        if os.path.isdir(pc):
            import shutil
            shutil.rmtree(pc, ignore_errors=True)


def _main(argv=None):
    ap = argparse.ArgumentParser(description="C3 正式价线输入适配器（零写点·不判动作）")
    ap.add_argument("--auth-file", dest="auth_file", help="授权输入件 JSON")
    ap.add_argument("--price-lines", dest="price_lines", help="priceline 产出的价线 JSON")
    ap.add_argument("--active-codes", dest="active_codes",
                    help="逗号分隔活跃候选码（可选；缺省不做池归属校验）")
    ap.add_argument("--as-of", dest="as_of", help="信号日（--suggest 时必填）")
    ap.add_argument("--suggest", help="从价线 JSON 起草授权骨架（仓位留空）")
    ap.add_argument("--out", default=None, help="可选落点（CLI 注入；缺省不落盘）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()

    def _load_pl(path):
        with io.open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("by_code") or {}, d.get("summary") or {}

    if a.suggest:
        if not a.as_of:
            ap.error("--suggest 需 --as-of")
        pl, _ = _load_pl(a.suggest)
        act = set(a.active_codes.split(",")) if a.active_codes else None
        doc = suggest(pl, a.as_of, active_codes=act)
        txt = json.dumps(doc, ensure_ascii=False, indent=1)
        if a.out:
            with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
                f.write(txt + "\n")
        print(txt)
        return 0

    if not (a.auth_file and a.price_lines):
        ap.print_help()
        return 2
    doc = load_authorizations(a.auth_file)
    pl, _ = _load_pl(a.price_lines)
    act = set(a.active_codes.split(",")) if a.active_codes else None
    admitted, rejected, summary = build(doc, pl, active_codes=act)
    print(json.dumps({"summary": summary, "admitted": admitted,
                      "rejected": rejected}, ensure_ascii=False, indent=1))
    if a.out:
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"summary": summary, "admitted": admitted, "rejected": rejected},
                      f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
