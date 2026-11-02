#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""channel_health.py · 数据通道健康检查（防"MCP 掉线 4 小时无人发现"）。

背景（2026-11-02 事故）：westock MCP 四件（快讯/热搜/新闻搜索/公告）长期 auth 失效，
期间所有盘中归因只能靠 CLI 单腿，直到用户质疑才暴露——**静默降级没人报**。

用法
    python tools/fs/channel_health.py            # 人读
    python tools/fs/channel_health.py --json     # 机器读（供日链/唤醒调用）
    python tools/fs/channel_health.py --selftest

判定
    · CLI 腿（westock quote / news / notice）：直接跑子命令，有输出=OK
    · MCP 腿：本进程无法直接探测（MCP 宿主在平台侧）；读最近一次成功调用的时间戳
      data/state/channel_mcp_ok.json（由 Agent 侧在成功调用后写入），超过 24h 视为 STALE
    · 任一腿 FAIL/STALE ⇒ 退出码 1（调用方可据此告警），但不阻塞主链
"""
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
STATE = os.path.join(ROOT, "data", "state")
MCP_STAMP = os.path.join(STATE, "channel_mcp_ok.json")
STALE_HOURS = 24

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cli_codes as CC                                          # noqa: E402


def _run(args, timeout=40):
    try:
        r = subprocess.run(["westock"] + args, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "").strip()
        return (r.returncode == 0 and len(out) > 20), (r.stderr or "").strip()[:120]
    except FileNotFoundError:
        return False, "westock CLI 未安装或不在 PATH"
    except subprocess.TimeoutExpired:
        return False, "超时 %ds" % timeout
    except Exception as e:                                       # noqa: BLE001
        return False, type(e).__name__


def _run_table(args, timeout=60):
    """表格类命令判真：必须有**数据行**，且不含业务错误标记。

    ★ 2026-11-02 事故修（fail-open）：原先只判 `returncode==0 and len(out)>20`。
      而 westock CLI **业务错误不改退出码**，报错文本（如
      `查询dayK线失败：[code=1620053001] ... service error`）本身远长于 20 字符
      ⇒ 通道明明是坏的，健康检查**恒定报 ✓**。这就是"kline 全挂两天没人发现"的原因。
    """
    try:
        r = subprocess.run(["westock"] + args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, "westock CLI 未安装或不在 PATH"
    except subprocess.TimeoutExpired:
        return False, "超时 %ds" % timeout
    except Exception as e:                                       # noqa: BLE001
        return False, type(e).__name__
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        return False, "rc=%d %s" % (r.returncode, out.strip()[:100])
    err = CC.cli_error(out)
    if err:
        return False, "业务错误：%s" % err
    if not CC.has_table_rows(out):
        return False, "返回无数据行（空表）"
    return True, "OK"


def check(deep=False):
    """→ {腿: {ok, note}}"""
    res = {}
    ok, note = _run_table(["quote", "sh000300"])
    res["cli_quote"] = {"ok": ok, "note": note if not ok else "行情"}
    ok, note = _run_table(["kline", "sh600519", "--period", "day", "--limit", "2"])
    res["cli_kline"] = {"ok": ok, "note": note if not ok else "日K"}
    ok, note = _run(["news", "list", "sh000001", "--type", "2", "--limit", "3"])
    res["cli_news"] = {"ok": ok, "note": note or "快讯"}
    if deep:
        ok, note = _run(["notice", "list", "sh600519", "--limit", "3"], timeout=60)
        res["cli_notice"] = {"ok": ok, "note": note or "公告"}

    # MCP 腿：读最近成功时间戳
    try:
        d = json.load(open(MCP_STAMP, encoding="utf-8")) if os.path.isfile(MCP_STAMP) else {}
        ts = float(d.get("epoch") or 0)
        age_h = (time.time() - ts) / 3600.0 if ts else None
        fresh = age_h is not None and age_h <= STALE_HOURS
        res["mcp_news"] = {"ok": fresh,
                           "note": "最近成功 %.1f 小时前" % age_h if age_h is not None
                                   else "无成功记录（MCP 可能从未连通或已失效）"}
    except Exception as e:                                       # noqa: BLE001
        res["mcp_news"] = {"ok": False, "note": "时间戳读取失败：%s" % e}
    return res


def mark_mcp_ok():
    """Agent 侧在 MCP 调用成功后调用本函数打时间戳（供下次健康检查判定）。"""
    os.makedirs(STATE, exist_ok=True)
    tmp = MCP_STAMP + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"epoch": time.time(),
                   "iso": time.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False)
    os.replace(tmp, MCP_STAMP)
    return MCP_STAMP


def _selftest():
    ok = fail = 0

    def T(c, m):
        nonlocal ok, fail
        if c:
            ok += 1
        else:
            fail += 1
            print("  [FAIL]", m)

    r = check()
    T(set(r) >= {"cli_quote", "cli_news", "mcp_news"}, "A1 返回三条腿")
    T(all("ok" in v and "note" in v for v in r.values()), "A2 结构完整")
    # 时间戳写入/读取闭环
    bak = None
    if os.path.isfile(MCP_STAMP):
        bak = open(MCP_STAMP, encoding="utf-8").read()
    mark_mcp_ok()
    d = json.load(open(MCP_STAMP, encoding="utf-8"))
    T(d.get("epoch", 0) > 0, "A3 mark_mcp_ok 落时间戳")
    T(check()["mcp_news"]["ok"] is True, "A4 刚标记后 MCP 腿判 OK")
    # 损坏文件 → 不抛异常
    with open(MCP_STAMP, "w", encoding="utf-8") as f:
        f.write("{bad json")
    T(check()["mcp_news"]["ok"] is False, "A5 损坏时间戳 → 判 FAIL 但不抛异常")
    if bak is not None:
        open(MCP_STAMP, "w", encoding="utf-8").write(bak)
    else:
        os.remove(MCP_STAMP)
    print("channel_health --selftest: %d/%d 通过" % (ok, ok + fail))
    return 0 if fail == 0 else 1


def main(argv):
    ap = argparse.ArgumentParser(description="数据通道健康检查（CLI+MCP 双腿）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--deep", action="store_true", help="加测公告腿（较慢）")
    ap.add_argument("--mark-mcp-ok", action="store_true", help="MCP 调用成功后打时间戳")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if a.mark_mcp_ok:
        print(mark_mcp_ok())
        return 0
    res = check(deep=a.deep)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        for k, v in res.items():
            print("%-12s %s  %s" % (k, "✓" if v["ok"] else "✗", v["note"]))
    return 0 if all(v["ok"] for v in res.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
