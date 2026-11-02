# -*- coding: utf-8 -*-
"""主线雷达报告（T3 · 2026-10-16）。

职责：把新系统**已有**的题材/主线能力装配为**用户可直接阅读**的主线雷达报告
（`data/generated/reports/mainline_<as_of>.md`）。

## 边界（硬）

- **零 BUY/WAIT/NO、零仓位、零交易动作**：判断归 C4（`c4-three-q`），仓位归 C5。
  本件**不回答"能不能买"**——那是 C4 三问 + C5 硬闸的事。
- **零新判据**：阶段 / 下一触发 / 结构偏向 / 代表标的**一律抄 S2 输出**；
  族态分组与排序是**呈现口径**（写在报告里、可复算），不改任何判据。
- **零新写点**：复用已登记写点 `premarket._atomic_write`（内部过 `writegate`）。
- **只读**：不写状态、不碰决策/pnl 账本、不碰持仓、不改 `brain_state.md`
  （「板块腿」属 C1/C2 写入段，AGENT 维护规矩①：LLM 不得代笔）。

## 输入（缺 → 如实标注；不编造、不回退旧日）

| 输入 | 真链路 | 缺时语义 |
|---|---|---|
| S2 题材快照 | `data/generated/s_s2_<as_of>.json`（`s2.read_themes`） | 整份报告降级为「S2 快照缺席」 |
| C2 主线状态机 | `data/state/mainline.json` | 该段 ABSENT |
| C1 环境档 | `memory/state/brain_state.md`（`brief.read_env`） | `UNAVAILABLE`（禁编档） |
| S3 情绪结构 | `data/generated/s_s3_<as_of>.json`（`loader.load_s_output`） | ABSENT |

用法：
    python tools/e2e/mainline_radar.py --eval 2026-09-29 [--base <沙盒>] [--stdout]
    python tools/e2e/mainline_radar.py --selftest
"""
import argparse
import io
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_FS = os.path.normpath(os.path.join(_HERE, "..", "fs"))
_AN = os.path.normpath(os.path.join(_HERE, "..", "analysis"))
_DM = os.path.normpath(os.path.join(_HERE, "..", "decision"))
for _p in (_FS, _AN, _DM):
    sys.path.insert(0, _p)

from paths import ROOT  # noqa: E402

import s2 as S2                        # noqa: E402  S2 真链路（阶段/触发/结构偏向唯一来源）
import loader as LD                    # noqa: E402  S 快照读取（精确 as_of）
import brief as BRF                    # noqa: E402  环境档读取（C1 产物）
import premarket as E1                 # noqa: E402  复用已登记原子写（零新增写点）

S_ID = "mainline_radar"
SCHEMA_VERSION = 1

# ── 呈现口径（本报告自己的分组/排序规则；**不是判据**，改它不影响 S2/C2）──────

# 八族名（缺 params 时退化为族码；名字真源 = params.b7_families.families）
_FAMILY_FALLBACK = {"A": "产业0→1", "B": "涨价供需", "C": "政策救助", "D": "海外映射",
                    "E": "重组壳", "F": "流动性重估", "G": "情绪代号图腾", "H": "事件单日"}

# 族态四态映射（**本报告口径**，机械派生自 S2 阶段；旧系统三态定义不适用，见报告声明）
_FAM_LIVE = ("加速", "扩散", "发酵")     # 点亮：主升/扩散段在走
_FAM_WINDOW = ("发现",)                   # 有窗：首现观察，窗口未确认
_FAM_FADE = ("分歧", "尾声")              # 退坡：接力赔率下降 / 涨停簇衰竭

# 明细表呈现序（最活跃在前；纯展示，不改 S2 判优顺序）
_DISPLAY_ORDER = ("加速", "扩散", "发酵", "发现", "分歧", "尾声", "UNRESOLVED")

# 证据分级（任务 §1.5：事实 / 能力输出 / 推断 / 待验证 必须可区分）
_GRADE_LEGEND = [
    ("F", "事实", "数据面直接读出或机械计算（收盘读数、家数、宽度、资金）"),
    ("C", "能力输出", "S2 / C2 / C1 的判定产物（阶段、触发器、环境档、状态机）"),
    ("I", "推断", "本报告把上述组合后的**阅读性解读**；可错，不构成结论"),
    ("V", "待验证", "未达统计门槛或口径未闭合，**不得作为决策依据**"),
]


class RadarError(Exception):
    """入参/契约不符（显式抛错，不静默降级）。"""


def _chk_date(v):
    if not isinstance(v, str) or len(v) != 10 or v[4] != "-" or v[7] != "-":
        raise RadarError("as_of 须为 YYYY-MM-DD，实际: %r" % (v,))
    return v


def _base(b):
    return os.path.normpath(b) if b else ROOT


def _load_json(p):
    if not os.path.isfile(p):
        return None
    try:
        with io.open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except (ValueError, IOError):
        return None


def _fam_names(b):
    """八族名 → params（唯一真源）；缺 → 退化族码并记缺口。"""
    pp = os.path.join(b, "data", "params.json")
    try:
        fam = S2.load_families(pp if os.path.isfile(pp) else None)
        names = {}
        for code, v in (fam.get("families") or {}).items():
            names[code] = (v or {}).get("name") or _FAMILY_FALLBACK.get(code, code)
        return names, (fam.get("note_scope") or ""), None
    except Exception as e:                                    # noqa: BLE001
        return dict(_FAMILY_FALLBACK), "", "params.b7_families 读取失败：%s" % e


