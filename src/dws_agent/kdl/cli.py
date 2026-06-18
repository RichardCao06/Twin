"""``dws-agent kb`` subcommands — the KDL (Knowledge Distillation Layer) CLI.

This module wires the operator-facing knowledge-base commands:

    dws-agent kb ingest   --input <candidates.json>   distill + persist KUs
    dws-agent kb reindex  --repo <path>               refresh CODE-KU freshness
    dws-agent kb search   --query "..." [--external]   structured Verdict
    dws-agent kb draft    --query "..."                local 'if-I-answered' preview
    dws-agent kb status                                store summary

HARD CONSTRAINTS (enforced structurally here):
- KDL is **read-only** with respect to the outside world. NONE of these
  commands send a message, post a reply, or issue any ``dws`` *write* command.
  ``draft`` only assembles a LOCAL preview and prints it to the operator's
  stdout, clearly banner-marked ``LOCAL PREVIEW — NOT SENT``.
- Distillation goes through a pluggable backend (default a deterministic,
  offline ``stub``); the LLM (if any) only emits *candidate JSON*, never writes
  to the store. The rule-based, no-LLM :class:`Ingestor` is what actually
  persists, after redaction + taint propagation.
- Provenance is mandatory: candidates lacking provenance are dropped by the
  Ingestor (audited ``privacy_filter`` / ``no_provenance``); KUs that somehow
  reach the store without provenance are locked ``DRAFT`` + ``serve_blocked``.
- ``search``/``draft`` never print KU bodies or raw quotes — only source
  identifiers (ku_id / source_type / authority / freshness / prov kind+ref).
  The decrypted body is used only by the *local* draft assembler.
- ABSTAIN is the safe default; on abstain ``draft`` prints the machine reason
  and **never fabricates** an answer.

All sibling KDL imports are lazy (inside functions) so this module imports
cleanly during incremental development and so unit tests can stub pieces.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List, Optional


# --------------------------------------------------------------------------- #
# shared helpers (mirror cli/main.py conventions; kept local to avoid a hard
# import dependency on the phase0 main module)
# --------------------------------------------------------------------------- #
def _load_paths():
    """Return the core.paths Paths object rooted at $DWS_AGENT_HOME.

    Mirrors ``cli.main._load_paths``; lazily imports core.paths so this module
    stays importable even if core is mid-development.
    """
    from dws_agent.core.paths import get_paths  # type: ignore

    return get_paths()


def _audit(paths, *, kdl_op: str, reason: str = "", detail: Optional[dict] = None) -> None:
    """Best-effort audit of a kb subcommand.

    Uses the closed phase0 audit vocabulary: ``event='cli'`` / ``actor='cli'``.
    The KDL-specific verb lives in ``detail['kdl_op']`` (the audit event enum is
    closed; no new event names are introduced). Never raises.
    """
    rec_detail = {"kdl_op": kdl_op}
    if detail:
        rec_detail.update(detail)
    try:
        from dws_agent.store.audit import AuditLogger  # type: ignore

        AuditLogger(paths).log(
            {
                "event": "cli",
                "actor": "cli",
                "decision": None,
                "level": None,
                "reason": reason or ("kb %s" % kdl_op),
                "detail": rec_detail,
            }
        )
    except Exception:
        pass


def _open_conn(paths):
    """Open the shared state.db and ensure KDL tables exist (idempotent)."""
    from dws_agent.store.state_db import open_state_db  # type: ignore
    from dws_agent.kdl.store import ensure_kdl_schema  # type: ignore

    conn = open_state_db(paths)
    ensure_kdl_schema(conn)
    return conn


def _enc_key():
    """Return the 32-byte file-encryption key for KU body/quote crypto.

    In CI (DWS_AGENT_TEST_MODE=1) core.crypto returns a deterministic fallback.
    """
    from dws_agent.core.crypto import get_keychain_secret  # type: ignore

    return get_keychain_secret("fileenc")


def _get(obj, key, default=None):
    """Attribute/dict accessor that works for both dataclasses and dicts."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _enum_val(v) -> Any:
    """Render an Enum as its ``.value`` (str-Enums) else the value itself."""
    return getattr(v, "value", v)


