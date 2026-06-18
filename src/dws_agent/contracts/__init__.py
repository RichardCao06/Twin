"""Shared contract artifacts (JSON schemas).

Holds the canonical JSON schema files so every module validates against one
copy. Currently owns ``action_intent.schema.json``.
"""

from __future__ import annotations

from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parent
ACTION_INTENT_SCHEMA = CONTRACTS_DIR / "action_intent.schema.json"

__all__ = ["CONTRACTS_DIR", "ACTION_INTENT_SCHEMA"]
