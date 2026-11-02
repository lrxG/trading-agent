# S5 资金筹码结构 · 技能入口（分析能力）

> 版本 1.0.0 · 2026-10-19 开插批次生成 · 注册表 id=S5 · 母环节挂载=C3
> 本文件 = 注册表落点约定的**技能入口**（`impl_exists` 判据：`.workbuddy/skills/<ID>/SKILL.md`）。
> 全部事实字段派生自注册表条目与执行件 docstring，零编造；方法论叙事见平台技能区（s1-event-analysis / c4-three-q / trade-rules 等描述性技能）。

## 是什么

s5.py · S5 资金筹码结构（任务9 · v1.0 · 2026-10-12 建）

## 执行件

- `tools/analysis/s5.py`
- 自检：`python tools/analysis/s5.py --selftest`

## 产出与消费者

- 产出：资金行为模式 / 席位结构(代理) / 筹码状态
- 输出落点：data/generated/s_s5_<as_of>.json

## 知识真源

- `data/knowledge/s5_capital_structure/`（注册表 knowledge_route；经 `loader.load_knowledge` 读取）

## 边界（注册表锁死）

- no_decision=True · can_order=False

- S/E 系总规则：只供专业判断素材，不越权决策、不下单（决策 = C4 三问，执行 = C5 硬闸）。

## 触发

- 三问①评 B 资金


## 上游（consumes）

主力资金流(main/jumbo/mid/small/block) / 换手率 / 筹码分布 / 龙虎榜(机构榜/活跃席位)
