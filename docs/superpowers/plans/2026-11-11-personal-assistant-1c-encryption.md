# 个人助理 1C：加密存储 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `bots.kind='assistant'` 的记录在 `inbound_events`、`tasks`、`task_streams`、`outbox`、`chat_logs` 里的内容列（以及报错信息、附件信息、`feishu_sent_messages` 的正文存档）写库前用所有者的数据密钥加密、读出时解密；销毁密钥后内容永久不可读，重新签发后旧内容仍不可读；普通机器人的行完全不变。

**Architecture:** 每人一把 256 位数据密钥，用平台主密钥包裹后存新表 `assistant_keys`。新模块 `coreman/core/assistants/sealed.py` 负责按 `bot_id` 找所有者、封存与还原内容列。写入在 bus 层的写函数里封存；经 ORM 新建或修改的行由 `before_flush` 钩子按行的 `bot_id` 判断归属后封存（不依赖行上的还原标记）。读取由会话类 `SealedSession` 的 `get` / `scalar` / `refresh` 自动还原，其余取法（`scalars`、`execute`、只取列）在点名的位置显式还原。调用方拿到的始终是明文。

**Tech Stack:** Python、SQLAlchemy async（ORM 事件、`AsyncSession` 子类）、Alembic、PostgreSQL JSONB、AES-256-GCM（`coreman/core/crypto.py` 的 `Cipher`）、pytest。

**Spec:** `docs/superpowers/specs/2026-11-11-personal-assistant.md`（第二节「加密与存储」、「威胁模型」）；总览 `docs/superpowers/plans/2026-11-11-personal-assistant-phase1.md`

## Global Constraints

- 开工前合并最新 `main`。写本计划时迁移头是 `0086`，并行的另一份子计划可能占用 `0087`，本计划的迁移写作 `0088`、`down_revision = "0087"`。**开工时按当时迁移头顺延**：头仍是 `0086` 就把文件名与 `revision` 改成 `0087`、`down_revision` 改成 `"0086"`。
- 公开仓：代码、测试、提交信息只用通用占位（`example.com`、`demo-bot`、`cli_example`、张三/李四），提交前跑仓库约定的敏感词自查。
- 普通员工（`kind='standard'`）的行为不变：库里仍是明文，现有用例不改即通过。每个接入任务都带一条普通机器人的断言。
- 接口名沿用总览：`coreman.core.assistants.keys.data_cipher(session, cipher, owner_id) -> Cipher`、`coreman.core.assistants.keys.destroy_key(session, owner_id) -> None`。
- 数据密钥只在平台进程内解开：不写入任务载荷、环境变量、日志、异常消息。
- 进程内缓存 TTL 为 60 秒（`keys.KEY_TTL_SECONDS`）：别的进程销毁密钥后，本进程最迟 60 秒后读不出、写不进。
- 状态、时间、类型、长度、路由信息（`reply_context`、`outbox.target`）等元数据保持明文。
- 不做：转录加密（1E）、审批表（1D）、知识库（第 2 期）、存量数据迁移（本期之前没有助理记录）。
- 验证命令：`uv run pytest <路径>`、`uv run ruff check .`、`uv run mypy coreman`、`uv run alembic check`（`TEST_DATABASE_URL` 按本机配置）。

## 设计结论

### 密钥与 AAD

| 项 | 结论 |
|---|---|
| 数据密钥 | `os.urandom(32)`，base64 后用主密钥 `Cipher.encrypt` 包裹，AAD `assistant_keys.dek_enc:<user_id>`，存 `assistant_keys.dek_enc`（`enc:v2:<主密钥 kid>:…`） |
| 内容加密 | `Cipher(dek, key_id=f"d{generation}")`，密文形如 `enc:v2:d1:…`。密文里的 `d<n>` 记下它是第几代密钥加密的 |
| 代数与重新签发 | `assistant_keys.generation` 从 1 起。`destroy_key` 清空 `dek_enc`、记 `destroyed_at`；之后 `ensure_key` 在已销毁的行上签发新密钥：`generation + 1`、写入新 `dek_enc`、清 `destroyed_at`。读到代数不等于当前代数的密文一律按「已锁定」处理（不抛 `DecryptError`），所以旧内容永久不可读、新内容正常。代数相同却解不开才算篡改，照常抛错 |
| 谁能重新签发 | 只有显式调用 `ensure_key`（1F 的开通流程）。写入路径首次用到时的自动生成传 `reissue=False`：否则停用之后一次迟到的写入就会把销毁撤销掉 |
| 内容 AAD | `<表名>.<列名>:<bot_id>`。**没有采用设计文档写的「表名:行主键」**：`inbound_events`、`tasks`、`outbox`、`chat_logs` 的主键都是 `Identity(always=True)`，插入时才生成，而载荷在同一条 `INSERT` 里写入；按主键绑定就得先插入再更新（每行多一次写），或绕过 `GENERATED ALWAYS` 预取序列。按 `bot_id` 绑定后，密文不能挪到别的表、别的列、别的助理；同一助理同一列的两行之间可以互换，这一点比设计弱，需同步改设计文档 |
| 主密钥轮换 | 内容密文只认数据密钥，与主密钥无关。轮换后旧主密钥留在 `MASTER_KEYS_PREVIOUS`，`dek_enc` 按自身 kid 照常解开，内容一个字节不用动。但 `dek_enc` 写一次就不再改，不会像其他凭证那样「随下一次写入自然迁移」，所以加管理命令 `assistant-keys rewrap`（Task 12），跑完才能把旧 kid 从 `MASTER_KEYS_PREVIOUS` 移除 |
| 缓存 | `keys.py` 按 `owner_id` 缓存数据密钥、`sealed.py` 按 `bot_id` 缓存所有者，TTL 都是 60 秒。本进程 `destroy_key` 立即清缓存。重新签发后，别的进程最迟 60 秒换到新一代；这期间它用旧一代写下的内容之后读不出来（停用到重新开通之间记录是停用的，实际不会有写入） |
| 平台主密钥从哪来 | bus 函数签名里没有 `cipher`。`sealed.master()` 取 `get_settings().build_cipher()`（所有进程本来就从这里造）；测试用 `sealed.use_master(cipher)` 指定 |

### JSONB 与文本列的封存形式

| 形态 | 库里的样子 | 用在 |
|---|---|---|
| 字典，部分键留明文 | `{"_sealed": "enc:v2:d1:…", <明文键>…}`。密文里是去掉明文键之后的其余部分 | `inbound_events.payload`、`tasks.payload`、`outbox.payload`、`task_streams.activity` |
| 整值封存 | `{"_sealed": "enc:v2:d1:…"}` | `tasks.result`、`task_streams.pending_card`、`task_streams.plan`、`chat_logs.file_info` |
| 文本 | `enc:v2:d1:…`；空串与 NULL 不加密 | `task_streams.thinking_md` / `pending_text` / `final_text`、`chat_logs.message_content` / `quoted_content` / `response_content` / `error_message`、`tasks.error_message`、`feishu_sent_messages.text` |
| 墓碑 | `{"_sealed": "", <明文键>…}`：密钥销毁之后写入，内容已丢弃 | 上面两种 JSONB 形态 |

还原规则：密文里的内容与封装顶层的其它键合并，**顶层的键优先**。所以对未还原的封装追加、删除明文键（裸 SQL 的 `payload || …`、网关写 `_feishu_message_id`）不需要密钥，也不会丢。

### 载荷里被 SQL 直接读取的字段（必须留明文）

| 表.列 | 字段 | 读取处 | 处理 |
|---|---|---|---|
| `tasks.payload` | `serialize_session`、`stale_hold` | `coreman/core/bus/tasks.py:31,38,285`、`coreman/runtime/gateway_common/inbound.py:272` | 明文键 |
| `tasks.payload` | `platform_user_id` | `inbound.py:273,296,365`、`coreman/core/chat/busy_choice.py:292`、`coreman/runtime/worker/busy_choice.py:111` | 明文键 |
| `tasks.payload` | `origin`、`busy_offer` | `inbound.py:295,304,364` | 明文键 |
| `tasks.payload` | `escalation_id`、`message_id` | `coreman/core/escalations/media_recovery.py:21-22,41-42` | 明文键 |
| `tasks.payload` | `skill_id` | `coreman/core/knowledge/installation.py:228` | 明文键 |
| `tasks.payload` | `doc_links` | `coreman/core/feishu_docs/access.py:225`（`has_key` 后整列取回）、`coreman/runtime/worker/chat/feishu_docs.py:130`（裸 SQL `payload \|\| …` 写入） | 明文键。值是文档 token 列表，不是正文 |
| `outbox.payload` | `_feishu_message_id` 等下划线开头的内部键 | `coreman/core/escalations/service.py:362`、`coreman/core/feishu_docs/consent.py:219`、`coreman/runtime/gateway_feishu/reply_buttons.py:143`、`coreman/runtime/worker/card_actions.py:94`、`coreman/runtime/worker/chat/feishu_quote.py:113` | 规则：下划线开头的键一律明文（它们本来就不发给平台，见 `outbox.public_payload`） |
| `outbox.payload` | `card.task_id`（嵌套） | `card_actions.py:96`、`coreman/runtime/worker/choice_flow.py:69` | 明文镜像：封装顶层另存一份 `{"card": {"task_id": …}}`，还原时以密文里的整张卡为准 |
| `outbox.payload` | `markdown`（取值，不是过滤） | `feishu_quote.py:107` | 是正文，不留明文；改成取整行再读（Task 10） |
| `inbound_events.payload` | `raw.event.message`（取值） | `feishu_quote.py:252` | 同上，改成取整行（Task 10） |
| `inbound_events.payload` | 整列 | `coreman/core/chat/group_observe.py:216` | 只处理群消息，助理不收群消息，不改 |
| `tasks.payload`、`inbound_events.payload` | `card_action.sealed`（裸 SQL `#-` 删除） | `coreman/runtime/worker/credential_cards.py:27,33` | 对封装是空操作，补一段经 ORM 删除（Task 10） |
| `outbox.target` | `chat_id`、`user_id`、`escalation_*`、`alert_key` | 多处 | `target` 整列不加密（路由元数据） |
| `chat_logs` | `message_content` / `response_content` 的 `ILIKE` | `coreman/api/routers/chat_logs.py:115-116` | 助理记录按关键词搜不到，接受 |

| `tasks.error_message`、`chat_logs.error_message`、`chat_logs.file_info` | 无 SQL 过滤、聚合或告警读取（告警与统计只看 `status`、`error_code`） | `coreman/runtime/scheduler/reaper.py:224` 只把任务的报错原样抄到对话记录 | 可以封存（Task 13）；`error_code`、`status` 保持明文 |
| `feishu_sent_messages.text` | 只按主键取值 | `coreman/runtime/worker/chat/feishu_quote.py:82`；`coreman/core/chat/group_observe.py:229` 只处理群消息 | 可以封存（Task 13）；`work_code`、`chat_id`、`created_at` 保持明文 |

| `task_streams.activity` | `label`（工具说明、命令首行，最多 80 字，会带出对话里的东西） | 没有 SQL 过滤、聚合或告警读取；只有网关、巡检、实时进度在已还原的行上读属性 | 封存。`at`、`until`、`expect`、`stuck`、`interrupted` 是规则生成的时间与状态，留明文键：读不出来时状态行照常显示，只是没有说明文字 |
| `outbox.last_error` | 整列 | 运维接口 `GET /api/admin/runtime/outbox`、管理命令的出站列表、定时任务投递结果直接输出；`human_collaboration.delivery_error` 按里面的飞书错误码判断；求助通知失败原因直接取用 | **不封存**。来源里有平台回的错误说明、异常文字（`Feishu <code>: <reason>`、`not_sent: <exc>`）。助理的行在 `outbox` 的五个写入口只留开头的代码（`sealed.code_for_assistants`）：一个标识符加可选的数字（`Feishu 230013`、`not_sent`、`TimeoutError`），不是这个形状的整个换成 `redacted` |
| `tasks.cancel_reason` | 整列 | `tasks.heartbeat` 用 `RETURNING` 取回交给 worker，随后被当作 `error_code` 写回；`api_tasks`、`approval_resume` 按值判断；运维接口 `GET /api/admin/runtime/tasks` 与管理命令直接输出 | **不封存**。绝大多数是固定代码（`superseded`、`user_stop`……），协作关闭、管理员手填时是自由文字。助理的行在 `tasks.request_cancel` 里只留代码，自由文字换成 `cancelled` |

前四列盘点下来没有一列需要留明文；后三列是交付前补查的。`code_for_assistants` 在原值本来就是代码时原样写入，否则写成随语句判断归属的 `CASE WHEN EXISTS(助理记录) …`，不多一次往返，普通机器人的行拿到的仍是原文。没有任何索引、唯一键、去重键建在这些内容列上（去重用 `dedupe_key`、`platform_msg_id`）。

### 加解密所在的层

**写入口（全部在 bus 层，共 7 处语句 + 1 个钩子）：**

| 表 | 入口 | 位置 | 任务 |
|---|---|---|---|
| `tasks` | `enqueue`（`insert(Task)`） | `coreman/core/bus/tasks.py:78` | 5 |
| `tasks` | `finish`（`result`） | `coreman/core/bus/tasks.py:139` | 5 |
| `inbound_events` | `enqueue_inbound`（`insert(InboundEvent)`） | `coreman/runtime/gateway_common/inbound.py:52` | 6 |
| `inbound_events` | API 任务合成事件 | `coreman/core/api_tasks/service.py:225` | 6 |
| `task_streams` | `update_state`、`complete` | `coreman/core/bus/streams.py:73,170` | 7 |
| `outbox` | `add` | `coreman/core/bus/outbox.py:87` | 8 |
| `chat_logs` | `_finish`、`open_turn`、`insert_turn`、`ChatLogWriter.write`、`amend_response` | `coreman/core/chat/chat_logs.py:161,177,220,293,307` | 9 |
| `tasks` | `finish`（`error_message`） | `coreman/core/bus/tasks.py:139` | 13 |
| `chat_logs` | `file_info`、`error_message` 随上面五个函数的整行写入 | 同上 | 9 |
| `chat_logs` | `close_running`（`error_message`） | `coreman/core/chat/chat_logs.py:238` | 13 |
| `chat_logs` | 巡检结掉漏网的进行中记录（`error_message`） | `coreman/runtime/scheduler/reaper.py:247` | 13 |
| `feishu_sent_messages` | `remember`（`insert(FeishuSentMessage)`） | `coreman/runtime/gateway_feishu/transport.py:205` | 13 |
| 六张表 | 经 ORM 新建（`session.add`）或修改的行（`task.payload = {...}`、`item.payload = {...}` 等约 25 处），含读出时内容为空的行与已锁定的行 | `sealed.py` 的 `before_flush` 钩子，按 `bot_id` 判断归属 | 4 |

`streams.create`（`TaskStream(...)`）建流时没有内容，不需要封存。`switch_to_proactive` 只写元数据。Task 10 的台账测试保证这六个模型的 `insert` / `update` / 构造不出现在上表之外的文件。

`feishu_sent_messages` 的读取处：`feishu_quote.py:82`（只取 `text` 一列，Task 13 还原）、`coreman/runtime/worker/chat/work.py:142`（只取 `work_code`，不改）、`group_observe.py:229`（群消息，助理不涉及，不改）、`reaper.py:458`（按时间删，不改）。`tasks.error_message` 的读取处：`reaper.py:224`（只取列，Task 13 还原）；其余都是 `session.get(Task, …)` 之后读属性，自动还原。

**读入口：**

| 取法 | 数量 | 处理 |
|---|---|---|
| `session.get(Task / InboundEvent / OutboxItem / TaskStream, …)` | 约 110 处，散在 `coreman/core/`、`coreman/runtime/`、`coreman/api/` | `SealedSession.get` 自动还原，调用点不改 |
| `session.scalar(select(<模型>)…)` | 约 15 处 | `SealedSession.scalar` 自动还原 |
| `session.refresh(row)` | `streams.create` | `SealedSession.refresh` 自动还原 |
| `session.scalars(select(<模型>))` / `session.execute(select(<模型>))` | 见下表 | 逐处判断 |
| 只取内容列 | 4 处 | Task 9、10 |

没有采用「只在 bus 的读函数里解密」：读这四个模型的代码绝大多数是直接 `session.get`，不经过 bus 函数；逐个改调用点既多又挡不住以后新增的。在会话类上兜住 `get` / `scalar` / `refresh` 后，剩下的取法由台账测试点名。

`scalars` / `execute` 取整行的位置（Task 10 的台账与此一致）：

| 文件 | 行 | 读什么 | 处理 |
|---|---|---|---|
| `coreman/core/bus/tasks.py` | 194、208 | 会话里的活动任务，调用方会读载荷 | 显式还原（Task 5） |
| `coreman/core/bus/tasks.py` | 312 | 运维列表，只出元数据 | 不还原 |
| `coreman/core/bus/streams.py` | 215、242 | 待推的流 | 显式还原，跳过不可读的行（Task 7） |
| `coreman/runtime/gateway_feishu/transport.py` | 260 | 待推的流 | 同上（Task 7） |
| `coreman/core/bus/outbox.py` | 300 | 运维列表 | 不还原 |
| `coreman/runtime/scheduler/reaper.py` | 119、181 | 失联任务与未收尾的流，读载荷与正文 | 显式还原（Task 10） |
| `coreman/runtime/worker/chat/queued_batch.py` | 75 | 合并排队消息，读载荷 | 显式还原（Task 10） |
| `coreman/core/chat/session_carry.py` | 37 | 换运行时带上文，读正文 | 显式还原（Task 9） |
| `coreman/api/routers/chat_logs.py` | 202、311 | 对话记录列表与详情 | 只对所有者还原（Task 11） |
| `coreman/api/routers/dev.py` | 165 | 开发环境任务详情 | 只对所有者还原（Task 11） |
| `coreman/api/routers/infra_push.py` | 65 | 幂等重放时比对载荷 | 显式还原（Task 8） |
| `coreman/api/routers/runtime.py` | 224、247 | 运维列表，`task_out` / `outbox_out` 不含载荷 | 不还原（Task 11 加断言） |
| `coreman/api/routers/cron_jobs.py` 273、541，`self_reminders.py` 146，`coreman/core/escalations/service.py` 430 | | 只读状态 | 不还原 |
| `coreman/core/chat/busy_choice.py` 284，`coreman/core/chat/bot_collaboration.py` 546 | | 只读状态与时间 | 不还原 |
| `coreman/core/chat/bot_collaboration.py` 435，`coreman/core/chat/collaboration_setup.py` 473，`coreman/api/routers/infra_notify.py` 102，`coreman/runtime/scheduler/notifications.py` 23 | | 群协作、不绑机器人的通知 | 助理不涉及，不改 |
| 其余（`media_recovery.py` 39、`escalations/service.py` 337、`human_collaboration.py` 453/496、`installation.py` 223、`inbound.py` 265、`reply_buttons.py` 139、`worker/chat/api.py` 89、`choice_flow.py` 64、`cron_handler.py` 655、`skill_install.py` 109、`escalation_media.py` 119、`api_tasks/service.py` 328） | | 都是 `session.scalar(...)` | 自动还原 |

### 怎么知道一行该不该加密、用谁的密钥

- 读：先看值是不是封装（`_sealed` 键或 `enc:` 前缀），不是就原样返回，**普通机器人的行零额外开销**。是封装才按行上的 `bot_id` 查所有者。
- 写：`sealed.owner_of(session, bot_id)` 查 `bots.kind, owner_id`，结果在进程内缓存 60 秒（普通机器人缓存为「不加密」）。六张表都有 `bot_id` 列。
- `streams.update_state` / `complete`、`tasks.finish`、`chat_logs.close_running`、`ChatLogWriter.amend_response` / `submit_amend` 的参数里原本没有 `bot_id`：加可选参数 `bot_id`，调用方把手里已有的传进来；没传且确有内容要写时才按主键查一次（回退）。

### 普通机器人路径上的开销

| 位置 | 开销 |
|---|---|
| 读 | 零：值不是封装就原样返回，不查归属 |
| 写入口（`seal_values`、flush 钩子） | 有内容列要写时查一次归属（`bots.kind, owner_id`，按主键），结果按 `bot_id` 在进程内缓存 60 秒。稳定状态下每个机器人每个进程每分钟最多一条 |
| `tasks.finish`、`close_running`、`amend_response` | 不多语句。现有调用点（带 `result` 或 `error_message` 的 `tasks.finish` 42 处、`close_running` 3 处、`submit_amend` 1 处）全部传入 `bot_id`，没有拿不到的；回退查询只留作兜底。`tests/integration/test_assistant_standard_overhead.py` 数语句并逐个检查调用点，新增调用点漏传会失败 |
| `outbox.last_error`、`tasks.cancel_reason` | 值本来就是代码时原样写入；是自由文字时同一条语句里多一个按主键的 `EXISTS` 子查询，不多往返 |

### 密钥已销毁时各入口的行为

| 入口 | 行为 |
|---|---|
| `SealedSession.get` / `scalar` / `refresh`、`sealed.open_rows` | 不抛错。内容列置空（字典只剩明文键，文本为空串或 NULL），`sealed.is_locked(row)` 为真。这些调用遍布巡检与列表，抛错会让一个人的销毁拖垮所有人的循环 |
| `tasks.claim` | 任务置 `failed`，`error_code='content_locked'`，返回 `None`，不交给处理器 |
| 飞书网关 `consume_one` | 条目置 `skipped`，`last_error='content_locked'` |
| 企微网关 `OutboxConsumer._handle` | 同上。助理记录只能是飞书（`bots` 的检查约束），它的条目正常到不了这里；仍加同样的判断，不留「读不出来的内容被当成空消息发出去」的路径。判断只是读行上的一个标记，普通条目没有开销 |
| `streams.pending_for_bot` / `active_for_bot`、飞书网关 `_deliver` | 跳过该行 |
| 对话记录接口 | 正文字段为 `null`，带 `content_locked: true` |
| 任何写入 | 不抛错。内容丢弃：字典写成墓碑加明文键，文本写空；行与元数据照常落库，状态机照走。记一条 `assistant_content_dropped` 告警 |
| 重新签发之后读旧一代的密文（`d<n>` 不等于当前代数） | 与密钥已销毁走同一条路径：置空、`is_locked` 为真，不抛错。新一代写下的内容正常读写 |
| 主密钥配错、代数相同的密文被改 | `DecryptError` 照常抛出（与现有凭证列一致），不当作销毁处理 |

### 后台清理、统计、导出、迁移

