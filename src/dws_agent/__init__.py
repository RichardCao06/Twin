"""dws_agent: a thinking/execution-separated agent for driving the dws CLI.

Phase 0 scaffolding. The package is split into modules owned by separate
concerns:

- core      : config, path layout, home scaffolding, crypto, shared contracts
- policy    : deterministic risk classification + confirm_token (no LLM)
- store     : append-only JSONL audit log
- executor  : consumes ActionIntent JSON, invokes the dws-shim (no LLM)
- privacy   : redaction / taint propagation
- daemon    : dwsd skeleton + launchd plist + CLI

This module exposes ``__version__`` and re-exports :func:`get_paths` for
convenience.
"""

from __future__ import annotations

__version__ = "0.0.0"

# Convenience re-export. Imported lazily-safe: paths only depends on config,
# both stdlib-only, so this never triggers heavy work at import time.
from .core.paths import get_paths  # noqa: E402

__all__ = ["__version__", "get_paths"]
