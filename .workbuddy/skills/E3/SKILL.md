# E3 盘后复盘 · 技能入口（入口报告）

> 版本 1.0.0 · 2026-10-19 开插批次生成 · 注册表 id=E3 · 母环节挂载=C6
> 本文件 = 注册表落点约定的**技能入口**（`impl_exists` 判据：`.workbuddy/skills/<ID>/SKILL.md`）。
> 全部事实字段派生自注册表条目与执行件 docstring，零编造；方法论叙事见平台技能区（s1-event-analysis / c4-three-q / trade-rules 等描述性技能）。

## 是什么

eod_review.py · E3 盘后复盘报告生产者（任务9#3 · v1.0 · 2026-10-10 建）

## 执行件

- `tools/e2e/eod_review.py`
- 自检：`python tools/e2e/eod_review.py --selftest`

## 产出与消费者

- 产出：判断比分 + 归因 + 教训落点
- 输出落点：data/generated/reports/e3_daily.jsonl

## 知识真源

- （注册表 knowledge_route=null——该能力无结构化知识库；判据在执行件/params）

## 边界（注册表锁死）

- no_decision=True · can_order=False

- S/E 系总规则：只供专业判断素材，不越权决策、不下单（决策 = C4 三问，执行 = C5 硬闸）。

## 触发

- 定时（收盘结算步内）
- 入口：定时：结算步内

## 上游（consumes）

结算结果 / 决策快照
