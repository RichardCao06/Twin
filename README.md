# dws-agent (阶段 0：地基 + 认证 + 安全门)

钉钉产品能力（dws CLI）之上的一个**思考与执行分离**的代理框架。本仓库目前完成
**阶段 0** 的骨架与硬约束落地：凭证隔离、落盘加密入口、确定性执行器、按 argv
判级的策略门、出口/污点骨架、全量审计、刷新串行化、以及无 LLM 的守护进程骨架。

> 设计文档见 `docs/design/md/dws-agent-设计方案.md`。本仓库只实现阶段 0；阶段 1-4 留扩展点。

## 这是什么

- **Executor（无 LLM）**：消费 `ActionIntent` JSON，行为完全确定性。执行权来自
  OS 权限 + token 隔离，而非改 `PATH`。
- **PolicyGate**：按规范化 argv 判级，`default-deny`（不在 R0 只读白名单内一律需
  `confirm_token`），未知子命令按 R2 拦截，`auth export/import/logout/reset` 列入
  `never` 终态拒绝。判级**完全不看 `--yes`**。
- **confirm_token**：绑定 `sha256(规范化 argv)` + `actionId` + TTL（默认 300s），
  一次性使用，哈希不匹配/过期/已用即拒。
- **dws-shim**：校验 `DWS_GATE_TOKEN`；无 token 时对 R0 只读放行、对写命令
  `exit 1` 并写审计。
- **隐私**：正则 + 熵的脱敏、污点传播、单聊硬过滤（仅 `conversationType==group`
  且在 `allowed_groups` 中才进入 signal）。
- **审计**：全量 JSONL（`audit/audit-YYYYMMDD.jsonl`），带 `ts/seq/pid`。
- **refresh-guard**：跨进程文件锁串行化 token 刷新。
- **dwsd**：无 LLM 的守护进程骨架 + launchd plist 生成器 + `dws-agent` CLI
  （`init` / `status` / `confirm`）。

## 目录结构

```
src/dws_agent/
  core/        config.py paths.py crypto.py scaffold.py   # 单根布局 + Keychain 派生密钥 + 落盘加密入口
  executor/    executor.py inbox.py shim.py refresh_guard.py _argvutil.py
  policy/      gate.py classifier.py confirm.py loader.py policy.yaml
  privacy/     redaction.py taint.py single_chat.py
  store/       audit.py state_db.py undo.py
  cli/         main.py dwsd.py launchd.py                  # dws-agent / dwsd / plist 生成
  contracts/   # 跨模块共享契约
tests/
  conftest.py + 7 个测试模块, tests/mock/dws (mock dws 二进制)
docs/          # 设计方案 + 会话记录
```

运行时根目录由 `DWS_AGENT_HOME`（默认 `~/.claude/dws-agent`）决定，由 `core/scaffold.py`
幂等创建。敏感子目录 `memory/`、`kb/`、`keys/` 强制 `0700`，并写入 `.sensitive` 标记，
文件级加密使用 Keychain 派生的 AES-GCM 密钥。

## 如何跑测试

```bash
/opt/homebrew/anaconda3/bin/python3 -m pip install pytest pyyaml -q   # 依赖
cd /Users/shujudagongren/Myspace/dingding-agent
/opt/homebrew/anaconda3/bin/python3 -m pytest tests/ -v
```

测试在 `DWS_AGENT_TEST_MODE=1` 下运行，`DWS_AGENT_DWS_BIN` 指向 `tests/mock/dws`，
**绝不触发真实 dws 写操作**，也无网络。

CLI 手动冒烟（同样需 `PYTHONPATH=src` 与 mock dws）：

```bash
export PYTHONPATH=src DWS_AGENT_TEST_MODE=1 DWS_AGENT_HOME=/tmp/h \
       DWS_AGENT_DWS_BIN="$PWD/tests/mock/dws"
python3 -m dws_agent.cli.main init
python3 -m dws_agent.cli.main status
python3 -m dws_agent.cli.main confirm --action-id AI-1 --argv dws calendar create --title x
python3 -m dws_agent.cli.dwsd --once
python3 -m dws_agent.cli.launchd        # 打印 launchd plist
```

> 注意：用 `python3 path/to/main.py` 直接运行脚本会让模块名变成 `__main__`，
> 若此时把 Paths 对象误塞进 `DWS_AGENT_HOME` 会生成名为 `<__main__.Paths ...>`
> 的垃圾目录。请用 `python3 -m dws_agent.cli.main` 模块方式运行；该模式已在
> `.gitignore` 中屏蔽相关垃圾目录。

