## KDL 阶段1 · 真实数据源接入 — 统一处理方案

> 基线：本方案对 `src/dws_agent/kdl/` 现有 10 个模块（model/store/code/distill/ingest/retrieve/config/cli/`__init__`/ku.schema.json）逐一核对源码后编写。复用 `GitReader.index_repo`/`reindex_repo`、`Ingestor`、`StubDistiller`、`store.upsert_ku`、`refresh_guard.refresh_lock`、`shim`（R0 路径）等既有实现，**不另造架构**。新增代码集中在 4 个新文件 + 对 store/distill/cli 的最小增量。
>
> 全程遵守 7 条 KDL 硬约束（见 constraint_audit）：纯只读、思考/执行分离、provenance≥1、入库即脱敏+污点、权威状态机禁越级、CODE 绑 commit、body/quote AES-GCM 落盘。

---

### 0. 已拍板决策（2026-06-22 本人确认）

> 本节为权威决策记录；下文 §2–§8 与此冲突处，以本节为准。

| # | 决策项 | 结论 |
|---|---|---|
| D1 | source_type 是否新增 DOC | **复用现有 4 枚举**（CODE/ISSUE/QA/PLAYBOOK）；文档块映射到 PLAYBOOK/ISSUE，不新增 DOC/NOTE |
| D2 | 文档摄取范围 | **仅本人创建**的文档（`creator_uids=[__SELF__]`，运行时由 `contact get-self` 解析本人 uid） |
| D3 | 蒸馏的 LLM 后端 | **Claude（本 agent）即 distiller**，不对接外部 LLM API（详见 §0.1） |
| D4 | my_account / allowed_groups 来源 | **统一 scope.yaml 的 chat 段**，且与 Executor 侧单聊过滤**共享同一来源**，避免两处漂移 |

自决默认（非拍板项，可后改）：dws 子命令名实现前对照 skill references 校准；同步频率 CODE 30s / 文档 600s / 对话 600s / reindex 6h（落 config）；`make_ku_id` 跨 commit 维持现状（prov_ref=HEAD）；`--changed-only` 本期粗粒度。

#### 0.1 蒸馏执行模型（D3 落地）

蒸馏（把原始材料"读懂"成知识候选）这一"动脑"动作由 **Claude 在会话内完成**，不引入外部 LLM 服务。三段式，硬约束 2（思考/执行分离）不破：

```
① 摄取（确定性 · 无 LLM · 可后台自动）
   DwsReader 只读拉取文档/对话 → 脱敏 → 存「待蒸馏材料」(RawItem)
② 蒸馏（LLM = Claude · 会话内 · 零副作用）
   Claude 读 RawItem → 产「知识候选 JSON」(契约 / Q-A / 步骤卡)
   只产 JSON：不写库、不发送、不调 dws 写
③ 落库（确定性 · 无 LLM）
   Ingestor 校验 → 脱敏 → 污点 → DRAFT → AES-GCM 落库
```

- **推论**：摄取与 CODE 索引可由 dwsd 全自动；**文档/对话的蒸馏需一个 Claude 会话在场**（非纯后台）。这与 KDL「KU 一律 DRAFT 起步、本人确认才升 AUTHORITATIVE」天然契合——蒸馏时本人在环，顺手轻确认。
- **StubDistiller 的定位**：保留为**确定性离线基线**（规则抽取，供测试与结构极规整的材料），不是语义蒸馏主路径。
- **`LlmDistiller`（外部模型后端）**：本期**不做**；若未来要无人值守，作为 `claude -p` headless 受限调用的可选扩展点（仍须钉死「只产 JSON、零副作用」）。

---

### 1. 总览与文字架构图

三类真实源 → 统一 `RawItem`/候选 dict 契约 → 统一无 LLM `Ingestor` 落库口 → 统一 `store`。摄取触发分「手动 CLI」与「dwsd 定时」两条入口，最终都汇聚到同一落库链路。

