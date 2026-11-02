# E7 个股深度研究 · 技能入口（入口报告）

> 版本 1.0.0 · 2026-10-19 开插批次生成 · 注册表 id=E7 · 母环节挂载=C3
> 本文件 = 注册表落点约定的**技能入口**（`impl_exists` 判据：`.workbuddy/skills/<ID>/SKILL.md`）。
> 全部事实字段派生自注册表条目与执行件 docstring，零编造；方法论叙事见平台技能区（s1-event-analysis / c4-three-q / trade-rules 等描述性技能）。

## 是什么

E7 个股研究装配（2026-10-13 · 第一阶段补齐）。

## 执行件

- `tools/e2e/e7_research.py`
- 自检：`python tools/e2e/e7_research.py --selftest`

## 产出与消费者

- 产出：驱动/位置/赔率/风险/证伪线
- 输出落点：data/generated/reports/e7_<as_of>.md

## 知识真源

- （注册表 knowledge_route=null——该能力无结构化知识库；判据在执行件/params）

## 边界（注册表锁死）

- no_decision=True · can_order=False

- S/E 系总规则：只供专业判断素材，不越权决策、不下单（决策 = C4 三问，执行 = C5 硬闸）。

## 触发

- 点名 / QUALIFIED 需深挖
- 入口：点名 / QUALIFIED 需深挖 / I4

## 上游（consumes）

个股数据 / Thesis
