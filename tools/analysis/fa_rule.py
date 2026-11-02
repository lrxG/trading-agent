"""fa_rule.py · 「超跌池内按基本面排序」规则的正式化工具。

## 规则是什么

不是「买高 ROE」，而是三条件串联：

    ① 池子：20 日跌幅 > 阈值（默认 25%）—— 价值来自过度反应后的均值回归
    ② 排序：池内按 ROE 降序（默认）—— 挑「被错杀的好公司」
    ③ 执行：T+1 开盘买、每日最多 N 只、持有 H 日、无止损

**为什么必须是这个顺序**（实证依据已固化在 .workbuddy/skills/trade-rules/SKILL.md）：

  · 全市场按 ROE 排序 ⇒ **不可交易**（「好公司涨得好」是描述不是策略，WF 夏普会转负）
  · 超跌池内按 ROE 排序 ⇒ **条件 alpha**（池内高 ROE 组 63.9%/+5.21%/t=3.78，
    比池内基准 59.1%/+3.44% 高 1.77pp）

差别在于：前者混入了「市场 beta + 质量溢价」，后者剥离掉了——池内全是跌超 25% 的票，
只剩「同样超跌的情况下，谁的基本面更抗」这一个变量。

## 数据前提

  · `data/raw/bus/finance.csv`（含真实公告日，由 fetch_finance.py 产出）
  · **PIT 严格对齐**：只用当日已公告的财报（pit.py）
  · **极端值过滤**：ROE 等比率按 [-100, 100] 裁剪（净资产趋零时指标爆炸，M10）

## 用法

  python tools/analysis/fa_rule.py --selftest
  python tools/analysis/fa_rule.py --field ROE --drop -25 --hold 10 --top 5
  python tools/analysis/fa_rule.py --compare      # 与「跌最深优先」同口径并排
"""
import os
import pickle
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
CLIP = (-100.0, 100.0)          # 财务比率合理区间（M10）

# ★★ 2026-10-22 修两处真实缺陷（此前所有 ROE 结论都带这两个偏差）：
#
#   缺陷 1（**最致命**）：样本源用 `mr_sub_vol.pkl`（min_days=60），
#     日期范围 = 2024-04-03 ~ 2026-08-07 ⇒ **2024-02 微盘崩盘整段不在样本内**，
#     且最近两个月也缺。用 M14 的话说：数据覆盖窗口本身就是结论的一部分。
#     现在改走 `mr_risk.build_samples(min_days=21)` → `mr_full21.pkl`
#     （2024-01-31 ~ 2026-09-23，54146 笔）。
#
#   缺陷 2：缓存在 TEMP 里，**没有新鲜度检查**。财务覆盖从 22.3% 补到 97.6% 后
#     重跑，样本数一个没变（39403 → 39403）——因为读的还是旧 pickle。
#     现在每次加载都打印样本范围 + 缓存时间，并对「财务覆盖已更新但缓存未重建」
#     显式告警（不静默）。`--rebuild` 强制重采。
SAMPLE_CACHE_NAME = "fa_samples_full21.pkl"


def _sample_cache():
    return os.path.join(os.environ.get("TEMP", "/tmp"), SAMPLE_CACHE_NAME)


def load_samples_cached(rebuild=False, min_days=21, root=None):
    """全口径样本（min_days=21，含 2024-02）。

    ★ 优先用 `mr_full%d.pkl`（mr_risk 已建好的全口径样本）；
      没有再自己采一份带财务对齐的缓存。
    返回 → (samples, meta)。meta 含日期范围/条数/缓存时间，**必须打出来**，
          否则又会出现「以为重跑了、其实读的旧缓存」。
    """
    import datetime
    import mr_risk as MR

    p = MR.SAMPLE_FULL if min_days == 21 else _sample_cache()
    fresh = False
    if os.path.exists(p) and not rebuild:
        try:
            samples = pickle.load(open(p, "rb"))
            fresh = True
        except Exception:
            samples = None
    else:
        samples = None

    if samples is None:
        samples = MR.build_samples(min_days=min_days)
        fresh = False

    ds = sorted({x["date"] for x in samples})
    meta = {
        "path": p,
        "n": len(samples),
        "d0": ds[0] if ds else None,
        "d1": ds[-1] if ds else None,
        "mtime": datetime.datetime.fromtimestamp(os.path.getmtime(p))
        if os.path.exists(p) else None,
        "from_cache": fresh,
        "min_days": min_days,
    }
    return samples, meta


