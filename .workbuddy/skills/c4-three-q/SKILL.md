---
name: c4-three-q
description: C4 三问决策方法层（驱动/赔率/证伪 → BUY/WAIT/NO）。LLM 判断本体与机械侧的接缝契约：输入 brief、输出 proposal、失败安全。触发词：三问、决策、买不买、BUY、WAIT、NO、赔率、证伪、下注、仓位请求。
trigger: 三问,决策,买不买,该不该买,赔率,证伪,BUY,WAIT,NO,下注,仓位请求
version: 1.0.0
contract:
  触发域: 下注前的三问裁决（"这票现在下不下注、下多少"）
  判据件: tools/fs/three_q.py（机械裁决）· tools/decision/orchestrate.py（幂等落账）· tools/decision/brief.py（装配+校验）
  输出边界: 只产「判断素材 + 动作请求」，不产仓位裁决（→C5）、不结账（→C6）、不执行交易
---

# C4 三问决策 · 方法层（SKILL）

> 版本 1.0 · 2026-10-12 · 对应施工方案 1.4（已批准）· 接缝件 `tools/decision/brief.py` ·
> 机械裁决 `tools/fs/three_q.answer` · 落账 `tools/decision/orchestrate.decide`

## Design Boundary（防退化成 prompt 解析器）

C4 是**唯一有权做决策**的环节（`can_order=false`，但 `no_decision=false`）。三件不可越：

- **动作不由 LLM 定**：LLM 只填「事实与数值」（driver / 价线三件 / window /
  position_request / 两个事件），**BUY / WAIT / NO 由 `three_q.answer` 机械分流**。
  机械规则：驱动不明 → NO；证据不足有补全事件 → WAIT，无 → NO；三问齐过 → BUY。
  LLM 输出里的 `action_proposed` **仅供参考对照**（判断错 / C5 降仓 的归因用），
  **不参与裁决**——写进去也不改变结果。
- **仓位不是批准**：`position_request` 是 LLM 的**请求仓**，批准权在 C5 硬闸
  （`position_plan ≤ position_request`）与用户。LLM 无权让一笔单必然成交。
- **零 SELL 权**：本层不得输出卖出、平仓、清仓语义——卖出只能由硬风控或用户触发。
- **零执行**：不得输出下单、价格指令、券商接口参数。`orders_created` 恒 0。
- **零模糊输出**（硬验收）：不许「再看看 / 看情况 / 酌情 / 大概率」。空话会被
  `brief.review_proposal` 直接拒绝，不是提醒。

判断本体在 LLM（AGENT.md 内核 4.1）——`brief.py` 只做装配与校验，不产判断。
**本 skill 的判据受 AGENT.md §3「三问准入」约束**；冲突时以内核为准。

## LLM 调用边界（代码 / 运行环境的职责分界）

本项目代码**不含 LLM 客户端**——这是设计，不是缺失。

```
内核（运行环境）          代码（本项目）
──────────────          ──────────────
读 brief ←────────────  brief.assemble()       ← 装配（本件）
调用 LLM（端点/模型/凭据/重试）
产出 proposal ────────→  brief.review_proposal() ← 校验（本件）
                         three_q.answer()        ← 机械裁决
                         orchestrate.decide()    ← 幂等落账
```

- 代码**不知道也不关心**端点地址、模型名、凭据、超时与重试——那些属运行环境配置。
- 这样切的理由：①判断本体在 LLM，代码复制一份判断 = 造第二真源；
  ②端点/凭据未授权时写客户端 = **空壳**，空壳冒充完成比不写更坏。
- **真实接入所需三件外部条件**（缺一即跑不出真实决策，均**不由本批伪造**）：
  ①LLM 端点 + 凭据（需用户授权）；②`data/snapshot` 行情数据源（**冻结中**）；
  ③C3 驱动链的真实生产者（依赖 ②）。
- 失败边界：内核调用失败或超时 → `proposal=None` → 本件落 `LLM_UNAVAILABLE` **不落账**
  （见下「失败安全」），内核无需为此编造替代输出。

## 输入契约（brief · 由 `brief.assemble()` 产出）

调用：`brief.assemble(as_of, code, *, thesis_id, chain, ...)` → 一个确定性 JSON。
**缺就缺，如实标 `missing`；不许脑补**。

| 字段 | 来源 | 缺时语义 |
|---|---|---|
| `env` | C1 `brain_state.md` 环境档段（含 `position_cap` 天花板） | `source="UNAVAILABLE"`（禁编档） |
| `env_ledger` | 上者投影到 0.3 账本契约（regime/signals/leg/source） | 同左 |
| `candidate` | C2 候选池（id/state/created_at/expiry） | `null` |
| `chain` | C3 驱动链（evidence/inference/prediction/five） | `null` |
| `evidence_room.s1_effects` | S1 效应库（32 类实证，带 kb_version） | `null` |
| `evidence_room.s3_emotion` | S3 情绪结构（当日快照 + 历史库） | `null` |
| `evidence_room.s2_theme` | S2 本票所属题材阶段（stage/DayN/族；经旧包成分股映射落到个股，最多 8 个） | `null` |
| `evidence_room.s4_crowding` | S4 本票拥挤度（crowding + evidence + reasons；`froth_stock` 恒 UNAVAILABLE） | `null` |
| `evidence_room.s5_capital` | S5 本票资金行为（accumulation/relay/distribution/…+ 命中闸） | `null` |
| `evidence_room.s8_policy` | S8 本票相关公告/政策事件（最多 10 条，超量报 n_truncated） | `null` |
| `params_version` | `data/params.json` 版本 | `PARAMS_VERSION_ABSENT` |
| `info_set_hash` | 输入指纹（与落账同一算法） | `INFO_SET_UNRESOLVED` |

