#!/usr/bin/env python3
"""feedback 巡检：检测 HiQ-AI/feedback 新 open issue → 钉钉通知曹勇（列出新 issue）。

守底线：**只通知、不建单、不执行**——巡检只告知有哪些新 issue，处理哪些由曹勇手动决定。
（历史：曾自动建 ClaudeCenter draft；2026-06-25 按要求降级为「只通知」。_create_draft / LINE_TO_PROJECT 保留待将来按需启用。）
供 launchd 每小时调用；也可手动：
  --dry-run   只预览（gh 拉取 + 映射），不建 draft、不发通知、不写状态
  --init      把当前所有 open issue 标记为"已见"（不建单），之后巡检只处理新增

依赖（由调用环境/ launchd plist 设置）：
  PYTHONPATH=src, CLAUDE_CENTER_URL/USER/PASSWORD, DWS_AGENT_DWS_BIN（通知用真实 dws）, gh 已登录。
"""

import json
import os
import subprocess
import sys
import tempfile

REPO = "HiQ-AI/feedback"
CAOYONG_UID = "0113226846838382"
STATE = os.path.expanduser("~/.claude/dws-agent/feedback_seen.json")
# line label → ClaudeCenter project（只映射已有 project 的线；其余线只通知、不自动建）
LINE_TO_PROJECT = {
    "line:editor": "dataset-web",        # 默认前端；后端问题 publish 前在 Console 改成 dataset/sso
    "line:ops-admin": "hiq-backend-admin",
}
DWS = [sys.executable, "-m", "dws_agent.cli.main"]


def _load_seen():
    try:
        with open(STATE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_seen(seen):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(sorted(seen), f)


def _gh_open_issues():
    r = subprocess.run(
        ["gh", "issue", "list", "--repo", REPO, "--state", "open",
         "--limit", "80", "--json", "number,title,labels"],
        capture_output=True, text=True, check=False)
    try:
        return json.loads(r.stdout or "[]")
    except Exception:
        return []


def _line_of(issue):
    for lab in issue.get("labels", []):
        n = lab.get("name", "")
        if n.startswith("line:"):
            return n
    return None


def _create_draft(num, title, line, project):
    desc = (
        "来自 GitHub issue **HiQ-AI/feedback#%d**（%s）。\n\n"
        "## 原始问题\n%s\n\n"
        "## 巡检建单说明（自动）\n"
        "- project=`%s` 是按 line 自动映射，**publish 前请在 Console 确认前后端/归属是否正确**；\n"
        "- 先用 KDL / 代码排查根因再改；PR 标题/描述/commit 用中文。\n"
        "- 关联 feedback#%d。\n" % (num, line, title, project, num)
    )
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(desc)
        descfile = f.name
    r = subprocess.run(
        DWS + ["task", "create", "--project", project, "--title", title,
               "--description-file", descfile],
        capture_output=True, text=True, check=False)
    tid = None
    for ln in (r.stdout or "").splitlines():
        if "id=" in ln and "draft" in ln:
            tid = ln.split("id=")[1].split()[0]
    return tid, (r.stdout or "") + (r.stderr or "")


def _notify(text):
    subprocess.run(DWS + ["send", "--user", CAOYONG_UID, "--text", text, "--confirm"],
                   capture_output=True, text=True, check=False)


def main():
    dry = "--dry-run" in sys.argv
    init = "--init" in sys.argv
    seen = _load_seen()
    issues = _gh_open_issues()

    if init:
        nums = {i["number"] for i in issues}
        _save_seen(nums)
        print("已初始化：标记 %d 个当前 open issue 为已见，之后只处理新增。" % len(nums))
        return 0

    new = [i for i in issues if i["number"] not in seen]

    if new:
        lines = ["🤖 feedback 巡检助理：发现 %d 个新 issue（只通知，处理哪些你手动定）" % len(new)]
        for issue in new:
            num, title = issue["number"], issue["title"]
            line = (_line_of(issue) or "line:无").replace("line:", "")
            prio = next((l["name"].split(":", 1)[1] for l in issue.get("labels", [])
                         if l["name"].startswith("priority:")), "?")
            lines.append("· #%d [%s/%s]《%s》" % (num, line, prio, title[:34]))
            lines.append("  https://github.com/%s/issues/%d" % (REPO, num))
        lines.append("（助理自动巡检·非本人发言；不建单、不处理，由你手动决定）")
        text = "\n".join(lines)
        if dry:
            print("=== DRY-RUN 通知预览 ===\n" + text)
        else:
            _notify(text)
            for issue in new:
                seen.add(issue["number"])
    elif dry:
        print("（窗口内无新 issue）")

    if not dry:
        _save_seen(seen)
    print("\n巡检完成：新 %d%s"
          % (len(new),
             "（dry-run，未发/未写状态）" if dry
             else "（已通知 + 标记已见）" if new else "（无新增）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
