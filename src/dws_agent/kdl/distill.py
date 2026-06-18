"""Pluggable distiller: raw source material -> knowledge-candidate JSON ONLY.

The distiller is the *only* place where an LLM could ever be involved, so it is
deliberately walled off behind a small :class:`Distiller` protocol that returns
plain ``dict`` candidates and has **no side effects**: it never touches the DB,
never sends anything, never issues a dws command. Whether the backend is a real
LLM or the shipped deterministic :class:`StubDistiller`, the contract is the
same — produce candidate dicts; *persisting* them is the job of the no-LLM
:class:`~dws_agent.kdl.ingest.Ingestor`, which re-validates everything by rule.

Phase1 ships ONLY the deterministic ``stub`` backend so tests never depend on a
real LLM or network. Selecting an unknown backend falls back to the stub (and
is audited by the caller).

Candidate dict schema (the bridge to ``ingest.Ingestor.validate``)::

    {
      "source_type": "CODE"|"ISSUE"|"QA"|"PLAYBOOK",   # required
      "title":       str,                               # required, non-empty
      "body":        str,                               # required (PLAINTEXT;
                                                        # redacted at ingest)
      "provenance": [                                   # >=1 required, else the
        {                                               # candidate is DROPPED
          "kind": "COMMIT"|"ISSUE_URL"|"MSG_ID"|"DOC_ID"|"MAIL_ID"|"FILE",
          "ref":  str,                                  # the recheck pointer
          "quote": str,                                 # optional excerpt
          "quote_taint": "CLEAN"|"INTERNAL"|"SENSITIVE",# optional, default CLEAN
          "captured_at": str,                           # optional RFC3339
        }, ...
      ],
      # optional declared taint hint (ingest still MAX-merges with redaction):
      "declared_taint": "CLEAN"|"INTERNAL"|"SENSITIVE",
      # CODE-only fields (None/absent for other source types):
      "repo": str, "commit_sha": str, "file_path": str, "symbol": str,
      "line_range": [int, int], "content_hash": str,
      # optional graph hints used to build ku_edge (ISSUE/QA <-> CODE):
      "linked_symbols": [ {"repo":..,"file_path":..,"symbol":..}, ... ],
      "owner": str,
    }

Authority is intentionally NOT set by the distiller: every candidate enters at
DRAFT (the Ingestor forces it), and only operator-confirmed QA may later be
promoted to AUTHORITATIVE. The distiller never asserts authority or public_ok.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Raw input container
# ---------------------------------------------------------------------------


@dataclass
class RawItem:
    """A single raw unit handed to a distiller.

    Attributes:
        source_type: one of CODE|ISSUE|QA|PLAYBOOK (string; the SourceType
            enum lives in :mod:`dws_agent.kdl.model` but the distiller stays
            decoupled and only emits string values).
        text: the raw source text (code slice / issue body / chat / SOP).
        meta: free-form metadata the adapter already knows — e.g. for CODE
            ``repo/commit_sha/file_path/symbol/line_range/content_hash``, for
            ISSUE ``issue_id/url``, for QA ``question/answer/msg_id``, for
            PLAYBOOK ``doc_id``. Distillers read meta to populate provenance.
    """

    source_type: str
    text: str
    meta: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Distiller protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Distiller(Protocol):
    """Turns a :class:`RawItem` into a list of candidate dicts.

    Implementations MUST be pure: no DB writes, no network, no dws commands.
    A real-LLM backend would only ever return *candidate JSON*; rule-based
    landing into the store is exclusively the Ingestor's responsibility.
    """

    def distill(self, raw: RawItem) -> List[dict]:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# Helpers shared by the stub rules
# ---------------------------------------------------------------------------

# ISSUE heading detection: Chinese + English synonyms for the three sections.
_ISSUE_HEADINGS = {
    "symptom": re.compile(
        r"^\s*(?:#+\s*)?(?:症状|现象|问题描述|symptom|symptoms|problem)\s*[:：]?\s*$",
        re.IGNORECASE,
    ),
    "root_cause": re.compile(
        r"^\s*(?:#+\s*)?(?:根因|根本原因|原因分析|原因|root[\s_-]?cause|cause)\s*[:：]?\s*$",
        re.IGNORECASE,
    ),
    "fix": re.compile(
        r"^\s*(?:#+\s*)?(?:处置|解决|解决方案|修复|处理|fix|resolution|workaround)\s*[:：]?\s*$",
        re.IGNORECASE,
    ),
}

# Inline "症状: ...." single-line form, e.g. "根因：xxx".
_ISSUE_INLINE = {
    "symptom": re.compile(r"(?:症状|现象|symptom)\s*[:：]\s*(.+)", re.IGNORECASE),
    "root_cause": re.compile(r"(?:根因|根本原因|root[\s_-]?cause)\s*[:：]\s*(.+)", re.IGNORECASE),
    "fix": re.compile(r"(?:处置|解决方案|修复|fix|resolution)\s*[:：]\s*(.+)", re.IGNORECASE),
}

# Numbered step lines for PLAYBOOK extraction: "1. ", "1) ", "步骤1:", "第一步".
_STEP_RE = re.compile(
    r"^\s*(?:(?:第)?\s*(\d+|[一二三四五六七八九十]+)\s*[\.\)、:：]|步骤\s*\d+\s*[:：]?)\s*(.+)$"
)


def _nonempty(s: Optional[str]) -> bool:
    return isinstance(s, str) and s.strip() != ""


def _meta_provenance(raw: RawItem, default_kind: str) -> List[dict]:
    """Build provenance entries from ``raw.meta`` when the adapter supplied a
    pointer. Returns ``[]`` when no usable pointer exists (the Ingestor will
    then DROP the candidate — provenance is a hard requirement).
    """
    meta = raw.meta or {}
    provs: List[dict] = []

    # Explicit provenance passed straight through (already shaped) wins.
    explicit = meta.get("provenance")
    if isinstance(explicit, list):
        for p in explicit:
            if isinstance(p, dict) and _nonempty(p.get("ref")):
                provs.append(dict(p))
        if provs:
            return provs

    # Otherwise synthesise from well-known meta keys per source type.
    captured = meta.get("captured_at")
    if raw.source_type == "CODE":
        commit = meta.get("commit_sha") or meta.get("commit")
        fp = meta.get("file_path")
        if _nonempty(commit):
            ref = f"{commit}:{fp}" if _nonempty(fp) else str(commit)
            provs.append({"kind": "COMMIT", "ref": ref, "captured_at": captured})
    elif raw.source_type == "ISSUE":
        ref = meta.get("url") or meta.get("issue_id") or meta.get("id")
        if _nonempty(ref):
            kind = "ISSUE_URL" if str(ref).startswith("http") else "DOC_ID"
            provs.append({"kind": kind, "ref": str(ref), "captured_at": captured})
    elif raw.source_type == "QA":
        ref = meta.get("msg_id") or meta.get("message_id")
        if _nonempty(ref):
            provs.append({"kind": "MSG_ID", "ref": str(ref), "captured_at": captured})
    elif raw.source_type == "PLAYBOOK":
        ref = meta.get("doc_id") or meta.get("url")
        if _nonempty(ref):
            kind = "DOC_ID" if not str(ref).startswith("http") else "ISSUE_URL"
            provs.append({"kind": kind, "ref": str(ref), "captured_at": captured})

    # Mail fallback (any source) if a mail id is present.
    mail = meta.get("mail_id")
    if _nonempty(mail):
        provs.append({"kind": "MAIL_ID", "ref": str(mail), "captured_at": captured})

    return provs


def _code_meta_fields(raw: RawItem) -> Dict[str, Any]:
    """Copy CODE-only structural fields from meta into a candidate."""
    meta = raw.meta or {}
    out: Dict[str, Any] = {}
    for k in ("repo", "commit_sha", "file_path", "symbol", "content_hash"):
        if _nonempty(meta.get(k)) or isinstance(meta.get(k), (int,)):
            out[k] = meta.get(k)
    lr = meta.get("line_range")
    if isinstance(lr, (list, tuple)) and len(lr) == 2:
        out["line_range"] = [int(lr[0]), int(lr[1])]
    return out


# ---------------------------------------------------------------------------
# Deterministic, offline stub distiller
# ---------------------------------------------------------------------------


class StubDistiller:
    """Deterministic, no-LLM, offline distiller (the only phase1 backend).

    Rules per source type (all output enters at DRAFT; authority/public_ok are
    never asserted here):

    * CODE — one candidate per extracted symbol. When the adapter already ran
      symbol extraction (``raw.meta['symbols']`` list of dicts with
      ``symbol``/``signature``/``docstring``/...), one candidate is produced per
      symbol with ``title=symbol`` and ``body`` = signature + docstring slice.
      Otherwise a single candidate is produced from the whole slice using the
      meta symbol.
    * ISSUE — split symptom / root-cause / fix by heading regex (block form)
      or inline ``key: value`` form; body is a normalised three-section text.
    * QA — passthrough of an already-paired question/answer (from
      ``raw.meta``); body = ``Q: ...\\nA: ...``.
    * PLAYBOOK — numbered-step extraction; body = the ordered step list.

    Every candidate gets provenance derived from ``raw.meta`` (see
    :func:`_meta_provenance`); candidates lacking a usable pointer are still
    emitted (the Ingestor enforces the >=1-provenance drop, keeping the policy
    in one deterministic place).
    """

    name = "stub"

    def distill(self, raw: RawItem) -> List[dict]:
        """Dispatch on ``raw.source_type``; returns candidate dicts (possibly
        empty). Unknown source types yield ``[]``."""
        st = (raw.source_type or "").upper()
        if st == "CODE":
            return self._distill_code(raw)
        if st == "ISSUE":
            return self._distill_issue(raw)
        if st == "QA":
            return self._distill_qa(raw)
        if st == "PLAYBOOK":
            return self._distill_playbook(raw)
        return []

    # -- CODE -------------------------------------------------------------
    def _distill_code(self, raw: RawItem) -> List[dict]:
        meta = raw.meta or {}
        prov = _meta_provenance(raw, "COMMIT")
        symbols = meta.get("symbols")
        cands: List[dict] = []

        if isinstance(symbols, list) and symbols:
            for sym in symbols:
                if not isinstance(sym, dict):
                    continue
                name = sym.get("symbol") or sym.get("name")
                if not _nonempty(name):
                    continue
                sig = sym.get("signature") or ""
                doc = sym.get("docstring") or ""
                body_parts = [p for p in (sig, doc) if _nonempty(p)]
                body = "\n".join(body_parts) if body_parts else (sym.get("source") or raw.text)
                # Per-symbol provenance pins the exact file/commit/symbol.
                sym_prov = self._symbol_provenance(meta, sym, prov)
                cand = {
                    "source_type": "CODE",
                    "title": str(name),
                    "body": body,
                    "provenance": sym_prov,
                    "repo": sym.get("repo") or meta.get("repo"),
                    "commit_sha": sym.get("commit_sha") or meta.get("commit_sha") or meta.get("commit"),
                    "file_path": sym.get("file_path") or meta.get("file_path"),
                    "symbol": str(name),
                    "content_hash": sym.get("content_hash"),
                    "owner": meta.get("owner"),
                }
                lr = sym.get("line_range")
                if isinstance(lr, (list, tuple)) and len(lr) == 2:
                    cand["line_range"] = [int(lr[0]), int(lr[1])]
                cands.append(cand)
            return cands

        # No pre-extracted symbols: single candidate from the whole slice.
        title = meta.get("symbol") or meta.get("file_path") or "code"
        cand = {
            "source_type": "CODE",
            "title": str(title),
            "body": raw.text,
            "provenance": prov,
            "symbol": meta.get("symbol"),
            "owner": meta.get("owner"),
        }
        cand.update(_code_meta_fields(raw))
        cands.append(cand)
        return cands

    @staticmethod
    def _symbol_provenance(meta: Dict[str, Any], sym: Dict[str, Any], fallback: List[dict]) -> List[dict]:
        commit = sym.get("commit_sha") or meta.get("commit_sha") or meta.get("commit")
        fp = sym.get("file_path") or meta.get("file_path")
        name = sym.get("symbol") or sym.get("name")
        if _nonempty(commit):
            ref = f"{commit}:{fp}#{name}" if _nonempty(fp) else str(commit)
            return [{"kind": "COMMIT", "ref": ref, "captured_at": meta.get("captured_at")}]
        return list(fallback)

    # -- ISSUE ------------------------------------------------------------
    def _distill_issue(self, raw: RawItem) -> List[dict]:
        sections = self._split_issue_sections(raw.text)
        # Normalised body keeps the three sections explicit and stable-ordered.
        ordered = []
        for key, label in (("symptom", "症状"), ("root_cause", "根因"), ("fix", "处置")):
            val = sections.get(key)
            if _nonempty(val):
                ordered.append(f"{label}: {val.strip()}")
        body = "\n".join(ordered) if ordered else raw.text.strip()

        meta = raw.meta or {}
        title = meta.get("title") or (sections.get("symptom") or raw.text.strip().splitlines()[0] if raw.text.strip() else "issue")
        cand = {
            "source_type": "ISSUE",
            "title": str(title)[:120],
            "body": body,
            "provenance": _meta_provenance(raw, "ISSUE_URL"),
            "owner": meta.get("owner"),
        }
        linked = meta.get("linked_symbols")
        if isinstance(linked, list) and linked:
            cand["linked_symbols"] = linked
        return [cand]

    @staticmethod
    def _split_issue_sections(text: str) -> Dict[str, str]:
        """Return {symptom,root_cause,fix} extracted by heading or inline regex."""
        out: Dict[str, str] = {}
        lines = (text or "").splitlines()

        # 1) block-heading form: a heading line then following lines until next heading.
        current: Optional[str] = None
        buf: List[str] = []

        def flush() -> None:
            if current and buf:
                out.setdefault(current, "\n".join(buf).strip())

        for line in lines:
            matched_heading = None
            for key, pat in _ISSUE_HEADINGS.items():
                if pat.match(line):
                    matched_heading = key
                    break
            if matched_heading:
                flush()
                current = matched_heading
                buf = []
                continue
            if current is not None:
                buf.append(line)
        flush()

        # 2) inline form fills any section the block form missed.
        for key, pat in _ISSUE_INLINE.items():
            if key in out:
                continue
            for line in lines:
                m = pat.search(line)
                if m and _nonempty(m.group(1)):
                    out[key] = m.group(1).strip()
                    break
        return out

    # -- QA ---------------------------------------------------------------
    def _distill_qa(self, raw: RawItem) -> List[dict]:
        meta = raw.meta or {}
        question = meta.get("question")
        answer = meta.get("answer")
        # Fall back to splitting the raw text on a Q/A marker if meta absent.
        if not _nonempty(question) or not _nonempty(answer):
            q, a = self._split_qa_text(raw.text)
            question = question if _nonempty(question) else q
            answer = answer if _nonempty(answer) else a
        if not _nonempty(question) or not _nonempty(answer):
            return []
        body = f"Q: {question.strip()}\nA: {answer.strip()}"
        cand = {
            "source_type": "QA",
            "title": question.strip()[:120],
            "body": body,
            "provenance": _meta_provenance(raw, "MSG_ID"),
            "owner": meta.get("owner") or meta.get("answer_author"),
        }
        linked = meta.get("linked_symbols")
        if isinstance(linked, list) and linked:
            cand["linked_symbols"] = linked
        return [cand]

    @staticmethod
    def _split_qa_text(text: str) -> tuple[str, str]:
        m = re.search(r"(?:^|\n)\s*(?:Q|问)\s*[:：]\s*(.+?)\n\s*(?:A|答)\s*[:：]\s*(.+)", text or "", re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        return "", ""

    # -- PLAYBOOK ---------------------------------------------------------
    def _distill_playbook(self, raw: RawItem) -> List[dict]:
        steps = self._extract_steps(raw.text)
        if not steps:
            return []
        body = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
        meta = raw.meta or {}
        title = meta.get("title") or (raw.text.strip().splitlines()[0] if raw.text.strip() else "playbook")
        cand = {
            "source_type": "PLAYBOOK",
            "title": str(title)[:120],
            "body": body,
            "provenance": _meta_provenance(raw, "DOC_ID"),
            "owner": meta.get("owner"),
        }
        return [cand]

    @staticmethod
    def _extract_steps(text: str) -> List[str]:
        steps: List[str] = []
        for line in (text or "").splitlines():
            m = _STEP_RE.match(line)
            if m:
                content = m.group(2).strip()
                if _nonempty(content):
                    steps.append(content)
        return steps


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# Registry of available backends. Phase1 ships only the deterministic stub.
_BACKENDS = {"stub": StubDistiller}


def get_distiller(name: Optional[str] = None, *, paths=None) -> Distiller:
    """Return a distiller backend by name (factory).

    Resolution order: explicit ``name`` arg -> env ``DWS_AGENT_KDL_DISTILLER``
    (via :func:`kdl_settings`) -> default ``stub``. Only ``stub`` is shipped in
    phase1; any unknown name falls back to the stub and is audited (when a
    ``paths`` is supplied) under the reused ``cli`` event so the substitution is
    greppable. NEVER selects a network/LLM backend in phase1.
    """
    if name is None:
        try:
            from .config import kdl_settings

            name = kdl_settings().distiller
        except Exception:
            name = "stub"

    key = (name or "stub").strip().lower()
    backend_cls = _BACKENDS.get(key)
    if backend_cls is None:
        # Unknown backend -> safe stub fallback, optionally audited.
        if paths is not None:
            try:
                from dws_agent.store.audit import get_audit_logger

                get_audit_logger(paths).log(
                    {
                        "event": "cli",
                        "actor": "store",
                        "reason": "unknown_distiller_fallback_stub",
                        "detail": {"kdl_op": "get_distiller", "requested": name},
                    }
                )
            except Exception:
                pass
        backend_cls = StubDistiller
    return backend_cls()
