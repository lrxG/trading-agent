# -*- coding: utf-8 -*-
"""backfill_zt_reason.py · 涨停池补源（腾讯自选股官方 CLI · v2）。

## 职责

`live_seed` 采的 `limit_up.csv` 三个细节列恒空 ⇒ `mech_driver` 的 **M2 板块扩散槽
无依据**（2026-10-19 实证：链被 drive.review 以 `PREDICTION_INVALID: prediction[1].
baseline 不得为空` 拒收，fail-closed 正确）。本件按 code **匹配补列**，不替换主源。

## 数据来源（全部腾讯自选股官方 CLI，零第三方库）

| 需要什么 | 命令 | 说明 |
|---|---|---|
| 连板天数 | `screen ranking --type limitup_days` | 校验口径用，不写回（boards 归 live_seed 自算） |
| 收盘封单量 | `screen ranking --type limitup_seal_volume` | 单位=**手** |
| 个股所属行业 | `sector constituent <板块码,...> --date D` | 申万二级行业名反查 |

## 口径（写死，防混）

· 池家数/连板数以 **westock 自算为基准**（live_seed，同源自洽）；本件**只补空列**，
  已有的值一律不动；
· `seal_amount`：腾讯给手（1 手=100 股）⇒ ×100×当日 close 换算为**元**；
· `reason`：**申万二级行业名**（`sector constituent` 反查；同票挂多板块取首个）；
· `first_seal_time`：**无历史分时源**（`minute` 只支持当日）⇒ **如实留空**。
  依据：该列只进证据文本（mech_driver 拼"首封 HH:MM:SS"），**不参与任何机械判据**；
  宁可空着也不编造。

## 纪律

· 零第三方库：只用 `westock` CLI + 标准库（本件不 import 任何数据 SDK）；
· CLI 失败/超时 ⇒ 抛错停止，**不静默补零**（缺就是缺）；
· 板块清单复用当日 `sector_ranking.csv`（sector_seed 已采），**不自造第二份清单**。

用法：
  python tools/e2e/backfill_zt_reason.py --from 2026-10-20 --to 2026-10-20            # dry-run
  python tools/e2e/backfill_zt_reason.py --from 2026-10-20 --to 2026-10-20 --execute  # 补列+重promote
"""
import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("fs", "analysis", "decision", "risk", "learning", "e2e"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

import dayclass as DC            # noqa: E402
import writegate as _WG          # noqa: E402
import ingest as ING             # noqa: E402

SRC_NOTE = "westock screen ranking + sector constituent（腾讯自选股 CLI）"


def _run(args, timeout=180):
    """跑 westock CLI → 原文（失败抛错，不静默补零）。"""
    try:
        p = subprocess.run(["westock"] + args, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise RuntimeError("westock CLI 未安装/不在 PATH")
    except subprocess.TimeoutExpired:
        raise RuntimeError("westock %s 超时(%ss)" % (" ".join(args[:2]), timeout))
    if p.returncode != 0:
        raise RuntimeError("westock %s 失败：%s"
                           % (" ".join(args[:2]), (p.stderr or "")[:200]))
    return p.stdout


_MD_ROW = re.compile(r"^\|")


def _md_rows(txt, min_cols=2):
    """解析 CLI 的 markdown 表格 → list[list[str]]（跳过表头/分隔行）。"""
    out = []
    for ln in txt.splitlines():
        s = ln.strip()
        if not _MD_ROW.match(s):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < min_cols:
            continue
        if set("".join(cells)) <= set("-: "):
            continue                      # 分隔行
        if cells[0] in ("#", "code", "代码"):
            continue                      # 表头
        out.append(cells)
    return out


def _norm_code(c):
    """腾讯返回 sz003029 / 数字 → 契约口径 sz003029。"""
    c = (c or "").strip()
    return c if re.match(r"^(sh|sz|bj)\d{6}$", c) else c


def fetch_limitup(date):
    """腾讯源 → {code: {boards, seal_lots}}。

    `limitup_days` 给连板天数、`limitup_seal_volume` 给封单量（手）。
    两个排行**各查一次**（CLI 单次只出一个指标），按 code 合并。
    """
    out = {}

    txt = _run(["screen", "ranking", "--type", "limitup_days",
                "--date", date, "--limit", "500"])
    for cells in _md_rows(txt):
        # | # | 代码 | 名称 | LimitUpDays | TradeDate |
        if len(cells) < 4:
            continue
        code = _norm_code(cells[1])
        try:
            b = int(float(cells[3]))
        except (TypeError, ValueError):
            continue
        out.setdefault(code, {})["boards"] = b

    txt = _run(["screen", "ranking", "--type", "limitup_seal_volume",
                "--date", date, "--limit", "500"])
    for cells in _md_rows(txt):
        # | # | 代码 | 名称 | LimitUpVolume | TradeDate |
        if len(cells) < 4:
            continue
        code = _norm_code(cells[1])
        try:
            v = float(cells[3])
        except (TypeError, ValueError):
            continue
        out.setdefault(code, {})["seal_lots"] = v

    if not out:
        raise RuntimeError("%s 腾讯源无涨停池返回（可能非交易日或数据未出）" % date)
    return out


def fetch_industry_map(date, sector_codes, batch=30):
    """`sector constituent` 批量反查 → {code: 申万行业名}。

    124 板块分 5 批（每批 30）⇒ 约 1-2 分钟。板块清单复用当日 `sector_ranking.csv`
    （sector_seed 已采，不由本件自造第二份清单）。
    同一票挂多个板块时**取首个**（申万二级归属唯一；跨板块=概念叠加，不混口径）。
    """
    out = {}
    for i in range(0, len(sector_codes), batch):
        chunk = sector_codes[i:i + batch]
        txt = _run(["sector", "constituent", ",".join(chunk),
                    "--date", date, "--limit", "0"], timeout=300)
        cur = None
        for ln in txt.splitlines():
            s = ln.strip()
            # 板块头：📈 申万二级行业成分股-消费电子 [申万二级行业] (100 只)
            m = re.match(r"^.*?成分股-(.+?)\s*\[", s)
            if m:
                cur = m.group(1).strip()
                continue
            if not s.startswith("|") or cur is None:
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) < 2 or cells[0] in ("code", "代码"):
                continue
            if set("".join(cells)) <= set("-: "):
                continue
            code = _norm_code(cells[0])
            if re.match(r"^(sh|sz|bj)\d{6}$", code):
                out.setdefault(code, cur)
        if i + batch < len(sector_codes):
            time.sleep(1.0)                       # CLI 节流
    return out


def _sector_codes(date, base):
    """读板块清单（不另造清单）：当日优先，缺则回退到最近一日。

    申万二级分类**跨日稳定**（不是每日重编），故历史日缺 `sector_ranking.csv` 时，
    用最近一日清单去查 `sector constituent --date <历史日>` 仍然正确——
    反过来若某板块当日临时无数据就放弃反查，那才是错误的保守。
    """
    root = base or _ROOT
    p = os.path.join(root, "data", "raw", date, "sector_ranking.csv")
    if os.path.isfile(p):
        with io.open(p, encoding="utf-8-sig") as f:
            return [r["code"] for r in csv.DictReader(f) if r.get("code")]
    # 回退：扫 data/raw/<日期>/sector_ranking.csv，取日期最大的那份
    raw = os.path.join(root, "data", "raw")
    cands = []
    if os.path.isdir(raw):
        for fn in os.listdir(raw):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", fn) and \
                    os.path.isfile(os.path.join(raw, fn, "sector_ranking.csv")):
                cands.append(fn)
    if not cands:
        return []
    with io.open(os.path.join(raw, max(cands), "sector_ranking.csv"),
                 encoding="utf-8-sig") as f:
        return [r["code"] for r in csv.DictReader(f) if r.get("code")]


def _close_of(date, base, codes):
    """当日 close（seal 手→元 换算需要）。缺则 None（该票不换算，留空）。"""
    p = os.path.join(base or _ROOT, "data", "raw", date, "daily_quote.csv")
    out = {}
    if not os.path.isfile(p):
        return out
    with io.open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("code") in codes:
                try:
                    out[r["code"]] = float(r["close"])
                except (TypeError, ValueError):
                    pass
    return out


def backfill_day(date, base=None, execute=False, recompute=False):
    """补一天的 limit_up.csv 细节列。→ (path, n_filled, n_total, notes)。

    `recompute=True`：**覆盖已有值**（不只补空）——用于把历史上由不可信来源填过的值
    全部替换为腾讯源重算值。默认 False（只补空列，不动既有值）。
    """
    root = base or _ROOT
    p = os.path.join(root, "data", "raw", date, "limit_up.csv")
    if not os.path.isfile(p):
        return None, 0, 0, ["%s 无 limit_up.csv（跳过）" % date]
    with io.open(p, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
        f.seek(0)
        header = f.readline().strip().lstrip("\ufeff").split(",")

    pool = fetch_limitup(date)                     # {code: {boards, seal_lots}}
    need_reason = [r for r in rows
                   if recompute or not (r.get("reason") or "").strip()]
    sec_codes = _sector_codes(date, base) if need_reason else []
    ind_map = fetch_industry_map(date, sec_codes) if sec_codes else {}
    close_map = _close_of(date, base, {r.get("code") for r in rows})

    filled = {"first_seal_time": 0, "seal_amount": 0, "reason": 0}
    replaced = {"seal_amount": 0, "reason": 0}     # recompute 模式下被覆盖的计数
    no_close = []                                  # 缺收盘价 ⇒ seal 无法换元（如实记）
    for r in rows:
        code = r.get("code")
        m = pool.get(code)
        # ① 封单量（手→元）：需当日 close，缺则不换算
        if recompute or not (r.get("seal_amount") or "").strip():
            lots = (m or {}).get("seal_lots")
            px = close_map.get(code)
            if lots and px:
                had = (r.get("seal_amount") or "").strip()
                r["seal_amount"] = str(int(round(float(lots) * 100 * px)))
                filled["seal_amount"] += 1
                if recompute and had:
                    replaced["seal_amount"] += 1
            elif lots and not px:
                no_close.append(code)
        # ② 行业（申万二级反查）
        if recompute or not (r.get("reason") or "").strip():
            ind = ind_map.get(code)
            if ind:
                had = (r.get("reason") or "").strip()
                r["reason"] = ind
                filled["reason"] += 1
                if recompute and had:
                    replaced["reason"] += 1
        # ③ first_seal_time：无历史分时源 ⇒ 保持空（不编造）
    notes = ["%s 补列：%s（池 %d 只 / 涨停池 %d 只 / 行业反查 %d 码）%s"
             % (date, filled, len(rows), len(pool), len(ind_map),
                " · 覆盖已有值 %s" % replaced if recompute else "")]
    if no_close:
        notes.append("  ⚠ %d 只缺当日 close，封单未换算（留空）：%s"
                     % (len(no_close), ",".join(no_close[:5])))
    if need_reason and not sec_codes:
        notes.append("  ⚠ 无板块清单 ⇒ 行业反查跳过（reason 留空）")
    if any(not (r.get("first_seal_time") or "").strip() for r in rows):
        notes.append("  ⚠ first_seal_time 留空：无历史分时源（该列只进证据文本）")
    if execute:
        _WG.check_write(p)
        tmp = p + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            for r in rows:
                w.writerow({k: (r.get(k) if r.get(k) is not None else "")
                            for k in header})
        os.replace(tmp, p)
        # snapshot 同步（promote 幂等：EXISTS 时 force 重写）
        ING.promote_snapshot(date, base=base, force=True,
                             provenance={"actor": "backfill_zt_reason",
                                         "reason": SRC_NOTE})
        notes.append("  → 已写回 + snapshot 重promote")
    return p, sum(filled.values()), len(rows), notes


def _main(argv):
    ap = argparse.ArgumentParser(description="涨停池补源（腾讯自选股官方 CLI）")
    ap.add_argument("--from", dest="d1", required=True)
    ap.add_argument("--to", dest="d2", required=True)
    ap.add_argument("--base", default=None)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--recompute", action="store_true",
                    help="覆盖已有值（用腾讯源重算全部细节列，而非只补空）")
    a = ap.parse_args(argv)
    cal = DC.Calendar.from_seed_csv(
        os.path.join(a.base or _ROOT, "data", "raw", "meta", "trade_cal.csv"),
        source="backfill")
    days = [d for d in cal.sessions_between(a.d1, a.d2)]
    if not days:
        print("⛔ 日历窗口内无交易日")
        return 2
    for d in days:
        p, n, total, notes = backfill_day(d, base=a.base, execute=a.execute,
                                          recompute=a.recompute)
        for x in notes:
            print(x)
        time.sleep(1.0)                       # CLI 节流
    print("完成 %d 日%s" % (len(days), "（已写回）" if a.execute else "（dry-run）"))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
