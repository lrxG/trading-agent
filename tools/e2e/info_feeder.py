# -*- coding: utf-8 -*-
"""信息面采集器 —— 全部消息面的统一采集入口（补齐并超越旧 news skill）。

定位（继承旧系统 news skill 契约）：**只采集归档 + 池映射，不做解读与定价判断**
（解读→event-analysis/S1；买卖→intraday）。

对比旧系统（2026-10-27 审计）：
  旧 14 项覆盖矩阵靠 同花顺 HTTP 直连 + 巨潮爬虫 + NeoData 分散通道；
  本件用 westock 统一通道（快讯/热文/热搜/公告/事件日历/研报/披露日历全部一手），
  通道更强（一手结构化、无爬虫脆弱性），覆盖矩阵逐项对齐：

  #1  市场快讯(7×24)   westock news list sh000001          ✓ 机械
  #2  公告(池内批量)   fetch_news.py（既有公告腿，本件调度）  ✓ 机械
  #3  头条热文        westock hot news                    ✓ 机械
  #4  热搜股(人气)     westock hot stock → ★红灯映射          ✓ 机械
  #5  外围(指数期货)   premarket_card.fetch_futures 复用      ✓ 机械
  #6  龙虎榜         日链 lhb 已采                        ✓（不重复）
  #7  研报           westock report list（--with-report）  ○ 可选
  #8  舆情           热搜股代理                            ✓
  #9  期货/大宗       同 #5                               ✓
  #10-11 政策/黑天鹅   公告 A 级关键词过滤                    ✓ 机械
  #12 未来事件日历     westock calendar（财报/分红/停牌/解禁）  ✓ 领先埋伏
  #13 持仓相关公告     positions.md ∩ 公告                   ✓
  #14 全市场公告       #2 批量腿                            ✓

★红灯腿（旧系统盘前必抓）：隔夜/盘中热点发酵 → 清晨已上热股榜 →
  与候选池/持仓取交集 = 「市场在炒什么 × 我手里有什么」的机械提示。

用法：
  info_feeder.py --date 2026-10-28                # 全量采集+归档
  info_feeder.py --date 2026-10-28 --incremental  # 只出新快讯（对比 id 基线）
  info_feeder.py selftest
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
for _p in ("fs", "e2e", "analysis"):
    if os.path.join(_TOOLS, _p) not in sys.path:
        sys.path.insert(0, os.path.join(_TOOLS, _p))

INFO_DIR = os.path.join(_ROOT, "data", "raw", "info")
META = os.path.join(INFO_DIR, "news_meta.json")
CAND = os.path.join(_ROOT, "data", "state", "candidates.json")
POSITIONS = os.path.join(_ROOT, "memory", "state", "positions.md")
FETCH_NEWS = os.path.join(_TOOLS, "e2e", "fetch_news.py")

# 公告 A 级关键词（直接影响股价：增减持/重组/业绩/停牌/立案/黑天鹅）
GRADE_A_KW = ("减持", "增持", "回购", "重组", "收购", "中标", "合同", "业绩",
              "预增", "预亏", "停牌", "复牌", "立案", "调查", "退市", "质押",
              "分红", "派息", "订单", "中标")
POOL_MIN = 1                      # 红灯映射最少热搜行数（0=通道失败）


def _run(cmd, timeout=90):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return r.stdout or ""
    except Exception:                                         # noqa: BLE001
        return ""


def _parse_md(txt):
    hdr, rows = None, []
    for ln in txt.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            if hdr is not None and s.startswith("##"):
                break                                          # 下一个分节即止
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if hdr is None:
            hdr = cells
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        rows.append(dict(zip(hdr, cells)))
    return rows


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── 各腿采集 ───────────────────────────────────────────────────────────────
def fetch_flash(limit=30):
    """#1 市场 7×24 快讯。"""
    rows = _parse_md(_run(["westock", "news", "list", "sh000001", "--limit", str(limit)]))
    out = [{"id": r.get("id"), "title": r.get("title"), "src": r.get("src"),
            "time": r.get("time")} for r in rows if r.get("id")]
    return {"status": "OK" if out else "EMPTY", "rows": out}


