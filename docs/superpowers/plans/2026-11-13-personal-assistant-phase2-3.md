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
| 审查修补 | 第 2 期与 3A 的独立审查发现的问题（盯消息倒灌历史、索引行引用、自动撤回循环、被打断的交回、游标封存等） | 2B–3A | `0101_assistant_hardening` |
| 3B 调度公共员工 | `assistant_ask_bot`、`assistant_bot_list`、目标员工那边的私有轮次、`assistant-dispatch` 审批 | 3A | — |
| 3C 页面 | `/api/me/assistant`、「我的助理」页、管理台的助理列表与设置项表单 | 2C 2E 3A | — |
| 3D 导入与访问日志 | `assistant_access_log`、导入与导出接口 | 2B 2C 3C | `0102_assistant_access_log` |
| 第二轮审查修补 | 第 2、3 期独立审查发现的问题（周末消息被年龄上限丢掉、无人在场调度到别人的私有运行时、重新生成口吻页的限频与事务等，见裁定 24–26） | 2D–3D | `0103_assistant_voice_attempts` |

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
10. **导入。** spec 说管理命令、由所有者本人执行，但管理命令是运维在服务器上跑的。裁定：做成 `/api/me/assistant/import` 上传接口（Markdown 压缩包），所有者本人登录后操作，并写访问日志。 落地时的补充（3D）：导出与导入用同一种格式；预览与确认之间用清单的摘要做乐观锁；解包后的硬上限取 32MB（正文合计仍按知识库的 20MB 算），否则装满的知识库导出后导不回去。
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

20. **审查修补改动的接口**（独立审查之后，迁移 `0101_assistant_hardening`；3B 落地后确认不需要迁移，3D 用 `0102`，见裁定 21）：
    - `assistant_watch.cursor` 改名 `cursor_enc` 并封存；要看游标里的字段先 `sealed.open_value` 或取整行，不能在 SQL 里过滤。
    - 新列：`assistant_approvals.revised_at`、`assistant_inbox.handovers`（带着它开过几轮）、`assistant_delivery.handover_misses` / `handover_retry_at`（没走完的交回之后的等待）。
    - 引用统一走 `assistants/quoting.py` 的 `quoted`；知识库注入里页面的路径、标题、摘要都在引用行里，平台写的行不带页面文字（3D 的导入页、3C 展示索引时同此口径）。
    - `approvals.checks.Message` 多了 `mentions`、`parent_id`、`root_id`；`anchor_verdict` 在群里只算与锚点有关的新消息。`approvals.service.auto_withdraw` 不等行锁、可传 `now`。
    - `watch.restart_chats(session, bot_id, chat_ids, now)`：会话从忽略名单移出时由 `settings.update` 调用；3C 的设置表单经 `settings.update` 走到它，会等正在进行的一轮轮询提交（至多几十秒）。
    - `voice.save` 在口吻页最后不是助理写的时候返回 None（不覆盖）；`notes.author(session, bot_id, path)` 取一页最后的作者。
    - 不出声的轮次继承 `worker.platform_turn.SilentTurnHandler`，`COLD_START_KIND` 已在 `SILENT_KINDS` 里。

