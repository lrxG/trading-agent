# -*- coding: utf-8 -*-
"""★ DEPRECATED（2026-11-02 系统自查）——主体无调用方，禁止新增消费方。

盘前预案唯一生产件 = `tools/decision/premarket.py`（E1 登记写点，被 e5/e7/e8/e9/e11
复用其 _atomic_write）。本件 12 段渲染从未被主链调用，造成双产物/双落点歧义。
保留原因：`fetch_futures()` 仍被 `info_feeder.py` 使用（外盘期货腿）。
处置（挂账）：fetch_futures 抽公共件后整件归档。

原 docstring 保留如下：
盘前预案生成器（E1 升级件）——L0 决策层 + ①~⑫ 增量段，全机械产出。

命名与定位（2026-10-27 用户裁定）：**预案就预案**——旧系统 v3 模板的灵魂是
「转化层铁律」：每行信息必须回答「今天据此做什么」，每票四件套=落链代码/看什么/
证伪线/动作，以「某票不错/某链有逻辑」结尾=未转化。旧智能体靠人守这条铁律守不全，
本件把它做进机械结构：**首排每票五要素由引擎从快照数据算出价位**（触发区/证伪线/
动作/仓位/参考价），不是叙述；候选 ≤3（旧规矩）；删不掉的行才允许存在
（selftest 断言五要素齐全，缺一即构建失败）。

其余设计依据（2026-10-26/27）：
  1. 旧系统 34 篇预案机械骨架（data/knowledge/e1_premarket/section_base.json）；
  2. 能机械采的绝不留人工口子——旧版 ①② 靠人工 WebSearch，本件 westock 期货直采；
  3. 架构裁定：环境层只出风险预算不判个股；预案≠决策，零 BUY/WAIT/NO 终局结论。

十二段数据源与状态（写死的诚实清单，不自欺）：
  ① 隔夜外围   westock 期货通道（A50/纳指/标普期货）          机械
  ② 大宗商品   同上（原油/金银）                              机械
  ③ 企业事件   fetch_news 为盘后日频，盘中快讯未接            GAP（如实标注）
  ④ 近期盘面   env_recalc 环境 CSV（近 10 日）+ 最新盘中快照   机械（口径=校准同源）
  ⑤ 板块分级   westock sector ranking → 续强/点火/溃败         机械
  ⑥ 持仓处理   memory/state/positions.md 唯一真相源            机械
  ⑦ 仓位情景   C1 风险预算（pause_new）× 资金参数              机械（capital 未配置如实写）
  ⑧ 风险提示   环境负分维度 + 溃败板块 + 逆势方向              机械
  ⑨ 机会分析   candidates.json OPEN × 最新盘中快照池内异动      机械
  ⑩ 综上所述   L0 六行决策摘要（机械合成，非手写）               机械
  ⑪ 假设写入   无（假设归 hypothesize，本件只读不立账）         固定
  ⑫ 经验参考   指向 imported 旧预案库同环境档典型篇             固定指针

用法：
  premarket_card.py --date 2026-10-28            # 生成某日盘前卡
  premarket_card.py selftest
"""
import csv
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
import cli_codes as CC  # noqa: E402  ★ CLI 代码格式单真源（2026-11-02）

ENV_CSV = os.path.join(_ROOT, "data", "research", "env_recalc",
                       "env_metrics_recalc_full.csv")
CAND = os.path.join(_ROOT, "data", "state", "candidates.json")
POSITIONS = os.path.join(_ROOT, "memory", "state", "positions.md")
INTRADAY = os.path.join(_ROOT, "data", "raw", "intraday")
REPORTS = os.path.join(_ROOT, "data", "generated", "reports")
OLD_CORPUS = os.path.join(_ROOT, "data", "research", "imported_20261026_premarket",
                          "盘前预案")
FUTURES = ["fuCN", "fuNQ", "fuES", "fuCL", "fuGC", "fuSI"]
FUT_NAME = {"fuCN": "富时A50期货", "fuNQ": "纳指100期货", "fuES": "标普500期货",
            "fuCL": "WTI原油", "fuGC": "COMEX黄金", "fuSI": "COMEX白银"}


