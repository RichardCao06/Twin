"""dwsd daemon skeleton (phase0, no LLM).

The daemon runs a simple blocking poll loop:

    with single-instance lock held:
        while not stopping:
            tick()        # Executor.run_once() drains the inbox once
            sleep(interval)

Phase-0 scope:
- NO LLM anywhere in this process; it only drives the deterministic Executor,
  which consumes ``ActionIntent`` JSON from the inbox and classifies/gates each
  intent itself. dwsd never interprets argv.
- single-instance enforcement via the refresh-guard file lock
  (``executor.refresh_guard.refresh_lock``), so two dwsd instances cannot poll /
  execute concurrently.
- graceful shutdown on SIGTERM/SIGINT: finish the current tick, release the
  instance lock, exit 0.

Tests can call ``Daemon.tick`` directly, or ``main(['--once'])`` for a single
drain, without starting the blocking loop.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from typing import List, Optional


def _load_paths():
    try:
        from dws_agent.core import paths as core_paths  # type: ignore

        return core_paths.get_paths()
    except Exception:
        from dws_agent.cli.main import _FallbackPaths  # type: ignore

        return _FallbackPaths()


def _audit(paths, **record):
    try:
        from dws_agent.store.audit import AuditLogger  # type: ignore

        AuditLogger(paths).log(record)
    except Exception:
        pass


class Daemon:
    """dwsd poll-loop daemon.

    Parameters
    ----------
    paths:
        core.paths Paths object (or fallback) describing the runtime root.
    interval:
        seconds between ticks (default 5).
    """

    LOCK_PURPOSE = "dwsd-instance"

    def __init__(self, paths, interval: int = 5) -> None:
        self.paths = paths
        self.interval = max(1, int(interval))
        self._stopping = False
        self._executor = None

    # -- lifecycle --------------------------------------------------------- #
    def _install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            # Request graceful shutdown; the loop checks _stopping each cycle.
            self._stopping = True

        try:
            signal.signal(signal.SIGTERM, _handler)
            signal.signal(signal.SIGINT, _handler)
        except ValueError:
            # Not on the main thread (e.g. under a test runner); skip silently.
            pass

    # -- work -------------------------------------------------------------- #
    def _get_executor(self):
        if self._executor is None:
            from dws_agent.executor.executor import Executor  # type: ignore

            self._executor = Executor(self.paths)
        return self._executor

    def tick(self) -> None:
        """One poll cycle: drain the inbox once via the deterministic Executor.

        ``Executor.run_once()`` itself polls the inbox, classifies/gates each
        ``ActionIntent`` and executes only AUTO (R0) intents (others are held as
        DRAFT, since an unattended drain has no confirm_token). dwsd never
        interprets argv; it only invokes the executor.
        """
        executor = self._get_executor()
        try:
            executor.run_once()
        except Exception as exc:  # never let one tick kill the loop
            _audit(
                self.paths,
                event="exec_result",
                actor="dwsd",
                action_id=None,
                decision="DENY",
                level=None,
                reason="run_once raised: %s" % exc,
                detail={},
            )

    def run(self) -> None:
        """Blocking poll loop, guarded by the single-instance refresh lock.

        Refuses to start (exits non-zero) if another instance already holds the
        lock. Returns only after graceful shutdown.
        """
        self._install_signal_handlers()

        try:
            from dws_agent.executor import refresh_guard as rg  # type: ignore
            from dws_agent.store.audit import AuditLogger  # type: ignore

            audit = None
            try:
                audit = AuditLogger(self.paths)
            except Exception:
                audit = None
            lock_cm = rg.refresh_lock(
                self.paths, timeout=0.5, purpose=self.LOCK_PURPOSE, audit=audit
            )
        except Exception:
            # refresh_guard unavailable: run without single-instance protection
            # (degraded; acceptable only outside production).
            self._run_loop()
            return

        try:
            with lock_cm:
                _audit(
                    self.paths,
                    event="cli",
                    actor="dwsd",
                    action_id=None,
                    decision=None,
                    level=None,
                    reason="dwsd started (interval=%ss)" % self.interval,
                    detail={},
                )
                self._run_loop()
        except TimeoutError:
            _audit(
                self.paths,
                event="kill_switch",
                actor="dwsd",
                action_id=None,
                decision="DENY",
                level=None,
                reason="another dwsd instance is already running",
                detail={},
            )
            raise SystemExit("dwsd: another instance is already running")
        finally:
            _audit(
                self.paths,
                event="cli",
                actor="dwsd",
                action_id=None,
                decision=None,
                level=None,
                reason="dwsd stopped (graceful)",
                detail={},
            )

    def _run_loop(self) -> None:
        while not self._stopping:
            self.tick()
            # Sleep in short slices so SIGTERM is honored promptly.
            slept = 0.0
            while slept < self.interval and not self._stopping:
                time.sleep(min(0.5, self.interval - slept))
                slept += 0.5


def main(argv: Optional[List[str]] = None) -> int:
    """Entrypoint for the ``dwsd`` console script."""
    parser = argparse.ArgumentParser(
        prog="dwsd", description="dws-agent daemon (poll loop, no LLM)."
    )
    parser.add_argument(
        "--interval", type=int, default=5, help="seconds between poll ticks"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single tick and exit (for testing/cron)",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    paths = _load_paths()
    daemon = Daemon(paths, interval=args.interval)
    if args.once:
        daemon.tick()
        return 0
    daemon.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
