# KDL 知识库 · 搭建与知识蒸馏

> 阶段 1（KDL 知识蒸馏层）已完成，7289 KU 灌入正式库，四条退出条件全部 PASS 并已签署。
> 本文档合并原《KDL 数据接入方案》+《子方案细节》，按**实际实现**留档；不再是"打算怎么做"，而是"真的这样跑起来了"。
> 最后更新：2026-07-06（合并版）。

---

## 目录

- [1. 定位与硬约束](#1-定位与硬约束)
- [2. 数据模型：KU + Provenance](#2-数据模型ku--provenance)
- [3. 三段式：摄取 → 蒸馏 → 落库](#3-三段式摄取--蒸馏--落库)
- [4. 四类数据源（枚举锁死，不新增）](#4-四类数据源枚举锁死不新增)
- [5. 模块实现（对应 src/dws_agent/kdl/）](#5-模块实现对应-srcdws_agentkdl)
- [6. 新鲜度治理（三层校验）](#6-新鲜度治理三层校验)
- [7. CLI 与工具链](#7-cli-与工具链)
- [8. 验收：阶段 1 退出条件](#8-验收阶段-1-退出条件)
- [9. 本期不做（推后 / 保留扩展点）](#9-本期不做推后--保留扩展点)
- [附录 A. 表结构清单](#附录-a-表结构清单)
- [附录 B. 关键决策记录（2026-06-22）](#附录-b-关键决策记录2026-06-22)

---

## 1. 定位与硬约束

**KDL** 是 DWS-Agent 的**长期记忆 + 可代答知识库**。三件事：**摄取（Ingest）→ 蒸馏（Distill）→ 供检索（Serve）**。**不直接对外回答**——那是分诊/起草 Agent 的事；KDL 只交出"带证据、带权威级、带新鲜度"的候选片段。

**7 条硬约束**（贯穿全模块，每条都由代码结构强制）：

| # | 硬约束 | 落地位置 |
|---|---|---|
| 1 | 纯只读接入 | `dws_read.DwsReader.ALLOWED` 白名单，非只读子命令在调 dws **之前** raise `PermissionError` |
| 2 | 思考 / 执行分离 | Distiller 唯一可能用 LLM，只产 JSON 无副作用；Ingestor 无 LLM、按规则落库 |
| 3 | `provenance` ≥ 1 | Ingestor 校验，0 provenance = DROP + `event='privacy_filter'` 审计；store 层 `upsert_ku` 再强制一次（无 provenance → 强制 DRAFT + `serve_blocked`） |
| 4 | 入库即脱敏 + 污点传播 | 每条 body / quote 过 `privacy.redaction.redact`，KU taint = MAX(redaction, quote taint, declared) |
| 5 | 权威状态机不越级 | `store.set_authority` DRAFT → REVIEWED → AUTHORITATIVE 单向；越级 raise；本人确认是唯一升 AUTHORITATIVE 路径 |
| 6 | CODE 绑 commit | `code.py` 每个 KU 强制携带 `commit_sha + file_path + symbol + line_range + content_hash`，代码漂移可检测 |
| 7 | body/quote 落密文 | `store.py` 用 `core.crypto` AES-256-GCM，磁盘 grep 明文 = 0（这是阶段 0 的退出条件之一） |

---

## 2. 数据模型：KU + Provenance

### 2.1 KnowledgeUnit（`kdl/model.py`）

```
KnowledgeUnit {
  ku_id             一次性稳定标识；non-CODE 由 (source_type|prov_ref) sha1；CODE 由 commit+symbol
  source_type       CODE | ISSUE | QA | PLAYBOOK            # 4 枚举锁死，不新增
  title, body       body 明文只在内存，落库前 encrypt → body_cipher
  taint             CLEAN | INTERNAL | SENSITIVE           # taint 永不下泄
  authority         DRAFT | REVIEWED | AUTHORITATIVE | DEPRECATED
  freshness         FRESH | STALE | EXPIRED | UNKNOWN
  public_ok         bool  # 明确核定"可对外公开"，默认 False
  serve_blocked     bool  # 任一硬门失败即锁；ANSWERABLE 永不选中 serve_blocked KU
  confidence        0..1  # 排序参考，不决定能否代答
  provenance[]      至少 1 条，否则 DROP
  # CODE-only:
  repo, commit_sha, file_path, symbol, line_range, content_hash
  # 时间:
  created_at, updated_at, last_verified_at, expires_at, superseded_by, owner
}
```

### 2.2 Provenance（每条 KU ≥ 1）

```
Provenance {
  kind        COMMIT | ISSUE_URL | MSG_ID | DOC_ID | MAIL_ID | FILE
  ref         回查用的引用（commit sha、doc nodeId、msg id、文件路径……）
  quote       原文摘录（可选），落库前脱敏 → quote_cipher
  quote_taint CLEAN | INTERNAL | SENSITIVE                 # 参与 KU taint MAX 合并
  captured_at 抓取时间（RFC3339）
  retrievable bool                                          # 未来回查是否还能取到
}
```

**硬规则**：`provenance` 空 → `authority` 被强制回 DRAFT + `serve_blocked=True`，检索侧永不选中。这条约束在 `Ingestor.ingest_candidates` 和 `store.upsert_ku` **两处独立校验**（防单点漏网）。

---

## 3. 三段式：摄取 → 蒸馏 → 落库

原 2026-06-22 拍板决策 D3：**蒸馏由当前 Claude 会话在线做，不引入外部 LLM 服务**。三段式保住硬约束 2（思考/执行分离）：

```
① 摄取（确定性 · 无 LLM · 可后台自动）
   DwsReader 只读拉文档/对话 / GitReader 只读读 git → 脱敏 → 存 RawItem
② 蒸馏（LLM = Claude · 会话内 · 零副作用）
   Claude 读 RawItem → 产候选 JSON（契约 / Q-A / 步骤卡）
   只产 JSON：不写库、不发送、不调 dws 写
③ 落库（确定性 · 无 LLM）
   Ingestor 校验 provenance/脱敏/污点/强制 DRAFT → AES-GCM 加密 → upsert_ku
```

**关键推论：**
- **CODE 源可以完全自动化**——`GitReader.index_repo` 直接产结构化候选（函数/类/接口 + `content_hash`），**绕过蒸馏 LLM** 直入 Ingestor（`scripts/kdl_ingest.py --repo` 就是这条路径）。
- **文档/对话的蒸馏必须有 Claude 会话在场**——因为要理解语义。这与"KU 一律 DRAFT 起步、本人确认才升 AUTHORITATIVE"天然契合：蒸馏时本人就在环，顺手轻确认。
- **`StubDistiller`** 保留为**确定性离线基线**（供测试与结构极规整的材料），不是主路径。
- **`LlmDistiller`（外部模型后端）不做**；若未来要无人值守，作为 `claude -p` headless 受限调用的扩展点。

---

## 4. 四类数据源（枚举锁死，不新增）

| 源 | source_type | 原始形态 | **实际入库产物** | 新鲜度风险 | 摄取路径 |
|---|---|---|---|---|---|
| 在开发项目代码 | CODE | commit / 函数 / 类 / 接口 | **仅索引 + 原文切片**：title = `文件路径::符号名 (kind)`；body = 一行头信息 + 该符号完整源码切片；**不做语义蒸馏**（"契约 / 行为 / 已知坑"是设计意图，未启用，见 [§9](#9-本期不做推后--保留扩展点)） | **极高**（代码漂移） | `GitReader.index_repo` 直接产结构化候选，**绕过 Distiller**，Ingestor 校验后直接 upsert |
| 历史 issue / 写过的问题 | ISSUE | issue 正文 / 标题 / 标签 / 状态 | 症状 → 根因 → 处置 卡（Claude 蒸馏） | 中 | RawItem → Claude 蒸馏 → 候选 |
| 历史问答对 | QA | 钉钉消息 / 文档评论 / 邮件往返 | 规范化 Q → A 对（Claude 蒸馏） | 中 | RawItem → Claude 蒸馏 → 候选（含反抢答校验） |
| 干过的工作套路 | PLAYBOOK | 重复操作序列 / 审批 / 部署流程 | SOP / Runbook 步骤卡（Claude 蒸馏） | 低-中 | 主要来自本人钉钉文档 |

> **CODE 与其它三类的关键差异：**其它三类走"翻译官（Claude）读原文 → 产语义摘要 → 存摘要"；CODE 走"结构化索引 + 存原文切片 → 语义理解推迟到检索使用点由 Claude 现场做"。这个设计取舍见 §5.2 末尾说明。

**为什么锁死 4 枚举不新增 DOC/NOTE：** 2026-06-22 拍板决策 D1。文档块按内容映射到 PLAYBOOK（SOP/流程类）或 ISSUE（症状-根因类），避免每加一个源就多一枚举、检索侧多一分支。

---

## 5. 模块实现（对应 `src/dws_agent/kdl/`）

10 个模块 + 1 个 schema。核心链路 6 个模块下面展开：

### 5.1 `dws_read.py` · DwsReader（196 行）

**唯一的 dws 入口**，硬只读白名单：

```python
ALLOWED = {
  ("doc", "search"), ("doc", "list"), ("doc", "info"), ("doc", "read"),
  ("chat", "search"),
  ("chat", "message", "list"), ("chat", "message", "list-all"),
  ("chat", "message", "search-advanced"), ("chat", "message", "list-mentions"),
  ("contact", "user", "get-self"),
}
```

`_run(cmd_path, flags)` 内部第一步就是白名单校验，非白名单 `raise PermissionError`——**在拼命令之前就拒绝**，杜绝任何一处代码路径能通过 DwsReader 写 / 发。字段解析按 2026-06-22 实测校准（见 [dws 只读接口校准](../../overview/dws-只读接口校准.md)）。

### 5.2 `code.py` · GitReader（738 行）

CODE 源的全部实现：
- **绑定 pinned commit**：`GitReader(repo_root)` 记录 HEAD sha，之后所有 walk 都用同一个 sha。
- **符号级抽取**：Python 用 `ast` 抽 function / class / async def（含方法）；其它语言用 regex fallback。
- **稳定身份**：`commit_sha + file_path + symbol + line_range + sha256(source_slice)` 组成 CODE-KU 的身份键。
- **只读 git 访问**：内部只调 `git show / cat-file / log --format` 等只读子命令；写 / commit / push 请求在拼命令之前就 raise。
- **三层新鲜度校验**（见 §6）。

`GitReader.index_repo()` 直接产**已结构化的候选 dict**——每个 dict 里 provenance / repo / commit / file / symbol / line / hash 齐全，Ingestor 只需校验后直接 upsert，**不需要经过 Distiller**。这就是 CODE 灌库的高吞吐路径。

**候选 dict 的实际字段**（`_candidate(repo, head, rel, sym)`，`code.py:526`）：

| 字段 | 值示例 |
|---|---|
| `title` | `src/dws_agent/kdl/store.py::upsert_ku (function)` |
| `body` | `function` upsert_ku `defined in src/…/store.py (lines 220-268) @ 7cb665f5abcd.` + 换行 + **该符号完整源码切片** |
| `provenance` | 一条 `kind=COMMIT, ref=<head_sha>, quote=<源码切片>` |
| 其它 | `repo / commit_sha / file_path / symbol / line_range / content_hash` |

**明确的实施边界（重要）：** 当前 CODE 摄取**只做索引 + 原文切片**——`body` 是"这段代码的原文"，不是"这段代码被理解过的摘要"。设计文档里描述的"契约、行为/副作用、已知坑"三类语义卡属于**未启用的扩展**（见 §9）。这条设计取舍的原因：
- 代码本身已经是结构化的（有文件、行号、符号名、`content_hash`），检索层通过 title + body 里的原文就能召回；
- 语义理解推迟到检索使用点由 Claude 现场做——`kb search` 命中一张 CODE 卡后，读 body 里的原文再理解、再答。相当于卡片是"搜索索引"，理解在使用点做。
- 走 Distiller 语义蒸馏所有函数的成本至少几十美元一轮，且每次代码大改要重跑。目前这种取舍解决了 80% 的问题，剩下 20% 待权威档场景真的需要时再启用。

### 5.3 `distill.py` · Distiller + RawItem（469 行）

**Distiller Protocol**：

```python
class Distiller(Protocol):
    def distill(self, raw: RawItem) -> list[dict]: ...   # 只产候选 dict，零副作用
```

- **RawItem**（`source_type, text, meta`）—— 一个源单元。CODE 蒸馏时基本不用（GitReader 已直接产候选）；DOC/ISSUE/QA 才走 RawItem → Distiller → 候选。
- **候选 dict schema** 见文件顶部 docstring（必填：`source_type / title / body / provenance`；CODE 附 `commit_sha` 等；可选 `linked_symbols` 用于建 ku_edge 反向引用）。
- **StubDistiller** —— 规则化基线（按 heading / regex 抽出 症状/根因/处置）；生产上主要跑测试和格式规整材料。
- **`get_distiller(name)`** 工厂：本期只暴露 `stub`；未知 name 回落到 stub 并让调用方 audit。

**Distiller 的边界**：可能是 LLM，但**只产 JSON**——不写库、不发送、不调 dws。所有落地由 Ingestor 按规则做。

### 5.4 `ingest.py` · Ingestor（549 行）

**无 LLM 落库口**，规则化半：

1. **校验候选** → 缺 `source_type`/`title`/`body`/`provenance` 或类型非法 → DROP，写审计
2. **脱敏 body + 每条 quote** → `privacy.redaction.redact`（正则 + 熵检出手机号 / 邮箱 / token / 密钥）
3. **计算 taint** → `MAX(declared, redaction_taint, quote_taints)`（`propagate` 单调不递减）
4. **强制 `authority=DRAFT`** —— 无论候选 dict 里声明什么
5. **构 `KnowledgeUnit` 不可变数据类**
6. **`upsert_ku`** —— store 侧再校验一次 provenance 数量 + AES 加密 + 落库

**产出 `IngestReport`**：`ingested / dropped / redacted_count`，可对上游透明反馈质量。

### 5.5 `store.py` · 持久化（878 行）

- **共用 phase 0 的 `state.db`**（SQLite）—— 不建独立 DB，减少运维面。
- **6 张表** + 索引（见附录 A）：`ku / ku_provenance / ku_symbol / ku_edge / kdl_meta / ku_inverted`；FTS5 用 `unicode61` tokenizer + 自定义 bigram tokenize（对中文友好，见 §5.6）
- **落密文**：`body_cipher` `quote_cipher` 是 AES-256-GCM 加密后的 BLOB，key 来自 `core.crypto`（Keychain 派生）。**任何明文 body/quote 都不会落盘**——这是阶段 0 "磁盘 grep 明文 = 0" 退出条件的关键依赖。
- **单调状态机**：`set_authority(DRAFT → REVIEWED → AUTHORITATIVE)`；越级或反向 raise。降级（AUTHORITATIVE → REVIEWED + STALE）是漂移传播用的，走 `mark_stale_by_file` 内部路径。
- **stale 传播**：`mark_stale_by_file(repo, file_path)` 沿 `ku_edge` 图把 ISSUE↔CODE / QA↔CODE 引用的 KU 一起标 `derived_stale`。

### 5.6 `retrieve.py` · 检索 + Serve 判定（883 行）

- **分词**：`bigram_tokenize` —— CJK 用重叠 2-gram（"知识库" → 知识 / 识库），ASCII 用 `\w+` 小写。**索引和查询用同一分词器**，中文召回不脏。
- **两阶段召回**：
  - L1 `ku_symbol` 符号精确查（`repo/file/symbol` 三键索引）
  - L2 FTS5 + 反向索引词汇匹配（`ku_inverted`）
- **硬门在打分前跑**（这是保证"绝不吐"的关键）：
  - EXPIRED / DEPRECATED / `serve_blocked` → 直接丢弃
  - `external_facing=True` 时 taint ≠ CLEAN → 丢弃
  - CODE-KU 做懒校验 → 漂移即拒
- **打分**：`score = w_f × freshness + w_a × authority + w_r × relevance`（权重在 `config.py`）
- **`serve()` 六条 abstain 规则**：任一命中即 ABSTAIN（不编）
  1. 无 candidate
  2. 全部 CODE-KU 懒校验失败
  3. Top-1 confidence 低于阈值
  4. Top-1 vs Top-2 一致性不足（矛盾）
  5. 命中全是 `serve_blocked`
  6. 任何 verify/decrypt/retrieve 异常 → 兜底 ABSTAIN
- **`assemble_draft()`** —— 本地预览"如果代答会怎么答"给本人看；**只引 source identifier，永不带 quote / body 明文，永不传输**。

### 5.7 其它：`model.py` / `config.py` / `ku.schema.json` / `cli.py`

- `model.py`（515 行）—— frozen dataclass + enum，`KnowledgeUnit / Provenance / Verdict / Citation / DraftPreview`
- `config.py`（98 行）—— 权重/阈值统一从环境变量读，符合"进化只落数据、不落代码"（设计 §3.6）
- `ku.schema.json` —— 候选 dict 契约的 JSON Schema，供 `Ingestor.validate` 使用（三处校验一致：schema / Ingestor / cli）
- `cli.py`（563 行）—— `dws-agent kb {ingest|reindex|search|draft|status}` 子命令入口 + 审计（见 §7）

---

## 6. 新鲜度治理（三层校验）

CODE 是漂移风险最高的源。三层递进：

| Freshness | 含义 | 判定 | 检索可用性 |
|---|---|---|---|
| **FRESH** | 绑定 commit 仍最新 + `content_hash` 匹配 | GitReader 校验通过 | 可作依据答（按 authority） |
| **STALE** | 符号在 HEAD 已变但仍存在 | `content_hash` 漂移 | 仅"参考性"答，必降级 + 提示"可能已过期" |
| **EXPIRED** | 符号已删 / 文件不存 / 源不可取回 | 找不到符号 | **禁止代答**，转 abstain / 升级 |
| **UNKNOWN** | 未绑定 commit 或从未校验 | 无绑定 | 视同 STALE 下限 |

**三层触发：**
1. **事件触发**：监听 push / merge，对受影响 `file_path` 反查 KU 逐符号重算 `content_hash`（`reindex` CLI 走这条）
2. **定时巡检**：超阈值未验证的批量复检（`dwsd` 钩子）
3. **检索时懒校验**（最后防线）：`retrieve.py` 取用 CODE-KU 时实时轻量 hash 比对再返回；懒校验失败 → 该 KU 从本次结果拿掉，全部失败 → ABSTAIN

**AUTHORITATIVE 也会被漂移撤销**：底层代码漂移 → `set_authority` 内部自动 AUTHORITATIVE → REVIEWED + STALE，同时生成"待本人复核"任务。**权威性不跨代码变更自动延续。**

---

## 7. CLI 与工具链

### 7.1 主 CLI · `dws-agent kb`

| 子命令 | 作用 |
|---|---|
| `dws-agent kb ingest --input <path>` | 从 JSON 文件读原始源材料 → Distiller → Ingestor → 落库 |
| `dws-agent kb reindex --repo <path>` | 对 repo 跑 GitReader 复检，更新新鲜度（不新增 KU） |
| `dws-agent kb search <query>` | 检索 + 打分，返回 Top-N（本地打印，永不发送） |
| `dws-agent kb draft <query>` | 组织成 draft 预览（只引 source identifier） |
| `dws-agent kb status` | 打印库统计（KU 总数、按 source_type / authority / freshness 分布、last_verified） |

**每一次 CLI 调用都写审计**（`_audit(paths, kdl_op=..., reason=..., detail=...)`），可追踪谁在什么时间对库做了什么。

**职责红线（强约束）：**
- `ingest` / `sync(doc/chat)` 只**新增 / 幂等覆盖 KU**，**绝不写 freshness 状态机标记**
- `reindex` / `sync(code)` 只**维护既有 CODE-KU 新鲜度**，**绝不新增 KU**
- 两者都 READ-ONLY 源、都不发送、都不调 dws 写

### 7.2 灌库工具链 · `scripts/kdl_*.py`

主 CLI 之外的独立工具（不进 `dws_agent` 包，避免 import 链复杂化）：

| 脚本 | 用途 |
|---|---|
| `scripts/kdl_ingest.py` | **候选直入灌库**——`--repo` 跑 GitReader 产 CODE 候选或 `--candidates` 读已蒸馏 JSON，都**绕过 Distiller** 直接给 Ingestor。为什么不用 `kb ingest --input`：那条路把输入当"原始源材料"再蒸馏一遍，会把已经产好的候选二次处理错位。 |
| `scripts/kdl_fetch_docs.py` | 摄取本人钉钉文档正文：`contact user get-self` → 拿 uid → `doc search --creator-uids <uid> --extensions adoc` 翻页 → 逐篇 `doc read` → 存 `/tmp/kdl-ddoc/<safe>.md` + `manifest.json`（含 nodeId 供后续做 provenance） |
| `scripts/kdl_dedup_docs.py` | 合并 `<dir>/doc-*.json` 候选，给每张卡 provenance ref 追加 `#k<i>` 后缀。防非 CODE 候选 `make_ku_id = sha1(source_type | prov_ref)` 撞键覆盖 |
| `scripts/kdl_review.py` | 批量把 DRAFT KU 升到 REVIEWED——本人一次性"轻确认"。走 `store.set_authority` 状态机（DRAFT → REVIEWED 合法，禁越级 AUTHORITATIVE），不绕过任何规则 |
| `scripts/kdl_acceptance.py` | 阶段 1 退出条件验收套件（§8） |

---

## 8. 验收：阶段 1 退出条件

`scripts/kdl_acceptance.py` 对**真实 KDL 库**跑 4 项可测验收，产报告 + PASS / FAIL。已跑过并签署。

| # | 指标 | 目标 | **实际数值（2026-06 签署时）** |
|---|---|---|---|
| 1 | 金标 Top-5 命中率 | ≥ 0.85 | **0.938** ✅ |
| 2 | 溯源抽样可点回率 | 100% | **100%** ✅ |
| 3 | 对外发送计数 | = 0 | **0** ✅ |
| 4 | 脱敏抽检 误 / 漏 | 均 = 0 | **0 / 0** ✅ |

**金标"自检式"**：以每个 KU 的 title 作查询，期望该 KU 落在 Top-5 检索结果里，量化"检索能否召回已知知识"。真实自然语言查询金标可由本人后续补充，跑同一 harness（`scripts/kdl_acceptance.py`）。

**灌入量**：7289 KU 覆盖 Workspace 全部 git 仓库（CODE 主体）+ 本人钉钉文档（PLAYBOOK/ISSUE）。

---

## 9. 本期不做（推后 / 保留扩展点）

以下条目**设计里覆盖了但代码里未启用**——不是遗漏，是刻意留在未来：

| 条目 | 状态 | 何时启用 |
|---|---|---|
| **CODE 语义蒸馏**（契约卡 / 行为卡 / 已知坑卡） | **未启用**——当前 CODE 只做索引 + 原文切片。设计中通过 `linked_symbols` 反向引用 + `ku_edge` stale 传播的框架已就绪 | 需要"直接调权威契约卡回答"、且 Claude 每次读原文成本太高时；成本预估 Workspace 全量至少几十美元/轮 |
| `LlmDistiller`（外部 LLM 后端） | 保留 `_BACKENDS['llm']` 插入点，本期不实现 | 需要无人值守蒸馏时；接 `claude -p` headless 且钉死"只产 JSON、零副作用" |
| 后台轮询 SLA 守护 | `dwsd` 有 `_kdl_tick` 钩子但只跑巡检；不做后台自动蒸馏 | 代答规模化后（见 [方案-MVP.md](方案-MVP.md) 阶段 4） |
| 对话/QA 抢答污染检测（`pair_qa`） | 已有强校验框架（回复必须真的出自本人 account，无中间插话），本期未上线大规模 QA 摄取 | QA 数据量上来后 |
| 权威升级到 AUTHORITATIVE 的批量流程 | 目前只有 DRAFT → REVIEWED 批量脚本；升 AUTHORITATIVE 走单条本人确认 | 当有真实需要"权威档"来源时逐条升；**注意** CODE 卡目前 body 是原文，升 AUTHORITATIVE 意义有限，等语义蒸馏启用后才真正有价值 |
| 域路由 + 采样回查 | 设计里在阶段 5，代答尚未规模化不谈这个 | 阶段 5 |

---

## 附录 A. 表结构清单

（`store.py` 内 `CREATE TABLE IF NOT EXISTS`，共用 phase 0 的 `state.db`）

| 表 | 用途 | 关键字段 |
|---|---|---|
| `ku` | KU 主表 | `ku_id, source_type, title, body_cipher, taint, authority, freshness, public_ok, serve_blocked, confidence, repo, commit_sha, file_path, symbol, line_range, content_hash, ...` |
| `ku_provenance` | 每 KU N 条 provenance | `ku_id, kind, ref, quote_cipher, quote_taint, captured_at, retrievable` |
| `ku_symbol` | L1 符号索引（CODE 精确查） | `repo, file_path, symbol, ku_id` |
| `ku_edge` | KU 之间的引用图（ISSUE↔CODE / QA↔CODE） | `src_ku_id, dst_ku_id, edge_type` |
| `kdl_meta` | 元数据 / 游标 | `key, value` |
| `ku_inverted` + FTS5 虚表 | L2 词汇 / 全文检索 | 按 bigram tokenize 建索引 |

---

## 附录 B. 关键决策记录（2026-06-22）

本人确认，冲突处以本表为准：

| # | 决策项 | 结论 |
|---|---|---|
| **D1** | `source_type` 是否新增 DOC | **复用现有 4 枚举**（CODE / ISSUE / QA / PLAYBOOK）；文档块映射到 PLAYBOOK / ISSUE，不新增 |
| **D2** | 文档摄取范围 | **仅本人创建**的文档（`creator_uids=[__SELF__]`，运行时 `contact user get-self` 解析本人 uid） |
| **D3** | 蒸馏的 LLM 后端 | **Claude（当前会话）即 distiller**，不对接外部 LLM API |
| **D4** | `my_account` / `allowed_groups` 来源 | **统一 `scope.yaml` 的 chat 段**，且与 Executor 侧单聊过滤**共享同一来源**，避免两处漂移 |

**自决默认**（非拍板项，可后改）：
- 同步频率：CODE 30s / 文档 600s / 对话 600s / reindex 6h（落 `config.py`）
- `make_ku_id` 跨 commit 维持现状（`prov_ref=HEAD`）
- `--changed-only` 本期粗粒度

---

> **写在最后：** 阶段 1 的核心成就不是"接了几个源"，是**证明了"三段式 · 无 LLM 落库 · 硬约束由代码结构强制"这条路径可行**——蒸馏可以引 Claude，但落库/检索/对外必须 100% 无 LLM。这条路径是后续所有平台化能力（Worker 派活、subagent 协同、复盘反哺）能挂上同一底座的前提。灌 7289 KU 只是它的第一次真实拉练。
