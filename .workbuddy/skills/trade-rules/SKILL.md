---
name: trade-rules
description: 交易规则速查（买入/仓位/持有/卖出四组 + 价线四件 + 成本）。每条规则的数值都锚定新系统权威源（params.json 键名 / 代码常量），可 grep 验证。触发词：买入、仓位、持有、卖出、止盈、止损、什么时候买、什么时候卖、拿不住、移动止盈、环境分层、仓位上限、止损位、买多少。
trigger: 买入,仓位,持有,卖出,止盈,止损,什么时候买,什么时候卖,拿不住,移动止盈,环境分层,仓位上限,止损位,买多少,单票上限,熔断
version: 3.0.0
contract:
  触发域: 买卖/仓位/持有/止盈止损的**行动参数查询**（"该买多少、止损放哪、什么时候走"）
  权威源: data/params.json（生存类硬闸唯一存放处）· tools/risk/position_gate.py · tools/risk/exit_gate.py · tools/analysis/priceline.py（PL-R1）· AGENT.md §3/§5
  输出边界: 只查行动参数。不产 BUY/WAIT/NO（→ c4-three-q）｜不裁决仓位（→ C5 position_gate）｜不出订单、不执行交易
  时点纪律: 本件数值以 data/params.json 的 version 为准；**数值与本件不一致时以代码/params 为准**，并回来改本件
---

# 交易规则速查（trade-rules）

> **性质**：**纯文档件 · 零代码零依赖**。它不产出任何数据，只是把散在 `params.json` / 三类闸件 / 内核里的**行动参数**收在一页，供人和 LLM 查询。
> **与旧包 `skills/trade-rules/` 的关系**：旧件是 V2.2.0（2026-09-09），含大量旧系统路径与旧系统专属机制。本件**以新系统架构为准重写**——旧件中能在新系统找到机械落点的，改写为键名/常量引用；找不到的，进 §8「未迁移登记」，**不冒充实装**。
> **与 C4 的关系**：`c4-three-q` = **下注前的三问裁决**（判 BUY/WAIT/NO）；本件 = **行动参数速查**（该类动作该配什么数值）。判断在 C4，参数在本件。

## 〇、10 秒速查卡（最常问的 12 条）

| 你问 | 答案 | 权威落点 |
|---|---|---|
| 今天最多几成仓？ | 主升 ≤7成 ｜ 强势 ≤5成 ｜ 震荡 ≤3成 ｜ 退潮 ≤1成 | `params.regime_ladder[*].position_cap` |
| 单票最多多少？ | **总仓 60% 与 权益 39% 取更严** | `single_stock_cap_of_position` / `single_stock_cap_of_equity` |
| 最多持几只？ | 3 只（pending 未成交不算） | `max_holding_count` |
| 回撤多少要减仓？ | 自近期高点回撤 **≥15%** → 总仓压到 **≤5成** | `circuit_breaker_drawdown_l1` / `circuit_breaker_cap_l1` |
| 回撤多少要清仓？ | **≥25%** → 强制空仓 **≥3 个交易日** | `circuit_breaker_drawdown_l2` / `circuit_breaker_flat_days` |
| 单票浮亏多少要复核？ | **≥本金 8%** → 止损复核 + 必给证伪条件 | `circuit_breaker_daily_drawdown` |
| 试仓多少？ | 10% | `trial_position` |
| 最少持有几天？ | 3 个交易日（★豁免逻辑见 §4 H1） | `min_hold_days` |
| 止损放哪？ | 近 10 根真实低点 ×0.98（跌破即失效） | `priceline.STOP_LOOKBACK/STOP_BUF` |
| 最多追高到哪？ | 末根收盘 ×1.01，且不得超次日涨停上沿 | `priceline.ENTRY_BUF` |
| 赔率要求？ | RR ≥ **1.5**，否则**不可交易** | `priceline.MIN_RR` |
| 交易成本？ | 买 0.125% ｜ 卖 0.175% | `cost_buy_rate` / `cost_sell_rate` |

---

## 一、权限边界（先看这个 · 越权结论一律作废）

