#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""premarket.py · E1 盘前预案（能力落地矩阵 E1 · registry id=E1 · 2026-10-13 建）

用途：每个交易日**盘前**出一份 `e1_<as_of>.md` 盘前预案（registry：mounted_on=C1，
      consumes=[环境档, 候选池, 持仓] · produces=[L0 决策摘要：定调/持仓动作/候选/假设]），
      给人**一眼看过就开工**。本件 = 装配 + 呈现，**零决策**（boundary.no_decision=true ·
      can_order=false）：不产仓位计划、不批 BUY/WAIT/NO——动作一律走 C4（三问）→ C5（闸）。

★ 一、输入（全部只读，缺就缺、如实 UNAVAILABLE，禁编造、禁以 0 冒充缺失）
  C1 环境档   ← `memory/state/brain_state.md`「环境档」段 —— 唯一读回实现 =
                `brief.read_env()`（本件**不重写第二套解析**；段落文本的呈现层提取
                复用 brief 的 `_ENV_SEC`/`_MD_ROW` 同一语法）
  C2 候选池   ← `candidates.list_candidates`（OPEN 三态；expiry < as_of 归「已过期」）
  C5 持仓     ← `position_gate.derive_holdings(sim=False)`（LIVE EXECUTION 视图；
                账本缺席 → {}，如实标「无 EXECUTION 记录」，不冒充确认空仓以外的含义）
  风险参数    ← `params.get_value("regime_ladder")`（各档 position_cap，展示用）
  游标        ← `data/state/cursor.json`（只读引用）
  交易日历    ← `dayclass.Calendar.from_seed_csv(data/raw/meta/trade_cal.csv)`
                （判 as_of 是否交易日 + 上一会话；日历缺席/覆盖外 → UNAVAILABLE 标注）
  声明缺口    ← `scan.known_gap_days()`（声明件 `_unresolved.decl.json` 并集；唯一实现复用）

★ 二、阻断语义（fail-closed，但「阻断」≠「不产文件」）
  阻断 = 结论区状态置 **BLOCKED** 并逐条列原因（报告仍落盘，留可追溯凭证）；
  CLI 退出码：0 = 报告已产出（READY 或 BLOCKED）；3 = 报告本身无法产出。
  阻断判据（缺一即 BLOCKED）：
    ① 环境档缺失（brain_state 无有效环境档段 / 档位 UNAVAILABLE）
    ② 仓位天花板缺失（cap 不可判 ⇒ C5 无依据，风险许可不可表述）
    ③ as_of 非交易日（日历判）或交易日历不可读
    ④ 无任何收盘快照（数据截至 UNAVAILABLE ⇒ 预案无数据基座）
  显式标注（不阻断，但必须可见）：声明缺陷日、环境档基准落后上一会话、
  游标不可读、候选池 EMPTY / 候选过期、数据截至落后上一会话。

★ 三、幂等（同输入 ⇒ 同字节）
  内容**零墙钟**（不含 generated_at）：全部字段由输入派生、字典序固定、
  浮点格式固定。`emit` 先比对既有字节，相同 → **跳过写**（返回 changed=False），
  不同 → 原子替换（同目录 tmp + os.replace，hboard._write 同款）。

★ 四、写点（全模块唯一）
  `data/generated/reports/e1_<as_of>.md` —— registry E1 `output_path` 原样。
  落点分类 = `landing.classify("data/generated/reports/…")` → **EXEMPT/X1**
  （E 系列交付物，给人看；故无需登记 WRITE_ENTRYPOINTS，与 e11 勘察结论一致）。
  测试一律走 `--out <沙盒>` 隔离；生产目录零接触（自检 G 组机械断言）。

