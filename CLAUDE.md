# CLAUDE.md

> 项目文档索引（每次会话自动加载）。只放指针，不内联进度——进度以 git log + 持久记忆 `MEMORY.md` 为准。

## 项目定位（先看这个）

**一个面向"个人 + 项目知识 / 多 Agent 编排 / 持续复盘"的工程助理平台。**

三根柱子：

- **知识（Knowledge）** —— KDL 灌入 Workspace 所有仓库 + 钉钉文档（7289+ KU），叠加 `memory/` 长期记忆和 `docs/retro/` 复盘沉淀，作为决策/答复/派活的事实来源。
- **编排（Orchestration）** —— 「无 LLM 确定性 Executor + PolicyGate 安全底座」之上，派活给远程 Worker（ClaudeCenter 桥接）或本地 subagent（Explore/uat-deploy/uat-verify/prod-verify 等），Human-in-loop 兜底。
- **复盘（Retrospective）** —— 每一次事故/上线/踩坑后写复盘 → 提炼进 `MEMORY.md` → 反过来指导下次决策（如"验收看最终产物"「链式根因想下一层」）。

钉钉（`dws` CLI）是这个平台的**入口通道之一**——最早的触发场景，仍在跑 feedback 巡检和 MVP 代答链路——但已经**不是重心**。实际的日常价值发生在"KDL 检索 + Worker 派活 + 复盘反哺"这条循环上。

## 参考文档

| 文档 | 作用 |
|---|---|
| [docs/design/md/dws-agent-设计方案.md](docs/design/md/dws-agent-设计方案.md) | 长期完整愿景（7 约束/三套分级/四角色/五阶段）；架构决策仍有效 |
| [docs/design/md/方案-MVP.md](docs/design/md/方案-MVP.md) | MVP1 钉钉代发链路（2026-06 已跑通）+ 平台演化下一步 |
| [docs/overview/dws-只读接口校准.md](docs/overview/dws-只读接口校准.md) | dws 只读命令+字段校准（钉钉入口用） |
| [README.md](README.md) | 阶段 0/1 已实现内容、退出条件、如何跑测试 |
| docs/design/md/ | 所有技术设计文档（含 KDL 数据接入方案/子方案/会话记录） · [docs/design/html/](docs/design/html/) 是同源渲染版 |
| docs/retro/ | 每日/事故复盘（真实工作实录，反哺 memory） |

## 阶段进度

- ✅ **阶段 0 · 安全地基** —— Executor + PolicyGate + confirm_token + dws-shim + 审计
- ✅ **阶段 1 · 知识层 KDL** —— 7289 KU 灌入完成、验收 4 条 PASS 已签署
- ✅ **阶段 2 · MVP1 钉钉代发** —— 读消息→检索→拟答→你确认→代发闭环跑通
- 🔄 **平台化演化（进行中）** —— ClaudeCenter Worker 桥接、多 subagent 协同、复盘自动化、生产验证工具链（`ks_logs.py`、`prod-verify` 等）
- 进度详情 → `git log` + `MEMORY.md`

## 双向复盘 · 协作类 memory（本项目扩展）

除了系统自带的 4 类 memory（`user` / `feedback` / `project` / `reference`），本项目**额外使用第 5 类 `collaboration`** —— 记录"人 + 分身协作模式的观察 + 双方约定的协作规则"。这是"分身 = 我在电脑里的另一个我"定位的物理落地：**分身不只是被塑造的仆从，也是能给用户反馈的搭档**。

**语气原则（硬约束）：** collaboration 规则不是 agent 评判 user——是"我们下次可以怎么约定"。就像现实里两个搭档合作久了发现"你俩配合有个地方可以调"——不是教育，是镜子。每一条规则的措辞必须过这道关：读起来像"我们的约定" ✅，而不是"你应该改" ❌。

**触发时机：**
- **复盘时** 按"### Agent 侧 + ### 协作侧"两个维度分别写（见 `docs/retro/复盘-2026-07-06.md` 为首个范例）
- **跨会话累积** N 次同类模式时 agent 可以主动提出（"我发现最近几次任务有个协作模式想跟你讨论"）
- **user review 后才落盘**——同 `feedback` / `project` 类 memory 一样过 human-in-loop 门槛

**格式：** 跟其他类型 memory 完全一致——frontmatter + `**Why:**` + `**How to apply:**`，metadata.type 用 `collaboration`。

**opt-in：** 用户可以随时说 "停止协作观察" 或 "只在我要求时写"——分身应尊重（这本身就是协作偏好）。默认开。

**已落 4 条**（首批，2026-07-06 复盘催生）：
- [clarify-implicit-approval-on-multi-decision](../.claude/projects/-Users-shujudagongren-Myspace-dingding-agent/memory/clarify-implicit-approval-on-multi-decision.md) —— 用户简短回复 + 上下文多决策点 → 主动列一遍求确认
- [check-env-health-before-uat-task](../.claude/projects/-Users-shujudagongren-Myspace-dingding-agent/memory/check-env-health-before-uat-task.md) —— uat / 生产任务开工前做 quick health check
- [agent-fail-must-trace-one-layer](../.claude/projects/-Users-shujudagongren-Myspace-dingding-agent/memory/agent-fail-must-trace-one-layer.md) —— 子代理报 FAIL 时追一层根因再转报
- [break-down-multi-step-request](../.claude/projects/-Users-shujudagongren-Myspace-dingding-agent/memory/break-down-multi-step-request.md) —— 用户派 3+ 步的活时主动列执行计划 + 检查点

## 已建的关键件（直接复用）

- `dws-agent kb search/draft/status` —— 检索知识库
- `dws-agent task create/publish` —— 派活给 ClaudeCenter Worker
- `scripts/kdl/*.py` —— 灌库/验收工具链；`src/dws_agent/kdl/dws_read.py` `DwsReader` —— dws 只读封装
- `scripts/ops/ks_logs.py` —— 生产日志只读查询（绕 kubectl RBAC，走 KubeSphere API）
- `scripts/ops/feedback_patrol.py` —— GitHub feedback issue 巡检 → 钉钉通知（launchd 每小时跑）
- `scripts/docs/render_design_html.py` —— 设计文档 md→html 幂等渲染
- 本地 subagents：`Explore` / `Plan` / `uat-deploy` / `uat-verify` / `prod-verify`
- 阶段 0 Executor + PolicyGate + confirm_token + dws-shim —— 安全动作底座