| 约束 | 内容 | 来源 |
|---|---|---|
| 硬闸唯一存放处 | 熔断 / 单票顶 / 仓位上限**只存** `data/params.json`；AI 无权自行放宽 | AGENT §5 |
| 参数写权 | AI **不得直写** `params.json`，只能提候选进 `calibration_queue` | AGENT §5 |
| 卖出权 | **LLM 无 SELL 权**。卖出只能由**硬风控**或**用户**触发 | AGENT §5 |
| S/E 能力 | 不得输出 BUY/WAIT/NO、不得出仓位、不得执行交易动作 | AGENT §5 |
| C3 边界 | C3 不拥有 `BUY/SELL/仓位/position_gate/最终交易结论` | AGENT §5 |
| 仓位不是批准 | C4 给的是 `position_request`（**请求**）；批准权在 C5（`position_plan ≤ position_request`）+ 用户 | AGENT §4.1 |
| 硬闸独立 | 风控触发时**不需要** Thesis 更新 | AGENT §5 |

**分权一句话**：AI 自主决定「**怎么打**」（策略/方向/RR 要求/仓位请求/周期/节奏）；系统硬闸决定「**最多打多少、最多承受多少风险**」。

---

## 二、B 买入组

### B1 三问准入（答不出不下）
任何下注前必答三问（AGENT §3），**答不出就不下**：

1. **驱动问**：这钱为什么现在来？说不清因果 = **NO**；`driver == 驱动不明` → **机械 NO**（不看其余两问）。**禁把「涨停」当驱动**。
2. **赔率问**：必须带 Target + 证伪价 + 持有窗口。历史样本 **N≥20** 才作参考，**禁编胜率**。
3. **证伪问**：证伪线必须是**机械条件**（价格/事件/规则），**不是心理止损**。

**机械分流**：三问齐过 → BUY（`position_request` 必显式给出）｜证据不足但存在**可验证补全事件** → WAIT（带 `next_trigger`）｜证据不足且无补全事件 → NO（带翻案条件）。
**★ 硬约束**：`NO / WAIT 不得带价位`（带价位就是 BUY 的意图）；价位方向矛盾（`target > entry > stop` 不成立）→ **显式报错，不降级成 WAIT**。
**★ 禁空话**：不许出现「再看看 / 看情况 / 视情况 / 酌情 / 待定」。

- 落点：判据件 `.workbuddy/skills/c4-three-q/SKILL.md`（方法本体）→ `tools/decision/brief.py`（装配+三层校验）→ `tools/fs/three_q.answer`（机械分流）
- BUY 三件齐备（不可豁免）：C3 驱动链成立 + C4 三问全过 + C5 `position_plan > 0`

### B2 只买主线 + 相对低位
板块共振 / 复盘确认主线才买；优先 20 日涨幅小（无套牢盘）。
**★ 未迁移**：旧件的「板块共振 ≥3 只」与「20 日涨幅小」在**新系统无机械阈值参数**——相关能力分散在 C2（`sector_scan_top_n=5`）、S2（`s2_rules.theme.active_zt_min=5`）、PL-R1（`SWING_LOOKBACK`）。**当前是 LLM 判断 + 参数近似，不是等价实现**（见 §8-②）。

### B3 买点等确认 / 不追高
- 不追高开：机械落点 = `priceline` 的 `entry_limit = 末根close × (1+ENTRY_BUF)`，且必须 ≤ 次日涨停上沿（`KR_LIMIT_MAIN=0.10` / 创业板科创板 0.20 / 北交所 0.30）
- 首仓轻仓：`trial_position = 10%`
- **★ 未迁移**：旧件「打板/竞价成交模型（mode=limitup/chase）」在新系统**无对应成交模型件**；新系统侧的等价约束是 AGENT §4「可成交性校验」（一字板未排到**不判成交**）
- **★ 未迁移**：旧件「板块共振时只买龙头，不买当日量比最高的跟风票」在**新系统无机械件**

### B4 数据前置（新系统新增 · 旧件无）
BUY 的价线必须来自 **PL-R1 正式输入**，不得手填：

- 适配器 `tools/decision/pl_input.py`，11 类准入校验：`PL_NOT_FOUND / PL_NOT_TRADEABLE / PL_FIELD_MISMATCH / PL_RULE_MISMATCH / PL_EXPIRED / PL_DIRECTION / PL_RR_LOW / POS_MISSING / POS_RANGE / CODE_NOT_IN_POOL`
- `tools/decision/pred_input.py`，16 类校验，含 `P_CUTOFF_FUTURE`（防未来数据）/ `P_COPY_FROM_REF`（**防复制历史分位充当预测**）/ `P_SLOTS_MISSING`（M1/M2/M3 三槽必须齐）

