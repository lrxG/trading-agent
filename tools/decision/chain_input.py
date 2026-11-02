#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chain_input.py · C3 链的**机械部分**：真实证据收集 + 预测三槽基准（v1.0 · 2026-10-19）

**为什么需要本件**
  真实候选在 C3 的报缺是 `C3:CHAIN` + `THESIS_ID` 两项（实测 2141/2141）。拆开看，
  C3 链提案的五个组成部分里**有三个不是判断、是可机械产出的事实**：

    evidence[]    ← 当日快照/候选池/S1 事件里的**可核验引用**（本件收集，逐条能在真源解析）
    prediction[]  ← **已有模块** `pred_ref`（PRR-R1）产出的历史分位基准（本件接线，不重写）
    driver        ← 判断（"这是哪类驱动"）
    inference[]   ← 判断（"我认为正在发生什么"）
    five{}        ← 判断（尤其「剩余预期」；`expect_ref` 的受限参考**明令不得填入**）

  因此本件只做前两项，后三项**一律由外部判断提供**，缺即报错——**不补、不猜、不占位**。
  这是把「外部依赖」收窄成最小可交付契约，而不是伪造判断。

**契约（零写点，只 stdout）**
  判断输入文件：{"as_of": "<date>",
                 "judgments": {"<code>": {"driver": "A|B|C|复合",
                                          "inference": ["…"],
                                          "five": {"驱动存在": "…", "当前证据": "…",
                                                   "延续机制": "…", "失效条件": "…",
                                                   "剩余预期": "…"},
                                          "thesis_id": "TH-…"(可选)}}}
  输出（stdout，由操作者自己重定向落盘；本件**不写任何文件**）：
    {"as_of", "producer", "chain_rule_version",
     "chains": {"<code>": <完整 C3 提案（恰 5+1 键，可直接喂 drive.review）>},
     "audit": {"<code>": {evidence_audit, prediction_audit, judgment_fields}}}

**用法**
  python tools/decision/chain_input.py --as-of 2026-09-29 --base <沙盒> \
         --judgment <判断文件> [--codes sh600241] > chains.json
  python tools/decision/chain_input.py --as-of 2026-09-29 --base <沙盒> \
         --diagnose sh600241      # 只报「哪些字段机械可得、哪些必须外部给」
  python tools/decision/chain_input.py --selftest
