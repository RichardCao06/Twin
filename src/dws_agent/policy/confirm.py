"""confirm_token issuance and verification.

Implements the confirm_token contract exactly:

  1. normalize_argv(argv) -> canonical token list (done in classifier.normalize_argv).
  2. canon = '\\n'.join(normalized_argv)
  3. argv_norm_sha256 = sha256(canon.encode('utf-8')).hexdigest()
  4. payload = f"{action_id}|{argv_norm_sha256}|{issued_at_epoch}|{ttl_seconds}"
  5. secret = Keychain-derived HMAC key (service '<prefix>-confirm')
  6. token = base64url(hmac_sha256(secret, payload))

The pending record is stored one-time at
$DWS_AGENT_HOME/state/pending/<action_id>.json and marked used on first
successful verify (replay protection).

Hard constraints:
  * Hash is computed over the NORMALIZED argv; --yes/-y already stripped.
  * VERIFY recomputes the hash from the *presented* argv and rejects on ANY
    mismatch (hash mismatch, expired TTL, missing/used record, HMAC mismatch).
  * One-time use: a verified token cannot be replayed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .classifier import normalize_argv

_CONFIRM_PURPOSE = "confirm"


@dataclass(frozen=True)
class ConfirmRecord:
    """A pending confirm_token record (persisted as JSON).

    Attributes:
        action_id: the ActionIntent action_id this token binds to.
        argv_norm_sha256: sha256 of the newline-joined normalized argv.
        issued_at: epoch seconds the token was issued.
        ttl_seconds: validity window in seconds.
        token: base64url(hmac_sha256(secret, payload)).
        used: one-time-use flag.
    """

    action_id: str
    argv_norm_sha256: str
    issued_at: int
    ttl_seconds: int
    token: str
    used: bool = False


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of verify_token."""

    ok: bool
    reason: str


# --------------------------------------------------------------------------- #
# crypto secret resolution
# --------------------------------------------------------------------------- #
def _get_secret() -> bytes:
    """Resolve the HMAC secret for the 'confirm' purpose.

    Prefers crypto.keychain (Keychain-derived key). Falls back to a deterministic
    test/dev key when crypto is unavailable so the module stays importable and
    unit-testable without a real Keychain. Production wiring uses the real key.
    """
    try:
        from ..crypto import keychain  # type: ignore

        return keychain.get_hmac_key(_CONFIRM_PURPOSE)
    except Exception:
        prefix = os.environ.get("DWS_AGENT_KEYCHAIN_SERVICE_PREFIX", "dws-agent")
        # Deterministic dev/test fallback; NOT used when crypto.keychain exists.
        seed = f"{prefix}-{_CONFIRM_PURPOSE}-fallback".encode("utf-8")
        return hashlib.sha256(seed).digest()


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def _pending_dir(paths: Any) -> Path:
    pd = getattr(paths, "pending_dir", None)
    if pd is not None:
        d = Path(pd)
    else:
        home = getattr(paths, "home", None) or os.environ.get(
            "DWS_AGENT_HOME", str(Path.home() / ".claude" / "dws-agent")
        )
        d = Path(home) / "state" / "pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _record_path(paths: Any, action_id: str) -> Path:
    return _pending_dir(paths) / f"{action_id}.json"


# --------------------------------------------------------------------------- #
# core crypto helpers
# --------------------------------------------------------------------------- #
def argv_norm_sha256(normalized_argv: list[str]) -> str:
    """Compute the canonical argv hash (contract steps 2-3).

    Args:
        normalized_argv: the already-normalized token list (dws + --yes stripped).

    Returns:
        Hex sha256 of the newline-joined utf-8 canonical string.
    """
    canon = "\n".join(normalized_argv)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _compute_token(action_id: str, argv_hash: str, issued_at: int, ttl: int) -> str:
    payload = f"{action_id}|{argv_hash}|{issued_at}|{ttl}"
    secret = _get_secret()
    mac = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------- #
# issue / verify
# --------------------------------------------------------------------------- #
def issue_token(
    action_id: str,
    normalized_argv: list[str],
    ttl: int,
    paths: Any,
    *,
    now: int | None = None,
) -> ConfirmRecord:
    """Issue a one-time confirm_token and persist its pending record.

    Args:
        action_id: ActionIntent action_id (binds the token).
        normalized_argv: already-normalized argv token list.
        ttl: TTL in seconds (typically policy.confirm_ttl_seconds).
        paths: core.paths.Paths instance (or duck-typed equivalent).
        now: optional epoch override for testing.

    Returns:
        The persisted :class:`ConfirmRecord`.
    """
    issued_at = int(now if now is not None else time.time())
    argv_hash = argv_norm_sha256(normalized_argv)
    token = _compute_token(action_id, argv_hash, issued_at, ttl)
    record = ConfirmRecord(
        action_id=action_id,
        argv_norm_sha256=argv_hash,
        issued_at=issued_at,
        ttl_seconds=int(ttl),
        token=token,
        used=False,
    )
    path = _record_path(paths, action_id)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(asdict(record), fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return record


def _load_record(path: Path) -> ConfirmRecord | None:
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return ConfirmRecord(**{k: data[k] for k in ConfirmRecord.__dataclass_fields__})
    except Exception:
        return None


def _mark_used(path: Path, record: ConfirmRecord) -> None:
    data = asdict(record)
    data["used"] = True
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def verify_token(
    action_id: str,
    presented_argv: list[str],
    paths: Any,
    *,
    now: int | None = None,
    dws_bin: str | None = None,
) -> VerifyResult:
    """Verify a confirm_token (one-time, hash + HMAC + TTL bound).

    Recomputes argv_norm_sha256 from the *presented* argv and checks ALL of:
    record exists & unused; now <= issued_at + ttl; stored hash == recomputed;
    recomputed HMAC == stored token. ANY mismatch => REJECT. On success the
    record is marked used (one-time).

    Args:
        action_id: ActionIntent action_id to look up the pending record.
        presented_argv: argv presented at confirm time (full or normalized).
        paths: core.paths.Paths instance.
        now: optional epoch override for testing.
        dws_bin: optional dws binary path for normalization.

    Returns:
        A :class:`VerifyResult`.
    """
    now_epoch = int(now if now is not None else time.time())
    path = _record_path(paths, action_id)
    record = _load_record(path)
    if record is None:
        return VerifyResult(False, "no pending record for action_id (or unreadable)")
    if record.used:
        return VerifyResult(False, "confirm_token already used (one-time)")
    if now_epoch > record.issued_at + record.ttl_seconds:
        return VerifyResult(False, "confirm_token expired (TTL exceeded)")

    recomputed_hash = argv_norm_sha256(normalize_argv(presented_argv, dws_bin))
    if not hmac.compare_digest(recomputed_hash, record.argv_norm_sha256):
        return VerifyResult(False, "argv hash mismatch (command changed since issue)")

    recomputed_token = _compute_token(
        action_id, recomputed_hash, record.issued_at, record.ttl_seconds
    )
    if not hmac.compare_digest(recomputed_token, record.token):
        return VerifyResult(False, "HMAC mismatch (token tampered or wrong key)")

    _mark_used(path, record)
    return VerifyResult(True, "confirm_token verified (marked used)")
