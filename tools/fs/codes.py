#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""证券代码类型判据 —— **全项目唯一真源**（2026-10-22 建立）

## 为什么需要这个模块

2026-10-22 排查发现：`data/state/candidates.json` 4649 条候选里混着 **181 条非个股**
（可转债 180 条 + 指数 1 条），全部来源 `news_hot`、全部停在 2026-09。

根因不是某个采集器写错，而是**判据散落多处、且采纳路径一份都没有**：

| 位置 | 判据 | 覆盖 |
|---|---|---|
| `tools/e2e/fetch_finance.py` `_is_stock_code` | sh6/sz0/sz3 白名单 | 只服务财务抓取 |
| `tools/analysis/s5.py` `_norm_code` | 前缀 sh/sz/bj + 6 位数字（**放行可转债**） |
| `tools/fs/ingest.py` `_CODE_RE` | 前缀可省 + 6 位数字（**放行可转债/指数**） |
| `tools/discovery/scan.py` `add()` | **无校验** | 新闻关联来的码直接进池 |

⇒ 同一件事四种口径，最松的那个（无校验）决定了实际行为。

**这个模块的口径**：A 股可交易个股。排除一切非个股：
- 可转债 / 可交换债：sh110/sh111/sh113/sh118/sh120/sh132、sz11x/sz12x
- 指数：sh000xxx（上证系列）、sz399xxx（深证系列）、sh880/sh999
- ETF / LOF / 基金：sh51x/sh58x、sz15x/sz16x/sz18x
- 北交所：bj（当前策略宇宙明确不含，R-2 已登记）

## 纪律

- **只有这一个地方定义"什么是个股"**。别处要判，import 这里。
- `is_stock()` 返回 False 时**不等于"数据错"**——可转债是真实存在的品种，
  只是**不属于本项目的候选池宇宙**。函数名叫 `is_tradable_stock` 更准，保留 `is_stock` 短名。
- 分类型（`classify`）用于诊断与统计，**不要拿它做过滤逻辑外的判断**。