def _fmt_citation(c) -> str:
    """Render a Citation tuple/obj as a single source-identifier line.

    NEVER includes body or raw quote — only ku_id / type / authority /
    freshness / provenance pointer / score.
    """
    ku_id = _get(c, "ku_id")
    src = _enum_val(_get(c, "source_type"))
    auth = _enum_val(_get(c, "authority"))
    fresh = _enum_val(_get(c, "freshness"))
    pk = _enum_val(_get(c, "prov_kind"))
    pref = _get(c, "prov_ref")
    score = _get(c, "score")
    # tuple fallback: Citation=(ku_id, source_type, authority, freshness,
    # prov_kind, prov_ref, score)
    if ku_id is None and isinstance(c, (tuple, list)) and len(c) >= 7:
        ku_id, src, auth, fresh, pk, pref, score = (
            c[0], _enum_val(c[1]), _enum_val(c[2]), _enum_val(c[3]),
            _enum_val(c[4]), c[5], c[6],
        )
    try:
        sc = "%.3f" % float(score) if score is not None else "n/a"
    except (TypeError, ValueError):
        sc = str(score)
    return "  - %s [%s/%s/%s] %s:%s  score=%s" % (
        ku_id, src, auth, fresh, pk, pref, sc,
    )


# --------------------------------------------------------------------------- #
# kb ingest
# --------------------------------------------------------------------------- #
def cmd_kb_ingest(args) -> int:
    """Distill candidates from ``--input`` JSON and persist them as KUs.

    Pipeline (no outward send, no dws write):
      1. Load the raw source/candidate JSON from ``--input`` (a fixture or a
         pre-extracted source dump). Read-only file access.
      2. Run the pluggable distiller (default offline ``stub``) to obtain a list
         of *candidate dicts*. The distiller NEVER writes to the store.
      3. Hand the candidates to the rule-based :class:`Ingestor`, which redacts
         + taint-tags every body/quote, enforces the provenance hard-rule
         (drop / lock DRAFT), and upserts. Returns an :class:`IngestReport`.
      4. Print the report. Audit ``kdl_op='ingest'``.
    """
    paths = _load_paths()
    input_path = args.input
    backend = getattr(args, "distiller", None)

    try:
        raw_text = _read_text(input_path)
        source = json.loads(raw_text)
    except Exception as exc:
        print("ERROR: cannot read/parse --input %s: %s" % (input_path, exc),
              file=sys.stderr)
        _audit(paths, kdl_op="ingest", reason="input read failed: %s" % exc,
               detail={"input": str(input_path)})
        return 2

    try:
        from dws_agent.kdl.distill import get_distiller, RawItem  # type: ignore
        from dws_agent.kdl.ingest import Ingestor  # type: ignore
    except Exception as exc:
        print("ERROR: KDL ingest modules unavailable: %s" % exc, file=sys.stderr)
        return 3

    conn = _open_conn(paths)
    try:
        distiller = get_distiller(backend)  # backend=None -> env default 'stub'
        # The input JSON is a *source dump*: either a single source item or a
        # list of them. Each item is normalized into a RawItem and run through
        # the (pure, no-store) distiller; candidates are aggregated. The
        # distiller NEVER writes to the store — only the Ingestor does.
        items = source if isinstance(source, list) else [source]
        candidates: List[Any] = []
        for it in items:
            raw = _to_raw_item(it, RawItem)
            if raw is None:
                continue
            candidates.extend(distiller.distill(raw))
        ingestor = Ingestor(paths, conn, _enc_key())
        report = ingestor.ingest_candidates(candidates)
    except Exception as exc:
        print("ERROR: ingest failed: %s" % exc, file=sys.stderr)
        _audit(paths, kdl_op="ingest", reason="ingest failed: %s" % exc)
        return 4

    ku_ids = _get(report, "ingested", None) or []
    dropped_pairs = _get(report, "dropped", None) or []
    redacted = _get(report, "redacted_count", 0)
    n_ingested = len(ku_ids)
    n_dropped = len(dropped_pairs)
    print("kb ingest: distiller=%s input=%s"
          % (backend or "stub", input_path))
    print("  candidates: %d" % len(candidates))
    print("  ingested:   %d" % n_ingested)
    print("  dropped:    %d (e.g. no_provenance / sensitive filter)" % n_dropped)
    print("  redacted:   %d (body/quote redaction fired)" % (redacted or 0))
    if ku_ids:
        print("  ku_ids:")
        for kid in ku_ids:
            print("    %s" % kid)
    if dropped_pairs:
        print("  drop reasons:")
        for r in dropped_pairs:
            # r is a (reason, title) tuple
            if isinstance(r, (tuple, list)) and len(r) >= 2:
                print("    - %s: %s" % (r[0], r[1]))
            else:
                print("    - %s" % (r,))

    _audit(paths, kdl_op="ingest",
           reason="ingested=%d dropped=%d" % (n_ingested, n_dropped),
           detail={"input": str(input_path), "distiller": backend or "stub",
                   "ingested": n_ingested, "dropped": n_dropped})
    return 0


