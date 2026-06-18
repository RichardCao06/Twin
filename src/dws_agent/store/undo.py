"""Undo snapshot store.

Before any reversible write, the executor captures a snapshot of the prior
state so the action can (eventually) be reversed. Phase0 implements:
  * ``snapshot``      - capture + persist a before-state record.
  * ``list_snapshots``- enumerate captured snapshots.
  * ``load_snapshot`` - read one back.
  * ``restore_stub``  - synthesize the *inverse* ActionIntent (NOT executed;
                        it is handed back to the thinking/gate side, which must
                        re-classify and re-confirm it like any other write).

Snapshots live at ``$DWS_AGENT_HOME/snapshots/<action_id>.json``. If the
snapshot is taint=='SENSITIVE' (or caller passes sensitive=True) the ``before``
blob is encrypted at rest via ``crypto.fileenc`` (Keychain-derived file key),
matching the "encrypted sensitive subdirs" hard constraint. We import fileenc
lazily so this module stays importable/testable even when Keychain is absent;
if encryption is requested but unavailable we REFUSE to write plaintext (fail
closed) rather than leak sensitive state.

restore_stub returns a *pure intent* (argv[0]=='dws'); it never runs dws and
never bypasses the gate. Producing an inverse for an arbitrary write is not
generally possible in phase0, so the stub marks the inverse as best-effort and
leaves argv as a placeholder ``["dws", "undo", "<action_id>"]`` to be resolved
by a later phase. The contract (return shape) is what matters here.
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from pathlib import Path


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_snapshots_dir(paths) -> Path:
    snaps = getattr(paths, "snapshots_dir", None)
    if snaps is not None:
        return Path(snaps)
    home = getattr(paths, "home", None)
    base = Path(home) if home is not None else Path(str(paths))
    return base / "snapshots"


def _new_action_id() -> str:
    return f"AI-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"


def snapshot(paths, action_id: str, kind: str, before: dict, sensitive: bool = False) -> Path:
    """Capture a before-state snapshot for ``action_id``.

    Args:
        paths: object exposing snapshots_dir or home.
        action_id: the owning ActionIntent id (binds audit/confirm).
        kind: short tag describing what is being changed (e.g. "im.send").
        before: JSON-serializable prior state to be able to reverse.
        sensitive: if True (or before is marked SENSITIVE), encrypt the blob at
            rest; if encryption is unavailable we raise rather than write
            plaintext (fail closed).

    Returns the Path of the written snapshot file.
    """
    snaps_dir = _resolve_snapshots_dir(paths)
    snaps_dir.mkdir(parents=True, exist_ok=True)
    target = snaps_dir / f"{action_id}.json"

    record = {
        "action_id": action_id,
        "kind": kind,
        "created_at": _now_iso(),
        "encrypted": False,
        "before": before,
    }

    auto_sensitive = bool(
        sensitive
        or (isinstance(before, dict) and before.get("taint") == "SENSITIVE")
    )

    if auto_sensitive:
        try:
            from ..crypto import fileenc  # lazy: keep module importable
        except Exception as exc:  # pragma: no cover - depends on crypto module
            raise RuntimeError(
                "sensitive snapshot requested but crypto.fileenc unavailable; "
                "refusing to write plaintext (fail closed)"
            ) from exc
        blob = json.dumps(before, ensure_ascii=False).encode("utf-8")
        try:
            ciphertext = fileenc.encrypt(blob)
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "sensitive snapshot encryption failed; refusing to write "
                "plaintext (fail closed)"
            ) from exc
        # base64-ish container; fileenc.encrypt is expected to return bytes.
        import base64

        record["encrypted"] = True
        record["before"] = base64.b64encode(ciphertext).decode("ascii")

    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)  # atomic on POSIX
    return target


def list_snapshots(paths) -> list[dict]:
    """List snapshot metadata (without decrypting sensitive blobs).

    Returns a list of dicts with action_id/kind/created_at/encrypted, sorted by
    created_at ascending. Malformed files are skipped.
    """
    snaps_dir = _resolve_snapshots_dir(paths)
    if not snaps_dir.exists():
        return []
    out: list[dict] = []
    for p in snaps_dir.glob("*.json"):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append(
            {
                "action_id": rec.get("action_id"),
                "kind": rec.get("kind"),
                "created_at": rec.get("created_at"),
                "encrypted": rec.get("encrypted", False),
                "path": str(p),
            }
        )
    out.sort(key=lambda r: r.get("created_at") or "")
    return out


def load_snapshot(paths, action_id: str) -> dict:
    """Load and return a snapshot record, decrypting ``before`` if encrypted.

    Raises FileNotFoundError if no snapshot exists for ``action_id``.
    """
    snaps_dir = _resolve_snapshots_dir(paths)
    target = snaps_dir / f"{action_id}.json"
    if not target.exists():
        raise FileNotFoundError(f"no snapshot for action_id {action_id!r}")
    rec = json.loads(target.read_text(encoding="utf-8"))
    if rec.get("encrypted"):
        import base64

        from ..crypto import fileenc  # lazy

        ciphertext = base64.b64decode(rec["before"])
        plaintext = fileenc.decrypt(ciphertext)
        rec = dict(rec)
        rec["before"] = json.loads(plaintext.decode("utf-8"))
        rec["encrypted"] = False
    return rec


def restore_stub(paths, action_id: str) -> dict:
    """Return an *inverse* ActionIntent for ``action_id`` (NOT auto-executed).

    Phase0 stub: produces a fresh, well-formed ActionIntent describing the
    intended reversal. It is pure intent and MUST be fed back through the
    policy gate (re-classified, re-confirmed) like any other write. We do NOT
    run dws here. argv is a placeholder to be resolved by a later phase.
    """
    snap = load_snapshot(paths, action_id)
    inverse_id = _new_action_id()
    return {
        "action_id": inverse_id,
        "created_at": _now_iso(),
        "source": "cli",
        # Placeholder inverse argv; later phases derive a real reversal.
        "argv": ["dws", "undo", action_id],
        "cwd": None,
        "stdin": None,
        "semantic_labels": {
            "commit_class": "maybe",
            "taint": "INTERNAL",
            "public_ok": False,
        },
        "meta": {
            "case_id": None,
            "task_id": None,
            "inverse_of": action_id,
            "restore_kind": snap.get("kind"),
            "best_effort": True,
        },
    }
