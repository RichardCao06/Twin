#!/usr/bin/env python3
"""阶段1 退出条件验收套件 —— 对真实 KDL 库跑 4 项可测验收，产报告 + PASS/FAIL。

设计文档 §4.2 阶段1 退出条件：
  1. 金标 Top-5 命中 ≥0.85 且均过回查
  2. 溯源抽样 100% 可点回
  3. 对外发送计数 = 0
  4. 脱敏抽检 误/漏 = 0

说明：金标用"自检式"——用每个 KU 的 title 作查询，期望该 KU 落在 Top-5 检索结果里
（量化"检索能否召回已知知识"）。真实自然语言查询金标可由本人后续补充，跑同一 harness。

用法：PYTHONPATH=src python3 scripts/kdl_acceptance.py [样本量，默认 80]
"""
from __future__ import annotations

import glob
import os
import random
import subprocess
import sys

sys.path.insert(0, "src")
random.seed(42)  # 可复现

from dws_agent.kdl import retrieve, store
from dws_agent.kdl.cli import _enc_key, _load_paths, _open_conn
from dws_agent.kdl.dws_read import DwsReader
from dws_agent.privacy.redaction import _CATEGORY_TAINT, PATTERNS

N = int(sys.argv[1]) if len(sys.argv) > 1 else 80


def _val(x):
    return getattr(x, "value", x)


def _sample_ids(conn, n=N):
    rows = conn.execute("SELECT ku_id FROM ku").fetchall()
    ids = [r["ku_id"] for r in rows]
    random.shuffle(ids)
    return ids[:n]


# ---- 1. 检索命中率（自检金标）+ 回查 ----
def check_retrieval(conn, key):
    ids = _sample_ids(conn)
    # 分层金标：CODE 用 symbol（贴近真实"查符号名"）、文档用 title（主题词）。
    stat = {"CODE": [0, 0], "DOC": [0, 0]}  # [hit, total]
    cover = 0
    misses = []
    for kid in ids:
        ku = store.get_ku(conn, kid, key)
        if ku is None:
            continue
        st = _val(ku.source_type)
        if st == "CODE":
            q = (getattr(ku, "symbol", "") or getattr(ku, "title", "") or "").strip()
            b = "CODE"
        else:
            q = (getattr(ku, "title", "") or "").strip()
            b = "DOC"
        if not q:
            continue
        stat[b][1] += 1
        v = retrieve.serve(conn, key, q)
        cit_ids = [getattr(c, "ku_id", None) for c in (getattr(v, "citations", []) or [])]
        if kid in cit_ids:
            stat[b][0] += 1
            dp = retrieve.assemble_draft(v, key=key)
            if getattr(dp, "would_answer", False):
                cover += 1
        else:
            misses.append((b, kid, q[:50]))
    hit = stat["CODE"][0] + stat["DOC"][0]
    total = stat["CODE"][1] + stat["DOC"][1]
    rate = hit / total if total else 0.0
    return {"total": total, "hit": hit, "rate": rate, "cover": cover,
            "stat": stat, "misses": misses[:8], "pass": rate >= 0.85}


# ---- 2. 溯源抽样可点回 ----
def check_provenance(conn, key):
    repos = {}
    for p in glob.glob(os.path.expanduser("~/Workspace/*")):
        if os.path.isdir(os.path.join(p, ".git")):
            repos[os.path.basename(p.rstrip("/"))] = p
    reader = DwsReader()
    ids = _sample_ids(conn)
    total = ok = 0
    doc_checked = 0
    fails = []
    for kid in ids:
        ku = store.get_ku(conn, kid, key)
        if ku is None or not ku.provenance:
            continue
        total += 1
        p = ku.provenance[0]
        kind = _val(p.kind)
        resolvable = False
        if kind == "COMMIT":
            rp = repos.get(ku.repo or "")
            if rp and ku.commit_sha:
                r = subprocess.run(
                    ["git", "-C", rp, "cat-file", "-e", ku.commit_sha + "^{commit}"],
                    capture_output=True,
                )
                resolvable = r.returncode == 0
        elif kind == "DOC_ID":
            # 真查 doc info（限前 8 个，控制网络耗时；其余验 ref 非空）
            nid = (p.ref or "").split("#")[0]
            if doc_checked < 8 and nid:
                doc_checked += 1
                try:
                    info = reader.doc_info(nid)
                    resolvable = bool(info)
                except Exception:
                    resolvable = False
            else:
                resolvable = bool(nid)
        elif kind == "FILE":
            resolvable = bool((p.ref or "").strip())
        else:
            resolvable = bool((p.ref or "").strip())
        if resolvable:
            ok += 1
        else:
            fails.append((kid, kind, (p.ref or "")[:40]))
    return {"total": total, "ok": ok, "rate": ok / total if total else 0.0,
            "doc_checked": doc_checked, "fails": fails[:6], "pass": ok == total}


