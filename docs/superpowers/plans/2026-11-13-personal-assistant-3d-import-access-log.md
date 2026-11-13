# 个人助理 3D：导入、导出与访问日志 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 运行时路径之外对个人助理内容的每一次解密都落一行不含内容的访问记录，所有者能在「我的助理」页上查看；所有者本人可以把一个 Markdown 压缩包导入知识库与待跟进清单（先预览、后确认），也可以把它们按同样的格式导出。

**Architecture:** 新表 `assistant_access_log`（迁移 `0102`）与核心模块 `access_log`：3C 留下的 `record(...)` 调用点不动，函数体换成在调用方的事务里、用保存点写一行；读取接口在返回前提交，所以「提交不了就不交出内容」，而写这一行本身失败只回滚到保存点、不挡主操作。导入导出拆成两层：`archive`（纯函数：zip 的安全检查、front matter、`followups.json`，只在内存里读）与 `transfer`（对着库出清单、写入、打包），路由在 `/api/me/assistant` 下，只对在管理台里登录的所有者本人开放——管理命令以成员身份建的会话整段拒绝。一张按语法树扫描的台账守着「解密就要记」。

**Tech Stack:** Python 3.12、FastAPI（`UploadFile` / `Form`，已有依赖 `python-multipart`）、SQLAlchemy async、PostgreSQL、Alembic、标准库 `zipfile`、已有依赖 PyYAML（`yaml.safe_load` / `safe_dump`）、pytest（`httpx.AsyncClient`、`structlog.testing.capture_logs`）；Vue 3（`<script setup>`）、Element Plus、vue-i18n、axios、vitest + `@vue/test-utils` + jsdom。不引入新依赖。

**Spec:** `docs/superpowers/specs/2026-11-11-personal-assistant.md`（第十节「导入」、第十一节「管理员控制与审计」、威胁模型表、第二节「加密与存储」、第六节）；分层总览 `docs/superpowers/plans/2026-11-13-personal-assistant-phase2-3.md`（裁定 10、「每层都要守的约束」）；前置层的计划 `2026-11-13-personal-assistant-3c-pages.md`（Task 1 的 `access_log.py` 与各读取接口的调用点）。

## Global Constraints

- 集成分支 `claude/personal-assistant-phase2-3`。开工前确认 3C 已在这条分支上（见「对已有代码与 3C 的假定」，逐条核对，对不上的先停下来改计划）。
- 迁移 `0102_assistant_access_log`，`down_revision = "0101"`。任务书写的是 `0101`（3B 没有迁移），但写这份计划期间分支上落了 `0101_assistant_hardening`（盯消息游标改为封存列、审批单的改稿时间），所以顺延一号。开工时先 `ls migrations/versions | tail -3`：若又有新的迁移占了 `0102`，文件名、`revision` 与 `down_revision` 再顺延，文档里提到编号的地方一并改。每个后端任务结束时 `uv run alembic check` 必须干净。
- 公开仓：代码、测试、文档、提交信息只用通用占位（`example.com`、`demo-bot`、`cli_example`、张三 / 李四），不出现任何公司名、内部域名、真实人名、本机绝对路径；提交前跑仓库约定的敏感词自查。
- 访问记录里没有任何内容：`target` 只放计数或明文的范围名（`open`、`rows=3`、`new=2,over=0,skip=1,fu=4`），至多 64 字符；知识页的路径、标题、正文、待跟进的标题都不放。`action` 只能是 `access_log.ACTIONS` 里的短代码。
- `access_log.record(session, bot_id, actor_id, action, target="")` 的位置参数与 3C 一致，调用点不改写；本层只在末尾加一个仅限关键字的 `kind`，并把 `actor_id` 放宽为可空。
- 记录与主操作同一个事务：读取密文的接口在 `return` 之前 `await session.commit()`；写记录本身失败不抛（保存点 + 应用日志 `assistant_access_log_failed`）。
- 保留期 180 天，由 scheduler 的保留期那一档（`reaper.run_retention`）分批清理。
- `/api/me/assistant` 整个前缀：交互登录的所有者本人、`verify_csrf`、`Cache-Control: no-store`（沿用 3C 的 `DEPENDENCIES`），本层再加一条——管理命令建的会话（`admin_sessions.auth_method = 'cli'`）一律 403。
- 导入的上限（数值取自已有代码，不另设）：压缩包本身 20MB；页面文件 2000 个（`notes.MAX_PAGES`）；单页正文 200KB（`notes.MAX_PAGE_BYTES`）；导入后知识库合计 20MB（`notes.MAX_TOTAL_BYTES`）；路径 8 级、200 字；标题 120 字；摘要 300 字；待跟进未结 500 条、标题 200 字、备注 4000 字。解包后的字节合计上限是 32MB（20MB 正文加 front matter 与待跟进清单的余量，见「设计选择」第 6 条）。
- 导入写入一律 `updated_by="import"`、`owner_present=True`；整个导入一个事务，任何一步失败整体回滚。
- 压缩包只在内存里读：代码里不出现 `extract` / `extractall`，不往磁盘写任何东西。
- 普通员工（`kind='standard'`）的行为不变。本层改到的共用路径只有三处：对话记录的两个读接口与开发页的任务详情（多记一行访问记录，输出不变），以及 `keys.rewrap` 多一个可选的出参。
- 前端只用仓库现有的组件库与封装（Element Plus、`@/api/client` 的 `call` / `http` / `ApiError`、`@/utils/errors`、`@/utils/format`、`vue-i18n`）。文案不写 `@ { } |`，插值参数名只用 `name`、`count`、`n`、`total`、`time`、`value`、`detail`、`field`、`start`、`end`；英文文案里不出现中日文字符；三种语言的键必须一致（`web/tests/assistantI18n.spec.ts` 会查）。
- 代码风格：`ruff` 行宽 100，`mypy --strict`；注释与文档字符串用中文，写清「为什么」。计划里的代码块若与格式化器的换行不同，以 `uv run ruff format <文件>` 的结果为准；导入语句的顺序以 `uv run ruff check --fix <文件>` 的结果为准。Vue 模板的换行以 `npx eslint --fix <文件>` 的结果为准。
- 后端测试命令：`uv run pytest <路径> -q`；`tests/api`、`tests/integration` 下的用例要数据库（见 `docs/development.md`「验证」，不要指向生产库，用例会清空表）。每个后端任务结束时另跑 `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman && uv run alembic check`。
- 前端测试命令（在 `web/` 目录下）：`npx vitest run tests/<文件>`（`package.json` 的 `test` 脚本是 `vitest`）；每个前端任务结束时另跑 `npm run lint && npm run build`（`build` 含 `vue-tsc --noEmit`）。

## 对已有代码与 3C 的假定

**已落地，按实际代码**

- `coreman/api/deps.py`：`get_session` 只开会话、不提交；`current_user` 把 `admin_sessions` 的行放在 `request.state.admin_session`；`via_bot_token(request)`。会话工厂 `expire_on_commit=False`，自动 flush 开着。
- `coreman/api/routers/feishu_personal.py::interactive_user(request, actor) -> User`：只查「不是机器人令牌、有管理台会话、会话是本人的」，不看会话是怎么来的。
- `coreman/admin_cli/client.py::connect` 以 `--as` 指定的成员身份建一行 `AdminSession(auth_method="cli")`，经 `httpx.ASGITransport` 调同一套接口；`coreman api get <路径>` 可以调任意路径。
- 现有的三处「只对所有者本人还原」：`coreman/api/routers/chat_logs.py` 的 `list_chat_logs`、`get_chat_log`，`coreman/api/routers/dev.py` 的 `get_task`；都没有记录。`coreman/api/routers/infra_push.py::push` 里的 `sealed.open_row` 只为比对幂等载荷。
- `sealed.open_row / open_rows(session, rows, *, viewer=None)`、`sealed.is_opened(row)`（这一行是助理的行且内容已还原）、`sealed.owner_of(session, bot_id) -> uuid.UUID | None`（进程内缓存）、`sealed.key_ready(session, bot_id)`、`sealed.master()`。
- `notes.write(session, bot_id, path, *, body, summary, title=None, pinned=None, version=0, updated_by="assistant", owner_present=True) -> Entry`；`notes.normalize(path) -> (显示路径, 折叠大小写的键)`；`notes.index(session, bot_id, *, folder="") -> list[Entry]`；`notes._rows(session, bot_id, *, bodies, pinned=False, since=None)`；`NotesError(code, **detail)`；常量 `MAX_PAGES`、`MAX_TOTAL_BYTES`、`MAX_PAGE_BYTES`、`MAX_PATH_CHARS`、`MAX_TITLE_CHARS`、`MAX_SUMMARY_CHARS`。`write` 把摘要与标题里的空白压成单个空格；新页不给标题时取路径最后一段。
- `followups.add(session, bot_id, *, category, title, notes=None, review_on=None, due_on=None, origin_message_id=None, job_id=None) -> AssistantFollowup`；`followups.snooze(session, bot_id, seq, until, *, today)`；`followups.list_(session, bot_id, scope, *, today, limit=50)`（`scope="open"` 是全部未结，含推后的；会顺手把到了回看日的推后条目改回未结）；`FollowupError(code)`；`MAX_OPEN`、`MAX_TITLE`、`MAX_NOTES`；`FOLLOWUP_CATEGORIES`。
- `keys.rewrap(session, cipher) -> tuple[int, int]`；`coreman/cli.py::_assistant_keys_rewrap(apply)`。
- `coreman/runtime/scheduler/reaper.py::run_retention` 的 `jobs` 字典，每项的签名是 `(session, now, *, limit) -> int`；`tests/integration/test_scheduler_reaper.py` 里有一条对它返回值的全等断言。
- 上传的现成写法：`coreman/api/routers/feishu_apps.py::upload_feishu_app_avatar`（`UploadFile` 参数，`await file.read(上限 + 1)` 再比长度）。仓库里没有解压缩包的现成代码（`coreman/core/wecom/media.py` 只读文件名列表）。
- 下载的现成写法：`coreman/api/routers/skill_local.py`（`Response` 加 `NO_STORE` 与 `Content-Disposition`）。
- `tests/api/conftest.py`：`login_as`、`login_existing`、夹具 `app`、`client`；会话 cookie 用的测试密钥是 `"unit-test-session-secret-0123456789"`。`tests/integration/assistant_helpers.py`：`seed_assistant`、`seed_standard`、`SECRET`、`raw`。`tests/integration/conftest.py` 的夹具 `assistant_master`。
- 3A 已落地（`coreman/core/assistants/jobs.py`、`tests/integration/job_helpers.py`）；`GET /api/admin/assistants` 每条记录里 `jobs_active` 在 `inbox_pending` 之前，`inbox_pending` 是最后一个键。本层自己新写的用例不依赖 3A 的测试公共件（3C 的守卫台账用到它们）。
- 迁移目录里最新的是 `0101_assistant_hardening`（`revision = "0101"`）。

**3C（按其计划，尚未落地）**

1. `coreman/core/assistants/access_log.py`：`ACTIONS = ("approvals.read", "followups.read", "jobs.read", "notes.index", "notes.read", "settings.read")`、`MAX_TARGET = 64`、`async def record(session: AsyncSession, bot_id: uuid.UUID, actor_id: uuid.UUID, action: str, target: str = "") -> None`（不认识的动作抛 `ValueError("unknown access action: …")`，写一条应用日志 `assistant_content_access`，键 `bot_id`、`actor_id`、`action`、`target`）。
2. `tests/unit/test_assistant_access_log.py`：用 `AsyncMock()` 当会话、对日志做全等断言。本层 Task 1 删掉它，换成对着数据库的用例。
3. `coreman/api/routers/me_assistant_common.py`：`PREFIX = "/api/me/assistant"`；`async def no_store(response: Response) -> None`；`DEPENDENCIES = [Depends(verify_csrf), Depends(no_store)]`；`@dataclass(frozen=True) class Mine`（`actor: User`、`bot: Bot`）；`async def mine(actor: User = Depends(interactive_user), session: AsyncSession = Depends(get_session)) -> Mine`（没有助理 → 404 `no_assistant`）；`async def writable(own: Mine = Depends(mine)) -> Mine`（不是 `active` → 409 `assistant_not_active`）；`def refuse(code: str, table: Mapping[str, tuple[int, str]]) -> ApiError`。
4. 三个路由文件与 `coreman/api/main.py` 里的注册：`me_assistant.py`（`router`；函数 `overview`、`job_list`、`job_cancel`、`attention`、`recent_approvals`、`settings_view`、`settings_patch`、`deprovision`）、`me_assistant_followups.py`（`listed` 等）、`me_assistant_notes.py`（`index`、`read`、`write`、`delete`、`_entry`、`_refuse`、`ERRORS`）。`main.py` 里是 `from coreman.api.routers import me_assistant, me_assistant_followups, me_assistant_notes` 加三行 `app.include_router(...)`。
5. 各读取接口记的（动作, 对象）：`GET /followups` →（`followups.read`, `open`）；`GET /notes` →（`notes.index`, `""`）；`GET /notes/page` →（`notes.read`, `page`）；`GET /jobs` →（`jobs.read`, `list`）；`GET /attention` →（`approvals.read`, `pending`）、（`jobs.read`, `blocked`）、（`followups.read`, `today`）；`GET /approvals/recent` →（`approvals.read`, `recent`）；`GET /settings` →（`settings.read`, `""`）；概览 `GET ""` 只有元数据，不记。
6. 其中四个读取接口在 `record` 之后直接 `return`、没有提交：`me_assistant_notes.index`、`me_assistant_notes.read`、`me_assistant.job_list`、`me_assistant.recent_approvals`。另外三个（`listed`、`attention`、`settings_view`）本来就提交。
7. `coreman/core/assistants/owner_view.py::approvals(session, bot, viewer, statuses, *, limit=APPROVAL_LIMIT) -> list[AssistantApproval]`：核心函数里调 `sealed.open_rows(session, rows, viewer=viewer)`，由路由记账。
8. `notes.Entry` 多一个字段 `updated_by: str`（在 `updated_at` 之后），`notes.Page(Entry)` 随之带上；`notes.py` 顶部是 `from dataclasses import dataclass`。
9. 测试公共件：`tests/api/conftest.py` 的夹具 `assistant_master`；`tests/api/my_assistant_helpers.py`：`URL = "/api/me/assistant"`、`async def owned(client, db_session, *, role="member") -> tuple[Bot, User]`（造一位有数据密钥的助理并以所有者登录）、`def as_bot_token(client, monkeypatch, user) -> None`、`def code(response) -> str | None`（`errors[0].type`）。
10. `tests/api/test_my_assistant_guard.py`：`CASES: dict[tuple[str, str], dict[str, Any]]`（键是（方法, 路由模板），值里用到 `path`、`params`、`json`）、`async def _call(client, seeded, method, template)`、`Seeded.intact`、`test_every_route_under_the_prefix_is_in_the_table`。
11. 前端：`web/src/api/myAssistant.ts`（`import { call, http } from '@/api/client'`、`const root = '/api/me/assistant'`、对象 `myAssistant`，最后一个方法是 `deprovision`）；`web/src/api/assistantsAdmin.ts` 的 `interface AssistantRow`（最后一个字段 `jobs_active: number`）；`web/src/i18n/assistant.ts` 的 `assistantZh` / `assistantEn` / `assistantJa`（`notes` 一节最后一个键是 `conflict`，`settings` 一节最后一个键是 `deprovisionStarted`、其中有 `deprovisionPrompt`，`admin` 一节最后一个键是 `deprovisionStarted`、其中有 `none`）；组件 `web/src/components/assistant/AssistantNotes.vue`（`index` 是 `ref<NoteIndex | null>`，有函数 `load`，模板里「新建一页」按钮带 `data-test="create"`）、`AssistantSettings.vue`（模板里表单之后是 `class="danger-zone"` 的 `el-alert`）、`web/src/views/AssistantsView.vue`（「轮询健康」一列之后是「待删目录」一列）；用例 `web/tests/AssistantNotes.spec.ts`、`AssistantSettings.spec.ts`（各有一个 `render` 函数，`global: { plugins: [ElementPlus, i18n] }`）、`AssistantsView.spec.ts`（`row(over)` 工厂）、`assistantI18n.spec.ts`。
12. 3C 不改 `GET /api/admin/assistants` 的输出（它的 Task 13 写明后端不用改），只把停用提成公共函数：`list_assistants` 里每条记录的最后一个键仍是 `inbox_pending`。

## 设计选择

**1. 记录与主操作同一个事务，写失败不挡主操作。** 读代码后的取舍：`get_session` 不提交，所以读取接口要自己在返回前提交——提交失败（库不可用）时请求以 500 结束，内容没有交出去，这就是「读了一定有记」。而「写这一行」单独出错（外键对不上、表被锁）不该让所有者看不了自己的东西：`record` 先 `flush`（主操作攒着的改动出错照常抛），再在保存点里用 Core 的 `insert` 写一行，失败只回滚到保存点并记应用日志 `assistant_access_log_failed`。无论落库成败，3C 的那条 `assistant_content_access` 应用日志都照写，是第二份记录。残留的缝：保存点里的写失败而外层提交成功时，这一次读取只在应用日志里有记。

**2. 谁算「非所有者」。** `actor_kind` 由 `record` 推出：`actor_id` 为空是 `system`（服务器上的管理命令）；等于助理所有者是 `owner`；否则是 `admin`。按今天的代码，接口层没有任何一条路能让非所有者解密成功（`viewer` 不是所有者时内容被收回，`record_opened` 只数真的还原了的行），所以 `admin` 目前没有产生者，留给以后；`system` 的来源是管理命令。

**3. 管理命令建的会话不是本人在场。** `coreman … --as 登录名` 会以任意成员的身份建一个真的管理台会话，`interactive_user` 分辨不出来。本层的处理分两类：`/api/me/assistant` 整个前缀拒绝这种会话（403 `cli_session_refused`）——否则运维能以所有者的身份读他的知识库，还能导入置顶页（等于给助理写规则）；第 1 期就有的对话记录与任务详情不改行为（仍对「以所有者身份」的会话还原），但记成 `system`（动作 `cli.chatlogs`、`cli.tasks`，操作人为空），所有者在访问记录里看得到、管理台列表的「最近一次非本人访问」也会更新。要不要干脆让这种会话读不到内容，见文末「没覆盖到的地方」。

**4. 动作代码沿用 3C 的。** 任务书举的例子是 `followups.list`、`approvals.recent`、`jobs.list`，3C 计划里实际用的是 `followups.read`、`approvals.read`、`jobs.read`（范围放在 `target`）。本层不改 3C 的调用点，新增：`chatlogs.read`、`tasks.read`、`import.preview`、`import.notes`、`export`、`cli.rewrap`、`cli.chatlogs`、`cli.tasks`。库里不对 `action` 加检查约束（加动作不必写迁移），由 `ACTIONS` 把关；`actor_kind` 有检查约束。

**5. front matter 用仓库已有的 PyYAML。** `pyproject.toml` 里已有 `pyyaml`，所以不自己写解析器：文件第一行是 `---`、到下一个只有 `---` 的行为止的一段，交给 `yaml.safe_load`（长度限 4096 字符）；只采用 `title`、`summary`（标量，按字符串用）、`pinned`（布尔）三个键，其余键忽略。两条 `---` 之间不是映射（比如开头只是一条分隔线）时整篇当正文；是映射但 YAML 写坏了、或三个键的类型不对，这一个文件跳过（`invalid_front_matter`），不影响别的文件。导出用 `yaml.safe_dump`，三个键都写出，读回来与原样一致。

**6. 解包上限是 32MB，不是 20MB。** 任务书写「解包后合计不超过知识库的 20MB 上限」。导出的文件带 front matter，再加 `followups.json`：一个装满的知识库导出来解包后会略大于 20MB，按 20MB 卡就导不回去。所以解包的硬上限取 20MB 加 12MB 余量（2000 个头各约 1.4KB，500 条待跟进各约 13KB 的最坏情况），正文合计是否放得进知识库由 `transfer.plan` 按 `notes.MAX_TOTAL_BYTES`（20MB）另算。

**7. 待跟进清单用 `followups.json`。** 两种格式里选 JSON：字段有日期、可空值与多行备注，Markdown 清单要另定一套转义规则；JSON 用标准库就能读写，导出再导入不走样。语法见 Task 5 的 `_followups`。

**8. 预览与确认之间的乐观锁：`digest`。** 预览（`dry_run=true`，默认值）返回一张清单与一个 `digest`——对「压缩包的 SHA-256、覆盖选项、每一页的去向与要覆盖的版本号、待跟进的去向」算的摘要。确认时（`dry_run=false`）必须带回 `expect=<digest>`：服务端对着此刻的库重新出一张清单，摘要对不上（期间助理写过页面、换了包、换了选项）就 409 `plan_changed`、什么都不写，前端重新预览。真正写入时每一页仍带着清单里的版本号调 `notes.write`，清单与写入之间再被改动会在 `write` 里撞上版本检查，整个导入回滚。没带 `expect` 的 `dry_run=false` 同样 409：不预览就写不进去。

**9. 条目级的问题只跳过那一条，安全问题拒绝整个包。** 路径不合法、路径重复、单页超限、正文为空、标题或摘要超长、front matter 写坏——出现在预览的「跳过」里，其余照常导入。压缩包本身的问题（穿越、符号链接、嵌套压缩包、加密、非 `.md`、非 UTF-8、超限、`followups.json` 整体结构不对）一律拒绝整个包、不读库、不留访问记录。

**10. 置顶页在预览里标出来。** 导入可以置顶（所有者本人在场）。置顶页每天第一轮原样交给助理当规则，所以预览清单里每一页带 `pinned`，前端在有置顶页时单独提示一句。

**11. 导出的内容。** 全部知识页（含置顶状态、标题、摘要）与未结的待跟进（含推后的）。不导出：已关闭的待跟进、审批流水、对话记录、设置、访问记录。压缩包在内存里打好（至多三十多 MB）再分块发出；访问记录在发出第一个字节之前已经提交。

## File Structure

| 文件 | 职责 |
|---|---|
| `migrations/versions/0102_assistant_access_log.py`（新） | 表 `assistant_access_log` 与三个索引 |
| `coreman/core/db/models/assistants.py`、`coreman/core/db/models/__init__.py` | 模型 `AssistantAccessLog`、常量 `ACCESS_ACTOR_KINDS` |
| `coreman/core/assistants/access_log.py`（改写 3C 的） | `record`、`record_opened`、`bots_of`、`list_`、`last_foreign`、`cleanup` |
| `coreman/core/assistants/archive.py`（新） | 压缩包的格式、安全检查、front matter、`followups.json`；纯函数 |
| `coreman/core/assistants/transfer.py`（新） | 对着库出导入清单、写入、导出 |
| `coreman/core/assistants/notes.py` | `dump`：全部页面连正文 |
| `coreman/core/assistants/keys.py` | `rewrap` 多一个出参 `touched` |
| `coreman/api/deps.py` | `via_cli(request)` |
| `coreman/api/routers/me_assistant_common.py` | `in_person`，并入 `DEPENDENCIES` |
| `coreman/api/routers/me_assistant.py`、`me_assistant_notes.py` | 四个读接口补提交；`GET /access-log` |
| `coreman/api/routers/me_assistant_transfer.py`（新） | `POST /import`、`GET /export` |
| `coreman/api/routers/chat_logs.py`、`dev.py` | 对所有者还原了内容时记一行 |
| `coreman/api/routers/assistants_admin.py` | 列表每条加 `last_non_owner_access_at` |
| `coreman/api/main.py` | 注册新路由 |
| `coreman/cli.py` | `assistant-keys rewrap --apply` 记 `cli.rewrap` |
| `coreman/runtime/scheduler/reaper.py` | 保留期清理加一项 |
| `tests/conftest.py` | `BUSINESS_TABLES` 加表名 |
| `tests/fakes/archives.py`（新） | 造压缩包的测试公共件 |
| `tests/integration/test_assistant_access_log.py`（新；取代 `tests/unit/test_assistant_access_log.py`）、`test_assistant_transfer.py`（新）、`test_assistant_keys_rewrap.py`、`test_scheduler_reaper.py` | 核心模块的用例 |
| `tests/unit/test_assistant_archive.py`、`test_assistant_access_ledger.py`（新） | 压缩包的每条限制；解密台账 |
| `tests/api/test_my_assistant_access_log.py`、`test_my_assistant_import.py`、`test_my_assistant_export.py`（新）、`test_my_assistant_guard.py`、`my_assistant_helpers.py`、`test_assistant_chat_logs.py`、`test_assistant_admin.py` | 接口的用例 |
| `web/src/api/myAssistant.ts`、`web/src/api/assistantsAdmin.ts` | 接口封装与类型 |
| `web/src/utils/download.ts`（新） | 把一段二进制存成文件 |
| `web/src/i18n/assistant.ts` | 三种语言的新文案 |
| `web/src/components/assistant/AssistantAccessLog.vue`、`AssistantTransfer.vue`（新） | 访问记录列表；导入的两步确认与导出 |
| `web/src/components/assistant/AssistantSettings.vue`、`AssistantNotes.vue`、`web/src/views/AssistantsView.vue` | 嵌入上面两个组件；管理台多一列 |
| `web/tests/AssistantAccessLog.spec.ts`、`AssistantTransfer.spec.ts`（新）、`AssistantSettings.spec.ts`、`AssistantNotes.spec.ts`、`AssistantsView.spec.ts` | 前端用例 |
| `docs/feishu.md`、`docs/operations.md`、`CHANGELOG.md`、spec、总览 | 文档同步 |

## 接口一览

| 方法 | 路径 | 作用 | 任务 |
|---|---|---|---|
| GET | `/api/me/assistant/access-log?limit=&before=` | 所有者看自己的访问记录，新的在前，按编号翻页 | 4 |
| POST | `/api/me/assistant/import` | 上传压缩包；`multipart/form-data`：`archive`（文件）、`dry_run`（`true` / `false`，默认 `true`）、`overwrite`（`true` / `false`，默认 `false`）、`expect`（确认时带回预览给的 `digest`） | 7 |
| GET | `/api/me/assistant/export` | 下载知识页与未结待跟进的压缩包 | 8 |
| GET | `/api/admin/assistants` | 每条记录多一个 `last_non_owner_access_at`（ISO 时间或 `null`） | 4 |

核心函数：

- `access_log.record(session, bot_id, actor_id, action, target="", *, kind=None) -> None`
- `access_log.record_opened(session, rows, actor_id, action) -> int`
- `access_log.bots_of(session, owner_ids) -> list[uuid.UUID]`
- `access_log.list_(session, bot_id, *, limit=50, before=None) -> list[Seen]`
- `access_log.last_foreign(session) -> dict[uuid.UUID, datetime]`
- `access_log.cleanup(session, now, *, limit=500) -> int`
- `archive.parse(data) -> Parsed`、`archive.page_text(*, title, summary, pinned, body) -> str`、`archive.followups_text(items) -> str`、`archive.build(pages, followups_json) -> bytes`
- `transfer.plan(session, bot_id, parsed, *, overwrite, today, fingerprint) -> Plan`、`transfer.apply(session, bot_id, plan, *, today) -> None`、`transfer.preview(plan) -> dict`、`transfer.counts(plan) -> str`、`transfer.export(session, bot_id, *, today) -> tuple[bytes, str]`
- `notes.dump(session, bot_id) -> list[Page]`
- `deps.via_cli(request) -> bool`

---

### Task 1: 表 `assistant_access_log` 与落库的 `record`

**Files:**
- Create: `migrations/versions/0102_assistant_access_log.py`
- Modify: `coreman/core/db/models/assistants.py`（文件末尾）
- Modify: `coreman/core/db/models/__init__.py`（从 `assistants` 导入的那一组与 `__all__`）
- Modify: `tests/conftest.py`（`BUSINESS_TABLES` 的第一项之前）
- Modify: `coreman/core/assistants/access_log.py`（整个文件换掉）
- Delete: `tests/unit/test_assistant_access_log.py`
- Test: `tests/integration/test_assistant_access_log.py`（新建）

**Interfaces:**
- Consumes: `sealed.owner_of(session, bot_id) -> uuid.UUID | None`；`sealed.is_opened(row) -> bool`；`coreman.core.logging.get_logger`；夹具 `db_session`、`assistant_master`；`tests.integration.assistant_helpers.seed_assistant`。
- Produces：
  - 模型 `AssistantAccessLog`（列 `id: int`、`bot_id: uuid.UUID`、`actor_id: uuid.UUID | None`、`actor_kind: str`、`action: str`、`target: str`、`at: datetime`）；常量 `ACCESS_ACTOR_KINDS = ("owner", "admin", "system")`。
  - `access_log.ACTIONS: tuple[str, ...]`（3C 的六个加本层的八个）、`MAX_TARGET = 64`、`RETENTION = timedelta(days=180)`、`MAX_LIST = 200`。
  - `async def record(session: AsyncSession, bot_id: uuid.UUID, actor_id: uuid.UUID | None, action: str, target: str = "", *, kind: str | None = None) -> None`
  - `async def record_opened(session: AsyncSession, rows: Iterable[Any], actor_id: uuid.UUID | None, action: str) -> int`（记了几位助理）
  - `@dataclass(frozen=True) class Seen`（`id: int`、`at: datetime`、`actor_kind: str`、`actor_name: str | None`、`action: str`、`target: str`）
  - `async def list_(session: AsyncSession, bot_id: uuid.UUID, *, limit: int = 50, before: int | None = None) -> list[Seen]`
  - `async def last_foreign(session: AsyncSession) -> dict[uuid.UUID, datetime]`
  - `async def cleanup(session: AsyncSession, now: datetime, *, limit: int = 500) -> int`

- [ ] **Step 1: 写失败测试**

删掉 3C 的 `tests/unit/test_assistant_access_log.py`（它拿 `AsyncMock()` 当会话，落库之后不再成立）：

```bash
git rm tests/unit/test_assistant_access_log.py
```

`tests/integration/test_assistant_access_log.py`（新建）：

