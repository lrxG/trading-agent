#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""snapshot.py · 信息截断（施工方案 0.2 · v1.0 · 2026-10-08 建）

用途：把「某交易日的行情信息集」冻结成一份日快照，让历史 / 回测 / 复盘 / 步进补单
      只能看到 ≤ 该日的数据 —— 这是设计方案 §11.1 三锁之「锁① 信息截断」的物理实现。
      没有它，步进补的单就是拿未来结果编的（后视镜造假器）。

两个落点（同属「行情数据轴」：输入数据，既非知识、也非状态，不受 0.7 五落点约束）：
  · data/raw/<date>/       采集投放区（输入轴；采集器投放 5 个契约文件，本模块只读）
                           A9 起可另投可选件 `_provenance.json`（非契约文件；给 as_of/证据等级/universe）
  · data/snapshot/<date>/  权威快照（归一化 + 契约校验后的产物：5 契约文件 + 1 判决 sidecar）

契约②（列结构写死 · 唯一真源 = 本文件 CONTRACT，别处禁再抄一份）：
  daily_quote.csv   code,name,open,high,low,close,pct,vol,amount  as_of=收盘后（最终日线，非实时 close）
  limit_up.csv      code,name,boards,first_seal_time,seal_amount,reason
  dragon_tiger.csv  code,name,seat,buy,sell,net
  news.jsonl        news_id,ts,source,title,body,related_codes    （每行一个 JSON 对象；news_id=稳定引用 ID）
  env_metrics.json  limit_up_count,max_board,turnover,up_down_ratio,fail_rate,index_ma20
                    （键名 = 英文 snake_case，与 regime(signals) 参数一一对应；禁中英两套键）

口径（一次写死，禁两套）：
  pct = 涨跌幅百分数（10.02 表示 +10.02%，不是 0.1002）
  vol = 成交量(股)；amount / seal_amount / buy / sell / net / turnover = 金额(元)
  index_ma20 = 指数 20 日均值(点)；boards = 连板数(1=首板)；first_seal_time = HH:MM(:SS)
  code = 6 位数字，可带 sh/sz/bj 前缀（小写）；★禁传 int（会丢前导零，静默算错）
  缺值 = 显式空（CSV 空字段 / JSON null），读取端按「数据缺口」处理，禁静默当 0
  编码 = UTF-8 无 BOM + LF

API（签名即冻结契约，骨架件 F1）：
  ingest_snapshot(date, ...)  盘后写当日快照：读投放区 → 归一化 → 契约校验 → 原子落盘
  load_asof(date, ...)        只返回 ≤date 的最新一日快照（物理拿不到 date 之后的任何东西）
  load_snapshot(date, ...)    精确读某一日（不存在 → UNAVAILABLE）
  available_dates(...)        列出快照日（回测 / 步进按日遍历用）
  check_snapshot(date, ...)   只读校验某日快照是否符契约 + 判决是否在场且可复现（[] = 合规）
  UNAVAILABLE                 数据缺口哨兵（与 0.10 loader 同一语义：is UNAVAILABLE）

A9 准入闸 API（三层阻断，详见下方 A9 段）：
  write_snapshot(date, ..., provenance=None)  层① 写前判定（声明外新缺陷 ⇒ AdmissionRejected）
  admission_of(date, ...)                     层③ 读判决（缺判决 ⇒ 如实报缺，不假定通过）
  assert_admissible(date, ..., need=...)      层③ 消费前断言（按需声明类别）
  AdmissionRejected                           准入闸拒绝（层① 拒写 / 层③ 断言失败）
  admission.ADMISSION_FNAME                   sidecar 文件名 `_admission.json`

触发者（施工 0.2 写死，防无快照空判）：tools/risk/settle.py 步 1 先调 ingest_snapshot(date)
  → 再判「行情落定」打勾。快照先行、落定后判，先后依赖不得颠倒。

插入式扩展（用户令 2026-10-08：骨架先定、插件后插）：
  接真实采集器 = 采集器把 5 个契约文件写进 data/raw/<date>/，本模块零改动。
  禁在本模块内直连数据源、禁用 LLM 补数、禁自造字段。

用法：
  python tools/fs/snapshot.py              # 查看快照目录现状（只读）
  python tools/fs/snapshot.py --selftest   # 内置验收（临时目录，不碰真数据）
  from snapshot import ingest_snapshot, load_asof

规则：日快照一经落盘不得回改（force 仅用于数据修正后的重跑，须留痕）；禁在别处内联快照读写；
      schema / 口径变更须升 SCHEMA_VERSION + 同步施工方案 0.2。
