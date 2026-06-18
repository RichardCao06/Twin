"""Exit-condition 1 + 2: provenance hard rule and citation traceability.

1. A KU with NO provenance is locked to DRAFT + serve_blocked and can never
   back an ANSWERABLE verdict (the retrieval layer must not cite it).
2. Every citation in a Verdict points back to a real, stored KU and a real
   source identifier (kind + ref) — i.e. results are traceable.
"""
from __future__ import annotations

import pytest

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


def _ku(
    *,
    title,
    body,
    source_type=SourceType.QA,
    authority=Authority.REVIEWED,
    freshness=Freshness.FRESH,
    provenance=None,
    symbol=None,
    content_hash=None,
    repo=None,
    file_path=None,
):
    if provenance is None:
        provenance = [Provenance(kind=ProvKind.MSG_ID, ref="msg-x", quote="")]
    prov_ref = provenance[0].ref if provenance else ""
    return KnowledgeUnit(
        ku_id=make_ku_id(source_type, prov_ref, symbol, content_hash),
        source_type=source_type,
        title=title,
        body=body,
        body_redacted=True,
        taint=Taint.CLEAN,
        authority=authority,
        public_ok=False,
        confidence=0.5,
        freshness=freshness,
        provenance=provenance,
        symbol=symbol,
        content_hash=content_hash,
        repo=repo,
        file_path=file_path,
    )


def test_no_provenance_locks_draft_and_serve_blocked():
    """A provenance-less KU is forced DRAFT + serve_blocked by the model itself."""
    ku = KnowledgeUnit(
        ku_id="KU-noprov",
        source_type=SourceType.QA,
        title="orphan",
        body="answer with no source",
        body_redacted=True,
        taint=Taint.CLEAN,
        authority=Authority.AUTHORITATIVE,  # caller TRIES to make it authoritative
        public_ok=True,
        confidence=0.99,
        freshness=Freshness.FRESH,
        provenance=[],  # <-- no provenance
    )
    # The model overrides the caller: locked to DRAFT, serve_blocked, cannot serve.
    assert ku.authority == Authority.DRAFT
    assert ku.serve_blocked is True
    assert ku.can_serve() is False


def test_no_provenance_ku_never_backs_answerable_verdict(tmp_path, monkeypatch):
    """serve() must not cite a provenance-less (locked DRAFT) KU as answerable."""
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)

    # One strong, well-sourced KU and one orphan KU sharing the query terms.
    good = _ku(
        title="deploy procedure dws",
        body="To deploy run the dws release pipeline and confirm the gate.",
        authority=Authority.AUTHORITATIVE,
        provenance=[Provenance(kind=ProvKind.DOC_ID, ref="doc-7", quote="")],
    )
    orphan = _ku(title="deploy procedure dws orphan", body="deploy something", provenance=[])
    store.upsert_ku(conn, good, key)
    store.upsert_ku(conn, orphan, key)

    verdict = retrieve.serve(conn, key, "deploy procedure dws")
    cited_ids = {retrieve._val(getattr(c, "ku_id", None)) for c in verdict.citations}
    # The orphan is serve_blocked and must never be cited in an answerable verdict.
    assert orphan.ku_id not in cited_ids


def test_every_citation_traces_back_to_a_real_ku(tmp_path, monkeypatch):
    """Each citation must resolve to a stored KU and carry a real kind+ref."""
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)

    ku = _ku(
        title="how to rotate the access key",
        body="Rotate the access key via the security console then update secrets.",
        authority=Authority.AUTHORITATIVE,
        provenance=[Provenance(kind=ProvKind.DOC_ID, ref="doc-rotate-42", quote="")],
    )
    store.upsert_ku(conn, ku, key)

    verdict = retrieve.serve(conn, key, "rotate access key")
    assert verdict.decision == VerdictDecision.ANSWERABLE
    assert verdict.citations
    for c in verdict.citations:
        ku_id = c.ku_id if not isinstance(c, dict) else c["ku_id"]
        # Citation resolves to a real persisted KU.
        loaded = store.get_ku(conn, ku_id, key)
        assert loaded is not None
        # Citation carries a real source identifier (no body / no raw quote).
        prov_ref = c.prov_ref if not isinstance(c, dict) else c["prov_ref"]
        prov_kind = c.prov_kind if not isinstance(c, dict) else c["prov_kind"]
        assert prov_ref
        assert retrieve._val(prov_kind)
        # The ref matches the KU's actual stored provenance pointer.
        assert prov_ref in {p.ref for p in loaded.provenance}
        # Citation must NOT leak body or quote.
        assert not hasattr(c, "body")
        assert not hasattr(c, "quote")