```
                         ┌─────────────────────────── 触发层 ───────────────────────────┐
                         │  CLI: dws-agent kb {ingest|reindex|sync|search|draft|status} │
                         │  dwsd: Daemon.tick() -> _kdl_tick()  (定时, 无 LLM)           │
                         └───────────────┬──────────────────────────┬──────────────────┘
                                         │                          │
                  ┌──────────────────────┼──────────────┐           │ (run_due)
                  ▼                      ▼              ▼           ▼
        ┌──────────────┐      ┌──────────────────┐  ┌──────────────────────────┐
  源①   │ CODE 索引     │  源② │ 钉钉文档 doc_source│  源③ 对话 qa_source         │
  适配  │ code_index.py│      │ doc_source.py    │  │ qa_source.py              │
        │ GitReader     │      │ DwsReader(doc)   │  │ DwsReader(chat)+single_chat│
        │ .index_repo() │      │ +split_markdown  │  │ +pair_qa(反投毒)          │
        └──────┬───────┘      └────────┬─────────┘  └────────────┬──────────────┘
   本地 git 只读│         R0 doc read/search/list/info │   R0 chat message list/search│
   (无 dws)     │         经 shim R0 (免 token)        │   经 shim R0 (免 token)     │
               ▼                       ▼                            ▼
        ┌───────────────────────────────────────────────────────────────────┐
        │   候选 dict（统一契约：source_type/title/body/provenance≥1 + ...）  │
        │   CODE 直接产候选(绕过 LLM)；DOC/QA 走 RawItem -> get_distiller()    │
        │   蒸馏（phase1=StubDistiller, 唯一可能 LLM 处, 零副作用, 只产 JSON） │
        └───────────────────────────────┬───────────────────────────────────┘
                                         ▼
        ┌───────────────────────────────────────────────────────────────────┐
        │  Ingestor(paths, conn, key).ingest_candidates(cands, 'INTERNAL')    │  无 LLM
        │  validate -> redact(body+每条quote) -> taint=MAX -> authority=DRAFT  │  规则化
        │  -> public_ok=False -> make_ku_id -> upsert_ku(AES-GCM 落密文)        │
        └───────────────────────────────┬───────────────────────────────────┘
                                         ▼
        ┌───────────────────────────────────────────────────────────────────┐
        │  state.db: ku(body_cipher) / ku_provenance(quote_cipher) /          │
        │  ku_symbol / ku_edge / ku_fts|ku_inverted / kdl_meta / sync_cursor* │  (*新增表)
        └───────────────────────────────┬───────────────────────────────────┘
                                         ▼
        新鲜度维护(独立链路, 不与 ingest 重叠):
        kb reindex / dwsd 巡检 -> GitReader.reindex_repo -> store.mark_* helpers
        retrieve.serve(只读) -> Verdict / assemble_draft(本地预览, 永不发送)
```

**职责红线（强约束）**：`ingest`/`sync(doc/chat)` 只**新增/幂等覆盖 KU**，绝不写 freshness 状态机标记；`reindex`/`sync(code)` 只**维护既有 CODE-KU 新鲜度**，绝不新增 KU。两者都 READ-ONLY、都不发送、都不调 dws 写。

---

### 2. 统一契约

#### 2.1 `RawItem`（不改，复用 `distill.RawItem`）
`RawItem(source_type:str, text:str, meta:Dict[str,Any])`。`meta` 承载适配层已知元数据；`meta['provenance']=[{kind,ref,...}]` 显式透传优先于按源类型合成（`_meta_provenance` 的 explicit-wins 分支，已实证 distill.py:140-146）。

#### 2.2 候选 dict 必填字段（三处校验一致：`candidate_from_json` / `ingest.validate` / `ku.schema.json`）
| 字段 | 必填 | 说明 |
|---|---|---|
| `source_type` | 是 | ∈ `{CODE,ISSUE,QA,PLAYBOOK}` |
| `title` | 是 | 非空 str（入库截断 200） |
| `body` | 是 | 非空 str，明文仅内存 |
| `provenance` | 是 | list，≥1 条「可用」（`kind`∈白名单 且 `ref` 非空） |
| 每条 prov `kind` | 是 | ∈ `{COMMIT,ISSUE_URL,MSG_ID,DOC_ID,MAIL_ID,FILE}` |
| 每条 prov `ref` | 是 | 非空 str（重取指针） |
| `quote`/`quote_taint`/`captured_at` | 否 | quote 默认空，quote_taint 默认 CLEAN |
| `declared_taint` | 否 | **注意键名**：Ingestor 只读 `declared_taint`，不读 `taint`（见 consistency #4） |
| `public_ok`/`confidence`/`owner`/`linked_symbols` | 否 | public_ok 入库恒被强制 False |
| CODE 额外 `repo`/`file_path`/`symbol` | CODE 必填 | `candidate_from_json`/schema 强校验；`ingest.validate` 不另校但 `make_ku_id`/幂等键依赖 symbol+content_hash |
| CODE 可选 `commit_sha`/`content_hash`/`line_range`/`freshness` | 否 | line_range=[start,end] |

#### 2.3 `source_type` 全集（统一结论：**复用 4 枚举，绝不新增 DOC/NOTE**）
- CODE = 代码符号（绑 commit）
- ISSUE = 问题→根因→处置
- QA = 一问一答对
- PLAYBOOK = SOP/步骤/方案
- **文档源映射**：有编号步骤的块→PLAYBOOK；命中 症状/根因/处置 标题的块→ISSUE；其余知识块→PLAYBOOK。理由：新增枚举牵动 model/schema/ingest/retrieve/store/distill ≥6 处，且 retrieve 打分按 4 类调好，新增无检索收益且高回归风险。文档 provenance 用现成 `ProvKind.DOC_ID`（已在三处枚举内）。

