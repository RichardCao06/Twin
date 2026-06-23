"""DwsReader — KDL 的 dws 只读封装（R0），按 2026-06-22 实测校准。

硬约束（结构性保证）：
- **只读**。只允许 :attr:`DwsReader.ALLOWED` 里的只读子命令前缀；任何写/发送/治理
  命令（send/create/update/delete/recall/auth ...）在调 dws **之前** raise
  PermissionError —— KDL 永不通过本类写或发送任何东西。
- 统一 `--format json` 解析；`doc read` 默认返回 Markdown 文本（非 JSON），原样透传。
- 返回字段映射见 ``docs/dws-只读接口校准.md``（命令/字段均为实测确认）。

本类只构造只读命令并解析返回；摄取/脱敏/落库由上层（doc_source/qa_source +
Ingestor）完成。
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Dict, List, Optional


class DwsReader:
    """硬只读的 dws 封装。非白名单子命令一律拒绝。"""

    #: 只读子命令路径白名单（命令前缀，不含 flags）。read-only by construction.
    ALLOWED = frozenset(
        {
            ("doc", "search"),
            ("doc", "list"),
            ("doc", "info"),
            ("doc", "read"),
            ("chat", "search"),
            ("chat", "message", "list"),
            ("chat", "message", "list-all"),
            ("chat", "message", "search-advanced"),
            ("chat", "message", "list-mentions"),
            ("contact", "user", "get-self"),
        }
    )

    def __init__(self, dws_bin: Optional[str] = None, audit: Any = None,
                 timeout: int = 60) -> None:
        self.dws_bin = dws_bin or os.environ.get("DWS_AGENT_DWS_BIN") or "dws"
        self._audit = audit
        self._timeout = timeout

    # -- guarded runner ----------------------------------------------------
    def _run(self, cmd_path: List[str], flags: Optional[Dict[str, Any]] = None) -> Any:
        """Run ``dws <cmd_path> <flags> --format json`` after whitelist check.

        Raises PermissionError if ``cmd_path`` is not (a prefix-extension of) a
        whitelisted read-only command — the single chokepoint for the no-write
        guarantee.
        """
        key = tuple(cmd_path)
        if not any(key == a or key[: len(a)] == a for a in self.ALLOWED):
            raise PermissionError(
                "dws subcommand '%s' not in KDL read-only whitelist" % " ".join(cmd_path)
            )
        argv = [self.dws_bin, *cmd_path]
        for k, v in (flags or {}).items():
            if v is None or v is False:
                continue
            if v is True:
                argv.append(k)
            else:
                argv += [k, str(v)]
        argv += ["--format", "json"]

        if self._audit is not None:
            try:
                self._audit.log(
                    {
                        "event": "cli",
                        "actor": "cli",
                        "decision": None,
                        "level": "R0",
                        "reason": "dws read",
                        "detail": {"kdl_op": "dws_read", "cmd": " ".join(cmd_path)},
                    }
                )
            except Exception:
                pass

        proc = subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=self._timeout
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "dws %s failed (rc=%d): %s"
                % (" ".join(cmd_path), proc.returncode, (proc.stderr or "")[:300])
            )
        out = (proc.stdout or "").strip()
        if not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return out  # doc read 默认 markdown 文本

    # -- identity ----------------------------------------------------------
    def self_uid(self) -> Optional[str]:
        """本人 userId（contact user get-self → orgEmployeeModel.userId）。"""
        d = self._run(["contact", "user", "get-self"])
        try:
            return d["result"][0]["orgEmployeeModel"]["userId"]
        except (KeyError, IndexError, TypeError):
            return None

    # -- documents ---------------------------------------------------------
    def doc_search(self, query: Optional[str] = None, creator_uids: Optional[str] = None,
                   extensions: Optional[str] = None, limit: int = 30,
                   cursor: Optional[str] = None) -> List[dict]:
        """返回 documents[]（nodeId/name/docUrl/contentType/extension/createTime/creatorUid）。"""
        d = self._run(
            ["doc", "search"],
            {"--query": query, "--creator-uids": creator_uids,
             "--extensions": extensions, "--limit": limit, "--cursor": cursor},
        )
        return d.get("documents", []) if isinstance(d, dict) else []

    def doc_search_all(self, query: Optional[str] = None, creator_uids: Optional[str] = None,
                       extensions: Optional[str] = None, max_pages: int = 30,
                       page_size: int = 30) -> List[dict]:
        """翻页聚合 doc search 的全部 documents（按 nextPageToken 翻到 hasMore=false）。"""
        out: List[dict] = []
        cursor: Optional[str] = None
        for _ in range(max_pages):
            d = self._run(
                ["doc", "search"],
                {"--query": query, "--creator-uids": creator_uids,
                 "--extensions": extensions, "--limit": page_size, "--cursor": cursor},
            )
            if not isinstance(d, dict):
                break
            out += d.get("documents", []) or []
            if not d.get("hasMore"):
                break
            cursor = d.get("nextPageToken")
            if not cursor:
                break
        return out

    def doc_info(self, node: str) -> dict:
        """文档元信息（contentType/extension 决定能否在线读）。"""
        d = self._run(["doc", "info"], {"--node": node})
        return d if isinstance(d, dict) else {}

    def doc_read_markdown(self, node: str) -> Optional[str]:
        """读文档正文（默认 Markdown 文本）。仅 contentType=ALIDOC+extension=adoc 适用。"""
        out = self._run(["doc", "read"], {"--node": node})
        if isinstance(out, str):
            return out
        if isinstance(out, dict):
            return out.get("content") or out.get("markdown")
        return None

    # -- messages（规范化展平）---------------------------------------------
    def chat_search_messages(self, query: Optional[str] = None, at_me: bool = False,
                             user: Optional[str] = None,
                             conversation_ids: Optional[str] = None,
                             start: Optional[str] = None, end: Optional[str] = None,
                             limit: int = 100, cursor: str = "0") -> dict:
        """search-advanced → 展平为统一消息列表。

        返回 ``{messages: [...], has_more, next_cursor}``，每条消息：
        ``{conversation_id, single_chat(bool), sender_id(senderOpenDingTalkId,
        反投毒稳定 account), sender_name(显示名,不可信), content, create_time, msg_id}``。
        """
        d = self._run(
            ["chat", "message", "search-advanced"],
            {"--query": query, "--at-me": at_me, "--user": user,
             "--conversation-ids": conversation_ids,
             "--start": start, "--end": end, "--limit": limit, "--cursor": cursor},
        )
        res = d.get("result", {}) if isinstance(d, dict) else {}
        flat: List[dict] = []
        for conv in res.get("conversationMessagesList", []) or []:
            conv_id = conv.get("openConversationId")
            single = bool(conv.get("singleChat"))
            for m in conv.get("messages", []) or []:
                flat.append(
                    {
                        "conversation_id": conv_id,
                        "single_chat": single,
                        "sender_id": m.get("senderOpenDingTalkId"),
                        "sender_name": m.get("sender"),
                        "content": m.get("content"),
                        "create_time": m.get("createTime"),
                        "msg_id": m.get("openMessageId"),
                    }
                )
        return {
            "messages": flat,
            "has_more": bool(res.get("hasMore", False)),
            "next_cursor": res.get("nextCursor"),
        }
