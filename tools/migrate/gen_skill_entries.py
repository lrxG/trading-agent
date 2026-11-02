# -*- coding: utf-8 -*-
"""gen_skill_entries.py · 开插批次：为 20 个 S/E 能力生成注册表约定的技能入口（2026-10-19）。

**为什么**：注册表 stage=SKELETON 的 note 写明「开插时改 stage 并逐条 mark_impl」；
`verify_registry` 的 GHOST_CAP 判据要求 S/E 的落点目录 `.workbuddy/skills/<ID>/SKILL.md`
在场（`impl_exists`）。用户 2026-10-19 令「继续执行剩余，一次执行完」= 开插。

**内容纪律（零编造）**：每个 SKILL.md 的全部事实字段从两处派生——
  ① `data/state/capability_registry.json` 该能力条目（name/mounted_on/produces/
     consumes/boundary/knowledge_route/trigger/status）；
  ② 执行件 docstring 首行（用途一句话）。
本脚本只做装配与排版，不新造任何能力描述。

**不重复既有平台技能**：`.workbuddy/skills/` 下已有描述性命名的平台技能
（s1-event-analysis / c4-three-q / trade-rules）——那是**平台注册**的方法层件；
本批生成的是**注册表落地约定**的 id 目录入口（impl_exists 判据只认这个）。
两者并存不冲突：id 入口指向执行件+知识库，平台技能承载方法论叙事。
"""
import io
import json
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SKILLS = os.path.join(ROOT, ".workbuddy", "skills")
REG = os.path.join(ROOT, "data", "state", "capability_registry.json")

# 注册表 id → 执行件（相对 tools/）；E1 的执行件是 decision/premarket.py（其 docstring 自报 registry id=E1）
TOOL_OF = {
    "S1": "analysis/s1.py", "S2": "analysis/s2.py", "S3": "analysis/s3.py",
    "S4": "analysis/s4.py", "S5": "analysis/s5.py", "S6": "analysis/s6.py",
    "S7": "analysis/s7.py", "S8": "analysis/s8.py", "S10": "analysis/s10.py",
    "E1": "decision/premarket.py", "E2": "e2e/e2_intraday.py",
    "E3": "e2e/eod_review.py", "E4": "e2e/e4_research.py",
    "E5": "e2e/e5_position.py", "E6": "e2e/e6_weekly.py",
    "E7": "e2e/e7_research.py", "E8": "e2e/e8_quality.py",
    "E9": "e2e/e9_portfolio.py", "E10": "e2e/e10_daily.py",
    "E11": "e2e/e11_research.py",
}
KIND_LABEL = {"S": "分析能力", "E": "入口报告"}


def _tool_purpose(rel):
    """执行件 docstring 首行（用途一句话）；读不到 ⇒ None（不编）。"""
    p = os.path.join(ROOT, "tools", rel)
    if not os.path.isfile(p):
        return None
    try:
        with io.open(p, encoding="utf-8") as f:
            for ln in f:
                s = ln.strip()
                if s.startswith('"""') and len(s) > 10:
                    return s.strip('"').strip()[:120]
                if s and not s.startswith("#") and not s.startswith("-*"):
                    break
    except Exception:                                         # noqa: BLE001
        return None
    return None


def render(cid, cap):
    tool = TOOL_OF.get(cid)
    purpose = _tool_purpose(tool) if tool else None
    b = cap.get("boundary") or {}
    kr = cap.get("knowledge_route")
    trg = cap.get("trigger") or {}
    L = [
        "# %s %s · 技能入口（%s）" % (cid, cap.get("name"), KIND_LABEL.get(cap.get("kind"), cap.get("kind"))),
        "",
        "> 版本 1.0.0 · 2026-10-19 开插批次生成 · 注册表 id=%s · 母环节挂载=%s" % (cid, cap.get("mounted_on")),
        "> 本文件 = 注册表落点约定的**技能入口**（`impl_exists` 判据：`.workbuddy/skills/<ID>/SKILL.md`）。",
        "> 全部事实字段派生自注册表条目与执行件 docstring，零编造；方法论叙事见平台技能区"
        "（s1-event-analysis / c4-three-q / trade-rules 等描述性技能）。",
        "",
        "## 是什么",
        "",
        purpose or "（执行件 docstring 首行未取到——以注册表 name 为准：%s）" % cap.get("name"),
        "",
        "## 执行件",
        "",
        "- `tools/%s`" % tool if tool else "- （注册表未关联执行件）",
        "- 自检：`python tools/%s --selftest`" % tool if tool else "",
        "",
        "## 产出与消费者",
        "",
        "- 产出：%s" % (" / ".join(cap.get("produces") or []) or "（注册表未列）"),
        "- 输出落点：%s" % (cap.get("output_path") or "（注册表未列）"),
        "",
        "## 知识真源",
        "",
        ("- `%s`（注册表 knowledge_route；经 `loader.load_knowledge` 读取）" % kr) if kr
        else "- （注册表 knowledge_route=null——该能力无结构化知识库；判据在执行件/params）",
        "",
        "## 边界（注册表锁死）",
        "",
        "- no_decision=%s · can_order=%s" % (b.get("no_decision"), b.get("can_order")),
        ("- %s" % b["note"]) if b.get("note") else "",
        "- S/E 系总规则：只供专业判断素材，不越权决策、不下单（决策 = C4 三问，执行 = C5 硬闸）。",
        "",
        "## 触发",
        "",
        "- %s" % (trg.get("text") or "（注册表未列）"),
        ("- 入口：%s" % " / ".join(trg.get("entry") or [])) if trg.get("entry") else "",
        "",
        "## 上游（consumes）",
        "",
        " / ".join(cap.get("consumes") or []) or "（注册表未列）",
        "",
    ]
    return "\n".join(x for x in L if x is not None)


def main():
    doc = json.load(io.open(REG, encoding="utf-8"))
    caps = {c["id"]: c for c in doc["capabilities"]}
    made, skipped = [], []
    for cid, tool in sorted(TOOL_OF.items()):
        cap = caps.get(cid)
        if cap is None:
            skipped.append((cid, "注册表无此 id"))
            continue
        if cap.get("status") != "active":
            skipped.append((cid, "status=%s（非 active，不建入口）" % cap.get("status")))
            continue
        d = os.path.join(SKILLS, cid)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "SKILL.md")
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(render(cid, cap))
        made.append(cid)
    print("生成 %d 个技能入口：%s" % (len(made), " ".join(made)))
    for cid, why in skipped:
        print("跳过 %s：%s" % (cid, why))
    return 0


if __name__ == "__main__":
    sys.exit(main())
