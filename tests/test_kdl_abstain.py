"""Exit-condition 3: abstain (绝不编).

serve() must return Verdict=ABSTAIN (and assemble_draft must produce NO answer
text) when:
  * nothing is retrieved (no_hit);
  * all hits are unconfirmed DRAFT (all_draft);
  * the best hit is STALE with no FRESH alternative (best_stale_no_fresh);
  * relevance is double-low (low_relevance).
In every abstain case the draft is None — the layer never fabricates an answer.
"""
from __future__ import annotations

from dws_agent.kdl import retrieve, store
from dws_agent.kdl.model import (
    Authority,
    Freshness,
    KnowledgeUnit,
    Provenance,
    ProvKind,
    SourceType,
    Taint,
    VerdictDecision,
    make_ku_id,
)

from kdl_helpers import make_paths, open_kdl


def _ku(title, body, *, authority=Authority.REVIEWED, freshness=Freshness.FRESH,
        ref="doc-1"):
    prov = [Provenance(kind=ProvKind.DOC_ID, ref=ref, quote="")]
    return KnowledgeUnit(
        ku_id=make_ku_id(SourceType.QA, ref, None, None) + "-" + title[:6],
        source_type=SourceType.QA,
        title=title,
        body=body,
        body_redacted=True,
        taint=Taint.CLEAN,
        authority=authority,
        public_ok=False,
        confidence=0.5,
        freshness=freshness,
        provenance=prov,
    )


def _decision(v):
    return retrieve._val(v.decision)


def test_abstain_when_no_hit(tmp_path, monkeypatch):
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    v = retrieve.serve(conn, key, "完全不存在的主题 zzz")
    assert _decision(v) == VerdictDecision.ABSTAIN.value
    assert v.reason == "no_hit"
    preview = retrieve.assemble_draft(v)
    assert preview.would_answer is False
    assert preview.draft_text is None
    assert preview.abstain_reason


def test_abstain_when_all_hits_are_draft(tmp_path, monkeypatch):
    """Unconfirmed (DRAFT) answers can be retrieved but must not be answerable."""
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    # Ingested-shape KU but never confirmed => DRAFT.
    store.upsert_ku(conn, _ku("restart the gateway service",
                              "Restart the gateway service via systemctl.",
                              authority=Authority.DRAFT), key)
    v = retrieve.serve(conn, key, "restart gateway service")
    # DRAFT KUs are serve_blocked, so they are dropped by the hard gate before
    # ranking: either way the verdict is ABSTAIN and no draft is produced — an
    # unconfirmed answer can never support an answerable verdict.
    assert _decision(v) == VerdictDecision.ABSTAIN.value
    assert v.reason in ("all_draft", "no_hit")
    assert retrieve.assemble_draft(v).draft_text is None


def test_abstain_when_best_is_stale_no_fresh(tmp_path, monkeypatch):
    """A single STALE hit with no FRESH alternative => abstain (degrade)."""
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    store.upsert_ku(conn, _ku("token refresh window expiry",
                              "The token refresh window is 30 minutes.",
                              authority=Authority.REVIEWED,
                              freshness=Freshness.STALE), key)
    v = retrieve.serve(conn, key, "token refresh window expiry")
    assert _decision(v) == VerdictDecision.ABSTAIN.value
    assert v.reason == "best_stale_no_fresh"
    assert retrieve.assemble_draft(v).draft_text is None


def test_abstain_on_commitment_marker_query(tmp_path, monkeypatch):
    """Queries about 承诺/对外口径/decisions defer to triage => abstain in phase1.

    Even with a perfectly good, fresh, authoritative KU present, a query that
    asks for an external commitment / official 口径 must NOT be auto-answered.
    """
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    store.upsert_ku(conn, _ku("上线时间安排",
                              "灰度上线计划在本周五完成。",
                              authority=Authority.AUTHORITATIVE,
                              freshness=Freshness.FRESH), key)
    v = retrieve.serve(conn, key, "对外口径上线时间能不能承诺这周五")
    assert _decision(v) == VerdictDecision.ABSTAIN.value
    assert v.reason == "commitment_marker"
    assert retrieve.assemble_draft(v).draft_text is None


def test_abstain_when_top_provenance_broken(tmp_path, monkeypatch):
    """If the top candidate's source pointer no longer resolves => abstain.

    A broken (non-retrievable) provenance means the claim can no longer be
    traced, so the layer refuses to answer from it (绝不编 on stale evidence).
    """
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    store.upsert_ku(conn, _ku("how to revoke an api token",
                              "Revoke an API token from the credentials page.",
                              authority=Authority.AUTHORITATIVE,
                              freshness=Freshness.FRESH, ref="doc-revoke"), key)
    # Find it, then break its provenance link (simulating a broken-link recheck).
    ku_id = next(iter(store.iter_kus(conn, key))).ku_id
    store.recheck_retrievable(conn, ku_id, ok=False)

    v = retrieve.serve(conn, key, "revoke api token")
    assert _decision(v) == VerdictDecision.ABSTAIN.value
    # broken provenance pushes the KU to EXPIRED+serve_blocked, so it is dropped
    # by the hard gate (no_hit) or flagged broken_provenance — both abstain.
    assert v.reason in ("broken_provenance", "no_hit", "all_expired")
    assert retrieve.assemble_draft(v).draft_text is None


def test_answerable_when_fresh_confirmed_and_relevant(tmp_path, monkeypatch):
    """Control: a FRESH, AUTHORITATIVE, relevant hit IS answerable (sanity)."""
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    store.upsert_ku(conn, _ku("how to reset the staging database",
                              "Reset the staging database by running the reset script.",
                              authority=Authority.AUTHORITATIVE,
                              freshness=Freshness.FRESH), key)
    v = retrieve.serve(conn, key, "reset staging database")
    assert _decision(v) == VerdictDecision.ANSWERABLE.value