#### 2.4 `scope.yaml` 合并格式（统一为**单文件、单 loader**，消解 3 设计冲突）
落点：`$DWS_AGENT_HOME/kdl/scope.yaml`（注意 `core.paths.Paths` **当前无 `kdl_dir`** — 见落地里程碑 M0 必须先补一个 `kdl_dir` property，否则路径无单一真相源）。单一 `kdl/scope.py:load_scope(paths)` 返回一个 `Scope` 聚合对象，含 `code`/`doc`/`chat` 三段；缺文件返回空范围（摄取 0，安全默认）。

```yaml
version: 1

code:                       # 源① CODE（design#1 的 repos 段）
  batch_size: 200
  roots: ["~/Myspace"]      # 取一层子目录，仅含 .git 者入选
  repos: ["~/Myspace/dingding-agent"]   # 额外显式补充（去重）
  include: ["*"]
  exclude: ["*-archive", "tmp-*"]
  extensions: [".py",".ts",".tsx",".js",".go",".rs",".java"]  # 缺省=code._INDEXABLE_EXTS
  skip_dirs: [".git","node_modules",".venv","dist","build",".idea"]  # 与 code._SKIP_DIRS 取并集
  overrides: { }            # per-repo 覆盖 extensions/skip_dirs

doc:                        # 源② 钉钉文档（design#2 的 doc 段）
  workspaces: []            # doc search --workspace-ids / doc list --workspace
  folders: []               # doc list --folder（nodeId 或 folder URL）
  queries: []               # doc search --query
  creator_uids: []          # 建议默认本人 uid（仅蒸馏本人文档；见 gaps）
  extensions: ["adoc"]      # 仅在线文字文档
  max_docs: 500
  max_sections: 200
  keep_prose_blocks: false
  default_taint: INTERNAL

chat:                       # 源③ 对话（design#3 的 QA 段）
  allowed_groups: []        # openConversationId 白名单（single_chat 硬过滤）
  my_account: ""            # 本人稳定 account（反投毒强校验；见 gaps）
  window_days: 7            # 无游标回看窗口
  limit_per_page: 50
  max_pages: 20
```

`Scope` 数据类：`Scope(code:CodeScope, doc:DocScope, chat:ChatScope, source_path:Optional[str])`，各子段为 frozen dataclass。`scope_from_single_repo(repo_path)` 便捷构造单仓 `CodeScope`，供 `kb ingest --repo` 复用同一编排器。

#### 2.5 增量游标 — 统一为 `sync_cursor` 表（采纳 design#4，废弃 design#1/#3 的 kdl_meta 散键方案）
**决策**：所有源的增量水位统一存 `sync_cursor` 表（design#4），而非 `kdl_meta` 散键。理由：(a) 退避/失败计数需要结构化列，kdl_meta 的 (k,v) 装不下；(b) 三源同构、一处查询。`kdl_meta` 仅保留 `last_indexed_commit:<repo>`（被 `kdl_status` 读取的展示用途，补现有写缺口）。

追加进 `store.KDL_SCHEMA_SQL`（随 `ensure_kdl_schema` 幂等建）：
```sql
CREATE TABLE IF NOT EXISTS sync_cursor (
    source             TEXT,   -- 'CODE' | 'DOC' | 'CHAT'
    scope              TEXT,   -- repo名 / workspace-id|folder / openConversationId
    last_synced_marker TEXT,   -- CODE=head_sha; DOC=updatedAt; CHAT=msg_id|createTime
    last_synced_at     TEXT,   -- 上次成功跑完时间(RFC3339)
    last_run_at        TEXT,   -- 上次尝试时间(无论成败)
    status             TEXT,   -- 'OK' | 'ERROR'
    error              TEXT,   -- 截断, 不含 body/quote
    attempts           INTEGER,-- 连续失败次数(成功清零)
    backoff_until      TEXT,   -- 在此前不重试(NULL=可立即)
    PRIMARY KEY (source, scope)
);
CREATE INDEX IF NOT EXISTS idx_sync_cursor_source ON sync_cursor(source);
```
游标字段命名统一：`source`/`scope`/`last_synced_marker`（不再有 `qa_cursor:` 前缀散键）。

---

### 3. 四个子系统的整合设计（消解命名/接口冲突，统一入口）

