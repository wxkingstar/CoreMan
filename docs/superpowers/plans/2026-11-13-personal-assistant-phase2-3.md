# 个人助理第 2、3 期：分层总览

设计文档：`docs/superpowers/specs/2026-11-11-personal-assistant.md`（下称 spec）。第 1 期总览：`2026-11-11-personal-assistant-phase1.md`。
本文定分层、层间接口和对 spec 的裁定；每层另有自己的实现计划（`2026-11-13-personal-assistant-2a-*.md` 等），按本文的接口写。

集成分支 `claude/personal-assistant-phase2-3`，各层依次提交在这一条分支上；迁移从 `0094` 起顺延。

## 分层

| 层 | 内容 | 依赖 | 迁移 |
|---|---|---|---|
| 2A 基础 | 助理设置表、平台来源的轮次（不带入站事件也能拿到个人工具）、新任务类型、助理工具的分组与分派位置 | — | `0094_assistant_settings` |
| 2B 知识库 | `assistant_pages`、`notes_*` 工具、会话首轮注入 | 2A | `0095_assistant_pages` |
| 2C 待跟进 | `assistant_followups`、`followup_*` 工具 | 2A | `0096_assistant_followups` |
| 2D 盯消息 | 以所有者身份调飞书的限频、`assistant_watch` 轮询循环、分级、`assistant_inbox` | 2A | `0097_assistant_watch` |
| 2E 投递 | `assistant_handover` 轮次、排队合并、每日换新会话、每日轮次预算、盯消息顺带的检查（撤回审批、表情挂起、到期待跟进、授权到期提醒） | 2B 2C 2D | `0098_assistant_delivery` |
| 2F 上下班与冷启动 | 工作时段启停、「上班」「下班」、下班编译、开通后的冷启动 | 2E | `0099_assistant_cold_start` |
| 3A 子任务 | `assistant_jobs`、`assistant_spawn`、`assistant_job_message`、只读工具范围、结果投回 | 2E | `0100_assistant_jobs` |
| 3B 调度公共员工 | `assistant_ask_bot`、目标员工那边的私有轮次、`assistant-dispatch` 审批 | 3A | `0101_assistant_dispatch` |
| 3C 页面 | `/api/me/assistant`、「我的助理」页、管理台的助理列表与设置项表单 | 2C 2E 3A | — |
| 3D 导入与访问日志 | `assistant_access_log`、导入接口 | 2B 2C 3C | `0102_assistant_access_log` |

## 裁定（与 spec 不同或 spec 没写明的地方）

每条写明：裁定、原因、错了的代价。

