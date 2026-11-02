"""external.py — 1.7 外部知识通道（C6-b）：六步链的机械侧执行件。

六步链（施工方案 1.7）：外部来源 → 采集去重去噪 → 可靠性分级 S(权威)/A(专业)/
B(经验)/C(传闻) → **只进 Hypothesis 候选**（external_queue.jsonl）→ 走 1.6 提炼链
回测验证（verify）→ 过则 Rule 候选（calibrate.rule_candidate，**本件零直写**）→
用户批准 → VALIDATED。

铁律（本件机械执行）：
  · **分级只决定「采不采、采多深」，不决定成不成立**——成 Rule 只由回测验证决定；
    **S 级来源无回测证据同样不得推进状态**（mark_verified 硬闸，实测拒绝）。
  · 外部知识唯一合法落点 = Hypothesis 候选；零后门写正式值区。
  · 学习需求触发器（主动器官）：1.6 需求缺口 → EXTERNAL_DEMAND 行
    （缺什么 / 去哪找 / 怎么验证）写**同一条** demand_queue.jsonl
    （v3.18 收口：不另立 demand.json），scanner 自动生成、非手工清单。

状态机（本件只管前半段，后半段归 1.6 提炼链）：
  HYP_CANDIDATE →（mark_registered 绑定 1.6 hyp_id）→ HYP_REGISTERED
  →（mark_verified 核验回测证据）→ VERIFIED_SUPPORT / MIXED / REFUTE / INSUFFICIENT。
  推进 Rule 候选不在本件：必须走 calibrate（SUPPORT 前置 + 用户批准），
  本件零正式值区写入（纪律判据写死）。

边界（纪律判据写死）：唯一写点 = external_queue.jsonl（append）与
demand_queue.jsonl（append），sink 均内联 `path or CONST` 静态可解析；
零正式值区 / 决策卡 / 仓位闸 / 校准队列写入；零转正调用；不接结算主链
（无结算模块导入）；actor=ext（0.17 FORMAL_ACTORS 在册）；时间截断：ts > as_of
的素材拒收（不采集「未来」的知识）。
"""

import os
import sys
import io
import ast
import json
import hashlib
import re
import tempfile

_FS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools", "fs")
if _FS not in sys.path:
    sys.path.insert(0, _FS)

from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
import params as PARAMS  # noqa: E402
import observe as OBS  # noqa: E402
import astwin as ASTW  # noqa: E402
import verify as VF  # noqa: E402
import demand_scanner as DS  # noqa: E402

ACTOR = "ext"
STAGE = "C6"
SCHEMA_VERSION = "1.0"
EXT_PATH = os.path.join(ROOT, "data", "learning", "external_queue.jsonl")
# 唯一队列真源：与 demand_scanner.DEMAND_PATH 同字面量（landing 静态解析要求本模块
# 常量字面直达，跨模块属性不可解析；自检 A3 断言两值相等防漂移）
DEMAND_PATH = os.path.join(ROOT, "data", "learning", "demand_queue.jsonl")

ST_HYP_CANDIDATE = "HYP_CANDIDATE"
ST_HYP_REGISTERED = "HYP_REGISTERED"
ST_VERIFIED = "VERIFIED_"            # 前缀：VERIFIED_SUPPORT / MIXED / REFUTE / INSUFFICIENT

# 可靠性分级只定「采不采、采多深」：source_id → 等级（新来源 = 注册表级变更，非随手加）
SOURCE_GRADES = {
    "official": {"grade": "S", "note": "监管 / 交易所 / 官方公告"},
    "research": {"grade": "A", "note": "卖方研报 / 专业机构方法"},
    "data_portal": {"grade": "A", "note": "数据发现类（结构化事实）"},
    "book": {"grade": "B", "note": "经典著作 / 系统性方法论"},
    "forum": {"grade": "C", "note": "投资社区 / 传闻（采且降权）"},
}
GRADES = ("S", "A", "B", "C")

