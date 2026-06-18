"""Scenario 7: refresh-guard inter-process file lock serialization.

Two concurrent refreshers must NOT be in the critical section at the same time.
We assert this two ways:

1. In-process concurrency: many threads each take refresh_lock() and bump a
   shared counter inside the critical section; a non-atomic check detects any
   overlap. Max concurrent holders must be exactly 1.

2. Cross-process: two child processes each try to grab the lock with a short
   timeout while a parent-held lock is active; exactly one of two contenders
   wins when the holder is alive, proving fcntl.flock serialization across
   processes (not just threads).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

from dws_agent.executor import refresh_guard


def test_threads_are_serialized_max_one_in_critical_section(home):
    in_section = 0
    max_seen = 0
    overlaps = 0
    lock = threading.Lock()

    def worker():
        nonlocal in_section, max_seen, overlaps
        with refresh_guard.refresh_lock(home, timeout=10, purpose="t"):
            with lock:
                in_section += 1
                if in_section > 1:
                    overlaps += 1
                max_seen = max(max_seen, in_section)
            time.sleep(0.02)  # widen the window to expose any overlap
            with lock:
                in_section -= 1

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlaps == 0
    assert max_seen == 1


# Child program: try to acquire the refresh lock with a short timeout, print
# WON or LOST. Reads DWS_AGENT_HOME from env.
_CHILD = r"""
import os, sys
sys.path.insert(0, os.environ["DWS_AGENT_SRC"])
from dws_agent.core import paths as cp
from dws_agent.executor import refresh_guard as rg
paths = cp.get_paths()
try:
    with rg.refresh_lock(paths, timeout=0.3, purpose="child"):
        print("WON")
        import time; time.sleep(0.5)
except TimeoutError:
    print("LOST")
"""


def test_cross_process_only_one_winner_while_held(home):
    """While the parent holds the lock, a child contender with a short timeout
    cannot enter the critical section (it times out => LOST)."""
    env = dict(os.environ)
    env["DWS_AGENT_SRC"] = str(__import__("conftest").SRC)

    with refresh_guard.refresh_lock(home, timeout=5, purpose="parent"):
        # Parent holds it; child must fail to acquire within its short timeout.
        child = subprocess.run(
            [sys.executable, "-c", _CHILD],
            env=env, capture_output=True, text=True, check=False,
        )
        assert child.stdout.strip() == "LOST", (child.stdout, child.stderr)

    # Once the parent releases, a fresh child CAN acquire it.
    after = subprocess.run(
        [sys.executable, "-c", _CHILD],
        env=env, capture_output=True, text=True, check=False,
    )
    assert after.stdout.strip() == "WON", (after.stdout, after.stderr)


def test_two_concurrent_children_exactly_one_wins(home):
    """Two children race for the lock simultaneously; with a short timeout at
    most one can hold it at a time. The holder sleeps 0.5s while the other has
    only a 0.3s timeout, so exactly one WON and one LOST."""
    env = dict(os.environ)
    env["DWS_AGENT_SRC"] = str(__import__("conftest").SRC)

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(2)
    ]
    outs = [p.communicate()[0].strip() for p in procs]
    assert outs.count("WON") == 1, outs
    assert outs.count("LOST") == 1, outs
