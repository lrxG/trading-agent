# -*- coding: utf-8 -*-
"""writegate.py · 生产写入闸（tools/fs 正式安全入口 · v3 集成版 2026-10-14）

动机（用户 2026-10-14 两轮指令）：
  「不要只用 isdir 作为安全证明。明确区分『目录存在校验』和『生产写入授权』。」
  「将写入安全防线完成实际集成……不能仅提供一个可选包装脚本。」
  历史事故：INC-E8-BASE-001（e8 CLI 漏传 --base 误写生产）、INC-WGATE-001
  （闸门演示中自身吃掉 --base 未转发 ⇒ 误写生产）。共同根因：**参数经多层转手，
  漏一层就静默落生产，且无任何机制能发现**。

v3 架构（双层强制，缺一不可）：
  ┌─ 第一层 · 包装闸（运维入口）────────────────────────────────────────
  │  `python tools/fs/writegate.py run --cli <CLI> --args "..." [--base SB]
  │      [--confirm-production <digest>]`
  │  plan（零执行预览）→ 四道闸 → 子进程（票据经 WB_WGATE_TICKET 环境变量传入）
  │  → 前后全树 sha256 指纹核验 → 证书 JSON。
  └─ 第二层 · 原语级守卫（不可绕过的核心）──────────────────────────────
     所有写点原语（_atomic_write / write_snapshot / _append / _save / emit /
     stage_core / update_brain / _dump …）在落盘前调 `writegate.check_write(p)`：
       · 路径不在生产受护区（data/ memory/）⇒ 放行（沙盒/自检/临时目录零影响）；
       · 路径在生产受护区 ⇒ **必须有有效生产票据**，且路径必须匹配票据的
         预期目标模式，否则 WriteGateError——**直连调用、漏传 --base、
         参数转发丢失、绕过包装闸，全部在这一层被拒**。
     票据来源只有两个：① 包装闸 run（子进程经 env 传入）；② 受控 API
     `enter_production(...)`（供 promote 等无 CLI 的生产写点显式取票）。
     同进程链（wakeup→settle→decision._append…）经模块级 _TICKET 自动继承。

信任边界（明示）：
  票据经环境变量传播，**防误不防恶意**——防的是漏传/误调用/路径错误/异常分支，
  不防有能力篡改环境变量的攻击者；完整防恶意需 OS 级强制（超出本轮范围）。
  事后核验（前后指纹 diff + 证书）提供第二重证据链。

digest 绑定（要求：确认后命令任何变化都必须拒绝）：
  digest = sha256({schema, mode, cli, **最终实参（转发后）**, root,
                   预期目标模式, 被执行脚本的 sha256})[:16]
  · 绑定转发后的完整命令行（INC-WGATE-001 修复①：漏转发 ⇒ digest 变 ⇒ 拒）；
  · 绑定脚本内容哈希（代码在确认后被改 ⇒ digest 变 ⇒ 拒）；
  · 绑定目标根与预期写入清单（目标路径变化 ⇒ 拒）。
  原语级 check_write 再按票据预期模式逐路径复核（双保险）。

与现有契约的关系：
  · 不改任何业务 CLI 的参数与语义；`plan` 子命令零执行；
  · 合法生产运行能力保留——但必须经包装闸显式确认（计划+根+目标+覆盖清单）；
  · 沙盒直跑（--base <sb>）仍然允许（无害写），包装闸为其附加前后核验证书；
  · 无沙盒重定向参数的 CLI 在**沙盒模式**拒绝执行（CANNOT_ISOLATE，禁假隔离）；
  · 各 CLI 自有闸（seed_real 四重闸 / expiry 默认只读 / settle 无日历拒绝 /
    audit_replay EVIDENCE_EXISTS）全部保留，本闸是外层不是替代。

用法：
  python tools/fs/writegate.py plan --cli tools/e2e/e8_quality.py \
      --args "--eval 2026-10-14 sh600519" [--base SB]
  python tools/fs/writegate.py run  --cli tools/e2e/e8_quality.py \
      --args "--eval 2026-10-14 sh600519" --base SB
  python tools/fs/writegate.py run  --cli tools/risk/settle.py \
      --args "--settle 2026-10-14 --calendar data/raw/meta/trade_cal.csv" \
      --confirm-production <digest>
  python tools/fs/writegate.py --selftest     # 全沙盒自检（含 8 大强制场景）
"""
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.normpath(os.path.dirname(os.path.abspath(__file__))))
from paths import ROOT as _PATHS_ROOT  # noqa: E402

SCHEMA_VERSION = 3

# ── 登记的生产根（动态：保护本件所在项目；副本环境自动跟随副本）──────────
PROD_ROOT = os.path.normpath(os.path.abspath(_PATHS_ROOT))

# 模块真实所在项目根（脚本解析用；自检期 PROD_ROOT 被覆盖时不变）
REAL_ROOT = PROD_ROOT

# ── 受护区（生产写入管制的子树）────────────────────────────────────────
PROTECTED = ("data", "memory")

# ── append-only 账本（行数只增不减判据）────────────────────────────────
APPEND_ONLY = (
    "data/generated/decision_ledger.jsonl",
    "data/generated/pnl_ledger.jsonl",
    "data/state/run_log.jsonl",
    "data/params_changelog.jsonl",
    "data/calibration_queue.jsonl",
    "data/generated/reports/e3_daily.jsonl",
    "data/generated/reports/e6_weekly.jsonl",
    "data/generated/reports/e10_daily.jsonl",
    "data/learning/demand_queue.jsonl",
    "data/state/migrations/expiry_rule_v2.jsonl",
)

