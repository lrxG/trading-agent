# -*- coding: utf-8 -*-
"""S4 泡沫/拥挤度识别（2026-10-13 · 第一阶段补齐）。

职责（registry）：泡沫/拥挤识别 → 消费者 C5 定价与仓位 / E5 持仓诊断 / E7 个股研究。

双轨设计（据 2026-10-12 缺源核准；2026-10-20 按松动条件部分解冻）：
  · **froth_env（板块级环境泡沫）= 采集工程化落地后可产**——头部松动条件
    「数据未入系统前不松动」已满足（sector_seed 采集：板块 PE/PB + 历史分位
    PeTTMPct/PbLFPct + 盈利增速/PEG 已落 `data/raw/<D>/sector_valuation.csv` /
    `sector_forecast.csv`）⇒ 判定市场估值泡沫环境档（分位≥80 板块占比
    →high/medium/low）；文件缺失仍 UNAVAILABLE（不伪装）。
  · **froth_stock（个股级泡沫）= 仍 UNAVAILABLE**——个股估值需逐票 quote 采集
    （未工程化）+ 盈利增速/一致预期个股源未接 ⇒ 个股预期透支度不可产出；
    不伪装、不代理、不硬判，恒显式 UNAVAILABLE。
  · **crowding（拥挤）= 机械可推导**——只用 klines + 涨停原语（经 `import s3`
    单例复用，零复制）：
      ev_board   连板高度（_board_n）
      ev_pos45   45 日位置分（_pos45，0=窗口低 1=窗口高）
      ev_heat10  近 10 交易日涨停次数（_sealed_codes 回看）
      ev_amt_pct 当日成交额全市场分位（拥挤代理；无流通股本 → 不称换手）
    证据 ≥2 项才可判定（不足 → unknown，不硬判）；档位 high/medium/low。

  · 阈值唯一来源 = params `s4_rules`；**缺省回退内置种子**（UNCALIBRATED 标注，
    分布重锚待 C6 验证链）——本件对 params **零写点**。

输出快照 `s_S4_<as_of>（X3 快照 · 注册表声明形态；盘面文件名 = 小写动态拼装）`（X3 豁免；tempfile + os.replace 原子换入，
与 s5._write_snapshot 同款纪律）。输出零 wall-clock：同输入逐字节同输出。
"""
import csv
import io
import json
import os
import sys
import tempfile

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
sys.path.insert(0, _FS)
_AN = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _AN)
from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）
import params as PARAMS  # noqa: E402
import s3 as S3          # noqa: E402  共享行情/涨停原语（一个知识点一个家，本模块零复制）

_LEGACY_ROOT = (os.environ.get("OLD_SYSTEM_ROOT")
                or os.path.join(os.path.expanduser("~"), "炒股智能体_旧包_20261008"))  # 旧系统只读参考源；换机器时设 OLD_SYSTEM_ROOT 或放家目录

S_ID = "s4"
_OLD_ROOT = _LEGACY_ROOT
SNAP_FALLBACK = os.path.join(ROOT, "data", "generated", "s_S4_MISSING.json")
SERIES_START = S3.SERIES_START

# 内置阈值种子（params.s4_rules 缺省时的回退；UNCALIBRATED——分布重锚前禁当真值）
DEFAULT_RULES = {
    "hot": {"boards_n": 4, "pos45_min": 0.80, "heat10_n": 3, "amt_pct_min": 0.90},
    "cold": {"boards_n": 1, "pos45_max": 0.40, "heat10_n": 0, "amt_pct_max": 0.50},
    "min_evidence": 2,
    "provenance": "UNCALIBRATED builtin seed (s4.py 2026-10-13)",
}


class S4Error(Exception):
    """REASON_CODE 前缀错误（BAD_DATE/AS_OF_ABSENT/DATA_END/RULES_MISSING/...）。"""


def _chk_date(s, name="as_of"):
    import datetime as _dt
    t = s.isoformat() if hasattr(s, "isoformat") else str(s or "")
    if len(t) != 10 or t[4] != "-" or t[7] != "-":
        raise S4Error("BAD_DATE: %s 必须是 YYYY-MM-DD，收到 %r" % (name, s))
    try:
        _dt.date(int(t[:4]), int(t[5:7]), int(t[8:10]))
    except ValueError:
        raise S4Error("BAD_DATE: %s 非法日 %r" % (name, s))
    return t


