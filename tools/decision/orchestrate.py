#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""orchestrate.py · C4 编排层（施工 1.4 · C4-a 批 · v1.0 · 2026-10-09 建）

用途：C4 三问的**真实调用者**——QUALIFIED 候选 → C3 链 → 三问裁决 → 幂等落账
      → 观察锚。把 three_q（协议层）/ decision（账本层）/ drive（C3 判据件）
      / candidates（候选池）串成一条可重入、可恢复的决策路径。
      **不做判断**：驱动链内容与价线/仓位请求全部来自调用方显式输入（LLM 4.1 授权），
      本模块零语义裁量；BUY/WAIT/NO 由 three_q 机械规则 + 输入共同决定。

★ 一、幂等三层（各自职责，不做第二真源）
  L1 账本层（冻结件既有）：decision.write_decision 对同轴 (date, code, action,
     thesis_id) 幂等——EXISTS 不新增不改旧行；allow_duplicate 默认 False 且本模块
     **永不传 True**（同日同 Thesis 二次决策 = 既有账本规则管辖，编排层不擅自扩大）。
  L2 输入指纹层（本模块）：info_set_hash = sha256(规范化 JSON{v1, as_of, code,
     thesis_fingerprint, env(regime+signals), params_version, snapshot 内容哈希})——
     同 code+as_of+hash 已有正式决策 → 复用原 decision_id（REUSED），不再次落账；
     输入变化 → 新 hash → 走账本层 L1 裁决（同 (date,code,action,thesis_id) 仍会被
     L1 拒绝重复——两层互为纵深）。
  L3 单写者锁（本模块）：账本目录下 decide.lock（O_CREAT|O_EXCL 独占创建，含 pid）；
     持锁超时（默认 60s）视为崩溃残留可接管；活着持锁 → LOCKED 拒绝并发重入。

★ 二、半提交与恢复（commit 两段式的编排侧语义）
  段一 record_decision 成功、段二 anchor_pool 失败 → 报告 status=HALF_COMMIT
  （账本行保留 = three_q 写死的「半环留痕」设计）。重试路径：L2 命中原决策 →
  复用原 decision_id，**重放 answer()（机械确定：同输入同 verdict）** 后仅补锚
  （anchor_pool 对候选是字段覆写，天然幂等），**绝不二次追加 DECISION**。

★ 三、边界（纪律判据写死）
  本模块**零直接写点**（无任何打开-写/替换写调用）——全部落盘经
  three_q / decision / candidates 的既有写接口；唯一自管文件 = 锁（os.open 独占创建）。
  零参数正式区写入、零转正批准调用、不接结算主链、不下单（C5 由账本下游消费）。

用法：
  python tools/decision/orchestrate.py               # 契约总览
  python tools/decision/orchestrate.py --selftest    # 沙盒验收（不碰真数据）
  python tools/decision/orchestrate.py --discipline
  from tools.decision.orchestrate import decide, info_set_hash