21. **调度公共员工层的补充**（3B 落地时定）：
    - 本层没有迁移（总览原先留的 `0102_assistant_dispatch` 不用，3D 改回 `0102`）：等待审批的调度不占 `assistant_jobs` 的行，批准后才建行，行的编号沿用审批单的编号（执行任务重跑不会派第二次）；审批单的 `channel` 用 `other`，靠 `tool = 'assistant_ask_bot'` 区分。
    - 一次求助只有一轮：`jobs.message` / `assistant_job_message` 对 `bot_help` 返回 `job_no_follow_up`（在占名额之前判断）。`jobs.list_` / `view` / `cancel` / `cancel_all` / `reconcile` / `active_counts` 不分种类，对它照常可用。
    - 目标员工那一轮的处理器 `AssistantBotHelpHandler` 继承 `SilentTurnHandler`，整个换掉入站解析（基类只认助理自己的轮次）；`BOT_HELP_KIND` 进了 `kinds.SILENT_KINDS`，但不进 `ORIGIN_KINDS` / `PLATFORM_TURN_KINDS`：它不是助理自己的轮次，`unattended` 对它为假，能力收紧在 `opening._open` 里按任务类型单开一支（不挂个人工具、本人定时任务、群文档、求助、凭证索取、云签名代理；只注入已保存的个人凭证）。
    - 目标员工用提问工具时改判为正常结束、答复是平台代写的 `blocked`；终稿是额度告示时按 `rate_limited` 失败（基类的行为）。收尸补记录时这类任务标私有、类型用任务类型、不写私聊可达。
    - `allow_write=false` 在平台侧能强制的只有业务系统目录的代理调用不放写（`catalog_policy.writable_for`）；业务令牌没有只读范围。云签名代理不论 `allow_write` 都不挂。
    - 可见性按 `chat_logs.message_type = 'assistant_bot_help' AND private` 判断：管得着这位员工的人看得到这一行、看不到内容（输出带 `content_withheld`），带关键词的列表与统计不含它；机器人令牌与无关成员整行不可见；开发接口（按任务类型与发起人把关，开流前也一样）与节点会话查看页对非所有者仍是「不存在」。
    - 停用的 `drain` 一步把目标员工那边在途的轮次算进「还在跑」（`dispatch.live_rounds`）。停用期间已批准的调度单执行时因助理不在 `active` 而记失败，不会派出。
    - 顺带修了转录交接的一处竞态（不属于调度）：被取消的一轮的转录还在从节点传回来时，下一轮开轮前最多等 `transcripts.UPLOAD_WAIT`（20 秒，从被取消那一轮结束起算；`transcripts.awaiting_upload`，在 `OpenStage._wait_transcript` 里用短事务轮询），等到了照常续，等不到才换新会话。

22. **页面层的补充**（3C 落地时定，所有者 2026-11-14 的取舍）：
    - 知识页的路径不进网址：读与删是 `POST /api/me/assistant/notes/read`、`POST /api/me/assistant/notes/delete`，路径在请求体里；写仍是 `PUT /notes/page`。索引不收目录参数（目录名也是路径的一部分）。原因：网址会进反向代理与应用的访问日志，而别处都在避免个人内容进日志。3D 的导入、导出同此口径。
    - 给所有者的链接只用审批系统的网页入口（`tower.web_url(settings)`，即 `TOWER_WEB_URL`）：私聊里「有 N 张待批」的提醒与页面「等你出手」都是它，没配就不带链接，不回落到接口地址 `TOWER_URL`。
    - 「最近落地」列审批单的全部终态（`owner_view.SETTLED`：`executed`、`rejected`、`withdrawn`、`failed`、`expired`、`stale`）；`approved` 是途中状态，两个分区都不列。
    - 页面上可以即时上班、下班、回到按工作时段（`POST /shift`，`owner_view.switch_shift`，与私聊命令同一个效果，同样写 `worked_on`），可以重新生成口吻页（`POST /voice/regenerate`，`cold_start.regenerate_voice`）：冷启动没做完时拒绝；每天（所有者时区）至多 `cold_start.REGENERATE_DAILY`（3）次，按排上的轮数算，靠任务的幂等键 `voice.dedupe_key(bot_id, <日期×10+第几次>)` 数，不加列；口吻页最后不是助理写的时不排（`voice.save` 本来也不会覆盖）。读飞书在这个请求里做，出错时提交而不回滚（刷新过的令牌不能丢）。（限频与事务的做法后来改了，见裁定 25。）
    - 每日轮次预算以个人值与平台默认里较小的为准（`delivery.effective_budget`，`budget_left`、`usage`、预算用完的告知、所有者概览都用它）；页面不让个人值高于平台默认（`owner_view.set_budget`）。管理员调低平台默认会同步收紧，个人值留着。
    - 两处「在班」同一口径：所有者概览的 `on_shift` 也要求记录启用且开通好。
    - 开通卡片多一句调度的告知（裁定 17 的存储方式）。
    - 所有者视图里的子任务比 `jobs.view` 多两项：`origin`（`system` 是平台派的下班整理）与 `target`（调度的目标员工，按 `target_bot_id` 另查名字）；`jobs.view` 本身不变，助理工具的输出不变。
    - 管理台对话记录认 `content_withheld`：列表与详情都显示「内容仅发起人可见」，不摆出空的消息与回复两栏。

