"""Shared pytest fixtures for the dws-agent phase0 test suite.

Every test runs against an isolated ``$DWS_AGENT_HOME`` under a tmp dir, in
``DWS_AGENT_TEST_MODE=1`` with ``DWS_AGENT_DWS_BIN`` pointing at the mock dws
(tests/mock/dws). This guarantees NO real dws write side effects and NO network.

The executor invokes the shim as ``python -m dws_agent.executor.shim`` in a
subprocess; we export ``PYTHONPATH=src`` so the child can import the package
without an editable install.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
MOCK_DWS = REPO_ROOT / "tests" / "mock" / "dws"

# Make the package importable in-process and in shim subprocesses.
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated $DWS_AGENT_HOME, test-mode env, mock dws wired in.

    Returns a core.paths.Paths object rooted at the tmp home (scaffolded).
    """
    h = tmp_path / "dws-home"
    mock_log = tmp_path / "mock_dws_calls.jsonl"

    monkeypatch.setenv("DWS_AGENT_HOME", str(h))
    monkeypatch.setenv("DWS_AGENT_TEST_MODE", "1")
    monkeypatch.setenv("DWS_AGENT_DWS_BIN", str(MOCK_DWS))
    monkeypatch.setenv("MOCK_DWS_LOG", str(mock_log))
    # Deterministic gate HMAC secret so executor and shim agree without Keychain.
    monkeypatch.setenv("DWS_AGENT_GATE_SECRET", "test-gate-secret-0xfeed")
    # Propagate src onto PYTHONPATH so the shim subprocess can import dws_agent.
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH", str(SRC) + (os.pathsep + existing if existing else "")
    )

    from dws_agent.core import paths as core_paths
    from dws_agent.core import scaffold

    paths = core_paths.get_paths()
    scaffold.scaffold_home(paths, force=True)
    return paths


def make_intent(
    argv,
    *,
    action_id=None,
    source="test",
    commit_class="maybe",
    taint="INTERNAL",
    cwd=None,
    stdin=None,
):
    """Build a contract-shaped ActionIntent dict for tests."""
    if action_id is None:
        action_id = "AI-20260618-%s" % uuid.uuid4().hex[:8]
    return {
        "action_id": action_id,
        "created_at": "2026-06-18T00:00:00Z",
        "source": source,
        "argv": list(argv),
        "cwd": cwd,
        "stdin": stdin,
        "semantic_labels": {
            "commit_class": commit_class,
            "taint": taint,
            "public_ok": False,
        },
        "meta": {"case_id": None, "task_id": None},
    }


def read_mock_calls(paths=None):
    """Return the list of recorded mock-dws invocations (parsed JSONL).

    The log path comes from the ``MOCK_DWS_LOG`` env var set by the ``home``
    fixture; ``paths`` is accepted for call-site readability but unused.
    """
    log = Path(os.environ["MOCK_DWS_LOG"])
    if not log.exists():
        return []
    out = []
    for line in log.read_text("utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


@pytest.fixture
def intent_factory():
    return make_intent


@pytest.fixture
def mock_calls():
    return read_mock_calls