# --------------------------------------------------------------------------- #
# kb reindex
# --------------------------------------------------------------------------- #
def cmd_kb_reindex(args) -> int:
    """Refresh CODE-KU freshness for a repo against its current HEAD.

    Delegates to ``kdl.code.reindex_repo(conn, repo_path, key, ...)`` which is
    READ-ONLY w.r.t. git (rev-parse/show/cat-file only). Each existing CODE-KU
    has its symbol re-extracted: gone => EXPIRED + serve_blocked; changed hash
    => STALE (AUTHORITATIVE auto-downgraded to REVIEWED); identical => FRESH.
    Prints the resulting counts. Issues NO writes to git or to dws.
    """
    paths = _load_paths()
    repo = args.repo
    try:
        from dws_agent.kdl.code import GitReader  # type: ignore
    except Exception as exc:
        print("ERROR: KDL code module unavailable: %s" % exc, file=sys.stderr)
        return 3

    conn = _open_conn(paths)
    try:
        reader = GitReader(repo)
        result = reader.reindex_repo(conn, _enc_key(), repo)
    except Exception as exc:
        print("ERROR: reindex failed for %s: %s" % (repo, exc), file=sys.stderr)
        _audit(paths, kdl_op="reindex", reason="reindex failed: %s" % exc,
               detail={"repo": str(repo)})
        return 4

    head = _get(result, "head_sha") or _get(result, "head_commit")
    examined = _get(result, "checked", _get(result, "examined", 0))
    fresh = _get(result, "fresh", 0)
    stale = _get(result, "stale", 0)
    expired = _get(result, "expired", 0)
    downgraded = _get(result, "downgraded", 0)
    print("kb reindex: repo=%s HEAD=%s" % (repo, head))
    print("  CODE-KUs examined: %s" % examined)
    print("  fresh:      %s" % fresh)
    print("  stale:      %s" % stale)
    print("  expired:    %s (symbol/file gone -> serve_blocked)" % expired)
    print("  downgraded: %s (AUTHORITATIVE -> REVIEWED on drift)" % downgraded)

    _audit(paths, kdl_op="reindex",
           reason="head=%s stale=%s expired=%s" % (head, stale, expired),
           detail={"repo": str(repo), "head": head, "stale": stale,
                   "expired": expired})
    return 0


# --------------------------------------------------------------------------- #
# kb search
# --------------------------------------------------------------------------- #
def cmd_kb_search(args) -> int:
    """Run retrieval and print a structured Verdict (no bodies, no quotes).

    Calls ``kdl.retrieve.serve(conn, query, key, external_facing=...)`` and
    prints decision / reason / confidence + citation source-identifiers only.
    ``--external`` sets external_facing=True (excludes non-CLEAN taint, the
    分库 gate); default operator-only retrieval keeps SENSITIVE KUs visible to
    me but they remain flagged via their taint in the citation line.
    """
    paths = _load_paths()
    query = args.query
    external = bool(getattr(args, "external", False))

    try:
        from dws_agent.kdl.retrieve import serve  # type: ignore
    except Exception as exc:
        print("ERROR: KDL retrieve module unavailable: %s" % exc, file=sys.stderr)
        return 3

    conn = _open_conn(paths)
    try:
        verdict = serve(conn, _enc_key(), query, external_facing=external)
    except Exception as exc:
        # abstain-on-exception: never fabricate, surface the abstain instead.
        print("DECISION: ABSTAIN")
        print("  reason:     internal_error")
        print("  confidence: 0.000")
        print("  (%s)" % exc, file=sys.stderr)
        _audit(paths, kdl_op="search", reason="serve raised: %s" % exc,
               detail={"external": external})
        return 0

    decision = _enum_val(_get(verdict, "decision"))
    reason = _get(verdict, "reason")
    conf = _get(verdict, "confidence")
    citations = _get(verdict, "citations") or []
    try:
        confs = "%.3f" % float(conf) if conf is not None else "n/a"
    except (TypeError, ValueError):
        confs = str(conf)

    print("DECISION: %s" % decision)
    print("  query:      %s" % query)
    print("  external:   %s" % external)
    print("  reason:     %s" % reason)
    print("  confidence: %s" % confs)
    if citations:
        print("  citations (%d) — source identifiers only, NO body/quote:"
              % len(citations))
        for c in citations:
            print(_fmt_citation(c))
    else:
        print("  citations: (none)")

    _audit(paths, kdl_op="search",
           reason="decision=%s reason=%s" % (decision, reason),
           detail={"external": external, "decision": decision,
                   "verdict_reason": reason, "n_citations": len(citations)})
    return 0


