"""KDL persistence layer on the shared ``state.db``.

The Knowledge Distillation Layer (KDL) stores Knowledge Units (KUs) and their
provenance, full-text/symbol indexes, and edge graph in the SAME SQLite
``state.db`` used by phase0 (reuses :func:`store.state_db.open_state_db`). All
KDL tables are created idempotently (``CREATE TABLE IF NOT EXISTS``) so they
coexist with the phase0 ``actions``/``pending_confirm``/``cases``/``kv`` tables.

Hard constraints baked into this module (see global contract):

* **Falling-back-to-DRAFT on missing provenance** — re-enforced at
  :func:`upsert_ku`: any KU with zero provenance is forced to ``DRAFT`` and
  ``serve_blocked=True``; the caller cannot override this.
* **At-rest encryption** — KU bodies and provenance quotes are NEVER persisted
  in plaintext. They are stored only as AES-256-GCM cipher BLOBs
  (``body_cipher`` / ``quote_cipher``) via :mod:`core.crypto`. The plaintext-
  grep=0 exit criterion (§3.5) depends on this.
* **No outward send, no dws write** — this module performs SQLite I/O and
  crypto only. It never sends anything and never invokes dws.
* **No LLM** — distillation LLM use lives elsewhere and only produces candidate
  JSON; this Ingestor-side store applies deterministic rules.

Authority / freshness state machines and stale propagation along ``ku_edge``
(ISSUE<->CODE, QA<->CODE) live here (see :func:`set_authority`,
:func:`mark_stale_by_file`).
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import Iterator

from dws_agent.core.crypto import decrypt_bytes, encrypt_bytes
from dws_agent.kdl.model import (
    Authority,
    Freshness,
    KnowledgeUnit,
    Provenance,
    ProvKind,
    SourceType,
    Taint,
)

# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

# NEVER store plaintext body/quote in any column — only *_cipher BLOBs.
KDL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ku (
    ku_id          TEXT PRIMARY KEY,
    source_type    TEXT,
    title          TEXT,
    body_cipher    BLOB,           -- AES-GCM(nonce||ct||tag) of plaintext body
    body_redacted  INTEGER,
    taint          TEXT,
    authority      TEXT,
    public_ok      INTEGER,
    confidence     REAL,
    freshness      TEXT,
    repo           TEXT,
    commit_sha     TEXT,
    file_path      TEXT,
    symbol         TEXT,
    line_start     INTEGER,
    line_end       INTEGER,
    content_hash   TEXT,
    created_at     TEXT,
    updated_at     TEXT,
    last_verified_at TEXT,
    expires_at     TEXT,
    superseded_by  TEXT,
    owner          TEXT,
    serve_blocked  INTEGER,
    derived_stale  INTEGER
);

CREATE TABLE IF NOT EXISTS ku_provenance (
    prov_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ku_id        TEXT REFERENCES ku(ku_id),
    kind         TEXT,
    ref          TEXT,
    quote_cipher BLOB,             -- AES-GCM cipher of the REDACTED quote
    quote_taint  TEXT,
    captured_at  TEXT,
    retrievable  INTEGER
);

CREATE TABLE IF NOT EXISTS ku_symbol (
    repo       TEXT,
    file_path  TEXT,
    symbol     TEXT,
    ku_id      TEXT,
    commit_sha TEXT,
    PRIMARY KEY (repo, file_path, symbol, ku_id)
);

CREATE TABLE IF NOT EXISTS ku_edge (
    src_ku TEXT,
    dst_ku TEXT,
    rel    TEXT,                   -- ISSUE_CODE|QA_CODE|SUPERSEDES
    PRIMARY KEY (src_ku, dst_ku, rel)
);

CREATE TABLE IF NOT EXISTS kdl_meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE INDEX IF NOT EXISTS idx_ku_source ON ku(source_type);
CREATE INDEX IF NOT EXISTS idx_ku_authority ON ku(authority);
CREATE INDEX IF NOT EXISTS idx_ku_freshness ON ku(freshness);
CREATE INDEX IF NOT EXISTS idx_ku_repo_file ON ku(repo, file_path);
CREATE INDEX IF NOT EXISTS idx_prov_ku ON ku_provenance(ku_id);
CREATE INDEX IF NOT EXISTS idx_symbol_lookup ON ku_symbol(repo, file_path, symbol);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON ku_edge(dst_ku, rel);
"""

