"""Idempotent home-directory scaffolding.

Creates the single-root runtime tree with correct permissions and seeds the
runtime ``policy.yaml`` from the packaged default if missing. Marks the
sensitive subdirectories (``memory/``, ``kb/``) as encrypted-sensitive by
restricting them to ``0700`` and dropping a ``.sensitive`` marker.

Scaffolding never touches the Keychain or invokes dws; it only manages the
filesystem layout. It is safe to call repeatedly.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .paths import SENSITIVE_DIRS, Paths

# Mode 0700 for the home root and sensitive dirs; 0700 keys; 0755 is never used
# since the whole tree is per-user private state.
_PRIVATE_MODE = 0o700

# Directories that must exist after scaffolding. Sensitive ones are created
# with 0700 explicitly via ensure_sensitive_perms.
_ALL_DIR_PROPS = (
    "memory_dir",
    "kb_dir",
    "audit_dir",
    "state_dir",
    "pending_dir",
    "policy_dir",
    "keys_dir",
    "logs_dir",
    "locks_dir",
    "snapshots_dir",
)

# Property names whose dirs must be locked down to 0700.
_PRIVATE_DIR_PROPS = ("memory_dir", "kb_dir", "keys_dir")

_PACKAGED_POLICY = Path(__file__).resolve().parent.parent / "policy" / "policy.yaml"


def _mkdir(path: Path, mode: int) -> bool:
    """Create ``path`` if absent. Returns True if newly created."""
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    # Always enforce mode (tighten even on existing dirs we own).
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return not existed


def ensure_sensitive_perms(paths: Paths) -> None:
    """Lock the sensitive subdirs (``memory/``, ``kb/``) and keys to 0700.

    Also writes a ``.sensitive`` marker file inside each sensitive dir so
    downstream tooling (crypto, backups) can detect that files there are
    expected to be file-level encrypted.
    """
    for name in SENSITIVE_DIRS:
        d = paths.home / name
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, _PRIVATE_MODE)
        except OSError:
            pass
        marker = d / ".sensitive"
        if not marker.exists():
            marker.write_text(
                "encrypted-sensitive: files here are file-level encrypted "
                "(Keychain-derived AES-GCM key, purpose=fileenc)\n",
                encoding="utf-8",
            )
    try:
        os.chmod(paths.keys_dir, _PRIVATE_MODE)
    except OSError:
        pass


def scaffold_home(paths: Paths, *, force: bool = False) -> dict:
    """Idempotently create the home tree and seed the runtime policy.

    Args:
        paths: target layout.
        force: if True, overwrite the runtime ``policy.yaml`` from the packaged
               default even if it already exists.

    Returns:
        A report dict ``{"created": [...], "skipped": [...], "policy": "..."}``
        listing absolute paths that were newly created vs. already present.
    """
    report: dict[str, list[str] | str] = {"created": [], "skipped": [], "policy": ""}

    # Home root first, 0700.
    if _mkdir(paths.home, _PRIVATE_MODE):
        report["created"].append(str(paths.home))  # type: ignore[union-attr]
    else:
        report["skipped"].append(str(paths.home))  # type: ignore[union-attr]

    for prop in _ALL_DIR_PROPS:
        d: Path = getattr(paths, prop)
        mode = _PRIVATE_MODE  # whole tree is private per-user state
        if _mkdir(d, mode):
            report["created"].append(str(d))  # type: ignore[union-attr]
        else:
            report["skipped"].append(str(d))  # type: ignore[union-attr]

    # Tighten sensitive dirs + markers.
    ensure_sensitive_perms(paths)

    # Seed runtime policy.yaml from packaged default.
    runtime_policy = paths.policy_file
    if force or not runtime_policy.exists():
        if _PACKAGED_POLICY.exists():
            shutil.copyfile(_PACKAGED_POLICY, runtime_policy)
            try:
                os.chmod(runtime_policy, 0o600)
            except OSError:
                pass
            report["policy"] = f"seeded:{runtime_policy}"
        else:
            report["policy"] = "missing-packaged-default"
    else:
        report["policy"] = f"exists:{runtime_policy}"

    return report