#### 3.0 统一新增组件清单（消解重复造件）
| 新组件 | 路径 | 来源设计 | 统一后职责 |
|---|---|---|---|
| `scope.py` | `kdl/scope.py` | 1,2,4 合并 | 唯一 scope loader（code/doc/chat 三段） |
| `code_index.py` | `kdl/code_index.py` | 1 | CODE 全量索引编排（建库侧） |
| `doc_source.py` | `kdl/doc_source.py` | 2 | 文档切块 → RawItem（不落库、不蒸馏） |
| `qa_source.py` | `kdl/sources/qa_source.py`→**改为 `kdl/qa_source.py`** | 3 | QA 摄取编排（单聊过滤+pair_qa） |
| `dws_read.py` | `kdl/dws_read.py` | 2,3,4 合并 | **唯一** dws R0 只读封装（doc+chat） |
| `sync.py` | `kdl/sync.py` | 4 | 定时/增量同步编排器（统一三源 run_due） |
| store helpers | `kdl/store.py` | 1,3,4 合并 | `set_repo_indexed_commit` + sync_cursor 5 helper + `find_kus_by_prov` |
| `PassthroughDistiller` | `kdl/distill.py` | 1 | 已结构化候选透传（CODE 用） |
| `LlmDistiller`(骨架) | `kdl/distill.py` | 2 | LLM 后端插入点（phase1 默认不启用） |

> **统一决定**：design#3 的 `kdl/sources/qa_source.py` 子包**取消**，与其它源平级放 `kdl/qa_source.py`（无需为单文件建子包，与 code_index/doc_source 一致）。

#### 3.1 dws 只读统一封装 `DwsReader`（消解 DwsDocReader vs DwsReadClient vs 裸 subprocess 三写法）
唯一封装类 `dws_read.DwsReader`，doc 与 chat 共用，复用 `Executor._shim_path()` 同形调用：
```python
class DwsReader:
    # 结构性 no-write：只暴露白名单只读动作（镜像 GitReader.ALLOWED 思路）
    DOC_ALLOWED  = frozenset({"read","search","list","info"})
    CHAT_ALLOWED = frozenset({"list","search","search-advanced","list-topic-replies"})
    def __init__(self, paths, *, timeout=60): ...
    def _run_readonly(self, argv: list[str]) -> tuple[int,str]:
        # argv 不含前导 'dws'；断言白名单；
        # subprocess.run([sys.executable,'-m','dws_agent.executor.shim', *argv],
        #                capture_output=True, text=True)  # R0 免 DWS_GATE_TOKEN
    # doc
    def doc_search(self,*,workspace_ids=None,query=None,extensions=None,limit=30,cursor=None)->list[dict]
    def doc_list(self,*,folder=None,workspace=None,cursor=None)->list[dict]
    def doc_info(self, node:str)->dict|None
    def doc_read(self, node:str,*,content_format="markdown")->str|None
    # chat
    def chat_message_list(self,*,conversation_id:str,since=None,limit=50)->list[dict]
    def chat_message_search(self,*,conversation_id=None,**kw)->list[dict]
```
- 不经 `Executor.execute_intent`（那是 inbox 写流程、会 mint gate token）。KDL 读路径直接 subprocess 调 shim 的 R0 路径，R0 免 token、shim 独立重判白名单。
- TEST_MODE：`DWS_AGENT_DWS_BIN`→`tests/mock/dws`；shim 拒真 dws。解析失败 → 返回 `[]`/`None`，由调用方视作「本轮无新增」并退避，**绝不臆造候选**。

#### 3.2 子系统①：CODE 全量索引（`kb ingest --repo/--scope`）
- 编排器 `code_index.index_paths(scope.code, conn, key, paths, *, changed_only=False)` / `index_one_repo(...)`：逐仓 `GitReader(repo).index_repo(repo)`（READ-ONLY git）→ scope 扩展名/排除兜底过滤 → 分批 `Ingestor.ingest_candidates(batch, 'INTERNAL')`，每批 `conn.commit()` → 本仓成功后 `store.set_repo_indexed_commit(conn, repo.name, head)`。整批 `refresh_lock(paths, purpose='kdl-sync', lock_file='kdl-sync.lock')` 串行（独立锁文件，见 §3.6）。
- **CODE 候选已结构化完整**（`index_repo._candidate` 实证产 source_type/title/body/repo/commit_sha/file_path/symbol/line_*/content_hash + 1 条 `kind=COMMIT, ref=HEAD全SHA, quote=源切片` provenance），**直接进 Ingestor，绕过 LLM**。为与 stub 路径同一入口，提供 `PassthroughDistiller`（`_BACKENDS['passthrough']`，零副作用透传 `meta['candidates']`），CLI 实际走 code_index 直连（更短），passthrough 供 `--input` 喂已成型候选时复用。
- 幂等：同 commit 重复 ingest → `make_ku_id(CODE, HEAD, symbol, content_hash)` 不变 → `upsert_ku` ON CONFLICT 覆盖，零重复。**跨 commit 注意**：prov_ref=HEAD 全 SHA，HEAD 变即便源码未改也得新 ku_id（旧 KU 残留）—— 这是设计取舍：ingest 建库一次 + reindex 跟踪 HEAD（FRESH 时 `mark_fresh_bump_commit` 原地 bump commit_sha 不新增行）。改 make_ku_id 语义属契约级改动，列 gaps。