"""
import csv
import datetime as _dt
import io
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools", "fs")))
from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）

SCHEMA_VERSION = 1
SNAPSHOT_ROOT = os.path.join(ROOT, "data", "snapshot")
RAW_ROOT = os.path.join(ROOT, "data", "raw")

# ── 契约②：5 文件 × 列结构（唯一真源） ─────────────────────────────────────────
# kind: code=6位代码 / text=字符串 / num=浮点 / int=整数 / time=HH:MM(:SS) / codes=代码列表(仅 jsonl)
CONTRACT = {
    "daily_quote.csv": {"key": "daily_quote", "fmt": "csv", "cols": [
        ("code", "code"), ("name", "text"), ("open", "num"), ("high", "num"), ("low", "num"),
        ("close", "num"), ("pct", "num"), ("vol", "num"), ("amount", "num")]},
    "limit_up.csv": {"key": "limit_up", "fmt": "csv", "cols": [
        ("code", "code"), ("name", "text"), ("boards", "int"), ("first_seal_time", "time"),
        ("seal_amount", "num"), ("reason", "text")]},
    "dragon_tiger.csv": {"key": "dragon_tiger", "fmt": "csv", "cols": [
        ("code", "code"), ("name", "text"), ("seat", "text"),
        ("buy", "num"), ("sell", "num"), ("net", "num")]},
    "news.jsonl": {"key": "news", "fmt": "jsonl", "cols": [
        ("news_id", "text"), ("ts", "text"), ("source", "text"), ("title", "text"),
        ("body", "text"), ("related_codes", "codes")]},
    "env_metrics.json": {"key": "env_metrics", "fmt": "json", "cols": [
        ("limit_up_count", "int"), ("max_board", "int"), ("turnover", "num"),
        ("up_down_ratio", "num"), ("fail_rate", "num"), ("index_ma20", "num")]},
}
_FNAMES = tuple(CONTRACT.keys())
_KEYS = tuple(s["key"] for s in CONTRACT.values())

_CODES_RE = re.compile(r"^(sh|sz|bj)?\d{6}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


class SnapshotError(Exception):
    """快照操作被拒（契约不符 / 日期非法 / 重复写入）——一律显式抛错，不静默降级。"""


class SnapshotUnavailable(SnapshotError):
    """无数据源（投放区缺失 / 契约文件不全）——数据缺口，绝不编造。"""


class _Unavailable(object):
    """数据缺口哨兵：`x is UNAVAILABLE` 为真；布尔值为 False；禁与字符串比较。"""
    __slots__ = ()

    def __repr__(self):
        return "UNAVAILABLE"

    def __bool__(self):
        return False

    def __eq__(self, other):
        return other is self

    def __hash__(self):
        return hash("__UNAVAILABLE__")


UNAVAILABLE = _Unavailable()


# ── A9 数据准入闸接线（2026-10-13 · 三层阻断） ───────────────────────────────
# `admission.py` 提供四类**互不替代**的判定（STRUCTURE / SEMANTIC / AS_OF / COVERAGE），
# 但它自己不接入任何数据流（其纪律自证①＝零写点）。本模块是**唯一接入点**：
#
#   层① 写入前：对归一化后的 payload 逐契约文件跑 `admit()`。**只拒「写入方自证矛盾」**的
#        负载 ⇒ 抛 `AdmissionRejected`，快照不得落盘（判据 = 形状不符；或**已声明**的
#        AS_OF / COVERAGE 判定失败，如自称 `close_final` 却只给 E1 档名证据）。
#   层② 写入后自检：tmp 写完即重读 5 契约文件 + sidecar，**复算判决**并断言
#        「落盘判决 == 由落盘内容复算的判决」；换入正式位后再校一次。不一致 ⇒ 抛错。
#   层③ 消费前：`admission_of()` / `assert_admissible()` —— 消费方取数前断言，缺判决如实报缺。
#
# ★ 层① 为什么**不**按「任一 `failed` 即拒写」（两类理由，缺一不可）：
#   ① **已声明阻塞是粘性的**：`DECLARED_BLOCKS` 里有 3/5 契约文件（limit_up / dragon_tiger /
#      env_metrics）恒阻塞；一律拒写 ⇒ 任何快照都写不进 ⇒ 整条盘后链死。静默放过又违反
#      空值闸（「列在 ≠ 值有」）。正解 = **可强制、不可静默通过的判决记录随快照同行**：
#      阻塞照记、照阻塞，只是"写入"这个动作不被已声明的结构缺陷拦死。销路在层③。
#   ② **数据层空值不是拒写理由**：`write_snapshot` 是 **5 文件全有全无**的原子写入口，
#      因单文件一个空值拒写 ⇒ 连带毁掉另 4 个**完好**文件 —— 这比"带缺陷落盘"是**更严重**的
#      静默失败（消费方看到的是"整日无数据"，而非"某文件语义阻塞"）。且空值可能是消费方
#      **已建模的合法状态**：实例 = `scan.py` 路4 明写「related_codes 非空 → 相关 code；
#      空 → unmapped 不猜」，空值是其正常分支而非错误态。
#      ⇒ 数据层缺陷**一律如实入判**（`data_defects_recorded_only` + `blocked_classes`），
#        由层③ 按消费方**真实需求**声明 `need` 精确拦截 —— 这才是按文件、按用途的粒度。
#
# 判决落点 = `data/snapshot/<date>/_admission.json`（**与 5 契约文件同批原子换入**，
# 物理上不可能与它所判的那份快照分离，也不可能指向别日）。
# 禁：本模块**不得**改写 admission.py 的判定逻辑（四类互不替代 / 空值闸 / 粘性阻塞归它管）。
ADMISSION_FNAME = "_admission.json"
ADMISSION_SCHEMA = "admission@v1"
_ADMISSION_CHECKS = ("STRUCTURE", "SEMANTIC", "AS_OF", "COVERAGE")
# 层① 拒写判据分组：**只拒「写入方自证矛盾」**（形状不符；或自称某档却证据不足）
_SHAPE_CHECKS = ("STRUCTURE",)
_DECLARATION_CHECKS = ("AS_OF", "COVERAGE")
# 数据层缺陷（值层空值）**不拒写**，如实入判、由层③ 按消费方真实需求拦
_DATA_CHECKS = ("SEMANTIC",)


class AdmissionRejected(SnapshotError):
    """准入闸拒绝（层① 写前拒写 / 层③ 消费前断言失败）——显式抛错，禁静默降级。"""


# ── 日期 ─────────────────────────────────────────────────────────────────────
def _check_date(date):
    """日期硬校验（YYYY-MM-DD），返回规范字符串。"""
    s = date.isoformat() if hasattr(date, "isoformat") else str(date)
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise SnapshotError("快照日期必须是 YYYY-MM-DD，收到: %r" % (date,))
    try:
        _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        raise SnapshotError("快照日期非法: %r" % (date,))
    return s


# ── 值归一化（写盘前唯一关口：不符契约在这里被拦下，绝不半截落盘） ───────────────
def _n_code(v, where):
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, str):
        raise SnapshotError("%s: code 必须是字符串（收到 %r）——传 int 会丢前导零，属静默算错"
                            % (where, v))
    s = v.strip().lower()
    if not _CODES_RE.match(s):
        raise SnapshotError("%s: code 格式非法 %r（须 6 位数字，可带 sh/sz/bj 前缀）" % (where, v))
    return s


def _n_codes(v, where):
    if v is None or v == "":
        return None
    if not isinstance(v, (list, tuple)):
        raise SnapshotError("%s: related_codes 必须是列表，收到 %r" % (where, type(v).__name__))
    return [_n_code(x, where) for x in v]


def _n_text(v, where):
    if v is None or v == "":
        return None
    if isinstance(v, (dict, list, tuple)):
        raise SnapshotError("%s: 文本字段收到 %s" % (where, type(v).__name__))
    return v if isinstance(v, str) else str(v)


def _n_time(v, where):
    if v is None or v == "":
        return None
    s = v if isinstance(v, str) else str(v)
    if not _TIME_RE.match(s.strip()):
        raise SnapshotError("%s: 时间格式非法 %r（须 HH:MM 或 HH:MM:SS）" % (where, v))
    return s.strip()


def _n_num(v, where):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise SnapshotError("%s: 数值字段收到 bool" % where)
    try:
        return float(v)
    except (TypeError, ValueError):
        raise SnapshotError("%s: 数值字段无法解析 %r" % (where, v))


def _n_int(v, where):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise SnapshotError("%s: 整数字段收到 bool" % where)
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise SnapshotError("%s: 整数字段无法解析 %r" % (where, v))
    if f != int(f):
        raise SnapshotError("%s: 整数字段收到非整数 %r" % (where, v))
    return int(f)


_NORM = {"code": _n_code, "codes": _n_codes, "text": _n_text, "time": _n_time,
         "num": _n_num, "int": _n_int}


def _normalize(payload):
    """校验 + 归一化整份快照。键集必须恰为 5 段；任何不符抛错（写盘前）。"""
    if not isinstance(payload, dict):
        raise SnapshotError("快照 payload 顶层必须是 dict，收到 %r" % type(payload).__name__)
    if set(payload.keys()) != set(_KEYS):
        raise SnapshotError("快照必须恰含 5 段 %s，实际 %s"
                            % (" / ".join(_KEYS), sorted(payload.keys())))
    out = {}
    for fname, spec in CONTRACT.items():
        raw = payload[spec["key"]]
        fmt = spec["fmt"]
        if fmt == "json":                                   # env_metrics：单对象，键写死
            if not isinstance(raw, dict):
                raise SnapshotError("env_metrics 必须是 dict，收到 %r" % type(raw).__name__)
            if set(raw.keys()) != set(c for c, _ in spec["cols"]):
                raise SnapshotError("env_metrics 键集不符契约：期望 %s，实际 %s"
                                    % ([c for c, _ in spec["cols"]], sorted(raw.keys())))
            out[spec["key"]] = {c: _NORM[k](raw.get(c), "env_metrics.%s" % c) for c, k in spec["cols"]}
            continue
        if not isinstance(raw, (list, tuple)):
            raise SnapshotError("%s 必须是记录列表，收到 %r" % (fname, type(raw).__name__))
        recs = []
        for i, rec in enumerate(raw):
            if not isinstance(rec, dict):
                raise SnapshotError("%s 第 %d 行必须是 dict，收到 %r" % (fname, i, type(rec).__name__))
            cols = [c for c, _ in spec["cols"]]
            if set(rec.keys()) != set(cols):
                raise SnapshotError("%s 第 %d 行字段集不符契约：期望 %s，实际 %s"
                                    % (fname, i, cols, sorted(rec.keys())))
            recs.append({c: _NORM[k](rec.get(c), "%s[%d].%s" % (fname, i, c)) for c, k in spec["cols"]})
        out[spec["key"]] = recs
    return out


# ── 落盘 / 读取（唯一真源，别处禁内联） ───────────────────────────────────────
def _fmt_cell(v, kind):
    if v is None:
        return ""
    if kind == "num":
        return repr(float(v))
    if kind == "int":
        return str(int(v))
    return str(v)


def _cell(v, kind, where):
    if v is None or v == "":
        return None
    return _NORM[kind](v, where)


def _dump(path, spec, data):
    fmt = spec["fmt"]
    cols = [c for c, _ in spec["cols"]]
    if fmt == "csv":
        with io.open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(cols)
            for rec in data:
                w.writerow([_fmt_cell(rec.get(c), k) for c, k in spec["cols"]])
    elif fmt == "jsonl":
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            for rec in data:
                f.write(json.dumps({c: rec.get(c) for c in cols}, ensure_ascii=False) + "\n")
    else:
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({c: data.get(c) for c in cols}, ensure_ascii=False, indent=2) + "\n")


def _load_file(path, spec):
    """读单个契约文件 → list[dict] / dict。列结构不符一律抛错（显式失败优于静默错值）。"""
    fname = os.path.basename(path)
    fmt = spec["fmt"]
    cols = [c for c, _ in spec["cols"]]
    if not os.path.exists(path):
        raise SnapshotError("契约文件缺失：%s" % path)
    if fmt == "csv":
        with io.open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        if not rows:
            raise SnapshotError("%s 是空文件（至少要有表头）" % fname)
        if rows[0] != cols:
            raise SnapshotError("%s 表头不符契约：期望 %s，实际 %s" % (fname, cols, rows[0]))
        out = []
        for i, r in enumerate(rows[1:], 2):
            if len(r) != len(cols):
                raise SnapshotError("%s 第 %d 行列数不符（期望 %d，实际 %d）" % (fname, i, len(cols), len(r)))
            out.append({c: _cell(r[j], k, "%s[L%d].%s" % (fname, i, c)) for j, (c, k) in enumerate(spec["cols"])})
        return out
    if fmt == "jsonl":
        out = []
        with io.open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError as e:
                    raise SnapshotError("%s 第 %d 行不是合法 JSON：%s" % (fname, i, e))
                if not isinstance(rec, dict) or set(rec.keys()) != set(cols):
                    raise SnapshotError("%s 第 %d 行键集不符契约：期望 %s，实际 %s"
                                        % (fname, i, cols, sorted(rec.keys()) if isinstance(rec, dict) else type(rec).__name__))
                out.append({c: _NORM[k](rec.get(c), "%s[L%d].%s" % (fname, i, c)) for c, k in spec["cols"]})
        return out
    with io.open(path, encoding="utf-8") as f:
        try:
            obj = json.load(f)
        except ValueError as e:
            raise SnapshotError("%s 不是合法 JSON：%s" % (fname, e))
    if not isinstance(obj, dict) or set(obj.keys()) != set(cols):
        raise SnapshotError("%s 键集不符契约：期望 %s，实际 %s"
                            % (fname, cols, sorted(obj.keys()) if isinstance(obj, dict) else type(obj).__name__))
    return {c: _NORM[k](obj.get(c), "%s.%s" % (fname, c)) for c, k in spec["cols"]}


def _list_dates(root):
    """列出快照目录下合法日期目录（忽略 .tmp-* / .old-* 中间态），升序。"""
    if not os.path.isdir(root):
        return []
    return sorted(n for n in os.listdir(root)
                  if _DATE_RE.match(n) and os.path.isdir(os.path.join(root, n)))


def available_dates(start=None, end=None, root=None):
    """快照可用日期（区间闭区间，None = 不限）。回测 / 步进按日遍历用。"""
    root = root or SNAPSHOT_ROOT
    ds = _list_dates(root)
    if start:
        ds = [d for d in ds if d >= _check_date(start)]
    if end:
        ds = [d for d in ds if d <= _check_date(end)]
    return ds


# ── A9 准入判决：计算 / 落盘 / 复核（三层阻断的机械内核） ──────────────────────
def _admission_module():
    """懒加载 `admission`（它 import 本模块 ⇒ 模块级导入必成环，只能调用期取）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import admission as ADM
    return ADM


