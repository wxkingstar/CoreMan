# 个人凭证：按需索取、加密保存、本人触发才注入

状态：已实现，待真机验证
日期：2026-10-03

## 1. 目标与边界

AI 员工执行任务时如果需要某个系统的账号、密码或 API Key，可以向**当前用户本人**索取。用户通过飞书卡片表单或企业微信 H5 页面提交，提交内容不经过聊天，直接由 CoreMan 加密保存。之后只有这个用户本人触发的轮次，才把这份凭证以环境变量的形式注入 AI CLI。

已确认的产品决定：

- **按（AI 员工, 用户）隔离**。在 A 里提交的凭证只给 A 用，不需要额外设置；换到 B 要重新提交。
- **索取哪些凭证由 agent 按场景判断**，不需要预先声明。平台只保留技术上必须的校验（第 5.3 节）。
- **飞书用卡片表单，企业微信用 H5 页面**，两条路径汇入同一个提交服务。
- **注入范围**：本人私聊、群聊中本人 @ 机器人、本人创建的定时任务、提交凭证后的续接轮。同事求助后的续接轮、机器人之间的协作轮不注入。

安全承诺的边界要写清楚：

- **能保证**：提交过程不经过聊天、不进入模型上下文、不进入对话记录；数据库里只有密文；任何接口都不返回明文。
- **不能保证**：注入之后，agent 通过 shell 读得到自己的环境变量（CLI 以免审批模式运行）。这部分只靠提示词约束和出站脱敏兜底，彻底解决需要二期的「凭证代理」（第 14 节）。因此面向用户的文案只说「提交内容不经过聊天、不发送给 AI 模型」，不说「AI 永远看不到」。

非目标见第 14 节。

## 2. 平台能力依据

以下都已按官方文档核对。

| 能力 | 结论 |
|---|---|
| 飞书表单 | Card 2.0 的 `form` 容器加 `input` 组件，点提交后一次性回调 `form_value` |
| 飞书密码输入 | `input_type: "password"`，输入内容显示为「•」；只能单行；`max_length` 最大 1000 |
| 飞书「仅特定人可见」卡片 | 只在在线客户端显示、没有通知、不支持话题群，**不采用** |
| 企业微信模板卡片 | 只有 `text_notice`、`news_notice`、`button_interaction`、`vote_interaction`、`multiple_interaction` 五种，**没有文本输入**，所以只能用 H5 |
| 企业微信网页身份 | 复用现有的 OAuth 登录（`/api/auth/wecom/start?mode=oauth`），要求配置了带 `login` 能力的自建应用 |

## 3. 总体流程

```
agent（本轮持有 $COREMAN_CREDENTIAL_URL / _TOKEN）
  │ POST /api/runtime/credentials/requests  {fields, purpose}
  ▼
credential_requests（open，1 小时过期）
  │
  ├─ 飞书：私聊发 Card 2.0 表单（credential@<id>）
  │     └─ 回调 → 网关立即封存（加密）→ 快车道 card_action → submit()
  │
  └─ 企微：在来源会话发链接 → /my-credentials/requests/<id>
        └─ OAuth 登录 + CSRF → POST /api/me/credential-requests/<id>/submit → submit()

submit()：核对提交人 → 加密写入 personal_credentials → 续接原会话
  ▼
续接轮 / 之后的本人轮次：开轮时按发言者解密注入 env，并加入出站脱敏集合
```

## 4. 数据模型（迁移 0054）

### 4.1 `personal_credentials`

| 列 | 说明 |
|---|---|
| `id` | uuid 主键 |
| `bot_id` | 外键 `bots`，`ON DELETE CASCADE` |
| `user_id` | 外键 `users`，`ON DELETE CASCADE` |
| `env_key` | 环境变量名 |
| `label` | 显示名，取自最近一次索取 |
| `secret` | 是否密文字段。账号类字段可以为 false，「我的凭证」页会显示原值 |
| `value_enc` | 密文（第 4.3 节） |
| `created_at` / `updated_at` | |
| `last_used_at` | 最近一次被注入的时间；距上次超过 1 小时才更新，避免每轮都写库 |

唯一约束为 `(bot_id, user_id, env_key)`，同名的值重新提交时直接覆盖。

### 4.2 `credential_requests`

