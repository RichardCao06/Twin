# Agent 编排与执行 · 从 Intent 到落地

> 项目定位见 [dws-agent-设计方案.md](dws-agent-设计方案.md)、知识库子系统见 [kdl-知识库.md](kdl-知识库.md)。本文档专门讲**任务从"起意"到"真的落地"这条编排链**——PolicyGate/Executor/dws-shim 三件套怎么协作、ClaudeCenter Worker 怎么被派活、本地 subagent 怎么被派活、跨这几种执行形态如何共用同一套安全底座。
> 最后更新：2026-07-06。

---

## 目录

- [1. 定位与硬约束](#1-定位与硬约束)
- [2. 6 道关卡的实际实现（映射到模块）](#2-6-道关卡的实际实现映射到模块)
- [3. ActionIntent 契约与 confirm_token 铸印](#3-actionintent-契约与-confirm_token-铸印)
- [4. R-axis 风险分级：policy.yaml 实际规则](#4-r-axis-风险分级policyyaml-实际规则)
- [5. 派活到 ClaudeCenter Worker（远程外包）](#5-派活到-claudecenter-worker远程外包)
- [6. 派活到本地 subagent（Claude Code 子代理）](#6-派活到本地-subagentclaude-code-子代理)
- [7. 辅助工具：ks_logs / feedback 巡检 / render_design_html 等](#7-辅助工具ks_logs--feedback-巡检--render_design_html-等)
- [8. 典型工作日全流程（一次真实链路）](#8-典型工作日全流程一次真实链路)
- [9. 什么没做（推后 / 保留扩展点）](#9-什么没做推后--保留扩展点)

---

## 1. 定位与硬约束

**Agent 编排层** 是 DWS-Agent 项目里"从思考侧到落地侧"的胶水层。它承担 3 类工作：

1. **本地动作落地**——分诊 Agent 产 ActionIntent → PolicyGate 判级 → Executor 落地（发钉钉消息、跑 `dws` 只读命令等）
2. **派活给外部 Worker**——把复杂开发任务打包成 ClaudeCenter draft，你 review → publish → 远程 Worker 认领执行（提 PR）
3. **派活给本地 subagent**——把机械环节交给 Claude Code 的 subagent（`uat-deploy`/`uat-verify`/`prod-verify`/`Explore`/`Plan`），主 Claude 只做协调

**四条硬约束**（贯穿这三类工作）：

| # | 约束 | 落地 |
|---|---|---|
| 1 | **单一收口**：所有"思考侧 → 落地侧"的动作走 **PolicyGate 一个 chokepoint** | `policy/gate.py` `PolicyGate.evaluate(intent)` |
| 2 | **默认拒绝**：不在 R0 只读白名单里的一律要 `confirm_token`；不认识的子命令按 R2 拦 | `policy/policy.yaml` `defaults.deny_level: R2` |
| 3 | **物理隔离**：无 `confirm_token` 时通不过 `dws-shim` 层——即使有人绕过 Executor 直接 exec dws，shim 也拦 | `executor/shim.py`（独立 subprocess，重算 argv hash） |
| 4 | **不可绕过审计**：每一次判决 / 确认 / 执行 / 拒绝都落 `audit/audit-YYYYMMDD.jsonl` | `store/audit.py`（append-only + `ts/seq/pid`） |

---

## 2. 6 道关卡的实际实现（映射到模块）

任何一个"想干什么"从起意到真正落地，都要过 6 道关卡：

```
关卡 5  Intent      ActionIntent JSON（"我想干这个"的申请单）
   ↓
关卡 4  Knowledge   KDL 检索（如需引证据，见 kdl-知识库.md）
   ↓
关卡 3  Gate        PolicyGate 判 R0/R1/R2/R3 + confirm_token 铸印/核验
   ↓
关卡 2  Executor    无 LLM 出纳，接 ActionIntent + confirm_token → 落地
   ↓
关卡 1  Shim        dws-shim 独立进程再核 argv hash + DWS_GATE_TOKEN
   ↓
关卡 0  Audit       全量 JSONL 流水，含判决 / 确认 / 执行 / 拒绝
```

### 模块映射

| 关卡 | 组件 | 源码位置 | 关键实现细节 |
|---|---|---|---|
| **L5 Intent** | ActionIntent 契约 | `src/dws_agent/contracts/action_intent.schema.json` | JSON Schema 定义了 `action_id` / `argv` / `semantic_labels` / `reason` 等字段 |
| **L4 Knowledge** | KDL | `src/dws_agent/kdl/` | 见 [kdl-知识库.md](kdl-知识库.md)。检索纯只读，产带出处的候选片段供起草 |
| **L3 Gate** | PolicyGate + confirm | `src/dws_agent/policy/{gate,classifier,confirm,loader}.py` + `policy.yaml` | `PolicyGate.evaluate()` 是唯一收口；`confirm.issue()` / `confirm.verify()` 处理印章 |
| **L2 Executor** | 主执行器 | `src/dws_agent/executor/{executor,inbox,refresh_guard,_argvutil}.py` | 消费 ActionIntent，铸 per-invocation `DWS_GATE_TOKEN`，subprocess exec shim |
| **L1 Shim** | dws-shim | `src/dws_agent/executor/shim.py` | 独立 subprocess；重算 argv hash；无 token 对 R0 放行、对写命令 `exit 1` |
| **L0 Audit** | JSONL 审计 | `src/dws_agent/store/audit.py` | 单例 append-only；`ts/seq/pid` 三元组防篡改 |

### 判决相关的辅助约束

- **`--yes` 不参与判级**：`_argvutil.normalize_argv` 在算 hash 前就把 `--yes/-y` 剥掉了。这条防止未来 LLM 学到"多加个 `--yes` 更容易过闸门"这类奇怪捷径。
- **默认拒绝**：`policy.yaml` `defaults.deny_level: R2`——不在 `r0_whitelist` 也不在 `rules` 里的命令一律 R2 待 confirm。
- **`auth export/import/logout/reset` 永久 DENY**：`policy.yaml` `never` 列表，terminal 拒绝，`confirm_token` 也铸不出来。
- **Kill Switch**：闸门检查一个 lockfile，触发即全局 DENY。

---

## 3. ActionIntent 契约与 confirm_token 铸印

### 3.1 ActionIntent

分诊/起草侧产的"申请单"是一份 JSON：

```json
{
  "action_id": "act-2026-07-06-abc123",
  "argv": ["dws", "chat", "message", "send", "--user", "0113...", "--text", "..."],
  "semantic_labels": {
    "contains_promise": false,
    "external_visible": true
  },
  "reason": "回复曹勇 feedback #24 的技术问题",
  "originator": "triage-worker@2026-07-06T15:03:22Z"
}
```

**关键设计：** `argv` 是最终要给 `dws` 命令行的参数原型（含 `dws` 二进制本身作 `argv[0]`）。这样闸门判决时看到的**就是**将来真正要 exec 的东西，杜绝"审的一份、跑的另一份"的偏差。

### 3.2 判级流程（`PolicyGate.evaluate`）

```
validate(argv)                     # argv[0] 必须是 'dws'，否则 R2 硬拒
    → normalize_argv(argv)         # 剥 --yes/-y，规范化重复空格
    → classifier.classify(...)     # 匹配 never / r0_whitelist / rules → R0/R1/R2/R3
    → apply semantic_labels        # 语义标签只能 AND-strict（更严），不能放宽
    → check Kill Switch            # 触发 → 全 DENY
    → audit(event='gate_decision') # 落一条审计
    → return GateDecision(
          decision=AUTO | HUMAN_CONFIRM | DENY,
          requires_confirm=bool,
          never=bool,
          argv_norm_sha256=<hash>)
```

**语义标签 AND-strict**：这条约束吸收原设计里的对抗审查 C1——"是否含承诺 / 是否高危"这类语义判定是思考侧的责任，Executor 只消费"已标注的保守标签"当额外的 AND 闸门。举例：`argv` 判到 R1 但 `semantic_labels.contains_promise=True` → 最终 R2/R3 待 confirm。**方向永远是 AUTO → HUMAN_CONFIRM → DENY，不允许反向**。

### 3.3 confirm_token 铸印算法（`policy/confirm.py`）

```
canon             = '\n'.join(normalized_argv)
argv_norm_sha256  = sha256(canon.encode('utf-8')).hexdigest()
payload           = f"{action_id}|{argv_norm_sha256}|{issued_at}|{ttl}"
secret            = Keychain-derived HMAC key (service '<prefix>-confirm')
token             = base64url(hmac_sha256(secret, payload))
```

**铸印规则：**
- **一次性使用**：`ConfirmRecord.used` 首次 verify 后立即置 True，重放拒绝
- **TTL 300s**（`policy.yaml.defaults.confirm_ttl_seconds`）
- **绑 argv hash**：verify 时重算 argv hash 对比，命令内容改一字节 → 失效
- **状态落盘**：`$DWS_AGENT_HOME/state/pending/<action_id>.json`

**verify 拒绝的所有场景**（`confirm.verify`）：
1. `action_id` 不匹配 pending record
2. `argv_norm_sha256` 不匹配（说明命令被改过）
3. TTL 过期
4. 记录已 used
5. HMAC 校验失败（密钥不对或篡改）

### 3.4 dws-shim 层（`executor/shim.py`）

Executor 铸完 token 后，把 token 塞进环境变量 `DWS_GATE_TOKEN`，然后 subprocess exec `dws-shim`。

shim 是**独立进程**，跑一遍：
1. 从自己的 `sys.argv` 重算 `argv_norm_sha256`（**不信父进程传的**）
2. 独立复查 R0 白名单
3. 如果不是 R0：**必须**有 `DWS_GATE_TOKEN` 且 verify 通过，否则 `exit 1` + `shim_deny` 审计
4. 通过后才 exec 真正的 `dws` 二进制（`DWS_AGENT_DWS_BIN`）

**为什么 shim 要独立进程 + 重算 hash？** —— 因为它是"最后一道物理隔离"：即使有人绕过 Executor 直接调 dws，只要 `DWS_GATE_TOKEN` 没铸对（或被别人偷来但 argv 不匹配），shim 就拦。这个"token via env + OS 权限"的隔离比"把命令放进 PATH"的方式强得多。

---

## 4. R-axis 风险分级：policy.yaml 实际规则

`src/dws_agent/policy/policy.yaml` 是 **grounded 到真实 `dws` v1.0.39 命令树**的（每条规则都是 `dws <cmd> --help` 探过的），不是设计文档里的假想命令名。

### 4.1 R0 只读白名单（部分示例）

```yaml
r0_whitelist:
  - { match: ["chat", "message", "list"] }
  - { match: ["chat", "message", "search"] }
  - { match: ["chat", "conversation-info"] }
  - { match: ["oa", "approval", "list-pending"] }
  - { match: ["doc", "read"] }
  - { match: ["doc", "search"] }
  - { match: ["contact", "search"] }
  - { match: ["contact", "get-self"] }
  - ...  # 共 60+ 条，覆盖 chat/oa/todo/calendar/doc/report/minutes/contact/drive/aitable/mail
```

**注意排除项：** `chat message list-direct`（单聊消息拉取）**故意不在 R0**——落进单聊硬过滤的白名单管辖，一进入知识流就会被过滤掉。这是隐私边界的一部分。

### 4.2 R3（对外发送 / 审批决策）—— 强制 confirm

```yaml
rules:
  - { prefix: ["chat", "message", "send"],             level: R3 }
  - { prefix: ["chat", "message", "send-by-bot"],      level: R3 }
  - { prefix: ["chat", "message", "reply"],            level: R3 }
  - { prefix: ["chat", "message", "forward"],          level: R3 }
  - { prefix: ["mail", "send"],                        level: R3 }
  - { prefix: ["oa", "approval", "approve"],           level: R3 }
  - { prefix: ["oa", "approval", "reject"],            level: R3 }
  - ...
```

### 4.3 R2（不可逆 / 高影响）—— 强制 confirm

```yaml
rules:
  - { prefix: ["chat", "message", "recall"],           level: R2 }
  - { prefix: ["chat", "group", "dismiss"],            level: R2 }
  - { prefix: ["chat", "group", "transfer-owner"],     level: R2 }
  - { prefix: ["doc", "delete"],                       level: R2 }
  - { prefix: ["doc", "permission"],                   level: R2 }
  - ...
```

### 4.4 R1（可逆写）—— 阶段 0 仍需 confirm；阶段 2 按可验证性放开

```yaml
rules:
  - { prefix: ["todo", "task", "create"],              level: R1 }
  - { prefix: ["calendar", "event", "create"],         level: R1 }
  - { prefix: ["aitable", "record", "create"],         level: R1 }
  - { prefix: ["doc", "create"],                       level: R1 }
```

### 4.5 `never`（terminal DENY，不可 confirm）

```yaml
never:
  - ["auth", "export"]      # 会导出凭证
  - ["auth", "import"]      # 会导入凭证
  - ["auth", "logout"]      # 会清空凭证
  - ["auth", "reset"]       # 会重置凭证
```

其余 `auth *` 子命令归为 R3（`rules` 里 `["auth"]: R3`）。

---

## 5. 派活到 ClaudeCenter Worker（远程外包）

### 5.1 定位

**ClaudeCenter Worker** 是外部服务——你在 Console 里注册项目（关联 GitHub 仓库 + Worker 本地路径），把一份"带完整验收标准的 draft task"派进 Console，某个 Worker 认领任务后：
- 起草代码改动
- 跑测试
- 提 PR 到目标分支

Worker 本身是完全独立的 Claude Code 实例，不共享 DWS-Agent 的 `~/.claude` 或 `~/.dws`。

### 5.2 REST 端点（`claudecenter/client.py`）

```
POST  /api/auth/login    {username,password}                    → 200 + Set-Cookie 会话
GET   /api/projects                                              → 200 {projects:[...]}
POST  /api/tasks         {projectId,title,description,...}       → 201 {task}（status=draft）
PATCH /api/tasks/{id}    {action:"publish"}                      → 200 {task}（draft→pending）
```

配置来自环境变量：
- `CLAUDE_CENTER_URL` —— Console 地址（例：`http://127.0.0.1:3000`）
- `CLAUDE_CENTER_USER` / `CLAUDE_CENTER_PASSWORD` —— **建议专用 publisher 账号，别用 admin**

### 5.3 CLI 子命令（`claudecenter/cli.py`）

```
dws-agent task projects
    → 列出可派活的项目

dws-agent task create --project P --title T --description-file F [--base-branch B]
                      [--submit-mode {pr,push}] [--auto-reply] [--model M] [--no-test-gate]
    → 建 draft（草稿不被认领、不执行，无害）
    → 打印 draft id，等你 review

dws-agent task publish <id>
    → 你显式确认后发布（draft → pending）
    → Worker 认领执行
```

**为什么拆两步？** 因为 `publish` 本质是"钉钉消息 → 自动改代码 → 提 PR"这条链的触发点——是一个 R3 级别的动作。拆成 `create`（无害）和 `publish`（触发）两步，让你在 review draft 内容后才按下"扳机"。

### 5.4 自动追加的"测试门禁"

`cli.py:54` 定义了 `TEST_GATE_CLAUSE`——每次 `task create` **自动**在 description 末尾追加：

```
---
## ✅ 测试门禁（提 PR 前必须满足；不满足不要提 PR）
1. 为本次改动写 mock 依赖的单元/组件测试...
2. 跑通项目测试套件并保证全绿...
3. PR 的 Test Plan 里"运行时/交互验证"用上述 mock 单测覆盖...
4. 真全链路 E2E（需完整部署环境）可标注"部署后验证"...
```

这段是"防 Worker 提未验证 PR"的兜底。**Worker 看到这段就必须先跑 mock 单测**——如果 Worker 说"环境不齐没测"，是不接受的。**`--no-test-gate` 关掉**（纯文档 task 用）。

### 5.5 群进度播报（`announce.py`）

`publish` 后可选地在"研发群"发一条模板化播报：**"某某 task 已开始 Worker 执行，负责人：XXX，PR 预计到达 [仓库]"**。播报走阶段 0 的完整审计通道（分配 `AI-announce-<uuid>` action_id），标注"非本人发言"。

**可关**：
- 全局 env `DWS_AGENT_ANNOUNCE=0`
- 单次 `publish --no-announce`
- 播报失败**不影响** task 发布——发布是主线，播报是通知，两者独立

### 5.6 Worker 完成后的验证

**Worker 只到"提 PR"就止**——它不合并 PR、不部署。合并 / 部署由你按下确认，或派本地 subagent 做（见 §6）。

---

## 6. 派活到本地 subagent（Claude Code 子代理）

### 6.1 subagent 概念

Claude Code 允许主 Claude 派活给"子代理"——每个 subagent 是一个受限的 Claude 实例，只能用一组指定的工具、执行一件专门的事，结束时把结果返回给主 Claude。**主 Claude 只做协调**，具体的机械环节交给专门的 subagent 干。

### 6.2 当前使用的 subagent 清单

| subagent | 定位 | 可用工具 | 触发场景 |
|---|---|---|---|
| **`Explore`** | 只读跨仓库摸清代码位置 / 搜关键字 | 除写入类之外的所有 | 需要研究：某功能在代码哪里、有哪些相关文件 |
| **`Plan`** | 只读拟一份实施计划 | 除写入类之外的所有 | 复杂改动前先要设计一个逐步方案 |
| **`uat-deploy`** | 本地 build 镜像 + push registry | 全部工具 | CI 自动部署坏掉、需应急上线 |
| **`uat-verify`** | Playwright E2E 复现场景 + 截图判验证 | 全部工具（含 Playwright MCP） | 部署后要 E2E 复测某 PR / 场景 |
| **`prod-verify`** | 用测试账号在生产跑非破坏性 case + 主动监控日志 | 全部工具 | uat 不足以复现，需在生产验证 |
| **`code-reviewer`** | 独立视角复审 diff / migration | Bash / Read / WebFetch | 想拿"没参与该 PR 讨论"的独立意见 |
| **`general-purpose`** | 通用型 | 全部 | 不属于任何专项的多步任务 |

**共同硬约束**：
- 主 Claude 派活时给一份"自包含"的 prompt（subagent 不知道会话上下文）
- 主 Claude 明确说要研究还是要改代码
- **subagent 出报告 / 出草稿，但绝不自动合并 PR / 自动提 issue** —— 那是主 Claude 或本人的动作

### 6.3 subagent 与 Worker 的关键差异

| 维度 | 本地 subagent | ClaudeCenter Worker |
|---|---|---|
| 位置 | 同一台 mac，同一份 `~/.claude` | 独立机器，独立 Claude 实例 |
| 生命周期 | 主 Claude 一次派活一次，秒级完成 | 长任务（几分钟到数小时），异步 |
| 结果返回 | 直接返回 tool result 给主 Claude | 提 PR + 状态回写 Console |
| 适合的活 | 机械但"看得见立刻做完"的（Playwright E2E、build 镜像、查日志） | 长周期开发（一整个功能、一次重构） |
| 派活成本 | 一条 `Agent(...)` 工具调用 | 一次 `dws-agent task create` + `publish` |
| 你的介入点 | 结果返回后你继续用 | Worker 提 PR 后你 review + 合并 |

### 6.4 subagent 的"信任边界"

subagent 本质上仍是**会思考的 LLM**——所以它跟主 Claude 一样属于"不可信外包"侧：只能出草稿 / 报告 / 意图，不能突破 PolicyGate 直接动手。

当前 subagent 的动作落地路径：
1. 需要 `dws` 调用（发消息、读文档）—— 走完 PolicyGate + confirm_token（就跟主 Claude 一样）
2. 需要本地 shell（git、docker、kubectl）—— **暂无统一闸门**，依赖 Claude Code 自身的权限模型（sandbox）
3. 需要浏览器操作（Playwright）—— 完全在 subagent 内部，主 Claude 拿不到 session

**未启用的约束**：把 subagent 的 shell 动作也纳入 PolicyGate（例：docker push 到 registry 应该是 R2 级别、rollout 是 R3）。这条属于阶段 3 "双 Agent 编排规范化" 的目标（见 [dws-agent-设计方案.md §4.2](dws-agent-设计方案.md#42-分阶段路线图)）。

---

## 7. 辅助工具：ks_logs / feedback 巡检 / render_design_html 等

除了 Executor + Worker + subagent，还有一批**不进 `dws_agent` 包**的独立脚本（`scripts/`），处理"专项但不属于任何 agent"的活。这些脚本不进 Python 包是为了减少 import chain。

| 脚本 | 用途 | 关键设计 |
|---|---|---|
| `scripts/ops/ks_logs.py` | 查生产日志（KubeSphere API 只读） | kubectl 被 RBAC 挡时用；仅 GET；token 从浏览器复制，不入盘（2h 过期） |
| `scripts/ops/feedback_patrol.sh` + `.py` | 每小时扫 `HiQ-AI/feedback` open issue → 有新的就发钉钉 | 走 launchd `com.dws-agent.feedback-patrol`；PATH 排 anaconda3 在前（`dws send` 依赖 PyYAML）；`_notify()` 返回真实结果，失败不标记 seen |
| `scripts/kdl/kdl_ingest.py` | KDL 候选直入灌库（绕过 Distiller） | 走 `Ingestor.ingest_candidates`；`--repo` 跑 `GitReader.index_repo` 产 CODE 候选 |
| `scripts/kdl/kdl_acceptance.py` | 阶段 1 退出条件验收 | 跑 4 项自动测量（命中/溯源/外发/脱敏） |
| `scripts/kdl/kdl_review.py` | 批量把 DRAFT KU 升到 REVIEWED | 走 `store.set_authority`（禁越级 AUTHORITATIVE） |
| `scripts/kdl/kdl_fetch_docs.py` | 摄取本人钉钉文档 → `/tmp/kdl-ddoc/` | 经 DwsReader 只读通道；产 `manifest.json` 供后续蒸馏做 provenance |
| `scripts/kdl/kdl_dedup_docs.py` | 合并文档候选 → 保证 provenance ref 唯一 | 防 `make_ku_id` 撞键覆盖 |
| `scripts/docs/render_design_html.py` | 渲染 `docs/design/md/*.md → design/html/*.html` | mtime 幂等 + 中文 slugify；见 [dws-agent-设计方案.md](dws-agent-设计方案.md) |

---

## 8. 典型工作日全流程（一次真实链路）

用 2026-07-06 的一次真实工作举例——把 dataset 新增 TIDAS 导出功能这件事从头走到尾：

| # | 动作 | 谁做 | 用了什么 |
|---|---|---|---|
| 1 | CLI 触发："给 dataset 项目加 TIDAS 导出功能" | 你 | 命令行 |
| 2 | 分诊：功能开发，属 dataset 项目，可拆后端+前端 | 主 Claude | KDL 检索 |
| 3 | Explore 摸清后端导出架构 | subagent `Explore` | 只读 |
| 4 | 分诊生成两份 draft task（后端 + 前端），含验收标准 + 不做清单 + fixture 位置 | 主 Claude | 写文件 |
| 5 | `dws-agent task create --project dataset --title ...` × 2 | 主 Claude → ClaudeCenter | `POST /api/tasks` |
| 6 | 你 review 两份 draft → publish | 你 | `dws-agent task publish <id>` |
| 7 | Worker 认领 → 起草代码 → 跑 mock 单测 → 提 PR | 远程 Worker | 独立 Claude 实例 |
| 8 | 你 review PR → base 分支从 main 改成 feature/uat2-base | 你 | `gh pr edit` |
| 9 | 派本地 subagent build uat2 镜像 × 2 | subagent `uat-deploy` | docker buildx，push 天翼云 registry |
| 10 | 你 rollout uat2（`kubectl rollout restart`） | 你 | kubeconfig（只读） |
| 11 | 派本地 subagent 跑 E2E 验收 | subagent `uat-verify` | Playwright + 截图 |
| 12 | E2E 报告出来：前端 3 case 全绿；后端因 uat2 OSS access key 失效 FAIL | subagent → 主 Claude | 报告草稿 |
| 13 | 主 Claude 查 uat2 dataset pod 日志确认根因 | 主 Claude | `kubectl logs`（只读） |
| 14 | 结论回给你：不是 PR 的锅，是 uat2 OSS access key 失效 | 主 Claude | 文本报告 |
| 15 | 顺带发现今天生产也有一起"选不存在版本导空包"的 bug | 你 + 主 Claude | 写复盘或建 task |

**整个链路里的动作分工特点：**
- 主 Claude 负责协调 + 检索 + 分诊 + 起草 draft
- Worker 负责长周期开发（起草代码 + 提 PR）
- 本地 subagent 负责机械但高频的环节（build、E2E）
- 你负责一切不可逆的决定（合并 PR、rollout、上生产）

---

## 9. 什么没做（推后 / 保留扩展点）

| 条目 | 状态 | 何时启用 |
|---|---|---|
| **subagent 的 shell 动作走 PolicyGate** | 未启用——目前 subagent 靠 Claude Code 自身权限模型；PolicyGate 只闸 `dws` 命令 | 阶段 3 "双 Agent 编排规范化"启动时（见 [dws-agent-设计方案.md §4.2](dws-agent-设计方案.md#42-分阶段路线图)） |
| **C 分级 + 出口管控 + 承诺语义检测** | `policy.yaml` `C_axis: { enabled: false }` 已声明扩展点 | 代答规模化后（阶段 4） |
| **W 分级（Worker 动作分级）** | `policy.yaml` `W_axis: { enabled: false }` 已声明扩展点 | 双 Agent 编排规范化后 |
| **Kill Switch 主动触发接口** | 检查 lockfile 已就绪；无独立 CLI 触发 | 出事时手动 `touch $DWS_AGENT_HOME/state/kill_switch` |
| **带外确认通道（DING）** | 设计里 R3 走 DING 二次确认；当前 R3 走同一 confirm_token 通道 | 需要"手机紧急拒绝"场景时；接钉钉 DING API |
| **Case 归并 + 后台 SLA** | 分诊侧的功能，见 [dws-agent-设计方案.md §2.2.5](dws-agent-设计方案.md) | 代答规模化后 |
| **子代理独立验收器** | 目前"验收"就是主 Claude 读报告；无独立 validator sandbox | 阶段 3 |
| **`--auto-reply` 无人值守 publish** | CLI 参数保留，实际生产不建议用 | 单域受控自治阶段 |

---

> **写在最后：** 这条编排链的核心不是"能派多少种活"，是"**无论派什么活，动作落地一定过同一套闸门**"。加入 subagent 时不为它开小门，加 Worker 时也不为它开小门，未来加 IDE 集成 / webhook 时也不会开小门。安全性不会随功能扩展打折——因为每一个新的执行形态都被要求"接进 PolicyGate + Executor + 审计流"这条统一路径。这就是这个系统的复杂度上限能被守住的原因。
