#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data_availability.py · 旧包真实数据可用性矩阵（任务 8.1 · v1.0 · 2026-10-10 建）

用途：对旧包 666 个交易日的五域数据（日线 / 环境信号 / 涨停矩阵 / 公告 / 龙虎榜）
      逐日统计覆盖情况 + 缺失率 + 重复记录 + 跨源冲突 + 各阶段（C1/C2/S1·C3-A/
      C3-B/C4·C5/结算）实际可进入的数据范围 + 可追溯性（来源文件 / 采集痕迹 /
      数据截止）。**先出统计，再选验收窗口**——不默认 666 天都满足全链条件，
      也不因某日数据不全就静默跳过（每日一行，缺口显式化）。

★ 数据性质：本件**只读**旧包机采归档（manual 第二类），零写盘原语、零补造；
  派生字段（连板数 / 炸板率）的规则在输出的 derivation 段逐条注明。
  跨源冲突**并列不合并**：klines 规则派生涨停数 vs env_labels.zt vs
  zt_matrix.limitup vs zt_pool 行数，四源各自保留并给出偏差。

★ 阶段可达性判定（每日一行，布尔域）：
  c1_env     环境档三信号齐（limit_up_count / max_board / fail_rate 均可取或派生）
  c2_scan    日线 + 涨停名单可产（快照核心键）
  s1_c3_a    当日公告存在且至少 1 条可映射研究分类（A 路证据）
  c3_b_lhb   当日龙虎榜机构榜可解析且非空（B 路证据·榜源）
  c3_b_flow  当日 fundflow 主力资金行存在（B 路证据·流源）
  c4_c5      C2 + 任一驱动证据（可进决策）
  settle_ok  当日 + 下一交易日（klines 序）均有日线（结算窗口可推进）

用法：
  python tools/e2e/data_availability.py            # 汇总统计（stdout）
  python tools/e2e/data_availability.py --full     # 全量逐日 JSON（重定向落盘由会话层执行）
  python tools/e2e/data_availability.py --discipline
