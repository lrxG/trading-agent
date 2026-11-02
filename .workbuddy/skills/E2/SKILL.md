# E2 盘中扫描 · 技能入口（入口报告）

> 版本 1.0.0 · 2026-10-19 开插批次生成 · 注册表 id=E2 · 母环节挂载=C4
> 本文件 = 注册表落点约定的**技能入口**（`impl_exists` 判据：`.workbuddy/skills/<ID>/SKILL.md`）。
> 全部事实字段派生自注册表条目与执行件 docstring，零编造；方法论叙事见平台技能区（s1-event-analysis / c4-three-q / trade-rules 等描述性技能）。

## 是什么

E2 盘中扫描（2026-10-13 · 第一阶段补齐：契约 + 降级）。

## 执行件

- `tools/e2e/e2_intraday.py`
- 自检：`python tools/e2e/e2_intraday.py --selftest`

## 产出与消费者

- 产出：BUY/WAIT/NO 建议 + 触发线
- 输出落点：data/generated/reports/e2_<as_of>.md

## 知识真源

- （注册表 knowledge_route=null——该能力无结构化知识库；判据在执行件/params）

## 边界（注册表锁死）

- no_decision=True · can_order=False

- S/E 系总规则：只供专业判断素材，不越权决策、不下单（决策 = C4 三问，执行 = C5 硬闸）。

## 触发

- 盘中提问 / T2 异动
- 入口：盘中提问 / I7

## 上游（consumes）

盘中异动 / Thesis