# ── CLI → 写入契约表（37 可写 CLI + 1 API 入口）────────────────────────
# trigger  ：args 出现任一键才可能写（否则本次调用零文件写）
# targets  ：落点模式（<as_of>/<code> 由实参代入；* = 通配）
# base_flag：沙盒模式拼回 CLI 的重定向参数；None = 无沙盒机制（生产专用）
TARGETS = {
    # ── A 组：有 --base 沙盒机制 ─────────────────────────────────────
    "premarket.py": {"trigger": ["--eval"], "base_flag": "--base",
                     "targets": ["data/generated/reports/e1_<as_of>.md",
                                 "data/state/run_log.jsonl"]},
    "scan.py": {"trigger": ["--eval"], "base_flag": "--base",
                "targets": ["data/state/candidates.json", "data/state/mainline.json",
                            "data/snapshot/<as_of>/*",
                            "data/state/run_log.jsonl"]},
    "s1.py": {"trigger": ["--eval"], "base_flag": "--base",
              "targets": ["data/generated/s_s1_<as_of>.json"],
              "note": "S1 事件催化评估（2026-10-22 补 --eval 入口）："
                      "`evaluate(event)` 本是**单事件接口**，此前只有 --selftest，"
                      "每日链跑不到它 ⇒ 判 NO_OUTPUT。新增 `scan_day()`："
                      "从当日 news.jsonl 取**带关联个股**的事件逐个评估。"
                      "口径：related_codes 为空一律跳过（市场资讯不带个股，不猜）；"
                      "first_seen_at=announcement_at=该行 ts 日期（本源无先泄露后公告）；"
                      "单条失败如实计入 rejected 不中断整批；evaluate 内按 as_of 合并快照幂等"},
    "s3.py": {"trigger": ["--eval"], "base_flag": "--base",
              "targets": ["data/generated/s_s3_<as_of>.json"]},
    "s4.py": {"trigger": ["--eval"], "base_flag": "--base",
              "targets": ["data/generated/s_s4_<as_of>.json"]},
    "s5.py": {"trigger": ["--eval"], "base_flag": "--base",
              "targets": ["data/generated/s_s5_<as_of>.json"]},
    "s6.py": {"trigger": ["--fetch-section"], "base_flag": "--base",
              "targets": ["data/generated/s_s6_section_<as_of>.json"]},
    "s7.py": {"trigger": ["--eval"], "base_flag": "--base",
              "targets": ["data/generated/s_s7_<as_of>.json"],
              "note": "S7 评估快照（evaluate 分支）；news.jsonl 仅自检夹具，非生产写点"},
    "s7.py:fetch-consensus": {"trigger": ["--fetch-consensus"], "base_flag": "--base",
                              "targets": ["data/generated/s_s7_consensus_<as_of>.json"],
                              "note": "S7 一致预期缓存（fetch 分支独立写点；按子命令收窄登记）"},
    "s7.py:fetch-sector-forecast": {
        "trigger": ["--fetch-sector-forecast"], "base_flag": "--base",
        "targets": ["data/generated/s_s7_sector_forecast_<as_of>.json"],
        "note": "S7 行业级盈利预测缓存（fetch 分支；入参=申万行业 CODE 串，非个股码；"
                "只取最新截面，as_of 对齐查询实测空表）"},
    "s8.py": {"trigger": ["--eval"], "base_flag": "--base",
              "targets": ["data/generated/s_s8_<as_of>.json"]},
    "e2_intraday.py": {"trigger": ["--eval"], "base_flag": "--base",
                       "targets": ["data/generated/reports/e2_<as_of>.md",
                                   "data/raw/intraday/*"]},
    "e5_position.py": {"trigger": ["--eval"], "base_flag": "--base",
                       "targets": ["data/generated/reports/e5_<as_of>.md",
                                   "data/state/run_log.jsonl"]},
    "e6_weekly.py": {"trigger": ["--week-end"], "base_flag": "--base",
                     "targets": ["data/generated/reports/e6_weekly.jsonl"]},
    "e7_research.py": {"trigger": ["--eval"], "base_flag": "--base",
                       "targets": ["data/generated/reports/e7_<as_of>.md"]},
    "e8_quality.py": {"trigger": ["--eval"], "base_flag": "--base",
                      "targets": ["data/generated/reports/e8_<as_of>.md"]},
    "e9_portfolio.py": {"trigger": ["--eval"], "base_flag": "--base",
                        "targets": ["data/generated/reports/e9_<as_of>.md",
                                    "data/state/run_log.jsonl"]},
    "e10_daily.py": {"trigger": ["--eval"], "base_flag": "--base",
                     "targets": ["data/generated/reports/e10_daily.jsonl",
                                 "data/state/run_log.jsonl"]},
    "e11_research.py": {"trigger": ["--emit"], "base_flag": "--base",
                        "targets": ["data/generated/reports/e11_<as_of>.md"]},
    # ── B 组：无沙盒机制（恒生产；必须经闸门 + 显式确认）─────────────
    "s1_backfill.py": {"trigger": ["--build"], "base_flag": None,
                       "targets": ["data/generated/reports/s1_backfill/summary.json"]},
    "s2.py": {"trigger": ["--eval"], "base_flag": "--base",
              "targets": ["data/generated/s_s2_<as_of>.json",
                          "data/generated/reports/s2_backfill/summary.json",
                          "data/state/run_log.jsonl"],
              "note": "2026-10-19 修：CLI 原无 --base 致 --eval 必打生产（argparse 直接拒"
                      " --base）⇒ 加 --base 并贯通 evaluate/build_history/history_check；"
                      "history-build 的 X1 提案报告一并登记"},
    "s1.py:evaluate": {"trigger": [], "base_flag": None,
                       "targets": ["data/generated/s_s1_<as_of>.json"],
                       "note": "API 入口（无生产 CLI；CLI 仅 --selftest/--discipline）；"
                               "小写 s_s1_ 为历史特例（CAP 注册表大写 S1 挂账）；"
                               "同键幂等覆盖不追加；经 enter_production 取票"},
    "eod_review.py": {"trigger": ["--as-of"], "base_flag": None,
                      "targets": ["data/generated/reports/e3_daily.jsonl"],
                      "note": "API 有 base 但写点用模块常量 REPORT（G9：base 只读半途）"},
    "e4_research.py": {"trigger": ["--ensure"], "base_flag": None,
                       "targets": ["data/research/e4_*/*",
                                   "data/state/run_log.jsonl"]},
    "audit_replay.py": {"trigger": [], "base_flag": None,
                        "targets": ["data/generated/reports/audit_replay/*"],
                        "note": "证据件路径硬编码 _ROOT；EVIDENCE_EXISTS 拒覆盖保留"},
    "cal_convert.py": {"trigger": ["--convert"], "base_flag": "--convert",
                       "targets": ["<out>"],
                       "note": "--convert <out> 显式目标；沙盒模式须在 base 之下"},
    "hboard.py": {"trigger": ["--refresh"], "base_flag": None,
                  "targets": ["data/generated/h_board.md"]},
    "registry.py": {"trigger": ["--manifest"], "base_flag": None,
                    "targets": ["data/state/skeleton_manifest.json"],
                    "note": "冻结台账重建=显式治理动作"},
    "registry.py:mark_impl": {"trigger": [], "base_flag": None,
                              "targets": ["data/state/capability_registry.json"],
                              "note": "实现落痕唯一写点（impl_log 追加/更新；供幽灵能力判定）；"
                                      "API 入口（无 CLI），经 enter_production 取票；"
                                      "子命令级精度：--manifest 只写 skeleton_manifest，"
                                      "本入口只写 capability_registry，两者目标不混"},
    "regime.py": {"trigger": ["--eval", "--live"], "base_flag": "--brain",
                  "targets": ["<brain>", "data/state/run_log.jsonl"],
                  "note": "--brain 只重定向 brain 段；run_log 恒生产（G3）"},
    "h_ledger.py": {"trigger": ["--create-from-plan", "--invalidate", "--settle"], "base_flag": None,
                    "targets": ["data/state/h_ledger.jsonl"],
                    "note": "盘前假设 H 账本（append-only 唯一写点；2026-10-29 建）。"
                            "用户裁决三缺口最小闭环第一环：H 结构化立账/盘中失效/盘后结算"
                            "共用同一 H ID；状态机单向 CREATED→INVALIDATED→SETTLED，结论不可改"},
    "fetch_regulatory.py": {"trigger": ["--date"], "base_flag": None,
                           "targets": ["data/raw/<as_of>/regulatory.jsonl",
                                       "data/raw/<as_of>/holder_behavior.jsonl"],
                           "note": "监管/股东行为日扫（信息能力升级 P0/P1 · 2026-10-30）。"
                                   "事实层关键词过滤，零判断；输出落 raw 采集件"},
    "daily_pipeline.py": {"trigger": ["--execute"], "base_flag": None,
                          "targets": ["data/state/run_log.jsonl",
                                      "data/state/candidates.json", "data/state/mainline.json",
                                      "data/state/timing_state.json",
                                      "data/raw/<as_of>/*", "data/snapshot/<as_of>/*",
                                      "data/raw/bus/*", "data/generated/s_s*_<as_of>.json",
                                      "data/generated/reports/*",
                                      "data/generated/sim/*", "data/input/*",
                                      "data/learning/demand_queue.jsonl",
                                      "memory/state/thesis.md",
                                      "data/state/cursor.json"],
                          "note": "每日15步编排器（2026-10-30 登记补漏：此前表外裸跑）。"
                                  "本件只编排，真实写点均为表内已登记 CLI（scan/live_seed/ingest/"
                                  "merge_klines/settle 等，子进程经 WB_WGATE_TICKET 放行）；"
                                  "本条目使编排器自身的 run_log 追加与包装产物合法化"},
    "settle.py": {"trigger": ["--settle"], "base_flag": None,
                  "targets": ["data/state/cursor.json",
                              "data/generated/decision_ledger.jsonl",
                              "data/generated/sim/decision_ledger.jsonl",
                              "data/generated/pnl_ledger.jsonl",
                              "data/generated/sim/pnl_ledger.jsonl",
                              "data/learning/demand_queue.jsonl",
                              "data/state/run_log.jsonl"],
                  "note": "五步结算=生产唯一执行器；无 --calendar 拒绝（fail-closed）。"
                          "★2026-10-20 补 SIM 轴双路径：`_step_fill`/`_step_pnl` 遍历 "
                          "(False, True) 两轴（SIM 决策同样要判成交/算盈亏），原登记只有 "
                          "LIVE 路径 ⇒ SIM 轴结算必 PROD_WRITE_UNEXPECTED（A 路 SIM 决策"
                          "无法结算 = 学习闭环断在最后一跳）。v1.4 物理分轴：sim=True 落 "
                          "generated/sim/，LIVE 落 generated/。"},
    "wakeup.py": {"trigger": ["--wake"], "base_flag": None,
                  "targets": ["data/state/cursor.json", "data/state/candidates.json",
                              "data/generated/decision_ledger.jsonl",
                              "data/generated/pnl_ledger.jsonl",
                              "data/learning/demand_queue.jsonl",
                              "data/generated/h_board.md",
                              "data/state/run_log.jsonl"],
                  "note": "唤醒即追平=生产全链；ctx 有 base 但 CLI 不暴露"},
    "expiry_rule_v2.py": {"trigger": ["--execute", "--rollback"], "base_flag": None,
                          "targets": ["data/state/candidates.json",
                                      "data/state/migrations/*"],
                          "note": "默认只读；--execute 才写；有备份+changelog"},
    "seed_real.py": {"trigger": ["--reseed", "--stage", "--promote",
                                 "--refresh-decl"], "base_flag": None,
                     "targets": ["data/raw/<as_of>/*", "data/snapshot/<as_of>/*",
                                 "data/state/reseeds/*"],
                     "note": "四重闸（--execute/--reason/防重入/备份）保留；--klines 只换输入；"
                             "2026-10-19：run_log 目标移除——本件不走 observe 包装、零 run_log "
                             "写入，其留痕 = RESEED.json 变更日志（before/after sha256）"},
    "apply_open_plugins.py": {"trigger": [], "base_flag": None,
                              "targets": ["data/state/capability_registry.json"],
                              "note": "开插批次（2026-10-19 用户令）：mark_impl 20 条 + stage "
                                      "SKELETON→PLUGINS；只改 impl_log/stage 两处，幂等；"
                                      "落点目录前置闸（缺一不翻）在执行件内"},
    "run_chain.py": {"trigger": ["--as-of"], "base_flag": "--base",
                     "targets": ["memory/state/brain_state.md",
                                 "data/research/drive/chains.jsonl",
                                 "data/generated/decision_ledger.jsonl",
                                 "data/generated/sim/decision_ledger.jsonl",
                                 "data/generated/pnl_ledger.jsonl",
                                 "data/generated/sim/pnl_ledger.jsonl",
                                 "data/generated/cases.jsonl",
                                 "data/generated/sim/cases.jsonl",
                                 "data/state/candidates.json"],
                     "note": "编排件：C1 环境档腿回写 brain_state.md + C3 drive.review 落链 "
                             "（data/research/drive/chains.jsonl）+ C4 decide / C5 risk_plan "
                             "落 decision_ledger + C6 case_builder 落 cases（2026-10-20 补全："
                             "原登记只有 brain_state.md，A 路生产首跑（10-19）需要整链票据，"
                             "与 wakeup「生产全链」同款口径）；**SIM/LIVE 双轴账本路径都要"
                             "登记**（v1.4 物理分轴：sim=True 落 generated/sim/，--live 落 "
                             "generated/；漏登 sim 轴 = A 路模拟决策全部 DECIDE_ERROR 实证）；"
                             "data/state/candidates.json = three_q.commit 落观察锚/状态机的写点"},
    "mech_driver.py": {"trigger": [], "base_flag": "--base",
                       "targets": ["data/input/*", "memory/state/thesis.md"],
                       "note": "机械基准判断器（2026-10-19 · 用户令「先 B 后 A」的 B 路）："
                               "写 data/input/mech_proposals_<as_of>.json + ensure_thesis "
                               "落 memory/state/thesis.md（2026-10-20 补登记：A 路生产首跑"
                               "暴露原登记缺 thesis 写点，沙盒期未暴露）；"
                               "本件**不是冻结件**，但产物只能跑 SIM（run_chain 有 "
                               "MECHANICAL_BASELINE_LIVE_FORBIDDEN 硬锁）"},
    "llm_emit.py": {"trigger": ["--as-of"], "base_flag": "--base",
                    "targets": ["data/input/*", "memory/state/thesis.md"],
                    "note": "A 路判断生成器（2026-10-20 · LLM 提案装配+写盘）：判断本体在"
                            " LLM（wb），本件只装配 chains（证据=机械可追溯）+ 按 judgments "
                            "覆写 proposals 层 → data/input/proposals_<as_of>.json；"
                            "producer=LLM_WB ≠ MECHANICAL_BASELINE ⇒ LIVE 锁放行源；"
                            "写点含 ensure_thesis（复用 mech_driver.build 幂等）"},
    "ingest.py": {"trigger": ["--promote"], "base_flag": None,
                  "targets": ["data/snapshot/*"],
                  "note": "投放区→快照区正式化（promote_snapshot；2026-10-20 补 CLI 操作"
                          "入口——此前只有 selftest/discipline，生产 promote 无入口）"},
    "fetch_finance.py": {"trigger": ["--execute"], "base_flag": "--base",
                        "targets": ["data/raw/bus/finance.csv"],
                        "note": "抓历史财务（含真实公告日 InfoPublDate，2026-10-21 · A 基本面）：数据源 `westock finance --type {income|balance|cashflow} --limit N --fields all`（**必须 all 才有 InfoPublDate**，core 窄表没有）。长表 code/end_date/publ_date/source_type/field/value。纪律：①缺 publ_date 的记录整条丢弃（无法做 PIT 对齐，留着会诱使下游误用）②断点续跑 ③dry-run 为默认须 --execute ④并发抓取（实测 8 并发较稳）"},
    "heat_sensor.py": {"trigger": ["--execute"], "base_flag": "--base",
                       "targets": ["data/raw/bus/margin_daily.csv"],
                       "note": "两融数据采集（2026-10-21 · 补 DATA-HUB 声称存在但从未实现的断链）："
                               "数据源 `westock fund margin`。★ 单票调用返回**英文列**"
                               "（FinanceValue/FinanceBuyValue/…）、批量调用返回**中文列**"
                               "（融资余额/融资买入/…且带单位后缀如 `融资余额环比(%)`）"
                               "⇒ 两套别名 + **前缀匹配**，否则批量一行都解析不出。"
                               "append-only 主键 date+code，幂等跳过已有；"
                               "批量接口缺「融资偿还」「两融余额」⇒ 这两列留空不填 0"},
    "heat_sensor.py:--clean": {"trigger": ["--clean"], "base_flag": "--base",
                               "targets": ["data/raw/bus/margin_daily.csv",
                                           "data/raw/bus/margin_daily.csv.bak.*"],
                               "note": "两融文件重复表头垃圾清理（治理动作）："
                                       "先写 .bak 备份再回写；"
                                       "2026-10-21 data_health 体检发现 53 行里 28 行是重复表头。"
                                       "★ 备份名带时间戳（动态）⇒ 用通配 `*.bak.*` 登记，"
                                       "否则闸会判「预期外写入」而拒绝（第一次执行就撞上）"},
    "fetch_auction.py": {"trigger": ["--execute"], "base_flag": "--base",
                         "targets": ["data/raw/bus/auction_<as_of>.csv",
                                     "data/state/_fetch_auction.lock"],
                         "note": "集合竞价采集（2026-10-22 · 为 S10 落地）："
                                 "S10 `--availability` 说 auction_feed 不可用，"
                                 "但**实测 westock `quote` 里就有完好竞价字段**"
                                 "（pre_quote_price / pre_quote_change_pct / "
                                 "pre_quote_volume / pre_quote_amount / wb_ratio / "
                                 "price_ceiling）—— sh603278 实测竞价 +9.93% 顶板。"
                                 "S10 缺的不是数据，是没人去采（它只查旧包目录，从不查 westock）。"
                                 "纪律：只采个股（codes.py 唯一真源）、批量 40 只、"
                                 "缺竞价字段的行丢弃不空行充数（M20）、原子写、进程锁（M21）"},
    "build_decision_card.py": {"trigger": ["--execute"], "base_flag": "--base",
                               "targets": ["data/decision_card.md"],
                               "note": "决策卡生成（0.15 载体 · 2026-10-22 补建）："
                                       "设计 §5.2/§6 明确要求本文件，landing LD5 也登记了它，"
                                       "但**全项目没有任何模块写它** ⇒ C4 判 NO_OUTPUT。"
                                       "本工具按「三问 + 驱动依据区 + 参数卡」装配，"
                                       "零判断零新增语义。驱动依据区**只收 VALIDATED** 的 Rule；"
                                       "参数卡未声明用途的标 PURPOSE_UNDECLARED，**不编理由**。"},
    "fetch_news.py": {"trigger": ["--execute"], "base_flag": "--base",
                      "targets": ["data/raw/*/news.jsonl",
                                  "data/state/_fetch_news.lock"],
                      "note": "新闻/公告采集（2026-10-22）：`data/raw/<d>/news.jsonl` "
                              "是 S1/S7/S8/E7 的共同上游，但生产代码**没有任何模块采它**"
                              "（2026-10-22 实测：9 月每天 1000+ 条，10 月全部 0 字节）。"
                              "数据源 `westock news list sh000001 --limit N`（市场资讯）"
                              "+ `westock notice list <代码> `（个股公告，related_codes 真源）。"
                              "纪律：append 按 news_id 判重（M22）、不编造 body、"
                              "市场资讯 related_codes 留空（下游计 unmapped，宁可漏召回不猜）、"
                              "只留目标日条目、.tmp+os.replace 原子写、进程锁（M21）"},
    "fetch_fundflow.py": {"trigger": ["--execute"], "base_flag": "--base",
                          "targets": ["data/raw/bus/fundflow.csv",
                                      "data/raw/bus/fundflow.csv.bak.*",
                                      "data/state/_fetch_fundflow.lock"],
                          "note": "个股日度资金流采集（2026-10-22）："
                                  "S5 资金筹码结构**只从旧包读 fundflow**，"
                                  "而旧包该表停在 2026-09-30 且当日仅 716 只票 ⇒ "
                                  "S5 永远拿不到 10 月数据，evaluate('2026-10-22') 直接抛 "
                                  "CODE_NOT_IN_UNIVERSE。本工具补建本项目通道。"
                                  "数据源 `westock fund flow <单码> --start --end`"
                                  "（单代码可一次取整个区间，实测 10-08~10-22 共 11 天全齐）。"
                                  "输出字段与旧包逐一对应、量纲一致（元）。"
                                  "★ 增量补采（键=(code,date)，M22）、不填0不插值、"
                                  "单只失败如实入清单（M20）、先 .bak、.tmp+os.replace、"
                                  "进程锁（M21）"},
    "fetch_etf_klines.py": {"trigger": ["--execute"], "base_flag": "--base",
                            "targets": ["data/raw/bus/etf_klines.csv",
                                        "data/raw/bus/etf_klines.csv.bak.*"],
                            "note": "ETF K线增量补采（2026-10-22）："
                                    "体检发现 etf_klines.csv 停在 2026-09-22 且是**旧包原样副本**，"
                                    "九步编排根本不采它 ⇒ strategy_family 的「低波轮动"
                                    "年化5.0%/夏普1.07」结论建立在**一个月前**的数据上。"
                                    "数据源 `westock kline <代码[,代码...]> --period day "
                                    "--start --end`（支持批量+日期区间）。"
                                    "★ 增量补采（已存在 (code,date) 跳过）、不填0不前向填充、"
                                    "先 .bak 备份、.tmp+os.replace 原子写、按 code+date 排序保证幂等。"
                                    "实测 62 只 ETF 一次补 992 行、0 失败"},
    "purge_nonstock_candidates.py": {"trigger": ["--execute"], "base_flag": "--base",
                                     "targets": ["data/state/candidates.json",
                                                 "data/state/candidates.json.bak.*"],
                                     "note": "候选池非个股清理（治理动作，2026-10-22）："
                                             "4650 条里 355 条非个股（BOND 180 / "
                                             "BSE_MISFIXED 171 / OTHER 3 / INDEX 1，"
                                             "全为 EXPIRED）。根因=五路采纳零类型校验，"
                                             "已由 codes.py + scan.add() 类型闸根治。"
                                             "只清 EXPIRED，遇非 EXPIRED 非个股拒绝执行。"
                                             ".tmp 不登记（os.replace 后必然消失）。"},
    "finance_dedup.py": {"trigger": ["--execute"], "base_flag": "--base",
                         # ★ 只登记**真正会留在盘上**的产物。
                         #   .tmp 是原子写的中转文件，`os.replace` 之后必然消失，
                         #   若登记为预期产物，闸会把它算成「漏写 1」——
                         #   2026-10-22 第一次跑就撞上。**闸是对的，是我登记错了。**
                         "targets": ["data/raw/bus/finance.csv",
                                     "data/raw/bus/finance.csv.bak.*"],
                         "note": "finance.csv 去重（治理动作，2026-10-22 · 真事故善后）："
                                 "2026-10-21 14:38 启动的单票采集进程未被回收，跑到 "
                                 "2026-10-22 08:41（18 小时），反复追加已采数据 ⇒ "
                                 "1,364,802 行里 344,631 行是完全重复。"
                                 "键 = (code, end_date, publ_date, source_type, field)，"
                                 "值冲突 166 条保留**后者**（财务追溯调整越晚越准）并显式列出。"
                                 "★ 先落 .bak 备份再写，原子写用 .tmp + os.replace，"
                                 "故三个路径都要登记（备份名带时间戳 ⇒ 用通配）"},
    "fetch_mktcap.py": {"trigger": ["--execute"], "base_flag": "--base",
                        "targets": ["data/raw/bus/mktcap.csv"],
                        "note": "抓逐日股本→推算历史市值（2026-10-21 · G1 能力 · 只读源）："
                                "数据源为通达信只读 get_gb_info（gb_type=1 + start/end_date "
                                "给逐日 Zgb 总股本/Ltgb 流通股本，单位=股，已用 600178 "
                                "ActiveCapital 校验一致）。市值 = 总股本 × 当日 close。"
                                "纪律：①缺股本的日期不输出、不插值、不用最新股本回推历史"
                                "②断点续跑（已抓 code 跳过）③dry-run 为默认，须 --execute"},
    "fetch_industry_map.py": {"trigger": ["--execute"], "base_flag": "--base",
                              "targets": ["data/raw/meta/stock_industry.json",
                                          "data/raw/meta/stock_industry.json.provenance.json"],
                              "note": "抓「个股→行业」映射（2026-10-21 · G2 能力 · 只读源）："
                                      "数据源为通达信只读接口 get_sector_list + "
                                      "get_stock_list_in_sector（只用 881xxx 一级行业 128 个）。"
                                      "口径：①一股一行业（实测零重叠；若重叠取字典序首个并告警）"
                                      "②代码 600000.SH → sh600000 ③dry-run 为默认，须 --execute "
                                      "④服务不可用则如实失败退出，不产出空文件冒充成功"},
    "backfill_klines_tq.py": {"trigger": ["--execute"], "base_flag": "--base",
                              "targets": ["data/raw/bus/klines.csv",
                                          "data/research/obs/"],
                              "note": "通达信 TQ 补历史 K 线（2026-10-21 · **只读源**）："
                                      "用 tq_bridge 的 get_market_data 拉 2024-01~2025-08 "
                                      "残缺段（实测每月仅 300~440 行，非全市场）⇒ 并入 "
                                      "klines.csv。纪律：①只补缺口，已有 (code,date) 绝不覆盖"
                                      "②取不到留空不插值 ③写前必留 gzip 备份到 "
                                      "data/research/obs/ ④dry-run 为默认，须 --execute "
                                      "才写盘。**只读交易无关**：tq_bridge 白名单禁全部 "
                                      "order/cancel/position 接口"},
    "backfill_zt_reason.py": {"trigger": ["--execute"], "base_flag": "--base",
                              "targets": ["data/raw/*/limit_up.csv", "data/snapshot/*"],
                              "note": "涨停池补源（**腾讯自选股官方 CLI**）："
                                      "`screen ranking --type limitup_days|limitup_seal_volume` "
                                      "给连板/封单（手×100×close→元），`sector constituent` "
                                      "反查申万二级行业；池家数以 westock 自算为基准，"
                                      "多出的票不加；first_seal_time 无历史分时源故留空"
                                      "（只进证据文本，不参与机械判据）；写后重 promote。"
                                      "零第三方数据依赖：只用腾讯官方 CLI + 标准库"},
    "a_path_daily.py": {"trigger": ["--stage"], "base_flag": "--base",
                        "targets": ["data/input/*", "memory/state/thesis.md",
                                    "data/generated/decision_ledger.jsonl",
                                    "data/generated/sim/decision_ledger.jsonl",
                                    "data/generated/pnl_ledger.jsonl",
                                    "data/generated/sim/pnl_ledger.jsonl",
                                    "data/generated/cases.jsonl",
                                    "data/generated/sim/cases.jsonl",
                                    "data/state/candidates.json",
                                    "data/research/drive/chains.jsonl",
                                    "memory/state/brain_state.md"],
                        "note": "A 路日常化编排器（2026-10-20 · 三阶段人机环）："
                                "①evidence（mech_driver.build 零判断选票+证据包 → "
                                "data/input/evidence_<as_of>.json）②emit（委托 llm_emit "
                                "装配 proposals）③chain（委托 run_chain 端到端）；"
                                "票据覆盖全链写点（与 run_chain/mech_driver 同款口径）。"
                                "★2026-10-20 补：SIM 轴双路径（v1.4 物理分轴）+ "
                                "data/state/candidates.json（three_q.commit 落观察锚）；"
                                "缺 SIM 轴登记 ⇒ A 路逐票 DECIDE_ERROR 实证"},
    "merge_klines.py": {"trigger": [], "base_flag": "--base",
                        "targets": ["data/raw/bus/klines.csv"],
                        "note": "klines 合并器（2026-10-20 · 数据层贯通）：10月按日 "
                                "daily_quote.csv → S 系全历史单文件（追加式，幂等按 "
                                "(code,date) 去重）；★前复权基准边界：CLI 历史价基准=各自"
                                "查询日，与旧包（基准=各自当日）混用遇除权边界票或有微差，"
                                "精确比价禁跨批混算（ledger v3.22 铁律）"},
    "sector_seed.py": {"trigger": ["--snapshot", "--history", "--forecast"],
                       "base_flag": "--base",
                       "targets": ["data/raw/*/sector_ranking.csv",
                                   "data/raw/*/sector_valuation.csv",
                                   "data/raw/*/sector_forecast.csv",
                                   "data/raw/meta/sector_valuation_history.csv"],
                       "note": "板块估值/行情采集（S4/S6 数据面·2026-10-20）：sector ranking"
                               "（124 板块截面含 upCount=M2 扩散依据）+ sector valuation"
                               "（PE/PB + 历史分位 PeTTMPct/PbLFPct=S4 froth/S6 锚判据）；"
                               "推翻 2026-10-12「S4/S6 全盘无源」核准（本机实证有源）；"
                               "--history 累积序列落 meta/sector_valuation_history.csv"},
    "chain_run.py": {"trigger": ["--as-of"], "base_flag": "--base",
                     "targets": ["data/generated/cases.jsonl",
                                 "data/research/obs/obs_candidates.jsonl",
                                 "data/learning/demand_queue.jsonl",
                                 "data/state/run_log.jsonl"],
                     "note": "C6 学习链独立触发件（2026-10-19 D2 路径 b）：不碰冻结件；obs 落 "
                             "data/research/obs/（X2 豁免，obs_scanner 真实写点）"
                             "三链步经 observe 留痕；与 settle 的 SETTLE_MINIMAL 行以 scanner "
                             "字段区分（不冒充、不并行双写同一语义行）"},
    "learn_pipeline.py": {"trigger": ["--window"], "base_flag": "--base",
                          "targets": ["data/generated/cases.jsonl",
                                      "data/research/obs/obs_candidates.jsonl",
                                      "data/learning/demand_queue.jsonl",
                                      "data/state/run_log.jsonl"],
                          "note": "回测驱动学习闭环（2026-10-23 用户令「学习用历史数据，"
                                  "实盘只要利润」）：①batch_replay 历史回放（沙盒在系统临时域，"
                                  "不碰生产）→ ②case 汇总到 cases.jsonl → ③复用 chain_run 跑 "
                                  "obs/demand 两链步。**不产 Rule**（仍须 verify + 用户批准）；"
                                  "写点与 chain_run 同集合（本件是它的回测入口，不新增语义）"},
    "hypothesize.py": {"trigger": ["--as-of"], "base_flag": "--base",
                       "targets": ["data/research/hypotheses_candidates.jsonl",
                                   "data/research/verify/*.jsonl",
                                   "data/calibration_queue.jsonl",
                                   "data/state/run_log.jsonl"],
                       "note": "观察→假设自动桥（2026-10-23 补 §8.3 第②层缺口）："
                               "obs 重复结构 → 可验证假设 → verify 分段验证 → "
                               "**仅 verdict=SUPPORT** 经 calibrate.propose_param 交候选。"
                               "不产 Rule、不改参数、不批准（批准权在用户）；"
                               "判据全读 params（hyp_min_samples/hyp_min_edge/hyp_max_tests），"
                               "缺参数即报错不用魔法数字"},
    "approve.py": {"trigger": ["review", "promote"], "base_flag": "--base",
                   "targets": ["data/calibration_queue.jsonl", "data/params.json"],
                   "note": "参数候选人工批准薄 CLI（2026-10-19）：--approver 必填非空，"
                           "零自动流转；写全部经 params.review/promote 内部已登记写点"},
    "live_seed.py": {"trigger": ["--stage", "--batch"], "base_flag": "--base",
                     "targets": ["data/raw/*",
                                 "data/state/run_log.jsonl",
                                 "memory/state/brain_state.md"],
                     "note": "活数据采集器（2026-10-19）：westock CLI → 0.2 契约；"
                             "--plan/--cross-check/--check 全只读零写入；--stage/--batch 自带"
                             "覆盖 ≥60% + 契约校验双前置（残日/坏件拒投）；声明件随投放落 "
                             "_unresolved.decl.json；data/raw/* 覆盖整批（--batch 多交易日）。"
                             "★ 2026-10-22 补登记两项目标：采集链内部会经 observe 写 "
                             "run_log.jsonl（留痕）、经 regime 写 memory/state/brain_state.md"
                             "（环境档段）——**都是闸体系的内部留痕，不是业务产出**，"
                             "但不登记就会被判「预期外写入」而让整批 FAIL。"
                             "事故：2026-10-22 补 10-21 时 7 项业务写入全部成功，"
                             "却因此 2 项内部写入被判预期外 ⇒ 第 1 步 FAIL、后续 9 步全未跑"},
    "case_builder.py": {"trigger": ["--build"], "base_flag": None,
                        "targets": ["data/generated/cases.jsonl",
                                    "data/state/run_log.jsonl"]},
    "demand_scanner.py": {"trigger": ["--scan", "--signal-check"],
                          "base_flag": None,
                          "targets": ["data/learning/demand_queue.jsonl",
                                      "data/research/obs/signal_promotion.jsonl",
                                      "data/state/run_log.jsonl"]},
    "obs_scanner.py": {"trigger": ["--scan"], "base_flag": None,
                       "targets": ["data/research/obs/obs_candidates.jsonl",
                                   "data/state/run_log.jsonl"]},
    "param_sweep.py": {"trigger": ["--sweep"], "base_flag": None,
                       "targets": ["data/research/sweep/sweep_facts.jsonl",
                                   "data/state/run_log.jsonl"]},
    "v6_grid.py": {"trigger": ["--grid"], "base_flag": None,
                   "targets": ["data/research/sweep/grid_facts.jsonl",
                               "data/state/run_log.jsonl"]},
    "verify.py": {"trigger": ["--verify"], "base_flag": None,
                  "targets": ["data/research/verify/results.jsonl",
                              "data/state/run_log.jsonl"]},
    "external.py": {"trigger": ["--demand"], "base_flag": None,
                    "targets": ["data/learning/external_queue.jsonl",
                                "data/state/run_log.jsonl"]},
    "position_gate.py": {"trigger": ["--eval"], "base_flag": None,
                         "targets": ["data/state/run_log.jsonl"],
                         "note": "C5 门；零数据写，仅留痕"},
    "exit_gate.py": {"trigger": ["--eval"], "base_flag": None,
                     "targets": ["data/state/run_log.jsonl"],
                     "note": "C5 门；零数据写，仅留痕"},
    # ── API 入口（无 CLI 的生产写点）────────────────────────────────
    "params.py:promote": {"trigger": [], "base_flag": None,
                          "targets": ["data/params.json",
                                      "data/params_changelog.jsonl",
                                      "data/calibration_queue.jsonl"],
                          "note": "正式值唯一写入口；四条件闸保留；经 enter_production 取票"},
    "ingest.py:stage_core": {"trigger": [], "base_flag": None,
                             "targets": ["data/raw/<as_of>/*"],
                             "note": "投放区唯一写点（5 契约键 + _core.meta.json [+声明件]）；"
                                     "龙虎榜补投等人工投放经此 API；经 enter_production 取票"},
}