def _admit_payload(norm, provenance=None, date=None):
    """对归一化后的 5 契约负载逐文件跑四类判定 → 判决报告（**纯函数，不碰盘**）。

    `provenance`（可选）按契约文件名或其 key 给出溯源：`{as_of_label, evidence_level,
    universe, claim}`。**未提供 ⇒ 该类判定为「未检查」，如实记为阻塞，但不当成"新缺陷"**
    （见模块头 A9 说明：把"未声明"当拒写理由会让任何无溯源负载都写不进）。

    返回结构对同一 (norm, provenance, date) **逐字节确定**（json.dumps sort_keys 后可比）。
    """
    ADM = _admission_module()
    prov = provenance or {}
    if not isinstance(prov, dict):
        raise SnapshotError("provenance 必须是 dict（按契约文件名/key 给溯源），收到 %r"
                            % type(provenance).__name__)
    files, rejected, blocked, unchecked = {}, [], [], {}
    for fname, spec in CONTRACT.items():
        cols = [c for c, _ in spec["cols"]]
        data = norm[spec["key"]]
        rows = data if isinstance(data, list) else [data]      # json 单对象 ⇒ 视作单行
        pf = prov.get(fname) or prov.get(spec["key"]) or {}
        if not isinstance(pf, dict):
            raise SnapshotError("provenance[%s] 必须是 dict，收到 %r"
                                % (fname, type(pf).__name__))
        as_of_label = pf.get("as_of_label")
        universe = pf.get("universe")
        res = ADM.admit(fname, header=cols, rows=rows,
                        as_of_label=as_of_label,
                        evidence_level=pf.get("evidence_level"),
                        universe=universe, claim=pf.get("claim"))
        # 「已提供输入」的类别才有资格拒写；未提供 ⇒ 未检查（不得视为通过，也不得当作新缺陷）
        provided = {"STRUCTURE": True, "SEMANTIC": True,
                    "AS_OF": as_of_label is not None, "COVERAGE": universe is not None}
        dec = ADM.DECLARED_BLOCKS.get(fname) or {}
        dec_classes = set(dec.get("classes") or ())     # ★ 粘性阻塞：不因"看起来过了"解除
        blocked_classes = list(res["blocked_classes"])
        new_defects = [c for c in blocked_classes
                       if c in res["failed"] and c not in dec_classes and provided[c]]
        # 层① 只拒「写入方自证矛盾」：形状不符 / **已声明**的 AS_OF·COVERAGE 判定失败。
        # 数据层空值（SEMANTIC）**不拒写**（见模块头：全有全无的 5 文件写入口下，因单文件
        # 数据缺陷拒写会连带毁掉另 4 个完好文件；且空值可能是消费方已建模的合法状态，
        # 实例 = scan 路4 把 news 空 related_codes 记为 `unmapped 不猜`）。
        rejecting = [c for c in new_defects if c in _SHAPE_CHECKS or c in _DECLARATION_CHECKS]
        data_only = [c for c in new_defects if c in _DATA_CHECKS]
        # 其余阻塞 = 声明性粘性阻塞（本轮可能未失败，如 env_metrics）∪ 「未提供 ⇒ 未检查」
        other = [c for c in blocked_classes if c not in new_defects]
        files[fname] = {
            "admissible": res["admissible"],
            "failed": list(res["failed"]),
            "blocked_classes": blocked_classes,
            "new_defects": new_defects,
            "rejecting_defects": rejecting,
            "data_defects_recorded_only": data_only,
            "not_checked": [c for c in _ADMISSION_CHECKS if not provided[c]],
            "checks": {c: {"status": res["checks"][c]["status"],
                           "basis": res["checks"][c]["basis"],
                           "problems": list(res["checks"][c]["problems"])}
                       for c in _ADMISSION_CHECKS},
            "declared_block": ({"classes": list(res["declared_block"]["classes"]),
                                "reason": res["declared_block"]["reason"],
                                "decided_by": res["declared_block"]["decided_by"]}
                               if res["declared_block"] else None),
        }
        if rejecting:
            rejected.append(fname)
        if other or data_only:
            blocked.append(fname)
        if not (provided["AS_OF"] and provided["COVERAGE"]):
            unchecked[fname] = [c for c in _ADMISSION_CHECKS if not provided[c]]
    return {
        "schema": ADMISSION_SCHEMA, "date": date,
        "admitted": not rejected and not blocked,
        "rejected": rejected, "blocked_files": blocked, "unchecked": unchecked,
        "declared_blocks_are_sticky": True,
        "reject_rule": "层① 只拒「形状不符」与「**已声明**的 AS_OF/COVERAGE 判定失败」"
                       "（写入方自证矛盾）；SEMANTIC 数据层空值如实入判但不拒写",
        "provenance": json.loads(json.dumps(prov, ensure_ascii=False, sort_keys=True)),
        "files": files,
    }


