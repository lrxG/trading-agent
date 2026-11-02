# data/raw 投放契约（供应商无关）· v1.0

> 2026-10-09 · 数据链施工批 · 执行件 `tools/fs/ingest.py` · 契约真源 =
> `snapshot.CONTRACT`（核心 5 键列规格）+ `loader.py`（类型规则/读取语义）。
> 本文档 = 采集器实施对照单；**任何采集器（外部数据源/手动投放）只允许
> 经 ingest 落盘，禁绕过校验直写**。

## 1. 布局

```
data/raw/<YYYY-MM-DD>/              投放日（交易日口径）目录
  daily_quote.csv                   核心键（fmt/列规格 = snapshot.CONTRACT）
  limit_up.csv
  dragon_tiger.csv
  news.jsonl
  env_metrics.json
  _core.meta.json                   核心包源信息 sidecar
  <type>.jsonl                      扩展类型（sector/capital_flow/.../policy 等 10 具名 + 新类型）
  <type>.meta.json                  扩展类型源信息 sidecar（loader 忽略，stem 带 .meta）
```

## 2. 源信息 meta（必填，禁无源投放）

| 字段 | 含义 |
|---|---|
| `source` | 来源标记（如 `westock:kline` / `manual`） |
| `fetched_at` | 抓取时刻 ISO |
| `data_upto` | **数据截止时间**（数据实际覆盖到的交易日） |
| `replace` | 是否补数覆盖 |
| `keys`/`row_count`/`dropped` | 投放内容盘点 |

## 3. 时间纪律（交易日错位防护）

- `data_upto > 目录日` → **拒绝**（FUTURE_DATA，未来数据禁投放）；
- `data_upto < 目录日` → 警告不阻断（合法：盘前投昨日收盘）；loader 消费时
  `is_exact` 按目录日如实标注，缺口由 UNAVAILABLE 机制呈现。

## 4. 重复 / 补数 / 部分成功 / 缺失

| 场景 | 机制 |
|---|---|
| 重复采集 | 同日同类型重投默认拒绝（STAGED_EXISTS） |
| 历史补数/修正 | `replace=True` 覆盖 + meta 留痕（投放区非账本，不堆历史版本） |
| 部分采集成功 | 核心键可只投部分；**快照正式化要求 5 键齐**（CORE_INCOMPLETE） |
| 坏行 | 默认全拒不投放（ROW_ERRORS 明细）；`drop_bad=True` 显式过滤并计数 |
| 数据缺失 | loader 侧 UNAVAILABLE（缺口）/ None（该票当日无）/ 空 Series（真实无）三态，禁编造 |

## 5. 采集 → 消费全链（接口清单）

```
采集器(外部) ──► ingest.stage_core / stage_ext     （投放前把关 + meta）
                    │
                    ├─► loader.load_ext / load_premarket   （扩展类型/盘前：直读投放区）
                    │
                    └─► ingest.promote_snapshot            （核心 5 键齐 → 转发
                            │                                snapshot.write_snapshot 严格校验）
                            └─► loader.load_quote / load_news / load_env_metrics /
                                load_snapshot / load_premarket   （核心接口消费 = C1/C2/C3 入口）
```

- 正式契约严格校验单一真源 = `snapshot.write_snapshot`（ingest 快筛只拦明显坏行，
  不重复实现列类型转换）；
- 扩展类型新增 = 采集器放文件 + `load_ext("<type>", as_of)`，**loader 零改动**；
- 扩展类型名禁与核心 5 键撞名（loader `_check_type` 拒收）。

## 6. 环境与依赖

- 现有依赖：纯标准库（csv/json/re/tempfile）——ingest 与 loader 均零第三方依赖；
- 采集器依赖（第三方SDK/pandas）**只允许存在于外部采集脚本**，禁进入
  `tools/` 生产代码区（纪律判据 D1 机械检查）；
- Python 3.13.12（managed）可运行全部现有链路。