---

## 三、P 仓位组（**硬闸 · 唯一权威**）

### P1 环境档天花板（四档，取最严的一层）
| 档位 | 判定条件 | 仓位天花板 |
|---|---|---|
| 主升 | 涨停 ≥60 **且** 连板高度 ≥5 **且** 炸板率 ≤20% | **≤7成** |
| 强势 | 涨停 ≥40 且 高度 ≥4 且 炸板率 ≤30% | **≤5成** |
| 震荡 | 涨停 ≥25 且 炸板率 ≤40% | **≤3成** |
| 退潮 | 兜底：涨停 <25 **或** 炸板率 ≥40% | **≤1成** |

- 落点：`params.regime_ladder`（判据件 `tools/analysis/regime.py` → `memory/state/brain_state.md`）
- **禁各自重述口径**：任何地方要引用环境档矩阵，一律引 `regime_ladder`

### P2 单票硬顶（两分母取更严）
| 限额 | 值 | 分母 | 键名 |
|---|---|---|---|
| 单票占总仓 | ≤60% | 总仓金额 | `single_stock_cap_of_position` |
| 单票占权益 | ≤39% | 总权益 | `single_stock_cap_of_equity` |
| 持仓只数 | ≤3 只 | — | `max_holding_count` |

- **两者取更严**；执行器 `tools/risk/position_gate.check`
- 组合暴露唯一真源：`position_gate.portfolio_exposure`（`{code: {executed, pending, exposure}}`）
- 三笔同票**累计**按 `pending + executed` 合并计 exposure（不是逐笔各判 39%）

### P3 账户熔断阀（优先于一切信号 · 回撤期不加仓摊平）
| 层 | 触发 | 动作 |
|---|---|---|
| L1 | 自近期高点回撤 **≥15%** | 总仓等比压缩到 **≤5成**（`cap_l1`） |
| L2 | 回撤 **≥25%** | **强制空仓 ≥3 个交易日**，只观察 |
| 日线 | 单票浮亏 **≥本金 8%** | 止损复核 + **必给证伪条件**（`circuit_breaker_daily_drawdown`） |

- **★ 口径差异**：旧件把「第一档区内新仓破例门」写进规则层（`P0b`：①环境升档 ②外部注资 —— 均须仓位降半 + 次日低开>3%即弃）。**新系统无对应机械参数**，属安全侧**收紧**：熔断期内除 `params` 明文外**无破例通道**（见 §8-③）。

### P4 四层取最严 + fail-safe
`position_plan = min(env 天花板, equity 顶, request, 期权约束)`，且 **plan ≤ request**。
**fail-safe**：缺曲线 / 缺参数 → **直接否决**（不猜测、不降级放行）。

### P5 逆势仓（环境×个股矛盾）
`exit_gate.counter_trend`：环境下跌 **≥1%**（`counter_trend_env_drop`）且个股跌幅 **≤0.5%**（`counter_trend_resist`）→ 判矛盾，看三闸：

| 闸 | 条件 |
|---|---|
| 独强 | 红盘时长 ≥30min **且** 未翻绿 **且** 阶梯上行 **且** 站上均线 |
| 有量 | 量比 ≥1.5 **且** 量比 <4 |
| 有锚 | `anchor_ok` **且** `falsify_ok` |

全过 → `EXCEPTION_ALLOWED`，仓位**降一档**（`counter_trend_position_drop`）；否则 → `ENV_PRIORITY`，**仓位因子 0**。

---

## 四、H 持有组

### H1 最短持有周期
`min_hold_days = 3` 个交易日；到期限评估。
**★ 豁免（旧件 V1.1 证据驱动修订）**：H1 逻辑证伪时**不受限**——能力产物否定方向 / 事件兑现 / 跟风票逻辑弱 → 立即走。
**★ 未迁移警告**：新系统 `min_hold_days` **只是一个参数值**，**没有强制执行的闸**，也没有豁免逻辑的机械实现。旧件的豁免来自 `_FEEDBACK.md` V1 回放（挽回 **+1876 元**）。
→ 现状态：**参数在册、执行靠人/LLM 纪律**（见 §8-④）。