| 列 | 说明 |
|---|---|
| `id` | uuid 主键，也作为卡片 `task_id` 与 H5 链接中的随机 ID |
| `bot_id` / `user_id` | 发起人，也就是凭证的所有者 |
| `origin_kind` | `chat` 或 `cron` |
| `origin_task_id` | 发起索取的那一轮任务 |
| `origin_chat_id` / `origin_chat_type` | 来源会话；定时任务为 `cron:<job_id>` |
| `origin_session_key` / `origin_event_id` | 来源轮次的会话键与入站事件，续接轮沿用；不建外键，避免挡住入站事件的保留期清理 |
| `origin_relay_session_id` | 来源轮次用的 relay 会话（可空，定时任务为空）：续接前核对它仍是该会话键当前的会话，重置或切换后不再续接（评审裁定 R26）；同一对话内的去重也要求它相同 |
| `delivery_chat_id` | 表单或链接实际发到的会话，「已保存」等通知也发到这里 |
| `cron_job_id` | 来源是定时任务时填 |
| `fields` | JSONB：`[{key, label, secret, placeholder}]`，**不含任何值** |
| `purpose` | agent 写的用途说明，限长 300 字 |
| `status` | `open` / `submitted` / `expired` / `cancelled` |
| `expires_at` | 创建后 1 小时 |
| `request_outbox_id` | 表单消息对应的 outbox 记录，用于更新卡片 |
| `resume_task_id` | 续接任务 |
| `submitted_at` | |

### 4.3 加密

- 沿用 `coreman.core.crypto.Cipher`：AES-256-GCM，支持 `MASTER_KEY` 轮换。
- 每条凭证单独加密，AAD 绑定行身份：`personal_credentials.value_enc:{bot_id}:{user_id}:{env_key}`。直接改库把一个人的密文挪到另一个人或另一个键上，解密会失败。
- 飞书回调的封存副本单独用一个 AAD：`credential_requests.sealed:{request_id}`。
- 明文只在三个地方出现：
  - 网关封存那一刻；
  - `submit()` 加密前；
  - 开轮注入时。
- 任何 API 都不返回明文，包括 AI 委员会和平台管理员。这一点与机器人 env 的 `env_vars_full` 不同。
- 审计日志用 `record_audit` 记录保存、更新、删除，只记 AI 员工、用户和键名。

## 5. 索取接口

### 5.1 本轮能力令牌

在会注入个人凭证的轮次（第 8.1 节）里，同时下发两个变量：

- `COREMAN_CREDENTIAL_URL` = `{PUBLIC_BASE_URL}/api/runtime/credentials/requests`
- `COREMAN_CREDENTIAL_TOKEN`：用 `Cipher` 加密的声明，写法与定时任务的能力令牌相同。
  - 声明内容：`task_id`、`user_id`、`bot_id`、`base_session_id`、`origin_kind`、`chat_id`、`chat_type`、`relay`（本轮的 relay 会话，定时任务为空）、过期时间。
  - 过期时间与 `BOT_TOKEN_*` 一致：本轮超时 + 300 秒。

`COREMAN_CREDENTIAL_` 加入 `is_reserved_key` 的保留前缀，机器人 env 和技能预设里的同名变量一律丢弃。

运行节点不需要升级：daemon 只拦截几个固定的 `COREMAN_*` 令牌名，新前缀能原样到达 CLI。

### 5.2 请求与响应

```
POST /api/runtime/credentials/requests
Authorization: Bearer $COREMAN_CREDENTIAL_TOKEN

{
  "fields": [
    {"key": "DEMO_USERNAME", "label": "Demo 系统账号", "secret": false},
    {"key": "DEMO_PASSWORD", "label": "Demo 系统密码", "secret": true, "placeholder": "登录 Demo 系统用的密码"}
  ],
  "purpose": "查询你本月的订单需要登录 Demo 系统"
}
```

| 结果 | 响应 |
|---|---|
| 已发出表单 | `202 {"status":"form_sent","request_id":"…"}` |
| 同一用户在本 AI 员工下已有键集合完全相同、来源也相同（同一对话会话或同一定时任务）的 `open` 请求 | `200 {"status":"already_pending","request_id":"…"}`，不重复发；来源不同则另发一张表单，续接才能回到各自的来源 |
| 键名不合法 | `422`，逐个说明原因 |
| 无法送达（企微未配置登录应用、定时任务来源且没有私聊记录等） | `409`，附原因，不创建请求 |
| 请求体超过 64 KiB | `413`，不创建请求 |
| 令牌无效或过期 | `401` |
| 来源任务已结束、用户已停用或 AI 员工已停用 | `403` |

