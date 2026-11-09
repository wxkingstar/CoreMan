# 飞书授权融入使用过程 + 群聊以提问人身份写入

## 背景

群聊目前只能读飞书文档（见 `2026-11-06-feishu-group-docs.md`）。业务方希望 AI 员工在群里也能写：云文档、知识库、表格、多维表、日程、任务。

另一个问题是授权入口。今天用户必须知道要在私聊发「连接飞书」，还要在卡片上选档位。

## 原则

1. **用户不需要学习权限申请。** 用到飞书能力时才触发授权，一键完成，完成后自动接着做原来的事。
2. **写入只用提问人身份。** 飞书修改记录显示本人，平台另有审计。不做应用身份写入。
3. **高风险操作由平台强制确认**，不靠模型自觉。

## 一、场景触发授权（私聊与群聊共用）

### 触发

| 场景 | 触发方式 |
|---|---|
| 私聊，未连接或档位不够 | 模型调用 `feishu_authorize(need)` |
| 群聊，工具调用时提问人没有可用授权 | 平台在工具内部直接触发，模型只收到结果 |
| 授权失效（撤销、刷新失败） | 同「未连接」 |
| 定时任务 | 不触发，行为不变（无人能点） |

`feishu_authorize` 的参数 `need` 取值与目标档位：

| need | 目标档位 |
|---|---|
| `general`（默认） | `all_except_send` |
| `read_messages` | `all_except_send` |
| `send_messages` | `all` |

当前授权已覆盖时返回 `already_connected`。私聊未连接时数据工具不在清单里，所以由这个工具承担入口，`feishu_guidance` 里「请引导用户发送连接飞书」的规则改为「需要时调用 feishu_authorize，发卡后简短说明并结束本轮」。

### 卡片

一律发到与本人的私聊，授权链接不进群。

| 现状 | 卡片 |
|---|---|
| 没有有效令牌 | 服务端立刻发起设备码流程，卡片按钮直接打开授权页，一次点击 |
| 已连接但档位不够 | 卡片按钮是回调「升级授权」，点击才发起流程，按钮随后换成授权链接 |
| 已有未过期的 pending | 不发新卡；复用同一授权，告知用户去私聊完成 |

档位不够时延后发起，是因为 `_start_authorization` 会清空现有令牌，用户不点就不应该让旧授权失效。

私信发送失败（如用户不在应用可用范围）时，在群里 @ 提问人说明原因。卡片上写明这次要做什么、授权范围、有效期和隐私说明（`RETENTION_NOTICE`）。

「连接飞书」和选档卡片保留，作为想选择更窄范围的入口，不再出现在引导文案里。`/my-feishu` 管理页不变。

### 续接

现有 `feishu_docs/resume.py` 只服务群文档，改造成通用模块：

- 新表 `feishu_auth_waits`：`bot_id`、`user_id`、`chat_id`、`origin_task_id`、`need`、`status`（offered：等用户点「升级授权」，此时不查不续接 / waiting / resumed / expired）、`expires_at`（30 分钟）。同一机器人、会话、用户只留一条有效等待，新的覆盖 `origin_task_id`。
- 看护进程按现有节奏替用户查授权结果，连接成功且档位满足 `need` 后，以同一入站事件续接（`serialize_session`）。
- `FeishuDocConsent` 的 `awaiting_auth` 状态不再写入，存量最多 30 分钟后过期，读取侧保留到下一版本。
- 卡片文案从「授权后回私聊回复已授权」改为「授权后会自动继续」。

身份校验沿用 `authorization_status`：授权人与发起人 `user_id`、`open_id`、`tenant_key` 不一致时拒绝，所以链接被转发也无法绑错人。

## 二、群聊工具

沿用 `coreman_feishu_docs` MCP 与 `DocsScope` 来源校验（只有真人发来的群消息或其按钮点击，排除旁听、调度、机器人互助）。

**工具清单对所有人相同，调用时才判提问人权限**：群会话是共用的，按人变化的清单会破坏提示词缓存。个人工具的处理函数直接复用，由一张白名单 `GROUP_TOOLS: {工具名: 类别}` 决定开放什么。不在表里的工具一律不可用。