def _fam_state(stages):
    """族内阶段集合 → 族态（本报告口径 · **存在语义**：族里只要还有一段在推进就算点亮）。

    这一条是刻意选的：若按 S2 的终局判优顺序取"最强"阶段，一个含 22 个题材、
    大部分在扩散/发酵、仅个别走到尾声的族会被误判成"退坡"——那是**题材级**
    的信息，不能覆盖**族级**状态。族级只回答"这个族今天还在不在台上"。
    """
    s = set(x for x in stages if x)
    if not s:
        return "静默"
    if s & set(_FAM_LIVE):
        return "点亮"
    if s & set(_FAM_WINDOW):
        return "有窗"
    if s & set(_FAM_FADE):
        return "退坡"
    return "静默"


def _rank_stage(st):
    try:
        return _DISPLAY_ORDER.index(st)
    except ValueError:
        return len(_DISPLAY_ORDER)


# ── 采集（全只读）────────────────────────────────────────────────────────────
def collect(as_of, base=None):
    as_of = _chk_date(as_of)
    b = _base(base)

    rec = S2.read_themes(as_of, base=base)
    s2_ok = rec is not LD.UNAVAILABLE
    snap = rec if isinstance(rec, dict) else {}

    fam_names, fam_note, fam_gap = _fam_names(b)
    themes = dict(snap.get("themes") or {})

    # 族视角分组（呈现）
    by_fam = {}
    for tid, t in themes.items():
        by_fam.setdefault(t.get("family") or "UNKNOWN", []).append((tid, t))
    families = []
    for code in sorted(by_fam):
        rows = by_fam[code]
        stages = [r[1].get("stage") for r in rows]
        rows.sort(key=lambda r: (_rank_stage(r[1].get("stage")),
                                 -(r[1].get("evidence", {}).get("metrics", {})
                                    .get("zt_count") or 0), r[0]))
        top = rows[0][1]
        cnt = {}
        for s in stages:
            cnt[s] = cnt.get(s, 0) + 1
        families.append({
            "family": code, "family_name": fam_names.get(code, code),
            "state": _fam_state(stages), "n_themes": len(rows),
            "stages": sorted(set(stages), key=_rank_stage),
            "stage_counts": sorted(cnt.items(), key=lambda kv: _rank_stage(kv[0])),
            "lead_theme": top.get("theme_name"), "lead_stage": top.get("stage"),
            "rows": rows,
        })
    fam_order = {"点亮": 0, "有窗": 1, "退坡": 2, "静默": 3}
    families.sort(key=lambda f: (fam_order[f["state"]], f["family"]))

    # 主线 token 状态机（C2）
    ml = _load_json(os.path.join(b, "data", "state", "mainline.json"))
    mainline = None
    if isinstance(ml, dict) and isinstance(ml.get("lines"), dict):
        act = [(k, v) for k, v in ml["lines"].items() if v.get("state") == "active"]
        dor = [(k, v) for k, v in ml["lines"].items() if v.get("state") == "dormant"]
        act.sort(key=lambda kv: (-(kv[1].get("day_n") or 0), kv[0]))
        mainline = {"updated_at": ml.get("updated_at"),
                    "processed_upto": ml.get("processed_upto"),
                    "annotations": ml.get("data_annotations") or {},
                    "n_active": len(act), "n_dormant": len(dor),
                    "active": act, "dormant": dor}

    env = BRF.read_env(os.path.join(b, "memory", "state", "brain_state.md"))

    s3rec = LD.load_s_output("s3", as_of, base=base)
    s3 = None
    if s3rec is not LD.UNAVAILABLE:
        s3 = {"stage": s3rec.get("stage"), "temp": s3rec.get("temp"),
              "leader": (s3rec.get("leader") or {}).get("name")}

    gaps = []
    if not s2_ok:
        gaps.append("S2 题材快照缺席（`s_s2_%s.json` 不存在）——"
                    "本报告无题材段可呈现，不回退旧日、不用夹具顶替" % as_of)
    for g in ("n_unresolved", "n_family_unknown", "n_radar_matched"):
        v = snap.get(g)
        if v:
            gaps.append("S2 自报 %s = %s" % (g, v))
    if fam_gap:
        gaps.append(fam_gap)
    if s3 is None:
        gaps.append("S3 情绪结构当日快照缺席（`s_s3_%s.json` 不存在）——不编造" % as_of)
    if mainline is None:
        gaps.append("C2 主线状态机 `data/state/mainline.json` 缺席或结构不符")
    uni = snap.get("universe") or {}
    if uni.get("caliber"):
        gaps.append("题材行情口径：%s" % uni["caliber"])
    if snap.get("n_radar_matched") is not None and snap.get("n_themes"):
        gaps.append("当日截面交叉证据（旧包 concept_radar）命中 %s/%s 个题材——"
                    "**未命中者缺资金/宽度外证**" % (snap.get("n_radar_matched"),
                                                    snap.get("n_themes")))
    stale = sorted({t.get("day_n_anchor") for _, t in themes.items()
                    if t.get("day_n_anchor") and t.get("day_n_anchor") != as_of})
    if stale:
        gaps.append("有题材的 `day_n_anchor` 早于 as_of（%s）—— 阶段判定的有效锚点"
                    "非当日，读该行须注意" % "、".join(stale))
    # ── 环境锚时点核验（任务 T3 收尾③）────────────────────────────────────
    # 三态：PASS（同日）/ FAIL_ABSENT（环境档无有效 as_of）/ FAIL_MISMATCH（不同日）
    if env.get("source") == "UNAVAILABLE" or env.get("regime") in (None, "UNAVAILABLE"):
        tp = {"status": "FAIL_ABSENT", "env_as_of": env.get("as_of"),
              "reason": "C1 环境档 UNAVAILABLE（brain_state.md 环境档段缺或未解析）"
                        "——无时点可比"}
    elif not env.get("as_of"):
        tp = {"status": "FAIL_ABSENT", "env_as_of": None,
              "reason": "环境档 `as_of / source` 行缺失或其左半不是 `YYYY-MM-DD`"
                        "——`brief.read_env` 已如实返回 as_of=None（缺就是缺，不猜）"}
    elif env["as_of"] == as_of:
        tp = {"status": "PASS", "env_as_of": env["as_of"],
              "reason": "环境档 as_of 与报告 as_of 同日"}
    else:
        tp = {"status": "FAIL_MISMATCH", "env_as_of": env["as_of"],
              "reason": "环境档 as_of = %s ≠ 报告 as_of = %s —— 环境锚不是当日值"
                        % (env["as_of"], as_of)}
    if tp["status"] != "PASS":
        gaps.append("**环境锚时点核验未通过（%s）**：%s"
                    % (tp["status"], tp["reason"]))

    return {
        "schema": SCHEMA_VERSION, "cap": S_ID, "as_of": as_of,
        "s2_present": s2_ok, "s2_universe": uni, "s2_stats": {
            "n_themes": snap.get("n_themes"), "n_unresolved": snap.get("n_unresolved"),
            "n_family_unknown": snap.get("n_family_unknown"),
            "n_radar_matched": snap.get("n_radar_matched")},
        "s2_confidence": snap.get("confidence"), "fam_note": fam_note,
        "themes": themes, "families": families,
        "mainline": mainline, "env": env, "s3": s3, "gaps": gaps,
        "timepoint": tp,
        "sort_note": "族序 = 族态(点亮/有窗/退坡/静默) → 族码；族内序 = 阶段呈现序 → "
                     "涨停家数降序 → 题材码。**纯呈现规则**，不改 S2 判优顺序",
    }


