# -*- coding: utf-8 -*-
"""live_seed.py · 活数据采集器（westock CLI → 0.2 契约播种 · 2026-10-19 建 · v1.1 增量口径）。

**为什么有本件**：`seed_real.py` 只能从旧包 bus 取数（源止于 2026-09-30）；通道实证
（`data/research/channel_verification_westock_cli_20261019.md`）确认 westock CLI 本机畅通
⇒ 本件把 CLI 输出按 0.2 契约映射成 payload，经 `ingest.stage_core` 落投放区。

## 设计：增量口径（v1.1 · 2026-10-19 实测后改）

CLI 批量有**静默上限**（实测：单次 ≤100 码——150 码只回 70 且摘要同步缩水；多日窗另有
约 840 行上限）。故本件**不拉窗口**，只做三件事：

| 需要什么 | 从哪来 | 为什么可行 |
|---|---|---|
| 当日 OHLCV | `kline <100码> --start D --end D`（53 次/日 ≈ 3 分钟） | 单日单批不触发行上限 |
| 前收盘（算涨停价/封板） | **前一日已投放 `daily_quote.csv` 的 close** | 增量播种天然有序；无需再取 |
| 连板数（boards） | **前一日已投放 `limit_up.csv` 的 boards` + 1**（两日皆封时） | 连板定义即递推 |
| MA20 | `kline sh000001 --start D-40 --end D`（单码，1 次） | 指数单独取 |

**优于窗口拉取**：3 分钟/日、无截断、无重复取数、可逐日推进。

## 口径纪律（与 seed_real CALIBER 逐条对齐，禁第二套映射）

| 键 | 活口径 |
|---|---|
| daily_quote | code/name(前一日)/open/high/low/close(=last)/pct(=change_pct)/vol(**volume×100** 手→股)/amount；剔指数行 |
| env_metrics | limit_up_count←**当日封板自算数**（changedist 无 --date，历史日不可用）；max_board←池内 boards 最大；turnover←当日 amount 求和；up_down_ratio←当日 pct>0 计数 ÷ pct<0 计数；fail_rate←触板未封 ÷(封板+触板未封)（分母 0 ⇒ None + 审计）；index_ma20←MA20(sh000001 含当日) |
| limit_up | 自算池（close==涨停价：主板10%/创业科创20%/ST5%，四舍五入到分）；boards 递推；**first_seal_time/seal_amount/reason 三列无直给源 ⇒ 显式 None（声明件登记）** |
| dragon_tiger | **已接通（2026-10-22）**：`westock lhb --date D` 默认榜 → `code/name/buyAmount/sellAmount/netBuyAmount` 与契约 6 列**逐列对应**；`seat` 诚实标注为 `lhb_stock_level(n=N)`（股票级汇总，**非席位名**）；net 保留负值（净卖出是事实，消费方按 `net>0` 过滤）。旧「声明空」基于旧包源（两张异维度聚合表），已被本通道推翻 |
| news | **声明空**（notice 按票批量，无全市场入口）｜**注**：日链第 9 步 `fetch_news.py` 已独立采集 `data/raw/<d>/news.jsonl`，本文件不重复负责 |

**当日（date == today）额外校验**：拉 `changedist` 与自算值交叉核对，差异写 `audit`，
**不以 changedist 为源**（它无历史日期参数）。

## 用法

```
python tools/e2e/live_seed.py --check                             # 通道探测（只读）
python tools/e2e/live_seed.py --cross-check --date 2026-09-30     # 口径对账（只读）
python tools/e2e/live_seed.py --plan --date 2026-10-16            # 沙盒构建+契约校验（零写入）
python tools/e2e/live_seed.py --stage --date 2026-10-16 --execute # 生产播种（须 writegate 票据）
```

**缺源行为**：任一 CLI 失败 ⇒ 该键零行 + 原因入 audit，**不静默补零**；`--stage` 前置断言
「覆盖 ≥ 宇宙 60%」（残日不得当整日）。

## 已知数据源特性（2026-10-19 对账实测 · 09-30 双跑）

① **复权基准漂移**：CLI 返回**前复权到今日**的序列，故「用今天的接口重取历史日」与
   已存件（前复权到当时基准）**在除权票上必然不同**（实测 795/5213 码，9-10 月中报
   分红季）。**对本件实际用途（播种新日期）无影响**——新日期只取一次、不重取历史。
   ⇒ 禁拿「重取历史」替代已存件；确需改历史值时走 `seed_real --reseed` 并留变更日志。
② **turnover 逐元级吻合**：自算 Σamount 与官方两市成交额相对差 2.4e-7（09-30 实测）。
③ **limit_up_count/up_down_ratio 有口径差**：自算封板数 vs 权威涨停家数（09-30：50 vs 53，
   差在"盘中触板后回落"与不到收盘封板边界）；up_down_ratio 旧口径来自 env_history。
   ⇒ 两者以自算为准并**在 audit 里与 changedist 交叉登记**（当日才可得），差量入留痕。
④ **index_ma20 微差**（09-30：3905.637 vs 3905.924，0.007%）：窗口/复权基准差异，
   量级可忽略，登记不修（生态差异，非本件可控）。
"""
import io
import json
import os
import re
import subprocess
import sys
from decimal import Decimal, ROUND_HALF_UP

_HERE = os.path.dirname(os.path.abspath(__file__))
_FS = os.path.normpath(os.path.join(_HERE, "..", "fs"))
if _FS not in sys.path:
    sys.path.insert(0, _FS)
import ingest as ING          # noqa: E402
import paths as P             # noqa: E402
import limit_rules as LIMIT  # noqa: E402  ★ 涨跌幅单真源（2026-11-02）
import cli_codes as CC       # noqa: E402  ★ CLI 代码格式单真源（2026-11-02）
import snapshot as S          # noqa: E402

ROOT = P.ROOT
SOURCE_LABEL = "westock_cli"
WESTOCK = "westock"
# 实测边界（2026-10-19 二次测定，见 data/research/channel_verification_*.md 第五节）：
#  · 单次请求**实际上限 ≈80 码**（请求 138 只回 80；请求 100 常回 80）
#  · **连续高频调用会熔断**：紧随其后的批次直接返回 0 行（不是报错，是空表）
#  ⇒ BATCH=60 留余量 + 批间 2.5s + 空响应当熔断冷却重试（不计丢码）
BATCH = 60
PAUSE_SEC = 2.5                # 批间节流
COOLDOWN_SEC = 18.0            # 熔断冷却（空响应/零回码）
COVERAGE_MIN = 0.60            # 残日阈值：daily_quote 行数 / 宇宙
PCT_MAIN, PCT_GROWTH, PCT_ST = 0.10, 0.20, 0.05