def _dump_admission(path, report):
    """落盘判决 sidecar（sort_keys 保证同判决 ⇒ 逐字节同）。"""
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _verify_dir(path, date, expected, provenance, strict_files=True):
    """层②：把**已落盘目录**当唯一输入重读 + 复算，断言判决可由落盘内容复现。

    这才是"写入后自检"的实质：证明 sidecar 没有与它所判的快照脱钩（不是重跑一遍同一内存对象）。
    返回问题清单（[] = 过）。**只读，不改盘。**
    `strict_files=False`：调用方已自行核对文件集（`check_snapshot`），跳过该项以免重复报错。
    """
    probs = []
    if not os.path.isdir(path):
        return ["目录不存在：%s" % path]
    names = sorted(n for n in os.listdir(path) if os.path.isfile(os.path.join(path, n)))
    want = sorted(list(_FNAMES) + [ADMISSION_FNAME])
    if strict_files and names != want:
        probs.append("目录文件集不符：期望 %s，实际 %s" % (want, names))
    loaded = {}
    for fname, spec in CONTRACT.items():
        fp = os.path.join(path, fname)
        if os.path.exists(fp):
            try:
                loaded[spec["key"]] = _load_file(fp, spec)
            except SnapshotError as e:
                probs.append(str(e))
        else:
            probs.append("契约文件缺失：%s" % fname)
    if len(loaded) == len(CONTRACT):
        if _admit_payload(loaded, provenance, date=date) != expected:
            probs.append("判决不可复现：由落盘内容复算的判决 ≠ 落盘判决（sidecar 与快照脱钩）")
    sp = os.path.join(path, ADMISSION_FNAME)
    if not os.path.exists(sp):
        probs.append("准入判决 sidecar 缺失：%s（该快照未经层①准入闸）" % ADMISSION_FNAME)
    else:
        try:
            on_disk = json.loads(io.open(sp, encoding="utf-8").read())
        except ValueError as e:
            probs.append("准入判决 sidecar 不是合法 JSON：%s" % e)
        else:
            if on_disk != expected:
                probs.append("准入判决 sidecar 与内存判决不一致（落盘被改动？）")
    return probs


# ── 公开 API ─────────────────────────────────────────────────────────────────
def write_snapshot(date, payload, root=None, force=False, provenance=None):
    """把一份已构造好的快照写进 data/snapshot/<date>/（唯一写入口）。

    返回 "WRITTEN" = 本次真落盘；"EXISTS" = 该日已有快照且未 force（幂等命中，settle 重跑安全）。
    force=True 覆盖重写，仅供数据修正后重跑（旧目录先改名再换入，中途失败自动回滚）。
    校验全部发生在写盘之前：任何不符契约 → 抛错且目录不留痕。

    ★ A9 三层阻断（见模块头）：写盘前跑准入判定，**只在「写入方自证矛盾」时**抛
    `AdmissionRejected`（不落盘）—— 判据 = 形状不符 / **已声明**的 AS_OF·COVERAGE 判定失败；
    数据层空值（SEMANTIC）与已声明粘性阻塞**不拒写**，但**一律如实落盘**为阻塞。
    落盘后层②复核「判决 == 由落盘内容复算的判决」。sidecar `_admission.json` 与 5 契约文件
    **同批原子换入**。`provenance` 缺省 ⇒ AS_OF/COVERAGE 记「未检查」（未判定 ≠ 通过）。
    幂等命中（未写盘）**不判定** —— 判决只约束"写"这个动作。
    """
    d = _check_date(date)
    root = root or SNAPSHOT_ROOT
    norm = _normalize(payload)                        # ← 先校验，后碰盘
    final = os.path.join(root, d)
    if os.path.isdir(final) and not force:
        return "EXISTS"
    _WG.check_write(final)
    # 层① 写入前：四类判定；写入方自证矛盾 ⇒ 拒写（目录不留痕）
    rep = _admit_payload(norm, provenance, date=d)
    if rep["rejected"]:
        raise AdmissionRejected(
            "层① 准入闸拒写 %s：%s 出现**写入方自证矛盾**（形状不符 / 已声明的 "
            "AS_OF·COVERAGE 判定失败）⇒ 快照不得落盘；数据层空值不构成拒写理由"
            % (d, " / ".join(rep["rejected"])))
    tmp = os.path.join(root, ".tmp-%s-%d" % (d, os.getpid()))
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    try:
        for fname, spec in CONTRACT.items():
            _dump(os.path.join(tmp, fname), spec, norm[spec["key"]])
        _dump_admission(os.path.join(tmp, ADMISSION_FNAME), rep)
        probs = _verify_dir(tmp, d, rep, provenance)   # 层② 发布前：tmp 已落盘，重读复算
        if probs:
            raise SnapshotError("层② 写入后自检未过（tmp）：%s" % " / ".join(probs))
        if os.path.isdir(final):                      # force：旧目录先让位，失败可回滚
            old = os.path.join(root, ".old-%s-%d" % (d, os.getpid()))
            if os.path.isdir(old):
                shutil.rmtree(old)
            os.replace(final, old)
            try:
                os.replace(tmp, final)
            except Exception:
                os.replace(old, final)                # 回滚，绝不留「有 tmp 没正式」的空窗
                raise
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.replace(tmp, final)                    # 同一卷内目录改名 = 原子换入
        probs2 = _verify_dir(final, d, rep, provenance)   # 层② 发布后：正式位再校一次
        if probs2:
            raise SnapshotError("层② 写入后自检未过（正式位）：%s" % " / ".join(probs2))
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return "WRITTEN"