23. **访问日志与导入层的补充**（3D 落地时定，所有者 2026-11-14 的取舍）：
    - 访问记录与读取在同一个事务里：`access_log.record` 在调用方的事务里用保存点写一行，读接口在返回前提交（`/jobs`、`/approvals/recent`、`/notes`、`/notes/read` 补了提交）。没有选「独立短事务」：那样记录写进去了、读取却可能失败（多记无妨），但更要紧的是反过来——独立事务提交失败时没法阻止内容已经算好并返回；同一个事务里「提交不了就不交出内容」是自然的。代价：保存点里的那一行单独写失败而外层提交成功时，这一次读取只在应用日志 `assistant_content_access` 里有记。
    - 改设置（`PATCH /settings`）与即时上下班（`POST /shift`）的响应带着解密后的重点群与忽略名单，也记 `settings.read`，对象分别是 `update`、`shift`（3C 的独立审查提出）。台账把 `owner_view.preferences` 算作解密。
    - 管理命令的会话读不到助理的加密内容（比计划更严）：`coreman … --as 登录名` 建的会话 `auth_method = 'cli'`（`deps.via_cli`）。`/api/me/assistant` 整个前缀拒绝它（403 `cli_session_refused`）；带 `viewer=` 的还原（对话记录列表与详情、开发页任务详情）对它用 `access_log.NOBODY` 当查看者，输出与不是所有者时相同；会话的身份正是所有者时记一条 `actor_kind = 'system'`、操作人为空的 `cli.chatlogs` / `cli.tasks`（`access_log.record_refused`）。不看内容的 `assistant-keys rewrap --apply` 照常并记 `cli.rewrap`。
    - 压缩包里的系统杂项文件忽略而不是拒绝：`.DS_Store`、`__MACOSX/` 下的一切、`Thumbs.db`、名字以 `._` 开头的文件（先过文件名安全与符号链接两项检查），不读内容、不占页面文件名额，预览里的 `ignored` 给出个数；其它非 `.md` 文件仍然拒绝整包。
    - 上传按块读，超过 20MB 立刻中止并回 413；框架已先把请求体落到临时文件，挡「传上来」本身靠反向代理（`docs/operations.md`）。导入、导出两个请求的前端超时 180 秒。
    - 调度公共员工那一轮（裁定 17，明文存在目标员工名下）不是解密，本人读它不进访问日志；管理命令以所有者身份建的会话原先仍能在对话记录与任务详情里看到它的内容（`withheld` 只看发起人与机器人令牌），第二轮审查修补里补上了，见裁定 26。

