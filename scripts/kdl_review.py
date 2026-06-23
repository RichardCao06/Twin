#!/usr/bin/env python3
"""把正式库里所有 DRAFT 的 KU 批量升到 REVIEWED。

语义：本人一次性"轻确认"——这些知识可作"待复核"级参考被检索/代答（仍标待复核，
非逐条审过的 AUTHORITATIVE 权威）。升级后 serve_blocked 归零、检索可召回。

走 store.set_authority 的状态机（DRAFT->REVIEWED 合法，禁越级到 AUTHORITATIVE），
不绕过任何安全规则。

用法：PYTHONPATH=src python3 scripts/kdl_review.py [--reason "..."]
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "src")

from dws_agent.kdl import store
from dws_agent.kdl.cli import _load_paths, _open_conn


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--reason",
        default="batch review: operator-trusted Workspace code/doc index (待复核级)",
    )
    args = ap.parse_args()

    conn = _open_conn(_load_paths())
    rows = conn.execute("SELECT ku_id FROM ku WHERE authority='DRAFT'").fetchall()
    ok = 0
    first_err = None
    for r in rows:
        try:
            store.set_authority(conn, r["ku_id"], "REVIEWED", args.reason)
            ok += 1
        except Exception as e:  # noqa
            if first_err is None:
                first_err = repr(e)

    print("升级 DRAFT->REVIEWED: %d / %d  首个错误: %s" % (ok, len(rows), first_err))
    sb = conn.execute("SELECT COUNT(*) c FROM ku WHERE serve_blocked=1").fetchone()["c"]
    total = conn.execute("SELECT COUNT(*) c FROM ku").fetchone()["c"]
    print("库内 KU 总数: %d  剩余 serve_blocked: %d" % (total, sb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
