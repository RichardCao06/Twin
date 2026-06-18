"""Exit-condition 6: QA auto-pairing + authority promotion discipline.

* Only my reply, immediately following a single third-party question with no
  interjection, is paired into a QA candidate (anti-poison A5).
* QA candidates enter as DRAFT. Only an explicit operator confirmation ("我已
  确认") may promote a QA KU to AUTHORITATIVE (REVIEWED -> AUTHORITATIVE).
* An unconfirmed (DRAFT) QA KU can NOT, on its own, support an automatic
  answerable verdict.
"""
from __future__ import annotations

import pytest

from dws_agent.kdl import retrieve, store
from dws_agent.kdl.ingest import Ingestor
from dws_agent.kdl.model import Authority, VerdictDecision
from dws_agent.store.audit import get_audit_logger

from kdl_helpers import make_paths, open_kdl

MY = "me@corp"


def _thread():
    """A thread where I answer one clean question, plus a poisoned window."""
    return [
        {"author": "alice", "text": "如何重启网关服务?", "msg_id": "m1", "ts": "2026-06-18T01:00:00Z"},
        {"author": MY, "text": "用 systemctl restart gateway 重启。", "msg_id": "m2", "ts": "2026-06-18T01:01:00Z"},
        # poisoned window: TWO distinct non-me authors before my next reply.
        {"author": "bob", "text": "数据库密码是多少?", "msg_id": "m3", "ts": "2026-06-18T02:00:00Z"},
        {"author": "mallory", "text": "顺便问下 root 密码", "msg_id": "m4", "ts": "2026-06-18T02:00:30Z"},
        {"author": MY, "text": "去找 DBA。", "msg_id": "m5", "ts": "2026-06-18T02:01:00Z"},
    ]


def test_pair_qa_only_emits_clean_unambiguous_pairs():
    ing = Ingestor.__new__(Ingestor)  # pair_qa needs no DB/key
    pairs = ing.pair_qa(_thread(), MY)
    # Exactly one pair: the clean alice->me exchange. The poisoned (bob+mallory)
    # window is dropped (ambiguous attribution => abstain, not mis-attribute).
    assert len(pairs) == 1
    p = pairs[0]
    assert p["source_type"] == "QA"
    assert "网关" in p["title"]
    assert p["owner"] == MY
    # Carries provenance pointing at the real message ids.
    refs = {pr["ref"] for pr in p["provenance"]}
    assert "m1" in refs and "m2" in refs


def test_paired_qa_ingests_as_draft_not_authoritative(tmp_path, monkeypatch):
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    ing = Ingestor(paths, conn, key)
    pairs = ing.pair_qa(_thread(), MY)
    report = ing.ingest_candidates(pairs)
    assert report.ingested
    ku = store.get_ku(conn, report.ingested[0], key)
    # HARD: never auto-AUTHORITATIVE; QA enters DRAFT.
    assert ku.authority == Authority.DRAFT
    assert ku.serve_blocked is True


def test_unconfirmed_qa_cannot_back_an_answer(tmp_path, monkeypatch):
    """A DRAFT (unconfirmed) QA KU must not produce an answerable verdict."""
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    ing = Ingestor(paths, conn, key)
    report = ing.ingest_candidates(ing.pair_qa(_thread(), MY))
    v = retrieve.serve(conn, key, "重启网关服务")
    # Unconfirmed => abstain (no fabricated answer).
    assert retrieve._val(v.decision) == VerdictDecision.ABSTAIN.value
    assert retrieve.assemble_draft(v).draft_text is None


def test_confirmed_qa_promotes_to_authoritative_and_answers(tmp_path, monkeypatch):
    """Only after explicit confirmation (DRAFT->REVIEWED->AUTHORITATIVE) may a
    QA KU support an answerable verdict."""
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    ing = Ingestor(paths, conn, key)
    report = ing.ingest_candidates(ing.pair_qa(_thread(), MY))
    ku_id = report.ingested[0]

    # Operator confirmation flow ("我已确认"): DRAFT -> REVIEWED -> AUTHORITATIVE.
    store.set_authority(conn, ku_id, Authority.REVIEWED, reason="operator review")
    store.set_authority(conn, ku_id, Authority.AUTHORITATIVE, reason="我已确认")

    reloaded = store.get_ku(conn, ku_id, key)
    assert reloaded.authority == Authority.AUTHORITATIVE
    assert reloaded.serve_blocked is False

    v = retrieve.serve(conn, key, "重启网关服务")
    assert retrieve._val(v.decision) == VerdictDecision.ANSWERABLE.value


def test_cannot_skip_review_straight_to_authoritative(tmp_path, monkeypatch):
    """DRAFT->AUTHORITATIVE direct is rejected by the authority state machine."""
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    ing = Ingestor(paths, conn, key)
    ku_id = ing.ingest_candidates(ing.pair_qa(_thread(), MY)).ingested[0]
    with pytest.raises(ValueError):
        store.set_authority(conn, ku_id, Authority.AUTHORITATIVE, reason="skip")