| 类别 | 工具 | 处理 |
|---|---|---|
| `locate` 定位只读 | `feishu_search_docs` `feishu_drive_files` `feishu_file_info` `feishu_wiki_spaces` `feishu_wiki_nodes` `feishu_wiki_node` `feishu_calendar_list` `feishu_calendar_events` `feishu_calendar_search` `feishu_calendar_event` `feishu_calendar_freebusy` `feishu_meeting_rooms` `feishu_task_list` `feishu_task_search` `feishu_task_get` `feishu_tasklists` `feishu_tasklist_tasks` | 只返回定位字段（id、标题、时间、状态、链接），去掉描述、参会人、备注 |
| `create` 新建 | `feishu_create_document` `feishu_create_sheet` `feishu_create_folder` `feishu_wiki_create_node` `feishu_prepare_upload` `feishu_save_to_drive` `feishu_task_create` `feishu_task_add_subtask` `feishu_calendar_create_event` | 直接执行；带上他人（参会人、负责人）时转 `confirm` |
| `target` 写既有对象 | `feishu_append_document` `feishu_edit_document`（禁整篇覆盖） `feishu_add_document_comment` `feishu_sheet_write` `feishu_sheet_append` `feishu_sheet_manage` `feishu_base_create_table` `feishu_base_create_field` `feishu_base_create_record` `feishu_base_update_record` `feishu_base_batch_update_records` `feishu_rename_file` `feishu_copy_file` `feishu_task_comment` | 目标在「提问人本条消息（含引用、转发）的链接」或「本轮新建」里时直接执行，否则转 `confirm` |
| `confirm` | `feishu_calendar_update_event` `feishu_calendar_add_attendees` `feishu_calendar_delete_event` `feishu_calendar_reply` `feishu_task_update` `feishu_task_delete` `feishu_share_document` `feishu_move_file` `feishu_base_delete_record` | 一律确认 |

正文读取继续走按链接的 `feishu_doc_*`，不开放按 token 读任意文档。知识库链接的 token 与文档 token 不同，判断 `target` 时先解析成实际文档 token。

邮件、消息、审批、OKR、考勤不进群：结果会留在共用会话里，后面的发言人可能看到。

### 没有权限时

调用时 `Requester` 不存在或 `reaches` 不通过：写入等待记录，发授权卡，返回 `{status: "authorization_sent", hint}`，模型一句话说明后结束本轮。

应用本身未开通权限（`app_permission_missing`）用户无法自助，提示联系机器人管理员。

### 确认

复用定时任务确认卡的做法（`InteractionState` + `group_card_scope`）：

1. 工具把名称和参数暂存到 `InteractionState`（kind `feishu_group_write`，10 分钟过期），在群里发卡片，返回 `pending_confirmation`，模型结束本轮。
2. 卡片内容由平台按工具模板从实际参数生成，不用模型写的描述；长正文截断到 500 字。
3. 只有提问人点击有效。点确认时平台重新校验授权，按暂存参数原样执行，卡片换成结果。
4. 执行后在原会话续接，把结果告诉模型，多步任务能接着做。取消同样续接。

### 防护

- **目标来源**：文档正文里出现的链接不算提问人给的。
- **审计**：新表 `feishu_group_actions`：`bot_id`、`user_id`、`chat_id`、`task_id`、`tool`、`target`、`category`、`status`、`upstream_code`、`args_hash`、`created_at`，不存正文。
- **去重**：同一任务同工具同 `args_hash` 已成功的不再执行，返回 `already_done` 和当时的目标（审计表不存正文，所以不返回完整结果）。
- **上限**：单轮最多 30 次写入，超出返回 `write_limit_reached`。
- **告知**：提示词要求回复里说明「已以 @某人 的身份修改」并附链接。
- **内容不可信**：工具返回继续标 `external_untrusted_data`。

## 不做

- 应用身份写入、群里使用邮件/消息/审批/OKR/考勤。
- 企业微信。
- 跨机器人共享授权：授权按飞书应用颁发，每个机器人各授权一次。

## 测试

单元：`GROUP_TOOLS` 分类、`need` 与档位映射、`target` 判定（含知识库解析）、字段裁剪、整篇覆盖拒绝、去重与上限。

集成：

- 私聊未连接 → `feishu_authorize` → 卡片 → 授权 → 续接。
- 群聊首次写入完整链路；档位不够时不清旧令牌，点击后才发起。
- 确认卡：非提问人点击无效、过期、重复点击、点击时授权已失效。
- 来源排除：旁听、调度、机器人互助、定时任务不能写。

## 待真机验证

- 写接口的权限名与 `docs.update` 的 Markdown 模式。
- 未与机器人聊过的同事能否收到私信卡片并一键授权。
- 群内「仅特定人可见」卡片接口是否支持现有卡片格式；若支持，档位不够和未连接时可优先用它，私信作兜底。
