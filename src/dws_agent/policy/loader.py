"""Load and validate the dws-agent policy.

Resolution order (per policy_yaml contract):
  1. Runtime override: $DWS_AGENT_HOME/policy/policy.yaml (if present)
  2. Packaged default: this package's policy.yaml

The parsed policy is cached per resolved file path so repeated loads are cheap.
Validation is conservative: structure must be sane and the `never` list must be
non-empty (a non-empty never list is a hard safety invariant).
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_PACKAGED_DEFAULT = Path(__file__).resolve().parent / "policy.yaml"

_CACHE: dict[str, "Policy"] = {}
_CACHE_LOCK = threading.Lock()


class PolicyError(Exception):
    """Raised when policy.yaml is missing required structure or invariants."""


@dataclass(frozen=True)
class Policy:
    """Parsed, validated policy.

    Attributes:
        version: schema version int.
        defaults: dict with deny_level + confirm_ttl_seconds.
        levels: mapping level-name -> {auto, needs_confirm, human_only?}.
        r0_whitelist: list of normalized prefix token-lists (list[list[str]]).
        never: list of (cmd, subcmd) tuples; checked first, terminal.
        rules: list of {prefix: list[str], level: str}, retained as given.
        c_axis: extension stub dict.
        w_axis: extension stub dict.
        source_path: file the policy was loaded from.
    """

    version: int
    defaults: dict[str, Any]
    levels: dict[str, dict[str, Any]]
    r0_whitelist: list[list[str]]
    never: list[tuple[str, str]]
    rules: list[dict[str, Any]]
    c_axis: dict[str, Any] = field(default_factory=dict)
    w_axis: dict[str, Any] = field(default_factory=dict)
    source_path: str = ""

    @property
    def deny_level(self) -> str:
        """Default-deny level for unmatched subcommands (>= R2)."""
        return str(self.defaults.get("deny_level", "R2"))

    @property
    def confirm_ttl_seconds(self) -> int:
        """Default confirm_token TTL in seconds."""
        return int(self.defaults.get("confirm_ttl_seconds", 300))


def _resolve_path(paths: Any) -> Path:
    """Resolve which policy.yaml to load.

    `paths` is expected to be a core.paths.Paths instance exposing either a
    `policy_dir` attribute or a `home` attribute. We degrade gracefully: any of
    those, or a missing/None paths, falls back to the packaged default.
    """
    runtime: Path | None = None
    if paths is not None:
        policy_dir = getattr(paths, "policy_dir", None)
        if policy_dir is not None:
            runtime = Path(policy_dir) / "policy.yaml"
        else:
            home = getattr(paths, "home", None)
            if home is None:
                home = os.environ.get("DWS_AGENT_HOME")
            if home is not None:
                runtime = Path(home) / "policy" / "policy.yaml"
    if runtime is not None and runtime.is_file():
        return runtime
    return _PACKAGED_DEFAULT


def _normalize_never(raw: Any) -> list[tuple[str, str]]:
    if not raw:
        raise PolicyError("policy.never must be a non-empty list")
    out: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            raise PolicyError(f"policy.never entry must be [cmd, subcmd]: {item!r}")
        out.append((str(item[0]), str(item[1])))
    return out


def _normalize_r0(raw: Any) -> list[list[str]]:
    out: list[list[str]] = []
    for item in raw or []:
        if isinstance(item, dict):
            match = item.get("match")
        else:
            match = item
        if not isinstance(match, (list, tuple)) or not match:
            raise PolicyError(f"r0_whitelist entry needs non-empty 'match': {item!r}")
        out.append([str(t) for t in match])
    return out


def _normalize_rules(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict) or "prefix" not in item or "level" not in item:
            raise PolicyError(f"rule needs 'prefix' and 'level': {item!r}")
        prefix = item["prefix"]
        if not isinstance(prefix, (list, tuple)) or not prefix:
            raise PolicyError(f"rule 'prefix' must be a non-empty list: {item!r}")
        out.append({"prefix": [str(t) for t in prefix], "level": str(item["level"])})
    return out


def _parse(data: dict[str, Any], source: str) -> Policy:
    if not isinstance(data, dict):
        raise PolicyError("policy.yaml root must be a mapping")

    levels = data.get("levels") or {}
    if not isinstance(levels, dict) or "R0" not in levels:
        raise PolicyError("policy.levels must define at least R0")

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise PolicyError("policy.defaults must be a mapping")
    if "deny_level" not in defaults:
        raise PolicyError("policy.defaults.deny_level is required (default-deny)")

    return Policy(
        version=int(data.get("version", 1)),
        defaults=defaults,
        levels=levels,
        r0_whitelist=_normalize_r0(data.get("r0_whitelist")),
        never=_normalize_never(data.get("never")),
        rules=_normalize_rules(data.get("rules")),
        c_axis=dict(data.get("C_axis") or {}),
        w_axis=dict(data.get("W_axis") or {}),
        source_path=source,
    )


def load_policy(paths: Any = None, *, force_reload: bool = False) -> Policy:
    """Load + validate the policy, caching by resolved source path.

    Args:
        paths: core.paths.Paths instance (or None to use env/packaged default).
        force_reload: bypass the cache and re-read from disk.

    Returns:
        A validated, frozen :class:`Policy`.

    Raises:
        PolicyError: if the file is missing required structure or invariants.
    """
    path = _resolve_path(paths)
    key = str(path)
    if not force_reload:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached is not None:
                return cached

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError as exc:
        raise PolicyError(f"policy.yaml not found at {path}") from exc

    policy = _parse(data, key)
    with _CACHE_LOCK:
        _CACHE[key] = policy
    return policy
