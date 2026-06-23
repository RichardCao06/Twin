"""KDL Retrieval + Serve layer (read-only, never sends, never calls dws).

Responsibilities (§2.1.6):

* Tokenization: :func:`bigram_tokenize` (CJK overlapping 2-grams + ascii
  ``\\w+`` lowercased) and :func:`to_fts_query` (OR of tokens) — used both at
  index time (by ``kdl.store``) and at query time, so the FTS5 ``unicode61``
  tokenizer matches CJK content.
* Retrieval: :func:`retrieve` runs L1 symbol-exact lookup first, then L2
  FTS/inverted lexical match. Hard gates run BEFORE ranking — EXPIRED and
  DEPRECATED candidates are dropped, ``external_facing`` drops taint!=CLEAN,
  CODE-KUs are lazily verified at query time via ``kdl.code``.
* Scoring: :func:`score` = w_f*freshness + w_a*authority + w_r*relevance.
* Decision: :func:`serve` produces a structured :class:`Verdict`
  (ANSWERABLE/ABSTAIN + citations + confidence) applying all six abstain
  rules. Any exception anywhere => ABSTAIN (绝不编).
* Local-only draft: :func:`assemble_draft` builds a "如果代答会怎么答"
  DraftPreview for the operator's eyes only. It is NEVER transmitted, cites
  only source identifiers (never raw quotes/body), and is empty on ABSTAIN.

HARD CONSTRAINTS embodied here:
  - No outward send, no dws write, no ActionIntent — pure read.
  - provenance missing => the KU is already DRAFT + serve_blocked upstream; we
    additionally never let serve_blocked KUs support an ANSWERABLE verdict.
  - abstain is the safe default: any verify/decrypt/retrieve error => ABSTAIN.
  - SENSITIVE/non-CLEAN taint is excluded from external_facing retrieval.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

from .config import KdlSettings, kdl_settings

# ---------------------------------------------------------------------------
# Soft imports of sibling modules owned by other phase-1 authors. We import by
# attribute access at call time and degrade to safe defaults if unavailable,
# so this module is importable/unit-testable in isolation.
# ---------------------------------------------------------------------------
try:  # model: dataclasses + enums (frozen). Owned by kdl/model.py.
    from . import model as _model  # type: ignore
except Exception:  # pragma: no cover - model should normally be present
    _model = None  # type: ignore

try:  # store: decryption + sqlite helpers. Owned by kdl/store.py.
    from . import store as _store  # type: ignore
except Exception:  # pragma: no cover
    _store = None  # type: ignore

try:  # code: lazy CODE verification + GitReader. Owned by kdl/code.py.
    from . import code as _code  # type: ignore
except Exception:  # pragma: no cover
    _code = None  # type: ignore


# ---------------------------------------------------------------------------
# Enum / value helpers — tolerant of model.py being a str-Enum or plain str.
# ---------------------------------------------------------------------------
def _val(x) -> str:
    """Return the ``.value`` of a str-Enum, else ``str(x)`` (or '' for None)."""
    if x is None:
        return ""
    return getattr(x, "value", x) if not isinstance(x, str) else x


# Fixed scoring tables (data, but intrinsic to the gate semantics §retrieval_design).
_FRESH_SCORE = {"FRESH": 1.0, "STALE": 0.3, "EXPIRED": 0.0, "UNKNOWN": 0.3}
_AUTH_SCORE = {"AUTHORITATIVE": 1.0, "REVIEWED": 0.7, "DRAFT": 0.2, "DEPRECATED": 0.0}

# Authority ordering for "REVIEWED+" membership (contradiction gate A5).
_AUTH_RANK = {"DRAFT": 0, "DEPRECATED": 0, "REVIEWED": 1, "AUTHORITATIVE": 2}

# Query markers that defer to the triage layer (phase2). Phase1 flags + abstains.
_COMMITMENT_MARKERS = (
    "承诺", "保证", "决定", "拍板", "对外", "口径", "上线时间", "deadline",
    "commit to", "guarantee", "we will", "官方",
)

# Verdict / decision machine-codes.
DECISION_ANSWERABLE = "ANSWERABLE"
DECISION_ABSTAIN = "ABSTAIN"

CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_RUN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]+|[A-Za-z0-9_]+")


# ---------------------------------------------------------------------------
# Tokenization (L2). Shared by index time (store) and query time (here).
# ---------------------------------------------------------------------------
def bigram_tokenize(text: str) -> list[str]:
    """Tokenize *text* into CJK overlapping 2-grams + ascii ``\\w+`` tokens.

    For each maximal CJK run emit overlapping 2-grams ``chars[i:i+2]``; an
    isolated single CJK char is emitted as itself. For each ascii run emit the
    lowercased token. Order is preserved, duplicates kept (callers dedupe as
    needed). FTS5 ``unicode61`` then matches these space-joined tokens.
    """
    if not text or not isinstance(text, str):
        return []
    tokens: list[str] = []
    for run in _RUN_RE.findall(text):
        if CJK_RE.match(run):
            if len(run) == 1:
                tokens.append(run)
            else:
                for i in range(len(run) - 1):
                    tokens.append(run[i : i + 2])
        else:
            tokens.append(run.lower())
    return tokens


def to_fts_query(query: str) -> str:
    """Build an FTS5 MATCH expression: OR of the query's tokens.

    Tokens are double-quoted so punctuation/operators in the source text are
    treated as literals, never as FTS5 syntax (defensive against injection).
    Returns an empty string if the query yields no tokens.
    """
    toks = []
    seen = set()
    for t in bigram_tokenize(query):
        if t in seen:
            continue
        seen.add(t)
        toks.append('"' + t.replace('"', '""') + '"')
    return " OR ".join(toks)


# ---------------------------------------------------------------------------
# Candidate / Citation / Verdict / DraftPreview value objects.
#
# Citation and Verdict are also declared in model.py per the contract; we
# prefer the model's classes when present (so types unify across modules) and
# otherwise fall back to local equivalents with the same field shape.
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    """An in-flight retrieval candidate (operator-local, never serialized out).

    Holds the (decrypted, in-memory) KnowledgeUnit plus the per-query lexical
    relevance and the path that surfaced it ('symbol' | 'fts' | 'inverted').
    """

    ku: object
    relevance: float = 0.0
    lexical_overlap: float = 0.0
    via: str = "fts"
    fused: float = 0.0


def _coerce_enum(name: str, value):
    """Coerce *value* to model.<name>(value) when the enum exists, else return raw."""
    if _model is not None and value not in (None, ""):
        enum_cls = getattr(_model, name, None)
        if enum_cls is not None:
            try:
                return enum_cls(_val(value))
            except Exception:
                return value
    return value


def _make_citation(ku, score_val: float):
    """Build a Citation carrying only source identifiers (no body, no quote).

    Prefers model.Citation with properly-typed enum fields (SourceType /
    Authority / Freshness / ProvKind). Falls back to a dict of the same shape
    if the model class is unavailable or construction fails.
    """
    ku_id = getattr(ku, "ku_id", None)
    source_type = _val(getattr(ku, "source_type", None))
    authority = _val(getattr(ku, "authority", None))
    freshness = _val(getattr(ku, "freshness", None))
    prov_kind = ""
    prov_ref = ""
    provs = getattr(ku, "provenance", None) or []
    if provs:
        p0 = provs[0]
        prov_kind = _val(getattr(p0, "kind", None))
        prov_ref = getattr(p0, "ref", "") or ""
    cls = getattr(_model, "Citation", None) if _model is not None else None
    if cls is not None:
        try:
            return cls(
                ku_id=ku_id,
                source_type=_coerce_enum("SourceType", source_type),
                authority=_coerce_enum("Authority", authority),
                freshness=_coerce_enum("Freshness", freshness),
                prov_kind=_coerce_enum("ProvKind", prov_kind),
                prov_ref=prov_ref,
                score=score_val,
            )
        except Exception:
            pass
    return {
        "ku_id": ku_id,
        "source_type": source_type,
        "authority": authority,
        "freshness": freshness,
        "prov_kind": prov_kind,
        "prov_ref": prov_ref,
        "score": score_val,
    }


def _make_verdict(decision: str, reason: str, confidence: float, citations, kus):
    """Build a Verdict, preferring model.Verdict when available."""
    cls = getattr(_model, "Verdict", None) if _model is not None else None
    dec_enum = decision
    if _model is not None:
        vd = getattr(_model, "VerdictDecision", None)
        if vd is not None:
            try:
                dec_enum = vd(decision)
            except Exception:
                dec_enum = decision
    if cls is not None:
        try:
            return cls(
                decision=dec_enum,
                reason=reason,
                confidence=confidence,
                citations=citations,
                kus=kus,
            )
        except Exception:
            pass
    return _LocalVerdict(
        decision=decision, reason=reason, confidence=confidence,
        citations=citations, kus=kus,
    )


@dataclass
class _LocalVerdict:
    """Fallback Verdict used only if model.Verdict is unavailable."""

    decision: str
    reason: str
    confidence: float
    citations: list = field(default_factory=list)
    kus: list = field(default_factory=list)


@dataclass
class DraftPreview:
    """Operator-local 'if I answered' preview. NEVER transmitted.

    Marked with an explicit assistant prefix and cites only source
    identifiers. On ABSTAIN, ``would_answer`` is False, ``draft_text`` is None
    and ``abstain_reason`` carries the machine code.
    """

    would_answer: bool
    draft_text: Optional[str]
    citations: list
    assistant_prefix: str = "助理代答(待本人复核)"
    abstain_reason: Optional[str] = None


class NoOpVectorBackend:
    """L3 vector backend stub interface (not implemented in phase1).

    Design (§retrieval_design) defers vectors to "按需/L1L2 不足时". This
    no-op satisfies the interface so callers can wire a real backend later
    without touching :func:`retrieve`. It always returns no candidates.
    """

    def available(self) -> bool:  # pragma: no cover - trivial
        return False

    def search(self, query: str, top_n: int):  # pragma: no cover - trivial
        return []


# ---------------------------------------------------------------------------
# Decryption of body / quote — delegated to store when present.
# ---------------------------------------------------------------------------
def _row_to_ku(conn, key, row):
    """Materialize a KnowledgeUnit (with decrypted body+provenance) from a row.

    Prefers ``store.get_ku`` (the real phase1 loader, which decrypts body and
    provenance quotes and reconstructs the frozen KnowledgeUnit). Falls back to
    a lightweight duck-typed namespace whose attributes match the fields
    retrieval/scoring touch, when store is unavailable (isolated unit tests).
    """
    ku_id = row["ku_id"] if not isinstance(row, dict) else row.get("ku_id")
    if _store is not None and hasattr(_store, "get_ku"):
        ku = _store.get_ku(conn, ku_id, key)
        if ku is not None:
            return ku
    # Lightweight fallback object (no decryption available => body left None).
    d = dict(row) if not isinstance(row, dict) else row

    class _KU:  # minimal duck-typed KU
        pass

    ku = _KU()
    for k, v in d.items():
        setattr(ku, k, v)
    if not hasattr(ku, "body"):
        ku.body = None
    if not hasattr(ku, "provenance"):
        ku.provenance = []
    return ku


# ---------------------------------------------------------------------------
# Hard gates + lazy verification.
# ---------------------------------------------------------------------------
def _evidence_broken(ku) -> bool:
    """True if any top provenance is non-retrievable / evidence flagged broken."""
    if getattr(ku, "evidence_broken", False):
        return True
    provs = getattr(ku, "provenance", None) or []
    if not provs:
        return True  # missing provenance => locked, cannot support answer
    # broken iff the first (top) provenance pointer no longer resolves
    p0 = provs[0]
    return getattr(p0, "retrievable", True) is False


def _coerce_freshness(value: str):
    """Return model.Freshness(value) when available, else the raw string."""
    if _model is not None:
        f = getattr(_model, "Freshness", None)
        if f is not None:
            try:
                return f(value)
            except Exception:
                return value
    return value


def _repo_path_for(ku):
    """Best-effort resolve a CODE-KU's repository working tree for verification.

    The ``repo`` field stores a repo *name*; phase1 has no name->path registry
    in retrieve. We accept a ``repo_path`` attribute if the store/model exposes
    one, else treat ``repo`` as a path if it points at an existing directory.
    Returns None when no working tree can be resolved (verify is then skipped
    and the stored freshness from reindex stands — abstain rules still gate it).
    """
    import os
    cand = getattr(ku, "repo_path", None) or getattr(ku, "repo", None)
    if cand and os.path.isdir(str(cand)):
        return str(cand)
    return None


def _lazy_verify_code(conn, key, ku):
    """Lazily re-verify a CODE-KU at query time (real-time, no cache).

    Delegates to ``kdl.code.GitReader.verify_fact`` which re-extracts the symbol
    via a READ-ONLY git read and re-hashes, returning a ``Freshness``. Mismatch
    downgrades freshness in place (STALE), symbol gone => EXPIRED (dropped by the
    gate), identical => FRESH. ``verify_fact`` itself returns STALE on any error
    (never FRESH), satisfying the abstain-safe rule. If we cannot resolve the
    repo working tree we leave the stored freshness untouched.
    """
    if _code is None or not hasattr(_code, "GitReader"):
        return ku
    repo_path = _repo_path_for(ku)
    if repo_path is None:
        return ku
    try:
        reader = _code.GitReader(repo_path)
        fresh = reader.verify_fact(ku)
        if fresh is None:
            return ku
        changes = {"freshness": fresh}
        if _val(fresh) == "EXPIRED":
            # symbol gone: evidence broken; block from supporting answers.
            changes["serve_blocked"] = True
        return _apply_ku_changes(ku, changes)
    except Exception:
        # abstain-safe: never upgrade to FRESH on verify failure; mark UNKNOWN.
        return _apply_ku_changes(ku, {"freshness": _coerce_freshness("UNKNOWN")})


def _apply_ku_changes(ku, changes: dict):
    """Apply field changes to a KU, supporting frozen dataclasses.

    For frozen model.KnowledgeUnit instances uses :func:`dataclasses.replace`
    (returns a new instance); for duck-typed fallback objects mutates in place.
    On any failure returns the original KU unchanged (abstain rules still gate).
    """
    import dataclasses
    if dataclasses.is_dataclass(ku) and not isinstance(ku, type):
        try:
            return dataclasses.replace(ku, **changes)
        except Exception:
            return ku
    try:
        for k, v in changes.items():
            setattr(ku, k, v)
    except Exception:
        pass
    return ku


def _passes_hard_gates(ku, external_facing: bool) -> bool:
    """Apply pre-ranking hard gates. Return True if the candidate survives.

    Dropped entirely: EXPIRED, DEPRECATED, serve_blocked. When
    ``external_facing`` is True, also drop taint != CLEAN (§3.5 C3 分库).
    """
    if getattr(ku, "serve_blocked", False):
        return False
    fresh = _val(getattr(ku, "freshness", None))
    if fresh == "EXPIRED":
        return False
    auth = _val(getattr(ku, "authority", None))
    if auth == "DEPRECATED":
        return False
    if external_facing:
        taint = _val(getattr(ku, "taint", "SENSITIVE")) or "SENSITIVE"
        if taint != "CLEAN":
            return False
    return True


# ---------------------------------------------------------------------------
# Relevance computation.
# ---------------------------------------------------------------------------
def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _lexical_overlap(query_tokens: set, ku) -> float:
    """Token-overlap fraction of query tokens present in the KU text."""
    if not query_tokens:
        return 0.0
    body = getattr(ku, "body", None) or ""
    title = getattr(ku, "title", "") or ""
    symbol = getattr(ku, "symbol", "") or ""
    ku_tokens = set(bigram_tokenize(f"{title} {symbol} {body}"))
    if not ku_tokens:
        return 0.0
    return len(query_tokens & ku_tokens) / len(query_tokens)


def _minmax_normalize(values: list[float]) -> list[float]:
    """Min-max normalize a list to [0,1]; constant lists map to 1.0."""
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if math.isclose(hi, lo):
        return [1.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


# ---------------------------------------------------------------------------
# Retrieval (L1 symbol exact + L2 FTS/inverted).
# ---------------------------------------------------------------------------
def _fts_available(conn) -> bool:
    """Detect whether the FTS5 ku_fts table exists (vs inverted fallback)."""
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ku_fts'"
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _symbol_lookup(conn, key, query_tokens: set, repos):
    """L1: exact symbol index lookup for CODE-flavored queries.

    A query token matching a known ``ku_symbol.symbol`` yields exact hits with
    relevance 1.0. Returns a dict ku_id -> Candidate.
    """
    out: dict = {}
    if not query_tokens:
        return out
    try:
        rows = conn.execute("SELECT DISTINCT symbol, ku_id, repo FROM ku_symbol").fetchall()
    except Exception:
        return out
    for r in rows:
        sym = r["symbol"] if not isinstance(r, dict) else r.get("symbol")
        repo = r["repo"] if not isinstance(r, dict) else r.get("repo")
        ku_id = r["ku_id"] if not isinstance(r, dict) else r.get("ku_id")
        if repos and repo not in repos:
            continue
        sym_tokens = set(bigram_tokenize(sym or ""))
        if (sym and sym.lower() in query_tokens) or (sym_tokens & query_tokens):
            try:
                krow = conn.execute("SELECT * FROM ku WHERE ku_id=?", (ku_id,)).fetchone()
            except Exception:
                continue
            if krow is None:
                continue
            ku = _row_to_ku(conn, key, krow)
            out[ku_id] = Candidate(ku=ku, relevance=1.0, lexical_overlap=1.0, via="symbol")
    return out


def _fts_search(conn, key, query: str, query_tokens: set, top_n: int, repos):
    """L2: FTS5 bm25 search; relevance = min-max-normalized negative bm25."""
    out: dict = {}
    match = to_fts_query(query)
    if not match:
        return out
    try:
        rows = conn.execute(
            "SELECT ku_id, bm25(ku_fts) AS rank FROM ku_fts WHERE ku_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (match, max(top_n * 4, 20)),
        ).fetchall()
    except Exception:
        return out
    raw = []
    for r in rows:
        ku_id = r["ku_id"] if not isinstance(r, dict) else r.get("ku_id")
        rank = r["rank"] if not isinstance(r, dict) else r.get("rank")
        raw.append((ku_id, rank if rank is not None else 0.0))
    if not raw:
        return out
    # bm25 is lower=better; negate so higher=better, then min-max normalize.
    neg = [-v for (_, v) in raw]
    norm = _minmax_normalize(neg)
    for (ku_id, _), rel in zip(raw, norm):
        try:
            krow = conn.execute("SELECT * FROM ku WHERE ku_id=?", (ku_id,)).fetchone()
        except Exception:
            continue
        if krow is None:
            continue
        repo = krow["repo"] if not isinstance(krow, dict) else krow.get("repo")
        if repos and repo not in repos:
            continue
        ku = _row_to_ku(conn, key, krow)
        lex = _lexical_overlap(query_tokens, ku)
        out[ku_id] = Candidate(ku=ku, relevance=rel, lexical_overlap=lex, via="fts")
    return out


def _inverted_search(conn, key, query_tokens: set, top_n: int, repos):
    """L2 fallback (no FTS5): ku_inverted lookup; relevance = Jaccard overlap."""
    out: dict = {}
    if not query_tokens:
        return out
    placeholders = ",".join("?" for _ in query_tokens)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT ku_id FROM ku_inverted WHERE term IN ({placeholders})",
            tuple(query_tokens),
        ).fetchall()
    except Exception:
        return out
    for r in rows:
        ku_id = r["ku_id"] if not isinstance(r, dict) else r.get("ku_id")
        try:
            krow = conn.execute("SELECT * FROM ku WHERE ku_id=?", (ku_id,)).fetchone()
        except Exception:
            continue
        if krow is None:
            continue
        repo = krow["repo"] if not isinstance(krow, dict) else krow.get("repo")
        if repos and repo not in repos:
            continue
        ku = _row_to_ku(conn, key, krow)
        rel = _jaccard(query_tokens, set(bigram_tokenize(
            f"{getattr(ku, 'title', '')} {getattr(ku, 'symbol', '')} {getattr(ku, 'body', '') or ''}"
        )))
        lex = _lexical_overlap(query_tokens, ku)
        out[ku_id] = Candidate(ku=ku, relevance=rel, lexical_overlap=lex, via="inverted")
    return out


def retrieve(conn, key, query: str, *, top_n: int = 5,
             external_facing: bool = False, repos=None) -> list[Candidate]:
    """Retrieve scored candidates for *query* (L1 symbol + L2 fts/inverted).

    Pipeline: L1 exact symbol lookup (CODE-flavored), then L2 lexical match.
    CODE-KUs are lazily re-verified (``kdl.code.verify_fact``) before gating.
    Hard gates (EXPIRED/DEPRECATED/serve_blocked dropped; external_facing drops
    taint!=CLEAN) run BEFORE ranking. Returns candidates sorted by fused score,
    truncated to ``top_n``. Any error degrades to an empty list (serve abstains).
    """
    try:
        settings = kdl_settings()
        query_tokens = set(bigram_tokenize(query))
        # L1 first — symbol hits win on relevance.
        cands: dict = _symbol_lookup(conn, key, query_tokens, repos)
        # L2 lexical.
        if _fts_available(conn):
            l2 = _fts_search(conn, key, query, query_tokens, top_n, repos)
        else:
            l2 = _inverted_search(conn, key, query_tokens, top_n, repos)
        for ku_id, c in l2.items():
            if ku_id not in cands:  # symbol hit takes precedence
                cands[ku_id] = c

        survivors: list[Candidate] = []
        for c in cands.values():
            ku = c.ku
            # Lazy CODE verification (real-time, read-only git).
            if _val(getattr(ku, "source_type", None)) == "CODE":
                ku = _lazy_verify_code(conn, key, ku)
                c.ku = ku
            if not _passes_hard_gates(ku, external_facing):
                continue
            c.fused = score(c, settings)
            survivors.append(c)

        survivors.sort(key=lambda c: c.fused, reverse=True)
        return survivors[: max(top_n, 0)]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Scoring.
# ---------------------------------------------------------------------------
def score(candidate: Candidate, settings: Optional[KdlSettings] = None) -> float:
    """Fused score = w_f*freshness + w_a*authority + w_r*relevance.

    Component tables per §retrieval_design. STALE survives the gate but is
    heavily down-weighted via its 0.3 freshness score. Confidence is sort/
    telemetry only and NEVER gates answerability.
    """
    if settings is None:
        settings = kdl_settings()
    ku = candidate.ku
    fresh = _FRESH_SCORE.get(_val(getattr(ku, "freshness", None)), 0.3)
    auth = _AUTH_SCORE.get(_val(getattr(ku, "authority", None)), 0.2)
    rel = candidate.relevance
    return settings.w_fresh * fresh + settings.w_auth * auth + settings.w_rel * rel


# ---------------------------------------------------------------------------
# Contradiction detection (A5): only among REVIEWED+ KUs (DRAFT excluded).
# ---------------------------------------------------------------------------
def _is_reviewed_plus(ku) -> bool:
    return _AUTH_RANK.get(_val(getattr(ku, "authority", None)), 0) >= 1


def _qa_fingerprint(ku) -> str:
    """Stable fingerprint for a QA question (title-based bigram signature)."""
    toks = sorted(set(bigram_tokenize(getattr(ku, "title", "") or "")))
    return "|".join(toks)


def _answer_hash(ku) -> str:
    """Coarse hash of a KU's answer body for divergence detection."""
    body = getattr(ku, "body", None) or ""
    return str(hash(re.sub(r"\s+", " ", body.strip())))