# FTS5 virtual table (preferred). body_bigram holds space-joined CJK bigrams +
# ascii word tokens produced by retrieve.bigram_tokenize so unicode61 matches
# CJK; the query side applies the same tokenizer.
_FTS5_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS ku_fts USING fts5("
    "ku_id UNINDEXED, title, body_bigram, symbol, tokenize='unicode61');"
)

# Fallback inverted index when FTS5 is unavailable at compile time.
_INVERTED_SQL = """
CREATE TABLE IF NOT EXISTS ku_inverted (
    term  TEXT,
    ku_id TEXT,
    field TEXT,
    PRIMARY KEY (term, ku_id, field)
);
CREATE INDEX IF NOT EXISTS idx_inverted_term ON ku_inverted(term);
"""

# Module-level flag set once by ensure_kdl_schema(): whether FTS5 compiled in.
_HAS_FTS5: bool | None = None


def _now_iso() -> str:
    """RFC3339 UTC string matching state_db's '%Y-%m-%dT%H:%M:%SZ' format."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Schema bootstrap / capability detection
# --------------------------------------------------------------------------- #


def fts5_available(conn: sqlite3.Connection) -> bool:
    """Return True if this SQLite build supports FTS5.

    Detection is a real probe: try creating a throwaway FTS5 table in a
    temporary namespace. Cached in module flag ``_HAS_FTS5`` after the first
    call (via :func:`ensure_kdl_schema`).
    """
    global _HAS_FTS5
    if _HAS_FTS5 is not None:
        return _HAS_FTS5
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS temp._kdl_fts_probe "
            "USING fts5(x);"
        )
        conn.execute("DROP TABLE IF EXISTS temp._kdl_fts_probe;")
        _HAS_FTS5 = True
    except sqlite3.Error:
        _HAS_FTS5 = False
    return _HAS_FTS5


def ensure_kdl_schema(conn: sqlite3.Connection) -> None:
    """Apply the KDL schema idempotently on the shared connection.

    Runs the core tables, then either the FTS5 virtual table or the inverted
    fallback depending on :func:`fts5_available`. Sets the module ``_HAS_FTS5``
    flag. Safe to call repeatedly.
    """
    conn.executescript(KDL_SCHEMA_SQL)
    if fts5_available(conn):
        conn.execute(_FTS5_SQL)
    else:
        conn.executescript(_INVERTED_SQL)


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _enum_value(v) -> str | None:
    """Coerce an enum / string to its stored ``.value`` form."""
    if v is None:
        return None
    return getattr(v, "value", v)


def _index_tokens(conn: sqlite3.Connection, ku: KnowledgeUnit) -> None:
    """(Re)build the FTS5 / inverted rows for one KU.

    Tokenization is delegated to :func:`kdl.retrieve.bigram_tokenize` so the
    index and query sides stay identical. Imported lazily to avoid a circular
    import (retrieve imports store).
    """
    from dws_agent.kdl.retrieve import bigram_tokenize

    title = ku.title or ""
    body = ku.body or ""
    symbol = ku.symbol or ""
    body_tokens = bigram_tokenize(body)
    body_bigram = " ".join(body_tokens)

    if fts5_available(conn):
        conn.execute("DELETE FROM ku_fts WHERE ku_id=?;", (ku.ku_id,))
        conn.execute(
            "INSERT INTO ku_fts(ku_id, title, body_bigram, symbol) "
            "VALUES(?, ?, ?, ?);",
            (ku.ku_id, " ".join(bigram_tokenize(title)), body_bigram, symbol),
        )
    else:
        conn.execute("DELETE FROM ku_inverted WHERE ku_id=?;", (ku.ku_id,))
        rows: set[tuple[str, str, str]] = set()
        for tok in bigram_tokenize(title):
            rows.add((tok, ku.ku_id, "title"))
        for tok in body_tokens:
            rows.add((tok, ku.ku_id, "body"))
        if symbol:
            rows.add((symbol.lower(), ku.ku_id, "symbol"))
        conn.executemany(
            "INSERT OR IGNORE INTO ku_inverted(term, ku_id, field) "
            "VALUES(?, ?, ?);",
            list(rows),
        )


# --------------------------------------------------------------------------- #
# Write path
# --------------------------------------------------------------------------- #


def upsert_ku(conn: sqlite3.Connection, ku: KnowledgeUnit, key: bytes) -> str:
    """Persist a KU (and its provenance / indexes), returning its ``ku_id``.

    Encrypts the plaintext body and every redacted provenance quote with
    AES-256-GCM (``key`` = ``get_keychain_secret('fileenc')``). Re-enforces the
    HARD provenance rule: a KU with no provenance is forced to ``DRAFT`` +
    ``serve_blocked=True`` regardless of what the caller passed. Rebuilds the
    FTS/inverted and symbol indexes for the KU.

    The plaintext ``ku.body`` / ``provenance.quote`` exist only in memory; only
    cipher BLOBs are written.
    """
    # --- re-enforce provenance->DRAFT hard rule (cannot be unset by caller) ---
    authority = _enum_value(ku.authority)
    serve_blocked = 1 if ku.serve_blocked else 0
    if not ku.provenance:
        authority = Authority.DRAFT.value
        serve_blocked = 1

    body_cipher = encrypt_bytes((ku.body or "").encode("utf-8"), key)

    line_start = ku.line_range[0] if ku.line_range else None
    line_end = ku.line_range[1] if ku.line_range else None

    conn.execute(
        """
        INSERT INTO ku(
            ku_id, source_type, title, body_cipher, body_redacted, taint,
            authority, public_ok, confidence, freshness, repo, commit_sha,
            file_path, symbol, line_start, line_end, content_hash,
            created_at, updated_at, last_verified_at, expires_at,
            superseded_by, owner, serve_blocked, derived_stale
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ku_id) DO UPDATE SET
            source_type=excluded.source_type,
            title=excluded.title,
            body_cipher=excluded.body_cipher,
            body_redacted=excluded.body_redacted,
            taint=excluded.taint,
            authority=excluded.authority,
            public_ok=excluded.public_ok,
            confidence=excluded.confidence,
            freshness=excluded.freshness,
            repo=excluded.repo,
            commit_sha=excluded.commit_sha,
            file_path=excluded.file_path,
            symbol=excluded.symbol,
            line_start=excluded.line_start,
            line_end=excluded.line_end,
            content_hash=excluded.content_hash,
            updated_at=excluded.updated_at,
            last_verified_at=excluded.last_verified_at,
            expires_at=excluded.expires_at,
            superseded_by=excluded.superseded_by,
            owner=excluded.owner,
            serve_blocked=excluded.serve_blocked,
            derived_stale=excluded.derived_stale;
        """,
        (
            ku.ku_id,
            _enum_value(ku.source_type),
            ku.title,
            body_cipher,
            1 if ku.body_redacted else 0,
            _enum_value(ku.taint),
            authority,
            1 if ku.public_ok else 0,
            float(ku.confidence),
            _enum_value(ku.freshness),
            ku.repo,
            ku.commit_sha,
            ku.file_path,
            ku.symbol,
            line_start,
            line_end,
            ku.content_hash,
            ku.created_at,
            ku.updated_at or _now_iso(),
            ku.last_verified_at,
            ku.expires_at,
            ku.superseded_by,
            ku.owner,
            serve_blocked,
            1 if ku.derived_stale else 0,
        ),
    )

    # Rewrite provenance rows (full replace keeps it idempotent).
    conn.execute("DELETE FROM ku_provenance WHERE ku_id=?;", (ku.ku_id,))
    for prov in ku.provenance:
        quote_cipher = encrypt_bytes((prov.quote or "").encode("utf-8"), key)
        conn.execute(
            """
            INSERT INTO ku_provenance(
                ku_id, kind, ref, quote_cipher, quote_taint, captured_at,
                retrievable
            ) VALUES (?,?,?,?,?,?,?);
            """,
            (
                ku.ku_id,
                _enum_value(prov.kind),
                prov.ref,
                quote_cipher,
                _enum_value(prov.quote_taint),
                prov.captured_at,
                1 if prov.retrievable else 0,
            ),
        )

    # Symbol index (CODE-only; rewrite for this KU).
    conn.execute("DELETE FROM ku_symbol WHERE ku_id=?;", (ku.ku_id,))
    if _enum_value(ku.source_type) == SourceType.CODE.value and ku.symbol:
        conn.execute(
            "INSERT OR REPLACE INTO ku_symbol(repo, file_path, symbol, ku_id, "
            "commit_sha) VALUES(?,?,?,?,?);",
            (ku.repo, ku.file_path, ku.symbol, ku.ku_id, ku.commit_sha),
        )

    _index_tokens(conn, ku)
    return ku.ku_id


# --------------------------------------------------------------------------- #
# Read path
# --------------------------------------------------------------------------- #


def _row_to_ku(
    conn: sqlite3.Connection, row: sqlite3.Row, key: bytes
) -> KnowledgeUnit:
    """Reconstruct a :class:`KnowledgeUnit` from a ``ku`` row (decrypts body)."""
    body = decrypt_bytes(row["body_cipher"], key).decode("utf-8") if row["body_cipher"] else ""

    provenance: list[Provenance] = []
    pcur = conn.execute(
        "SELECT * FROM ku_provenance WHERE ku_id=? ORDER BY prov_id;",
        (row["ku_id"],),
    )
    for prow in pcur.fetchall():
        quote = (
            decrypt_bytes(prow["quote_cipher"], key).decode("utf-8")
            if prow["quote_cipher"]
            else ""
        )
        provenance.append(
            Provenance(
                kind=ProvKind(prow["kind"]),
                ref=prow["ref"],
                quote=quote,
                quote_taint=Taint(prow["quote_taint"]) if prow["quote_taint"] else Taint.CLEAN,
                captured_at=prow["captured_at"],
                retrievable=bool(prow["retrievable"]),
            )
        )

    line_range = None
    if row["line_start"] is not None and row["line_end"] is not None:
        line_range = (int(row["line_start"]), int(row["line_end"]))

    return KnowledgeUnit(
        ku_id=row["ku_id"],
        source_type=SourceType(row["source_type"]),
        title=row["title"],
        body=body,
        body_redacted=bool(row["body_redacted"]),
        taint=Taint(row["taint"]) if row["taint"] else Taint.CLEAN,
        authority=Authority(row["authority"]) if row["authority"] else Authority.DRAFT,
        public_ok=bool(row["public_ok"]),
        confidence=float(row["confidence"]) if row["confidence"] is not None else 0.0,
        freshness=Freshness(row["freshness"]) if row["freshness"] else Freshness.UNKNOWN,
        provenance=provenance,
        repo=row["repo"],
        commit_sha=row["commit_sha"],
        file_path=row["file_path"],
        symbol=row["symbol"],
        line_range=line_range,
        content_hash=row["content_hash"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_verified_at=row["last_verified_at"],
        expires_at=row["expires_at"],
        superseded_by=row["superseded_by"],
        owner=row["owner"],
        serve_blocked=bool(row["serve_blocked"]),
        derived_stale=bool(row["derived_stale"]),
    )


def get_ku(
    conn: sqlite3.Connection, ku_id: str, key: bytes
) -> KnowledgeUnit | None:
    """Load a single KU by id, decrypting body and provenance quotes.

    Returns ``None`` if no such KU exists.
    """
    cur = conn.execute("SELECT * FROM ku WHERE ku_id=?;", (ku_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return _row_to_ku(conn, row, key)


def iter_kus(
    conn: sqlite3.Connection, key: bytes, source_type=None
) -> Iterator[KnowledgeUnit]:
    """Yield every KU (optionally filtered by ``source_type``), decrypted."""
    st = _enum_value(source_type)
    if st is not None:
        cur = conn.execute(
            "SELECT * FROM ku WHERE source_type=? ORDER BY ku_id;", (st,)
        )
    else:
        cur = conn.execute("SELECT * FROM ku ORDER BY ku_id;")
    for row in cur.fetchall():
        yield _row_to_ku(conn, row, key)


# --------------------------------------------------------------------------- #
# Authority state machine
# --------------------------------------------------------------------------- #

# Allowed authority transitions (state machine). DRAFT->AUTHORITATIVE direct is
# rejected: AUTHORITATIVE requires a prior REVIEWED state (strong confirm). Any
# state may be DEPRECATED. DEPRECATED is terminal (only re-review back to
# REVIEWED is allowed to recover).
_ALLOWED_AUTHORITY_TRANSITIONS: dict[str, set[str]] = {
    Authority.DRAFT.value: {Authority.REVIEWED.value, Authority.DEPRECATED.value},
    Authority.REVIEWED.value: {
        Authority.AUTHORITATIVE.value,
        Authority.DEPRECATED.value,
        Authority.DRAFT.value,
    },
    Authority.AUTHORITATIVE.value: {
        Authority.REVIEWED.value,
        Authority.DEPRECATED.value,
    },
    Authority.DEPRECATED.value: {Authority.REVIEWED.value},
}


def set_authority(
    conn: sqlite3.Connection, ku_id: str, authority, reason: str
) -> None:
    """Transition a KU's authority through the legal state machine.

    Transitions: DRAFT->REVIEWED (light confirm), REVIEWED->AUTHORITATIVE
    (strong confirm, i.e. "我已确认"), any->DEPRECATED. Illegal transitions
    (e.g. DRAFT->AUTHORITATIVE direct) raise :class:`ValueError`.

    A KU with no provenance can NEVER leave DRAFT (provenance hard rule):
    promoting such a KU raises :class:`ValueError`.

    ``reason`` is recorded as audit context by the caller; this function only
    enforces the transition and updates ``serve_blocked`` accordingly.
    """
    target = _enum_value(authority)
    cur = conn.execute(
        "SELECT authority FROM ku WHERE ku_id=?;", (ku_id,)
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown ku_id: {ku_id}")
    current = row["authority"]

    if target == current:
        return  # no-op

    allowed = _ALLOWED_AUTHORITY_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise ValueError(
            f"illegal authority transition {current!r}->{target!r} "
            f"for {ku_id} (reason={reason!r})"
        )

    # Provenance hard rule: cannot promote a provenance-less KU above DRAFT.
    pcount = conn.execute(
        "SELECT COUNT(*) AS n FROM ku_provenance WHERE ku_id=?;", (ku_id,)
    ).fetchone()["n"]
    if pcount == 0 and target != Authority.DRAFT.value:
        raise ValueError(
            f"cannot promote {ku_id} to {target!r}: no provenance (locked DRAFT)"
        )

    # serve_blocked: True for DRAFT (no review) and for DEPRECATED.
    new_blocked = 1 if target in (Authority.DRAFT.value, Authority.DEPRECATED.value) else 0
    conn.execute(
        "UPDATE ku SET authority=?, serve_blocked=?, updated_at=? WHERE ku_id=?;",
        (target, new_blocked, _now_iso(), ku_id),
    )


# --------------------------------------------------------------------------- #
# Freshness / staleness
# --------------------------------------------------------------------------- #


def set_freshness(
    conn: sqlite3.Connection,
    ku_id: str,
    freshness,
    last_verified_at: str | None = None,
) -> None:
    """Set a KU's freshness label (and optionally bump ``last_verified_at``).

    EXPIRED freshness forces ``serve_blocked=True`` (evidence gone / broken);
    FRESH clears the stale-derived flag.
    """
    fval = _enum_value(freshness)
    sets = ["freshness=?", "updated_at=?"]
    params: list = [fval, _now_iso()]
    if last_verified_at is not None:
        sets.append("last_verified_at=?")
        params.append(last_verified_at)
    if fval == Freshness.EXPIRED.value:
        sets.append("serve_blocked=1")
    if fval == Freshness.FRESH.value:
        sets.append("derived_stale=0")
    params.append(ku_id)
    conn.execute(
        f"UPDATE ku SET {', '.join(sets)} WHERE ku_id=?;", params
    )


def mark_stale_by_file(
    conn: sqlite3.Connection, repo: str, file_path: str
) -> list[str]:
    """Mark all CODE-KUs on ``repo``/``file_path`` STALE and propagate.

    Steps (§2.1.4 batch staleness):
      1. Every CODE-KU on the file -> freshness=STALE.
      2. AUTHORITATIVE CODE-KUs on the file auto-downgrade to REVIEWED
         (authority never auto-survives a code change).
      3. ``derived_stale`` propagates to KUs linked via ``ku_edge``
         (ISSUE<->CODE, QA<->CODE) so dependent issue/QA KUs flag stale too.

    Returns the list of directly-affected CODE ku_ids.
    """
    cur = conn.execute(
        "SELECT ku_id, authority FROM ku WHERE repo=? AND file_path=? AND source_type=?;",
        (repo, file_path, SourceType.CODE.value),
    )
    affected: list[str] = []
    for row in cur.fetchall():
        kid = row["ku_id"]
        affected.append(kid)
        new_auth = row["authority"]
        if new_auth == Authority.AUTHORITATIVE.value:
            new_auth = Authority.REVIEWED.value  # auto-downgrade
        conn.execute(
            "UPDATE ku SET freshness=?, authority=?, updated_at=? WHERE ku_id=?;",
            (Freshness.STALE.value, new_auth, _now_iso(), kid),
        )

    # Propagate derived_stale along edges (both directions of ISSUE_CODE/QA_CODE).
    for kid in affected:
        neighbors = conn.execute(
            "SELECT src_ku AS other FROM ku_edge "
            "WHERE dst_ku=? AND rel IN ('ISSUE_CODE','QA_CODE') "
            "UNION "
            "SELECT dst_ku AS other FROM ku_edge "
            "WHERE src_ku=? AND rel IN ('ISSUE_CODE','QA_CODE');",
            (kid, kid),
        ).fetchall()
        for nrow in neighbors:
            conn.execute(
                "UPDATE ku SET derived_stale=1, updated_at=? WHERE ku_id=?;",
                (_now_iso(), nrow["other"]),
            )
    return affected


# --------------------------------------------------------------------------- #
# Per-KU freshness reconciliation helpers (used by code.GitReader.reindex_repo)
#
# These are thin, key-free row helpers: ``reindex_repo`` only needs the
# structural code-identity columns (repo/file_path/symbol/commit_sha/
# content_hash/authority), never the encrypted body, so they operate directly
# on ``ku`` rows and require no decryption key. They never send anything.
# --------------------------------------------------------------------------- #


def get_code_kus_for_repo(
    conn: sqlite3.Connection, repo: str
) -> list[dict]:
    """Return lightweight dict views of every CODE-KU on ``repo``.

    Each dict carries exactly the structural fields the freshness verifier
    needs (``ku_id/file_path/symbol/commit_sha/content_hash/authority``) — no
    body, no decryption. Matched by the repo *name* (the same value
    ``GitReader`` derives from ``repo_path.name``).
    """
    cur = conn.execute(
        "SELECT ku_id, repo, file_path, symbol, commit_sha, content_hash, "
        "authority, source_type FROM ku WHERE repo=? AND source_type=?;",
        (repo, SourceType.CODE.value),
    )
    return [dict(row) for row in cur.fetchall()]


def mark_expired_evidence_broken(conn: sqlite3.Connection, ku_id: str) -> None:
    """Symbol gone: freshness=EXPIRED, serve_blocked, provenance unretrievable."""
    if not ku_id:
        return
    conn.execute(
        "UPDATE ku SET freshness=?, serve_blocked=1, updated_at=? WHERE ku_id=?;",
        (Freshness.EXPIRED.value, _now_iso(), ku_id),
    )
    conn.execute(
        "UPDATE ku_provenance SET retrievable=0 WHERE ku_id=?;", (ku_id,)
    )


def downgrade_authority(
    conn: sqlite3.Connection, ku_id: str, authority
) -> None:
    """Drift downgrade (e.g. AUTHORITATIVE->REVIEWED). Direct, idempotent.

    Unlike :func:`set_authority` this is an unconditional reconciliation write
    used by the freshness loop, so it does not run the promote-direction state
    machine; it only ever *lowers* trust on detected code drift.
    """
    if not ku_id:
        return
    target = _enum_value(authority)
    new_blocked = 1 if target in (
        Authority.DRAFT.value, Authority.DEPRECATED.value
    ) else 0
    conn.execute(
        "UPDATE ku SET authority=?, serve_blocked=?, updated_at=? WHERE ku_id=?;",
        (target, new_blocked, _now_iso(), ku_id),
    )


def mark_stale(conn: sqlite3.Connection, ku_id: str) -> None:
    """Set a single KU's freshness to STALE (down-weighted, not blocked)."""
    if not ku_id:
        return
    conn.execute(
        "UPDATE ku SET freshness=?, updated_at=? WHERE ku_id=?;",
        (Freshness.STALE.value, _now_iso(), ku_id),
    )


