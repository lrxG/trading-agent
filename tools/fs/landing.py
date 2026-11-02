#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""landing.py · 落点封闭（施工方案 0.7 · v1.0 · 2026-10-08 建）

用途：把《核心能力设计方案》§6「产物落点是封闭的」落成**机器可判**的约束——
      系统里任何「知识 / 结论 / 研究 / 假设」的持久化写入，只有 **5 个落点**，
      外加 **5 类豁免**（非知识：交付物 / 证据库 / 能力产物 / 输入数据 / 运行状态）。
      **没有第 6 类**；写不出 Rule / 参数 / 证据的东西，落点里没有容身之处。

为什么不靠「约定」而靠机械判（用户 2026-10-08 点名审核项）：
  · 只做「目录检查」是假的封闭——目录在不在跟「谁能写、写到哪」无关。
  · 落点封闭的真实含义是：**能力输出无法自行变成正式落点**——任何被当作落点使用的
    路径，要么是 5 落点之一，要么是 5 类豁免之一，要么是**注册表里登记过**的能力输出。
    未在 `capability_registry.json` 登记的能力产物 → `UNREGISTERED_CAP_OUTPUT` 拒。

三条机械判据：
  ① 路径词汇封闭：扫全库生产代码，出现的每个「项目相对路径」必须能归类（5+5）；
     不能归类 → `UNKNOWN_PATH`（第 6 类的唯一可观测形式）。
  ② 写点目标封闭：AST 找写 sink（open(w/a/x) / os.replace / os.rename / shutil.copy|move），
     静态可解析的目标必须归类；不可解析的（走参数注入）必须出现在声明表 INDIRECT_ALLOW，
     否则 `UNDECLARED_WRITE`（新增写路径 = 必须登记，不能悄悄多一个）。
  ③ 注册表闭环：每个能力的 `output_path` 必须归类；代码里出现的能力产物路径
     （`s_<cap>_<as_of>.json` / `data/knowledge/<cap>/` / `.workbuddy/skills/<cap>/` /
     `reports/e<N>_<as_of>.md`）里的 `<cap>` 必须已在注册表登记，否则 `UNREGISTERED_CAP_OUTPUT`。

审计范围：`<root>/tools/**/*.py` 的**生产代码区**。测试/自检入口整段剔除——**按模块自报的
`_TEST_FUNCS` 派生**（`wakeup.py` 报 `_selftest`/`_acceptance`/`_discipline`；未自报者回退
`_selftest`），连带其私有夹具闭包一并置空：夹具写临时目录，不属系统落点。
**不在这里硬编码一份种子**（硬编码 = 与模块口径漂移 = 漏判「第二个测试入口」调用的夹具写点）。
平台托管目录 `.workbuddy/` 不纳入本项目落点审计（0.7）：平台运行记忆归平台，二者互不统计。

用法：
  python tools/fs/landing.py                  # 只读：落点清单 + 审计结论
  python tools/fs/landing.py --report         # 全量明细（词汇/写点/注册表闭环）
  python tools/fs/landing.py --selftest       # 内置验收（临时目录，不碰真数据）
  python tools/fs/landing.py --discipline