#### 3.3 子系统②：文档蒸馏（`kb sync --source doc` 或 `kb ingest --input`）
- `doc_source.split_markdown_sections(md, *, doc_id, doc_title, max_sections)` → 按 ATX 标题层级切块，每块产 `RawItem`，`meta` 含 `doc_id`/`title=f'{doc_title} › {heading_path}'`/`heading_path`/`section_index`/`captured_at`/`declared_taint='INTERNAL'`/`provenance=[{kind:DOC_ID, ref:f'{nodeId}#{section_index}', quote:块原文}]`。
  - **块级 ku_id 唯一性修正（关键，否则同文档多块撞 id）**：非 CODE 的 `make_ku_id` 不吃 symbol/content_hash，仅吃 `provs[0].ref`。故首条 provenance ref 必须带块锚点后缀 `f'{nodeId}#{section_index}'`（仍是 DOC_ID kind；重取时 reader 侧 split `#` 取 nodeId 调 `doc read`）。这把块级幂等做实且不改 make_ku_id 签名。
  - 切块前剥离 OSS 临时附件链接占位（过期 URL + 高熵串易被 redaction 误判 SENSITIVE）。
- `doc_source.doc_source_adapter(scope.doc, reader)` → 枚举 `doc search/list` 取 nodeId → `doc info` 确认 `contentType=ALIDOC && extension=adoc` → `doc read` 取 Markdown → 切块累积 RawItem。
- 蒸馏走 `get_distiller('stub')`（块已按 source_type 预切，命中 `_distill_playbook`/`_distill_issue`）。落库经统一 Ingestor。

#### 3.4 子系统③：对话蒸馏 / QA 配对（`kb sync --source chat` 或 `kb qa-sync`）
- `qa_source.sync_qa(paths, conn, key, *, allowed_groups, my_account, since=None, window_days=7, dry_run=False)`：
  1. `DwsReader.chat_message_list(conv, since)` 翻页拉群消息（R0）。
  2. `_normalize_for_filter(raw, conv)` → `single_chat.classify_message(msg, set(allowed_groups))` 硬过滤（仅 group+白名单，单聊 `list-direct` 既被 policy 排除又被此过滤双保险）。
  3. `_to_pair_messages(admitted)` → `{author(稳定account), text, msg_id, ts, taint}`，每条 `redact` 评估并 `propagate([max_taint], own='INTERNAL')`（群内容至少 INTERNAL），按 ts 升序。
  4. `Ingestor.pair_qa(messages, my_account)`（**原样复用已落地反投毒**：仅本人回复且窗口内恰好 1 个非我作者才配对，≥2 作者插话放弃）→ QA 候选（provenance kind=MSG_ID）。
  5. `Ingestor.ingest_candidates(cands)` 落库 DRAFT。
  6. `store.upsert_cursor('CHAT', conv, last_synced_marker=本批最大marker, ...)`。
- 反投毒命门：`_author_of` 必须抽**稳定 account**（senderStaffId/senderId 之一，非 senderNick），取不到则 author 置空（既不成为「我的回复」也不成为干净问题）。确切字段名见 gaps。

#### 3.5 子系统④：定期/增量同步（`sync.py` + dwsd 钩子）
- `sync.SyncEngine(paths, *, reader=None, my_account=None)`：`sync_code` / `sync_docs` / `sync_chat` + 统一入口 `run_due(conn, key, *, now=None, kinds=('code','doc','chat'))->SyncReport`。
  - CODE：`head_sha()` 与 `sync_cursor.last_synced_marker` 比对，变更或超 `reindex_max_age`（建议 6h）才 `GitReader.reindex_repo(conn, key, repo)` → `set_repo_indexed_commit` → `upsert_cursor`。
  - DOC/CHAT：调 `DwsReader` 增量列举 → RawItem/候选 → `get_distiller().distill()` → `Ingestor.ingest_candidates()` → 按返回最大 marker 推进游标。
  - 文档/对话「被删/不可达」：`store.find_kus_by_prov(conn, kind, ref)` 反查 → `store.recheck_retrievable(conn, ku_id, ok=False)` → EXPIRED+serve_blocked。
  - 每个 scope 独立 try/except + `bump_cursor_failure`（指数退避），单 scope 失败不中断整轮。
- dwsd 挂载（`cli/dwsd.py`）：`Daemon` 增 `_kdl_tick()`，`tick()` 末尾调用（异常吞掉+审计，绝不杀循环）。各源 interval：CODE 30s（跟 push）/ DOC 600s / CHAT 600s，作为 `DWS_AGENT_KDL_SYNC_*` 环境变量。

