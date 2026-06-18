"""KDL threshold/weight settings resolution from environment.

Mirrors :mod:`dws_agent.core.config` style: a single place that reads
``DWS_AGENT_KDL_*`` environment variables and falls back to documented
defaults. Per design §3.6 (进化只落数据) all tunables live as data here,
never hard-coded into algorithm bodies.

Scoring weights (``w_fresh``/``w_auth``/``w_rel``) and abstain thresholds are
read once via :func:`kdl_settings`; retrieval/serve code receives an immutable
:class:`KdlSettings` snapshot so behaviour is reproducible within a process.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Documented defaults (global contract env_vars). Keep in sync with the
# contract; these are the floor used when env is unset/garbage.
DEFAULT_W_FRESH = 0.3
DEFAULT_W_AUTH = 0.3
DEFAULT_W_REL = 0.4
DEFAULT_ABSTAIN_REL_MIN = 0.35
DEFAULT_ABSTAIN_LEX_MIN = 0.15
DEFAULT_CONF_FLOOR = 0.45
DEFAULT_COVERAGE_MIN = 0.6
DEFAULT_TOP_N = 5
DEFAULT_DISTILLER = "stub"


@dataclass(frozen=True)
class KdlSettings:
    """Resolved KDL tunables (weights + abstain thresholds + retrieval knobs).

    Attributes:
        w_fresh:        weight of the freshness component in the fused score.
        w_auth:         weight of the authority component in the fused score.
        w_rel:          weight of the relevance component in the fused score.
        abstain_rel_min: fused-relevance floor for the low-relevance gate.
        abstain_lex_min: lexical token-overlap floor for the low-relevance gate.
        conf_floor:     fused-confidence floor below which (with no
                        AUTHORITATIVE/REVIEWED hit) serve abstains.
        coverage_min:   minimum draft/citation coverage ratio (grounding gate).
        top_n:          number of candidates surfaced / kept in a Verdict.
        distiller:      pluggable distiller backend selector ('stub' only in
                        phase1 — deterministic, offline, no LLM/network).
    """

    w_fresh: float = DEFAULT_W_FRESH
    w_auth: float = DEFAULT_W_AUTH
    w_rel: float = DEFAULT_W_REL
    abstain_rel_min: float = DEFAULT_ABSTAIN_REL_MIN
    abstain_lex_min: float = DEFAULT_ABSTAIN_LEX_MIN
    conf_floor: float = DEFAULT_CONF_FLOOR
    coverage_min: float = DEFAULT_COVERAGE_MIN
    top_n: int = DEFAULT_TOP_N
    distiller: str = DEFAULT_DISTILLER


def _env_float(name: str, default: float) -> float:
    """Read ``name`` as float, returning ``default`` on missing/garbage."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    """Read ``name`` as int, returning ``default`` on missing/garbage."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def kdl_settings() -> KdlSettings:
    """Resolve :class:`KdlSettings` from ``DWS_AGENT_KDL_*`` env vars.

    Every value falls back to the documented default when the env var is
    unset or cannot be parsed, so the function never raises on bad input.
    """
    distiller = os.environ.get("DWS_AGENT_KDL_DISTILLER") or DEFAULT_DISTILLER
    return KdlSettings(
        w_fresh=_env_float("DWS_AGENT_KDL_W_FRESH", DEFAULT_W_FRESH),
        w_auth=_env_float("DWS_AGENT_KDL_W_AUTH", DEFAULT_W_AUTH),
        w_rel=_env_float("DWS_AGENT_KDL_W_REL", DEFAULT_W_REL),
        abstain_rel_min=_env_float("DWS_AGENT_KDL_ABSTAIN_REL_MIN", DEFAULT_ABSTAIN_REL_MIN),
        abstain_lex_min=_env_float("DWS_AGENT_KDL_ABSTAIN_LEX_MIN", DEFAULT_ABSTAIN_LEX_MIN),
        conf_floor=_env_float("DWS_AGENT_KDL_CONF_FLOOR", DEFAULT_CONF_FLOOR),
        coverage_min=_env_float("DWS_AGENT_KDL_COVERAGE_MIN", DEFAULT_COVERAGE_MIN),
        top_n=_env_int("DWS_AGENT_KDL_TOPN", DEFAULT_TOP_N),
        distiller=distiller.strip() or DEFAULT_DISTILLER,
    )
