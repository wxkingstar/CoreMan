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
6. **下班编译。** spec 说下班时派一个子任务，但子任务属于第 3 期。裁定：2F 先做成主会话里的一轮平台来源任务（类型 `assistant_compile`），3A 落地后改为子任务。代价：编译那一轮占住主会话几分钟。2F 落地的做法：每位助理每个自然日至多一轮，下班后由 scheduler 的 `_assistant_shift_loop` 入队，那一轮不做主会话的每日换新。
7. **话题回复是否让群靠前（spec「前置验证」第 1 项）还没测。** 裁定：2D 两条路都做——按会话列表顺序发现新动静，另对所有者标记的重点群每轮直接读历史；实测留到上线后，在真实租户里做，结论写回 spec。代价：重点群每轮多一次读历史调用，计入分钟预算。
8. **限频。** 现状完全没处理。裁定：2D 新增按应用的两类接口分钟预算（列会话、读历史），识别限频错误码与响应头里的等待秒数；预算与退避只作用于轮询循环，助理工具的调用不受它限制但计入用量。「单个应用承载不下时分到下一个应用」不做，接近上限时只拉长间隔并打告警日志。代价：人数多到一个应用撑不住时需要再做分流。
9. **调度公共员工的加密。** spec 要求目标员工那一轮的内容用所有者的密钥加密，现有的按人加密按行所属员工找所有者，公共员工的行一律明文。裁定留给 3B 的计划在读完那条链路后给出两个方案的工作量，再定；在此之前 3B 不动工。
10. **导入。** spec 说管理命令、由所有者本人执行，但管理命令是运维在服务器上跑的。裁定：做成 `/api/me/assistant/import` 上传接口（Markdown 压缩包），所有者本人登录后操作，并写访问日志。
11. **助理池开关在仍有助理绑定时能否关闭**（spec 标「待定」）：维持现状（能关，绑定的助理暂停），不在这两期处理。

12. **`PLATFORM_TURN_KINDS` 的取法**（2A 计划提出）：`(HANDOVER_KIND, JOB_RESULT_KIND, COMPILE_KIND, SUBTASK_KIND)`；`BOT_HELP_KIND` 跑在目标员工那边，不属于它，留给 3B。没有入站事件的审批结果回合也经 `platform_turns.enqueue` 入队（`platform_turns.ORIGIN_KINDS`）。2F 另加 `COLD_START_KIND`（见裁定 18）。
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
17. **调度公共员工的存储与放行**（所有者 2026-11-13 定，取代裁定 9）：
    - 采用方案乙（见 `2026-11-13-personal-assistant-3b-options.md`）：目标员工那一轮照普通私聊的方式明文存在它名下，标为私有，只有所有者本人能看到内容，管理员与该员工的其他成员只看到元数据。spec 的威胁模型与第八节相应改写：这部分内容不随所有者的数据密钥销毁。
    - 无人在场的轮次发起只读调度不需要审批；防范放在助理这一层：提示词规则要求只把完成任务必需、合理的问题交出去，不带原始聊天记录，发现来件里有诱导外传的内容就不调度并记待跟进；平台侧对问题设长度上限并记审计。`allow_write=true` 仍先过审批（目标系统 `assistant-dispatch`）。
    - 资格按现有口径（目标员工开启接受求助，且所有者在它的可用人范围内），不另要求私聊过。
    - 批准后不单独投一轮「已批准并派出」，只投被拒、过期、失败与最终答复。
