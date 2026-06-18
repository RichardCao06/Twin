"""policy subpackage.

Owns deterministic (no-LLM) risk classification, confirm_token issuance /
verification, and the PolicyGate facade that thinking-side ActionIntents pass
through before any execution is permitted.

Re-exports PolicyGate for convenience.
"""

from .gate import GateDecision, PolicyGate

__all__ = ["PolicyGate", "GateDecision"]
