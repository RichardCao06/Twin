"""Append-only JSONL audit logger (single entry point for ALL modules).

Implements the ``audit_record`` contract. Every module that needs to write an
audit line constructs a partial record dict and calls
``AuditLogger.log(record)``; this module injects ``ts`` (RFC3339 UTC with
millis), ``seq`` (monotonic per process) and ``pid``, validates the ``event``
enum, then appends one JSON object per line to the daily-rotated file
``$DWS_AGENT_HOME/audit/audit-<YYYYMMDD>.jsonl`` and fsyncs.

Thread- AND process-safety:
  * within a process: a ``threading.Lock`` guards seq increment + write.
  * across processes: each write takes an advisory ``fcntl.flock`` (LOCK_EX)
    on the file fd while it appends, so interleaving processes never tear a
    line. Append-mode (O_APPEND) guarantees atomic positioning on POSIX.

HARD CONSTRAINT: this logger NEVER redacts for you. Callers that may pass
message bodies / secrets in ``detail`` MUST run them through
``privacy.redaction.redact()`` first. We only refuse to write obviously
malformed records.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import threading
from pathlib import Path

# Allowed event names from the audit_record contract. Unknown events are not
# silently accepted (they are coerced to a logged warning value) so that typos
# in callers surface during testing rather than corrupting analytics.
_VALID_EVENTS = frozenset(
    {
        "classify",
        "gate_decision",
        "confirm_issued",
        "confirm_verified",
        "confirm_rejected",
        "shim_invoke",
        "shim_deny",
        "exec_result",
        "refresh_lock_acquire",
        "refresh_lock_release",
        "scaffold",
        "kill_switch",
        "privacy_filter",
        "undo_snapshot",
        "cli",
    }
)

_VALID_ACTORS = frozenset(
    {"executor", "shim", "policygate", "cli", "dwsd", "privacy", "store"}
)


def _now_iso_millis() -> str:
    """Return current UTC time as RFC3339 with millisecond precision, e.g.
    ``2026-06-18T12:34:56.789Z``."""
    now = _dt.datetime.now(_dt.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _today_stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d")


class AuditLogger:
    """Append-only JSONL audit logger bound to a ``paths`` object.

    ``paths`` must expose an ``audit_dir`` attribute (a directory Path) OR a
    ``home`` attribute from which we derive ``<home>/audit``. We keep this
    duck-typed so the module does not hard-depend on core.paths import order.
    """

    def __init__(self, paths) -> None:
        self._paths = paths
        self._audit_dir = self._resolve_audit_dir(paths)
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0

    @staticmethod
    def _resolve_audit_dir(paths) -> Path:
        audit_dir = getattr(paths, "audit_dir", None)
        if audit_dir is not None:
            return Path(audit_dir)
        home = getattr(paths, "home", None)
        if home is not None:
            return Path(home) / "audit"
        # Last resort: treat paths itself as a directory-like value.
        return Path(str(paths)) / "audit"

    def _current_file(self) -> Path:
        return self._audit_dir / f"audit-{_today_stamp()}.jsonl"

    def log(self, record: dict) -> None:
        """Append a single audit record. Injects ts/seq/pid; fills missing
        contract keys with their null defaults; validates event/actor.

        The write is fsync'd before returning so a crash cannot lose an
        already-acknowledged audit line.
        """
        if not isinstance(record, dict):
            raise TypeError("audit record must be a dict")

        event = record.get("event")
        if event not in _VALID_EVENTS:
            # Do not drop the line; record it but flag the bad event so it is
            # greppable. This keeps the audit trail complete.
            record = dict(record)
            record["_invalid_event"] = event
            record["event"] = "cli"  # safest generic bucket

        actor = record.get("actor")
        if actor not in _VALID_ACTORS:
            record = dict(record)
            record["_invalid_actor"] = actor
            record["actor"] = "store"

        with self._lock:
            self._seq += 1
            full = {
                "ts": _now_iso_millis(),
                "seq": self._seq,
                "event": record.get("event"),
                "action_id": record.get("action_id"),
                "actor": record.get("actor"),
                "argv_norm_sha256": record.get("argv_norm_sha256"),
                "level": record.get("level"),
                "decision": record.get("decision"),
                "reason": record.get("reason", ""),
                "detail": record.get("detail", {}),
                "pid": os.getpid(),
            }
            # Preserve any extra/flagged keys (e.g. _invalid_event) without
            # letting them clobber the canonical fields.
            for k, v in record.items():
                if k not in full:
                    full[k] = v

            line = json.dumps(full, ensure_ascii=False, sort_keys=False) + "\n"
            target = self._current_file()
            # O_APPEND => atomic positioning; flock => no cross-process tearing.
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def read_all(self, date: str | None = None) -> list[dict]:
        """Read back all audit records for ``date`` (YYYYMMDD); default today.

        Malformed lines are skipped (best-effort reader). Returns a list of
        parsed dicts in file order.
        """
        stamp = date or _today_stamp()
        target = self._audit_dir / f"audit-{stamp}.jsonl"
        if not target.exists():
            return []
        out: list[dict] = []
        with open(target, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        return out


# Singleton registry keyed by resolved audit dir so all modules in a process
# share one seq counter and one in-process lock.
_INSTANCES: dict[str, AuditLogger] = {}
_REGISTRY_LOCK = threading.Lock()


def get_audit_logger(paths) -> AuditLogger:
    """Return a process-singleton ``AuditLogger`` for the given ``paths``.

    Singleton is keyed by the resolved audit directory so that all modules
    sharing one ``$DWS_AGENT_HOME`` see a single monotonic seq sequence.
    """
    key = str(AuditLogger._resolve_audit_dir(paths))
    with _REGISTRY_LOCK:
        inst = _INSTANCES.get(key)
        if inst is None:
            inst = AuditLogger(paths)
            _INSTANCES[key] = inst
        return inst