#### 3.6 并发：`refresh_lock` 自死锁修正（必做，design#4 risk#1 已确认）
`refresh_guard.refresh_lock` 用**模块常量** `LOCK_FILE='refresh.lock'`，`purpose` 只是写进锁文件的记录字段，**不区分锁实例**。dwsd 已持有 `refresh.lock`（purpose='dwsd-instance'），若 `_kdl_tick` 内再 `refresh_lock(...)` 同文件 → 同进程二次 flock 行为不可靠/可能阻塞。**修正**：给 `refresh_lock` 增可选 `lock_file: str = LOCK_FILE` 参数（最小改动，默认行为不变），KDL 同步统一用 `lock_file='kdl-sync.lock'`，与 `dwsd-instance`/token 刷新锁分离，专门与**跨进程**的 `kb sync`/`kb reindex`/`kb ingest` 互斥。

---

### 4. 数据流（统一）

```
摄取(写入候选, 一次性/增量):
  CODE : scope.code -> code_index.index_paths -> [GitReader.index_repo -> 候选] -> Ingestor.upsert
                     -> store.set_repo_indexed_commit + upsert_cursor('CODE')
  DOC  : scope.doc  -> DwsReader(doc R0) -> doc_source.split -> RawItem -> get_distiller().distill
                     -> Ingestor.upsert -> upsert_cursor('DOC')
  CHAT : scope.chat -> DwsReader(chat R0) -> single_chat.classify -> pair_qa -> Ingestor.upsert
                     -> upsert_cursor('CHAT')
  公共落库: validate -> redact(body+每条quote) -> taint=MAX(declared,body,quote) -> DRAFT
           -> public_ok=False -> make_ku_id -> upsert_ku(body->body_cipher, quote->quote_cipher AES-GCM)
           -> ku_provenance 全删重写 -> ku_symbol(CODE) -> ku_fts|ku_inverted 重建
           -> 每条 ingest/drop 审计 event='privacy_filter' actor='store'

新鲜度维护(只改既有 KU, 不新增):
  reindex/sync_code -> GitReader.reindex_repo -> verify_fact(异常->STALE 绝不->FRESH)
       FRESH  -> mark_fresh_bump_commit          STALE -> [AUTH则downgrade_authority(REVIEWED)]
       EXPIRED-> mark_expired_evidence_broken            + mark_stale + propagate_derived_stale
  doc/chat 不可达 -> find_kus_by_prov -> recheck_retrievable(ok=False) -> EXPIRED

检索(纯读, 永不发送):
  serve(conn,key,query,external_facing) -> retrieve(L1 symbol + L2 fts/inverted)
       -> 硬门(EXPIRED/DEPRECATED/serve_blocked丢弃; external丢 taint!=CLEAN) -> 6 条 abstain
       -> Verdict(citations 仅 kind+ref, 无 body/quote)
  assemble_draft(verdict) -> DraftPreview('助理代答(待本人复核)', 本地, 永不发送)
```

幂等总则：`make_ku_id(source_type, provs[0].ref, symbol, content_hash)` 确定性 → 同 (首条 prov ref + symbol + content_hash) 恒同 id → upsert 覆盖；游标「先全部 ingest 完再推进一次」，中途异常不推进 → 下轮从旧 marker 重放（重放因 upsert 幂等而安全）。

---

### 5. CLI 接口全集（统一后）

| 命令 | 现状/新增 | 入参 | 行为 |
|---|---|---|---|
| `kb ingest --input <json> [--distiller]` | 现有，不动 | 候选/源 JSON | `get_distiller -> Ingestor`（含 passthrough 喂已成型候选） |
| `kb ingest --repo <path> [--changed-only]` | **新增**(互斥) | 单仓工作树 | `code_index.index_paths(scope_from_single_repo)` |
| `kb ingest --scope <scope.yaml> [--changed-only]` | **新增**(互斥) | scope.yaml | `code_index.index_paths(scope.code)` |
| `kb reindex --repo <path>` | 现有，不动 | 仓路径 | `GitReader(repo).reindex_repo(conn, key, repo)`（注意实参序 conn,key,repo） |
| `kb sync [--source code\|doc\|chat\|all] [--repo ...] [--force]` | **新增** | — | `sync.SyncEngine.run_due`（手动触发与定时同路径），打印 SyncReport |
| `kb search --query "..." [--external]` | 现有，不动 | 查询 | `serve` → Verdict（仅出处标识） |
| `kb draft --query "..." [--external]` | 现有，不动 | 查询 | `serve`+`assemble_draft`（LOCAL PREVIEW，永不发送） |
| `kb status` | 现有，**增 sync_cursor 汇总** | — | `kdl_status`（+ per-source 游标/退避状态） |

> `kb qa-sync`（design#3）与 `kb ingest-doc`（design#2）**收敛进 `kb sync --source chat|doc`**，避免命令面碎片化；`--source` 缺省 `all`。`kb ingest --input | --repo | --scope` 为三选一互斥组。所有 KDL 同级 import 惰性（函数内）。审计统一：CLI `event='cli' actor='cli' detail['kdl_op']∈{ingest,reindex,sync,search,draft,status}`；dwsd 定时 `actor='dwsd'`；Ingestor drop/ingest `event='privacy_filter' actor='store'`；shim `shim_invoke/shim_deny`；锁 `refresh_lock_acquire/release`。**绝不新增 event 名**（`_VALID_EVENTS` 封闭）。

