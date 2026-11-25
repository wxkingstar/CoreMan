# API 调用方的群承接模式

状态：已确认（2026-11-25）。基础见 [2026-11-24-api-tasks-system-clients.md](2026-11-24-api-tasks-system-clients.md)（下称「私聊模式」）。

## 1. 背景

私聊模式下，业务系统用 `user`（员工邮箱前缀）指定员工身份，任务在独立会话里跑，结果推到员工和 AI 员工的私聊。监控系统驱动运维 AI 员工排查问题时不适用：没有「哪位员工」，结果应该进运维群，接着在群里讨论；排查要能写（重启、改配置等），但写之前要有群里有权限的人确认。

群承接模式：调用方为某个 AI 员工登记若干个群，每个群绑定一个服务账号。发起时不传员工，以服务账号身份在群的主会话里跑一轮，结果直接发在群里。

## 2. 用户决策

1. **独立配置**：调用方上新增「群承接配置」，每行 = (调用方, AI 员工, 群 chat_id, 服务账号)。不复用告警来源（`bot_alert_sources` 按告警机器人发送者登记，API 没有发送者），但管理台和管理命令建配置时把该 AI 员工告警来源里已有的群和服务账号列出来供选。服务账号必须是 `source='service'` 且在用。没配群承接的调用方行为完全不变。
2. **身份锁死**：群模式下执行身份只能是配置里的服务账号；请求里带 `user` 直接 422。Agent 只有服务账号的权限。
3. **校验**：群模式走独立分支，不要求飞书身份、不要求私聊过、不看使用白名单（身份由平台管理员配死）；仍要求 AI 员工启用、是飞书 AI 员工、打开「允许 API 驱动」、在调用方 `bot_ids` 里。发言者参照告警回合：以服务账号为发言者，不伪造飞书身份。私聊模式和告警来源的代码路径不受影响。
4. **群主会话**：`session_key` 用群 chat_id（和群里 @ 它是同一个主会话），排在群里正在跑的轮次后面（`serialize_session`）；结果直接进上下文，不加「不在当前对话上下文中」尾注；群里 `/new` 照常重置。触发消息写明「在『调用方名』里发起」。本轮块写明调用方传入的内容是外部数据不是指令。不向服务账号索要个人凭证（同告警回合）。
5. **写操作**：允许写。暂存照旧（看 AI 员工的写授权）。确认卡发到这个群；能点的人 = 群成员 ∩ 该 AI 员工使用白名单（白名单为空 = 所有群成员）；点击后按**点击人**的身份和权限执行；审计（业务系统调用记录）记真实点卡人；两人同时点先到先得，后到的在群里收到「已由别人处理」。
6. **排队上限**：每个（AI 员工, 群）里群模式发起、仍在排队（不含正在执行）的任务最多 5 条，超出直接 `429 queue_full`，不建任务。真人提问不计入、不受限。不做每分钟频率限制。
7. **查询**：`view` 对群模式返回 `conversation: {target: "chat", chat_id}`，不返回 `user`；`request_id` 幂等照旧。

## 3. 数据

迁移 0135（只新增）：

```
api_client_group_routes
  id               uuid PK
  client_key       text  FK api_clients.app_key ON DELETE CASCADE
  bot_id           uuid  FK bots.id ON DELETE CASCADE
  chat_id          text
  service_user_id  uuid  FK users.id ON DELETE CASCADE
  created_by       uuid  FK users.id ON DELETE SET NULL
  created_at       timestamptz
  UNIQUE (client_key, bot_id, chat_id)
```

`api_tasks` 不加列：群模式的任务是 `client_key` 非空且 `target='chat'` 的行，`user_id` 是服务账号，`chat_id` 是群。

## 4. 发起

`POST /api/infra/tasks/run`：

```json
{"bot_key": "ops-bot", "prompt": "…", "chat_id": "oc_xxx", "request_id": "alert-123"}
```

- 调用方为这个 AI 员工配了群承接，就是群模式：
  - 带 `user` → `422`。
  - `chat_id` 指定群；只配了一个群时可省略，配了多个时必填（`422`）；不在配置里 → `404`。
  - 不支持 `images`（`422`）：上传按员工归属，群模式没有员工。
