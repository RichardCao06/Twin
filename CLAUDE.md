# CLAUDE.md

> 项目文档索引（每次会话自动加载）。只放指针，不内联进度——进度以 git log + 持久记忆 `MEMORY.md` 为准。

## 当前执行蓝图（先看这个）

- **[docs/design/md/方案-MVP.md](docs/design/md/方案-MVP.md)** —— MVP 优先方案：手动触发 → 读钉钉消息 → 检索 KDL → Claude 拟答 → 你确认 → 代发。**前期只走通这条线**；复杂的分级/编排/自治推到后期。

## 参考文档

| 文档 | 作用 |
|---|---|
| [docs/design/md/dws-agent-设计方案.md](docs/design/md/dws-agent-设计方案.md) | 长期完整愿景（7 约束/三套分级/四角色/五阶段）；MVP 跑通后逐级演进 |
| [docs/overview/dws-只读接口校准.md](docs/overview/dws-只读接口校准.md) | dws 只读命令+字段校准（读消息/文档，MVP 用） |
| [README.md](README.md) | 阶段 0/1 已实现内容、退出条件、如何跑测试 |
| docs/design/md/ | 所有技术设计文档（含 KDL 数据接入方案/子方案/会话记录） · [docs/design/html/](docs/design/html/) 是同源渲染版 |

## 做到哪 / 下一步

- ✅ 阶段 0（安全地基）+ 阶段 1（KDL 知识库 7289 KU，验收通过、已签署）。
- 🔜 当前：按 MVP 走通"读消息→拟答→你确认代发"。
- 进度详情 → `git log` + 持久记忆 `MEMORY.md`。

## 已建的关键件（直接复用）

- `dws-agent kb search/draft/status` —— 检索知识库
- `scripts/kdl_*.py` —— 灌库/验收工具链；`src/dws_agent/kdl/dws_read.py` `DwsReader` —— dws 只读封装
- 阶段0 Executor + PolicyGate + confirm_token + dws-shim —— 安全代发底座