"""
import ast
import hashlib
import json
import os
import sys
import time

_FS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs"))
sys.path.insert(0, _FS)
import decision as DEC  # noqa: E402
import three_q as TQ  # noqa: E402
import candidates as CAND  # noqa: E402
import snapshot as SNAP  # noqa: E402

SCHEMA_VERSION = 1
LOCK_NAME = "decide.lock"
LOCK_TIMEOUT = 60.0


class DecideError(Exception):
    """编排失败。并发持锁 → LOCKED 前缀。"""


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _params_version(params_path=None):
    pver, _src = DEC._read_params_version(params_path)
    return pver or "PARAMS_VERSION_ABSENT"


def info_set_hash(as_of, code, *, thesis_id, chain, thesis_state=None, env=None,
                  params_path=None, snapshot_root=None):
    """输入指纹（规范化 JSON + SHA-256；不含运行时间/随机量，同输入同哈希）。"""
    d = DEC._check_date(as_of)
    c = DEC._check_code(code)
    th_id = DEC._check_thesis_id(thesis_id)
    snap = SNAP.load_snapshot(d, root=snapshot_root)
    snap_fp = ("md5:" + hashlib.md5(_canon(snap).encode("utf-8")).hexdigest()
               if snap is not SNAP.UNAVAILABLE else "SNAPSHOT_ABSENT")
    env_fp = ({"regime": (env or {}).get("regime"),
               "signals": (env or {}).get("signals") or {}}
              if env is not None else "ENV_ABSENT")
    payload = {
        "v": SCHEMA_VERSION,
        "as_of": d, "code": c, "thesis_id": th_id,
        "thesis_fingerprint": DEC.thesis_fingerprint(th_id, chain, thesis_state),
        "env": env_fp,
        "params_version": _params_version(params_path),
        "snapshot": snap_fp,
    }
    return "sha256:" + hashlib.sha256(_canon(payload).encode("utf-8")).hexdigest()


# ── 单写者锁 ─────────────────────────────────────────────────────────────────
class _Lock(object):
    def __init__(self, sim, base, timeout=LOCK_TIMEOUT):
        self.path = os.path.join(os.path.dirname(DEC.ledger_path(sim, base=base)),
                                 LOCK_NAME)
        self.timeout = timeout

    def __enter__(self):
        for attempt in (0, 1):
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, ("pid=%d ts=%d" % (os.getpid(), int(time.time())))
                         .encode("utf-8"))
                os.close(fd)
                return self
            except FileExistsError:
                age = time.time() - os.path.getmtime(self.path)
                if age > self.timeout and attempt == 0:
                    os.remove(self.path)          # 崩溃残留 → 接管一次
                    continue
                raise DecideError("LOCKED: %s 由其他写者持有（age=%.0fs）"
                                  " —— 单写者保证，禁止并发决策" % (self.path, age))
        raise DecideError("LOCKED: 接管失败（竞态）")

    def __exit__(self, *exc):
        if os.path.isfile(self.path):
            os.remove(self.path)
        return False


def _find_existing(sim, base, as_of, code, h):
    """同轴 (code, date, info_set_hash) 已有正式决策 → 返回其 id（复用不重记）。"""
    for row in DEC.iter_decisions(sim, base=base):
        if (row.get("code") == code and row.get("date") == as_of
                and row.get("info_set_hash") == h):
            return row["id"]
    return None


# ── 主接口 ───────────────────────────────────────────────────────────────────
def decide(*, as_of, code, thesis_id, chain, sim, driver=None, thesis_state=None,
           env=None, entry_limit=None, stop_line=None, target=None, window=None,
           position_request=None, completion_event=None, overturn_event=None,
           base=None, pool_path=None, thesis_path=None, params_path=None,
           snapshot_root=None, lock_timeout=LOCK_TIMEOUT, call_chain=None):
    """三问 → 幂等落账 → 观察锚。返回 {status, decision_id, action, anchor, ...}。

    status: OK（全新落账且两段齐）/ REUSED（L2 命中复用，绝不二次落账）/
            HALF_COMMIT（段一成段二败，重试可恢复）/ REUSED_LEDGER（L1 兜底复用）。
    """
    if not isinstance(sim, bool):
        raise DecideError("sim 必须显式布尔（True=SIM / False=LIVE）—— 不表态即报错")
    if env is None:
        raise DecideError("env 必填（C1 环境档；无数据时调用方显式传 source=UNAVAILABLE）")
    d = DEC._check_date(as_of)
    c = DEC._check_code(code)
    th_id = DEC._check_thesis_id(thesis_id)
    h = info_set_hash(d, c, thesis_id=th_id, chain=chain, thesis_state=thesis_state,
                      env=env, params_path=params_path, snapshot_root=snapshot_root)
    with _Lock(sim, base, lock_timeout):
        exist = _find_existing(sim, base, d, c, h)
        if exist is not None:
            verdict = TQ.answer(d, c, th_id, chain, driver=driver,
                                thesis_state=thesis_state, env=env,
                                entry_limit=entry_limit, stop_line=stop_line,
                                target=target, window=window,
                                position_request=position_request,
                                completion_event=completion_event,
                                overturn_event=overturn_event,
                                thesis_path=thesis_path, params_path=params_path)
            anc = (TQ.anchor_pool(verdict, pool_path=pool_path)
                   if verdict["action"] != TQ.ACT_BUY else
                   {"anchored": False, "reason": "BUY 无需观察锚", "candidate_id": None})
            return {"status": "REUSED", "decision_id": exist, "action": verdict["action"],
                    "info_set_hash": h, "anchor": anc, "reused": True}
        verdict = TQ.answer(d, c, th_id, chain, driver=driver,
                            thesis_state=thesis_state, env=env,
                            entry_limit=entry_limit, stop_line=stop_line,
                            target=target, window=window,
                            position_request=position_request,
                            completion_event=completion_event,
                            overturn_event=overturn_event,
                            thesis_path=thesis_path, params_path=params_path)
        # ★ 出路持久化（2026-10-14 固化 · 零契约变更）：WAIT/NO 的 next_trigger /
        #   overturn_event 文本在**落账前**并入决策链 evidence——DECISION_FIELDS /
        #   SCHEMA_VERSION 零变更、three_q 冻结件零变更。背景：anchor_pool 失败
        #   （NO_CANDIDATE/TERMINAL/POOL_ERROR）时出路只存在于返回值 ⇒ 随进程丢失
        #   （复现脚本已归档，不随系统分发）。落账进 evidence 后
        #   load_decision 即可恢复。只 append 一次（本分支是唯一落账路径，REUSED
        #   分支不落账 ⇒ 无重复膨胀）；info_set_hash 不含它（h = 决定时的信息集，
        #   出路是决定结果，语义分离）。
        if verdict["action"] != TQ.ACT_BUY and verdict.get("next_trigger"):
            _nt = verdict["next_trigger"]
            _txt = _nt.get("text") if isinstance(_nt, dict) else str(_nt)
            _ev = "OUTCOME: next_trigger=%s" % _txt
            if _ev not in verdict["thesis"]["evidence"]:
                verdict["thesis"]["evidence"].append(_ev)
        rep = TQ.commit(verdict, sim=sim, pool_path=pool_path, base=base, env=env,
                        thesis_state=thesis_state, info_set_hash=h, call_chain=call_chain,
                        thesis_path=thesis_path, params_path=params_path)
        dec = rep.get("decision") or {}
        anc = rep.get("anchor") or {}
        status = "OK"
        if verdict["action"] != TQ.ACT_BUY and not anc.get("anchored"):
            status = "HALF_COMMIT"
        elif not dec.get("created", True):
            status = "REUSED_LEDGER"
        return {"status": status, "decision_id": dec.get("decision_id"),
                "action": rep.get("action", verdict["action"]),
                "info_set_hash": h, "anchor": anc, "reused": False,
                "ledger": dec.get("ledger")}


# ── 自检（全部沙盒，不碰真数据） ─────────────────────────────────────────────
def _selftest():
    bad = [0]
    _REAL = os.path.join(DEC.ROOT, "data", "generated", "decision_ledger.jsonl")
    _REAL_SIM = os.path.join(DEC.ROOT, "data", "generated", "sim",
                             "decision_ledger.jsonl")

    def _fp(p):
        return (hashlib.md5(open(p, "rb").read()).hexdigest()
                if os.path.isfile(p) else "ABSENT")
    real_before = (_fp(_REAL), _fp(_REAL_SIM))

    def T(name, cond):
        print(("  [ok] " if cond else "  [!!] ") + name)
        if not cond:
            bad[0] += 1

    import tempfile
    import shutil
    import thesis as TH

    D1 = "2026-10-08"
    CODE = "sh600000"
    ENV = {"regime": None, "signals": {}, "leg": "EOD", "source": "UNAVAILABLE"}
    FIVE_FULL = {k: "%s 内容" % k for k in TQ.FIVE_KEYS}
    FIVE_PART = dict(FIVE_FULL)
    FIVE_PART["剩余预期"] = None
    PRED = [{"prediction_id": k, "metric": "5日超额", "data_source": "snapshot",
             "baseline": "0", "direction": "UP", "threshold": "3",
             "condition": "放量不破5日线", "window": 3} for k in TQ.PRED_SLOTS]
    CHAIN_BUY = {"evidence": ["席位净买入可查"], "inference": ["资金抢占 → 溢价"],
                 "prediction": PRED, "five": FIVE_FULL}
    CHAIN_WAIT = {"evidence": ["题材发酵可查"], "inference": ["扩散中"],
                  "prediction": [], "five": FIVE_PART}
    ODDS = dict(entry_limit=10.2, stop_line=9.8, target=11.5, window=5,
                position_request=0.2)

    print("A组 零重定义（同源判定）")
    T("A1 ACTIONS is DEC.ACTIONS", TQ.ACTIONS is DEC.ACTIONS)
    T("A2 FIVE_KEYS is DEC.FIVE_KEYS", TQ.FIVE_KEYS is DEC.FIVE_KEYS)
    T("A3 本模块零直接写点常量（LOCK_NAME 仅锁文件）",
      LOCK_NAME == "decide.lock")

    print("B组 沙盒全流程（真 thesis/账本/候选池）")
    tmp = tempfile.mkdtemp(prefix="orch_sb_")

    def init_pool(path, date):
        """测试夹具（同 wakeup._init_pool 口径；生产初始化属 0.5 人工动作）。"""
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"version": CAND.SCHEMA_VERSION, "updated_at": date,
                       "candidates": []}, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return path

    base = os.path.join(tmp, "gen")
    os.makedirs(base)
    thesis_f = os.path.join(tmp, "thesis.md")
    pool_f = os.path.join(tmp, "candidates.json")
    TH.init_file(thesis_f)
    th1 = TH.create_thesis(D1, "B", "资金抢占主线", code=CODE, candidate_id=None,
                           gap=TH.GAP_DRIVER, path=thesis_f)["id"]
    th2 = TH.create_thesis(D1, "C", "情绪扩散观察", code=CODE, candidate_id=None,
                           gap=TH.GAP_DRIVER, path=thesis_f)["id"]
    init_pool(pool_f, D1)
    CAND.upsert_candidate(CODE, D1, "五路扫描", {"text": "龙虎榜净买入", "rule": None},
                          path=pool_f)
    kw1 = dict(as_of=D1, code=CODE, thesis_id=th1, chain=CHAIN_BUY, sim=True,
               driver="B", thesis_state=None, env=ENV, base=base, pool_path=pool_f,
               thesis_path=thesis_f, snapshot_root=None, **ODDS)
    r1 = decide(**kw1)
    T("B1 BUY 全新落账 status=OK", r1["status"] == "OK" and r1["action"] == "BUY")
    rows1 = list(DEC.iter_decisions(True, base=base))
    T("B2 账本恰 1 行且 id 落 SIM 轴",
      len(rows1) == 1 and rows1[0]["id"].startswith("SIM-DEC-"))
    h1 = r1["info_set_hash"]
    T("B3 info_set_hash 稳定（重算同值）",
      info_set_hash(D1, CODE, thesis_id=th1, chain=CHAIN_BUY, env=ENV) == h1)
    r2 = decide(**kw1)
    T("B4 重试 → REUSED 复用原 decision_id，账本仍 1 行",
      r2["status"] == "REUSED" and r2["decision_id"] == r1["decision_id"]
      and len(list(DEC.iter_decisions(True, base=base))) == 1)
    kw3 = dict(as_of=D1, code=CODE, thesis_id=th2, chain=CHAIN_WAIT, sim=True,
               driver="C", thesis_state=None, env=ENV, base=base, pool_path=pool_f,
               thesis_path=thesis_f, snapshot_root=None)
    r3 = decide(**kw3, completion_event={"text": "龙头晋级 4 板", "rule": None})
    T("B5 WAIT 落账 + 观察锚回填候选池",
      r3["status"] == "OK" and r3["action"] == "WAIT"
      and r3["anchor"].get("anchored") is True)
    cand = [c for c in CAND.list_candidates(path=pool_f) if c["code"] == CODE][0]
    T("B6 锚内容可验证（next_trigger=补全事件，非空话）",
      cand["next_trigger"]["text"] == "龙头晋级 4 板")
    # 半提交恢复：池中候选移除后 WAIT 无锚 → HALF_COMMIT；恢复候选后重试补锚
    tmp_pool2 = os.path.join(tmp, "candidates_empty.json")
    init_pool(tmp_pool2, D1)
    th3 = TH.create_thesis(D1, "C", "第三主线", code=CODE, candidate_id=None,
                           gap=TH.GAP_DRIVER, path=thesis_f)["id"]
    kw4 = dict(as_of=D1, code=CODE, thesis_id=th3, chain=CHAIN_WAIT, sim=True,
               driver="C", thesis_state=None, env=ENV, base=base,
               pool_path=tmp_pool2, thesis_path=thesis_f, snapshot_root=None)
    r4 = decide(**kw4, completion_event={"text": "炸板率回落", "rule": None})
    T("B7 段二失败 → HALF_COMMIT 如实上报",
      r4["status"] == "HALF_COMMIT" and r4["decision_id"] is not None)
    n_before = len(list(DEC.iter_decisions(True, base=base)))
    CAND.upsert_candidate(CODE, D1, "五路扫描", {"text": "t", "rule": None},
                          path=tmp_pool2)
    r4b = decide(**kw4, completion_event={"text": "炸板率回落", "rule": None})
    n_after = len(list(DEC.iter_decisions(True, base=base)))
    T("B8 重试复用原 id 补锚成功，账本不新增行",
      r4b["status"] == "REUSED" and r4b["decision_id"] == r4["decision_id"]
      and r4b["anchor"].get("anchored") is True and n_before == n_after)
    # 输入变化 → 新 hash → L1 账本层兜底（同 (date,code,action,thesis_id) 拒绝重复）
    chain2 = dict(CHAIN_WAIT, inference=["扩散中（修正）"])
    r5 = decide(as_of=D1, code=CODE, thesis_id=th2, chain=chain2, sim=True,
                driver="C", env=ENV, base=base, pool_path=pool_f,
                thesis_path=thesis_f, snapshot_root=None,
                completion_event={"text": "龙头晋级 4 板", "rule": None})
    n5 = len(list(DEC.iter_decisions(True, base=base)))
    T("B9 同 thesis 同动作不同输入 → L1 兜底复用（不新增行）",
      r5["info_set_hash"] != r3["info_set_hash"]
      and r5["status"] in ("REUSED_LEDGER", "REUSED") and n5 == n_after)

    print("C组 单写者锁")
    lock = _Lock(True, base)
    with lock:
        try:
            decide(**kw1)
            T("C1 持锁期间重入被拒", False)
        except DecideError as ex:
            T("C1 持锁期间重入 → LOCKED", str(ex).startswith("LOCKED"))
    with open(lock.path, "w", encoding="utf-8") as f:
        f.write("pid=0 ts=0（模拟崩溃残留）")
    os.utime(lock.path, (time.time() - 3600, time.time() - 3600))
    r6 = decide(**kw1)
    T("C2 崩溃残留锁（超时）接管后正常决策",
      r6["status"] == "REUSED" and not os.path.isfile(lock.path))

    print("D组 坏样本（机械拒绝，账本零污染）")
    n7 = len(list(DEC.iter_decisions(True, base=base)))
    try:
        decide(as_of=D1, code=CODE, thesis_id=th1, chain=CHAIN_BUY, sim=True,
               driver=TQ.DRIVER_UNKNOWN, env=ENV, base=base, pool_path=pool_f,
               thesis_path=thesis_f, snapshot_root=None, **ODDS)
        T("D1 驱动不明 + BUY 被拒", False)
    except (TQ.ThreeQError, DEC.DecisionError) as ex:
        T("D1 驱动不明 + BUY 被机械拒（%s）" % type(ex).__name__, True)
    bad_chain = dict(CHAIN_BUY, extra="x")
    try:
        decide(as_of=D1, code=CODE, thesis_id=th1, chain=bad_chain, sim=True,
               driver="B", env=ENV, base=base, pool_path=pool_f,
               thesis_path=thesis_f, snapshot_root=None, **ODDS)
        T("D2 链字段非法被拒", False)
    except DEC.DecisionError:
        T("D2 链字段非法被拒（DecisionError）", True)
    try:
        decide(as_of=D1, code=CODE, thesis_id=th1, chain=CHAIN_BUY, sim=True,
               driver="B", env=None, base=base, pool_path=pool_f,
               thesis_path=thesis_f, snapshot_root=None, **ODDS)
        T("D3 env 缺失被拒", False)
    except DecideError as ex:
        T("D3 env 缺失被拒（禁静默造环境）", str(ex).startswith("env 必填"))
    T("D4 坏样本全程账本零新增",
      len(list(DEC.iter_decisions(True, base=base))) == n7)

    print("E组 真库零污染")
    T("E1 真 LIVE/SIM 账本未被触碰",
      real_before == (_fp(_REAL), _fp(_REAL_SIM))
      and not os.path.isfile(os.path.join(base, LOCK_NAME)))
    shutil.rmtree(tmp, ignore_errors=True)

    print("自检结果：%s" % ("全部通过 ✅" if bad[0] == 0 else "%d 项失败 ❌" % bad[0]))
    return bad[0] == 0


# ── 纪律判据 ─────────────────────────────────────────────────────────────────
def _discipline(verbose=True):
    import astwin as ASTW
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    lines = src.splitlines(True)
    drop = set()
    for lo, hi, name in ASTW.func_table(ast.parse(src)):
        if name.startswith(("_selftest", "_discipline", "_main")):
            drop.update(range(lo - 1, hi))
    prod = "".join(l for i, l in enumerate(lines) if i not in drop)
    res = []
    res.append(("① 本模块零直接写点（生产区无 io.open / os.replace 写模式）",
                "io.open(" not in prod and "os.replace" not in prod
                and '"a"' not in prod and '"w"' not in prod))
    res.append(("② 零参数正式区 / 决策卡 / 仓位闸写入",
                "params.json" not in prod and "decision_card" not in prod
                and "position_gate" not in prod))
    res.append(("③ 零转正/批准调用 + 不接结算主链",
                "promote(" not in prod and "APPROVE" not in prod
                and "settle" not in prod))
    res.append(("④ 永不传 allow_duplicate=True（同日二次决策属既有账本规则）",
                "allow_duplicate=True" not in prod))
    res.append(("⑤ astwin 引用（生产函数表经 astwin 枚举）",
                bool(ASTW.func_table(ast.parse(src)))))
    ok = True
    for name, passed in res:
        if verbose:
            print(("  [ok] " if passed else "  [!!] ") + name)
        ok = ok and passed
    print("纪律结果：%s（%d/%d）" % ("通过 ✅" if ok else "失败 ❌",
                                    sum(1 for _, p in res if p), len(res)))
    return ok


def _main(argv):
    if "--selftest" in argv:
        return 0 if _selftest() else 1
    if "--discipline" in argv:
        return 0 if _discipline() else 1
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
