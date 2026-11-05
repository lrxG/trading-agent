# 交易智能体 · 平台入口（WorkBuddy 每会话自动注入）

> **本文件只放「去哪读」，不放规则内容** —— 不放内容是为了不与内核形成第二真源。
> 真源：`AGENT.md`（内核行为）· `data/state/capability_registry.json`（能力登记，机器读）· `.workbuddy/skills/`（判断本体）
> 落点依据：WorkBuddy 平台**注入项目根 `CODEBUDDY.md`**（`memoryManager` PROJECT scope → `system-reminder-md`），**不注入 `AGENT.md`** —— 故内核入口必须落在本文件。

## 第一动作

**任何判断或动作前，先读 `AGENT.md`**（内核：主循环唯一来源 · 三锁 · 落点封闭 · 风控权限 · 七入口 I1~I7 · Thesis 认知主线 · 三问准入 · 骨架/插件纪律）。
`AGENT.md` 已落盘（0.11 · v1.2），同时是 `tools/fs/paths.py` 的项目根锚文件。

**追平优先**：若游标落后于今天（`python tools/fs/wakeup.py` 一看便知），先跑
`python tools/fs/wakeup.py --wake <today>` 把时间对齐，**再**谈判断——别拿旧时间做新判断。

**学习不占实盘时间**（2026-10-23 用户令 · 骨架 v1.5.0）：自我进化走历史回测，实盘只产利润。
每周跑一次 `python tools/learning/learn_pipeline.py --weekly`（带周度闸，本周已跑会自动跳过；
强制重跑加 `--force`；只看报告用 `--report`）。它把「真实历史回放 → Case → Obs → Demand
→ 每周 5 指标」串成 C6 生产入口，产物落 `cases.jsonl` / `obs_candidates.jsonl` /
`demand_queue.jsonl`。**它不产 Rule**——Rule 仍须 `verify` + 用户批准。
唤醒输出顶部自带三项自检（上一日是否结账 / 未处理 FAIL / STALL）。

## 4 条不可绕过（只声明，内容在各真源）

1. **判断类问题**（为什么 / 买什么 / 能不能 / 怎么走）→ 走内核完整调度，禁单点作答，禁拿单个统计数字当答案。
2. **三锁** → 信息截断（取数 ≤ 游标日 T）· 决策冻结（下注前定格判据，禁后视改写）· 判定机械化（结算按冻结判据机械判，不靠文字复盘）。
3. **落点封闭** → 知识 / 结论 / 研究 / 假设只进 5 落点，其余路径不存在（清单见设计 §6）。
4. **硬风控独立** → 熔断 / 单票顶 / 仓位上限只存 `data/params.json`，AI 无权自改（走候选 → 验证 → 用户批准）。

## 系统图纸

| 要什么 | 去哪 |
| --- | --- |
| **内核全文（唯一真源）** | `AGENT.md`（本文件只放指针，**不放规则**） |
| 骨架与插件分层 · **全项目文件结构** | `AGENT.md` §4.1 调用关系 + `tools/` 三子目录（analysis / e2e / fs） |
| 能力登记（唯一真源，禁第二份清单） | `data/state/capability_registry.json` |
| 骨架冻结台账（改骨架 = 显式动作） | `data/state/skeleton_manifest.json` |
| 当前施工到哪 | `data/state/capability_registry.json` 各能力 `state` 字段 |
| 每日运转留痕 / 失败与空转 | `data/state/run_log.jsonl`（`python tools/fs/observe.py`） |
| 认知状态（判断类问题先整读） | `memory/state/brain_state.md` · `memory/state/positions.md` |
| 平台技能（S/E 插件） | `.workbuddy/skills/<id>/SKILL.md` |
| 项目工作记忆 | `.workbuddy/memory/MEMORY.md` + `.workbuddy/memory/YYYY-MM-DD.md` |
| **成文报告模板（交付前必读，缺章=未完成）** | `templates/产业趋势研究模板_v2.md`（产业趋势研究·填充骨架 + 11 条自检门）· `templates/盘前预案模板.md`（九章契约）· `templates/盘后复盘模板_v1.md` · `templates/业绩研究模板_v1.md` |