# ── 渲染 ─────────────────────────────────────────────────────────────────────
def _metrics_line(t):
    m = (t.get("evidence") or {}).get("metrics") or {}
    if not m:
        return "UNAVAILABLE"
    return ("涨停 %s ｜ 等权涨幅 %s%% ｜ 宽度 %s ｜ 最高连板 %s ｜ 首板占比 %s"
            % (m.get("zt_count"), _f3(m.get("pct")), _f3(m.get("breadth")),
               m.get("max_board"), _f3(m.get("first_share"))))


def _f3(v):
    return "UNAVAILABLE" if v is None else (
        ("%.3f" % v).rstrip("0").rstrip(".") if isinstance(v, float) else str(v))


# 环境信号展示格式（**只格式化，不改值**；真源 = brief.read_env）
_SIG_FMT = {"limit_up_count": ("%d", None), "max_board": ("%d", None),
            "up_down_ratio": ("%.2f", None), "index_ma20": ("%.2f", None),
            "fail_rate": ("%.1f%%", 100.0), "turnover": ("%.2f 万亿", 1e-12)}


def _sig_line(sig):
    out = []
    for k in sorted(sig):
        fmt, mul = _SIG_FMT.get(k, ("%s", None))
        v = sig[k]
        try:
            out.append("%s=%s" % (k, fmt % ((v * mul) if mul else v)))
        except (TypeError, ValueError):
            out.append("%s=%s" % (k, v))
    return " ｜ ".join(out)


def _leader_line(t):
    ld = (t.get("evidence") or {}).get("leader") or {}
    out = []
    if ld.get("name") or ld.get("code"):
        out.append("%s %s(%s板)" % (ld.get("code") or "-", ld.get("name") or "-",
                                    ld.get("boards")))
    ref = (t.get("evidence") or {}).get("radar_ref") or {}
    if ref.get("leader"):
        out.append("外证龙头 %s" % ref["leader"])
    return " ｜ ".join(out) if out else "UNAVAILABLE"


def _fail_line(t):
    """失效条件（机械口径）：当前阶段 required 判据 + 降级方向。"""
    ev = t.get("evidence") or {}
    g = (ev.get("gates") or {}).get(t.get("stage")) or {}
    req = g.get("required") or []
    if not req:
        return "UNAVAILABLE（S2 未给该阶段判据）"
    txt = "；".join(("%s%s" % ("" if ok else "✗", d)) for ok, d in req)
    return "全过才维持 → %s" % txt