# 零文件写的显式输出 CLI（stdout 转换器）
STDOUT_ONLY = ("lhb_backfill.py",)

# 票据环境变量名（子进程传播；防误不防恶意——信任边界见模块 docstring）
TICKET_ENV = "WB_WGATE_TICKET"

# 子进程输出回显上限（字符）——超长截断，避免刷屏但保留尾部错误
_ECHO_MAX = 6000


class WriteGateError(Exception):
    """闸门拒绝（fail-closed）——显式抛错，不静默放行。"""


# ═══════════════════════════════════════════════════════════════════════
# 第二层 · 原语级守卫（所有写点落盘前必过）
# ═══════════════════════════════════════════════════════════════════════
_TICKET = None


def _norm(p):
    return os.path.normpath(os.path.abspath(p))


def protected_rel(p):
    """p 若落在生产受护区 → 返回项目相对路径；否则 None（沙盒/外部路径放行）。"""
    ap = _norm(p)
    root = _norm(PROD_ROOT)
    if not (ap == root or ap.startswith(root + os.sep)):
        return None
    rel = os.path.relpath(ap, root).replace("\\", "/")
    top = rel.split("/")[0]
    return rel if top in PROTECTED else None


def current_ticket():
    return _TICKET


def _set_ticket(t):
    global _TICKET
    _TICKET = t


