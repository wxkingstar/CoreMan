# 通过 API 驱动 AI 员工

外部程序（自己的脚本、CI、工单系统）可以用成员的「API 令牌」给 AI 员工下任务，并拿回结果。任务以令牌所有人本人的身份运行，在飞书会话里承接：会话里能看到「通过 API 发起」和 AI 员工的回复，人可以接着在飞书里往下推进。

目前只支持飞书 AI 员工。

管理台「API 令牌」→「使用手册」是给人看的图解（全景图、飞书里的样子、时序、状态）。给 Agent 的版本是文档站的一页（源文件 `coreman/docs_site/pages/api-tasks.md`，网页 `/docs/api-tasks`，Markdown `/docs/api-tasks.md`），原地址 `/integration/api-tasks-agent-guide.md` 继续可用：手册页里「给 Agent 的版本」会把地址替换成当前 CoreMan 的地址，复制全文交给 Agent，或者把链接交给它自己读。设计见 [2026-11-06-api-driven-tasks.md](../superpowers/specs/2026-11-06-api-driven-tasks.md)。

## 准备

1. **AI 员工打开开关。** AI 员工详情 →「API 驱动」→「允许 API 驱动」，默认关闭，创建者和协作者可改。
2. **成员建令牌。** 管理台「API 令牌」→「新建令牌」：选可驱动的 AI 员工（只能选本人能用的飞书 AI 员工），有效期 1–365 天。明文只显示一次，库里只存哈希；吊销后立即失效。
3. **选承接会话。**
   - 私聊（默认）：本人要先在飞书里私聊过这个 AI 员工一次。
   - 群：群成员在群里 @ AI 员工发 `/api 绑定 <别名>`（`/api 解绑` 解除，`/api` 查看）。别名 1–32 个字符，可用中文、字母、数字、`-`、`_`；一个群一个别名，同一个 AI 员工里别名不重复。白名单非空时只有名单内的成员能绑定。

需要数据库迁移 `0077`。运行时节点不用升级。

## 接口

认证：`Authorization: Bearer <令牌>`。带 `Origin` 头（浏览器页面发起）的请求一律拒绝。

```bash
curl -X POST https://coreman.example.com/api/infra/tasks \
  -H "Authorization: Bearer $COREMAN_TOKEN" -H "Content-Type: application/json" \
  -d '{"bot_key": "demo-bot", "prompt": "整理今天的告警", "request_id": "ticket-123",
       "conversation": {"target": "chat", "alias": "值班群"}}'
```

| 接口 | 说明 |
|---|---|
| `POST /api/infra/tasks` | 发起。`prompt` 1–20000 字符；`request_id` 可选，同一令牌同一编号同样内容返回原任务（200），内容不同返回 409；`conversation` 省略时为私聊。新建返回 202。 |
| `GET /api/infra/tasks/{id}` | 查询。只能查这枚令牌自己发起的任务。 |
| `POST /api/infra/tasks/{id}/cancel` | 取消未结束的任务。 |

返回的 `data`：`id`、`request_id`、`bot_key`、`status`、`conversation`、`reply`（结束后的最终回复，已脱敏；超过 50000 字截断，`reply_truncated` 为 true）、`error_code`、`error_message`、`created_at`、`finished_at`。

| `status` | 含义 |
|---|---|
| `queued` | 排队中（含同一会话里前面还有人的轮次在跑） |
| `running` | 执行中 |
| `succeeded` | 完成 |
| `waiting_user` | AI 员工在飞书里向人提问，或发了凭证表单等人填写；之后在飞书里继续，API 不再更新这一轮 |
| `failed` | 失败（`api_unavailable` 表示开轮前复核没通过，原因在 `error_message`） |
| `superseded` | 被本人在飞书里的新消息替代 |
| `cancelled` | 被取消 |

发起时的校验：令牌有效、AI 员工启用且开了「允许 API 驱动」、在令牌可驱动的列表里、本人在白名单里（白名单为空则所有人）、本人绑定了飞书身份；群别名要实时向飞书确认本人还在群里（AI 员工的应用需要 `im:chat.members:read`）。同一枚令牌最多 5 个任务未结束（429）。

## 执行

- 发起后，AI 员工先在承接会话里发一句「**某某** 通过 API 发起（令牌：名称）」加任务内容（超过 500 字截断），然后照常流式回复。
- API 这一轮和本人在飞书里发一条消息走同一条对话流水线：业务系统令牌、个人凭证、记忆、统计、审计都按本人算，对话记录里是本人的一轮。飞书个人授权的工具和个人定时任务例外：它们要求飞书回调验证过的本人消息，API 发起的轮次里不提供。
- 承接会话里的「通过 API 发起」先发出，回复才开始：触发消息还在发送队列里时，这一轮每 2 秒重排一次，最多等约 20 秒，发送失败就不再等。
- 同一会话里前面有人的轮次在跑时，API 这一轮排在后面，不打断人；API 这一轮在跑时本人在飞书里发新消息会替代它（`superseded`）；群里其他人 @ AI 员工按群聊规则排在后面。
- 开轮前再查一遍：令牌没吊销、AI 员工还开着开关、本人还在白名单、群别名还在。任何一项不过就不执行，在会话里说明原因。

