# Twin

**一个面向「个人 + 项目知识 / 多 Agent 编排 / 持续复盘」的工程助理平台。**

在两三个项目并行的时候，日常瓶颈往往不是"这段代码怎么写"，而是——知识散落各处、手动派活成本高、踩过的坑会再踩。Twin 想做的是把 **知识 + 编排 + 复盘** 这三个环节闭合起来：让重复动作和事实检索外包给 agent，把注意力留给需要判断的地方。

> 前身叫 `dws-agent`——从"钉钉分身"一步步长成通用的 AI-agent 编排底座。核心 CLI 至今保留 `dws-agent` 命名。

---

## 三根柱子

### 🧠 Knowledge · 知识蒸馏层 (KDL)

把工作流里能碰到的所有资料——代码仓库、聊天记录、issue、文档、复盘——统一蒸馏成 **KU (Knowledge Unit)**，落到本地 SQLite + 加密存储。查、检索、代答，都从这里出。

- **强绑 provenance**：无溯源即拒收（即使入库也锁 `DRAFT`，永不支撑代答）
- **入库即脱敏 + 污点传播**（`CLEAN` / `INTERNAL` / `SENSITIVE`），未核 `public_ok` 不入可对外路径
- **CODE 类绑 commit**：代码变了自动 `STALE`，从检索结果里剔除
- **六条 abstain 规则**：证据不够就明确说"不知道"，绝不编——包括无命中 / 全 DRAFT / 全 EXPIRED / 证据不可达 / 相关度双低 / 承诺口径

### 🎼 Orchestration · 编排层

**无 LLM 确定性 Executor + PolicyGate 安全底座**——所有对外部环境的写操作都要走：policy 判级 + 一次性 confirm_token + 全量审计。

- **default-deny + R0 只读白名单**：白名单外一律需 `confirm_token`（绑 `sha256(argv) + actionId + TTL`，一次性使用）
- **判级不看 `--yes`**：`--yes` 只在业务命令里，判级层完全无视
- **`auth export/import/logout/reset`** 列入 `never` 终态拒绝

之上叠加：
- **本地 subagent**：`Explore` / `Plan` / `uat-verify` / `uat-deploy` / `prod-verify`（每类是一份可授权的独立能力）
- **远程 Worker**：通过 ClaudeCenter 桥接派活给云端 agent（比如"这个 issue 你去排查建 PR"）
- **Human-in-loop 兜底**：关键 gate 都留人工确认

### 🔁 Retrospective · 复盘沉淀

每次事故 / 上线 / 踩坑后写复盘 → 提炼进 `MEMORY.md` 长期记忆 → 反过来喂给下一次决策。

附带扩展的 **collaboration 类 memory**——不只 agent 学 user 偏好，user 也可以给 agent 反馈，让协作模式双向调整。这是"分身不只是被塑造的仆从，也是能给用户反馈的搭档"的物理落地。

---

## 快速上手

```bash
# 依赖
pip install pyyaml cryptography pytest

# 跑测试（全离线，不依赖真实钉钉/网络）
python3 -m pytest tests/ -v

# 手动冒烟
export PYTHONPATH=src DWS_AGENT_TEST_MODE=1 DWS_AGENT_HOME=/tmp/h \
       DWS_AGENT_DWS_BIN="$PWD/tests/mock/dws"
python3 -m dws_agent.cli.main init
python3 -m dws_agent.cli.main status
```

CLI 常用命令（安装后：`pip install -e .`）：

```bash
# 知识检索
dws-agent kb search --query "网关 token TTL 是多少"     # 结构化 Verdict + citations
dws-agent kb draft  --query "..."                       # 本地"如果代答会怎么答"预览
dws-agent kb status                                     # KU 库存状态

# 派活
dws-agent task create   ...                             # 派活给 ClaudeCenter Worker
dws-agent task publish  ...

# 安全动作（需 confirm_token）
dws-agent confirm --action-id AI-1 --argv <dws-command>
```

运行时根目录由 `DWS_AGENT_HOME`（默认 `~/.claude/dws-agent`）决定，敏感子目录 `memory/`、`kb/`、`keys/` 强制 `0700`，文件级加密使用 Keychain 派生的 AES-GCM 密钥。

---

## 目录导览

```
src/dws_agent/
├── core/         # 单根布局 + Keychain 派生密钥 + 落盘加密入口
├── executor/     # Executor + inbox + dws-shim + refresh_guard
├── policy/       # PolicyGate + classifier + confirm_token + policy.yaml
├── privacy/      # 脱敏 + 污点传播 + 单聊硬过滤
├── store/        # 审计 + state_db + undo
├── kdl/          # KDL 知识蒸馏层（model/distill/ingest/store/retrieve/serve）
└── cli/          # dws-agent / dwsd / launchd plist 生成

scripts/          # 运维脚本（kdl 灌库 / 日志查询 / feedback 巡检 / 文档渲染）
tests/            # 全离线测试（mock dws 二进制）
docs/design/      # 技术设计文档（md 源 + html 渲染）
docs/retro/       # 每日 / 事故复盘（反哺 memory）
```

---

## 项目状态

- ✅ **阶段 0 · 安全地基**：Executor + PolicyGate + confirm_token + dws-shim + 审计
- ✅ **阶段 1 · KDL 知识蒸馏**：provenance 强绑 / 脱敏污点 / QA 配对 / 新鲜度回路 / abstain
- ✅ **阶段 2 · MVP1 钉钉代答**：读消息 → 检索 → 拟答 → 用户确认 → 代发（闭环跑通）
- 🔄 **平台化演化（进行中）**：ClaudeCenter Worker 桥接、多 subagent 协同、生产验证工具链（`ks_logs.py`、`prod-verify`）、复盘半自动化

进度详情见 `git log` + `docs/design/` + `docs/retro/`。

---

## 设计原则（硬约束）

1. **纯只读默认**：KDL 检索 / 草稿全在本进程，绝不对外发送
2. **不编**：六条 abstain 规则触发就返回 `ABSTAIN`，不产答复
3. **不越权**：所有对外写操作 `default-deny`，判级不看 `--yes`
4. **不失忆**：审计 JSONL 全量落盘，`confirm_token` 绑 argv 一次性
5. **不失联**：Human-in-loop 兜底，关键 gate 都留人工确认

---

## 更多阅读

- [`docs/design/md/dws-agent-设计方案.md`](docs/design/md/dws-agent-设计方案.md) —— 长期完整愿景（7 约束 / 三套分级 / 四角色 / 五阶段）
- [`docs/design/md/方案-MVP.md`](docs/design/md/方案-MVP.md) —— MVP1 链路 + 平台演化下一步
- [`docs/design/md/kdl-知识库.md`](docs/design/md/kdl-知识库.md) —— KDL 数据接入方案
- [`docs/design/md/agent-编排与执行.md`](docs/design/md/agent-编排与执行.md) —— Agent 编排细节
- [`docs/design/md/复盘与memory进化系统.md`](docs/design/md/复盘与memory进化系统.md) —— 复盘反哺 memory 机制

---

## License

Apache License 2.0 —— 详见 [`LICENSE`](LICENSE)