## 阶段 0 已实现的退出条件

设计文档阶段 0 退出条件（可实测）与对应测试：

| 退出条件 | 状态 | 对应测试 |
|---|---|---|
| 并发抢锁 100% 串行、0 撕裂 | 已覆盖 | `tests/test_refresh_guard.py`（线程内串行、跨进程唯一持有、并发子进程恰一胜出）|
| never 拒绝覆盖 100% | 已覆盖 | `tests/test_policy_classify.py::test_never_list_is_terminal_deny`、`test_executor_e2e.py::test_never_list_terminal_deny_no_execution` |
| 磁盘明文 grep = 0（敏感目录） | 入口已立 | `core/crypto.py` + `scaffold.py`（`memory/`、`kb/` 标记 + Keychain 派生密钥）|
| 污点不丢 = 100% | 已覆盖 | `tests/test_privacy_filter.py::test_taint_propagation_blocks_outbound`、`test_sensitive_never_washes_down` |
| 绕过 EgressGuard 路径 = 0 | 已覆盖 | 单聊硬过滤 + 污点：`test_privacy_filter.py`；写操作必须经 PolicyGate + token：`test_shim_token.py` |

其他阶段 0 硬约束（均有测试）：

- 思考/执行分离：Executor 无 LLM，确定性消费 `ActionIntent`。
- default-deny + R0 白名单 + 未知子命令按 R2：`test_policy_classify.py`。
- 判级不看 `--yes`：`test_policy_classify.py::test_yes_flag_*`、`test_confirm_token.py::test_yes_flag_ignored_on_verify`。
- confirm_token 绑 sha256(argv)+actionId+TTL、一次性、防篡改：`test_confirm_token.py`。
- dws-shim 无 token 时写命令 `exit 1` 并审计、R0 只读放行：`test_shim_token.py`。
- 全量审计 JSONL：`test_executor_e2e.py::test_audit_trail_is_written`。
- CLI `init/status/confirm`、dwsd 骨架、launchd 生成器：`test_cli.py` + 手动冒烟。

**测试结果：61 / 61 通过。**

## 集成验证发现并修复

- `cli/main.py` 的 `_FallbackPaths.locks_dir` 返回 `state/locks`，与权威布局
  `core/paths.py` 的 `home/locks` 不一致；若降级路径生效会使 refresh-guard 与 dwsd
  实例锁落到不同文件，破坏串行化。已修正为 `home/locks`，并补齐缺失的
  `snapshots_dir`（`undo` 使用）。`cli/dwsd.py` 复用该类，一处修复同时生效。
- 清理了仓库根下早期手动误运行产生的 `<__main__.Paths object at 0x...>` 垃圾目录，
  并在 `.gitignore` 中屏蔽。

## 还差什么（留给阶段 2+）

- 三套风险分级目前仅落地 **R 套（R0-R3）**；**C 套（拟答）/ W 套（派活）** 仅留扩展点。
- 落盘加密为**入口级**实现（Keychain 派生密钥 + 0700 + 标记）；阶段 1 的 KU body 与
  provenance quote 已走 `core.crypto` AES-GCM 落盘加密，但尚无「磁盘明文 grep=0」的
  自动化断言测试覆盖全目录。
- 分诊拟答（阶段 2，KDL 草稿 → 真正对外答复）、双 Agent 编排（阶段 3）、受控自治
  （阶段 4）均未开始。
- dwsd 为**无 LLM 骨架**，仅做 inbox 排空与生命周期；尚无真正的轮询业务逻辑。

---

# 阶段 1：知识蒸馏层（KDL，Knowledge Distillation Layer）

数字分身的**长期记忆 + 可代答知识库**。只做三件事：**摄取 → 蒸馏 → 供检索**。

> **硬约束（代码中已结构性落地）**：KDL **纯只读**，绝不对外发送任何东西、绝不调用
> 任何 `dws` 写命令。检索/草稿只输出给本人看；`draft` 产出带 `LOCAL PREVIEW — NOT
> SENT` 横幅的「如果代答会怎么答」草稿，全程不出本进程。

## 做了什么

- **四类知识源 + 统一知识单元 KU**：`CODE`（代码 @commit，symbol→行为/契约，漂移风险
  最高）/`ISSUE`（症状→根因→处置）/`QA`（规范化问答对）/`PLAYBOOK`（SOP 步骤卡）。
