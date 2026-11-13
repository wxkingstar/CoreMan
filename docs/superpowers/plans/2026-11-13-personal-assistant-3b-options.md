# 个人助理 3B：调度公共 AI 员工的方案比较

设计文档：`docs/superpowers/specs/2026-11-11-personal-assistant.md` 第八节（下称 spec）。分层总览：`2026-11-13-personal-assistant-phase2-3.md` 裁定 9、15。前置层：`2026-11-13-personal-assistant-3a-jobs.md`。

本文只做比较与推荐，不是实现计划。写作时分支上迁移头是 `0098`，2F、3A 还没有代码（`_silent`、`assistant_jobs`、`jobs.py` 只存在于 3A 的计划里），下文凡引用它们都按 3A 计划的写法。

## 结论

1. **推荐方案丙（乙的变体）：目标员工那一轮不落正文。** 问题与答复只存在 `assistant_jobs` 的密文列里（3A 已有，按助理的 `bot_id` 封存，加密层一行不改）；目标员工名下的 `tasks`、`task_streams`、`chat_logs` 只留元数据，`inbound_events`、`outbox` 不产生行。库里没有明文可看，也就不需要「按行另指定所有者」。工作量约 10 个任务，比方案乙多 1 个，不到方案甲的六成。
2. **方案甲不值得做。** 它要给 5 张以上的公共表加所有者列、改 `sealed` 的归属判断与约 15 处写入口、重审台账里约 100 条读写登记，全部落在普通员工的热路径上；换来的只是「拿到数据库的人看不到这一轮」，而同样的内容在目标员工的节点上（转录、工作目录、文件记忆）始终是明文，甲、乙、丙都盖不住。
3. **方案乙可行但留下一份多余的明文。** 可见性几乎不用做：飞书私聊的对话记录现有规则就是「只有本人能看」。但明文在库里、不随所有者密钥销毁，spec 的威胁模型要开例外。丙用一个任务的代价把这个例外消掉。
4. **有一个不分方案的问题比加密更要紧：不经审批的调度是助理沙箱的出口。** 公共员工不在沙箱里、可以任意出网、带着所有者的业务令牌。被注入的助理可以把内容写进 `question`，让公共员工带出去，或让它以所有者身份改业务数据（`allow_write=false` 在平台侧无法强制）。建议：无人在场的轮次里 `assistant_ask_bot` 一律先过审批；所有者在场的轮次按 spec。见「不分方案的问题」第 2、4 条，需要用户裁定。

## 现状要点（读代码所得）

- **加密按 `bot_id` 找所有者。** `sealed.owner_of(session, bot_id)` 查 `bots.kind/owner_id`，进程内缓存；`seal_values`、`open_row`、`open_value`、flush 钩子 `_reseal`、`code_for_assistants`、`guard_logs`、`routing_context` 全都由此判断。AAD 是「表.列:bot_id」。普通员工的行在这些入口第一步就原样返回。
- **台账** `tests/unit/test_assistant_sealed_ledger.py` 登记了约 50 个文件的整行查询、25 个文件的内容列引用、4 个文件的裸 SQL、22 个文件的写语句。其中不少条目的理由是「助理没有这个功能」（定时任务、群协作、话题、旁听）或「只读状态」。
- **一轮对话写到的表**：`inbound_events`（载荷、回复上下文）、`tasks`（载荷、结果、报错）、`task_streams`（思考、正文、终稿、卡片、计划、最近动作）、`chat_logs`（消息、引用、回复、附件信息、报错）、`outbox`（流式之外的每条消息：长任务提醒、选择卡、主动模式终稿、会话忙、收尸补发）、`feishu_sent_messages` 与 `feishu_deliveries`（网关推流时）、`interaction_states`（选择卡等待答案）、`credential_requests`（凭证表单）、`cloud_pending_approvals`（云服务写操作）、`business_token_issues`（签发流水，元数据）、`chat_sessions`（只有会话坐标）。
- **谁能看到公共员工的轮次**：
  - `coreman/api/routers/chat_logs.py` 的 `_scope`：飞书私聊（`platform='feishu' AND chat_type='single'`）与 `private=true` 的记录只对 `user_id` 本人放行，管理员也不行；机器人令牌一律看不到。列表、统计、详情三个接口共用它。
  - `coreman/api/routers/dev.py` 的任务详情：飞书私聊的任务对非本人返回「不存在」。
  - `coreman/api/routers/runtime_nodes.py` 的会话查看页（从节点读转录）：飞书私聊或 `private` 的会话只对本人开放，管理员得到「不存在」。
  - `coreman/api/routers/runtime.py` 的任务与出站队列列表：只出元数据（含 `session_key`、`cancel_reason`、`last_error`）。
  - `coreman/api/routers/statistics.py`、`relay_agent.py`：只做计数与 token 汇总。
  - 节点上的工作目录文件与文件记忆（`bot_workspace.py`、`bot_memories.py`）：该员工的管理者可读。
