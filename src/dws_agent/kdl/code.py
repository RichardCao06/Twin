"""Read-only git + Python-AST code indexer for KDL CODE knowledge units.

This module walks a repository at a *pinned commit*, extracts symbols
(functions / classes / async defs, including methods, via the Python ``ast``
module; a regex fallback covers other languages), and binds every symbol to a
durable identity::

    commit_sha + file_path + symbol + line_range + content_hash

where ``content_hash = sha256(source_slice)``. From that it builds CODE
knowledge *candidates* (dicts the Ingestor turns into KUs) and the L1 symbol
index (``ku_symbol`` table).

Freshness verification (design §2.1.4) is implemented in three tiers:
  1. event / explicit reindex  -> :func:`GitReader.reindex_repo`
  2. batch stale-by-file       -> delegated to ``store.mark_stale_by_file``
  3. lazy real-time verify      -> :func:`GitReader.verify_fact` /
     :func:`GitReader.symbol_exists`

HARD CONSTRAINTS honoured here:
  * git access is **read-only**: only a whitelist of read subcommands is ever
    invoked; any write/commit/push request raises before touching git.
  * no network, no LLM, stdlib + git CLI only.
  * AUTHORITATIVE CODE-KUs never auto-survive a code change: on hash drift they
    are downgraded to REVIEWED + STALE; on symbol loss they become
    EXPIRED + evidence_broken (serve_blocked) — the retrieval side then abstains
    rather than answering from drifted code.

The module is importable and unit-testable on its own; KU/enum types are
imported from :mod:`dws_agent.kdl.model` when present, with a minimal,
contract-faithful fallback so this file can be exercised in isolation.
"""

from __future__ import annotations

import ast
import datetime as _dt
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Shared types: import from the canonical model when available. The fallbacks
# below mirror the global contract EXACTLY (same enum names/values, same field
# names) so this module behaves identically whether or not model.py has landed.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - exercised both ways depending on sibling availability
    from .model import (  # type: ignore
        Authority,
        Freshness,
        ProvKind,
        SourceType,
    )
except Exception:  # pragma: no cover
    from enum import Enum

    class _StrEnum(str, Enum):
        def __str__(self) -> str:  # so sqlite stores .value cleanly
            return str(self.value)

    class SourceType(_StrEnum):
        CODE = "CODE"
        ISSUE = "ISSUE"
        QA = "QA"
        PLAYBOOK = "PLAYBOOK"

    class Authority(_StrEnum):
        DRAFT = "DRAFT"
        REVIEWED = "REVIEWED"
        AUTHORITATIVE = "AUTHORITATIVE"
        DEPRECATED = "DEPRECATED"

    class Freshness(_StrEnum):
        FRESH = "FRESH"
        STALE = "STALE"
        EXPIRED = "EXPIRED"
        UNKNOWN = "UNKNOWN"

    class ProvKind(_StrEnum):
        COMMIT = "COMMIT"
        ISSUE_URL = "ISSUE_URL"
        MSG_ID = "MSG_ID"
        DOC_ID = "DOC_ID"
        MAIL_ID = "MAIL_ID"
        FILE = "FILE"