- **KU 数据模型**（`kdl/model.py`，字段最小化）：`ku_id / source_type / title / body /
  body_redacted / taint(CLEAN|INTERNAL|SENSITIVE) / authority(DRAFT|REVIEWED|
  AUTHORITATIVE|DEPRECATED) / public_ok / confidence / provenance / freshness /
  commit 绑定（CODE 类）`。
- **可溯源硬约束**：每个 KU 至少 1 条 provenance（`repo@commit` / `issueId` / `msgId`
  / `docId`）；**provenance 缺失即被 Ingestor 丢弃**（审计 `no_provenance`），即使
  入库也锁 `DRAFT` + `serve_blocked`，永不支撑代答。
- **入库即脱敏 + 污点标注**：body 与 quote 经 `privacy.redaction` 脱敏、
  `privacy.taint` 传播；污点只升不降，`SENSITIVE` / 未核 `public_ok` 不进入可对外路径。
- **蒸馏 LLM 可插拔**：`kdl/distill.py` 的 Distiller 仅产「知识候选 JSON」，**绝不**
  写库；阶段 1 默认确定性 `stub` 后端（离线、不联网，测试不依赖真实 LLM）。落库由
  **无 LLM 的规则化 `Ingestor`** 按规则完成。
- **QA 自动配对**：`Ingestor.pair_qa` 从消息线程配对「问↔我的回答」，含反投毒插话规则；
  入库即 `DRAFT`，只有「我已确认」才能升 `AUTHORITATIVE`（经 `store.set_authority`
  状态机：`DRAFT→REVIEWED→AUTHORITATIVE`，禁止越级）。
- **新鲜度回路**：CODE 类绑 commit；代码变更后 `reindex` 三态对账——symbol 仍在且哈希
  一致→`FRESH`（commit 指针 bump 到 HEAD）；哈希漂移→`STALE`（`AUTHORITATIVE` 自动降
  `REVIEWED`，沿 `ku_edge` 传播 `derived_stale`）；symbol 消失→`EXPIRED` + `serve_blocked`
  + provenance 置不可达。git 仅走只读白名单（`rev-parse/show/cat-file/log/ls-files/blame`）。
- **abstain 机制（绝不编）**：六条 abstain 规则——无命中 / 全 DRAFT / 全 EXPIRED 或最佳
  STALE 无新鲜替代 / 证据不可达 / 相关度双低 / 承诺口径标记，外加置信度地板；任一触发
  即返回 `ABSTAIN`，`draft` 不产答复只给机器可读理由。
- **检索（MVP）**：中文 bigram + FTS（不可用时回退倒排）+ 代码符号索引；Serve 层产结构化
  Verdict（`ANSWERABLE`/`ABSTAIN` + citations + confidence），**citations 只含来源标识
  （ku_id/type/authority/freshness/provenance 指针/score），绝不含 body 或原文引用**。

## 目录

```
src/dws_agent/kdl/
  model.py        # KU 数据模型 + 枚举 + make_ku_id（provenance 硬约束在模型层兜底）
  distill.py      # 可插拔 Distiller（默认确定性 stub）；只产候选 JSON，绝不写库
  ingest.py       # 无 LLM 规则化 Ingestor：validate→脱敏→污点→建 KU→upsert；QA 配对/adapter
  store.py        # sqlite（复用 store.state_db）：upsert/检索/FTS/符号索引/新鲜度对账/状态机
  code.py         # 只读 GitReader：符号抽取、verify_fact（实时校验）、reindex_repo（HEAD 对账）
  retrieve.py     # 检索 + 六条 abstain + serve(Verdict) + assemble_draft（本地草稿）
  cli.py          # dws-agent kb 子命令（ingest/reindex/search/draft/status）
  config.py       # KDL 设置（distiller 后端、top_n、阈值）
  ku.schema.json  # 候选 JSON 的 schema
tests/            # test_kdl_*.py（provenance/脱敏污点/QA 配对/新鲜度/abstain/无副作用/CLI）
```

KU body 与 provenance quote 以 `core.crypto` 派生的 AES-GCM 密钥落盘加密，存于共享
`state.db`（`kdl/store.py` 幂等建表，复用阶段 0 的 `store.state_db`）。

## 如何用 kb 命令