1. **平台来源的轮次。** spec 没说平台自己发起的轮次怎么拿到个人工具。现状：`policy.verified_origin_scope` 要求任务带一条真实的私聊入站事件。裁定：新增「平台来源」——任务类型属于 `kinds.PLATFORM_TURN_KINDS`、员工是启用中的助理、`task.user_id` 是所有者、`task.session_key` 属于这位助理（主会话键或它派生的子任务键）时，scope 由 `assistant_settings` 里记下的身份（`open_id`、`tenant_key`、`platform_user_id`、`chat_id`）与授权行组成，不需要入站事件。这些身份在开通时由飞书给的操作人写入，不来自任何请求内容。只有平台代码能创建这些类型的任务（网关入库只产生 `chat`）。原因：交回事项没有「所有者刚发的那条消息」可复用。代价：放宽了一道校验；靠「任务类型只能由平台创建」兜住，2A 要为此写台账式测试（扫 `NewTask(kind=…)` 的出处）。
2. **审批结果的投回不改。** 仍复用提交那一轮的入站事件；平台来源的轮次里提交的审批单没有入站事件，`origin_event_id` 为空时改走平台来源（2A 同时改 `approvals.service.deliver`）。
3. **排队与合并。** 现状：所有者一发消息，同会话其它活动任务被取消。裁定：收件箱条目只有在交回轮次**正常结束**后才记为已交回；被取消或失败的轮次，条目回到待交回，下次再带上。同一位助理同一时刻至多一个待处理的交回任务，新条目不另开任务，由下一轮一并取走。原因：不改通用的取消语义。代价：所有者频繁发消息时交回被推迟；可接受，因为他本人正在场。
4. **知识页注入的位置。** spec 要求注入「稳定前缀之后」并在此之前把提示词改成经文件传递。裁定：置顶页与索引放进**会话首轮**的本轮块（经标准输入传，不进命令行），之后的轮次只在置顶页有变更时补一条变更说明。会话每天换新，所以每天注入一次。原因：不动节点驱动和提示词传递方式，命令行里始终没有个人内容，对话前缀稳定、缓存不受影响。代价：注入内容进了当天的转录（转录本来就按人加密、每轮清出节点）；当天内置顶页被改写后，旧版本仍留在上下文里，靠变更说明纠正。
5. **每日换新会话。** 现状是按空闲时长换。裁定：助理的主会话按所有者时区的自然日换新：开轮时发现会话的开始日期早于今天就换；子任务会话不受影响。时区与工作时段存在 `assistant_settings`。
6. **下班编译。** spec 说下班时派一个子任务，但子任务属于第 3 期。裁定：2F 先做成主会话里的一轮平台来源任务（类型 `assistant_compile`），3A 落地后改为子任务。代价：编译那一轮占住主会话几分钟。
7. **话题回复是否让群靠前（spec「前置验证」第 1 项）还没测。** 裁定：2D 两条路都做——按会话列表顺序发现新动静，另对所有者标记的重点群每轮直接读历史；实测留到上线后，在真实租户里做，结论写回 spec。代价：重点群每轮多一次读历史调用，计入分钟预算。
8. **限频。** 现状完全没处理。裁定：2D 新增按应用的两类接口分钟预算（列会话、读历史），识别限频错误码与响应头里的等待秒数；预算与退避只作用于轮询循环，助理工具的调用不受它限制但计入用量。「单个应用承载不下时分到下一个应用」不做，接近上限时只拉长间隔并打告警日志。代价：人数多到一个应用撑不住时需要再做分流。
9. **调度公共员工的加密。** spec 要求目标员工那一轮的内容用所有者的密钥加密，现有的按人加密按行所属员工找所有者，公共员工的行一律明文。裁定留给 3B 的计划在读完那条链路后给出两个方案的工作量，再定；在此之前 3B 不动工。
10. **导入。** spec 说管理命令、由所有者本人执行，但管理命令是运维在服务器上跑的。裁定：做成 `/api/me/assistant/import` 上传接口（Markdown 压缩包），所有者本人登录后操作，并写访问日志。
11. **助理池开关在仍有助理绑定时能否关闭**（spec 标「待定」）：维持现状（能关，绑定的助理暂停），不在这两期处理。

12. **`PLATFORM_TURN_KINDS` 的取法**（2A 计划提出）：`(HANDOVER_KIND, JOB_RESULT_KIND, COMPILE_KIND, SUBTASK_KIND)`；`BOT_HELP_KIND` 跑在目标员工那边，不属于它，留给 3B。没有入站事件的审批结果回合也经 `platform_turns.enqueue` 入队（`platform_turns.ORIGIN_KINDS`）。
13. **暂停时平台来源的轮次静默失败**（2A 计划提出）：没有入站事件的轮次遇到运行时暂停，不私信，任务记失败（`assistant_paused`）；带入站事件的回合行为不变。
14. **比 spec 多出的列与上限**（2B、2C 计划提出）：`assistant_pages.size`（正文字节数，算合计上限用）；索引注入上限 16KB；`assistant_followups.closed_at`、`close_code`。spec 的同步随各层最后一个任务做。
15. **平台来源的轮次无人在场、输入来自他人，能力要收紧**（2A 审查提出）：
    - 不挂云签名代理、业务系统目录与所有者的个人凭证；需要时由所有者在私聊里亲自让助理做。
    - 授权工具只保留查询状态，不提供发起授权与撤销授权。
    - 开轮时不取消同会话的其它任务，而是让路：同会话有所有者的消息在排队或在跑时，自己放回队列。
    - 任务入队处加运行时闸：没有入站事件的平台来源类型，只有 `platform_turns.enqueue` 能入队。
    - 入站事件已被清理的审批结果，回落到平台来源投回。
    这些在 2A 的修补提交里落地，2E 之后的各层按此假定。
    - 后续补入两点：「无人在场」按任务类型判断（`kinds.unattended(kind)`，即类型属于 `kinds.ORIGIN_KINDS`；`platform_turns.unattended(task)` 与 `Scope.unattended` 都落到它），带入站事件的审批结果回合同样让路、同样收紧、同样动不了置顶页，`Scope.platform`（无入站事件 → 身份取自设置、会话围栏）含义不变；无人在场的轮次不下发以所有者身份签发的业务系统令牌（`BOT_TOKEN_*`）、业务系统账号与令牌配置，也不挂业务系统 MCP，机器人自己配的静态环境变量不动。新增的收紧一律用 `unattended` 判断，不要再用 `is_platform` 或 `scope.platform`。

