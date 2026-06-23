#!/usr/bin/env python3
"""合并 <dir>/doc-*.json 的文档候选，并给每张卡的 provenance ref 追加全局唯一
后缀 #k<i>，避免非 CODE 候选 make_ku_id 撞键（同文件多卡互相覆盖）。

非 CODE 的 ku_id = sha1(source_type | prov_ref | '' | '')，只取决于 source_type
和首条 provenance.ref；同文件多张卡若 ref 相同就会算出同一 ku_id 而覆盖。给 ref
追加唯一序号即可保证一一对应（ref 仍以原文件路径开头，可溯源）。

用法: python3 scripts/kdl_dedup_docs.py [候选目录, 默认 /tmp/kdl-build]
输出 <dir>/all-docs-unique.json（合法 JSON 数组）。
"""
import glob
import json
import sys

SRC = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "/tmp/kdl-build"

out = []
i = 0
per = {}
for f in sorted(glob.glob(SRC + "/doc-*.json")):
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception as e:  # noqa
        print("跳过(解析失败):", f, e)
        continue
    if not isinstance(d, list):
        print("跳过(非数组):", f)
        continue
    per[f.split("/")[-1]] = len(d)
    for cand in d:
        provs = cand.get("provenance") or []
        if provs and isinstance(provs[0], dict):
            ref = provs[0].get("ref", "") or ""
            provs[0]["ref"] = "%s#k%d" % (ref, i)
        i += 1
        out.append(cand)

out_path = SRC + "/all-docs-unique.json"
json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False)
print("合并文档候选: %d 张 -> %s (ref 已唯一化, 防 ku_id 撞键)" % (len(out), out_path))
for k, v in per.items():
    print("  %s: %d" % (k, v))
