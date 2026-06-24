#!/usr/bin/env python3
"""feedback 巡检：检测 HiQ-AI/feedback 新 open issue → 建 ClaudeCenter draft → 通知曹勇。

守底线：**只建 draft（不执行）+ 通知，绝不自动 publish**（publish 由曹勇在 Console 手动确认）。
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
    built, skipped = [], []
    for issue in new:
        num, title, line = issue["number"], issue["title"], _line_of(issue)
        project = LINE_TO_PROJECT.get(line or "")
        if not project:
            skipped.append((num, title, line or "无 line"))
            continue
        if dry:
            built.append((num, title, project, "DRY"))
            continue
        tid, _ = _create_draft(num, title, line, project)
        built.append((num, title, project, tid or "?"))
        seen.add(num)

    if built or skipped:
        lines = ["🤖 feedback 巡检（自动建 draft，待你确认 publish）"]
        if built:
            lines.append("【已建 draft】")
            for num, title, proj, tid in built:
                lines.append("· #%d《%s》→ %s  draft=%s" % (num, title[:28], proj, tid))
        if skipped:
            lines.append("【未自动建·需手动】(line 无对应 project)")
            for num, title, line in skipped:
                lines.append("· #%d《%s》(%s)" % (num, title[:28], line))
        lines.append("（自动巡检·非本人发言；publish 前在 Console 核对 project）")
        text = "\n".join(lines)
        if dry:
            print("=== DRY-RUN 通知预览 ===\n" + text)
        else:
            _notify(text)
    elif dry:
        print("（窗口内无新 issue）")

    if not dry:
        _save_seen(seen)
    print("\n巡检完成：新 %d / 建 draft %d / 跳过 %d%s"
          % (len(new), len([b for b in built if b[3] != "DRY"]), len(skipped),
             "（dry-run，未真建/未发/未写状态）" if dry else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