# 缺口类型 → 建议采集来源（去哪找；机械映射非 LLM 判断）
GAP_SOURCES = {
    "REPEAT_ERROR": ("research", "data_portal"),
    "WIN_BUT_WRONG": ("book", "research"),
    "JUDGEMENT_QUALITY_LOW": ("book", "data_portal"),
}
DEFAULT_GAP_SOURCES = ("research",)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ExternalError(Exception):
    """外部通道被拒（来源未登记 / 字段缺失 / 状态机非法 / 无回测证据）。"""


def _now():
    import datetime as dt
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _rows_of(path):
    if not os.path.isfile(path):
        return []
    out = []
    with io.open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


def _append_ext(row, path=None):
    """append-only 唯一写点（sink 内联 BoolOp，静态可解析）。"""
    _WG.check_write(path or EXT_PATH)
    with io.open(path or EXT_PATH, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _append_demand_row(row, path=None):
    """需求行追加（同一队列真源；sink 内联 BoolOp）。"""
    _WG.check_write(path or DEMAND_PATH)
    with io.open(path or DEMAND_PATH, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _rewrite(rows, path=None):
    """状态推进 = 整文件原子重写（os.replace 终点内联 BoolOp，regime 先例）。"""
    os.makedirs(os.path.dirname(path or EXT_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path or EXT_PATH), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path or EXT_PATH)
    _WG.check_write(path or EXT_PATH)


# ── 步骤①②③：采集 → 去重去噪 → 可靠性分级 ──────────────────────────────────
def ingest(items, *, as_of, path=None):
    """外部素材入队 → status=HYP_CANDIDATE（只进假设候选，永不直接生效）。

    items: [{source_id, title, digest, ts, url?}]——source_id 必须在 SOURCE_GRADES
    （未登记来源拒收，等级不可现场编造）；ts=素材时点（> as_of 拒收：时间截断）。
    去重键 = md5(规范化 {source_id,title,digest,ts})，同指纹重放不追加。
    返回 {"ingested", "reused", "rejected": [{index, reason}], "items": [新行]}。
    """
    if not _DATE_RE.match(str(as_of or "")):
        raise ExternalError("as_of 必须是 YYYY-MM-DD，实际 %r" % (as_of,))
    if not isinstance(items, list):
        raise ExternalError("items 必须是 list")
    existing = {r.get("item_id") for r in _rows_of(path or EXT_PATH)}
    out, rejected = [], []
    n_new = n_dup = 0
    for i, it in enumerate(items):
        try:
            if not isinstance(it, dict):
                raise ExternalError("素材必须是 dict")
            sid = str(it.get("source_id") or "").strip()
            if sid not in SOURCE_GRADES:
                raise ExternalError("UNKNOWN_SOURCE: %r（来源未登记，等级不可现场编造）" % sid)
            title = str(it.get("title") or "").strip()
            digest = str(it.get("digest") or "").strip()
            if not title or not digest:
                raise ExternalError("去噪拒绝：title/digest 不得为空")
            ts = str(it.get("ts") or "").strip()
            if not _DATE_RE.match(ts):
                raise ExternalError("ts 必须是 YYYY-MM-DD，实际 %r" % ts)
            if ts > as_of:
                raise ExternalError("时间截断：素材 ts %s > as_of %s（不采集未来的知识）"
                                    % (ts, as_of))
            fp = hashlib.md5(_canon({"source_id": sid, "title": title,
                                     "digest": digest, "ts": ts})
                             .encode("utf-8")).hexdigest()
            item_id = "EXT-" + fp[:12]
            if item_id in existing:
                n_dup += 1
                continue
            row = {"item_id": item_id, "as_of": as_of, "schema_version": SCHEMA_VERSION,
                   "source_id": sid, "grade": SOURCE_GRADES[sid]["grade"],
                   "title": title, "digest": digest, "ts": ts,
                   "url": str(it.get("url") or "").strip(),
                   "status": ST_HYP_CANDIDATE, "hyp_id": None,
                   "data_fingerprint": "md5:" + fp,
                   "params_version": PARAMS.load_params().get("version"),
                   "created_at": _now()}
            _append_ext(row, path)
            existing.add(item_id)
            out.append(row)
            n_new += 1
        except ExternalError as ex:
            rejected.append({"index": i, "reason": str(ex)})
    return {"ingested": n_new, "reused": n_dup, "rejected": rejected, "items": out}


def _get_row(item_id, path):
    rows = _rows_of(path or EXT_PATH)
    for i, r in enumerate(rows):
        if r.get("item_id") == item_id:
            return rows, i, r
    raise ExternalError("item 不存在: %r" % (item_id,))


# ── 步骤④：只进 Hypothesis 候选（绑定 1.6 假设 id） ─────────────────────────
def mark_registered(item_id, *, hyp_id, path=None):
    """HYP_CANDIDATE → HYP_REGISTERED（绑定 1.6 提炼链假设 id；空 id 拒收）。"""
    hid = str(hyp_id or "").strip()
    if not hid:
        raise ExternalError("hyp_id 必填（外部知识只进 Hypothesis 候选，须可被 1.6 链消费）")
    rows, i, r = _get_row(item_id, path)
    if r.get("status") != ST_HYP_CANDIDATE:
        raise ExternalError("状态机非法: %s（仅 %s 可登记）" % (r.get("status"), ST_HYP_CANDIDATE))
    r["status"] = ST_HYP_REGISTERED
    r["hyp_id"] = hid
    r["registered_at"] = _now()
    _rewrite(rows, path)
    return {"item_id": item_id, "status": r["status"], "hyp_id": hid}


# ── 步骤⑤前闸：无回测证据不得推进（S 级也不例外） ────────────────────────────
def mark_verified(item_id, *, results_path=None, path=None):
    """HYP_REGISTERED → VERIFIED_<verdict>；证据 = 1.6 verify 结果（按 hyp_id 查）。

    **铁律闸**：verify 结果为空 → 拒绝（NO_VERIFICATION_EVIDENCE）——
    分级再高（S 级）也替代不了回测，成不成立只由回测定。
    """
    rows, i, r = _get_row(item_id, path)
    if r.get("status") != ST_HYP_REGISTERED or not r.get("hyp_id"):
        raise ExternalError("状态机非法: %s（须先 mark_registered 绑定假设 id）"
                            % r.get("status"))
    ev = VF.load_results(r["hyp_id"], results_path=results_path)
    if not ev:
        raise ExternalError("NO_VERIFICATION_EVIDENCE: %s 无回测验证记录"
                            "（S 级来源也不例外——成不成只由回测定）" % r["hyp_id"])
    verdict = str(ev[-1].get("verdict") or "")
    if verdict not in ("SUPPORT", "MIXED", "REFUTE", "INSUFFICIENT"):
        raise ExternalError("verdict 值域非法: %r" % verdict)
    r["status"] = ST_VERIFIED + verdict
    r["verification_ref"] = {"hyp_id": r["hyp_id"], "verdict": verdict,
                             "n_results": len(ev)}
    r["verified_at"] = _now()
    _rewrite(rows, path)
    return {"item_id": item_id, "status": r["status"],
            "verification_ref": r["verification_ref"],
            "next": "VERIFIED_SUPPORT → 经 calibrate 提 Rule 候选（本件零直写，批准属用户）"}


# ── 学习需求触发器（主动器官：缺口 → 外部采集需求） ───────────────────────────
def demand_trigger(gaps, *, as_of, demand_path=None):
    """1.6 需求缺口 → EXTERNAL_DEMAND 行（缺什么 / 去哪找 / 怎么验证）。

    gaps: [{kind, where, ...}]（结算步5 / demand_scanner 产出的缺口条目）。
    幂等键 = md5(规范化 {as_of, gap})，同日同缺口不重复生成（scanner 自动、非手工）。
    """
    if not _DATE_RE.match(str(as_of or "")):
        raise ExternalError("as_of 必须是 YYYY-MM-DD，实际 %r" % (as_of,))
    have = {r.get("data_fingerprint") for r in _rows_of(demand_path or DEMAND_PATH)
            if r.get("kind") == "EXTERNAL_DEMAND"}
    out, n_new = [], 0
    for g in (gaps or []):
        if not isinstance(g, dict) or not g.get("kind"):
            continue
        fp = hashlib.md5(_canon({"as_of": as_of, "gap": g})
                         .encode("utf-8")).hexdigest()
        key = "md5:" + fp
        if key in have:
            continue
        row = {"as_of": as_of, "schema_version": SCHEMA_VERSION,
               "kind": "EXTERNAL_DEMAND", "gap": g,
               "need": {"what": "%s @ %s" % (g.get("kind"), g.get("where")),
                        "where_to": list(GAP_SOURCES.get(g.get("kind"),
                                                         DEFAULT_GAP_SOURCES)),
                        "how_to_verify": "1.6 提炼链回测（IS/OOS；证据不足不得包装成通过）"},
               "status": "OPEN", "scanner": "EXTERNAL_TRIGGER",
               "data_fingerprint": key,
               "params_version": PARAMS.load_params().get("version"),
               "created_at": as_of}
        _append_demand_row(row, demand_path)
        have.add(key)
        out.append(row)
        n_new += 1
    return {"triggered": n_new, "items": out}


# ── CLI 留痕（actor=ext） ─────────────────────────────────────────────────────
def _cli_demand():
    """真库需求触发：读 demand_queue GAP 行 → 生成 EXTERNAL_DEMAND（无缺口则如实空转）。"""
    gaps = []
    for r in _rows_of(DEMAND_PATH):
        if r.get("kind") == "GAP":
            gaps.extend(r.get("gaps") or [])
    def _run():
        return demand_trigger(gaps, as_of=_today())
    rep = OBS.run_observed(ACTOR, "external_demand", STAGE, _run,
                           as_of=_today(), detail="缺口 %d 条 → 外部采集需求" % len(gaps))
    return rep["value"]


def _today():
    import datetime as dt
    return dt.date.today().isoformat()


# ── 自检（全临时沙盒，真库零接触） ────────────────────────────────────────────
_TEST_FUNCS = ("_selftest", "_discipline")


def _prod_src():
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    lines = src.splitlines(True)
    drop = set()
    for lo, hi, name in ASTW.func_table(ast.parse(src)):
        if name in _TEST_FUNCS or name.startswith("_main"):
            drop.update(range(lo - 1, hi))
    return "".join(l for i, l in enumerate(lines) if i not in drop)


def _discipline(verbose=True):
    prod = _prod_src()
    res = [
        ("① 写点唯一且静态可解析（两 sink 均内联 path or CONST）",
         prod.count("io.open(path or EXT_PATH") == 1
         and prod.count("io.open(path or DEMAND_PATH") == 1
         and prod.count("os.replace(tmp, path or EXT_PATH)") == 1),
        ("② 反例判据：两步赋值坏形态（io.open(p, \"a\"）不得重现",
         'io.open(p, "a"' not in prod),
        ("③ 零正式值区 / 决策卡 / 仓位闸 / 校准队列写入",
         all(tok not in prod for tok in
             ("params.json", "decision_card", "position_gate", "calibration_queue",
              "rules.json"))),
        ("④ 零转正调用（候选≠生效，批准属用户）",
         "promote(" not in prod and "APPROVE" not in prod and "settle" not in prod),
        ("⑤ astwin 生产函数表非空", bool(ASTW.func_table(ast.parse(
            io.open(os.path.abspath(__file__), encoding="utf-8").read())))),
    ]
    ok = True
    for name, cond in res:
        ok = ok and cond
        if verbose:
            print(("  [ok] " if cond else "  [!!] ") + name)
    print("纪律判据：%d/%d" % (sum(1 for _, c in res if c), len(res)))
    return ok


def _selftest():
    bad = [0]
    real_fp = (hashlib.md5(io.open(EXT_PATH, "rb").read()).hexdigest()
               if os.path.isfile(EXT_PATH) else "ABSENT")
    real_dq = (hashlib.md5(io.open(DEMAND_PATH, "rb").read()).hexdigest()
               if os.path.isfile(DEMAND_PATH) else "ABSENT")

    def T(name, cond):
        print(("  [ok] " if cond else "  [!!] ") + name)
        if not cond:
            bad[0] += 1

    tmp = tempfile.mkdtemp(prefix="ext_sb_")
    q = os.path.join(tmp, "external_queue.jsonl")
    dq = os.path.join(tmp, "demand_queue.jsonl")
    vr = os.path.join(tmp, "verify_results.jsonl")

    print("== A. 同源与常量 ==")
    T("A1 等级值域恰 S/A/B/C", GRADES == ("S", "A", "B", "C"))
    T("A2 全部登记来源的等级都在值域内",
      all(v["grade"] in GRADES for v in SOURCE_GRADES.values()))
    T("A3 需求队列真源与 demand_scanner 同值（禁第二份清单）",
      DEMAND_PATH == DS.DEMAND_PATH)
    T("A4 状态机常量：候选 → 已登记 → 已验证（前缀）",
      ST_HYP_CANDIDATE == "HYP_CANDIDATE" and ST_HYP_REGISTERED == "HYP_REGISTERED"
      and ST_VERIFIED == "VERIFIED_")

    print("== B. 采集 / 去重去噪 / 分级 ==")
    items = [
        {"source_id": "official", "title": "交易所公告", "digest": "分红方案", "ts": "2026-10-08"},
        {"source_id": "research", "title": "研报方法", "digest": "动量因子检验", "ts": "2026-10-08"},
        {"source_id": "forum", "title": "社区传闻", "digest": "某票要涨", "ts": "2026-10-07"},
    ]
    r1 = ingest(items, as_of="2026-10-09", path=q)
    T("B1 三条入队且分级正确（S/A/C）",
      r1["ingested"] == 3 and [x["grade"] for x in r1["items"]] == ["S", "A", "C"])
    T("B2 全部 HYP_CANDIDATE（只进假设候选）",
      all(x["status"] == "HYP_CANDIDATE" for x in r1["items"]))
    r2 = ingest([items[0]], as_of="2026-10-09", path=q)
    T("B3 去重幂等：同指纹重放 reused、不追加", r2["reused"] == 1 and r2["ingested"] == 0)
    bad1 = [{"source_id": "weibo", "title": "t", "digest": "d", "ts": "2026-10-08"}]
    rb = ingest(bad1, as_of="2026-10-09", path=q)
    T("B4 未登记来源拒收（等级不可现场编造）",
      len(rb["rejected"]) == 1 and "UNKNOWN_SOURCE" in rb["rejected"][0]["reason"])
    bad2 = [{"source_id": "book", "title": "", "digest": "d", "ts": "2026-10-08"}]
    T("B5 去噪拒绝：title/digest 为空",
      len(ingest(bad2, as_of="2026-10-09", path=q)["rejected"]) == 1)
    bad3 = [{"source_id": "book", "title": "t", "digest": "d", "ts": "2026-10-10"}]
    rb3 = ingest(bad3, as_of="2026-10-09", path=q)
    T("B6 时间截断：ts > as_of 拒收（不采集未来的知识）",
      len(rb3["rejected"]) == 1 and "时间截断" in rb3["rejected"][0]["reason"])
    fp1 = r1["items"][0]["data_fingerprint"]
    T("B7 data_fingerprint 稳定（同输入重算同值）",
      fp1 == ingest([items[0]], as_of="2026-10-09", path=os.path.join(tmp, "q2.jsonl"))
      ["items"][0]["data_fingerprint"])
    T("B8 队列恰 3 行（拒绝项不入队）", len(_rows_of(q)) == 3)

    print("== C. 状态机 + 铁律闸（S 级无回测证据不得推进） ==")
    iid = r1["items"][0]["item_id"]          # S 级素材（official）
    try:
        mark_registered(iid, hyp_id="", path=q)
        T("C1 空 hyp_id 拒收", False)
    except ExternalError as ex:
        T("C1 空 hyp_id 拒收", "hyp_id 必填" in str(ex))
    try:
        mark_verified(iid, results_path=vr, path=q)
        T("C2 未登记先验证 → 状态机拒", False)
    except ExternalError as ex:
        T("C2 未登记先验证 → 状态机拒", "状态机非法" in str(ex))
    m1 = mark_registered(iid, hyp_id="H-EXT-1", path=q)
    T("C3 登记成功并绑定 hyp_id", m1["status"] == "HYP_REGISTERED"
      and m1["hyp_id"] == "H-EXT-1")
    try:
        mark_registered(iid, hyp_id="H-EXT-2", path=q)
        T("C4 重复登记拒（状态机单向）", False)
    except ExternalError as ex:
        T("C4 重复登记拒（状态机单向）", "状态机非法" in str(ex))
    try:
        mark_verified(iid, results_path=vr, path=q)
        T("C5 **铁律闸：S 级来源无回测证据 → 拒**", False)
    except ExternalError as ex:
        T("C5 **铁律闸：S 级来源无回测证据 → 拒（NO_VERIFICATION_EVIDENCE）**",
          "NO_VERIFICATION_EVIDENCE" in str(ex))
    with io.open(vr, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"hyp_id": "H-EXT-1", "kind": "RULE", "verdict": "SUPPORT",
                            "data_fingerprint": "md5:x"}, ensure_ascii=False) + "\n")
    m2 = mark_verified(iid, results_path=vr, path=q)
    T("C6 有回测证据 → VERIFIED_SUPPORT + 证据引用可追溯",
      m2["status"] == "VERIFIED_SUPPORT"
      and m2["verification_ref"]["hyp_id"] == "H-EXT-1"
      and m2["verification_ref"]["n_results"] == 1)

    print("== D. 学习需求触发器（主动器官） ==")
    gaps = [{"kind": "REPEAT_ERROR", "where": "L1L5.L2", "count": 3},
            {"kind": "WIN_BUT_WRONG", "where": "M1/M2/M3 全 REFUTE", "count": 1}]
    d1 = demand_trigger(gaps, as_of="2026-10-09", demand_path=dq)
    T("D1 缺口 → EXTERNAL_DEMAND 行（scanner 自动非手工）",
      d1["triggered"] == 2 and all(x["kind"] == "EXTERNAL_DEMAND"
                                   and x["scanner"] == "EXTERNAL_TRIGGER"
                                   for x in d1["items"]))
    T("D2 结构化三件：缺什么 / 去哪找 / 怎么验证",
      all(x["need"]["what"] and x["need"]["where_to"] and "回测" in x["need"]["how_to_verify"]
          for x in d1["items"]))
    T("D3 去哪找 = 机械映射（REPEAT_ERROR → research/data_portal）",
      d1["items"][0]["need"]["where_to"] == ["research", "data_portal"])
    d2 = demand_trigger(gaps, as_of="2026-10-09", demand_path=dq)
    T("D4 同日同缺口幂等不重复", d2["triggered"] == 0)
    d3 = demand_trigger([{"kind": "JUDGEMENT_QUALITY_LOW", "where": "SUPPORT 占比"}],
                        as_of="2026-10-09", demand_path=dq)
    T("D5 新缺口 → 新需求行", d3["triggered"] == 1)

    print("== E. 边界与真库零污染 ==")
    T("E1 真库 external_queue 未被自检触碰",
      (hashlib.md5(io.open(EXT_PATH, "rb").read()).hexdigest()
       if os.path.isfile(EXT_PATH) else "ABSENT") == real_fp)
    T("E2 真库 demand_queue 未被自检触碰",
      (hashlib.md5(io.open(DEMAND_PATH, "rb").read()).hexdigest()
       if os.path.isfile(DEMAND_PATH) else "ABSENT") == real_dq)
    T("E3 自检全程沙盒（无真库路径写入）",
      tmp.startswith(tempfile.gettempdir()))

    print("external 自检：%d 通过 / %d 失败" % (26 - bad[0], bad[0]))
    return bad[0] == 0


def _main(argv):
    if "--selftest" in argv:
        ok = _selftest()
        d = _discipline()
        raise SystemExit(0 if (ok and d) else 1)
    if "--discipline" in argv:
        raise SystemExit(0 if _discipline() else 1)
    if "--demand" in argv:
        print(json.dumps(_cli_demand(), ensure_ascii=False, indent=2))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
