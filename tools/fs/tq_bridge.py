# -*- coding: utf-8 -*-
"""tq_bridge.py · 通达信 TQ 本地服务桥接（**只读** · 2026-10-21 建）。

## 是什么

通达信客户端（`D:\\new_tdx64\\Tdxw.exe`）运行时会开本地 HTTP 服务
`http://127.0.0.1:17709/`，提供 59 个 JSON-RPC 接口（TdxQuant 平台）。
本件是**只读桥接**：只调行情/财务/板块/公式类接口，**不碰任何交易接口**。

## 为什么需要

当前数据源（westock CLI）有三个短板：
1. 历史 K 线覆盖不均（2024-01~2025-08 每月仅 300~440 行，残缺）
2. 无"选股公式"能力（通达信有 `formula_xg`，几百个现成公式直接算）
3. 无专业交易序列（`get_gpjy_value` 等非普通 OHLC 数据）

## 硬边界（**不可绕过**）

| 边界 | 说明 |
|---|---|
| ❌ **禁调交易接口** | `order_stock` / `cancel_order_stock` / `query_stock_positions` 等**一律禁用**（本件主动拦截） |
| ❌ **禁写通达信** | `create_sector` / `send_user_block` / `send_message` 等写接口禁用 |
| ✅ 只读白名单 | 见 `READ_ONLY_METHODS`，白名单外一律拒 |
| ⚠️ 依赖 GUI | 通达信是 GUI 程序，**必须用户手动启动**；未启动 ⇒ 本件明确报错，不静默降级 |

## 状态探测

```bash
python tools/fs/tq_bridge.py --check     # 探测 端口/进程/安装（只读）
python tools/fs/tq_bridge.py --methods   # 列出可用只读接口
python tools/fs/tq_bridge.py --probe-dll # 实测 tqcenter DLL 直连（需 numpy/pandas）
```

## 2026-10-21 实测结论（**重要，别重复踩**）

### 三套字段体系（极易混，务必先查 SKILL.md 再写代码）

| 接口 | type | 字段前缀 | 内容 | 实测状态 |
|---|---|---|---|---|
| `get_market_data` | — | OHLCV | K线/分时 | ✅ **完整可用**（已补 100 万行历史） |
| `get_gp_one_data` | **9** | **GO**（非 GP！） | 一致预期财务/机构持股/业绩预告 | ✅ **可用**（600519 返回 33/46 非零） |
| `get_gpjy_value` | **3** | **GP** | 涨停/龙虎榜/融资/大宗等交易序列 | ⚠️ 接口通但**返回空**（本地未下载该数据包） |
| `get_financial_data` | 1 | FN | 专业财务序列 | 未实测 |

> ⚠️ **2026-10-21 踩坑记录**：先按 GP 字段名调 `get_gp_one_data`，返回 `GO*` 编号且值
> 看似涨停数据 → 一度误判"字段编号错位"。查 SKILL.md 第 2137 行才确认：**GO 是财务、
> GP 是交易，两套独立**。`GO14` = 一致预期T年营业利润（不是"涨停金额"）。
> **教训：动手前先读对应接口的字段定义段，别拿另一套字段名去套。**

### 两条通路的失败根因（同一条）

| 通路 | 依赖 | 实测 |
|---|---|---|
| A. HTTP JSON-RPC `127.0.0.1:17709` | 通达信启动（**启动后自动监听**） | ✅ 可用 |
| B. `tqcenter.py` + `TPythClient.dll` 直连 | 需 numpy/pandas（已装隔离 venv） | ✅ 能 init_ok |

若服务未就绪 ⇒ `SERVICE_DOWN`，如实报错 + 操作指引，不静默降级。

> ⚠️ **`tqcenter.py` 的异常处理有缺陷**（通达信官方代码）：`except` 把所有异常统一报成
> 「连接路径为空，请先调用 initialize(path)」，**真实错误被吞**。
> 排查时勿被这句误导——路径明明传了，真因是 `InitConnect` 失败。

### GP 交易数据的可用性（**未解决，需要你操作**）

`get_gpjy_value`（GP01~GP46：涨停金额/封单/封成比/首次涨停时间/龙虎榜/融资/大宗）
**接口通、ErrorId=0，但 Value 全为 null** —— 通达信本地**没有下载专业交易数据包**。
这需要在通达信客户端里手动执行「专业数据下载」（GUI 操作），本工具无法触发
（`download_file` 属写接口且只下股东/舆情，不含 GP 序列）。

**一旦你下载完，这部分能力立刻可用**——它们是打板研究的关键字段：
`GP14` 涨停金额(板上成交)+开板次数 · `GP15` 涨跌停状态+封单金额 ·
`GP22` 封成比+封流比 · `GP24` 首次涨停时间+最大封单额 ·
`GP39` 近1年首板封板率+**次日红盘率** · `GP40` 近1年连板率
"""
import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request