规则：新增落点 = 改架构（须先改设计 §6 与施工 0.7，再改本文件）；禁在本文件之外另立落点清单。
"""
import ast
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools", "fs")))
from paths import ROOT  # noqa: E402
import registry as REG  # noqa: E402
import astwin as ASTW  # noqa: E402   ← AST 窗口（尾界）唯一实现，禁本模块自写

SCHEMA_VERSION = 1

# ── 5 落点（知识 / 结论 / 研究 / 假设的唯一归宿）─────────────────────────────────
LANDINGS = (
    {"id": "LD1", "name": "决策快照", "pattern": "data/generated/decision_ledger.jsonl",
     "also": ("data/generated/sim/decision_ledger.jsonl",), "item": "0.3",
     "judge": "「当时为什么这么做」——append-only，代码无 update/delete 路径"},
    {"id": "LD2", "name": "净值账本", "pattern": "data/generated/pnl_ledger.jsonl",
     "also": ("data/generated/sim/pnl_ledger.jsonl",), "item": "0.4",
     "judge": "「后来实际发生了什么」——与 L1 严格分层，不复制判断字段"},
    {"id": "LD3", "name": "正式参数", "pattern": "data/params.json",
     "also": ("data/params_changelog.jsonl",), "item": "0.8 / 0.9",
     "judge": "唯一运行时权威 + 其变更史（changelog 与 params 同族，同属落点③）"},
    {"id": "LD4", "name": "参数候选队列", "pattern": "data/calibration_queue.jsonl",
     "also": (), "item": "0.9",
     "judge": "候选值只在队列、**永不进 params.json**（转正唯一入口 = promote）"},
    {"id": "LD5", "name": "决策卡驱动依据区", "pattern": "data/decision_card.md",
     "also": (), "item": "0.15",
     "judge": "只收 VALIDATED + 高置信 UNDER_TEST 的 Rule / 经验条目（同一文件另有"
              "「0.15 载体」身份，属豁免 X5；两身份都记，互不否认）"},
)

# ── 5 类豁免（非知识：交付物 / 证据库 / 能力产物 / 知识库 / 状态与输入数据）─────
EXEMPTIONS = (
    {"id": "X1", "name": "E 系列交付物（给人看）", "patterns": ("data/generated/reports/",),
     "item": "E1~E11", "judge": "格式化报告是交付物、非知识落点"},
    {"id": "X2", "name": "研究证据库", "patterns": ("data/research/<research_id>/",),
     "item": "1.7 / 0.7", "judge": "append-only 证据，不自动进决策链，只供校准审查读"},
    {"id": "X3", "name": "S 输出快照", "patterns": ("data/generated/s_<cap>_<as_of>.json",),
     "item": "2.10", "judge": "S 运行产物、非知识落点（as_of 唯一键）"},
    {"id": "X4", "name": "S 知识库", "patterns": ("data/knowledge/<cap>/",),
     "item": "2.10", "judge": "经 _registry.json 路由；决策期只读、C6 维护期更新"},
    {"id": "X5", "name": "运行状态与行情数据（非知识）",
     "patterns": ("data/snapshot/", "data/raw/", "data/state/", "data/learning/",
                  "data/generated/sim/", "data/generated/cases.jsonl",
                  "data/generated/h_board.md", "data/decision_card.md",
                  "memory/", ".workbuddy/"),
     "item": "0.1 / 0.2 / 0.5 / 0.12 / 0.13 / 0.15 / 0.18 / 4.2",
     "judge": "判据 = 记的是「知识」还是「非知识」：状态（游标/池/持仓/进度/看板）与"
              "输入数据（行情快照 / 采集投放）走本豁免。`h_board.md` = 0.12 跨日看板"
              "（今日必裁/日历预装/明日到期）——**由状态派生的视图，不是新知识**，"
              "与 `brain_state.md`/`decision_card.md`（0.15 载体）同性质"},
    {"id": "X6", "name": "外部判断输入（proposals 交换件）",
     "patterns": ("data/input/",),
     "item": "1.4 / 4.2",
     "judge": "C3/C4 的**判断产物**交换件（LLM 路或机械基准路产），非知识非状态："
              "它按 as_of 一次性消费、不进知识库、不参与回放真源。"
              "★ 机械基准另存 `mech_proposals_<as_of>.json`（与 LLM 的 "
              "`proposals_<as_of>.json` 分文件），producer 字段可机械区分"},
)

# 能力落点目录（S/E 插件源码；平台项目级技能目录）
CAPS_DIR_PATTERN = ".workbuddy/skills/<cap>/"

# 写点声明表：静态不可解析（走参数注入 / 局部变量）的写点必须在登记。
# 每条 = (模块文件名, 函数名, 真实目标（项目相对，可多个）, 理由)。
#
# 这张表不是「豁免名单」而是**可反查的契约**，审计器对它做三项机械校验：
#   ① 目标可归类：每个 target 必须落进 5 落点 / 5 豁免，否则 `WRITE_TARGET_UNKNOWN`；
#   ② 无悬空登记：登记的 (文件, 函数) 必须**真的**在真库中命中写点，否则 `STALE_DECLARATION`
#      （防「函数改名 / 删了」后登记表变成一堆好看的废话）；
#   ③ 无漏登记：出现未登记的不可解析写点 → `UNDECLARED_WRITE`。
# 新增写路径必须登记 —— 「悄悄多一个写路径」和「悄悄少一条登记」同样违规。
WRITE_ENTRYPOINTS = (
    ("cursor.py", "_write", ("data/state/cursor.json",),
     "path 为路径注入口（默认 = 全案唯一游标落点，测试可注入）"),
    ("candidates.py", "_save", ("data/state/candidates.json",),
     "path 为路径注入口（默认 = 候选池落点）；tmp 为同目录原子写临时件"),
    ("decision.py", "_append", ("data/generated/decision_ledger.jsonl",),
     "base 为账本根注入口（默认 = data/generated/，按 SIM/LIVE 轴内分流）"),
    ("pnl.py", "_append", ("data/generated/pnl_ledger.jsonl",),
     "base 为账本根注入口（默认 = data/generated/，按 SIM/LIVE 轴内分流）"),
    ("params.py", "_append_jsonl", ("data/calibration_queue.jsonl",
                                    "data/params_changelog.jsonl"),
     "path 为队列/变更史落点注入口（默认 = data/ 根两个 jsonl）"),
    ("params.py", "_write_params", ("data/params.json",),
     "path 为正式值落点注入口；仅 promote() 可调用（AST 纪律另检）"),
    ("registry.py", "_write_json", ("data/state/capability_registry.json",
                                    "data/state/skeleton_manifest.json"),
     "path 为注册表/冻结台账落点注入口"),
    ("snapshot.py", "_dump", ("data/snapshot/<date>/",),
     "path 由 write_snapshot 计算（data/snapshot/<date>/<契约文件>）"),
    ("snapshot.py", "write_snapshot", ("data/snapshot/<date>/",),
     "final/old/tmp 为该日快照目录的原子换入三态（同卷目录改名 = 原子）"),
    ("observe.py", "_append", ("data/state/run_log.jsonl",),
     "run_log 唯一写入方（0.17 单写点纪律：只有 observe 能写）；path 为路径注入口"),
    ("learn_pipeline.py", "_collect_cases_from_sandbox",
     ("data/generated/cases.jsonl",),
     "回测学习池汇总写点（2026-10-23）：把历史回放沙盒产出的 cases 并入学习样本池；"
     "沙盒在系统临时域（不碰生产），本写点按 case_id 幂等去重；"
     "落点 = X5 豁免区（非知识，运行状态）；锚定模块常量 CASES（禁形参，"
     "否则审计报 unresolved-name）"),
    ("strategy_matrix.py", "_write_matrix_json", ("data/research/sweep/",),
     "策略×环境矩阵机读产物（2026-10-23 · 用户令「所有策略都要研究」）：默认零写点，"
     "仅 --json 显式指定才落盘；path 为调用方注入（同 cursor._write 先例）；"
     "落点 = X2 研究豁免区（append-only 证据，不自动进决策链）"),
    ("hypothesize.py", "_append", ("data/research/hypotheses_candidates.jsonl",),
     "假设候选写点（2026-10-23 · §8.3 第②层）：obs 事实 → {condition,evidence,target} "
     "假设，append JSONL；落点 = X2 研究豁免区（append-only 证据，不自动进决策链）"),
    ("learn_pipeline.py", "_mark_weekly_done",
     ("data/state/learn_weekly_state.json",),
     "周度闸状态写点（2026-10-23 同批）：记 last_week/last_date/last_window，"
     "本周已跑则跳过回测（回测实测 680s，日跑会拖垮日链；设计 §12 度量口径本就是每周）；"
     "落点 = X5 豁免区（运行状态，非知识）"),
    ("hboard.py", "_write", ("data/generated/h_board.md",),
     "跨日看板唯一写入口（0.12）；看板=当日快照视图（整篇重写，非 append），"
     "path 为路径注入口；tmp 为同目录原子写临时件"),
    ("thesis.py", "_save", ("memory/state/thesis.md",),
     "path 为 Thesis 落点注入口（默认 = memory/state/thesis.md）；tmp 为同目录原子写临时件"),
    ("settle.py", "_append_jsonl", ("data/learning/demand_queue.jsonl",),
     "path 为学习队列落点注入口（默认 = data/learning/）"),
    ("gen_skill_entries.py", "main", (".workbuddy/skills/",),
     "开插批次（2026-10-19）：注册表 impl_exists 判据要求的 S/E 技能入口"
     "（.workbuddy/skills/<ID>/SKILL.md）；内容全部派生自注册表条目+执行件 docstring"
     "（零编造），幂等重生成；目录级模式（带 <ID> 的模式会被 cap-ref 判据读成未登记 cap）"),
    ("run_chain.py", "write_env_leg", ("memory/state/brain_state.md",),
     "C1 环境档腿回写（2026-10-19 修断链）：C1 的产出必须落到 `brief.read_env` 真正读的"
     "段，否则下游读到上一交易日的环境档（实测 09-30 链：C1 报强势/5成而 brief 读 09-29）。"
     "路径由 base 派生（p 局部变量）⇒ 静态不可解析，须登记"),
    ("mech_driver.py", "emit", ("data/input/",),
     "机械基准判断产物 `data/input/mech_proposals_<as_of>.json`（文件名按日动态拼装 ⇒ "
     "静态不可解析，须登记）。★ 与 LLM 产物 `proposals_<as_of>.json` **分文件**，"
     "producer/axis 字段可机械区分；LIVE 轴由 run_chain 的 MECHANICAL_BASELINE 锁阻断"),
    ("gen_method_skills.py", "main", (".workbuddy/skills/",),
     "S4/S6/S7/S8 方法层补全（2026-10-19）：把 id 入口桩升级为真方法层 SKILL"
     "（Design Boundary/框架/输入契约/接缝/红线），内容溯源 e4/e7 KB + 台账设计目的；"
     "幂等覆盖写；.workbuddy/skills/ 为 X5 豁免区"),
    ("snapshot.py", "_dump_admission", ("data/snapshot/<date>/",),
     "A9 准入判决 sidecar（`_admission.json`）；path 由 write_snapshot 传入 tmp/正式目录，"
     "与 5 契约文件同批原子换入"),
    ("writegate.py", "run_gated", ("data/state/",),
     "写入闸运行证书（cert_path）：前后核验明细落 X5 运行状态区；证书自身禁落生产"
     "受护区（check_write 守卫）；cert_path 由调用方注入（沙盒/运维目录）"),
    ("llm_emit.py", "emit", ("data/input/",),
     "A 路判断产物 `data/input/proposals_<as_of>.json`（2026-10-20）：LLM（wb）"
     "判断文件（非受护区）→ 装配 chains → 覆写 proposals；tmp/dst 均在该目录内"
     "（X6 外部判断输入豁免区；文件名按日动态拼装 ⇒ 静态不可解析，须登记）"),
    ("backfill_klines_tq.py", "write_out",
     ("data/raw/bus/klines.csv", "data/research/obs/"),
     "通达信补历史 K 线（2026-10-21）：p = klines.csv（全历史单文件），tmp 为同目录原子"
     "写临时件；备份走 data/research/obs/klines_backup_<ts>.csv.gz。路径由 root 派生 ⇒ "
     "静态不可解析，须登记"),
    ("fetch_finance.py", "write_append", ("data/raw/bus/finance.csv",),
     "抓历史财务含公告日（2026-10-21 · A 基本面）：数据源 `westock finance --fields all`（**必须 all 才有 InfoPublDate**）。长表存储。纪律：缺 publ_date 整条丢弃、断点续跑、dry-run 默认须 --execute。"
     "2026-10-22 加写前键级判重（M22）：事故见下条 finance_dedup"),
    ("fetch_finance.py", "acquire_lock", ("data/state/_fetch_finance.lock",),
     "采集进程锁（2026-10-22 · M21 孤儿进程事故修复）：**不落业务数据，只落互斥标记**，"
     "内容 = pid+ts，长跑时 touch 刷新 mtime。作用：把「同时只允许一条采集链写 finance.csv」"
     "变成机器可检查的事实，避免 2026-10-21 起 18 小时孤儿进程那种并发污染。"
     "超过 LOCK_STALE_SEC(4h) 未刷新视为僵死锁可覆盖（防崩溃后永久堵死）"),
    ("heat_sensor.py", "write_rows", ("data/raw/bus/margin_daily.csv",),
     "两融数据采集（2026-10-21 · 补 DATA-HUB 声称存在但从未实现的断链）："
     "数据源 `westock fund margin`。★ 单票调用返回**英文列**、批量调用返回**中文列**"
     "（且列名带单位后缀如 `融资余额环比(%)`）⇒ 必须两套别名 + 前缀匹配，否则批量一行都解析不出。"
     "append-only，主键 date+code，幂等跳过已有。纪律：取不到不写空行不填 0；"
     "`--clean` 清重复表头垃圾属治理动作（先备份 .bak）"),
    ("fetch_auction.py", "write_all", ("data/raw/bus/auction_<as_of>.csv",),
     "集合竞价采集（2026-10-22 · 为 S10 落地）：S10 的 availability 说 auction_feed 不可用，"
     "但实测 westock quote 里就有完好竞价字段（pre_quote_price/change_pct/volume/amount、"
     "wb_ratio、price_ceiling）。S10 缺的不是数据，是没人去采 —— 它只查旧包目录。"
     "纪律：只采个股、批量 40、缺竞价字段丢弃、.tmp+os.replace 原子写、进程锁"),
    ("build_decision_card.py", "write_card", ("data/decision_card.md",),
     "决策卡生成（0.15 载体 · 2026-10-22 补建）：设计 §5.2/§6 要求、landing LD5 已登记，"
     "但全项目没有任何模块写它 ⇒ C4 判 NO_OUTPUT。本工具只做装配："
     "三问（proposals + decision_ledger）/ 驱动依据区（只收 VALIDATED 的 Rule）/ "
     "参数卡（每个数带防什么抓什么，未声明的标 PURPOSE_UNDECLARED 不编理由）。"
     "纪律：零判断、只读+单文件写、.tmp+os.replace 原子写、幂等"),
    ("fetch_news.py", "write_append", ("data/raw/*/news.jsonl",
                                       "data/state/_fetch_news.lock"),
     "新闻/公告采集（2026-10-22）：data/raw/<d>/news.jsonl 是 S1/S7/S8/E7 的共同上游，"
     "但生产代码**没有任何模块采它** —— 2026-10-22 实测 9 月每天 1000+ 条、10 月全部 0 字节，"
     "S8 报「扫 8134 条」其实是回看 30 天读到 9 月的数据。"
     "数据源 `westock news list`（市场资讯）+ `westock notice list`（个股公告）。"
     "纪律：append 按 news_id 判重、不编造 body、市场资讯 related_codes 留空、"
     "只留目标日条目、.tmp+os.replace 原子写、进程锁"),
    ("fetch_fundflow.py", "write_all", ("data/raw/bus/fundflow.csv",
                                        "data/raw/bus/fundflow.csv.bak.*"),
     "个股日度资金流采集（2026-10-22 · 为 S5 落地）：S5 只从旧包读 fundflow，"
     "旧包该表停在 2026-09-30 且当日仅 716 只票 ⇒ S5 永远拿不到 10 月数据。"
     "数据源 `westock fund flow <单码> --start --end`（单码可一次取整区间）。"
     "字段与旧包逐一对应、量纲一致（元）。纪律：增量补采键=(code,date)、"
     "不填0不插值、单只失败如实入清单、先 .bak（同函数内，名带时间戳 ⇒ 通配登记）、"
     ".tmp+os.replace 原子写、按 code+date 排序保证幂等"),
    ("fetch_fundflow.py", "acquire_lock", ("data/state/_fetch_fundflow.lock",),
     "采集进程锁（M21）：不落业务数据，只落互斥标记 pid+ts"),
    ("fetch_etf_klines.py", "write_all", ("data/raw/bus/etf_klines.csv",
                                          "data/raw/bus/etf_klines.csv.bak.*"),
     "ETF K线增量补采（2026-10-22）：etf_klines.csv 是旧包原样副本、停在 2026-09-22，"
     "九步编排不采它 ⇒ strategy_family 的低波轮动结论建立在旧数据上。"
     "数据源 `westock kline` 批量+日期区间。纪律：增量补采、不填0不插值、"
     "先 .bak 备份（备份名带时间戳 ⇒ 通配登记）、.tmp+os.replace 原子写、"
     "按 code+date 排序保证幂等"),
    ("s1.py", "_write_snapshot", ("data/generated/s_s1_<as_of>.json",),
     "S1 事件催化唯一写点（X3 豁免 s_<cap>_ 前缀）。2026-10-22 补 --eval/scan_day 入口："
     "evaluate 本是单事件接口、此前只有 --selftest，每日链跑不到它。scan_day 从当日 "
     "news.jsonl 取带关联个股的事件逐个评估；related_codes 为空一律跳过（不猜）"),
    ("purge_nonstock_candidates.py", "purge", ("data/state/candidates.json",
                                              "data/state/candidates.json.bak.*"),
     "候选池非个股清理（2026-10-22）：4650 条里 355 条非个股，全为 EXPIRED —— "
     "BOND 180（可转债）/ BSE_MISFIXED 171（北交所股被上游新闻源错标成 sz，"
     "如 sz920033 实为 bj920033 康普化学）/ OTHER 3（上交所回购、深市债券、新三板）/ INDEX 1。"
     "根因=五路采纳零代码类型校验，判据散落四处口径不一；"
     "已由 tools/fs/codes.py（唯一真源）+ scan.add() 类型闸根治。"
     "纪律：只清 EXPIRED（遇非 EXPIRED 非个股拒绝执行）、先 .bak 备份、"
     ".tmp+os.replace 原子写、默认干跑、幂等、不改任何候选业务字段"),
    ("finance_dedup.py", "write_rows", ("data/raw/bus/finance.csv",),
     "finance.csv 去重（2026-10-22 · 孤儿采集进程善后）：2026-10-21 14:38 启动的单票采集"
     "进程未被回收，跑到次日 08:41（18 小时）反复追加已采数据 ⇒ 136.5 万行里 34.5 万行完全重复。"
     "键=(code,end_date,publ_date,source_type,field)；值冲突 166 条保留后者（财务追溯调整越晚越准）"
     "并显式列出。纪律：只去重不插补不缩尾、先 .bak 备份、.tmp+os.replace 原子写、dry-run 默认"),
    ("heat_sensor.py", "clean_garbage", ("data/raw/bus/margin_daily.csv", "data/raw/bus/"),
     "两融文件重复表头垃圾清理（治理动作）：先写 .bak 备份再回写；"
     "已有 28 行重复表头垃圾（2026-10-21 data_health 体检发现）"),
    ("fetch_mktcap.py", "write_rows_append", ("data/raw/bus/mktcap.csv",),
     "抓逐日股本→推算历史市值（2026-10-21 · G1 能力）：数据源为通达信只读 "
     "`get_gb_info`（gb_type=1 + start/end_date 给逐日 Zgb/Ltgb，单位=股，已用 "
     "600178 ActiveCapital 校验一致）；市值 = 总股本 × 当日 close。纪律：缺股本"
     "的日期不输出不插值、断点续跑、dry-run 为默认须 --execute"),
    ("fetch_industry_map.py", "write_map", ("data/raw/meta/stock_industry.json",),
     "抓「个股→行业」映射并固化（2026-10-21 · G2 能力）：数据源为通达信只读接口"
     "`get_sector_list` + `get_stock_list_in_sector`（881xxx 一级行业 128 个）；"
     "写 stock_industry.json 及同名 .provenance.json（原子写 tmp→replace）；"
     "服务不可用则如实失败退出、不产出空文件冒充成功"),
    ("backtest.py", "_main", ("data/generated/reports/",),
     "回测交割单 `--out <路径>`（2026-10-21 · 策略验证）：显式导出逐笔交割单 CSV，"
     "路径由调用方命令行注入；**本件零生产自动落点**（回测是只读研究，不写账本/不改数据），"
     "登记以声明这是受控外导（建议指 data/generated/reports/ 或临时目录）"),
    ("backfill_zt_reason.py", "backfill_day", ("data/raw/<date>/",),
     "涨停池补源（腾讯自选股官方 CLI）：limit_up.csv 补 first_seal/"
     "seal_amount/reason 后重 promote 回 snapshot；tmp/p 为按日目录内原子写"
     "（路径按日循环变量派生 ⇒ 静态不可解析，须登记）"),
    ("sector_seed.py", "_write_csv", ("data/raw/<date>/",),
     "板块估值/行情截面（2026-10-20）：sector_ranking.csv + sector_valuation.csv "
     "按日落盘；tmp/path 为按日目录内原子写（派生 ⇒ 静态不可解析，须登记）"),
    ("sector_seed.py", "history", ("data/raw/meta/sector_valuation_history.csv",),
     "估值序列补采（2026-10-20）：单文件多日期累积 append（序列语义≠按日截面）；"
     "out_p 由 meta 目录派生（静态不可解析，须登记）"),
    ("merge_klines.py", "merge_days", ("data/raw/bus/klines.csv",),
     "klines 合并器（2026-10-20 · 数据层贯通）：10月按日截面 → S 系全历史单文件"
     "（追加式写点；bus_p 由 root 派生 ⇒ 静态不可解析，须登记）"),
    ("a_path_daily.py", "evidence", ("data/input/",),
     "A 路证据包 `data/input/evidence_<as_of>.json`（2026-10-20 · A 路日常化）："
     "选票+证据五件+环境档投给 LLM 判断者；p 由 root 按日拼装 ⇒ 静态不可解析，须登记"
     "（同 llm_emit 的 X6 外部判断输入豁免区口径）"),
    ("a_path_daily.py", "_main", ("data/input/",),
     "A 路诊断报告 `--report-out <路径>`（2026-10-20）：完整 run_chain 报告的显式导出，"
     "路径由调用方命令行注入（建议临时目录）；非自动落点 ⇒ 唯一目的即「不污染生产」，"
     "登记以声明这是受控外导而非常规写点"),
    ("seed_real.py", "do_reseed", ("data/state/reseeds/",),
     "重播种 changelog「RESEED.json」；写目标 box = data/state/reseeds/<TS>/（X5 属 data/state/，"
     "但 box 为运行期拼装 ⇒ 静态不可解析，须登记）。数据文件本身不走本写点"
     "（经 ingest/snapshot 公开 API 的 replace/force 通道）"),
    ("seed_real.py", "_mk_meta", ("data/raw/<date>/",),
     "自检助手：只在**测试沙盒** `LD.roots(base)` 派生投放区写合成 `_core.meta.json`"
     "（V22 防重入闸单测用）；rdir 由参数注入 ⇒ 静态不可解析，故须登记。"
     "生产路径不调用本函数（生产 meta 由 `ingest.stage_core` 写）"),
    ("premarket.py", "_atomic_write", ("data/generated/reports/",),
     "E1 盘前预案唯一写点：`e1_<as_of>.md`（registry E1 output_path 原样；文件名按日"
     "动态拼装 ⇒ 静态不可解析，须登记）。路径分类 = **EXEMPT/X1**（E 系列交付物，给人看），"
     "X1 只豁免「路径归类」不豁免「写点声明」——与 seed_real.do_reseed 同理。"
     "幂等：同输入同字节 ⇒ 跳过写；测试一律 --out 沙盒隔离"),
    ("s4.py", "_write_snapshot", ("data/generated/s_s4_<as_of>.json",),
     "S4 拥挤度唯一快照写点（X3 豁免 pattern 形态；文件名按日动态拼装 ⇒ 须登记）。"
     "tempfile 同目录 + os.replace 原子换入（与 s5._write_snapshot 同款纪律）"),
    ("s7.py", "evaluate", ("data/generated/s_s7_<as_of>.json",),
     "S7 财报节点唯一快照写点（X3 豁免 pattern 形态；文件名按日动态 ⇒ 须登记）"),
    ("s7.py", "fetch_consensus", ("data/generated/s_s7_consensus_<as_of>.json",),
     "S7 一致预期缓存唯一写点（2026-10-14 接线）：westock consensus CLI 取数 → 缓存件"
     "（X3 同款 S 产物形态；**登记目标 = 小写，与源码盘面文件逐字符一致**；"
     "大小写判据经 cap 归一大写匹配注册表 id S7）。**evaluate 只读缓存零网络**——本函数是唯一网络触点"
     "（人触发 CLI --fetch-consensus）；缓存缺席 = 预期差 UNAVAILABLE（不伪装）"),
    ("s7.py", "fetch_sector_forecast", ("data/generated/s_s7_sector_forecast_<as_of>.json",),
     "S7 行业级盈利预测缓存唯一写点（2026-10-15 Option A）：westock sector forecast "
     "→ 申万行业机构一致预期盈利路径（X3 同款 S 产物形态；小写登记，与盘面逐字符一致）。"
     "**行业代理≠个股一致预期**（kind=INDUSTRY_LEVEL_PROXY；不回填个股字段）；"
     "evaluate 只读缓存零网络；缓存缺席 = industry_forecast ABSENT（不影响个股段语义）"),
    ("s6.py", "fetch_valuation_section", ("data/generated/s_s6_section_<as_of>.json",),
     "S6 估值截面缓存唯一写点（2026-10-14 接线）：westock quote（个股 PE/PB/市值截面）"
     "+ sector valuation（行业截面+历史百分位）→ 缓存件（X3 同款；小写登记，与盘面逐字符一致）。"
     "**evaluate 只读缓存零网络**；截面≠序列——≥120 日序列契约恒保留，禁伪装满足"),
    ("s8.py", "evaluate", ("data/generated/s_s8_<as_of>.json",),
     "S8 政策事件流唯一快照写点（X3 豁免 pattern 形态；文件名按日动态 ⇒ 须登记）"),
    ("e10_daily.py", "emit", ("data/generated/reports/",),
     "E10 每日投研简报唯一写点：`e10_daily.jsonl`（E3 同款 append-only + content_hash "
     "幂等；路径分类 = **EXEMPT/X1**，给人看的交付物；X1 只豁免路径归类不豁免写点声明）"),
    ("eod_review.py", "append_review", ("data/generated/reports/",),
     "E3 盘后复盘写点①（任务 209 补登记 · 原为审计盲区：路径为 Name 非常量，静态分析"
     "不可见）：`_report_path(base)` 下 `e3_daily.jsonl`（append-only + content_hash "
     "幂等，同日异内容拒改写）。**路径跟随 --base**（沙盒隔离；生产根写入须过 "
     "writegate 无票即拒）。数据全部只读（账本/快照/候选池/持仓档），零交易动作"),
    ("eod_review.py", "_write_md", ("data/generated/reports/",),
     "E3 盘后复盘写点②：`_write_md` 下 `e3_<as_of>.md`（人读报告，幂等覆写——md 是 "
     "jsonl 的渲染投影非账本，故允许覆写保持一致）。同样跟随 --base；生产根过 writegate"),
    ("e4_research.py", "ensure_topics", ("data/research/e4_<topic_id>/",),
     "E4 课题载体写点：topic.md + status.json（X2 研究豁免区；目录按需求指纹动态 "
     "拼装 ⇒ 静态不可解析，须登记）"),
    ("e4_research.py", "advance", ("data/research/e4_<topic_id>/status.json",),
     "E4 状态流转写点（open→researching→delivered；禁跳级/倒退由模块自检固化）"),
    ("cal_convert.py", "_main", ("data/raw/meta/",),
     "日历续期转换层唯一写点（X5 豁免区 = 原始数据/元数据）：`--convert <out>` 目标由"
     "CLI 注入（沙盒路径或新日历文件如 trade_cal_2027.csv）⇒ 静态不可解析，须登记。"
     "**禁覆盖生产 trade_cal.csv**——替换须单独授权（先沙盒交叉核对零冲突）"),
    ("run_chain.py", "_main", ("data/generated/reports/",),
     "C0→C6 链路运行器唯一写点：`--out <path>` 链路报告 JSON 路径由 CLI 注入（静态不可解析，"
     "缺省 = 只打印不落盘；生产惯例落 X1 交付物区 data/generated/reports/）⇒ 须登记"
     "（cal_convert 同款口径）。生产数据写一律经被调模块（regime/scan/orchestrate/settle…）"
     "各自的 writegate 守卫，本件零直写生产"),
    ("gendrive.py", "generate", ("data/generated/reports/",),
     "C3 驱动规格生成器唯一写点：`generate(..., out=<path>)` 对 out 以写模式落盘 driver-file"
     "（路径由 CLI --out 注入，静态不可解析；缺省 out=None = 只打印统计不落盘；生产惯例落 "
     "X1 交付物区 data/generated/reports/，或沙盒路径）。本件**只读**生产各证据面"
     "（candidates/mainline/S2/S4/S5/快照/thesis.md），零直写生产；driver-file 是临时交换件"
     "（供 run_chain --driver-file 消费），非 5 知识落点 ⇒ 须登记"),
    ("priceline.py", "main", ("data/generated/reports/",),
     "C3 价线输入件唯一写点：`main()` 对 `--out` 注入的 a.out 以写模式落盘价线 JSON"
     "（缺省 a.out=None = 只打印统计不落盘；生产惯例落 X1 交付物区 data/generated/reports/，"
     "或沙盒路径）。本件**只读**旧包 klines.csv（外部输入，禁写禁搬）+ 候选码清单，零直写"
     "生产；价线件是临时规划交换件（供 gendrive --price-lines 备查消费），非 5 知识落点"
     "⇒ 须登记"),
    ("pl_input.py", "_main", ("data/generated/reports/",),
     "C3 正式价线输入适配器唯一写点：`_main()` 对 `--out` 注入的 a.out 以写模式落盘"
     "（授权准入结果 / 骨架起草件；缺省 a.out=None = 只打印不落盘；生产惯例落 X1 交付物区 "
     "data/generated/reports/，或沙盒路径）。本件**只读** priceline 价线 + 授权输入件，"
     "零直写生产；本件**不判动作、不落决策账本**（决策落账唯一路径 = orchestrate.decide），"
     "不绕过 C5 硬闸；授权件是临时交换件，非 5 知识落点 ⇒ 须登记"),
    ("outcome_watch.py", "watch", ("data/learning/outcome_watch.jsonl",),
     "C6 非下注决策出路兑现验证唯一写点：`watch(..., out=<path>)` 对 out 以 append 模式"
     "落 outcome_watch.jsonl（规则判定行）。生产惯例落 X5 运行区 data/learning/（与 "
     "demand_queue 同区）；路径由 CLI --out 注入（静态不可解析，缺省 out=None = 只打印"
     "不落盘）。本件**只读**决策账本 + 快照 + 候选池，零直写资金账本/候选池；"
     "rule=null/非白名单 field ⇒ UNJUDGEABLE、快照缺 ⇒ PENDING（零伪造）"),
    ("expect_ref.py", "main", ("data/generated/reports/",),
     "C3 剩余预期（S1 历史证据）输入件唯一写点：`main()` 对 `--out` 注入的 a.out 以写模式"
     "落盘剩余预期 JSON（缺省 a.out=None = 只打印统计不落盘；生产惯例落 X1 交付物区 "
     "data/generated/reports/，或沙盒路径）。本件**只读** S1 效应库（经 s1.load_kb/loader "
     "注册表路由 + 时序守卫）+ S1 backfill（read_backfill 带 LOOKAHEAD_GUARD）+ 快照 news 面 "
     "+ 旧包 klines.csv（市场等权涨幅），零直写生产；知识库**只读**（晋升走治理动作，非本件）；"
     "输出 room_ref 恒带 restricted_reference=True（受限历史参考≠当前预期/胜率），供 gendrive "
     "--expect-ref 备查消费，非 5 知识落点 ⇒ 须登记"),
    ("pred_ref.py", "_main", ("data/generated/reports/",),
     "C3 M1/M2/M3 预测基准输入件唯一写点：`_main()` 对 `--out` 注入的 a.out 以写模式落盘"
     "基准件 JSON（缺省 a.out=None = 只打印统计不落盘；生产惯例落 X1 交付物区 "
     "data/generated/reports/，或沙盒路径）。本件**只读**旧包 klines.csv（M1 个股 T+N 前向"
     "收益分布 / M3 量比分布，严格 date<as_of 防未来函数，外部输入禁写禁搬）+ S2 知识库"
     "（data/knowledge/s2_theme_lifecycle/，M2 family 前向基准，loader 时序守卫只读）+ 当日 "
     "s_s2 生成件（code→题材家族映射），零直写生产；三槽 baseline/threshold 恒为历史分位"
     "（可追溯、非主观），恒带 restricted_reference=True，供 gendrive --pred-ref 备查消费，"
     "非 5 知识落点 ⇒ 须登记"),
    ("pred_input.py", "_main", ("data/generated/reports/",),
     "C3 正式 prediction 授权输入适配器唯一写点：`_main()` 对 `--out` 注入的 a.out 以写模式"
     "落盘准入件 JSON（缺省 a.out=None = 只打印统计不落盘；生产惯例落 X1 交付物区 "
     "data/generated/reports/，或沙盒路径）。本件**零读零写生产知识库**：只读授权输入件 "
     "（--pred-file）+ 可选 pred_ref 基准件（防复制校验）+ 候选池码清单（--base/--active-codes "
     "只读），零直写生产；准入产出 `chain_prediction` 是临时交换件（供 gendrive --pred-input "
     "填 chain.prediction），非 5 知识落点 ⇒ 须登记。**三方角色分离**：提交者（自报，弱绑定）/ "
     "授权者（仅审计记录）/ 机械校验者（本件，只校结构口径不判对错）"),
    ("intraday_scan.py", "write_snapshot", ("data/raw/intraday/<date>/",),
     "盘中实时扫描快照（2026-10-26 · E2 盘中腿）：jp/cp/tmp/tmpc 全在该日目录内，"
     "**双落 JSON+CSV**（JSON=完整 payload 供下游消费，CSV=扁平截面供人看/表格工具）；"
     "文件名按 date+hhmm 动态拼装 ⇒ 静态不可解析，须登记。"
     "落点 = X5 豁免区（data/raw/ 行情数据，非知识）；tmp + os.replace 原子写；"
     "进程锁见 acquire_lock"),
    ("intraday_scan.py", "acquire_lock", ("data/state/_intraday_scan.lock",),
     "盘中扫描进程锁（M21 同款）：不落业务数据，只落互斥标记 pid；"
     "防「两次扫描同时写同一 hhmm 快照」与「孤儿进程长期持锁」"),
    ("intraday_scan.py", "append_alerts", ("data/raw/intraday/<date>/",),
     "盘中发现流水（2026-10-26 补 · 旧包 tools/data/intraday_scan.py 原始形态）："
     "**append-only + 按时点分节 + 只记增量 + 零判断**，旧版原文「不做买卖建议、"
     "任何市场判断，记录本身就是发现」；同一 HHMM 重跑幂等跳过，绝不覆盖历史档；"
     "路径由 alerts_path(date) 派生 ⇒ 静态不可解析，须登记。落点 = X5 运行数据区"),
    ("intraday_scan.py", "_main", ("data/generated/reports/",),
     "E2 盘中扫描报告唯一写点：`intraday_scan_<date>_<hhmm>.md`（X1 交付物区；"
     "文件名按日+时刻动态拼装 ⇒ 静态不可解析，须登记）。"
     "★命名纪律：**禁 e2_ 前缀** —— `e2_<as_of>.md` 是 registry 登记的 E2 产物路径，""capability_status 按 e2_*.md 取最新件判 LIVE/DEGRADED；实测 e2_scan_* 会因字符序更大抢到「最新」位置，正文无 UNAVAILABLE ⇒ E2 假 LIVE。"
     "本件是实盘事实层报告，不覆盖；tmp + os.replace 原子写"),
    ("timing.py", "write_state", ("data/state/timing_state.json",),
     "择时闸最新信号（risk_on/off + 指数/MA20/偏离）——C1 上游开关的读取源；"
     "tmp+replace 原子写"),
    ("timing.py", "append_log", ("data/state/timing_log.jsonl",),
     "择时前瞻验证日志（每日一行，3 个月后核对实盘与回测是否同向）——"
     "回测只是必要条件，前瞻记录才是证据终点"),
    ("fetch_micro.py", "write_rows", ("data/raw/bus/micro.csv",),
     "筹码+散户资金双采（2026-10-28 · 课题 E 重启，旧系统 E2/E3 立项零样本的空地）："
     "westock chip（avg_cost/conc70/conc90/profit_rate）+ fund flow（RetailIn/Out/Small/Mid/Main/Jumbo）；"
     "append-only 长表 + (code,date) 分析侧去重；进度文件支持断点续采。"),
    ("fetch_micro.py", "save_progress", ("data/state/_fetch_micro_progress.json",),
     "采集断点进度（已采码集合）——断点续采用；tmp+replace 原子写；"
     "不属业务数据，落 state 台账区。"),
    ("env_recalc.py", "write_csv", ("data/research/env_recalc/",),
     "封板池口径环境指标重算器（2026-10-27 · 环境闸统一 M1 配套）：从 klines 全历史离线"
     "重算校准训练集（统计等价口径：涨幅过阈+收盘≈最高，继承旧系统 limitup_rebuild 哲学）；"
     "口径与已知特性（除权日漏判=口径的一部分）见模块 docstring 与"
     "data/research/env_gate_audit_20261027/DECISION.md；out 路径由调用方注入"
     "（同 strategy_matrix --out 先例）⇒ 静态不可解析，须登记；"
     "落点 = X2 研究豁免区（校准证据，非知识落点）"),
    ("replay.py", "save_state", ("data/research/replay/",),
     "回放引擎 v2（2026-10-27 · 四层分离重设计，取代 step_replay v1——用户裁定"
     "「理解旧系统理念后重新设计，非照抄」）：World Feed / Policy（mech_v0 纯函数）/"
     "Executor（T+1·涨跌停拒单·−8% 止损兜底，所有 policy 共用）/ Audit 三路反推"
     "（逐笔归因·闸门反事实·环境档分组CI）。状态持久化 state.json 可中断续跑；"
     "feed/exec 版本钉扎 ⇒ V0/V1 compare 有硬判据；"
     "路径由 BT_DIR(btid) 派生 ⇒ 静态不可解析，须登记；tmp + os.replace 原子写；"
     "落点 = X2 研究豁免区（回测审计证据，产出禁直接改生产规则）"),
    ("replay.py", "write_result", ("data/research/replay/",),
     "期末归档 result.json（指标/交割单/净值曲线/版本钉扎）——与 save_state 同目录，"
     "path 由 BT_DIR(btid) 派生 ⇒ 静态不可解析，须登记"),
    ("info_feeder.py", "write_snapshot", ("data/raw/info/",),
     "信息面统一采集器（2026-10-27 · 补齐旧 news skill 14 项覆盖矩阵）：快讯/热文/"
     "热搜红灯映射/事件日历/外盘/公告分级，westock 一手通道；契约=只采集归档+池映射，"
     "解读归 S1/event-analysis；文件名 info_<date>_<hhmm>.json 动态 ⇒ 静态不可解析，"
     "须登记；tmp+replace 原子写；落点 = 原始采集区"),
    ("event_study.py", "write_report", ("data/research/event_study/",),
     "事件研究（2026-10-27 · 验证「消息→股价」传导链条件性）：公告样本×klines "
     "事件包→分类/位置/情绪/缺口/环境五维调制，对照 F7 基准表独立复核；"
     "events.csv + REPORT.json 动态路径 ⇒ 静态不可解析，须登记；"
     "落点 = X2 研究豁免区"),
    ("info_feeder.py", "incremental_filter", ("data/raw/info/",),
     "快讯增量基线 news_meta.json（已见 id 集合，--incremental 只出新快讯；"
     "旧 news_lib 同机制）——tmp+replace 原子写"),
    ("info_feeder.py", "write_redlight", ("data/raw/info/",),
     "信息面可读版 redlight_<date>.md（红灯=热搜×候选池/持仓交集 + 快讯 Top + "
     "未来事件日历）——盘前预案③段与盘中扫描②段的直接数据源；同上登记"),
    ("premarket_card.py", "write_card", ("data/generated/reports/",),
     "盘前预案生成器（E1 升级件 2026-10-27）：L0 六行 + ①~⑫ 段全机械产出"
     "（外盘期货/大宗/板块光谱/风险预算情景/池内分态），十二段数据源状态写死"
     "（③ 事件 GAP 如实标注）；预案≠决策，零 BUY/WAIT/NO；"
     "文件名 premarket_card_<date>.md ⇒ 静态不可解析，须登记；tmp+replace 原子写"),
    ("replay.py", "write_audit", ("data/research/replay/",),
     "反推平面归档 audit.json（逐笔退出归因 / 闸门反事实按环境档分组 / "
     "分组 n·胜率·净期望·95%CI，n<20 标 INSUFFICIENT）——同目录派生路径，"
     "须登记；audit 产出=研究证据，接 hypothesize/verify 闭环，禁直接改生产参数"),
    ("channel_health.py", "mark_mcp_ok", ("data/state/channel_mcp_ok.json",),
     "MCP 腿健康时间戳写点（2026-11-02 通道判真）：tmp + os.replace 原子换入；"
     "落点 = X5 豁免区（运行状态，非知识）"),
    ("init_state.py", "_w_json", ("data/state/",),
     "干净机器一键建状态骨架（2026-11-02）：path 由调用方给，恒落 data/state/；"
     "tmp + os.replace 原子写；落点 = X5 豁免区（运行状态）"),
    ("init_state.py", "_w_empty_lines", ("data/state/",),
     "空账本骨架建点（2026-11-02）：登账 jsonl 建空文件（显式空 ≠ 省略），path 由调用方给；"
     "落点 = X5 豁免区（运行状态）"),
    ("fetch_regulatory.py", "main", ("data/raw/<date>/",),
     "交易所监管 + 股东行为日采（2026-11-02 信息面补齐 P0）：落 regulatory.jsonl / "
     "holder_behavior.jsonl，rd 由 _raw_dir(date) 派生 ⇒ 静态不可解析，须登记；"
     "落点 = X5 豁免区（原始数据）"),
    ("earnings_research.py", "main", ("data/research/earnings/",),
     "业绩研究证据包（2026-11-02 业绩推理）：五腿证据包落 OUT_DIR，仅 --execute 才写；"
     "落点 = X2 研究豁免区（证据，不自动进决策链）"),
    ("mainline_scan.py", "main", ("data/generated/reports/",),
     "主升浪/产业趋势扫描产物（2026-11-02）：五维机械打分 + 梯队证据，仅 --execute 才写 md；"
     "落点 = X1 交付物（给人看，非知识落点）"),
)

# 代码目录（执行层 Python；不属知识落点）
CODE_DIR_PREFIX = "tools/"
ANCHORS = ("data", "memory", "tools", ".workbuddy")

_WRITE_MODES = ("w", "w+", "a", "a+", "x", "x+", "r+")
# 仅 os.* / shutil.* 上的这些方法算「写落点 sink」（防 str.replace / dict.copy 误判）
_SINK_ATTRS = {"replace": 1, "rename": 1, "copy": -1, "copy2": -1, "copyfile": -1, "move": -1}
_TOOLS_DIR = os.path.join(ROOT, "tools")


class LandingError(Exception):
    """落点审计自身出错（扫描失败 / 目标不存在）——显式抛错，不静默给结论。"""


# ── 归类 ─────────────────────────────────────────────────────────────────────
def _to_regex(pattern):
    """把落点/豁免模式转成正则：`<xxx>` = 单段通配；结尾 `/` = 目录前缀。"""
    pre = pattern.endswith("/")
    body = pattern[:-1] if pre else pattern
    rx = re.sub(r"<[a-z_]+>", "[^/]+", re.escape(body))
    return re.compile("^" + rx + ("(/|$)" if pre else "$"))


_RULES = []
for _l in LANDINGS:
    for _p in (_l["pattern"],) + tuple(_l.get("also") or ()):
        _RULES.append(("LANDING", _l["id"], _p, _to_regex(_p)))
for _x in EXEMPTIONS:
    for _p in _x["patterns"]:
        _RULES.append(("EXEMPT", _x["id"], _p, _to_regex(_p)))
_RULES.append(("EXEMPT", "X5", CAPS_DIR_PATTERN, _to_regex(CAPS_DIR_PATTERN)))

_ALL_PATTERNS = tuple(p for _, _, p, _ in _RULES)

# 能力 id 出现的位置 → 从相对路径里抽 cap 引用
_CAP_REF_RULES = (
    (re.compile(r"^data/generated/s_([A-Za-z0-9]+)_"), "S 输出快照"),
    (re.compile(r"^data/knowledge/([^/]+)/"), "S 知识库"),
    (re.compile(r"^\.workbuddy/skills/([^/]+)/"), "能力落点目录"),
    (re.compile(r"^data/generated/reports/(e[0-9]+)_"), "E 系列交付物"),
)


def normalize(rel):
    """规范化相对路径：统一斜杠、去 `./`、去尾部 `/`（保留根）。"""
    s = str(rel).replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    s = re.sub(r"/+", "/", s)
    return s.rstrip("/") if s != "/" else s


def classify(rel):
    """把项目相对路径归类。返回 {rel, kind, landings, exemptions, containers, cap_refs}。

    kind: LANDING（至少命中 1 个落点）/ EXEMPT / CONTAINER（是某模式的目录前缀，或代码目录）
          / UNKNOWN（第 6 类，唯一违例形态）
    """
    r = normalize(rel)
    hits_l, hits_x, hits_p = [], [], []
    for kind, rid, pat, rx in _RULES:
        if rx.match(r):
            (hits_l if kind == "LANDING" else hits_x).append(rid)
    for p in _ALL_PATTERNS:
        if p.startswith(r + "/"):
            hits_p.append(p)
    caps = []
    for rx, why in _CAP_REF_RULES:
        m = rx.match(r)
        if m:
            caps.append({"cap": m.group(1), "why": why})
    if hits_l:
        kind = "LANDING"
    elif hits_x:
        kind = "EXEMPT"
    elif hits_p or r == "tools" or r.startswith(CODE_DIR_PREFIX):
        kind = "CONTAINER"
    else:
        kind = "UNKNOWN"
    return {"rel": r, "kind": kind, "landings": sorted(set(hits_l)),
            "exemptions": sorted(set(hits_x)), "containers": sorted(set(hits_p)),
            "cap_refs": caps}


def landing_ids():
    return tuple(l["id"] for l in LANDINGS)


def exemption_ids():
    return tuple(x["id"] for x in EXEMPTIONS)


# ── 生产代码区（剔除自检函数整段：夹具写临时目录，不属系统落点） ──────────────
def _external_names(root):
    """全库「被别的模块调用的名字」——跨模块入口不得被判成测试夹具。

    单模块内看不出来：`ingest_snapshot` 在本模块只被 `_selftest` 调用，却由 settle.py 调用。
    故先做一遍全库名字表，再据此排除。
    """
    ext = set()
    for p in _py_files(root):
        src = io.open(p, encoding="utf-8").read()
        tree = ast.parse(src)
        defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                nm = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                if nm and nm not in defined:
                    ext.add(nm)
    return ext


def _test_only_fns(tree, seeds=("_selftest",), ext=()):
    """只被自检调用的顶层函数（含传递闭包）——夹具不参与落点审计。

    判据：f 的所有调用者都已是 test-only，则 f 也是 test-only；根 = `seeds`。
    豁免（恒视为生产代码，永不置空）：
      · 被其它模块引用的名字（`ext`，外部入口）；
      · 无调用者的悬空入口；
      · **非下划线开头的公开名** —— 骨架期「尚未接线」不等于「是夹具」：
        `upsert_candidate` / `promote` / `resolve` 这一层 API 现在只被 `_selftest` 直接调用，
        但它们是 F7 装配要用的正式入口，若按调用图判成夹具就会把整个模块的写点审计
        **静默屏蔽**（本审计器最危险的自身缺陷形态：看起来全绿，其实没看）。
    故夹具只可能是下划线私有名；公开名一律进审计。
    """
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    names = {n.name for n in fns}
    calls = {}
    for n in fns:
        calls[n.name] = {(getattr(c.func, "id", None) or getattr(c.func, "attr", None))
                         for c in ast.walk(n) if isinstance(c, ast.Call)} & names
    callers = {x: set() for x in names}
    for f, cs in calls.items():
        for c in cs:
            callers[c].add(f)
    test = set(seeds) & names
    changed = True
    while changed:
        changed = False
        for f in sorted(names - test):
            if f in ext or not callers[f] or not f.startswith("_"):
                continue
            if callers[f] <= test:
                test.add(f)
                changed = True
    return test


def _module_test_seeds(tree, default=("_selftest",)):
    """从模块**自身**声明的 `_TEST_FUNCS` 常量读测试入口（唯一清单，禁在审计器里复制一份）。

    各模块自己声明哪些函数是「测试/自检入口」（写的是临时沙盒，不属系统落点）：
    `wakeup.py` 写 `_TEST_FUNCS = ("_selftest", "_acceptance", "_discipline")`。
    审计器若把种子硬编码成 `("_selftest",)`，就会把 `wakeup._acceptance` 当成**生产代码**，
    于是它调用的沙盒夹具（`_init_pool` / `_put_raw` / `_init_cursor`）被误判成真写点
    （`UNDECLARED_WRITE`）—— 与 F6「夹具判别」同族的**口径不一致**缺陷。
    故**派生而非复制**：模块声明几位，审计器就认几位；未声明者回退 `default`。
    """
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name) \
                and n.targets[0].id == "_TEST_FUNCS":
            v = n.value
            if isinstance(v, (ast.Tuple, ast.List)):
                got = tuple(e.value for e in v.elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str))
                if got:
                    return got
    return tuple(default)


def prod_lines(path, skip=None, ext=()):
    """返回该文件的 (源码行列表, 被剔除的行号集合)。

    **自检夹具（`_selftest` 及其测试专用辅助函数）整段置空后再解析**：夹具写的是临时
    目录、造的是假树，不属系统落点（与各模块 `--discipline` 的「覆盖写只在自检内」同口径）。
    种子按模块自报的 `_TEST_FUNCS` 派生（未声明则回退 `_selftest`），**不在这里硬编码一份**。
    """
    src = io.open(path, encoding="utf-8").read()
    lines = src.splitlines()
    tree = ast.parse(src)
    seeds = _module_test_seeds(tree) if skip is None else tuple(skip)
    drop = set(seeds) | _test_only_fns(tree, seeds, ext)
    masked = set()
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in drop:
            # 尾界（含多行表达式续行）= `astwin.line_set` 唯一实现；本处原来的
            # 「用 end_lineno 而非 max(lineno)」说明与 F7 的 IndentationError 来历
            # 已随唯一实现迁到 `tools/fs/astwin.py` 模块 docstring（落点唯一化）。
            masked |= ASTW.line_set(n, clamp=len(lines))
    for i in masked:
        lines[i - 1] = ""
    return lines, masked


def _py_files(root):
    out = []
    base = os.path.join(root, "tools")
    if not os.path.isdir(base):
        raise LandingError("执行层目录不存在：%s" % base)
    for dp, _dn, fn in os.walk(base):
        for f in sorted(fn):
            if f.endswith(".py"):
                out.append(os.path.join(dp, f))
    return sorted(out)


def _join_parts(call):
    """os.path.join(...) 的静态片段：常量 → 原值；Name/Attribute → <name>；其余 → <dyn>。"""
    parts = []
    for a in call.args:
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            parts.append(a.value)
        elif isinstance(a, ast.Attribute):
            parts.append("<%s>" % a.attr)
        elif isinstance(a, ast.Name):
            parts.append("<%s>" % a.id)
        elif isinstance(a, ast.BinOp) and isinstance(a.op, ast.Mod):
            parts.append("<dyn>")
        elif isinstance(a, ast.JoinedStr):
            parts.append("<dyn>")
        else:
            parts.append("<dyn>")
    idx = next((i for i, x in enumerate(parts) if x in ANCHORS), None)
    if idx is None:
        return None
    return normalize("/".join(parts[idx:]))


def _module_consts(tree):
    """模块级常量 → 静态相对路径（仅可解析者）。"""
    out = {}
    for n in tree.body:
        if not isinstance(n, ast.Assign) or len(n.targets) != 1:
            continue
        t = n.targets[0]
        if not isinstance(t, ast.Name):
            continue
        v = n.value
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            if _is_path_literal(v.value):
                out[t.id] = normalize(v.value)
        elif isinstance(v, ast.Call) and getattr(v.func, "attr", None) == "join":
            r = _join_parts(v)
            if r:
                out[t.id] = r
    return out


def _is_path_literal(s):
    """是否「项目相对路径」字面量：恰为锚点，或锚点 + `/`（防 data_source 之类误判）。"""
    return any(s == a or s.startswith(a + "/") for a in ANCHORS)


def vocabulary(root=None, ext=None):
    """全库生产代码区的「项目相对路径」词汇。返回 {rel: [file, ...]}。"""
    root = root or ROOT
    ext = _external_names(root) if ext is None else ext
    seen = {}
    for p in _py_files(root):
        lines, masked = prod_lines(p, ext=ext)
        base = os.path.basename(p)
        for n in ast.walk(ast.parse("\n".join(lines))):
            rel = None
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "join":
                rel = _join_parts(n)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                if _is_path_literal(n.value):
                    rel = normalize(n.value)
            if rel:
                seen.setdefault(rel, [])
                if base not in seen[rel]:
                    seen[rel].append(base)
    return seen


def write_sites(root=None, ext=None):
    """全库生产代码区的写 sink（可解析 / 不可解析分开）。"""
    root = root or ROOT
    ext = _external_names(root) if ext is None else ext
    sites = []
    for p in _py_files(root):
        lines, masked = prod_lines(p, ext=ext)
        src = "\n".join(lines)
        tree = ast.parse(src)
        consts = _module_consts(tree)
        fns = ASTW.func_table(tree)       # 函数区间表 = astwin 唯一实现（尾界含多行尾部）
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            attr = getattr(n.func, "attr", None)
            name = getattr(n.func, "id", None)
            mod = getattr(getattr(n.func, "value", None), "id", None)
            target, mode = None, None
            if attr == "open" or name == "open":
                for a in n.args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        mode = a.value
                for kw in n.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = kw.value.value
                if mode not in _WRITE_MODES or not n.args:
                    continue
                target = n.args[0]
            elif mod in ("os", "shutil") and attr in _SINK_ATTRS:
                i = _SINK_ATTRS[attr]
                if not n.args:
                    continue
                target = n.args[i]
                mode = "%s.%s" % (mod, attr)
            else:
                continue
            owner = ASTW.owner_of(fns, n.lineno)
            rel, how = _resolve(target, consts)
            sites.append({"file": os.path.relpath(p, root).replace("\\", "/"),
                          "line": n.lineno, "fn": owner, "mode": mode,
                          "resolved": rel, "how": how,
                          "expr": ast.unparse(target)[:60]})
    return sites


def _resolve(node, consts):
    """把写目标表达式解析成相对路径 → (rel|None, 说明)。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if _is_path_literal(node.value):
            return normalize(node.value), "literal"
        return None, "non-project-literal"
    if isinstance(node, ast.Name):
        if node.id in consts:
            return consts[node.id], "module-const:%s" % node.id
        return None, "unresolved-name:%s" % node.id
    if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "join":
        r = _join_parts(node)
        return (r, "join") if r else (None, "join-without-anchor")
    if isinstance(node, ast.BoolOp):
        for v in node.values:
            r, how = _resolve(v, consts)
            if r:
                return r, "boolop:" + how
        return None, "boolop-unresolved"
    if isinstance(node, ast.BinOp):
        r, how = _resolve(node.left, consts)
        if r:
            return r, "binop-left"
        r, how = _resolve(node.right, consts)
        return (r, "binop-right") if r else (None, "binop-unresolved")
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        return None, "attr/subscript"
    return None, type(node).__name__


