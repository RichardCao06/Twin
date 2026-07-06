# UAT（测试环境）环境清单

> **用途**：给数字分身的「检索/分诊」与 ClaudeCenter「诊断型 task」提供**环境拓扑指针**——
> 让它知道"大后台在哪、登录走哪条链路、连哪个库、依赖哪些服务"，从而能把"为什么登不上"
> 这类运行时问题转成可执行的诊断任务。
>
> **安全铁律**：本文件**只放非敏感的"去哪连"指针**（taint=INTERNAL）。**任何凭证（密码 /
> token / 密钥 / 连接串里的 password）一律不写**——占位 `⛔走注入`，由执行环境按需注入，
> 不落库、不蒸馏。
>
> **数据来源**：2026-06-23 由 Explore agent 读 `~/Workspace` 各仓 `src/main/resources/
> application-*.yml`、`.env*`、`CLAUDE.md` 提取。标 `⚙️注入` 的是 UAT 走环境变量、代码里
> 无明文的项（实际值去 k8s/部署配置取）；标 `(local)` 的是本地开发硬编码值，仅作参考。
> 标 `❓待确认` 的是没挖到、需你补的。

---

## 0. 元信息

| 项 | 值 |
|---|---|
| 环境名 | UAT / 测试环境 |
| UAT 域名 | `*.hiqdat.dev`（见第 2 节） |
| 维护人 | ❓待确认 |
| 最后更新 | 2026-06-23（自动初稿，待人工校对） |

---

## 1. 服务总览（俗称 → 正式名 → 仓 → 技术栈 → 端口）

| 俗称 | 正式服务 | 仓 | 技术栈 | UAT 端口 | local 端口 |
|---|---|---|---|---|---|
| **大后台** | hiq-backend-admin（`com.hiqadmin`） | hiq-backend-admin | Java 11 / Spring Boot 2.5.15 + Sa-Token | 8080 | 10093 |
| 大后台前端 | HiQ-backend-web | HiQ-backend-web | Vue 3 + Vite | (静态) | 5173 |
| 数据转换 | hiq-data-convertor | hiq-data-convertor(+ -qa 副本) | Java 17 / Spring Boot 3.3 | 8080 | 8091 |
| SSO 认证 | ec-sso / dataset-sso | dataset-sso | Java 8 / Spring Boot 2.5.15 + Sa-Token 1.39 | 8080 | 10096 |
| 数据集后端 | jimu_dataset | dataset | Java 11 / Spring Boot 2.x | 8080(`/api/dataset`) | 8080 |
| 数据集前端 | hiq-lcd-editor-web | dataset-web | Vue 2.7 + Vue CLI | (静态) | 8080 |
| LCA 格式转换（独立项目） | lca-convertor | lca-convertor(+ -dev/-qa 同仓副本) | Java 17 / Spring Boot 3.3.4 | 8080 | 8080 |
| LCA 数据审查（非服务） | lca-check | lca-check | Python + GitHub Issues 工作流 | — | — |

> 备注：
> - `hiq-data-convertor-qa` 是 `hiq-data-convertor` 的 QA 副本，非独立部署。
> - `lca-convertor` / `-dev` / `-qa` 是**同一仓**（`RichardCao06/lca_convertor`）的三个工作树，疑似**独立于 HiQ 主系统**的 LCA 转换项目——与"大后台"业务线不是一回事，注意别混。
> - `iteams` 目前是**空目录**，跳过。

---

## 2. UAT 环境入口地址

| 入口 | 地址 | 来源/备注 |
|---|---|---|
| 数据集系统 UAT | `https://uat1.hiqdat.dev`（uat2/uat3 同构） | dataset-web 代理目标 |
| 数据集编辑器 | `https://editor1.hiqdat.dev` / `editor2.hiqdat.dev` | dataset 取 token 示例 |
| **大后台 UAT 后台地址** | ❓待确认（前端走网关前缀 `/api/backend`，需补完整 URL） | HiQ-backend-web `.env` |
| API 网关前缀 | 大后台 `/api/backend`、SSO `/api/sso`、导出 `/api/data-convertor`、数据集 `/api/dataset` | 前端 `.env` |

