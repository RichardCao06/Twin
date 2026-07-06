# uat1 E2E 验收报告 —— accessScope 数组化「读存量数据崩溃」3 处 hotfix

> 环境：uat1（`hiqlcd-app-uat1` 命名空间，天翼云镜像）· 账号 hiqadmin（共用，本次任务已获授权）· 2026-07-02
> 验收方式：Playwright 真实浏览器操作 + 截图取证 + `kubectl logs` 交叉验证（非纯代码推断）

## 一、被测改动 & 版本核实（先于验收发现的重要偏差）

任务描述里认为 3 处 hotfix「已修复、已部署到 uat1」，但实测 `gh pr list` 发现**代码库状态比描述复杂**：

| 仓库 | PR | 状态 | 内容 |
|---|---|---|---|
| dataset-sso | [#46](https://github.com/HiQ-AI/dataset-sso/pull/46) | MERGED (main) | accessScope 数组化改造（生成端） |
| dataset-sso | [#48](https://github.com/HiQ-AI/dataset-sso/pull/48) | **OPEN，未合并** | `DataScope.java` 兼容新数组格式（hotfix，即 Case A） |
| square-web-next | [#25](https://github.com/HiQ-AI/square-web-next/pull/25) | MERGED → 后被 [#27](https://github.com/HiQ-AI/square-web-next/pull/27) revert | accessScope 消费端数组化 + checkout 支付扩展 |
| square-web-next | [#28](https://github.com/HiQ-AI/square-web-next/pull/28) | **OPEN，未合并** | `getUserDatabasePackage` 兼容新格式 scope（hotfix，即 Case B）|
| square-web-next | [#29](https://github.com/HiQ-AI/square-web-next/pull/29) | **OPEN，未合并** | ①②③ 重新打包重新合并（含 #28 hotfix）|

即：**main 分支目前并不包含这 3 个 hotfix**。但 `kubectl get pods -n hiqlcd-app-uat1` 显示 `sso`/`square-web-next` 两个 pod 在验收开始时仅 **8 分钟新**（backend/backend-web 仍是 5h56m 前的旧 pod，未变动），说明确实刚发生过一次**针对性手动部署**——推断是 `uat-deploy` 式手动流程直接从 hotfix 分支（`hotfix/data-scope-array-format-compat` / `feature/accessscope-payment-reland`）build+push+重启，绕开了「先合并 main」的步骤。

**结论：不能仅凭 GitHub PR 合并状态判断"未部署"，已按协议实测行为验证，见下文。** 这一版本状态差异建议同步给曹勇/李辰，确认后续要不要把这 3 个 hotfix 分支合回 main（当前 main 缺这些修复，若之后有人从 main 重新部署 uat1 会导致"回退"）。

补充发现：`gh pr view 48/28` 的 PR 描述显示，这 3 个问题其实**先在生产环境报错过一次并已回滚**（`hiqlcd-app-prod/sso v20260702-1`、`www1.hiqdat.dev` 广场页），本次 uat1 验证等同于「生产事故回归前的复测」，重要性高于普通功能验收。

## 二、登录与鉴权链路补充校准

`scripts/uat/uat_login.py` 默认的 `Authorization` 头对 uat1（`backend1.hiqdat.dev` / `www1.hiqdat.dev`）**不适用**——探测后确认 dataset-sso 的 Sa-Token `token-name` 配置为 `accessToken`（见 `application-uat1.yml:111`），必须用 **`accessToken` 请求头 + `userId` 请求头**（而不是 `Authorization: Bearer ...`），验证接口才会认得。已用 API 探测坐实（见下 Case A）。

广场（square-web-next）登录态走 **iron-session**，裸 cookie 注入无效；沿用 `square-auth-kick` 记忆里记录的方法：用 `square-web-next/node_modules/iron-session` 的 `sealData()` + `app/session/lib.ts` 硬编码密钥，手工封装 `app-session-cookie`，配合 `satoken`/`accessToken`/`userId` 三个明文 cookie 一起注入（对齐 `app/session/auth.ts:setLoginCookies` 的真实字段）。

工具限制：Playwright `browser_run_code_unsafe` 沙箱内无 Node `fs`/`require`（非完整 Node 进程），解决办法是先在 Bash 里用 `node -e` 算好 sealed cookie 字符串，再把字面量粘进浏览器代码里执行 `addCookies`。

另发现：`browser_take_screenshot` 工具在本环境固定卡在「waiting for fonts to load」超时（即便 `document.fonts.ready` 立即 resolve，工具内部逻辑仍会挂起），改用 `page.locator('body').screenshot({path:...})`（`browser_run_code_unsafe`）稳定可用，全程截图改走这条路径。

## 三、Case 分项结论

### Case A：dataset-sso 读取新格式 scope 不再报错 —— ✅ PASS

| # | 验收点 | 预期信号 | 实测 | 判定 |
|---|---|---|---|---|
| 1 | hiqadmin 走 SSO 登录 | `POST /api/sso/auth/login` 200，无 500 | `{"code":200,"data":{...accessToken...}}` | ✅ |
| 2 | 读取含新数组格式 scope 的存量数据不再抛 `UnrecognizedPropertyException` | `GET /api/sso/user/info/current?productCode=hiq_square` 200，`packageScopes` 正常返回 | 200，返回 7 条 `packageScopes`，其中 `Exiobase3`/`CarbonMinds`/`HiQLCD_BY_CUSTOM_39FF241B` 均为 `{"version":["ALL"],"model":["ALL"],"tag":["ALL"]}` 新数组格式，**解析无异常** | ✅ |
| 3 | sso 服务日志无 DataScope 相关异常 | `kubectl logs deploy/sso` 无 `UnrecognizedPropertyException`/`DataScope` | 覆盖 pod 全生命周期（24 分钟）日志，命中数 0；期间出现的少量 `NotLoginException`/Tomcat 堆栈系我方早期探测时头名写错（`Authorization` 而非 `accessToken`）导致，已核对上下文排除误判 | ✅ |

**细节**：hiqadmin 账号本身就持有多个"ALL 通配"套餐权限（Exiobase3、CarbonMinds、HiQLCD_BY_CUSTOM_39FF241B 等），命中题目要求的崩溃触发数据形状，**未额外找测试账号**。

证据：`docs/assets/uat1-e2e-2026-07-02/caseB_search_page_loggedin.png`（头像 `hiqadmin` 已登录，间接佐证登录+SSO鉴权链路整体打通）+ 上述 API 响应体（已记录，未落库明文 token）。

### Case B：广场数据集列表页不再因新格式 scope 崩溃 —— ✅ PASS

| # | 验收点 | 预期信号 | 实测 | 判定 |
|---|---|---|---|---|
| 1 | 登录广场并打开 `/lab/search` | console 无 `TypeError: t.toLowerCase is not a function` | 0 console errors | ✅ |
| 2 | 定位到"ALL 通配"套餐对应数据源（CarbonMinds，hiqadmin 有 `version/model/tag` 全 `ALL`） | 点进筛选后正常展示、无崩溃 | 点击 CarbonMinds 卡片 → 1844 条数据集正常列出，GWP100 数值正常显示（未被误拦截） | ✅ |
| 3 | 系统模型/版本筛选下拉可用 | 下拉正常展开、有选项、无报错 | 打开「系统模型」下拉，正常展示「分配，基于分类截断」选项，console 全程 0 errors | ✅ |

证据：
- `docs/assets/uat1-e2e-2026-07-02/caseB_search_page_loggedin.png`（搜索首页，已登录，0 报错）
- `docs/assets/uat1-e2e-2026-07-02/caseB_carbonminds_scope_all_filter.png`（CarbonMinds ALL 通配套餐列表 + 系统模型筛选下拉展开，1844 条数据集正常渲染）

### Case C：checkout 支付方式扩展回归 —— ✅ PASS（无新问题）

| # | 验收点 | 预期信号 | 实测 | 判定 |
|---|---|---|---|---|
| 1 | Stripe PaymentElement 渲染 3 个支付方式 tab | 微信支付/支付宝/银行卡三选项齐全 | 齐全，默认选中微信支付 | ✅ |
| 2 | 切换支付宝 tab | 文案变为"提交后将跳转"提示 | 命中，文案「提交后，您将被跳转，在新的页面安全地完成后续步骤。」 | ✅ |
| 3 | 切换银行卡 tab | 展示完整卡号/有效期/CVC/国家/邮编表单 | 命中，完整 Stripe 卡表单渲染正常 | ✅ |

证据：
- `docs/assets/uat1-e2e-2026-07-02/caseC_checkout_wechat_tab.png`
- `docs/assets/uat1-e2e-2026-07-02/caseC_checkout_alipay_tab.png`
- `docs/assets/uat1-e2e-2026-07-02/caseC_checkout_card_tab.png`

**踩坑记录（非产品问题，过程记录）**：第一轮测试时用了页面刷新前的旧 `aria-ref` 点击「银行卡」tab，导致 Stripe iframe 出现「无 tab 栏、只剩空白卡号输入框」的异常画面——一度怀疑是本批改动引入的新 bug。**重新导航、用刷新后的新 ref 复测一次即完全正常**（tab 栏 + 完整表单都在），确认是测试脚本自身的 stale-ref 问题，不是产品缺陷（已按协议「判 FAIL 前复现排除抖动」处理，未误报）。

Stripe 右下角"开发者工具"徽标常驻显示"1"（`查看错误`计数），点击未能在无头浏览器里展开明细；这是 Stripe Elements 测试模式的标准调试徽标（昨天 07-01 报告已记录过同类"非阻塞"现象），未观察到与其关联的 console error 或功能异常，判定为**已知非阻塞项**，不算新回归。

## 四、总体结论

✅ **3 个 Case 全部 PASS**，均有浏览器截图 + API/日志交叉证据，无回归（Case A 的 SSO 登录链路、Case B 的广场核心浏览路径、Case C 的支付方式 UI 均正常）。

## 五、遗留 action items（重要，非阻塞但需跟进）

1. **代码库状态与部署状态不一致**：`dataset-sso` #48、`square-web-next` #28/#29 三个 hotfix PR 目前都还是 **OPEN 未合并**，uat1 上跑的是从 hotfix 分支手动构建的镜像。建议尽快把这 3 个 PR 合并回 main，否则后续任何人从 main 重新构建/部署 uat1（或生产）都会**丢失这次修复、复现原始崩溃**。
2. 这 3 处 bug 本质是同一类模式（"读存量/新格式数据但代码没跟上"），且已经真实在生产捅过一次篓子（已回滚）。建议排查 dataset-sso / square-web-next 里是否还有其它地方读 `ts_data_package.scope` 却没做新旧格式兼容，提前扫一遍，别等第二次生产报错才补。
3. Stripe 开发者工具徽标"查看错误 1"长期存在，建议找个能展开明细的场景（非无头浏览器）确认具体是什么错误，排除是否为遗留配置问题（如 07-01 报告提到的 `SITE_X_API_KEY` 未配置 WARN）。
4. 沿用之前的 action：uat 专属测试账号（替换共用 hiqadmin，避免误踢真人）。