# ── 审计 ─────────────────────────────────────────────────────────────────────
def audit(root=None, registry_path=None, declared=None):
    """三条判据一起跑。返回报告；`ok = not violations`。

    `declared` 默认取 WRITE_ENTRYPOINTS；可注入以自检「悬空登记 / 目标越界」两类负例。
    """
    root = root or ROOT
    ext = _external_names(root)
    voc = vocabulary(root, ext)
    violations = []

    # ① 路径词汇封闭
    unknown = []
    for rel in sorted(voc):
        c = classify(rel)
        if c["kind"] == "UNKNOWN":
            unknown.append({"rel": rel, "files": voc[rel]})
            violations.append("UNKNOWN_PATH %s（出现在 %s）—— 第 6 类落点"
                              % (rel, ", ".join(voc[rel])))

    # ② 写点目标封闭
    sites = write_sites(root, ext)
    declared = {(a[0], a[1]): a for a in (WRITE_ENTRYPOINTS if declared is None else declared)}
    undeclared, matched, bad_targets = [], set(), []
    for s in sites:
        key = (os.path.basename(s["file"]), s["fn"])
        if key in declared:
            matched.add(key)
        if s["resolved"]:
            c = classify(s["resolved"])
            if c["kind"] == "UNKNOWN":
                violations.append("UNKNOWN_WRITE %s:%d %s() → %s"
                                  % (s["file"], s["line"], s["fn"], s["resolved"]))
        else:
            if key not in declared:
                undeclared.append(s)
                violations.append("UNDECLARED_WRITE %s:%d %s() → %s（%s）—— "
                                  "新增写路径必须在 WRITE_ENTRYPOINTS 登记"
                                  % (s["file"], s["line"], s["fn"], s["expr"], s["how"]))

    # ②b 登记表反查：目标必须可归类；登记不得悬空（函数改名 / 删除后自动暴露）
    for key, ent in sorted(declared.items()):
        for t in ent[2]:
            cc = classify(t)
            if cc["kind"] not in ("LANDING", "EXEMPT"):
                bad_targets.append({"entry": key, "target": t, "kind": cc["kind"]})
                violations.append("WRITE_TARGET_UNKNOWN 登记项 %s.%s 声明的目标 %s 不属 5 落点/5 豁免"
                                  % (key[0], key[1], t))
    stale = [{"entry": k, "note": declared[k][3]} for k in sorted(declared) if k not in matched]
    for s2 in stale:
        violations.append("STALE_DECLARATION %s.%s 已登记但真库中无对应写点（函数改名/删除后须同步删登记）"
                          % (s2["entry"][0], s2["entry"][1]))

    # ③ 注册表闭环
    reg = _load_registry(registry_path)
    reg_ids = {c["id"] for c in reg.get("capabilities", []) if isinstance(c, dict)}
    reg_rep = {"registry": reg_ids and len(reg_ids) or 0, "outputs_checked": 0,
               "bad_output_path": [], "bad_cap_ref": [], "registered": sorted(reg_ids)}
    for c in reg.get("capabilities", []):
        if not isinstance(c, dict):
            continue
        op = c.get("output_path")
        if not op:
            continue
        reg_rep["outputs_checked"] += 1
        base = normalize(str(op).split("#")[0])
        cc = classify(base)
        if cc["kind"] == "UNKNOWN":
            reg_rep["bad_output_path"].append({"cap": c.get("id"), "output_path": base})
            violations.append("OUTPUT_NOT_LANDING %s 的 output_path=%s 既不属 5 落点也不属 5 豁免"
                              % (c.get("id"), base))
    for rel in sorted(voc):
        for ref in classify(rel)["cap_refs"]:
            cap = ref["cap"]
            want = cap.upper() if cap[1:].isdigit() else cap   # e<N>/s<N> 小写形态一律按大写 cap 归一（s1 特例）
            if cap not in reg_ids and want not in reg_ids:
                reg_rep["bad_cap_ref"].append({"rel": rel, "cap": cap, "why": ref["why"]})
                violations.append("UNREGISTERED_CAP_OUTPUT %s 引用了未登记的能力 %r（%s）—— "
                                  "注册表之外的能力输出不得成为落点" % (rel, cap, ref["why"]))
    return {"scanned": len(_py_files(root)), "paths": len(voc), "unknown": unknown,
            "sites": sites, "write_sites": len(sites),
            "indirect": [s for s in sites if not s["resolved"]],
            "undeclared_write": undeclared, "registry": reg_rep,
            "declared": len(declared), "declared_matched": len(matched),
            "stale_declaration": stale, "bad_write_target": bad_targets,
            "violations": violations, "ok": not violations}


