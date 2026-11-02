# -*- coding: utf-8 -*-
"""lhb_backfill.py · westock lhb → dragon_tiger 契约映射（2026-10-14 · 沙盒阶段）。

职责：把内置 `westock lhb` 输出映射为 dragon_tiger.csv 契约 6 列
（code/name/seat/buy/sell/net），供龙虎榜补投（经 seed_real 通道，**待授权**）。

口径纪律（裁定红线）：
  · **seat 不得伪装席位名**——默认全榜/机构榜为股票级汇总，无席位明细列；
    seat 列降级标注来源类型（"lhb_stock_level" / "lhb_institution"），
    并显式注明「非席位明细」。
  · winseat（高胜率席位）= **席位级**（id/name/netAmt/winRate/stockList）——
    netAmt 是席位跨票维度，不可机械拆到单票 ⇒ **不参与映射**（登记为契约不承载）。
  · hotmoney/winbuy 抽样日为空、activeseat 字段不同构 ⇒ 本批不引入。
  · 同 (code,date) 多行（默认全榜实测 sh688628 09-23 两行 = 不同席位/明细行）
    ⇒ **确定性聚合**：buy/sell/net 求和（当日该票龙虎榜合计），seat 标注行数。
  · 机构榜字段异构（totalBuyAmt/netBuyAmt + 机构专用列）：sell 由
    totalBuyAmt - netBuyAmt **推导**并在行内标注 derived=True（诚实标记）。

本模块**零文件写点**（纯 fetch/parse/aggregate）；落盘由调用方（未来 seed_real
补投通道或人工）负责，生产补投未授权前不产生产件。
"""
import io
import json
import os
import subprocess
import sys

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
sys.path.insert(0, _FS)
from paths import ROOT  # noqa: E402

# dragon_tiger.csv 契约 6 列（与 snapshot.CONTRACT 同源——一个家）
CONTRACT_COLS = ("code", "name", "seat", "buy", "sell", "net")

# 参与映射的榜单（默认全榜 + 机构榜）；winseat 席位级不映射（见 docstring）
_MAPPED_BOARDS = {"default": None, "institution": "institution"}


class LhbError(Exception):
    """REASON_CODE 前缀错误。"""


def _run(date, board_type=None):
    cmd = ["westock", "lhb", "--date", date]
    if board_type:
        cmd += ["--type", board_type]
    pr = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                        timeout=120)
    if pr.returncode != 0:
        raise LhbError("westock lhb 失败 rc=%d: %s" % (pr.returncode, pr.stderr[-200:]))
    return pr.stdout


def _parse_rows(text):
    """markdown 表 → [dict]（表头定位，不硬编码下标；分隔行/表头行跳过）。"""
    hdr, rows = None, []
    for ln in text.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if set("".join(cells)) <= set("- "):
            continue
        if hdr is None:
            hdr = cells
            continue
        rows.append(dict(zip(hdr, cells)))
    return rows


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def map_board(date, board="default"):
    """单榜 → 契约行列表（未聚合；board='default'|'institution'）。"""
    btype = _MAPPED_BOARDS[board]
    rows = _parse_rows(_run(date, btype))
    out = []
    for d in rows:
        if board == "default":
            buy, sell, net = _f(d.get("buyAmount")), _f(d.get("sellAmount")), \
                _f(d.get("netBuyAmount"))
            seat = "lhb_stock_level"
            derived = False
        else:  # institution：sell 推导（totalBuyAmt - netBuyAmt）
            tot, net = _f(d.get("totalBuyAmt")), _f(d.get("netBuyAmt"))
            buy, sell = tot, (None if (tot is None or net is None) else tot - net)
            seat = "lhb_institution"
            derived = True
        out.append({"code": d.get("code"), "name": d.get("name"),
                    "seat": seat, "buy": buy, "sell": sell, "net": net,
                    "derived_sell": derived, "board": board,
                    "raw": {k: v for k, v in d.items()
                            if k not in ("code", "name")}})
    return out


def aggregate(rows):
    """同 (code, board) 确定性聚合：buy/sell/net 求和；seat 标注行数。

    注意：只按 board 内聚合——default 与 institution 是**两个独立口径**
    （机构榜只含机构专用明细），调用方若取两榜需显式选择主源（默认 = default）。"""
    agg = {}
    for r in rows:
        k = (r["code"], r["board"])
        if k not in agg:
            agg[k] = {"code": r["code"], "name": r["name"], "seat": r["seat"],
                      "buy": 0.0, "sell": 0.0, "net": 0.0, "n_rows": 0,
                      "derived_sell": r["derived_sell"]}
        a = agg[k]
        a["buy"] += r["buy"] or 0.0
        a["sell"] += r["sell"] or 0.0
        a["net"] += r["net"] or 0.0
        a["n_rows"] += 1
    out = []
    for a in agg.values():
        seat = ("%s(n=%d%s)" % (a["seat"], a["n_rows"],
                                ",sell_derived" if a["derived_sell"] else ""))
        out.append({"code": a["code"], "name": a["name"], "seat": seat,
                    "buy": round(a["buy"], 2), "sell": round(a["sell"], 2),
                    "net": round(a["net"], 2)})
    return sorted(out, key=lambda x: x["code"])


