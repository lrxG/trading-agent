# 手动投放模板 · 使用说明 v1.0（2026-10-09）

> 配套契约：`docs/RAW-INGEST-CONTRACT.md` v1.0 · 执行件 `tools/fs/ingest.py`
> 本目录 = 直接可填的投放模板（表头与 `snapshot.CONTRACT` 逐列一致，**禁改列名/列序**）。
> 验收背书：`tools/e2e/raw_to_settle.py` E2E-c 20/20 —— 按本模板填数 → `stage_core` →
> `promote_snapshot` → loader → C3/C4/C5 → 结算 → Case 全链已沙盒贯通。

## 0. 三类数据界限（不得混称）

| 类别 | meta.source 填法 | 说明 |
|---|---|---|
| 合成/测试 | `manual:test` | 只进沙盒，禁投生产 data/raw |
| 人工整理的历史真实数据 | `manual:<来源说明>` | 本模板的正常用途 |
| 实时采集 | `<采集器>:<接口名>` | 待数据源拍板后另批接入 |

## 1. 投放流程（四步）

```python
# 在项目根目录执行；<date>=交易日 YYYY-MM-DD
import sys; sys.path.insert(0, "tools/fs")
import ingest as ING

# ① 整包投放（rows 用下面各文件模板填充的行）
ING.stage_core("<date>", {
    "daily_quote": [ {...} ], "limit_up": [ {...} ], "dragon_tiger": [ {...} ],
    "news": [ {...} ], "env_metrics": {...},
}, source="manual:<来源说明>", data_upto="<date>")

# ② 核心 5 键齐 → 正式化为快照（loader/C1/C2/C3 的消费入口）
ING.promote_snapshot("<date>")

# ③ 诊断（可选）：缺口/错位盘点
ING.diagnose_staging("<date>")
```

自动把关：未来数据拒（FUTURE_DATA）· 重复投放拒（STAGED_EXISTS）· 无来源拒
（SOURCE_REQUIRED）· 坏行默认全拒（ROW_ERRORS，`drop_bad=True` 显式过滤）·
补数用 `replace=True`（留痕）· 缺键日永不误正式化（CORE_INCOMPLETE）。

## 2. daily_quote.csv（日线行情 · 核心键）

| 列 | 必填 | 格式/单位 | 缺失处理 | 校验规则 |
|---|---|---|---|---|
| code | 是 | 6 位数字，可带 sh/sz/bj 前缀（如 `sh600000`、`000001`） | 缺→整行拒 | 正则 `^(sh\|sz\|bj)?\d{6}$` |
| name | 是 | 文本 | 缺→整行拒 | 非空 |
| open/high/low/close | 是 | 元（不复权口径须全程一致并记录在投放备注） | 缺→整行拒 | 数值；high≥max(open,close)≥min(open,close)≥low 建议自检 |
| pct | 是 | **百分数**（涨跌幅 %，如 3.18 表示 +3.18%） | 缺→整行拒 | 数值 |
| vol | 是 | 手（与数据源口径一致；手动填恒用同一口径） | 缺→整行拒 | 数值 |
| amount | 是 | 元 | 缺→整行拒 | 数值 |

示例行：`sh600000,模板票,10.0,10.5,9.9,10.4,3.18,100,127000000.0`

## 3. limit_up.csv（涨停池 · 核心键）

| 列 | 必填 | 格式/单位 | 缺失处理 | 校验规则 |
|---|---|---|---|---|
| code/name | 是 | 同上 | 缺→整行拒 | 同上 |
| boards | 是 | 连板数（整数，首板=1） | 缺→整行拒 | 正整数 |
| first_seal_time | 是 | `HH:MM` 或 `HH:MM:SS`（首次封板时刻） | 缺→整行拒 | 时间格式 |
| seal_amount | 是 | 元（封单金额） | 缺→整行拒 | 数值 |
| reason | 否 | 涨停原因/题材文本 | 缺→留空 | 文本（**弱代理，不得拼产业因果链**） |

