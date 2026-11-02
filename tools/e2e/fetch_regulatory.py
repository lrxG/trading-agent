#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fetch_regulatory.py · 监管/股东行为日扫（2026-10-30 建 · 信息能力升级 P0/P1 落地 · task #281）

## 功能

- `--mode reg`（默认）：监管动态日扫——对 news 归档按关键词组（问询函/监管工作函/关注函/
  立案/警示函/处罚）检索 → 事件去重（news_id 唯一）→ 全部记 B 级（媒体转述，A 级须对公告原文核）。
- `--mode holder`：候选池股东行为日扫——逐码 `westock notice` 取近 20 条公告，
  标题命中（减持/质押/解禁/回购/定增/立案）→ 输出清单。
- 输出：`data/raw/<date>/regulatory.jsonl` / `holder_behavior.jsonl`（原始采集件，落 raw 不落 knowledge）。

## 纪律

1. 本件只做**事实层采集与关键词过滤**，零判断（影响方向/定价判定归 Agent 与 S1）。
2. 全部条目默认 **B 级**（媒体/公告转述）；升 A 须对交易所公告原文核验。
3. 数据源 = `tools/query/news_search.py`（库内 news.jsonl 归档）+ `westock notice` CLI。

## 用法

python tools/e2e/fetch_regulatory.py --date 2026-10-30                # 监管扫描（默认）
python tools/e2e/fetch_regulatory.py --mode holder --date 2026-10-30  # 股东行为日扫
python tools/e2e/fetch_regulatory.py --selftest
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import subprocess
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "tools", "query"))
import news_search as NS  # noqa: E402

REG_KEYWORDS = ("问询函", "监管工作函", "关注函", "立案", "警示函", "处罚")
HOLDER_KEYWORDS = ("减持", "质押", "解禁", "回购", "定增", "立案", "询问")


def _raw_dir(date):
    d = os.path.join(_ROOT, "data", "raw", date)
    os.makedirs(d, exist_ok=True)
    return d


def scan_regulatory(date, window_days=3):
    """监管关键词检索 → 去重事件列表（B 级）。"""
    dt = datetime.date.fromisoformat(date)
    date_from = (dt - datetime.timedelta(days=window_days)).isoformat()
    seen, out = set(), []
    for kw in REG_KEYWORDS:
        try:
            rep = NS.search(keyword=kw, date_from=date_from, date_to=date, limit=100)
        except Exception as e:  # 检索器异常如实记录，不中断其余关键词
            out.append({"grade": "B", "type": "SCAN_ERROR", "keyword": kw, "error": repr(e)})
            continue
        for h in rep.get("hits") or []:
            nid = h.get("news_id") or "%s|%s" % (h.get("ts"), h.get("title"))
            if nid in seen:
                continue  # 事件归并：同一 news_id 不重复计证据
            seen.add(nid)
            title = h.get("title") or ""
            kws = [k for k in REG_KEYWORDS if k in title]
            out.append({"date": h.get("date"), "ts": h.get("ts"), "grade": "B",
                        "type": "监管", "hit_keywords": kws,
                        "target": h.get("related_codes") or [],
                        "fact": title, "source": h.get("source"),
                        "news_id": h.get("news_id"), "keyword": kw})
    return out


def _notice_titles(code, limit=20):
    """westock notice CLI → [(time, title)]；解析失败返回空（如实）。"""
    try:
        p = subprocess.run(["westock", "notice", code, "--limit", str(limit)],
                           capture_output=True, timeout=60)
        text = p.stdout.decode("utf-8", "replace")
    except Exception:
        return []
    out = []
    for ln in text.splitlines():
        if not ln.strip().startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) >= 4 and cells[0] not in ("", "id", "#"):
            # 列序随模板浮动：取含日期样式的格与最长文本格为 title
            tcell = next((c for c in cells if re.match(r"20\d{2}-\d{2}-\d{2}", c)), "")
            title = max(cells[1:], key=len) if len(cells) > 1 else ""
            out.append((tcell, title))
    return out


def scan_holder(codes, date):
    """候选池股东行为日扫。→ 列表（B 级；A 级须对公告原文核验）。"""
    out = []
    for c in codes:
        for tcell, title in _notice_titles(c):
            hits = [k for k in HOLDER_KEYWORDS if k in title]
            if hits:
                out.append({"date": tcell or date, "grade": "B", "code": c,
                            "hit_keywords": hits, "fact": title})
    return out


def _load_codes():
    """候选池代码；空则返回空（如实，不猜）。"""
    p = os.path.join(_ROOT, "data", "state", "candidates.json")
    codes = []
    try:
        d = json.load(io.open(p, encoding="utf-8"))
        def _walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    if isinstance(k, str) and re.match(r"^(s[hz]|bj)\d{6}$", k):
                        codes.append(k)
                    _walk(v)
            elif isinstance(x, list):
                for v in x:
                    _walk(v)
        _walk(d)
    except Exception:
        pass
    return sorted(set(codes))[:60]


def _selftest():
    import tempfile
    tmp = tempfile.mkdtemp(prefix="reg_selftest_")
    d1 = os.path.join(tmp, "data", "raw", "2026-10-29")
    os.makedirs(d1, exist_ok=True)
    with io.open(os.path.join(d1, "news.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"news_id": "n1", "ts": "2026-10-29 18:00", "title": "某某公司收到监管工作函",
                            "related_codes": ["sh600000"], "source": "财联社"}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"news_id": "n1", "ts": "2026-10-29 18:01", "title": "某某公司收到监管工作函(转载)",
                            "related_codes": ["sh600000"], "source": "转载"}, ensure_ascii=False) + "\n")
    orig_root = NS.ROOT
    NS.ROOT = tmp
    try:
        items = scan_regulatory("2026-10-29", window_days=1)
        assert len(items) == 1, "事件去重失败: %d" % len(items)
        assert items[0]["hit_keywords"] == ["监管工作函"]
    finally:
        NS.ROOT = orig_root
    print("fetch_regulatory selftest OK（去重+关键词命中）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="监管/股东行为日扫（事实层，零判断）")
    ap.add_argument("--mode", choices=("reg", "holder", "all"), default="reg")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--codes", default="", help="holder 模式逗号分隔代码（缺省=候选池）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    rd = _raw_dir(a.date)
    outs = []
    if a.mode in ("reg", "all"):
        items = scan_regulatory(a.date)
        outs.append(("regulatory.jsonl", items))
        print("[reg] %d 条（B级，去重后）" % len(items))
    if a.mode in ("holder", "all"):
        codes = [c.strip() for c in a.codes.split(",") if c.strip()] or _load_codes()
        items = scan_holder(codes, a.date)
        outs.append(("holder_behavior.jsonl", items))
        print("[holder] 候选池 %d 码 · %d 条命中" % (len(codes), len(items)))
    for fn, items in outs:
        p = os.path.join(rd, fn)
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            for x in items:
                f.write(json.dumps(x, ensure_ascii=False) + "\n")
        print("[SAVED]", os.path.relpath(p, _ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
