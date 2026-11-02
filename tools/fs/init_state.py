#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""init_state.py · 干净机器冷启动：初始化 data/state 状态骨架。

用途
    仓库只分发**系统本体**，不含任何个人记忆与运行态账本（见 .gitignore）。
    新机器 clone 后必须跑一次本件，把状态骨架建出来，否则 wakeup/cursor 会按设计
    拒绝工作（"游标文件不存在：需先初始化，不得静默新建"）。

设计原则
    · **幂等**：已存在的文件默认跳过，不覆盖（保护历史账本）。要重来加 --force。
    · **零伪造**：只写"空账本 + 明确初值"，绝不预填任何行情/判断/持仓数据。
    · 资金初值写 **0**：C5 资金闸会因此拒绝开新仓（fail-closed），用户填真实值后才放行
      —— 这是设计意图，不是 bug。填法见本文件末尾说明。

用法
    python tools/fs/init_state.py --date 2026-11-02
    python tools/fs/init_state.py --date 2026-11-02 --force      # 覆盖重建（危险，账本会清空）
    python tools/fs/init_state.py --selftest

产出（data/state/ 下）
    cursor.json            游标：date=指定日，五步 checklist 全 False（该日待结账）
    run_log.jsonl          运转留痕（空）
    h_ledger.jsonl         假设账本（空）
    timing_log.jsonl       择时操作日志（空）
    candidates.json        候选池（空）
    mainline.json          主线识别结果（空）
    timing_state.json      择时状态（OFF 起步，无参数）
    learn_weekly_state.json 学习周闸状态（空）
    account.json           账户权益（capital=0，须用户填真实值）
    paper_portfolio.json   模拟盘（现金 0 / 空仓）
"""
import argparse
import datetime
import io
import json
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
STATE = os.path.join(ROOT, "data", "state")
STEPS = ["行情落定", "成交记录", "盈亏结算", "复盘归因", "学习反馈"]


def _w_json(path, obj, force):
    if os.path.exists(path) and not force:
        return "skip"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return "new"


def _w_empty_lines(path, force):
    if os.path.exists(path) and not force:
        return "skip"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    io.open(path, "w", encoding="utf-8", newline="\n").close()
    return "new"


def init(date, force=False, quiet=False):
    """初始化状态骨架。返回 {文件名: new|skip}。"""
    date = (date or datetime.date.today().isoformat())[:10]
    datetime.date.fromisoformat(date)          # 校验格式，非法即抛
    plan = {}

    plan["cursor.json"] = _w_json(os.path.join(STATE, "cursor.json"), {
        "version": 1,
        "date": date,
        "checklist": {s: False for s in STEPS},
    }, force)

    plan["run_log.jsonl"] = _w_empty_lines(os.path.join(STATE, "run_log.jsonl"), force)
    plan["h_ledger.jsonl"] = _w_empty_lines(os.path.join(STATE, "h_ledger.jsonl"), force)
    plan["timing_log.jsonl"] = _w_empty_lines(os.path.join(STATE, "timing_log.jsonl"), force)

    plan["candidates.json"] = _w_json(os.path.join(STATE, "candidates.json"), {
        "version": "candidates@1", "as_of": date, "items": [],
        "note": "空池初值；由 scan/候选刷新写入",
    }, force)

    plan["mainline.json"] = _w_json(os.path.join(STATE, "mainline.json"), {
        "version": "mainline@1", "as_of": date, "sectors": [],
        "note": "空初值；由 mainline_scan.py 写入",
    }, force)

    plan["timing_state.json"] = _w_json(os.path.join(STATE, "timing_state.json"), {
        "version": "timing@1", "as_of": date, "signal": "OFF", "params": {},
        "note": "OFF 起步（无参数选择前不得开仓）；由 timing/v6_grid 月末重选写入",
    }, force)

    plan["learn_weekly_state.json"] = _w_json(os.path.join(STATE, "learn_weekly_state.json"), {
        "version": 1, "week": None, "note": "空初值；由 learn_pipeline --weekly 写入",
    }, force)

    plan["account.json"] = _w_json(os.path.join(STATE, "account.json"), {
        "version": "account@1", "owner": "USER", "currency": "CNY",
        "capital": 0.0, "capital_as_of": date,
        "status": "UNSET_NEEDS_USER",
        "note": "★必填：capital 改为你的真实总权益（现金+持仓市值），capital_as_of 同步改今天。"
                "capital=0 时 C5 资金闸拒绝一切开仓（fail-closed，设计如此）。",
        "equity_curve": [],
    }, force)

    plan["paper_portfolio.json"] = _w_json(os.path.join(STATE, "paper_portfolio.json"), {
        "version": "paper@1-3layer", "created": date, "capital_init": 0.0,
        "cash": 0.0, "positions": [],
        "note": "空模拟盘初值；三层框架：核心仓择时轮动+卫星仓(仅RISK_ON)+空仓期国债",
    }, force)

    if not quiet:
        print("初始化状态骨架 @ %s" % date)
        for k, v in plan.items():
            print("  [%s] %s" % ("NEW " if v == "new" else "SKIP", k))
        if plan.get("account.json") == "new":
            print("\n⚠️  data/state/account.json 的 capital=0 —— 填入真实权益后 C5 才放行开仓。")
    return plan


def _selftest():
    """自检：用临时目录验证幂等与格式，不触碰生产 data/state。"""
    import tempfile
    global STATE
    ok = fail = 0
    bak = STATE
    with tempfile.TemporaryDirectory() as td:
        STATE = td
        r1 = init("2026-11-02", force=False, quiet=True)
        n1 = sum(1 for v in r1.values() if v == "new")
        r2 = init("2026-11-02", force=False, quiet=True)
        n2 = sum(1 for v in r2.values() if v == "new")
        for cond, msg in (
            (n1 == len(r1), "A1 首跑全部新建（%d/%d）" % (n1, len(r1))),
            (n2 == 0, "A2 二次跑全部跳过（幂等，新建 %d）" % n2),
            (json.load(io.open(os.path.join(td, "cursor.json"), encoding="utf-8"))["date"] == "2026-11-02",
             "A3 游标日期正确"),
            (json.load(io.open(os.path.join(td, "account.json"), encoding="utf-8"))["capital"] == 0.0,
             "A4 资金初值=0（fail-closed）"),
            (len(json.load(io.open(os.path.join(td, "cursor.json"), encoding="utf-8"))["checklist"]) == 5,
             "A5 五步 checklist 齐"),
        ):
            if cond:
                ok += 1
            else:
                fail += 1
                print("  [FAIL]", msg)
        # 非法日期必须抛
        try:
            init("2026-13-45", force=False, quiet=True)
            fail += 1
            print("  [FAIL] A6 非法日期未抛错")
        except ValueError:
            ok += 1
    STATE = bak
    print("init_state --selftest: %d/%d 通过" % (ok, ok + fail))
    return 0 if fail == 0 else 1


def main(argv):
    ap = argparse.ArgumentParser(description="干净机器冷启动：初始化 data/state 骨架（幂等）")
    ap.add_argument("--date", default=None, help="游标起始日 YYYY-MM-DD，默认今天")
    ap.add_argument("--force", action="store_true", help="覆盖已有文件（危险：会清空账本）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    init(a.date, force=a.force)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
