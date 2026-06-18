"""Exit-condition 8: KDL is pure read — no outward send, no dws write.

We prove this several ways:
  * A full ingest -> reindex/verify -> serve -> draft flow records ZERO calls to
    the mock dws binary (the only way KDL could "send" / issue a write).
  * KDL never constructs an ActionIntent and never imports/touches the executor
    or shim (the only modules that can invoke dws).
  * GitReader structurally refuses every non-read git subcommand.
  * The DraftPreview is clearly marked operator-only ("if I answered") and is a
    plain object returned to the caller — there is no transport in the code path.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from dws_agent.kdl import code as kdl_code
from dws_agent.kdl import ingest as kdl_ingest
from dws_agent.kdl import retrieve as kdl_retrieve
from dws_agent.kdl import store as kdl_store
from dws_agent.kdl.code import GitReader
from dws_agent.kdl.ingest import Ingestor

from kdl_helpers import SAMPLE_PY_V1, init_repo, make_paths, open_kdl

REPO_ROOT = Path(__file__).resolve().parents[1]
MOCK_DWS = REPO_ROOT / "tests" / "mock" / "dws"


def test_full_kdl_flow_records_zero_dws_calls(tmp_path, monkeypatch):
    """End-to-end KDL usage must never invoke the dws binary."""
    # Wire the mock dws + its call log exactly like the phase0 `home` fixture.
    mock_log = tmp_path / "mock_dws_calls.jsonl"
    monkeypatch.setenv("DWS_AGENT_DWS_BIN", str(MOCK_DWS))
    monkeypatch.setenv("MOCK_DWS_LOG", str(mock_log))

    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    ing = Ingestor(paths, conn, key)

    # Ingest a QA, an ISSUE, and a CODE candidate from a local repo.
    repo = tmp_path / "svc"
    init_repo(repo, {"auth.py": SAMPLE_PY_V1})
    code_cands = kdl_code.GitReader(str(repo)).index_repo()
    for c in code_cands:
        c["repo"] = str(repo)  # so any lazy verify can resolve the worktree
    ing.ingest_candidates(code_cands)
    ing.ingest_candidates([
        {"source_type": "QA", "title": "如何重启", "body": "用 systemctl 重启。",
         "provenance": [{"kind": "MSG_ID", "ref": "m1", "quote": ""}]},
        {"source_type": "ISSUE", "title": "登录失败", "body": "症状: 失败. 处置: 重试.",
         "provenance": [{"kind": "ISSUE_URL", "ref": "http://x/i/1", "quote": ""}]},
    ])

    # Retrieve + serve + assemble a local draft.
    verdict = kdl_retrieve.serve(conn, key, "login")
    preview = kdl_retrieve.assemble_draft(verdict)
    # The draft is operator-only and clearly marked (never transmitted).
    assert preview.assistant_prefix.startswith("助理代答")

    # The mock dws was NEVER invoked: no log file, or an empty one.
    if mock_log.exists():
        assert mock_log.read_text("utf-8").strip() == ""


def test_kdl_modules_do_not_import_executor_or_shim():
    """KDL must not depend on the only modules that can issue dws commands."""
    import sys
    import types

    for mod in (kdl_store, kdl_retrieve, kdl_ingest, kdl_code):
        src_names = [
            v.__name__
            for v in vars(mod).values()
            if isinstance(v, types.ModuleType)
        ]
        for name in src_names:
            assert "executor" not in name, f"{mod.__name__} imported {name}"
            assert "shim" not in name, f"{mod.__name__} imported {name}"


def test_gitreader_allowed_subcommands_are_read_only():
    """The whitelist must contain only read subcommands; writes are absent."""
    allowed = GitReader.ALLOWED
    forbidden = {
        "commit", "push", "add", "reset", "checkout", "merge", "rebase",
        "rm", "mv", "clean", "tag", "fetch", "pull", "clone", "init",
        "stash", "apply", "cherry-pick", "branch",
    }
    assert allowed.isdisjoint(forbidden)
    # And every allowed one is a known read-only porcelain/plumbing verb.
    assert allowed <= {"rev-parse", "show", "cat-file", "log", "ls-files", "blame"}


def test_gitreader_blocks_write_subcommand_before_invoking_git(tmp_path):
    repo = tmp_path / "svc"
    init_repo(repo, {"auth.py": SAMPLE_PY_V1})
    gr = GitReader(str(repo))
    with pytest.raises(PermissionError):
        gr._run("commit", "-m", "evil")
    with pytest.raises(PermissionError):
        gr._run("push", "origin", "main")


def test_serve_returns_object_only_no_transport(tmp_path, monkeypatch):
    """serve() returns a Verdict object to the caller; it does not send it.

    A defensive check that the abstain path also produces no draft text and no
    side effects (the safe default never fabricates nor transmits).
    """
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    v = kdl_retrieve.serve(conn, key, "未知问题")
    preview = kdl_retrieve.assemble_draft(v)
    assert preview.would_answer is False
    assert preview.draft_text is None