两种成功响应都附带一句给 agent 的话：「已向用户发送安全表单。请简短告诉用户去填写，然后结束本轮；用户提交后会自动续接。」定时任务来源的令牌拿到另一句，不承诺续接：「定时任务不会因为用户提交而续接：请在输出里说明本次缺少凭证，然后结束本轮；用户提交的值从下一次定时运行起生效。」

### 5.3 校验

按产品决定，只保留技术上必须的校验：

- `key` 符合 `ENV_KEY_RE`（`^[A-Z][A-Z0-9_]{0,63}$`）；不命中 `is_blocked_env_key`、`is_reserved_key`；不以 `COREMAN_` 开头。
- 单次 1–20 个字段（受飞书卡片元素预算限制），同一请求内键名不重复。
- `label` ≤ 50 字，`placeholder` ≤ 100 字，`purpose` ≤ 300 字。
- 不限制每人凭证总数，也不限制索取频率。

## 6. 表单

### 6.1 飞书卡片

- **发送目标**：优先发到本人与该 AI 员工的私聊（`user_reached` 记录，目标里带 `recipient_user_id`，发送前由 `private_target_valid` 重新核验）。来源是群聊时，群里再回一句「已私信你一张安全表单」。
- **没有私聊记录时**：表单直接发到来源群并 @ 发起人。只有发起人提交有效，第 7.1 节会校验点击人；`password` 输入框的内容只在填写人本地，其他人看不到。
- **卡片由系统构建**（新模块 `coreman/core/personal_credentials/cards.py`），包含：
  - 标题：卡片 header 主标题「🔒 需要你的个人凭证」，副标题「AI 员工「{bot_name}」」；
  - 用途：agent 的 `purpose`，作为纯文本渲染；
  - `form` 容器：每个字段一个 `input`，`name` 为键名，`required: true`，`max_length: 1000`，`secret` 字段用 `input_type: "password"`；
  - 提交按钮：`value.task_id = "credential@<request_id>"`；
  - 固定安全说明（第 6.3 节）；
  - 配置了飞书登录应用时，附「内容较长？用网页填写」链接，指向第 6.2 节的同一个页面。
- 模型自己写的卡片照旧会被 `clean_elements(strict=True)` 剥掉 form 和 input，模型无法伪造这种表单。

### 6.2 H5 页面（企微必用，飞书可选）

- 页面 `/my-credentials/requests/:id`，发送方式沿用 `/my-wecom`。有私聊记录并且有本人的平台身份时，链接发到私聊（来源是群聊时群里再回一句「已私信你一张安全表单」）；否则发回来源会话，因为页面只认发起人本人。
- 未登录时跳转企微或飞书 OAuth 登录，登录后回到原页面（`redirect` 限同站路径）。
- 页面显示 AI 员工名、用途、字段（`secret` 字段用密码框），以及第 6.3 节的说明。
- 链接里只有随机请求 ID，不带任何身份或个人信息。

### 6.3 用户可见文案

> 🔒 安全说明：此表单的内容直接提交给 CoreMan 加密保存，不经过聊天，不会发送给 AI 模型。只有你本人与「{bot_name}」对话、或运行你创建的定时任务时才会使用。请勿在此填写飞书、企业微信或邮箱的登录密码。

## 7. 提交

### 7.1 飞书回调：在网关里立即封存

- `gateway_feishu/inbound.py` 的卡片回调白名单加入 `credential@` 前缀。
- 解析到这类回调时，`normalize_event` 在**写库之前**完成三件事（调用方没有传入加密器时直接丢弃这条回调，宁可丢也不落明文）：
  1. 把 `form_value` 序列化后用 `credential_requests.sealed:{id}` 加密，放进 `card_action = {"task_id", "card_type": "credential", "sealed": …}`；
  2. 把 `raw.event.action.form_value` 替换为空对象；
  3. 不生成 `selected`。
