# 个人助理 3C：页面层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 所有者在管理台的「我的助理」页上看到并处理自己助理的一切（等你出手、正在跑、待跟进、最近落地、知识库、设置、停用），平台管理员在助理列表页看到每位助理的元数据并能停用，在平台设置页改助理的四个设置项；接口在 `/api/me/assistant` 下，只对所有者本人还原密文。

**Architecture:** 后端新增三个薄路由文件（共用一个前缀与一组依赖：交互登录、CSRF、`no-store`、「取本人的助理」），业务全部落在已有的核心模块上（`followups`、`notes`、`jobs`、`deprovisioning`、`delivery`、`settings`——设置的校验与写入用 2F 的 `settings.update`），本层只新增两个核心模块：`owner_view`（概览、空状态、审批单的所有者视图、设置表单的视图与每日预算）与 `access_log`（解密访问的调用点，本层只记结构化日志，3D 换成落库）。前端新增一个页面壳加六个分区组件、一个管理员列表页和一个嵌进平台设置页的表单组件，文案放在按功能拆出的 `i18n/assistant.ts`（中 / 英 / 日）。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy async、PostgreSQL、pytest（`httpx.AsyncClient`、`structlog.testing.capture_logs`）；Vue 3（`<script setup>`）、Element Plus、vue-i18n、vue-router、axios、vitest + `@vue/test-utils` + jsdom。

**Spec:** `docs/superpowers/specs/2026-11-11-personal-assistant.md`（第九节「我的助理」页、第十一节「管理员控制与审计」、威胁模型表、第二节「加密与存储」、第六节、第七节、第十节「停用」）；分层总览 `docs/superpowers/plans/2026-11-13-personal-assistant-phase2-3.md`（「3B、3C、3D」一段与「每层都要守的约束」）；前置层的计划 `2026-11-13-personal-assistant-2e-delivery.md`（Task 10）、`2026-11-13-personal-assistant-3a-jobs.md`（文末「给后面各层的交接」）。

## Global Constraints

- 集成分支 `claude/personal-assistant-phase2-3`。开工前确认 3A 已在这条分支上（2A–2F 已落地，见「对已有代码的假定」）；本层没有迁移，`uv run alembic check` 必须仍然干净。
- 公开仓：代码、测试、文档、提交信息只用通用占位（`example.com`、`demo-bot`、`cli_example`、张三 / 李四），不出现任何公司名、内部域名、真实人名、本机绝对路径；提交前跑仓库约定的敏感词自查。
- 接口前缀 `/api/me/assistant`，鉴权照 `coreman.api.routers.feishu_personal.interactive_user`：要求交互登录会话，不接受机器人令牌（`via_bot_token` 为真一律 403）；路由级依赖 `verify_csrf`（写操作缺 `X-CSRF-Token` 一律 403）；每个成功响应带 `Cache-Control: no-store`。仓库没有通用的接口限流依赖（`coreman/api/security.py` 里只有登录用的 `DbLoginLimiter`），本层不加限流。
- 可见性：接口不接受任何「助理编号」参数，助理一律由登录人推出（`bots.kind = 'assistant' AND owner_id = <登录人> AND enabled`）；输出内容前再用 `sealed.open_rows(session, rows, viewer=<登录人>)` 核一次。平台管理员、团队负责人、其他成员调这些接口，得到的只是自己的助理或「没有助理」，拿不到别人的任何内容。
- 管理台的助理列表只出元数据（人、运行时、状态、时间、计数、错误代码），不读、不解密任何内容列。
- 读取密文的每个接口在返回之前调用 `access_log.record(session, bot_id, actor_id, action, target)`；`target` 只放明文的编号或范围名，知识页的路径本身是个人内容，不进 `target`。
- 所有者在页面上改知识页一律 `owner_present=True`、`updated_by="owner"`；这是所有者看见并修改置顶页的唯一界面。
- 停用不可撤销：请求体的 `confirm` 必须是 JSON 的 `true`（`"true"`、`1` 都是 422），复用 `deprovisioning.request`，触发方 `owner`。
- 数值（原样取自已有代码）：待跟进未结上限 500、标题 200 字、备注 4000 字（`followups.MAX_OPEN` / `MAX_TITLE` / `MAX_NOTES`）；知识库 2000 页、合计 20MB、单页 200KB、路径 200 字、标题 120 字、摘要 300 字（`notes.MAX_*`）；每日轮次预算 10–2000（`delivery.BUDGET_MIN` / `BUDGET_MAX`），所有者自己设的不得超过平台默认值；重点群至多 5 个（`settings.MAX_FOCUS`，与 `watch.FOCUS_MAX` 一致），忽略的会话至多 50 个（`settings.MAX_IGNORED`）；工作时段 `HH:MM`、开始早于结束、不跨午夜；工作日是 ISO 星期 1–7。
- 普通员工（`kind='standard'`）的行为不变：本层不动任何共用路径，只新增路由与页面；管理接口只把「开始停用并记审计」提成公共函数，行为不变。
- 前端只用仓库现有的组件库与封装：Element Plus、`@/api/client` 的 `call` / `http`、`@/utils/errors` 的 `errorMessage` / `isVersionConflict`、`@/utils/format` 的 `formatDateTime`、`vue-i18n`；不引入新依赖。文案不写 `@ { } |`（vue-i18n 会当成语法），插值参数名只用 `tests/i18n.spec.ts` 渲染用例已经提供的那几个（`name`、`count`、`n`、`total`、`time`、`value`、`detail`、`field`、`start`、`end`）。英文文案里不出现中日文字符（同一用例会查）。
- 前端的用例也在 `vue-tsc` 的检查范围内（`web/tsconfig.json` 的 `include` 含 `tests/**/*.ts`，`strict`、`noUnusedLocals`）：用例里的类型要写对，没用到的导入要删。`vue-tsc` 若对某个 `v-model` 报类型不符，改成 `:model-value` 加 `@update:model-value` 并断言类型（`SettingsView.vue` 的 `default_effort_level` 就是这么写的）。模板里属性与内容的换行以 `npx eslint --fix <文件>` 的结果为准。
- 代码风格：`ruff` 行宽 100，`mypy --strict`；注释与文档字符串用中文，写清「为什么」。计划里的代码块若与格式化器的换行不同，以 `uv run ruff format <文件>` 的结果为准。
- 后端测试命令：`uv run pytest <路径> -q`；`tests/api` 下的用例要数据库（见 `docs/development.md`「验证」，不要指向生产库，用例会清空表）。每个后端任务结束时另跑 `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`。
- 前端测试命令（在 `web/` 目录下）：`npx vitest run tests/<文件>`（`package.json` 的 `test` 脚本就是 `vitest`，这与 `npm test -- --run tests/<文件>` 等价，`docs/development.md` 写的也是 `npx vitest run`）；每个前端任务结束时另跑 `npm run lint && npm run build`（`build` 含 `vue-tsc --noEmit`）。
- 本层不做：访问日志表与所有者查看自己的访问记录、导入接口（3D）；调度公共员工在页面上的展示（3B 落地后 `assistant_jobs.kind = 'bot_help'` 的行会自然出现在「正在跑」里，本层不为它加专门的字段）；按群名挑重点群（见文末「没覆盖到的地方」）。

## 对已有代码的假定

本计划对着分支上已落地的 2A–2F 的代码与 3A 的计划写成（写计划时 3A 还没有代码；2E 的最后一个任务与 2F 是写计划期间落地的，下面按落地后的实际代码写）。开工时逐条核对，对不上的先停下来改计划。

**已落地，按实际代码**

- `coreman.api.routers.feishu_personal.interactive_user(request, actor) -> User`；`coreman.api.security.verify_csrf`；`coreman.api.mcp_rpc.NO_STORE == {"Cache-Control": "no-store"}`；`coreman.api.errors.ApiError(status_code, code, message, errors=None)`、`not_found(message)`、`VERSION_CONFLICT == 40901`。
- `records.is_assistant(bot)`、`records.visible_to(bot, user)`；`bots.assistant_state` 取 `provisioning` / `active` / `deprovisioning` / `deprovisioned`；部分唯一索引保证一人至多一条 `enabled` 的助理记录，停用的最后一步才把 `enabled` 置假。
- `followups.add / update / snooze / close / list_ / due / get / view / parse_label / label / KEEP / FollowupError`，签名见 `coreman/core/assistants/followups.py`；`list_(session, bot_id, scope, *, today, limit=50)` 的 `scope` 取 `today` / `open` / `all` / 四个分类名。
- `notes.index / read / write / delete / normalize / NotesError / Entry / Page`，签名见 `coreman/core/assistants/notes.py`；`write(..., updated_by="assistant", owner_present=True)`；`Entry` 目前没有 `updated_by` 字段（本层 Task 3 加）。
- `sealed.open_row(session, row, *, viewer=None)`、`open_rows(session, rows, *, viewer=None)`、`is_locked(row)`、`key_ready(session, bot_id)`、`master()`；`keys.ensure_key(session, cipher, owner_id)`、`keys.destroy_key(session, owner_id)`。
- `deprovisioning.request(session, bot, *, trigger, actor_id) -> AssistantLifecycle | None`；`lifecycle.live(session, owner_id, *, lock=True, skip_locked=False)`；`runtime.turn_blocker(session, bot) -> str | None`；`eligibility.refusal(session, user) -> str | None`（`inactive` / `no_login_name` / `not_listed`）。
- `AssistantApproval` 的列见模型文件；`args_enc` 还原后是字典，摘要在键 `abstract`（`approvals.service.listing` 就是这么取的）；`reason_enc` 是文本。
- 部署配置 `Settings.tower_web_url`（`TOWER_WEB_URL`）、`Settings.tower_display_name`（`TOWER_DISPLAY_NAME`），都可不配；`provisioning.approval_system` 只在以 `http://` / `https://` 开头时才把它当链接，本层同样处理。Tower 没有「按编号打开某张单」的地址约定，链接只指向审批系统的网页入口。
- 平台设置：`SETTING_DEFAULTS` 与 `SettingsPatch` 已有 `assistant_eligibility`、`assistant_backends`、`assistant_node_concurrency`（1–32）、`assistant_daily_turn_budget`（10–2000），`GET` / `PUT /api/admin/settings` 已能读写它们；本层后端不改设置接口，只补前端表单。
- `tests/unit/test_assistant_sealed_ledger.py`：`coreman/` 下每一处「取内容表整行的 `select`」都登记在 `ENTITY_SELECTS`，新增一处不登记会失败。
- 测试公共件：`tests.api.conftest.login_as / login_existing`；`tests.integration.assistant_helpers.seed_assistant / seed_standard / SECRET / MASTER_KEY / raw`；`tests.fakes.runtime_node.add_node_relay(session, *, name=…)`（实例名是「节点编号/类型」，节点名是 `name`）。夹具 `assistant_master` 只在 `tests/integration/conftest.py` 里，`tests/api` 下没有（本层 Task 1 加一份）。

**2E（已落地）**

- `delivery.default_budget(session) -> int`、`delivery.turns_used(session, bot_id, zone, today) -> int`、`delivery.budget_left(session, bot, today) -> int`、`delivery.BUDGET_MIN == 10`、`delivery.BUDGET_MAX == 2000`；`delivery.Usage(used, budget)` 与 `async def delivery.usage(session, now) -> dict[uuid.UUID, Usage]`。
- `GET /api/admin/assistants` 的每条记录有 `turns`（`{"today": int, "budget": int}` 或 `None`）与 `inbox_pending`（`int`）。
- 所有者概览里的轮次用 `delivery.turns_used` 与 `delivery.default_budget` 算；管理台列表（Task 13）直接用接口里的 `turns`。

**2F（已落地）**

- `settings.get(session, bot_id) -> AssistantSettings`（没有就建，调用方提交）、`settings.zone(row)`、`settings.local_today(row, now)`、`settings.on_shift(row, now)`、`settings.cold_start_state(row) -> str`（`pending` / `in_progress` / `done`）。
- `def settings.view(row: AssistantSettings, now: datetime) -> dict[str, Any]`：键 `timezone`、`work_start`、`work_end`、`workdays`、`focus_chats`、`ignored_chats`、`on_shift`、`shift_override`、`shift_override_until`、`cold_start`、`today`。
- `async def settings.update(session: AsyncSession, bot_id: uuid.UUID, *, now: datetime, timezone: str | None = None, work_start: str | None = None, work_end: str | None = None, workdays: list[int] | None = None, focus_chats: list[str] | None = None, ignored_chats: list[str] | None = None) -> AssistantSettings`：只改给出的项（`None` = 没给），名单整份替换，只 flush；抛 `settings.SettingsError`（属性 `code`、`detail`），代码 `nothing_to_update`、`invalid_timezone`、`invalid_work_hours`（格式不对，或上班时间不早于下班时间）、`invalid_workdays`、`invalid_chats`、`too_many_chats`、`chat_in_both_lists`、`content_locked`。改了时区、工作时段或工作日时会清掉「上班」「下班」的临时切换。
- `settings.MAX_FOCUS == 5`、`settings.MAX_IGNORED == 50`。`settings.update` 不管每日轮次预算（`assistant_settings.daily_turn_budget`），本层 Task 6 补一个 `owner_view.set_budget`。
- `AssistantSettings` 的冷启动列：`cold_start_started_at`、`cold_start_step`（`voice` / `people` / `duties` / `focus` / `hours` 或空）、`cold_start_outcome`（`completed` / `skipped` / `timeout` 或空）、`cold_start_done_at`。
- `GET /api/admin/assistants` 的每条记录已有 `on_shift`（`bool`：启用、开通好且此刻在班）与 `cold_start`（`{"state", "step", "outcome", "started_at", "done_at"}`，没有设置行的是 `None`）：管理台列表的后端不用再改。
- 2F 的交接里留给 3C、本层没有做的两件事：页面上「上班」「下班」的即时切换（`settings.clock_in` / `clock_out`）与「重新生成口吻页」的入口。见文末「没覆盖到的地方」。

**3A（按其计划，尚未落地）**

- `coreman.core.db.models.AssistantJob`（列见 3A 计划 Task 1）；`sealed.COLUMNS[AssistantJob]` 有 `title_enc`、`brief_enc`、`message_enc`、`result_enc`、`blocked_enc`。
- `async def jobs.list_(session: AsyncSession, bot_id: uuid.UUID, *, scope: str = "active", limit: int = 20) -> list[AssistantJob]`，`scope` 取 `active` / `blocked` / `recent`，新的在前，内容已还原。
- `def jobs.view(row: AssistantJob, *, full: bool = False) -> dict[str, Any]`：键 `id`、`kind`、`status`、`title`、`rounds`、`created_at`、`finished_at`、`has_result`，可能有 `error_code`、`locked`；`full=True` 时另有 `brief`、`result`、`blocked_reason`。
- `async def jobs.cancel(session: AsyncSession, bot_id: uuid.UUID, job_id: uuid.UUID, *, code: str = "cancelled", report: bool = False, now: datetime | None = None) -> AssistantJob`；抛 `jobs.JobError`，`code` 为 `job_not_found`（包括编号是别的助理的）或 `job_closed`。
- `jobs.LIVE == ("queued", "running")`、`jobs.ENDED == ("done", "failed", "cancelled", "blocked")`；`class jobs.JobError(Exception)`，属性 `code: str`。
- `async def jobs.active_counts(session: AsyncSession) -> dict[uuid.UUID, int]`；`GET /api/admin/assistants` 的每条记录多 `jobs_active`（`int`）。
- 测试公共件 `tests/integration/job_helpers.py`：`TITLE`、`BRIEF`、`async def owner_with_key(session, *, chat_id="oc_private", platform_user_id="owner_pid", open_id="ou_owner") -> tuple[Bot, User]`、`async def spawned(session, bot, *, title=TITLE, brief=BRIEF, **kw) -> AssistantJob`、`async def reloaded(session, row) -> AssistantJob`。
- 3A 的交接写明：所有者在页面上取消用 `jobs.cancel(session, bot_id, job_id, code="cancelled_by_owner", report=True)`，让助理在主会话里得知。

## 设计选择

**1. 助理由登录人推出，路径里没有助理编号。** 「猜路径」能猜的只剩子任务编号与待跟进编号，这两个都在核心函数里按 `bot_id` 过滤（`job_not_found` / `followup_not_found`）。管理员想看别人的，没有任何参数可改。输出前仍调 `sealed.open_rows(..., viewer=…)`：将来有人把「取本人的助理」改坏了，内容也出不去。

**2. 哪些状态能读、能写。** 读：记录还 `enabled` 就能读（开通中、可用、停用中）；停用完成后记录不再 `enabled`，接口一律回「没有助理」（概览回空状态，其余 404 `no_assistant`），库里的密文此时也已不可解。写（待跟进、知识页、设置、取消子任务）：只在 `assistant_state = 'active'` 时受理，否则 409 `assistant_not_active`——停用一旦开始，页面上再改什么都没有意义。停用接口不受这条限制。

**3. 错误怎么回。** 核心函数抛的是带 `code` 的异常；路由把它映射成 `ApiError(status, status, <给人看的中文>, [{"type": <code>}])`（与 `personal_credentials._error` 同形）。前端的 `call()` 只保留 `message`，所以中文原话就是界面上的提示；知识页的版本冲突另用业务码 `VERSION_CONFLICT`（40901），前端的 `isVersionConflict` 认得出。

**4. 每日预算的上限。** 所有者可以把自己的预算调低，不能调到平台默认值以上（否则管理员的设置形同虚设）；`null` 表示跟随平台默认。管理员之后把平台默认调到某人自己的值以下时，那个人的值不会被自动收紧（`delivery.budget_left` 以个人值为准）——见文末「没覆盖到的地方」。

**5. 重点群与忽略的会话只收会话 id。** 页面上按群名挑需要一个以所有者身份列会话的接口，那要占 2D 的分钟预算、还要处理限频，本层不做；表单一行一个 `oc_…`，冷启动（2F）的选择卡是主要入口。

**6. 「最近落地」只列三种终态。** spec 写的是已执行、被拒、撤回；`failed` / `stale` / `expired` 的单会作为结果回合回到助理会话，由助理重新提或告诉所有者，不在这个分区重复。

**7. 知识页的路径走查询参数。** 任务书定的接口是 `GET /notes/page?path=`。路径本身是个人内容，会出现在反向代理与应用的访问日志里——见文末「没覆盖到的地方」，上线前需要所有者确认是否改成请求体。

**8. 前端的结构。** 一个页面壳（概览 + 空状态 + 标签页）加六个分区组件，每个分区自己取数、自己有用例；`el-tabs` 的 `lazy` 让没点开的分区不发请求。难以在 jsdom 里可靠操作的控件（下拉选择）通过 `defineExpose` 暴露处理函数给用例调用（`SettingsView.spec.ts` 已有同样的做法）。

## File Structure

| 文件 | 职责 |
|---|---|
| `coreman/core/assistants/access_log.py`（新） | `record(session, bot_id, actor_id, action, target)`：解密访问的唯一调用点，本层只写结构化日志 |
| `coreman/core/assistants/owner_view.py`（新） | 取本人的助理、概览、空状态、审批单的所有者视图、审批系统链接、设置表单的视图与每日预算 |
| `coreman/core/assistants/notes.py` | `Entry` 加 `updated_by` |
| `coreman/api/assistant_deprovision.py`（新） | 「开始停用并在真的开始时记审计」：管理员接口与所有者接口共用 |
| `coreman/api/routers/me_assistant_common.py`（新） | 路由公共件：`no_store`、`Mine`、`mine`、`writable`、`refuse` |
| `coreman/api/routers/me_assistant.py`（新） | 概览、等你出手、子任务、最近落地、设置、停用 |
| `coreman/api/routers/me_assistant_followups.py`（新） | 待跟进的读与增改关 |
| `coreman/api/routers/me_assistant_notes.py`（新） | 知识页的索引、读、写、删 |
| `coreman/api/routers/assistants_admin.py` | 停用改用公共函数 |
| `coreman/api/main.py` | 注册三个路由 |
| `tests/api/conftest.py` | 夹具 `assistant_master` |
| `tests/api/my_assistant_helpers.py`（新） | 用例公共件 |
| `tests/unit/test_assistant_access_log.py`（新） | 访问记录 |
| `tests/api/test_my_assistant_overview.py`、`test_my_assistant_followups.py`、`test_my_assistant_notes.py`、`test_my_assistant_jobs.py`、`test_my_assistant_attention.py`、`test_my_assistant_settings.py`、`test_my_assistant_guard.py`（新） | 各组接口；最后一个是整个前缀的鉴权与隔离台账 |
| `tests/unit/test_assistant_sealed_ledger.py` | 台账登记 `owner_view.py` 的一处整行读取 |
| `web/src/api/myAssistant.ts`、`web/src/api/assistantsAdmin.ts`（新） | 接口封装与类型 |
| `web/src/api/types.ts` | `SettingsOut` 加助理的四个设置项 |
| `web/src/i18n/assistant.ts`（新）、`index.ts`、`zh.ts`、`en.ts`、`ja.ts` | 文案模块与菜单名 |
| `web/src/router/index.ts`、`web/src/layouts/AdminLayout.vue` | 两条路由、两个菜单项 |
| `web/src/views/MyAssistantView.vue`（新） | 页面壳：概览、空状态、标签页 |
| `web/src/components/assistant/AssistantAttention.vue`、`AssistantJobs.vue`、`AssistantRecent.vue`、`AssistantFollowups.vue`、`AssistantNotes.vue`、`AssistantSettings.vue`（新） | 六个分区 |
| `web/src/utils/assistantSettings.ts`（新） | 设置表单的纯函数校验 |
| `web/src/views/AssistantsView.vue`（新） | 管理台的助理列表 |
| `web/src/components/AssistantPlatformSettings.vue`（新）、`web/src/views/SettingsView.vue` | 平台设置页里的助理设置项表单 |
| `web/tests/MyAssistantView.spec.ts`、`AssistantSections.spec.ts`、`AssistantFollowups.spec.ts`、`AssistantNotes.spec.ts`、`AssistantSettings.spec.ts`、`AssistantsView.spec.ts`、`AssistantPlatformSettings.spec.ts`、`assistantI18n.spec.ts`（新）、`SettingsView.spec.ts`、`AdminLayout.spec.ts` | 前端用例 |
| `docs/feishu.md`、`docs/operations.md`、`CHANGELOG.md`、spec、总览 | 文档同步 |

## 接口一览

所有路径都在 `/api/me/assistant` 之下，响应信封 `{"code": 0, "data": …}`。

| 方法 | 路径 | 作用 | 任务 |
|---|---|---|---|
| GET | `` | 概览；没有助理时回空状态与开通指引 | 1 |
| GET | `/followups` | 未结的待跟进，按四类分组 | 2 |
| POST | `/followups` | 新增一条 | 2 |
| PATCH | `/followups/{label}` | 改分类、标题、备注、日期 | 2 |
| POST | `/followups/{label}/close` | 办完或不再需要 | 2 |
| POST | `/followups/{label}/snooze` | 推后到某天 | 2 |
| GET | `/notes` | 知识页索引 | 3 |
| GET | `/notes/page?path=` | 读一页 | 3 |
| PUT | `/notes/page` | 新建或整页替换（含置顶） | 3 |
| DELETE | `/notes/page?path=` | 删除一页 | 3 |
| GET | `/jobs` | 进行中与最近结束的子任务 | 4 |
| POST | `/jobs/{job_id}/cancel` | 取消一个进行中的子任务 | 4 |
| GET | `/attention` | 等你出手：待批的单、卡住的子任务、今天到期的待跟进 | 5 |
| GET | `/approvals/recent` | 最近落地：已执行、被拒、撤回的单 | 5 |
| GET | `/settings` | 所有者的设置 | 6 |
| PATCH | `/settings` | 改工作时段、时区、重点群、忽略的会话、每日预算 | 6 |
| POST | `/deprovision` | 停用（`confirm: true`） | 7 |

---

### Task 1: 路由骨架、访问记录、概览与空状态

**Files:**
- Create: `coreman/core/assistants/access_log.py`
- Create: `coreman/core/assistants/owner_view.py`
- Create: `coreman/api/routers/me_assistant_common.py`
- Create: `coreman/api/routers/me_assistant.py`
- Modify: `coreman/api/main.py`（`app.include_router(assistants_admin.router)` 之后）
- Modify: `tests/api/conftest.py`（导入区；文件末尾加夹具）
- Create: `tests/api/my_assistant_helpers.py`
- Test: `tests/unit/test_assistant_access_log.py`（新建）、`tests/api/test_my_assistant_overview.py`（新建）

**Interfaces:**
- Consumes: `interactive_user`、`verify_csrf`、`mcp_rpc.NO_STORE`、`ApiError`、`records.visible_to`、`settings.get / zone / local_today / on_shift / cold_start_state`、`delivery.turns_used / default_budget`、`lifecycle.live`、`runtime.turn_blocker`、`eligibility.refusal`、`sealed.key_ready`、`runtime_nodes.transport.online`、`get_settings()`。
- Produces：
  - `access_log.ACTIONS: tuple[str, ...]`；`async def access_log.record(session: AsyncSession, bot_id: uuid.UUID, actor_id: uuid.UUID, action: str, target: str = "") -> None`（`action` 不在 `ACTIONS` 里抛 `ValueError`）。
  - `owner_view.HOW_TO = ("open_private_chat", "accept_offer", "authorize")`；`async def owner_view.current(session, user_id: uuid.UUID) -> Bot | None`；`async def owner_view.overview(session, bot: Bot, now: datetime) -> dict[str, Any]`；`async def owner_view.absent(session, user: User) -> dict[str, Any]`；`def owner_view.approval_link() -> str | None`。
  - `me_assistant_common`：`async def no_store(response: Response) -> None`；`@dataclass(frozen=True) class Mine`（`actor: User`、`bot: Bot`）；`async def mine(...) -> Mine`（没有助理 → 404 `no_assistant`）；`async def writable(own: Mine) -> Mine`（不是 `active` → 409 `assistant_not_active`）；`def refuse(code: str, table: Mapping[str, tuple[int, str]]) -> ApiError`；`PREFIX = "/api/me/assistant"`；`DEPENDENCIES`（`[Depends(verify_csrf), Depends(no_store)]`）。
  - `GET /api/me/assistant`。
  - 测试夹具 `assistant_master`（`tests/api/conftest.py`）；`tests.api.my_assistant_helpers`：`URL`、`async def owned(client, db_session, *, role="member") -> tuple[Bot, User]`、`def as_bot_token(client, monkeypatch, user) -> None`、`def code(response) -> str | None`。

- [ ] **Step 1: 写测试公共件**

`tests/api/conftest.py`：把 `from collections.abc import AsyncIterator` 换成 `from collections.abc import AsyncIterator, Iterator`，文件末尾追加：

```python
@pytest.fixture
def assistant_master() -> Iterator[None]:
    """个人助理加密存储的平台主密钥：与接口进程从配置里取到的是同一把；清掉进程内的缓存。"""
    from coreman.core.assistants import keys, sealed
    from coreman.core.crypto import Cipher

    sealed.use_master(Cipher(base64.b64decode(MASTER_KEY)))
    keys.forget()
    sealed.forget()
    yield
    sealed.use_master(None)
    keys.forget()
    sealed.forget()
```

`tests/api/my_assistant_helpers.py`（新建）：

```python
"""「我的助理」接口用例的公共件：一位登录着的所有者、机器人令牌的替身、错误代码。"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import Bot, User
from tests.api.conftest import login_existing
from tests.integration.assistant_helpers import seed_assistant

URL = "/api/me/assistant"


async def owned(
    client: httpx.AsyncClient, db_session: AsyncSession, *, role: str = "member"
) -> tuple[Bot, User]:
    """一位开通好、有数据密钥的助理，并以所有者登录；`role` 是所有者在平台里的角色。"""
    bot, owner = await seed_assistant(db_session)
    owner.role = role
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    await db_session.commit()
    await login_existing(client, db_session, owner)
    return bot, owner


def as_bot_token(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, user: User) -> None:
    """让之后的请求以一枚有效的机器人令牌认证成 `user`（没有交互登录会话）。"""
    from coreman.api import bot_auth

    async def token_user(request, session):  # type: ignore[no-untyped-def]
        request.state.bot_token_authenticated = True
        request.state.admin_session = None
        return user

    monkeypatch.setattr(bot_auth, "token_user", token_user)
    client.cookies.set("bot_token", "valid-human-bot-token")


def code(response: httpx.Response) -> str | None:
    """错误信封里的原因代码（`errors[0].type`）。"""
    errors = response.json().get("errors") or [{}]
    found = errors[0].get("type")
    return found if isinstance(found, str) else None
```

- [ ] **Step 2: 写失败测试**

`tests/unit/test_assistant_access_log.py`（新建）：

```python
"""解密访问的记录：只有编号、动作与对象代号；不认识的动作直接报错。"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from coreman.core.assistants import access_log

BOT, ACTOR = uuid.uuid4(), uuid.uuid4()


async def test_a_read_is_logged_with_ids_action_and_target_only() -> None:
    with capture_logs() as logs:
        await access_log.record(AsyncMock(), BOT, ACTOR, "followups.read", "open")
    assert logs == [
        {
            "event": "assistant_content_access",
            "log_level": "info",
            "bot_id": str(BOT),
            "actor_id": str(ACTOR),
            "action": "followups.read",
            "target": "open",
        }
    ]


async def test_the_target_is_cut_and_unknown_actions_are_rejected() -> None:
    with capture_logs() as logs:
        await access_log.record(AsyncMock(), BOT, ACTOR, "jobs.read", "x" * 500)
    assert len(logs[0]["target"]) == access_log.MAX_TARGET
    with pytest.raises(ValueError, match="unknown access action"):
        await access_log.record(AsyncMock(), BOT, ACTOR, "pages.dump", "")
```

`tests/api/test_my_assistant_overview.py`（新建）：

```python
"""「我的助理」概览：空状态与开通指引；所有者看到状态、运行时、在班、轮次与轮询健康。"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys, settings
from coreman.core.db.models import AssistantWatch, Bot, ChatLog, PlatformApp, Setting
from coreman.core.timeutils import utcnow
from tests.api.conftest import login_as
from tests.api.my_assistant_helpers import URL, as_bot_token, code, owned
from tests.fakes.runtime_node import add_node_relay
from tests.integration.assistant_helpers import SECRET

pytestmark = pytest.mark.usefixtures("assistant_master")
SEEN = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)


def _turn(bot: Bot) -> ChatLog:
    return ChatLog(
        bot_id=bot.id,
        bot_key=bot.bot_key,
        platform="feishu",
        chat_type="single",
        message_type="text",
        status="success",
        request_at=utcnow(),
    )


async def test_without_an_assistant_the_page_says_how_to_get_one(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    db_session.add(
        PlatformApp(
            platform="feishu",
            name="个人助理",
            capabilities=["assistant"],
            app_id="cli_example",
            secret_enc="x",
        )
    )
    await login_as(client, db_session, role="member")
    response = await client.get(URL)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["data"] == {
        "exists": False,
        "eligible": True,
        "reason": None,
        "how_to": {
            "app_name": "个人助理",
            "steps": ["open_private_chat", "accept_offer", "authorize"],
        },
        "lifecycle": None,
    }


async def test_the_empty_state_says_when_the_member_is_out_of_scope(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    db_session.add(
        Setting(
            key="assistant_eligibility",
            value={"mode": "listed", "department_ids": [], "user_ids": []},
        )
    )
    await login_as(client, db_session, role="member")
    data = (await client.get(URL)).json()["data"]
    assert data["exists"] is False and data["eligible"] is False
    assert data["reason"] == "not_listed" and data["how_to"]["app_name"] is None


async def test_the_owner_sees_state_runtime_shift_turns_and_watch(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    relay = await add_node_relay(db_session, name="pool-1")
    bot.relay_server_id = relay.id
    bot.description = SECRET
    prefs = await settings.get(db_session, bot.id)
    prefs.shift_override, prefs.daily_turn_budget = "on", 50
    db_session.add(AssistantWatch(bot_id=bot.id, state="polling", last_ok_at=SEEN))
    db_session.add_all([_turn(bot) for _ in range(3)])
    await db_session.commit()
    response = await client.get(URL)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert data["exists"] is True and data["bot_id"] == str(bot.id)
    assert data["name"] == "个人助理" and data["state"] == "active"
    assert data["runtime"]["name"] == "pool-1" and isinstance(data["runtime"]["online"], bool)
    assert data["on_shift"] is True
    assert data["cold_start"] == {"state": "pending", "step": None, "outcome": None}
    assert data["turns"] == {"today": 3, "budget": 50}
    assert data["watch"] == {
        "state": "polling",
        "last_ok_at": SEEN.isoformat(),
        "last_error": None,
    }
    assert data["lifecycle"] is None and data["content_locked"] is False
    assert data["approval_system"] == {"name": None, "url": None}
    assert "paused_reason" in data
    assert SECRET not in response.text and "/w" not in response.text


async def test_the_overview_uses_the_platform_budget_and_names_the_approval_system(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coreman.core.config import reset_settings_cache

    monkeypatch.setenv("TOWER_DISPLAY_NAME", "Example Tower")
    monkeypatch.setenv("TOWER_WEB_URL", "https://tower.example.com")
    reset_settings_cache()
    _bot, owner = await owned(client, db_session)
    db_session.add(Setting(key="assistant_daily_turn_budget", value=80))
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    data = (await client.get(URL)).json()["data"]
    assert data["turns"] == {"today": 0, "budget": 80}
    assert data["runtime"] is None and data["watch"] is None
    assert data["content_locked"] is True
    assert data["approval_system"] == {
        "name": "Example Tower",
        "url": "https://tower.example.com",
    }


async def test_a_link_that_is_not_http_is_not_given(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coreman.core.config import reset_settings_cache

    monkeypatch.setenv("TOWER_WEB_URL", "javascript:alert(1)")
    reset_settings_cache()
    await owned(client, db_session)
    assert (await client.get(URL)).json()["data"]["approval_system"]["url"] is None


async def test_another_member_sees_only_their_own_empty_state(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await login_as(client, db_session, role="platform_admin")
    response = await client.get(URL)
    assert response.json()["data"]["exists"] is False
    assert str(bot.id) not in response.text


async def test_a_stopped_assistant_is_no_assistant(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    bot.enabled, bot.assistant_state = False, "deprovisioned"
    await db_session.commit()
    assert (await client.get(URL)).json()["data"]["exists"] is False


async def test_a_bot_token_is_refused(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bot, owner = await owned(client, db_session)
    as_bot_token(client, monkeypatch, owner)
    response = await client.get(URL)
    assert response.status_code == 403 and code(response) is None


async def test_signed_out_is_401(client: httpx.AsyncClient) -> None:
    assert (await client.get(URL)).status_code == 401
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_access_log.py tests/api/test_my_assistant_overview.py -q`
Expected: FAIL：`ModuleNotFoundError: No module named 'coreman.core.assistants.access_log'`；接口用例 404（路由还没有）。

- [ ] **Step 4: 实现访问记录**

`coreman/core/assistants/access_log.py`（新建）：

```python
"""个人内容的访问记录：运行时路径之外的每一次解密都经这里（「我的助理」页的读取）。

本层只写一条结构化应用日志；3D 把 `record` 的函数体换成写 `assistant_access_log` 表，调用点
不动（所以这里已经收下 `session`）。记录里只有编号、动作与对象的代号：`target` 只能是明文的
编号或范围名（`F012`、子任务编号、`open`），知识页的路径本身是个人内容，调用方不要传进来。
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.logging import get_logger

log = get_logger(__name__)

ACTIONS = (
    "approvals.read",
    "followups.read",
    "jobs.read",
    "notes.index",
    "notes.read",
    "settings.read",
)
MAX_TARGET = 64


async def record(
    session: AsyncSession,
    bot_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: str,
    target: str = "",
) -> None:
    """记一次读取。调用方在返回内容之前调用；与读取在同一个请求里。

    Args:
        session: 这次请求的会话（3D 用它落库；本层不用）
        action: `ACTIONS` 之一
        target: 明文的编号或范围名，超长截断

    Raises:
        ValueError: 不认识的动作（写错的调用点要在测试里就失败）
    """
    del session
    if action not in ACTIONS:
        raise ValueError(f"unknown access action: {action}")
    log.info(
        "assistant_content_access",
        bot_id=str(bot_id),
        actor_id=str(actor_id),
        action=action,
        target=target[:MAX_TARGET],
    )
```

- [ ] **Step 5: 实现所有者视图**

`coreman/core/assistants/owner_view.py`（新建）：

```python
"""「我的助理」页的读取：本人的助理是哪一条、概览、没有助理时的开通指引。

只给所有者本人用：调用方（`/api/me/assistant` 的路由）传进来的 `bot` 一律由登录人推出。概览只有
元数据——状态、运行时的名字、是否在班、轮次、轮询健康——不读任何内容列。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import delivery, eligibility, lifecycle, runtime, sealed
from coreman.core.assistants import settings as assistant_settings
from coreman.core.config import get_settings
from coreman.core.db.models import (
    AssistantLifecycle,
    AssistantWatch,
    Bot,
    PlatformApp,
    RelayServer,
    RuntimeNode,
    User,
)
from coreman.core.runtime_nodes.transport import online

# 开通的三步，页面按这些代号显示文案：私聊助理应用 → 点卡片上的「开通」→ 完成飞书授权。
HOW_TO = ("open_private_chat", "accept_offer", "authorize")


async def current(session: AsyncSession, user_id: uuid.UUID) -> Bot | None:
    """这个人现在的助理记录：还启用着的那一条（部分唯一索引保证至多一条）。

    停用的最后一步才把记录置为不启用，所以开通中、停用中的记录都算；停用完成的不算。
    """
    found: Bot | None = await session.scalar(
        select(Bot)
        .where(Bot.kind == "assistant", Bot.owner_id == user_id, Bot.enabled.is_(True))
        .order_by(Bot.created_at.desc())
        .limit(1)
    )
    return found


def approval_link() -> str | None:
    """审批系统的网页入口；没配、或配的不是 http(s) 地址时不给。"""
    url = (get_settings().tower_web_url or "").strip()
    return url if url.startswith(("https://", "http://")) else None


def _flow(row: AssistantLifecycle | None) -> dict[str, Any] | None:
    return None if row is None else {"action": row.action, "step": row.step}


async def overview(session: AsyncSession, bot: Bot, now: datetime) -> dict[str, Any]:
    """所有者看到的概览。会按默认值建设置行，调用方提交。"""
    prefs = await assistant_settings.get(session, bot.id)
    own = prefs.daily_turn_budget
    used = await delivery.turns_used(
        session, bot.id, assistant_settings.zone(prefs), assistant_settings.local_today(prefs, now)
    )
    relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    node = (
        await session.get(RuntimeNode, relay.runtime_node_id)
        if relay is not None and relay.runtime_node_id
        else None
    )
    # 只取状态、时间与错误代码三列：游标与上一轮的会话顺序不取。
    watch = (
        await session.execute(
            select(AssistantWatch.state, AssistantWatch.last_ok_at, AssistantWatch.last_error).where(
                AssistantWatch.bot_id == bot.id
            )
        )
    ).first()
    flow = await lifecycle.live(session, bot.owner_id, lock=False) if bot.owner_id else None
    name = (get_settings().tower_display_name or "").strip() or None
    return {
        "exists": True,
        "bot_id": str(bot.id),
        "name": bot.name,
        "state": bot.assistant_state,
        "runtime": (
            {
                "name": node.name if node is not None else relay.name,
                "online": node is not None and online(node),
            }
            if relay is not None
            else None
        ),
        # 可用状态下为什么现在跑不了轮次（运行时离线、不满足隔离……）；能跑是 None。
        "paused_reason": (
            await runtime.turn_blocker(session, bot) if bot.assistant_state == "active" else None
        ),
        "on_shift": assistant_settings.on_shift(prefs, now),
        "cold_start": {
            "state": assistant_settings.cold_start_state(prefs),
            "step": prefs.cold_start_step,
            "outcome": prefs.cold_start_outcome,
        },
        "turns": {
            "today": used,
            "budget": own if own is not None else await delivery.default_budget(session),
        },
        "watch": (
            {
                "state": watch[0],
                "last_ok_at": watch[1].isoformat() if watch[1] else None,
                "last_error": watch[2],
            }
            if watch is not None
            else None
        ),
        "lifecycle": _flow(flow),
        "content_locked": not await sealed.key_ready(session, bot.id),
        "approval_system": {"name": name, "url": approval_link()},
    }


async def absent(session: AsyncSession, user: User) -> dict[str, Any]:
    """没有助理时页面显示的内容：能不能开通、不能的原因代号、怎么开通。"""
    reason = await eligibility.refusal(session, user)
    app_name = (
        await session.scalar(
            select(PlatformApp.name)
            .where(
                PlatformApp.platform == "feishu",
                PlatformApp.enabled.is_(True),
                PlatformApp.capabilities.contains(["assistant"]),
                PlatformApp.app_id.is_not(None),
            )
            .order_by(PlatformApp.name)
            .limit(1)
        )
        if reason is None
        else None
    )
    flow = await lifecycle.live(session, user.id, lock=False)
    return {
        "exists": False,
        "eligible": reason is None,
        "reason": reason,
        "how_to": {"app_name": app_name, "steps": list(HOW_TO)},
        "lifecycle": _flow(flow),
    }
```

- [ ] **Step 6: 实现路由公共件与概览接口**

`coreman/api/routers/me_assistant_common.py`（新建）：

```python
"""`/api/me/assistant` 各路由文件的公共件：前缀、路由级依赖、「取本人的助理」、错误映射。

助理一律由登录人推出：这些接口没有任何「助理编号」参数，管理员也只能拿到自己的那一条。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import mcp_rpc
from coreman.api.deps import get_session
from coreman.api.errors import ApiError
from coreman.api.routers.feishu_personal import interactive_user
from coreman.api.security import verify_csrf
from coreman.core.assistants import owner_view
from coreman.core.assistants.records import visible_to
from coreman.core.db.models import Bot, User

PREFIX = "/api/me/assistant"


async def no_store(response: Response) -> None:
    """个人内容不进任何缓存。"""
    response.headers.update(mcp_rpc.NO_STORE)


# 路由级依赖：写操作先过 CSRF；成功的响应都带 no-store。
DEPENDENCIES = [Depends(verify_csrf), Depends(no_store)]


@dataclass(frozen=True)
class Mine:
    """登录人与他自己的助理记录。"""

    actor: User
    bot: Bot


async def mine(
    actor: User = Depends(interactive_user), session: AsyncSession = Depends(get_session)
) -> Mine:
    bot = await owner_view.current(session, actor.id)
    if bot is None or not visible_to(bot, actor):
        raise ApiError(404, 404, "你还没有个人助理", [{"type": "no_assistant"}])
    return Mine(actor, bot)


async def writable(own: Mine = Depends(mine)) -> Mine:
    """页面上的修改只在助理可用时受理：开通没走完、正在停用时改什么都没有意义。"""
    if own.bot.assistant_state != "active":
        raise ApiError(
            409, 409, "个人助理还没开通好或正在停用，暂时不能修改", [{"type": "assistant_not_active"}]
        )
    return own


def refuse(code: str, table: Mapping[str, tuple[int, str]]) -> ApiError:
    """核心模块给的原因代码 → 接口错误：状态码与给人看的原话查表，代码放进 `errors[0].type`。"""
    status, message = table.get(code, (400, "这次操作没有完成"))
    return ApiError(status, status, message, [{"type": code}])
```

`coreman/api/routers/me_assistant.py`（新建）：

```python
"""「我的助理」页的接口：概览、等你出手、子任务、最近落地、设置、停用。

只对所有者本人：要求交互登录会话（机器人令牌 403），助理由登录人推出。读取密文的接口在返回前
记一次访问（`access_log.record`）。待跟进与知识页在 `me_assistant_followups.py`、
`me_assistant_notes.py`。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.routers.feishu_personal import interactive_user
from coreman.api.routers.me_assistant_common import DEPENDENCIES, PREFIX
from coreman.core.assistants import owner_view
from coreman.core.db.models import User
from coreman.core.timeutils import utcnow

router = APIRouter(prefix=PREFIX, tags=["my-assistant"], dependencies=DEPENDENCIES)


@router.get("")
async def overview(
    actor: User = Depends(interactive_user), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """概览；没有助理时回空状态与开通指引（不是 404：页面要据此显示怎么开通）。"""
    bot = await owner_view.current(session, actor.id)
    data = (
        await owner_view.absent(session, actor)
        if bot is None
        else await owner_view.overview(session, bot, utcnow())
    )
    # 概览会按默认值建设置行。
    await session.commit()
    return {"code": 0, "data": data}
```

`coreman/api/main.py`：在

```python
    from coreman.api.routers import assistants_admin

    app.include_router(assistants_admin.router)
```

之后加

```python
    from coreman.api.routers import me_assistant

    app.include_router(me_assistant.router)
```

- [ ] **Step 7: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_access_log.py tests/api/test_my_assistant_overview.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: PASS（台账不变：本任务没有取内容表整行的 `select`）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 8: 提交**

```bash
git add coreman/core/assistants/access_log.py coreman/core/assistants/owner_view.py \
  coreman/api/routers/me_assistant_common.py coreman/api/routers/me_assistant.py coreman/api/main.py \
  tests/api/conftest.py tests/api/my_assistant_helpers.py \
  tests/unit/test_assistant_access_log.py tests/api/test_my_assistant_overview.py
git commit -m "我的助理接口的骨架：概览与没有助理时的开通指引，只认交互登录的所有者本人"
```

---

### Task 2: 待跟进的读与增改关

**Files:**
- Create: `coreman/api/routers/me_assistant_followups.py`
- Modify: `coreman/api/main.py`（Task 1 加的两行之后）
- Test: `tests/api/test_my_assistant_followups.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `PREFIX`、`DEPENDENCIES`、`Mine`、`mine`、`writable`、`refuse`、`access_log.record`；`followups.add / update / snooze / close / list_ / get / view / parse_label / KEEP / MAX_OPEN / MAX_TITLE / MAX_NOTES / FollowupError`；`settings.get / local_today`；`sealed.open_rows`；`FOLLOWUP_CATEGORIES`。
- Produces：
  - `GET /api/me/assistant/followups` → `{"today": "YYYY-MM-DD", "groups": {"decide": [...], "promised": [...], "waiting": [...], "remind": [...]}, "total": int}`；每条是 `followups.view(row)` 加 `due_today: bool`。
  - `POST /api/me/assistant/followups`（`{category, title, notes?, review_on?, due_on?}`）、`PATCH /api/me/assistant/followups/{label}`（同样的键，全部可省；`notes` / `review_on` / `due_on` 给 `null` 是清空）、`POST …/{label}/close`（`{dropped?: bool}`）、`POST …/{label}/snooze`（`{until: "YYYY-MM-DD"}`），都返回改后的那一条。
  - `me_assistant_followups.ERRORS: dict[str, tuple[int, str]]`。
  - `assistant_followups` 没有 `updated_by` 列，本层不加：所有者在页面上改的条目与助理改的不作区分。

- [ ] **Step 1: 写失败测试**

`tests/api/test_my_assistant_followups.py`（新建）：

```python
"""「我的助理」页的待跟进：所有者按四类看到自己的条目，可以新增、改、推后、关闭；别人碰不到。"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import followups, keys
from coreman.core.db.models import AssistantFollowup, Bot
from tests.api.my_assistant_helpers import URL, as_bot_token, code, owned
from tests.integration.assistant_helpers import SECRET, raw

pytestmark = pytest.mark.usefixtures("assistant_master")
ITEMS = f"{URL}/followups"
STORED = "SELECT string_agg(title_enc, chr(32)) FROM assistant_followups WHERE bot_id = :bot"
COUNT = "SELECT count(*) FROM assistant_followups WHERE bot_id = :bot"


async def _seed(db_session: AsyncSession, bot: Bot) -> None:
    await followups.add(
        db_session, bot.id, category="promised", title=f"给李四报价 {SECRET}", notes="他周一问的"
    )
    await followups.add(
        db_session, bot.id, category="remind", title="续约", due_on=date(2000, 1, 1)
    )
    await db_session.commit()


async def _row(db_session: AsyncSession, bot: Bot, seq: int) -> AssistantFollowup:
    db_session.expire_all()
    return await followups.get(db_session, bot.id, seq)


async def test_the_owner_sees_open_items_in_four_groups(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owned(client, db_session)
    await _seed(db_session, bot)
    with capture_logs() as logs:
        response = await client.get(ITEMS)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert list(data["groups"]) == ["decide", "promised", "waiting", "remind"]
    assert data["total"] == 2 and data["groups"]["decide"] == []
    [promised] = data["groups"]["promised"]
    assert promised["id"] == "F001" and promised["title"] == f"给李四报价 {SECRET}"
    assert promised["notes"] == "他周一问的" and promised["due_today"] is False
    [remind] = data["groups"]["remind"]
    assert remind["id"] == "F002" and remind["due_today"] is True
    assert [
        (entry["action"], entry["target"], entry["actor_id"])
        for entry in logs
        if entry["event"] == "assistant_content_access"
    ] == [("followups.read", "open", str(owner.id))]
    assert SECRET not in str(logs)


async def test_the_owner_adds_edits_snoozes_and_closes(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    added = await client.post(
        ITEMS, json={"category": "waiting", "title": f"  等张三回复 {SECRET} ", "due_on": "2099-03-01"}
    )
    assert added.status_code == 200, added.text
    item = added.json()["data"]
    assert item["id"] == "F001" and item["title"] == f"等张三回复 {SECRET}"
    assert item["category"] == "waiting" and item["due_on"] == "2099-03-01"
    assert SECRET not in (await raw(db_session, STORED, bot=bot.id))

    patched = await client.patch(
        f"{ITEMS}/F001", json={"category": "decide", "notes": "改成我来定", "due_on": None}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["category"] == "decide"
    assert patched.json()["data"]["notes"] == "改成我来定"
    assert patched.json()["data"]["due_on"] is None
    assert patched.json()["data"]["title"] == f"等张三回复 {SECRET}"

    snoozed = await client.post(f"{ITEMS}/f1/snooze", json={"until": "2099-01-01"})
    assert snoozed.json()["data"]["status"] == "snoozed"
    assert snoozed.json()["data"]["review_on"] == "2099-01-01"
    # 推后的条目仍是未结的：列表里还在。
    assert (await client.get(ITEMS)).json()["data"]["total"] == 1

    closed = await client.post(f"{ITEMS}/F001/close", json={"dropped": True})
    assert closed.json()["data"]["status"] == "dropped"
    assert (await client.get(ITEMS)).json()["data"]["total"] == 0
    assert (await _row(db_session, bot, 1)).status == "dropped"
    # 已经关了的再关一次原样返回；再改就拒绝。
    assert (await client.post(f"{ITEMS}/F001/close", json={})).json()["data"]["status"] == "dropped"
    again = await client.patch(f"{ITEMS}/F001", json={"title": "x"})
    assert again.status_code == 409 and code(again) == "followup_closed"


@pytest.mark.parametrize(
    ("method", "path", "body", "status", "reason"),
    [
        ("post", "", {"category": "someday", "title": "x"}, 422, "invalid_category"),
        ("post", "", {"category": "remind", "title": "   "}, 422, "invalid_title"),
        ("post", "", {"category": "remind", "title": "x", "extra": 1}, 422, None),
        ("patch", "/F001", {}, 422, "nothing_to_change"),
        ("patch", "/F001", {"title": None}, 422, "null_not_allowed"),
        ("patch", "/F001", {"category": None}, 422, "null_not_allowed"),
        ("patch", "/F099", {"title": "x"}, 404, "followup_not_found"),
        ("patch", "/nonsense", {"title": "x"}, 404, "followup_not_found"),
        ("post", "/F001/snooze", {"until": "2000-01-01"}, 422, "snooze_not_future"),
        ("post", "/F001/snooze", {"until": "明天"}, 422, None),
        ("post", "/F001/close", {"dropped": "yes"}, 422, None),
    ],
)
async def test_bad_requests_are_refused_with_a_reason(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    method: str,
    path: str,
    body: dict[str, object],
    status: int,
    reason: str | None,
) -> None:
    bot, _owner = await owned(client, db_session)
    await _seed(db_session, bot)
    response = await client.request(method, f"{ITEMS}{path}", json=body)
    assert response.status_code == status, response.text
    assert code(response) == reason
    row = await _row(db_session, bot, 1)
    assert row.status == "open" and row.title_enc == f"给李四报价 {SECRET}"
    assert await raw(db_session, COUNT, bot=bot.id) == 2


async def test_writes_need_csrf_and_an_interactive_login(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, owner = await owned(client, db_session)
    await _seed(db_session, bot)
    csrf = client.headers.pop("X-CSRF-Token")
    for method, path, body in (
        ("post", "", {"category": "remind", "title": "x"}),
        ("patch", "/F001", {"title": "x"}),
        ("post", "/F001/close", {}),
        ("post", "/F001/snooze", {"until": "2099-01-01"}),
    ):
        assert (await client.request(method, f"{ITEMS}{path}", json=body)).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    as_bot_token(client, monkeypatch, owner)
    assert (await client.get(ITEMS)).status_code == 403
    assert (await client.post(f"{ITEMS}/F001/close", json={})).status_code == 403
    assert (await _row(db_session, bot, 1)).status == "open"
    assert await raw(db_session, COUNT, bot=bot.id) == 2


async def test_an_admin_with_their_own_assistant_cannot_reach_the_owners_items(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _seed(db_session, bot)
    await owned(client, db_session, role="platform_admin")
    listed = await client.get(ITEMS)
    assert listed.json()["data"]["total"] == 0 and SECRET not in listed.text
    for response in (
        await client.patch(f"{ITEMS}/F001", json={"title": "改掉"}),
        await client.post(f"{ITEMS}/F001/close", json={}),
        await client.post(f"{ITEMS}/F001/snooze", json={"until": "2099-01-01"}),
    ):
        assert response.status_code == 404 and SECRET not in response.text
    row = await _row(db_session, bot, 1)
    assert row.status == "open" and row.title_enc == f"给李四报价 {SECRET}"


async def test_after_the_key_is_destroyed_items_show_as_locked_and_cannot_be_written(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owned(client, db_session)
    await _seed(db_session, bot)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    response = await client.get(ITEMS)
    assert response.status_code == 200 and SECRET not in response.text
    [promised] = response.json()["data"]["groups"]["promised"]
    assert promised["locked"] is True and promised["title"] == "" and promised["notes"] is None
    refused = await client.post(ITEMS, json={"category": "remind", "title": "x"})
    assert refused.status_code == 409 and code(refused) == "content_locked"
    # 只动元数据的操作不需要密钥。
    assert (await client.post(f"{ITEMS}/F001/close", json={})).status_code == 200


async def test_reading_still_works_while_stopping_but_writing_does_not(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _seed(db_session, bot)
    bot.assistant_state = "deprovisioning"
    await db_session.commit()
    assert (await client.get(ITEMS)).json()["data"]["total"] == 2
    refused = await client.post(f"{ITEMS}/F001/close", json={})
    assert refused.status_code == 409 and code(refused) == "assistant_not_active"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_my_assistant_followups.py -q`
Expected: FAIL：全部用例 404 / 405（路由还没有）。

- [ ] **Step 3: 实现**

`coreman/api/routers/me_assistant_followups.py`（新建）：

```python
"""「我的助理」页的待跟进：所有者按四类看，直接在页面上新增、改、推后、关闭。

核心逻辑全在 `coreman/core/assistants/followups.py`，这里只做入参、所有者时区的「今天」、错误
映射与提交。待跟进没有「谁改的」一列：所有者改的与助理改的不作区分。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.routers.me_assistant_common import (
    DEPENDENCIES,
    PREFIX,
    Mine,
    mine,
    refuse,
    writable,
)
from coreman.core.assistants import access_log, followups, sealed
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.followups import FollowupError
from coreman.core.db.models import FOLLOWUP_CATEGORIES, AssistantFollowup
from coreman.core.timeutils import utcnow

router = APIRouter(prefix=PREFIX + "/followups", tags=["my-assistant"], dependencies=DEPENDENCIES)

ERRORS: dict[str, tuple[int, str]] = {
    "followup_not_found": (404, "这条待跟进不存在"),
    "followup_closed": (409, "这条待跟进已经关闭"),
    "invalid_category": (422, "分类只能是：等我决定、我答应的、等别人、提醒我"),
    "invalid_title": (422, f"标题不能为空，且不超过 {followups.MAX_TITLE} 字"),
    "invalid_notes": (422, f"备注不超过 {followups.MAX_NOTES} 字"),
    "too_many_followups": (409, f"未结的待跟进已达 {followups.MAX_OPEN} 条上限，先关掉一些"),
    "snooze_not_future": (422, "推后的日期必须晚于今天"),
    "content_locked": (409, "加密保存的个人内容已不可读取，不能再写入"),
    "nothing_to_change": (422, "至少修改一项"),
    "null_not_allowed": (422, "分类与标题不能清空"),
}


class AddIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(max_length=32)
    # 长度由核心函数按去掉首尾空白之后的字数判断，这里只挡住离谱的请求体。
    title: str = Field(max_length=followups.MAX_TITLE * 4)
    notes: str | None = Field(default=None, max_length=followups.MAX_NOTES * 4)
    review_on: date | None = None
    due_on: date | None = None


class PatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str | None = Field(default=None, max_length=32)
    title: str | None = Field(default=None, max_length=followups.MAX_TITLE * 4)
    notes: str | None = Field(default=None, max_length=followups.MAX_NOTES * 4)
    review_on: date | None = None
    due_on: date | None = None


class CloseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dropped: StrictBool = False


class SnoozeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    until: date


async def _today(session: AsyncSession, own: Mine) -> date:
    """所有者时区里的今天：到期与推后都按它算。"""
    prefs = await assistant_settings.get(session, own.bot.id)
    return assistant_settings.local_today(prefs, utcnow())


def _item(row: AssistantFollowup, today: date) -> dict[str, Any]:
    out = followups.view(row)
    out["due_today"] = row.status == "open" and any(
        day is not None and day <= today for day in (row.review_on, row.due_on)
    )
    return out


def _seq(label: str) -> int:
    try:
        return followups.parse_label(label)
    except FollowupError as exc:
        raise refuse(exc.code, ERRORS) from None


@router.get("")
async def listed(
    own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    today = await _today(session, own)
    rows = await followups.list_(
        session, own.bot.id, "open", today=today, limit=followups.MAX_OPEN
    )
    # 核心函数已经还原过；这里按查看者再核一次，不是所有者本人就全部收回。
    await sealed.open_rows(session, rows, viewer=own.actor.id)
    groups: dict[str, list[dict[str, Any]]] = {category: [] for category in FOLLOWUP_CATEGORIES}
    for row in rows:
        groups[row.category].append(_item(row, today))
    await access_log.record(session, own.bot.id, own.actor.id, "followups.read", "open")
    # 列表会把到了回看日的推后条目改回未结，并可能按默认值建设置行。
    await session.commit()
    return {
        "code": 0,
        "data": {"today": today.isoformat(), "groups": groups, "total": len(rows)},
    }


@router.post("")
async def add(
    body: AddIn, own: Mine = Depends(writable), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    today = await _today(session, own)
    try:
        row = await followups.add(
            session,
            own.bot.id,
            category=body.category,
            title=body.title,
            notes=body.notes,
            review_on=body.review_on,
            due_on=body.due_on,
        )
    except FollowupError as exc:
        await session.rollback()
        raise refuse(exc.code, ERRORS) from None
    data = _item(row, today)
    await session.commit()
    return {"code": 0, "data": data}


@router.patch("/{label}")
async def patch(
    label: str,
    body: PatchIn,
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    seq = _seq(label)
    changes = body.model_dump(include=body.model_fields_set)
    if not changes:
        raise refuse("nothing_to_change", ERRORS)
    if any(key in changes and changes[key] is None for key in ("category", "title")):
        raise refuse("null_not_allowed", ERRORS)
    today = await _today(session, own)
    try:
        row = await followups.update(
            session,
            own.bot.id,
            seq,
            category=changes.get("category"),
            title=changes.get("title"),
            notes=changes.get("notes", followups.KEEP),
            review_on=changes.get("review_on", followups.KEEP),
            due_on=changes.get("due_on", followups.KEEP),
        )
    except FollowupError as exc:
        await session.rollback()
        raise refuse(exc.code, ERRORS) from None
    data = _item(row, today)
    await session.commit()
    return {"code": 0, "data": data}


@router.post("/{label}/close")
async def close(
    label: str,
    body: CloseIn,
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    seq = _seq(label)
    today = await _today(session, own)
    try:
        row = await followups.close(session, own.bot.id, seq, dropped=body.dropped)
    except FollowupError as exc:
        await session.rollback()
        raise refuse(exc.code, ERRORS) from None
    data = _item(row, today)
    await session.commit()
    return {"code": 0, "data": data}


@router.post("/{label}/snooze")
async def snooze(
    label: str,
    body: SnoozeIn,
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    seq = _seq(label)
    today = await _today(session, own)
    try:
        row = await followups.snooze(session, own.bot.id, seq, body.until, today=today)
    except FollowupError as exc:
        await session.rollback()
        raise refuse(exc.code, ERRORS) from None
    data = _item(row, today)
    await session.commit()
    return {"code": 0, "data": data}
```

`coreman/api/main.py`：把 Task 1 加的

```python
    from coreman.api.routers import me_assistant

    app.include_router(me_assistant.router)
```

换成

```python
    from coreman.api.routers import me_assistant, me_assistant_followups

    app.include_router(me_assistant.router)
    app.include_router(me_assistant_followups.router)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/api/test_my_assistant_followups.py tests/api/test_my_assistant_overview.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: PASS。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/api/routers/me_assistant_followups.py coreman/api/main.py \
  tests/api/test_my_assistant_followups.py
git commit -m "我的助理接口：待跟进按四类列出，所有者可以新增、改、推后与关闭"
```

---

### Task 3: 知识页的索引、读、写、删

**Files:**
- Modify: `coreman/core/assistants/notes.py`（`Entry`、`_entry`、`read`）
- Create: `coreman/api/routers/me_assistant_notes.py`
- Modify: `coreman/api/main.py`
- Test: `tests/api/test_my_assistant_notes.py`（新建）

**Interfaces:**
- Consumes: Task 1 的路由公共件与 `access_log.record`；`notes.index / read / write / delete / NotesError / MAX_PAGE_BYTES / MAX_PATH_CHARS / MAX_TITLE_CHARS / MAX_SUMMARY_CHARS / MAX_PAGES / PIN_REQUIRES_OWNER`；`sealed.key_ready`；`VERSION_CONFLICT`。
- Produces：
  - `notes.Entry.updated_by: str`（`owner` / `assistant` / `import`，数据类的最后一个字段，在 `updated_at` 之后）；`notes.Page` 随之带上。`notes_tools._listed` 逐键取值，工具的输出不变。
  - `GET /api/me/assistant/notes?folder=` → `{"items": [<条目>…], "locked": bool}`；条目的键：`path`、`title`、`summary`、`pinned`、`version`、`size`、`updated_by`、`updated_at`。
  - `GET /api/me/assistant/notes/page?path=` → 条目加 `body`；`PUT /api/me/assistant/notes/page`（`{path, body, summary?, title?, pinned?, version?}`）→ 条目；`DELETE /api/me/assistant/notes/page?path=` → `{"deleted": bool}`。
  - 版本冲突：HTTP 409、业务码 `40901`、`errors[0] == {"type": "version_conflict", "current_version": n}`。

- [ ] **Step 1: 写失败测试**

`tests/api/test_my_assistant_notes.py`（新建）：

```python
"""「我的助理」页的知识库：所有者看索引、读写每一页（含置顶页），返回里写明是谁改的；别人碰不到。"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import keys, notes
from coreman.core.db.models import Bot
from tests.api.my_assistant_helpers import URL, as_bot_token, code, owned
from tests.integration.assistant_helpers import SECRET, raw

pytestmark = pytest.mark.usefixtures("assistant_master")
INDEX, PAGE = f"{URL}/notes", f"{URL}/notes/page"
PATH = "规则/口吻"
STORED = (
    "SELECT string_agg(path_enc || title_enc || summary_enc || body_enc, chr(32))"
    " FROM assistant_pages WHERE bot_id = :bot"
)


async def _by_assistant(db_session: AsyncSession, bot: Bot) -> None:
    """助理在所有者发起的一轮里写下并置顶的一页。"""
    await notes.write(
        db_session,
        bot.id,
        PATH,
        body=f"回复要短。{SECRET}",
        summary="口吻规则",
        pinned=True,
        updated_by="assistant",
    )
    await db_session.commit()


async def test_the_owner_creates_reads_and_lists_pages(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owned(client, db_session)
    created = await client.put(
        PAGE,
        json={"path": " 人//李四 ", "body": f"采购负责人。{SECRET}", "summary": "李四是谁"},
    )
    assert created.status_code == 200, created.text
    entry = created.json()["data"]
    assert entry["path"] == "人/李四" and entry["title"] == "李四" and entry["version"] == 1
    assert entry["pinned"] is False and entry["updated_by"] == "owner"
    assert SECRET not in (await raw(db_session, STORED, bot=bot.id))

    with capture_logs() as logs:
        index = await client.get(INDEX)
        page = await client.get(PAGE, params={"path": "人/李四"})
    assert index.headers["cache-control"] == "no-store"
    assert index.json()["data"]["locked"] is False
    assert [item["path"] for item in index.json()["data"]["items"]] == ["人/李四"]
    assert "body" not in index.json()["data"]["items"][0]
    assert page.json()["data"]["body"] == f"采购负责人。{SECRET}"
    assert page.json()["data"]["updated_by"] == "owner"
    seen = [
        (entry["action"], entry["target"], entry["actor_id"])
        for entry in logs
        if entry["event"] == "assistant_content_access"
    ]
    assert seen == [("notes.index", "", str(owner.id)), ("notes.read", "page", str(owner.id))]
    # 路径本身是个人内容：不进访问记录。
    assert "李四" not in str(logs) and SECRET not in str(logs)


async def test_the_owner_can_rewrite_and_unpin_a_pinned_page(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _by_assistant(db_session, bot)
    [listed] = (await client.get(INDEX)).json()["data"]["items"]
    assert listed["pinned"] is True and listed["updated_by"] == "assistant"

    rewritten = await client.put(
        PAGE, json={"path": PATH, "body": "回复要短，先说结论。", "summary": "口吻规则", "version": 1}
    )
    assert rewritten.status_code == 200, rewritten.text
    entry = rewritten.json()["data"]
    assert entry["updated_by"] == "owner" and entry["version"] == 2 and entry["pinned"] is True

    # 同样的改写放在所有者不在场的轮次里是被拒绝的：页面是改置顶页的唯一入口。
    with pytest.raises(notes.NotesError) as refused:
        await notes.write(
            db_session, bot.id, PATH, body="x", summary="", version=2, owner_present=False
        )
    assert refused.value.code == notes.PIN_REQUIRES_OWNER
    await db_session.rollback()

    unpinned = await client.put(
        PAGE,
        json={
            "path": PATH,
            "body": "回复要短，先说结论。",
            "summary": "口吻规则",
            "pinned": False,
            "version": 2,
        },
    )
    assert unpinned.json()["data"]["pinned"] is False
    assert unpinned.json()["data"]["updated_by"] == "owner"
    assert unpinned.json()["data"]["version"] == 3


@pytest.mark.parametrize(
    ("body", "status", "reason"),
    [
        ({"path": PATH, "body": "x", "version": 0}, 409, "page_exists"),
        ({"path": PATH, "body": "x", "version": 9}, 409, "version_conflict"),
        ({"path": "不存在的页", "body": "x", "version": 3}, 404, "page_not_found"),
        ({"path": "a/../b", "body": "x"}, 422, "invalid_path"),
        ({"path": PATH, "body": "   ", "version": 1}, 422, "invalid_page"),
        ({"path": PATH, "body": "x", "summary": "长" * 301, "version": 1}, 422, "invalid_page"),
        ({"path": PATH, "body": "x", "pinned": "yes", "version": 1}, 422, None),
        ({"path": PATH, "body": "x", "version": 1, "updated_by": "import"}, 422, None),
        ({"path": PATH, "version": 1}, 422, None),
    ],
)
async def test_bad_writes_are_refused_with_a_reason(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    body: dict[str, object],
    status: int,
    reason: str | None,
) -> None:
    bot, _owner = await owned(client, db_session)
    await _by_assistant(db_session, bot)
    response = await client.put(PAGE, json=body)
    assert response.status_code == status, response.text
    assert code(response) == reason
    if reason == "version_conflict":
        assert response.json()["code"] == 40901
        assert response.json()["errors"][0]["current_version"] == 1
    page = await notes.read(db_session, bot.id, PATH)
    assert page is not None and page.version == 1 and page.updated_by == "assistant"


async def test_a_page_over_the_size_limit_is_refused(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await owned(client, db_session)
    response = await client.put(PAGE, json={"path": "大页", "body": "字" * 70_000})
    assert response.status_code == 422 and code(response) == "page_too_large"


async def test_reading_and_deleting(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    bot, _owner = await owned(client, db_session)
    await _by_assistant(db_session, bot)
    missing = await client.get(PAGE, params={"path": "没有这一页"})
    assert missing.status_code == 404 and code(missing) == "page_not_found"
    assert (await client.get(PAGE, params={"path": "a/./b"})).status_code == 422
    assert (await client.get(PAGE)).status_code == 422
    # 置顶页所有者也能删。
    assert (await client.delete(PAGE, params={"path": PATH})).json()["data"] == {"deleted": True}
    assert (await client.delete(PAGE, params={"path": PATH})).json()["data"] == {"deleted": False}
    assert await notes.read(db_session, bot.id, PATH) is None


async def test_the_index_can_be_limited_to_a_folder(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _by_assistant(db_session, bot)
    await client.put(PAGE, json={"path": "人/李四", "body": "x"})
    listed = await client.get(INDEX, params={"folder": "规则"})
    assert [item["path"] for item in listed.json()["data"]["items"]] == [PATH]


async def test_writes_need_csrf_and_an_interactive_login(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, owner = await owned(client, db_session)
    await _by_assistant(db_session, bot)
    csrf = client.headers.pop("X-CSRF-Token")
    assert (await client.put(PAGE, json={"path": PATH, "body": "x", "version": 1})).status_code == 403
    assert (await client.delete(PAGE, params={"path": PATH})).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    as_bot_token(client, monkeypatch, owner)
    for response in (
        await client.get(INDEX),
        await client.get(PAGE, params={"path": PATH}),
        await client.put(PAGE, json={"path": PATH, "body": "x", "version": 1}),
        await client.delete(PAGE, params={"path": PATH}),
    ):
        assert response.status_code == 403 and SECRET not in response.text
    page = await notes.read(db_session, bot.id, PATH)
    assert page is not None and page.version == 1


async def test_an_admin_with_their_own_assistant_cannot_reach_the_owners_pages(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _by_assistant(db_session, bot)
    theirs, _admin = await owned(client, db_session, role="platform_admin")
    assert (await client.get(INDEX)).json()["data"]["items"] == []
    read = await client.get(PAGE, params={"path": PATH})
    assert read.status_code == 404 and SECRET not in read.text
    # 同一个路径在管理员自己的知识库里是另一页：写与删都只落在他自己那边。
    written = await client.put(PAGE, json={"path": PATH, "body": "管理员自己的", "version": 0})
    assert written.status_code == 200
    assert (await client.delete(PAGE, params={"path": PATH})).json()["data"] == {"deleted": True}
    page = await notes.read(db_session, bot.id, PATH)
    assert page is not None and page.body == f"回复要短。{SECRET}" and page.updated_by == "assistant"
    assert await notes.read(db_session, theirs.id, PATH) is None


async def test_after_the_key_is_destroyed_the_index_is_locked(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owned(client, db_session)
    await _by_assistant(db_session, bot)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    index = await client.get(INDEX)
    assert index.status_code == 200 and index.json()["data"] == {"items": [], "locked": True}
    for response in (
        await client.get(PAGE, params={"path": PATH}),
        await client.put(PAGE, json={"path": PATH, "body": "x", "version": 1}),
        await client.delete(PAGE, params={"path": PATH}),
    ):
        assert response.status_code == 409 and code(response) == "content_locked"
        assert SECRET not in response.text
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_my_assistant_notes.py -q`
Expected: FAIL：404（路由还没有）。

- [ ] **Step 3: `notes.Entry` 带上最后修改者**

`coreman/core/assistants/notes.py`：

`Entry` 的定义里，在 `updated_at: datetime` 之后加：

```python
    # 最后一次写这一页的是谁：`owner`（所有者在页面上）、`assistant`、`import`。
    updated_by: str
```

`_entry` 的返回里，在 `updated_at=row.updated_at,` 之后加：

```python
        updated_by=row.updated_by,
```

`read` 构造 `Page(...)` 的参数里，在 `updated_at=entry.updated_at,` 之后加：

```python
        updated_by=entry.updated_by,
```

（`Page(Entry)` 的 `body` 仍排在继承来的字段之后；两处构造都用关键字参数，字段顺序不影响它们。）

Run: `uv run pytest tests/integration tests/api tests/unit -q -k "notes or tool_groups"`
Expected: 已有的知识库用例全部 PASS；只有 `tests/api/test_my_assistant_notes.py` 仍然失败。

- [ ] **Step 4: 实现路由**

`coreman/api/routers/me_assistant_notes.py`（新建）：

```python
"""「我的助理」页的知识库：索引、读一页、新建或整页替换、删除。

这是所有者看见「置顶了什么」并修改置顶页的唯一界面：写入一律 `owner_present=True`、
`updated_by="owner"`。核心逻辑在 `coreman/core/assistants/notes.py`。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.errors import VERSION_CONFLICT, ApiError
from coreman.api.routers.me_assistant_common import (
    DEPENDENCIES,
    PREFIX,
    Mine,
    mine,
    refuse,
    writable,
)
from coreman.core.assistants import access_log, notes, sealed
from coreman.core.assistants.notes import NotesError

router = APIRouter(prefix=PREFIX + "/notes", tags=["my-assistant"], dependencies=DEPENDENCIES)

# 查询参数与请求体里路径的长度上限：规范化之前可能带多余的斜杠与空白，放宽到核心上限的两倍。
PATH_INPUT = notes.MAX_PATH_CHARS * 2
ERRORS: dict[str, tuple[int, str]] = {
    "invalid_path": (422, "路径不合法：最多 8 级、200 字，不能有「.」「..」这样的段或控制字符"),
    "invalid_page": (
        422,
        f"正文不能为空，摘要不超过 {notes.MAX_SUMMARY_CHARS} 字，标题不超过 {notes.MAX_TITLE_CHARS} 字",
    ),
    "page_too_large": (422, f"单页不能超过 {notes.MAX_PAGE_BYTES // 1024}KB"),
    "page_exists": (409, "这个路径已经有一页了，请从列表里打开它再改"),
    "page_not_found": (404, "这一页不存在"),
    "too_many_pages": (409, f"知识库已达 {notes.MAX_PAGES} 页上限"),
    "notes_full": (409, "知识库合计已达 20MB 上限"),
    "content_locked": (409, "加密保存的个人内容已不可读取"),
    "assistant_only": (404, "你还没有个人助理"),
    "pin_requires_owner": (409, "置顶页只能由所有者本人修改"),
}


def _refuse(exc: NotesError) -> ApiError:
    if exc.code == "version_conflict":
        # 专用业务码：前端据此重新载入这一页，而不是原话提示。
        return ApiError(
            409,
            VERSION_CONFLICT,
            "这一页已被改动，请重新打开后再保存",
            [{"type": exc.code, **exc.detail}],
        )
    return refuse(exc.code, ERRORS)


def _entry(entry: notes.Entry) -> dict[str, Any]:
    out = asdict(entry)
    out["updated_at"] = entry.updated_at.isoformat()
    return out


class PageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=PATH_INPUT)
    # 字节上限由核心函数判断（中文一字三字节）；这里按字符数挡住离谱的请求体。
    body: str = Field(max_length=notes.MAX_PAGE_BYTES)
    summary: str = Field(default="", max_length=notes.MAX_SUMMARY_CHARS * 4)
    title: str | None = Field(default=None, max_length=notes.MAX_TITLE_CHARS * 4)
    # 省略：新页不置顶，已有的页保持原样。
    pinned: StrictBool | None = None
    # 调用方读到的版本；0 表示「我认为这页还不存在」。
    version: int = Field(default=0, ge=0)


@router.get("")
async def index(
    folder: str = Query("", max_length=PATH_INPUT),
    own: Mine = Depends(mine),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        entries = await notes.index(session, own.bot.id, folder=folder)
    except NotesError as exc:
        raise _refuse(exc) from None
    # 密钥不可用时核心函数把读不出来的行都略过了：告诉页面这是「读不出来」而不是「没有」。
    locked = not await sealed.key_ready(session, own.bot.id)
    await access_log.record(session, own.bot.id, own.actor.id, "notes.index", "")
    return {"code": 0, "data": {"items": [_entry(entry) for entry in entries], "locked": locked}}


@router.get("/page")
async def read(
    path: str = Query(min_length=1, max_length=PATH_INPUT),
    own: Mine = Depends(mine),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        page = await notes.read(session, own.bot.id, path)
    except NotesError as exc:
        raise _refuse(exc) from None
    if page is None:
        raise refuse("page_not_found", ERRORS)
    # 路径本身是个人内容，不进访问记录。
    await access_log.record(session, own.bot.id, own.actor.id, "notes.read", "page")
    return {"code": 0, "data": _entry(page)}


@router.put("/page")
async def write(
    body: PageIn, own: Mine = Depends(writable), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    try:
        entry = await notes.write(
            session,
            own.bot.id,
            body.path,
            body=body.body,
            summary=body.summary,
            title=body.title,
            pinned=body.pinned,
            version=body.version,
            updated_by="owner",
            owner_present=True,
        )
    except NotesError as exc:
        await session.rollback()
        raise _refuse(exc) from None
    data = _entry(entry)
    await session.commit()
    return {"code": 0, "data": data}


@router.delete("/page")
async def delete(
    path: str = Query(min_length=1, max_length=PATH_INPUT),
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        gone = await notes.delete(session, own.bot.id, path, owner_present=True)
    except NotesError as exc:
        await session.rollback()
        raise _refuse(exc) from None
    await session.commit()
    return {"code": 0, "data": {"deleted": gone}}
```

实现时留意三点：

- `_entry(page)` 对 `Page` 同样适用（`asdict` 会带上 `body`）；索引的条目是 `Entry`，没有 `body`。
- 「页面大小超限」用例里正文 70000 个汉字（210KB）没超过 `PageIn.body` 的字符上限 204800，所以由核心函数按字节判为 `page_too_large`。
- 密钥销毁后 `notes.read` / `write` / `delete` 在算路径摘要时就抛 `content_locked`（`_path_key`），三个接口因此都是 409。

`coreman/api/main.py`：导入改为

```python
    from coreman.api.routers import me_assistant, me_assistant_followups, me_assistant_notes
```

并在 `app.include_router(me_assistant_followups.router)` 之后加

```python
    app.include_router(me_assistant_notes.router)
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/api/test_my_assistant_notes.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: PASS。

Run: `uv run pytest tests/integration tests/api tests/unit -q -k "notes"`
Expected: PASS。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add coreman/core/assistants/notes.py coreman/api/routers/me_assistant_notes.py \
  coreman/api/main.py tests/api/test_my_assistant_notes.py
git commit -m "我的助理接口：所有者查看与编辑知识页，置顶页也能改，返回里写明最后是谁改的"
```

---

### Task 4: 子任务的列表与取消

**Files:**
- Modify: `coreman/api/routers/me_assistant.py`
- Test: `tests/api/test_my_assistant_jobs.py`（新建）

**Interfaces:**
- Consumes: 3A 的 `jobs.list_(session, bot_id, *, scope, limit)`、`jobs.view(row, *, full)`、`jobs.cancel(session, bot_id, job_id, *, code, report)`、`jobs.ENDED`、`jobs.JobError`；`tests.integration.job_helpers.owner_with_key / spawned / reloaded`；Task 1 的公共件。
- Produces：
  - `GET /api/me/assistant/jobs` → `{"active": [<view(full=True)>…], "recent": [<view(full=True)>…]}`；`active` 是 `queued` / `running`，`recent` 是最近 20 个里已经结束的（含 `blocked`）。
  - `POST /api/me/assistant/jobs/{job_id}/cancel` → `{"id": str, "status": "cancelled"}`；`job_not_found` → 404，`job_closed` → 409。
  - `me_assistant.JOB_ERRORS`、`me_assistant.JOB_LIMIT = 20`。

- [ ] **Step 1: 写失败测试**

`tests/api/test_my_assistant_jobs.py`（新建）：

```python
"""「我的助理」页的子任务：所有者看到交代原文与进度，可以取消进行中的；别人碰不到。"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import jobs, keys, sealed
from coreman.core.db.models import Bot, User
from tests.api.conftest import login_existing
from tests.api.my_assistant_helpers import URL, as_bot_token, code
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.job_helpers import owner_with_key, reloaded, spawned

pytestmark = pytest.mark.usefixtures("assistant_master")
JOBS = f"{URL}/jobs"
BRIEF = f"查一下上季度的退货原因。{SECRET}"
STORED = "SELECT string_agg(title_enc || brief_enc, chr(32)) FROM assistant_jobs WHERE bot_id = :bot"


async def _owner(client: httpx.AsyncClient, db_session: AsyncSession) -> tuple[Bot, User]:
    bot, owner = await owner_with_key(db_session)
    await db_session.commit()
    await login_existing(client, db_session, owner)
    return bot, owner


async def test_the_owner_sees_running_and_finished_jobs_with_the_brief(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner_row = await _owner(client, db_session)
    running = await spawned(db_session, bot, title="退货原因", brief=BRIEF)
    ended = await spawned(db_session, bot, title="已取消的调查", brief="不用查了")
    await jobs.cancel(db_session, bot.id, ended.id)
    await db_session.commit()
    response = await client.get(JOBS)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    [active] = data["active"]
    assert active["id"] == str(running.id) and active["status"] == "queued"
    assert active["title"] == "退货原因" and active["brief"] == BRIEF
    [recent] = data["recent"]
    assert recent["id"] == str(ended.id) and recent["status"] == "cancelled"
    assert SECRET not in (await raw(db_session, STORED, bot=bot.id))


async def test_the_owner_cancels_a_running_job_and_the_assistant_is_told(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner_row = await _owner(client, db_session)
    row = await spawned(db_session, bot, title="退货原因", brief=BRIEF)
    response = await client.post(f"{JOBS}/{row.id}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"id": str(row.id), "status": "cancelled"}
    row = await reloaded(db_session, row)
    assert row.status == "cancelled" and row.error_code == "cancelled_by_owner"
    # 要投回主会话：没有被记成「不需要投回」。
    assert row.report_code != "not_needed"
    again = await client.post(f"{JOBS}/{row.id}/cancel")
    assert again.status_code == 409 and code(again) == "job_closed"
    missing = await client.post(f"{JOBS}/{uuid.uuid4()}/cancel")
    assert missing.status_code == 404 and code(missing) == "job_not_found"
    assert (await client.post(f"{JOBS}/not-a-uuid/cancel")).status_code == 422


async def test_cancel_needs_csrf_and_an_interactive_login(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, owner = await _owner(client, db_session)
    row = await spawned(db_session, bot, title="退货原因", brief=BRIEF)
    csrf = client.headers.pop("X-CSRF-Token")
    assert (await client.post(f"{JOBS}/{row.id}/cancel")).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    as_bot_token(client, monkeypatch, owner)
    assert (await client.get(JOBS)).status_code == 403
    assert (await client.post(f"{JOBS}/{row.id}/cancel")).status_code == 403
    assert (await reloaded(db_session, row)).status == "queued"


async def test_an_admin_with_their_own_assistant_cannot_see_or_cancel_the_owners_jobs(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner_row = await owner_with_key(db_session)
    row = await spawned(db_session, bot, title="退货原因", brief=BRIEF)
    _theirs, admin = await owner_with_key(
        db_session, chat_id="oc_admin", platform_user_id="admin_pid", open_id="ou_admin"
    )
    admin.role = "platform_admin"
    await db_session.commit()
    await login_existing(client, db_session, admin)
    listed = await client.get(JOBS)
    assert listed.json()["data"] == {"active": [], "recent": []} and SECRET not in listed.text
    refused = await client.post(f"{JOBS}/{row.id}/cancel")
    assert refused.status_code == 404 and code(refused) == "job_not_found"
    assert (await reloaded(db_session, row)).status == "queued"


async def test_after_the_key_is_destroyed_jobs_show_as_locked(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await _owner(client, db_session)
    await spawned(db_session, bot, title="退货原因", brief=BRIEF)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    sealed.forget()
    response = await client.get(JOBS)
    assert response.status_code == 200 and SECRET not in response.text
    [active] = response.json()["data"]["active"]
    assert active["locked"] is True and active["title"] == "" and active["brief"] == ""
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_my_assistant_jobs.py -q`
Expected: FAIL：404（路由还没有）。若报 `ModuleNotFoundError: coreman.core.assistants.jobs` 或 `tests.integration.job_helpers`，说明 3A 还没落地：停下来，先完成 3A。

- [ ] **Step 3: 实现**

`coreman/api/routers/me_assistant.py`：

导入区改为：

```python
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.routers.feishu_personal import interactive_user
from coreman.api.routers.me_assistant_common import (
    DEPENDENCIES,
    PREFIX,
    Mine,
    mine,
    refuse,
    writable,
)
from coreman.core.assistants import access_log, jobs, owner_view, sealed
from coreman.core.db.models import User
from coreman.core.timeutils import utcnow
```

`router = …` 之后加：

```python
# 「最近结束」看最近这么多个子任务里已经结束的。
JOB_LIMIT = 20
JOB_ERRORS: dict[str, tuple[int, str]] = {
    "job_not_found": (404, "这个子任务不存在"),
    "job_closed": (409, "这个子任务已经结束"),
}
```

文件末尾追加：

```python
@router.get("/jobs")
async def job_list(
    own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """正在跑与最近结束的子任务，含交代原文、轮数与结论。"""
    active = await jobs.list_(session, own.bot.id, scope="active", limit=JOB_LIMIT)
    recent = [
        row
        for row in await jobs.list_(session, own.bot.id, scope="recent", limit=JOB_LIMIT)
        if row.status in jobs.ENDED
    ]
    # 核心函数已经还原过；这里按查看者再核一次，不是所有者本人就全部收回。
    await sealed.open_rows(session, [*active, *recent], viewer=own.actor.id)
    await access_log.record(session, own.bot.id, own.actor.id, "jobs.read", "list")
    return {
        "code": 0,
        "data": {
            "active": [jobs.view(row, full=True) for row in active],
            "recent": [jobs.view(row, full=True) for row in recent],
        },
    }


@router.post("/jobs/{job_id}/cancel")
async def job_cancel(
    job_id: uuid.UUID,
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """所有者取消一个进行中的子任务；`report=True` 让助理在主会话里得知这件事。"""
    try:
        row = await jobs.cancel(
            session, own.bot.id, job_id, code="cancelled_by_owner", report=True
        )
    except jobs.JobError as exc:
        await session.rollback()
        raise refuse(exc.code, JOB_ERRORS) from None
    data = {"id": str(row.id), "status": row.status}
    await session.commit()
    return {"code": 0, "data": data}
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/api/test_my_assistant_jobs.py tests/api/test_my_assistant_overview.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: PASS。

（「取消后要投回」那条断言用的是 3A 计划里的列名 `report_code`；若 3A 实际落地时改了名，以它的实现为准改这一行断言——要守住的是 `jobs.cancel(..., report=True)` 没有把这次取消记成不需要投回。）

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/api/routers/me_assistant.py tests/api/test_my_assistant_jobs.py
git commit -m "我的助理接口：子任务的列表与取消，取消后助理在对话里得知"
```

---

### Task 5: 等你出手与最近落地

**Files:**
- Modify: `coreman/core/assistants/owner_view.py`（导入区；常量；文件末尾追加）
- Modify: `coreman/api/routers/me_assistant.py`
- Modify: `tests/unit/test_assistant_sealed_ledger.py`（`ENTITY_SELECTS`）
- Test: `tests/api/test_my_assistant_attention.py`（新建）

**Interfaces:**
- Consumes: `AssistantApproval`；`sealed.open_rows(..., viewer=…)`、`sealed.is_locked`；`owner_view.approval_link`；`jobs.list_(…, scope="blocked")`、`jobs.view`；`followups.due(session, bot_id, today)`、`followups.view`；`settings.get / local_today`；Task 4 的 `JOB_LIMIT`。
- Produces：
  - `owner_view.PENDING = ("pending",)`、`owner_view.SETTLED = ("executed", "rejected", "withdrawn")`、`owner_view.APPROVAL_LIMIT = 30`。
  - `async def owner_view.approvals(session, bot: Bot, viewer: uuid.UUID, statuses: tuple[str, ...], *, limit: int = APPROVAL_LIMIT) -> list[AssistantApproval]`（新近有动静的在前，内容只对 `viewer` 是所有者时还原）。
  - `def owner_view.pending_view(row: AssistantApproval, link: str | None) -> dict[str, Any]`：恰好五个键 `id`、`tool`、`summary`、`created_at`、`link`。
  - `def owner_view.settled_view(row: AssistantApproval) -> dict[str, Any]`：键 `id`、`tool`、`channel`、`status`、`summary`、`reason`、`error`、`created_at`、`decided_at`、`executed_at`、`locked`。
  - `GET /api/me/assistant/attention` → `{"today": "YYYY-MM-DD", "approvals": [<pending_view>…], "jobs": [<jobs.view(full=True)>…], "followups": [<followups.view>…]}`。
  - `GET /api/me/assistant/approvals/recent` → `{"items": [<settled_view>…]}`。

- [ ] **Step 1: 写失败测试**

`tests/api/test_my_assistant_attention.py`（新建）：

```python
"""等你出手与最近落地：待批的单只给摘要与链接，落地的单给本人解密后的摘要与理由；别人拿不到。"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import followups, keys, sealed
from coreman.core.config import reset_settings_cache
from coreman.core.db.models import AssistantApproval, Bot, User
from coreman.core.timeutils import utcnow
from tests.api.conftest import login_existing
from tests.api.my_assistant_helpers import URL, as_bot_token
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.job_helpers import owner_with_key, reloaded, spawned

pytestmark = pytest.mark.usefixtures("assistant_master")
ATTENTION, RECENT = f"{URL}/attention", f"{URL}/approvals/recent"
ABSTRACT = f"私聊 → 李四 {SECRET}"
BODY_TEXT = "正文-不该出现在页面接口里-91c2"
STORED = "SELECT string_agg(args_enc::text, chr(32)) FROM assistant_approvals WHERE bot_id = :bot"


def _approval(bot: Bot, status: str, **over: Any) -> AssistantApproval:
    return AssistantApproval(
        bot_id=bot.id,
        tool="feishu_im_send_message",
        args_enc={"abstract": ABSTRACT, "calls": [{"text": BODY_TEXT}]},
        args_hash=uuid.uuid4().hex,
        request_hash=uuid.uuid4().hex,
        channel="p2p",
        external_id=f"cm-pa-{uuid.uuid4().hex}",
        expires_at=utcnow() + timedelta(hours=72),
        status=status,
        **over,
    )


async def _owner(client: httpx.AsyncClient, db_session: AsyncSession) -> tuple[Bot, User]:
    bot, owner = await owner_with_key(db_session)
    await db_session.commit()
    await login_existing(client, db_session, owner)
    return bot, owner


async def _seed(db_session: AsyncSession, bot: Bot) -> dict[str, AssistantApproval]:
    now = utcnow()
    rows = {
        "pending": _approval(bot, "pending"),
        "executed": _approval(bot, "executed", decided_at=now, executed_at=now),
        "rejected": _approval(
            bot, "rejected", decided_at=now - timedelta(hours=1), reason_enc=f"语气太硬 {SECRET}"
        ),
        "withdrawn": _approval(bot, "withdrawn", decided_at=now - timedelta(hours=2)),
        "failed": _approval(bot, "failed", decided_at=now, error_code="feishu_error"),
    }
    db_session.add_all(rows.values())
    blocked = await spawned(db_session, bot, title="合同条款", brief="查一下违约条款")
    blocked = await reloaded(db_session, blocked)
    blocked.status, blocked.blocked_enc = "blocked", f"需要你确认范围 {SECRET}"
    await spawned(db_session, bot, title="还在跑的", brief="不该出现在等你出手里")
    await followups.add(
        db_session, bot.id, category="remind", title=f"续约 {SECRET}", due_on=date(2000, 1, 1)
    )
    await followups.add(
        db_session, bot.id, category="waiting", title="以后的事", due_on=date(2099, 1, 1)
    )
    await db_session.commit()
    return rows


async def test_attention_lists_pending_approvals_blocked_jobs_and_due_followups(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOWER_WEB_URL", "https://tower.example.com")
    reset_settings_cache()
    bot, owner = await _owner(client, db_session)
    rows = await _seed(db_session, bot)
    with capture_logs() as logs:
        response = await client.get(ATTENTION)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    [pending] = data["approvals"]
    assert pending == {
        "id": str(rows["pending"].id),
        "tool": "feishu_im_send_message",
        "summary": ABSTRACT,
        "created_at": pending["created_at"],
        "link": "https://tower.example.com",
    }
    [job] = data["jobs"]
    assert job["title"] == "合同条款" and job["status"] == "blocked"
    assert job["blocked_reason"] == f"需要你确认范围 {SECRET}"
    [due] = data["followups"]
    assert due["id"] == "F001" and due["title"] == f"续约 {SECRET}"
    # 暂存的参数（要发出的正文）不经这个接口给出：正文在审批系统里看。
    assert BODY_TEXT not in response.text
    assert SECRET not in (await raw(db_session, STORED, bot=bot.id))
    accesses = [entry for entry in logs if entry["event"] == "assistant_content_access"]
    assert sorted((entry["action"], entry["target"]) for entry in accesses) == [
        ("approvals.read", "pending"),
        ("followups.read", "today"),
        ("jobs.read", "blocked"),
    ]
    assert {entry["actor_id"] for entry in accesses} == {str(owner.id)}


async def test_without_a_configured_entry_there_is_no_link(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner_row = await _owner(client, db_session)
    await _seed(db_session, bot)
    [pending] = (await client.get(ATTENTION)).json()["data"]["approvals"]
    assert pending["link"] is None and pending["summary"] == ABSTRACT


async def test_recent_lists_executed_rejected_and_withdrawn_newest_first(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner_row = await _owner(client, db_session)
    rows = await _seed(db_session, bot)
    response = await client.get(RECENT)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    items = response.json()["data"]["items"]
    assert [item["status"] for item in items] == ["executed", "rejected", "withdrawn"]
    assert [item["id"] for item in items] == [
        str(rows["executed"].id),
        str(rows["rejected"].id),
        str(rows["withdrawn"].id),
    ]
    assert items[0]["summary"] == ABSTRACT and items[0]["locked"] is False
    assert items[0]["executed_at"] is not None and items[0]["channel"] == "p2p"
    assert items[1]["reason"] == f"语气太硬 {SECRET}"
    assert BODY_TEXT not in response.text


async def test_an_admin_with_their_own_assistant_gets_nothing_of_the_owners(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, owner = await owner_with_key(db_session)
    await _seed(db_session, bot)
    _theirs, admin = await owner_with_key(
        db_session, chat_id="oc_admin", platform_user_id="admin_pid", open_id="ou_admin"
    )
    admin.role = "platform_admin"
    await db_session.commit()
    await login_existing(client, db_session, admin)
    attention = await client.get(ATTENTION)
    assert attention.json()["data"]["approvals"] == []
    assert attention.json()["data"]["jobs"] == [] and attention.json()["data"]["followups"] == []
    recent = await client.get(RECENT)
    assert recent.json()["data"]["items"] == []
    assert SECRET not in attention.text + recent.text
    as_bot_token(client, monkeypatch, owner)
    assert (await client.get(ATTENTION)).status_code == 403
    assert (await client.get(RECENT)).status_code == 403


async def test_after_the_key_is_destroyed_only_metadata_is_left(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await _owner(client, db_session)
    await _seed(db_session, bot)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    sealed.forget()
    attention = await client.get(ATTENTION)
    recent = await client.get(RECENT)
    assert attention.status_code == 200 and recent.status_code == 200
    assert SECRET not in attention.text + recent.text
    [pending] = attention.json()["data"]["approvals"]
    assert pending["summary"] is None and pending["tool"] == "feishu_im_send_message"
    items = recent.json()["data"]["items"]
    assert [item["status"] for item in items] == ["executed", "rejected", "withdrawn"]
    assert all(item["locked"] is True and item["summary"] is None for item in items)
    assert items[1]["reason"] is None
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_my_assistant_attention.py -q`
Expected: FAIL：404（路由还没有）。

- [ ] **Step 3: 实现审批单的所有者视图**

`coreman/core/assistants/owner_view.py`：

导入里把

```python
from sqlalchemy import select
```

换成

```python
from sqlalchemy import func, select
```

`from coreman.core.db.models import (` 的清单里在 `AssistantLifecycle,` 之前加 `AssistantApproval,`。

`HOW_TO = …` 之后加：

```python
# 「等你出手」与「最近落地」各看哪些状态的审批单。失败、过期、内容已过时的单会作为结果回合
# 回到助理会话，由助理重提或告诉所有者，不在页面上重复。
PENDING = ("pending",)
SETTLED = ("executed", "rejected", "withdrawn")
APPROVAL_LIMIT = 30
```

文件末尾追加：

```python
async def approvals(
    session: AsyncSession,
    bot: Bot,
    viewer: uuid.UUID,
    statuses: tuple[str, ...],
    *,
    limit: int = APPROVAL_LIMIT,
) -> list[AssistantApproval]:
    """这位助理处于这些状态的审批单，新近有动静的在前。

    内容列只对所有者本人还原：`viewer` 不是所有者时摘要、理由、结果都是空的，
    `sealed.is_locked(row)` 为真。
    """
    latest = func.coalesce(
        AssistantApproval.executed_at, AssistantApproval.decided_at, AssistantApproval.created_at
    )
    rows = list(
        await session.scalars(
            select(AssistantApproval)
            .where(AssistantApproval.bot_id == bot.id, AssistantApproval.status.in_(statuses))
            .order_by(latest.desc(), AssistantApproval.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, rows, viewer=viewer)
    return rows


def _summary(row: AssistantApproval) -> str | None:
    """提交时平台生成的单行摘要（渠道与对象）；读不出来是 None。"""
    stored = row.args_enc if isinstance(row.args_enc, dict) else {}
    found = stored.get("abstract")
    return found if isinstance(found, str) and found else None


def pending_view(row: AssistantApproval, link: str | None) -> dict[str, Any]:
    """等所有者批的一张单：只有编号、工具名、摘要、提交时间与审批系统的入口。

    要发出的正文不在这里给：它在审批系统里，由所有者在那边看着批。
    """
    return {
        "id": str(row.id),
        "tool": row.tool,
        "summary": _summary(row),
        "created_at": row.created_at.isoformat(),
        "link": link,
    }


def settled_view(row: AssistantApproval) -> dict[str, Any]:
    """已经落地的一张单：结果、摘要、审批人写的理由与各个时刻。"""
    return {
        "id": str(row.id),
        "tool": row.tool,
        "channel": row.channel,
        "status": row.status,
        "summary": _summary(row),
        "reason": row.reason_enc or None,
        "error": row.error_code,
        "created_at": row.created_at.isoformat(),
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "executed_at": row.executed_at.isoformat() if row.executed_at else None,
        "locked": sealed.is_locked(row),
    }
```

`tests/unit/test_assistant_sealed_ledger.py`：`ENTITY_SELECTS` 里在 `"coreman/core/assistants/notes.py": 2,` 那一项之后加：

```python
    # 「我的助理」页的审批单：显式还原，且只对所有者本人（viewer）还原
    "coreman/core/assistants/owner_view.py": 1,
```

- [ ] **Step 4: 实现两个接口**

`coreman/api/routers/me_assistant.py`：

导入里把

```python
from coreman.core.assistants import access_log, jobs, owner_view, sealed
```

换成

```python
from coreman.core.assistants import access_log, followups, jobs, owner_view, sealed
from coreman.core.assistants import settings as assistant_settings
```

文件末尾追加：

```python
@router.get("/attention")
async def attention(
    own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """等你出手：待批的审批单、被卡住的子任务、今天到期的待跟进。"""
    bot, actor = own.bot, own.actor
    prefs = await assistant_settings.get(session, bot.id)
    today = assistant_settings.local_today(prefs, utcnow())
    link = owner_view.approval_link()
    pending = await owner_view.approvals(session, bot, actor.id, owner_view.PENDING)
    blocked = await jobs.list_(session, bot.id, scope="blocked", limit=JOB_LIMIT)
    due = await followups.due(session, bot.id, today)
    # 核心函数已经还原过；这里按查看者再核一次。
    await sealed.open_rows(session, [*blocked, *due], viewer=actor.id)
    await access_log.record(session, bot.id, actor.id, "approvals.read", "pending")
    await access_log.record(session, bot.id, actor.id, "jobs.read", "blocked")
    await access_log.record(session, bot.id, actor.id, "followups.read", "today")
    data = {
        "today": today.isoformat(),
        "approvals": [owner_view.pending_view(row, link) for row in pending],
        "jobs": [jobs.view(row, full=True) for row in blocked],
        "followups": [followups.view(row) for row in due],
    }
    # 取今天到期的条目会把到了回看日的推后条目改回未结。
    await session.commit()
    return {"code": 0, "data": data}


@router.get("/approvals/recent")
async def recent_approvals(
    own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """最近落地：已执行、被拒、撤回的审批单，给本人解密后的摘要与理由。"""
    rows = await owner_view.approvals(session, own.bot, own.actor.id, owner_view.SETTLED)
    await access_log.record(session, own.bot.id, own.actor.id, "approvals.read", "recent")
    return {"code": 0, "data": {"items": [owner_view.settled_view(row) for row in rows]}}
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/api/test_my_assistant_attention.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: PASS。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add coreman/core/assistants/owner_view.py coreman/api/routers/me_assistant.py \
  tests/unit/test_assistant_sealed_ledger.py tests/api/test_my_assistant_attention.py
git commit -m "我的助理接口：等你出手与最近落地，审批单的内容只对所有者本人还原"
```

---

### Task 6: 所有者的设置

**Files:**
- Modify: `coreman/core/assistants/owner_view.py`（导入区；文件末尾追加 `preferences`、`set_budget`）
- Modify: `coreman/api/routers/me_assistant.py`
- Test: `tests/api/test_my_assistant_settings.py`（新建）

**Interfaces:**
- Consumes: 2F 的 `settings.view(row, now) -> dict`（键 `timezone`、`work_start`、`work_end`、`workdays`、`focus_chats`、`ignored_chats`、`on_shift`、`shift_override`、`shift_override_until`、`cold_start`、`today`）、`settings.update(session, bot_id, *, now, timezone=None, work_start=None, work_end=None, workdays=None, focus_chats=None, ignored_chats=None) -> AssistantSettings`、`settings.SettingsError`（`code`、`detail`；代码 `nothing_to_update`、`invalid_timezone`、`invalid_work_hours`、`invalid_workdays`、`invalid_chats`、`too_many_chats`、`chat_in_both_lists`、`content_locked`）、`settings.MAX_FOCUS == 5`、`settings.MAX_IGNORED == 50`；`settings.get`；`delivery.default_budget / BUDGET_MIN / BUDGET_MAX`；`sealed.open_row / is_locked`；Task 1 的公共件。
- Produces：
  - `async def owner_view.preferences(session: AsyncSession, row: AssistantSettings, now: datetime) -> dict[str, Any]`：`settings.view(row, now)` 的全部键，另加 `daily_turn_budget`（个人值或 `None`）、`platform_daily_turn_budget`、`limits`（`focus_chats`、`ignored_chats`、`budget_min`）、`locked`。
  - `async def owner_view.set_budget(session: AsyncSession, bot_id: uuid.UUID, value: int | None) -> AssistantSettings`：`None` 是清掉个人值；高于平台默认值抛 `settings.SettingsError("budget_over_platform", limit=<平台默认>)`。只 flush。
  - `GET /api/me/assistant/settings`、`PATCH /api/me/assistant/settings`（键 `timezone`、`work_start`、`work_end`、`workdays`、`focus_chats`、`ignored_chats`、`daily_turn_budget`，全部可省），都返回 `preferences(...)`；`me_assistant.SettingsIn`、`me_assistant.SETTINGS_ERRORS`。
  - 语义：前六项沿用 `settings.update` 的约定——给 `null` 等同于没给；`daily_turn_budget` 给 `null` 是清掉个人值、跟随平台默认（所以它按「请求里出现过没有」判断）。取值规则全部由核心函数判断并给出原因代码，入参模型只挡类型与离谱的长度。

- [ ] **Step 1: 写失败测试**

`tests/api/test_my_assistant_settings.py`（新建）：

```python
"""「我的助理」页的设置：所有者读写工作时段、时区、重点群、忽略的会话与每日预算；每条规则都拦得住。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import keys, settings
from coreman.core.db.models import Setting
from tests.api.my_assistant_helpers import URL, as_bot_token, code, owned
from tests.integration.assistant_helpers import raw

pytestmark = pytest.mark.usefixtures("assistant_master")
PREFS = f"{URL}/settings"
STORED = (
    "SELECT coalesce(focus_chats_enc::text, chr(32)) || coalesce(ignored_chats_enc::text, chr(32))"
    " FROM assistant_settings WHERE bot_id = :bot"
)


async def test_the_owner_reads_the_defaults(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    _bot, owner = await owned(client, db_session)
    with capture_logs() as logs:
        response = await client.get(PREFS)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    # 这两项随钟点变。
    assert isinstance(data.pop("on_shift"), bool) and len(data.pop("today")) == 10
    assert data == {
        "timezone": "Asia/Shanghai",
        "work_start": "09:30",
        "work_end": "19:00",
        "workdays": [1, 2, 3, 4, 5],
        "focus_chats": [],
        "ignored_chats": [],
        "shift_override": None,
        "shift_override_until": None,
        "cold_start": "pending",
        "daily_turn_budget": None,
        "platform_daily_turn_budget": 200,
        "limits": {"focus_chats": 5, "ignored_chats": 50, "budget_min": 10},
        "locked": False,
    }
    assert [
        (entry["action"], entry["actor_id"])
        for entry in logs
        if entry["event"] == "assistant_content_access"
    ] == [("settings.read", str(owner.id))]


async def test_the_owner_changes_everything_and_chat_lists_are_stored_encrypted(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    response = await client.patch(
        PREFS,
        json={
            "timezone": "America/New_York",
            "work_start": "08:00",
            "work_end": "17:30",
            "workdays": [6, 1, 2],
            "focus_chats": ["oc_focus_1", "oc_focus_1"],
            "ignored_chats": ["oc_noise_1"],
            "daily_turn_budget": 120,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["timezone"] == "America/New_York" and data["workdays"] == [1, 2, 6]
    assert (data["work_start"], data["work_end"]) == ("08:00", "17:30")
    assert data["focus_chats"] == ["oc_focus_1"] and data["ignored_chats"] == ["oc_noise_1"]
    assert data["daily_turn_budget"] == 120
    stored = await raw(db_session, STORED, bot=bot.id)
    assert "oc_focus_1" not in stored and "oc_noise_1" not in stored
    # 读回来与写进去的一致；只改一项时别的不动。
    assert (await client.get(PREFS)).json()["data"]["focus_chats"] == ["oc_focus_1"]
    cleared = await client.patch(PREFS, json={"daily_turn_budget": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["daily_turn_budget"] is None
    assert cleared.json()["data"]["ignored_chats"] == ["oc_noise_1"]
    emptied = await client.patch(PREFS, json={"ignored_chats": []})
    assert emptied.json()["data"]["ignored_chats"] == []
    db_session.expire_all()
    row = await settings.get(db_session, bot.id)
    assert row.timezone == "America/New_York" and row.daily_turn_budget is None


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({}, "nothing_to_update"),
        # 前六项给 null 等同于没给。
        ({"timezone": None}, "nothing_to_update"),
        ({"timezone": "Asia/Nowhere"}, "invalid_timezone"),
        ({"timezone": ""}, "invalid_timezone"),
        ({"work_start": "9:30"}, "invalid_work_hours"),
        ({"work_end": "24:00"}, "invalid_work_hours"),
        # 只改一头，和库里的另一头合起来看：默认 09:30–19:00。
        ({"work_end": "09:00"}, "invalid_work_hours"),
        ({"work_start": "19:00"}, "invalid_work_hours"),
        ({"work_start": "20:00", "work_end": "08:00"}, "invalid_work_hours"),
        ({"workdays": []}, "invalid_workdays"),
        ({"workdays": [8]}, "invalid_workdays"),
        ({"workdays": [1, 1]}, "invalid_workdays"),
        ({"focus_chats": ["ou_person"]}, "invalid_chats"),
        ({"focus_chats": [f"oc_{n}" for n in range(6)]}, "too_many_chats"),
        ({"ignored_chats": [f"oc_{n}" for n in range(51)]}, "too_many_chats"),
        ({"focus_chats": ["oc_a"], "ignored_chats": ["oc_a"]}, "chat_in_both_lists"),
        ({"daily_turn_budget": 201}, "budget_over_platform"),
        # 一项不过，整次请求都不落库（时区没有被改掉，见下面的断言）。
        ({"timezone": "UTC", "daily_turn_budget": 201}, "budget_over_platform"),
        # 类型不对、范围离谱、不认识的键：由请求校验挡下，没有原因代码。
        ({"workdays": [True]}, None),
        ({"workdays": ["1"]}, None),
        ({"focus_chats": "oc_a"}, None),
        ({"daily_turn_budget": 5}, None),
        ({"daily_turn_budget": 2001}, None),
        ({"daily_turn_budget": "200"}, None),
        ({"chat_id": "oc_other"}, None),
        ({"cold_start_done_at": "2026-11-13T00:00:00Z"}, None),
        ({"shift_override": "on"}, None),
    ],
)
async def test_every_rule_is_enforced(
    client: httpx.AsyncClient, db_session: AsyncSession, body: dict[str, Any], reason: str | None
) -> None:
    bot, _owner = await owned(client, db_session)
    response = await client.patch(PREFS, json=body)
    assert response.status_code == 422, response.text
    assert code(response) == reason
    db_session.expire_all()
    row = await settings.get(db_session, bot.id)
    assert (row.timezone, row.work_start, row.work_end) == ("Asia/Shanghai", "09:30", "19:00")
    assert list(row.workdays) == [1, 2, 3, 4, 5]
    assert row.daily_turn_budget is None and not row.focus_chats_enc


async def test_a_chat_cannot_end_up_in_both_lists_across_two_requests(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await owned(client, db_session)
    assert (await client.patch(PREFS, json={"ignored_chats": ["oc_a"]})).status_code == 200
    refused = await client.patch(PREFS, json={"focus_chats": ["oc_a"]})
    assert refused.status_code == 422 and code(refused) == "chat_in_both_lists"


async def test_the_budget_cannot_exceed_the_platform_default(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await owned(client, db_session)
    db_session.add(Setting(key="assistant_daily_turn_budget", value=80))
    await db_session.commit()
    assert (await client.get(PREFS)).json()["data"]["platform_daily_turn_budget"] == 80
    refused = await client.patch(PREFS, json={"daily_turn_budget": 81})
    assert refused.status_code == 422 and code(refused) == "budget_over_platform"
    accepted = await client.patch(PREFS, json={"daily_turn_budget": 80})
    assert accepted.status_code == 200 and accepted.json()["data"]["daily_turn_budget"] == 80


async def test_writes_need_csrf_an_interactive_login_and_an_active_assistant(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, owner = await owned(client, db_session)
    csrf = client.headers.pop("X-CSRF-Token")
    assert (await client.patch(PREFS, json={"timezone": "UTC"})).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    bot.assistant_state = "provisioning"
    await db_session.commit()
    assert (await client.get(PREFS)).status_code == 200
    refused = await client.patch(PREFS, json={"timezone": "UTC"})
    assert refused.status_code == 409 and code(refused) == "assistant_not_active"
    as_bot_token(client, monkeypatch, owner)
    assert (await client.get(PREFS)).status_code == 403
    assert (await client.patch(PREFS, json={"timezone": "UTC"})).status_code == 403
    db_session.expire_all()
    assert (await settings.get(db_session, bot.id)).timezone == "Asia/Shanghai"


async def test_an_admin_with_their_own_assistant_changes_only_their_own(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    assert (await client.patch(PREFS, json={"focus_chats": ["oc_owner_focus"]})).status_code == 200
    theirs, _admin = await owned(client, db_session, role="platform_admin")
    seen = await client.get(PREFS)
    assert seen.json()["data"]["focus_chats"] == [] and "oc_owner_focus" not in seen.text
    assert (await client.patch(PREFS, json={"timezone": "UTC"})).status_code == 200
    db_session.expire_all()
    assert (await settings.get(db_session, bot.id)).timezone == "Asia/Shanghai"
    assert (await settings.get(db_session, theirs.id)).timezone == "UTC"


async def test_after_the_key_is_destroyed_chat_lists_cannot_be_written(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    _bot, owner = await owned(client, db_session)
    assert (await client.patch(PREFS, json={"focus_chats": ["oc_focus_1"]})).status_code == 200
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    seen = await client.get(PREFS)
    assert seen.json()["data"]["focus_chats"] == [] and seen.json()["data"]["locked"] is True
    assert "oc_focus_1" not in seen.text
    refused = await client.patch(PREFS, json={"ignored_chats": ["oc_noise_1"]})
    assert refused.status_code == 409 and code(refused) == "content_locked"
    # 不涉及密文的项照常能改。
    assert (await client.patch(PREFS, json={"work_end": "18:00"})).status_code == 200
    assert (await client.patch(PREFS, json={"daily_turn_budget": 50})).status_code == 200
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_my_assistant_settings.py -q`
Expected: FAIL：404 / 405（路由还没有）。

- [ ] **Step 3: 实现每日预算与表单要的视图**

`coreman/core/assistants/owner_view.py`：

导入区加 `from coreman.core.timeutils import utcnow`；`from coreman.core.db.models import (` 的清单里在 `AssistantLifecycle,` 之后加 `AssistantSettings,`。

文件末尾追加：

```python
async def preferences(
    session: AsyncSession, row: AssistantSettings, now: datetime
) -> dict[str, Any]:
    """页面上的设置表单要的全部：`settings.view` 加上每日预算、平台默认值与各项上限。

    `row` 的内容列要已还原（`settings.get` 取出来的就是）。
    """
    return {
        **assistant_settings.view(row, now),
        # 个人值；None 表示跟随平台默认。
        "daily_turn_budget": row.daily_turn_budget,
        "platform_daily_turn_budget": await delivery.default_budget(session),
        "limits": {
            "focus_chats": assistant_settings.MAX_FOCUS,
            "ignored_chats": assistant_settings.MAX_IGNORED,
            "budget_min": delivery.BUDGET_MIN,
        },
        "locked": sealed.is_locked(row),
    }


async def set_budget(
    session: AsyncSession, bot_id: uuid.UUID, value: int | None
) -> AssistantSettings:
    """所有者改自己的每日轮次预算；`None` 是清掉个人值、跟随平台默认。只 flush，调用方提交。

    个人只能调低：调到平台默认值之上，管理员的设置就形同虚设。范围下限与上限由入参模型挡。

    Raises:
        SettingsError: budget_over_platform
    """
    limit = await delivery.default_budget(session)
    if value is not None and value > limit:
        raise assistant_settings.SettingsError("budget_over_platform", limit=limit)
    row = await assistant_settings.get(session, bot_id)
    row.daily_turn_budget = value
    row.updated_at = utcnow()
    await session.flush()
    return row
```

- [ ] **Step 4: 实现两个接口**

`coreman/api/routers/me_assistant.py`：

导入区加：

```python
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints

from coreman.core.assistants import delivery
```

（`from typing import Any` 并进上面这一行；`delivery` 并进已有的 `from coreman.core.assistants import …` 那一行。）

`JOB_ERRORS = …` 之后加：

```python
SETTINGS_ERRORS: dict[str, tuple[int, str]] = {
    "nothing_to_update": (422, "至少修改一项"),
    "invalid_timezone": (422, "不认识的时区，请用 IANA 时区名，例如 Asia/Shanghai"),
    "invalid_work_hours": (
        422,
        "时间要写成 HH:MM，且上班时间必须早于下班时间（不支持跨午夜的时段）",
    ),
    "invalid_workdays": (422, "工作日是 1（周一）到 7（周日），至少选一个，不能重复"),
    "invalid_chats": (422, "会话要填 chat_id（以 oc_ 开头）"),
    "too_many_chats": (
        422,
        f"重点群最多 {assistant_settings.MAX_FOCUS} 个，"
        f"忽略的会话最多 {assistant_settings.MAX_IGNORED} 个",
    ),
    "chat_in_both_lists": (422, "同一个会话不能既是重点群又被忽略"),
    "budget_over_platform": (422, "每日轮次预算不能超过平台默认值"),
    "content_locked": (409, "加密保存的个人内容已不可读取，不能再写入重点群与忽略的会话"),
}
_ChatId = Annotated[str, StringConstraints(max_length=100)]


class SettingsIn(BaseModel):
    """所有者改的设置。这里只挡类型与离谱的长度：取值规则由 `settings.update` 判断并给出原因代码。

    前六项给 null 等同于没给；`daily_turn_budget` 给 null 是清掉个人值、跟随平台默认。
    """

    model_config = ConfigDict(extra="forbid")
    timezone: str | None = Field(default=None, max_length=64)
    work_start: str | None = Field(default=None, max_length=5)
    work_end: str | None = Field(default=None, max_length=5)
    workdays: list[StrictInt] | None = Field(default=None, max_length=7)
    focus_chats: list[_ChatId] | None = Field(default=None, max_length=200)
    ignored_chats: list[_ChatId] | None = Field(default=None, max_length=200)
    daily_turn_budget: StrictInt | None = Field(
        default=None, ge=delivery.BUDGET_MIN, le=delivery.BUDGET_MAX
    )
```

文件末尾追加：

```python
@router.get("/settings")
async def settings_view(
    own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    now = utcnow()
    row = await assistant_settings.get(session, own.bot.id)
    # 重点群与忽略的会话是密文：只对所有者本人还原。
    await sealed.open_row(session, row, viewer=own.actor.id)
    await access_log.record(session, own.bot.id, own.actor.id, "settings.read", "")
    data = await owner_view.preferences(session, row, now)
    # 第一次读会按默认值建设置行。
    await session.commit()
    return {"code": 0, "data": data}


@router.patch("/settings")
async def settings_patch(
    body: SettingsIn,
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    now = utcnow()
    schedule = body.model_dump(exclude={"daily_turn_budget"}, exclude_none=True)
    # None 对预算是合法取值，不能用它判断「没传」。
    budget_given = "daily_turn_budget" in body.model_fields_set
    try:
        if not schedule and not budget_given:
            raise assistant_settings.SettingsError("nothing_to_update")
        if schedule:
            await assistant_settings.update(session, own.bot.id, now=now, **schedule)
        if budget_given:
            await owner_view.set_budget(session, own.bot.id, body.daily_turn_budget)
    except assistant_settings.SettingsError as exc:
        # 一项不过，这次请求里已经做了的改动一并回滚。
        await session.rollback()
        raise refuse(exc.code, SETTINGS_ERRORS) from None
    row = await assistant_settings.get(session, own.bot.id)
    data = await owner_view.preferences(session, row, now)
    await session.commit()
    return {"code": 0, "data": data}
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/api/test_my_assistant_settings.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: PASS（台账不变：`session.get` 取设置行不是 `select`，写入经 ORM 属性赋值）。

Run: `uv run pytest tests/integration tests/api tests/unit -q -k "assistant_settings or shift or cold_start"`
Expected: PASS（2F 的设置、上下班与冷启动用例不受影响）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add coreman/core/assistants/owner_view.py coreman/api/routers/me_assistant.py \
  tests/api/test_my_assistant_settings.py
git commit -m "我的助理接口：所有者改时区、工作时段、重点群、忽略的会话与每日预算，逐项校验"
```

---

### Task 7: 停用，以及整个前缀的鉴权与隔离台账

**Files:**
- Create: `coreman/api/assistant_deprovision.py`
- Modify: `coreman/api/routers/assistants_admin.py`（`deprovision` 的函数体与导入）
- Modify: `coreman/api/routers/me_assistant.py`
- Test: `tests/api/test_my_assistant_guard.py`（新建）；`tests/api/test_assistant_admin.py`（不改，回归）

**Interfaces:**
- Consumes: `deprovisioning.request`、`lifecycle.live`、`record_audit`、`client_ip`；Task 1–6 的全部接口；`tests.integration.job_helpers`。
- Produces：
  - `async def assistant_deprovision.begin(session: AsyncSession, request: Request, bot: Bot, *, trigger: str, actor: User) -> str`：开始停用，返回 `"deprovisioning"` 或 `"deprovisioned"`；只在这一次真的开始了停用时记一条审计（`assistant.deprovision`）。调用方提交。
  - `POST /api/me/assistant/deprovision`（`{"confirm": true}`）→ `{"state": "deprovisioning" | "deprovisioned"}`。
  - `tests/api/test_my_assistant_guard.py::CASES`：前缀下每一条路由的一次合法调用；新增路由不登记进来，用例失败。

- [ ] **Step 1: 写失败测试**

`tests/api/test_my_assistant_guard.py`（新建）：

```python
"""`/api/me/assistant` 整个前缀的台账：每条路由都只认交互登录的所有者本人，写操作都要 CSRF。

新增一条路由而不登记进 `CASES`，第一个用例就失败：先想清楚它对「别人」「机器人令牌」「没带
CSRF」各自怎么表现，再登记。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, notes
from coreman.core.db.models import (
    AssistantApproval,
    AssistantJob,
    AssistantLifecycle,
    AuditLog,
    Bot,
    User,
)
from coreman.core.timeutils import utcnow
from tests.api.conftest import login_as, login_existing
from tests.api.my_assistant_helpers import URL, as_bot_token, code
from tests.integration.assistant_helpers import SECRET
from tests.integration.job_helpers import owner_with_key, reloaded, spawned

pytestmark = pytest.mark.usefixtures("assistant_master")
PAGE = "规则/口吻"
SAFE = frozenset({"GET"})
# （方法，路由模板）→ 一次合法调用的参数；`{job}` 在用例里换成所有者那个子任务的编号。
CASES: dict[tuple[str, str], dict[str, Any]] = {
    ("GET", ""): {},
    ("GET", "/attention"): {},
    ("GET", "/jobs"): {},
    ("POST", "/jobs/{job_id}/cancel"): {"path": "/jobs/{job}/cancel"},
    ("GET", "/followups"): {},
    ("POST", "/followups"): {"json": {"category": "remind", "title": "新的一条"}},
    ("PATCH", "/followups/{label}"): {"path": "/followups/F001", "json": {"title": "改掉"}},
    ("POST", "/followups/{label}/close"): {"path": "/followups/F001/close", "json": {}},
    ("POST", "/followups/{label}/snooze"): {
        "path": "/followups/F001/snooze",
        "json": {"until": "2099-01-01"},
    },
    ("GET", "/approvals/recent"): {},
    ("GET", "/notes"): {},
    ("GET", "/notes/page"): {"params": {"path": PAGE}},
    ("PUT", "/notes/page"): {"json": {"path": PAGE, "body": "改掉", "version": 1}},
    ("DELETE", "/notes/page"): {"params": {"path": PAGE}},
    ("GET", "/settings"): {},
    ("PATCH", "/settings"): {"json": {"timezone": "UTC"}},
    ("POST", "/deprovision"): {"json": {"confirm": True}},
}


def test_every_route_under_the_prefix_is_in_the_table(app: FastAPI) -> None:
    found = {
        (method, route.path.removeprefix(URL))
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith(URL)
        for method in route.methods
    }
    assert found == set(CASES)


@dataclass
class Seeded:
    """一位所有者和他的全部内容；`intact` 核对它们一样都没被动过。"""

    bot: Bot
    owner: User
    job: AssistantJob

    async def intact(self, db_session: AsyncSession) -> None:
        db_session.expire_all()
        bot = await db_session.get(Bot, self.bot.id, populate_existing=True)
        assert bot is not None and bot.assistant_state == "active" and bot.enabled
        row = await followups.get(db_session, self.bot.id, 1)
        assert row.status == "open" and row.title_enc == f"续约 {SECRET}"
        page = await notes.read(db_session, self.bot.id, PAGE)
        assert page is not None and page.version == 1 and page.body == f"回复要短。{SECRET}"
        assert (await reloaded(db_session, self.job)).status == "queued"
        flows = await db_session.scalars(
            select(AssistantLifecycle).where(AssistantLifecycle.bot_id == self.bot.id)
        )
        assert list(flows) == []


async def _seeded(db_session: AsyncSession) -> Seeded:
    bot, owner = await owner_with_key(db_session)
    await followups.add(
        db_session, bot.id, category="remind", title=f"续约 {SECRET}", due_on=date(2000, 1, 1)
    )
    await notes.write(
        db_session, bot.id, PAGE, body=f"回复要短。{SECRET}", summary="口吻", pinned=True
    )
    db_session.add(
        AssistantApproval(
            bot_id=bot.id,
            tool="feishu_im_send_message",
            args_enc={"abstract": f"私聊 → 李四 {SECRET}", "calls": []},
            args_hash=uuid.uuid4().hex,
            request_hash=uuid.uuid4().hex,
            channel="p2p",
            external_id=f"cm-pa-{uuid.uuid4().hex}",
            expires_at=utcnow() + timedelta(hours=72),
            status="pending",
        )
    )
    job = await spawned(db_session, bot, title=f"退货原因 {SECRET}", brief=f"查一下 {SECRET}")
    await db_session.commit()
    return Seeded(bot, owner, job)


async def _call(
    client: httpx.AsyncClient, seeded: Seeded, method: str, template: str
) -> httpx.Response:
    spec = CASES[(method, template)]
    path = spec.get("path", template).replace("{job}", str(seeded.job.id))
    return await client.request(
        method, f"{URL}{path}", params=spec.get("params"), json=spec.get("json")
    )


async def test_signed_out_gets_nothing(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    seeded = await _seeded(db_session)
    client.headers["X-CSRF-Token"] = "t"
    client.cookies.set("coreman_csrf", "t")
    for method, template in CASES:
        response = await _call(client, seeded, method, template)
        assert response.status_code == 401, (method, template)
        assert SECRET not in response.text
    await seeded.intact(db_session)


async def test_a_bot_token_gets_nothing_even_for_the_owner(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seeded(db_session)
    await login_existing(client, db_session, seeded.owner)
    as_bot_token(client, monkeypatch, seeded.owner)
    for method, template in CASES:
        response = await _call(client, seeded, method, template)
        assert response.status_code == 403, (method, template)
        assert SECRET not in response.text
    await seeded.intact(db_session)


async def test_every_write_needs_the_csrf_header(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    seeded = await _seeded(db_session)
    await login_existing(client, db_session, seeded.owner)
    del client.headers["X-CSRF-Token"]
    for method, template in CASES:
        if method in SAFE:
            continue
        response = await _call(client, seeded, method, template)
        assert response.status_code == 403, (method, template)
    await seeded.intact(db_session)


@pytest.mark.parametrize("role", ["platform_admin", "ai_committee", "team_lead", "member"])
async def test_someone_without_an_assistant_reaches_nothing_of_the_owners(
    client: httpx.AsyncClient, db_session: AsyncSession, role: str
) -> None:
    seeded = await _seeded(db_session)
    await login_as(client, db_session, role=role)
    for method, template in CASES:
        response = await _call(client, seeded, method, template)
        assert SECRET not in response.text, (method, template)
        assert str(seeded.bot.id) not in response.text
        if (method, template) == ("GET", ""):
            assert response.json()["data"]["exists"] is False
        else:
            assert response.status_code == 404 and code(response) == "no_assistant", (
                method,
                template,
            )
    await seeded.intact(db_session)


async def test_an_admin_with_their_own_assistant_only_ever_touches_their_own(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    seeded = await _seeded(db_session)
    theirs, admin = await owner_with_key(
        db_session, chat_id="oc_admin", platform_user_id="admin_pid", open_id="ou_admin"
    )
    admin.role = "platform_admin"
    await db_session.commit()
    await login_existing(client, db_session, admin)
    for method, template in CASES:
        response = await _call(client, seeded, method, template)
        assert SECRET not in response.text, (method, template)
        assert str(seeded.bot.id) not in response.text
    # 最后一条是停用：停的是管理员自己的助理，所有者的原封不动。
    await seeded.intact(db_session)
    await db_session.refresh(theirs)
    assert theirs.assistant_state == "deprovisioning"


@pytest.mark.parametrize("confirm", ["true", 1, False, None, "yes"])
async def test_stopping_needs_a_literal_true(
    client: httpx.AsyncClient, db_session: AsyncSession, confirm: object
) -> None:
    seeded = await _seeded(db_session)
    await login_existing(client, db_session, seeded.owner)
    response = await client.post(f"{URL}/deprovision", json={"confirm": confirm})
    assert response.status_code == 422, response.text
    assert (await client.post(f"{URL}/deprovision", json={})).status_code == 422
    assert (
        await client.post(f"{URL}/deprovision", json={"confirm": True, "reason": "x"})
    ).status_code == 422
    await seeded.intact(db_session)


async def test_the_owner_stops_their_assistant_once(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    seeded = await _seeded(db_session)
    await login_existing(client, db_session, seeded.owner)
    response = await client.post(f"{URL}/deprovision", json={"confirm": True})
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"state": "deprovisioning"}
    assert response.headers["cache-control"] == "no-store"
    await db_session.refresh(seeded.bot)
    assert seeded.bot.assistant_state == "deprovisioning"
    [flow] = list(
        await db_session.scalars(
            select(AssistantLifecycle).where(AssistantLifecycle.bot_id == seeded.bot.id)
        )
    )
    assert (flow.action, flow.step, flow.trigger) == ("deprovision", "drain", "owner")
    assert flow.actor_id == seeded.owner.id
    # 再点一次不重复做，也不重复记审计。
    again = await client.post(f"{URL}/deprovision", json={"confirm": True})
    assert again.json()["data"] == {"state": "deprovisioning"}
    audits = list(
        await db_session.scalars(select(AuditLog).where(AuditLog.action == "assistant.deprovision"))
    )
    assert len(audits) == 1 and audits[0].actor_id == seeded.owner.id
    assert audits[0].target_id == str(seeded.bot.id)
    # 停用开始之后：还能看，不能再改。
    assert (await client.get(f"{URL}/followups")).status_code == 200
    assert (await client.get(URL)).json()["data"]["state"] == "deprovisioning"
    refused = await client.post(f"{URL}/followups/F001/close", json={})
    assert refused.status_code == 409 and code(refused) == "assistant_not_active"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_my_assistant_guard.py -q`
Expected: FAIL：台账用例少一条 `("POST", "/deprovision")`；停用相关的用例 404 / 405。

- [ ] **Step 3: 把「开始停用并记审计」提成公共函数**

`coreman/api/assistant_deprovision.py`（新建）：

```python
"""开始停用一位助理并记审计：管理员在助理列表里停用、所有者在「我的助理」页停用，共用这一处。

停用不可撤销，重复请求不重复做；审计只在这一次真的开始了停用时记（新开的停用流程，或把宽限期里
的停用推进到现在），重复点、已经停用完的都不记。
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip
from coreman.core.assistants import deprovisioning, lifecycle
from coreman.core.audit import record_audit
from coreman.core.db.models import Bot, User


async def begin(
    session: AsyncSession, request: Request, bot: Bot, *, trigger: str, actor: User
) -> str:
    """开始停用 `bot`，返回它之后的状态：`deprovisioning`，或早已停用完的 `deprovisioned`。

    Args:
        trigger: `owner`（所有者本人）或 `admin`（平台管理员）

    调用方提交。
    """
    prior = await lifecycle.live(session, bot.owner_id, lock=False) if bot.owner_id else None
    before = (prior.id, prior.action, prior.step) if prior is not None else None
    flow = await deprovisioning.request(session, bot, trigger=trigger, actor_id=actor.id)
    started = flow is not None and (
        before is None
        or before[0] != flow.id
        or before[1] != "deprovision"
        or (before[2] == "grace" and flow.step != "grace")
    )
    if started:
        await record_audit(
            session,
            action="assistant.deprovision",
            actor_id=actor.id,
            actor_login=actor.login_name,
            target_type="bot",
            target_id=str(bot.id),
            diff={},
            ip=client_ip(request),
        )
    return "deprovisioning" if flow else "deprovisioned"
```

`coreman/api/routers/assistants_admin.py`：`deprovision` 函数里，从 `prior = await lifecycle.live(…)` 起到 `await session.commit()` 之前的全部语句换成一行，函数变为：

```python
@router.post("/{bot_id}/deprovision")
async def deprovision(
    bot_id: uuid.UUID,
    body: DeprovisionIn,
    request: Request,
    user: User = Depends(_require_platform_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if body.confirm is not True:
        raise ApiError(422, 422, "停用不可撤销，请带上 confirm: true")
    bot = await session.get(Bot, bot_id)
    if bot is None or bot.kind != "assistant":
        raise not_found("个人助理不存在")
    state = await assistant_deprovision.begin(session, request, bot, trigger="admin", actor=user)
    await session.commit()
    return {"code": 0, "data": {"bot_id": str(bot_id), "state": state}}
```

导入区加 `from coreman.api import assistant_deprovision`；这个文件里不再用到的名字（`client_ip`、`record_audit`、`deprovisioning`、`lifecycle`——以 `uv run ruff check coreman/api/routers/assistants_admin.py` 报的 F401 为准）从导入里删掉。

Run: `uv run pytest tests/api/test_assistant_admin.py -q`
Expected: PASS（管理员停用的行为与审计不变）。

- [ ] **Step 4: 实现所有者的停用接口**

`coreman/api/routers/me_assistant.py`：

导入区加：

```python
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints

from coreman.api import assistant_deprovision
from coreman.api.errors import ApiError
```

（前两行分别替换已有的 `from fastapi import …` 与 `from pydantic import …`：只是各多了 `Request` 与 `StrictBool`。）

`class SettingsIn(BaseModel): …` 的定义之后加：

```python
class DeprovisionIn(BaseModel):
    """停用不可撤销：必须明确带上 `confirm: true`（JSON 的 true；"true"、1 都不算）。"""

    model_config = ConfigDict(extra="forbid")
    confirm: StrictBool
```

文件末尾追加：

```python
@router.post("/deprovision")
async def deprovision(
    body: DeprovisionIn,
    request: Request,
    own: Mine = Depends(mine),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """所有者停用自己的助理：与私聊里发「停用助理」并确认是同一件事，一旦开始不可撤销。

    不要求助理处于可用状态：开通到一半、已经在停用中都可以点（后者不重复做）。
    """
    if body.confirm is not True:
        raise ApiError(422, 422, "停用不可撤销，请带上 confirm: true")
    state = await assistant_deprovision.begin(
        session, request, own.bot, trigger="owner", actor=own.actor
    )
    await session.commit()
    return {"code": 0, "data": {"state": state}}
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/api/test_my_assistant_guard.py tests/api/test_assistant_admin.py -q`
Expected: PASS。

Run: `uv run pytest tests/api -q -k "my_assistant"` 与 `uv run pytest tests/unit/test_assistant_sealed_ledger.py tests/unit/test_assistant_access_log.py -q`
Expected: PASS（本层后端的全部用例）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman && uv run alembic check`
Expected: 全部通过；`alembic check` 报没有待生成的迁移。

- [ ] **Step 6: 提交**

```bash
git add coreman/api/assistant_deprovision.py coreman/api/routers/assistants_admin.py \
  coreman/api/routers/me_assistant.py tests/api/test_my_assistant_guard.py
git commit -m "我的助理接口：所有者在页面上停用；整个前缀的鉴权与隔离有一张台账守着"
```

---

### Task 8: 前端的接口封装、三种语言的文案、三个只读分区

**Files:**
- Create: `web/src/api/myAssistant.ts`
- Create: `web/src/i18n/assistant.ts`
- Modify: `web/src/i18n/index.ts`、`web/src/i18n/zh.ts`、`web/src/i18n/en.ts`、`web/src/i18n/ja.ts`（`menu` 一节各加两个键）
- Create: `web/src/components/assistant/AssistantAttention.vue`、`AssistantJobs.vue`、`AssistantRecent.vue`
- Test: `web/tests/assistantI18n.spec.ts`（新建）、`web/tests/AssistantSections.spec.ts`（新建）

**Interfaces:**
- Consumes: Task 1–7 的接口；`@/api/client` 的 `call` / `http`；`@/components/LoadState.vue`；`@/utils/errors` 的 `errorMessage`；`@/utils/format` 的 `formatDateTime`。
- Produces：
  - `@/api/myAssistant`：类型 `AssistantOverview`、`AssistantAbsent`、`AssistantFlow`、`FollowupCategory`、`Followup`、`FollowupList`、`FollowupIn`、`FollowupPatch`、`AssistantJob`、`JobList`、`PendingApproval`、`SettledApproval`、`Attention`、`NoteEntry`、`NotePage`、`NoteIndex`、`NoteIn`、`AssistantPrefs`、`AssistantPrefsPatch`；常量 `FOLLOWUP_CATEGORIES`；对象 `myAssistant`，方法 `overview`、`attention`、`jobs`、`cancelJob(id)`、`followups`、`addFollowup(body)`、`updateFollowup(id, body)`、`closeFollowup(id, dropped)`、`snoozeFollowup(id, until)`、`recentApprovals`、`notes`、`page(path)`、`savePage(body)`、`deletePage(path)`、`settings`、`saveSettings(body)`、`deprovision`。
  - `@/i18n/assistant`：`assistantZh`、`assistantEn`、`assistantJa`（后两个的类型是 `typeof assistantZh`），挂在各语言消息的 `assistant` 键下；文案键在组件里一律写 `assistant.<分区>.<键>`。本任务一次写全后面各任务要用的文案（`page`、`empty`、`overview`、`attention`、`jobs`、`followups`、`recent`、`notes`、`settings`、`admin`、`platform`），后面的任务不再改这个文件。
  - 菜单名 `menu.myAssistant`、`menu.assistants`（三种语言）。
  - 三个组件都没有属性，挂载时自己取数，`defineExpose({ load })`。

- [ ] **Step 1: 写接口封装**

`web/src/api/myAssistant.ts`（新建）：

```ts
import { call, http } from '@/api/client'

/** 「我的助理」页的接口：只对所有者本人，助理由登录人推出，路径里没有助理编号。 */

export interface AssistantFlow { action: string; step: string }

/** 没有助理时的空状态：能不能开通、不能的原因代号、怎么开通。 */
export interface AssistantAbsent {
  exists: false
  eligible: boolean
  reason: 'inactive' | 'no_login_name' | 'not_listed' | null
  how_to: { app_name: string | null; steps: string[] }
  lifecycle: AssistantFlow | null
}

export interface AssistantOverview {
  exists: true
  bot_id: string
  name: string
  state: 'provisioning' | 'active' | 'deprovisioning' | 'deprovisioned' | null
  runtime: { name: string; online: boolean } | null
  /** 可用状态下现在跑不了轮次的原因代码；能跑是 null。 */
  paused_reason: string | null
  on_shift: boolean
  cold_start: { state: 'pending' | 'in_progress' | 'done'; step: string | null; outcome: string | null }
  turns: { today: number; budget: number }
  watch: { state: string; last_ok_at: string | null; last_error: string | null } | null
  lifecycle: AssistantFlow | null
  /** 数据密钥已不可用：内容列都读不出来，页面只显示元数据。 */
  content_locked: boolean
  approval_system: { name: string | null; url: string | null }
}

export type FollowupCategory = 'decide' | 'promised' | 'waiting' | 'remind'
export const FOLLOWUP_CATEGORIES: readonly FollowupCategory[] = ['decide', 'promised', 'waiting', 'remind']

export interface Followup {
  id: string
  category: FollowupCategory
  status: 'open' | 'snoozed' | 'done' | 'dropped'
  title: string
  notes?: string | null
  review_on: string | null
  due_on: string | null
  origin_message_id: string | null
  created_at: string
  due_today?: boolean
  close_code?: string
  locked?: boolean
}
export interface FollowupList { today: string; groups: Record<FollowupCategory, Followup[]>; total: number }
export interface FollowupIn {
  category: FollowupCategory
  title: string
  notes?: string | null
  review_on?: string | null
  due_on?: string | null
}
export type FollowupPatch = Partial<FollowupIn>

export interface AssistantJob {
  id: string
  kind: string
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'blocked'
  title: string
  rounds: number
  created_at: string
  finished_at: string | null
  has_result: boolean
  error_code?: string
  brief?: string
  result?: string | null
  blocked_reason?: string | null
  locked?: boolean
}
export interface JobList { active: AssistantJob[]; recent: AssistantJob[] }

/** 等所有者批的一张单：正文在审批系统里看，这里只有摘要与入口。 */
export interface PendingApproval { id: string; tool: string; summary: string | null; created_at: string; link: string | null }
export interface SettledApproval {
  id: string
  tool: string
  channel: string
  status: 'executed' | 'rejected' | 'withdrawn'
  summary: string | null
  reason: string | null
  error: string | null
  created_at: string
  decided_at: string | null
  executed_at: string | null
  locked: boolean
}
export interface Attention { today: string; approvals: PendingApproval[]; jobs: AssistantJob[]; followups: Followup[] }

export interface NoteEntry {
  path: string
  title: string
  summary: string
  pinned: boolean
  version: number
  size: number
  updated_by: 'owner' | 'assistant' | 'import'
  updated_at: string
}
export interface NotePage extends NoteEntry { body: string }
export interface NoteIndex { items: NoteEntry[]; locked: boolean }
/** `version` 是读到的版本，0 表示新建；`pinned` 省略表示保持原样。 */
export interface NoteIn { path: string; body: string; summary: string; title?: string | null; pinned?: boolean | null; version: number }

export interface AssistantPrefs {
  timezone: string
  work_start: string
  work_end: string
  workdays: number[]
  focus_chats: string[]
  ignored_chats: string[]
  /** null = 跟随平台默认。 */
  daily_turn_budget: number | null
  platform_daily_turn_budget: number
  limits: { focus_chats: number; ignored_chats: number; budget_min: number }
  locked: boolean
  /** 以下几项来自后端的 `settings.view`，表单不改它们。 */
  on_shift: boolean
  shift_override: 'on' | 'off' | null
  shift_override_until: string | null
  cold_start: 'pending' | 'in_progress' | 'done'
  today: string
}
export type AssistantPrefsPatch = Partial<Pick<AssistantPrefs,
  'timezone' | 'work_start' | 'work_end' | 'workdays' | 'focus_chats' | 'ignored_chats' | 'daily_turn_budget'>>

const root = '/api/me/assistant'
export const myAssistant = {
  overview: () => call<AssistantOverview | AssistantAbsent>(http.get(root)),
  attention: () => call<Attention>(http.get(`${root}/attention`)),
  jobs: () => call<JobList>(http.get(`${root}/jobs`)),
  cancelJob: (id: string) => call<{ id: string; status: string }>(http.post(`${root}/jobs/${id}/cancel`)),
  followups: () => call<FollowupList>(http.get(`${root}/followups`)),
  addFollowup: (body: FollowupIn) => call<Followup>(http.post(`${root}/followups`, body)),
  updateFollowup: (id: string, body: FollowupPatch) => call<Followup>(http.patch(`${root}/followups/${id}`, body)),
  closeFollowup: (id: string, dropped = false) => call<Followup>(http.post(`${root}/followups/${id}/close`, { dropped })),
  snoozeFollowup: (id: string, until: string) => call<Followup>(http.post(`${root}/followups/${id}/snooze`, { until })),
  recentApprovals: () => call<{ items: SettledApproval[] }>(http.get(`${root}/approvals/recent`)),
  notes: () => call<NoteIndex>(http.get(`${root}/notes`)),
  page: (path: string) => call<NotePage>(http.get(`${root}/notes/page`, { params: { path } })),
  savePage: (body: NoteIn) => call<NoteEntry>(http.put(`${root}/notes/page`, body)),
  deletePage: (path: string) => call<{ deleted: boolean }>(http.delete(`${root}/notes/page`, { params: { path } })),
  settings: () => call<AssistantPrefs>(http.get(`${root}/settings`)),
  saveSettings: (body: AssistantPrefsPatch) => call<AssistantPrefs>(http.patch(`${root}/settings`, body)),
  /** 停用不可撤销：调用前必须已经让所有者二次确认。 */
  deprovision: () => call<{ state: string }>(http.post(`${root}/deprovision`, { confirm: true })),
}
```

- [ ] **Step 2: 写失败测试（文案）**

`web/tests/assistantI18n.spec.ts`（新建）：

```ts
import { describe, expect, it } from 'vitest'
import { i18n } from '@/i18n'
import { assistantEn, assistantJa, assistantZh } from '@/i18n/assistant'

function flatten(value: object, prefix = ''): Record<string, string> {
  return Object.fromEntries(Object.entries(value).flatMap(([key, entry]) => {
    const path = prefix ? `${prefix}.${key}` : key
    return typeof entry === 'string' ? [[path, entry]] : Object.entries(flatten(entry, path))
  }))
}
const placeholders = (text: string) => text.match(/\{\w+\}/g)?.sort() ?? []
// tests/i18n.spec.ts 的渲染用例只提供这些插值参数：用了别的名字，那条用例会因为缺参数而告警。
const ALLOWED = ['{name}', '{count}', '{n}', '{total}', '{time}', '{value}', '{detail}', '{field}', '{start}', '{end}']

describe('assistant messages', () => {
  const zh = flatten(assistantZh), en = flatten(assistantEn), ja = flatten(assistantJa)

  it('has the same keys and placeholders in all three languages', () => {
    expect(Object.keys(en).sort()).toEqual(Object.keys(zh).sort())
    expect(Object.keys(ja).sort()).toEqual(Object.keys(zh).sort())
    for (const [key, source] of Object.entries(zh)) {
      expect(placeholders(en[key]), key).toEqual(placeholders(source))
      expect(placeholders(ja[key]), key).toEqual(placeholders(source))
      for (const name of placeholders(source)) expect(ALLOWED, key).toContain(name)
    }
  })

  it('leaves no message empty and avoids vue-i18n syntax characters', () => {
    for (const messages of [zh, en, ja]) {
      for (const [key, text] of Object.entries(messages)) {
        expect(text.trim(), key).not.toBe('')
        expect(text.replace(/\{\w+\}/g, ''), key).not.toMatch(/[@{}|]/)
      }
    }
  })

  it('keeps Chinese and Japanese characters out of the English messages', () => {
    for (const [key, text] of Object.entries(en)) {
      expect(text, key).not.toMatch(/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u)
    }
  })

  it('is mounted under assistant with menu names in every locale', () => {
    for (const locale of ['zh', 'en', 'ja'] as const) {
      const messages = i18n.global.messages.value[locale] as unknown as Record<string, Record<string, unknown>>
      expect(messages.assistant.page, locale).toBeTruthy()
      expect(messages.menu.myAssistant, locale).toBeTruthy()
      expect(messages.menu.assistants, locale).toBeTruthy()
    }
    expect(i18n.global.t('assistant.followups.categories.decide')).toBe('等我决定')
  })
})
```

- [ ] **Step 3: 运行确认失败**

Run（在 `web/` 下）: `npx vitest run tests/assistantI18n.spec.ts`
Expected: FAIL：找不到模块 `@/i18n/assistant`。

- [ ] **Step 4: 写三种语言的文案**

`web/src/i18n/assistant.ts`（新建）：

```ts
// 「我的助理」页、管理台的助理列表、平台设置里的助理设置项。
// 避免在文案里写 @ { } |：vue-i18n 会当成语法。插值参数名只用 name、count、n、total、time、value、detail。
export const assistantZh = {
  page: {
    intro: '以你的飞书身份工作的个人助理。这里的内容只有你本人看得到，管理员也看不到。',
    refresh: '刷新',
    tabs: { attention: '等你出手', jobs: '正在跑', followups: '待跟进', recent: '最近落地', notes: '知识库', settings: '设置' },
    locked: '加密保存的个人内容已不可读取（数据密钥已销毁），下面只显示编号、状态与时间。',
    lockedText: '（内容已不可读取）',
  },
  empty: {
    title: '你还没有个人助理',
    eligible: '按下面的步骤开通：',
    app: '助理应用：{name}',
    steps: {
      open_private_chat: '在飞书里搜索助理应用，打开和它的私聊，随便发一条消息。',
      accept_offer: '点它回的卡片上的「开通」。',
      authorize: '按提示完成飞书授权，授权完成后助理开始工作。',
    },
    reasons: {
      inactive: '你的账号已停用，开通不了个人助理。',
      no_login_name: '你的账号还没有登录名，开通不了个人助理，请联系管理员。',
      not_listed: '你暂时不在个人助理的开通范围内，如需开通请联系管理员。',
    },
    inProgress: '正在开通或停用中，请按飞书私聊里的提示继续。',
  },
  overview: {
    state: '状态',
    states: { provisioning: '开通中', active: '可用', deprovisioning: '停用中', deprovisioned: '已停用', unknown: '未知' },
    runtime: '运行时', runtimeNone: '未分配', online: '在线', offline: '离线',
    paused: '暂时跑不了，原因代码 {detail}',
    shift: '工作时段', onShift: '在班', offShift: '下班',
    coldStart: '冷启动', coldStartStates: { pending: '未开始', in_progress: '进行中', done: '已完成' },
    turns: '今日轮次', turnsValue: '{n} / {total}',
    watch: '盯消息', watchNone: '还没开始',
    watchStates: { idle: '空闲', polling: '轮询中', backoff: '退避中', paused: '已暂停', stopped: '已停止' },
    watchLastOk: '上次成功 {time}', watchError: '原因代码 {detail}',
  },
  attention: {
    approvals: '待批的审批单', approvalsEmpty: '没有等你批的审批单。',
    open: '去审批系统处理', noLink: '请到审批系统里处理', submittedAt: '提交于 {time}',
    jobs: '卡住的子任务', jobsEmpty: '没有卡住的子任务。', blockedReason: '卡住的原因',
    followups: '今天到期的待跟进', followupsEmpty: '今天没有到期的待跟进。',
  },
  jobs: {
    active: '进行中', activeEmpty: '没有进行中的子任务。', recent: '最近结束', recentEmpty: '还没有结束的子任务。',
    brief: '交代原文', result: '结论', rounds: '第 {n} 轮', startedAt: '派出于 {time}', finishedAt: '结束于 {time}',
    statuses: { queued: '排队中', running: '进行中', done: '已完成', failed: '失败', cancelled: '已取消', blocked: '卡住' },
    cancel: '取消', cancelConfirm: '取消子任务「{name}」？助理会在对话里得知这件事。', cancelled: '已取消',
    errorCode: '原因代码 {detail}',
  },
  followups: {
    categories: { decide: '等我决定', promised: '我答应的', waiting: '等别人', remind: '提醒我' },
    empty: '这一类没有待跟进。', add: '新增', title: '标题', notes: '备注', dueOn: '期限', category: '分类',
    done: '办完', drop: '不再需要', dropConfirm: '把 {name} 标为不再需要？',
    snooze: '推后', snoozePrompt: '推后到哪一天再看（必须晚于今天）', snoozeInvalid: '请填一个晚于今天的日期',
    snoozedUntil: '推后到 {time}', dueToday: '今天到期', due: '期限 {time}', review: '回看 {time}',
    added: '已新增', updated: '已更新', closed: '已关闭', snoozed: '已推后', titleRequired: '请填写标题',
  },
  recent: {
    empty: '最近没有落地的审批单。', reason: '审批人的话',
    statuses: { executed: '已执行', rejected: '被拒', withdrawn: '已撤回' },
    decidedAt: '决定于 {time}', executedAt: '执行于 {time}',
  },
  notes: {
    intro: '助理的个人知识库。置顶页会在每天第一轮原样交给助理，当作你的偏好和规则；只有你本人能在这里置顶、取消置顶或改写置顶页。',
    empty: '知识库还是空的。', path: '路径', title: '标题', summary: '摘要',
    pinned: '置顶', pinnedHint: '每天第一轮原样交给助理',
    authors: { owner: '我', assistant: '助理', import: '导入' }, updatedBy: '最后修改：{name}', updatedAt: '修改于 {time}',
    open: '查看', edit: '编辑', remove: '删除', create: '新建一页', body: '正文', save: '保存', cancel: '取消',
    saved: '已保存', deleted: '已删除', removeConfirm: '删除「{name}」？删除后无法恢复。', version: '版本 {n}',
    pathHint: '例如：人/李四、规则/口吻', required: '路径和正文都要填写',
    conflict: '这一页刚被改动过（多半是助理写的），已载入最新内容，请核对后再保存。',
  },
  settings: {
    timezone: '时区', timezoneHint: 'IANA 时区名，例如 Asia/Shanghai、America/New_York。',
    workStart: '上班时间', workEnd: '下班时间', workdays: '工作日',
    weekdays: { d1: '周一', d2: '周二', d3: '周三', d4: '周四', d5: '周五', d6: '周六', d7: '周日' },
    focusChats: '重点群', focusChatsHint: '一行一个群的 chat_id（oc_ 开头），最多 {count} 个。这些群里的消息立刻交给助理。',
    ignoredChats: '忽略的会话', ignoredChatsHint: '一行一个 chat_id，最多 {count} 个。这些会话里的消息助理不看。',
    budget: '每日轮次预算', budgetFollow: '跟随平台默认（{value} 轮）', budgetHint: '可以调低，不能超过平台默认的 {value} 轮。',
    save: '保存设置', saved: '设置已保存', nothingChanged: '没有修改',
    errors: {
      timezone: '不认识的时区', time: '时间要写成 HH:MM', hours: '上班时间必须早于下班时间（不支持跨午夜）',
      workdays: '至少选一个工作日', chatId: '有一行不是 chat_id（应以 oc_ 开头）',
      focusTooMany: '重点群太多了', ignoredTooMany: '忽略的会话太多了',
      chatInBoth: '同一个会话不能既是重点群又被忽略', budget: '每日预算超出了允许的范围',
    },
    runtime: '所在运行时', runtimeNone: '未分配',
    danger: '停用助理',
    dangerHint: '停用后：飞书授权撤销，待批的审批单撤回，加密保存的个人内容（对话、知识库、待跟进）永久不可恢复。一旦开始不可撤销。',
    deprovision: '停用我的助理', deprovisionPrompt: '输入助理的名字「{name}」确认停用',
    deprovisionMismatch: '名字不对', deprovisionStarted: '已开始停用',
  },
  admin: {
    intro: '谁开通了个人助理、运行在哪、是否正常。这里只有元数据，看不到任何人的对话、知识库或待跟进内容。',
    owner: '所有者', state: '状态', runtime: '运行时', shift: '在班', coldStart: '冷启动', turns: '今日轮次',
    inbox: '待交回', jobs: '进行中的子任务', watch: '轮询健康', lifecycle: '流程', dirs: '待删目录',
    yes: '是', no: '否', none: '—', empty: '还没有人开通个人助理。',
    online: '在线', offline: '离线',
    deprovision: '停用',
    deprovisionPrompt: '停用后这位成员加密保存的个人内容永久不可恢复，且不可撤销。输入所有者的登录名「{name}」确认',
    deprovisionMismatch: '登录名不对', deprovisionStarted: '已开始停用',
  },
  platform: {
    title: '个人助理', hint: '谁能开通个人助理、用哪种 AI、每个节点同时跑几轮、每人每天最多几轮。',
    eligibility: '可开通范围', modeAll: '全员', modeListed: '指定的人与部门',
    users: '指定的人', departments: '指定的部门（含下级部门）',
    listedEmpty: '选了「指定的人与部门」但名单是空的：保存后谁都开通不了。',
    backends: '允许的 AI', backendFirst: '优先使用', backendAlsoOther: '优先的那种没有可用节点时，也允许用另一种',
    backendNames: { claude: 'Claude', codex: 'Codex' },
    concurrency: '每个节点的并发上限', concurrencyHint: '每个运行时节点同时跑的个人助理轮次，超出的排队。',
    budget: '每日轮次预算默认值', budgetHint: '每位助理每天最多跑的轮次；成员可以在「我的助理」里调低自己的。',
    save: '保存', saved: '已保存', nothingChanged: '没有修改',
  },
}

export const assistantEn: typeof assistantZh = {
  page: {
    intro: 'A personal assistant that works as you in Feishu. Only you can see what is on this page; administrators cannot.',
    refresh: 'Refresh',
    tabs: { attention: 'Needs you', jobs: 'Running', followups: 'Follow-ups', recent: 'Recently settled', notes: 'Knowledge base', settings: 'Settings' },
    locked: 'Your encrypted personal content can no longer be read (the data key was destroyed). Only numbers, states and times are shown below.',
    lockedText: '(content can no longer be read)',
  },
  empty: {
    title: 'You do not have a personal assistant yet',
    eligible: 'Follow these steps to get one:',
    app: 'Assistant app: {name}',
    steps: {
      open_private_chat: 'Search for the assistant app in Feishu, open a direct chat with it and send any message.',
      accept_offer: 'Tap the button on the card it replies with to accept.',
      authorize: 'Complete the Feishu authorization as prompted. The assistant starts working once it is done.',
    },
    reasons: {
      inactive: 'Your account is disabled, so a personal assistant cannot be set up.',
      no_login_name: 'Your account has no login name yet, so a personal assistant cannot be set up. Contact an administrator.',
      not_listed: 'You are not in scope for personal assistants at the moment. Contact an administrator if you need one.',
    },
    inProgress: 'Setup or shutdown is in progress. Follow the prompts in the Feishu direct chat.',
  },
  overview: {
    state: 'State',
    states: { provisioning: 'Setting up', active: 'Available', deprovisioning: 'Shutting down', deprovisioned: 'Shut down', unknown: 'Unknown' },
    runtime: 'Runtime', runtimeNone: 'Not assigned', online: 'Online', offline: 'Offline',
    paused: 'Cannot run turns right now, reason code {detail}',
    shift: 'Working hours', onShift: 'On shift', offShift: 'Off shift',
    coldStart: 'Cold start', coldStartStates: { pending: 'Not started', in_progress: 'In progress', done: 'Done' },
    turns: 'Turns today', turnsValue: '{n} / {total}',
    watch: 'Message watch', watchNone: 'Not started',
    watchStates: { idle: 'Idle', polling: 'Polling', backoff: 'Backing off', paused: 'Paused', stopped: 'Stopped' },
    watchLastOk: 'Last success {time}', watchError: 'Reason code {detail}',
  },
  attention: {
    approvals: 'Approvals waiting for you', approvalsEmpty: 'No approvals are waiting for you.',
    open: 'Open the approval system', noLink: 'Handle it in the approval system', submittedAt: 'Submitted {time}',
    jobs: 'Blocked sub-tasks', jobsEmpty: 'No sub-tasks are blocked.', blockedReason: 'Why it is blocked',
    followups: 'Follow-ups due today', followupsEmpty: 'No follow-ups are due today.',
  },
  jobs: {
    active: 'In progress', activeEmpty: 'No sub-tasks are in progress.', recent: 'Recently finished', recentEmpty: 'No sub-tasks have finished yet.',
    brief: 'Original brief', result: 'Conclusion', rounds: 'Round {n}', startedAt: 'Started {time}', finishedAt: 'Finished {time}',
    statuses: { queued: 'Queued', running: 'Running', done: 'Done', failed: 'Failed', cancelled: 'Cancelled', blocked: 'Blocked' },
    cancel: 'Cancel', cancelConfirm: 'Cancel the sub-task "{name}"? The assistant will be told in the conversation.', cancelled: 'Cancelled',
    errorCode: 'Reason code {detail}',
  },
  followups: {
    categories: { decide: 'Waiting for my decision', promised: 'I promised', waiting: 'Waiting on others', remind: 'Remind me' },
    empty: 'Nothing in this category.', add: 'Add', title: 'Title', notes: 'Notes', dueOn: 'Due date', category: 'Category',
    done: 'Done', drop: 'No longer needed', dropConfirm: 'Mark {name} as no longer needed?',
    snooze: 'Snooze', snoozePrompt: 'Which day should it come back (must be after today)?', snoozeInvalid: 'Enter a date after today',
    snoozedUntil: 'Snoozed until {time}', dueToday: 'Due today', due: 'Due {time}', review: 'Review {time}',
    added: 'Added', updated: 'Updated', closed: 'Closed', snoozed: 'Snoozed', titleRequired: 'Enter a title',
  },
  recent: {
    empty: 'No approvals have been settled recently.', reason: 'Note from the approver',
    statuses: { executed: 'Executed', rejected: 'Rejected', withdrawn: 'Withdrawn' },
    decidedAt: 'Decided {time}', executedAt: 'Executed {time}',
  },
  notes: {
    intro: 'The assistant\'s private knowledge base. Pinned pages are handed to the assistant verbatim in the first turn of each day as your preferences and rules; only you can pin, unpin or rewrite a pinned page, and only here.',
    empty: 'The knowledge base is empty.', path: 'Path', title: 'Title', summary: 'Summary',
    pinned: 'Pinned', pinnedHint: 'Handed to the assistant in the first turn of each day',
    authors: { owner: 'Me', assistant: 'Assistant', import: 'Import' }, updatedBy: 'Last changed by: {name}', updatedAt: 'Changed {time}',
    open: 'View', edit: 'Edit', remove: 'Delete', create: 'New page', body: 'Body', save: 'Save', cancel: 'Cancel',
    saved: 'Saved', deleted: 'Deleted', removeConfirm: 'Delete "{name}"? This cannot be undone.', version: 'Version {n}',
    pathHint: 'For example: people/alex, rules/tone', required: 'Both the path and the body are required',
    conflict: 'This page was just changed (most likely by the assistant). The latest content has been loaded; review it and save again.',
  },
  settings: {
    timezone: 'Time zone', timezoneHint: 'An IANA time zone name, for example Asia/Shanghai or America/New_York.',
    workStart: 'Start of work', workEnd: 'End of work', workdays: 'Working days',
    weekdays: { d1: 'Mon', d2: 'Tue', d3: 'Wed', d4: 'Thu', d5: 'Fri', d6: 'Sat', d7: 'Sun' },
    focusChats: 'Focus groups', focusChatsHint: 'One group chat_id per line (starting with oc_), at most {count}. Messages in these groups go to the assistant immediately.',
    ignoredChats: 'Ignored chats', ignoredChatsHint: 'One chat_id per line, at most {count}. The assistant does not look at messages in these chats.',
    budget: 'Daily turn budget', budgetFollow: 'Follow the platform default ({value} turns)', budgetHint: 'You can lower it, but not above the platform default of {value} turns.',
    save: 'Save settings', saved: 'Settings saved', nothingChanged: 'Nothing changed',
    errors: {
      timezone: 'Unknown time zone', time: 'Write times as HH:MM', hours: 'Work must start before it ends (overnight hours are not supported)',
      workdays: 'Pick at least one working day', chatId: 'A line is not a chat_id (it should start with oc_)',
      focusTooMany: 'Too many focus groups', ignoredTooMany: 'Too many ignored chats',
      chatInBoth: 'A chat cannot be both a focus group and ignored', budget: 'The daily budget is outside the allowed range',
    },
    runtime: 'Runtime', runtimeNone: 'Not assigned',
    danger: 'Shut down the assistant',
    dangerHint: 'After shutdown the Feishu authorization is revoked, pending approvals are withdrawn, and your encrypted personal content (conversations, knowledge base, follow-ups) can never be recovered. It cannot be undone once started.',
    deprovision: 'Shut down my assistant', deprovisionPrompt: 'Type the assistant\'s name "{name}" to confirm the shutdown',
    deprovisionMismatch: 'The name does not match', deprovisionStarted: 'Shutdown started',
  },
  admin: {
    intro: 'Who has a personal assistant, where it runs and whether it is healthy. Only metadata is shown here: nobody\'s conversations, knowledge base or follow-ups are visible.',
    owner: 'Owner', state: 'State', runtime: 'Runtime', shift: 'On shift', coldStart: 'Cold start', turns: 'Turns today',
    inbox: 'Waiting to hand over', jobs: 'Sub-tasks in progress', watch: 'Watch health', lifecycle: 'Flow', dirs: 'Directories to remove',
    yes: 'Yes', no: 'No', none: '—', empty: 'Nobody has a personal assistant yet.',
    online: 'Online', offline: 'Offline',
    deprovision: 'Shut down',
    deprovisionPrompt: 'After shutdown this member\'s encrypted personal content can never be recovered, and it cannot be undone. Type the owner\'s login name "{name}" to confirm',
    deprovisionMismatch: 'The login name does not match', deprovisionStarted: 'Shutdown started',
  },
  platform: {
    title: 'Personal assistants', hint: 'Who may set up a personal assistant, which AI it uses, how many turns each node runs at once and how many turns each person gets per day.',
    eligibility: 'Who may set one up', modeAll: 'Everyone', modeListed: 'Listed people and departments',
    users: 'Listed people', departments: 'Listed departments (including sub-departments)',
    listedEmpty: '"Listed people and departments" is selected but the list is empty: after saving nobody can set one up.',
    backends: 'Allowed AI', backendFirst: 'Preferred', backendAlsoOther: 'Also allow the other one when the preferred one has no available node',
    backendNames: { claude: 'Claude', codex: 'Codex' },
    concurrency: 'Concurrency per node', concurrencyHint: 'Personal assistant turns each runtime node runs at once; the rest wait in the queue.',
    budget: 'Default daily turn budget', budgetHint: 'The most turns each assistant runs per day; members can lower their own under My assistant.',
    save: 'Save', saved: 'Saved', nothingChanged: 'Nothing changed',
  },
}

export const assistantJa: typeof assistantZh = {
  page: {
    intro: 'あなたの飛書アカウントとして働く個人アシスタントです。このページの内容は本人だけが閲覧でき、管理者にも見えません。',
    refresh: '更新',
    tabs: { attention: '対応待ち', jobs: '実行中', followups: 'フォローアップ', recent: '最近の結果', notes: 'ナレッジベース', settings: '設定' },
    locked: '暗号化された個人データは読み取れなくなりました（データ鍵は破棄済み）。以下は番号・状態・時刻のみ表示します。',
    lockedText: '（内容は読み取れません）',
  },
  empty: {
    title: '個人アシスタントはまだありません',
    eligible: '次の手順で開設できます：',
    app: 'アシスタントアプリ：{name}',
    steps: {
      open_private_chat: '飛書でアシスタントアプリを検索し、個人チャットを開いて何かメッセージを送ります。',
      accept_offer: '返信されたカードの「開設」ボタンを押します。',
      authorize: '案内に従って飛書の認可を完了します。完了するとアシスタントが動き始めます。',
    },
    reasons: {
      inactive: 'アカウントが無効のため、個人アシスタントを開設できません。',
      no_login_name: 'アカウントにログイン名がないため、個人アシスタントを開設できません。管理者に連絡してください。',
      not_listed: '現在、個人アシスタントの開設対象に含まれていません。必要な場合は管理者に連絡してください。',
    },
    inProgress: '開設または停止の処理中です。飛書の個人チャットの案内に従ってください。',
  },
  overview: {
    state: '状態',
    states: { provisioning: '開設中', active: '利用可能', deprovisioning: '停止処理中', deprovisioned: '停止済み', unknown: '不明' },
    runtime: 'ランタイム', runtimeNone: '未割り当て', online: 'オンライン', offline: 'オフライン',
    paused: '現在は実行できません。理由コード {detail}',
    shift: '勤務時間', onShift: '勤務中', offShift: '勤務外',
    coldStart: 'コールドスタート', coldStartStates: { pending: '未開始', in_progress: '進行中', done: '完了' },
    turns: '本日のターン', turnsValue: '{n} / {total}',
    watch: 'メッセージ監視', watchNone: '未開始',
    watchStates: { idle: '待機', polling: 'ポーリング中', backoff: 'バックオフ中', paused: '一時停止', stopped: '停止' },
    watchLastOk: '最終成功 {time}', watchError: '理由コード {detail}',
  },
  attention: {
    approvals: '承認待ちの申請', approvalsEmpty: '承認待ちの申請はありません。',
    open: '承認システムで処理する', noLink: '承認システムで処理してください', submittedAt: '提出 {time}',
    jobs: '止まっているサブタスク', jobsEmpty: '止まっているサブタスクはありません。', blockedReason: '止まっている理由',
    followups: '本日期限のフォローアップ', followupsEmpty: '本日期限のフォローアップはありません。',
  },
  jobs: {
    active: '進行中', activeEmpty: '進行中のサブタスクはありません。', recent: '最近終了', recentEmpty: '終了したサブタスクはまだありません。',
    brief: '依頼の原文', result: '結論', rounds: '第 {n} ラウンド', startedAt: '開始 {time}', finishedAt: '終了 {time}',
    statuses: { queued: '待機中', running: '実行中', done: '完了', failed: '失敗', cancelled: 'キャンセル済み', blocked: '停止中' },
    cancel: 'キャンセル', cancelConfirm: 'サブタスク「{name}」をキャンセルしますか？アシスタントには会話の中で伝わります。', cancelled: 'キャンセルしました',
    errorCode: '理由コード {detail}',
  },
  followups: {
    categories: { decide: '自分の判断待ち', promised: '自分が約束した', waiting: '相手待ち', remind: 'リマインド' },
    empty: 'この分類にはありません。', add: '追加', title: 'タイトル', notes: 'メモ', dueOn: '期限', category: '分類',
    done: '完了', drop: '不要になった', dropConfirm: '{name} を不要にしますか？',
    snooze: '後回し', snoozePrompt: 'いつ再確認しますか（今日より後の日付）', snoozeInvalid: '今日より後の日付を入力してください',
    snoozedUntil: '{time} まで後回し', dueToday: '本日期限', due: '期限 {time}', review: '再確認 {time}',
    added: '追加しました', updated: '更新しました', closed: 'クローズしました', snoozed: '後回しにしました', titleRequired: 'タイトルを入力してください',
  },
  recent: {
    empty: '最近決着した申請はありません。', reason: '承認者のコメント',
    statuses: { executed: '実行済み', rejected: '却下', withdrawn: '取り下げ' },
    decidedAt: '決定 {time}', executedAt: '実行 {time}',
  },
  notes: {
    intro: 'アシスタントの個人ナレッジベースです。ピン留めしたページは毎日最初のターンでそのままアシスタントに渡され、あなたの好みやルールとして扱われます。ピン留め・解除・ピン留めページの書き換えは、本人がこの画面でのみ行えます。',
    empty: 'ナレッジベースは空です。', path: 'パス', title: 'タイトル', summary: '概要',
    pinned: 'ピン留め', pinnedHint: '毎日最初のターンでアシスタントに渡されます',
    authors: { owner: '自分', assistant: 'アシスタント', import: 'インポート' }, updatedBy: '最終更新者：{name}', updatedAt: '更新 {time}',
    open: '表示', edit: '編集', remove: '削除', create: '新しいページ', body: '本文', save: '保存', cancel: 'キャンセル',
    saved: '保存しました', deleted: '削除しました', removeConfirm: '「{name}」を削除しますか？元に戻せません。', version: 'バージョン {n}',
    pathHint: '例：人/山田、ルール/口調', required: 'パスと本文の両方が必要です',
    conflict: 'このページは直前に変更されました（おそらくアシスタントによる変更）。最新の内容を読み込みました。確認してからもう一度保存してください。',
  },
  settings: {
    timezone: 'タイムゾーン', timezoneHint: 'IANA タイムゾーン名（例：Asia/Shanghai、America/New_York）。',
    workStart: '始業時刻', workEnd: '終業時刻', workdays: '勤務日',
    weekdays: { d1: '月', d2: '火', d3: '水', d4: '木', d5: '金', d6: '土', d7: '日' },
    focusChats: '重点グループ', focusChatsHint: '1 行に 1 つ、グループの chat_id（oc_ で始まる）を最大 {count} 件。これらのグループのメッセージはすぐにアシスタントへ渡されます。',
    ignoredChats: '無視するチャット', ignoredChatsHint: '1 行に 1 つ、chat_id を最大 {count} 件。これらのチャットのメッセージをアシスタントは見ません。',
    budget: '1 日のターン上限', budgetFollow: 'プラットフォームの既定値に従う（{value} ターン）', budgetHint: '下げることはできますが、プラットフォーム既定の {value} ターンを超えられません。',
    save: '設定を保存', saved: '設定を保存しました', nothingChanged: '変更はありません',
    errors: {
      timezone: '不明なタイムゾーンです', time: '時刻は HH:MM 形式で入力してください', hours: '始業は終業より前にしてください（日をまたぐ時間帯は未対応）',
      workdays: '勤務日を 1 日以上選んでください', chatId: 'chat_id ではない行があります（oc_ で始まる必要があります）',
      focusTooMany: '重点グループが多すぎます', ignoredTooMany: '無視するチャットが多すぎます',
      chatInBoth: '同じチャットを重点グループと無視の両方には指定できません', budget: '1 日の上限が許容範囲外です',
    },
    runtime: '現在のランタイム', runtimeNone: '未割り当て',
    danger: 'アシスタントを停止',
    dangerHint: '停止すると、飛書の認可は取り消され、承認待ちの申請は取り下げられ、暗号化された個人データ（会話、ナレッジベース、フォローアップ）は永久に復元できません。開始後は取り消せません。',
    deprovision: '自分のアシスタントを停止', deprovisionPrompt: '確認のためアシスタント名「{name}」を入力してください',
    deprovisionMismatch: '名前が一致しません', deprovisionStarted: '停止を開始しました',
  },
  admin: {
    intro: '誰が個人アシスタントを開設したか、どこで動いているか、正常かどうかを表示します。ここにあるのはメタデータのみで、誰の会話・ナレッジベース・フォローアップも見えません。',
    owner: '所有者', state: '状態', runtime: 'ランタイム', shift: '勤務中', coldStart: 'コールドスタート', turns: '本日のターン',
    inbox: '引き渡し待ち', jobs: '進行中のサブタスク', watch: '監視の状態', lifecycle: 'フロー', dirs: '削除待ちディレクトリ',
    yes: 'はい', no: 'いいえ', none: '—', empty: '個人アシスタントを開設した人はまだいません。',
    online: 'オンライン', offline: 'オフライン',
    deprovision: '停止',
    deprovisionPrompt: '停止すると、このメンバーの暗号化された個人データは永久に復元できず、取り消しもできません。確認のため所有者のログイン名「{name}」を入力してください',
    deprovisionMismatch: 'ログイン名が一致しません', deprovisionStarted: '停止を開始しました',
  },
  platform: {
    title: '個人アシスタント', hint: '誰が開設できるか、どの AI を使うか、ノードごとの同時実行数、1 人あたり 1 日のターン上限を設定します。',
    eligibility: '開設できる範囲', modeAll: '全員', modeListed: '指定したユーザーと部署',
    users: '指定したユーザー', departments: '指定した部署（配下の部署を含む）',
    listedEmpty: '「指定したユーザーと部署」を選択していますがリストが空です。保存すると誰も開設できなくなります。',
    backends: '利用できる AI', backendFirst: '優先', backendAlsoOther: '優先する AI に利用可能なノードがないとき、もう一方も許可する',
    backendNames: { claude: 'Claude', codex: 'Codex' },
    concurrency: 'ノードごとの同時実行上限', concurrencyHint: '各ランタイムノードが同時に実行する個人アシスタントのターン数。超えた分は待機します。',
    budget: '1 日のターン上限の既定値', budgetHint: '各アシスタントが 1 日に実行できる最大ターン数。メンバーは「マイアシスタント」で自分の上限を下げられます。',
    save: '保存', saved: '保存しました', nothingChanged: '変更はありません',
  },
}
```

`web/src/i18n/index.ts`：

导入区加一行：

```ts
import { assistantZh, assistantEn, assistantJa } from './assistant'
```

`createI18n({...})` 的 `messages` 里，三种语言各加一个键：`zh` 的对象里在 `skillEditor: skillEditorZh` 之后加 `, assistant: assistantZh`；`ja` 的对象里在 `skillEditor: skillEditorJa` 之后加 `, assistant: assistantJa`；`en` 的对象里在 `skillEditor: skillEditorEn` 之后加 `, assistant: assistantEn`。

三种语言的 `menu` 一节各加两个键（放在 `myFeishu` 之前）：

- `web/src/i18n/zh.ts`：`myAssistant: "我的助理",` 与 `assistants: "个人助理",`
- `web/src/i18n/en.ts`：`myAssistant: "My assistant", assistants: "Personal assistants",`
- `web/src/i18n/ja.ts`：`myAssistant: "マイアシスタント",` 与 `assistants: "個人アシスタント",`

- [ ] **Step 5: 运行确认文案用例通过**

Run（在 `web/` 下）: `npx vitest run tests/assistantI18n.spec.ts tests/i18n.spec.ts`
Expected: PASS（`i18n.spec.ts` 的「英文覆盖全部中文键」「英文逐条渲染无告警」两条同时守住新文案）。

- [ ] **Step 6: 写失败测试（三个只读分区）**

`web/tests/AssistantSections.spec.ts`（新建）：

```ts
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Component } from 'vue'

vi.mock('@/api/myAssistant', async (original) => ({
  ...(await original<typeof import('@/api/myAssistant')>()),
  myAssistant: { attention: vi.fn(), jobs: vi.fn(), cancelJob: vi.fn(), recentApprovals: vi.fn() },
}))

import { myAssistant, type AssistantJob, type Attention, type SettledApproval } from '@/api/myAssistant'
import AssistantAttention from '@/components/assistant/AssistantAttention.vue'
import AssistantJobs from '@/components/assistant/AssistantJobs.vue'
import AssistantRecent from '@/components/assistant/AssistantRecent.vue'
import { i18n } from '@/i18n'

const t = i18n.global.t
const job = (over: Partial<AssistantJob>): AssistantJob => ({
  id: 'j1', kind: 'subtask', status: 'queued', title: '退货原因', rounds: 1, created_at: '2026-11-13T01:00:00Z',
  finished_at: null, has_result: false, brief: '查一下上季度的退货原因', result: null, blocked_reason: null, ...over,
})
const attention: Attention = {
  today: '2026-11-13',
  approvals: [
    { id: 'a1', tool: 'feishu_im_send_message', summary: '私聊 → 李四', created_at: '2026-11-13T01:00:00Z', link: 'https://tower.example.com' },
    { id: 'a2', tool: 'feishu_doc_update', summary: null, created_at: '2026-11-13T02:00:00Z', link: null },
  ],
  jobs: [job({ id: 'j9', status: 'blocked', title: '合同条款', blocked_reason: '需要你确认范围' })],
  followups: [{ id: 'F003', category: 'remind', status: 'open', title: '续约', review_on: null, due_on: '2026-11-13', origin_message_id: null, created_at: '2026-11-01T00:00:00Z' }],
}
const settled = (over: Partial<SettledApproval>): SettledApproval => ({
  id: 's1', tool: 'feishu_im_send_message', channel: 'p2p', status: 'executed', summary: '私聊 → 李四', reason: null, error: null,
  created_at: '2026-11-12T01:00:00Z', decided_at: '2026-11-12T02:00:00Z', executed_at: '2026-11-12T02:00:05Z', locked: false, ...over,
})
const render = (component: Component) => mount(component, { global: { plugins: [ElementPlus, i18n] } })

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(myAssistant.attention).mockReset().mockResolvedValue(attention)
  vi.mocked(myAssistant.jobs).mockReset().mockResolvedValue({ active: [job({})], recent: [job({ id: 'j2', status: 'done', title: '已完成的调查', result: '结论在此', finished_at: '2026-11-12T03:00:00Z', has_result: true })] })
  vi.mocked(myAssistant.cancelJob).mockReset().mockResolvedValue({ id: 'j1', status: 'cancelled' })
  vi.mocked(myAssistant.recentApprovals).mockReset().mockResolvedValue({ items: [settled({}), settled({ id: 's2', status: 'rejected', reason: '语气太硬' }), settled({ id: 's3', status: 'withdrawn', summary: null, locked: true })] })
})

describe('needs you', () => {
  it('shows pending approvals with a safe link, blocked sub-tasks and due follow-ups', async () => {
    const wrapper = render(AssistantAttention); await flushPromises()
    const first = wrapper.get('[data-test="approval-a1"]')
    expect(first.text()).toContain('私聊 → 李四')
    const link = first.get('a')
    expect(link.attributes('href')).toBe('https://tower.example.com')
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toBe('noopener noreferrer')
    const second = wrapper.get('[data-test="approval-a2"]')
    expect(second.find('a').exists()).toBe(false)
    expect(second.text()).toContain(t('assistant.attention.noLink'))
    expect(second.text()).toContain(t('assistant.page.lockedText'))
    expect(wrapper.text()).toContain('合同条款')
    expect(wrapper.text()).toContain('需要你确认范围')
    expect(wrapper.text()).toContain('F003')
    expect(wrapper.text()).toContain('续约')
    wrapper.unmount()
  })

  it('says so when nothing is waiting', async () => {
    vi.mocked(myAssistant.attention).mockResolvedValue({ today: '2026-11-13', approvals: [], jobs: [], followups: [] })
    const wrapper = render(AssistantAttention); await flushPromises()
    for (const key of ['approvalsEmpty', 'jobsEmpty', 'followupsEmpty']) expect(wrapper.text()).toContain(t(`assistant.attention.${key}`))
    wrapper.unmount()
  })

  it('shows the error and retries', async () => {
    vi.mocked(myAssistant.attention).mockRejectedValueOnce(new Error('加载不了'))
    const wrapper = render(AssistantAttention); await flushPromises()
    expect(wrapper.text()).toContain('加载不了')
    await (wrapper.vm as unknown as { load: () => Promise<void> }).load(); await flushPromises()
    expect(wrapper.text()).toContain('私聊 → 李四')
    wrapper.unmount()
  })
})

describe('running', () => {
  it('shows the brief of running jobs and the conclusion of finished ones', async () => {
    const wrapper = render(AssistantJobs); await flushPromises()
    expect(wrapper.get('[data-test="job-j1"]').text()).toContain('查一下上季度的退货原因')
    expect(wrapper.get('[data-test="job-j1"]').text()).toContain(t('assistant.jobs.statuses.queued'))
    expect(wrapper.get('[data-test="job-j2"]').text()).toContain('结论在此')
    expect(wrapper.find('[data-test="cancel-j2"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('cancels a running job after confirmation and reloads', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const wrapper = render(AssistantJobs); await flushPromises()
    await wrapper.get('[data-test="cancel-j1"]').trigger('click'); await flushPromises()
    expect(confirm.mock.calls[0][0]).toBe(t('assistant.jobs.cancelConfirm', { name: '退货原因' }))
    expect(myAssistant.cancelJob).toHaveBeenCalledWith('j1')
    expect(myAssistant.jobs).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('does nothing when the confirmation is dismissed', async () => {
    vi.spyOn(ElMessageBox, 'confirm').mockRejectedValue('cancel')
    const wrapper = render(AssistantJobs); await flushPromises()
    await wrapper.get('[data-test="cancel-j1"]').trigger('click'); await flushPromises()
    expect(myAssistant.cancelJob).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

describe('recently settled', () => {
  it('shows each outcome, the approver note and locked rows without content', async () => {
    const wrapper = render(AssistantRecent); await flushPromises()
    expect(wrapper.get('[data-test="settled-s1"]').text()).toContain(t('assistant.recent.statuses.executed'))
    expect(wrapper.get('[data-test="settled-s2"]').text()).toContain('语气太硬')
    expect(wrapper.get('[data-test="settled-s3"]').text()).toContain(t('assistant.page.lockedText'))
    wrapper.unmount()
  })

  it('says so when nothing has been settled', async () => {
    vi.mocked(myAssistant.recentApprovals).mockResolvedValue({ items: [] })
    const wrapper = render(AssistantRecent); await flushPromises()
    expect(wrapper.text()).toContain(t('assistant.recent.empty'))
    wrapper.unmount()
  })
})
```

- [ ] **Step 7: 运行确认失败**

Run（在 `web/` 下）: `npx vitest run tests/AssistantSections.spec.ts`
Expected: FAIL：找不到 `@/components/assistant/AssistantAttention.vue`。

- [ ] **Step 8: 实现三个分区**

`web/src/components/assistant/AssistantAttention.vue`（新建）：

```vue
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { myAssistant, type Attention } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const data = ref<Attention | null>(null)
const error = ref(''), loading = ref(false)
async function load() {
  loading.value = true; error.value = ''
  try { data.value = await myAssistant.attention() }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
onMounted(load)
defineExpose({ load })
</script>
<template>
  <section class="assistant-section">
    <LoadState
      :loading="loading && !data"
      :error="error"
      @retry="load"
    />
    <template v-if="data">
      <h3>{{ t('assistant.attention.approvals') }}</h3>
      <p
        v-if="!data.approvals.length"
        class="muted"
      >
        {{ t('assistant.attention.approvalsEmpty') }}
      </p>
      <ul v-else>
        <li
          v-for="item in data.approvals"
          :key="item.id"
          :data-test="'approval-' + item.id"
        >
          <strong>{{ item.summary ?? t('assistant.page.lockedText') }}</strong>
          <span class="muted">{{ item.tool }} · {{ t('assistant.attention.submittedAt', { time: formatDateTime(item.created_at) }) }}</span>
          <!-- 链接只可能是部署配置的审批系统入口（后端只放行 http/https）；新窗口打开，不带来源页。 -->
          <a
            v-if="item.link"
            :href="item.link"
            target="_blank"
            rel="noopener noreferrer"
          >{{ t('assistant.attention.open') }}</a>
          <span
            v-else
            class="muted"
          >{{ t('assistant.attention.noLink') }}</span>
        </li>
      </ul>
      <h3>{{ t('assistant.attention.jobs') }}</h3>
      <p
        v-if="!data.jobs.length"
        class="muted"
      >
        {{ t('assistant.attention.jobsEmpty') }}
      </p>
      <ul v-else>
        <li
          v-for="job in data.jobs"
          :key="job.id"
          :data-test="'blocked-' + job.id"
        >
          <strong>{{ job.locked ? t('assistant.page.lockedText') : job.title }}</strong>
          <span v-if="job.blocked_reason">{{ t('assistant.attention.blockedReason') }}：{{ job.blocked_reason }}</span>
        </li>
      </ul>
      <h3>{{ t('assistant.attention.followups') }}</h3>
      <p
        v-if="!data.followups.length"
        class="muted"
      >
        {{ t('assistant.attention.followupsEmpty') }}
      </p>
      <ul v-else>
        <li
          v-for="item in data.followups"
          :key="item.id"
          :data-test="'due-' + item.id"
        >
          <code>{{ item.id }}</code>
          <strong>{{ item.locked ? t('assistant.page.lockedText') : item.title }}</strong>
          <el-tag size="small">
            {{ t('assistant.followups.categories.' + item.category) }}
          </el-tag>
        </li>
      </ul>
    </template>
  </section>
</template>
<style scoped>
.assistant-section ul { list-style: none; padding: 0; }
.assistant-section li { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; padding: 8px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.muted { color: var(--el-text-color-secondary); }
</style>
```

`web/src/components/assistant/AssistantJobs.vue`（新建）：

```vue
<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { myAssistant, type AssistantJob, type JobList } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const data = ref<JobList | null>(null)
const error = ref(''), loading = ref(false)
async function load() {
  loading.value = true; error.value = ''
  try { data.value = await myAssistant.jobs() }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
/** 取消确认框不算失败。 */
async function cancel(job: AssistantJob) {
  try {
    await ElMessageBox.confirm(t('assistant.jobs.cancelConfirm', { name: job.title || job.id }), t('assistant.jobs.cancel'), { type: 'warning' })
    await myAssistant.cancelJob(job.id)
    ElMessage.success(t('assistant.jobs.cancelled'))
    await load()
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorMessage(e)) }
}
onMounted(load)
defineExpose({ load })
</script>
<template>
  <section class="assistant-section">
    <LoadState
      :loading="loading && !data"
      :error="error"
      @retry="load"
    />
    <template v-if="data">
      <template
        v-for="group in (['active', 'recent'] as const)"
        :key="group"
      >
        <h3>{{ t('assistant.jobs.' + group) }}</h3>
        <p
          v-if="!data[group].length"
          class="muted"
        >
          {{ t('assistant.jobs.' + group + 'Empty') }}
        </p>
        <ul v-else>
          <li
            v-for="job in data[group]"
            :key="job.id"
            :data-test="'job-' + job.id"
          >
            <div class="job-head">
              <strong>{{ job.locked ? t('assistant.page.lockedText') : job.title }}</strong>
              <el-tag size="small">
                {{ t('assistant.jobs.statuses.' + job.status) }}
              </el-tag>
              <span class="muted">{{ t('assistant.jobs.rounds', { n: job.rounds }) }} · {{ t('assistant.jobs.startedAt', { time: formatDateTime(job.created_at) }) }}</span>
              <span
                v-if="job.finished_at"
                class="muted"
              >{{ t('assistant.jobs.finishedAt', { time: formatDateTime(job.finished_at) }) }}</span>
              <span
                v-if="job.error_code"
                class="muted"
              >{{ t('assistant.jobs.errorCode', { detail: job.error_code }) }}</span>
              <el-button
                v-if="group === 'active'"
                link
                type="danger"
                :data-test="'cancel-' + job.id"
                @click="cancel(job)"
              >
                {{ t('assistant.jobs.cancel') }}
              </el-button>
            </div>
            <details v-if="job.brief">
              <summary>{{ t('assistant.jobs.brief') }}</summary>
              <pre>{{ job.brief }}</pre>
            </details>
            <details
              v-if="job.blocked_reason"
              open
            >
              <summary>{{ t('assistant.attention.blockedReason') }}</summary>
              <pre>{{ job.blocked_reason }}</pre>
            </details>
            <details
              v-if="job.result"
              open
            >
              <summary>{{ t('assistant.jobs.result') }}</summary>
              <pre>{{ job.result }}</pre>
            </details>
          </li>
        </ul>
      </template>
    </template>
  </section>
</template>
<style scoped>
.assistant-section ul { list-style: none; padding: 0; }
.assistant-section li { padding: 8px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.job-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; }
.assistant-section pre { white-space: pre-wrap; word-break: break-word; margin: 4px 0; }
.muted { color: var(--el-text-color-secondary); }
</style>
```

`web/src/components/assistant/AssistantRecent.vue`（新建）：

```vue
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { myAssistant, type SettledApproval } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const items = ref<SettledApproval[] | null>(null)
const error = ref(''), loading = ref(false)
const tags: Record<SettledApproval['status'], 'success' | 'danger' | 'info'> = { executed: 'success', rejected: 'danger', withdrawn: 'info' }
async function load() {
  loading.value = true; error.value = ''
  try { items.value = (await myAssistant.recentApprovals()).items }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
onMounted(load)
defineExpose({ load })
</script>
<template>
  <section class="assistant-section">
    <LoadState
      :loading="loading && !items"
      :error="error"
      @retry="load"
    />
    <p
      v-if="items && !items.length"
      class="muted"
    >
      {{ t('assistant.recent.empty') }}
    </p>
    <ul v-if="items && items.length">
      <li
        v-for="item in items"
        :key="item.id"
        :data-test="'settled-' + item.id"
      >
        <el-tag
          size="small"
          :type="tags[item.status]"
        >
          {{ t('assistant.recent.statuses.' + item.status) }}
        </el-tag>
        <strong>{{ item.summary ?? t('assistant.page.lockedText') }}</strong>
        <span class="muted">{{ item.tool }}</span>
        <span
          v-if="item.executed_at"
          class="muted"
        >{{ t('assistant.recent.executedAt', { time: formatDateTime(item.executed_at) }) }}</span>
        <span
          v-else-if="item.decided_at"
          class="muted"
        >{{ t('assistant.recent.decidedAt', { time: formatDateTime(item.decided_at) }) }}</span>
        <span v-if="item.reason">{{ t('assistant.recent.reason') }}：{{ item.reason }}</span>
      </li>
    </ul>
  </section>
</template>
<style scoped>
.assistant-section ul { list-style: none; padding: 0; }
.assistant-section li { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; padding: 8px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.muted { color: var(--el-text-color-secondary); }
</style>
```

- [ ] **Step 9: 运行确认通过**

Run（在 `web/` 下）: `npx vitest run tests/AssistantSections.spec.ts tests/assistantI18n.spec.ts tests/i18n.spec.ts`
Expected: PASS。

Run（在 `web/` 下）: `npm run lint && npm run build`
Expected: 没有 error（模板里属性与内容的换行若有 warning，以 `npx eslint --fix src/components/assistant` 的结果为准）；`vue-tsc` 与构建通过。

- [ ] **Step 10: 提交**

```bash
git add web/src/api/myAssistant.ts web/src/i18n/assistant.ts web/src/i18n/index.ts \
  web/src/i18n/zh.ts web/src/i18n/en.ts web/src/i18n/ja.ts web/src/components/assistant \
  web/tests/assistantI18n.spec.ts web/tests/AssistantSections.spec.ts
git commit -m "我的助理页的接口封装与三种语言的文案；等你出手、正在跑、最近落地三个分区"
```

---

### Task 9: 待跟进分区

**Files:**
- Create: `web/src/components/assistant/AssistantFollowups.vue`
- Test: `web/tests/AssistantFollowups.spec.ts`（新建）

**Interfaces:**
- Consumes: Task 8 的 `myAssistant.followups / addFollowup / updateFollowup / closeFollowup / snoozeFollowup`、`FOLLOWUP_CATEGORIES`、类型 `Followup` / `FollowupCategory` / `FollowupList`；文案 `assistant.followups.*`、`assistant.page.lockedText`。
- Produces：组件 `AssistantFollowups`（无属性）；`defineExpose({ form, add, recategorize, load })`，其中 `form: { category: FollowupCategory; title: string; due_on: string }`、`recategorize(item: Followup, category: FollowupCategory): Promise<void>`。测试钩子：`data-test="item-<编号>"`、`done-<编号>`（勾选框）、`snooze-<编号>`、`drop-<编号>`、`add`。

- [ ] **Step 1: 写失败测试**

`web/tests/AssistantFollowups.spec.ts`（新建）：

```ts
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/myAssistant', async (original) => ({
  ...(await original<typeof import('@/api/myAssistant')>()),
  myAssistant: { followups: vi.fn(), addFollowup: vi.fn(), updateFollowup: vi.fn(), closeFollowup: vi.fn(), snoozeFollowup: vi.fn() },
}))

import { myAssistant, type Followup, type FollowupCategory, type FollowupList } from '@/api/myAssistant'
import AssistantFollowups from '@/components/assistant/AssistantFollowups.vue'
import { i18n } from '@/i18n'

const t = i18n.global.t
const item = (over: Partial<Followup>): Followup => ({
  id: 'F001', category: 'promised', status: 'open', title: '给李四报价', notes: '他周一问的', review_on: null, due_on: null,
  origin_message_id: null, created_at: '2026-11-10T00:00:00Z', due_today: false, ...over,
})
const list: FollowupList = {
  today: '2026-11-13',
  total: 3,
  groups: {
    decide: [],
    promised: [item({})],
    waiting: [item({ id: 'F002', category: 'waiting', title: '', notes: null, locked: true })],
    remind: [item({ id: 'F003', category: 'remind', title: '续约', notes: null, due_on: '2026-11-13', due_today: true })],
  },
}
interface Exposed {
  form: { category: FollowupCategory; title: string; due_on: string }
  add: () => Promise<void>
  recategorize: (row: Followup, category: FollowupCategory) => Promise<void>
}
const render = () => mount(AssistantFollowups, { global: { plugins: [ElementPlus, i18n] } })
const exposed = (wrapper: ReturnType<typeof render>) => wrapper.vm as unknown as Exposed

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(myAssistant.followups).mockReset().mockResolvedValue(list)
  vi.mocked(myAssistant.addFollowup).mockReset().mockResolvedValue(item({}))
  vi.mocked(myAssistant.updateFollowup).mockReset().mockResolvedValue(item({}))
  vi.mocked(myAssistant.closeFollowup).mockReset().mockResolvedValue(item({}))
  vi.mocked(myAssistant.snoozeFollowup).mockReset().mockResolvedValue(item({}))
  vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
})

describe('follow-ups', () => {
  it('shows the four categories, due marks and locked items', async () => {
    const wrapper = render(); await flushPromises()
    for (const category of ['decide', 'promised', 'waiting', 'remind']) expect(wrapper.text()).toContain(t(`assistant.followups.categories.${category}`))
    expect(wrapper.text()).toContain(t('assistant.followups.empty'))
    expect(wrapper.get('[data-test="item-F001"]').text()).toContain('给李四报价')
    expect(wrapper.get('[data-test="item-F001"]').text()).toContain('他周一问的')
    expect(wrapper.get('[data-test="item-F002"]').text()).toContain(t('assistant.page.lockedText'))
    expect(wrapper.get('[data-test="item-F003"]').text()).toContain(t('assistant.followups.dueToday'))
    wrapper.unmount()
  })

  it('closes an item as done when its checkbox is ticked, then reloads', async () => {
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="done-F001"] input').setValue(true); await flushPromises()
    expect(myAssistant.closeFollowup).toHaveBeenCalledWith('F001', false)
    expect(myAssistant.followups).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('drops an item only after confirmation', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel').mockResolvedValueOnce('confirm' as never)
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="drop-F003"]').trigger('click'); await flushPromises()
    expect(myAssistant.closeFollowup).not.toHaveBeenCalled()
    await wrapper.get('[data-test="drop-F003"]').trigger('click'); await flushPromises()
    expect(confirm.mock.calls[1][0]).toBe(t('assistant.followups.dropConfirm', { name: 'F003' }))
    expect(myAssistant.closeFollowup).toHaveBeenCalledWith('F003', true)
    wrapper.unmount()
  })

  it('snoozes to a date after today and rejects earlier dates in the prompt', async () => {
    const prompt = vi.spyOn(ElMessageBox, 'prompt').mockResolvedValue({ value: '2026-11-20', action: 'confirm' } as never)
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="snooze-F001"]').trigger('click'); await flushPromises()
    expect(myAssistant.snoozeFollowup).toHaveBeenCalledWith('F001', '2026-11-20')
    const options = prompt.mock.calls[0][2] as { inputType: string; inputValidator: (value: string) => boolean | string }
    expect(options.inputType).toBe('date')
    expect(options.inputValidator('2026-11-14')).toBe(true)
    expect(options.inputValidator('2026-11-13')).toBe(t('assistant.followups.snoozeInvalid'))
    expect(options.inputValidator('')).toBe(t('assistant.followups.snoozeInvalid'))
    wrapper.unmount()
  })

  it('changes the category through the update endpoint and skips a no-op', async () => {
    const wrapper = render(); await flushPromises()
    await exposed(wrapper).recategorize(list.groups.promised[0], 'promised')
    expect(myAssistant.updateFollowup).not.toHaveBeenCalled()
    await exposed(wrapper).recategorize(list.groups.promised[0], 'waiting'); await flushPromises()
    expect(myAssistant.updateFollowup).toHaveBeenCalledWith('F001', { category: 'waiting' })
    wrapper.unmount()
  })

  it('adds an item with a trimmed title and refuses an empty one', async () => {
    const warning = vi.spyOn(ElMessage, 'warning').mockReturnValue({ close: () => {} })
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="add"]').trigger('click'); await flushPromises()
    expect(myAssistant.addFollowup).not.toHaveBeenCalled()
    expect(warning).toHaveBeenCalledWith(t('assistant.followups.titleRequired'))
    Object.assign(exposed(wrapper).form, { category: 'decide', title: '  是否续约 ', due_on: '2026-12-01' })
    await wrapper.get('[data-test="add"]').trigger('click'); await flushPromises()
    expect(myAssistant.addFollowup).toHaveBeenCalledWith({ category: 'decide', title: '是否续约', due_on: '2026-12-01' })
    expect(exposed(wrapper).form.title).toBe('')
    wrapper.unmount()
  })

  it('shows the backend message when a change is refused', async () => {
    const error = vi.spyOn(ElMessage, 'error').mockReturnValue({ close: () => {} })
    vi.mocked(myAssistant.closeFollowup).mockRejectedValueOnce(new Error('这条待跟进已经关闭'))
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="done-F001"] input').setValue(true); await flushPromises()
    expect(error).toHaveBeenCalledWith('这条待跟进已经关闭')
    wrapper.unmount()
  })
})
```

- [ ] **Step 2: 运行确认失败**

Run（在 `web/` 下）: `npx vitest run tests/AssistantFollowups.spec.ts`
Expected: FAIL：找不到 `@/components/assistant/AssistantFollowups.vue`。

- [ ] **Step 3: 实现**

`web/src/components/assistant/AssistantFollowups.vue`（新建）：

```vue
<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { FOLLOWUP_CATEGORIES, myAssistant, type Followup, type FollowupCategory, type FollowupList } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'

const { t } = useI18n()
const data = ref<FollowupList | null>(null)
const error = ref(''), loading = ref(false), busy = ref(false)
const form = reactive<{ category: FollowupCategory; title: string; due_on: string }>({ category: 'remind', title: '', due_on: '' })

async function load() {
  loading.value = true; error.value = ''
  try { data.value = await myAssistant.followups() }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
/** 做一件事、提示、重新取列表；取消确认框不算失败。 */
async function act(action: () => Promise<unknown>, done: string) {
  busy.value = true
  try { await action(); ElMessage.success(t(done)); await load() }
  catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorMessage(e)) }
  finally { busy.value = false }
}
async function add() {
  const title = form.title.trim()
  if (!title) { ElMessage.warning(t('assistant.followups.titleRequired')); return }
  await act(async () => {
    await myAssistant.addFollowup({ category: form.category, title, ...(form.due_on ? { due_on: form.due_on } : {}) })
    form.title = ''; form.due_on = ''
  }, 'assistant.followups.added')
}
const finish = (item: Followup) => act(() => myAssistant.closeFollowup(item.id, false), 'assistant.followups.closed')
const drop = (item: Followup) => act(async () => {
  await ElMessageBox.confirm(t('assistant.followups.dropConfirm', { name: item.id }), t('assistant.followups.drop'), { type: 'warning' })
  await myAssistant.closeFollowup(item.id, true)
}, 'assistant.followups.closed')
/** 日期串是 YYYY-MM-DD，按字典序比就是按日期比；「今天」以后端给的所有者时区的今天为准。 */
const laterThanToday = (value: string) => /^\d{4}-\d{2}-\d{2}$/.test(value ?? '') && value > (data.value?.today ?? '')
const snooze = (item: Followup) => act(async () => {
  const { value } = await ElMessageBox.prompt(t('assistant.followups.snoozePrompt'), t('assistant.followups.snooze'), {
    inputType: 'date',
    inputValidator: (input: string) => laterThanToday(input) || t('assistant.followups.snoozeInvalid'),
  })
  await myAssistant.snoozeFollowup(item.id, value)
}, 'assistant.followups.snoozed')
async function recategorize(item: Followup, category: FollowupCategory) {
  if (category === item.category) return
  await act(() => myAssistant.updateFollowup(item.id, { category }), 'assistant.followups.updated')
}
onMounted(load)
defineExpose({ form, add, recategorize, load })
</script>
<template>
  <section class="assistant-section">
    <LoadState
      :loading="loading && !data"
      :error="error"
      @retry="load"
    />
    <template v-if="data">
      <div class="followup-add">
        <el-select
          v-model="form.category"
          :aria-label="t('assistant.followups.category')"
          style="width: 140px"
        >
          <el-option
            v-for="category in FOLLOWUP_CATEGORIES"
            :key="category"
            :value="category"
            :label="t('assistant.followups.categories.' + category)"
          />
        </el-select>
        <el-input
          v-model="form.title"
          :placeholder="t('assistant.followups.title')"
          maxlength="200"
          style="width: 320px"
        />
        <el-date-picker
          v-model="form.due_on"
          type="date"
          value-format="YYYY-MM-DD"
          :placeholder="t('assistant.followups.dueOn')"
          style="width: 160px"
        />
        <el-button
          type="primary"
          :loading="busy"
          data-test="add"
          @click="add"
        >
          {{ t('assistant.followups.add') }}
        </el-button>
      </div>
      <template
        v-for="category in FOLLOWUP_CATEGORIES"
        :key="category"
      >
        <h3>{{ t('assistant.followups.categories.' + category) }}（{{ data.groups[category].length }}）</h3>
        <p
          v-if="!data.groups[category].length"
          class="muted"
        >
          {{ t('assistant.followups.empty') }}
        </p>
        <ul v-else>
          <li
            v-for="item in data.groups[category]"
            :key="item.id"
            :data-test="'item-' + item.id"
          >
            <!-- 勾上即办完：受控为未勾选，办完后这一条随列表刷新消失。 -->
            <el-checkbox
              :model-value="false"
              :disabled="busy"
              :aria-label="t('assistant.followups.done')"
              :data-test="'done-' + item.id"
              @change="finish(item)"
            />
            <code>{{ item.id }}</code>
            <strong>{{ item.locked ? t('assistant.page.lockedText') : item.title }}</strong>
            <el-tag
              v-if="item.due_today"
              size="small"
              type="danger"
            >
              {{ t('assistant.followups.dueToday') }}
            </el-tag>
            <el-tag
              v-if="item.status === 'snoozed' && item.review_on"
              size="small"
              type="info"
            >
              {{ t('assistant.followups.snoozedUntil', { time: item.review_on }) }}
            </el-tag>
            <span
              v-if="item.due_on"
              class="muted"
            >{{ t('assistant.followups.due', { time: item.due_on }) }}</span>
            <span
              v-if="item.status !== 'snoozed' && item.review_on"
              class="muted"
            >{{ t('assistant.followups.review', { time: item.review_on }) }}</span>
            <span
              v-if="item.notes"
              class="followup-notes"
            >{{ item.notes }}</span>
            <span class="followup-actions">
              <el-select
                :model-value="item.category"
                size="small"
                :aria-label="t('assistant.followups.category')"
                style="width: 120px"
                @change="recategorize(item, $event as FollowupCategory)"
              >
                <el-option
                  v-for="option in FOLLOWUP_CATEGORIES"
                  :key="option"
                  :value="option"
                  :label="t('assistant.followups.categories.' + option)"
                />
              </el-select>
              <el-button
                link
                type="primary"
                :data-test="'snooze-' + item.id"
                @click="snooze(item)"
              >
                {{ t('assistant.followups.snooze') }}
              </el-button>
              <el-button
                link
                type="danger"
                :data-test="'drop-' + item.id"
                @click="drop(item)"
              >
                {{ t('assistant.followups.drop') }}
              </el-button>
            </span>
          </li>
        </ul>
      </template>
    </template>
  </section>
</template>
<style scoped>
.followup-add { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
.assistant-section ul { list-style: none; padding: 0; }
.assistant-section li { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.followup-notes { flex-basis: 100%; white-space: pre-wrap; color: var(--el-text-color-regular); }
.followup-actions { margin-left: auto; display: flex; gap: 4px; align-items: center; }
.muted { color: var(--el-text-color-secondary); }
</style>
```

- [ ] **Step 4: 运行确认通过**

Run（在 `web/` 下）: `npx vitest run tests/AssistantFollowups.spec.ts`
Expected: PASS。

Run（在 `web/` 下）: `npm run lint && npm run build`
Expected: 没有 error；`vue-tsc` 与构建通过。

- [ ] **Step 5: 提交**

```bash
git add web/src/components/assistant/AssistantFollowups.vue web/tests/AssistantFollowups.spec.ts
git commit -m "我的助理页：待跟进分区，可以勾选办完、推后、改分类、标为不再需要与新增"
```

---

### Task 10: 知识库分区

**Files:**
- Create: `web/src/components/assistant/AssistantNotes.vue`
- Test: `web/tests/AssistantNotes.spec.ts`（新建）

**Interfaces:**
- Consumes: Task 8 的 `myAssistant.notes / page / savePage / deletePage`、类型 `NoteEntry` / `NoteIndex` / `NotePage`；`@/utils/errors` 的 `errorMessage`、`isVersionConflict`；`@/utils/format` 的 `formatBytes`、`formatDateTime`；`@/api/client` 的 `ApiError`（只在用例里）；文案 `assistant.notes.*`、`assistant.page.locked`。
- Produces：组件 `AssistantNotes`（无属性）；`defineExpose({ editor, save, load })`，其中 `editor: { open: boolean; isNew: boolean; path: string; title: string; summary: string; body: string; pinned: boolean; version: number }`。测试钩子：`data-test="page-<序号>"`、`pinned-<序号>`、`open-<序号>`、`edit-<序号>`、`remove-<序号>`、`create`、`viewer`、`editor`、`save-page`、`notes-locked`。

- [ ] **Step 1: 写失败测试**

`web/tests/AssistantNotes.spec.ts`（新建）：

```ts
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/myAssistant', async (original) => ({
  ...(await original<typeof import('@/api/myAssistant')>()),
  myAssistant: { notes: vi.fn(), page: vi.fn(), savePage: vi.fn(), deletePage: vi.fn() },
}))

import { ApiError } from '@/api/client'
import { myAssistant, type NoteEntry, type NotePage } from '@/api/myAssistant'
import AssistantNotes from '@/components/assistant/AssistantNotes.vue'
import { i18n } from '@/i18n'

const t = i18n.global.t
const entry = (over: Partial<NoteEntry>): NoteEntry => ({
  path: '规则/口吻', title: '口吻', summary: '口吻规则', pinned: true, version: 3, size: 120,
  updated_by: 'assistant', updated_at: '2026-11-12T08:00:00Z', ...over,
})
const page = (over: Partial<NotePage>): NotePage => ({ ...entry({}), body: '回复要短。', ...over })
interface Exposed {
  editor: { open: boolean; isNew: boolean; path: string; title: string; summary: string; body: string; pinned: boolean; version: number }
}
const render = () => mount(AssistantNotes, { global: { plugins: [ElementPlus, i18n] } })
const editor = (wrapper: ReturnType<typeof render>) => (wrapper.vm as unknown as Exposed).editor

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(myAssistant.notes).mockReset().mockResolvedValue({
    items: [entry({ path: '人/李四', title: '李四', summary: '采购负责人', pinned: false, updated_by: 'owner' }), entry({})],
    locked: false,
  })
  vi.mocked(myAssistant.page).mockReset().mockResolvedValue(page({}))
  vi.mocked(myAssistant.savePage).mockReset().mockResolvedValue(entry({ version: 4, updated_by: 'owner' }))
  vi.mocked(myAssistant.deletePage).mockReset().mockResolvedValue({ deleted: true })
  vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
})

describe('knowledge base', () => {
  it('lists pages, marks pinned ones clearly and says who changed each last', async () => {
    const wrapper = render(); await flushPromises()
    expect(wrapper.text()).toContain(t('assistant.notes.intro'))
    expect(wrapper.find('[data-test="pinned-0"]').exists()).toBe(false)
    const pinned = wrapper.get('[data-test="pinned-1"]')
    expect(pinned.text()).toBe(t('assistant.notes.pinned'))
    expect(pinned.attributes('title')).toBe(t('assistant.notes.pinnedHint'))
    expect(wrapper.get('[data-test="page-0"]').text()).toContain(t('assistant.notes.updatedBy', { name: t('assistant.notes.authors.owner') }))
    expect(wrapper.get('[data-test="page-1"]').text()).toContain(t('assistant.notes.updatedBy', { name: t('assistant.notes.authors.assistant') }))
    expect(wrapper.find('[data-test="notes-locked"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('says the content can no longer be read when the index is locked', async () => {
    vi.mocked(myAssistant.notes).mockResolvedValue({ items: [], locked: true })
    const wrapper = render(); await flushPromises()
    expect(wrapper.get('[data-test="notes-locked"]').text()).toContain(t('assistant.page.locked'))
    expect(wrapper.text()).not.toContain(t('assistant.notes.empty'))
    wrapper.unmount()
  })

  it('opens a page to read it', async () => {
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="open-1"]').trigger('click'); await flushPromises()
    expect(myAssistant.page).toHaveBeenCalledWith('规则/口吻')
    expect(wrapper.get('[data-test="viewer"]').text()).toContain('回复要短。')
    expect(wrapper.find('[data-test="editor"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('edits a pinned page and saves it with the version it read', async () => {
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="edit-1"]').trigger('click'); await flushPromises()
    expect(editor(wrapper)).toMatchObject({ open: true, isNew: false, path: '规则/口吻', pinned: true, version: 3, body: '回复要短。' })
    editor(wrapper).body = '回复要短，先说结论。'
    await wrapper.get('[data-test="save-page"]').trigger('click'); await flushPromises()
    expect(myAssistant.savePage).toHaveBeenCalledWith({
      path: '规则/口吻', body: '回复要短，先说结论。', summary: '口吻规则', title: '口吻', pinned: true, version: 3,
    })
    expect(editor(wrapper).open).toBe(false)
    expect(myAssistant.notes).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('creates a page with version 0', async () => {
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="create"]').trigger('click'); await flushPromises()
    expect(editor(wrapper)).toMatchObject({ open: true, isNew: true, version: 0, pinned: false })
    Object.assign(editor(wrapper), { path: ' 项目/续约 ', body: '明年一月到期。', summary: '续约进展', pinned: true })
    await wrapper.get('[data-test="save-page"]').trigger('click'); await flushPromises()
    expect(myAssistant.savePage).toHaveBeenCalledWith({
      path: '项目/续约', body: '明年一月到期。', summary: '续约进展', title: null, pinned: true, version: 0,
    })
    wrapper.unmount()
  })

  it('refuses to save without a path or a body', async () => {
    const warning = vi.spyOn(ElMessage, 'warning').mockReturnValue({ close: () => {} })
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="create"]').trigger('click'); await flushPromises()
    Object.assign(editor(wrapper), { path: '项目/续约', body: '   ' })
    await wrapper.get('[data-test="save-page"]').trigger('click'); await flushPromises()
    expect(myAssistant.savePage).not.toHaveBeenCalled()
    expect(warning).toHaveBeenCalledWith(t('assistant.notes.required'))
    wrapper.unmount()
  })

  it('reloads the latest content on a version conflict instead of overwriting', async () => {
    const warning = vi.spyOn(ElMessage, 'warning').mockReturnValue({ close: () => {} })
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="edit-1"]').trigger('click'); await flushPromises()
    editor(wrapper).body = '我改的'
    vi.mocked(myAssistant.savePage).mockRejectedValueOnce(new ApiError(409, 40901, '这一页已被改动，请重新打开后再保存'))
    vi.mocked(myAssistant.page).mockResolvedValueOnce(page({ version: 5, body: '助理刚写的' }))
    await wrapper.get('[data-test="save-page"]').trigger('click'); await flushPromises()
    expect(warning).toHaveBeenCalledWith(t('assistant.notes.conflict'))
    expect(editor(wrapper)).toMatchObject({ open: true, version: 5, body: '助理刚写的' })
    expect(myAssistant.savePage).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('deletes a page only after confirmation', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel').mockResolvedValueOnce('confirm' as never)
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="remove-1"]').trigger('click'); await flushPromises()
    expect(myAssistant.deletePage).not.toHaveBeenCalled()
    await wrapper.get('[data-test="remove-1"]').trigger('click'); await flushPromises()
    expect(confirm.mock.calls[1][0]).toBe(t('assistant.notes.removeConfirm', { name: '规则/口吻' }))
    expect(myAssistant.deletePage).toHaveBeenCalledWith('规则/口吻')
    wrapper.unmount()
  })
})
```

- [ ] **Step 2: 运行确认失败**

Run（在 `web/` 下）: `npx vitest run tests/AssistantNotes.spec.ts`
Expected: FAIL：找不到 `@/components/assistant/AssistantNotes.vue`。

- [ ] **Step 3: 实现**

`web/src/components/assistant/AssistantNotes.vue`（新建）：

```vue
<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { myAssistant, type NoteEntry, type NoteIndex, type NotePage } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import { errorMessage, isVersionConflict } from '@/utils/errors'
import { formatBytes, formatDateTime } from '@/utils/format'

const { t } = useI18n()
const index = ref<NoteIndex | null>(null)
const current = ref<NotePage | null>(null)
const error = ref(''), loading = ref(false), busy = ref(false)
const editor = reactive({ open: false, isNew: false, path: '', title: '', summary: '', body: '', pinned: false, version: 0 })

async function load() {
  loading.value = true; error.value = ''
  try { index.value = await myAssistant.notes() }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
function fill(page: NotePage) {
  Object.assign(editor, { open: true, isNew: false, path: page.path, title: page.title, summary: page.summary, body: page.body, pinned: page.pinned, version: page.version })
}
async function open(entry: NoteEntry) {
  try { current.value = await myAssistant.page(entry.path); editor.open = false }
  catch (e) { ElMessage.error(errorMessage(e)) }
}
async function edit(entry: NoteEntry) {
  try { const page = await myAssistant.page(entry.path); current.value = page; fill(page) }
  catch (e) { ElMessage.error(errorMessage(e)) }
}
function create() {
  current.value = null
  Object.assign(editor, { open: true, isNew: true, path: '', title: '', summary: '', body: '', pinned: false, version: 0 })
}
async function save() {
  const path = editor.path.trim()
  if (!path || !editor.body.trim()) { ElMessage.warning(t('assistant.notes.required')); return }
  busy.value = true
  try {
    await myAssistant.savePage({ path, body: editor.body, summary: editor.summary, title: editor.title.trim() || null, pinned: editor.pinned, version: editor.version })
    ElMessage.success(t('assistant.notes.saved'))
    editor.open = false; current.value = null
    await load()
  } catch (e) {
    if (isVersionConflict(e) && !editor.isNew) {
      // 助理在这期间写过这一页：换成最新内容，让所有者看过再存，不拿旧版本盖掉。
      ElMessage.warning(t('assistant.notes.conflict'))
      try { fill(await myAssistant.page(path)) } catch (inner) { ElMessage.error(errorMessage(inner)) }
    } else ElMessage.error(errorMessage(e))
  } finally { busy.value = false }
}
/** 取消确认框不算失败。 */
async function remove(entry: NoteEntry) {
  try {
    await ElMessageBox.confirm(t('assistant.notes.removeConfirm', { name: entry.path }), t('assistant.notes.remove'), { type: 'warning' })
    await myAssistant.deletePage(entry.path)
    ElMessage.success(t('assistant.notes.deleted'))
    if (current.value?.path === entry.path) current.value = null
    if (editor.path === entry.path) editor.open = false
    await load()
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorMessage(e)) }
}
onMounted(load)
defineExpose({ editor, save, load })
</script>
<template>
  <section class="assistant-section">
    <p class="muted">
      {{ t('assistant.notes.intro') }}
    </p>
    <LoadState
      :loading="loading && !index"
      :error="error"
      @retry="load"
    />
    <template v-if="index">
      <el-alert
        v-if="index.locked"
        type="warning"
        :closable="false"
        show-icon
        data-test="notes-locked"
        :title="t('assistant.page.locked')"
      />
      <el-button
        v-else
        type="primary"
        data-test="create"
        @click="create"
      >
        {{ t('assistant.notes.create') }}
      </el-button>
      <p
        v-if="!index.locked && !index.items.length"
        class="muted"
      >
        {{ t('assistant.notes.empty') }}
      </p>
      <ul>
        <li
          v-for="(entry, i) in index.items"
          :key="entry.path"
          :data-test="'page-' + i"
        >
          <!-- 置顶页每天第一轮原样交给助理：用醒目的实心标签，所有者一眼看得到置顶了什么。 -->
          <el-tag
            v-if="entry.pinned"
            type="warning"
            effect="dark"
            :title="t('assistant.notes.pinnedHint')"
            :data-test="'pinned-' + i"
          >
            {{ t('assistant.notes.pinned') }}
          </el-tag>
          <code>{{ entry.path }}</code>
          <strong>{{ entry.title }}</strong>
          <span>{{ entry.summary }}</span>
          <span class="muted">{{ t('assistant.notes.updatedBy', { name: t('assistant.notes.authors.' + entry.updated_by) }) }} · {{ t('assistant.notes.updatedAt', { time: formatDateTime(entry.updated_at) }) }} · {{ formatBytes(entry.size) }}</span>
          <span class="note-actions">
            <el-button
              link
              type="primary"
              :data-test="'open-' + i"
              @click="open(entry)"
            >
              {{ t('assistant.notes.open') }}
            </el-button>
            <el-button
              link
              type="primary"
              :data-test="'edit-' + i"
              @click="edit(entry)"
            >
              {{ t('assistant.notes.edit') }}
            </el-button>
            <el-button
              link
              type="danger"
              :data-test="'remove-' + i"
              @click="remove(entry)"
            >
              {{ t('assistant.notes.remove') }}
            </el-button>
          </span>
        </li>
      </ul>
    </template>
    <article
      v-if="current && !editor.open"
      data-test="viewer"
      class="note-viewer"
    >
      <h3>
        {{ current.path }}
        <small class="muted">{{ t('assistant.notes.version', { n: current.version }) }}</small>
      </h3>
      <!-- 正文按纯文本显示：内容可能来自别人发来的消息，不当成 HTML 或 Markdown 渲染。 -->
      <pre>{{ current.body }}</pre>
    </article>
    <el-form
      v-if="editor.open"
      label-position="top"
      data-test="editor"
      class="note-editor"
      @submit.prevent
    >
      <el-form-item :label="t('assistant.notes.path')">
        <el-input
          v-model="editor.path"
          :disabled="!editor.isNew"
          :placeholder="t('assistant.notes.pathHint')"
          maxlength="200"
        />
      </el-form-item>
      <el-form-item :label="t('assistant.notes.title')">
        <el-input
          v-model="editor.title"
          maxlength="120"
        />
      </el-form-item>
      <el-form-item :label="t('assistant.notes.summary')">
        <el-input
          v-model="editor.summary"
          maxlength="300"
        />
      </el-form-item>
      <el-form-item :label="t('assistant.notes.pinned')">
        <el-switch v-model="editor.pinned" />
        <span class="muted note-hint">{{ t('assistant.notes.pinnedHint') }}</span>
      </el-form-item>
      <el-form-item :label="t('assistant.notes.body')">
        <el-input
          v-model="editor.body"
          type="textarea"
          :rows="16"
        />
      </el-form-item>
      <el-button
        type="primary"
        :loading="busy"
        data-test="save-page"
        @click="save"
      >
        {{ t('assistant.notes.save') }}
      </el-button>
      <el-button @click="editor.open = false">
        {{ t('assistant.notes.cancel') }}
      </el-button>
      <span
        v-if="!editor.isNew"
        class="muted note-hint"
      >{{ t('assistant.notes.version', { n: editor.version }) }}</span>
    </el-form>
  </section>
</template>
<style scoped>
.assistant-section ul { list-style: none; padding: 0; }
.assistant-section li { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; padding: 8px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.note-actions { margin-left: auto; }
.note-viewer pre { white-space: pre-wrap; word-break: break-word; }
.note-editor { margin-top: 16px; max-width: 880px; }
.note-hint { margin-left: 8px; }
.muted { color: var(--el-text-color-secondary); }
</style>
```

- [ ] **Step 4: 运行确认通过**

Run（在 `web/` 下）: `npx vitest run tests/AssistantNotes.spec.ts`
Expected: PASS。

Run（在 `web/` 下）: `npm run lint && npm run build`
Expected: 没有 error；`vue-tsc` 与构建通过。

- [ ] **Step 5: 提交**

```bash
git add web/src/components/assistant/AssistantNotes.vue web/tests/AssistantNotes.spec.ts
git commit -m "我的助理页：知识库分区，置顶页醒目标出，所有者可以查看、编辑、置顶与删除"
```

---

### Task 11: 设置分区与停用

**Files:**
- Create: `web/src/utils/assistantSettings.ts`
- Create: `web/src/components/assistant/AssistantSettings.vue`
- Test: `web/tests/AssistantSettings.spec.ts`（新建）

**Interfaces:**
- Consumes: Task 8 的 `myAssistant.settings / saveSettings / deprovision`、类型 `AssistantPrefs` / `AssistantPrefsPatch`；文案 `assistant.settings.*`。
- Produces：
  - `@/utils/assistantSettings`：`interface PrefsForm`（`timezone`、`work_start`、`work_end`、`workdays: number[]`、`focus_chats: string`、`ignored_chats: string`（一行一个）、`follow_platform: boolean`、`daily_turn_budget: number`）；`type PrefsField`；`lines(text): string[]`；`toForm(prefs): PrefsForm`；`validTimezone(name): boolean`；`validate(form, prefs): Partial<Record<PrefsField, string>>`（值是 `assistant.settings.errors` 之下的文案键）；`toPatch(form, prefs): AssistantPrefsPatch`（只含改过的键）。
  - 组件 `AssistantSettings`：属性 `name: string`（助理的名字，停用确认要输入它）、`runtime: string | null`；事件 `deprovisioned`；`defineExpose({ form, save, load })`。测试钩子：`data-test="save-settings"`、`deprovision`、`settings-runtime`。

- [ ] **Step 1: 写失败测试**

`web/tests/AssistantSettings.spec.ts`（新建）：

```ts
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/myAssistant', async (original) => ({
  ...(await original<typeof import('@/api/myAssistant')>()),
  myAssistant: { settings: vi.fn(), saveSettings: vi.fn(), deprovision: vi.fn() },
}))

import { myAssistant, type AssistantPrefs } from '@/api/myAssistant'
import AssistantSettings from '@/components/assistant/AssistantSettings.vue'
import { i18n } from '@/i18n'
import { lines, toForm, toPatch, validate, validTimezone, type PrefsField, type PrefsForm } from '@/utils/assistantSettings'

const t = i18n.global.t
const prefs: AssistantPrefs = {
  timezone: 'Asia/Shanghai', work_start: '09:30', work_end: '19:00', workdays: [1, 2, 3, 4, 5],
  focus_chats: ['oc_a'], ignored_chats: [], daily_turn_budget: null, platform_daily_turn_budget: 200,
  limits: { focus_chats: 5, ignored_chats: 50, budget_min: 10 }, locked: false,
  on_shift: true, shift_override: null, shift_override_until: null, cold_start: 'done', today: '2026-11-13',
}
const CASES: [Partial<PrefsForm>, Partial<Record<PrefsField, string>>][] = [
  [{}, {}],
  [{ timezone: 'America/New_York' }, {}],
  [{ timezone: 'Asia/Nowhere' }, { timezone: 'timezone' }],
  [{ timezone: '  ' }, { timezone: 'timezone' }],
  [{ work_start: '9:30' }, { work_start: 'time' }],
  [{ work_end: '25:00' }, { work_end: 'time' }],
  [{ work_start: '20:00' }, { work_end: 'hours' }],
  [{ work_start: '19:00' }, { work_end: 'hours' }],
  [{ workdays: [] }, { workdays: 'workdays' }],
  [{ focus_chats: 'oc_a\nnot-a-chat' }, { focus_chats: 'chatId' }],
  [{ focus_chats: 'oc_1\noc_2\noc_3\noc_4\noc_5\noc_6' }, { focus_chats: 'focusTooMany' }],
  [{ ignored_chats: 'ou_person' }, { ignored_chats: 'chatId' }],
  [{ ignored_chats: 'oc_b\noc_a' }, { ignored_chats: 'chatInBoth' }],
  [{ follow_platform: false, daily_turn_budget: 9 }, { daily_turn_budget: 'budget' }],
  [{ follow_platform: false, daily_turn_budget: 201 }, { daily_turn_budget: 'budget' }],
  [{ follow_platform: false, daily_turn_budget: 20.5 }, { daily_turn_budget: 'budget' }],
  [{ follow_platform: false, daily_turn_budget: 200 }, {}],
  // 跟随平台默认时，输入框里留着什么数都不校验。
  [{ follow_platform: true, daily_turn_budget: 9999 }, {}],
]

describe('settings form rules', () => {
  it.each(CASES)('validate %j gives %j', (change, expected) => {
    expect(validate({ ...toForm(prefs), ...change }, prefs)).toEqual(expected)
  })

  it('knows time zones and splits chat lists', () => {
    expect(validTimezone('UTC')).toBe(true)
    expect(validTimezone('Mars/Olympus')).toBe(false)
    expect(lines(' oc_a \n\noc_b\noc_a\n')).toEqual(['oc_a', 'oc_b'])
    expect(toForm({ ...prefs, daily_turn_budget: 80 })).toMatchObject({ follow_platform: false, daily_turn_budget: 80, focus_chats: 'oc_a' })
    expect(toForm(prefs)).toMatchObject({ follow_platform: true, daily_turn_budget: 200 })
  })

  it('builds a patch with only what changed', () => {
    expect(toPatch(toForm(prefs), prefs)).toEqual({})
    expect(toPatch({ ...toForm(prefs), timezone: 'UTC', workdays: [5, 1], focus_chats: 'oc_a\noc_b' }, prefs)).toEqual({
      timezone: 'UTC', workdays: [1, 5], focus_chats: ['oc_a', 'oc_b'],
    })
    expect(toPatch({ ...toForm(prefs), follow_platform: false, daily_turn_budget: 120 }, prefs)).toEqual({ daily_turn_budget: 120 })
    const own = { ...prefs, daily_turn_budget: 120 }
    expect(toPatch({ ...toForm(own), follow_platform: true }, own)).toEqual({ daily_turn_budget: null })
  })
})

const render = () => mount(AssistantSettings, { props: { name: '个人助理', runtime: 'pool-1' }, global: { plugins: [ElementPlus, i18n] } })
const form = (wrapper: ReturnType<typeof render>) => (wrapper.vm as unknown as { form: PrefsForm }).form

describe('settings section', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.mocked(myAssistant.settings).mockReset().mockResolvedValue(prefs)
    vi.mocked(myAssistant.saveSettings).mockReset().mockImplementation(async (patch) => ({ ...prefs, ...patch }) as AssistantPrefs)
    vi.mocked(myAssistant.deprovision).mockReset().mockResolvedValue({ state: 'deprovisioning' })
    vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
  })

  it('loads the current values and shows the runtime', async () => {
    const wrapper = render(); await flushPromises()
    expect(form(wrapper)).toMatchObject({ timezone: 'Asia/Shanghai', work_start: '09:30', follow_platform: true })
    expect(wrapper.get('[data-test="settings-runtime"]').text()).toContain('pool-1')
    expect(wrapper.text()).toContain(t('assistant.settings.budgetFollow', { value: 200 }))
    wrapper.unmount()
  })

  it('blocks saving and shows the reason when a rule is broken', async () => {
    const wrapper = render(); await flushPromises()
    form(wrapper).work_start = '20:00'
    await wrapper.get('[data-test="save-settings"]').trigger('click'); await flushPromises()
    expect(myAssistant.saveSettings).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain(t('assistant.settings.errors.hours'))
    form(wrapper).work_start = '08:00'
    await wrapper.get('[data-test="save-settings"]').trigger('click'); await flushPromises()
    expect(myAssistant.saveSettings).toHaveBeenCalledWith({ work_start: '08:00' })
    expect(wrapper.text()).not.toContain(t('assistant.settings.errors.hours'))
    wrapper.unmount()
  })

  it('sends only the changed keys', async () => {
    const wrapper = render(); await flushPromises()
    Object.assign(form(wrapper), { timezone: 'UTC', follow_platform: false, daily_turn_budget: 120, ignored_chats: 'oc_noise' })
    await wrapper.get('[data-test="save-settings"]').trigger('click'); await flushPromises()
    expect(myAssistant.saveSettings).toHaveBeenCalledWith({ timezone: 'UTC', ignored_chats: ['oc_noise'], daily_turn_budget: 120 })
    wrapper.unmount()
  })

  it('does not call the API when nothing changed', async () => {
    const info = vi.spyOn(ElMessage, 'info').mockReturnValue({ close: () => {} })
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="save-settings"]').trigger('click'); await flushPromises()
    expect(myAssistant.saveSettings).not.toHaveBeenCalled()
    expect(info).toHaveBeenCalledWith(t('assistant.settings.nothingChanged'))
    wrapper.unmount()
  })

  it('shows the backend message when the change is refused', async () => {
    const error = vi.spyOn(ElMessage, 'error').mockReturnValue({ close: () => {} })
    vi.mocked(myAssistant.saveSettings).mockRejectedValueOnce(new Error('每日轮次预算不能超过平台默认值'))
    const wrapper = render(); await flushPromises()
    form(wrapper).timezone = 'UTC'
    await wrapper.get('[data-test="save-settings"]').trigger('click'); await flushPromises()
    expect(error).toHaveBeenCalledWith('每日轮次预算不能超过平台默认值')
    wrapper.unmount()
  })

  it('shuts the assistant down only after its name is typed', async () => {
    const prompt = vi.spyOn(ElMessageBox, 'prompt').mockResolvedValue({ value: '个人助理', action: 'confirm' } as never)
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="deprovision"]').trigger('click'); await flushPromises()
    expect(prompt.mock.calls[0][0]).toBe(t('assistant.settings.deprovisionPrompt', { name: '个人助理' }))
    const options = prompt.mock.calls[0][2] as { inputValidator: (value: string) => boolean | string }
    expect(options.inputValidator('个人助理')).toBe(true)
    expect(options.inputValidator('个人')).toBe(t('assistant.settings.deprovisionMismatch'))
    expect(options.inputValidator('')).toBe(t('assistant.settings.deprovisionMismatch'))
    expect(myAssistant.deprovision).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('deprovisioned')).toHaveLength(1)
    wrapper.unmount()
  })

  it('does not shut down when the confirmation is dismissed', async () => {
    vi.spyOn(ElMessageBox, 'prompt').mockRejectedValue('cancel')
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="deprovision"]').trigger('click'); await flushPromises()
    expect(myAssistant.deprovision).not.toHaveBeenCalled()
    expect(wrapper.emitted('deprovisioned')).toBeUndefined()
    wrapper.unmount()
  })
})
```

- [ ] **Step 2: 运行确认失败**

Run（在 `web/` 下）: `npx vitest run tests/AssistantSettings.spec.ts`
Expected: FAIL：找不到 `@/utils/assistantSettings`。

- [ ] **Step 3: 实现表单规则**

`web/src/utils/assistantSettings.ts`（新建）：

```ts
import type { AssistantPrefs, AssistantPrefsPatch } from '@/api/myAssistant'

/** 「我的助理」设置表单的值：两个会话列表在表单里是多行文本，一行一个 chat_id。 */
export interface PrefsForm {
  timezone: string
  work_start: string
  work_end: string
  workdays: number[]
  focus_chats: string
  ignored_chats: string
  /** 跟随平台默认：提交时每日预算传 null。 */
  follow_platform: boolean
  daily_turn_budget: number
}
export type PrefsField = 'timezone' | 'work_start' | 'work_end' | 'workdays' | 'focus_chats' | 'ignored_chats' | 'daily_turn_budget'

const CLOCK = /^([01][0-9]|2[0-3]):[0-5][0-9]$/
const CHAT = /^oc_[A-Za-z0-9_-]{1,80}$/

/** 多行文本 → 去掉空行与重复之后的各行。 */
export function lines(text: string): string[] {
  return [...new Set(text.split('\n').map((line) => line.trim()).filter(Boolean))]
}

export function toForm(prefs: AssistantPrefs): PrefsForm {
  return {
    timezone: prefs.timezone,
    work_start: prefs.work_start,
    work_end: prefs.work_end,
    workdays: [...prefs.workdays],
    focus_chats: prefs.focus_chats.join('\n'),
    ignored_chats: prefs.ignored_chats.join('\n'),
    follow_platform: prefs.daily_turn_budget === null,
    daily_turn_budget: prefs.daily_turn_budget ?? prefs.platform_daily_turn_budget,
  }
}

/** 浏览器认不认识这个 IANA 时区名。 */
export function validTimezone(name: string): boolean {
  if (!name.trim()) return false
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: name.trim() })
    return true
  } catch {
    return false
  }
}

/**
 * 出错的字段 → 文案键（`assistant.settings.errors` 之下）；没有错误是空对象。
 * 与后端 `coreman/core/assistants/settings.py` 的 `update` 同一套规则，为的是不发一个注定被拒的请求；后端仍是最终裁决。
 */
export function validate(form: PrefsForm, prefs: AssistantPrefs): Partial<Record<PrefsField, string>> {
  const errors: Partial<Record<PrefsField, string>> = {}
  if (!validTimezone(form.timezone)) errors.timezone = 'timezone'
  if (!CLOCK.test(form.work_start)) errors.work_start = 'time'
  if (!CLOCK.test(form.work_end)) errors.work_end = 'time'
  // HH:MM 定宽，按字典序比就是按时间比。
  if (!errors.work_start && !errors.work_end && form.work_start >= form.work_end) errors.work_end = 'hours'
  if (!form.workdays.length) errors.workdays = 'workdays'
  const focus = lines(form.focus_chats), ignored = lines(form.ignored_chats)
  if (focus.some((id) => !CHAT.test(id))) errors.focus_chats = 'chatId'
  else if (focus.length > prefs.limits.focus_chats) errors.focus_chats = 'focusTooMany'
  if (ignored.some((id) => !CHAT.test(id))) errors.ignored_chats = 'chatId'
  else if (ignored.length > prefs.limits.ignored_chats) errors.ignored_chats = 'ignoredTooMany'
  else if (!errors.focus_chats && ignored.some((id) => focus.includes(id))) errors.ignored_chats = 'chatInBoth'
  const budget = form.daily_turn_budget
  if (!form.follow_platform && (!Number.isInteger(budget) || budget < prefs.limits.budget_min || budget > prefs.platform_daily_turn_budget)) {
    errors.daily_turn_budget = 'budget'
  }
  return errors
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

/** 表单 → 请求体，只带改过的键。 */
export function toPatch(form: PrefsForm, prefs: AssistantPrefs): AssistantPrefsPatch {
  const next = {
    timezone: form.timezone.trim(),
    work_start: form.work_start,
    work_end: form.work_end,
    workdays: [...form.workdays].sort((a, b) => a - b),
    focus_chats: lines(form.focus_chats),
    ignored_chats: lines(form.ignored_chats),
    daily_turn_budget: form.follow_platform ? null : form.daily_turn_budget,
  }
  const patch: Record<string, unknown> = {}
  for (const key of Object.keys(next) as (keyof typeof next)[]) {
    if (!same(next[key], prefs[key])) patch[key] = next[key]
  }
  return patch as AssistantPrefsPatch
}
```

- [ ] **Step 4: 实现分区组件**

`web/src/components/assistant/AssistantSettings.vue`（新建）：

```vue
<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { myAssistant, type AssistantPrefs } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import { toForm, toPatch, validate, type PrefsField, type PrefsForm } from '@/utils/assistantSettings'
import { errorMessage } from '@/utils/errors'

const props = defineProps<{ name: string; runtime: string | null }>()
const emit = defineEmits<{ deprovisioned: [] }>()
const { t } = useI18n()

const WEEKDAYS = [1, 2, 3, 4, 5, 6, 7]
const prefs = ref<AssistantPrefs | null>(null)
const form = reactive<PrefsForm>({ timezone: '', work_start: '', work_end: '', workdays: [], focus_chats: '', ignored_chats: '', follow_platform: true, daily_turn_budget: 0 })
const errors = ref<Partial<Record<PrefsField, string>>>({})
const error = ref(''), loading = ref(false), saving = ref(false)

/** 这一项的错误提示；没有错误是空串（el-form-item 不显示）。 */
const fieldError = (field: PrefsField) => (errors.value[field] ? t('assistant.settings.errors.' + errors.value[field]) : '')
function apply(data: AssistantPrefs) {
  prefs.value = data
  Object.assign(form, toForm(data))
  errors.value = {}
}
async function load() {
  loading.value = true; error.value = ''
  try { apply(await myAssistant.settings()) }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
async function save() {
  if (!prefs.value || saving.value) return
  errors.value = validate(form, prefs.value)
  if (Object.keys(errors.value).length) return
  const patch = toPatch(form, prefs.value)
  if (!Object.keys(patch).length) { ElMessage.info(t('assistant.settings.nothingChanged')); return }
  saving.value = true
  try { apply(await myAssistant.saveSettings(patch)); ElMessage.success(t('assistant.settings.saved')) }
  catch (e) { ElMessage.error(errorMessage(e)) }
  finally { saving.value = false }
}
/** 停用不可撤销：要所有者原样输入助理的名字才发请求；取消确认框不算失败。 */
async function deprovision() {
  try {
    await ElMessageBox.prompt(t('assistant.settings.deprovisionPrompt', { name: props.name }), t('assistant.settings.danger'), {
      type: 'warning',
      inputValidator: (input: string) => (input ?? '').trim() === props.name || t('assistant.settings.deprovisionMismatch'),
    })
    await myAssistant.deprovision()
    ElMessage.success(t('assistant.settings.deprovisionStarted'))
    emit('deprovisioned')
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorMessage(e)) }
}
onMounted(load)
defineExpose({ form, save, load })
</script>
<template>
  <section class="assistant-section">
    <LoadState
      :loading="loading && !prefs"
      :error="error"
      @retry="load"
    />
    <el-form
      v-if="prefs"
      label-width="160px"
      class="assistant-settings"
      @submit.prevent
    >
      <el-form-item
        :label="t('assistant.settings.timezone')"
        :error="fieldError('timezone')"
      >
        <el-input
          v-model="form.timezone"
          style="width: 260px"
        />
        <div class="hint">
          {{ t('assistant.settings.timezoneHint') }}
        </div>
      </el-form-item>
      <el-form-item
        :label="t('assistant.settings.workStart')"
        :error="fieldError('work_start')"
      >
        <el-input
          v-model="form.work_start"
          placeholder="09:30"
          style="width: 120px"
        />
      </el-form-item>
      <el-form-item
        :label="t('assistant.settings.workEnd')"
        :error="fieldError('work_end')"
      >
        <el-input
          v-model="form.work_end"
          placeholder="19:00"
          style="width: 120px"
        />
      </el-form-item>
      <el-form-item
        :label="t('assistant.settings.workdays')"
        :error="fieldError('workdays')"
      >
        <el-checkbox-group v-model="form.workdays">
          <el-checkbox
            v-for="day in WEEKDAYS"
            :key="day"
            :value="day"
          >
            {{ t('assistant.settings.weekdays.d' + day) }}
          </el-checkbox>
        </el-checkbox-group>
      </el-form-item>
      <el-form-item
        :label="t('assistant.settings.focusChats')"
        :error="fieldError('focus_chats')"
      >
        <el-input
          v-model="form.focus_chats"
          type="textarea"
          :rows="3"
          :disabled="prefs.locked"
        />
        <div class="hint">
          {{ t('assistant.settings.focusChatsHint', { count: prefs.limits.focus_chats }) }}
        </div>
      </el-form-item>
      <el-form-item
        :label="t('assistant.settings.ignoredChats')"
        :error="fieldError('ignored_chats')"
      >
        <el-input
          v-model="form.ignored_chats"
          type="textarea"
          :rows="3"
          :disabled="prefs.locked"
        />
        <div class="hint">
          {{ t('assistant.settings.ignoredChatsHint', { count: prefs.limits.ignored_chats }) }}
        </div>
      </el-form-item>
      <el-form-item
        :label="t('assistant.settings.budget')"
        :error="fieldError('daily_turn_budget')"
      >
        <el-checkbox v-model="form.follow_platform">
          {{ t('assistant.settings.budgetFollow', { value: prefs.platform_daily_turn_budget }) }}
        </el-checkbox>
        <!-- el-input-number 清空时发 undefined：保持原值不动。 -->
        <el-input-number
          :model-value="form.daily_turn_budget"
          :min="prefs.limits.budget_min"
          :max="prefs.platform_daily_turn_budget"
          :disabled="form.follow_platform"
          class="budget-input"
          @update:model-value="form.daily_turn_budget = $event ?? form.daily_turn_budget"
        />
        <div class="hint">
          {{ t('assistant.settings.budgetHint', { value: prefs.platform_daily_turn_budget }) }}
        </div>
      </el-form-item>
      <el-form-item
        :label="t('assistant.settings.runtime')"
        data-test="settings-runtime"
      >
        {{ props.runtime ?? t('assistant.settings.runtimeNone') }}
      </el-form-item>
      <el-form-item>
        <el-button
          type="primary"
          :loading="saving"
          data-test="save-settings"
          @click="save"
        >
          {{ t('assistant.settings.save') }}
        </el-button>
      </el-form-item>
    </el-form>
    <el-alert
      type="error"
      :closable="false"
      show-icon
      class="danger-zone"
      :title="t('assistant.settings.danger')"
    >
      <p>{{ t('assistant.settings.dangerHint') }}</p>
      <el-button
        type="danger"
        plain
        data-test="deprovision"
        @click="deprovision"
      >
        {{ t('assistant.settings.deprovision') }}
      </el-button>
    </el-alert>
  </section>
</template>
<style scoped>
.assistant-settings { max-width: 760px; }
.hint { flex-basis: 100%; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
.budget-input { margin-left: 16px; }
.danger-zone { margin-top: 24px; max-width: 760px; }
</style>
```

- [ ] **Step 5: 运行确认通过**

Run（在 `web/` 下）: `npx vitest run tests/AssistantSettings.spec.ts`
Expected: PASS。

Run（在 `web/` 下）: `npm run lint && npm run build`
Expected: 没有 error；`vue-tsc` 与构建通过。

- [ ] **Step 6: 提交**

```bash
git add web/src/utils/assistantSettings.ts web/src/components/assistant/AssistantSettings.vue \
  web/tests/AssistantSettings.spec.ts
git commit -m "我的助理页：设置分区带校验，停用要输入助理的名字二次确认"
```

---

### Task 12: 「我的助理」页的壳、路由与菜单，`docs/feishu.md`

**Files:**
- Create: `web/src/views/MyAssistantView.vue`
- Modify: `web/src/router/index.ts`（`my-feishu` 那条路由之前）
- Modify: `web/src/layouts/AdminLayout.vue`（`MENU`、图标导入、`icons`、`groups`）
- Modify: `web/tests/AdminLayout.spec.ts`（新增一个用例）
- Modify: `docs/feishu.md`（「个人助理应用」一节）
- Test: `web/tests/MyAssistantView.spec.ts`（新建）

**Interfaces:**
- Consumes: Task 8 的 `myAssistant.overview`、类型 `AssistantOverview` / `AssistantAbsent`；Task 8–11 的六个分区组件（`AssistantSettings` 的属性 `name`、`runtime` 与事件 `deprovisioned`）；文案 `assistant.page.*`、`assistant.empty.*`、`assistant.overview.*`、`menu.myAssistant`。
- Produces：
  - 路由 `{ path: 'my-assistant', name: 'my-assistant' }`（所有登录用户）。
  - 菜单项 `{ key: 'myAssistant', path: '/my-assistant' }`，在「AI 协作」分组（`groups` 里的 `collaboration`，各个「我的…」页都在这一组）里排在「我的飞书」之前。
  - 页面 `MyAssistantView`：`defineExpose({ tab, load })`；测试钩子 `data-test="assistant-empty"`、`assistant-overview`、`assistant-locked`。

- [ ] **Step 1: 写失败测试**

`web/tests/MyAssistantView.spec.ts`（新建）：

```ts
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

vi.mock('@/api/myAssistant', async (original) => ({
  ...(await original<typeof import('@/api/myAssistant')>()),
  myAssistant: { overview: vi.fn() },
}))
// 各分区有自己的用例，这里换成只带标记的替身：壳只管概览、空状态与标签页。
vi.mock('@/components/assistant/AssistantAttention.vue', () => ({ default: { template: '<div data-test="pane-attention" />' } }))
vi.mock('@/components/assistant/AssistantJobs.vue', () => ({ default: { template: '<div data-test="pane-jobs" />' } }))
vi.mock('@/components/assistant/AssistantFollowups.vue', () => ({ default: { template: '<div data-test="pane-followups" />' } }))
vi.mock('@/components/assistant/AssistantRecent.vue', () => ({ default: { template: '<div data-test="pane-recent" />' } }))
vi.mock('@/components/assistant/AssistantNotes.vue', () => ({ default: { template: '<div data-test="pane-notes" />' } }))
vi.mock('@/components/assistant/AssistantSettings.vue', () => ({
  default: { props: ['name', 'runtime'], emits: ['deprovisioned'], template: '<div data-test="pane-settings">{{ name }} / {{ runtime }}</div>' },
}))

import { myAssistant, type AssistantAbsent, type AssistantOverview } from '@/api/myAssistant'
import { i18n } from '@/i18n'
import { MENU } from '@/layouts/AdminLayout.vue'
import { router } from '@/router'
import MyAssistantView from '@/views/MyAssistantView.vue'

const t = i18n.global.t
const absent: AssistantAbsent = {
  exists: false, eligible: true, reason: null,
  how_to: { app_name: '个人助理', steps: ['open_private_chat', 'accept_offer', 'authorize'] }, lifecycle: null,
}
const overview: AssistantOverview = {
  exists: true, bot_id: 'b1', name: '个人助理', state: 'active', runtime: { name: 'pool-1', online: true }, paused_reason: null,
  on_shift: true, cold_start: { state: 'in_progress', step: 'focus', outcome: null }, turns: { today: 3, budget: 50 },
  watch: { state: 'polling', last_ok_at: '2026-11-13T02:00:00Z', last_error: null },
  lifecycle: null, content_locked: false, approval_system: { name: null, url: null },
}
const render = () => mount(MyAssistantView, { global: { plugins: [ElementPlus, i18n] } })

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(myAssistant.overview).mockReset().mockResolvedValue(overview)
})

describe('My assistant page', () => {
  it('is a personal page every signed-in user can open', () => {
    const entry = MENU.find((item) => item.key === 'myAssistant')
    expect(entry?.path).toBe('/my-assistant')
    expect(entry?.roles).toBeUndefined()
    const route = router.resolve({ name: 'my-assistant' })
    expect(route.path).toBe('/my-assistant')
    expect(route.meta.roles).toBeUndefined()
  })

  it('tells someone without an assistant how to get one', async () => {
    vi.mocked(myAssistant.overview).mockResolvedValue(absent)
    const wrapper = render(); await flushPromises()
    const empty = wrapper.get('[data-test="assistant-empty"]')
    expect(empty.text()).toContain(t('assistant.empty.title'))
    expect(empty.text()).toContain(t('assistant.empty.app', { name: '个人助理' }))
    for (const step of absent.how_to.steps) expect(empty.text()).toContain(t(`assistant.empty.steps.${step}`))
    expect(wrapper.find('[data-test="assistant-overview"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="pane-attention"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('gives the reason instead of the steps when the member is out of scope', async () => {
    vi.mocked(myAssistant.overview).mockResolvedValue({ ...absent, eligible: false, reason: 'not_listed', how_to: { app_name: null, steps: absent.how_to.steps } })
    const wrapper = render(); await flushPromises()
    expect(wrapper.text()).toContain(t('assistant.empty.reasons.not_listed'))
    expect(wrapper.text()).not.toContain(t('assistant.empty.steps.accept_offer'))
    wrapper.unmount()
  })

  it('says setup is in progress while a flow is running', async () => {
    vi.mocked(myAssistant.overview).mockResolvedValue({ ...absent, lifecycle: { action: 'provision', step: 'offered' } })
    const wrapper = render(); await flushPromises()
    expect(wrapper.text()).toContain(t('assistant.empty.inProgress'))
    expect(wrapper.text()).not.toContain(t('assistant.empty.steps.accept_offer'))
    wrapper.unmount()
  })

  it('shows state, runtime, shift, cold start, turns and watch health', async () => {
    const wrapper = render(); await flushPromises()
    const head = wrapper.get('[data-test="assistant-overview"]').text()
    expect(head).toContain(t('assistant.overview.states.active'))
    expect(head).toContain('pool-1')
    expect(head).toContain(t('assistant.overview.online'))
    expect(head).toContain(t('assistant.overview.onShift'))
    expect(head).toContain(t('assistant.overview.coldStartStates.in_progress'))
    expect(head).toContain(t('assistant.overview.turnsValue', { n: 3, total: 50 }))
    expect(head).toContain(t('assistant.overview.watchStates.polling'))
    expect(wrapper.find('[data-test="assistant-locked"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows why turns cannot run and that content is unreadable', async () => {
    vi.mocked(myAssistant.overview).mockResolvedValue({
      ...overview, runtime: null, paused_reason: 'runtime_offline', on_shift: false, watch: null, content_locked: true,
      cold_start: { state: 'done', step: null, outcome: 'completed' },
    })
    const wrapper = render(); await flushPromises()
    const head = wrapper.get('[data-test="assistant-overview"]').text()
    expect(head).toContain(t('assistant.overview.runtimeNone'))
    expect(head).toContain(t('assistant.overview.paused', { detail: 'runtime_offline' }))
    expect(head).toContain(t('assistant.overview.offShift'))
    expect(head).toContain(t('assistant.overview.coldStartStates.done'))
    expect(head).toContain(t('assistant.overview.watchNone'))
    expect(wrapper.get('[data-test="assistant-locked"]').text()).toContain(t('assistant.page.locked'))
    wrapper.unmount()
  })

  it('mounts sections lazily and reloads the overview after shutdown starts', async () => {
    const wrapper = render(); await flushPromises()
    for (const name of ['attention', 'jobs', 'followups', 'recent', 'notes', 'settings']) expect(wrapper.text()).toContain(t(`assistant.page.tabs.${name}`))
    expect(wrapper.find('[data-test="pane-attention"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="pane-settings"]').exists()).toBe(false)
    ;(wrapper.vm as unknown as { tab: string }).tab = 'settings'
    await nextTick(); await flushPromises()
    const settings = wrapper.get('[data-test="pane-settings"]')
    expect(settings.text()).toBe('个人助理 / pool-1')
    wrapper.findComponent('[data-test="pane-settings"]').vm.$emit('deprovisioned')
    await flushPromises()
    expect(myAssistant.overview).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('shows the error when the overview cannot be loaded', async () => {
    vi.mocked(myAssistant.overview).mockRejectedValue(new Error('加载不了'))
    const wrapper = render(); await flushPromises()
    expect(wrapper.text()).toContain('加载不了')
    expect(wrapper.find('[data-test="assistant-empty"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
```

`web/tests/AdminLayout.spec.ts`：在 `describe('AdminLayout menu', …)` 里最后一个用例之后加：

```ts
  it('lists My assistant right before My Feishu for every signed-in user', () => {
    for (const role of ['member', 'team_lead', 'platform_admin']) {
      const labels = mountAs(role).findAll('.el-menu-item').map((el) => el.text())
      const feishu = labels.indexOf(i18n.global.t('menu.myFeishu'))
      expect(feishu).toBeGreaterThan(0)
      expect(labels[feishu - 1]).toBe(i18n.global.t('menu.myAssistant'))
    }
  })
```

- [ ] **Step 2: 运行确认失败**

Run（在 `web/` 下）: `npx vitest run tests/MyAssistantView.spec.ts tests/AdminLayout.spec.ts`
Expected: FAIL：找不到 `@/views/MyAssistantView.vue`；菜单用例找不到「我的助理」。

- [ ] **Step 3: 实现页面壳**

`web/src/views/MyAssistantView.vue`（新建）：

```vue
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { myAssistant, type AssistantAbsent, type AssistantOverview } from '@/api/myAssistant'
import AssistantAttention from '@/components/assistant/AssistantAttention.vue'
import AssistantFollowups from '@/components/assistant/AssistantFollowups.vue'
import AssistantJobs from '@/components/assistant/AssistantJobs.vue'
import AssistantNotes from '@/components/assistant/AssistantNotes.vue'
import AssistantRecent from '@/components/assistant/AssistantRecent.vue'
import AssistantSettings from '@/components/assistant/AssistantSettings.vue'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const data = ref<AssistantOverview | AssistantAbsent | null>(null)
const error = ref(''), loading = ref(false)
const tab = ref('attention')
/** 有助理时的概览；没有助理时是 null。 */
const mine = computed(() => (data.value?.exists ? data.value : null))
/** 没有助理时的空状态；有助理时是 null。 */
const absent = computed(() => (data.value && !data.value.exists ? data.value : null))

async function load() {
  loading.value = true; error.value = ''
  try { data.value = await myAssistant.overview() }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
onMounted(load)
defineExpose({ tab, load })
</script>
<template>
  <section class="my-assistant">
    <div class="cm-page-header">
      <h1>{{ t('menu.myAssistant') }}</h1>
      <p class="cm-page-intro">
        {{ t('assistant.page.intro') }}
      </p>
      <el-button
        :loading="loading"
        @click="load"
      >
        {{ t('assistant.page.refresh') }}
      </el-button>
    </div>
    <LoadState
      :loading="loading && !data"
      :error="error"
      @retry="load"
    />
    <el-empty
      v-if="absent"
      :description="t('assistant.empty.title')"
      data-test="assistant-empty"
    >
      <p v-if="absent.lifecycle">
        {{ t('assistant.empty.inProgress') }}
      </p>
      <template v-else-if="absent.eligible">
        <p>{{ t('assistant.empty.eligible') }}</p>
        <p v-if="absent.how_to.app_name">
          {{ t('assistant.empty.app', { name: absent.how_to.app_name }) }}
        </p>
        <ol class="how-to">
          <li
            v-for="step in absent.how_to.steps"
            :key="step"
          >
            {{ t('assistant.empty.steps.' + step) }}
          </li>
        </ol>
      </template>
      <p v-else>
        {{ t('assistant.empty.reasons.' + absent.reason) }}
      </p>
    </el-empty>
    <template v-if="mine">
      <dl
        class="assistant-overview"
        data-test="assistant-overview"
      >
        <div>
          <dt>{{ t('assistant.overview.state') }}</dt>
          <dd>
            <el-tag :type="mine.state === 'active' ? 'success' : 'warning'">
              {{ t('assistant.overview.states.' + (mine.state ?? 'unknown')) }}
            </el-tag>
            <span
              v-if="mine.paused_reason"
              class="muted"
            >{{ t('assistant.overview.paused', { detail: mine.paused_reason }) }}</span>
          </dd>
        </div>
        <div>
          <dt>{{ t('assistant.overview.runtime') }}</dt>
          <dd v-if="mine.runtime">
            {{ mine.runtime.name }} · {{ mine.runtime.online ? t('assistant.overview.online') : t('assistant.overview.offline') }}
          </dd>
          <dd v-else>
            {{ t('assistant.overview.runtimeNone') }}
          </dd>
        </div>
        <div>
          <dt>{{ t('assistant.overview.shift') }}</dt>
          <dd>{{ mine.on_shift ? t('assistant.overview.onShift') : t('assistant.overview.offShift') }}</dd>
        </div>
        <div>
          <dt>{{ t('assistant.overview.coldStart') }}</dt>
          <dd>{{ t('assistant.overview.coldStartStates.' + mine.cold_start.state) }}</dd>
        </div>
        <div>
          <dt>{{ t('assistant.overview.turns') }}</dt>
          <dd>{{ t('assistant.overview.turnsValue', { n: mine.turns.today, total: mine.turns.budget }) }}</dd>
        </div>
        <div>
          <dt>{{ t('assistant.overview.watch') }}</dt>
          <dd v-if="mine.watch">
            {{ t('assistant.overview.watchStates.' + mine.watch.state) }}
            <span
              v-if="mine.watch.last_ok_at"
              class="muted"
            >{{ t('assistant.overview.watchLastOk', { time: formatDateTime(mine.watch.last_ok_at) }) }}</span>
            <span
              v-if="mine.watch.last_error"
              class="muted"
            >{{ t('assistant.overview.watchError', { detail: mine.watch.last_error }) }}</span>
          </dd>
          <dd v-else>
            {{ t('assistant.overview.watchNone') }}
          </dd>
        </div>
      </dl>
      <el-alert
        v-if="mine.content_locked"
        type="warning"
        :closable="false"
        show-icon
        data-test="assistant-locked"
        :title="t('assistant.page.locked')"
      />
      <!-- lazy：没点开的分区不取数。 -->
      <el-tabs v-model="tab">
        <el-tab-pane
          :label="t('assistant.page.tabs.attention')"
          name="attention"
          lazy
        >
          <AssistantAttention />
        </el-tab-pane>
        <el-tab-pane
          :label="t('assistant.page.tabs.jobs')"
          name="jobs"
          lazy
        >
          <AssistantJobs />
        </el-tab-pane>
        <el-tab-pane
          :label="t('assistant.page.tabs.followups')"
          name="followups"
          lazy
        >
          <AssistantFollowups />
        </el-tab-pane>
        <el-tab-pane
          :label="t('assistant.page.tabs.recent')"
          name="recent"
          lazy
        >
          <AssistantRecent />
        </el-tab-pane>
        <el-tab-pane
          :label="t('assistant.page.tabs.notes')"
          name="notes"
          lazy
        >
          <AssistantNotes />
        </el-tab-pane>
        <el-tab-pane
          :label="t('assistant.page.tabs.settings')"
          name="settings"
          lazy
        >
          <AssistantSettings
            :name="mine.name"
            :runtime="mine.runtime?.name ?? null"
            @deprovisioned="load"
          />
        </el-tab-pane>
      </el-tabs>
    </template>
  </section>
</template>
<style scoped>
.assistant-overview { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin: 16px 0; }
.assistant-overview dt { color: var(--el-text-color-secondary); font-size: 12px; }
.assistant-overview dd { margin: 4px 0 0; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.how-to { text-align: left; display: inline-block; }
.muted { color: var(--el-text-color-secondary); }
</style>
```

- [ ] **Step 4: 路由与菜单**

`web/src/router/index.ts`：在

```ts
        { path: 'my-feishu', name: 'my-feishu', component: () => import('@/views/MyFeishuView.vue') },
```

之前加一行：

```ts
        { path: 'my-assistant', name: 'my-assistant', component: () => import('@/views/MyAssistantView.vue') },
```

`web/src/layouts/AdminLayout.vue`：

`MENU` 里在 `{ key: 'myFeishu', path: '/my-feishu' },` 之前加：

```ts
  { key: 'myAssistant', path: '/my-assistant' },
```

图标导入那一行（`import { Menu as MenuIcon, Close, Grid, … } from '@element-plus/icons-vue'`）的清单里加 `Service`；`icons` 对象里加 `myAssistant: Service,`。

`groups` 的第一项（`collaboration`）的 `keys` 里，在 `'myFeishu'` 之前加 `'myAssistant'`：

```ts
  { key: 'collaboration', keys: ['bots', 'workTasks', 'skills', 'cron', 'chatLogs', 'myAssistant', 'myFeishu', 'myWecom', 'myCredentials', 'myApiTokens', 'selfReminders'] },
```

- [ ] **Step 5: 运行确认通过**

Run（在 `web/` 下）: `npx vitest run tests/MyAssistantView.spec.ts tests/AdminLayout.spec.ts tests/router.spec.ts`
Expected: PASS。

Run（在 `web/` 下）: `npx vitest run`
Expected: 全部 PASS（含 `tests/i18n.spec.ts`）。

Run（在 `web/` 下）: `npm run lint && npm run build`
Expected: 没有 error；`vue-tsc` 与构建通过。

- [ ] **Step 6: 文档**

`docs/feishu.md`：在「个人助理应用」一节的条目里，「**待跟进。**」那一条之后加一条：

```markdown
- **「我的助理」页。** 管理台菜单「AI 协作 → 我的助理」（所有登录用户可见）是所有者在飞书之外看自己助理的地方，接口在 `/api/me/assistant` 下。没有助理的人看到的是开通指引（或不在开通范围的原因）。有助理的人看到概览（状态、所在运行时、是否在班、冷启动、今天的轮次与预算、盯消息的健康状态）与六个分区：**等你出手**（待批的审批单只有摘要与跳到审批系统的入口，入口取自部署配置 `TOWER_WEB_URL`，没配就不给链接；被卡住的子任务；今天到期的待跟进）、**正在跑**（子任务的交代原文、轮数与结论，可以取消进行中的，助理会在对话里得知）、**待跟进**（按四类列出，可以勾选办完、推后、改分类、标为不再需要、新增）、**最近落地**（已执行、被拒、撤回的审批单与审批人写的理由）、**知识库**（索引、查看、编辑、新建、删除；置顶页有醒目的标记，只有所有者本人能在这里置顶、取消置顶或改写置顶页，页面上写明每一页最后是「我」「助理」还是「导入」改的；助理同时改了同一页时保存会提示并载入最新内容，不会互相覆盖）、**设置**（时区、工作时段、工作日、重点群与忽略的会话的 `chat_id`、每日轮次预算——只能调低，不能超过平台默认值——以及停用）。在页面上停用与在私聊里发「停用助理」是同一件事：要输入助理的名字确认，一旦开始不可撤销。这些接口只认交互登录的所有者本人：助理由登录人推出，路径里没有助理编号；机器人令牌一律 403；平台管理员、团队负责人调用时拿到的是自己的助理或「没有助理」，拿不到别人的任何内容。数据密钥销毁后页面只剩编号、状态与时间。每次经页面读取个人内容，平台记一条 `assistant_content_access` 日志（操作人、助理、动作、对象代号，不含内容）。
```

- [ ] **Step 7: 提交**

```bash
git add web/src/views/MyAssistantView.vue web/src/router/index.ts web/src/layouts/AdminLayout.vue \
  web/tests/MyAssistantView.spec.ts web/tests/AdminLayout.spec.ts docs/feishu.md
git commit -m "我的助理页：概览、没有助理时的开通指引与六个分区；菜单与路由；文档"
```

---

### Task 13: 管理台的助理列表

**Files:**
- Create: `web/src/api/assistantsAdmin.ts`
- Create: `web/src/views/AssistantsView.vue`
- Modify: `web/src/router/index.ts`、`web/src/layouts/AdminLayout.vue`、`web/tests/AdminLayout.spec.ts`
- Modify: `docs/operations.md`（「个人助理盯消息排障」之前加一小节）
- Test: `web/tests/AssistantsView.spec.ts`（新建）；`tests/api/test_assistant_admin.py`（不改，回归）

**Interfaces:**
- Consumes: 现有的 `GET /api/admin/assistants`（每条记录的键：`bot_id`、`state`、`enabled`、`created_at`、`owner`、`relay`、`node`、`lifecycle`、`on_shift`、`cold_start`、`directories_pending_removal`、`watch`、`turns`、`inbox_pending`，以及 3A 加的 `jobs_active`）与 `POST /api/admin/assistants/{bot_id}/deprovision`——后端不用改，这一页要的字段 1F、2D、2E、2F、3A 已经给全；文案 `assistant.admin.*`、`assistant.overview.states.*`、`assistant.overview.coldStartStates.*`、`assistant.overview.watchStates.*`、`menu.assistants`。
- Produces：
  - `@/api/assistantsAdmin`：`interface AssistantRow`；`assistantsAdmin.list()`、`assistantsAdmin.deprovision(botId)`。
  - 路由 `{ path: 'assistants', name: 'assistants', meta: { roles: ['platform_admin'] } }`；菜单项 `{ key: 'assistants', path: '/assistants', roles: ['platform_admin'] }`，在「平台管理」分组里排在「平台运行状态」之后。
  - 页面 `AssistantsView`；测试钩子 `data-test="deprovision-<bot_id>"`。

- [ ] **Step 1: 确认后端已经给全字段**

Run: `uv run pytest tests/api/test_assistant_admin.py -q`
Expected: PASS（其中 2F 的用例守着 `on_shift` 与 `cold_start`，2E 的守着 `turns` 与 `inbox_pending`，3A 的守着 `jobs_active`，1F 的守着「只有元数据」）。若 `jobs_active` 那条不存在，说明 3A 还没落地：停下来，先完成 3A。

- [ ] **Step 2: 写失败测试**

`web/tests/AssistantsView.spec.ts`（新建）：

```ts
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/assistantsAdmin', () => ({ assistantsAdmin: { list: vi.fn(), deprovision: vi.fn() } }))

import { assistantsAdmin, type AssistantRow } from '@/api/assistantsAdmin'
import { i18n } from '@/i18n'
import { MENU } from '@/layouts/AdminLayout.vue'
import { router } from '@/router'
import AssistantsView from '@/views/AssistantsView.vue'

const t = i18n.global.t
const row = (over: Partial<AssistantRow>): AssistantRow => ({
  bot_id: 'b1', state: 'active', enabled: true, created_at: '2026-11-10T00:00:00Z',
  owner: { id: 'u1', login_name: 'zhangsan', display_name: '张三' },
  relay: { id: 'r1', name: 'n1/claude' }, node: { id: 'n1', name: 'pool-1', online: true },
  lifecycle: null, on_shift: true,
  cold_start: { state: 'in_progress', step: 'focus', outcome: null, started_at: '2026-11-13T01:00:00Z', done_at: null },
  directories_pending_removal: 0,
  watch: { state: 'backoff', last_ok_at: '2026-11-13T02:00:00Z', last_error: 'rate_limited' },
  turns: { today: 12, budget: 200 }, inbox_pending: 4, jobs_active: 2, ...over,
})
const render = () => mount(AssistantsView, { global: { plugins: [ElementPlus, i18n] } })

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(assistantsAdmin.list).mockReset().mockResolvedValue({
    items: [
      row({}),
      row({ bot_id: 'b2', state: 'deprovisioned', enabled: false, owner: { id: 'u2', login_name: null, display_name: '李四' }, node: null, relay: null, watch: null, turns: null, on_shift: false, cold_start: null }),
    ],
  })
  vi.mocked(assistantsAdmin.deprovision).mockReset().mockResolvedValue({ bot_id: 'b1', state: 'deprovisioning' })
  vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
})

describe('personal assistants (admin)', () => {
  it('is open to platform administrators only', () => {
    expect(MENU.find((item) => item.key === 'assistants')).toMatchObject({ path: '/assistants', roles: ['platform_admin'] })
    const route = router.resolve({ name: 'assistants' })
    expect(route.path).toBe('/assistants')
    expect(route.meta.roles).toEqual(['platform_admin'])
  })

  it('shows who, where, health and counters, and says it is metadata only', async () => {
    const wrapper = render(); await flushPromises()
    const text = wrapper.text()
    expect(text).toContain(t('assistant.admin.intro'))
    for (const expected of ['张三 (zhangsan)', '李四', 'pool-1', '12 / 200', 'rate_limited', 'focus']) expect(text).toContain(expected)
    expect(text).toContain(t('assistant.overview.states.active'))
    expect(text).toContain(t('assistant.overview.states.deprovisioned'))
    expect(text).toContain(t('assistant.overview.coldStartStates.in_progress'))
    expect(text).toContain(t('assistant.overview.watchStates.backoff'))
    wrapper.unmount()
  })

  it('says so when nobody has an assistant', async () => {
    vi.mocked(assistantsAdmin.list).mockResolvedValue({ items: [] })
    const wrapper = render(); await flushPromises()
    expect(wrapper.text()).toContain(t('assistant.admin.empty'))
    wrapper.unmount()
  })

  it('shuts an assistant down only after the owner login name is typed', async () => {
    const prompt = vi.spyOn(ElMessageBox, 'prompt').mockResolvedValue({ value: 'zhangsan', action: 'confirm' } as never)
    const wrapper = render(); await flushPromises()
    expect(wrapper.find('[data-test="deprovision-b2"]').exists()).toBe(false)
    await wrapper.get('[data-test="deprovision-b1"]').trigger('click'); await flushPromises()
    expect(prompt.mock.calls[0][0]).toBe(t('assistant.admin.deprovisionPrompt', { name: 'zhangsan' }))
    const options = prompt.mock.calls[0][2] as { inputValidator: (value: string) => boolean | string }
    expect(options.inputValidator('zhangsan')).toBe(true)
    expect(options.inputValidator('张三')).toBe(t('assistant.admin.deprovisionMismatch'))
    expect(assistantsAdmin.deprovision).toHaveBeenCalledWith('b1')
    expect(assistantsAdmin.list).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('does nothing when the confirmation is dismissed', async () => {
    vi.spyOn(ElMessageBox, 'prompt').mockRejectedValue('cancel')
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="deprovision-b1"]').trigger('click'); await flushPromises()
    expect(assistantsAdmin.deprovision).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
```

`web/tests/AdminLayout.spec.ts`：在 Task 12 加的用例之后再加：

```ts
  it('shows Personal assistants to platform administrators only', () => {
    const label = i18n.global.t('menu.assistants')
    const labels = (role: string) => mountAs(role).findAll('.el-menu-item').map((el) => el.text())
    expect(labels('platform_admin')).toContain(label)
    for (const role of ['member', 'team_lead', 'ai_committee']) expect(labels(role)).not.toContain(label)
  })
```

- [ ] **Step 3: 运行确认失败**

Run（在 `web/` 下）: `npx vitest run tests/AssistantsView.spec.ts tests/AdminLayout.spec.ts`
Expected: FAIL：找不到 `@/api/assistantsAdmin`；菜单里没有「个人助理」。

- [ ] **Step 4: 实现**

`web/src/api/assistantsAdmin.ts`（新建）：

```ts
import { call, http } from '@/api/client'

/** 平台管理员看到的一位助理：只有元数据，没有任何内容。 */
export interface AssistantRow {
  bot_id: string
  state: 'provisioning' | 'active' | 'deprovisioning' | 'deprovisioned' | null
  enabled: boolean
  created_at: string | null
  owner: { id: string; login_name: string | null; display_name: string }
  relay: { id: string; name: string } | null
  node: { id: string; name: string; online: boolean } | null
  lifecycle: { action: string; step: string; trigger: string; error_code: string | null } | null
  /** 此刻是否在班：记录启用、开通好且在工作时段内（含「上班」「下班」的临时切换）。 */
  on_shift: boolean
  /** 还没有设置行的助理是 null。 */
  cold_start: {
    state: 'pending' | 'in_progress' | 'done'
    step: string | null
    outcome: string | null
    started_at: string | null
    done_at: string | null
  } | null
  directories_pending_removal: number
  watch: { state: string; last_ok_at: string | null; last_error: string | null } | null
  turns: { today: number; budget: number } | null
  inbox_pending: number
  jobs_active: number
}

const root = '/api/admin/assistants'
export const assistantsAdmin = {
  list: () => call<{ items: AssistantRow[] }>(http.get(root)),
  /** 停用不可撤销：调用前必须已经二次确认。 */
  deprovision: (botId: string) => call<{ bot_id: string; state: string }>(http.post(`${root}/${botId}/deprovision`, { confirm: true })),
}
```

`web/src/views/AssistantsView.vue`（新建）：

```vue
<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { assistantsAdmin, type AssistantRow } from '@/api/assistantsAdmin'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const rows = ref<AssistantRow[] | null>(null)
const error = ref(''), loading = ref(false)

async function load() {
  loading.value = true; error.value = ''
  try { rows.value = (await assistantsAdmin.list()).items }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
const ownerLabel = (row: AssistantRow) => (row.owner.login_name ? `${row.owner.display_name} (${row.owner.login_name})` : row.owner.display_name)
/** 停用确认要输入的那个名字：有登录名用登录名，没有就用显示名。 */
const confirmName = (row: AssistantRow) => row.owner.login_name ?? row.owner.display_name
/** 停用不可撤销：要原样输入所有者的登录名才发请求；取消确认框不算失败。 */
async function deprovision(row: AssistantRow) {
  const name = confirmName(row)
  try {
    await ElMessageBox.prompt(t('assistant.admin.deprovisionPrompt', { name }), t('assistant.admin.deprovision'), {
      type: 'warning',
      inputValidator: (input: string) => (input ?? '').trim() === name || t('assistant.admin.deprovisionMismatch'),
    })
    await assistantsAdmin.deprovision(row.bot_id)
    ElMessage.success(t('assistant.admin.deprovisionStarted'))
    await load()
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorMessage(e)) }
}
onMounted(load)
</script>
<template>
  <section class="assistants">
    <div class="cm-page-header">
      <h1>{{ t('menu.assistants') }}</h1>
      <p class="cm-page-intro">
        {{ t('assistant.admin.intro') }}
      </p>
      <el-button
        :loading="loading"
        @click="load"
      >
        {{ t('assistant.page.refresh') }}
      </el-button>
    </div>
    <LoadState
      :loading="loading && !rows"
      :error="error"
      @retry="load"
    />
    <el-empty
      v-if="rows && !rows.length"
      :description="t('assistant.admin.empty')"
    />
    <el-table
      v-if="rows && rows.length"
      :data="rows"
      row-key="bot_id"
    >
      <el-table-column :label="t('assistant.admin.owner')">
        <template #default="{ row }: { row: AssistantRow }">
          {{ ownerLabel(row) }}
        </template>
      </el-table-column>
      <el-table-column :label="t('assistant.admin.state')">
        <template #default="{ row }: { row: AssistantRow }">
          <el-tag :type="row.state === 'active' ? 'success' : 'info'">
            {{ t('assistant.overview.states.' + (row.state ?? 'unknown')) }}
          </el-tag>
          <div
            v-if="row.lifecycle"
            class="muted"
          >
            {{ t('assistant.admin.lifecycle') }}：{{ row.lifecycle.action }} / {{ row.lifecycle.step }}
            <span v-if="row.lifecycle.error_code">（{{ row.lifecycle.error_code }}）</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column :label="t('assistant.admin.runtime')">
        <template #default="{ row }: { row: AssistantRow }">
          <template v-if="row.node">
            {{ row.node.name }} · {{ row.node.online ? t('assistant.admin.online') : t('assistant.admin.offline') }}
          </template>
          <template v-else>
            {{ row.relay?.name ?? t('assistant.admin.none') }}
          </template>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('assistant.admin.shift')"
        width="80"
      >
        <template #default="{ row }: { row: AssistantRow }">
          {{ row.on_shift ? t('assistant.admin.yes') : t('assistant.admin.no') }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('assistant.admin.coldStart')"
        width="130"
      >
        <template #default="{ row }: { row: AssistantRow }">
          <template v-if="row.cold_start">
            {{ t('assistant.overview.coldStartStates.' + row.cold_start.state) }}
            <code v-if="row.cold_start.step">{{ row.cold_start.step }}</code>
            <code v-if="row.cold_start.outcome">{{ row.cold_start.outcome }}</code>
          </template>
          <template v-else>
            {{ t('assistant.admin.none') }}
          </template>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('assistant.admin.turns')"
        width="110"
      >
        <template #default="{ row }: { row: AssistantRow }">
          {{ row.turns ? t('assistant.overview.turnsValue', { n: row.turns.today, total: row.turns.budget }) : t('assistant.admin.none') }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('assistant.admin.inbox')"
        prop="inbox_pending"
        width="90"
      />
      <el-table-column
        :label="t('assistant.admin.jobs')"
        prop="jobs_active"
        width="130"
      />
      <el-table-column :label="t('assistant.admin.watch')">
        <template #default="{ row }: { row: AssistantRow }">
          <template v-if="row.watch">
            {{ t('assistant.overview.watchStates.' + row.watch.state) }}
            <code v-if="row.watch.last_error">{{ row.watch.last_error }}</code>
            <div
              v-if="row.watch.last_ok_at"
              class="muted"
            >
              {{ t('assistant.overview.watchLastOk', { time: formatDateTime(row.watch.last_ok_at) }) }}
            </div>
          </template>
          <template v-else>
            {{ t('assistant.admin.none') }}
          </template>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('assistant.admin.dirs')"
        prop="directories_pending_removal"
        width="100"
      />
      <el-table-column width="90">
        <template #default="{ row }: { row: AssistantRow }">
          <el-button
            v-if="row.state !== 'deprovisioned'"
            link
            type="danger"
            :data-test="'deprovision-' + row.bot_id"
            @click="deprovision(row)"
          >
            {{ t('assistant.admin.deprovision') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>
<style scoped>
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
</style>
```

`web/src/router/index.ts`：在 `path: 'runtime'` 那一条路由对象之后加：

```ts
        {
          path: 'assistants',
          name: 'assistants',
          component: () => import('@/views/AssistantsView.vue'),
          meta: { roles: ['platform_admin'] },
        },
```

`web/src/layouts/AdminLayout.vue`：

`MENU` 里在 `{ key: 'runtime', … },` 之后加：

```ts
  { key: 'assistants', path: '/assistants', roles: ['platform_admin'] },
```

图标导入的清单里加 `Avatar`；`icons` 对象里加 `assistants: Avatar,`。

`groups` 的第三项（`platform`）改为：

```ts
  { key: 'platform', keys: ['relays', 'runtime', 'assistants', 'users', 'apps', 'systems', 'credentials', 'settings'] },
```

- [ ] **Step 5: 运行确认通过**

Run（在 `web/` 下）: `npx vitest run tests/AssistantsView.spec.ts tests/AdminLayout.spec.ts tests/router.spec.ts`
Expected: PASS。

Run（在 `web/` 下）: `npm run lint && npm run build`
Expected: 没有 error；`vue-tsc` 与构建通过。

- [ ] **Step 6: 文档**

`docs/operations.md`：在「### 个人助理盯消息排障」之前加一小节：

```markdown
### 个人助理列表

管理台「平台管理 → 个人助理」（仅平台管理员）列出每一位助理，数据来自 `GET /api/admin/assistants`。这一页与这个接口只有元数据：看不到任何人的对话、知识库、待跟进、审批单内容、重点群。

| 列 | 接口里的键 | 含义 |
|---|---|---|
| 所有者 | `owner` | 显示名与登录名 |
| 状态 | `state`、`lifecycle` | `provisioning` 开通中、`active` 可用、`deprovisioning` 停用中、`deprovisioned` 已停用；流程进行中时另显示动作、步骤与错误代码 |
| 运行时 | `node`、`relay` | 所在节点的名字与是否在线 |
| 在班 | `on_shift` | 记录启用、开通好，且此刻在所有者的工作时段内（含「上班」「下班」的临时切换） |
| 冷启动 | `cold_start` | `pending` 没开始、`in_progress` 进行中（后面是走到哪一步）、`done` 已结束（后面是怎么结束的：`completed` / `skipped` / `timeout`）；没完成时不盯消息、不交回事项 |
| 今日轮次 | `turns` | 今天（所有者时区）跑了几轮 / 预算；预算用完后只交回紧急的 |
| 待交回 | `inbox_pending` | 收件箱里还没交回的条目数；一直不降见下一节 |
| 进行中的子任务 | `jobs_active` | 排队或在跑的子任务数，每位助理至多 3 个 |
| 轮询健康 | `watch` | 盯消息的状态、上次成功的时刻与错误代码，对照下一节的表 |
| 待删目录 | `directories_pending_removal` | 换运行时或停用后等原节点删除的旧工作目录数 |

「停用」要输入所有者的登录名确认。停用不可撤销：飞书授权撤销、待批的审批单撤回、数据密钥销毁，这位成员加密保存的个人内容从此不可恢复；接口只回状态。已经停用完的记录没有这个按钮。

可开通范围、允许的 AI 及其先后、每个节点的并发上限、每日轮次预算的默认值在「设置 → 个人助理」里改（设置项 `assistant_eligibility`、`assistant_backends`、`assistant_node_concurrency`、`assistant_daily_turn_budget`）。成员可以在「我的助理 → 设置」里把自己的每日预算调低，不能调到平台默认值以上。
```

- [ ] **Step 7: 提交**

```bash
git add web/src/api/assistantsAdmin.ts web/src/views/AssistantsView.vue web/src/router/index.ts \
  web/src/layouts/AdminLayout.vue web/tests/AssistantsView.spec.ts web/tests/AdminLayout.spec.ts \
  docs/operations.md
git commit -m "管理台的个人助理列表：谁开通了、在哪跑、是否在班与健康，只有元数据；停用要输入登录名确认"
```

---

### Task 14: 平台设置页的助理设置项表单，CHANGELOG 与 spec 同步，整体验证

**Files:**
- Modify: `web/src/api/types.ts`（`SettingsOut` 之前加类型；`SettingsOut` 的声明）
- Create: `web/src/components/AssistantPlatformSettings.vue`
- Modify: `web/src/views/SettingsView.vue`（导入；分区导航；「通知与告警」标题之前）
- Modify: `web/tests/SettingsView.spec.ts`（替身）
- Modify: `CHANGELOG.md`、`docs/superpowers/specs/2026-11-11-personal-assistant.md`、`docs/superpowers/plans/2026-11-13-personal-assistant-phase2-3.md`
- Test: `web/tests/AssistantPlatformSettings.spec.ts`（新建）

**Interfaces:**
- Consumes: `@/api/admin` 的 `settings.get / update`、`departments.tree(platform)`、`users.get(id)`；`@/components/UserPicker.vue`（`modelValue: string[]`、`initial`）；后端已有的四个设置项与校验（`assistant_eligibility`、`assistant_backends`（1–2 项，`claude` / `codex`）、`assistant_node_concurrency`（1–32）、`assistant_daily_turn_budget`（10–2000））；文案 `assistant.platform.*`。
- Produces：
  - `@/api/types`：`interface AssistantEligibility`、`type AssistantBackend`、`interface AssistantPlatformSettings`；`SettingsOut` 另继承 `Partial<AssistantPlatformSettings>`（旧后端不带这些键时表单用默认值）。
  - 组件 `AssistantPlatformSettings`（无属性，自己取数与保存，只提交改过的键）；`defineExpose({ form, save })`，`form: { mode: 'all' | 'listed'; user_ids: string[]; department_ids: string[]; primary: AssistantBackend; also_other: boolean; concurrency: number; budget: number }`。测试钩子 `data-test="save-assistant-platform"`、`listed-empty`。
  - 平台设置页多一个分区「个人助理」（锚点 `#settings-assistant`）。

- [ ] **Step 1: 写失败测试**

`web/tests/AssistantPlatformSettings.spec.ts`（新建）：

```ts
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/admin', () => ({
  settings: { get: vi.fn(), update: vi.fn() },
  departments: { tree: vi.fn() },
  users: { get: vi.fn(), list: vi.fn() },
}))

import { departments, settings, users } from '@/api/admin'
import type { AssistantBackend } from '@/api/types'
import AssistantPlatformSettings from '@/components/AssistantPlatformSettings.vue'
import { i18n } from '@/i18n'

const t = i18n.global.t
const U1 = '11111111-1111-4111-8111-111111111111', D1 = '22222222-2222-4222-8222-222222222222'
// 键的顺序故意与表单里的不同：settings 表是 JSONB，读回来的键序不保证。
const server = {
  assistant_eligibility: { user_ids: [U1], mode: 'listed', department_ids: [] },
  assistant_backends: ['codex'],
  assistant_node_concurrency: 2,
  assistant_daily_turn_budget: 200,
}
interface Form {
  mode: 'all' | 'listed'; user_ids: string[]; department_ids: string[]
  primary: AssistantBackend; also_other: boolean; concurrency: number; budget: number
}
const render = () => mount(AssistantPlatformSettings, { global: { plugins: [ElementPlus, i18n] } })
const form = (wrapper: ReturnType<typeof render>) => (wrapper.vm as unknown as { form: Form }).form
const save = async (wrapper: ReturnType<typeof render>) => { await wrapper.get('[data-test="save-assistant-platform"]').trigger('click'); await flushPromises() }

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(settings.get).mockReset().mockResolvedValue({ ...server } as never)
  vi.mocked(settings.update).mockReset().mockImplementation(async (body) => ({ ...server, ...body }) as never)
  vi.mocked(departments.tree).mockReset().mockResolvedValue([{ id: D1, platform_dept_id: '1', name: '销售部', path: '/销售部', sort_order: 1, member_count: 3, children: [] }])
  vi.mocked(users.get).mockReset().mockResolvedValue({ id: U1, login_name: 'zhangsan', display_name: '张三' } as never)
  vi.mocked(users.list).mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 20 } as never)
  vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
})

describe('assistant platform settings', () => {
  it('loads the four settings and the names of listed people', async () => {
    const wrapper = render(); await flushPromises()
    expect(form(wrapper)).toEqual({ mode: 'listed', user_ids: [U1], department_ids: [], primary: 'codex', also_other: false, concurrency: 2, budget: 200 })
    expect(departments.tree).toHaveBeenCalledWith('feishu')
    expect(users.get).toHaveBeenCalledWith(U1)
    expect(wrapper.text()).toContain(t('assistant.platform.title'))
    wrapper.unmount()
  })

  it('does not call the API when nothing changed, whatever the stored key order', async () => {
    const info = vi.spyOn(ElMessage, 'info').mockReturnValue({ close: () => {} })
    const wrapper = render(); await flushPromises()
    await save(wrapper)
    expect(settings.update).not.toHaveBeenCalled()
    expect(info).toHaveBeenCalledWith(t('assistant.platform.nothingChanged'))
    wrapper.unmount()
  })

  it('submits only the changed setting: who may set one up', async () => {
    const wrapper = render(); await flushPromises()
    form(wrapper).department_ids = [D1]
    await save(wrapper)
    expect(settings.update).toHaveBeenCalledWith({ assistant_eligibility: { mode: 'listed', department_ids: [D1], user_ids: [U1] } })
    form(wrapper).mode = 'all'
    await save(wrapper)
    expect(settings.update).toHaveBeenLastCalledWith({ assistant_eligibility: { mode: 'all', department_ids: [D1], user_ids: [U1] } })
    wrapper.unmount()
  })

  it('submits the allowed AI in order of preference', async () => {
    const wrapper = render(); await flushPromises()
    form(wrapper).also_other = true
    await save(wrapper)
    expect(settings.update).toHaveBeenLastCalledWith({ assistant_backends: ['codex', 'claude'] })
    form(wrapper).primary = 'claude'
    await save(wrapper)
    expect(settings.update).toHaveBeenLastCalledWith({ assistant_backends: ['claude', 'codex'] })
    wrapper.unmount()
  })

  it('submits concurrency and the default budget', async () => {
    const wrapper = render(); await flushPromises()
    Object.assign(form(wrapper), { concurrency: 4, budget: 120 })
    await save(wrapper)
    expect(settings.update).toHaveBeenCalledWith({ assistant_node_concurrency: 4, assistant_daily_turn_budget: 120 })
    wrapper.unmount()
  })

  it('warns when the list is selected but empty', async () => {
    const wrapper = render(); await flushPromises()
    expect(wrapper.find('[data-test="listed-empty"]').exists()).toBe(false)
    form(wrapper).user_ids = []
    await flushPromises()
    expect(wrapper.get('[data-test="listed-empty"]').text()).toContain(t('assistant.platform.listedEmpty'))
    wrapper.unmount()
  })

  it('falls back to defaults when the backend does not send the keys, and shows a refusal', async () => {
    const error = vi.spyOn(ElMessage, 'error').mockReturnValue({ close: () => {} })
    vi.mocked(settings.get).mockResolvedValue({} as never)
    vi.mocked(settings.update).mockRejectedValueOnce(new Error('设置项不能置空'))
    const wrapper = render(); await flushPromises()
    expect(form(wrapper)).toEqual({ mode: 'all', user_ids: [], department_ids: [], primary: 'claude', also_other: true, concurrency: 2, budget: 200 })
    form(wrapper).budget = 300
    await save(wrapper)
    expect(error).toHaveBeenCalledWith('设置项不能置空')
    wrapper.unmount()
  })
})
```

- [ ] **Step 2: 运行确认失败**

Run（在 `web/` 下）: `npx vitest run tests/AssistantPlatformSettings.spec.ts`
Expected: FAIL：找不到 `@/components/AssistantPlatformSettings.vue`。

- [ ] **Step 3: 类型**

`web/src/api/types.ts`：在 `export interface SettingsOut extends PromptSettings {` 之前加：

```ts
/** 个人助理的开通范围：`listed` 时只有名单里的人与部门（含下级部门）能开通。 */
export interface AssistantEligibility { mode: 'all' | 'listed'; department_ids: string[]; user_ids: string[] }
export type AssistantBackend = 'claude' | 'codex'
/** 平台设置里个人助理的四项（`coreman/core/settings_schema.py`）。 */
export interface AssistantPlatformSettings {
  assistant_eligibility: AssistantEligibility
  /** 开通时按顺序挑第一个助理池里有可用节点的。 */
  assistant_backends: AssistantBackend[]
  /** 每个运行时节点同时跑的助理轮次上限，1–32。 */
  assistant_node_concurrency: number
  /** 每位助理每天的轮次预算默认值，10–2000。 */
  assistant_daily_turn_budget: number
}
```

并把 `SettingsOut` 的声明行改为：

```ts
export interface SettingsOut extends PromptSettings, Partial<AssistantPlatformSettings> {
```

（`SettingsPatch` 是 `Partial<Omit<SettingsOut, 'default_model'>>`，随之带上这四个键；`SettingsView.vue` 的 `empty()` 与 `KEYS` 不用改——这四项由新组件自己读写。）

- [ ] **Step 4: 实现表单组件**

`web/src/components/AssistantPlatformSettings.vue`（新建）：

```vue
<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { departments, settings, users } from '@/api/admin'
import type { AssistantBackend, AssistantPlatformSettings, DeptNode, SettingsPatch } from '@/api/types'
import UserPicker from '@/components/UserPicker.vue'
import { errorMessage } from '@/utils/errors'

interface Form {
  mode: 'all' | 'listed'
  user_ids: string[]
  department_ids: string[]
  /** 优先用的那种 AI；`also_other` 为真时另一种排在它后面。 */
  primary: AssistantBackend
  also_other: boolean
  concurrency: number
  budget: number
}
// 与 coreman/core/settings_schema.py::SETTING_DEFAULTS 一致：后端没带这些键时用。
const DEFAULTS: AssistantPlatformSettings = {
  assistant_eligibility: { mode: 'all', department_ids: [], user_ids: [] },
  assistant_backends: ['claude', 'codex'],
  assistant_node_concurrency: 2,
  assistant_daily_turn_budget: 200,
}
// 已选的人只查这么多个名字：名单再长，剩下的在选择框里显示编号。
const NAME_LOOKUPS = 50

const { t } = useI18n()
const form = reactive<Form>({ mode: 'all', user_ids: [], department_ids: [], primary: 'claude', also_other: true, concurrency: 2, budget: 200 })
/** 上一次从后端拿到的值（按表单的形状重排过键序）：save() 靠它算出改了哪几项。 */
let original: AssistantPlatformSettings = DEFAULTS
const loaded = ref(false), saving = ref(false)
const tree = ref<DeptNode[]>([])
const knownUsers = ref<{ id: string; display_name: string; login_name: string | null }[]>([])
const other = computed<AssistantBackend>(() => (form.primary === 'claude' ? 'codex' : 'claude'))
const listedEmpty = computed(() => form.mode === 'listed' && !form.user_ids.length && !form.department_ids.length)

function current(): AssistantPlatformSettings {
  return {
    assistant_eligibility: { mode: form.mode, department_ids: [...form.department_ids], user_ids: [...form.user_ids] },
    assistant_backends: form.also_other ? [form.primary, other.value] : [form.primary],
    assistant_node_concurrency: form.concurrency,
    assistant_daily_turn_budget: form.budget,
  }
}
function apply(data: Partial<AssistantPlatformSettings>) {
  const rule = data.assistant_eligibility ?? DEFAULTS.assistant_eligibility
  const backends = data.assistant_backends?.length ? data.assistant_backends : DEFAULTS.assistant_backends
  Object.assign(form, {
    mode: rule.mode,
    user_ids: [...rule.user_ids],
    department_ids: [...rule.department_ids],
    primary: backends[0],
    also_other: backends.length > 1,
    concurrency: data.assistant_node_concurrency ?? DEFAULTS.assistant_node_concurrency,
    budget: data.assistant_daily_turn_budget ?? DEFAULTS.assistant_daily_turn_budget,
  })
  // settings 表是 JSONB，读回来的键序不保证：按表单的形状重新拼一遍，之后才能逐项比较。
  original = current()
}
function changes(): SettingsPatch {
  const next = current()
  const out: Record<string, unknown> = {}
  for (const key of Object.keys(next) as (keyof AssistantPlatformSettings)[]) {
    if (JSON.stringify(next[key]) !== JSON.stringify(original[key])) out[key] = next[key]
  }
  return out as SettingsPatch
}
async function save() {
  if (saving.value) return
  const body = changes()
  if (!Object.keys(body).length) { ElMessage.info(t('assistant.platform.nothingChanged')); return }
  saving.value = true
  try { apply(await settings.update(body)); ElMessage.success(t('assistant.platform.saved')) }
  catch (e) { ElMessage.error(errorMessage(e)) }
  finally { saving.value = false }
}
/** el-input-number 清空时发 undefined：保持原值不动。 */
function setNumber(key: 'concurrency' | 'budget', value: number | null | undefined) {
  if (value !== null && value !== undefined) form[key] = value
}
onMounted(async () => {
  try { apply(await settings.get()); loaded.value = true }
  catch (e) { ElMessage.error(errorMessage(e)); return }
  // 部门树与已选用户的名字只影响显示：取不到时表单照常可用。
  departments.tree('feishu').then((nodes) => { tree.value = nodes }).catch(() => { tree.value = [] })
  const found = await Promise.allSettled(form.user_ids.slice(0, NAME_LOOKUPS).map((id) => users.get(id)))
  knownUsers.value = found.flatMap((result) => (result.status === 'fulfilled'
    ? [{ id: result.value.id, display_name: result.value.display_name, login_name: result.value.login_name }]
    : []))
})
defineExpose({ form, save })
</script>
<template>
  <el-card class="assistant-platform-settings">
    <template #header>
      {{ t('assistant.platform.title') }}
    </template>
    <p class="hint">
      {{ t('assistant.platform.hint') }}
    </p>
    <el-form
      label-position="top"
      :disabled="!loaded"
      @submit.prevent
    >
      <el-form-item :label="t('assistant.platform.eligibility')">
        <el-radio-group v-model="form.mode">
          <el-radio value="all">
            {{ t('assistant.platform.modeAll') }}
          </el-radio>
          <el-radio value="listed">
            {{ t('assistant.platform.modeListed') }}
          </el-radio>
        </el-radio-group>
      </el-form-item>
      <template v-if="form.mode === 'listed'">
        <el-form-item :label="t('assistant.platform.users')">
          <UserPicker
            v-model="form.user_ids"
            :initial="knownUsers"
          />
        </el-form-item>
        <el-form-item :label="t('assistant.platform.departments')">
          <el-tree-select
            v-model="form.department_ids"
            :data="tree"
            :props="{ label: 'name', children: 'children' }"
            node-key="id"
            multiple
            show-checkbox
            check-strictly
            filterable
            style="width: 100%"
          />
        </el-form-item>
        <el-alert
          v-if="listedEmpty"
          type="warning"
          :closable="false"
          show-icon
          data-test="listed-empty"
          :title="t('assistant.platform.listedEmpty')"
        />
      </template>
      <el-form-item :label="t('assistant.platform.backends')">
        <span class="inline-label">{{ t('assistant.platform.backendFirst') }}</span>
        <el-radio-group v-model="form.primary">
          <el-radio value="claude">
            {{ t('assistant.platform.backendNames.claude') }}
          </el-radio>
          <el-radio value="codex">
            {{ t('assistant.platform.backendNames.codex') }}
          </el-radio>
        </el-radio-group>
        <el-checkbox
          v-model="form.also_other"
          class="also-other"
        >
          {{ t('assistant.platform.backendAlsoOther') }}
        </el-checkbox>
      </el-form-item>
      <el-form-item :label="t('assistant.platform.concurrency')">
        <el-input-number
          :model-value="form.concurrency"
          :min="1"
          :max="32"
          @update:model-value="setNumber('concurrency', $event)"
        />
        <div class="hint">
          {{ t('assistant.platform.concurrencyHint') }}
        </div>
      </el-form-item>
      <el-form-item :label="t('assistant.platform.budget')">
        <el-input-number
          :model-value="form.budget"
          :min="10"
          :max="2000"
          @update:model-value="setNumber('budget', $event)"
        />
        <div class="hint">
          {{ t('assistant.platform.budgetHint') }}
        </div>
      </el-form-item>
      <el-button
        type="primary"
        :loading="saving"
        data-test="save-assistant-platform"
        @click="save"
      >
        {{ t('assistant.platform.save') }}
      </el-button>
    </el-form>
  </el-card>
</template>
<style scoped>
.assistant-platform-settings { max-width: 720px; margin-bottom: 18px; }
.hint { width: 100%; color: var(--el-text-color-secondary); font-size: 12px; }
.inline-label { margin-right: 12px; color: var(--el-text-color-secondary); }
.also-other { margin-left: 24px; }
</style>
```

实现时留意：最后一条用例里表单的预算被直接设成 300（超出输入框的上限，输入框本身不会产生这个值），用来让请求发出去、验证后端的拒绝原话会显示出来；真正的范围校验在后端的 `SettingsPatch`。

- [ ] **Step 5: 嵌进平台设置页**

`web/src/views/SettingsView.vue`：

导入区，在 `import AlertSettings from '@/components/AlertSettings.vue'` 之后加：

```ts
import AssistantPlatformSettings from '@/components/AssistantPlatformSettings.vue'
```

分区导航里，在 `<a href="#settings-prompts">{{ t('workspace.prompts') }}</a>` 之后加 `<a href="#settings-assistant">{{ t('assistant.platform.title') }}</a>`。

模板里，在

```html
    <h3
      id="settings-notifications"
      class="cm-section-title"
    >
```

之前加：

```html
    <h3
      id="settings-assistant"
      class="cm-section-title"
    >
      {{ t('assistant.platform.title') }}
    </h3>
    <AssistantPlatformSettings />
```

`web/tests/SettingsView.spec.ts`：在 `vi.mock('@/components/NotificationSettings.vue', …)` 之后加一行（这个文件测的是通用设置表单，助理的表单有自己的用例）：

```ts
vi.mock('@/components/AssistantPlatformSettings.vue', () => ({ default: { template: '<div />' } }))
```

- [ ] **Step 6: 运行确认通过**

Run（在 `web/` 下）: `npx vitest run tests/AssistantPlatformSettings.spec.ts tests/SettingsView.spec.ts`
Expected: PASS。

- [ ] **Step 7: CHANGELOG、spec、总览**

`CHANGELOG.md`：`## [Unreleased]` → `### Added` 的第一条之前加：

```markdown
- Personal assistants have a page of their own and an admin list. "My assistant" (under AI collaboration, visible to every signed-in user) shows the owner their assistant's state, runtime, shift, cold start, today's turns against the budget and message-watch health, and six sections: what needs the owner (pending approvals with a link to the approval system taken from `TOWER_WEB_URL`, blocked sub-tasks, follow-ups due today), running and recently finished sub-tasks with the original brief (running ones can be cancelled and the assistant is told), follow-ups in four categories (tick to finish, snooze, recategorise, drop, add), recently settled approvals, the knowledge base (index, read, edit, create, delete; pinned pages are clearly marked, and this page is the only place where pinned pages can be pinned, unpinned or rewritten, each page showing whether the owner, the assistant or an import changed it last), and settings (time zone, working hours and days, focus and ignored chats, a daily turn budget that can only be lowered below the platform default, and shutdown, which requires typing the assistant's name). Someone without an assistant sees how to get one, or why they cannot. The page is backed by `/api/me/assistant`, which only serves the interactively signed-in owner: there is no assistant id in any path, bot tokens are refused, writes need the CSRF header, responses are `no-store`, and administrators calling it get their own assistant or none. After the owner's data key is destroyed only numbers, states and times remain. Every read of personal content through the page writes an `assistant_content_access` log line without content. Platform administrators get "Personal assistants" (under Platform management): owner, state, runtime, shift, cold start, today's turns, items waiting to be handed over, sub-tasks in progress, watch health and directories pending removal, with a shutdown button that requires typing the owner's login name; it shows metadata only. The settings page gains a "Personal assistants" form for who may set one up, the allowed AI and their order, per-node concurrency and the default daily turn budget. No database migration; runtime nodes need no upgrade; ordinary employees are not affected. See `docs/feishu.md` and `docs/operations.md`.
```

`docs/superpowers/specs/2026-11-11-personal-assistant.md`：

第九节的表格换成：

```markdown
| 分区 | 内容 |
|---|---|
| 等你出手 | 待批的审批单（只给摘要，跳转 Tower：入口取自部署配置 `TOWER_WEB_URL`，没配就不给链接）、被卡住的子任务、今天到期的待跟进 |
| 正在跑 | 子任务与调度出去的求助，含问题原文与进度；进行中的可以取消 |
| 待跟进 | 四类清单；可以勾选办完、推后、改分类、标为不再需要、新增 |
| 最近落地 | 已执行、被拒、撤回的审批单 |
| 知识库 | 索引、查看、编辑、新建、删除；置顶页醒目标出。所有者只能在这里置顶、取消置顶或改写置顶页，每页写明最后由所有者、助理还是导入修改 |
| 设置 | 时区、工作时段与工作日、重点群与忽略的会话（填 `chat_id`）、每日轮次预算（只能调低，不超过平台默认值）、所在 Runtime（只读）、停用（输入助理名确认） |
```

表格之后加一段：

```markdown
没有助理的人看到开通指引或不在范围的原因。接口没有任何助理编号参数，助理一律由登录人推出；管理员调用同样只能拿到自己的。数据密钥销毁后页面只显示编号、状态与时间。经页面读取个人内容的每个接口都调用 `access_log.record`：在访问日志表落地之前它只写一条不含内容的应用日志（`assistant_content_access`）。
```

第十一节「管理员能看到的只有：…」那一条换成：

```markdown
- 管理员能看到的只有：谁开通了、状态与流程步骤、所在节点、是否在班、冷启动是否完成、轮次与额度用量、待交回的条目数、进行中的子任务数、轮询健康状态、待删的旧目录数（管理台「个人助理」列表）。停用要输入所有者的登录名确认。
```

`docs/superpowers/plans/2026-11-13-personal-assistant-phase2-3.md`：把「### 3B、3C、3D」一节里关于 3C 的那句换成：

```markdown
3C 的接口前缀 `/api/me/assistant`，鉴权照 `feishu_personal.interactive_user`，接口清单与各键见 `2026-11-13-personal-assistant-3c-pages.md`「接口一览」。给 3D 的交接：

- `coreman/core/assistants/access_log.py` 的 `record(session, bot_id, actor_id, action, target)` 是页面读取密文的唯一记账点，现在只写日志 `assistant_content_access`；3D 把函数体换成写 `assistant_access_log`，调用点与 `ACTIONS` 不动，新增动作（导出、导入）加进 `ACTIONS`。`target` 只放明文的编号或范围名，知识页的路径不进 `target`。
- 所有者查看自己的访问记录：在 `coreman/api/routers/me_assistant.py` 里加接口，并登记进 `tests/api/test_my_assistant_guard.py` 的 `CASES`（不登记，台账用例失败）。
- 导入接口 `/api/me/assistant/import` 同样用 `me_assistant_common` 的 `mine` / `writable` 与路由级依赖；写知识页用 `notes.write(..., updated_by="import", owner_present=True)`，页面会显示「导入」。
- 所有者的每日轮次预算由 `owner_view.set_budget(session, bot_id, value)` 写（不得高于平台默认值）；其余设置项仍是 2F 的 `settings.update`。
```

- [ ] **Step 8: 整体验证**

Run: `uv run pytest tests/api -q -k "my_assistant or assistant_admin or assistant_settings"`
Expected: PASS。

Run: `uv run pytest tests/unit -q -k "assistant"`
Expected: PASS（含台账与访问记录）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman && uv run alembic check`
Expected: 全部通过；没有待生成的迁移。

Run（在 `web/` 下）: `npx vitest run && npm run lint && npm run build`
Expected: 全部用例 PASS；lint 没有 error；构建通过。

Run（仓库约定的敏感词自查，按协作约定里的那条命令对 `git diff --cached` 执行）
Expected: 没有输出。

手工走一遍（本机栈，按 `docs/development.md` 起后端与 `npm run dev`）：

1. 以一个没有助理的成员登录，打开「我的助理」：看到开通指引；把可开通范围改成「指定的人与部门」且名单为空后刷新：看到「不在开通范围」。
2. 以有助理的所有者登录：六个标签页都能打开；在「待跟进」新增一条、改分类、推后、勾选办完；在「知识库」新建一页并置顶、编辑、删除；在「设置」把下班时间改到上班时间之前：保存被拦下并在该项下显示原因。
3. 以平台管理员登录：打开「我的助理」看到的是自己的（或空状态）；「个人助理」列表里有那位所有者的一行，且页面与接口响应里都搜不到他的任何内容。
4. 浏览器开发者工具里看任一 `/api/me/assistant` 响应头：`Cache-Control: no-store`。

- [ ] **Step 9: 提交**

```bash
git add web/src/api/types.ts web/src/components/AssistantPlatformSettings.vue \
  web/src/views/SettingsView.vue web/tests/AssistantPlatformSettings.spec.ts \
  web/tests/SettingsView.spec.ts CHANGELOG.md \
  docs/superpowers/specs/2026-11-11-personal-assistant.md \
  docs/superpowers/plans/2026-11-13-personal-assistant-phase2-3.md
git commit -m "平台设置页加个人助理的设置项表单；CHANGELOG、spec 与总览同步"
```

---

## 没覆盖到的地方

写在这里的都是本层有意不做、或需要所有者拍板的事，不是待办的占位。

- **知识页的路径出现在查询串里。** `GET` / `DELETE /api/me/assistant/notes/page?path=…` 是任务书定的接口形状。路径（如「人/某人」）本身是个人内容，会进反向代理与应用的访问日志，而本层别处都在避免个人内容进日志（访问记录的 `target` 就不带路径）。要彻底避免，需要把读与删改成请求体传路径（如 `POST /notes/read`、`POST /notes/delete`），或让索引返回一个不透明的页编号再按编号读。上线前请所有者确认是否接受现状。
- **审批单没有深链。** 「等你出手」的链接是审批系统的网页入口（`TOWER_WEB_URL`），不是某一张单的地址：Tower 没有约定「按编号打开」的地址格式。所有者进去后要自己在列表里找。
- **按群名挑重点群。** 设置里只能填 `chat_id`。要按群名挑，需要一个以所有者身份列会话的接口，它会占 2D 的分钟预算并要处理限频；本层没做，冷启动（2F）的选择卡是主要入口。
- **个人预算高于新的平台默认值。** 管理员把 `assistant_daily_turn_budget` 调到某人自己设的值以下时，那个人的值不会被收紧：`delivery.budget_left` 以个人值为准（`coreman/core/assistants/delivery.py`）。页面此后不允许他保存一个更高的值，但已有的值继续生效。要收紧，应在 `delivery.budget_left` 与 `delivery.usage` 里取 `min(个人值, 平台默认)`，那是 2E 的代码，本层没有动。
- **失败、过期、已过时的审批单不在页面上。** 「最近落地」按 spec 只列已执行、被拒、撤回；`failed` / `expired` / `stale` 的单只会作为结果回合回到助理会话。所有者想在页面上看到它们，需要给 `owner_view.SETTLED` 加状态并补文案。
- **换运行时的入口。** spec 第九节的设置里列了 Runtime；本层只读显示所在运行时，切换仍走现有的换运行时接口，没有在这一页放入口。
- **调度出去的求助（3B）。** `assistant_jobs.kind = 'bot_help'` 的行会随 `jobs.list_` 自然出现在「正在跑」里，但没有显示目标员工是谁；3B 落地后在 `jobs.view` 里补一个明文的目标员工名，再在 `AssistantJobs.vue` 里显示。
- **限流。** 仓库没有通用的接口限流依赖，本层没有加。这些接口都要求交互登录且只读写登录人自己的数据，滥用面是「一个登录用户刷自己的接口」；其中 `GET /notes`（解密全部页面的路径、标题、摘要）最重，上限 2000 页。
- **所有者改设置不记审计。** 改的是他自己的个人设置；停用记审计（`assistant.deprovision`）。
- **2F 留给本层、本层没有做的两个入口。** 页面上「上班」「下班」的即时切换（`settings.clock_in` / `clock_out`，还要照 `assistant_shift._switch` 的做法写 `worked_on`），与冷启动之外「重新生成口吻页」的入口（`voice.collect` + `voice.enqueue`）。任务书的范围里没有这两项；所有者现在仍可在私聊里发「上班」「下班」，口吻页可以在「知识库」分区里手工改。要做的话各是一个 `POST` 接口加一个按钮，并登记进 `tests/api/test_my_assistant_guard.py` 的 `CASES`。
- **概览里的「在班」与管理台列表的口径略有不同。** 所有者概览直接给 `settings.on_shift`（按工作时段与临时切换）；管理台列表的 `on_shift` 还要求记录启用且开通好。停用中的助理在所有者页面上可能显示「在班」，状态一栏同时显示「停用中」。

## Self-Review

**Spec 与任务书的覆盖**

| 要求 | 落在哪 |
|---|---|
| `GET /api/me/assistant` 概览与空状态（含开通指引） | Task 1 |
| `GET /attention`（五个键的审批单、卡住的子任务、今天到期的待跟进；链接取 `TOWER_WEB_URL`） | Task 5 |
| `GET /jobs`、`POST /jobs/{id}/cancel` | Task 4 |
| 待跟进的读与增改关（`updated_by` 不加） | Task 2 |
| `GET /approvals/recent` | Task 5 |
| 知识页索引、读、写、删；置顶页；`updated_by="owner"` | Task 3 |
| `GET` / `PATCH /settings`（时区、工作时段、重点群、忽略的会话、每日预算不超过平台上限；校验用 2F 的 `settings.update`） | Task 6 |
| `POST /deprovision`（`confirm` 严格为真，复用现有入口） | Task 7 |
| 只对所有者还原、管理员拿不到别人的、机器人令牌被拒、CSRF、`no-store` | Task 1 的公共件；每个任务的用例；Task 7 的全前缀台账 |
| 访问日志的调用点与最小实现 | Task 1（`access_log.py`），Task 2–6 各读取接口 |
| 密钥销毁后的表现 | Task 1（概览）、2、3、4、5、6 各一条用例 |
| 「我的助理」页：空状态、五个分区加知识库、三种语言 | Task 8（文案与三个只读分区）、9、10、11、12 |
| 待跟进在页面上勾选关闭、推后、改分类 | Task 9 |
| 设置表单带校验；停用二次确认 | Task 11 |
| 管理台助理列表（元数据、停用二次确认；后端字段前面各层已给全，本层只做页面） | Task 13 |
| 平台设置页的助理设置项表单 | Task 14 |
| `docs/feishu.md`、`docs/operations.md`、CHANGELOG | Task 12、13、14 |
| 后端必须有的测试（别人拿不到、机器人令牌、CSRF、空状态、密钥销毁、置顶页 `updated_by`、设置的每条校验、`confirm` 严格为真） | Task 1–7；设置的每条规则在 `tests/api/test_my_assistant_settings.py::test_every_rule_is_enforced` 里各有一例 |
| 前端必须有的测试（空状态、各分区渲染、待跟进操作调对接口、设置校验、停用二次确认、三种语言键齐全） | Task 12、8、9、11、11、8（`assistantI18n.spec.ts` 加已有的 `i18n.spec.ts`） |

**占位符扫描**：全文没有待定项；每个代码步骤都给了完整代码；对尚未落地的层的依赖都写成了确切的函数签名并在「对已有代码的假定」里列出，Task 4 的 Step 2 写明了它们没落地时的表现与处理。

**名称与签名的一致性**

- 路由公共件：`PREFIX`、`DEPENDENCIES`、`Mine(actor, bot)`、`mine`、`writable`、`refuse(code, table)`（Task 1 定义；Task 2–7 使用）。
- `access_log.record(session, bot_id, actor_id, action, target="")`；用到的动作 `followups.read`、`notes.index`、`notes.read`、`jobs.read`、`approvals.read`、`settings.read` 都在 `ACTIONS` 里。
- `owner_view`：`current`、`overview`、`absent`、`approval_link`（Task 1）；`PENDING`、`SETTLED`、`approvals`、`pending_view`、`settled_view`（Task 5）。
- 设置：`owner_view.preferences(session, row, now)`、`owner_view.set_budget(session, bot_id, value)`、`me_assistant.SettingsIn`、`me_assistant.SETTINGS_ERRORS`（Task 6）；核心校验是 2F 的 `settings.update` / `settings.SettingsError`。
- `assistant_deprovision.begin(session, request, bot, *, trigger, actor) -> str`（Task 7，两处调用）。
- `me_assistant.py` 里的常量：`JOB_LIMIT`、`JOB_ERRORS`（Task 4）、`SETTINGS_ERRORS`（Task 6）；Task 5 的 `attention` 用到 Task 4 的 `JOB_LIMIT`。
- 前端：`myAssistant` 的方法名在 Task 8 定义，Task 8–12 的组件与用例使用同样的名字（`overview`、`attention`、`jobs`、`cancelJob`、`followups`、`addFollowup`、`updateFollowup`、`closeFollowup`、`snoozeFollowup`、`recentApprovals`、`notes`、`page`、`savePage`、`deletePage`、`settings`、`saveSettings`、`deprovision`）；`assistantsAdmin.list` / `deprovision`（Task 13）。
- 文案键：组件里用到的键都在 Task 8 的 `assistantZh` 里（`page`、`empty`、`overview`、`attention`、`jobs`、`followups`、`recent`、`notes`、`settings`、`admin`、`platform`）；`menu.myAssistant`、`menu.assistants` 在 Task 8 加进三种语言。
- 台账：`tests/unit/test_assistant_sealed_ledger.py` 的 `ENTITY_SELECTS` 在 Task 5 加一项（`owner_view.py`: 1）；`tests/api/test_my_assistant_guard.py` 的 `CASES` 与「接口一览」的 17 条一一对应。
