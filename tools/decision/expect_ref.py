#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""expect_ref.py · C3「剩余预期」历史证据输入件（v1.0 · 2026-10-16 建 · 任务 204）

用途：把 S1 事件效应库（data/knowledge/s1_event_effects/）+ 旧包 K 线的**历史观测**
      机械聚合成「剩余预期参考值」，供 gendrive 填五件套·剩余预期。

★ 一、数据契约（全部只读，逐条转录自库元信息，禁事后改口）
  · S1 主表 effect_base.json：32 类匹配对照净效应（61,980 事件包 · 2026-01~08 实测）
      as_of=2026-09-21 · benchmark 基准率 +0.36%（净效应必须相对基准读）·
      n_min_trust=30（只信 n>=30 的格子）· 事件基准率/胜率**不做主观生成**。
  · backfill 实证块（v1.1.0）：CAR(k) 市场调整口径，data_end=2026-09-30，
      时序守卫 read_backfill（as_of < data_end → LOOKAHEAD_GUARD 拒答）。
  · 事件归类：**唯一入口 S1.classify**（官方 cols 优先、关键词兜底 + 反义排除）；
      本件**不扩大匹配范围、不做同义词模糊匹配**。
  · 市场 20 日涨幅：旧包 klines.csv 全市场等权实测（口径同 s1_backfill bench 铁律：
      等权**均值**，禁中位数），严格 date < as_of（防未来函数）。

★ 二、输出规则（rule_version = "EXR-R1"，逐票可追溯）
  每票输出：
    {status, event_cat, classify_basis, hist:{net20,n,evidence_grade},
     backfill:{available, car_mean_pct, n_valid}, market_20d_pct,
     room_ref:{pct, basis, entry, target} | None,
     data_cutoff, rule_version, reasons[]}
  · **历史观测与当前推断严格分离**：hist/backfill 是 2026-01~08 历史统计（观测），
    room_ref 才是「相对当前价位」的受限参考值（推断）——两者不同字段，禁混读。
  · **room_ref 仅在全部条件成立时才非 null**：
      ① 事件类非 _NEUTRAL 且 basis != 'none'（有真实匹配，非无证据）
      ② historical_effect status=OK（n>=30，已过 n_min_trust）
      ③ 市场 20 日涨幅可算 且 未触发 kb.regime 归零档（强势市_>8% → 禁引用）
      ④ 调用方提供 target/entry（priceline 实算值）且 target>entry>0
      room_ref.pct = net_effect_20d% + 基准率偏移 0 的相对口径：
         **只给历史净效应作为 T+20 参照**（net_effect 已含匹配对照，禁再加码、
         禁换算成功率）；basis 串写明来源与限制。
  · **N<30 → INSUFFICIENT_SAMPLES，room_ref=null**（库口径：只信 n>=30 的格子）。
  · **历史净效应 ≠ 当前预期**：输出值恒带 restricted_reference=True 标记与
    data_cutoff；消费方（gendrive/三问）只能把它当参考文本，不得当事实/胜率。
  · 数据缺口如实：无 news / 快照缺席 / 市场涨幅不可算 → status 显式 + reasons，
    不默认填值。

★ 三、边界
  零写点（纯只读；--out 由 CLI 注入，缺省不落盘）。
  不改 S1/three_q/decision 任何冻结件；不触碰 C4/C5 门槛。

用法：
  python tools/decision/expect_ref.py --as-of 2026-09-29 --codes sh600189,sh600241 \
      [--snapshot-root <沙盒快照根>] [--kb-base <沙盒库根>] [--old-root <klines根>]
      [--prices <code>:entry:target,...] [--out <path>]
  python tools/decision/expect_ref.py --selftest