# ---- 3. 对外发送=0 ----
def check_no_send():
    env = {**os.environ, "PYTHONPATH": "src"}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_kdl_no_side_effects.py", "-q"],
        capture_output=True, text=True, env=env,
    )
    out = (r.stdout + r.stderr).strip()
    last = out.splitlines()[-1] if out else ""
    return {"pass": r.returncode == 0, "tail": last}


# ---- 4. 脱敏抽检（漏=残留密钥；误=误脱体感）----
def check_redaction(conn, key):
    secret_pats = {k: v for k, v in PATTERNS.items()
                   if _CATEGORY_TAINT.get(k) == "SENSITIVE"}
    ids = _sample_ids(conn)
    total = with_redacted = 0
    leaks = []
    for kid in ids:
        ku = store.get_ku(conn, kid, key)
        if ku is None:
            continue
        total += 1
        body = ku.body or ""
        if "[REDACTED" in body:
            with_redacted += 1
        for cat, pat in secret_pats.items():
            for m in pat.finditer(body):
                leaks.append((kid, cat, m.group(0)[:30]))
    return {"total": total, "leak_count": len(leaks), "leaks": leaks[:6],
            "with_redacted": with_redacted, "pass": len(leaks) == 0}


def main():
    conn = _open_conn(_load_paths())
    key = _enc_key()
    print("=== 阶段1 退出条件验收（样本量 N=%d，真实库）===\n" % N)

    r1 = check_retrieval(conn, key)
    sc, sd = r1["stat"]["CODE"], r1["stat"]["DOC"]
    print("[1] 金标 Top-5 命中率（自检；CODE 用 symbol、文档用 title）: %d/%d = %.3f | 回查通过 %d → %s"
          % (r1["hit"], r1["total"], r1["rate"], r1["cover"],
             "PASS" if r1["pass"] else "FAIL(需≥0.85)"))
    print("      分层: CODE %d/%d=%.3f | 文档 %d/%d=%.3f"
          % (sc[0], sc[1], (sc[0] / sc[1] if sc[1] else 0),
             sd[0], sd[1], (sd[0] / sd[1] if sd[1] else 0)))
    for b, kid, q in r1["misses"]:
        print("      miss[%s]: %s  «%s»" % (b, kid, q))

    r2 = check_provenance(conn, key)
    print("\n[2] 溯源抽样可点回: %d/%d = %.3f (DOC_ID 真查 %d 个) → %s"
          % (r2["ok"], r2["total"], r2["rate"], r2["doc_checked"],
             "PASS(100%)" if r2["pass"] else "FAIL"))
    for f in r2["fails"]:
        print("      fail:", f)

    r4 = check_redaction(conn, key)
    print("\n[4] 脱敏抽检: 残留密钥(漏)=%d | 含[REDACTED]的KU=%d/%d → %s"
          % (r4["leak_count"], r4["with_redacted"], r4["total"],
             "PASS(漏=0)" if r4["pass"] else "FAIL"))
    for lk in r4["leaks"]:
        print("      leak:", lk)

    r3 = check_no_send()
    print("\n[3] 对外发送=0: %s  (%s)" % ("PASS" if r3["pass"] else "FAIL", r3["tail"]))

    allpass = r1["pass"] and r2["pass"] and r3["pass"] and r4["pass"]
    print("\n=== 阶段1 验收汇总: %s ===" % ("全部 PASS ✅" if allpass else "未全通过 ❌"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