```python
"""访问记录：只有编号、动作与对象代号；与主操作同一个事务；写不进去不挡主操作；按人查、按期清。"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import access_log
from coreman.core.db.models import AssistantAccessLog, User
from coreman.core.timeutils import utcnow
from tests.conftest import BUSINESS_TABLES
from tests.integration.assistant_helpers import seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
STORED = (
    "SELECT actor_id, actor_kind, action, target FROM assistant_access_log"
    " WHERE bot_id = :bot ORDER BY id"
)


async def _stored(session: AsyncSession, bot_id: uuid.UUID) -> list[tuple[Any, ...]]:
    rows = await session.execute(text(STORED), {"bot": bot_id})
    return [tuple(row) for row in rows.all()]


def test_table_is_truncated_with_the_other_business_tables() -> None:
    assert "assistant_access_log" in BUSINESS_TABLES


async def test_the_table_has_no_column_that_could_hold_content(db_session: AsyncSession) -> None:
    rows = await db_session.execute(
        text(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'assistant_access_log'"
        )
    )
    assert {row[0] for row in rows.all()} == {
        "id",
        "bot_id",
        "actor_id",
        "actor_kind",
        "action",
        "target",
        "at",
    }


async def test_a_read_by_the_owner_is_stored_and_logged(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    with capture_logs() as logs:
        await access_log.record(db_session, bot.id, owner.id, "followups.read", "open")
    await db_session.commit()
    assert await _stored(db_session, bot.id) == [(owner.id, "owner", "followups.read", "open")]
    assert logs == [
        {
            "event": "assistant_content_access",
            "log_level": "info",
            "bot_id": str(bot.id),
            "actor_id": str(owner.id),
            "actor_kind": "owner",
            "action": "followups.read",
            "target": "open",
        }
    ]


async def test_the_actor_kind_follows_who_acted(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    other = User(login_name="auditor", display_name="李四")
    db_session.add(other)
    await db_session.flush()
    await access_log.record(db_session, bot.id, other.id, "chatlogs.read", "rows=1")
    await access_log.record(db_session, bot.id, None, "cli.rewrap")
    await access_log.record(db_session, bot.id, owner.id, "cli.chatlogs", "rows=1", kind="system")
    await db_session.commit()
    assert await _stored(db_session, bot.id) == [
        (other.id, "admin", "chatlogs.read", "rows=1"),
        (None, "system", "cli.rewrap", ""),
        (owner.id, "system", "cli.chatlogs", "rows=1"),
    ]
    with pytest.raises(ValueError, match="unknown actor kind"):
        await access_log.record(db_session, bot.id, owner.id, "notes.read", "page", kind="robot")


async def test_the_target_is_cut_and_unknown_actions_are_rejected(
    db_session: AsyncSession,
) -> None:
    bot, owner = await seed_assistant(db_session)
    # 回滚会让会话里的对象过期：之后还要用的编号先取出来。
    bot_id, owner_id = bot.id, owner.id
    await access_log.record(db_session, bot_id, owner_id, "jobs.read", "x" * 500)
    await db_session.commit()
    [(_, _, _, target)] = await _stored(db_session, bot_id)
    assert target == "x" * access_log.MAX_TARGET
    with pytest.raises(ValueError, match="unknown access action"):
        await access_log.record(db_session, bot_id, owner_id, "pages.dump", "")
    await db_session.rollback()
    assert len(await _stored(db_session, bot_id)) == 1


async def test_the_row_shares_the_callers_transaction(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    bot_id = bot.id
    await access_log.record(db_session, bot_id, owner.id, "notes.read", "page")
    await db_session.rollback()
    assert await _stored(db_session, bot_id) == []


async def test_a_failed_insert_does_not_block_the_main_operation(
    db_session: AsyncSession,
) -> None:
    _bot, owner = await seed_assistant(db_session)
    # 主操作攒着的改动。
    db_session.add(User(login_name="probe", display_name="李四"))
    nowhere = uuid.uuid4()
    with capture_logs() as logs:
        # 没有这条助理记录：外键对不上，这一行写不进去。
        await access_log.record(db_session, nowhere, owner.id, "notes.read", "page")
    await db_session.commit()
    assert [entry["event"] for entry in logs] == [
        "assistant_content_access",
        "assistant_access_log_failed",
    ]
    assert logs[1]["error"] == "IntegrityError" and logs[1]["action"] == "notes.read"
    # 主操作照常提交了；那一行没有留下。
    assert await db_session.scalar(select(User.id).where(User.login_name == "probe")) is not None
    assert await _stored(db_session, nowhere) == []


async def test_a_deleted_actor_leaves_the_row_without_a_name(db_session: AsyncSession) -> None:
    bot, _owner = await seed_assistant(db_session)
    other = User(login_name="leaver", display_name="李四")
    db_session.add(other)
    await db_session.flush()
    await access_log.record(db_session, bot.id, other.id, "chatlogs.read", "rows=1")
    await db_session.commit()
    await db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": other.id})
    await db_session.commit()
    assert await _stored(db_session, bot.id) == [(None, "admin", "chatlogs.read", "rows=1")]


async def test_listing_is_newest_first_pages_by_id_and_names_the_actor(
    db_session: AsyncSession,
) -> None:
    bot, owner = await seed_assistant(db_session)
    other = User(login_name="auditor", display_name="李四")
    db_session.add(other)
    await db_session.flush()
    await access_log.record(db_session, bot.id, owner.id, "notes.index", "")
    await access_log.record(db_session, bot.id, other.id, "chatlogs.read", "rows=2")
    await access_log.record(db_session, bot.id, None, "cli.rewrap", "")
    await db_session.commit()
    rows = await access_log.list_(db_session, bot.id, limit=2)
    assert [(row.action, row.actor_kind, row.actor_name) for row in rows] == [
        ("cli.rewrap", "system", None),
        ("chatlogs.read", "admin", "李四"),
    ]
    older = await access_log.list_(db_session, bot.id, limit=2, before=rows[-1].id)
    assert [(row.action, row.actor_kind, row.actor_name, row.target) for row in older] == [
        ("notes.index", "owner", "张三", "")
    ]
    # 别人的助理：一行都拿不到。
    theirs, _their_owner = await seed_assistant(db_session)
    assert await access_log.list_(db_session, theirs.id) == []
    # 「最近一次非所有者访问」不数所有者自己的。
    assert await access_log.last_foreign(db_session) == {bot.id: rows[0].at}


async def test_rows_older_than_180_days_are_removed_in_batches(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    now = utcnow()
    for days in (181, 181, 181, 179, 0):
        db_session.add(
            AssistantAccessLog(
                bot_id=bot.id,
                actor_id=owner.id,
                actor_kind="owner",
                action="notes.read",
                target="page",
                at=now - timedelta(days=days),
            )
        )
    await db_session.commit()
    assert await access_log.cleanup(db_session, now, limit=2) == 2
    assert await access_log.cleanup(db_session, now, limit=2) == 1
    assert await access_log.cleanup(db_session, now, limit=2) == 0
    await db_session.commit()
    left = await db_session.scalar(select(func.count()).select_from(AssistantAccessLog))
    assert left == 2
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_access_log.py -q`
Expected: FAIL：收集时 `ImportError: cannot import name 'AssistantAccessLog' from 'coreman.core.db.models'`。

- [ ] **Step 3: 写迁移与模型**

`migrations/versions/0102_assistant_access_log.py`（新建）：

```python
"""Personal assistant access log: every decryption of an assistant's content outside its own
turns, with who, what and when, and no content."""

import sqlalchemy as sa
from alembic import op

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None

KINDS = "actor_kind IN ('owner', 'admin', 'system')"
FOREIGN = "actor_kind <> 'owner'"


def upgrade() -> None:
    op.create_table(
        "assistant_access_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "actor_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(KINDS, name="actor_kind"),
    )
    # 所有者翻自己的记录。
    op.create_index("assistant_access_log_bot_at_idx", "assistant_access_log", ["bot_id", "at"])
    # 保留期清理。
    op.create_index("assistant_access_log_at_idx", "assistant_access_log", ["at"])
    # 管理台列表的「最近一次非所有者访问」：这样的行很少，单独一个部分索引。
    op.create_index(
        "assistant_access_log_foreign_idx",
        "assistant_access_log",
        ["bot_id", "at"],
        postgresql_where=sa.text(FOREIGN),
    )


def downgrade() -> None:
    op.drop_index("assistant_access_log_foreign_idx", table_name="assistant_access_log")
    op.drop_index("assistant_access_log_at_idx", table_name="assistant_access_log")
    op.drop_index("assistant_access_log_bot_at_idx", table_name="assistant_access_log")
    op.drop_table("assistant_access_log")
```

`coreman/core/db/models/assistants.py`：文件末尾追加（`BigInteger`、`CheckConstraint`、`ForeignKey`、`Identity`、`Index`、`Text`、`Uuid`、`text`、`enum_check`、`_ts` 这个文件都已导入或定义）：

```python
ACCESS_ACTOR_KINDS = ("owner", "admin", "system")


class AssistantAccessLog(Base):
    """运行时路径之外对个人内容的每一次解密：谁、什么时候、做了什么。没有内容列。

    `target` 只放计数或明文的范围名（`open`、`rows=3`）；知识页的路径、标题都属于内容，不放。
    `actor_id` 为空：这次操作来自服务器上的管理命令（`system`），或操作人的账号已删除。
    `action` 的取值由 `coreman/core/assistants/access_log.py` 的 `ACTIONS` 把关，库里不设检查
    约束（加一个动作不必写迁移）。保留 180 天，由 scheduler 清理。
    """

    __tablename__ = "assistant_access_log"
    __table_args__ = (
        CheckConstraint(enum_check("actor_kind", ACCESS_ACTOR_KINDS), name="actor_kind"),
        Index("assistant_access_log_bot_at_idx", "bot_id", "at"),
        Index("assistant_access_log_at_idx", "at"),
        Index(
            "assistant_access_log_foreign_idx",
            "bot_id",
            "at",
            postgresql_where=text("actor_kind <> 'owner'"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_kind: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    target: Mapped[str] = mapped_column(Text, server_default=text("''"))
    at: Mapped[datetime] = _ts(server_default=text("now()"))
```

`coreman/core/db/models/__init__.py`：在 `from coreman.core.db.models.assistants import (` 这一组里加 `ACCESS_ACTOR_KINDS,` 与 `AssistantAccessLog,`，在 `__all__` 里加 `"ACCESS_ACTOR_KINDS",` 与 `"AssistantAccessLog",`（位置以 `uv run ruff check --fix coreman/core/db/models/__init__.py` 的排序为准）。

`tests/conftest.py`：`BUSINESS_TABLES = [` 之后、`"assistant_jobs",` 之前加一行：

```python
    "assistant_access_log",
```

- [ ] **Step 4: 改写 `access_log.py`**

`coreman/core/assistants/access_log.py`（整个文件换成）：

```python
"""个人内容的访问记录：运行时路径之外的每一次解密都记一行（表 `assistant_access_log`）。

「运行时路径」是助理自己的轮次（worker、盯消息、投递、审批执行）。之外的是有人或有命令把内容
拿出来：「我的助理」页的读取、导入与导出、所有者在管理台读自己的对话记录、服务器上的管理命令。
记录里只有编号、动作与对象的代号：`target` 只能是计数或明文的范围名（`open`、`rows=3`），
知识页的路径与标题本身是个人内容，调用方不要传进来。

与审计日志（`coreman/core/audit.py`）的区别：审计记「谁改了平台上的什么」，给管理员看；这里记
「谁读了某位所有者的个人内容」，给所有者本人看，管理员只看得到「最近一次非所有者访问的时间」。

落库与主操作在同一个事务里（调用方提交）：读取接口在返回之前提交，提交不了内容也不会交出去。
写这一行本身失败（外键对不上、表不可用）不挡主操作——放在保存点里，失败只回滚到保存点并记
一条应用日志。`assistant_content_access` 这条应用日志无论落库成败都写，是兜底的第二份记录。
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed
from coreman.core.db.models import ACCESS_ACTOR_KINDS, AssistantAccessLog, Bot, User
from coreman.core.logging import get_logger

log = get_logger(__name__)

ACTIONS = (
    # 「我的助理」页的读取
    "approvals.read",
    "followups.read",
    "jobs.read",
    "notes.index",
    "notes.read",
    "settings.read",
    # 所有者在管理台读自己的对话记录、任务详情
    "chatlogs.read",
    "tasks.read",
    # 导入与导出
    "import.preview",
    "import.notes",
    "export",
    # 服务器上的管理命令
    "cli.rewrap",
    "cli.chatlogs",
    "cli.tasks",
)
MAX_TARGET = 64
RETENTION = timedelta(days=180)
MAX_LIST = 200


@dataclass(frozen=True)
class Seen:
    """给所有者看的一行访问记录。"""

    id: int
    at: datetime
    actor_kind: str
    # 操作人的显示名；管理命令或账号已删除时为空。
    actor_name: str | None
    action: str
    target: str


async def record(
    session: AsyncSession,
    bot_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    target: str = "",
    *,
    kind: str | None = None,
) -> None:
    """记一次解密。调用方在交出内容之前调用，并负责提交。

    Args:
        actor_id: 操作人；服务器上的管理命令没有操作人，传 None
        action: `ACTIONS` 之一
        target: 计数或明文的范围名，超长截断；不要放任何内容
        kind: 省略时推出来——没有操作人是 `system`，操作人是所有者是 `owner`，否则 `admin`；
            管理命令以某位成员的身份操作时，调用方明说 `system`

    Raises:
        ValueError: 不认识的动作或操作人类别（写错的调用点要在测试里就失败）
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown access action: {action}")
    if kind is None:
        if actor_id is None:
            kind = "system"
        else:
            kind = "owner" if await sealed.owner_of(session, bot_id) == actor_id else "admin"
    elif kind not in ACCESS_ACTOR_KINDS:
        raise ValueError(f"unknown actor kind: {kind}")
    cut = target[:MAX_TARGET]
    log.info(
        "assistant_content_access",
        bot_id=str(bot_id),
        actor_id=str(actor_id) if actor_id else None,
        actor_kind=kind,
        action=action,
        target=cut,
    )
    # 先把主操作攒着的改动刷下去：它们出错应当照常抛给调用方，不能被下面的保存点吞掉。
    await session.flush()
    try:
        async with session.begin_nested():
            await session.execute(
                insert(AssistantAccessLog).values(
                    bot_id=bot_id, actor_id=actor_id, actor_kind=kind, action=action, target=cut
                )
            )
    except SQLAlchemyError as exc:
        log.warning(
            "assistant_access_log_failed",
            bot_id=str(bot_id),
            action=action,
            error=type(exc).__name__,
        )


async def record_opened(
    session: AsyncSession, rows: Iterable[Any], actor_id: uuid.UUID | None, action: str
) -> int:
    """一批可能混着普通员工的行：只数真的还原出了内容的助理行，每位助理记一条。

    `sealed.open_rows(..., viewer=…)` 之后调用：查看者不是所有者、密钥已销毁的行内容已被收回，
    `sealed.is_opened` 为假，不算。返回记了几位助理（为 0 时调用方不必提交）。
    """
    counts: Counter[uuid.UUID] = Counter(
        row.bot_id for row in rows if row is not None and sealed.is_opened(row)
    )
    for bot_id, count in counts.items():
        await record(session, bot_id, actor_id, action, f"rows={count}")
    return len(counts)


async def bots_of(session: AsyncSession, owner_ids: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    """这些人名下还启用着的助理记录。"""
    owners = list(owner_ids)
    if not owners:
        return []
    found = await session.scalars(
        select(Bot.id).where(
            Bot.kind == "assistant", Bot.enabled.is_(True), Bot.owner_id.in_(owners)
        )
    )
    return list(found)


async def list_(
    session: AsyncSession, bot_id: uuid.UUID, *, limit: int = 50, before: int | None = None
) -> list[Seen]:
    """这位助理的访问记录，新的在前。

    Args:
        limit: 至多 `MAX_LIST`
        before: 只要编号小于它的（上一页最后一行的编号）
    """
    stmt = (
        select(AssistantAccessLog, User.display_name)
        .outerjoin(User, User.id == AssistantAccessLog.actor_id)
        .where(AssistantAccessLog.bot_id == bot_id)
        .order_by(AssistantAccessLog.id.desc())
        .limit(max(1, min(limit, MAX_LIST)))
    )
    if before is not None:
        stmt = stmt.where(AssistantAccessLog.id < before)
    return [
        Seen(row.id, row.at, row.actor_kind, name, row.action, row.target)
        for row, name in (await session.execute(stmt)).all()
    ]


async def last_foreign(session: AsyncSession) -> dict[uuid.UUID, datetime]:
    """每位助理最近一次非所有者访问的时间；从来没有的不在结果里。"""
    rows = await session.execute(
        select(AssistantAccessLog.bot_id, func.max(AssistantAccessLog.at))
        .where(AssistantAccessLog.actor_kind != "owner")
        .group_by(AssistantAccessLog.bot_id)
    )
    return {bot_id: seen for bot_id, seen in rows.all()}


async def cleanup(session: AsyncSession, now: datetime, *, limit: int = 500) -> int:
    """删掉超过保留期的行，一次至多 `limit` 行；返回删了多少。调用方提交。"""
    doomed = (
        select(AssistantAccessLog.id)
        .where(AssistantAccessLog.at < now - RETENTION)
        .order_by(AssistantAccessLog.id)
        .limit(limit)
        .scalar_subquery()
    )
    result = await session.execute(
        delete(AssistantAccessLog)
        .where(AssistantAccessLog.id.in_(doomed))
        .returning(AssistantAccessLog.id)
    )
    return len(result.all())
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_access_log.py -q`
Expected: PASS（10 个用例）。

Run: `uv run pytest tests/unit/test_migration_rules.py -q`
Expected: PASS（迁移门禁：`upgrade()` 里只有建表与建索引，不需要登记例外）。

Run: `uv run pytest tests/api -q -k "my_assistant"`
Expected: PASS。3C 的用例只从应用日志里取 `action`、`target`、`actor_id` 三个键，多出来的 `actor_kind` 不影响它们；此时四个没提交的读接口还不落库（Task 2 补）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman && uv run alembic check`
Expected: 全部通过；`alembic check` 报没有待生成的迁移。

- [ ] **Step 6: 提交**

```bash
git add migrations/versions/0102_assistant_access_log.py coreman/core/db/models/assistants.py \
  coreman/core/db/models/__init__.py coreman/core/assistants/access_log.py tests/conftest.py \
  tests/integration/test_assistant_access_log.py
git commit -m "个人助理访问记录落库：表 assistant_access_log，与主操作同一事务，写失败不挡主操作"
```

---

### Task 2: 「我的助理」页的每个读接口都落一行

**Files:**
- Modify: `coreman/api/routers/me_assistant_notes.py`（`index`、`read` 的结尾）
- Modify: `coreman/api/routers/me_assistant.py`（`job_list`、`recent_approvals` 的结尾）
- Test: `tests/api/test_my_assistant_access_log.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `access_log.record`（落库）；3C 的七个读取接口与它们记的（动作, 对象）；`tests.api.my_assistant_helpers.URL / owned`；`notes.write`、`followups.add`。
- Produces：
  - 四个读接口在返回前提交（`index`、`read`、`job_list`、`recent_approvals`）。
  - `tests/api/test_my_assistant_access_log.py::READS: dict[str, list[tuple[str, str]]]`——前缀下每一条 GET 路由 → 它应当留下的（动作, 对象）；新增 GET 路由不登记进来，用例失败。Task 4、Task 8 各往里加一项。
  - 同一个文件里的 `async def _stored(db_session) -> list[tuple[str, str, str, uuid.UUID | None]]`（动作, 对象, 操作人类别, 操作人）与 `async def _seed(db_session, bot) -> None`。

- [ ] **Step 1: 写失败测试**

`tests/api/test_my_assistant_access_log.py`（新建）：

```python
"""「我的助理」页的访问记录：每个读密文的接口各留下说好的那几行，行里没有任何内容。

新增一条 GET 路由而不登记进 `READS`，第一个用例就失败：先想清楚它解不解密、该记什么。
"""

from __future__ import annotations

import uuid
from datetime import date

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, notes
from coreman.core.db.models import Bot
from tests.api.my_assistant_helpers import URL, owned
from tests.integration.assistant_helpers import SECRET

pytestmark = pytest.mark.usefixtures("assistant_master")
PAGE = "规则/口吻"
# GET 路由（去掉前缀）→ 调一次应当留下的（动作, 对象）。
READS: dict[str, list[tuple[str, str]]] = {
    # 概览只有元数据：状态、运行时、轮次、轮询健康。
    "": [],
    "/attention": [
        ("approvals.read", "pending"),
        ("jobs.read", "blocked"),
        ("followups.read", "today"),
    ],
    "/jobs": [("jobs.read", "list")],
    "/followups": [("followups.read", "open")],
    "/approvals/recent": [("approvals.read", "recent")],
    "/notes": [("notes.index", "")],
    "/notes/page": [("notes.read", "page")],
    "/settings": [("settings.read", "")],
}
PARAMS = {"/notes/page": {"path": PAGE}}


async def _stored(db_session: AsyncSession) -> list[tuple[str, str, str, uuid.UUID | None]]:
    rows = await db_session.execute(
        text("SELECT action, target, actor_kind, actor_id FROM assistant_access_log ORDER BY id")
    )
    return [(row[0], row[1], row[2], row[3]) for row in rows.all()]


async def _seed(db_session: AsyncSession, bot: Bot) -> None:
    """一页知识页、一条今天到期的待跟进；路径、标题、摘要、正文里都带着机密片段。"""
    await notes.write(
        db_session,
        bot.id,
        PAGE,
        body=f"回复要短。{SECRET}",
        summary=f"口吻的规矩 {SECRET}",
        title=f"口吻 {SECRET}",
    )
    await followups.add(
        db_session, bot.id, category="remind", title=f"续约 {SECRET}", due_on=date(2000, 1, 1)
    )
    await db_session.commit()


def test_every_get_route_under_the_prefix_is_in_the_table(app: FastAPI) -> None:
    found = {
        route.path.removeprefix(URL)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith(URL) and "GET" in route.methods
    }
    assert found == set(READS)


async def test_each_read_leaves_exactly_its_records(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owned(client, db_session)
    await _seed(db_session, bot)
    for path, expected in READS.items():
        before = len(await _stored(db_session))
        response = await client.get(f"{URL}{path}", params=PARAMS.get(path))
        assert response.status_code == 200, (path, response.text)
        added = (await _stored(db_session))[before:]
        assert [(action, target) for action, target, _, _ in added] == expected, path
        assert all(kind == "owner" and actor == owner.id for _, _, kind, actor in added), path


async def test_the_log_holds_no_content(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _seed(db_session, bot)
    for path in READS:
        await client.get(f"{URL}{path}", params=PARAMS.get(path))
    logged = " ".join(f"{action} {target}" for action, target, _, _ in await _stored(db_session))
    assert logged
    # 正文、摘要、标题里的机密片段，路径的两段，待跟进的标题：一样都不在。
    for piece in (SECRET, "规则", "口吻", "续约"):
        assert piece not in logged
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_my_assistant_access_log.py -q`
Expected: FAIL：`test_each_read_leaves_exactly_its_records` 在 `/jobs` 处断言失败（`[] != [("jobs.read", "list")]`）——那四个接口记了却没提交，行随会话关闭被丢弃。另外两个用例通过。

- [ ] **Step 3: 四个读接口在返回前提交**

`coreman/api/routers/me_assistant_notes.py`，`index` 的最后三行

```python
    locked = not await sealed.key_ready(session, own.bot.id)
    await access_log.record(session, own.bot.id, own.actor.id, "notes.index", "")
    return {"code": 0, "data": {"items": [_entry(entry) for entry in entries], "locked": locked}}
```

换成

```python
    locked = not await sealed.key_ready(session, own.bot.id)
    await access_log.record(session, own.bot.id, own.actor.id, "notes.index", "")
    data = {"items": [_entry(entry) for entry in entries], "locked": locked}
    # 访问记录与这次读取同一个事务：提交不了，内容也不交出去。
    await session.commit()
    return {"code": 0, "data": data}
```

同一个文件，`read` 的最后三行

```python
    # 路径本身是个人内容，不进访问记录。
    await access_log.record(session, own.bot.id, own.actor.id, "notes.read", "page")
    return {"code": 0, "data": _entry(page)}
```

换成

```python
    # 路径本身是个人内容，不进访问记录。
    await access_log.record(session, own.bot.id, own.actor.id, "notes.read", "page")
    data = _entry(page)
    # 访问记录与这次读取同一个事务：提交不了，内容也不交出去。
    await session.commit()
    return {"code": 0, "data": data}
```

`coreman/api/routers/me_assistant.py`，`job_list` 里从 `await access_log.record(…, "jobs.read", "list")` 到函数结尾

```python
    await access_log.record(session, own.bot.id, own.actor.id, "jobs.read", "list")
    return {
        "code": 0,
        "data": {
            "active": [jobs.view(row, full=True) for row in active],
            "recent": [jobs.view(row, full=True) for row in recent],
        },
    }
```

换成

```python
    await access_log.record(session, own.bot.id, own.actor.id, "jobs.read", "list")
    data = {
        "active": [jobs.view(row, full=True) for row in active],
        "recent": [jobs.view(row, full=True) for row in recent],
    }
    # 访问记录与这次读取同一个事务：提交不了，内容也不交出去。
    await session.commit()
    return {"code": 0, "data": data}
```

同一个文件，`recent_approvals` 的最后两行

```python
    await access_log.record(session, own.bot.id, own.actor.id, "approvals.read", "recent")
    return {"code": 0, "data": {"items": [owner_view.settled_view(row) for row in rows]}}
```

换成

```python
    await access_log.record(session, own.bot.id, own.actor.id, "approvals.read", "recent")
    data = {"items": [owner_view.settled_view(row) for row in rows]}
    # 访问记录与这次读取同一个事务：提交不了，内容也不交出去。
    await session.commit()
    return {"code": 0, "data": data}
```

（另外三个读接口——`me_assistant_followups.listed`、`me_assistant.attention`、`me_assistant.settings_view`——3C 已经在 `record` 之后提交，不用动。输出一律在提交之前算好：提交之后不再碰会话里的对象。）

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/api/test_my_assistant_access_log.py -q`
Expected: PASS（3 个用例）。

Run: `uv run pytest tests/api -q -k "my_assistant"`
Expected: PASS（3C 的用例不受影响）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman && uv run alembic check`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/api/routers/me_assistant.py coreman/api/routers/me_assistant_notes.py \
  tests/api/test_my_assistant_access_log.py
git commit -m "我的助理页的读取都落访问记录：四个读接口在返回前提交，记录里没有任何内容"
```

---

### Task 3: 页面之外的解密——对话记录、任务详情、管理命令，以及台账

**Files:**
- Modify: `coreman/api/deps.py`（`via_bot_token` 之后）
- Modify: `coreman/api/routers/me_assistant_common.py`（导入、`DEPENDENCIES`）
- Modify: `coreman/api/routers/chat_logs.py`（导入；`list_chat_logs`、`get_chat_log` 的结尾）
- Modify: `coreman/api/routers/dev.py`（导入；`get_task` 的结尾）
- Modify: `coreman/core/assistants/keys.py`（`rewrap`）
- Modify: `coreman/cli.py`（导入；`_assistant_keys_rewrap`）
- Modify: `tests/api/my_assistant_helpers.py`（加 `as_cli`）
- Modify: `tests/api/test_my_assistant_guard.py`、`tests/api/test_assistant_chat_logs.py`、`tests/integration/test_assistant_keys_rewrap.py`
- Test: `tests/unit/test_assistant_access_ledger.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `access_log.record / record_opened / bots_of`；`sealed.is_opened`；`request.state.admin_session`（`AdminSession`，列 `auth_method`）；3C 的 `DEPENDENCIES`、`tests/api/test_my_assistant_guard.py` 的 `CASES` / `_call` / `_seeded`。
- Produces：
  - `def deps.via_cli(request: Request) -> bool`
  - `async def me_assistant_common.in_person(request: Request, _actor: User = Depends(current_user)) -> None`（管理命令的会话 → 403，`errors[0].type == "cli_session_refused"`）；`DEPENDENCIES = [Depends(verify_csrf), Depends(no_store), Depends(in_person)]`
  - `async def keys.rewrap(session: AsyncSession, cipher: Cipher, *, touched: list[uuid.UUID] | None = None) -> tuple[int, int]`
  - `async def tests.api.my_assistant_helpers.as_cli(client, db_session, user) -> None`
  - `tests/unit/test_assistant_access_ledger.py`：`DECRYPTING`、`EXEMPT`、`def unrecorded(relative: str, tree: ast.AST) -> set[tuple[str, str]]`。之后的任务新增解密位置时，要么在同一个函数里记，要么登记进 `EXEMPT`。

- [ ] **Step 1: 写测试公共件**

`tests/api/my_assistant_helpers.py`：导入区加

```python
import secrets
from datetime import UTC, datetime, timedelta

from coreman.api.security import CSRF_COOKIE, SESSION_COOKIE, sign_session_id
from coreman.core.db.models import AdminSession
```

（`Bot`、`User` 的导入已有；把 `AdminSession` 并进同一行。）文件末尾追加：

```python
async def as_cli(client: httpx.AsyncClient, db_session: AsyncSession, user: User) -> None:
    """让之后的请求带着管理命令建的那种会话（`coreman … --as 登录名`）：`auth_method = "cli"`。"""
    now = datetime.now(UTC)
    row = AdminSession(
        user_id=user.id,
        auth_method="cli",
        user_agent="coreman-cli",
        expires_at=now + timedelta(hours=1),
        last_seen_at=now,
    )
    db_session.add(row)
    await db_session.commit()
    csrf = secrets.token_urlsafe(16)
    # 与 tests/api/conftest.py 的 _attach_session 用同一把测试密钥。
    client.cookies.set(
        SESSION_COOKIE, sign_session_id("unit-test-session-secret-0123456789", row.id)
    )
    client.cookies.set(CSRF_COOKIE, csrf)
    client.headers["X-CSRF-Token"] = csrf
```

- [ ] **Step 2: 写失败测试**

`tests/unit/test_assistant_access_ledger.py`（新建）：

```python
"""运行时路径之外的解密台账：每一处都要在同一个函数里记访问日志，或写明为什么不用记。

按语法树扫 `coreman/` 下的全部代码，认两类位置：

1. 任何地方带 `viewer=` 的 `open_row` / `open_rows` 调用——「只对查看者本人还原」只在把内容交给
   人看的时候才用。
2. 接口层（`coreman/api/`）与管理命令（`coreman/cli.py`）里对 `DECRYPTING` 中函数的调用——它们
   返回解密后的个人内容，或动了数据密钥。

所在的函数（最内层的 `def`）里必须有 `access_log.record(...)` 或 `access_log.record_opened(...)`；
确实不用记的登记在 `EXEMPT` 里并写明理由。新增一处解密而两样都没做，这里失败。

扫不到的：`session.get` / `session.scalar` 取整行时的自动还原（那一类由
`tests/unit/test_assistant_sealed_ledger.py` 的整行读取台账守着），以及经 `coreman/admin_cli/`
以某位成员的身份调接口——后者由接口那一侧处理（`deps.via_cli`）。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# 接口层与管理命令里，调用这些「模块.函数」就是在解密个人内容或动数据密钥。
DECRYPTING = frozenset(
    {
        ("notes", "index"),
        ("notes", "read"),
        ("notes", "search"),
        ("notes", "dump"),
        ("followups", "list_"),
        ("followups", "due"),
        ("followups", "get"),
        ("jobs", "list_"),
        ("owner_view", "approvals"),
        ("transfer", "plan"),
        ("transfer", "export"),
        ("sealed", "open_row"),
        ("sealed", "open_rows"),
        ("sealed", "open_value"),
        ("assistant_keys", "rewrap"),
    }
)
OUTER_LAYERS = ("coreman/api/", "coreman/cli.py")
RECORDERS = frozenset({("access_log", "record"), ("access_log", "record_opened")})
# （文件, 函数）→ 为什么这一处解密不用记访问日志。
EXEMPT = {
    # 幂等重放时比对已入队的明文载荷，只回编号与状态，不输出内容
    ("coreman/api/routers/infra_push.py", "push"): "compares, never outputs",
    # 核心函数：由调用它的路由记账（路由受上面第 2 类约束）
    ("coreman/core/assistants/owner_view.py", "approvals"): "recorded by the calling route",
}


def _dotted(node: ast.Call) -> tuple[str, str]:
    """`模块.函数(...)` 调用的（模块, 函数）；不是这种形状的返回空串。"""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id, func.attr
    return "", ""


def _decrypts(node: ast.Call, relative: str) -> bool:
    attr = node.func.attr if isinstance(node.func, ast.Attribute) else ""
    if attr in ("open_row", "open_rows") and any(kw.arg == "viewer" for kw in node.keywords):
        return True
    return relative.startswith(OUTER_LAYERS) and _dotted(node) in DECRYPTING


def _own_calls(function: ast.AST) -> Iterator[ast.Call]:
    """这个函数自己的调用；嵌套定义的函数各算各的。"""
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def unrecorded(relative: str, tree: ast.AST) -> set[tuple[str, str]]:
    """这个文件里解密了个人内容、却没有在同一个函数里记访问日志的（文件, 函数）。"""
    found: set[tuple[str, str]] = set()
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = list(_own_calls(function))
        if not any(_decrypts(call, relative) for call in calls):
            continue
        if not any(_dotted(call) in RECORDERS for call in calls):
            found.add((relative, function.name))
    return found


def _scan() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted((ROOT / "coreman").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        found |= unrecorded(relative, ast.parse(path.read_text(encoding="utf-8")))
    return found


def test_every_decryption_outside_the_runtime_is_recorded_or_exempt() -> None:
    # 两头都查：漏记的会多出来；豁免清单里已经不存在的条目也要删掉。
    assert _scan() == set(EXEMPT)


def test_the_ledger_catches_a_route_that_forgets_to_record() -> None:
    forgetful = """
async def leak(own, session):
    page = await notes.read(session, own.bot.id, "x")
    return {"data": page}


async def careful(own, session):
    page = await notes.read(session, own.bot.id, "x")
    await access_log.record(session, own.bot.id, own.actor.id, "notes.read", "page")
    return {"data": page}


async def peek(session, rows, actor):
    await sealed.open_rows(session, rows, viewer=actor.id)


async def outer(own, session):
    async def inner():
        return await notes.index(session, own.bot.id)

    await access_log.record(session, own.bot.id, own.actor.id, "notes.index", "")
    return await inner()
"""
    tree = ast.parse(forgetful)
    assert unrecorded("coreman/api/routers/example.py", tree) == {
        ("coreman/api/routers/example.py", "leak"),
        ("coreman/api/routers/example.py", "peek"),
        # 记账写在外层函数里不算：解密发生在 inner 里。
        ("coreman/api/routers/example.py", "inner"),
    }
    # 同样的调用放在核心模块里：只有带 viewer= 的那一处算。
    assert unrecorded("coreman/core/assistants/example.py", tree) == {
        ("coreman/core/assistants/example.py", "peek")
    }


def test_sessions_made_by_the_admin_cli_stay_recognisable() -> None:
    # `deps.via_cli` 靠这个取值认出管理命令建的会话：两边要一起改。
    client = (ROOT / "coreman/admin_cli/client.py").read_text(encoding="utf-8")
    deps = (ROOT / "coreman/api/deps.py").read_text(encoding="utf-8")
    assert 'auth_method="cli"' in client
    assert 'auth_method == "cli"' in deps
```