# --------------------------------------------------------------------------- #
# kb draft
# --------------------------------------------------------------------------- #
def cmd_kb_draft(args) -> int:
    """Assemble and print a LOCAL 'if-I-answered' preview (NEVER sent).

    Calls ``serve`` then ``assemble_draft``. On ABSTAIN, ``draft_text`` is None
    and the abstain reason is printed (绝不编). The output is fenced by a loud
    ``LOCAL PREVIEW — NOT SENT`` banner and carries the operator-review prefix
    plus citation source identifiers. Nothing leaves this process.
    """
    paths = _load_paths()
    query = args.query
    external = bool(getattr(args, "external", False))

    try:
        from dws_agent.kdl.retrieve import serve, assemble_draft  # type: ignore
    except Exception as exc:
        print("ERROR: KDL retrieve module unavailable: %s" % exc, file=sys.stderr)
        return 3

    conn = _open_conn(paths)
    try:
        key = _enc_key()
        verdict = serve(conn, key, query, external_facing=external)
        preview = assemble_draft(verdict, key=key)
    except Exception as exc:
        _print_draft_banner()
        print("  would_answer: False")
        print("  abstain_reason: internal_error")
        print("  (%s)" % exc, file=sys.stderr)
        _print_draft_footer()
        _audit(paths, kdl_op="draft", reason="draft raised: %s" % exc,
               detail={"external": external})
        return 0

    would = bool(_get(preview, "would_answer", False))
    draft_text = _get(preview, "draft_text")
    prefix = _get(preview, "assistant_prefix", "助理代答(待本人复核)")
    citations = _get(preview, "citations") or []
    abstain_reason = _get(preview, "abstain_reason")

    _print_draft_banner()
    print("  query:        %s" % query)
    print("  would_answer: %s" % would)
    if would and draft_text:
        print("  preview prefix: %s" % prefix)
        print("  --- draft (LOCAL, not sent) ---")
        for line in str(draft_text).splitlines() or [""]:
            print("  | %s" % line)
        print("  --- end draft ---")
    else:
        print("  ABSTAIN — no answer assembled (绝不编)")
        print("  abstain_reason: %s" % (abstain_reason or "abstain"))
    if citations:
        print("  citations (%d) — source identifiers only:" % len(citations))
        for c in citations:
            print(_fmt_citation(c))
    _print_draft_footer()

    _audit(paths, kdl_op="draft",
           reason="would_answer=%s abstain=%s" % (would, abstain_reason),
           detail={"external": external, "would_answer": would,
                   "abstain_reason": abstain_reason})
    return 0


def _print_draft_banner() -> None:
    print("=" * 60)
    print("  LOCAL PREVIEW — NOT SENT")
    print("  (KDL never transmits; this is an 'if-I-answered' draft for you)")
    print("=" * 60)


def _print_draft_footer() -> None:
    print("=" * 60)
    print("  END LOCAL PREVIEW — nothing was sent")
    print("=" * 60)


# --------------------------------------------------------------------------- #
# kb status
# --------------------------------------------------------------------------- #
def cmd_kb_status(args) -> int:
    """Pretty-print the KDL store summary via ``kdl.store.kdl_status``."""
    paths = _load_paths()
    try:
        from dws_agent.kdl.store import kdl_status  # type: ignore
    except Exception as exc:
        print("ERROR: KDL store module unavailable: %s" % exc, file=sys.stderr)
        return 3

    conn = _open_conn(paths)
    try:
        status = kdl_status(conn)
    except Exception as exc:
        print("ERROR: status failed: %s" % exc, file=sys.stderr)
        return 4

    print("KDL store status")
    print("  total KUs:     %s" % _get(status, "total", 0))
    by_src = _get(status, "by_source_type") or {}
    if by_src:
        print("  by source_type:")
        for k, v in by_src.items():
            print("    %-10s %s" % (str(_enum_val(k)) + ":", v))
    by_auth = _get(status, "by_authority") or {}
    if by_auth:
        print("  by authority:")
        for k, v in by_auth.items():
            print("    %-14s %s" % (str(_enum_val(k)) + ":", v))
    by_fresh = _get(status, "by_freshness") or {}
    if by_fresh:
        print("  by freshness:")
        for k, v in by_fresh.items():
            print("    %-10s %s" % (str(_enum_val(k)) + ":", v))
    print("  serve_blocked: %s" % _get(status, "serve_blocked", 0))
    print("  stale:         %s" % _get(status, "stale", 0))
    print("  expired:       %s" % _get(status, "expired", 0))
    print("  public_ok:     %s" % _get(status, "public_ok", 0))
    repos = _get(status, "indexed_commits") or _get(status, "last_indexed_commit")
    if repos:
        print("  last_indexed_commit:")
        if isinstance(repos, dict):
            for r, c in repos.items():
                print("    %s @ %s" % (r, c))
        else:
            print("    %s" % repos)

    _audit(paths, kdl_op="status", reason="status",
           detail={"total": _get(status, "total", 0)})
    return 0


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #
def _read_text(path: str) -> str:
    """Read a UTF-8 text file (read-only)."""
    from pathlib import Path

    return Path(path).read_text("utf-8")