---

## 3. 登录与鉴权（诊断"登不上"的核心）⭐

### 3a. 大后台（hiq-backend-admin）登录链路
```
HiQ-backend-web ──POST /api/sso/auth/login──▶ 网关 ──▶ hiq-backend-admin /auth/login
                                                          (Sa-Token SSO 模式二)
   校验用户(库 hiq_admin) → 发 accessToken → 存 Redis 会话(Ticket 有效期 300s)
```
- 登录接口：`POST /auth/login`（登出 `/auth/logout`，注册 `/auth/register`）
  - 源：`hiq-backend-admin/.../auth/controller/AuthController.java:42-50`
- Token 名：`accessToken`；会话存 Redis；登录白名单配置 `AuthUrlWhiteList`
- 鉴权框架：Sa-Token SSO Server 模式二

### 3b. 数据集系统（dataset）登录链路 —— 走独立 SSO 中枢
```
dataset-web ──POST /api/sso/auth/login──▶ dataset-sso（ec-sso）
   dataset 本身不自建登录：LoginInterceptor 校验三件套 header
   Authorization + Cookie(accessToken) + userId；不匹配 → 903 INCONSISTENT
```
- SSO 服务（dataset-sso）登录接口：`POST /sso/doLogin`、`POST /auth/login`；OAuth2：`GET /api/oauth2/authorize`、`POST /api/oauth2/token`
- `AUTH_MODE`：`internal`（默认，Sa-Token 本地认证，用户表在 `hiq_admin`）/ `external`（对接外部 OAuth2）
- Token：`accessToken`，有效期 3 天，Cookie Domain `zgktt.com`（`COOKIE_DOMAIN` 可覆盖）；存 Sa-Token 专用 Redis
- 网关校验端点（内部）：`POST /api/auth/verify`
- 取 token 示例：`POST https://editor1.hiqdat.dev/api/sso/auth/login {username,password,grantType:"PASSWORD"}`（密码 ⛔走注入）

> ⚠️ **两套 SSO 别混**：大后台用自己的 Sa-Token（hiq-backend-admin）；数据集系统用 dataset-sso。李辰说的"大后台"指 hiq-backend-admin → 走 3a。

---

## 4. 数据库总览（地址/库名=INTERNAL；凭证=⛔走注入）

| 库名 | 用途 | 被谁用 | UAT 地址 | local 地址 |
|---|---|---|---|---|
| `hiq_admin` | 用户/认证/租户/角色权限 | 大后台、SSO、dataset | `⚙️注入 ${POSTGRES_HOST}:${POSTGRES_PORT}` | `192.168.8.8:30770` |
| `hiq_square` | 主业务库 | 大后台、data-convertor | 同上 | 同上 |
| `hiq_editor` | 数据集主编辑库（工艺/文档/分类） | dataset | 同上 | 同上 |
| `hiq_basic_data` | 参考数据（地点/流/单位） | dataset、data-convertor | 同上 | 同上 |
| `hiq_background_db` | 背景库（已发布/存档副本） | dataset、data-convertor | 同上 | 同上 |
| ClickHouse（LCI/ecoinvent） | LCI 矩阵数据 | data-convertor（仅 uat1） | `⚙️注入` | `192.168.8.8:32537` |
| MySQL `dev_data_basic` | 外部数据 | data-convertor（仅 uat1） | — | `192.168.20.129:3306` |
| `lca_convertor`（schema `lcd`） | LCA 转换（独立项目） | lca-convertor | `⚙️注入` | `localhost:5432` |

> uat3 的 data-convertor primary 库曾指向外网 `101.89.215.147:5432`（`application-uat3.yml`）。
> 数据库**凭证**：UAT 走 `${POSTGRES_USER/PASSWORD}` ⚙️注入；凭证一律 ⛔走注入（**勿写明文**）。

---

## 5. 中间件（地址=INTERNAL；凭证=⛔走注入）

