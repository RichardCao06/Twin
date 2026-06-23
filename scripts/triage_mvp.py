#!/usr/bin/env python3
"""MVP 分诊：读钉钉消息 → KDL 检索 → 组"拟答包"（供 Claude 会话内拟答）。

纯只读：只用 DwsReader 读消息 + KDL serve 检索，绝不发送、绝不调 dws 写。
拟答（理解+组织答复）由 Claude 在会话里完成；代发（你确认后）是另一步。

用法（需 PYTHONPATH=src）：
  python3 scripts/triage_mvp.py --query "ILCD 导出怎么做"      # 直接检索（模拟提问）
  python3 scripts/triage_mvp.py --mentions [--days 7]          # 读最近 @我 的消息
  python3 scripts/triage_mvp.py --group <openConversationId>   # 读某群最近消息

产出 /tmp/triage_pkg.json：每条 = {message, from, decision, kus[{title,body}]}，供拟答。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys

sys.path.insert(0, "src")

from dws_agent.kdl.cli import _enc_key, _load_paths, _open_conn
from dws_agent.kdl.dws_read import DwsReader
from dws_agent.kdl.retrieve import serve

OUT = "/tmp/triage_pkg.json"


def _iso(days_offset: int) -> str:
    d = datetime.datetime.now() - datetime.timedelta(days=days_offset)
    return d.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _val(x):
    return getattr(x, "value", x)


def main() -> int:
    ap = argparse.ArgumentParser(description="MVP 分诊：读消息→检索→组拟答包")
    ap.add_argument("--query", help="直接检索一个问题（模拟提问，不读消息）")
    ap.add_argument("--mentions", action="store_true", help="读最近 @我 的消息")
    ap.add_argument("--group", help="读某群（openConversationId）最近消息")
    ap.add_argument("--days", type=int, default=7, help="时间窗（天），默认 7")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    conn = _open_conn(_load_paths())
    key = _enc_key()
    reader = DwsReader()

    # 1) 收集要回应的消息
    items = []
    if args.query:
        items = [{"sender_name": "(模拟提问)", "content": args.query,
                  "single_chat": False, "conversation_id": None, "msg_id": None}]
    elif args.mentions:
        r = reader.chat_search_messages(at_me=True, start=_iso(args.days),
                                        end=_iso(-1), limit=args.limit)
        items = r["messages"]
    elif args.group:
        r = reader.chat_search_messages(conversation_ids=args.group, start=_iso(args.days),
                                        end=_iso(-1), limit=args.limit)
        items = r["messages"]
    else:
        print("需指定 --query / --mentions / --group", file=sys.stderr)
        return 2

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

    json.dump(pkg, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n拟答包 → %s（含命中知识 body，供 Claude 会话内拟答；ABSTAIN 的不编、转你处理）" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
