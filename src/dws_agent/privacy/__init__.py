"""privacy subpackage.

Provides secret/PII redaction (entropy + regex), taint propagation, and the
single-chat hard filter used at ingestion. No LLM, stdlib-first.
"""
from .redaction import redact, shannon_entropy, RedactResult, Hit, PATTERNS, ENTROPY_THRESHOLD
from .taint import TAINT_ORDER, max_taint, propagate, is_external_safe
from .single_chat import classify_message, to_signal, Signal, MessageClass

__all__ = [
    "redact",
    "shannon_entropy",
    "RedactResult",
    "Hit",
    "PATTERNS",
    "ENTROPY_THRESHOLD",
    "TAINT_ORDER",
    "max_taint",
    "propagate",
    "is_external_safe",
    "classify_message",
    "to_signal",
    "Signal",
    "MessageClass",
]