def _load_registry(registry_path=None):
    p = registry_path or REG.REGISTRY_PATH
    if not os.path.exists(p):
        return {"capabilities": []}
    with io.open(p, encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict):
        raise LandingError("注册表顶层必须是对象：%s" % p)
    return doc


# ── 自检 ─────────────────────────────────────────────────────────────────────
def _selftest():
    import shutil
    import tempfile
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

    tmp = tempfile.mkdtemp(prefix="ta_landing_selftest_")
    try:
        print("== A. 契约：5 落点 + 5 豁免 ==")
        T("A1 落点数 = 5，id 唯一且为 LD1~LD5（前缀非 L，避归因五维 L1~L5 撞名）",
          len(LANDINGS) == 5 and landing_ids() == ("LD1", "LD2", "LD3", "LD4", "LD5"))
        # ★ 2026-10-19 改：原断言硬编码「=5 / X1~X5」，新增 X6（外部判断输入 proposals）
        #   后必然红。豁免**数量会随落点需求增长**，故断言改为「id 唯一 + 连续编号 + X1~X5 在位」
        #   —— 保留原意（编号体系完整、无重复、无跳号），不再锁死数量。
        _ex_ids = exemption_ids()
        T("A2 豁免 id 唯一、连续编号、X1~X5 在位（数量随落点需求增长）",
          len(set(_ex_ids)) == len(_ex_ids)
          and _ex_ids == tuple("X%d" % (i + 1) for i in range(len(_ex_ids)))
          and "X1" in _ex_ids and "X5" in _ex_ids, _ex_ids)
        T("A3 每条有判据 + 归属条目（无空话条目）",
          all(l["judge"] and l["item"] for l in LANDINGS)
          and all(x["judge"] and x["item"] for x in EXEMPTIONS))
        T("A4 落点模式无重叠（同一条路径不会同时是两个落点）",
          _no_overlap([l["pattern"] for l in LANDINGS]
                      + [p for l in LANDINGS for p in (l.get("also") or ())]))

        print("== B. 归类正确（含轴分流与双身份） ==")
        cases = [
            ("data/generated/decision_ledger.jsonl", "LANDING", "LD1"),
            ("data/generated/sim/decision_ledger.jsonl", "LANDING", "LD1"),
            ("data/generated/pnl_ledger.jsonl", "LANDING", "LD2"),
            ("data/generated/sim/pnl_ledger.jsonl", "LANDING", "LD2"),
            ("data/params.json", "LANDING", "LD3"),
            ("data/params_changelog.jsonl", "LANDING", "LD3"),
            ("data/calibration_queue.jsonl", "LANDING", "LD4"),
            ("data/decision_card.md", "LANDING", "LD5"),
            ("data/generated/reports/e1_2026-10-08.md", "EXEMPT", "X1"),
            ("data/research/R-2026-001/notes.md", "EXEMPT", "X2"),
            ("data/generated/s_S3_2026-10-08.json", "EXEMPT", "X3"),
            ("data/knowledge/s3/kb.json", "EXEMPT", "X4"),
            ("data/state/cursor.json", "EXEMPT", "X5"),
            ("data/state/candidates.json", "EXEMPT", "X5"),
            ("memory/state/thesis.md", "EXEMPT", "X5"),
            ("data/snapshot/2026-10-08/daily_quote.csv", "EXEMPT", "X5"),
            ("data/raw/2026-10-08/news.jsonl", "EXEMPT", "X5"),
            ("data/generated/h_board.md", "EXEMPT", "X5"),
            ("data/learning/demand_queue.jsonl", "EXEMPT", "X5"),
            ("data/generated/cases.jsonl", "EXEMPT", "X5"),
            (".workbuddy/memory/MEMORY.md", "EXEMPT", "X5"),
            (".workbuddy/skills/s3/SKILL.md", "EXEMPT", "X5"),
            ("data", "CONTAINER", None),
            ("data/generated", "CONTAINER", None),
            ("tools", "CONTAINER", None),
            ("tools/fs/loader.py", "CONTAINER", None),
            ("data/misc/whatever.txt", "UNKNOWN", None),
            ("research/scratch.md", "UNKNOWN", None),
        ]
        bad = []
        for rel, kind, rid in cases:
            c = classify(rel)
            ok_kind = c["kind"] == kind
            ok_id = (rid is None) or (rid in c["landings"] or rid in c["exemptions"])
            if not (ok_kind and ok_id):
                bad.append("%s → %s/%s" % (rel, c["kind"], c["landings"] + c["exemptions"]))
        T("B1 27 条归类样例全对（含 SIM 分轴、changelog 同族、双身份、h_board 看板、容器、越界）",
          not bad, "错: %s" % bad)
        c = classify("data/decision_card.md")
        T("B2 decision_card 双身份：既是落点⑤（驱动依据区）又在豁免 X5（0.15 载体）",
          "LD5" in c["landings"] and "X5" in c["exemptions"])
        T("B3 能力产物路径可抽出 cap 引用",
          classify("data/generated/s_S3_2026-10-08.json")["cap_refs"][0]["cap"] == "S3"
          and classify("data/knowledge/s3_emotion/kb.json")["cap_refs"][0]["cap"] == "s3_emotion"
          and classify(".workbuddy/skills/S4/SKILL.md")["cap_refs"][0]["cap"] == "S4"
          and classify("data/generated/reports/e7_2026-10-08.md")["cap_refs"][0]["cap"] == "e7")

        print("== C. 真库审计（生产代码区） ==")
        rep = audit()
        T("C1 真库零违例（路径词汇全部可归类、写点目标全部可归类/已登记）",
          rep["ok"], "违例: %s" % rep["violations"][:5])
        T("C2 生产代码区已扫（≥10 文件、≥8 条路径词汇）",
          rep["scanned"] >= 10 and rep["paths"] >= 8)
        T("C3 未登记写点 = 0（WRITE_ENTRYPOINTS 是完备声明表）",
          len(rep["undeclared_write"]) == 0)
        T("C4 _selftest 夹具不参与审计（自检造出的 tools/bad2.py 等不入词汇）",
          not any("bad2" in k or "s3_emotion_structure" in k for k in vocabulary()))
        T("C5 登记表反查：无悬空登记（STALE_DECLARATION = 0）",
          not rep["stale_declaration"],
          "悬空: %s" % [s["entry"] for s in rep["stale_declaration"]])
        T("C6 登记表反查：每条登记目标均可归类（WRITE_TARGET_UNKNOWN = 0）",
          not rep["bad_write_target"])
        T("C7 登记表被真库写点逐条命中（登记数 = 命中数，无「写了不用」）",
          rep["declared"] == rep["declared_matched"] and rep["declared"] >= 10)
        T("C8 公开 API 不被夹具判别吞掉（写点里含未接线的正式入口原名）",
          {"candidates.py._save", "params.py._write_params", "params.py._append_jsonl"}
          <= {os.path.basename(s["file"]) + "." + s["fn"] for s in rep["sites"]})
        T("C9 测试入口种子派生自模块自报 `_TEST_FUNCS`（不硬编码第二份）；"
          "wakeup 的 `_acceptance` 沙盒夹具写点不入审计",
          _module_test_seeds_case())
        T("C10 被置空函数以多行表达式结尾 → 尾部续行一并置空（end_lineno，防 IndentationError）",
          _multiline_tail_masked_case(tmp))

        print("== D. 注册表闭环（用户点名：注册表之外的能力输出不得成为落点） ==")
        T("D1 全部能力 output_path 均归类进 5 落点 / 5 豁免",
          not rep["registry"]["bad_output_path"]
          and rep["registry"]["outputs_checked"] >= 20)
        T("D2 代码里的能力产物引用全部已登记（无幽灵输出）",
          not rep["registry"]["bad_cap_ref"])
        T("D3 负例：未登记能力 id 的能力产物 → UNREGISTERED_CAP_OUTPUT 被抓",
          _unregistered_case(tmp))
        T("D4 负例：新增第 6 类落点路径 → UNKNOWN_PATH 被抓",
          _unknown_path_case(tmp))
        T("D5 负例：未声明的写路径 → UNDECLARED_WRITE 被抓",
          _undeclared_write_case(tmp))
        T("D6 负例：output_path 写成非落点 → OUTPUT_NOT_LANDING 被抓",
          _bad_output_case(tmp))
        T("D7 负例：登记悬空（登记了不存在的写点）→ STALE_DECLARATION 被抓",
          _stale_declaration_case(tmp))
        T("D8 负例：登记目标越界 → WRITE_TARGET_UNKNOWN 被抓",
          _bad_write_target_case(tmp))
        T("D9 反向负例：真夹具（私有名、仅 _selftest 调）被正确置空，不误报",
          _private_fixture_masked_case(tmp))

        print("== E. 纪律 ==")
        src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
        T("E1 无网络/子进程导入（审计器不取外部数据）",
          not re.search(r"^\s*(import|from)\s+(requests|urllib|socket|subprocess|openai)\b",
                        src, re.M))
        T("E2 本文件只读审计：生产区（剔自检夹具）无任何写盘/改名/删除调用",
          not _prod_write_calls())
        T("E3 落点清单只在本文件维护（真库生产代码中无第二份 LD1~LD5 / X1~X5 字面量）",
          _single_source_of_truth())
        T("E4 夹具判别永不吞公开名（非下划线名恒进审计，含跨模块自检）",
          not _public_name_leaks())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：%s" % " / ".join(fails))
    return 1 if fail else 0