_FNAME_OF = {spec["key"]: f for f, spec in S.CONTRACT.items()}


class LiveSeedError(Exception):
    """REASON_CODE 前缀错误。"""


# ── CLI 与解析 ──────────────────────────────────────────────────────────────
_HARD_ERR = ("[BIZ_ERROR]", "状态: error", "状态:error")


def _biz(text):
    """硬错误摘要（`[BIZ_ERROR]` / `状态: error`）；无硬错误返回 ''。

    ★ 2026-11-02：这些标记**不区分成因**——代码格式不符会报，瞬时限流也会报
      （实测：连发两批 60 码，第二批可能整批 `状态: error`，冷却后同批可成功）。
      所以 **不能在 _run 里见标就抛**，否则一次限流就打穿整条日链。
      正确做法：标出来交给调用方，由「是否全批零成功」这一更强判据决定要不要 fail-loud。
    """
    for mark in _HARD_ERR:
        if mark in (text or ""):
            return CC.cli_error(text) or mark
    return ""


def _run(args, timeout=180):
    try:
        p = subprocess.run([WESTOCK] + list(args), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        raise LiveSeedError("CLI_MISSING: 未找到 westock 命令")
    except subprocess.TimeoutExpired:
        raise LiveSeedError("CLI_TIMEOUT: %s" % " ".join(args[:3]))
    if p.returncode != 0:
        raise LiveSeedError("CLI_FAIL(%s): %s"
                            % (p.returncode, (p.stderr or p.stdout)[:200]))
    # ★ 2026-11-02 事故修：westock CLI **业务错误不改退出码**（恒 0），
    #   过去只判 returncode ⇒ 坏通道被当成功，调用方把"整批零回码"理解成
    #   限流，于是 sleep 冷却 + 多轮重取，日链表面在跑、实际空转 26 分钟。
    #   ⇒ 这里只做「摘出错误串」，不抛；判死交给 fetch_* 的"全批零成功"判据。
    return p.stdout or ""


def parse_md_table(text):
    """markdown 表格 → [dict]（表头列名取值；跳过分隔/空行；取第一张表）。"""
    rows, header = [], None
    for ln in text.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            if header and rows:
                break
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c or "-") for c in cells):
            continue
        if header is None:
            header = cells
            continue
        if len(cells) == len(header):
            rows.append(dict(zip(header, cells)))
    return rows