24. **无人在场时不能调度跑在别人私有运行时上的员工**（独立审查之后，对裁定 17 的补充）：裁定 17 定了无人在场的只读调度不经审批、不收紧目标侧的令牌与凭证，这一条不改它，只在平台侧多拦一种目标。现状：交回轮次的输入来自他人；`accept_bot_help` 默认开、没设可用人名单的员工谁都能调度；私有运行时（`relay_servers.visibility = 'private'`）的所有者在自己的节点上读得到那一轮的问题。三者合起来，同事发一条诱导消息就能让助理把内容问给他自己建的员工。裁定：发起的轮次无人在场（`scope.unattended`）时，目标员工绑的运行时实例是私有的且 `owner_id` 不是这位助理的所有者（含没记所有者的），`dispatch.authorize` / `resolve` 拒绝并返回 `owner_turn_required`，`dispatch.candidates` 不列它；所有者在场的轮次、公开运行时、所有者自己的私有运行时不受限。`allow_write=true` 的调度由无人在场的轮次提交时在提交前就拒（`dispatch_tools` 先 `resolve` 后提交），批准后执行时按审批单里记的 `unattended` 再查一次（提交之后运行时才改成私有的情况）。目标那一轮开轮前的复查不带这道拦截（求助的行里没记发起时在不在场，派出时已经查过）。代价：所有者不在场时助理问不了同事私有运行时上的员工，只能记待跟进等他当面发起；公开运行时上的员工仍可能被诱导调度，那部分仍按裁定 17 靠提示词、长度上限与审计。接口：`dispatch.authorize(..., attended=True)`、`resolve(..., attended=True)`、`candidates(..., attended=True)`、`on_foreign_runtime(session, target, owner_id)`。

25. **重新生成口吻页的限频与事务**（独立审查之后，改裁定 22 里的相应一条，迁移 `0103`）：原先每天 3 次只数排上的轮数，样本太少、飞书读不了都不占次数却每次都读飞书，可以无限重放；读飞书时请求的事务一直开着，所有者的授权锁挡住这位助理的轮询与工具调用。裁定：`cold_start.regenerate_voice(factory, cipher, bot_id, now)` 自己分三段短事务（过门槛并记一次尝试、读飞书、排任务），路由先结束请求的事务再调它；读飞书时每调完一次就提交（`voice.collect` 的 `between`），读的过程中任何异常都不冒出事务（刷新过的令牌照常落库），按 `feishu_unavailable` 报。限频两层：排上的轮数每天至多 3（不变）；尝试次数每小时至多 3、每 24 小时至多 6（滚动窗口，读了飞书就算），存在 `assistant_settings.voice_attempts`（秒级时间戳的列表）。读之前看应用的分钟用量（`ratelimit.Budget.load`），到 `ratelimit.TIGHT` 就返回 `feishu_busy`（429），不读、不占次数。同时只有一个请求在读：`assistant_settings.voice_regenerating_at` 作标记，第二个请求返回 `regenerate_in_progress`；标记读完即清，进程中途没了的 3 分钟后失效。代价：多两列；滚动窗口与「每天 3 轮」按所有者时区的自然日口径不同，页面只显示后者的剩余次数。

26. **第二轮审查修补的其余改动**（独立审查之后；每条一个提交）：
    - 盯消息的 24 小时年龄上限改为「现在 − 24 小时」与上一次成功轮询里更早的那个（`watch._oldest`）：下班后不轮询，周末、请假期间发来的消息都晚于上一次成功轮询，回来后的第一轮照常入箱；上限仍管「轮询照常、单个会话停读了几天」。游标里没有的会话仍从 `_floor` 读起。
    - 一轮读不完、排进 `todo` 的会话当场把这一轮的回看起点写成游标的 `t`，并记下这一轮的年龄截止点（游标条目的 `a`，读完去掉；只对还在排队的会话保留）；一轮读到页数上限的会话同样带着 `a`。`feed.dump` 裁剪游标时先留排队中的会话。
    - `approvals.checks.ChatView.thread`：按话题容器读的视图。带话题的审批单按私聊口径全算；主线视图里另加「与锚点同一个话题根」的也算有关。`anchor_verdict` 在锚点本身已撤回时返回 `message_gone`（`delivery_checks` 把它映射成 `anchor_recalled`）。
    - `dispatch.resolve` 按编号、标识、名称解析；标识命中而另有员工的名称正是这个串时与同名一样报 `bot_ambiguous`，`DispatchError.bots` 带候选，工具输出里是 `candidates: [{bot, name, id}]`。给编号是因为标识与名字相撞的那一位用标识再调仍然相撞，只有编号指得明。
    - 已批准的调度不受并发上限与每日个数限制（`jobs._admit(..., capped=False)`），仍计入计数。
    - `jobs.report` 入不了队时设 `report_after`：60 秒起每次加倍、封顶 15 分钟；上一次的等待由 `report_after − updated_at` 推出，不加列。
    - 管理命令的会话（`deps.via_cli`）读调度公共员工那一轮时按非所有者处理：对话记录列表与详情带 `content_withheld`、按正文的搜索与统计不含它；开发页任务详情只给元数据（不给流，任务的 `error_message` 置空，带 `content_withheld`）。在所有者启用着的助理名下记一条 `actor_kind = 'system'` 的 `cli.chatlogs` / `cli.tasks`，对象 `dispatch=N`（`access_log.record_dispatch_refused`；所有者已经没有助理时没有可记的行）。节点会话查看页没有动。

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