| 代码 | 是否读内容 | 处理 |
|---|---|---|
| `reaper.cleanup_outbox` / `cleanup_tasks` / `cleanup_inbound_events` | 否，按时间删 | 不改；密文行照常回收 |
| `reaper.reap_lost_tasks`、`recover_terminal_streams` | 是 | Task 10 |
| `reaper.close_abandoned_turns` | 否 | 不改 |
| `observability/metrics.py`、`alerts.py`、`knowledge/memory_transfer.py`、`runtime.py` 的队列概览 | 否，只计数 | 不改 |
| `core/chat/session_carry.py`、`session_switch.py`（换运行时带上文、最近会话预览） | 是 | Task 9 |
| `core/bots/rules_backup.py` | 否，备份的是文件 | 不改 |
| `migrations/versions/*` | 否，没有迁移读这些内容列 | 不改 |
| `scripts/` | 否 | 不改 |

## File Structure

| 文件 | 职责 |
|---|---|
| `migrations/versions/0088_assistant_keys.py` | 新表 `assistant_keys` |
| `coreman/core/db/models/assistants.py` | 模型 `AssistantKey` |
| `coreman/core/assistants/keys.py` | 数据密钥：生成、取用、销毁、重新包裹、缓存 |
| `coreman/core/assistants/sealed.py` | 封存形式的编解码；按行封存与还原；`before_flush` 钩子 |
| `coreman/core/assistants/session.py` | `SealedSession`：`get` / `scalar` / `refresh` 自动还原 |
| `coreman/core/db/session.py` | 会话工厂改用 `SealedSession` |
| `coreman/core/bus/tasks.py`、`streams.py`、`outbox.py` | 写入封存、列表还原、不可读时的处理 |
| `coreman/runtime/gateway_common/inbound.py`、`coreman/core/api_tasks/service.py` | 入站事件写入封存 |
| `coreman/core/chat/chat_logs.py`、`session_carry.py`、`session_switch.py` | 对话记录写入封存、读取还原 |
| `coreman/runtime/gateway_feishu/transport.py`、`coreman/runtime/worker/stream_writer.py`、`replies.py`、`service.py` | 流与出站的调用点 |
| `coreman/runtime/scheduler/reaper.py`、`coreman/runtime/worker/chat/queued_batch.py`、`feishu_quote.py`、`coreman/runtime/worker/credential_cards.py` | 散落的直接读取 |
| `coreman/api/routers/chat_logs.py`、`dev.py`、`infra_push.py` | 接口读取 |
| `coreman/core/bus/tasks.py`、`coreman/core/chat/chat_logs.py`、`coreman/runtime/scheduler/reaper.py`、`coreman/runtime/gateway_feishu/transport.py`、`coreman/runtime/worker/chat/feishu_quote.py` | Task 13：报错信息与已发消息存档的封存与还原 |
| `tests/integration/test_orm_flush_reseal_probe.py` | flush 钩子做法在 asyncpg 上的最小验证 |
| `coreman/cli.py`、`docs/operations.md` | `assistant-keys rewrap` 与轮换说明 |
| `tests/integration/assistant_helpers.py`、`tests/integration/conftest.py` | 测试公共件与主密钥夹具 |

---

### Task 1: 表 `assistant_keys`

**Files:**
- Create: `migrations/versions/0088_assistant_keys.py`
- Create: `coreman/core/db/models/assistants.py`
- Modify: `coreman/core/db/models/__init__.py`（导出 `AssistantKey`）
- Modify: `tests/conftest.py:24`（`BUSINESS_TABLES`）
- Test: `tests/integration/test_assistant_keys_schema.py`

**Interfaces:**
- Produces: `coreman.core.db.models.AssistantKey`，列 `user_id: uuid.UUID`（主键，外键 `users.id`）、`dek_enc: str | None`、`generation: int`（从 1 起，每次重新签发加 1）、`created_at: datetime`、`destroyed_at: datetime | None`。

- [ ] **Step 1: 写失败测试**

```python
"""assistant_keys：每人一行。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import AssistantKey, User
from tests.conftest import BUSINESS_TABLES


async def _user(session: AsyncSession) -> User:
    user = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name="张三")
    session.add(user)
    await session.flush()
    return user


def test_table_is_truncated_between_tests() -> None:
    assert BUSINESS_TABLES.index("assistant_keys") < BUSINESS_TABLES.index("users")


async def test_one_key_per_user(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    db_session.add(AssistantKey(user_id=user.id, dek_enc="enc:v2:k1:eA=="))
    await db_session.commit()
    # populate_existing：服务端默认值（created_at、generation）要重新读回来。
    row = await db_session.get(AssistantKey, user.id, populate_existing=True)
    assert row is not None and row.created_at is not None and row.destroyed_at is None
    assert row.generation == 1
    db_session.expunge(row)
    db_session.add(AssistantKey(user_id=user.id, dek_enc="enc:v2:k1:eQ=="))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_key_requires_existing_user(db_session: AsyncSession) -> None:
    db_session.add(AssistantKey(user_id=uuid.uuid4(), dek_enc="enc:v2:k1:eA=="))
    with pytest.raises(IntegrityError):
        await db_session.flush()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_keys_schema.py -v`
Expected: FAIL，`ImportError: cannot import name 'AssistantKey'`。

- [ ] **Step 3: 写模型**

`coreman/core/db/models/assistants.py`：

