# KDL 数据接入方案 — 子方案详细设计（附录）

> 本文件是 workflow 四路并行设计 agent 的**原始详细产物**，作为 [kdl-数据接入方案.md](kdl-数据接入方案.md)（综合版）的细节附录备查。综合版与本附录如有冲突，以综合版为准。

---

# 子方案：kdl CODE 源全量索引接入（kb ingest --repo / --scope）

**摘要**：把已实现但未接线的 code.GitReader.index_repo() 接进 Ingestor 落库链路：新增 kb ingest --repo <path> 与 kb ingest --scope <scope.yaml>，对 ~/Myspace 下 git 仓库做全量、确定性（无 LLM）符号索引，产 CODE-KU。index_repo 产出的候选 dict 已结构化完整（含 source_type/title/body/repo/commit_sha/file_path/symbol/line_range/content_hash + 至少 1 条 COMMIT provenance），可直接绕过 LLM Distiller 交给 Ingestor.ingest_candidates；为与 stub 路径保持「同一落库入口」，新增一个零副作用 PassthroughDistiller（注册进 _BACKENDS）使 RawItem(meta=candidate) 原样透出，CLI 两条路径最终都汇聚到 Ingestor。幂等基于 store.upsert_ku 的 ku_id 主键 ON CONFLICT；新增 scope.yaml（pyyaml，复用 policy/loader 模式）做多仓库枚举与包含/排除/扩展名规则；新增 kdl_meta['last_indexed_commit:<repo>'] 写入补缺口；并补 ku_symbol.repo_path 解析缺口的两个可选项。ingest 只建库、reindex 只维护新鲜度，职责严格分离；body=源码切片，不调 LLM，契约卡留作后续扩展点。

