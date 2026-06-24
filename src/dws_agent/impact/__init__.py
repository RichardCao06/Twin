"""影响面体检：改共享组件前，自动枚举依赖方 + 提示跨系统风险。

动机（见 docs/复盘-2026-06-23.md）：feedback#24 改 dataset-sso 的 `is-concurrent=false`
时没枚举出 backend/editor/square 共用同一 SSO，上线后才实测发现跨系统互踢。本工具在
「改共享组件」前给出影响面，让这类副作用提前可见。

命令在 :mod:`dws_agent.impact.cli`，由 ``dws-agent`` 主 CLI 懒加载挂载为 ``dws-agent impact``。
"""