```python
"""个人助理的数据密钥。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base


class AssistantKey(Base):
    """每人一把数据密钥，用平台主密钥包裹；销毁后 dek_enc 为空，对应内容永久不可读。

    重新签发时 generation 加 1：内容密文记着自己是第几代加密的，旧一代的读不出来。
    """

    __tablename__ = "assistant_keys"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), primary_key=True)
    dek_enc: Mapped[str | None] = mapped_column(Text)
    generation: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    destroyed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

`coreman/core/db/models/__init__.py`：在 `from coreman.core.db.models.bots import (` 这一段之前加一行，并在文件末尾加一行：

```python
from coreman.core.db.models.assistants import AssistantKey
```

```python
__all__ += ["AssistantKey"]
```

- [ ] **Step 4: 写迁移**

`migrations/versions/0088_assistant_keys.py`：

```python
"""Personal assistant data keys: one wrapped key per owner."""

import sqlalchemy as sa
from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_keys",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("dek_enc", sa.Text(), nullable=True),
        sa.Column("generation", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("destroyed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("assistant_keys")
```

- [ ] **Step 5: 测试清表名单**

`tests/conftest.py`，`BUSINESS_TABLES = [` 的下一行加：

```python
    "assistant_keys",
```

- [ ] **Step 6: 运行确认通过，并确认模型与迁移一致**

Run: `uv run pytest tests/integration/test_assistant_keys_schema.py tests/integration/test_migrations.py -v`
Expected: 全部通过。

Run: `uv run alembic check`
Expected: `No new upgrade operations detected.`。若报外键名不一致，把迁移里的 `sa.ForeignKey("users.id")` 换成表级 `sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("<报告里的名字>"))`，再跑一次。

- [ ] **Step 7: 提交**

```bash
git add migrations/versions/0088_assistant_keys.py coreman/core/db/models tests/conftest.py tests/integration/test_assistant_keys_schema.py
git commit -m "个人助理：新表 assistant_keys"
```

---

### Task 2: 数据密钥的生成、取用与销毁

**Files:**
- Create: `coreman/core/assistants/keys.py`
- Test: `tests/integration/test_assistant_keys.py`

**Interfaces:**
- Consumes: Task 1 的 `AssistantKey`；`coreman.core.crypto.Cipher`。
- Produces:
  - `KEY_TTL_SECONDS: float = 60.0`
  - `data_key_id(generation: int) -> str`（`"d<generation>"`，内容密文里的密钥标识）
  - `class KeyUnavailable(Exception)`，子类 `KeyMissing`、`KeyDestroyed`
  - `wrap_aad(owner_id: uuid.UUID) -> str`
  - `async ensure_key(session: AsyncSession, cipher: Cipher, owner_id: uuid.UUID, *, reissue: bool = True) -> bool`（返回是否签发了新密钥：没有行时生成第 1 代；行已销毁且 `reissue` 为真时签发下一代；有可用密钥时不动）
  - `data_cipher` 返回的 `Cipher.key_id` 是 `data_key_id(当前代数)`
  - `async data_cipher(session: AsyncSession, cipher: Cipher, owner_id: uuid.UUID) -> Cipher`
  - `cached(owner_id: uuid.UUID) -> Cipher | None`
  - `async destroy_key(session: AsyncSession, owner_id: uuid.UUID) -> None`
  - `forget(owner_id: uuid.UUID | None = None) -> None`（清本进程缓存）

- [ ] **Step 1: 写失败测试**

```python
"""数据密钥：生成一次、主密钥包裹、缓存 60 秒、销毁后不可用、主密钥轮换后仍可用。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys
from coreman.core.crypto import Cipher, DecryptError, key_id_of
from coreman.core.db.models import AssistantKey, User

OLD = b"\x05" * 32
NEW = b"\x06" * 32
MASTER = Cipher(OLD)


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    keys.forget()
    yield
    keys.forget()


async def _owner(session: AsyncSession) -> uuid.UUID:
    user = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name="张三")
    session.add(user)
    await session.commit()
    return user.id


async def test_ensure_key_creates_once_and_wraps_with_master(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    assert await keys.ensure_key(db_session, MASTER, owner) is True
    assert await keys.ensure_key(db_session, MASTER, owner) is False
    await db_session.commit()
    row = (await db_session.execute(select(AssistantKey))).scalar_one()
    assert row.dek_enc is not None and key_id_of(row.dek_enc) == "k1"
    data = await keys.data_cipher(db_session, MASTER, owner)
    token = data.encrypt("机密", "t.c:x")
    assert token.startswith("enc:v2:d1:") and data.decrypt(token, "t.c:x") == "机密"
    # 第二次命中缓存，是同一个对象。
    assert await keys.data_cipher(db_session, MASTER, owner) is data
    assert keys.cached(owner) is data


async def test_missing_key_is_reported(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    with pytest.raises(keys.KeyMissing):
        await keys.data_cipher(db_session, MASTER, owner)


async def test_wrapped_key_is_bound_to_its_owner(db_session: AsyncSession) -> None:
    a, b = await _owner(db_session), await _owner(db_session)
    await keys.ensure_key(db_session, MASTER, a)
    await keys.ensure_key(db_session, MASTER, b)
    await db_session.execute(
        text(
            "UPDATE assistant_keys SET dek_enc ="
            " (SELECT dek_enc FROM assistant_keys WHERE user_id = :a) WHERE user_id = :b"
        ),
        {"a": a, "b": b},
    )
    await db_session.commit()
    with pytest.raises(DecryptError):
        await keys.data_cipher(db_session, MASTER, b)


async def test_destroy_makes_the_key_unavailable_for_good(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    await keys.ensure_key(db_session, MASTER, owner)
    await db_session.commit()
    await keys.data_cipher(db_session, MASTER, owner)
    await keys.destroy_key(db_session, owner)
    await db_session.commit()
    row = (await db_session.execute(select(AssistantKey))).scalar_one()
    assert row.dek_enc is None and row.destroyed_at is not None
    with pytest.raises(keys.KeyDestroyed):
        await keys.data_cipher(db_session, MASTER, owner)
    # 写入路径的自动生成（reissue=False）不会把销毁撤销掉。
    assert await keys.ensure_key(db_session, MASTER, owner, reissue=False) is False
    with pytest.raises(keys.KeyDestroyed):
        await keys.data_cipher(db_session, MASTER, owner)


async def test_reissue_after_destroy_starts_a_new_generation(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    await keys.ensure_key(db_session, MASTER, owner)
    await db_session.commit()
    first = await keys.data_cipher(db_session, MASTER, owner)
    old_token = first.encrypt("旧内容", "t.c:x")
    assert first.key_id == "d1" and keys.data_key_id(1) == "d1"
    await keys.destroy_key(db_session, owner)
    await db_session.commit()
    # 显式签发（开通流程）：代数加一，换一把新钥匙。
    assert await keys.ensure_key(db_session, MASTER, owner) is True
    await db_session.commit()
    row = (
        await db_session.execute(
            select(AssistantKey).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.generation == 2 and row.dek_enc is not None and row.destroyed_at is None
    second = await keys.data_cipher(db_session, MASTER, owner)
    assert second.key_id == "d2"
    new_token = second.encrypt("新内容", "t.c:x")
    assert new_token.startswith("enc:v2:d2:") and second.decrypt(new_token, "t.c:x") == "新内容"
    # 新钥匙不认旧一代的密文：密钥标识对不上（按行读取时据此判为已锁定，见 Task 4）。
    with pytest.raises(DecryptError):
        second.decrypt(old_token, "t.c:x")
    # 已有可用密钥时再调不会又换一代。
    assert await keys.ensure_key(db_session, MASTER, owner) is False


async def test_destroy_elsewhere_takes_effect_within_one_ttl(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await _owner(db_session)
    await keys.ensure_key(db_session, MASTER, owner)
    await db_session.commit()
    start = keys._now()
    data = await keys.data_cipher(db_session, MASTER, owner)
    # 别的进程销毁：本进程的缓存不知道。
    await db_session.execute(
        text("UPDATE assistant_keys SET dek_enc = NULL, destroyed_at = now() WHERE user_id = :u"),
        {"u": owner},
    )
    await db_session.commit()
    assert await keys.data_cipher(db_session, MASTER, owner) is data
    monkeypatch.setattr(keys, "_now", lambda: start + keys.KEY_TTL_SECONDS + 1)
    with pytest.raises(keys.KeyDestroyed):
        await keys.data_cipher(db_session, MASTER, owner)


async def test_uncommitted_key_is_not_cached(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    await keys.ensure_key(db_session, MASTER, owner)
    await keys.data_cipher(db_session, MASTER, owner)
    await db_session.rollback()
    # 事务回滚后这把钥匙不存在；缓存里不能留着它，否则会用一把库里没有的钥匙加密。
    assert keys.cached(owner) is None
    with pytest.raises(keys.KeyMissing):
        await keys.data_cipher(db_session, MASTER, owner)


async def test_master_key_rotation_keeps_data_readable(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    await keys.ensure_key(db_session, MASTER, owner)
    await db_session.commit()
    token = (await keys.data_cipher(db_session, MASTER, owner)).encrypt("机密", "t.c:x")
    keys.forget()
    rotated = Cipher(NEW, key_id="k2", previous={"k1": OLD})
    assert (await keys.data_cipher(db_session, rotated, owner)).decrypt(token, "t.c:x") == "机密"
    keys.forget()
    with pytest.raises(DecryptError):
        await keys.data_cipher(db_session, Cipher(NEW, key_id="k2"), owner)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_keys.py -v`
Expected: FAIL，`ImportError: cannot import name 'keys'`。

- [ ] **Step 3: 实现**

`coreman/core/assistants/keys.py`：

```python
"""个人助理的数据密钥：每人一把 256 位密钥，用平台主密钥包裹后存 `assistant_keys`。

数据密钥只在平台进程内解开，不进任务载荷、环境变量、日志或异常消息。解开后的密钥在进程内
缓存 `KEY_TTL_SECONDS`：别的进程销毁之后，本进程最迟这么久就读不出来。
"""

from __future__ import annotations

import base64
import os
import time
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantKey

KEY_TTL_SECONDS = 60.0
# 本会话里刚插入、可能还没提交的密钥：不进缓存，事务回滚时才不会留下一把库里没有的钥匙。
_FRESH = "assistant_keys_fresh"
_CACHE: dict[uuid.UUID, tuple[Cipher, float]] = {}


class KeyUnavailable(Exception):
    """所有者的数据密钥取不到：对应内容不可读。"""


class KeyMissing(KeyUnavailable):
    """还没有生成过数据密钥。"""


class KeyDestroyed(KeyUnavailable):
    """数据密钥已销毁。"""


def _now() -> float:
    return time.monotonic()


def wrap_aad(owner_id: uuid.UUID) -> str:
    return f"assistant_keys.dek_enc:{owner_id}"


def data_key_id(generation: int) -> str:
    """内容密文里的密钥标识：记下是第几代数据密钥加密的。与主密钥的 kid 无关。"""
    return f"d{generation}"


def forget(owner_id: uuid.UUID | None = None) -> None:
    """清本进程的缓存；不传就全清。"""
    if owner_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(owner_id, None)


def cached(owner_id: uuid.UUID) -> Cipher | None:
    hit = _CACHE.get(owner_id)
    return hit[0] if hit is not None and hit[1] > _now() else None


async def ensure_key(
    session: AsyncSession, cipher: Cipher, owner_id: uuid.UUID, *, reissue: bool = True
) -> bool:
    """保证所有者有一把可用的数据密钥，返回是否签发了新的。调用方负责提交。

    没有密钥行：生成第 1 代。密钥已销毁：`reissue` 为真时签发下一代（代数加一、清掉销毁时间），
    旧一代加密的内容从此按已锁定处理；为假时不动。已有可用密钥：不动。

    Args:
        reissue: 开通流程用默认的 True；写入路径首次用到时的自动生成必须传 False，
            否则停用之后一次迟到的写入就会把销毁撤销掉
    """
    token = cipher.encrypt(
        base64.b64encode(os.urandom(32)).decode("ascii"), wrap_aad(owner_id)
    )
    stmt = insert(AssistantKey).values(user_id=owner_id, dek_enc=token)
    if reissue:
        stmt = stmt.on_conflict_do_update(
            index_elements=[AssistantKey.user_id],
            set_={
                "dek_enc": stmt.excluded.dek_enc,
                "generation": AssistantKey.generation + 1,
                "destroyed_at": None,
            },
            where=AssistantKey.destroyed_at.is_not(None),
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=[AssistantKey.user_id])
    row = (await session.execute(stmt.returning(AssistantKey.user_id))).first()
    forget(owner_id)
    if row is None:
        return False
    session.info.setdefault(_FRESH, set()).add(owner_id)
    return True


async def data_cipher(session: AsyncSession, cipher: Cipher, owner_id: uuid.UUID) -> Cipher:
    """用所有者的数据密钥构造的 Cipher。

    Raises:
        KeyMissing: 没有密钥行
        KeyDestroyed: 密钥已销毁
        DecryptError: 主密钥解不开包裹（主密钥配错，或密钥行被挪动）
    """
    hit = cached(owner_id)
    if hit is not None:
        return hit
    _CACHE.pop(owner_id, None)
    row = (
        await session.execute(
            select(
                AssistantKey.dek_enc, AssistantKey.destroyed_at, AssistantKey.generation
            ).where(AssistantKey.user_id == owner_id)
        )
    ).first()
    if row is None:
        raise KeyMissing("所有者没有数据密钥")
    if row[1] is not None or not row[0]:
        raise KeyDestroyed("所有者的数据密钥已销毁")
    data = Cipher(
        base64.b64decode(cipher.decrypt(row[0], wrap_aad(owner_id))),
        key_id=data_key_id(int(row[2])),
    )
    if owner_id not in session.info.get(_FRESH, ()):
        _CACHE[owner_id] = (data, _now() + KEY_TTL_SECONDS)
    return data


async def destroy_key(session: AsyncSession, owner_id: uuid.UUID) -> None:
    """清空包裹后的密钥并记下时间；之后 `data_cipher` 抛 `KeyDestroyed`。调用方负责提交。

    本进程立即生效；其他进程最迟 `KEY_TTL_SECONDS` 后生效。
    """
    await session.execute(
        update(AssistantKey)
        .where(AssistantKey.user_id == owner_id, AssistantKey.destroyed_at.is_(None))
        .values(dek_enc=None, destroyed_at=func.now())
    )
    forget(owner_id)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_keys.py -v`
Expected: 8 passed。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants/keys.py tests/integration/test_assistant_keys.py
git commit -m "个人助理：数据密钥的生成、取用与销毁"
```

---

### Task 3: 封存形式的编解码（纯函数）

**Files:**
- Create: `coreman/core/assistants/sealed.py`（本任务只写编解码部分）
- Test: `tests/unit/test_assistant_sealed_codec.py`

**Interfaces:**
- Consumes: `coreman.core.crypto.Cipher`、`is_encrypted`。
- Produces（都在 `coreman.core.assistants.sealed`）:
  - `SEALED = "_sealed"`、`LOCKED_CODE = "content_locked"`
  - `class ContentLocked(Exception)`
  - `JsonSpec(plain: frozenset[str] = frozenset(), underscore: bool = False, mirrors: tuple[tuple[str, str], ...] = ())`、`Whole(blank: Any = None)`、`Text(blank: str | None = None)`
  - `TASK_PLAIN: frozenset[str]`、`COLUMNS: dict[type, dict[str, JsonSpec | Whole | Text]]`
  - `aad(table: str, column: str, bot_id: uuid.UUID) -> str`
  - `is_sealed(value: object) -> bool`
  - `seal_dict(cipher, aad_, value: dict, spec: JsonSpec) -> dict`、`open_dict(cipher, aad_, value: dict, spec: JsonSpec) -> dict`
  - `seal_whole(cipher, aad_, value) -> Any`、`open_whole(cipher, aad_, value) -> Any`
  - `seal_text(cipher, aad_, value: str | None) -> str | None`、`open_text(cipher, aad_, value) -> Any`

- [ ] **Step 1: 写失败测试**

```python
"""封存形式：明文键留在外面，其余进密文；封装顶层的键优先；AAD 不对解不开。"""

from __future__ import annotations

import json
import uuid

import pytest

from coreman.core.assistants import sealed
from coreman.core.crypto import Cipher, DecryptError
from coreman.core.db.models import (
    ChatLog,
    FeishuSentMessage,
    InboundEvent,
    OutboxItem,
    Task,
    TaskStream,
)

CIPHER = Cipher(b"\x09" * 32, key_id="d1")
AAD = "tasks.payload:bot-a"
SECRET = "机密-片段-7f3a"


def _text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def test_every_content_column_is_registered() -> None:
    assert set(sealed.COLUMNS) == {
        InboundEvent,
        Task,
        TaskStream,
        OutboxItem,
        ChatLog,
        FeishuSentMessage,
    }
    assert set(sealed.COLUMNS[Task]) == {"payload", "result", "error_message"}
    assert set(sealed.COLUMNS[FeishuSentMessage]) == {"text"}
    assert set(sealed.COLUMNS[TaskStream]) == {
        "thinking_md",
        "pending_text",
        "final_text",
        "pending_card",
        "plan",
    }
    assert set(sealed.COLUMNS[ChatLog]) == {
        "message_content",
        "quoted_content",
        "response_content",
        "file_info",
        "error_message",
    }
    assert {"serialize_session", "stale_hold", "platform_user_id"} <= sealed.TASK_PLAIN


def test_aad_names_table_column_and_bot() -> None:
    bot = uuid.UUID(int=7)
    assert sealed.aad("tasks", "payload", bot) == f"tasks.payload:{bot}"


def test_dict_keeps_plain_keys_and_hides_the_rest() -> None:
    spec = sealed.JsonSpec(plain=frozenset({"stale_hold", "platform_user_id"}))
    value = {"stale_hold": True, "platform_user_id": "ou_1", "message": {"text": SECRET}}
    out = sealed.seal_dict(CIPHER, AAD, value, spec)
    assert set(out) == {"_sealed", "stale_hold", "platform_user_id"}
    assert out["stale_hold"] is True and SECRET not in _text(out)
    assert sealed.is_sealed(out) and not sealed.is_sealed(value)
    assert sealed.open_dict(CIPHER, AAD, out, spec) == value
    # 没封存过的原样返回。
    assert sealed.open_dict(CIPHER, AAD, value, spec) == value


def test_envelope_top_level_keys_win_on_open() -> None:
    spec = sealed.JsonSpec(plain=frozenset({"stale_hold", "doc_links"}))
    out = sealed.seal_dict(CIPHER, AAD, {"stale_hold": True, "message": SECRET}, spec)
    # 不解密就能加键、删键（裸 SQL 的 payload || …、放行排队消息）。
    edited = {k: v for k, v in out.items() if k != "stale_hold"} | {"doc_links": ["doc_x"]}
    assert sealed.open_dict(CIPHER, AAD, edited, spec) == {
        "message": SECRET,
        "doc_links": ["doc_x"],
    }


def test_underscore_keys_stay_plain_and_card_task_id_is_mirrored() -> None:
    spec = sealed.JsonSpec(underscore=True, mirrors=(("card", "task_id"),))
    value = {
        "card": {"task_id": "choice:1:q0", "body": SECRET},
        "markdown": SECRET,
        "_feishu_message_id": "om_1",
    }
    out = sealed.seal_dict(CIPHER, AAD, value, spec)
    assert out["_feishu_message_id"] == "om_1" and out["card"] == {"task_id": "choice:1:q0"}
    assert SECRET not in _text(out)
    # 还原以密文里的整张卡为准，镜像不覆盖它。
    assert sealed.open_dict(CIPHER, AAD, out, spec) == value


def test_resealing_an_envelope_absorbs_stray_keys() -> None:
    spec = sealed.JsonSpec(plain=frozenset({"stale_hold"}))
    out = sealed.seal_dict(CIPHER, AAD, {"message": "a"}, spec)
    again = sealed.seal_dict(CIPHER, AAD, {**out, "note": SECRET, "stale_hold": True}, spec)
    assert set(again) == {"_sealed", "stale_hold"} and SECRET not in _text(again)
    assert sealed.open_dict(CIPHER, AAD, again, spec) == {
        "message": "a",
        "note": SECRET,
        "stale_hold": True,
    }


def test_ciphertext_cannot_move_to_another_bot_column_or_table() -> None:
    spec = sealed.JsonSpec()
    out = sealed.seal_dict(CIPHER, AAD, {"message": SECRET}, spec)
    for other in ("tasks.payload:bot-b", "tasks.result:bot-a", "outbox.payload:bot-a"):
        with pytest.raises(DecryptError):
            sealed.open_dict(CIPHER, other, out, spec)
    with pytest.raises(DecryptError):
        sealed.open_dict(Cipher(b"\x0a" * 32, key_id="d1"), AAD, out, spec)


def test_another_generation_is_locked_not_an_error() -> None:
    # 重新签发之后：旧一代（d1）的密文遇到新一代（d2）的密钥，按已锁定处理，不算篡改。
    reissued = Cipher(b"\x0b" * 32, key_id="d2")
    old_dict = sealed.seal_dict(CIPHER, AAD, {"message": SECRET}, sealed.JsonSpec())
    old_whole = sealed.seal_whole(CIPHER, AAD, [SECRET])
    old_text = sealed.seal_text(CIPHER, AAD, SECRET)
    with pytest.raises(sealed.ContentLocked):
        sealed.open_dict(reissued, AAD, old_dict, sealed.JsonSpec())
    with pytest.raises(sealed.ContentLocked):
        sealed.open_whole(reissued, AAD, old_whole)
    with pytest.raises(sealed.ContentLocked):
        sealed.open_text(reissued, AAD, old_text)
    # 新一代自己写的照常读得出。
    fresh = sealed.seal_text(reissued, AAD, SECRET)
    assert fresh is not None and fresh.startswith("enc:v2:d2:")
    assert sealed.open_text(reissued, AAD, fresh) == SECRET


def test_tombstone_is_reported_as_locked() -> None:
    with pytest.raises(sealed.ContentLocked):
        sealed.open_dict(CIPHER, AAD, {"_sealed": "", "stale_hold": True}, sealed.JsonSpec())
    with pytest.raises(sealed.ContentLocked):
        sealed.open_whole(CIPHER, AAD, {"_sealed": ""})


def test_whole_value_and_text() -> None:
    plan = [{"text": SECRET, "status": "pending"}]
    stored = sealed.seal_whole(CIPHER, AAD, plan)
    assert set(stored) == {"_sealed"} and SECRET not in _text(stored)
    assert sealed.open_whole(CIPHER, AAD, stored) == plan
    assert sealed.open_whole(CIPHER, AAD, plan) == plan
    token = sealed.seal_text(CIPHER, AAD, SECRET)
    assert token is not None and token.startswith("enc:v2:d1:") and SECRET not in token
    assert sealed.open_text(CIPHER, AAD, token) == SECRET
    assert sealed.open_text(CIPHER, AAD, "明文") == "明文"
    assert sealed.seal_text(CIPHER, AAD, "") == "" and sealed.seal_text(CIPHER, AAD, None) is None
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_sealed_codec.py -v`
Expected: FAIL，`ImportError: cannot import name 'sealed'`。

- [ ] **Step 3: 实现**

`coreman/core/assistants/sealed.py`：

```python
"""个人助理内容列的封存与还原。

对 `bots.kind='assistant'` 的记录，登记在 `COLUMNS` 里的内容列写库前用所有者的数据密钥加密、读出后解密；
普通机器人的行原样读写，调用方拿到的始终是明文。

封存形式：
- 字典列：`{"_sealed": <密文>, <明文键>…}`，密文里是去掉明文键之后的其余部分。还原时与封装
  顶层的其它键合并，顶层优先——所以不解密也能对封装增删明文键。
- 整值列：`{"_sealed": <密文>}`。
- 文本列：密文串本身。
- `{"_sealed": ""}` 是墓碑：密钥销毁之后写入，内容已丢弃。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from coreman.core.crypto import Cipher, is_encrypted, key_id_of
from coreman.core.db.models import (
    ChatLog,
    FeishuSentMessage,
    InboundEvent,
    OutboxItem,
    Task,
    TaskStream,
)

SEALED = "_sealed"
LOCKED_CODE = "content_locked"


class ContentLocked(Exception):
    """内容读不出来：数据密钥已销毁或不存在、内容由已作废的旧一代密钥加密，或是销毁之后写下的墓碑。"""


@dataclass(frozen=True)
class JsonSpec:
    """字典列：哪些键留在密文外面。

    Attributes:
        plain: 留明文的顶层键（被 SQL 直接读取的控制字段）
        underscore: 下划线开头的键是否留明文（出站的内部记账键）
        mirrors: `(根键, 子键)`：把 `value[根键][子键]` 在封装顶层另存一份供 SQL 过滤，
            还原时以密文里的为准
    """

    plain: frozenset[str] = frozenset()
    underscore: bool = False
    mirrors: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Whole:
    """整值封存的 JSONB 列；`blank` 是读不出来时给调用方的值。"""

    blank: Any = None


@dataclass(frozen=True)
class Text:
    """文本列；`blank` 是读不出来、或密钥销毁后写入时的值（非空列用空串）。"""

    blank: str | None = None


Kind = JsonSpec | Whole | Text

# tasks.payload 里被 SQL 直接读取的键：认领语句、排队判断、媒体补偿、技能安装、文档链接。
TASK_PLAIN = frozenset(
    {
        "serialize_session",
        "stale_hold",
        "platform_user_id",
        "origin",
        "busy_offer",
        "escalation_id",
        "message_id",
        "skill_id",
        "doc_links",
    }
)
COLUMNS: dict[type, dict[str, Kind]] = {
    InboundEvent: {"payload": JsonSpec()},
    Task: {
        "payload": JsonSpec(plain=TASK_PLAIN),
        "result": Whole(),
        "error_message": Text(),
    },
    TaskStream: {
        "thinking_md": Text(""),
        "pending_text": Text(""),
        "final_text": Text(),
        "pending_card": Whole(),
        "plan": Whole([]),
    },
    OutboxItem: {"payload": JsonSpec(underscore=True, mirrors=(("card", "task_id"),))},
    ChatLog: {
        "message_content": Text(),
        "quoted_content": Text(),
        "response_content": Text(),
        "file_info": Whole(),
        "error_message": Text(),
    },
    # 机器人发出的每条飞书消息的正文存档；text 非空，读不出来时给空串。
    FeishuSentMessage: {"text": Text("")},
}


def aad(table: str, column: str, bot_id: uuid.UUID) -> str:
    """密文绑定到表、列和助理：挪到别处就解不开。"""
    return f"{table}.{column}:{bot_id}"


def is_sealed(value: object) -> bool:
    return isinstance(value, dict) and isinstance(value.get(SEALED), str)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decrypt(cipher: Cipher, token: str, aad_: str) -> str:
    """解一段内容密文。

    密文里的密钥标识（`d<代数>`）与当前数据密钥不是同一代：密钥销毁后重新签发过，这段内容
    永久读不出来，按已锁定处理。同一代却解不开才是密文被改或被挪动，由 `Cipher` 抛
    `DecryptError`。

    Raises:
        ContentLocked: 墓碑，或由另一代数据密钥加密
        DecryptError: 同一代密钥解不开
    """
    if not token:
        raise ContentLocked("内容在密钥销毁之后写入，已丢弃")
    if key_id_of(token) != cipher.key_id:
        raise ContentLocked("内容由已作废的旧一代数据密钥加密")
    return cipher.decrypt(token, aad_)


def _keep(spec: JsonSpec, value: dict[str, Any]) -> dict[str, Any]:
    """留在密文外面的那部分。"""
    return {
        k: v
        for k, v in value.items()
        if k != SEALED and (k in spec.plain or (spec.underscore and k.startswith("_")))
    }


def seal_dict(cipher: Cipher, aad_: str, value: dict[str, Any], spec: JsonSpec) -> dict[str, Any]:
    if is_sealed(value):
        # 已经是封装（可能被追加了别的键）：先还原，杂散的键一起收进密文。
        value = open_dict(cipher, aad_, value, spec)
    keep = _keep(spec, value)
    rest = {k: v for k, v in value.items() if k not in keep and k != SEALED}
    out: dict[str, Any] = {SEALED: cipher.encrypt(_dumps(rest), aad_), **keep}
    for root, child in spec.mirrors:
        inner = rest.get(root)
        if isinstance(inner, dict) and child in inner:
            out[root] = {child: inner[child]}
    return out


def open_dict(cipher: Cipher, aad_: str, value: dict[str, Any], spec: JsonSpec) -> dict[str, Any]:
    if not is_sealed(value):
        return value
    rest: dict[str, Any] = json.loads(_decrypt(cipher, value[SEALED], aad_))
    roots = {root for root, _ in spec.mirrors}
    extra = {k: v for k, v in value.items() if k != SEALED and not (k in roots and k in rest)}
    return {**rest, **extra}


def seal_whole(cipher: Cipher, aad_: str, value: Any) -> Any:
    if value is None or is_sealed(value):
        return value
    return {SEALED: cipher.encrypt(_dumps(value), aad_)}


def open_whole(cipher: Cipher, aad_: str, value: Any) -> Any:
    if not is_sealed(value):
        return value
    return json.loads(_decrypt(cipher, value[SEALED], aad_))


def seal_text(cipher: Cipher, aad_: str, value: str | None) -> str | None:
    return cipher.encrypt(value, aad_) if value else value


def open_text(cipher: Cipher, aad_: str, value: Any) -> Any:
    if not isinstance(value, str) or not is_encrypted(value):
        return value
    return _decrypt(cipher, value, aad_)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_sealed_codec.py -v`
Expected: 10 passed。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants/sealed.py tests/unit/test_assistant_sealed_codec.py
git commit -m "个人助理：内容列封存形式的编解码"
```

---

### Task 4: 按行封存与还原、会话类、flush 钩子

**Files:**
- Modify: `coreman/core/assistants/sealed.py`（在 Task 3 的内容之后追加）
- Create: `coreman/core/assistants/session.py`
- Modify: `coreman/core/db/session.py:1-8,43-44`
- Create: `tests/integration/assistant_helpers.py`
- Modify: `tests/integration/conftest.py`（加夹具 `assistant_master`）
- Test: `tests/integration/test_assistant_sealed_rows.py`
- Test: `tests/integration/test_orm_flush_reseal_probe.py`（Step 0，先于其它步骤）
- Test: `tests/integration/test_assistant_orm_flush.py`（钩子按归属封存：已锁定的行、读出时内容为空的行、`session.add` 新建的行、冷缓存、普通机器人不变且缓存后不查库）
- Modify: `coreman/core/assistants/keys.py`（加同步取用 `data_cipher_sync`，与 `data_cipher` 共用解包逻辑 `_unwrap`）

**Interfaces:**
- Consumes: Task 2 的 `keys.data_cipher` / `ensure_key` / `cached` / `KeyMissing` / `KeyDestroyed` / `KEY_TTL_SECONDS`；Task 3 的编解码。
- Produces（`coreman.core.assistants.sealed`）:
  - `use_master(cipher: Cipher | None) -> None`、`master() -> Cipher`、`forget() -> None`
  - `async owner_of(session: AsyncSession, bot_id: uuid.UUID) -> uuid.UUID | None`（助理的所有者；普通机器人为 `None`）
  - `async seal_values(session: AsyncSession, model: type, bot_id: uuid.UUID | None, values: dict[str, Any]) -> dict[str, Any]`（返回新字典；普通机器人原样返回；密钥已销毁时内容丢弃、不抛错）
  - `async open_row(session: AsyncSession, row: Any, *, viewer: uuid.UUID | None = None) -> bool`（返回内容是否可读；`viewer` 给了就只对所有者本人还原）
  - `async open_rows(session: AsyncSession, rows: Iterable[Any], *, viewer: uuid.UUID | None = None) -> None`
  - `async open_value(session: AsyncSession, model: type, column: str, bot_id: uuid.UUID, value: Any) -> Any`（只取了一列时用；读不出来返回该列的空值）
  - `is_locked(row: Any) -> bool`、`is_opened(row: Any) -> bool`
- Produces（flush 钩子）: `before_flush` 遍历 `session.new` 与 `session.dirty` 里属于 `COLUMNS` 的行。没有内容列要写（新建行看有没有非空值，已有行看属性历史）就跳过、不查库；否则按 `bot_id` 取归属（`_owner_sync`：进程内缓存 → 同一次 flush 里新建的 `Bot` → 用当前事务的同步连接查 `bots`，结果按 TTL 缓存，普通机器人缓存为「不加密」）。助理的行用 `keys.data_cipher_sync(session, master(), owner)` 取数据密钥（缓存 → 同步连接读 `assistant_keys`；只取用，不签发、不重新签发）后封存；密钥取不到（没有、已销毁）或原值是旧一代密文时内容丢弃：JSONB 写墓碑、文本写空，记 `assistant_content_dropped`，内存里同样置空并标为已锁定。`after_flush_postexec` 把内存里的值换回明文（丢弃的换成空值）且不标脏。
- Produces（`coreman.core.assistants.keys`）: `data_cipher_sync(session: Session, cipher: Cipher, owner_id: uuid.UUID) -> Cipher`；另有 `after_commit` / `after_rollback` 监听清掉会话上的「刚插入」标记，提交后的密钥才能进缓存。
- Produces（`coreman.core.assistants.session`）: `class SealedSession(AsyncSession)`；`make_session_factory` 产出的会话都是它。
- Produces（测试）: `tests.integration.assistant_helpers.seed_assistant(session) -> tuple[Bot, User]`、`seed_standard(session) -> Bot`、`raw(session, sql, **params) -> Any`、`MASTER_KEY: bytes`、`SECRET: str`；夹具 `assistant_master`。

- [ ] **Step 0: 先在真实 PostgreSQL（asyncpg）上跑通 flush 钩子的最小用例，再继续**

本任务的做法依赖三件事：`set_committed_value` 换进去的明文不把行标脏；`before_flush` 里把属性换成要落库的值，`UPDATE` 写的就是它；`after_flush_postexec` 里再换回明文，内存里仍是明文且不脏。写计划时只在同步的 SQLite 会话上验证过。下面这个用例不依赖 `sealed.py`，只用 ORM 事件，在 `AsyncSession` + asyncpg + JSONB 上把这三件事各验一遍（含由 Core 语句触发的自动 flush）。通过后保留为回归用例。

`tests/integration/test_orm_flush_reseal_probe.py`：

```python
"""flush 钩子做法的最小验证（AsyncSession + asyncpg + JSONB）。

`coreman/core/assistants/sealed.py` 用同一套做法：读出后把明文作为「已提交的值」放进属性，
落库前换成密文，落库后换回明文。这里用一个不加密的替身变换验证 ORM 的行为本身。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import event, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from coreman.core.db.models import Bot, Task, User
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import raw

PROBE = "flush_probe"


def _wrap(value: dict[str, Any]) -> dict[str, Any]:
    """替身变换：库里存的形式。"""
    return {"wrapped": json.dumps(value, sort_keys=True)}


@pytest.fixture
def swap_hooks() -> Iterator[None]:
    swapped: list[tuple[Any, dict[str, Any]]] = []

    def before(session: Session, flush_context: Any, instances: Any) -> None:
        for row in session.dirty:
            if not isinstance(row, Task):
                continue
            state = sa_inspect(row)
            if PROBE in state.info and state.attrs.payload.history.has_changes():
                plain = row.payload
                row.payload = _wrap(plain)
                swapped.append((row, plain))

    def after(session: Session, flush_context: Any) -> None:
        while swapped:
            row, plain = swapped.pop()
            set_committed_value(row, "payload", plain)

    event.listen(Session, "before_flush", before)
    event.listen(Session, "after_flush_postexec", after)
    yield
    event.remove(Session, "before_flush", before)
    event.remove(Session, "after_flush_postexec", after)


async def _stored(session: AsyncSession, task_id: int) -> dict[str, Any]:
    text_ = await raw(session, "SELECT payload::text FROM tasks WHERE id = :id", id=task_id)
    return dict(json.loads(text_))


@pytest.mark.usefixtures("swap_hooks")
async def test_swap_on_flush_and_restore_after(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    creator = User(login_name="probe", display_name="李四")
    db_session.add(creator)
    await db_session.flush()
    bot = Bot(
        bot_key="demo-bot",
        platform="feishu",
        name="demo-bot",
        created_by=creator.id,
        model="m",
        working_dir="/d",
        credentials_enc="",
    )
    db_session.add(bot)
    await db_session.flush()
    task_id = int(
        (
            await db_session.execute(
                insert(Task)
                .values(bot_id=bot.id, kind="chat", payload=_wrap({"a": 1}))
                .returning(Task.id)
            )
        ).scalar_one()
    )
    await db_session.commit()

    # 1. 读出后把明文作为已提交的值放进去：不标脏，别的列的更新不会把它写回库。
    row = await db_session.get(Task, task_id)
    assert row is not None
    set_committed_value(row, "payload", {"a": 1})
    sa_inspect(row).info[PROBE] = True
    assert row not in db_session.dirty
    row.priority = 3
    await db_session.commit()
    assert await _stored(db_session, task_id) == _wrap({"a": 1})

    # 2. 改了内容：落库的是换过的形式，内存里仍是明文，且不再是脏的。
    row.payload = {**row.payload, "b": 2}
    await db_session.flush()
    assert row.payload == {"a": 1, "b": 2} and row not in db_session.dirty
    await db_session.commit()
    assert await _stored(db_session, task_id) == _wrap({"a": 1, "b": 2})
    assert row.payload == {"a": 1, "b": 2}

    # 3. 由 Core 语句触发的自动 flush 走同一条路。
    row.payload = {**row.payload, "c": 3}
    await db_session.execute(update(Task).where(Task.id == task_id).values(priority=4))
    assert row.payload == {"a": 1, "b": 2, "c": 3} and row not in db_session.dirty
    await db_session.commit()
    assert await _stored(db_session, task_id) == _wrap({"a": 1, "b": 2, "c": 3})

    # 4. 别的会话读到的是库里的形式。
    async with make_session_factory(db_engine)() as other:
        got = await other.get(Task, task_id)
        assert got is not None and got.payload == _wrap({"a": 1, "b": 2, "c": 3})
```

这个用例要用到 `tests/integration/assistant_helpers.py` 里的 `raw`：先按 Step 1 建好该文件（只建文件，不做 Step 1 里对 `conftest.py` 的改动也可以），再运行：

Run: `uv run pytest tests/integration/test_orm_flush_reseal_probe.py -v`
Expected: 1 passed。

**通过**：继续 Step 1。

**不通过（钩子在 asyncpg 下不可行）时的退路**：不注册 `before_flush` / `after_flush_postexec`，删掉 Step 4 里的 `_RESTORE`、`_reseal`、`_unseal_after_flush` 与本用例；「经 ORM 改已读出的行」改为在每个改动点显式写库。在 `sealed.py` 加：

```python
async def store(session: AsyncSession, row: Any, **changes: Any) -> None:
    """改一行的内容列并立即写库：库里是封存后的值，内存里是明文（不标脏）。

    代替 `row.payload = {...}`：助理记录的内容列不能经 ORM 的脏检查落库，那样写下去的是明文。
    """
    model = type(row)
    mapper = sa_inspect(model)
    identity = sa_inspect(row).identity
    stored = await seal_values(session, model, row.bot_id, changes)
    stmt = sa_update(model).values(**stored)
    for column, value in zip(mapper.primary_key, identity, strict=True):
        stmt = stmt.where(column == value)
    await session.execute(stmt)
    for column, value in changes.items():
        set_committed_value(row, column, value)
```

（`from sqlalchemy import update as sa_update`。）然后把下面这些赋值逐处换成 `await sealed.store(session, row, payload=...)`（或 `result=...`），约 24 处、12 个文件：

| 文件 | 处数 |
|---|---|
| `coreman/core/chat/collaboration_tools.py` | 5 |
| `coreman/runtime/gateway_feishu/transport.py`（发出后记 `_feishu_message_id`） | 5 |
| `coreman/runtime/worker/busy_choice.py` | 3 |
| `coreman/runtime/worker/chat/queued_batch.py` | 2 |
| `coreman/api/routers/systems_catalog.py` | 2 |
| `coreman/core/chat/bot_collaboration.py` | 1 |
| `coreman/core/bus/tasks.py`（`release_hold`） | 1 |
| `coreman/core/bus/outbox.py`（`begin_attempt`） | 1 |
| `coreman/runtime/gateway_feishu/reply_buttons.py` | 1 |
| `coreman/runtime/worker/chat/collaboration.py`（`task.result`） | 1 |
| `coreman/core/notifications.py`（通知项不绑机器人，可不改） | 1 |
| `coreman/runtime/worker/credential_cards.py`（Task 10 新增的那一处） | 1 |

并在 Task 10 的台账测试里加一条：`coreman/` 下除 `sealed.py` 外不得出现对这六个模型内容列的属性赋值（正则 `\.(payload|result|error_message|final_text|pending_text|thinking_md)\s*=[^=]`，按文件列出允许的例外）。Task 4 的 `test_orm_mutation_is_resealed_on_flush` 改为调用 `sealed.store`。其余任务（写入口的 `seal_values`、读取的 `SealedSession`）不受影响。退路的代价是这 24 处改动与「以后新增的赋值靠台账拦」，所以只在钩子确实不可行时采用。

- [ ] **Step 1: 写测试公共件**

`tests/integration/assistant_helpers.py`：

```python
"""加密存储用例的公共件：造助理记录与普通机器人，按原始 SQL 看库里到底存了什么。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.records import new_assistant_bot
from coreman.core.db.models import Bot, PlatformApp, User

MASTER_KEY = b"\x07" * 32
# 断言「库里不含明文」用的片段：不会碰巧出现在 base64 密文里。
SECRET = "机密-片段-7f3a"


async def seed_assistant(session: AsyncSession) -> tuple[Bot, User]:
    owner = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name="张三")
    app = PlatformApp(
        platform="feishu",
        name=f"个人助理-{uuid.uuid4().hex[:6]}",
        capabilities=["assistant"],
        app_id=f"cli_{uuid.uuid4().hex[:10]}",
        secret_enc="x",
    )
    session.add_all([owner, app])
    await session.flush()
    bot = new_assistant_bot(
        owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc=""
    )
    session.add(bot)
    await session.commit()
    return bot, owner


async def seed_standard(session: AsyncSession) -> Bot:
    creator = User(login_name=f"c{uuid.uuid4().hex[:8]}", display_name="李四")
    session.add(creator)
    await session.flush()
    bot = Bot(
        bot_key=f"demo-bot-{uuid.uuid4().hex[:6]}",
        platform="feishu",
        name="demo-bot",
        created_by=creator.id,
        model="m",
        working_dir="/d",
        credentials_enc="",
    )
    session.add(bot)
    await session.commit()
    return bot


async def raw(session: AsyncSession, sql: str, **params: Any) -> Any:
    """原始 SQL 取一个值：绕开会话的自动还原，看到的就是库里存的。"""
    return (await session.execute(text(sql), params)).scalar_one()
```

`tests/integration/conftest.py` 末尾追加（文件头的 `from tests.integration.worker_helpers import MASTER` 之后补两行导入）：

```python
from coreman.core.assistants import keys, sealed
from coreman.core.crypto import Cipher
```

```python
@pytest.fixture
def assistant_master() -> Iterator[None]:
    """加密存储用例的平台主密钥，并清掉进程内的密钥与所有者缓存。"""
    sealed.use_master(Cipher(MASTER))
    keys.forget()
    sealed.forget()
    yield
    sealed.use_master(None)
    keys.forget()
    sealed.forget()
```

- [ ] **Step 2: 写失败测试**

`tests/integration/test_assistant_sealed_rows.py`：

```python
"""按行封存与还原：库里是密文，取回是明文；普通机器人不变；销毁后置空不抛错。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.assistants.session import SealedSession
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantKey, Task
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import (
    MASTER_KEY,
    SECRET,
    raw,
    seed_assistant,
    seed_standard,
)

pytestmark = pytest.mark.usefixtures("assistant_master")
PAYLOAD = {"message": {"text": SECRET}, "platform_user_id": "ou_1", "stale_hold": True}


async def _insert(session: AsyncSession, bot_id, payload: dict) -> int:  # type: ignore[no-untyped-def]
    values = await sealed.seal_values(
        session, Task, bot_id, {"bot_id": bot_id, "kind": "chat", "payload": payload}
    )
    task_id = (await session.execute(insert(Task).values(**values).returning(Task.id))).scalar_one()
    await session.commit()
    return int(task_id)


async def _stored(session: AsyncSession, task_id: int) -> str:
    return str(await raw(session, "SELECT payload::text FROM tasks WHERE id = :id", id=task_id))


def test_factory_builds_sealed_sessions(db_session: AsyncSession) -> None:
    assert isinstance(db_session, SealedSession)


async def test_assistant_row_is_ciphertext_at_rest_and_plain_when_read(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    task_id = await _insert(db_session, bot.id, PAYLOAD)
    stored = json.loads(await _stored(db_session, task_id))
    assert SECRET not in json.dumps(stored, ensure_ascii=False)
    assert stored["platform_user_id"] == "ou_1" and stored["stale_hold"] is True
    assert stored["_sealed"].startswith("enc:v2:d1:")
    # 首次写入时生成了所有者的密钥。
    assert (await db_session.execute(select(AssistantKey.user_id))).scalar_one() == owner.id
    async with make_session_factory(db_engine)() as other:
        got = await other.get(Task, task_id)
        assert got is not None and got.payload == PAYLOAD
        assert sealed.is_opened(got) and not sealed.is_locked(got) and got not in other.dirty
        by_scalar = await other.scalar(
            select(Task).where(Task.id == task_id).execution_options(populate_existing=True)
        )
        assert by_scalar is got and got.payload == PAYLOAD
        await other.refresh(got)
        assert got.payload == PAYLOAD


async def test_standard_bot_row_stays_plain(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    task_id = await _insert(db_session, bot.id, PAYLOAD)
    stored = await _stored(db_session, task_id)
    assert SECRET in stored and "_sealed" not in stored
    got = await db_session.get(Task, task_id, populate_existing=True)
    assert got is not None and got.payload == PAYLOAD and not sealed.is_opened(got)
    assert (await db_session.execute(select(AssistantKey))).first() is None


async def test_orm_mutation_is_resealed_on_flush(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    task_id = await _insert(db_session, bot.id, PAYLOAD)
    task = await db_session.get(Task, task_id, populate_existing=True)
    assert task is not None
    task.payload = {**task.payload, "note": f"{SECRET}-2", "busy_offer": True}
    await db_session.commit()
    # 内存里仍是明文，库里是新的密文，明文键留在外面。
    assert task.payload["note"] == f"{SECRET}-2" and task not in db_session.dirty
    stored = json.loads(await _stored(db_session, task_id))
    assert SECRET not in json.dumps(stored, ensure_ascii=False) and stored["busy_offer"] is True
    async with make_session_factory(db_engine)() as other:
        got = await other.get(Task, task_id)
        assert got is not None
        assert got.payload == {**PAYLOAD, "note": f"{SECRET}-2", "busy_offer": True}


async def test_plain_key_added_by_raw_sql_survives(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    task_id = await _insert(db_session, bot.id, PAYLOAD)
    # 与 worker/chat/feishu_docs.py 里那条语句同形。
    await db_session.execute(
        text("UPDATE tasks SET payload = payload || CAST(:extra AS jsonb) WHERE id = :id"),
        {"extra": json.dumps({"doc_links": ["doc_x"]}), "id": task_id},
    )
    await db_session.commit()
    got = await db_session.get(Task, task_id, populate_existing=True)
    assert got is not None and got.payload == {**PAYLOAD, "doc_links": ["doc_x"]}


async def test_open_value_for_a_single_column(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    task_id = await _insert(db_session, bot.id, PAYLOAD)
    column = (await db_session.execute(select(Task.payload).where(Task.id == task_id))).scalar_one()
    assert "message" not in column
    assert await sealed.open_value(db_session, Task, "payload", bot.id, column) == PAYLOAD


async def test_only_the_owner_may_open_when_a_viewer_is_given(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    other = (await seed_standard(db_session)).created_by
    task_id = await _insert(db_session, bot.id, PAYLOAD)
    row = (
        await db_session.execute(
            select(Task).where(Task.id == task_id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert await sealed.open_row(db_session, row, viewer=other) is False
    assert sealed.is_locked(row) and "message" not in row.payload and "_sealed" not in row.payload
    # 已经还原过的行，换成别人看也要收回去。
    mine = await db_session.get(Task, task_id, populate_existing=True)
    assert mine is not None and mine.payload == PAYLOAD
    assert await sealed.open_row(db_session, mine, viewer=other) is False
    assert "message" not in mine.payload
    again = await db_session.get(Task, task_id, populate_existing=True)
    assert await sealed.open_row(db_session, again, viewer=owner.id) is True
    assert again is not None and again.payload == PAYLOAD


async def test_destroyed_key_blanks_reads_and_drops_writes(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    task_id = await _insert(db_session, bot.id, PAYLOAD)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    got = await db_session.get(Task, task_id, populate_existing=True)
    assert got is not None and sealed.is_locked(got)
    assert got.payload == {"platform_user_id": "ou_1", "stale_hold": True}
    # 库里的密文没有被改写。
    assert "enc:v2:d1:" in await _stored(db_session, task_id)
    # 销毁之后的写入：内容丢弃，行照常落库，留下墓碑。
    late = await _insert(db_session, bot.id, {"message": SECRET, "stale_hold": True})
    stored = json.loads(await _stored(db_session, late))
    assert stored == {"_sealed": "", "stale_hold": True}
    tomb = await db_session.get(Task, late, populate_existing=True)
    assert tomb is not None and sealed.is_locked(tomb) and tomb.payload == {"stale_hold": True}


async def test_master_key_rotation_keeps_rows_readable(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    task_id = await _insert(db_session, bot.id, PAYLOAD)
    before = await _stored(db_session, task_id)
    sealed.use_master(Cipher(b"\x08" * 32, key_id="k2", previous={"k1": MASTER_KEY}))
    keys.forget()
    got = await db_session.get(Task, task_id, populate_existing=True)
    assert got is not None and got.payload == PAYLOAD
    # 轮换不重写内容。
    assert await _stored(db_session, task_id) == before


async def test_reissued_key_locks_old_rows_and_reads_new_ones(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    old_id = await _insert(db_session, bot.id, PAYLOAD)
    assert '"_sealed": "enc:v2:d1:' in await _stored(db_session, old_id)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    # 重新开通：显式签发第 2 代。
    assert await keys.ensure_key(db_session, sealed.master(), owner.id) is True
    await db_session.commit()
    new_id = await _insert(db_session, bot.id, PAYLOAD)
    assert '"_sealed": "enc:v2:d2:' in await _stored(db_session, new_id)
    new = await db_session.get(Task, new_id, populate_existing=True)
    assert new is not None and new.payload == PAYLOAD and not sealed.is_locked(new)
    # 旧一代的行：不抛错，按已锁定处理，库里的旧密文原样留着。
    old = await db_session.get(Task, old_id, populate_existing=True)
    assert old is not None and sealed.is_locked(old)
    assert old.payload == {"platform_user_id": "ou_1", "stale_hold": True}
    assert '"_sealed": "enc:v2:d1:' in await _stored(db_session, old_id)
    column = (await db_session.execute(select(Task.payload).where(Task.id == old_id))).scalar_one()
    assert await sealed.open_value(db_session, Task, "payload", bot.id, column) == {
        "platform_user_id": "ou_1",
        "stale_hold": True,
    }
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_sealed_rows.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'coreman.core.assistants.session'`。

- [ ] **Step 4: 实现按行封存与还原**

`coreman/core/assistants/sealed.py`：把文件头的导入换成下面这组（在 Task 3 的基础上增加），其余内容不动：

```python
from __future__ import annotations

import copy
import json
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from coreman.core.assistants import keys
from coreman.core.config import get_settings
from coreman.core.crypto import Cipher, is_encrypted, key_id_of
from coreman.core.db.models import (
    Bot,
    ChatLog,
    FeishuSentMessage,
    InboundEvent,
    OutboxItem,
    Task,
    TaskStream,
)
from coreman.core.logging import get_logger

log = get_logger(__name__)
```

在文件末尾追加：

```python
# ---- 按行封存与还原 --------------------------------------------------------

# 行上的标记（InstanceState.info）：还原时用的数据密钥、内容读不出来。
_CIPHER = "sealed_cipher"
_LOCKED = "sealed_locked"
# flush 期间被换成密文的属性，flush 之后换回明文。
_RESTORE = "sealed_restore"
_master: Cipher | None = None
# bot_id → (所有者；普通机器人为 None, 过期时刻)。
_OWNERS: dict[uuid.UUID, tuple[uuid.UUID | None, float]] = {}


def use_master(cipher: Cipher | None) -> None:
    """指定平台主密钥（测试用）；None 表示回到配置里的那一把。"""
    global _master  # noqa: PLW0603
    _master = cipher


def master() -> Cipher:
    return _master or get_settings().build_cipher()


def forget() -> None:
    """清本进程的所有者缓存。"""
    _OWNERS.clear()


async def owner_of(session: AsyncSession, bot_id: uuid.UUID) -> uuid.UUID | None:
    """助理记录的所有者；普通机器人（或记录不存在）返回 None。结果在进程内缓存一个 TTL。"""
    now = time.monotonic()
    hit = _OWNERS.get(bot_id)
    if hit is not None and hit[1] > now:
        return hit[0]
    row = (
        await session.execute(select(Bot.kind, Bot.owner_id).where(Bot.id == bot_id))
    ).first()
    if row is None:
        # 不缓存「不存在」：记录可能只是还没提交。
        return None
    owner: uuid.UUID | None = row[1] if row[0] == "assistant" else None
    _OWNERS[bot_id] = (owner, now + keys.KEY_TTL_SECONDS)
    return owner


async def _cipher(session: AsyncSession, bot_id: uuid.UUID, *, create_key: bool) -> Cipher | None:
    """这条记录的数据密钥；普通机器人返回 None。取不到时抛 ContentLocked。"""
    owner = await owner_of(session, bot_id)
    if owner is None:
        return None
    hit = keys.cached(owner)
    if hit is not None:
        return hit
    try:
        return await keys.data_cipher(session, master(), owner)
    except keys.KeyDestroyed:
        raise ContentLocked("所有者的数据密钥已销毁") from None
    except keys.KeyMissing:
        if not create_key:
            raise ContentLocked("所有者没有数据密钥") from None
    # 首次用到时生成第 1 代。reissue=False：已销毁的密钥只能由开通流程显式重新签发。
    await keys.ensure_key(session, master(), owner, reissue=False)
    try:
        return await keys.data_cipher(session, master(), owner)
    except keys.KeyUnavailable:
        raise ContentLocked("所有者的数据密钥已销毁") from None


def _seal(cipher: Cipher, aad_: str, kind: Kind, value: Any) -> Any:
    if isinstance(kind, JsonSpec):
        return seal_dict(cipher, aad_, value, kind)
    if isinstance(kind, Whole):
        return seal_whole(cipher, aad_, value)
    return seal_text(cipher, aad_, value)


def _open(cipher: Cipher, aad_: str, kind: Kind, value: Any) -> Any:
    if isinstance(kind, JsonSpec):
        return open_dict(cipher, aad_, value, kind)
    if isinstance(kind, Whole):
        return open_whole(cipher, aad_, value)
    return open_text(cipher, aad_, value)


def _looks_sealed(kind: Kind, value: Any) -> bool:
    if isinstance(kind, Text):
        return isinstance(value, str) and is_encrypted(value)
    return is_sealed(value)


def _nothing(kind: Kind, value: Any) -> bool:
    """没有内容可封存：字典列哪怕是空的也要封（靠封装认出这是助理的行）。"""
    if value is None:
        return True
    if isinstance(kind, JsonSpec):
        return not isinstance(value, dict)
    return value in ("", [], {})


def _blank(kind: Kind, value: Any) -> Any:
    """读不出来时给调用方的值：字典只剩明文键，其余为该列的空值。"""
    if isinstance(kind, JsonSpec):
        return _keep(kind, value) if isinstance(value, dict) else {}
    return copy.copy(kind.blank)


def _dropped(kind: Kind, value: Any) -> Any:
    """密钥销毁之后写入：内容丢弃，JSONB 列留下墓碑。"""
    if isinstance(kind, JsonSpec):
        return {**_keep(kind, value), SEALED: ""}
    if isinstance(kind, Whole):
        return {SEALED: ""}
    return kind.blank


async def seal_values(
    session: AsyncSession, model: type, bot_id: uuid.UUID | None, values: dict[str, Any]
) -> dict[str, Any]:
    """把 `values` 里的内容列封存后返回新字典；普通机器人、没有内容要写时原样返回。

    首次为某个所有者写入时生成其数据密钥。密钥已销毁时不抛错：内容丢弃，其余列照常返回，
    调用方的状态机照走。
    """
    spec = COLUMNS[model]
    touched = [c for c, kind in spec.items() if c in values and not _nothing(kind, values[c])]
    if not touched or bot_id is None:
        return values
    table = str(model.__tablename__)  # type: ignore[attr-defined]
    try:
        cipher = await _cipher(session, bot_id, create_key=True)
    except ContentLocked:
        log.warning("assistant_content_dropped", table=table, bot_id=str(bot_id))
        return {**values, **{c: _dropped(spec[c], values[c]) for c in touched}}
    if cipher is None:
        return values
    return {
        **values,
        **{c: _seal(cipher, aad(table, c, bot_id), spec[c], values[c]) for c in touched},
    }


async def open_row(session: AsyncSession, row: Any, *, viewer: uuid.UUID | None = None) -> bool:
    """把一行里封存的内容列还原成明文（不标脏）；返回内容是否可读。

    普通机器人的行、已经还原过的行原样返回。读不出来（密钥已销毁、墓碑、`viewer` 不是所有者）
    时不抛错：内容列置空，`is_locked(row)` 为真。主密钥配错、密文被改仍抛 `DecryptError`。

    Args:
        viewer: 给了就只对所有者本人还原（接口输出正文时用）；不给按运行时对待，一律还原
    """
    spec = COLUMNS.get(type(row)) if row is not None else None
    if spec is None:
        return True
    state = sa_inspect(row)
    values = state.dict
    pending = [c for c, kind in spec.items() if _looks_sealed(kind, values.get(c))]
    bot_id = values.get("bot_id")
    if bot_id is None or (not pending and _CIPHER not in state.info):
        return not state.info.get(_LOCKED, False)
    targets = pending
    try:
        if viewer is not None and await owner_of(session, bot_id) not in (None, viewer):
            # 不是本人：连已经还原在内存里的也收回去。
            targets = [c for c in spec if c in values]
            raise ContentLocked("只有所有者本人可以读取")
        cipher = await _cipher(session, bot_id, create_key=False)
        if cipher is None:
            # 普通机器人的正文恰好长得像密文。
            return True
        table = str(row.__tablename__)
        opened = {c: _open(cipher, aad(table, c, bot_id), spec[c], values[c]) for c in pending}
    except ContentLocked:
        for column in targets:
            set_committed_value(row, column, _blank(spec[column], values[column]))
        state.info.pop(_CIPHER, None)
        state.info[_LOCKED] = True
        return False
    for column, value in opened.items():
        set_committed_value(row, column, value)
    state.info[_CIPHER] = cipher
    state.info.pop(_LOCKED, None)
    return True


async def open_rows(
    session: AsyncSession, rows: Iterable[Any], *, viewer: uuid.UUID | None = None
) -> None:
    for row in rows:
        await open_row(session, row, viewer=viewer)


async def open_value(
    session: AsyncSession, model: type, column: str, bot_id: uuid.UUID, value: Any
) -> Any:
    """只取了一列时还原这个值；读不出来返回该列的空值。"""
    kind = COLUMNS[model][column]
    if not _looks_sealed(kind, value):
        return value
    try:
        cipher = await _cipher(session, bot_id, create_key=False)
        if cipher is None:
            return value
        table = str(model.__tablename__)  # type: ignore[attr-defined]
        return _open(cipher, aad(table, column, bot_id), kind, value)
    except ContentLocked:
        return _blank(kind, value)


def is_locked(row: Any) -> bool:
    """这一行的内容读不出来（已置空）。"""
    return row is not None and bool(sa_inspect(row).info.get(_LOCKED))


def is_opened(row: Any) -> bool:
    """这一行是助理的行，且内容已还原成明文。"""
    return row is not None and _CIPHER in sa_inspect(row).info


def _owner_sync(session: Session, bot_id: uuid.UUID) -> uuid.UUID | None:
    """`owner_of` 的同步版：给 flush 钩子用。缓存没有时走会话当前事务的同步连接查一次。"""
    now = time.monotonic()
    hit = _OWNERS.get(bot_id)
    if hit is not None and hit[1] > now:
        return hit[0]
    for pending in session.new:
        # 记录与它的内容行在同一次 flush 里新建：库里还查不到，也不进缓存。
        if isinstance(pending, Bot) and pending.id == bot_id:
            return pending.owner_id if pending.kind == "assistant" else None
    row = (
        session.connection().execute(select(Bot.kind, Bot.owner_id).where(Bot.id == bot_id)).first()
    )
    if row is None:
        return None
    owner: uuid.UUID | None = row[1] if row[0] == "assistant" else None
    _OWNERS[bot_id] = (owner, now + keys.KEY_TTL_SECONDS)
    return owner


def _changed(state: Any, spec: dict[str, Kind], *, new: bool) -> list[str]:
    """这次 flush 要写的、有内容的内容列。新建的行看有没有值，已有的行看属性历史。"""
    out: list[str] = []
    for column, kind in spec.items():
        if column not in state.dict or _nothing(kind, state.dict[column]):
            continue
        if new or state.attrs[column].history.has_changes():
            out.append(column)
    return out


@event.listens_for(Session, "before_flush")
def _reseal(session: Session, flush_context: Any, instances: Any) -> None:
    """助理的行经 ORM 新建或改了内容列：落库前换成密文。

    按行的 `bot_id` 判断归属，不看行上有没有还原标记——新建的行、读出时内容为空的行、已锁定
    的行都要管。没有内容列要写的行直接跳过，不查库；归属与数据密钥先查进程内缓存，缓存没有
    才用当前事务的同步连接各查一次（只取用，不签发）。密钥取不到时内容丢弃：JSONB 留墓碑、
    文本写空，内存里同样置空并标为已锁定。
    """
    restore: list[tuple[Any, str, Any]] = []
    session.info[_RESTORE] = restore
    for rows, new in ((session.new, True), (session.dirty, False)):
        for row in rows:
            spec = COLUMNS.get(type(row))
            if spec is None:
                continue
            state = sa_inspect(row)
            columns = _changed(state, spec, new=new)
            if not columns:
                continue
            bot_id = row.bot_id
            owner = _owner_sync(session, bot_id) if bot_id is not None else None
            if owner is None:
                continue
            table = str(row.__tablename__)
            try:
                cipher: Cipher | None = keys.data_cipher_sync(session, master(), owner)
            except keys.KeyUnavailable:
                cipher = None
            dropped = False
            for column in columns:
                kind = spec[column]
                plain = state.dict[column]
                try:
                    if cipher is None:
                        raise ContentLocked("所有者的数据密钥不可用")
                    stored = _seal(cipher, aad(table, column, bot_id), kind, plain)
                    kept = plain
                except ContentLocked:
                    stored, kept, dropped = _dropped(kind, plain), _blank(kind, plain), True
                setattr(row, column, stored)
                restore.append((row, column, kept))
            if dropped:
                log.warning("assistant_content_dropped", table=table, bot_id=str(bot_id))
                state.info.pop(_CIPHER, None)
                state.info[_LOCKED] = True
            elif cipher is not None:
                state.info[_CIPHER] = cipher


@event.listens_for(Session, "after_flush_postexec")
def _unseal_after_flush(session: Session, flush_context: Any) -> None:
    for row, column, plain in session.info.pop(_RESTORE, []):
        set_committed_value(row, column, plain)
```

- [ ] **Step 5: 会话类**

`coreman/core/assistants/session.py`：

```python
"""会话类：按主键取行、取单个实体、刷新实体时，自动把助理的内容列还原成明文。

读这几张表的代码绝大多数是直接 `session.get(...)`，不经过 bus 函数；在这里兜住，调用点不用改。
`scalars()` / `execute()` 的结果是惰性的，这里管不到，由调用点显式 `sealed.open_rows`。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed


class SealedSession(AsyncSession):
    async def get(self, entity: Any, ident: Any, **kw: Any) -> Any:
        row = await super().get(entity, ident, **kw)
        await sealed.open_row(self, row)
        return row

    async def scalar(self, statement: Any, params: Any = None, **kw: Any) -> Any:
        value = await super().scalar(statement, params, **kw)
        if type(value) in sealed.COLUMNS:
            await sealed.open_row(self, value)
        return value

    async def refresh(
        self, instance: object, attribute_names: Any = None, with_for_update: Any = None
    ) -> None:
        await super().refresh(instance, attribute_names, with_for_update)
        await sealed.open_row(self, instance)
```

若 `uv run mypy coreman` 对这三个方法报 `override` 不兼容，在对应的 `async def` 行尾加 `# type: ignore[override]`。

`coreman/core/db/session.py`：导入段之后加一行，并把最后一个函数改成：

```python
from coreman.core.assistants.session import SealedSession
```

```python
def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # SealedSession：个人助理的内容列读出时自动还原，见 core/assistants/sealed.py。
    return async_sessionmaker(engine, class_=SealedSession, expire_on_commit=False)
```

- [ ] **Step 6: 运行确认通过，并确认现有用例不受影响**

Run: `uv run pytest tests/integration/test_assistant_sealed_rows.py tests/unit/test_assistant_sealed_codec.py -v`
Expected: 全部通过。

Run: `uv run pytest tests/integration/test_bus_tasks.py tests/integration/test_bus_streams_outbox.py tests/integration/test_chat_logs.py -q`
Expected: 全部通过（现有用例不改）。

Run: `uv run mypy coreman`
Expected: 无新增报错。

- [ ] **Step 7: 提交**

```bash
git add coreman/core/assistants coreman/core/db/session.py tests/integration/assistant_helpers.py tests/integration/conftest.py tests/integration/test_assistant_sealed_rows.py tests/integration/test_orm_flush_reseal_probe.py tests/integration/test_assistant_orm_flush.py
git commit -m "个人助理：内容列按行封存与还原，会话读取时自动解密"
```

---

### Task 5: `tasks` 接入

**Files:**
- Modify: `coreman/core/bus/tasks.py`（导入；`enqueue` :78、`claim` :103、`finish` :139、`active_for_session` :190、`open_for_session` :203）
- Test: `tests/integration/test_assistant_sealed_tasks.py`

**Interfaces:**
- Consumes: `sealed.seal_values`、`sealed.open_rows`、`sealed.is_locked`、`sealed.LOCKED_CODE`；`SealedSession.get`（`tasks.get` 经它自动还原）。
- Produces:
  - `tasks.enqueue` / `claim` / `get` / `active_for_session` / `open_for_session` 返回的 `Task.payload`、`Task.result` 始终是明文。
  - `tasks.finish(..., bot_id: uuid.UUID | None = None)`：新增可选参数；没传且 `result` 非空时按主键查一次。
  - `tasks.claim` 遇到内容不可读的任务：置 `failed` / `content_locked`，返回 `None`。
  - `tasks.list_active` 不还原（运维列表只出元数据）。

- [ ] **Step 1: 写失败测试**

```python
"""tasks：载荷与结果在库里是密文，经 bus 函数取回是明文；被 SQL 读取的键仍在明文部分。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys
from coreman.core.bus import instances, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Task
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
WORKER = "worker-a:h:1:1"


async def _worker(session: AsyncSession) -> None:
    await instances.register(
        session, instance_id=WORKER, service="worker", version="dev", capacity=30
    )
    await session.commit()


def _chat(bot_id, **extra: object) -> NewTask:  # type: ignore[no-untyped-def]
    payload = {"message": {"text": SECRET}, "platform_user_id": "ou_1", **extra}
    return NewTask(bot_id=bot_id, kind="chat", payload=payload, session_key="oc_1")


async def _column(session: AsyncSession, column: str, task_id: int) -> str:
    return str(await raw(session, f"SELECT {column}::text FROM tasks WHERE id = :id", id=task_id))


async def test_payload_and_result_roundtrip(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    await _worker(db_session)
    task = await tasks.enqueue(db_session, _chat(bot.id))
    await db_session.commit()
    assert task is not None and task.payload["message"]["text"] == SECRET
    stored = await _column(db_session, "payload", task.id)
    assert SECRET not in stored and json.loads(stored)["platform_user_id"] == "ou_1"
    factory = make_session_factory(db_engine)
    async with factory() as other:
        claimed = await tasks.claim(other, lane="normal", instance_id=WORKER)
        await other.commit()
        assert claimed is not None and claimed.id == task.id
        assert claimed.payload == {"message": {"text": SECRET}, "platform_user_id": "ou_1"}
        active = await tasks.active_for_session(other, bot.id, "oc_1")
        assert [t.payload["message"]["text"] for t in active] == [SECRET]
        opened = await tasks.open_for_session(other, bot.id, "oc_1", "chat")
        assert [t.id for t in opened] == [task.id] and opened[0].payload["message"]
        # 不传 bot_id：按主键查到这条任务属于谁。
        assert await tasks.finish(other, task.id, status="succeeded", result={"reply": SECRET})
        await other.commit()
    assert SECRET not in await _column(db_session, "result", task.id)
    async with factory() as other:
        got = await tasks.get(other, task.id)
        assert got is not None and got.result == {"reply": SECRET}


async def test_claim_sql_still_reads_plain_keys(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    await _worker(db_session)
    first = await tasks.enqueue(db_session, _chat(bot.id))
    await db_session.commit()
    assert first is not None
    assert (await tasks.claim(db_session, lane="normal", instance_id=WORKER)) is not None
    await db_session.commit()
    second = await tasks.enqueue(db_session, _chat(bot.id, serialize_session=True))
    await db_session.commit()
    assert second is not None
    # 认领语句里的 payload->>'serialize_session' 在封装上照样命中：前一条没做完，这条不放行。
    assert await tasks.claim(db_session, lane="normal", instance_id=WORKER) is None
    assert not await tasks.has_newer(
        db_session, bot.id, "oc_1", task_id=first.id, kind="chat"
    )


async def test_release_hold_removes_the_plain_key(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    held = await tasks.enqueue(db_session, _chat(bot.id, stale_hold=True))
    await db_session.commit()
    assert held is not None
    row = await db_session.get(Task, held.id, with_for_update=True, populate_existing=True)
    assert row is not None
    await tasks.release_hold(db_session, row)
    await db_session.commit()
    stored = json.loads(await _column(db_session, "payload", held.id))
    assert "stale_hold" not in stored and "_sealed" in stored and "stale_confirmed" not in stored
    async with make_session_factory(db_engine)() as other:
        got = await tasks.get(other, held.id)
        assert got is not None and got.payload["stale_confirmed"] is True
        assert got.payload["message"]["text"] == SECRET


async def test_claim_fails_a_task_whose_key_is_destroyed(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await _worker(db_session)
    task = await tasks.enqueue(db_session, _chat(bot.id))
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    assert task is not None
    assert await tasks.claim(db_session, lane="normal", instance_id=WORKER) is None
    await db_session.commit()
    assert await _column(db_session, "status", task.id) == "failed"
    assert await _column(db_session, "error_code", task.id) == "content_locked"
    # 销毁之后结束任务：不抛错，结果丢弃。
    assert await tasks.finish(db_session, task.id, status="failed", result={"reply": SECRET})
    await db_session.commit()
    assert json.loads(await _column(db_session, "result", task.id)) == {"_sealed": ""}


async def test_standard_bot_tasks_stay_plain(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    task = await tasks.enqueue(db_session, _chat(bot.id))
    assert task is not None
    await tasks.finish(db_session, task.id, status="succeeded", result={"reply": SECRET})
    await db_session.commit()
    assert SECRET in await _column(db_session, "payload", task.id)
    assert "_sealed" not in await _column(db_session, "payload", task.id)
    assert json.loads(await _column(db_session, "result", task.id)) == {"reply": SECRET}
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_sealed_tasks.py -v`
Expected: FAIL，`test_payload_and_result_roundtrip` 断言 `SECRET not in stored` 不成立。

- [ ] **Step 3: 实现**

`coreman/core/bus/tasks.py`，在 `from coreman.core.bus.notify import notify` 之前加：

```python
from coreman.core.assistants import sealed
```

`enqueue`：在 `stmt = insert(Task).values(**values)` 之前加一行：

```python
    values = await sealed.seal_values(session, Task, new.bot_id, values)
```

`claim` 整个函数换成：

```python
async def claim(session: AsyncSession, *, lane: str, instance_id: str) -> Task | None:
    row = (await session.execute(_CLAIM_SQL, {"instance": instance_id, "lane": lane})).first()
    if row is None:
        return None
    task = await get(session, int(row[0]))
    if task is not None and sealed.is_locked(task):
        # 个人助理的数据密钥已销毁：内容读不出来，不交给处理器。
        await finish(
            session,
            task.id,
            status="failed",
            error_code=sealed.LOCKED_CODE,
            error_message="所有者的数据密钥已销毁，任务内容不可读",
        )
        return None
    return task
```

`finish`：签名在 `only_active: bool = False,` 之后加 `bot_id: uuid.UUID | None = None,`；函数体第一行（`stmt = update(Task).where(Task.id == task_id)` 之前）加：

```python
    if result is not None:
        if bot_id is None:
            bot_id = await session.scalar(select(Task.bot_id).where(Task.id == task_id))
        result = (await sealed.seal_values(session, Task, bot_id, {"result": result}))["result"]
```

`active_for_session` 与 `open_for_session`：两处的 `return list(...)` 都改成：

```python
    rows = list(
        (await session.execute(stmt, execution_options={"populate_existing": True})).scalars()
    )
    await sealed.open_rows(session, rows)
    return rows
```

`list_active` 不动。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_sealed_tasks.py tests/integration/test_bus_tasks.py -v`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/bus/tasks.py tests/integration/test_assistant_sealed_tasks.py
git commit -m "个人助理：tasks 的载荷与结果加密入库"
```

---

### Task 6: `inbound_events` 接入

**Files:**
- Modify: `coreman/runtime/gateway_common/inbound.py:50-67`
- Modify: `coreman/core/api_tasks/service.py:224-239`
- Test: `tests/integration/test_assistant_sealed_inbound.py`

**Interfaces:**
- Consumes: `sealed.seal_values`；Task 5 的 `tasks.enqueue`（任务载荷里嵌的那份消息由它封存）。
- Produces: `inbound_events.payload` 对助理记录是封装；`reply_context` 与其余列保持明文。读取不需要新函数：`session.get(InboundEvent, …)`（含 `coreman/runtime/worker/replies.py:32` 的 `load_inbound`）经 `SealedSession` 自动还原。

- [ ] **Step 1: 写失败测试**

```python
"""inbound_events：事件载荷加密入库，路由信息与去重键不变。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.db.models import Bot
from coreman.core.db.session import make_session_factory
from coreman.core.wecom.messages import InboundMessage, Sender, TextPart
from coreman.runtime.gateway_common.inbound import enqueue_inbound
from coreman.runtime.worker.replies import load_inbound
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")


def _message(bot: Bot) -> InboundMessage:
    return InboundMessage(
        platform="feishu",
        bot_id=bot.id,
        kind="message",
        chat_type="single",
        chat_id="oc_1",
        sender=Sender(platform_user_id="ou_1"),
        message_id="om_1",
        parts=[TextPart(text=SECRET)],
        reply_context={"chat_id": "oc_1", "message_id": "om_1"},
        raw={"event": {"message": {"message_type": "text", "content": SECRET}}},
    )


def _snapshot(bot: Bot) -> SimpleNamespace:
    return SimpleNamespace(id=bot.id, bot_key=bot.bot_key, welcome_message=None)


async def test_event_payload_is_sealed_and_readable(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    task = await enqueue_inbound(db_session, _snapshot(bot), _message(bot), lease_generation=0)
    await db_session.commit()
    assert task is not None and task.inbound_event_id is not None
    event = await raw(
        db_session,
        "SELECT payload::text || '|' || reply_context::text FROM inbound_events WHERE id = :id",
        id=task.inbound_event_id,
    )
    assert SECRET not in event and "_sealed" in event and '"message_id": "om_1"' in event
    stored_task = await raw(
        db_session, "SELECT payload::text FROM tasks WHERE id = :id", id=task.id
    )
    assert SECRET not in stored_task
    async with make_session_factory(db_engine)() as other:
        loaded = await load_inbound(other, task)
        assert loaded.payload["parts"][0]["text"] == SECRET
        assert loaded.payload["raw"]["event"]["message"]["content"] == SECRET
    # 平台重推：去重键不受加密影响。
    again = await enqueue_inbound(db_session, _snapshot(bot), _message(bot), lease_generation=0)
    assert again is None


async def test_standard_bot_event_stays_plain(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    task = await enqueue_inbound(db_session, _snapshot(bot), _message(bot), lease_generation=0)
    await db_session.commit()
    assert task is not None
    event = await raw(
        db_session,
        "SELECT payload::text FROM inbound_events WHERE id = :id",
        id=task.inbound_event_id,
    )
    assert SECRET in event and "_sealed" not in event
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_sealed_inbound.py -v`
Expected: FAIL，第一条用例断言 `SECRET not in event` 不成立。

- [ ] **Step 3: 实现**

`coreman/runtime/gateway_common/inbound.py`：导入段加 `from coreman.core.assistants import sealed`。`enqueue_inbound` 里 `payload = message.model_dump(mode="json")` 的下一行加：

```python
    stored = await sealed.seal_values(session, InboundEvent, bot.id, {"payload": payload})
```

并把 `insert(InboundEvent).values(...)` 里的 `payload=payload,` 改成：

```python
            payload=stored["payload"],
```

局部变量 `payload` 保持明文，后面建任务、触发告警源仍用它。

`coreman/core/api_tasks/service.py`：导入段加 `from coreman.core.assistants import sealed`。在 `event_id = await session.scalar(` 之前加：

```python
    stored = await sealed.seal_values(
        session, InboundEvent, bot.id, {"payload": message.model_dump(mode="json")}
    )
```

并把 `insert(InboundEvent).values(...)` 里的 `payload=message.model_dump(mode="json"),` 改成：

```python
            payload=stored["payload"],
```

（助理记录不开放 API 驱动，这一处实际只会遇到普通机器人；统一经过 `seal_values` 是为了 `inbound_events` 的每个写入口都同一个写法。）

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_sealed_inbound.py tests/integration/test_api_tasks.py tests/integration/test_gateway_wecom.py -q`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/runtime/gateway_common/inbound.py coreman/core/api_tasks/service.py tests/integration/test_assistant_sealed_inbound.py
git commit -m "个人助理：inbound_events 的事件载荷加密入库"
```

---

### Task 7: `task_streams` 接入

**Files:**
- Modify: `coreman/core/bus/streams.py`（导入；`update_state` :73、`update` :98、`complete` :170、`pending_for_bot` :211、`active_for_bot` :240）
- Modify: `coreman/runtime/worker/stream_writer.py:98,121,129`
- Modify: `coreman/runtime/worker/replies.py:81`
- Modify: `coreman/runtime/worker/service.py:310`
- Modify: `coreman/runtime/scheduler/reaper.py:197,277`
- Modify: `coreman/runtime/gateway_feishu/transport.py:258-269`
- Test: `tests/integration/test_assistant_sealed_streams.py`

**Interfaces:**
- Consumes: `sealed.seal_values`、`sealed.open_rows`、`sealed.is_locked`。
- Produces:
  - `streams.update_state(session, task_id, *, bot_id: uuid.UUID | None = None, **fields) -> Completion`
  - `streams.update(session, task_id, *, bot_id: uuid.UUID | None = None, **fields) -> int`
  - `streams.complete(session, task_id, *, final_text: str, pending_card: dict[str, Any] | None = None, bot_id: uuid.UUID | None = None) -> Completion`
  - `bot_id` 没传且确有内容要写时，按主键查一次 `task_streams.bot_id`。
  - `streams.pending_for_bot` / `active_for_bot` 返回的行内容是明文，内容不可读的行不返回。
  - `streams.create` 不变（建流时没有内容）；`streams.get` 经 `SealedSession` 自动还原。

- [ ] **Step 1: 写失败测试**

```python
"""task_streams：思考、正文、卡片与计划加密入库；网关取到的是明文；销毁后跳过。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.bus import streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Bot
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
CONTENT = (
    "SELECT concat_ws('|', thinking_md, pending_text, final_text, pending_card::text, plan::text)"
    " FROM task_streams WHERE task_id = :id"
)
PLAN = [{"text": SECRET, "status": "pending"}]


async def _stream(session: AsyncSession, bot: Bot) -> int:
    task = await tasks.enqueue(
        session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="oc_1")
    )
    assert task is not None
    await streams.create(
        session,
        task_id=task.id,
        bot_id=bot.id,
        platform="feishu",
        stream_id="s1",
        reply_context={"chat_id": "oc_1"},
        lease_generation=1,
        running_since=datetime.now(UTC),
    )
    await session.commit()
    return task.id


async def test_stream_content_roundtrip(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    task_id = await _stream(db_session, bot)
    version = await streams.update(
        db_session,
        task_id,
        bot_id=bot.id,
        thinking_md=f"想：{SECRET}",
        pending_text=f"答：{SECRET}",
        segment_boundaries=[2],
        plan=PLAN,
    )
    await db_session.commit()
    assert version == 1
    stored = str(await raw(db_session, CONTENT, id=task_id))
    assert SECRET not in stored and "enc:v2:d1:" in stored
    factory = make_session_factory(db_engine)
    async with factory() as other:
        pending = await streams.pending_for_bot(other, bot.id)
        assert [p.task_id for p in pending] == [task_id]
        assert pending[0].pending_text == f"答：{SECRET}" and pending[0].plan == PLAN
        assert pending[0].segment_boundaries == [2]
        active = await streams.active_for_bot(other, bot.id)
        assert active[0].thinking_md == f"想：{SECRET}"
    # 不传 bot_id：按主键查到这条流属于谁。
    done = await streams.complete(
        db_session, task_id, final_text=f"终：{SECRET}", pending_card={"body": SECRET}
    )
    await db_session.commit()
    assert done.version == 2
    assert SECRET not in str(await raw(db_session, CONTENT, id=task_id))
    async with factory() as other:
        row = await streams.get(other, task_id)
        assert row is not None and row.final_text == f"终：{SECRET}"
        assert row.pending_card == {"body": SECRET} and row.is_complete


async def test_metadata_only_update_needs_no_content(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    task_id = await _stream(db_session, bot)
    await streams.update(
        db_session, task_id, delivery_mode="proactive", background_state={"offset": 3}
    )
    await db_session.commit()
    stored = await raw(
        db_session,
        "SELECT background_state::text FROM task_streams WHERE task_id = :id",
        id=task_id,
    )
    assert stored == '{"offset": 3}'


async def test_destroyed_key_skips_the_stream(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    task_id = await _stream(db_session, bot)
    await streams.update(db_session, task_id, bot_id=bot.id, pending_text=SECRET, plan=PLAN)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    assert await streams.pending_for_bot(db_session, bot.id) == []
    assert await streams.active_for_bot(db_session, bot.id) == []
    row = await streams.get(db_session, task_id)
    assert row is not None and sealed.is_locked(row)
    assert row.pending_text == "" and row.final_text is None and row.plan == []
    # 销毁之后收尾：不抛错，终稿丢弃，流照常结束。
    done = await streams.complete(db_session, task_id, final_text=SECRET, bot_id=bot.id)
    await db_session.commit()
    assert done.version == 2
    assert await raw(
        db_session, "SELECT final_text IS NULL FROM task_streams WHERE task_id = :id", id=task_id
    )


async def test_standard_bot_stream_stays_plain(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    task_id = await _stream(db_session, bot)
    await streams.update(db_session, task_id, pending_text=SECRET, plan=PLAN)
    await streams.complete(db_session, task_id, final_text=SECRET)
    await db_session.commit()
    stored = str(await raw(db_session, CONTENT, id=task_id))
    assert SECRET in stored and "enc:v2:" not in stored and "_sealed" not in stored
    pending = await streams.pending_for_bot(db_session, bot.id)
    assert pending[0].final_text == SECRET
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_sealed_streams.py -v`
Expected: FAIL，`test_stream_content_roundtrip` 报 `ValueError: 不可写字段: ['bot_id']`。

- [ ] **Step 3: 实现 bus 层**

`coreman/core/bus/streams.py`，在 `from coreman.core.bus.notify import notify` 之前加：

```python
from coreman.core.assistants import sealed
```

在 `async def create(` 之前加：

```python
async def _sealed_fields(
    session: AsyncSession, task_id: int, bot_id: uuid.UUID | None, fields: dict[str, Any]
) -> dict[str, Any]:
    """把内容列封存；没有内容要写时不查库。调用方没给 bot_id 就按主键查这条流属于谁。"""
    if not any(fields.get(column) for column in sealed.COLUMNS[TaskStream]):
        return fields
    if bot_id is None:
        bot_id = await session.scalar(select(TaskStream.bot_id).where(TaskStream.task_id == task_id))
    return await sealed.seal_values(session, TaskStream, bot_id, fields)


async def _readable(session: AsyncSession, rows: list[TaskStream]) -> list[TaskStream]:
    """内容还原成明文；读不出来的行（数据密钥已销毁）不交给网关。"""
    await sealed.open_rows(session, rows)
    return [row for row in rows if not sealed.is_locked(row)]
```

`update_state`：签名改成下面这样，并在 `raise ValueError(...)` 那个 `if` 之后加一行：

```python
async def update_state(
    session: AsyncSession, task_id: int, *, bot_id: uuid.UUID | None = None, **fields: Any
) -> Completion:
```

```python
    fields = await _sealed_fields(session, task_id, bot_id, fields)
```

`update` 整个函数换成：

```python
async def update(
    session: AsyncSession, task_id: int, *, bot_id: uuid.UUID | None = None, **fields: Any
) -> int:
    """兼容调用方只读取新版本号。"""
    return (await update_state(session, task_id, bot_id=bot_id, **fields)).version
```

`complete`：签名在 `pending_card: dict[str, Any] | None = None,` 之后加 `bot_id: uuid.UUID | None = None,`；函数体第一行加：

```python
    stored = await _sealed_fields(
        session, task_id, bot_id, {"final_text": final_text, "pending_card": pending_card}
    )
```

并把 `.values(...)` 里的两行改成：

```python
                final_text=stored["final_text"],
                pending_card=stored["pending_card"],
```

`pending_for_bot` 与 `active_for_bot`：两处的 `return list(...)` 都改成：

```python
    rows = list(
        (await session.execute(stmt, execution_options={"populate_existing": True})).scalars()
    )
    return await _readable(session, rows)
```

- [ ] **Step 4: 调用点传 `bot_id`，飞书网关的直接查询显式还原**

流式写入是热路径，传 `bot_id` 省掉按主键查的那一次：

- `coreman/runtime/worker/stream_writer.py:98` 的 `streams.update_state(` 与 `:121` 的 `streams.update(`：参数表里 `self.ctx.task.id,` 之后各加一行 `bot_id=self.ctx.task.bot_id,`。
- `coreman/runtime/worker/stream_writer.py:129` 的 `streams.complete(`：`pending_card=pending_card,` 之后加 `bot_id=self.ctx.task.bot_id,`。
- `coreman/runtime/worker/replies.py:81` 改成 `await streams.complete(session, ctx.task.id, final_text=text, bot_id=ctx.task.bot_id)`。
- `coreman/runtime/worker/service.py:310` 的 `streams.complete(` 调用里，`final_text=msg("relay_error", ctx.locale, relay="?")` 之后加 `, bot_id=ctx.task.bot_id`。
- `coreman/runtime/scheduler/reaper.py:197` 改成 `await streams.complete(session, task.id, final_text=final, bot_id=task.bot_id)`；`:277` 改成 `await streams.complete(session, task.id, final_text=prefix + lost, bot_id=task.bot_id)`。

`coreman/runtime/gateway_feishu/transport.py`：导入段加 `from coreman.core.assistants import sealed`。`_deliver` 里，`rows = list(await session.scalars(select(TaskStream)...))` 这条语句之后、仍在 `async with self.factory() as session:` 块内，加：

```python
                await sealed.open_rows(session, rows)
                rows = [row for row in rows if not sealed.is_locked(row)]
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_sealed_streams.py tests/integration/test_bus_streams_outbox.py tests/integration/test_feishu_transport.py tests/integration/test_scheduler_reaper.py tests/integration/test_worker_service.py -q`
Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add coreman/core/bus/streams.py coreman/runtime tests/integration/test_assistant_sealed_streams.py
git commit -m "个人助理：task_streams 的思考与正文加密入库"
```

---

### Task 8: `outbox` 接入

**Files:**
- Modify: `coreman/core/bus/outbox.py`（导入；`add` :87）
- Modify: `coreman/runtime/gateway_feishu/transport.py:818-822`（`consume_one`）
- Modify: `coreman/api/routers/infra_push.py:65-70`
- Test: `tests/integration/test_assistant_sealed_outbox.py`

**Interfaces:**
- Consumes: `sealed.seal_values`、`sealed.open_row`、`sealed.is_locked`、`sealed.LOCKED_CODE`；Task 4 的 flush 钩子（网关发出后写 `_feishu_message_id` 靠它重新封存）。
- Produces:
  - `outbox.add` 写入的 `payload` 对助理记录是封装；返回的 `OutboxItem.payload` 是明文。
  - `outbox.claim_next` 返回的条目内容是明文；内容不可读时 `sealed.is_locked(item)` 为真，由网关置 `skipped`。
  - `target` 整列、`kind='notify'`（不绑机器人）的条目不加密。

- [ ] **Step 1: 写失败测试**

```python
"""outbox：出站载荷加密入库，网关发送前取到明文；内部键与卡片的 task_id 仍可被 SQL 过滤。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.bus import outbox
from coreman.core.db.models import OutboxItem
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
PAYLOAD = {
    "markdown": SECRET,
    "card": {"task_id": "choice:1:q0", "body": SECRET},
    "_reply": True,
}


async def _add(session: AsyncSession, bot_id, key: str = "77:send:final") -> OutboxItem:  # type: ignore[no-untyped-def]
    item = await outbox.add(
        session,
        bot_id=bot_id,
        platform="feishu",
        kind="send",
        dedupe_key=key,
        target={"chat_id": "oc_1"},
        payload=PAYLOAD,
    )
    await session.commit()
    assert item is not None
    return item


async def _stored(session: AsyncSession, item_id: int) -> str:
    return str(
        await raw(
            session,
            "SELECT payload::text || '|' || target::text FROM outbox WHERE id = :id",
            id=item_id,
        )
    )


async def test_payload_roundtrip_and_sql_filters(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    item = await _add(db_session, bot.id)
    assert item.payload == PAYLOAD
    stored = await _stored(db_session, item.id)
    assert SECRET not in stored and '"chat_id": "oc_1"' in stored
    envelope = json.loads(stored.split("|")[0])
    assert envelope["_reply"] is True and envelope["card"] == {"task_id": "choice:1:q0"}
    async with make_session_factory(db_engine)() as other:
        claimed = await outbox.claim_next(other, bot_id=bot.id)
        assert claimed is not None and claimed.payload == PAYLOAD
        # 网关发出后记下平台消息编号：内部键留明文，其余重新封存。
        claimed.payload = {**claimed.payload, "_feishu_message_id": "om_9"}
        await outbox.mark_sent(other, claimed.id)
        await other.commit()
    assert SECRET not in await _stored(db_session, item.id)
    found = await db_session.scalar(
        select(OutboxItem.id).where(
            OutboxItem.bot_id == bot.id,
            OutboxItem.status == "sent",
            OutboxItem.payload["_feishu_message_id"].astext == "om_9",
            OutboxItem.payload["card"]["task_id"].astext == "choice:1:q0",
        )
    )
    assert found == item.id
    again = await db_session.get(OutboxItem, item.id, populate_existing=True)
    assert again is not None and again.payload == {**PAYLOAD, "_feishu_message_id": "om_9"}


async def test_destroyed_key_marks_the_item_locked(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await _add(db_session, bot.id)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    claimed = await outbox.claim_next(db_session, bot_id=bot.id)
    assert claimed is not None and sealed.is_locked(claimed)
    assert "markdown" not in claimed.payload and claimed.payload["_reply"] is True
    await outbox.mark_skipped(db_session, claimed.id, sealed.LOCKED_CODE)
    await db_session.commit()
    assert await outbox.claim_next(db_session, bot_id=bot.id) is None


async def test_standard_bot_outbox_stays_plain(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    item = await _add(db_session, bot.id)
    stored = await _stored(db_session, item.id)
    assert SECRET in stored and "_sealed" not in stored
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_sealed_outbox.py -v`
Expected: FAIL，`test_payload_roundtrip_and_sql_filters` 断言 `SECRET not in stored` 不成立。

- [ ] **Step 3: 实现**

`coreman/core/bus/outbox.py`，在 `from coreman.core.bus.notify import notify` 之前加：

```python
from coreman.core.assistants import sealed
```

`add`：在 `stmt = (` 之前（`if not_before is not None:` 那个分支之后）加一行：

```python
    values = await sealed.seal_values(session, OutboxItem, bot_id, values)
```

`coreman/runtime/gateway_feishu/transport.py` 的 `consume_one`（`sealed` 已在 Task 7 导入）：在 `if item is None: return False` 之后、`if not await private_target_valid(session, item):` 之前加：

```python
            if sealed.is_locked(item):
                # 个人助理的数据密钥已销毁：内容读不出来，不发。
                await outbox.mark_skipped(session, item.id, sealed.LOCKED_CODE)
                await session.commit()
                return True
```

这个分支与紧随其后的 `private_target_valid` 分支同形；它依赖的 `is_locked` 已由本任务的 `test_destroyed_key_marks_the_item_locked` 覆盖。

`coreman/api/routers/infra_push.py`：导入段加 `from coreman.core.assistants import sealed`。幂等重放那一段，在 `).scalar_one()` 之后、`if item.bot_id != bot.id or ...` 之前加一行（比对的是明文载荷，取回的行要先还原）：

```python
        await sealed.open_row(session, item)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_sealed_outbox.py tests/integration/test_bus_streams_outbox.py tests/integration/test_feishu_transport.py tests/integration/test_card_actions.py -q`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/bus/outbox.py coreman/runtime/gateway_feishu/transport.py coreman/api/routers/infra_push.py tests/integration/test_assistant_sealed_outbox.py
git commit -m "个人助理：outbox 的出站载荷加密入库"
```

---

### Task 9: `chat_logs` 接入

**Files:**
- Modify: `coreman/core/chat/chat_logs.py`（导入；`_finish` :161、`open_turn` :177、`insert_turn` :220、`ChatLogWriter.write` :293、`amend_response` :307）
- Modify: `coreman/core/chat/session_carry.py:35-47`
- Modify: `coreman/core/chat/session_switch.py:135-138`
- Test: `tests/integration/test_assistant_sealed_chat_logs.py`

**Interfaces:**
- Consumes: `sealed.seal_values`、`sealed.open_rows`、`sealed.open_value`。
- Produces: `chat_logs.message_content` / `quoted_content` / `response_content` 对助理记录是密文；`session_carry.carried_context` 与 `session_switch.recent_sessions` 读到明文。截断（`LIMITS`）仍按明文长度在加密之前做。`file_info`、`error_message` 已在 Task 3 登记为内容列，随这五个函数的整行写入一并封存；`close_running` 与巡检的 `update(ChatLog)` 单独写 `error_message`，留到 Task 13。`user_name`、`error_code`、token 与耗时保持明文。

- [ ] **Step 1: 写失败测试**

```python
"""chat_logs：消息与回复正文加密入库；换运行时带上文、最近会话预览读到明文。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys
from coreman.core.chat import chat_logs
from coreman.core.chat.chat_logs import ChatLogEntry, ChatLogWriter
from coreman.core.chat.session_carry import carried_context
from coreman.core.chat.session_switch import recent_sessions
from coreman.core.db.models import Bot, ChatLog
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
RELAY_SESSION = uuid.uuid4()
CONTENT = (
    "SELECT concat_ws('|', message_content, quoted_content, response_content)"
    " FROM chat_logs WHERE task_id = :task_id"
)


def _entry(bot: Bot, user_id: uuid.UUID | None, **over: object) -> ChatLogEntry:
    base: dict[str, object] = dict(
        bot_id=bot.id,
        bot_key=bot.bot_key,
        platform="feishu",
        chat_type="single",
        message_type="text",
        status="success",
        request_at=datetime.now(UTC),
        user_id=user_id,
        user_name="张三",
        chat_id="oc_1",
        session_key="oc_1",
        relay_session_id=RELAY_SESSION,
        task_id=1,
        message_content=f"问：{SECRET}",
        quoted_content=f"引：{SECRET}",
        response_content=f"答：{SECRET}",
    )
    base.update(over)
    return ChatLogEntry(**base)  # type: ignore[arg-type]


async def test_turn_is_sealed_from_open_to_finish(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    assert await chat_logs.open_turn(db_session, _entry(bot, owner.id, response_content=None))
    await db_session.commit()
    opened = str(await raw(db_session, CONTENT, task_id=1))
    assert SECRET not in opened and "enc:v2:d1:" in opened
    assert await chat_logs.finish_turn(db_session, _entry(bot, owner.id))
    await db_session.commit()
    assert SECRET not in str(await raw(db_session, CONTENT, task_id=1))
    writer = ChatLogWriter(make_session_factory(db_engine))
    assert await writer.amend_response(1, f"改：{SECRET}")
    assert SECRET not in str(await raw(db_session, CONTENT, task_id=1))
    row = await db_session.scalar(
        select(ChatLog).where(ChatLog.task_id == 1).execution_options(populate_existing=True)
    )
    assert row is not None and row.status == "success"
    assert row.message_content == f"问：{SECRET}" and row.quoted_content == f"引：{SECRET}"
    assert row.response_content == f"改：{SECRET}"


async def test_carry_and_recent_sessions_read_plaintext(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id))
    # 先提交：首次写入时生成的数据密钥要对另一个会话可见，否则它会等在这一行上。
    await db_session.commit()
    assert await ChatLogWriter(make_session_factory(db_engine)).write(
        _entry(bot, owner.id, task_id=2)
    )
    assert SECRET not in str(await raw(db_session, CONTENT, task_id=1))
    assert SECRET not in str(await raw(db_session, CONTENT, task_id=2))
    carried = await carried_context(
        db_session, bot_id=bot.id, session_key="oc_1", relay_session_id=RELAY_SESSION
    )
    assert carried is not None and f"问：{SECRET}" in carried and f"答：{SECRET}" in carried
    previews = await recent_sessions(
        db_session, bot_id=bot.id, session_key="oc_1", relay_server_id=None
    )
    assert [p.preview for p in previews] == [f"问：{SECRET}"]


async def test_destroyed_key_leaves_nothing_to_carry(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id))
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    assert (
        await carried_context(
            db_session, bot_id=bot.id, session_key="oc_1", relay_session_id=RELAY_SESSION
        )
        is None
    )
    # 销毁之后的轮次照常记一行，正文丢弃。
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id, task_id=3))
    await db_session.commit()
    assert str(await raw(db_session, CONTENT, task_id=3)) == ""


