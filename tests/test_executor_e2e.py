"""End-to-end Executor pipeline (no LLM) + audit coverage.

Ties together: PolicyGate decision -> (confirm gate) -> minted DWS_GATE_TOKEN ->
shim subprocess -> mock dws. Asserts:

* R0 read AUTO executes and reaches the mock dws.
* A write WITHOUT a verified confirm_token is held as DRAFT and NEVER reaches
  the mock dws (no real side effect).
* A write WITH a verified confirm_token executes through the shim to mock dws.
* never-list => terminal DENY, no execution.
* The audit JSONL records the full decision trail (classify/gate_decision/...).
"""
from __future__ import annotations

import json
from pathlib import Path

from conftest import make_intent, read_mock_calls

from dws_agent.executor.executor import Executor
from dws_agent.executor.inbox import Intent
from dws_agent.policy import confirm
from dws_agent.policy.classifier import normalize_argv
from dws_agent.policy.gate import PolicyGate
from dws_agent.store.audit import AuditLogger


class _ConfirmGate:
    """Adapter exposing verify(action_id, argv, now) backed by policy.confirm."""

    def __init__(self, paths):
        self.paths = paths

    def verify(self, action_id, argv, now=None):
        return confirm.verify_token(action_id, argv, self.paths, now=now).ok


def _intent_obj(d):
    return Intent.from_obj(d)


def test_r0_read_executes_to_mock_dws(home):
    ex = Executor(home, policy=PolicyGate(paths=home), gate=_ConfirmGate(home))
    res = ex.execute_intent(_intent_obj(make_intent(["dws", "chat", "message", "list"])))
    assert res.decision == "AUTO"
    assert res.exit_code == 0
    calls = read_mock_calls(home)
    assert len(calls) == 1 and calls[0]["argv"] == ["chat", "message", "list"]


def test_write_without_confirm_is_drafted_no_execution(home):
    ex = Executor(home, policy=PolicyGate(paths=home), gate=_ConfirmGate(home))
    intent = _intent_obj(make_intent(["dws", "chat", "message", "send", "--text", "hi"]))
    res = ex.execute_intent(intent, confirm_token=None)
    assert res.decision == "DRAFT"
    assert res.exit_code is None
    # Crucially: the mock dws was never touched.
    assert read_mock_calls(home) == []


def test_write_with_confirm_executes(home):
    gate = _ConfirmGate(home)
    ex = Executor(home, policy=PolicyGate(paths=home), gate=gate)
    full = ["dws", "chat", "message", "send", "--to", "boss", "--text", "hi"]
    intent = _intent_obj(make_intent(full))

    # Human confirms in-band: issue a one-time confirm_token bound to this argv.
    confirm.issue_token(intent.action_id, normalize_argv(full), 300, home)

    res = ex.execute_intent(intent, confirm_token="present")
    assert res.decision == "HUMAN_CONFIRM"
    assert res.exit_code == 0
    calls = read_mock_calls(home)
    assert len(calls) == 1
    assert calls[0]["argv"] == ["chat", "message", "send", "--to", "boss", "--text", "hi"]
    assert calls[0]["had_gate_token"] is False


def test_never_list_terminal_deny_no_execution(home):
    ex = Executor(home, policy=PolicyGate(paths=home), gate=_ConfirmGate(home))
    res = ex.execute_intent(_intent_obj(make_intent(["dws", "auth", "export"])))
    assert res.decision == "DENY"
    assert read_mock_calls(home) == []


def test_audit_trail_is_written(home):
    ex = Executor(home, policy=PolicyGate(paths=home), gate=_ConfirmGate(home))
    ex.execute_intent(_intent_obj(make_intent(["dws", "chat", "message", "list"])))

    records = AuditLogger(home).read_all()
    events = {r["event"] for r in records}
    assert "classify" in events
    assert "exec_result" in events
    # Every record carries the contract-mandated injected fields.
    for r in records:
        assert "ts" in r and "seq" in r and "pid" in r
        assert r["event"] is not None


def test_inbox_drain_holds_writes_as_draft(home):
    """run_once drains the inbox: R0 read executes; write held as DRAFT (no
    confirm available in unattended drain) and never reaches mock dws."""
    inbox = Path(home.state_dir) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    read_intent = make_intent(["dws", "chat", "message", "list"], action_id="AI-20260618-00000001")
    write_intent = make_intent(["dws", "chat", "message", "send", "--text", "x"],
                               action_id="AI-20260618-00000002")
    for it in (read_intent, write_intent):
        (inbox / (it["action_id"] + ".json")).write_text(json.dumps(it), "utf-8")

    ex = Executor(home, policy=PolicyGate(paths=home), gate=_ConfirmGate(home))
    results = {r.action_id: r for r in ex.run_once()}

    assert results["AI-20260618-00000001"].decision == "AUTO"
    assert results["AI-20260618-00000002"].decision == "DRAFT"
    # Only the read reached mock dws.
    calls = read_mock_calls(home)
    assert [c["argv"] for c in calls] == [["chat", "message", "list"]]