当日确实无涨停 → 投空列表 `[]`（合法状态），不要伪造行。

## 4. dragon_tiger.csv（龙虎榜 · 核心键）

| 列 | 必填 | 格式/单位 | 缺失处理 | 校验规则 |
|---|---|---|---|---|
| code/name | 是 | 同上 | 缺→整行拒 | 同上 |
| seat | 是 | 席位名称文本 | 缺→整行拒 | 非空 |
| buy/sell/net | 是 | 元；net=buy−sell（自洽） | 缺→整行拒 | 数值 |

无上榜 → 投空列表 `[]`。

## 5. news.jsonl（公告/新闻 · 核心键，每行一个 JSON）

| 字段 | 必填 | 格式 | 缺失处理 | 校验规则 |
|---|---|---|---|---|
| news_id | 是 | **稳定 ID**（公告用交易所/巨潮公告号；新闻用来源原生 ID；无原生 ID 时用 `来源缩写-YYYYMMDD-序号`，重投须同 ID） | 缺→整行拒 | 全局唯一 |
| ts | 是 | `YYYY-MM-DD HH:MM:SS`（发布时刻；日期必须=投放日） | 缺→整行拒 | 非当日 → C3 证据拒收 |
| source | 是 | 来源名（如 `巨潮资讯`/`交易所公告`） | 缺→整行拒 | 非空 |
| title/body | 是 | 文本 | 缺→整行拒 | 非空 |
| related_codes | 是 | code 列表（无关联票投 `[]`） | 缺→整行拒 | 元素过 code 正则 |

公告/事件类额外要求（用户裁决 2026-10-09）：**保留来源、原始分类、发布时间、
采集时间、稳定 ID**——原始分类（如东财 226 类官方分类名）填进 `title` 前缀或
`body` 首行均可（S1 事件流的 `cols` 字段消费它），不得只填人工改写后的归类。

示例行：
```json
{"news_id": "ANN-2026-1008-001", "ts": "2026-10-08 08:45:00", "source": "巨潮资讯",
 "title": "回购预案（原始分类：回购预案）", "body": "...", "related_codes": ["sh600000"]}
```

## 6. env_metrics.json（环境指标 · 核心键，单对象）

| 字段 | 必填 | 格式/单位 | 缺失处理 |
|---|---|---|---|
| limit_up_count | 是 | 涨停家数（整数） | 缺→整包拒 |
| max_board | 是 | 最高连板（整数） | 缺→整包拒 |
| turnover | 是 | 两市成交额（元） | 缺→整包拒 |
| up_down_ratio | 是 | 涨跌家数比（数值，如 2.8） | 缺→整包拒 |
| fail_rate | 是 | 炸板率（百分数，如 24.0） | 缺→整包拒 |
| index_ma20 | 是 | 上证指数 MA20（点位）——C 类驱动/C5 档位消费 | 缺→整包拒 |

示例：
```json
{"limit_up_count": 58, "max_board": 5, "turnover": 142000000000.0,
 "up_down_ratio": 2.8, "fail_rate": 24.0, "index_ma20": 3412.6}
```

## 7. 时间口径总则

- 目录日/投放日 = **交易日**；`data_upto` = 数据实际截止交易日；
- 盘后投当日收盘：`data_upto = 投放日`；盘前投昨日数据：`data_upto = 昨日`（合法，DATA_LAG 警告留痕）；
- **未来数据一律拒收**（`data_upto > 投放日` → FUTURE_DATA）；
- 停市日不投放（目录按交易日建，无数据日 loader 如实 UNAVAILABLE）。

## 8. 已知边界（如实声明）

- 复权口径不校验（建议前复权全程一致；混用会污染 pnl 与 S1 效应对照，由投放者负责）；
- 扩展类型（sector/capital_flow 等）走 `ING.stage_ext`，本模板暂不覆盖；
- 本模板不接任何付费服务、不提交凭据；采集器自动化待数据源正式拍板。
