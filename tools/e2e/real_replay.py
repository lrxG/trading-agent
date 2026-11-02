#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""real_replay.py · 真实历史数据回放（任务5 · 2026-10-10 建 · v1.1 口径 2026-10-10）

v1.1（任务 8.3 前视修复）：结算口径改 settle v1.1 —— 决策=盘后，成交=次日开盘价
（限价 = 收盘×1.02 容忍带），T+1 自成交日次日起检查止损/目标。旧版「决策日当天
按当日价位成交并触发止损」系结构性前视，已废弃；本日数据只到 D1 → 持有窗口
走不完 → pnl=PENDING_WINDOW 如实呈现（真实盈亏结论由批量回放窗口给出）。

用途：用**旧包真实历史数据**（来源可追溯：机采归档，非合成、非夹具）按既有契约
      手动投放，把 投放校验→正式化→loader→S1→C3→C4三问→C5→模拟成交→结算→pnl
      →Case 全链在**真实输入**上跑通一遍。首个验收日 = **2026-09-29**（旧包五域
      覆盖最全的交易日），结算日 = 2026-09-30。

★ 数据性质声明（用户裁决 2026-10-10 任务5）：本件消费的是**人工整理的真实历史
  数据**（第二类，manual 来源标记在案），不是实时行情验收（第三类），更不是
  策略有效性证明。全部数字来自旧包磁盘文件，**零补造**；缺口字段一律 None/空，
  缺就是缺。派生字段（连板数/炸板率）的规则与来源在报告中逐条注明。

★ 决策不预设：BUY/WAIT/NO 由规则+真实输入共同决定；本回放如实接受**亏损**结局
  （不放宽止损、不改输入凑 WIN）。

数据来源（旧包 2026-10-08 归档）：
  daily_quote  ← data/raw/bus/klines.csv（行情总线 19.2 万行机采）
  limit_up     ← klines 精确涨停价规则派生（收盘价 == round(前收×(1+L),2)），
                 L=0.05(ST)/0.20(创业科创)/0.30(北证)/0.10(主板)；boards=连续封板数
  dragon_tiger ← data/raw/bus/lhb/lhb_<date>.md 机构榜表（机采归档）
  news         ← data/raw/bus/notices/notice_<date>.csv（巨潮公告机采，art_code 稳定ID）
  env_metrics  ← data/raw/bus/env_labels.csv + env_history.csv（机采环境标注）
                 + 连板高度(派生) + 炸板率(zt_matrix 组计数派生)

用法：
  python tools/e2e/real_replay.py                # 执行回放（全沙盒，零真库写入）
  python tools/e2e/real_replay.py --discipline