自检：`python tools/fs/codes.py --selftest`（22 项）
"""

from __future__ import annotations

import io
import os
import sys

# ── 判据表（改这里就改了全项目口径） ─────────────────────────────────────────
# 可转债/可交换债前缀（6 位数字的部分）
_BOND_3 = ("110", "111", "113", "118", "120", "132")   # sh
_BOND_2_SZ = ("11", "12")                              # sz 前 2 位

# 指数前缀
_INDEX_SH_3 = ("000", "880", "999")                    # sh000xxx / sh880xxx / sh999xxx
_INDEX_SZ_3 = ("399",)                                 # sz399xxx

# ETF / LOF / 基金
_FUND_SH_2 = ("51", "52", "56", "58")                  # sh51x/52x/56x/58x
_FUND_SZ_2 = ("15", "16", "18")                        # sz15x/16x/18x

# 个股白名单（**白名单优先于黑名单**：宁可漏认，不可误放）
_STOCK_PREFIX = ("sh6", "sz0", "sz3")


def split_code(code):
    """'sh600000' → ('sh', '600000')；'600000' → ('', '600000')。非法 → (None, None)。"""
    if not code:
        return None, None
    s = str(code).strip().lower()
    if len(s) == 8 and s[:2] in ("sh", "sz", "bj"):
        return s[:2], s[2:]
    if len(s) == 6 and s.isdigit():
        return "", s
    return None, None


# 北交所代码段（2024 起 920xxx 全面启用；新三板/北交所历史还有 43/83/87/88 开头）
_BSE_3 = ("920",)
_BSE_2 = ("43", "83", "87", "88")

# 其他非个股品种（2026-10-22 从真实候选池里实测出来的 3 个漏网）
#   sh246xxx  上交所质押式回购 / 债券
#   sz1xxxxx  深市债券（sz148xxx 等）
#   sz8xxxxx  新三板（sz810014 实测存在）
_OTHER_SH_3 = ("241", "242", "243", "244", "245", "246", "247")
_OTHER_SZ_1 = ("14", "10", "19")     # sz14x 债券 / sz10x、19x 国债地方债
_OTHER_SZ_8 = ("80", "81", "82")     # 新三板


def _misfixed_bse(num):
    """数字段是否是北交所股 —— 用于识别**前缀错标成 sz 的北交所代码**。

    ★ 2026-10-22 实测：新闻源 `related_codes` 直接给出 `sz920436`，
      而 `westock quote sz920436` → 「未找到行情数据」，
      `westock quote bj920436` → 正常返回（北交所）。**上游把市场前缀标错了。**

      本项目口径：北交所不在策略宇宙（R-2 已登记），所以这些码**本来就该剔**；
      但**剔除理由要正确** —— 不是"看不懂"（UNKNOWN），
      而是"这是北交所、不在宇宙"（BSE_MISFIXED）。两者运维含义不同：
      前者要查解析逻辑，后者只需确认遗漏计数。
    """
    return num[:3] in _BSE_3 or num[:2] in _BSE_2


def classify(code):
    """→ 类型字符串。**永不抛错**，认不出就返回 'UNKNOWN'。

    返回值：
      STOCK        本策略宇宙内的 A 股个股
      BOND         可转债 / 可交换债
      INDEX        指数
      FUND         ETF / LOF / 基金
      BSE          北交所（**带 bj 前缀**，如 bj920033）
      BSE_MISFIXED 北交所股但**前缀被错标成 sh/sz**（如 sz920033 —— 上游实测确有）
      AMBIGUOUS    无市场前缀且数字段歧义（如 000001：sh 是上证指数、sz 是平安银行）
      BAD          形状非法（空 / 非 6 位数字 / 未知市场前缀）
      UNKNOWN      形状合法但判不出类型
    """
    mkt, num = split_code(code)
    if mkt is None:
        return "BAD"
    if mkt == "bj":
        return "BSE"
    if mkt == "sh":
        if num[:3] in _BOND_3:
            return "BOND"
        if num[:3] in _OTHER_SH_3:
            return "OTHER"          # 上交所回购/债券
        if num[:3] in _INDEX_SH_3:
            return "INDEX"
        if num[:2] in _FUND_SH_2:
            return "FUND"
        if num.startswith("6"):
            return "STOCK"
        if _misfixed_bse(num):
            return "BSE_MISFIXED"
    elif mkt == "sz":
        if num[:2] in _BOND_2_SZ:
            return "BOND"
        if num[:2] in _OTHER_SZ_1 or num[:2] in _OTHER_SZ_8:
            return "OTHER"          # 深市债券 / 新三板
        if num[:3] in _INDEX_SZ_3:
            return "INDEX"
        if num[:2] in _FUND_SZ_2:
            return "FUND"
        if num.startswith(("0", "3")):
            return "STOCK"
        if _misfixed_bse(num):
            return "BSE_MISFIXED"
    else:
        # 无市场前缀：按数字段猜（只用于诊断，不用于过滤决策的强保证）
        # ★ 歧义段（000xxx）说明：`sh000001` 是上证指数，`sz000001` 是平安银行，
        #   数字段完全相同、市场段才是判据。所以**无前缀时一律保守**：
        #   宁可漏认（自检夹具请带前缀），也不能把上证指数当个股放进候选池。
        if num[:3] in _BOND_3 or num[:2] in _BOND_2_SZ:
            return "BOND"
        if num[:3] in _BOND_3:
            return "BOND"
        if _misfixed_bse(num):
            return "BSE"
        if num[:3] in _INDEX_SH_3 + _INDEX_SZ_3:
            return "INDEX"
        if num[:2] in _FUND_SH_2 + _FUND_SZ_2:
            return "FUND"
        if num.startswith(("6", "3")):
            return "STOCK"
        if num.startswith("0"):
            return "AMBIGUOUS"
        return "UNKNOWN"
    return "UNKNOWN"


def is_stock(code):
    """是否是**本策略宇宙内的可交易个股**（A 股主板/创业板/科创板，含 sh68x）。

    排除 BSE（北交所）。

    ★ 2026-10-22 修正：**允许无市场前缀**（`600000` 与 `sh600000` 等价）。
      第一版强制要求前缀，结果 `scan --selftest` 的夹具（用 `600003` 这类无前缀码）
      全部被剔光、自检直接崩。而生产数据是带前缀的（`sh600113`）——
      说明「带前缀」是**生产现状不是判据本身**，拿它当硬条件就是拿实现细节
      冒充规范。无前缀时按数字段判（`classify` 已能正确区分转债/指数/个股）。
    """
    mkt, num = split_code(code)
    if mkt is None:
        return False
    if mkt == "bj":
        return False                       # 北交所：R-2 已登记不在宇宙内
    return classify(code) == "STOCK"


def is_bond(code):
    return classify(code) == "BOND"


def is_index(code):
    return classify(code) == "INDEX"


def is_fund(code):
    return classify(code) == "FUND"


def filter_stocks(codes):
    """保序过滤，只留个股。返回 (kept, dropped)；dropped 是 [(code, kind)]，供报告如实列出。"""
    kept, dropped = [], []
    for c in codes or ():
        if is_stock(c):
            kept.append(c)
        else:
            dropped.append((c, classify(c)))
    return kept, dropped


def summarize(codes):
    """→ {类型: 数量}，用于把"混进了什么"变成可见数字。"""
    out = {}
    for c in codes or ():
        k = classify(c)
        out[k] = out.get(k, 0) + 1
    return out


# ── 自检 ────────────────────────────────────────────────────────────────────
def _selftest():
    ok = fail = 0

    def T(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ✓ %s" % name)
        else:
            fail += 1
            print("  ✗ %s  %s" % (name, detail))

    print("codes.py 自检（证券代码类型判据）")

    # A 个股：各种板都要认
    T("A1 sh600000（主板）是个股", is_stock("sh600000"))
    T("A2 sz000001（深主板）是个股", is_stock("sz000001"))
    T("A3 sz300750（创业板）是个股", is_stock("sz300750"))
    T("A4 sh688981（科创板）是个股", is_stock("sh688981"))
    T("A5 sh601777（曾缺 name 的那只）是个股", is_stock("sh601777"))

    # B 可转债：本次事故主角
    T("B1 sh110084（转债）不是个股", not is_stock("sh110084"))
    T("B2 sh113042（转债）不是个股", not is_stock("sh113042"))
    T("B3 sh118000（转债）不是个股", not is_stock("sh118000"))
    T("B4 sz123001（转债）不是个股", not is_stock("sz123001"))
    T("B5 sz128001（转债）不是个股", not is_stock("sz128001"))
    T("B6 classify(sh110084) == BOND", classify("sh110084") == "BOND")

    # C 指数 / ETF / 北交所
    T("C1 sh000001（上证指数）不是个股", not is_stock("sh000001"))
    T("C2 sz399001（深证成指）不是个股", not is_stock("sz399001"))
    T("C3 sh510300（ETF）不是个股", not is_stock("sh510300"))
    T("C4 sz159915（ETF）不是个股", not is_stock("sz159915"))
    T("C5 bj430047（北交所）不是个股", not is_stock("bj430047"))
    T("C6 classify(bj430047) == BSE", classify("bj430047") == "BSE")

    # C7~C10 北交所新代码段 + 前缀错标（2026-10-22 真实数据里发现的）
    T("C7 bj920033（北交所 920 新段）不是个股", not is_stock("bj920033"))
    T("C8 sz920033（北交所码被错标 sz）判 BSE_MISFIXED，不判 UNKNOWN",
      classify("sz920033") == "BSE_MISFIXED", classify("sz920033"))
    T("C9 sz874571（新三板 87 段错标 sz）判 BSE_MISFIXED",
      classify("sz874571") == "BSE_MISFIXED", classify("sz874571"))
    T("C10 前缀错标的北交所码不被当个股（宇宙外）",
      not is_stock("sz920033") and not is_stock("sz874571"))
    T("C11 sh246107（上交所回购）判 OTHER", classify("sh246107") == "OTHER",
      classify("sh246107"))
    T("C12 sz148002（深市债券）判 OTHER", classify("sz148002") == "OTHER",
      classify("sz148002"))
    T("C13 sz810014（新三板）判 OTHER", classify("sz810014") == "OTHER",
      classify("sz810014"))

    # D 边界：空/畸形/无前缀
    T("D1 None 不是个股（不抛错）", is_stock(None) is False)
    T("D2 '' 不是个股", is_stock("") is False)
    T("D3 'abc' 不是个股", is_stock("abc") is False)
    T("D4 '600000'（无前缀）是个股（判据看数字段不看前缀）",
      is_stock("600000"))
    T("D5 '000001'（无前缀·歧义段）**保守不当个股**（判 INDEX，宁可漏认不可误放）",
      not is_stock("000001") and classify("000001") in ("INDEX", "AMBIGUOUS"))
    T("D6 'sz000001'（带前缀）是个股（歧义段靠市场段解开）",
      is_stock("sz000001"))
    T("D7 'sh000001'（带前缀）是上证指数不是个股",
      not is_stock("sh000001") and classify("sh000001") == "INDEX")

    # E 批量：保序 + 如实列出丢弃项
    kept, dropped = filter_stocks(["sh600000", "sh110084", "sz000001", "sh000001"])
    T("E1 filter_stocks 保序保留个股",
      kept == ["sh600000", "sz000001"], str(kept))
    T("E2 filter_stocks 如实列丢弃项（含类型）",
      dropped == [("sh110084", "BOND"), ("sh000001", "INDEX")], str(dropped))
    s = summarize(["sh600000", "sh600000", "sh110084"])
    T("E3 summarize 计数正确", s.get("STOCK") == 2 and s.get("BOND") == 1, str(s))

    # F 只读自证：本模块不得写任何文件
    tree_src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    import ast
    writes = []
    for node in ast.walk(ast.parse(tree_src)):
        if isinstance(node, ast.Call):
            fn = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            if fn in ("remove", "unlink", "replace", "rename", "rmtree",
                      "dump", "write", "writerow", "writerows"):
                writes.append(fn)
            if fn == "open" and len(node.args) > 1:
                a = node.args[1]
                if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                        and any(ch in a.value for ch in "wax+"):
                    writes.append("open(%s)" % a.value)
    T("F1 本模块零写点（纯判据函数，无副作用）", not writes, str(writes))

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--classify" in sys.argv:
        for c in sys.argv[sys.argv.index("--classify") + 1:]:
            print("%-10s %s" % (c, classify(c)))
        sys.exit(0)
    print(__doc__)
    print("用法：--selftest | --classify <code>...")