- **`ChatLog.private`** 现在只在挂了企微个人工具的轮次为真（`ctx.private_turn`）。
- **员工间求助的 helper 阶段**（`coreman/core/chat/bot_collaboration.py`、`coreman/runtime/worker/chat/collaboration.py`）：以原始提问人的身份在 `collaboration:<id>` 独立会话里跑；回复上下文带 `_collaboration_helper`，网关不推；提示词要求终稿是纯 JSON `{"status": "completed"|"blocked", "answer": …}`，`read_helper_result` 解析；个人工具、定时任务、凭证索取的各个 policy 都按载荷里的 `collaboration_phase` 拒绝。但它依赖一条真实的群 @ 消息作入站事件，往返都经飞书群。

## 不分方案的问题

### 1. 在目标员工那边开一轮且不往飞书发任何东西，用什么

| 现成机制 | 贴近之处 | 差什么 |
|---|---|---|
| helper 阶段 | 语义最近：独立会话、以原提问人身份、不推流、结构化的 completed / blocked、个人工具全部不挂 | 入站事件必须是群里的真实 @ 消息；绑着 `bot_collaborations` 与路由表；答复经出站箱发回群里 |
| API 任务（`coreman/core/api_tasks/service.py`、`worker/chat/api.py`） | 「以某人身份、按私聊口径开一轮」，开轮前把权限重查一遍（`_reason`） | 落一条合成入站事件（明文载荷）；先往会话里发一句「某人通过 API 发起」；回复照常发到飞书；要求员工开了 API 驱动、本人私聊过它 |
| 平台来源轮次的替身（`worker/platform_turn.py`） | 入站事件不落库，会话坐标由平台给 | 只认助理：`_resolve` 要求 `is_assistant(bot)`，`platform_turns.enqueue` 要求会话键属于这位助理；替身的坐标取自 `assistant_settings` |
| 3A 的 `_silent` | 五处出声点（流式、长任务提醒、主动模式、选择卡、收尸与流恢复）都按它或按任务类型堵上 | 收尸那一处只认 `SUBTASK_KIND`，要加 `BOT_HELP_KIND`；处理器覆盖的几个方法写在助理的处理器上，目标员工那边要再写一份 |

做法：新处理器 `AssistantBotHelpHandler(ChatTaskHandler)`，自己的 `_resolve` 照 `api.resolve` 的形状重查权限、照 `stand_in` 造一个不落库的入站替身（会话坐标用合成值 `assistant-help:<job_id>`，`chat_type='single'`），开流时回复上下文带 `_silent`，提示词与解析复用 helper 的结构化答复。`_silent` 可以原样复用，3A 交接里的三条成立；另外任务入队要在 `platform_turns.enqueue` 之外开第二个入口（它拒绝非助理的记录），`tests/unit/test_assistant_platform_turn_ledger.py` 要认这个入口。

### 2. 「以所有者的身份执行，权限与所有者私聊它时相同」具体是什么

