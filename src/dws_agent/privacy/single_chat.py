"""Single-chat hard filter and message classification at ingestion.

Runs on the Executor side, deterministically, with NO LLM.

Hard constraint (default-deny for ingestion): a raw message becomes a Signal
ONLY IF both hold:
    1. conversationType == 'group'
    2. conversationId is in the allowed_groups set

Everything else -- single (1:1) chats, group chats not on the allow-list, or
messages missing the required fields -- is dropped and MUST NEVER enter the
signal pipeline. This prevents private/peripheral conversations from being
acted upon and contains accidental exposure.

On admission, the message text is redacted (privacy.redaction) and its taint
is computed/propagated (privacy.taint) before being exposed as a Signal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Set

from .redaction import redact
from .taint import propagate


@dataclass
class MessageClass:
    """Classification verdict for a raw message.

    Attributes:
        kind: 'signal' (admitted) or 'drop' (rejected).
        reason: human-readable explanation for the audit log.
    """

    kind: str  # 'signal' | 'drop'
    reason: str


@dataclass
class Signal:
    """An admitted message, ready for the signal pipeline.

    The raw ``text`` is retained for downstream processing but
    ``redacted_text`` is what should be logged/exported. ``taint`` reflects
    the strictest label implied by the content.
    """

    conversation_id: str
    sender_id: str
    text: str
    taint: str
    redacted_text: str
    refs: List[str] = field(default_factory=list)


def classify_message(msg: dict, allowed_groups: Set[str]) -> MessageClass:
    """Classify a raw message dict against the single-chat hard filter.

    Returns a :class:`MessageClass`. A message is admitted as a 'signal' only
    when conversationType=='group' AND conversationId is in *allowed_groups*.
    All other cases are 'drop' with a specific reason. Default-deny: anything
    unexpected (missing fields, unknown type) drops.
    """
    if not isinstance(msg, dict):
        return MessageClass(kind="drop", reason="not_a_dict")

    conv_type = msg.get("conversationType")
    conv_id = msg.get("conversationId")

    if conv_type != "group":
        # Includes single (1:1) chats and any non-group / missing type.
        return MessageClass(
            kind="drop",
            reason=f"non_group_conversation_type:{conv_type!r}",
        )

    if not conv_id:
        return MessageClass(kind="drop", reason="missing_conversation_id")

    if conv_id not in allowed_groups:
        return MessageClass(
            kind="drop",
            reason="group_not_in_allowed_groups",
        )

    return MessageClass(kind="signal", reason="group_allowed")


def _extract_text(msg: dict) -> str:
    """Best-effort plain-text extraction from a message dict."""
    text = msg.get("text")
    if isinstance(text, dict):
        text = text.get("content")
    if text is None:
        text = msg.get("content")
    return text if isinstance(text, str) else ""


def _extract_refs(msg: dict) -> List[str]:
    """Extract reference/quote-chain ids from a message dict, if present."""
    refs = msg.get("refs")
    if refs is None:
        refs = msg.get("references")
    if isinstance(refs, (list, tuple)):
        return [str(r) for r in refs if r is not None]
    if refs is None:
        return []
    return [str(refs)]


def to_signal(msg: dict, allowed_groups: Set[str]) -> Optional[Signal]:
    """Convert a raw message to a :class:`Signal`, or None if it is dropped.

    Applies the single-chat hard filter via :func:`classify_message`. For
    admitted messages, redacts the text and propagates taint:
    the Signal taint is the MAX of a baseline INTERNAL (group content is at
    least internal) and the taint implied by detected secrets/PII.
    """
    verdict = classify_message(msg, allowed_groups)
    if verdict.kind != "signal":
        return None

    raw_text = _extract_text(msg)
    result = redact(raw_text)
    # Group messages are at least INTERNAL; never wash below that, and never
    # below what the content itself implies.
    taint = propagate([result.max_taint], own="INTERNAL")

    return Signal(
        conversation_id=str(msg.get("conversationId")),
        sender_id=str(msg.get("senderId") or msg.get("senderStaffId") or ""),
        text=raw_text,
        taint=taint,
        redacted_text=result.text,
        refs=_extract_refs(msg),
    )