def propagate_derived_stale(conn: sqlite3.Connection, ku_id: str) -> int:
    """Flag KUs linked to ``ku_id`` via ISSUE_CODE/QA_CODE edges as derived-stale.

    Returns the number of neighbor KUs flagged. Used so a drifting code fact
    taints the issue/QA KUs that depend on it.
    """
    if not ku_id:
        return 0
    neighbors = conn.execute(
        "SELECT src_ku AS other FROM ku_edge "
        "WHERE dst_ku=? AND rel IN ('ISSUE_CODE','QA_CODE') "
        "UNION "
        "SELECT dst_ku AS other FROM ku_edge "
        "WHERE src_ku=? AND rel IN ('ISSUE_CODE','QA_CODE');",
        (ku_id, ku_id),
    ).fetchall()
    n = 0
    for nrow in neighbors:
        conn.execute(
            "UPDATE ku SET derived_stale=1, updated_at=? WHERE ku_id=?;",
            (_now_iso(), nrow["other"]),
        )
        n += 1
    return n


def mark_fresh_bump_commit(
    conn: sqlite3.Connection, ku_id: str, head_sha: str, verified_at: str
) -> None:
    """Symbol unchanged: freshness=FRESH, bump commit_sha to HEAD, refresh ts."""
    if not ku_id:
        return
    conn.execute(
        "UPDATE ku SET freshness=?, commit_sha=?, last_verified_at=?, "
        "derived_stale=0, updated_at=? WHERE ku_id=?;",
        (Freshness.FRESH.value, head_sha, verified_at, _now_iso(), ku_id),
    )