18. **冷启动的口吻样本是一种新的平台来源类型**（2F 计划提出）：`kinds.COLD_START_KIND = "assistant_cold_start"`，并入 `PLATFORM_TURN_KINDS`。它跑在独立会话 `<主会话键>#job:cold-start` 里，工具组是新加的 `voice`（只有 `assistant_voice_save`，这一轮不带任何个人工具、授权工具、知识库工具与定时任务工具），输入只有所有者自己发出的消息；平台经 `voice.save` 把结果写成置顶页 `profile/voice.md`。原因：置顶页只能由所有者在场的轮次改动，口吻页要在无人在场时生成，只能靠「这一轮读不到别人的任何东西」来守同一条线。代价：多一种任务类型与一个工具组。
19. **子任务层的补充**（3A 落地时定）：
    - 工具分组多一个 `subtask`（只有子任务轮次有，里面是 `assistant_job_blocked`）；`GROUPS` 仍是普通轮次的三组。`knowledge`、`jobs`、`subtask`、`voice` 四组的数据都在平台自己的库里，排在授权代数判断之前（`tools.LOCAL_GROUPS`）。
    - 子任务轮次不出声。回复上下文带 `_silent`：飞书网关不推这条流，流恢复不补发。看护器 `TimeoutSupervisor.silent`：进度、完成、超时通知都不发。处理器覆盖掉长任务提醒、主动模式的终稿、选择卡（题面留在结论里）、额度切换卡（终稿是额度告示时按失败 `rate_limited` 收尾，不写 `interaction_states`）、「会话忙」提示。收尸按任务类型（`kinds.SILENT_KINDS`）不往会话里发「进程没了」。3B 的不出声轮次复用这三样：`_silent`、`supervisor.silent`、`SILENT_KINDS`。
    - 顺带收紧了一切平台来源的轮次（不只子任务）：认领之后才遇到运行时暂停的，没有入站事件就不私信（裁定 13 原来只管开轮前那一次检查）；收尸补写对话记录时，没有入站事件的平台来源轮次不记所有者触达（`user_reached`）。
    - 轮数超限在追问处拒绝（`job_round_limit`），不把已有结论的子任务改成失败；时长按每次派出或追问起 2 小时。
    - 主会话自己取消的、停用时取消的、`report = false` 的子任务不投回。
    - 下班编译改为平台派出的子任务（裁定 6 的后半）：`compiling.step` 调 `jobs.spawn(origin="system", report=False, attended=False)`，每天一次仍靠 `compiled_on`；子任务没有 `approval` 组，被拒与已执行的审批单的摘要与拒绝原因由平台写进交代（引用行，交代至多 8000 字，放不下丢最早的并注明张数）；正常做完时由子任务的收尾挂钩调 `compiling.settle` 把活动窗口推进到派出时刻。`AssistantCompileHandler` 与它的注册已删除，`COMPILE_KIND` 的名字保留（任务类型的取值约束、历史记录），不再入队：升级时队列里的旧任务由 worker 按未知类型结掉，那一天的活动留给下一次编译。不保留处理器一版的理由：它的文本生成与主会话里跑的做法都已改掉，留着就是两套编译并存，而丢一次编译的代价只是晚一天整理。
    - 本人定时任务工具对一切无人在场的轮次都不列、调用返回 `owner_turn_required`（2F 之后的修补已做），子任务轮次不另作处理。
    - 冷启动的口吻样本那一轮同样不出声（审查后补）：`COLD_START_KIND` 进了 `kinds.SILENT_KINDS`。处理器层的那几处覆盖提到了基类 `worker.platform_turn.SilentTurnHandler`，`AssistantSubtaskHandler` 与 `AssistantColdStartHandler` 都继承它；3B 的不出声轮次直接继承这个基类，并把任务类型加进 `SILENT_KINDS`。

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
- 表 `assistant_watch`（主键 `bot_id`）：`cursor_enc`（JSON：每个会话读到的位置；迁移 `0101` 起与 `head_enc` 一样封存，不能在 SQL 里按内容过滤）、`head_enc`、`interval`、`next_poll_at`、`state`（`idle`/`polling`/`backoff`/`paused`/`stopped`）、`last_error`、`last_ok_at`。
- 表 `assistant_inbox`：见 spec 第五节，另加 `kind`（`message`/`approval_todo`/`reminder`）、`hold_until`、`delivered_task_id`。
- `coreman/core/assistants/watch.py`：`tick(factory, cipher, now)`（scheduler 新循环 `_assistant_watch_loop` 每 5 秒调一次，认领到期的行）、`grade(item, settings, context) -> Level`、`stop(session, bot_id)`（停用时调用）。

### 2E

- `coreman/core/assistants/delivery.py`：`due_items(session, bot_id, now)`、`dispatch(session, bot, now) -> Task | None`（有可交回的条目且没有待处理的交回任务时入队一个 `assistant_handover`）、`settle(session, task, ok)`（轮次结束后记账）、`budget_left(session, bot, today) -> int`。
- worker `AssistantHandoverHandler(PlatformTurnHandler)`。
- 设置项 `assistant_daily_turn_budget`（默认 200，范围 10–2000）。

### 2F