def _f(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:                                          # noqa: BLE001
        return None


def fetch_kline_day(codes, date, max_rounds=4, pause=None, time_budget=1500):
    """单日批量日 K → ({code: bar}, [confirmed_missing])。

    ★ 实测（2026-10-19）：CLI 批内会**静默丢码**——请求 100 码只回 80（摘要同步显示
      成功 80），且连续高频调用会被限流（3.3s → 15s/次）。故：逐批比对「请求码 vs 回码」，
      **丢的码进入下一轮重取**（最多 max_rounds 轮）；每批之间 pause 秒节流。
      仍取不到的码 = 该日无成交/停牌/退市 ⇒ 一次性返回为 confirmed_missing（由调用方审计）。

    ★ 2026-11-02 补两道护栏（当日日链跑 26 分钟不前进的根因就在本函数）：
      ① **代码格式**：统一走 `cli_codes.to_cli_csv` 归一到交易所前缀码；
         回码**原样使用**（项目内部主键恒为前缀码，禁止 to_bare，见 cli_codes 文档）。
      ② **整体时间预算** `time_budget`：限流冷却重试在「通道整体坏掉」时会变成
         无限变慢的空转（每批 2 次调用 + 18s 冷却 × 轮数）。到预算就**提前收工**，
         已拿到的照常返回、未拿到的进 confirmed_missing，由调用方按 PARTIAL 处理——
         宁可交残日，也不许静默挂死整条链。
      ③ **判死判据**：只有「**全批零成功** 且有硬错误」才 fail-loud 抛
         `KLINE_CHANNEL_DEAD`。零星批次报错是瞬时限流（实测冷却后同批可成功），
         按原设计进下一轮重取，不许一次限流就打穿日链。
    """
    import time as _t
    pause = PAUSE_SEC if pause is None else pause
    t0 = _t.monotonic()
    truncated = False
    hard_err = ""
    out, missing = {}, list(codes)
    for rnd in range(max_rounds):
        if not missing:
            break
        still = []
        for i in range(0, len(missing), BATCH):
            if _t.monotonic() - t0 > time_budget:
                truncated = True
                break
            chunk = missing[i:i + BATCH]
            text = _run(["kline", CC.to_cli_csv(chunk), "--period", "day",
                         "--start", date, "--end", date])
            rows = parse_md_table(text)
            # ★ 熔断探测：整批零回码 = 被限流（实测紧随高频道就会空表，也可能整批报
            #   `状态: error`）⇒ 冷却后原地重试一次；仍空则把该批记为待下轮
            #   （不当作「该日无数据」）。
            if not rows:
                hard_err = _biz(text) or hard_err
                _t.sleep(COOLDOWN_SEC)
                text = _run(["kline", CC.to_cli_csv(chunk), "--period", "day",
                             "--start", date, "--end", date])
                rows = parse_md_table(text)
                if not rows:
                    hard_err = _biz(text) or hard_err
            got = set()
            for r in rows:
                # ★ 不要在这里 to_bare：本项目内部口径恒为**带前缀码**
                #   （prev 的 daily_quote.csv 主键就是 sh600113），削前缀会让
                #   prev_closes 对不上、整日 universe 匹配失败。
                c = r.get("code") or (chunk[0] if len(chunk) == 1 else None)
                if not c:
                    continue
                got.add(c)
                out[c] = {"date": r.get("date"), "open": _f(r.get("open")),
                          "last": _f(r.get("last")), "high": _f(r.get("high")),
                          "low": _f(r.get("low")), "volume": _f(r.get("volume")),
                          "amount": _f(r.get("amount")),
                          "change_pct": _f(r.get("change_pct"))}
            still.extend(c for c in chunk if c not in got)
            if i + BATCH < len(missing):
                _t.sleep(pause)
        if truncated:
            # 预算耗尽：直接收工，未取到的码在函数末尾统一算进 todo
            break
        missing = still
    todo = [c for c in missing if c not in out]
    if truncated:
        print("  ⚠️ kline 采集**超时间预算** %ds：已取 %d / 待取 %d，余下按 PARTIAL 处理"
              % (time_budget, len(out), len(todo)))
    if not out and hard_err:
        # 一份都没拿到 + 有硬错误 ⇒ 通道/格式真坏，立刻报，不要拖成"整日空投放"
        raise LiveSeedError("KLINE_CHANNEL_DEAD: 全批零成功｜%s｜首批 %d 码"
                            % (hard_err, min(BATCH, len(codes))))
    if hard_err:
        print("  ⚠️ kline 有批次报错（疑似瞬时限流，已重取）：%s；最终取到 %d/%d"
              % (hard_err[:100], len(out), len(codes)))
    return out, sorted(todo)


def fetch_index_ma20(date, days=40, retries=3):
    """sh000001 MA20（含当日）；不足 20 根 ⇒ None。

    ★ 2026-10-20 修（实测 10-12/10-13 连续 null 而 10-09 有值）：批量 kline 采完
    紧接单取指数会撞进熔断窗口（连续高频 ⇒ 空表非报错）⇒ 空响应当熔断，冷却
    COOLDOWN_SEC 后重试（与 fetch_kline_day 同款处置，最多 retries 次）。
    """
    import datetime as _dt
    import time as _t
    start = (_dt.date.fromisoformat(date) - _dt.timedelta(days=days)).isoformat()
    rows = []
    for attempt in range(retries + 1):
        rows = parse_md_table(_run(["kline", "sh000001", "--period", "day",
                                    "--start", start, "--end", date]))
        if rows:
            break
        if attempt < retries:
            _t.sleep(COOLDOWN_SEC)
    bars = sorted((r for r in rows if r.get("date") and r["date"] <= date),
                  key=lambda x: x["date"])
    closes = [_f(r.get("last")) for r in bars[-20:]]
    if len(closes) == 20 and all(c is not None for c in closes):
        return round(sum(closes) / 20.0, 4)
    return None


def fetch_dragon_tiger(date):
    """全市场龙虎榜 → 契约 6 列（2026-10-22 接入）。

    ★ 推翻旧声明：本文件原写「dragon_tiger 声明空（`fund lhb` 个股权总额 ≠
      契约席位粒度；营业部明细实测可空 ⇒ 不拼假席位）」。该判断基于**旧包源**
      （`bus/lhb/lhb_<date>.md`，两张异维度聚合表，确无 1:1 映射）。
      但 `westock lhb --date D`（默认榜单）实测直接给出
      `code / name / buyAmount / sellAmount / netBuyAmount`，与契约
      `code,name,seat,buy,sell,net` **逐列对应**，无需推导、无需拼凑。

    口径（诚实标注，不冒充席位明细）：
      · `seat` = `lhb_stock_level(n=N)` —— **股票级汇总**，N = 该票当日明细行数。
        它**不是**席位名，禁止当营业部用；真席位明细在 `--type activeseat`
        （席位×股票聚合、无 sell/net，不满足契约 ⇒ 不混入）。
      · sell = sellAmount 直取（不推导）；机构榜才需 totalBuyAmt-netBuyAmt 推导，
        本函数不取机构榜 ⇒ 无派生值。

    `net` 保留原值（含负）：净卖出是**真实事实**，消费方（mech_driver）自行按
    `net > 0` 过滤。剔掉负值会让「在榜但净卖出」与「不在榜」无法区分。

    失败即抛错 ⇒ 调用方降级为声明空（不静默补零）。
    """
    txt = _run(["lhb", "--date", date], timeout=120)
    rows = parse_md_table(txt)
    if not rows:
        raise LiveSeedError("LHB_EMPTY: 龙虎榜无返回（%s）" % date)

    agg = {}
    for r in rows:
        code = (r.get("code") or "").strip()
        if not code:
            continue
        a = agg.setdefault(code, {"code": code,
                                  "name": (r.get("name") or "").strip(),
                                  "buy": 0.0, "sell": 0.0, "net": 0.0, "n": 0})
        a["buy"] += _f(r.get("buyAmount")) or 0.0
        a["sell"] += _f(r.get("sellAmount")) or 0.0
        a["net"] += _f(r.get("netBuyAmount")) or 0.0
        a["n"] += 1

    out = []
    for c in sorted(agg):
        a = agg[c]
        out.append({"code": a["code"], "name": a["name"],
                    "seat": "lhb_stock_level(n=%d)" % a["n"],
                    "buy": round(a["buy"], 2), "sell": round(a["sell"], 2),
                    "net": round(a["net"], 2)})
    return out


def fetch_changedist():
    """当日涨跌分布（**仅作当日自算的交叉校验**；无历史日期参数）。"""
    txt = _run(["changedist"])
    rows = parse_md_table(txt)
    if not rows:
        raise LiveSeedError("CHANGEDIST_EMPTY")
    r = rows[0]
    amt = None
    m = re.search(r"两市成交额[：:]\s*([\d.]+)\s*亿", txt)
    if m:
        amt = float(m.group(1)) * 1e8
    return {"up": int(_f(r.get("上涨")) or 0), "down": int(_f(r.get("下跌")) or 0),
            "limit_up": int(_f(r.get("涨停")) or 0),
            "limit_down": int(_f(r.get("跌停")) or 0), "amount_yuan": amt}


# ── 口径计算 ───────────────────────────────────────────────────────────────
def limit_pct(code, name):
    """委托 `fs/limit_rules`（2026-11-02 收敛；原实现缺 bj 分支 ⇒ 北证错落 10%）。"""
    return LIMIT.limit_pct(code, name)


def limit_price(prev_close, pct):
    if prev_close is None:
        return None
    v = Decimal(str(prev_close)) * (Decimal(1) + Decimal(str(pct)))
    return float(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def implied_prev_close(bar):
    """从**当日** close/pct 反推真实前收盘（四舍五入到分）。

    ★ 为什么必须反推（2026-10-19 实测修）：CLI 返回的前复权序列以**查询当日**为基准，
    而库里历史日来自旧包（基准=各自当日）⇒ 两套价格基准混用会把涨停价全算错。
    10-09 实测：用库里 09-30 的 close 当 prev ⇒ 全市场只判出 **1 只**封板（真实 49 只），
    且 fail_rate 荒唐到 0.989。改用当日自带 pct 反推 ⇒ 09-30 自检 55 家（官方 53）、
    10-09 49 家（合理），完全自洽且不依赖任何历史基准。

    口径：prev = close / (1 + pct/100)，再四舍五入到分。pct 与 close 同源于同日响应，
    不存在跨基准问题；pct 缺失 ⇒ None（退化为「不判封板」，不猜）。
    """
    c, pct = bar.get("last"), bar.get("change_pct")
    if c is None or pct is None:
        return None
    try:
        v = Decimal(str(c)) / (Decimal(1) + Decimal(str(pct)) / Decimal(100))
    except Exception:                                            # noqa: BLE001
        return None
    return float(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_sealed(bar, prev_close, name, code):
    lp = limit_price(prev_close, limit_pct(code, name))
    return (lp is not None and bar.get("last") is not None
            and abs(bar["last"] - lp) < 0.005)


def is_touched(bar, prev_close, name, code):
    lp = limit_price(prev_close, limit_pct(code, name))
    return (lp is not None and bar.get("high") is not None
            and bar["high"] - lp > -0.005)


def _fill_missing_names(date, codes, names, timeout=180):
    """补齐 names 里缺失的票名（用 `westock quote`，**kline 不返回 name**）。

    ★★ 2026-10-22 修一个**会累积的真缺陷**：
      `names` 只从**前一日 daily_quote** 继承，而 `kline` 接口不返回 name。
      一旦某只票在历史某天缺名，之后**每天都会缺**（10-22 实测 16/4942 行 name 为空，
      且 10-20、10-21 同样缺，说明是从更早一直继承下来的）。
      后果：A9 的 SEMANTIC 判 `REQ为空:name(16/4942行)` ⇒ 快照永远 admitted=false。

      修法：当日采集后，对 names 缺失的票**回退查 quote**（该接口有 name 字段）。
      只补缺的（通常十几只），不重复查全市场。

    返回补到的 {code: name}（可能为空 dict）。
    """
    missing = [c for c in codes if not (names.get(c) or "").strip()]
    if not missing:
        return {}
    out = {}
    step = 40
    for i in range(0, len(missing), step):
        chunk = missing[i:i + step]
        try:
            text = _run(["quote", CC.to_cli_csv(chunk)])
        except Exception:
            continue
        for r in parse_md_table(text):
            # 同上：保持带前缀口径，不削前缀
            c = (r.get("code") or "").strip()
            nm = (r.get("name") or "").strip()
            if c and nm:
                out[c] = nm
        if i + step < len(missing):
            _t.sleep(0.5)
    return out


def build_day(date, codes, names, bars, prev_closes, prev_boards, changedist=None,
              index_ma20=None):
    """→ (payload, audit)。增量口径：只用当日 bars + 前一日 close/boards。"""
    dq, sealed_rows = [], []
    for c in codes:
        b = bars.get(c)
        if not b or b.get("last") is None:
            continue
        dq.append({"code": c, "name": names.get(c) or "", "open": b["open"],
                   "high": b["high"], "low": b["low"], "close": b["last"],
                   "pct": b["change_pct"],
                   # 口径：CLI volume = 手；契约 vol = 股 ⇒ ×100（同 seed_real）
                   "vol": (b["volume"] * 100 if b["volume"] is not None else None),
                   "amount": b["amount"]})
        _pc = implied_prev_close(b) or prev_closes.get(c)
        if is_sealed(b, _pc, names.get(c), c):
            sealed_rows.append({"code": c, "name": names.get(c) or "",
                                "boards": int(prev_boards.get(c) or 0) + 1,
                                "first_seal_time": None, "seal_amount": None,
                                "reason": None})
    touched_unsealed = 0
    for c in codes:
        b = bars.get(c)
        if not b or b.get("last") is None:
            continue
        _pc2 = implied_prev_close(b) or prev_closes.get(c)
        if (is_touched(b, _pc2, names.get(c), c)
                and not is_sealed(b, _pc2, names.get(c), c)):
            touched_unsealed += 1
    sealed_n = len(sealed_rows)
    fail_rate = None
    audit = {"caliber": "增量自算（封板判定用当日 pct 反推前收·同源自洽；"
                        "boards 递推前一日已投放件）",
             "n_sealed": sealed_n, "n_touched_unsealed": touched_unsealed}
    if sealed_n + touched_unsealed > 0 and sealed_n > 0:
        fail_rate = round(touched_unsealed / float(sealed_n + touched_unsealed), 6)
    else:
        audit["fail_rate_uncomputable"] = "封板 0 ⇒ 分母为 0，置 None"
    up = sum(1 for r in dq if (r["pct"] or 0) > 0)
    down = sum(1 for r in dq if (r["pct"] or 0) < 0)
    turnover = sum(r["amount"] for r in dq if r["amount"] is not None) or None
    env = {"limit_up_count": sealed_n,
           "max_board": (max(x["boards"] for x in sealed_rows) if sealed_rows else None),
           "turnover": turnover,
           "up_down_ratio": (round(up / float(down), 4) if down else None),
           "fail_rate": fail_rate, "index_ma20": index_ma20}
    audit.update({"n_up": up, "n_down": down, "n_rows": len(dq)})
    if index_ma20 is None:
        audit["index_ma20_missing"] = ("指数 MA20 取数失败（熔断冷却重试后仍空）"
                                       "⇒ 显式 None（非省略）；消费方按缺 MA20 处置，"
                                       "禁拿旧日值冒充当日")
    if changedist:                                   # 仅当日交叉校验，不改源
        audit["cross_changedist"] = {
            "limit_up_ref": changedist.get("limit_up"), "limit_up_self": sealed_n,
            "turnover_ref": changedist.get("amount_yuan"), "turnover_self": turnover}
    return ({"daily_quote": dq, "limit_up": sealed_rows, "env_metrics": env,
             "news": [], "dragon_tiger": []}, audit)


def _provenance(date, source=SOURCE_LABEL):
    """A9 准入所需的溯源声明（AS_OF / COVERAGE 两类判定的输入）。

    ★★ 2026-10-22 补：此前 `_declaration()` **完全没有 provenance 字段**，
      导致 `ingest.promote_snapshot` 继承到 None ⇒
      `snapshot.write_snapshot` 把 AS_OF/COVERAGE 一律记为「未检查」
      ⇒ **每个快照的 `_admission.json` 都是 `admitted: false`**
      （实测 2026-10-14 ~ 2026-10-22 全部如此，从无一天通过）。

    这不是"数据有问题"，是**声明链断了一环**：
    采集端明明知道 as_of 和采集时点，却没有把它写进声明件。

    ## 各项含义（口径必须诚实，不能为了过闸而抬高）

    · `as_of_label = "close_final"`：本采集**在收盘后**跑，拿的是收盘定格数据。
      判据见 `admission.AS_OF_MIN_EVIDENCE`：`close_final` 最低需 **E3**
      （采集日志 with 真实 ts）。我们确实有 `_core.meta.json` 里的
      `fetched_at` 时间戳 ⇒ E3 成立，**不是抬价**。
    · `evidence_level = "E3"`：如实取最低充分等级。E4（调度设计）与 E5（交叉复现）
      我们并没有做，**不得claim 更高**。
    · `universe`：必须显式声明三个布尔 + complete 的理由。
      - `include_st=False`：ST 股在 klines 自算池里**未被识别并剔除**，
        故如实声明「未包含 ST 过滤」——注意这**不是**说数据里没 ST 股，
        而是说**我们没有做这个过滤**。
      - `include_bse=False`：北交所**确实未覆盖**（R-2 已登记）。
      - `complete=False`：因为上面两条，覆盖不完整，给 reason。
    这样声明后 `check_coverage` 会判 **BLOCKED**——**这是对的**，
    因为它反映事实。我们不为过闸把 `complete` 写成 True。
    ★ 结构必须**平铺**：`{契约文件名 或 key: {as_of_label, evidence_level, universe}}`。
      第一版我多包了一层 `{"files": {...}}`，导致
      `_admit_payload` 里 `prov.get(fname)` 取不到 ⇒ AS_OF/COVERAGE 仍记「未检查」，
      白白排查了半小时。`snapshot._admit_payload` 只认平铺。
    """
    files = {}
    for fname in ("daily_quote.csv", "limit_up.csv", "env_metrics.json",
                  "news.jsonl", "dragon_tiger.csv"):
        files[fname] = {
            "as_of_label": "close_final",
            "evidence_level": "E3",
            "universe": {
                "include_st": False,
                "include_bse": False,
                "complete": False,
                "reason": "北交所未覆盖（R-2 已登记）；ST 未做识别剔除；"
                          "dragon_tiger/news 无全市场直给源（见 key_declarations）",
            },
        }
    # ★ 平铺返回（不套 "files" 层）—— snapshot._admit_payload 按文件名/key 直接取
    out = dict(files)
    out["as_of"] = date          # 冗余但无害：_admit_payload 只按 CONTRACT 的 fname/key 取
    out["source"] = source
    return out


def _declaration(date, declared_keys, ma20_missing=False):
    notes = {
        "limit_up": ("klines 自算池：code/name/boards 可信（boards 递推）；"
                     "first_seal_time/seal_amount/reason 无直给源（CLI 无 zt_pool）"
                     "⇒ 显式 None，禁以估算冒充实测"),
        "dragon_tiger": ("westock lhb 默认榜 → 契约 6 列逐列对应（2026-10-22 已接通）；"
                         "seat 标注 lhb_stock_level(n=N) 属股票级汇总非席位名；"
                         "net 保留负值由消费方按 >0 过滤"),
        "news": "notice 按票批量，无全市场单次入口 ⇒ 显式空",
    }
    decls = {k: {"status": "EXPLICIT_EMPTY", "src": SOURCE_LABEL,
                 "note": notes.get(k, ""),
                 "empty_form": "显式空（非省略）"} for k in declared_keys}
    if ma20_missing:
        decls["env_metrics.index_ma20"] = {
            "status": "EXPLICIT_NULL", "src": SOURCE_LABEL,
            "note": "指数 MA20 单取熔断冷却重试后仍空 ⇒ env_metrics.index_ma20=null；"
                    "自算键（sh000001 含当日 20 根），非通道缺失",
            "empty_form": "显式空（非省略）"}
    return {"schema": 1, "kind": "EXPLICIT_DECLARATION_LIVE_SEED",
            "authorized_by": "用户 2026-10-19 令：负责把系统建设完善（活数据采集）",
            "as_of": date, "declared_unresolved_keys": sorted(declared_keys),
            "key_declarations": decls,
            # ★ 2026-10-22 补：A9 准入的溯源声明（AS_OF/COVERAGE 判定输入）
            "provenance": _provenance(date)}


def _validate(payload):
    probs = []
    for key, rows in payload.items():
        fname = _FNAME_OF.get(key)
        if not fname:
            probs.append("未知键 %s" % key)
            continue
        if key == "env_metrics":
            if not isinstance(rows, dict) or set(rows) != {
                    "limit_up_count", "max_board", "turnover", "up_down_ratio",
                    "fail_rate", "index_ma20"}:
                probs.append("env_metrics 六键不符")
            continue
        cols = [c for c, _t in S.CONTRACT[fname]["cols"]]
        for i, r in enumerate(rows[:3000]):
            miss = [c for c in cols if c not in r]
            if miss:
                probs.append("%s[%d] 缺列 %s" % (key, i, miss))
                break
    return probs


# ── 前一日上下文（增量口径的关键输入）──────────────────────────────────────
def load_prev(base, date):
    """→ (prev_day, {code: close}, {code: boards}, codes, names)。"""
    import csv
    raw = os.path.join(base or ROOT, "data", "raw")
    if not os.path.isdir(raw):
        raise LiveSeedError("NO_RAW: 投放区不存在")
    days = sorted(d for d in os.listdir(raw)
                  if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d < date
                  and os.path.isfile(os.path.join(raw, d, "daily_quote.csv")))
    if not days:
        raise LiveSeedError("NO_PREV: %s 之前无已投放日（首日须用 seed_real 播种）" % date)
    p = days[-1]
    with io.open(os.path.join(raw, p, "daily_quote.csv"), encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("code")]
    closes = {r["code"]: _f(r.get("close")) for r in rows}
    names = {r["code"]: (r.get("name") or "") for r in rows}
    boards = {}
    lu_p = os.path.join(raw, p, "limit_up.csv")
    if os.path.isfile(lu_p):
        with io.open(lu_p, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("code"):
                    boards[r["code"]] = int(_f(r.get("boards")) or 0)
    return p, closes, boards, [r["code"] for r in rows], names


def collect_day(date, base=None):
    prev_day, prev_closes, prev_boards, codes, names = load_prev(base, date)
    bars, missing = fetch_kline_day(codes, date)
    ma20 = fetch_index_ma20(date)

    # ★ 补齐 names 缺失的票名（kline 不返回 name ⇒ 会从历史永久继承空值）
    filled = _fill_missing_names(date, codes, names)
    if filled:
        names = dict(names)
        names.update(filled)
        print("  [补名] %d 只票名从 quote 回退补齐（kline 不返回 name）"
              % len(filled))
    still_no_name = [c for c in codes if not (names.get(c) or "").strip()]
    if still_no_name:
        print("  ⚠ 仍有 %d 只票名为空（%s…）——A9 SEMANTIC 会如实阻塞"
              % (len(still_no_name), ",".join(still_no_name[:3])))

    dist = None
    import datetime as _dt
    if date == _dt.date.today().isoformat():          # 当日才拉 changedist 交叉校验
        try:
            dist = fetch_changedist()
        except LiveSeedError:
            dist = None
    payload, audit = build_day(date, codes, names, bars, prev_closes, prev_boards,
                              changedist=dist, index_ma20=ma20)

    # ★ 龙虎榜（2026-10-22 接入）：B 路驱动的唯一证据源。
    #   此前恒为 0 行 ⇒ mech_driver 永远判不出 B/复合，驱动三分类退化成单标签。
    #   采不到就如实留空 + audit 记原因（消费方按「在榜为空」处理，不补零）。
    try:
        dt = fetch_dragon_tiger(date)
        payload["dragon_tiger"] = dt
        n_pos = sum(1 for r in dt if (r.get("net") or 0) > 0)
        print("  [龙虎榜] %d 只在榜 · 净买入>0 %d 只" % (len(dt), n_pos))
    except Exception as e:
        payload["dragon_tiger"] = []
        audit["dragon_tiger_failed"] = "%s: %s" % (type(e).__name__, e)
        print("  ⚠ 龙虎榜采集失败：%s（按空投放，audit 留痕）" % e)

    meta = {"prev_day": prev_day, "n_universe": len(codes), "n_klines": len(bars),
            "n_dq": len(payload["daily_quote"]),
            "n_limit_up": len(payload["limit_up"]),
            "n_dragon_tiger": len(payload.get("dragon_tiger") or []),
            "n_missing_codes": len(missing),
            "missing_head": missing[:5],
            "audit": audit,
            "coverage": (round(len(payload["daily_quote"]) / float(len(codes)), 3)
                         if codes else 0.0)}
    return payload, meta


# ── 自检（全夹具 · 零网络 · 零写入）────────────────────────────────────────
def _selftest():
    ok = fail = 0

    def T(name, cond, extra=None):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s%s" % ("✓" if cond else "✗", name,
                               "" if cond else "  ← %s" % (extra,)))

    print("== live_seed --selftest（全夹具）==")
    T("A1 批量表解析（跳 Batch 行/分隔行）", len(parse_md_table(
        "| code | date | open | last |\n| --- | --- | --- | --- |\n"
        "| sh600000 | 2026-10-16 | 9.6 | 9.73 |")) == 1)
    T("A2 涨跌分布解析", parse_md_table(
        "| 上涨 | 下跌 | 涨停 | 跌停 |\n| --- | --- | --- | --- |\n"
        "| 2275 | 3129 | 50 | 27 |")[0]["涨停"] == "50")
    T("B1 涨停价：主板 10% / 创业 20% / ST 5%（四舍五入到分）",
      limit_price(10.0, PCT_MAIN) == 11.0 and limit_price(10.0, PCT_GROWTH) == 12.0
      and limit_pct("sh600001", "*ST某") == PCT_ST)
    T("B2 封板 / 触板未封判定",
      is_sealed({"last": 11.0, "high": 11.0}, 10.0, "某", "sh600001")
      and is_touched({"last": 10.8, "high": 11.0}, 10.0, "某", "sh600001")
      and not is_sealed({"last": 10.8, "high": 11.0}, 10.0, "某", "sh600001"))
    bars = {
        "sh600001": {"date": "2026-10-16", "open": 10, "last": 11.0, "high": 11.0,
                     "low": 10, "volume": 200, "amount": 2000, "change_pct": 10.0},
        "sz300002": {"date": "2026-10-16", "open": 10, "last": 12.0, "high": 12.0,
                     "low": 10, "volume": 60, "amount": 600, "change_pct": 20.0},
        "sh600003": {"date": "2026-10-16", "open": 10, "last": 10.5, "high": 11.0,
                     "low": 10, "volume": 20, "amount": 200, "change_pct": 5.0},
        "sh600004": {"date": "2026-10-16", "open": 10, "last": 9.5, "high": 10.0,
                     "low": 9.4, "volume": 10, "amount": 100, "change_pct": -5.0},
    }
    prev_closes = {c: 10.0 for c in bars}
    prev_boards = {"sh600001": 2}                  # 前一日两连板 ⇒ 今日递推 3
    names = {"sh600001": "某", "sz300002": "创", "sh600003": "触", "sh600004": "跌"}
    payload, audit = build_day("2026-10-16", list(bars), names, bars, prev_closes,
                              prev_boards, index_ma20=3905.9)
    dq = payload["daily_quote"]
    T("C1 daily_quote 行齐且 vol=手×100", len(dq) == 4 and dq[0]["vol"] == 20000.0)
    T("C2 封板池两只且 boards 递推（2→3）", len(payload["limit_up"]) == 2
      and any(x["code"] == "sh600001" and x["boards"] == 3
              for x in payload["limit_up"]))
    T("C3 三列无直给源 ⇒ 显式 None",
      all(x["first_seal_time"] is None and x["seal_amount"] is None
          and x["reason"] is None for x in payload["limit_up"]))
    env = payload["env_metrics"]
    T("C4 env：limit_up_count=封板数 / max_board=3 / up_down_ratio 自算",
      env["limit_up_count"] == 2 and env["max_board"] == 3
      and env["up_down_ratio"] == round(3 / 1.0, 4))
    T("C5 fail_rate = 触板未封/(封板+触板未封) = 1/3（只 sh600003 触未封）",
      env["fail_rate"] == round(1 / 3.0, 6), env["fail_rate"])
    T("C6 index_ma20 透传", env["index_ma20"] == 3905.9)
    T("C7 turnover = 当日 amount 求和", env["turnover"] == 2900.0)
    T("C8 契约校验通过", not _validate(payload), _validate(payload)[:2])
    # ── C9~C12：MA20 缺失显式化 + 指数取数熔断重试（2026-10-20 修）────────────
    p2, a2 = build_day("2026-10-16", list(bars), names, bars, prev_closes,
                       prev_boards, index_ma20=None)
    T("C9 ma20 缺失 ⇒ audit 显式标记（非静默）",
      "index_ma20_missing" in a2 and p2["env_metrics"]["index_ma20"] is None)
    d2 = _declaration("2026-10-16", ("limit_up",), ma20_missing=True)
    T("C10 declaration 载 env_metrics.index_ma20（EXPLICIT_NULL 非 EMPTY）",
      d2["key_declarations"].get("env_metrics.index_ma20", {}).get("status")
      == "EXPLICIT_NULL")
    d3 = _declaration("2026-10-16", ("limit_up",), ma20_missing=False)
    T("C11 ma20 在场 ⇒ declaration 无该键（零噪音）",
      "env_metrics.index_ma20" not in d3["key_declarations"])
    _orig_run, _orig_cd = _run, globals().get("COOLDOWN_SEC")
    globals()["COOLDOWN_SEC"] = 0.0
    _calls = {"n": 0}

    def _empty_run(*a, **k):
        _calls["n"] += 1
        return ""                                   # 恒空 = 恒熔断

    try:
        globals()["_run"] = _empty_run
        v = fetch_index_ma20("2026-10-16")
        T("C12 指数取数恒空 ⇒ 冷却重试 retries+1 次后 None（不抛不猜）",
          _calls["n"] == 4 and v is None)
    finally:
        globals()["_run"] = _orig_run
        globals()["COOLDOWN_SEC"] = _orig_cd
    decl = _declaration("2026-10-16", ("limit_up", "dragon_tiger", "news"))
    T("D1 声明件三键齐且逐键有 note",
      decl["declared_unresolved_keys"] == ["dragon_tiger", "limit_up", "news"]
      and all(v.get("note") for v in decl["key_declarations"].values()))
    T("D2 声明件不带数据值", all(set(v) <= {"status", "src", "note", "empty_form"}
                                for v in decl["key_declarations"].values()))

    # ── D3~D6 声明件必须携带 provenance（A9 准入的输入；此前断链导致永远不通过）──
    T("D3 声明件含 provenance 字段（此前缺失 ⇒ 准入永远阻塞）",
      "provenance" in decl, str(list(decl.keys())))
    prov = decl.get("provenance") or {}
    files_p = {k: v for k, v in prov.items()
               if k.endswith((".csv", ".json", ".jsonl"))}
    T("D4 provenance 覆盖全部 5 个契约文件",
      len(files_p) == 5, str(sorted(files_p)))
    T("D5 每个文件都给 as_of_label + evidence_level",
      all(v.get("as_of_label") and v.get("evidence_level")
          for v in files_p.values()),
      str([(k, v.get("as_of_label"), v.get("evidence_level"))
           for k, v in list(files_p.items())[:2]]))
    T("D6 universe 三布尔显式声明（缺一即阻塞）",
      all(isinstance(v.get("universe", {}).get(k), bool)
          for v in files_p.values()
          for k in ("include_st", "include_bse", "complete")))
    # D7 口径诚实性：complete 必须为 False（我们确实没覆盖北交所 + 没剔 ST）
    T("D7 ★ complete 如实写 False（不为过闸抬价）",
      all(v["universe"]["complete"] is False for v in files_p.values()))
    T("D8 ★ evidence_level 如实取 E3（不 claim 未做的 E4/E5）",
      all(v["evidence_level"] == "E3" for v in files_p.values()))
    _orig = ING.stage_core

    def _boom(*a, **k):
        raise AssertionError("selftest 不应写盘")

    try:
        ING.stage_core = _boom
        build_day("2026-10-16", list(bars), names, bars, prev_closes, prev_boards)
        _validate(payload)
        _declaration("2026-10-16", ("limit_up",))
        T("E1 自检零写点（stage_core 换成抛错后全程不触发）", True)
    except AssertionError:
        T("E1 自检零写点（stage_core 换成抛错后全程不触发）", False, "触发了写盘")
    finally:
        ING.stage_core = _orig

    # F 龙虎榜映射（2026-10-22 接入 · 全夹具零网络）
    md_lhb = ("**全市场龙虎榜**（2026-10-22，3只）\n\n"
              "| rank | code | name | changePct | netBuyAmount | buyAmount | sellAmount |\n"
              "| --- | --- | --- | --- | --- | --- | --- |\n"
              "| 1 | sh600192 | 长城电工 | 10.04 | 38085980.34 | 65965017.49 | 27879037.15 |\n"
              "| 2 | sh600178 | 东安动力 | 10.05 | -21268830.26 | 106345586.78 | 127614417.04 |\n"
              "| 3 | sh600192 | 长城电工 | 10.04 | 1000000.00 | 2000000.00 | 1000000.00 |\n")
    _orig_run = globals()["_run"]

    def _dt_raises():
        globals()["_run"] = lambda args, timeout=180: "（无表格）"
        try:
            fetch_dragon_tiger("2026-10-22")
            return False
        except LiveSeedError:
            return True
        finally:
            globals()["_run"] = lambda args, timeout=180: md_lhb

    try:
        globals()["_run"] = lambda args, timeout=180: md_lhb
        dt = fetch_dragon_tiger("2026-10-22")
        by = {r["code"]: r for r in dt}
        cols = [c for c, _t in S.CONTRACT["dragon_tiger.csv"]["cols"]]
        T("F1 龙虎榜契约 6 列齐（set 完全相等）",
          all(set(r) == set(cols) for r in dt), str(dt[:1]))
        T("F2 同码多行确定性聚合（长城电工 2 行求和）",
          len(dt) == 2 and abs(by["sh600192"]["net"] - 39085980.34) < 0.01
          and "n=2" in by["sh600192"]["seat"], str(by.get("sh600192")))
        T("F3 seat 诚实标注股票级汇总（非席位名，不冒充营业部）",
          all(r["seat"].startswith("lhb_stock_level(") for r in dt))
        T("F4 净卖出保留负值（不剔：在榜≠净买入，消费方自行过滤）",
          by["sh600178"]["net"] < 0, str(by.get("sh600178", {}).get("net")))
        T("F5 空返回抛错（供调用方降级为声明空，不静默补零）", _dt_raises())
    finally:
        globals()["_run"] = _orig_run

    print("== live_seed --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def _main(argv):
    if "--selftest" in argv:
        return _selftest()

    def _val(flag):
        return argv[argv.index(flag) + 1] if flag in argv else None

    base = _val("--base")
    if base and not os.path.isdir(base):
        print("--base 必须是已存在目录", file=sys.stderr)
        return 2
    # 声明空的键（batch/stage 共用；2026-10-19 修：原定义在 batch 分支之后 ⇒ UnboundLocalError）
    declared = ("limit_up", "dragon_tiger", "news")
    if "--check" in argv:
        r = {}
        try:
            r["kline"] = bool(parse_md_table(_run(
                ["kline", "sh600241", "--period", "day", "--limit", "1"])))
        except LiveSeedError as e:
            r["kline"] = str(e)
        try:
            r["changedist"] = fetch_changedist()
        except LiveSeedError as e:
            r["changedist"] = str(e)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0
    if "--batch" in argv:
        d1, d2 = _val("--from"), _val("--to")
        if not d1 or not d2:
            print("⛔ --batch 需要 --from/--to", file=sys.stderr)
            return 2
        cal_p = os.path.join(base or ROOT, "data", "raw", "meta", "trade_cal.csv")
        days = []
        with io.open(cal_p, encoding="utf-8") as f:
            for ln in f:
                parts = ln.strip().split(",")
                if len(parts) >= 2 and parts[1] == "1" and d1 <= parts[0] <= d2:
                    days.append(parts[0])
        if not days:
            print("⛔ 日历窗口内无交易日", file=sys.stderr)
            return 2
        if "--execute" not in argv:
            print("计划（只读）：%d 个交易日 %s" % (len(days), days))
            return 0
        done = []
        for d in days:
            try:
                pay, mt = collect_day(d, base=base)
            except LiveSeedError as e:
                print("⛔ %s 采集失败：%s —— 停止（不得跳跃补数）" % (d, e))
                return 1
            if mt["coverage"] < COVERAGE_MIN:
                print("⛔ %s 覆盖 %.1f%% < %.0f%% —— 停止" 
                      % (d, mt["coverage"] * 100, COVERAGE_MIN * 100))
                return 1
            pr = _validate(pay)
            if pr:
                print("⛔ %s 契约校验失败：%s" % (d, pr[:3]))
                return 1
            ING.stage_core(d, pay, source=SOURCE_LABEL, base=base,
                          replace=("--replace" in argv), data_upto=d,
                          declaration=_declaration(
                              d, declared,
                              ma20_missing=("index_ma20_missing" in mt["audit"])))
            done.append((d, mt["n_dq"], mt["coverage"]))
            print("✅ %s 投放：%d 行 · 覆盖 %.1f%% · limit_up %d"
                  % (d, mt["n_dq"], mt["coverage"] * 100, mt["n_limit_up"]))
        print("完成 %d 日：%s" % (len(done), [x[0] for x in done]))
        return 0
    date = _val("--date")
    if not date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        print("用法: --check | --cross-check --date D | --plan --date D | "
              "--stage --date D --execute | --selftest", file=sys.stderr)
        return 2
    try:
        payload, meta = collect_day(date, base=base)
    except LiveSeedError as e:
        print("⛔ %s" % e, file=sys.stderr)
        return 2
    probs = _validate(payload)
    if probs:
        print("⛔ 契约校验未通过，零写入：\n  " + "\n  ".join(probs[:8]))
        return 1
    if "--plan" in argv:
        print("--plan（沙盒构建 · 零写入）")
        print("  日 = %s · 前一日 = %s · 宇宙 %d 码 · K线命中 %d"
              % (date, meta["prev_day"], meta["n_universe"], meta["n_klines"]))
        print("  daily_quote %d 行（覆盖 %.1f%%）· limit_up %d 行"
              % (meta["n_dq"], meta["coverage"] * 100, meta["n_limit_up"]))
        print("  env_metrics: %s" % json.dumps(payload["env_metrics"], ensure_ascii=False))
        print("  审计: %s" % json.dumps(meta["audit"], ensure_ascii=False))
        print("  声明空键: %s · 契约校验 ✅" % ", ".join(declared))
        return 0
    if "--cross-check" in argv:
        import csv
        raw = os.path.join(base or ROOT, "data", "raw", date)
        ref_p = os.path.join(raw, "daily_quote.csv")
        if not os.path.isfile(ref_p):
            print("⛔ 无参照件：%s（只能对已投放日对账）" % ref_p, file=sys.stderr)
            return 2
        ref_env = os.path.join(raw, "env_metrics.json")
        with io.open(ref_p, encoding="utf-8") as f:
            old = {r["code"]: r for r in csv.DictReader(f)}
        new = {r["code"]: r for r in payload["daily_quote"]}
        common = sorted(set(old) & set(new))
        diffs = []
        adj_drift = []          # 复权基准漂移（预期内，量化告知）
        for c in common:
            for k, atol, rtol in (("close", 0.011, 0), ("pct", 0.02, 0),
                                  ("vol", 1.0, 0), ("amount", 0, 1e-3)):
                a, b = _f(old[c].get(k)), _f(new[c].get(k))
                if a is None or b is None:
                    continue
                if abs(a - b) > max(atol, abs(a) * rtol):
                    diffs.append((c, k, a, b))
            # close 相对漂移（>0.1% 视为复权影响，单列告知不判失败）
            a, b = _f(old[c].get("close")), _f(new[c].get("close"))
            if a and b and abs(a - b) / a > 1e-3:
                adj_drift.append((c, a, b, (b - a) / a))
        print("对账 %s：参照 %d 行 / 本次 %d 行 / 交集 %d / 覆盖 %.1f%% · 超差 %d"
              % (date, len(old), len(new), len(common), meta["coverage"] * 100,
                 len(diffs)))
        for c, k, a, b in diffs[:8]:
            print("  超差 %s.%s ref=%s new=%s" % (c, k, a, b))
        print("env 本侧: %s" % json.dumps(payload["env_metrics"], ensure_ascii=False))
        if os.path.isfile(ref_env):
            print("env 参照: %s" % io.open(ref_env, encoding="utf-8").read().strip())
        # 判据（面向「新日播种」等价性，非「重取历史」逐字节一致）：
        #   A 覆盖 ≥95%  B env 量纲一致（max_board 严格相等 / turnover 相对 ≤1e-4）
        #   C 除复权漂移外的 close/pct/vol 超差 = 0
        drift_codes = {c for c, _a, _b, _r in adj_drift}
        other = [d for d in diffs if d[1] in ("close", "pct") and d[0] in drift_codes]
        hard = [d for d in diffs if d not in other]
        env_self, env_ref = payload["env_metrics"], None
        if os.path.isfile(ref_env):
            try:
                env_ref = json.load(io.open(ref_env, encoding="utf-8"))
            except Exception:                                    # noqa: BLE001
                env_ref = None
        env_ok = True
        if env_ref:
            if env_ref.get("max_board") is not None and                     env_self.get("max_board") != env_ref.get("max_board"):
                env_ok = False
            a, b = env_ref.get("turnover"), env_self.get("turnover")
            if a and b and abs(a - b) / a > 1e-4:
                env_ok = False
        print("  复权漂移（预期内）：%d 码 close 相对漂移 >0.1%%（9-10 月中报分红季）"
              % len(adj_drift))
        print("  非漂移超差：%d 条（应为 0）" % len(hard))
        print("  env 量纲一致（max_board 严格 / turnover 相对 1e-4）：%s" % env_ok)
        ok = len(hard) == 0 and meta["coverage"] >= 0.95 and env_ok
        print("  播种等价判据（覆盖≥95% + 非漂移超差=0 + env 量纲一致）⇒ %s"
              % ("通过" if ok else "未通过"))
        return 0 if ok else 1
    if "--stage" in argv:
        if "--execute" not in argv:
            print("⛔ 拒绝写入：缺少 --execute", file=sys.stderr)
            return 2
        if meta["coverage"] < COVERAGE_MIN:
            print("⛔ 拒绝投放：覆盖 %.1f%% < %.0f%%（残日不得当整日）"
                  % (meta["coverage"] * 100, COVERAGE_MIN * 100), file=sys.stderr)
            return 2
        r = ING.stage_core(date, payload, source=SOURCE_LABEL, base=base,
                          replace=("--replace" in argv),
                          data_upto=date,
                          declaration=_declaration(
                              date, declared,
                              ma20_missing=("index_ma20_missing" in meta["audit"])))
        print("✅ 已投放 %s · staged=%s · warnings=%s"
              % (date, r.get("staged"), r.get("warnings")))
        return 0
    print("未知模式", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