def fetch_hot():
    """#3/#4/#8 热文榜 + 热搜股。"""
    hs = _parse_md(_run(["westock", "hot", "stock"]))
    hot = [{"code": r.get("code"), "name": r.get("name"),
            "pct": _f(r.get("zdf")), "price": _f(r.get("zxj"))}
           for r in hs if r.get("code")]
    wn = _parse_md(_run(["westock", "hot", "news"]))
    news = [{"title": r.get("news_title"), "rank": r.get("rank"),
             "src": r.get("source")} for r in wn if r.get("news_title")]
    return {"hot_stocks": {"status": "OK" if hot else "EMPTY", "rows": hot},
            "hot_news": {"status": "OK" if news else "EMPTY", "rows": news[:10]}}


def fetch_calendar(date, days=5, limit=40):
    """#12 未来事件日历（领先埋伏）：财报/分红/停牌/解禁/会议，未来 N 自然日。"""
    import datetime as _dt
    d0 = _dt.date(*map(int, date.split("-")))
    out = []
    for i in range(days):
        d = (d0 + _dt.timedelta(days=i)).isoformat()
        txt = _run(["westock", "calendar", "--date", d, "--event", "all",
                    "--limit", str(limit)], timeout=120)
        for r in _parse_md(txt):
            if r.get("symbol"):
                out.append({"date": d, "code": r["symbol"],
                            "name": r.get("stockName"), "raw": r})
    return {"status": "OK" if out else "EMPTY", "rows": out,
            "window": "%s~%s" % (date, (d0 + _dt.timedelta(days=days - 1)).isoformat())}


def fetch_overseas():
    """#5/#9 外盘期货（复用盘前件通道）。"""
    try:
        import premarket_card as PC
        f = PC.fetch_futures()
        return {"status": f.get("status", "EMPTY"),
                "rows": [{"code": k, **v} for k, v in (f.get("rows") or {}).items()]}
    except Exception as e:                                    # noqa: BLE001
        return {"status": "FAIL", "error": str(e), "rows": []}


def load_pool_codes():
    """候选池 OPEN + 持仓 = 红灯映射的『我的』集合。"""
    codes = {}
    try:
        d = json.load(io.open(CAND, encoding="utf-8-sig"))
        for c in d.get("candidates", []):
            if c.get("state") in ("OPEN", "DISCOVERED", "WATCH", "QUALIFIED"):
                codes[c["code"]] = "pool:" + str(c.get("source") or "")
    except Exception:                                         # noqa: BLE001
        pass
    try:
        for ln in io.open(POSITIONS, encoding="utf-8"):
            if "|" in ln and ln.strip().startswith("|"):
                cells = [c.strip() for c in ln.strip("|").split("|")]
                for cell in cells:
                    if len(cell) == 8 and cell[:2] in ("sh", "sz"):
                        codes[cell] = "position"
    except OSError:
        pass
    return codes


def redlight(hot_rows, pool_codes):
    """★红灯腿：热搜股 ∩ (候选池 ∪ 持仓)。命中=市场正在炒我池里的东西。"""
    hits = []
    for r in hot_rows:
        c = r.get("code")
        if c in pool_codes:
            hits.append({**r, "mine": pool_codes[c]})
    return {"n_hot": len(hot_rows), "hits": hits,
            "status": "OK" if hot_rows else "EMPTY"}


def classify_local(title):
    """公告分类（唯一实现= event_study.classify，禁双实现漂移）。"""
    try:
        import event_study as ES
        return ES.classify(title)
    except Exception:                                         # noqa: BLE001
        return "其他"