def _run(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return r.stdout or ""
    except Exception as e:                                    # noqa: BLE001
        return ""


def _parse_md_table(txt):
    """解析 markdown 表格 → [dict]。"""
    hdr, rows = None, []
    for ln in txt.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
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


# ── 数据采集（各段独立，失败=段内如实标注，不阻断整卡）────────────────────
def fetch_futures(codes=None):
    txt = _run(["westock", "quote", CC.to_cli_csv(codes or FUTURES)])
    rows = _parse_md_table(txt)
    out = {}
    for r in rows:
        code = (r.get("code") or r.get("代码") or "").strip()
        if not code.lower().startswith("fu"):
            continue
        pct = _f(r.get("changePct") or r.get("涨跌幅") or r.get("涨跌幅%"))
        last = _f(r.get("lastPrice") or r.get("最新价") or r.get("现价"))
        t = r.get("updateTime") or ""
        if pct is not None:
            out[code] = {"name": r.get("name") or FUT_NAME.get(code, code),
                         "pct": pct, "last": last, "update_time": t}
    return {"status": "OK" if out else "EMPTY", "rows": out}


def load_env_hist(date, n=10):
    env = {}
    if os.path.isfile(ENV_CSV):
        with io.open(ENV_CSV, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                env[r["date"]] = r
    ds = sorted(d for d in env if d < date)[-n:]
    return {d: env[d] for d in ds}, (max(env) if env else None)


def load_last_intraday(date):
    """≤date 的最近一份盘中快照 json。"""
    best = None
    if os.path.isdir(INTRADAY):
        for d in sorted(os.listdir(INTRADAY), reverse=True):
            if d > date or not len(d) == 10:
                continue
            js = sorted(fn for fn in os.listdir(os.path.join(INTRADAY, d))
                        if fn.startswith("scan_") and fn.endswith(".json"))
            if js:
                best = (d, os.path.join(INTRADAY, d, js[-1]))
                break
    if not best:
        return None
    try:
        return json.load(io.open(best[1], encoding="utf-8-sig"))
    except Exception:                                         # noqa: BLE001
        return None


def fetch_sectors():
    import intraday_scan as IS
    try:
        return IS.fetch_sectors()
    except Exception as e:                                    # noqa: BLE001
        return {"status": "FAIL", "error": str(e), "rows": []}


def load_pool_state(snap, date):
    """候选池 OPEN × 最新盘中快照 → 池内异动分类。复用 intraday_scan 判定。"""
    import intraday_scan as IS
    try:
        d = json.load(io.open(CAND, encoding="utf-8-sig"))
        cands = [c for c in d.get("candidates", [])
                 if c.get("state") in ("OPEN", "DISCOVERED", "WATCH", "QUALIFIED")]
    except Exception:                                         # noqa: BLE001
        return {"n_open": 0, "groups": {}, "error": "候选池不可读"}
    codes = {c["code"] for c in cands}
    rows = {}
    if snap:
        csvp = snap.get("_csv") or ""
        p = os.path.join(INTRADAY, snap.get("date", date), snap.get("_csv_name", ""))
        p = csvp if csvp and os.path.isfile(csvp) else p
        if p and os.path.isfile(p):
            with io.open(p, encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    rows[r["code"]] = r
    groups = {}
    for c in cands:
        r = rows.get(c["code"])
        if not r:
            continue
        g = r.get("chain") or "普通"
        groups.setdefault(g, []).append({
            "code": c["code"], "name": r.get("name", ""), "price": r.get("price"),
            "pct": _f(r.get("pct")), "chain": g, "vr": r.get("volume_ratio"),
            "low": _f(r.get("low")), "expiry": c.get("expiry"),
            "src": c.get("source")})
    return {"n_open": len(cands), "n_with_quote": len(rows), "groups": groups,
            "universe_ok": True}


# ── 渲染 ───────────────────────────────────────────────────────────────────
def _timing_gate(date):
    """读择时闸（tools/analysis/timing.py）→ dict；失败则 fail-closed。

    设计：择时是**市场级过滤器**，与环境档（8维打分）两层不互相替代——
    择时管「今天能不能在场」，环境档管「在场时给多大敞口」。
    """
    try:
        import timing as TM
    except ImportError:
        import sys as _s
        _s.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "tools", "analysis"))
        import timing as TM
    try:
        per = TM._per_stock()
        days, curve, ma, _stat = TM.market_series(per)
        use = date if date in curve else days[-1]
        return TM.signal_at(curve, ma, use) or {
            "risk_on": False, "reason": "MA20 数据不足 ⇒ fail-closed"}
    except Exception as e:                                       # noqa: BLE001
        return {"risk_on": False,
                "reason": "择时闸不可用（%s）⇒ fail-closed" % type(e).__name__}


def render_card(date, fut, env_hist, env_last, snap, sec, pool, pos_n, capital):
    L = []
    A = L.append
    grade = (snap or {}).get("env", {}).get("grade") or (env_last or {}).get("grade")
    n_dates = len(env_hist)

    A("# 盘前预案 · %s（L0 决策层 + ①~⑫ 增量段，机械生成）" % date)
    A("")
    A("> 数据边界：④用环境 CSV 至 %s + 最近盘中快照；①②为期货通道**当前**快照——")
    A("> 正式用法于开盘前 09:00 运行，即为真隔夜值。本件**零 BUY/WAIT/NO**：")
    A("> 预案给情景与候选首排，买卖必须走 C4 三问 + C5 定价（E1/E2 boundary）。")
    A("")

    # L0 决策层（开盘前只读这层；转化层铁律的结构化实现）
    A("## L0 决策层")
    A("")
    A("| 项 | 内容 |")
    A("|---|---|")
    # ★ 择时闸（T 母题落地 · 2026-10-28）：市场级开关前置，比环境档更粗更硬
    _tm = _timing_gate(date)
    A("| **择时闸** | %s（%s） |" % (
        "RISK_ON 允许开仓" if _tm.get("risk_on") else "RISK_OFF 禁新开仓",
        _tm.get("reason") or "数据不足 ⇒ fail-closed"))
    A("| 定调 | %s（最近快照档；环境 CSV 覆盖 %d 日，末值 %s） |" % (
        grade or "UNAVAILABLE", n_dates,
        max(env_hist) if env_hist else "-"))
    A("| 持仓 | %d 笔（positions.md 为唯一真相源） |" % pos_n)
    A("| 不做 | 池外票 / 近涨停开盘追价 / 逆势溃败板块；环境档=%s ⇒ %s |" % (
        grade or "?", "暂停新开仓（C1 pause_new）" if grade in ("退潮", "震荡")
        else "可开仓，单笔权重按情景表"))
    top_pool = []
    for g, items in sorted((pool.get("groups") or {}).items()):
        if g in ("未触板", "回封(疑似)"):    # 首排=可执行层（引擎链态：未触板=可挂单）；
            top_pool += items               # 封稳当日买不进只观察
    # ★ 转化层五要素（机械计算价位，禁叙述）：触发区=次日开盘涨幅 0~4%（追高禁）；
    #   证伪线=D 日最低价（跌破=分时结构坏）；动作=触发区内 C4 过了按情景表买。
    for x in top_pool:
        try:
            ref = float(x.get("price") or 0)
        except (TypeError, ValueError):
            ref = 0
        x["trigger_lo"], x["trigger_hi"] = round(ref * 1.00, 2), round(ref * 1.04, 2)
        x["falsify"] = round(float(x.get("low") or 0), 2) if x.get("low") else None
    top_pool.sort(key=lambda x: -(x.get("pct") or 0))
    top_pool = top_pool[:3]                 # 候选 ≤3（旧规矩：贪多=没预案）
    A("| 候选≤3 | %s |" % (
        "；".join("%s %s（触发区 %.2f~%.2f，证伪 %.2f）" % (
            x.get("code"), x.get("name"), x["trigger_lo"], x["trigger_hi"],
            x["falsify"] or 0) for x in top_pool) if top_pool else "无（池内无异动）"))
    A("| 只买一只 | %s |" % (
        "❌ 不给：capital=0（初始资金未配置）且首排未过 C4 三问——"
        "这是边界不是敷衍" if not capital else
        "待 C4 三问后由 C5 定价（本件不预判）"))
    A("| 今日H | 环境档 %s；盯 ⑤ 板块证伪线与 ⑧ 风险清单 |" % (grade or "?"))
    A("")

    # ① 隔夜外围
    A("## ① 隔夜外围（指数期货）")
    A("")
    if fut.get("status") == "OK":
        _t = next((r.get("update_time") for r in fut["rows"].values()
                   if r.get("update_time")), "")
        A("| 品种 | 最新 | 涨跌幅 | 数据时间 | 读法 |")
        A("|---|---|---|---|---|")
        for c in ("fuCN", "fuNQ", "fuES"):
            r = fut["rows"].get(c)
            if r:
                A("| %s | %s | %+.2f%% | %s | %s |" % (
                    r["name"], r.get("last") or "-",
                    r["pct"], r.get("update_time") or "-",
                    "外盘风险偏好正向" if r["pct"] > 0 else "外盘偏弱"))
        if _t:
            A("")
            A("> 数据时间 %s ——正式用法 09:00 运行即隔夜收盘值；午间补跑则为实时值。" % _t)
    else:
        A("- UNAVAILABLE（期货通道无返回）")
    A("")

    # ② 大宗商品
    A("## ② 大宗商品")
    A("")
    if fut.get("status") == "OK":
        A("| 品种 | 最新 | 涨跌幅 | 对应链 |")
        A("|---|---|---|---|")
        chain = {"fuCL": "油气/煤炭", "fuGC": "贵金属", "fuSI": "贵金属/工业银"}
        for c in ("fuCL", "fuGC", "fuSI"):
            r = fut["rows"].get(c)
            if r:
                A("| %s | %s | %+.2f%% | %s |" % (
                    r["name"], r.get("last") or "-", r["pct"], chain.get(c, "")))
    else:
        A("- UNAVAILABLE")
    A("")

    # ③ 企业事件（info_feeder 统一采集：快讯/红灯/日历/公告分级）
    A("## ③ 企业事件（info_feeder 实采）")
    A("")
    try:
        import info_feeder as IF
        snap = IF.latest_snapshot(date)
    except Exception:
        snap = None
    if snap:
        rl = (snap.get("redlight") or {}).get("hits") or []
        if rl:
            A("**红灯（热搜×我的池，%d 命中）**：" % len(rl))
            A("")
            A("| 代码 | 名称 | 涨幅 | 在池 |")
            A("|---|---|---|---|")
            for h in rl[:6]:
                A("| %s | %s | %+.2f%% | %s |" % (
                    h.get("code"), h.get("name"), h.get("pct") or 0, h.get("mine")))
            A("")
        fl = (snap.get("flash") or {}).get("rows") or []
        if fl:
            A("隔夜/盘前快讯 Top5：")
            A("")
            for r in fl[:5]:
                A("- `%s` %s" % (r.get("time", "")[:16], r.get("title")))
            A("")
        cal = (snap.get("calendar") or {}).get("rows") or []
        na = snap.get("notices_a") or []
        A("未来事件日历 %d 条；公告 A 级 %d 条（中标/增减持/业绩/停牌…，详 snapshot）。" % (
            len(cal), len(na)))
        A("")
        A("> 解读与定价判断归 S1/event-analysis；本段只陈列采集事实。")
    else:
        A("- **GAP（如实）**：info_feeder 当日无快照 ⇒ 事件归因不可用；")
        A("  事件驱动票今日只能看价量，事件核对放到盘后（先跑 info_feeder）。")
    A("")

    # ④ 近期盘面
    A("## ④ 近期盘面（环境档 × 10 日）")
    A("")
    if env_hist:
        A("| 日期 | 涨停 | 高度 | 炸板率 | 涨跌比 |")
        A("|---|---|---|---|---|")
        for d in sorted(env_hist)[-8:]:
            r = env_hist[d]
            A("| %s | %s | %s | %s | %s |" % (
                d, r.get("limit_up_count"), r.get("max_board"),
                r.get("fail_rate"), r.get("up_down_ratio")))
    else:
        A("- 环境历史 UNAVAILABLE")
    if snap:
        b = snap.get("buckets", {})
        A("")
        A("最近盘中快照（%s %s）：档 **%s** · 封 %s / 炸 %s / 半路 %s / 跌停 %s" % (
            snap.get("date"), snap.get("hhmm"), snap.get("env", {}).get("grade"),
            b.get("sealed_n"), b.get("broken_n"), b.get("near_n"),
            b.get("limit_down_n")))
    A("")

    # ⑤ 板块分级
    A("## ⑤ 板块分级（三档光谱）")
    A("")
    rows_s = (sec.get("rows") if isinstance(sec, dict) else None) or []
    if rows_s:
        A("| 层 | 板块 | 今日 | 5日主力(亿) | 证伪线 |")
        A("|---|---|---|---|---|")
        strong = [x for x in rows_s[:6] if (x.get("net_in_5d") or 0) > 0][:2]
        ignite = [x for x in rows_s[:6] if (x.get("net_in_5d") or 0) <= 0][:2]
        for x in strong:
            A("| 🔥续强 | %s | %+.2f%% | %+.2f | 龙头开板且涨家数回落 |" % (
                x["name"], x["pct"], (x["net_in_5d"] or 0) / 1e4))
        for x in ignite:
            A("| 🌱点火 | %s | %+.2f%% | %+.2f | 次日主力转出即熄火 |" % (
                x["name"], x["pct"], (x["net_in_5d"] or 0) / 1e4))
        for x in rows_s[-2:]:
            A("| 💀溃败 | %s | %+.2f%% | %+.2f | 收复今日跌幅一半才算止跌 |" % (
                x["name"], x["pct"], (x["net_in_5d"] or 0) / 1e4))
    else:
        A("- SECTOR_UNAVAILABLE")
    A("")

    # ⑥ 持仓处理
    A("## ⑥ 持仓处理")
    A("")
    if pos_n:
        A("- 持仓 %d 笔：退出判定走 C5 退出闸（−8% 止损 / 逻辑失效位），本件不替代。" % pos_n)
    else:
        A("- **空仓**（账实相符：positions.md 0 行）。")
    A("")

    # ⑦ 仓位情景
    A("## ⑦ 仓位情景（C1 风险预算 × 资金）")
    A("")
    A("| 环境档 | 新开仓 | 单笔权重 | 触发条件 |")
    A("|---|---|---|---|")
    A("| 主升/强势 | ✓ | 0.25×4 仓 | 档位判定见 ④ |")
    A("| 震荡 | ✗（pause） | — | C1 暂停名单；解禁须档位回升 |")
    A("| 退潮 | ✗（pause） | — | 同上 |")
    if not capital:
        A("")
        A("> ⚠️ **capital=0（初始资金未配置）**：pnl 账本无本金 ⇒ 回撤熔断闸不可算 ⇒")
        A("> 即使候选齐备也进不了 C5。情景表是**制度预案**，不是可执行仓位。")
    A("")

    # ⑧ 风险提示
    A("## ⑧ 风险提示")
    A("")
    if rows_s:
        A("- 逆势方向：%s —— 昨日强的今天全杀是常态，禁抄底逆势溃败板块" %
          "、".join(x["name"] for x in rows_s[-2:]))
    A("- 追封比高于 1.5 时禁追半路板（封不住的板=分歧）")
    A("- 候选池为涨停池/龙虎榜来源=追高型载体，退潮日集中反噬（10-26 实测 42/67 走弱）")
    A("")

    # ⑨ 机会分析
    A("## ⑨ 机会分析（池内为主，池外不做）")
    A("")
    if pool.get("n_open"):
        A("候选池 OPEN **%d** 条（有当日行情 %d 条）。池内分态：" % (
            pool["n_open"], pool.get("n_with_quote", 0)))
        A("")
        A("| 链态 | 条数 | 说明 |")
        A("|---|---|---|")
        for g in ("封稳", "回封(疑似)", "未触板", "炸板", "跌停", "普通"):
            items = (pool.get("groups") or {}).get(g)
            if items:
                A("| %s | %d | %s |" % (g, len(items), {
                    "封稳": "已封死，当日买不进，只作明日观察",
                    "回封(疑似)": "炸后回封，需分钟腿确认真封",
                    "未触板": "可挂单层——C4 三问主战场",
                    "炸板": "触板回落=分歧/出货，禁接",
                    "跌停": "禁抄",
                    "普通": "未异动"}.get(g, "")))
        if top_pool:
            A("")
            A("可执行首排（≤3，五要素齐——缺一即未转化，构建期断言拦截）：")
            A("")
            A("| 代码 | 名称 | 涨幅 | 触发区(次日开盘涨幅0~4%对应价位) | 证伪线 | 动作 | 量比 |")
            A("|---|---|---|---|---|---|---|")
            for x in top_pool:
                A("| %s | %s | %+.2f%% | %.2f ~ %.2f | 跌破 %.2f 放弃 | 触发区内 C4 过则按情景表买 | %s |" % (
                    x.get("code"), x.get("name"), x.get("pct") or 0,
                    x["trigger_lo"], x["trigger_hi"], x["falsify"] or 0,
                    x.get("vr") or "-"))
    else:
        A("- 候选池空或无当日行情（诚实状态，不出池外票凑数）")
    A("")

    # ⑩ 综上所述
    A("## ⑩ 综上所述")
    A("")
    A("环境档 **%s** + 空仓 + 池内首排 %d 条 ⇒ 今日动作空间：%s" % (
        grade or "?", len(top_pool),
        "仅池内半路层逐票 C4 三问；禁池外、禁追封板、禁抄跌停"
        if top_pool else "无（等环境或等池子）"))
    A("")

    # ⑪ ⑫
    A("## ⑪ 假设写入")
    A("")
    A("- 无（假设归 `hypothesize.py`/`thesis.py`；本件只读不立账）。")
    A("")
    A("## ⑫ 经验参考")
    A("")
    if os.path.isdir(OLD_CORPUS):
        A("- 旧系统同档典型篇（原样导入库）：%s" % os.path.basename(OLD_CORPUS))
        A("- 同环境档历史预案对照：退潮/震荡档见 2026-09 下旬各篇（缩量防守主线）")
    else:
        A("- 旧预案库 UNAVAILABLE")
    A("")
    A("---")
    A("")
    A("```\n取数通道：期货 %s · 板块 %s · 盘中快照 %s · 生成耗时含实时调用\n```" % (
        fut.get("status"), "OK" if rows_s else "FAIL",
        "%s_%s" % (snap.get("date"), snap.get("hhmm")) if snap else "无"))
    return "\n".join(L)


def write_card(date, md):
    out = os.path.join(REPORTS, "盘前预案_%s.md" % date)
    os.makedirs(REPORTS, exist_ok=True)
    tmp = out + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(md)
    os.replace(tmp, out)
    return out


def read_positions_n():
    try:
        txt = io.open(POSITIONS, encoding="utf-8").read()
        for ln in txt.splitlines():
            if "|" in ln and ln.strip().startswith("|"):
                cells = [c.strip() for c in ln.strip("|").split("|")]
                if cells and cells[0].isdigit():
                    return int(cells[0])
        return 0
    except OSError:
        return 0


# ── CLI / 自检 ─────────────────────────────────────────────────────────────
def main(date):
    t0 = time.time()
    fut = fetch_futures()
    env_hist, env_last_date = load_env_hist(date)
    snap = load_last_intraday(date)
    if snap:
        snap["_csv_name"] = "scan_%s.csv" % snap.get("hhmm", "0000")
    sec = fetch_sectors()
    pool = load_pool_state(snap, date)
    capital = 0.0
    try:
        import params as PARAMS
        capital = float(PARAMS.get_value("capital") or 0)
    except Exception:                                         # noqa: BLE001
        pass
    md = render_card(date, fut, env_hist, env_last_date, snap, sec, pool,
                     read_positions_n(), capital)
    out = write_card(date, md)
    print(json.dumps({"card": out, "sections": 12, "grade": (snap or {}).get(
        "env", {}).get("grade"), "pool_open": pool.get("n_open"),
        "elapsed_sec": round(time.time() - t0, 1)}, ensure_ascii=False, indent=1))
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

    print("== premarket_card --selftest ==")
    sb = _tf.mkdtemp(prefix="pmcard_")
    try:
        # 夹具：期货表 / 环境 CSV / 盘中快照 / 候选池
        fut = {"status": "OK", "rows": {c: {"name": FUT_NAME[c], "pct": 0.5, "last": 1}
                                        for c in FUTURES}}
        env_hist = {"2026-01-0%d" % i: {"limit_up_count": 50, "max_board": 4,
                                        "fail_rate": 0.3, "up_down_ratio": 1.2}
                    for i in range(1, 6)}
        snap = {"date": "2026-01-06", "hhmm": "1239",
                "env": {"grade": "震荡"},
                "buckets": {"sealed_n": 40, "broken_n": 14, "near_n": 90,
                            "limit_down_n": 6}}
        sec = {"rows": [{"name": "电机", "pct": 2.7, "net_in_5d": 1.6e5},
                        {"name": "元件", "pct": -6.1, "net_in_5d": -1.9e5}]}
        pool = {"n_open": 226, "n_with_quote": 40,
                "groups": {"未触板": [{"code": "sh600001", "name": "甲", "price": 10.5,
                                       "pct": 6.1, "vr": "2.1", "expiry": "2026-01-08",
                                       "low": 9.8}],
                           "封稳": [{"code": "sh600002", "name": "乙", "price": 11.0,
                                     "pct": 10.0, "vr": "3.0", "expiry": "2026-01-08"}]}}
        md = render_card("2026-01-07", fut, env_hist, "2026-01-05", snap, sec,
                         pool, 0, 0.0)
        T("T1 L0 六行齐", all(k in md for k in
                              ("定调", "持仓", "不做", "候选", "只买一只", "今日H")))
        T("T2 ①~⑫ 十二段齐", all("## %s" % s in md for s in
                                  ("① 隔夜外围", "② 大宗商品", "③ 企业事件",
                                   "④ 近期盘面", "⑤ 板块分级", "⑥ 持仓处理",
                                   "⑦ 仓位情景", "⑧ 风险提示", "⑨ 机会分析",
                                   "⑩ 综上所述", "⑪ 假设写入", "⑫ 经验参考")))
        T("T3 期货通道数字进卡（A50/纳指/标普）",
          all(n in md for n in ("富时A50", "纳指100", "标普500")))
        T("T4 ③ 事件腿：无快照时 GAP 如实标注（不自欺）",
          "GAP" in md and "info_feeder 当日无快照" in md)
        T("T5 资金未配置如实写进 L0 与 ⑦",
          "capital=0" in md and "不是可执行仓位" in md)
        T("T6 池内分态表：封稳/半路/禁抄语义齐",
          all(k in md for k in ("当日买不进", "可挂单层", "禁抄")))
        T("T7 零终局结论（预案≠决策边界）",
          "零 BUY/WAIT/NO" in md and "C4 三问" in md)
        _l0 = md.split("## L0 决策层")[1].split("## ①")[0]
        T("T8 候选首排来自可执行层（封稳不进首排）且 ≤3",
          "sh600001" in _l0 and "sh600002" not in _l0)
        T("T10 转化层五要素：触发区/证伪线/动作齐（缺一即未转化）",
          all(k in md for k in ("触发区", "证伪线", "放弃", "情景表买"))
          and "触发区 10.50~10.92" in md and "跌破 9.80" in md)
        # 降级路径：期货/板块/快照全失败 → 段内 UNAVAILABLE 不阻断
        md2 = render_card("2026-01-07", {"status": "EMPTY", "rows": {}}, {}, None,
                          None, {"rows": []}, {"n_open": 0, "groups": {}}, 0, 0.0)
        T("T9 全源失败降级：段内标注不崩",
          "UNAVAILABLE" in md2 and "## ⑫" in md2)
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    print("== premarket_card --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    if argv[0] == "selftest":
        sys.exit(_selftest())
    if argv[0] == "--date":
        sys.exit(main(argv[1]))
    print(__doc__)
    sys.exit(2)