def clear_ticket():
    _set_ticket(None)


def _load_ticket_from_env():
    """子进程启动时从环境变量恢复票据（包装闸传入）。"""
    raw = os.environ.get(TICKET_ENV)
    if not raw:
        return
    try:
        t = json.loads(raw)
        if t.get("schema") == SCHEMA_VERSION and t.get("mode") == "production" \
                and t.get("digest") and isinstance(t.get("expected"), list):
            _set_ticket(t)
    except (ValueError, AttributeError):
        pass  # 畸型票据视为无票（fail-closed 由 check_write 负责）


_load_ticket_from_env()


def check_write(p):
    """原语级守卫。返回受护区相对路径或 None。

    · 非生产受护区 ⇒ None（放行：沙盒/自检/临时目录，零影响）；
    · 生产受护区 ⇒ 必须有有效生产票据且路径匹配预期模式，否则抛错。
    """
    rel = protected_rel(p)
    if rel is None:
        return None
    t = _TICKET
    if t is None or t.get("mode") != "production":
        raise WriteGateError(
            "PROD_WRITE_NO_TICKET: 生产受护区写入未被授权：%s\n"
            "  —— 生产写入必须经统一闸门显式确认：\n"
            "  python tools/fs/writegate.py run --cli <CLI> --args \"...\" "
            "--confirm-production <digest>（digest 见 plan 输出）" % rel)
    for pat in t.get("expected", []):
        if fnmatch.fnmatch(rel, pat):
            return rel
        # 目录形态：rel 是某预期文件模式的祖先目录（如快照日目录/投放区）⇒ 放行
        if pat.startswith(rel + "/") or fnmatch.fnmatch(rel, pat.split("*")[0].rstrip("/")):
            return rel
    raise WriteGateError(
        "PROD_WRITE_UNEXPECTED: %s 不在票据 %s 的预期目标内（%s）"
        " —— 命令/目标在确认后发生变化，拒绝写入"
        % (rel, t.get("cli"), t.get("expected")))