- 网关子进程本来就能构造 `Cipher`（`cfg.build_cipher()`），通过参数传进 `normalize_event`。
- 结果：`inbound_events.payload` 和 `tasks.payload` 里都只有密文。
- 快车道 `CardActionHandler` 新增 `credential@` 分支，依次：
  1. **防伪**：沿用现有校验，outbox 中必须有 `status='sent'` 且消息 ID、`task_id`、会话都对得上的卡片；
  2. **核对点击人**：`operator` 的 open_id / user_id 必须对应请求的 `user_id`（`UserIdentity`），不符时这次点击被静默忽略（评审裁定 R18）：不提交、不改卡片，飞书只返回统一的受理 toast，发起人的表单保持打开；
  3. 解密封存副本并调用 `submit()`；
  4. **擦除副本**：从 `tasks.payload` 和 `inbound_events.payload` 里删掉 `card_action.sealed`，与 `submit()` 在同一事务内；
  5. **更新卡片**：改为「✅ 已保存 DEMO_USERNAME、DEMO_PASSWORD（内容不显示）」，不带任何值。

### 7.2 H5 提交

- `GET /api/me/credential-requests/{id}`：只有发起人能读，返回字段、用途、AI 员工名、状态、过期时间。
- `POST /api/me/credential-requests/{id}/submit`，请求体 `{"values": {KEY: value}}`。
- 两个接口都要求浏览器登录会话（`interactive_user`，不接受机器人令牌），写操作要求 `verify_csrf`。

### 7.3 `submit()`

位于 `coreman/core/personal_credentials/service.py`，飞书和 H5 共用：

1. 锁住请求行（`FOR UPDATE`）。状态不是 `open` 时返回「已提交」或「已失效」；已过期时置为 `expired` 并拒绝。
2. 核对提交人等于 `user_id`，且用户状态为 active、不是引导管理员。
3. **值校验**：
   - 每个字段都必填；
   - 去掉首尾空白；
   - 长度 1–4096（飞书侧受 1000 限制）；
   - 不允许 NUL 等控制字符。
4. 逐个字段加密，按 `(bot_id, user_id, env_key)` 插入或覆盖 `personal_credentials`。
5. 请求置为 `submitted`，写入审计日志（只记键名）。
6. 续接（第 7.4 节）。
7. 结算其他在等的请求：锁住（`FOR UPDATE SKIP LOCKED`）同一用户、同一 AI 员工下键全部被刚保存的键覆盖的其他 `open`、未过期请求，各自走第 5–6 步的状态、续接和卡片更新，不再写值，也不再记审计。

### 7.4 续接

- **来源是对话**：排一个新任务类型 `credential_resume`：`user_id=发起人`、`session_key=origin_session_key`、`inbound_event_id=origin_event_id`、`dedupe_key="credential-request:{id}:resume"`、`payload={"credential_request_id", "serialize_session": True}`。
  - 用独立类型而不是 `chat`：滚动升级时旧 worker 不认识它，会直接失败；如果用 `chat`，旧 worker 会把来源消息当新消息再跑一遍。
  - 处理器 `CredentialResumeHandler` 继承 `ChatTaskHandler`，写法与 `ChoiceSubmitHandler` 相同：覆盖 `_resolve`；企业微信的流从创建起就是主动推送（续接轮没有可用的 `req_id`），飞书沿用来源事件的回复上下文。
  - 发言者设为发起人，会话类型取来源会话的。
  - 用户消息是系统生成的固定文本：「[CoreMan] 用户已通过安全表单提交 DEMO_USERNAME、DEMO_PASSWORD，已作为环境变量注入本轮，值不会出现在对话中。请继续完成之前的任务。」
