"""Exit-conditions 4 + 5: code freshness / staleness + real-time symbol verify.

4. When a repo commit changes the referenced file/symbol, the affected CODE-KU
   is marked STALE (and AUTHORITATIVE auto-downgrades to REVIEWED); dependent
   ISSUE/QA KUs flag derived_stale via the edge graph.
5. No hallucination from drifted code: at query time a CODE-KU's symbol is
   re-verified against git. If the symbol is GONE the fact verifies false and
   the candidate is dropped/EXPIRED (=> abstain). If it merely drifted it is
   STALE (down-weighted) — the layer never answers from code that moved.

All git is a LOCAL throwaway repo (no network). GitReader only ever READS git.
"""
from __future__ import annotations

from pathlib import Path

from dws_agent.kdl import code as kdl_code
from dws_agent.kdl import retrieve, store
from dws_agent.kdl.code import GitReader, content_hash, extract_symbols
from dws_agent.kdl.model import (
    Authority,
    Freshness,
    KnowledgeUnit,
    Provenance,
    ProvKind,
    SourceType,
    Taint,
    make_ku_id,
)

from kdl_helpers import (
    SAMPLE_PY_V1,
    SAMPLE_PY_V2_DRIFT,
    SAMPLE_PY_V3_REMOVED,
    commit_all,
    init_repo,
    make_paths,
    open_kdl,
    write_files,
)


def _code_ku(repo_path: str, commit: str, file_path: str, symbol: str,
             chash: str, *, authority=Authority.REVIEWED,
             freshness=Freshness.FRESH):
    prov = [Provenance(kind=ProvKind.COMMIT, ref=commit, quote="")]
    return KnowledgeUnit(
        ku_id=make_ku_id(SourceType.CODE, commit, symbol, chash),
        source_type=SourceType.CODE,
        title=f"{file_path}::{symbol}",
        body=f"{symbol} defined in {file_path}.",
        body_redacted=True,
        taint=Taint.CLEAN,
        authority=authority,
        public_ok=False,
        confidence=0.7,
        freshness=freshness,
        provenance=prov,
        repo=repo_path,          # store the working-tree path so lazy verify resolves
        commit_sha=commit,
        file_path=file_path,
        symbol=symbol,
        content_hash=chash,
    )


def _login_hash(repo: Path, commit: str) -> str:
    gr = GitReader(str(repo))
    text = gr.read_at(commit, "auth.py")
    sym = [s for s in extract_symbols(text, lang_hint="auth.py") if s.name == "login"][0]
    return sym.content_hash


# --------------------------------------------------------------------------- #
# verify_fact: the real-time, read-only freshness check (tier 3).
# --------------------------------------------------------------------------- #
def test_verify_fact_fresh_then_stale_then_expired(tmp_path):
    repo = tmp_path / "svc"
    head1 = init_repo(repo, {"auth.py": SAMPLE_PY_V1})
    chash1 = _login_hash(repo, head1)
    gr = GitReader(str(repo))

    ku = _code_ku(str(repo), head1, "auth.py", "login", chash1)
    assert gr.verify_fact(ku) == Freshness.FRESH

    # Drift: change login()'s body and commit; old hash no longer matches.
    write_files(repo, {"auth.py": SAMPLE_PY_V2_DRIFT})
    head2 = commit_all(repo)
    ku_drift = _code_ku(str(repo), head2, "auth.py", "login", chash1)
    assert gr.verify_fact(ku_drift) == Freshness.STALE

    # Removal: delete login() entirely -> symbol gone -> EXPIRED.
    write_files(repo, {"auth.py": SAMPLE_PY_V3_REMOVED})
    head3 = commit_all(repo)
    ku_gone = _code_ku(str(repo), head3, "auth.py", "login", chash1)
    assert gr.verify_fact(ku_gone) == Freshness.EXPIRED


def test_symbol_exists_false_after_removal(tmp_path):
    repo = tmp_path / "svc"
    head1 = init_repo(repo, {"auth.py": SAMPLE_PY_V1})
    chash1 = _login_hash(repo, head1)
    write_files(repo, {"auth.py": SAMPLE_PY_V3_REMOVED})
    head2 = commit_all(repo)
    gr = GitReader(str(repo))
    ku = _code_ku(str(repo), head2, "auth.py", "login", chash1)
    assert gr.symbol_exists(ku) is False
    # logout still there at the new commit.
    ku_logout = _code_ku(str(repo), head2, "auth.py", "logout", "irrelevant-hash")
    assert gr.symbol_exists(ku_logout) is True


