#!/usr/bin/env python3
"""KDL 灌库工具：把"已成型候选"直入 Ingestor（候选直入，绕过 distiller）。

为什么不用 `dws-agent kb ingest --input`：那条路把输入当"原始源材料"再过一遍
distiller（默认 stub），会把 index_repo / Claude 蒸馏已经产好的*候选*二次处理而
错位。本工具直接交给无 LLM 的 Ingestor.ingest_candidates 落库。

支持两种输入：
  --repo <path>        对 git 仓库跑 GitReader.index_repo 产 CODE 候选（确定性、只读）
  --candidates <json>  读一份已蒸馏的候选 JSON（CODE/QA/ISSUE/PLAYBOOK）

入库链路（无 LLM）：候选 -> Ingestor（校验 provenance/脱敏/污点/强制 DRAFT/AES 加密）
-> $DWS_AGENT_HOME/state/state.db。全程只读源、绝不对外发送、绝不调 dws 写。

用法（需 PYTHONPATH=src）：
  PYTHONPATH=src python3 scripts/kdl_ingest.py --repo /path/to/repo
  PYTHONPATH=src python3 scripts/kdl_ingest.py --candidates distilled.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from dws_agent.kdl.cli import _enc_key, _load_paths, _open_conn
from dws_agent.kdl.ingest import Ingestor


def _load_code_candidates(repo: str) -> list:
    """对一个 git 仓库跑 index_repo（传绝对路径，确保 repo 名非空）。"""
    from dws_agent.kdl.code import GitReader

    repo_abs = os.path.abspath(repo)
    cands = GitReader(repo_abs).index_repo(repo_abs)
    print("[index] %s -> %d CODE 候选" % (repo_abs, len(cands)))
    return cands


def _load_json_candidates(path: str) -> list:
    data = json.load(open(path, encoding="utf-8"))
    cands = data if isinstance(data, list) else [data]
    print("[candidates] %s -> %d 候选" % (path, len(cands)))
    return cands


def main() -> int:
    ap = argparse.ArgumentParser(description="KDL 候选直入灌库工具")
    ap.add_argument("--repo", help="git 仓库路径（跑 index_repo 产 CODE 候选）")
    ap.add_argument("--candidates", help="已蒸馏候选 JSON 路径")
    ap.add_argument("--default-taint", default="INTERNAL")
    args = ap.parse_args()

    if not args.repo and not args.candidates:
        print("需要 --repo 或 --candidates", file=sys.stderr)
        return 2

    cands: list = []
    if args.repo:
        cands += _load_code_candidates(args.repo)
    if args.candidates:
        cands += _load_json_candidates(args.candidates)

    if not cands:
        print("无候选，跳过")
        return 0

    paths = _load_paths()
    conn = _open_conn(paths)
    key = _enc_key()
    ing = Ingestor(paths, conn, key)
    report = ing.ingest_candidates(cands, args.default_taint)

    ingested = getattr(report, "ingested", None) or []
    dropped = getattr(report, "dropped", None) or []
    redacted = getattr(report, "redacted_count", 0)
    print("  ingested=%d dropped=%d redacted=%d"
          % (len(ingested), len(dropped), redacted))
    if isinstance(dropped, list):
        for d in dropped[:8]:
            print("   drop:", d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
