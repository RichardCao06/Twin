"""KDL core data model: KnowledgeUnit, Provenance, Verdict, Citation, DraftPreview.

This module is the single source of truth for the global shared contract of the
Knowledge Distillation Layer (KDL). It is PURE stdlib (plus a re-use of
``privacy.taint.TAINT_ORDER``). It performs NO I/O, NO network, NO LLM calls and
NEVER issues a dws command — it only defines data and the deterministic rules
that bind that data.

Hard constraints baked into the model (cannot be bypassed by a caller):

* ``ku_id`` is a deterministic hash of ``source_type|provenance.ref|symbol|
  content_hash`` so the same source slice always yields the same id.
* PROVENANCE-EMPTY LOCK: a KnowledgeUnit with zero provenance is forced to
  ``authority=DRAFT`` and ``serve_blocked=True`` in ``__post_init__`` and can
  never be un-blocked by the caller (re-enforced again at store.upsert_ku()).
* ``serve_blocked`` is a *computed* safety flag: True if provenance is empty, OR
  freshness==EXPIRED, OR authority==DRAFT, OR evidence is broken. A blocked KU
  must not support an outward answer (phase1 never answers outward anyway, but
  the label is the gate the later triage layer relies on).
* ``body``/``quote`` are PLAINTEXT in memory ONLY. They are never serialised in
  the public/row dicts produced here (the store encrypts them to ``*_cipher``).
* ``confidence`` is sort/telemetry only — it NEVER gates answerability. Only the
  hard gates (freshness/authority/provenance) decide answerable vs abstain.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

# Re-use the phase0 taint ordering rather than redefining it. Keeping the KDL
# Taint enum aligned with TAINT_ORDER guarantees propagate()/max_taint() and the
# enum agree on severity.
from ..privacy.taint import TAINT_ORDER


# --------------------------------------------------------------------------- #
# Enums. All are str-Enum subclasses so sqlite stores the ``.value`` directly
# and round-trips through JSON as plain strings.
# --------------------------------------------------------------------------- #
class SourceType(str, Enum):
    """The four knowledge sources of §2.1."""

    CODE = "CODE"        # symbol -> behaviour/contract/pitfall, bound to a commit
    ISSUE = "ISSUE"      # symptom -> root cause -> remediation
    QA = "QA"            # normalised question -> answer pair
    PLAYBOOK = "PLAYBOOK"  # SOP step card


class Authority(str, Enum):
    """Trust level of a KU. Only operator-confirmed KUs reach AUTHORITATIVE."""

    DRAFT = "DRAFT"
    REVIEWED = "REVIEWED"
    AUTHORITATIVE = "AUTHORITATIVE"
    DEPRECATED = "DEPRECATED"


class Taint(str, Enum):
    """Sensitivity label. Order CLEAN < INTERNAL < SENSITIVE (see TAINT_ORDER)."""

    CLEAN = "CLEAN"
    INTERNAL = "INTERNAL"
    SENSITIVE = "SENSITIVE"


class Freshness(str, Enum):
    """Code/knowledge freshness. UNKNOWN is the conservative default."""

    FRESH = "FRESH"
    STALE = "STALE"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class ProvKind(str, Enum):
    """The kind of pointer a Provenance.ref encodes."""

    COMMIT = "COMMIT"
    ISSUE_URL = "ISSUE_URL"
    MSG_ID = "MSG_ID"
    DOC_ID = "DOC_ID"
    MAIL_ID = "MAIL_ID"
    FILE = "FILE"


class VerdictDecision(str, Enum):
    """Outcome of a retrieval serve()."""

    ANSWERABLE = "ANSWERABLE"
    ABSTAIN = "ABSTAIN"


# Sanity check at import time: the KDL Taint enum must be a subset of the shared
# ordering, otherwise propagate() and the enum could disagree on severity.
assert set(t.value for t in Taint) <= set(TAINT_ORDER), (
    "Taint enum out of sync with privacy.taint.TAINT_ORDER"
)


def _now_iso() -> str:
    """RFC3339 UTC timestamp, byte-identical to store.state_db._now_iso."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #
