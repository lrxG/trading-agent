#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fetch_announcements.py · 全市场公告日采（2026-11-09 建）

## 为什么有这个文件

2026-11-09 用户令：「我发现你总是缺少企业公告方面的消息」。
病根审计结论（不是漏看，是采集器设计错了）：

1. 原 `fetch_regulatory.py --mode reg` **不抓公告**——它只在自家 `news.jsonl`
   （11-06 仅 23 条标题、body 全空）里搜 6 个监管关键词，命中期望 0 条，
   所以 `regulatory.jsonl` 连续多日为 0 字节是**必然**，不是偶发。
2. 原 `--mode holder` 依赖 `westock notice` + `candidates.json`；RISK_OFF 期
   候选池为空 → codes=[] → 同样 0 条。
3. **最致命的**：整个系统没有「全市场公告」采集器。它只扫「我在意的票」和
   「自家的新闻库」，而主线恰恰来自「我没在意的票」。且它拿不到
   「昨盘后~今晨」这个对盘前最关键的窗口。

本件解决第 3 点：直连**巨潮资讯网**（证监会指定法定信息披露平台）
`hisAnnouncement/query` 接口，按日期区间拉全市场公告，落 raw，按业务类别分桶。

## 数据源与口径

- 源：`http://www.cninfo.com.cn/new/hisAnnouncement/query`（POST，无需 key）
- `column=szse` 覆盖深沪全市场（不是只深市，实测含 600801/603920）
- 时间戳为毫秒，按东八区转 `YYYY-MM-DD`
- 公告原文 PDF 前缀：`http://static.cninfo.com.cn/` + `adjunctUrl`

## 分级纪律

- 本件只做**事实层采集 + 关键词分桶**，零判断（影响方向/定价归 Agent）。
- 全部条目为 **A 级来源（法定披露平台原文标题）**，优于媒体转述；
  但「标题级」仍不等于「原文核过」——标题含歧义时须核 PDF。

## 用法

python tools/e2e/fetch_announcements.py --date 2026-11-09
python tools/e2e/fetch_announcements.py --date 2026-11-09 --since 2026-11-07   # 含周末
python tools/e2e/fetch_announcements.py --date 2026-11-09 --show 回购,立案     # 只看某几桶
python tools/e2e/fetch_announcements.py --selftest

输出：
- `data/raw/<date>/announcements.jsonl`（全量，逐条含分桶标签）
- `data/raw/<date>/ann_summary.json`（分桶计数 + 每桶条数）
屏显：分桶 Top 清单（默认每桶 8 条）
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
_PDF = "http://static.cninfo.com.cn/"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 业务分桶：(桶名, 关键词元组)。顺序即屏显顺序 = 对操作的相关度顺序。
BUCKETS = (
    ("回购增持", ("回购", "增持", "员工持股", "股权激励")),
    ("立案处罚", ("立案", "处罚", "警示函", "监管函", "关注函", "问询函", "纪律处分", "监管工作函")),
    ("减持解禁", ("减持", "解禁", "限售", "解除限售", "质押", "冻结", "司法拍卖")),
    ("重组收购", ("收购", "重组", "重大资产", "要约", "合并", "分立", "股权转让", "实际控制人")),
    ("订单合同", ("中标", "合同", "订单", "框架协议", "供货", "采购", "签署")),
    ("业绩", ("业绩预告", "业绩快报", "季度报告", "年度报告", "半年度报告", "预增", "预减", "扭亏", "亏损")),
    ("停复牌", ("停牌", "复牌", "暂停上市", "终止上市", "退市")),
    ("涨价扩产", ("涨价", "调价", "提价", "投产", "扩产", "项目建设", "投资建设", "产能")),
    ("定增融资", ("非公开", "定向增发", "可转债", "配股", "发行股份", "募集资金", "H股")),
    ("异动澄清", ("异常波动", "风险提示", "澄清")),
)

# 噪音过滤器：这些标题形式是常规合规披露，不含交易信息，直接归"其他"。
# 2026-11-09 实测：不收此规则时"异动澄清"96 条里 7 成是业绩说明会，
# "减持解禁"里混入大量"回购事项前十大股东持股情况"，屏显可用性差。
NOISE = ("业绩说明会", "前十大股东", "前十名股东", "翌日披露报表", "翌日披露報表",
         "独立董事候选人", "独立董事提名人", "独立董事专门会议", "法律意见书",
         "财务顾问报告", "持续督导", "董事会决议", "股东大会决议", "会议通知",
         "公司章程", "议事规则", "管理制度")

_HDRS = {
    "User-Agent": _UA,
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "http://www.cninfo.com.cn",
}


def _ts_to_date(ms):
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d")
    except Exception:
        return ""


