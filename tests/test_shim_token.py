"""Scenario 1: dws-shim is the OS/token isolation boundary.

A Worker that tries to run a write command through the shim with NO gate token
is refused (exit 1, shim_deny audit), and the mock dws is NEVER invoked. With a
valid executor-minted token the same command passes through to the mock dws.

These tests invoke the shim exactly the way the Executor does: as a subprocess
``python -m dws_agent.executor.shim <subcmd...>`` with the gate token (if any)
supplied ONLY via env (never on PATH / argv).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import subprocess
import sys

from conftest import read_mock_calls

from dws_agent.executor._argvutil import argv_norm_sha256


def _run_shim(sub_tokens, env_extra=None, stdin=""):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "dws_agent.executor.shim", *sub_tokens],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _mint_token(secret: str, full_argv, action_id: str) -> str:
    norm_sha = argv_norm_sha256(full_argv)
    msg = (norm_sha + action_id).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def test_write_command_without_token_is_denied_exit1(home):
    """Worker calls a write command via shim without a token => exit 1."""
    # `im send` is classified non-R0 (write) => requires gate token.
    res = _run_shim(["chat", "message", "send", "--to", "boss", "--text", "hi"])
    assert res.returncode == 1, res.stderr
    assert "gate token" in res.stderr.lower()

    # The mock dws must NOT have been called (no real write side effect).
    assert read_mock_calls(home) == []


def test_write_command_with_valid_token_passes_through(home):
    """A valid executor-minted token lets the write reach the mock dws."""
    action_id = "AI-20260618-deadbeef"
    full_argv = ["dws", "chat", "message", "send", "--to", "boss", "--text", "hi"]
    secret = os.environ["DWS_AGENT_GATE_SECRET"]
    token = _mint_token(secret, full_argv, action_id)

    res = _run_shim(
        full_argv[1:],
        env_extra={"DWS_GATE_TOKEN": token, "DWS_GATE_ACTION_ID": action_id},
    )
    assert res.returncode == 0, res.stderr

    calls = read_mock_calls(home)
    assert len(calls) == 1
    assert calls[0]["argv"] == ["chat", "message", "send", "--to", "boss", "--text", "hi"]
    # The gate token must have been stripped from the child env before exec.
    assert calls[0]["had_gate_token"] is False


def test_tampered_token_is_denied(home):
    """A token bound to a different argv does not authorize this command."""
    action_id = "AI-20260618-deadbeef"
    secret = os.environ["DWS_AGENT_GATE_SECRET"]
    # Token minted for `im send` ...
    token = _mint_token(secret, ["dws", "chat", "message", "send", "--text", "a"], action_id)
    # ... but presented for `approval create` (different argv hash).
    res = _run_shim(
        ["todo", "task", "create", "--text", "a"],
        env_extra={"DWS_GATE_TOKEN": token, "DWS_GATE_ACTION_ID": action_id},
    )
    assert res.returncode == 1, res.stderr
    assert read_mock_calls(home) == []


def test_worker_absolute_path_write_still_blocked(home):
    """A Worker invoking dws via an ABSOLUTE path (argv[0]=/.../dws) to run a
    write 'script' is still classified as a write and blocked without a token.

    The shim is invoked with the dws subcommand tokens; the leading binary is
    not part of those tokens, but this proves normalize_argv recognises the dws
    binary by basename even from an absolute path so judging is unaffected."""
    from dws_agent.executor._argvutil import normalize_argv

    abs_argv = ["/opt/homebrew/bin/dws", "chat", "message", "send", "--text", "run-script"]
    assert normalize_argv(abs_argv) == ["chat", "message", "send", "--text", "run-script"]

    res = _run_shim(["chat", "message", "send", "--text", "run-script"])
    assert res.returncode == 1, res.stderr
    assert read_mock_calls(home) == []


def test_r0_read_passes_without_token(home):
    """R0 read commands are allowed through the shim WITHOUT a gate token."""
    res = _run_shim(["chat", "message", "list"])
    assert res.returncode == 0, res.stderr
    calls = read_mock_calls(home)
    assert len(calls) == 1
    assert calls[0]["argv"] == ["chat", "message", "list"]