# --------------------------------------------------------------------------- #
# Edges & provenance recheck
# --------------------------------------------------------------------------- #


def add_edge(
    conn: sqlite3.Connection, src_ku: str, dst_ku: str, rel: str
) -> None:
    """Add a relationship edge (ISSUE_CODE | QA_CODE | SUPERSEDES), idempotent."""
    conn.execute(
        "INSERT OR IGNORE INTO ku_edge(src_ku, dst_ku, rel) VALUES(?,?,?);",
        (src_ku, dst_ku, rel),
    )


def recheck_retrievable(
    conn: sqlite3.Connection, ku_id: str, ok: bool
) -> None:
    """Update provenance retrievability after a broken-link check.

    When ``ok`` is False the source pointer (commit/symbol/file) no longer
    resolves: all provenance rows are marked ``retrievable=0`` and the KU is
    pushed to EXPIRED + ``serve_blocked`` (evidence_broken). When ``ok`` is
    True the provenance is marked retrievable again (freshness untouched here;
    the freshness state machine governs FRESH/STALE).
    """
    conn.execute(
        "UPDATE ku_provenance SET retrievable=? WHERE ku_id=?;",
        (1 if ok else 0, ku_id),
    )
    if not ok:
        conn.execute(
            "UPDATE ku SET freshness=?, serve_blocked=1, updated_at=? WHERE ku_id=?;",
            (Freshness.EXPIRED.value, _now_iso(), ku_id),
        )