**证据间（evidence_room）是 LLM 唯一可引用的宇宙**。引用命名空间：

```
C1:ENV         环境档（regime / 六信号）
C2:CANDIDATE   候选池条目
C3:CHAIN       驱动链
S1:EFFECT      效应库某类别（如 S1:EFFECT:业绩预告）
S2:THEME       本票题材阶段（发酵/扩散/加速/分歧/尾声 + DayN）
S3:EMOTION     S3 情绪阶段 / 龙头中军补涨
S4:CROWDING    本票拥挤度（high/medium/low + 命中证据）
S5:CAPITAL     本票资金行为（accumulation/relay/distribution/…）
S8:POLICY      本票相关公告政策事件
```

> S2/S4/S5/S8 是 **2026-10-23 新增**的增强证据：它们**缺席不进 `missing`**
> （不像 S1/S3 那样是决策前提），所以引用前先判空 —— 引用一个 `null` 来源
> 会被 `review_proposal` 的证据可追溯闸拒掉。
> 这四项一律**只带本票**（不夹带别票），防串号。

## 判断流程（LLM 部分）

**① 驱动问**：这钱为什么现在来？读 `chain.five`（五件套：驱动存在/当前证据/延续机制/
失效条件/剩余预期）+ `chain.driver_effective`。说不清 → `driver="驱动不明"`
（此时机械直判 NO，不看其余两问）。**禁把「涨停」当驱动**。

**② 赔率问**：填 `entry_limit / stop_line / target / window` 四件。
方向必须 `target > entry_limit > stop_line`（矛盾会显式抛错，**不降级成 WAIT**）。
赔率 = `(target−entry)/(entry−stop)`，由机械算出，LLM 不填。
`stop_line` 必须是**机械条件**（价格/事件/规则），不是心理止损。

**③ 证伪问**：买入逻辑什么条件下算被推翻？→ 写进 `chain.five.失效条件` 与
`overturn_event`（翻案条件）。

**④ 仓位请求**：`position_request ∈ [0,1]` —— 这是**请求**，受 `env.position_cap`
天花板约束，最终由 C5 裁决。**禁写成 `position_cap × 试仓` 这类公式**。

每条判断必须挂 `basis` 引用（见下），**引用不可追溯 = 判断作废**。

## 输出 schema（proposal）

```json
{
  "schema_version": 1,
  "as_of": "2026-09-29", "code": "sh600000", "thesis_id": "TH-20260929-001",
  "driver": "A|B|C|复合|驱动不明",
  "entry_limit": 10.2, "stop_line": 9.8, "target": 11.5, "window": 5,
  "position_request": 0.2,
  "completion_event": {"text": "...", "rule": {"field":"...","op":">","value":0}},
  "overturn_event": null,
  "action_proposed": "BUY",
  "basis": [{"ref": "S3:EMOTION", "claim": "情绪=发酵、领先者放量晋级"}],
  "missing": [], "confidence": 0.6
}
```

- `completion_event`（补全事件）：证据不足但**存在可验证补全路径**时填 → 机械给 WAIT。
- `overturn_event`（翻案条件）：给不出补全事件时填 → 机械给 NO（**NO 也产信息**：
  它自动成为观察锚进候选池）。
- 两者都是 `{text, rule}`；`rule` 可为 null（人话条件），非 null 时必须是
  `{field, op, value}` 且 `op ∈` 候选池算子集。**空话文本会被拒**。
- `basis` 至少 1 条，`ref` 必须落在上文引用命名空间内，且该来源在 brief 中
  **非 null**（引用了缺席来源 → `EVIDENCE_UNRESOLVED` 拒收）。

## 失败安全（5 类，机械可判）

| 情形 | 状态 | 机械行为 |
|---|---|---|
| brief 缺决策前提（无 `thesis_id` / 无 `chain`） | `BRIEF_INCOMPLETE` | **不调用 LLM、不落账**（无决策主体，谈不上判断） |
| LLM 未产出（端点不可用/超时/未返回） | `LLM_UNAVAILABLE` | **不落账**，当该 code 无决策 |
| 结构不合规（缺字段/类型错/串号/空话） | `PROPOSAL_MALFORMED` | **拒收**，不落账 |
| 引用不可追溯（编造证据/引缺席来源） | `EVIDENCE_UNRESOLVED` | **拒收**，不落账 |
| 合规但证据不足 | → 机械分流 | **正常落账** WAIT / NO |