## 层间接口

各层计划必须原样使用这些名字；需要改名时先改本文。

### 2A

- 表 `assistant_settings`（主键 `bot_id`，随记录删除）：`open_id`、`tenant_key`、`platform_user_id`、`chat_id`（开通时写入，明文）；`timezone`（IANA 名，默认 `Asia/Shanghai`）、`work_start`/`work_end`（`HH:MM`，默认 `09:30`/`19:00`）、`workdays`（整数数组，1–7，默认 1–5）；`focus_chats_enc`、`ignored_chats_enc`（加密的会话 id 列表）；`daily_turn_budget`（可空，空则用平台默认）；`shift_override`（`on`/`off`/空）与 `shift_override_until`；`cold_start_done_at`；`updated_at`。
- `coreman/core/assistants/settings.py`：`get(session, bot_id) -> AssistantSettings`（没有就按默认值建）、`local_today(settings, now) -> date`、`on_shift(settings, now) -> bool`。
- `coreman/core/assistants/kinds.py` 新增：`HANDOVER_KIND = "assistant_handover"`、`JOB_RESULT_KIND = "assistant_job_result"`、`COMPILE_KIND = "assistant_compile"`、`SUBTASK_KIND = "assistant_subtask"`、`BOT_HELP_KIND = "assistant_bot_help"`、`PLATFORM_TURN_KINDS`（前四个加 `SUBTASK_KIND`）。
- `coreman/core/assistants/platform_turns.py`：`enqueue(session, bot, kind, payload, *, session_key=None, dedupe_key) -> Task | None`（唯一允许创建这些类型任务的入口）；`scope_for(session, bot, task) -> Scope`（平台来源的 scope）。
- `policy.capability_scope` 对平台来源的任务改走 `platform_turns.scope_for`；`PRIVATE_TURN_KINDS` 并入 `PLATFORM_TURN_KINDS`。
- `coreman/core/assistants/tools.py`：助理自有工具的总注册表，`register(group, definitions, dispatch)`，分三组：`knowledge`（不受授权代数影响，子任务可用）、`approval`（现有的审批与表情工具）、`jobs`（子任务不可用）。MCP 路由按组分派；`definitions(base, *, groups)` 给不同轮次裁剪工具范围。
- worker 处理器基类 `PlatformTurnHandler(ChatTaskHandler)`：发言者固定为所有者，消息文本由子类给出，不需要入站内容。

### 2B

- 表 `assistant_pages`：见 spec 第六节，另加 `bot_id` 外键与 `updated_by`（`owner`/`assistant`/`import`）。
- `coreman/core/assistants/notes.py`：`index`、`read`、`search`、`write`、`delete`，以及 `pinned_block(session, bot_id) -> str | None`、`changes_since(session, bot_id, since) -> str | None`。上限 2000 页、合计 20MB、单页 200KB。
- 工具 `notes_index`、`notes_read`、`notes_search`、`notes_write`、`notes_delete`，注册在 `knowledge` 组。