TQ_HOST = "127.0.0.1"
TQ_PORT = 17709
TQ_URL = "http://%s:%d/" % (TQ_HOST, TQ_PORT)

# 通达信安装路径（注册表实测值；可由 --tdx-dir 覆盖）
DEFAULT_TDX_DIR = r"D:\new_tdx64"

# ── 只读白名单（行情/基础/板块/财务/公式/日历）────────────────────────────
READ_ONLY_METHODS = (
    # 行情
    "get_market_data", "get_market_snapshot", "get_pricevol", "get_divid_factors",
    "refresh_cache", "refresh_kline",
    # 基础信息
    "get_stock_info", "get_match_stkinfo", "get_stock_list", "get_more_info",
    "get_gb_info", "get_gb_info_by_date", "get_kzz_info", "get_ipo_info",
    "get_trackzs_etf_info", "get_relation",
    # 板块
    "get_sector_list", "get_user_sector", "get_stock_list_in_sector",
    # 日历
    "get_trading_calendar", "get_trading_dates",
    # 财务/专业序列
    "get_financial_data", "get_financial_data_by_date",
    "get_gpjy_value", "get_gpjy_value_by_date",
    "get_bkjy_value", "get_bkjy_value_by_date",
    "get_scjy_value", "get_scjy_value_by_date", "get_gp_one_data",
    # 公式（只算，不写）
    "formula_set_data", "formula_set_data_info", "formula_get_data",
    "formula_zb", "formula_xg", "formula_exp",
    "formula_process_mul_xg", "formula_process_mul_zb",
    "formula_process_mul_exp", "formula_get_all", "formula_get_info",
)

# ── 明令禁止（交易与写入）──────────────────────────────────────────────────
FORBIDDEN_METHODS = (
    "order_stock", "cancel_order_stock", "stock_account",
    "query_stock_asset", "query_stock_orders", "query_stock_positions",
    "create_sector", "delete_sector", "rename_sector", "clear_sector",
    "send_message", "send_file", "send_warn", "send_bt_data",
    "send_user_block", "exec_to_tdx", "download_file",
)


class TqError(Exception):
    """REASON_CODE 前缀错误。"""