async def test_standard_bot_log_stays_plain(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    assert await chat_logs.insert_turn(db_session, _entry(bot, None))
    await db_session.commit()
    stored = str(await raw(db_session, CONTENT, task_id=1))
    assert stored == f"问：{SECRET}|引：{SECRET}|答：{SECRET}"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_sealed_chat_logs.py -v`
Expected: FAIL，第一条用例断言 `SECRET not in opened` 不成立。

- [ ] **Step 3: 写入封存**

`coreman/core/chat/chat_logs.py`，在 `from coreman.core.db.models import CHAT_LOG_RUNNING, ChatLog, UserReached` 之前加：

```python
from coreman.core.assistants import sealed
```

`_finish`：在 `await _with_cost(session, entry, values)` 之后加一行：

```python
    values = await sealed.seal_values(session, ChatLog, entry.bot_id, values)
```

`open_turn`：`async with session.begin_nested():` 块内第一行加一句，并把 `.values(**values)` 改成 `.values(**stored)`（封存放在保存点里：出错只回滚保存点，不弄挂对话）：

```python
            stored = await sealed.seal_values(session, ChatLog, entry.bot_id, values)
```

`insert_turn`：把 `await _insert_once(session, entry, values)` 改成：

```python
            await _insert_once(
                session, entry, await sealed.seal_values(session, ChatLog, entry.bot_id, values)
            )
```

`ChatLogWriter.write` 里的 `run`：把 `await _insert_once(session, entry, values)` 改成与上面相同的三行（缩进按所在位置）。

`ChatLogWriter.amend_response` 里的 `run` 整个换成（只有 `task_id`，先按它查到这一行属于谁）：

```python
        async def run(session: AsyncSession) -> None:
            bot_id = await session.scalar(
                select(ChatLog.bot_id).where(ChatLog.task_id == task_id).limit(1)
            )
            stored = await sealed.seal_values(
                session, ChatLog, bot_id, {"response_content": text}
            )
            await session.execute(
                update(ChatLog)
                .where(ChatLog.task_id == task_id, ChatLog.status != CHAT_LOG_RUNNING)
                .values(response_content=stored["response_content"])
            )
```

- [ ] **Step 4: 读取还原**

`coreman/core/chat/session_carry.py`：导入段加 `from coreman.core.assistants import sealed`。`carried_context` 里 `rows = (...).all()` 之后加一行：

```python
    await sealed.open_rows(session, rows)
```

`coreman/core/chat/session_switch.py`：导入段加 `from coreman.core.assistants import sealed`。把预览那个循环换成：

```python
    for rs, content in firsts:
        content = await sealed.open_value(session, ChatLog, "message_content", bot_id, content)
        if rs is None or not content:
            continue
        preview.setdefault(rs, content[:PREVIEW_LEN])
```

（原来的判断是 `content is None`；查询本身已带 `message_content IS NOT NULL`，改成 `not content` 只多挡掉「密钥已销毁、还原成空」的行，普通机器人的结果不变——空串的预览本来也会回落到「非文本消息」。）

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_sealed_chat_logs.py tests/integration/test_chat_logs.py tests/integration/test_session_switch.py tests/integration/test_turn_log_lifecycle.py -q`
Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add coreman/core/chat/chat_logs.py coreman/core/chat/session_carry.py coreman/core/chat/session_switch.py tests/integration/test_assistant_sealed_chat_logs.py
git commit -m "个人助理：chat_logs 的消息与回复正文加密入库"
```

---

### Task 10: 散落的直接读取与入口台账

**Files:**
- Modify: `coreman/runtime/scheduler/reaper.py:127-128,192`
- Modify: `coreman/runtime/worker/chat/queued_batch.py:89`
- Modify: `coreman/runtime/worker/chat/feishu_quote.py:91-118,248-259`
- Modify: `coreman/runtime/worker/credential_cards.py:16,24-36`
- Test: `tests/integration/test_assistant_sealed_scattered.py`
- Test: `tests/unit/test_assistant_sealed_ledger.py`

**Interfaces:**
- Consumes: `sealed.open_row` / `open_rows` / `open_value` / `is_opened`；`SealedSession.scalar` / `get`。
- Produces: 行为不变的四处修补；台账测试 `REVIEWED_READS`、`WRITE_FILES`——以后新增一处取整行或写入，测试会失败，逼着决定要不要还原或封存。

**实现时的修正（以代码为准）：**
- 台账测试改为按语法树扫描，不用正则，并从两张表扩到四张：`ENTITY_SELECTS`（实体出现在 `select(...)` 任意位置的条数，正则只认第一个参数）、`COLUMN_REFERENCES`（语句里直接引用 `<模型>.<内容列>` 的次数——只取内容列、按内容列过滤、`returning` 内容列都算）、`RAW_STATEMENTS`（提到这六张表与内容列名的裸 SQL 条数）、`WRITE_STATEMENTS`（Task 13 起由文件集合改成按文件计数：在已登记的文件里新增一条写语句同样会失败）。内容列与表名取自 `sealed.COLUMNS`，以后登记新列会自动纳入。故意各加一处未登记的读、只取列、写、裸 SQL，四条用例全部失败，已验证后撤掉。
- 行为用例多两条：`reap_lost_tasks` 收尸（载荷里的会话信息与半截正文都要读到明文）、`queued_batch.gather` 合并排队消息；巡检类用例另开会话执行，拿到的才是库里读出、尚未还原的行。
- 扫描另外盘出、计划的表里没有的引用：`coreman/core/feishu_docs/access.py` 只取 `Task.payload` 一列后读 `doc_links`（明文键，封装顶层就有，不用还原）；`coreman/runtime/worker/busy_choice.py`、`coreman/core/chat/busy_choice.py` 按 `platform_user_id` 过滤（明文键）。都不需要改代码，已记入台账。

- [ ] **Step 1: 写失败测试（行为）**

`tests/integration/test_assistant_sealed_scattered.py`：

```python
"""bus 层之外直接读写内容的几处：巡检补流、引用回复取原文、凭证副本擦除。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import sealed
from coreman.core.bus import outbox, streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Bot, InboundEvent
from coreman.core.db.session import make_session_factory
from coreman.runtime.scheduler.reaper import recover_terminal_streams
from coreman.runtime.worker.chat import feishu_quote
from coreman.runtime.worker.credential_cards import wipe
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
MESSAGE = {"message_type": "text", "content": json.dumps({"text": SECRET}, ensure_ascii=False)}


async def _event(session: AsyncSession, bot: Bot, payload: dict[str, Any]) -> int:
    stored = await sealed.seal_values(session, InboundEvent, bot.id, {"payload": payload})
    event_id = await session.scalar(
        insert(InboundEvent)
        .values(
            bot_id=bot.id,
            platform="feishu",
            platform_msg_id="om_1",
            kind="message",
            chat_type="single",
            chat_id="oc_1",
            payload=stored["payload"],
            reply_context={},
        )
        .returning(InboundEvent.id)
    )
    return int(event_id)


async def test_recover_terminal_streams_carries_the_partial_text(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    task = await tasks.enqueue(
        db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="oc_1")
    )
    assert task is not None
    await streams.create(
        db_session,
        task_id=task.id,
        bot_id=bot.id,
        platform="feishu",
        stream_id="s1",
        reply_context={"chat_id": "oc_1"},
        lease_generation=1,
        running_since=datetime.now(UTC),
    )
    await streams.update(db_session, task.id, bot_id=bot.id, pending_text=f"半截：{SECRET}")
    await tasks.finish(db_session, task.id, status="succeeded")
    await db_session.execute(
        text("UPDATE tasks SET finished_at = now() - interval '5 minutes' WHERE id = :id"),
        {"id": task.id},
    )
    await db_session.commit()
    assert await recover_terminal_streams(db_session, datetime.now(UTC)) == 1
    await db_session.commit()
    stored = await raw(
        db_session, "SELECT final_text FROM task_streams WHERE task_id = :id", id=task.id
    )
    assert stored.startswith("enc:v2:d1:")
    async with make_session_factory(db_engine)() as other:
        row = await streams.get(other, task.id)
        assert row is not None and row.is_complete and row.final_text == f"半截：{SECRET}"


@pytest.mark.parametrize("seed", ["assistant", "standard"])
async def test_quote_reads_the_original_from_inbound_and_outbox(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, seed: str
) -> None:
    bot = (await seed_assistant(db_session))[0] if seed == "assistant" else await seed_standard(
        db_session
    )
    await _event(db_session, bot, {"raw": {"event": {"message": MESSAGE}}})
    item = await outbox.add(
        db_session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key="9:send:final",
        target={"chat_id": "oc_1"},
        payload={"markdown": f"答：{SECRET}"},
    )
    assert item is not None
    item.payload = {**item.payload, "_feishu_message_id": "om_2"}
    await outbox.mark_sent(db_session, item.id)
    await db_session.commit()
    monkeypatch.setattr(feishu_quote, "render", lambda kind, content, names=None: (kind, content))
    assert await feishu_quote._from_inbound(
        db_session, bot=bot, chat_id="oc_1", parent_id="om_1"
    ) == ("text", MESSAGE["content"])
    assert (
        await feishu_quote.persisted_text(db_session, bot=bot, chat_id="oc_1", parent_id="om_2")
        == f"答：{SECRET}"
    )


@pytest.mark.parametrize("seed", ["assistant", "standard"])
async def test_wipe_removes_the_sealed_copy(
    db_engine: AsyncEngine, db_session: AsyncSession, seed: str
) -> None:
    bot = (await seed_assistant(db_session))[0] if seed == "assistant" else await seed_standard(
        db_session
    )
    action = {"task_id": "cred:1", "sealed": "copy-of-the-submission"}
    event_id = await _event(db_session, bot, {"card_action": action})
    task = await tasks.enqueue(
        db_session,
        NewTask(bot_id=bot.id, kind="card_action", payload={"card_action": action}),
    )
    await db_session.commit()
    assert task is not None
    factory = make_session_factory(db_engine)
    async with factory() as session:
        await wipe(session, task.id, event_id)
        await session.commit()
    async with factory() as session:
        got = await tasks.get(session, task.id)
        event = await session.get(InboundEvent, event_id)
        assert got is not None and got.payload["card_action"] == {"task_id": "cred:1"}
        assert event is not None and event.payload["card_action"] == {"task_id": "cred:1"}
```

- [ ] **Step 2: 写失败测试（台账）**

`tests/unit/test_assistant_sealed_ledger.py`：

```python
"""六张内容表的读写入口台账。

个人助理的内容列在库里是密文。`session.get` / `session.scalar` 取整行会自动还原；
`session.scalars(select(<模型>))`、`session.execute(select(<模型>))` 不会，写入也只在 bus 层封存。
新增一处时这里会失败：先决定这一处要不要 `sealed.open_rows`（读）或 `sealed.seal_values`（写），
再更新台账。各处的处理方式见 docs/superpowers/plans/2026-11-11-personal-assistant-1c-encryption.md。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODELS = r"(?:Task|TaskStream|OutboxItem|ChatLog|InboundEvent|FeishuSentMessage)"
READ = re.compile(rf"select\(\s*{MODELS}\s*[,)]")
WRITE = re.compile(rf"(?:insert|update)\(\s*{MODELS}\s*\)|(?<![\w.]){MODELS}\(")

# 文件 → 取整行的 select 条数。
REVIEWED_READS = {
    "coreman/api/routers/chat_logs.py": 2,
    "coreman/api/routers/cron_jobs.py": 2,
    "coreman/api/routers/dev.py": 1,
    "coreman/api/routers/infra_notify.py": 1,
    "coreman/api/routers/infra_push.py": 1,
    "coreman/api/routers/runtime.py": 2,
    "coreman/api/routers/self_reminders.py": 1,
    "coreman/core/api_tasks/service.py": 1,
    "coreman/core/bus/outbox.py": 1,
    "coreman/core/bus/streams.py": 2,
    "coreman/core/bus/tasks.py": 3,
    "coreman/core/chat/bot_collaboration.py": 2,
    "coreman/core/chat/busy_choice.py": 1,
    "coreman/core/chat/collaboration_setup.py": 1,
    "coreman/core/chat/human_collaboration.py": 2,
    "coreman/core/chat/session_carry.py": 1,
    "coreman/core/escalations/media_recovery.py": 1,
    "coreman/core/escalations/service.py": 2,
    "coreman/core/knowledge/installation.py": 1,
    "coreman/runtime/gateway_common/inbound.py": 1,
    "coreman/runtime/gateway_feishu/reply_buttons.py": 1,
    "coreman/runtime/gateway_feishu/transport.py": 1,
    "coreman/runtime/scheduler/notifications.py": 1,
    "coreman/runtime/scheduler/reaper.py": 2,
    "coreman/runtime/worker/chat/api.py": 1,
    "coreman/runtime/worker/chat/feishu_quote.py": 2,
    "coreman/runtime/worker/chat/queued_batch.py": 1,
    "coreman/runtime/worker/choice_flow.py": 1,
    "coreman/runtime/worker/cron_handler.py": 1,
    "coreman/runtime/worker/escalation_media.py": 1,
    "coreman/runtime/worker/skill_install.py": 1,
}
# 允许出现 insert / update / 构造这五个模型的文件：都经过 sealed.seal_values，或只写元数据。
WRITE_FILES = {
    "coreman/core/api_tasks/service.py",
    "coreman/core/bus/outbox.py",
    "coreman/core/bus/streams.py",
    "coreman/core/bus/tasks.py",
    "coreman/core/chat/chat_logs.py",
    "coreman/runtime/gateway_common/inbound.py",
    # 已发消息的正文存档（insert(FeishuSentMessage)），Task 13 起经过 seal_values。
    "coreman/runtime/gateway_feishu/transport.py",
    "coreman/runtime/scheduler/reaper.py",
}


def _scan(pattern: re.Pattern[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for path in sorted((ROOT / "coreman").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith("coreman/core/db/models/"):
            continue
        count = len(pattern.findall(path.read_text(encoding="utf-8")))
        if count:
            found[relative] = count
    return found


def test_every_entity_select_is_reviewed() -> None:
    assert _scan(READ) == REVIEWED_READS


def test_writes_stay_in_the_sealing_layer() -> None:
    assert set(_scan(WRITE)) == WRITE_FILES
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_sealed_scattered.py tests/unit/test_assistant_sealed_ledger.py -v`
Expected: FAIL。`test_every_entity_select_is_reviewed` 缺 `feishu_quote.py`；`test_quote_reads_the_original_from_inbound_and_outbox[assistant]` 取到 `None`；`test_wipe_removes_the_sealed_copy[assistant]` 里 `sealed` 还在；`test_recover_terminal_streams_carries_the_partial_text` 的终稿不含原文。`[standard]` 两条与 `test_writes_stay_in_the_sealing_layer` 现在就通过。

- [ ] **Step 4: 巡检与合并排队消息**

`coreman/runtime/scheduler/reaper.py`：导入段加 `from coreman.core.assistants import sealed`。

`reap_lost_tasks` 里，`lost = list(...)` 这条语句之后加一行（后面要从载荷里取会话与消息信息写日志、通知用户）：

```python
    await sealed.open_rows(session, lost)
```

`recover_terminal_streams` 里，`for task, stream in rows:` 的循环体第一行加：

```python
        await sealed.open_rows(session, [task, stream])
```

密钥已销毁时这两处都不抛错：载荷只剩明文键、半截正文为空，补流走兜底文案，`streams.complete` 与 `outbox.add` 把内容丢弃后照常落库。

`coreman/runtime/worker/chat/queued_batch.py`：导入段加 `from coreman.core.assistants import sealed`。`gather` 里 `for task, received_at in rows.all():` 的循环体第一行加：

```python
            await sealed.open_row(session, task)
```

- [ ] **Step 5: 引用回复取原文**

`coreman/runtime/worker/chat/feishu_quote.py`：导入段加 `from coreman.core.assistants import sealed`。

`persisted_text` 里，`final_text = await session.scalar(select(TaskStream.final_text)...)` 这条语句之后加一行：

```python
    final_text = await sealed.open_value(session, TaskStream, "final_text", bot.id, final_text)
```

同一函数里取出站原文的那一段（从 `markdown = await session.scalar(` 到函数末尾的 `return`）换成取整行再读，正文不再由 SQL 从载荷里取：

```python
    sent = await session.scalar(
        select(OutboxItem)
        .where(
            OutboxItem.bot_id == bot.id,
            OutboxItem.platform == "feishu",
            OutboxItem.status == "sent",
            OutboxItem.target["chat_id"].astext == chat_id,
            OutboxItem.payload["_feishu_message_id"].astext == parent_id,
        )
        .order_by(OutboxItem.id.desc())
        .limit(1)
    )
    markdown = sent.payload.get("markdown") if sent is not None else None
    return markdown if isinstance(markdown, str) and markdown.strip() else None
```

`_from_inbound` 里，从 `message = await session.scalar(` 到 `if not isinstance(message, dict): return None` 之前换成：

```python
    event = await session.scalar(
        select(InboundEvent).where(
            InboundEvent.bot_id == bot.id,
            InboundEvent.platform == "feishu",
            InboundEvent.platform_msg_id == parent_id,
            InboundEvent.chat_id == chat_id,
        )
    )
    raw = event.payload.get("raw") if event is not None else None
    inner = raw.get("event") if isinstance(raw, dict) else None
    message = inner.get("message") if isinstance(inner, dict) else None
```

两处都是 `session.scalar(select(<模型>))`，由 `SealedSession` 自动还原。

- [ ] **Step 6: 凭证副本擦除**

`coreman/runtime/worker/credential_cards.py`（该文件里已有局部变量叫 `sealed`，模块换个名字导入）：

```python
from coreman.core.assistants import sealed as content_seal
from coreman.core.db.models import Bot, InboundEvent, Task
```

`wipe` 整个函数换成：

```python
async def wipe(session: AsyncSession, task_id: int, event_id: int | None) -> None:
    """封存副本用完即删：任务与入站事件按 90 天保留，不留这份密文。"""
    await session.execute(
        text("UPDATE tasks SET payload = payload #- '{card_action,sealed}' WHERE id = :id"),
        {"id": task_id},
    )
    if event_id is not None:
        await session.execute(
            text(
                "UPDATE inbound_events SET payload = payload #- '{card_action,sealed}' "
                "WHERE id = :id"
            ),
            {"id": event_id},
        )
    # 个人助理的载荷整体加密，上面两条语句碰不到里面的副本：还原后删掉，落库时重新封存。
    rows: list[Task | InboundEvent | None] = [await session.get(Task, task_id)]
    if event_id is not None:
        rows.append(await session.get(InboundEvent, event_id))
    for row in rows:
        if row is None or not content_seal.is_opened(row):
            continue
        action = row.payload.get("card_action")
        if isinstance(action, dict) and "sealed" in action:
            kept = {k: v for k, v in action.items() if k != "sealed"}
            row.payload = {**row.payload, "card_action": kept}
    await session.flush()
```

普通机器人的行 `is_opened` 为假，只走原来的两条语句。

- [ ] **Step 7: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_sealed_scattered.py tests/unit/test_assistant_sealed_ledger.py tests/integration/test_scheduler_reaper.py tests/integration/test_feishu_quote.py tests/integration/test_busy_merge.py tests/integration/test_credential_card_submit.py -q`
Expected: 全部通过。

- [ ] **Step 8: 提交**

```bash
git add coreman/runtime tests/integration/test_assistant_sealed_scattered.py tests/unit/test_assistant_sealed_ledger.py
git commit -m "个人助理：补齐 bus 层之外的内容读取，加读写入口台账"
```

---

### Task 11: 接口读取

**Files:**
- Modify: `coreman/api/routers/chat_logs.py:135-180,203-205,314-319`
- Modify: `coreman/api/routers/dev.py:163-167`
- Test: `tests/api/test_assistant_chat_logs.py`

**Interfaces:**
- Consumes: `sealed.open_row` / `open_rows` 的 `viewer` 参数、`sealed.is_locked`。
- Produces:
  - `GET /api/admin/chat-logs` 与 `GET /api/admin/chat-logs/{id}`：只对助理的所有者本人解密。读不出来（不是本人、密钥已销毁）的行，预览与正文为 `null`，并带 `"content_locked": true`；普通机器人的行不带这个键，输出与现在逐字段相同。
  - 运维接口 `GET /api/admin/runtime/tasks`、`/outbox` 不变：只返回元数据，不解密。
  - 按关键词搜索（`keyword`）搜不到助理记录的正文。

**实现时的修正（以代码为准）：**
- `dev.py` 的 `get_task` 不只还原对话记录：任务与流是 `session.get` 取的、已经自动还原，而 `task_out` / `stream_out` 会输出报错信息与思考、正文。所以对任务、流、对话记录三行一起 `open_rows(..., viewer=actor.id)`，不是所有者就把已还原的内容收回去。补一条用例。
- 接口用例的主密钥按现有助理接口用例的做法显式指定（`sealed.use_master(Cipher(MASTER_KEY))`，与 `api_settings` 配的是同一把），不用 `use_master(None)`。
- 多一条用例：按关键词搜不到助理记录的正文。

可见性规则不在本任务里改：飞书私聊的记录本来就只有本人可见（`_scope`）。`viewer` 是第二道闸——哪怕某条助理记录因为类型不同（如定时任务）出现在别人的列表里，接口也不会为他解密。

- [ ] **Step 1: 写失败测试**

```python
"""对话记录接口：所有者读到明文；其他人与密钥销毁之后读不到；运维接口不含内容。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.bus import outbox, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import chat_logs
from coreman.core.chat.chat_logs import ChatLogEntry
from coreman.core.db.models import Bot
from tests.api.conftest import login_as, login_existing
from tests.integration.assistant_helpers import SECRET, seed_assistant, seed_standard


@pytest.fixture(autouse=True)
def _fresh_caches() -> Iterator[None]:
    # 主密钥用 api_settings 配的那一把（get_settings），不指定覆盖。
    sealed.use_master(None)
    keys.forget()
    sealed.forget()
    yield
    keys.forget()
    sealed.forget()


def _entry(bot: Bot, user_id: uuid.UUID | None, **over: object) -> ChatLogEntry:
    base: dict[str, object] = dict(
        bot_id=bot.id,
        bot_key=bot.bot_key,
        platform="feishu",
        chat_type="single",
        message_type="text",
        status="success",
        request_at=datetime.now(UTC),
        user_id=user_id,
        chat_id="oc_1",
        session_key="oc_1",
        task_id=1,
        message_content=f"问：{SECRET}",
        response_content=f"答：{SECRET}",
    )
    base.update(over)
    return ChatLogEntry(**base)  # type: ignore[arg-type]


async def _items(client: httpx.AsyncClient) -> list[dict]:  # type: ignore[type-arg]
    response = await client.get("/api/admin/chat-logs")
    assert response.status_code == 200, response.text
    return list(response.json()["data"]["items"])


async def test_owner_reads_plaintext(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id))
    await db_session.commit()
    await login_existing(client, db_session, owner)
    items = await _items(client)
    assert len(items) == 1 and items[0]["message_preview"] == f"问：{SECRET}"
    assert "content_locked" not in items[0]
    detail = (await client.get(f"/api/admin/chat-logs/{items[0]['id']}")).json()["data"]
    assert detail["message_content"] == f"问：{SECRET}"
    assert detail["response_content"] == f"答：{SECRET}"


async def test_nobody_else_gets_the_content(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id))
    # 一条不按私聊规则隐藏的记录（定时任务类型）：管理员看得到这一行，但内容不为他解密。
    assert await chat_logs.insert_turn(
        db_session, _entry(bot, None, chat_type="cron", task_id=4242)
    )
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    items = await _items(client)
    assert [i["chat_type"] for i in items] == ["cron"]
    assert items[0]["content_locked"] is True
    assert items[0]["message_preview"] is None and items[0]["response_preview"] is None
    response = await client.get(f"/api/admin/chat-logs/{items[0]['id']}")
    assert SECRET not in response.text and "enc:v2:" not in response.text
    assert response.json()["data"]["message_content"] is None


async def test_owner_sees_locked_after_key_destroyed(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id))
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    await login_existing(client, db_session, owner)
    items = await _items(client)
    assert items[0]["content_locked"] is True and items[0]["message_preview"] is None
    detail = await client.get(f"/api/admin/chat-logs/{items[0]['id']}")
    assert detail.status_code == 200 and SECRET not in detail.text
    assert detail.json()["data"]["response_content"] is None


async def test_standard_bot_output_is_unchanged(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot = await seed_standard(db_session)
    viewer = await login_as(client, db_session, role="platform_admin")
    assert await chat_logs.insert_turn(
        db_session, _entry(bot, viewer.id, platform="wecom")
    )
    await db_session.commit()
    items = await _items(client)
    assert items[0]["message_preview"] == f"问：{SECRET}" and "content_locked" not in items[0]


async def test_runtime_pages_carry_no_content(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    await tasks.enqueue(
        db_session,
        NewTask(bot_id=bot.id, kind="chat", payload={"message": SECRET}, session_key="oc_1"),
    )
    await outbox.add(
        db_session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key="1:send:final",
        target={"chat_id": "oc_1"},
        payload={"markdown": SECRET},
    )
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    for path in (
        "/api/admin/runtime/tasks?status=queued",
        "/api/admin/runtime/outbox?status=pending",
    ):
        response = await client.get(path)
        assert response.status_code == 200 and len(response.json()["data"]) == 1
        assert SECRET not in response.text and "_sealed" not in response.text
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_assistant_chat_logs.py -v`
Expected: FAIL。`test_owner_reads_plaintext` 的预览是密文串；`test_nobody_else_gets_the_content` 没有 `content_locked`。`test_standard_bot_output_is_unchanged` 与 `test_runtime_pages_carry_no_content` 现在就通过（回归护栏）。

- [ ] **Step 3: 实现**

`coreman/api/routers/chat_logs.py`：导入段加 `from coreman.core.assistants import sealed`。

`chat_log_out`：在 `return data` 之前加（`full` 分支之后）：

```python
    if sealed.is_locked(row):
        # 个人助理的内容读不出来：不是所有者本人，或数据密钥已销毁。
        data["content_locked"] = True
```

`list_chat_logs`：`rows: list[ChatLog] = page["items"]` 之后加一行：

```python
    await sealed.open_rows(session, rows, viewer=actor.id)
```

`get_chat_log`：`raise not_found("对话记录不存在")` 那个 `if` 之后加一行：

```python
    await sealed.open_row(session, row, viewer=actor.id)
```

读不出来的行，`open_row` 已把三个正文列置为 `None`，`_preview` / `_readable` 对 `None` 原样返回，其余输出代码不用改。

`coreman/api/routers/dev.py`：导入段加 `from coreman.core.assistants import sealed`。`get_task` 里取 `log` 的那条语句（`).scalar_one_or_none()`）之后加一行：

```python
    await sealed.open_row(session, log, viewer=actor.id)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/api/test_assistant_chat_logs.py tests/api/test_chat_logs.py -v`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/api/routers/chat_logs.py coreman/api/routers/dev.py tests/api/test_assistant_chat_logs.py
git commit -m "个人助理：对话记录接口只对所有者解密"
```

---

### Task 12: 主密钥轮换后重新包裹数据密钥

**Files:**
- Modify: `coreman/core/assistants/keys.py`（追加 `rewrap`）
- Modify: `coreman/cli.py`（模块说明、导入、`_assistant_keys_rewrap`、`main`）
- Modify: `docs/operations.md:69-71`
- Test: `tests/integration/test_assistant_keys_rewrap.py`

**Interfaces:**
- Consumes: Task 2 的 `keys.wrap_aad`、`keys.forget`；`coreman.core.crypto.key_id_of`。
- Produces:
  - `async keys.rewrap(session: AsyncSession, cipher: Cipher) -> tuple[int, int]`：返回（已由当前主密钥包裹的行数，本次重新包裹的行数）；调用方决定提交还是回滚。
  - 命令 `python -m coreman.cli assistant-keys rewrap [--apply]`：不加 `--apply` 只统计。

**实现时的修正（以代码为准）：** 多一条用例直接跑命令函数：不加 `--apply` 只统计、库里不变，加了才改由当前主密钥包裹。

- [ ] **Step 1: 写失败测试**

```python
"""主密钥轮换：数据密钥重新包裹后不再依赖旧主密钥，内容不重写。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys
from coreman.core.crypto import Cipher, key_id_of
from coreman.core.db.models import AssistantKey, User

OLD = b"\x05" * 32
NEW = b"\x06" * 32


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    keys.forget()
    yield
    keys.forget()


async def test_rewrap_moves_keys_to_the_current_master(db_session: AsyncSession) -> None:
    owners = [User(login_name=f"u{i}", display_name="张三") for i in range(3)]
    db_session.add_all(owners)
    await db_session.flush()
    old = Cipher(OLD)
    for owner in owners:
        await keys.ensure_key(db_session, old, owner.id)
    await keys.destroy_key(db_session, owners[2].id)
    await db_session.commit()
    token = (await keys.data_cipher(db_session, old, owners[0].id)).encrypt("机密", "t.c:x")
    rotated = Cipher(NEW, key_id="k2", previous={"k1": OLD})
    assert await keys.rewrap(db_session, rotated) == (0, 2)
    await db_session.commit()
    rows = (await db_session.execute(select(AssistantKey))).scalars().all()
    assert {key_id_of(r.dek_enc) for r in rows if r.dek_enc} == {"k2"}
    # 重新包裹只换外层：代数不变，内容密文里的 d1 仍然对得上。
    assert {r.generation for r in rows} == {1}
    # 再跑一次什么都不用做。
    assert await keys.rewrap(db_session, rotated) == (2, 0)
    # 旧主密钥移除之后仍读得出，内容密文没有动。
    only_new = Cipher(NEW, key_id="k2")
    assert (await keys.data_cipher(db_session, only_new, owners[0].id)).decrypt(
        token, "t.c:x"
    ) == "机密"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_keys_rewrap.py -v`
Expected: FAIL，`AttributeError: module 'coreman.core.assistants.keys' has no attribute 'rewrap'`。

- [ ] **Step 3: 实现 `rewrap`**

`coreman/core/assistants/keys.py`：把 `from coreman.core.crypto import Cipher` 改成 `from coreman.core.crypto import Cipher, key_id_of`，文件末尾追加：

```python
async def rewrap(session: AsyncSession, cipher: Cipher) -> tuple[int, int]:
    """主密钥轮换之后：把不是当前主密钥包裹的数据密钥重新包裹一遍。

    内容密文只认数据密钥，不用动。返回（已是当前主密钥的行数，本次重新包裹的行数）；
    调用方提交才生效。全部跑完后，旧主密钥才能从 `MASTER_KEYS_PREVIOUS` 里移除。
    """
    rows = (
        (
            await session.execute(
                select(AssistantKey).where(AssistantKey.dek_enc.is_not(None)).with_for_update()
            )
        )
        .scalars()
        .all()
    )
    current = changed = 0
    for row in rows:
        assert row.dek_enc is not None
        if key_id_of(row.dek_enc) == cipher.key_id:
            current += 1
            continue
        aad = wrap_aad(row.user_id)
        row.dek_enc = cipher.encrypt(cipher.decrypt(row.dek_enc, aad), aad)
        changed += 1
    await session.flush()
    forget()
    return current, changed
```

- [ ] **Step 4: 管理命令**

`coreman/cli.py`：模块说明里 `rules-backup` 那一段之后加一段：

```python
`assistant-keys rewrap [--apply]`：轮换 MASTER_KEY 之后，把个人助理的数据密钥改由新主密钥包裹，
见 core.assistants.keys。
```

导入段加：

```python
from coreman.core.assistants import keys as assistant_keys
```

在 `def _preview_texts(` 之前加：

```python
async def _assistant_keys_rewrap(apply: bool) -> int:
    """把个人助理的数据密钥改由当前主密钥包裹；不加 --apply 只统计、不落库。"""
    settings = get_settings()
    configure_logging(
        service="cli", instance="assistant-keys", level=settings.log_level, stream=sys.stderr
    )
    engine = make_engine(settings.database_url, command_timeout=None)
    try:
        async with make_session_factory(engine)() as session:
            current, changed = await assistant_keys.rewrap(session, settings.build_cipher())
            if apply:
                await session.commit()
            else:
                await session.rollback()
    finally:
        await engine.dispose()
    print(json.dumps({"current": current, "rewrapped" if apply else "stale": changed}))
    return 0
```

`main` 里，`cards = sub.add_parser("feishu-cards", help="飞书富卡片")` 之前加：

```python
    akeys = sub.add_parser("assistant-keys", help="个人助理数据密钥")
    akeys_ops = akeys.add_subparsers(dest="operation", required=True)
    rewrap = akeys_ops.add_parser("rewrap", help="主密钥轮换后重新包裹数据密钥")
    rewrap.add_argument("--apply", action="store_true", help="真正写入；不加只统计")
```

`if args.command == "rules-backup":` 之前加：

```python
    if args.command == "assistant-keys" and args.operation == "rewrap":
        return asyncio.run(_assistant_keys_rewrap(args.apply))
```

- [ ] **Step 5: 运维说明**

`docs/operations.md`「轮换 MASTER_KEY」一节，在「只有确认库里不再有某个 kid 的密文时，才能把它从 `MASTER_KEYS_PREVIOUS` 中移除」这一段之后加一段：

```markdown
个人助理的数据密钥（`assistant_keys`）写一次就不再改，不会随写入自然迁移。重启全部服务后执行一次
`python -m coreman.cli assistant-keys rewrap`（只统计，输出里的 `stale` 是还由旧主密钥包裹的把数），
确认无误再加 `--apply`。助理的对话内容由数据密钥加密，轮换主密钥不需要重写它们。
```

- [ ] **Step 6: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_keys_rewrap.py tests/integration/test_assistant_keys.py tests/unit/test_cli.py -v`
Expected: 全部通过。

- [ ] **Step 7: 全量验证并提交**

Run: `uv run pytest -q`
Expected: 全部通过。

Run: `uv run ruff check . && uv run mypy coreman && uv run alembic check`
Expected: 均无报错。

```bash
git add coreman/core/assistants/keys.py coreman/cli.py docs/operations.md tests/integration/test_assistant_keys_rewrap.py
git commit -m "个人助理：主密钥轮换后重新包裹数据密钥"
```

---

### Task 13: 报错信息、附件信息与已发消息存档

设计文档的加密范围表只列了五张表的正文列。盘点时发现助理记录还有四处内容以明文落库：`tasks.error_message`、`chat_logs.error_message`（处理器的异常文字，可能带出消息片段）、`chat_logs.file_info`（附件文件名）、`feishu_sent_messages.text`（机器人发出的每条飞书消息的正文存档）。这四列没有任何 SQL 过滤、聚合或告警读取（见「载荷里被 SQL 直接读取的字段」表末两行），全部封存。`chat_logs.file_info` 与整行写入的 `error_message` 已随 Task 3 的登记和 Task 9 的写函数生效；本任务补齐剩下单独写、单独读的入口。

**Files:**
- Modify: `coreman/core/bus/tasks.py`（`finish`，在 Task 5 的改动之上）
- Modify: `coreman/core/chat/chat_logs.py:238-269`（`close_running`）
- Modify: `coreman/runtime/scheduler/reaper.py:222-255`（`close_abandoned_turns`）
- Modify: `coreman/runtime/gateway_feishu/transport.py:205-232`（`remember`）
- Modify: `coreman/runtime/worker/chat/feishu_quote.py:81-89`（`persisted_text` 读存档）
- Test: `tests/integration/test_assistant_sealed_extras.py`

**Interfaces:**
- Consumes: Task 3 的 `COLUMNS`（已含 `Task.error_message`、`ChatLog.file_info` / `error_message`、`FeishuSentMessage.text`）；`sealed.seal_values`、`sealed.open_value`；Task 5 的 `tasks.finish(..., bot_id=None)`；Task 7 起 `transport.py` 已导入 `sealed`，Task 10 起 `reaper.py`、`feishu_quote.py` 已导入 `sealed`。
- Produces: 这四列对助理记录是密文；读取处拿到明文。`error_code`、`status`、`work_code`、`chat_id` 保持明文。`coreman/core/chat/group_observe.py:229` 读存档只发生在群聊旁听里，助理不涉及，不改。

**实现时的修正（以代码为准）：**
- 步骤与代码按计划实现，用例照搬。`tasks.finish` 与 `close_running` 在调用方没给 `bot_id` 时按主键查归属的那条语句，由随后的「调用方传入 bot_id」一笔去掉（见文末「普通机器人路径上的开销」）。
- `tasks.finish` 是 Core 语句，默认会把写入的值同步到会话里已有的任务对象上：同一会话里结束之后不重新取行、直接读 `task.result` / `task.error_message`，读到的是封存后的值。盘点过现有代码没有这样读的（都是结束前读，或 `session.get` 重新取，重新取会自动还原）；以后新增时要先 `tasks.get`。

- [ ] **Step 1: 写失败测试**

```python
"""五张表正文列之外的内容：报错信息、附件信息、已发消息的正文存档。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import chat_logs
from coreman.core.chat.chat_logs import ChatLogEntry
from coreman.core.db.models import Bot, ChatLog
from coreman.core.db.session import make_session_factory
from coreman.runtime.gateway_feishu.transport import FeishuTransport
from coreman.runtime.scheduler.reaper import close_abandoned_turns
from coreman.runtime.worker.chat import feishu_quote
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
ERROR = f"RuntimeError: 处理「{SECRET}」时出错"


async def _task(session: AsyncSession, bot: Bot) -> int:
    task = await tasks.enqueue(
        session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="oc_1")
    )
    await session.commit()
    assert task is not None
    return task.id


def _entry(bot: Bot, user_id: uuid.UUID | None, task_id: int, **over: object) -> ChatLogEntry:
    base: dict[str, object] = dict(
        bot_id=bot.id,
        bot_key=bot.bot_key,
        platform="feishu",
        chat_type="single",
        message_type="file",
        status="error",
        request_at=datetime.now(UTC),
        user_id=user_id,
        chat_id="oc_1",
        session_key="oc_1",
        task_id=task_id,
        message_content="看一下这个文件",
        file_info={"filename": f"{SECRET}.pdf", "size": 12},
        error_code="handler_crashed",
        error_message=ERROR,
    )
    base.update(over)
    return ChatLogEntry(**base)  # type: ignore[arg-type]


async def _log_text(session: AsyncSession, task_id: int) -> str:
    return str(
        await raw(
            session,
            "SELECT concat_ws('|', file_info::text, error_message, error_code)"
            " FROM chat_logs WHERE task_id = :id",
            id=task_id,
        )
    )


async def test_task_error_message_is_sealed(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    task_id = await _task(db_session, bot)
    long_error = ERROR + "x" * 6000
    assert await tasks.finish(
        db_session, task_id, status="failed", error_code="handler_crashed", error_message=long_error
    )
    await db_session.commit()
    stored = str(
        await raw(
            db_session,
            "SELECT error_code || '|' || error_message FROM tasks WHERE id = :id",
            id=task_id,
        )
    )
    assert stored.startswith("handler_crashed|enc:v2:d1:") and SECRET not in stored
    async with make_session_factory(db_engine)() as other:
        got = await tasks.get(other, task_id)
        # 截断按明文长度做，在加密之前。
        assert got is not None and got.error_message == long_error[:5000]


async def test_chat_log_file_info_and_error_are_sealed(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    task_id = await _task(db_session, bot)
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id, task_id))
    await db_session.commit()
    stored = await _log_text(db_session, task_id)
    assert SECRET not in stored and stored.endswith("|handler_crashed")
    row = await db_session.scalar(select(ChatLog).where(ChatLog.task_id == task_id))
    assert row is not None and row.error_message == ERROR
    assert row.file_info == {"filename": f"{SECRET}.pdf", "size": 12}


async def test_close_running_seals_the_error(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    task_id = await _task(db_session, bot)
    opening = _entry(bot, owner.id, task_id, error_code=None, error_message=None)
    assert await chat_logs.open_turn(db_session, opening)
    await db_session.commit()
    assert await chat_logs.close_running(
        db_session, task_id, status="error", error_code="handler_crashed", error_message=ERROR
    )
    await db_session.commit()
    assert SECRET not in await _log_text(db_session, task_id)
    row = await db_session.scalar(select(ChatLog).where(ChatLog.task_id == task_id))
    assert row is not None and row.status == "error" and row.error_message == ERROR
    assert row.message_content == "看一下这个文件"


async def test_reaper_copies_the_task_error_into_the_log(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    task_id = await _task(db_session, bot)
    opening = _entry(bot, owner.id, task_id, error_code=None, error_message=None)
    assert await chat_logs.open_turn(db_session, opening)
    await tasks.finish(
        db_session, task_id, status="failed", error_code="handler_crashed", error_message=ERROR
    )
    await db_session.execute(
        text("UPDATE tasks SET finished_at = now() - interval '1 day' WHERE id = :id"),
        {"id": task_id},
    )
    await db_session.commit()
    assert await close_abandoned_turns(db_session, datetime.now(UTC)) == 1
    await db_session.commit()
    assert SECRET not in await _log_text(db_session, task_id)
    row = await db_session.scalar(select(ChatLog).where(ChatLog.task_id == task_id))
    assert row is not None and row.status == "error" and row.error_code == "handler_crashed"
    assert row.error_message == ERROR


@pytest.mark.parametrize("seed", ["assistant", "standard"])
async def test_sent_message_archive(
    db_engine: AsyncEngine, db_session: AsyncSession, seed: str
) -> None:
    owner = None
    if seed == "assistant":
        bot, owner = await seed_assistant(db_session)
    else:
        bot = await seed_standard(db_session)
    # remember 只用到 factory 与 bot_id，不需要一条真的飞书连接。
    transport = SimpleNamespace(factory=make_session_factory(db_engine), bot_id=bot.id)
    await FeishuTransport.remember(transport, "om_5", "oc_1", f"答：{SECRET}", work_code="W-1")  # type: ignore[arg-type]
    stored = str(
        await raw(
            db_session,
            "SELECT work_code || '|' || text FROM feishu_sent_messages WHERE message_id = 'om_5'",
        )
    )
    if owner is None:
        assert stored == f"W-1|答：{SECRET}"
    else:
        assert stored.startswith("W-1|enc:v2:d1:") and SECRET not in stored
    assert (
        await feishu_quote.persisted_text(db_session, bot=bot, chat_id="oc_1", parent_id="om_5")
        == f"答：{SECRET}"
    )
    if owner is not None:
        # 销毁之后引用这条消息：存档读不出来，按「没有存档」处理。
        await keys.destroy_key(db_session, owner.id)
        await db_session.commit()
        assert (
            await feishu_quote.persisted_text(
                db_session, bot=bot, chat_id="oc_1", parent_id="om_5"
            )
            is None
        )


async def test_standard_bot_errors_stay_plain(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    task_id = await _task(db_session, bot)
    await tasks.finish(db_session, task_id, status="failed", error_message=ERROR)
    assert await chat_logs.insert_turn(db_session, _entry(bot, None, task_id))
    await db_session.commit()
    assert await raw(
        db_session, "SELECT error_message FROM tasks WHERE id = :id", id=task_id
    ) == ERROR
    stored = await _log_text(db_session, task_id)
    assert SECRET in stored and "enc:v2:" not in stored and "_sealed" not in stored
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_sealed_extras.py -v`
Expected: FAIL。`test_task_error_message_is_sealed`、`test_close_running_seals_the_error`、`test_reaper_copies_the_task_error_into_the_log`、`test_sent_message_archive[assistant]` 四条里库中仍是明文。`test_chat_log_file_info_and_error_are_sealed`（Task 9 起已生效）、`test_sent_message_archive[standard]`、`test_standard_bot_errors_stay_plain` 现在就通过。

- [ ] **Step 3: `tasks.finish` 封存报错信息**

`coreman/core/bus/tasks.py` 的 `finish`：把 Task 5 加在函数体开头的那段（`if result is not None:` 起四行）换成：

```python
    message = error_message[:5000] if error_message else None
    content: dict[str, Any] = {}
    if result is not None:
        content["result"] = result
    if message is not None:
        content["error_message"] = message
    if content:
        if bot_id is None:
            bot_id = await session.scalar(select(Task.bot_id).where(Task.id == task_id))
        stored = await sealed.seal_values(session, Task, bot_id, content)
        result, message = stored.get("result"), stored.get("error_message")
```

并把 `.values(...)` 里的 `error_message=error_message[:5000] if error_message else None,` 改成：

```python
                error_message=message,
```

截断在加密之前按明文长度做。没带 `result` 也没带 `error_message` 的结束（最常见的成功路径之一）不查库。`claim` 里对不可读任务写的那句固定说明，在密钥已销毁时会被丢弃成 `NULL`，`error_code='content_locked'` 仍在。

- [ ] **Step 4: `close_running` 与巡检**

`coreman/core/chat/chat_logs.py` 的 `close_running`：`async with session.begin_nested():` 块整个换成：

```python
        async with session.begin_nested():
            message = error_message[:limit] if error_message else None
            if message:
                # 只有 task_id：先查到这一行属于谁，再决定要不要封存。
                bot_id = await session.scalar(
                    select(ChatLog.bot_id)
                    .where(ChatLog.task_id == task_id, ChatLog.status == CHAT_LOG_RUNNING)
                    .limit(1)
                )
                message = (
                    await sealed.seal_values(session, ChatLog, bot_id, {"error_message": message})
                )["error_message"]
            closed = await session.scalar(
                update(ChatLog)
                .where(ChatLog.task_id == task_id, ChatLog.status == CHAT_LOG_RUNNING)
                .values(
                    status=status,
                    error_code=error_code,
                    error_message=message,
                    latency_ms=elapsed_ms(),
                )
                .returning(ChatLog.id)
            )
```

`coreman/runtime/scheduler/reaper.py` 的 `close_abandoned_turns`：查询的列表改成多取一列 `bot_id`：

```python
            select(ChatLog.id, ChatLog.bot_id, Task.status, Task.error_code, Task.error_message)
```

循环（从 `for log_id, task_status, error_code, error_message in rows:` 到那条 `update(ChatLog)` 语句结束）换成：

```python
    for log_id, bot_id, task_status, error_code, error_message in rows:
        code: str | None
        message: str | None
        if task_status is None:
            status, code, message = "error", "task_missing", "任务记录已不存在（如机器人已删除）"
        else:
            status = TASK_TO_LOG_STATUS.get(task_status, "error")
            # 这里只取了任务的一列：先还原成明文，再按对话记录这一列重新封存（两列的 AAD 不同）。
            plain = await sealed.open_value(session, Task, "error_message", bot_id, error_message)
            code, message = error_code, plain[:5000] if plain else None
        stored = await sealed.seal_values(session, ChatLog, bot_id, {"error_message": message})
        await session.execute(
            update(ChatLog)
            .where(ChatLog.id == log_id)
            .values(
                status=status,
                error_code=code,
                error_message=stored["error_message"],
                latency_ms=chat_logs.elapsed_ms(),
            )
        )
```

- [ ] **Step 5: 已发消息的正文存档**

`coreman/runtime/gateway_feishu/transport.py` 的 `remember`：`async with self.factory() as session:` 块里的那条 `await session.execute(...)` 换成：

```python
                stored = await sealed.seal_values(
                    session, FeishuSentMessage, self.bot_id, values
                )
                await session.execute(
                    insert(FeishuSentMessage)
                    .values(bot_id=self.bot_id, message_id=message_id, **stored)
                    .on_conflict_do_update(
                        index_elements=[FeishuSentMessage.bot_id, FeishuSentMessage.message_id],
                        set_=stored,
                    )
                )
```

`bounded(text)` 的截断在加密之前；`chat_id`、`work_code` 不是内容列，原样写入。密钥已销毁时存下的是空串，引用时按没有存档处理。

`coreman/runtime/worker/chat/feishu_quote.py` 的 `persisted_text`：`archived = await session.scalar(select(FeishuSentMessage.text)...)` 这条语句之后加一行：

```python
    archived = await sealed.open_value(session, FeishuSentMessage, "text", bot.id, archived)
```

- [ ] **Step 6: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_sealed_extras.py tests/integration/test_assistant_sealed_tasks.py tests/integration/test_assistant_sealed_chat_logs.py tests/unit/test_assistant_sealed_ledger.py tests/integration/test_scheduler_reaper.py tests/integration/test_turn_log_lifecycle.py tests/integration/test_feishu_quote.py tests/integration/test_feishu_transport.py -q`
Expected: 全部通过。

- [ ] **Step 7: 全量验证并提交**

Run: `uv run pytest -q`
Expected: 全部通过。

Run: `uv run ruff check . && uv run mypy coreman && uv run alembic check`
Expected: 均无报错。

```bash
git add coreman/core/bus/tasks.py coreman/core/chat/chat_logs.py coreman/runtime tests/integration/test_assistant_sealed_extras.py
git commit -m "个人助理：报错信息、附件信息与已发消息存档加密入库"
```

---

## 交付后的状态与遗留

- 助理记录在六张表里的内容列是密文：五张表的正文列，加上 `tasks.error_message`、`chat_logs.error_message`、`chat_logs.file_info`、`feishu_sent_messages.text`、`task_streams.activity` 的说明文字。`destroy_key` 之后永久不可读，各入口的行为见「密钥已销毁时各入口的行为」。
- 1F 停用流程调用 `keys.destroy_key(session, owner_id)`；重新开通时调用 `keys.ensure_key(session, cipher, owner_id)` 签发下一代密钥（本计划已实现）。旧一代的内容按已锁定处理，新内容正常。
- 设计文档第二节需要同步改三处：AAD 写法改成「表名.列名:bot_id」；`assistant_keys` 加 `generation` 与重新签发；加密范围表补上上面四列。
- 仍以明文落库、本期不做的内容：
  - `cron_jobs.prompt` / `cron_runs.prompt` 及执行结果：助理的定时任务属于第 2 期，届时一并处理。
  - 各表的 `error_code`、`status`、`chat_logs.user_name` / `user_login`、`outbox.target`、`reply_context`：元数据，供调度、告警与排障使用。`outbox.last_error`、`tasks.cancel_reason` 同样不加密，但助理的行只留开头的代码（见「载荷里被 SQL 直接读取的字段」）。
  - **`feishu_deliveries.layout`（交付前盘出，未处理）**：飞书网关流式推卡片时把思考预览（最后五行）、各段正文的文本、任务清单存在这一列（`coreman/runtime/gateway_feishu/transport.py` 的流式推送与 `push_plan`，`coreman/core/feishu_cards/stream.py` 的 `StreamUnit.record`），助理的每一次流式回复都会经过，明文保留到流被清理（结束后约一小时）。这张表没有 `bot_id` 列，flush 钩子管不到，需要单独设计（按流的 `bot_id` 封存，或只存摘要不存文本）。同表的 `last_error` 存的是飞书错误说明。
  - 六张表之外带内容的表，助理用到相应功能时会明文落库：`interaction_states.state`（提问卡片的题目与选项）、`human_collaborations` / `bot_collaborations` 的 `question` / `response`、`api_tasks.prompt`、`work_tasks`、`memories`。本期不涉及，各功能接入助理时逐个决定。
  - 日志流：`outbox._note_permanent_failure` 把报错原文的前 200 字写进 `outbox_dependents_released` 告警，助理的条目没有改写。
- 真机未验证：本计划只有自动化测试；端到端在 1F。

## 自查记录

- 设计第二节逐条对应：密钥表与包裹（Task 1、2）、内容加密与 AAD（Task 3、4，AAD 有偏差已注明）、主密钥轮换（Task 2、4 的用例 + Task 12）、五张表（Task 5–9）、销毁（Task 2 + 各表用例）、运维接口只返回元数据（Task 11）。
- 评审后补充：设计范围之外的四列（Task 13，盘点结果是四列都能封存，没有需要留明文的）；密钥代数与重新签发（Task 1、2、3、4、12）；flush 钩子先在 asyncpg 上验证及退路（Task 4 Step 0）。
- 类型与名字前后一致：`keys.ensure_key(session, cipher, owner_id, *, reissue=True)`、`keys.data_key_id(generation)`、`seal_values(session, model, bot_id, values)`、`open_row(session, row, *, viewer=None)`、`open_rows`、`open_value(session, model, column, bot_id, value)`、`is_locked`、`is_opened`、`LOCKED_CODE`、`keys.data_cipher(session, cipher, owner_id)`、`keys.destroy_key(session, owner_id)`。
- 台账里的条数是按 Task 10 改完之后的代码数的（`feishu_quote.py` 由 0 变 2，其余与写计划时相同）；开工合并 `main` 后若有出入，先按「加解密所在的层」的规则判断新增的那一处，再改台账。