| 项 | 代码位置 | 这一轮的取法 |
|---|---|---|
| 发言者 | `resolve_speaker` → `Intake.speaker` | 所有者的飞书身份；开轮前与收尾时各核一次仍是同一人、账号可用 |
| 业务系统令牌与账号 | `build_system_access(speaker=…)`、`record_issues(user_id=…)` | 按所有者签发，流水记在所有者名下 |
| 个人凭证 | `worker/chat/credentials.py` 的 `configure` / `withhold` | 所有者在**这位员工**名下存过的照常注入；索取入口不给（见第 3 条） |
| 云签名代理 | `cloud_proxy.configure(user_id=…)` | 见下面的取舍 |
| 可用人白名单 | `IntakeStage._denied`、`api_tasks.service.allowed_to_use` | 调用工具时查一次，开轮前再查一次 |
| 用量 | `log_entry`：`chat_logs.bot_id` 是目标员工、`user_id` 是所有者 | 按员工的统计记在目标员工，按人的统计记在所有者；额度消耗在目标员工的节点。助理的每日轮次预算数的是助理名下的 `chat_logs`，数不到这一轮，由 3A 的「每天 20 个」与「同时 3 个」约束 |
| 飞书个人工具、企微个人工具、个人定时任务、飞书文档授权 | 各自的 policy | **不挂。** 这些 policy 要求真实的私聊入站事件；更重要的是目标员工若挂着所有者的飞书个人授权，调度就成了绕过审批闸门的写通道 |

所以「与所有者私聊它时相同」要收窄成「业务系统权限与已存的个人凭证相同；一切需要所有者当场点卡片的能力不提供」。spec 第八节要照此改一句。

**与裁定 15 的冲突。** 裁定 15 规定无人在场的轮次不下发以所有者身份签发的令牌。但无人在场的助理轮次（交回事项，输入来自别人的消息）调用 `assistant_ask_bot` 后，目标员工那一轮带着所有者的令牌、不在沙箱里、可以出网。两种处理，需要裁定：

- （推荐）发起方是无人在场的轮次时，一律先过 `assistant-dispatch` 审批，不看 `allow_write`；
- 或者这种情况下目标那一轮也按 `unattended` 收紧（不签发令牌、不注入凭证），多数公共员工会因此查不了东西。

### 3. 目标员工中途要确认、授权、凭证时

| 情形 | 现有行为 | 3B 的处理 |
|---|---|---|
| CLI 的提问工具 | `finalize` 判成 `ask_user`，`_open_choice` 经出站箱发选择卡，下一条消息当答案 | 照 3A：处理器把这种结局改判，不开卡；题面写进 `blocked` 的所需事项 |
| 索取个人凭证 | 工具调平台接口，给发言者发表单卡 | 不签发索取令牌（`withhold` 的做法），本轮块里写明「缺凭证时返回 blocked 并写出变量名」 |
| 飞书 / 企微个人授权卡、文档读取授权、群写入确认卡 | 由各 policy 在真实私聊入站事件上触发 | 这些入口整组不挂：照 `origin in ("observe", "dispatch")` 的分支跳过 `opening._open` 里那一段配置。各 `configure` 对不落库替身是否都安静退出**没有逐个核实**，实现计划要核 |
| 业务令牌签发失败 | 本轮块里给一段用户可照做的说明 | 说明进了提示词，模型按结构化答复返回 blocked |
| 云服务写操作待审批 | `cloud_pending_approvals` 挂起并私信 | `allow_write=false` 时不挂云签名代理；`true` 时也不挂（审批套审批，结果回不到助理），在答复里说明 |
| MCP 登录过期 | 工具报错 | 模型返回 blocked |

统一出口：提示词用 helper 的结构化答复，再加一个字段 `needs`（所需事项的短列表）。`status=blocked` 时写 `assistant_jobs.blocked_enc`。3A 的 `jobs.block` 认的是助理名下、会话键带 `#job:` 的在途任务，3B 需要一个按 `job_id` 加 `last_task_id` 核对的变体（3A 交接第 3 条）。

### 4. `allow_write=true` 的审批能否复用第 1 期

骨架能复用，内容层要另写：