def bucket_of(title):
    """标题 → 命中桶列表（可多桶；空 = 其他）。先过噪音滤器再分桶。"""
    if any(n in title for n in NOISE):
        return ["其他"]
    hits = []
    for name, kws in BUCKETS:
        if any(k in title for k in kws):
            hits.append(name)
    return hits or ["其他"]


def fetch_page(se_date, page, page_size=30, retries=3):
    body = {
        "pageNum": str(page), "pageSize": str(page_size), "column": "szse",
        "tabName": "fulltext", "plate": "", "stock": "", "searchkey": "",
        "secid": "", "category": "", "trade": "", "seDate": se_date,
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    data = urllib.parse.urlencode(body).encode()
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(_URL, data=data, headers=_HDRS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError("fetch_page p%d failed: %r" % (page, last))


def collect(se_date, page_size=30, max_pages=200, sleep=0.25):
    """按区间拉全量公告 → [{code,name,date,title,pdf,tags}]（按 code+title+date 去重）。"""
    first = fetch_page(se_date, 1, page_size)
    total_pages = int(first.get("totalpages") or 1)
    total_pages = min(total_pages, max_pages)
    pages = [first] + [fetch_page(se_date, p, page_size)
                       for p in range(2, total_pages + 1) if not time.sleep(sleep)]
    out, seen = [], set()
    for d in pages:
        for a in (d.get("announcements") or []):
            code = (a.get("secCode") or "").strip()
            name = (a.get("secName") or "").strip()
            title = (a.get("announcementTitle") or "").strip()
            dt = _ts_to_date(a.get("announcementTime"))
            pdf = a.get("adjunctUrl") or ""
            key = (code, dt, title)
            if not code or key in seen:
                continue
            seen.add(key)
            out.append({"code": code, "name": name, "date": dt, "title": title,
                        "pdf": (_PDF + pdf) if pdf else "", "tags": bucket_of(title)})
    return out, int(first.get("totalAnnouncement") or 0)


def _selftest():
    assert bucket_of("关于以集中竞价交易方式回购股份方案的公告") == ["回购增持"], bucket_of("回购")
    assert "立案处罚" in bucket_of("关于公司收到中国证监会立案告知书的公告")
    assert bucket_of("关于股东减持股份的预披露公告") == ["减持解禁"]
    assert bucket_of("关于签署日常经营重大合同的公告") == ["订单合同"]
    assert bucket_of("关于股价异常波动的风险提示公告") == ["异动澄清"]
    assert bucket_of("完全不相关的东西") == ["其他"]
    assert bucket_of("关于召开2026年第三季度业绩说明会的公告") == ["其他"], "噪音滤器失效"
    assert bucket_of("关于回购股份事项前十大股东持股情况的公告") == ["其他"], "噪音滤器失效"
    assert bucket_of("股票交易异常波动暨风险提示公告") == ["异动澄清"]
    print("fetch_announcements selftest OK（分桶规则）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="全市场公告日采（巨潮 · 事实层）")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--since", default="", help="区间起点；缺省=--date 前一天")
    ap.add_argument("--page-size", type=int, default=30)
    ap.add_argument("--show", default="", help="只看这些桶（逗号分隔）")
    ap.add_argument("--per-bucket", type=int, default=8)
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()

    date = a.date
    since = a.since or (datetime.date.fromisoformat(date) - datetime.timedelta(days=1)).isoformat()
    se_date = "%s~%s" % (since, date) if since != date else date

    t0 = time.time()
    items, total = collect(se_date, a.page_size, a.max_pages)
    print("巨潮区间 %s · 官方口径 %d 条 · 去重后落盘 %d 条 · 耗时 %.1fs"
          % (se_date, total, len(items), time.time() - t0))

    rd = os.path.join(_ROOT, "data", "raw", date)
    os.makedirs(rd, exist_ok=True)
    p = os.path.join(rd, "announcements.jsonl")
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        for x in items:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")

    # 分桶统计（一票多桶时各桶都计）
    cnt = {}
    for x in items:
        for t in x["tags"]:
            cnt[t] = cnt.get(t, 0) + 1
    summ = {"date": date, "se_date": se_date, "official_total": total,
            "saved": len(items), "buckets": cnt}
    with io.open(os.path.join(rd, "ann_summary.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(summ, ensure_ascii=False, indent=1))

    order = [b[0] for b in BUCKETS] + ["其他"]
    show = [s.strip() for s in a.show.split(",") if s.strip()] or order
    print("\n分桶：", " · ".join("%s %d" % (k, cnt[k]) for k in order if k in cnt))
    for b in show:
        rows = [x for x in items if b in x["tags"]]
        if not rows:
            continue
        print("\n【%s】%d 条" % (b, len(rows)))
        for x in rows[:a.per_bucket]:
            print("  %s %s | %s | %s" % (x["date"][5:], x["code"], x["name"], x["title"][:60]))
    print("\n[SAVED]", os.path.relpath(p, _ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