def enter_production(cli, args, root=None, confirm=None, expected=None,
                     script=None):
    """受控 API 取票（供 promote 等无 CLI 的生产写点）。

    与包装闸同纪律：digest 绑定 {cli, args, root, expected, script_sha}；
    confirm 必须等于 digest（显式确认），否则拒绝。
    """
    root = _norm(root or PROD_ROOT)
    if root != PROD_ROOT:
        raise WriteGateError("API_TICKET_ROOT_MISMATCH: 生产票根必须是登记生产根")
    if expected is None:
        ent = TARGETS.get(os.path.basename(cli)) or {}
        expected = ent.get("targets") or []
    plan = {"schema": SCHEMA_VERSION, "mode": "production", "cli": cli,
            "args": list(args), "root": root,
            "expected": list(expected),
            "script_sha256": _script_sha(script or cli)}
    digest = _digest_of(plan)
    if confirm != digest:
        raise WriteGateError(
            "API_TICKET_NO_CONFIRM: 必须显式传入 confirm=<digest>"
            "（digest=%s；缺参/错值=拒绝）" % digest)
    _set_ticket(plan)
    return plan


# ═══════════════════════════════════════════════════════════════════════
# 第一层 · 包装闸（运维统一入口）
# ═══════════════════════════════════════════════════════════════════════
def sha256_file(p):
    h = hashlib.sha256()
    with io.open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _script_sha(cli):
    """被执行脚本的内容哈希（digest 绑定代码：确认后改码即拒）。

    脚本按 REAL_ROOT（模块真实项目根）解析——自检期 PROD_ROOT 被覆盖为
    仿生产树时，代码仍在真实项目里。
    """
    p = cli if os.path.isabs(cli) else os.path.join(REAL_ROOT, cli)
    if not os.path.isfile(p):
        raise WriteGateError("SCRIPT_NOT_FOUND: %s" % p)
    return sha256_file(p)


def manifest(root):
    """受护区全树指纹 {rel: sha256}（__pycache__/.pyc 除外）。"""
    out = {}
    for area in PROTECTED:
        base = os.path.join(root, area)
        if not os.path.isdir(base):
            continue
        for dp, dn, fn in os.walk(base):
            dn[:] = sorted(d for d in dn if d != "__pycache__")
            for f in sorted(fn):
                if f.endswith(".pyc"):
                    continue
                p = os.path.join(dp, f)
                rel = os.path.relpath(p, root).replace("\\", "/")
                try:
                    out[rel] = sha256_file(p)
                except OSError as e:
                    out[rel] = "ERR:%s" % e
    return out