### 2C

- 表 `assistant_followups`：见 spec 第六节。
- `coreman/core/assistants/followups.py`：`add`、`update`、`snooze`、`close`、`list_`，以及 `due(session, bot_id, today) -> list[AssistantFollowup]`、`close_for_message(session, bot_id, message_id)`。
- 工具 `followup_add`、`followup_update`、`followup_snooze`、`followup_close`、`followup_list`，注册在 `knowledge` 组。

### 2D

- `coreman/core/feishu_personal/ratelimit.py`：`Budget`（按应用、按接口类别的分钟计数，存库以便多进程共享）、`RateLimited(retry_after)`；`service._http` 保留响应头并在限频时抛它。
- 表 `assistant_watch`（主键 `bot_id`）：`cursor`（JSON：每个会话读到的位置）、`head_enc`、`interval`、`next_poll_at`、`state`（`idle`/`polling`/`backoff`/`paused`/`stopped`）、`last_error`、`last_ok_at`。
- 表 `assistant_inbox`：见 spec 第五节，另加 `kind`（`message`/`approval_todo`/`reminder`）、`hold_until`、`delivered_task_id`。
- `coreman/core/assistants/watch.py`：`tick(factory, cipher, now)`（scheduler 新循环 `_assistant_watch_loop` 每 5 秒调一次，认领到期的行）、`grade(item, settings, context) -> Level`、`stop(session, bot_id)`（停用时调用）。

### 2E

- `coreman/core/assistants/delivery.py`：`due_items(session, bot_id, now)`、`dispatch(session, bot, now) -> Task | None`（有可交回的条目且没有待处理的交回任务时入队一个 `assistant_handover`）、`settle(session, task, ok)`（轮次结束后记账）、`budget_left(session, bot, today) -> int`。
- worker `AssistantHandoverHandler(PlatformTurnHandler)`。
- 设置项 `assistant_daily_turn_budget`（默认 200，范围 10–2000）。

### 2F

- 私聊命令「上班」「下班」（与「停用助理」同一处截下）。
- 开通步骤新增 `cold_start`（在 `await_grant` 之后、置 `active` 之前不阻塞：先置 `active`，冷启动作为一轮 `assistant_handover` 之外的平台来源任务跑，完成后写 `cold_start_done_at`；完成前 `watch` 不开始）。
- `COMPILE_KIND` 的处理器。

### 3A

- 表 `assistant_jobs`：见 spec 第七节。
- 子任务会话键 `"<chat_id>#job:<id>"`；工具范围 = 个人工具的读类 + `knowledge` 组。
- 工具 `assistant_spawn`、`assistant_job_message`、`assistant_job_list`，注册在 `jobs` 组。
- 结果以 `JOB_RESULT_KIND` 投回主会话（经 `platform_turns.enqueue`）。

### 3B、3C、3D

接口在各自的计划里定；3C 的接口前缀 `/api/me/assistant`，鉴权照 `feishu_personal.interactive_user`。

## 每层都要守的约束

- 新的内容表必须有 `bot_id` 列，登记进 `sealed.COLUMNS`、`tests/unit/test_assistant_sealed_ledger.py` 的四张表、`tests/conftest.py` 的 `BUSINESS_TABLES`、`coreman/core/db/models/__init__.py`。
- 以所有者身份调飞书时，每位助理每轮一个短事务：刷新令牌的锁要到事务提交才释放。
- 轮询与投递的循环必须过滤 `bots.enabled` 与 `assistant_state = 'active'`，并尊重 `assistant_runtime.paused`；暂停期间不重复私信。
- 不把飞书的原始错误文本交给模型；日志里不出现消息内容（`sealed.guard_logs`）。
- 验证命令：`uv run pytest <路径>`、`uv run ruff check .`、`uv run ruff format --check .`、`uv run mypy coreman`、`uv run alembic check`。
- 文档：每层更新 `docs/feishu.md`「个人助理应用」与 `CHANGELOG.md`；3C 另更新 `docs/operations.md`。