def render_md(d):
    L = []
    axis = "SIM" if d.get("_sim") else "LIVE"
    L.append("# 主线雷达 · %s（%s 轴）" % (d["as_of"], axis))
    L.append("")
    L.append("> **读者 = 用户**。本表 = 新系统题材/主线能力的**呈现投影**，"
             "数据全部只读，时点锚 = `as_of` 精确命中（**不回退旧日、不用夹具顶替**）。")
    L.append("> **边界**：本报告**不含 BUY / WAIT / NO，不含仓位，不含交易动作**。"
             "「能不能买」属 C4 三问 + C5 硬闸，不在本件。")
    L.append("> **判读纪律**：**阶段 ≠ 可买**；**族态只是「族内活跃状态」的呈现映射**，"
             "既**不代表族级强度排名**，也**不构成交易判断**。S2 有输出只说明题材结构在哪一段，"
             "不等于该题材构成可买主线——本件**不做可买性排序、不给优先级**。")
    L.append("> **时点纪律**：环境锚的 as_of 与报告 as_of **同日才算核验通过**；"
             "缺失或不一致会在 L0 **显式标为未通过并给出原因**（见 L0「环境锚时点核验」）。")
    L.append("> **证据分级**：" + " ｜ ".join(
        "`%s` %s（%s）" % (c, n, s) for c, n, s in _GRADE_LEGEND))
    L.append("")

    # ── L0 环境锚 ──────────────────────────────────────────────────────────
    L.append("## L0 环境锚")
    L.append("")
    env = d.get("env") or {}
    if env.get("regime") not in (None, "UNAVAILABLE") and env.get("source") != "UNAVAILABLE":
        cap = env.get("position_cap")
        L.append("- **环境档位**：%s（仓位天花板 %s 成）`[C]`"
                 % (env.get("regime"),
                    "UNAVAILABLE" if cap is None
                    else ("%d" % cap if float(cap).is_integer() else "%g" % cap)))
        if env.get("as_of"):
            _aof = env["as_of"]
        else:
            _aof = "UNAVAILABLE（环境档无有效 as_of，见下核验）"
        L.append("- 环境档 source：%s（真源 `regime.py` → `brain_state.md`）｜"
                 "环境档 as_of：%s" % (env.get("source"), _aof))
        sig = env.get("signals") or {}
        if sig:
            L.append("- 环境信号 `[F]`：%s" % _sig_line(sig))
    else:
        L.append("- **环境档位**：`UNAVAILABLE`（brain_state.md 环境档段缺席或未解析"
                 "——**禁编档**）`[F]`")

    # ── 环境锚时点核验（任务 T3 收尾③ · 三态显式）──────────────────────────
    tp = d.get("timepoint") or {}
    mark = {"PASS": "✅ **通过**", "FAIL_ABSENT": "❌ **未通过（时点缺失）**",
            "FAIL_MISMATCH": "❌ **未通过（时点不一致）**"}.get(
                tp.get("status"), "⚠️ **未核验**")
    L.append("- **环境锚时点核验**：%s —— 环境档 as_of = `%s` ／ 报告 as_of = `%s`"
             % (mark, tp.get("env_as_of") or "UNAVAILABLE", d["as_of"]))
    L.append("  - 判据：**同日才算通过**。原因：%s" % (tp.get("reason") or "未核验"))
    if tp.get("status") != "PASS":
        L.append("  - ⚠️ 本报告的**环境锚不是当日值**：L0 的环境档位/信号"
                 "**不得当作 `as_of` 当日的市场状态**解读。`[V]`")

    if d.get("s3"):
        L.append("- **S3 情绪结构** `[C]`：stage=%s ｜ temp=%s ｜ 领先=%s"
                 % (d["s3"].get("stage"), d["s3"].get("temp"),
                    d["s3"].get("leader") or "无"))
    else:
        L.append("- **S3 情绪结构**：当日快照缺席（不编造、不回退旧日）`[F]`")
    st = d.get("s2_stats") or {}
    if d.get("s2_present"):
        L.append("- **S2 题材面** `[C]`：题材 %s 个（未解出 %s ｜ 族未知 %s）｜"
                 "截面交叉外证命中 %s 个"
                 % (st.get("n_themes"), st.get("n_unresolved"),
                    st.get("n_family_unknown"), st.get("n_radar_matched")))
    else:
        L.append("- **S2 题材面**：快照缺席 —— **本报告无题材段可呈现**")
    L.append("- 排序规则：%s" % d.get("sort_note"))
    L.append("")

    if not d.get("s2_present"):
        L.append("## 一、题材段（缺席）")
        L.append("")
        L.append("- S2 快照 `s_s2_%s.json` 不存在 → 题材/主线段 **ABSENT**。"
                 "本件**不以夹具或旧日快照顶替**（§4 三锁之信息截断）。" % d["as_of"])
        L.append("")
    else:
        # ── 一、族视角总览 ────────────────────────────────────────────────
        L.append("## 一、族视角总览（八族 · 四态 = 族内活跃状态的呈现映射）")
        L.append("")
        L.append("> **本节四态只是「族内活跃状态」的呈现映射**（机械派生自 S2 阶段输出，"
                 "纯呈现 · **存在语义**）：**点亮** = 族内**存在**阶段 ∈ {加速, 扩散, 发酵}；"
                 "**有窗** = 族内存在 ∈ {发现}（且无点亮项）；"
                 "**退坡** = 族内**全部** ∈ {分歧, 尾声}；**静默** = 当日无题材快照记录。")
        L.append("> **两个「不代表」**：①**不代表族级强度排名**——同为「点亮」的族之间"
                 "**没有强弱之分**，族序仅为阅读顺序（族态 → 族码），不是打分、不是排序结论；"
                 "②**不构成交易判断**——族态与阶段都**不回答「能不能买」**。")
        L.append("> **族态是族级信号，不覆盖题材级差异**：族里可能同时有正在扩散的题材"
                 "和已经走到尾声的题材——**前者让族点亮，后者的退坡风险看「族内阶段」列**"
                 "（下方**保留族内阶段计数**，如 `扩散×9 · 发酵×3 · 发现×4 · 尾声×6`）。")
        L.append("> **不改 S2 判据**：四态映射与行序都是本**报告呈现层**的规则，"
                 "`params.s2_rules.stage` 与 S2 的阶段判定**零改动**。")
        L.append("> **「静默」≠ 删除**：休眠族随时可被事件/政策/海外映射重新点亮。"
                 "旧系统的三态定义依赖旧机制，**未沿用**。")
        L.append("")
        L.append("| 族 | 状态 | 题材数 | 族内阶段（计数） | 族内首行题材 | 首行读数 | 代表标的 |")
        L.append("|---|---|---|---|---|---|---|")
        for f in d["families"]:
            top = f["rows"][0][1]
            L.append("| %s %s | **%s** | %d | %s | %s（%s） | %s | %s |"
                     % (f["family"], f["family_name"], f["state"], f["n_themes"],
                        " · ".join("%s×%d" % (k, v) for k, v in f["stage_counts"]),
                        f["lead_theme"], f["lead_stage"],
                        _metrics_line(top), _leader_line(top)))
        L.append("")

        # ── 二、题材明细（按族分组）────────────────────────────────────────
        L.append("## 二、题材明细（按族分组 · 逐题材）")
        L.append("")
        for f in d["families"]:
            L.append("### %s族 %s（%s · %d 个题材）" % (f["family"], f["family_name"],
                                                   f["state"], f["n_themes"]))
            L.append("")
            L.append("| 题材 | 阶段 | 关键证据 `[F]` | 代表标的 `[F]` | 下一触发条件 `[C]` | 结构背景 `[C]` |")
            L.append("|---|---|---|---|---|---|")
            for _tid, t in f["rows"]:
                nst = t.get("next_stage_trigger") or {}
                sb = t.get("structure_bias") or {}
                L.append("| %s | %s | %s | %s | %s | %s |"
                         % (t.get("theme_name"), t.get("stage"), _metrics_line(t),
                            _leader_line(t), nst.get("text") or "UNAVAILABLE",
                            sb.get("note") or "UNAVAILABLE"))
            L.append("")

        # ── 三、主线 token 状态机（C2）────────────────────────────────────
        L.append("## 三、主线 token 状态机（C2 产物）")
        L.append("")
        ml = d.get("mainline")
        if not ml:
            L.append("- `mainline.json` 缺席或结构不符 → **本段 ABSENT**（不编造）")
        else:
            L.append("- 台账 `updated_at` = %s ｜ `processed_upto` = %s ｜ "
                     "active %d ／ dormant %d"
                     % (ml.get("updated_at"), ml.get("processed_upto"),
                        ml["n_active"], ml["n_dormant"]))
            L.append("")
            L.append("| 主线 token | 状态 | 首见 | 末次命中 | 连续日 | 断档 | 成员数 |")
            L.append("|---|---|---|---|---|---|---|")
            for k, v in ml["active"]:
                L.append("| %s | **active** | %s | %s | %s | %s | %d |"
                         % (k, v.get("first_seen"), v.get("last_hit"), v.get("day_n"),
                            v.get("miss_streak"), len(v.get("members") or {})))
            if not ml["active"]:
                L.append("| — | （当日无 active 主线 token） | — | — | — | — | — |")
            L.append("")
            L.append("- dormant 主线 %d 个：%s" % (
                ml["n_dormant"],
                "、".join(k for k, _ in ml["dormant"][:20]) +
                ("…" if ml["n_dormant"] > 20 else "") if ml["n_dormant"] else "无"))
            if ml.get("annotations"):
                L.append("- ⚠️ 台账数据注记：" + "；".join(
                    "%s = %s" % (k, (v or {}).get("kind")) for k, v
                    in sorted(ml["annotations"].items())))
        L.append("")

        # ── 四、失效条件 ─────────────────────────────────────────────────
        L.append("## 四、下一触发与失效条件（机械口径）")
        L.append("")
        L.append("> **下一触发** = S2 `next_stage_trigger`（原样抄，含数值条件）`[C]`。")
        L.append("> **失效条件** = 该题材**当前阶段**的 required 判据（S2 `evidence.gates`）"
                 "`[C]`：**全过才维持本阶段**；任一不过 → 阶段不再成立。")
        L.append("> ⚠️ **缺口**：**S2 未输出显式「失效价 / 失效线」，也未输出显式降级触发器**"
                 "—— 降级方向只能按阶段序列（`S2._STAGE_ORDER` 判优顺序）机械回退。"
                 "**故本段只回答「该阶段还成不成立」，不回答「该题材会跌到哪」**。`[V]`")
        L.append("")
        L.append("| 题材 | 阶段 | 下一触发 `[C]` | 失效条件（本阶段判据不再全过）`[C]` |")
        L.append("|---|---|---|---|")
        for f in d["families"]:
            for _tid, t in f["rows"]:
                nst = t.get("next_stage_trigger") or {}
                L.append("| %s | %s | %s | %s |"
                         % (t.get("theme_name"), t.get("stage"),
                            nst.get("text") or "UNAVAILABLE", _fail_line(t)))
        L.append("")

        # ── 五、观察对象 ─────────────────────────────────────────────────
        L.append("## 五、观察对象（**非动作指令**）")
        L.append("")
        L.append("> 汇总口径：S2 `evidence.leader`（题材内最高连板=leader）与 "
                 "`radar_ref.leader`（旧包 concept_radar 外证龙头）**原样列出**。")
        L.append("> **本段不是推荐名单、不是买入候选**——不含任何买卖语义；"
                 "谁值得下注由 C4 三问 + C5 硬闸判。`[I]`")
        L.append("")
        L.append("| 题材 | 阶段 | 题材内 leader `[F]` | 外证龙头 `[F]` |")
        L.append("|---|---|---|---|")
        for f in d["families"]:
            for _tid, t in f["rows"]:
                ld = (t.get("evidence") or {}).get("leader") or {}
                ref = (t.get("evidence") or {}).get("radar_ref") or {}
                L.append("| %s | %s | %s | %s |"
                         % (t.get("theme_name"), t.get("stage"),
                            ("%s %s(%s板)" % (ld.get("code"), ld.get("name"),
                                              ld.get("boards")))
                            if ld.get("name") else "UNAVAILABLE",
                            ref.get("leader") or "UNAVAILABLE（无外证）"))
        L.append("")

    # ── 六、数据缺口与口径 ───────────────────────────────────────────────
    L.append("## 六、数据缺口与口径声明")
    L.append("")
    if d.get("gaps"):
        for g in d["gaps"]:
            L.append("- %s" % g)
    else:
        L.append("- 无自报缺口")
    L.append("")
    if d.get("fam_note"):
        L.append("- 八族字典 note：%s" % d["fam_note"])
    L.append("- `s_s2_confidence` = %s（快照自报；**非统计置信度**，"
             "无外证命中时按 S2 规则降档）" % d.get("s2_confidence"))
    L.append("")

    # ── 七、本报告不做什么 ───────────────────────────────────────────────
    L.append("## 七、本报告不做什么（边界复述）")
    L.append("")
    L.append("- **不输出 BUY / WAIT / NO**，不给仓位请求，不生成订单（→ C4 / C5）。")
    L.append("- **不新增判据**：阶段、触发器、结构偏向、代表标的全部来自 S2 输出原样呈现。")
    L.append("- **不写状态**：不更新 `brain_state.md` 板块腿（属 C1/C2 写入段，"
             "AGENT 维护规矩①：LLM 不得代笔），不改候选池、不改参数。")
    L.append("- **不承诺未来**：阶段是**当日截面**判定，不是「未来会怎样」的预测。")
    return "\n".join(L)