| 组件 | 用途 | UAT 地址 | local 地址 |
|---|---|---|---|
| Redis | 会话/缓存/队列/分布式锁 | `⚙️注入 ${REDIS_HOST}:${REDIS_PORT}` | `192.168.8.10:31519`（密码 ⛔走注入） |
| Sa-Token 专用 Redis | Token 存储（SSO） | `⚙️注入 ${SATOKEN_REDIS_*}` | 同上（不同 db 号） |
| Kafka | 事件消息（下单/许可/注册用户） | `⚙️注入 ${KAFKA_URL}`（SASL_PLAINTEXT） | — |
| MinIO / 对象存储 | 文件/导出产物 | `https://obs.cn-sh1.ctyun.cn`（bucket `hiq-admin`/`hiq-editor`） | `192.168.8.8:32535` |
| SMTP（SSO 发信） | 邮件 | `smtp.exmail.qq.com:465`，发件人 `info@hiqlcd.com` | 同 |
| H5 矩阵文件 | LCI/LCIA 矩阵 | 本地 `/mnt/h5files`，远程 `192.168.8.8:31680` | 同 |

---

## 6. 服务间调用拓扑

```
[大后台前端 HiQ-backend-web]──/api/backend──▶[大后台 hiq-backend-admin :8080]
                            └──/api/sso──────▶ (大后台自带 Sa-Token SSO)
                                                   │
   ┌───────────────────────────────────────────────┤
   ▼                          ▼                      ▼
 Redis(会话)            PostgreSQL(hiq_admin/        Kafka / MinIO /
                        hiq_square)                  Blockchain(192.168.8.8:30980) /
                                                     PDFForge(192.168.8.8:31760) / Stripe

[数据集前端 dataset-web]──/api/dataset──▶[dataset :8080 /api/dataset]
                        ├─/api/sso───────▶[dataset-sso :8080]──▶ hiq_admin / Sa-Token Redis
                        └─/api/data-convertor─▶[hiq-data-convertor :8080]
   dataset ──gRPC(:8091 calc / local :31481)──▶ lcd-calculation
           ──HTTP──▶ lca-search:8080（local 192.168.8.8:30380）

[hiq-data-convertor]──gRPC──▶ search(192.168.8.9:31188) / calculate(192.168.8.8:31481) / message(hiq-message:8081)
                     ──HTTP──▶ SODA(soda.ecdigit.cn/resource) / GLaD(sandbox.globallcadataaccess.org)
```

---

## 7. 健康检查端点

| 服务 | 端点 | 说明 |
|---|---|---|
| dataset | `GET /health` | Liveness，恒 200（仅进程活跃） |
| dataset | `GET /ready` | Readiness，检查 4 个 PG 源 + Redis + gRPC，全 UP 才 200 |
| hiq-backend-admin | `GET /actuator/health` | ❓未在 yml 显式配置，按 Spring 默认尝试 |
| hiq-data-convertor | `GET /actuator/health` | 同上 |
| lca-convertor | `GET /actuator/health` | Spring Boot Actuator 默认 |

---

## 8. 日志（诊断最关键）✅ 已打通验证（2026-06-23）

**集群**：k3s，API Server `https://192.168.8.8:6443`（连接需 `--insecure-skip-tls-verify`）。
- kubeconfig：`hiq-backend-admin/.claude/kubeconfig-uat.yaml`（user `richardcao06`）。
  ⚠️ **这是凭证文件（内嵌 client cert+key），走本地/按需注入，本清单只记路径指针、不写其内容、不蒸馏入库。**
- RBAC 实测：可 `get ns` / `get pods -A` / `logs`（`CLAUDE.local.md` 里"cluster-scoped forbidden"已过时，以实测为准）。

**命名空间结构**（应用与 DB 混部在 `db-<env>`）：
- `db-uat1` / `db-uat2`：UAT 应用 + 中间件
- `db-dev` / `db-prod`：dev / 生产
- `hiqlcd-app-uat1`~`uat9`、`hiqlcd-app-prod`：另一套应用环境（按需排查）
- `db`：共享数据中间件（ClickHouse / ZooKeeper 等）；`mysql` / `flink` / `apisix`（网关）等独立 NS

