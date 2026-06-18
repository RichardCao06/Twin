"""ActionIntent inbox reader.

Reads ActionIntent JSON files from ``$DWS_AGENT_HOME/state/inbox/``, validates
them against the shared contract, and moves them between the
inbox -> processing -> done/failed lifecycle directories.

No LLM. Ordering is fully deterministic: by ``created_at`` (RFC3339 string,
lexically sortable for UTC) then by filename as a tie-break.

The ActionIntent contract (see ``contracts/action_intent.schema.json``) is the
ONLY message the Executor consumes from the thinking side. Hard reject rules
mirror the global contract:

* missing ``argv`` or ``argv[0] != 'dws'`` => hard reject (R2-class deny).
* unknown / extra fields are ignored (validation does not fail on them) but the
  caller is expected to log them.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

# Lifecycle subdirectories under $DWS_AGENT_HOME/state/
INBOX_DIR = "inbox"
PROCESSING_DIR = "processing"
DONE_DIR = "done"
FAILED_DIR = "failed"

_ACTION_ID_RE = re.compile(r"^AI-\d{8}-[0-9a-fA-F]{8}$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)

_VALID_SOURCE = {"triage", "work", "cli", "test"}
_VALID_COMMIT_CLASS = {"none", "maybe", "yes"}
_VALID_TAINT = {"CLEAN", "INTERNAL", "SENSITIVE"}


@dataclass
class Intent:
    """Parsed, validated ActionIntent.

    ``raw`` keeps the full original object so unknown fields survive (they are
    ignored for classification but may be logged). ``path`` is the location the
    intent was read from at poll time (typically in ``processing/`` once it has
    been claimed).
    """

    action_id: str
    created_at: str
    source: str
    argv: list[str]
    cwd: str | None = None
    stdin: str | None = None
    semantic_labels: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    path: str | None = None

    @property
    def commit_class(self) -> str:
        return str(self.semantic_labels.get("commit_class", "maybe"))

    @property
    def taint(self) -> str:
        return str(self.semantic_labels.get("taint", "INTERNAL"))

    @property
    def public_ok(self) -> bool:
        return bool(self.semantic_labels.get("public_ok", False))

    @classmethod
    def from_obj(cls, obj: dict[str, Any], *, path: str | None = None) -> "Intent":
        labels = dict(obj.get("semantic_labels") or {})
        # Apply conservative phase0 defaults without mutating raw.
        labels.setdefault("commit_class", "maybe")
        labels.setdefault("taint", "INTERNAL")
        labels.setdefault("public_ok", False)
        return cls(
            action_id=str(obj.get("action_id", "")),
            created_at=str(obj.get("created_at", "")),
            source=str(obj.get("source", "")),
            argv=list(obj.get("argv") or []),
            cwd=obj.get("cwd"),
            stdin=obj.get("stdin"),
            semantic_labels=labels,
            meta=dict(obj.get("meta") or {}),
            raw=obj,
            path=path,
        )


def validate_intent(obj: Any) -> list[str]:
    """Validate a decoded ActionIntent object against the shared contract.

    Returns a list of human-readable error strings; an empty list means valid.
    Extra/unknown fields are intentionally NOT treated as errors (they are
    ignored but should be logged by the caller).
    """
    errors: list[str] = []
    if not isinstance(obj, dict):
        return ["intent is not a JSON object"]

    action_id = obj.get("action_id")
    if not isinstance(action_id, str) or not _ACTION_ID_RE.match(action_id):
        errors.append("action_id missing or not matching AI-<YYYYMMDD>-<8hex>")

    created_at = obj.get("created_at")
    if not isinstance(created_at, str) or not _RFC3339_RE.match(created_at):
        errors.append("created_at missing or not RFC3339 UTC")

    source = obj.get("source")
    if source not in _VALID_SOURCE:
        errors.append(f"source must be one of {sorted(_VALID_SOURCE)}")

    # HARD REJECT rules from the contract: argv must exist and argv[0]=='dws'.
    argv = obj.get("argv")
    if not isinstance(argv, list) or not argv:
        errors.append("argv missing or empty (hard reject, R2-class deny)")
    elif not all(isinstance(t, str) for t in argv):
        errors.append("argv must be a list of strings")
    elif argv[0] != "dws":
        errors.append("argv[0] must be 'dws' (hard reject, R2-class deny)")

    cwd = obj.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        errors.append("cwd must be a string or null")
    stdin = obj.get("stdin")
    if stdin is not None and not isinstance(stdin, str):
        errors.append("stdin must be a string or null")

    labels = obj.get("semantic_labels")
    if labels is not None:
        if not isinstance(labels, dict):
            errors.append("semantic_labels must be an object")
        else:
            cc = labels.get("commit_class", "maybe")
            if cc not in _VALID_COMMIT_CLASS:
                errors.append(f"semantic_labels.commit_class invalid: {cc!r}")
            taint = labels.get("taint", "INTERNAL")
            if taint not in _VALID_TAINT:
                errors.append(f"semantic_labels.taint invalid: {taint!r}")

    meta = obj.get("meta")
    if meta is not None and not isinstance(meta, dict):
        errors.append("meta must be an object")

    return errors


def _state_dir(paths: Any, name: str) -> str:
    """Resolve a state subdirectory from a ``paths`` object or mapping.

    Accepts either an object exposing ``state_dir`` / ``home`` attributes (the
    core.paths object) or a plain dict/string fallback so this module remains
    unit-testable without core being present.
    """
    # Preferred: core.paths-style object.
    for attr in ("state_dir", "state"):
        base = getattr(paths, attr, None)
        if base:
            return os.path.join(str(base), name)
    # Mapping fallback.
    if isinstance(paths, dict):
        if "state" in paths:
            return os.path.join(str(paths["state"]), name)
        if "home" in paths:
            return os.path.join(str(paths["home"]), "state", name)
    # Object with a home attribute.
    home = getattr(paths, "home", None)
    if home:
        return os.path.join(str(home), "state", name)
    # Plain string treated as $DWS_AGENT_HOME.
    if isinstance(paths, str):
        return os.path.join(paths, "state", name)
    raise ValueError("cannot resolve state directory from paths")


def _ensure_dirs(paths: Any) -> None:
    for name in (INBOX_DIR, PROCESSING_DIR, DONE_DIR, FAILED_DIR):
        os.makedirs(_state_dir(paths, name), exist_ok=True)


def _atomic_move(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.replace(src, dst)


def _find_by_action_id(directory: str, action_id: str) -> str | None:
    if not os.path.isdir(directory):
        return None
    # Exact filename first, then scan for matching action_id inside files.
    candidate = os.path.join(directory, f"{action_id}.json")
    if os.path.exists(candidate):
        return candidate
    for fn in sorted(os.listdir(directory)):
        if not fn.endswith(".json"):
            continue
        full = os.path.join(directory, fn)
        try:
            with open(full, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
            if isinstance(obj, dict) and obj.get("action_id") == action_id:
                return full
        except (OSError, json.JSONDecodeError):
            continue
    return None


def poll_inbox(paths: Any) -> Iterator[Intent]:
    """Yield ActionIntents from the inbox in deterministic order.

    Each yielded intent is *claimed*: its file is atomically moved from
    ``inbox/`` to ``processing/`` before being yielded, so that a concurrent
    poller cannot pick the same intent. Files that fail to parse or validate are
    moved to ``failed/`` (and not yielded); the caller is responsible for
    auditing those moves if needed.

    Ordering: by ``created_at`` then filename.
    """
    _ensure_dirs(paths)
    inbox = _state_dir(paths, INBOX_DIR)
    entries: list[tuple[str, str, dict[str, Any] | None]] = []
    for fn in os.listdir(inbox):
        if not fn.endswith(".json"):
            continue
        full = os.path.join(inbox, fn)
        if not os.path.isfile(full):
            continue
        obj: dict[str, Any] | None = None
        created = ""
        try:
            with open(full, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                obj = loaded
                created = str(loaded.get("created_at", ""))
        except (OSError, json.JSONDecodeError):
            obj = None
        entries.append((created, fn, obj))

    # Deterministic: created_at then filename.
    entries.sort(key=lambda e: (e[0], e[1]))

    for created, fn, obj in entries:
        src = os.path.join(inbox, fn)
        if obj is None:
            # Unparseable: quarantine to failed/.
            try:
                _atomic_move(src, os.path.join(_state_dir(paths, FAILED_DIR), fn))
            except OSError:
                pass
            continue
        errors = validate_intent(obj)
        if errors:
            # Invalid intent: quarantine, do not yield.
            try:
                _atomic_move(src, os.path.join(_state_dir(paths, FAILED_DIR), fn))
            except OSError:
                pass
            continue
        # Claim it: move to processing/ keyed by action_id.
        action_id = str(obj["action_id"])
        proc_path = os.path.join(_state_dir(paths, PROCESSING_DIR), f"{action_id}.json")
        try:
            _atomic_move(src, proc_path)
        except OSError:
            # Another worker may have claimed it; skip.
            continue
        yield Intent.from_obj(obj, path=proc_path)


def mark_done(paths: Any, action_id: str) -> str | None:
    """Move a processing intent to ``done/``. Returns the new path or None."""
    _ensure_dirs(paths)
    src = _find_by_action_id(_state_dir(paths, PROCESSING_DIR), action_id)
    if src is None:
        return None
    dst = os.path.join(_state_dir(paths, DONE_DIR), f"{action_id}.json")
    _atomic_move(src, dst)
    return dst


def mark_failed(paths: Any, action_id: str, reason: str) -> str | None:
    """Move a processing intent to ``failed/``, annotating it with ``reason``.

    Returns the new path or None if the intent was not found in processing/.
    """
    _ensure_dirs(paths)
    src = _find_by_action_id(_state_dir(paths, PROCESSING_DIR), action_id)
    if src is None:
        return None
    obj: dict[str, Any] = {}
    try:
        with open(src, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            obj = loaded
    except (OSError, json.JSONDecodeError):
        obj = {"action_id": action_id}
    obj["_failure_reason"] = reason
    dst = os.path.join(_state_dir(paths, FAILED_DIR), f"{action_id}.json")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, dst)
    try:
        os.remove(src)
    except OSError:
        pass
    return dst