`tests/api/test_my_assistant_guard.py`：导入里把 `from tests.api.my_assistant_helpers import URL, as_bot_token, code` 换成 `from tests.api.my_assistant_helpers import URL, as_bot_token, as_cli, code`，文件末尾追加：

```python
async def test_a_cli_session_gets_nothing_even_as_the_owner(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    # 管理命令能以任意成员的身份建会话：那不是本人在场，整个前缀都不受理。
    seeded = await _seeded(db_session)
    await as_cli(client, db_session, seeded.owner)
    for method, template in CASES:
        response = await _call(client, seeded, method, template)
        assert response.status_code == 403, (method, template)
        assert code(response) == "cli_session_refused" and SECRET not in response.text
    await seeded.intact(db_session)
```

`tests/api/test_assistant_chat_logs.py`：导入区加 `from sqlalchemy import text` 与 `from tests.api.my_assistant_helpers import as_cli`；`_items` 之后加

```python
async def _access(db_session: AsyncSession) -> list[tuple[str, str, str, uuid.UUID | None]]:
    rows = await db_session.execute(
        text("SELECT action, target, actor_kind, actor_id FROM assistant_access_log ORDER BY id")
    )
    return [(row[0], row[1], row[2], row[3]) for row in rows.all()]
```

`test_dev_task_page_only_decrypts_for_the_owner` 的末尾追加（管理员那一次什么都没解开，不记；所有者那一次记一行）：

```python
    recorded = await _access(db_session)
    assert [(action, kind, who) for action, _, kind, who in recorded] == [
        ("tasks.read", "owner", owner.id)
    ]
    assert recorded[0][1].startswith("rows=")
```

文件末尾追加：

```python
async def test_the_owner_reading_their_chat_logs_is_recorded(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id))
    await db_session.commit()
    await login_existing(client, db_session, owner)
    items = await _items(client)
    assert await _access(db_session) == [("chatlogs.read", "rows=1", "owner", owner.id)]
    assert (await client.get(f"/api/admin/chat-logs/{items[0]['id']}")).status_code == 200
    assert await _access(db_session) == [("chatlogs.read", "rows=1", "owner", owner.id)] * 2


async def test_reads_that_decrypt_nothing_leave_no_record(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    standard = await seed_standard(db_session)
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id))
    assert await chat_logs.insert_turn(
        db_session, _entry(standard, None, chat_type="group", task_id=2)
    )
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    for item in await _items(client):
        await client.get(f"/api/admin/chat-logs/{item['id']}")
    # 管理员读不到助理的内容；普通员工的记录不属于访问记录的范围。
    assert await _access(db_session) == []


async def test_a_cli_session_acting_as_the_owner_is_recorded_as_system(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id))
    await db_session.commit()
    await as_cli(client, db_session, owner)
    items = await _items(client)
    assert items[0]["message_preview"] == f"问：{SECRET}"
    # 不是所有者本人在场：记成 system，操作人为空，动作带 cli 前缀。
    assert await _access(db_session) == [("cli.chatlogs", "rows=1", "system", None)]
```

`tests/integration/test_assistant_keys_rewrap.py`：

导入区加 `import uuid`、`from typing import Any`、`from sqlalchemy import select, text`（替换原来的 `from sqlalchemy import select`）与 `from tests.integration.assistant_helpers import seed_assistant`。

`test_rewrap_moves_keys_to_the_current_master` 里，把

```python
    assert await keys.rewrap(db_session, rotated) == (0, 2)
```

换成

```python
    touched: list[uuid.UUID] = []
    assert await keys.rewrap(db_session, rotated, touched=touched) == (0, 2)
    # 只报真的重新包裹了的那两把；已销毁的不算。
    assert set(touched) == {owners[0].id, owners[1].id}
```

`test_command_only_counts_unless_applied` 里，把开头的

```python
    owner = User(login_name="u0", display_name="张三")
    db_session.add(owner)
    await db_session.flush()
    await keys.ensure_key(db_session, Cipher(OLD, key_id="k0"), owner.id)
```

换成

```python
    bot, owner = await seed_assistant(db_session)
    # 下面的 stored_kid 会让会话里的对象过期：之后还要用的编号先取出来。
    bot_id = bot.id
    await keys.ensure_key(db_session, Cipher(OLD, key_id="k0"), owner.id)

    async def logged() -> list[tuple[Any, ...]]:
        rows = await db_session.execute(
            text(
                "SELECT bot_id, actor_id, actor_kind, action, target"
                " FROM assistant_access_log ORDER BY id"
            )
        )
        return [tuple(row) for row in rows.all()]
```

在 `assert await stored_kid() == "k0"` 之后加

```python
    # 只统计的那一次不落库，也就不留访问记录。
    assert await logged() == []
```

在最后一行 `assert await stored_kid() == "k1"` 之后加

```python
    # 不看内容的密钥操作，也让所有者在访问记录里看得到。
    assert await logged() == [(bot_id, None, "system", "cli.rewrap", "")]
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_access_ledger.py tests/api/test_my_assistant_guard.py tests/api/test_assistant_chat_logs.py tests/integration/test_assistant_keys_rewrap.py -q`
Expected: FAIL：
- 台账：`_scan()` 多出 `chat_logs.py` 的 `list_chat_logs`、`get_chat_log`，`dev.py` 的 `get_task`，`cli.py` 的 `_assistant_keys_rewrap`；`test_sessions_made_by_the_admin_cli_stay_recognisable` 在 `deps.py` 那一句失败。
- 守卫：CLI 会话的用例拿到 200 而不是 403。
- 对话记录：三条新用例与任务详情那几行断言失败（一行记录都没有）。
- 密钥轮换：`rewrap() got an unexpected keyword argument 'touched'`。

- [ ] **Step 4: 认出管理命令的会话，并让「我的助理」前缀拒绝它**

`coreman/api/deps.py`：`via_bot_token` 之后加

```python
def via_cli(request: Request) -> bool:
    """本次请求的管理台会话是不是管理命令建的（`coreman … --as 登录名`）。

    管理命令以指定成员的身份建一个真的会话（见 `coreman/admin_cli/client.py`），权限与那位成员
    一致；但操作的人在服务器上，不是成员本人。只对本人开放的个人内容据此区分。
    """
    login = getattr(request.state, "admin_session", None)
    return login is not None and login.auth_method == "cli"
```

`coreman/api/routers/me_assistant_common.py`：

导入里把

```python
from fastapi import Depends, Response
```

换成

```python
from fastapi import Depends, Request, Response
```

把

```python
from coreman.api.deps import get_session
```

换成

```python
from coreman.api.deps import current_user, get_session, via_cli
```

把

```python
# 路由级依赖：写操作先过 CSRF；成功的响应都带 no-store。
DEPENDENCIES = [Depends(verify_csrf), Depends(no_store)]
```

换成

```python
async def in_person(request: Request, _actor: User = Depends(current_user)) -> None:
    """管理命令以某位成员的身份建的会话不是本人在场：整个前缀都不受理。

    否则服务器上的人能以所有者的身份读他的知识库，还能导入置顶页——等于替他给助理写规则。
    依赖 `current_user`：会话是它认出来并放进 `request.state` 的。
    """
    if via_cli(request):
        raise ApiError(
            403, 403, "这些接口只对本人在管理台里的登录开放", [{"type": "cli_session_refused"}]
        )


# 路由级依赖：写操作先过 CSRF；成功的响应都带 no-store；管理命令的会话一律拒绝。
DEPENDENCIES = [Depends(verify_csrf), Depends(no_store), Depends(in_person)]
```

- [ ] **Step 5: 对话记录与任务详情记账**

`coreman/api/routers/chat_logs.py`：

导入里把 `from coreman.api.deps import current_user, get_session, via_bot_token` 换成 `from coreman.api.deps import current_user, get_session, via_bot_token, via_cli`，把 `from coreman.core.assistants import sealed` 换成 `from coreman.core.assistants import access_log, sealed`。

`list_chat_logs` 里从 `await sealed.open_rows(…)` 到函数结尾

```python
    await sealed.open_rows(session, rows, viewer=actor.id)
    names = await bot_names(session, (r.bot_id for r in rows))
    return {
        "code": 0,
        "data": {**page, "items": [chat_log_out(r, bot_name=names.get(r.bot_id)) for r in rows]},
    }
```

换成

```python
    await sealed.open_rows(session, rows, viewer=actor.id)
    names = await bot_names(session, (r.bot_id for r in rows))
    data = {**page, "items": [chat_log_out(r, bot_name=names.get(r.bot_id)) for r in rows]}
    # 真的为查看者还原了助理的内容才记；管理命令以所有者身份来的记成 system，不留操作人。
    cli = via_cli(request)
    if await access_log.record_opened(
        session, rows, None if cli else actor.id, "cli.chatlogs" if cli else "chatlogs.read"
    ):
        # 与这次读取同一个事务：提交不了，内容也不交出去。
        await session.commit()
    return {"code": 0, "data": data}
```

`get_chat_log` 里从 `await sealed.open_row(…)` 到函数结尾

```python
    await sealed.open_row(session, row, viewer=actor.id)
    names = await bot_names(session, [row.bot_id])
    data = chat_log_out(row, full=True, bot_name=names.get(row.bot_id))
    data["business_tokens"] = await business_tokens(session, row.task_id)
    return {"code": 0, "data": data}
```

换成

```python
    await sealed.open_row(session, row, viewer=actor.id)
    names = await bot_names(session, [row.bot_id])
    data = chat_log_out(row, full=True, bot_name=names.get(row.bot_id))
    data["business_tokens"] = await business_tokens(session, row.task_id)
    cli = via_cli(request)
    if await access_log.record_opened(
        session, [row], None if cli else actor.id, "cli.chatlogs" if cli else "chatlogs.read"
    ):
        await session.commit()
    return {"code": 0, "data": data}
```

`coreman/api/routers/dev.py`：

导入里把 `from coreman.api.deps import get_session, via_bot_token` 换成 `from coreman.api.deps import get_session, via_bot_token, via_cli`，把 `from coreman.core.assistants import sealed` 换成 `from coreman.core.assistants import access_log, sealed`。

`get_task` 里从 `await sealed.open_rows(…)` 到函数结尾

```python
    await sealed.open_rows(session, [task, stream, log], viewer=actor.id)
    now = datetime.now(UTC)
    return {
        "code": 0,
        "data": {
            "task": task_out(task),
            "stream": stream_out(stream, now) if stream else None,
            "chat_log": chat_log_out(log) if log else None,
        },
    }
```

换成

```python
    await sealed.open_rows(session, [task, stream, log], viewer=actor.id)
    now = datetime.now(UTC)
    data = {
        "task": task_out(task),
        "stream": stream_out(stream, now) if stream else None,
        "chat_log": chat_log_out(log) if log else None,
    }
    cli = via_cli(request)
    if await access_log.record_opened(
        session, [task, stream, log], None if cli else actor.id, "cli.tasks" if cli else "tasks.read"
    ):
        await session.commit()
    return {"code": 0, "data": data}
```

- [ ] **Step 6: `assistant-keys rewrap` 记账**

`coreman/core/assistants/keys.py`，`rewrap` 的签名与文档字符串

```python
async def rewrap(session: AsyncSession, cipher: Cipher) -> tuple[int, int]:
    """主密钥轮换之后：把不是当前主密钥包裹的数据密钥重新包裹一遍。

    内容密文只认数据密钥，不用动。返回（已是当前主密钥的行数，本次重新包裹的行数）；
    调用方提交才生效。全部跑完后，旧主密钥才能从 `MASTER_KEYS_PREVIOUS` 里移除。
    """
```

换成

```python
async def rewrap(
    session: AsyncSession, cipher: Cipher, *, touched: list[uuid.UUID] | None = None
) -> tuple[int, int]:
    """主密钥轮换之后：把不是当前主密钥包裹的数据密钥重新包裹一遍。

    内容密文只认数据密钥，不用动。返回（已是当前主密钥的行数，本次重新包裹的行数）；
    调用方提交才生效。全部跑完后，旧主密钥才能从 `MASTER_KEYS_PREVIOUS` 里移除。

    Args:
        touched: 给了就把本次重新包裹的数据密钥属于谁（用户编号）追加进去：管理命令据此给
            这些人的助理各记一条访问记录
    """
```

循环体里，在

```python
        changed += 1
```

之后加

```python
        if touched is not None:
            touched.append(row.user_id)
```

`coreman/cli.py`：

导入区加 `import uuid`（放在 `import sys` 之后），把

```python
from coreman.core.assistants import keys as assistant_keys
```

换成

```python
from coreman.core.assistants import access_log
from coreman.core.assistants import keys as assistant_keys
```

`_assistant_keys_rewrap` 里，把

```python
        async with make_session_factory(engine)() as session:
            current, changed = await assistant_keys.rewrap(session, settings.build_cipher())
            if apply:
                await session.commit()
            else:
                await session.rollback()
```

换成

```python
        async with make_session_factory(engine)() as session:
            touched: list[uuid.UUID] = []
            current, changed = await assistant_keys.rewrap(
                session, settings.build_cipher(), touched=touched
            )
            if apply:
                # 这条命令解开并重新包裹了数据密钥，没有读任何内容：照样让所有者看得到。
                # 与重新包裹在同一个事务里提交。
                for bot_id in await access_log.bots_of(session, touched):
                    await access_log.record(session, bot_id, None, "cli.rewrap")
                await session.commit()
            else:
                await session.rollback()
```

- [ ] **Step 7: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_access_ledger.py tests/api/test_my_assistant_guard.py tests/api/test_assistant_chat_logs.py tests/integration/test_assistant_keys_rewrap.py -q`
Expected: PASS。

若台账列出了这里没有提到的位置（比如 3B 先落地，带来了新的 `viewer=` 调用或接口层的解密调用）：逐个决定——把内容交给人看的，在同一个函数里调 `access_log.record` / `record_opened`（动作不在 `ACTIONS` 里就加一个，并在 Task 9 的文案与用例里补上）；不输出内容的，登记进 `EXEMPT` 并写明理由。

Run: `uv run pytest tests/api -q -k "my_assistant or chat_logs or dev"`
Expected: PASS（对话记录、任务详情的输出没有变）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman && uv run alembic check`
Expected: 全部通过。

- [ ] **Step 8: 提交**

```bash
git add coreman/api/deps.py coreman/api/routers/me_assistant_common.py \
  coreman/api/routers/chat_logs.py coreman/api/routers/dev.py \
  coreman/core/assistants/keys.py coreman/cli.py \
  tests/api/my_assistant_helpers.py tests/api/test_my_assistant_guard.py \
  tests/api/test_assistant_chat_logs.py tests/integration/test_assistant_keys_rewrap.py \
  tests/unit/test_assistant_access_ledger.py
git commit -m "页面之外的解密也记访问记录：对话记录、任务详情、密钥轮换；管理命令的会话进不了我的助理接口"
```

---

### Task 4: 所有者查看访问记录、管理台的「最近一次非本人访问」、180 天清理

**Files:**
- Modify: `coreman/api/routers/me_assistant.py`（导入；文件末尾加一个接口）
- Modify: `coreman/api/routers/assistants_admin.py`（导入、文件头的说明、`list_assistants`）
- Modify: `coreman/runtime/scheduler/reaper.py`（导入、`run_retention` 的 `jobs`）
- Modify: `tests/api/test_my_assistant_access_log.py`、`tests/api/test_my_assistant_guard.py`、`tests/api/test_assistant_admin.py`、`tests/integration/test_scheduler_reaper.py`

**Interfaces:**
- Consumes: Task 1 的 `access_log.list_ / last_foreign / cleanup / MAX_LIST`、`Seen`；Task 2 的 `READS`、`_stored`、`_seed`；3C 的 `Mine`、`mine`、`CASES`。
- Produces：
  - `GET /api/me/assistant/access-log?limit=<1–200，默认 50>&before=<编号>` → `{"items": [{"id": int, "at": ISO 时间, "actor_kind": "owner" | "admin" | "system", "actor_name": str | null, "action": str, "target": str}…], "next_before": int | null}`（这一页装满时 `next_before` 是最后一行的编号）。看访问记录本身不解密任何东西，不记。
  - `GET /api/admin/assistants` 每条记录多 `last_non_owner_access_at`（ISO 时间或 `null`）。
  - `reaper.run_retention` 的返回值多一个键 `old_assistant_access_log`。

- [ ] **Step 1: 写失败测试**

`tests/api/test_my_assistant_access_log.py`：

导入区加 `from coreman.core.assistants import access_log, followups, notes`（替换原来的 `from coreman.core.assistants import followups, notes`）、`from tests.api.conftest import login_as`，并把 `from tests.api.my_assistant_helpers import URL, owned` 换成 `from tests.api.my_assistant_helpers import URL, code, owned`。

`READS` 里在 `"/settings": [("settings.read", "")],` 之后加：

```python
    # 访问记录本身只有编号、动作与时间：看它不解密任何东西。
    "/access-log": [],
```

文件末尾追加：

```python
LOG = f"{URL}/access-log"


async def test_the_owner_pages_through_their_own_records(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _seed(db_session, bot)
    for _ in range(3):
        await client.get(f"{URL}/notes")
    await access_log.record(db_session, bot.id, None, "cli.rewrap")
    await db_session.commit()
    first = await client.get(LOG, params={"limit": 2})
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    data = first.json()["data"]
    assert [(item["action"], item["actor_kind"], item["actor_name"]) for item in data["items"]] == [
        ("cli.rewrap", "system", None),
        ("notes.index", "owner", "张三"),
    ]
    assert set(data["items"][0]) == {"id", "at", "actor_kind", "actor_name", "action", "target"}
    assert data["next_before"] == data["items"][-1]["id"]
    rest = (await client.get(LOG, params={"before": data["next_before"]})).json()["data"]
    assert [item["action"] for item in rest["items"]] == ["notes.index", "notes.index"]
    assert rest["next_before"] is None
    # 看访问记录本身不算一次解密：还是那四行。
    assert len(await _stored(db_session)) == 4


async def test_nobody_reads_someone_elses_records(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _seed(db_session, bot)
    await client.get(f"{URL}/notes")
    # 另一位有自己助理的平台管理员：只看得到他自己的（空的）。
    await owned(client, db_session, role="platform_admin")
    mine_only = await client.get(LOG)
    assert mine_only.json()["data"] == {"items": [], "next_before": None}
    assert str(bot.id) not in mine_only.text
    # 没有助理的平台管理员：没有助理，也就没有记录可看。
    await login_as(client, db_session, role="platform_admin")
    refused = await client.get(LOG)
    assert refused.status_code == 404 and code(refused) == "no_assistant"


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"before": 0}, {"before": "x"}])
async def test_bad_paging_is_refused(
    client: httpx.AsyncClient, db_session: AsyncSession, params: dict[str, object]
) -> None:
    await owned(client, db_session)
    assert (await client.get(LOG, params=params)).status_code == 422
```

`tests/api/test_my_assistant_guard.py`：`CASES` 里在 `("PATCH", "/settings"): {"json": {"timezone": "UTC"}},` 之后加：

```python
    ("GET", "/access-log"): {},
```

`tests/api/test_assistant_admin.py`：导入区加 `from coreman.core.assistants import access_log`，文件末尾追加：

```python
async def test_the_list_shows_when_someone_other_than_the_owner_last_accessed(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="member", display_name="张三")
    bot = await _assistant(db_session, owner)
    await access_log.record(db_session, bot.id, owner.id, "notes.read", "page", kind="owner")
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    [item] = (await client.get("/api/admin/assistants")).json()["data"]["items"]
    # 所有者自己看自己的不算。
    assert item["last_non_owner_access_at"] is None

    await access_log.record(db_session, bot.id, None, "cli.rewrap")
    await db_session.commit()
    seen = await db_session.scalar(
        text('SELECT max("at") FROM assistant_access_log WHERE actor_kind = \'system\'')
    )
    response = await client.get("/api/admin/assistants")
    [item] = response.json()["data"]["items"]
    assert item["last_non_owner_access_at"] == seen.isoformat()
    # 管理员只看到时间：做了什么、对着什么，都不在这里。
    assert "cli.rewrap" not in response.text and "notes.read" not in response.text
```

`tests/integration/test_scheduler_reaper.py`：把对 `run_retention` 返回值的全等断言

```python
    assert counts == {
        "old_tasks": 1,
        "old_inbound_events": 2,
        "old_outbox": 2,
        "old_feishu_sent": 0,
        "old_observe_decisions": 0,
        "old_credential_requests": 0,
        "old_doc_consents": 0,
    }
```

换成

```python
    assert counts == {
        "old_tasks": 1,
        "old_inbound_events": 2,
        "old_outbox": 2,
        "old_feishu_sent": 0,
        "old_observe_decisions": 0,
        "old_credential_requests": 0,
        "old_doc_consents": 0,
        "old_assistant_access_log": 0,
    }
```

并在 `test_retention_drops_month_old_observe_decisions` 之后加：

```python
async def test_retention_drops_access_log_rows_after_180_days(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from coreman.core.db.models import AssistantAccessLog

    bot = await _bot(db_session)
    now = datetime.now(UTC)
    for age in (181, 179):
        db_session.add(
            AssistantAccessLog(
                bot_id=bot.id,
                actor_kind="system",
                action="cli.rewrap",
                at=now - timedelta(days=age),
            )
        )
    await db_session.commit()
    counts = await reaper.run_retention(make_session_factory(db_engine), now, batch=1)
    assert counts["old_assistant_access_log"] == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_my_assistant_access_log.py tests/api/test_my_assistant_guard.py tests/api/test_assistant_admin.py tests/integration/test_scheduler_reaper.py -q`
Expected: FAIL：两张路由台账各多出一条没有的路由（`/access-log`）；访问记录接口的用例 404；管理台列表 `KeyError: 'last_non_owner_access_at'`；保留期两条用例缺 `old_assistant_access_log`。

- [ ] **Step 3: 所有者查看访问记录的接口**

`coreman/api/routers/me_assistant.py`：

导入里把 `from fastapi import APIRouter, Depends, Request` 换成 `from fastapi import APIRouter, Depends, Query, Request`（3C 的导入若还带着别的名字，只是多加一个 `Query`）。

文件末尾追加：

```python
@router.get("/access-log")
async def access_entries(
    limit: int = Query(50, ge=1, le=access_log.MAX_LIST),
    before: int | None = Query(None, ge=1),
    own: Mine = Depends(mine),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """所有者看自己的访问记录：谁、什么时候、做了什么，新的在前。

    记录里只有编号、动作代码与时间，没有任何内容：看它不算一次解密，所以这个接口自己不记。
    """
    rows = await access_log.list_(session, own.bot.id, limit=limit, before=before)
    return {
        "code": 0,
        "data": {
            "items": [
                {
                    "id": row.id,
                    "at": row.at.isoformat(),
                    "actor_kind": row.actor_kind,
                    "actor_name": row.actor_name,
                    "action": row.action,
                    "target": row.target,
                }
                for row in rows
            ],
            # 这一页装满了才可能还有更早的。
            "next_before": rows[-1].id if len(rows) == limit else None,
        },
    }
```

- [ ] **Step 4: 管理台列表与保留期清理**

`coreman/api/routers/assistants_admin.py`：

在 `from coreman.core.assistants import …` 那一行的名字里加上 `access_log`。

文件头的说明里，把「按设置此刻是否在班），」换成「按设置此刻是否在班、最近一次非所有者访问个人内容的时间），」。

`list_assistants` 里，在 `now = utcnow()` 之前加：

```python
    # 最近一次非所有者访问个人内容的时间（管理命令等）：只取时间，动作与对象不出现在这里。
    foreign = await access_log.last_foreign(session)
```

`items.append({...})` 的字典里，在最后一个键 `"inbox_pending": waiting.get(bot_id, 0),` 之后加：

```python
                "last_non_owner_access_at": (
                    foreign[bot_id].isoformat() if bot_id in foreign else None
                ),
```

`coreman/runtime/scheduler/reaper.py`：

把 `from coreman.core.assistants import platform_turns, sealed` 换成 `from coreman.core.assistants import access_log, platform_turns, sealed`。

`run_retention` 的 `jobs` 字典里，在 `"old_doc_consents": doc_consents.cleanup,` 之后加：

```python
        # 个人助理的访问记录留 180 天。
        "old_assistant_access_log": access_log.cleanup,
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/api/test_my_assistant_access_log.py tests/api/test_my_assistant_guard.py tests/api/test_assistant_admin.py tests/integration/test_scheduler_reaper.py -q`
Expected: PASS。

Run: `uv run pytest tests/unit/test_assistant_access_ledger.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: PASS（新接口不解密；`select(AssistantAccessLog, …)` 不是内容表，整行读取的台账不变）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman && uv run alembic check`
Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add coreman/api/routers/me_assistant.py coreman/api/routers/assistants_admin.py \
  coreman/runtime/scheduler/reaper.py tests/api/test_my_assistant_access_log.py \
  tests/api/test_my_assistant_guard.py tests/api/test_assistant_admin.py \
  tests/integration/test_scheduler_reaper.py
git commit -m "所有者可以查看自己的访问记录；管理台只显示最近一次非本人访问的时间；记录保留 180 天"
```

---

### Task 5: 压缩包的格式与安全检查（`archive.py`）

**Files:**
- Create: `coreman/core/assistants/archive.py`
- Create: `tests/fakes/archives.py`
- Test: `tests/unit/test_assistant_archive.py`（新建）

**Interfaces:**
- Consumes: `notes.MAX_TOTAL_BYTES`、`notes.MAX_PAGES`；`followups.MAX_OPEN / MAX_TITLE / MAX_NOTES`；`FOLLOWUP_CATEGORIES`；`yaml.safe_load / safe_dump`。
- Produces：
  - `archive.MAX_ARCHIVE_BYTES = 20 * 1024 * 1024`、`MAX_UNPACKED_BYTES`（32MB）、`MAX_PAGE_FILES = 2000`、`MAX_RAW_ENTRIES = 4096`、`MAX_HEADER_CHARS = 4096`、`MAX_FOLLOWUP_ITEMS = 500`、`FOLLOWUPS_FILE = "followups.json"`、`FORMAT_VERSION = 1`。
  - `class ArchiveError(Exception)`（`code: str`、`detail: dict[str, Any]`，可能带 `name`）。代码：`archive_too_large`、`invalid_archive`、`too_many_entries`、`unpacked_too_large`、`unsafe_path`、`symlink_not_allowed`、`encrypted_entry`、`nested_archive`、`unsupported_file`、`not_utf8`、`invalid_followups`。
  - `@dataclass(frozen=True) class PageFile`（`path: str`、`title: str | None`、`summary: str | None`、`pinned: bool | None`、`body: str`、`problem: str | None = None`）；条目级的 `problem` 只有 `invalid_front_matter`。
  - `@dataclass(frozen=True) class FollowupItem`（`index: int`、`category: str`、`title: str`、`notes: str | None`、`review_on: date | None`、`due_on: date | None`、`snoozed: bool`、`problem: str | None = None`）；`problem` 取 `invalid_item`、`invalid_category`、`invalid_title`、`invalid_notes`、`invalid_status`、`invalid_date`。
  - `@dataclass(frozen=True) class Parsed`（`pages: list[PageFile]`、`followups: list[FollowupItem]`）。
  - `def parse(data: bytes) -> Parsed`；`def page_text(*, title: str, summary: str, pinned: bool, body: str) -> str`；`def followups_text(items: Iterable[Mapping[str, Any]]) -> str`；`def build(pages: Iterable[tuple[str, str]], followups_json: str | None) -> bytes`（`pages` 是（页的路径, 文件内容），文件名自动加 `.md`）。
  - `tests.fakes.archives`：`pack(files: dict[str, str | bytes]) -> bytes`、`special(name: str, kind: int) -> bytes`、`encrypted(name: str) -> bytes`。

**压缩包的格式（写进模块的文档字符串与 `docs/feishu.md`）**

- 一个 zip。每个 `.md` 文件是一页，页的路径是文件在包里的相对路径去掉结尾的 `.md`（`人/李四.md` → `人/李四`）；不剥掉最外层目录。
- 文件开头可选的 front matter：第一行 `---`，到下一个只有 `---` 的行为止，中间是一个 YAML 映射；只采用 `title`、`summary`、`pinned`。
- 根目录下可选的 `followups.json`：`{"version": 1, "items": [{"category": "decide" | "promised" | "waiting" | "remind", "title": "…", "notes": "…" | null, "review_on": "YYYY-MM-DD" | null, "due_on": "YYYY-MM-DD" | null, "status": "open" | "snoozed"}]}`；`category` 与 `title` 必填，其余可省。

**每一条安全检查**（任何一条不过，整个包拒绝）

| 检查 | 代码 |
|---|---|
| 压缩包本身 ≤ 20MB | `archive_too_large` |
| 是读得出中央目录的 zip；条目的数据没有损坏 | `invalid_archive` |
| 条目总数（含目录条目）≤ 4096，页面文件 ≤ 2000（目录条目与 `followups.json` 不计） | `too_many_entries` |
| 名字不是绝对路径，没有反斜杠、`.` / `..` 段、空段、控制字符、NUL（路径穿越） | `unsafe_path` |
| 不是符号链接 | `symlink_not_allowed` |
| 没有加密 | `encrypted_entry` |
| 后缀不是压缩包（`.zip` `.jar` `.tar` `.tgz` `.gz` `.bz2` `.xz` `.zst` `.7z` `.rar`），内容开头不是 zip 魔数（嵌套压缩包） | `nested_archive` |
| 是普通文件，且是 `.md` 或根目录的 `followups.json` | `unsupported_file` |
| 中央目录里写的大小合计 ≤ 32MB；实际读出的字节数再核一次 | `unpacked_too_large` |
| 内容是不含 NUL 的 UTF-8（开头的 BOM 去掉） | `not_utf8` |
| `followups.json` 是对象、`version` 是 1、`items` 是至多 500 项的数组 | `invalid_followups` |

- [ ] **Step 1: 写测试公共件**

`tests/fakes/archives.py`（新建）：

```python
"""造压缩包的测试公共件：普通的包、带文件类型位的条目、标了加密位的条目。"""

from __future__ import annotations

import io
import zipfile