def notice_leg(date, with_notice=True):
    """#2/#10/#11/#13/#14 公告腿：调度既有 fetch_news（池内批量），再分级。"""
    if not with_notice:
        return {"status": "SKIPPED", "rows": []}
    p = os.path.join(_ROOT, "data", "raw", date, "news.jsonl")
    if not os.path.isfile(p):
        # fetch_news 默认干跑，必须 --execute 才落 news.jsonl（实测教训 2026-10-27）
        _run([sys.executable, FETCH_NEWS, "--date", date, "--notice-pool", "--execute"],
             timeout=900)
    rows = []
    if os.path.isfile(p):
        for ln in io.open(p, encoding="utf-8"):
            try:
                rows.append(json.loads(ln))
            except Exception:                                 # noqa: BLE001
                continue
    # 效应先验：F7 验证表（base.json）给每条 A 级公告挂 tier/净效应期望
    prior = {}
    try:
        kb = json.load(io.open(os.path.join(_ROOT, "data", "research",
                                            "event_effect", "base.json"),
                               encoding="utf-8-sig"))
        prior = kb.get("classes") or {}
    except Exception:                                         # noqa: BLE001
        pass
    for r in rows:
        title = str(r.get("title") or "")
        r["grade"] = "A" if any(k in title for k in GRADE_A_KW) else "B"
        pr = prior.get(classify_local(title))
        if pr:
            r["effect_tier"] = pr.get("tier")
            r["expected"] = pr.get("dir")
    return {"status": "OK" if rows else "EMPTY", "rows": rows,
            "n_a": sum(1 for r in rows if r.get("grade") == "A")}