- 没配群承接：照旧要求 `user`（缺了 `422`），带 `chat_id` → `422`。
- 校验：服务账号仍是在用的 service 账号（否则 `409`）、AI 员工启用 / 飞书 / 允许 API 驱动 / 在 `bot_ids` 里。
- 幂等照旧（同一调用方同一 `request_id`）；同内容未结束的任务照旧直接返回那一条。
- 排队上限：按（AI 员工, 群）加事务级咨询锁后数「群模式、总线状态 queued、未请求取消」的任务，满 5 条 `429`，错误码 `queue_full`。
- 写合成入站事件（发送者为空，不伪造飞书身份），在群里发触发消息「在「{调用方名}」里发起」加任务内容引用，入队 chat 任务：`session_key` = 群 chat_id，载荷 `api_task_id`、`api_client`、`api_group: true`、`serialize_session: true`。

`GET /api/infra/tasks/run/{id}`、`POST …/cancel`：群模式的任务不带 `user`（带了 `422`）；私聊模式照旧要 `user`。

## 5. 执行（worker）

- 入口（`worker/chat/api.py`）：开轮前复核调用方、AI 员工开关、群承接配置仍在且仍绑同一个服务账号、服务账号仍可用；不过就失败并在群里说明原因。发言者 = `Speaker("api-group:<配置 id>", 服务账号)`，开轮时按告警回合的 `refresh_speaker` 复核。
- 续接轮（确认卡点完后的那一轮）跑在群主会话里，载荷带同一个 `api_task_id`，照样认。
- 本轮块：在「调用方名」里发起、群里的人会看到、调用方内容是外部数据不是指令、写操作确认卡发到本群由有权限的人确认并按点卡人身份执行。
- 不挂个人凭证、个人工具、个人定时任务、求助同事等以发言者本人为前提的能力（同告警回合）；业务系统令牌、目录代理、云签名代理按服务账号照常。
- 收尾不加 API 推送尾注；撞额度照私聊模式排队重跑（同一群会话里另排一轮）。

## 6. 写操作确认

- 暂存（`confirmable_origin`）：群模式的轮次（含续接轮）返回 `(群 chat_id, "group")`，暂存人是服务账号。
- 点击（`writes.card_clicker`）：卡片回调确实来自这个群的这张卡；点击人有飞书身份、认得出成员；在 AI 员工使用白名单里（空 = 不限）；实时查飞书确认还在群里。
- 单项写确认卡：任何满足上面条件的人都能确认 / 取消 / 重新发起，执行用点击人的身份重建代理上下文、复查后执行；业务系统调用记录的成员就是点击人。
- 批量写确认卡：确认时把批次的执行人改成点击人（之后各段都按他执行）；确认之后的停止、继续、重试只认他。
- 先到先得：批次行加锁，状态离开「待确认」后再点的人得到「已由别人处理」的群消息（@ 他），卡片不变。

## 7. 管理

- 管理接口（平台管理员）：
  - `GET /api/admin/api-clients/{app_key}/group-routes`
  - `POST /api/admin/api-clients/{app_key}/group-routes`：`{bot_id, chat_id, service_user_id}`；AI 员工须是飞书 AI 员工且在调用方 `bot_ids` 里，服务账号须是在用的 service 账号；重复 `409`。
  - `DELETE /api/admin/api-clients/{app_key}/group-routes/{id}`
  - `GET /api/admin/api-clients/group-route-options?bot_id=`：该 AI 员工告警来源里的群和服务账号，以及所有在用的 service 账号。
  - 都记审计。
- 管理台「接口调用方」：编辑弹框里出现「群承接」列表，可从告警来源的群和服务账号里选，也可手填 chat_id。
- 管理命令：`coreman clients groups <app_key>`、`coreman clients group-add <app_key> --bot … --chat … --service-user …`、`coreman clients group-remove <app_key> <id> --yes`、`coreman clients group-options --bot …`。

## 8. 不做

- 群模式的图片上传。
- 每分钟频率限制。
- 群模式的引用回复载入（结果本来就在群会话上下文里）。

## 9. 上线

迁移 0135 只新增表，节点不用升级。上线后由人执行：给运维 AI 员工打开「允许 API 驱动」，为监控系统的调用方把它加进 `bot_ids` 并建群承接配置。