- **能复用**：`assistant_approvals` 表（`tool` 记为 `assistant_ask_bot`，`args_enc` 存目标员工与问题）、`args_hash` 去重、待批上限、Tower 回调与对账、过期、撤回、`approval_list` / `approval_withdraw`、`recover_stuck`。
- **要改**：
  - `approvals/tower.py` 的 `TARGET` 是常量 `feishu-personal`，`config()`、`request_hash`、提交体都用它；要按审批单的种类取目标系统与对应的 `tower_target_keys` 项。部署侧在 Tower 登记 `assistant-dispatch`（关闭模型比对）并配它的 Key。
  - `approvals/service.py` 的 `_stage` 与 `approvals/content.py`、`catalog.py` 按飞书个人工具的模板、锚点规则排版；调度单要一个新模板（渠道「调度员工」、对象是员工名、正文是问题原文），不做锚点检查（`checks.check_call`）。
  - `approvals/execute.py` 的 `run` 在核销后调 `PERSONAL_TOOLS[tool].run`；调度单在这里改为「派出这次求助」（把 `assistant_jobs` 从等待审批推到排队）。派出是幂等的入队，`execute_started_at` 的「绝不第二次执行」语义照旧适用。
  - 批准之后结果有两段：审批结果（已派出）与求助结果（答复）。建议批准后不单独投一轮「已批准」，只在被拒、过期、失败时投回，答复由 `assistant_job_result` 带回。
  - `assistant_jobs.status` 要多一个取值（如 `awaiting_approval`），或在派出前不建行、批准后才建。后者不占「同时 3 个」的名额，更简单。
- **`allow_write` 的效力有限**：平台管不住目标员工用它自己配置的静态环境变量、所有者的业务令牌去写。它能做的只有两件：`false` 时在本轮块里告诉目标员工「只查不改」，并且不挂平台自己把得住的写通道（云签名代理、业务系统目录里登记为可写的代理调用）。审批单与提示词里都不要把 `false` 描述成「只读」。

### 5. 「所有者本人有权使用它」怎么判断

用 `bot_collaboration.authorized_peer` 的口径，去掉「来源员工」那一半：目标是启用中的飞书普通员工（`kind='standard'`）、`accept_bot_help` 为真、`bot_allowed_users` 为空或含所有者。调用工具时、开轮前、收尾时各查一次。

- `accept_bot_help` 默认开启，它原本的含义是「群里别的员工可以 @ 我求助」。这里是所有者的助理代他去问，语义上等同所有者自己私聊，沿用这个开关可以接受；管理者关掉它就同时关掉了被助理调度。
- 管理台的可见性（`accessible_bot_ids`：创建者、成员、团队负责人）是「谁能管理」，不是「谁能使用」，不用它。
- 飞书应用的可用范围平台管不到。要更严可以再要求 `user_reached` 里有所有者与这位员工的私聊记录（API 任务的做法），代价是没私聊过的员工调度不了。建议不加，由用户定。
- `assistant_ask_bot` 的 `bot` 参数按名字或 `bot_key` 解析；另给一个只列可调度员工（名字与一句话简介）的工具，否则助理不知道有谁可问。

## 方案甲：完整加密

### 改动

- **迁移 `0101`**：`tasks`、`task_streams`、`chat_logs`、`outbox` 各加可空列 `sealed_owner_id`（uuid，不设外键，普通行为空）。若选择卡、凭证表单、云审批改判为 blocked 的路径都堵严，`interaction_states`、`cloud_pending_approvals`、`inbound_events`、`feishu_sent_messages`、`feishu_deliveries` 可以不加；堵不严就要一起加，否则那些表里是明文。
- **`coreman/core/assistants/sealed.py`**：
  - 归属从「`bot_id` → 所有者」改为「行上的 `sealed_owner_id`，没有再看 `bot_id`」。`_cipher`、`seal_values`、`open_row`、`open_value`、`keep_plain`、`_reseal`、`_owner_sync`、`key_ready`、`content_mac` 的签名或内部都要动；`open_row(viewer=…)` 的本人判断改按行。
  - `code_for_assistants` 现在是按 `bots.kind` 的 SQL 表达式，要改成同时看本行的所有者列；`guard_logs` 按 `bot_id` 标私有，要能按任务标；`routing_context` 同理。
  - AAD：仍可用「表.列:bot_id」（密钥本身按人），但要补一条规则：带所有者列的行，`bot_id` 是目标员工。