def line_count(p):
    try:
        with io.open(p, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return None


_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _arg_value(args, flag):
    """取 `--flag VALUE` 或 `--flag=VALUE` 的 VALUE。**开关式 flag 返回 None。**

    ★ 2026-10-22 修：此前 `--eval --as-of 2026-10-22` 里 `_arg_value(args,"--eval")`
      会把 `--as-of` 当成 `--eval` 的值返回 ⇒ `as_of="--as-of"` ⇒
      登记目标 `data/snapshot/<as_of>/*` 被实例化成 `data/snapshot/--as-of/*`
      （实际看到的就是这个）⇒ **预期清单错、漏写计数 2**。

      这是"开关 flag 吞掉下一个 flag 名"的通用 bug：`--eval`/`--selftest` 这类
      本来就不带值，但取值函数不区分。判据：**下一个 token 以 `--` 开头 ⇒ 本 flag 无值**。
      这不是猜，是命令行解析的常识——值不可能以 `--` 开头（若真有此需求须用 `=` 形式）。
    """
    for i, a in enumerate(args):
        if a == flag:
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                return args[i + 1]
            return None
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def instantiate(patterns, args):
    """落点模式占位符代入（<as_of>/<code>/<out>/<brain>）。"""
    as_of = _arg_value(args, "--eval") or _arg_value(args, "--as-of") \
        or _arg_value(args, "--week-end") or _arg_value(args, "--settle") \
        or _arg_value(args, "--refresh") or _arg_value(args, "--emit") \
        or _arg_value(args, "--build") or _arg_value(args, "--wake") \
        or _arg_value(args, "--date")
    # ★ 2026-10-22 加 `--date`：`fetch_auction.py` / `fetch_news.py` 用 `--date D`
    #   而不是 `--as-of D`，此前 <as_of> 占位符永不代入 ⇒ 落点被当成字面量
    #   `auction_<as_of>.csv` ⇒ 实际写出的 `auction_2026-10-22.csv` 被判「预期外」。
    code = None
    for i, a in enumerate(args):
        if _DATE_RX.match(a) and i + 1 < len(args) and not args[i + 1].startswith("--"):
            code = args[i + 1]
            break
    # 2026-10-19 修：seed_real 的日期形（--reseed <dates> / --from / --to）不在
    # 上表 ⇒ <as_of> 永不代入 ⇒ 运行时 PROD_WRITE_UNEXPECTED 误拒（fail-closed
    # 但可用性断）。补这三源；支持逗号分隔多日期逐日展开（票据与实写一一对应）。
    seed_dates = []
    for flag in ("--reseed", "--from", "--to"):
        v = _arg_value(args, flag)
        if v:
            seed_dates.extend(x.strip() for x in v.split(",") if _DATE_RX.match(x.strip()))
    seed_dates = sorted(set(seed_dates))
    dates = [as_of] if as_of else (seed_dates or [None])
    out = []
    for pat in patterns:
        for d in dates:
            s = pat
            if "<as_of>" in s and d:
                s = s.replace("<as_of>", d)
            if "<code>" in s and code:
                s = s.replace("<code>", code)
            if "<out>" in s:
                v = _arg_value(args, "--convert")
                if v:
                    s = os.path.relpath(_norm(v), PROD_ROOT).replace("\\", "/") \
                        if os.path.isabs(v) else v
            if "<brain>" in s:
                v = _arg_value(args, "--brain")
                s = (os.path.relpath(_norm(v), PROD_ROOT).replace("\\", "/")
                     if v and os.path.isabs(v) else (v or "memory/state/brain_state.md"))
            if s not in out:
                out.append(s)
    return out, {"as_of": as_of, "code": code}


def effective_args(cli, args, root, mode):
    """转发后的最终实参（digest 绑定对象；纯函数，plan/run 共用）。

    INC-WGATE-001 修复①：沙盒模式把 base_flag 拼回 CLI；无 base_flag 的
    CLI 在沙盒模式拒绝（CANNOT_ISOLATE）；args 已含同flag ⇒ BASE_CONFLICT。
    """
    base = os.path.basename(cli)
    args = list(args)
    if mode != "sandbox":
        return args
    if base in STDOUT_ONLY:
        return args
    ent = TARGETS.get(base)
    flag = (ent or {}).get("base_flag")
    if not flag:
        raise WriteGateError(
            "CANNOT_ISOLATE: %s 无沙盒重定向参数（%s）——沙盒模式拒绝运行，"
            "防「以为在沙盒实则写生产」"
            % (base, (ent or {}).get("note") or "生产专用 CLI"))
    if flag in ("--base", "--brain"):
        if flag in args:
            raise WriteGateError(
                "BASE_CONFLICT: args 已含 %s——沙盒根由闸门统一注入，禁双来源"
                "（防包装层吃掉参数事故重演）" % flag)
        return args + [flag, root]
    if flag == "--convert":
        out = _arg_value(args, "--convert")
        if not out:
            raise WriteGateError("NO_OUT: --convert 必须给显式输出路径")
        op = _norm(out)
        if not (op == _norm(root) or op.startswith(_norm(root) + os.sep)):
            raise WriteGateError(
                "OUT_ESCAPE: --convert 目标 %s 在沙盒根 %s 之外" % (op, root))
        return args
    return args


def build_plan(cli, args, root, mode="production", script=None):
    """预期写入清单 + 覆盖判定 + digest（绑定最终实参/根/目标/脚本哈希）。"""
    base = os.path.basename(cli)
    ent = TARGETS.get(base)
    root = _norm(root)
    script = script or (cli if os.path.isabs(cli) else os.path.join(REAL_ROOT, cli))
    script_sha = _script_sha(script)
    # 子命令精确登记优先：`<cli>:<trigger>` 键命中且 args 含该 trigger ⇒ 用其 targets
    # （收窄超集：如 s7 的 fetch 分支不写 <as_of> 快照与 news.jsonl）
    for key, sub in TARGETS.items():
        if ":" not in key or key.split(":", 1)[0] != base:
            continue
        trig = sub.get("trigger") or []
        if trig and any(k in args for k in trig):
            ent = sub
            break
    if ent is None:
        exp, meta, declared = [], {}, False
    else:
        trig = ent.get("trigger") or []
        if trig and not any(k in args for k in trig):
            exp, meta = [], {}
        else:
            exp, meta = instantiate(ent["targets"], args)
        declared = True
    items = []
    for rel in exp:
        p = os.path.join(root, rel.replace("/", os.sep))
        exists = os.path.isfile(p)
        kind = "append" if rel in APPEND_ONLY else ("overwrite" if exists else "new")
        items.append({"rel": rel, "exists": exists, "kind": kind})
    plan = {"schema": SCHEMA_VERSION, "mode": mode, "cli": cli,
            "args": list(args), "root": root, "declared": declared,
            "meta": meta, "expected": items,
            "base_flag": (ent or {}).get("base_flag"),
            "note": (ent or {}).get("note"),
            "script": os.path.relpath(script, REAL_ROOT).replace("\\", "/")
            if script.startswith(REAL_ROOT) else script,
            "script_sha256": script_sha}
    return plan, _digest_of(plan)


def _digest_of(plan):
    core = {k: plan[k] for k in ("schema", "mode", "cli", "args", "root",
                                 "expected", "script_sha256")}
    if isinstance(core.get("expected"), list) and core["expected"] \
            and isinstance(core["expected"][0], dict):
        core["expected"] = [e["rel"] for e in core["expected"]]
    return hashlib.sha256(json.dumps(core, ensure_ascii=False,
                                     sort_keys=True).encode("utf-8")).hexdigest()[:16]


def check_root(root, mode):
    """闸2：目标根确认。"""
    r = _norm(root)
    if not os.path.isdir(r):
        raise WriteGateError("ROOT_NOT_DIR: 目标根不是已存在目录：%s" % r)
    if mode == "production" and r != PROD_ROOT:
        raise WriteGateError("ROOT_MISMATCH: 生产模式目标根 %s ≠ 登记生产根 %s"
                             "（禁相对路径/符号链接/盘符漂移）" % (r, PROD_ROOT))
    return r


def check_confirm(plan, digest, confirm, ack_undeclared):
    """闸1+闸3：显式确认 + 表外 CLI 认领。"""
    if not plan["declared"] and not ack_undeclared:
        raise WriteGateError(
            "UNDECLARED_CLI: %s 不在 TARGETS 写入清单表——生产模式拒绝"
            "（确需运行请显式 --ack-undeclared，原语级守卫仍然生效）"
            % os.path.basename(plan["cli"]))
    if not confirm:
        raise WriteGateError(
            "NO_CONFIRM: 生产模式必须 --confirm-production <digest>"
            "（digest 见 plan 输出；缺参 = 拒绝，零写入）")
    if confirm != digest:
        raise WriteGateError(
            "CONFIRM_MISMATCH: 确认 digest %s ≠ 计划 digest %s"
            "（命令/根/目标/脚本在确认后发生变化——拒绝）" % (confirm, digest))


def _diff(before, after):
    written = [r for r in after if r not in before]
    changed = [r for r in after if r in before and after[r] != before[r]]
    deleted = [r for r in before if r not in after]
    return sorted(written), sorted(changed), sorted(deleted)


def run_gated(cli, args, root, mode, confirm=None, ack_undeclared=False,
              py=None, script=None, env=None, cert_path=None,
              guard_prod=True, echo=True):
    """包装闸执行 + 前后核验。返回证书 dict。"""
    root = check_root(root, mode)
    fargs = effective_args(cli, args, root, mode)      # 转发（纯函数先行）
    plan, digest = build_plan(cli, fargs, root, mode=mode, script=script)
    if mode == "production":
        check_confirm(plan, digest, confirm, ack_undeclared)   # 授权先于路径/执行
    ticket = None
    if mode == "production":
        ticket = {"schema": SCHEMA_VERSION, "mode": "production", "cli": cli,
                  "args": fargs, "root": root, "digest": digest,
                  "expected": [e["rel"] for e in plan["expected"]]}
    before = manifest(root)
    lines_before = {rel: line_count(os.path.join(root, rel.replace("/", os.sep)))
                    for rel in APPEND_ONLY}
    prod_before = manifest(PROD_ROOT) if (mode == "sandbox" and guard_prod) else None
    if script is None:
        script = cli if os.path.isabs(cli) else os.path.join(REAL_ROOT, cli)
    cmd = [py or sys.executable, script] + list(fargs)
    cenv = dict(env or os.environ)
    if ticket:
        cenv[TICKET_ENV] = json.dumps(ticket, ensure_ascii=False)
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=PROD_ROOT, capture_output=True, text=True,
                          env=cenv)
    dt = round(time.time() - t0, 2)

    # ★ 子进程输出回显（2026-10-23 补 · 可观测性）
    #   原先只报计数（写入 N / 漏写 N），子进程 stdout/stderr 被彻底吞掉。
    #   实害：C3 全部 DECIDE_ERROR 时，只能看到「漏写 11」，看不到真正的
    #   `WriteGateError: ...candidates.json` ⇒ 排障要多绕十几分钟。
    #   计数回答「有没有写」，输出回答「为什么没写」，两者都要。
    #   默认回显（可在 plan 里关掉以免刷屏），且**不改判定**：只多打印，不改逻辑。
    if echo:
        _out = (proc.stdout or "").rstrip()
        _err = (proc.stderr or "").rstrip()
        if _out:
            print("  ── 子进程 stdout ──")
            print(_out if len(_out) < _ECHO_MAX else _out[:_ECHO_MAX] + "\n…（截断）")
        if _err:
            print("  ── 子进程 stderr ──")
            print(_err if len(_err) < _ECHO_MAX else _err[:_ECHO_MAX] + "\n…（截断）")
    after = manifest(root)
    written, changed, deleted = _diff(before, after)
    exp_rels = {e["rel"] for e in plan["expected"]}
    actual = set(written) | set(changed)
    # 2026-10-19 修：原用朴素集合差 actual - exp_rels，而预期含 glob 模式
    # （如 data/raw/<as_of>/* 代入后的 data/raw/2026-09-30/*）⇒ 字面量文件永远
    # 减不掉模式 ⇒ 一切目录型写点全被误判「预期外」（子进程内 _covered 的
    # fnmatch 是对的，此处保镖却用集合差，两套判据不一致）。统一走 fnmatch 覆盖。
    def _covered_by(rel, patterns):
        for pat in patterns:
            if fnmatch.fnmatch(rel, pat):
                return True
            if pat.startswith(rel + "/") or fnmatch.fnmatch(rel, pat.split("*")[0].rstrip("/")):
                return True
        return False

    unexpected = sorted(r for r in actual if not _covered_by(r, exp_rels))
    missed = sorted(p for p in exp_rels
                    if not any(_covered_by(r, {p}) for r in actual)) \
        if plan["declared"] else []

    # ★ 漏写语义收口（2026-10-23）：
    #   原 `missed` = 「计划里列了但这次没写」，对**多 stage 的 CLI** 必然误报。
    #   实证：`a_path_daily.py --stage evidence` 只写 `data/input/evidence_<d>.json`
    #   一个文件，但 TARGETS 按整个 CLI 登记了 11 个写点
    #   ⇒ 每次都报「漏写 11」，看起来像严重缺陷，其实是「本 stage 不负责那些写点」。
    #   这在排障时是**噪音**（真正该看的错误被它盖住）。
    #
    #   收口口径：若本次**一个预期写点都没写**（actual 为空），那不是「漏写」，
    #   而是「本 stage 无写点」——单列 `no_write_stage` 提示，`missed` 归零。
    #   反之只要写了至少一个，仍按原语义报漏写（那才是真异常）。
    no_write_stage = bool(exp_rels) and not actual
    if no_write_stage:
        missed = []
    ao_bad = []
    for rel in APPEND_ONLY:
        lb = lines_before.get(rel)
        la = line_count(os.path.join(root, rel.replace("/", os.sep)))
        if lb is not None and la is not None and la < lb:
            ao_bad.append({"rel": rel, "before": lb, "after": la})
    guard_bad = []
    if prod_before is not None:
        gw, gc, gd = _diff(prod_before, manifest(PROD_ROOT))
        guard_bad = sorted(set(gw) | set(gc) | set(gd))
    ok = (proc.returncode == 0 and not unexpected and not deleted
          and not ao_bad and not guard_bad
          and (plan["declared"] or ack_undeclared))
    cert = {"schema": SCHEMA_VERSION, "ok": ok, "mode": mode, "cli": cli,
            "args": fargs, "root": root, "digest": digest,
            "script_sha256": plan["script_sha256"],
            "rc": proc.returncode, "seconds": dt,
            "stdout_tail": (proc.stdout or "")[-2000:],
            "stderr_tail": (proc.stderr or "")[-2000:],
            "n_expected": len(plan["expected"]), "expected": plan["expected"],
            "written": written, "changed": changed, "deleted": deleted,
            "unexpected": unexpected, "missed": missed,
            # 本 stage 无写点（不是漏写）：多 stage CLI 按整个 CLI 登记写点时的正常现象
            "no_write_stage": no_write_stage,
            "append_only_violation": ao_bad,
            "prod_guard_violation": guard_bad}
    if cert_path:
        check_write(cert_path)      # 证书禁落生产受护区
        with io.open(cert_path, "w", encoding="utf-8") as f:
            json.dump(cert, f, ensure_ascii=False, indent=1)
    return cert