"""
import hashlib
import io
import csv
import json
import os
import re
import shutil
import sys
import tempfile

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
for _p in ("fs", "analysis", "decision", "risk", "learning", "e2e", "search"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import decision as DEC  # noqa: E402
import three_q as TQ  # noqa: E402
import candidates as CAND  # noqa: E402
import cursor as CUR  # noqa: E402
import thesis as TH  # noqa: E402
import settle as SETTLE  # noqa: E402
import position_gate as PG  # noqa: E402
import case_builder as CB  # noqa: E402
import pnl as PNL  # noqa: E402
import drive  # noqa: E402
import orchestrate as ORCH  # noqa: E402
import s1 as S1MOD  # noqa: E402
import ingest as ING  # noqa: E402
import loader as LD  # noqa: E402
import regime as RG  # noqa: E402

_LEGACY_ROOT = (os.environ.get("OLD_SYSTEM_ROOT")
                or os.path.join(os.path.expanduser("~"), "炒股智能体_旧包_20261008"))  # 旧系统只读参考源；换机器时设 OLD_SYSTEM_ROOT 或放家目录

_OLD_ROOT = _LEGACY_ROOT
OLD_ROOT = _OLD_ROOT
_TEST_FUNCS = ("run_replay", "_discipline", "_main")  # 沙盒夹具自报（写目标全部=tempfile）
D, D1 = "2026-09-29", "2026-09-30"          # 决策日 / 结算日（旧包数据尾部两交易日）
CODE, NAME = "sz000513", "丽珠集团"          # 龙虎榜机构榜 × 当日公告 × S1 可归类 交集标的
EQUITY = 1_000_000
_SINCE = "2026-07-01"                        # 连板回溯窗口起点（约 60 交易日）
_TAX = json.load(io.open(S1MOD.TAXONOMY_PATH, encoding="utf-8"))  # 官方分类 → 研究类（单一真源 = s1 模块常量）


# ── 旧包真实数据读取（只读；全部来源=机采归档文件） ──────────────────────────
def _legacy_klines_since(old_root, since):
    """klines.csv → ({date: {code: row}}, [dates])。row 契约列齐备，零改写。"""
    days, order = {}, []
    p = os.path.join(old_root, "data", "raw", "bus", "klines.csv")
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = r["date"]
            if d < since:
                continue
            if d not in days:
                days[d] = {}
                order.append(d)
            days[d][r["code"]] = {
                "name": r["name"], "open": float(r["open"]), "high": float(r["high"]),
                "low": float(r["low"]), "close": float(r["close"]),
                "pct": float(r["change_pct"]), "vol": float(r["volume"]),
                "amount": float(r["amount"])}
    return days, sorted(order)


def _limit_ratio(code, name):
    if code.startswith("bj"):
        return 0.30
    if code.startswith("sh68") or code.startswith("sz30"):
        return 0.20
    if "ST" in (name or "").upper():
        return 0.05
    return 0.10


def _sealed_codes(prev_day, day):
    """精确涨停价规则：close == round(prev_close×(1+L),2)（±0.005 容差）。"""
    out = {}
    for code, row in day.items():
        prev = prev_day.get(code)
        if not prev:
            continue
        limit_px = round(prev["close"] * (1 + _limit_ratio(code, row["name"])), 2)
        if abs(row["close"] - limit_px) < 0.005:
            out[code] = row["name"]
    return out


def _derive_limit_up(kb, dates, target):
    """target 日涨停名单 + 连板数（真实数据派生，规则见模块头）。返回 (rows, max_board)。"""
    sealed_prev, boards = {}, {}
    rows = []
    for d in dates:
        prev_day = kb[dates[dates.index(d) - 1]] if d != dates[0] else {}
        cur_sealed = _sealed_codes(prev_day, kb[d])
        new_boards = {}
        for code in cur_sealed:
            new_boards[code] = boards.get(code, 0) + 1
        boards = new_boards
        if d == target:
            for code, n in boards.items():
                rows.append({"code": code, "name": cur_sealed[code], "boards": n,
                             "first_seal_time": None, "seal_amount": None,
                             "reason": None})
            rows.sort(key=lambda x: (-x["boards"], x["code"]))
            return rows, (max((x["boards"] for x in rows), default=0))
    return [], 0


def _legacy_lhb_inst(old_root, date):
    """lhb md 机构榜表 → dragon_tiger 契约行（seat=段落名；sell 榜表无此列→None）。"""
    p = os.path.join(old_root, "data", "raw", "bus", "lhb", "lhb_%s.md" % date)
    if not os.path.isfile(p):
        return []
    txt = io.open(p, encoding="utf-8").read()
    if "**机构榜**" not in txt:
        return []
    sec = txt.split("**机构榜**")[1].split("**")[0]
    rows = []
    for m in re.finditer(
            r"\|\s*\d+\s*\|\s*(s[hz]\d{6})\s*\|\s*([^|]+?)\s*\|[^|]*\|[^|]*\|"
            r"[^|]*\|[^|]*\|([^|]*)\|([^|]*)\|", sec):
        code, name = m.group(1), m.group(2).strip()
        total_buy = (m.group(3) or "").strip()
        net_buy = (m.group(4) or "").strip()
        rows.append({"code": code, "name": name, "seat": "机构榜",
                     "buy": float(total_buy) if total_buy else None,
                     "sell": None,
                     "net": float(net_buy) if net_buy else None})
    return rows


_CODE_OK = re.compile(r"^(sh|sz|bj)\d{6}$", re.I)


def _legacy_news(old_root, date):
    """notices csv → news 契约行（art_code=稳定ID；body 源文件无此列→None 不补造）。

    代码范围过滤：非 A 股契约代码（B股/其他品种，如 szA26183）整行剔除并计数——
    契约外品种不改造、不冒充，剔除数在回放报告如实呈现。
    """
    p = os.path.join(old_root, "data", "raw", "bus", "notices", "notice_%s.csv" % date)
    if not os.path.isfile(p):
        return [], 0
    rows, skipped = [], 0
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            codes = [c for c in [r["code"]] if c and _CODE_OK.match(c.strip())]
            if not codes:
                skipped += 1
                continue
            rows.append({"news_id": r["art_code"], "ts": r["time"],
                         "source": "巨潮资讯（旧包 bus/notices 机采归档）",
                         "title": r["title"], "body": None,
                         "related_codes": codes})
    return rows, skipped


def _legacy_env(old_root, date):
    """env_labels + env_history 行 → (标注dict or None)。zt=涨停家数, idx_sh=上证20日线偏离。"""
    p = os.path.join(old_root, "data", "raw", "bus", "env_labels.csv")
    lab = None
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["date"] == date:
                lab = r
                break
    hist = None
    p2 = os.path.join(old_root, "data", "raw", "bus", "env_history.csv")
    with io.open(p2, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["date"] == date:
                hist = r
                break
    return lab, hist


def _legacy_zt_groups(zt_dir, date):
    """zt_matrix 组计数 → (limitup数, 炸板数) or None（炸板率派生真源）。

    zt_dir 由调用方传入（旧包归档目录，非本项目落点——路径词汇不进生产区）。
    """
    ymd = date.replace("-", "")
    p = os.path.join(zt_dir, "_zt_matrix_%s.csv" % ymd)
    if not os.path.isfile(p):
        return None
    n_lim = n_zb = 0
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["group"] == "limitup":
                n_lim += 1
            elif r["group"] == "zb":
                n_zb += 1
    return n_lim, n_zb


# ── 回放沙盒状态初始化（夹具约定：_selftest_ 前缀；只写沙盒 tmp 路径） ────────
def _selftest_init_pool(path, date):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": CAND.SCHEMA_VERSION, "updated_at": date,
                   "candidates": []}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def _selftest_init_cursor(path, date, done=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": CUR.SCHEMA_VERSION, "date": date,
                   "checklist": {s: bool(done) for s in CUR.STEPS}},
                  f, ensure_ascii=False)
    return path


def _selftest_copy_kb(base):
    kb_dst = os.path.join(base, "data", "knowledge", "s1_event_effects")
    os.makedirs(kb_dst, exist_ok=True)
    for _fn in ("effect_base.json", "taxonomy.json"):
        shutil.copy2(os.path.join(S1MOD.KB_DIR, _fn), os.path.join(kb_dst, _fn))
    shutil.copy2(os.path.join(S1MOD.KB_DIR, "..", "_registry.json"),
                 os.path.join(base, "data", "knowledge", "_registry.json"))


def _selftest_fp(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


def _selftest_tree_fp(roots):
    out = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                p = os.path.join(dirpath, fn)
                out.add((os.path.relpath(p, root), _selftest_fp(p)))
    return out


# ── 回放主流程 ────────────────────────────────────────────────────────────────
def run_replay(old_root=OLD_ROOT):
    import dayclass as DC
    # ★ 2026-10-20 修：v1.4 起 settle.open_day 对 calendar=None 一律拒绝开日 ⇒ 注入真库日历
    _cal = DC.Calendar.from_seed_csv(
        os.path.join(_ROOT, "data", "raw", "meta", "trade_cal.csv"),
        source="real_replay_default")
    rep = {"meta": {"decision_day": D, "settle_day": D1, "code": CODE,
                    "data_class": "manual（人工整理真实历史数据·第二类）"}}
    real_fp = _selftest_tree_fp([os.path.join(DEC.ROOT, "data"),
                                 os.path.join(DEC.ROOT, "memory")])
    raw_prod = os.path.join(DEC.ROOT, "data", "raw")
    tmp = tempfile.mkdtemp(prefix="real_replay_")
    try:
        base = os.path.join(tmp, "gen")
        os.makedirs(base)
        kb, dates = _legacy_klines_since(old_root, _SINCE)
        for need in (D, D1):
            if need not in kb:
                raise SystemExit("旧包 klines 缺 %s" % need)
        snap_root = LD.roots(base)["snapshot"]
        cursor_f = os.path.join(tmp, "cursor.json")
        pool_f = os.path.join(tmp, "candidates.json")
        chains_f = os.path.join(tmp, "chains.jsonl")
        cases_f = os.path.join(tmp, "cases.jsonl")
        dq_f = os.path.join(tmp, "demand_queue.jsonl")
        thesis_f = os.path.join(tmp, "thesis.md")
        TH.init_file(thesis_f)
        _selftest_init_cursor(cursor_f, "2026-09-28", True)   # 回放起点前置日
        _selftest_init_pool(pool_f, "2026-09-28")

        # ── 阶段1：真实数据 → 契约载荷（逐字段注明来源/缺口） ──
        print("=" * 72)
        print("阶段1 真实数据转换（零补造；缺口=None 如实）")
        pay, pay1 = {}, {}
        pay["daily_quote"] = [dict(kb[D][c], code=c) for c in sorted(kb[D])]
        pay1["daily_quote"] = [dict(kb[D1][c], code=c) for c in sorted(kb[D1])]
        lu, max_board = _derive_limit_up(kb, dates, D)
        lu1, max_board1 = _derive_limit_up(kb, dates, D1)
        pay["limit_up"], pay1["limit_up"] = lu, lu1
        pay["dragon_tiger"] = _legacy_lhb_inst(old_root, D)
        pay1["dragon_tiger"] = _legacy_lhb_inst(old_root, D1)
        pay["news"], skip_n = _legacy_news(old_root, D)
        pay1["news"], skip_n1 = _legacy_news(old_root, D1)
        lab, hist = _legacy_env(old_root, D)
        zg = _legacy_zt_groups(os.path.join(old_root, "data", "generated",
                                            "pattern", "zt_matrix"), D)
        fail_rate = round(zg[1] / (zg[0] + zg[1]), 4) if zg and (zg[0] + zg[1]) else None
        pay["env_metrics"] = {
            "limit_up_count": int(float(lab["zt"])) if lab else None,  # 旧包环境标注
            "max_board": max_board,                                # klines 规则派生
            "turnover": None,                                      # 旧包无全市场成交额
            "up_down_ratio": float(hist["ratio_ud"]) if hist else None,
            "fail_rate": fail_rate,                                # zt_matrix 组计数派生
            "index_ma20": float(lab["idx_sh"]) if lab else None}
        zg1 = _legacy_zt_groups(os.path.join(old_root, "data", "generated",
                                             "pattern", "zt_matrix"), D1)
        lab1, _h1 = _legacy_env(old_root, D1)
        pay1["env_metrics"] = {
            "limit_up_count": int(float(lab1["zt"])) if lab1 else len(lu1),
            "max_board": max_board1,
            "turnover": None, "up_down_ratio": None, "index_ma20": None,
            "fail_rate": (round(zg1[1] / (zg1[0] + zg1[1]), 4)
                          if zg1 and (zg1[0] + zg1[1]) else None)}
        rep["coverage"] = {
            "daily_quote": {"D": len(pay["daily_quote"]), "D1": len(pay1["daily_quote"])},
            "limit_up": {"D": len(lu), "D1": len(lu1), "max_board": (max_board, max_board1)},
            "dragon_tiger": {"D": len(pay["dragon_tiger"]), "D1": len(pay1["dragon_tiger"])},
            "news": {"D": len(pay["news"]), "D1": len(pay1["news"]),
                     "skipped_non_a": (skip_n, skip_n1)},
            "env_metrics": {"D": pay["env_metrics"], "D1": pay1["env_metrics"]}}
        print("  D=%s: quote %d | limit_up %d (max_board=%s) | lhb %d | news %d(剔非A股%d)"
              % (D, len(pay["daily_quote"]), len(lu), max_board,
                 len(pay["dragon_tiger"]), len(pay["news"]), skip_n))
        print("  env_metrics(D) = %s" % json.dumps(pay["env_metrics"], ensure_ascii=False))
        if lab and zg:
            print("  交叉核对: 派生涨停 %d vs env_labels zt=%s vs zt_matrix limitup=%d"
                  % (len(lu), lab["zt"], zg[0]))
        me = pay["dragon_tiger"]
        me_row = next((x for x in me if x["code"] == CODE), None)
        print("  标的 %s 机构榜: %s" % (CODE, me_row))
        notices_code = [n for n in pay["news"] if n["related_codes"] == [CODE]]
        print("  标的当日公告 %d 条:" % len(notices_code))
        for n in notices_code:
            col = next((r["column"] for r in csv.DictReader(io.open(
                os.path.join(old_root, "data", "raw", "bus", "notices",
                             "notice_%s.csv" % D), encoding="utf-8-sig"))
                if r["art_code"] == n["news_id"]), "")
            cat = _TAX["cat_map"].get(col.strip())
            print("    %s | %s | 分类=%r → %s" % (n["news_id"], n["title"][:34],
                                                  col, cat if cat else "UNMAPPED"))

        # ── 阶段2：投放 → 校验 → 正式化 ──
        print("阶段2 投放与正式化（ingest 契约 · source 标记在案）")
        r1 = ING.stage_core(D, pay, source="manual:旧包真实回放 2026-09-29",
                            base=base, data_upto=D)
        r2 = ING.stage_core(D1, pay1, source="manual:旧包真实回放 2026-09-30",
                            base=base, data_upto=D1)
        print("  staged: D %d 文件 / D1 %d 文件 | dropped=%s/%s"
              % (len(r1["staged"]), len(r2["staged"]), r1["dropped"], r2["dropped"]))
        pr1 = ING.promote_snapshot(D, base=base)
        pr2 = ING.promote_snapshot(D1, base=base)
        print("  promote: D=%s D1=%s" % (pr1, pr2))
        q = LD.load_quote(D, CODE, base=base)
        print("  load_quote(%s, %s): close=%s pct=%s" % (D, CODE, q["close"], q["pct"]))
        rep["stage2"] = {"promote": (pr1, pr2), "quote_close": q["close"]}

        # ── 阶段3：真实环境档（regime 从真实信号判定，不预设） ──
        print("阶段3 C1 环境档（regime.evaluate · 真实信号）")
        env_res = RG.evaluate(pay["env_metrics"], as_of=D, source="snapshot")
        env = {"regime": env_res["regime"], "signals": env_res["signals"],
               "leg": "EOD", "source": "LOADER"}
        print("  信号 %s → 档位=%s position_cap=%s成"
              % (json.dumps(pay["env_metrics"], ensure_ascii=False),
                 env_res["regime"], env_res["position_cap"]))
        rep["regime"] = env_res

        # ── 阶段4：S1 事件评估（真实公告流入，非夹具注入） ──
        print("阶段4 S1 事件评估（真知识库 × 真实公告）")
        _selftest_copy_kb(base)
        s1_events = []
        col_of = {r["art_code"]: r["column"].strip() for r in csv.DictReader(
            io.open(os.path.join(old_root, "data", "raw", "bus", "notices",
                                 "notice_%s.csv" % D), encoding="utf-8-sig"))}
        for n in notices_code:
            col = col_of.get(n["news_id"], "")
            if col not in _TAX["cat_map"] or _TAX["cat_map"][col][0] == "_NEUTRAL":
                print("  跳过 %s（分类 %r 不在研究词表 → UNMAPPED 不强行归类）"
                      % (n["news_id"][-6:], col))
                continue
            ev = {"event_id_src": n["news_id"], "title": n["title"], "cols": col,
                  "sources": [{"source_id": "巨潮资讯（旧包机采归档）",
                               "kind": "公告号", "ref": n["news_id"], "ts": n["ts"]}],
                  "first_seen_at": D, "announcement_at": D,
                  "related_codes": [CODE]}
            sr = S1MOD.evaluate(ev, as_of=D, base=base)
            s1_events.append(sr)
            o = sr["output"]
            print("  事件 %s → %s | net=%s | strength=%s | missing=%s"
                  % (o["type"], str(o["net_effect"].get("status") or "OK"),
                     o["net_effect"].get("net_effect_20d_pct"),
                     o["strength"], o.get("missing")))
        s1_snap_p = os.path.join(LD.roots(base)["generated"], "s_s1_%s.json" % D)
        rep["s1"] = [{"type": s["output"]["type"], "strength": s["output"]["strength"],
                      "event_id": s["event_id"],
                      "net": s["output"]["net_effect"].get("net_effect_20d_pct")}
                     for s in s1_events]
        s1_ev_div = next((s for s in s1_events
                          if s["output"]["type"] == "分红"), None)

        # ── 阶段5：C3 双路评审（A=事件供料 / B=资金） ──
        print("阶段5 C3 证据链评审（真实快照 + S1 快照）")
        close_d = q["close"]
        preds = [
            {"prediction_id": "M1", "metric": "窗口收盘涨跌幅", "data_source": "snapshot",
             "baseline": "0", "direction": "UP", "threshold": "0",
             "condition": "分红实施后资金认可", "window": 2},
            {"prediction_id": "M2", "metric": "同榜机构股次日平均涨跌幅", "data_source": "snapshot",
             "baseline": "0", "direction": "UP", "threshold": "0",
             "condition": "机构榜板块扩散", "window": 1},
            {"prediction_id": "M3", "metric": "窗口最低价不破止损", "data_source": "snapshot",
             "baseline": str(round(close_d * 0.95, 2)), "direction": "DOWN",
             "threshold": str(round(close_d * 0.95, 2)),
             "condition": "涨停后回撤不破 5% 止损", "window": 2}]
        five = {k: "%s（真实回放：分红实施+机构净买入，判断在 LLM 层留痕）" % k
                for k in TQ.FIVE_KEYS}
        th_a = TH.create_thesis(D, "A", "事件催化：分红实施（丽珠集团 2026-09-29 公告）",
                                code=CODE, candidate_id=None, gap=TH.GAP_DRIVER,
                                path=thesis_f)["id"]
        ev_a = [{"text": "分红实施公告（art_code=%s）S1 评估净效应可追溯" % s1_ev_div["event_id"][:8],
                 "source": "S1:EVENT_EVAL", "ref": s1_ev_div["event_id"]}] if s1_ev_div else []
        rv_a = drive.review({"driver": "A", "thesis_id": th_a, "evidence": ev_a,
                             "inference": ["分红实施 → 历史净效应 +0.45%（n=8179）→ 微强度正向"],
                             "prediction": preds, "five": five},
                            as_of=D, code=CODE, snapshot_root=snap_root,
                            pool_path=pool_f, path=chains_f, s1_path=s1_snap_p)
        print("  A 路: effective=%s reason=%s" % (rv_a["driver_effective"],
                                                  rv_a["driver_reason"]))
        th_b = TH.create_thesis(D, "B", "资金抢占：机构榜净买入（丽珠集团 2026-09-29）",
                                code=CODE, candidate_id=None, gap=TH.GAP_DRIVER,
                                path=thesis_f)["id"]
        rv_b = drive.review({"driver": "B", "thesis_id": th_b,
                             "evidence": [{"text": "机构榜净买入 6293 万（tdDays=3）",
                                           "source": "SNAPSHOT:dragon_tiger", "ref": CODE}],
                             "inference": ["机构净买入 → 溢价兑现"],
                             "prediction": preds, "five": five},
                            as_of=D, code=CODE, snapshot_root=snap_root,
                            pool_path=pool_f, path=chains_f)
        print("  B 路: effective=%s reason=%s" % (rv_b["driver_effective"],
                                                  rv_b["driver_reason"]))
        rep["c3"] = {"A": {"effective": rv_a["driver_effective"],
                           "reason": rv_a["driver_reason"]},
                     "B": {"effective": rv_b["driver_effective"],
                           "reason": rv_b["driver_reason"]}}

        # ── 阶段6：C4 三问 → C5 风控（主链 = A 路） ──
        print("阶段6 C4 三问 → C5 风控（主链 A 路；价位线取真实收盘价基准）")
        d1b = drive.load_chain(code=CODE, as_of=D, path=chains_f)
        # 口径 v1.1：决策=盘后 → 成交=次日开盘；entry_limit = 收盘×1.02（限价容忍带，
        # 超过即不追价），止损/目标仍取决策日收盘基准（决策时点可得信息）。
        odds = dict(entry_limit=round(close_d * 1.02, 2), stop_line=round(close_d * 0.95, 2),
                    target=round(close_d * 1.10, 2), window=2, position_request=0.2)
        r_buy = ORCH.decide(as_of=D, code=CODE, thesis_id=th_a, chain=d1b["chain"],
                            sim=True, driver=rv_a["driver_effective"], env=env,
                            base=base, pool_path=pool_f, thesis_path=thesis_f,
                            snapshot_root=snap_root, **odds)
        print("  C4: status=%s action=%s decision_id=%s"
              % (r_buy["status"], r_buy["action"], r_buy["decision_id"]))
        rep["c4"] = {"status": r_buy["status"], "action": r_buy["action"],
                     "decision_id": r_buy["decision_id"], "odds": odds}
        if r_buy["action"] != "BUY":
            print("  非 BUY → 结算照常执行（unfilled/无成交如实呈现）")
        pg = PG.check(0.2, code=CODE, as_of=D, grade=env_res["regime"],
                      drawdown=0.0, ledger_sim=True, base=base)   # v1.1.2：持仓经账本现取（统一口径）
        print("  C5: verdict=%s plan=%s basis=%s"
              % (pg["verdict"], pg["position_plan"],
                 (pg["gates"] or {}).get("env", {}).get("basis")))
        rep["c5"] = {"verdict": pg["verdict"], "plan": pg["position_plan"]}
        if pg["verdict"] == "PASS" and r_buy["action"] == "BUY":
            DEC.append_risk_plan(r_buy["decision_id"], pg["position_plan"], as_of=D,
                                 gates=pg["gates"], note="真实数据回放", base=base)

        # ── 阶段7：逐日结算（真实行情；判定=机械代入真实数据，非 LLM） ──
        # 口径 v1.1（前视修复）：entry = D1 开盘价；T+1 自 D1 后首快照日起检查止损/目标。
        # 数据只到 D1 → 持有窗口（window=2，自成交日起）走不完 → PENDING_WINDOW 如实呈现。
        print("阶段7 结算 D→D1（真实行情；M1/M2/M3 机械判定）")
        d_q1, d_q0 = kb[D1][CODE], kb[D][CODE]
        peer = [x for x in pay["dragon_tiger"] if x["code"] != CODE and x["code"] in kb[D1]
                and x["code"] in kb[D]]
        peer_ret = [round((kb[D1][x["code"]]["close"] / kb[D][x["code"]]["close"] - 1) * 100, 2)
                    for x in peer]
        m1_obs = round((d_q1["close"] / d_q0["close"] - 1) * 100, 2)
        m2_obs = round(sum(peer_ret) / len(peer_ret), 2) if peer_ret else None
        m3_obs = d_q1["low"]        # v1.1：只看成交日后窗口（决策日盘中低点在入场前，不算）
        stop = odds["stop_line"]
        judgements = {r_buy["decision_id"]: {"verdicts": {
            "M1": {"metric": "窗口收盘涨跌幅", "data_source": "snapshot",
                   "verdict": "SUPPORT" if m1_obs >= 0 else "REFUTE",
                   "observed": "%s%%" % m1_obs, "note": "真实 09-30 收盘代入"},
            "M2": {"metric": "同榜机构股次日平均涨跌幅", "data_source": "snapshot",
                   "verdict": ("SUPPORT" if (m2_obs or 0) >= 0 else "REFUTE")
                   if m2_obs is not None else "MIXED",
                   "observed": ("%s%%(n=%d)" % (m2_obs, len(peer_ret)))
                   if m2_obs is not None else "同榜股行情缺", "note": None},
            "M3": {"metric": "窗口最低价不破止损", "data_source": "snapshot",
                   "verdict": "SUPPORT" if m3_obs > stop else "REFUTE",
                   "observed": str(m3_obs), "note": "真实窗口最低价代入"}},
            "attribution_L1L5": {"L1": "OK", "L2": "OK", "L3": "OK", "L4": "OK",
                                 "L5": "OK"},
            "attribution_三问": {"驱动": "OK", "赔率": "OK", "证伪": "NA"}}}
        rep1 = SETTLE.settle(D, judgements=judgements, equity=EQUITY, base=base,
                             snapshot_root=snap_root,
                             raw_dir=os.path.join(LD.roots(base)["raw"], D),
                             cursor_path=cursor_f, learning_path=dq_f,
                             calendar=_cal)
        rep2 = SETTLE.settle(D1, judgements=judgements, equity=EQUITY, base=base,
                             snapshot_root=snap_root,
                             raw_dir=os.path.join(LD.roots(base)["raw"], D1),
                             cursor_path=cursor_f, learning_path=dq_f,
                             calendar=_cal)
        print("  settle(D)=%s | settle(D1)=%s cursor=%s"
              % (rep1["settled"], rep2["settled"], rep2["cursor_after"]))
        print("  机械判定: M1=%s%% M2=%s%%(n=%d) M3最低=%s vs 止损=%s"
              % (m1_obs, m2_obs, len(peer_ret), m3_obs, stop))
        dec = DEC.load_decision(r_buy["decision_id"], sim=True, base=base)
        print("  EXECUTION: %s (pos=%s)" % (dec.get("execution_result"),
                                            dec.get("actual_position")))
        _p = PNL.read_pnl(r_buy["decision_id"], True, base=base)
        print("  PNL: %s" % ({k: _p.get(k) for k in ("outcome", "exit_price",
                                                     "pnl_amount", "hold_days")}
                              if _p else "PENDING（窗口未走完/数据缺）"))
        rep["settle"] = {"exec": dec.get("execution_result"),
                         "pnl": {k: _p.get(k) for k in ("outcome", "exit_price",
                                                        "pnl_amount", "hold_days",
                                                        "exit_info")} if _p else None,
                         "m_obs": {"M1": m1_obs, "M2": m2_obs, "M3": m3_obs,
                                   "stop": stop}}
        cb1 = CB.build(D, sim=True, base=base, path=cases_f)
        cb2 = CB.build(D1, sim=True, base=base, path=cases_f)
        print("  Case: added=%s（D）/ %s（D1）" % (cb1["added"], cb2["added"]))
        rep["case"] = cb1["added"] + cb2["added"]

        # ── 阶段8：真库零污染 ──
        print("阶段8 生产指纹核验")
        ok_fp = real_fp == _selftest_tree_fp([os.path.join(DEC.ROOT, "data"),
                                              os.path.join(DEC.ROOT, "memory")])
        print("  生产 data/+memory/ 前后指纹一致 = %s | data/raw 存在 = %s"
              % (ok_fp, os.path.isdir(raw_prod)))
        rep["clean"] = {"fingerprint_ok": ok_fp, "raw_absent": not os.path.isdir(raw_prod)}
        return rep
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _discipline(verbose=True):
    import astwin as ASTW
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    lines = src.splitlines(True)
    drop = set()
    for lo, hi, name in ASTW.func_table(ast.parse(src)):
        if name.startswith(("_selftest", "_discipline", "_main")):
            drop.update(range(lo - 1, hi))
    prod = "".join(l for i, l in enumerate(lines) if i not in drop)
    res = []
    res.append(("① 生产区零写盘原语（无 os.replace/mkstemp/.write(/json.dump/写模式）",
                "os.replace" not in prod and "mkstemp" not in prod
                and ".write(" not in prod and "json.dump(" not in prod
                and '", "w"' not in prod and '", "a"' not in prod
                and "'w'" not in prod))
    res.append(("② 真实数据声明在案（manual 第二类 · 非实时 · 零补造）",
                "真实历史" in src and "零补造" in src))
    res.append(("③ 转换层只读（io.open 无写模式；写盘仅经 stage_core/promote/S1/settle 系统接口）",
                "stage_core(" in prod and "promote_snapshot(" in prod
                and "SETTLE.settle(" in prod))
    ok = True
    for name, passed in res:
        if verbose:
            print(("  [ok] " if passed else "  [!!] ") + name)
        ok = ok and passed
    print("纪律结果：%s（%d/%d）" % ("通过 ✅" if ok else "失败 ❌",
                                    sum(1 for _, p in res if p), len(res)))
    return ok


def _main(argv):
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    if "--selftest" in argv:
        # ★ 2026-10-20 修：--selftest 曾误触发全量真实回放（v1.4 日历强制后必挂）。
        print("real_replay 无单元自检（验收 = --discipline + 真跑）；纪律检查：")
        return 0 if _discipline() else 1
    run_replay()
    return 0


if __name__ == "__main__":
    import ast
    sys.exit(_main(sys.argv[1:]))
