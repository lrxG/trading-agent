"""fa_study.py · 基本面因子验证（A 路线第一步）。

## 与量价研究的根本区别

量价因子的超额全在低流动性票上（门槛一提高就消失，见 G1 报告）。
基本面因子理论上不含这个偏差：ROE、营收增长是**可查、不依赖成交价**的信息。
但基本面有自己的偏差：**财报滞后 + 公告时点**，必须用 `pit.py` 严格对齐。

## 本件的检验对象

可用字段（westock finance --fields all）：
  ROE / ROEWeighted / ROETTM        盈利能力
  BasicEPS / EPSTTM                 每股收益
  NPParentCompanyOwners / YOY       净利润与增速
  OperatingRevenue / GrowRate_Q     营收与增速
  GrossIncomeRatio / NetProfitRatio 毛利率/净利率
  TotalAssets / TotalLiability      规模与杠杆

## 四道检验（缺一不可）

  Q1 单因子分层（五档）→ 是否有单调性
  Q2 与流动性的相关性（M7）→ 是否又是低流动性伪装
  Q3 walk-forward（M3）→ 样本外中位数
  Q4 敏感性（M9）→ 参数变动下是否稳定

用法：
  python tools/analysis/fa_study.py --selftest
  python tools/analysis/fa_study.py --fields ROE,BasicEPS
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

import mr_study as M            # noqa: E402
import mr_sim as S              # noqa: E402
import pit as PIT               # noqa: E402
import walkforward as WF        # noqa: E402

CACHE = os.path.join(os.environ.get("TEMP", "/tmp"), "mr_sub_vol.pkl")
DEFAULT_FIELDS = ("ROE", "BasicEPS", "NPParentCompanyYOY", "OperatingRevenueGrowRate_Q")


def load_base():
    """基底样本（跌>25% 池）+ 市值 + 相对行业偏离 + 波动率。"""
    with open(CACHE, "rb") as f:
        sub = pickle.load(f)
    cap = M.load_mktcap()
    M.attach_mktcap(sub, cap)
    imap = M.load_industry_map()
    ind_med = M.industry_rel_field(M.load_by_code(), imap)
    M.attach_rel(sub, imap, ind_med)
    return [x for x in sub if x.get("cap_yi")]


def attach_fundamentals(samples, fields, lag_days=0, root=None):
    """用 PIT 严格对齐（只用当日已公告的财报）。→ 诊断"""
    rows = PIT.load_rows(root)
    if not rows:
        return {"error": "finance.csv 不存在或为空", "n": len(samples)}
    idx = PIT.build_index(rows, fields=set(fields))
    diag = PIT.align(idx, samples, list(fields), lag_days=lag_days)
    diag["n_codes_in_index"] = len(idx)
    diag["n_rows"] = len(rows)
    return diag


def clip_extremes(samples, field, lo=-100.0, hi=100.0, key_prefix="f_"):
    """剔除财务极端值。

    ★ 为什么必须做：ROE = 净利润/净资产，当净资产接近 0 或为负时 ROE 会爆炸
      （实测有 -1270%）。这类值不是「很差的公司」，而是**该指标失去意义**。
      不做过滤会拉偏分层边界，并制造假的分组差异（实测：48 条极端值全部落在
      低流动性票里，导致「无门槛组」被拖差，进而误判为「门槛越高越好」）。
    → (保留样本, 剔除数)
    """
    key = key_prefix + field
    keep, dropped = [], 0
    for s in samples:
        v = s.get(key)
        if v is None:
            keep.append(s)
        elif lo <= v <= hi:
            keep.append(s)
        else:
            dropped += 1
    return keep, dropped


def q1_bins(samples, field, hold=10, n_bins=5, min_n=50, clip=True):
    """单因子五档分层。"""
    key = "f_" + field
    if clip:
        samples, _ = clip_extremes(samples, field)
    vals = [s for s in samples if s.get(key) is not None]
    if len(vals) < n_bins * min_n:
        return {"error": "样本不足（有效 %d）" % len(vals)}
    xs = sorted(s[key] for s in vals)
    if xs[-1] - xs[0] < 1e-12:
        return {"error": "字段值全同"}
    cuts = [xs[int(len(xs) * k / n_bins)] for k in range(1, n_bins)]
    out = []
    for b in range(n_bins):
        lo = cuts[b - 1] if b > 0 else float("-inf")
        hi = cuts[b] if b < n_bins - 1 else float("inf")
        seg = [s for s in vals if lo <= s[key] < hi]
        rets = [M.trade_ret(s, hold) for s in seg]
        rets = [r for r in rets if r is not None]
        if len(rets) < min_n:
            continue
        st = M.stat_block(rets)
        dt = M.daily_t(seg, hold)
        out.append({"bin": b + 1,
                    "range": "[%.3f,%.3f)" % (lo if lo != float("-inf") else xs[0],
                                              hi if hi != float("inf") else xs[-1]),
                    "n": st["n"], "win_rate": st["win_rate"], "avg": st["avg"],
                    "t": dt["t"], "pf": st["pf"]})
    if len(out) < 3:
        return {"error": "有效档位不足"}
    # 单调性：用最高档与最低档之差，并检查符号一致性
    diffs = [out[i + 1]["avg"] - out[i]["avg"] for i in range(len(out) - 1)]
    monotone = all(d >= 0 for d in diffs) or all(d <= 0 for d in diffs)
    return {"field": field, "bins": out,
            "spread": round(out[-1]["avg"] - out[0]["avg"], 3),
            "monotone": monotone,
            "n_valid": len(vals)}


def q2_liquidity(samples, field, hold=10):
    """M7：因子值与流动性的相关性（防「低流动性伪装」）。"""
    key = "f_" + field
    vals = [s for s in samples if s.get(key) is not None and s.get("amt5")]
    if len(vals) < 100:
        return {"error": "样本不足"}
    xs = [s[key] for s in vals]
    ys = [s["amt5"] for s in vals]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / n
    sx = (sum((x - mx) ** 2 for x in xs) / n) ** 0.5
    sy = (sum((y - my) ** 2 for y in ys) / n) ** 0.5
    corr = cov / (sx * sy) if sx and sy else None
    # 因子最高档的流动性 vs 全体
    top = sorted(vals, key=lambda s: -s[key])[:max(1, n // 5)]
    bot = sorted(vals, key=lambda s: s[key])[:max(1, n // 5)]
    med = lambda arr: statistics.median([a["amt5"] for a in arr]) / 1e4
    return {"corr_with_amt": round(corr, 3) if corr is not None else None,
            "top_quintile_amt_wan": round(med(top), 0),
            "bottom_quintile_amt_wan": round(med(bot), 0),
            "all_amt_wan": round(med(vals), 0)}


def q3_q4_wf(samples, field, calendar, hold=10, top_n=5):
    """把因子当排序键做 walk-forward + 敏感性。"""
    key = "f_" + field
    vals = [s for s in samples if s.get(key) is not None]
    if len(vals) < 300:
        return {"error": "样本不足（%d）" % len(vals)}
    # 高因子优先（取负号让 r20 升序 = 因子降序）
    pick = sorted(vals, key=lambda s: -s[key])
    tmp = []
    for r, s in enumerate(pick):
        y = dict(s)
        y["r20"] = r
        tmp.append(y)
    wf = WF.run(tmp, calendar, train=250, test=60, step=60, verbose=False)
    ws = wf.get("summary") or {}
    sens = WF.sensitivity(tmp, calendar, "amt5", [0, 5e7, 1e8, 2e8],
                          hold=hold, top_n=top_n)
    return {"wf_cagr_median": ws.get("oos_cagr_median"),
            "wf_sharpe_median": ws.get("oos_sharpe_median"),
            "wf_pos": ws.get("oos_sharpe_positive"),
            "sens_verdict": sens.get("verdict"),
            "sens_stable": sens.get("stable")}


def report(fields=DEFAULT_FIELDS, lag_days=0):
    samples = load_base()
    diag = attach_fundamentals(samples, fields, lag_days=lag_days)
    print("=" * 78)
    print("基本面因子验证（PIT 严格对齐 · 只用当日已公告财报）")
    print("=" * 78)
    print("基底样本 %d · finance 行数 %s · 索引覆盖 %s 只"
          % (len(samples), diag.get("n_rows"), diag.get("n_codes_in_index")))
    if diag.get("error"):
        print("  ⛔", diag["error"])
        print("  （需先跑 fetch_finance.py --execute）")
        return
    print("  额外滞后 %d 天（更保守）" % lag_days)
    print("  字段命中率:", diag["hit_rate"])
    print()

    cal = S._build_calendar(samples)
    for f in fields:
        if diag["hit_rate"].get(f, 0) < 20:
            print("【%s】命中率仅 %.1f%%，跳过" % (f, diag["hit_rate"].get(f, 0)))
            continue
        r1 = q1_bins(samples, f)
        print("【%s】分层（持 10 日）" % f)
        if r1.get("error"):
            print("   ", r1["error"])
            print()
            continue
        print("  %-18s %-7s %-7s %-9s %-7s %-6s"
              % ("区间", "n", "胜率", "平均净", "日级t", "盈亏比"))
        for b in r1["bins"]:
            print("  %-18s %-7d %-7.1f %+8.2f%% %-7s %-6s"
                  % (b["range"], b["n"], b["win_rate"], b["avg"], b["t"], b["pf"]))
        print("  极差 %+.3f%% · 单调: %s · 有效 %d"
              % (r1["spread"], "是" if r1["monotone"] else "**否**", r1["n_valid"]))

        r2 = q2_liquidity(samples, f)
        if not r2.get("error"):
            print("  流动性检验: corr(factor, amt)=%-7s 高1/5成交额中位 %s万 · 全体 %s万"
                  % (r2["corr_with_amt"], r2["top_quintile_amt_wan"], r2["all_amt_wan"]))
        r3 = q3_q4_wf(samples, f, cal)
        if not r3.get("error"):
            print("  walk-forward: 年化中位 %-8s 夏普中位 %-6s 正值 %s"
                  % (r3["wf_cagr_median"], r3["wf_sharpe_median"], r3["wf_pos"]))
            print("  敏感性: %s" % r3["sens_verdict"])
        print()


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

    # 合成：因子与收益正相关（因子越大，卖出价越高）
    sy = []
    for i in range(600):
        # 每档收益不同：i 越大 → 卖出价越高 ⇒ 平均净收益越高
        sell = 10.0 + (i / 600.0) * 3.0
        days = [(10.0, 10.0, 10.0, 10.0)] + [(10.0, 10.0, 10.0, sell)] * 30
        sy.append({"code": "sh6%05d" % i,
                   "date": "2025-%02d-%02d" % (1 + i // 28, 1 + i % 28),
                   "r20": -30.0, "buy": 10.0, "amt5": 1e8 + i * 1e5,
                   "f_ROE": -50.0 + i / 6.0, "days": days,
                   "ddates": ["2025-%02d-%02d" % (1 + i // 28, 1 + j % 28) for j in range(1, 31)]})
    r = q1_bins(sy, "ROE")
    T("A1 分层产出 5 档", len(r.get("bins") or []) == 5, str(len(r.get("bins") or [])))
    T("A2 正相关时判定单调", r.get("monotone") is True, str(r.get("monotone")))
    T("A3 极差为正", (r.get("spread") or 0) > 0, str(r.get("spread")))

    # 反向
    for s in sy:
        s["f_ROE"] = -s["f_ROE"]
    r2 = q1_bins(sy, "ROE")
    T("B1 反向时仍判单调（只是方向不同）", r2.get("monotone") is True)
    T("B2 极差为负", (r2.get("spread") or 0) < 0, str(r2.get("spread")))

    # 流动性相关
    lq = q2_liquidity(sy, "ROE")
    T("C1 流动性检验返回相关性", lq.get("corr_with_amt") is not None, str(lq))
    T("C2 相关系数在 [-1,1]", -1 <= (lq.get("corr_with_amt") or 0) <= 1)

    # 样本不足如实报错
    T("D1 样本不足如实报错", "error" in q1_bins(sy[:30], "ROE"))
    T("D2 字段全同如实报错",
      "error" in q1_bins([{"f_X": 1.0, "r20": -30, "days": days} for _ in range(300)], "X"))

    # 缺字段样本被跳过（不填充）
    def _mk(fv, i):
        return {"f_ROE": fv, "r20": -30, "days": days, "amt5": 1e8, "buy": 10.0,
                "date": "2025-%02d-%02d" % (1 + i // 28, 1 + i % 28),
                "ddates": ["2025-%02d-%02d" % (1 + i // 28, 1 + j % 28)
                           for j in range(1, 31)]}
    mixed = [_mk(None, i) for i in range(100)]
    mixed += [_mk(-50.0 + i / 3.0, i) for i in range(300)]
    r3 = q1_bins(mixed, "ROE")
    T("E1 None 样本被排除（n_valid=300）", r3.get("n_valid") == 300, str(r3.get("n_valid")))

    # F 极端值过滤（净资产为负时 ROE 数学上无意义，必须剔除）
    ext = ([{"f_X": -1270.0}, {"f_X": 500.0}] +
           [{"f_X": float(i)} for i in range(10)])
    kept, dropped = clip_extremes(ext, "X")
    T("F1 极端值被剔除（-1270 与 500）", dropped == 2, str(dropped))
    T("F2 正常值保留", len(kept) == 10, str(len(kept)))
    kept2, dropped2 = clip_extremes([{"f_X": None}, {"f_X": 5.0}], "X")
    T("F3 None 不算极端值（交给上层判缺失）",
      len(kept2) == 2 and dropped2 == 0, "%d/%d" % (len(kept2), dropped2))

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    if "--selftest" in argv or not argv:
        return _selftest()
    fields = DEFAULT_FIELDS
    lag = 0
    for i, a in enumerate(argv):
        if a == "--fields":
            fields = tuple(f.strip() for f in argv[i + 1].split(",") if f.strip())
        elif a == "--lag":
            lag = int(argv[i + 1])
    report(fields, lag_days=lag)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
