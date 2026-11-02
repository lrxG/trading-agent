# -*- coding: utf-8 -*-
"""news_search.py · 库内公告检索（2026-10-19 建 · 补「检索能力」断层）。

**为什么建**：审计（2026-10-19）发现旧系统有 `tools/search`（events_lib/events_window/
evt_taxonomy）+ news 技能，新系统**零检索能力**——公告只被 s7/s8/drive/chain_input
按需读过，没有「按词/按票/按日窗」的检索入口。本件补上**库内**检索（不外连）。

## 边界（硬）

- **只读**：零写点（不落盘、不改任何账本）；检索范围 = 本项目 `data/raw/<date>/news.jsonl`
  投放归档（seed_real 自旧包 notices 转换，来源 = 东财公告流）。
- **无网**：本件禁 import 任何数据源客户端（网络/子进程）——实时检索属数据源阶段
  （westock-mcp 当前 unavailable），不在本件。
- **精确匹配**：子串包含匹配（大小写不敏感），**禁模糊/禁语义推断**——命中的就是
  字面包含，不猜「大概相关」。
- **如实报缺**：日期无归档 / 词无命中 ⇒ 显式空结果 + 原因，不造行。
- **base 派生**：沙盒根可检索沙盒归档（与全系统 --base 口径一致）。

## 与旧系统 search 的关系

旧 `tools/search/`（events_lib 等）面向**实时**事件窗口（westock 通道）；本件面向
**已归档**公告。二者不是替代关系：实时腿待数据源落地后另行建设（旧件整目录在旧包
`tools/search/` 可参照）。

## 输出契约（stdout JSON）

```
{"schema": 1, "query": {...}, "scanned_days": [...], "n_hits": N,
 "hits": [{"date","news_id","ts","title","related_codes","source","file"}],
 "miss_reason": null}
```
"""
import io
import json
import os
import sys

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
sys.path.insert(0, _FS)
from paths import ROOT  # noqa: E402

SCHEMA_VERSION = 1


class NewsSearchError(Exception):
    """REASON_CODE 前缀错误。"""


def _raw_root(base=None):
    return os.path.join(base or ROOT, "data", "raw")


def _iter_news_files(base=None):
    """→ [(date, path)] 按日期排序；只认 `data/raw/<YYYY-MM-DD>/news.jsonl` 形态。"""
    root = _raw_root(base)
    if not os.path.isdir(root):
        return []
    out = []
    for d in sorted(os.listdir(root)):
        p = os.path.join(root, d, "news.jsonl")
        if len(d) == 10 and d[4] == "-" and os.path.isfile(p):
            out.append((d, p))
    return out