def _print_plan(plan, digest):
    print("写入闸 · 计划（SCHEMA_VERSION=%d · mode=%s）"
          % (SCHEMA_VERSION, plan["mode"]))
    print("  CLI      : %s %s" % (plan["cli"], " ".join(plan["args"])))
    print("  目标根   : %s" % plan["root"])
    print("  清单登记 : %s" % ("是" if plan["declared"] else "否（表外 CLI）"))
    print("  脚本     : %s（sha256 %s…）" % (plan["script"],
                                             plan["script_sha256"][:16]))
    if plan.get("base_flag"):
        print("  沙盒重定向: %s（沙盒模式由闸门自动注入）" % plan["base_flag"])
    elif plan["declared"]:
        print("  沙盒重定向: 无——%s" % (plan.get("note") or "生产专用"))
    print("  预期写入 : %d 项" % len(plan["expected"]))
    for e in plan["expected"]:
        print("    [%-8s] %s%s" % (e["kind"], e["rel"],
                                   "（已存在，将覆盖）" if e["exists"] else ""))
    if not plan["expected"]:
        print("    （无——本次调用不产生文件写）")
    print("  digest   : %s" % digest)
    print("  生产模式运行：--confirm-production %s" % digest)


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    mode = None
    base = None
    confirm = None
    ack = False
    cli = None
    args = []
    cert_path = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("plan", "run", "ticket"):
            pass
        elif a == "--cli":
            i += 1
            cli = argv[i]
        elif a == "--args":
            i += 1
            args = argv[i].split()
        elif a == "--base":
            i += 1
            base = argv[i]
        elif a == "--mode":
            i += 1
            mode = argv[i]
        elif a == "--confirm-production":
            i += 1
            confirm = argv[i]
        elif a == "--ack-undeclared":
            ack = True
        elif a == "--cert":
            i += 1
            cert_path = argv[i]
        else:
            raise WriteGateError("UNKNOWN_ARG: %s" % a)
        i += 1
    if not cli:
        raise WriteGateError("NO_CLI: 必须 --cli <tools/... 路径>")
    if base:
        mode = "sandbox"
    elif mode is None:
        mode = "production"
    root = base or PROD_ROOT
    # plan 也走转发后的最终实参（digest 与 run 完全一致）
    fargs = effective_args(cli, args, root, mode)
    plan, digest = build_plan(cli, fargs, root, mode=mode)
    if "plan" in argv or "ticket" in argv:
        _print_plan(plan, digest)
        return 0
    cert = run_gated(cli, args, root, mode, confirm=confirm,
                     ack_undeclared=ack, cert_path=cert_path)
    print("写入闸 · 结果 ok=%s（rc=%s · %ss）" % (cert["ok"], cert["rc"],
                                                 cert["seconds"]))
    print("  写入 %d · 变更 %d · 删除 %d · 预期外 %d · 漏写 %d · 账本违规 %d · 生产保镖违例 %d"
          % (len(cert["written"]), len(cert["changed"]), len(cert["deleted"]),
             len(cert["unexpected"]), len(cert["missed"]),
             len(cert["append_only_violation"]),
             len(cert["prod_guard_violation"])))
    for rel in cert["unexpected"]:
        print("  [!!] 预期外写入: %s" % rel)
    for rel in cert["deleted"]:
        print("  [!!] 受护区删除: %s" % rel)
    for x in cert["append_only_violation"]:
        print("  [!!] 账本行数减少: %s" % x["rel"])
    for rel in cert["prod_guard_violation"]:
        print("  [!!] 沙盒运行触碰生产: %s" % rel)
    if cert["rc"] != 0:
        print("  [!!] 子进程退出码 %s；stderr 尾部：%s"
              % (cert["rc"], (cert["stderr_tail"] or "")[-300:]))
    if cert_path:
        print("  证书: %s" % cert_path)
    return 0 if cert["ok"] else 1