```bash
# 摄取：从源/候选 JSON（fixture 或预抽取转储）蒸馏并落库（只读文件，绝不发送）
dws-agent kb ingest --input candidates.json [--distiller stub]

# 重建/对账：对某仓库当前 HEAD 刷新 CODE-KU 新鲜度（只读 git）
dws-agent kb reindex --repo /path/to/repo

# 检索：产结构化 Verdict（只打印来源标识，无 body/原文）；--external 排除非 CLEAN 污点
dws-agent kb search --query "网关 token TTL 是多少" [--external]

# 草稿：本地「如果代答会怎么答」预览，带 LOCAL PREVIEW—NOT SENT 横幅与引用；abstain 不编
dws-agent kb draft  --query "网关 token TTL 是多少"

# 状态：KU 库存（按 source_type/authority/freshness 分组、serve_blocked 计数等）
dws-agent kb status
```

候选 JSON 形如 `[{"source_type":"QA","text":"...","meta":{"question":"...","answer":"...","msg_id":"msg-1"}}]`
（单条对象或列表均可；CLI 归一为 `RawItem` 后逐条过 Distiller，再交 Ingestor 落库）。

## 阶段 1 退出条件对照

| 退出条件 | 状态 | 证据 |
|---|---|---|
| 每个 KU 可溯源；无 provenance 锁 DRAFT 且禁支撑代答 | 已覆盖 | `tests/test_kdl_provenance.py` |
| 入库即脱敏 + 污点标注；SENSITIVE/未核 public_ok 不入可对外路径 | 已覆盖 | `tests/test_kdl_redaction_taint.py` |
| QA 自动配对；仅「我已确认」升 AUTHORITATIVE | 已覆盖 | `tests/test_kdl_qa_pairing.py` |
| CODE 绑 commit；变更后受影响 KU 标 stale；命中 stale 降级；实时校验 symbol | 已覆盖 | `tests/test_kdl_freshness_code.py` + reindex 三态冒烟 |
| abstain：置信度低/全 stale/未确认 → 不编 | 已覆盖 | `tests/test_kdl_abstain.py` |
| 检索产结构化 Verdict（answerable/abstain + citations，只含来源标识） | 已覆盖 | `tests/test_kdl_*`、CLI 冒烟 |
| 纯只读、绝不对外发送、绝不调 dws 写 | 已覆盖 | `tests/test_kdl_no_side_effects.py` + git 只读白名单 |
| 蒸馏 LLM 可插拔 + 确定性 stub，测试不联网 | 已覆盖 | `kdl/distill.py` + 全套离线测试 |

**测试结果：94 / 94 通过（含阶段 0 的 61）。**

## 集成验证发现并修复（阶段 1）

- `kdl/cli.py` 的 `kb ingest` 直接把整个 JSON 传给 `Distiller.distill()`（它只吃单个
  `RawItem`），且 `Ingestor(...)` 形参顺序、`IngestReport` 字段读取均与实现不符。已修：
  归一输入为 `RawItem` 列表逐条蒸馏聚合候选、按 `Ingestor(paths, conn, key)` 正确构造、
  按实际 `ingested(list)/dropped(list)/redacted_count` 渲染报告。
- `kb search/draft` 的 `serve(...)` / `assemble_draft(...)` 调用签名与实现不符
  （`serve(conn, key, query, *, external_facing)`、`assemble_draft(verdict, key=)`）。已对齐。
- `kb reindex` 误把 `reindex_repo` 当模块函数调用；实际是 `GitReader.reindex_repo` 方法，
  且 `ReindexReport` 字段为 `head_sha/checked`。已改为 `GitReader(repo).reindex_repo(conn, key, repo)`。
- `code.GitReader.reindex_repo` 依赖 6 个 `store.*` 辅助函数（`get_code_kus_for_repo`/
  `mark_expired_evidence_broken`/`downgrade_authority`/`mark_stale`/`propagate_derived_stale`/
  `mark_fresh_bump_commit`）尚未在 `store.py` 落地，导致 reindex 直接报错。已基于既有原语
  补齐这 6 个轻量、免密钥的行级辅助函数。
- `reindex_repo` 原本在 KU 的**已固定 commit** 上重抽符号（按定义恒与自身哈希一致，永远
  FRESH，检测不到漂移）。已改为在**当前 HEAD** 的 KU 视图上校验，确认 FRESH 后才由
  `mark_fresh_bump_commit` 把 commit 指针推进到 HEAD——这样 FRESH→STALE→EXPIRED 三态对账
  全部正确（端到端冒烟通过）。
- crypto 依赖 macOS `security` 命令派生 Keychain 密钥，跨平台后备尚未实现。
