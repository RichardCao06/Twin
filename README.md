# dws-agent (阶段 0：地基 + 认证 + 安全门)

钉钉产品能力（dws CLI）之上的一个**思考与执行分离**的代理框架。本仓库目前完成
**阶段 0** 的骨架与硬约束落地：凭证隔离、落盘加密入口、确定性执行器、按 argv
判级的策略门、出口/污点骨架、全量审计、刷新串行化、以及无 LLM 的守护进程骨架。

> 设计文档见 `docs/dws-agent-设计方案.md`。本仓库只实现阶段 0；阶段 1-4 留扩展点。

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

## 还差什么（留给阶段 1+）

- 三套风险分级目前仅落地 **R 套（R0-R3）**；**C 套（拟答）/ W 套（派活）** 仅留扩展点。
- 落盘加密为**入口级**实现（Keychain 派生密钥 + 0700 + 标记）；尚未对 `memory/`、
  `kb/` 的实际读写做端到端加解密包裹与「磁盘明文 grep=0」的自动化断言测试。
- 知识蒸馏/检索（阶段 1）、分诊拟答（阶段 2）、双 Agent 编排（阶段 3）、受控自治
  （阶段 4）均未开始。
- dwsd 为**无 LLM 骨架**，仅做 inbox 排空与生命周期；尚无真正的轮询业务逻辑。
- crypto 依赖 macOS `security` 命令派生 Keychain 密钥，跨平台后备尚未实现。
