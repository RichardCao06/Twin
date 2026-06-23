"""dws-agent CLI entrypoint.

Subcommands:
- ``init``    : scaffold $DWS_AGENT_HOME (dirs + encrypted subdirs + keys +
                default policy.yaml copied from the packaged default).
- ``confirm`` : in-band (带内) local confirm flow. Given an ``--action-id`` and
                the full ``--argv`` token list, re-classify the command, and if
                it requires HUMAN_CONFIRM, issue a confirm_token and mark the
                pending record verified (one-time). Refuses ``never`` commands.
- ``status``  : show home health, refresh-lock state, recent audit tail and the
                pending-confirm count.

Hard constraints surfaced here:
- default-deny: unknown subcommands classify >= R2 and require confirm.
- judging NEVER looks at ``--yes``/``-y`` (normalize_argv strips them).
- ``auth export|import|logout|reset`` are in the ``never`` list and are refused
  by ``confirm`` (not confirmable, terminal DENY).
- execution tokens are never placed on PATH; ``confirm`` only issues/verifies
  the in-band confirm_token, it does NOT execute dws.

All sibling-module imports are lazy (inside functions) so this module imports
cleanly even before the sibling modules are written, and so unit tests can
monkeypatch the runtime root via ``DWS_AGENT_HOME``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List, Optional


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _load_paths():
    """Return the core.paths Paths object rooted at $DWS_AGENT_HOME.

    Lazily imports core.config/core.paths. Falls back to a minimal local
    Paths shim if core is not yet available, so the CLI remains importable
    and partially usable during incremental development.
    """
    try:
        from dws_agent.core import paths as core_paths  # type: ignore

        return core_paths.get_paths()
    except Exception:
        return _FallbackPaths()


class _FallbackPaths:
    """Minimal stand-in for core.paths when core is unavailable.

    Derives every subpath from DWS_AGENT_HOME exactly like the contract
    describes. Used only as a degradation path; core.paths is authoritative.
    """

    def __init__(self) -> None:
        from pathlib import Path

        default = os.path.expanduser("~/.claude/dws-agent")
        self.home = Path(os.environ.get("DWS_AGENT_HOME", default))

    @property
    def audit_dir(self):
        return self.home / "audit"

    @property
    def state_dir(self):
        return self.home / "state"

    @property
    def pending_dir(self):
        return self.state_dir / "pending"

    @property
    def locks_dir(self):
        # Authoritative layout (core.paths) places locks at the home root, not
        # under state/. Keep the fallback in lockstep so the refresh-guard and
        # dwsd instance lock resolve to the same file regardless of code path.
        return self.home / "locks"

    @property
    def snapshots_dir(self):
        return self.home / "snapshots"

    @property
    def policy_dir(self):
        return self.home / "policy"

    @property
    def policy_file(self):
        return self.policy_dir / "policy.yaml"

    @property
    def memory_dir(self):
        return self.home / "memory"

    @property
    def kb_dir(self):
        return self.home / "kb"

    @property
    def keys_dir(self):
        return self.home / "keys"

    @property
    def logs_dir(self):
        return self.home / "logs"


def _dws_bin():
    """Return the configured dws binary path string (for argv[0] normalization).

    Reads core.config; falls back to the documented default. Used only to let
    normalize_argv recognize argv[0]; never used to execute anything here.
    """
    try:
        from dws_agent.core import config as core_config  # type: ignore

        return str(core_config.load_config().dws_bin)
    except Exception:
        return os.environ.get("DWS_AGENT_DWS_BIN", "/opt/homebrew/bin/dws")


def _audit(paths, **record):
    """Best-effort audit write via store.audit.AuditLogger.

    Never raises: auditing failures must not crash a CLI command, but we try
    hard to record. AuditLogger injects ts/seq/pid per the contract.
    """
    try:
        from dws_agent.store.audit import AuditLogger  # type: ignore

        AuditLogger(paths).log(record)
    except Exception:
        # Degrade silently; the CLI prints its own user-facing output.
        pass


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #
def cmd_init(args) -> int:
    """Scaffold the runtime home and seed the default policy.

    Delegates to ``core.scaffold.scaffold_home`` (single root with private
    perms + sensitive subdirs + packaged policy.yaml seed). Idempotent:
    re-running does not destroy state. ``--force`` re-seeds policy.yaml from
    the packaged default.
    """
    paths = _load_paths()
    force = bool(getattr(args, "force", False))
    report = None
    try:
        from dws_agent.core import scaffold  # type: ignore

        report = scaffold.scaffold_home(paths, force=force)
    except Exception as exc:
        # Fallback minimal scaffold: directory skeleton only. Encryption/keys
        # and policy seeding are owned by core.scaffold; this is a degraded path.
        for d in (
            paths.audit_dir,
            paths.pending_dir,
            paths.locks_dir,
            paths.policy_dir,
            paths.memory_dir,
            paths.kb_dir,
            paths.keys_dir,
            paths.logs_dir,
        ):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        _install_default_policy(paths)
        _audit(
            paths,
            event="scaffold",
            actor="cli",
            action_id=None,
            decision=None,
            level=None,
            reason="fallback scaffold (core.scaffold unavailable: %s)" % exc,
            detail={"home": str(paths.home)},
        )
    else:
        _audit(
            paths,
            event="scaffold",
            actor="cli",
            action_id=None,
            decision=None,
            level=None,
            reason="init complete",
            detail={"home": str(paths.home), "policy": report.get("policy")},
        )

    print("dws-agent home initialized at: %s" % paths.home)
    print("  policy:  %s" % paths.policy_file)
    print("  audit:   %s" % paths.audit_dir)
    print("  state:   %s" % paths.state_dir)
    if report:
        print("  created: %d dir(s), policy: %s"
              % (len(report.get("created", [])), report.get("policy")))
    return 0


def _install_default_policy(paths) -> None:
    """Copy the packaged default policy.yaml to $DWS_AGENT_HOME/policy if absent.

    Degraded fallback only (used when core.scaffold is unavailable). An existing
    runtime policy.yaml is never overwritten.
    """
    from pathlib import Path

    dst = Path(paths.policy_file)
    if dst.exists():
        return
    try:
        import importlib.resources as ir

        src_text = (
            ir.files("dws_agent.policy").joinpath("policy.yaml").read_text("utf-8")
        )
    except Exception:
        src = Path(__file__).resolve().parents[1] / "policy" / "policy.yaml"
        if not src.exists():
            return
        src_text = src.read_text("utf-8")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src_text, "utf-8")


# --------------------------------------------------------------------------- #
# confirm
# --------------------------------------------------------------------------- #
def cmd_confirm(args) -> int:
    """In-band confirm: classify, then issue + verify a confirm_token.

    Flow (deterministic, no LLM):
      1. Build normalized argv from ``--argv`` (the FULL dws command line,
         argv[0] must be ``dws``). normalize_argv strips ``--yes``/``-y``.
      2. Classify via policy.classifier. ``never`` => terminal DENY (refused).
         R0/AUTO => nothing to confirm. R1/R2/R3 => HUMAN_CONFIRM.
      3. For HUMAN_CONFIRM: issue a confirm_token (HMAC over
         action_id|argv_norm_sha256|issued_at|ttl) into
         $DWS_AGENT_HOME/state/pending/<action_id>.json and immediately verify
         it (this CLI is the human-in-the-loop), marking the record used.

    The confirm_token binds the normalized argv hash; presenting different argv
    later will fail VERIFY. This never executes dws.
    """
    paths = _load_paths()
    action_id: str = args.action_id
    raw_argv: List[str] = list(args.argv or [])

    # Hard reject: missing argv or argv[0] != 'dws' (R2-class deny per contract).
    if not raw_argv or raw_argv[0] != "dws":
        _audit(
            paths,
            event="confirm_rejected",
            actor="cli",
            action_id=action_id,
            level="R2",
            decision="DENY",
            reason="missing argv or argv[0] != 'dws'",
            detail={"argv_present": bool(raw_argv)},
        )
        print("DENY: argv must be a full dws command line with argv[0]=='dws'",
              file=sys.stderr)
        return 2

    # Classify. policy modules are authoritative.
    try:
        from dws_agent.policy import classifier as clf  # type: ignore
        from dws_agent.policy import confirm as confirm_mod  # type: ignore
        from dws_agent.policy.loader import load_policy  # type: ignore
    except Exception as exc:
        print("ERROR: policy module unavailable: %s" % exc, file=sys.stderr)
        return 3

    # Resolve the dws binary path (for argv[0] normalization) from config.
    dws_bin = _dws_bin()

    policy = load_policy(paths)
    result = clf.classify(raw_argv, policy, dws_bin=dws_bin)
    # Classification carries .level / .decision / .never / .reason.
    level = _get(result, "level")
    decision = _get(result, "decision")
    is_never = bool(_get(result, "never", default=False))
    # Compute the normalized-argv hash for audit (same algorithm policy uses).
    normalized = clf.normalize_argv(raw_argv, dws_bin)
    argv_sha = confirm_mod.argv_norm_sha256(normalized)

    if is_never or decision == "DENY":
        _audit(
            paths,
            event="confirm_rejected",
            actor="cli",
            action_id=action_id,
            argv_norm_sha256=argv_sha,
            level=level,
            decision="DENY",
            reason="command is in never-list / terminal DENY; not confirmable",
            detail={},
        )
        print("DENY: this command is never permitted (auth export/import/"
              "logout/reset or terminal deny). Not confirmable.",
              file=sys.stderr)
        return 4

    if decision == "AUTO" or level == "R0":
        print("R0/AUTO: no confirmation required for this command.")
        return 0

    # HUMAN_CONFIRM path (R1/R2/R3). Issue then verify.
    ttl = int(_get(policy, "confirm_ttl_seconds", default=300) or 300)
    try:
        issued = confirm_mod.issue_token(action_id, normalized, ttl, paths)
    except Exception as exc:
        _audit(
            paths,
            event="confirm_rejected",
            actor="cli",
            action_id=action_id,
            argv_norm_sha256=argv_sha,
            level=level,
            decision="DENY",
            reason="issue_token failed: %s" % exc,
            detail={},
        )
        print("ERROR: could not issue confirm_token: %s" % exc, file=sys.stderr)
        return 5

    _audit(
        paths,
        event="confirm_issued",
        actor="cli",
        action_id=action_id,
        argv_norm_sha256=_get(issued, "argv_norm_sha256", default=argv_sha),
        level=level,
        decision="HUMAN_CONFIRM",
        reason="confirm_token issued (TTL %ss)"
        % _get(issued, "ttl_seconds", default=ttl),
        detail={},
    )

    vr = confirm_mod.verify_token(action_id, raw_argv, paths, dws_bin=dws_bin)
    verified = bool(_get(vr, "ok", default=False))
    if not verified:
        _audit(
            paths,
            event="confirm_rejected",
            actor="cli",
            action_id=action_id,
            argv_norm_sha256=argv_sha,
            level=level,
            decision="DENY",
            reason="verify failed: %s" % _get(vr, "reason", default="?"),
            detail={},
        )
        print("DENY: confirm_token verification failed: %s"
              % _get(vr, "reason", default="?"), file=sys.stderr)
        return 6

    _audit(
        paths,
        event="confirm_verified",
        actor="cli",
        action_id=action_id,
        argv_norm_sha256=argv_sha,
        level=level,
        decision="HUMAN_CONFIRM",
        reason="human confirmed via cli (in-band)",
        detail={},
    )
    token = _get(issued, "token", default=None)
    print("CONFIRMED action_id=%s level=%s" % (action_id, level))
    if token:
        print("confirm_token=%s" % token)
    return 0


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def cmd_status(args) -> int:
    """Print home health, refresh-lock state, pending count and recent audit."""
    paths = _load_paths()
    from pathlib import Path

    home = Path(paths.home)
    print("DWS_AGENT_HOME: %s (%s)"
          % (home, "exists" if home.exists() else "MISSING"))

    # Component presence
    for label, p in (
        ("policy", paths.policy_file),
        ("audit", paths.audit_dir),
        ("pending", paths.pending_dir),
        ("locks", paths.locks_dir),
        ("keys", paths.keys_dir),
    ):
        pp = Path(p)
        print("  %-8s %s [%s]" % (label + ":", pp, "ok" if pp.exists() else "missing"))

    # refresh-guard lock state
    lock_state = _refresh_lock_state(paths)
    print("refresh-guard lock: %s" % lock_state)

    # pending confirm count
    pending = _count_pending(paths)
    print("pending confirms: %d" % pending)

    # recent audit tail
    n = getattr(args, "audit_lines", 5) or 5
    print("recent audit (last %d):" % n)
    for line in _tail_audit(paths, n):
        print("  " + line)
    return 0


def _refresh_lock_state(paths) -> str:
    """Report whether the refresh-guard token lock is currently held."""
    try:
        from dws_agent.executor import refresh_guard as rg  # type: ignore

        hc = rg.healthcheck(paths)
        if hc.get("held"):
            return "HELD (pid=%s, purpose=%s)" % (
                hc.get("holder_pid"), hc.get("purpose"))
        if hc.get("stale"):
            return "free (stale holder record present)"
        return "free"
    except Exception:
        from pathlib import Path

        lock = Path(paths.locks_dir) / "refresh.lock"
        return "HELD(?)" if lock.exists() else "free"


def _count_pending(paths) -> int:
    from pathlib import Path

    d = Path(paths.pending_dir)
    if not d.exists():
        return 0
    return sum(1 for _ in d.glob("*.json"))


def _tail_audit(paths, n: int) -> List[str]:
    """Return the last ``n`` audit lines from today's audit file (best effort)."""
    from pathlib import Path
    import datetime

    day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    f = Path(paths.audit_dir) / ("audit-%s.jsonl" % day)
    if not f.exists():
        return ["(no audit for today)"]
    try:
        lines = f.read_text("utf-8").splitlines()
    except Exception:
        return ["(audit unreadable)"]
    return lines[-n:] if lines else ["(empty)"]


