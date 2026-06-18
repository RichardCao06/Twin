"""Deterministic R-axis risk classifier (NO LLM).

Implements the classification_rules contract, in strict evaluation order:

  1. NEVER check  : (argv[0], argv[1]) in policy.never  => DENY, terminal, not
                    confirmable.
  2. R0 whitelist : normalized argv prefix-matches an entry => R0 => AUTO.
  3. rules        : longest-prefix match in policy.rules => that level.
  4. default-deny : otherwise => policy.defaults.deny_level (>= R2).

Hard constraints embedded here:
  * Judging NEVER looks at --yes / -y (stripped by normalize_argv).
  * argv[0] must be the dws binary; missing argv or argv[0] != dws => R2-class
    hard reject (handled by the gate; classifier guards against empty argv).
  * Unknown subcommands fall through to default-deny (need confirm).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .loader import Policy

# Decision vocabulary (R-axis, phase0).
AUTO = "AUTO"
HUMAN_CONFIRM = "HUMAN_CONFIRM"
DENY = "DENY"


@dataclass(frozen=True)
class Classification:
    """Result of classifying a normalized argv against a Policy.

    Attributes:
        level: "R0".."R3" or None when never-denied / invalid.
        decision: AUTO | HUMAN_CONFIRM | DENY.
        never: True if denied by the never list (terminal, not confirmable).
        reason: human-readable explanation for audit.
        matched_prefix: the prefix tokens that produced the match (or []).
    """

    level: str | None
    decision: str
    never: bool
    reason: str
    matched_prefix: list[str] = field(default_factory=list)


def _dws_basenames(dws_bin: str | None) -> set[str]:
    names = {"dws"}
    if dws_bin:
        names.add(os.path.basename(dws_bin))
        names.add(dws_bin)
    return names


def normalize_argv(argv: list[str], dws_bin: str | None = None) -> list[str]:
    """Canonicalize argv for judging.

    Steps (per confirm_token contract step 1):
      * Drop argv[0] if it is the dws binary path/name.
      * Strip any token exactly '--yes' or '-y' (judging never looks at --yes).
      * Preserve order of all remaining tokens; no other reordering.

    Args:
        argv: full command-line token list (argv[0] should be the dws binary).
        dws_bin: optional path to the dws binary, used to recognize argv[0].

    Returns:
        The canonical token list (dws binary + --yes/-y removed).
    """
    if not argv:
        return []
    tokens = list(argv)
    names = _dws_basenames(dws_bin)
    if tokens and (tokens[0] in names or os.path.basename(str(tokens[0])) in names):
        tokens = tokens[1:]
    return [t for t in tokens if t not in ("--yes", "-y")]


def _level_to_decision(level: str, policy: Policy) -> str:
    spec = policy.levels.get(level, {})
    if spec.get("auto"):
        return AUTO
    return HUMAN_CONFIRM


def _matches_never(normalized: list[str], policy: Policy) -> tuple[str, str] | None:
    if len(normalized) < 2:
        return None
    head = (normalized[0], normalized[1])
    for cmd, subcmd in policy.never:
        if head == (cmd, subcmd):
            return (cmd, subcmd)
    return None


def _prefix_match(normalized: list[str], prefix: list[str]) -> bool:
    if len(prefix) > len(normalized):
        return False
    return normalized[: len(prefix)] == prefix


def classify(argv: list[str], policy: Policy, dws_bin: str | None = None) -> Classification:
    """Classify a command per the deterministic R-axis rules.

    Args:
        argv: full or already-normalized argv. It is normalized internally, so
            passing either form is safe (idempotent for normalized input that
            does not start with the dws binary name).
        policy: a loaded :class:`Policy`.
        dws_bin: optional dws binary path for argv[0] recognition.

    Returns:
        A :class:`Classification`.
    """
    normalized = normalize_argv(argv, dws_bin)

    if not normalized:
        # Empty after normalization => no subcommand; treat as default-deny.
        return Classification(
            level=policy.deny_level,
            decision=_level_to_decision(policy.deny_level, policy),
            never=False,
            reason="empty argv after normalization => default-deny",
            matched_prefix=[],
        )

    # 1. NEVER (terminal).
    hit = _matches_never(normalized, policy)
    if hit is not None:
        return Classification(
            level=None,
            decision=DENY,
            never=True,
            reason=f"never list: {hit[0]} {hit[1]} is always denied (not confirmable)",
            matched_prefix=[hit[0], hit[1]],
        )

    # 2. R0 whitelist (prefix match). Longest matching whitelist prefix wins.
    best_wl: list[str] | None = None
    for entry in policy.r0_whitelist:
        if _prefix_match(normalized, entry) and (
            best_wl is None or len(entry) > len(best_wl)
        ):
            best_wl = entry
    if best_wl is not None:
        return Classification(
            level="R0",
            decision=AUTO,
            never=False,
            reason=f"R0 whitelist match: {' '.join(best_wl)}",
            matched_prefix=list(best_wl),
        )

    # 3. rules: longest-prefix match wins.
    best_rule: dict | None = None
    best_len = -1
    for rule in policy.rules:
        prefix = rule["prefix"]
        if _prefix_match(normalized, prefix) and len(prefix) > best_len:
            best_rule = rule
            best_len = len(prefix)
    if best_rule is not None:
        level = best_rule["level"]
        return Classification(
            level=level,
            decision=_level_to_decision(level, policy),
            never=False,
            reason=f"rule match: prefix {' '.join(best_rule['prefix'])} => {level}",
            matched_prefix=list(best_rule["prefix"]),
        )

    # 4. default-deny.
    level = policy.deny_level
    return Classification(
        level=level,
        decision=_level_to_decision(level, policy),
        never=False,
        reason=f"no match => default-deny ({level})",
        matched_prefix=[],
    )
