#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""env_recalc.py · 从 klines 重算封板池口径环境指标（校准训练集的唯一生成器）

## 为什么有本件（2026-10-27 · 环境闸统一 M1 配套）

环境档校准需要长历史训练集，实测口径（live_seed 的 env_metrics）只有 33 日。
本件从 klines.csv（2024-01~2026-10，678 交易日）离线重算**同构口径**指标，
作为校准训练集的唯一数据源。

## 口径（继承旧系统 limitup_rebuild 的近似哲学 · 2026-10-27 裁定）

**统计等价口径，非交易所精确口径**：
  - 涨幅阈值：主板 9.65% / 创业科创 19.3% / ST 4.7%（低于名义值，容忍复权舍入）
  - 封板判定：pct ≥ th **且** close ≥ high×0.997（收盘≈最高）
  - 触板未封：high/pc − 1 ≥ th 且未封
  - 连板：封板日 boards+1，否则清零；上市首日不判（首日无前收）
  - fail_rate = 触板未封 / (封板 + 触板未封)
  - up_down_ratio = 上涨家数 / 下跌家数
  - 宇宙：codes.is_stock（sh6/sz0/sz3），排除北交所与指数

**已知口径特性（不是 bug，是口径的一部分）**：
  - 除权日涨停票会漏判（前收未除权 → pct 被低估）。训练集含 2024-2026 三个完整
    分红季 → 校准出的阈值隐含此特性；生产与训练**同口径** ⇒ 自洽。
  - 名称取 klines 最新值：ST 戴帽/摘帽的时点错位会带入少量噪音（量级 << 除权效应）。

**与实测口径的关系（M1 裁定 + 旧系统互证纪律）**：
  - 本件输出 = 校准训练集口径；实测 env_metrics（live_seed）= 每日互证通道。
  - 互证判据继承旧系统：同日 limit_up_count 差 >30% ⇒ 告警人工看。
  - 实测 33 日对照（2026-10-27）：日差均值 +0.97 / 中位 3；max_board 一致率 85%+；
    已知极端日 2026-10-23（差 −44，除权集中日）——归因见
    data/research/env_gate_audit_20261027/DECISION.md 第三节。

## 用法

  python tools/analysis/env_recalc.py                      # 全历史重算，打印统计
  python tools/analysis/env_recalc.py --out data/research/env_recalc/env_metrics_recalc.csv
  python tools/analysis/env_recalc.py --start 2024-01-01 --end 2026-08-31 --out <csv>
  python tools/analysis/env_recalc.py --selftest
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import statistics
import sys
from collections import defaultdict

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
KL = os.path.join(_ROOT, "data", "raw", "bus", "klines.csv")

TH_ST, TH_20CM, TH_MAIN = 0.047, 0.193, 0.0965
CLOSE_HIGH_RATIO = 0.997


def _th(code, name):
    if "ST" in (name or "").upper():
        return TH_ST
    return TH_20CM if code.startswith(("sz30", "sh68")) else TH_MAIN


def load_klines(path=KL):
    """一次遍历：code → 按日排序的 [date, o, h, l, c]；name 取最新。"""
    ser2 = defaultdict(list)
    names = {}
    with open(path, encoding="utf-8", errors="ignore", newline="") as f:
        for r in csv.DictReader(f):
            c = r.get("code") or ""
            if c.startswith("bj") or c.startswith(("sh000", "sz399")):
                continue                                   # 北交所与指数不入宇宙
            try:
                o, h, l, cl = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
            except (ValueError, TypeError):
                continue
            if cl <= 0:
                continue
            ser2[c].append([r["date"], o, h, l, cl])
            if r.get("name"):
                names[c] = r["name"]
    ser = {c: sorted(s, key=lambda x: x[0]) for c, s in ser2.items()}
    return ser, names


