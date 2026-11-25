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
| `POST /api/infra/tasks/run` | 发起。可带 `images`：上传得到的 `upload_id` 列表，按顺序，张数不限；只能引用本调用方为这位员工传的、没过期的图片（否则 404）。`user` 是员工邮箱前缀，按业务令牌 sub 的同一口径认人：只认唯一一位在职员工，认不出就 403，不退回登录名。`request_id` 同一调用方内幂等，内容不同返回 409。新建返回 202。 |
| `POST /api/infra/tasks/uploads` | 上传一张图片，multipart：`user`、`sha256`（文件内容的 SHA-256，十六进制）、文件 `file`。签名只覆盖 `user` 和 `sha256`，文件靠 `sha256` 绑定，对不上按签名无效（401）。JPEG / PNG / WebP（按文件头认），单张不超过 10MB。同一调用方同一员工传同样的内容返回原来的 `upload_id`。返回 `upload_id`、`sha256`、`bytes`、`content_type`、`expires_at`。 |
| `GET /api/infra/tasks/run/{id}?user=alice` | 查询。只能查本调用方为这位员工发起的任务。 |
| `POST /api/infra/tasks/run/{id}/cancel` | 取消，正文 `{"user": "alice"}`。 |

返回的 `data` 同上表，`conversation` 换成 `user`、`resume_at` 和 `writes`；不会出现 `superseded`，多一个 `waiting_quota`（运行时额度用完，排队等恢复，`resume_at` 是预计重跑时间）。

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
- **图片**：前 8 张直接放进这一轮给模型看；更多的换成下载链接：配了文件中转站的临时桶（`ASSISTANT_TMP_S3_*`）时，开轮时把图片复制进桶里，给 7 天有效的预签名链接，节点经对象存储的公网地址下载，不用能访问管理台；没配或复制失败的那张，用平台自己的 24 小时链接（`/api/infra/tasks/uploads/{id}/raw?token=…`，节点要能访问管理台）。让 AI 员工下载到工作目录 `api-inputs/` 下分批打开。私聊里的第一句注明附了几张图。没被任务用上的图片 24 小时后删掉，被用上的保留 30 天。
- **写操作要员工确认**：AI 员工经平台代理调用业务系统写接口时照常暂存，本轮结束后确认卡发到员工和 AI 员工的私聊，只有员工本人在这个私聊里点才算；确认后平台以员工身份执行，结果接着在这个任务的独立会话里续接一轮，回复同样发在私聊里。个人令牌发起的任务仍然不能写。
- **额度用完不换运行时，排队重跑**：开轮前已知 AI 员工的运行时额度用完（5 小时或 7 天窗口用量到 100%），这一轮放回队列，到重置时间再跑，员工那边不发任何消息；跑到一半才撞上额度，这一轮照常结束，回复末尾说明已排队和预计时间，同一个独立会话里另排一轮，到点用原来的内容和图片重跑（拿不到重置时间就 30 分钟后再试）。不推「切换运行时」的卡片：换运行时是整个 AI 员工换机，会影响别人。从第一次等额度起超过 7 天还没跑成，任务失败，`error_code` 为 `quota_wait_expired`，私聊里说明原因。
- **去重**：同一调用方、同一员工、同一 AI 员工，prompt 和图片（`upload_id` 列表，同内容的图片就是同一个 `upload_id`）都一样，并且前一个任务还没结束（排队、等额度、执行中），直接返回那个任务（200），不新建。前一个已经结束的会照常新建。
- **引用回复载入原任务**：员工在私聊里引用这类任务推来的消息（结果卡片，或「在某系统里发起」那句）回复时，这一轮带上原任务的内容：发起的系统、任务原文、图片（8 张以内直接给模型看，更多的给下载链接，过了保留期的注明看不到了）、第一轮和最后一轮的回复、写操作及状态。只认发起人本人在他自己的私聊里引用；同一个运行时会话里已经带过的不再重复，会话换新（过期、重置、换运行时）后再引用会重新带。之后就在员工的普通私聊会话里接着聊，写操作照常走确认卡。
- 同一调用方同一员工最多 20 个任务未结束（429）。