# ═══════════════════════════════════════════════════════════════════════
# 自检（全沙盒：仿生产树 + 双替身；覆盖 8 大强制场景 + 回归负例）
# ═══════════════════════════════════════════════════════════════════════
def _selftest():
    """全沙盒自检。自检期把 PROD_ROOT 覆盖为仿生产树、沙盒根另设——
    生产保镖与原语级守卫因此能在**不碰真生产**的前提下被完整验证。"""
    global PROD_ROOT
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

    tmp = tempfile.mkdtemp(prefix="wgate_")
    fake_root = os.path.join(tmp, "fake_prod")
    sb = os.path.join(tmp, "sb")
    for d in ("data/state", "data/generated/reports", "memory/state"):
        os.makedirs(os.path.join(fake_root, d), exist_ok=True)
        os.makedirs(os.path.join(sb, d), exist_ok=True)
    with io.open(os.path.join(fake_root, "data", "state", "run_log.jsonl"),
                 "w", encoding="utf-8") as f:
        f.write('{"old":1}\n')
    with io.open(os.path.join(fake_root, "memory", "state", "brain_state.md"),
                 "w", encoding="utf-8") as f:
        f.write("brain v0\n")
    with io.open(os.path.join(fake_root, "data", "generated", "reports",
                              "e8_2026-10-14.md"), "w", encoding="utf-8") as f:
        f.write("旧版报告\n")          # 覆盖场景夹具
    # 双替身（S1 起用）：把 --base 之后的目录当根，写 e8_<as_of>.md
    twin = os.path.join(tmp, "twin.py")
    with io.open(twin, "w", encoding="utf-8") as f:
        f.write(
            "import io, os, sys\n"
            "a = sys.argv[1:]\n"
            "root = a[a.index('--base') + 1] if '--base' in a else os.environ['WG_ROOT']\n"
            "p = os.path.join(root, 'data', 'generated', 'reports',"
            " 'e8_2026-10-14.md')\n"
            "os.makedirs(os.path.dirname(p), exist_ok=True)\n"
            "io.open(p, 'w').write('新版')\n"
        )
    # S1/S6 覆盖场景夹具：沙盒根预置旧版报告
    with io.open(os.path.join(sb, "data", "generated", "reports",
                              "e8_2026-10-14.md"), "w", encoding="utf-8") as f:
        f.write("沙盒旧版\n")
    _real_prod = PROD_ROOT
    PROD_ROOT = fake_root
    try:
        print("== S0 计划与 digest ==")
        plan, digest = build_plan("tools/e2e/e8_quality.py",
                                  ["--eval", "2026-10-14", "sh600519"],
                                  fake_root, mode="sandbox")
        T("S0.1 预期写入实例化正确（e8_<as_of>.md · overwrite）",
          len(plan["expected"]) == 1
          and plan["expected"][0]["rel"] == "data/generated/reports/e8_2026-10-14.md"
          and plan["expected"][0]["kind"] == "overwrite")
        T("S0.2 digest 16 位且绑定脚本哈希",
          len(digest) == 16 and plan["script_sha256"])
        d2 = build_plan("tools/e2e/e8_quality.py",
                        ["--eval", "2026-10-15", "sh600519"],
                        fake_root, mode="sandbox")[1]
        T("S0.3 计划敏感（改 as_of ⇒ digest 变）", d2 != digest)
        T("S0.4 脚本哈希敏感（改码 ⇒ digest 变）",
          build_plan("tools/e2e/e8_quality.py",
                     ["--eval", "2026-10-14", "sh600519"], fake_root,
                     mode="sandbox", script=twin)[1] != digest)

        print("== S1 正常沙盒写入（场景①）==")
        cert = run_gated("tools/e2e/e8_quality.py",
                         ["--eval", "2026-10-14", "sh600519"], sb,
                         "sandbox", script=twin,
                         env=dict(os.environ, WG_ROOT=sb),
                         cert_path=os.path.join(tmp, "cert1.json"))
        T("S1.1 沙盒转发 --base；写入落沙盒；ok=True",
          cert["ok"] and cert["rc"] == 0
          and ("data/generated/reports/e8_2026-10-14.md"
               in cert["written"] + cert["changed"])
          and cert["args"][-2:] == ["--base", os.path.normpath(sb)])
        T("S1.2 覆盖形态登记（expected kind=overwrite）",
          cert["expected"][0]["kind"] == "overwrite")

        print("== S2 缺少确认（场景②）==")
        try:
            run_gated("tools/e2e/e8_quality.py",
                      ["--eval", "2026-10-14", "sh600519"], fake_root,
                      "production", confirm=None, py=sys.executable)
            T("S2.1 缺 confirm → 拒绝", False, "竟放行")
        except WriteGateError as e:
            T("S2.1 缺 confirm → NO_CONFIRM 拒绝", "NO_CONFIRM" in str(e))
        try:
            run_gated("tools/e2e/e8_quality.py",
                      ["--eval", "2026-10-14", "sh600519"], fake_root,
                      "production", confirm="deadbeefdeadbeef", py=sys.executable)
            T("S2.2 错 confirm → 拒绝", False, "竟放行")
        except WriteGateError as e:
            T("S2.2 错 confirm → CONFIRM_MISMATCH 拒绝",
              "CONFIRM_MISMATCH" in str(e))

        print("== S3 目标路径不匹配（场景③·原语级）==")
        # 替身：写票据预期之外的生产路径（rogue）
        twin_rogue = os.path.join(tmp, "twin_rogue.py")
        with io.open(twin_rogue, "w", encoding="utf-8") as f:
            f.write(
                "import io, os, sys\n"
                "sys.path.insert(0, %r)\n"
                "import writegate as WG\n"
                "WG.check_write(os.path.join(WG.PROD_ROOT, 'data', 'state',"
                " 'rogue.json'))\n"
                "io.open(os.path.join(WG.PROD_ROOT, 'data', 'state',"
                " 'rogue.json'), 'w').write('x')\n" % os.path.dirname(
                    os.path.abspath(__file__))
            )
        # 有票（经 env 传播）但目标在预期外 ⇒ PROD_WRITE_UNEXPECTED
        tk = {"schema": SCHEMA_VERSION, "mode": "production",
              "cli": "tools/e2e/e8_quality.py",
              "args": ["--eval", "2026-10-14", "sh600519"],
              "root": fake_root, "digest": "x",
              "expected": ["data/generated/reports/e8_2026-10-14.md"]}
        rc1 = subprocess.run([sys.executable, twin_rogue],
                             capture_output=True, text=True,
                             env=dict(os.environ, WG_ROOT=fake_root,
                                      WB_WGATE_TICKET=json.dumps(tk)))
        T("S3.1 有票但目标在预期外 → PROD_WRITE_UNEXPECTED 拒绝",
          rc1.returncode != 0 and "PROD_WRITE_UNEXPECTED" in (rc1.stderr or ""))
        # 无票直连写生产 ⇒ PROD_WRITE_NO_TICKET（场景④：缺少 --base/绕过入口）
        rc2 = subprocess.run([sys.executable, twin_rogue],
                             capture_output=True, text=True,
                             env=dict(os.environ, WG_ROOT=fake_root))
        T("S3.2 无票直连写生产 ⇒ PROD_WRITE_NO_TICKET 拒绝",
          rc2.returncode != 0 and "PROD_WRITE_NO_TICKET" in (rc2.stderr or ""))
        T("S3.3 拒绝后 rogue.json 未落盘",
          not os.path.exists(os.path.join(fake_root, "data", "state",
                                          "rogue.json")))

        print("== S4 参数转发丢失（场景⑤·INC-WGATE-001 回归）==")
        # 替身不理 --base，恒写 WG_ROOT（=仿生产树）
        twin_bad = os.path.join(tmp, "twin_bad.py")
        with io.open(twin_bad, "w", encoding="utf-8") as f:
            f.write(
                "import io, os, sys\n"
                "p = os.path.join(os.environ['WG_ROOT'], 'data', 'generated',"
                " 'reports', 'e8_2026-10-14.md')\n"
                "io.open(p, 'w').write('leak')\n"
            )
        cert_b = run_gated("tools/e2e/e8_quality.py",
                           ["--eval", "2026-10-14", "sh600519"], sb,
                           "sandbox", script=twin_bad,
                           env=dict(os.environ, WG_ROOT=fake_root))
        T("S4.1 CLI 不认 --base 恒写仿生产树 ⇒ ok=False 且生产保镖抓住",
          not cert_b["ok"]
          and "data/generated/reports/e8_2026-10-14.md"
          in cert_b["prod_guard_violation"])
        # 转发本身：effective_args 把 --base 拼在末尾（digest 绑定它）
        fa = effective_args("tools/e2e/e8_quality.py",
                            ["--eval", "2026-10-14", "sh600519"], sb, "sandbox")
        T("S4.2 effective_args 转发 --base（digest 绑定最终实参）",
          fa[-2:] == ["--base", os.path.normpath(sb)])
        try:
            effective_args("tools/e2e/seed_real.py", ["--stage", "2026-10-14"],
                           sb, "sandbox")
            T("S4.3 无 base_flag CLI 沙盒模式 → 拒绝", False, "竟放行")
        except WriteGateError as e:
            T("S4.3 无 base_flag CLI 沙盒模式 → CANNOT_ISOLATE 拒绝",
              "CANNOT_ISOLATE" in str(e))
        try:
            effective_args("tools/e2e/e8_quality.py",
                           ["--eval", "2026-10-14", "sh600519", "--base", "/x"],
                           sb, "sandbox")
            T("S4.4 args 自带 --base（双来源）→ 拒绝", False, "竟放行")
        except WriteGateError as e:
            T("S4.4 args 自带 --base（双来源）→ BASE_CONFLICT 拒绝",
              "BASE_CONFLICT" in str(e))

        print("== S5 试图绕过统一入口（场景⑥）==")
        # 5.1 直连替身写仿生产树（无票、无闸门）——已在 S3.2 覆盖，这里补
        #    「有票但经环境变量篡改 widen expected」仍然被原语级拒绝的形态：
        _set_ticket({"schema": SCHEMA_VERSION, "mode": "sandbox",
                     "cli": "twin", "args": [], "root": sb,
                     "digest": "x", "expected": []})
        try:
            check_write(os.path.join(fake_root, "data", "state", "rogue.json"))
            T("S5.1 沙盒票写生产受护区 → 拒", False, "竟放行")
        except WriteGateError as e:
            T("S5.1 沙盒票写生产受护区 → PROD_WRITE_NO_TICKET 拒绝",
              "PROD_WRITE_NO_TICKET" in str(e))
        _set_ticket(None)
        # 5.2 --ack-undeclared 不解除原语级守卫
        twin_decl = os.path.join(tmp, "twin_decl.py")
        with io.open(twin_decl, "w", encoding="utf-8") as f:
            f.write(
                "import io, os, sys\n"
                "sys.path.insert(0, %r)\n"
                "import writegate as WG\n"
                "WG.check_write(os.path.join(WG.PROD_ROOT, 'data', 'state',"
                " 'rogue.json'))\n" % os.path.dirname(os.path.abspath(__file__))
            )
        rc = subprocess.run([sys.executable, twin_decl], capture_output=True,
                            text=True, env=dict(os.environ, WG_ROOT=fake_root))
        T("S5.2 表外/未登基础写仍被原语级守卫拒（ack 不松闸）",
          rc.returncode != 0 and "PROD_WRITE_NO_TICKET" in (rc.stderr or ""))

        print("== S6 目标文件覆盖（场景⑦）==")
        # 沙盒覆盖已存在文件 ⇒ ok=True 且 kind=overwrite 明示
        with io.open(os.path.join(sb, "data", "generated", "reports",
                                  "e8_2026-10-14.md"), "w", encoding="utf-8") as f:
            f.write("沙盒旧版\n")
        cert_ov = run_gated("tools/e2e/e8_quality.py",
                            ["--eval", "2026-10-14", "sh600519"], sb,
                            "sandbox", script=twin,
                            env=dict(os.environ, WG_ROOT=sb))
        T("S6.1 沙盒覆盖已存在文件 ⇒ ok=True 且 changed 明示",
          cert_ov["ok"]
          and "data/generated/reports/e8_2026-10-14.md" in cert_ov["changed"])
        # 生产覆盖必须有确认（无确认连计划都不放行）
        try:
            run_gated("tools/e2e/e8_quality.py",
                      ["--eval", "2026-10-14", "sh600519"], fake_root,
                      "production", confirm=None, py=sys.executable)
            T("S6.2 生产覆盖无确认 → 拒", False, "竟放行")
        except WriteGateError as e:
            T("S6.2 生产覆盖无确认 → NO_CONFIRM 拒绝", "NO_CONFIRM" in str(e))

        print("== S7 异常退出（场景⑧）==")
        twin_fail = os.path.join(tmp, "twin_fail.py")
        with io.open(twin_fail, "w", encoding="utf-8") as f:
            f.write("import sys\nsys.exit(3)\n")
        cert_f = run_gated("tools/e2e/e8_quality.py",
                           ["--eval", "2026-10-14", "sh600519"], sb,
                           "sandbox", script=twin_fail)
        T("S7.1 子进程 rc≠0 → ok=False（不静默）",
          not cert_f["ok"] and cert_f["rc"] == 3)

        print("== S8 账本与删除 ==")
        twin_ao = os.path.join(tmp, "twin_ao.py")
        with io.open(twin_ao, "w", encoding="utf-8") as f:
            f.write("import io,os\n"
                    "io.open(os.path.join(os.environ['WG_ROOT'],'data','state',"
                    "'run_log.jsonl'),'a').write('{\"new\":1}\\n')\n")
        lb = line_count(os.path.join(fake_root, "data", "state", "run_log.jsonl"))
        subprocess.run([sys.executable, twin_ao], cwd=PROD_ROOT,
                       capture_output=True,
                       env=dict(os.environ, WG_ROOT=fake_root))
        la = line_count(os.path.join(fake_root, "data", "state", "run_log.jsonl"))
        T("S8.1 append-only 账本行数只增不减（%d → %d）" % (lb, la), la > lb)
        m0 = manifest(fake_root)
        T("S8.2 负例全程仿生产树零 rogue 写入",
          "data/state/rogue.json" not in m0)

        print("== S9 纪律 ==")
        src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
        T("S9.1 本件无网络导入",
          not re.search(r"^\s*(import|from)\s+(requests|urllib|socket|openai)\b",
                        src, re.M))
        T("S9.2 生产模式确认判据是计划 digest（非固定口令）",
          "confirm != digest" in src)
        T("S9.3 原语级守卫在场（check_write 导出且写点必过）",
          "def check_write" in src)
        T("S9.4 沙盒模式有生产保镖", "prod_guard_violation" in src)

        # S10 开关式 flag 不得吞掉下一个 flag 名（2026-10-22 实测踩过）
        #   事故：`--eval --as-of 2026-10-22` ⇒ as_of="--as-of"
        #   ⇒ 落点实例化成 `data/snapshot/--as-of/*` ⇒ 预期清单错、漏写 2。
        T("S10.1 开关 flag 无值时返回 None（不吞下一 flag 名）",
          _arg_value(["--eval", "--as-of", "2026-10-22"], "--eval") is None,
          str(_arg_value(["--eval", "--as-of", "2026-10-22"], "--eval")))
        T("S10.2 带值 flag 正常取值",
          _arg_value(["--as-of", "2026-10-22"], "--as-of") == "2026-10-22")
        T("S10.3 --flag=VALUE 形式仍可用",
          _arg_value(["--as-of=2026-10-22"], "--as-of") == "2026-10-22")
        _inst = instantiate(["data/snapshot/<as_of>/*"],
                            ["--eval", "--as-of", "2026-10-22"])
        _inst_paths = _inst[0] if isinstance(_inst, tuple) else _inst
        T("S10.4 真实调用里 as_of 代入正确（不再出现 --as-of 目录）",
          _inst_paths == ["data/snapshot/2026-10-22/*"], str(_inst_paths))
    finally:
        PROD_ROOT = _real_prod
        _set_ticket(None)
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：%s" % " / ".join(fails))
    return 1 if fail else 0


if __name__ == "__main__":
    try:
        sys.exit(_main(sys.argv[1:]))
    except WriteGateError as e:
        print("⛔ 写入闸拒绝：%s" % e, file=sys.stderr)
        sys.exit(2)
