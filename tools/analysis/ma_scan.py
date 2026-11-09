# -*- coding: utf-8 -*-
"""并购重组公告每日扫描器 —— 把 ml_09/ml_suspended 的统计结论变成盘后可跑的清单

用法：
  python tools/analysis/ma_scan.py                    # 扫当日 data/raw/<最新日期>/announcements.jsonl
  python tools/analysis/ma_scan.py --date 2026-11-09
  python tools/analysis/ma_scan.py --all              # 列出当日全部命中（不过滤）

历史统计底座（2024-01 ~ 2026-11，1,492 起披露日事件 + 708 起停牌复牌事件）：
  披露日当天：上涨 58.2%（基准 47.6%），均值 +1.28%，但开盘跳空 +1.69% 吃掉全部
  D 日开盘买入：全部持有期均值为负（T+1 -0.19% / T+5 -0.45% / T+20 -0.37%），胜率 44%
  停牌复牌首日：上涨 63.4%、涨停 23%，但开盘买入 T+1~T+20 全部 -1.2~-1.4%，胜率 40%
  → 唯一正期望：要约收购 × 公告前 5 日跌超 5%（T+20 +16.49%，胜率 73%，n=15）
  → 唯一明确回避：公告前 5 日涨超 15%（T+5 -4.35%~-4.76%，胜率 27%~35%）
"""
import argparse, glob, json, os, sys
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

TRUE_PAT = ("发行股份购买资产", "发行股份及支付现金购买资产", "重大资产重组",
            "购买资产", "收购", "要约收购", "控制权", "实际控制人变更",
            "吸收合并", "资产置换", "重大资产出售", "股权转让")
DROP_PAT = ("法律意见", "财务顾问", "核查意见", "附表", "承销", "保荐", "回复", "反馈",
            "问询", "股东大会", "监事会", "独立董事", "审计报告", "审阅报告", "备考合并",
            "资产评估", "估值报告", "持续督导", "承诺", "进展", "实施情况", "完成",
            "过户", "上市流通", "限售股", "解除限售", "会议安排", "审核委员会", "受理",
            "中止", "终止", "核准", "章程", "议事规则", "管理制度", "风险提示", "异常波动",
    "修订说明", "申报稿", "过渡期损益", "第二次", "第三次", "第四次", "说明的公告")
IGNITE = ("筹划", "预案", "意向", "签署", "拟", "停牌", "暨", "提示性公告",
          "报告书", "摘要", "首次", "框架")


def classify(title):
    """标题 → 子类型；None = 非事件本体"""
    if not any(k in title for k in TRUE_PAT):
        return None
    if any(d in title for d in DROP_PAT):
        return None
    # 注：此处原用 IGNITE 白名单（筹划/预案/意向/拟…）做二次过滤。2026-11-09 实锤它会
    # 滤掉「关于收购巴西CSN股权的公告」这类真事件（不含任何启动词），
    # 现改为只靠 DROP_PAT 黑名单；IGNITE 保留作「首次 vs 进展」判断的辅助素材。
    if "要约收购" in title:
        return "要约收购"
    if "吸收合并" in title:
        return "吸收合并"
    if "控制权" in title or "实际控制人" in title:
        return "控制权变更"
    if "重大资产重组" in title or "购买资产" in title:
        return "发股买资产"
    if "收购" in title:
        return "现金收购"
    if "股权转让" in title or "资产出售" in title:
        return "出售/转让"
    return "其他"


# 历史分档表：(子类型, 前5日档) → (T+5均值, T+5胜率, T+20均值, 结论)
GRID = {
    ("要约收购", "A"):  (6.27, 73, 16.49, "★历史最优组合，但样本仅15起"),
    ("现金收购", "C"):  (0.87, 50, 1.97, "弱正期望"),
    ("发股买资产", "A"): (0.86, 46, 0.58, "弱正期望"),
    ("现金收购", "A"):  (0.57, 53, -0.35, "持平"),
    ("现金收购", "B"):  (0.50, 46, 1.66, "持平"),
    ("控制权变更", "B"): (0.43, 56, 0.66, "样本小"),
    ("吸收合并", "B"):  (0.28, 56, -2.84, "样本小"),
    ("发股买资产", "C"): (-1.16, 43, -2.10, "负期望"),
    ("现金收购", "D"):  (-1.21, 40, -2.67, "负期望"),
    ("发股买资产", "D"): (-1.31, 41, -2.95, "负期望"),
    ("发股买资产", "B"): (-1.47, 44, -2.22, "负期望"),
    ("现金收购", "E"):  (-4.35, 27, -1.85, "★历史最差档之一"),
    ("发股买资产", "E"): (-4.76, 35, -1.39, "★历史最差档之一"),
}


def pre5_bin(x):
    if pd.isna(x):
        return "?"
    if x <= -5:
        return "A"
    if x <= 0:
        return "B"
    if x <= 5:
        return "C"
    if x <= 15:
        return "D"
    return "E"


BIN_DESC = {"A": "前5日跌超5%", "B": "前5日跌0~5%", "C": "前5日涨0~5%",
            "D": "前5日涨5~15%", "E": "前5日涨超15%", "?": "数据不足"}