- **写入口**（都要把所有者带进来）：`coreman/core/bus/tasks.py`（`enqueue`、`finish`、取消原因）、`core/bus/streams.py`（开流、`update_state`、`complete`）、`core/chat/chat_logs.py`（五处，含后台批量写入的队列，所有者得随 `ChatLogEntry` 走）、`core/bus/outbox.py`（`add`、`last_error`）、`runtime/scheduler/reaper.py`（收尸补记录）、`runtime/worker/stream_writer.py`、`worker/chat/records.py`。
- **读取口**：`runtime/gateway_feishu/transport.py`（认领流与出站时的还原与跳过）、`core/bus/tasks.py` 的认领（密钥销毁后把任务按 `content_locked` 结掉，这条现在只会落在助理的任务上，之后会落在普通员工的任务上）、`core/chat/session_carry.py`、`core/chat/session_switch.py`、`runtime/scheduler/reaper.py`、`api/routers/chat_logs.py`（两处 `viewer` 还原与关键词搜索）、`api/routers/dev.py`、`core/api_tasks/service.py`。
- **管理台**：`chat_logs` 列表与详情已有 `content_locked` 标记，前端对普通员工名下出现这个标记要有说明文字。
- **停用**：所有者销毁密钥后，目标员工名下那些行永久不可读，这是想要的；但在途的那一轮会在认领、收尾、收尸三处遇到 `ContentLocked`，要逐处确认普通员工的处理器不会因此留下悬着的任务。

### 侵入与回归风险

高。现在普通员工在每个封存入口的开销是一次进程内缓存命中后原样返回；改后要先看行上的所有者列，写入口要多传一个参数，Core 的 `update()` 语句拿不到行、得由调用方显式给。漏传一处的后果是静默写成明文（数据保护失效但功能正常，测试不容易发现）；多判一处的后果是普通员工的内容被当成密文处理。`chat_logs` 的后台写入队列、流的 0.5 秒节流写、认领语句都是全平台最热的路径。

### 台账怎么扩

- 现有约 100 条登记里，凡理由是「助理没有这个功能」「只处理群消息」的要逐条重审：密文行现在可以出现在普通员工名下（例如 `core/cloud_proxy/resume.py`、`core/chat/topics.py`、`core/chat/group_observe.py`、`core/chat/bot_collaboration.py`）。多数因为会话键独立而碰不到，但要逐条写明依据。
- 新增一张表：带所有者列的模型清单，断言模型上有 `sealed_owner_id`；新增一类扫描：对这些模型的每个 `seal_values(` 调用与每个模型构造，必须出现所有者参数（按语法树查关键字）。
- `tests/unit/test_assistant_sealed_codec.py` 补「目标员工的 `bot_id` + 所有者密钥」的 AAD 用例与「同一员工名下明文行与密文行并存」的用例。

### 节点侧

不覆盖。目标员工的节点上留下：CLI 转录（问题、全部工具输出、答复）、工作目录里这一轮写的文件、文件记忆（模型可能把问题里的事记下来，该员工的管理者在管理台读得到）、进程环境变量里所有者的业务令牌、守护进程日志里的编号。spec 第八节已写明不做转录清除。

### 工作量

功能部分 8–9 个任务（与乙相同），加密部分另加 7–8 个：迁移与模型、`sealed` 归属改造、四组写入口、读取口与网关、认领与收尸的锁定处理、台账重审、管理台。合计 **16–18 个任务，计划约 8000–9000 行**，是 2A（9 个任务、约 3700 行）的两倍多，与 1C 加密层同量级。

### 残余风险

