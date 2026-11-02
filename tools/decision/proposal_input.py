#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""proposal_input.py · C4 正式提案入口（外部判断 → 机械校验 → 决策落账）

**为什么需要本件**：`brief.decide_from_proposal`（C4 正式提案通道）此前**只有 brief 自身自检
在调用**，生产路径无调用方 ⇒ 判断链在「提案」这一环是断的。本件补上这一环。

**职责边界（不重设计 C4）**：
  · 本件**不生成、不补全、不猜测** proposal 的任何字段 —— proposal **全部来自外部文件**
    （LLM / 操作者的唯一通道）。缺 ⇒ 如实报 `LLM_UNAVAILABLE`，**不落账**。
  · 装配复用 `brief.assemble`；校验复用 `brief.classify`；落账复用
    `brief.decide_from_proposal` → `orchestrate.decide`（写点已在案）⇒ **零新增写点**。
  · 不 import 任何网络模块（骨架纪律：数据只从投放区进）。

**失败行为（fail-closed，四态互斥且均不落账）**：
  `BRIEF_INCOMPLETE`    brief 缺决策前提（无 Thesis / 无 C3 链）—— 连 LLM 都不该调
  `LLM_UNAVAILABLE`     外部未提供该 code 的 proposal（端点不可用 / 超时 / 未产出）
  `PROPOSAL_MALFORMED`  结构或值域不合规
  `EVIDENCE_UNRESOLVED` basis 引用了不在场的来源（防编造）

**proposal 文件契约**（外部产物，本件只读）：
  {"schema_version": 1, "as_of": "<date>", "producer": "<谁产的>",
   "proposals": {"<code>": {…C4 提案…}, ...},
   "chains":    {"<code>": {…C3 驱动链 proposal…}, ...},
   "note": "…"}
  · `proposals[*]` 字段契约 = `brief.PROPOSAL_KEYS`（本件不复制该清单）。
  · `chains[*]`    字段契约 = `drive._review_core` 的入参
    `{driver, evidence[], inference[], prediction[], five{五件套}, thesis_id?}`
    —— 这是**C3 的判断产物**（证据→推断→预测→五件套），与 C4 提案是**两份不同的判断**，
    **不可互相推导**：C4 的 `basis[].ref` 命名空间（C1:ENV/S3:EMOTION/S1:EFFECT）与
    C3 的 `evidence[].source` 枚举（SNAPSHOT:*/C2:CANDIDATE/C1:BRAIN_STATE/S1:EVENT_EVAL）
    不同域，把 basis 映射成 evidence 等于**伪造证据出处**，故本件**不做任何映射**。
  · 缺 `chains[code]` ⇒ 该票不落链，状态如实标 `C3_PROPOSAL_ABSENT`，**不落账**。

**正式输入目录约定**（未显式给 `--proposals` 时按此查找；不存在即 LLM_UNAVAILABLE）：
  `<base>/data/input/proposals_<as_of>.json`（生产 base = 仓库根）。
  本件**只读该文件/目录级约定**，不扫描、不轮询、不做文件发现。
