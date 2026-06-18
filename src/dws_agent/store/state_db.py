"""SQLite state store at ``$DWS_AGENT_HOME/state/state.db``.

Holds durable agent state that complements the append-only audit log:
  * ``actions``        - one row per ActionIntent the system has seen, with its
                         classified level / decision / lifecycle status.
  * ``pending_confirm``- confirm_token bookkeeping (mirrors the per-action JSON
                         in state/pending/; this table gives queryable TTL /
                         one-time-use state). ``used`` enforces one-time use.
  * ``cases``          - stub for phase0 (case/task correlation later).
  * ``kv``             - generic key/value scratch (e.g. schema_version).

WAL mode is enabled for concurrent reader/writer safety. ``open_state_db``
applies the schema idempotently (``CREATE TABLE IF NOT EXISTS``) so it doubles
as a migration entry point.

This module deliberately stores ONLY metadata, never raw secrets or message
bodies. ``argv_sha`` is the normalized-argv sha256 (see confirm_token
contract); the HMAC token itself lives in the pending JSON file, not here.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS actions (
    action_id   TEXT PRIMARY KEY,
    level       TEXT,            -- R0|R1|R2|R3|NULL
    decision    TEXT,            -- AUTO|DRAFT|HUMAN_CONFIRM|DENY|NULL
    status      TEXT,            -- pending|confirmed|executed|denied|...
    created_at  TEXT NOT NULL,   -- RFC3339 UTC
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_confirm (
    action_id   TEXT PRIMARY KEY,
    argv_sha    TEXT NOT NULL,   -- sha256 of normalized argv
    issued_at   INTEGER NOT NULL,-- epoch seconds
    ttl         INTEGER NOT NULL,-- seconds
    used        INTEGER NOT NULL DEFAULT 0  -- 0/1 one-time-use flag
);

CREATE TABLE IF NOT EXISTS cases (
    case_id     TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    detail      TEXT             -- JSON blob, stub for phase0
);

CREATE TABLE IF NOT EXISTS kv (
    k           TEXT PRIMARY KEY,
    v           TEXT
);

CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
CREATE INDEX IF NOT EXISTS idx_pending_issued ON pending_confirm(issued_at);
"""

_SCHEMA_VERSION = "1"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_db_path(paths) -> Path:
    """Resolve the state.db path from a duck-typed ``paths`` object.

    Honors ``paths.state_db`` (full file path), else ``paths.state_dir``,
    else ``<paths.home>/state``.
    """
    state_db = getattr(paths, "state_db", None)
    if state_db is not None:
        return Path(state_db)
    state_dir = getattr(paths, "state_dir", None)
    if state_dir is None:
        home = getattr(paths, "home", None)
        state_dir = (Path(home) / "state") if home is not None else Path(str(paths)) / "state"
    return Path(state_dir) / "state.db"


def open_state_db(paths) -> sqlite3.Connection:
    """Open (creating if needed) the state DB, enable WAL, apply schema.

    Returns a ``sqlite3.Connection`` with ``row_factory = sqlite3.Row`` so
    helpers can return dicts. Idempotent: safe to call repeatedly.
    """
    db_path = _resolve_db_path(paths)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO kv(k, v) VALUES('schema_version', ?) "
        "ON CONFLICT(k) DO NOTHING;",
        (_SCHEMA_VERSION,),
    )
    return conn


def upsert_action(conn: sqlite3.Connection, action_id: str, level, decision, status) -> None:
    """Insert or update the lifecycle row for ``action_id``.

    ``created_at`` is set only on first insert; ``updated_at`` always refreshes.
    """
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO actions(action_id, level, decision, status, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(action_id) DO UPDATE SET
            level=excluded.level,
            decision=excluded.decision,
            status=excluded.status,
            updated_at=excluded.updated_at;
        """,
        (action_id, level, decision, status, now, now),
    )


def record_pending(
    conn: sqlite3.Connection,
    action_id: str,
    argv_sha: str,
    issued_at: int,
    ttl: int,
    used: bool = False,
) -> None:
    """Record (or replace) a pending confirm entry.

    Stores the normalized-argv sha and TTL window for queryable verification.
    Replacing an existing row re-arms the action (a fresh confirm_issued).
    """
    conn.execute(
        """
        INSERT INTO pending_confirm(action_id, argv_sha, issued_at, ttl, used)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(action_id) DO UPDATE SET
            argv_sha=excluded.argv_sha,
            issued_at=excluded.issued_at,
            ttl=excluded.ttl,
            used=excluded.used;
        """,
        (action_id, argv_sha, int(issued_at), int(ttl), 1 if used else 0),
    )


def mark_pending_used(conn: sqlite3.Connection, action_id: str) -> None:
    """Flip the one-time-use flag for a pending confirm (after verify)."""
    conn.execute(
        "UPDATE pending_confirm SET used=1 WHERE action_id=?;", (action_id,)
    )


def get_pending(conn: sqlite3.Connection, action_id: str) -> dict | None:
    """Return the pending_confirm row as a dict, or None."""
    cur = conn.execute(
        "SELECT * FROM pending_confirm WHERE action_id=?;", (action_id,)
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def get_action(conn: sqlite3.Connection, action_id: str) -> dict:
    """Return the actions row for ``action_id`` as a dict.

    Returns an empty dict if no such action is recorded (callers treat empty
    as "unknown action").
    """
    cur = conn.execute("SELECT * FROM actions WHERE action_id=?;", (action_id,))
    row = cur.fetchone()
    return dict(row) if row is not None else {}