def emit(as_of, *, sim=False, base=None):
    d = collect(as_of, base=base)
    d["_sim"] = bool(sim)
    md = render_md(d)
    p = os.path.join(_base(base), "data", "generated", "reports",
                     "mainline_%s.md" % as_of)
    if os.path.isfile(p) and io.open(p, encoding="utf-8").read() == md:
        return {"created": False, "reason": "EXISTS", "path": p, "review": d}
    E1._atomic_write(p, md)          # 已登记写点；内部过 writegate（无票即拒）
    return {"created": True, "path": p, "review": d}


# ── 自检（全合成夹具，真库零接触）─────────────────────────────────────────────
def _selftest():
    import shutil

    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s" % ("✓" if cond else "✗", name))

    print("== mainline_radar --selftest ==")
    sb = tempfile.mkdtemp(prefix="mlr_sb_")
    try:
        gen = os.path.join(sb, "data", "generated")
        st = os.path.join(sb, "data", "state")
        mem = os.path.join(sb, "memory", "state")
        for x in (gen, st, mem):
            os.makedirs(x, exist_ok=True)

        # 夹具：S2 快照（2 族 3 题材；一族点亮、一族退坡）
        def th(tid, name, fam, stage, zt, pct, mb, lead=None, ref=None):
            m = {"n_valid": 100, "pct": pct, "breadth": 0.7, "zt_count": zt,
                 "max_board": mb, "n_lb2": 1, "n_first": zt - 1,
                 "first_share": 0.9, "amount": 1e9, "amount_ma": 1.2e9}
            ev = {"metrics": m, "n_constituents": 120,
                  "gates": {stage: {"required": [[True, "涨停≥5=%d" % zt],
                                                 [True, "宽度≥0.60=0.700"]]}},
                  "missing_metrics": [], "radar_ref": ref or {}}
            if lead:
                ev["leader"] = {"code": lead[0], "name": lead[1], "boards": mb,
                                "amount": 1e8, "tie_n": 1}
            return {tid: {"theme_id": tid, "theme_name": name, "family": fam,
                          "stage": stage, "day_n": 1, "day_n_anchor": "2026-09-29",
                          "stage_reason": ["夹具"],
                          "structure_bias": {"key": "k", "note": "结构背景夹具"},
                          "next_stage_trigger": {
                              "type": "t",
                              "conditions": [{"field": "pct", "op": ">=", "value": 3.0}],
                              "text": "涨≥3.00% → 转加速"},
                          "evidence": ev, "confidence": 0.9}}

        themes = {}
        themes.update(th("T1", "夹具题材甲", "A", "扩散", 9, 1.5, 2,
                         ("sz000001", "夹具龙头甲"), {"leader": "外证甲(10.00)"}))
        themes.update(th("T2", "夹具题材乙", "A", "发现", 3, 1.0, 1,
                         ("sz000002", "夹具龙头乙")))
        themes.update(th("T3", "夹具题材丙", "C", "尾声", 2, -0.5, 1))
        # 同族混合：A 族内再放一个「尾声」题材 —— 用于验证族级不被题材级覆盖
        themes.update(th("T4", "夹具题材丁", "A", "尾声", 4, 0.2, 1))
        snap = {"schema": 1, "s_id": "s2", "cap": "S2", "as_of": "2026-09-29",
                "n_themes": 4, "n_unresolved": 1, "n_family_unknown": 0,
                "n_radar_matched": 1, "themes": themes, "confidence": 0.9,
                "universe": {"caliber": "夹具口径（成分股等权聚合）"}}
        with io.open(os.path.join(gen, "s_s2_2026-09-29.json"), "w",
                     encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        with io.open(os.path.join(st, "mainline.json"), "w", encoding="utf-8") as f:
            json.dump({"version": 2, "updated_at": "2026-09-29",
                       "processed_upto": "2026-09-29",
                       "lines": {
                           "L:夹具主线": {"token": "夹具主线", "state": "active",
                                      "first_seen": "2026-09-20",
                                      "last_hit": "2026-09-29", "day_n": 4,
                                      "miss_streak": 0,
                                      "members": {"sz000001": {}}},
                           "L:休眠线": {"token": "休眠线", "state": "dormant",
                                     "first_seen": "2026-09-01",
                                     "last_hit": "2026-09-10", "day_n": 8,
                                     "miss_streak": 0, "members": {}}},
                       "data_annotations": {
                           "2026-09-23": {"kind": "DECLARED_KNOWN_GAP"}}},
                      f, ensure_ascii=False)
        with io.open(os.path.join(mem, "brain_state.md"), "w", encoding="utf-8") as f:
            # 环境档段须后接下一个 `## ` 标题（brief._ENV_SEC 契约：lookahead 到下一段）
            f.write("## 环境档（← C1 产出）\n\n| 项 | 值 |\n| --- | --- |\n"
                    "| 档位 | **强势** |\n| 仓位天花板（成） | 5 |\n"
                    "| as_of / source | 2026-09-29 / snapshot |\n| 涨停家数 | 57 |\n"
                    "\n## 板块腿（主线 / 题材阶段）\n\n（夹具占位）\n")

        d = collect("2026-09-29", base=sb)
        T("A1 S2 命中：题材 4 / 族 2",
          d["s2_present"] and len(d["themes"]) == 4 and len(d["families"]) == 2)
        fam = {f["family"]: f for f in d["families"]}
        T("A2 族态派生：A(扩散+发现+尾声)=点亮；C(仅尾声)=退坡",
          fam["A"]["state"] == "点亮" and fam["C"]["state"] == "退坡")
        T("A3 族序：点亮在前", d["families"][0]["family"] == "A")
        T("A4 族内序：扩散(9涨停) 排在 发现(3涨停) 前，尾声(4涨停) 垫底",
          [t[1]["theme_name"] for t in fam["A"]["rows"]]
          == ["夹具题材甲", "夹具题材乙", "夹具题材丁"])
        T("A9 族级不被题材级覆盖：含尾声的 A 族仍为点亮，且计数带出尾声",
          fam["A"]["state"] == "点亮"
          and dict(fam["A"]["stage_counts"]).get("尾声") == 1)
        T("A9b 环境锚时点核验 = PASS（夹具 as_of 与报告同日）",
          d["timepoint"]["status"] == "PASS"
          and d["timepoint"]["env_as_of"] == "2026-09-29"
          and not any("时点核验未通过" in g for g in d["gaps"]))
        # 时点不一致分支：把环境档 as_of 改成早一日
        with io.open(os.path.join(mem, "brain_state.md"), "w", encoding="utf-8") as f:
            f.write("## 环境档（← C1 产出）\n\n| 项 | 值 |\n| --- | --- |\n"
                    "| 档位 | **强势** |\n| 仓位天花板（成） | 5 |\n"
                    "| as_of / source | 2026-09-28 / snapshot |\n| 涨停家数 | 57 |\n"
                    "\n## 板块腿（主线 / 题材阶段）\n\n（夹具占位）\n")
        d_mm = collect("2026-09-29", base=sb)
        T("A10 时点不一致 → FAIL_MISMATCH 且点名两日期",
          d_mm["timepoint"]["status"] == "FAIL_MISMATCH"
          and "2026-09-28" in d_mm["timepoint"]["reason"]
          and "2026-09-29" in d_mm["timepoint"]["reason"]
          and any("时点核验未通过" in g for g in d_mm["gaps"]))
        T("A10b 不一致态报告含未通过标记与「不得当作当日」警示",
          "❌ **未通过（时点不一致）**" in render_md(d_mm)
          and "不得当作" in render_md(d_mm))
        # 时点缺失分支：环境档无 `as_of / source` 行
        with io.open(os.path.join(mem, "brain_state.md"), "w", encoding="utf-8") as f:
            f.write("## 环境档（← C1 产出）\n\n| 项 | 值 |\n| --- | --- |\n"
                    "| 档位 | **强势** |\n| 仓位天花板（成） | 5 |\n| 涨停家数 | 57 |\n"
                    "\n## 板块腿（主线 / 题材阶段）\n\n（夹具占位）\n")
        d_ab = collect("2026-09-29", base=sb)
        T("A11 时点缺失 → FAIL_ABSENT 且原因点明「`as_of / source` 行缺失」",
          d_ab["timepoint"]["status"] == "FAIL_ABSENT"
          and d_ab["timepoint"]["env_as_of"] is None
          and "缺失" in d_ab["timepoint"]["reason"]
          and "❌ **未通过（时点缺失）**" in render_md(d_ab))
        # 还原夹具环境档（后续用例依赖 PASS 态）
        with io.open(os.path.join(mem, "brain_state.md"), "w", encoding="utf-8") as f:
            f.write("## 环境档（← C1 产出）\n\n| 项 | 值 |\n| --- | --- |\n"
                    "| 档位 | **强势** |\n| 仓位天花板（成） | 5 |\n"
                    "| as_of / source | 2026-09-29 / snapshot |\n| 涨停家数 | 57 |\n"
                    "\n## 板块腿（主线 / 题材阶段）\n\n（夹具占位）\n")
        T("A5 环境档读出（非 UNAVAILABLE）", d["env"].get("regime") == "强势")
        T("A6 C2 主线状态机：active 1 / dormant 1，注记带出",
          d["mainline"]["n_active"] == 1 and d["mainline"]["n_dormant"] == 1
          and "2026-09-23" in d["mainline"]["annotations"])
        T("A7 S3 缺席 → s3 is None（不编造）", d["s3"] is None)
        T("A8 缺口点名校验：S3 缺席 + 外证 1/4 + 口径",
          any("S3" in g for g in d["gaps"]) and any("命中 1/4" in g for g in d["gaps"])
          and any("夹具口径" in g for g in d["gaps"]))

        md = render_md(d)
        T("B1 报告含边界声明（零 BUY/WAIT/NO）",
          "不含 BUY / WAIT / NO" in md and "不含仓位" in md)
        T("B2 报告含「阶段 ≠ 可买」纪律", "阶段 ≠ 可买" in md)
        T("B3 报告含证据分级 legend 四档",
          all(x in md for x in ("`F` 事实", "`C` 能力输出", "`I` 推断", "`V` 待验证")))
        T("B4 下一触发原样抄 S2 文本", "涨≥3.00% → 转加速" in md)
        T("B5 失效条件=本阶段 required 判据", "全过才维持 → 涨停≥5=9" in md)
        T("B6 失效条件缺口显式声明（无显式失效价）",
          "未输出显式「失效价 / 失效线」" in md)
        T("B7 观察对象段声明非推荐名单", "不是推荐名单、不是买入候选" in md)
        T("B8 族态「静默≠删除」声明在位", "「静默」≠ 删除" in md)
        T("B9 族态说明 = 呈现映射 + 两个「不代表」",
          "族内活跃状态的呈现映射" in md
          and "不代表族级强度排名" in md and "不构成交易判断" in md)
        T("B9b 声明不改 S2 判据 + 保留族内阶段计数",
          "不改 S2 判据" in md and "扩散×" in md)
        T("B10 L0 含时点核验 PASS 态（✅ 通过 + 两日期同显）",
          "**环境锚时点核验**：✅ **通过**" in md
          and "环境档 as_of = `2026-09-29`" in md and "报告 as_of = `2026-09-29`" in md)

        r1 = emit("2026-09-29", base=sb)
        T("C1 落点命名 = mainline_<as_of>.md 且在 reports/ 下",
          os.path.basename(r1["path"]) == "mainline_2026-09-29.md"
          and os.path.basename(os.path.dirname(r1["path"])) == "reports")
        T("C2 首跑 created=True", r1["created"] is True)
        T("C3 幂等：重跑 EXISTS 且零改写",
          emit("2026-09-29", base=sb)["reason"] == "EXISTS")
        T("C4 产物非空且含表头", os.path.getsize(r1["path"]) > 1500)

        # D 组：S2 缺席分支
        sb2 = tempfile.mkdtemp(prefix="mlr_sb2_")
        try:
            os.makedirs(os.path.join(sb2, "data", "generated"), exist_ok=True)
            d2 = collect("2026-09-29", base=sb2)
            md2 = render_md(d2)
            T("D1 S2 缺席 → 题材段 ABSENT 且如实声明",
              (not d2["s2_present"]) and "本报告无题材段可呈现" in md2)
            T("D2 S2 缺席 → 不虚构题材表",
              "夹具题材甲" not in md2 and "题材明细" not in md2)
            r2 = emit("2026-09-29", base=sb2)
            T("D3 缺席仍可落点（不因缺数据崩）", r2["created"] is True)
        finally:
            shutil.rmtree(sb2, ignore_errors=True)

        # E 组：入参契约（逐条显式抛错）
        for bad in ("2026/09/29", "20260929", None, 20260929):
            try:
                collect(bad, base=sb)
                T("E1 非法 as_of 须显式抛错 %r" % (bad,), False)
            except RadarError:
                T("E1 非法 as_of 须显式抛错 %r" % (bad,), True)

        # F 组：生产零触碰
        T("F1 本自检未写生产 reports/",
          not os.path.isfile(os.path.join(
              ROOT, "data", "generated", "reports", "mainline_2026-09-29.md")))
    finally:
        shutil.rmtree(sb, ignore_errors=True)
    print("== mainline_radar --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def _main(argv):
    ap = argparse.ArgumentParser(description="主线雷达报告（T3）")
    ap.add_argument("--eval", metavar="AS_OF", help="生成某日报告")
    ap.add_argument("--sim", action="store_true", help="SIM 轴标注（缺省 LIVE）")
    ap.add_argument("--base", help="数据根覆盖（沙盒；空 = 生产，生产写入须过 writegate）")
    ap.add_argument("--stdout", action="store_true", help="同时打印 Markdown")
    a = ap.parse_args(argv)
    if not a.eval:
        ap.print_help()
        return 2
    if a.base and not os.path.isdir(a.base):
        print("--base 必须是已存在目录，实际: %s" % a.base, file=sys.stderr)
        return 2
    r = emit(a.eval, sim=a.sim, base=a.base)
    md = io.open(r["path"], encoding="utf-8").read()
    if a.stdout:
        print(md)
    print("[%s] %s" % ("CREATED" if r["created"] else "EXISTS", r["path"]))
    print("  题材 %s 个 ｜ 族 %s 个 ｜ 缺口 %d 条"
          % (len(r["review"]["themes"]), len(r["review"]["families"]),
             len(r["review"]["gaps"])))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(_selftest())
    sys.exit(_main(sys.argv[1:]))