def _now_iso() -> str:
    """RFC3339 UTC, identical format to store.state_db._now_iso."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------

# Default file extensions we attempt to index. Anything else is skipped.
_PY_EXTS = {".py", ".pyi"}
_GENERIC_EXTS = {
    ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".rb", ".php", ".swift", ".scala",
}
_INDEXABLE_EXTS = _PY_EXTS | _GENERIC_EXTS

# Directories never walked (build artefacts / vendored deps / vcs internals).
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".tox", "target", "vendor", ".idea",
}

# Generic (non-Python) symbol heuristics: function / class / export-ish decls
# across common languages. Captures the declared name. Deliberately conservative
# — over-matching just yields extra low-signal candidates, never corruption.
_GENERIC_DECL_RE = re.compile(
    r"""^[ \t]*
    (?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*
    (?:
        (?:function|func|fn|def|class|interface|struct|trait|enum|type)\s+(?P<a>[A-Za-z_$][\w$]*)
      | (?:const|let|var)\s+(?P<b>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:function|\([^)]*\)\s*=>)
    )
    """,
    re.VERBOSE,
)

_GENERIC_KIND_RE = re.compile(
    r"\b(function|func|fn|def|class|interface|struct|trait|enum|type|const|let|var)\b"
)


@dataclass(frozen=True)
class Symbol:
    """A single extracted code symbol with its source identity.

    ``content_hash`` is the sha256 hex of ``source_slice`` and is the value the
    freshness loop compares against to detect drift.
    """

    name: str
    kind: str  # 'function' | 'async function' | 'class' | 'method' | generic kw
    line_start: int  # 1-based, inclusive
    line_end: int  # 1-based, inclusive
    source_slice: str
    content_hash: str


def content_hash(slice_: str) -> str:
    """Return ``sha256`` hex of a source slice (the CODE-KU drift fingerprint)."""
    return hashlib.sha256(slice_.encode("utf-8", "surrogatepass")).hexdigest()


def _lang_of(path_or_hint: Optional[str]) -> str:
    """Best-effort language family: 'python' or 'generic'."""
    if not path_or_hint:
        return "generic"
    hint = path_or_hint.lower()
    if hint in ("python", "py"):
        return "python"
    if hint in ("generic", "text"):
        return "generic"
    ext = Path(hint).suffix.lower()
    if ext in _PY_EXTS:
        return "python"
    return "generic"


def _slice_lines(lines: list[str], start_1: int, end_1: int) -> str:
    """Join ``lines`` (0-based list) for the 1-based inclusive [start,end]."""
    start = max(start_1 - 1, 0)
    end = min(end_1, len(lines))
    return "\n".join(lines[start:end])


def _extract_python(text: str) -> list[Symbol]:
    """Extract functions/classes/methods/async-defs via the ``ast`` module.

    Nested defs (methods, inner functions) are included. Each symbol's slice is
    the exact source span [lineno, end_lineno]. On a SyntaxError we fall back to
    the generic regex extractor so a single unparseable file never aborts a
    repo index.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return _extract_generic(text)

    lines = text.splitlines()
    syms: list[Symbol] = []
    seen: set[tuple[str, int, int]] = set()

    class _Visitor(ast.NodeVisitor):
        def _emit(self, node, name: str, kind: str) -> None:
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", None) or start
            if start is None:
                return
            key = (name, int(start), int(end))
            if key in seen:
                return
            seen.add(key)
            slice_ = _slice_lines(lines, start, end)
            syms.append(
                Symbol(
                    name=name,
                    kind=kind,
                    line_start=int(start),
                    line_end=int(end),
                    source_slice=slice_,
                    content_hash=content_hash(slice_),
                )
            )

        def _is_method(self, node) -> bool:
            parent = getattr(node, "_kdl_parent", None)
            return isinstance(parent, ast.ClassDef)

        def visit_FunctionDef(self, node):  # noqa: N802
            self._emit(node, node.name, "method" if self._is_method(node) else "function")
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node):  # noqa: N802
            self._emit(
                node,
                node.name,
                "async method" if self._is_method(node) else "async function",
            )
            self.generic_visit(node)

        def visit_ClassDef(self, node):  # noqa: N802
            self._emit(node, node.name, "class")
            self.generic_visit(node)

    # Tag parents so we can tell methods from top-level functions.
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._kdl_parent = parent  # type: ignore[attr-defined]

    _Visitor().visit(tree)
    syms.sort(key=lambda s: (s.line_start, s.line_end))
    return syms


def _block_end(lines: list[str], start_idx: int) -> int:
    """Heuristic end line (1-based) for a brace/indent block starting at start_idx.

    Used by the generic extractor. For brace languages we balance ``{`` / ``}``;
    otherwise we fall back to the declaration line itself. Always returns at
    least the start line so a slice is never empty.
    """
    line = lines[start_idx]
    if "{" not in line and "}" not in line:
        # Brace likely on a following line, or a one-liner decl; scan ahead a
        # little for an opening brace on the same logical statement.
        for j in range(start_idx, min(start_idx + 3, len(lines))):
            if "{" in lines[j]:
                start_idx = j
                line = lines[j]
                break
        else:
            return start_idx + 1  # 1-based: just the decl line

    depth = 0
    started = False
    for j in range(start_idx, len(lines)):
        for ch in lines[j]:
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
        if started and depth <= 0:
            return j + 1  # 1-based inclusive
    return len(lines)


