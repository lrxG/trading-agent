# E6 周度报告 · 技能入口（入口报告）

> 版本 1.0.0 · 2026-10-19 开插批次生成 · 注册表 id=E6 · 母环节挂载=C6
> 本文件 = 注册表落点约定的**技能入口**（`impl_exists` 判据：`.workbuddy/skills/<ID>/SKILL.md`）。
> 全部事实字段派生自注册表条目与执行件 docstring，零编造；方法论叙事见平台技能区（s1-event-analysis / c4-three-q / trade-rules 等描述性技能）。

## 是什么

e6_weekly.py · E6 周度报告生产者（任务9#9 · v1.0 · 2026-10-12 建）

## 执行件

- `tools/e2e/e6_weekly.py`
- 自检：`python tools/e2e/e6_weekly.py --selftest`

## 产出与消费者

- 产出：7 段周报（净值基准/判断质量/重复错误/Case 召回/Rule OOS/跑输归因展示/待批准候选）
- 输出落点：data/generated/reports/e6_weekly.jsonl

## 知识真源

- （注册表 knowledge_route=null——该能力无结构化知识库；判据在执行件/params）

## 边界（注册表锁死）

- no_decision=True · can_order=False

- S/E 系总规则：只供专业判断素材，不越权决策、不下单（决策 = C4 三问，执行 = C5 硬闸）。

## 触发

- 周校准节内
- 入口：周度校准节

## 上游（consumes）

净值账本 / 判断质量 / C6 产物（cases/rules/verify/queue）