def _detect_contradiction(candidates: list[Candidate]) -> bool:
    """True if >=2 REVIEWED+ KUs share a key but diverge in content.

    CODE key = (repo, symbol) compared by content_hash; QA key = question
    fingerprint compared by answer hash. DRAFT KUs are excluded (anti-DoS A5).
    """
    code_groups: dict = {}
    qa_groups: dict = {}
    for c in candidates:
        ku = c.ku
        if not _is_reviewed_plus(ku):
            continue
        st = _val(getattr(ku, "source_type", None))
        if st == "CODE":
            # Key by (repo, file_path, symbol): same-named symbols in DIFFERENT
            # files of one repo (__init__/main/Config/...) are NOT contradictions
            # — only a same file+symbol with diverging content_hash is.
            keyc = (getattr(ku, "repo", None), getattr(ku, "file_path", None), getattr(ku, "symbol", None))
            if keyc[2] is None:
                continue
            code_groups.setdefault(keyc, set()).add(getattr(ku, "content_hash", None))
        elif st == "QA":
            fp = _qa_fingerprint(ku)
            if not fp:
                continue
            qa_groups.setdefault(fp, set()).add(_answer_hash(ku))
    for vals in code_groups.values():
        if len([v for v in vals if v is not None]) >= 2:
            return True
    for vals in qa_groups.values():
        if len(vals) >= 2:
            return True
    return False