@dataclass
class Provenance:
    """A single traceable source pointer for a KU (§2.1.3 可溯源硬约束).

    Attributes:
        kind: which kind of pointer (commit / issue url / dingtalk msgId / ...).
        ref: the pointer itself — a git SHA, URL, msgId, or file path. This is
            the value the broken-link recheck resolves against.
        quote: a redacted excerpt. PLAINTEXT in memory ONLY; the store persists
            it encrypted as ``quote_cipher`` and a draft NEVER surfaces the raw
            quote — only ``kind``+``ref`` are ever shown (§2.1.6 A7).
        quote_taint: taint of the quote after redaction.
        captured_at: when this provenance was captured (RFC3339 UTC).
        retrievable: False once the broken-link recheck cannot resolve ``ref``.
    """

    kind: ProvKind
    ref: str
    quote: str = ""
    quote_taint: Taint = Taint.CLEAN
    captured_at: str = field(default_factory=_now_iso)
    retrievable: bool = True

    def __post_init__(self) -> None:
        # Coerce string inputs (e.g. from JSON) into enums so equality/serialisation
        # behave uniformly regardless of construction path.
        if not isinstance(self.kind, ProvKind):
            self.kind = ProvKind(self.kind)
        if not isinstance(self.quote_taint, Taint):
            self.quote_taint = Taint(self.quote_taint)


# --------------------------------------------------------------------------- #
# KnowledgeUnit
# --------------------------------------------------------------------------- #
@dataclass
class KnowledgeUnit:
    """The unified knowledge unit (KU) of §2.1.2.

    ``body``/quote fields are plaintext in memory only and are never emitted by
    :func:`ku_to_row` or :func:`ku_to_public_dict` as plaintext. The provenance
    lock and the computed ``serve_blocked`` flag are enforced in
    :meth:`__post_init__` and re-enforced at the store layer.
    """

    ku_id: str
    source_type: SourceType
    title: str
    body: str  # PLAINTEXT in-memory only — NEVER persisted plaintext.
    body_redacted: bool
    taint: Taint
    authority: Authority
    public_ok: bool
    confidence: float  # sort/telemetry only — NEVER gates answerability.
    freshness: Freshness
    provenance: List[Provenance]

    # CODE-only fields; None for non-CODE KUs.
    repo: Optional[str] = None
    commit_sha: Optional[str] = None
    file_path: Optional[str] = None
    symbol: Optional[str] = None
    line_range: Optional[Tuple[int, int]] = None
    content_hash: Optional[str] = None  # sha256 hex of the symbol's source slice

    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    last_verified_at: Optional[str] = None
    expires_at: Optional[str] = None
    superseded_by: Optional[str] = None
    owner: Optional[str] = None

    serve_blocked: bool = False  # computed in __post_init__
    derived_stale: bool = False

    # Non-persisted in-memory flag set by the freshness layer when a CODE-KU's
    # evidence (symbol/file) no longer resolves at its commit. Feeds serve_blocked.
    evidence_broken: bool = False

    def __post_init__(self) -> None:
        # Normalise string inputs into enums (tolerates JSON/row construction).
        if not isinstance(self.source_type, SourceType):
            self.source_type = SourceType(self.source_type)
        if not isinstance(self.taint, Taint):
            self.taint = Taint(self.taint)
        if not isinstance(self.authority, Authority):
            self.authority = Authority(self.authority)
        if not isinstance(self.freshness, Freshness):
            self.freshness = Freshness(self.freshness)
        if self.line_range is not None and not isinstance(self.line_range, tuple):
            self.line_range = tuple(self.line_range)  # type: ignore[assignment]
        for p in self.provenance:
            if not isinstance(p, Provenance):
                raise TypeError("provenance entries must be Provenance instances")

        # HARD RULE: provenance-empty => locked DRAFT + serve_blocked.
        if len(self.provenance) == 0:
            self.authority = Authority.DRAFT

        # Recompute serve_blocked as a pure function of the gate-relevant state.
        self.serve_blocked = self._compute_serve_blocked()

    # -- derived helpers --------------------------------------------------- #
    def _compute_serve_blocked(self) -> bool:
        """True if this KU must not back an outward answer.

        Blocked when ANY of: no provenance, freshness EXPIRED, authority DRAFT,
        broken evidence, or a broken (non-retrievable) provenance pointer.
        """
        if len(self.provenance) == 0:
            return True
        if self.freshness == Freshness.EXPIRED:
            return True
        if self.authority == Authority.DRAFT:
            return True
        if self.evidence_broken:
            return True
        if any(not p.retrievable for p in self.provenance):
            return True
        return False

    def is_code(self) -> bool:
        """True iff this is a CODE-flavoured KU (commit-bound)."""
        return self.source_type == SourceType.CODE

    def can_serve(self) -> bool:
        """True iff this KU may support an outward answer right now.

        Recomputes the block from current state so callers always see a fresh
        decision even if a field was mutated in place by the freshness layer.
        """
        return not self._compute_serve_blocked()


# --------------------------------------------------------------------------- #
# Verdict / Citation / DraftPreview — produced by the retrieve layer only.
# --------------------------------------------------------------------------- #
@dataclass
class Citation:
    """A source-identifier-only reference returned in a Verdict.

    Carries NO body and NO raw quote — only the pointers needed to trace the
    claim (§2.1.6 A7).
    """

    ku_id: str
    source_type: SourceType
    authority: Authority
    freshness: Freshness
    prov_kind: ProvKind
    prov_ref: str
    score: float


