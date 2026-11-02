#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cli_codes.py · westock CLI 代码格式唯一真源（裸码 ⇄ 交易所前缀码）。

背景（2026-11-02 事故 · 日链"挂死"的真正原因）
    westock CLI 后端升级后，**裸 6 位码不再被接受**：
        westock kline 600519 --period day --start 2026-11-02 --end 2026-11-02
        ⇒ [BIZ_ERROR] 查询dayK线失败：[code=1620053001] error_type=2 msg=service error
        westock kline sh600519 ...            ⇒ 正常返回
    更阴的是：CLI **退出码 0**，错误只体现在文本里（`[BIZ_ERROR]` / `状态: error`）。
    live_seed 把"整批零回码"判为**被限流**，于是 sleep 冷却 + 多轮重取，每批重复
    2 次调用 + 冷却 —— 表面上进程活着、CPU 近 0、无网络连接，实际在无限变慢地空转，
    实测一次日链跑到 26 分钟仍未前进。用户看到的"系统卡死"根因即此。

    同时 `channel_health.check()` 只判 `returncode==0 and len(out)>20`，
    而错误文本本身长于 20 字符 ⇒ **健康检查恒定报 OK（fail-open）**，坏了两天没人发现。

所以本模块是"代码格式"的唯一真源，不许在调用点各写一份 if 判断。

用法
    from cli_codes import to_cli, to_bare, to_cli_csv, to_bare_csv
    to_cli("600519")      -> "sh600519"
    to_cli("sh600519")    -> "sh600519"      # 幂等
    to_cli("600519.SH")   -> "sh600519"
    to_bare("sh600519")   -> "600519"
    to_cli_csv(["600519", "sz000001"]) -> "sh600519,sz000001"

`--selftest` 自检（纯函数，不打网络）

★ 口径警告（2026-11-02 差点改错的地方）
  本项目**内部主键恒为带前缀码**：`data/raw/<date>/daily_quote.csv` 的 code 列就是
  `sh600113`，`prev_closes`/`names`/`boards` 的 key 同样是前缀码。
  所以 **CLI 回来的 `code` 列一律原样使用，禁止 to_bare**——削前缀会让 prev 日
  对不上、整日 universe 匹配失败。`to_bare` 只用于**展示**（聊天里给裸码）与
  少数文件名场景。