def _has_commitment_marker(query: str) -> bool:
    """Phase1 flag for commitment/decision/external-口径 markers in *query*."""
    if not query:
        return False
    low = query.lower()
    return any(m.lower() in low for m in _COMMITMENT_MARKERS)


# ---------------------------------------------------------------------------
# Serve: the structured Verdict producer.
# ---------------------------------------------------------------------------
def serve(conn, key, query: str, *, top_n: Optional[int] = None,
          external_facing: bool = False, repos=None):
    """Produce a structured :class:`Verdict` for *query*.

    Applies all six ABSTAIN rules (§abstain_rules):
      1. no hit OR all hits authority==DRAFT.
      2. all candidates EXPIRED, OR best is STALE with no FRESH alternative.
      3. top candidate provenance broken (retrievable False / evidence_broken).
      4. contradiction among REVIEWED+ KUs.
      5. top1 relevance double-low (fused rel < REL_MIN AND lexical < LEX_MIN).
      6. commitment/decision/外部口径 markers (phase1 flag => abstain).
      plus a confidence floor: below CONF_FLOOR with no AUTHORITATIVE/REVIEWED
      hit => abstain.

    Citations carry ONLY source identifiers (no body, no raw quote). Any
    exception => ABSTAIN (绝不编). Never sends, never calls dws.
    """
    try:
        settings = kdl_settings()
        n = settings.top_n if top_n is None else top_n

        # Rule 6: commitment markers — flag and defer to triage (phase2).
        if _has_commitment_marker(query):
            return _make_verdict(DECISION_ABSTAIN, "commitment_marker", 0.0, [], [])

        candidates = retrieve(
            conn, key, query, top_n=n, external_facing=external_facing, repos=repos
        )

        # Rule 1a: no hit.
        if not candidates:
            return _make_verdict(DECISION_ABSTAIN, "no_hit", 0.0, [], [])

        authorities = [_val(getattr(c.ku, "authority", None)) for c in candidates]
        freshnesses = [_val(getattr(c.ku, "freshness", None)) for c in candidates]

        # Rule 1b: all hits are DRAFT.
        if all(a == "DRAFT" for a in authorities):
            return _make_verdict(DECISION_ABSTAIN, "all_draft", 0.0,
                                 _citations(candidates), [])

        # Rule 2a: all candidates EXPIRED (defensive; gate usually drops them).
        if all(f == "EXPIRED" for f in freshnesses):
            return _make_verdict(DECISION_ABSTAIN, "all_expired", 0.0,
                                 _citations(candidates), [])

        top = candidates[0]
        top_ku = top.ku

        # Rule 3: top candidate provenance broken.
        if _evidence_broken(top_ku):
            return _make_verdict(DECISION_ABSTAIN, "broken_provenance", top.fused,
                                 _citations(candidates), [])

        # Rule 2b: best is STALE with no FRESH alternative anywhere.
        if _val(getattr(top_ku, "freshness", None)) == "STALE" \
                and not any(f == "FRESH" for f in freshnesses):
            return _make_verdict(DECISION_ABSTAIN, "best_stale_no_fresh", top.fused,
                                 _citations(candidates), [])

        # Rule 4: contradiction among REVIEWED+ KUs.
        if _detect_contradiction(candidates):
            return _make_verdict(DECISION_ABSTAIN, "contradiction", top.fused,
                                 _citations(candidates), [])

        # Rule 5: top1 relevance double-low gate.
        if top.relevance < settings.abstain_rel_min \
                and top.lexical_overlap < settings.abstain_lex_min:
            return _make_verdict(DECISION_ABSTAIN, "low_relevance", top.fused,
                                 _citations(candidates), [])

        # Confidence floor: below floor AND no authoritative/reviewed support.
        has_strong = any(_is_reviewed_plus(c.ku) for c in candidates)
        if top.fused < settings.conf_floor and not has_strong:
            return _make_verdict(DECISION_ABSTAIN, "low_relevance", top.fused,
                                 _citations(candidates), [])

        # ANSWERABLE: keep the decrypted KUs in-memory for the local assembler.
        return _make_verdict(
            DECISION_ANSWERABLE, "ok", top.fused,
            _citations(candidates), [c.ku for c in candidates],
        )
    except Exception:
        # abstain is the safe default on any failure (decrypt/verify/sql/...).
        return _make_verdict(DECISION_ABSTAIN, "no_hit", 0.0, [], [])