def latest_raw_date():
    ds = sorted(os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "data/raw/2026-*"))
                if os.path.isdir(p))
    return ds[-1] if ds else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--all", action="store_true", help="列出全部命中，不按期望值过滤")
    args = ap.parse_args()
    date = args.date or latest_raw_date()
    if not date:
        sys.exit("找不到 data/raw/ 下的日期目录")

    path = os.path.join(ROOT, "data/raw", date, "announcements.jsonl")
    if not os.path.exists(path):
        sys.exit("缺 %s\n先跑：python tools/e2e/fetch_announcements.py --date %s --since <前2日>"
                 % (path, date))

    rows = [json.loads(l) for l in open(path, encoding="utf-8")]
    hits = {}
    for r in rows:
        sub = classify(r.get("title", ""))
        if not sub:
            continue
        hits.setdefault((r["code"], r["name"]), []).append((r["date"], sub, r["title"]))

    if not hits:
        print("【并购重组扫描 %s】全市场 %d 条公告中无事件本体" % (date, len(rows)))
        return

    # 行情：算前 5 日涨幅 / 流动性
    k = pd.read_csv(os.path.join(ROOT, "data/raw/bus/klines.csv"),
                    usecols=["code", "date", "open", "close", "amount"],
                    dtype={"code": "str", "date": "str"})
    k = k[~k.code.str.match(r"^(sh000|sz399|bj)")]
    k["c6"] = k.code.str[-6:]
    k = k[(k[["open", "close"]].fillna(0) > 0).all(axis=1)]
    k = k[k.date <= date].sort_values(["c6", "date"])
    last = {}
    for c, d in k.groupby("c6"):
        d = d.reset_index(drop=True)
        i = len(d) - 1
        if i < 5:
            continue
        last[c] = dict(close=d.loc[i, "close"],
                       pre5=(d.loc[i, "close"] / d.loc[i - 5, "close"] - 1) * 100,
                       amt20=d.loc[max(0, i - 19):i, "amount"].mean() / 1e8,
                       last_date=d.loc[i, "date"])

    out = []
    for (code, name), evs in sorted(hits.items()):
        c6 = code[-6:]
        evs = sorted(evs)
        sub = evs[0][1]
        q = last.get(c6)
        if q is None:
            out.append(dict(code=c6, name=name, sub=sub, pre5=float("nan"),
                            amt20=float("nan"), b="?", verdict="停牌/无行情 → 复牌首日买不到涨停，回避",
                            detail=evs[0][2][:40], n_ev=len(evs)))
            continue
        b = pre5_bin(q["pre5"])
        stat = GRID.get((sub, b))
        if stat:
            verdict = "%s（T+5 %+.2f%% / 胜率%d%%）" % (stat[3], stat[0], stat[1])
        else:
            verdict = "样本不足，无统计结论"
        out.append(dict(code=c6, name=name, sub=sub, pre5=q["pre5"], amt20=q["amt20"],
                        b=b, verdict=verdict, detail=evs[0][2][:40], n_ev=len(evs)))

    O = pd.DataFrame(out).sort_values(["b", "pre5"])
    print("=" * 96)
    print("并购重组公告扫描 · %s · 全市场 %d 条公告 → 事件本体 %d 家公司"
          % (date, len(rows), len(O)))
    print("=" * 96)
    print("%-8s %-8s %-10s %8s %8s %-6s %s"
          % ("代码", "名称", "类型", "前5日%", "20日均额", "档", "历史结论"))
    print("-" * 96)
    for r in O.itertuples(index=False):
        print("%-8s %-8s %-10s %+8.2f %7.2f亿 %-6s %s"
              % (r.code, r.name, r.sub, r.pre5, r.amt20, r.b, r.verdict))
    print("-" * 96)
    pos = O[O["b"].isin(["A", "C"]) & O["sub"].isin(["要约收购", "现金收购", "发股买资产"])]
    neg = O[O["b"] == "E"]
    print("\n【弱信号】历史正期望档：%d 家（均未通过安慰剂检验，不要当买入依据）" % len(pos))
    for r in pos.itertuples(index=False):
        print("   %s %s  %s  %s | %s" % (r.code, r.name, r.sub, r.verdict, r.detail))
    print("\n【回避】前5日已涨超15%%（历史最差档 T+5 -4.35~-4.76%%）：%d 家" % len(neg))
    for r in neg.itertuples(index=False):
        print("   %s %s  前5日 %+.1f%%  | %s" % (r.code, r.name, r.pre5, r.detail))
    print("\n提醒：本表只做筛选，不做买点。披露日开盘跳空均值 +1.69%（停牌型 +3.55%），\n"
          "      开盘价买入在全部持有期均为负期望（T+1 -0.58% / T+5 -0.66%）。")
    print("      ✗ 不要「回踩到披露日开盘价再进」——2026-11-09 七种买法回测证伪：该买法 T+5 -0.47%，\n"
          "        劣于「干脆 D+1 开盘买」的 -0.05%，且成交率仅 35.6%（等不到就踏空）。")
    print("      ✓ 全部并购事件组合回测 -14.5%（随机抽样中位 -12.0%），无可交易 alpha。\n"
          "        仅【要约收购】过安慰剂检验（98.7 分位 / 年化 15.1%），但它 2026 年已衰减到 +0.79%。")


if __name__ == "__main__":
    main()