"""
import re
import sys

VENUES = ("sh", "sz", "bj")

# 裸 6 位码 → 交易所。判断依据是号码段（交易所规则），顺序即优先级。
_NUM_RULES = (
    ("92", "bj"),   # 北交所新段 920xxx（必须先于 9 判沪，否则与沪 B 股 900xxx 打架）
    ("90", "sh"),   # 沪 B 股
    ("68", "sh"),   # 科创板
    ("60", "sh"),   # 沪主板
    ("20", "sz"),   # 深 B 股
    ("30", "sz"),   # 创业板
    ("00", "sz"),   # 深主板
    ("43", "bj"),   # 北交所（原新三板精选层平移）
    ("83", "bj"),   # 北交所
    ("87", "bj"),   # 北交所
    ("88", "bj"),   # 北交所
)


def venue_of(num):
    """裸 6 位号 → 'sh'/'sz'/'bj'；认不出返回 None（调用方须显式给前缀）。

    ⚠️ 指数不要走这里：`000001` 既是深市平安银行、也是上证指数（sh000001），
    号码段本身不可判。指数一律由调用方**显式写前缀**（如 sh000300 / sh000001）。
    """
    if not num or not re.fullmatch(r"\d{6}", num):
        return None
    head2 = num[:2]
    for pre, venue in _NUM_RULES:
        if head2 == pre:
            return venue
    return None


def to_cli(code):
    """任意写法 → CLI 前缀码（sh600519）。

    接受：'600519' / 'sh600519' / 'SH600519' / '600519.SH' / '600519.sh'
    已带前缀则**原样返回**（幂等，调用点可无脑包一层）。
    认不出返回原串不做猜测——宁可让 CLI 报错，也不静默换成错市场。
    """
    if code is None:
        return ""
    s = str(code).strip()
    if not s:
        return ""
    low = s.lower()
    if len(low) == 8 and low[:2] in VENUES:              # sh600519
        return low
    if "." in s:                                          # 600519.SH
        num, _, ex = s.partition(".")
        ex = ex.strip().lower()
        num = num.strip()
        if ex in VENUES and re.fullmatch(r"\d{6}", num):
            return ex + num
        return s
    v = venue_of(s)
    return (v + s) if v else s


def to_bare(code):
    """CLI 前缀码 → 裸 6 位（sh600519 → 600519）；无前缀原样返回。"""
    if code is None:
        return ""
    s = str(code).strip().lower()
    if len(s) == 8 and s[:2] in VENUES:
        return s[2:]
    return str(code).strip()


def to_cli_csv(codes):
    """可迭代或逗号串 → 'sh600519,sz000001'（幂等，可重复套）。"""
    if codes is None:
        return ""
    if isinstance(codes, str):
        parts = [x for x in codes.split(",") if x.strip()]
    else:
        parts = [str(x) for x in codes if str(x).strip()]
    return ",".join(to_cli(p) for p in parts)


def to_bare_csv(codes):
    """'sh600519,sz000001' → '600519,000001'。"""
    if codes is None:
        return ""
    if isinstance(codes, str):
        parts = [x for x in codes.split(",") if x.strip()]
    else:
        parts = [str(x) for x in codes if str(x).strip()]
    return ",".join(to_bare(p) for p in parts)


# ── CLI 输出判真：把"退出码 0 但其实是错误"的文本识别出来 ─────────────────
_ERR_MARKS = ("[BIZ_ERROR]", "状态: error", "状态:error", "查询dayK线失败",
              "查询K线失败", "未找到行情数据", "数据为空")


def cli_error(text):
    """→ 错误摘要串；无错返回 ''。

    ★ 存在的唯一理由：westock CLI 业务错误**不改退出码**。任何"看退出码就说成功"
      的判据都是 fail-open（本次事故的第二个根因）。
    """
    if not text:
        return "EMPTY_OUTPUT"
    for m in _ERR_MARKS:
        if m in text:
            for ln in text.splitlines():
                if m in ln:
                    return ln.strip()[:160]
            return m
    return ""


def has_table_rows(text):
    """文本里是否有**数据行**（排除表头与分隔行）。空表/纯报错返回 False。"""
    if not text:
        return False
    n = 0
    for ln in text.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c or "-") for c in cells):
            continue
        n += 1
    return n >= 2          # 第 1 行是表头，≥2 才算有数据


def _selftest():
    ok = fail = 0

    def T(c, m):
        nonlocal ok, fail
        if c:
            ok += 1
        else:
            fail += 1
            print("  [FAIL]", m)

    # 号码段判定
    T(venue_of("600519") == "sh", "A1 沪主板 60 → sh")
    T(venue_of("688981") == "sh", "A2 科创板 68 → sh")
    T(venue_of("900901") == "sh", "A3 沪B 90 → sh")
    T(venue_of("000001") == "sz", "A4 深主板 00 → sz")
    T(venue_of("300750") == "sz", "A5 创业板 30 → sz")
    T(venue_of("200011") == "sz", "A6 深B 20 → sz")
    T(venue_of("430047") == "bj", "A7 北证 43 → bj")
    T(venue_of("832000") == "bj", "A8 北证 83 → bj")
    T(venue_of("920001") == "bj", "A9 北证 92 → bj（不得判成沪B 90）")
    T(venue_of("12345") is None and venue_of("abcdef") is None, "A10 非法码 → None")

    # to_cli 各种写法
    T(to_cli("600519") == "sh600519", "B1 裸码加前缀")
    T(to_cli("sh600519") == "sh600519", "B2 已带前缀幂等")
    T(to_cli("SH600519") == "sh600519", "B3 大写前缀归一")
    T(to_cli("600519.SH") == "sh600519", "B4 点后缀转前缀")
    T(to_cli("600519.sh") == "sh600519", "B5 点后缀小写")
    T(to_cli("") == "" and to_cli(None) == "", "B6 空值不抛异常")
    T(to_cli("sh000300") == "sh000300", "B7 指数前缀保持（不被号码段改写）")
    T(to_cli("999999") == "999999", "B8 认不出不猜（原样返回，让 CLI 报错）")

    # to_bare
    T(to_bare("sh600519") == "600519", "C1 去前缀")
    T(to_bare("600519") == "600519", "C2 无前缀原样")
    T(to_bare("bj430047") == "430047", "C3 北证去前缀")

    # 批量
    T(to_cli_csv(["600519", "000001", "bj430047"]) == "sh600519,sz000001,bj430047",
      "D1 列表批量加前缀")
    T(to_cli_csv("sh600519,sz000001") == "sh600519,sz000001", "D2 逗号串幂等")
    T(to_bare_csv("sh600519,sz000001") == "600519,000001", "D3 批量去前缀")
    T(to_cli_csv([]) == "" and to_cli_csv(None) == "", "D4 空集合")
    # 幂等性：反复套不叠加前缀
    T(to_cli(to_cli(to_cli("300750"))) == "sz300750", "D5 三次套壳仍单前缀")

    # cli_error 判真
    T(cli_error("数据为空") != "", "E1 '数据为空' 判为错误")
    T(cli_error("[Batch] 状态: error | 总数: 2") != "", "E2 '状态: error' 判为错误")
    T(cli_error("查询dayK线失败：[code=1620053001]") != "", "E3 BIZ_ERROR 文本判为错误")
    T(cli_error("") == "EMPTY_OUTPUT", "E4 空输出判为错误（fail-closed）")
    T(cli_error("| date | open |\n| --- | --- |\n| 2026-11-02 | 1410 |") == "",
      "E5 正常表格不误判")

    # has_table_rows
    T(has_table_rows("| a | b |\n| --- | --- |\n| 1 | 2 |") is True, "F1 有数据行")
    T(has_table_rows("| a | b |\n| --- | --- |") is False, "F2 只有表头=空表")
    T(has_table_rows("查询失败") is False, "F3 纯报错=无行")
    T(has_table_rows("") is False, "F4 空串=无行")

    print("cli_codes --selftest: %d/%d 通过" % (ok, ok + fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(_selftest())
    print(__doc__)