## 管理命令

```bash
coreman me api-tokens --as alice
coreman me api-token-create 工单系统 --bot demo-bot --days 90 --as alice   # 明文只打印这一次
coreman me api-token-revoke 工单系统 --yes --as alice
coreman bots api-access demo-bot
coreman bots api-access-set demo-bot on
coreman bots api-alias-remove demo-bot 值班群 --yes
```

## 业务系统代员工发起

业务系统（比如内部的资料录入系统）服务很多员工，不用每个人去建个人令牌：平台管理员在「凭证」→「API 调用方」里给它建一个调用方，勾选接口组「代员工下任务」（`tasks`），再选「可驱动的 AI 员工」。调用方密钥不过期，停用或轮换后立即失效。

业务系统用调用方签名（同 [基础设施接口](../infrastructure-api.md)）调用，请求里写明是哪位员工：

```bash
POST /api/infra/tasks/run
{"user": "alice", "bot_key": "demo-bot", "prompt": "识别截图里的账号信息，写回录入系统", "request_id": "form-8812"}
```

| 接口 | 说明 |
|---|---|
| `POST /api/infra/tasks/run` | 发起。`user` 是员工邮箱前缀，按业务令牌 sub 的同一口径认人：只认唯一一位在职员工，认不出就 403，不退回登录名。`request_id` 同一调用方内幂等，内容不同返回 409。新建返回 202。 |
| `GET /api/infra/tasks/run/{id}?user=alice` | 查询。只能查本调用方为这位员工发起的任务。 |
| `POST /api/infra/tasks/run/{id}/cancel` | 取消，正文 `{"user": "alice"}`。 |

返回的 `data` 同上表，`conversation` 换成 `user` 和 `writes`；不会出现 `superseded`。

`writes` 是这次任务（含确认后的续接轮）提出的业务系统写操作，只有业务系统、操作和状态，不含请求内容：

| `status` | 含义 |
|---|---|
| `pending_confirm` | 等员工在私聊里点确认卡 |
| `rejected` | 员工点了取消，或这一轮没正常结束、没发卡 |
| `expired` | 确认卡过期没点 |
| `succeeded` / `failed` / `unknown` | 确认后平台执行的结果；`unknown` 表示可能已经生效，要到业务系统核对 |
| `running` / `not_run` | 执行中 / 前面一项失败后没有执行 |

批量写操作是一条，带 `"batch": true`，确认后带 `items`（各状态的项数）。任务 `succeeded` 只表示 AI 员工这一轮做完了，数据有没有写进业务系统看 `writes`，或者直接看业务系统里的数据。

和个人令牌不同的地方：

- **独立会话**：每个任务一个会话（`api:<任务 id>`），不带员工和 AI 员工私聊里的上下文，私聊里发消息也不会替代它。
- **结果发在员工私聊里**：先发一句「**某某** 在「系统名」里发起」加任务内容，回复末尾标注「此消息由 API 任务推送，不在当前对话上下文中」（返回给业务系统的 `reply` 不带这句）。
- 校验：调用方启用且有 `tasks`、AI 员工在调用方的可驱动列表里、AI 员工开着「允许 API 驱动」、员工在白名单里、绑定了飞书并私聊过这个 AI 员工。开轮前再查一遍，不过就在私聊里说明原因。
- **写操作要员工确认**：AI 员工经平台代理调用业务系统写接口时照常暂存，本轮结束后确认卡发到员工和 AI 员工的私聊，只有员工本人在这个私聊里点才算；确认后平台以员工身份执行，结果接着在这个任务的独立会话里续接一轮，回复同样发在私聊里。个人令牌发起的任务仍然不能写。
- 同一调用方同一员工最多 20 个任务未结束（429）。

管理命令：

```bash
coreman clients create intake --name 资料录入 --scope tasks --bot demo-bot
coreman clients set intake --bot demo-bot --bot other-bot   # 替换可驱动的 AI 员工
coreman clients set intake --no-bot
```

需要数据库迁移 `0125`。运行时节点不用升级。设计见 [2026-11-24-api-tasks-system-clients.md](../superpowers/specs/2026-11-24-api-tasks-system-clients.md)，图片、额度排队、引用回复载入原任务在后续步骤里做。
