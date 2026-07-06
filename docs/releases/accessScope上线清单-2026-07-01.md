# 本轮发布上线清单（2026-07-01 更新至 2026-07-02）

> 最初只有 accessScope 一条线，后续陆续把「支付方式扩展」「订单上链重试」也并进同一批 PR 一起发。本文档已更新为覆盖当前 4 个 PR 的**全部**内容，按功能线分节列检查项。来源：`dataset-sso` 仓库 `docs/accessScope设计方案.md/.html` 第 9、10 节（已随 [PR #46](https://github.com/HiQ-AI/dataset-sso/pull/46) 一起提到 main）+ 本次会话逐条核实的其余两条功能线。

## 关联 PR（4 仓库，一次发布，内容已不止 accessScope 一条线）

| 仓库 | PR | 包含的功能线 |
|---|---|---|
| hiq-backend-admin | [#56](https://github.com/HiQ-AI/hiq-backend-admin/pull/56) | ① accessScope 写入端强校验 + ② 支付扩展（Stripe Customer/createOrder/webhook）+ ③ 订单上链重试(后端) |
| HiQ-backend-web | [#15](https://github.com/HiQ-AI/HiQ-backend-web/pull/15) | ① accessScope 写入端前端表单 + ③ 订单上链重试(前端，上链状态列+重试按钮) |
| dataset-sso | [#46](https://github.com/HiQ-AI/dataset-sso/pull/46) | ① accessScope scope 驱动构建 + minimize + ④ SSO 读取端 |
| square-web-next | [#25](https://github.com/HiQ-AI/square-web-next/pull/25) | ① accessScope 消费端 matchDim 门控 + ② 支付扩展（checkout PaymentElement）|

**不在本次发布范围内**（单独处理，跟以上无关）：
- dataset-sso [#47](https://github.com/HiQ-AI/dataset-sso/pull/47)：revert「编辑器账号单设备登录·仅uat2」，target base 是 `feature/uat2-0430`（一个独立的 uat2 环境分支），不合入 main，跟这批发布无关联，别混进来一起看进度。

## 落地顺序（不可颠倒）

> 颠倒的后果：②漏做 → 把"通配实为漏配"的行固化成显式合法全权限；③未完成就切④ → 整库等合法授权会被判拒绝而消失；①不先堵写入端 → 边回填边产生新脏数据。

- [ ] **① 写入端数组形态 + 强校验**（hiq-backend-admin PR #56 + HiQ-backend-web PR #15 先合并上线）
  - 保存套餐时四维数组合法：每维非空、`["ALL"]` 不与具体值混存、db 维不得 `["ALL"]`
  - 任一维缺/空/非法 → 拒绝保存
  - `dataSaleType` 与 `scope.version` 不一致 → 拒绝保存，以 scope 为准
- [ ] **② 漏配甄别（回填前必做，人工逐行确认）**
  - 导出"通配实为可疑"的行：version/model/tag 为通配，或 `data_sale_type` 与 `scope.version` 不一致
  - 已知现网命中：`Ecoinvent_v 3.10.0`（应为 3.10.0 却通配）→ 需校正为 `version=["3.10.0"]`
  - 不一致/存疑行先挂起，不准进③
- [ ] **③ 一次性回填存量 `ts_data_package.scope`**（仅已过②甄别的行）
  - 2.1 整库行（`sale_method='ALL'` 且 scope 为空）→ 三维显式 `["ALL"]`，现网 6 行
  - 2.2 BY_SCOPE 行 flat → 数组 + 键改名（`systemModel`→`model`、`dataTag`→`tag`），现网约 24 行，建议脚本化执行
  - 2.4 BY_VERSION 迁移（`data_sale_type` 刷进 `version` 数组），现网 0 行，兜底未来新增
  - 脚本断言：任何 `"ALL"`/`["ALL"]` 必落到 `["ALL"]`，不得与具体值混存；数组不得含空串
  - 执行前先 SELECT 复核，执行后留存前后快照
  - 原文出处：`dataset-sso` repo `docs/accessScope设计方案.html` 第 9 节；完整 SQL 见下方「附：完整 SQL 脚本」

### 附：完整 SQL 脚本 —— ②甄别·校正 + ③一次性回填（accessScope，PostgreSQL，库 = hiq_admin / `ts_data_package` 所在库）

> **执行顺序固定，不可颠倒**：先跑「②甄别·校正」，人工确认候选行后再执行 UPDATE；确认无误后再依次跑 2.1 → 2.2 → 2.4。2.2 没有现成脚本（需按实际 flat 结构逐值映射，建议单独写一次性 Python/SQL 脚本，不要手写批量 UPDATE）。执行前后都要留 `SELECT` 快照存档。

```sql
-- ================================================================
-- ②甄别·校正（必须先于一切回填执行）
-- 影响：dataSaleType 是具体版本、但 scope.version 仍通配的行，现网命中 1 行
-- （Ecoinvent_v 3.10.0）。先 SELECT 出全部候选逐行人工确认，确认后再 UPDATE；
-- 不一致/存疑行先挂起，不允许进入下一步回填。
-- ================================================================

-- 第一步：先 SELECT 复核候选行，不要直接 UPDATE
SELECT id, code, name, sale_method, data_sale_type, scope
FROM ts_data_package
WHERE sale_method = 'BY_SCOPE'
  AND data_sale_type IS NOT NULL AND data_sale_type NOT IN ('', 'ALL')
  AND (scope->'version' = '["ALL"]'::jsonb OR scope->>'version' = 'ALL')
  AND COALESCE(is_deleted, false) = false;

-- 第二步：逐行人工确认无误后执行
UPDATE ts_data_package
SET scope = jsonb_set(scope, '{version}', to_jsonb(ARRAY[data_sale_type]))
WHERE sale_method = 'BY_SCOPE'
  AND data_sale_type IS NOT NULL AND data_sale_type NOT IN ('', 'ALL')
  AND (scope->'version' = '["ALL"]'::jsonb OR scope->>'version' = 'ALL')
  AND COALESCE(is_deleted, false) = false;
-- 现网命中：Ecoinvent_v 3.10.0（1 行）


-- ================================================================
-- 2.1 一次性回填：整库行（三维显式全通配）
-- 影响：sale_method='ALL' 且 scope 为空的行，现网 6 行
-- ================================================================
UPDATE ts_data_package
SET scope = '{"version":["ALL"],"model":["ALL"],"tag":["ALL"]}'::jsonb
WHERE sale_method = 'ALL' AND scope IS NULL
  AND COALESCE(is_deleted, false) = false;


-- ================================================================
-- 2.2 一次性回填：BY_SCOPE flat → 数组（逐值映射 + 键改名）
-- 影响：约 24 行（仅已过②甄别的行）。没有通用批量 UPDATE 语句——
-- 需要逐值判断（["ALL"] ↔ 具体值）+ 键改名（systemModel→model、dataTag→tag），
-- 建议写一次性脚本执行，按下面规则映射：
--   version : "ALL"/缺失 → ["ALL"]；具体值 → ["v"]
--   model   : ["ALL"]/缺失 → ["ALL"]；[...] → 原样数组
--   tag     : null/缺失 → ["ALL"]；"x" → ["x"]
-- ★断言（脚本执行后必须校验）：任何 "ALL"/["ALL"] 必落到 ["ALL"]，
--   不得与具体值混存；数组不得含空串。
-- ================================================================


-- ================================================================
-- 2.4 一次性回填：BY_VERSION 迁移
-- 影响：data_sale_type 刷进 version 数组，之后纯 scope 驱动，现网 0 行（兜底未来新增）
-- ================================================================
UPDATE ts_data_package
SET scope = jsonb_set(
        COALESCE(scope, '{"model":["ALL"],"tag":["ALL"]}'::jsonb),
        '{version}', to_jsonb(ARRAY[data_sale_type]))
WHERE sale_method = 'BY_VERSION'
  AND data_sale_type IS NOT NULL AND data_sale_type NOT IN ('', 'ALL')
  AND COALESCE(is_deleted, false) = false;
```
- [ ] **④ SSO 读取端切 fail-closed**（dataset-sso PR #46 上线，且必须在③之后）
  - 删除 `null→["ALL"]` 与 `db→["ALL"]` 兜底，缺失即不下发 + 报数据质量
  - 顺序：合法性校验 → 丢弃非法授权 → 数组 minimize
- [ ] **⑤ 收尾**
  - `sale_method` 仅留后台 UI/列表展示用
  - 废弃 `data_sale_type` 字段
  - 整库授权强制走 `WHOLE_DB`

## ② 支付方式扩展（Alipay + WeChat Pay）—— 检查项

涉及仓库：hiq-backend-admin PR #56、square-web-next PR #25。

- [ ] **数据库迁移**：`V1.2.16__create_ts_stripe_customer.sql` 新建 `ts_stripe_customer` 表（本人↔Stripe Customer 1:1 映射），非破坏性新建表，上线前确认迁移脚本已随 CI/手动执行。完整 SQL 见下方「附：完整 SQL 脚本」。
- [ ] **Stripe Dashboard 前置配置**：确认生产 Stripe 账号已开通 `alipay` / `wechat_pay` capability（代码假设已开通，未做运行时探测，没开通会在 `PaymentIntent.create` 时报错）。
- [x] **uat1 E2E 已验证**（[docs/uat1-E2E验收报告-2026-07-01.md](uat1-E2E验收报告-2026-07-01.md)）：PaymentElement 渲染 3 个支付方式 tab、支付宝跳转提示、微信二维码弹窗均 PASS（Stripe 测试模式）。
- [ ] `SITE_X_API_KEY 未配置` 的 WARN 对 checkout 的影响仅做了前端行为侧排除（接口全 200），**未做生产日志交叉确认**——上线后建议用 `scripts/ks_logs.py --service square-web-next --grep 'X-API-Key|SITE_X_API_KEY'` 补一次日志级验证。
- [ ] `handleAlipayWebhook` 目前是空实现（`log.warn` 占位，注释"本期不实现，所有支付走 Stripe"），确认支付宝确实全程走 Stripe 托管、不需要独立 webhook。

### 附：完整 SQL 脚本 —— V1.2.16 建表（hiq-backend-admin，PostgreSQL）

> 来源：`hiq-backend-admin` repo `src/main/resources/db/V1.2.16__create_ts_stripe_customer.sql`（Flyway 迁移文件，新建表，非破坏性，随应用启动自动执行，无需手动跑）。

```sql
-- ============================================================
-- V1.2.16 Stripe Customer 映射表
-- ts_stripe_customer：本平台用户 ↔ Stripe Customer 的 1:1 映射
--
-- 背景：
--   下单创建 PaymentIntent 时若不关联 Customer，Stripe Dashboard 的 Customer 栏为空，
--   无法在 Customers 列表看到付款人、也无法按客户聚合历史交易。
--   Stripe Customer ID（cus_xxx）由 Stripe 生成、不可指定，必须落库复用，
--   否则每次下单都会 Customer.create 产生大量重复客户。
--
-- 关键约束：
--   1. user_id 唯一键 = ts_user.id，天然 1:1，重复下单命中缓存直接复用
--   2. stripe_customer_id 唯一，防止同一 Stripe 客户被映射到多个用户
--   3. 不在 ts_user 加列，Stripe 相关数据独立成表（与 ts_payment_stripe_detail /
--      ts_stripe_event_log 风格一致），避免对共享核心表做侵入式 DDL
-- ============================================================

CREATE TABLE IF NOT EXISTS ts_stripe_customer (
    id                  VARCHAR(64)   PRIMARY KEY,                                -- UUID 代理主键
    user_id             VARCHAR(64)   NOT NULL,                                   -- ts_user.id（本平台用户ID），业务唯一键
    stripe_customer_id  VARCHAR(64)   NOT NULL,                                   -- Stripe Customer ID（cus_xxx），Stripe 生成不可指定
    email               VARCHAR(255)  NULL,                                       -- 建档时快照的邮箱，便于排障
    create_time         TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,         -- 首次建档时间
    update_time         TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,         -- 最后更新时间
    CONSTRAINT uk_stripe_customer_user UNIQUE (user_id),
    CONSTRAINT uk_stripe_customer_cus  UNIQUE (stripe_customer_id)
);

COMMENT ON TABLE  ts_stripe_customer                     IS '本平台用户 ↔ Stripe Customer 1:1 映射，下单时复用避免重复建客户';
COMMENT ON COLUMN ts_stripe_customer.id                  IS 'UUID 代理主键';
COMMENT ON COLUMN ts_stripe_customer.user_id             IS 'ts_user.id，业务唯一键（1:1 映射，下单按此查/幂等）';
COMMENT ON COLUMN ts_stripe_customer.stripe_customer_id  IS 'Stripe Customer ID（cus_xxx），由 Stripe 生成，不可指定，唯一';
COMMENT ON COLUMN ts_stripe_customer.email               IS '建档时快照的用户邮箱，仅用于排障，非实时同步';
COMMENT ON COLUMN ts_stripe_customer.create_time         IS '首次为该用户建立 Stripe Customer 的时间';
COMMENT ON COLUMN ts_stripe_customer.update_time         IS '记录最后更新时间';
```

> 注：表注释头的 PostgreSQL DDL 与建表语句一致，`id` 是 UUID 代理主键、`user_id` 是唯一键（这是后续 `4bd3180` commit 修正后的最终形态，非最初 PR #42 的版本——最初版本 `user_id` 直接做主键，因自定义 `@Insert` 不触发 MyBatis-Plus 的 `ASSIGN_UUID` 填充而改成了代理主键，见 PR #56 描述里的冲突合并说明）。

## ③ 订单上链重试 —— 检查项

涉及仓库：hiq-backend-admin PR #56（后端）、HiQ-backend-web PR #15（前端）。这条来自一直挂着没合并的旧 PR（后端 #39、前端 #3），跟 accessScope/支付扩展在代码上互不冲突，纯功能补充。

- [ ] `OrderLicenseServiceImpl.uploadLicenseToBlockchainWithRetry`：3 次内联重试逻辑 + 全部失败后 `ts_license.chain_status=failed` 并写订单 description——确认重试期间是否会阻塞主流程（同步重试还是异步）。
- [ ] 新接口 `POST /system/admin/order/{orderSn}/retry-blockchain`：确认权限控制（谁能调，是否需要额外的操作审计日志）。
- [ ] `INSERT blockchain_info` 前按 hash 和 license_sn 双重查重——确认这条查重逻辑不会跟 3 次重试的幂等性冲突（重试失败后再手动点重试，是否会查重出「已存在」而误判成功）。
- [ ] 前端"上链状态列"+"重试上链"按钮：确认按钮只在 `chain_status != chained` 时可点，避免已上链成功的订单被重复触发。
- [ ] 未做 E2E 验证（uat1 那轮验收只覆盖了 accessScope + 支付扩展，这条还没测）。

## 上线硬 Gate（G1–G6，任一项未闭环不得投产，仅覆盖 accessScope 那条线）

| # | Gate | 关注点 | 状态 |
|---|---|---|---|
| G1 | 下游 GWP matcher 通过 fail-closed 契约测试（缺维 / `[]` / 含 ALL 混存 / 旧位置码 / 大小写差异 → 全部拒绝） | 下游判权一致性 | ⚠️ **本轮 4 个 PR 未包含对应改动，需单独确认谁来改** |
| G2 | 生成端：dbCode 取不到 → skip + 报警，绝不 fallback `["ALL"]`；db 维禁止 `["ALL"]`（除 WHOLE_DB） | 生成端 db 维 | 待核实（dataset-sso PR #46 范围内） |
| G3 | 回填前完成"漏配甄别"，不一致/存疑行挂起；②甄别·校正 SQL 先于一切回填执行 | 回填数据正确性 | 待执行（见上面②③） |
| G4 | 数组 minimize 重写并过用例：先丢弃非法授权、再 minimize；非法码不参与覆盖去冗余 | 正确性 | 待核实（dataset-sso PR #46 范围内，`AccessScopeUtil`） |
| G5 | 写入端强校验：四维非空、`["ALL"]` 不与具体值混存、db 不得 `["ALL"]` | 写入端校验 | ✅ hiq-backend-admin PR #56 已实现 |
| G6 | scope POJO 各维为 `List<String>`（键 version/model/tag），无反序列化默认值；两端共用规范化字典 | 序列化与口径一致 | 待核实（两端字典是否已对齐） |

**G1 是总开关**：SSO 只发码，判权在下游，下游一旦放行即前功尽弃。

## 落地前必须回答的开放问题

- [ ] 下游 GWP matcher 由谁改、何时改、契约测试谁维护？
- [ ] 除 Ecoinvent 3.10.0 外，~24 行 BY_SCOPE 中还有几行"通配实为漏配"？回填前必须导出逐行确认。
- [ ] 过渡期双发的下游合并语义已定为"以新对象为准、认不出即丢弃"——下游是否已确认照此实现？
- [ ] `accessScopes` 是否拆出独立 `packageScopes` 字段（避免 String/Object 混装）？前端/网关/下游三方对齐。（dataset-sso 已在 PR #46 里做了 `packageScopes` 拆分，需确认前端/网关/下游三方是否都已对齐消费）
- [ ] 是否强制 `WHOLE_DB`？（设计文档已定为强制；若运营暂不具备，需给出过渡兜底）
- [ ] 规范化字典（大小写/全半角/枚举口径）的归属与版本管理谁负责？

## 备注

- **accessScope 那条线**本身无数据库 DDL 变更（`scope` 列已是既有 jsonb 列，只是存入形状变了），无新增环境变量。
- **支付扩展那条线新增了一个数据库迁移**：`V1.2.16__create_ts_stripe_customer.sql`（新建表，非破坏性）——上一版备注"无 DDL 变更"只针对 accessScope，本轮整体发布**不再是无 DDL**，见上面②的检查项。
- uat1 环境部署踩过的坑（已记 memory）：这几个服务的 uat1 实际命名空间是 `hiqlcd-app-uat1`（走天翼云 ctyun 镜像），不是 `db-uat1`（那是走 harbor 的另一套环境，本次一度推错方向），入口域名是 `backend1.hiqdat.dev` / `www1.hiqdat.dev`，不是 `uat1.hiqdat.dev`（那个是假入口，指向未更新的旧镜像）。
- uat1 E2E 验收报告：[docs/uat1-E2E验收报告-2026-07-01.md](uat1-E2E验收报告-2026-07-01.md)（覆盖 accessScope + 支付扩展两条线，订单上链重试未测）。