- 节点侧全部明文（见上），所以「拿到数据库的人看不到」之外没有增益。
- 目标员工写进业务系统日志、它自己的记忆与文件的内容不受控。
- 漏封一处即静默明文。
- 第 2、4 条的出口问题与加密无关，照旧存在。

## 方案乙：不加密，隔离可见性

### 改动

- **迁移**：不需要新表。`assistant_jobs` 的 `kind='bot_help'`、`target_bot_id` 在 3A 的 `0100` 里已留好。若审批前要占行则给 `status` 加一个取值（`0101`，只改检查约束）。
- **新增**：
  - `coreman/core/assistants/dispatch.py`：资格判断、`ask_bot` 入口（复用 3A 的 `_admit`、`_end`、`complete` 的形状）、目标侧任务的入队、按 `job_id` 写 blocked、可调度员工列表。
  - `coreman/core/assistants/dispatch_tools.py`、`dispatch_prompt.py`：工具 `assistant_ask_bot`、`assistant_bot_list`，登记在 `jobs` 组；助理侧规则（问题只带必需信息、不带原始聊天记录、`allow_write` 的含义）；目标侧本轮块（这是某人的助理代他来问、只答不发、结构化答复）。
  - `coreman/runtime/worker/assistant_bot_help.py`：`AssistantBotHelpHandler`。
- **修改**：
  - `coreman/core/assistants/kinds.py`（`BOT_HELP_KIND` 已有，补一个「目标侧私有轮次」的判断函数）、`jobs.py`（`message` / `cancel` / `list_` / `reconcile` 认 `bot_help`）、`tools.py`。
  - `coreman/runtime/worker/__main__.py`（注册处理器）、`worker/chat/opening.py`（按任务类型跳过个人工具那一段、只注入已存凭证、`_stream_kwargs` 加 `_silent`）、`worker/chat/records.py`（`private=True`）、`worker/chat/finalize.py`（结构化答复的解析与静默，照 `collaboration_silent` 的分支）。
  - `coreman/runtime/scheduler/reaper.py`（静默判断加 `BOT_HELP_KIND`）。
  - `coreman/core/assistants/approvals/tower.py`、`service.py`、`content.py`、`catalog.py`、`execute.py`（第 4 条）；`coreman/core/config.py` 的说明。
  - `coreman/core/assistants/deprovisioning.py`：停用时按 `assistant_jobs.last_task_id` 取消目标员工名下在途的任务（`step_drain` 现在只按助理的 `bot_id` 取消，够不着）。
  - `coreman/api/routers/chat_logs.py`、`dev.py`、`runtime_nodes.py`：不用改逻辑。这一轮记成飞书、`single`、`user_id` 是所有者，现有规则已经只对本人放行；另置 `private=true` 作双保险。补测试守住。
  - 文档：`docs/feishu.md`、`CHANGELOG.md`、spec、总览。
- **接口**：无新增对外接口；「我的助理」页的「正在跑」由 3C 读 `assistant_jobs`。

### 侵入与回归风险

低到中。共用路径上的改动都是对已加载任务类型的判断：`opening._open` 一个分支、`_stream_kwargs` 一个分支、`records.log_entry` 一个字段、`finalize` 一个分支、`reaper` 一个条件。最容易出错的是 `opening._open`：那一段里各个 `configure` 互相改写 `extra` 与 `env`，跳过的范围要与 `observe` / `dispatch` 分支对齐。

### 台账怎么扩

- `test_assistant_sealed_ledger.py`：只更新计数（`jobs.py` / `dispatch.py` 新增的写语句），不加新维度。
- `test_assistant_platform_turn_ledger.py`：`NewTask(kind=BOT_HELP_KIND)` 只允许出现在 `dispatch.py`。
- 新增一个可见性用例集（API 层）：同一条调度记录，所有者能读正文；平台管理员、目标员工的创建者与成员、团队负责人、机器人令牌经 `chat-logs` 列表 / 详情 / 统计、开发接口的任务详情、节点会话查看页都读不到正文，统计里计数与 token 照常。

### 节点侧

与甲相同，全部留下。乙与 spec 的说法一致（「目标员工的节点不做转录清除」）。会话查看页对本人开放，所有者可以看到这一轮的完整过程；管理员看不到。

