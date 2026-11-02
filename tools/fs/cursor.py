#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cursor.py · 认知游标 C（施工方案 0.1 · v1.1 · 2026-10-08 建）

用途：游标 C 的唯一实现——「最后已结算交易日」的读 / 打勾 / 步进。
      C 是时间线地基：没有它，步进补的单就是拿未来结果编的（后视镜造假器）。

状态落点（唯一）：data/state/cursor.json
  {"version":1,"date":"YYYY-MM-DD",
   "checklist":{"行情落定":bool,"成交记录":bool,"盈亏结算":bool,"复盘归因":bool,"学习反馈":bool}}

五步顺序（设计 §11：某日 D 的五步全部做完，C 才推进到 D）：
  行情落定 → 成交记录 → 盈亏结算 → 复盘归因 → 学习反馈

★ 唯一执行器纪律（施工方案 0.1 / 0.6 写死，违反 = 游标推进权竞态）：
  · checklist 打勾的唯一执行器 = tools/risk/settle.py（0.6 五步结算）
  · date 推进的唯一执行器      = tools/risk/settle.py（经本模块 advance_cursor）
  · 本模块只提供实现、不自行触发；其他脚本/会话禁调 mark_step / advance_cursor，
    要游标状态只调 read_cursor()。
  · 与 tools/data/settle.py（旧盘后数据自动化）职责分离：那个做抓取/归档，不碰游标。

date 与 checklist 的联合读法：
  · checklist 全勾   → C = date（该日已完整结账）
  · checklist 未全勾 → date = 正在结账的交易日，C 仍是它之前那一交易日
    （折算前一交易日需交易日历，本模块不引入该依赖，由调用方处理）

advance_cursor(new) 语义 = 「旧日已结账 → 游标前移到 new，新日 checklist 清零」：
  前置 = 五步全勾；幂等 = 重复调同一 new 不二次推进、不清已打勾；禁倒退。
  原子 = date 前移 + checklist 清零 一次落盘，不出现「日期变了、勾还在」的中间态。

被谁调用：tools/risk/settle.py（0.6 · 唯一写入方）、tools/fs/wakeup.py（0.14 · 只读+触发）。

路径注入（v1.1 · F6 补口）：全部读写接口接受可选 `path=`，默认 = 唯一状态落点 CURSOR_PATH。
  · 存在的理由：settle.py 的唯一执行器测试必须在**临时目录**里验证五步原子性/幂等，
    而本模块原是全案唯一无注入口的模块（其余模块皆有 `base=` / `path=`）——无注入口
    = 只能改真实游标才能测 = 真数据污染。
  · 纪律不变：参数只改「读写哪个文件」，**不改语义**（前置/幂等/禁倒退全部照旧）；
    生产调用一律不传（保持唯一状态落点）。

用法：
  python tools/fs/cursor.py     # 只读查看游标状态（本模块无写命令入口，防绕过唯一执行器）
  from tools.fs.cursor import read_cursor, mark_step, advance_cursor, pending_steps

规则：禁在别处内联游标读写；禁硬编码路径；schema 变更必须升 version 并同步施工方案 0.1。
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools", "fs")))
from paths import ROOT  # noqa: E402
import writegate as _WG  # noqa: E402   ← 生产写入闸（原语级守卫，落盘前必过）

STEPS = ("行情落定", "成交记录", "盈亏结算", "复盘归因", "学习反馈")
SCHEMA_VERSION = 1
CURSOR_PATH = os.path.join(ROOT, "data", "state", "cursor.json")

# 测试入口自报（0.7 落点审计按此派生测试区种子 —— 本自检全在临时目录，不得算写点）
_TEST_FUNCS = ("_selftest",)


class CursorError(Exception):
    """游标操作被拒（前置未满足 / 非法值 / 文件缺失）——一律显式抛错，不静默降级。"""


def _check_date(date):
    """日期硬校验（YYYY-MM-DD），防把时间戳或中文日期写进游标。返回规范化字符串。"""
    import datetime as _dt
    s = date.isoformat() if hasattr(date, "isoformat") else str(date)
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise CursorError("游标日期必须是 YYYY-MM-DD，收到: %r" % (date,))
    try:
        _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        raise CursorError("游标日期非法: %r" % (date,))
    return s


def _validate(d):
    """schema 强校验：字段名、版本、步名集合全部写死，任何不符都抛错（不开兼容口）。"""
    if not isinstance(d, dict):
        raise CursorError("cursor.json 顶层必须是对象，实际: %r" % type(d).__name__)
    if d.get("version") != SCHEMA_VERSION:
        raise CursorError("cursor.json version 不匹配（期望 %s，实际 %r）——schema 变更须同步施工方案 0.1"
                          % (SCHEMA_VERSION, d.get("version")))
    _check_date(d.get("date"))
    cl = d.get("checklist")
    if not isinstance(cl, dict) or set(cl.keys()) != set(STEPS):
        raise CursorError("checklist 步名必须且只能是 %s，实际 %r" % (" / ".join(STEPS), cl))
    for s in STEPS:
        if not isinstance(cl[s], bool):
            raise CursorError("checklist[%s] 必须是 bool，实际 %r" % (s, cl[s]))
    return d


