# src/dws_agent 目录结构

三根柱子 → 子包映射：

| 子包 | 属于 | 内容 |
|---|---|---|
| kdl/ | 知识层 | KDL 知识库全套（10 模块，4200+ 行） |
| policy/ | 编排层 · 判决 | PolicyGate + confirm_token + never 名单 |
| executor/ | 编排层 · 执行 | Executor + shim + inbox + refresh_guard |
| store/ | 编排层 · 持久化 | 审计流 + state DB + undo |
| privacy/ | 编排层 · 隐私边界 | 脱敏 + 污点 + 单聊过滤 |
| claudecenter/ | 编排层 · 派活 | ClaudeCenter Worker 桥接 |
| contracts/ | 编排层 · 契约 | ActionIntent JSON schema |
| cli/ | 编排层 · 入口 | dws-agent 主命令 + dwsd |
| triage/ | 编排层 · 应用（原 mvp/） | 分诊代答链路：读消息→拟答→确认代发 |
| analysis/ | 编排层 · 应用（原 diagnose/ + impact/） | dws-agent diagnose + impact |
| evolution/ | 进化层（占位） | 进化层物理载体主要在项目外（~/.claude memory + docs/retro/）；本包保留占位承载未来"读 memory / 起草复盘草稿 / 便签失效检测"这类工具 |
| core/ | 基础设施 | paths / crypto / config / scaffold |

参考：
- docs/design/md/dws-agent-设计方案.md 里的三柱子叙事
- docs/overview/dws-agent-项目说明.html 里的通俗介绍