### H2 逻辑开关（灵魂）
每日对持仓问三问：①买入逻辑还在吗 → **逻辑没了=卖，不管价格**；②洗盘还是出货；③环境变了吗（系统性风险 → **降仓保护，不精清**）。
- 机械出口：`exit_gate.check` 的 **THESIS 分支**（LLM 判 `thesis_invalidated` → **闸定动作**）
- **★ 边界**：LLM 只能提交 `llm_payloads` 白名单字段（`LLM_PAYLOAD_KEYS`），越权字段 → `PayloadRejected` **整体拒收**，绝不忽略字段后继续执行

### H3 逆势仓证伪（**双条件同现才走**）
`exit_gate.counter_trend_exit`：①红转绿 **且** 破关键位；②承接萎缩 **或** 大额流出。
**仅单条件 → HOLD**。**★ 无 REDUCE**（禁机械减半容差，洗盘震仓不由该闸执行）。

### H4 强主线龙头首次回调 ≠ 反转
缩量破 5 日线 + 板块还强 = 洗盘，**减半不精清**。
- **★ 未迁移**：新系统**无机械件**判定「缩量/板块还强/洗盘 vs 出货」——`exit_gate` 只有 HARD/THESIS/EVENT/NONE 四类，不含此类判定。属人/LLM 纪律（见 §8-⑤）。

---

## 五、S 卖出组

### S1 四分类退出（机械 · 判定顺序即优先级）
`exit_gate.check` 逐票按序短路，命中即 `continue`：

| 序 | 类型 | 触发 | 动作 |
|---|---|---|---|
| ① | HARD | `last ≤ stop_line`（证伪线） | SELL 全部 |
| ② | HARD | 熔断 L2 激活（回撤 ≥25%） | SELL 全部 |
| ③ | HARD | 单票 > 权益顶 39%（**先于 L1**：个股问题逐票压帽） | REDUCE 到 39% |
| ④ | HARD | 熔断 L1 激活且总仓 > 5成 | REDUCE，等比压缩到总仓帽 |
| ⑤ | THESIS | LLM 判 `thesis_invalidated` | SELL 全部 |
| ⑥ | EVENT | 事件兑现（`events` 命中） | SELL 全部 |
| ⑦ | NONE | 以上皆否 | HOLD |

- **★ 方向性差异**：`drawdown=None`（无法判熔断）时熔断子分支**跳过**——退出方向**宁松勿误杀**，HOLD 兜底。这与 `position_gate` 的 fail-safe（缺数据即否决）**方向相反，是有意设计**。

### S2 纠错回补闸（卖出后想买回 · 新系统新增）
`position_gate.correction_gate`：
- `driver_now == sell.driver` → `CORRECTION` 模式：①冷却期 `correction_cooldown_days=3` 内已纠错过 → **DENY**；②现价 > 卖价 ×(1+`correction_price_up=5%`) → **DENY(CHASE)**；③三条件缺一 → **DENY**；全过 → **仓位减半**（`correction_position_half=0.5`）
- `driver_now != sell.driver` → `NEW_DRIVER`，全量放行，**重新过完整三问**（归 C4）

### S3 ★ 分档止盈 / 移动止盈 —— **未迁移**
旧件 `S1 分档止盈`：+8% 兑现 1/3 → +15% 再兑 1/3 → 余 1/3 跑主升；`S2 吊灯止损`：浮盈<8% 回撤5%走 / 8~15% 回撤8%走 / >15% 回撤10%走。
**新系统现状（已核实）**：`params.json` **零**止盈类参数；`exit_gate.py` **无** TAKE_PROFIT / 移动止盈分支。
→ 属**用户手动纪律**，不是系统机械规则。**不得描述为"系统会分档止盈"**（见 §8-⑥）。

### S4 ★ 换仓禁令 —— **未迁移**
旧件 `S0`：**禁止仅因「当日不涨/涨幅落后」而卖出换仓**；卖出理由不得出现 `weak_today / not_up` 作为独立理由（旧件靠 `DECISION_LOG` 校验）。
**新系统现状**：`exit_gate` 无此判定；`decision_ledger.jsonl` 无该理由字段的校验器。
→ 属**纪律条款**，无机械闸（见 §8-⑥）。

