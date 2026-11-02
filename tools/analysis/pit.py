"""pit.py · Point-in-Time 对齐引擎（基本面回测的防未来函数核心）。

## 为什么必须有这个模块

基本面数据最大的坑不是数据本身，是**对齐方式**。实测证据（600178）：

    EndDate=2025-12-31 的年报，InfoPublDate = 2026-04-14

若按 `EndDate` 在 2026-01 就用它 ⇒ **提前 3.5 个月使用当时不存在的信息**。
这类错误在回测里会造出巨大的假 alpha，且极难察觉（因为数字看起来"合理"）。

## 本模块的硬规则

1. **只允许按 `publ_date` 对齐**：某交易日 T 能用到的财报，必须满足 `publ_date <= T`
   （公告当日即可用——保守起见也可设 `publ_date < T`，本件默认 `<=`，
   并可通过 `lag_days` 加额外滞后以更保守）
2. **绝不用 `end_date` 做可用性判断**，`end_date` 只用于排序「哪份更新」
3. **缺失即缺失**：某票某日没有任何已公告财报 ⇒ 返回 None，不用行业均值/上期值填充
4. **同一 (code, field) 多份财报可选时取 publ_date 最大的那份**（最新已公告）

## 接口

    idx = build_index(rows)          # 建 {code: {field: [(publ_date, value), ...]}}
    v = value_at(idx, code, field, date, lag_days=0)   # 某日可用的最新值
    panel = align(idx, samples, fields)                # 给样本批量附加字段

用法：
  python tools/analysis/pit.py --selftest
"""
import bisect
import csv
import io
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("fs", "analysis"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

FIN_REL = os.path.join("data", "raw", "bus", "finance.csv")


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    # NaN / inf 一律当缺失（不参与计算）
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return x


def load_rows(root=None):
    """读 finance.csv 长表 → list[dict]。

    跳过缺 publ_date 或 end_date 的行（无法做 PIT 对齐，留着只会诱使误用）。
    """
    root = root or _ROOT
    p = os.path.join(root, FIN_REL)
    if not os.path.isfile(p):
        return []
    out = []
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if not r.get("publ_date") or not r.get("end_date"):
                continue
            out.append(r)
    return out


def build_index(rows, fields=None):
    """→ {code: {field: {"dates": [...], "vals": [...]}}}

    `dates` = publ_date 升序（同日同字段只留最后一条，避免重复财报日冲突）
    """
    raw = {}
    for r in rows:
        code = r.get("code")
        field = r.get("field")
        if not code or not field:
            continue
        if fields is not None and field not in fields:
            continue
        v = _f(r.get("value"))
        if v is None:
            continue
        raw.setdefault(code, {}).setdefault(field, {})[r["publ_date"]] = v
    idx = {}
    for code, fmap in raw.items():
        idx[code] = {}
        for field, dmap in fmap.items():
            dates = sorted(dmap)
            idx[code][field] = {"dates": dates,
                                "vals": [dmap[d] for d in dates]}
    return idx


def value_at(idx, code, field, date, lag_days=0):
    """某交易日 date 当日**已公告**的最新值。无可用数据返回 None（不填充）。"""
    node = (idx.get(code) or {}).get(field)
    if not node:
        return None
    dates = node["dates"]
    if not dates:
        return None
    if lag_days:
        date = _shift(date, -abs(lag_days))
    # 找 <= date 的最后一个
    i = bisect.bisect_right(dates, date) - 1
    if i < 0:
        return None
    return node["vals"][i]


def _shift(date, days):
    """'YYYY-MM-DD' 平移 n 天（负=往前）。"""
    import datetime
    y, m, d = (int(x) for x in date.split("-"))
    t = datetime.date(y, m, d) + datetime.timedelta(days=days)
    return t.isoformat()


def available_since(idx, code, field, date):
    """该字段在 date 当日可用的值，以及它对应的公告日（用于溯源/审计）。"""
    node = (idx.get(code) or {}).get(field)
    if not node or not node["dates"]:
        return None, None
    i = bisect.bisect_right(node["dates"], date) - 1
    if i < 0:
        return None, None
    return node["vals"][i], node["dates"][i]


def align(idx, samples, fields, lag_days=0, code_key="code", date_key="date"):
    """给样本批量附加基本面字段（前缀 `f_`）。

    → 诊断 dict（各字段命中率）。**缺失的样本置 None，不填充**。
    """
    hit = {f: 0 for f in fields}
    for s in samples:
        code = s.get(code_key)
        d = s.get(date_key)
        for f in fields:
            v = value_at(idx, code, f, d, lag_days) if (code and d) else None
            s["f_" + f] = v
            if v is not None:
                hit[f] += 1
    n = max(len(samples), 1)
    return {"n": len(samples),
            "hit_rate": {f: round(hit[f] / n * 100, 1) for f in fields},
            "lag_days": lag_days}


def check_publ_plausible(end_date, publ_date):
    """公告日相对报告期是否合理（A 股法定披露窗口）。

    规则（保守上界，超界即视为数据错误）：
      · 一季报（03-31）→ 最迟 04-30
      · 半年报（06-30）→ 最迟 08-31
      · 三季报（09-30）→ 最迟 10-31
      · 年报  （12-31）→ 最迟次年 04-30

    公告日**早于报告期结束日** ⇒ 必然错误（还没结束怎么出报表）。
    → (ok: bool, reason: str)
    """
    if not end_date or not publ_date:
        return False, "缺日期"
    if publ_date < end_date:
        return False, "公告日早于报告期末（不可能）"
    # 同一年内的披露上限
    y = int(end_date[:4])
    md = end_date[5:]
    limit = {"03-31": "%d-04-30" % y,
             "06-30": "%d-08-31" % y,
             "09-30": "%d-10-31" % y,
             "12-31": "%d-04-30" % (y + 1)}.get(md)
    if limit is None:
        # 非标准报告期（少数公司有特殊季度），只做基本单调性检查
        return True, "非标准报告期，仅查单调"
    if publ_date > limit:
        # 超过法定期限（可容忍一点，年报偶有延迟披露），给 120 天宽限
        from datetime import date
        def _d(s):
            a, b, c = (int(x) for x in s.split("-"))
            return date(a, b, c)
        if (_d(publ_date) - _d(limit)).days > 120:
            return False, "超法定期限 %s 逾 120 天" % limit
    return True, "ok"


def audit_publ_plausible(rows, max_report=10):
    """批量检查公告日合理性（抓数据质量问题的兜底）。→ dict"""
    bad = []
    checked = 0
    seen = set()
    for r in rows:
        key = (r.get("code"), r.get("end_date"), r.get("publ_date"))
        if key in seen:
            continue
        seen.add(key)
        ok, why = check_publ_plausible(r.get("end_date"), r.get("publ_date"))
        checked += 1
        if not ok:
            bad.append({"code": r.get("code"), "end": r.get("end_date"),
                        "publ": r.get("publ_date"), "why": why})
    return {"checked": checked, "violations": bad[:max_report],
            "n_violations": len(bad), "ok": not bad}


def audit_no_lookahead(idx, codes=None, fields=None, samples_per_code=3, seed=0):
    """★ 用**真实数据**自证「无未来函数」。

    做法：对每只票的每个字段，取它的一条财报公告日 D，验证：
      · value_at(D 前一天) 必须 == 该条之前的那一期（或 None）—— 不能提前看到
      · value_at(D 当天)   必须 == 该条的值                —— 当天可用
      · value_at(D 后一天) 仍 == 该条的值                   —— 不回退

    任何一条不满足 ⇒ 存在未来函数或对齐错误。

    → dict{"checked": n, "violations": [...], "ok": bool}
    """
    import random
    rnd = random.Random(seed)
    cs = list(idx) if codes is None else [c for c in codes if c in idx]
    if not cs:
        return {"checked": 0, "violations": [], "ok": True, "note": "无可用数据"}
    rnd.shuffle(cs)
    checked = 0
    bad = []
    for code in cs:
        fmap = idx.get(code) or {}
        for field, node in fmap.items():
            if fields is not None and field not in fields:
                continue
            dates = node["dates"]
            vals = node["vals"]
            if len(dates) < 2:
                continue
            for _ in range(samples_per_code):
                k = rnd.randrange(1, len(dates))
                d_k = dates[k]
                prev_val = vals[k - 1]
                this_val = vals[k]
                before = _shift(d_k, -1)
                # ① 公告前一天不能看到本期
                got_before = value_at(idx, code, field, before)
                if got_before not in (None, prev_val):
                    bad.append({"code": code, "field": field, "at": before,
                                "got": got_before, "expected": prev_val,
                                "why": "公告前看到了不一致的值"})
                if got_before == this_val and prev_val != this_val:
                    bad.append({"code": code, "field": field, "at": before,
                                "got": got_before, "why": "★ 公告前就看到本期（未来函数）"})
                # ② 公告当天必须可用
                got_on = value_at(idx, code, field, d_k)
                if got_on != this_val:
                    bad.append({"code": code, "field": field, "at": d_k,
                                "got": got_on, "expected": this_val,
                                "why": "公告当天未生效"})
                # ③ 公告后一天不回退
                after = _shift(d_k, 1)
                got_after = value_at(idx, code, field, after)
                if got_after != this_val:
                    bad.append({"code": code, "field": field, "at": after,
                                "got": got_after, "expected": this_val,
                                "why": "公告次日回退了"})
                checked += 1
    return {"checked": checked, "violations": bad, "ok": not bad,
            "n_codes": len(cs)}


def _selftest():
    ok = fail = 0

    def T(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ✓ %s" % name)
        else:
            fail += 1
            print("  ✗ %s  %s" % (name, extra))

    rows = [
        # 年报：报告期 2025-12-31，公告 2026-04-14
        {"code": "sh600178", "end_date": "2025-12-31", "publ_date": "2026-04-14",
         "source_type": "income", "field": "ROE", "value": "0.4899"},
        # 三季报：报告期 2025-09-30，公告 2025-10-28
        {"code": "sh600178", "end_date": "2025-09-30", "publ_date": "2025-10-28",
         "source_type": "income", "field": "ROE", "value": "-0.1291"},
        # 半年报
        {"code": "sh600178", "end_date": "2025-06-30", "publ_date": "2025-08-25",
         "source_type": "income", "field": "ROE", "value": "0.2"},
    ]
    idx = build_index(rows, fields={"ROE"})
    T("A1 索引建成", "sh600178" in idx and "ROE" in idx["sh600178"])

    # ★ 核心测试：2026-01-05 时，2025 年报还没公告 ⇒ 只能用三季报
    v = value_at(idx, "sh600178", "ROE", "2026-01-05")
    T("B1 2026-01-05 用三季报（年报未公告）", v == -0.1291, str(v))

    # 2026-04-14 当天可用（<=）
    v2 = value_at(idx, "sh600178", "ROE", "2026-04-14")
    T("B2 公告当日即可用（<= 口径）", v2 == 0.4899, str(v2))

    # 2026-04-13 仍不可用
    v3 = value_at(idx, "sh600178", "ROE", "2026-04-13")
    T("B3 公告前一日不可用", v3 == -0.1291, str(v3))

    # 第一份财报公告前 ⇒ None（不填充）
    v4 = value_at(idx, "sh600178", "ROE", "2025-08-24")
    T("B4 首份财报公告前返回 None（不填充）", v4 is None, str(v4))

    # lag_days 更保守
    v5 = value_at(idx, "sh600178", "ROE", "2026-04-14", lag_days=1)
    T("B5 lag_days=1 时公告当日退回上一期", v5 == -0.1291, str(v5))

    # 未知 code / field
    T("C1 未知 code → None", value_at(idx, "sz000001", "ROE", "2026-05-01") is None)
    T("C2 未知 field → None", value_at(idx, "sh600178", "XXX", "2026-05-01") is None)

    # 溯源
    v6, d6 = available_since(idx, "sh600178", "ROE", "2026-05-01")
    T("D1 available_since 返回值与公告日", v6 == 0.4899 and d6 == "2026-04-14",
      "%s/%s" % (v6, d6))

    # 缺失值不参与（NaN 场景）
    T("E1 NaN 被过滤", _f("nan") is None and _f("inf") is None)
    T("E2 正常数值可用", _f("0.4899") == 0.4899)
    T("E3 空串当缺失", _f("") is None and _f(None) is None)

    # align 不得填充
    smp = [{"code": "sh600178", "date": "2025-08-24"},
           {"code": "sh600178", "date": "2026-05-01"}]
    diag = align(idx, smp, ["ROE"])
    T("F1 无数据样本置 None（不填充）", smp[0]["f_ROE"] is None, str(smp[0]))
    T("F2 有数据样本正确", smp[1]["f_ROE"] == 0.4899, str(smp[1]))
    T("F3 命中率统计正确", diag["hit_rate"]["ROE"] == 50.0, str(diag))

    # 同码头多份财报同日冲突 → 留最后一条（确定性）
    dup = [{"code": "x", "end_date": "2025-12-31", "publ_date": "2026-04-14",
            "source_type": "income", "field": "ROE", "value": "1.0"},
           {"code": "x", "end_date": "2026-03-31", "publ_date": "2026-04-14",
            "source_type": "income", "field": "ROE", "value": "2.0"}]
    idx2 = build_index(dup, fields={"ROE"})
    T("G1 同日多份只留一条（长度=1）",
      len(idx2["x"]["ROE"]["dates"]) == 1, str(idx2["x"]["ROE"]))

    # H3 公告日合理性检查（能抓到「公告日被写错」这类错误）
    bad_rows = [
        {"code": "y", "end_date": "2025-12-31", "publ_date": "2025-01-01",
         "source_type": "income", "field": "ROE", "value": "9.9"},
        {"code": "y", "end_date": "2024-09-30", "publ_date": "2024-10-25",
         "source_type": "income", "field": "ROE", "value": "1.0"},
    ]
    T("H3 公告日早于报告期末 ⇒ 判不合理",
      check_publ_plausible("2025-12-31", "2025-01-01")[0] is False)
    T("H4 正常年报（次年4月）⇒ 合理",
      check_publ_plausible("2025-12-31", "2026-04-14")[0] is True)
    T("H5 超期太久（年报次年9月）⇒ 不合理",
      check_publ_plausible("2025-12-31", "2026-09-01")[0] is False)
    pa = audit_publ_plausible(bad_rows)
    T("H6 批量检查抓到坏记录", pa["ok"] is False and pa["n_violations"] >= 1,
      str(pa))
    pa2 = audit_publ_plausible(rows)
    T("H7 正常数据批量检查通过", pa2["ok"] is True, str(pa2["violations"][:2]))

    # H8 无未来函数自证（值层面）
    aud = audit_no_lookahead(idx, fields={"ROE"}, samples_per_code=2)
    T("H8 无未来函数自证通过", aud["ok"] is True, str(aud["violations"][:2]))

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    if "--selftest" in argv or not argv:
        return _selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
