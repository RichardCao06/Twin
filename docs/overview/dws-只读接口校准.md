## dws 只读接口校准（KDL 数据源摄取用）

> 2026-06-22 对本机 `dws v1.0.39`（已登录，corp `dingb1273...`）**实测**校准。KDL 只用其中的
> **只读 R0** 子命令；封装见 `src/dws_agent/kdl/dws_read.py` 的 `DwsReader`。
> 硬约束：只读、绝不发送、绝不调 `auth` 写命令。

### 0. 登录态 / 本人身份
| 用途 | 命令 | 取值 |
|---|---|---|
| 确认登录 | `dws auth status --format json` | `.authenticated == true && .token_valid == true` |
| **本人 uid**（D4 命门） | `dws contact user get-self --format json` | `.result[0].orgEmployeeModel.userId`（实测 `0113226846838382`；另有 `orgUserName`/`orgUserMobile`） |

> ⚠️ **身份双体系**：`get-self` 给的是 **userId**；而消息里的发送者是 **openDingTalkId**（`senderOpenDingTalkId`）。二者不同名空间。QA 配对判断"回复是否出自本人"时，用 `search-advanced --user <本人userId>` 按 userId 过滤发送者（dws 内部解析），而**不要**直接拿本人 userId 去比对 `senderOpenDingTalkId`。

### 1. 文档读取链（钉钉文档源）
| 步骤 | 命令 | 关键返回字段 |
|---|---|---|
| 搜文档（本人创建） | `dws doc search --creator-uids <本人uid> --extensions adoc --limit 30 [--cursor X] --format json` | `documents[].{nodeId, name, docUrl, contentType, extension, createTime, creatorUid, nodeType}` + `hasMore` + `nextPageToken` |
| 遍历目录 | `dws doc list [--folder X] [--workspace X] --format json` | `nodes[].nodeId`（list 用 `nodes`，search 用 `documents`，注意区别） |
| 元信息/路由 | `dws doc info --node <id> --format json` | `contentType` + `extension`（**仅 `ALIDOC`+`adoc` 能在线读 Markdown**；PDF/Word 等不支持） |
| 读正文 | `dws doc read --node <id> --format json` | 默认 **Markdown 文本**（非 JSON）；仅有下载权限的文档 |

- 摄取 KDL：`doc search --creator-uids <self> --extensions adoc` → 逐个 `doc info` 确认 `ALIDOC/adoc` → `doc read` 取 Markdown → 切块 → 蒸馏。
- provenance：`kind=DOC_ID`，`ref = nodeId`（+ 块内 `#section` 锚点防 ku_id 撞键）。

### 2. 消息读取（对话 / QA 源）
| 命令 | 用途 | 备注 |
|---|---|---|
| `dws chat message search-advanced [--query] [--user <uid>] [--at-me] [--conversation-ids ids] --start <ISO> --end <ISO> --limit N --cursor 0 --format json` | **首选**多维搜索 | 推荐入口 |
| `dws chat message list --group <openConversationId> --time "yyyy-MM-dd HH:mm:ss" [--forward] [--limit N]` | 拉**群聊**消息 | 仅群聊 |
| `dws chat message list-all --start --end --limit --cursor` | 当前用户所有会话 | 时间窗 + 游标 |
| `dws chat search --query "群名"` | 找群 → `openConversationId` | |

**返回结构**（实测 search-advanced / list-all 同构）：
```
result.conversationMessagesList[] = {
  openConversationId, singleChat(bool), title,
  messages[] = {
    content,                 // 正文
    createTime,              // "yyyy-MM-dd HH:mm:ss"
    openMessageId,           // 消息 id
    senderOpenDingTalkId,    // ★ 发送者稳定 account（反投毒强校验用这个）
    sender                   // 显示名（可改，不可信）
  }
}
result.hasMore / result.nextCursor
```

**两个命门**：
- **反投毒**：判断"回复出自本人 / 谁发的"，**只认 `senderOpenDingTalkId`**，绝不用 `sender`（显示名可伪造）。
- **单聊硬过滤**：会话级 `singleChat==true` 即单聊，KDL 只摄取 `singleChat==false` 的群聊（且在 `allowed_groups` 白名单内）。

### 3. KDL 只读白名单（`DwsReader.ALLOWED`）
`doc search/list/info/read`、`chat search`、`chat message list/list-all/search-advanced/list-mentions`、`contact user get-self`。任何 `send/create/update/delete/recall/auth *` 等写命令在调 dws 前 `raise PermissionError`。

### 4. never（永不调用，印证项目 never 名单）
实测 `dws auth` 子命令含 `export/import/logout/reset`——与项目 `policy.never` 一致，KDL/Executor 均不得调用。

### 5. 仍待实测细化（不阻塞接入，使用时补）
- `doc list` 的 `nodes[]` 完整字段（本次未取样，search 的 `documents[]` 已确认）。
- 群消息 `chat message list --group` 的逐条字段是否与 search-advanced 完全一致（结构应一致，字段名以本文件为准）。
- 本人 `openDingTalkId`（若 QA 配对改用 openDingTalkId 比对时再取；当前策略用 `--user <userId>` 规避）。
