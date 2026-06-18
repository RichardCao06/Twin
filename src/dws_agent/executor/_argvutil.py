"""Shared argv normalization + hashing used by executor and shim.

These mirror the global ``confirm_token`` contract exactly. They are duplicated
here (small, pure) so the executor subpackage remains importable and unit
testable even before the canonical ``policy.confirm`` / ``crypto`` modules land.
When those modules exist, callers should prefer them; this stays as a
contract-faithful fallback.
"""

from __future__ import annotations

import hashlib
import os

# Token names that judging must NEVER look at.
_YES_FLAGS = {"--yes", "-y"}

_DWS_NAMES = {"dws"}


def normalize_argv(argv: list[str]) -> list[str]:
    """Canonicalize an argv per the contract.

    1. Drop argv[0] if it is the dws binary path/name (``dws`` or a path whose
       basename is ``dws``).
    2. Strip any token that is exactly ``--yes`` or ``-y`` (judging never looks
       at --yes).
    3. Keep all other tokens in order; no other reordering.
    """
    if not argv:
        return []
    tokens = list(argv)
    first = tokens[0]
    if first in _DWS_NAMES or os.path.basename(first) == "dws":
        tokens = tokens[1:]
    return [t for t in tokens if t not in _YES_FLAGS]


def argv_norm_sha256(argv: list[str]) -> str:
    """Return sha256 hex of the newline-joined normalized argv (contract)."""
    canon = "\n".join(normalize_argv(argv))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
