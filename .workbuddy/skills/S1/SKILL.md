# S1 事件催化评估 · 技能入口（分析能力）

> 版本 1.0.0 · 2026-10-19 开插批次生成 · 注册表 id=S1 · 母环节挂载=C3
> 本文件 = 注册表落点约定的**技能入口**（`impl_exists` 判据：`.workbuddy/skills/<ID>/SKILL.md`）。
> 全部事实字段派生自注册表条目与执行件 docstring，零编造；方法论叙事见平台技能区（s1-event-analysis / c4-three-q / trade-rules 等描述性技能）。

## 是什么

s1.py · S1 事件催化评估执行件（施工 2.1 · S1 批 · v1.0 · 2026-10-09 建）

## 执行件

- `tools/analysis/s1.py`
- 自检：`python tools/analysis/s1.py --selftest`

## 产出与消费者

- 产出：催化类型 / 兑现路径 / 时间窗口 / 已定价程度
- 输出落点：data/generated/s_s1_<as_of>.json

## 知识真源

- `data/knowledge/s1_event_effects/`（注册表 knowledge_route；经 `loader.load_knowledge` 读取）

## 边界（注册表锁死）

- no_decision=True · can_order=False

- S/E 系总规则：只供专业判断素材，不越权决策、不下单（决策 = C4 三问，执行 = C5 硬闸）。

## 触发

- 三问①评 A 事件催化


## 上游（consumes）

事件归档 / 消息
