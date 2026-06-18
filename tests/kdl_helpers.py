"""Shared helpers + tiny fixtures for the phase-1 KDL test suite.

Everything here is hermetic: temp dirs only, ``DWS_AGENT_TEST_MODE=1`` (so
``core.crypto`` uses its deterministic fallback key — no Keychain, no network),
and a *local* git repo created with ``git init`` in a tmp dir (no clone, no
remote, no network). No real ``dws`` binary is ever invoked by these helpers,
and the only git subcommands used to build fixtures are local writes against a
throwaway repo — the KDL code under test only ever READS git.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from dws_agent.core import paths as core_paths
from dws_agent.core import scaffold
from dws_agent.core.crypto import get_keychain_secret
from dws_agent.kdl.store import ensure_kdl_schema
from dws_agent.store.state_db import open_state_db


def open_kdl(paths):
    """Return ``(conn, key)`` for a scaffolded home with KDL schema applied."""
    conn = open_state_db(paths)
    ensure_kdl_schema(conn)
    key = get_keychain_secret("fileenc")
    return conn, key


def make_paths(tmp_home: Path, monkeypatch):
    """Scaffold an isolated $DWS_AGENT_HOME under *tmp_home*; return Paths."""
    monkeypatch.setenv("DWS_AGENT_HOME", str(tmp_home))
    monkeypatch.setenv("DWS_AGENT_TEST_MODE", "1")
    paths = core_paths.get_paths()
    scaffold.scaffold_home(paths, force=True)
    return paths


# --------------------------------------------------------------------------- #
# Local throwaway git repo fixture builder (no network, no clone).
# --------------------------------------------------------------------------- #
def git(repo: Path, *args: str) -> str:
    """Run a git command inside *repo* (used only to BUILD fixtures)."""
    env_args = [
        "git",
        "-C",
        str(repo),
        "-c",
        "user.email=t@t.t",
        "-c",
        "user.name=t",
        "-c",
        "commit.gpgsign=false",
        *args,
    ]
    out = subprocess.run(env_args, capture_output=True, text=True)
    if out.returncode != 0:  # pragma: no cover - fixture failure surfaces loudly
        raise RuntimeError(f"git {args} failed: {out.stderr}")
    return out.stdout.strip()


def init_repo(repo: Path, files: dict[str, str]) -> str:
    """Create a git repo at *repo* with *files* and one commit; return HEAD SHA."""
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    write_files(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return git(repo, "rev-parse", "HEAD")


def write_files(repo: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def commit_all(repo: Path, msg: str = "update") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


# A small, deterministic source file used across CODE tests.
SAMPLE_PY_V1 = (
    "def login(user, password):\n"
    "    # validates a user against the directory\n"
    "    return user == 'admin' and password == 'secret'\n"
    "\n"
    "\n"
    "def logout(session):\n"
    "    return None\n"
)

# v2: body of login() changes (drift) but the symbol still exists.
SAMPLE_PY_V2_DRIFT = (
    "def login(user, password):\n"
    "    # now validates against an LDAP backend\n"
    "    return ldap_check(user, password)\n"
    "\n"
    "\n"
    "def logout(session):\n"
    "    return None\n"
)

# v3: login() symbol removed entirely (deleted) -> EXPIRED on verify.
SAMPLE_PY_V3_REMOVED = (
    "def logout(session):\n"
    "    return None\n"
)