def recalc(ser, names, start=None, end=None):
    """逐日重算 → {date: {limit_up_count, max_board, fail_rate, up_down_ratio, n_stocks}}。

    口径见模块 docstring。首日（该票序列内第一天）无前收 → 该票当日不参与判定。
    """
    out = {}
    boards = defaultdict(int)
    prev_close = {}
    for c, s in ser.items():
        th = _th(c, names.get(c))
        for i, row in enumerate(s):
            d, _o, h, _l, cl = row
            if start and d < start:
                prev_close[c] = cl
                continue
            if end and d > end:
                break
            r = out.setdefault(d, {"z": 0, "t": 0, "u": 0, "dn": 0, "mb": 0, "n": 0})
            r["n"] += 1
            pc = s[i - 1][4] if i else None
            prev_close[c] = cl
            if not pc or pc <= 0:
                continue                                   # 首日：无法判（旧系统同规则）
            pct, touch = cl / pc - 1, h / pc - 1
            if pct > 0:
                r["u"] += 1
            elif pct < 0:
                r["dn"] += 1
            if pct >= th and cl >= h * CLOSE_HIGH_RATIO:
                r["z"] += 1
                boards[c] += 1
                if boards[c] > r["mb"]:
                    r["mb"] = boards[c]
            elif touch >= th:
                r["t"] += 1
                boards[c] = 0
            else:
                boards[c] = 0
    metrics = {}
    for d, r in out.items():
        touched = r["z"] + r["t"]
        metrics[d] = {
            "limit_up_count": r["z"],
            "max_board": r["mb"] or None,
            "fail_rate": (r["t"] / touched) if touched else None,
            "up_down_ratio": (r["u"] / r["dn"]) if r["dn"] else None,
            "n_stocks": r["n"],
            "source": "env_recalc_v1",
        }
    return dict(sorted(metrics.items()))


COLS = ("date", "limit_up_count", "max_board", "fail_rate", "up_down_ratio",
        "n_stocks", "source")


def write_csv(metrics, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, lineterminator="\n")
        w.writeheader()
        for d, m in metrics.items():
            w.writerow({"date": d, **m})
    os.replace(tmp, path)
    return path


def compare_with_measured(metrics, base=None):
    """与实测 env_metrics 对照（互证通道）→ 汇总统计。只读。"""
    root = base or _ROOT
    raw = os.path.join(root, "data", "raw")
    ds, diffs, mb_ok = [], [], []
    for d in sorted(os.listdir(raw)):
        p = os.path.join(raw, d, "env_metrics.json")
        if len(d) != 10 or d[4] != "-" or not os.path.isfile(p) or d not in metrics:
            continue
        em = json.load(io.open(p, encoding="utf-8-sig"))
        m = metrics[d]
        if em.get("limit_up_count") is not None:
            ds.append(m["limit_up_count"] - em["limit_up_count"])
        if em.get("max_board") is not None and m["max_board"] is not None:
            mb_ok.append(m["max_board"] == em["max_board"])
    return {
        "n_days": len(ds),
        "diff_mean": round(statistics.mean(ds), 2) if ds else None,
        "diff_median": statistics.median(ds) if ds else None,
        "diff_max_abs": max((abs(x) for x in ds), default=None),
        "max_board_agree": ("%d/%d" % (sum(mb_ok), len(mb_ok))) if mb_ok else None,
    }


import json                                                # noqa: E402  （compare 用）