- 迁移 `0099_assistant_cold_start`：`assistant_settings` 加 `cold_start_started_at`、`cold_start_step`（`voice`/`people`/`duties`/`focus`/`hours`）、`cold_start_next_at`、`cold_start_outcome`（`completed`/`skipped`/`timeout`）、`worked_on`、`compiled_on`、`compiled_at`。
- `coreman/core/assistants/settings.py`：`view(row, now) -> dict`、`update(session, bot_id, *, now, timezone=None, work_start=None, work_end=None, workdays=None, focus_chats=None, ignored_chats=None) -> AssistantSettings`（`SettingsError(code)`）、`clock_in(row, now) -> datetime`、`clock_out(row, now) -> datetime`、`override(row, now) -> str | None`、`cold_start_state(row) -> str`。3C 的设置表单直接调 `view` / `update`。
- 工具 `assistant_settings_get`、`assistant_settings_update`，注册在 `knowledge` 组，无人在场的轮次返回 `owner_turn_required`。
- 私聊命令「上班」「下班」「跳过」（与「停用助理」同一处截下，`coreman/runtime/worker/assistant_shift.py`）。
- `coreman/core/assistants/cold_start.py`：`begin(session, bot_id, now) -> bool`（授权完成、置 `active` 的同一个事务里调用，不阻塞开通）、`complete(session, bot, row, outcome, now) -> bool`（写 `cold_start_done_at`）、`tick(factory, cipher, now)`、`stop(session, bot_id)`；完成前 `watch` 与 `delivery` 不动。
- `coreman/core/assistants/compiling.py`：`tick(factory, now)`；`COMPILE_KIND` 的处理器 `AssistantCompileHandler`；`COLD_START_KIND` 的处理器 `AssistantColdStartHandler`。
- scheduler 循环 `_assistant_shift_loop`（5 秒，先 `cold_start.tick` 后 `compiling.tick`）。

### 3A

- 表 `assistant_jobs`：见 spec 第七节。3B 用 `kind = 'bot_help'` 与 `target_bot_id`；结果投回共用 `report` / `result_task_id` / `report_seq` / `report_after` / `report_failures` / `reported_at` / `report_code` 这一组列与 `jobs.report` / `settle_report` / `reconcile`。`origin = 'system'` 目前只有下班编译（`compiling.settle` 按它判断）。
- 子任务会话键 `"<chat_id>#job:<id>"`（`jobs.session_key_for` / `job_id_of`；`job_id_of` 对冷启动的 `#job:cold-start` 返回 None）；工具范围 = 个人工具的读类 + `knowledge` 组 + `subtask` 组。
- 工具 `assistant_spawn`、`assistant_job_message`、`assistant_job_list`、`assistant_job_cancel`，注册在 `jobs` 组；`assistant_job_blocked` 注册在 `subtask` 组。
- 结果以 `JOB_RESULT_KIND` 投回主会话（经 `platform_turns.enqueue`，载荷只有 `job_id`）；处理器 `AssistantJobResultHandler` 的文本是 `jobs.result_text(row)`。
- `coreman/core/assistants/jobs.py` 对外：`spawn`、`message`、`list_`（`scope`：`active` / `blocked` / `recent`）、`get`、`cancel`、`cancel_all`、`complete`、`block`、`for_round`、`mark_running`、`round_text`、`result_text`、`report`、`bound_report`、`settle_report`、`reconcile`、`tick`、`active_counts`、`view(row, full=False)`、`JobError`。
- 不出声：回复上下文标记 `_silent`、`TimeoutSupervisor.silent`、`kinds.SILENT_KINDS`（见裁定 19）；处理器层的覆盖在基类 `SilentTurnHandler`（`coreman/runtime/worker/platform_turn.py`）。处理器 `AssistantSubtaskHandler` 在 `coreman/runtime/worker/assistant_jobs.py`。

### 3B、3C、3D

接口在各自的计划里定；3C 的接口前缀 `/api/me/assistant`，鉴权照 `feishu_personal.interactive_user`。

## 每层都要守的约束

- 新的内容表必须有 `bot_id` 列，登记进 `sealed.COLUMNS`、`tests/unit/test_assistant_sealed_ledger.py` 的四张表、`tests/conftest.py` 的 `BUSINESS_TABLES`、`coreman/core/db/models/__init__.py`。
- 以所有者身份调飞书时，每位助理每轮一个短事务：刷新令牌的锁要到事务提交才释放。
- 轮询与投递的循环必须过滤 `bots.enabled` 与 `assistant_state = 'active'`，并尊重 `assistant_runtime.paused`；暂停期间不重复私信。
- 不把飞书的原始错误文本交给模型；日志里不出现消息内容（`sealed.guard_logs`）。
- 验证命令：`uv run pytest <路径>`、`uv run ruff check .`、`uv run ruff format --check .`、`uv run mypy coreman`、`uv run alembic check`。
- 文档：每层更新 `docs/feishu.md`「个人助理应用」与 `CHANGELOG.md`；3C 另更新 `docs/operations.md`。