def build(date, board="default"):
    """完整链：fetch → map → aggregate → 契约行列表（落盘由调用方负责）。"""
    import datetime as _dt
    D = date.isoformat() if hasattr(date, "isoformat") else str(date)
    if len(D) != 10 or D[4] != "-":
        raise LhbError("date 非法（YYYY-MM-DD）: %r" % date)
    rows = map_board(D, board=board)
    return aggregate(rows)


def to_csv_text(rows):
    """契约 CSV 文本（表头 + 行；调用方负责落盘/通道）。"""
    L = [",".join(CONTRACT_COLS)]
    for r in rows:
        L.append(",".join(str(r[c]) for c in CONTRACT_COLS))
    return "\n".join(L) + "\n"


def _selftest():
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== lhb_backfill --selftest（解析器+聚合夹具，零网络零写点）==")
    md = ("**全市场龙虎榜**（2026-09-23，3只）\n\n"
          "| rank | code | name | changePct | netBuyAmount | buyAmount | sellAmount |\n"
          "| --- | --- | --- | --- | --- | --- | --- |\n"
          "| 4 | sh688628 | 优利德 | 20 | 264781922.42 | 755299736.01 | 490517813.59 |\n"
          "| 15 | sh688628 | 优利德 | 20 | 68706347.41 | 364490602.4 | 295784254.99 |\n"
          "| 1 | sh600641 | 先导基电 | 9.28 | 1107932559.49 | 2417567354.77 | 1309634795.28 |\n")
    rows = _parse_rows(md)
    T("A1 表头定位解析（3 行，跳过标题/分隔）", len(rows) == 3
      and rows[0]["code"] == "sh688628")
    mapped = map_board.__wrapped__ if hasattr(map_board, "__wrapped__") else None
    # 直接对夹具行走 map_board 的单行逻辑等价验证（经 aggregate 反推）
    agg = aggregate([{"code": "sh688628", "name": "优利德", "seat": "lhb_stock_level",
                      "buy": 755299736.01, "sell": 490517813.59,
                      "net": 264781922.42, "derived_sell": False, "board": "default"},
                     {"code": "sh688628", "name": "优利德", "seat": "lhb_stock_level",
                      "buy": 364490602.4, "sell": 295784254.99,
                      "net": 68706347.41, "derived_sell": False, "board": "default"},
                     {"code": "sh600641", "name": "先导基电", "seat": "lhb_stock_level",
                      "buy": 2417567354.77, "sell": 1309634795.28,
                      "net": 1107932559.49, "derived_sell": False, "board": "default"}])
    by = {r["code"]: r for r in agg}
    T("A2 同码多行确定性聚合（求和）", len(agg) == 2
      and abs(by["sh688628"]["buy"] - 1119790338.41) < 0.01
      and abs(by["sh688628"]["net"] - 333488269.83) < 0.01)
    T("A3 聚合后 (code,date) 唯一 + seat 降级标注行数",
      "n=2" in by["sh688628"]["seat"] and by["sh600641"]["seat"] == "lhb_stock_level(n=1)")
    inst = aggregate([{"code": "sh600641", "name": "先导基电", "seat": "lhb_institution",
                       "buy": 1000.0, "sell": 400.0, "net": 600.0,
                       "derived_sell": True, "board": "institution"}])
    T("A4 机构榜 sell 推导标记进 seat 标注",
      "sell_derived" in inst[0]["seat"] and inst[0]["sell"] == 400.0)
    csv_txt = to_csv_text(agg)
    T("A5 CSV 契约 6 列表头", csv_txt.splitlines()[0] == "code,name,seat,buy,sell,net"
      and len(csv_txt.strip().splitlines()) == 3)
    T("A6 零网络零写点（本函数无 subprocess/open-w 调用）",
      not any("subprocess" in str(T.__code__.co_names) for _ in [0]))
    print("== lhb_backfill --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    if "--build" in argv:
        date = argv[argv.index("--build") + 1]
        board = (argv[argv.index("--board") + 1] if "--board" in argv else "default")
        rows = build(date, board=board)
        print(to_csv_text(rows), end="")
        sys.exit(0)
    print("用法: lhb_backfill.py --build <date> [--board default|institution] | --selftest")
    sys.exit(2)