### 3B

- 一次求助是一行 `assistant_jobs`：`kind = 'bot_help'`、`origin = 'assistant'`、`target_bot_id`、`session_key = "assistant-help:<id>"`、`origin_session_key` 是主会话键、`rounds = 1`；`title_enc` 是「请「员工名」协助」，`brief_enc` 是问题原文，`result_enc` 是答复，`blocked_enc` 是对方需要的东西（多条用「；」连接）。状态与子任务相同：`queued` / `running` / `done` / `blocked` / `failed` / `cancelled`；失败代码有 `bot_help_not_allowed`、`bot_help_announced`、`relay_unavailable`、`session_busy`、`empty_answer`、`rate_limited`、`job_timeout`、`turn_lost` 以及任务自己的错误代码。
- `coreman/core/assistants/dispatch.py` 对外：`resolve(session, bot, ref)`、`authorize(session, bot, target_id)`、`candidates(session, bot)`、`listing(rows)`、`clean_question(value)`、`owner_of(session, bot)`、`send(session, bot, target, question, *, allow_write, attended, approved=False, job_id=None, now=None)`、`for_round(session, task)`、`round_text(row, owner_name)`、`mark_running(session, task)`、`finish(session, task, *, ok, answer=None, error_code=None)`、`fail(session, task, code)`、`read_answer`、`blocked_text`、`result_text(session, row)`、`live_rounds(session, bot_id)`、`session_key_for(job_id)`、`DispatchError`、`Answer`、`Round`、`MAX_QUESTION`、`TOOL`、`AUDIT_ACTION`、`STATUS_WORDS`、`FAILURE_NOTES`。
- 工具 `assistant_bot_list`（返回 `bots: [{bot, name, description}]` 与 `limits`）、`assistant_ask_bot`（只读返回 `{job: jobs.view(row), bot, name, hint}`；`allow_write=true` 返回审批提交的结果加 `bot`），注册在 `jobs` 组；目标侧处理器 `AssistantBotHelpHandler`，任务类型 `BOT_HELP_KIND`，载荷 `{job_id, round, allow_write, bot_key, platform_user_id}`。
- 审计：`audit_logs.action = 'assistant.dispatch'`，`actor_id` / `actor_login` 是所有者，`target_type = 'bot'`，`target_id` 是目标员工，`diff = {assistant_bot_id, job_id, unattended, allow_write, approved, question_chars}`；每次派出一条（等待审批时不写）。
- 审批：`approvals/dispatching.py` 的 `submit(session, scope, *, target, question, cfg)` / `run(factory, approval_id, cfg)`；`tower.DISPATCH_TARGET = "assistant-dispatch"`，`tower.config(settings, target)`，`tower.Config.target`。审批单 `tool = 'assistant_ask_bot'`、`channel = 'other'`，`args_enc.calls[0].arguments = {bot_id, bot, name, question}`，`args_enc.abstract` 是「调度员工 → 员工名」。
- 3C 的「正在跑」里调度与子任务同表同接口，用 `view(row)["kind"]` 区分；等待审批的调度在「等你出手」的待批审批单里（`tool = 'assistant_ask_bot'`），还没有 `assistant_jobs` 行。目标员工的名字由 3C 按 `target_bot_id` 另查（`view` 里的 `title` 带着派出当时的名字）。所有者在页面上取消一次调度用 `jobs.cancel(session, bot_id, job_id, code="cancelled_by_owner", report=True)`，与子任务相同。管理台对话记录列表要认 `content_withheld`。

