# -*- coding: utf-8 -*-
"""paths.py · 项目根路径探测（tools/fs）

用途：唯一权威的「项目根定位」实现——各机械件不内联 _project_root，也不硬编码绝对路径。

被谁调用：tools/fs/ 下各模块（cursor / snapshot / loader / candidates / thesis）及
          tools/ 下其他机械件。

口径：以 marker 文件（默认 AGENT.md）为锚，从调用文件所在目录逐级向上探测。
      找不到时回退到「向上 2 级」（tools/fs → 项目根），保证不抛异常。
      ★ 注意：AGENT.md（0.11 内核）尚未落盘前走的是回退分支，结果同样正确；
        在 0.11 落盘之前**不要移动本项目目录**。
规则：脚本内禁止再内联 _project_root；禁止硬编码绝对路径。
"""
import os

__all__ = ["project_root", "ROOT"]

_MARKER = "AGENT.md"
_UPLEVELS = 6


def project_root(marker=_MARKER, up=_UPLEVELS):
    """定位项目根（含 marker 文件的最近上级目录）。

    参数：marker=锚文件（默认 AGENT.md）；up=探测失败时的回退级数
    返回：绝对路径字符串
    """
    p = os.path.dirname(os.path.abspath(__file__))
    for _ in range(up + 1):
        if os.path.exists(os.path.join(p, marker)):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    # 回退：以本文件位置向上推（tools/fs -> 根）
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


ROOT = project_root()


if __name__ == "__main__":
    print("  [OK] 项目根 =", ROOT)
    for rel in ("AGENT.md", "CODEBUDDY.md", "docs", "tools", "data", "memory"):
        print("      %-14s exists=%s" % (rel, os.path.exists(os.path.join(ROOT, rel))))
