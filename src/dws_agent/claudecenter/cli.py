"""``dws-agent task`` 命令组：把钉钉对话/内容建成 ClaudeCenter 任务。

    dws-agent task projects                               列出 ClaudeCenter 项目
    dws-agent task create --project P --title T \
                          --description D | --description-file F   建 draft（不发布）
    dws-agent task publish <id>                           你确认后发布 → Worker 执行

安全分层：``create`` 只建 **draft**（草稿不被认领、不执行，无害）；``publish`` 才把
任务放行进队列——那是你显式确认的危险动作（钉钉消息 → 自动改代码 → 提 PR）。
把口语化对话提炼成结构化、可执行的 ``--title`` / ``--description`` 由 Claude 会话内完成。

配置经环境变量：CLAUDE_CENTER_URL / CLAUDE_CENTER_USER / CLAUDE_CENTER_PASSWORD。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _client():
    from dws_agent.claudecenter.client import ClaudeCenterClient

    return ClaudeCenterClient()


# --------------------------------------------------------------------------- #
# task projects —— 列项目（拿 --project 的名字/ID）
# --------------------------------------------------------------------------- #
def cmd_task_projects(args) -> int:
    from dws_agent.claudecenter.client import ClaudeCenterError

    try:
        projects = _client().list_projects()
    except ClaudeCenterError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 3
    if not projects:
        print("（ClaudeCenter 暂无项目；先在 Console 建 project，"
              "并让某个 Worker 关联它的本地路径）")
        return 0
    print("ClaudeCenter 项目（%d）：" % len(projects))
    for p in projects:
        print("  - %-24s id=%s  repo=%s  default=%s"
              % (p.get("name"), p.get("id"), p.get("repo_url"), p.get("default_branch")))
    return 0


# --------------------------------------------------------------------------- #
# task create —— 建 draft（默认不发布）
# --------------------------------------------------------------------------- #
# 自动追加到每个 task description 的「测试门禁」——通过 prompt 要求 worker 提 PR 前
# 写并跑通 mock 单测（验证降级，见 docs/复盘-2026-06-23.md 测试金字塔）。--no-test-gate 关。
TEST_GATE_CLAUSE = """\
---
## ✅ 测试门禁（提 PR 前必须满足；不满足不要提 PR）
1. 为本次改动写 **mock 依赖的单元/组件测试**：把外部依赖（后端响应/DB/Redis/网络/浏览器）mock 掉，
   验证改动的核心逻辑。例：前端"收到 code=4011 → 提示并跳登录"应 mock 响应、断言拦截器行为，**无需起真后端**。
2. **跑通项目测试套件并保证全绿**（如 `npm test` / `mvn test`）；**测试不过，不要提 PR**。
3. PR 的 Test Plan 里"运行时/交互验证"用上述 mock 单测覆盖、标记为已跑通（附命令 + 结果）；
   **不允许**用"需起后端 / 本环境未运行"作为未验证的借口——能 mock 的必须 mock 验证。