### 工作量

**约 9 个任务，计划约 4000–4500 行**，与 2A、3A 同量级：资格与入队；目标侧处理器与替身；身份与能力收窄；结构化答复、blocked 与收尾；对账、追问、取消；调度审批；可见性用例与停用；工具与提示词；文档与 spec。

### 残余风险

- 库里有明文：问题、目标员工的思考与工具摘要、答复，存在 `chat_logs`、`task_streams`、`tasks` 里，直到各表的保留期清理。拿到数据库或备份的人能读；所有者停用助理、销毁密钥后这些行仍在。
- 可见性靠查询条件，新增任何读 `chat_logs` 正文的接口都要记得套 `_scope`；现在没有台账守这一条。
- `tasks.cancel_reason`、`outbox.last_error`、`tasks.error_message` 对普通员工的行不做「只留代码」的裁剪，报错文字可能带出片段，运维接口直接输出前两项。
- 普通员工的日志不走 `guard_logs`，异常文字可能进日志。

## 方案丙（推荐）：乙 + 目标侧不落正文

在乙的基础上加一条规则：**这一轮在目标员工名下的行只有元数据，正文只在 `assistant_jobs` 的密文列里。**

| 表 | 做法 |
|---|---|
| `inbound_events` | 用不落库的替身，没有行 |
| `tasks.payload` | 只放 `{"job_id", "round"}` 与明文控制键；问题在开轮时从 `assistant_jobs.brief_enc` / `message_enc` 解出，只在内存里（3A 对子任务就是这么做的） |
| `tasks.result`、`error_message` | 不写结果；报错只留代码 |
| `chat_logs` | `log_entry` 对这类任务把消息、引用、回复、附件信息、报错文字置空；状态、时长、token、工具名照记 |
| `task_streams` | `StreamWriter.flush` / `complete` 对这类任务不写思考、正文、终稿、卡片、计划、最近动作的说明文字；心跳与完成标记照写。网关本来就不推这条流，没有读者 |
| `outbox`、`feishu_*`、`interaction_states`、`credential_requests` | 出声点全部堵上后没有行；用例守住 |
| 答复 | 收尾时从内存里的 `pre.writer` 取终稿（helper 阶段现在就这么取），解析后写 `assistant_jobs.result_enc` / `blocked_enc` |

- **多出的改动**：`worker/chat/records.py`（置空）、`worker/stream_writer.py`（两处写入点）、`worker/chat/finalize.py`（不写结果、报错只留代码）、`scheduler/reaper.py`（补记录时同样置空）。都是按任务类型的判断。`StreamWriter` 不写正文后，两阶段超时转后台、流恢复这两条路是否还依赖库里的正文，**实现计划要核实**；核实不过就退一步：运行期间照写，收尾与收尸时清空（明文在库里只存在于这一轮运行期间）。
- **加密层**：零改动。`assistant_jobs` 已在 3A 登记进 `sealed.COLUMNS`，按助理的 `bot_id` 封存，随所有者密钥销毁。
- **台账**：同乙。另加一个端到端用例：问题与答复里各放一个哨兵串，跑完一轮（含失败、取消、被收尸三种结局）后扫描全部业务表的全部文本与 JSON 列，哨兵串只能以密文形式出现在 `assistant_jobs`。这个用例比按文件计数的台账更直接。
- **可见性**：管理员与该员工的管理者看到一条只有时间、状态、用量的记录；所有者在「我的助理」页看问题原文与答复。`chat_logs` 的那一行对所有者也是空的，详情接口可以给一句「内容在我的助理页」。
- **代价**：这类会话不能靠 `session_carry` 在换运行时后带上文（它读 `chat_logs` 的正文）；追问靠目标节点上留着的转录续聊，转录不在了就只有追问的文本，与 3A 的子任务一致。
- **工作量**：**约 10 个任务，计划约 4500–5000 行**（乙加 1 个任务）。
- **残余风险**：节点侧同甲、同乙；`StreamWriter` 那条待核实；出声点漏堵一处会在 `outbox` 里留下一行明文（哨兵用例能发现）。

