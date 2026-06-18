"""PolicyGate: the single收口 (choke point) for thinking-side ActionIntents.

Given an ActionIntent dict it:
  1. validates argv (missing / argv[0] != 'dws' => R2-class hard reject);
  2. classifies on the R-axis (deterministic, no LLM, never looks at --yes);
  3. applies semantic-label AND-strictness (labels can only make stricter,
     never relax toward AUTO);
  4. resolves R(/C/W stub) via MIN_BY_STRICTNESS (C/W are no-ops in phase0);
  5. checks the Kill Switch lock;
  6. emits a gate_decision audit record and returns a GateDecision.

Hard constraints embedded here:
  * Executor reads ONLY argv for classification; semantic_labels can only make
    the decision stricter (AND), never more permissive.
  * default-deny: anything not whitelisted/ruled needs confirm.
  * never list: terminal DENY, surfaced as never=True.
  * Kill Switch engaged => everything is denied.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import classifier, confirm
from .classifier import AUTO, DENY, HUMAN_CONFIRM, Classification, normalize_argv
from .loader import Policy, load_policy

# Strictness ordering for MIN_BY_STRICTNESS (higher index == stricter).
_STRICTNESS = {AUTO: 0, HUMAN_CONFIRM: 1, DENY: 2}


@dataclass(frozen=True)
class GateDecision:
    """Final gate decision for an ActionIntent.

    Attributes:
        action_id: echoed from the intent (or None).
        level: R-axis level or None (never-denied / invalid).
        decision: AUTO | HUMAN_CONFIRM | DENY.
        requires_confirm: True when a valid confirm_token is required to execute.
        never: True if denied by the never list (terminal, not confirmable).
        reason: human-readable explanation for audit / UX.
        argv_norm_sha256: hash of the normalized argv (None when argv invalid).
        human_only: True for R3-style decisions (out-of-band channel is an
            extension point; phase0 still requires confirm_token).
    """

    action_id: str | None
    level: str | None
    decision: str
    requires_confirm: bool
    never: bool
    reason: str
    argv_norm_sha256: str | None = None
    human_only: bool = False


def _stricter(a: str, b: str) -> str:
    """Return the stricter of two decisions (MIN_BY_STRICTNESS == max strictness)."""
    return a if _STRICTNESS[a] >= _STRICTNESS[b] else b


class PolicyGate:
    """Facade evaluating ActionIntents against the loaded policy."""

    def __init__(self, policy: Policy | None = None, paths: Any = None):
        """Construct a gate.

        Args:
            policy: a loaded Policy; if None it is loaded via load_policy(paths).
            paths: core.paths.Paths instance (for policy resolution, kill-switch
                lock, audit, and confirm-token persistence).
        """
        self.paths = paths
        self.policy = policy if policy is not None else load_policy(paths)
        self._dws_bin = os.environ.get("DWS_AGENT_DWS_BIN")
        self._audit = self._resolve_audit()

    # ------------------------------------------------------------------ #
    # wiring helpers (graceful degradation when store/core not yet present)
    # ------------------------------------------------------------------ #
    def _resolve_audit(self):
        try:
            from ..store.audit import AuditLogger  # type: ignore

            return AuditLogger(self.paths)
        except Exception:
            return None

    def _kill_switch_path(self) -> Path:
        ks = getattr(self.paths, "kill_switch_path", None)
        if ks is not None:
            return Path(ks)
        home = getattr(self.paths, "home", None) or os.environ.get(
            "DWS_AGENT_HOME", str(Path.home() / ".claude" / "dws-agent")
        )
        return Path(home) / "state" / "KILL_SWITCH"

    def _emit(self, decision: GateDecision) -> None:
        if self._audit is None:
            return
        try:
            self._audit.log(
                {
                    "event": "gate_decision",
                    "action_id": decision.action_id,
                    "actor": "policygate",
                    "argv_norm_sha256": decision.argv_norm_sha256,
                    "level": decision.level,
                    "decision": decision.decision,
                    "reason": decision.reason,
                    "detail": {
                        "never": decision.never,
                        "requires_confirm": decision.requires_confirm,
                        "human_only": decision.human_only,
                    },
                }
            )
        except Exception:
            # Audit must never break the gate; failures are best-effort here.
            pass

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def check_kill_switch(self) -> bool:
        """Return True if the Kill Switch is engaged (a flag file exists)."""
        return self._kill_switch_path().exists()

    def evaluate(self, intent: dict) -> GateDecision:
        """Evaluate an ActionIntent dict and return a GateDecision.

        Args:
            intent: an ActionIntent dict (see action_intent_json contract).

        Returns:
            A :class:`GateDecision`. Audit is emitted as a side effect.
        """
        action_id = intent.get("action_id") if isinstance(intent, dict) else None
        argv = intent.get("argv") if isinstance(intent, dict) else None

        # --- argv validation: missing or argv[0] != 'dws' => R2-class deny. ---
        if not isinstance(argv, list) or not argv or argv[0] != "dws":
            decision = GateDecision(
                action_id=action_id,
                level=self.policy.deny_level,
                decision=DENY,
                requires_confirm=False,
                never=False,
                reason="invalid ActionIntent: missing argv or argv[0] != 'dws' (R2-class hard reject)",
                argv_norm_sha256=None,
                human_only=False,
            )
            self._emit(decision)
            return decision

        normalized = normalize_argv(argv, self._dws_bin)
        argv_hash = confirm.argv_norm_sha256(normalized)

        cls: Classification = classifier.classify(argv, self.policy, self._dws_bin)

        # --- never list is terminal. ---
        if cls.never:
            decision = GateDecision(
                action_id=action_id,
                level=None,
                decision=DENY,
                requires_confirm=False,
                never=True,
                reason=cls.reason,
                argv_norm_sha256=argv_hash,
                human_only=False,
            )
            self._emit(decision)
            return decision

        # --- semantic-label AND-strictness (取严, can only tighten). ---
        base_decision = cls.decision
        labels = intent.get("semantic_labels") or {}
        commit_class = str(labels.get("commit_class", "maybe"))
        taint = str(labels.get("taint", "INTERNAL"))
        label_reason = ""
        # AND-strictness (取严): only AFFIRMATIVE risk signals tighten the
        # decision. commit_class=='yes' is an affirmative commitment; SENSITIVE
        # taint is an affirmative data-sensitivity signal. The conservative
        # default 'maybe'/'INTERNAL' is NOT itself a tightening trigger, else
        # R0 AUTO could never fire. Labels can ONLY tighten, never relax.
        if commit_class == "yes" or taint == "SENSITIVE":
            # Labels may only push toward HUMAN_CONFIRM, never toward AUTO.
            tightened = _stricter(base_decision, HUMAN_CONFIRM)
            if tightened != base_decision:
                label_reason = (
                    f"; tightened by semantic_labels"
                    f"(commit_class={commit_class}, taint={taint})"
                )
            base_decision = tightened

        # --- MIN_BY_STRICTNESS over R(/C/W). C/W return AUTO (no-op) in phase0,
        #     so the strictest-wins resolution == the R-axis result. ---
        c_axis_decision = AUTO  # extension point, no-op in phase0
        w_axis_decision = AUTO  # extension point, no-op in phase0
        final_decision = _stricter(_stricter(base_decision, c_axis_decision), w_axis_decision)

        # --- Kill Switch overrides everything (engaged => deny). ---
        if self.check_kill_switch():
            decision = GateDecision(
                action_id=action_id,
                level=cls.level,
                decision=DENY,
                requires_confirm=False,
                never=False,
                reason="Kill Switch engaged: all execution denied",
                argv_norm_sha256=argv_hash,
                human_only=False,
            )
            self._emit(decision)
            return decision

        level_spec = self.policy.levels.get(cls.level or "", {})
        human_only = bool(level_spec.get("human_only", False))
        requires_confirm = final_decision == HUMAN_CONFIRM

        decision = GateDecision(
            action_id=action_id,
            level=cls.level,
            decision=final_decision,
            requires_confirm=requires_confirm,
            never=False,
            reason=cls.reason + label_reason,
            argv_norm_sha256=argv_hash,
            human_only=human_only,
        )
        self._emit(decision)
        return decision
