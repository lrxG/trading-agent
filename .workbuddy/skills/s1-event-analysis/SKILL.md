# S1 事件催化评估 · 方法层（SKILL）

> 版本 1.0 · 2026-10-09 · 对应施工方案 2.1（已批准）· 执行件 `tools/analysis/s1.py` ·
> 知识真源 `data/knowledge/s1_event_effects/`（F7 效应库，经 `load_knowledge("s1", as_of)` 读取）

## Design Boundary（2.10 强制段——防退化成 prompt 解析器）

S1 **只供专业判断素材，不越权决策**（S 系列总规则 2）：

- S1 输出**零 BUY / WAIT / NO、零仓位、零动作**——输出结构上没有 action 字段，
  执行件落盘前有机械断言（`_assert_neutral`），违者拒绝落盘；
- S1 输出 = C3 A 类驱动的**候选证据来源之一**：仍必须经 C3 证据链校验
  （`drive.py` review：证据可解析、三槽分离）→ C4 三问决策 → C5 仓位闸。
  **S1 不得直接触发 BUY，不得绕过 C5**——这是 C3/C4/C5 三强制约束的一部分；
- strength 来自 F7 效应库机械映射（py 现场重算公式），**LLM 不许拍脑袋标"强"**；
- S1 运行**不改知识库**（两期分离：决策运行期只读，维护期回填走 C6 验证）；
- 判断本体（传导链、证伪条件细化、定价状态判断）在 LLM 层；机械件只做
  归类 / 匹配 / 映射 / 校验 / 装配。缺事实就是缺（MISSING 显式标记），**禁补造**。

## 输入契约（8 项 · 缺失显式标记）

| # | 项 | 字段 | 必填 | 说明 |
|---|---|---|---|---|
| 1 | 事件事实 | `title`（+`body` 可选） | ✅ | 缺 → 拒评（不评估无事实事件） |
| 2 | 来源与时间 | `sources[]`（source_id/kind/ref/ts）+ `first_seen_at` + `announcement_at` | ✅ | 首现日 ≠ 公告日 ≠ 市场充分反应日，**分列禁混** |
| 3 | 事件类别 | `cols`（官方公告分类串，`\|` 分隔） | 可缺 | 缺 → MISSING 标记 + 降置信；py 只做官方分类→研究类机械映射（title 语义匹配属 LLM 层） |
| 4 | 影响对象 | `related_codes` | ✅ | 非空列表 |
| 5 | 传导链 | `transmission_path` | 可缺 | LLM 槽；缺 → `UNKNOWN_BY_DESIGN` |
| 6 | 预期兑现窗口 | `window_hint` | 可缺 | 缺 → 机械默认效应表 T+20 / UNKNOWN |
| 7 | 证伪条件 | `falsifier` | 可缺 | LLM 槽；缺 → MISSING（禁编造数值判据） |
| 8 | 历史相似案例 | `historical_analogs` | 可缺 | 个例由输入提供；统计匹配由效应库机械给出 |

时间纪律：`announcement_at > as_of` 或证据 `ts > as_of` → 拒评（FUTURE_EVENT，
不评估未来）；公告距 as_of 超 60 自然日 → EXPIRED_EVENT 警示（自然日近似，
交易日历落盘后收紧）。

## 处理链（机械部分 = s1.py；判断部分 = 本 skill 的 LLM 流程）

1. **事件归类**（机械）：官方 226 类 → 31+1 研究类（`taxonomy.json`，
   CAT_MAP 先验方向 + 关键词兜底 + 反义词排除）。多类取第一个非中性类。
2. **效应库匹配**（机械）：`historical_effect(cat)` → 净效应 / 样本量 / 证据等级 /
   strength 档位。n<30 → `INSUFFICIENT_SAMPLES`（只信 n≥30 的格子）；
   库无记录 → `NO_RECORD`。**无统计结论时 strength 恒 UNKNOWN，禁造胜率**。
3. **矛盾检测**（机械）：先验方向 vs 实测符号冲突 → `DIRECTION_CONFLICT_WITH_PRIOR`
   （以实测为准）；多来源时点不一致 → `SOURCE_TS_INCONSISTENT`。
4. **LLM 判断层**（本 skill 方法）：
   - 传导链：事件 → 催化强度 → 兑现路径（供给收缩/需求外溢/成本变化/流动性…）
     → 定价窗口。只允许引用输入事实 + 效应库统计上下文，**每个环节挂证据引用**；
   - 定价状态辅助判断：对比 first_seen_at / announcement_at / 价格反应分列数据
     （价格通道未建前恒 UNKNOWN，不冒充判断）；
   - 证伪条件细化：把窗口/方向写成 C3/C4 可消费的可证伪判据（无数值依据时留空）。
5. **结构化输出**（机械装配 + 落盘）：`data/generated/s_s1_<as_of>.json`
   （唯一键 as_of，事件级按 event_id 去重合并；跨 as_of 独立快照）。

## 输出 schema（2.1 批准 + 2.10 三件套）

```json
{
  "s_id": "s1", "as_of": "...", "event_id": "EV-<md5>",
  "type": "<研究类|UNCLASSIFIED>", "type_basis": "official|keyword|none",
  "strength": "强|中|弱|微|UNKNOWN",
  "net_effect": {"pct": 1.76, "sample_size": 3448, "benchmark_rate_pct": 0.36,
                  "benchmark_method": "等权均值...", "period": "2026-01~08",
                  "window": "T+20...", "evidence_grade": "A",
                  "kb_version": "1.0.0", "kb_as_of": "2026-09-21"},
  "path": "<传导链|UNKNOWN_BY_DESIGN>", "window": "T+20|UNKNOWN",
  "priced": "UNKNOWN",
  "evidence": [{"source_id": "...", "kind": "news_id|公告号", "ref": "...", "ts": "..."}],
  "confidence": 0.0-0.9,
  "slots": {"evidence": {...}, "inference": {...}, "prediction": {...}},
  "missing": [...], "warnings": [...]
}
```

- **strength ≠ confidence**：strength = 历史实证催化强度档位（效应库映射）；
  confidence = 本次归类/匹配/判断可信度（机械规则：归类依据 + 效应等级 − 缺失 − 警示，封顶 0.9）。
- **net_effect 带统计上下文**：sample_size / benchmark / period / evidence_grade
  必在——"+1.76%" 不是孤立伪精确，必须相对 +0.36% 事件类基准率读。

## F7 效应库关键口径（评估必读）

1. 净效应相对 **+0.36% 事件类基准率**读，不能相对 0；
2. 匹配对照是唯一可用于决策的量；安慰剂只做否决（pre/post 同号且 post≤pre =
   趋势延续，如停复牌）；朴素 post 只读形态不定方向；
3. 环境分档：偏弱市事件最有效，**强势市（>8%）事件全部归零**——强势市禁引用本表；
4. 位置修正：利空前 10 日深跌 <-20% 最毒（−6.77%）；情绪票（近 5 日涨停过）遇利空
   显著更危险（−1.97% vs 冷票 −0.00%）；
5. 先验方向可被实测推翻（询价转让/补贴/业绩快报/资质认证/经营数据等均有翻转）——
   **以实测净效应符号为准**。

## 消费与回填

- C3 消费：`from tools.analysis.s1 import load_output; load_output(as_of)`（经
  loader 上游产物入口）；S1 结论进入 C3 证据链后按 drive.py 契约逐条 ref 可解析；
- 结果回填（T+1/T+5/T+20 实际反应）：S1 运行期**不写**；由 C6 维护期验证后
  版本化回填 effect_base.json（append 证据列 + bump version + 更新 as_of）。
