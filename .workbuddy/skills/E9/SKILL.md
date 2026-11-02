# E9 持仓组合诊断 · 技能入口（入口报告）

> 版本 1.0.0 · 2026-10-19 开插批次生成 · 注册表 id=E9 · 母环节挂载=C5
> 本文件 = 注册表落点约定的**技能入口**（`impl_exists` 判据：`.workbuddy/skills/<ID>/SKILL.md`）。
> 全部事实字段派生自注册表条目与执行件 docstring，零编造；方法论叙事见平台技能区（s1-event-analysis / c4-three-q / trade-rules 等描述性技能）。

## 是什么

E9 组合诊断（2026-10-13 · 第一阶段补齐）。

## 执行件

- `tools/e2e/e9_portfolio.py`
- 自检：`python tools/e2e/e9_portfolio.py --selftest`

## 产出与消费者

- 产出：敞口/集中度/相关性/环境匹配
- 输出落点：data/generated/reports/e9_<as_of>.md

## 知识真源

- （注册表 knowledge_route=null——该能力无结构化知识库；判据在执行件/params）

## 边界（注册表锁死）

- no_decision=True · can_order=False

- S/E 系总规则：只供专业判断素材，不越权决策、不下单（决策 = C4 三问，执行 = C5 硬闸）。

## 触发

- 发持仓问「有无隐患」
- 入口：发持仓问隐患 / I6

## 上游（consumes）

持仓组合
