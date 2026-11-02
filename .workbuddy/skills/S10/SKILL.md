# S10 竞价/一字板结构 · 技能入口（分析能力）

> 版本 1.0.0 · 2026-10-19 开插批次生成 · 注册表 id=S10 · 母环节挂载=C2
> 本文件 = 注册表落点约定的**技能入口**（`impl_exists` 判据：`.workbuddy/skills/<ID>/SKILL.md`）。
> 全部事实字段派生自注册表条目与执行件 docstring，零编造；方法论叙事见平台技能区（s1-event-analysis / c4-three-q / trade-rules 等描述性技能）。

## 是什么

S10 竞价/一字板结构（2026-10-13 第一阶段补齐 · 2026-10-19 接旧链 G 研究知识库）。

## 执行件

- `tools/analysis/s10.py`
- 自检：`python tools/analysis/s10.py --selftest`

## 产出与消费者

- 产出：竞价强弱 / 当日/次日溢价 / 封板/炸板/撬板
- 输出落点：data/generated/s_s10_<as_of>.json

## 知识真源

- `data/knowledge/s10_auction/`（注册表 knowledge_route；经 `loader.load_knowledge` 读取）

## 边界（注册表锁死）

- no_decision=True · can_order=False

- S/E 系总规则：只供专业判断素材，不越权决策、不下单（决策 = C4 三问，执行 = C5 硬闸）。

## 触发

- 盘前竞价 / 盘中一字板


## 上游（consumes）

集合竞价 / 封单
