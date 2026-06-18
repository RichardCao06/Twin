"""Runtime path layout derived from ``DWS_AGENT_HOME``.

This is the ONLY place the on-disk layout is defined. Every other module
imports :func:`get_paths` (and the helpers below) rather than constructing
paths by hand, guaranteeing a single-root layout.

Layout under ``$DWS_AGENT_HOME``::

    home/
      memory/      (sensitive, encrypted, 0700)
      kb/          (sensitive, encrypted, 0700)
      audit/       append-only JSONL
      state/
        pending/   confirm_token pending records (<action_id>.json)
      policy/      runtime policy.yaml override
      keys/        key material markers (0700; real keys live in Keychain)
      logs/        dwsd / process logs
      locks/       refresh-guard file locks
      snapshots/   undo snapshots
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, load_config


@dataclass(frozen=True)
class Paths:
    """All runtime paths, derived from a single home root.

    The directory names are fixed here; callers must never hard-code them
    elsewhere.
    """

    home: Path

    @property
    def memory_dir(self) -> Path:
        """Encrypted-sensitive long-term memory store."""
        return self.home / "memory"

    @property
    def kb_dir(self) -> Path:
        """Encrypted-sensitive knowledge-base store."""
        return self.home / "kb"

    @property
    def audit_dir(self) -> Path:
        """Append-only JSONL audit logs (one file per day)."""
        return self.home / "audit"

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def pending_dir(self) -> Path:
        """Pending confirm_token records, keyed by action_id."""
        return self.state_dir / "pending"

    @property
    def policy_dir(self) -> Path:
        """Runtime policy.yaml override location."""
        return self.home / "policy"

    @property
    def keys_dir(self) -> Path:
        """Key-material markers (0700). Real keys live in the Keychain."""
        return self.home / "keys"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def locks_dir(self) -> Path:
        """File locks for the refresh-guard inter-process serialization."""
        return self.home / "locks"

    @property
    def snapshots_dir(self) -> Path:
        """Undo snapshots for reversible operations."""
        return self.home / "snapshots"

    @property
    def policy_file(self) -> Path:
        """Runtime policy.yaml override path."""
        return self.policy_dir / "policy.yaml"

    def pending_file(self, action_id: str) -> Path:
        """Path of the pending confirm record for ``action_id``."""
        return self.pending_dir / f"{action_id}.json"

    def audit_file(self, date: datetime | None = None) -> Path:
        """Path of the audit JSONL for ``date`` (UTC today by default)."""
        d = date or datetime.now(timezone.utc)
        return self.audit_dir / f"audit-{d.strftime('%Y%m%d')}.jsonl"


# Subdirectories marked sensitive (encrypted, 0700). Consumed by scaffold +
# crypto so the two stay in sync.
SENSITIVE_DIRS = ("memory", "kb")


def get_paths(cfg: Config | None = None) -> Paths:
    """Return the :class:`Paths` layout for ``cfg`` (or the live env config)."""
    cfg = cfg or load_config()
    return Paths(home=cfg.home)


def pending_file(action_id: str, cfg: Config | None = None) -> Path:
    """Module-level convenience for :meth:`Paths.pending_file`."""
    return get_paths(cfg).pending_file(action_id)


def audit_file(date: datetime | None = None, cfg: Config | None = None) -> Path:
    """Module-level convenience for :meth:`Paths.audit_file`."""
    return get_paths(cfg).audit_file(date)