def _no_overlap(patterns):
    """落点模式两两不重叠（否则同一路径归属不清）。"""
    for i, a in enumerate(patterns):
        for b in patterns[i + 1:]:
            ra, rb = _to_regex(a), _to_regex(b)
            for probe in (a, b):
                if ra.match(probe) and rb.match(probe):
                    return False
    return True


def _fake_tree(tmp, files, registry=None):
    """造一棵临时项目树（tools/ + 注册表），供负例使用。"""
    import tempfile
    root = tempfile.mkdtemp(dir=tmp)
    os.makedirs(os.path.join(root, "tools", "x"), exist_ok=True)
    for name, body in files.items():
        with io.open(os.path.join(root, "tools", "x", name), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write(body)
    rp = os.path.join(root, "registry.json")
    with io.open(rp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(registry if registry is not None else
                  {"registry_version": "1", "capabilities": [{"id": "S3"}]}, f)
    return root, rp


def _unregistered_case(tmp):
    import tempfile as _t
    root, rp = _fake_tree(tmp, {"a.py":
                                "import os\nROOT = '.'\n"
                                "P = os.path.join(ROOT, 'data', 'knowledge', 's99_ghost', 'kb.json')\n"})
    try:
        r = audit(root=root, registry_path=rp, declared=())
        return any(v.startswith("UNREGISTERED_CAP_OUTPUT") for v in r["violations"])
    finally:
        _rm(root)


def _unknown_path_case(tmp):
    root, rp = _fake_tree(tmp, {"a.py":
                                "import os\nROOT = '.'\n"
                                "P = os.path.join(ROOT, 'data', 'misc', 'scratch.txt')\n"})
    try:
        r = audit(root=root, registry_path=rp, declared=())
        return any(v.startswith("UNKNOWN_PATH") for v in r["violations"])
    finally:
        _rm(root)


def _undeclared_write_case(tmp):
    """公开名（非下划线）即使**只被 _selftest 调用**也必须进审计。

    这正是本审计器最容易自欺的形态：骨架期 API 尚未接线，若按调用图判成夹具，
    整个模块的写点就被静默屏蔽（全绿 = 没看）。故此处夹具故意只由 `_selftest` 调。
    """
    root, rp = _fake_tree(tmp, {"a.py":
                                "def save(path=None):\n"
                                "    with open(path, 'w') as f:\n"
                                "        f.write('x')\n\n\n"
                                "def _selftest():\n"
                                "    save('x')\n"})
    try:
        r = audit(root=root, registry_path=rp, declared=())
        return any(v.startswith("UNDECLARED_WRITE") for v in r["violations"])
    finally:
        _rm(root)


def _private_fixture_masked_case(tmp):
    """反向负例：真正的夹具（下划线私有名、仅被 _selftest 调用）必须被置空，不误报。"""
    root, rp = _fake_tree(tmp, {"a.py":
                                "def _make(path=None):\n"
                                "    with open(path, 'w') as f:\n"
                                "        f.write('x')\n\n\n"
                                "def _selftest():\n"
                                "    _make('x')\n"})
    try:
        r = audit(root=root, registry_path=rp, declared=())
        return not r["violations"]
    finally:
        _rm(root)


def _module_test_seeds_case():
    """C9：审计器认模块**自报**的测试入口（`_TEST_FUNCS`），不硬编码种子。

    真库实证：`wakeup.py` 声明三位测试入口（`_selftest` / `_acceptance` / `_discipline`）。
    审计器若只认 `_selftest`，则 `_acceptance` 被当生产代码 → 它调用的沙盒夹具
    （`_init_pool` / `_put_raw` / `_init_cursor`）被误判成真写点（`UNDECLARED_WRITE`）。
    同时验证「未自报者回退 `_selftest`」（landing.py 自身即未声明 `_TEST_FUNCS`）。
    """
    wp = os.path.join(ROOT, "tools", "fs", "wakeup.py")
    seeds = _module_test_seeds(ast.parse(io.open(wp, encoding="utf-8").read()))
    if "_acceptance" not in seeds or "_selftest" not in seeds:
        return False
    lp = os.path.join(ROOT, "tools", "fs", "landing.py")
    if _module_test_seeds(ast.parse(io.open(lp, encoding="utf-8").read())) != ("_selftest",):
        return False
    fixture = {"_sandbox", "_init_pool", "_init_cursor", "_put_raw", "_fp"}
    bad = {s["fn"] for s in write_sites(ROOT, _external_names(ROOT))
           if os.path.basename(s["file"]) == "wakeup.py"} & fixture
    return not bad


def _multiline_tail_masked_case(tmp):
    """C10：被置空函数若以多行表达式结尾，尾部续行必须一并置空。

    旧实现用子节点 `lineno` 取 max：多行调用（如 `f.write({...})`）的 Call/Dict 节点
    `lineno` 只到**起始行**，漏掉 `})` 续行 → 残留缩进行 → `IndentationError`
    （F7 收口实测暴露）。改用 `end_lineno` 后整段覆盖。
    """
    root, rp = _fake_tree(tmp, {"a.py":
                                "def _make(path=None):\n"
                                "    with open(path, 'w') as f:\n"
                                "        f.write({\n"
                                "            'k': 1,\n"
                                "        })\n\n\n"
                                "def _selftest():\n"
                                "    _make('x')\n"})
    try:
        lines, masked = prod_lines(os.path.join(root, "tools", "x", "a.py"), ext=())
        ast.parse("\n".join(lines))                 # 置空后必须仍是合法 Python
        return all(ln.strip() == "" for ln in lines[:5])   # 函数体含续行整段被覆盖
    finally:
        _rm(root)


def _stale_declaration_case(tmp):
    """负例：登记了真库中并不存在的写点（函数改名/删除后没同步）→ STALE_DECLARATION。"""
    root, rp = _fake_tree(tmp, {"a.py":
                                "def _save(path=None):\n"
                                "    with open(path, 'w') as f:\n"
                                "        f.write('x')\n"})
    decl = (("a.py", "_save", ("data/params.json",), "真"),
            ("a.py", "_gone_save", ("data/params.json",), "假（函数已不存在）"))
    try:
        r = audit(root=root, registry_path=rp, declared=decl)
        return (any(v.startswith("STALE_DECLARATION") and "_gone_save" in v
                    for v in r["violations"])
                and not any("UNDECLARED_WRITE" in v for v in r["violations"]))
    finally:
        _rm(root)


def _bad_write_target_case(tmp):
    """负例：登记项声明的目标本身不属 5 落点/5 豁免 → WRITE_TARGET_UNKNOWN。"""
    root, rp = _fake_tree(tmp, {"a.py":
                                "def _save(path=None):\n"
                                "    with open(path, 'w') as f:\n"
                                "        f.write('x')\n"})
    decl = (("a.py", "_save", ("data/foo/bar.json",), "目标越界"),)
    try:
        r = audit(root=root, registry_path=rp, declared=decl)
        return any(v.startswith("WRITE_TARGET_UNKNOWN") for v in r["violations"])
    finally:
        _rm(root)


def _bad_output_case(tmp):
    root, rp = _fake_tree(tmp, {"a.py": "ROOT = '.'\n"},
                          registry={"registry_version": "1", "capabilities": [
                              {"id": "S3", "output_path": "data/foo/bar.json"}]})
    try:
        r = audit(root=root, registry_path=rp, declared=())
        return any(v.startswith("OUTPUT_NOT_LANDING") for v in r["violations"])
    finally:
        _rm(root)


def _rm(p):
    import shutil
    shutil.rmtree(p, ignore_errors=True)


def _prod_write_calls():
    """本文件**生产区**（剔除 `_selftest` 及其夹具整段）不得出现任何写盘形态。

    只在生产区查 —— 自检必须能造临时树（在 `_fake_tree` 里以写模式 open），故判据按
    段落划分，而不是拿正则扫全文源码（那会把夹具里的代码字符串一起当成写盘 = 假判据）。
    注意：本函数自身在三处 `pats` 字面量里含这些形态的**正则**，故模式串写成转义形
    （不带字面 `(`），否则会自己命中自己。
    """
    ext = _external_names(ROOT)
    lines, _m = prod_lines(os.path.abspath(__file__), ext=ext)
    src = "\n".join(lines)
    pats = (r"open\([^\)]*['\"][wax]", r"os\.(replace|rename|remove|rmdir|makedirs|unlink)\(",
            r"shutil\.(copy|copy2|copyfile|move|rmtree)\(")
    return [p for p in pats if re.search(p, src)]


def _single_source_of_truth():
    """落点清单只在本文件维护：真库其它生产代码里不得出现落点/豁免 id 字面量。

    只查「生产代码区」（剔除各自 `_selftest` 段）：自检里写样例 id 是合法的。
    id 前缀 `LD` 而非 `L` —— 归因五维已有 `attribution_L1L5`（L1 环境…L5 执行，F4 落盘），
    落点若沿用 `L1~L5` 会在日志与归因输出里与「L4 时间环」直接撞名。
    """
    me = os.path.abspath(__file__)
    ext = _external_names(ROOT)
    bad = []
    for p in _py_files(ROOT):
        if os.path.abspath(p) == me:
            continue
        lines, _m = prod_lines(p, ext=ext)
        for i, ln in enumerate(lines, 1):
            if re.search(r"['\"](?:LD|X)[1-5]['\"]", ln):
                bad.append("%s:%d" % (os.path.relpath(p, ROOT), i))
    return not bad


def _public_name_leaks():
    """全库扫一遍：`_test_only_fns` 的返回值里不得出现公开名（非下划线开头）。

    这是 C8 的通用化形态——夹具判定只能吃掉私有名，公开 API 必须恒进审计。
    """
    ext = _external_names(ROOT)
    leaks = []
    for p in _py_files(ROOT):
        tree = ast.parse(io.open(p, encoding="utf-8").read())
        for n in _test_only_fns(tree, ("_selftest",), ext):
            if not n.startswith("_"):
                leaks.append("%s:%s" % (os.path.basename(p), n))
    return leaks


def _discipline(verbose=True):
    """纪律自检：落点审计（三条判据）+ 本文件不写盘、不联外网。"""
    rep = audit()
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    bad_imp = re.findall(r"^\s*(?:import|from)\s+(requests|urllib|socket|subprocess|openai)\b",
                         src, re.M)
    bad_write = _prod_write_calls()
    if verbose:
        print("纪律自检 · landing.py")
        print("  ① 扫描文件数            : %d（生产代码区，已剔 _selftest 夹具）" % rep["scanned"])
        print("  ② 路径词汇 / 未知       : %d / %d" % (rep["paths"], len(rep["unknown"])))
        print("  ③ 写点 / 未声明         : %d / %d"
              % (rep["write_sites"], len(rep["undeclared_write"])))
        print("  ④ 登记表反查            : %d 条登记 · 命中 %d · 悬空 %d · 目标越界 %d"
              % (rep["declared"], rep["declared_matched"], len(rep["stale_declaration"]),
                 len(rep["bad_write_target"])))
        print("  ⑤ 注册表闭环            : output_path %d 条 · 未归类 %d · 未登记能力引用 %d"
              % (rep["registry"]["outputs_checked"], len(rep["registry"]["bad_output_path"]),
                 len(rep["registry"]["bad_cap_ref"])))
        print("  ⑥ 网络/子进程导入       : %s" % (bad_imp or "无 [OK]"))
        print("  ⑦ 生产区写盘调用        : %s" % (bad_write or "无 [OK]（纯只读审计）"))
        print("  ⑧ 公开名被夹具吞掉      : %s" % (_public_name_leaks() or "无 [OK]"))
        print("  ⑨ 落点封闭结论          : %s"
              % ("无第 6 类落点 [OK]" if rep["ok"] else "违例 %d 条 [!!]" % len(rep["violations"])))
    return 0 if (rep["ok"] and not bad_imp and not bad_write) else 1


def _main(argv):
    if "--selftest" in argv:
        print("landing.py 自检（0.7 落点封闭 · SCHEMA_VERSION=%d · 临时目录）" % SCHEMA_VERSION)
        return _selftest()
    if "--discipline" in argv:
        return _discipline()
    rep = audit()
    if "--report" in argv:
        print("落点封闭审计报告 · 0.7")
        print("  扫描：%d 文件 · 路径词汇 %d 条 · 写点 %d 个"
              % (rep["scanned"], rep["paths"], rep["write_sites"]))
        print("\n  路径词汇归类（生产代码区）：")
        for rel in sorted(vocabulary()):
            c = classify(rel)
            tag = c["landings"] and ("落点 " + "/".join(c["landings"])) or \
                (c["exemptions"] and ("豁免 " + "/".join(c["exemptions"]))) or c["kind"]
            print("    %-46s %-14s %s" % (rel, tag, ",".join(vocabulary()[rel])))
        print("\n  写点（不可静态解析者）：")
        for s in rep["indirect"]:
            print("    %s:%d %s() → %s" % (s["file"], s["line"], s["fn"], s["expr"]))
        print("\n  注册表闭环：output_path %d 条 · 全部归类 ok=%s"
              % (rep["registry"]["outputs_checked"], not rep["registry"]["bad_output_path"]))
        print("\n  结论：%s" % ("无第 6 类落点 [OK]" if rep["ok"]
                               else "违例 %d 条" % len(rep["violations"])))
        for v in rep["violations"]:
            print("    [!!] %s" % v)
        return 0 if rep["ok"] else 1
    print("落点封闭（0.7）· 知识/结论/研究/假设的持久化只有 5 个落点 + 5 类豁免")
    print("\n  5 落点：")
    for l in LANDINGS:
        print("    %-4s %-14s %-44s %s" % (l["id"], l["name"], l["pattern"], l["item"]))
    print("\n  5 类豁免：")
    for x in EXEMPTIONS:
        print("    %-4s %-24s %s" % (x["id"], x["name"], " | ".join(x["patterns"])))
    print("\n  审计：%d 文件 · %d 条路径词汇 · %d 个写点 · 结论 = %s"
          % (rep["scanned"], rep["paths"], rep["write_sites"],
             "无第 6 类落点 [OK]" if rep["ok"] else "违例 %d 条" % len(rep["violations"])))
    for v in rep["violations"]:
        print("    [!!] %s" % v)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
