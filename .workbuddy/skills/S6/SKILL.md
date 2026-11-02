# S6 估值定价判断 · 技能入口（分析能力）

> 版本 1.0.0 · 2026-10-19 开插批次生成 · 注册表 id=S6 · 母环节挂载=C3
> 本文件 = 注册表落点约定的**技能入口**（`impl_exists` 判据：`.workbuddy/skills/<ID>/SKILL.md`）。
> 全部事实字段派生自注册表条目与执行件 docstring，零编造；方法论叙事见平台技能区（s1-event-analysis / c4-three-q / trade-rules 等描述性技能）。

## 是什么

S6 估值定价判断（2026-10-13 · 第一阶段补齐：契约 + 降级，核心待数据源）。

## 执行件

- `tools/analysis/s6.py`
- 自检：`python tools/analysis/s6.py --selftest`

## 产出与消费者

- 产出：估值法 / 市场交易什么预期 / 重估风险
- 输出落点：data/generated/s_s6_<as_of>.json

## 知识真源

- `data/knowledge/s6/`（注册表 knowledge_route；经 `loader.load_knowledge` 读取）

## 边界（注册表锁死）

- no_decision=True · can_order=False

- S/E 系总规则：只供专业判断素材，不越权决策、不下单（决策 = C4 三问，执行 = C5 硬闸）。

## 触发

- 三问②估赔率


## 上游（consumes）

行业估值数据