# --------------------------------------------------------------------------- #
# batch staleness + edge propagation (tier 2): store.mark_stale_by_file.
# --------------------------------------------------------------------------- #
def test_mark_stale_by_file_downgrades_and_propagates(tmp_path, monkeypatch):
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    repo = tmp_path / "svc"
    head1 = init_repo(repo, {"auth.py": SAMPLE_PY_V1})
    chash1 = _login_hash(repo, head1)

    # An AUTHORITATIVE CODE-KU on auth.py, plus a linked ISSUE KU.
    code_ku = _code_ku(str(repo), head1, "auth.py", "login", chash1,
                       authority=Authority.AUTHORITATIVE)
    store.upsert_ku(conn, code_ku, key)

    issue_prov = [Provenance(kind=ProvKind.ISSUE_URL, ref="http://x/issue/9", quote="")]
    issue_ku = KnowledgeUnit(
        ku_id="KU-issue-9",
        source_type=SourceType.ISSUE,
        title="login fails intermittently",
        body="Symptom: login fails. Root cause: race in login(). Fix: lock.",
        body_redacted=True,
        taint=Taint.CLEAN,
        authority=Authority.REVIEWED,
        public_ok=False,
        confidence=0.6,
        freshness=Freshness.FRESH,
        provenance=issue_prov,
    )
    store.upsert_ku(conn, issue_ku, key)
    store.add_edge(conn, issue_ku.ku_id, code_ku.ku_id, "ISSUE_CODE")

    affected = store.mark_stale_by_file(conn, str(repo), "auth.py")
    assert code_ku.ku_id in affected

    reloaded_code = store.get_ku(conn, code_ku.ku_id, key)
    # CODE-KU now STALE and AUTHORITATIVE auto-downgraded to REVIEWED.
    assert reloaded_code.freshness == Freshness.STALE
    assert reloaded_code.authority == Authority.REVIEWED

    reloaded_issue = store.get_ku(conn, issue_ku.ku_id, key)
    # Dependent ISSUE KU flagged derived_stale via the edge graph.
    assert reloaded_issue.derived_stale is True


# --------------------------------------------------------------------------- #
# Lazy verify at query time (tier 3 via serve): no hallucination from drift.
# --------------------------------------------------------------------------- #
def test_serve_abstains_when_code_symbol_gone(tmp_path, monkeypatch):
    """A stored CODE-KU whose symbol was deleted must NOT answer (abstain)."""
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    repo = tmp_path / "svc"
    head1 = init_repo(repo, {"auth.py": SAMPLE_PY_V1})
    chash1 = _login_hash(repo, head1)

    ku = _code_ku(str(repo), head1, "auth.py", "login", chash1,
                  authority=Authority.AUTHORITATIVE)
    store.upsert_ku(conn, ku, key)

    # Sanity: while the symbol exists, the CODE fact is answerable.
    v_ok = retrieve.serve(conn, key, "login")
    assert retrieve._val(v_ok.decision) == "ANSWERABLE"

    # Now delete login() and re-pin the KU's commit to the new HEAD so the lazy
    # verify re-extracts at a commit where the symbol is gone.
    write_files(repo, {"auth.py": SAMPLE_PY_V3_REMOVED})
    head2 = commit_all(repo)
    import dataclasses
    ku_gone = dataclasses.replace(ku, commit_sha=head2,
                                  provenance=[Provenance(kind=ProvKind.COMMIT,
                                                         ref=head2, quote="")])
    store.upsert_ku(conn, ku_gone, key)

    v = retrieve.serve(conn, key, "login")
    # Lazy verify sees the symbol gone -> EXPIRED -> dropped -> abstain.
    assert retrieve._val(v.decision) == "ABSTAIN"
    cited = {retrieve._val(getattr(c, "ku_id", None)) for c in v.citations}
    assert ku_gone.ku_id not in cited
    assert retrieve.assemble_draft(v).draft_text is None


def test_serve_downweights_drifted_code(tmp_path, monkeypatch):
    """When code drifts (symbol present, hash changed) the CODE-KU goes STALE.

    With no FRESH alternative the serve layer abstains rather than answering
    from drifted code.
    """
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    repo = tmp_path / "svc"
    head1 = init_repo(repo, {"auth.py": SAMPLE_PY_V1})
    chash1 = _login_hash(repo, head1)

    ku = _code_ku(str(repo), head1, "auth.py", "login", chash1,
                  authority=Authority.AUTHORITATIVE)
    store.upsert_ku(conn, ku, key)

    write_files(repo, {"auth.py": SAMPLE_PY_V2_DRIFT})
    head2 = commit_all(repo)
    import dataclasses
    ku_drift = dataclasses.replace(ku, commit_sha=head2,
                                   provenance=[Provenance(kind=ProvKind.COMMIT,
                                                          ref=head2, quote="")])
    store.upsert_ku(conn, ku_drift, key)

    v = retrieve.serve(conn, key, "login")
    # Lazy verify downgrades to STALE; only candidate, no FRESH alt -> abstain.
    assert retrieve._val(v.decision) == "ABSTAIN"
    assert retrieve.assemble_draft(v).draft_text is None


# --------------------------------------------------------------------------- #
# Read-only git guarantee.
# --------------------------------------------------------------------------- #
def test_gitreader_refuses_write_subcommands(tmp_path):
    repo = tmp_path / "svc"
    init_repo(repo, {"auth.py": SAMPLE_PY_V1})
    gr = GitReader(str(repo))
    import pytest
    for bad in ("commit", "push", "add", "reset", "checkout", "rm"):
        with pytest.raises(PermissionError):
            gr._run(bad, "--anything")
    # Allowed read subcommands do not raise on the guard.
    assert gr.head_sha()  # rev-parse is allowed