**前 4 类坚决不落账**：记一个 LLM 没真正给出的决策 = 污染账本（C6 学习闭环读它），
比不决策更坏。**「答不出就不下」在这里的落点是「不落账」，不是「落一个 NO 凑数」。**
前两类是**数据/接口层阻塞**（要补链、补端点），后两类才是**输出质量问题**——归因不同，
处置也不同：前者重试无用，后者可让 LLM 重答一次。

## 接缝与消费

```
brief.assemble(as_of, code)            → brief          （装配，零写点）
        ↓  LLM 判断（本 skill）
proposal                                → brief.review_proposal(proposal, brief)
        ↓  结构 + 恒等 + 证据引用三层校验
answer_kwargs → brief.decide_from_proposal(..., sim=…)
        ↓  three_q.answer 机械分流 → orchestrate.decide 幂等落账
data/generated/decision_ledger.jsonl   （DECISION 行 + 候选锚）
```

- 校验三层：①结构（字段/类型/值域）②**恒等**（`as_of`/`code`/`thesis_id` 必须与
  brief 逐字相同——防串号，把 A 票的判断记到 B 票头上）③证据引用可追溯。
- `SIM` / `LIVE` 物理分轴，`sim` **必须显式传**（不表态即报错）。
- 幂等：同 `info_set_hash` 已有正式决策 → `REUSED` 复用原 id，**绝不二次落账**。

## A 路日常运行（2026-10-20 接通 · 用户令「继续落地」）

**「外部 LLM」= 运行环境内的模型（wb）**，不是第三方端点——代码不含 LLM 客户端是设计（判断本体在 LLM，代码复制判断 = 第二真源）。A 路 = LLM 判断通路，与 B 路（mech_driver 机械基准）互斥二选一。

### 两段式（人机环）

```
第一段（自动）  wakeup / run_chain（不带 --proposals）
                → C1 环境档 → C2 候选池 → C3 驱动链落盘
                → C4 无判断输入 ⇒ 如实 BLOCKED/EMPTY（不冒充决策）
第二段（LLM）   wb 读链材料（evidence / five / 环境档 / 候选前史）
                → 产 judgments 文件（落 `data/input/judgments_<as_of>.json`——
                  X6 外部判断输入豁免区，与 proposals 同家、留痕可审计；
                  临时试验可放 /tmp 等非受护区）
                → llm_emit.py 装配 → data/input/proposals_<as_of>.json（producer=LLM_WB）
第三段（自动）  run_chain --proposals <该文件> → three_q 分流 → C4 落账 → C5 风控 → C6
```

### judgments 格式与三种判断的诚实映射

| LLM 判断 | 映射方式 | 机械结果 |
|---|---|---|
| **BUY** | 三问全齐 + `position_request` + `overturn_event` | BUY（C5 再裁决仓位） |
| **WAIT**（证据待补） | 某问**诚实答不出** = 显式置 `null`（如 `entry_limit`/`target`）+ `completion_event` | WAIT（带 next_trigger 观察锚） |
| **否决**（位置过高/流动性陷阱/集中度） | 同上：给不出诚实验价位 ⇒ 置 `null`（不伪造价位凑 BUY） | WAIT |

★ 纪律：禁伪造价位凑 BUY；禁自造票（只对 mech_driver.build 选出的票做判断，
  LLM 提池外票 llm_emit 如实丢弃）；禁引缺席证据（basis ref 必须在命名空间且在场）；
  价位四件未提供的键沿用机械基准（保证与 B 路可比）。

### 首跑案例（2026-09-30 · 5 票 · 已验）

昭衍新药 **BUY 0.1**（首板低位 + 26.5 亿巨量 + C2 候选前史 + 医药板块扩散日）｜
康希诺 **BUY 0.05**（20cm 龙头半仓试）｜新华传媒 **WAIT**（7 板一字位置过高：`entry_limit=null`）｜
美诺华 **WAIT**（医药同板块第三票集中度控制：`target=null`）｜
园林股份 **WAIT**（494 万成交流动性陷阱：`entry_limit=null`）。
端到端 7 OK/0 FAIL，账本 5 行全部按判断分流（2 BUY / 3 WAIT）。

### LIVE 边界

`producer=LLM_WB` 的 proposals 可跑 LIVE（`run_chain --live`）；
`producer=MECHANICAL_BASELINE` 禁 LIVE（`MECHANICAL_BASELINE_LIVE_FORBIDDEN` 硬锁）。
真实资金动作仍需用户显式批准（本层 can_order=false 永不变）。

## 当前边界（如实）

- **真实数据链路受阻**：`data/snapshot` 为空（数据源待接入）→ `env`
  恒 `UNAVAILABLE`、C3 链与行情证据无从产生。本层**已就绪但跑不出真实 BUY**，
  直至数据源授权解冻。**不得用沙盒夹具冒充真实决策。**
- S1 / S3 知识库可真实消费（已晋升 v1.1.0 / v1.0.0），S3 当日快照在 2026-09-29
  为真数据。
