# -*- coding: utf-8 -*-
"""research.py · 信号研究平台（2026-10-21 建 · 防自欺版）。

## 为什么建本件

2026-10-21 回测证明涨停打板策略净期望 -0.273%/笔（毛 +0.027% vs 成本 0.300%）。
继续优化前必须先解决一个方法论问题：**在大样本上搜索参数组合，必然能搜出"正期望"
的子集——那是过拟合，不是 alpha。**

所以本件不是"找赚钱参数"的工具，是**证伪工具**：任何被宣称有效的信号，
都要过下面三道关：

| 关卡 | 做什么 | 不过会怎样 |
|---|---|---|
| **① 随机基准** | 同样样本数下随机抽，得到期望分布；看信号是否显著超出 | 信号在随机波动范围内 = 无效 |
| **② 样本外检验** | 前半段找规则、后半段验证；反之亦然 | 只在样本内有效 = 过拟合 |
| **③ 多重检验校正** | 记录本次会话试过多少个假设，按 Bonferroni 收紧阈值 | 试 100 个组合必有一个"显著"，要罚 |

## 用法

```bash
# 单特征分层
python tools/analysis/research.py --feature seal_ratio --bins 5

# 表达式筛选 + 三重检验
python tools/analysis/research.py --expr "boards>=2 and seal_ratio<0.15" --validate

# 列出所有可用特征
python tools/analysis/research.py --list-features
```

## 设计纪律

- **零未来数据**：特征只用决策日 T 及之前的信息。
- **成交判定与 backtest/settle 同口径**（一字买不进不计入）。
- **不写生产**：纯只读研究，产物只打印/显式导出。
- **诚实记录失败**：跑不出正期望就明说，不挑好看的切法汇报。
"""
import argparse
import csv
import io
import os
import random
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("fs", "analysis", "decision", "risk"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

from paths import ROOT  # noqa: E402
import backtest as BT  # noqa: E402

COST = BT.COST_BUY + BT.COST_SELL   # 往返成本（%）
VALID_START = BT.VALID_START

# 会话内试过的假设数（多重检验校正用）；进程级，不落盘
_TRIALS = {"n": 0, "log": []}


# ── 特征工厂 ────────────────────────────────────────────────────────────────
def build_features(by_date, dates, d1=None, d2=None, hold=2):
    """构建「涨停票 × 特征 × 结果」样本表。

    → list[dict]，每条含决策日 T 可得的特征 + T+1 买 T+1+hold-1 卖的净收益。
    **一字/开盘即涨停的不计入**（买不进，不能假装买到）。
    """
    from collections import Counter
    samples = []
    for i, d in enumerate(dates[:-max(3, hold + 1)]):
        if d1 and d < d1:
            continue
        if d2 and d > d2:
            continue
        pool, pool_src = BT.load_pool(d, by_date, ROOT)
        if not pool:
            continue
        dq = by_date.get(d) or {}
        nd = dates[i + 1]
        nq = by_date.get(nd) or {}
        ei = i + 1 + hold - 1
        if ei >= len(dates):
            continue
        ed = dates[ei]
        eq = by_date.get(ed) or {}

        # 当日市场环境特征（决策日可得）
        lu_n = len(pool)
        max_board = max([_f(r.get("boards")) for r in pool] or [0])
        reason_cnt = Counter((r.get("reason") or "") for r in pool if r.get("reason"))
        # 全市场涨跌（从当日行情算）
        up = dn = 0
        for rr in dq.values():
            try:
                p = float(rr.get("change_pct") or 0)
            except (TypeError, ValueError):
                continue
            if p > 0:
                up += 1
            elif p < 0:
                dn += 1
        ud_ratio = up / dn if dn else 99.0

        for r in pool:
            code = r.get("code")
            q1 = nq.get(code)
            prev = dq.get(code) or {}
            if not q1:
                continue
            pc = _f(prev.get("close"))
            o = _f(q1.get("open"))
            h = _f(q1.get("high"))
            lo = _f(q1.get("low"))
            cl = _f(q1.get("close"))
            if pc <= 0 or o <= 0:
                continue
            if o == h == lo == cl:
                continue                                   # 一字买不进
            lim = round(pc * (1 + BT.limit_ratio(code, r.get("name") or q1.get("name"))), 2)
            if o >= lim - 0.005:
                continue                                   # 开盘即涨停
            q2 = eq.get(code)
            if not q2:
                continue
            sp = _f(q2.get("close"))
            if sp <= 0:
                continue
            gross = (sp / o - 1) * 100
            net = ((sp * (1 - BT.COST_SELL)) / (o * (1 + BT.COST_BUY)) - 1) * 100

            amt = _f(prev.get("amount"))
            seal = _f(r.get("seal_amount"))
            reason = r.get("reason") or ""
            # 位置特征：过去 N 日涨幅（用当日及之前行情，禁未来）
            pos5 = _ret_n(by_date, dates, i, code, 5)
            pos20 = _ret_n(by_date, dates, i, code, 20)
            # ── 移植旧系统 C1 首板矩阵的两个强特征（2026-10-21）──
            # 旧系统口径（n=862 涨停 vs 660 对照）：超跌低位 25.3% vs 1.7%（+23.6pp）
            #                                  高活跃 58.9% vs 34.7%（+24.2pp）
            oversold = _oversold(by_date, dates, i, code)
            active = _active(by_date, dates, i, code)
            pos60 = _ret_n(by_date, dates, i, code, 60)

            samples.append({
                # ── 决策日可得特征 ──
                "date": d, "code": code, "boards": _f(r.get("boards")),
                "seal": seal, "seal_ratio": seal / amt if amt > 0 else 0.0,
                "amt": amt, "amt_yi": amt / 1e8,
                "pct_t": _f(prev.get("change_pct")),
                "turnover": _f(prev.get("turnover_rate")) if "turnover_rate" in prev else None,
                "sector_n": reason_cnt.get(reason, 0) if reason else 0,
                "lu_n": lu_n, "max_board": max_board, "ud_ratio": ud_ratio,
                "pos5": pos5, "pos20": pos20, "pos60": pos60,
                "oversold": oversold, "active": active,
                # ── 结果 ──
                "gross": round(gross, 3), "net": round(net, 3),
                "win": 1 if net > 0 else 0,
            })
    return samples


def _win(by_date, dates, i, code, n):
    """取过去 n 日（含当日 i）的收盘序列 → list[float]，不足 n 返回 None。"""
    if i - n + 1 < 0:
        return None
    out = []
    for j in range(i - n + 1, i + 1):
        row = (by_date.get(dates[j]) or {}).get(code)
        if not row:
            return None
        v = _f(row.get("close"))
        if v <= 0:
            return None
        out.append(v)
    return out


def _oversold(by_date, dates, i, code):
    """旧系统「超跌低位」口径近似：60 日区间位置 ≤ 30%（低位）+ 20 日跌幅 < -10%。

    旧系统口径原文：`超跌低位 oversold`（涨停组 25.3% vs 对照 1.7%）。
    本实现按其语义近似：①当前收盘处于过去 60 日区间的下 30% ②近 20 日跌幅 >10%。
    返回 1/0/None（数据不足）。**近似口径，须重验**——移植不是照抄，是重算。
    """
    w60 = _win(by_date, dates, i, code, 60)
    w20 = _win(by_date, dates, i, code, 20)
    if not w60 or not w20:
        return None
    lo, hi = min(w60), max(w60)
    px = w60[-1]
    if hi <= lo:
        return None
    rank = (px - lo) / (hi - lo)
    ret20 = (w20[-1] / w20[0] - 1) * 100
    return 1 if (rank <= 0.30 and ret20 < -10) else 0


def _active(by_date, dates, i, code):
    """旧系统「高活跃」口径近似：近 10 日成交额均值 / 近 60 日成交额均值 ≥ 1.5。

    旧系统口径原文：`高活跃 active`（涨停组 58.9% vs 对照 34.7%）。
    本实现按其语义近似（量能放大=活跃）。返回 1/0/None。**须重验**。
    """
    if i - 59 < 0:
        return None
    a10 = a60 = 0.0
    n10 = n60 = 0
    for j in range(i - 59, i + 1):
        row = (by_date.get(dates[j]) or {}).get(code)
        if not row:
            continue
        v = _f(row.get("amount"))
        if v <= 0:
            continue
        a60 += v
        n60 += 1
        if j >= i - 9:
            a10 += v
            n10 += 1
    if n60 < 40 or n10 < 8:
        return None
    return 1 if (a10 / n10) >= 1.5 * (a60 / n60) else 0


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _ret_n(by_date, dates, i, code, n):
    """过去 n 日累计涨幅（%）。i=决策日索引；只用 i 及之前 ⇒ 零未来。

    ★ 2026-10-21 修：原写法用 `(by_date.get(d) or {}).get(code) or {}` 后直接
      `.get("close")`，当 code 不存在时 `or {}` 返回空 dict、`.get` 得 None ⇒ 恒 None
      （pos5/pos20 两特征全空、分箱直接崩）。改为显式取值 + 判空。
    """
    if i - n < 0:
        return None
    row0 = (by_date.get(dates[i - n]) or {}).get(code)
    row1 = (by_date.get(dates[i]) or {}).get(code)
    if not row0 or not row1:
        return None
    p0 = _f(row0.get("close"))
    p1 = _f(row1.get("close"))
    if p0 <= 0 or p1 <= 0:
        return None
    return round((p1 / p0 - 1) * 100, 2)


FEATURES = {
    "boards": "连板数", "seal": "封单额(元)", "seal_ratio": "封单/成交额",
    "amt_yi": "成交额(亿)", "pct_t": "决策日涨幅", "sector_n": "同行业涨停数",
    "lu_n": "全市场涨停家数", "max_board": "全市场最高板", "ud_ratio": "涨跌家数比",
    "pos5": "过去5日涨幅", "pos20": "过去20日涨幅", "pos60": "过去60日涨幅",
    "oversold": "超跌低位(旧C1·须重验)", "active": "高活跃(旧C1·须重验)",
}


# ── 统计核心 ────────────────────────────────────────────────────────────────
def stat_block(rets, label=""):
    """→ 统计块。期望用**净收益**（已扣成本）。"""
    n = len(rets)
    if n == 0:
        return {"n": 0}
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    avg = sum(rets) / n
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0
    srt = sorted(rets)
    return {"n": n, "win_rate": round(len(wins) / n * 100, 1),
            "avg_net": round(avg, 3),
            "avg_win": round(aw, 2), "avg_loss": round(al, 2),
            "profit_factor": round(abs(aw / al), 2) if al else None,
            "median": round(srt[n // 2], 2),
            "p05": round(srt[max(0, n // 20)], 2),
            "p95": round(srt[min(n - 1, n - n // 20)], 2),
            "std": round((sum((x - avg) ** 2 for x in rets) / n) ** 0.5, 2)}


def by_value(samples, feature):
    """二值/离散特征按取值分组（by_bins 对 0/1 特征无效——分位切点全落在同一值）。"""
    groups = {}
    for s in samples:
        v = s.get(feature)
        if v is None:
            continue
        groups.setdefault(v, []).append(s["net"])
    if len(groups) < 2:
        return {"error": "取值只有 %d 种，无法比较" % len(groups)}
    out = []
    for v in sorted(groups):
        st = stat_block(groups[v])
        st["value"] = v
        st["range"] = "= %s" % v
        out.append(st)
    return {"feature": feature, "bins": out}


def by_bins(samples, feature, bins=5):
    """按特征分位分箱 → 各箱统计。二值特征自动转 by_value。"""
    uniq = {s.get(feature) for s in samples if s.get(feature) is not None}
    if len(uniq) <= 2:
        return by_value(samples, feature)
    vals = [s[feature] for s in samples if s.get(feature) is not None]
    if len(vals) < bins * 10:
        return {"error": "样本不足（有效值 %d / 总 %d）" % (len(vals), len(samples))}
    if max(vals) - min(vals) < 1e-9:
        return {"error": "特征值全部相同（%s），无法分层" % round(min(vals), 4)}
    vals.sort()
    cuts = [vals[int(len(vals) * k / bins)] for k in range(1, bins)]
    out = []
    for b in range(bins):
        lo = cuts[b - 1] if b > 0 else float("-inf")
        hi = cuts[b] if b < bins - 1 else float("inf")
        sub = [s["net"] for s in samples
               if s.get(feature) is not None and lo <= s[feature] < hi]
        if not sub:
            continue
        st = stat_block(sub)
        st["range"] = "[%s, %s)" % (round(lo, 3) if lo != float("-inf") else "-inf",
                                    round(hi, 3) if hi != float("inf") else "inf")
        out.append(st)
    if len(out) < 2:
        return {"error": "分层后有效档位不足（%d）" % len(out)}
    return {"feature": feature, "bins": out}


def random_baseline(samples, n_draw, trials=2000, seed=42):
    """① 随机基准：同规模随机抽样 n_draw 次，得到平均净收益的分布。

    → (p50, p95, p99, 实测均值是否超过 p95)。
    **若信号的实测均值落在随机分布内 ⇒ 无显著 alpha。**
    """
    if len(samples) < n_draw or n_draw < 5:
        return None
    allr = [s["net"] for s in samples]
    rnd = random.Random(seed)
    means = []
    for _ in range(trials):
        pick = rnd.sample(allr, n_draw)
        means.append(sum(pick) / n_draw)
    means.sort()
    return {"n_draw": n_draw,
            "rand_mean": round(sum(means) / len(means), 3),
            "rand_p50": round(means[len(means) // 2], 3),
            "rand_p95": round(means[int(len(means) * 0.95)], 3),
            "rand_p99": round(means[int(len(means) * 0.99)], 3),
            "trials": trials}


def out_of_sample(samples, predicate, split=0.5):
    """② 样本外检验：按时间切分前后两段，各自算信号与基准的差。

    → {"is": {...}, "oos": {...}, "consistent": bool}
    `consistent` = 两段同号且样本外也超随机 p95。
    """
    dates = sorted({s["date"] for s in samples})
    if len(dates) < 20:
        return {"error": "日期太少（%d）" % len(dates)}
    cut = dates[int(len(dates) * split)]
    seg_a = [s for s in samples if s["date"] < cut]     # 前半（样本内）
    seg_b = [s for s in samples if s["date"] >= cut]    # 后半（样本外）

    def _eval(seg, tag):
        hit = [s for s in seg if predicate(s)]
        if len(hit) < 20:
            return {"tag": tag, "n": len(hit), "error": "样本不足"}
        st = stat_block([s["net"] for s in hit])
        base = stat_block([s["net"] for s in seg])
        rb = random_baseline([s for s in seg if s not in hit], len(hit), trials=500)
        st.update({"tag": tag, "base_avg": base["avg_net"],
                   "delta_vs_base": round(st["avg_net"] - base["avg_net"], 3),
                   "rand_p95": rb["rand_p95"] if rb else None,
                   "beats_random": (st["avg_net"] > rb["rand_p95"]) if rb else None})
        return st

    a = _eval(seg_a, "样本内(前半)")
    b = _eval(seg_b, "样本外(后半)")
    consistent = (a.get("delta_vs_base", 0) > 0 and b.get("delta_vs_base", 0) > 0
                  and b.get("beats_random") is True)
    return {"cut": cut, "is": a, "oos": b, "consistent": consistent}


def multiple_testing_penalty(n_trials, alpha=0.05):
    """③ 多重检验校正：Bonferroni 阈值。"""
    return {"trials": n_trials, "alpha": alpha,
            "corrected_alpha": round(alpha / max(n_trials, 1), 6),
            "note": "试过 %d 个假设 ⇒ 阈值收紧到 %.6f（试得越多，越难判真显著）"
                    % (n_trials, alpha / max(n_trials, 1))}


# ══════════════════════════════════════════════════════════════════════════
# 研究三铁律（2026-10-21 移植自旧系统 research/ 母题体系）
#
# 来源：`data/research/imported_20261019/research/` —— 那套体系跑了 3950 行研究、
# 862 样本特征矩阵，沉淀出三条硬约束。本平台原缺这三条 ⇒ 今天就犯了其中两条：
#   · 用 13 笔样本说"连板≥4 正期望"（违反样本量底线）
#   · 在 13 个月上直接外推（违反独立市场状态数）
#   · 用 T 日收盘后才知道的涨停事实做"决策日可得"特征（违反前知/后知分离）
# ══════════════════════════════════════════════════════════════════════════

# 样本量底线（旧系统：862 样本才敢下特征结论；分组结论建议 ≥100）
MIN_SAMPLE_GROUP = 100
MIN_SAMPLE_CONCLUSION = 300

# 前知/后知：这些是**决策日收盘后**才知道的，不能当作"决策时可见"
POST_HOC_FIELDS = {
    "seal_amount": "封单额（收盘才定）——T 日盘中不可知终值",
    "boards": "连板数（收盘确认）——T 日盘中不确定是否封住",
    "pct_t": "当日涨幅（收盘才定）",
}

# 决策时（T 日盘后/ T+1 盘前）真正可见的
PRE_KNOWABLE = {
    "pos5", "pos20", "amt_yi", "seal_ratio", "sector_n", "lu_n",
    "max_board", "ud_ratio", "boards_prev", "turnover",
}


def check_ex_ante(fields):
    """铁律①：前知/后知分离。

    「这条件在**做决策那一刻**能看到吗？」看不到的字段用作筛选条件 = 事后诸葛。
    注意：本平台场景是「T 日收盘后决策、T+1 开盘买」，故 T 日收盘确认的字段
    （boards/seal_amount）**在这个场景下是前知的**——但必须显式声明场景，
    换成「T 日盘中决策」就变成后知。本函数按**盘后决策**场景判定，并对
    盘中决策场景给出警告。
    → {"ok": bool, "post_hoc": [...], "note": str}
    """
    hit = [f for f in fields if f in POST_HOC_FIELDS]
    return {
        "ok": not hit,
        "post_hoc": hit,
        "note": ("以下字段依赖 T 日收盘：%s。本平台场景=盘后决策⇒可用；"
                 "若改盘中决策⇒必须剔除" % ",".join(hit)) if hit else "全部前知",
        "post_hoc_fields": dict(POST_HOC_FIELDS),
    }


def check_market_states(samples, max_per_state=40):
    """铁律②：独立市场状态数。

    旧系统教训：22 日矩阵的环境分布看似 6 种，实为**同一波 7-8 月行情**
    ⇒ 独立市场状态数=1 ⇒ 结论禁止跨状态外推。
    判据（近似）：把交易日按 `lu_n`（全市场涨停家数）聚类成状态，
    相邻日归为同一状态（行情是连续的），统计**状态段数**。
    → {"independent_states": int, "segments": [...], "warning": str|None}
    """
    by_date = {}
    for s in samples:
        by_date[s["date"]] = s.get("lu_n")
    ds = sorted(by_date)
    if not ds:
        return {"independent_states": 0, "segments": [], "warning": "无样本"}
    segs = []
    cur = [ds[0]]
    for d in ds[1:]:
        prev_v = by_date.get(cur[-1])
        v = by_date.get(d)
        if prev_v and v and abs(v - prev_v) <= max_per_state:
            cur.append(d)
        else:
            segs.append(cur)
            cur = [d]
    segs.append(cur)
    n = len(segs)
    warn = None
    if n <= 2:
        warn = ("⚠ 独立市场状态仅 %d 段 ⇒ 结论**禁止跨市场状态外推**"
                "（旧系统教训：22 日看似 6 种环境实为同一波行情）" % n)
    elif n <= 4:
        warn = "独立市场状态 %d 段，偏少，外推需谨慎" % n
    return {"independent_states": n,
            "segments": [{"from": g[0], "to": g[-1], "days": len(g)} for g in segs],
            "warning": warn}


def check_sample_size(n, min_n=None, label=""):
    """铁律③：样本量底线。"""
    th = min_n or MIN_SAMPLE_GROUP
    ok_concl = n >= MIN_SAMPLE_CONCLUSION
    ok_group = n >= th
    lvl = "结论级" if ok_concl else ("分组级" if ok_group else "不足")
    return {"n": n, "threshold_group": th,
            "threshold_conclusion": MIN_SAMPLE_CONCLUSION,
            "level": lvl, "ok": ok_group,
            "warning": None if ok_group else
            ("⚠ 样本 %d < %d ⇒ 不得下任何结论（旧系统：862 样本才敢说特征有效性）" % (n, th))}


def record_trial(desc):
    """登记一次尝试（多重检验用）。"""
    _TRIALS["n"] += 1
    _TRIALS["log"].append(desc)
    return _TRIALS["n"]


# ── 表达式求值（受限 eval，只白名单）──────────────────────────────────────
_SAFE = {"abs": abs, "min": min, "max": max, "round": round}


def make_predicate(expr):
    """把表达式编译成 predicate(sample)->bool。禁内建 except 白名单。"""
    code = compile(expr, "<expr>", "eval")

    def pred(s):
        env = dict(_SAFE)
        env.update({k: v for k, v in s.items() if k not in ("code", "date")})
        # None 一律视为不满足（缺特征 ≠ 特征为 0）
        for k in FEATURES:
            if s.get(k) is None:
                return False
        try:
            return bool(eval(code, {"__builtins__": {}}, env))  # noqa: S307
        except Exception:                                       # noqa: BLE001
            return False
    return pred


# ── 自检 ────────────────────────────────────────────────────────────────────
def _selftest():
    ok = fail = 0
    fails = []

    def T(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [OK] %s" % name)
        else:
            fail += 1
            fails.append(name)
            print("  [!!] %s %s" % (name, extra))

    # 合成样本：构造"已知有效"的信号（pos5 低 → 收益高）
    rnd = random.Random(7)
    fake = []
    for i in range(600):
        p5 = rnd.uniform(-20, 30)
        net = -p5 * 0.1 + rnd.gauss(0, 3)      # pos5 越低越好
        fake.append({"date": "2026-%02d-%02d" % (1 + i % 6, 1 + i % 20),
                     "code": "c%d" % i, "boards": rnd.randint(1, 4),
                     "seal": rnd.uniform(1e6, 1e8),
                     "seal_ratio": rnd.uniform(0, 0.4),
                     "amt": rnd.uniform(5e7, 2e9),
                     "amt_yi": rnd.uniform(0.5, 20),
                     "pct_t": 10.0, "sector_n": rnd.randint(0, 5),
                     "lu_n": rnd.randint(30, 100),
                     "max_board": rnd.randint(2, 8),
                     "ud_ratio": rnd.uniform(0.5, 3),
                     "pos5": p5, "pos20": rnd.uniform(-30, 40),
                     "gross": net + 0.3, "net": net,
                     "win": 1 if net > 0 else 0})

    # A 统计
    st = stat_block([s["net"] for s in fake])
    T("A1 stat_block 字段齐", all(k in st for k in
        ("n", "win_rate", "avg_net", "profit_factor", "std")))
    T("A2 样本数正确", st["n"] == 600, str(st["n"]))

    # B 分箱
    b = by_bins(fake, "pos5", bins=5)
    T("B1 分箱 5 档", len(b.get("bins") or []) == 5, str(len(b.get("bins") or [])))
    if b.get("bins"):
        first, last = b["bins"][0], b["bins"][-1]
        T("B2 已知信号方向可见（pos5 低档收益 > 高档）",
          first["avg_net"] > last["avg_net"],
          "低档 %.2f vs 高档 %.2f" % (first["avg_net"], last["avg_net"]))

    # C 随机基准
    hit = [s for s in fake if s["pos5"] < 0]
    base = [s for s in fake if s["pos5"] >= 0]
    rb = random_baseline(base, len(hit), trials=300)
    T("C1 随机基准有 p95", rb and "rand_p95" in rb)
    T("C2 已知信号显著超出随机 p95",
      rb is not None and (sum(s["net"] for s in hit) / len(hit)) > rb["rand_p95"],
      "sig %.2f vs p95 %.2f" % (sum(s["net"] for s in hit) / len(hit),
                                rb["rand_p95"] if rb else 0))

    # D 样本外
    pred = lambda s: s["pos5"] < 0  # noqa: E731
    oos = out_of_sample(fake, pred)
    T("D1 样本外检验产出前后两段", "is" in oos and "oos" in oos)
    T("D2 真信号两段一致 ⇒ consistent=True", oos.get("consistent") is True,
      str(oos.get("consistent")))

    # E 假信号应被识别（随机噪声）
    fake2 = []
    for i in range(600):
        p5 = rnd.uniform(-20, 30)
        net = rnd.gauss(0, 5)                  # 与 pos5 无关
        fake2.append({**{k: v for k, v in fake[0].items()},
                      "date": "2026-%02d-%02d" % (1 + i % 6, 1 + i % 20),
                      "code": "x%d" % i, "pos5": p5, "net": net})
    oos2 = out_of_sample(fake2, pred)
    T("E1 噪声信号不判 consistent（防自欺）",
      oos2.get("consistent") is False, str(oos2.get("consistent")))

    # F 多重检验
    mt = multiple_testing_penalty(100)
    T("F1 Bonferroni 阈值收紧", abs(mt["corrected_alpha"] - 0.0005) < 1e-9,
      str(mt["corrected_alpha"]))

    # G 表达式
    p1 = make_predicate("boards >= 2 and seal_ratio < 0.15")
    T("G1 表达式可编译", p1(fake[0]) in (True, False))
    T("G2 缺特征(None) ⇒ 不满足（不当作 0）",
      p1({"boards": 3, "seal_ratio": None}) is False)
    T("G3 表达式异常 ⇒ False 不崩", make_predicate("1/0")(fake[0]) is False)

    # H 特征清单
    T("H1 FEATURES 非空且含关键特征",
      all(k in FEATURES for k in ("boards", "seal_ratio", "lu_n", "pos5")))
    T("H2 旧 C1 特征已注册（oversold/active）",
      "oversold" in FEATURES and "active" in FEATURES)

    # I 铁律①：前知/后知
    e1 = check_ex_ante(["pos5", "lu_n", "ud_ratio"])
    T("I1 纯前知字段 ⇒ ok", e1["ok"] is True)
    e2 = check_ex_ante(["pos5", "seal_amount"])
    T("I2 含后知字段 ⇒ 揪出并警告", e2["ok"] is False
      and "seal_amount" in e2["post_hoc"], str(e2["post_hoc"]))
    T("I3 后知字段有解释", bool(e2["post_hoc_fields"].get("seal_amount")))

    # J 铁律②：独立市场状态数
    mk = []
    for k in range(300):
        # 前 100 天 lu_n≈50，中间 100 天≈150，后 100 天≈50 ⇒ 应识别出 3 段
        v = 50 if k < 100 else (150 if k < 200 else 50)
        mk.append({"date": "2026-%02d-%02d" % (1 + k // 28, 1 + k % 28),
                   "lu_n": v, "net": 0.0})
    st = check_market_states(mk, max_per_state=40)
    T("J1 三段行情 ⇒ 独立状态数 3", st["independent_states"] == 3,
      str(st["independent_states"]))
    T("J2 状态段有起止日", all("from" in g and "to" in g for g in st["segments"]))
    mk1 = [{"date": "2026-01-%02d" % (i + 1), "lu_n": 50 + (i % 3), "net": 0.0}
           for i in range(20)]
    st1 = check_market_states(mk1, max_per_state=40)
    T("J3 单一状态 ⇒ 发出外推禁令警告",
      st1["independent_states"] == 1 and "禁止跨市场状态外推" in (st1["warning"] or ""),
      str(st1["warning"]))

    # K 铁律③：样本量底线
    T("K1 样本 13 ⇒ 判定不足", check_sample_size(13)["ok"] is False)
    T("K2 样本 150 ⇒ 分组级", check_sample_size(150)["level"] == "分组级")
    T("K3 样本 400 ⇒ 结论级", check_sample_size(400)["level"] == "结论级")
    T("K4 样本不足时有警告", bool(check_sample_size(13)["warning"]))

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：%s" % "；".join(fails))
    return 0 if fail == 0 else 1


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    ap = argparse.ArgumentParser(description="信号研究平台（防自欺版）")
    ap.add_argument("--from", dest="d1", default=VALID_START)
    ap.add_argument("--to", dest="d2", default=None)
    ap.add_argument("--feature", default=None, help="单特征分层")
    ap.add_argument("--bins", type=int, default=5)
    ap.add_argument("--expr", default=None, help="筛选表达式，如 'boards>=2 and seal_ratio<0.15'")
    ap.add_argument("--validate", action="store_true", help="跑三重检验")
    ap.add_argument("--hold", type=int, default=2)
    ap.add_argument("--list-features", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)

    if a.list_features:
        print("可用特征：")
        for k, v in FEATURES.items():
            print("  %-12s %s" % (k, v))
        return 0

    # 载数据
    d2 = a.d2 or max(d for d in os.listdir(os.path.join(ROOT, "data", "raw"))
                     if len(d) == 10 and d[0].isdigit())
    by_date, dates = BT.load_klines(ROOT, d1=a.d1, d2=d2)
    print("载入 %d 个交易日（%s ~ %s）" % (len(dates), dates[0] if dates else "-",
                                          dates[-1] if dates else "-"))
    cov = BT.coverage_check(ROOT, a.d1, d2)
    if cov.get("bad_months"):
        print("⚠ 覆盖警告：%d 个月残缺，建议 --from %s" % (
            len(cov["bad_months"]), VALID_START))
    samples = build_features(by_date, dates, a.d1, d2, hold=a.hold)
    print("样本 %d 笔（已剔一字/开盘涨停）\n" % len(samples))
    if not samples:
        print("⛔ 无样本"); return 1

    base = stat_block([s["net"] for s in samples])
    print("=== 基准（全样本，净收益）===")
    print("  n=%d · 胜率 %.1f%% · 平均净 %+.3f%% · 盈亏比 %s"%(
        base["n"], base["win_rate"], base["avg_net"], base["profit_factor"]))
    print("  中位 %+.2f%% · p05 %+.2f%% · p95 %+.2f%% · 标准差 %.2f"%(
        base["median"], base["p05"], base["p95"], base["std"]))
    print()

    # ── 研究三铁律（每轮强制打印，不通过也要如实显示）──
    print("=== 研究三铁律自检 ===")
    mk = check_market_states(samples)
    print("  ② 独立市场状态数：%d 段 %s" % (
        mk["independent_states"],
        "（%s ~ %s）" % (mk["segments"][0]["from"], mk["segments"][-1]["to"])
        if mk["segments"] else ""))
    if mk["warning"]:
        print("     " + mk["warning"])
    ss = check_sample_size(base["n"], label="全样本")
    print("  ③ 样本量：%d ⇒ %s（结论级需 ≥%d）" % (
        ss["n"], ss["level"], ss["threshold_conclusion"]))
    if ss["warning"]:
        print("     " + ss["warning"])
    print("  ① 前知/后知：本平台场景=**T 日收盘后决策、T+1 开盘买** ⇒ "
          "T 日收盘确认的字段（boards/seal_amount/pct_t）在此场景属前知；")
    print("     若改「盘中决策」则必须剔除这些字段（见 check_ex_ante）")
    print()

    if a.feature:
        r = by_bins(samples, a.feature, bins=a.bins)
        if "error" in r:
            print("⛔", r["error"]); return 1
        print("=== 分层：%s（%s）===" % (a.feature, FEATURES.get(a.feature, "")))
        print("  %-22s %-6s %-8s %-9s %-8s" % ("区间", "n", "胜率", "平均净", "盈亏比"))
        for x in r["bins"]:
            flag = "" if x["n"] >= MIN_SAMPLE_GROUP else " ⚠样本不足"
            print("  %-22s %-6d %-8.1f%% %+9.3f%% %-8s%s" % (
                x["range"], x["n"], x["win_rate"], x["avg_net"],
                x["profit_factor"], flag))
        print()

    if a.expr:
        record_trial(a.expr)
        pred = make_predicate(a.expr)
        hit = [s for s in samples if pred(s)]
        print("=== 表达式：%s ===" % a.expr)
        if len(hit) < 20:
            print("  ⛔ 命中样本仅 %d（<20，无法判断）" % len(hit)); return 1
        st = stat_block([s["net"] for s in hit])
        print("  n=%d（占比 %.1f%%）· 胜率 %.1f%% · 平均净 %+.3f%% · 盈亏比 %s" % (
            st["n"], st["n"] / len(samples) * 100, st["win_rate"],
            st["avg_net"], st["profit_factor"]))
        print("  vs 基准 %+.3f%% ⇒ 增量 %+.3f%%" % (base["avg_net"],
                                                st["avg_net"] - base["avg_net"]))
        print()

        if a.validate:
            print("=== 三重检验 ===")
            rest = [s for s in samples if not pred(s)]
            rb = random_baseline(rest, len(hit), trials=2000)
            if rb:
                verdict = "★ 超出" if st["avg_net"] > rb["rand_p99"] else (
                    "超过 p95" if st["avg_net"] > rb["rand_p95"] else "✗ 未超出（随机波动内）")
                print("  ① 随机基准（同规模 %d 笔 × %d 次）：p50 %+.3f  p95 %+.3f  p99 %+.3f ⇒ %s"
                      % (len(hit), rb["trials"], rb["rand_p50"], rb["rand_p95"],
                         rb["rand_p99"], verdict))
            oos = out_of_sample(samples, pred)
            if "error" in oos:
                print("  ② 样本外：%s" % oos["error"])
            else:
                for tag in ("is", "oos"):
                    x = oos[tag]
                    if "error" in x:
                        print("  ② %s：%s" % (x["tag"], x["error"]))
                    else:
                        print("  ② %s：n=%d 平均净 %+.3f%%（基准 %+.3f%%，增量 %+.3f%%）超随机=%s"
                              % (x["tag"], x["n"], x["avg_net"], x["base_avg"],
                                 x["delta_vs_base"], x["beats_random"]))
                print("     ⇒ 前后两段一致性：%s" % (
                    "✅ 通过（两段同为正增量且样本外超随机）" if oos["consistent"]
                    else "❌ 未通过（不构成可信信号）"))
            mt = multiple_testing_penalty(_TRIALS["n"])
            print("  ③ 多重检验：本次会话已试 %d 个假设 ⇒ 校正阈值 α=%.6f"
                  % (mt["trials"], mt["corrected_alpha"]))
            print()

    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
