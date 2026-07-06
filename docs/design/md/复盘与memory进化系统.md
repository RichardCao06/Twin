# 复盘与 Memory 进化系统 · 让系统自己变聪明

> 项目三根柱子之一（另两根：知识库 KDL、Agent 编排）。本文档讲：怎么把"每次踩坑"变成"下次自动生效"——从事件发生 → 复盘沉淀 → 提炼 memory → 反哺决策的闭环设计。
> 最后更新：2026-07-06。部分设计已落地（memory + 复盘手写 + 会话自动加载），部分尚未启用（自动复盘草稿、专项扫描、失效检测）。**按闭环设计写全，标注实施状态。**

---

## 目录

- [1. 定位与硬约束](#1-定位与硬约束)
- [2. 三层记忆载体：session / docs/retro / memory](#2-三层记忆载体session--docsretro--memory)
- [3. 事件与触发点：什么时候记什么](#3-事件与触发点什么时候记什么)
- [4. Memory 条目的契约（4 种类型 + 结构）](#4-memory-条目的契约4-种类型--结构)
- [5. 复盘文档的结构与写作纪律](#5-复盘文档的结构与写作纪律)
- [6. 从复盘到 memory：提炼流程（当前 + 未来）](#6-从复盘到-memory提炼流程当前--未来)
- [7. Memory 如何反哺决策：下次会话自动加载](#7-memory-如何反哺决策下次会话自动加载)
- [8. 自我进化的闭环全图](#8-自我进化的闭环全图)
- [9. 未启用扩展点（推后 / 保留）](#9-未启用扩展点推后--保留)
- [附录 A. 已沉淀 memory 条目清单（2026-07 快照）](#附录-a-已沉淀-memory-条目清单2026-07-快照)
- [附录 B. 复盘文档模板](#附录-b-复盘文档模板)

---

## 1. 定位与硬约束

**进化系统** 是这个平台里唯一能让"系统本身变聪明"的机制——KDL 让 agent"记住过去的代码和文档"，编排层让 agent"能派活干活"，但**只有复盘 + memory 让 agent"从错误里学到不再犯"**。

**5 条硬约束**（贯穿整个进化循环）：

| # | 约束 | 落地位置 |
|---|---|---|
| 1 | **只落数据，不落代码**（避免"AI 修改自己的判断逻辑"这类失控） | 所有沉淀都是 markdown / yaml / json，从不改 Python 源码 |
| 2 | **人工确认才升 "永久规则"**（对应 KDL 的 DRAFT → REVIEWED → AUTHORITATIVE） | Memory 条目由本人手写落盘；未来自动提炼要走 draft 状态待复核 |
| 3 | **每次踩坑都要留证据**——不能只留"以后要小心"这类空话 | 每条 memory 必须带 `**Why:**` 段（具体事故 + 数据）+ `**How to apply:**` 段（何时该触发） |
| 4 | **反哺路径不可绕过**——memory 必须在会话开头自动加载，不允许"想起来才用" | 通过 CLAUDE.md 顶部 + `MEMORY.md` 索引强制加载 |
| 5 | **失效 memory 要能被发现**——不能"过了半年这条规则已经不适用了但还在触发" | 见 §9 未启用扩展点 |

---

## 2. 三层记忆载体：session / docs/retro / memory

系统里的"记忆"按**时效性 + 触达范围**分三层：

```
        瞬时                                          永久
     ────────────────────────────────────────────────────────
     ①  Session context             会话内             一次会话结束就消失
                    │  (值得留的)
                    ▼
     ②  docs/retro/复盘-YYYY-MM-DD.md   项目仓库内     人可搜、git blame、跨会话可读
                    │  (值得让"下次触发相似情境时自动生效"的)
                    ▼
     ③  memory/*.md                    全局           每次会话开头自动加载,cross-session 生效
```

### 2.1 三层的定位

| 层 | 存哪 | 生命周期 | 谁能读 | 何时用 |
|---|---|---|---|---|
| **① Session** | Claude 会话上下文 | 一次会话 | 只有当前 Claude 实例 | 对话进行中 |
| **② `docs/retro/`** | 项目仓库 | 永久（进 git） | 未来的你、任何读文档的人、未来的 Claude 会话（通过读文件） | 需要"回忆某次事故的全貌"时 |
| **③ `memory/*.md`** | `~/.claude/projects/<slug>/memory/` | 永久（不进 git） | 每次会话开头自动加载到当前 Claude 上下文 | **自动生效**——不需要你想起来 |

### 2.2 关键差异：memory 是"自动触发"的

`docs/retro/` 是"我想起来去查"——你或未来的 Claude 得主动记得"上次好像踩过类似的坑"。

`memory/*.md` 是"下次自动上桌"——每次开新会话时全部 memory 被自动 prepend 进 Claude 的系统提示，Claude **不需要想起来**就已经带着这些规则做决策。

**这是这两层的本质分工**：docs/retro 是**档案**，memory 是**下意识**。

---

## 3. 事件与触发点：什么时候记什么

不是所有事情都值得沉淀。规则如下：

### 3.1 触发写复盘（`docs/retro/`）的场景

| 场景 | 是否写复盘 |
|---|---|
| 生产事故 / 上线事故 | **必写** |
| 一次多仓库联合上线 | **必写**（对应 §5"输出物清单"结构） |
| 排查一个 N 层套娃的复杂 bug | **必写**（提炼"链式根因"这种规律） |
| 发现一个"看起来在跑但实际全部失效"的自动化 bug | **必写**（silent failure 值钱） |
| 单纯改代码、走流程、E2E 通过 | 不写 |
| 一次会话解决了小问题 | 不写 |

**核心判据：** 有没有"下次别人也会踩"的普适教训 or "以为在跑其实没在跑"的反直觉发现。

### 3.2 触发提炼 memory 的场景

在复盘里发现下面任一情况：

1. **规则型教训**：能提炼出"下次遇到 X 应该 Y"的一句话规则（例：`E2E 必须截图才算通过`）
2. **反模式识别**：发现一类共性坑法（例：`silent catch 是 bug 温床`）
3. **判断偏差校准**：某类判断以前信错了信源（例：`不信前人根因`）
4. **工具用法沉淀**：某个复杂工具的正确用法（例：`生产 PG 直连`）

不提炼 memory 的情况：一次性事故的具体 fix、代码位置、commit sha——这些查 git 就行。

### 3.3 触发实现改动的场景

复盘发现的问题里，能立刻做工程改动的：

| 复盘发现 | 改动落到哪 |
|---|---|
| 现有代码有 silent catch pattern | 独立 commit 修复 + 加单测（对应改进措施第 4 条） |
| 现有工具 CLI 缺一个 flag | 加 flag + 更新 CLAUDE.md |
| 现有子代理 prompt 有漏洞 | 改子代理的 system prompt |
| 新增子代理才能覆盖某场景 | 建新 subagent |
| 现有 policy.yaml 缺一条规则 | 加规则 |
| 现有 KDL 分类被证明不合理 | 更新 [kdl-知识库.md](kdl-知识库.md) §4 |

---

## 4. Memory 条目的契约（4 种类型 + 结构）

### 4.1 4 种类型

从 CLAUDE.md 顶部"auto memory"约定：

| 类型 | 定位 | 举例 |
|---|---|---|
| **user** | 用户的角色、偏好、知识背景 | "用户偏好简短技术回复、不接受长段解释" |
| **feedback** | 用户明确/隐含给过的行为指导（做过 / 不做 / 怎么做） | `verify-by-final-artifact`、`e2e-必须截图才算通过` |
| **project** | 当前项目的进展、决策、上下文（非代码可derive 的部分） | `mvp-代发链路`、`kdl-ingestion-design-status` |
| **reference** | 外部系统的指向（哪里能查到什么） | `prod-pg-direct-access`、`prod-verify-log` |

### 4.2 单条 memory 的文件结构

**文件路径：** `~/.claude/projects/<slug>/memory/<kebab-case-slug>.md`

**Frontmatter + body：**

```markdown
---
name: silent-catch-hides-bug
description: 业务关键路径的 catch + log + 继续往下 = 隐藏 bug 反模式;
             必须让任务级状态反映失败(failTask/计数+判 FAIL)
metadata:
  node_type: memory
  type: feedback
  originSessionId: 950002d0-...
---

业务关键路径里 `catch (Exception e) { log.error(...); }` 但不让
任务级状态反映失败 = **隐藏 bug 反模式**。代码 review 看到这种
pattern 必须问"这层失败用户/任务级别能感知吗?"

特别危险的位置:
- 异步 fan-out 子线程的 catch
- 批处理循环的 catch (一条失败继续下一条)
- 任何"为了不影响主流程"的 catch

**Why:** 06-29 dataset 导出 bug,`processBatch` fan-out 工作线程的
catch 只打 `log.error("syncRun, import failed, Thread:...")` 不抛、
不标 task FAILED。结果 203 个 process 全部 NPE 失败,但外部看:
- `tm_task.status=COMPLETED` ✓
- OSS zip 上传成功 ✓
- 用户消息"导出完成" ✓

**How to apply:** 代码 review、bug 排查、写异步代码时。
```

### 4.3 3 段必填的正文结构

- **规则本体**（第一段）：一句话说清"下次遇到 X 应该 Y"
- **`**Why:**` 段**：具体事故 / 数据支撑——**没有 Why 的 memory 是空话**
- **`**How to apply:**` 段**：什么场景该触发这条规则

**`Why:` 是硬约束**——因为 6 个月后你自己都不记得为啥定这条规则时，Why 段就是唯一能判断"这条规则是不是还适用"的证据。没有 Why 的 memory 半年就成了"祖传玄学"。

### 4.4 索引：`MEMORY.md`

`memory/` 目录下的 `MEMORY.md` 是所有 memory 的索引文件，每条一行：

```markdown
- [规则短标题](file-slug.md) — 一句话核心 + 关键触发场景
```

**格式约束（CLAUDE.md 定义）：** 每行 <150 字符；总条数按 200 行截断；不要把内容直接写进 MEMORY.md（那是索引不是 memory）。

会话开头 Claude 会自动读到这个 index，看到相关的就点开细节文件；不相关的只留一个索引进上下文，成本可控。

---

## 5. 复盘文档的结构与写作纪律

### 5.1 文件命名与位置

`docs/retro/复盘-YYYY-MM-DD.md`。日期以复盘写作时间为准（不是事故发生时间）。

### 5.2 4 段结构（现行模板，来自 `docs/retro/复盘-2026-07-03.md`）

```markdown
# 复盘 · YYYY-MM-DD ~ YYYY-MM-DD

> 一句话：**整个复盘期最有价值的一句提炼**。

## 输出物清单
（表格：PR / 上线镜像 / 事故起数 / 数据 SQL / 上线清单文档 / 巡检修复）

## 做了什么
（分子章节，按"1. 一次多仓库发布 / 2. N 起事故 / 3. 抽取失联功能 / 
 4. 发现并修复 XX 静默失效"分开写）

## 过程中暴露的问题
（编号列表——每条一个"暴露出的系统性问题"，不是"某代码 bug"）

## 可以改进的措施
（编号列表——每条对应一个能落到 PR / memory / prompt / policy 的具体动作）
```

### 5.3 写作纪律（关键）

- **重点在"暴露了什么问题"，不是"我做了什么"**——流水账没价值
- **优先记 "看起来正常但其实没在跑" 的类型**（silent failure）
- **每条"暴露的问题"必须有具体证据**（commit hash / 数据 SQL / 日志片段 / 截图）
- **每条"改进措施"必须能被验证**（"下次注意"不是措施，"加一条 memory + 扫一遍现有代码里的 XX pattern" 是措施）

### 5.4 复盘 → PR / memory / 代码改动的分派

复盘尾部的"改进措施"每一条要标注去向：

| 类别 | 去向 | 举例（2026-07-03 复盘的实际分派） |
|---|---|---|
| 普适规则 | 提炼进 `memory/*.md` | 迁移脚本版本号靠人工数序 → memory 条：`使用时间戳版本号` |
| 具体代码修复 | 独立 PR | 静默 catch 专项扫描 → 扫全部业务代码里的 `check=False` |
| 工具能力欠缺 | 新增脚本 / 子代理 | 生产日志 token 频繁过期 → 沉淀成 `ks_logs.py` 独立工具 |
| 流程约定 | 更新 CLAUDE.md 或 skill 提示词 | "PR 已合并"要显式检查 base branch | 更新 skill 提示词 |

---

## 6. 从复盘到 memory：提炼流程（当前 + 未来）

### 6.1 当前状态（手工）

```
                  事故发生 / 会话踩坑
                        │
                        ▼
                手写 docs/retro/复盘-XX.md  (会话内起草,你 review 后 commit)
                        │
                        ▼
              识别哪些是"普适规则型"教训
                        │
                        ▼
                手写 memory/xxx.md  (frontmatter + Why + How to apply)
                        │
                        ▼
                更新 memory/MEMORY.md 索引  (加一行)
                        │
                        ▼
              下次会话自动加载 → 生效
```

**目前 100% 靠人**——每次复盘完你告诉 Claude "把这条提炼成 memory"，Claude 起草 markdown 文件，你复核后落盘。

### 6.2 未启用的自动化（阶段 4 目标）

**目标：事故触发时自动起草复盘草稿 + 候选 memory 条目，你只做复核**。

技术上可以做但目前手工可控，未做。真做的话架构是：

```
              PolicyGate 拒绝 / Executor 异常 / 复盘信号触发
                        │
                        ▼
            分诊 Agent 收到事件 → 起草复盘候选 (draft)
                        │
                        ▼
            KDL 检索相关历史事故 → 匹配"是不是老坑重犯"
                        │
                        ▼
                  你 review draft → publish
                        │
                ┌───────┴───────┐
                ▼               ▼
        docs/retro/落盘     memory 候选草稿 → 你确认后落盘
```

**为什么现在不做：** 每周踩坑数少，手工提炼 + 我们迭代设计（此文档就是例子）更值得。等踩坑规模化后再上。

---

## 7. Memory 如何反哺决策：下次会话自动加载

### 7.1 加载机制（已实施）

CLAUDE.md 顶部的 `# auto memory` 段是**每次新会话自动 prepend 进 Claude 系统提示**的一部分。它告诉 Claude：

- Memory 目录位置：`/Users/shujudagongren/.claude/projects/<slug>/memory/`
- 4 种类型（user / feedback / project / reference）
- 何时读 memory：相关时、用户明确要求时
- 何时更新 memory：明显的规则型教训、用户明确要求时

CLAUDE.md 里同时嵌入了 `memory/MEMORY.md` 的**完整内容**（200 行以内）——这样 Claude 一开始就知道所有 memory 的一句话摘要，需要细节时按 slug 打开对应文件。

### 7.2 触发方式：3 种

1. **相关时自动触发**——Claude 在做决策前意识到"这场景和某条 memory 相关"，就点开细节文件
2. **用户明确要求**——"你之前不是有条规则说 XX 吗，为什么这次没遵守？"→ 直接读对应 memory
3. **矛盾发现时**——Claude 在 tool 结果里发现的现实和某条 memory 描述不一致 → 应该**更新 memory** 而非继续用旧规则

### 7.3 用后校验：现实优先

CLAUDE.md 的 `## Before recommending from memory` 段有一条硬规则：

> Memory 记的东西可能已经过期。基于 memory 的推荐**必须先在当前代码里验证一遍**才能给用户。

例：memory 说"XX 函数在 YY 文件"——推荐前先 `grep` 一下确认还在。这条约束防止 memory 变"祖传玄学"。

---

## 8. 自我进化的闭环全图

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  ①  你或 Claude 在会话里做事                                       │
│           │                                                     │
│           │  踩坑 / 事故 / 发现反模式                                │
│           ▼                                                     │
│  ②  写 docs/retro/复盘-YYYY-MM-DD.md                              │
│           │  (输出物 / 做了什么 / 暴露的问题 / 改进措施)                │
│           │                                                     │
│           ▼  分派"改进措施"                                        │
│      ┌────┴────────────────────────┬───────────────┐            │
│      ▼                             ▼               ▼            │
│  ③a 独立 PR                    ③b memory/xxx.md  ③c 更新       │
│     修复代码 pattern             提炼规则          CLAUDE.md    │
│     (合到 main)                  (frontmatter+Why)  / 子代理提示词│
│                                    │                            │
│                                    ▼                            │
│                            更新 MEMORY.md 索引                     │
│                                    │                            │
│                                    ▼                            │
│  ④ 下次会话开始                                                   │
│      CLAUDE.md 自动加载  →  memory 全部索引进 Claude 系统提示        │
│                                    │                            │
│                                    ▼                            │
│  ⑤ Claude 在做决策前                                              │
│      "这场景是不是和某条 memory 相关?" → 命中 → 按规则行动            │
│      不再犯上次的错                                                │
│           │                                                     │
│           ▼                                                     │
│      如果又犯了 (说明规则有漏洞) → 回到 ① 继续复盘                     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 8.1 闭环里 4 个关键"物理隔离"

- **人工确认才能升 memory**——即使 Claude 会话内起草了候选，落盘之前你要 review（对齐 KDL 的 DRAFT → REVIEWED → AUTHORITATIVE）
- **只落数据不落代码**——sonderline：`memory/*.md`、`docs/retro/*.md`、`CLAUDE.md` 是数据；Python 源码是代码。**AI 只能改前者，代码改动必须经 PR 流程**
- **反哺路径不可绕过**——CLAUDE.md 里的 `# auto memory` 段是标准 Claude Code 特性，会话开始时强制加载
- **加载后仍要现实校验**——`Before recommending from memory` 硬约束避免玄学

### 8.2 从 2026-06 到 07 的实际证据

近 6 周沉淀轨迹（附录 A）：

| 阶段 | Memory 条目数 | 复盘文档数 |
|---|---|---|
| 2026-06-23 | 4 条（KDL 基础） | 1 篇 |
| 2026-06-30 | 12 条 | 4 篇 |
| 2026-07-06 | 19 条 | 5 篇 |

**平均每篇复盘产出 3-4 条 memory 条目**——不是每条改进措施都升 memory（因为多数是具体代码修复），但确实每次复盘都在扩充"下意识规则集"。

---

## 9. 未启用扩展点（推后 / 保留）

按闭环设计写全，标注当前是否实施：

| 条目 | 状态 | 何时启用 |
|---|---|---|
| **事故自动触发复盘草稿** | **未启用** · 目前 100% 手工起草 | 事故规模化（每周 3+ 次同类）时值得做 |
| **候选 memory 自动提炼** | **未启用** · Claude 会话内起草，人复核落盘 | 同上——手工可控时不做 |
| **memory 失效检测** | **未启用** · 目前靠"用之前再 grep 验证"这条口头规则 | 需要写一个 CI 任务定期扫 memory 里提到的 file path / function name 是否还存在；对不上的产生"待复核"标记 |
| **memory 冲突检测** | **未启用** · 目前靠人写的时候察觉 | 需要一个跨 memory 语义比对——2 条 memory 说了矛盾的事就应该产 warn |
| **memory 使用率追踪** | **未启用** · 无法知道哪条 memory 半年没被触发过 | 需要每次 memory 命中都埋点，长期无命中的 → 待复核 |
| **失效规则的降级路径**（对齐 KDL 的 REVIEWED + STALE） | **未启用** · 目前 memory 只有 "存在 / 不存在" 二态 | 需要 memory frontmatter 加 `authority: DRAFT/REVIEWED/AUTHORITATIVE` + `last_verified_at`  |
| **跨项目 memory 共享** | **部分实施** · 全局 `~/.claude/CLAUDE.md` 已有全局规则；项目级 memory 仍独立 | 需要一层"哪些 memory 该升到全局"的判断 |
| **专项扫描（silent catch 类 pattern）** | **部分实施** · 每次复盘识别一个 pattern 后做一次专项扫描（手工）| 需要固化成 dws-agent CLI + 定期跑 |
| **复盘完整度检查** | **未启用** · 目前靠模板约束 4 段结构 | 需要 CI 检查每份复盘有没有"暴露的问题"章节 |

---

## 附录 A. 已沉淀 memory 条目清单（2026-07 快照）

按类型分组，摘要来自 `MEMORY.md`：

**feedback 类（12 条）**——用户行为指导：
- `verify-tool-output-before-claiming` · 别把预期输出当实际结果
- `verify-runtime-by-testing` · 运行时判断先实测再下结论
- `e2e-必须截图才算通过` · 每个 case 必须浏览器截图证据才能说 PASS
- `silent-catch-hides-bug` · 业务路径 catch + log + 继续 = 隐藏 bug 反模式
- `verify-by-final-artifact` · PASS 必须基于用户视角产物本身
- `chain-root-cause-think-next-layer` · 复杂 bug 经常 N 层套娃
- `local-reproducer-over-remote-loop` · 3 次以上远程 build/push 循环 = 停下来建本地
- `dont-trust-prior-root-cause` · commit/issue/PR 的"根因"是最佳猜测
- `square-auth-kick` · 三端顶下线行为
- `cortex-sso-device` · cortex 接同套 dataset-sso
- `mvp-代发链路` · MVP 已跑通
- `claudecenter-task-bridge` · 任务桥接已跑通

**project 类（4 条）**——项目状态：
- `kdl-knowledge-sources` · KDL 知识源
- `kdl-ingestion-design-status` · KDL 已灌入现状
- `uat-部署链路` · 手动部署 / registry 映射
- `uat-verify-e2e-agent` · uat-verify agent 定位

**reference 类（3 条）**——外部系统指针：
- `prod-verify-log` · prod-verify + ks_logs.py
- `prod-pg-direct-access` · 生产 PG 直连

---

## 附录 B. 复盘文档模板

```markdown
# 复盘 · YYYY-MM-DD ~ YYYY-MM-DD

> 一句话：**这段时间最有价值的一句提炼**。

---

## 输出物清单

| 类型 | 数量 | 详情 |
|---|---|---|
| 生产 PR | N | ... |
| 生产镜像 | N | ... |
| 生产事故 | N 起 | ... |
| 数据回填 SQL | N 类 | ... |
| 上线清单文档 | N | ... |
| 运维 bug 修复 | N | ... |

---

## 做了什么

### 1. <一件事的标题>
（该事的经过 + 关键数据 + 涉及的 PR / commit）

### 2. <另一件事的标题>
...

---

## 过程中暴露的问题

### 1. <问题的一句话>
（具体证据：commit hash / SQL / 日志 / 截图）
（为什么是"系统性问题"而非"偶发 bug"）

### 2. ...

---

## 可以改进的措施

1. **<措施标题>** —— <具体动作，能被验证>
   - 去向：memory / PR / prompt 更新 / policy.yaml
2. ...
```

---

> **写在最后：** 这个循环的核心不是"记录很多东西"——是**"确保下次遇到相似情境时，规则已经内化到决策入口"**。docs/retro 是档案（人查的），memory 是下意识（Claude 自动带上桌的）。两者分工不同、载体不同、生命周期不同。写复盘的价值不在写完那一刻，而在半年后你早就忘了这事 memory 却在恰当时刻提醒当前 Claude"上次是这么栽的"——这才是"系统在变聪明"的物理含义。
