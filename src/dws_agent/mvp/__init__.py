"""MVP 工作流（手动触发的"读消息→检索→拟答→你确认→代发"闭环）。

子命令在 :mod:`dws_agent.mvp.cli`，由 ``dws-agent`` 主 CLI 懒加载挂载：

- ``dws-agent triage`` —— 读钉钉消息 + KDL 检索 → 拟答包（纯只读）。
- ``dws-agent send``   —— 你确认后经阶段0 安全链路代发（默认仅预览）。

拟答（理解 + 组织答复）由 Claude 在会话内完成（D3「你就是 LLM」），
不在脚本里；详见 ``docs/方案-MVP.md``。
"""