**大后台日志（db-uat1 / deployment `backend`）**：
- 镜像 `harbor.ecdigit.cn/hiq-ai/hiq-backend-admin:uat1`；前端 = `backend-web`
- 标准命令：
  ```
  kubectl --kubeconfig <kubeconfig-uat.yaml> --server https://192.168.8.8:6443 \
    --insecure-skip-tls-verify -n db-uat1 logs deploy/backend --tail=200
  ```
- 查登录报错：`... logs deploy/backend --since=1h | grep -iE 'login|auth|exception|error'`

**同 NS 其它服务（db-uat1，按需 `logs deploy/<名>`）**：`backend-web`(大后台前端)、`square`/`square-backend`/`square-web`/`square-web-next`；db-uat2 另有 `data-convertor-web`。
**SSO 日志**：dataset-sso 的 pod 未在 db-uat1 直接见到，可能在 `hiqlcd-app-*` 或独立 NS —— ❓待定位（`kubectl get pods -A | grep -i sso`）。

---

## 9. 测试账号（用户名/角色=INTERNAL；密码=⛔走注入）

| 账号(用户名) | 角色 | 用途 | 密码 |
|---|---|---|---|
| `hiqadmin` | 管理员 | 大后台/SSO 登录（dataset CLAUDE.md 示例用此名） | ⛔走注入 / 找谁要：❓ |
| 李辰用的账号 | ? | 复现她"123 都不行" | ⛔走注入；账号名 ❓待问李辰 |

---

## 10. Playbook：大后台（hiq-backend-admin）登不上 / "123 都不行"

按 3a 链路逐段排查：
1. **服务在不在**：访问大后台 `GET /actuator/health`；不通 → 服务挂了/没部署（运维重启，非代码问题）。
2. **看日志**：拉 hiq-backend-admin 日志（第 8 节，位置待补），grep `/auth/login` 的报错堆栈。
3. **Redis 通不通**：会话存 Redis（第 5 节）；Redis 挂 → 登录态写不进 → 登不上。
4. **hiq_admin 库**：连 `hiq_admin`（第 4 节），查该账号是否存在/启用、密码哈希是否正常。
5. **网关路由**：确认 `/api/sso` → hiq-backend-admin 的 `/auth/login` 路由正常（网关配置）。
6. **下结论**：区分 **代码 bug**（建修复 task）/ **环境配置**（Redis/DB/网关）/ **账号问题**（重置）。

---

## 11. E2E 验收映射（`uat-verify` agent 用）

> PR 仓库 → 验收前端 → uat 入口 → 登录 → 典型验收页/关键 API。登录走 `scripts/uat/uat_login.py`
> （凭证注入、storageState 不落库）；工作流见 `.claude/agents/uat-verify.md`。

| PR 仓库 | 前端（俗称） | uat2 入口 | 登录接口 | 典型验收页 / 关键 API |
|---|---|---|---|---|
| **dataset-web** | 数据集编辑器 | `https://editor2.hiqdat.dev`（editor1 同构） | `POST /api/sso/auth/login` `{username,password,grantType:PASSWORD}` | 单元过程页 `/background-db/version/{id}/process` → `GET /backgroundDbBrowse/version/{id}/process`（被顶下线返 `code=4011/4012`） |
| square-web-next | 广场 | ❓待补 | `/api/sso`（同 dataset-sso） | ❓待补 |
| hiq-backend-admin / HiQ-backend-web | 大后台 | ❓待补（见第 2 节） | 自带 Sa-Token `POST /auth/login` | ❓待补 |
| dataset-sso | SSO（无独立前端） | 经各前端验证 | — | 顶下线码 4011/4012 的来源 |

**登录态机制**（dataset-web，源码实证 2026-06-24）：登录响应 data 整体存 cookie `user`（含 `accessToken`）；
业务请求拦截器从该 cookie 取 token 放 `Authorization` 头、`userId` 放 userId 头；权限初始化
`GET /api/sso/user/info/current?productCode=hiq_editor`；无 token → 跳 `/login?redirect=...`。

---

## 12. 部署映射 & 手动部署（`uat-deploy` agent 用；2026-06-24 实测）

> CI 自动部署当前不可用（见 12b）。本节记手动部署需要的映射与链路。