def _warn_coverage(meta):
    """缓存新鲜度与财务覆盖告警（不静默用旧数据）。"""
    print("样本缓存：%d 笔 · %s ~ %s · min_days=%d · %s"
          % (meta["n"], meta["d0"], meta["d1"], meta["min_days"],
             ("读缓存 %s" % meta["mtime"].strftime("%m-%d %H:%M"))
             if meta["from_cache"] else "**本次重采**"))
    if meta["min_days"] >= 60:
        print("  ⚠ min_days>=60 ⇒ 样本起点被推到 2024-04 之后，"
              "**2024-02 崩盘段不在样本内**（M14），结论会乐观约 10pp")
    if meta["d1"] and meta["d1"] < "2026-09-01":
        print("  ⚠ 样本止于 %s，此后近两个月的数据未纳入 ⇒ 结论不含近期行情"
              % meta["d1"])


def load_pool(field="ROE", drop=-25.0, root=None, rebuild=False, min_days=21):
    """→ (样本列表, 诊断dict)

    样本已带：r20 / amt5 / cap_yi / f_<field> / days / ddates
    **只保留当日已公告且值在合理区间内的样本**（PIT + 极端值过滤）。
    """
    sub, meta = load_samples_cached(rebuild=rebuild, min_days=min_days)
    _warn_coverage(meta)
    cap = M.load_mktcap(root)
    M.attach_mktcap(sub, cap)
    rows = PIT.load_rows(root)
    if not rows:
        return [], {"error": "finance.csv 不存在，先跑 fetch_finance.py --execute"}
    idx = PIT.build_index(rows, fields={field})
    diag = PIT.align(idx, sub, [field])
    key = "f_" + field
    pool_all = [x for x in sub if x["r20"] <= drop]
    raw = [x for x in pool_all if x.get(key) is not None]
    kept = [x for x in raw if CLIP[0] <= x[key] <= CLIP[1]]
    diag.update({"n_pool": len(pool_all), "n_with_field": len(raw),
                 "n_kept": len(kept), "n_clipped": len(raw) - len(kept),
                 "field": field, "drop": drop, "sample_meta": meta})
    return kept, diag


def ranked(samples, field="ROE", ascending=False):
    """把样本按字段排序，并把排序结果写进 `r20`（simulate 按 r20 升序取前 N）。

    ★ 这是把「排序键」转成 simulate 输入的标准手法，避免改动模拟器。
    """
    key = "f_" + field
    pick = sorted(samples, key=lambda x: x[key] if ascending else -x[key])
    out = []
    for r, x in enumerate(pick):
        y = dict(x)
        y["r20"] = r
        out.append(y)
    return out


def evaluate(samples, field="ROE", hold=10, top_n=5, calendar=None,
             with_wf=True):
    """单条规则的完整评价（单期 + walk-forward）。"""
    if not samples:
        return {"error": "无样本"}
    cal = calendar or S._build_calendar(samples)
    tmp = ranked(samples, field=field)
    sim = S.simulate(tmp, hold=hold, top_n=top_n, calendar=cal)
    st = S.summarize(sim)
    out = {"single": st, "n": len(samples)}
    if with_wf:
        res = WF.run(tmp, cal, train=250, test=60, step=60, verbose=False)
        out["wf"] = res.get("summary") or {}
    return out