**遗留问题**：
- dws 只读子命令名：CODE 源不经 dws，故本方案无需任何 dws 子命令；钉钉文档源(子方案①)涉及的 doc read/search/list/info 名以 policy.yaml r0_whitelist 为准，已确证，不在本子方案范围。
- ku_id 跨 commit 幂等语义：是否要让『同符号同源码切片在不同 commit 下』映射同一 ku_id？当前 make_ku_id 吃 provs[0].ref=HEAD SHA 导致跨 commit 必变。改法(provenance ref 用 file#symbol 稳定指针，或 make_ku_id 不吃 commit)属全局契约改动，需产品/契约确认；本方案默认『ingest 建库一次 + reindex 跟踪 HEAD』规避，不擅改 make_ku_id。
- repo 名->工作树路径注册表：是否在 model/store 增 repo_path 字段或用 kdl_meta['repo_path:<repo>'] 旁表，以让 retrieve 查询时懒校验对全量索引的 CODE-KU 生效？涉及 retrieve._repo_path_for 增强，建议与检索侧子方案统一拍板。
- --changed-only 的『变更』定义：phase1 先实现为『按当前 HEAD 文件存在性过滤』，还是要基于 last_indexed_commit 做 git diff 级增量(需 GitReader 增 diff 能力，超出现有 ALLOWED 白名单——log 可用但 diff 不在白名单)？若要真增量需评估是否给 GitReader.ALLOWED 增 'diff'(仍只读)。
- scope.yaml 默认落点：是放包内默认(同 policy.yaml 模式由 init 拷到 $DWS_AGENT_HOME/policy/)还是仅显式 --scope 传路径？建议默认仅显式传，避免与 policy 混淆；待确认是否需要 $DWS_AGENT_HOME/kdl/scope.yaml 缺省路径与 init 拷贝。

## 0. 范围与定位

把 `code.GitReader.index_repo()`（已实现、未接线）接入既有落库链路，新增两条 CLI：
- `dws-agent kb ingest --repo <path>`：单仓全量索引。
- `dws-agent kb ingest --scope <scope.yaml>`：多仓库（对齐 MEMORY「~/Myspace 全部 git 仓库」）全量索引。

核心判断：**这是「索引」而非「蒸馏」**——`body = 源码切片`，不调 LLM，不产契约卡。`index_repo()` 产出的候选 dict 已是 `ingest.Ingestor.validate` 的完整合法输入（实测字段见下），因此 **CODE 路径直接绕过 LLM Distiller**。为不另造入口、与 `--input`(stub) 路径保持「同一落库口」，新增零副作用 `PassthroughDistiller` 作为「已结构化候选」的统一旁路；两条 CLI 路径最终都汇聚到 `Ingestor.ingest_candidates -> store.upsert_ku`。

---

## 1. 复用的真实签名（已逐一核对源码）

- `GitReader.index_repo(repo_path=None) -> list[dict]`（code.py:493）。`repo = self.repo_path.name`；`head = self.head_sha()`；遍历 `list_files()`（`git ls-files` 过滤 `_INDEXABLE_EXTS`，回退 `_walk_files` 用 `_SKIP_DIRS`）；优先 `read_at(head, rel)` 取 pinned-commit 内容，回退 `working_read`；每符号经 `_candidate` 产 1 条 dict。
- `_candidate` 产物（code.py:526）字段：`source_type='CODE'`、`title=f"{rel}::{name} ({kind})"`、`body=描述+源切片`、`repo`、`commit_sha=head(全SHA)`、`file_path=rel`、`symbol`、`line_start/line_end`、`line_range=(s,e)`、`content_hash`、`freshness=FRESH(有head)/UNKNOWN`、`provenance=[{kind:'COMMIT', ref: head or 'WORKTREE', quote: 源切片, captured_at, retrievable:True}]`。**注意**：provenance.ref 是 **HEAD 全 SHA**，不是 `commit:file#symbol`（后者只在 StubDistiller._symbol_provenance 路径）。
- `Ingestor(self, paths, conn, key)`（ingest.py:67），入参顺序固定。`ingest_candidates(cands, default_taint='INTERNAL') -> IngestReport`（ingest.py:125）。`validate`（ingest.py:81）对 CODE 不额外强校验 repo/file_path/symbol（那是 candidate_from_json/schema 的事；ingest 只校 source_type/title/body/usable-provenance）——但 index_repo 本就带齐，幂等键也依赖 symbol/content_hash，故必须保留这些字段。
- `make_ku_id(st, provs[0].ref, symbol, content_hash)`（model.py:310）。CODE 路径 prov_ref=HEAD SHA、symbol/content_hash 来自候选。
- `store.upsert_ku(conn, ku, key)`（store.py:244）：`ON CONFLICT(ku_id) DO UPDATE`（幂等覆盖），body/quote AES-GCM，重写 ku_provenance / ku_symbol / FTS。
- `kdl_status`（store.py:834）读 `kdl_meta LIKE 'last_indexed_commit:%'`，但**全库无任何位置写它**（确证缺口）——本方案补 `set_indexed_commit`。
- `cli.cmd_kb_ingest`（cli.py:149）现有 `--input -> get_distiller -> RawItem -> distill -> Ingestor`；`_open_conn/_enc_key/_audit` 复用。
- `policy/loader.py:178` 的 `yaml.safe_load` 模式 + `pyproject` 已含 `pyyaml>=6.0`——scope 加载直接照搬。

---

## 2. 候选已结构化 → 可绕过 LLM Distiller（论证）

`index_repo()` 每条候选已满足 `Ingestor.validate` 全部硬条件：`source_type='CODE'`(合法)、`title`/`body` 非空、`provenance` 含 1 条 `kind='COMMIT' + ref=非空(HEAD/WORKTREE)`。因此**无需任何蒸馏**即可直接 `ingest_candidates`。

为「与 stub 路径一致入口」，两种等价接法（本方案采用 (B) 为主、(A) 为内部直连）：
- (A) **直连**：`cmd_kb_ingest` 在 `--repo/--scope` 分支直接调 `code_index.index_paths(...)`，其内部 `Ingestor.ingest_candidates(index_repo产物)`，完全不经 distiller（最短路径、零额外抽象）。
- (B) **passthrough 对齐**：保留「RawItem -> get_distiller -> distill -> Ingestor」骨架，但用 `get_distiller('passthrough')`，把 `index_repo` 候选塞进 `RawItem(source_type='CODE', text='', meta={'candidates':[...]} )`，PassthroughDistiller 原样吐回。语义上「与 --input 同一入口」，证明「LLM 是可选环节、落库恒由 Ingestor」。

> 决策：CLI 走 (A)（清晰、少一次包装），同时实现 (B) 的 PassthroughDistiller 以满足「保持与 stub 路径一致入口/可经 passthrough distiller」的显式诉求，并供 `--input` 喂「已成型候选」时复用。两者落库口完全相同。

---

## 3. scope.yaml 格式与加载

### 3.1 样例（src/dws_agent/kdl/scope.example.yaml）
```yaml
# KDL CODE 源全量索引范围。纯只读枚举，绝不触网、绝不调 dws。
version: 1

# 批量大小：每批交一次 Ingestor.ingest_candidates 后提交，控大库内存/事务。
batch_size: 200

# 仓库来源（roots 取一层子目录；repos 为显式工作树路径）。仅含 .git 的目录入选。
roots:
  - ~/Myspace            # 展开为其下每个含 .git 的子目录

repos:                   # 额外显式补充（可与 roots 重叠，去重）
  - ~/Myspace/dingding-agent

# 包含/排除（对「仓库名」做 glob；exclude 优先于 include）。
include: ["*"]
exclude:
  - "*-archive"
  - "tmp-*"

# 可索引扩展名（缺省 = code._INDEXABLE_EXTS 全集）。可全局或 per-repo 覆盖。
extensions: [".py", ".pyi", ".ts", ".tsx", ".js", ".go", ".rs", ".java"]

# 额外跳过目录（与 code._SKIP_DIRS 取并集）。
skip_dirs: [".git", "node_modules", "__pycache__", ".venv", "dist", "build", ".idea"]

# per-repo 覆盖（可选）：name 命中后覆盖该仓的 extensions/skip_dirs。
overrides:
  TalkTrade:
    extensions: [".py"]
    skip_dirs: ["fixtures", "testdata"]
```

### 3.2 加载逻辑（scope.py，镜像 policy/loader）
1. `yaml.safe_load(open(path))`，缺失 -> `ScopeError`。
2. `roots` 每项 `expanduser/resolve` 后列一层子目录；`repos` 每项 expanduser/resolve。
3. 合并去重为候选路径集；逐个判 `(p/'.git').is_dir()`，否则记入 `repos_skipped(reason='not_a_git_repo')`（由 code_index 收集）。
4. `include/exclude` 对 `Path.name` 做 `fnmatch`；exclude 优先。
5. 每仓 `extensions/skip_dirs` = 全局值，被 `overrides[name]` 覆盖；`extensions` 缺省落 `code._INDEXABLE_EXTS`，`skip_dirs` 始终与 `code._SKIP_DIRS` 取并集。
6. 产出 `Scope(repos=(RepoSpec...), batch_size, source_path)`。

`scope_from_single_repo(repo_path)`：单 `RepoSpec`（全局缺省扩展名/skip_dirs，batch_size 默认），供 `--repo` 复用同一编排器。

---

## 4. 编排器 code_index.py（建库侧）

```python
def index_paths(scope, conn, key, paths, *, changed_only=False, audit=None) -> IndexReport:
    report = IndexReport(repos_indexed=[], repos_skipped=[], per_repo={})
    from dws_agent.executor.refresh_guard import refresh_lock
    with refresh_lock(paths, purpose="kdl-index"):          # 整批串行，复用现成锁
        for spec in scope.repos:
            if not _is_git_repo(spec.path):
                report.repos_skipped.append((spec.path, "not_a_git_repo")); continue
            r = index_one_repo(spec, conn, key, paths,
                               changed_only=changed_only, batch_size=scope.batch_size)
            report.per_repo[spec.name] = r
            report.repos_indexed.append(spec.name)
            report.batches += r.get("batches", 0)
    return report

def index_one_repo(spec, conn, key, paths, *, changed_only=False, batch_size=200) -> dict:
    from dws_agent.kdl.code import GitReader
    from dws_agent.kdl.ingest import Ingestor
    from dws_agent.kdl import store
    reader = GitReader(spec.path)
    head = reader.head_sha()
    cands = reader.index_repo(spec.path)                    # READ-ONLY git
    cands = _apply_scope_filter(cands, spec)                # 扩展名/排除兜底
    if changed_only:
        cands = _filter_changed(cands, reader, conn, spec.name)  # phase1: 文件存在性
    ing = Ingestor(paths, conn, key)
    ingested=dropped=redacted=batches=0; drop_reasons={}
    for batch in _chunk(cands, batch_size):
        rep = ing.ingest_candidates(batch, default_taint="INTERNAL")
        ingested += len(rep.ingested); redacted += rep.redacted_count
        for reason,_t in rep.dropped:
            drop_reasons[reason]=drop_reasons.get(reason,0)+1; dropped+=1
        conn.commit(); batches += 1
    if head:
        store.set_indexed_commit(conn, spec.name, head)     # 补 kdl_meta 写缺口
    return {"head_sha":head,"files":_count_files(cands),"candidates":len(cands),
            "ingested":ingested,"dropped":dropped,"drop_reasons":drop_reasons,
            "redacted":redacted,"batches":batches}
```
要点：
- `default_taint='INTERNAL'`（与 CLI 现状一致）；body 经 redaction 命中密钥即升 SENSITIVE。
- 每批 `conn.commit()` 控事务规模；整批 `refresh_lock` 与 dwsd 巡检串行，避免并发写 state.db。
- 不触碰任何 freshness 状态机标记（mark_stale/expired 等）——那是 reindex 的事。

---

## 5. 幂等 / 去重（与 store upsert 对齐）

- **同一 commit 重复 ingest**：候选字段（含 HEAD SHA、symbol、content_hash）不变 -> `make_ku_id` 不变 -> `upsert_ku` 走 `ON CONFLICT(ku_id) DO UPDATE` 覆盖；ku_provenance/ku_symbol 全删重写 -> **零重复行**。满足题面「重复 ingest 同一 commit 不产生重复 KU」。
- **键来源**：`make_ku_id(CODE, provs[0].ref=HEAD, symbol, content_hash)`。同源切片(同 HEAD+同 symbol+同 hash)恒同 id。
- **跨 commit 的语义注意（重要）**：因 prov_ref=HEAD 全 SHA，HEAD 变化即使源码未改也会得到新 ku_id（旧 KU 残留）。这是设计取舍：**ingest 用于建库一次，跨 commit 的连续性由 `kb reindex` 承担**（reindex 对既有 CODE-KU 在 HEAD 视图 verify_fact，FRESH 时 `mark_fresh_bump_commit` 原地 bump commit_sha、不新增行）。如需 ingest 自身跨 commit 幂等，须改 provenance ref 为稳定指针或调整 make_ku_id 语义——属契约改动，列入 open_questions，本方案不擅改。

---

## 6. 大库性能

- **分批**：`batch_size`（scope.yaml 可调，默认 200）逐批 ingest+commit。
- **跳目录**：复用 `code._SKIP_DIRS`（含 .git/node_modules/.venv/dist/build/.idea…）+ scope per-repo `skip_dirs` 并集；`list_files` 优先 `git ls-files`（天然不含被 ignore/未跟踪垃圾）。
- **仅变更文件（可选）**：`--changed-only`。phase1 实现为「按当前 HEAD 文件存在性 + last_indexed_commit 是否变化」的粗粒度过滤（HEAD 未变直接整仓跳过、记 skip 'unchanged_head'）。真正 git diff 级增量需给 `GitReader.ALLOWED` 评估增 `diff`（仍只读）——列 open_questions。
- **串行**：`refresh_lock(purpose='kdl-index')` 防并发巨写。

---

## 7. provenance / 加密 / 初始状态

- provenance：`kind=COMMIT`，`ref=HEAD 全 SHA`（无 HEAD 时 'WORKTREE'），`quote=源码切片`，`retrievable=True`，`captured_at=_now_iso`。（题面期望的 `commit:file#symbol` 形态属 StubDistiller 路径；index_repo 现状是裸 HEAD SHA，本方案据实复用，不改 code.py 的 ref 形态以免破坏 reindex 的 commit 比对——如需更细 ref 列入 open_questions。）
- 加密：唯一经 `store.upsert_ku` -> body->body_cipher、每条 quote->quote_cipher（AES-256-GCM，nonce||ct||tag，key=fileenc 32B）。明文只在内存。
- 初始：`authority=DRAFT`、`public_ok=False`（Ingestor 强制）、`freshness=FRESH(有HEAD)/UNKNOWN`、`taint>=INTERNAL`。

---

## 8. 与 reindex 的职责边界（强约束）

| 命令 | 入口 | 行为 | 是否新增 KU | 是否改 freshness 状态机 |
|---|---|---|---|---|
| `kb ingest --repo/--scope` | code_index.index_paths | index_repo -> Ingestor.upsert + 写 last_indexed_commit | 是（幂等 upsert） | 否（除同 ku_id 覆盖） |
| `kb reindex --repo` | GitReader.reindex_repo | get_code_kus_for_repo -> verify_fact -> mark_fresh/stale/expired/downgrade/propagate | 否 | 是 |

两者均 READ-ONLY git、不发送、不调 dws 写。推荐工作流：`ingest` 建库一次 → 之后 `reindex`（或 dwsd 定时巡检，挂载点 `cli/dwsd.py` 的 `tick()`，目前未调度 KDL，属后续）维护新鲜度。

---

## 9. 「索引而非蒸馏」+ 契约卡扩展点

- 本路径 `body=源码切片`，**不调 LLM**，不生成「behaviour/contract/pitfall 契约卡」。
- 扩展点（留点，不在本子方案实现）：未来若要契约卡，可在 `distill.py` 增 LLM backend（注册进 `_BACKENDS`，仅产候选 JSON、零副作用），CLI 用 `kb ingest --repo --distiller <llm>` 把 `index_repo` 候选的 `body`/`meta.symbols` 喂给它生成富 `body`，落库仍恒经 Ingestor（redact/taint/DRAFT 不变）。即「索引(本方案) 与 蒸馏(后续)」共用同一 Ingestor 落库口，互不耦合。

---

## 10. CLI 接线（cli.py）

`register_kb` 的 ingest 子命令新增互斥参数：
```python
g = p_ing.add_mutually_exclusive_group(required=True)
g.add_argument("--input", help="source/candidate JSON (distiller path)")
g.add_argument("--repo", help="single git repo working tree (full CODE index)")
g.add_argument("--scope", help="scope.yaml enumerating repos to index")
p_ing.add_argument("--changed-only", action="store_true")
```
`cmd_kb_ingest` 分流：
```python
if getattr(args,'repo',None) or getattr(args,'scope',None):
    from dws_agent.kdl.scope import load_scope, scope_from_single_repo
    from dws_agent.kdl.code_index import index_paths
    scope = load_scope(args.scope) if args.scope else scope_from_single_repo(args.repo)
    conn = _open_conn(paths)
    report = index_paths(scope, conn, _enc_key(), paths, changed_only=args.changed_only)
    # 打印 per-repo head/files/candidates/ingested/dropped/redacted + repos_skipped
    _audit(paths, kdl_op='ingest', reason='code-index repos=%d' % len(report.repos_indexed),
           detail={'mode':'scope' if args.scope else 'repo',
                   'repos': report.repos_indexed})
    return 0
# else: 现状 --input -> get_distiller -> Ingestor（不动）
```
审计沿用封闭词表：CLI 层 `event='cli'/actor='cli'` + `detail['kdl_op']='ingest'`；Ingestor 内部 ingest/drop 仍 `event='privacy_filter'/actor='store'`。**不新增任何 event 名**。

---

## 11. store.set_indexed_commit（唯一 store 新增，无表改动）
```python
def set_indexed_commit(conn, repo: str, commit_sha: str) -> None:
    if not repo or not commit_sha: return
    conn.execute("INSERT OR REPLACE INTO kdl_meta(k, v) VALUES(?, ?);",
                 (f"last_indexed_commit:{repo}", commit_sha))
```
使 `kdl_status` 的 `last_indexed_commit` 真正有数据（补现状缺口）。

---

## 12. 测试落点
见 test_points；全部用 `tests/kdl_helpers`(本地 throwaway 仓 + TEST_MODE 确定性 key)，无网络、无真 dws。重点验证：符号数==ingested、同 commit 幂等、分批等价、脱敏升 SENSITIVE、明文 grep=0、last_indexed_commit 写入、ingest/reindex 互不越界、GitReader 拒写。

---

# 子方案：KDL 真实数据源接入 · 子方案①：钉钉文档知识蒸馏 (dws doc R0 只读 -> RawItem 切块 -> Distiller -> Ingestor -> DRAFT KU)

**摘要**：新增一个只读 doc adapter (src/dws_agent/kdl/doc_source.py)，把钉钉文档经 dws doc R0 白名单 (list/search -> info -> read) 枚举并取正文，按 Markdown 标题层级把长文档切成「章节块」RawItem（每块带 doc nodeId + heading-path 锚点），再交给现有 StubDistiller/Ingestor 落库为 DRAFT KU。source_type 决策：复用现有枚举，不新增 DOC/NOTE——有编号步骤的块走 PLAYBOOK，命中症状/根因/处置标题的块走 ISSUE，其余知识性块默认 PLAYBOOK。Distiller 侧 phase1 仍只用确定性 stub（块已按类型预切，直接调 _distill_playbook/_distill_issue），并在 _BACKENDS 预留 'llm' 后端插入点（输入 RawItem、只产候选 JSON、零副作用、严格 only-JSON 校验失败回退 stub）。provenance kind=DOC_ID、ref=文档 nodeId(dentryUuid)、quote=块原文切片；入库即 redact + taint(MAX) + authority=DRAFT + public_ok=False；SENSITIVE/未核 public_ok 不进对外路径，仅本人确认可经 store.set_authority 升 REVIEWED->AUTHORITATIVE。全程只读：dws 调用只走 shim 的 R0 路径（doc read/search/list/info 已在 policy.yaml R0 白名单，R0 免 gate token），绝不发送、绝不调任何 dws 写命令。

**遗留问题**：
- dws doc 的精确 R0 子命令名与参数已由 policy.yaml + dws skill 确证为 list/search/info/read（--node/--query/--folder/--workspace-ids/--creator-uids/--extensions/--limit/--cursor/--format json）；但仓库内 mock dws 不分派子命令，真实可执行行为需在接真 dws 时校准一次（尤其 read 默认 markdown 的 stdout 包裹格式 / --format json 时的字段名 nodes[].nodeId / contentType / extension）。
- provenance 是否需要章节级（block 级）只读重取指针：dws 现有 doc-block 偏写向，是否存在/可用 block 级只读读取来支持按 heading_path 精确重取？若无，ref 维持 node 粒度（本方案默认）。
- 文档漂移/版本对账策略：phase1 走幂等 upsert 覆盖（不删旧块）。若文档删了某章节，旧 ku 会残留为 DRAFT——是否需要一个 doc 侧的 reindex（对账『本次摄取见过的 ku_id 集合』与库中该 DOC_ID 的 ku 集合，差集标 DEPRECATED/derived_stale）？这需要在 kdl_meta 记录 per-doc 最近摄取的块清单（类比未写入的 last_indexed_commit:<repo> 缺口）。
- scope.yaml 的文档枚举范围由谁界定：是否限定为『本人创建』(creator_uids=本人) 以贴合『数字分身只蒸馏用户自己写过的文档』语义？建议默认 creator_uids=本人 uid，避免摄取他人文档导致污点/归属问题——需确认本人 uid 的获取方式（contact get-self 为 R0 只读）。
- LlmDistiller 的 LLM 客户端走哪条链路与是否允许出网：契约要求除 LLM 推理外零副作用，但 LLM 调用本身是否被允许、经何种受控通道（本地模型/受限 API），需产品侧拍板；phase1 默认不启用（_BACKENDS 注册但 get_distiller 不解析为 llm）。

# 子方案①：钉钉文档知识蒸馏 — 详细设计

## 0. 设计原则与定位
KDL 是「数字分身」的长期可引用记忆。本子方案把**用户写过的钉钉文档**经 `dws doc` 只读摄取、切块、蒸馏，落库为 **DRAFT KU**。它是一个**新的源 adapter**，挂在已实现的 `RawItem -> Distiller -> Ingestor -> store` 流水线前端，**不改动**蒸馏/落库/检索的既有契约。核心取舍：**复用现有 4 个 source_type，不新增 DOC/NOTE**；**phase1 蒸馏仍用确定性 stub**，LLM 仅作为 `_BACKENDS['llm']` 插入点预留。

---

## 1. dws doc 只读调用序列（R0，绝不写）

每篇文档固定三段式（均为 R0 白名单，已由 `policy.yaml` r0_whitelist + `~/.claude/skills/dws/references/products/doc/*.md` 确证）：

1. **枚举 nodeId**（二选一或并用）：
   - `dws doc search --query "<关键词>" [--workspace-ids ...] [--creator-uids ...] [--extensions adoc] --limit 30 [--cursor ...] --format json` → 取 `nodes[].nodeId`
   - `dws doc list [--folder <folderNodeId|URL>] [--workspace <wsId>] [--cursor ...] --format json` → 取 `nodes[].nodeId`
2. **类型确认**：`dws doc info --node <nodeId> --format json` → 读 `contentType`/`extension`；**仅 `contentType=ALIDOC && extension=adoc`（在线文字文档）继续 read**；非 ALIDOC（pdf/docx/图片等）跳过（只读元信息，不 download，避免把二进制/过期附件当正文）。
3. **取正文**：`dws doc read --node <nodeId> --format json`（默认 Markdown）→ 得整篇 Markdown。

**只读保证（两道）**：
- 模块级：`DwsDocReader.ALLOWED = {list,search,info,read}`，`_run_doc` 调 dws 前断言子命令在白名单，否则 `raise PermissionError`（镜像 `code.GitReader` 的结构性 no-write）。
- 进程级：所有 dws 调用经 `python -m dws_agent.executor.shim doc ...`。shim 独立重判：`is_write_command(['dws','doc',...])` 对 list/search/info/read 命中 r0_whitelist → 判 R0 → **免 `DWS_GATE_TOKEN`**；任何写子命令（create/update/delete/move/permission）在 shim 处因缺 token 被拒并 `shim_deny`。

> 为什么不复用 `Executor.execute_intent`：那是 inbox/Intent 写流程入口（会 mint gate token、走 confirm）。KDL 读路径只需 R0，直接以 `Executor._shim_path()` 同形的 `subprocess.run([sys.executable,'-m','dws_agent.executor.shim','doc',*args])` 起子进程即可，不引入任何写路径依赖。

---

## 2. 文档 -> RawItem：Markdown 章节切分策略

`split_markdown_sections(md, *, doc_id, doc_title, max_sections)`：

- **按 ATX 标题层级切**：以 `#`..`######` 为边界，把文档切成「章节块」。每个 H1/H2 起一个块，块体含其下正文直到下一个**同级或更高级**标题；子标题（更深层）内容并入当前块（保留行结构，供 stub 抽编号步骤/分节）。
- **锚点**：每块记 `heading_path`（如 `部署手册 › 环境准备 › 数据库`）与 `section_index`（块在文中序号）。锚点写进 `meta['title']`（= `f'{doc_title} › {heading_path}'`，最终成为 `KU.title`）与 body，**不**进 `provenance.ref`（ref 是文档 node 粒度的可重取指针）。
- **粒度可调（落数据，不写死）**：`max_sections` 上限 + 最小块长合并（过短块并入父节），均作为 `scope.yaml` 项；符合 §3.6「进化只落数据」。
- **附件链接规整**：read 返回的 Markdown 含 OSS 临时附件链接（会过期、且是高熵串易被 redaction 误判 SENSITIVE），切块前用占位符替换 `alidocs*.oss-*.aliyuncs.com/.../att/<id>...`。

每块产出一个 `RawItem`：
```
RawItem(
  source_type = <预判: 'PLAYBOOK' | 'ISSUE'>,   # 见 §3
  text        = <块正文>,
  meta = {
    'doc_id': <nodeId>,                          # provenance.ref
    'title':  f'{doc_title} › {heading_path}',
    'heading_path': '...',
    'section_index': <int>,
    'captured_at': <RFC3339>,
    'provenance': [{'kind':'DOC_ID','ref':<nodeId>,'quote':<块原文切片>,'captured_at':...}],
    'declared_taint': 'INTERNAL',                # 注意键名（见 §6）
  },
)
```

`meta['provenance']` 显式透传，命中 `distill._meta_provenance` 的「explicit wins」分支，确保 ref 始终是 nodeId、quote 始终是块原文。

---

## 3. source_type 决策（结论：复用 PLAYBOOK / ISSUE，不新增枚举）

**预判规则（split 内确定性判定，复用 stub 的同款正则思路）**：
- 块内命中编号步骤（`_STEP_RE`：`1. / 1) / 步骤1: / 第一步`）→ **PLAYBOOK**（SOP/操作手册/方案步骤）。
- 块标题或行内命中 症状/现象 / 根因/原因 / 处置/解决/修复（`_ISSUE_HEADINGS`/`_ISSUE_INLINE`）→ **ISSUE**（问题排查记录）。
- 其余知识性块 → **PLAYBOOK**（默认；PLAYBOOK 的 stub 抽编号步骤，无步骤时 `_distill_playbook` 返回 [] → 该块不产 KU，天然过滤纯叙述噪声；若要保留纯叙述块，可在 stub 无步骤时回退「整块为 1 步」——作为 scope 开关）。

**为何不新增 SourceType.DOC/NOTE**：
1. 现有 4 类已能语义承载文档知识（步骤=PLAYBOOK、排查=ISSUE）。
2. 新增枚举会牵动至少 6 处：`model.SourceType`、`ku.schema.json` enum、`ingest._VALID_SOURCE_TYPES`、`distill.distill` 分派、`store`/`kdl_status` 分组、`retrieve` 硬门槛与打分（按现有 4 类调好）。
3. retrieve 的 abstain/打分对类型不敏感（按 authority/freshness/taint/relevance），新增类无检索收益却增维护面与回归风险。

> 若未来文档知识确需独立检索维度（如「只查 SOP」），更稳妥的路径是给 KU 加一个**非枚举的轻量标签**（如 `owner`/title 前缀约定）而非动 SourceType——但 phase1 不需要。

---

## 4. Distiller：phase1 stub + LLM 后端插入点

### 4.1 phase1（确定性 stub，零 LLM）
块在 split 阶段已按 source_type 预切，故 `get_distiller('stub').distill(raw)` 直接命中：
- PLAYBOOK 块 → `StubDistiller._distill_playbook`：`_extract_steps` 抽编号步骤，body=`1. ...\n2. ...`，provenance 取 `meta['provenance']`（DOC_ID）。
- ISSUE 块 → `StubDistiller._distill_issue`：`_split_issue_sections` 抽 症状/根因/处置，body 三段式规整。

无需改 StubDistiller。亦可直接复用 `ingest.playbook_adapter(docs)`（它内部就是 `StubDistiller().distill(RawItem('PLAYBOOK',...))`）——但本方案走 doc_source_adapter 以统一处理切块+类型预判+附件规整。

### 4.2 LLM 后端插入点（`_BACKENDS['llm']`，phase1 默认不启用）
```
class LlmDistiller:
    name = 'llm'
    def distill(self, raw) -> list[dict]:
        if self._client is None: return []          # 无客户端：安全空（或回退 stub）
        prompt = self._build_prompt(raw)
        out = self._client.complete(prompt)         # 唯一 LLM 触点
        return self._only_json(out)                 # 严格 JSON-only + 逐条 schema 校验
```
- **契约（与 stub 完全一致）**：输入 `RawItem`、输出 `List[dict]` 候选；**零副作用**——不写库、不 send、不调 dws；除 LLM 推理外不联网。
- **only-JSON 校验** `_only_json(text)`：
  1. 必须能解析为 JSON 数组（容忍前后空白；拒绝任何自然语言包裹/markdown fence——可先 strip ```json fence 再 `json.loads`，失败即 `return []`）。
  2. 逐条经 **`model.candidate_from_json`**（ku.schema.json 的 in-Python 镜像）校验：`source_type` 合法、title/body 非空、`provenance>=1` 且每条 `kind∈枚举 & ref 非空`；任一条违约 → 丢弃该条（不抛、不污染其它候选）。
  3. 强制为每条注入/校正 `provenance=[{kind:DOC_ID, ref:doc_id}]`（不信任模型自报 ref，用 `raw.meta['doc_id']` 覆盖，杜绝模型编造指针）。
- **get_distiller 默认仍 stub**：`kdl_settings().distiller` 默认 `'stub'`；只有显式 `DWS_AGENT_KDL_DISTILLER=llm` 或 `--distiller llm` 才选 LLM，未知名回退 stub 并审计。

#### LLM prompt 契约（System 段要点）
```
你是知识蒸馏器。输入是一段钉钉文档章节的纯文本。
只输出一个 JSON 数组，元素为知识候选对象，禁止任何解释/markdown/代码围栏。
每个对象字段：
  source_type: 'PLAYBOOK'（操作步骤/方案）或 'ISSUE'（问题-根因-处置）
  title: 该知识点短标题（<=120 字）
  body:  自包含的知识正文（不要照抄整段，提炼为可复用结论/步骤）
  provenance: [{ "kind":"DOC_ID", "ref":"<占位，将被系统用真实 nodeId 覆盖>" }]
约束：不得编造文档中不存在的事实；无可蒸馏知识则输出 []。不要输出 taint/authority/public_ok。
```
（authority/public_ok/taint 一律由无 LLM 的 Ingestor 决定；LLM 不得自报。）

---

## 5. 落库（完全复用 Ingestor，零新落库逻辑）
`Ingestor(paths, conn, _enc_key()).ingest_candidates(cands, default_taint='INTERNAL')`：
- 逐候选 `validate`（drop: no_provenance/empty_*/bad_source_type，审计 `event='privacy_filter'`）。
- `_build_ku`：body+每条 quote 过 `redaction.redact`；`taint=propagate([body.max_taint, *quote_taints], own=MAX(declared_taint, default))`；`authority=DRAFT`、`public_ok=False`；`ku_id=make_ku_id(source_type, provs[0].ref(=nodeId), symbol=None, content_hash=None)`。
- `upsert_ku`：AES-GCM 加密 body/quote 落 `body_cipher`/`quote_cipher`；重写 ku_provenance；建 FTS/inverted。无 provenance 再次锁 DRAFT+serve_blocked。

**幂等**：`ku_id` 由 `(DOC_ID, 非 CODE 故 symbol/hash 为空)` 决定 → 同一文档同一首条 provenance ref 的块映射同 id。**注意**：非 CODE 的 `make_ku_id` 不含 symbol/content_hash，故**同一文档内多个块的 ref 都是同一 nodeId 时会撞 id**。解决：本方案让 `provs[0].ref` 携带块锚点后缀以保证块级唯一——即 provenance 第一条 ref 用 `f'{nodeId}#{section_index}'`（仍是 DOC_ID kind，重取时按 `#` 前缀取 nodeId 调 doc read）。这把「块级幂等」做实，且不改 make_ku_id 签名。（见 open_questions 关于 block 级重取的权衡——`#index` 是锚点不是 dws 可解析参数，重取时需在 reader 侧 split `#`。）

---

## 6. 脱敏 + 污点 + 权威（硬约束落点）
- **脱敏**：body 与每条 quote 必过 `redact`，`body_redacted=True`。文档常见 email/内网域名/手机号 → INTERNAL；私钥/AKSK/JWT/连接串/高熵 → SENSITIVE 且替换为 `[REDACTED:*]`。
- **污点只升不降**：`default_taint='INTERNAL'`（文档=组织内容）。**关键坑**：声明污点必须写 `meta['declared_taint']` 而非 `'taint'`——`Ingestor._build_ku` 只读 `declared_taint`（`candidate_from_json` 用的 `'taint'` 键 Ingestor 不读）。split 产 meta 时写 `declared_taint`。
- **对外门**：`public_ok` 恒 False；`retrieve.serve(external_facing=True)` 对 `taint!=CLEAN` 丢弃；`is_external_safe` 仅 CLEAN。故 INTERNAL/SENSITIVE 的 DOC KU 默认不进对外路径。
- **权威**：入库恒 DRAFT；升级唯一入口 `store.set_authority`（禁越级、无 provenance 不得升非 DRAFT）。本方案**不提供任何自动升权**，仅本人确认（人工 CLI/后续审阅流）经状态机 DRAFT->REVIEWED->AUTHORITATIVE。

---

## 7. 配置（scope.yaml）
`$DWS_AGENT_HOME/kdl/scope.yaml`（与 CODE 源 `repos:` 并列）：
```yaml
doc:
  workspaces: []           # 知识库 wsId 列表（doc search --workspace-ids / doc list --workspace）
  folders: []              # 文档文件夹 nodeId 或 alidocs folder URL（doc list --folder）
  queries: []              # 关键词（doc search --query）
  creator_uids: []         # 建议默认本人 uid（contact get-self 为 R0）——只蒸馏本人文档
  extensions: [adoc]       # 仅在线文字文档
  max_docs: 500
  max_sections: 200
  keep_prose_blocks: false # 无编号步骤的纯叙述块是否保留为 1 步 PLAYBOOK
  default_taint: INTERNAL
```
`load_scope` 无文件时返回空范围（摄取 0 篇，安全默认）。

---

## 8. CLI / 审计
- **推荐方案A（零新子命令）**：`doc_source_adapter` 产 `List[RawItem]` → `[asdict(r) for r]`（`{source_type,text,meta}`）写临时 JSON → 复用 `dws-agent kb ingest --input <json>`（`_to_raw_item` 已接受该形）。
- 方案B：新增 `kb ingest-doc --scope <scope.yaml> [--distiller]`，内部串 reader+adapter+Ingestor。
- **审计**：复用封闭词表——CLI 动作 `event='cli'/actor='cli'`，`detail['kdl_op']='ingest_doc'`；Ingestor drop/ingest 仍 `event='privacy_filter'/actor='store'`。**绝不新增 event 名**（audit `_VALID_EVENTS` 封闭，未知会被改写为 'cli' 并标 _invalid_event）。
- **串行化**（若挂定时巡检）：`refresh_guard.refresh_lock(paths, purpose='kdl-doc-ingest')`，与 reindex 互斥，复用现成 flock，不另造锁。

---

## 9. 与既有缺口的关系（仅标注，不在本方案修复）
- `ingest._record_edges` 引用的 `store.find_code_ku_id` 未定义 → DOC 源不传 `linked_symbols`、不建 edge，故不触发该路径，无影响。
- `kdl_meta['last_indexed_commit:<repo>']` 只读未写 → 类比地，若后续要做 doc 版本对账（标记已删章节为 DEPRECATED），需在 kdl_meta 写 per-doc 最近摄取块清单（见 open_questions）。

---

# 子方案：KDL 真实数据源接入 · 子方案【对话记录蒸馏 / QA 自动配对】(src/dws_agent/kdl/sources/qa_source.py 新增 + 复用 single_chat / pair_qa / Ingestor)

**摘要**：从钉钉群聊只读拉取消息 → single_chat 单聊硬过滤(仅 group+allowed_groups) → 按线程做确定性 QA 配对(本人 account 强校验 + 拒中间插话抢答, 直接复用已落地的 Ingestor.pair_qa) → RawItem(QA) → StubDistiller → Ingestor 落库为 DRAFT QA-KU, provenance kind=MSG_ID。脱敏/污点全程复用 redaction+taint(群内容至少 INTERNAL, 永不洗白), 一律 DRAFT 仅本人确认可升 AUTHORITATIVE。新增唯一组件是一个无 LLM 的「摄取编排器」qa_source.py: 负责经 dws-shim R0 只读路径调 chat message list/search-advanced、归一消息字段、按 conversation 维护增量游标(kdl_meta 的 qa_cursor:<conv_id> 存边界 createTime)、串行化用现成 refresh_lock。零写、零对外发送、零越级。

**遗留问题**：
- dws chat message list 的输出 JSON 中, 发送者『稳定不可改 account』的确切字段名是什么? skill 脚本只确证了 senderNick(显示名, 不可用于反投毒匹配)与 sender; 需确认 senderStaffId / senderUserId / senderId / openDingTalkId 哪个是稳定标识, 以及 my_account 应填哪种(userId 还是 openDingTalkId)。这是反投毒账号强校验的命门, 必须先确认再编码。
- 单条消息的 message-id 字段名: 写命令(recall)用 openMessageId, 但 list 输出里 msg_id 的确切 key(openMessageId / msgId / messageId)需确证, 以正确填 provenance.ref(MSG_ID)。
- @我 检测的数据形态: 用 chat message search-advanced --at-me / list-mentions 拿『@我』消息更准, 还是在 list 输出里读 atUsers/atUserIds 字段判断? 字段名与结构需确证(影响『@我的疑问句』这类配对信号的实现; 当前方案以 pair_qa 的『紧邻一问一答』为主信号, @我 作为可选增强)。
- 话题(thread)类消息: list 返回含 openConvThreadId 时需先 list 主消息再 list-topic-replies 才是完整线程。QA 配对是否要把话题回复并入同一 messages 序列(按 createTime 归并)再喂 pair_qa? 还是话题消息暂不纳入首版? 需产品确认范围。
- 增量游标语义与定期同步子方案的字段命名对齐: 本方案用 kdl_meta key='qa_cursor:<openConversationId>' value=边界 createTime(字符串)。需与『定期同步』子方案统一(键前缀、value 是 createTime 还是 nextCursor、是否也存 last_synced_at)。chat message list 用 --time(createTime)翻页, 而 search-advanced/list-all 用 nextCursor 翻页, 两类游标语义不同, 需约定统一存储结构。
- allowed_groups 的配置来源: single_chat 需要 openConversationId 白名单。这些 conv id 从哪里读(env / state.db / policy)? 与 Executor 侧单聊过滤用的同一份 allowed_groups 配置应共享同一来源, 避免两处漂移。
- my_account 的来源: CLI --my-account 显式传, 还是从 dws contact get-self / 配置读? 涉及『仅本人回答可蒸馏』的本人身份单一真相源。

## 1. 设计目标与边界

把钉钉群聊里「别人提问 ↔ 我的回答」蒸馏为规范化 QA-KU(DRAFT)，含反投毒。本子方案**不新造架构**：配对算法与落库已由 `Ingestor.pair_qa` + `Ingestor.ingest_candidates` 落地；唯一新增的是一个**无 LLM 的摄取编排器** `qa_source.py`，负责「从钉钉只读拉消息 → 单聊硬过滤 → 喂给现成 pair_qa → 现成 Ingestor 落库 → 维护增量游标」。

硬边界：纯只读、零对外发送、零 dws 写、零越级升权、明文不落盘。

---

## 2. 摄取：dws 只读 + 单聊硬过滤接线

### 2.1 经 dws-shim R0 只读路径拉消息
钉钉消息拉取走**已确证在 R0 白名单**的子命令(policy.yaml line 28-39)：
- 主路径：`dws chat message list --group <openConversationId> --time "<边界createTime>" --limit <N>`（chat.md 已确证：群聊专用、`--time` 之后、`hasMore`+边界 `createTime` 翻页）。
- 可选增强：`dws chat message search-advanced --at-me ...`（@我消息，多维过滤）。
- `chat message list-direct`（单聊）**永不调用**——既被 policy.yaml 故意排除(注释 line 24-25)，又被 single_chat 硬过滤兜底。

调用形态**复用 Executor 既有模式**(executor.py:172-200 `_shim_path`/`_invoke_shim`)：
```python
import subprocess, sys, json, os
def _read_messages_via_shim(paths, conv_id, since_time, *, limit, max_pages):
    out = []
    cursor_time = since_time
    for _ in range(max_pages):
        argv = ["chat","message","list","--group",conv_id,"--limit",str(limit)]
        if cursor_time:
            argv += ["--time", cursor_time]
        # R0 读: 不设 DWS_GATE_TOKEN (shim 对 R0 免 token; line 206)
        env = {k:v for k,v in os.environ.items()}
        proc = subprocess.run(
            [sys.executable, "-m", "dws_agent.executor.shim", *argv],
            env=env, capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            break
        try:
            data = json.loads(proc.stdout)
        except Exception:
            break
        inner = data.get("result", data)                       # 对齐 skill 脚本
        page = inner.get("messages", inner.get("records", []))  # 对齐 skill 脚本
        if not page:
            break
        out.extend(page)
        has_more = inner.get("hasMore", data.get("hasMore", False))
        boundary = _create_time_of(page[-1])                    # 边界 createTime
        if not has_more or not boundary or boundary == cursor_time:
            break
        cursor_time = boundary                                  # 翻页: 下次 --time
    return out
```
> shim 在 TEST_MODE 会拒绝真实 dws（安全特性），故单测对本函数打桩注入 fixture，集成验证用 `tests/mock/dws` 断言 argv 全是 R0 读且 `had_gate_token=False`。

### 2.2 单聊硬过滤（对齐 single_chat.py 真实接口）
拉到的每条原始消息先归一成 `single_chat.classify_message` 期望的形状，再过硬过滤：
```python
def _normalize_for_filter(raw_msg, conv_id):
    return {
        "conversationType": "group",          # list --group 只可能是群; 仍显式标注
        "conversationId":   conv_id,
        "senderId":         _author_of(raw_msg),
        "senderStaffId":    raw_msg.get("senderStaffId"),
        "text":             raw_msg.get("text") or raw_msg.get("content"),
        "refs":             raw_msg.get("refs") or raw_msg.get("references"),
        # 透传供下游用:
        "_createTime":      _create_time_of(raw_msg),
        "_msgId":           _msg_id_of(raw_msg),
    }

admitted = []
for raw in fetched:
    msg = _normalize_for_filter(raw, conv_id)
    verdict = classify_message(msg, set(allowed_groups))   # single_chat 真实接口
    if verdict.kind == "signal":
        admitted.append((raw, msg))
    # else: drop(默认拒); 由 sync_qa 计数, 不另发审计(single_chat 不写审计, 保持现状)
```
这是**双保险**：即便误传单聊会话 id，`conversationType` 非 group 或不在 `allowed_groups` 一律 drop（`classify_message` line 71-87 的 default-deny）。

---

## 3. QA 配对信号 + 反投毒（确定性规则，复用已落地 pair_qa）

配对**不重新实现**——`Ingestor.pair_qa`(ingest.py:309-370) 已是确定性、含反投毒的最终实现。本方案只负责把消息整理成它的入参形状。

### 3.1 喂给 pair_qa 的形状
```python
def _to_pair_messages(admitted):
    msgs = []
    for raw, _ in admitted:
        text = raw.get("text") or raw.get("content") or ""
        # 群内容预脱敏 + taint 提升到至少 INTERNAL (对齐 single_chat.to_signal 语义)
        from dws_agent.privacy.redaction import redact
        from dws_agent.privacy.taint import propagate
        rr = redact(text)
        taint = propagate([rr.max_taint], own="INTERNAL")
        msgs.append({
            "author":  _author_of(raw),         # 稳定 account, 非 senderNick
            "text":    text,                    # 原文(pair_qa 内部再被 Ingestor 脱敏)
            "msg_id":  _msg_id_of(raw),
            "ts":      _create_time_of(raw),
            "taint":   taint,
        })
    msgs.sort(key=lambda m: m["ts"] or "")      # 按 createTime 升序 = 到达顺序
    return msgs
```

### 3.2 配对 + 反投毒算法（伪代码，即 pair_qa 现行逻辑，原样复用）
```
pair_qa(messages, my_account):
    pending = []            # 自我上次发言以来累积的非我消息
    pending_authors = set() # 这些消息的不同作者集合
    candidates = []
    for msg in messages:                      # 已按时间升序
        author, text = msg.author, msg.text
        if text 为空: continue
        if author == my_account:              # 这是我的回复
            # 反投毒核心: 仅当『恰好 1 个不同非我作者』在我上次发言后说过话
            if len(pending_authors) == 1 and pending:
                q = pending[-1]               # 紧邻我回复的那条非我消息 = 问题
                emit QA候选:
                    title = q.text[:120]
                    body  = "Q: {q.text}\nA: {text}"
                    declared_taint = MAX(q.taint, msg.taint, INTERNAL)   # propagate
                    owner = my_account
                    provenance = []
                    if q.msg_id: prov += {kind:MSG_ID, ref:q.msg_id, quote:q.text, captured_at:q.ts}
                    if msg.msg_id: prov += {kind:MSG_ID, ref:msg.msg_id, quote:text, captured_at:msg.ts}
            # 无论配没配上, 我的发言都重置窗口
            pending = []; pending_authors = set()
        else:                                 # 非我消息: 累积
            pending.append(msg)
            pending_authors.add(author)        # 第二个不同作者出现 => 下次配对判定为投毒/歧义
    return candidates
```
**反投毒三条确定性规则**(已实现)：
1. **本人 account 强校验**：只有 `author == my_account` 的消息才被当作「我的回答」。`my_account` 与 `_author_of` 必须用**稳定标识**（非显示名 senderNick），否则可被改昵称伪造（见 open_questions / risks）。
2. **拒中间插话抢答**：自我上次发言后若出现 **≥2 个不同非我作者**，`pending_authors` 长度 >1，**不产候选**（宁可放弃也不误归因）——攻击者无法把恶意消息楔入「问→我答」之间冒充被我回答的问题。
3. **问题 = 紧邻我回复的、当前窗口唯一非我作者的最后一条**（`pending[-1]`），时间邻近天然由到达顺序保证。

> reply/quote 链：当前以「紧邻一问一答 + 单一作者窗口」为主信号（最稳、误报最低）。若要利用显式 quote(`refs`/`references`，single_chat._extract_refs 已能抽)增强（如跨多条精确指向被引用的问题），属增量，列入 open_questions 的话题/引用项。

---

## 4. 蒸馏 → 候选 → Ingestor（落库纪律）

两条等价路径，本方案取**直接 pair_qa**（更直接、零 LLM、与现有测试同款）：
- 路径 A（采用）：`Ingestor.pair_qa(messages, my_account)` 直接产候选 dict 列表。
- 路径 B（等价、可选）：把每对 `{question, answer, msg_id, answer_author}` 包成 `RawItem(source_type="QA", text="", meta={question, answer, msg_id, owner/answer_author})` 喂 `StubDistiller._distill_qa`（distill.py:365-387 已实现，body=`Q:/A:`、provenance kind=MSG_ID）。

落库统一经：
```python
ingestor = Ingestor(paths, conn, key)              # 顺序: paths, conn, key
report   = ingestor.ingest_candidates(cands)       # default_taint='INTERNAL'
```
`ingest_candidates`(ingest.py:125-162) 对每条候选：`validate → _build_ku(redact body + 每条 quote、propagate MAX taint、**authority 强制 DRAFT**、public_ok=False)→ upsert_ku(AES-GCM 落 body_cipher/quote_cipher)`。drop/ingest 各自审计 `event='privacy_filter' actor='store'`。

**body 规范化**：`Q: <question>\nA: <answer>`（pair_qa line 348 / _distill_qa line 376 一致）。
**一律 DRAFT**：`_build_ku` 硬置 `authority=DRAFT`（line 251）；model.__post_init__ 对 DRAFT 置 `serve_blocked=True`；retrieve 对 DRAFT 必 ABSTAIN。
**仅本人确认可升 AUTHORITATIVE**：经 `store.set_authority(DRAFT→REVIEWED→AUTHORITATIVE)`（test_kdl_qa_pairing 已验证「我已确认」流程），qa_source **绝不**调 set_authority。

---

## 5. 脱敏 + 污点（聊天 PII/密钥高发）

三处脱敏，污点只升不降：
1. **摄取归一**（_to_pair_messages）：每条消息 `redact` 评估，`propagate([max_taint], own='INTERNAL')` → 群内容至少 INTERNAL（对齐 single_chat.to_signal:128）。
2. **配对声明污点**（pair_qa）：`declared_taint = propagate([q.taint, a.taint], own='INTERNAL')`。
3. **入库**（Ingestor._build_ku line 189-223）：对 `body` 与**每条 provenance.quote** 再跑 `redact`，最终 `merged_taint = propagate(body_taints, own=_coerce_taint(declared_taint, 'INTERNAL'))`，即「声明 / body脱敏 / 每条quote」取 MAX。

类别→污点（redaction._CATEGORY_TAINT）：私钥/AKSK/JWT/bearer/连接串/高熵 → **SENSITIVE**；email/内网host/手机号 → INTERNAL。命中 SENSITIVE 的 QA-KU 整条 taint=SENSITIVE，`retrieve` 的 `external_facing=True` 路径直接排除（is_external_safe 仅 CLEAN 为真）。明文 body/quote 仅在内存，落盘恒为 AES-GCM 密文。

---

## 6. 增量水位（游标，避免重复摄取）

**存储**：复用 `kdl_meta(k,v)` 表，新增通用助手 `get_kdl_meta/set_kdl_meta`。
**键**：`qa_cursor:<openConversationId>`（与现有 `last_indexed_commit:<repo>` 同表同冒号分段风格；kdl_status 的 `LIKE 'last_indexed_commit:%'` 不受影响）。
**值**：该会话**已摄取到的边界 createTime** 字符串（格式与 `chat message list --time` 一致 "YYYY-MM-DD HH:MM:SS"）。

**算法**：
```
for conv in allowed_groups:
    since = args.since or get_kdl_meta(conn, f"qa_cursor:{conv}") or (now - window_days)
    fetched = _read_messages_via_shim(paths, conv, since, ...)   # 拉 since 之后
    ... 过滤 / pair_qa / ingest ...
    batch_max_ts = max(_create_time_of(m) for m in fetched) if fetched else since
    if not dry_run:
        set_kdl_meta(conn, f"qa_cursor:{conv}", batch_max_ts)    # 推进水位
```
**幂等兜底**：即便游标同秒边界导致少量重复拉取，`ku_id = make_ku_id('QA', 问题msg_id, None, None)` 确定性派生 → 重复 `upsert_ku` 不产生副本。`max_pages` 防单次失控。`dry_run` 不推进游标、不落库。

> 与「定期同步」子方案的字段命名统一点（列入 open_questions）：键前缀 `qa_cursor:`、value 用 createTime（list 语义）还是 nextCursor（search/list-all 语义）、是否并存 `qa_last_synced_at:<conv>`。两类游标语义不同，需在两方案间约定统一存储结构。

---

## 7. CLI / 调度接线

新增 `dws-agent kb qa-sync`（register_kb 内，惰性 import）：
```
dws-agent kb qa-sync --groups <c1,c2> --my-account <acct> \
                     [--since "YYYY-MM-DD HH:MM:SS"] [--window-days 7] [--dry-run]
```
`cmd_kb_qa_sync` 在 `refresh_lock(paths, purpose='kdl-qa-sync')`（refresh_guard 现成咨询锁，dwsd 已用同款）内调 `sync_qa`，打印 `QASyncReport`（fetched/admitted/pairs/ingested/dropped/redacted/cursors），审计 `event='cli' actor='cli' detail['kdl_op']='qa_sync'`。
定时巡检（可选，与定期同步子方案对齐后）：挂到 `dwsd.tick` 或 cron，复用同一 `refresh_lock` 串行化，**不另造调度**。

---

## 8. 文件清单

| 动作 | 路径 | 说明 |
|---|---|---|
| 新增 | src/dws_agent/kdl/sources/__init__.py | 包入口（空/文档） |
| 新增 | src/dws_agent/kdl/sources/qa_source.py | 摄取编排器 sync_qa + 归一/游标/shim 调用（无 LLM） |
| 改 | src/dws_agent/kdl/store.py | +get_kdl_meta/set_kdl_meta（纯 SQL，既有 kdl_meta 表） |
| 改 | src/dws_agent/kdl/cli.py | register_kb +qa-sync 子命令、cmd_kb_qa_sync |
| 新增 | tests/test_kdl_qa_source.py | 过滤/配对/反投毒/幂等/游标/零副作用（打桩 shim） |

不改：model.py、distill.py、ingest.py（pair_qa 原样复用）、single_chat.py、redaction.py、taint.py、policy.yaml、shim.py、audit.py。

---

# 子方案：KDL 知识新增后的定期/增量同步与新鲜度维护（CODE / 钉钉文档 / 对话 三源），落在 dwsd 无 LLM 守护 + launchd 调度上

**摘要**：在已落地的 KDL 之上新增一个无 LLM、纯只读、可插拔蒸馏的「定期/增量同步层」。CODE 走本地 git GitReader.reindex_repo 的三态对账（事件/定时/懒校验三层）；钉钉文档与对话走 dws R0 只读（doc / chat message，经 executor/shim.py R0 免 token 路径）按游标增量拉取，再喂现有 Distiller→Ingestor 落库。新增一张 store.sync_cursor 表记录每个 (source,scope) 的水位（last_synced_marker/at + 退避状态），并补上目前缺失的 kdl_meta['last_indexed_commit:<repo>'] 写入。调度挂在 dwsd.tick()：CODE 高频跟 push（默认每 30s 看 HEAD 变更才 reindex），文档/对话定时（默认 600s）。并发安全复用 executor.refresh_guard.refresh_lock（新 purpose='kdl-sync'）串行化，避免与 token 刷新及多实例竞争。漂移联动、删/不可达→EXPIRED 全部委托现有 store helpers；幂等去重靠 make_ku_id 确定性 id + upsert + 游标推进；每轮写审计、失败指数退避；全程绝不触发任何对外发送（CODE 不调 dws，文档/对话只调 doc/chat 的 R0 读）。

**遗留问题**：
- dws doc 子命令是否支持服务端「按更新时间/版本增量」过滤（如 doc search 的 since/updated-after 或 doc list 的修改时间）？若不支持，文档增量只能全量列举 + 客户端按 updatedAt 裁剪。精确 flag 需对照 ~/.claude/skills/dws/references/products/doc/{doc-read,doc-search,doc-list,doc-info}.md 确证，仓库内无法确定。
- dws chat message list 的增量游标形态：是 since msg_id、since ts、还是 cursor 分页 token？字段名（msgId/messageId、createTime/ts）与单页上限 limit 上限值，需以 dws skill / policy.yaml 实测确证；事实清单只确证了子命令头 'chat message list/search'。
- my_account（本人在钉钉的 sender_id/account）从何处取？qa_adapter/pair_qa 需要它判定「本人回复」。是否有现成 contact/whoami 只读能力或配置项（DWS_AGENT_MY_ACCOUNT）？
- 对话增量的 allowed conversations 来源：复用 privacy.single_chat 的 allowed_groups 配置吗？该白名单当前存放位置/读取入口未在本次涉及文件中确证。
- refresh_guard 是否接受新增「独立锁文件名」参数（推荐方案）来分离 kdl-sync 与 dwsd-instance/token-refresh 锁？还是约定 KDL 同步只在 dwsd 进程内跑、永不在 dwsd 运行时并发独立 kb reindex？需本人在「单锁复用 vs 多锁文件」间拍板（影响 risks#1）。
- CODE 第①层是否要从「轮询 HEAD」升级为真正的 push/merge 事件（git hook / CI webhook）？本阶段按无 LLM、无外部依赖给出轮询近似；事件源接入属下一步。
- retrieve 懒校验 repo 名→路径缺口的修法（model_changes 方案 A 写 kdl_meta repo_path vs 方案 B 新建 repo_registry 表）选哪个？默认建议 A，但牵涉到改 retrieve._repo_path_for，需确认是否纳入本子方案范围。
- DWS_AGENT_KDL_SYNC_* 各默认间隔（CODE 30s / 文档 600s / 对话 600s）与 reindex_max_age（建议 6h）是否合适？应作为 KdlSettings 扩展项落数据，具体阈值待本人定。

# KDL 知识新增后的定期/增量同步 — 详细设计

> 原则：**调度全程无 LLM**；蒸馏走可插拔 `Distiller`（phase1=stub，唯一可能用 LLM 处，零副作用）；落库唯一走无 LLM 的 `Ingestor`；**绝不对外发送**；CODE 不调 dws，文档/对话只调 dws **R0 只读**。复用既有 `reindex_repo / refresh_guard / dwsd / store helpers`，不另造架构。

## 1. sync_cursor 表结构（追加进 store.KDL_SCHEMA_SQL，幂等）

```sql
CREATE TABLE IF NOT EXISTS sync_cursor (
    source              TEXT,        -- 'CODE' | 'DOC' | 'CHAT'
    scope               TEXT,        -- repo 名 / workspace-id 或文件夹 id / conversation_id
    last_synced_marker  TEXT,        -- CODE=head_sha ; DOC=updatedAt 或版本 ; CHAT=msg_id 或 ts（单调水位）
    last_synced_at      TEXT,        -- RFC3339 UTC，本 scope 上次"成功跑完"的时间（定时判定用）
    last_run_at         TEXT,        -- 上次"尝试"时间（无论成败）
    status              TEXT,        -- 'OK' | 'ERROR'
    error               TEXT,        -- 最近一次失败原因（截断；不含 body/quote）
    attempts            INTEGER,     -- 连续失败次数（成功清零），用于指数退避
    backoff_until       TEXT,        -- 在此时间前不重试（NULL=可立即重试）
    PRIMARY KEY (source, scope)
);
CREATE INDEX IF NOT EXISTS idx_sync_cursor_source ON sync_cursor(source);
```

- 只存「水位 + 调度/退避元数据」，**绝无** body/quote/明文内容（加密只针对 KU，由 upsert_ku 负责）。
- 与 `kdl_meta['last_indexed_commit:<repo>']` 分工：sync_cursor 是「调度水位」（每源每 scope，含退避）；kdl_meta 是「展示用每仓最近索引 commit」（kdl_status 读）。CODE 成功 reindex 后两者都写。

## 2. store 新增 helper（纯 SQLite，无 key/无网络/无发送）

```python
def get_cursor(conn, source, scope):
    r = conn.execute("SELECT * FROM sync_cursor WHERE source=? AND scope=?;",
                     (source, scope)).fetchone()
    return dict(r) if r else None

def upsert_cursor(conn, source, scope, *, last_synced_marker, last_synced_at,
                  status="OK", error="", backoff_until=None, attempts=0):
    conn.execute("""
        INSERT INTO sync_cursor(source,scope,last_synced_marker,last_synced_at,
                                last_run_at,status,error,attempts,backoff_until)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source,scope) DO UPDATE SET
            last_synced_marker=excluded.last_synced_marker,
            last_synced_at=excluded.last_synced_at,
            last_run_at=excluded.last_run_at,
            status=excluded.status, error=excluded.error,
            attempts=excluded.attempts, backoff_until=excluded.backoff_until;
    """, (source, scope, last_synced_marker, last_synced_at, _now_iso(),
          status, error[:500], attempts, backoff_until))

def bump_cursor_failure(conn, source, scope, error, *, base_s=60.0, cap_s=3600.0):
    cur = get_cursor(conn, source, scope) or {}
    attempts = int(cur.get("attempts") or 0) + 1
    delay = min(cap_s, base_s * (2 ** (attempts - 1)))      # 指数退避，封顶
    backoff_until = _iso_plus(delay)
    # 关键：失败不动 last_synced_marker（下轮从旧水位重放，幂等安全）
    conn.execute("""
        INSERT INTO sync_cursor(source,scope,last_synced_marker,last_synced_at,
                                last_run_at,status,error,attempts,backoff_until)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source,scope) DO UPDATE SET
            last_run_at=excluded.last_run_at, status='ERROR',
            error=excluded.error, attempts=excluded.attempts,
            backoff_until=excluded.backoff_until;
    """, (source, scope, cur.get("last_synced_marker"),
          cur.get("last_synced_at"), _now_iso(), "ERROR",
          str(error)[:500], attempts, backoff_until))
    return backoff_until

def set_repo_indexed_commit(conn, repo, head_sha):       # 补 kdl_status 只读未写的缺口
    conn.execute("INSERT INTO kdl_meta(k,v) VALUES(?,?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v;",
                 (f"last_indexed_commit:{repo}", head_sha))
```

## 3. 各源频率建议（落数据，KdlSettings 扩展 / 环境变量）

| 源 | 触发方式 | 默认间隔 | 兜底 | 说明 |
|---|---|---|---|---|
| CODE | 跟 push（轮询 HEAD 变更） | `DWS_AGENT_KDL_SYNC_CODE_INTERVAL=30`s | `reindex_max_age=6h`（HEAD 未变也巡检） | 高频；HEAD 未变即 skip，开销极低 |
| 文档 | 定时增量 | `DWS_AGENT_KDL_SYNC_DOC_INTERVAL=600`s | — | dws R0 doc search/read，按 updatedAt 水位 |
| 对话 | 定时增量 | `DWS_AGENT_KDL_SYNC_CHAT_INTERVAL=600`s | — | dws R0 chat message list，按 msg_id/ts 水位 |

退避：单 scope 失败 `base=60s` 指数翻倍封顶 `1h`；成功清零 attempts。

## 4. dwsd 主循环伪代码（在现有 tick 末尾挂 KDL，复用单实例锁）

```python
class Daemon:
    LOCK_PURPOSE = "dwsd-instance"          # 既有：整生命周期持有
    def __init__(self, paths, interval=5, *, kdl_enabled=True,
                 kdl_code_interval=30, kdl_doc_interval=600, kdl_chat_interval=600):
        ...
        self._kdl_last = {"code": 0.0, "doc": 0.0, "chat": 0.0}
        self._kdl_iv   = {"code": kdl_code_interval, "doc": kdl_doc_interval,
                          "chat": kdl_chat_interval}

    def tick(self):
        self._get_executor().run_once()     # phase0 原样：排空 inbox
        if self.kdl_enabled:
            self._kdl_tick()                 # 新增

    def _kdl_tick(self):
        now = time.monotonic()
        due = tuple(k for k, iv in self._kdl_iv.items()
                    if now - self._kdl_last[k] >= iv)
        if not due:
            return
        try:
            from dws_agent.kdl import sync, store
            from dws_agent.store.state_db import open_state_db
            from dws_agent.core.crypto import get_keychain_secret
            from dws_agent.executor import refresh_guard as rg
            conn = open_state_db(self.paths); store.ensure_kdl_schema(conn)
            key  = get_keychain_secret("fileenc")
            # 独立锁文件（推荐）：与 dwsd-instance / token-refresh 分离，
            # 仅与跨进程 kb reindex/kb sync 互斥。timeout=0：拿不到就跳过本轮。
            with rg.refresh_lock(self.paths, timeout=0, purpose="kdl-sync",
                                 lock_file="kdl-sync.lock",
                                 audit=AuditLogger(self.paths)):
                rep = sync.SyncEngine(self.paths).run_due(conn, key, kinds=due)
            for k in due:
                self._kdl_last[k] = now
            self._audit(event="cli", actor="dwsd", reason="kdl sync",
                        detail={"kdl_op": "sync", **rep.as_dict()})
        except TimeoutError:
            pass                              # 另一进程在跑，正常让步
        except Exception as exc:              # 绝不让一次同步杀掉主循环
            self._audit(event="cli", actor="dwsd",
                        reason="kdl sync raised: %s" % exc,
                        detail={"kdl_op": "sync"})
```

> 若不接「独立锁文件」增强，则 `_kdl_tick` **不再二次取锁**（dwsd-instance 已串行化同进程内同步），而把 `kdl-sync` 锁只用在独立的 `kb reindex` / `kb sync` CLI 路径（见 risks#1）。

## 5. SyncEngine 核心伪代码

```python
class SyncEngine:
    def run_due(self, conn, key, *, now=None, kinds=("code","doc","chat")):
        now = now or _now_iso()
        rep = SyncReport()
        if "code" in kinds: rep.per_source["code"] = self.sync_code(conn, key)
        if "doc"  in kinds: rep.per_source["doc"]  = self.sync_docs(conn, key)
        if "chat" in kinds: rep.per_source["chat"] = self.sync_chat(conn, key)
        return rep

    def sync_code(self, conn, key, *, repos=None, force=False):
        from dws_agent.kdl.code import GitReader
        from dws_agent.kdl import store
        repos = repos or self._discover_repos()      # ~/Myspace/* 的 git 仓库（含本项目）
        out = {"examined":0,"reindexed":0,"skipped":0,"failed":0,
               "fresh":0,"stale":0,"expired":0}
        for repo in repos:
            try:
                gr = GitReader(repo); head = gr.head_sha()      # 只读
                cur = store.get_cursor(conn, "CODE", repo) or {}
                changed = head and head != cur.get("last_synced_marker")
                aged = self._older_than(cur.get("last_synced_at"), self._reindex_max_age)
                if not (force or changed or aged):
                    out["skipped"] += 1; continue
                r = gr.reindex_repo(conn, key, repo)             # 三态对账（已实现）
                store.set_repo_indexed_commit(conn, repo, head)  # 写 kdl_meta
                store.upsert_cursor(conn, "CODE", repo,
                                    last_synced_marker=head, last_synced_at=_now_iso(),
                                    status="OK", attempts=0)
                out["examined"] += r.checked; out["reindexed"] += 1
                out["fresh"] += r.fresh; out["stale"] += r.stale; out["expired"] += r.expired
            except Exception as exc:
                out["failed"] += 1
                store.bump_cursor_failure(conn, "CODE", repo, exc)
        return out

    def sync_docs(self, conn, key, *, scopes=None, force=False):
        from dws_agent.kdl.distill import get_distiller, RawItem
        from dws_agent.kdl.ingest import Ingestor
        from dws_agent.kdl import store
        scopes = scopes or self._doc_scopes()        # 配置: workspace-ids / 文件夹
        ing = Ingestor(self.paths, conn, key)
        distiller = get_distiller()                  # stub（可插拔；唯一可能 LLM 处）
        out = {"examined":0,"ingested":0,"dropped":0,"failed":0,"skipped":0}
        for scope in scopes:
            try:
                cur = store.get_cursor(conn, "DOC", scope) or {}
                if not force and store.in_backoff(cur): out["skipped"] += 1; continue
                since = cur.get("last_synced_marker")
                items = self.reader.doc_search(workspace_ids=scope)     # R0
                items = self._filter_since(items, since, key_fn="updatedAt")
                max_marker = since
                for it in sorted(items, key=lambda x: x.get("updatedAt") or ""):
                    body = self.reader.doc_read(node=it["nodeId"])      # R0
                    if body is None:                  # 不可达 → 失效既有 KU
                        self._expire_doc(conn, it["nodeId"]); continue
                    raw = RawItem("PLAYBOOK", body, meta={
                        "doc_id": it["nodeId"], "title": it.get("title"),
                        "captured_at": it.get("updatedAt"),
                        "provenance": [{"kind":"DOC_ID","ref": it["nodeId"],
                                        "captured_at": it.get("updatedAt")}]})
                    r = ing.ingest_candidates(distiller.distill(raw))   # 无 LLM 落库
                    out["examined"] += 1
                    out["ingested"] += len(r.ingested); out["dropped"] += len(r.dropped)
                    max_marker = max(filter(None, [max_marker, it.get("updatedAt")]),
                                     default=max_marker)
                store.upsert_cursor(conn, "DOC", scope,
                                    last_synced_marker=max_marker,
                                    last_synced_at=_now_iso(), status="OK", attempts=0)
            except Exception as exc:
                out["failed"] += 1; store.bump_cursor_failure(conn, "DOC", scope, exc)
        return out

    def sync_chat(self, conn, key, *, conversations=None, force=False):
        from dws_agent.kdl.ingest import Ingestor, qa_adapter
        from dws_agent.kdl import store
        conversations = conversations or self._chat_convs()   # 仅群, 沿用 allowed_groups
        ing = Ingestor(self.paths, conn, key)
        out = {"examined":0,"ingested":0,"dropped":0,"failed":0,"skipped":0}
        for conv in conversations:
            try:
                cur = store.get_cursor(conn, "CHAT", conv) or {}
                if not force and store.in_backoff(cur): out["skipped"] += 1; continue
                msgs = self.reader.chat_message_list(             # R0
                    conversation_id=conv, since_id=cur.get("last_synced_marker"))
                cands = qa_adapter([{"messages": msgs}], self.my_account)  # 反投毒配对
                r = ing.ingest_candidates(cands)
                out["examined"] += len(msgs)
                out["ingested"] += len(r.ingested); out["dropped"] += len(r.dropped)
                marker = self._max_msg_marker(msgs) or cur.get("last_synced_marker")
                store.upsert_cursor(conn, "CHAT", conv,
                                    last_synced_marker=marker,
                                    last_synced_at=_now_iso(), status="OK", attempts=0)
            except Exception as exc:
                out["failed"] += 1; store.bump_cursor_failure(conn, "CHAT", conv, exc)
        return out

    def _expire_doc(self, conn, node_id):
        # 文档被删/不可达：反查绑该 DOC_ID 的 KU，置 EXPIRED + serve_blocked
        from dws_agent.kdl import store
        for ku_id in store.find_kus_by_prov(conn, kind="DOC_ID", ref=node_id):
            store.recheck_retrievable(conn, ku_id, ok=False)
```

> `store.in_backoff(cur)` / `find_kus_by_prov(conn, kind, ref)` 是两个新增小读 helper（前者比较 backoff_until 与 now；后者按 ku_provenance.kind+ref 反查 ku_id，用于文档/对话源失效联动）。`recheck_retrievable` 已存在。

## 6. DwsReadClient（R0 只读拉取，复用 shim）

```python
class DwsReadClient:
    def __init__(self, paths, *, shim_cmd=None, timeout=60.0):
        self.paths = paths
        self.shim_cmd = shim_cmd or [sys.executable, "-m", "dws_agent.executor.shim"]
        self.timeout = timeout
    def _run_readonly(self, argv):                 # argv 不含前导 'dws'
        # R0 读：shim 独立重判白名单、免 DWS_GATE_TOKEN（结构性 no-write）
        p = subprocess.run([*self.shim_cmd, *argv], capture_output=True,
                           text=True, timeout=self.timeout, check=False)
        return p.returncode, p.stdout
    def doc_read(self, node, *, content_format="markdown"):
        rc, out = self._run_readonly(["doc","read","--node",node,
                                      "--content-format",content_format])
        return out if rc == 0 else None
    def doc_search(self, *, workspace_ids=None, query=None, limit=30, cursor=None):
        argv = ["doc","search","--limit",str(limit)]
        if workspace_ids: argv += ["--workspace-ids", workspace_ids]
        if query:         argv += ["--query", query]
        if cursor:        argv += ["--cursor", cursor]
        rc, out = self._run_readonly(argv)
        return self._parse_doc_list(out) if rc == 0 else []
    def chat_message_list(self, *, conversation_id, since_id=None, limit=50):
        argv = ["chat","message","list","--conversation-id",conversation_id,
                "--limit",str(limit)]
        if since_id: argv += ["--since-id", since_id]     # flag 名待确证(open_question)
        rc, out = self._run_readonly(argv)
        return self._parse_msgs(out) if rc == 0 else []
```

- **结构性 no-write**：类内只暴露 doc read/search/list/info + chat message list/search，全部在 policy.yaml r0_whitelist；不存在任何 send/reply/写方法。
- 解析失败 → 返回 `[]`/`None`，由 SyncEngine 视作「本轮无新增」并退避，**绝不臆造候选**。
- TEST_MODE：`DWS_AGENT_DWS_BIN` 指向 `tests/mock/dws`，shim 拒绝真实 dws，可离线测且零钉钉副作用。

## 7. launchd

`render_plist` 无需改（已 KeepAlive+RunAtLoad+注入 DWS_AGENT_HOME+日志到 logs/）。同步间隔由 `dwsd` 启动参数 / `DWS_AGENT_KDL_SYNC_*` 环境变量控制；如需在 plist 注入间隔，仅在 EnvironmentVariables 增对应 env（不改函数签名，调用方传 home 之外的 env 即可）——本子方案默认走 dwsd 默认值，不动 launchd.py。

## 8. 审计（复用封闭词表，绝不新增 event 名）

- 每轮同步：`event='cli'`, `actor='dwsd'`, `detail={'kdl_op':'sync', ...rep.as_dict()}`（CLI 触发时 actor='cli'）。
- Ingestor 内 drop/ingest：原样 `event='privacy_filter'`, `actor='store'`。
- dws 只读 subprocess：shim 自身写 `shim_invoke`（level=R0/decision=AUTO）。
- 锁：`refresh_lock_acquire/release`（refresh_guard 既有）。
- 失败：`event='cli'` + reason 描述 + `detail['kdl_op']='sync'`；未知 event 会被 audit 改写为 'cli' 并标 `_invalid_event`，故严禁自造名。

## 9. 幂等与「绝不对外发送」的结构性保证小结

- 幂等：`make_ku_id(source_type, provs[0].ref, symbol, content_hash)` 确定性 → 同 nodeId/同 msg_id/同代码切片恒同 id → upsert 覆盖；游标「先 ingest 完再推进」+ 失败不推进 → 崩溃重放安全。
- 无发送：CODE 路径只 import GitReader（git 只读白名单）；doc/chat 路径只经 DwsReadClient 的 R0 读（shim 免 token、结构性无写方法）；落库只走 upsert_ku；同步层无任何 inbox 写入、无任何 chat send/reply 调用。`test_kdl_no_side_effects` + MOCK_DWS_LOG 断言可机器验证。