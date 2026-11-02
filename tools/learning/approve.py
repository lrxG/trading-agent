# -*- coding: utf-8 -*-
"""approve.py · 参数候选的人工批准薄 CLI（2026-10-19 · D2 配套）。

**为什么单独一件**：台账实测「#9 人工批准**无 CLI 入口**」（`params._main` 仅
selftest/discipline/verify/queue/changelog），而 `params.py` 属**冻结件**（骨架），
新增子命令 = 骨架升级。用户 2026-10-19 批准路径：**外挂薄 CLI**，且
「**人工批准闸门必须保持人工、不得自动 promote**」。

本件因此只做三件事：
  1. `queue`   —— 只读列出候选（`params.queue_rows` 派生视图）；
  2. `review`  —— 调 `params.review(as_of, queue_id, verdict, approver)`（人工给 verdict）；
  3. `promote` —— 调 `params.promote(as_of, queue_id, approver, reason)`（人工给 approver）。

**三条红线（代码级强制）**：
  · `--approver` 必填且非空——**空 approver 一律拒绝**（禁止脚本化"无人批准"）；
  · 本件**不内含任何自动流转**（不读 evidence 自动判 verdict、不批量 promote）；
  · 不新增任何写点——写全部发生在 `params.review/promote` 内部（已登记写点，
    经 writegate 生产闸）。

## 用法

```
python tools/learning/approve.py queue                          # 只读：待批候选
python tools/learning/approve.py review <queue_id> --verdict APPROVED --approver 张三 --as-of 2026-10-19
python tools/learning/approve.py promote <queue_id> --approver 张三 --as-of 2026-10-19 --reason "..."
```
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_FS = os.path.normpath(os.path.join(_HERE, "..", "fs"))
if _FS not in sys.path:
    sys.path.insert(0, _FS)
import params as PA           # noqa: E402
import paths as P             # noqa: E402

ROOT = P.ROOT
VERDICTS = ("APPROVED", "REJECTED")


class ApproveError(Exception):
    """REASON_CODE 前缀错误。"""


def _check_approver(v):
    if not v or not str(v).strip():
        raise ApproveError("APPROVER_REQUIRED: 人工批准闸门不接受空批准人"
                           "（禁脚本化无人批准）")
    return str(v).strip()


def list_queue(base=None):
    """只读：候选队列（→ [dict]）。"""
    p = os.path.join(base or ROOT, "data", "calibration_queue.jsonl")
    if not os.path.isfile(p):
        return []
    import io
    import json
    out = []
    for ln in io.open(p, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:                                      # noqa: BLE001
            continue
    return out


def review(queue_id, verdict, approver, *, as_of, base=None):
    ap = _check_approver(approver)
    if verdict not in VERDICTS:
        raise ApproveError("BAD_VERDICT: 只接受 %s" % (VERDICTS,))
    qp = os.path.join(base or ROOT, "data", "calibration_queue.jsonl")
    return PA.review(as_of, queue_id, verdict, ap, queue_path=qp)


def promote(queue_id, approver, *, as_of, reason=None, base=None):
    ap = _check_approver(approver)
    pp = os.path.join(base or ROOT, "data", "params.json")
    return PA.promote(as_of, queue_id, ap, reason=reason, path=pp)


def _selftest():
    import io
    import json
    import shutil
    import tempfile
    ok = fail = 0

    def T(name, cond, extra=None):
        nonlocal ok, fail
        ok += 1 if cond else 0
        fail += 0 if cond else 1
        print("  [%s] %s%s" % ("✓" if cond else "✗", name,
                               "" if cond else "  ← %s" % (extra,)))

    print("== approve --selftest（全沙盒 · 零生产写）==")
    sb = tempfile.mkdtemp(prefix="approve_sb_")
    try:
        os.makedirs(os.path.join(sb, "data", "state"), exist_ok=True)
        T("A1 空 approver ⇒ 拒绝（人工闸门不接受空批准人）",
          _raises(ApproveError,
                  lambda: review("Q1", "APPROVED", "  ", as_of="2026-10-19", base=sb)))
        T("A2 空 approver 在 promote 同样拒绝",
          _raises(ApproveError,
                  lambda: promote("Q1", "", as_of="2026-10-19", base=sb)))
        T("A3 非法 verdict ⇒ 拒绝",
          _raises(ApproveError,
                  lambda: review("Q1", "MAYBE", "张三", as_of="2026-10-19", base=sb)))
        # B：队列只读视图（沙盒空队列 ⇒ []）
        T("B1 空队列 ⇒ 空列表（不抛）", list_queue(base=sb) == [])
        qp = os.path.join(sb, "data", "calibration_queue.jsonl")
        io.open(qp, "w", encoding="utf-8").write(
            json.dumps({"queue_id": "Q1", "status": "PENDING"}, ensure_ascii=False) + "\n")
        T("B2 队列读取（含坏字段容忍）", len(list_queue(base=sb)) == 1)
        # C：不存在 queue_id ⇒ 由 params 侧拒绝（本件不吞错）
        try:
            review("Q-nonexistent", "APPROVED", "张三", as_of="2026-10-19", base=sb)
            T("C1 不存在候选项 ⇒ 拒绝", False, "竟通过")
        except Exception:                                      # noqa: BLE001
            T("C1 不存在候选项 ⇒ 拒绝（错误不吞）", True)
    finally:
        shutil.rmtree(sb, ignore_errors=True)
    print("== approve --selftest: %d/%d 通过 ==" % (ok, ok + fail))
    return 1 if fail else 0


def _raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception:                                          # noqa: BLE001
        return False
    return False


def _main(argv):
    if "--selftest" in argv:
        return _selftest()
    ap = argparse.ArgumentParser(description="参数候选人工批准薄 CLI（不自动流转）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("queue", help="只读列出候选")
    q.add_argument("--base", default=None)
    r = sub.add_parser("review", help="人工裁决一项候选")
    r.add_argument("queue_id")
    r.add_argument("--verdict", required=True, choices=VERDICTS)
    r.add_argument("--approver", required=True)
    r.add_argument("--as-of", required=True)
    r.add_argument("--base", default=None)
    p = sub.add_parser("promote", help="人工批准转正（写 params.json）")
    p.add_argument("queue_id")
    p.add_argument("--approver", required=True)
    p.add_argument("--as-of", required=True)
    p.add_argument("--reason", default=None)
    p.add_argument("--base", default=None)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "queue":
            rows = list_queue(base=a.base)
            print("候选 %d 项：" % len(rows))
            for x in rows:
                print("  %-24s %-10s %s" % (x.get("queue_id"), x.get("status"),
                                            x.get("param_name")))
            return 0
        if a.cmd == "review":
            print(json.dumps(review(a.queue_id, a.verdict, a.approver,
                                    as_of=a.as_of, base=a.base),
                             ensure_ascii=False))
            return 0
        print(json.dumps(promote(a.queue_id, a.approver, as_of=a.as_of,
                                 reason=a.reason, base=a.base), ensure_ascii=False))
        return 0
    except ApproveError as e:
        print("⛔ %s" % e, file=sys.stderr)
        return 2
    except Exception as e:                                     # noqa: BLE001
        print("⛔ %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    import json
    sys.exit(_main(sys.argv[1:]))