def ingest_snapshot(date, raw_dir=None, root=None, force=False, provenance=None):
    """盘后写当日快照 = 读采集投放区 → 归一化 → 契约校验 → 原子落盘。

    投放区（默认 data/raw/<date>/）须含 5 个契约文件；缺文件 → SnapshotUnavailable（数据缺口，禁编造）。
    接真实采集器只须让它往投放区写文件，本模块零改动（插入式扩展）。
    返回 {"date","status","source","path","files"}；status 同 write_snapshot。

    ★ A9 溯源（`provenance`）：显式参数优先；未给则读投放区可选件 `_provenance.json`
    （**非契约文件**，缺省即"未声明"）。内容按契约文件名/key 给
    `{as_of_label, evidence_level, universe, claim}` —— 供 AS_OF / COVERAGE 两类判定。
    不给 ⇒ 这两类记「未检查」（如实阻塞，不伪装通过），AS_OF/COVERAGE 型缺陷不拒写。
    """
    d = _check_date(date)
    raw = raw_dir or os.path.join(RAW_ROOT, d)
    if not os.path.isdir(raw):
        raise SnapshotUnavailable(
            "采集投放区不存在：%s —— 采集器须先投放 5 个契约文件（本模块禁直连数据源、禁用 LLM 补数）" % raw)
    missing = [f for f in _FNAMES if not os.path.exists(os.path.join(raw, f))]
    if missing:
        raise SnapshotUnavailable("投放区缺 %d 个契约文件：%s（目录：%s）"
                                  % (len(missing), " / ".join(missing), raw))
    if provenance is None:
        pv = os.path.join(raw, "_provenance.json")
        if os.path.exists(pv):
            try:
                provenance = json.loads(io.open(pv, encoding="utf-8").read())
            except ValueError as e:
                raise SnapshotError("投放区溯源件 %s 不是合法 JSON：%s" % (pv, e))
    payload = {spec["key"]: _load_file(os.path.join(raw, f), spec) for f, spec in CONTRACT.items()}
    status = write_snapshot(d, payload, root=root, force=force, provenance=provenance)
    return {"date": d, "status": status, "source": raw,
            "path": os.path.join(root or SNAPSHOT_ROOT, d), "files": list(_FNAMES)}


def load_snapshot(date, root=None):
    """精确读某一日快照 → {"daily_quote":[...],"limit_up":[...],"dragon_tiger":[...],
    "news":[...],"env_metrics":{...},"date":<该日>}；该日无快照 → UNAVAILABLE。

    结构不符契约 → 抛 SnapshotError（不返回半份数据）。
    """
    d = _check_date(date)
    root = root or SNAPSHOT_ROOT
    p = os.path.join(root, d)
    if not os.path.isdir(p):
        return UNAVAILABLE
    snap = {spec["key"]: _load_file(os.path.join(p, f), spec) for f, spec in CONTRACT.items()}
    snap["date"] = d
    return snap


def load_asof(date, root=None):
    """★ 锁① 信息截断的唯一入口：只返回「≤ date 的最新一日」快照。

    date 之后（含任何未来日）的数据物理上取不到——只遍历 ≤date 的日期目录，不做任何"最近可用日"猜测；
    落在非交易日 / 无快照日 → 自动回退到最近一个 ≤date 的快照（is_exact=False），符合盘前腿
    `load_asof(昨日)` 用法；date 之前完全无快照 → UNAVAILABLE。
    """
    d = _check_date(date)
    root = root or SNAPSHOT_ROOT
    cands = [x for x in _list_dates(root) if x <= d]
    if not cands:
        return UNAVAILABLE
    latest = max(cands)
    snap = load_snapshot(latest, root)
    if snap is UNAVAILABLE:                            # 目录存在但读失败 → 显式抛错，不静默回退更早
        raise SnapshotError("快照目录存在但读取失败：%s" % os.path.join(root, latest))
    if snap["date"] > d:                               # 物理截断硬断言（截断是锁①，不是约定）
        raise SnapshotError("信息截断被破坏：请求 %s，却取到 %s" % (d, snap["date"]))
    snap["is_exact"] = (latest == d)
    return snap


def check_snapshot(date, root=None):
    """只读校验某日快照是否符契约 **+ 准入判决是否在场且可复现**，返回问题清单（[] = 合规）。

    A9 起兼作层②的对外形式：除 5 契约文件外，还要求 sidecar `_admission.json` 在场，
    并断言**其判决可由本目录内容复现** —— 判决与快照不得脱钩。供审计 / 自检 / settle 步1 用。
    """
    d = _check_date(date)
    root = root or SNAPSHOT_ROOT
    p = os.path.join(root, d)
    probs = []
    if not os.path.isdir(p):
        return ["快照目录不存在：%s" % p]
    names = sorted(n for n in os.listdir(p) if os.path.isfile(os.path.join(p, n)))
    extra = [n for n in names if n not in _FNAMES and n != ADMISSION_FNAME]
    missing = [f for f in _FNAMES if f not in names]
    if extra:
        probs.append("存在契约外文件：%s" % " / ".join(extra))
    if missing:
        probs.append("缺契约文件：%s" % " / ".join(missing))
    for f, spec in CONTRACT.items():
        fp = os.path.join(p, f)
        if os.path.exists(fp):
            try:
                _load_file(fp, spec)
            except SnapshotError as e:
                probs.append(str(e))
    rep = None
    if ADMISSION_FNAME not in names:
        probs.append("缺准入判决 sidecar：%s —— 该快照未经层①准入闸（未判定 ≠ 通过）"
                     % ADMISSION_FNAME)
    else:
        try:
            rep = json.loads(io.open(os.path.join(p, ADMISSION_FNAME), encoding="utf-8").read())
        except ValueError as e:
            probs.append("准入判决 sidecar 不是合法 JSON：%s" % e)
    if rep is not None and not missing:
        probs += _verify_dir(p, d, rep, rep.get("provenance") or {}, strict_files=False)
    return probs


# ── 层③ 消费前闸（A9） ───────────────────────────────────────────────────────
def admission_of(date, root=None):
    """读某日快照的准入判决（层③ 查询入口）。**缺判决 ⇒ 如实报缺，禁假定通过。**

    返回 sidecar 原文（`admission@v1`）。sidecar 不在场 ⇒ 返回 `{missing: True, admitted: False}`
    —— 缺判决与"判决为阻塞"在效果上等价，但**原因必须可区分**（禁把"没判"说成"判过了"）。
    """
    d = _check_date(date)
    root = root or SNAPSHOT_ROOT
    p = os.path.join(root, d, ADMISSION_FNAME)
    if not os.path.exists(p):
        return {"schema": ADMISSION_SCHEMA, "date": d, "admitted": False, "missing": True,
                "reason": "该日无准入判决 sidecar（%s）—— 未判定 ≠ 通过，一律按阻塞处理" % p}
    try:
        obj = json.loads(io.open(p, encoding="utf-8").read())
    except ValueError as e:
        raise SnapshotError("准入判决 sidecar 不是合法 JSON：%s" % e)
    if not isinstance(obj, dict) or obj.get("schema") != ADMISSION_SCHEMA:
        raise SnapshotError("准入判决 sidecar schema 不符（期望 %s）：%s"
                            % (ADMISSION_SCHEMA, p))
    return obj


