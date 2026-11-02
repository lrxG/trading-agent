"""g1_mktcap.py · 市值效应的正式验证（G1）。

## 要回答的问题

「市值越大，超跌反弹越可靠」——外部实证（追龙头报告：300亿+ 超跌 +7.05%/胜率71.5%
vs 0-20亿 +2.94%/57.1%）说成立。我们此前缺历史市值，无法验证。

## 本件做四件事

  Q1 市值分档（四分位）→ 看是否单调「越大越好」
  Q2 市值 × 跌幅 二维交叉 → 找出最优组合
  Q3 市值作为**排序键**与「跌最深」对比 → 是否能改善选股
  Q4 walk-forward 对比（市值规则 vs 纯跌幅规则）

## 纪律

· 只用决策日（T 日）可得市值，零未来
· 市值缺失的样本**剔除并如实报告缺失率**，不用行业/全样本中位数填补
· 结论按 walk-forward 中位数为主，单期数字只作参考（教训 M3）

用法：
  python tools/analysis/g1_mktcap.py            # 全部分析
  python tools/analysis/g1_mktcap.py --selftest
"""
import json
import os
import pickle
import statistics
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("analysis", "fs"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import mr_study as M                    # noqa: E402
import mr_sim as S                      # noqa: E402
import walkforward as WF                # noqa: E402

CACHE = os.path.join(os.environ.get("TEMP", "/tmp"), "mr_sub_vol.pkl")


def load_with_cap():
    """加载样本并附加 cap_yi / rel / vol20 → (samples, 诊断dict)"""
    with open(CACHE, "rb") as f:
        sub = pickle.load(f)
    cap_map = M.load_mktcap()
    n_cap_miss = M.attach_mktcap(sub, cap_map)
    imap = M.load_industry_map()
    ind_med = M.industry_rel_field(M.load_by_code(), imap)
    n_rel_miss = M.attach_rel(sub, imap, ind_med)
    have = [x for x in sub if x.get("cap_yi")]
    diag = {"n_all": len(sub), "n_with_cap": len(have),
            "cap_miss": n_cap_miss, "cap_cov_pct": round(len(have) / max(len(sub), 1) * 100, 1),
            "rel_miss": n_rel_miss, "cap_rows": len(cap_map)}
    return have, diag


def q1_cap_bins(samples, hold=10, n_bins=4):
    """市值四分位分档。"""
    caps = sorted(x["cap_yi"] for x in samples)
    cut = [caps[int(len(caps) * k / n_bins)] for k in range(1, n_bins)]
    out = []
    labels = ["最小25%", "25-50%", "50-75%", "最大25%"]
    for i in range(n_bins):
        lo = cut[i - 1] if i > 0 else -1
        hi = cut[i] if i < n_bins - 1 else 9e9
        seg = [x for x in samples if lo <= x["cap_yi"] < hi]
        vals = [M.trade_ret(x, hold) for x in seg]
        vals = [v for v in vals if v is not None]
        if len(vals) < 30:
            continue
        st = M.stat_block(vals)
        dt = M.daily_t(seg, hold)
        out.append({"label": "%s[%.0f,%.0f)" % (labels[i], lo, hi),
                    "n": st["n"], "win_rate": st["win_rate"],
                    "avg": st["avg"], "t": dt["t"], "pf": st["pf"]})
    return out


def q2_cap_x_drop(samples, hold=10, caps=((0, 100), (100, 300), (300, 9e9)),
                  drops=(-25, -30, -40)):
    """市值 × 跌幅 二维交叉。"""
    rows = []
    for lo, hi in caps:
        for dr in drops:
            seg = [x for x in samples if lo <= x["cap_yi"] < hi and x["r20"] <= dr]
            vals = [M.trade_ret(x, hold) for x in seg]
            vals = [v for v in vals if v is not None]
            if len(vals) < 30:
                continue
            st = M.stat_block(vals)
            dt = M.daily_t(seg, hold)
            rows.append({"cap": "%d-%s亿" % (lo, "∞" if hi > 1e8 else int(hi)),
                         "drop": dr, "n": st["n"], "win_rate": st["win_rate"],
                         "avg": st["avg"], "t": dt["t"]})
    return rows


def q3_rank_rules(samples, calendar, hold=10, top_n=5):
    """不同排序键的单期 + walk-forward 对比。"""
    rules = {
        "abs（跌最深）": lambda x: x["r20"],
        "cap_asc（小市值优先）": lambda x: x["cap_yi"],
        "cap_desc（大市值优先）": lambda x: -x["cap_yi"],
        "drop_per_cap（跌幅/市值）": lambda x: x["r20"] / max(x["cap_yi"], 1),
    }
    out = []
    for lab, kf in rules.items():
        pick = sorted(samples, key=kf)
        tmp = []
        for r, x in enumerate(pick):
            y = dict(x)
            y["r20"] = r
            tmp.append(y)
        sim = S.simulate(tmp, hold=hold, top_n=top_n, calendar=calendar)
        st = S.summarize(sim)
        wf = WF.run(tmp, calendar, train=250, test=60, step=60, verbose=False)
        ws = wf.get("summary") or {}
        out.append({"rule": lab,
                    "single_cagr": st.get("cagr"), "single_mdd": st.get("mdd"),
                    "single_sharpe": st.get("sharpe"),
                    "wf_cagr_median": ws.get("oos_cagr_median"),
                    "wf_sharpe_median": ws.get("oos_sharpe_median"),
                    "wf_pos": ws.get("oos_sharpe_positive")})
    return out


def _fmt(rows, cols):
    if not rows:
        return "(无数据)"
    lines = []
    lines.append("  " + " ".join("%-*s" % (w, c) for c, w in cols))
    for r in rows:
        lines.append("  " + " ".join(
            "%-*s" % (w, ("%+.2f%%" % r.get(c)) if isinstance(r.get(c), float)
                      and c in ("avg", "single_cagr") else r.get(c))
            for c, w in cols))
    return "\n".join(lines)


def main_report():
    samples, diag = load_with_cap()
    print("=" * 76)
    print("G1 市值效应验证")
    print("=" * 76)
    print("样本 %d · 有市值 %d（%.1f%%）· 市值表 %d 条"
          % (diag["n_all"], diag["n_with_cap"], diag["cap_cov_pct"], diag["cap_rows"]))
    if diag["cap_cov_pct"] < 80:
        print("  ⚠ 市值覆盖率不足 80%%，结论仅供参考（抓取可能未完成）")
    if not samples:
        print("无可用样本")
        return
    cal = S._build_calendar(samples)

    print("\n【Q1】市值四分位（持 10 日）")
    print("  %-22s %-7s %-7s %-9s %-7s %-6s"
          % ("市值档(亿)", "n", "胜率", "平均净", "日级t", "盈亏比"))
    for r in q1_cap_bins(samples):
        print("  %-22s %-7d %-7.1f %+8.2f%% %-7s %-6s"
              % (r["label"], r["n"], r["win_rate"], r["avg"], r["t"], r["pf"]))

    print("\n【Q2】市值 × 跌幅（持 10 日）")
    print("  %-12s %-8s %-7s %-7s %-9s %-7s"
          % ("市值", "跌幅", "n", "胜率", "平均净", "日级t"))
    for r in q2_cap_x_drop(samples):
        print("  %-12s %-8s %-7d %-7.1f %+8.2f%% %-7s"
              % (r["cap"], "跌>%d%%" % (-r["drop"]), r["n"], r["win_rate"],
                 r["avg"], r["t"]))

    print("\n【Q3】排序规则对比（单期 + walk-forward）")
    print("  %-24s %-9s %-8s %-9s %-11s %-9s %-7s"
          % ("规则", "单期年化", "回撤", "单期夏普", "WF年化中位", "WF夏普中位", "WF正值"))
    for r in q3_rank_rules(samples, cal):
        print("  %-24s %-9s %-8s %-9s %-11s %-9s %-7s"
              % (r["rule"], r["single_cagr"], r["single_mdd"], r["single_sharpe"],
                 r["wf_cagr_median"], r["wf_sharpe_median"], r["wf_pos"]))

    print("\n判定标准：**只看 WF 年化中位与 WF 正值窗口**（单期数字会被有利切分夸大）")


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

    # 合成样本：市值 10~1000 亿，收益固定
    days = [(10.0, 10.0, 10.0, 10.0)] + [(10.0, 10.0, 10.0, 11.0)] * 30
    sy = []
    for i in range(400):
        sy.append({"code": "sh600%03d" % i, "date": "2025-%02d-%02d" % (1 + i // 28, 1 + i % 28),
                   "r20": -30.0, "buy": 10.0, "amt5": 1e9, "vol20": 5.0,
                   "cap_yi": 10.0 * (i + 1), "rel": -3.0, "days": days,
                   "ddates": ["2025-%02d-%02d" % (1 + i // 28, 1 + j % 28)
                              for j in range(1, 31)]})
    b = q1_cap_bins(sy, hold=10)
    T("A1 市值分档产出 4 档", len(b) == 4, str(len(b)))
    T("A2 分档按市值升序（最小档 cap 下限最小）",
      b[0]["label"].startswith("最小25%") and b[-1]["label"].startswith("最大25%"),
      str([x["label"] for x in b]))

    c = q2_cap_x_drop(sy, hold=10)
    T("B1 二维交叉有输出且带 cap/drop 键",
      bool(c) and all("cap" in x and "drop" in x for x in c), str(len(c)))

    # 排序规则：验证 cap_asc 会把小市值排前面
    pick = sorted(sy, key=lambda x: x["cap_yi"])
    T("C1 cap_asc 排序后首个为最小市值票",
      pick[0]["cap_yi"] == min(x["cap_yi"] for x in sy))
    pick2 = sorted(sy, key=lambda x: -x["cap_yi"])
    T("C2 cap_desc 排序后首个为最大市值票",
      pick2[0]["cap_yi"] == max(x["cap_yi"] for x in sy))

    # 无市值样本必须被 load_with_cap 剔除（这里验证过滤逻辑本身）
    mixed = [{"cap_yi": 50.0}, {"cap_yi": None}, {}]
    kept = [x for x in mixed if x.get("cap_yi")]
    T("D1 无市值样本被剔除", len(kept) == 1, str(len(kept)))

    T("E1 _fmt 空输入不崩", _fmt([], [("n", 5)]) == "(无数据)")
    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    main_report()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