用法：
  python tools/decision/premarket.py --eval --as-of 2026-09-30   # 盘前真跑（写生产报告）
  python tools/decision/premarket.py --selftest
  python tools/decision/premarket.py --discipline
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "..", ".."))
for _p in ("fs", "analysis", "decision", "risk", "discovery"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）

import loader as LD            # noqa: E402
import candidates as CAND      # noqa: E402
import dayclass as DC          # noqa: E402
import params as PM            # noqa: E402
import position_gate as PG     # noqa: E402
import landing as LND          # noqa: E402
import brief as BR             # noqa: E402   （C1 环境档唯一读回实现：read_env）
import scan as SC              # noqa: E402   （声明件读取唯一实现：known_gap_days）

SCHEMA_VERSION = 1
ROOT = _ROOT
SOURCE_LABEL = "premarket_v1"

CAL_PATH = os.path.join(_ROOT, "data", "raw", "meta", "trade_cal.csv")
CURSOR_PATH = os.path.join(_ROOT, "data", "state", "cursor.json")
REPORTS_DIR = os.path.join(_ROOT, "data", "generated", "reports")
_REPORT_REL_PREFIX = "data/generated/reports/e1_"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PremarketError(Exception):
    pass


# ── 只读取数 ─────────────────────────────────────────────────────────────────
def read_cursor(path=None):
    """游标只读引用；缺席/损坏 → None（如实：不可读 ≠ 不存在）。"""
    p = path or CURSOR_PATH
    if not os.path.isfile(p):
        return None
    try:
        o = json.load(io.open(p, encoding="utf-8"))
        return o if isinstance(o, dict) else None
    except Exception:
        return None


def data_upto(base=None):
    """收盘快照最大日期 = 数据截至；一个都没有 → None。"""
    try:
        sroot = LD.roots(base)["snapshot"]
    except Exception:
        return None
    if not os.path.isdir(sroot):
        return None
    ds = [d for d in os.listdir(sroot) if _DATE_RE.match(d)]
    return max(ds) if ds else None


def read_env_full(brain_path=None):
    """C1 环境档读回（brief.read_env）+ 呈现层补「基准日期」（同段同语法提取）。

    返回 (env, env_basis)；env_basis = 环境档自身的 as_of / source 原文
    （仅呈现；语义判定一律以 env 为准）。缺档 → env.source == "UNAVAILABLE"。
    """
    env = BR.read_env(brain_path)
    basis = None
    p = brain_path or BR.BRAIN_PATH
    if os.path.isfile(p):
        try:
            text = io.open(p, encoding="utf-8").read()
            m = BR._ENV_SEC.search(text)
            if m:
                rows = dict(BR._MD_ROW.findall(m.group(1)))
                basis = (rows.get("as_of / source") or "").strip() or None
        except Exception:
            basis = None
    return env, basis


def load_calendar(cal_path=None):
    """交易日历只读；缺席/损坏 → None（UNAVAILABLE，不猜）。"""
    p = cal_path or CAL_PATH
    if not os.path.isfile(p):
        return None
    try:
        return DC.Calendar.from_seed_csv(p)
    except Exception:
        return None


def read_pool(pool_path=None):
    """候选池 OPEN 三态只读。池缺席 → UNAVAILABLE 哨兵；在但空 → []。"""
    p = pool_path or CAND.POOL_PATH if hasattr(CAND, "POOL_PATH") \
        else (pool_path or os.path.join(_ROOT, "data", "state", "candidates.json"))
    if not os.path.isfile(p):
        return "UNAVAILABLE", p
    try:
        rows = CAND.list_candidates(path=p)
    except Exception:
        return "UNAVAILABLE", p
    return rows, p


def read_holdings(base=None):
    """C5 在仓视图（LIVE EXECUTION）。账本缺席 → ({}, "LEDGER_ABSENT")。"""
    try:
        h = PG.derive_holdings(sim=False, base=base)
        return dict(h), "OK"
    except Exception as e:
        return {}, "LEDGER_ABSENT:%s" % type(e).__name__


# ── 装配 ─────────────────────────────────────────────────────────────────────
def collect(as_of, *, base=None, pool_path=None, brain_path=None, cal_path=None,
            cursor_path=None):
    """E1 全输入装配 → doc（含 status / blocks / notes；全部事实，零判断）。"""
    as_of = CAND._check_date(as_of)
    doc = {"schema_version": SCHEMA_VERSION, "as_of": as_of}
    blocks, notes = [], []

    # 数据截至 / 游标
    doc["data_upto"] = data_upto(base)
    cur = read_cursor(cursor_path)
    doc["cursor"] = (cur or {}).get("date") if isinstance(cur, dict) else None
    doc["cursor_ok"] = cur is not None
    if not doc["cursor_ok"]:
        notes.append("游标不可读（cursor.json 缺席或损坏）——已结账交易日未知，如实标注")
    if doc["data_upto"] is None:
        blocks.append("无任何收盘快照——预案无数据基座（data_upto=UNAVAILABLE）")

    # 交易日历
    cal = load_calendar(cal_path)
    if cal is None:
        doc["cal_kind"] = "UNAVAILABLE"
        doc["cal_basis"] = None
        doc["prev_session"] = None
        blocks.append("交易日历不可读（%s 缺席或解析失败）——无法确认 as_of 为交易日"
                      % os.path.relpath(cal_path or CAL_PATH, _ROOT).replace(os.sep, "/"))
    else:
        doc["cal_basis"] = cal.basis()
        if cal.is_session(as_of):
            doc["cal_kind"] = "SESSION"
            doc["prev_session"] = cal.prev_session(as_of)
        elif not cal.covers(as_of):
            doc["cal_kind"] = "OUT_OF_COVERAGE"
            doc["prev_session"] = None
            notes.append("as_of 超出日历覆盖（%s）——交易日归属不可判"
                         % (cal.span() or ("∅", "∅"))[0:1] + (cal.span() or ("∅",))[1:])
            blocks.append("as_of 超出交易日历覆盖——盘前预案无法定调（日历续期待处理）")
        else:
            doc["cal_kind"] = "NOT_SESSION"
            doc["prev_session"] = cal.prev_session(as_of)
            blocks.append("as_of 非交易日（日历判，依据=%s）——无盘前预案可出" % cal.basis())

    # C1 环境档
    env, env_basis = read_env_full(brain_path)
    doc["env"] = env
    doc["env_basis"] = env_basis
    if env.get("source") == "UNAVAILABLE" or not env.get("regime"):
        blocks.append("环境档缺失（brain_state 无有效环境档段 / 档位 UNAVAILABLE）"
                      "——禁止据本件开盘前定调")

    # 声明缺口（唯一实现 = scan.known_gap_days）
    gaps, decls = SC.known_gap_days(base=base)
    doc["gaps"] = {d: gaps[d] for d in sorted(gaps)}
    doc["decls"] = sorted(os.path.relpath(p, _ROOT).replace(os.sep, "/")
                          if os.path.isabs(p) else p for p in decls)
    if doc["gaps"]:
        notes.append("声明缺陷日 %s ——按既定裁定为「可扫描日 + 显式标注」，"
                     "其数据不得用于量能比较/完整性验收（详见声明件）"
                     % ", ".join(sorted(doc["gaps"])))

    # 环境档基准滞后标注（呈现层事实：env 基准日 < 上一会话）
    if doc.get("prev_session") and env_basis:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", env_basis)
        if m and m.group(1) < doc["prev_session"]:
            notes.append("环境档基准（%s）落后上一会话（%s）——盘前定调沿用最近档，"
                         "开盘后以盘中档为准" % (m.group(1), doc["prev_session"]))

    # C2 候选池
    rows, ppath = read_pool(pool_path)
    doc["pool_path"] = os.path.relpath(ppath, _ROOT).replace(os.sep, "/")
    if rows == "UNAVAILABLE":
        doc["pool"] = {"kind": "NO_DATA"}
        notes.append("候选池文件缺席或不可读（%s）——候选段如实 NO_DATA" % doc["pool_path"])
    else:
        opened = [r for r in rows if r.get("state") in CAND.OPEN_STATES]
        expired = sorted((r for r in opened
                          if r.get("expiry") and r["expiry"] < as_of),
                         key=lambda r: (r.get("expiry") or "", r.get("id") or ""))
        active = sorted((r for r in opened if r not in expired),
                        key=lambda r: (r.get("expiry") or "9999", r.get("id") or ""))
        if not opened:
            doc["pool"] = {"kind": "EMPTY"}
            notes.append("候选池为空（EMPTY：池文件在、无 OPEN 态候选）——非数据缺失")
        else:
            doc["pool"] = {"kind": "OK", "active": active, "expired": expired,
                           "counts": {s: sum(1 for r in opened if r["state"] == s)
                                      for s in CAND.OPEN_STATES}}
        if expired:
            notes.append("OPEN 态中 %d 只 expiry < as_of（已过期，仅列示，"
                         "推进一律由 advance_candidate 机械裁定）" % len(expired))

    # C5 风险约束
    cap = env.get("position_cap")
    if cap is None:
        blocks.append("仓位天花板缺失（环境档无 cap / 不可解析）——风险许可不可判"
                      "（C5 无 cap 依据，禁表述任何加仓空间）")
    holdings, hsrc = read_holdings(base)
    doc["holdings"] = holdings
    doc["holdings_src"] = hsrc
    held = round(sum(holdings.values()), 4)
    doc["held_sum"] = held
    doc["headroom"] = (round(cap - held, 4) if isinstance(cap, (int, float)) else None)

    ladder = PM.get_value("regime_ladder")
    doc["ladder"] = ladder if isinstance(ladder, dict) else None
    if doc["ladder"] is None:
        notes.append("params.regime_ladder 不可读——各档 cap 对照表缺，如实标注")

    doc["notes"] = notes
    doc["blocks"] = blocks
    doc["status"] = "READY" if not blocks else "BLOCKED"
    return doc


# ── 渲染（确定性：同输入同字节） ─────────────────────────────────────────────
def _na(v):
    return "UNAVAILABLE" if v is None else v


def _sig_rows(signals):
    s = signals or {}
    fr = s.get("fail_rate")
    tv = s.get("turnover")
    ud = s.get("up_down_ratio")
    ma = s.get("index_ma20")
    return [("| 涨停家数 | %s |" % _na(s.get("limit_up_count"))),
            ("| 连板高度 | %s |" % _na(s.get("max_board"))),
            ("| 炸板率 | %.1f%% |" % (fr * 100) if fr is not None
             else "| 炸板率 | UNAVAILABLE |"),
            ("| 成交额（只记录） | %.2f 万亿 |" % (tv / 1e12) if tv is not None
             else "| 成交额（只记录） | UNAVAILABLE |"),
            ("| 涨跌家数比（只记录） | %s |" % _na(ud)),
            ("| 指数20日线（只记录） | %s |" % _na(ma))]


def render(doc):
    L = []
    L.append("# E1 盘前预案（as_of=%s）" % doc["as_of"])
    L.append("")
    L.append("> 本件 = 装配与呈现（registry E1 · boundary.no_decision=true · "
             "can_order=false）。**零决策、零 BUY 结论**：动作一律走 C4（三问）→ "
             "C5（闸）。缺失数据一律 UNAVAILABLE，禁以 0 冒充。")
    L.append("")
    L.append("- 状态：**%s**" % doc["status"])
    L.append("- 数据截至（收盘快照最大日）：%s" % _na(doc["data_upto"]))
    L.append("- 游标 C（最后已结账交易日）：%s（**迁移遗留值**；新管线扫描进度以"
             " mainline.processed_upto 为准，两者非同一进度真源）" % _na(doc["cursor"]))
    L.append("- 交易日历：%s（依据=%s）"
             % (doc["cal_kind"], _na(doc.get("cal_basis"))))
    if doc.get("prev_session"):
        L.append("- 上一会话：%s" % doc["prev_session"])
    L.append("")

    L.append("## 环境档（← C1 · brain_state）")
    L.append("")
    env = doc["env"]
    L.append("| 项 | 值 |")
    L.append("| --- | --- |")
    L.append("| 档位 | %s |" % (env.get("regime") or "UNAVAILABLE"))
    L.append("| 仓位天花板（成） | %s |"
             % (env.get("position_cap")
                if env.get("position_cap") is not None else "UNAVAILABLE"))
    L.extend(_sig_rows(env.get("signals")))
    L.append("| as_of / source | %s |" % (doc.get("env_basis") or "UNAVAILABLE"))
    L.append("")

    L.append("## 候选池（← C2 · OPEN 三态）")
    L.append("")
    pool = doc["pool"]
    if pool["kind"] == "NO_DATA":
        L.append("候选池：**NO_DATA**（%s 缺席或不可读）" % doc["pool_path"])
    elif pool["kind"] == "EMPTY":
        L.append("候选池：**EMPTY**（池文件在、无 OPEN 态候选——非数据缺失）")
    else:
        cnt = pool["counts"]
        L.append("OPEN 态计数：discovered=%d · watch=%d · qualified=%d；"
                 "未过期 %d 只、已过期 %d 只"
                 % (cnt.get("discovered", 0), cnt.get("watch", 0),
                    cnt.get("qualified", 0), len(pool["active"]),
                    len(pool["expired"])))
        L.append("")
        L.append("| id | code | state | expiry | source | created_at |")
        L.append("| --- | --- | --- | --- | --- | --- |")
        for r in pool["active"] + pool["expired"]:
            L.append("| %s | %s | %s | %s | %s | %s |"
                     % (r.get("id") or "—", r.get("code") or "—",
                        r.get("state") or "—", r.get("expiry") or "—",
                        r.get("source") or "—", r.get("created_at") or "—"))
    L.append("")

    L.append("## 风险约束（← C5 + params）")
    L.append("")
    cap = env.get("position_cap")
    L.append("- 仓位天花板（成）：%s"
             % (cap if cap is not None else "**UNAVAILABLE**（风险许可不可判）"))
    if doc["holdings"]:
        L.append("- 在仓（LIVE EXECUTION 视图）：%s（合计 %.4f 成）"
                 % (", ".join("%s=%.4f" % (c, v)
                              for c, v in sorted(doc["holdings"].items())),
                    doc["held_sum"]))
    else:
        L.append("- 在仓（LIVE EXECUTION 视图）：无 EXECUTION 记录（%s）"
                 % ("账本读取异常" if doc["holdings_src"] != "OK" else "账本在、零记录"))
    L.append("- 天花板余量：%s（**信息量，非许可**——余量 > 0 不得解读为可加仓）"
             % (("%.4f 成" % doc["headroom"])
                if doc["headroom"] is not None else "UNAVAILABLE"))
    if doc["ladder"]:
        L.append("- regime_ladder 各档 cap（params 原文对照）：%s"
                 % json.dumps({k: v.get("position_cap")
                               for k, v in sorted(doc["ladder"].items())
                               if isinstance(v, dict)},
                              ensure_ascii=False, sort_keys=True))
    L.append("- 硬边界：本件不产仓位计划、不批 BUY/WAIT/NO；任何动作须走 "
             "C4（三问机械分流）→ C5（position_gate 硬闸）。")
    L.append("")

    L.append("## 声明缺口与数据状态")
    L.append("")
    if doc["gaps"]:
        L.append("声明缺陷日（`known_gap_days` 并集 · 唯一读取实现=scan.known_gap_days）：")
        L.append("")
        for d in sorted(doc["gaps"]):
            L.append("- **%s**：%s" % (d, doc["gaps"][d]))
        if doc["decls"]:
            L.append("")
            L.append("声明件：%s" % (" · ".join(doc["decls"])))
    else:
        L.append("声明缺陷日：无（全库声明件 `known_gap_days` 为空）")
    for n in doc["notes"]:
        L.append("- 注：%s" % n)
    L.append("")

    L.append("## 结论")
    L.append("")
    if doc["status"] == "READY":
        n_act = len(doc["pool"].get("active") or []) \
            if doc["pool"].get("kind") == "OK" else 0
        L.append("**PREMARKET_READY**：盘前定调基座齐备——环境档（%s）+ 候选 %d 只"
                 "（未过期）+ 风险约束可判（cap=%s）。"
                 % (env.get("regime"), n_act,
                    cap if cap is not None else "UNAVAILABLE"))
        L.append("本件不含任何 BUY 结论；开盘后以盘中档与 C4/C5 流程为准。")
    else:
        L.append("**PREMARKET_BLOCKED**（%d 项）："
                 % len(doc["blocks"]))
        for i, b in enumerate(doc["blocks"], 1):
            L.append("%d. %s" % (i, b))
        L.append("阻断期间：本件不得作为开盘前定调依据；缺失项补齐后重跑即幂等更新。")
    L.append("")
    return "\n".join(L)


# ── 写点（全模块唯一 sink；X1 豁免区） ──────────────────────────────────────
def _atomic_write(p, text):
    """原子写（同目录 tmp + os.replace；hboard._write 同款）。"""
    d = os.path.dirname(p)
    _WG.check_write(p)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".e1_", suffix=".tmp")
    try:
        with io.open(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def emit(as_of, *, base=None, out_dir=None, pool_path=None, brain_path=None,
         cal_path=None, cursor_path=None):
    """装配 + 渲染 + 幂等写。返回 {path, changed, status, blocks}。"""
    doc = collect(as_of, base=base, pool_path=pool_path, brain_path=brain_path,
                  cal_path=cal_path, cursor_path=cursor_path)
    text = render(doc)
    d = out_dir or REPORTS_DIR
    os.makedirs(d, exist_ok=True)          # ★ 干净机器上 data/generated 不进库，输出目录须自建
    p = os.path.join(d, "e1_%s.md" % doc["as_of"])
    changed = True
    if os.path.isfile(p):
        try:
            if io.open(p, "rb").read() == text.encode("utf-8"):
                changed = False
        except Exception:
            changed = True
    if changed:
        _atomic_write(p, text)
    return {"path": p, "changed": changed, "status": doc["status"],
            "blocks": doc["blocks"], "doc": doc}


# ── CLI ──────────────────────────────────────────────────────────────────────
def _main(argv=None):
    ap = argparse.ArgumentParser(description="E1 盘前预案（零决策预案生成器）")
    ap.add_argument("--eval", action="store_true", help="真跑：产出 e1_<as_of>.md")
    ap.add_argument("--as-of", dest="as_of", help="盘前交易日（YYYY-MM-DD）")
    ap.add_argument("--out", help="报告目录覆盖（测试沙盒用；默认生产 data/generated/reports/）")
    ap.add_argument("--pool", help="候选池路径覆盖（沙盒）")
    ap.add_argument("--brain", help="brain_state 路径覆盖（沙盒）")
    ap.add_argument("--calendar", help="交易日历路径覆盖（沙盒）")
    ap.add_argument("--cursor", help="游标路径覆盖（沙盒）")
    ap.add_argument("--base", help="数据根覆盖（沙盒；空 = 生产）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--discipline", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return 0 if _selftest() else 1
    if a.discipline:
        return 0 if _discipline() else 1
    if not a.eval or not a.as_of:
        print("用法：--eval --as-of <YYYY-MM-DD>（或 --selftest / --discipline）")
        return 2
    # 运转留痕（E 系列留痕欠账收口 · 2026-10-14）：run_log 经 observe 唯一写点追加。
    # log_path 跟随 base 重定向（与 scan._log_path_for 同纪律）——沙盒跑落沙盒，生产跑落生产。
    import observe as _OBS
    if a.base and not os.path.isdir(a.base):
        print("--base 必须是已存在目录（留痕 log_path 与数据根同源），实际: %s" % a.base)
        return 2
    _logp = (os.path.join(a.base, "data", "state", "run_log.jsonl")
             if a.base else None)
    _obs = _OBS.run_observed(
        "ext:e1", "e1_premarket", "PRE",
        lambda: emit(a.as_of, base=a.base, out_dir=a.out, pool_path=a.pool,
                     brain_path=a.brain, cal_path=a.calendar, cursor_path=a.cursor),
        as_of=a.as_of, detail="E1 盘前预案（ext 外延 · 人触发盘前入口）",
        raise_on_error=False, log_path=_logp)
    if _obs["error"]:
        print("失败: %s" % _obs["error"])
        return 1
    r = _obs["value"]
    print("状态: %s" % r["status"])
    print("产出: %s (%s)" % (r["path"], "已更新" if r["changed"] else "字节相同·跳过写（幂等）"))
    for b in r["blocks"]:
        print("阻断: %s" % b)
    return 0


# ── 自检（全沙盒；生产零副作用机械断言） ────────────────────────────────────
def _md5(p):
    if not os.path.exists(p):
        return None
    return hashlib.md5(io.open(p, "rb").read()).hexdigest()


# 生产零副作用基线（import 期捕获）
_PROD_FILES = ("data/state/candidates.json", "data/state/mainline.json",
               "data/state/cursor.json", "memory/state/brain_state.md")
_PROD_BASELINE = tuple((f, _md5(os.path.join(_ROOT, f))) for f in _PROD_FILES)
def _reports_snapshot():
    """报告目录快照；目录不存在（干净机器 data/generated 不进库）返回 ()。"""
    return tuple(sorted(os.listdir(REPORTS_DIR))) if os.path.isdir(REPORTS_DIR) else ()


_PROD_REPORTS = _reports_snapshot()

_TEST_FUNCS = ("_selftest", "_discipline", "_sb_brain", "_sb_cal", "_sb_decl",
               "_sb_pool")


def _sb_brain(dirpath, grade="强势", cap="5", with_section=True, name="brain_state.md"):
    """沙盒 brain_state.md（brief 自检同款表格；可去段/去 cap 造缺失分支）。
    ★ name 须逐变体唯一——同目录互覆盖会让后续用例读到错误变体（G5 教训）。"""
    L = ["# 当前状态", ""]
    if with_section:
        L += ["## 环境档（← C1 产出）", "",
              "| 项 | 值 |", "| --- | --- |",
              "| 档位 | **%s** |" % grade]
        if cap is not None:
            L.append("| 仓位天花板（成） | %s |" % cap)
        L += ["| as_of / source | 2026-09-29 / snapshot |",
              "| 涨停家数 | 57 |", "| 连板高度 | 6 |", "| 炸板率 | 21.1% |",
              "| 成交额（只记录） | 1.40 万亿 |",
              "| 涨跌家数比（只记录） | 1.80 |",
              "| 指数20日线（只记录） | 3912.8 |", "", "## 板块腿", "",
              "| 板块 | 阶段 | 依据 |", "| --- | --- | --- |",
              "| — | `UNAVAILABLE` | — |", ""]
    p = os.path.join(dirpath, name)
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L))
    return p