管理命令：

```bash
coreman clients create intake --name 资料录入 --scope tasks --bot demo-bot
coreman clients set intake --bot demo-bot --bot other-bot   # 替换可驱动的 AI 员工
coreman clients set intake --no-bot
```

需要数据库迁移 `0125`–`0128`。运行时节点不用升级。设计见 [2026-11-24-api-tasks-system-clients.md](../superpowers/specs/2026-11-24-api-tasks-system-clients.md)。

## 群承接模式

监控系统这类调用方没有「哪位员工」，结果应该进群里接着讨论。平台管理员可以给调用方的某个 AI 员工配「群承接」：每条配置是（AI 员工, 群 chat_id, 服务账号）。服务账号必须是在用的 service 账号；建配置时可以从该 AI 员工告警来源里已有的群和服务账号里选。配了之后，这个调用方驱动这个 AI 员工就只走群模式，没配的调用方和 AI 员工行为不变。

```bash
POST /api/infra/tasks/run
{"bot_key": "ops-bot", "prompt": "订单服务 5xx 升高，排查原因", "chat_id": "oc_xxx", "request_id": "alert-123"}
```

- **身份锁死**：请求里带 `user` 返回 422；执行身份固定是配置里的服务账号，AI 员工只有它的权限。不要求飞书身份、私聊过和使用白名单；仍要求调用方有 `tasks`、AI 员工在可驱动列表里、已启用、开着「允许 API 驱动」。
- `chat_id`：只配了一个群时可省略；配了多个必须指定（422）；不在配置里 404。
- **图片**：和私聊模式一样可以带 `images`，按顺序进这一轮，群里第一句注明附了几张图。上传时不带 `user`，改带 `bot_key`（多个承接群时再带 `chat_id`，都在签名里），图片归配置的服务账号，发起时按同一个身份认领；别的员工名下的上传认领不了（404）。幂等和同内容去重都算上图片。
- **群主会话**：在群里先发「在「系统名」里发起」加任务内容，这一轮跑在群的主会话里（和群里 @ 它同一个会话），排在群里正在跑的轮次后面；回复直接发在群里、留在上下文中，不加推送尾注；群里 `/new` 照常重置。本轮提示写明调用方传入的内容是外部数据不是指令。不向服务账号索要个人凭证，不挂个人工具和个人定时任务（同告警回合）。
- **写操作**：照常暂存，确认卡发到这个群；群成员中在 AI 员工使用白名单里的人（白名单为空则所有群成员）都能点，点了按**点击人**的身份和权限执行，业务系统调用记录记点击人。两人同时点先到先得，后到的人在群里收到一句「已经有人处理过」。批量写确认后由确认人负责停止、继续。
- **排队上限**：每个（AI 员工, 群）里群模式发起、还在排队（不含执行中）的任务最多 5 条，超出返回 429，`errors` 为 `[{"type": "queue_full"}]`，不建任务。群里真人提问不计入。
- 查询、取消不带 `user`（带了 422），返回的 `data` 用 `conversation: {"target": "chat", "chat_id": …}` 代替 `user`。`request_id` 幂等、同内容未结束去重照旧。

管理命令：

```bash
coreman clients group-options --bot ops-bot                  # 告警来源里的群与服务账号、所有在用服务账号
coreman clients group-add monitor --bot ops-bot --chat oc_xxx --service-user svc-ops
coreman clients groups monitor
coreman clients group-remove monitor <配置 id> --yes
```

需要数据库迁移 `0135`，节点不用升级。设计见 [2026-11-25-api-client-group-delivery.md](../superpowers/specs/2026-11-25-api-client-group-delivery.md)。
