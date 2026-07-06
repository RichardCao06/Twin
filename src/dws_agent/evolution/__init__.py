"""进化层占位子包（Evolution Layer, Placeholder）.

DWS-Agent 三根柱子里的"进化层"，物理载体绝大部分在项目外:

- memory/*.md: ~/.claude/projects/<slug>/memory/（每次会话自动加载）
- MEMORY.md 索引: 同上
- CLAUDE.md 自动加载机制: Claude Code 平台特性
- 复盘文档: docs/retro/*.md（仅这一处在项目内）

本包为进化层保留一个"物理门牌"——目前只有 __init__.py，未来承载:

- 读 memory 的 helper（可能是 dws-agent memory search）
- 起草复盘草稿（复盘时脚本化模板 + 数据填充）
- 便签失效检测（扫描便签里 file/function 引用是否还存在）
- 从复盘自动提炼便签草稿

设计意图: 三根柱子对称——让读代码的人一眼看到 evolution 子包,
明白"进化层不在这里", 而不是误以为项目只有 2 根柱子。
"""
