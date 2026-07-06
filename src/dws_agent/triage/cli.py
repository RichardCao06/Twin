"""分诊 · Triage 命令组：``dws-agent triage`` / ``dws-agent send``。

把原 ``scripts/triage_mvp.py`` / ``scripts/send_mvp.py`` 提升为正式子命令，
串起分诊代答闭环（手动触发）：

    dws-agent triage   →   [Claude 会话内拟答]   →   你确认   →   dws-agent send
       读消息+检索+出包      带相关性判断/ABSTAIN                 经阶段0安全代发

底线（见 docs/design/md/方案-MVP.md）：
- triage 纯只读：只用 DwsReader 读 + KDL serve 检索，绝不发送、绝不调 dws 写。
- send 默认**只预览不发**；加 ``--confirm`` 才经阶段0（confirm_token →
  Executor → dws-shim → 真实 dws）真发。绝不自动发、不冒充本人（提醒署名）。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import uuid
from pathlib import Path


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _iso(days_offset: int) -> str:
    """ISO 时间戳（本地 +08:00），用作消息搜索的时间窗端点。"""
    d = datetime.datetime.now() - datetime.timedelta(days=days_offset)
    return d.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _val(x):
    """枚举取 .value，其余原样返回。"""
    return getattr(x, "value", x)


def _triage_pkg_path(paths) -> Path:
    """拟答包落盘位置（运行时状态，非 /tmp）。"""
    return paths.state_dir / "triage_pkg.json"


# --------------------------------------------------------------------------- #
# triage —— 读消息 → 检索 → 出拟答包（只读）
# --------------------------------------------------------------------------- #
def cmd_triage(args) -> int:
    from dws_agent.core.paths import get_paths
    from dws_agent.kdl.cli import _enc_key, _load_paths, _open_conn
    from dws_agent.kdl.dws_read import DwsReader
    from dws_agent.kdl.retrieve import serve

    conn = _open_conn(_load_paths())
    key = _enc_key()

    # 1) 收集要回应的消息（--query 是纯本地检索，不读消息、不实例化 DwsReader）
    if args.query:
        items = [{"sender_name": "(模拟提问)", "content": args.query,
                  "single_chat": False, "conversation_id": None, "msg_id": None}]
    elif args.mentions:
        r = DwsReader().chat_search_messages(at_me=True, start=_iso(args.days),
                                             end=_iso(-1), limit=args.limit)
        items = r["messages"]
    else:  # args.group
        r = DwsReader().chat_search_messages(conversation_ids=args.group, start=_iso(args.days),
                                             end=_iso(-1), limit=args.limit)
        items = r["messages"]

    items = [m for m in items if (m.get("content") or "").strip()]
    if not items:
        print("窗口内没有要回应的消息。")
        return 0

    # 2) 逐条 KDL 检索 → 组拟答包
    print("=== 待拟答（%d 条）===" % len(items))
    pkg = []
    for m in items:
        content = m["content"]
        v = serve(conn, key, content)
        dec = _val(getattr(v, "decision", None))
        cits = getattr(v, "citations", []) or []
        kus = getattr(v, "kus", []) or []
        print("\n--- 消息（来自 %s，单聊=%s）---" % (m.get("sender_name"), m.get("single_chat")))
        print("  内容: %s" % content[:160].replace("\n", " "))
        print("  检索: %s/%s | 命中 %d 条" % (dec, getattr(v, "reason", None), len(cits)))
        for c in cits[:3]:
            print("    - [%s/%s/%s] %s:%s score=%.3f"
                  % (_val(getattr(c, "source_type", "")), _val(getattr(c, "authority", "")),
                     _val(getattr(c, "freshness", "")), _val(getattr(c, "prov_kind", "")),
                     getattr(c, "prov_ref", ""), getattr(c, "score", 0) or 0))
        pkg.append({
            "from": m.get("sender_name"),
            "single_chat": m.get("single_chat"),
            "conversation_id": m.get("conversation_id"),
            "msg_id": m.get("msg_id"),
            "message": content,
            "decision": dec,
            "kus": [{"title": getattr(k, "title", ""), "body": getattr(k, "body", "")}
                    for k in kus[:5]],
        })

    out = _triage_pkg_path(get_paths())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pkg, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n拟答包 → %s" % out)
    print("（含命中知识 body，供 Claude 会话内拟答；ABSTAIN 的不编、转你处理）")
    return 0


# --------------------------------------------------------------------------- #
# send —— 你确认后经阶段0 代发（默认仅预览）
# --------------------------------------------------------------------------- #
class _ConfirmGate:
    """适配器：把 policy.confirm 暴露成 Executor 需要的 verify(action_id, argv, now)。

    分诊代答里"你敲 send --confirm"即视为你的确认：铸一次性 confirm_token，再由
    Executor 验证后 mint 一次性 gate token 交给 shim。验证的是**安全链路**，
    真正的人意是"你决定执行这条命令"。
    """

    def __init__(self, paths):
        self.paths = paths

    def verify(self, action_id, argv, now=None):
        from dws_agent.policy import confirm

        return confirm.verify_token(action_id, argv, self.paths, now=now).ok


def _build_send_argv(args):
    """构造对外发送的完整 argv（argv[0]=='dws'）。缺目标返回 None。"""
    full = ["dws", "chat", "message", "send"]
    if args.user:
        full += ["--user", args.user]
    elif args.group:
        full += ["--group", args.group]
    else:
        return None
    full += ["--text", args.text]
    return full


def _classify_level(full, paths) -> str:
    """对 argv 判级（预览也判级），失败回退 'R?'。"""
    from dws_agent.policy.classifier import classify, normalize_argv
    from dws_agent.policy.loader import load_policy

    policy = None
    try:
        policy = load_policy(str(paths.policy_dir))
    except Exception:
        try:
            policy = load_policy()
        except Exception:
            policy = None
    if policy is None:
        return "R?"
    try:
        return str(_val(getattr(classify(normalize_argv(full), policy), "level", "R?")))
    except Exception:
        return "R?"


def cmd_send(args) -> int:
    from dws_agent.core.paths import get_paths

    full = _build_send_argv(args)
    if full is None:
        print("需 --user 或 --group", file=sys.stderr)
        return 2

    paths = get_paths()
    level = _classify_level(full, paths)
    target = ("--user %s" % args.user) if args.user else ("--group %s" % args.group)

    # ---- 预览（无论是否 --confirm 都先打印，便于你核对）----
    print("=== 代发预览 ===")
    print("  目标: %s" % target)
    print("  判级: %s（chat message send = 对外写，需你确认）" % level)
    print("  正文（%d 字）:" % len(args.text))
    for line in (args.text.splitlines() or [args.text]):
        print("    | %s" % line)
    if "助理" not in args.text and "代答" not in args.text:
        print("  ⚠ 正文未见'助理代答'类署名 —— 底线是不冒充本人，建议带署名。")

    if not args.confirm:
        print("\n仅预览，未发送。确认无误后加 --confirm 真发。")
        return 0

    # ---- --confirm：经阶段0 安全链路真发 ----
    if os.environ.get("DWS_AGENT_TEST_MODE") == "1":
        print("\nTEST_MODE=1：dws-shim 会拒绝真实 dws，不会真发。"
              "去掉 DWS_AGENT_TEST_MODE 再发。", file=sys.stderr)
        return 1

    from dws_agent.executor.executor import Executor
    from dws_agent.executor.inbox import Intent
    from dws_agent.policy import confirm
    from dws_agent.policy.classifier import normalize_argv
    from dws_agent.policy.gate import PolicyGate

    action_id = "AI-mvp-" + uuid.uuid4().hex[:8]
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    intent = Intent.from_obj({
        "action_id": action_id,
        "created_at": now_iso,
        "source": "mvp-send",
        "argv": full,
        "cwd": None,
        "stdin": None,
        "semantic_labels": {"commit_class": "none", "taint": "INTERNAL", "public_ok": False},
        "meta": {},
    })

    # "你确认" = 铸一次性 confirm_token（绑 normalized argv + action_id + TTL 300s）
    confirm.issue_token(action_id, normalize_argv(full), 300, paths)
    print("\n已铸 confirm_token；action_id=%s，经阶段0 代发中…" % action_id)

    ex = Executor(paths, policy=PolicyGate(paths=paths), gate=_ConfirmGate(paths))
    res = ex.execute_intent(intent, confirm_token="present")
    print("判级/代发: level=%s decision=%s exit_code=%s"
          % (res.level, res.decision, res.exit_code))
    tail = (res.stdout_tail or "")[:800]
    if tail:
        print("dws 输出:\n%s" % tail)
    return 0 if res.exit_code == 0 else 1


# --------------------------------------------------------------------------- #
# argparse wiring — register_triage(subparsers)
# --------------------------------------------------------------------------- #
def register_triage(subparsers) -> None:
    """把分诊 · Triage 命令组（triage/send）挂到 ``dws-agent`` 的 add_subparsers 上。

    由 ``cli.main._build_parser`` 懒加载、非致命地调用（缺这个包时主 CLI 仍可用）。
    """
    p_tri = subparsers.add_parser(
        "triage",
        help="分诊：读消息→KDL 检索→出拟答包（只读，绝不发送）",
        description=("读消息 + 检索知识库，组装拟答包供 Claude 会话内拟答。"
                     "纯只读：绝不发送、绝不调 dws 写命令。"),
    )
    g = p_tri.add_mutually_exclusive_group(required=True)
    g.add_argument("--query", help="直接检索一个问题（模拟提问，不读消息）")
    g.add_argument("--mentions", action="store_true", help="读最近 @我 的消息")
    g.add_argument("--group", help="读某群（openConversationId）最近消息")
    p_tri.add_argument("--days", type=int, default=7, help="时间窗（天），默认 7")
    p_tri.add_argument("--limit", type=int, default=5, help="最多取几条消息")
    p_tri.set_defaults(func=cmd_triage)

    p_snd = subparsers.add_parser(
        "send",
        help="代发：你确认后经阶段0 代发（默认仅预览，--confirm 才真发）",
        description=("经阶段0（confirm_token → Executor → dws-shim → 真实 dws）代发。"
                     "默认只预览不发；加 --confirm 才真发。绝不自动发。"),
    )
    g2 = p_snd.add_mutually_exclusive_group(required=True)
    g2.add_argument("--user", help="收件人 userId（私聊）")
    g2.add_argument("--group", help="群 openConversationId")
    p_snd.add_argument("--text", required=True,
                       help="正文（建议带'助理代答·待本人复核'署名）")
    p_snd.add_argument("--confirm", action="store_true",
                       help="真发（不加则仅预览）")
    p_snd.set_defaults(func=cmd_send)
