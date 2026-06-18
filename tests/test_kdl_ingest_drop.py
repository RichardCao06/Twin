"""Exit-condition 1 (ingest side): candidates without provenance are DROPPED
and audited (never silently locked into the store as answerable).

The Ingestor is the no-LLM, rule-based gate: a candidate with zero usable
provenance is rejected with reason ``no_provenance`` and an audit event
(``event='privacy_filter'``, ``decision='drop'``) is written. This guarantees a
sourceless "fact" can never enter the knowledge base at all.
"""
from __future__ import annotations

import json

from dws_agent.kdl import store
from dws_agent.kdl.ingest import Ingestor

from kdl_helpers import make_paths, open_kdl


def _audit_lines(paths):
    out = []
    for p in sorted(paths.audit_dir.glob("audit-*.jsonl")):
        out += [json.loads(x) for x in p.read_text("utf-8").splitlines() if x.strip()]
    return out


def test_no_provenance_candidate_is_dropped_and_audited(tmp_path, monkeypatch):
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    ing = Ingestor(paths, conn, key)

    rep = ing.ingest_candidates([
        {"source_type": "QA", "title": "no source", "body": "answer", "provenance": []},
    ])
    assert rep.ingested == []
    assert rep.dropped and rep.dropped[0][0] == "no_provenance"
    # Nothing landed in the store.
    assert list(store.iter_kus(conn, key)) == []

    # A privacy_filter drop event was audited.
    drops = [
        r for r in _audit_lines(paths)
        if r.get("event") == "privacy_filter" and r.get("decision") == "drop"
    ]
    assert any(r.get("reason") == "no_provenance" for r in drops)


def test_good_and_bad_candidates_partition_correctly(tmp_path, monkeypatch):
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    ing = Ingestor(paths, conn, key)

    rep = ing.ingest_candidates([
        {"source_type": "QA", "title": "ok", "body": "good answer",
         "provenance": [{"kind": "MSG_ID", "ref": "m1", "quote": ""}]},
        {"source_type": "QA", "title": "bad", "body": "orphan", "provenance": []},
        {"source_type": "NOPE", "title": "bad type", "body": "x",
         "provenance": [{"kind": "MSG_ID", "ref": "m2"}]},
    ])
    assert len(rep.ingested) == 1
    reasons = {d[0] for d in rep.dropped}
    assert "no_provenance" in reasons
    assert "bad_source_type" in reasons