def _sb_cal(dirpath, start="2026-09-14", end="2026-10-13"):
    """沙盒 trade_cal.csv：工作日 is_trade=1，周末 0。"""
    import datetime
    p = os.path.join(dirpath, "trade_cal.csv")
    d0 = datetime.date.fromisoformat(start)
    d1 = datetime.date.fromisoformat(end)
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("date,is_trade\n")
        d = d0
        while d <= d1:
            f.write("%s,%d\n" % (d.isoformat(), 0 if d.weekday() >= 5 else 1))
            d += datetime.timedelta(days=1)
    return p


def _sb_decl(base, date, note):
    """沙盒声明件（scan 声明感知的唯一读取形态）。"""
    d = os.path.join(base, "data", "raw", date)
    os.makedirs(d, exist_ok=True)
    with io.open(os.path.join(d, "_unresolved.decl.json"), "w",
                 encoding="utf-8", newline="\n") as f:
        json.dump({"known_gap_days": {date: note}}, f, ensure_ascii=False)
    return os.path.join(d, "_unresolved.decl.json")


def _sb_pool(base, cal_path, dates=("2026-09-28", "2026-09-29")):
    """沙盒候选池：先按 0.5 骨架初始化，再经 CAND.upsert_candidate 真实入池
    （expiry 由 compute_expiry 算；不得静默新建 → 先显式写骨架）。"""
    p = os.path.join(base, "pool.json")
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": CAND.SCHEMA_VERSION, "updated_at": dates[0],
                   "candidates": []}, f)
    for i, (d, code) in enumerate(zip(dates, ("sz000001", "sh600000"))):
        CAND.upsert_candidate(code, d, "mainline", "主线|%s 触发" % code,
                              path=p, calendar_path=cal_path)
    return p