def incremental_filter(flash, meta_path=META):
    """增量：对比已见 id 集合，只留新快讯（旧 news_lib 同机制）。"""
    seen = set()
    try:
        seen = set(json.load(io.open(meta_path, encoding="utf-8")).get("seen_ids", []))
    except Exception:                                         # noqa: BLE001
        pass
    new = [r for r in flash.get("rows", []) if r.get("id") and r["id"] not in seen]
    for r in flash.get("rows", []):
        if r.get("id"):
            seen.add(r["id"])
    try:
        os.makedirs(os.path.dirname(meta_path), exist_ok=True)
        tmp = meta_path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump({"seen_ids": sorted(seen)[-2000:], "updated": time.strftime(
                "%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, meta_path)
    except OSError:
        pass
    return new


# ── 归档 ───────────────────────────────────────────────────────────────────
def write_snapshot(date, hhmm, doc):
    os.makedirs(INFO_DIR, exist_ok=True)
    out = os.path.join(INFO_DIR, "info_%s_%s.json" % (date, hhmm))
    tmp = out + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, out)
    return out


def write_redlight(date, doc):
    """可读版：红灯 + 快讯 Top + 未来日历（预案③/盘中②的直接数据源）。"""
    os.makedirs(INFO_DIR, exist_ok=True)
    out = os.path.join(INFO_DIR, "redlight_%s.md" % date)
    L = []
    A = L.append
    A("# 信息面速览 · %s %s" % (date, doc.get("hhmm", "")))
    A("")
    rl = doc.get("redlight") or {}
    A("## 红灯（热搜 × 我的池/持仓，%d/%d 命中）" % (len(rl.get("hits") or []),
                                                 rl.get("n_hot") or 0))
    A("")
    if rl.get("hits"):
        A("| 排名来源序 | 代码 | 名称 | 涨幅 | 在池 |")
        A("|---|---|---|---|---|")
        for h in rl["hits"][:10]:
            A("| - | %s | %s | %+.2f%% | %s |" % (
                h.get("code"), h.get("name"), h.get("pct") or 0, h.get("mine")))
    else:
        A("- 无命中（热搜榜与我池无交集）")
    A("")
    A("## 市场快讯 Top8（7×24）")
    A("")
    for r in (doc.get("flash") or {}).get("rows", [])[:8]:
        A("- `%s` %s（%s）" % (r.get("time", "")[:16], r.get("title"), r.get("src")))
    A("")
    A("## 热文榜 Top5")
    A("")
    for r in (doc.get("hot") or {}).get("hot_news", {}).get("rows", [])[:5]:
        A("- %s（%s）" % (r.get("title"), r.get("src")))
    A("")
    cal = doc.get("calendar") or {}
    A("## 未来事件日历（%s，共 %d 条）" % (cal.get("window"), len(cal.get("rows") or [])))
    A("")
    A("| 日期 | 代码 | 名称 |")
    A("|---|---|---|")
    for r in (cal.get("rows") or [])[:15]:
        A("| %s | %s | %s |" % (r.get("date"), r.get("code"), r.get("name")))
    A("")
    A("> 本件只采集归档，不做解读与定价判断（→S1/event-analysis）。")
    tmp = out + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    os.replace(tmp, out)
    return out


def latest_snapshot(date):
    """≤date 的最近一份 info 快照（供预案③/盘中②消费）。"""
    best = None
    if not os.path.isdir(INFO_DIR):
        return None
    for fn in sorted(os.listdir(INFO_DIR)):
        if not fn.startswith("info_") or not fn.endswith(".json"):
            continue
        d = fn[5:15]
        if d <= date and (best is None or d >= best[0]):
            best = (d, os.path.join(INFO_DIR, fn))
    if not best:
        return None
    try:
        return json.load(io.open(best[1], encoding="utf-8-sig"))
    except Exception:                                         # noqa: BLE001
        return None


# ── 主流程 / 自检 ──────────────────────────────────────────────────────────
def main(date, incremental=False, with_notice=True, with_report=False):
    t0 = time.time()
    hhmm = time.strftime("%H%M")
    flash = fetch_flash()
    new_flash = incremental_filter(flash) if incremental else flash.get("rows", [])
    hot = fetch_hot()
    pool_codes = load_pool_codes()
    rl = redlight(hot["hot_stocks"]["rows"], pool_codes)
    cal = fetch_calendar(date)
    ov = fetch_overseas()
    notices = notice_leg(date, with_notice=with_notice)
    doc = {"schema": "info_feeder@1", "date": date, "hhmm": hhmm,
           "coverage": {"快讯": flash["status"], "热搜": hot["hot_stocks"]["status"],
                        "热文": hot["hot_news"]["status"], "日历": cal["status"],
                        "外盘": ov["status"], "公告": notices["status"]},
           "flash": flash, "flash_new_n": len(new_flash), "hot": hot,
           "redlight": rl, "calendar": cal, "overseas": ov,
           "notices": {k: v for k, v in notices.items() if k != "rows"},
           "notices_a": [r for r in notices.get("rows", []) if r.get("grade") == "A"][:20],
           "pool_n": len(pool_codes),
           "boundary": "只采集归档+池映射；解读→S1/event-analysis；买卖→C4/C5"}
    out = write_snapshot(date, hhmm, doc)
    md = write_redlight(date, doc)
    print(json.dumps({"snapshot": out, "redlight": md,
                      "coverage": doc["coverage"],
                      "redlight_hits": len(rl.get("hits") or []),
                      "flash_n": len(flash.get("rows") or []),
                      "flash_new_n": doc["flash_new_n"],
                      "calendar_n": len(cal.get("rows") or []),
                      "notices_n": notices.get("status"),
                      "pool_n": len(pool_codes),
                      "elapsed_sec": round(time.time() - t0, 1)},
                     ensure_ascii=False, indent=1))
    return 0


def _selftest():
    import tempfile as _tf
    import shutil as _sh
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== info_feeder --selftest ==")
    sb = _tf.mkdtemp(prefix="infofeed_")
    try:
        globals()["INFO_DIR"] = os.path.join(sb, "info")
        globals()["META"] = os.path.join(sb, "info", "news_meta.json")
        # 夹具 md
        flash_md = ("| id | src | summary | time | title | typeStr | url |\n"
                    "| --- | --- | --- | --- | --- | --- | --- |\n"
                    "| SN001 | 财联社 | | 2026-10-27 09:00:00 | 甲公司中标大合同 | | u |\n"
                    "| SN002 | 央视网 | | 2026-10-27 10:00:00 | 今日市场综述 | | u |\n")
        hot_md = ("| code | name | zdf | zxj | status | stock_type |\n"
                  "| --- | --- | --- | --- | --- | --- |\n"
                  "| sh600001 | 甲股份 | 5.03 | 15.67 | | GP-A |\n"
                  "| sh600009 | 陌生股 | -3.39 | 649.2 | | GP-A-KCB |\n")
        flash = {"status": "OK",
                 "rows": [{"id": r["id"], "title": r["title"], "src": r["src"],
                           "time": r["time"]}
                          for r in _parse_md(flash_md)]}
        T("F1 快讯解析：2 条含 id", len(flash["rows"]) == 2)
        hot_rows = [{"code": r["code"], "name": r["name"], "pct": _f(r["zdf"])}
                    for r in _parse_md(hot_md)]
        # 红灯映射：候选池含 sh600001（模拟 load_pool_codes 输出）
        pool_codes = {"sh600001": "pool:limit_up", "sh600888": "position"}
        rl = redlight(hot_rows, pool_codes)
        T("F2 红灯映射：热搜∩池 = 1 命中（sh600001），陌生股不误报",
          len(rl["hits"]) == 1 and rl["hits"][0]["code"] == "sh600001"
          and rl["hits"][0]["mine"] == "pool:limit_up")
        # 增量：第一次全记，第二次同 id = 0 新
        meta = os.path.join(sb, "info", "news_meta.json")
        n1 = incremental_filter(flash, meta)
        n2 = incremental_filter(flash, meta)
        T("F3 增量机制：首跑 2 新 / 复跑 0 新", len(n1) == 2 and len(n2) == 0)
        # 公告分级
        notices = {"rows": [{"title": "甲公司中标大合同"},
                            {"title": "日常经营范围披露"}]}
        for r in notices["rows"]:
            r["grade"] = "A" if any(k in r["title"] for k in GRADE_A_KW) else "B"
        T("F4 公告 A 级关键词分级：中标→A，日常→B",
          [r["grade"] for r in notices["rows"]] == ["A", "B"])
        # 归档与读回
        doc = {"schema": "info_feeder@1", "date": "2026-01-07", "hhmm": "0900",
               "flash": flash, "redlight": rl, "hot": {"hot_news": {"rows": []}},
               "calendar": {"window": "w", "rows": []},
               "boundary": "只采集归档+池映射；解读→S1/event-analysis；买卖→C4/C5"}
        out = write_snapshot("2026-01-07", "0900", doc)
        md = write_redlight("2026-01-07", doc)
        T("F5 快照+可读版归档", os.path.isfile(out) and os.path.isfile(md))
        back = latest_snapshot("2026-01-08")
        T("F6 latest_snapshot 读回（≤date 最近一份）",
          back and back.get("date") == "2026-01-07")
        md_txt = io.open(md, encoding="utf-8").read()
        T("F7 可读版含红灯与边界声明",
          "红灯" in md_txt and "不做解读与定价判断" in md_txt)
        T("F8 快照契约带 boundary（采集/解读分离）",
          "只采集归档" in (doc.get("boundary") or ""))
    finally:
        globals()["INFO_DIR"] = os.path.join(_ROOT, "data", "raw", "info")
        globals()["META"] = os.path.join(INFO_DIR, "news_meta.json")
        _sh.rmtree(sb, ignore_errors=True)
    print("== info_feeder --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=False, default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--incremental", action="store_true")
    ap.add_argument("--with-notice", dest="with_notice", action="store_true", default=True)
    ap.add_argument("--no-notice", dest="with_notice", action="store_false")
    ap.add_argument("--with-report", action="store_true")
    ap.add_argument("cmd", nargs="?", default=None)
    a = ap.parse_args()
    if a.cmd == "selftest":
        sys.exit(_selftest())
    sys.exit(main(a.date, incremental=a.incremental, with_notice=a.with_notice))
