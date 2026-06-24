"""ClaudeCenter 集成：把钉钉对话/内容建成可执行的 ClaudeCenter 任务。

ClaudeCenter（Next.js Console + Electron Worker + PostgreSQL）已把"建任务 →
Worker 跑 Claude Code → 提 PR"全做好；这里只补一个"钉钉入口"：经它的 REST API
登录 → 建 draft 任务 → 你确认后发布。执行/提 PR 由 ClaudeCenter 负责。

- :mod:`dws_agent.claudecenter.client` —— 最小 HTTP 客户端（仅标准库）。
- :mod:`dws_agent.claudecenter.cli`    —— ``dws-agent task`` 子命令组。

安全分层（与 MVP send 一致）：``create`` 只建 **draft**（草稿不被 Worker 认领、
不执行，无害）；``publish`` 才把 draft 放行进队列——那是你显式确认的危险动作。
拟答/提炼（把口语化对话变成结构化、可执行的 task 指令）由 Claude 会话内完成。
"""
