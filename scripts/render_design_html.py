#!/usr/bin/env python3
"""渲染 docs/design/md/*.md → docs/design/html/*.html。

用法：
    python3 scripts/render_design_html.py           # 渲染所有
    python3 scripts/render_design_html.py --force   # 覆盖已存在的（默认只补缺失/更新）

设计文档源在 docs/design/md/，本地渲染版在 docs/design/html/（.gitignore 忽略）。
新增设计文档只需扔进 docs/design/md/，跑一次这个脚本就能出 html + 被 index.html 引用。

样式沿用 archive 里原有 HTML（蓝紫配色 + .wrap 容器 + 移动端断点）。
不做 syntax highlighting（避免 pygments 硬依赖，够看即可）。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

try:
    import markdown  # noqa: F401
except ImportError:
    sys.exit("需要 python-markdown：pip install markdown")

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "docs" / "design" / "md"
DST_DIR = ROOT / "docs" / "design" / "html"

MD_EXT = ["extra", "tables", "fenced_code", "toc", "sane_lists"]

TEMPLATE_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root{{--fg:#1f2933;--muted:#6b7280;--accent:#2563eb;--accent2:#7c3aed;--line:#e5e7eb;--bg:#ffffff;--code-bg:#f6f8fa;--th:#f0f4f8;}}
*{{box-sizing:border-box;}}
body{{margin:0;background:#f3f4f6;color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",Segoe UI,Roboto,Helvetica,Arial,sans-serif;line-height:1.78;font-size:16px;}}
.wrap{{max-width:1020px;margin:0 auto;padding:56px 30px 120px;background:var(--bg);box-shadow:0 1px 3px rgba(0,0,0,.08);}}
h1{{font-size:32px;font-weight:800;letter-spacing:-.5px;border-bottom:3px solid var(--accent);padding-bottom:14px;margin:0 0 14px;}}
h2{{font-size:24px;font-weight:700;margin:46px 0 16px;padding-left:13px;border-left:5px solid var(--accent);scroll-margin-top:20px;}}
h3{{font-size:19px;font-weight:700;margin:30px 0 12px;color:#111827;scroll-margin-top:20px;}}
h4{{font-size:16px;font-weight:700;margin:22px 0 8px;color:#374151;}}
p,li{{color:var(--fg);}}
a{{color:var(--accent);text-decoration:none;}}
a:hover{{text-decoration:underline;}}
blockquote{{margin:18px 0;padding:14px 20px;background:#eff6ff;border-left:4px solid var(--accent);border-radius:8px;color:#1e3a5f;}}
blockquote p{{margin:6px 0;}}
code{{background:var(--code-bg);padding:2px 6px;border-radius:5px;font-family:"SF Mono",ui-monospace,Menlo,Consolas,monospace;font-size:13.5px;color:#9333ea;}}
pre{{background:#0f172a;color:#e2e8f0;padding:18px 20px;border-radius:10px;overflow:auto;font-size:13px;line-height:1.6;}}
pre code{{background:none;color:inherit;padding:0;font-size:12.5px;}}
table{{border-collapse:collapse;width:100%;margin:18px 0;font-size:14.5px;display:block;overflow-x:auto;}}
th,td{{border:1px solid var(--line);padding:9px 12px;text-align:left;vertical-align:top;}}
th{{background:var(--th);font-weight:700;}}
tr:nth-child(even) td{{background:#fafbfc;}}
hr{{border:none;border-top:1px solid var(--line);margin:42px 0;}}
strong{{color:#111827;}}
ul,ol{{padding-left:24px;}}
li{{margin:4px 0;}}
.toc{{background:#f9fafb;border:1px solid var(--line);border-radius:12px;padding:18px 26px;margin:20px 0;}}
.toc ul{{padding-left:20px;}}
.toc li{{margin:2px 0;font-size:14.5px;}}
::selection{{background:#bfdbfe;}}
@media(max-width:640px){{.wrap{{padding:32px 16px 80px;}}}}
</style>
</head>
<body><div class="wrap">
"""
TEMPLATE_TAIL = "\n</div></body>\n</html>"


def derive_title(md_text: str, fallback: str) -> str:
    """取 md 首个 h1，fallback 到文件名。"""
    for line in md_text.splitlines():
        m = re.match(r"^\s*#\s+(.+?)\s*$", line)
        if m:
            return m.group(1).strip()
    return fallback


def render_one(src: pathlib.Path, dst: pathlib.Path) -> None:
    import markdown
    text = src.read_text(encoding="utf-8")
    title = derive_title(text, src.stem)
    body = markdown.markdown(text, extensions=MD_EXT, output_format="html5")
    html = TEMPLATE_HEAD.format(title=title) + body + TEMPLATE_TAIL
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(html, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="覆盖已存在且未过期的 html（默认按 mtime 判断）")
    args = ap.parse_args()

    if not SRC_DIR.exists():
        sys.exit(f"源目录不存在: {SRC_DIR}")

    mds = sorted(SRC_DIR.glob("*.md"))
    if not mds:
        print(f"无 md 文件在 {SRC_DIR}")
        return 0

    rendered = skipped = 0
    for src in mds:
        dst = DST_DIR / (src.stem + ".html")
        if dst.exists() and not args.force and dst.stat().st_mtime >= src.stat().st_mtime:
            print(f"  skip (up-to-date): {src.name}")
            skipped += 1
            continue
        render_one(src, dst)
        print(f"  rendered: {src.name} → design/html/{dst.name}")
        rendered += 1

    print(f"\n完成：渲染 {rendered} 个，跳过 {skipped} 个（用 --force 强制重跑）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