### 3C、3D

3C 的接口前缀 `/api/me/assistant`，鉴权照 `feishu_personal.interactive_user`，落地的接口如下（计划 `2026-11-13-personal-assistant-3c-pages.md` 的「接口一览」是落地前写的，以这里为准）：

| 方法 | 路径 | 访问记录（动作, 对象） |
|---|---|---|
| GET | `` | — |
| GET | `/attention` | `approvals.read` `pending`、`jobs.read` `blocked`、`followups.read` `today` |
| GET | `/jobs` | `jobs.read` `list` |
| POST | `/jobs/{job_id}/cancel` | — |
| GET | `/followups` | `followups.read` `open` |
| POST | `/followups`、PATCH `/followups/{label}`、POST `/followups/{label}/close`、POST `/followups/{label}/snooze` | — |
| GET | `/approvals/recent` | `approvals.read` `recent` |
| GET | `/notes` | `notes.index` 空串 |
| POST | `/notes/read` | `notes.read` `page`（路径不进记录） |
| PUT | `/notes/page`、POST `/notes/delete` | — |
| GET | `/settings` | `settings.read` 空串 |
| PATCH | `/settings`、POST `/shift`、POST `/voice/regenerate`、POST `/deprovision` | — |

给 3D 的交接：

- `coreman/core/assistants/access_log.py` 的 `async def record(session: AsyncSession, bot_id: uuid.UUID, actor_id: uuid.UUID, action: str, target: str = "") -> None` 是页面读取密文的唯一记账点，现在只写日志 `assistant_content_access`（`del session`，不落库）；`ACTIONS = ("approvals.read", "followups.read", "jobs.read", "notes.index", "notes.read", "settings.read")`，`MAX_TARGET = 64`，不认识的动作抛 `ValueError`。3D 把函数体换成写 `assistant_access_log`（调用方都在记完之后才返回；只读接口有的不提交，落库时要么在 `record` 里用独立的短事务，要么给这些接口补上提交——`/jobs`、`/approvals/recent`、`/notes`、`/notes/read` 现在没有 `session.commit()`），调用点不动，新增动作（导出、导入）加进 `ACTIONS`。`target` 只放明文的编号或范围名，知识页的路径不进 `target`。
- 所有者查看自己的访问记录、导入、导出：接口加在 `coreman/api/routers/me_assistant*.py`，用 `me_assistant_common` 的 `mine` / `writable` 与路由级依赖 `DEPENDENCIES`，并登记进 `tests/api/test_my_assistant_guard.py` 的 `CASES`（不登记，台账用例失败；`/deprovision` 要留在最后一项）。路径、文件名这类个人内容不进网址。
- 导入写知识页用 `notes.write(..., updated_by="import", owner_present=True)`，页面会显示「导入」；待跟进没有 `updated_by` 列。
- 所有者的每日轮次预算由 `owner_view.set_budget(session, bot_id, value)` 写（不得高于平台默认值）；其余设置项仍是 2F 的 `settings.update`。
- 前端：页面壳 `web/src/views/MyAssistantView.vue`（标签页 `attention` / `jobs` / `followups` / `recent` / `notes` / `settings`，`el-tab-pane lazy`），分区组件在 `web/src/components/assistant/`（`AssistantAttention.vue`、`AssistantJobs.vue`、`AssistantFollowups.vue`、`AssistantRecent.vue`、`AssistantNotes.vue`、`AssistantSettings.vue`，都没有属性或只有 `name` / `runtime`，挂载时自己取数，`defineExpose({ load })`），接口封装 `web/src/api/myAssistant.ts`（对象 `myAssistant`），文案 `web/src/i18n/assistant.ts`（`assistantZh` / `assistantEn` / `assistantJa`，键在 `assistant.<分区>.<键>` 下；`web/tests/assistantI18n.spec.ts` 与 `tests/i18n.spec.ts` 会查三种语言的键齐全与插值参数名）。管理台列表 `web/src/views/AssistantsView.vue` + `web/src/api/assistantsAdmin.ts`；平台设置表单 `web/src/components/AssistantPlatformSettings.vue`。