# --------------------------------------------------------------------------- #
# Retrieval primitives (L1 symbol, L2 FTS/inverted)
# --------------------------------------------------------------------------- #


def search_symbol(
    conn: sqlite3.Connection, repo: str | None, file_path: str | None, symbol: str
) -> list[str]:
    """L1 exact symbol lookup. Returns matching ku_ids.

    ``repo`` / ``file_path`` are optional filters; ``symbol`` is required and
    matched exactly (case-sensitive, as code symbols are).
    """
    clauses = ["symbol=?"]
    params: list = [symbol]
    if repo is not None:
        clauses.append("repo=?")
        params.append(repo)
    if file_path is not None:
        clauses.append("file_path=?")
        params.append(file_path)
    cur = conn.execute(
        f"SELECT DISTINCT ku_id FROM ku_symbol WHERE {' AND '.join(clauses)};",
        params,
    )
    return [r["ku_id"] for r in cur.fetchall()]


def fts_query(
    conn: sqlite3.Connection, bigram_query: str
) -> list[tuple[str, float]]:
    """L2 full-text query. Returns ``[(ku_id, bm25_score), ...]``.

    ``bigram_query`` must already be tokenized by
    :func:`kdl.retrieve.bigram_tokenize`. With FTS5 the raw ``bm25()`` value is
    returned (lower = more relevant; the retrieve layer negates/normalizes).
    Without FTS5 the inverted fallback returns a token-overlap count as the
    score so the caller can min-max normalize uniformly.
    """
    tokens = [t for t in bigram_query.split() if t]
    if not tokens:
        return []

    if fts5_available(conn):
        # OR the tokens; quote each to neutralize FTS5 query syntax.
        match_expr = " OR ".join(f'"{t}"' for t in tokens)
        try:
            cur = conn.execute(
                "SELECT ku_id, bm25(ku_fts) AS score FROM ku_fts "
                "WHERE ku_fts MATCH ? ORDER BY score;",
                (match_expr,),
            )
            return [(r["ku_id"], float(r["score"])) for r in cur.fetchall()]
        except sqlite3.Error:
            return []

    # Inverted fallback: count distinct matched query terms per ku_id.
    placeholders = ",".join("?" for _ in tokens)
    cur = conn.execute(
        f"SELECT ku_id, COUNT(DISTINCT term) AS hits FROM ku_inverted "
        f"WHERE term IN ({placeholders}) GROUP BY ku_id ORDER BY hits DESC;",
        tokens,
    )
    return [(r["ku_id"], float(r["hits"])) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# Status / telemetry
# --------------------------------------------------------------------------- #


def kdl_status(conn: sqlite3.Connection) -> dict:
    """Return KDL inventory counts and per-repo last-indexed commit.

    Shape::

        {
          "total": int,
          "by_source_type": {SourceType.value: count, ...},
          "by_authority": {Authority.value: count, ...},
          "by_freshness": {Freshness.value: count, ...},
          "serve_blocked": int,
          "fts5": bool,
          "last_indexed_commit": {repo: commit_sha, ...},
        }
    """

    def _group(col: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in conn.execute(
            f"SELECT {col} AS k, COUNT(*) AS n FROM ku GROUP BY {col};"
        ).fetchall():
            out[r["k"]] = r["n"]
        return out

    total = conn.execute("SELECT COUNT(*) AS n FROM ku;").fetchone()["n"]
    blocked = conn.execute(
        "SELECT COUNT(*) AS n FROM ku WHERE serve_blocked=1;"
    ).fetchone()["n"]

    last_indexed: dict[str, str] = {}
    for r in conn.execute(
        "SELECT k, v FROM kdl_meta WHERE k LIKE 'last_indexed_commit:%';"
    ).fetchall():
        repo = r["k"].split(":", 1)[1]
        last_indexed[repo] = r["v"]

    return {
        "total": total,
        "by_source_type": _group("source_type"),
        "by_authority": _group("authority"),
        "by_freshness": _group("freshness"),
        "serve_blocked": blocked,
        "fts5": fts5_available(conn),
        "last_indexed_commit": last_indexed,
    }