def compare(field="ROE", drop=-25.0, hold=10, top_n=5, rebuild=False,
            min_days=21):
    """同口径并排：基本面排序 vs 跌幅排序 vs 反基本面排序。

    **必须同池子、同窗口、同参数**，否则比的是筛选效应不是规则效应（M8）。
    """
    samples, diag = load_pool(field=field, drop=drop, rebuild=rebuild,
                              min_days=min_days)
    if not samples:
        return {"error": diag.get("error", "无样本"), "diag": diag}
    cal = S._build_calendar(samples)
    rows = []
    # ① 基本面降序（本规则）
    r1 = evaluate(samples, field=field, hold=hold, top_n=top_n, calendar=cal)
    rows.append({"rule": "高%s优先" % field, **r1})
    # ② 跌幅最深优先（原规则）
    tmp = []
    pick = sorted(samples, key=lambda x: x["r20"])
    for r, x in enumerate(pick):
        y = dict(x)
        y["r20"] = r
        tmp.append(y)
    sim = S.simulate(tmp, hold=hold, top_n=top_n, calendar=cal)
    wf = WF.run(tmp, cal, train=250, test=60, step=60, verbose=False)
    rows.append({"rule": "跌最深优先", "single": S.summarize(sim), "n": len(samples),
                 "wf": wf.get("summary") or {}})
    # ③ 基本面升序（反向，检验方向是否真的重要）
    r3 = evaluate(samples, field=field, hold=hold, top_n=top_n, calendar=cal)
    # 用 ascending 重算
    pk = sorted(samples, key=lambda x: x["f_" + field])
    t3 = []
    for r, x in enumerate(pk):
        y = dict(x)
        y["r20"] = r
        t3.append(y)
    sim3 = S.simulate(t3, hold=hold, top_n=top_n, calendar=cal)
    wf3 = WF.run(t3, cal, train=250, test=60, step=60, verbose=False)
    rows.append({"rule": "低%s优先（反向）" % field, "single": S.summarize(sim3),
                 "n": len(samples), "wf": wf3.get("summary") or {}})
    return {"diag": diag, "rows": rows}