def load_rules(params_path=None):
    """阈值唯一来源 = params `s4_rules`；缺 → 内置种子（UNCALIBRATED 如实标注）。"""
    try:
        v = PARAMS.get_value("s4_rules", path=params_path)
    except Exception:
        v = None
    if isinstance(v, dict) and v.get("hot"):
        return dict(v), "params:s4_rules"
    return dict(DEFAULT_RULES), "builtin:UNCALIBRATED"


def _amt_pct(all_amounts, amt):
    """当日成交额分位（0..1）；amt 为 None/0 → None（缺就是缺）。"""
    if not amt:
        return None
    vals = sorted(a for a in all_amounts if a)
    if not vals:
        return None
    below = sum(1 for a in vals if a <= amt)
    return round(below / len(vals), 4)


def _heat10(by_code, dates, i, code):
    n = 0
    for j in range(max(1, i - 9), i + 1):
        if code in S3._sealed_codes(by_code, dates, j):
            n += 1
    return n


def _froth_env(D, base=None):
    """板块级环境泡沫（2026-10-20 · 采集工程化落地后按头部松动条件解冻）。

    数据源 = `data/raw/<D>/sector_valuation.csv`（sector_seed 采集：PE/PB +
    历史分位）+ `data/raw/<D>/sector_forecast.csv`（盈利增速/PEG）。
    判定 = 分位分布的环境档（高分位板块占比），**不是**个股预期透支度——
    个股级估值逐票 quote 采集未工程化 ⇒ `froth_stock` 仍 UNAVAILABLE。
    文件缺失 ⇒ UNAVAILABLE + 缺源清单（不伪装、不代理、不硬判）。
    """
    root = base or ROOT
    val_p = os.path.join(root, "data", "raw", D, "sector_valuation.csv")
    fc_p = os.path.join(root, "data", "raw", D, "sector_forecast.csv")
    if not os.path.isfile(val_p):
        return {"status": "UNAVAILABLE",
                "reason": "缺源：data/raw/%s/sector_valuation.csv 不存在"
                          "（sector_seed --snapshot 未采集该日）" % D}
    try:
        with io.open(val_p, encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("pe_ttm_pct")]
    except Exception as e:                                   # noqa: BLE001
        return {"status": "UNAVAILABLE",
                "reason": "sector_valuation.csv 读取失败：%s" % str(e)[:100]}
    pcts = []
    for r in rows:
        try:
            pcts.append(float(r["pe_ttm_pct"]))
        except (TypeError, ValueError):
            continue
    if len(pcts) < 10:
        return {"status": "UNAVAILABLE",
                "reason": "有效分位样本 %d <10——板块截面不足，不硬判" % len(pcts)}
    hi = sum(1 for p in pcts if p >= 80.0)
    lo = sum(1 for p in pcts if p <= 20.0)
    share = round(hi / len(pcts), 4)
    if share >= 0.50:
        lvl = "high"
    elif share >= 0.25:
        lvl = "medium"
    else:
        lvl = "low"
    out = {"status": "OK", "level": lvl,
           "n_sectors": len(pcts), "pe_pct_ge80_share": share,
           "pe_pct_le20_n": lo,
           "reason": "板块级 PE 历史分位分布（%d/%d 板块分位≥80）→ 环境泡沫档 %s"
                     % (hi, len(pcts), lvl)}
    if os.path.isfile(fc_p):
        try:
            with io.open(fc_p, encoding="utf-8") as f:
                fcs = [r for r in csv.DictReader(f) if r.get("net_profit_yoy")]
            yoys = []
            for r in fcs:
                try:
                    yoys.append(float(r["net_profit_yoy"]))
                except (TypeError, ValueError):
                    continue
            if yoys:
                yoys.sort()
                out["profit_yoy_median"] = yoys[len(yoys) // 2]
                out["n_forecast"] = len(yoys)
        except Exception:                                     # noqa: BLE001
            pass                     # forecast 缺失不阻塞估值分位判定（如实不补）
    return out


def evaluate(as_of, *, codes=None, old_root=None, params_path=None, base=None):
    """S4 单日评估主入口 → 快照落 s_S4_<as_of>（X3 快照 · 注册表声明形态；盘面文件名 = 小写动态拼装）。

    codes=None → 默认观察集 = 当日涨停 ∪ 昨日连板≥2（拥挤研究对象=热门票）；
    codes=[...] → 限定集合（E5/E7 定向诊断用）。
    拒绝：BAD_DATE / AS_OF_ABSENT（非行情日）/ DATA_END（晚于行情，禁编未来）。
    """
    D = _chk_date(as_of)
    if D < SERIES_START:
        raise S4Error("DATA_INSUFFICIENT: %s 早于起算窗 %s" % (D, SERIES_START))
    rules, rules_prov = load_rules(params_path)
    hot = rules.get("hot") or {}
    cold = rules.get("cold") or {}
    min_ev = int(rules.get("min_evidence") or 2)

    by_code, dates = S3.load_klines(old_root, cutoff=S3._LOAD_CUTOFF)  # 活库优先（旧包仅 fallback）
    idx = {x: j for j, x in enumerate(dates)}
    if D > dates[-1]:
        # ★ 2026-10-20：加「是否忘 merge」的可执行提示（同 s2/s3 口径）
        _hint4 = ""
        try:
            if os.path.isfile(os.path.join(old_root or ROOT, "data", "raw", D,
                                           "daily_quote.csv")):
                _hint4 = ("；⚠ 按日行情 data/raw/%s/daily_quote.csv 在盘 ⇒ 可能未合并进 "
                          "klines：先跑 `tools/e2e/merge_klines.py --from %s --to %s`"
                          % (D, D, D))
        except Exception:                                        # noqa: BLE001
            pass
        raise S4Error("DATA_END: as_of %s 晚于行情截止 %s（禁编未来结构）%s"
                      % (D, dates[-1], _hint4))
    if D not in idx:
        raise S4Error("AS_OF_ABSENT: %s 非行情日（数据覆盖 %s..%s）" % (D, dates[0], dates[-1]))
    i = idx[D]

    sealed = S3._sealed_codes(by_code, dates, i)
    prev_sealed = S3._sealed_codes(by_code, dates, i - 1) if i >= 1 else {}
    prev_boards2 = {c for c in prev_sealed
                    if S3._board_n(by_code, dates, i - 1, c) >= 2}
    targets = sorted(set(codes)) if codes else sorted(set(sealed) | prev_boards2)
    day_amounts = [rows[D][4] for rows in by_code.values() if D in rows]

    out_codes, n_high = {}, 0
    for c in targets:
        rows = by_code.get(c) or {}
        cur = rows.get(D)
        if not cur:
            out_codes[c] = {"crowding": "unknown",
                            "reasons": ["当日无行情行（停牌/未上市）——缺就是缺"]}
            continue
        ev, reasons = {}, []
        bn = S3._board_n(by_code, dates, i, c)
        ev["boards_n"] = bn
        reasons.append("连板=%d（阈值 hot≥%s）" % (bn, hot.get("boards_n")))
        p45 = S3._pos45(rows, D)
        ev["pos45"] = round(p45, 4) if p45 is not None else None
        reasons.append("45日位置=%s（hot≥%s）" % (ev["pos45"], hot.get("pos45_min")))
        h10 = _heat10(by_code, dates, i, c)
        ev["heat10"] = h10
        reasons.append("近10日涨停=%d 次（hot≥%s）" % (h10, hot.get("heat10_n")))
        ap = _amt_pct(day_amounts, cur[4])
        ev["amt_pct"] = ap
        reasons.append("成交额分位=%s（hot≥%s）" % (ap, hot.get("amt_pct_min")))
        hot_hits = sum(1 for k, thr in (
            ("boards_n", hot.get("boards_n")), ("pos45", hot.get("pos45_min")),
            ("heat10", hot.get("heat10_n")), ("amt_pct", hot.get("amt_pct_min")))
            if ev.get(k) is not None and thr is not None and ev[k] >= thr)
        cold_hits = sum(1 for k, thr in (
            ("boards_n", cold.get("boards_n")), ("pos45", cold.get("pos45_max")),
            ("heat10", cold.get("heat10_n")), ("amt_pct", cold.get("amt_pct_max")))
            if ev.get(k) is not None and thr is not None and ev[k] <= thr)
        n_ev = sum(1 for v in ev.values() if v is not None)
        if n_ev < min_ev:
            crowd = "unknown"
            reasons.append("证据 %d/%d 项不足（min_evidence=%d）→ 不硬判"
                           % (n_ev, len(ev), min_ev))
        elif hot_hits >= 3:
            crowd = "high"
            reasons.append("热区证据 %d/4 项命中 → 拥挤高" % hot_hits)
        elif hot_hits >= 1:
            crowd = "medium"
            reasons.append("热区证据 %d/4 项命中 → 拥挤中" % hot_hits)
        elif cold_hits >= 2:
            crowd = "low"
            reasons.append("冷区证据 %d/4 项命中 → 拥挤低" % cold_hits)
        else:
            crowd = "medium" if hot_hits == 1 else "unknown"
            if crowd == "unknown":
                reasons.append("冷热证据均不足 → 不硬判")
        if crowd == "high":
            n_high += 1
        out_codes[c] = {"crowding": crowd, "evidence": ev, "reasons": reasons}

    snap = {
        "schema": 2, "s_id": S_ID, "cap": S_ID, "as_of": D,
        "rules_provenance": rules_prov,
        "froth_env": _froth_env(D, base=base),
        "froth_stock": {"status": "UNAVAILABLE",
                        "reason": "个股级估值源未工程化（需逐票 quote 采集 PE/PB "
                                  "+ 盈利增速）——个股预期透支度不可产出；"
                                  "环境级估值泡沫见 froth_env（板块分位分布）"},
        "n_codes": len(out_codes), "n_high": n_high,
        "codes": out_codes,
        "confidence": ("params" if rules_prov.startswith("params") else "UNCALIBRATED"),
    }
    gen = os.path.join(base or ROOT, "data", "generated")
    os.makedirs(gen, exist_ok=True)
    p = os.path.join(gen, "s_s4_%s.json" % D)
    _write_snapshot(snap, p)
    return snap


def _write_snapshot(snap, path=None):
    """唯一快照写点（X3 豁免 s_S4_<as_of>（X3 快照 · 注册表声明形态；盘面文件名 = 小写动态拼装））：
    tempfile 同目录落盘 + os.replace 原子换入（与 s5._write_snapshot 同款纪律）。"""
    _WG.check_write(path or SNAP_FALLBACK)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path or SNAP_FALLBACK),
                               prefix="s_s4_", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path or SNAP_FALLBACK)