def pack(files: dict[str, str | bytes]) -> bytes:
    """按给的顺序把（文件名 → 内容）打成 zip；名字以 `/` 结尾的是目录条目。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def special(name: str, kind: int) -> bytes:
    """只有一个条目的 zip，条目带着文件类型位（`stat.S_IFLNK`、`stat.S_IFIFO`……）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        info = zipfile.ZipInfo(name)
        info.external_attr = (kind | 0o777) << 16
        zf.writestr(info, "target")
    return buffer.getvalue()


def encrypted(name: str) -> bytes:
    """只有一个条目的 zip，中央目录里它的通用标志位第 0 位（加密）被置上。"""
    data = bytearray(pack({name: "x"}))
    at = data.rfind(b"PK\x01\x02")
    data[at + 8] |= 0x1
    return bytes(data)
```

- [ ] **Step 2: 写失败测试**

`tests/unit/test_assistant_archive.py`（新建）：

```python
"""导入 / 导出压缩包的格式与安全检查：每一条限制各有一例；导出的文件读回来与原样一致。"""

from __future__ import annotations

import io
import json
import stat
import zipfile
from datetime import date

import pytest

from coreman.core.assistants import archive
from coreman.core.assistants.archive import ArchiveError, FollowupItem, PageFile
from tests.fakes.archives import encrypted, pack, special


def code_of(data: bytes) -> str:
    with pytest.raises(ArchiveError) as refused:
        archive.parse(data)
    return refused.value.code


def test_markdown_files_become_pages_and_the_header_is_optional() -> None:
    parsed = archive.parse(
        pack(
            {
                "人/李四.md": (
                    "---\ntitle: 李四\nsummary: 采购负责人\npinned: true\ntags: [a, b]\n---\n"
                    "正文第一行\n"
                ),
                "规则/口吻.md": "没有头的一页\n",
                "日志/": "",
                "分隔线.md": "---\n只是一条分隔线\n---\n后面还有\n",
                "带签名.md": "﻿---\ntitle: 2026\n---\n正文",
                "空的头.md": "---\n---\n正文",
            }
        )
    )
    assert parsed.followups == []
    assert parsed.pages == [
        # 其余键（tags）忽略；收尾的 --- 之后全是正文。
        PageFile("人/李四", "李四", "采购负责人", True, "正文第一行\n"),
        PageFile("规则/口吻", None, None, None, "没有头的一页\n"),
        # 两条 --- 之间不是映射：整篇都是正文。
        PageFile("分隔线", None, None, None, "---\n只是一条分隔线\n---\n后面还有\n"),
        # 开头的 BOM 去掉；数字写的标题按字符串用。
        PageFile("带签名", "2026", None, None, "正文"),
        PageFile("空的头", None, None, None, "正文"),
    ]


@pytest.mark.parametrize(
    "header",
    ["title: [1, 2]", "pinned: yes please", "title: true", "summary: {a: b}", 'title: "没收尾'],
)
def test_a_bad_header_marks_only_that_file(header: str) -> None:
    parsed = archive.parse(pack({"坏.md": f"---\n{header}\n---\n正文", "好.md": "正文"}))
    assert parsed.pages[0] == PageFile("坏", None, None, None, "", "invalid_front_matter")
    assert parsed.pages[1] == PageFile("好", None, None, None, "正文")


@pytest.mark.parametrize(
    "name",
    ["../外面.md", "a/../../b.md", "/etc/x.md", "a\\b.md", "a/./b.md", "a//b.md", "响\x07铃.md"],
)
def test_names_that_could_leave_the_archive_root_are_refused(name: str) -> None:
    assert code_of(pack({"好.md": "正文", name: "正文"})) == "unsafe_path"


def test_symlinks_and_other_special_files_are_refused() -> None:
    assert code_of(special("链接.md", stat.S_IFLNK)) == "symlink_not_allowed"
    assert code_of(special("目录链接/", stat.S_IFLNK)) == "symlink_not_allowed"
    assert code_of(special("管道.md", stat.S_IFIFO)) == "unsupported_file"


def test_encrypted_entries_are_refused() -> None:
    assert code_of(encrypted("密.md")) == "encrypted_entry"


@pytest.mark.parametrize("name", ["里面.zip", "备份.tar.gz", "a/b.7z", "X.ZIP"])
def test_nested_archives_are_refused_by_name(name: str) -> None:
    assert code_of(pack({name: b"whatever"})) == "nested_archive"


def test_a_renamed_archive_is_refused_by_content() -> None:
    assert code_of(pack({"伪装.md": pack({"x.md": "正文"})})) == "nested_archive"


@pytest.mark.parametrize("name", ["照片.png", "笔记.txt", "子目录/followups.json", ".DS_Store"])
def test_anything_but_markdown_and_the_root_followups_file_is_refused(name: str) -> None:
    assert code_of(pack({name: "x"})) == "unsupported_file"


@pytest.mark.parametrize("content", ["中文".encode("gbk"), b"\xff\xfe\x00\x00", b"a\x00b"])
def test_text_that_is_not_utf8_is_refused(content: bytes) -> None:
    assert code_of(pack({"页.md": content})) == "not_utf8"


def test_the_number_of_page_files_is_capped() -> None:
    most = {f"p/{n}.md": "x" for n in range(archive.MAX_PAGE_FILES)}
    # 目录条目与 followups.json 不占页面文件的名额。
    full = pack({"p/": "", **most, "followups.json": json.dumps({"version": 1, "items": []})})
    assert len(archive.parse(full).pages) == archive.MAX_PAGE_FILES
    assert code_of(pack({**most, "多一个.md": "x"})) == "too_many_entries"


def test_the_raw_entry_count_is_capped_before_anything_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive, "MAX_RAW_ENTRIES", 3)
    assert code_of(pack({"a/": "", "b/": "", "c/": "", "d/": ""})) == "too_many_entries"


def test_the_unpacked_size_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive, "MAX_UNPACKED_BYTES", 1000)
    assert code_of(pack({"a.md": "字" * 200, "b.md": "字" * 200})) == "unpacked_too_large"
    assert len(archive.parse(pack({"a.md": "x" * 600, "b.md": "y" * 400})).pages) == 2
    # 中央目录里写的大小不可信：实际读的时候按读到的字节数再核一次。
    data = pack({"a.md": "x" * 600})
    with zipfile.ZipFile(io.BytesIO(data)) as zf, pytest.raises(ArchiveError) as refused:
        archive._read(zf, zf.infolist()[0], 599)
    assert refused.value.code == "unpacked_too_large"


def test_the_upload_itself_is_capped_and_must_be_a_zip(monkeypatch: pytest.MonkeyPatch) -> None:
    assert code_of(b"this is not a zip file") == "invalid_archive"
    assert code_of(pack({"a.md": "x"})[:40]) == "invalid_archive"
    monkeypatch.setattr(archive, "MAX_ARCHIVE_BYTES", 100)
    assert code_of(b"x" * 101) == "archive_too_large"


def test_followups_are_read_item_by_item() -> None:
    items = [
        {"category": "decide", "title": " 续约 ", "notes": "明年一月", "due_on": "2026-12-01"},
        {"category": "waiting", "title": "报价", "review_on": "2026-11-20", "status": "snoozed"},
        {"category": "remind", "title": "体检", "notes": None, "due_on": None, "extra": 1},
        {"category": "someday", "title": "x"},
        {"category": "decide", "title": "   "},
        {"category": "decide", "title": "字" * 201},
        {"category": "decide", "title": "x", "notes": "字" * 4001},
        {"category": "decide", "title": "x", "due_on": "12/01/2026"},
        {"category": "decide", "title": "x", "due_on": "2026-13-40"},
        {"category": "decide", "title": "x", "status": "done"},
        "不是对象",
    ]
    parsed = archive.parse(
        pack({"followups.json": json.dumps({"version": 1, "items": items}, ensure_ascii=False)})
    )
    assert parsed.pages == []
    assert parsed.followups[:3] == [
        FollowupItem(1, "decide", "续约", "明年一月", None, date(2026, 12, 1), False),
        FollowupItem(2, "waiting", "报价", None, date(2026, 11, 20), None, True),
        FollowupItem(3, "remind", "体检", None, None, None, False),
    ]
    assert [(item.index, item.problem) for item in parsed.followups[3:]] == [
        (4, "invalid_category"),
        (5, "invalid_title"),
        (6, "invalid_title"),
        (7, "invalid_notes"),
        (8, "invalid_date"),
        (9, "invalid_date"),
        (10, "invalid_status"),
        (11, "invalid_item"),
    ]


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"version": 2, "items": []}),
        json.dumps({"version": True, "items": []}),
        json.dumps({"version": 1, "items": {}}),
        json.dumps({"version": 1, "items": [{}] * 501}),
    ],
)
def test_a_malformed_followups_file_refuses_the_archive(content: str) -> None:
    assert code_of(pack({"followups.json": content})) == "invalid_followups"


@pytest.mark.parametrize(
    ("title", "summary", "pinned", "body"),
    [
        ("李四", "采购负责人", True, "正文\n第二行\n"),
        ("2026", "true", False, "---\n正文以分隔线开头\n---\n"),
        ("带: 冒号 # 井号", "引号 \" 与 ' 都有", False, "行尾是回车换行\r\n第二行\r\n"),
        ("- 像列表", "{像映射}", True, "\n\n前面有空行"),
        ("长" * 120, "长" * 300, False, "x"),
    ],
)
def test_an_exported_page_reads_back_unchanged(
    title: str, summary: str, pinned: bool, body: str
) -> None:
    text = archive.page_text(title=title, summary=summary, pinned=pinned, body=body)
    parsed = archive.parse(archive.build([("目录/页", text)], None))
    assert parsed.pages == [PageFile("目录/页", title, summary, pinned, body)]


def test_exported_followups_read_back_unchanged() -> None:
    items = [
        {
            "category": "waiting",
            "title": "报价",
            "notes": "等李四",
            "review_on": "2026-11-20",
            "due_on": None,
            "status": "snoozed",
        }
    ]
    data = archive.build([], archive.followups_text(items))
    assert archive.parse(data).followups == [
        FollowupItem(1, "waiting", "报价", "等李四", date(2026, 11, 20), None, True)
    ]


def test_nothing_is_ever_extracted_to_disk() -> None:
    # 包只在内存里读：模块里不该出现把条目解到磁盘上的调用。
    source = archive.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert ".extract(" not in text and ".extractall(" not in text
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_archive.py -q`
Expected: FAIL：收集时 `ImportError: cannot import name 'archive' from 'coreman.core.assistants'`。

- [ ] **Step 4: 实现**

`coreman/core/assistants/archive.py`（新建）：

```python
"""个人助理导入 / 导出用的压缩包：格式、解析与安全检查。

纯函数：不碰数据库，不往磁盘写任何东西，包只在内存里读。

格式（导入与导出相同）：

- 一个 zip。包里每个 `.md` 文件是一页，页的路径是文件在包里的相对路径去掉结尾的 `.md`
  （`人/李四.md` → `人/李四`）；不剥掉最外层目录。
- 文件开头可以有一段 YAML front matter：第一行是 `---`，到下一个只有 `---` 的行为止，中间
  是一个 YAML 映射。只采用三个键：`title`、`summary`（标量，按字符串用）与 `pinned`（布尔），
  其余键忽略。收尾的 `---` 之后的全部内容是正文；没有 front matter 的文件整篇是正文。
- 包根目录下可选的 `followups.json`：待跟进清单，语法见 `_followups`。

任何一条安全检查不过，整个包都拒绝（`ArchiveError`），见 `parse`。
"""

from __future__ import annotations

import io
import json
import re
import stat
import unicodedata
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import yaml

from coreman.core.assistants import followups, notes
from coreman.core.db.models import FOLLOWUP_CATEGORIES

MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
# 解包后的合计：知识库正文的 20MB 上限，加上 front matter 与待跟进清单的余量。正文合计是否
# 放得进知识库由 `transfer.plan` 按 `notes.MAX_TOTAL_BYTES` 另算。
MAX_UNPACKED_BYTES = notes.MAX_TOTAL_BYTES + 12 * 1024 * 1024
MAX_PAGE_FILES = notes.MAX_PAGES
# 包里条目总数（含目录条目）的硬上限：读中央目录之后、读任何内容之前就判断。
MAX_RAW_ENTRIES = 4096
MAX_HEADER_CHARS = 4096
MAX_FOLLOWUP_ITEMS = followups.MAX_OPEN
MAX_SHOWN_NAME = 200
FOLLOWUPS_FILE = "followups.json"
FORMAT_VERSION = 1
PAGE_SUFFIX = ".md"
NESTED_SUFFIXES = (".zip", ".jar", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".zst", ".7z", ".rar")
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_FENCE = "---"
_BOM = "﻿"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ArchiveError(Exception):
    """整个包被拒绝；`code` 是原因代码，`detail` 是所有者改正时用得上的数或文件名。"""

    def __init__(self, code: str, **detail: Any) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class _BadHeader(Exception):
    """front matter 不是合法的 YAML。"""


@dataclass(frozen=True)
class PageFile:
    """包里的一个页面文件。`problem` 不为空时这一个文件不能导入（其余字段无意义）。"""

    path: str
    title: str | None
    summary: str | None
    pinned: bool | None
    body: str
    problem: str | None = None


@dataclass(frozen=True)
class FollowupItem:
    """`followups.json` 里的一条；`index` 是它在清单里的位置（从 1 起）。"""

    index: int
    category: str
    title: str
    notes: str | None
    review_on: date | None
    due_on: date | None
    snoozed: bool
    problem: str | None = None


@dataclass(frozen=True)
class Parsed:
    pages: list[PageFile]
    followups: list[FollowupItem]


def _shown(name: str) -> str:
    return name[:MAX_SHOWN_NAME]


def _unsafe(name: str) -> bool:
    """文件名是否可能越出包的根：绝对路径、反斜杠、`.` / `..` 段、空段、控制字符。"""
    parts = name.rstrip("/").split("/")
    return (
        name.startswith("/")
        or "\\" in name
        or any(part in ("", ".", "..") for part in parts)
        or any(unicodedata.category(char)[0] == "C" for char in name)
    )


def _check(info: zipfile.ZipInfo) -> None:
    """只看中央目录里的一条，不读内容。"""
    name = info.filename
    # zipfile 在第一个 NUL 处截断文件名：原始文件名里有 NUL 的一律拒绝。
    if "\x00" in info.orig_filename or _unsafe(name):
        raise ArchiveError("unsafe_path", name=_shown(name))
    kind = stat.S_IFMT(info.external_attr >> 16)
    if kind == stat.S_IFLNK:
        raise ArchiveError("symlink_not_allowed", name=_shown(name))
    if info.flag_bits & 0x1:
        raise ArchiveError("encrypted_entry", name=_shown(name))
    if info.is_dir():
        return
    lowered = name.lower()
    if lowered.endswith(NESTED_SUFFIXES):
        raise ArchiveError("nested_archive", name=_shown(name))
    # 设备、管道之类的特殊文件，以及 .md 和根目录的 followups.json 之外的任何文件。
    if kind not in (0, stat.S_IFREG) or (
        name != FOLLOWUPS_FILE and not lowered.endswith(PAGE_SUFFIX)
    ):
        raise ArchiveError("unsupported_file", name=_shown(name))


