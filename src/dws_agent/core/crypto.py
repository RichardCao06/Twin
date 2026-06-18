"""Keychain-derived key management + file-level encryption.

Keys are NEVER written to disk. On macOS each per-purpose key is stored as a
generic password in the login Keychain via the ``security`` command; on first
use a cryptographically random 32-byte key is generated and stored, then read
back on subsequent calls. The raw key bytes live only in process memory.

Per-purpose service names are derived from
``DWS_AGENT_KEYCHAIN_SERVICE_PREFIX`` (default ``dws-agent``):

- ``<prefix>-fileenc`` : AES-GCM file encryption for memory/ and kb/
- ``<prefix>-confirm`` : HMAC key for confirm_token
- ``<prefix>-gate``    : HMAC key for the per-invocation DWS_GATE_TOKEN

File-level encryption uses AES-256-GCM with layout ``nonce(12) || ciphertext ||
tag(16)`` (the cryptography library appends the tag to the ciphertext).

If the Keychain is unavailable (non-macOS, CI), a deterministic test fallback
keyed off the service name is used ONLY when ``DWS_AGENT_TEST_MODE=1``; this is
never used for real secrets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import subprocess
from pathlib import Path

from .config import is_test_mode, load_config

try:  # AES-GCM via the cryptography library (not stdlib, but lightweight).
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _HAVE_AESGCM = True
except Exception:  # pragma: no cover - exercised only when dep missing
    _HAVE_AESGCM = False

_KEY_LEN = 32  # AES-256
_NONCE_LEN = 12  # GCM standard nonce

# Cache derived secrets per service for the process lifetime to avoid repeated
# `security` invocations. Keys are service names, values are raw bytes.
_SECRET_CACHE: dict[str, bytes] = {}


def _service_name(purpose: str) -> str:
    """Map a logical purpose to the prefixed Keychain service name."""
    prefix = load_config().keychain_prefix
    return f"{prefix}-{purpose}"


def _account() -> str:
    """Keychain account name (the current OS user)."""
    return os.environ.get("USER") or "dws-agent"


def _security_find(service: str) -> bytes | None:
    """Read a stored key from the Keychain, or None if absent."""
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                _account(),
                "-w",  # print the password (our base64 key) to stdout
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return None
    if out.returncode != 0:
        return None
    val = out.stdout.strip()
    if not val:
        return None
    try:
        return base64.b64decode(val)
    except Exception:
        return None


def _security_add(service: str, key: bytes) -> bool:
    """Store a new random key in the Keychain. Returns success."""
    encoded = base64.b64encode(key).decode("ascii")
    try:
        out = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-s",
                service,
                "-a",
                _account(),
                "-w",
                encoded,
                "-U",  # update if it already exists
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return False
    return out.returncode == 0


def _test_fallback_secret(service: str) -> bytes:
    """Deterministic non-Keychain key for test mode only.

    Derived from the service name so each purpose gets a distinct, stable key.
    NEVER used outside DWS_AGENT_TEST_MODE.
    """
    return hashlib.sha256(f"DWS_AGENT_TEST::{service}".encode("utf-8")).digest()


def get_keychain_secret(purpose: str) -> bytes:
    """Return the 32-byte secret for ``purpose``, creating it on first use.

    On macOS this reads/creates a generic password in the Keychain. In test
    mode, if the Keychain is unavailable, a deterministic fallback is used so
    tests run without prompting for credentials.
    """
    service = _service_name(purpose)
    if service in _SECRET_CACHE:
        return _SECRET_CACHE[service]

    key = _security_find(service)
    if key is None:
        new_key = secrets.token_bytes(_KEY_LEN)
        if _security_add(service, new_key):
            key = _security_find(service) or new_key
        elif is_test_mode():
            key = _test_fallback_secret(service)
        else:
            raise RuntimeError(
                f"Unable to create/read Keychain secret for service '{service}'. "
                "Is the `security` command available?"
            )

    _SECRET_CACHE[service] = key
    return key


def hmac_key(purpose: str) -> bytes:
    """Return the HMAC key bytes for ``purpose`` (e.g. 'confirm', 'gate')."""
    return get_keychain_secret(purpose)


def hmac_sha256_b64url(purpose: str, payload: str) -> str:
    """Convenience: base64url(HMAC-SHA256(key(purpose), payload)).

    Used by policy.confirm (confirm_token) and the gate-token derivation so
    both sides share one implementation.
    """
    mac = hmac.new(hmac_key(purpose), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def _require_aesgcm() -> None:
    if not _HAVE_AESGCM:
        raise RuntimeError(
            "AES-GCM requires the 'cryptography' package; install it to use "
            "file-level encryption."
        )


def encrypt_bytes(data: bytes, key: bytes) -> bytes:
    """Encrypt ``data`` with AES-256-GCM. Layout: nonce(12) || ct || tag."""
    _require_aesgcm()
    if len(key) != _KEY_LEN:
        raise ValueError("key must be 32 bytes for AES-256-GCM")
    nonce = secrets.token_bytes(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, data, None)  # ct includes the 16-byte tag
    return nonce + ct


def decrypt_bytes(blob: bytes, key: bytes) -> bytes:
    """Decrypt a ``nonce || ct || tag`` blob produced by :func:`encrypt_bytes`."""
    _require_aesgcm()
    if len(key) != _KEY_LEN:
        raise ValueError("key must be 32 bytes for AES-256-GCM")
    if len(blob) < _NONCE_LEN + 16:
        raise ValueError("ciphertext blob too short")
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ct, None)


def encrypt_file(path: Path, key: bytes) -> None:
    """Encrypt ``path`` in place (atomic replace) with AES-256-GCM."""
    data = path.read_bytes()
    blob = encrypt_bytes(data, key)
    tmp = path.with_suffix(path.suffix + ".enc.tmp")
    tmp.write_bytes(blob)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def decrypt_file(path: Path, key: bytes) -> bytes:
    """Return the decrypted plaintext of an encrypted file (no in-place write)."""
    return decrypt_bytes(path.read_bytes(), key)