---

### 6. 落地里程碑（标注 现在就能接 / 需 LLM 后端 / 阶段2+）

**M0 — 公共基建（现在就能接，零 dws/零 LLM，先合）**
1. `core.paths.Paths` 增 `kdl_dir` property（`home/'kdl'`）+ scaffold 建目录。**否则 scope.yaml 无单一真相源落点。**
2. `store.py`：追加 `sync_cursor` 表到 `KDL_SCHEMA_SQL`；新增 `get_cursor/upsert_cursor/bump_cursor_failure/due_cursors/in_backoff` + `set_repo_indexed_commit`（写 `kdl_meta['last_indexed_commit:<repo>']`，补 kdl_status 只读未写缺口）+ `find_kus_by_prov(conn,kind,ref)`。
3. `refresh_guard.refresh_lock` 增可选 `lock_file` 参数（默认 `refresh.lock`，KDL 用 `kdl-sync.lock`）。
4. `kdl/scope.py`：统一 `load_scope(paths)` + `scope_from_single_repo`（code/doc/chat 三段）。
5. `distill.py`：`PassthroughDistiller` 注册 `_BACKENDS['passthrough']`。

**M1 — CODE 源全量索引 + 新鲜度同步（现在就能接，零 dws/零 LLM，最高价值先落）**
6. `kdl/code_index.py`：`index_one_repo/index_paths/IndexReport`，串 `index_repo -> Ingestor -> set_repo_indexed_commit`，整批 `kdl-sync` 锁。
7. `kdl/sync.py`：`SyncEngine.sync_code` + `run_due`（CODE-only 先跑）。
8. `cli.py`：`kb ingest --repo/--scope` + `kb sync --source code`。

**M2 — dws 只读封装 + 文档/对话摄取（现在就能接，依赖真实 dws 子命令名校准）**
9. `kdl/dws_read.py`：`DwsReader`（doc+chat R0），TEST_MODE 走 mock dws。
10. `kdl/doc_source.py`：`split_markdown_sections`（纯函数先单测）+ `doc_source_adapter`。
11. `kdl/qa_source.py`：`sync_qa`（复用 single_chat + pair_qa）。
12. `sync.py`：补 `sync_docs`/`sync_chat`；`cli.py`：`kb sync --source doc|chat|all`。
   - **阻塞点**：真实 dws 子命令的精确 flag（doc 增量过滤、chat since/游标、稳定 account 字段名）需对照 `~/.claude/skills/dws` 校准（见 gaps）；校准前用 mock + 多 key 兜底解析。

**M3 — dwsd 定时调度（现在就能接，依赖 M1/M2）**
13. `cli/dwsd.py`：`Daemon._kdl_tick` + 间隔参数 + `--no-kdl`；`launchd.py` 不改（间隔走 env）。

**外部 LLM 后端（`LlmDistiller`——本期不做；蒸馏已由 Claude 会话内完成，见 §0.1。此项仅为未来 headless 无人值守的可选扩展）**
14. `distill.py`：`LlmDistiller` 注册 `_BACKENDS['llm']`，`_only_json`+`candidate_from_json` 严格校验，强制用 `raw.meta['doc_id']` 覆盖 provenance.ref（不信任模型自报）。`get_distiller` 默认仍 stub；仅显式 `--distiller llm`/env 才选。LLM 出网与受控通道需产品拍板。

**阶段2+**
15. retrieve 懒校验 repo 名→路径映射（写 `kdl_meta['repo_path:<repo>']` + `retrieve._repo_path_for` 增分支），让 CODE-KU 查询时懒校验真正触发（现 repo 存的是名非路径，phase1 仅靠 reindex 维新鲜度）。
16. CODE 第①层从「轮询 HEAD」升级为真正 push/merge 事件（git hook/CI webhook）。
17. `--changed-only` 真 git diff 增量（需评估给 `GitReader.ALLOWED` 增 `diff`，仍只读）。
18. 文档/对话版本对账（删章节→DEPRECATED；需在 sync_cursor/kdl_meta 记 per-doc 已见块清单）。
19. `ingest._record_edges` 依赖的 `store.find_code_ku_id`（**当前未定义**，import 在 try/except 静默 no-op）——若要 ISSUE/QA↔CODE 边生效需补此函数。

---

### 7. 测试矩阵

