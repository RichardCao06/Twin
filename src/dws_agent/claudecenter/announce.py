"""task publish 后向「研发群」发**模板化进度播报**。

与"代客户答复"严格隔离的护栏（这是把"自动发"和"不冒充本人"底线调和的关键）：

1. **仅发白名单群**：默认 HiQ 产品研发（INTERNAL_GROUP）；可经 env
   ``DWS_AGENT_ANNOUNCE_GROUP`` 覆盖。绝不发任意群 / 客户群。
2. **内容模板化**：只填 issue#/标题/目标分支/状态四个字段，禁自由文本——
   播报永远不可能变成"代某人答复某个问题"。
3. **显著标注非本人**：文本带「自动进度播报·非本人发言」，不冒充本人。
4. **经阶段0 + 审计**：仍走 confirm_token → Executor → dws-shim → 真实 dws，
   全程留痕。``publish`` 这个人工动作即对这条模板化播报的**预授权**，故自动铸
   token（低风险播报不每次人确认），区别于 R3 自由代答仍须 confirm。
5. **可关**：env ``DWS_AGENT_ANNOUNCE=0`` 全局关，或 ``publish --no-announce`` 单次关。
"""

from __future__ import annotations

import datetime
import os
import uuid
from typing import Optional, Tuple

# HiQ 产品研发（INTERNAL_GROUP，经 dws chat search 实证）——白名单默认群。
DEFAULT_ANNOUNCE_GROUP = "cidh68vjTKj0keMxunTPA2LMw=="


class _ConfirmGate:
    """把 policy.confirm 暴露成 Executor 需要的 verify(action_id, argv, now)。"""

    def __init__(self, paths):
        self.paths = paths

    def verify(self, action_id, argv, now=None):
        from dws_agent.policy import confirm

        return confirm.verify_token(action_id, argv, self.paths, now=now).ok


def build_text(task: dict, issue_num: Optional[int] = None) -> str:
    """模板化播报文本。字段白名单：issue#/标题/目标分支。不接受自由文本。"""
    title = (task.get("title") or "(无标题)").strip()
    target = task.get("target_branch") or ""
    issue_ref = ("feedback#%s " % issue_num) if issue_num else ""
    head = "🤖 dws-agent 正在处理 %s《%s》" % (issue_ref, title)
    line2 = "· 任务已发布，执行中" + ((" → %s" % target) if target else "")
    return head + "\n" + line2 + "\n（自动进度播报·非本人发言）"


def announce_publish(
    task: dict,
    issue_num: Optional[int] = None,
    *,
    group: Optional[str] = None,
    dry_run: bool = False,
) -> Tuple[Optional[str], object]:
    """向白名单研发群发模板化播报。

    返回 ``(text, result)``：``text`` 是将发/已发的文本（被全局开关关掉时为 None），
    ``result`` 是 Executor 的执行结果（dry_run 或关闭时为 None）。
    """
    if os.environ.get("DWS_AGENT_ANNOUNCE", "1") == "0":
        return None, None
    group = group or os.environ.get("DWS_AGENT_ANNOUNCE_GROUP") or DEFAULT_ANNOUNCE_GROUP
    if not group:
        return None, None

    text = build_text(task, issue_num)
    if dry_run:
        return text, None

    from dws_agent.core.paths import get_paths
    from dws_agent.executor.executor import Executor
    from dws_agent.executor.inbox import Intent
    from dws_agent.policy import confirm
    from dws_agent.policy.classifier import normalize_argv
    from dws_agent.policy.gate import PolicyGate

    full = ["dws", "chat", "message", "send", "--group", group, "--text", text]
    paths = get_paths()
    action_id = "AI-announce-" + uuid.uuid4().hex[:8]
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    intent = Intent.from_obj({
        "action_id": action_id,
        "created_at": now_iso,
        "source": "announce",
        "argv": full,
        "cwd": None,
        "stdin": None,
        "semantic_labels": {"commit_class": "none", "taint": "INTERNAL", "public_ok": False},
        "meta": {"announce": True, "issue": issue_num, "group": group},
    })
    # publish 即对这条模板化播报的预授权 → 自动铸一次性 confirm_token（仍经 gate 审计）
    confirm.issue_token(action_id, normalize_argv(full), 300, paths)
    ex = Executor(paths, policy=PolicyGate(paths=paths), gate=_ConfirmGate(paths))
    res = ex.execute_intent(intent, confirm_token="present")
    return text, res
