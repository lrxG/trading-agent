#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ingest.py · data/raw 投放区把关件（0.10 配套 · 数据链施工批 · v1.0 · 2026-10-09 建）

用途：真实数据链的**供应商无关投放前把关**——采集器（无论何种数据源/手动投放）
      投放）统一经本件写入投放区，做到：字段快筛、来源标记、数据截止时间、
      重复/补数防护、交易日错位警示。**正式契约校验的单一真源仍是
      snapshot.write_snapshot**（核心 5 键落快照时的严格校验）；本件只做投放前
      快筛与元数据治理，不重复实现列类型转换（防双真源）。

★ 一、投放区布局（0.10 既有契约，本件零新增路径形态）
  data/raw/<date>/<type>.json|jsonl      数据文件（loader.load_ext / load_snapshot 消费）
  data/raw/<date>/<type>.meta.json       源信息 sidecar（loader 忽略：stem 含 .meta 不匹配）
  meta = {"schema": "v1", "source": "...", "fetched_at": "ISO时间", "data_upto":
          "YYYY-MM-DD 数据截止", "row_count": N, "replace": bool, "staged_at": ISO}
  —— 来源标记与数据截止时间**必填**（禁无源投放）；data_upto 是交易日错位防护锚点。

★ 二、时间纪律（错位防护）
  目录 date = 投放日（交易日口径）；data_upto = 数据实际覆盖截止。
  data_upto > date → 拒绝（FUTURE_DATA：未来数据禁投放）；
  data_upto < date → 警告不阻断（合法场景：盘前投放昨日收盘）——loader 消费时
  is_exact 按目录日标注，缺口由 diagnose/UNAVAILABLE 机制如实呈现。

★ 三、重复与补数
  同日同类型重投：默认拒绝（STAGED_EXISTS，防覆盖好数据）；replace=True = 补数/
  修正通道（覆盖 + meta.replace=true 留痕）。投放区非账本，不做历史版本堆积。

★ 四、部分成功（全有或全无）
  行级快筛发现坏行 → 返回 errors 明细，**默认不写任何文件**（半坏文件不投放）；
  调用方修数后重投。drop_bad=True = 过滤坏行投放剩余（meta 记 dropped 数）——
  只允许显式选择，禁静默。

★ 五、核心 5 键与扩展类型
  核心键（daily_quote/limit_up/dragon_tiger/news/env_metrics）列规格真源 =
  snapshot.CONTRACT（公开名，零重定义）；本件快筛 = 必需列存在 + code/ts 格式，
  数值/int 转换与严格校验留给 write_snapshot（不双实现）。
  扩展类型走 stage_ext（name 合法性复用 loader 规则：^\\w+$ 且不撞核心键）。

★ 六、边界
  唯一写点 = data/raw/<date>/ 内文件（X5 豁免）；零快照写入（write_snapshot 归
  settle/快照层）；零第三方数据依赖（本件不 import 任何数据 SDK——采集器在外部，
  经本件或手动落盘进投放区）。

用法：
  python tools/fs/ingest.py --selftest     # 沙盒全链（tempfile base，不碰真数据）
  python tools/fs/ingest.py --discipline   # 纪律判据
  from tools.fs.ingest import stage_core, stage_ext, diagnose_staging