def _to_raw_item(item: Any, RawItem):
    """Normalize one source-dump entry into a ``RawItem`` (or None to skip).

    Accepts the adapter-friendly shape ``{"source_type","text","meta"}``. If a
    flat dict is given without an explicit ``meta``, the remaining keys (minus
    source_type/text) are treated as meta so fixtures can stay terse. Items
    that are not dicts or lack a source_type are skipped (the Ingestor would
    drop them anyway, but skipping here keeps the candidate list clean).
    """
    if not isinstance(item, dict):
        return None
    st = item.get("source_type")
    if not st:
        return None
    text = str(item.get("text") or "")
    meta = item.get("meta")
    if not isinstance(meta, dict):
        meta = {k: v for k, v in item.items()
                if k not in ("source_type", "text", "meta")}
    return RawItem(source_type=str(st).upper(), text=text, meta=meta)


# --------------------------------------------------------------------------- #
# argparse wiring — register_kb(subparsers)
# --------------------------------------------------------------------------- #
def register_kb(subparsers) -> None:
    """Attach the ``kb`` command group to an existing ``add_subparsers`` object.

    Called from ``cli.main._build_parser`` (lazily, non-fatally). Adds a ``kb``
    parser with nested subcommands ingest/reindex/search/draft/status, each
    wired via ``set_defaults(func=...)`` to the ``cmd_kb_*`` handlers above.
    """
    p_kb = subparsers.add_parser(
        "kb",
        help="knowledge-base (KDL): ingest/reindex/search/draft/status (read-only)",
        description=(
            "Knowledge Distillation Layer — long-term, citable memory. "
            "Read-only: never sends anything, never issues a dws write command."
        ),
    )
    kb_sub = p_kb.add_subparsers(dest="kb_command", required=True)

    p_ing = kb_sub.add_parser(
        "ingest", help="distill candidates from a source/fixture JSON and persist KUs")
    p_ing.add_argument("--input", required=True,
                       help="path to source/candidate JSON (fixture or dump)")
    p_ing.add_argument("--distiller", default=None,
                       help="distiller backend (default env DWS_AGENT_KDL_DISTILLER or 'stub')")
    p_ing.set_defaults(func=cmd_kb_ingest)

    p_re = kb_sub.add_parser(
        "reindex", help="refresh CODE-KU freshness against a repo's current HEAD (read-only git)")
    p_re.add_argument("--repo", required=True, help="path to the git repo working tree")
    p_re.set_defaults(func=cmd_kb_reindex)

    p_se = kb_sub.add_parser(
        "search", help="retrieve and print a structured Verdict (no bodies)")
    p_se.add_argument("--query", required=True, help="natural-language / symbol query")
    p_se.add_argument("--external", action="store_true",
                      help="external-facing retrieval: exclude non-CLEAN taint (分库)")
    p_se.set_defaults(func=cmd_kb_search)

    p_dr = kb_sub.add_parser(
        "draft", help="assemble a LOCAL 'if-I-answered' preview (NEVER sent)")
    p_dr.add_argument("--query", required=True, help="natural-language / symbol query")
    p_dr.add_argument("--external", action="store_true",
                      help="external-facing retrieval: exclude non-CLEAN taint (分库)")
    p_dr.set_defaults(func=cmd_kb_draft)

    p_st = kb_sub.add_parser("status", help="print KDL store summary")
    p_st.set_defaults(func=cmd_kb_status)