@dataclass
class Verdict:
    """Structured retrieval result (§2.1.7). Produced ONLY by retrieve.serve().

    Attributes:
        decision: ANSWERABLE or ABSTAIN. Abstain is the safe default.
        reason: machine-readable code (e.g. 'no_hit', 'all_draft', 'ok').
        confidence: fused score of the top candidate (sort/telemetry only).
        citations: source-identifier-only references.
        kus: top-N KUs with decrypted bodies, for the LOCAL draft assembler
            only — never transmitted.
    """

    decision: VerdictDecision
    reason: str
    confidence: float
    citations: List[Citation] = field(default_factory=list)
    kus: List[KnowledgeUnit] = field(default_factory=list)


# The fixed prefix every locally-assembled preview carries so it can never be
# mistaken for a sent answer.
ASSISTANT_PREFIX = "助理代答(待本人复核)"


@dataclass
class DraftPreview:
    """A local "if-I-answered" preview (§2.1.7). NEVER transmitted.

    When the backing Verdict is ABSTAIN, ``would_answer`` is False, ``draft_text``
    is None and ``abstain_reason`` is set — the layer NEVER fabricates an answer
    (绝不编).
    """

    would_answer: bool
    draft_text: Optional[str]
    citations: List[Citation] = field(default_factory=list)
    assistant_prefix: str = ASSISTANT_PREFIX
    abstain_reason: Optional[str] = None


# --------------------------------------------------------------------------- #
# Deterministic id derivation
# --------------------------------------------------------------------------- #
def make_ku_id(
    source_type: "SourceType | str",
    prov_ref: str,
    symbol: Optional[str],
    content_hash: Optional[str],
) -> str:
    """Return the deterministic ``KU-<sha1[:16]>`` id for a knowledge unit.

    The id is a stable hash of ``source_type|prov_ref|symbol|content_hash`` so
    the same source slice always maps to the same id (enabling idempotent
    upserts and stable cross-references). Missing optional parts hash as empty.
    """
    st = source_type.value if isinstance(source_type, SourceType) else str(source_type)
    seed = "|".join([st, prov_ref or "", symbol or "", content_hash or ""])
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return f"KU-{digest}"


# --------------------------------------------------------------------------- #
# (De)serialisation
# --------------------------------------------------------------------------- #
def ku_to_row(ku: KnowledgeUnit) -> Dict[str, Any]:
    """Map a KU to the column dict for the ``ku`` table.

    NOTE: the plaintext ``body`` is intentionally NOT included — the store
    encrypts it separately into ``body_cipher``. This dict carries only the
    non-secret scalar columns plus boolean->int coercions matching the schema.
    Provenance rows are handled separately by the store (with encrypted quotes).
    """
    ls = ku.line_range[0] if ku.line_range else None
    le = ku.line_range[1] if ku.line_range else None
    return {
        "ku_id": ku.ku_id,
        "source_type": ku.source_type.value,
        "title": ku.title,
        # body_cipher is filled by the store; not here.
        "body_redacted": 1 if ku.body_redacted else 0,
        "taint": ku.taint.value,
        "authority": ku.authority.value,
        "public_ok": 1 if ku.public_ok else 0,
        "confidence": float(ku.confidence),
        "freshness": ku.freshness.value,
        "repo": ku.repo,
        "commit_sha": ku.commit_sha,
        "file_path": ku.file_path,
        "symbol": ku.symbol,
        "line_start": ls,
        "line_end": le,
        "content_hash": ku.content_hash,
        "created_at": ku.created_at,
        "updated_at": ku.updated_at,
        "last_verified_at": ku.last_verified_at,
        "expires_at": ku.expires_at,
        "superseded_by": ku.superseded_by,
        "owner": ku.owner,
        "serve_blocked": 1 if ku.serve_blocked else 0,
        "derived_stale": 1 if ku.derived_stale else 0,
    }