def _citations(candidates: list[Candidate]) -> list:
    return [_make_citation(c.ku, c.fused) for c in candidates]


# ---------------------------------------------------------------------------
# Coverage / grounding gate (A4).
# ---------------------------------------------------------------------------
def coverage_ratio(text: str, kus) -> float:
    """Fraction of *text*'s content tokens covered by the cited KU bodies.

    Deterministic char/token coverage (no LLM): tokenize the draft text with
    the shared tokenizer, tokenize the union of cited KU bodies+titles, and
    return |draft_tokens ∩ ku_tokens| / |draft_tokens|. Empty draft => 1.0
    (nothing to ground); empty corpus with non-empty draft => 0.0.
    """
    draft_tokens = set(bigram_tokenize(text or ""))
    if not draft_tokens:
        return 1.0
    corpus = []
    for ku in (kus or []):
        corpus.append(getattr(ku, "title", "") or "")
        corpus.append(getattr(ku, "body", "") or "")
    ku_tokens = set(bigram_tokenize(" ".join(corpus)))
    if not ku_tokens:
        return 0.0
    return len(draft_tokens & ku_tokens) / len(draft_tokens)


def coverage_ok(text: str, citations=None, kus=None,
                settings: Optional[KdlSettings] = None) -> bool:
    """True if :func:`coverage_ratio` meets ``settings.coverage_min`` (A4).

    ``kus`` (decrypted KnowledgeUnits) are the grounding corpus. ``citations``
    is accepted for signature compatibility but carries no body and is not used
    for coverage. Insufficient coverage forces ABSTAIN at the call site.
    """
    if settings is None:
        settings = kdl_settings()
    return coverage_ratio(text, kus or []) >= settings.coverage_min


