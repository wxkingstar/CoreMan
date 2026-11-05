# 通过 API 驱动 AI 员工

外部程序（自己的脚本、CI、工单系统）可以用成员的「API 令牌」给 AI 员工下任务，并拿回结果。任务以令牌所有人本人的身份运行，在飞书会话里承接：会话里能看到「通过 API 发起」和 AI 员工的回复，人可以接着在飞书里往下推进。

目前只支持飞书 AI 员工。

管理台「API 令牌」→「使用手册」是给人看的图解（全景图、飞书里的样子、时序、状态）。给 Agent 的版本随管理台公开发布在 `/integration/api-tasks-agent-guide.md`：手册页里「给 Agent 的版本」会把地址替换成当前 CoreMan 的地址，复制全文交给 Agent，或者把链接交给它自己读。设计见 [2026-11-06-api-driven-tasks.md](../superpowers/specs/2026-11-06-api-driven-tasks.md)。

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

返回的 `data`：`id`、`request_id`、`bot_key`、`status`、`conversation`、`reply`（结束后的最终回复，已脱敏）、`error_code`、`error_message`、`created_at`、`finished_at`。

| `status` | 含义 |
|---|---|
| `queued` | 排队中（含同一会话里前面还有人的轮次在跑） |
| `running` | 执行中 |
| `succeeded` | 完成 |
| `waiting_user` | AI 员工在飞书里向人提问；之后在飞书里继续，API 不再更新这一轮 |
| `failed` | 失败（`api_unavailable` 表示开轮前复核没通过，原因在 `error_message`） |
| `superseded` | 被本人在飞书里的新消息替代 |
| `cancelled` | 被取消 |

发起时的校验：令牌有效、AI 员工启用且开了「允许 API 驱动」、在令牌可驱动的列表里、本人在白名单里（白名单为空则所有人）、本人绑定了飞书身份；群别名要实时向飞书确认本人还在群里（AI 员工的应用需要 `im:chat.members:read`）。同一枚令牌最多 5 个任务未结束（429）。

## 执行

- 发起后，AI 员工先在承接会话里发一句「**某某** 通过 API 发起（令牌：名称）」加任务内容（超过 500 字截断），然后照常流式回复。
- API 这一轮和本人在飞书里发一条消息走同一条对话流水线：业务系统令牌、个人凭证、记忆、统计、审计都按本人算，对话记录里是本人的一轮。
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