| 维度 | 用例 | 断言 | 适用源 |
|---|---|---|---|
| 只读·git | GitReader 拒写子命令(commit/push) | raise PermissionError，未起 git | CODE |
| 只读·dws | DwsReader 非白名单(doc create/chat send) | raise PermissionError，未起子进程 | DOC/CHAT |
| 只读·端到端 | 跑一轮三源 | MOCK_DWS_LOG 仅 doc/chat R0 argv，`had_gate_token=False`，无 send/reply/recall | 全 |
| 思考/执行分离 | sync 路径 | 不 import LLM；distiller 始终 stub（未知名回退+审计） | 全 |
| provenance | 缺 nodeId/缺两端 msg_id 块 | Ingestor drop reason='no_provenance'，审计 privacy_filter | DOC/CHAT |
| provenance·CODE | index_repo 每候选 | 恒 1 条 kind=COMMIT ref=HEAD | CODE |
| 脱敏+污点 | 植入 email/内网域名 → INTERNAL；植入 AKSK/私钥/JWT/高熵 → SENSITIVE | KU.taint 升级正确；body_cipher/quote_cipher 为 BLOB，grep 明文=0 | 全 |
| 污点键名 | 候选写 `declared_taint` vs `taint` | 仅 `declared_taint` 生效（Ingestor 不读 taint） | 全 |
| 加密落盘 | upsert 后 sqlite | ku.body_cipher/ku_provenance.quote_cipher 密文；get_ku 解密回明文 | 全 |
| authority | 入库 | 恒 DRAFT + serve_blocked；serve 必 ABSTAIN、draft_text=None | 全 |
| 升权禁越级 | set_authority DRAFT->AUTHORITATIVE | raise ValueError；DRAFT->REVIEWED->AUTHORITATIVE 合法后可 ANSWERABLE | 全 |
| 升权·无provenance | 无 prov KU 升非 DRAFT | raise ValueError（永锁 DRAFT） | 全 |
| 幂等·CODE 同 commit | 同仓连续 ingest 两次 | ku_ids 一致，ku 行数不增，ku_symbol 无重复 | CODE |
| 幂等·DOC 块 | 同 (DOC_ID#index) 重跑 | 同 ku_id，upsert 覆盖，prov 全删重写不翻倍 | DOC |
| 幂等·QA | 同问题 msg_id 重跑 | 同 ku_id，不增 KU | CHAT |
| 分批 | batch_size=1 vs 200 同仓 | 相同 KU 集合（顺序无关） | CODE |
| 单聊硬过滤 | conversationType≠group 或 ∉allowed_groups | 100% drop，永不进 pair_qa | CHAT |
| 反投毒·插话 | bob+mallory 同窗口插话 | 不产候选 | CHAT |
| 反投毒·账号 | 伪造 senderNick 但 account 不符 | 不被当「我的回复」 | CHAT |
| CODE 漂移 | 改符号源切片 | STALE；AUTH→REVIEWED；邻居 derived_stale=1 | CODE |
| CODE 失效 | 删符号 | EXPIRED + serve_blocked + prov.retrievable=0 | CODE |
| DOC 失效 | 上轮 nodeId 本轮不可达 | find_kus_by_prov → recheck_retrievable → EXPIRED | DOC |
| 边界分离 | 纯 ingest | 不改既有不同名 KU 的 freshness | CODE |
| 边界分离 | 纯 reindex | 不新增 KU（ku 行数前后一致） | CODE |
| last_indexed_commit | ingest/sync 后 | kdl_status.last_indexed_commit[repo]==HEAD；reindex FRESH 后 bump 而行数不变 | CODE |
| 游标·语义 | upsert_cursor 后 get_cursor | 回读一致；bump_cursor_failure attempts 递增、backoff 指数封顶；due_cursors 仅返回到期 | 全 |
| 游标·增量 | 首次无游标按 window 回看；二次仅拉 marker 之后；dry_run 不推进 | — | DOC/CHAT |
| 并发·锁 | 持 kdl-sync 锁时第二个 kb sync | timeout 立即让步，写 DENY 审计，不并发改库 | 全 |
| 并发·自死锁 | dwsd `_kdl_tick` 内取 kdl-sync.lock | 不与 dwsd-instance 锁竞争（独立文件） | 全 |
| 调度健壮 | 单 scope 抛异常 | bump_cursor_failure 退避，其余 scope 继续，_kdl_tick 不杀主循环 | 全 |
| CLI 互斥 | `--input` 与 `--repo/--scope` 同给 | 报错退出 | CODE |
| 审计词表封闭 | 全流程 | event 仅 {cli,privacy_filter,shim_invoke,refresh_lock_*}，无 _invalid_event | 全 |
| split | H1/H2/H3 嵌套；编号步骤→PLAYBOOK；症状/根因→ISSUE；空文档退化单块；max_sections 截断 | — | DOC |
| LlmDistiller(若实现) | 模型返回非 JSON/缺字段 | _only_json 拒该条不抛、不污染其它；无 client 不联网回退 | DOC |

全部用 `tests/kdl_helpers`（本地 throwaway 仓 + TEST_MODE 确定性 key）+ 打桩 `DwsReader._run_readonly`/`_read_messages_via_shim` 注入 fixture，无网络、无真 dws。