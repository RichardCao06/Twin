#!/usr/bin/env python3
"""MVP 代发：你确认后，经阶段0 安全链路真发一条 dws 消息。

链路（全部复用阶段0）：
  构造 ActionIntent(dws chat message send ...)
  → PolicyGate 判级（chat message send = 写 → HUMAN_CONFIRM）
  → confirm.issue_token（"你确认" = 铸一次性 token，绑 sha256(argv)+action_id+TTL）
  → Executor.execute_intent（验 token → mint 一次性 DWS_GATE_TOKEN）
  → dws-shim 子进程（独立重校验 gate token，token 经 env 隔离、不走 PATH）
  → 真实 dws chat message send。全程写审计。

需真实环境：DWS_AGENT_DWS_BIN 指向真实 dws、且**非** TEST_MODE。
用法：PYTHONPATH=src DWS_AGENT_DWS_BIN=$(command -v dws) \\
        python3 scripts/send_mvp.py --user <uid> --text "..."
"""
from __future__ import annotations

import argparse
import sys
import uuid

sys.path.insert(0, "src")

from dws_agent.core.paths import get_paths
from dws_agent.executor.executor import Executor
from dws_agent.executor.inbox import Intent
from dws_agent.policy import confirm
from dws_agent.policy.classifier import normalize_argv
from dws_agent.policy.gate import PolicyGate


class _ConfirmGate:
    """适配器：把 policy.confirm 暴露成 Executor 需要的 verify(action_id, argv, now)。"""

    def __init__(self, paths):
        self.paths = paths

    def verify(self, action_id, argv, now=None):
        return confirm.verify_token(action_id, argv, self.paths, now=now).ok


def main() -> int:
    ap = argparse.ArgumentParser(description="MVP 代发（经阶段0 安全链路）")
    ap.add_argument("--user")
    ap.add_argument("--group")
    ap.add_argument("--text", required=True)
    args = ap.parse_args()

    paths = get_paths()
    full = ["dws", "chat", "message", "send"]
    if args.user:
        full += ["--user", args.user]
    elif args.group:
        full += ["--group", args.group]
    else:
        print("需 --user 或 --group", file=sys.stderr)
        return 2
    full += ["--text", args.text]

    action_id = "AI-mvp-" + uuid.uuid4().hex[:8]
    intent = Intent.from_obj({
        "action_id": action_id,
        "created_at": "2026-06-23T00:00:00Z",
        "source": "mvp-send",
        "argv": full,
        "cwd": None,
        "stdin": None,
        "semantic_labels": {"commit_class": "none", "taint": "INTERNAL", "public_ok": False},
        "meta": {},
    })

    # "你确认" = 铸一次性 confirm_token（绑 normalized argv + action_id + TTL 300s）
    confirm.issue_token(action_id, normalize_argv(full), 300, paths)
    print("已铸 confirm_token；action_id=%s" % action_id)
    print("argv:", " ".join(full[:6]), "... --text <%d字>" % len(args.text))

    ex = Executor(paths, policy=PolicyGate(paths=paths), gate=_ConfirmGate(paths))
    res = ex.execute_intent(intent, confirm_token="present")
    print("判级/代发: level=%s decision=%s exit_code=%s" % (res.level, res.decision, res.exit_code))
    tail = (res.stdout_tail or "")[:800]
    print("dws 输出:\n%s" % tail)
    return 0 if res.exit_code == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