- **开轮守卫**：在 `_resolve` 里加锁检查，请求必须是 `submitted` 且 `resume_task_id` 等于本任务，发起人仍为 active 并在白名单内；不满足时结束任务，只发「已保存，下次对话生效」。AI 员工已停用时 `submit()` 不排续接任务。
- **会话核对（排任务前）**：`_resume` 先按 `(bot_id, origin_session_key)` 读 `ChatSession`，行不存在、请求没记 `origin_relay_session_id` 或两者不相等就不排任务，卡片和企业微信通知落到「下次对话时生效」，不承诺继续。这样重置后新对话的表单和重置前的旧表单被同一次填写结算时，只有新对话续接，旧表单不会再补一句「对话已重置」（评审裁定 R27）。
- **会话核对（开轮前）**：`_resolve` 里再核对一次，覆盖提交之后、任务被认领之前发生的重置：按 `(bot_id, origin_session_key)` 读 `ChatSession`，它的 `relay_session_id` 必须等于请求的 `origin_relay_session_id`。用户在表单打开期间重置、清除或切换了会话（行不存在或不相等，请求里没记也算），续接会落进另一个对话、没有那个任务的上下文，所以不续接：结束任务（`cancelled`，`credential_resume_session_changed`），只发「凭证已保存。对话已重置，请重新发起刚才的请求。」，与机器人协作的续接处理一致。`_resolve` 与开轮之间隔着排队等待，期间换模型、换运行时或清会话，默认的 `get_or_create` 会悄悄建一个空会话，所以 `CredentialResumeHandler._session_info` 在持有 bot 行锁的开轮事务里再核对一次：解析出的会话必须仍是请求记录的那个，否则开轮事务回滚（不留流、记录与新会话映射），任务同样按 `credential_resume_session_changed` 结束并发同一条通知。
- **来源是定时任务**：不续接，只私信「已保存，下次执行时生效」。
- **一次填写，所有在等的都续接**：被 7.3 第 7 步结算的请求各自按来源续接，所以两个对话会话在等同一组键时，填一张表单两边都会继续。

## 8. 注入

### 8.1 规则

新函数 `personal_env(session, cipher, bot_id, user_id)`，返回 `(env, secret_values, names)`。

| 轮次 | 注入谁的 | 下发索取令牌 |
|---|---|---|
| 私聊，已验证的本人发言 | 发言者 | 是 |
| 群聊，已验证的本人 @ 机器人 | 发言者（只有当前发言者的） | 是 |
| 定时任务执行 | 任务创建人（`cron_handler` 中的 `actor`） | 是 |
| 提交凭证后的续接轮 | 发起人 | 是 |
| 同事求助后的续接轮 | 不注入 | 否 |
| 机器人之间协作的轮次 | 不注入 | 否 |
| 发言者未知、已停用、引导管理员 | 不注入 | 否 |

调用位置：`runtime/worker/chat/opening.py`（对话）和 `runtime/worker/cron_handler.py`（定时任务），都放在 `build_env` 之后、`collect_secrets` 之前。

### 8.2 优先级与过滤

- 个人凭证 > 机器人 env > 技能 env。
- 注入时再按 `is_blocked_env_key`、`is_reserved_key` 过滤一遍，防止策略收紧后存量数据越界。
- 某条解密失败时跳过这一条，日志只记键名，不影响本轮。

### 8.3 出站脱敏

- `secret` 字段的值强制加入本轮 `ctx.secrets`，不再依赖键名里有没有 token、password 等标记。
- 长度阈值为 6 位：由 `store.injected` 按值收集后与 `collect_secrets(env)` 合并，`redaction.py` 不用改。
- 覆盖范围与现有机制相同：IM 投递、`chat_logs`、分类、定时任务结果。

### 8.4 提示词

追加一段「个人凭证」说明（新模块 `runtime/worker/chat/credentials.py`，结构与 `schedules.configure` 相同）：

- 列出发言者在本 AI 员工中已保存的变量名，例如 `$DEMO_USERNAME`、`$DEMO_PASSWORD`，**只给名字**。
- 缺少凭证或凭证失效时，给出用 `curl` 调用 `$COREMAN_CREDENTIAL_URL` 的示例；调用后简短告诉用户去填写，然后结束本轮。
- 规则：
  - 绝不让用户在聊天里发送密码或密钥；
  - 不得打印、回显、记录这些变量的值；
  - 不得写入文件、工作区（工作区是多人共用的）或 URL；
  - 只用于当前发言者本人的请求。

## 9. 「我的凭证」页面

- 路由 `/my-credentials`，登录后可用，与 `/my-wecom`、`/self-reminders` 同属个人页面。
- 按 AI 员工分组列出：标签、变量名、值（`secret` 字段只显示「已设置」，非密文字段显示原值）、更新时间、最近使用时间。
- 可以更新（重新填写）或删除。
- 接口：
  - `GET /api/me/credentials`
  - `PUT /api/me/credentials/{bot_id}/{env_key}`，CSRF
  - `DELETE /api/me/credentials/{bot_id}/{env_key}`，CSRF
  - 都只操作本人的数据。
