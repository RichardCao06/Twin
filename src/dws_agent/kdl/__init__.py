"""KDL — Knowledge Distillation Layer (§2.1).

The KDL is the digital twin's long-term memory and answer-knowledge base. It does
exactly three things: ingest -> distill -> serve-for-retrieval.

HARD CONSTRAINTS (enforced throughout the package):
  * The KDL is strictly READ-ONLY with respect to the outside world.
  * It NEVER sends anything outward and NEVER issues a dws write command.
  * It produces no outward replies — only retrieval results (Verdict) and a
    local "if-I-answered" preview (DraftPreview) for the operator.

This module re-exports the core data model for convenience.
"""
from __future__ import annotations

from .model import (
    Authority,
    Citation,
    DraftPreview,
    Freshness,
    KnowledgeUnit,
    Provenance,
    ProvKind,
    SourceType,
    Taint,
    Verdict,
    VerdictDecision,
)

__all__ = [
    "KnowledgeUnit",
    "Provenance",
    "Verdict",
    "Citation",
    "SourceType",
    "Authority",
    "Taint",
    "Freshness",
    "ProvKind",
    "VerdictDecision",
    "DraftPreview",
]
