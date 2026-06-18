"""Inter-process refresh guard.

Serializes dws token-refresh / single-instance credential-touching operations
across processes using an advisory ``fcntl.flock`` on
``$DWS_AGENT_HOME/locks/refresh.lock`` (design 3.4).

Only one holder at a time. The lock file records the holder PID + purpose +
acquire timestamp so other processes can introspect (``lock_held_by``,
``healthcheck``) and so a stale lock from a dead PID can be detected. Acquire /
release are audited via the store.AuditLogger when one is supplied.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator

LOCK_DIR = "locks"
LOCK_FILE = "refresh.lock"


def _lock_path(paths: Any) -> str:
    """Resolve the refresh lock file path from a paths object/mapping/string."""
    base: str | None = None
    for attr in ("locks_dir", "locks"):
        v = getattr(paths, attr, None)
        if v:
            base = str(v)
            break
    if base is None:
        if isinstance(paths, dict):
            if "locks" in paths:
                base = str(paths["locks"])
            elif "home" in paths:
                base = os.path.join(str(paths["home"]), LOCK_DIR)
        elif isinstance(paths, str):
            base = os.path.join(paths, LOCK_DIR)
        else:
            home = getattr(paths, "home", None)
            if home:
                base = os.path.join(str(home), LOCK_DIR)
    if base is None:
        raise ValueError("cannot resolve locks directory from paths")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, LOCK_FILE)


def _audit(audit: Any, **record: Any) -> None:
    """Best-effort audit: tolerate absence of a logger (unit tests)."""
    if audit is None:
        return
    try:
        audit.log(record)
    except Exception:
        # Audit must never break the lock primitive.
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
        # EPERM => exists but not ours.
        return True
    return True


def _read_holder(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        if isinstance(obj, dict) and "pid" in obj:
            return obj
    except (OSError, json.JSONDecodeError):
        return None
    return None


def lock_held_by(paths: Any) -> int | None:
    """Return the PID recorded as holding the refresh lock, or None.

    This is advisory/introspective: it reads the holder record written into the
    lock file. If the recorded PID is no longer alive the lock is considered
    stale and None is returned.
    """
    path = _lock_path(paths)
    holder = _read_holder(path)
    if holder is None:
        return None
    pid = int(holder.get("pid", 0))
    if not _pid_alive(pid):
        return None
    return pid


@contextmanager
def refresh_lock(
    paths: Any,
    *,
    timeout: float = 30,
    purpose: str = "refresh",
    audit: Any = None,
) -> Iterator[dict[str, Any]]:
    """Acquire the inter-process refresh lock, serializing token refresh.

    Blocks (polling non-blocking flock) up to ``timeout`` seconds. Raises
    ``TimeoutError`` if the lock cannot be obtained in time. On success the lock
    file is populated with the holder record (pid/purpose/acquired_at) and that
    record dict is yielded. The flock is released and the holder record cleared
    on exit. Acquire/release are audited.

    A stale lock whose recorded PID is dead does not block acquisition because
    flock is released by the OS when the holder process dies; the stale holder
    record is simply overwritten.
    """
    path = _lock_path(paths)
    deadline = time.monotonic() + max(0.0, float(timeout))
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    held = lock_held_by(paths)
                    _audit(
                        audit,
                        event="refresh_lock_acquire",
                        actor="executor",
                        action_id=None,
                        decision="DENY",
                        reason=f"timeout after {timeout}s; held_by={held}",
                        detail={"purpose": purpose, "held_by": held},
                    )
                    raise TimeoutError(
                        f"refresh lock not acquired within {timeout}s "
                        f"(held by pid={held})"
                    )
                time.sleep(0.05)

        record = {
            "pid": os.getpid(),
            "purpose": purpose,
            "acquired_at": time.time(),
        }
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(record).encode("utf-8"))
        os.fsync(fd)
        _audit(
            audit,
            event="refresh_lock_acquire",
            actor="executor",
            action_id=None,
            decision="AUTO",
            reason="acquired",
            detail={"purpose": purpose},
        )
        yield record
    finally:
        if acquired:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                os.fsync(fd)
            except OSError:
                pass
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            _audit(
                audit,
                event="refresh_lock_release",
                actor="executor",
                action_id=None,
                decision=None,
                reason="released",
                detail={"purpose": purpose},
            )
        os.close(fd)


def healthcheck(paths: Any) -> dict[str, Any]:
    """Return a snapshot of refresh-lock health for diagnostics/CLI status.

    Keys: ``lock_path``, ``exists``, ``held``, ``holder_pid``,
    ``holder_alive``, ``purpose``, ``held_seconds``, ``stale``.
    """
    path = _lock_path(paths)
    exists = os.path.exists(path)
    holder = _read_holder(path) if exists else None
    result: dict[str, Any] = {
        "lock_path": path,
        "exists": exists,
        "held": False,
        "holder_pid": None,
        "holder_alive": False,
        "purpose": None,
        "held_seconds": None,
        "stale": False,
    }
    if holder:
        pid = int(holder.get("pid", 0))
        alive = _pid_alive(pid)
        result["holder_pid"] = pid
        result["holder_alive"] = alive
        result["purpose"] = holder.get("purpose")
        acquired_at = holder.get("acquired_at")
        if isinstance(acquired_at, (int, float)):
            result["held_seconds"] = max(0.0, time.time() - acquired_at)
        result["held"] = alive
        result["stale"] = bool(pid) and not alive
    return result