def _extract_generic(text: str) -> list[Symbol]:
    """Regex fallback for non-Python languages: def/class/func/export decls."""
    lines = text.splitlines()
    syms: list[Symbol] = []
    seen: set[tuple[str, int]] = set()
    for i, line in enumerate(lines):
        m = _GENERIC_DECL_RE.match(line)
        if not m:
            continue
        name = m.group("a") or m.group("b")
        if not name:
            continue
        kind_m = _GENERIC_KIND_RE.search(line)
        kind = kind_m.group(1) if kind_m else "symbol"
        key = (name, i + 1)
        if key in seen:
            continue
        seen.add(key)
        end_1 = _block_end(lines, i)
        slice_ = _slice_lines(lines, i + 1, end_1)
        syms.append(
            Symbol(
                name=name,
                kind=kind,
                line_start=i + 1,
                line_end=end_1,
                source_slice=slice_,
                content_hash=content_hash(slice_),
            )
        )
    return syms


def extract_symbols(path_or_text: str, lang_hint: Optional[str] = None) -> list[Symbol]:
    """Extract symbols from a file path *or* a raw source string.

    If ``path_or_text`` names an existing readable file its contents are read;
    otherwise it is treated as source text directly. ``lang_hint`` ('python' /
    'generic' / a filename / extension) forces a parser; when omitted the
    language is inferred from the path extension (defaulting to Python-with-
    generic-fallback for unknown text).
    """
    text: str
    lang = _lang_of(lang_hint if lang_hint else path_or_text)

    p = Path(path_or_text)
    is_file = False
    try:
        is_file = len(path_or_text) < 4096 and p.is_file()
    except (OSError, ValueError):
        is_file = False

    if is_file:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        if lang_hint is None:
            lang = _lang_of(str(p))
    else:
        text = path_or_text

    if lang == "python":
        return _extract_python(text)
    return _extract_generic(text)


# ---------------------------------------------------------------------------
# Read-only git reader + repository indexing / freshness
# ---------------------------------------------------------------------------

@dataclass
class ReindexReport:
    """Outcome of :meth:`GitReader.reindex_repo`.

    Counters describe how existing CODE-KUs were reconciled against HEAD:
      * ``fresh``      : symbol present and hash unchanged (commit bumped).
      * ``stale``      : symbol present but hash drifted (downgrade + propagate).
      * ``expired``    : symbol gone (evidence_broken + serve_blocked).
      * ``downgraded`` : AUTHORITATIVE CODE-KUs auto-demoted to REVIEWED on drift.
    """

    repo: str = ""
    head_sha: str = ""
    checked: int = 0
    fresh: int = 0
    stale: int = 0
    expired: int = 0
    downgraded: int = 0
    propagated: int = 0
    ku_ids_stale: list[str] = field(default_factory=list)
    ku_ids_expired: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "repo": self.repo,
            "head_sha": self.head_sha,
            "checked": self.checked,
            "fresh": self.fresh,
            "stale": self.stale,
            "expired": self.expired,
            "downgraded": self.downgraded,
            "propagated": self.propagated,
            "ku_ids_stale": list(self.ku_ids_stale),
            "ku_ids_expired": list(self.ku_ids_expired),
        }


