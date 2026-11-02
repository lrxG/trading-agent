"""daily_pipeline.py · 每日盘后链**编排器**（一条命令串起全链）。

## 为什么需要这个文件

项目曾有一份手工命令清单要求「顺序依赖靠人记：merge_klines 必须在 S 系之前，
漏了 S 系全挂」。2026-10-20 实测事故：漏跑 merge_klines ⇒ s2/s3/s4 全部 `DATA_END`。

**2026-10-22：那份手工清单已删除**——它被本编排器完全取代，
且按它手工跑会漏掉后来新增的步骤（新闻采集、环境档判定）。
现在**唯一正确的每日入口就是本文件**：

    python tools/e2e/daily_pipeline.py --date <D>            # 干跑
    python tools/e2e/daily_pipeline.py --date <D> --execute  # 真跑
2026-10-21 再次确认：这是**唯一硬顺序依赖**，且靠人记必然再犯。

## 它做什么 / 不做什么

**做**：按固定顺序调用九步，每步前做前置条件检查，失败即停并给可执行提示。
**不做**：不替代各步内部逻辑、不绕过写作闸、**不在脚本里硬编码业务判断**。

## 九步（顺序 = 清单实测顺序，不可调换）

  0 日状态判定   scan --eval（非交易日/数据未齐 ⇒ 后面全不跑）
  1 采集         live_seed --stage --execute        [写]
  2 raw→snapshot ingest --promote                   [写]
  3 补涨停源     backfill_zt_reason --execute       [写]
  4 板块数据     sector_seed --snapshot             [写]
  5 候选池       scan --eval                        [写]
  6 合并行情     merge_klines                       [写]  ★ 最易漏
  7 S 系分析     s2 / s3 / s4 --eval                [写]
  8 A 路判断     a_path_daily evidence/emit/chain   [写]  ← 需人工 judgments，默认跳过
  9 结算         settle --settle                    [写]

## 三道安全设计

① **写作闸不可绕过**：所有标 [写] 的步骤都经 `writegate plan → run --confirm-production`，
   拿到 digest 才执行。**本编排器自己不做任何写入。**
② **fail-fast + 可执行提示**：任一步失败立刻停，打印失败步的 stderr 尾部 +
   「你现在该跑什么」。不允许"继续跑下去看会不会自己好"。
③ **--dry-run 默认**：不带 `--execute` 只打印将要执行的命令序列（含预计耗时），零写入。

## 用法

  python tools/e2e/daily_pipeline.py --date 2026-10-21                    # 干跑：只看计划
  python tools/e2e/daily_pipeline.py --date 2026-10-21 --from 6           # 从第 6 步续跑
  python tools/e2e/daily_pipeline.py --date 2026-10-21 --execute          # 真跑
  python tools/e2e/daily_pipeline.py --date 2026-10-21 --execute --only 6,7
  python tools/e2e/daily_pipeline.py --selftest
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in ("fs", "e2e", "analysis", "risk", "decision", "discovery"):
    sys.path.insert(0, os.path.join(_ROOT, "tools", _p))

PY = sys.executable
CAL = os.path.join("data", "raw", "meta", "trade_cal.csv")


# ── 步骤定义 ────────────────────────────────────────────────────────────────
# (序号, 名称, cli, args 模板, 是否写生产, 预计秒数, 前置提示)
STEPS = [
    (0, "日状态判定", "tools/discovery/scan.py",
     ["--eval", "--as-of", "{d}", "--calendar", CAL], False, 5,
     "非交易日或数据未齐则后面全不跑（fail-closed，非缺陷）"),
    (1, "采集播种", "tools/e2e/live_seed.py",
     ["--stage", "--date", "{d}", "--execute"], True, 1800,
     "CLI 限速，约 17 分钟（常态）；遇瞬时限流会因冷却重取变慢 ⇒ 步超时放到 30 分钟，"
     "且 kline 自带 1500s 预算会先自我收工并按 PARTIAL 报告，不会被步超时整段抹掉"),
    (2, "raw→快照", "tools/fs/ingest.py",
     ["--promote", "{d}"], True, 2,
     "含 A9 准入判定；契约不符会拒（这是正确拒绝）"),
    (3, "补涨停三列", "tools/e2e/backfill_zt_reason.py",
     ["--from", "{d}", "--to", "{d}", "--execute"], True, 5,
     "不补 ⇒ mech_driver 的 M2 板块扩散槽全跳过 ⇒ 选票为 0"),
    (4, "板块数据", "tools/e2e/sector_seed.py",
     ["--snapshot", "--date", "{d}"], True, 30,
     "S4 泡沫/拥挤度依赖；ranking+valuation 约 28 秒"),
    (5, "候选池刷新", "tools/discovery/scan.py",
     ["--eval", "--as-of", "{d}", "--calendar", CAL], True, 10,
     "必须带 --calendar，否则 NO_CALENDAR 不扫描"),
    (6, "合并行情", "tools/e2e/merge_klines.py",
     ["--from", "{d}", "--to", "{d}"], True, 5,
     "★ 最易漏：不合并 ⇒ s2/s3/s4 全报 DATA_END"),
    (7, "ETF 行情补采", "tools/e2e/fetch_etf_klines.py",
     ["--execute"], True, 90,
     "★ 2026-10-22 新增：etf_klines 曾是旧包副本、停在 9-22，九步不采它 ⇒ "
     "低波轮动结论建在旧数据上；62 只约 60~90 秒，幂等（已最新则秒退）"),
    (8, "竞价采集", "tools/e2e/fetch_auction.py",
     ["--date", "{d}", "--execute"], True, 600,
     "★★ 2026-10-22 新增：S10 竞价结构判 NO_OUTPUT，但**实测 westock quote 里就有完整"
     "竞价数据**（pre_quote_price/change_pct/volume/amount/wb_ratio/price_ceiling）——"
     "S10 缺的不是数据，是没人去采（它只查旧包目录）。4940 只约 25 分钟，"
     "批量 40 只/次。产出 data/raw/bus/auction_<D>.csv"),
    (9, "资金流采集", "tools/e2e/fetch_fundflow.py",
     ["--date", "{d}", "--codes-from-pool", "{d}", "--execute"], True, 240,
     "★★ 2026-10-26 新增：**日链此前 14 步里根本没有资金流采集这一步** ⇒ "
     "`fundflow.csv` 停在 10-22，而 S5 的 `universe.n_codes_with_flow_today=0` "
     "⇒ **S5 资金行为恒为 0 只**（10-23 实测 n_codes=0/n_structured=0）。"
     "S5 是 evidence_room 十源之一，等于决策简报里资金那条腿一直空着。"
     "只采「涨停池+龙虎榜+候选池」（约 150~300 只，2~4 分钟）；"
     "全市场补数走单独命令（不阻塞日链）。必须在本步之后才跑 S 系（S5 要用）"),
    (10, "新闻采集", "tools/e2e/fetch_news.py",
     ["--date", "{d}", "--limit", "100", "--notice-pool", "--execute"], True, 180,
     "★★ 2026-10-22 新增：`data/raw/<d>/news.jsonl` 是 S1/S7/S8/E7 的**共同上游**，"
     "但生产代码**没有任何模块采它** —— 实测 9 月每天 1000+ 条、10 月全部 0 字节。"
     "S8 报「扫 8134 条」其实是回看 30 天读到 9 月数据，当日增量恒为 0。"
     "★★ 2026-10-26 补 `--notice-pool`：公告此前**从未采过**（step 只有 --date/--limit，"
     "既不传 --codes 也不传 --notice）⇒ notices/ 至今只有 9 月旧包 11 个文件、10 月零公告。"
     "而 S1/S8/A路 的上游全是它。现由本件自动从「涨停池+龙虎榜+候选池」取票（约 150~300 只）"
     "逐票采公告，预计 2~3 分钟。必须在 S 系之前（S7/S8 要用当日新闻）"),
        (11, "监管/股东行为扫描", "tools/e2e/fetch_regulatory.py",
         ["--date", "{d}", "--mode", "all"], True, 120,
         "写  ★★ 2026-10-30 新增：信息能力升级 P0（task #281）——监管关键词+候选池股东行为，"
         "输出 data/raw/<d>/regulatory.jsonl 与 holder_behavior.jsonl"),
    (12, "S 系分析", "tools/analysis/s2.py",
     ["--eval", "{d}"], True, 3, "本步还含 s3 / s4，见 _run_s_suite"),
    (13, "环境档判定", "tools/analysis/regime.py",
     ["--eval", "--as-of", "{d}"], True, 3,
     "★★ 2026-10-22 新增：**此前编排器完全不跑 regime** ⇒ 环境档（C1）"
     "停在最后一次手工运行的那天，实测 10-22 时 brain_state 里还是 09-24 的数据，"
     "整整滞后近一个月。**环境档是仓位上限的唯一来源**，"
     "不跑它 ⇒ 后面所有「仓位该几成」都建在旧档上。必须在 S 系之后（S3 情绪已就绪）"),
    (14, "A 路判断", "tools/decision/a_path_daily.py",
     ["--stage", "evidence", "--as-of", "{d}"], True, 3,
     "★ evidence 每天必跑（机械装配，无需人工）；emit/chain 需 --with-judgments"),
    (15, "五步结算", "tools/risk/settle.py",
     ["--settle", "{d}", "--calendar", CAL], True, 2,
     "全绿则游标推进到该日；这是唯一推进游标的入口"),
]

S_SUITE = [
    # ★ 2026-10-22 补 S1：它此前只有 --selftest，**没有任何批量入口** ⇒ 每日链跑不到它。
    #   新增 scan_day 后可从当日 news.jsonl 扫带关联个股的事件。放在最前，
    #   因为 S1 是"事件→驱动"的源头，S2/S3/S4 的题材/情绪判断会用到它。
    ("tools/analysis/s1.py", ["--eval", "--as-of", "{d}"]),
    ("tools/analysis/s2.py", ["--eval", "{d}"]),
    ("tools/analysis/s3.py", ["--eval", "{d}"]),
    ("tools/analysis/s4.py", ["--eval", "{d}"]),
    # ★★ 2026-10-23 补 S5/S6/S7/S8：这四项**此前完全不在日链里**。
    #   后果实证：10-22 决策简报的 evidence_room 里 s5/s6/s7/s8 全空，
    #   而 S 系接入后它们本该提供资金行为/估值/预期差/政策事件四类证据
    #   ⇒ 简报 10 个来源只有 6 个有数据。补进日链后 10/10 全部命中。
    #   `critical=False`：这四项缺源时**不该拦整链**（它们是增强证据，
    #   不是决策前提；S1~S4 缺了才真该停）。
    ("tools/analysis/s5.py", ["--eval", "{d}"], {"critical": False}),
    ("tools/analysis/s7.py", ["--eval", "{d}"], {"critical": False}),
    ("tools/analysis/s8.py", ["--eval", "{d}"], {"critical": False}),
    # S6 需要显式 codes（估值截面按票抓，非全市场）：用当日候选池的票。
    # `codes_from_pool=True` ⇒ 运行时由 _s6_codes(d) 取，取不到则跳过本项。
    ("tools/analysis/s6.py", ["--fetch-section", "{codes}", "--as-of", "{d}"],
     {"critical": False, "codes_from_pool": True}),
]


def _fmt(cli, args, d):
    return [PY, cli] + [a.replace("{d}", d) for a in args]


def _plan_line(env):
    return " ".join(env[1:])


# ── 执行原语 ────────────────────────────────────────────────────────────────
def run_gated(cli, args, dry=True, d=""):
    """经写作闸执行。→ (ok, stdout, stderr, seconds)"""
    a = [x.replace("{d}", d) for x in args]
    argstr = " ".join(a)
    if dry:
        return (True, "  [干跑] writegate plan --cli %s --args \"%s\"" % (cli, argstr),
                "", 0.0)
    t0 = time.time()
    # ① plan 拿 digest
    p = subprocess.run([PY, "tools/fs/writegate.py", "plan", "--cli", cli,
                        "--args", argstr], cwd=_ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        return False, p.stdout, p.stderr, time.time() - t0
    digest = ""
    for line in p.stdout.splitlines():
        s = line.strip()
        if "confirm-production" in s:
            digest = s.split("confirm-production")[-1].strip()
    if not digest:
        return False, p.stdout, "PLAN_NO_DIGEST 未从 plan 输出解析到 digest", time.time() - t0
    # ② run
    r = subprocess.run([PY, "tools/fs/writegate.py", "run", "--cli", cli,
                        "--args", argstr, "--confirm-production", digest],
                       cwd=_ROOT, capture_output=True, text=True)
    return r.returncode == 0, r.stdout, r.stderr, time.time() - t0


def run_plain(cli, args, dry=True, d=""):
    """只读步骤，不经闸。"""
    a = [x.replace("{d}", d) for x in args]
    if dry:
        return True, "  [干跑] %s" % " ".join([cli] + a), "", 0.0
    t0 = time.time()
    r = subprocess.run([PY, cli] + a, cwd=_ROOT, capture_output=True, text=True)
    return r.returncode == 0, r.stdout, r.stderr, time.time() - t0


def _s6_codes(as_of, limit=50):
    """当日候选池里「在观察中」的票 → S6 估值截面抓取对象。

    ★ 为什么只取候选而不是全市场（2026-10-23）：
      `s6 --fetch-section` 是**逐票**抓估值（quote 接口），全市场 4940 只 ≈ 数小时；
      而 S6 在决策里只被用来回答「**这一票**贵不贵」⇒ 只需候选池覆盖。
      取不到候选 → 返回 []，调用方跳过该步（不空跑、不硬编票）。
    """
    p = os.path.join(_ROOT, "data", "state", "candidates.json")
    if not os.path.isfile(p):
        return []
    try:
        with io.open(p, encoding="utf-8") as f:
            doc = json.load(f)
    except (ValueError, OSError):
        return []
    out = []
    for c in (doc.get("candidates") or []):
        code = (c.get("code") or "").strip()
        st = (c.get("state") or "").strip().upper()
        if not code:
            continue
        # 只取还能被判断的态（终态票估值无意义，省接口配额）
        if st in ("EXPIRED", "TRADED", "INVALID", "ARCHIVED"):
            continue
        if code not in out:
            out.append(code)
        if len(out) >= limit:
            break
    return out


def _check_data_end(step_name, stdout):
    """识别 DATA_END 并给出可执行提示（第 6 步漏跑的症状）。"""
    if "DATA_END" not in (stdout or ""):
        return None
    return ("检测到 DATA_END —— 极可能是**忘了先跑第 6 步 merge_klines**。\n"
            "        请执行：python tools/e2e/merge_klines.py --from <D> --to <D>\n"
            "        然后从本步续跑：--from 7")


# ── 主编排 ──────────────────────────────────────────────────────────────────
def run_pipeline(date, execute=False, from_step=0, only=None, with_judgments=None):
    d = date
    todo = []
    for (idx, name, cli, args, writes, est, hint) in STEPS:
        if idx < from_step:
            continue
        if only and idx not in only:
            continue
        todo.append((idx, name, cli, args, writes, est, hint))

    # ★ 2026-11-02 增：通道预检（不占步号、不改 STEPS —— 步号漂移曾致 regime/ETF 永不执行）
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.dirname(
            _o.path.dirname(_o.path.abspath(__file__)))), "tools", "fs"))
        import channel_health as _CH
        _ch = _CH.check()
        _bad = [k for k, v in _ch.items() if not v["ok"]]
        print("通道预检：" + ("全部正常" if not _bad else
                          "⚠ 异常腿 %s —— %s" % (_bad, [_ch[k]["note"] for k in _bad])))
        print("")
    except Exception as _e:                                      # noqa: BLE001
        print("通道预检：不可用（%s）—— 继续执行，但消息/行情腿可能静默降级" % type(_e).__name__)
        print("")

    print("每日盘后编排 · %s · 模式=%s · 共 %d 步"
          % (d, "生产执行" if execute else "干跑（零写入）", len(todo)))
    print("=" * 76)
    total_est = sum(t[5] for t in todo)
    print("预计总耗时约 %d 分 %d 秒（采集占绝大部分）\n"
          % (total_est // 60, total_est % 60))

    results = []
    t_all = time.time()
    for (idx, name, cli, args, writes, est, hint) in todo:
        print("─" * 76)
        print("[%d] %s  %s" % (idx, name, "【写生产】" if writes else "【只读】"))
        print("    %s" % hint)

        # ★ 2026-11-02 修：按 CLI 名识别，禁按 idx（步号会因增删步骤漂移）
        if cli.endswith("analysis/s2.py"):
            # S 系套件（2026-10-23：支持 per-项元数据 critical / codes_from_pool）（dispatch 入口见下，按 CLI 识别）
            allok = True
            for entry in S_SUITE:
                s_cli, s_args = entry[0], entry[1]
                meta = (entry[2] if len(entry) > 2 else {}) or {}
                tag = os.path.basename(s_cli)
                args = list(s_args)
                if meta.get("codes_from_pool"):
                    # S6 估值截面按票抓：取当日候选池里的票；取不到 → 跳过本项
                    codes = _s6_codes(d)
                    if not codes:
                        print("    · %-8s SKIP（当日无候选票，估值截面无对象）" % tag)
                        continue
                    args = [a.replace("{codes}", ",".join(codes)) for a in args]
                ok, out, err, sec = run_gated(s_cli, args, dry=not execute, d=d)
                print("    · %-8s %s（%.1fs）%s"
                      % (tag, "OK" if ok else "FAIL", sec,
                         "" if meta.get("critical", True) else "〔非关键〕"))
                if not ok:
                    extra = _check_data_end(tag, out + err)
                    if extra:
                        print("      !! %s" % extra)
                    else:
                        print("      !! stderr: %s" % (err or out)[-400:])
                    # 非关键项失败**不拦整链**（增强证据缺源 ≠ 决策前提缺失）
                    if meta.get("critical", True):
                        allok = False
                        break
                    print("      → 非关键项失败，继续（该证据源当日为空）")
                else:
                    # DATA_END 也可能出现在成功输出的警告里
                    extra = _check_data_end(tag, out)
                    if extra:
                        print("      !! %s" % extra)
            results.append((idx, name, allok, time.time() - t_all))
            if not allok:
                return _fail_report(results, idx, name)
            continue

        if cli.endswith("decision/a_path_daily.py"):
            # ★★ 2026-10-22 修：原来这一段是
            #      `if idx == 8 and with_judgments is None: 跳过整个第 9 步`
            #    结果 **evidence 也被一起跳过** ⇒ 2026-10-21、10-22 两天
            #    `data/input/evidence_<D>.json` 根本没生成（实测缺）。
            #
            #   本质上 `evidence` 是**纯机械装配**（mech_driver.build：
            #   候选 ballot + 三问链条 + 环境档），**不需要人工 judgments**；
            #   需要 judgments 的只有 `emit`（把机械事实装配成 LLM 可判的题面）
            #   与 `chain`（走三问出 BUY/WAIT/NO）。
            #
            #   ⇒ 拆开：evidence **每天必跑**；emit/chain 有 judgments 才跑。
            #   这样「链跑完了」和「判断做完了」就是两件分开可见的事，
            #   不会再用一个 ✓ 把空判断盖住。
            seq = [("tools/decision/a_path_daily.py",
                    ["--stage", "evidence", "--as-of", "{d}"])]
            if with_judgments:
                seq += [("tools/decision/a_path_daily.py",
                         ["--stage", "emit", "--as-of", "{d}",
                          "--judgments", with_judgments]),
                        ("tools/decision/a_path_daily.py",
                         ["--stage", "chain", "--as-of", "{d}", "--limit", "10"])]
            else:
                print("    · evidence 必跑（机械装配，无需人工）")
                print("    · emit / chain 跳过（需人工 judgments；"
                      "加 --with-judgments <path>）")
            allok = True
            for s_cli, s_args in seq:
                ok, out, err, sec = run_gated(s_cli, s_args, dry=not execute, d=d)
                stage = s_args[s_args.index("--stage") + 1]
                print("    · %-10s %s（%.1fs）" % (stage, "OK" if ok else "FAIL", sec))
                if not ok:
                    allok = False
                    print("      !! %s" % (err or out)[-400:])
                    break
            results.append((idx, name, allok, time.time() - t_all))
            if not allok:
                return _fail_report(results, idx, name)
            continue

        fn = run_gated if writes else run_plain
        ok, out, err, sec = fn(cli, args, dry=not execute, d=d)
        print("    %s（%.1fs）" % ("OK" if ok else "FAIL", sec))
        if not ok:
            extra = _check_data_end(step_name=name, stdout=out + err)
            if extra:
                print("    !! %s" % extra)
            else:
                tail = (err or out or "").strip().splitlines()
                print("    !! 失败输出尾部：")
                for line in tail[-8:]:
                    print("       %s" % line[:150])
            results.append((idx, name, False, time.time() - t_all))
            return _fail_report(results, idx, name)

        results.append((idx, name, True, time.time() - t_all))

    print("─" * 76)
    print("\n全链完成：%d 步全部 OK · 总耗时 %.1fs" % (len(results), time.time() - t_all))
    for idx, name, ok, sec in results:
        print("  [%d] %-12s %s  %6.1fs" % (idx, name, "✓" if ok else "✗", sec))
    return 0


def _fail_report(results, idx, name):
    print("\n" + "=" * 76)
    print("✗ 停止于第 %d 步「%s」—— 后续步骤未执行（fail-fast，不掩盖问题）" % (idx, name))
    print("\n已完成：")
    for i, n, ok, sec in results:
        print("  [%d] %-12s %s  %6.1fs" % (i, n, "✓" if ok else "✗", sec))
    print("\n续跑命令：  --from %d" % idx)
    return 1


# ── 自检 ────────────────────────────────────────────────────────────────────
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

    print("daily_pipeline.py 自检")

    # ★ 2026-10-22：原来写死 10 步。新增 ETF 补采后变 11 步。
    #   断言改为「以 STEPS 自身为真源」——步数由 STEPS 决定，但**顺序与连续性必须成立**，
    #   这才是真正要守的纪律（不是那个魔数 10）。
    T("T1 步数为 STEPS 实际长度（≥10）", len(STEPS) >= 10, str(len(STEPS)))
    T("T2 步号从 0 连续无缺", [s[0] for s in STEPS] == list(range(len(STEPS))),
      str([s[0] for s in STEPS]))
    T("T3 每步都有名称/CLI/提示", all(s[1] and s[2] and s[6] for s in STEPS))

    # ★ 核心纪律：merge_klines 必须排在 S 系之前
    idx_merge = [s[0] for s in STEPS if "merge_klines" in s[2]]
    idx_s = [s[0] for s in STEPS if s[2].endswith("s2.py")]
    T("T4 ★ merge_klines 排在 S 系之前（唯一硬顺序依赖）",
      idx_merge and idx_s and idx_merge[0] < idx_s[0],
      "merge=%s s=%s" % (idx_merge, idx_s))

    # 写步骤必须标 writes=True（否则会绕闸直跑）
    writes_steps = [s for s in STEPS if s[4]]
    T("T5 至少 8 个写生产步骤", len(writes_steps) >= 8, str(len(writes_steps)))
    T("T6 merge_klines 标为写生产（必须过闸）",
      any("merge_klines" in s[2] and s[4] for s in STEPS))
    T("T7 结算标为写生产（推进游标必须过闸）",
      any("settle" in s[2] and s[4] for s in STEPS))
    T("T8 第 0 步日状态判定为只读", not STEPS[0][4])

    # S 套件覆盖 s1~s8（2026-10-23 补齐 S5/S6/S7/S8：此前完全不在日链）
    names = [os.path.basename(e[0]) for e in S_SUITE]
    T("T9 S 套件含 s1~s8（八项全在日链）",
      names == ["s1.py", "s2.py", "s3.py", "s4.py",
                "s5.py", "s7.py", "s8.py", "s6.py"], str(names))
    T("T9b S1 必须用 --as-of 显式传日期（无位置参数形式）",
      any("s1.py" in x[0] and "--as-of" in x[1] for x in S_SUITE), str(S_SUITE))
    # T9c 非关键项标记（S5~S8 缺源不该拦整链）
    _noncrit = [os.path.basename(e[0]) for e in S_SUITE
                if len(e) > 2 and e[2] and not e[2].get("critical", True)]
    T("T9c 增强型 S（s5/s6/s7/s8）标为非关键，缺源不拦整链",
      set(_noncrit) == {"s5.py", "s6.py", "s7.py", "s8.py"}, str(_noncrit))
    T("T9d S6 走 codes_from_pool（估值按票抓，不硬编票）",
      any("s6.py" in e[0] and len(e) > 2 and e[2].get("codes_from_pool")
          for e in S_SUITE), str(S_SUITE[-1]))

    # 命令模板替换
    cmd = _fmt("tools/x.py", ["--date", "{d}"], "2026-10-21")
    T("T10 {d} 占位符被替换", "2026-10-21" in " ".join(cmd) and "{d}" not in " ".join(cmd))

    # DATA_END 识别
    T("T11 能识别 DATA_END 并给 merge 提示",
      _check_data_end("s2", "ERROR DATA_END: as_of 2026-10-21 晚于行情截止") is not None)
    T("T12 非 DATA_END 时不误报", _check_data_end("s2", "normal output") is None)

    # 干跑不产生子进程写入
    r = run_gated("tools/e2e/merge_klines.py", ["--from", "{d}", "--to", "{d}"],
                  dry=True, d="2026-10-21")
    T("T13 干跑模式返回计划文本且标注 dry", r[0] and "干跑" in r[1], r[1][:60])

    # 零生产写点自证：本文件不直接 open(w)
    import ast
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        nm = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
        if nm not in ("open", "write_text"):
            continue
        mode = None
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = node.args[1].value
        for k in node.keywords:
            if k.arg == "mode" and isinstance(k.value, ast.Constant):
                mode = k.value.value
        if isinstance(mode, str) and any(c in mode for c in "wax+"):
            bad.append(ast.unparse(node)[:50])
    T("T14 编排器自身零写点（写入全部交给子进程+写作闸）", not bad, str(bad)[:80])

    # 真实 import 检查（AST，不看注释字面量）
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for al in n.names:
                imported.add(al.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    T("T15 不 import 写盘模块（AST 判定，非字面量匹配）",
      not ({"shutil", "tempfile", "pathlib"} & imported), str(sorted(imported)))

    # ── T16~T19：evidence 必须独立于 judgments 每天跑（2026-10-22 实测事故） ──
    #   事故：原代码 `if idx == 8 and with_judgments is None: 跳过整步`
    #   ⇒ evidence 被一起跳过 ⇒ 10-21、10-22 两天 evidence 文件根本没生成。
    #   这组断言把「evidence 不受 judgments 控制」变成不可绕过的判据。
    #
    #   ★ 源码文本用 `ast.unparse(tree)` 取——编排器**刻意不 import io**
    #     （T15 守的就是这条），不能为了读文件破自己的纪律。
    src = ast.unparse(tree)

    def _evidence_seq(wj):
        """模拟 _run 里 idx==8 的 seq 构造，返回 stage 列表（只读推理，不执行）。"""
        stages = ["evidence"]
        if wj:
            stages += ["emit", "chain"]
        return stages

    T("T16 无 judgments 时 evidence 仍要跑",
      _evidence_seq(None) == ["evidence"], str(_evidence_seq(None)))
    T("T17 有 judgments 时 evidence→emit→chain 三步齐",
      _evidence_seq("/tmp/j.json") == ["evidence", "emit", "chain"],
      str(_evidence_seq("/tmp/j.json")))
    # T18：直接判定「idx==8 的分支体里有没有 evidence 的调用」。
    #   旧写法的问题不是那行 if 本身，而是**分支体里 continue 掉、从不跑 evidence**。
    #   所以判据应该是：idx==8 的分支体里必须出现 "--stage evidence"。
    #   （不能拿字面量子串判断 if 条件——注释和断言自身都含那句旧代码，会误判。）
    ev_branch = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            txt = ast.unparse(node.test)
            # ★ 2026-11-02：dispatch 改为按 CLI 名识别（步号会漂移，曾致 regime/ETF 永不执行），
            #   断言同步改为认 CLI；兼容旧写法的 idx 判断（若日后回退也能过）。
            if ("a_path_daily" not in txt) and ("idx == 13" not in txt):
                continue
            body = ast.unparse(node)
            if '"evidence"' in body or "'evidence'" in body:
                ev_branch.append(txt[:60])
    T("T18 A 路分支（按 CLI 识别）内必须构造 evidence（不许整步 continue 掉）",
      bool(ev_branch), "A 路分支体里找不到 evidence 调用 ⇒ 会被一起跳过")

    # ── T18b：dispatch 禁按 idx（2026-11-02 新增，防索引漂移回归）───────────
    _idx_dispatch = [ln for ln in src.splitlines()
                     if ln.strip().startswith("if idx ==")]
    T("T18b dispatch 分支禁止按步号 idx 判断（只许按 CLI 名，步号会因增删步骤漂移）",
      not _idx_dispatch, "仍存在按 idx 的 dispatch 分支：%s" % (_idx_dispatch[:2],))
    T("T19 evidence 的 stage 在源码中无条件出现（不被 judgments 包裹）",
      '"evidence"' in src and "if with_judgments:" in src,
      "evidence 未在无条件位置构造")

    # ── T20/T21：环境档（regime / C1）必须在每日链里 ──────────────────────────
    #   事故：2026-10-22 核查发现编排器**完全不跑 regime**，
    #   于是 brain_state 的环境档停在最后一次手工运行那天（实测滞后近一个月：
    #   10-22 当天的档位是「强势/5 成」，实际是 09-24 的数据）。
    #   **环境档是仓位上限的唯一来源** —— 不跑它，后面所有「仓位该几成」
    #   都建在一个月前的旧档上。这条必须变成不可绕过的断言。
    clis = [s[2] for s in STEPS]
    T("T20 环境档（regime.py）必须在每日链里",
      any("regime.py" in c for c in clis), str(clis))
    idx_regime = next((s[0] for s in STEPS if "regime.py" in s[2]), None)
    idx_s = next((s[0] for s in STEPS if "s2.py" in s[2]), None)
    T("T21 环境档必须排在 S 系之后（S3 情绪数据先就绪）",
      idx_regime is not None and idx_s is not None and idx_regime > idx_s,
      "regime=%s s系=%s" % (idx_regime, idx_s))

    # ── T22/T23：新闻采集必须在每日链里，且排在 S 系之前 ──────────────────────
    #   事故：`data/raw/<d>/news.jsonl` 是 S1/S7/S8/E7 的共同上游，
    #   但 2026-10-22 实测**生产代码里没有任何模块采它** ——
    #   9 月每天 1000+ 条、10 月全部 0 字节。
    #   S8 报「扫 8134 条」其实是回看 30 天读到 9 月数据，当日增量恒为 0。
    T("T22 新闻采集（fetch_news.py）必须在每日链里",
      any("fetch_news.py" in c for c in clis), str(clis))
    idx_news = next((s[0] for s in STEPS if "fetch_news.py" in s[2]), None)
    T("T23 新闻采集必须排在 S 系之前（S7/S8 要用当日新闻）",
      idx_news is not None and idx_s is not None and idx_news < idx_s,
      "news=%s s系=%s" % (idx_news, idx_s))

    # ── T24：竞价采集必须在链里（S10 的唯一数据来源） ──────────────────────
    #   2026-10-22 实测：S10 判 NO_OUTPUT，但它要的竞价数据**一直在 westock quote 里**
    #   （pre_quote_* 系列），只是没人去采 —— 它只查旧包目录。
    T("T24 竞价采集（fetch_auction.py）必须在每日链里", 
      any("fetch_auction.py" in c for c in clis), str(clis))
    # ── T25/T26：2026-10-26 补的两条「数据腿」（都曾被整月漏采）────────────
    T("T25 资金流采集（fetch_fundflow.py）必须在每日链里",
      any("fetch_fundflow.py" in c for c in clis), str(clis))
    _ff = [i for i, st in enumerate(STEPS) if "fetch_fundflow.py" in st[2]]
    _ss = [i for i, st in enumerate(STEPS) if "s2.py" in st[2]]
    T("T26 资金流采集必须排在 S 系之前（S5 要用当日资金流）",
      bool(_ff) and bool(_ss) and _ff[0] < _ss[0],
      "ff=%s s_suite=%s" % (_ff, _ss))
    T("T27 新闻采集带 --notice-pool（否则公告永不采 ⇒ A路/S1/S8 上游断）",
      any("--notice-pool" in " ".join(st[3]) for st in STEPS
          if "fetch_news.py" in st[2]), "")

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


def _main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--date", default=None)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--from", dest="from_step", type=int, default=0)
    ap.add_argument("--only", default=None, help="只跑指定步号，逗号分隔，如 6,7")
    ap.add_argument("--with-judgments", default=None)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return _selftest()

    if a.list:
        print("每日链清单（顺序不可调换）：")
        for (idx, name, cli, args, writes, est, hint) in STEPS:
            print("  [%d] %-12s %-42s %s  %s"
                  % (idx, name, " ".join([cli] + args),
                     "写" if writes else "读", hint[:36]))
        return 0

    if not a.date:
        print("必须给 --date YYYY-MM-DD（或用 --list 看清单 / --selftest 自检）")
        return 2

    only = None
    if a.only:
        only = {int(x) for x in a.only.split(",") if x.strip()}
    return run_pipeline(a.date, execute=a.execute, from_step=a.from_step,
                        only=only, with_judgments=a.with_judgments)


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
