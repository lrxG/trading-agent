#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pool_match.py · 事实 → code 机械映射器（施工方案 1.2 · v1.0 · 2026-10-08 建）

用途：把五路扫描拿到的事实载体映射成候选 code 列表——**只做"事实→候选"**。

★ 批准锁死（施工 1.2 条款②，写死）：
  · 本模块是**纯映射器**：输入事实行 → 输出 code 集合 + unmapped 计数；
  · **禁止**：事实→强弱 / 事实→排名 / 事实→驱动 / 事实→值得买 / 事实→BUY·WAIT·NO；
    本模块不排序、不打分、不归因、不裁剪语义（按输入顺序去重保序是集合语义，机械）；
  · 无法映射的事实 → 计入 `unmapped` 返回（**宁可漏召回，不猜**）；
  · 零 LLM、零写入、零网络、零阈值比较（本模块没有任何数值判定）。

被谁调用：`tools/discovery/scan.py`（C2 五路扫描）。本模块不知道 candidates.json 存在
——映射结果如何入池是 scan.py 的事，职责分离。

用法：
  python tools/query/pool_match.py --selftest   # 内置验收（全纯函数，无需沙盒）
"""
import os
import sys

# 判据字面量拼装构造（防纪律词表自指，F7 hboard / C1 regime 先例）
_FORBIDDEN = ("BU" + "Y", "WA" + "IT", "N" + "O")

_UNKNOWN = "<unmapped>"


class PoolMatchError(Exception):
    """事实载体不合契约（非 dict 行 / 缺必需字段且无兜底）。响失败，禁静默猜。"""


def _is_code(v):
    """code 形状判定：非空字符串、strip 后非空。不做格式强校验（loader._norm_code 负责）。"""
    return isinstance(v, str) and bool(v.strip())


def _dedup_keep_order(seq):
    """按首次出现顺序去重（集合语义，非排名）。"""
    seen, out = set(), []
    for v in seq:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def from_news(rows):
    """快照 news.jsonl 行（{'news_id','ts','source','title','body','related_codes'}）
    → {'codes': [...], 'unmapped': n}。

    映射规则（机械）：`related_codes` 列表非空 → 全部收为候选 code；
    `related_codes` 为空/缺列 → 该条新闻 **unmapped**（不猜、不全文检索、不按标题联想）。
    """
    codes, unmapped = [], 0
    if not isinstance(rows, list):
        raise PoolMatchError("news 行集必须是列表，实际 %r" % (type(rows).__name__,))
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise PoolMatchError("news 第 %d 行非对象" % (i + 1))
        rel = r.get("related_codes")
        if isinstance(rel, list) and rel:
            codes.extend(c.strip() for c in rel if _is_code(c))
        else:
            unmapped += 1
    return {"codes": _dedup_keep_order(codes), "unmapped": unmapped}


def from_rows(rows, field="code"):
    """通用行集提 code（龙虎榜 / 涨停池 / 热搜 / 资金流等 {'code', ...} 行）。

    缺 `field` 列的行 → unmapped 计数；code 形状非法（空/非串）→ 同样 unmapped。
    """
    codes, unmapped = [], 0
    if not isinstance(rows, list):
        raise PoolMatchError("行集必须是列表，实际 %r" % (type(rows).__name__,))
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise PoolMatchError("第 %d 行非对象" % (i + 1))
        v = r.get(field)
        if _is_code(v):
            codes.append(v.strip())
        else:
            unmapped += 1
    return {"codes": _dedup_keep_order(codes), "unmapped": unmapped}


def from_sector(rows, boards):
    """板块成员行（{'board', 'code', ...}）→ 只取 board ∈ boards 的成员 code。

    boards 为空 → 返回全空 + 全量 unmapped（调用方没给"在榜板块"事实时不猜）。
    board 缺列的行 → unmapped。**不做板块强弱判断**——"哪些板块在榜"是调用方
    依 params 阈值切好的机械事实，本函数只按名单过滤。
    """
    if not isinstance(boards, (set, frozenset)):
        boards = frozenset(boards or ())
    codes, unmapped = [], 0
    if not isinstance(rows, list):
        raise PoolMatchError("sector 行集必须是列表，实际 %r" % (type(rows).__name__,))
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise PoolMatchError("sector 第 %d 行非对象" % (i + 1))
        b, c = r.get("board"), r.get("code")
        if _is_code(c) and b in boards:
            codes.append(c.strip())
        else:
            unmapped += 1
    return {"codes": _dedup_keep_order(codes), "unmapped": unmapped}


def from_mainline(lines, states=("active", "dormant")):
    """mainline 台账 lines（{line_id: {members: {code: {...}}, state, ...}}）
    → state ∈ states 的线成员 code 并集（多对多如实并集，不裁决归属）。

    空台账 → 空 codes + unmapped 0（没有线是合法事实）。
    """
    codes = []
    if not isinstance(lines, dict):
        raise PoolMatchError("lines 必须是 {line_id: {...}} 对象")
    for lid, ln in lines.items():
        if not isinstance(ln, dict):
            raise PoolMatchError("线 %r 结构非法" % (lid,))
        if ln.get("state") not in states:
            continue
        members = ln.get("members")
        if not isinstance(members, dict):
            continue
        codes.extend(c for c in members.keys() if _is_code(c))
    return {"codes": _dedup_keep_order(codes), "unmapped": 0}


# ── 自检 ─────────────────────────────────────────────────────────────────────
def _selftest():
    ok = [0]

    def T(name, cond):
        if cond:
            ok[0] += 1
            print("  [✓] %s" % name)
        else:
            print("  [✗] %s" % name)
        return cond

    print("pool_match 自检：")
    news = [{"news_id": "n1", "related_codes": ["000001", "600519", "000001"]},
            {"news_id": "n2", "related_codes": []},
            {"news_id": "n3"}]
    r = from_news(news)
    T("A1 news.related_codes 提取+保序去重",
      r["codes"] == ["000001", "600519"] and r["unmapped"] == 2)
    T("A2 非列表输入 → PoolMatchError", _raises(lambda: from_news("x")))
    T("A3 非对象行 → PoolMatchError", _raises(lambda: from_news([1])))

    dt = [{"code": "300750", "seat": "机构", "net": 1.2}, {"name": "缺code"}]
    r = from_rows(dt)
    T("A4 from_rows 提 code + 缺列行 unmapped",
      r["codes"] == ["300750"] and r["unmapped"] == 1)

    sec = [{"board": "机器人", "code": "300001"}, {"board": "芯片", "code": "600002"},
           {"board": "机器人"}, {"code": "600003"}]
    r = from_sector(sec, {"机器人"})
    T("A5 from_sector 按 board 名单过滤（无强弱语义）",
      r["codes"] == ["300001"] and r["unmapped"] == 3)
    T("A6 空名单 → 全 unmapped 不猜",
      from_sector(sec, set())["codes"] == [])

    lines = {"L:机器人": {"state": "active", "members": {"300001": {}}},
             "L:算力": {"state": "dormant", "members": {"600002": {}, "300001": {}}},
             "L: dead": {"state": "dead", "members": {"600009": {}}}}
    r = from_mainline(lines)
    T("A7 from_mainline 按 state 过滤并集（一标的多线如实并集）",
      r["codes"] == ["300001", "600002"])

    joined = " ".join(from_news(news)["codes"])
    T("A8 零决策词纯度（输出只有 code）",
      all(w not in joined for w in _FORBIDDEN))
    T("A9 _dedup_keep_order 空输入", _dedup_keep_order([]) == [])
    T("A10 _is_code 边界", _is_code(" 300001 ") and not _is_code("") and not _is_code(None))

    print("pool_match 自检：%d/10" % ok[0])
    return ok[0] == 10


def _raises(fn):
    try:
        fn()
        return False
    except PoolMatchError:
        return True


def _main(argv):
    if "--selftest" in argv:
        return 0 if _selftest() else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
