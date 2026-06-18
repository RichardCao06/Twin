"""store subpackage: append-only audit log, SQLite state DB, undo snapshots.

Re-exports the most commonly used entry points so callers can do
``from dws_agent.store import AuditLogger, open_state_db``.
"""

from .audit import AuditLogger, get_audit_logger
from .state_db import open_state_db

__all__ = ["AuditLogger", "get_audit_logger", "open_state_db"]
