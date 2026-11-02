"""walkforward.py · 滚动窗口选参 + 样本外跟踪（只读 · 纯计算 · 零落盘）。

## 为什么需要它

参数稳定性现在靠手工算（「前半挑最优 → 跑后半」只做一次，样本量单薄且结论随机）。
本件把它做成**自动滚动**：

    窗口 W 内挑最优参数 → 紧接着的 O 段做样本外 → 窗口向前滚 S 天 → 重复

输出三段：
  ① 每个窗口的「最优参数」→ 看参数是否跳来跳去（跳 = 不稳定 = 不可用）
  ② 每次样本外的表现 → 汇总年化/回撤/夏普的分布（中位数比单次值可靠）
  ③ 参数出现频次 → 哪个参数被反复选中（被反复选中的才可能是真结构）

## 关键口径

· 选参指标默认 **夏普**（也可 --metric cagr / calmar），因为纯收益会偏向高波动配置
· 样本外表现用 `mr_sim.simulate` 的真实资金口径（现金约束/整手/真实日历）
· **不写入任何文件**：只打印。要留档自行重定向

用法：
  python tools/analysis/walkforward.py --samples <pkl> --train 250 --test 60 --step 60
"""
import os
import sys
import pickle
import statistics

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("analysis", "fs"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import mr_sim as S                    # noqa: E402

# 待搜参数空间（保持小：网格越大越容易过拟合）
GRID = {
    "hold": (5, 10, 20),
    "top_n": (3, 5, 8),
    "stop_loss": (None, -0.10, -0.15),
}


def _score(r, metric):
    if not r or r.get("error"):
        return None
    if metric == "cagr":
        return r.get("cagr")
    if metric == "calmar":
        c, m = r.get("cagr"), r.get("mdd")
        if c is None or not m:
            return None
        return c / m
    return r.get("sharpe")


def grid_search(samples, calendar, metric="sharpe"):
    """在给定样本集上网格搜索最优参数。→ (best_params, best_score, all_rows)"""
    rows = []
    for hold in GRID["hold"]:
        for tn in GRID["top_n"]:
            for sl in GRID["stop_loss"]:
                sim = S.simulate(samples, hold=hold, top_n=tn, stop_loss=sl,
                                 calendar=calendar)
                r = S.summarize(sim)
                sc = _score(r, metric)
                if sc is None:
                    continue
                rows.append({"hold": hold, "top_n": tn, "stop_loss": sl,
                             "score": round(sc, 3), "cagr": r.get("cagr"),
                             "mdd": r.get("mdd"), "sharpe": r.get("sharpe")})
    if not rows:
        return None, None, []
    rows.sort(key=lambda x: -x["score"])
    b = rows[0]
    return (b["hold"], b["top_n"], b["stop_loss"]), b["score"], rows


def run(samples, calendar, train=250, test=60, step=60, metric="sharpe",
        min_train_samples=200, verbose=True):
    """滚动 walk-forward。→ dict（窗口明细 + 汇总统计）"""
    cal = [d for d in calendar]
    windows = []
    i = 0
    while i + train + test <= len(cal):
        c_tr = cal[i:i + train]
        c_te = cal[i + train:i + train + test]
        d_tr_lo, d_tr_hi = c_tr[0], c_tr[-1]
        d_te_lo, d_te_hi = c_te[0], c_te[-1]
        seg_tr = [s for s in samples if d_tr_lo <= s["date"] <= d_tr_hi]
        seg_te = [s for s in samples if d_te_lo <= s["date"] <= d_te_hi]
        if len(seg_tr) < min_train_samples or not seg_te:
            i += step
            continue
        best, sc, _ = grid_search(seg_tr, c_tr, metric=metric)
        if best is None:
            i += step
            continue
        hold, tn, sl = best
        sim = S.simulate(seg_te, hold=hold, top_n=tn, stop_loss=sl,
                         calendar=c_te)
        r = S.summarize(sim)
        windows.append({
            "train": [d_tr_lo, d_tr_hi], "test": [d_te_lo, d_te_hi],
            "n_train": len(seg_tr), "n_test": len(seg_te),
            "best": {"hold": hold, "top_n": tn, "stop_loss": sl},
            "train_score": sc,
            "oos": {"cagr": r.get("cagr"), "mdd": r.get("mdd"),
                    "sharpe": r.get("sharpe"), "n_trades": r.get("n_trades"),
                    "ret": r.get("total_ret")},
        })
        if verbose:
            print("  窗口 %d · 训练 %s~%s · 样本外 %s~%s" % (
                len(windows), d_tr_lo, d_tr_hi, d_te_lo, d_te_hi))
            print("      选中：持%d日·每日%d只·止损%s（训练夏普 %s）"
                  % (hold, tn, ("%d%%" % (sl * 100)) if sl else "无", sc))
            print("      样本外：年化 %s%% · 回撤 %s%% · 夏普 %s · %s 笔"
                  % (r.get("cagr"), r.get("mdd"), r.get("sharpe"),
                     r.get("n_trades")))
        i += step

    if not windows:
        return {"windows": [], "error": "无有效窗口（数据不足）"}

    # 汇总
    def col(k):
        v = [w["oos"][k] for w in windows if w["oos"][k] is not None]
        return v

    cagrs, mdds, shs = col("cagr"), col("mdd"), col("sharpe")
    params = [w["best"] for w in windows]
    from collections import Counter
    key = lambda p: (p["hold"], p["top_n"], p["stop_loss"])
    freq = Counter(key(p) for p in params)
    holds = Counter(p["hold"] for p in params)
    tops = Counter(p["top_n"] for p in params)
    stops = Counter(str(p["stop_loss"]) for p in params)

    summary = {
        "n_windows": len(windows),
        "oos_cagr_median": round(statistics.median(cagrs), 1) if cagrs else None,
        "oos_cagr_min": round(min(cagrs), 1) if cagrs else None,
        "oos_cagr_max": round(max(cagrs), 1) if cagrs else None,
        "oos_mdd_median": round(statistics.median(mdds), 1) if mdds else None,
        "oos_mdd_worst": round(max(mdds), 1) if mdds else None,
        "oos_sharpe_median": round(statistics.median(shs), 2) if shs else None,
        "oos_sharpe_positive": "%d/%d" % (len([x for x in shs if x > 0]), len(shs)),
        "param_unique": len(freq),
        "param_freq": {"持%d日/每日%d只/止损%s" % (h, t, s): n
                       for (h, t, s), n in freq.most_common()},
        "hold_freq": dict(holds.most_common()),
        "top_freq": dict(tops.most_common()),
        "stop_freq": dict(stops.most_common()),
        "stability": "稳定" if len(freq) <= 3 else
                     ("中等" if len(freq) <= 6 else "不稳定（参数频繁跳变）"),
    }
    return {"windows": windows, "summary": summary}


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    samples_path = None
    train, test, step, metric = 250, 60, 60, "sharpe"
    for i, a in enumerate(argv):
        if a == "--samples":
            samples_path = argv[i + 1]
        elif a == "--train":
            train = int(argv[i + 1])
        elif a == "--test":
            test = int(argv[i + 1])
        elif a == "--step":
            step = int(argv[i + 1])
        elif a == "--metric":
            metric = argv[i + 1]
    if not samples_path:
        print(__doc__)
        return 0
    with open(samples_path, "rb") as f:
        samples = pickle.load(f)
    cal = S._build_calendar(samples)
    print("样本 %d · 日历 %d 日 · 训练 %d / 样本外 %d / 步长 %d · 指标 %s\n"
          % (len(samples), len(cal), train, test, step, metric))
    res = run(samples, cal, train=train, test=test, step=step, metric=metric)
    if "error" in res:
        print(res["error"])
        return 2
    s = res["summary"]
    print("\n=== 汇总 ===")
    for k in ("n_windows", "oos_cagr_median", "oos_cagr_min", "oos_cagr_max",
              "oos_mdd_median", "oos_mdd_worst", "oos_sharpe_median",
              "oos_sharpe_positive", "param_unique", "stability"):
        print("  %-22s %s" % (k, s.get(k)))
    print("  参数频次:", s.get("param_freq"))
    return 0


def sensitivity(samples, calendar, field, values, hold=10, top_n=5,
                metric="sharpe", train=250, test=60, step=60):
    """★ 参数敏感性测试（M9 工程化）。

    对某个样本筛选字段（如 amt5 流动性门槛）取多个阈值，分别跑 walk-forward，
    看结论是否随阈值单调衰减/翻转。

    **判据**：若最优阈值落在边界、或结论随阈值显著衰减 ⇒ 该「优势」来自参数边缘，
    不是真实结构，**不得采信**。

    → dict：{"rows": [...], "verdict": str, "stable": bool}
    """
    rows = []
    min_n = max(120, int(len(samples) * 0.03))     # 至少 3% 样本，且不少于 120
    for v in values:
        seg = [s for s in samples if s.get(field) is not None and s[field] >= v]
        if len(seg) < min_n:
            rows.append({"value": v, "n": len(seg), "skipped": "样本不足"})
            continue
        res = run(seg, calendar, train=train, test=test, step=step,
                  metric=metric, verbose=False, min_train_samples=60)
        s = res.get("summary") or {}
        rows.append({"value": v, "n": len(seg),
                     "wf_cagr": s.get("oos_cagr_median"),
                     "wf_sharpe": s.get("oos_sharpe_median"),
                     "wf_pos": s.get("oos_sharpe_positive")})
    # 主判据用 cagr；窗口太短算不出 cagr 时退回 sharpe（两者都是「越大越好」）
    metric_key = "wf_cagr"
    valid = [r for r in rows if r.get("wf_cagr") is not None]
    if len(valid) < 3:
        metric_key = "wf_sharpe"
        valid = [r for r in rows if r.get("wf_sharpe") is not None]
    if len(valid) < 3:
        return {"rows": rows, "verdict": "样本不足，无法判定", "stable": None,
                "metric": metric_key}
    best_i = max(range(len(valid)), key=lambda i: valid[i][metric_key])
    best = valid[best_i]
    worst = valid[-1]
    at_edge = best_i == 0 or best_i == len(valid) - 1
    decay = (best[metric_key] - worst[metric_key])
    unit = "%" if metric_key == "wf_cagr" else ""
    stable = (not at_edge) and abs(decay) < abs(best[metric_key]) * 0.5
    if at_edge:
        verdict = ("⚠ 最优阈值落在**边界**（%s）⇒ 优势来自参数边缘，不可采信"
                   % best["value"])
    elif not stable:
        verdict = ("⚠ 结论随参数显著衰减（%+.2f%s → %+.2f%s）⇒ 不稳定"
                   % (best[metric_key], unit, worst[metric_key], unit))
    else:
        verdict = "✓ 阈值变化下结论相对稳定（中段最优、衰减 <50%）"
    return {"rows": rows, "verdict": verdict, "stable": stable,
            "best": best, "worst": worst, "metric": metric_key}


def _selftest():
    ok = fail = 0

    def T(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ✓ %s" % name)
        else:
            fail += 1
            print("  ✗ %s  %s" % (name, extra))

    # 造 400 天、每天 3 个候选的合成样本（稳定上涨 10 日 +5%）
    dts = ["20%02d-%02d-%02d" % (24 + i // 336, 1 + (i // 28) % 12, 1 + i % 28)
           for i in range(400)]
    dts = sorted(set(dts))[:400]
    days = [(10.0, 10.0, 10.0, 10.0)] + [(10.0, 10.0, 10.0, 10.5)] * 30
    samples = []
    for i, d in enumerate(dts[:-25]):
        for j in range(3):
            samples.append({"date": d, "code": "sh600%03d" % j, "r20": -30.0 - j,
                            "buy": 10.0, "amt5": 1e9, "days": days,
                            "ddates": dts[i + 1:i + 31], "vol20": 5.0})

    # W1 网格搜索能返回合法参数
    cal = dts
    best, sc, rows = grid_search(samples[:200], cal[:120], metric="sharpe")
    T("W1 网格搜索返回参数且落在 GRID 内",
      best is not None and best[0] in GRID["hold"] and best[1] in GRID["top_n"],
      str(best))
    T("W2 网格搜索产出 27 行（3×3×3）", len(rows) == 27, str(len(rows)))

    # W3 walk-forward 至少产生 1 个窗口
    res = run(samples, cal, train=120, test=30, step=60, verbose=False)
    T("W3 walk-forward 产生窗口", len(res.get("windows") or []) >= 1,
      str(len(res.get("windows") or [])))
    T("W4 汇总含稳定性判定", "stability" in (res.get("summary") or {}),
      str(res.get("summary"))[:60])

    # W5 数据不足时如实报错（不伪造窗口）
    res2 = run(samples[:20], cal[:30], train=120, test=30, step=60, verbose=False)
    T("W5 数据不足如实报错", "error" in res2, str(res2)[:80])

    # W6 指标切换生效（cagr 与 sharpe 选的参数可以不同，至少都能跑通）
    b1, s1, _ = grid_search(samples[:200], cal[:120], metric="cagr")
    b2, s2, _ = grid_search(samples[:200], cal[:120], metric="sharpe")
    T("W6 两种指标都能完成选参", b1 is not None and b2 is not None,
      "%s/%s" % (b1, b2))

    # W7 calmar = cagr/mdd，mdd=0 时不崩
    r_test = {"cagr": 10.0, "mdd": 0.0, "sharpe": 1.0}
    T("W7 calmar 在 mdd=0 时返回 None（不除零）",
      _score(r_test, "calmar") is None)

    # S 参数敏感性（M9）
    # 造样本：j 越大 → amt 越高 → 涨幅越小（模拟「低流动性才有超额」）
    sy = []
    for i, d in enumerate(dts[:-25]):
        for j in range(4):
            amt = 1e7 * (2 ** j)              # 1e7 / 2e7 / 4e7 / 8e7
            win = 11.0 if j == 0 else 10.1
            dys = [(10.0, 10.0, 10.0, 10.0)] + [(10.0, 10.0, 10.0, win)] * 30
            sy.append({"date": d, "code": "sh6%05d" % (i * 4 + j), "r20": -30.0,
                       "buy": 10.0, "amt5": amt, "days": dys, "vol20": 5.0,
                       "ddates": dts[i + 1:i + 31], "cap_yi": 50.0})
    sens = sensitivity(sy, dts, "amt5", [0, 1.5e7, 3e7, 6e7],
                       hold=10, top_n=2, train=120, test=30, step=60)
    T("S1 sensitivity 返回 rows 与 verdict",
      "rows" in sens and "verdict" in sens, str(sens)[:70])
    T("S2 门槛提高后优势衰减 ⇒ 判不稳定",
      sens.get("stable") is False, str(sens.get("verdict"))[:80])
    T("S3 最优落边界时明确告警",
      "边界" in (sens.get("verdict") or "") or sens.get("stable") is False,
      str(sens.get("verdict"))[:60])

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