def _selftest():
    fails = [0]

    def T(name, cond, extra=""):
        if cond:
            print("  [OK] %s" % name)
        else:
            fails[0] += 1
            print("  [FAIL] %s %s" % (name, extra))

    sb = tempfile.mkdtemp(prefix="e1_sb_")
    try:
        cal = _sb_cal(sb)
        brain = _sb_brain(sb)
        pool = _sb_pool(sb, cal)
        decl = _sb_decl(sb, "2026-09-23", "沙盒合成缺陷日（自检用，非生产）")
        snap = os.path.join(sb, "data", "snapshot", "2026-09-29")
        os.makedirs(snap, exist_ok=True)
        out = os.path.join(sb, "reports")
        kw = dict(base=sb, out_dir=out, pool_path=pool, brain_path=brain,
                  cal_path=cal, cursor_path=os.path.join(sb, "cursor.json"))

        # ── G1 正常产出 ──
        r1 = emit("2026-09-30", **kw)
        t1 = io.open(r1["path"], encoding="utf-8").read()
        T("G1 正常产出：状态 READY 且含档位/候选/风控/零决策声明",
          r1["status"] == "READY" and "强势" in t1 and "sh600000" in t1
          and "零决策" in t1 and "PREMARKET_READY" in t1, "status=%s" % r1["status"])
        T("G1b 信号非伪造：六信号逐一呈现（非 0 冒充）",
          "| 涨停家数 | 57 |" in t1 and "| 炸板率 | 21.1% |" in t1
          and "| 指数20日线（只记录） | 3912.8 |" in t1)
        T("G1c 声明缺口显式标注：缺陷日 + 声明件相对路径可追溯",
          "2026-09-23" in t1 and "沙盒合成缺陷日" in t1
          and "_unresolved.decl.json" in t1)
        T("G1d 落点形态：e1_<as_of>.md 且 landing 归类 EXEMPT/X1",
          os.path.basename(r1["path"]) == "e1_2026-09-30.md"
          and (lambda c: c["kind"] == "EXEMPT" and "X1" in c["exemptions"])(
              LND.classify("data/generated/reports/e1_2026-09-30.md")))

        # ── G2 依赖缺失：环境档缺段 ──（brain 变体各自唯一名，防同路径互覆盖）
        out2 = os.path.join(sb, "out2")
        brain2 = _sb_brain(sb, with_section=False, name="brain_nosec.md")
        r2 = emit("2026-09-30", **dict(kw, out_dir=out2, brain_path=brain2))
        t2 = io.open(r2["path"], encoding="utf-8").read()
        T("G2 环境档缺失 → BLOCKED + 档位 UNAVAILABLE（不编档）",
          r2["status"] == "BLOCKED" and "环境档缺失" in "|".join(r2["blocks"])
          and "| 档位 | UNAVAILABLE |" in t2 and "PREMARKET_BLOCKED" in t2)

        # ── G3 依赖缺失：日历缺席 ──
        out3 = os.path.join(sb, "out3")
        r3 = emit("2026-09-30", **dict(kw, out_dir=out3,
                                       cal_path=os.path.join(sb, "no_cal.csv")))
        T("G3 日历缺席 → BLOCKED（UNAVAILABLE，不拿自然日冒充交易日）",
          r3["status"] == "BLOCKED"
          and any("交易日历不可读" in b for b in r3["blocks"]))

        # ── G4 风险阻断：cap 缺失 ──
        out4 = os.path.join(sb, "out4")
        brain4 = _sb_brain(sb, cap=None, name="brain_nocap.md")
        r4 = emit("2026-09-30", **dict(kw, out_dir=out4, brain_path=brain4))
        t4 = io.open(r4["path"], encoding="utf-8").read()
        T("G4 仓位天花板缺失 → BLOCKED + 风险许可不可判（cap=UNAVAILABLE）",
          r4["status"] == "BLOCKED"
          and any("仓位天花板缺失" in b for b in r4["blocks"])
          and "| 仓位天花板（成） | UNAVAILABLE |" in t4)

        # ── G5 空候选 ──
        out5 = os.path.join(sb, "out5")
        empty_pool = os.path.join(sb, "pool_empty.json")
        with io.open(empty_pool, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "updated_at": "2026-09-30", "candidates": []}, f)
        r5 = emit("2026-09-30", **dict(kw, out_dir=out5, pool_path=empty_pool))
        t5 = io.open(r5["path"], encoding="utf-8").read()
        T("G5 空候选 → EMPTY（非 NO_DATA、非 0 冒充；池文件在）",
          "候选池：**EMPTY**" in t5 and r5["status"] == "READY")

        # ── G6 幂等：重复运行字节级不变 + 二次为跳过写 ──
        r6 = emit("2026-09-30", **kw)
        T("G6 重复运行：changed=False 且字节级相同（同输入同字节）",
          r6["changed"] is False
          and io.open(r6["path"], "rb").read() == t1.encode("utf-8"))
        r6b = emit("2026-10-01", **kw)   # 同内容不同 as_of → 不同文件、互不覆盖
        T("G6b 不同 as_of → 独立文件（按日命名契约）",
          os.path.basename(r6b["path"]) == "e1_2026-10-01.md"
          and os.path.isfile(r6["path"]))

        # ── G7 非交易日阻断 ──
        out7 = os.path.join(sb, "out7")
        r7 = emit("2026-10-10", **dict(kw, out_dir=out7))   # 2026-10-10 周六（沙盒日历）
        T("G7 as_of 非交易日 → BLOCKED（日历判，不出预案）",
          r7["status"] == "BLOCKED"
          and any("非交易日" in b for b in r7["blocks"]))

        # ── G8 候选过期列示（as_of 晚于沙盒候选 expiry 2026-10-05/06）──
        out8 = os.path.join(sb, "out8")
        r8 = emit("2026-10-07", **dict(kw, out_dir=out8))
        t8 = io.open(r8["path"], encoding="utf-8").read()
        T("G8 已过期候选仅列示（expiry < as_of，推进留给 advance_candidate）",
          "已过期" in t8 and "advance_candidate" in t8)

        # ── G9 沙盒隔离：生产零副作用 ──
        T("G9 生产零副作用：台账/游标/brain_state 逐字节未变 + 报告目录无新增",
          all(_md5(os.path.join(_ROOT, f)) == h for f, h in _PROD_BASELINE)
          and _reports_snapshot() == _PROD_REPORTS)
    finally:
        shutil.rmtree(sb, ignore_errors=True)

    print("premarket 自检：%d 失败" % fails[0])
    return fails[0] == 0