---

## 六、价线四件（PL-R1 · `tools/analysis/priceline.py`）

| 件 | 公式 | 常量 |
|---|---|---|
| **stop_line** 证伪价 | `min(low of 最近 STOP_LOOKBACK 根) × (1 − STOP_BUF)` | `STOP_LOOKBACK=10` · `STOP_BUF=0.02` |
| **entry_limit** 入场上沿 | `close(最后一根) × (1 + ENTRY_BUF)`，且 ≤ 次日涨停上沿 | `ENTRY_BUF=0.01` |
| **target** 目标价 | 仅当近 `SWING_LOOKBACK` 根存在**真实前高 > entry** 才给；否则 **null**（不给"无阻力"豁免） | `SWING_LOOKBACK=120` |
| **window** 持有窗口 | `ceil(TARGET_WINDOW_BASE / ATR%ₙ)`，夹在 `[WINDOW_MIN, WINDOW_MAX]` | `TARGET_WINDOW_BASE=0.05` · `3~10` |

**RR** = `(target − entry) / (entry − stop)`，**RR < MIN_RR(1.5) → 不可交易**。

**护栏（任一命中 → 不可交易，不降级）**：`NO_KLINES`（历史 <`LOOKBACK=60` 根）｜`FUTURE_LEAK`（数据晚于 as_of）｜`NO_TARGET_STRUCTURE`｜`ENTRY_ABOVE_LIMIT`｜`STOP_GAP_ABNORMAL`（止损距 entry 下浮 >`STOP_GAP_MAX=25%`=崩盘失真）｜`RR_TOO_LOW`｜数据过期 >`MAX_STALE_DAYS=7` 自然日。

---

## 七、成本（结算正式扣费口径）

| 项 | 值 | 键名 |
|---|---|---|
| 买入费率 | 0.125% | `cost_buy_rate = 0.00125` |
| 卖出费率 | 0.175% | `cost_sell_rate = 0.00175` |

- 口径统一入口：`verify_cost_model = PARAM_RATES:cost_buy_rate+cost_sell_rate`
- **禁口径漂移**：任何收益率/胜率结论必须声明是否已扣费

---

## 八、口径差异与未迁移登记（**读本节前不要引用旧件**）

| # | 项 | 旧件 | 新系统现状 | 处置 |
|---|---|---|---|---|
| ① | **证据门槛** | 旧 `_FEEDBACK` V1 用 **n=16** 支撑结论 | AGENT §3 明写 **N≥20 才作参考**；`pred_ref.N_MIN_EFFECTIVE=30`；`expect_ref` N<30 拒用 | ★ 旧证据按新口径**未达参考门槛**，只能当"线索"不能当"结论" |
| ② | **主线/低位机械阈值** | `板块共振≥3只` / `20日涨幅小` | 无等价参数（近似件：`sector_scan_top_n=5` / `s2_rules.theme.active_zt_min=5`） | 近似，**非等价** |
| ③ | **熔断破例门** | 旧 `P0b`（环境升档 / 外部注资 → 额度重算） | 无机械参数 | 新系统**更严**：熔断期内无破例通道 |
| ④ | **最短持仓执行** | `S3` + V1.1 豁免条款 | `min_hold_days=3` 仅在册，**无强制闸、无豁免逻辑** | 参数在册 / 执行靠纪律 |
| ⑤ | **洗盘 vs 出货** | `H2` / 盘感卡④「放量下跌+破线+板块退潮三证」 | 无机械件 | 未迁移 |
| ⑥ | **分档止盈 / 吊灯止损 / 换仓禁令** | `S1` / `S2` / `S0` | `params` 零止盈键；`exit_gate` 无对应分支 | 未迁移 · **用户手动纪律** |
| ⑦ | **接力环境七证据打分** | `B1`（昨涨今溢/晋级率/…→ 强中弱禁止） | 无机械件（近似件：C1 `regime_ladder` 四档 + S3 `s3_rules.temp/stage`） | 近似，**非等价** |
| ⑧ | **旧系统路径引用** | `memory/research/...` / `skills/backtest/...` / `AGENT §3`（旧内核） | 新系统同名路径**不存在** | 本件已改写为**旧包绝对路径**（只读）或新系统对应件 |
| ⑨ | **决策质量分级 A-D** | 过程分 A/B/C/D（C 类=坏过程侥幸赚钱，最危险） | 无机械件；C6 `cases/verify/calibrate` 不判过程分 | 未迁移 · 方法论保留 |
| ⑩ | **概率使用铁律** | 只允许条件概率 `P(结果|目标票完整画像)`；禁大池边际统计否决单票 | 与 C4 `basis` 引用可追溯机制**一致**（引用不可追溯=判断作废） | ✅ 已在新系统落地为机制 |