### 12a. dataset-web 的两套 uat2 后端（关键：registry 不一致）
| 对外入口 | k8s ns/deployment | registry | tag | 备注 |
|---|---|---|---|---|
| `editor2.hiqdat.dev`(APISIX 路由) | `hiqlcd-app-uat2`/`dataset-web`+`dataset` | **registry.cn-sh1.ctyun.cn**(天翼云) | `uat2` | **真实用户/李辰走这个**；CI reusable 也推天翼云 |
| `uat2.hiqdat.dev`(db-uat2 ingress) | `db-uat2`/`dataset-web`+`dataset` | **harbor.ecdigit.cn** | `uat2` | 另一套（别和 editor2 混）|

- ✅ **手动部署 editor2 = 推天翼云 `:uat2` + 重启 `hiqlcd-app-uat2/<svc>`**（2026-06-25 dataset 后端 4011 修复实测确认：推天翼云 + 重启 hiqlcd-app-uat2 → editor2 业务接口生效）。
- ⚠️ 早前误记 editor2 吃 db-uat2/harbor——"推天翼云没生效"其实是**没重启对的 deployment（hiqlcd-app-uat2）**，非 registry 错；harbor 那套（db-uat2）push 还会 `unauthorized`（无权限）。
- 两套 deployment 都 `imagePullPolicy: Always`、tag 固定 `uat2` 覆盖式 → 重启即拉新。
- 其它：`hiqlcd-app-uatN`(N=1..9) → 天翼云 `:uatN`、对外 `editorN.ecdigit.cn`；`db-dev`/`db-prod` 各一套（harbor）。

### 12b. CI 自动部署现状（为什么要手动）
- 各前端 `.github/workflows/main.yml` → 调 `HiQ-AI/workflow/.github/workflows/reusable-docker-build.yml@main`。
- 该 reusable **只 build+push 镜像（天翼云）+ 钉钉通知，没有 k8s deploy 步骤**；让 k8s 用新镜像靠外部（ArgoCD/手动 rollout）。
- 当前 build 因 `platforms: amd64,arm64` 多架构 QEMU **超时/失败**（修复 PR `HiQ-AI/workflow#2` 改单架构、**未合**）→ 镜像推不上 → uat 不更新。
- ⚠️ 该 reusable 内**硬编码明文凭证**（天翼云密码 / GitHub PAT / 钉钉 token）——**待轮换 + 移 GitHub Secrets**。

### 12c. 手动部署链路（绕过坏 CI；见 `.claude/agents/uat-deploy.md`）
1. `cd <项目>`、checkout 对的分支、`git pull`；
2. 本地 build（dataset-web：`npm run build:test` 出 dist）；
3. `docker buildx build -f DockerfileLocal --platform linux/amd64 --push --provenance=false -t <registry>/hiq-ai/<repo>:<tag> .`（`--platform amd64` 必须；用本地现有 registry 登录态、**不用 reusable 的明文**）；
4. 重启对应 deployment 拉新镜像（见 12d）。

### 12d. k8s 权限（rollout 谁做）
- kubeconfig：`hiq-backend-admin/.claude/kubeconfig-uat.yaml`。
- RBAC 实测（2026-06-24）：**只读**——`get/logs/auth can-i` 可，**`patch/update deployments`=no**（各 uat ns 均拒）。
- ⇒ **agent 不能 `rollout restart`**；重启由有权限的人 / ArgoCD 做。`uat-deploy` 只做到 push，给出重启命令交人。

---

## 附：凭证需求清单（**只列"要哪些 + 去哪取"，不写值；本节不入知识库**）

| 凭证 | 用途 | 取法（Keychain key / vault 路径 / 找谁要） |
|---|---|---|
| 大后台/SSO 测试账号密码 | 复现登录 | ❓（建议 Keychain key 名 / 找李辰或管理员） |
| UAT `hiq_admin` 库密码 | 查账号/权限表 | ❓（vault 路径 / 找 DBA） |
| UAT Redis 密码 | 查会话 | ❓ |
| UAT 集群访问（kubeconfig / SSH） | 拉日志 | ❓ |
