#!/usr/bin/env python3
"""摄取本人钉钉文档（adoc）正文到本地，供 Claude 蒸馏。只读、绝不发送/写。

链路（全部经 DwsReader 只读白名单）：
  contact user get-self → 本人 uid
  doc search --creator-uids <uid> --extensions adoc（翻页聚合）→ 本人 adoc 文档列表
  逐篇 doc read（Markdown）→ 存 /tmp/kdl-ddoc/<safe>.md（顶部带 nodeId/name 注释）

产出 /tmp/kdl-ddoc/manifest.json（nodeId/name/file/createTime/chars），供蒸馏阶段
按 manifest 取 provenance（kind=DOC_ID, ref=nodeId#section）。

用法：PYTHONPATH=src python3 scripts/kdl_fetch_docs.py
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, "src")

from dws_agent.kdl.dws_read import DwsReader

OUT = "/tmp/kdl-ddoc"


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    r = DwsReader()
    uid = r.self_uid()
    print("本人 uid:", uid)
    if not uid:
        print("ERROR: 拿不到本人 uid（确认 dws 已登录）", file=sys.stderr)
        return 2

    docs = r.doc_search_all(creator_uids=uid, extensions="adoc")
    print("本人 adoc 文档总数:", len(docs))

    manifest = []
    ok = 0
    for doc in docs:
        nid = doc.get("nodeId")
        name = doc.get("name") or nid
        if not nid:
            continue
        try:
            md = r.doc_read_markdown(nid)
        except Exception as e:  # noqa
            print("  跳过(读失败): %s -> %s" % (name, repr(e)[:60]))
            md = None
        if not md or not md.strip():
            continue
        safe = re.sub(r"[^\w\-]", "_", str(name))[:40] + "_" + str(nid)[:8]
        path = os.path.join(OUT, safe + ".md")
        header = "<!-- nodeId: %s | name: %s -->\n\n" % (nid, name)
        open(path, "w", encoding="utf-8").write(header + md)
        manifest.append(
            {"nodeId": nid, "name": name, "file": safe + ".md",
             "createTime": doc.get("createTime"), "chars": len(md)}
        )
        ok += 1

    json.dump(
        manifest, open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8"),
        ensure_ascii=False, indent=2,
    )
    print("成功读取正文: %d 篇 -> %s" % (ok, OUT))
    for m in manifest:
        print("  - %s (%d 字)" % (m["name"], m["chars"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