def port_open(host=TQ_HOST, port=TQ_PORT, timeout=1.0):
    """端口是否在监听（最轻的探测，不发请求）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False
    finally:
        s.close()


def check_status():
    """→ 状态字典（不止端口，还给可用性判定与原因）。"""
    dir_ok = os.path.isfile(os.path.join(DEFAULT_TDX_DIR, "Tdxw.exe"))
    tq_py = os.path.join(DEFAULT_TDX_DIR, "PYPlugins", "user", "tqcenter.py")
    py_ok = os.path.isfile(tq_py)
    running = port_open()
    st = {"tdx_dir": DEFAULT_TDX_DIR, "tdx_installed": dir_ok,
          "tqcenter_py": py_ok, "port": TQ_PORT, "service_running": running,
          "usable": bool(dir_ok and running)}
    if not dir_ok:
        st["reason"] = "通达信未安装于 %s（注册表实测路径）" % DEFAULT_TDX_DIR
    elif not running:
        st["reason"] = ("通达信已安装但**未运行**，%d 端口无监听 ⇒ 请手动启动 "
                        "`%s\\Tdxw.exe`（GUI 程序，本工具无法后台拉起）"
                        % (TQ_PORT, DEFAULT_TDX_DIR))
    else:
        st["reason"] = None
    return st


def call(method, params=None, timeout=60):
    """JSON-RPC 调用（**只读方法白名单校验**）。→ result。

    params 按通达信约定：`{"id": 1, "method": <name>, "params": {...}}`。
    """
    if method in FORBIDDEN_METHODS:
        raise TqError("METHOD_FORBIDDEN: %r 属交易/写入接口，本桥接明确禁用" % method)
    if method not in READ_ONLY_METHODS:
        raise TqError("METHOD_NOT_WHITELISTED: %r 不在只读白名单内（防误调未知接口）"
                      % method)
    if not port_open():
        raise TqError("SERVICE_DOWN: %d 端口无监听（通达信未启动？见 --check）" % TQ_PORT)
    payload = json.dumps({"id": 1, "method": method,
                          "params": params or {}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(TQ_URL, data=payload,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        raise TqError("HTTP_ERROR: %s" % str(e)[:200])
    try:
        doc = json.loads(raw)
    except ValueError:
        raise TqError("BAD_RESPONSE: 非 JSON（前 200 字：%s）" % raw[:200])
    if isinstance(doc, dict) and doc.get("error"):
        raise TqError("REMOTE_ERROR: %s" % str(doc.get("error"))[:300])
    return doc.get("result", doc) if isinstance(doc, dict) else doc


def get_market_data(stock_list, period="1d", start_time="", end_time="",
                    count=-1, dividend_type="none", field_list=None):
    """K 线（只读）。补历史数据的主力接口。"""
    return call("get_market_data", {
        "stock_list": stock_list, "period": period,
        "start_time": start_time, "end_time": end_time,
        "count": count, "dividend_type": dividend_type,
        "field_list": field_list or []})


def get_market_snapshot(stock_list):
    """实时快照（只读）。"""
    return call("get_market_snapshot", {"stock_list": stock_list})


def get_trading_dates(start_time, end_time):
    """交易日序列（只读）。"""
    return call("get_trading_dates", {"start_time": start_time,
                                      "end_time": end_time})


def _fmt_time(t):
    """YYYY-MM-DD / YYYYMMDD → YYYYMMDDHHMMSS（底层要求 14 位，同 tqcenter）。"""
    if not t:
        return ""
    s = str(t).replace("-", "").replace(":", "").replace(" ", "")
    if len(s) == 8:
        return s + "000000"
    return s


def get_gpjy_value(stock_list, field_list, start_time="", end_time=""):
    """股票交易**专业序列**数据（GP01~GP46）。只读。

    ★ 2026-10-21 关键发现：底层 DLL 要的是 **`table_list`** 而非 `field_list`，
      且需 `id`（run_id）+ `type`（3=股票序列）+ **14 位时间格式**。
      `tqcenter.py` 内部做了这层转换，但 HTTP 通路是**原样透传** ⇒ 直接用
      `field_list` 会报 `json has no table_list`。本函数补上这层映射。

    字段价值（对本项目）：
      GP14 涨停金额(板上成交)+开板次数 · GP15 涨跌停状态+封单金额 ·
      GP22 封成比+封流比 · GP24 首次涨停时间+最大封单额 ·
      GP38 近1年涨停次数+溢价5%次数 · GP39 近1年首板封板率+次日红盘率 ·
      GP40 近1年连板率+最后涨停时间 ·
      GP02/08/09/42 龙虎榜（总/机构卖方/机构买方/专业机构净额）·
      GP10 机构调研 · GP27 人气排名 · GP36 竞价涨停买
    """
    tbl = field_list if isinstance(field_list, list) else [field_list]
    return call("get_gpjy_value", {
        "id": 0,
        "type": 3,
        "stock_list": stock_list,
        "table_list": tbl,
        "start_time": _fmt_time(start_time),
        "end_time": _fmt_time(end_time) or _fmt_time(_now14()),
    }, timeout=180)


def get_gp_one_data(stock_list, field_list):
    """**截面一次**性专业数据（无时间轴）。只读。

    ⚠️ **字段体系是 GO 不是 GP**（2026-10-21 实测纠正）：本接口返回 `GO01~GO4x`，
    是**一致预期财务 + 机构持股 + 业绩预告**，与 `get_gpjy_value` 的 `GP01~GP46`
    （交易类：涨停/龙虎榜/融资/大宗）**完全两套**。传 GP 也能返回但值是 0（错配）。

    ★ 底层要 `type=9`（gpjy_value 是 3），且**不要** start/end_time。

    GO 字段价值（实测 600519 有 33/46 非零）：
      GO03 一致预期目标价 · GO05-07 EPS(T/T+1/T+2) · GO08-10 净利润 ·
      GO20-22 ROE · GO23-25 PE · GO29 持股机构家数 · GO30 机构持股量 ·
      GO33 总股本 · GO36/37 业绩预告净利润上下限 · GO38/39 预增降幅%
    """
    tbl = field_list if isinstance(field_list, list) else [field_list]
    return call("get_gp_one_data", {
        "id": 0, "type": 9, "stock_list": stock_list, "table_list": tbl,
    }, timeout=180)


def get_financial_data(stock_list, field_list, start_time="", end_time=""):
    """专业财务**序列**（FN 字段，按报告期）。只读。

    ⚠️ 又是另一套字段（FN 开头），实现前必须先查 SKILL.md 的 FN 定义，
    **禁凭 GP/GO 推测**（2026-10-21 踩过：三套字段体系 GP/GO/FN 极易混）。
    """
    tbl = field_list if isinstance(field_list, list) else [field_list]
    return call("get_financial_data", {
        "id": 0, "type": 1, "stock_list": stock_list, "table_list": tbl,
        "start_time": _fmt_time(start_time), "end_time": _fmt_time(end_time),
    }, timeout=180)


def _now14():
    import datetime
    return datetime.datetime.now().strftime("%Y%m%d%H%M%S")


def _selftest():
    """自检（零网络、零通达信依赖）：白名单/黑名单/参数校验/状态判定。"""
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

    # A 黑名单优先于白名单
    for m in ("order_stock", "cancel_order_stock", "query_stock_positions",
              "send_user_block", "exec_to_tdx"):
        try:
            call(m, {})
            T("A 禁调 %s（应抛错但没抛）" % m, False)
        except TqError as e:
            T("A 禁调 %s" % m, "METHOD_FORBIDDEN" in str(e))
        except Exception as e:                                  # noqa: BLE001
            T("A 禁调 %s（异常类型不对：%s）" % (m, type(e).__name__), False)

    # B 白名单外拒绝
    try:
        call("some_unknown_method", {})
        T("B 白名单外拒调", False)
    except TqError as e:
        T("B 白名单外拒调", "METHOD_NOT_WHITELISTED" in str(e))

    # C 白名单内但服务未起 ⇒ SERVICE_DOWN（不是白名单错）
    if not port_open():
        try:
            call("get_trading_dates", {"start_time": "20260101",
                                       "end_time": "20260131"})
            T("C 服务未起报 SERVICE_DOWN", False)
        except TqError as e:
            T("C 服务未起报 SERVICE_DOWN", "SERVICE_DOWN" in str(e), str(e)[:80])
    else:
        T("C 服务未起报 SERVICE_DOWN（本次服务在跑，跳过）", True)

    # D 白名单/黑名单无交集
    T("D 白名单与黑名单零交集",
      not (set(READ_ONLY_METHODS) & set(FORBIDDEN_METHODS)))
    T("D2 白名单非空且含关键接口",
      all(m in READ_ONLY_METHODS for m in
          ("get_market_data", "get_market_snapshot", "formula_xg",
           "get_financial_data", "get_trading_dates")))

    # E 状态探测结构
    st = check_status()
    T("E1 状态含必需键", all(k in st for k in
        ("tdx_installed", "service_running", "usable", "reason")))
    T("E2 不可用时必给原因", st["usable"] or bool(st["reason"]))
    T("E3 usable 与 (安装 and 运行) 一致",
      st["usable"] == (st["tdx_installed"] and st["service_running"]))

    # F 装机路径检查反映真实
    T("F1 通达信安装判定与文件存在性一致",
      st["tdx_installed"] == os.path.isfile(
          os.path.join(DEFAULT_TDX_DIR, "Tdxw.exe")))

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：%s" % "；".join(fails))
    return 0 if fail == 0 else 1


def probe_dll(tdx_dir=None, timeout=120):
    """实测 tqcenter DLL 直连通路（B 路）。→ dict。

    **必须用装了 numpy/pandas 的解释器跑**（本机 = 隔离 venv），主解释器没有。
    调用前会先起子进程隔离——tqcenter 会注册 atexit、可能挂住主进程。
    """
    import subprocess
    d = tdx_dir or DEFAULT_TDX_DIR
    py = _dll_python()
    if not py:
        return {"ok": False,
                "reason": "找不到装了 numpy/pandas 的解释器（试过隔离 venv）",
                "path_a_http": port_open(), "path_b_dll": False}
    code = ("import sys, json\n"
            "sys.path.insert(0, r'%s\\PYPlugins\\user')\n"
            "out = {'stages': []}\n"
            "try:\n"
            "    from tqcenter import tq\n"
            "    out['stages'].append('import_ok')\n"
            "    tq.initialize(r'%s')\n"
            "    out['stages'].append('init_ok')\n"
            "    r = tq.get_trading_dates('20260105', '20260109')\n"
            "    out['stages'].append('call_ok')\n"
            "    out['sample'] = str(r)[:200]\n"
            "    tq.close()\n"
            "    out['ok'] = True\n"
            "except Exception as e:\n"
            "    out['ok'] = False\n"
            "    out['error'] = '%%s: %%s' %% (type(e).__name__, str(e)[:300])\n"
            "print('__TQ__' + json.dumps(out, ensure_ascii=False))\n"
            % (d.replace("\\", "\\\\"), d.replace("\\", "\\\\")))
    try:
        p = subprocess.run([py, "-c", code], capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "子进程超时（%ss）——TQ 服务可能挂住" % timeout,
                "path_a_http": port_open(), "path_b_dll": False}
    line = ""
    for ln in (p.stdout or "").splitlines():
        if ln.startswith("__TQ__"):
            line = ln[6:]
            break
    if not line:
        return {"ok": False,
                "reason": "子进程无有效输出（stderr 前 200 字：%s）"
                          % (p.stderr or "")[:200],
                "path_a_http": port_open(), "path_b_dll": False}
    import json as _j
    r = _j.loads(line)
    r["path_a_http"] = port_open()
    r["path_b_dll"] = bool(r.get("ok"))
    if not r.get("ok"):
        r["reason"] = ("DLL 直连失败：%s ⇒ 通达信客户端 TQ 量化服务未启用"
                       "（软件在跑但服务端未就绪；需在客户端 GUI 里手动开启）"
                       % r.get("error", "?"))
    return r


def _dll_python():
    """找装了 numpy 的解释器（优先隔离 venv）。"""
    import subprocess
    cands = [
        sys.executable,
        sys.executable,
    ]
    for py in cands:
        if not os.path.isfile(py):
            continue
        try:
            p = subprocess.run([py, "-c", "import numpy, pandas"],
                               capture_output=True, timeout=30)
            if p.returncode == 0:
                return py
        except (subprocess.TimeoutExpired, OSError):
            continue
    return None


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    if "--methods" in argv:
        print("只读白名单（%d 个）：" % len(READ_ONLY_METHODS))
        for m in READ_ONLY_METHODS:
            print("  ✓ %s" % m)
        print("\n明令禁止（%d 个）：" % len(FORBIDDEN_METHODS))
        for m in FORBIDDEN_METHODS:
            print("  ✗ %s" % m)
        return 0
    if "--probe-dll" in argv:
        r = probe_dll()
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0 if r.get("ok") else 2
    if "--check" in argv:
        st = check_status()
        st["dll_python"] = _dll_python()
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return 0 if st["usable"] else 2
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
