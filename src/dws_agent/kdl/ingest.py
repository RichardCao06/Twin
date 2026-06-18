"""No-LLM deterministic Ingestor: candidate dicts -> persisted KnowledgeUnits.

The Ingestor is the *rule-based* half of the distill/ingest split. The
distiller (possibly an LLM in a future phase) only ever emits candidate JSON;
this module — with **no LLM, no network, no dws write, no outward send** —
validates each candidate against the KU schema, runs privacy redaction over the
body and every provenance quote, computes the merged taint, builds an immutable
:class:`~dws_agent.kdl.model.KnowledgeUnit` (authority forced to DRAFT at
ingest, provenance >=1 enforced else dropped), and persists it via
:func:`dws_agent.kdl.store.upsert_ku`. Every drop is audited.

Hard constraints enforced here (mirrors the global contract):

* **provenance >= 1** — a candidate with zero usable provenance is DROPPED and
  audited (``event='privacy_filter'``, ``reason='no_provenance'``). The model
  also re-locks to DRAFT + ``serve_blocked`` if provenance is somehow empty, so
  a missing pointer can never support an answer.
* **ingest = redact + taint** — bodies and quotes always pass through
  :func:`privacy.redaction.redact`; ``body_redacted=True``; the KU taint is the
  MAX of the redaction taint, every quote taint, and the declared taint
  (:func:`privacy.taint.propagate` — taint never washes down).
* **authority = DRAFT at ingest** — only operator-confirmed QA may later be
  promoted; the Ingestor never sets AUTHORITATIVE.
* **CODE is commit-bound** — CODE candidates carry repo/commit/file/symbol so
  the freshness loop (kdl.code) can detect drift; ISSUE/QA <-> CODE links are
  recorded as ``ku_edge`` rows for stale propagation.

Source adapters read from fixtures or R0 read-only message/issue dumps; they
NEVER perform live dws writes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from dws_agent.privacy.redaction import redact
from dws_agent.privacy.taint import propagate
from dws_agent.store.audit import get_audit_logger

# Accepted enum string values (mirror dws_agent.kdl.model enums; kept as plain
# strings here so ingest does not hard-fail if model import order shifts — the
# canonical enums live in model.py and are used when building the KU).
_VALID_SOURCE_TYPES = {"CODE", "ISSUE", "QA", "PLAYBOOK"}
_VALID_PROV_KINDS = {"COMMIT", "ISSUE_URL", "MSG_ID", "DOC_ID", "MAIL_ID", "FILE"}
_VALID_TAINTS = {"CLEAN", "INTERNAL", "SENSITIVE"}


@dataclass
class IngestReport:
    """Outcome of an :meth:`Ingestor.ingest_candidates` run.

    Attributes:
        ingested: ku_ids successfully upserted.
        dropped: ``(reason, title)`` pairs for rejected candidates.
        redacted_count: number of candidates whose body/quote redaction
            actually fired (at least one hit), for telemetry.
    """

    ingested: List[str] = field(default_factory=list)
    dropped: List[Tuple[str, str]] = field(default_factory=list)
    redacted_count: int = 0


class Ingestor:
    """Validates + redacts + persists knowledge candidates (rule-based)."""

    def __init__(self, paths, conn, key) -> None:
        """Bind to runtime ``paths``, an open sqlite ``conn`` (shared state.db)
        and the 32-byte AES ``key`` (``core.crypto.get_keychain_secret('fileenc')``).

        The Ingestor never opens its own connection and never sends anything.
        """
        self._paths = paths
        self._conn = conn
        self._key = key
        self._audit = get_audit_logger(paths)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate(self, cand: dict) -> Tuple[bool, str]:
        """Return ``(ok, reason)`` for a candidate dict.

        Rejects (deterministically, no LLM):
          * non-dict candidate;
          * missing/unknown ``source_type``;
          * empty ``title`` or ``body``;
          * zero usable provenance entries (the hard >=1 rule) — reason
            ``'no_provenance'`` so the drop is greppable in the audit log.
        A provenance entry is "usable" iff it has a known ``kind`` and a
        non-empty ``ref`` (the recheck pointer).
        """
        if not isinstance(cand, dict):
            return False, "not_a_dict"
        st = str(cand.get("source_type") or "").upper()
        if st not in _VALID_SOURCE_TYPES:
            return False, "bad_source_type"
        if not _is_nonempty(cand.get("title")):
            return False, "empty_title"
        if not _is_nonempty(cand.get("body")):
            return False, "empty_body"
        if not self._usable_provenance(cand.get("provenance")):
            return False, "no_provenance"
        return True, "ok"

    @staticmethod
    def _usable_provenance(provs: Any) -> List[dict]:
        """Return the subset of provenance entries that have a known kind and
        a non-empty ref (the only ones that can support an answer)."""
        out: List[dict] = []
        if not isinstance(provs, list):
            return out
        for p in provs:
            if not isinstance(p, dict):
                continue
            kind = str(p.get("kind") or "").upper()
            ref = p.get("ref")
            if kind in _VALID_PROV_KINDS and _is_nonempty(ref):
                out.append(p)
        return out

    # ------------------------------------------------------------------
    # Main ingest path
    # ------------------------------------------------------------------
    def ingest_candidates(
        self, cands: List[dict], default_taint: str = "INTERNAL"
    ) -> IngestReport:
        """Validate -> redact -> taint -> build KU -> upsert, for each candidate.

        Candidates failing :meth:`validate` are dropped and audited
        (``event='privacy_filter'``). All survivors enter at authority DRAFT;
        provenance >=1 is guaranteed for survivors and re-enforced by the model
        and the store layer. Returns an :class:`IngestReport`.
        """
        report = IngestReport()
        if default_taint not in _VALID_TAINTS:
            default_taint = "INTERNAL"

        for cand in cands or []:
            title = str((cand or {}).get("title") or "")[:120] if isinstance(cand, dict) else ""
            ok, reason = self.validate(cand)
            if not ok:
                report.dropped.append((reason, title))
                self._audit_drop(reason, cand)
                continue
            try:
                ku, fired = self._build_ku(cand, default_taint)
            except Exception as exc:  # build failure => drop, never fabricate
                report.dropped.append(("build_error", title))
                self._audit_drop(f"build_error:{type(exc).__name__}", cand)
                continue
            if fired:
                report.redacted_count += 1
            try:
                ku_id = self._persist(ku, cand)
            except Exception as exc:
                report.dropped.append(("persist_error", title))
                self._audit_drop(f"persist_error:{type(exc).__name__}", cand)
                continue
            report.ingested.append(ku_id)
            self._audit_ingest(ku_id, cand)
        return report

    # ------------------------------------------------------------------
    # KU construction (redaction + taint + DRAFT lock)
    # ------------------------------------------------------------------
    def _build_ku(self, cand: dict, default_taint: str):
        """Build an immutable KnowledgeUnit from a validated candidate.

        Runs redaction on the body and every provenance quote; merges taint via
        propagate(); forces authority=DRAFT; preserves CODE structural fields.
        Returns ``(KnowledgeUnit, redaction_fired: bool)``.
        """
        # Lazy import: model.py is owned by a sibling module and may import this
        # package's config; keep the import inside the call to avoid cycles.
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

        redaction_fired = False

        # --- body redaction ---
        raw_body = str(cand.get("body") or "")
        rb = redact(raw_body)
        if rb.hits:
            redaction_fired = True
        body_taints: List[str] = [rb.max_taint]

        # --- provenance redaction (quotes only; ref/kind never redacted so the
        #     recheck pointer stays resolvable) ---
        provs: List[Provenance] = []
        for p in self._usable_provenance(cand.get("provenance")):
            quote = str(p.get("quote") or "")
            if quote:
                rq = redact(quote)
                if rq.hits:
                    redaction_fired = True
                q_text = rq.text
                q_taint = propagate([rq.max_taint], own=_coerce_taint(p.get("quote_taint")))
            else:
                q_text = ""
                q_taint = _coerce_taint(p.get("quote_taint"))
            body_taints.append(q_taint)
            provs.append(
                Provenance(
                    kind=ProvKind(str(p.get("kind")).upper()),
                    ref=str(p.get("ref")),
                    quote=q_text,
                    quote_taint=Taint(q_taint),
                    captured_at=p.get("captured_at") or _now_iso(),
                    retrievable=True,
                )
            )

        # --- merged taint (declared + redaction + every quote), never washes down ---
        merged_taint = propagate(body_taints, own=_coerce_taint(cand.get("declared_taint"), default_taint))

        st = SourceType(str(cand.get("source_type")).upper())
        line_range = cand.get("line_range")
        if isinstance(line_range, (list, tuple)) and len(line_range) == 2:
            line_range = (int(line_range[0]), int(line_range[1]))
        else:
            line_range = None

        # Deterministic id: stable hash of source_type|first-prov-ref|symbol|
        # content_hash so the same source slice always upserts to the same KU.
        prov_ref = provs[0].ref if provs else ""
        ku_symbol = cand.get("symbol") if st == SourceType.CODE else None
        ku_content_hash = cand.get("content_hash") if st == SourceType.CODE else None
        # CODE freshness comes from the candidate (commit-bound); everything else
        # is UNKNOWN until verified.
        if st == SourceType.CODE:
            fresh = Freshness(str(cand.get("freshness") or "UNKNOWN").upper())
        else:
            fresh = Freshness.UNKNOWN

        ku = KnowledgeUnit(
            ku_id=make_ku_id(st, prov_ref, ku_symbol, ku_content_hash),
            source_type=st,
            title=str(cand.get("title"))[:200],
            body=rb.text,                       # redacted plaintext (in-memory only)
            body_redacted=True,                 # redaction always ran at ingest
            taint=Taint(merged_taint),
            authority=Authority.DRAFT,          # HARD: every ingest enters DRAFT
            public_ok=False,                    # never auto-public at ingest
            confidence=float(cand.get("confidence") or 0.0),
            freshness=fresh,
            provenance=provs,                   # >=1 guaranteed by validate()
            repo=cand.get("repo") if st == SourceType.CODE else None,
            commit_sha=cand.get("commit_sha") if st == SourceType.CODE else None,
            file_path=cand.get("file_path") if st == SourceType.CODE else None,
            symbol=cand.get("symbol") if st == SourceType.CODE else None,
            line_range=line_range if st == SourceType.CODE else None,
            content_hash=cand.get("content_hash") if st == SourceType.CODE else None,
            owner=cand.get("owner"),
        )
        return ku, redaction_fired

    def _persist(self, ku, cand: dict) -> str:
        """Upsert the KU and any ISSUE/QA <-> CODE edges; returns the ku_id."""
        from dws_agent.kdl.store import upsert_ku

        ku_id = upsert_ku(self._conn, ku, self._key)
        self._record_edges(ku_id, ku, cand)
        return ku_id

    def _record_edges(self, ku_id: str, ku, cand: dict) -> None:
        """Record ku_edge rows linking ISSUE/QA candidates to CODE symbols.

        Best-effort and silent on failure: edges power stale propagation but
        their absence never blocks ingest. Uses the store helper when present.
        """
        linked = cand.get("linked_symbols")
        if not isinstance(linked, list) or not linked:
            return
        try:
            from dws_agent.kdl.store import add_edge, find_code_ku_id
        except Exception:
            return
        st = str(cand.get("source_type") or "").upper()
        rel = "ISSUE_CODE" if st == "ISSUE" else ("QA_CODE" if st == "QA" else None)
        if rel is None:
            return
        for link in linked:
            if not isinstance(link, dict):
                continue
            try:
                dst = find_code_ku_id(
                    self._conn,
                    repo=link.get("repo"),
                    file_path=link.get("file_path"),
                    symbol=link.get("symbol"),
                )
                if dst:
                    add_edge(self._conn, ku_id, dst, rel)
            except Exception:
                continue

    # ------------------------------------------------------------------
    # QA auto-pairing (anti-poison)
    # ------------------------------------------------------------------
    def pair_qa(self, messages: List[dict], my_account: str) -> List[dict]:
        """Pair a thread question with *my* reply into QA candidates.

        A pair is emitted ONLY when the replying author == ``my_account`` AND no
        third party interjected between the question and my reply (A5
        anti-poison: an attacker cannot wedge a malicious message in between to
        get it attributed to me). All pairs enter as DRAFT — only operator
        confirmation may later promote them to AUTHORITATIVE.

        ``messages`` is a list of dicts with at least ``author`` and ``text``;
        optional ``msg_id``/``ts``/``taint``. The list is treated in arrival
        order. The question is the single non-me message immediately preceding
        my reply. If **more than one distinct non-me author** spoke since my
        previous turn, attribution is ambiguous (an interjector could be wedged
        in) so NO pair is emitted — we abstain rather than mis-attribute.
        """
        candidates: List[dict] = []
        if not isinstance(messages, list) or not _is_nonempty(my_account):
            return candidates

        # Messages accumulated since my last turn, and the distinct non-me
        # authors among them. A clean question == exactly one distinct author.
        pending: List[dict] = []
        pending_authors: set[str] = set()
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            author = str(msg.get("author") or "")
            text = str(msg.get("text") or "")
            if not _is_nonempty(text):
                continue
            if author == my_account:
                # My reply: pair only when exactly one third party spoke since
                # my last turn (no interjection ambiguity, A5 anti-poison).
                if len(pending_authors) == 1 and pending:
                    q = pending[-1]
                    cand = {
                        "source_type": "QA",
                        "title": str(q.get("text"))[:120],
                        "body": f"Q: {str(q.get('text')).strip()}\nA: {text.strip()}",
                        "declared_taint": propagate(
                            [_coerce_taint(q.get("taint")), _coerce_taint(msg.get("taint"))],
                            own="INTERNAL",
                        ),
                        "owner": my_account,
                    }
                    prov: List[dict] = []
                    if _is_nonempty(q.get("msg_id")):
                        prov.append({"kind": "MSG_ID", "ref": str(q.get("msg_id")), "quote": str(q.get("text")), "captured_at": q.get("ts")})
                    if _is_nonempty(msg.get("msg_id")):
                        prov.append({"kind": "MSG_ID", "ref": str(msg.get("msg_id")), "quote": text, "captured_at": msg.get("ts")})
                    cand["provenance"] = prov
                    candidates.append(cand)
                # My turn always resets the window (paired or abstained).
                pending = []
                pending_authors = set()
            else:
                # A non-me message: accumulate into the current window. A second
                # distinct author makes the next pairing ambiguous (poisoned).
                pending.append(msg)
                pending_authors.add(author)
        return candidates

    # ------------------------------------------------------------------
    # Issue parsing
    # ------------------------------------------------------------------
    def parse_issue(self, issue: dict) -> dict:
        """Parse an issue dict into a single ISSUE candidate.

        Extracts 症状/根因/处置 via the stub distiller's heading/inline rules and
        carries any ``linked_symbols`` so the Ingestor can build ku_edge rows
        (ISSUE<->CODE). Provenance is derived from the issue id/url. Returns a
        candidate dict (still subject to :meth:`validate`).
        """
        from .distill import RawItem, StubDistiller

        body = str((issue or {}).get("body") or (issue or {}).get("text") or "")
        meta: Dict[str, Any] = {
            "title": issue.get("title"),
            "url": issue.get("url"),
            "issue_id": issue.get("id") or issue.get("issue_id"),
            "owner": issue.get("owner") or issue.get("assignee"),
            "captured_at": issue.get("ts") or issue.get("updated_at"),
        }
        linked = issue.get("linked_symbols")
        if isinstance(linked, list) and linked:
            meta["linked_symbols"] = linked
        cands = StubDistiller().distill(RawItem(source_type="ISSUE", text=body, meta=meta))
        return cands[0] if cands else {
            "source_type": "ISSUE",
            "title": str(issue.get("title") or "issue"),
            "body": body,
            "provenance": [],
        }

    # ------------------------------------------------------------------
    # Audit helpers
    # ------------------------------------------------------------------
    def _audit_drop(self, reason: str, cand: Any) -> None:
        title = ""
        st = ""
        if isinstance(cand, dict):
            title = str(cand.get("title") or "")[:80]
            st = str(cand.get("source_type") or "")
        self._audit.log(
            {
                "event": "privacy_filter",
                "actor": "store",
                "decision": "drop",
                "reason": reason,
                "detail": {"kdl_op": "ingest", "source_type": st, "title": title},
            }
        )

    def _audit_ingest(self, ku_id: str, cand: dict) -> None:
        self._audit.log(
            {
                "event": "privacy_filter",
                "actor": "store",
                "decision": "ingest",
                "reason": "ok",
                "detail": {
                    "kdl_op": "ingest",
                    "ku_id": ku_id,
                    "source_type": str(cand.get("source_type") or ""),
                },
            }
        )


# ---------------------------------------------------------------------------
# Source adapters (fixtures / R0 read-only dumps -> candidate dicts)
# ---------------------------------------------------------------------------


def code_adapter(repo_path: str, *, paths=None) -> List[dict]:
    """Adapter for CODE: delegate symbol extraction to :mod:`dws_agent.kdl.code`.

    Read-only. Returns candidate dicts (one per extracted symbol), each
    commit-bound (repo/commit_sha/file_path/symbol/line_range/content_hash) so
    the freshness loop can detect drift. Falls back to ``[]`` if the code
    module is unavailable.
    """
    try:
        from dws_agent.kdl import code as code_mod
        from .distill import RawItem, StubDistiller
    except Exception:
        return []

    distiller = StubDistiller()
    out: List[dict] = []
    # code.extract_repo_symbols (read-only) yields per-file symbol meta dicts.
    extractor = getattr(code_mod, "extract_repo_symbols", None) or getattr(
        code_mod, "extract_symbols", None
    )
    if extractor is None:
        return []
    try:
        groups = extractor(repo_path)
    except Exception:
        return []
    for grp in groups or []:
        # grp is expected to be a RawItem-shaped meta dict carrying 'symbols'.
        meta = grp if isinstance(grp, dict) else {}
        text = meta.get("text", "")
        out.extend(distiller.distill(RawItem(source_type="CODE", text=text, meta=meta)))
    return out


def issue_adapter(issues: List[dict]) -> List[dict]:
    """Adapter for ISSUE fixtures / R0 read-only dumps -> candidate dicts."""
    ing = Ingestor.__new__(Ingestor)  # parse_issue needs no DB/key
    out: List[dict] = []
    for issue in issues or []:
        if isinstance(issue, dict):
            out.append(ing.parse_issue(issue))
    return out


def qa_adapter(threads: List[dict], my_account: str) -> List[dict]:
    """Adapter for QA: pair questions with my replies across message threads.

    Each thread dict carries ``messages`` (list of message dicts). Uses
    :meth:`Ingestor.pair_qa` so the anti-poison interjection rule applies.
    """
    ing = Ingestor.__new__(Ingestor)
    out: List[dict] = []
    for thread in threads or []:
        if not isinstance(thread, dict):
            continue
        msgs = thread.get("messages")
        if isinstance(msgs, list):
            out.extend(ing.pair_qa(msgs, my_account))
    return out


def playbook_adapter(docs: List[dict]) -> List[dict]:
    """Adapter for PLAYBOOK fixtures -> numbered-step candidate dicts."""
    from .distill import RawItem, StubDistiller

    distiller = StubDistiller()
    out: List[dict] = []
    for doc in docs or []:
        if not isinstance(doc, dict):
            continue
        text = str(doc.get("body") or doc.get("text") or "")
        meta = {
            "title": doc.get("title"),
            "doc_id": doc.get("doc_id") or doc.get("id"),
            "url": doc.get("url"),
            "owner": doc.get("owner"),
            "captured_at": doc.get("ts") or doc.get("updated_at"),
        }
        out.extend(distiller.distill(RawItem(source_type="PLAYBOOK", text=text, meta=meta)))
    return out


# ---------------------------------------------------------------------------
# small local helpers
# ---------------------------------------------------------------------------


def _is_nonempty(v: Any) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _coerce_taint(v: Any, default: str = "CLEAN") -> str:
    s = str(v or "").upper()
    return s if s in _VALID_TAINTS else default


def _now_iso() -> str:
    """RFC3339 UTC, same format the store/state_db uses ('%Y-%m-%dT%H:%M:%SZ')."""
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