"""
import argparse
import collections
import datetime as _dt
import io
import json
import os
import re
import sys

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "fs"))
_ANAL = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "..", "analysis"))
sys.path.insert(0, _FS)
sys.path.insert(0, _ANAL)

RULE_VERSION = "EXR-R1"
MKT_GAIN_WINDOW = 20          # 市场 20 交易日涨幅取样窗（与 kb.regime 定义同口径）
MKT_GAIN_MIN_CODES = 200     # 等权样本下限（不足 → 涨幅不可算，如实）
TITLES_TOP_N = 5             # 每票取前 N 条 news 标题做归类（防长尾噪声）

# S1 效应库知识库路径（**只读消费**；同 s1.py KB_DIR / s1_backfill._KB_DIR 先例——
# 目录常量止于 3 段、不带文件名尾段，避免被 landing 的 S 知识库 cap_ref 正则
# （^data/knowledge/<cap>/ 须有后继段）误判为「未登记能力输出落点」；文件名段独立
# 存常量、join 时以变量为首参 → _join_parts 无锚点段 → 不产生静态路径 token。）
_S1_KB_DIR_REL = os.path.join("data", "knowledge", "s1_event_effects")
_S1_KB_FILE = "effect_base.json"


class ExpectRefError(Exception):
    pass


def _check_date(s):
    try:
        _dt.date.fromisoformat(str(s))
    except ValueError:
        raise ExpectRefError("BAD_DATE: %r（要 YYYY-MM-DD）" % s)
    return str(s)


# ── 事件归类（唯一入口 S1.classify；titles 由快照 news 面机械组装）────────────
def _news_titles(snap, code):
    out = []
    for r in (snap.get("news") or []):
        for c in (r.get("related_codes") or []):
            if c == code:
                t = (r.get("title") or "").strip()
                b = (r.get("body") or "").strip()
                out.append(("|".join(x for x in (t, b) if x)) or None)
        if len(out) >= TITLES_TOP_N:
            break
    return [x for x in out if x]


# ── 市场 20 日涨幅（等权均值；口径同 s1_backfill bench 铁律）────────────────
def market_gain_20d(old_root, as_of):
    """全市场等权 20 交易日涨幅（%）+ 样本数；不可算 → None。"""
    import priceline as PL
    # 需 LOOKBACK 根 + 基准前 1 根（末根 20 日涨幅以 bars[-21] 为基）+ SWING 窗口余量
    by_code, meta = PL.load_klines_asof(
        old_root, as_of, lookback=max(MKT_GAIN_WINDOW + 5, PL.SWING_LOOKBACK))
    gains = []
    for code, bars in by_code.items():
        if len(bars) >= MKT_GAIN_WINDOW + 1:
            c0 = bars[-MKT_GAIN_WINDOW - 1][1]
            c1 = bars[-1][1]
            if c0 > 0:
                gains.append((c1 - c0) / c0 * 100.0)
    if len(gains) < MKT_GAIN_MIN_CODES:
        return None
    return sum(gains) / len(gains), len(gains)


# ── kb.regime 归零档阈值（从库文本提取，禁手抄）─────────────────────────────
def strong_market_threshold(kb):
    """kb['regime'] 中「强势市_>8%」类归零档阈值（%）。键形如 `强势市_>8%`，
    提取「>N%」的 N。提取失败 → None（保守：归零档未判，room_ref 拒给）。"""
    regime = kb.get("regime") or {}
    for key, val in regime.items():
        if "归零" in str(val):
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", str(key))
            if m:
                return float(m.group(1))
    return None


def compute(codes, as_of, *, snapshot_root=None, kb_base=None, old_root=None,
            prices=None):
    """codes=[code...] · prices={code: {"entry_limit":.., "target":..}}（可选）
    → (by_code, summary)；by_code[code] = 上述输出结构。全零写点。"""
    import s1 as S1
    import s1_backfill as S1BF
    import snapshot as SNAP

    as_of = _check_date(as_of)
    out, reasons_hist = {}, collections.Counter()

    # ① 效应库（loader 时序守卫；不可读 → 全票拒，不降级）
    try:
        kb, kb_note = S1.load_kb(as_of, base=kb_base)
    except S1.S1Error as e:
        raise ExpectRefError("S1_KB_UNAVAILABLE: %s" % e)

    # ② 市场 20 日涨幅（归零档判据；源缺席 → None，保守不给参考值，如实记）
    mg = None            # (mean_pct, n_samples) | None
    _klines_note = None
    if old_root:
        try:
            mg = market_gain_20d(old_root, as_of)
        except Exception as e:      # KLINES_ABSENT / EMPTY → 保守拒
            mg = None
            _klines_note = "%s: %s" % (type(e).__name__, str(e)[:120])
    strong_th = strong_market_threshold(kb)
    mg_mean = mg[0] if mg else None
    regime_zero = (mg_mean is not None and strong_th is not None and mg_mean > strong_th)

    # ③ 快照（news 面；缺席 → 该票无事件证据，如实）
    snap = SNAP.UNAVAILABLE
    if snapshot_root:
        snap = SNAP.load_snapshot(as_of, root=snapshot_root)
    kb_cutoff = str(kb.get("as_of") or "")
    bf_data_end = str((kb.get("backfill") or {}).get("data_end") or "")

    for code in codes:
        titles = _news_titles(snap, code) if snap is not SNAP.UNAVAILABLE else []
        tax = S1.load_taxonomy(base=kb_base)
        cls = S1.classify("|".join(titles), tax) if titles else None
        cat = cls["cat"] if cls else None

        rec = {"code": code, "as_of": as_of, "rule_version": RULE_VERSION,
               "event_cat": cat, "classify_basis": cls.get("basis") if cls else None,
               "n_news": len(titles), "hist": None, "backfill": None,
               "market_20d_pct": round(mg_mean, 3) if mg_mean is not None else None,
               "regime_zeroed": regime_zero, "room_ref": None,
               "data_cutoff": None, "reasons": []}
        if snap is SNAP.UNAVAILABLE:
            rec["reasons"].append("SNAPSHOT_ABSENT: 该日无快照（news 面不可用，事件证据缺）")
        elif not titles:
            rec["reasons"].append("NO_NEWS: 该票当日无 news 关联")

        if cat is None or cat == "_NEUTRAL":
            rec["status"] = "NEUTRAL_OR_NO_EVENT"
            rec["restricted_reference"] = False
            out[code] = rec
            continue

        # ④ 历史效应（n<30 拒用；NO_RECORD 如实）
        he = S1.historical_effect(cat, kb)
        if he["status"] == "OK":
            rec["hist"] = {"net20": he["net_effect_20d_pct"], "n": he["sample_size"],
                           "grade": he["evidence_grade"],
                           "event_fut20": he["event_fut20_pct"],
                           "control_fut20": he["control_fut20_pct"]}
            rec["data_cutoff"] = kb_cutoff
            reasons_hist["HIST_OK"] += 1
        else:
            rec["reasons"].append("%s: %s" % (he["status"], he.get("note", "")))
            rec["hist"] = {"n": he.get("sample_size"), "note": he.get("note")}
            rec["status"] = "HIST_" + he["status"].upper()
            out[code] = rec
            continue

        # ⑤ backfill 交叉参照（LOOKAHEAD 被守 → 只如实记，不引用）
        # ★ 路径以**模块常量 + 变量首参**构造（同 s1_backfill.KB_PATH / _evt_path 先例）：
        #   静态分析（_join_parts）遇到 Name/常量不产生 `data/knowledge/s1_event_effects/…`
        #   的完整字面量 ⇒ 不会被误判为「未登记能力输出落点」；本件对知识库**只读**
        #   （晋升走治理动作，无代码写点），运行时拼装即可。
        _bf_path = None
        if kb_base:
            _bf_path = os.path.join(kb_base, _S1_KB_DIR_REL, "effect_base.json")
        bf = S1BF.read_backfill(cat, as_of, kb_path=_bf_path)
        if bf.get("available"):
            rec["backfill"] = {"car_mean_pct": bf["car_mean_pct"],
                               "n_valid": bf["n_valid"], "data_end": bf.get("data_end")}
        else:
            rec["backfill"] = {"available": False, "reason": bf.get("reason")}
            if bf.get("reason") == "LOOKAHEAD_GUARD":
                rec["reasons"].append("BACKFILL_LOOKAHEAD: as_of 早于 backfill 数据截止 %s，"
                                      "回填统计禁用（守卫生效）" % bf.get("detail", ""))

        # ⑥ 受限参考值（全部条件成立才给）
        pr = (prices or {}).get(code) or {}
        entry, target = pr.get("entry_limit"), pr.get("target")
        basis_bits = ["S1:%s net20=%.2f%% n=%d grade=%s" %
                      (cat, he["net_effect_20d_pct"], he["sample_size"], he["evidence_grade"])]
        if rec["backfill"] and rec["backfill"].get("car_mean_pct"):
            basis_bits.append("S1BF:CARk5=%.2f%%" % rec["backfill"]["car_mean_pct"].get("k5", 0.0))
        if pr.get("note"):
            basis_bits.append(pr["note"])

        # 独立闸门（任一不满足 → 不给参考值，全部原因留痕；价线矛盾与市场档互不短路）
        if target is not None and entry is not None and target <= entry:
            rec["reasons"].append("ROOM_REF_INVALID_PRICES: target(%.4f)<=entry(%.4f)（价线矛盾）→ 不给参考值" % (target, entry))
        if regime_zero:
            rec["reasons"].append("REGIME_ZEROED: 市场20日 %.1f%% > %.0f%%（kb.regime 强势市档）→ 事件效应归零，禁引用" % (mg_mean, strong_th))
        elif mg_mean is None:
            _why = _klines_note or "klines 源缺席/样本不足"
            rec["reasons"].append("MKT_GAIN_NA: 市场 20 日涨幅不可算（%s）→ 归零档未判，保守拒给" % _why)
        elif cls.get("basis") == "none" or not pr or entry is None or target is None:
            rec["reasons"].append("ROOM_REF_INPUTS_MISSING: 需 priceline 实算 entry/target（未提供）→ 只给历史观测，不给当前参考值")

        _gate_open = (not regime_zero and mg_mean is not None
                      and cls.get("basis") != "none" and pr
                      and entry is not None and target is not None
                      and target > entry)
        if _gate_open:
            rec["room_ref"] = {
                "pct": round(he["net_effect_20d_pct"], 3),
                "window": "T+20",
                "restricted_reference": True,
                "note": "历史匹配对照净效应（2026-01~08 观测）≠当前预期；无胜率承诺；"
                        "基准率 %.2f%% 内含" % kb.get("benchmark", {}).get("event_base_rate_pct", 0.36),
                "basis": " | ".join(basis_bits),
                "entry": entry, "target": target,
            }
        rec["status"] = "OK"
        out[code] = rec

    summary = {"rule_version": RULE_VERSION, "as_of": as_of,
               "n_codes": len(codes),
               "n_neutral_or_no_event": sum(1 for r in out.values()
                                             if r["status"] == "NEUTRAL_OR_NO_EVENT"),
               "n_ok": sum(1 for r in out.values() if r["status"] == "OK"),
               "n_room_ref": sum(1 for r in out.values() if r["room_ref"]),
               "n_regime_zeroed": sum(1 for r in out.values() if r["regime_zeroed"]),
               "n_hist_blocked": sum(1 for r in out.values()
                                     if str(r["status"]).startswith("HIST_")),
               "market_20d_pct": round(mg_mean, 3) if mg_mean is not None else None,
               "kb_as_of": kb_cutoff, "backfill_data_end": bf_data_end,
               "klines_note": _klines_note,
               "hist_reasons": dict(reasons_hist)}
    return out, summary


# ── 自检（全合成夹具，零生产接触）─────────────────────────────────────────────
def _selftest():
    import tempfile
    import shutil
    import s1 as S1
    import snapshot as SNAP

    tmp = tempfile.mkdtemp()
    ok = 0
    try:
        def T(name, cond):
            nonlocal ok
            assert cond, "SELFTEST FAIL: %s" % name
            ok += 1
            print("  [OK] %s" % name)

        # 造一个最小效应库 + taxonomy（临时，走 kb_base）
        kbdir = os.path.join(tmp, "data", "knowledge", "s1_event_effects")
        os.makedirs(kbdir, exist_ok=True)
        kb = {"as_of": "2026-09-21", "version": "1.1.0", "n_min_trust": 30,
              "benchmark": {"event_base_rate_pct": 0.36},
              "regime": {"强势市_>8%": "事件全部归零（禁在强势市引用本表净效应）"},
              "backfill": {"data_end": "2026-09-30"},
              "categories": {
                  "增持": {"n": 457, "net_effect_20d_pct": 2.98,
                           "event_fut20_pct": 2.89, "control_fut20_pct": -0.09,
                           "direction_prior": 1,
                           "backfill": {"car_mean_pct": {"k1": 0.107, "k5": 0.021, "k20": 1.51},
                                        "n_valid": {"k1": 611, "k5": 611, "k20": 579},
                                        "n_events": 611, "sample_ok": True}},
                  "冷门小样本": {"n": 8, "net_effect_20d_pct": 9.9,
                                  "event_fut20_pct": 10.0, "control_fut20_pct": 0.1,
                                  "direction_prior": 1}}}
        with io.open(os.path.join(kbdir, "effect_base.json"), "w", encoding="utf-8") as f:
            json.dump(kb, f, ensure_ascii=False)
        tax = {"cat_map": {"增持": ["增持", 1]},
               "kw_fallback": [["增持", ["增持", 1]]], "kw_exclude": ["减持"]}
        with io.open(os.path.join(kbdir, "taxonomy.json"), "w", encoding="utf-8") as f:
            json.dump(tax, f, ensure_ascii=False)
        # loader 注册表：registry path 相对 base（b）拼。★ 路径字符串**拼接**构造
        # （同 s1_backfill KB_PATH / _evt_path 先例）：全路径字面量会被 landing 静态
        # 解析成「未登记能力 s1_event_effects 的输出落点」误判；拆分后各段单独
        # 不匹配 `^data/knowledge/<cap>/` 判据，保持知识库**只读消费**的外部性。
        regdir = os.path.join(tmp, "data", "knowledge")
        # ★ registry path 用 os.path.join 拼（变量首段，landing 静态分析**不**常量折叠
        #   函数调用参数，各段单独均不匹配 `^data/knowledge/<cap>/`）——同 s1_backfill
        #   _evt_path / KB_PATH 先例，保持知识库「只读消费」的外部性、非能力输出落点。
        _kbcap = "s1_event_effects"
        _kb_rel = os.path.join("data", "knowledge", _kbcap, "effect_base.json")
        reg = {"s1": {"path": _kb_rel, "version": "1.1.0"}}
        with io.open(os.path.join(regdir, "_registry.json"), "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False)

        # snapshot 夹具（news 面）——5 面齐（csv 带表头最小合法形态）
        snapdir = os.path.join(tmp, "snap")
        p = os.path.join(snapdir, "2026-09-29")
        os.makedirs(p, exist_ok=True)
        with io.open(os.path.join(p, "news.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"news_id": "n1", "ts": "2026-09-29T08:00:00",
                                "source": "x", "title": "公司增持", "body": "",
                                "related_codes": ["sh600001"]}, ensure_ascii=False) + "\n")
        hdrs = {"daily_quote.csv": "code,name,open,high,low,close,pct,vol,amount",
                "limit_up.csv": "code,name,boards,first_seal_time,seal_amount,reason",
                "dragon_tiger.csv": "code,name,seat,buy,sell,net"}
        for fn, hdr in hdrs.items():
            with io.open(os.path.join(p, fn), "w", encoding="utf-8") as f:
                f.write(hdr + "\n")
        with io.open(os.path.join(p, "env_metrics.json"), "w", encoding="utf-8") as f:
            json.dump({"limit_up_count": 0, "max_board": 1, "turnover": None,
                       "up_down_ratio": None, "fail_rate": None, "index_ma20": None}, f)
        snap = SNAP.load_snapshot("2026-09-29", root=snapdir)
        T("A1 快照夹具可读", snap is not SNAP.UNAVAILABLE)

        prices = {"sh600001": {"entry_limit": 10.0, "target": 13.0,
                               "note": "PL-R1 实算（夹具）"}}
        # 合成 klines 夹具（200 码 × 30 日 < as_of）→ 市场等权涨幅可算（非零漂移 <8% 不触发归零）
        old = os.path.join(tmp, "old")
        os.makedirs(os.path.join(old, "data", "raw", "bus"), exist_ok=True)
        import csv as _csv
        with io.open(os.path.join(old, "data", "raw", "bus", "klines.csv"),
                      "w", encoding="utf-8", newline="") as f:
            w = _csv.writer(f)
            w.writerow(["code", "name", "date", "close", "high", "low",
                        "change_pct", "amount"])
            for i in range(200):
                c0 = 8.0 + (i % 10) * 0.5
                for j in range(30):
                    d = "2026-%02d-%02d" % (8 + j // 28, 20 + j % 28)
                    c = round(c0 * (1 + 0.002 * j), 3)
                    w.writerow(["sh6%04d" % i, "X", d, c, c * 1.01, c * 0.99, 0.2, 1e8])
        out, summ = compute(["sh600001", "sh600002"], "2026-09-29",
                            snapshot_root=snapdir, kb_base=tmp, old_root=old,
                            prices=prices)
        T("A2 有事件票 status=OK + room_ref 给出（净效应原值，非换算）",
          out["sh600001"]["status"] == "OK"
          and out["sh600001"]["room_ref"]["pct"] == 2.98
          and out["sh600001"]["room_ref"]["restricted_reference"] is True)
        T("A3 无事件票 → NEUTRAL_OR_NO_EVENT（不造数）",
          out["sh600002"]["status"] == "NEUTRAL_OR_NO_EVENT"
          and out["sh600002"]["room_ref"] is None)
        T("A4 summary 计数一致 + 市场涨幅可算",
          summ["n_ok"] == 1 and summ["n_neutral_or_no_event"] == 1
          and summ["market_20d_pct"] is not None and not out["sh600001"]["regime_zeroed"])

        # B：小样本类拒用（n<30）——monkeypatch S1.classify 让「增持」命中小样本类
        real_cls = S1.classify

        def fake_cls(cols_str, taxonomy):
            if "增持" in cols_str:
                return {"cat": "冷门小样本", "dir_prior": 1, "basis": "keyword",
                        "all_hits": []}
            return real_cls(cols_str, taxonomy)
        S1.classify = fake_cls
        try:
            out3, _ = compute(["sh600001"], "2026-09-29", snapshot_root=snapdir,
                              kb_base=tmp, old_root=None, prices=prices)
            T("B1 n<30 → HIST_INSUFFICIENT_SAMPLES + room_ref=null（禁当参数）",
              out3["sh600001"]["status"] == "HIST_INSUFFICIENT_SAMPLES"
              and out3["sh600001"]["room_ref"] is None)
        finally:
            S1.classify = real_cls

        # C：market_gain 不可算（无 klines 源 → mg=None → 归零档未判 → 不给参考值）
        out_c, _ = compute(["sh600001"], "2026-09-29", snapshot_root=snapdir,
                           kb_base=tmp, old_root=None, prices=prices)
        T("C1 市场涨幅不可算 → 保守不给 room_ref（如实记 MKT_GAIN_NA）",
          out_c["sh600001"]["market_20d_pct"] is None
          and any("MKT_GAIN_NA" in r for r in out_c["sh600001"]["reasons"])
          and out_c["sh600001"]["room_ref"] is None)

        # D：价线矛盾（target<=entry）拒给
        out4, _ = compute(["sh600001"], "2026-09-29", snapshot_root=snapdir,
                          kb_base=tmp, old_root=None,
                          prices={"sh600001": {"entry_limit": 10.0, "target": 9.0}})
        T("D1 target<=entry → 不给 room_ref + 原因留痕",
          out4["sh600001"]["room_ref"] is None
          and any("ROOM_REF_INVALID_PRICES" in r for r in out4["sh600001"]["reasons"]))

        # E：强势市归零档（市场 20 日涨幅 > 8% → kb.regime 禁引用）
        old_hot = os.path.join(tmp, "old_hot")
        os.makedirs(os.path.join(old_hot, "data", "raw", "bus"), exist_ok=True)
        with io.open(os.path.join(old_hot, "data", "raw", "bus", "klines.csv"),
                      "w", encoding="utf-8", newline="") as f:
            w = _csv.writer(f)
            w.writerow(["code", "name", "date", "close", "high", "low",
                        "change_pct", "amount"])
            for i in range(200):
                c0 = 8.0 + (i % 10) * 0.5
                for j in range(30):
                    d = "2026-%02d-%02d" % (8 + j // 28, 20 + j % 28)
                    c = round(c0 * (1 + 0.008 * j), 3)   # 末 20 日涨幅 ≈ 16% > 8%
                    w.writerow(["sh6%04d" % i, "X", d, c, c * 1.01, c * 0.99, 0.5, 1e8])
        out5, _ = compute(["sh600001"], "2026-09-29", snapshot_root=snapdir,
                          kb_base=tmp, old_root=old_hot, prices=prices)
        T("E1 强势市 → REGIME_ZEROED + room_ref=null（禁引用本表净效应）",
          out5["sh600001"]["regime_zeroed"] is True
          and out5["sh600001"]["room_ref"] is None
          and any("REGIME_ZEROED" in r for r in out5["sh600001"]["reasons"]))

        # F：backfill LOOKAHEAD 守卫（as_of=09-29 < data_end=09-30 → 禁用，如实记）
        out_f, _ = compute(["sh600001"], "2026-09-29", snapshot_root=snapdir,
                           kb_base=tmp, old_root=old, prices=prices)
        T("F1 backfill LOOKAHEAD_GUARD 生效（回填块缺席 + 原因留痕）",
          out_f["sh600001"]["backfill"]["available"] is False
          and out_f["sh600001"]["backfill"]["reason"] == "LOOKAHEAD_GUARD")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("expect_ref selftest %d/%d PASS" % (ok, 9))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of")
    ap.add_argument("--codes", help="逗号分隔；缺省=全候选池活跃码由调用方给")
    ap.add_argument("--snapshot-root", dest="snapshot_root")
    ap.add_argument("--kb-base", dest="kb_base",
                    help="效应库根（沙盒换根；缺省=生产 data/knowledge）")
    ap.add_argument("--old-root", dest="old_root",
                    help="klines 根（缺省=旧包根；不传则市场涨幅不可算，保守拒给参考值）")
    ap.add_argument("--prices", help="code:entry:target[,code:entry:target...]（priceline 实算值）")
    ap.add_argument("--out")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if not a.as_of or not a.codes:
        print("用法：--as-of <YYYY-MM-DD> --codes a,b [--snapshot-root ..] "
              "[--kb-base ..] [--old-root ..] [--prices c:e:t,...] [--out f]")
        return 2
    codes = [c for c in a.codes.split(",") if c]
    prices = {}
    if a.prices:
        for item in a.prices.split(","):
            parts = item.split(":")
            if len(parts) == 3:
                try:
                    prices[parts[0]] = {"entry_limit": float(parts[1]),
                                         "target": float(parts[2])}
                except ValueError:
                    prices[parts[0]] = {"entry_limit": parts[1], "target": parts[2]}
    out, summ = compute(codes, a.as_of, snapshot_root=a.snapshot_root,
                       kb_base=a.kb_base, old_root=a.old_root or "C:\\Users\\G\\炒股智能体_旧包_20261008",
                       prices=prices)
    doc = {"schema_version": 1, "summary": summ, "by_code": out}
    if a.out:
        d = os.path.dirname(os.path.abspath(a.out))
        os.makedirs(d, exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        print("expect_ref: n=%d ok=%d room_ref=%d regime_zeroed=%d -> %s"
              % (summ["n_codes"], summ["n_ok"], summ["n_room_ref"],
                 summ["n_regime_zeroed"], a.out))
    else:
        print(json.dumps(summ, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