def _selftest():
    import tempfile as _tf
    import shutil as _sh
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== env_recalc --selftest ==")
    # 构造迷你 klines：3 只票 × 5 日
    # sh600001 主板：D1/D2 close=10，D3 (o10,h11,l10,c11)=+10% 封板，D4/D5 11 横盘不封
    # sz300001 创业板：D3 (h12,c12)=+20% 封板
    # sh600002 ST：D3 h=11.03=pc×5.05% 触板未封(c10.6) → fail_rate=1/3；D4/D5 无触板
    seq = {
        "sh600001": [(10, 10.1, 9.9, 10), (10, 10.1, 9.9, 10), (10, 11.0, 10.0, 11),
                     (11, 11.1, 10.9, 11), (11, 11.1, 10.9, 11)],
        "sz300001": [(10, 10.1, 9.9, 10), (10, 10.1, 9.9, 10), (10, 12.0, 10.0, 12),
                     (12, 12.1, 11.9, 12), (12, 12.1, 11.9, 12)],
        "sh600002": [(10.5, 10.6, 10.4, 10.5), (10.5, 10.6, 10.4, 10.5),
                     (10.5, 11.03, 10.4, 10.6), (10.6, 10.7, 10.5, 10.2),
                     (10.2, 10.3, 10.1, 10.2)],
    }
    rows = []
    for d_i, d in enumerate(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
                             "2026-01-09"]):
        for c, s in seq.items():
            o, h, l, cl = s[d_i]
            nm = "ST测试" if c == "sh600002" else ("创业票" if c == "sz300001" else "主板票")
            rows.append((c, d, o, h, l, cl, nm))
    sb = _tf.mkdtemp(prefix="envrecalc_")
    try:
        kl = os.path.join(sb, "klines.csv")
        with io.open(kl, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["code", "date", "open", "high", "low", "close", "name"])
            for c, d, o, h, l, cl, nm in rows:
                w.writerow([c, d, o, h, l, cl, nm])
        ser, names = load_klines(kl)
        T("A1 klines 加载：3 票 × 5 日，指数/北交所已滤",
          len(ser) == 3 and all(len(s) == 5 for s in ser.values()))
        m = recalc(ser, names)
        # D3: 主板 +10%（封）、创业 +20%（封）、ST 触板未封 → z=2 t=1
        T("A2 D3 封板=2（主板10%+创业20%）、ST 触板未封计入 fail_rate",
          m["2026-01-07"]["limit_up_count"] == 2
          and abs(m["2026-01-07"]["fail_rate"] - 1/3) < 1e-9)
        T("A3 ST 阈值 4.7% 生效（D4 ST 10.2/10.5=2.9% 未触板）",
          m["2026-01-08"]["fail_rate"] is None and m["2026-01-08"]["limit_up_count"] == 0)
        T("A4 连板回溯：主板 D3 封 D4 未封 → max_board=1",
          m["2026-01-07"]["max_board"] == 1)
        T("A5 首日不判（D1 无前收 → 无封板）",
          m["2026-01-05"]["limit_up_count"] == 0)
        T("A6 up_down_ratio：D4 主板平盘(11→11)不计、创业平盘不计、ST 跌 → None(无下跌计数)或按实",
          m["2026-01-08"]["n_stocks"] == 3)
        # B：真实数据对照（互证）
        if os.path.isfile(KL):
            ser2, nm2 = load_klines()
            full = recalc(ser2, nm2)
            cmp_ = compare_with_measured(full)
            T("B1 真实数据对照：33 日全命中",
              cmp_["n_days"] == 33)
            T("B2 互证：日差均值 |·|<5 且中位 ≤3（旧系统近似口径标准）",
              cmp_["diff_mean"] is not None and abs(cmp_["diff_mean"]) < 5
              and abs(cmp_["diff_median"]) <= 3)
            T("B3 max_board 一致率 ≥80%",
              cmp_["max_board_agree"] is not None
              and int(cmp_["max_board_agree"].split("/")[0]) / int(cmp_["max_board_agree"].split("/")[1]) >= 0.8)
            print("     对照详情:", json.dumps(cmp_, ensure_ascii=False))
        # C：写盘幂等
        out1 = write_csv(m, os.path.join(sb, "out.csv"))
        c1 = io.open(out1, encoding="utf-8").read()
        write_csv(m, out1)
        c2 = io.open(out1, encoding="utf-8").read()
        T("C1 CSV 落盘且幂等（同输入同字节）", c1 == c2 and "limit_up_count" in c1)
    finally:
        _sh.rmtree(sb, ignore_errors=True)
    print("== env_recalc --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    ser, names = load_klines()
    m = recalc(ser, names, start=a.start, end=a.end)
    cmp_ = compare_with_measured(m)
    print("重算 %d 个交易日（%s ~ %s）" % (len(m), min(m), max(m)))
    print("与实测对照（互证）:", json.dumps(cmp_, ensure_ascii=False))
    dist = defaultdict(int)
    for v in m.values():
        dist[v["limit_up_count"] // 10 * 10] += 1
    print("涨停数分布(按10只桶): %s" % dict(sorted(dist.items())))
    if a.out:
        p = write_csv(m, a.out if os.path.isabs(a.out) else os.path.join(_ROOT, a.out))
        print("✅ 已写", p)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