# --------------------------------------------------------------------------- #
# small accessor that works for dicts and objects
# --------------------------------------------------------------------------- #
def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# --------------------------------------------------------------------------- #
# argparse wiring
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dws-agent",
        description="dws-agent control CLI (init / confirm / status / kb / triage / send).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="scaffold home + keys + default policy")
    p_init.add_argument(
        "--force",
        action="store_true",
        help="re-run scaffold steps even if home exists (never deletes state)",
    )
    p_init.set_defaults(func=cmd_init)

    p_conf = sub.add_parser(
        "confirm",
        help="verify/issue a confirm_token for a pending action (in-band)",
    )
    p_conf.add_argument("--action-id", required=True, dest="action_id")
    p_conf.add_argument(
        "--argv",
        nargs=argparse.REMAINDER,
        required=True,
        help="FULL dws command line as tokens, e.g. --argv dws im send ...",
    )
    p_conf.set_defaults(func=cmd_confirm)

    p_stat = sub.add_parser("status", help="show home health, locks, audit tail")
    p_stat.add_argument(
        "--audit-lines",
        type=int,
        default=5,
        dest="audit_lines",
        help="number of recent audit lines to show",
    )
    p_stat.set_defaults(func=cmd_status)

    # KDL knowledge-base command group (phase1). Lazy + non-fatal: if the kdl
    # package is absent/broken, the rest of the CLI must still work.
    try:
        from dws_agent.kdl.cli import register_kb  # type: ignore

        register_kb(sub)
    except Exception:
        pass

    # MVP workflow command group (triage/send). Lazy + non-fatal too.
    try:
        from dws_agent.mvp.cli import register_mvp  # type: ignore

        register_mvp(sub)
    except Exception:
        pass

    return p


def main(argv: Optional[List[str]] = None) -> int:
    """Entrypoint for the ``dws-agent`` console script.

    Returns a process exit code. ``argv`` defaults to ``sys.argv[1:]``.
    """
    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    rc = args.func(args)
    # Best-effort cli audit of the invocation itself.
    try:
        _audit(
            _load_paths(),
            event="cli",
            actor="cli",
            action_id=getattr(args, "action_id", None),
            decision=None,
            level=None,
            reason="cli %s rc=%s" % (args.command, rc),
            detail={"command": args.command},
        )
    except Exception:
        pass
    return int(rc)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
