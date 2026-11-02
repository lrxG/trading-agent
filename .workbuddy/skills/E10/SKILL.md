# E10 每日投研简报 · 技能入口（入口报告）

> 版本 1.0.0 · 2026-10-19 开插批次生成 · 注册表 id=E10 · 母环节挂载=C1
> 本文件 = 注册表落点约定的**技能入口**（`impl_exists` 判据：`.workbuddy/skills/<ID>/SKILL.md`）。
> 全部事实字段派生自注册表条目与执行件 docstring，零编造；方法论叙事见平台技能区（s1-event-analysis / c4-three-q / trade-rules 等描述性技能）。

## 是什么

E10 每日投研简报（2026-10-13 · 第一阶段补齐）。

## 执行件

- `tools/e2e/e10_daily.py`
- 自检：`python tools/e2e/e10_daily.py --selftest`

## 产出与消费者

- 产出：主线/异动/消息/今日看什么
- 输出落点：data/generated/reports/e10_daily.jsonl

## 知识真源

- （注册表 knowledge_route=null——该能力无结构化知识库；判据在执行件/params）

## 边界（注册表锁死）

- no_decision=True · can_order=False
- output_path 2026-10-14 同步：实际实现 = E3 同款 append-only jsonl（非 per-day md）
- S/E 系总规则：只供专业判断素材，不越权决策、不下单（决策 = C4 三问，执行 = C5 硬闸）。

## 触发

- 要「今日市场梳理」
- 入口：要每日梳理

## 上游（consumes）

盘面 / 消息
