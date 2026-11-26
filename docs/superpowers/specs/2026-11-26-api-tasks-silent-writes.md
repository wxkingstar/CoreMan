# 业务系统代员工调用：静默执行与写接口白名单

状态：已实现（2026-11-26）。前置见 [2026-11-24-api-tasks-system-clients.md](2026-11-24-api-tasks-system-clients.md) 与 [2026-11-25-api-client-group-delivery.md](2026-11-25-api-client-group-delivery.md)。

## 1. 背景

业务系统以员工身份调用 AI 员工时，每个写回都要在员工飞书私聊里点确认卡，任务内容和回复也都推到私聊。有些写回只是「AI 结果槽」（受众初评、AI 审稿结果、改写草稿），人之后还要在业务系统里复核：对这类写回，确认卡和私聊消息都是打扰；系统自动发起的任务，确认卡没人点还会过期。要调哪个写接口过去只写在 prompt 的自然语言里，平台不强制。

## 2. 决策

- 平台提供静默模式和写接口白名单，不让业务系统自己解析回复写库。
- 静默只能用于**调用方级别预先登记**的接口：调用方密钥泄露或代码写错时，也只能静默调用登记过的接口。
- 涉及资金、收款账户的接口不应进静默清单，但由管理员配置把关，代码不硬编码。
- 公司规则是 AI 的写操作要人审批：静默模式下审批挪到业务系统里的人工复核；文档写清静默只适用于这类场景。

## 3. 接口

`POST /api/infra/tasks/run` 新增两个可选字段，都参与签名（列表展开成 `键[i]=值`，true 写成 `1`）：

| 字段 | 说明 |
|---|---|
| `allowed_writes` | 字符串列表，`<业务系统标识>.<operationId>`。带了就只允许这些写操作，名单外的拒绝；不带表示不限。空列表 422：空列表不参与签名，中间人删掉它就从「一个都不许」变成「不限」。最多 100 项。 |
| `silent` | `true` 时静默执行；`allowed_writes` 必填且每项都在调用方的 `silent_writes` 里，否则 422（`write_not_registered`，`message` 指出哪一项）。 |

- 非静默时 `allowed_writes` 只是收窄，不要求登记过（它不会让任何写操作免确认）。
- 幂等指纹加上 `silent` 和排序去重后的 `allowed_writes`：同 `request_id` 换值 409；同内容未结束去重也比较它们。
- 群承接模式：`silent` 直接 422（`silent_group`）——结果本来就发在群里，群里有人接着讨论，没有「不打扰」的需求；`allowed_writes` 照常生效。
- 查询结果回报 `silent`、`allowed_writes`；`writes` 新增状态 `not_allowed`，静默执行的项带 `"silent": true`，状态 `succeeded` / `failed` / `unknown`。`writes` 按发生先后排（确认卡按暂存时间，直接执行与拒绝按调用时间）。
- 错误码 `user_input_required`：静默执行时 AI 员工需要问人。

## 4. 数据

迁移 `0137`：

- `api_clients.silent_writes`：text[]，默认空。管理台「API 调用方」与 `coreman clients set --silent-write` 维护；改动记 `api_client.update` 审计（前后值）。保存时校验系统存在且经平台代理，目录可用时操作要在目录里且不是读操作。
- `api_tasks.silent`（bool，默认 false）、`api_tasks.allowed_writes`（text[]，NULL = 不限）。

## 5. 执行

- 发起：静默时不写「在某系统里发起」那条出站消息；chat 任务载荷带 `api_silent`（续接、额度重跑都沿用载荷）。
- 开轮：流的回复上下文带 `_silent`（网关不推、收尸不补发）；看护器不推进度；能力同个人助理调度轮——只注入已保存的凭证，不挂求助同事、个人工具、定时任务、群文档、任务平台、凭证索取、云产品代理，也不给回复图片、附件说明。本轮块写明静默执行和可做的写操作。开轮前复核没通过只记失败，不在私聊说明。
- 收尾：按「不出声」的一轮收尾（不加推送尾注、不发卡、不发长任务完成提醒、不主动推终稿），对话记录与用量照常记。模型提问（`ask_user`）改判失败 `user_input_required`，题面留在回复里。
- 收尸：进程丢失时不往私聊发「异常中断」。
- 代理写操作（`systems_call`）：目录 MCP 每次调用按任务找到 API 任务行，把 `allowed_writes` 和 `silent` 放进代理上下文（`caller_writes`、`silent_writes`）。
  - 名单外的写操作（write / destructive / financial）在策略检查之前拒绝，记 `system_calls.outcome = not_allowed`，返回 `write_not_allowed_by_caller`。
  - 静默且在名单里：不暂存、不发卡，经 `proxy.send` 以员工身份执行（同确认后执行：服务端现签的员工业务令牌），`outcome` 前缀 `silent_`。仍受「允许写入」授权与每轮代理调用上限约束。
  - 静默任务的批量写拒绝（没有人能确认整批）。
  - `systems_describe` / `systems_browse` 的调用说明里，名单外的写操作标「发起的业务系统未允许」，静默允许的写操作不再标「需确认」。

## 6. 不做

- 令牌下发到运行环境（`token_delivery = env`）或 MCP 的系统不受 `allowed_writes` 约束：AI 员工自己拿令牌调用，平台拦不到；这类系统本来也不发确认卡，静默不让它更宽。
- 静默模式仍要求员工绑定飞书身份并私聊过 AI 员工（复用发起时的身份核验与承接会话）。
- 不按风险硬编码禁止登记 financial / destructive 接口。