class GitReader:
    """A hard read-only git wrapper.

    Only the subcommands in :attr:`ALLOWED` may ever be executed. Any attempt to
    run another subcommand (notably any write: commit/push/add/reset/...) raises
    :class:`PermissionError` *before* git is invoked. This is the structural
    guarantee that KDL never mutates a repository.
    """

    #: The complete set of permitted git subcommands. Read-only by construction.
    ALLOWED = frozenset({"rev-parse", "show", "cat-file", "log", "ls-files", "blame"})

    def __init__(self, repo_path: str | os.PathLike) -> None:
        self.repo_path = Path(repo_path)

    # -- low-level guarded runner ------------------------------------------
    def _run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        """Run ``git <subcommand> ...`` after asserting the subcommand is allowed.

        Raises PermissionError if the subcommand is not in the read-only
        whitelist — the single chokepoint for the no-write guarantee.
        """
        if not args:
            raise ValueError("no git subcommand given")
        sub = args[0]
        if sub not in self.ALLOWED:
            raise PermissionError(
                f"git subcommand '{sub}' is not permitted; KDL is read-only "
                f"(allowed: {sorted(self.ALLOWED)})"
            )
        cmd = ["git", "-C", str(self.repo_path), *args]
        return subprocess.run(
            cmd, capture_output=True, text=True, check=check
        )

    # -- public read API ----------------------------------------------------
    def head_sha(self) -> str:
        """Return the full SHA of HEAD (``git rev-parse HEAD``).

        Returns an empty string if the directory is not a git repo / has no
        commits, so callers can degrade gracefully rather than crash.
        """
        try:
            out = self._run("rev-parse", "HEAD")
        except (OSError, PermissionError):
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""

    def read_at(self, commit: str, rel_path: str) -> str | None:
        """Return file contents at ``commit`` (``git show <commit>:<path>``).

        Returns None if the path does not exist at that commit. Read-only.
        """
        if not commit:
            return None
        spec = f"{commit}:{rel_path}"
        try:
            out = self._run("show", spec)
        except (OSError, PermissionError):
            return None
        if out.returncode != 0:
            return None
        return out.stdout

    def working_read(self, rel_path: str) -> str | None:
        """Return the working-tree contents of ``rel_path``, or None if missing."""
        p = self.repo_path / rel_path
        try:
            if not p.is_file():
                return None
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def list_files(self, commit: Optional[str] = None) -> list[str]:
        """List tracked, indexable files (working tree, ``git ls-files``).

        ``commit`` is accepted for symmetry; ls-files reports the index/worktree.
        Falls back to a filesystem walk if git is unavailable (fixture repos).
        """
        try:
            out = self._run("ls-files")
            if out.returncode == 0 and out.stdout.strip():
                files = [
                    f for f in out.stdout.splitlines()
                    if Path(f).suffix.lower() in _INDEXABLE_EXTS
                ]
                return files
        except (OSError, PermissionError):
            pass
        return self._walk_files()

    def _walk_files(self) -> list[str]:
        """Filesystem walk fallback (for non-git fixtures / detached dirs)."""
        files: list[str] = []
        root = self.repo_path
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                if Path(fn).suffix.lower() in _INDEXABLE_EXTS:
                    rel = os.path.relpath(os.path.join(dirpath, fn), root)
                    files.append(rel)
        files.sort()
        return files

    # -- candidate building -------------------------------------------------
    def index_repo(self, repo_path: Optional[str | os.PathLike] = None) -> list[dict]:
        """Walk the repo at HEAD and emit CODE knowledge *candidates*.

        Each candidate is a plain dict (the Distiller/Ingestor contract): it
        carries the symbol identity, a body draft describing the symbol, and
        **at least one** provenance entry of kind=COMMIT ref=<head_sha>. The
        Ingestor is responsible for redaction, taint and encryption — this layer
        produces only structured, plaintext-in-memory candidates.

        Returns an empty list for a non-repo / empty repo (never raises for the
        common "not a git directory" case).
        """
        if repo_path is not None:
            self.repo_path = Path(repo_path)
        repo = self.repo_path.name
        head = self.head_sha()
        candidates: list[dict] = []
        for rel in self.list_files():
            # Prefer the pinned-commit content so the slice matches commit_sha;
            # fall back to the working tree for non-git fixtures.
            text = self.read_at(head, rel) if head else None
            if text is None:
                text = self.working_read(rel)
            if text is None:
                continue
            # Pass source text (not the path) so extraction is independent of
            # the process cwd and always matches the pinned-commit content.
            for sym in extract_symbols(text, lang_hint=str(rel)):
                candidates.append(
                    self._candidate(repo, head, rel, sym)
                )
        return candidates

    def _candidate(self, repo: str, head: str, rel: str, sym: Symbol) -> dict:
        """Build one CODE candidate dict for ``sym`` in ``rel`` at ``head``."""
        now = _now_iso()
        title = f"{rel}::{sym.name} ({sym.kind})"
        body = (
            f"{sym.kind} `{sym.name}` defined in {rel} "
            f"(lines {sym.line_start}-{sym.line_end}) @ {head[:12] or 'WORKTREE'}.\n\n"
            f"{sym.source_slice}"
        )
        return {
            "source_type": SourceType.CODE.value,
            "title": title,
            "body": body,
            "repo": repo,
            "commit_sha": head,
            "file_path": rel,
            "symbol": sym.name,
            "line_start": sym.line_start,
            "line_end": sym.line_end,
            "line_range": (sym.line_start, sym.line_end),
            "content_hash": sym.content_hash,
            "freshness": Freshness.FRESH.value if head else Freshness.UNKNOWN.value,
            # Provenance is MANDATORY. Always >= 1 COMMIT entry pointing at the
            # pinned commit; without it the Ingestor must drop / lock DRAFT.
            "provenance": [
                {
                    "kind": ProvKind.COMMIT.value,
                    "ref": head or "WORKTREE",
                    "quote": sym.source_slice,
                    "captured_at": now,
                    "retrievable": True,
                }
            ],
        }

    # -- freshness: lazy real-time verify ----------------------------------
    def _reextract_symbol(self, ku) -> Optional[Symbol]:
        """Re-extract the KU's symbol from its pinned commit (or working tree).

        Returns the matching :class:`Symbol` or None if the symbol is gone.
        Tries the pinned commit first; if that content is unavailable (e.g. a
        shallow / fixture repo) it falls back to the working tree.
        """
        file_path = _attr(ku, "file_path")
        symbol = _attr(ku, "symbol")
        commit = _attr(ku, "commit_sha")
        if not file_path or not symbol:
            return None
        text = self.read_at(commit, file_path) if commit else None
        if text is None:
            text = self.working_read(file_path)
        if text is None:
            return None
        for sym in extract_symbols(text, lang_hint=str(file_path)):
            if sym.name == symbol:
                return sym
        return None

    def symbol_exists(self, reader_or_ku, ku=None) -> bool:
        """True iff the KU's symbol still exists at its pinned commit/worktree.

        Accepts either ``symbol_exists(reader, ku)`` or, when called as a bound
        method, ``reader.symbol_exists(ku)``.
        """
        target = ku if ku is not None else reader_or_ku
        reader = self if ku is None else reader_or_ku
        if not isinstance(reader, GitReader):
            reader = self
        return reader._reextract_symbol(target) is not None

    def verify_fact(self, reader_or_ku, ku=None) -> Freshness:
        """Lazy, real-time freshness check for a single CODE-KU (§2.1.4 tier 3).

        Re-extracts the symbol at the KU's pinned commit (working-tree fallback),
        re-hashes its source slice and compares to the stored ``content_hash``:

          * symbol missing            -> Freshness.EXPIRED  (candidate dropped)
          * hash differs              -> Freshness.STALE    (down-weighted)
          * hash identical            -> Freshness.FRESH

        Non-CODE KUs and KUs lacking identity return Freshness.UNKNOWN. Any
        exception degrades to STALE (never FRESH) — verification failure must
        never be read as "fresh". No caching (high-frequency domain, §D1).
        """
        target = ku if ku is not None else reader_or_ku
        reader = self if ku is None else reader_or_ku
        if not isinstance(reader, GitReader):
            reader = self

        if _enum_value(_attr(target, "source_type")) != SourceType.CODE.value:
            return Freshness.UNKNOWN
        try:
            sym = reader._reextract_symbol(target)
        except (OSError, PermissionError):
            return Freshness.STALE
        if sym is None:
            return Freshness.EXPIRED
        stored_hash = _attr(target, "content_hash")
        if stored_hash and sym.content_hash != stored_hash:
            return Freshness.STALE
        return Freshness.FRESH

    # -- freshness: event / explicit reindex --------------------------------
    def reindex_repo(self, conn, key: bytes, repo_path: Optional[str | os.PathLike] = None) -> ReindexReport:
        """Re-verify every CODE-KU of this repo against HEAD and reconcile.

        For each existing CODE-KU on this repo:
          * symbol gone   -> freshness=EXPIRED, serve_blocked, evidence_broken,
                             provenance.retrievable=False.
          * hash drift    -> freshness=STALE; AUTHORITATIVE auto-downgraded to
                             REVIEWED; derived_stale propagated along ku_edge
                             (ISSUE<->CODE / QA<->CODE) via store helpers.
          * identical     -> freshness=FRESH; commit_sha bumped to HEAD;
                             last_verified_at refreshed.

        Persistence is delegated to :mod:`dws_agent.kdl.store` helpers (imported
        lazily so this module stays importable before store.py lands). The
        function is a no-op-safe reconciliation: it only ever READS git and
        WRITES KDL rows — it never writes the repository.
        """
        if repo_path is not None:
            self.repo_path = Path(repo_path)
        repo = self.repo_path.name
        head = self.head_sha()
        report = ReindexReport(repo=repo, head_sha=head)

        store = _load_store()
        if store is None or conn is None:
            # Without the store we cannot reconcile persisted KUs; report the
            # HEAD we observed so callers/tests can still assert read behaviour.
            return report

        kus = store.get_code_kus_for_repo(conn, repo)  # type: ignore[attr-defined]
        for ku in kus:
            report.checked += 1
            # Reindex is the explicit "re-verify against current HEAD" pass:
            # re-extract the symbol AT HEAD (not at the KU's stored pinned
            # commit, which by definition still matches its own hash) and
            # compare to the stored content_hash. We verify against a HEAD-
            # pinned *view* of the KU so the pinned commit_sha is only bumped
            # by mark_fresh_bump_commit on a confirmed FRESH result.
            if isinstance(ku, dict):
                ku_at_head = dict(ku)
                ku_at_head["commit_sha"] = head
            else:
                ku_at_head = ku
            fresh = self.verify_fact(self, ku_at_head)
            ku_id = _attr(ku, "ku_id")
            file_path = _attr(ku, "file_path")
            if fresh == Freshness.EXPIRED:
                report.expired += 1
                if ku_id:
                    report.ku_ids_expired.append(ku_id)
                store.mark_expired_evidence_broken(conn, ku_id)  # type: ignore[attr-defined]
            elif fresh == Freshness.STALE:
                report.stale += 1
                if ku_id:
                    report.ku_ids_stale.append(ku_id)
                authority = _enum_value(_attr(ku, "authority"))
                if authority == Authority.AUTHORITATIVE.value:
                    report.downgraded += 1
                    store.downgrade_authority(  # type: ignore[attr-defined]
                        conn, ku_id, Authority.REVIEWED.value
                    )
                store.mark_stale(conn, ku_id)  # type: ignore[attr-defined]
                if file_path:
                    report.propagated += store.propagate_derived_stale(  # type: ignore[attr-defined]
                        conn, ku_id
                    ) or 0
            else:  # FRESH
                report.fresh += 1
                store.mark_fresh_bump_commit(  # type: ignore[attr-defined]
                    conn, ku_id, head, _now_iso()
                )
        return report


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _enum_value(v) -> str:
    """Return the ``.value`` of a str-Enum (or ``str(v)``); '' for None.

    Robust against the canonical model enums (``class X(str, Enum)`` whose
    ``str()`` yields the member repr, not the value) AND plain strings used in
    isolated unit tests.
    """
    if v is None:
        return ""
    return getattr(v, "value", v) if not isinstance(v, str) else v


def _attr(obj, name: str):
    """Read ``name`` from a dataclass/object or a dict candidate, else None."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _load_store():
    """Lazily import the KDL store module; None if not yet available.

    Keeps :mod:`code` importable and unit-testable before its sibling
    ``store.py`` exists, while using the real store at runtime.
    """
    try:
        from . import store as _store  # type: ignore
        return _store
    except Exception:
        return None
