"""Taint label model and propagation.

Three ordered labels: CLEAN < INTERNAL < SENSITIVE.

Hard rule: taint never washes down. Any datum derived from upstream data
inherits the MAX (strictest) taint of all its inputs. This mirrors the
"strictest-wins" hard constraint used by the policy side: labels may only
make handling stricter, never more permissive.
"""
from __future__ import annotations

from typing import Iterable

# Ordered from least to most sensitive. Index = severity rank.
TAINT_ORDER = ["CLEAN", "INTERNAL", "SENSITIVE"]

_RANK = {label: i for i, label in enumerate(TAINT_ORDER)}

# Anything we do not recognise is treated as the strictest label, so an
# unknown/garbage label can never accidentally relax handling (default-deny
# applied to taint).
_UNKNOWN_RANK = len(TAINT_ORDER) - 1
_UNKNOWN_LABEL = TAINT_ORDER[-1]


def _rank(label: str) -> int:
    """Return the severity rank of *label*; unknown labels rank as strictest."""
    return _RANK.get(label, _UNKNOWN_RANK)


def max_taint(*labels: str) -> str:
    """Return the strictest (maximum) taint among *labels*.

    With no arguments returns CLEAN. Unknown labels are treated as the
    strictest known label so they can never relax the result.
    """
    if not labels:
        return "CLEAN"
    best = "CLEAN"
    best_rank = -1
    for label in labels:
        r = _rank(label)
        if r > best_rank:
            best_rank = r
            # Map the unknown rank back to a concrete known label.
            best = label if label in _RANK else _UNKNOWN_LABEL
    return best


def propagate(upstream: Iterable[str], own: str = "CLEAN") -> str:
    """Compute the taint of derived data.

    The result is the MAX of *own* and every label in *upstream*. Derived
    data can only be as clean as its dirtiest input; it never washes down.
    """
    labels = [own]
    labels.extend(upstream)
    return max_taint(*labels)


def is_external_safe(label: str) -> bool:
    """True only if *label* is CLEAN, i.e. safe to send outside the org.

    INTERNAL and SENSITIVE (and any unknown label) are not external-safe.
    """
    return label == "CLEAN"