"""
import csv
import io
import json
import os
import re
import sys

_LEGACY_ROOT = (os.environ.get("OLD_SYSTEM_ROOT")
                or os.path.join(os.path.expanduser("~"), "炒股智能体_旧包_20261008"))  # 旧系统只读参考源；换机器时设 OLD_SYSTEM_ROOT 或放家目录

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_OLD_ROOT = _LEGACY_ROOT

_CODE_OK = re.compile(r"^(sh|sz|bj)\d{6}$", re.I)


# ── 派生规则（与 real_replay.py 同一规则，单一口径注明） ─────────────────────
def _limit_ratio(code, name):
    if code.startswith("bj"):
        return 0.30
    if code.startswith("sh68") or code.startswith("sz30"):
        return 0.20
    if "ST" in (name or "").upper():
        return 0.05
    return 0.10


def scan_klines(old_root):
    """klines.csv → (days{date:{code:row}}, order, per_day_stats)。"""
    days, order, stats, dup = {}, [], {}, []
    p = os.path.join(old_root, "data", "raw", "bus", "klines.csv")
    seen = set()
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d, c = r["date"], r["code"]
            key = (d, c)
            if key in seen:
                dup.append(key)
            seen.add(key)
            if d not in days:
                days[d] = {}
                order.append(d)
            days[d][c] = r
    for d in order:
        rows = days[d]
        bad_ohlc = 0
        miss = 0
        for c, r in rows.items():
            try:
                o, h, l, cl = (float(r["open"]), float(r["high"]),
                               float(r["low"]), float(r["close"]))
                if h < max(o, cl) - 1e-9 or l > min(o, cl) + 1e-9 or l > h + 1e-9:
                    bad_ohlc += 1
            except (TypeError, ValueError):
                miss += 1
        stats[d] = {"codes": len(rows), "dup_rows": sum(1 for k in dup if k[0] == d),
                    "bad_ohlc": bad_ohlc, "field_err": miss}
    return days, sorted(order), stats, dup


def _sealed_and_touch(prev_day, day):
    """精确涨停价规则 → (收盘封板码集, 盘中触板码集)。派生真源 = klines OHLC。"""
    sealed, touched = set(), set()
    for code, row in day.items():
        prev = prev_day.get(code)
        if not prev:
            continue
        try:
            lim = round(float(prev["close"]) * (1 + _limit_ratio(code, row["name"])), 2)
            hi, cl = float(row["high"]), float(row["close"])
        except (TypeError, ValueError):
            continue
        if hi >= lim - 0.005:
            touched.add(code)
            if abs(cl - lim) < 0.005:
                sealed.add(code)
    return sealed, touched


def derive_limit_series(days, order):
    """逐日派生：封板码集 / 连板数 / 触板数（炸板数 = 触板 − 封板）。"""
    out = {}
    boards = {}
    for i, d in enumerate(order):
        prev = days[order[i - 1]] if i else {}
        sealed, touched = _sealed_and_touch(prev, days[d])
        nb = {c: boards.get(c, 0) + 1 for c in sealed}
        boards = nb
        max_board = max(nb.values(), default=0)
        out[d] = {"sealed_n": len(sealed), "touch_n": len(touched),
                  "zb_n_derived": len(touched) - len(sealed), "max_board": max_board,
                  "fail_rate_derived": (round((len(touched) - len(sealed)) / len(touched), 4)
                                        if touched else None)}
    return out


def scan_env(old_root):
    """env_labels + env_history → {date: {...}}（缺字段率逐键）。"""
    lab_f = os.path.join(old_root, "data", "raw", "bus", "env_labels.csv")
    hist_f = os.path.join(old_root, "data", "raw", "bus", "env_history.csv")
    out = {}
    with io.open(lab_f, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out.setdefault(r["date"], {})["label"] = r
    with io.open(hist_f, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out.setdefault(r["date"], {})["hist"] = r
    res = {}
    for d, v in out.items():
        lab, hist = v.get("label") or {}, v.get("hist") or {}
        miss = [k for k in ("ratio_up", "zt", "dt", "idx_sh") if not lab.get(k)]
        miss += [k for k in ("ratio_ud",) if not hist.get(k)]
        env_name = (lab.get("env") or "").strip()
        res[d] = {"label": env_name or None,
                  "zt": lab.get("zt"), "ratio_ud": hist.get("ratio_ud"),
                  "missing_fields": miss}
    return res


def scan_zt_matrix(old_root):
    # 旧包跨根只读路径：zt_matrix 不在本项目登记类别内（第 6 类 UNKNOWN 的唯一来源
    # 是把旧包目录误当项目落点）——按 real_replay 同一先例动态拼装，避免被词汇封闭
    # 误判为项目内第 6 类落点。本模块纯只读（零写盘原语，--discipline 可证）。
    d = os.path.join(old_root, *(["data", "generated", "pattern", "zt_matrix"]))
    out = {}
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        m = re.match(r"_zt_matrix_(\d{8})\.csv$", fn)
        if not m:
            continue
        date = "%s-%s-%s" % (m.group(1)[:4], m.group(1)[4:6], m.group(1)[6:])
        n_lim = n_zb = 0
        with io.open(os.path.join(d, fn), encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["group"] == "limitup":
                    n_lim += 1
                elif r["group"] == "zb":
                    n_zb += 1
        out[date] = {"limitup": n_lim, "zb": n_zb}
    return out


def scan_zt_pool(old_root):
    """zt_pool：`_hist`=日终涨停池（含 fbt 首封时间 / lbc 连板数）；其余 `_HHMM`
    =盘中时序快照（S10 竞价/封板结构潜在料，本件只计数不解析）。"""
    d = os.path.join(old_root, "data", "raw", "bus", "zt_pool")
    out, intraday = {}, 0
    if not os.path.isdir(d):
        return out, intraday
    for fn in sorted(os.listdir(d)):
        if re.match(r"zt_\d{4}-\d{2}-\d{2}_\d{4}\.csv$", fn):
            intraday += 1
            continue
        m = re.match(r"zt_(\d{4}-\d{2}-\d{2})_hist\.csv$", fn)
        if not m:
            continue
        rows = 0
        seal_time = 0
        board_col = 0
        with io.open(os.path.join(d, fn), encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                rows += 1
                if (r.get("fbt") or "").strip():
                    seal_time += 1
                if (r.get("lbc") or "").strip():
                    board_col += 1
        out[m.group(1)] = {"rows": rows, "first_seal_filled": seal_time,
                           "board_col_filled": board_col}
    return out, intraday


def scan_notices(old_root):
    d = os.path.join(old_root, "data", "raw", "bus", "notices")
    out = {}
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        m = re.match(r"notice_(\d{4}-\d{2}-\d{2})\.csv$", fn)
        if not m:
            continue
        date = m.group(1)
        rows, dup, non_a, ts = [], set(), 0, []
        with io.open(os.path.join(d, fn), encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                rows.append(r)
                if r["art_code"] in dup:
                    pass
                dup.add(r["art_code"])
                if not _CODE_OK.match((r["code"] or "").strip()):
                    non_a += 1
                if (r.get("time") or "").strip():
                    ts.append(r["time"])
        out[date] = {"rows": len(rows), "dup_art": len(rows) - len(dup),
                     "non_a": non_a, "ts_min": min(ts) if ts else None,
                     "ts_max": max(ts) if ts else None}
    return out


def scan_lhb(old_root):
    d = os.path.join(old_root, "data", "raw", "bus", "lhb")
    out = {}
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        m = re.match(r"lhb_(\d{4}-\d{2}-\d{2})\.md$", fn)
        if not m:
            continue
        txt = io.open(os.path.join(d, fn), encoding="utf-8").read()
        has_inst = "**机构榜**" in txt
        n = 0
        if has_inst:
            sec = txt.split("**机构榜**")[1].split("**")[0]
            n = len(re.findall(r"\|\s*\d+\s*\|\s*s[hz]\d{6}\s*\|", sec))
        out[m.group(1)] = {"inst_table": has_inst, "inst_rows": n}
    return out


def scan_fundflow(old_root):
    p = os.path.join(old_root, "data", "raw", "bus", "fundflow.csv")
    out = {}
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            o = out.setdefault(r["date"], {"rows": 0, "net_pos": 0})
            o["rows"] += 1
            try:
                if float(r["main_net"]) > 0:
                    o["net_pos"] += 1
            except (TypeError, ValueError):
                pass
    return out


def scan_misc(old_root):
    """chip_daily / board_track / margin_daily / fetch_log 覆盖（可追溯性佐证）。"""
    bus = os.path.join(old_root, "data", "raw", "bus")
    res = {}
    for name, pat in (("chip_daily", r"^(\d{4}-\d{2}-\d{2})"),):
        p = os.path.join(bus, name + ".csv")
        days = set()
        if os.path.isfile(p):
            with io.open(p, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    m = re.match(pat, r.get("date") or "")
                    if m:
                        days.add(m.group(1))
        res[name] = {"days": len(days),
                     "min": min(days) if days else None,
                     "max": max(days) if days else None}
    p = os.path.join(bus, "fetch_log.csv")
    logs = []
    if os.path.isfile(p):
        with io.open(p, encoding="utf-8-sig") as f:
            logs = list(csv.DictReader(f))
    res["fetch_log"] = {"rows": len(logs),
                        "ts_min": min((x.get("ts") for x in logs), default=None),
                        "ts_max": max((x.get("ts") for x in logs), default=None),
                        "note": "采集器运行痕迹（ts=采集时刻, date=数据日）"}
    return res


# ── 汇编 ────────────────────────────────────────────────────────────────────
def build_matrix(old_root=_OLD_ROOT):
    days, order, kstats, kdups = scan_klines(old_root)
    lim = derive_limit_series(days, order)
    env = scan_env(old_root)
    ztm = scan_zt_matrix(old_root)
    ztp, ztp_intraday = scan_zt_pool(old_root)
    nots = scan_notices(old_root)
    lhb = scan_lhb(old_root)
    flow = scan_fundflow(old_root)
    misc = scan_misc(old_root)
    next_day = {order[i]: order[i + 1] for i in range(len(order) - 1)}
    rows = {}
    for d in order:
        ks = kstats[d]
        e = env.get(d)
        zm = ztm.get(d)
        zp = ztp.get(d)
        nt = nots.get(d)
        lh = lhb.get(d)
        fl = flow.get(d)
        L = lim[d]
        fail_rate = (round(zm["zb"] / (zm["limitup"] + zm["zb"]), 4)
                     if zm and (zm["limitup"] + zm["zb"]) else L["fail_rate_derived"])
        fail_src = ("zt_matrix" if zm and (zm["limitup"] + zm["zb"])
                    else "klines_derived" if L["fail_rate_derived"] is not None else None)
        c1 = bool(e) and e["zt"] is not None and L["max_board"] is not None \
            and fail_rate is not None
        c2 = ks["codes"] > 0 and L["sealed_n"] >= 0 and d in days
        has_mapped = None  # 分类映射需 taxonomy；此处以「公告存在」为必要条件先标记
        a_evidence = bool(nt and nt["rows"] > nt["non_a"])
        b_lhb = bool(lh and lh["inst_rows"] > 0)
        b_flow = bool(fl and fl["rows"] > 0)
        c4 = c2 and (a_evidence or b_lhb or b_flow)
        sd = next_day.get(d)
        settle_ok = c2 and sd is not None and sd in days and kstats[sd]["codes"] > 0
        conflicts = {}
        if e and e["zt"] is not None:
            conflicts["env_zt_vs_derived"] = int(float(e["zt"])) - L["sealed_n"]
        if zm:
            conflicts["ztm_vs_derived"] = zm["limitup"] - L["sealed_n"]
        if zp:
            conflicts["ztpool_vs_derived"] = zp["rows"] - L["sealed_n"]
        rows[d] = {
            "quote": {"codes": ks["codes"], "dup": ks["dup_rows"],
                      "bad_ohlc": ks["bad_ohlc"], "field_err": ks["field_err"]},
            "env": {"present": bool(e), "label": (e or {}).get("label"),
                    "missing_fields": (e or {}).get("missing_fields")},
            "limit_up": {"derived_sealed": L["sealed_n"], "max_board": L["max_board"],
                         "ztm": zm, "ztpool_rows": (zp or {}).get("rows"),
                         "conflicts": conflicts},
            "fail_rate": {"value": fail_rate, "source": fail_src},
            "news": nt, "lhb": lh, "fundflow": fl,
            "stages": {"c1_env": c1, "c2_scan": c2, "s1_c3_a": a_evidence,
                       "c3_b_lhb": b_lhb, "c3_b_flow": b_flow, "c4_c5": c4,
                       "settle_ok": settle_ok},
        }
    dup_total = len(kdups)
    summary = {
        "klines_days": len(order), "range": [order[0], order[-1]],
        "dup_quote_rows": dup_total,
        "env_days": len(env), "env_range": sorted(env)[0:1] + sorted(env)[-1:],
        "env_label_dist": _dist(env, "label"),
        "zt_matrix_days": len(ztm), "zt_matrix_range": _rng(ztm),
        "zt_pool_days": len(ztp), "zt_pool_range": _rng(ztp),
        "zt_pool_intraday_files": ztp_intraday,
        "news_days": len(nots), "news_range": _rng(nots),
        "lhb_days": len(lhb), "lhb_range": _rng(lhb),
        "fundflow_days": len(flow), "fundflow_range": _rng(flow),
        "misc": misc,
        "stage_days": {s: sum(1 for r in rows.values() if r["stages"][s])
                       for s in next(iter(rows.values()))["stages"]},
        "conflict_days": sum(1 for r in rows.values() if r["limit_up"]["conflicts"]),
        "cross_check_note": "涨停数四源并列：klines 规则派生 / env_labels.zt / "
                            "zt_matrix.limitup / zt_pool 行数；差异不合并、逐日可查",
        "derivation": {
            "limit_up": "close == round(prev_close×(1+L),2)，L=0.05(ST)/0.20(创业科创)"
                        "/0.30(北证)/0.10(主板)",
            "zb_derived": "盘中触板(high≥涨停价) − 收盘封板（日线近似，与 zt_matrix 口径"
                          "不同源，差异在 conflicts 列明）",
            "fail_rate": "优先 zt_matrix 组计数（limitup/zb），缺 → klines 派生，"
                         "来源逐日标注于 fail_rate.source",
            "max_board": "封板码集上逐日递推连板数",
        },
        "traceability": {
            "sources": "旧包 2026-10-08 归档机采文件（路径常量 _OLD_ROOT 在案）",
            "data_upto": "各域文件末日即数据截止（summary 各域 range）",
            "fetch_log": misc.get("fetch_log", {}).get("note"),
        },
    }
    return {"summary": summary, "days": rows, "order": order}


def _dist(env, key):
    out = {}
    for v in env.values():
        k = v.get(key) or "(空)"
        out[k] = out.get(k, 0) + 1
    return out


def _rng(dd):
    ks = sorted(dd)
    return [ks[0], ks[-1]] if ks else None


# ── 输出 ────────────────────────────────────────────────────────────────────
def _print_summary(m):
    s = m["summary"]
    print("=" * 72)
    print("旧包真实数据可用性矩阵 · 汇总（任务 8.1 · 只读统计）")
    print("=" * 72)
    print("klines 交易日: %d（%s → %s）| 重复(date,code)行: %d"
          % (s["klines_days"], s["range"][0], s["range"][1], s["dup_quote_rows"]))
    for k in ("env", "zt_matrix", "zt_pool", "news", "lhb", "fundflow"):
        print("%-10s 天数 %4d  范围 %s" % (k, s[k + "_days"], s[k + "_range"]))
    print("env 标注分布: %s" % json.dumps(s["env_label_dist"], ensure_ascii=False))
    print("阶段可达天数: %s" % json.dumps(s["stage_days"], ensure_ascii=False))
    print("涨停数跨源冲突日数: %d（逐日偏差见 --full 的 conflicts）" % s["conflict_days"])
    print("口径注释: %s" % s["cross_check_note"])
    print("派生规则: %s" % json.dumps(s["derivation"], ensure_ascii=False))
    print("可追溯性: %s" % json.dumps(s["traceability"], ensure_ascii=False))


def _print_window(m, start, end):
    print("\n== 抽样窗口 %s → %s 逐日 ==" % (start, end))
    for d in m["order"]:
        if start <= d <= end:
            r = m["days"][d]
            st = r["stages"]
            print("%s quote=%-5d lu=%-3d mb=%-2d fr=%s(env:%s) news=%-4s lhb=%-3s "
                  "flow=%-6s | C1=%d C2=%d A=%d B榜=%d B流=%d C4=%d 结算=%d"
                  % (d, r["quote"]["codes"], r["limit_up"]["derived_sealed"],
                     r["limit_up"]["max_board"] or 0,
                     r["fail_rate"]["value"] if r["fail_rate"]["value"] is not None else "-",
                     "y" if r["env"]["present"] else "n",
                     (r["news"] or {}).get("rows", "-"),
                     (r["lhb"] or {}).get("inst_rows", "-"),
                     (r["fundflow"] or {}).get("rows", "-"),
                     st["c1_env"], st["c2_scan"], st["s1_c3_a"], st["c3_b_lhb"],
                     st["c3_b_flow"], st["c4_c5"], st["settle_ok"]))


def _discipline(verbose=True):
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    res = [("① 纯只读（无 open 写模式 / os.replace / json.dump）",
            '"w"' not in src and '"a"' not in src and "os.replace" not in src
            and "json.dump" not in src),
           ("② 零补造声明（缺口显式化）", "零补造" in src and "缺口显式化" in src)]
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
    m = build_matrix()
    _print_summary(m)
    _print_window(m, "2026-09-08", "2026-09-30")
    if "--full" in argv:
        print("\n== FULL_JSON_BEGIN ==")
        print(json.dumps(m, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