"""
import argparse
import io
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_FS = os.path.normpath(os.path.join(_HERE, "..", "fs"))
if _FS not in sys.path:
    sys.path.insert(0, _FS)

import brief as BR                                                    # noqa: E402
import candidates as CAND                                             # noqa: E402
import decision as DEC                                                # noqa: E402
import drive as DRV                                                   # noqa: E402
import thesis as TH                                                   # noqa: E402

# 活跃候选状态（与 C2 状态机一致；EXPIRED/TRADED 不再提案）
_INACTIVE = ("EXPIRED", "TRADED")
_S_NAME = "candidates.json"
_S_DIR = ("data", "state")
_S_THESIS = ("memory", "state", "thesis.md")
_S_SNAP = ("data", "snapshot")
_S_PARAMS = ("data", "params.json")
_S_BRAIN = ("memory", "state", "brain_state.md")
_S_CHAINS = ("data", "research", "drive", "chains.jsonl")
_S_CHAINS_DIR = ("data", "research", "drive")
_S_INPUT = ("data", "input")


class ProposalInputError(Exception):
    """本件入参/契约违规（显式抛，不静默降级）。"""


def _check_date(s):
    if not isinstance(s, str) or len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise ProposalInputError("as_of 必须是 YYYY-MM-DD，实际 %r" % (s,))
    for i in (0, 1, 2, 3, 5, 6, 8, 9):
        if not s[i].isdigit():
            raise ProposalInputError("as_of 非法：%r" % (s,))
    return s


def _p(base, *parts):
    return os.path.join(base, *parts) if base else None


def default_proposals_path(as_of, base=None):
    """正式输入路径：`<base>/data/input/proposals_<as_of>.json`（只读约定，不扫描目录）。

    两步拼装：先常量目录（审计可归类），再拼动态文件名 —— 避免被落点审计误判为
    「`data/input/<动态名>` 第 6 类落点」（本件对该路径**只读**，无写点）。
    """
    if not base:
        return None
    d = os.path.join(base, *_S_INPUT)
    return os.path.join(d, "proposals_%s.json" % as_of)


def _pairs_hook(pairs):
    """JSON 对象重复键检测 —— `json.load` 默认**静默后者覆盖前者**（proposal 会被吞掉）。"""
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise ProposalInputError("proposal 文件存在重复键 %r（JSON 重复键会被静默覆盖，拒收）"
                                     % (k,))
        seen[k] = v
    return seen


def load_proposals(path):
    """外部判断产物 → (as_of, {code: C4 提案}, {code: C3 链提案})。

    文件缺失 ⇒ (None, {}, {})（= LLM 不可用，合法态，不是错误）。
    `chains` 键可选：缺 ⇒ 空映射（下游按 `C3_PROPOSAL_ABSENT` fail-closed，不臆造链）。
    """
    if not path or not os.path.isfile(path):
        return None, {}, {}
    with io.open(path, encoding="utf-8-sig") as f:
        d = json.load(f, object_pairs_hook=_pairs_hook)
    if not isinstance(d, dict):
        raise ProposalInputError("proposal 文件顶层必须是 dict")
    items = d.get("proposals")
    if items is None:
        raise ProposalInputError("proposal 文件缺 proposals 键")
    if not isinstance(items, dict):
        raise ProposalInputError("proposals 必须是 {code: proposal} 映射")
    chains = d.get("chains") or {}
    if not isinstance(chains, dict):
        raise ProposalInputError("chains 必须是 {code: C3 链提案} 映射")
    return d.get("as_of"), items, chains


def active_codes(base=None, codes=None, limit=None):
    """候选来源：显式 codes > 候选池活跃票。仅取 code，不读候选内容当判断。"""
    if codes:
        out = list(codes)
    else:
        p = _p(base, *_S_DIR, _S_NAME)
        if not p or not os.path.isfile(p):
            return []
        with io.open(p, encoding="utf-8-sig") as f:
            cs = (json.load(f) or {}).get("candidates") or []
        out = sorted({c.get("code") for c in cs
                      if c.get("code") and c.get("state") not in _INACTIVE})
    return out[:limit] if limit else out


def run(as_of, proposals_path, *, base=None, sim=True, codes=None, limit=None):
    """逐票：C3 链落盘（若有链提案）→ assemble → 取外部提案 → classify → decide。

    失败一律**不落账**，且**单票异常不中断整批**（逐票如实记录后继续）。
    """
    d = _check_date(as_of)
    if not isinstance(sim, bool):
        raise ProposalInputError("sim 必须显式布尔（LIVE/SIM 不表态 = 报错）")
    src = proposals_path or default_proposals_path(d, base=base)
    p_asof, props, chains_map = load_proposals(src)
    if proposals_path and os.path.isfile(proposals_path) and p_asof and p_asof != d:
        raise ProposalInputError("proposal 文件 as_of=%s 与本次 as_of=%s 不一致（拒用旧判断）"
                                 % (p_asof, d))

    pool_p = _p(base, *_S_DIR, _S_NAME)
    thesis_p = _p(base, *_S_THESIS)
    snap_root = _p(base, *_S_SNAP)
    params_p = _p(base, *_S_PARAMS)
    # ★ brain_path 必须跟随 base：`brief.read_env` 缺省回落**生产** `brain_state.md`
    #   （实测：不给时读到了生产真实环境档）——不显式传即等于绕过隔离。
    brain_p = _p(base, *_S_BRAIN)
    chains_p = _p(base, *_S_CHAINS)
    # decide 形参（`decide_from_proposal` **无** chains_path —— 曾误传致 TypeError）
    kw = {"pool_path": pool_p, "thesis_path": thesis_p, "params_path": params_p,
          "snapshot_root": snap_root}

    todo = active_codes(base=base, codes=codes, limit=limit)
    results, by_status = [], {}

    def bump(s):
        by_status[s] = by_status.get(s, 0) + 1

    for code in todo:
        rec = {"code": code}
        prop = (props or {}).get(code)          # ★ 只取外部给的；不生成、不补默认

        # ── C3 前置链：用**独立的外部 C3 链提案**落链（drive.review 幂等） ──────────
        #    ★ 2026-10-19 修复：原实现把 **C4 提案** 直接喂给 `drive.review`，而 review 的
        #      入参契约是 C3 形态 {driver,evidence,inference,prediction,five} ⇒ 必然
        #      `PROPOSAL_INVALID`（实测「多=['basis','action_proposed',…] 缺=['evidence',…]」）
        #      ⇒ 链**从未落过盘**，C3:CHAIN 恒缺。C4 的 basis 与 C3 的 evidence 不同域，
        #      互相推导 = 伪造证据出处，故**不做映射**，改为要求外部同时交付 C3 链提案。
        c3 = (chains_map or {}).get(code)
        if prop is not None and c3 is None:
            rec["chain_error"] = ("C3_PROPOSAL_ABSENT: 外部未提供该票 C3 链提案"
                                  "（chains[%r]），链不臆造" % code)
        elif c3 is not None:
            try:
                # s1_path / sx_root 同样跟随 base（否则 review 会读生产 S1/S2/S3/S5 快照）
                # 两步拼装：先常量目录（可归类），再动态文件名（不产生 data/generated/<dyn> 假落点）
                _gend = os.path.join(base, "data", "generated") if base else None
                _s1p = os.path.join(_gend, "s_s1_%s.json" % d) if _gend else None
                # ★ 目录准备由**调用方**负责：`drive.review` 以 "a" 打开不建父目录，
                #   而 `data/research/drive` 生产/沙盒都可能不存在 ⇒ 首条真实链必
                #   FileNotFoundError（实测）。放这里而不改 drive.py，是为了只动本件。
                #   ★ 2026-10-20 修（A 路生产首跑实证）：原用 `_p(base, *_S_CHAINS_DIR)`，
                #   而 `_p(None, …)` 返回 None ⇒ 生产（base=None）makedirs 被跳过 ⇒
                #   review 回退默认 CHAINS_PATH 时父目录不存在 ⇒ FileNotFoundError。
                #   改为从 review **实际使用**的路径（path or CHAINS_PATH）派生目录。
                _cdir = os.path.dirname(chains_p or DRV.CHAINS_PATH)
                os.makedirs(_cdir, exist_ok=True)
                rv = DRV.review(c3, as_of=d, code=code, path=chains_p,
                                snapshot_root=snap_root, pool_path=pool_p,
                                brain_state_path=brain_p,
                                s1_path=_s1p, sx_root=_gend)
                rec["chain_appended"] = bool(rv.get("appended"))
                rec["chain_id"] = rv.get("chain_id")
            except Exception as e:                                   # noqa: BLE001
                rec["chain_error"] = "%s: %s" % (type(e).__name__, e)

        # ★ fail-closed：本轮**给了** C3 链提案但未落成 ⇒ 不拿当日既有链顶替。
        #   （`load_chain(code, as_of)` 会取当日最近一条 —— 若本轮判断失败仍放行，
        #     就是"旧判断冒充新判断"。无链提案时允许读当日既有链，属同日已落判断。）
        if c3 is not None and rec.get("chain_error"):
            rec["status"] = BR.ST_BRIEF_INCOMPLETE
            rec["detail"] = "C3 链未落成，不放行决策：%s" % rec["chain_error"]
            rec["ledger_written"] = False
            bump(rec["status"])
            results.append(rec)
            continue

        try:
            b = BR.assemble(d, code, pool_path=pool_p, thesis_path=thesis_p,
                            params_path=params_p, brain_path=brain_p, base=base,
                            chains_path=chains_p)
        except Exception as e:                                       # noqa: BLE001
            # 前提类缺口（Thesis 未初始化 / 链缺失）= 输入装配缺口，**不调 LLM、不落账**；
            # 映射为 BRIEF_INCOMPLETE，与 classify 的前提分支同语义（非真错误）。
            nm = type(e).__name__
            st = (BR.ST_BRIEF_INCOMPLETE
                  if nm in ("ThesisError", "DriveError", "ChainError", "CandidateError")
                  else "ASSEMBLE_ERROR")
            rec["status"] = st
            rec["detail"] = "%s: %s" % (nm, e)
            rec["ledger_written"] = False
            bump(st)
            results.append(rec)
            continue

        rec["missing"] = list(b.get("missing") or [])
        rec["env_source"] = (b.get("env") or {}).get("source")   # 隔离自证：沙盒须为 UNAVAILABLE
        st, info = BR.classify(prop, b)
        rec["status"] = st
        rec["detail"] = info
        rec["ledger_written"] = False

        if st == BR.ST_OK:
            # ★ 决策主体存在性前置校验：proposal 自报的 thesis_id 必须真的在 Thesis 池里。
            #   `decision.write_decision` 对此有 fail-closed（"决策不得挂悬空 thesis_id"），
            #   但那是**异常**、往上抛会中断整批；这里提前判并把原因落到逐票记录上。
            th_state = None
            if b.get("thesis_id"):
                try:
                    th_state = TH.read_thesis(b["thesis_id"], thesis_p)
                except TH.ThesisError as e:
                    rec["status"] = "THESIS_ABSENT"
                    rec["detail"] = ("%s —— proposal 自报 thesis_id=%s 不在 Thesis 池"
                                     "（不代建：Thesis 的 claim 是外部判断，本件无该字段）"
                                     % (e, b.get("thesis_id")))
                    bump("THESIS_ABSENT")
                    results.append(rec)
                    continue
            try:
                rep = BR.decide_from_proposal(prop, b, sim=sim, thesis_state=th_state,
                                              base=base, **kw)
            except Exception as e:                                   # noqa: BLE001
                # 单票异常**不中断整批**：如实记录，零落账（write_decision 先校验后写）
                rec["status"] = "DECIDE_ERROR"
                rec["detail"] = "%s: %s" % (type(e).__name__, e)
                bump("DECIDE_ERROR")
                results.append(rec)
                continue
            rec["decide"] = rep.get("status")
            rec["decision_id"] = rep.get("decision_id")
            rec["action"] = rep.get("action")
            rec["ledger_written"] = rep.get("decision_id") is not None
        bump(st)
        results.append(rec)

    return {"as_of": d, "sim": sim, "proposals_source": src,
            "proposal_as_of": p_asof, "n_candidates": len(todo),
            "n_ledger_written": sum(1 for r in results if r.get("ledger_written")),
            "by_status": by_status, "results": results}


# ── 自检（全合成夹具，真库零接触） ────────────────────────────────────────────
def _selftest():
    ok = fail = 0
    # ★ 零污染动态基准（2026-10-20）：A 路生产落账后 sim/decision_ledger.jsonl
    #   合法存在 ⇒「不存在」式硬编码失效；G1 改「自检前后账本状态不变」。
    from paths import ROOT as _R

    def _led_state():
        p = os.path.join(_R, "data", "generated", "sim",
                         "decision_ledger.jsonl")
        if not os.path.isfile(p):
            return None
        return (os.path.getsize(p), os.path.getmtime(p))
    _G1_BEFORE = _led_state()

    def T(name, cond, extra=None):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s%s" % ("✓" if cond else "✗", name,
                               "" if cond else "  ← %s" % (extra,)))

    print("== proposal_input --selftest ==")
    sb = tempfile.mkdtemp(prefix="pi_sb_")
    try:
        gend = os.path.join(sb, "data", "generated")
        for x in ("data/state", "data/snapshot", "memory/state", "data", "sim"):
            os.makedirs(os.path.join(sb, x.replace("/", os.sep)), exist_ok=True)

        # 候选池 / params 从生产**只读复制**（契约形态真实；只读源、写沙盒副本）
        import shutil as _sh
        from paths import ROOT as _R0
        _sh.copy(os.path.join(_R0, "data", "state", "candidates.json"),
                 os.path.join(sb, "data", "state", "candidates.json"))
        _sh.copy(os.path.join(_R0, "data", "params.json"),
                 os.path.join(sb, "data", "params.json"))
        with io.open(os.path.join(sb, "data", "state", "candidates.json"),
                     encoding="utf-8-sig") as f:
            _cs = (json.load(f) or {}).get("candidates") or []
        _act = [c["code"] for c in _cs if c.get("code")
                and c.get("state") not in ("EXPIRED", "TRADED")]
        TH.init_file(os.path.join(sb, "memory", "state", "thesis.md"))

        # A 组：入参契约
        for bad in ("2026/09/29", "20260929", None, 20260929):
            try:
                run(bad, None, base=sb)
                T("A1 非法 as_of 须抛错 %r" % (bad,), False)
            except ProposalInputError:
                T("A1 非法 as_of 须抛错 %r" % (bad,), True)
        try:
            run("2026-09-29", None, base=sb, sim="yes")
            T("A2 sim 非布尔须抛错", False)
        except ProposalInputError:
            T("A2 sim 非布尔须抛错", True)

        # B 组：proposal 文件缺失 = LLM 不可用（合法态，零落账）
        r = run("2026-09-29", None, base=sb, limit=2)
        T("B1 无 proposal → 全部 fail-closed 且零落账",
          r["n_ledger_written"] == 0 and
          all(x["status"] in (BR.ST_BRIEF_INCOMPLETE, BR.ST_LLM_ABSENT)
              for x in r["results"]),
          "%s ｜ %s" % (r["by_status"], [x.get("detail") for x in r["results"]][:1]))
        # B1c：前提齐全 + proposal 缺席 ⇒ 真正的 LLM_UNAVAILABLE 分支
        _thp = os.path.join(sb, "memory", "state", "thesis.md")
        _bp = BR.assemble("2026-09-29", _act[0],
                          pool_path=os.path.join(sb, "data", "state", "candidates.json"),
                          thesis_path=_thp,
                          params_path=os.path.join(sb, "data", "params.json"),
                          brain_path=os.path.join(sb, "memory", "state", "brain_state.md"),
                          base=sb)
        _bfull = dict(_bp, thesis_id="TH-T",
                      chain={"evidence": ["C1:ENV"], "inference": [], "prediction": [],
                             "five": {k: "有" for k in DEC.FIVE_KEYS}})
        T("B1c 前提齐 + proposal 缺席 → LLM_UNAVAILABLE（不落账）",
          BR.classify(None, _bfull)[0] == BR.ST_LLM_ABSENT)
        T("B1b ★ 环境档未读生产（brain_path 跟随 base）",
          all(x.get("env_source") == "UNAVAILABLE" for x in r["results"]),
          [x.get("env_source") for x in r["results"]])
        T("B2 EXPIRED/TRADED 不进候选（全量比对真实候选池）",
          len(active_codes(base=sb, limit=None)) == len(set(_act)),
          "%d vs %d" % (len(active_codes(base=sb, limit=None)), len(set(_act))))
        T("B3 账本未被创建（零落账）",
          not os.path.isfile(os.path.join(sb, "sim", "decision_ledger.jsonl")))

        # C 组：proposal 文件结构违规
        bad_f = os.path.join(sb, "bad.json")
        with io.open(bad_f, "w", encoding="utf-8") as f:
            json.dump({"as_of": "2026-09-29"}, f)
        try:
            run("2026-09-29", bad_f, base=sb)
            T("C1 缺 proposals 键须抛错", False)
        except ProposalInputError:
            T("C1 缺 proposals 键须抛错", True)

        # D 组：as_of 不一致（拒用旧判断）
        old_f = os.path.join(sb, "old.json")
        with io.open(old_f, "w", encoding="utf-8") as f:
            json.dump({"as_of": "2026-09-01", "proposals": {}}, f)
        try:
            run("2026-09-29", old_f, base=sb)
            T("D1 proposal as_of 不一致须抛错", False)
        except ProposalInputError:
            T("D1 proposal as_of 不一致须抛错", True)

        # E 组：非法 proposal（值域）→ MALFORMED，零落账
        e_f = os.path.join(sb, "e.json")
        with io.open(e_f, "w", encoding="utf-8") as f:
            json.dump({"as_of": "2026-09-29", "producer": "fixture",
                       "proposals": {"sz000001": {"schema_version": 1,
                                                  "as_of": "2026-09-29",
                                                  "code": "sz000001",
                                                  "thesis_id": "TH-X",
                                                  "driver": "不存在的驱动",
                                                  "basis": [{"ref": "C1:ENV", "claim": "c"}]}}},
                      f)
        r = run("2026-09-29", e_f, base=sb, limit=2)
        T("E1 非法 driver → 不落账，状态非 OK",
          r["n_ledger_written"] == 0 and
          all(x["status"] != BR.ST_OK for x in r["results"]), r["by_status"])

        # F 组：codes 显式指定 + limit
        r = run("2026-09-29", None, base=sb, codes=["sz000001"], limit=1)
        T("F1 codes 显式指定生效", r["n_candidates"] == 1, r["n_candidates"])

        # ── F 组：C3 前置链（真实双契约：C3 链提案 + C4 提案）───────────────────
        #    夹具：合成 5 件快照（沙盒内）、真实 create_thesis、合成链/提案。
        #    five 的「剩余预期」如实留空 —— 不做判断的项不许编，q1 会因此不过 ⇒ NO。
        _c0 = _act[0]
        _snap = os.path.join(sb, "data", "snapshot", "2026-09-29")
        os.makedirs(_snap, exist_ok=True)
        with io.open(os.path.join(_snap, "daily_quote.csv"), "w", encoding="utf-8",
                     newline="\n") as f:
            f.write("code,name,open,high,low,close,pct,vol,amount\n"
                    "%s,夹具,10.0,10.5,9.8,10.2,2.0,100,1000\n" % _c0)
        with io.open(os.path.join(_snap, "limit_up.csv"), "w", encoding="utf-8",
                     newline="\n") as f:
            f.write("code,name,boards,first_seal_time,seal_amount,reason\n"
                    "%s,夹具,1,09:35,100000000,夹具\n" % _c0)
        with io.open(os.path.join(_snap, "dragon_tiger.csv"), "w", encoding="utf-8",
                     newline="\n") as f:
            f.write("code,name,seat,buy,sell,net\n")
        with io.open(os.path.join(_snap, "news.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"news_id": "N1", "ts": "2026-09-29 20:00:00",
                                "source": "fixture", "title": "夹具",
                                "body": "夹具", "related_codes": [_c0]},
                               ensure_ascii=False) + "\n")
        with io.open(os.path.join(_snap, "env_metrics.json"), "w",
                     encoding="utf-8") as f:
            json.dump({"limit_up_count": 57, "max_board": 6, "turnover": 1.4e12,
                       "up_down_ratio": 1.8, "fail_rate": 0.21,
                       "index_ma20": 3912.8}, f)

        _thp2 = os.path.join(sb, "memory", "state", "thesis.md")
        _th = TH.create_thesis("2026-09-29", "C", "夹具：C3 链契约验证", code=_c0,
                               path=_thp2)
        _five = {k: "夹具值" for k in DEC.FIVE_KEYS}
        _five["剩余预期"] = None          # ← 不做判断就留空（q1 会不过 ⇒ NO）
        C3 = {"driver": "C", "thesis_id": _th["id"],
              "evidence": [{"text": "涨停家数 57（夹具）",
                            "source": "SNAPSHOT:env_metrics",
                            "ref": "limit_up_count"}],
              "inference": ["情绪扩散（夹具）"], "prediction": [], "five": _five}
        C4 = {"schema_version": 1, "as_of": "2026-09-29", "code": _c0,
              "thesis_id": _th["id"], "driver": "C",
              "basis": [{"ref": "C3:CHAIN", "claim": "驱动链已落（夹具）"}],
              "action_proposed": "NO", "position_request": 0,
              # NO 按 1.4 必须带翻案条件（三问契约，不在本件放宽）
              "overturn_event": {"text": "涨停家数回升至 80 以上且该票成交额放大一倍",
                                 "rule": None}}

        def _write_pf(name, props=None, chains=None):
            p = os.path.join(sb, name)
            d = {"as_of": "2026-09-29", "producer": "fixture"}
            if props is not None:
                d["proposals"] = props
            if chains is not None:
                d["chains"] = chains
            with io.open(p, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            return p

        f_f = _write_pf("f.json", {_c0: C4}, {_c0: C3})
        r5 = run("2026-09-29", f_f, base=sb, codes=[_c0], limit=1)
        _r0 = r5["results"][0]
        T("F2 ★ C3 链真落盘（C3 链提案 → drive.review appended=True）",
          _r0.get("chain_appended") is True,
          "chain_appended=%r chain_error=%r"
          % (_r0.get("chain_appended"), _r0.get("chain_error")))
        T("F3 ★ 解包生效：C3:CHAIN 不再缺（信封 → 4 键链）",
          "C3:CHAIN" not in (_r0.get("missing") or []), _r0.get("missing"))
        T("F4 链文件落点跟随 base（沙盒）",
          os.path.isfile(os.path.join(sb, *_S_CHAINS)))
        T("F4b ★ 双契约齐 ⇒ 真跑到落账（C4 三问给出 NO，非零推荐伪造）",
          _r0.get("ledger_written") is True and _r0.get("action") == "NO",
          "status=%r action=%r decide=%r detail=%r"
          % (_r0.get("status"), _r0.get("action"), _r0.get("decide"),
             str(_r0.get("detail"))[:120]))

        # F5：缺 chains 键 ⇒ C3_PROPOSAL_ABSENT（链不臆造）
        _c1 = _act[1] if len(_act) > 1 else _c0
        C4b = dict(C4, code=_c1)
        r6 = run("2026-09-29", _write_pf("g.json", {_c1: C4b}, None),
                 base=sb, codes=[_c1], limit=1)
        T("F5 缺 chains 键 ⇒ C3_PROPOSAL_ABSENT 且零落账（不臆造链）",
          "C3_PROPOSAL_ABSENT" in str(r6["results"][0].get("chain_error"))
          and r6["n_ledger_written"] == 0, r6["results"][0].get("chain_error"))

        # F6：JSON 重复键 ⇒ 拒收（json 默认静默覆盖会吞 proposal）
        dup_f = os.path.join(sb, "dup.json")
        with io.open(dup_f, "w", encoding="utf-8") as f:
            f.write('{"as_of":"2026-09-29","proposals":{"sz000001":{},'
                    '"sz000001":{}}}')
        try:
            run("2026-09-29", dup_f, base=sb)
            T("F6 JSON 重复键须拒收（防静默覆盖）", False)
        except ProposalInputError:
            T("F6 JSON 重复键须拒收（防静默覆盖）", True)

        # F7：缺省 --proposals ⇒ 正式输入目录约定
        _inp = os.path.join(sb, *_S_INPUT)
        os.makedirs(_inp, exist_ok=True)
        with io.open(os.path.join(_inp, "proposals_2026-09-29.json"), "w",
                     encoding="utf-8") as f:
            json.dump({"as_of": "2026-09-29", "proposals": {}}, f)
        T("F7 缺省 --proposals ⇒ <base>/data/input/proposals_<as_of>.json",
          default_proposals_path("2026-09-29", base=sb)
          == os.path.join(_inp, "proposals_2026-09-29.json")
          and os.path.isfile(default_proposals_path("2026-09-29", base=sb)))

        # F8：proposal 自报的 thesis 不在池 ⇒ THESIS_ABSENT（不代建、不落账、不中断整批）
        _c2 = _act[2] if len(_act) > 2 else _c0
        C3b = dict(C3, thesis_id="TH-20260929-999")
        C4c = dict(C4, code=_c2, thesis_id="TH-20260929-999")
        r8 = run("2026-09-29", _write_pf("h.json", {_c2: C4c}, {_c2: C3b}),
                 base=sb, codes=[_c2], limit=1)
        T("F8 ★ 悬空 thesis ⇒ THESIS_ABSENT（不代建 claim）且零落账",
          r8["results"][0]["status"] == "THESIS_ABSENT"
          and r8["n_ledger_written"] == 0,
          "status=%r ｜ %s" % (r8["results"][0]["status"],
                               str(r8["results"][0].get("detail"))[:140]))

        # G 组：生产零接触
        from paths import ROOT as _R
        T("G1 自检未触碰生产账本（前后状态不变——动态基准，非硬编码不存在）",
          _led_state() == _G1_BEFORE, "%r → %r" % (_G1_BEFORE, _led_state()))

        # H 组：read_env 路径泄漏修复（显式 > base 派生 > 正式默认）
        T("H1 read_env(base=沙盒) 不读生产 ⇒ 沙盒无该文件故 UNAVAILABLE",
          BR.read_env(base=sb)["source"] == "UNAVAILABLE",
          BR.read_env(base=sb)["source"])
        T("H2 read_env 显式 brain_path 优先（既有调用兼容）",
          BR.read_env(os.path.join(sb, "nope.md"), base=sb)["source"]
          == "UNAVAILABLE")
        T("H3 read_env 全缺省 = 正式默认（生产行为不变）",
          BR.read_env()["source"] != "UNAVAILABLE", BR.read_env()["source"])
    finally:
        import shutil
        shutil.rmtree(sb, ignore_errors=True)

    print("== proposal_input --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def _main(argv):
    ap = argparse.ArgumentParser(description="C4 正式提案入口（外部判断 → 校验 → 落账）")
    ap.add_argument("--as-of", dest="as_of", help="决策日 YYYY-MM-DD")
    ap.add_argument("--proposals", help="外部 proposal 文件；缺省 = "
                                        "<base>/data/input/proposals_<as_of>.json"
                                        "（仍不存在 ⇒ LLM 不可用，零落账）")
    ap.add_argument("--base", help="数据/账本根（沙盒）；缺省 = 生产")
    ap.add_argument("--live", action="store_true", help="LIVE 轴（缺省 SIM）")
    ap.add_argument("--code", action="append", help="显式 code（可多次）")
    ap.add_argument("--limit", type=int, help="候选上限")
    ap.add_argument("--which", action="store_true",
                    help="只打印本次会用的 proposal 文件路径与存在性（不跑决策）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return _selftest()
    if not a.as_of:
        ap.print_help()
        return 2
    if a.base and not os.path.isdir(a.base):
        print("--base 必须是已存在目录，实际: %s" % a.base, file=sys.stderr)
        return 2
    if a.which:
        p = a.proposals or default_proposals_path(a.as_of, base=a.base)
        print("proposal 文件 = %s" % (p or "UNAVAILABLE（无 base 且未显式给路径）"))
        print("  存在 = %s" % (bool(p) and os.path.isfile(p)))
        return 0
    rep = run(a.as_of, a.proposals, base=a.base, sim=not a.live,
              codes=a.code, limit=a.limit)
    print("[proposal_input] as_of=%s 候选=%d 落账=%d"
          % (rep["as_of"], rep["n_candidates"], rep["n_ledger_written"]))
    for k in sorted(rep["by_status"]):
        print("   %-22s %d" % (k, rep["by_status"][k]))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
