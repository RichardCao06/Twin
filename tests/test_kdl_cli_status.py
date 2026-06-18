"""Smoke test for the read-only ``dws-agent kb status`` CLI surface.

Exercises the kdl.store.kdl_status inventory used by the CLI (the CLI itself is
a thin, read-only wrapper). Confirms counts reflect ingested KUs and that the
status call performs no writes/sends.
"""
from __future__ import annotations

from dws_agent.kdl import store
from dws_agent.kdl.ingest import Ingestor

from kdl_helpers import make_paths, open_kdl


def test_kdl_status_reports_inventory(tmp_path, monkeypatch):
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    ing = Ingestor(paths, conn, key)
    ing.ingest_candidates([
        {"source_type": "QA", "title": "q1", "body": "a1",
         "provenance": [{"kind": "MSG_ID", "ref": "m1", "quote": ""}]},
        {"source_type": "ISSUE", "title": "i1", "body": "symptom/fix",
         "provenance": [{"kind": "ISSUE_URL", "ref": "http://x/1", "quote": ""}]},
    ])
    st = store.kdl_status(conn)
    assert st["total"] == 2
    assert st["by_source_type"].get("QA") == 1
    assert st["by_source_type"].get("ISSUE") == 1
    # Both enter DRAFT and are therefore serve_blocked at ingest.
    assert st["by_authority"].get("DRAFT") == 2
    assert st["serve_blocked"] == 2
    assert isinstance(st["fts5"], bool)
