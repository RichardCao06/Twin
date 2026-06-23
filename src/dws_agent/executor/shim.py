"""dws-shim: the OS-permission / token isolation boundary.

The shim is a thin wrapper invoked as a *subprocess* by the Executor. It:

1. Re-derives the normalized argv and its sha256 from its own ``sys.argv``
   (never trusting the parent's claims, never looking at ``--yes``).
2. Independently re-checks the R0 read-only whitelist via PolicyGate. R0 reads
   are allowed WITHOUT a gate token.
3. For any write (non-R0) command, REQUIRES a valid ``DWS_GATE_TOKEN`` in the
   environment. The token is verified against the recomputed argv + action_id
   using the Keychain-derived 'dws-agent-gate' key. Absent or invalid token on
   a write command => exit 1 + ``shim_deny`` audit.
4. Only after passing does it exec the (mock) dws binary resolved from
   ``DWS_AGENT_DWS_BIN``. In ``DWS_AGENT_TEST_MODE`` it refuses to run if that
   resolves to the real binary, guaranteeing no real write side effects.

The token travels via env (OS-level isolation), never on PATH.

Exposed as console script ``dws-shim`` (entrypoint ``main``).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import subprocess
import sys
from typing import Any

from ._argvutil import argv_norm_sha256, normalize_argv

# ---------------------------------------------------------------------------
# Lazy / defensive imports of sibling modules. The shim must stay importable
# and the core risk logic must work even if those modules are not yet present;
# we fall back to contract-faithful local implementations.
# ---------------------------------------------------------------------------

_REAL_DWS_DEFAULT = "/opt/homebrew/bin/dws"


def _load_policy(paths: Any) -> Any:
    """Load the canonical Policy (grounded R0 whitelist + rules).

    Returns the parsed :class:`Policy` or ``None`` if it cannot be loaded — in
    which case :func:`_classify` fails closed (treats everything as a write).
    """
    try:  # pragma: no cover - prefers canonical module when available
        from ..policy.loader import load_policy

        policy_dir = getattr(paths, "policy_dir", None)
        return load_policy(str(policy_dir)) if policy_dir else load_policy()
    except Exception:
        return None


def _classify(policy: Any, norm_argv: list[str]) -> str:
    """Return a risk level string (R0/R1/R2/R3) for normalized argv.

    Uses the canonical deterministic classifier. If the policy could not be
    loaded, **fails closed**: everything is treated as a write (R2), so the shim
    never auto-passes a command it cannot prove is an R0 read.
    """
    if policy is not None:
        try:
            from ..policy.classifier import classify

            res = classify(norm_argv, policy)
            level = getattr(res, "level", None)
            if isinstance(level, str) and level in ("R0", "R1", "R2", "R3"):
                return level
        except Exception:
            pass
    return "R2"  # fail-closed default-deny (cannot prove R0 => treat as write)


def is_write_command(argv: list[str], policy: Any) -> bool:
    """Return True if ``argv`` is anything other than an R0 read.

    Anything not classified R0 (including unknown subcommands by default-deny)
    is treated as a write/side-effecting command requiring a gate token.
    """
    norm = normalize_argv(argv)
    return _classify(policy, norm) != "R0"


def _gate_secret(paths: Any) -> bytes | None:
    """Fetch the Keychain-derived 'dws-agent-gate' HMAC key.

    Prefers the canonical crypto module. Returns None if unavailable (callers
    treat that as "cannot verify" => deny write).
    """
    # Env override wins (test determinism / Keychain-free CI); real runs derive
    # the 'gate' HMAC key from Keychain so Executor-mint and shim-verify agree.
    env_secret = os.environ.get("DWS_AGENT_GATE_SECRET")
    if env_secret:
        return env_secret.encode("utf-8")
    try:  # pragma: no cover - prefers canonical module when available
        from ..core.crypto import get_keychain_secret  # type: ignore

        return get_keychain_secret("gate")
    except Exception:
        return None


def expected_gate_token(secret: bytes, norm_sha: str, action_id: str) -> str:
    """Compute the expected gate token: HMAC(secret, argv_norm_sha256+action_id)."""
    msg = (norm_sha + action_id).encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def verify_gate_token(token: str, argv: list[str], policy: Any, paths: Any) -> bool:
    """Verify ``token`` against the recomputed argv + action_id.

    The action_id is read from ``DWS_GATE_ACTION_ID`` in the environment (set by
    the Executor alongside the token). Verification recomputes the normalized
    argv sha256 from the *presented* argv and the expected HMAC, comparing in
    constant time. Returns False on any mismatch or missing material.
    """
    if not token:
        return False
    action_id = os.environ.get("DWS_GATE_ACTION_ID", "")
    if not action_id:
        return False
    secret = _gate_secret(paths)
    if secret is None:
        return False
    norm_sha = argv_norm_sha256(argv)
    expected = expected_gate_token(secret, norm_sha, action_id)
    return hmac.compare_digest(token, expected)


def _resolve_dws_bin() -> str:
    return os.environ.get("DWS_AGENT_DWS_BIN", _REAL_DWS_DEFAULT)


def _is_real_binary(path: str) -> bool:
    try:
        real = os.path.realpath(path)
    except OSError:
        real = path
    return real == os.path.realpath(_REAL_DWS_DEFAULT) or path == _REAL_DWS_DEFAULT


def _audit(paths: Any, **record: Any) -> None:
    try:  # pragma: no cover - prefers canonical module when available
        from ..store.audit import AuditLogger  # type: ignore

        AuditLogger(paths).log(record)
    except Exception:
        pass


def _load_paths() -> Any:
    """Resolve the runtime paths object from core, or fall back to env home."""
    try:  # pragma: no cover - prefers canonical module when available
        from ..core import paths as corepaths  # type: ignore

        return corepaths.from_env()
    except Exception:
        return os.environ.get(
            "DWS_AGENT_HOME", os.path.expanduser("~/.claude/dws-agent")
        )


def main(argv: list[str] | None = None) -> int:
    """dws-shim entrypoint.

    Usage: ``dws-shim <subcmd> ...`` (argv[0] is the shim itself). The remaining
    tokens are the dws command line WITHOUT the leading ``dws`` binary token;
    we reconstruct ``['dws', *rest]`` for classification/verification so it lines
    up with the ActionIntent argv contract.
    """
    raw = sys.argv if argv is None else argv
    rest = list(raw[1:])
    # Reconstruct a contract-shaped argv (argv[0]=='dws').
    full_argv = ["dws", *rest] if (not rest or rest[0] != "dws") else list(rest)

    paths = _load_paths()
    policy = _load_policy(paths)
    norm = normalize_argv(full_argv)
    norm_sha = argv_norm_sha256(full_argv)

    write = is_write_command(full_argv, policy)
    token = os.environ.get("DWS_GATE_TOKEN", "")
    action_id = os.environ.get("DWS_GATE_ACTION_ID")

    if write:
        if not verify_gate_token(token, full_argv, policy, paths):
            _audit(
                paths,
                event="shim_deny",
                actor="shim",
                action_id=action_id,
                argv_norm_sha256=norm_sha,
                level=None,
                decision="DENY",
                reason="write command without valid DWS_GATE_TOKEN",
                detail={"argv_norm": norm},
                pid=os.getpid(),
            )
            sys.stderr.write(
                "dws-shim: refusing write command without valid gate token\n"
            )
            return 1
    # else: R0 read — token NOT required; whitelist already re-checked above.

    dws_bin = _resolve_dws_bin()
    test_mode = os.environ.get("DWS_AGENT_TEST_MODE") == "1"
    if test_mode and _is_real_binary(dws_bin):
        _audit(
            paths,
            event="shim_deny",
            actor="shim",
            action_id=action_id,
            argv_norm_sha256=norm_sha,
            level=None,
            decision="DENY",
            reason="TEST_MODE resolves to real dws binary; refusing",
            detail={"dws_bin": dws_bin},
            pid=os.getpid(),
        )
        sys.stderr.write("dws-shim: TEST_MODE but dws bin is real; refusing\n")
        return 1

    _audit(
        paths,
        event="shim_invoke",
        actor="shim",
        action_id=action_id,
        argv_norm_sha256=norm_sha,
        level="R0" if not write else None,
        decision="AUTO" if not write else "HUMAN_CONFIRM",
        reason="invoking dws binary",
        detail={"dws_bin": dws_bin, "argv_norm": norm},
        pid=os.getpid(),
    )

    # Exec the (mock) dws binary. The gate token is NOT forwarded to the child.
    child_env = dict(os.environ)
    for k in ("DWS_GATE_TOKEN", "DWS_GATE_ACTION_ID", "DWS_AGENT_GATE_SECRET"):
        child_env.pop(k, None)
    try:
        proc = subprocess.run(
            [dws_bin, *rest],
            env=child_env,
            stdin=sys.stdin,
            check=False,
        )
        return proc.returncode
    except FileNotFoundError:
        sys.stderr.write(f"dws-shim: dws binary not found: {dws_bin}\n")
        return 127


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