def assert_admissible(date, root=None, need=None):
    """层③ 消费前断言：`need` 类别被阻塞 ⇒ 抛 `AdmissionRejected`（并返回判决备查）。

    `need=None` ⇒ 要求**完全准入**（无任何阻塞类别）；给元组（如 `("SEMANTIC",)`）⇒
    只要求这些类别不阻塞。**调用方须按自己的真实需求声明**，禁放宽到未声明的类别：
      · 只当纯行情数值消费（不碰涨停时点语义）⇒ `need=("STRUCTURE",)`
      · 要按涨停清单产候选（B1 §5.2.7 / R-7）⇒ `need=("STRUCTURE","SEMANTIC","AS_OF","COVERAGE")`
    判定取自 sidecar，是**写入时**的四类判决；本函数只做断言，**不在消费期重判**。
    """
    rep = admission_of(date, root=root)
    if rep.get("missing"):
        raise AdmissionRejected("层③ %s：%s" % (date, rep.get("reason")))
    blocked = set()
    for _f, v in (rep.get("files") or {}).items():
        blocked |= set(v.get("blocked_classes") or ())
    want = set(_ADMISSION_CHECKS if need is None else need)
    bad = sorted(blocked & want)
    if bad:
        raise AdmissionRejected(
            "层③ %s 准入未过：阻塞类别 %s（本次需要 %s）—— 判决见 %s/%s"
            % (date, bad, sorted(want), date, ADMISSION_FNAME))
    return rep


