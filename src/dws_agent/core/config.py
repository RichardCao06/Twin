"""Central environment-variable and default resolution.

This is the single source of truth for runtime configuration. Every other
module reads configuration through :func:`load_config` rather than touching
``os.environ`` directly, so the documented defaults live in one place.

Environment variables (see global contract ``env_vars``):

- ``DWS_AGENT_HOME``                   runtime root (default ``~/.claude/dws-agent``)
- ``DWS_AGENT_DWS_BIN``                path to dws binary (default ``/opt/homebrew/bin/dws``)
- ``DWS_AGENT_TEST_MODE``              ``"1"`` => never invoke the real dws binary
- ``DWS_AGENT_KEYCHAIN_SERVICE_PREFIX`` default ``dws-agent``
- ``DWS_AGENT_CONFIRM_TTL``            fallback confirm TTL seconds (default 300)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOME = "~/.claude/dws-agent"
DEFAULT_DWS_BIN = "/opt/homebrew/bin/dws"
DEFAULT_KEYCHAIN_PREFIX = "dws-agent"
DEFAULT_CONFIRM_TTL = 300


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration.

    Attributes:
        home:            runtime root directory (DWS_AGENT_HOME).
        dws_bin:         path to the dws binary (real or mock).
        test_mode:       if True, real dws must never be invoked.
        keychain_prefix: per-purpose Keychain service name prefix.
        confirm_ttl:     fallback confirm_token TTL in seconds. policy.yaml
                         overrides this at runtime; this is the floor used when
                         policy is unavailable.
    """

    home: Path
    dws_bin: Path
    test_mode: bool
    keychain_prefix: str
    confirm_ttl: int = DEFAULT_CONFIRM_TTL


def _resolve_home(raw: str | None) -> Path:
    return Path(os.path.expanduser(raw or DEFAULT_HOME)).resolve()


def load_config() -> Config:
    """Build a :class:`Config` from the current process environment.

    Reads ``os.environ`` exactly once. Unset variables fall back to the
    documented defaults. ``DWS_AGENT_TEST_MODE == "1"`` enables test mode.
    """
    home = _resolve_home(os.environ.get("DWS_AGENT_HOME"))
    dws_bin = Path(
        os.path.expanduser(os.environ.get("DWS_AGENT_DWS_BIN", DEFAULT_DWS_BIN))
    )
    test_mode = os.environ.get("DWS_AGENT_TEST_MODE", "") == "1"
    keychain_prefix = os.environ.get(
        "DWS_AGENT_KEYCHAIN_SERVICE_PREFIX", DEFAULT_KEYCHAIN_PREFIX
    )
    try:
        confirm_ttl = int(os.environ.get("DWS_AGENT_CONFIRM_TTL", DEFAULT_CONFIRM_TTL))
    except (TypeError, ValueError):
        confirm_ttl = DEFAULT_CONFIRM_TTL

    return Config(
        home=home,
        dws_bin=dws_bin,
        test_mode=test_mode,
        keychain_prefix=keychain_prefix,
        confirm_ttl=confirm_ttl,
    )


def is_test_mode() -> bool:
    """Return True if DWS_AGENT_TEST_MODE is enabled.

    Convenience helper so callers don't have to build a full Config just to
    check the test-mode guard.
    """
    return os.environ.get("DWS_AGENT_TEST_MODE", "") == "1"