- 没有配置飞书登录应用时，飞书用户可以让 agent 重新索取来覆盖旧值。

## 10. 过期与清理

- 清理任务（`runtime/scheduler/reaper.py`）把超时的 `open` 请求置为 `expired`；有飞书卡片时通过 outbox `card_update` 改成「已过期，请让 AI 员工重新发起」。
- 已提交、已过期、已取消的请求保留 90 天后删除，与 `tasks` 的保留期一致。请求里本来就没有值。

## 11. 错误处理

| 情况 | 处理 |
|---|---|
| 没有可发送的会话，或企微未配置登录应用 | 接口返回 `409` 并附原因，不创建请求，由 agent 告诉用户 |
| 表单过期后才提交 | 拒绝；卡片或页面提示重新发起 |
| 重复点击或重复提交 | 行锁加状态检查，第二次提示「已提交」 |
| 非发起人点卡片或打开链接 | 卡片点击静默忽略（飞书只返回统一的受理 toast，发起人的表单保持打开，评审裁定 R18）；页面返回 403 |
| 续接前 AI 员工已停用 | 不续接，只通知「已保存」 |
| 注入时解密失败 | 跳过该条，记录键名 |
| 凭证失效（例如外部系统返回 401） | agent 用同一个键重新索取，提交后覆盖 |

## 12. 防泄漏与残余风险

已有并继续生效的防线：

- **提交通道**：值不经过聊天，网关在写库前就加密，处理完即擦除副本。
- **存储**：逐行加密、AAD 绑定行身份、接口不返回明文。
- **伪造表单**：模型写的卡片不能包含 form 和 input。
- **使用**：提示词约束，加上强制出站脱敏。

已知残余风险（写进使用说明，一期不承诺消除）：

- agent 用 shell（`env`、`printenv`）读得到自己的环境变量。如果它把值打印出来，值就进入模型上下文；如果被网页或文件里的提示注入诱导，还可能外发。
- 运行节点上 CLI 自己的会话记录和会话查看器不做脱敏。
- 用户仍可能无视提示，把密码直接发在聊天里。
- 企业微信里 agent 仍可以自己发一条样式与系统消息相仿的 markdown 链接，把用户引到别处；只有系统构建的链接消息受保护，用户应使用带固定安全说明的那一条里的链接。

## 13. 测试

**单元测试**

- 加密：AAD 绑定，把密文换到另一行后解密失败。
- 注入：第 8.1 节表格逐行覆盖，包括群聊里换人发言。
- 键名校验：合法名、禁用键、保留键、`COREMAN_` 前缀、重复键。
- 脱敏：键名不含关键字的 `secret` 值也被替换；6 位阈值生效。
- 提示词：只出现变量名，不出现值。

**网关测试**

- 构造 `credential@` 回调，断言 `inbound_events.payload` 和 `tasks.payload` 的 JSON 文本里搜不到提交的值。
- 处理完成后 `sealed` 已被擦除。

**接口测试**

- H5：非本人 403、已提交 409、已过期或已取消 410、缺 CSRF 被拒、提交成功后排出续接任务。
- 飞书回调：点击人和发起人不一致时被拒。
- 索取接口：令牌无效或过期 401、`already_pending` 不重复发。

**集成测试**（本机测试库）

索取 → 提交 → 续接任务的 payload 中没有值 → 续接轮的 `ChatRequest.env_vars` 中有值，并且 `ctx.secrets` 包含该值。

**真机验证**

- 飞书桌面端和手机端的 `password` 输入框与表单回调。
- 企业微信手机端打开 H5，完成 OAuth 登录和提交。
- Codex 后端能否读到名字含 `KEY`、`SECRET`、`TOKEN` 的变量（Codex 的 `shell_environment_policy` 默认可能过滤这类变量），读不到时在驱动中显式放开。

## 14. 一期不做

- **凭证代理**：env 里只放占位符，由 CoreMan 侧工具或节点出口代理在请求发出时替换成真值，彻底不让 agent 接触明文。留作二期，用于高敏凭证。
- 管理员查看或代管用户凭证。
- 跨 AI 员工共享凭证。
- 定时任务的自动续接。
- 用聊天命令管理凭证。
- 文件类凭证（多行 PEM、JSON 密钥文件）。
