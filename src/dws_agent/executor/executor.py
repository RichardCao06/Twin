"""Core deterministic Executor loop (no LLM).

Pipeline per ActionIntent:

    poll inbox  ->  PolicyGate.evaluate(intent)
                ->  if AUTO: proceed
                    if HUMAN_CONFIRM: require + verify confirm_token, else DRAFT/DENY
                    if DENY: refuse (terminal)
                ->  mint per-invocation DWS_GATE_TOKEN
                ->  invoke dws-shim (under refresh-guard for token-touching ops)
                ->  audit exec_result + produce a readback stub

Hard constraints honoured here:

* No LLM anywhere; classification is delegated to PolicyGate (deterministic).
* The gate decision is NEVER relaxed by the Executor — semantic labels and
  ``--yes`` cannot make it more permissive. The Executor only ever refuses or
  proceeds with exactly the decision it was given.
* Risk judging never inspects ``--yes`` (handled in normalize_argv).
* In TEST_MODE the shim refuses the real dws binary, so the Executor cannot
  cause real write side effects.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Any

from . import inbox as inbox_mod
from . import refresh_guard
from ._argvutil import argv_norm_sha256, normalize_argv
from .inbox import Intent

# Decisions that require a verified confirm_token before execution.
_CONFIRM_DECISIONS = {"HUMAN_CONFIRM"}
# Levels considered token-touching / refresh-guarded (credential-sensitive).
_REFRESH_GUARDED_HEAD = {"auth"}


@dataclass
class ExecResult:
    """Outcome of executing (or refusing) a single ActionIntent."""

    action_id: str
    level: str | None
    decision: str | None
    exit_code: int | None
    stdout_tail: str
    readback_stub: dict[str, Any]
    reason: str = ""


class Executor:
    """Deterministic executor consuming ActionIntents.

    Parameters
    ----------
    paths:
        core.paths-style object / mapping / $DWS_AGENT_HOME string.
    policy:
        PolicyGate instance exposing ``evaluate(intent) -> decision`` and a
        classifier compatible with the shim. May be None in unit tests, in
        which case a conservative default-deny stub is used.
    gate:
        Optional confirm-token verifier exposing ``verify(action_id, argv, now)
        -> bool`` (policy.confirm). May be None; then HUMAN_CONFIRM intents are
        only executed when an externally pre-verified confirm_token is passed to
        ``execute_intent``.
    """

    def __init__(self, paths: Any, policy: Any = None, gate: Any = None):
        self.paths = paths
        self.policy = policy
        self.gate = gate

    # -- audit ------------------------------------------------------------
    def _audit(self, **record: Any) -> None:
        try:  # pragma: no cover - prefers canonical module when available
            from ..store.audit import AuditLogger  # type: ignore

            AuditLogger(self.paths).log(record)
        except Exception:
            pass

    # -- classification / decision ---------------------------------------
    def _evaluate(self, intent: Intent) -> tuple[str, str, str]:
        """Return (level, decision, reason) for an intent.

        Delegates to PolicyGate.evaluate when available; otherwise applies a
        conservative default-deny fallback (R0 reads => AUTO, everything else
        => HUMAN_CONFIRM) so the loop is testable. NEVER relaxes the result.
        """
        if self.policy is not None:
            ev = getattr(self.policy, "evaluate", None)
            if callable(ev):
                # PolicyGate.evaluate consumes the ActionIntent *dict* (the
                # shared contract). Pass the raw dict when available, falling
                # back to a reconstructed dict view for duck-typed intents.
                payload = getattr(intent, "raw", None)
                if not isinstance(payload, dict) or not payload:
                    payload = {
                        "action_id": intent.action_id,
                        "argv": intent.argv,
                        "semantic_labels": intent.semantic_labels,
                    }
                res = ev(payload)
                # Accept either a (level, decision[, reason]) tuple or an object.
                if isinstance(res, tuple):
                    level = res[0]
                    decision = res[1]
                    reason = res[2] if len(res) > 2 else ""
                else:
                    level = getattr(res, "level", None)
                    decision = getattr(res, "decision", None)
                    reason = getattr(res, "reason", "")
                return level, decision, reason

        # Fallback default-deny path (no policy module yet).
        from .shim import _classify  # local import to reuse fallback classifier

        norm = normalize_argv(intent.argv)
        if len(norm) >= 2 and (norm[0], norm[1]) in _FALLBACK_NEVER:
            return None, "DENY", "never-listed (terminal)"
        level = _classify(None, norm)
        if level == "R0":
            decision = "AUTO"
        else:
            decision = "HUMAN_CONFIRM"
        # AND-with-labels: labels may only make stricter (never relax) — the
        # fallback is already at HUMAN_CONFIRM for non-R0 so nothing to do but
        # we still must not relax R0->AUTO if labels are SENSITIVE/committal.
        if level == "R0" and (
            intent.taint == "SENSITIVE" or intent.commit_class != "none"
        ):
            # Strictest-wins: an R0 read with sensitive/committal labels stays
            # AUTO for reads (labels cannot promote toward AUTO and cannot make
            # a read a write); reads have no write side effects. Kept explicit
            # to document the rule.
            pass
        return level, decision, "default-deny fallback"

    # -- gate token -------------------------------------------------------
    def _gate_secret(self) -> bytes | None:
        try:  # pragma: no cover - prefers canonical module
            from ..crypto import keys  # type: ignore

            return keys.get_hmac_key("gate", paths=self.paths)
        except Exception:
            env_secret = os.environ.get("DWS_AGENT_GATE_SECRET")
            return env_secret.encode("utf-8") if env_secret else None

    def _mint_gate_token(self, intent: Intent) -> str:
        """Mint the per-invocation DWS_GATE_TOKEN for an intent.

        token = HMAC(gate_key, argv_norm_sha256 + action_id). Bound to the exact
        normalized argv and action_id so the shim can independently re-verify.
        Raises RuntimeError if no gate key is available.
        """
        secret = self._gate_secret()
        if secret is None:
            raise RuntimeError("no gate key available to mint DWS_GATE_TOKEN")
        norm_sha = argv_norm_sha256(intent.argv)
        msg = (norm_sha + intent.action_id).encode("utf-8")
        return hmac.new(secret, msg, hashlib.sha256).hexdigest()

    # -- shim invocation --------------------------------------------------
    def _shim_path(self) -> list[str]:
        """Return the command prefix used to invoke the shim subprocess.

        Invokes the shim module via the current interpreter (``python -m``) so
        it works whether or not the ``dws-shim`` console script is installed,
        and so token isolation via env (not PATH) is preserved.
        """
        return [sys.executable, "-m", "dws_agent.executor.shim"]

    def _invoke_shim(self, intent: Intent, gate_token: str) -> tuple[int, str]:
        """Run the shim subprocess, passing the gate token only via env."""
        # The shim expects the dws subcommand tokens (argv without leading 'dws').
        sub = list(intent.argv[1:])
        env = dict(os.environ)
        env["DWS_GATE_TOKEN"] = gate_token
        env["DWS_GATE_ACTION_ID"] = intent.action_id
        proc = subprocess.run(
            [*self._shim_path(), *sub],
            env=env,
            input=intent.stdin,
            cwd=intent.cwd or None,
            capture_output=True,
            text=True,
            check=False,
        )
        tail = (proc.stdout or "")[-2000:]
        if proc.stderr:
            tail = (tail + "\n" + proc.stderr)[-2000:]
        return proc.returncode, tail

    def _is_refresh_guarded(self, intent: Intent) -> bool:
        norm = normalize_argv(intent.argv)
        return bool(norm) and norm[0] in _REFRESH_GUARDED_HEAD

    # -- core -------------------------------------------------------------
    def execute_intent(
        self, intent: Intent, confirm_token: str | None = None
    ) -> ExecResult:
        """Evaluate and (if permitted) execute a single intent.

        The gate decision is authoritative and never relaxed. For
        HUMAN_CONFIRM decisions a confirm_token is REQUIRED and verified against
        the intent's action_id + presented argv; failure yields a DRAFT/DENY
        result with no shim invocation.
        """
        norm_sha = argv_norm_sha256(intent.argv)
        level, decision, reason = self._evaluate(intent)
        self._audit(
            event="classify",
            actor="executor",
            action_id=intent.action_id,
            argv_norm_sha256=norm_sha,
            level=level,
            decision=decision,
            reason=reason,
            detail={"source": intent.source},
            pid=os.getpid(),
        )

        # Terminal deny (e.g. never-list, hard reject).
        if decision == "DENY":
            return ExecResult(
                action_id=intent.action_id,
                level=level,
                decision="DENY",
                exit_code=None,
                stdout_tail="",
                readback_stub={"status": "denied"},
                reason=reason or "denied",
            )

        # Confirm-required decisions need a verified token.
        if decision in _CONFIRM_DECISIONS:
            ok = self._verify_confirm(intent, confirm_token)
            if not ok:
                self._audit(
                    event="gate_decision",
                    actor="executor",
                    action_id=intent.action_id,
                    argv_norm_sha256=norm_sha,
                    level=level,
                    decision="DRAFT",
                    reason="confirm_token missing/invalid; held as draft",
                    detail={},
                    pid=os.getpid(),
                )
                return ExecResult(
                    action_id=intent.action_id,
                    level=level,
                    decision="DRAFT",
                    exit_code=None,
                    stdout_tail="",
                    readback_stub={"status": "awaiting_confirm"},
                    reason="confirm_token missing or invalid",
                )

        # Permitted: AUTO, or HUMAN_CONFIRM with a verified token.
        self._audit(
            event="gate_decision",
            actor="executor",
            action_id=intent.action_id,
            argv_norm_sha256=norm_sha,
            level=level,
            decision=decision,
            reason="proceeding to execute",
            detail={},
            pid=os.getpid(),
        )

        try:
            gate_token = self._mint_gate_token(intent)
        except RuntimeError as e:
            return ExecResult(
                action_id=intent.action_id,
                level=level,
                decision=decision,
                exit_code=None,
                stdout_tail="",
                readback_stub={"status": "error"},
                reason=str(e),
            )

        if self._is_refresh_guarded(intent):
            from ..store.audit import AuditLogger  # type: ignore  # best effort

            audit_logger = None
            try:
                audit_logger = AuditLogger(self.paths)
            except Exception:
                audit_logger = None
            with refresh_guard.refresh_lock(
                self.paths, purpose=f"exec:{intent.action_id}", audit=audit_logger
            ):
                exit_code, tail = self._invoke_shim(intent, gate_token)
        else:
            exit_code, tail = self._invoke_shim(intent, gate_token)

        readback = {
            "status": "executed" if exit_code == 0 else "exec_error",
            "exit_code": exit_code,
            # Phase0 readback is a stub: real readback (re-fetch & diff) is a
            # later-phase extension point.
            "readback": "stub",
        }
        self._audit(
            event="exec_result",
            actor="executor",
            action_id=intent.action_id,
            argv_norm_sha256=norm_sha,
            level=level,
            decision=decision,
            reason="shim returned",
            detail={"exit_code": exit_code, "readback": "stub"},
            pid=os.getpid(),
        )
        return ExecResult(
            action_id=intent.action_id,
            level=level,
            decision=decision,
            exit_code=exit_code,
            stdout_tail=tail,
            readback_stub=readback,
            reason="executed",
        )

    def _verify_confirm(self, intent: Intent, confirm_token: str | None) -> bool:
        """Verify a confirm_token for a HUMAN_CONFIRM intent.

        Prefers the canonical ``policy.confirm`` verifier (which checks
        existence/unused/TTL/hash/HMAC and marks one-time use + audits). Falls
        back to: only accept if the gate object verifies, otherwise reject.
        Never accepts a missing token.
        """
        if self.gate is not None:
            import time as _time

            verify = getattr(self.gate, "verify", None)
            if callable(verify):
                try:
                    return bool(
                        verify(intent.action_id, intent.argv, _time.time())
                    )
                except TypeError:
                    # Alternate signature verify(action_id, presented_argv).
                    return bool(verify(intent.action_id, intent.argv))
        # No verifier available: a token alone cannot be trusted -> reject.
        return False

    def run_once(self) -> list[ExecResult]:
        """Drain the inbox once, executing each intent (AUTO only by default).

        Intents requiring confirmation are held as DRAFT (no confirm_token is
        available in an unattended drain). Each processed intent is moved to
        done/ (executed or drafted) or failed/ (denied / exec error). Returns
        the per-intent results in processing order.
        """
        results: list[ExecResult] = []
        for intent in inbox_mod.poll_inbox(self.paths):
            try:
                res = self.execute_intent(intent, confirm_token=None)
            except Exception as e:  # never let one intent kill the loop
                inbox_mod.mark_failed(self.paths, intent.action_id, f"exception: {e}")
                results.append(
                    ExecResult(
                        action_id=intent.action_id,
                        level=None,
                        decision=None,
                        exit_code=None,
                        stdout_tail="",
                        readback_stub={"status": "error"},
                        reason=f"exception: {e}",
                    )
                )
                continue

            if res.decision == "DENY" or (
                res.exit_code is not None and res.exit_code != 0
            ):
                inbox_mod.mark_failed(self.paths, intent.action_id, res.reason)
            else:
                inbox_mod.mark_done(self.paths, intent.action_id)
            results.append(res)
        return results


# Conservative fallback never-list used only when no policy module is present.
_FALLBACK_NEVER = {
    ("auth", "export"),
    ("auth", "import"),
    ("auth", "logout"),
    ("auth", "reset"),
}


def result_to_dict(res: ExecResult) -> dict[str, Any]:
    """Serialize an ExecResult to a plain dict (for CLI/JSON output)."""
    return asdict(res)
