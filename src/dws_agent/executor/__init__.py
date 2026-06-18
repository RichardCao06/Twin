"""executor subpackage.

Deterministic, no-LLM execution side of the dws-agent. Consumes ActionIntent
JSON, asks PolicyGate for a decision, and (only when AUTO or a valid
confirm_token is present) mints a per-invocation DWS_GATE_TOKEN and invokes the
dws-shim under the refresh-guard. The shim is the OS-permission/token isolation
boundary that actually exec's the (mock) dws binary.
"""