def _path(path=None):
    """路径注入的唯一收口（默认 = 唯一状态落点，生产调用不传）。"""
    return path or CURSOR_PATH


def _write(d, path=None):
    """原子写：先写 .tmp 再 os.replace（中途崩溃不留半截文件）。"""
    path = _path(path)
    _WG.check_write(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def read_cursor(path=None):
    """读游标（不写盘）。文件缺失/损坏一律抛错，绝不静默造一份（防游标凭空复位）。"""
    path = _path(path)
    if not os.path.exists(path):
        raise CursorError("游标文件不存在：%s —— 需先按施工方案 0.1 初始化（不得静默新建）" % path)
    with io.open(path, encoding="utf-8") as f:
        return _validate(json.load(f))


def pending_steps(cur=None):
    """尚未完成的步名列表（按 STEPS 顺序）。空列表 = 该日已完整结账。"""
    cur = cur or read_cursor()
    return [s for s in STEPS if not cur["checklist"][s]]


def is_settled(cur=None):
    """该日是否已完整结账（五步全勾）。True 时 C = cursor.date。"""
    return not pending_steps(cur)


def mark_step(step, path=None):
    """给五步中的一步打勾。幂等：已完成步重复打勾不报错、不覆盖，返回 False。

    返回 True = 本次真打上了；False = 之前已完成（幂等命中）。
    """
    if step not in STEPS:
        raise CursorError("未知步名 %r，合法步名：%s" % (step, " / ".join(STEPS)))
    cur = read_cursor(path)
    if cur["checklist"][step]:
        return False
    cur["checklist"][step] = True
    _write(cur, path)
    return True


def advance_cursor(date, path=None):
    """推进游标到 date。前置 = 五步全勾；幂等；禁倒退。

    返回 True = 已推进（date 前移 + checklist 清零，原子落盘）；False = 幂等命中未动。
    """
    new = _check_date(date)
    cur = read_cursor(path)
    old = cur["date"]
    if new == old:
        return False                      # 幂等：不二次推进、不清已打勾（防重启连跳两天）
    if new < old:
        raise CursorError("游标禁倒退：当前 %s，请求 %s" % (old, new))
    pend = pending_steps(cur)
    if pend:
        raise CursorError("五步未走完，C 停在 %s（待办：%s）——结账未完不得推进"
                          % (old, " / ".join(pend)))
    cur["date"] = new
    cur["checklist"] = {s: False for s in STEPS}
    _write(cur, path)
    return True


def _main():
    cur = read_cursor()
    pend = pending_steps(cur)
    print("游标 C · %s" % CURSOR_PATH)
    print("  version : %s" % cur["version"])
    print("  date    : %s" % cur["date"])
    print("  checklist:")
    for s in STEPS:
        print("    [%s] %s" % ("x" if cur["checklist"][s] else " ", s))
    if pend:
        print("  状态    : 结账中（date=%s 尚未走完，待办 = %s）" % (cur["date"], " / ".join(pend)))
    else:
        print("  状态    : 已结账 → C = %s" % cur["date"])
    print("  注意    : 本模块无写命令入口；打勾/推进的唯一执行器 = tools/risk/settle.py")


# ── 自检（--selftest）：全部在临时目录跑，不碰真实游标 ─────────────────────────
def _selftest():
    """0.1 游标契约自检 —— 用户 P2 令：原「15/15 依赖真实游标文件、不可复现」，

    现改为**用 `path=` 注入口写临时文件**，任何机器任何时刻结果一致。
    判据覆盖：文件缺失/损坏一律抛错不静默新建 · schema 强校验（version/步名/类型/日期）
    · mark_step 幂等 · advance 前置（五步全勾）+ 原子前移重置 + 同日不二次推进 + 禁倒退
    · 原子写无 .tmp 残留 · 口径②（真游标逐字节未变）。
    """
    import hashlib
    import shutil
    import tempfile

    ok = fail = 0
    fails = []

    def T(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [OK] %s" % name)
        else:
            fail += 1
            fails.append(name)
            print("  [!!] %s %s" % (name, extra))

    def _fp(p):
        return None if not os.path.exists(p) else hashlib.md5(io.open(p, "rb").read()).hexdigest()

    def _raises(exc, fn, *a, **kw):
        try:
            fn(*a, **kw)
        except exc:
            return True
        except Exception:
            return False
        return False

    def _mk(d, cl=None, ver=SCHEMA_VERSION):
        return {"version": ver, "date": d,
                "checklist": cl if cl is not None else {s: False for s in STEPS}}

    print("游标 C 自检 · 0.1（全在临时目录，真游标只读比指纹）")
    real_fp = _fp(CURSOR_PATH)
    sb = tempfile.mkdtemp(prefix="ta_cursor_")
    cp = os.path.join(sb, "cursor.json")
    try:
        # ① 契约常量
        T("A1 STEPS 恰为 0.1 五步（顺序即契约）",
          tuple(STEPS) == ("行情落定", "成交记录", "盈亏结算", "复盘归因", "学习反馈"))
        T("A2 SCHEMA_VERSION = 1（schema 变更须同步施工方案 0.1）", SCHEMA_VERSION == 1)

        # ② 文件缺失 / 损坏一律抛错 —— 绝不静默造一份（防游标凭空复位）
        T("B1 文件缺失 → 抛 CursorError（不静默新建）", _raises(CursorError, read_cursor, cp))
        T("B2 缺失抛错后仍未创建文件（无副作用）", not os.path.exists(cp))
        io.open(cp, "w", encoding="utf-8").write("{不是合法 json")
        T("B3 损坏 JSON → 抛错", _raises(Exception, read_cursor, cp))

        # ③ 读初值
        _write(_mk("2026-10-08"), cp)
        cur = read_cursor(cp)
        T("B4 读初值 date/version 正确",
          cur["date"] == "2026-10-08" and cur["version"] == SCHEMA_VERSION)
        T("B5 新日 checklist 全未勾（pending=5 · is_settled=False）",
          len(pending_steps(cur)) == 5 and not is_settled(cur))

        # ④ schema 强校验（不开兼容口）
        _write(_mk("2026-10-08", ver=2), cp)
        T("B6 version 不符 → 抛错", _raises(CursorError, read_cursor, cp))
        _write(_mk("2026-10-08", cl={"行情落定": False}), cp)
        T("B7 checklist 缺步 → 抛错", _raises(CursorError, read_cursor, cp))
        _write(_mk("2026-10-08", cl={s: "x" for s in STEPS}), cp)
        T("B8 checklist 值非 bool → 抛错", _raises(CursorError, read_cursor, cp))
        T("B9 时间戳式日期 → 抛错", _raises(CursorError, _check_date, "2026-10-08T10:00:00"))
        T("B10 中文日期 → 抛错", _raises(CursorError, _check_date, "2026年10月8日"))
        T("B11 语义非法日期 2026-02-30 → 抛错", _raises(CursorError, _check_date, "2026-02-30"))

        # ⑤ mark_step：幂等 + 拒非法步名
        _write(_mk("2026-10-08"), cp)
        T("C1 mark_step 合法步 → True", mark_step("行情落定", cp) is True)
        T("C2 mark_step 重复 → False（幂等、不报错）", mark_step("行情落定", cp) is False)
        T("C3 重复后该步仍为已勾（未被覆盖）", read_cursor(cp)["checklist"]["行情落定"] is True)
        T("C4 mark_step 非法步名 → 抛错", _raises(CursorError, mark_step, "不存在的步", cp))

        # ⑥ advance：前置 = 五步全勾（缺口不得被吞）
        T("C5 五步未走完 advance → 抛错", _raises(CursorError, advance_cursor, "2026-10-09", cp))
        T("C6 拒推进后游标不动（date 仍 2026-10-08）", read_cursor(cp)["date"] == "2026-10-08")
        T("C7 拒推进后已打勾未被清（缺口不被永久吞掉）",
          read_cursor(cp)["checklist"]["行情落定"] is True)

        for s in STEPS[1:]:
            mark_step(s, cp)
        T("C8 五步全勾后 is_settled = True", is_settled(read_cursor(cp)))

        # ⑦ 推进：原子前移 + checklist 重置
        T("C9 advance → True（date 前移 + checklist 清零）",
          advance_cursor("2026-10-09", cp) is True
          and read_cursor(cp)["date"] == "2026-10-09"
          and not any(read_cursor(cp)["checklist"].values()))
        T("C10 同日重复 advance → False 且不清已打勾（防重启连跳两天）",
          mark_step("行情落定", cp) is True
          and advance_cursor("2026-10-09", cp) is False
          and read_cursor(cp)["checklist"]["行情落定"] is True)
        T("C11 倒退 advance → 抛错", _raises(CursorError, advance_cursor, "2026-10-01", cp))
        T("C12 非法格式日期 advance → 抛错", _raises(CursorError, advance_cursor, "2026/10/09", cp))

        # ⑧ 原子写 + 口径②（真游标逐字节未变）
        T("D1 原子写无 .tmp 残留", not any(f.endswith(".tmp") for f in os.listdir(sb)))
        T("D2 ②沙盒泄漏 = 0：真游标未被本自检触碰", _fp(CURSOR_PATH) == real_fp)
    finally:
        shutil.rmtree(sb, ignore_errors=True)

    print("结果：%d 通过 / %d 失败" % (ok, fail))
    if fails:
        print("失败项：%s" % ", ".join(fails))
    return 1 if fail else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    _main()