# ── 自检（--selftest）：全部在临时目录跑，不碰真实数据 ─────────────────────────
def _selftest():
    import tempfile
    ok = fail = 0
    fails = []
    # ★ 零污染动态基准（2026-10-20）：真库 snapshot 已有正式 promote 的数据
    #   （10月八天）⇒「目录不存在」式硬编码断言失效；改「自检前后真库
    #   该目录状态不变」——保持零污染强度，不断言生产区为空。
    def _snap_state(p):
        if not os.path.exists(p):
            return None
        return (os.path.getmtime(p), len(os.listdir(p)))
    _F5_BEFORE = _snap_state(os.path.join(SNAPSHOT_ROOT, "2026-10-14"))

    def T(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [OK] %s" % name)
        else:
            fail += 1
            fails.append(name)
            print("  [!!] %s" % name)

    def PAY():
        return {
            "daily_quote": [{"code": "sh600000", "name": "浦发银行", "open": 10.0, "high": 10.5,
                             "low": 9.9, "close": 10.4, "pct": 3.18, "vol": 12345600, "amount": 127000000.0}],
            "limit_up": [{"code": "sz000001", "name": "平安银行", "boards": 2, "first_seal_time": "09:35:12",
                          "seal_amount": 82000000.0, "reason": "银行"}],
            "dragon_tiger": [{"code": "sz000001", "name": "平安银行", "seat": "机构专用",
                              "buy": 50000000.0, "sell": 10000000.0, "net": 40000000.0}],
            "news": [{"news_id": "N20261008-001", "ts": "2026-10-08 09:12:00", "source": "证券时报",
                      "title": "标题", "body": "正文", "related_codes": ["sz000001"]}],
            "env_metrics": {"limit_up_count": 52, "max_board": 6, "turnover": 1.1e12,
                            "up_down_ratio": 1.35, "fail_rate": 0.18, "index_ma20": 3210.5},
        }

    tmp = tempfile.mkdtemp(prefix="ta_snapshot_selftest_")
    root = os.path.join(tmp, "snapshot")
    raw = os.path.join(tmp, "raw")
    # D1/D2/D4 = 有快照日；D3 = 无快照日（测精确读返回 UNAVAILABLE）；10-10/10-11 = 周末空档
    D1, D2, D4, D3 = "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"
    try:
        print("== A. 写入与契约 ==")
        T("A1 write 返回 WRITTEN", write_snapshot(D1, PAY(), root=root) == "WRITTEN")
        dir1 = os.path.join(root, D1)
        T("A2 目录名 = YYYY-MM-DD", os.path.isdir(dir1))
        names = sorted(os.listdir(dir1))
        T("A3 目录内恰 5 契约文件 + 1 判决 sidecar（无多余）",
          names == sorted(list(_FNAMES) + [ADMISSION_FNAME]))
        for f in ("daily_quote.csv", "limit_up.csv", "dragon_tiger.csv"):
            lines = io.open(os.path.join(dir1, f), encoding="utf-8").read().split("\n")
            want = ",".join(c for c, _ in CONTRACT[f]["cols"])
            T("A4 %s 表头符契约" % f, lines[0] == want)
        news = [json.loads(x) for x in io.open(os.path.join(dir1, "news.jsonl"), encoding="utf-8") if x.strip()]
        T("A5 news.jsonl 键集符契约",
          len(news) == 1 and set(news[0]) == set(c for c, _ in CONTRACT["news.jsonl"]["cols"]))
        env = json.load(io.open(os.path.join(dir1, "env_metrics.json"), encoding="utf-8"))
        T("A6 env_metrics 键集符契约（无中英两套键）",
          set(env) == set(c for c, _ in CONTRACT["env_metrics.json"]["cols"]))
        T("A7 check_snapshot 无问题", check_snapshot(D1, root=root) == [])
        empty = PAY()
        empty["limit_up"] = []
        T("A8 空段也落文件（write 正常）", write_snapshot("2026-10-07", empty, root=root) == "WRITTEN")
        T("A8b 空段文件仅表头",
          io.open(os.path.join(root, "2026-10-07", "limit_up.csv"), encoding="utf-8").read().count("\n") == 1)

        print("== B. round-trip ==")
        s1 = load_snapshot(D1, root=root)
        T("B1 code/close 回读一致", s1["daily_quote"][0]["code"] == "sh600000"
          and s1["daily_quote"][0]["close"] == 10.4)
        T("B2 env 回读一致", s1["env_metrics"]["max_board"] == 6 and s1["env_metrics"]["turnover"] == 1.1e12)
        T("B3 related_codes 保持列表", s1["news"][0]["related_codes"] == ["sz000001"])
        T("B4 load_snapshot 带 date", s1["date"] == D1)

        print("== C. ★锁① 信息截断 ==")
        T("C1 load_asof(D1) 拿 D1 且 is_exact", load_asof(D1, root=root)["date"] == D1
          and load_asof(D1, root=root)["is_exact"] is True)
        write_snapshot(D2, PAY(), root=root)
        a = load_asof(D1, root=root)
        T("C2 ★写了 D2 后 load_asof(D1) 仍只拿 D1（物理截断）", a["date"] == D1)
        T("C3 load_asof(D2) 拿 D2", load_asof(D2, root=root)["date"] == D2)
        T("C4 无快照日（周日 10-11）→ 回退最近 ≤它 的 D2 且 is_exact=False",
          load_asof("2026-10-11", root=root)["date"] == D2
          and load_asof("2026-10-11", root=root)["is_exact"] is False)
        write_snapshot(D4, PAY(), root=root)
        T("C4b ★回退不跳未来：请求 10-11 仍拿 D2，拿不到 D4=10-12", load_asof("2026-10-11", root=root)["date"] == D2)
        T("C5 请求远未来日 → 拿最新 ≤它（=D4）", load_asof("2027-01-01", root=root)["date"] == D4)
        T("C6 早于全部快照 → UNAVAILABLE", load_asof("2020-01-01", root=root) is UNAVAILABLE)
        T("C7 load_snapshot(无快照日) → UNAVAILABLE", load_snapshot(D3, root=root) is UNAVAILABLE)
        T("C8 UNAVAILABLE 布尔为假、可 is 比较", not UNAVAILABLE and UNAVAILABLE is UNAVAILABLE)
        T("C9 available_dates 闭区间过滤（start 之前的日被排除）",
          available_dates(D1, D2, root=root) == [D1, D2])

        print("== D. 幂等 / 覆盖 / 拒写 ==")
        T("D1 重复写同日 → EXISTS（settle 重跑安全）", write_snapshot(D1, PAY(), root=root) == "EXISTS")
        mod = PAY()
        mod["env_metrics"]["max_board"] = 9
        write_snapshot(D1, mod, root=root)
        T("D2 EXISTS 不改内容", load_snapshot(D1, root=root)["env_metrics"]["max_board"] == 6)
        T("D3 force 重写生效", write_snapshot(D1, mod, root=root, force=True) == "WRITTEN"
          and load_snapshot(D1, root=root)["env_metrics"]["max_board"] == 9)
        T("D4 force 后无 .tmp/.old 残留",
          not [n for n in os.listdir(root) if n.startswith(".tmp") or n.startswith(".old")])

        print("== E. 拒写入（写盘前拦下） ==")
        bad = PAY()
        del bad["daily_quote"][0]["close"]
        try:
            write_snapshot("2026-10-13", bad, root=root)
            T("E1 缺列 → 抛错", False)
        except SnapshotError:
            T("E1 缺列 → 抛错", True)
        T("E2 拒写后目录未创建", not os.path.isdir(os.path.join(root, "2026-10-13")))
        bad2 = PAY()
        bad2["daily_quote"][0]["code"] = 600000
        try:
            write_snapshot("2026-10-13", bad2, root=root)
            T("E3 code 传 int → 抛错（防丢前导零）", False)
        except SnapshotError:
            T("E3 code 传 int → 抛错（防丢前导零）", True)
        bad3 = PAY()
        bad3["env_metrics"]["涨停数"] = 52
        try:
            write_snapshot("2026-10-13", bad3, root=root)
            T("E4 env 键中英混用 → 抛错", False)
        except SnapshotError:
            T("E4 env 键中英混用 → 抛错", True)
        bad4 = PAY()
        bad4.pop("news")
        try:
            write_snapshot("2026-10-13", bad4, root=root)
            T("E5 缺段 → 抛错", False)
        except SnapshotError:
            T("E5 缺段 → 抛错", True)

        print("== F. ingest（投放区取数） ==")
        try:
            ingest_snapshot("2026-10-14", raw_dir=os.path.join(tmp, "nope"), root=root)
            T("F1 无投放区 → SnapshotUnavailable（不造假）", False)
        except SnapshotUnavailable:
            T("F1 无投放区 → SnapshotUnavailable（不造假）", True)
        drop = os.path.join(raw, "2026-10-14")
        os.makedirs(drop)
        src = PAY()
        write_snapshot("2026-10-15", src, root=os.path.join(tmp, "seed"))
        for f in _FNAMES:
            shutil.copy(os.path.join(tmp, "seed", "2026-10-15", f), os.path.join(drop, f))
        os.remove(os.path.join(drop, "news.jsonl"))
        try:
            ingest_snapshot("2026-10-14", raw_dir=drop, root=root)
            T("F2 投放区缺文件 → SnapshotUnavailable", False)
        except SnapshotUnavailable:
            T("F2 投放区缺文件 → SnapshotUnavailable", True)
        shutil.copy(os.path.join(tmp, "seed", "2026-10-15", "news.jsonl"), os.path.join(drop, "news.jsonl"))
        r = ingest_snapshot("2026-10-14", raw_dir=drop, root=root)
        T("F3 投放区齐全 → WRITTEN 且 5 契约文件 + sidecar", r["status"] == "WRITTEN"
          and sorted(os.listdir(os.path.join(root, "2026-10-14")))
          == sorted(list(_FNAMES) + [ADMISSION_FNAME]))
        T("F4 ingest 后可 load_asof", load_asof("2026-10-14", root=root)["date"] == "2026-10-14")
        T("F5 真数据目录未被自检触碰（前后状态不变——动态基准，非硬编码空目录）",
          _snap_state(os.path.join(SNAPSHOT_ROOT, "2026-10-14")) == _F5_BEFORE)

        print("== G. 契约外干扰 ==")
        os.makedirs(os.path.join(root, "not-a-date"))
        T("G1 非日期目录被忽略", available_dates(root=root).count("not-a-date") == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ── K 组：A9 准入闸三层阻断 ──
    print("== K. A9 准入闸（层① 写前 / 层② 写后 / 层③ 消费前） ==")
    kt = tempfile.mkdtemp(prefix="ta_snapshot_a9_")
    kroot = os.path.join(kt, "snapshot")
    K1, K2, K3, K4 = ("2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13")
    _K20_BEFORE = _snap_state(os.path.join(SNAPSHOT_ROOT, K4))
    try:
        # 层① 落盘判决 + 层② 可复现
        write_snapshot(K1, PAY(), root=kroot)
        rep1 = admission_of(K1, root=kroot)
        T("K1 层① 落盘 sidecar 且 schema = admission@v1（未退化为假定通过）",
          rep1.get("schema") == ADMISSION_SCHEMA and rep1.get("missing") is not True)
        T("K2 层② check_snapshot 复核通过（判决 == 由落盘内容复算的判决）",
          check_snapshot(K1, root=kroot) == [])
        T("K3 层② 是**真复算**而非重放：5 文件 + sidecar 恰齐",
          sorted(os.listdir(os.path.join(kroot, K1)))
          == sorted(list(_FNAMES) + [ADMISSION_FNAME]))

        # 层② 负例：篡改 sidecar 伪造成"完全准入" ⇒ 必须被检出（证明复算有效）
        sp1 = os.path.join(kroot, K1, ADMISSION_FNAME)
        orig = io.open(sp1, encoding="utf-8").read()
        fake = json.loads(orig)
        fake["admitted"] = True
        io.open(sp1, "w", encoding="utf-8").write(
            json.dumps(fake, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        probs_t = check_snapshot(K1, root=kroot)
        io.open(sp1, "w", encoding="utf-8").write(orig)          # 复原
        T("K4 层② 篡改 sidecar（伪造 admitted=True）被检出（判决与快照脱钩）",
          any(("脱钩" in p) or ("不一致" in p) for p in probs_t))
        T("K5 层② 复原后复归合规（证明 K4 检出的是篡改而非偶发）",
          check_snapshot(K1, root=kroot) == [])

        # 层① 不拒写「已声明阻塞」与「未提供溯源」，但必须如实记阻塞
        T("K6 未提供溯源 ⇒ 5 文件的 AS_OF/COVERAGE 全记「未检查」（未判定 ≠ 通过）",
          set(rep1.get("unchecked") or {}) == set(_FNAMES)
          and all(v == ["AS_OF", "COVERAGE"] for v in rep1["unchecked"].values()))
        T("K7 已声明阻塞如实入判（limit_up / dragon_tiger / env_metrics 均 blocked）",
          {"limit_up.csv", "dragon_tiger.csv", "env_metrics.json"}
          <= set(rep1.get("blocked_files") or ()))
        T("K8 blocked_classes = 失败的 ∪ 已声明（env_metrics: 未检查的 AS_OF 并入声明的 "
          "COVERAGE ⇒ 并集两类别，非交集）",
          rep1["files"]["env_metrics.json"]["blocked_classes"] == ["AS_OF", "COVERAGE"])
        T("K9 层① 未把「未检查」当新缺陷 ⇒ 不拒写（否则 3/5 恒阻塞会让盘后链死）",
          rep1.get("rejected") == [] and rep1.get("admitted") is False)

        # 溯源模板（给齐 ⇒ AS_OF/COVERAGE 由未判转为真判）
        prov = {f: {"as_of_label": "close_final", "evidence_level": "E3",
                    "universe": {"include_st": True, "include_bse": True, "complete": True}}
                for f in _FNAMES}

        # 层① 正例：写入方**自证矛盾** ⇒ 拒写且目录不留痕
        #（自称 close_final 档，却只给出 E1 档名级证据 —— E1 单独不得证明时点）
        prov_weak = {f: dict(v, evidence_level="E1") for f, v in prov.items()}
        try:
            write_snapshot(K2, PAY(), root=kroot, provenance=prov_weak)
            T("K10 层① 拒写「声明与事实矛盾」负载（自称 close_final 却只给 E1 证据）", False)
        except AdmissionRejected:
            T("K10 层① 拒写「声明与事实矛盾」负载（自称 close_final 却只给 E1 证据）",
              not os.path.exists(os.path.join(kroot, K2)))

        # 层① 反例：**数据层空值不构成拒写理由**（否则 5 文件全有全无的写入口会
        # 因单文件空值连带毁掉另 4 个完好文件；且空值可能是消费方已建模的合法状态）
        dat = PAY()
        dat["news"].append({"news_id": "N2", "ts": "2026-10-08 09:00:00", "source": "s",
                            "title": "t", "body": None, "related_codes": []})
        ok_w = write_snapshot(K2, dat, root=kroot, provenance=prov)
        rep2 = admission_of(K2, root=kroot)
        T("K10b ★数据层空值不拒写，但如实记为 SEMANTIC 阻塞（news 空 related_codes）",
          ok_w == "WRITTEN"
          and rep2["files"]["news.jsonl"]["data_defects_recorded_only"] == ["SEMANTIC"]
          and rep2["files"]["news.jsonl"]["admissible"] is False)
        T("K10c ★scan 路4 契约兼容：news 空 related_codes 是**已建模合法状态**"
          "（'空 → unmapped 不猜'），故只能记录不可拒写",
          "news.jsonl" in (rep2.get("blocked_files") or ())
          and rep2.get("rejected") == [])

        # 溯源给齐 ⇒ AS_OF/COVERAGE 真判，阻塞面收缩到声明项
        write_snapshot(K4, PAY(), root=kroot, provenance=prov)
        rep4 = admission_of(K4, root=kroot)
        T("K11 给齐溯源 ⇒ 无「未检查」（AS_OF/COVERAGE 由未判转为真判且 OK）",
          rep4.get("unchecked") == {}
          and all(v["checks"]["AS_OF"]["status"] == "OK"
                  and v["checks"]["COVERAGE"]["status"] == "OK"
                  for v in rep4["files"].values()))
        T("K12 给齐溯源后阻塞面收缩到声明项（daily_quote / news 完全准入）",
          set(rep4.get("blocked_files") or ())
          == {"limit_up.csv", "dragon_tiger.csv", "env_metrics.json"})
        T("K12b ★粘性实证：env_metrics 本轮四类全过（failed=[]）仍 blocked_classes 非空 "
          "—— 数据层'看起来过了'不得推翻设计层已认定的结构性缺陷（COVERAGE 残留覆盖未声明）",
          rep4["files"]["env_metrics.json"]["failed"] == []
          and rep4["files"]["env_metrics.json"]["blocked_classes"] == ["COVERAGE"]
          and rep4["files"]["env_metrics.json"]["admissible"] is False)

        # 层③ 消费前闸：按需声明类别，缺判决一律报缺
        T("K13 层③ need=(STRUCTURE,) 放行（按需声明，不放宽到未声明类别）",
          assert_admissible(K4, root=kroot, need=("STRUCTURE",)) is not None)
        try:
            assert_admissible(K4, root=kroot, need=("SEMANTIC",))
            T("K14 层③ need=(SEMANTIC,) 被粘性阻塞拦下", False)
        except AdmissionRejected:
            T("K14 层③ need=(SEMANTIC,) 被粘性阻塞拦下", True)
        try:
            assert_admissible(K4, root=kroot)               # need=None ⇒ 要求完全准入
            T("K15 层③ need=None（完全准入）对含声明阻塞的快照必拦", False)
        except AdmissionRejected:
            T("K15 层③ need=None（完全准入）对含声明阻塞的快照必拦", True)
        miss = admission_of(K3, root=kroot)
        T("K16 层③ 无 sidecar ⇒ 报缺且 admitted=False（缺判决 ≠ 通过）",
          miss.get("missing") is True and miss["admitted"] is False)
        try:
            assert_admissible(K3, root=kroot)
            T("K17 层③ 无 sidecar ⇒ assert_admissible 抛错", False)
        except AdmissionRejected:
            T("K17 层③ 无 sidecar ⇒ assert_admissible 抛错", True)

        # 幂等命中不判定（判决只约束"写"这个动作）
        T("K18 幂等命中返回 EXISTS（未写盘 ⇒ 不判、不因负载缺陷抛错）",
          write_snapshot(K1, PAY(), root=kroot) == "EXISTS")
        T("K19 判决对同输入逐字节确定（层② 可复现的前提）",
          _admit_payload(_normalize(PAY()), None, date=K1)
          == _admit_payload(_normalize(PAY()), None, date=K1))
        T("K20 真数据目录未被 A9 自检触碰（前后状态不变——动态基准）",
          _snap_state(os.path.join(SNAPSHOT_ROOT, K4)) == _K20_BEFORE)
    finally:
        shutil.rmtree(kt, ignore_errors=True)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：%s" % " / ".join(fails))
    return 1 if fail else 0


def _main(argv):
    if "--selftest" in argv:
        print("snapshot.py 自检（SCHEMA_VERSION=%d · 临时目录运行）" % SCHEMA_VERSION)
        return _selftest()
    print("快照目录 · %s" % SNAPSHOT_ROOT)
    print("  采集投放区 : %s" % RAW_ROOT)
    ds = available_dates()
    if not ds:
        print("  快照数     : 0 —— 尚无任何日快照（0.2 已可运行，待采集器向投放区写文件）")
    else:
        print("  快照数     : %d（%s ~ %s）" % (len(ds), ds[0], ds[-1]))
        for d in ds:
            probs = check_snapshot(d)
            print("    %s  %s" % (d, "契约合规" if not probs else "!! " + " ; ".join(probs)))
    print("  触发者     : tools/risk/settle.py 步 1 先 ingest_snapshot(date)，再判「行情落定」")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
