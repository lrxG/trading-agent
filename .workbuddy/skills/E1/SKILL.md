# E1 盘前预案 · 技能入口（入口报告）

> 版本 1.0.0 · 2026-10-19 开插批次生成 · 注册表 id=E1 · 母环节挂载=C1
> 本文件 = 注册表落点约定的**技能入口**（`impl_exists` 判据：`.workbuddy/skills/<ID>/SKILL.md`）。
> 全部事实字段派生自注册表条目与执行件 docstring，零编造；方法论叙事见平台技能区（s1-event-analysis / c4-three-q / trade-rules 等描述性技能）。

## 是什么

premarket.py · E1 盘前预案（能力落地矩阵 E1 · registry id=E1 · 2026-10-13 建）

## 执行件

- `tools/decision/premarket.py`
- 自检：`python tools/decision/premarket.py --selftest`

## 产出与消费者

- 产出：L0 作战卡：定调/持仓动作/候选/假设
- 输出落点：data/generated/reports/e1_<as_of>.md

## 知识真源

- （注册表 knowledge_route=null——该能力无结构化知识库；判据在执行件/params）

## 边界（注册表锁死）

- no_decision=True · can_order=False

- S/E 系总规则：只供专业判断素材，不越权决策、不下单（决策 = C4 三问，执行 = C5 硬闸）。

## 触发

- 定时（盘前）/ 主动
- 入口：定时：盘前 / I1

## 上游（consumes）

环境档 / 候选池 / 持仓