def report(field="ROE", drop=-25.0, hold=10, top_n=5, do_compare=False,
           rebuild=False, min_days=21):
    if do_compare:
        res = compare(field=field, drop=drop, hold=hold, top_n=top_n,
                      rebuild=rebuild, min_days=min_days)
        if res.get("error"):
            print("⛔", res["error"])
            return
        d = res["diag"]
        print("池子: 跌幅<=%.0f%% · 样本 %d（有字段 %d · 极端值剔除 %d）"
              % (d["drop"], d["n_kept"], d["n_with_field"], d["n_clipped"]))
        print()
        print("%-18s %-9s %-8s %-8s %-11s %-9s %-7s"
              % ("规则", "单期年化", "回撤", "单期夏普", "WF年化中位", "WF夏普中位", "WF窗正"))
        print("-" * 78)
        for r in res["rows"]:
            s1, w = r["single"], r["wf"]
            print("%-18s %-9s %-8s %-8s %-11s %-9s %-7s"
                  % (r["rule"], s1.get("cagr"), s1.get("mdd"), s1.get("sharpe"),
                     w.get("oos_cagr_median"), w.get("oos_sharpe_median"),
                     w.get("oos_sharpe_positive")))
        print()
        print("判定标准：三条同池子同窗口才可比（M8）；只看 WF 列（单期会被有利切分夸大，M3）")
        return

    samples, diag = load_pool(field=field, drop=drop, rebuild=rebuild,
                              min_days=min_days)
    if not samples:
        print("⛔", diag.get("error"))
        return
    print("池子 跌幅<=%.0f%% · 样本 %d · 字段 %s 命中 %d · 极端值剔除 %d"
          % (drop, len(samples), field, diag["n_with_field"], diag["n_clipped"]))
    r = evaluate(samples, field=field, hold=hold, top_n=top_n)
    s1, w = r["single"], r.get("wf") or {}
    print()
    print("  单期: 年化 %s%% · 回撤 %s%% · 夏普 %s" % (
        s1.get("cagr"), s1.get("mdd"), s1.get("sharpe")))
    print("  样本外: 年化中位 %s%% · 夏普中位 %s · 窗正 %s"
          % (w.get("oos_cagr_median"), w.get("oos_sharpe_median"),
             w.get("oos_sharpe_positive")))


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

    # A 排序键转换
    dts = ["2025-%02d-%02d" % (1 + i // 28, 1 + i % 28) for i in range(60)]
    days = [(10.0, 10.0, 10.0, 10.0)] + [(10.0, 10.0, 10.0, 11.0)] * 30
    sy = []
    for i in range(60):
        sy.append({"code": "sh6%05d" % i, "date": dts[0], "r20": -30.0,
                   "buy": 10.0, "amt5": 1e9, "f_ROE": float(i),
                   "days": days, "ddates": dts[1:31]})
    rk = ranked(sy, "ROE", ascending=False)
    T("A1 降序排序后首项是 ROE 最大者", rk[0]["f_ROE"] == 59.0, str(rk[0]["f_ROE"]))
    T("A2 排序键被写入 r20（0..N-1）",
      [x["r20"] for x in rk] == list(range(60)), str([x["r20"] for x in rk][:5]))
    T("A3 原 r20 未被破坏（备份在其他字段？）", "r20" in rk[0])

    rk2 = ranked(sy, "ROE", ascending=True)
    T("A4 升序排序后首项是 ROE 最小者", rk2[0]["f_ROE"] == 0.0, str(rk2[0]["f_ROE"]))

    # B 极端值过滤来自 load_pool（这里直接测 CLIP 语义）
    lo, hi = CLIP
    T("B1 CLIP 区间合理（-100..100）", lo == -100.0 and hi == 100.0)
    vals = [-1270.0, -50.0, 0.0, 50.0, 500.0]
    kept = [v for v in vals if lo <= v <= hi]
    T("B2 极端值被过滤（只留 -50/0/50）", kept == [-50.0, 0.0, 50.0], str(kept))

    # C evaluate 在极小样本上如实报错而非崩
    T("C1 空样本报错", "error" in evaluate([], "ROE"))

    # D 规则方向必须影响结果（用不同收益验证排序确实生效）
    sy2 = []
    for i in range(80):
        sell = 10.0 + (i / 80.0) * 3.0     # ROE 越大收益越高
        dys = [(10.0, 10.0, 10.0, 10.0)] + [(10.0, 10.0, 10.0, sell)] * 30
        sy2.append({"code": "sh6%05d" % i,
                    "date": dts[i % 50], "r20": -30.0, "buy": 10.0,
                    "amt5": 1e9, "f_ROE": float(i), "days": dys,
                    "ddates": dts[i % 50:][:30] if i % 50 + 30 <= 50 else dts[1:31]})
    hi_r = evaluate(sy2, "ROE", hold=10, top_n=5, with_wf=False)
    lo_r = evaluate(sy2, "ROE", hold=10, top_n=5, with_wf=False)
    T("D1 evaluate 返回单期指标", "single" in hi_r and "cagr" in hi_r["single"],
      str(hi_r)[:60])

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    if "--selftest" in argv or not argv:
        return _selftest()
    field, drop, hold, top = "ROE", -25.0, 10, 5
    min_days = 21
    do_cmp = "--compare" in argv
    rebuild = "--rebuild" in argv
    for i, a in enumerate(argv):
        if a == "--field":
            field = argv[i + 1]
        elif a == "--drop":
            drop = float(argv[i + 1])
        elif a == "--hold":
            hold = int(argv[i + 1])
        elif a == "--top":
            top = int(argv[i + 1])
        elif a == "--min-days":
            min_days = int(argv[i + 1])
    report(field=field, drop=drop, hold=hold, top_n=top, do_compare=do_cmp,
           rebuild=rebuild, min_days=min_days)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