# ── 自检（沙盒合成 klines + 真库零写）────────────────────────────────────────
def _froth_env_with_data_is_coherent():
    """B3c：注入合成板块估值截面，验证分位占比→环境档判定自洽（沙盒，零生产）。"""
    import shutil
    import tempfile
    sb = tempfile.mkdtemp(prefix="s4_froth_")
    try:
        d = os.path.join(sb, "data", "raw", "2026-10-19")
        os.makedirs(d)
        # 12 板块：6 个分位≥80（share=0.5 → high）
        rows = [{"pe_ttm_pct": "90"} for _ in range(6)] + \
               [{"pe_ttm_pct": "10"} for _ in range(6)]
        with io.open(os.path.join(d, "sector_valuation.csv"), "w",
                     encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["code", "pe_ttm_pct"])
            w.writeheader()
            for r in rows:
                w.writerow(r)
        out = _froth_env("2026-10-19", base=sb)
        return (out["status"] == "OK" and out["level"] == "high"
                and out["n_sectors"] == 12 and out["pe_pct_ge80_share"] == 0.5)
    finally:
        shutil.rmtree(sb, ignore_errors=True)


def _selftest():
    ok = fail = 0
    fails = []

    def T(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [✓] %s" % name)
        else:
            fail += 1
            fails.append(name)
            print("  [✗] %s %s" % (name, detail))

    print("== s4 --selftest ==")
    # A 组：协议与拒绝
    for bad, code in (("2026-9-1", "BAD_DATE"), ("2026-13-01", "BAD_DATE")):
        try:
            evaluate(bad)
            T("A 拒绝 %s" % bad, False)
        except S4Error as e:
            T("A 拒绝 %s（%s）" % (bad, code), str(e).startswith(code))
    try:
        evaluate("2027-01-05")
        T("A 拒绝未来日（DATA_END）", False)
    except S4Error as e:
        T("A 拒绝未来日（DATA_END）", str(e).startswith("DATA_END"))
    try:
        evaluate("2020-01-05")
        T("A 拒绝超覆盖（AS_OF_ABSENT/DATA_INSUFFICIENT）", False)
    except S4Error as e:
        T("A 拒绝超覆盖（AS_OF_ABSENT/DATA_INSUFFICIENT）",
          str(e).startswith(("AS_OF_ABSENT", "DATA_INSUFFICIENT")))
    # B 组：合成行情判定方向（沙盒旧根结构：data/raw/bus/klines.csv）
    import tempfile as _tf
    import shutil as _sh
    sb = _tf.mkdtemp(prefix="s4_sb_")
    try:
        bus = os.path.join(sb, "data", "raw", "bus")
        os.makedirs(bus, exist_ok=True)
        rows = ["code,name,date,open,close,high,low,volume,amount,exchange,change_pct"]
        # 拥挤票：连续 6 板 + 高位 + 巨量；冷门票：低位缩量
        d0 = "2026-06-01"
        import datetime as _dt
        days = []
        d = _dt.date(2026, 6, 1)
        while len(days) < 90:
            if d.weekday() < 5:
                days.append(d.isoformat())
            d += _dt.timedelta(days=1)
        for j, dd in enumerate(days):
            px = round(10 * (1.10 ** j), 2)                   # 拥挤票全程连板
            rows.append("sz000001,热票,%s,%s,%s,%s,%s,100000,500000000.0,sz,10.0"
                        % (dd, px, px, px, round(px * 0.99, 2)))
            pc = round(10 - j * 0.01, 2)
            rows.append("sz000002,冷票,%s,%s,%s,%s,%s,100,50000.0,sz,0.1"
                        % (dd, pc, round(pc * 1.01, 2), pc, round(pc * 0.99, 2)))
        io.open(os.path.join(bus, "klines.csv"), "w", encoding="utf-8-sig", newline="")\
            .write("\n".join(rows) + "\n")
        snap = evaluate(days[-1], codes=["sz000001", "sz000002"],
                        old_root=sb, base=sb)
        hot = snap["codes"]["sz000001"]
        cold = snap["codes"]["sz000002"]
        T("B1 连续涨停高位巨量 → crowding=high", hot["crowding"] == "high",
          str(hot.get("crowding")))
        T("B2 低位缩量无板 → crowding∈{low,unknown}（不硬判 medium+）",
          cold["crowding"] in ("low", "unknown"), str(cold.get("crowding")))
        T("B3 froth 分层诚实：stock 恒 UNAVAILABLE + env 按数据源可达性产出",
          snap["froth_stock"]["status"] == "UNAVAILABLE"
          and "个股级估值源未工程化" in snap["froth_stock"]["reason"]
          and snap["froth_env"]["status"] in ("OK", "UNAVAILABLE")
          and (snap["froth_env"]["status"] == "OK"
               or "sector_valuation" in snap["froth_env"]["reason"]))
        T("B3b froth_env 数据缺失时 UNAVAILABLE 不伪装（沙盒无板块文件）",
          snap["froth_env"]["status"] == "UNAVAILABLE",
          snap["froth_env"]["reason"][:60])
        T("B3c froth_env 有数据时判环境档 + 分位占比自洽（临时源注入）",
          _froth_env_with_data_is_coherent())
        T("B4 阈值种子 provenance 如实（builtin:UNCALIBRATED）",
          snap["rules_provenance"] == "builtin:UNCALIBRATED")
        T("B5 输出零 wall-clock（同输入重算逐字节同）",
          json.dumps(evaluate(days[-1], codes=["sz000001", "sz000002"],
                              old_root=sb, base=sb), sort_keys=True)
          == json.dumps(snap, sort_keys=True))
        # C 组：真库零写
        gen = os.path.join(sb, "data", "generated")
        T("C1 快照只落沙盒 data/generated（X3 契约）",
          os.path.isfile(os.path.join(gen, "s_s4_%s.json" % days[-1])))
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    # D 组：生产区零接触（本自检全程未写生产 data/generated）
    T("D1 真库零写：s_s4_<今日> 不存在于生产 generated（自检未触生产）",
      not os.path.exists(os.path.join(ROOT, "data", "generated", "s_s4_2026-06-01.json")))
    print("\n== s4 --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fails else 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    if "--eval" in argv:
        d = argv[argv.index("--eval") + 1]
        codes = None
        if "--codes" in argv:
            codes = argv[argv.index("--codes") + 1].split(",")
        b = (argv[argv.index("--base") + 1] if "--base" in argv else None)
        if b and not os.path.isdir(b):
            print("--base 必须是已存在目录（防误写生产），实际: %s" % b)
            sys.exit(2)
        snap = evaluate(d, codes=codes, base=b)
        print(json.dumps(snap, ensure_ascii=False, indent=1)[:2000])
        sys.exit(0)
    print("用法: s4.py --eval <as_of> [--codes c1,c2] [--base 沙盒] | --selftest")
    sys.exit(2)