## 三个方案对比

| | 甲 | 乙 | 丙 |
|---|---|---|---|
| 管理台与接口看不到正文 | 是 | 是（靠查询条件） | 是（没有正文可看） |
| 只拿到数据库的人看不到 | 是 | 否 | 是 |
| 所有者密钥销毁后内容不可读 | 是 | 否 | 是 |
| 目标节点上的转录、文件、记忆 | 明文 | 明文 | 明文 |
| 加密层改动 | 大 | 无 | 无 |
| 普通员工热路径的改动 | 每个封存入口 | 5 处类型判断 | 约 9 处类型判断 |
| 新迁移 | 4 张以上的表加列 | 无（或一条检查约束） | 同乙 |
| 任务数 / 计划行数 | 16–18 / 8000–9000 | 9 / 4000–4500 | 10 / 4500–5000 |
| 需要改写 spec | 否 | 第八节、威胁模型、已定决策 | 第八节两句 |

## 推荐

选丙。甲的全部增益是「数据库里的这一轮是密文」，丙用「数据库里没有这一轮的正文」拿到同样的效果，而且不碰加密层与普通员工的封存路径；甲要在全平台最热的几条写路径上多带一个参数，漏一处就是静默明文，这个风险换不来任何丙没有的保护。乙比丙只省一个任务，却要在威胁模型里开一条「调度的内容在库里是明文、不随密钥销毁」的例外。三个方案在节点侧完全一样：目标员工不在沙箱里，转录、工作目录、文件记忆都留着，这一层的保护等级就是「所有者私聊这位公共员工」，应当在提示词、审批单与开通页里如实说明。比选哪个方案更需要先定的是出口问题：无人在场的轮次能否不经审批调度公共员工。建议不能。

## spec 需要改写的地方

**选丙时：**

- 第八节第 3 条改为：「这一轮在目标员工名下只留元数据（时间、状态、用量），问题与答复只存在助理的 `assistant_jobs` 里，用所有者的数据密钥加密；目标员工的管理者与平台管理员只看得到发生过一次调度。」
- 第八节第 2 条「权限与所有者私聊它时相同」改为：「业务系统权限与所有者在该员工名下已存的个人凭证相同；飞书与企微个人工具、个人定时任务、凭证索取、云签名代理不提供。」
- 第八节第 4 条补：「无人在场的轮次发起的调度一律先过审批」（若按推荐裁定）；`allow_write=false` 的说明改为「告诉对方只查不改，平台不保证」。
- 第八节补一条：问题会以明文交给目标员工所在的节点，留在它的转录、工作目录与记忆里，等同所有者自己私聊它。
- 威胁模型「被注入的助理自身」一行的依据补：「调度公共员工是沙箱之外的通道，受每日个数、问题长度与（无人在场时的）审批约束」。
- 第十节开通处的告知加一句同样的话。

**选乙时，在上面之外还要改：**

- 「已定决策」的「个人数据」一行：加例外「调度公共员工时交出去的问题与对方的答复，在目标员工的记录里是明文」。
- 威胁模型「只拿到数据库或备份的人」一行：加同样的例外，并写明这部分内容不随密钥销毁、按各表保留期清理。
- 第二节「加密范围」表下加一句：目标员工名下的行不在范围内。
- 第二节「销毁」：补「调度留在目标员工名下的记录不受影响」。
- 第八节第 3 条改为：「标为 `private`，正文只对所有者本人开放，明文存储。」
- 「测试」的「停用后库里密文不可解」加限定。
- 总览裁定 9 写上结论。

## 待用户裁定

1. 方案：丙 / 乙 / 甲。
2. 无人在场的轮次发起调度：一律审批（推荐）/ 目标侧按无人在场收紧 / 不限制。
3. 资格是否另要求「所有者私聊过这位员工」（建议不要）。
4. 批准后是否单独投一轮「已批准并派出」（建议不投，只投被拒、过期、失败与最终答复）。