4. 真全链路 E2E（需完整部署环境）可标注"部署后验证"，但静态检查 + mock 单测必须本地跑过。"""


def _read_description(args) -> str:
    if args.description_file:
        return Path(args.description_file).read_text("utf-8")
    return args.description or ""


def cmd_task_create(args) -> int:
    from dws_agent.claudecenter.client import ClaudeCenterError

    try:
        desc = _read_description(args).strip()
    except FileNotFoundError as exc:
        print("ERROR: 读不到 description 文件：%s" % exc, file=sys.stderr)
        return 2
    if not desc:
        print("需 --description 或 --description-file（任务指令/prompt）", file=sys.stderr)
        return 2
    if not getattr(args, "no_test_gate", False):
        desc = desc + "\n\n" + TEST_GATE_CLAUSE

    try:
        cli = _client()
        proj = cli.resolve_project(args.project)
        base_branch = args.base_branch or proj.get("default_branch") or "main"
        target_branch = args.target_branch or base_branch

        # 预览（建的是 draft，无害；先让你看清要建什么）
        print("=== 任务预览（将建为 draft，不自动执行）===")
        print("  项目: %s  [id=%s]" % (proj.get("name"), proj.get("id")))
        print("  标题: %s" % args.title)
        print("  base 分支: %s  →  PR 目标分支: %s" % (base_branch, target_branch))
        print("  提交模式: %s   模型: %s   自动回复: %s"
              % (args.submit_mode, args.model, args.auto_reply))
        print("  指令（%d 字）:" % len(desc))
        lines = desc.splitlines()
        for line in lines[:30]:
            print("    | %s" % line)
        if len(lines) > 30:
            print("    | … (+%d 行)" % (len(lines) - 30))

        task = cli.create_task(
            proj.get("id"), args.title, desc,
            base_branch=base_branch, target_branch=target_branch,
            submit_mode=args.submit_mode, auto_reply=args.auto_reply, model=args.model)
    except ClaudeCenterError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 3

    tid = task.get("id")
    print("\n✅ 已建 draft 任务：id=%s  status=%s" % (tid, task.get("status")))
    print("   在 Console 复核无误后放行执行： dws-agent task publish %s" % tid)
    return 0


# --------------------------------------------------------------------------- #
# task publish —— 你确认后发布（draft→pending→Worker 执行）
# --------------------------------------------------------------------------- #
def cmd_task_publish(args) -> int:
    from dws_agent.claudecenter.client import ClaudeCenterError

    try:
        task = _client().publish_task(args.task_id)
    except ClaudeCenterError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 3
    print("✅ 已发布：id=%s  status=%s —— Worker 将认领并执行（提 PR）。"
          % (task.get("id"), task.get("status")))
    print("   进度 / 验收去 ClaudeCenter Console 看。")

    # 群进度播报（模板化·标注非本人·仅白名单研发群·经阶段0审计；见 announce.py）。
    # 可关：--no-announce 或 env DWS_AGENT_ANNOUNCE=0。失败不影响 task 发布。
    if not getattr(args, "no_announce", False):
        try:
            from dws_agent.claudecenter import announce

            text, res = announce.announce_publish(task, getattr(args, "issue", None))
            if text:
                print("\n=== 群进度播报（HiQ 产品研发）===")
                for ln in text.splitlines():
                    print("  | %s" % ln)
                if res is not None:
                    print("  播报: level=%s exit=%s" % (res.level, res.exit_code))
        except Exception as exc:  # noqa: BLE001 - 播报失败绝不阻塞 task
            print("  ⚠ 群播报失败（不影响 task 发布）：%s" % exc, file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# argparse wiring — register_task(subparsers)
# --------------------------------------------------------------------------- #
def register_task(subparsers) -> None:
    """把 ``task`` 命令组挂到 ``dws-agent`` 的 add_subparsers 上（懒加载、非致命）。"""
    p_task = subparsers.add_parser(
        "task",
        help="把钉钉对话/内容建成 ClaudeCenter 任务（建 draft → 你确认 publish → Worker 执行）",
        description=("对接 ClaudeCenter（apps/console REST API）。create 只建 draft、"
                     "绝不自动执行；publish 是你显式确认放行。"
                     "配置：CLAUDE_CENTER_URL / CLAUDE_CENTER_USER / CLAUDE_CENTER_PASSWORD。"),
    )
    task_sub = p_task.add_subparsers(dest="task_command", required=True)

    p_pj = task_sub.add_parser("projects", help="列出 ClaudeCenter 项目")
    p_pj.set_defaults(func=cmd_task_projects)

    p_cr = task_sub.add_parser("create", help="建 draft 任务（默认不发布）")
    p_cr.add_argument("--project", required=True, help="项目名或 id（见 task projects）")
    p_cr.add_argument("--title", required=True, help="任务标题")
    g = p_cr.add_mutually_exclusive_group(required=True)
    g.add_argument("--description", help="任务指令 / prompt 文本")
    g.add_argument("--description-file", help="从文件读任务指令（长 prompt 用）")
    p_cr.add_argument("--base-branch", help="基础分支（工作分支从这拉；默认取项目默认分支）")
    p_cr.add_argument("--target-branch", help="PR 目标分支（PR 合并到这；默认同 --base-branch）")
    p_cr.add_argument("--submit-mode", choices=["pr", "push"], default="pr",
                      help="提交模式（默认 pr：建 PR）")
    p_cr.add_argument("--auto-reply", action="store_true",
                      help="遇歧义自动决策（无人值守，谨慎用）")
    p_cr.add_argument("--model", choices=["default", "opus", "sonnet", "haiku"],
                      default="default", help="执行模型（默认 default）")
    p_cr.add_argument("--no-test-gate", action="store_true",
                      help="不追加测试门禁条款（纯文档/配置类 task 用）")
    p_cr.set_defaults(func=cmd_task_create)

    p_pub = task_sub.add_parser(
        "publish", help="发布 draft（draft→pending，Worker 认领执行）")
    p_pub.add_argument("task_id", help="任务 id（task create 返回的）")
    p_pub.add_argument("--issue", type=int,
                       help="关联的 GitHub issue 编号（群播报里引用，如 24）")
    p_pub.add_argument("--no-announce", action="store_true",
                       help="本次不发群进度播报（默认会向研发群播报）")
    p_pub.set_defaults(func=cmd_task_publish)
