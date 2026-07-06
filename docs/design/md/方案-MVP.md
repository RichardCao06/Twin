# DWS-Agent 方案（MVP 已跑通 · 平台化演化路径）

> 本文档记录 MVP1（钉钉代发）的执行蓝图与完成状态，并说明它跑通后项目的实际演化方向。
> 复杂的风险分级 / 编排 / 自治 / 各种安全闸 → 见 [长期完整愿景](dws-agent-设计方案.md)。
> 最后更新：2026-07-06（重新定位）。

## MVP1 · 钉钉代答链路（已完成，2026-06）

### 一句话

**手动触发 → 读一条钉钉消息 → 检索我的知识库 → Claude 拟答 → 我过目确认 → 代发（署名"助理代答"）。**

### 核心流程（就这一条线）

```
[你] 手动触发（指定群 / 最近 @我 的消息）
  → 读消息    DwsReader.chat_search_messages（只读，已校准）
  → 检索知识  KDL serve（7289 KU：代码 + Workspace 文档 + 钉钉文档）
  → 拟答草稿  Claude 基于检索结果组织答复（署名"助理代答·待本人复核" + 出处）
  → 你过目    本地呈现：草稿 + 命中的知识 + 引用
  → 你确认    发 / 改 / 不发
  → 代发      确认后经阶段0 写闸门（confirm_token + dws-shim token 隔离 + 全量审计）
              → dws chat message send
```

### 已就绪的底座（直接复用，不重做）

| 已完成 | 提供给 MVP |
|---|---|
| **阶段0**（确定性 Executor + PolicyGate argv 判级 + confirm_token + dws-shim token 隔离 + 审计） | **安全代发**（写命令必须 confirm_token、token 隔离、留审计） |
| **阶段1 KDL**（7289 KU，验收 4 条 PASS：命中 0.938 / 溯源 100% / 外发 0 / 脱敏漏 0） | **检索知识**（`dws-agent kb search/draft`） |
| **DwsReader**（dws 只读封装，已校准命令与字段） | **读消息 / 读文档** |

### 4 条底线（MVP 也守，简单且是分身的安全本质）

1. **不自动发** —— 必须你确认（confirm_token）才发，绝无自动发送。
2. **不冒充你** —— 草稿一律署名「助理代答·待本人复核」。
3. **知识不外泄** —— KDL 纯只读；对外只发你确认过的那条答复。
4. **可溯源** —— 答复基于 KDL 命中知识，带出处。

---

## 跑通之后：项目的实际演化方向

MVP1 跑通后，项目的实际重心迁移到**"知识 + 编排 + 复盘"这条循环**——钉钉从"主战场"退到"入口通道之一"。

### 现在每天真正在跑什么

| 类别 | 组件 | 使用频率 | 涉及钉钉？ |
|---|---|---|---|
| 知识检索 | `dws-agent kb search`、`MEMORY.md` 自动加载 | 每次会话 | ❌ |
| 派活远程 Worker | `dws-agent task create/publish` → ClaudeCenter | 高频 | ❌ |
| 派活本地 Subagent | `Explore` `Plan` `uat-deploy` `uat-verify` `prod-verify` | 高频 | ❌ |
| 生产日志排查 | `scripts/ops/ks_logs.py`（KubeSphere API 只读） | 中频 | ❌ |
| 生产直连 DB | `prod.env` + psql | 中频 | ❌ |
| Feedback 巡检 | `scripts/ops/feedback_patrol.sh`（launchd 每小时） | 后台常驻 | ✅（发通知） |
| MVP1 钉钉代答 | `dws-agent triage` | 低频（按需） | ✅ |
| 复盘 & memory 更新 | `docs/retro/`、`memory/*.md` | 每次事故/上线 | ❌ |

### 演化路径（不再强制串行，按需推进）

1. **✅ MVP1 · 钉钉代答链路** —— 已跑通
2. **🔄 平台化能力**（进行中）
   - 多 Worker 并发派活（ClaudeCenter + 本地 subagent 组合）
   - 生产验证工具链（`prod-verify` + 突破"生产只读"边界的授权机制）
   - Uat 部署/验收链路自动化（`uat-deploy` + `uat-verify`）
   - 复盘半自动化（事故触发时起草复盘草稿）
3. **⏳ 分诊后台化** —— 钉钉后台轮询新消息、Case 归并（如果代答规模化再做）
4. **⏳ 分级护栏** —— C 分级 + 出口管控 + 承诺语义检测（代答规模化时）
5. **⏳ 派活规范化** —— 双 Agent 编排（W0–W2，提 PR 封顶），把当前 task bridge + subagents 抽出统一契约
6. **⏳ 受控自治** —— 按域逐个开放 C0 自动代答

> 3-6 级的完整设计已在 [dws-agent-设计方案.md](dws-agent-设计方案.md)（长期愿景）里，不浪费，到需要时取用。**当前阶段的重点是"平台化能力"，钉钉相关的第 3-6 级按需推进即可，不是必经路径。**

### 明确"不做"（无论钉钉还是平台化都守）

- 任何形式的**自动发送**（钉钉消息、PR merge、部署）——永远需要人确认
- 冒充本人身份对外说话
- Executor 侧引入 LLM
- 跳过 audit 或 confirm_token
- 长期分支替代 main 作为集成主干（历史踩过坑，详见 `docs/retro/复盘-2026-07-03.md`）