3D 落地的接口：

- 迁移 `0102_assistant_access_log`：表 `assistant_access_log`（`id`、`bot_id`、`actor_id`、`actor_kind`、`action`、`target`、`at`）。
- `coreman/core/assistants/access_log.py`：`record(session, bot_id, actor_id, action, target="", *, kind=None)`（调用方提交）、`record_opened(session, rows, actor_id, action) -> int`、`record_refused(session, rows, owner_id, action) -> int`、`NOBODY`、`bots_of(session, owner_ids)`、`list_(session, bot_id, *, limit=50, before=None)`、`last_foreign(session)`、`cleanup(session, now, *, limit=500)`；动作代码在 `ACTIONS`（十五个，含裁定 27 加的 `cli.session`）。
- `coreman/core/assistants/archive.py`（压缩包的格式与安全检查，纯函数；`Parsed.ignored`）与 `transfer.py`（`plan` / `apply` / `preview` / `counts` / `export`）；`notes.dump(session, bot_id)`。
- `GET /api/me/assistant/access-log?limit=&before=`、`POST /api/me/assistant/import`（`multipart/form-data`：`archive`、`dry_run`、`overwrite`、`expect`）、`GET /api/me/assistant/export`；`GET /api/admin/assistants` 多 `last_non_owner_access_at`。
- `deps.via_cli(request)`：管理命令建的会话。之后新增只对本人开放的接口时用它区分；新增带 `viewer=` 的还原时照对话记录的写法（`NOBODY` + `record_refused`）。
- 之后的层新增运行时路径之外的解密时：在同一个函数里调 `access_log.record`，否则 `tests/unit/test_assistant_access_ledger.py` 失败。
- 前端：`web/src/components/assistant/AssistantAccessLog.vue`（嵌在设置分区）、`AssistantTransfer.vue`（嵌在知识库分区）、`web/src/utils/download.ts`。

## 每层都要守的约束

- 新的内容表必须有 `bot_id` 列，登记进 `sealed.COLUMNS`、`tests/unit/test_assistant_sealed_ledger.py` 的四张表、`tests/conftest.py` 的 `BUSINESS_TABLES`、`coreman/core/db/models/__init__.py`。
- 以所有者身份调飞书时，每位助理每轮一个短事务：刷新令牌的锁要到事务提交才释放。
- 轮询与投递的循环必须过滤 `bots.enabled` 与 `assistant_state = 'active'`，并尊重 `assistant_runtime.paused`；暂停期间不重复私信。
- 不把飞书的原始错误文本交给模型；日志里不出现消息内容（`sealed.guard_logs`）。
- 验证命令：`uv run pytest <路径>`、`uv run ruff check .`、`uv run ruff format --check .`、`uv run mypy coreman`、`uv run alembic check`。
- 文档：每层更新 `docs/feishu.md`「个人助理应用」与 `CHANGELOG.md`；3C 另更新 `docs/operations.md`。