"""
import io
import json
import os
import re
import sys
import tempfile
import datetime as _dt

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _FS)
import snapshot as S  # noqa: E402  （CONTRACT / SnapshotError / 日期规则真源）
import loader as LD   # noqa: E402  （投放区读取 / 类型规则真源）
from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）

SCHEMA_VERSION = 1
# 写点终点 fallback（landing 静态解析要求本模块字面量；真实终点由调用方经 path
# 传入，本常量恒不触发；data/raw/ 属 X5 豁免）
RAW_FALLBACK = os.path.join(ROOT, "data", "raw", "_unknown")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]?\d{0,2}:?\d{0,2}")
_CODE_RE = re.compile(r"^(sh|sz|bj)?\d{6}$", re.I)
_META_SUFFIX = ".meta.json"
# P0：显式声明件（投放区内、非契约文件、非数据）—— 记载「哪些键以显式空值投放、为什么」
DECL_FNAME = "_unresolved.decl.json"
# key → 文件名 映射（从 snapshot.CONTRACT 派生，零重定义）
_FNAME_OF = {spec["key"]: fname for fname, spec in S.CONTRACT.items()}


def _chk_date(v):
    """日期格式校验（格式真源 = snapshot 同款正则口径）。"""
    s = str(v or "").strip()
    if not _DATE_RE.match(s):
        raise IngestError("BAD_DATE: 必须是 YYYY-MM-DD，实际 %r" % (v,))
    return s


class IngestError(Exception):
    """投放被拒。str 以 REASON_CODE: 开头（每类拒绝可机械断言）。"""


def _now():
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _raw_dir(date, base=None):
    d = _chk_date(date)
    return os.path.join(LD.roots(base)["raw"], d)


# ── 行级快筛 ─────────────────────────────────────────────────────────────────
def _screen_rows(key, rows):
    """核心键行级快筛（列规格真源 = snapshot.CONTRACT）→ errors 列表（空=全过）。"""
    fname = _FNAME_OF.get(key)
    spec = S.CONTRACT.get(fname) if fname else None
    errors = []
    if spec is None:
        return errors
    cols = dict(spec["cols"])
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            errors.append({"row": i, "error": "row not dict"})
            continue
        for col, typ in cols.items():
            if col not in r or r[col] in (None, ""):
                continue                                  # 空值放行（快照层定严格度）
            v = r[col]
            if typ == "code" and not _CODE_RE.match(str(v)):
                errors.append({"row": i, "error": "bad code %r" % (v,)})
            elif typ == "codes":
                if not isinstance(v, list):
                    errors.append({"row": i, "error": "related_codes must be list"})
                elif any(not _CODE_RE.match(str(c)) for c in v):
                    errors.append({"row": i, "error": "bad related code in %r" % (v,)})
            elif typ == "text" and col in ("ts",) and not _TS_RE.match(str(v)):
                errors.append({"row": i, "error": "bad ts %r" % (v,)})
            elif typ == "num":
                try:
                    float(v)
                except (TypeError, ValueError):
                    errors.append({"row": i, "error": "bad num %s=%r" % (col, v)})
            elif typ == "int" and not isinstance(v, int):
                errors.append({"row": i, "error": "bad int %s=%r" % (col, v)})
    return errors


def _screen_ext(rows):
    """扩展类型行级快筛：dict 行 + code 格式（若带）+ ts 格式（若带）。"""
    errors = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            errors.append({"row": i, "error": "row not dict"})
            continue
        c = r.get("code")
        if c and not _CODE_RE.match(str(c)):
            errors.append({"row": i, "error": "bad code %r" % (c,)})
        t = r.get("ts")
        if t and not _TS_RE.match(str(t)):
            errors.append({"row": i, "error": "bad ts %r" % (t,)})
    return errors


# ── 投放（唯一写点家族） ──────────────────────────────────────────────────────
def stage_core(date, payload, *, source, base=None, replace=False,
               data_upto=None, drop_bad=False, declaration=None):
    """核心 5 键整包投放 → data/raw/<date>/<契约文件> + _meta.json。

    payload 键名 = snapshot.CONTRACT 的 key 集合（可只投部分键——部分采集成功
    是合法状态，缺什么 loader 如实 UNAVAILABLE）。
    返回 {"staged": [文件名...], "warnings": [...], "dropped": n}。

    ★ `declaration`（P0「显式声明阻塞后播种」）：非 None ⇒ 额外落盘
    `_unresolved.decl.json`（**声明件**，与 `_core.meta.json` 并列，同属投放区）。
    它只承载「哪些键以显式空值投放、为什么、谁授权的」，**不承载数据值**；
    投放方据此把"空值"与"无值"区分开，禁以省略冒充事实。
    """
    if not isinstance(payload, dict) or not payload:
        raise IngestError("BAD_INPUT: payload 必须是非空 dict（键=核心契约键）")
    d = _chk_date(date)
    upto = _chk_date(data_upto or date)
    if upto > d:
        raise IngestError("FUTURE_DATA: data_upto %s > 投放日 %s（未来数据禁投放）" % (upto, d))
    if not str(source or "").strip():
        raise IngestError("SOURCE_REQUIRED: source 必填（禁无源投放）")
    warnings = []
    if upto < d:
        warnings.append("DATA_LAG: data_upto %s < 投放日 %s（数据滞后——盘前投昨日收盘"
                        "等合法场景，loader 按 is_exact 如实标注）" % (upto, d))
    unknown = [k for k in payload if k not in S._KEYS]
    if unknown:
        raise IngestError("BAD_INPUT: 未知键 %s（核心契约键 = %s）" % (unknown, S._KEYS))

    rdir = _raw_dir(d, base)
    marker = os.path.join(rdir, "_staged.marker")
    if not replace and os.path.isdir(rdir) and any(
            f.endswith(tuple(S._FNAMES)) for f in os.listdir(rdir)):
        raise IngestError("STAGED_EXISTS: %s 已有核心投放（补数/修正用 replace=True）" % d)
    if replace:
        warnings.append("REPLACED: 本次投放覆盖 %s 既有核心文件（补数通道留痕）" % d)

    dropped = 0
    staged, out_payload = [], {}
    for key in S._KEYS:
        if key not in payload:
            continue                                  # 部分采集成功 = 合法
        rows = payload[key]
        errs = _screen_rows(key, rows if isinstance(rows, list) else [rows])
        if errs:
            if not drop_bad:
                raise IngestError("ROW_ERRORS: %s 快筛失败 %d 行（修数重投或显式 "
                                  "drop_bad=True）: %s..." % (key, len(errs), errs[:3]))
            bad = {e["row"] for e in errs}
            rows = [r for i, r in enumerate(rows) if i not in bad]
            dropped += len(bad)
        out_payload[key] = rows

    os.makedirs(rdir, exist_ok=True)
    for key, rows in out_payload.items():
        _write_raw(os.path.join(rdir, _FNAME_OF[key]), rows)
    _write_json_meta(os.path.join(rdir, "_core.meta.json"), {
        "schema": SCHEMA_VERSION, "source": str(source).strip(),
        "fetched_at": _now(), "data_upto": upto, "replace": bool(replace),
        "keys": sorted(out_payload), "dropped": dropped, "staged_at": _now()})
    staged = [_FNAME_OF[k] for k in out_payload] + ["_core.meta.json"]
    if declaration is not None:
        if not isinstance(declaration, dict):
            raise IngestError("BAD_INPUT: declaration 必须是 dict（显式声明件内容）")
        _write_json_meta(os.path.join(rdir, DECL_FNAME),
                         dict(declaration, date=d, staged_at=_now()))
        staged.append(DECL_FNAME)
    return {"staged": staged, "warnings": warnings, "dropped": dropped, "date": d}


def stage_ext(type_name, date, rows, *, source, base=None, replace=False,
              data_upto=None, drop_bad=False):
    """扩展类型投放 → data/raw/<date>/<type>.jsonl + <type>.meta.json。

    类型名合法性复用 loader 规则（^\\w+$ 且不撞核心键——撞名在 loader 侧被
    _check_type 拒收，投放侧提前拦截同规则）。
    """
    t = LD._check_type(type_name)                     # 复用 loader 校验（零重定义）
    d = _chk_date(date)
    upto = _chk_date(data_upto or date)
    if upto > d:
        raise IngestError("FUTURE_DATA: data_upto %s > 投放日 %s" % (upto, d))
    if not str(source or "").strip():
        raise IngestError("SOURCE_REQUIRED: source 必填（禁无源投放）")
    if not isinstance(rows, list) or not rows:
        raise IngestError("BAD_INPUT: rows 必须是非空 list")
    warnings = []
    if upto < d:
        warnings.append("DATA_LAG: data_upto %s < 投放日 %s" % (upto, d))

    rdir = _raw_dir(d, base)
    data_p = os.path.join(rdir, t + ".jsonl")
    if not replace and os.path.isfile(data_p):
        raise IngestError("STAGED_EXISTS: %s/%s.jsonl 已投放（补数用 replace=True）" % (d, t))
    if replace:
        warnings.append("REPLACED: 覆盖既有 %s.jsonl（补数通道留痕）" % t)

    errs = _screen_ext(rows)
    if errs:
        if not drop_bad:
            raise IngestError("ROW_ERRORS: 快筛失败 %d 行: %s..." % (len(errs), errs[:3]))
        bad = {e["row"] for e in errs}
        rows = [r for i, r in enumerate(rows) if i not in bad]
        if not rows:
            raise IngestError("ROW_ERRORS: 过滤坏行后无剩余")
    os.makedirs(rdir, exist_ok=True)
    _write_jsonl(data_p, rows)
    _write_jsonl_meta(os.path.join(rdir, t + _META_SUFFIX), {
        "schema": SCHEMA_VERSION, "type": t, "source": str(source).strip(),
        "fetched_at": _now(), "data_upto": upto, "replace": bool(replace),
        "row_count": len(rows), "staged_at": _now()})
    return {"staged": [t + ".jsonl", t + _META_SUFFIX],
            "warnings": warnings, "dropped": len(errs) if drop_bad else 0, "date": d}


def _atomic_write(p, writer):
    """唯一写点（X5 豁免 data/raw/；形态 = external._rewrite 同构：mkstemp 中转 +
    终点内联 BoolOp 指向本模块常量 RAW_FALLBACK——landing 静态解析要求）。"""
    _WG.check_write(p or RAW_FALLBACK)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p or RAW_FALLBACK), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        writer(f)
    os.replace(tmp, p or RAW_FALLBACK)


def _write_raw(p, rows):
    """核心键文件写出：按 snapshot.CONTRACT 的 fmt（csv/json/jsonl）落投放区。"""
    key = os.path.basename(p)
    spec = S.CONTRACT[key]
    fmt = spec["fmt"]
    if fmt == "csv":
        import csv as _csv
        names = [c[0] for c in spec["cols"]]

        def _csv_writer(f):
            w = _csv.DictWriter(f, fieldnames=names, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in names})
        _atomic_write(p, _csv_writer)
    elif fmt == "json":
        _atomic_write(p, lambda f: json.dump(rows, f, ensure_ascii=False))
    else:
        _atomic_write(p, lambda f: [f.write(json.dumps(r, ensure_ascii=False) + "\n")
                                    for r in rows])


def _write_jsonl(p, rows):
    _atomic_write(p, lambda f: [f.write(json.dumps(r, ensure_ascii=False) + "\n")
                                for r in rows])


def _write_jsonl_meta(p, meta):
    _atomic_write(p, lambda f: json.dump(meta, f, ensure_ascii=False, indent=1))


def _write_json_meta(p, meta):
    _atomic_write(p, lambda f: json.dump(meta, f, ensure_ascii=False, indent=1))


# ── 快照正式化（采集 → 校验 → 快照生成 的接口闭环；转发非重实现） ─────────────
def declare_unresolved(date, declaration, *, base=None):
    """显式声明件的**唯一刷新入口**：只写 `_unresolved.decl.json`，**绝不碰数据文件**。

    用处：授权文案 / 已知缺口登记变更后，把已投放日的声明件与当前代码一致化
    （幂等可重跑；数据文件逐字节不动）。要求该日**已有投放区**（先 `stage_core`）。
    """
    if not isinstance(declaration, dict):
        raise IngestError("BAD_INPUT: declaration 必须是 dict（显式声明件内容）")
    d = _chk_date(date)
    rdir = _raw_dir(d, base)
    if not os.path.isdir(rdir):
        raise IngestError("NOT_STAGED: %s 无投放区（须先 stage_core；禁凭空造声明）" % d)
    p = os.path.join(rdir, DECL_FNAME)
    _write_json_meta(p, dict(declaration, date=d, refreshed_at=_now()))
    return p


def promote_snapshot(date, *, base=None, force=False, provenance=None):
    """核心 5 键齐 → 借 snapshot.write_snapshot 把该日投放正式化为快照。

    校验真源 = snapshot._normalize（严格契约校验在写盘前发生，不符即抛错）；
    本件只做前置检查（核心键齐缺 + meta 来源在案）与转发，零重实现。
    返回 write_snapshot 结果（"WRITTEN" / "EXISTS"）。

    ★ `provenance`：透传给 `write_snapshot` 的 A9 准入溯源（AS_OF / COVERAGE 两类判定用）。
    未显式给出时，自动继承投放区的显式声明件 `_unresolved.decl.json` 里的 `provenance`
    字段 —— 声明随数据同行，禁在正式化环节把声明丢掉（丢了就成了"未证明"而非"已声明")。
    """
    d = _chk_date(date)
    rdir = _raw_dir(d, base)
    present = [k for k, fn in _FNAME_OF.items() if os.path.isfile(os.path.join(rdir, fn))]
    missing = [k for k in _FNAME_OF if k not in present]
    if missing:
        raise IngestError("CORE_INCOMPLETE: 核心键缺 %s——部分采集成功不是快照"
                          "（补齐后重试；缺口由 loader UNAVAILABLE 如实呈现）" % missing)
    mp = os.path.join(rdir, "_core.meta.json")
    if not os.path.isfile(mp):
        raise IngestError("SOURCE_REQUIRED: 该日投放无 _core.meta.json（禁无源正式化）")
    if provenance is None:
        dp = os.path.join(rdir, DECL_FNAME)
        if os.path.isfile(dp):
            with io.open(dp, encoding="utf-8") as f:
                provenance = (json.load(f) or {}).get("provenance") or None
    # 组装 payload（从投放区读回，交 snapshot 严格校验）
    payload = {}
    for k, fn in _FNAME_OF.items():
        p = os.path.join(rdir, fn)
        fmt = S.CONTRACT[fn]["fmt"]
        with io.open(p, encoding="utf-8") as f:
            if fmt == "csv":
                import csv as _csv
                payload[k] = list(_csv.DictReader(f))
            elif fmt == "json":
                payload[k] = json.load(f)
            else:
                payload[k] = [json.loads(ln) for ln in f if ln.strip()]
    return S.write_snapshot(d, payload, root=LD.roots(base)["snapshot"], force=force,
                            provenance=provenance)


# ── 投放诊断（缺口/错位/部分成功的可验证呈现） ────────────────────────────────
def diagnose_staging(as_of, base=None):
    """≤as_of 各投放日盘点：核心键齐缺 / 扩展类型 / meta 错位。

    返回 Record{date: {core_present, core_missing, ext_types, lag_days?, sources}}。
    """
    d0 = _chk_date(as_of)
    out = {}
    for d in LD._raw_dates(base=base, upto=d0):
        rdir = os.path.join(LD.roots(base)["raw"], d)
        names = os.listdir(rdir)
        core_present = [k for k, fn in _FNAME_OF.items() if fn in names]
        core_missing = [k for k in _FNAME_OF if k not in core_present]
        exts = sorted(os.path.splitext(n)[0] for n in names
                      if n.endswith(".jsonl") and os.path.splitext(n)[0] not in S._KEYS)
        entry = {"core_present": core_present, "core_missing": core_missing,
                 "ext_types": exts, "sources": [], "data_upto": None}
        mp = os.path.join(rdir, "_core.meta.json")
        if os.path.isfile(mp):
            with io.open(mp, encoding="utf-8") as f:
                meta = json.load(f)
            entry["sources"].append(meta.get("source"))
            entry["data_upto"] = meta.get("data_upto")
            try:
                entry["lag_days"] = (_dt.date.fromisoformat(d)
                                     - _dt.date.fromisoformat(meta["data_upto"])).days
            except (KeyError, ValueError):
                pass
        out[d] = entry
    return out


# ── 自检（tempfile 沙盒全链：stage → loader 消费 → 快照生成 → 回读） ──────────
def _selftest():
    ok = []
    def T(name, cond):
        ok.append(cond)
        print(("PASS" if cond else "FAIL"), name)

    B = tempfile.mkdtemp(prefix="ing_")
    D = "2026-10-08"

    # 真库零触碰：**前后同态**判据（P0 播种后生产已有授权投放目录 ⇒ 不能再要求"为空"）
    def _prod_dated_dirs():
        r = os.path.join(S.ROOT if hasattr(S, "ROOT") else os.getcwd(), "data", "raw")
        return sorted(x for x in (os.listdir(r) if os.path.isdir(r) else [])
                      if len(x) == 10 and x[4] == "-" and x[7] == "-"
                      and x.replace("-", "").isdigit())
    _PROD_DATED_BEFORE = _prod_dated_dirs()

    PAYLOAD = {
        "daily_quote": [{"code": "sh600000", "name": "浦发银行", "open": 10.0,
                         "high": 10.5, "low": 9.9, "close": 10.2, "pct": 1.2,
                         "vol": 100000, "amount": 1020000}],
        "news": [{"news_id": "N1", "ts": "2026-10-08 09:00", "source": "东财",
                  "title": "某公司公告", "body": "正文", "related_codes": ["sh600000"]}],
        "env_metrics": {"limit_up_count": 50, "max_board": 5, "turnover": 1.5,
                        "up_down_ratio": 2.0, "fail_rate": 0.3, "index_ma20": 0.02},
    }

    # A 核心键投放：3 键部分成功 → 未正式化时核心接口如实 UNAVAILABLE
    r = stage_core(D, PAYLOAD, source="test_channel", base=B, data_upto=D)
    T("A1 核心投放 staged=4 文件（3 数据+meta）", len(r["staged"]) == 4)
    T("A2 核心 5 键未齐 → load_quote 如实 UNAVAILABLE",
      LD.load_quote(D, "sh600000", base=B) is LD.UNAVAILABLE)
    try:
        promote_snapshot(D, base=B)
        T("A3 键未齐 promote 拒", False)
    except IngestError as e:
        T("A3 键未齐 promote 拒 CORE_INCOMPLETE", "CORE_INCOMPLETE" in str(e))
    try:
        LD.load_ext("daily_quote", D, "sh600000", base=B)
        T("A4 load_ext 拒核心键名（防两套口径）", False)
    except S.SnapshotError:
        T("A4 load_ext 拒核心键名（防两套口径）", True)

    # A+ 补齐 5 键 → promote 正式化 → 核心接口消费
    full = dict(PAYLOAD, limit_up=[], dragon_tiger=[])
    stage_core(D, full, source="test_channel", base=B, replace=True, data_upto=D)
    pr = promote_snapshot(D, base=B)
    T("A5 promote_snapshot 正式化 WRITTEN", pr == "WRITTEN")
    q = LD.load_quote(D, "sh600000", base=B)
    T("A6 load_quote 消费成功", q is not None and q["code"] == "sh600000")
    n = LD.load_news(D, base=B)
    T("A7 load_news 消费成功", len(n) == 1 and n[0]["news_id"] == "N1")
    snap = LD.load_snapshot(D, base=B)
    T("A8 load_snapshot 消费（limit_up/dragon_tiger=空 Series 非缺席）",
      snap is not LD.UNAVAILABLE and len(snap["limit_up"]) == 0)
    T("A9 早于全部快照的日期 → UNAVAILABLE（缺就是缺）",
      LD.load_snapshot("2026-10-01", base=B) is LD.UNAVAILABLE)

    # B 字段校验拒绝
    try:
        stage_core(D, {"daily_quote": [{"code": "bad", "name": "x", "open": 1,
                                        "high": 1, "low": 1, "close": 1, "pct": 1,
                                        "vol": 1, "amount": 1}]},
                   source="t", base=B, replace=True)
        T("B1 坏 code 拒", False)
    except IngestError as e:
        T("B1 坏 code 拒 ROW_ERRORS", "ROW_ERRORS" in str(e))
    try:
        stage_core(D, {"daily_quote": PAYLOAD["daily_quote"]}, source="", base=B,
                   replace=True)
        T("B2 无 source 拒", False)
    except IngestError as e:
        T("B2 无 source 拒 SOURCE_REQUIRED", "SOURCE_REQUIRED" in str(e))

    # C 未来数据 / 数据滞后
    try:
        stage_core("2026-10-09", PAYLOAD, source="t", base=B, data_upto="2026-10-10")
        T("C1 data_upto>date 拒", False)
    except IngestError as e:
        T("C1 data_upto>date 拒 FUTURE_DATA", "FUTURE_DATA" in str(e))
    r2 = stage_core("2026-10-09", PAYLOAD, source="t", base=B, data_upto="2026-10-08")
    T("C2 数据滞后 → 警告不阻断（盘前投昨日收盘）",
      any("DATA_LAG" in w for w in r2["warnings"]))

    # D 重复投放 / replace 补数
    try:
        stage_core(D, PAYLOAD, source="t2", base=B)
        T("D1 重复投放拒", False)
    except IngestError as e:
        T("D1 重复投放拒 STAGED_EXISTS", "STAGED_EXISTS" in str(e))
    r3 = stage_core(D, PAYLOAD, source="t2", base=B, replace=True)
    T("D2 replace 补数 + 留痕", any("REPLACED" in w for w in r3["warnings"]))

    # E 部分成功（坏行过滤须显式）
    bad_pay = {"daily_quote": PAYLOAD["daily_quote"] +
               [{"code": "bad", "name": "x", "open": 1, "high": 1, "low": 1,
                 "close": 1, "pct": 1, "vol": 1, "amount": 1}]}
    try:
        stage_core("2026-10-10", bad_pay, source="t", base=B)
        T("E1 坏行默认全拒不投放", False)
    except IngestError as e:
        T("E1 坏行默认全拒 ROW_ERRORS", "ROW_ERRORS" in str(e))
    r4 = stage_core("2026-10-10", bad_pay, source="t", base=B, drop_bad=True)
    T("E2 drop_bad 显式过滤 dropped=1", r4["dropped"] == 1)

    # F 扩展类型投放 + load_ext 消费
    r5 = stage_ext("hot_search", D, [{"code": "sz000001", "rank": 1, "ts": "2026-10-08"}],
                   source="test_channel", base=B, data_upto=D)
    T("F1 扩展投放 staged=2（数据+meta）", len(r5["staged"]) == 2)
    he = LD.load_ext("hot_search", D, base=B)
    T("F2 load_ext 消费成功", isinstance(he, LD.Series) and he[0]["code"] == "sz000001")
    T("F3 list_ext_types 盘点", "hot_search" in dict(LD.list_ext_types(D, base=B)))
    try:
        stage_ext("daily_quote", "2026-10-11", [{"x": 1}], source="t", base=B)
        T("F4 撞核心键拒", False)
    except Exception as e:
        T("F4 撞核心键拒（loader 规则复用）", "撞名" in str(e))

    # G 投放诊断
    dg = diagnose_staging("2026-10-10", base=B)
    T("G1 诊断：各投放日可见", "2026-10-08" in dg and "2026-10-09" in dg)
    d08 = dg["2026-10-08"]
    T("G2 诊断：补齐后核心键齐全如实呈现", d08 is not None
      and d08["core_missing"] == [] and set(d08["core_present"]) == set(_FNAME_OF))

    # H promote 幂等与 force（settle 重跑安全同款口径）
    stage_core("2026-10-12", dict(PAYLOAD, limit_up=[], dragon_tiger=[]),
               source="t", base=B)
    pr0 = promote_snapshot("2026-10-12", base=B)
    T("H0 首次正式化 WRITTEN", pr0 == "WRITTEN")
    pr2 = promote_snapshot("2026-10-12", base=B)
    T("H1 重复 promote → EXISTS（幂等命中）", pr2 == "EXISTS")
    pr3 = promote_snapshot("2026-10-12", base=B, force=True)
    T("H2 force 修正重写 → WRITTEN", pr3 == "WRITTEN")
    q2 = LD.load_quote("2026-10-12", "sh600000", base=B)
    T("H3 快照层消费（盘后腿正式数据）", q2 is not None and q2["close"] == 10.2)

    # I 真库零触碰（base 换根，本目录下无 data/raw 生成）
    # v1.4：日历落点 `data/raw/meta/trade_cal.csv` 是**只读元数据** ⇒ 判据由「data/raw 不存在」
    #       精确化为「无**投放日期目录**」（非日期名目录：meta/ 等不参与）。
    # v1.5：P0 生产播种（2026-10-13 授权）后生产**已有**授权投放日期目录 ⇒ 判据再精确化为
    #       「**自检前后同态**」—— 这才是零污染的真实不变量（要求"为空"已不成立）。
    _prod_dated_after = _prod_dated_dirs()
    T("I1 真库 data/raw 投放日期目录集**自检前后不变**（before=%d after=%d；日历 meta/ 不算投放）"
      % (len(_PROD_DATED_BEFORE), len(_prod_dated_after)),
      _prod_dated_after == _PROD_DATED_BEFORE)

    n_pass = sum(1 for c in ok if c)
    print("\n== ingest --selftest: %d/%d 通过 ==" % (n_pass, len(ok)))
    return 0 if n_pass == len(ok) else 1


# ── 纪律判据 ─────────────────────────────────────────────────────────────────
def _discipline(verbose=True):
    ok = []
    def T(name, cond):
        ok.append(cond)
        if verbose:
            print(("PASS" if cond else "FAIL"), name)

    with io.open(os.path.abspath(__file__), encoding="utf-8") as f:
        src = f.read()

    # D1 零第三方数据依赖（采集器在外部；本件只吃已投放的文件）
    #    机械判据：生产区不得出现任何网络/第三方数据 SDK 的 import —— 按**模式**判，
    #    不列举具体库名（列举法会随库名变化漏判，且把已废弃的库名永久留在代码里）。
    _NET_IMPORT = re.compile(
        r"^\s*(?:import|from)\s+("
        r"requests|urllib|urllib2|urllib3|httpx|aiohttp|socket|http\.client|"
        r"pandas_datareader|tushare|baostock|efinance|adata|pywencai|"
        r"\w*(?:ak|ts|bs)_?share\w*|"
        r"ak\w*share|"
        r"openai|anthropic|llm"
        r")\b", re.M)
    T("D1 零网络/第三方数据 SDK import（模式判据，不列库名）",
      not _NET_IMPORT.search(src))

    # D2 快照正式化 = 转发 snapshot.write_snapshot（零重实现；判据串拼装防自指）
    body = re.sub(r'""".*?"""', "", src, flags=re.S)
    sig = ("write" + "_snapshot(")
    T("D2 write_snapshot 仅 promote 转发调用",
      body.count(sig) == 1 and ("S." + sig) in body)
    nz = ("_norm" + "alize")
    T("D2b 零重实现校验逻辑", nz not in body)

    # D3 列规格/类型规则零重定义（消费 snapshot.CONTRACT 与 loader 校验）
    T("D3 复用 snapshot.CONTRACT", "S.CONTRACT" in src and "S._KEYS" in src)
    T("D3b 复用 loader 类型校验", "LD._check_type" in src)

    # D4 来源标记强制（source 必填两处）
    T("D4 source 必填", src.count("SOURCE_REQUIRED") >= 2)

    # D5 未来数据拒绝
    T("D5 FUTURE_DATA 闸", "FUTURE_DATA" in src)

    n_pass = sum(1 for c in ok if c)
    print("\n== ingest --discipline: %d/%d 通过 ==" % (n_pass, len(ok)))
    return 0 if n_pass == len(ok) else 1


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    if "--discipline" in argv:
        return _discipline()
    if "--promote" in argv:                    # 投放区 → 快照区正式化（生产写，须持票）
        try:
            date = argv[argv.index("--promote") + 1]
        except IndexError:                                     # noqa: BLE001
            print("⛔ --promote 需跟日期", file=sys.stderr)
            return 2
        # `--force` = 数据修正后重跑通道（2026-10-22 补）。
        # 缺它会怎样：投放区数据被补齐/修正后，promote 因「该日已有快照」返回
        # EXISTS 而**静默不改**（实测：补了龙虎榜+修了 news 列，快照仍是 0 行）。
        # 语义仍受闸门约束 —— force 只放宽「同日已存在」这一条，不放宽取票。
        r = promote_snapshot(date, force=("--force" in argv),
                             provenance={"actor": "cli", "reason": "投放区正式化"
                                         + ("（force 修正重跑）" if "--force" in argv else "")})
        print("promote %s → %s%s" % (date, r,
                                     "（force）" if "--force" in argv else ""))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