---

## 九、历史证据（**静态 · 时点冻结 · 只读引用**）

> 来源为**旧包**，**只读**引用，**不在新系统复制数据**（防第二真源）。引用时必须带 as_of。

| 证据 | 时点 | 来源（旧包绝对路径） | 要点 |
|---|---|---|---|
| V1 首轮 16 笔核对 | 2026-09-06 | `~/炒股智能体_旧包_20261008\skills\backtest\docs\BT-20260905-222919\TRADE_LEDGER.csv` | H1 错判 4 笔**全卖飞**；S3 违反 6 笔；卖出完全正确 5 笔 |
| V1→V1.1 S3 豁免修订 | 2026-09-06 | 同批回放 | 旧规则 −452 元 → 强制持有 3 日 **+1424 元**（差 **+1876 元**）；反例 4 笔全是逻辑已证伪票 |
| V1b B1 接力核对 | 2026-09-06 | `...\skills\backtest\docs\BT-20260906-015751\TRADE_LEDGER.csv` | **B1 反例 #1** 时代出版 `-397.12`（已独立复核，与台账逐字一致）；根因=「板块内唯一连板 ≠ 龙头」 |
| B1 拟议修订（**未生效**） | 2026-09-06 | 同上 | 接力候选须满足①板块内 5 日最强 或②20 日涨幅最高 或③封板质量最好 至少一项，否则降级"补涨"不接力 |
| S2 待验证点（**登记不改参**） | 2026-09-06 | 同上 | 百花从 +57.9% 峰值回吐 26pp 才出场 → 接力票 10% 吊灯阈值偏宽 |
| A3 环境档×选票策略总图 | — | `...\memory\research\A_市场环境\A3_环境档×选票策略_总图.md` | 旧系统六档→矩阵四档桥接件；**新系统无对应文件** |

**★ 引用纪律**：上表任何数字**不得**被表述为「新系统已验证」——新系统的验证链是 C6（`cases/verify/calibrate`），与旧包回测是两条独立证据线。

---

## 十、数值自校验（一条命令）

本件所有数值都可 grep 验证；发现不一致**以代码/params 为准**并回来改本件：

```bash
python -c "import json,io;d=json.load(io.open('data/params.json',encoding='utf-8-sig'));print({k:(v['value'] if isinstance(v,dict) and 'value' in v else 'COMPOSITE') for k,v in d['params'].items()})"
grep -n "MIN_RR\|STOP_LOOKBACK\|STOP_BUF\|ENTRY_BUF\|SWING_LOOKBACK\|STOP_GAP_MAX\|MAX_STALE_DAYS\|LOOKBACK " tools/analysis/priceline.py | head -20
grep -n "HARD\|THESIS\|EVENT\|NONE" tools/risk/exit_gate.py | head -20
python -c "import json,io;d=json.load(io.open('data/params.json',encoding='utf-8-sig'));print(d['version'], d['updated_at'])"
```

**核验基线**：`params.json version = 1.0.8` · `updated_at = 2026-10-12`（本件成文时）。

---

## 十一、反哺（改规则只有一条路）

- 每轮新案例 → 核对规则命中 → 统计命中率/反例 → 记入 `_FEEDBACK.md`
- **触发修订**：某规则连续 **`rule_decay_refute_streak = 2`** 次反例 → 出**修订建议**（含证据）→ **人审** → 更新规则层（记版本）
- **禁手动改规则绕过证据层**（用户红线）
- **AI 零 `params.json` 直写**：只能写候选进 `calibration_queue`；**唯一出口** = 用户批准后 `params.promote()`