def _read(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    """读一个文件，至多 `limit` 字节：中央目录里写的大小不可信，按实际读到的算。"""
    try:
        with zf.open(info) as handle:
            raw = handle.read(limit + 1)
    except Exception:  # noqa: BLE001 坏数据抛什么由压缩算法与 CPython 版本决定
        raise ArchiveError("invalid_archive") from None
    if len(raw) > limit:
        raise ArchiveError("unpacked_too_large", limit_bytes=MAX_UNPACKED_BYTES)
    return raw


def _text(raw: bytes, name: str) -> str:
    if raw[:4] in _ZIP_MAGIC:
        # 改了后缀名的压缩包。
        raise ArchiveError("nested_archive", name=_shown(name))
    if b"\x00" in raw:
        raise ArchiveError("not_utf8", name=_shown(name))
    try:
        return raw.decode("utf-8").removeprefix(_BOM)
    except UnicodeDecodeError:
        raise ArchiveError("not_utf8", name=_shown(name)) from None


def _split(text: str) -> tuple[dict[Any, Any], str]:
    """（front matter 的映射, 正文）。没有 front matter 时映射为空、正文是全文。

    Raises:
        _BadHeader: 有收尾的 `---`，但中间不是合法的 YAML
    """
    first, newline, rest = text.partition("\n")
    if not newline or first.rstrip("\r") != _FENCE:
        return {}, text
    position = 0
    while position <= MAX_HEADER_CHARS:
        end = rest.find("\n", position)
        line = rest[position:] if end < 0 else rest[position:end]
        if line.rstrip("\r") == _FENCE:
            header, body = rest[:position], "" if end < 0 else rest[end + 1 :]
            break
        if end < 0:
            return {}, text
        position = end + 1
    else:
        return {}, text
    try:
        meta = yaml.safe_load(header)
    except (yaml.YAMLError, ValueError, RecursionError, OverflowError):
        raise _BadHeader from None
    if meta is None:
        return {}, body
    if not isinstance(meta, dict):
        # 开头只是一条分隔线，后面碰巧又有一条：整篇都是正文。
        return {}, text
    return meta, body


def _scalar(value: object) -> bool:
    return value is None or (
        isinstance(value, (str, int, float, date)) and not isinstance(value, bool)
    )


def _page(name: str, text: str) -> PageFile:
    path = name[: -len(PAGE_SUFFIX)]
    try:
        meta, body = _split(text)
    except _BadHeader:
        return PageFile(path, None, None, None, "", "invalid_front_matter")
    title, summary, pinned = meta.get("title"), meta.get("summary"), meta.get("pinned")
    if not (_scalar(title) and _scalar(summary) and (pinned is None or isinstance(pinned, bool))):
        return PageFile(path, None, None, None, "", "invalid_front_matter")
    return PageFile(
        path,
        None if title is None else str(title),
        None if summary is None else str(summary),
        pinned,
        body,
    )


def _day(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str) or _DAY.match(value) is None:
        raise ValueError("not a date")
    return date.fromisoformat(value)


def _followup(index: int, item: object) -> FollowupItem:
    def bad(problem: str) -> FollowupItem:
        return FollowupItem(index, "", "", None, None, None, False, problem)

    if not isinstance(item, dict):
        return bad("invalid_item")
    category, title, note = item.get("category"), item.get("title"), item.get("notes")
    status = item.get("status", "open")
    if not isinstance(category, str) or category not in FOLLOWUP_CATEGORIES:
        return bad("invalid_category")
    if not isinstance(title, str) or not 0 < len(title.strip()) <= followups.MAX_TITLE:
        return bad("invalid_title")
    if note is not None and (not isinstance(note, str) or len(note) > followups.MAX_NOTES):
        return bad("invalid_notes")
    if status not in ("open", "snoozed"):
        return bad("invalid_status")
    try:
        review_on, due_on = _day(item.get("review_on")), _day(item.get("due_on"))
    except ValueError:
        return bad("invalid_date")
    return FollowupItem(
        index, category, title.strip(), note or None, review_on, due_on, status == "snoozed"
    )


def _followups(text: str) -> list[FollowupItem]:
    """`followups.json`：`{"version": 1, "items": [<条目>…]}`，至多 500 条。

    条目是一个对象：`category`（`decide` / `promised` / `waiting` / `remind`）、`title`（非空，
    至多 200 字）必填；`notes`（至多 4000 字）、`review_on`、`due_on`（`YYYY-MM-DD`）、`status`
    （`open` 或 `snoozed`，默认 `open`）可选，可以写 null；其余键忽略。某一条不合规只跳过这
    一条（`problem`）；整个文件不合规则拒绝整个包。
    """
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        raise ArchiveError("invalid_followups") from None
    items = doc.get("items") if isinstance(doc, dict) else None
    version = doc.get("version") if isinstance(doc, dict) else None
    if (
        type(version) is not int
        or version != FORMAT_VERSION
        or not isinstance(items, list)
        or len(items) > MAX_FOLLOWUP_ITEMS
    ):
        raise ArchiveError("invalid_followups")
    return [_followup(index, item) for index, item in enumerate(items, start=1)]


def parse(data: bytes) -> Parsed:
    """解析上传的压缩包。

    依次检查，任何一条不过都拒绝整个包：

    1. 压缩包本身不超过 20MB（`archive_too_large`），是一个读得出中央目录的 zip
       （`invalid_archive`）。
    2. 条目总数不超过 4096，其中页面文件不超过 2000 个（`too_many_entries`）。
    3. 每个条目的名字：不是绝对路径，没有反斜杠、`.` / `..` 段、空段、控制字符或 NUL
       （`unsafe_path`）；不是符号链接（`symlink_not_allowed`）；没有加密
       （`encrypted_entry`）；后缀不是压缩包（`nested_archive`）；是 `.md` 文件或根目录的
       `followups.json`，且是普通文件（`unsupported_file`）。
    4. 中央目录里写的大小合计不超过上限；实际读出来的字节数再核一次（`unpacked_too_large`）。
    5. 每个文件的内容：开头不是 zip 的魔数（`nested_archive`），是不含 NUL 的 UTF-8
       （`not_utf8`）；`followups.json` 的整体结构正确（`invalid_followups`）。

    Raises:
        ArchiveError
    """
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ArchiveError("archive_too_large", limit_bytes=MAX_ARCHIVE_BYTES)
    try:
        opened = zipfile.ZipFile(io.BytesIO(data))
    except Exception:  # noqa: BLE001 坏中央目录抛什么由 CPython 版本决定
        raise ArchiveError("invalid_archive") from None
    with opened as zf:
        infos = zf.infolist()
        if len(infos) > MAX_RAW_ENTRIES:
            raise ArchiveError("too_many_entries", limit=MAX_PAGE_FILES)
        for info in infos:
            _check(info)
        files = [info for info in infos if not info.is_dir()]
        if sum(1 for info in files if info.filename != FOLLOWUPS_FILE) > MAX_PAGE_FILES:
            raise ArchiveError("too_many_entries", limit=MAX_PAGE_FILES)
        if sum(info.file_size for info in files) > MAX_UNPACKED_BYTES:
            raise ArchiveError("unpacked_too_large", limit_bytes=MAX_UNPACKED_BYTES)
        left = MAX_UNPACKED_BYTES
        pages: list[PageFile] = []
        listed: list[FollowupItem] = []
        for info in files:
            raw = _read(zf, info, left)
            left -= len(raw)
            text = _text(raw, info.filename)
            if info.filename == FOLLOWUPS_FILE:
                listed = _followups(text)
            else:
                pages.append(_page(info.filename, text))
    return Parsed(pages, listed)


def page_text(*, title: str, summary: str, pinned: bool, body: str) -> str:
    """导出的一页：front matter 加正文，`parse` 读回来与原样一致。"""
    header = yaml.safe_dump(
        {"title": title, "summary": summary, "pinned": pinned},
        allow_unicode=True,
        sort_keys=False,
        width=MAX_HEADER_CHARS,
    )
    return f"{_FENCE}\n{header}{_FENCE}\n{body}"


def followups_text(items: Iterable[Mapping[str, Any]]) -> str:
    """导出的 `followups.json`。"""
    return json.dumps(
        {"version": FORMAT_VERSION, "items": list(items)}, ensure_ascii=False, indent=2
    )


def build(pages: Iterable[tuple[str, str]], followups_json: str | None) -> bytes:
    """打成 zip：`pages` 是（页的路径, 文件内容）；`followups_json` 为 None 时不放清单。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, text in pages:
            zf.writestr(path + PAGE_SUFFIX, text)
        if followups_json is not None:
            zf.writestr(FOLLOWUPS_FILE, followups_json)
    return buffer.getvalue()
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_archive.py -q`
Expected: PASS（45 个用例）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add coreman/core/assistants/archive.py tests/fakes/archives.py \
  tests/unit/test_assistant_archive.py
git commit -m "个人助理导入导出的压缩包格式：只在内存里读，穿越、符号链接、嵌套包、加密、非 Markdown、非 UTF-8 与超限一律拒绝"
```

---

### Task 6: 导入的清单与写入（`transfer.py`）

**Files:**
- Create: `coreman/core/assistants/transfer.py`
- Test: `tests/integration/test_assistant_transfer.py`（新建）

**Interfaces:**
- Consumes: Task 5 的 `archive.parse / build / followups_text`、`Parsed`、`PageFile`、`FollowupItem`；`notes.index / normalize / write`、`NotesError`、`notes.MAX_*`；`followups.list_ / add / snooze`、`FollowupError`、`followups.MAX_OPEN`；`sealed.key_ready`。
- Produces：
  - `class TransferError(Exception)`（`code: str`）。代码：`content_locked`、`too_many_pages`、`notes_full`、`too_many_followups`、`plan_changed`，以及 `notes.write` / `followups.add` 透出来的其余原因代码。
  - `@dataclass(frozen=True) class PagePlan`（`path: str`、`title: str | None`、`summary: str`、`pinned: bool`、`body: str`、`version: int`）；`class Skipped`（`path: str`、`reason: str`）；`class Plan`（`create: list[PagePlan]`、`overwrite: list[PagePlan]`、`skip: list[Skipped]`、`followups: list[FollowupItem]`、`followups_skip: list[tuple[int, str]]`、`digest: str`）。
  - `async def plan(session: AsyncSession, bot_id: uuid.UUID, parsed: Parsed, *, overwrite: bool, today: date, fingerprint: str) -> Plan`
  - `async def apply(session: AsyncSession, bot_id: uuid.UUID, planned: Plan, *, today: date) -> None`（只 flush）
  - `def preview(planned: Plan) -> dict[str, Any]`：`{"digest": str, "create": [{"path", "pinned"}…], "overwrite": [{"path", "pinned"}…], "skip": [{"path", "reason"}…], "followups": {"create": int, "skip": [{"index", "reason"}…]}}`
  - `def counts(planned: Plan) -> str`：`"new=<n>,over=<n>,skip=<n>,fu=<n>"`
  - 条目级的跳过原因：页面 `exists`、`duplicate_path`、`invalid_path`、`page_too_large`、`empty_page`、`invalid_page`、`invalid_front_matter`；待跟进 `duplicate` 加 Task 5 的各 `problem`。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_transfer.py`（新建）：

```python
"""导入的清单与写入：新建 / 覆盖 / 跳过各归各位，出清单不写库，写入带着清单里的版本号。"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import archive, followups, keys, notes, sealed, transfer
from coreman.core.assistants.transfer import TransferError
from coreman.core.db.models import Bot
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
TODAY = date(2026, 11, 13)
VOICE = "---\ntitle: 口吻\nsummary: 说话的规矩\npinned: true\n---\n回复要短。"
PAGES = "SELECT count(*) FROM assistant_pages WHERE bot_id = :bot"
FOLLOWUPS = "SELECT count(*) FROM assistant_followups WHERE bot_id = :bot"
AUTHORS = "SELECT string_agg(DISTINCT updated_by, ',') FROM assistant_pages WHERE bot_id = :bot"
STORED = (
    "SELECT string_agg(path_enc || title_enc || summary_enc || body_enc, chr(32))"
    " FROM assistant_pages WHERE bot_id = :bot"
)


async def _owner(db_session: AsyncSession) -> Bot:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    await db_session.commit()
    return bot


def _parsed(pages: dict[str, str], items: list[dict[str, Any]] | None = None) -> archive.Parsed:
    listed = archive.followups_text(items) if items is not None else None
    return archive.parse(archive.build(list(pages.items()), listed))


async def _plan(
    db_session: AsyncSession,
    bot: Bot,
    parsed: archive.Parsed,
    *,
    overwrite: bool = False,
    fingerprint: str = "f",
) -> transfer.Plan:
    return await transfer.plan(
        db_session, bot.id, parsed, overwrite=overwrite, today=TODAY, fingerprint=fingerprint
    )


async def test_planning_sorts_everything_and_writes_nothing(db_session: AsyncSession) -> None:
    bot = await _owner(db_session)
    await notes.write(db_session, bot.id, "规则/口吻", body="旧的", summary="旧摘要", pinned=True)
    await followups.add(db_session, bot.id, category="waiting", title="等报价")
    await db_session.commit()
    parsed = _parsed(
        {
            "规则/口吻": "新的正文",
            "人/李四": "---\ntitle: 李四\nsummary: 采购  负责人\npinned: true\n---\n正文",
            "人/李四 ": "规范化之后和上一个是同一个路径",
            "a/b/c/d/e/f/g/h/i": "层数太多",
            "空": "   ",
            "坏头": "---\ntitle: [1]\n---\nx",
            "大": "字" * 70_000,
            "长摘要": "---\nsummary: " + "长" * 301 + "\n---\nx",
        },
        [
            {"category": "waiting", "title": "等报价"},
            {"category": "remind", "title": "体检"},
            {"category": "nope", "title": "x"},
        ],
    )
    planned = await _plan(db_session, bot, parsed)
    assert transfer.preview(planned) == {
        "digest": planned.digest,
        "create": [{"path": "人/李四", "pinned": True}],
        "overwrite": [],
        "skip": [
            {"path": "规则/口吻", "reason": "exists"},
            {"path": "人/李四", "reason": "duplicate_path"},
            {"path": "a/b/c/d/e/f/g/h/i", "reason": "invalid_path"},
            {"path": "空", "reason": "empty_page"},
            {"path": "坏头", "reason": "invalid_front_matter"},
            {"path": "大", "reason": "page_too_large"},
            {"path": "长摘要", "reason": "invalid_page"},
        ],
        "followups": {
            "create": 1,
            "skip": [
                {"index": 1, "reason": "duplicate"},
                {"index": 3, "reason": "invalid_category"},
            ],
        },
    }
    assert len(planned.digest) == 64
    assert transfer.counts(planned) == "new=1,over=0,skip=7,fu=1"
    # 出清单不写任何页面与待跟进。
    await db_session.commit()
    assert await raw(db_session, PAGES, bot=bot.id) == 1
    assert await raw(db_session, FOLLOWUPS, bot=bot.id) == 1


async def test_applying_creates_pages_and_followups_as_planned(db_session: AsyncSession) -> None:
    bot = await _owner(db_session)
    parsed = _parsed(
        {"规则/口吻": VOICE + SECRET, "人/李四": "采购负责人。"},
        [
            {"category": "waiting", "title": "等报价", "notes": "催过一次", "due_on": "2026-12-01"},
            {"category": "remind", "title": "体检", "review_on": "2099-01-01", "status": "snoozed"},
            {
                "category": "remind",
                "title": "早该看了",
                "review_on": "2020-01-01",
                "status": "snoozed",
            },
        ],
    )
    planned = await _plan(db_session, bot, parsed)
    await transfer.apply(db_session, bot.id, planned, today=TODAY)
    await db_session.commit()
    voice = await notes.read(db_session, bot.id, "规则/口吻")
    assert voice is not None and voice.body == f"回复要短。{SECRET}"
    # front matter 里写了置顶：导入是所有者本人在场的操作，照办。
    assert (voice.title, voice.summary, voice.pinned, voice.version) == (
        "口吻",
        "说话的规矩",
        True,
        1,
    )
    other = await notes.read(db_session, bot.id, "人/李四")
    assert other is not None
    assert (other.title, other.summary, other.pinned) == ("李四", "", False)
    assert await raw(db_session, AUTHORS, bot=bot.id) == "import"
    assert SECRET not in str(await raw(db_session, STORED, bot=bot.id))
    rows = await followups.list_(db_session, bot.id, "open", today=TODAY)
    assert [
        (row.category, row.title_enc, row.notes_enc, row.review_on, row.due_on, row.status)
        for row in rows
    ] == [
        ("waiting", "等报价", "催过一次", None, date(2026, 12, 1), "open"),
        ("remind", "体检", None, date(2099, 1, 1), None, "snoozed"),
        # 回看日已经过了的推后条目，导进来就是未结。
        ("remind", "早该看了", None, date(2020, 1, 1), None, "open"),
    ]


async def test_overwriting_keeps_what_the_file_does_not_say(db_session: AsyncSession) -> None:
    bot = await _owner(db_session)
    await notes.write(
        db_session, bot.id, "规则/口吻", body="旧的", summary="旧摘要", title="口吻", pinned=True
    )
    await db_session.commit()
    planned = await _plan(db_session, bot, _parsed({"规则/口吻": "第二版"}), overwrite=True)
    assert transfer.preview(planned)["overwrite"] == [{"path": "规则/口吻", "pinned": True}]
    assert transfer.preview(planned)["create"] == []
    await transfer.apply(db_session, bot.id, planned, today=TODAY)
    await db_session.commit()
    page = await notes.read(db_session, bot.id, "规则/口吻")
    assert page is not None
    assert (page.body, page.summary, page.title, page.pinned, page.version) == (
        "第二版",
        "旧摘要",
        "口吻",
        True,
        2,
    )
    assert await raw(db_session, AUTHORS, bot=bot.id) == "import"
    # 文件里写明了的，照文件的来。
    explicit = _parsed({"规则/口吻": "---\nsummary: 新摘要\npinned: false\n---\n第三版"})
    planned = await _plan(db_session, bot, explicit, overwrite=True)
    await transfer.apply(db_session, bot.id, planned, today=TODAY)
    await db_session.commit()
    page = await notes.read(db_session, bot.id, "规则/口吻")
    assert page is not None
    assert (page.body, page.summary, page.title, page.pinned, page.version) == (
        "第三版",
        "新摘要",
        "口吻",
        False,
        3,
    )


async def test_the_digest_changes_with_anything_the_owner_confirmed(
    db_session: AsyncSession,
) -> None:
    bot = await _owner(db_session)
    await notes.write(db_session, bot.id, "规则/口吻", body="旧的", summary="")
    await db_session.commit()
    parsed = _parsed({"规则/口吻": "新的", "人/李四": "x"})
    base = await _plan(db_session, bot, parsed, overwrite=True)
    assert (await _plan(db_session, bot, parsed, overwrite=True)).digest == base.digest
    assert (await _plan(db_session, bot, parsed, overwrite=False)).digest != base.digest
    changed = await _plan(db_session, bot, parsed, overwrite=True, fingerprint="another archive")
    assert changed.digest != base.digest
    # 助理在预览与确认之间改了要覆盖的那一页：版本号变了，摘要跟着变。
    await notes.write(db_session, bot.id, "规则/口吻", body="助理改的", summary="", version=1)
    await db_session.commit()
    assert (await _plan(db_session, bot, parsed, overwrite=True)).digest != base.digest


async def test_a_plan_made_before_a_page_appeared_is_refused(db_session: AsyncSession) -> None:
    bot = await _owner(db_session)
    # 回滚会让会话里的对象过期：之后还要用的编号先取出来。
    bot_id = bot.id
    planned = await _plan(db_session, bot, _parsed({"人/李四": "包里的", "规则/口吻": "包里的"}))
    await notes.write(db_session, bot_id, "规则/口吻", body="助理先写了", summary="")
    await db_session.commit()
    with pytest.raises(TransferError) as refused:
        await transfer.apply(db_session, bot_id, planned, today=TODAY)
    assert refused.value.code == "plan_changed"
    # 调用方回滚：前面已经写下的那一页一并作废。
    await db_session.rollback()
    assert await notes.read(db_session, bot_id, "人/李四") is None
    page = await notes.read(db_session, bot_id, "规则/口吻")
    assert page is not None and page.body == "助理先写了"


@pytest.mark.parametrize(
    ("module", "name", "value", "reason"),
    [
        (notes, "MAX_PAGES", 1, "too_many_pages"),
        (notes, "MAX_TOTAL_BYTES", 5, "notes_full"),
        (followups, "MAX_OPEN", 1, "too_many_followups"),
    ],
)
async def test_limits_refuse_the_whole_import(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    name: str,
    value: int,
    reason: str,
) -> None:
    bot = await _owner(db_session)
    parsed = _parsed(
        {"a": "正文一", "b": "正文二"},
        [{"category": "remind", "title": "一"}, {"category": "remind", "title": "二"}],
    )
    monkeypatch.setattr(module, name, value)
    with pytest.raises(TransferError) as refused:
        await _plan(db_session, bot, parsed)
    assert refused.value.code == reason


async def test_a_destroyed_key_refuses_before_anything_is_read(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    with pytest.raises(TransferError) as refused:
        await _plan(db_session, bot, _parsed({"a": "x"}))
    assert refused.value.code == "content_locked"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_transfer.py -q`
Expected: FAIL：收集时 `ImportError: cannot import name 'transfer' from 'coreman.core.assistants'`。

- [ ] **Step 3: 实现**

`coreman/core/assistants/transfer.py`（新建）：

```python
"""个人助理的导入与导出：把压缩包里的页面与待跟进并进知识库，或把现有的打成同样格式的包。

导入分两步，都由所有者本人在「我的助理」页上发起（写入一律 `updated_by="import"`、
`owner_present=True`）：先 `plan` 出一张清单给他看——只有路径、原因与是否置顶，不带正文——他
确认后再 `apply`。清单带一个 `digest`：确认时带回来，和服务端对着此刻的库重新算出来的对不上
（期间助理写过页面、换了一个包、换了「覆盖」选项），调用方就不写，让他重新看一遍。写入时每
一页仍带着清单里读到的版本号，清单与写入之间再被改动会撞上 `notes.write` 的版本检查。

这里只 flush、不提交：调用方把写入和访问记录放在一个事务里，任何一步失败整体回滚。压缩包的
格式与安全检查在 `archive.py`。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, notes, sealed
from coreman.core.assistants.archive import FollowupItem, PageFile, Parsed
from coreman.core.assistants.followups import FollowupError
from coreman.core.assistants.notes import NotesError

# 清单与写入之间这一页被别人动过：让所有者重新预览。
_MOVED = frozenset({"page_exists", "page_not_found", "version_conflict"})


class TransferError(Exception):
    """这次导入或导出做不了；`code` 是原因代码，不带任何内容。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PagePlan:
    """要写的一页。`version` 为 0 是新建，否则是清单里读到的、要覆盖的那一版。"""

    path: str
    title: str | None
    summary: str
    pinned: bool
    body: str
    version: int


@dataclass(frozen=True)
class Skipped:
    path: str
    reason: str


@dataclass(frozen=True)
class Plan:
    create: list[PagePlan]
    overwrite: list[PagePlan]
    skip: list[Skipped]
    followups: list[FollowupItem]
    # （清单里的位置, 原因）
    followups_skip: list[tuple[int, str]]
    digest: str


def _squash(value: str | None) -> str:
    return " ".join((value or "").split())


def _checked(file: PageFile) -> tuple[str, str, str | None]:
    """（给人看的路径, 唯一性用的键, 不能导入的原因）；能导入时原因为 None。"""
    if file.problem is not None:
        return file.path[: notes.MAX_PATH_CHARS], "", file.problem
    try:
        shown, key = notes.normalize(file.path)
    except NotesError:
        return file.path[: notes.MAX_PATH_CHARS], "", "invalid_path"
    if len(file.body.encode("utf-8")) > notes.MAX_PAGE_BYTES:
        return shown, key, "page_too_large"
    if not file.body.strip():
        return shown, key, "empty_page"
    if (
        len(_squash(file.summary)) > notes.MAX_SUMMARY_CHARS
        or len(_squash(file.title)) > notes.MAX_TITLE_CHARS
    ):
        return shown, key, "invalid_page"
    return shown, key, None


async def plan(
    session: AsyncSession,
    bot_id: uuid.UUID,
    parsed: Parsed,
    *,
    overwrite: bool,
    today: date,
    fingerprint: str,
) -> Plan:
    """对着此刻的库，给这个包出一张清单；不写任何页面与待跟进。

    Args:
        overwrite: 已有的页是覆盖（真）还是跳过（假）
        today: 所有者时区的今天
        fingerprint: 压缩包的 SHA-256，算进 `digest`：确认时换了一个包也对不上

    Raises:
        TransferError: `content_locked`、`too_many_pages`、`notes_full`、`too_many_followups`
    """
    if not await sealed.key_ready(session, bot_id):
        raise TransferError("content_locked")
    existing = {
        notes.normalize(entry.path)[1]: entry for entry in await notes.index(session, bot_id)
    }
    create: list[PagePlan] = []
    replace: list[PagePlan] = []
    skip: list[Skipped] = []
    seen: set[str] = set()
    total = sum(entry.size for entry in existing.values())
    for file in parsed.pages:
        shown, key, reason = _checked(file)
        if reason is None and key in seen:
            reason = "duplicate_path"
        current = existing.get(key) if reason is None else None
        if current is not None and not overwrite:
            reason = "exists"
        if reason is not None:
            skip.append(Skipped(shown, reason))
            continue
        seen.add(key)
        total += len(file.body.encode("utf-8")) - (current.size if current else 0)
        if current is None:
            create.append(
                PagePlan(shown, file.title, _squash(file.summary), bool(file.pinned), file.body, 0)
            )
        else:
            # 文件没写的项保持原样：摘要、置顶沿用现有的，标题不给（`notes.write` 不动它）。
            replace.append(
                PagePlan(
                    shown,
                    file.title,
                    current.summary if file.summary is None else _squash(file.summary),
                    current.pinned if file.pinned is None else file.pinned,
                    file.body,
                    current.version,
                )
            )
    if len(existing) + len(create) > notes.MAX_PAGES:
        raise TransferError("too_many_pages")
    if total > notes.MAX_TOTAL_BYTES:
        raise TransferError("notes_full")
    fresh: list[FollowupItem] = []
    passed: list[tuple[int, str]] = []
    if parsed.followups:
        live = await followups.list_(session, bot_id, "open", today=today, limit=followups.MAX_OPEN)
        taken = {(row.category, row.title_enc) for row in live}
        for item in parsed.followups:
            if item.problem is not None:
                passed.append((item.index, item.problem))
            elif (item.category, item.title) in taken:
                # 未结的清单里已经有同一类、同一个标题的一条。
                passed.append((item.index, "duplicate"))
            else:
                taken.add((item.category, item.title))
                fresh.append(item)
        if len(live) + len(fresh) > followups.MAX_OPEN:
            raise TransferError("too_many_followups")
    stamp = json.dumps(
        [
            fingerprint,
            overwrite,
            [page.path for page in create],
            [[page.path, page.version] for page in replace],
            [[item.path, item.reason] for item in skip],
            [item.index for item in fresh],
            passed,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(stamp.encode("utf-8")).hexdigest()
    return Plan(create, replace, skip, fresh, passed, digest)


async def apply(session: AsyncSession, bot_id: uuid.UUID, planned: Plan, *, today: date) -> None:
    """照清单写入。只 flush；抛错时调用方回滚，已经写了的一并作废。

    Raises:
        TransferError: `plan_changed`（清单之后有页面被动过）、`too_many_pages`、`notes_full`、
            `too_many_followups`、`content_locked` 等核心模块给的原因代码
    """
    try:
        for page in (*planned.create, *planned.overwrite):
            await notes.write(
                session,
                bot_id,
                page.path,
                body=page.body,
                summary=page.summary,
                title=page.title,
                pinned=page.pinned,
                version=page.version,
                updated_by="import",
                owner_present=True,
            )
        for item in planned.followups:
            row = await followups.add(
                session,
                bot_id,
                category=item.category,
                title=item.title,
                notes=item.notes,
                review_on=item.review_on,
                due_on=item.due_on,
            )
            if item.snoozed and item.review_on is not None and item.review_on > today:
                await followups.snooze(session, bot_id, row.seq, item.review_on, today=today)
    except NotesError as exc:
        raise TransferError("plan_changed" if exc.code in _MOVED else exc.code) from None
    except FollowupError as exc:
        raise TransferError(exc.code) from None


def preview(planned: Plan) -> dict[str, Any]:
    """给所有者看的清单：路径、原因、是否置顶与条数；没有标题、摘要与正文。"""
    return {
        "digest": planned.digest,
        "create": [{"path": page.path, "pinned": page.pinned} for page in planned.create],
        "overwrite": [{"path": page.path, "pinned": page.pinned} for page in planned.overwrite],
        "skip": [{"path": item.path, "reason": item.reason} for item in planned.skip],
        "followups": {
            "create": len(planned.followups),
            "skip": [
                {"index": index, "reason": reason} for index, reason in planned.followups_skip
            ],
        },
    }


def counts(planned: Plan) -> str:
    """访问记录里的对象：只有四个数。"""
    return (
        f"new={len(planned.create)},over={len(planned.overwrite)},"
        f"skip={len(planned.skip)},fu={len(planned.followups)}"
    )
```

实现时留意：

- `notes.index` 把读不出来的行（密钥已销毁）悄悄略过，所以先用 `sealed.key_ready` 把「读不出来」和「没有」分开。
- 「人/李四 」与「人/李四」规范化之后是同一个键：后一个记为 `duplicate_path`。`..`、反斜杠之类的名字在 `archive.parse` 就整包拒绝了，走不到这里；这里的 `invalid_path` 是层数超过 8、长度超过 200 这类。
- `followups.list_` 会把到了回看日的推后条目改回未结（它每次都这么做），所以出清单并非一条语句都不写；页面与待跟进的内容不会被出清单改动。
- 一页一页地调 `notes.write`（每页约五条语句）：装满 2000 页的包要十几秒。见文末「没覆盖到的地方」。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_transfer.py -q`
Expected: PASS（9 个用例）。

Run: `uv run pytest tests/unit/test_assistant_sealed_ledger.py tests/unit/test_assistant_access_ledger.py -q`
Expected: PASS（`transfer.py` 没有取内容表整行的 `select`，读写都经 `notes` / `followups`）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants/transfer.py tests/integration/test_assistant_transfer.py
git commit -m "个人助理导入的清单与写入：新建、覆盖、跳过分开列出，清单带摘要，写入带着读到的版本号"
```

---

### Task 7: 导入接口 `POST /api/me/assistant/import`

**Files:**
- Create: `coreman/api/routers/me_assistant_transfer.py`
- Modify: `coreman/api/main.py`（「我的助理」三个路由的导入与注册处）
- Modify: `tests/api/test_my_assistant_guard.py`（导入、`CASES`、`_call`）
- Test: `tests/api/test_my_assistant_import.py`（新建）

**Interfaces:**
- Consumes: Task 5 的 `archive.parse`、`ArchiveError`、`archive.MAX_ARCHIVE_BYTES / MAX_PAGE_FILES`；Task 6 的 `transfer.plan / apply / preview / counts`、`TransferError`；Task 1 的 `access_log.record`；3C 的 `PREFIX`、`DEPENDENCIES`（Task 3 之后含 `in_person`）、`Mine`、`writable`、`refuse`；`settings.get / local_today`；`tests.fakes.archives`。
- Produces：
  - `POST /api/me/assistant/import`，`multipart/form-data`：
    - `archive`：zip 文件（必填）。
    - `dry_run`：`true`（默认）或 `false`。
    - `overwrite`：`true` 或 `false`（默认）。
    - `expect`：确认时带回预览给的 `digest`（至多 64 字符）。
  - 成功：`{"code": 0, "data": {"dry_run": bool, "digest": str, "create": [...], "overwrite": [...], "skip": [...], "followups": {...}}}`（清单的形状见 Task 6 的 `preview`）。
  - 失败：压缩包的问题 413 / 422（`errors[0].type` 是 `ArchiveError` 的代码，`message` 里带出问题的文件名）；`content_locked`、`too_many_pages`、`notes_full`、`too_many_followups`、`plan_changed` 都是 409；助理不可用 409 `assistant_not_active`。
  - 访问记录：预览、摘要对不上、写入失败都记 `import.preview`；写入成功记 `import.notes`；对象都是 `transfer.counts(plan)`。压缩包本身被拒绝时没有读库，不记。
  - `me_assistant_transfer.router`、`ARCHIVE_ERRORS`、`TRANSFER_ERRORS`。

- [ ] **Step 1: 写失败测试**

`tests/api/test_my_assistant_import.py`（新建）：

```python
"""导入接口：先预览后确认。预览不写页面；确认带回摘要才写；任何一步失败整体回滚。"""

from __future__ import annotations

import json
import stat
from datetime import date

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import archive, followups, keys, notes
from tests.api.conftest import login_as
from tests.api.my_assistant_helpers import URL, as_bot_token, code, owned
from tests.fakes.archives import encrypted, pack, special
from tests.integration.assistant_helpers import SECRET, raw

pytestmark = pytest.mark.usefixtures("assistant_master")
IMPORT = f"{URL}/import"
TODAY = date(2026, 11, 13)
PAGES = "SELECT count(*) FROM assistant_pages WHERE bot_id = :bot"
FOLLOWUPS = "SELECT count(*) FROM assistant_followups WHERE bot_id = :bot"
AUTHORS = "SELECT string_agg(DISTINCT updated_by, ',') FROM assistant_pages WHERE bot_id = :bot"
SAMPLE = pack(
    {
        "规则/口吻.md": (
            f"---\ntitle: 口吻\nsummary: 说话的规矩\npinned: true\n---\n回复要短。{SECRET}\n"
        ),
        "人/李四.md": "采购负责人。\n",
        "followups.json": json.dumps(
            {
                "version": 1,
                "items": [{"category": "waiting", "title": "等报价", "due_on": "2026-12-01"}],
            },
            ensure_ascii=False,
        ),
    }
)


async def _post(client: httpx.AsyncClient, data: bytes, **fields: str) -> httpx.Response:
    return await client.post(
        IMPORT, files={"archive": ("notes.zip", data, "application/zip")}, data=fields
    )


async def _logged(db_session: AsyncSession) -> list[tuple[str, str]]:
    rows = await db_session.execute(
        text("SELECT action, target FROM assistant_access_log ORDER BY id")
    )
    return [(row[0], row[1]) for row in rows.all()]


async def test_a_preview_lists_what_would_happen_and_writes_no_page(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    response = await _post(client, SAMPLE)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert data["dry_run"] is True and len(data["digest"]) == 64
    assert data["create"] == [
        {"path": "规则/口吻", "pinned": True},
        {"path": "人/李四", "pinned": False},
    ]
    assert data["overwrite"] == [] and data["skip"] == []
    assert data["followups"] == {"create": 1, "skip": []}
    # 清单只有路径、原因与是否置顶：正文不回显。
    assert SECRET not in response.text
    assert await raw(db_session, PAGES, bot=bot.id) == 0
    assert await raw(db_session, FOLLOWUPS, bot=bot.id) == 0
    # 出清单读了知识库的目录：留一条访问记录，对象只有四个数。
    assert await _logged(db_session) == [("import.preview", "new=2,over=0,skip=0,fu=1")]


async def test_confirming_with_the_digest_writes_everything(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    digest = (await _post(client, SAMPLE)).json()["data"]["digest"]
    done = await _post(client, SAMPLE, dry_run="false", expect=digest)
    assert done.status_code == 200, done.text
    assert done.json()["data"]["dry_run"] is False and done.json()["data"]["digest"] == digest
    voice = await notes.read(db_session, bot.id, "规则/口吻")
    assert voice is not None and voice.body == f"回复要短。{SECRET}\n"
    # 置顶页确实置顶了；这是所有者本人在场的操作。
    assert (voice.title, voice.summary, voice.pinned, voice.version) == (
        "口吻",
        "说话的规矩",
        True,
        1,
    )
    other = await notes.read(db_session, bot.id, "人/李四")
    assert other is not None and (other.title, other.pinned) == ("李四", False)
    assert await raw(db_session, AUTHORS, bot=bot.id) == "import"
    [item] = await followups.list_(db_session, bot.id, "open", today=TODAY)
    assert (item.category, item.title_enc, item.due_on) == ("waiting", "等报价", date(2026, 12, 1))
    assert await _logged(db_session) == [
        ("import.preview", "new=2,over=0,skip=0,fu=1"),
        ("import.notes", "new=2,over=0,skip=0,fu=1"),
    ]


async def test_existing_pages_are_skipped_unless_overwrite_is_asked(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    digest = (await _post(client, SAMPLE)).json()["data"]["digest"]
    await _post(client, SAMPLE, dry_run="false", expect=digest)
    # 同一个包再来一次：页面都已存在，待跟进也已有同样的一条。
    again = (await _post(client, SAMPLE)).json()["data"]
    assert again["create"] == [] and again["overwrite"] == []
    assert again["skip"] == [
        {"path": "规则/口吻", "reason": "exists"},
        {"path": "人/李四", "reason": "exists"},
    ]
    assert again["followups"] == {"create": 0, "skip": [{"index": 1, "reason": "duplicate"}]}

    newer = pack({"规则/口吻.md": "第二版，没有写头。\n"})
    plan = (await _post(client, newer, overwrite="true")).json()["data"]
    # 文件没说置顶与否：保持原样（原来是置顶的），清单里如实标出。
    assert plan["overwrite"] == [{"path": "规则/口吻", "pinned": True}] and plan["skip"] == []
    done = await _post(client, newer, dry_run="false", overwrite="true", expect=plan["digest"])
    assert done.status_code == 200, done.text
    voice = await notes.read(db_session, bot.id, "规则/口吻")
    assert voice is not None
    assert (voice.body, voice.summary, voice.title, voice.pinned, voice.version) == (
        "第二版，没有写头。\n",
        "说话的规矩",
        "口吻",
        True,
        2,
    )
    # 同一个摘要拿去确认「不覆盖」的导入：选项变了，对不上。
    stale = await _post(client, newer, dry_run="false", overwrite="false", expect=plan["digest"])
    assert stale.status_code == 409 and code(stale) == "plan_changed"


async def test_nothing_is_written_without_a_matching_digest(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    for fields in ({"dry_run": "false"}, {"dry_run": "false", "expect": "0" * 64}):
        refused = await _post(client, SAMPLE, **fields)
        assert refused.status_code == 409 and code(refused) == "plan_changed"
    # 预览之后助理先写了其中一页：清单变了，旧摘要作废。
    digest = (await _post(client, SAMPLE)).json()["data"]["digest"]
    await notes.write(db_session, bot.id, "人/李四", body="助理先写了", summary="")
    await db_session.commit()
    refused = await _post(client, SAMPLE, dry_run="false", expect=digest)
    assert refused.status_code == 409 and code(refused) == "plan_changed"
    assert await raw(db_session, PAGES, bot=bot.id) == 1
    assert await raw(db_session, FOLLOWUPS, bot=bot.id) == 0
    assert {action for action, _ in await _logged(db_session)} == {"import.preview"}


async def test_a_failure_half_way_rolls_everything_back(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _owner = await owned(client, db_session)
    digest = (await _post(client, SAMPLE)).json()["data"]["digest"]

    async def refuse_add(*args: object, **kwargs: object) -> None:
        raise followups.FollowupError("too_many_followups")

    # 两页都写下去之后，待跟进这一步失败。
    monkeypatch.setattr(followups, "add", refuse_add)
    failed = await _post(client, SAMPLE, dry_run="false", expect=digest)
    assert failed.status_code == 409 and code(failed) == "too_many_followups"
    assert await raw(db_session, PAGES, bot=bot.id) == 0
    assert await raw(db_session, FOLLOWUPS, bot=bot.id) == 0
    # 没有写成：不留 import.notes；清单读过库，照预览记。
    assert [action for action, _ in await _logged(db_session)] == [
        "import.preview",
        "import.preview",
    ]


async def test_bad_items_are_skipped_and_the_rest_is_imported(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    mixed = pack(
        {
            "好.md": "正文",
            "a/b/c/d/e/f/g/h/i.md": "层数太多",
            "空.md": "  \n",
            "坏头.md": "---\npinned: maybe\n---\nx",
            "好 .md": "规范化之后和第一个重名",
        }
    )
    plan = (await _post(client, mixed)).json()["data"]
    assert plan["create"] == [{"path": "好", "pinned": False}]
    assert plan["skip"] == [
        {"path": "a/b/c/d/e/f/g/h/i", "reason": "invalid_path"},
        {"path": "空", "reason": "empty_page"},
        {"path": "坏头", "reason": "invalid_front_matter"},
        {"path": "好", "reason": "duplicate_path"},
    ]
    done = await _post(client, mixed, dry_run="false", expect=plan["digest"])
    assert done.status_code == 200 and await raw(db_session, PAGES, bot=bot.id) == 1


@pytest.mark.parametrize(
    ("data", "status", "reason"),
    [
        (b"not a zip", 422, "invalid_archive"),
        (pack({"../外面.md": "x"}), 422, "unsafe_path"),
        (special("链接.md", stat.S_IFLNK), 422, "symlink_not_allowed"),
        (encrypted("密.md"), 422, "encrypted_entry"),
        (pack({"里面.zip": b"x"}), 422, "nested_archive"),
        (pack({"伪装.md": pack({"x.md": "x"})}), 422, "nested_archive"),
        (pack({"照片.png": b"x"}), 422, "unsupported_file"),
        (pack({"页.md": "中文".encode("gbk")}), 422, "not_utf8"),
        (pack({"followups.json": "[]"}), 422, "invalid_followups"),
    ],
)
async def test_an_unsafe_archive_is_refused_whole(
    client: httpx.AsyncClient, db_session: AsyncSession, data: bytes, status: int, reason: str
) -> None:
    bot, _owner = await owned(client, db_session)
    for fields in ({}, {"dry_run": "false", "expect": "0" * 64}):
        response = await _post(client, data, **fields)
        assert response.status_code == status, response.text
        assert code(response) == reason
    assert await raw(db_session, PAGES, bot=bot.id) == 0
    # 包本身被拒绝：没有读库，也就没有访问记录。
    assert await _logged(db_session) == []


@pytest.mark.parametrize(
    ("name", "value", "status", "reason"),
    [
        ("MAX_ARCHIVE_BYTES", 100, 413, "archive_too_large"),
        ("MAX_PAGE_FILES", 1, 422, "too_many_entries"),
        ("MAX_UNPACKED_BYTES", 10, 422, "unpacked_too_large"),
    ],
)
async def test_size_limits_refuse_the_archive(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: int,
    status: int,
    reason: str,
) -> None:
    await owned(client, db_session)
    monkeypatch.setattr(archive, name, value)
    response = await _post(client, SAMPLE)
    assert response.status_code == status and code(response) == reason


async def test_the_refusal_names_the_offending_file(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await owned(client, db_session)
    response = await _post(client, pack({"好.md": "x", "照片.png": b"x"}))
    assert "照片.png" in response.json()["message"]


@pytest.mark.parametrize(
    ("module", "name", "reason"),
    [(notes, "MAX_PAGES", "too_many_pages"), (followups, "MAX_OPEN", "too_many_followups")],
)
async def test_an_import_that_would_exceed_the_limits_is_refused(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    name: str,
    reason: str,
) -> None:
    bot, _owner = await owned(client, db_session)
    monkeypatch.setattr(module, name, 0)
    response = await _post(client, SAMPLE)
    assert response.status_code == 409 and code(response) == reason
    assert await raw(db_session, PAGES, bot=bot.id) == 0


@pytest.mark.parametrize("fields", [{"dry_run": "yes"}, {"overwrite": "1"}, {"expect": "x" * 65}])
async def test_form_fields_are_strict(
    client: httpx.AsyncClient, db_session: AsyncSession, fields: dict[str, str]
) -> None:
    await owned(client, db_session)
    assert (await _post(client, SAMPLE, **fields)).status_code == 422
    assert (await client.post(IMPORT, data={"dry_run": "true"})).status_code == 422


async def test_importing_needs_a_usable_assistant(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owned(client, db_session)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    locked = await _post(client, SAMPLE)
    assert locked.status_code == 409 and code(locked) == "content_locked"
    bot.assistant_state = "deprovisioning"
    await db_session.commit()
    stopping = await _post(client, SAMPLE)
    assert stopping.status_code == 409 and code(stopping) == "assistant_not_active"


async def test_only_the_owner_in_person_can_import(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, owner = await owned(client, db_session)
    # 没有助理的平台管理员：404，碰不到所有者的知识库。
    await login_as(client, db_session, role="platform_admin")
    refused = await _post(client, SAMPLE)
    assert refused.status_code == 404 and code(refused) == "no_assistant"
    # 有自己助理的平台管理员：导进去的是他自己的知识库。
    theirs, _admin = await owned(client, db_session, role="platform_admin")
    digest = (await _post(client, SAMPLE)).json()["data"]["digest"]
    assert (await _post(client, SAMPLE, dry_run="false", expect=digest)).status_code == 200
    assert await raw(db_session, PAGES, bot=bot.id) == 0
    assert await raw(db_session, PAGES, bot=theirs.id) == 2
    # 机器人令牌：即使是所有者的也不行。
    as_bot_token(client, monkeypatch, owner)
    assert (await _post(client, SAMPLE)).status_code == 403
    assert await raw(db_session, PAGES, bot=bot.id) == 0
```

`tests/api/test_my_assistant_guard.py`：

导入区加 `from tests.fakes.archives import pack`；`CASES` 之前加

```python
# 导入接口的一次合法调用：默认只预览，不改任何东西。
ARCHIVE = pack({"新页.md": "正文"})
```

`CASES` 里在 `("GET", "/access-log"): {},` 之后加：

```python
    ("POST", "/import"): {"files": {"archive": ("notes.zip", ARCHIVE, "application/zip")}},
```

`_call` 的最后一句

```python
    return await client.request(
        method, f"{URL}{path}", params=spec.get("params"), json=spec.get("json")
    )
```

换成

```python
    return await client.request(
        method,
        f"{URL}{path}",
        params=spec.get("params"),
        json=spec.get("json"),
        files=spec.get("files"),
    )
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_my_assistant_import.py tests/api/test_my_assistant_guard.py -q`
Expected: FAIL：导入的用例 404 / 405（路由还没有）；守卫的路由台账少一条 `("POST", "/import")`。

- [ ] **Step 3: 实现路由**

`coreman/api/routers/me_assistant_transfer.py`（新建）：

```python
"""「我的助理」页的导入与导出：所有者本人上传一个压缩包并进知识库与待跟进，或把它们打包下载。

导入分两步：`dry_run`（默认）只出清单，确认时带回清单的 `digest` 才真正写入；整个导入一个
事务，任何一步失败整体回滚。压缩包的格式与安全检查在 `coreman/core/assistants/archive.py`，
对着库出清单与写入在 `transfer.py`。出清单要读知识库的目录，所以预览也记一条访问记录。
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Literal

from fastapi import APIRouter, Depends, Form, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.errors import ApiError
from coreman.api.routers.me_assistant_common import (
    DEPENDENCIES,
    PREFIX,
    Mine,
    refuse,
    writable,
)
from coreman.core.assistants import access_log, followups, notes, transfer
from coreman.core.assistants import archive as archive_format
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.archive import ArchiveError
from coreman.core.assistants.transfer import TransferError
from coreman.core.timeutils import utcnow

router = APIRouter(prefix=PREFIX, tags=["my-assistant"], dependencies=DEPENDENCIES)

ARCHIVE_ERRORS: dict[str, tuple[int, str]] = {
    "archive_too_large": (413, "压缩包不能超过 20MB"),
    "invalid_archive": (422, "这不是一个读得出来的 zip 压缩包"),
    "too_many_entries": (422, f"压缩包里的页面文件不能超过 {archive_format.MAX_PAGE_FILES} 个"),
    "unpacked_too_large": (422, "压缩包解开之后太大了"),
    "unsafe_path": (422, "压缩包里有不安全的文件名（绝对路径、..、反斜杠或控制字符）"),
    "symlink_not_allowed": (422, "压缩包里不能有符号链接"),
    "encrypted_entry": (422, "压缩包里不能有加密的文件"),
    "nested_archive": (422, "压缩包里不能再套压缩包"),
    "unsupported_file": (422, "压缩包里只能有 .md 文件和根目录下的 followups.json"),
    "not_utf8": (422, "压缩包里有不是 UTF-8 文本的文件"),
    "invalid_followups": (422, "followups.json 的格式不对"),
}
TRANSFER_ERRORS: dict[str, tuple[int, str]] = {
    "content_locked": (409, "加密保存的个人内容已不可读取"),
    "too_many_pages": (409, f"导入之后会超过知识库 {notes.MAX_PAGES} 页的上限"),
    "notes_full": (409, "导入之后会超过知识库合计 20MB 的上限"),
    "too_many_followups": (409, f"导入之后未结的待跟进会超过 {followups.MAX_OPEN} 条的上限"),
    "plan_changed": (409, "预览之后内容有变化，请核对新的清单后再确认"),
}


def _bad_archive(exc: ArchiveError) -> ApiError:
    status, message = ARCHIVE_ERRORS.get(exc.code, (422, "这个压缩包不能导入"))
    # 文件名是所有者自己这次上传的东西，只回给他本人，方便他改；不写进日志。
    name = exc.detail.get("name")
    return ApiError(status, status, f"{message}：{name}" if name else message, [{"type": exc.code}])


@router.post("/import")
async def import_archive(
    archive: UploadFile,
    dry_run: Literal["true", "false"] = Form("true"),
    overwrite: Literal["true", "false"] = Form("false"),
    expect: str = Form("", max_length=64),
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """导入一个 Markdown 压缩包。

    默认只预览（`dry_run=true`）：返回将新建、覆盖、跳过的清单与一个 `digest`，不写任何页面。
    确认时发 `dry_run=false` 并带上 `expect=<digest>`；对着此刻的库重新出的清单与它对不上
    （期间助理写过页面、换了包、换了选项）就 409 `plan_changed`，什么都不写。
    """
    # 之后可能回滚：回滚会让会话里的对象过期，先把要用的编号取出来。
    bot_id, actor_id = own.bot.id, own.actor.id
    data = await archive.read(archive_format.MAX_ARCHIVE_BYTES + 1)
    try:
        # 解压与解析是纯计算，至多三十多 MB：放到线程里，不占事件循环。
        parsed = await asyncio.to_thread(archive_format.parse, data)
    except ArchiveError as exc:
        raise _bad_archive(exc) from None
    prefs = await assistant_settings.get(session, bot_id)
    today = assistant_settings.local_today(prefs, utcnow())
    try:
        plan = await transfer.plan(
            session,
            bot_id,
            parsed,
            overwrite=overwrite == "true",
            today=today,
            fingerprint=hashlib.sha256(data).hexdigest(),
        )
    except TransferError as exc:
        await session.rollback()
        raise refuse(exc.code, TRANSFER_ERRORS) from None
    summary, counted = transfer.preview(plan), transfer.counts(plan)
    if dry_run == "false" and expect == plan.digest:
        try:
            await transfer.apply(session, bot_id, plan, today=today)
        except TransferError as exc:
            # 一步写不进去：已经写了的全部回滚。清单读过库，照预览记一次。
            await session.rollback()
            await access_log.record(session, bot_id, actor_id, "import.preview", counted)
            await session.commit()
            raise refuse(exc.code, TRANSFER_ERRORS) from None
        # 写入与这条记录同一个事务。
        await access_log.record(session, bot_id, actor_id, "import.notes", counted)
        await session.commit()
        return {"code": 0, "data": {**summary, "dry_run": False}}
    await access_log.record(session, bot_id, actor_id, "import.preview", counted)
    await session.commit()
    if dry_run == "false":
        # 没带摘要，或摘要对不上：不预览就写不进去。
        raise refuse("plan_changed", TRANSFER_ERRORS)
    return {"code": 0, "data": {**summary, "dry_run": True}}
```

`coreman/api/main.py`：把

```python
    from coreman.api.routers import me_assistant, me_assistant_followups, me_assistant_notes
```

换成

```python
    from coreman.api.routers import (
        me_assistant,
        me_assistant_followups,
        me_assistant_notes,
        me_assistant_transfer,
    )
```

并在 `app.include_router(me_assistant_notes.router)` 之后加

```python
    app.include_router(me_assistant_transfer.router)
```

实现时留意：

- 表单里的布尔值用 `Literal["true", "false"]`，不用 `bool`：FastAPI 的布尔表单项把 `1`、`on`、`yes` 都当真，这里要的是「没写对就 422」。
- FastAPI 在依赖之前就把整个 multipart 请求体收下来了（Starlette 先落到临时文件），所以 20MB 的上限挡不住「先传上来」；见文末「没覆盖到的地方」。`archive.read(上限 + 1)` 保证进内存的至多 20MB 加 1 字节。
- 包本身被拒绝时（`_bad_archive`）还没有碰库，不记访问记录，也不用回滚。
- `plan` 失败时（`content_locked` 等）还没有解出任何内容，不记。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/api/test_my_assistant_import.py tests/api/test_my_assistant_guard.py -q`
Expected: PASS。

Run: `uv run pytest tests/unit/test_assistant_access_ledger.py tests/api/test_my_assistant_access_log.py -q`
Expected: PASS（`import_archive` 调了 `transfer.plan`，同一个函数里有 `access_log.record`；新路由是 POST，不进 `READS`）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman && uv run alembic check`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/api/routers/me_assistant_transfer.py coreman/api/main.py \
  tests/api/test_my_assistant_import.py tests/api/test_my_assistant_guard.py
git commit -m "我的助理接口：所有者上传 Markdown 压缩包导入知识库与待跟进，先预览后确认，失败整体回滚"
```

---

### Task 8: 导出 `GET /api/me/assistant/export`，以及导出再导入的往返

**Files:**
- Modify: `coreman/core/assistants/notes.py`（导入；文件末尾加 `dump`）
- Modify: `coreman/core/assistants/transfer.py`（导入；文件末尾加 `export`）
- Modify: `coreman/api/routers/me_assistant_transfer.py`（导入；文件末尾加一个接口）
- Modify: `tests/api/test_my_assistant_access_log.py`（`READS`）、`tests/api/test_my_assistant_guard.py`（`CASES`）
- Test: `tests/api/test_my_assistant_export.py`（新建）

**Interfaces:**
- Consumes: Task 5 的 `archive.page_text / followups_text / build / parse`；Task 6 的 `TransferError`；Task 7 的路由文件与 `TRANSFER_ERRORS`；`notes._rows / _by_path / _entry`、`notes.Page`；`followups.list_`；3C 的 `mine`；`mcp_rpc.NO_STORE`。
- Produces：
  - `async def notes.dump(session: AsyncSession, bot_id: uuid.UUID) -> list[Page]`（全部页面连正文，按路径排序；读不出来的行不返回）
  - `async def transfer.export(session: AsyncSession, bot_id: uuid.UUID, *, today: date) -> tuple[bytes, str]`（压缩包, `"pages=<n>,followups=<n>"`）；`TransferError("content_locked")`
  - `GET /api/me/assistant/export` → `application/zip`，`Cache-Control: no-store`，`Content-Disposition: attachment; filename="assistant-export-<YYYYMMDD>.zip"`；访问记录 `export`。密钥不可用 409 `content_locked`。

- [ ] **Step 1: 写失败测试**

`tests/api/test_my_assistant_export.py`（新建）：

```python
"""导出接口：知识页与未结的待跟进打成与导入相同的格式；导出再导入，内容原样回来。"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import archive, followups, keys, notes
from coreman.core.db.models import Bot
from tests.api.conftest import login_as
from tests.api.my_assistant_helpers import URL, as_bot_token, code, owned
from tests.integration.assistant_helpers import SECRET

pytestmark = pytest.mark.usefixtures("assistant_master")
EXPORT, IMPORT = f"{URL}/export", f"{URL}/import"


async def _fill(db_session: AsyncSession, bot: Bot) -> None:
    await notes.write(
        db_session,
        bot.id,
        "规则/口吻",
        body=f"回复要短。\n先说结论。{SECRET}\n",
        summary="说话的规矩",
        title="口吻: 要点",
        pinned=True,
    )
    # 正文以分隔线开头、没有摘要、标题取默认值的一页。
    await notes.write(db_session, bot.id, "人/李四", body="---\n正文以分隔线开头\n", summary="")
    await followups.add(
        db_session,
        bot.id,
        category="waiting",
        title="等报价",
        notes="催过一次\n第二行",
        due_on=date(2026, 12, 1),
    )
    later = await followups.add(db_session, bot.id, category="remind", title="体检")
    await followups.snooze(db_session, bot.id, later.seq, date(2099, 1, 1), today=date(2026, 1, 1))
    done = await followups.add(db_session, bot.id, category="decide", title="已经办完的")
    await followups.close(db_session, bot.id, done.seq)
    await db_session.commit()


async def _snapshot(db_session: AsyncSession, bot_id: uuid.UUID) -> tuple[list[Any], list[Any]]:
    pages = [
        (page.path, page.title, page.summary, page.pinned, page.body)
        for page in await notes.dump(db_session, bot_id)
    ]
    rows = await followups.list_(
        db_session, bot_id, "open", today=date(2026, 1, 1), limit=followups.MAX_OPEN
    )
    listed = [
        (row.category, row.title_enc, row.notes_enc, row.review_on, row.due_on, row.status)
        for row in rows
    ]
    # 提交而不是回滚：回滚会让会话里的对象过期，调用方手里的记录就不能再读了。
    await db_session.commit()
    return pages, listed


async def _logged(db_session: AsyncSession) -> list[tuple[str, str]]:
    rows = await db_session.execute(
        text("SELECT action, target FROM assistant_access_log ORDER BY id")
    )
    return [(row[0], row[1]) for row in rows.all()]


async def test_the_owner_downloads_pages_and_open_followups(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _fill(db_session, bot)
    response = await client.get(EXPORT)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="assistant-export-')
    assert disposition.endswith('.zip"')
    parsed = archive.parse(response.content)
    assert [(page.path, page.title, page.summary, page.pinned) for page in parsed.pages] == [
        ("人/李四", "李四", "", False),
        ("规则/口吻", "口吻: 要点", "说话的规矩", True),
    ]
    assert parsed.pages[0].body == "---\n正文以分隔线开头\n"
    assert parsed.pages[1].body == f"回复要短。\n先说结论。{SECRET}\n"
    # 已关闭的待跟进不导出；推后的照样导出。
    assert [(item.category, item.title, item.snoozed) for item in parsed.followups] == [
        ("waiting", "等报价", False),
        ("remind", "体检", True),
    ]
    # 访问记录的对象只有两个数。
    assert await _logged(db_session) == [("export", "pages=2,followups=2")]


async def test_an_export_imports_back_unchanged(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _fill(db_session, bot)
    exported = (await client.get(EXPORT)).content
    # 另一位所有者把这个包导进自己的（空的）助理。
    theirs, _other = await owned(client, db_session)
    upload = {"archive": ("export.zip", exported, "application/zip")}
    plan = (await client.post(IMPORT, files=upload)).json()["data"]
    assert len(plan["create"]) == 2 and plan["skip"] == []
    assert plan["followups"] == {"create": 2, "skip": []}
    done = await client.post(
        IMPORT, files=upload, data={"dry_run": "false", "expect": plan["digest"]}
    )
    assert done.status_code == 200, done.text
    assert await _snapshot(db_session, theirs.id) == await _snapshot(db_session, bot.id)


async def test_an_empty_assistant_exports_an_empty_archive(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await owned(client, db_session)
    response = await client.get(EXPORT)
    assert response.status_code == 200
    parsed = archive.parse(response.content)
    assert parsed.pages == [] and parsed.followups == []
    assert await _logged(db_session) == [("export", "pages=0,followups=0")]


async def test_a_destroyed_key_exports_nothing(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owned(client, db_session)
    await _fill(db_session, bot)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    response = await client.get(EXPORT)
    assert response.status_code == 409 and code(response) == "content_locked"
    assert SECRET not in response.text and await _logged(db_session) == []


async def test_only_the_owner_in_person_can_export(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, owner = await owned(client, db_session)
    await _fill(db_session, bot)
    # 没有助理的平台管理员。
    await login_as(client, db_session, role="platform_admin")
    refused = await client.get(EXPORT)
    assert refused.status_code == 404 and code(refused) == "no_assistant"
    # 有自己助理的平台管理员：拿到的是他自己的（空的）。
    await owned(client, db_session, role="platform_admin")
    theirs = await client.get(EXPORT)
    assert archive.parse(theirs.content).pages == []
    assert SECRET.encode() not in theirs.content
    # 机器人令牌：即使是所有者的也不行。
    as_bot_token(client, monkeypatch, owner)
    assert (await client.get(EXPORT)).status_code == 403
    # 一路下来，所有者的助理没有被导出过。
    assert ("export", "pages=2,followups=2") not in await _logged(db_session)
```

`tests/api/test_my_assistant_access_log.py`：`READS` 里在 `"/access-log": [],` 之后加：

```python
    # 导出把全部知识页与未结的待跟进解了出来：对象只有两个数。
    "/export": [("export", "pages=1,followups=1")],
```

`tests/api/test_my_assistant_guard.py`：`CASES` 里在 `("POST", "/import"): …` 之后加：

```python
    ("GET", "/export"): {},
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_my_assistant_export.py tests/api/test_my_assistant_access_log.py tests/api/test_my_assistant_guard.py -q`
Expected: FAIL：导出的用例 404；两张路由台账各多出一条没有的路由（`/export`）。

- [ ] **Step 3: `notes.dump` 与 `transfer.export`**

`coreman/core/assistants/notes.py`：

把 `from dataclasses import dataclass` 换成 `from dataclasses import asdict, dataclass`。文件末尾追加：

```python
async def dump(session: AsyncSession, bot_id: uuid.UUID) -> list[Page]:
    """全部页面连正文，按路径排序：导出用。读不出来的行（密钥已销毁）不返回。"""
    rows = _by_path(await _rows(session, bot_id, bodies=True))
    return [Page(**asdict(_entry(row)), body=row.body_enc) for row in rows]
```

`coreman/core/assistants/transfer.py`：

导入区加 `import asyncio`（放在 `import hashlib` 之前），把

```python
from coreman.core.assistants import followups, notes, sealed
```

换成

```python
from coreman.core.assistants import archive, followups, notes, sealed
```

文件末尾追加：

```python
async def export(session: AsyncSession, bot_id: uuid.UUID, *, today: date) -> tuple[bytes, str]:
    """全部知识页与未结的待跟进（含推后的），打成与导入相同格式的 zip。

    Returns:
        （压缩包, 访问记录里的对象——只有两个数）

    Raises:
        TransferError: `content_locked`
    """
    if not await sealed.key_ready(session, bot_id):
        raise TransferError("content_locked")
    pages = await notes.dump(session, bot_id)
    live = await followups.list_(session, bot_id, "open", today=today, limit=followups.MAX_OPEN)
    items = [
        {
            "category": row.category,
            "title": row.title_enc,
            "notes": row.notes_enc,
            "review_on": row.review_on.isoformat() if row.review_on else None,
            "due_on": row.due_on.isoformat() if row.due_on else None,
            "status": row.status,
        }
        for row in live
    ]
    files = [
        (
            page.path,
            archive.page_text(
                title=page.title, summary=page.summary, pinned=page.pinned, body=page.body
            ),
        )
        for page in pages
    ]
    # 压缩是纯计算，装满的知识库有二十多 MB：放到线程里，不占事件循环。
    data = await asyncio.to_thread(archive.build, files, archive.followups_text(items))
    return data, f"pages={len(pages)},followups={len(items)}"
```

- [ ] **Step 4: 导出接口**

`coreman/api/routers/me_assistant_transfer.py`：

导入区加

```python
from collections.abc import Iterator

from fastapi.responses import StreamingResponse

from coreman.api import mcp_rpc
```

并把从 `me_assistant_common` 导入的名字加上 `mine`：

```python
from coreman.api.routers.me_assistant_common import (
    DEPENDENCIES,
    PREFIX,
    Mine,
    mine,
    refuse,
    writable,
)
```

`TRANSFER_ERRORS` 之后加：

```python
# 下载时一次发出去的块。
CHUNK = 64 * 1024


def _chunks(data: bytes) -> Iterator[bytes]:
    view = memoryview(data)
    for start in range(0, len(view), CHUNK):
        yield bytes(view[start : start + CHUNK])
```

文件末尾追加：

```python
@router.get("/export")
async def export_archive(
    own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> StreamingResponse:
    """把全部知识页与未结的待跟进打成与导入相同格式的 zip 下载。

    不要求助理处于可用状态：停用开始之后、密钥销毁之前仍然可以导出。
    """
    bot_id, actor_id = own.bot.id, own.actor.id
    prefs = await assistant_settings.get(session, bot_id)
    today = assistant_settings.local_today(prefs, utcnow())
    try:
        data, counted = await transfer.export(session, bot_id, today=today)
    except TransferError as exc:
        await session.rollback()
        raise refuse(exc.code, TRANSFER_ERRORS) from None
    await access_log.record(session, bot_id, actor_id, "export", counted)
    # 发出第一个字节之前先把记录提交：提交不了，内容也不交出去。
    await session.commit()
    return StreamingResponse(
        _chunks(data),
        media_type="application/zip",
        # 直接返回响应对象时路由级的 no_store 依赖不生效，这里自己带上。
        headers={
            **mcp_rpc.NO_STORE,
            "Content-Disposition": f'attachment; filename="assistant-export-{today:%Y%m%d}.zip"',
            "Content-Length": str(len(data)),
        },
    )
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/api/test_my_assistant_export.py tests/api/test_my_assistant_access_log.py tests/api/test_my_assistant_guard.py tests/api/test_my_assistant_import.py -q`
Expected: PASS。

Run: `uv run pytest tests/unit/test_assistant_access_ledger.py tests/unit/test_assistant_sealed_ledger.py tests/integration tests/api -q -k "notes or transfer or ledger"`
Expected: PASS（`notes.dump` 复用 `_rows`，整行读取的台账条数不变；`export_archive` 调了 `transfer.export`，同一个函数里有 `access_log.record`）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman && uv run alembic check`
Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add coreman/core/assistants/notes.py coreman/core/assistants/transfer.py \
  coreman/api/routers/me_assistant_transfer.py tests/api/test_my_assistant_export.py \
  tests/api/test_my_assistant_access_log.py tests/api/test_my_assistant_guard.py
git commit -m "我的助理接口：所有者导出知识页与未结的待跟进，格式与导入相同，导出再导入原样回来"
```

---

### Task 9: 前端——设置分区里的访问记录，管理台列表多一列

**Files:**
- Modify: `web/src/api/myAssistant.ts`（类型；`myAssistant` 加一个方法）
- Modify: `web/src/api/assistantsAdmin.ts`（`AssistantRow` 加一个字段）
- Modify: `web/src/i18n/assistant.ts`（三种语言的 `settings`、`admin` 各加键）
- Create: `web/src/components/assistant/AssistantAccessLog.vue`
- Modify: `web/src/components/assistant/AssistantSettings.vue`（导入；模板里嵌入）
- Modify: `web/src/views/AssistantsView.vue`（多一列）
- Modify: `web/tests/AssistantSettings.spec.ts`（`render`）、`web/tests/AssistantsView.spec.ts`（`row` 工厂、加一条用例）
- Test: `web/tests/AssistantAccessLog.spec.ts`（新建）

**Interfaces:**
- Consumes: Task 4 的 `GET /api/me/assistant/access-log` 与 `last_non_owner_access_at`；`@/components/LoadState.vue`；`@/utils/errors` 的 `errorMessage`；`@/utils/format` 的 `formatDateTime`。
- Produces：
  - `@/api/myAssistant`：`interface AccessEntry`（`id: number`、`at: string`、`actor_kind: 'owner' | 'admin' | 'system'`、`actor_name: string | null`、`action: string`、`target: string`）、`interface AccessPage`（`items: AccessEntry[]`、`next_before: number | null`）、`myAssistant.accessLog(before?: number)`。
  - `AssistantRow.last_non_owner_access_at: string | null`。
  - 文案 `assistant.settings.accessLog / accessLogHint / accessEmpty / accessMore / accessWho.{owner,admin,system} / accessActions.<动作代码把点换成下划线>`、`assistant.admin.lastAccess`。
  - 组件 `AssistantAccessLog`（无属性；`defineExpose({ load })`）。测试钩子：`data-test="access-<序号>"`、`access-foreign-<序号>`、`access-empty`、`access-more`；管理台列表 `last-access-<bot_id>`。

- [ ] **Step 1: 接口封装与文案**

`web/src/api/myAssistant.ts`：在 `const root = '/api/me/assistant'` 之前加

```ts
/** 访问记录的一行：谁、什么时候、做了什么；没有任何内容。 */
export interface AccessEntry {
  id: number
  at: string
  actor_kind: 'owner' | 'admin' | 'system'
  /** 操作人的显示名；管理命令或账号已删除时为 null。 */
  actor_name: string | null
  action: string
  target: string
}
/** `next_before` 不为 null 时还有更早的，带着它再取下一页。 */
export interface AccessPage { items: AccessEntry[]; next_before: number | null }
```

`myAssistant` 对象里，在 `deprovision: …` 那一项之后加

```ts
  accessLog: (before?: number) => call<AccessPage>(http.get(`${root}/access-log`, { params: before ? { before } : {} })),
```

`web/src/api/assistantsAdmin.ts`：`AssistantRow` 里在 `jobs_active: number` 之后加

```ts
  /** 最近一次非所有者访问这位助理个人内容的时间（管理命令等）；从来没有是 null。只有时间。 */
  last_non_owner_access_at: string | null
```

`web/src/i18n/assistant.ts`：

`assistantZh.settings` 里在 `deprovisionMismatch: '名字不对', deprovisionStarted: '已开始停用',` 之后加

```ts
    accessLog: '访问记录',
    accessLogHint: '助理自己干活之外，谁在什么时候读取过你加密保存的个人内容。这里只记动作，不记内容；保留 180 天。',
    accessEmpty: '还没有访问记录。', accessMore: '更早的记录',
    accessWho: { owner: '你本人', admin: '{name}（不是你本人）', system: '服务器上的管理命令' },
    accessActions: {
      approvals_read: '查看审批单', followups_read: '查看待跟进', jobs_read: '查看子任务',
      notes_index: '查看知识库目录', notes_read: '打开一页知识页', settings_read: '查看设置',
      chatlogs_read: '查看对话记录', tasks_read: '查看任务详情',
      import_preview: '预览导入', import_notes: '导入知识页与待跟进', export: '导出知识页与待跟进',
      cli_rewrap: '主密钥轮换（没有读取内容）', cli_chatlogs: '用管理命令查看对话记录', cli_tasks: '用管理命令查看任务详情',
    },
```

`assistantZh.admin` 里在 `deprovisionMismatch: '登录名不对', deprovisionStarted: '已开始停用',` 之后加

```ts
    lastAccess: '最近一次非本人访问',
```

`assistantEn.settings` 的最后一个键（`deprovisionStarted`）之后加

```ts
    accessLog: 'Access log',
    accessLogHint: 'Who read your encrypted personal content, and when, outside the assistant doing its own work. Only the action is recorded, never the content. Entries are kept for 180 days.',
    accessEmpty: 'No access has been recorded yet.', accessMore: 'Earlier entries',
    accessWho: { owner: 'You', admin: '{name} (not you)', system: 'An admin command on the server' },
    accessActions: {
      approvals_read: 'Viewed approvals', followups_read: 'Viewed follow-ups', jobs_read: 'Viewed sub-tasks',
      notes_index: 'Viewed the knowledge base index', notes_read: 'Opened a knowledge page', settings_read: 'Viewed settings',
      chatlogs_read: 'Viewed conversation logs', tasks_read: 'Viewed task details',
      import_preview: 'Previewed an import', import_notes: 'Imported pages and follow-ups', export: 'Exported pages and follow-ups',
      cli_rewrap: 'Master key rotation (no content was read)', cli_chatlogs: 'Viewed conversation logs with an admin command', cli_tasks: 'Viewed task details with an admin command',
    },
```

`assistantEn.admin` 的最后一个键之后加

```ts
    lastAccess: 'Last access by someone else',
```

`assistantJa.settings` 的最后一个键之后加

```ts
    accessLog: 'アクセス記録',
    accessLogHint: 'アシスタント自身の作業以外で、暗号化して保存されたあなたの個人データを誰がいつ読み取ったかを表示します。記録するのは操作のみで、内容は含みません。180 日間保存されます。',
    accessEmpty: 'アクセス記録はまだありません。', accessMore: '以前の記録',
    accessWho: { owner: 'あなた本人', admin: '{name}（本人以外）', system: 'サーバー上の管理コマンド' },
    accessActions: {
      approvals_read: '承認申請を表示', followups_read: 'フォローアップを表示', jobs_read: 'サブタスクを表示',
      notes_index: 'ナレッジベースの一覧を表示', notes_read: 'ナレッジページを開いた', settings_read: '設定を表示',
      chatlogs_read: '会話ログを表示', tasks_read: 'タスクの詳細を表示',
      import_preview: 'インポートをプレビュー', import_notes: 'ページとフォローアップをインポート', export: 'ページとフォローアップをエクスポート',
      cli_rewrap: 'マスターキーのローテーション（内容は読み取っていません）', cli_chatlogs: '管理コマンドで会話ログを表示', cli_tasks: '管理コマンドでタスクの詳細を表示',
    },
```

`assistantJa.admin` 的最后一个键之后加

```ts
    lastAccess: '本人以外による最終アクセス',
```

（动作代码里的点在文案键里换成下划线：vue-i18n 把点当成路径分隔。3C 的计划写了「后面的任务不再改这个文件」，那是对 3C 自己的任务说的；本层要加文案，三种语言一起加，`web/tests/assistantI18n.spec.ts` 守着键与占位符一致。）

- [ ] **Step 2: 写失败测试**

`web/tests/AssistantAccessLog.spec.ts`（新建）：

```ts
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/myAssistant', async (original) => ({
  ...(await original<typeof import('@/api/myAssistant')>()),
  myAssistant: { accessLog: vi.fn() },
}))

import { myAssistant, type AccessEntry } from '@/api/myAssistant'
import AssistantAccessLog from '@/components/assistant/AssistantAccessLog.vue'
import { i18n } from '@/i18n'

const t = i18n.global.t
const entry = (over: Partial<AccessEntry>): AccessEntry => ({
  id: 9, at: '2026-11-13T02:00:00Z', actor_kind: 'owner', actor_name: '张三', action: 'notes.read', target: 'page', ...over,
})
const render = () => mount(AssistantAccessLog, { global: { plugins: [ElementPlus, i18n] } })
// 后端 coreman/core/assistants/access_log.py 的 ACTIONS：那边加了动作，这里和文案要一起加。
const ACTIONS = [
  'approvals.read', 'followups.read', 'jobs.read', 'notes.index', 'notes.read', 'settings.read',
  'chatlogs.read', 'tasks.read', 'import.preview', 'import.notes', 'export',
  'cli.rewrap', 'cli.chatlogs', 'cli.tasks',
]

beforeEach(() => {
  vi.mocked(myAssistant.accessLog).mockReset().mockResolvedValue({
    items: [
      entry({ id: 12, actor_kind: 'system', actor_name: null, action: 'cli.rewrap', target: '' }),
      entry({ id: 11, actor_kind: 'admin', actor_name: '李四', action: 'chatlogs.read', target: 'rows=2' }),
      entry({ id: 10 }),
      entry({ id: 9, action: 'something.new', target: '' }),
    ],
    next_before: null,
  })
})

describe('access log', () => {
  it('shows when, who and what, and flags access by anyone but the owner', async () => {
    const wrapper = render(); await flushPromises()
    expect(wrapper.text()).toContain(t('assistant.settings.accessLogHint'))
    expect(wrapper.get('[data-test="access-foreign-0"]').text()).toBe(t('assistant.settings.accessWho.system'))
    expect(wrapper.get('[data-test="access-0"]').text()).toContain(t('assistant.settings.accessActions.cli_rewrap'))
    expect(wrapper.get('[data-test="access-foreign-1"]').text()).toBe(t('assistant.settings.accessWho.admin', { name: '李四' }))
    expect(wrapper.get('[data-test="access-1"]').text()).toContain('rows=2')
    expect(wrapper.find('[data-test="access-foreign-2"]').exists()).toBe(false)
    const mine = wrapper.get('[data-test="access-2"]').text()
    expect(mine).toContain(t('assistant.settings.accessWho.owner'))
    expect(mine).toContain(t('assistant.settings.accessActions.notes_read'))
    expect(wrapper.find('[data-test="access-more"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('falls back to the action code when it has no label', async () => {
    const wrapper = render(); await flushPromises()
    expect(wrapper.get('[data-test="access-3"]').text()).toContain('something.new')
    wrapper.unmount()
  })

  it('loads earlier entries on demand', async () => {
    vi.mocked(myAssistant.accessLog)
      .mockResolvedValueOnce({ items: [entry({ id: 30 }), entry({ id: 29 })], next_before: 29 })
      .mockResolvedValueOnce({ items: [entry({ id: 28 })], next_before: null })
    const wrapper = render(); await flushPromises()
    expect(myAssistant.accessLog).toHaveBeenLastCalledWith(undefined)
    await wrapper.get('[data-test="access-more"]').trigger('click'); await flushPromises()
    expect(myAssistant.accessLog).toHaveBeenLastCalledWith(29)
    expect(wrapper.findAll('li')).toHaveLength(3)
    expect(wrapper.find('[data-test="access-more"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('says so when nothing has been recorded', async () => {
    vi.mocked(myAssistant.accessLog).mockResolvedValue({ items: [], next_before: null })
    const wrapper = render(); await flushPromises()
    expect(wrapper.get('[data-test="access-empty"]').text()).toBe(t('assistant.settings.accessEmpty'))
    wrapper.unmount()
  })

  it('has a label for every action in all three languages', () => {
    for (const locale of ['zh', 'en', 'ja'] as const) {
      for (const action of ACTIONS) {
        const key = 'assistant.settings.accessActions.' + action.replace(/\./g, '_')
        expect(i18n.global.te(key, locale), `${locale} ${action}`).toBe(true)
      }
    }
  })
})
```

`web/tests/AssistantsView.spec.ts`：

导入区加 `import { formatDateTime } from '@/utils/format'`。`row` 工厂里把

```ts
  turns: { today: 12, budget: 200 }, inbox_pending: 4, jobs_active: 2, ...over,
```

换成

```ts
  turns: { today: 12, budget: 200 }, inbox_pending: 4, jobs_active: 2, last_non_owner_access_at: null, ...over,
```

`describe('personal assistants (admin)', …)` 里加一条用例：

```ts
  it('shows only the time of the last access by someone other than the owner', async () => {
    vi.mocked(assistantsAdmin.list).mockResolvedValue({
      items: [row({ last_non_owner_access_at: '2026-11-12T08:00:00Z' }), row({ bot_id: 'b2' })],
    })
    const wrapper = render(); await flushPromises()
    expect(wrapper.text()).toContain(t('assistant.admin.lastAccess'))
    expect(wrapper.get('[data-test="last-access-b1"]').text()).toBe(formatDateTime('2026-11-12T08:00:00Z'))
    expect(wrapper.get('[data-test="last-access-b2"]').text()).toBe(t('assistant.admin.none'))
    wrapper.unmount()
  })
```

`web/tests/AssistantSettings.spec.ts`：访问记录列表有自己的用例，这里把它换成桩。把

```ts
const render = () => mount(AssistantSettings, { props: { name: '个人助理', runtime: 'pool-1' }, global: { plugins: [ElementPlus, i18n] } })
```

换成

```ts
const render = () => mount(AssistantSettings, {
  props: { name: '个人助理', runtime: 'pool-1' },
  global: { plugins: [ElementPlus, i18n], stubs: { AssistantAccessLog: true } },
})
```

并在 `describe('settings section', …)` 里加一条用例：

```ts
  it('shows the access log under the form', async () => {
    const wrapper = render(); await flushPromises()
    expect(wrapper.find('assistant-access-log-stub').exists()).toBe(true)
    wrapper.unmount()
  })
```

- [ ] **Step 3: 运行确认失败**

Run（在 `web/` 下）: `npx vitest run tests/AssistantAccessLog.spec.ts tests/AssistantsView.spec.ts tests/AssistantSettings.spec.ts`
Expected: FAIL：找不到 `@/components/assistant/AssistantAccessLog.vue`；管理台列表找不到 `last-access-b1`；设置分区里没有访问记录的桩。

- [ ] **Step 4: 实现**

`web/src/components/assistant/AssistantAccessLog.vue`（新建）：

```vue
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { myAssistant, type AccessEntry } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t, te } = useI18n()
const items = ref<AccessEntry[] | null>(null)
const next = ref<number | null>(null)
const error = ref(''), loading = ref(false)

/** `more` 为真时接着上一页往更早取，否则从头取。 */
async function load(more = false) {
  loading.value = true; error.value = ''
  try {
    const page = await myAssistant.accessLog(more && next.value ? next.value : undefined)
    items.value = more && items.value ? [...items.value, ...page.items] : page.items
    next.value = page.next_before
  } catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
const who = (entry: AccessEntry) => (entry.actor_kind === 'admin'
  ? t('assistant.settings.accessWho.admin', { name: entry.actor_name ?? t('assistant.admin.none') })
  : t('assistant.settings.accessWho.' + entry.actor_kind))
/** 动作代码 → 文案；后端新加的、这里还没有文案的动作原样显示代码。 */
function what(entry: AccessEntry) {
  const key = 'assistant.settings.accessActions.' + entry.action.replace(/\./g, '_')
  return te(key) ? t(key) : entry.action
}
onMounted(() => load())
defineExpose({ load })
</script>
<template>
  <section class="access-log">
    <h3>{{ t('assistant.settings.accessLog') }}</h3>
    <p class="muted">
      {{ t('assistant.settings.accessLogHint') }}
    </p>
    <LoadState
      :loading="loading && !items"
      :error="error"
      @retry="load()"
    />
    <p
      v-if="items && !items.length"
      class="muted"
      data-test="access-empty"
    >
      {{ t('assistant.settings.accessEmpty') }}
    </p>
    <ul v-if="items && items.length">
      <li
        v-for="(entry, i) in items"
        :key="entry.id"
        :data-test="'access-' + i"
      >
        <span class="muted">{{ formatDateTime(entry.at) }}</span>
        <!-- 不是所有者本人的访问用醒目的标签：这正是这张表要让他看见的。 -->
        <el-tag
          v-if="entry.actor_kind !== 'owner'"
          type="warning"
          :data-test="'access-foreign-' + i"
        >
          {{ who(entry) }}
        </el-tag>
        <span v-else>{{ who(entry) }}</span>
        <span>{{ what(entry) }}</span>
        <code v-if="entry.target">{{ entry.target }}</code>
      </li>
    </ul>
    <el-button
      v-if="next"
      link
      type="primary"
      :loading="loading"
      data-test="access-more"
      @click="load(true)"
    >
      {{ t('assistant.settings.accessMore') }}
    </el-button>
  </section>
</template>
<style scoped>
.access-log { margin-top: 24px; max-width: 760px; }
.access-log ul { list-style: none; padding: 0; }
.access-log li { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; padding: 6px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.muted { color: var(--el-text-color-secondary); }
</style>
```

`web/src/components/assistant/AssistantSettings.vue`：

`<script setup>` 的导入里，在 `import LoadState from '@/components/LoadState.vue'` 之前加

```ts
import AssistantAccessLog from '@/components/assistant/AssistantAccessLog.vue'
```

模板里，在表单的 `</el-form>` 之后、`class="danger-zone"` 的 `<el-alert` 之前加

```vue
    <AssistantAccessLog />
```

`web/src/views/AssistantsView.vue`：在「轮询健康」一列（`:label="t('assistant.admin.watch')"` 的 `<el-table-column>`）的 `</el-table-column>` 之后、「待删目录」一列之前加

```vue
      <el-table-column
        :label="t('assistant.admin.lastAccess')"
        width="170"
      >
        <template #default="{ row }: { row: AssistantRow }">
          <span :data-test="'last-access-' + row.bot_id">{{ row.last_non_owner_access_at ? formatDateTime(row.last_non_owner_access_at) : t('assistant.admin.none') }}</span>
        </template>
      </el-table-column>
```

- [ ] **Step 5: 运行确认通过**

Run（在 `web/` 下）: `npx vitest run tests/AssistantAccessLog.spec.ts tests/AssistantsView.spec.ts tests/AssistantSettings.spec.ts tests/assistantI18n.spec.ts tests/i18n.spec.ts`
Expected: PASS。

Run（在 `web/` 下）: `npm run lint && npm run build`
Expected: 没有 error；`vue-tsc` 与构建通过。

- [ ] **Step 6: 提交**

```bash
git add web/src/api/myAssistant.ts web/src/api/assistantsAdmin.ts web/src/i18n/assistant.ts \
  web/src/components/assistant/AssistantAccessLog.vue \
  web/src/components/assistant/AssistantSettings.vue web/src/views/AssistantsView.vue \
  web/tests/AssistantAccessLog.spec.ts web/tests/AssistantsView.spec.ts \
  web/tests/AssistantSettings.spec.ts
git commit -m "我的助理页：设置分区里看访问记录，非本人的访问醒目标出；管理台列表显示最近一次非本人访问"
```

---

### Task 10: 前端——知识库分区的导入（预览后确认）与导出，停用前提示导出

**Files:**
- Modify: `web/src/api/myAssistant.ts`（导入、类型、两个方法）
- Create: `web/src/utils/download.ts`
- Modify: `web/src/i18n/assistant.ts`（三种语言的 `notes` 加键、`settings.deprovisionPrompt` 换文案）
- Create: `web/src/components/assistant/AssistantTransfer.vue`
- Modify: `web/src/components/assistant/AssistantNotes.vue`（导入；模板里嵌入）
- Modify: `web/tests/AssistantNotes.spec.ts`（`render`、加一条用例）、`web/tests/AssistantSettings.spec.ts`（停用那条用例加一句断言）
- Test: `web/tests/AssistantTransfer.spec.ts`、`web/tests/myAssistantTransfer.spec.ts`（新建）

**Interfaces:**
- Consumes: Task 7 的 `POST /import`（表单项 `archive`、`dry_run`、`overwrite`、`expect`；清单的形状）；Task 8 的 `GET /export`；`@/api/client` 的 `ApiError` / `call` / `http`；`@/utils/errors` 的 `errorMessage`。
- Produces：
  - `@/api/myAssistant`：`interface ImportLine`（`path: string`、`pinned?: boolean`、`reason?: string`）、`interface ImportPlan`（`dry_run: boolean`、`digest: string`、`create: ImportLine[]`、`overwrite: ImportLine[]`、`skip: ImportLine[]`、`followups: { create: number; skip: { index: number; reason: string }[] }`）；`myAssistant.importArchive(file: File, options: { dryRun: boolean; overwrite: boolean; expect?: string }): Promise<ImportPlan>`；`myAssistant.exportArchive(): Promise<Blob>`。两个请求的超时是 180 秒。
  - `@/utils/download`：`saveBlob(blob: Blob, name: string): void`。
  - 文案 `assistant.notes.importOpen / exportButton / exported / importHint / importOverwrite / importPreview / importConfirm / importCancel / importChoose / importCreate / importReplace / importSkip / importFollowups / importFollowupSkip / importPinned / importPinnedWarning / importNothing / importDone / importReasons.<原因代码>`。
  - 组件 `AssistantTransfer`（无属性；事件 `imported`；`defineExpose({ pick, setOverwrite })`）。测试钩子：`data-test="import-open"`、`export`、`import-panel`、`import-file`、`import-overwrite`、`import-preview`、`import-cancel`、`import-plan`、`import-pinned-warning`、`import-create`、`import-replace`、`import-skip`、`import-confirm`。

- [ ] **Step 1: 接口封装、下载的小工具与文案**

`web/src/api/myAssistant.ts`：

把 `import { call, http } from '@/api/client'` 换成 `import { ApiError, call, http } from '@/api/client'`。

在 `const root = '/api/me/assistant'` 之前加

```ts
/** 导入清单里的一行：只有路径、是否置顶或跳过的原因代码，没有正文。 */
export interface ImportLine { path: string; pinned?: boolean; reason?: string }
/** 导入的清单。确认时把 `digest` 原样带回去：期间库里有变化，服务端会拒绝并要求重新预览。 */
export interface ImportPlan {
  dry_run: boolean
  digest: string
  create: ImportLine[]
  overwrite: ImportLine[]
  skip: ImportLine[]
  followups: { create: number; skip: { index: number; reason: string }[] }
}

/** 导入与导出要解压、逐页写入或打包，比普通接口慢得多。 */
const SLOW = 180000
```

`myAssistant` 对象里，在 `accessLog: …` 那一项之后加

```ts
  /** `dryRun` 为真只出清单；为假时必须带上预览给的 `expect`。 */
  importArchive: (file: File, options: { dryRun: boolean; overwrite: boolean; expect?: string }) => {
    const form = new FormData()
    form.append('archive', file)
    form.append('dry_run', String(options.dryRun))
    form.append('overwrite', String(options.overwrite))
    if (options.expect) form.append('expect', options.expect)
    return call<ImportPlan>(http.post(`${root}/import`, form, { timeout: SLOW }))
  },
  /** 下载的是二进制：失败时响应体不是 JSON 信封，只带状态码。 */
  exportArchive: async (): Promise<Blob> => {
    try {
      return (await http.get<Blob>(`${root}/export`, { responseType: 'blob', timeout: SLOW })).data
    } catch (e) {
      const err = e as { response?: { status?: number }; message?: string }
      const status = err.response?.status ?? 0
      throw new ApiError(status, status, err.message ?? '')
    }
  },
```

`web/src/utils/download.ts`（新建）：

```ts
/** 让浏览器把一段二进制存成文件。 */
export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
  // 立刻回收地址有的浏览器会取消下载：稍等一下。
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
```

`web/src/i18n/assistant.ts`：

`assistantZh.notes` 里在 `conflict: …` 那一行之后加

```ts
    importOpen: '导入', exportButton: '导出', exported: '已导出',
    importHint: '上传一个 zip：里面每个 .md 文件成为一页，路径取文件在包里的相对路径去掉 .md；文件开头可以用 front matter 写 title、summary、pinned。根目录下的 followups.json 是待跟进清单。压缩包不超过 20MB，里面只能有这两种文件。',
    importOverwrite: '已有的页用包里的内容覆盖（不勾选则跳过已有的页）',
    importPreview: '预览', importConfirm: '确认导入', importCancel: '取消', importChoose: '请先选择一个 zip 文件',
    importCreate: '将新建 {count} 页', importReplace: '将覆盖 {count} 页', importSkip: '将跳过 {count} 项',
    importFollowups: '将新增 {count} 条待跟进', importFollowupSkip: '第 {n} 条待跟进',
    importPinned: '将置顶',
    importPinnedWarning: '清单里有 {count} 页会置顶。置顶页每天第一轮原样交给助理当作规则，请确认它们是你自己写的。',
    importNothing: '没有可以导入的内容。', importDone: '导入完成',
    importReasons: {
      exists: '已有同名的页', invalid_path: '路径不合法', duplicate_path: '包里有重复的路径', page_too_large: '单页超过 200KB',
      empty_page: '正文是空的', invalid_page: '标题或摘要太长', invalid_front_matter: 'front matter 写得不对',
      duplicate: '已有相同的待跟进', invalid_category: '分类不对', invalid_title: '标题为空或太长', invalid_notes: '备注太长',
      invalid_date: '日期要写成 YYYY-MM-DD', invalid_status: '状态只能是 open 或 snoozed', invalid_item: '这一条不是一个对象',
    },
```

`assistantZh.settings.deprovisionPrompt` 的值整个换成

```ts
    deprovisionPrompt: '停用后知识库与待跟进无法恢复，需要的话先到「知识库」分区导出。输入助理的名字「{name}」确认停用',
```

`assistantEn.notes` 的最后一个键（`conflict`）之后加

```ts
    importOpen: 'Import', exportButton: 'Export', exported: 'Exported',
    importHint: 'Upload a zip. Every .md file in it becomes a page whose path is the file path inside the archive without .md, and a file may start with front matter giving title, summary and pinned. A followups.json file at the root is the follow-up list. The archive may be up to 20 MB and may contain only these two kinds of file.',
    importOverwrite: 'Replace existing pages with the ones in the archive (otherwise existing pages are skipped)',
    importPreview: 'Preview', importConfirm: 'Confirm import', importCancel: 'Cancel', importChoose: 'Choose a zip file first',
    importCreate: '{count} pages will be created', importReplace: '{count} pages will be replaced', importSkip: '{count} items will be skipped',
    importFollowups: '{count} follow-ups will be added', importFollowupSkip: 'Follow-up number {n}',
    importPinned: 'Will be pinned',
    importPinnedWarning: '{count} pages in this list will be pinned. Pinned pages are handed to the assistant as rules in the first turn of every day, so make sure you wrote them yourself.',
    importNothing: 'There is nothing to import.', importDone: 'Import finished',
    importReasons: {
      exists: 'A page with this path already exists', invalid_path: 'The path is not valid', duplicate_path: 'The archive has this path more than once', page_too_large: 'The page is over 200 KB',
      empty_page: 'The body is empty', invalid_page: 'The title or summary is too long', invalid_front_matter: 'The front matter is malformed',
      duplicate: 'The same follow-up already exists', invalid_category: 'The category is not valid', invalid_title: 'The title is empty or too long', invalid_notes: 'The notes are too long',
      invalid_date: 'Dates must be written as YYYY-MM-DD', invalid_status: 'The status must be open or snoozed', invalid_item: 'This entry is not an object',
    },
```

`assistantEn.settings.deprovisionPrompt` 的值整个换成

```ts
    deprovisionPrompt: 'After shutdown the knowledge base and follow-ups cannot be recovered. Export them from the Knowledge base tab first if you need them. Type the assistant name "{name}" to confirm',
```

`assistantJa.notes` 的最后一个键之后加

```ts
    importOpen: 'インポート', exportButton: 'エクスポート', exported: 'エクスポートしました',
    importHint: 'zip をアップロードします。中の .md ファイルがそれぞれ 1 ページになり、パスはアーカイブ内の相対パスから .md を除いたものです。ファイルの先頭に front matter で title、summary、pinned を書けます。ルートの followups.json はフォローアップの一覧です。アーカイブは 20MB まで、この 2 種類のファイルだけを含められます。',
    importOverwrite: '既存のページをアーカイブの内容で上書きする（チェックしない場合、既存のページはスキップ）',
    importPreview: 'プレビュー', importConfirm: 'インポートを確定', importCancel: 'キャンセル', importChoose: '先に zip ファイルを選択してください',
    importCreate: '{count} ページを新規作成', importReplace: '{count} ページを上書き', importSkip: '{count} 件をスキップ',
    importFollowups: 'フォローアップを {count} 件追加', importFollowupSkip: 'フォローアップ {n} 件目',
    importPinned: 'ピン留めされます',
    importPinnedWarning: 'この一覧のうち {count} ページがピン留めされます。ピン留めページは毎日最初のターンでルールとしてそのままアシスタントに渡されます。自分で書いたものか確認してください。',
    importNothing: 'インポートできる内容がありません。', importDone: 'インポートが完了しました',
    importReasons: {
      exists: '同じパスのページが既にあります', invalid_path: 'パスが正しくありません', duplicate_path: 'アーカイブ内でパスが重複しています', page_too_large: '1 ページが 200KB を超えています',
      empty_page: '本文が空です', invalid_page: 'タイトルまたは要約が長すぎます', invalid_front_matter: 'front matter の書式が正しくありません',
      duplicate: '同じフォローアップが既にあります', invalid_category: '分類が正しくありません', invalid_title: 'タイトルが空か長すぎます', invalid_notes: 'メモが長すぎます',
      invalid_date: '日付は YYYY-MM-DD で書いてください', invalid_status: '状態は open か snoozed のみです', invalid_item: 'この項目はオブジェクトではありません',
    },
```

`assistantJa.settings.deprovisionPrompt` 的值整个换成

```ts
    deprovisionPrompt: '停止するとナレッジベースとフォローアップは復元できません。必要な場合は先に「ナレッジベース」タブでエクスポートしてください。確認のためアシスタントの名前「{name}」を入力してください',
```

- [ ] **Step 2: 写失败测试**

`web/tests/myAssistantTransfer.spec.ts`（新建）：

```ts
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, http } from '@/api/client'
import { myAssistant } from '@/api/myAssistant'

afterEach(() => { vi.restoreAllMocks() })

describe('import and export calls', () => {
  it('uploads the archive as a form with the preview flags and a long timeout', async () => {
    const post = vi.spyOn(http, 'post').mockResolvedValue({ data: { code: 0, data: { digest: 'd' } } } as never)
    const file = new File(['zip'], 'notes.zip', { type: 'application/zip' })
    await myAssistant.importArchive(file, { dryRun: true, overwrite: false })
    await myAssistant.importArchive(file, { dryRun: false, overwrite: true, expect: 'abc' })
    const [url, first, config] = post.mock.calls[0] as unknown as [string, FormData, { timeout: number }]
    expect(url).toBe('/api/me/assistant/import')
    expect(first.get('archive')).toBeInstanceOf(File)
    expect([first.get('dry_run'), first.get('overwrite'), first.get('expect')]).toEqual(['true', 'false', null])
    expect(config.timeout).toBe(180000)
    const second = post.mock.calls[1][1] as FormData
    expect([second.get('dry_run'), second.get('overwrite'), second.get('expect')]).toEqual(['false', 'true', 'abc'])
  })

  it('downloads the export as a blob and reports a failure with its status', async () => {
    const blob = new Blob(['zip'])
    const get = vi.spyOn(http, 'get').mockResolvedValueOnce({ data: blob } as never)
    expect(await myAssistant.exportArchive()).toBe(blob)
    expect(get.mock.calls[0]).toEqual(['/api/me/assistant/export', { responseType: 'blob', timeout: 180000 }])
    get.mockRejectedValueOnce({ response: { status: 409 }, message: 'Request failed with status code 409' })
    const failure = await myAssistant.exportArchive().catch((e: unknown) => e)
    expect(failure).toBeInstanceOf(ApiError)
    expect((failure as ApiError).status).toBe(409)
  })
})
```

`web/tests/AssistantTransfer.spec.ts`（新建）：

```ts
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/myAssistant', async (original) => ({
  ...(await original<typeof import('@/api/myAssistant')>()),
  myAssistant: { importArchive: vi.fn(), exportArchive: vi.fn() },
}))
vi.mock('@/utils/download', () => ({ saveBlob: vi.fn() }))

import { ApiError } from '@/api/client'
import { myAssistant, type ImportPlan } from '@/api/myAssistant'
import AssistantTransfer from '@/components/assistant/AssistantTransfer.vue'
import { i18n } from '@/i18n'
import { saveBlob } from '@/utils/download'

const t = i18n.global.t
const plan = (over: Partial<ImportPlan>): ImportPlan => ({
  dry_run: true, digest: 'd'.repeat(64),
  create: [{ path: '规则/口吻', pinned: true }, { path: '人/李四', pinned: false }],
  overwrite: [{ path: '项目/续约', pinned: false }],
  skip: [{ path: '旧页', reason: 'exists' }, { path: '怪页', reason: 'brand_new_reason' }],
  followups: { create: 1, skip: [{ index: 2, reason: 'duplicate' }] },
  ...over,
})
const file = new File(['zip'], 'notes.zip', { type: 'application/zip' })
interface Exposed { pick: (chosen: File | null) => void; setOverwrite: (value: boolean) => void }
const render = () => mount(AssistantTransfer, { global: { plugins: [ElementPlus, i18n] } })
const exposed = (wrapper: ReturnType<typeof render>) => wrapper.vm as unknown as Exposed
/** 打开导入面板、选好文件、点预览。 */
async function previewed(wrapper: ReturnType<typeof render>) {
  await wrapper.get('[data-test="import-open"]').trigger('click')
  exposed(wrapper).pick(file); await flushPromises()
  await wrapper.get('[data-test="import-preview"]').trigger('click'); await flushPromises()
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(myAssistant.importArchive).mockReset().mockResolvedValue(plan({}))
  vi.mocked(myAssistant.exportArchive).mockReset().mockResolvedValue(new Blob(['zip']))
  vi.mocked(saveBlob).mockReset()
  vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
  vi.spyOn(ElMessage, 'error').mockReturnValue({ close: () => {} })
})

describe('import and export', () => {
  it('asks for a file before previewing', async () => {
    const warning = vi.spyOn(ElMessage, 'warning').mockReturnValue({ close: () => {} })
    const wrapper = render()
    expect(wrapper.find('[data-test="import-panel"]').exists()).toBe(false)
    await wrapper.get('[data-test="import-open"]').trigger('click')
    expect(wrapper.get('[data-test="import-panel"]').text()).toContain(t('assistant.notes.importHint'))
    await wrapper.get('[data-test="import-preview"]').trigger('click'); await flushPromises()
    expect(warning).toHaveBeenCalledWith(t('assistant.notes.importChoose'))
    expect(myAssistant.importArchive).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('previews first and shows what would be created, replaced and skipped', async () => {
    const wrapper = render(); await previewed(wrapper)
    expect(myAssistant.importArchive).toHaveBeenCalledWith(file, { dryRun: true, overwrite: false })
    const text = wrapper.get('[data-test="import-plan"]').text()
    expect(text).toContain(t('assistant.notes.importCreate', { count: 2 }))
    expect(text).toContain(t('assistant.notes.importReplace', { count: 1 }))
    expect(text).toContain(t('assistant.notes.importSkip', { count: 3 }))
    expect(text).toContain(t('assistant.notes.importFollowups', { count: 1 }))
    expect(wrapper.get('[data-test="import-create"]').text()).toContain('规则/口吻')
    expect(wrapper.get('[data-test="import-create"]').text()).toContain(t('assistant.notes.importPinned'))
    expect(wrapper.get('[data-test="import-replace"]').text()).toContain('项目/续约')
    const skipped = wrapper.get('[data-test="import-skip"]').text()
    expect(skipped).toContain(t('assistant.notes.importReasons.exists'))
    // 还没有文案的原因代码原样显示。
    expect(skipped).toContain('brand_new_reason')
    expect(skipped).toContain(t('assistant.notes.importFollowupSkip', { n: 2 }))
    expect(skipped).toContain(t('assistant.notes.importReasons.duplicate'))
    // 有页面会置顶：单独提醒一句。
    expect(wrapper.get('[data-test="import-pinned-warning"]').text()).toContain(t('assistant.notes.importPinnedWarning', { count: 1 }))
    wrapper.unmount()
  })

  it('confirms with the digest it previewed and tells the parent to reload', async () => {
    const wrapper = render(); await previewed(wrapper)
    await wrapper.get('[data-test="import-confirm"]').trigger('click'); await flushPromises()
    expect(myAssistant.importArchive).toHaveBeenLastCalledWith(file, { dryRun: false, overwrite: false, expect: 'd'.repeat(64) })
    expect(wrapper.emitted('imported')).toHaveLength(1)
    expect(wrapper.find('[data-test="import-panel"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('throws the preview away when the file or the overwrite choice changes', async () => {
    const wrapper = render(); await previewed(wrapper)
    exposed(wrapper).setOverwrite(true); await flushPromises()
    expect(wrapper.find('[data-test="import-plan"]').exists()).toBe(false)
    await wrapper.get('[data-test="import-preview"]').trigger('click'); await flushPromises()
    expect(myAssistant.importArchive).toHaveBeenLastCalledWith(file, { dryRun: true, overwrite: true })
    exposed(wrapper).pick(new File(['other'], 'other.zip')); await flushPromises()
    expect(wrapper.find('[data-test="import-confirm"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('previews again instead of importing when the server says the plan changed', async () => {
    const wrapper = render(); await previewed(wrapper)
    vi.mocked(myAssistant.importArchive)
      .mockRejectedValueOnce(new ApiError(409, 409, '预览之后内容有变化，请核对新的清单后再确认'))
      .mockResolvedValueOnce(plan({ digest: 'e'.repeat(64), create: [] }))
    await wrapper.get('[data-test="import-confirm"]').trigger('click'); await flushPromises()
    expect(ElMessage.error).toHaveBeenCalledWith('预览之后内容有变化，请核对新的清单后再确认')
    expect(myAssistant.importArchive).toHaveBeenCalledTimes(3)
    expect(myAssistant.importArchive).toHaveBeenLastCalledWith(file, { dryRun: true, overwrite: false })
    expect(wrapper.emitted('imported')).toBeUndefined()
    expect(wrapper.get('[data-test="import-plan"]').text()).toContain(t('assistant.notes.importCreate', { count: 0 }))
    wrapper.unmount()
  })

  it('will not confirm a plan that imports nothing', async () => {
    vi.mocked(myAssistant.importArchive).mockResolvedValue(plan({ create: [], overwrite: [], followups: { create: 0, skip: [] } }))
    const wrapper = render(); await previewed(wrapper)
    expect(wrapper.get('[data-test="import-plan"]').text()).toContain(t('assistant.notes.importNothing'))
    expect(wrapper.get('[data-test="import-confirm"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-test="import-pinned-warning"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('downloads the export', async () => {
    const wrapper = render()
    await wrapper.get('[data-test="export"]').trigger('click'); await flushPromises()
    expect(myAssistant.exportArchive).toHaveBeenCalledTimes(1)
    expect(saveBlob).toHaveBeenCalledWith(expect.any(Blob), 'assistant-export.zip')
    expect(ElMessage.success).toHaveBeenCalledWith(t('assistant.notes.exported'))
    wrapper.unmount()
  })
})
```

`web/tests/AssistantNotes.spec.ts`：导入与导出有自己的用例，这里把它换成桩。把

```ts
const render = () => mount(AssistantNotes, { global: { plugins: [ElementPlus, i18n] } })
```

换成

```ts
const render = () => mount(AssistantNotes, { global: { plugins: [ElementPlus, i18n], stubs: { AssistantTransfer: true } } })
```

并在 `describe('knowledge base', …)` 里加一条用例：

```ts
  it('offers import and export, reloads after an import, and hides both when locked', async () => {
    const wrapper = render(); await flushPromises()
    const transfer = wrapper.getComponent({ name: 'AssistantTransfer' })
    transfer.vm.$emit('imported'); await flushPromises()
    expect(myAssistant.notes).toHaveBeenCalledTimes(2)
    wrapper.unmount()
    vi.mocked(myAssistant.notes).mockResolvedValue({ items: [], locked: true })
    const locked = render(); await flushPromises()
    expect(locked.findComponent({ name: 'AssistantTransfer' }).exists()).toBe(false)
    locked.unmount()
  })
```

`web/tests/AssistantSettings.spec.ts`：`it('shuts the assistant down only after its name is typed', …)` 里，在 `expect(prompt.mock.calls[0][0]).toBe(…)` 之后加一句：

```ts
    // 停用前提示可以先导出。
    expect(prompt.mock.calls[0][0]).toContain('导出')
```

- [ ] **Step 3: 运行确认失败**

Run（在 `web/` 下）: `npx vitest run tests/AssistantTransfer.spec.ts tests/myAssistantTransfer.spec.ts tests/AssistantNotes.spec.ts tests/AssistantSettings.spec.ts`
Expected: FAIL：找不到 `@/components/assistant/AssistantTransfer.vue`；`AssistantNotes` 里没有 `AssistantTransfer` 组件。（接口封装与文案在 Step 1 已经加了：`myAssistantTransfer.spec.ts` 与停用那一句断言此时通过。）

- [ ] **Step 4: 实现**

`web/src/components/assistant/AssistantTransfer.vue`（新建）：

```vue
<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ApiError } from '@/api/client'
import { myAssistant, type ImportPlan } from '@/api/myAssistant'
import { saveBlob } from '@/utils/download'
import { errorMessage } from '@/utils/errors'

const emit = defineEmits<{ imported: [] }>()
const { t, te } = useI18n()
const open = ref(false), busy = ref(false), overwrite = ref(false)
const file = ref<File | null>(null)
const plan = ref<ImportPlan | null>(null)

/** 清单里会置顶的页数：置顶页每天当规则交给助理，要单独提醒。 */
const pinned = computed(() => (plan.value ? [...plan.value.create, ...plan.value.overwrite].filter((line) => line.pinned).length : 0))
const skipped = computed(() => (plan.value ? plan.value.skip.length + plan.value.followups.skip.length : 0))
const nothing = computed(() => !!plan.value && !plan.value.create.length && !plan.value.overwrite.length && !plan.value.followups.create)

/** 换了文件或「覆盖」选项，看过的清单就作废：必须重新预览。 */
function pick(chosen: File | null) { file.value = chosen; plan.value = null }
function onFile(event: Event) { pick((event.target as HTMLInputElement).files?.[0] ?? null) }
function setOverwrite(value: boolean) { overwrite.value = value; plan.value = null }
function close() { open.value = false; overwrite.value = false; pick(null) }
/** 原因代码 → 文案；后端新加的、这里还没有文案的原样显示代码。 */
function reason(code: string | undefined) {
  const key = 'assistant.notes.importReasons.' + code
  return code && te(key) ? t(key) : (code ?? '')
}

async function preview() {
  if (!file.value) { ElMessage.warning(t('assistant.notes.importChoose')); return }
  busy.value = true
  try { plan.value = await myAssistant.importArchive(file.value, { dryRun: true, overwrite: overwrite.value }) }
  catch (e) { plan.value = null; ElMessage.error(errorMessage(e)) }
  finally { busy.value = false }
}
/** 第二步：带着预览给的摘要确认。服务端发现清单已经变了（409）时不写，这里重新出一张清单。 */
async function confirm() {
  if (!file.value || !plan.value || nothing.value) return
  busy.value = true
  try {
    await myAssistant.importArchive(file.value, { dryRun: false, overwrite: overwrite.value, expect: plan.value.digest })
    ElMessage.success(t('assistant.notes.importDone'))
    close()
    emit('imported')
  } catch (e) {
    ElMessage.error(errorMessage(e))
    if (e instanceof ApiError && e.status === 409) await preview()
  } finally { busy.value = false }
}
async function download() {
  busy.value = true
  try {
    saveBlob(await myAssistant.exportArchive(), 'assistant-export.zip')
    ElMessage.success(t('assistant.notes.exported'))
  } catch (e) { ElMessage.error(errorMessage(e)) }
  finally { busy.value = false }
}
defineExpose({ pick, setOverwrite })
</script>
<template>
  <div class="transfer">
    <el-button
      data-test="import-open"
      @click="open = true"
    >
      {{ t('assistant.notes.importOpen') }}
    </el-button>
    <el-button
      :loading="busy && !open"
      data-test="export"
      @click="download"
    >
      {{ t('assistant.notes.exportButton') }}
    </el-button>
    <div
      v-if="open"
      class="import-panel"
      data-test="import-panel"
    >
      <p class="muted">
        {{ t('assistant.notes.importHint') }}
      </p>
      <input
        type="file"
        accept=".zip,application/zip"
        data-test="import-file"
        @change="onFile"
      >
      <el-checkbox
        :model-value="overwrite"
        data-test="import-overwrite"
        @update:model-value="setOverwrite(!!$event)"
      >
        {{ t('assistant.notes.importOverwrite') }}
      </el-checkbox>
      <div>
        <el-button
          :loading="busy"
          data-test="import-preview"
          @click="preview"
        >
          {{ t('assistant.notes.importPreview') }}
        </el-button>
        <el-button
          data-test="import-cancel"
          @click="close"
        >
          {{ t('assistant.notes.importCancel') }}
        </el-button>
      </div>
      <div
        v-if="plan"
        data-test="import-plan"
      >
        <el-alert
          v-if="pinned"
          type="warning"
          :closable="false"
          show-icon
          data-test="import-pinned-warning"
          :title="t('assistant.notes.importPinnedWarning', { count: pinned })"
        />
        <h4>{{ t('assistant.notes.importCreate', { count: plan.create.length }) }}</h4>
        <ul data-test="import-create">
          <li
            v-for="line in plan.create"
            :key="line.path"
          >
            <code>{{ line.path }}</code>
            <el-tag
              v-if="line.pinned"
              type="warning"
              size="small"
            >
              {{ t('assistant.notes.importPinned') }}
            </el-tag>
          </li>
        </ul>
        <h4>{{ t('assistant.notes.importReplace', { count: plan.overwrite.length }) }}</h4>
        <ul data-test="import-replace">
          <li
            v-for="line in plan.overwrite"
            :key="line.path"
          >
            <code>{{ line.path }}</code>
            <el-tag
              v-if="line.pinned"
              type="warning"
              size="small"
            >
              {{ t('assistant.notes.importPinned') }}
            </el-tag>
          </li>
        </ul>
        <h4>{{ t('assistant.notes.importSkip', { count: skipped }) }}</h4>
        <ul data-test="import-skip">
          <li
            v-for="(line, i) in plan.skip"
            :key="'p' + i"
          >
            <code>{{ line.path }}</code>
            <span class="muted">{{ reason(line.reason) }}</span>
          </li>
          <li
            v-for="item in plan.followups.skip"
            :key="'f' + item.index"
          >
            <span>{{ t('assistant.notes.importFollowupSkip', { n: item.index }) }}</span>
            <span class="muted">{{ reason(item.reason) }}</span>
          </li>
        </ul>
        <p>{{ t('assistant.notes.importFollowups', { count: plan.followups.create }) }}</p>
        <p
          v-if="nothing"
          class="muted"
        >
          {{ t('assistant.notes.importNothing') }}
        </p>
        <el-button
          type="primary"
          :disabled="nothing"
          :loading="busy"
          data-test="import-confirm"
          @click="confirm"
        >
          {{ t('assistant.notes.importConfirm') }}
        </el-button>
      </div>
    </div>
  </div>
</template>
<style scoped>
.transfer { display: inline-block; margin-left: 12px; vertical-align: top; }
.import-panel { margin-top: 12px; padding: 12px; max-width: 760px; border: 1px solid var(--el-border-color); border-radius: 4px; }
.import-panel ul { list-style: none; padding: 0; }
.import-panel li { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; padding: 2px 0; }
.muted { color: var(--el-text-color-secondary); }
</style>
```

`web/src/components/assistant/AssistantNotes.vue`：

`<script setup>` 的导入里，在 `import LoadState from '@/components/LoadState.vue'` 之前加

```ts
import AssistantTransfer from '@/components/assistant/AssistantTransfer.vue'
```

模板里，在「新建一页」按钮（`data-test="create"` 的 `<el-button>`）的 `</el-button>` 之后加

```vue
      <!-- 密钥不可用时内容读不出来也写不进去：不给导入导出。导入完成后重新取索引。 -->
      <AssistantTransfer
        v-if="!index.locked"
        @imported="load"
      />
```

实现时留意：

- 面板是页面里的一块，不用 `el-dialog`：对话框会被传送到 `body`，jsdom 里的用例够不着。
- 文件输入框在 jsdom 里不能真的选文件，用例经 `defineExpose` 的 `pick` 放进去（3C 的「设计选择」第 8 条同样的做法）。
- 确认失败且状态码是 409 时重新预览：`plan_changed` 之外的 409（比如超过上限）会在重新预览时再报一次同样的原因，面板里不留旧清单。

- [ ] **Step 5: 运行确认通过**

Run（在 `web/` 下）: `npx vitest run tests/AssistantTransfer.spec.ts tests/myAssistantTransfer.spec.ts tests/AssistantNotes.spec.ts tests/AssistantSettings.spec.ts tests/assistantI18n.spec.ts tests/i18n.spec.ts`
Expected: PASS。

Run（在 `web/` 下）: `npm run lint && npm run build`
Expected: 没有 error；`vue-tsc` 与构建通过。

- [ ] **Step 6: 提交**

```bash
git add web/src/api/myAssistant.ts web/src/utils/download.ts web/src/i18n/assistant.ts \
  web/src/components/assistant/AssistantTransfer.vue \
  web/src/components/assistant/AssistantNotes.vue \
  web/tests/AssistantTransfer.spec.ts web/tests/myAssistantTransfer.spec.ts \
  web/tests/AssistantNotes.spec.ts web/tests/AssistantSettings.spec.ts
git commit -m "我的助理页：知识库可以导入（先预览清单再确认）与导出，停用前提示先导出"
```

---

### Task 11: 文档、CHANGELOG、spec 与总览同步，整体验证

**Files:**
- Modify: `docs/feishu.md`（「个人助理应用」一节）
- Modify: `docs/operations.md`（「个人助理投递排障」之后加一小节）
- Modify: `CHANGELOG.md`（`## [Unreleased]` 的 `### Added` 第一条）
- Modify: `docs/superpowers/specs/2026-11-11-personal-assistant.md`（威胁模型表、第九节的表、第十节「导入」、第十一节）
- Modify: `docs/superpowers/plans/2026-11-13-personal-assistant-phase2-3.md`（分层表、裁定 10、「3B、3C、3D」）

**Interfaces:**
- Consumes: Task 1–10 落地后的行为。
- Produces: 文档与实现一致；本层的全部用例通过。

- [ ] **Step 1: `docs/feishu.md`**

「个人助理应用」一节的列表里，在以 `- **待跟进。**` 开头的那一条之后加两条：

```markdown
- **导入与导出。** 所有者在管理台「我的助理」页的「知识库」分区可以上传一个 zip，把已有的笔记并进知识库：包里每个 `.md` 文件成为一页，页的路径是文件在包里的相对路径去掉 `.md`；文件开头可以用 YAML front matter（两行 `---` 之间）写 `title`、`summary`、`pinned`，其余键忽略。包的根目录下可以放一个 `followups.json`（`{"version": 1, "items": [{"category": "decide|promised|waiting|remind", "title": "…", "notes": "…", "review_on": "YYYY-MM-DD", "due_on": "YYYY-MM-DD", "status": "open|snoozed"}]}`，只有 `category` 与 `title` 必填）导入待跟进。导入分两步：先预览，页面列出将新建、覆盖、跳过的路径与原因（不显示正文），并标出哪些页会置顶；确认后才写入。已有的页默认跳过，勾选「覆盖」才整页替换；预览之后知识库有变化（比如助理又写了一页）时确认会被拒绝，需要重新预览。整个导入要么全部写入、要么一页都不写，导入的页记为 `import` 所写。压缩包不超过 20MB、页面文件不超过 2000 个，只能有 `.md` 与根目录的 `followups.json`；带 `..` 或绝对路径的文件名、符号链接、加密的条目、嵌套的压缩包、非 UTF-8 的文本都会让整个包被拒绝（用系统自带的压缩功能打包时，注意去掉 `.DS_Store` 之类的隐藏文件）。同一个分区的「导出」把全部知识页与未结的待跟进打成同样格式的 zip 下载，可以原样导回；已关闭的待跟进、对话记录与审批流水不在导出里。这两个接口（`POST /api/me/assistant/import`、`GET /api/me/assistant/export`）只对在管理台里登录的所有者本人开放。
- **访问记录。** 助理自己的轮次之外，凡是把加密保存的个人内容解出来的操作都记一行（表 `assistant_access_log`）：所有者在「我的助理」页上的读取、导入的预览与写入、导出、所有者在管理台读自己的对话记录与任务详情，以及服务器上的管理命令。记录里只有助理、操作人、操作人类别（`owner` 本人、`admin` 其他登录用户、`system` 服务器上的管理命令）、动作代码、一个不含内容的对象（范围名或计数）与时间；知识页的路径、标题、正文都不记。所有者在「我的助理」页的「设置」分区里可以翻看自己的记录，不是本人的访问会醒目标出；平台管理员只在助理列表里看到每位助理「最近一次非本人访问」的时间。记录保留 180 天。管理命令以某位成员的身份（`--as`）建的会话不算本人在场：`/api/me/assistant` 下的接口一律拒绝它（403），用它读对话记录、任务详情时照常返回，但记成 `system`。
```

- [ ] **Step 2: `docs/operations.md`**

在 `## 隔离演练` 之前（「个人助理投递排障」一节之后）加：

```markdown
### 个人助理访问记录

表 `assistant_access_log` 记运行时路径之外对个人助理内容的每一次解密，没有任何内容列：`bot_id`、`actor_id`（操作人，账号删除后为空）、`actor_kind`（`owner` / `admin` / `system`）、`action`、`target`（范围名或计数）、`at`。它和审计日志（`audit_logs`）是两回事：审计记谁改了平台上的什么，给管理员看；访问记录记谁读了某位所有者的个人内容，给所有者本人看（「我的助理」页的「设置」分区），管理员只在 `GET /api/admin/assistants` 的 `last_non_owner_access_at` 里看到最近一次非本人访问的时间。

| 动作 | 什么时候记 |
|---|---|
| `notes.index`、`notes.read`、`followups.read`、`jobs.read`、`approvals.read`、`settings.read` | 所有者在「我的助理」页上读取 |
| `import.preview`、`import.notes`、`export` | 导入的预览（含没写成的确认）、导入写入成功、导出；对象是计数 |
| `chatlogs.read`、`tasks.read` | 所有者在管理台读自己助理的对话记录、任务详情 |
| `cli.chatlogs`、`cli.tasks` | 管理命令以所有者的身份（`--as`）读对话记录、任务详情；记成 `system`，不留操作人 |
| `cli.rewrap` | `assistant-keys rewrap --apply` 重新包裹了这位所有者的数据密钥；没有读内容 |

- **保留。** 180 天；scheduler 的保留期那一档每小时分批清理，计数在日志里是 `old_assistant_access_log`。助理记录被删除时它的访问记录一并删除。
- **写不进去不挡读取。** 记录与读取在同一个事务里提交。单独这一行写失败时（应用日志 `assistant_access_log_failed`，带助理编号、动作与错误类型）读取照常返回；每次解密另有一条应用日志 `assistant_content_access`（助理、操作人、类别、动作、对象），可以拿来补查。持续出现 `assistant_access_log_failed` 说明表不可用，要当故障处理。
- **管理命令。** `coreman … --as 登录名` 以那位成员的身份建一个管理台会话（`admin_sessions.auth_method = 'cli'`）。`/api/me/assistant` 下的接口不受理这种会话（403 `cli_session_refused`）：所有者的知识库、待跟进、导入导出只能由他本人在管理台里操作。
- **上传的大小。** 导入的压缩包上限 20MB，由应用在收下请求体之后判断；反向代理若没有限制请求体大小，超大的上传会先落到应用的临时目录再被拒绝。建议在反向代理上给 `/api/me/assistant/import` 设 25MB 左右的请求体上限。装满的知识库导入要十几秒，代理的超时不要低于 180 秒。
- **排查。** 所有者问「谁看过我的内容」：让他看「设置」分区的访问记录；管理员一侧只能确认时间。直接连库、拿主密钥解密不经过平台，不会留下记录（见设计文档的威胁模型）。
```

- [ ] **Step 3: `CHANGELOG.md`**

`## [Unreleased]` 的 `### Added` 之下，作为第一条加：

```markdown
- Personal assistants: an access log, and import and export of the knowledge base. Every decryption of an assistant's content outside its own turns is recorded in the new table `assistant_access_log` (migration `0102`) with the assistant, the actor, the actor kind (`owner`, `admin` or `system`), an action code, a target that holds only a scope name or counts, and the time; page paths, titles and bodies are never recorded. This covers the owner's reads on the "My assistant" page, the owner reading their own assistant's conversation logs and task details in the admin console, imports, exports and server-side commands; `assistant-keys rewrap --apply` records `cli.rewrap` for each assistant whose data key it rewrapped. The record is committed in the same transaction as the read, so content is not returned when the commit fails, while a failure to write the record alone is logged (`assistant_access_log_failed`) and does not block the read. Owners see their own log under Settings on the "My assistant" page (`GET /api/me/assistant/access-log`), with access by anyone else highlighted; administrators only see `last_non_owner_access_at` on `GET /api/admin/assistants`. Records are kept for 180 days. Sessions created by the admin CLI on behalf of a member (`--as`) are no longer accepted by `/api/me/assistant` (403), and reading conversation logs or task details through such a session is recorded as `system`. Owners can import a zip of Markdown files into the knowledge base (`POST /api/me/assistant/import`): each `.md` file becomes a page at its path without the extension, an optional YAML front matter supplies `title`, `summary` and `pinned`, and an optional `followups.json` at the root adds follow-ups. The import previews first (pages to create, replace and skip with reasons, no bodies, pinned pages marked) and writes only after the owner confirms with the digest of that preview; existing pages are skipped unless overwrite is chosen, a preview made stale by a concurrent change is refused, and the whole import is one transaction. Archives are read in memory only and are refused when over 20 MB, over 2000 page files, or containing path traversal, symbolic links, encrypted entries, nested archives, files other than `.md` and the root `followups.json`, or text that is not UTF-8. `GET /api/me/assistant/export` downloads all pages and open follow-ups in the same format. Runtime nodes need no upgrade; ordinary employees are not affected. See `docs/feishu.md` and `docs/operations.md`.
```

- [ ] **Step 4: spec 与总览同步**

`docs/superpowers/specs/2026-11-11-personal-assistant.md`：

威胁模型表里，把

```markdown
| 其他成员、团队负责人、平台管理员（经管理台和接口） | 不能 | 接口只对本人放行 |
```

换成

```markdown
| 其他成员、团队负责人、平台管理员（经管理台和接口） | 不能 | 接口只对本人放行；「我的助理」接口不受理管理命令以成员身份建的会话 |
```

把

```markdown
| 同时拿到数据库和平台主密钥的人 | 能 | 不防；记录解密访问审计 |
```

换成

```markdown
| 同时拿到数据库和平台主密钥的人（含能在服务器上执行管理命令的人） | 能 | 不防；经平台的接口与命令解密时记访问记录（`assistant_access_log`），直接读库解密不留痕 |
```

第九节的表里，「设置」一行的内容末尾加「、访问记录」；表里有「知识库」一行就在它的内容末尾加「、导入与导出」，没有就在「最近落地」一行之后加一行：

```markdown
| 知识库 | 索引、查看与编辑、置顶、导入与导出 |
```

第十节里，把「**导入**：提供管理命令，把一个目录里的 Markdown 页面与待跟进清单导入某人的知识库，供已有个人笔记的成员迁入。由所有者本人执行。」整段换成：

```markdown
**导入与导出**：所有者本人在「我的助理」页的「知识库」分区上传一个 zip（`POST /api/me/assistant/import`），把已有的个人笔记并进知识库：每个 `.md` 文件成为一页，路径取相对路径去掉 `.md`，文件开头可选的 YAML front matter 里的 `title`、`summary`、`pinned` 被采用（导入是所有者在场的操作，可以置顶；写入记为 `import`）；根目录下可选的 `followups.json` 导入待跟进。先预览（将新建、覆盖、跳过的路径与原因，不回显正文，置顶的页标出），确认时带回预览的摘要才写入；已有的页默认跳过，选了覆盖才整页替换；预览之后有变化则拒绝并要求重新预览。整个导入一个事务。压缩包只在内存里读：上限 20MB、页面文件 2000 个、解包后合计 32MB（正文合计仍受知识库 20MB 上限约束），拒绝路径穿越、符号链接、加密条目、嵌套压缩包、非 `.md` 文件与非 UTF-8 文本。`GET /api/me/assistant/export` 把全部知识页与未结的待跟进打成同样格式的 zip；停用确认里提示可以先导出。两者都记访问记录。没有做成管理命令：管理命令在服务器上由运维执行，不是所有者本人。
```

第十一节里，把以「- 新表 `assistant_access_log`：」开头的那一条整条换成：

```markdown
- 新表 `assistant_access_log`（`id`、`bot_id`、`actor_id`、`actor_kind`、`action`、`target`、`at`，没有内容列）：记录运行时路径之外的每次解密——「我的助理」页的读取、导入与导出、所有者在管理台读自己的对话记录与任务详情、服务器上的管理命令（密钥轮换记 `cli.rewrap`；管理命令以成员身份读对话记录记成 `system`）。`target` 只放范围名或计数，知识页的路径属于内容，不放。记录与读取同一个事务提交；单独这一行写失败不挡读取，只记应用日志。所有者在「设置」分区查看自己的记录；保留 180 天。一张按语法树扫描的测试台账要求每一处这样的解密都在同一个函数里记账或写明豁免理由。
```

把「- 管理员能看到的只有：谁开通了、所在节点、轮次与额度用量、轮询健康状态。」换成：

```markdown
- 管理员能看到的只有：谁开通了、所在节点、轮次与额度用量、轮询健康状态、最近一次非所有者访问个人内容的时间（没有动作与对象）。
```

`docs/superpowers/plans/2026-11-13-personal-assistant-phase2-3.md`：

「分层」表里，3D 一行的迁移是 `` `0102_assistant_access_log` ``（与实际落地的编号一致）、内容写成「`assistant_access_log`、导入与导出接口」；3B 一行的迁移若还是 `0101_assistant_dispatch`，改成「—」（3B 没有迁移，`0101` 是 `0101_assistant_hardening`）。

裁定 10 的末尾加一句：

```markdown
落地时的补充（3D 计划）：导出与导入用同一种格式；预览与确认之间用清单的摘要做乐观锁；管理命令以成员身份建的会话进不了 `/api/me/assistant`；解包后的硬上限取 32MB（正文合计仍按知识库的 20MB 算），否则装满的知识库导出后导不回去。
```

「### 3B、3C、3D」一节的末尾加：

```markdown
3D 落地的接口：

- 迁移 `0102_assistant_access_log`：表 `assistant_access_log`（`id`、`bot_id`、`actor_id`、`actor_kind`、`action`、`target`、`at`）。
- `coreman/core/assistants/access_log.py`：`record(session, bot_id, actor_id, action, target="", *, kind=None)`（调用方提交）、`record_opened(session, rows, actor_id, action) -> int`、`list_(session, bot_id, *, limit=50, before=None)`、`last_foreign(session)`、`cleanup(session, now, *, limit=500)`；动作代码在 `ACTIONS`。
- `coreman/core/assistants/archive.py`（压缩包的格式与安全检查，纯函数）与 `transfer.py`（`plan` / `apply` / `preview` / `counts` / `export`）。
- `GET /api/me/assistant/access-log`、`POST /api/me/assistant/import`、`GET /api/me/assistant/export`；`GET /api/admin/assistants` 多 `last_non_owner_access_at`。
- `deps.via_cli(request)`：管理命令建的会话。之后新增只对本人开放的接口时用它区分。
- 之后的层新增运行时路径之外的解密时：在同一个函数里调 `access_log.record`，否则 `tests/unit/test_assistant_access_ledger.py` 失败。
```

- [ ] **Step 5: 整体验证**

Run: `uv run pytest tests/unit/test_assistant_archive.py tests/unit/test_assistant_access_ledger.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: PASS。

Run: `uv run pytest tests/integration/test_assistant_access_log.py tests/integration/test_assistant_transfer.py tests/integration/test_assistant_keys_rewrap.py tests/integration/test_scheduler_reaper.py -q`
Expected: PASS。

Run: `uv run pytest tests/api -q -k "my_assistant or assistant_admin or chat_logs"`
Expected: PASS。

Run: `uv run pytest tests -q -k "assistant"`
Expected: PASS（个人助理各层的用例没有被本层带坏）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman && uv run alembic check`
Expected: 全部通过；`alembic check` 报没有待生成的迁移。

Run（在 `web/` 下）: `npx vitest run && npm run lint && npm run build`
Expected: 全部通过。

按仓库约定跑一遍敏感词自查（公司名、内部域名、真实人名、本机绝对路径），有输出就改成通用表述。

- [ ] **Step 6: 提交**

```bash
git add docs/feishu.md docs/operations.md CHANGELOG.md \
  docs/superpowers/specs/2026-11-11-personal-assistant.md \
  docs/superpowers/plans/2026-11-13-personal-assistant-phase2-3.md
git commit -m "个人助理导入导出与访问记录：文档、CHANGELOG、spec 与总览同步"
```

---

## 没覆盖到的地方

写在这里的都是本层有意不做、或需要所有者拍板的事，不是待办的占位。

- **管理命令的会话仍然读得到对话记录。** `coreman … --as 所有者` 调 `/api/admin/chat-logs`、`/api/dev/tasks/{id}` 时，第 1 期的代码按「查看者就是所有者」还原内容；本层只是把它记成 `system`，没有改成不还原。要堵上，应在这两处把 `viewer` 换成一个不等于任何人的值（或在 `via_cli` 为真时直接跳过还原）。这会改变管理命令现有的行为，需要所有者决定。
- **上传在应用判断大小之前已经被收下。** FastAPI 在依赖与处理函数之前就解析整个 multipart 请求体，20MB 的上限挡的是「进内存与进库」，挡不住「传上来」。仓库里现有的上传接口（应用图标）是同样的情况，仓库的部署文件也没有对请求体大小的限制。本层只在 `docs/operations.md` 里建议在反向代理上限制；要在应用里挡，需要一个按路径限制请求体字节数的 ASGI 中间件。
- **系统自带的压缩功能会带上隐藏文件。** 按任务书，`.md` 与根目录 `followups.json` 之外的任何文件都让整个包被拒绝；用访达等工具直接压缩文件夹产生的 `__MACOSX/`、`.DS_Store` 也在其列，所有者要先清掉。若要对这几个已知的系统文件网开一面（忽略而不是拒绝），在 `archive._check` 里加一份忽略名单即可，本层没有做。
- **不剥最外层目录。** 压缩一个文件夹得到的包里，路径都带着那个文件夹的名字（`笔记/人/李四`）。预览里看得到，所有者可以重新打包；没有做「只有一个顶层目录时自动去掉」。
- **文件名的编码。** 没有打 UTF-8 标志的 zip（部分旧工具在中文系统上产生）里，中文文件名会被 `zipfile` 按 CP437 解出乱码；不会被拒绝，但页的路径是乱的，预览里看得到。
- **大包导入慢。** 每页一次 `notes.write`（约五条语句），2000 页要十几秒，期间这位助理的知识库写入被同一把咨询锁串行。前端把这两个请求的超时放到了 180 秒；反向代理的超时需要相应放宽。要快，得给 `notes` 加一个批量写入口，本层没有动 2B 的写路径。
- **导出不是真正的流式。** 压缩包先在内存里打好（至多三十多 MB）再分块发出：访问记录要在发出第一个字节之前提交，而且要先知道计数。内容几乎不可压缩、打出来超过 20MB 的知识库，导出的包自己导不回去（撞上传上限）。
- **预览并非一条语句都不写。** 它写一条访问记录（它读了知识库的目录）；`followups.list_` 会顺手把到了回看日的推后条目改回未结；第一次调用时可能按默认值建设置行。页面与待跟进的内容不会被预览改动。
- **`admin` 这个类别目前没有产生者。** 接口层没有任何一条路能让非所有者解密成功，所以除管理命令（`system`）外，记录都是 `owner`。类别留着，给以后可能出现的「经授权的他人访问」。
- **不经平台的解密不留痕。** 直接连库并持有主密钥的人、节点上的 root 不经过 `access_log`；这在 spec 的威胁模型里本来就是不防的对象。
- **助理记录被删除时访问记录一并删除**（外键 `ON DELETE CASCADE`，与助理的其它表一致）。停用完成前记录删不掉；停用之后内容已经不可解，记录随记录删除而消失。要让它活得比助理记录久，得去掉外键、另存所有者编号。
- **看访问记录本身不记。** `GET /api/me/assistant/access-log` 不解密任何东西；概览（`GET /api/me/assistant`）只有元数据，同样不记。
- **导出不含的东西。** 已关闭的待跟进、对话记录、审批流水、设置（重点群等）、访问记录本身。
- **台账扫不到自动还原。** `session.get` / `session.scalar` 取整行时内容自动还原，`test_assistant_access_ledger.py` 认不出这一类；它们由 `test_assistant_sealed_ledger.py` 的整行读取台账守着，但那张台账不要求记访问日志。接口层今天没有这样的输出口（`dev.get_task` 之后有一次带 `viewer=` 的收回，已被本层的台账覆盖）。
- **3B 的求助记录。** 目标员工那一轮按裁定 17 明文存在目标员工名下、只对所有者本人可见，不是用所有者密钥加密的内容，不属于访问记录的范围。

## Self-Review

**Spec 与任务书的覆盖**

| 要求 | 落在哪 |
|---|---|
| 迁移（任务书写 `0101`，实际顺延为 `0102_assistant_access_log`，`down_revision` `0101`）、七列、无内容列、按 `bot_id, at` 索引 | Task 1（另加保留期用的 `at` 索引与非所有者的部分索引） |
| `record(...)` 落库、签名不变；`list_(session, bot_id, *, limit, before)` | Task 1 |
| 写失败不挡主操作、与主操作同一事务，取舍写明 | 「设计选择」第 1 条；Task 1 的实现与两条用例；Task 2 补提交 |
| `/api/me/assistant` 下的读取都记 | Task 2（`READS` 台账：每条 GET 路由 → 应留下的行） |
| 导入、导出记 | Task 7（`import.preview` / `import.notes`）、Task 8（`export`） |
| 会解密助理内容的管理命令逐个接上；`assistant-keys rewrap` 记 `cli.rewrap` | Task 3（`cli.rewrap`；管理命令经接口读对话记录、任务详情记 `cli.chatlogs` / `cli.tasks`；`/api/me/assistant` 拒绝管理命令的会话） |
| 第 1 期已有的页面外解密（对话记录、任务详情） | Task 3 |
| 台账式测试：`viewer=` 或接口 / 管理命令层的解密必须同函数记账或豁免 | Task 3（`test_assistant_access_ledger.py`） |
| 180 天清理 | Task 1（`cleanup`）、Task 4（接进 `run_retention`） |
| `GET /api/me/assistant/access-log`（分页）与页面上的只读列表 | Task 4、Task 9 |
| 管理员只看到「最近一次非所有者访问的时间」 | Task 4、Task 9 |
| `POST /api/me/assistant/import`：20MB、解包上限、条目数、穿越、符号链接、嵌套包、非 UTF-8、非 `.md`，逐条写明并测试 | Task 5（表与 45 个用例）、Task 7（接口层逐条再测） |
| Markdown 文件成页、front matter 的三个键、置顶、`updated_by="import"`、`owner_present=True` | Task 5、Task 6、Task 7 |
| `followups.md` 或 `followups.json` 选一种并写明语法 | 「设计选择」第 7 条；Task 5 |
| `dry_run` 清单（只给路径与原因）、确认后写入、默认跳过、`overwrite` 才覆盖（带版本） | Task 6、Task 7 |
| 一个事务、失败整体回滚、访问记录 `import.notes`（对象是计数） | Task 7 |
| 前端「导入」按钮与两步确认 | Task 10 |
| 导出（流式、`NO_STORE`、`Content-Disposition`）、访问记录 `export`、停用确认里提示先导出 | Task 8、Task 10 |
| 文档：`docs/feishu.md`、`docs/operations.md`、spec 第十与十一节、CHANGELOG | Task 11 |
| 测试：记录里没有内容 | Task 1（列）、Task 2（`test_the_log_holds_no_content`）、Task 7 与 8（对象只有计数） |
| 测试：别的用户读不到别人的访问记录 | Task 1（`list_`）、Task 4（`test_nobody_reads_someone_elses_records`） |
| 测试：每个读接口都产生一条记录 | Task 2 |
| 测试：台账能抓到一处漏记 | Task 3（`test_the_ledger_catches_a_route_that_forgets_to_record`） |
| 测试：180 天清理 | Task 1、Task 4 |
| 测试：`dry_run` 不写库；覆盖与跳过；置顶页确实置顶且 `updated_by` 为 `import`；事务回滚 | Task 6、Task 7 |
| 测试：导出再导入的往返一致；导出的访问记录 | Task 5（单页与清单）、Task 8 |
| 测试：管理员与机器人令牌调不了导入导出 | Task 7、Task 8 各一条；全前缀的守卫台账（Task 7、8 各登记一条路由）；管理命令的会话（Task 3） |

**与任务书不同的地方**（都在「设计选择」或「没覆盖到的地方」里写了原因）

- 动作代码沿用 3C 实际用的（`followups.read` 等），不是任务书举例的 `followups.list` 等。
- front matter 用仓库已有的 PyYAML，没有自己写解析器。
- 解包上限 32MB 而不是 20MB（正文合计仍按 20MB）。
- 多做的两件事：`/api/me/assistant` 拒绝管理命令的会话；确认时必须带回预览的摘要。

**占位符扫描**：全文没有待定项；每个代码步骤都给了完整代码。对 3C 的依赖写成了确切的名字与签名，列在「对已有代码与 3C 的假定」里。Task 4 的管理台列表与 Task 11 的 spec 表各有一处「视前置层是否已落地而定」的插入位置，都写明了两种情况下各自怎么做。

**名称与签名的一致性**

- `access_log.record(session, bot_id, actor_id, action, target="", *, kind=None)`：Task 1 定义；Task 2（3C 的调用点，五个位置参数）、Task 3（`record(session, bot_id, None, "cli.rewrap")`）、Task 7、Task 8 使用。`record_opened(session, rows, actor_id, action) -> int`：Task 1 定义，Task 3 用三处。`bots_of`：Task 1 定义，Task 3 用。`list_` / `last_foreign` / `cleanup` / `MAX_LIST`：Task 1 定义，Task 4 用。
- `ACTIONS` 的十四个代码：Task 1 一次定全；Task 3 用 `chatlogs.read`、`tasks.read`、`cli.chatlogs`、`cli.tasks`、`cli.rewrap`，Task 7 用 `import.preview`、`import.notes`，Task 8 用 `export`；Task 9 的文案键与用例里的 `ACTIONS` 列表与之一一对应。
- `archive`：`parse`、`build(pages, followups_json)`、`page_text(*, title, summary, pinned, body)`、`followups_text(items)`、`PageFile`、`FollowupItem`、`Parsed`、`ArchiveError`（Task 5）；Task 6、7、8 与测试公共件 `tests.fakes.archives.pack / special / encrypted` 按同样的名字使用。
- `transfer`：`plan(session, bot_id, parsed, *, overwrite, today, fingerprint)`、`apply(session, bot_id, planned, *, today)`、`preview`、`counts`（Task 6）；`export(session, bot_id, *, today)`（Task 8）；路由在 Task 7、8 按此调用。清单的键 `digest` / `create` / `overwrite` / `skip` / `followups.create` / `followups.skip` 在 Task 6 的 `preview`、Task 7 的用例、Task 10 的 `ImportPlan` 类型里一致。
- 跳过的原因代码：Task 5（`problem`）、Task 6（`_checked` 与待跟进的 `duplicate`）产生，Task 10 的 `importReasons` 十四个键与之对应；没有文案的代码前端原样显示。
- 路由：`me_assistant_transfer.router`、`ARCHIVE_ERRORS`、`TRANSFER_ERRORS`（Task 7）；Task 8 在同一个文件里加 `export_archive`、`CHUNK`、`_chunks`。`in_person` 与 `DEPENDENCIES`（Task 3）对新旧路由一并生效。
- 测试台账：`tests/api/test_my_assistant_access_log.py::READS` 在 Task 2 建立，Task 4（`/access-log`）、Task 8（`/export`）各加一项；`tests/api/test_my_assistant_guard.py::CASES` 在 Task 4、7、8 各加一条，`_call` 在 Task 7 多传 `files`；`tests/unit/test_assistant_access_ledger.py::DECRYPTING` 在 Task 3 就列全了 Task 6、8 才出现的 `transfer.plan` / `transfer.export` / `notes.dump`。
- 前端：`myAssistant.accessLog(before?)`（Task 9）、`importArchive(file, { dryRun, overwrite, expect? })`、`exportArchive()`（Task 10）；组件 `AssistantAccessLog`、`AssistantTransfer` 的测试钩子与各自的用例一致；两个被嵌入的组件在父组件的用例里都换成桩（`AssistantSettings.spec.ts`、`AssistantNotes.spec.ts` 的 `render`）。