def ku_to_public_dict(ku: KnowledgeUnit) -> Dict[str, Any]:
    """Return a body-free / quote-free view of a KU safe to print/log.

    Surfaces only metadata and source identifiers (kind+ref) of each provenance
    — NEVER the body or the raw quote (§2.1.6 A7). Used by CLI status/search
    output and the public-shape JSON-schema contract.
    """
    return {
        "ku_id": ku.ku_id,
        "source_type": ku.source_type.value,
        "title": ku.title,
        "body_redacted": ku.body_redacted,
        "taint": ku.taint.value,
        "authority": ku.authority.value,
        "public_ok": ku.public_ok,
        "confidence": ku.confidence,
        "freshness": ku.freshness.value,
        "serve_blocked": ku.serve_blocked,
        "derived_stale": ku.derived_stale,
        "repo": ku.repo,
        "commit_sha": ku.commit_sha,
        "file_path": ku.file_path,
        "symbol": ku.symbol,
        "line_range": list(ku.line_range) if ku.line_range else None,
        "content_hash": ku.content_hash,
        "superseded_by": ku.superseded_by,
        "owner": ku.owner,
        "provenance": [
            {
                "kind": p.kind.value,
                "ref": p.ref,
                "quote_taint": p.quote_taint.value,
                "captured_at": p.captured_at,
                "retrievable": p.retrievable,
            }
            for p in ku.provenance
        ],
    }


class CandidateError(ValueError):
    """Raised when a distiller candidate dict fails structural validation."""


def candidate_from_json(d: Any) -> Dict[str, Any]:
    """Validate and normalise a distiller knowledge-candidate dict.

    This is the in-Python mirror of ``ku.schema.json``: the contract a Distiller
    (LLM or the deterministic stub) must satisfy before the no-LLM Ingestor will
    consider landing it. It does NOT build a KnowledgeUnit and does NOT touch the
    store — it only enforces shape so the Ingestor can reject bad candidates
    deterministically.

    Hard rules enforced here:
      * ``source_type`` must be a valid SourceType.
      * ``title`` and ``body`` must be non-empty strings.
      * ``provenance`` must have >=1 entry, each with a valid ``kind`` and a
        non-empty ``ref`` (missing provenance is the no_provenance drop reason).
      * For ``source_type==CODE`` the ``repo``, ``file_path`` and ``symbol``
        fields are required.

    Returns a normalised candidate dict (enums coerced to their string values,
    optional fields defaulted). Raises :class:`CandidateError` on any violation.
    """
    if isinstance(d, (str, bytes, bytearray)):
        d = json.loads(d)
    if not isinstance(d, dict):
        raise CandidateError("candidate must be a JSON object")

    # source_type
    raw_st = d.get("source_type")
    try:
        st = SourceType(raw_st)
    except (ValueError, KeyError):
        raise CandidateError(f"invalid source_type: {raw_st!r}")

    title = d.get("title")
    if not isinstance(title, str) or not title.strip():
        raise CandidateError("title must be a non-empty string")

    body = d.get("body")
    if not isinstance(body, str) or not body.strip():
        raise CandidateError("body must be a non-empty string")

    prov_in = d.get("provenance")
    if not isinstance(prov_in, list) or len(prov_in) < 1:
        raise CandidateError("no_provenance: provenance must have >=1 entry")
    norm_prov: List[Dict[str, Any]] = []
    for i, p in enumerate(prov_in):
        if not isinstance(p, dict):
            raise CandidateError(f"provenance[{i}] must be an object")
        try:
            pk = ProvKind(p.get("kind"))
        except (ValueError, KeyError):
            raise CandidateError(f"provenance[{i}].kind invalid: {p.get('kind')!r}")
        ref = p.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            raise CandidateError(f"provenance[{i}].ref must be a non-empty string")
        qt_raw = p.get("quote_taint", Taint.CLEAN.value)
        try:
            qt = Taint(qt_raw)
        except ValueError:
            raise CandidateError(f"provenance[{i}].quote_taint invalid: {qt_raw!r}")
        norm_prov.append(
            {
                "kind": pk.value,
                "ref": ref,
                "quote": p.get("quote", ""),
                "quote_taint": qt.value,
            }
        )

    out: Dict[str, Any] = {
        "source_type": st.value,
        "title": title,
        "body": body,
        "provenance": norm_prov,
        "taint": d.get("taint", Taint.CLEAN.value),
        "public_ok": bool(d.get("public_ok", False)),
        "confidence": float(d.get("confidence", 0.0)),
        "owner": d.get("owner"),
    }

    if st == SourceType.CODE:
        for fld in ("repo", "file_path", "symbol"):
            v = d.get(fld)
            if not isinstance(v, str) or not v.strip():
                raise CandidateError(f"CODE candidate requires non-empty {fld}")
            out[fld] = v
        # optional CODE extras pass through if present
        for fld in ("commit_sha", "content_hash"):
            if d.get(fld) is not None:
                out[fld] = d[fld]
        lr = d.get("line_range")
        if lr is not None:
            if (not isinstance(lr, (list, tuple))) or len(lr) != 2:
                raise CandidateError("line_range must be a [start, end] pair")
            out["line_range"] = [int(lr[0]), int(lr[1])]

    # validate declared taint enum
    try:
        Taint(out["taint"])
    except ValueError:
        raise CandidateError(f"invalid taint: {out['taint']!r}")

    return out