def search(*, keyword=None, code=None, date_from=None, date_to=None,
           base=None, limit=200):
    """库内公告检索（只读）。

    参数（全可选，至少给一个）：
      keyword    —— 标题子串（大小写不敏感）；
      code       —— related_codes 精确包含（如 'sh600241'）；
      date_from/to —— 闭区间日期窗（YYYY-MM-DD）；
      limit      —— 命中上限（防爆 stdout；默认 200）。
    返回 dict（见模块 docstring 输出契约）；无过滤条件 ⇒ NewsSearchError。
    """
    if not keyword and not code and not date_from and not date_to:
        raise NewsSearchError("BAD_QUERY: 至少给一个过滤条件（keyword/code/date_from/date_to）")
    for k, v in (("date_from", date_from), ("date_to", date_to)):
        if v is not None and (len(v) != 10 or v[4] != "-"):
            raise NewsSearchError("BAD_DATE: %s 须为 YYYY-MM-DD，实得 %r" % (k, v))
    if date_from and date_to and date_from > date_to:
        raise NewsSearchError("BAD_RANGE: date_from(%s) > date_to(%s)" % (date_from, date_to))
    kw = keyword.lower() if keyword else None
    files = _iter_news_files(base)
    scanned, hits, truncated = [], [], False
    for d, p in files:
        if date_from and d < date_from:
            continue
        if date_to and d > date_to:
            continue
        scanned.append(d)
        with io.open(p, encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue                     # 坏行跳过（检索器禁崩）；不计数不声张
                title = rec.get("title") or ""
                codes = rec.get("related_codes") or []
                if kw and kw not in title.lower():
                    continue
                if code and code not in codes:
                    continue
                hits.append({"date": d, "news_id": rec.get("news_id"),
                             "ts": rec.get("ts"), "title": title,
                             "related_codes": codes,
                             "source": rec.get("source"),
                             "file": os.path.relpath(p, base or ROOT).replace("\\", "/")})
                if len(hits) >= limit:
                    truncated = True
                    break
        if truncated:
            break
    miss = None
    if not hits:
        if not scanned:
            miss = "NO_ARCHIVE: 日期窗内无 news.jsonl 归档（raw 覆盖 %s ~ %s）" % (
                files[0][0] if files else "—", files[-1][0] if files else "—")
        else:
            miss = "NO_HIT: 扫描 %d 个归档日无命中（精确子串匹配，禁模糊推断）" % len(scanned)
    return {"schema": SCHEMA_VERSION,
            "query": {"keyword": keyword, "code": code,
                      "date_from": date_from, "date_to": date_to, "limit": limit},
            "scanned_days": scanned, "n_hits": len(hits), "hits": hits,
            "truncated": truncated, "miss_reason": miss}


def _selftest():
    import tempfile as _tf
    import shutil as _sh
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== news_search --selftest ==")
    # A 组：参数闸
    try:
        search()
        T("A1 无过滤条件 ⇒ 拒绝（禁全库倾泻）", False)
    except NewsSearchError:
        T("A1 无过滤条件 ⇒ 拒绝（禁全库倾泻）", True)
    try:
        search(keyword="x", date_from="2026-9-1")
        T("A2 非法日期形态拒绝", False)
    except NewsSearchError:
        T("A2 非法日期形态拒绝", True)
    try:
        search(keyword="x", date_from="2026-09-30", date_to="2026-09-01")
        T("A3 逆序日期窗拒绝", False)
    except NewsSearchError:
        T("A3 逆序日期窗拒绝", True)
    # B 组：合成归档沙盒（精确匹配 / 按码 / 按窗 / 上限 / 坏行）
    sb = _tf.mkdtemp(prefix="ns_sb_")
    try:
        for d, rows in (("2026-09-28", [
                            {"news_id": "N1", "ts": "2026-09-28T10:00", "title": "某某公司签订重大合同",
                             "related_codes": ["sh600241"], "source": "eastmoney_notice"},
                            {"news_id": "N2", "ts": "2026-09-28T11:00", "title": "另一公司分红方案",
                             "related_codes": ["sz000002"], "source": "eastmoney_notice"}]),
                        ("2026-09-29", [
                            {"news_id": "N3", "ts": "2026-09-29T09:30", "title": "某某公司再次公告",
                             "related_codes": ["sh600241"], "source": "eastmoney_notice"}])):
            os.makedirs(os.path.join(sb, "data", "raw", d), exist_ok=True)
            with io.open(os.path.join(sb, "data", "raw", d, "news.jsonl"), "w",
                         encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.write("{坏行不是JSON}\n")            # 坏行须被跳过不崩
        r = search(keyword="重大合同", base=sb)
        T("B1 关键词精确命中（大小写不敏感）",
          r["n_hits"] == 1 and r["hits"][0]["news_id"] == "N1")
        r2 = search(keyword="MAJOR contract".lower(), base=sb)
        T("B1b 英文关键词同样大小写不敏感（对中文库零命中=如实）",
          r2["n_hits"] == 0 and r2["miss_reason"].startswith("NO_HIT"))
        r3 = search(code="sh600241", base=sb)
        T("B2 按票码精确包含（两日共 2 条）",
          r3["n_hits"] == 2 and {h["date"] for h in r3["hits"]} == {"2026-09-28", "2026-09-29"})
        r4 = search(keyword="公司", date_from="2026-09-29", date_to="2026-09-29", base=sb)
        T("B3 日期窗闭区间过滤", r4["n_hits"] == 1 and r4["scanned_days"] == ["2026-09-29"])
        r5 = search(keyword="公司", base=sb, limit=1)
        T("B4 limit 上限生效且 truncated 如实标",
          r5["n_hits"] == 1 and r5["truncated"] is True)
        r6 = search(keyword="不存在的词xyz", base=sb)
        T("B5 无命中 ⇒ 显式 miss_reason（不造行）",
          r6["n_hits"] == 0 and "NO_HIT" in r6["miss_reason"])
        r7 = search(keyword="公司", date_from="2026-01-01", date_to="2026-01-31", base=sb)
        T("B6 窗外无归档 ⇒ NO_ARCHIVE 原因（禁静默空）",
          r7["n_hits"] == 0 and "NO_ARCHIVE" in r7["miss_reason"])
        # B7：零写点——检索前后沙盒树指纹不变
        import hashlib as _h

        def _fp():
            out = []
            for dp, _, fns in os.walk(sb):
                for fn in fns:
                    p = os.path.join(dp, fn)
                    out.append((os.path.relpath(p, sb),
                                _h.sha256(io.open(p, "rb").read()).hexdigest()[:12]))
            return sorted(out)

        before = _fp()
        search(keyword="公司", base=sb)
        search(code="sh600241", base=sb)
        T("B7 检索零写点（沙盒树指纹前后不变）", _fp() == before)
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    print("== news_search --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def _main(argv):
    if "--selftest" in argv:
        return _selftest()

    def _val(flag):
        return argv[argv.index(flag) + 1] if flag in argv else None

    base = _val("--base")
    if base and not os.path.isdir(base):
        print("--base 必须是已存在目录（防误读生产），实际: %s" % base)
        return 2
    kw, code = _val("--keyword"), _val("--code")
    dfrom, dto = _val("--from"), _val("--to")
    try:
        r = search(keyword=kw, code=code, date_from=dfrom, date_to=dto, base=base)
    except NewsSearchError as e:
        print("⛔ %s" % e, file=sys.stderr)
        return 2
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