def _discipline():
    ok = True

    def T(name, cond, extra=""):
        nonlocal ok
        print(("  [OK] " if cond else "  [FAIL] ") + name + (" " + extra if not cond else ""))
        ok = ok and cond

    # ① 生产路径写点唯一：非测试函数内的写模式 open 恰 1 处（_atomic_write 内）
    #   （open 与 io.open 两种调用形态都扫；写模式 = 含 w/a/x；测试助手 _sb_*/_selftest 豁免）
    import ast
    src = io.open(__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = [(f.name, f.lineno, f.end_lineno) for f in ast.walk(tree)
             if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def _enclosing(lineno):
        for name, lo, hi in funcs:
            if lo <= lineno <= hi:
                return name
        return "<module>"

    def _is_write_open(n):
        if not isinstance(n, ast.Call):
            return False
        if not ((isinstance(n.func, ast.Name) and n.func.id == "open")
                or (isinstance(n.func, ast.Attribute) and n.func.attr == "open")):
            return False
        mode = ""
        for ai in (1, 2):                      # open(p, "w", …) 与 open(p, …, "w")
            if len(n.args) > ai and isinstance(n.args[ai], ast.Constant):
                mode += str(n.args[ai].value)
        for kw in n.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode += str(kw.value.value)
        return any(c in mode for c in "wax")

    prod_writes = sorted((_enclosing(n.lineno), n.lineno)
                         for n in ast.walk(tree) if _is_write_open(n)
                         and not _enclosing(n.lineno).startswith(("_sb_", "_selftest")))
    T("① 生产路径写点唯一：非测试写模式 open 恰 1 处（_atomic_write）",
      len(prod_writes) == 1 and prod_writes[0][0] == "_atomic_write",
      "prod_writes=%s" % prod_writes)

    # ② 落点归类：e1_*.md ∈ X1 豁免（E 系列交付物，给人看）
    c = LND.classify("data/generated/reports/e1_2026-09-30.md")
    T("② 落点归类 = EXEMPT/X1（无需 WRITE_ENTRYPOINTS 登记）",
      c["kind"] == "EXEMPT" and "X1" in c["exemptions"],
      "kind=%s x=%s" % (c["kind"], c["exemptions"]))

    # ③ registry 契约一致：output_path 形态与本件 emit 命名一致
    reg = os.path.join(_ROOT, "data", "state", "capability_registry.json")
    e1 = None
    try:
        for x in (json.load(io.open(reg, encoding="utf-8"))
                  .get("capabilities") or []):
            if x.get("id") == "E1":
                e1 = x
                break
    except Exception:
        pass
    T("③ registry E1 契约一致（output_path + no_decision/can_order）",
      e1 is not None
      and e1.get("output_path") == "data/generated/reports/e1_<as_of>.md"
      and (e1.get("boundary") or {}).get("no_decision") is True
      and (e1.get("boundary") or {}).get("can_order") is False,
      "e1=%s" % e1)

    # ④ 零生产状态写：台账/游标/投放写 API 的**调用**只允许出现在测试助手里
    #   （_sb_pool 经 CAND.upsert_candidate 写沙盒池是合法夹具；生产函数 collect/emit/
    #    render/read_* 内出现任一调用即违例；纯文字提及不算——按 Call 节点判）
    forb = ("upsert_candidate", "advance_candidate", "backfill_candidate",
            "mainline_update", "stage_core", "promote_snapshot", "reseed")
    offenders = []
    for f in ast.walk(tree):
        if not isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                or f.name.startswith(("_sb_", "_selftest")):
            continue
        for n in ast.walk(f):
            if isinstance(n, ast.Call):
                nm = n.func.id if isinstance(n.func, ast.Name) \
                    else (n.func.attr if isinstance(n.func, ast.Attribute) else "")
                if nm in forb:
                    offenders.append((f.name, nm, n.lineno))
    T("④ 零生产状态写：台账/游标/投放写 API 仅测试助手调用（按 Call 节点判）",
      not offenders, "offenders=%s" % offenders)

    # ⑤ C1/C2/声明读取无第二真源：复用 brief.read_env 与 scan.known_gap_days
    T("⑤ 无第二套事实源：环境档/声明件读取分别复用 brief/scan 唯一实现",
      "BR.read_env(" in src and "SC.known_gap_days(" in src)

    return ok


if __name__ == "__main__":
    sys.exit(_main())