# ---------------------------------------------------------------------------
# Local-only draft assembly (NEVER transmitted).
# ---------------------------------------------------------------------------
def assemble_draft(verdict, key=None) -> DraftPreview:
    """Assemble an operator-local 'if I answered' :class:`DraftPreview`.

    Only produces ``draft_text`` when the verdict is ANSWERABLE AND the
    deterministic coverage gate passes; otherwise ``would_answer=False`` and
    ``draft_text=None`` with the abstain reason set (绝不编). The draft cites
    only source identifiers (kind+ref), never the raw quote or body (§2.1.6 A7),
    and is prefixed '助理代答(待本人复核)'. This object is NEVER sent.
    """
    settings = kdl_settings()
    prefix = "助理代答(待本人复核)"
    try:
        decision = _val(getattr(verdict, "decision", DECISION_ABSTAIN))
        citations = list(getattr(verdict, "citations", []) or [])

        if decision != DECISION_ANSWERABLE:
            reason = getattr(verdict, "reason", "abstain") or "abstain"
            return DraftPreview(
                would_answer=False, draft_text=None, citations=citations,
                assistant_prefix=prefix, abstain_reason=reason,
            )

        kus = list(getattr(verdict, "kus", []) or [])
        # Build a draft strictly from the cited KU bodies (top candidate first).
        body_parts = []
        for ku in kus[: settings.top_n]:
            body = getattr(ku, "body", None)
            if body:
                body_parts.append(body.strip())
        draft_text = "\n\n".join(body_parts).strip()

        # A4 grounding gate: draft must be covered by its citations.
        if not draft_text or not coverage_ok(draft_text, citations, kus, settings):
            return DraftPreview(
                would_answer=False, draft_text=None, citations=citations,
                assistant_prefix=prefix, abstain_reason="low_coverage",
            )

        return DraftPreview(
            would_answer=True, draft_text=draft_text, citations=citations,
            assistant_prefix=prefix, abstain_reason=None,
        )
    except Exception:
        return DraftPreview(
            would_answer=False, draft_text=None, citations=[],
            assistant_prefix=prefix, abstain_reason="error",
        )