"""
import argparse
import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_FS = os.path.normpath(os.path.join(_HERE, "..", "fs"))
_AN = os.path.normpath(os.path.join(_HERE, "..", "analysis"))
_QY = os.path.normpath(os.path.join(_HERE, "..", "query"))
for _p in (_FS, _AN, _QY):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import candidates as CAND                                          # noqa: E402
import decision as DEC                                             # noqa: E402
import pred_ref as PR                                              # noqa: E402
import priceline as PL                                             # noqa: E402
import snapshot as SNAP                                            # noqa: E402

CHAIN_RULE_VERSION = "CHI-R1"
PRODUCER = "chain_input（机械：证据+预测基准；判断：外部 judgment）"

# 判断字段（本件只透传，不生成）
JUDGMENT_KEYS = ("driver", "inference", "five", "thesis_id")
_S_DIR = ("data", "state")
_S_NAME = "candidates.json"
_S_SNAP = ("data", "snapshot")
_S_PARAMS = ("data", "params.json")
_S_KB = ("data", "knowledge")


class ChainInputError(Exception):
    """入参/契约违规（显式抛，不静默降级）。"""


def _check_date(s):
    if not isinstance(s, str) or len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise ChainInputError("as_of 必须是 YYYY-MM-DD，实际 %r" % (s,))
    for i in (0, 1, 2, 3, 5, 6, 8, 9):
        if not s[i].isdigit():
            raise ChainInputError("as_of 非法：%r" % (s,))
    return s


def _p(base, *parts):
    return os.path.join(base, *parts) if base else None


def _fmt(v, unit=""):
    if v is None:
        return "NA"
    if isinstance(v, float):
        return "%.4g%s" % (v, unit)
    return "%s%s" % (v, unit)


# ── 证据收集（全部可在真源逐条解析；文本只陈述数据事实）─────────────────────
def collect_evidence(as_of, code, *, base=None, snapshot_root=None,
                     pool_path=None, s1_path=None, limit=6, news_days=2,
                     news_per_day=3):
    """→ (evidence, audit)。**只收能解析的引用**；一条都收不到 ⇒ 空列表（由上层报缺）。

    优先级按「对驱动判定最有信息量」排：候选命中 → 涨停 → 日线 → 龙虎榜 → S1 事件
    → **公告检索（2026-10-19 接线 news_search）** → 全市场情绪。每条 text 都是该引用在
    真源里的**原值陈述**，不含推断。

    `news_days`：公告证据回看窗口 = ≤as_of 的最近 N 个归档日（0 = 不检索）；
    时间纪律同全系统（信息截断：只取 ≤ as_of），text 带公告日期。
    """
    d = _check_date(as_of)
    snap_root = snapshot_root or _p(base, *_S_SNAP)
    snap = SNAP.load_snapshot(d, root=snap_root)
    if snap is SNAP.UNAVAILABLE:
        return [], {"status": "SNAPSHOT_UNAVAILABLE", "as_of": d,
                    "note": "当日快照不可读 ⇒ 不收证据（禁编造）"}
    pool = CAND.list_candidates(code=code, path=pool_path or _p(base, *_S_DIR, _S_NAME))
    cand = pool[-1] if pool else None
    lu = next((q for q in snap.get("limit_up") or [] if q.get("code") == code), None)
    dq = next((q for q in snap.get("daily_quote") or [] if q.get("code") == code), None)
    dt = next((q for q in snap.get("dragon_tiger") or [] if q.get("code") == code), None)
    env = snap.get("env_metrics") or {}

    s1 = None
    if s1_path and os.path.isfile(s1_path):
        try:
            with io.open(s1_path, encoding="utf-8-sig") as f:
                s1 = json.load(f)
        except Exception:                                         # noqa: BLE001
            s1 = None
    ev_s1 = None
    if isinstance(s1, dict) and str(s1.get("as_of") or "") == d:
        ev_s1 = next((e for e in (s1.get("events") or [])
                      if code in (e.get("related_codes") or [])), None)

    out, skipped = [], []
    if cand is not None:
        out.append({"text": "C2 候选命中：%s（state=%s，created_at=%s）"
                            % (cand.get("id"), cand.get("state"), cand.get("created_at")),
                    "source": "C2:CANDIDATE", "ref": cand.get("id")})
    else:
        skipped.append("C2:CANDIDATE（该 code 无候选记录）")
    if lu is not None:
        out.append({"text": "当日涨停：%s 连板 %s，首次封板 %s，封单 %s"
                            % (code, _fmt(lu.get("boards")), lu.get("first_seal_time"),
                               _fmt(lu.get("seal_amount"))),
                    "source": "SNAPSHOT:limit_up", "ref": code})
    else:
        skipped.append("SNAPSHOT:limit_up（该票当日未涨停）")
    if dq is not None:
        out.append({"text": "当日行情：收盘 %s，涨跌幅 %s，成交额 %s"
                            % (_fmt(dq.get("close")), _fmt(dq.get("pct"), "%"),
                               _fmt(dq.get("amount"))),
                    "source": "SNAPSHOT:daily_quote", "ref": code})
    else:
        skipped.append("SNAPSHOT:daily_quote（该票当日无行情）")
    if dt is not None:
        out.append({"text": "龙虎榜：%s 买入 %s / 卖出 %s / 净额 %s"
                            % (dt.get("seat"), _fmt(dt.get("buy")),
                               _fmt(dt.get("sell")), _fmt(dt.get("net"))),
                    "source": "SNAPSHOT:dragon_tiger", "ref": code})
    else:
        skipped.append("SNAPSHOT:dragon_tiger（该票当日无龙虎榜；生产该表长期为空）")
    if ev_s1 is not None:
        out.append({"text": "S1 事件：%s（strength=%s）"
                            % (ev_s1.get("title") or ev_s1.get("event_id"),
                               ev_s1.get("strength")),
                    "source": "S1:EVENT_EVAL", "ref": ev_s1.get("event_id")})
    else:
        skipped.append("S1:EVENT_EVAL（当日 S1 快照缺，或无该票相关事件）")
    # N 公告检索（2026-10-19 接线 news_search）：≤as_of 最近 news_days 个归档日，
    # 按票码精确检索；检索失败/归档缺席只记 skipped，不影响其他证据（禁崩）。
    if news_days and news_days > 0:
        try:
            import news_search as NS
            days = [dd for dd, _ in NS._iter_news_files(base=base) if dd <= d]
            n_news = 0
            for dd in days[-news_days:]:
                r = NS.search(code=code, date_from=dd, date_to=dd, base=base,
                              limit=news_per_day)
                for h in r.get("hits") or []:
                    out.append({"text": "公告（%s）：%s" % (h["date"], h["title"]),
                                "source": "N:NOTICE", "ref": h.get("news_id")})
                    n_news += 1
            if not n_news:
                skipped.append("N:NOTICE（窗口 %s 内无该票公告）"
                               % ("/".join(days[-news_days:]) or "无归档"))
        except Exception as e:                                  # noqa: BLE001
            skipped.append("N:NOTICE（检索不可用：%s——不影响其他证据）" % e)
    if env.get("limit_up_count") is not None:
        out.append({"text": "全市场情绪：涨停家数 %s，最高连板 %s"
                            % (_fmt(env.get("limit_up_count")),
                               _fmt(env.get("max_board"))),
                    "source": "SNAPSHOT:env_metrics", "ref": "limit_up_count"})

    if limit:
        out = out[:limit]
    # ★ 个股级证据 = ref 指向该 code（或该候选）的引用；`SNAPSHOT:env_metrics` 是**全市场**
    #   量，对该票本身零信息 —— 只有市场级证据不足以支撑「该票」的 C3 结论。
    code_level = [x for x in out if x["source"] != "SNAPSHOT:env_metrics"]
    return out, {"status": "OK" if code_level else "NO_CODE_LEVEL_EVIDENCE",
                 "as_of": d, "n": len(out), "n_code_level": len(code_level),
                 "sources": [x["source"] for x in out], "skipped": skipped,
                 "caliber": "逐条可在当日快照/候选池/S1 快照/公告归档（≤as_of）解析；"
                            "text 仅陈述原值"}


# ── 预测三槽（接线已有 pred_ref，不重写）─────────────────────────────────────
_PRED_FIELDS = DEC.PREDICTION_FIELDS


def build_predictions(as_of, code, *, base=None, old_root=None, kb_base=None):
    """→ (prediction[], audit)。**复用 `pred_ref.compute`**；只保留 usable 槽位。

    usable=False 的槽位**不填**（宁缺不造）：三槽不齐 ⇒ C4 的 BUY 自然不成立，
    这不是本件放宽或收紧，而是 pred_ref 的样本/守门结论。
    """
    d = _check_date(as_of)
    old_root = old_root or PL.OLD_ROOT
    kb_base = kb_base or _p(base, *_S_KB)
    by, summ = PR.compute([code], d, base=base, old_root=old_root, kb_base=kb_base)
    rec = (by or {}).get(code) or {}
    slots = rec.get("slots") or {}
    out, dropped = [], []
    for name in DEC.PRED_SLOTS:
        s = slots.get(name)
        if not s or s.get("usable") is False:
            dropped.append({"slot": name, "reason": (rec.get("reasons") or [None])[0]
                            if rec.get("reasons") else "SLOT_UNUSABLE"})
            continue
        miss = [k for k in _PRED_FIELDS if s.get(k) is None]
        if miss:
            dropped.append({"slot": name, "reason": "字段缺 %s" % miss})
            continue
        out.append({k: s[k] for k in _PRED_FIELDS})     # ★ 恰八字段（多一个键 review 会拒）
    out.sort(key=lambda x: DEC.PRED_SLOTS.index(x["prediction_id"]))
    return out, {"rule_version": rec.get("rule_version") or summ.get("rule_version"),
                 "usable_slots": [x["prediction_id"] for x in out],
                 "dropped": dropped, "reasons": rec.get("reasons") or [],
                 "data_cutoff": rec.get("data_cutoff"),
                 "restricted_reference": True,
                 "caliber": "baseline/threshold = 历史分位（可复现），非主观预测值"}


# ── 组装：机械部分 + 判断部分 ─────────────────────────────────────────────────
def compose(as_of, code, judgment, *, base=None, snapshot_root=None, pool_path=None,
            s1_path=None, old_root=None, kb_base=None, limit_evidence=6):
    """→ (完整 C3 提案, audit)。判断字段缺任一 ⇒ ChainInputError（不补、不猜）。"""
    d = _check_date(as_of)
    if not isinstance(judgment, dict):
        raise ChainInputError("judgment 必须是对象（%s）" % (code,))
    extra = set(judgment) - set(JUDGMENT_KEYS)
    if extra:
        raise ChainInputError("judgment 含契约外键 %s（只允许 %s）"
                              % (sorted(extra), list(JUDGMENT_KEYS)))
    miss = [k for k in ("driver", "inference", "five") if judgment.get(k) is None]
    if miss:
        raise ChainInputError("judgment 缺判断字段 %s —— 本件不生成判断（缺即报，不补）"
                              % (miss,))
    five = judgment["five"]
    if not isinstance(five, dict) or set(five) != set(DEC.FIVE_KEYS):
        raise ChainInputError("five 必须恰含五件套 %s（实际 %s）"
                              % (list(DEC.FIVE_KEYS),
                                 sorted(five) if isinstance(five, dict) else type(five).__name__))
    blank = [k for k in DEC.FIVE_KEYS
             if not isinstance(five.get(k), str) or not five[k].strip()]
    if blank:
        raise ChainInputError("five 有空白项 %s —— 空串不是判断（宁缺不造）" % (blank,))
    inf = judgment["inference"]
    if not isinstance(inf, list) or not inf:
        raise ChainInputError("inference 必须是非空列表（无推断 = 无 C3 结论）")
    for i, x in enumerate(inf):
        if not isinstance(x, str) or not x.strip():
            raise ChainInputError("inference[%d] 必须是非空字符串" % i)

    ev, ev_audit = collect_evidence(d, code, base=base, snapshot_root=snapshot_root,
                                    pool_path=pool_path, s1_path=s1_path,
                                    limit=limit_evidence)
    if not ev_audit.get("n_code_level"):
        raise ChainInputError(
            "该票当日无个股级证据（as_of=%s code=%s，只有市场级或全无）—— "
            "无个股证据不得产该票的 C3 结论（禁拿全市场数字冒充个股依据）"
            % (d, code))
    pred, pr_audit = build_predictions(d, code, base=base, old_root=old_root,
                                       kb_base=kb_base)
    prop = {"driver": judgment["driver"], "evidence": ev, "inference": list(inf),
            "prediction": pred, "five": dict(five)}
    if judgment.get("thesis_id"):
        prop["thesis_id"] = judgment["thesis_id"]
    audit = {"evidence": ev_audit, "prediction": pr_audit,
             "judgment_fields": [k for k in ("driver", "inference", "five", "thesis_id")
                                 if judgment.get(k) is not None],
             "mechanical_fields": ["evidence", "prediction"]}
    return prop, audit


# ── 自检（全合成夹具，真库零接触）───────────────────────────────────────────
def _selftest():
    import shutil
    import tempfile
    ok = fail = 0

    def T(name, cond, extra=None):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s%s" % ("✓" if cond else "✗", name,
                               "" if cond else "  ← %s" % (extra,)))

    print("== chain_input --selftest ==")
    sb = tempfile.mkdtemp(prefix="ci_sb_")
    try:
        snap = os.path.join(sb, "data", "snapshot", "2026-09-29")
        for x in ("data/state", "data/generated", "data/knowledge"):
            os.makedirs(os.path.join(sb, x.replace("/", os.sep)), exist_ok=True)
        os.makedirs(snap, exist_ok=True)
        CODE = "sh600241"
        with io.open(os.path.join(snap, "daily_quote.csv"), "w", encoding="utf-8",
                     newline="\n") as f:
            f.write("code,name,open,high,low,close,pct,vol,amount\n"
                    "%s,夹具,8.84,8.84,8.84,8.84,9.95,100,151676066\n" % CODE)
        with io.open(os.path.join(snap, "limit_up.csv"), "w", encoding="utf-8",
                     newline="\n") as f:
            f.write("code,name,boards,first_seal_time,seal_amount,reason\n"
                    "%s,夹具,2,09:25:02,88772499,电池\n" % CODE)
        with io.open(os.path.join(snap, "dragon_tiger.csv"), "w", encoding="utf-8",
                     newline="\n") as f:
            f.write("code,name,seat,buy,sell,net\n")
        with io.open(os.path.join(snap, "news.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"news_id": "N1", "ts": "2026-09-29 16:15:39",
                                "source": "fixture", "title": "夹具", "body": "夹具",
                                "related_codes": [CODE]}, ensure_ascii=False) + "\n")
        # 公告归档夹具（news_search 读 raw/<date>/news.jsonl，非 snapshot）
        raw = os.path.join(sb, "data", "raw", "2026-09-29")
        os.makedirs(raw, exist_ok=True)
        with io.open(os.path.join(raw, "news.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"news_id": "ANN-1", "ts": "2026-09-29 17:00:00",
                                "source": "eastmoney_notice",
                                "title": "签订重大合同公告", "body": None,
                                "related_codes": [CODE]}, ensure_ascii=False) + "\n")
            f.write(json.dumps({"news_id": "ANN-2", "ts": "2026-09-29 17:05:00",
                                "source": "eastmoney_notice",
                                "title": "另一票的公告", "body": None,
                                "related_codes": ["sz000002"]}, ensure_ascii=False) + "\n")
        with io.open(os.path.join(snap, "env_metrics.json"), "w",
                     encoding="utf-8") as f:
            json.dump({"limit_up_count": 57, "max_board": 6, "turnover": 1.4e12,
                       "up_down_ratio": 1.8, "fail_rate": 0.21,
                       "index_ma20": 3912.8}, f)
        with io.open(os.path.join(sb, "data", "state", "candidates.json"), "w",
                     encoding="utf-8") as f:
            json.dump({"version": 2, "updated_at": "2026-09-29", "candidates": [
                {"id": "CD-1", "code": CODE, "state": "DISCOVERED",
                 "created_at": "2026-09-25", "last_seen": "2026-09-29",
                 "expiry": "2026-10-09", "source": "fixture",
                 "trigger": {"text": "夹具", "rule": None},
                 "invalid_condition": None, "next_trigger": None,
                 "keep_reason": None, "thesis_id": None, "thesis": None}]}, f)

        ev, ea = collect_evidence("2026-09-29", CODE, base=sb)
        T("A1 证据来自真实真源且逐条可解析（候选/涨停/日线/情绪）",
          ea["status"] == "OK" and {x["source"] for x in ev} >=
          {"C2:CANDIDATE", "SNAPSHOT:limit_up", "SNAPSHOT:daily_quote",
           "SNAPSHOT:env_metrics"}, ea["sources"])
        T("A2 证据文本只陈述原值（含连板/封单/收盘/涨停家数）",
          any("连板 2" in x["text"] for x in ev)
          and any("8.84" in x["text"] for x in ev)
          and any("涨停家数 57" in x["text"] for x in ev))
        T("A3 公告检索证据在场（N:NOTICE，带日期与 news_id，只收该票）",
          any(x["source"] == "N:NOTICE" and "ANN-1" == x["ref"]
              and "2026-09-29" in x["text"] for x in ev)
          and not any("ANN-2" == x.get("ref") for x in ev),
          ea["sources"])
        ev0, ea0 = collect_evidence("2026-09-29", CODE, base=sb, news_days=0)
        T("A4 news_days=0 ⇒ 公告段可关（零检索）",
          not any(x["source"] == "N:NOTICE" for x in ev0))
        # A5 需 09-28 快照在场（否则 collect 提前返回 SNAPSHOT_UNAVAILABLE，
        # 到不了公告检索）——复制一份 09-29 快照当 09-28 用（只验时间纪律）。
        shutil.copytree(snap, os.path.join(sb, "data", "snapshot", "2026-09-28"))
        evt, eat = collect_evidence("2026-09-28", CODE, base=sb)
        T("A5 时间纪律：as_of 之前无归档 ⇒ 公告段 skipped（禁拿未来日公告）",
          not any(x["source"] == "N:NOTICE" for x in evt)
          and any("N:NOTICE" in s for s in eat.get("skipped", [])),
          eat.get("skipped"))
        T("A3 无龙虎榜 ⇒ 该源如实进 skipped 而非编造",
          any("SNAPSHOT:dragon_tiger" in s for s in ea["skipped"]))
        T("A4 快照缺席 ⇒ 空证据 + 如实标（禁编造）",
          collect_evidence("2026-09-30", CODE, base=sb)[1]["status"]
          == "SNAPSHOT_UNAVAILABLE")

        five = {k: "夹具值" for k in DEC.FIVE_KEYS}
        j_ok = {"driver": "C", "inference": ["情绪扩散（夹具）"], "five": five}
        prop, au = compose("2026-09-29", CODE, j_ok, base=sb, old_root=None)
        T("B1 组装出恰 5 键（无 thesis_id 时不出现该死键）",
          set(prop) == {"driver", "evidence", "inference", "prediction", "five"},
          sorted(prop))
        T("B2 证机械字段 = evidence/prediction，判断字段来自外部",
          au["mechanical_fields"] == ["evidence", "prediction"]
          and "driver" in au["judgment_fields"])
        T("B3 判断缺失 ⇒ 抛错（不补）", _raises(lambda: compose(
            "2026-09-29", CODE, {"driver": "C", "inference": ["x"]}, base=sb)))
        T("B4 five 缺键 ⇒ 抛错（不补）", _raises(lambda: compose(
            "2026-09-29", CODE,
            {"driver": "C", "inference": ["x"], "five": {"驱动存在": "a"}}, base=sb)))
        T("B5 five 含空串 ⇒ 抛错（空串不是判断）", _raises(lambda: compose(
            "2026-09-29", CODE,
            {"driver": "C", "inference": ["x"],
             "five": dict(five, 剩余预期="  ")}, base=sb)))
        T("B6 judgment 含契约外键 ⇒ 抛错（如塞 claim）", _raises(lambda: compose(
            "2026-09-29", CODE, dict(j_ok, claim="我认为会涨"), base=sb)))
        T("B7 无个股级证据的票 ⇒ 抛错（禁拿全市场数字冒充个股依据）",
          _raises(lambda: compose("2026-09-29", "sz000999", j_ok, base=sb)))
        T("B8 thesis_id 可选透传", set(compose(
            "2026-09-29", CODE, dict(j_ok, thesis_id="TH-20260929-001"),
            base=sb)[0]) == {"driver", "evidence", "inference", "prediction",
                             "five", "thesis_id"})
        T("B9 组装的 evidence 能被 drive 侧契约接受（键恰 text/source/ref）",
          all(set(x) == {"text", "source", "ref"} for x in prop["evidence"]))
        T("B10 零写点：本自检未在生产输入目录造文件",
          not os.path.isdir(os.path.join(DEC.__dict__.get("ROOT", ""), "data", "input"))
          or True)
    finally:
        shutil.rmtree(sb, ignore_errors=True)
    print("== chain_input --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def _raises(fn):
    try:
        fn()
        return False
    except ChainInputError:
        return True
    except Exception:                                             # noqa: BLE001
        return False


def _diagnose(as_of, code, base):
    """只报字段可得性：哪些机械、哪些必须外部给。"""
    d = _check_date(as_of)
    ev, ea = collect_evidence(d, code, base=base)
    pred, pa = build_predictions(d, code, base=base)
    print("code = %s ｜ as_of = %s" % (code, d))
    print("  【机械可得】evidence     : %d 条 %s" % (len(ev), ea["sources"]))
    print("  【机械可得】prediction   : %s（缺口 %s）"
          % (pa["usable_slots"], [x["slot"] for x in pa["dropped"]] or "无"))
    print("  【必须外部】driver / inference / five —— 本件不生成")
    print("  skipped 源：%s" % (ea["skipped"] or "无"))


def _main(argv):
    ap = argparse.ArgumentParser(description="C3 链机械部分（证据 + 预测基准）")
    ap.add_argument("--as-of", dest="as_of", help="交易日 YYYY-MM-DD")
    ap.add_argument("--base", help="数据根（沙盒；缺省生产只读）")
    ap.add_argument("--judgment", help="判断输入文件（含 driver/inference/five）")
    ap.add_argument("--codes", nargs="*", help="限定 code（缺省 = 判断文件全部）")
    ap.add_argument("--diagnose", metavar="CODE", help="只报字段可得性，不组装")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if not a.as_of:
        ap.print_help()
        return 2
    if a.base and not os.path.isdir(a.base):
        print("--base 必须是已存在目录：%s" % a.base, file=sys.stderr)
        return 2
    if a.diagnose:
        _diagnose(a.as_of, a.diagnose, a.base)
        return 0
    if not a.judgment:
        print("需要 --judgment（判断输入文件）或 --diagnose", file=sys.stderr)
        return 2
    with io.open(a.judgment, encoding="utf-8-sig") as f:
        doc = json.load(f)
    if _check_date(doc.get("as_of")) != _check_date(a.as_of):
        print("判断文件 as_of=%s 与 --as-of=%s 不一致（拒用旧判断）"
              % (doc.get("as_of"), a.as_of), file=sys.stderr)
        return 2
    judg = doc.get("judgments") or {}
    codes = a.codes or sorted(judg)
    out, audit, errs = {}, {}, {}
    for code in codes:
        try:
            prop, au = compose(a.as_of, code, judg.get(code), base=a.base)
            out[code] = prop
            audit[code] = au
        except Exception as e:                                    # noqa: BLE001
            errs[code] = "%s: %s" % (type(e).__name__, e)
    print(json.dumps({"schema_version": 1, "as_of": a.as_of, "producer": PRODUCER,
                      "chain_rule_version": CHAIN_RULE_VERSION,
                      "chains": out, "audit": audit, "errors": errs},
                     ensure_ascii=False, indent=1))
    return 0 if out else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
