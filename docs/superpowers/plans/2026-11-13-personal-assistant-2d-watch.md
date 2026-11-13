# 个人助理 2D：盯消息 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 平台以所有者的身份轮询飞书，把别人发给他的消息分成四级，把要交回的条目加密写进 `assistant_inbox`；同时给「以所有者身份调飞书」加上按应用的限频识别、用量计数与退避。本层做完后收件箱里开始有条目，但还没有人取走（投递是 2E）；普通员工（`kind='standard'`）的行为不变。

**Architecture:** scheduler 的新循环 `_assistant_watch_loop` 每 5 秒调 `watch.tick`：登记缺行的助理，取到期的 `assistant_watch` 行，每位助理开一个自己的短事务（`FOR UPDATE SKIP LOCKED` 认领），过了门槛（记录启用且 `active`、记下了身份、冷启动已完成、在上班时段、运行时没暂停）才轮询。一轮只列一次会话，与上一轮保存的顺序（`head_enc`，密文）对比找出有新动静的会话，只读这些会话游标之后的历史，另读所有者标记的重点群与跟着的话题；消息在内存里由纯函数 `grade` 分级，入箱时摘要用所有者的数据密钥加密，`(bot_id, kind, message_id)` 唯一索引保证不重复。限频在 `feishu_personal/ratelimit.py`：`service._http` 认出 429 与错误码 99991400 后抛 `RateLimited(retry_after)`；助理的列会话与读历史调用各记一行用量流水，轮询循环按最近 60 秒的用量拉长间隔或整轮跳过，助理工具的调用只记不拦。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy async、Alembic、PostgreSQL 16、httpx；pytest（pytest-asyncio 自动模式、respx、替身）。

**Spec:** `docs/superpowers/specs/2026-11-11-personal-assistant.md`（「原则」第 2、4 条，「二、加密与存储」，「四、审批闸门 → 改稿与撤回」，「五、盯消息」全文，「前置验证」里列会话与限频的各行）；分层总览 `docs/superpowers/plans/2026-11-13-personal-assistant-phase2-3.md`（「分层」2D 一行，裁定 3、7、8，「层间接口 → 2D」，「每层都要守的约束」）；依赖的两层：`2026-11-13-personal-assistant-2a-foundation.md`、`2026-11-13-personal-assistant-2c-followups.md`。

## Global Constraints

- 集成分支 `claude/personal-assistant-phase2-3`。开工前确认 2A、2B、2C 已在这条分支上（迁移头是 `0096`）；本层的迁移写作 `0097_assistant_watch`、`down_revision = "0096"`。
- 层间接口的名字原样使用总览里的：`ratelimit.Budget`、`ratelimit.RateLimited(retry_after)`；表 `assistant_watch`（主键 `bot_id`；`cursor`、`head_enc`、`interval`、`next_poll_at`、`state`、`last_error`、`last_ok_at`）、`assistant_inbox`（`bot_id`、`level`、`chat_id`、`message_id`、`thread_id`、`summary_enc`、`status`、`created_at`，另加 `kind`、`hold_until`、`delivered_task_id`）；`watch.tick(factory, cipher, now)`、`watch.grade(item, settings, context) -> Level`、`watch.stop(session, bot_id)`；scheduler 循环 `_assistant_watch_loop`。`state` 的取值 `idle` / `polling` / `backoff` / `paused` / `stopped`；`kind` 的取值 `message` / `approval_todo` / `reminder`。要改名先改总览。
- 2A 的接口按其计划的确切签名用：`settings.get(session, bot_id) -> AssistantSettings`、`settings.identified(row) -> bool`、`settings.on_shift(settings, now) -> bool`、`settings.record_identity(...)`；`policy.Scope` 的 `platform: bool = False` 字段。2C 的接口：`followups.close_for_message(session, bot_id, message_id: str) -> list[AssistantFollowup]`、`followups.add(session, bot_id, *, category, title, ..., origin_message_id=None)`。
- 公开仓：代码、测试、文档、提交信息只用通用占位（`example.com`、`demo-bot`、`cli_example`、张三 / 李四），不出现任何公司名、内部域名、真实人名、本机绝对路径；提交前跑仓库约定的敏感词自查。
- 普通员工的行为不变：共用路径（`service._http`、`service.api_request`）上的改动对普通员工只多一次对已加载对象的属性判断（`is_assistant(scope.bot)`），不加查询、不加等待、不加锁。
- 数值照 spec 第五节：轮询间隔 60 秒；列会话取前 50 条、按最近活跃倒序；列会话与读历史各每分钟 1000 次（按应用）；攒批 5 分钟；压着 10 分钟；刷新令牌约 7 天不刷新即失效，本层保证每 24 小时至少刷新一次。
- 每位助理每轮一个短事务：刷新令牌的锁（`service.lock` 的事务级咨询锁与授权行的行锁）到事务提交才释放。一轮里调过飞书之后不回滚：刷新令牌一次性使用，换到的新令牌必须落库。
- 消息内容只在内存里分级。库里只有加密的摘要（`summary_enc`）、加密的会话顺序（`head_enc`）与定位信息；游标（`cursor`）里只有会话 id、话题 id、消息 id 与毫秒时间。日志只带代码、编号、计数与异常类型名，每位助理的处理开头调 `sealed.guard_logs`。
- 不把飞书的原始错误文本交给模型或写进库：`last_error` 只存代码（`sealed.code_only`），`RateLimited.payload()` 只有代码与秒数。
- 新的内容表有 `bot_id` 列，登记进 `sealed.COLUMNS`、`tests/unit/test_assistant_sealed_codec.py` 的模型清单、`tests/unit/test_assistant_sealed_ledger.py` 的台账、`tests/conftest.py` 的 `BUSINESS_TABLES`、`coreman/core/db/models/__init__.py`。
- 飞书接口的参数与返回字段以现有代码的用法（`toolsets/messages.py`、`approvals/checks.py`）和 spec 的实测结论为准；拿不准的列在下面「需真机确认」，实现对缺字段容错。
- 代码风格：`ruff` 行宽 100，`mypy --strict`；注释与文档字符串用中文，写清「为什么」。计划里的代码块若与格式化器的换行不同，以 `uv run ruff format <文件>` 的结果为准。
- 测试命令：`uv run pytest <路径> -q`。`tests/integration`、`tests/api` 下的用例要数据库：本机有 Docker 时直接跑（testcontainers 自动起 `postgres:16`）；没有 Docker 时先指定一个专用的测试库，例如 `export TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/coreman_test`（见 `docs/development.md`「验证」；不要指向生产库，用例会清空表）。
- 每个任务结束时另跑：`uv run ruff check .`、`uv run ruff format --check .`、`uv run mypy coreman`。
- 本层不做：把收件箱条目投递成轮次、每日轮次预算（2E）；撤回审批、「处理中」表情挂起提醒、到期待跟进、授权到期提醒（2E）；上下班命令、冷启动与 `cold_start_done_at` 的写入（2F）；重点群与忽略会话的设置入口（3C）；单个应用撑不住时分到下一个应用（裁定 8）。

## 设计选择

**1. 顺序对比不止看「排到上一轮队首之前」。** spec 的说法漏两种情况。其一，上一轮的队首自己又来了新消息，顺序不变。其二，排第二的会话来了消息、随后排第一的也来了，顺序同样不变。可以证明：顺序没变的会话里，藏着新消息的一定是开头连续的一段（排第 r 位的有而顺序没变，说明排它前面的都在它之后又有了消息）。所以做法是：`feed.split` 把列表切成「挪动过的前缀」与「相对顺序没变的后缀」，前缀全读；后缀从第一个开始逐个探，读到一个没有新消息的就停。空闲时每人每分钟 1 次列会话加 1 次读历史。

**2. 游标按会话存毫秒时间与同一毫秒的消息 id。** 读历史的 `start_time` 是秒，所以每次都会把游标所在那一秒的消息再读回来；`cursor.chats[<会话>]` 存 `t`（读到的最新一条的毫秒时间）与 `m`（时间等于 `t` 的消息 id），内存里按「晚于 `t`，或等于 `t` 且不在 `m` 里」判新。入箱另有唯一索引兜底。

**3. 第一轮只记基线。** `cursor.since` 是这位助理开始被盯的毫秒时间；第一轮只保存顺序、不读历史。之后第一次读到的会话从 `since` 起读，不把开通前的历史倒进收件箱。

**4. 会话是单聊还是群。** 列会话的返回项带 `chat_mode` 就用它；不带时对要读的会话调一次 `im.chat`（现有接口，`approvals/checks.py` 用同一个字段），结果记进游标的 `k`，每个会话只查一次。

**5. 所有者与助理自己的私聊不读。** `assistant_settings.chat_id` 那个会话里是助理的回复与所有者对助理说的话，读了会自己喂自己。它和「忽略的会话」一样不读历史，也不打断设计选择 1 的逐个探测。

**6. 「压着」对已入箱的条目同样生效。** 读到所有者自己在某个会话里发的消息时：记下时间（`cursor.chats[<会话>].o`），并把这个会话里还没交回的条目改成 `hold`、`hold_until` 推到他这次发言之后 10 分钟。之后 10 分钟内这个会话的新消息直接按 `hold` 入箱。不作废条目：他说了一句不等于处理完了，10 分钟后交回时由助理读会话判断。

**7. 攒批对齐到同一个时刻。** 攒批的条目 `hold_until` 取这位助理还没到点的攒批条目里最早的那个时刻，没有才取「现在 + 5 分钟」。这样一批条目同时到点，2E 一轮取走。

**8. 私聊里机器人发来的消息算「攒批」。** spec 的表没有这一格。飞书审批的待办通知就是审批应用发来的单聊消息，按攒批入箱正好对上表里的「新出现的飞书审批待办」；别的通知机器人嫌吵，由所有者设为忽略的会话。本层不另外轮询审批待办接口（见文末「没覆盖到的地方」）。`grade` 仍认 `approval_todo` 与 `approval_outcome` 两种条目，供 2E 复用同一张分级表。

**9. 话题。** 主线里读到带 `thread_id` 的消息就把这个话题记进 `cursor.chats[<会话>].th`（每个会话只跟最近 3 个）；读这个会话时顺带读这 3 个话题（话题容器不带时间范围，取最近 20 条在内存里按时间过滤）。重点群每轮直接读主线与跟着的话题（裁定 7），每位助理最多 5 个重点群。

**10. 一轮的上限。** 读历史最多 12 次、最多 30 秒（每次调用前看一眼，不用取消：取消正在进行的令牌刷新会把刷新令牌用掉却拿不到新的）。没读完的会话记进 `cursor.todo`，下一轮先读。逐个探测另有上限 10 个。

**11. 读到一半失败不回滚。** `PersonalError`（含限频）之后：这一轮已入箱的条目、已读完的会话的游标、刚刷新的令牌一起提交，顺序（`head_enc`）不更新，下一轮重新比；重读由游标与唯一索引去重。只有意外异常（数据库错误）才回滚，再另开事务把这一行往后推 15 分钟。

**12. 用量是流水，不是计数器。** 表 `feishu_rate_usage` 一次调用一行，统计最近 60 秒的 `sum(units)`。计数器要更新同一行，行锁会被调用方的事务（里面有对飞书与审批系统的网络调用）一直占着，跨人互相卡住；插入不冲突。代价：调用方的事务回滚时这一笔丢掉，用量略偏低，可接受。限频时写一笔 `units = 上限`、恰好在等待秒数之后滑出窗口的流水（`Budget.exhaust`），同一应用的其他助理看到额度已满就跳过这一轮。

**13. `RateLimited` 是 `PersonalError` 的子类。** 这样助理工具撞到限频时，MCP 入口照旧返回 `{"error": "rate_limited", "upstream_code": 99991400, "retry_after": N}`，没有上游文本。`ratelimit.py` 要引用 `PersonalError`、`service.py` 要引用 `ratelimit`，为避免成环，把 `PersonalError` 挪到新模块 `feishu_personal/errors.py`，`service` 里照旧导出同一个类。顺带的好处：刷新令牌的请求被限频时不再把授权当成过期清掉。

**14. 轮询用的 `Scope` 不带任务。** 轮询循环里没有任务。`watch._owner_scope` 照 `provisioning._grant_scope` 的做法给 `Scope.task` 传 `cast(Task, None)`，身份取自 `assistant_settings`，并照 2A `platform_turns.scope_for` 的做法先核对（所有者账号可用、通讯录身份与授权行都还是记下的那个），对不上就不调：`service._locked` 遇到身份不一致会清空令牌。调用链 `Call.__call__` → `service.api_request` → `_authorize` / `_row` / `_locked` / `_failure` 与 `service.keep_alive` 都不读 `scope.task`（全仓只有 `begin_selection`、`inflow`、`feishu_docs`、`cloud_proxy` 与几个路由读它，都不在这条链上）；集成用例全程走真的 `api_request`，有人读了就会当场抛错。

**15. 保活。** 不轮询的助理（下班、冷启动没做完、运行时暂停）只要记录是启用中的 `active`，每 24 小时强制刷新一次令牌（`service.keep_alive`，不调任何业务接口）。轮询成功也算一次（轮询期间访问令牌每 2 小时过期，自然会刷新）。

**16. 错开。** 每位助理的相位是 `bot_id.int % 间隔`，下一次轮询落在「现在之后、对齐到相位」的时刻（`watch.slot`）。间隔被拉长时相位按新间隔重算。

**17. 文件拆分。** `watch.py` 只留循环、门槛与一轮的编排，对外的三个名字都在这里（`grade` 从 `watch_grade` 转出）；分级（纯函数）在 `watch_grade.py`，读飞书与顺序对比在 `watch_feed.py`，收件箱的写入在 `watch_inbox.py`。

## 需真机确认

实现对下面每一项都容错；上线后在真实租户里确认，结论写回 spec「前置验证」。

| # | 事项 | 现在的假定 | 假定不成立时的表现 |
|---|---|---|---|
| 1 | 列会话一次同时返回单聊与群要带的 `types` 取值（spec 只实测了 `types=p2p`，且该参数不在官方文档里） | `watch_feed.CHAT_TYPES = "p2p,group"` | 飞书拒绝：`last_error = feishu_read_failed`，状态 `backoff`；只返回一类：另一类盯不到。改常量即可 |
| 2 | 列会话的返回项有没有 `chat_mode`、有没有最近活跃时间 | 有 `chat_mode` 就用；没有就调一次 `im.chat` 并缓存；不依赖时间字段 | 每个会话多一次 `im.chat` |
| 3 | 话题容器（`container_id_type=thread`）不接受 `start_time` | 不带时间范围，取最近 20 条 | 无 |
| 4 | 普通群里「回复到话题」是否让群靠前、是否出现在主线历史里（spec 前置验证未完成的第 1 项） | 两种都兼容：靠前就按顺序发现；出现在主线就按 `thread_id` 记话题；都不成立时只有重点群的话题盯得到 | 非重点群里对旧消息新开的话题盯不到 |
| 5 | 撤回的消息在读历史与 `im.mget` 里的样子 | 条目还在，带 `deleted: true`（`approvals/checks.py` 同样的假定） | 条目消失：撤回发现不了，条目照常交回，由助理读会话时发现 |
| 6 | 以用户身份读消息时 `mentions[].id` 是不是 `open_id`；@所有人不产生 `mentions` 条目 | 是；@所有人不算 @ 本人 | 群里 @ 本人被分成攒批而不是立即 |
| 7 | 限频应答：HTTP 429 或正文 `code = 99991400`；等待秒数在应答头 `x-ogw-ratelimit-reset` | 两种都认；头读不到按 60 秒，另认 `Retry-After` | 退避时间不准 |
| 8 | 消息的 `create_time` 是毫秒字符串；`sender.sender_type` 真人是 `user`、机器人是 `app` | 是；读不出时间的消息跳过 | 该条消息不入箱 |
| 9 | 审批应用的待办通知以单聊消息出现在用户身份的会话列表里 | 是（设计选择 8） | 审批待办盯不到，需另加轮询 |

## 已知限制

- 逐个探测最多 10 个：一分钟内有超过 10 个会话都来了消息且顺序完全没变时，后面的要等它们各自再有动静。
- 每个会话只跟最近 3 个话题；话题群里更早的话题有新回复盯不到。
- 同一话题里与上次读到的最后一条同一毫秒的回复会被跳过。
- 用量流水在调用方事务回滚时丢失，统计略偏低。
- 运行时暂停期间不轮询，恢复后从游标接着读；每个会话一轮最多读 100 条，积压多时分几轮读完。
- 没有记下身份的助理（迁移 `0093` 之前建的）不轮询，`last_error` 为 `identity_missing`，重新开通后恢复。

## File Structure

| 文件 | 职责 |
|---|---|
| `migrations/versions/0097_assistant_watch.py`（新） | 表 `assistant_watch`、`assistant_inbox`、`feishu_rate_usage` |
| `coreman/core/db/models/assistants.py`、`feishu_personal.py`、`__init__.py` | 模型 `AssistantWatch`、`AssistantInbox`、`FeishuRateUsage` 与枚举常量 |
| `coreman/core/assistants/sealed.py` | `COLUMNS` 登记 `head_enc`、`summary_enc` |
| `coreman/core/feishu_personal/errors.py`（新） | `PersonalError`（从 `service.py` 挪来） |
| `coreman/core/feishu_personal/ratelimit.py`（新） | `RateLimited`、`detect`、`category`、`Budget`、`stretch`、`purge` |
| `coreman/core/feishu_personal/service.py` | `_http` 认限频；`api_request` 记用量、给限频标类别；`_authorize(refresh=)`；`keep_alive` |
| `coreman/core/assistants/watch_grade.py`（新） | `Level`、`Item`、`Context`、`grade`、`release_at` |
| `coreman/core/assistants/watch_feed.py`（新） | 列会话、顺序对比、读历史与话题、消息解析、游标的读写 |
| `coreman/core/assistants/watch_inbox.py`（新） | 入箱、压着、作废、待查撤回的条目 |
| `coreman/core/assistants/watch.py`（新） | `tick`、认领、门槛、一轮的编排、错开、退避、保活、`stop` |
| `coreman/core/assistants/deprovisioning.py` | `step_drain` 开头调 `watch.stop` |
| `coreman/runtime/scheduler/service.py` | `_assistant_watch_loop` |
| `coreman/api/routers/assistants_admin.py` | 列表每条加 `watch` |
| `tests/integration/watch_helpers.py`（新） | 可轮询的助理、飞书读接口与令牌刷新的替身 |
| `tests/conftest.py`、`tests/unit/test_assistant_sealed_codec.py`、`tests/unit/test_assistant_sealed_ledger.py` | 清表顺序、登记清单、台账 |
| `tests/unit/test_feishu_ratelimit.py`、`tests/integration/test_feishu_ratelimit_budget.py`（新） | 限频识别、用量、计量 |
| `tests/unit/test_assistant_watch_grade.py`、`tests/unit/test_assistant_watch_feed.py`（新） | 分级表、顺序对比与解析 |
| `tests/integration/test_assistant_watch_schema.py`、`test_assistant_watch_inbox.py`、`test_assistant_watch_poll.py`、`test_assistant_watch_limits.py`、`test_assistant_watch_keepalive_stop.py`、`test_assistant_watch_scheduler.py`（新） | 表、收件箱、轮询、限频、保活与停用、循环 |
| `tests/api/test_assistant_admin.py` | 轮询健康状态 |
| `docs/feishu.md`、`docs/operations.md`、`CHANGELOG.md`、spec | 说明、排障、变更记录、spec 同步 |

---

### Task 1: 三张表、迁移 `0097` 与封存登记

**Files:**
- Create: `migrations/versions/0097_assistant_watch.py`
- Modify: `coreman/core/db/models/assistants.py`（顶部 `from sqlalchemy import (` 加 `Identity`；文件末尾追加）
- Modify: `coreman/core/db/models/feishu_personal.py`（顶部导入加 `BigInteger`、`Identity`；文件末尾追加）
- Modify: `coreman/core/db/models/__init__.py`（两处导入清单与 `__all__`）
- Modify: `coreman/core/assistants/sealed.py`（`from coreman.core.db.models import (` 清单；`COLUMNS`）
- Modify: `tests/conftest.py`（`BUSINESS_TABLES`）
- Modify: `tests/unit/test_assistant_sealed_codec.py`（导入与 `test_every_content_column_is_registered`）
- Test: `tests/integration/test_assistant_watch_schema.py`（新建）

**Interfaces:**
- Consumes: `coreman.core.db.base.Base`、`enum_check`；`sealed.Whole`；`tests.integration.assistant_helpers.seed_assistant` / `raw` / `SECRET`；fixture `assistant_master`（`tests/integration/conftest.py`）、`db_session`、`db_engine`（`tests/conftest.py`）。
- Produces（都从 `coreman.core.db.models` 导出）：
  - `WATCH_STATES = ("idle", "polling", "backoff", "paused", "stopped")`、`INBOX_KINDS = ("message", "approval_todo", "reminder")`、`INBOX_LEVELS = ("immediate", "batch", "hold")`、`INBOX_STATUSES = ("pending", "delivered", "void")`、`RATE_CATEGORIES = ("chats", "history")`。
  - `AssistantWatch`：`bot_id: uuid.UUID`（主键，外键 `bots.id`，随记录删除）、`cursor: dict[str, Any]`（默认 `{}`）、`head_enc: list[str] | None`、`interval: int`（默认 60，范围 60–3600）、`next_poll_at: datetime`（默认现在）、`state: str`（默认 `idle`）、`last_error: str | None`、`last_ok_at: datetime | None`、`failures: int`（默认 0）、`keepalive_at: datetime | None`、`created_at`、`updated_at`。
  - `AssistantInbox`：`id: int`（自增主键）、`bot_id: uuid.UUID`、`kind: str`（默认 `message`）、`level: str`、`chat_id: str | None`、`message_id: str | None`、`thread_id: str | None`、`message_at: datetime | None`、`summary_enc: dict[str, Any] | None`、`status: str`（默认 `pending`）、`void_code: str | None`、`hold_until: datetime | None`、`delivered_task_id: int | None`、`created_at`、`updated_at`。唯一索引 `assistant_inbox_message_uq`：`(bot_id, kind, message_id)`，只对 `message_id IS NOT NULL` 的行。
  - `FeishuRateUsage`：`id: int`、`app_id: str`、`category: str`、`units: int`（默认 1，必须为正）、`at: datetime`（默认现在）。
  - `head_enc`、`summary_enc` 经 ORM 写入时自动封存；密钥销毁后读出来分别是 `[]` 与 `None`。

比总览多出的列（随最后一个任务同步进 spec）：`assistant_watch.failures`（连续失败次数，算退避）、`keepalive_at`（上次确认令牌还活着的时刻）、`created_at`、`updated_at`；`assistant_inbox.message_at`（消息自己的时间）、`void_code`（作废原因的代码）、`updated_at`。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_watch_schema.py`（新建）：

```python
"""assistant_watch / assistant_inbox / feishu_rate_usage：默认值、约束、去重索引，内容列是密文。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import (
    INBOX_KINDS,
    INBOX_LEVELS,
    INBOX_STATUSES,
    RATE_CATEGORIES,
    WATCH_STATES,
    AssistantInbox,
    AssistantWatch,
    FeishuRateUsage,
)
from coreman.core.db.session import make_session_factory
from tests.conftest import BUSINESS_TABLES
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")


def test_tables_are_registered_for_truncation() -> None:
    for name in ("assistant_watch", "assistant_inbox"):
        assert BUSINESS_TABLES.index(name) < BUSINESS_TABLES.index("bots")
    assert "feishu_rate_usage" in BUSINESS_TABLES
    assert WATCH_STATES == ("idle", "polling", "backoff", "paused", "stopped")
    assert INBOX_KINDS == ("message", "approval_todo", "reminder")
    assert INBOX_LEVELS == ("immediate", "batch", "hold")
    assert INBOX_STATUSES == ("pending", "delivered", "void")
    assert RATE_CATEGORIES == ("chats", "history")


async def test_defaults_and_removal_with_the_record(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    db_session.add(AssistantWatch(bot_id=bot.id))
    db_session.add(AssistantInbox(bot_id=bot.id, level="batch"))
    await db_session.commit()
    watch = await db_session.get(AssistantWatch, bot.id, populate_existing=True)
    assert watch is not None
    assert watch.cursor == {} and watch.head_enc is None
    assert (watch.interval, watch.state, watch.failures) == (60, "idle", 0)
    assert watch.next_poll_at is not None and watch.created_at is not None
    assert watch.last_error is None and watch.last_ok_at is None and watch.keepalive_at is None
    item = await db_session.scalar(
        select(AssistantInbox).execution_options(populate_existing=True)
    )
    assert item is not None and item.id > 0
    assert (item.kind, item.status) == ("message", "pending")
    assert item.chat_id is None and item.message_id is None and item.thread_id is None
    assert item.summary_enc is None and item.hold_until is None
    assert item.delivered_task_id is None and item.void_code is None and item.message_at is None
    await db_session.execute(text("DELETE FROM bots WHERE id = :id"), {"id": bot.id})
    await db_session.commit()
    assert await raw(db_session, "SELECT count(*) FROM assistant_watch") == 0
    assert await raw(db_session, "SELECT count(*) FROM assistant_inbox") == 0


@pytest.mark.parametrize("bad", [{"state": "sleeping"}, {"interval": 59}, {"interval": 3601}])
async def test_watch_values_are_constrained(db_session: AsyncSession, bad: dict[str, Any]) -> None:
    bot, _ = await seed_assistant(db_session)
    db_session.add(AssistantWatch(bot_id=bot.id, **bad))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    "bad",
    [{"level": "ignore"}, {"level": "batch", "kind": "note"}, {"level": "batch", "status": "x"}],
)
async def test_inbox_values_are_constrained(db_session: AsyncSession, bad: dict[str, Any]) -> None:
    bot, _ = await seed_assistant(db_session)
    db_session.add(AssistantInbox(bot_id=bot.id, **bad))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_one_item_per_message_and_kind(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    other, _ = await seed_assistant(db_session)
    db_session.add_all(
        [
            AssistantInbox(bot_id=bot.id, level="batch", message_id="om_1"),
            # 别的类型、别的助理、没有消息编号的条目都不受这个索引限制。
            AssistantInbox(bot_id=bot.id, level="batch", kind="reminder", message_id="om_1"),
            AssistantInbox(bot_id=other.id, level="batch", message_id="om_1"),
            AssistantInbox(bot_id=bot.id, level="batch", kind="reminder"),
            AssistantInbox(bot_id=bot.id, level="batch", kind="reminder"),
        ]
    )
    await db_session.commit()
    db_session.add(AssistantInbox(bot_id=bot.id, level="immediate", message_id="om_1"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_head_and_summary_are_sealed_and_read_back(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    db_session.add(AssistantWatch(bot_id=bot.id, head_enc=[f"oc_{SECRET}"]))
    db_session.add(
        AssistantInbox(
            bot_id=bot.id,
            level="immediate",
            chat_id="oc_li",
            message_id="om_1",
            summary_enc={"text": SECRET},
        )
    )
    await db_session.commit()
    head = str(await raw(db_session, "SELECT head_enc::text FROM assistant_watch"))
    summary = str(await raw(db_session, "SELECT summary_enc::text FROM assistant_inbox"))
    assert SECRET not in head and SECRET not in summary
    assert "enc:v2:d1:" in head and "enc:v2:d1:" in summary
    async with make_session_factory(db_engine)() as other:
        watch = await other.get(AssistantWatch, bot.id)
        item = await other.scalar(select(AssistantInbox))
        assert watch is not None and watch.head_enc == [f"oc_{SECRET}"]
        assert item is not None and item.summary_enc == {"text": SECRET}
        # 密钥销毁之后：行还在、定位信息可读，内容按空值处理，不抛错。
        await keys.destroy_key(other, owner.id)
        await other.commit()
        locked = await other.get(AssistantWatch, bot.id, populate_existing=True)
        gone = await other.scalar(select(AssistantInbox).execution_options(populate_existing=True))
        assert locked is not None and sealed.is_locked(locked) and locked.head_enc == []
        assert gone is not None and gone.summary_enc is None and gone.message_id == "om_1"


async def test_rate_usage_defaults_and_constraints(db_session: AsyncSession) -> None:
    db_session.add(FeishuRateUsage(app_id="cli_example", category="chats"))
    await db_session.commit()
    row = await db_session.scalar(select(FeishuRateUsage))
    assert row is not None and row.units == 1 and row.at is not None
    for bad in ({"category": "search"}, {"category": "history", "units": 0}):
        db_session.add(FeishuRateUsage(app_id="cli_example", **bad))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_watch_schema.py -q`
Expected: FAIL，收集阶段 `ImportError: cannot import name 'INBOX_KINDS' from 'coreman.core.db.models'`。

- [ ] **Step 3: 写模型**

`coreman/core/db/models/assistants.py`：顶部 `from sqlalchemy import (` 的清单里在 `ForeignKey,` 之后加一行 `Identity,`。文件末尾追加：

```python
WATCH_STATES = ("idle", "polling", "backoff", "paused", "stopped")
INBOX_KINDS = ("message", "approval_todo", "reminder")
INBOX_LEVELS = ("immediate", "batch", "hold")
INBOX_STATUSES = ("pending", "delivered", "void")


class AssistantWatch(Base):
    """一位助理的盯消息状态：读到哪了、上一轮的会话顺序、下次什么时候轮询、健康状况。

    `cursor` 是明文的定位信息，只有会话 id、话题 id、消息 id 与毫秒时间：
    `{"since": <开始被盯的时刻>, "chats": {<会话>: {"t": <读到的最新一条>, "m": [<时间等于 t 的
    消息>], "k": "p2p" | "group", "o": <所有者上次在这里发言>, "th": {<话题>: <读到的最新一条>}}},
    "todo": [<上一轮没读完的会话>]}`。
    `head_enc` 登记在 `sealed.COLUMNS`：库里是密文，经会话取出的行上是上一轮列会话得到的会话 id
    列表（按最近活跃倒序）。
    `state`：`polling` 在轮询；`idle` 没在轮询但没有问题（下班、冷启动没做完）；`paused` 运行时
    暂停；`backoff` 出错或限频后在退避；`stopped` 已停用。`last_error` 只存代码。
    `keepalive_at` 是上次确认令牌还活着的时刻（轮询成功或保活刷新），不轮询时每 24 小时刷新一次。
    """

    __tablename__ = "assistant_watch"
    __table_args__ = (
        CheckConstraint(enum_check("state", WATCH_STATES), name="state"),
        CheckConstraint('"interval" BETWEEN 60 AND 3600', name="interval"),
        Index("assistant_watch_due_idx", "next_poll_at"),
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
    )
    cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    head_enc: Mapped[list[str] | None] = mapped_column(JSONB)
    interval: Mapped[int] = mapped_column(Integer, server_default=text("60"))
    next_poll_at: Mapped[datetime] = _ts(server_default=text("now()"))
    state: Mapped[str] = mapped_column(Text, server_default=text("'idle'"))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_ok_at: Mapped[datetime | None] = _ts()
    failures: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    keepalive_at: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
    updated_at: Mapped[datetime] = _ts(server_default=text("now()"))


class AssistantInbox(Base):
    """要交回给助理的一件事。消息内容不落库：这里只有定位信息与加密的摘要。

    `summary_enc` 登记在 `sealed.COLUMNS`。`level` 是入箱时的分级；`hold_until` 为空表示可以
    立刻交回，否则攒批或压着到那个时刻。`status`：`pending` 待交回，`delivered` 已交回，`void`
    作废（`void_code`：`recalled` 对方撤回、`stopped` 助理停用）。`delivered_task_id` 由投递层
    （2E）写入：交回它的那一轮；`pending` 且非空表示正在交回，盯消息不再改这一行。
    """

    __tablename__ = "assistant_inbox"
    __table_args__ = (
        CheckConstraint(enum_check("kind", INBOX_KINDS), name="kind"),
        CheckConstraint(enum_check("level", INBOX_LEVELS), name="level"),
        CheckConstraint(enum_check("status", INBOX_STATUSES), name="status"),
        # 同一条消息只入箱一次：轮询重跑、两个 scheduler 交接时重叠都靠它兜住。
        Index(
            "assistant_inbox_message_uq",
            "bot_id",
            "kind",
            "message_id",
            unique=True,
            postgresql_where=text("message_id IS NOT NULL"),
        ),
        Index("assistant_inbox_due_idx", "bot_id", "status", "hold_until"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text, server_default=text("'message'"))
    level: Mapped[str] = mapped_column(Text)
    chat_id: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(Text)
    thread_id: Mapped[str | None] = mapped_column(Text)
    message_at: Mapped[datetime | None] = _ts()
    summary_enc: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    void_code: Mapped[str | None] = mapped_column(Text)
    hold_until: Mapped[datetime | None] = _ts()
    # 不设外键：任务行按保留期清理，条目比它留得久。
    delivered_task_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
    updated_at: Mapped[datetime] = _ts(server_default=text("now()"))
```

`coreman/core/db/models/feishu_personal.py`：顶部 `from sqlalchemy import (` 的清单开头加 `BigInteger,`，在 `ForeignKey,` 之后加 `Identity,`。文件末尾追加：

```python
RATE_CATEGORIES = ("chats", "history")


class FeishuRateUsage(Base):
    """以所有者身份调飞书的用量流水：一行一次调用，或一笔限频后占满额度的退避。

    飞书按「应用 × 企业 × 接口」限频，全员共用，所以按应用、按接口类别统计最近 60 秒的
    `sum(units)`。只插入不更新：计数器的行锁会被调用方的事务一直占着。旧行由盯消息循环清理。
    """

    __tablename__ = "feishu_rate_usage"
    __table_args__ = (
        CheckConstraint(enum_check("category", RATE_CATEGORIES), name="category"),
        CheckConstraint("units > 0", name="units"),
        Index("feishu_rate_usage_window_idx", "app_id", "category", "at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    app_id: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    units: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
```

`coreman/core/db/models/__init__.py`：`from coreman.core.db.models.assistants import (` 的清单里加 `INBOX_KINDS,`、`INBOX_LEVELS,`、`INBOX_STATUSES,`、`WATCH_STATES,`、`AssistantInbox,`、`AssistantWatch,`（按现有的排序放）；把

```python
from coreman.core.db.models.feishu_personal import FeishuPersonalGrant, FeishuPersonalSend
```

换成

```python
from coreman.core.db.models.feishu_personal import (
    RATE_CATEGORIES,
    FeishuPersonalGrant,
    FeishuPersonalSend,
    FeishuRateUsage,
)
```

并在 `__all__` 末尾追加 `"AssistantInbox"`、`"AssistantWatch"`、`"FeishuRateUsage"`、`"INBOX_KINDS"`、`"INBOX_LEVELS"`、`"INBOX_STATUSES"`、`"RATE_CATEGORIES"`、`"WATCH_STATES"`。

- [ ] **Step 4: 写迁移**

`migrations/versions/0097_assistant_watch.py`（新建）：

```python
"""Personal assistant watch: polling state, graded inbox, per-app Feishu rate usage."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def _now(name: str) -> sa.Column:
    return _ts(name, server_default=sa.text("now()"), nullable=False)


def _jsonb(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), **kw)


def upgrade() -> None:
    op.create_table(
        "assistant_watch",
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
        ),
        _jsonb("cursor", server_default=sa.text("'{}'::jsonb"), nullable=False),
        _jsonb("head_enc", nullable=True),
        sa.Column("interval", sa.Integer(), server_default=sa.text("60"), nullable=False),
        _now("next_poll_at"),
        sa.Column("state", sa.Text(), server_default=sa.text("'idle'"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        _ts("last_ok_at", nullable=True),
        sa.Column("failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("keepalive_at", nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint(
            "state IN ('idle', 'polling', 'backoff', 'paused', 'stopped')", name="state"
        ),
        sa.CheckConstraint('"interval" BETWEEN 60 AND 3600', name="interval"),
    )
    op.create_index("assistant_watch_due_idx", "assistant_watch", ["next_poll_at"])
    op.create_table(
        "assistant_inbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.Text(), server_default=sa.text("'message'"), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=True),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("thread_id", sa.Text(), nullable=True),
        _ts("message_at", nullable=True),
        _jsonb("summary_enc", nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("void_code", sa.Text(), nullable=True),
        _ts("hold_until", nullable=True),
        sa.Column("delivered_task_id", sa.BigInteger(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint("kind IN ('message', 'approval_todo', 'reminder')", name="kind"),
        sa.CheckConstraint("level IN ('immediate', 'batch', 'hold')", name="level"),
        sa.CheckConstraint("status IN ('pending', 'delivered', 'void')", name="status"),
    )
    op.create_index(
        "assistant_inbox_message_uq",
        "assistant_inbox",
        ["bot_id", "kind", "message_id"],
        unique=True,
        postgresql_where=sa.text("message_id IS NOT NULL"),
    )
    op.create_index(
        "assistant_inbox_due_idx", "assistant_inbox", ["bot_id", "status", "hold_until"]
    )
    op.create_table(
        "feishu_rate_usage",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("app_id", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("units", sa.Integer(), server_default=sa.text("1"), nullable=False),
        _now("at"),
        sa.CheckConstraint("category IN ('chats', 'history')", name="category"),
        sa.CheckConstraint("units > 0", name="units"),
    )
    op.create_index(
        "feishu_rate_usage_window_idx", "feishu_rate_usage", ["app_id", "category", "at"]
    )


def downgrade() -> None:
    op.drop_table("feishu_rate_usage")
    op.drop_table("assistant_inbox")
    op.drop_table("assistant_watch")
```

- [ ] **Step 5: 登记封存、清表顺序与模型清单**

`coreman/core/assistants/sealed.py`：`from coreman.core.db.models import (` 的清单里加 `AssistantInbox,`、`AssistantWatch,`（按字母序）；`COLUMNS` 末尾（最后一个 `}` 之前）加：

```python
    # 盯消息：上一轮的会话顺序（会话 id 的列表）与交回条目的摘要。
    AssistantWatch: {"head_enc": Whole([])},
    AssistantInbox: {"summary_enc": Whole()},
```

`tests/conftest.py` 的 `BUSINESS_TABLES`：在开头 `"assistant_settings",` 之前加三行：

```python
    "assistant_inbox",
    "assistant_watch",
    "feishu_rate_usage",
```

`tests/unit/test_assistant_sealed_codec.py`：导入清单加 `AssistantInbox,`、`AssistantWatch,`；`test_every_content_column_is_registered` 里 `assert set(sealed.COLUMNS) == {` 的集合加 `AssistantInbox,`、`AssistantWatch,`（前面各层加的模型保留）；在 `assert set(sealed.COLUMNS[AssistantTranscript]) == {"blob_enc"}` 之后加：

```python
    assert set(sealed.COLUMNS[AssistantWatch]) == {"head_enc"}
    assert set(sealed.COLUMNS[AssistantInbox]) == {"summary_enc"}
```

- [ ] **Step 6: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_watch_schema.py tests/unit/test_assistant_sealed_codec.py tests/unit/test_assistant_sealed_ledger.py tests/integration/test_migrations.py -q`
Expected: PASS。

Run: `uv run alembic check`
Expected: `No new upgrade operations detected.`

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 7: 提交**

```bash
git add migrations/versions/0097_assistant_watch.py coreman/core/db/models/assistants.py \
  coreman/core/db/models/feishu_personal.py coreman/core/db/models/__init__.py \
  coreman/core/assistants/sealed.py tests/conftest.py \
  tests/unit/test_assistant_sealed_codec.py tests/integration/test_assistant_watch_schema.py
git commit -m "盯消息的三张表：assistant_watch、assistant_inbox、feishu_rate_usage 与迁移 0097"
```

### Task 2: 限频识别、用量流水与保活入口

**Files:**
- Create: `coreman/core/feishu_personal/errors.py`
- Create: `coreman/core/feishu_personal/ratelimit.py`
- Modify: `coreman/core/feishu_personal/service.py:24`（导入）、`:40-50`（`PersonalError` 挪走）、`:98-117`（`_http`）、`:504-506`、`:519`（`_authorize`）、`:569-578`（`api_request`），文件末尾追加 `keep_alive`
- Create: `tests/integration/watch_helpers.py`
- Test: `tests/unit/test_feishu_ratelimit.py`（新建）、`tests/integration/test_feishu_ratelimit_budget.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `FeishuRateUsage`；`endpoints.Endpoint`（`method`、`path`）；2A 的 `settings.record_identity`；`tests.integration.lifecycle_helpers.pool_relay`、`CIPHER`；`coreman.core.assistants.apps.SECRET_AAD`；`coreman.core.bots.secrets.CREDENTIALS_AAD` / `encrypt_json`。
- Produces（`coreman/core/feishu_personal/errors.py`）：`class PersonalError(Exception)`，与原来在 `service.py` 的定义逐字相同；`service.PersonalError` 仍是同一个类。
- Produces（`coreman/core/feishu_personal/ratelimit.py`）：
  - `RATE_LIMIT_CODE = 99991400`、`CHATS = "chats"`、`HISTORY = "history"`、`LIMITS: dict[str, int]`（各 1000）、`WINDOW = timedelta(seconds=60)`、`DEFAULT_RETRY = 60`、`MAX_RETRY = 600`、`TIGHT = 0.6`。
  - `class RateLimited(PersonalError)`：`RateLimited(retry_after: int, category: str | None = None)`；属性 `retry_after`（夹在 1–600）、`category`；`code == "rate_limited"`；`payload()` 为 `{"error": "rate_limited", "upstream_code": 99991400, "retry_after": N}`。
  - `def retry_after(headers: Mapping[str, str]) -> int`
  - `def detect(status: int, data: Any, headers: Mapping[str, str]) -> RateLimited | None`
  - `def category(endpoint: endpoints.Endpoint) -> str | None`：`GET /im/v1/chats` 是 `chats`，`GET /im/v1/messages` 是 `history`，其余 `None`。
  - `@dataclass(frozen=True) class Budget`：`Budget(app_id: str)`；`async record(session, category: str, *, at: datetime | None = None, units: int = 1) -> None`；`async used(session, now: datetime) -> dict[str, int]`；`async load(session, now: datetime) -> float`（两类里用量占上限比例的较大者）；`async exhaust(session, category: str, now: datetime, retry_after: int) -> None`。都不提交。
  - `def stretch(load: float) -> int | None`：间隔倍数 1 / 2 / 4；`None` 表示额度已满。
  - `async def purge(session, now: datetime) -> int`：删掉 15 分钟前的流水。
- Produces（`service.py`）：
  - `_http` 遇到 HTTP 429 或正文 `code == 99991400` 抛 `RateLimited`；返回值与签名不变。
  - `api_request`：助理的列会话与读历史各记一笔用量；`_http` 抛出的 `RateLimited` 带上 `category`。
  - `_authorize(..., *, refresh: bool = False)`：`refresh=True` 时不看到期时间，直接刷新。
  - `async def keep_alive(session: AsyncSession, cipher: Cipher, scope: Scope) -> None`：只刷新令牌，不调业务接口。
- Produces（`tests/integration/watch_helpers.py`）：
  - 常量 `APP_ID = "cli_example"`、`APP_SECRET = "app-secret"`、`TENANT = "tenant-example"`、`READ_SCOPES`。
  - `@dataclass class Watched`：`bot: Bot`、`owner: User`、`open_id: str`、`chat_id: str`（他与助理的私聊）、`pid: str`、`token: str`。
  - `async def assistant(session, tag: str = "a", *, cold_started: bool = True, shift: str | None = "on", expired: bool = False) -> Watched`：一位可以轮询的助理（启用、`active`、绑在助理池的可用节点上、有数据密钥、授权有效、记下了身份）。`shift` 写进 `shift_override`。
  - `def scope_of(who: Watched) -> Scope`
  - `def msg(mid: str, at: datetime, *, chat: str, sender: str = "ou_li", kind: str = "user", text: str = ..., **extra) -> dict[str, Any]`
  - `@dataclass class Hit`：`path: str`、`params: dict[str, Any]`、`token: str`。
  - `class FakeFeishuApi`：属性 `chats: list[dict]`、`history: dict[str, list[dict]]`、`modes: dict[str, str]`、`hits: list[Hit]`、`refreshes: int`、`limited: dict[str, int]`（路径或网址 → 等待秒数）、`failing: set[str]`（读历史时要失败的会话）、`before`（每次业务调用前的回调）；方法 `http(...)`（替换 `service._http`）、`say(...)`、`paths()`、`read(container)`。
  - `def install(monkeypatch) -> FakeFeishuApi`

- [ ] **Step 1: 写失败测试（纯函数与 `_http`）**

`tests/unit/test_feishu_ratelimit.py`（新建）：

```python
"""限频：认出飞书的限频应答、只给代码与等待秒数；哪些接口计入分钟用量；用量多了拉长多少。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from coreman.core.feishu_personal import endpoints, ratelimit, service

SECRET = "机密-片段-7f3a"
URL = service.BASE + "/im/v1/chats"


def test_rate_limited_is_a_personal_error_with_a_code_and_seconds_only() -> None:
    exc = ratelimit.RateLimited(7)
    assert isinstance(exc, service.PersonalError) and exc.code == "rate_limited"
    assert exc.category is None
    assert exc.payload() == {"error": "rate_limited", "upstream_code": 99991400, "retry_after": 7}
    assert ratelimit.RateLimited(0).retry_after == 1
    assert ratelimit.RateLimited(99999).retry_after == 600


@pytest.mark.parametrize(
    ("headers", "seconds"),
    [
        ({"x-ogw-ratelimit-reset": "12"}, 12),
        ({"retry-after": "3"}, 3),
        ({"x-ogw-ratelimit-reset": "0", "retry-after": "5"}, 5),
        ({"x-ogw-ratelimit-reset": "soon"}, 60),
        ({"x-ogw-ratelimit-reset": "9000"}, 600),
        ({}, 60),
    ],
)
def test_wait_comes_from_the_response_headers(headers: dict[str, str], seconds: int) -> None:
    assert ratelimit.retry_after(headers) == seconds


def test_detect_knows_the_status_and_the_error_code() -> None:
    assert ratelimit.detect(429, None, {}) is not None
    found = ratelimit.detect(400, {"code": 99991400}, {"x-ogw-ratelimit-reset": "4"})
    assert found is not None and found.retry_after == 4
    assert ratelimit.detect(200, {"code": 0}, {}) is None
    assert ratelimit.detect(400, {"code": 230001}, {}) is None
    assert ratelimit.detect(502, "<html>", {}) is None


def test_only_listing_chats_and_reading_history_are_budgeted() -> None:
    known = endpoints.ENDPOINTS
    assert ratelimit.category(known["im.chats"]) == "chats"
    assert ratelimit.category(known["im.history"]) == "history"
    # 同一个路径的 POST 是发消息；按编号取消息、单个会话、搜索会话各有自己的限额。
    for key in ("im.send", "im.mget", "im.chat", "im.chat_search", "im.search"):
        assert ratelimit.category(known[key]) is None, key


@pytest.mark.parametrize(
    ("load", "factor"), [(0.0, 1), (0.59, 1), (0.6, 2), (0.79, 2), (0.8, 4), (0.99, 4), (1.0, None)]
)
def test_the_interval_stretches_as_the_budget_fills(load: float, factor: int | None) -> None:
    assert ratelimit.stretch(load) == factor


@respx.mock
async def test_http_raises_rate_limited_on_429_without_the_upstream_text() -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            429, headers={"x-ogw-ratelimit-reset": "9"}, text=f"too many requests {SECRET}"
        )
    )
    with pytest.raises(ratelimit.RateLimited) as caught:
        await service._http("GET", URL)
    assert caught.value.retry_after == 9
    assert SECRET not in str(caught.value)
    assert SECRET not in json.dumps(caught.value.payload(), ensure_ascii=False)


@respx.mock
async def test_http_raises_rate_limited_on_the_error_code() -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(400, json={"code": 99991400, "msg": f"frequency limit {SECRET}"})
    )
    with pytest.raises(ratelimit.RateLimited) as caught:
        await service._http("GET", URL)
    assert caught.value.retry_after == 60


@respx.mock
async def test_http_is_unchanged_for_everything_else() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"code": 0, "data": {"items": []}}))
    assert await service._http("GET", URL) == {"code": 0, "data": {"items": []}}
    respx.get(URL).mock(return_value=httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(service.PersonalError) as caught:
        await service._http("GET", URL)
    assert caught.value.code == "upstream_unavailable"
    assert not isinstance(caught.value, ratelimit.RateLimited)


async def test_only_assistant_calls_are_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """普通员工的调用不记用量（它们各有自己的应用，这条路径上也不该多一条语句）。"""
    recorded: list[tuple[str, str]] = []

    async def authorize(_s: Any, _c: Any, _scope: Any, method: str, path: str, **_kw: Any) -> Any:
        row = SimpleNamespace(app_id="cli_example")
        return endpoints.match(method, path), row, "secret", {"access_token": "t"}

    async def http(_method: str, _url: str, **_kw: Any) -> dict[str, Any]:
        return {"code": 0, "data": {}}

    async def record(self: ratelimit.Budget, _session: Any, category: str, **_kw: Any) -> None:
        recorded.append((self.app_id, category))

    monkeypatch.setattr(service, "_authorize", authorize)
    monkeypatch.setattr(service, "_http", http)
    monkeypatch.setattr(ratelimit.Budget, "record", record)
    for kind in ("standard", "assistant"):
        scope: Any = SimpleNamespace(bot=SimpleNamespace(kind=kind))
        for path in ("/im/v1/chats", "/im/v1/messages", "/im/v1/messages/mget"):
            await service.api_request(None, None, scope, "GET", path)  # type: ignore[arg-type]
    assert recorded == [("cli_example", "chats"), ("cli_example", "history")]
```

- [ ] **Step 2: 写测试公共件**

`tests/integration/watch_helpers.py`（新建）：

```python
"""盯消息用例的公共件：一位可以轮询的助理，以及飞书读接口与令牌刷新的替身。

替身替换的是 `service._http`：授权、加锁、刷新、限频识别之后的路径都走真的代码。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys, sealed, settings
from coreman.core.assistants.apps import SECRET_AAD
from coreman.core.assistants.records import new_assistant_bot
from coreman.core.bots.secrets import CREDENTIALS_AAD, encrypt_json
from coreman.core.db.models import Bot, FeishuPersonalGrant, PlatformApp, Task, User, UserIdentity
from coreman.core.feishu_personal import ratelimit
from coreman.core.feishu_personal import service as personal
from coreman.core.feishu_personal.policy import Scope
from coreman.core.timeutils import utcnow
from tests.integration.lifecycle_helpers import CIPHER, pool_relay

APP_ID, APP_SECRET, TENANT = "cli_example", "app-secret", "tenant-example"
READ_SCOPES = ["offline_access", "im:chat:read", "im:message:readonly"]


@dataclass
class Watched:
    bot: Bot
    owner: User
    open_id: str
    chat_id: str
    pid: str
    token: str


async def assistant(
    session: AsyncSession,
    tag: str = "a",
    *,
    cold_started: bool = True,
    shift: str | None = "on",
    expired: bool = False,
) -> Watched:
    """一位可以轮询的助理。`shift="on"` 让它不看钟点一直在上班；`expired` 让访问令牌已过期。"""
    owner = User(login_name=f"watch-{tag}", display_name="张三", source="sync")
    session.add(owner)
    await session.flush()
    pid, open_id, chat_id = f"pid_{tag}", f"ou_owner_{tag}", f"oc_assistant_{tag}"
    session.add(
        UserIdentity(user_id=owner.id, platform="feishu", platform_user_id=pid, open_id=open_id)
    )
    # 所有助理共用一个应用：限频按应用计。
    app = await session.scalar(select(PlatformApp).where(PlatformApp.app_id == APP_ID))
    if app is None:
        app = PlatformApp(
            platform="feishu",
            name="个人助理-watch",
            capabilities=["assistant"],
            app_id=APP_ID,
            secret_enc=CIPHER.encrypt(APP_SECRET, SECRET_AAD),
        )
        session.add(app)
        await session.flush()
    relay = await pool_relay(session)
    bot = new_assistant_bot(
        owner,
        app,
        relay_server_id=relay.id,
        model="m",
        working_dir=f"/srv/pa/{tag}",
        credentials_enc=encrypt_json(
            CIPHER, {"app_id": APP_ID, "app_secret": APP_SECRET}, CREDENTIALS_AAD
        ),
    )
    bot.assistant_state = "active"
    session.add(bot)
    await session.flush()
    await keys.ensure_key(session, sealed.master(), owner.id)
    token = f"u-access-{tag}"
    grant = FeishuPersonalGrant(
        bot_id=bot.id,
        user_id=owner.id,
        app_id=APP_ID,
        app_fingerprint=personal._fingerprint(APP_ID, APP_SECRET),
        platform_user_id=pid,
        open_id=open_id,
        tenant_key=TENANT,
        status="connected",
        authorization_level="all",
        scopes=list(READ_SCOPES),
        requested_scopes=list(READ_SCOPES),
        expires_at=utcnow() + timedelta(hours=-1 if expired else 1),
    )
    grant.token_enc = CIPHER.encrypt(
        json.dumps({"access_token": token, "refresh_token": f"u-refresh-{tag}"}),
        personal._aad(grant, "token_enc"),
    )
    session.add(grant)
    prefs = await settings.record_identity(
        session, bot.id, open_id=open_id, tenant_key=TENANT, platform_user_id=pid, chat_id=chat_id
    )
    prefs.shift_override = shift
    prefs.cold_start_done_at = utcnow() if cold_started else None
    await session.commit()
    sealed.forget()
    return Watched(bot, owner, open_id, chat_id, pid, token)


def scope_of(who: Watched) -> Scope:
    """不带任务的 scope，与盯消息循环里拼的一样。"""
    return Scope(
        cast(Task, None), who.bot, who.owner.id, TENANT, APP_ID, who.open_id, who.pid, who.chat_id
    )


async def tokens(session: AsyncSession, who: Watched) -> dict[str, Any]:
    """库里这位所有者现在的令牌。"""
    grant = await session.get(
        FeishuPersonalGrant, (who.bot.id, who.owner.id), populate_existing=True
    )
    assert grant is not None and grant.token_enc
    found: dict[str, Any] = json.loads(
        CIPHER.decrypt(grant.token_enc, personal._aad(grant, "token_enc"))
    )
    return found


def msg(
    mid: str,
    at: datetime,
    *,
    chat: str,
    sender: str = "ou_li",
    kind: str = "user",
    text: str = "周五能给报价吗？",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "message_id": mid,
        "chat_id": chat,
        "msg_type": "text",
        "create_time": str(int(at.timestamp() * 1000)),
        "sender": {"id": sender, "id_type": "open_id", "sender_type": kind},
        "body": {"content": json.dumps({"text": text}, ensure_ascii=False)},
        **extra,
    }


@dataclass
class Hit:
    path: str
    params: dict[str, Any]
    token: str


@dataclass
class FakeFeishuApi:
    """飞书的读接口与令牌刷新。`chats` 按最近活跃倒序；`history` 按会话存消息（旧的在前）。

    带 `"_thread_only": True` 的消息只在话题容器里读得到，主线里没有。
    """

    chats: list[dict[str, Any]] = field(default_factory=list)
    history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    modes: dict[str, str] = field(default_factory=dict)
    hits: list[Hit] = field(default_factory=list)
    refreshes: int = 0
    limited: dict[str, int] = field(default_factory=dict)
    failing: set[str] = field(default_factory=set)
    before: Callable[[Hit], Awaitable[None]] | None = None

    def say(self, chat: str, mid: str, at: datetime, **kw: Any) -> dict[str, Any]:
        item = msg(mid, at, chat=chat, **kw)
        self.history.setdefault(chat, []).append(item)
        return item

    def paths(self) -> list[str]:
        return [hit.path for hit in self.hits]

    def read(self, container: str) -> list[Hit]:
        """读过这个会话（或话题）历史的调用。"""
        return [
            hit
            for hit in self.hits
            if hit.path == "/im/v1/messages" and hit.params.get("container_id") == container
        ]

    def _wait(self, key: str) -> None:
        if key in self.limited:
            raise ratelimit.RateLimited(self.limited[key])

    async def http(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        if url == personal.TOKEN_URL:
            self._wait(url)
            self.refreshes += 1
            return {
                "access_token": f"u-access-r{self.refreshes}",
                "refresh_token": f"u-refresh-r{self.refreshes}",
                "expires_in": 7200,
                "scope": " ".join(READ_SCOPES),
            }
        path = url.removeprefix(personal.BASE)
        params = dict(kwargs.get("params") or {})
        bearer = str((kwargs.get("headers") or {}).get("Authorization", ""))
        hit = Hit(path, params, bearer.removeprefix("Bearer "))
        self.hits.append(hit)
        if self.before is not None:
            await self.before(hit)
        self._wait(path)
        everything = [item for items in self.history.values() for item in items]
        if path == "/im/v1/chats":
            return {"code": 0, "data": {"items": [dict(chat) for chat in self.chats]}}
        if path == "/im/v1/messages/mget":
            wanted = params["message_ids"]
            return {
                "code": 0,
                "data": {"items": [m for m in everything if m["message_id"] in wanted]},
            }
        if path == "/im/v1/messages":
            container = params["container_id"]
            if container in self.failing:
                return {"code": 230001, "msg": "upstream text that must not travel"}
            if params["container_id_type"] == "thread":
                assert "start_time" not in params
                found = [m for m in everything if m.get("thread_id") == container]
                found.sort(key=lambda m: int(m["create_time"]), reverse=True)
            else:
                floor = int(params.get("start_time", "0")) * 1000
                found = [
                    m
                    for m in self.history.get(container, [])
                    if int(m["create_time"]) >= floor and not m.get("_thread_only")
                ]
            return {"code": 0, "data": {"items": found, "has_more": False}}
        if path.startswith("/im/v1/chats/"):
            chat_id = path.rsplit("/", 1)[1]
            return {"code": 0, "data": {"chat_mode": self.modes.get(chat_id, "group"), "name": ""}}
        raise AssertionError((method, path))


def install(monkeypatch: pytest.MonkeyPatch) -> FakeFeishuApi:
    api = FakeFeishuApi()
    monkeypatch.setattr(personal, "_http", api.http)
    return api
```

- [ ] **Step 3: 写失败测试（用量与计量）**

`tests/integration/test_feishu_ratelimit_budget.py`（新建）：

```python
"""按应用的分钟用量：滑动 60 秒、限频后占满到等待结束；助理的调用只记不拦；限频不丢令牌。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import FeishuPersonalGrant, FeishuRateUsage
from coreman.core.feishu_personal import ratelimit
from coreman.core.feishu_personal import service as personal
from coreman.core.feishu_personal.toolbase import Call
from coreman.core.timeutils import utcnow
from tests.integration.lifecycle_helpers import CIPHER
from tests.integration.watch_helpers import APP_ID, assistant, install, scope_of, tokens

pytestmark = pytest.mark.usefixtures("assistant_master")


async def test_usage_is_per_app_and_category_over_a_sliding_minute(
    db_session: AsyncSession,
) -> None:
    now = utcnow()
    mine, theirs = ratelimit.Budget("cli_example"), ratelimit.Budget("cli_other")
    await mine.record(db_session, "chats", at=now - timedelta(seconds=10))
    await mine.record(db_session, "history", at=now - timedelta(seconds=10), units=3)
    await mine.record(db_session, "history", at=now - timedelta(seconds=61))
    await theirs.record(db_session, "history", at=now, units=900)
    assert await mine.used(db_session, now) == {"chats": 1, "history": 3}
    assert await mine.load(db_session, now) == pytest.approx(0.003)
    assert await theirs.load(db_session, now) == pytest.approx(0.9)
    # 不带时间时用数据库的现在。
    await mine.record(db_session, "chats")
    assert (await mine.used(db_session, utcnow()))["chats"] == 2


async def test_exhaust_fills_the_budget_until_the_wait_is_over(db_session: AsyncSession) -> None:
    now = utcnow()
    budget = ratelimit.Budget("cli_example")
    await budget.exhaust(db_session, "history", now, 90)
    assert await budget.load(db_session, now) >= 1.0
    assert await budget.load(db_session, now + timedelta(seconds=89)) >= 1.0
    assert await budget.load(db_session, now + timedelta(seconds=91)) == 0.0
    assert ratelimit.stretch(await budget.load(db_session, now)) is None


async def test_purge_drops_rows_older_than_the_retention(db_session: AsyncSession) -> None:
    now = utcnow()
    budget = ratelimit.Budget("cli_example")
    await budget.record(db_session, "chats", at=now - timedelta(minutes=16))
    await budget.record(db_session, "chats", at=now - timedelta(minutes=1))
    assert await ratelimit.purge(db_session, now) == 1
    assert await db_session.scalar(select(func.count()).select_from(FeishuRateUsage)) == 1


async def test_assistant_reads_are_counted_but_never_blocked(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    budget = ratelimit.Budget(APP_ID)
    await budget.record(db_session, "history", units=5000)  # 早已超额：工具照样能调
    call = Call(db_session, CIPHER, scope_of(who))
    await call("im.chats", params={"page_size": 20})
    await call("im.history", params={"container_id_type": "chat", "container_id": "oc_li"})
    await call("im.mget", params={"message_ids": ["om_1"]})
    await db_session.commit()
    assert api.paths() == ["/im/v1/chats", "/im/v1/messages", "/im/v1/messages/mget"]
    assert await budget.used(db_session, utcnow()) == {"chats": 1, "history": 5001}


async def test_a_rate_limit_names_its_category_and_keeps_the_refreshed_token(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session, expired=True)
    api.limited["/im/v1/messages"] = 7
    call = Call(db_session, CIPHER, scope_of(who))
    with pytest.raises(ratelimit.RateLimited) as caught:
        await call("im.history", params={"container_id_type": "chat", "container_id": "oc_li"})
    assert (caught.value.category, caught.value.retry_after) == ("history", 7)
    # 调用方照 MCP 入口的做法提交：这次调用里换到的新令牌（旧的刷新令牌已经用掉）必须落库。
    await db_session.commit()
    assert api.refreshes == 1
    assert (await tokens(db_session, who))["refresh_token"] == "u-refresh-r1"


async def test_a_rate_limited_refresh_does_not_expire_the_grant(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session, expired=True)
    api.limited[personal.TOKEN_URL] = 5
    with pytest.raises(ratelimit.RateLimited) as caught:
        await Call(db_session, CIPHER, scope_of(who))("im.chats", params={})
    assert caught.value.category is None
    await db_session.commit()
    grant = await db_session.get(
        FeishuPersonalGrant, (who.bot.id, who.owner.id), populate_existing=True
    )
    assert grant is not None and grant.status == "connected"
    assert (await tokens(db_session, who))["refresh_token"] == "u-refresh-a"


async def test_keep_alive_refreshes_without_calling_any_api(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)  # 访问令牌还有一小时才过期
    await personal.keep_alive(db_session, CIPHER, scope_of(who))
    await db_session.commit()
    assert api.refreshes == 1 and api.hits == []
    assert (await tokens(db_session, who))["refresh_token"] == "u-refresh-r1"
```

- [ ] **Step 4: 运行确认失败**

Run: `uv run pytest tests/unit/test_feishu_ratelimit.py tests/integration/test_feishu_ratelimit_budget.py -q`
Expected: FAIL，收集阶段 `ImportError: cannot import name 'ratelimit' from 'coreman.core.feishu_personal'`。

- [ ] **Step 5: 挪 `PersonalError`**

`coreman/core/feishu_personal/errors.py`（新建）：

```python
"""个人工具报给调用方（模型、worker、轮询循环）的错误：只带代码与上游的数字错误码。

单独成模块：`ratelimit` 要继承它，`service` 要用 `ratelimit`，放在 `service` 里会成环。
`service.PersonalError` 仍是这个类。
"""

from __future__ import annotations

from typing import Any


class PersonalError(Exception):
    def __init__(self, code: str, *, upstream_code: int | None = None):
        self.code = code
        self.upstream_code = upstream_code
        super().__init__(code)

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.code}
        if self.upstream_code is not None:
            out["upstream_code"] = self.upstream_code
        return out
```

`coreman/core/feishu_personal/service.py`：删掉整个 `class PersonalError(Exception): ...` 的定义（`__init__` 与 `payload` 两个方法一起），把导入

```python
from coreman.core.feishu_personal import endpoints, permissions, revocation
from coreman.core.feishu_personal.policy import Scope, app_credentials
```

换成

```python
from coreman.core.feishu_personal import endpoints, permissions, ratelimit, revocation
from coreman.core.feishu_personal.errors import PersonalError
from coreman.core.feishu_personal.policy import Scope, app_credentials
```

- [ ] **Step 6: 写 `ratelimit.py`**

`coreman/core/feishu_personal/ratelimit.py`（新建）：

```python
"""以所有者身份调飞书的限频：认出限频应答，按应用记用量。

飞书按「应用 × 企业 × 接口」限频，全员共用一个助理应用：列会话与读会话历史各每分钟 1000 次。
盯消息的轮询循环每轮先看最近 60 秒的用量，接近上限就拉长所有人的间隔，满了就跳过这一轮；
助理工具的调用只记不拦。用量是流水（一次调用一行）：计数器的行锁会被调用方的事务占着。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import FeishuRateUsage
from coreman.core.feishu_personal import endpoints
from coreman.core.feishu_personal.errors import PersonalError
from coreman.core.timeutils import aware_utc

RATE_LIMIT_CODE = 99991400
CHATS, HISTORY = "chats", "history"
# 每分钟的上限（官方文档；见 spec「前置验证」）。
LIMITS: dict[str, int] = {CHATS: 1000, HISTORY: 1000}
WINDOW = timedelta(seconds=60)
RETENTION = timedelta(minutes=15)
DEFAULT_RETRY, MAX_RETRY = 60, 600
# 用量到这个比例算「接近上限」：开始拉长间隔，并打告警日志。
TIGHT = 0.6
# 应答头里的等待秒数：飞书网关的头在前，标准的 Retry-After 兜底。
_RESET_HEADERS = ("x-ogw-ratelimit-reset", "retry-after")
_CATEGORIES = {("GET", "/im/v1/chats"): CHATS, ("GET", "/im/v1/messages"): HISTORY}


class RateLimited(PersonalError):
    """飞书说调得太频繁。只带等待秒数与接口类别，不带上游的任何文字。"""

    def __init__(self, retry_after: int, category: str | None = None) -> None:
        super().__init__("rate_limited", upstream_code=RATE_LIMIT_CODE)
        self.retry_after = max(1, min(MAX_RETRY, int(retry_after)))
        self.category = category

    def payload(self) -> dict[str, Any]:
        return {**super().payload(), "retry_after": self.retry_after}


def retry_after(headers: Mapping[str, str]) -> int:
    """应答头里说的等待秒数；读不到按 `DEFAULT_RETRY`。"""
    for name in _RESET_HEADERS:
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            seconds = int(float(raw))
        except ValueError:
            continue
        if seconds > 0:
            return min(MAX_RETRY, seconds)
    return DEFAULT_RETRY


def detect(status: int, data: Any, headers: Mapping[str, str]) -> RateLimited | None:
    """这个应答是不是限频：HTTP 429，或正文里的错误码 99991400。"""
    code = data.get("code") if isinstance(data, dict) else None
    if status == 429 or code == RATE_LIMIT_CODE:
        return RateLimited(retry_after(headers))
    return None


def category(endpoint: endpoints.Endpoint) -> str | None:
    """这个接口计入哪一类分钟用量；不计的返回 None。"""
    return _CATEGORIES.get((endpoint.method, endpoint.path))


def stretch(load: float) -> int | None:
    """用量占比对应的轮询间隔倍数；None 表示额度已满，这一轮不调。"""
    if load >= 1.0:
        return None
    if load >= 0.8:
        return 4
    return 2 if load >= TIGHT else 1


@dataclass(frozen=True)
class Budget:
    """一个应用的分钟用量。各方法都不提交。"""

    app_id: str

    async def record(
        self,
        session: AsyncSession,
        category: str,
        *,
        at: datetime | None = None,
        units: int = 1,
    ) -> None:
        values: dict[str, Any] = {"app_id": self.app_id, "category": category, "units": units}
        if at is not None:
            values["at"] = aware_utc(at)
        await session.execute(insert(FeishuRateUsage).values(**values))

    async def used(self, session: AsyncSession, now: datetime) -> dict[str, int]:
        """最近 60 秒各类用了多少。上界留给 `exhaust` 写在未来的那一笔。"""
        now = aware_utc(now)
        rows = await session.execute(
            select(FeishuRateUsage.category, func.coalesce(func.sum(FeishuRateUsage.units), 0))
            .where(
                FeishuRateUsage.app_id == self.app_id,
                FeishuRateUsage.at > now - WINDOW,
                FeishuRateUsage.at <= now + timedelta(seconds=MAX_RETRY),
            )
            .group_by(FeishuRateUsage.category)
        )
        found = {name: int(total) for name, total in rows.all()}
        return {name: found.get(name, 0) for name in LIMITS}

    async def load(self, session: AsyncSession, now: datetime) -> float:
        """两类里用量占上限比例的较大者。"""
        used = await self.used(session, now)
        return max(used[name] / LIMITS[name] for name in LIMITS)

    async def exhaust(
        self, session: AsyncSession, category: str, now: datetime, retry_after: int
    ) -> None:
        """飞书限频了：记一笔占满额度、恰好在等待结束时滑出窗口的用量，同一应用的人都先停下。"""
        at = aware_utc(now) + timedelta(seconds=retry_after) - WINDOW
        await self.record(session, category, at=at, units=LIMITS[category])


async def purge(session: AsyncSession, now: datetime) -> int:
    result = await session.execute(
        delete(FeishuRateUsage).where(FeishuRateUsage.at < aware_utc(now) - RETENTION)
    )
    return int(getattr(result, "rowcount", 0) or 0)
```

- [ ] **Step 7: 改 `service.py`**

`_http` 里把

```python
                data = jsonlib.loads(chunks)
                if not isinstance(data, dict):
                    raise PersonalError("upstream_unavailable")
```

换成

```python
                try:
                    data = jsonlib.loads(chunks)
                except ValueError:
                    data = None
                # 限频：状态码 429 或错误码 99991400。应答头里的等待秒数交给调用方退避；
                # 正文里的文字不外传。
                limited = ratelimit.detect(response.status_code, data, response.headers)
                if limited is not None:
                    raise limited
                if not isinstance(data, dict):
                    raise PersonalError("upstream_unavailable")
```

（`RateLimited` 是 `PersonalError`，不会被下面的 `except (httpx.HTTPError, ValueError)` 接住。）

`_authorize` 的签名

```python
async def _authorize(
    session: AsyncSession, cipher: Cipher, scope: Scope, method: str, path: str
) -> tuple[endpoints.Endpoint, FeishuPersonalGrant, str, dict[str, Any]]:
    """The registered endpoint, the owner's grant and a fresh token, or a PersonalError."""
```

换成

```python
async def _authorize(
    session: AsyncSession,
    cipher: Cipher,
    scope: Scope,
    method: str,
    path: str,
    *,
    refresh: bool = False,
) -> tuple[endpoints.Endpoint, FeishuPersonalGrant, str, dict[str, Any]]:
    """The registered endpoint, the owner's grant and a fresh token, or a PersonalError.

    `refresh` 为真时不看到期时间，直接刷新一次（保活用）。
    """
```

同一个函数里把

```python
    if not row.expires_at or row.expires_at <= datetime.now(UTC) + timedelta(seconds=60):
```

换成

```python
    if (
        refresh
        or not row.expires_at
        or row.expires_at <= datetime.now(UTC) + timedelta(seconds=60)
    ):
```

`api_request` 里把

```python
    endpoint, row, secret, tokens = await _authorize(session, cipher, scope, method, path)
    body: dict[str, Any] = {"data": data, "files": files} if files is not None else {"json": json}
    response = await _http(
        method,
        BASE + path,
        headers={"Authorization": "Bearer " + tokens["access_token"]},
        params=params,
        seconds=120 if files is not None else 20,
        **body,
    )
```

换成

```python
    endpoint, row, secret, tokens = await _authorize(session, cipher, scope, method, path)
    # 助理共用一个应用，飞书按应用限频：列会话与读历史各记一笔用量，盯消息的循环据此拉长
    # 间隔。这里只记不拦；普通员工不记。
    category = ratelimit.category(endpoint) if is_assistant(scope.bot) else None
    if category is not None:
        await ratelimit.Budget(row.app_id).record(session, category)
    body: dict[str, Any] = {"data": data, "files": files} if files is not None else {"json": json}
    try:
        response = await _http(
            method,
            BASE + path,
            headers={"Authorization": "Bearer " + tokens["access_token"]},
            params=params,
            seconds=120 if files is not None else 20,
            **body,
        )
    except ratelimit.RateLimited as exc:
        exc.category = category
        raise
```

文件末尾追加：

```python
async def keep_alive(session: AsyncSession, cipher: Cipher, scope: Scope) -> None:
    """只把所有者的令牌续上，不调任何业务接口。

    刷新令牌约 7 天不用就失效：盯消息在不轮询的时候（下班、暂停）每 24 小时调一次。与读写
    走同一把锁、同一次落库；调用方必须提交。

    Raises:
        PersonalError: 没有有效的授权、刷新被拒（授权随之标为过期）、上游不可用或限频
    """
    await _authorize(
        session, cipher, scope, "GET", endpoints.ENDPOINTS["im.chats"].path, refresh=True
    )
```

- [ ] **Step 8: 运行确认通过**

Run: `uv run pytest tests/unit/test_feishu_ratelimit.py tests/integration/test_feishu_ratelimit_budget.py -q`
Expected: PASS。

回归（`_http`、`api_request`、`PersonalError` 在所有个人工具的路径上）：

Run: `uv run pytest tests/api/test_feishu_personal.py tests/api/test_feishu_personal_levels.py tests/api/test_feishu_personal_cards.py tests/api/test_feishu_personal_worker.py tests/api/test_assistant_approval_gate.py tests/integration/test_assistant_approval_execute.py tests/integration/test_assistant_approval_submit.py tests/integration/test_assistant_provisioning.py tests/integration/test_assistant_deprovisioning.py tests/integration/test_assistant_standard_overhead.py -q`
Expected: PASS。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 9: 提交**

```bash
git add coreman/core/feishu_personal/errors.py coreman/core/feishu_personal/ratelimit.py \
  coreman/core/feishu_personal/service.py tests/integration/watch_helpers.py \
  tests/unit/test_feishu_ratelimit.py tests/integration/test_feishu_ratelimit_budget.py
git commit -m "以所有者身份调飞书：识别限频、按应用记用量、保活刷新"
```

### Task 3: 分级（`watch_grade.py`）

**Files:**
- Create: `coreman/core/assistants/watch_grade.py`
- Test: `tests/unit/test_assistant_watch_grade.py`（新建）

**Interfaces:**
- Consumes: 2A 的 `AssistantSettings`（`open_id`、`focus_chats_enc`、`ignored_chats_enc`）；`coreman.core.timeutils.aware_utc`。
- Produces（`coreman/core/assistants/watch_grade.py`）：
  - `class Level(StrEnum)`：`IMMEDIATE = "immediate"`、`BATCH = "batch"`、`HOLD = "hold"`、`IGNORE = "ignore"`（前三个的值就是 `assistant_inbox.level` 的取值）。
  - `HOLD_WINDOW = timedelta(minutes=10)`、`BATCH_WINDOW = timedelta(minutes=5)`。
  - `@dataclass(frozen=True) class Item`：`chat_id: str`、`chat_kind: str`（`p2p` / `group`）、`message_id: str`、`sender_id: str`、`sender_type: str`、`at_ms: int`，以及带默认值的 `kind: str = "message"`（另认 `approval_todo`、`approval_outcome`）、`thread_id: str | None = None`、`msg_type: str = "text"`、`text: str = ""`、`mentions: frozenset[str] = frozenset()`、`deleted: bool = False`、`chat_name: str = ""`；属性 `created_at -> datetime`。
  - `@dataclass(frozen=True) class Context`：`now: datetime`、`owner_spoke_at: datetime | None = None`（所有者上次在这个会话里亲自发言的时刻）。
  - `def grade(item: Item, settings: AssistantSettings, context: Context) -> Level`
  - `def release_at(level: Level, context: Context, *, batch_until: datetime | None) -> datetime | None`：这一级的条目压到什么时候；立即交回的返回 `None`。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_assistant_watch_grade.py`（新建）：

```python
"""分级表（spec 第五节）的每一行，以及压着与攒批的时间窗。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from coreman.core.assistants.watch_grade import (
    BATCH_WINDOW,
    HOLD_WINDOW,
    Context,
    Item,
    Level,
    grade,
    release_at,
)
from coreman.core.db.models import AssistantSettings

OWNER = "ou_owner"
NOW = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)


def prefs() -> AssistantSettings:
    return AssistantSettings(
        bot_id=uuid.uuid4(),
        open_id=OWNER,
        focus_chats_enc=["oc_focus"],
        ignored_chats_enc=["oc_noise"],
    )


def item(**over: Any) -> Item:
    values: dict[str, Any] = {
        "chat_id": "oc_team",
        "chat_kind": "group",
        "message_id": "om_1",
        "sender_id": "ou_li",
        "sender_type": "user",
        "at_ms": int(NOW.timestamp() * 1000),
    }
    values.update(over)
    return Item(**values)


P2P = {"chat_id": "oc_li", "chat_kind": "p2p"}
BOT = {"sender_type": "app", "sender_id": "cli_other"}
AT_ME = {"mentions": frozenset({OWNER})}
NO_CHAT = {"chat_id": "", "chat_kind": ""}
# （说明, 条目, 所有者多久前在这个会话里发过言, 期望的级别）
TABLE: list[tuple[str, Item, timedelta | None, Level]] = [
    ("私聊", item(**P2P), None, Level.IMMEDIATE),
    ("群里 @ 本人", item(**AT_ME), None, Level.IMMEDIATE),
    ("群里机器人 @ 本人", item(**BOT, **AT_ME), None, Level.IMMEDIATE),
    ("审批被拒或执行失败", item(kind="approval_outcome", **NO_CHAT), None, Level.IMMEDIATE),
    ("重点群里的消息", item(chat_id="oc_focus"), None, Level.IMMEDIATE),
    ("其他群里真人的消息", item(), None, Level.BATCH),
    ("群里 @ 了别人", item(mentions=frozenset({"ou_wang"})), None, Level.BATCH),
    ("新出现的飞书审批待办", item(kind="approval_todo", **NO_CHAT), None, Level.BATCH),
    ("私聊里机器人发来的通知", item(**P2P, **BOT), None, Level.BATCH),
    ("所有者 10 分钟内在该会话发过言", item(**P2P), timedelta(minutes=9), Level.HOLD),
    ("所有者刚在重点群里发过言", item(chat_id="oc_focus"), timedelta(minutes=1), Level.HOLD),
    ("所有者刚发过言的群里 @ 本人", item(**AT_ME), timedelta(seconds=5), Level.HOLD),
    ("他停下已满 10 分钟", item(**P2P), timedelta(minutes=10), Level.IMMEDIATE),
    ("所有者自己发的", item(sender_id=OWNER), None, Level.IGNORE),
    ("所有者自己在私聊里发的", item(**P2P, sender_id=OWNER), None, Level.IGNORE),
    ("群里没 @ 他的机器人消息", item(**BOT), None, Level.IGNORE),
    ("重点群里没 @ 他的机器人消息", item(chat_id="oc_focus", **BOT), None, Level.IGNORE),
    ("所有者设为忽略的会话", item(chat_id="oc_noise", **AT_ME), None, Level.IGNORE),
    ("忽略的会话里他刚发过言", item(chat_id="oc_noise"), timedelta(minutes=1), Level.IGNORE),
    ("系统消息", item(**P2P, msg_type="system", sender_type=""), None, Level.IGNORE),
    ("已撤回的消息", item(**P2P, deleted=True), None, Level.IGNORE),
]


@pytest.mark.parametrize(("why", "one", "ago", "level"), TABLE, ids=[row[0] for row in TABLE])
def test_every_row_of_the_table(
    why: str, one: Item, ago: timedelta | None, level: Level
) -> None:
    spoke = NOW - ago if ago is not None else None
    assert grade(one, prefs(), Context(now=NOW, owner_spoke_at=spoke)) is level, why


def test_settings_without_lists_grade_like_empty_lists() -> None:
    bare = AssistantSettings(bot_id=uuid.uuid4(), open_id=OWNER)
    assert grade(item(chat_id="oc_focus"), bare, Context(now=NOW)) is Level.BATCH
    assert grade(item(chat_id="oc_noise"), bare, Context(now=NOW)) is Level.BATCH


def test_levels_are_the_stored_values() -> None:
    assert [level.value for level in Level] == ["immediate", "batch", "hold", "ignore"]
    assert item().created_at == NOW
    assert (HOLD_WINDOW, BATCH_WINDOW) == (timedelta(minutes=10), timedelta(minutes=5))


def test_release_times() -> None:
    quiet = Context(now=NOW)
    spoke = NOW - timedelta(minutes=4)
    assert release_at(Level.IMMEDIATE, quiet, batch_until=None) is None
    # 攒批：没有在攒的一批就从现在起 5 分钟；有就并进去，一起到点。
    assert release_at(Level.BATCH, quiet, batch_until=None) == NOW + timedelta(minutes=5)
    soon = NOW + timedelta(minutes=2)
    assert release_at(Level.BATCH, quiet, batch_until=soon) == soon
    stale = NOW - timedelta(seconds=1)
    assert release_at(Level.BATCH, quiet, batch_until=stale) == NOW + timedelta(minutes=5)
    # 压着：到他这次发言之后 10 分钟。
    held = Context(now=NOW, owner_spoke_at=spoke)
    assert release_at(Level.HOLD, held, batch_until=soon) == spoke + timedelta(minutes=10)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_watch_grade.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'coreman.core.assistants.watch_grade'`。

- [ ] **Step 3: 实现**

`coreman/core/assistants/watch_grade.py`（新建）：

```python
"""盯消息的分级：一条拉到的消息该立即交回、攒批、压着，还是忽略。

纯函数，不碰库也不调飞书：消息内容只在调用方的内存里。分级表见 spec 第五节。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from coreman.core.db.models import AssistantSettings
from coreman.core.timeutils import aware_utc

# 所有者在一个会话里亲自发言之后这么久内，这个会话的条目先压着。
HOLD_WINDOW = timedelta(minutes=10)
# 攒批的条目每这么久交回一次。
BATCH_WINDOW = timedelta(minutes=5)


class Level(StrEnum):
    IMMEDIATE = "immediate"
    BATCH = "batch"
    HOLD = "hold"
    IGNORE = "ignore"


@dataclass(frozen=True)
class Item:
    """一件可能要交回的事。`kind` 为 `message` 时其余字段取自飞书的一条消息。"""

    chat_id: str
    chat_kind: str
    message_id: str
    sender_id: str
    sender_type: str
    at_ms: int
    kind: str = "message"
    thread_id: str | None = None
    msg_type: str = "text"
    text: str = ""
    mentions: frozenset[str] = frozenset()
    deleted: bool = False
    chat_name: str = ""

    @property
    def created_at(self) -> datetime:
        return datetime.fromtimestamp(self.at_ms / 1000, UTC)


@dataclass(frozen=True)
class Context:
    """分级时消息之外要知道的：现在几点，所有者上次在这个会话里亲自发言是什么时候。"""

    now: datetime
    owner_spoke_at: datetime | None = None


def _holding(context: Context) -> bool:
    spoke = context.owner_spoke_at
    return spoke is not None and aware_utc(context.now) - aware_utc(spoke) < HOLD_WINDOW


def grade(item: Item, settings: AssistantSettings, context: Context) -> Level:
    """按 spec 第五节的表分级。先排除（忽略），再看压着，最后才分立即与攒批。"""
    if item.kind == "approval_outcome":
        return Level.IMMEDIATE
    if item.kind == "approval_todo":
        return Level.BATCH
    owner = settings.open_id
    if item.chat_id in (settings.ignored_chats_enc or []):
        return Level.IGNORE
    if item.deleted or item.msg_type == "system":
        return Level.IGNORE
    human = item.sender_type == "user"
    if human and item.sender_id == owner:
        return Level.IGNORE
    mentioned = owner in item.mentions
    private = item.chat_kind == "p2p"
    if not private and not human and not mentioned:
        # 群里没 @ 他的机器人消息。
        return Level.IGNORE
    if _holding(context):
        return Level.HOLD
    if private:
        # 私聊里机器人发来的（审批待办等通知）不急，攒批。
        return Level.IMMEDIATE if human else Level.BATCH
    if mentioned or item.chat_id in (settings.focus_chats_enc or []):
        return Level.IMMEDIATE
    return Level.BATCH


def release_at(
    level: Level, context: Context, *, batch_until: datetime | None
) -> datetime | None:
    """这一级的条目压到什么时候才交回；立即交回的返回 None。

    Args:
        batch_until: 这位助理正在攒的那一批什么时候到点（没有在攒的给 None）
    """
    now = aware_utc(context.now)
    if level is Level.HOLD and context.owner_spoke_at is not None:
        return aware_utc(context.owner_spoke_at) + HOLD_WINDOW
    if level is Level.BATCH:
        if batch_until is not None and aware_utc(batch_until) > now:
            return aware_utc(batch_until)
        return now + BATCH_WINDOW
    return None
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_watch_grade.py -q`
Expected: PASS。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants/watch_grade.py tests/unit/test_assistant_watch_grade.py
git commit -m "盯消息的四级分级"
```

### Task 4: 列会话、顺序对比与读历史（`watch_feed.py`）

**Files:**
- Create: `coreman/core/assistants/watch_feed.py`
- Test: `tests/unit/test_assistant_watch_feed.py`（新建）

**Interfaces:**
- Consumes: Task 3 的 `Item`；`coreman.core.assistants.approvals.checks.parse`（把飞书的一条消息解析成 `Message(message_id, chat_id, sender_id, sender_type, text, deleted)`）与 `checks.Reader`（`toolbase.Call` 或签名相同的替身：`await call(key, *, path=None, params=None, json=None) -> dict`）；`PersonalError`。
- Produces（`coreman/core/assistants/watch_feed.py`）：
  - `Reader = checks.Reader`；`CHAT_TYPES = "p2p,group"`；`LIST_PARAMS: dict[str, Any]`；`TOP = 50`、`PAGE = 50`、`MAX_PAGES = 2`、`THREAD_PAGE = 20`、`THREADS_TRACKED = 3`、`CURSOR_CHATS = 200`、`RECALL_BATCH = 20`。
  - `@dataclass(frozen=True) class Listed`：`order: list[str]`、`names: dict[str, str]`、`kinds: dict[str, str]`。
  - `async def list_chats(call: Reader) -> Listed`
  - `def split(order: list[str], prev: list[str]) -> tuple[list[str], list[str]]`：（挪动过的前缀, 相对顺序没变的后缀）。
  - `def to_item(raw: dict[str, Any], *, chat_id: str, chat_kind: str, chat_name: str, thread_id: str | None = None) -> Item | None`
  - `def fresh(last: int, same: list[str], item: Item) -> bool`
  - `async def read_main(call: Reader, chat_id: str, since_ms: int) -> tuple[list[dict[str, Any]], int]`：（主线里 `since_ms` 那一秒起的消息，从旧到新；调用次数）。
  - `async def read_thread(call: Reader, thread_id: str) -> list[dict[str, Any]]`：话题里最近 20 条，从旧到新。
  - `async def chat_kind(call: Reader, chat_id: str) -> tuple[str, str]`：（`p2p` / `group`，会话名）。
  - `async def recalled(call: Reader, message_ids: list[str]) -> list[str]`：其中已被撤回的。
  - `def load(raw: dict[str, Any]) -> dict[str, Any]`、`def dump(cursor: dict[str, Any], todo: list[str]) -> dict[str, Any]`：游标的规整副本与写回（只留最近 200 个会话）。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_assistant_watch_feed.py`（新建）：

```python
"""盯消息的读取：顺序对比找出有新动静的会话，读历史的参数，消息解析与游标。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from coreman.core.assistants import watch_feed as feed
from coreman.core.feishu_personal.service import PersonalError


class Reader:
    """`toolbase.Call` 的替身：按接口名依次给出准备好的应答，记下每次调用。"""

    def __init__(self, **answers: list[Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, str] | None, dict[str, Any] | None]] = []

    async def __call__(
        self, key: str, *, path: Any = None, params: Any = None, json: Any = None
    ) -> dict[str, Any]:
        self.calls.append((key, path, params))
        answer = self.answers[key].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[no-any-return]


def raw(mid: str, ms: int, **extra: Any) -> dict[str, Any]:
    return {
        "message_id": mid,
        "chat_id": "oc_team",
        "msg_type": "text",
        "create_time": str(ms),
        "sender": {"id": "ou_li", "id_type": "open_id", "sender_type": "user"},
        "body": {"content": json.dumps({"text": "在吗"}, ensure_ascii=False)},
        **extra,
    }


@pytest.mark.parametrize(
    ("order", "prev", "moved", "rest"),
    [
        # 顺序没变：没有挪动过的，后缀从第一个开始探。
        (["a", "b", "c"], ["a", "b", "c"], [], ["a", "b", "c"]),
        # b 来了消息，排到了前面。
        (["b", "a", "c"], ["a", "b", "c"], ["b"], ["a", "c"]),
        # c 来了消息，随后队首 a 也来了：a 没挪位置，但排在挪动过的 c 前面，一起读。
        (["a", "c", "b"], ["a", "b", "c"], ["a", "c"], ["b"]),
        # 没见过的会话排在最前：新动静。
        (["x", "a", "b"], ["a", "b", "c"], ["x"], ["a", "b"]),
        # 上面有会话退出，末尾补进来一个没见过的旧会话：不算新动静。
        (["a", "c", "d"], ["a", "b", "c"], [], ["a", "c", "d"]),
        # 没见过的会话夹在中间：留在后缀里，由逐个探测决定读不读。
        (["a", "x", "b"], ["a", "b"], [], ["a", "x", "b"]),
        # 上一轮什么都没有：全算挪动过。
        (["a", "b"], [], ["a", "b"], []),
        ([], ["a"], [], []),
    ],
)
def test_split_finds_the_chats_that_moved_up(
    order: list[str], prev: list[str], moved: list[str], rest: list[str]
) -> None:
    assert feed.split(order, prev) == (moved, rest)


async def test_list_chats_asks_once_and_tolerates_odd_items() -> None:
    reader = Reader(
        **{
            "im.chats": [
                {
                    "items": [
                        {"chat_id": "oc_li", "chat_mode": "p2p"},
                        {"chat_id": "oc_team", "name": "项目群", "chat_mode": "group"},
                        {"chat_id": "oc_topic", "chat_mode": "topic"},
                        {"chat_id": "oc_plain", "name": "没带类型"},
                        {"chat_id": "oc_team", "name": "重复的"},
                        {"name": "没有编号"},
                        "不是对象",
                    ]
                }
            ]
        }
    )
    listed = await feed.list_chats(reader)
    assert listed.order == ["oc_li", "oc_team", "oc_topic", "oc_plain"]
    assert listed.names["oc_team"] == "项目群" and listed.names["oc_li"] == ""
    assert listed.kinds == {"oc_li": "p2p", "oc_team": "group", "oc_topic": "group"}
    assert reader.calls == [
        (
            "im.chats",
            None,
            {
                "user_id_type": "open_id",
                "sort_type": "ByActiveTimeDesc",
                "page_size": 50,
                "types": "p2p,group",
            },
        )
    ]
    assert (await feed.list_chats(Reader(**{"im.chats": [{}]}))).order == []


def test_to_item_reads_time_mentions_and_thread() -> None:
    item = feed.to_item(
        raw(
            "om_1",
            1_700_000_000_123,
            thread_id="omt_1",
            mentions=[{"key": "@_user_1", "id": "ou_owner", "id_type": "open_id"}, "坏的"],
        ),
        chat_id="oc_team",
        chat_kind="group",
        chat_name="项目群",
    )
    assert item is not None
    assert (item.message_id, item.at_ms, item.thread_id) == ("om_1", 1_700_000_000_123, "omt_1")
    assert (item.sender_id, item.sender_type, item.msg_type) == ("ou_li", "user", "text")
    assert item.text == "在吗" and item.mentions == frozenset({"ou_owner"})
    assert (item.chat_id, item.chat_kind, item.chat_name) == ("oc_team", "group", "项目群")
    in_thread = feed.to_item(
        raw("om_2", 5), chat_id="oc_team", chat_kind="group", chat_name="", thread_id="omt_9"
    )
    assert in_thread is not None and in_thread.thread_id == "omt_9"
    gone = feed.to_item(raw("om_3", 5, deleted=True), chat_id="c", chat_kind="p2p", chat_name="")
    assert gone is not None and gone.deleted
    # 没有编号或读不出时间的不要。
    for bad in (raw("", 5), raw("om_4", 0), {**raw("om_5", 5), "create_time": "昨天"}, {}):
        assert feed.to_item(bad, chat_id="c", chat_kind="p2p", chat_name="") is None


def test_fresh_uses_milliseconds_and_the_ids_at_the_cursor() -> None:
    def at(mid: str, ms: int) -> Any:
        return feed.to_item(raw(mid, ms), chat_id="c", chat_kind="p2p", chat_name="")

    assert feed.fresh(100, ["om_1"], at("om_2", 101))
    assert feed.fresh(100, ["om_1"], at("om_2", 100))
    assert not feed.fresh(100, ["om_1"], at("om_1", 100))
    assert not feed.fresh(100, [], at("om_0", 99))


async def test_read_main_starts_at_the_cursor_second_and_follows_one_more_page() -> None:
    reader = Reader(
        **{
            "im.history": [
                {"items": [raw("om_1", 1000), "坏的"], "has_more": True, "page_token": "p2"},
                {"items": [raw("om_2", 2000)], "has_more": True, "page_token": "p3"},
            ]
        }
    )
    items, calls = await feed.read_main(reader, "oc_team", 1_700_000_000_999)
    assert [item["message_id"] for item in items] == ["om_1", "om_2"] and calls == 2
    first = {
        "container_id_type": "chat",
        "container_id": "oc_team",
        "start_time": "1700000000",
        "sort_type": "ByCreateTimeAsc",
        "page_size": 50,
    }
    assert reader.calls == [
        ("im.history", None, first),
        ("im.history", None, {**first, "page_token": "p2"}),
    ]
    one, calls = await feed.read_main(Reader(**{"im.history": [{"items": []}]}), "oc_x", 0)
    assert one == [] and calls == 1


async def test_read_thread_takes_the_latest_page_without_a_time_range() -> None:
    reader = Reader(**{"im.history": [{"items": [raw("om_3", 3000), raw("om_2", 2000)]}]})
    items = await feed.read_thread(reader, "omt_1")
    assert [item["message_id"] for item in items] == ["om_2", "om_3"]
    assert reader.calls == [
        (
            "im.history",
            None,
            {
                "container_id_type": "thread",
                "container_id": "omt_1",
                "sort_type": "ByCreateTimeDesc",
                "page_size": 20,
            },
        )
    ]


async def test_chat_kind_asks_the_chat_itself() -> None:
    reader = Reader(**{"im.chat": [{"chat_mode": "p2p"}, {"chat_mode": "group", "name": "项目群"}]})
    assert await feed.chat_kind(reader, "oc_li") == ("p2p", "")
    assert await feed.chat_kind(reader, "oc_team") == ("group", "项目群")
    assert reader.calls[0] == ("im.chat", {"chat_id": "oc_li"}, {"user_id_type": "open_id"})


async def test_recalled_checks_at_most_twenty_ids() -> None:
    ids = [f"om_{n}" for n in range(25)]
    answer = {"items": [raw("om_1", 5, deleted=True), raw("om_2", 5), {"deleted": True}]}
    reader = Reader(**{"im.mget": [answer]})
    assert await feed.recalled(reader, ids) == ["om_1"]
    assert reader.calls == [("im.mget", None, {"message_ids": ids[:20]})]
    assert await feed.recalled(Reader(), []) == []
    # 一条都读不到（不可见、已不存在）不算撤回；别的错误照抛。
    unreadable = Reader(**{"im.mget": [PersonalError("feishu_read_failed")]})
    assert await feed.recalled(unreadable, ["om_1"]) == []
    with pytest.raises(PersonalError):
        await feed.recalled(Reader(**{"im.mget": [PersonalError("authorization_required")]}), ["x"])


def test_cursor_round_trip_copies_and_prunes() -> None:
    stored = {"since": 5, "chats": {"oc_a": {"t": 9, "m": ["om_1"]}, "bad": "x"}, "todo": ["oc_b", 3]}
    cursor = feed.load(stored)
    assert cursor == {"since": 5, "chats": {"oc_a": {"t": 9, "m": ["om_1"]}}, "todo": ["oc_b"]}
    cursor["chats"]["oc_a"]["t"] = 10
    assert stored["chats"]["oc_a"]["t"] == 9  # 改的是副本
    assert feed.load({}) == {"since": 0, "chats": {}, "todo": []}
    many = {"since": 1, "chats": {f"oc_{n}": {"t": n} for n in range(250)}, "todo": []}
    kept = feed.dump(many, ["oc_z"] * 80)
    assert len(kept["chats"]) == 200 and "oc_249" in kept["chats"] and "oc_49" not in kept["chats"]
    assert kept["since"] == 1 and len(kept["todo"]) == 50
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_watch_feed.py -q`
Expected: FAIL，`ImportError: cannot import name 'watch_feed' from 'coreman.core.assistants'`。

- [ ] **Step 3: 实现**

`coreman/core/assistants/watch_feed.py`（新建）：

```python
"""盯消息的读取：列会话、对比顺序找出有新动静的会话、读历史。

只调读接口（列会话、会话信息、会话历史、按编号取消息），都经 `toolbase.Call`，以所有者的身份。
拉到的消息只在调用方的内存里；这里不碰库。

飞书返回的字段有几处没在真实租户里核对过（见实现计划「需真机确认」），所以每个字段都按
「可能没有」来读：缺了就退到保守的做法，不抛错。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from coreman.core.assistants.approvals import checks
from coreman.core.assistants.watch_grade import Item
from coreman.core.feishu_personal.service import PersonalError

Reader = checks.Reader
# 一次列出单聊与群。spec「前置验证」只实测过 `types=p2p`，这个取值待真机确认。
CHAT_TYPES = "p2p,group"
TOP = 50
LIST_PARAMS: dict[str, Any] = {
    "user_id_type": "open_id",
    "sort_type": "ByActiveTimeDesc",
    "page_size": TOP,
    "types": CHAT_TYPES,
}
PAGE = 50
# 一个会话一轮最多读这么多页；读不完的下一轮从游标接着读。
MAX_PAGES = 2
THREAD_PAGE = 20
# 每个会话跟着的话题数：读这个会话时顺带读它们。
THREADS_TRACKED = 3
CURSOR_CHATS = 200
RECALL_BATCH = 20


@dataclass(frozen=True)
class Listed:
    """列会话的结果：按最近活跃倒序的会话 id，以及顺带拿到的名字与类型。"""

    order: list[str]
    names: dict[str, str]
    kinds: dict[str, str]


def _kind(mode: Any) -> str | None:
    if mode == "p2p":
        return "p2p"
    return "group" if isinstance(mode, str) and mode else None


async def list_chats(call: Reader) -> Listed:
    data = await call("im.chats", params=dict(LIST_PARAMS))
    order: list[str] = []
    names: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for item in data.get("items") or []:
        chat_id = item.get("chat_id") if isinstance(item, dict) else None
        if not isinstance(chat_id, str) or not chat_id or chat_id in names:
            continue
        order.append(chat_id)
        names[chat_id] = str(item.get("name") or "")
        kind = _kind(item.get("chat_mode"))
        if kind is not None:
            kinds[chat_id] = kind
    return Listed(order[:TOP], names, kinds)


def split(order: list[str], prev: list[str]) -> tuple[list[str], list[str]]:
    """把这一轮的顺序切成（挪动过的前缀, 相对顺序没变的后缀）。

    上一轮之后有新消息的会话都会排到没有新消息的前面，所以没有新消息的会话是一段后缀，彼此的
    先后与上一轮相同。从末尾往前找这段最长的后缀；没见过的会话夹在其中不打断它（上面有会话
    退出时末尾会补进旧会话），但排在最前面的没见过的会话算新动静。

    后缀开头的几个仍可能藏着新消息（它们来了消息但名次没变），由调用方从头逐个探。
    """
    rank = {chat: index for index, chat in enumerate(prev)}
    cut, floor = len(order), len(prev)
    for index in range(len(order) - 1, -1, -1):
        position = rank.get(order[index])
        if position is not None:
            if position >= floor:
                break
            floor = position
        cut = index
    while cut < len(order) and order[cut] not in rank:
        cut += 1
    return order[:cut], order[cut:]


def to_item(
    raw: dict[str, Any],
    *,
    chat_id: str,
    chat_kind: str,
    chat_name: str,
    thread_id: str | None = None,
) -> Item | None:
    """飞书的一条消息；没有编号或读不出时间的返回 None。"""
    parsed = checks.parse(raw)
    try:
        at_ms = int(raw.get("create_time") or 0)
    except (TypeError, ValueError):
        return None
    if not parsed.message_id or at_ms <= 0:
        return None
    mentions = frozenset(
        str(one["id"])
        for one in raw.get("mentions") or []
        if isinstance(one, dict) and one.get("id")
    )
    own_thread = raw.get("thread_id")
    return Item(
        chat_id=chat_id,
        chat_kind=chat_kind,
        message_id=parsed.message_id,
        sender_id=parsed.sender_id,
        sender_type=parsed.sender_type,
        at_ms=at_ms,
        thread_id=thread_id or (own_thread if isinstance(own_thread, str) and own_thread else None),
        msg_type=str(raw.get("msg_type") or ""),
        text=parsed.text,
        mentions=mentions,
        deleted=parsed.deleted,
        chat_name=chat_name,
    )


def fresh(last: int, same: list[str], item: Item) -> bool:
    """这条消息在游标之后：晚于 `last`，或与它同一毫秒但不在已读过的 `same` 里。"""
    return item.at_ms > last or (item.at_ms == last and item.message_id not in same)


async def read_main(
    call: Reader, chat_id: str, since_ms: int
) -> tuple[list[dict[str, Any]], int]:
    """会话主线里 `since_ms` 所在那一秒起的消息，从旧到新；另返回调了几次接口。

    `start_time` 以秒计，所以会把游标那一秒的消息再读回来，由调用方用 `fresh` 滤掉。
    """
    params: dict[str, Any] = {
        "container_id_type": "chat",
        "container_id": chat_id,
        "start_time": str(since_ms // 1000),
        "sort_type": "ByCreateTimeAsc",
        "page_size": PAGE,
    }
    items: list[dict[str, Any]] = []
    calls = 0
    for _ in range(MAX_PAGES):
        data = await call("im.history", params=dict(params))
        calls += 1
        items.extend(item for item in data.get("items") or [] if isinstance(item, dict))
        token = data.get("page_token") if data.get("has_more") else None
        if not isinstance(token, str) or not token:
            break
        params["page_token"] = token
    return items, calls


async def read_thread(call: Reader, thread_id: str) -> list[dict[str, Any]]:
    """话题里最近的一页，从旧到新。话题容器不带时间范围，由调用方按时间过滤。"""
    data = await call(
        "im.history",
        params={
            "container_id_type": "thread",
            "container_id": thread_id,
            "sort_type": "ByCreateTimeDesc",
            "page_size": THREAD_PAGE,
        },
    )
    items = [item for item in data.get("items") or [] if isinstance(item, dict)]
    items.reverse()
    return items


async def chat_kind(call: Reader, chat_id: str) -> tuple[str, str]:
    """列会话没给类型时问会话本身：（`p2p` 或 `group`，会话名）。"""
    info = await call("im.chat", path={"chat_id": chat_id}, params={"user_id_type": "open_id"})
    return ("p2p" if info.get("chat_mode") == "p2p" else "group"), str(info.get("name") or "")


async def recalled(call: Reader, message_ids: list[str]) -> list[str]:
    """这些消息里已被对方撤回的。一条都读不到（不可见、已不存在）不算撤回。"""
    if not message_ids:
        return []
    try:
        data = await call("im.mget", params={"message_ids": message_ids[:RECALL_BATCH]})
    except PersonalError as exc:
        if exc.code != "feishu_read_failed":
            raise
        return []
    return [
        str(item["message_id"])
        for item in data.get("items") or []
        if isinstance(item, dict) and item.get("deleted") and item.get("message_id")
    ]


def load(raw: dict[str, Any]) -> dict[str, Any]:
    """库里的游标规整成一份可以随便改的副本。"""
    chats = raw.get("chats")
    todo = raw.get("todo")
    since = raw.get("since")
    return {
        "since": since if isinstance(since, int) else 0,
        "chats": {
            str(chat): dict(entry)
            for chat, entry in (chats if isinstance(chats, dict) else {}).items()
            if isinstance(entry, dict)
        },
        "todo": [chat for chat in (todo if isinstance(todo, list) else []) if isinstance(chat, str)],
    }


def dump(cursor: dict[str, Any], todo: list[str]) -> dict[str, Any]:
    """写回库的游标：只留最近读过的 `CURSOR_CHATS` 个会话。"""
    chats: dict[str, dict[str, Any]] = cursor["chats"]
    kept = sorted(chats, key=lambda chat: int(chats[chat].get("t") or 0), reverse=True)
    return {
        "since": cursor["since"],
        "chats": {chat: chats[chat] for chat in kept[:CURSOR_CHATS]},
        "todo": todo[:TOP],
    }
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_watch_feed.py -q`
Expected: PASS。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants/watch_feed.py tests/unit/test_assistant_watch_feed.py
git commit -m "盯消息的读取：列会话、顺序对比、读历史与话题"
```

### Task 5: 收件箱的写入（`watch_inbox.py`）

**Files:**
- Create: `coreman/core/assistants/watch_inbox.py`
- Modify: `tests/unit/test_assistant_sealed_ledger.py`（`WRITE_STATEMENTS`）
- Test: `tests/integration/test_assistant_watch_inbox.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `AssistantInbox`；Task 3 的 `Item`、`Level`；`sealed.seal_values(session, model, bot_id, values) -> dict`（Core 语句写内容列之前封存）。
- Produces（`coreman/core/assistants/watch_inbox.py`，都不提交）：
  - `SUMMARY_TEXT = 500`
  - `def summary(item: Item, owner_open_id: str) -> dict[str, Any]`：键 `chat_kind`、`chat_name`、`sender_id`、`sender_type`、`msg_type`、`text`（截到 500 字）、`mentioned`。
  - `async def batch_until(session, bot_id: uuid.UUID, now: datetime) -> datetime | None`：这位助理正在攒的那一批什么时候到点。
  - `async def add(session, bot_id: uuid.UUID, item: Item, level: Level, *, owner_open_id: str, hold_until: datetime | None) -> bool`：入箱；这条消息已经在箱里时什么都不做，返回 `False`。
  - `async def hold_chat(session, bot_id: uuid.UUID, chat_id: str, until: datetime) -> int`：这个会话里还没交回的条目改成 `hold`，压到 `until`（只往后推）。
  - `async def void_message(session, bot_id: uuid.UUID, message_id: str, code: str) -> int`
  - `async def void_all(session, bot_id: uuid.UUID, code: str) -> int`
  - `async def pending_messages(session, bot_id: uuid.UUID, limit: int = 20) -> list[str]`：还没交回的消息条目的消息 id，新的在前。
  - 「还没交回」统一指 `status = 'pending' AND delivered_task_id IS NULL`：投递层（2E）给条目写上 `delivered_task_id` 之后，这里不再改它。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_watch_inbox.py`（新建）：

```python
"""收件箱的写入：摘要是密文、同一条消息只入一次、压着只往后推、作废不碰正在交回的条目。"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.assistants import watch_inbox as inbox
from coreman.core.assistants.watch_grade import Item, Level
from coreman.core.db.models import AssistantInbox
from coreman.core.timeutils import utcnow
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
OWNER = "ou_owner"


def item(mid: str = "om_1", **over: Any) -> Item:
    values: dict[str, Any] = {
        "chat_id": "oc_li",
        "chat_kind": "p2p",
        "message_id": mid,
        "sender_id": "ou_li",
        "sender_type": "user",
        "at_ms": int(utcnow().timestamp() * 1000),
        "text": f"周五能给报价吗？{SECRET}",
        "chat_name": "李四",
    }
    values.update(over)
    return Item(**values)


async def owned(session: AsyncSession) -> uuid.UUID:
    bot, owner = await seed_assistant(session)
    await keys.ensure_key(session, sealed.master(), owner.id)
    return bot.id


async def rows(session: AsyncSession, bot_id: uuid.UUID) -> list[AssistantInbox]:
    found = list(
        await session.scalars(
            select(AssistantInbox)
            .where(AssistantInbox.bot_id == bot_id)
            .order_by(AssistantInbox.id)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, found)
    return found


async def test_an_item_is_stored_with_a_sealed_summary(db_session: AsyncSession) -> None:
    bot_id = await owned(db_session)
    one = item(thread_id="omt_1", mentions=frozenset({OWNER}), text="长" * 600 + SECRET)
    assert await inbox.add(
        db_session, bot_id, one, Level.IMMEDIATE, owner_open_id=OWNER, hold_until=None
    )
    await db_session.commit()
    stored = str(await raw(db_session, "SELECT summary_enc::text FROM assistant_inbox"))
    assert "enc:v2:d1:" in stored
    for plain in (SECRET, "长长", "李四"):
        assert plain not in stored
    [row] = await rows(db_session, bot_id)
    assert (row.kind, row.level, row.status) == ("message", "immediate", "pending")
    assert (row.chat_id, row.message_id, row.thread_id) == ("oc_li", "om_1", "omt_1")
    assert row.message_at == one.created_at and row.hold_until is None
    assert row.summary_enc == {
        "chat_kind": "p2p",
        "chat_name": "李四",
        "sender_id": "ou_li",
        "sender_type": "user",
        "msg_type": "text",
        "text": "长" * 500,
        "mentioned": True,
    }


async def test_the_same_message_is_stored_once(db_session: AsyncSession) -> None:
    bot_id, other = await owned(db_session), await owned(db_session)
    until = utcnow() + timedelta(minutes=5)
    first = await inbox.add(
        db_session, bot_id, item(), Level.BATCH, owner_open_id=OWNER, hold_until=until
    )
    again = await inbox.add(
        db_session, bot_id, item(text="改过的"), Level.IMMEDIATE, owner_open_id=OWNER, hold_until=None
    )
    elsewhere = await inbox.add(
        db_session, other, item(), Level.BATCH, owner_open_id=OWNER, hold_until=until
    )
    await db_session.commit()
    assert (first, again, elsewhere) == (True, False, True)
    [row] = await rows(db_session, bot_id)
    assert row.level == "batch" and row.hold_until == until
    assert SECRET in row.summary_enc["text"]  # type: ignore[index]


async def test_batch_until_is_the_earliest_open_batch(db_session: AsyncSession) -> None:
    bot_id = await owned(db_session)
    now = utcnow()
    assert await inbox.batch_until(db_session, bot_id, now) is None
    for mid, level, minutes in (
        ("om_1", Level.BATCH, 4),
        ("om_2", Level.BATCH, 2),
        ("om_3", Level.BATCH, -1),  # 已经到点、还没被取走的一批不算
        ("om_4", Level.HOLD, 1),
    ):
        await inbox.add(
            db_session,
            bot_id,
            item(mid),
            level,
            owner_open_id=OWNER,
            hold_until=now + timedelta(minutes=minutes),
        )
    assert await inbox.batch_until(db_session, bot_id, now) == now + timedelta(minutes=2)


async def test_holding_a_chat_only_pushes_later_and_spares_other_chats(
    db_session: AsyncSession,
) -> None:
    bot_id = await owned(db_session)
    now = utcnow()
    far = now + timedelta(minutes=30)
    await inbox.add(db_session, bot_id, item("om_1"), Level.IMMEDIATE, owner_open_id=OWNER, hold_until=None)
    await inbox.add(db_session, bot_id, item("om_2"), Level.HOLD, owner_open_id=OWNER, hold_until=far)
    await inbox.add(
        db_session,
        bot_id,
        item("om_3", chat_id="oc_team", chat_kind="group"),
        Level.IMMEDIATE,
        owner_open_id=OWNER,
        hold_until=None,
    )
    until = now + timedelta(minutes=10)
    assert await inbox.hold_chat(db_session, bot_id, "oc_li", until) == 2
    await db_session.commit()
    first, second, third = await rows(db_session, bot_id)
    assert (first.level, first.hold_until) == ("hold", until)
    assert (second.level, second.hold_until) == ("hold", far)
    assert (third.level, third.hold_until) == ("immediate", None)


async def test_voiding_leaves_delivered_and_in_flight_items_alone(
    db_session: AsyncSession,
) -> None:
    bot_id = await owned(db_session)
    for mid in ("om_1", "om_2", "om_3", "om_4"):
        await inbox.add(
            db_session, bot_id, item(mid), Level.IMMEDIATE, owner_open_id=OWNER, hold_until=None
        )
    await db_session.commit()
    one, two, three, four = await rows(db_session, bot_id)
    two.status = "delivered"
    three.delivered_task_id = 7  # 投递层已经把它装进一轮，正在交回
    await db_session.commit()
    assert await inbox.pending_messages(db_session, bot_id) == ["om_4", "om_1"]
    assert await inbox.pending_messages(db_session, bot_id, limit=1) == ["om_4"]
    assert await inbox.void_message(db_session, bot_id, "om_1", "recalled") == 1
    for untouched in ("om_2", "om_3", "om_x"):
        assert await inbox.void_message(db_session, bot_id, untouched, "recalled") == 0
    assert await inbox.void_all(db_session, bot_id, "stopped") == 1
    await db_session.commit()
    found = {row.message_id: (row.status, row.void_code) for row in await rows(db_session, bot_id)}
    assert found == {
        "om_1": ("void", "recalled"),
        "om_2": ("delivered", None),
        "om_3": ("pending", None),
        "om_4": ("void", "stopped"),
    }
    # 作废之后同一条消息再读到也不会重新入箱。
    assert not await inbox.add(
        db_session, bot_id, item("om_1"), Level.IMMEDIATE, owner_open_id=OWNER, hold_until=None
    )
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_watch_inbox.py -q`
Expected: FAIL，`ImportError: cannot import name 'watch_inbox' from 'coreman.core.assistants'`。

- [ ] **Step 3: 实现**

`coreman/core/assistants/watch_inbox.py`（新建）：

```python
"""盯消息的收件箱：把分好级的条目写进 `assistant_inbox`，以及压着、作废。

消息内容不落库：这里写下的只有定位信息（会话、消息、话题、时间）与加密的摘要。取走与交回
在投递层；条目一旦被它装进一轮（`delivered_task_id` 非空），这里不再改。各函数都不提交。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed
from coreman.core.assistants.watch_grade import Item, Level
from coreman.core.db.models import AssistantInbox

# 摘要里正文最多留这么多字：交回时够助理判断轻重，细节由它自己再读会话。
SUMMARY_TEXT = 500


def summary(item: Item, owner_open_id: str) -> dict[str, Any]:
    """入箱的摘要（整块加密）：谁、在哪种会话里、说了什么的开头、有没有 @ 所有者。"""
    return {
        "chat_kind": item.chat_kind,
        "chat_name": item.chat_name[:100],
        "sender_id": item.sender_id,
        "sender_type": item.sender_type,
        "msg_type": item.msg_type,
        "text": item.text[:SUMMARY_TEXT],
        "mentioned": owner_open_id in item.mentions,
    }


def _open(bot_id: uuid.UUID) -> tuple[Any, ...]:
    """这位助理还没交回、也没被投递层装进一轮的条目。"""
    return (
        AssistantInbox.bot_id == bot_id,
        AssistantInbox.status == "pending",
        AssistantInbox.delivered_task_id.is_(None),
    )


async def batch_until(
    session: AsyncSession, bot_id: uuid.UUID, now: datetime
) -> datetime | None:
    """这位助理正在攒的那一批什么时候到点；没有在攒的返回 None。"""
    found: datetime | None = await session.scalar(
        select(func.min(AssistantInbox.hold_until)).where(
            *_open(bot_id), AssistantInbox.level == Level.BATCH.value, AssistantInbox.hold_until > now
        )
    )
    return found


async def add(
    session: AsyncSession,
    bot_id: uuid.UUID,
    item: Item,
    level: Level,
    *,
    owner_open_id: str,
    hold_until: datetime | None,
) -> bool:
    """入箱；这条消息已经在箱里（不论什么状态）就什么都不做，返回 False。

    靠 `(bot_id, kind, message_id)` 的唯一索引去重：轮询重跑、两个 scheduler 交接时重叠，
    同一条消息都只有一行。
    """
    values = await sealed.seal_values(
        session,
        AssistantInbox,
        bot_id,
        {
            "bot_id": bot_id,
            "kind": "message",
            "level": level.value,
            "chat_id": item.chat_id,
            "message_id": item.message_id,
            "thread_id": item.thread_id,
            "message_at": item.created_at,
            "summary_enc": summary(item, owner_open_id),
            "hold_until": hold_until,
        },
    )
    inserted = await session.scalar(
        insert(AssistantInbox)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=["bot_id", "kind", "message_id"],
            index_where=text("message_id IS NOT NULL"),
        )
        .returning(AssistantInbox.id)
    )
    return inserted is not None


def _count(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


async def hold_chat(
    session: AsyncSession, bot_id: uuid.UUID, chat_id: str, until: datetime
) -> int:
    """所有者刚在这个会话里亲自发过言：还没交回的条目压到 `until`（只往后推，不提前）。"""
    result = await session.execute(
        update(AssistantInbox)
        .where(*_open(bot_id), AssistantInbox.kind == "message", AssistantInbox.chat_id == chat_id)
        .values(
            level=Level.HOLD.value,
            hold_until=func.greatest(func.coalesce(AssistantInbox.hold_until, until), until),
            updated_at=func.now(),
        )
    )
    return _count(result)


async def void_message(
    session: AsyncSession, bot_id: uuid.UUID, message_id: str, code: str
) -> int:
    """这条消息对应的、还没交回的条目作废（对方撤回）。"""
    result = await session.execute(
        update(AssistantInbox)
        .where(
            *_open(bot_id), AssistantInbox.kind == "message", AssistantInbox.message_id == message_id
        )
        .values(status="void", void_code=code, updated_at=func.now())
    )
    return _count(result)


async def void_all(session: AsyncSession, bot_id: uuid.UUID, code: str) -> int:
    """这位助理还没交回的条目全部作废（停用）。"""
    result = await session.execute(
        update(AssistantInbox)
        .where(*_open(bot_id))
        .values(status="void", void_code=code, updated_at=func.now())
    )
    return _count(result)


async def pending_messages(
    session: AsyncSession, bot_id: uuid.UUID, limit: int = 20
) -> list[str]:
    """还没交回的消息条目的消息 id，新的在前：每轮拿它们去查有没有被撤回。"""
    found = await session.scalars(
        select(AssistantInbox.message_id)
        .where(
            *_open(bot_id), AssistantInbox.kind == "message", AssistantInbox.message_id.is_not(None)
        )
        .order_by(AssistantInbox.id.desc())
        .limit(limit)
    )
    return [message_id for message_id in found if message_id]
```

- [ ] **Step 4: 更新台账**

`tests/unit/test_assistant_sealed_ledger.py` 的 `WRITE_STATEMENTS` 里加一条（按路径放在 `coreman/core/assistants/` 的各条之间）：

```python
    # 收件箱：入箱经 seal_values 封存摘要；压着与两种作废只改级别、状态与时间
    "coreman/core/assistants/watch_inbox.py": 4,
```

`ENTITY_SELECTS`、`COLUMN_REFERENCES`、`RAW_STATEMENTS` 不加：模块里没有取整行的 `select(AssistantInbox)`，没有 `AssistantInbox.summary_enc` 这样的语句级引用，唯一的裸 SQL 片段是去重索引的条件，不提内容列。

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_watch_inbox.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: PASS。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add coreman/core/assistants/watch_inbox.py tests/unit/test_assistant_sealed_ledger.py \
  tests/integration/test_assistant_watch_inbox.py
git commit -m "盯消息的收件箱：加密入箱、去重、压着与作废"
```

### Task 6: 轮询循环（`watch.py`）：登记、认领、门槛、一轮、错开

**Files:**
- Create: `coreman/core/assistants/watch.py`
- Modify: `tests/integration/watch_helpers.py`（导入；文件末尾追加）
- Modify: `tests/unit/test_assistant_sealed_ledger.py`（`ENTITY_SELECTS`、`WRITE_STATEMENTS`）
- Test: `tests/integration/test_assistant_watch_poll.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `AssistantWatch`；Task 2 的 `PersonalError`、测试公共件；Task 3 的 `Level` / `Item` / `Context` / `grade` / `release_at` / `HOLD_WINDOW`；Task 4 的 `watch_feed`；Task 5 的 `watch_inbox`；2A 的 `settings.get` / `identified` / `on_shift` 与 `Scope.platform`；2C 的 `followups.close_for_message` / `followups.add`；`runtime.turn_blocker(session, bot) -> str | None`；`toolbase.Call(session, cipher, scope)`；`sealed.guard_logs` / `tolerate_undecryptable` / `code_only`。
- Produces（`coreman/core/assistants/watch.py`）：
  - 常量 `BASE_INTERVAL = 60`、`MAX_INTERVAL = 3600`、`BATCH = 40`、`CONCURRENCY = 4`、`ROUND_SECONDS = 30.0`、`MAX_READS = 12`、`CHAIN_MAX = 10`、`FOCUS_MAX = 5`、`ERROR_BACKOFF_MAX = 900`。
  - `async def tick(factory: async_sessionmaker[AsyncSession], cipher: Cipher, now: datetime) -> dict[str, int]`：返回计数，键 `assistant_watch_enrolled`、`assistant_watch_polled`、`assistant_watch_items`、`assistant_watch_backoff`、`assistant_watch_failed`、`assistant_watch_keepalives`。
  - `grade`、`Level`、`Item`、`Context`（从 `watch_grade` 转出，`watch.grade(item, settings, context) -> Level`）。
  - `def phase(bot_id: uuid.UUID, interval: int = BASE_INTERVAL) -> int`、`def slot(now: datetime, interval: int, offset: int) -> datetime`。
  - `@dataclass class Outcome`：`state: str`、`items: int = 0`、`kept_alive: bool = False`、`app_id: str = ""`、`load: float = 0.0`（后两项 Task 7 用，`kept_alive` Task 8 用）。
- Produces（`tests/integration/watch_helpers.py` 追加）：`def ms(at: datetime) -> int`、`async def primed(session, who: Watched, since: datetime, *, head: list[str] | None = None) -> None`（建一行已经过了第一轮的 `assistant_watch`，立刻到期）、`async def tick(factory, at: datetime) -> dict[str, int]`、`async def watch_row(session, who: Watched) -> AssistantWatch`、`async def inbox_rows(session, who: Watched) -> list[AssistantInbox]`（内容已还原）。

本任务的 `_step` 还没有预算、限频退避（Task 7）与保活（Task 8）；`stop` 在 Task 8。

- [ ] **Step 1: 补测试公共件**

`tests/integration/watch_helpers.py`：导入里把

```python
from coreman.core.assistants import keys, sealed, settings
```

换成

```python
from coreman.core.assistants import keys, sealed, settings, watch
```

把

```python
from sqlalchemy.ext.asyncio import AsyncSession
```

换成

```python
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
```

把 `from coreman.core.db.models import Bot, FeishuPersonalGrant, PlatformApp, Task, User, UserIdentity` 换成

```python
from coreman.core.db.models import (
    AssistantInbox,
    AssistantWatch,
    Bot,
    FeishuPersonalGrant,
    PlatformApp,
    Task,
    User,
    UserIdentity,
)
```

文件末尾追加：

```python
def ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


async def primed(
    session: AsyncSession, who: Watched, since: datetime, *, head: list[str] | None = None
) -> None:
    """一行已经过了第一轮（只记基线）的盯消息状态，立刻到期；`head` 是上一轮的会话顺序。"""
    session.add(
        AssistantWatch(
            bot_id=who.bot.id,
            cursor={"since": ms(since), "chats": {}, "todo": []},
            head_enc=list(head or []),
            next_poll_at=since,
        )
    )
    await session.commit()


async def tick(factory: async_sessionmaker[AsyncSession], at: datetime) -> dict[str, int]:
    return await watch.tick(factory, CIPHER, at)


async def watch_row(session: AsyncSession, who: Watched) -> AssistantWatch:
    row = await session.get(AssistantWatch, who.bot.id, populate_existing=True)
    assert row is not None
    return row


async def inbox_rows(session: AsyncSession, who: Watched) -> list[AssistantInbox]:
    rows = list(
        await session.scalars(
            select(AssistantInbox)
            .where(AssistantInbox.bot_id == who.bot.id)
            .order_by(AssistantInbox.id)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, rows)
    return rows
```

- [ ] **Step 2: 写失败测试**

`tests/integration/test_assistant_watch_poll.py`（新建）：

```python
"""盯消息的一轮：只记基线的第一轮、分级入箱、压着、去重、逐个探测、重点群与话题、撤回，

以及什么时候不轮询、每位助理一个短事务、日志里没有内容。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import followups, settings, watch
from coreman.core.assistants.watch_grade import Context, Item, Level
from coreman.core.db.models import AssistantFollowup, Bot, FeishuPersonalGrant, RelayServer
from coreman.core.db.session import make_session_factory
from coreman.core.timeutils import utcnow
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.watch_helpers import (
    Hit,
    Watched,
    assistant,
    inbox_rows,
    install,
    ms,
    primed,
    tick,
    watch_row,
)

pytestmark = pytest.mark.usefixtures("assistant_master")
LI = {"chat_id": "oc_li", "chat_mode": "p2p"}
TEAM = {"chat_id": "oc_team", "name": "项目群", "chat_mode": "group"}
# 行被别的事务锁着时这两句会立刻报错。
GRANT_UNLOCKED = "SELECT 1 FROM feishu_personal_grants WHERE bot_id = :id FOR UPDATE NOWAIT"
WATCH_UNLOCKED = "SELECT 1 FROM assistant_watch WHERE bot_id = :id FOR UPDATE NOWAIT"


def base_time() -> datetime:
    return utcnow().replace(microsecond=0)


def after(base: datetime, seconds: int) -> datetime:
    return base + timedelta(seconds=seconds)


def test_grade_is_exported_and_polls_are_staggered() -> None:
    one = Item("oc_li", "p2p", "om_1", "ou_li", "user", 1)
    assert watch.grade(one, settings.AssistantSettings(open_id="ou_o"), Context(now=utcnow())) is (
        Level.IMMEDIATE
    )
    now = datetime(2026, 11, 16, 2, 0, 30, 500000, tzinfo=UTC)
    for offset in (0, 15, 30, 31, 59):
        at = watch.slot(now, 60, offset)
        assert now < at <= now + timedelta(seconds=60)
        assert int(at.timestamp()) % 60 == offset and at.microsecond == 0
    assert watch.phase(uuid.UUID(int=61)) == 1
    assert watch.phase(uuid.UUID(int=61), 120) == 61


async def test_new_assistants_are_enrolled_and_the_first_round_only_sets_a_baseline(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    factory = make_session_factory(db_engine)
    base = base_time()
    api.chats = [LI, TEAM]
    api.say("oc_li", "om_old", after(base, -3600))
    api.say("oc_li", "om_now", after(base, 5))
    counts = await tick(factory, base)
    assert counts["assistant_watch_enrolled"] == 1 and api.hits == []
    row = await watch_row(db_session, who)
    assert row.state == "idle" and row.cursor == {} and row.head_enc is None
    assert base < row.next_poll_at <= after(base, 60)
    first = row.next_poll_at
    counts = await tick(factory, first)
    assert counts["assistant_watch_polled"] == 1 and counts["assistant_watch_items"] == 0
    # 第一轮只列一次会话：记下顺序与开始的时刻，不读任何历史。
    assert api.paths() == ["/im/v1/chats"]
    row = await watch_row(db_session, who)
    assert row.state == "polling" and row.last_ok_at == first and row.last_error is None
    assert row.cursor == {"since": ms(first), "chats": {}, "todo": []}
    assert row.head_enc == ["oc_li", "oc_team"]
    assert first < row.next_poll_at <= after(first, 60)
    assert await inbox_rows(db_session, who) == []
    # 再登记一次不会多出一行。
    assert (await tick(factory, first))["assistant_watch_enrolled"] == 0


async def test_new_messages_are_graded_into_a_sealed_inbox(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    mine = {"chat_id": who.chat_id, "chat_mode": "p2p"}
    api.chats = [LI, TEAM, mine]
    api.say("oc_li", "om_1", after(base, 10), text=f"周五能给报价吗？{SECRET}")
    api.say("oc_team", "om_2", after(base, 11))
    api.say("oc_team", "om_3", after(base, 12), sender="cli_other", kind="app")
    api.say(who.chat_id, "om_4", after(base, 13), sender=who.open_id)
    now = after(base, 30)
    with capture_logs() as logs:
        counts = await tick(make_session_factory(db_engine), now)
    assert counts["assistant_watch_polled"] == 1 and counts["assistant_watch_items"] == 2
    # 列一次会话、读两个会话的历史、查一次待交回的条目有没有被撤回；所有者与助理自己的私聊不读。
    assert api.paths() == [
        "/im/v1/chats",
        "/im/v1/messages",
        "/im/v1/messages",
        "/im/v1/messages/mget",
    ]
    assert api.read(who.chat_id) == []
    private, group = await inbox_rows(db_session, who)
    assert (private.message_id, private.level, private.hold_until) == ("om_1", "immediate", None)
    assert private.chat_id == "oc_li" and private.status == "pending"
    assert SECRET in private.summary_enc["text"]  # type: ignore[index]
    assert (group.message_id, group.level) == ("om_2", "batch")
    assert group.hold_until == now + timedelta(minutes=5)
    assert group.summary_enc["chat_name"] == "项目群"  # type: ignore[index]
    # 库里与日志里都没有消息内容；会话顺序是密文。
    stored = str(await raw(db_session, "SELECT string_agg(summary_enc::text, '') FROM assistant_inbox"))
    head = str(await raw(db_session, "SELECT head_enc::text FROM assistant_watch"))
    assert SECRET not in stored and "报价" not in stored
    assert "oc_team" not in head and "enc:v2:d1:" in head
    assert SECRET not in json.dumps(logs, ensure_ascii=False, default=str)
    row = await watch_row(db_session, who)
    assert row.head_enc == ["oc_li", "oc_team", who.chat_id] and row.cursor["todo"] == []
    assert row.cursor["chats"]["oc_li"] == {"k": "p2p", "t": ms(after(base, 10)), "m": ["om_1"], "th": {}}
    assert row.cursor["chats"]["oc_team"]["t"] == ms(after(base, 12))
    assert who.chat_id not in row.cursor["chats"]
    assert (row.state, row.failures, row.last_ok_at) == ("polling", 0, now)


async def test_chat_kind_is_looked_up_once_when_the_list_does_not_say(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [{"chat_id": "oc_li"}]
    api.modes["oc_li"] = "p2p"
    api.say("oc_li", "om_1", after(base, 10))
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    api.say("oc_li", "om_2", after(base, 40))
    await tick(factory, after(base, 91))
    assert api.paths().count("/im/v1/chats/oc_li") == 1
    assert [row.level for row in await inbox_rows(db_session, who)] == ["immediate", "immediate"]


async def test_the_owners_own_message_is_not_boxed_and_holds_the_chat(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]
    api.say("oc_li", "om_1", after(base, 10))
    api.say("oc_li", "om_2", after(base, 20), sender=who.open_id, text=f"我来回。{SECRET}")
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    release = after(base, 20) + timedelta(minutes=10)
    [held] = await inbox_rows(db_session, who)
    # 所有者自己发的不入箱；先到的那条被压到他这次发言之后 10 分钟。
    assert (held.message_id, held.level, held.hold_until) == ("om_1", "hold", release)
    assert (await watch_row(db_session, who)).cursor["chats"]["oc_li"]["o"] == ms(after(base, 20))
    # 10 分钟内这个会话的新消息直接按「压着」入箱。
    api.say("oc_li", "om_3", after(base, 70))
    await tick(factory, after(base, 91))
    assert [(row.message_id, row.level, row.hold_until) for row in await inbox_rows(db_session, who)] == [
        ("om_1", "hold", release),
        ("om_3", "hold", release),
    ]
    # 他停下满 10 分钟之后来的，照常分级。
    api.say("oc_li", "om_4", after(base, 700))
    await tick(factory, after(base, 760))
    last = (await inbox_rows(db_session, who))[-1]
    assert (last.message_id, last.level, last.hold_until) == ("om_4", "immediate", None)


async def test_the_same_message_is_boxed_once_across_reruns_and_overlap(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]
    api.say("oc_li", "om_1", after(base, 10))
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    # 重跑：游标与顺序退回到这一轮之前（比如上一任 scheduler 的这一轮没来得及提交就没了）。
    await db_session.execute(
        text(
            "UPDATE assistant_watch SET cursor = CAST(:cursor AS jsonb), head_enc = NULL,"
            " next_poll_at = :due"
        ),
        {"cursor": json.dumps({"since": ms(base), "chats": {}, "todo": []}), "due": base},
    )
    await db_session.commit()
    counts = await tick(factory, after(base, 31))
    assert counts["assistant_watch_polled"] == 1 and counts["assistant_watch_items"] == 0
    # 换主重叠：两个 scheduler 同时认领同一位助理，只有一个轮询。
    await db_session.execute(text("UPDATE assistant_watch SET next_poll_at = :due"), {"due": base})
    await db_session.commit()
    api.hits.clear()
    both = await asyncio.gather(tick(factory, after(base, 32)), tick(factory, after(base, 32)))
    assert sum(counts["assistant_watch_polled"] for counts in both) == 1
    assert api.paths().count("/im/v1/chats") == 1
    assert [row.message_id for row in await inbox_rows(db_session, who)] == ["om_1"]


async def test_an_unchanged_order_is_probed_from_the_top_until_a_quiet_chat(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    order = ["oc_a", "oc_b", "oc_c", "oc_d"]
    await primed(db_session, who, base, head=order)
    api.chats = [{"chat_id": chat, "chat_mode": "group"} for chat in order]
    factory = make_session_factory(db_engine)
    # 什么都没发生：只探排第一的会话。
    await tick(factory, after(base, 30))
    assert api.paths() == ["/im/v1/chats", "/im/v1/messages"] and len(api.read("oc_a")) == 1
    # b 来了消息、随后 a 也来了：顺序没变。从头探到第一个没有新消息的会话（c）为止。
    api.say("oc_b", "om_1", after(base, 40))
    api.say("oc_a", "om_2", after(base, 50))
    api.hits.clear()
    counts = await tick(factory, after(base, 91))
    assert counts["assistant_watch_items"] == 2
    assert [len(api.read(chat)) for chat in order] == [1, 1, 1, 0]
    # d 来了消息排到最前：读 d，再照例探一下 a。
    api.say("oc_d", "om_3", after(base, 100))
    api.chats = [api.chats[3], *api.chats[:3]]
    api.hits.clear()
    await tick(factory, after(base, 152))
    assert [len(api.read(chat)) for chat in order] == [1, 0, 0, 1]
    # 入箱的先后就是读的先后：先探到 a 的那条，再是 b 的。
    assert [row.message_id for row in await inbox_rows(db_session, who)] == ["om_2", "om_1", "om_3"]


async def test_focus_chats_are_read_every_round_with_their_threads_and_ignored_ones_never(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    prefs = await settings.get(db_session, who.bot.id)
    prefs.focus_chats_enc, prefs.ignored_chats_enc = ["oc_focus"], ["oc_noise"]
    await db_session.commit()
    base = base_time()
    order = ["oc_noise", "oc_a", "oc_focus"]
    await primed(db_session, who, base, head=order)
    api.chats = [{"chat_id": chat, "chat_mode": "group"} for chat in order]
    api.say("oc_noise", "om_n", after(base, 5))
    api.say("oc_focus", "om_root", after(base, 10), thread_id="omt_1")
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    # 忽略的会话不读，也不打断逐个探测（oc_a 照样被探）；重点群排在后面也读，并记下话题。
    assert api.read("oc_noise") == [] and len(api.read("oc_a")) == 1
    assert len(api.read("oc_focus")) == 1 and len(api.read("omt_1")) == 1
    [root] = await inbox_rows(db_session, who)
    assert (root.message_id, root.level, root.thread_id) == ("om_root", "immediate", "omt_1")
    # 话题里的回复不出现在主线、群的排序也没变：重点群每轮直接读，照样盯到。
    api.say("oc_focus", "om_reply", after(base, 60), thread_id="omt_1", _thread_only=True)
    api.hits.clear()
    await tick(factory, after(base, 91))
    assert len(api.read("oc_focus")) == 1 and len(api.read("omt_1")) == 1
    reply = (await inbox_rows(db_session, who))[-1]
    assert (reply.message_id, reply.level, reply.thread_id) == ("om_reply", "immediate", "omt_1")
    assert (await watch_row(db_session, who)).cursor["chats"]["oc_focus"]["th"] == {
        "omt_1": ms(after(base, 60))
    }


async def test_a_recalled_message_voids_its_pending_item_and_closes_followups(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]
    sent = api.say("oc_li", "om_1", after(base, 10))
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    followup = await followups.add(
        db_session, who.bot.id, category="promised", title="回李四报价", origin_message_id="om_1"
    )
    await db_session.commit()
    sent["deleted"] = True
    await tick(factory, after(base, 91))
    [row] = await inbox_rows(db_session, who)
    assert (row.status, row.void_code) == ("void", "recalled")
    closed = await db_session.get(AssistantFollowup, followup.id, populate_existing=True)
    assert closed is not None and (closed.status, closed.close_code) == ("dropped", "origin_recalled")
    # 发出后在两轮之间就撤回的消息：读到时已经带撤回标记，不入箱。
    api.say("oc_li", "om_2", after(base, 100), deleted=True)
    await tick(factory, after(base, 152))
    assert [item.message_id for item in await inbox_rows(db_session, who)] == ["om_1"]


async def _off_shift(session: AsyncSession, who: Watched) -> None:
    (await settings.get(session, who.bot.id)).shift_override = "off"


async def _cold(session: AsyncSession, who: Watched) -> None:
    (await settings.get(session, who.bot.id)).cold_start_done_at = None


async def _paused(session: AsyncSession, who: Watched) -> None:
    relay = await session.get(RelayServer, who.bot.relay_server_id)
    assert relay is not None
    relay.assistant_pool = False


async def _disabled(session: AsyncSession, who: Watched) -> None:
    bot = await session.get(Bot, who.bot.id)
    assert bot is not None
    bot.enabled = False


async def _deprovisioning(session: AsyncSession, who: Watched) -> None:
    bot = await session.get(Bot, who.bot.id)
    assert bot is not None
    bot.assistant_state = "deprovisioning"


async def _no_identity(session: AsyncSession, who: Watched) -> None:
    (await settings.get(session, who.bot.id)).chat_id = ""


async def _revoked(session: AsyncSession, who: Watched) -> None:
    grant = await session.get(FeishuPersonalGrant, (who.bot.id, who.owner.id))
    assert grant is not None
    grant.status, grant.token_enc = "revoked", None


async def _other_identity(session: AsyncSession, who: Watched) -> None:
    (await settings.get(session, who.bot.id)).open_id = "ou_someone_else"


@pytest.mark.parametrize(
    ("change", "state", "code"),
    [
        (_off_shift, "idle", None),
        (_cold, "idle", "cold_start_pending"),
        (_paused, "paused", "runtime_not_allowed"),
        (_disabled, "idle", None),
        (_deprovisioning, "idle", None),
        (_no_identity, "idle", "identity_missing"),
        (_revoked, "backoff", "authorization_required"),
        (_other_identity, "backoff", "personal_grant_required"),
    ],
)
async def test_nothing_is_polled_when(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    change: Any,
    state: str,
    code: str | None,
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    await change(db_session, who)
    await db_session.commit()
    api.chats = [LI]
    api.say("oc_li", "om_1", after(base, 10))
    counts = await tick(make_session_factory(db_engine), after(base, 30))
    assert api.hits == [] and api.refreshes == 0
    assert counts["assistant_watch_polled"] == 0 and counts["assistant_watch_items"] == 0
    row = await watch_row(db_session, who)
    assert (row.state, row.last_error) == (state, code)
    assert row.next_poll_at > after(base, 30) and row.last_ok_at is None
    assert await inbox_rows(db_session, who) == []
    if change is _other_identity:
        # 记下的身份与授权行对不上：不调飞书，也不动授权行（`service._locked` 会清空令牌）。
        grant = await db_session.get(
            FeishuPersonalGrant, (who.bot.id, who.owner.id), populate_existing=True
        )
        assert grant is not None and grant.status == "connected" and grant.token_enc


async def test_each_assistant_is_polled_in_its_own_short_transaction(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """轮到第二位时，第一位的事务已经提交：他的授权行与盯消息状态都没有被锁着。"""
    api = install(monkeypatch)
    first, second = await assistant(db_session, "a"), await assistant(db_session, "b")
    base = base_time()
    await primed(db_session, first, base)
    await primed(db_session, second, base)
    api.chats = [LI]
    factory = make_session_factory(db_engine)
    monkeypatch.setattr(watch, "CONCURRENCY", 1)
    order: list[str] = []
    seen: list[str] = []

    async def before(hit: Hit) -> None:
        if hit.path != "/im/v1/chats":
            return
        order.append(hit.token)
        if len(order) != 2:
            return
        earlier = first if order[0] == first.token else second
        async with factory() as other:
            for locked in (GRANT_UNLOCKED, WATCH_UNLOCKED):
                await other.execute(text(locked), {"id": earlier.bot.id})
            seen.append(
                str(await raw(other, "SELECT state FROM assistant_watch WHERE bot_id = :id", id=earlier.bot.id))
            )

    api.before = before
    counts = await tick(factory, after(base, 30))
    assert counts["assistant_watch_polled"] == 2 and counts["assistant_watch_failed"] == 0
    assert sorted(order) == [first.token, second.token] and seen == ["polling"]


async def test_an_unexpected_error_pushes_one_assistant_back_and_spares_the_rest(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    first, second = await assistant(db_session, "a"), await assistant(db_session, "b")
    base = base_time()
    await primed(db_session, first, base)
    await primed(db_session, second, base)
    api.chats = [LI]

    async def before(hit: Hit) -> None:
        if hit.token == first.token:
            raise RuntimeError(f"synthetic {SECRET}")

    api.before = before
    now = after(base, 30)
    with capture_logs() as logs:
        counts = await tick(make_session_factory(db_engine), now)
    assert counts["assistant_watch_failed"] == 1 and counts["assistant_watch_polled"] == 1
    broken = await watch_row(db_session, first)
    assert (broken.state, broken.last_error, broken.failures) == ("backoff", "RuntimeError", 1)
    assert broken.next_poll_at == now + timedelta(seconds=watch.ERROR_BACKOFF_MAX)
    assert (await watch_row(db_session, second)).state == "polling"
    assert SECRET not in json.dumps(logs, ensure_ascii=False, default=str)
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_watch_poll.py -q`
Expected: FAIL，收集阶段 `ImportError: cannot import name 'watch' from 'coreman.core.assistants'`（出在 `tests/integration/watch_helpers.py` 的导入）。

- [ ] **Step 4: 实现 `watch.py`**

`coreman/core/assistants/watch.py`（新建）：

```python
"""盯消息：平台以所有者的身份轮询飞书，把别人发给他的消息分级写进收件箱。不经模型。

scheduler 的 `_assistant_watch_loop` 每 5 秒调一次 `tick`。每位助理一个自己的短事务：认领
（`FOR UPDATE SKIP LOCKED`）、过门槛、列一次会话、读有新动静的会话、入箱、记下游标与下次
轮询的时刻，然后提交——刷新令牌的锁（`feishu_personal.service.lock` 与授权行的行锁）随之
释放，不会被别人的轮询占着。

一轮里调过飞书之后不回滚：刷新令牌一次性使用，这一轮里换到的新令牌必须落库。读到一半失败
（`PersonalError`）时，已入箱的条目与已读完的会话的游标照常提交，下一轮接着来；重读由游标
与收件箱的唯一索引去重。只有意外的异常才回滚，再另开事务把这一行往后推。

消息内容只在这里的内存里：出去的只有加密的摘要（`watch_inbox`）、加密的会话顺序与游标里的
编号和时间。日志只带代码与计数。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants import followups, runtime, sealed
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants import watch_feed as feed
from coreman.core.assistants import watch_inbox as inbox
from coreman.core.assistants.records import is_assistant
from coreman.core.assistants.watch_grade import (
    HOLD_WINDOW,
    Context,
    Item,
    Level,
    grade,
    release_at,
)
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AssistantSettings,
    AssistantWatch,
    Bot,
    FeishuPersonalGrant,
    Task,
    User,
    UserIdentity,
)
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.feishu_personal.toolbase import Call
from coreman.core.logging import get_logger
from coreman.core.timeutils import aware_utc

__all__ = ["Context", "Item", "Level", "grade", "tick"]

log = get_logger(__name__)
# 轮询间隔（秒）；用量接近上限时成倍拉长，不超过 MAX_INTERVAL。
BASE_INTERVAL = 60
MAX_INTERVAL = 3600
# 一次 tick 最多认领多少位助理、同时轮询几位（scheduler 进程的连接池是 5 + 5）。
BATCH = 40
CONCURRENCY = 4
# 一轮最多读多少次历史、最多用多久。到点不取消正在进行的调用（取消一次正在进行的令牌刷新
# 会把刷新令牌用掉却拿不到新的），只是不再发起新的；没读完的会话下一轮先读。
MAX_READS = 12
ROUND_SECONDS = 30.0
# 顺序没变的会话从头逐个探，最多探这么多个。
CHAIN_MAX = 10
# 每轮直接读的重点群数。
FOCUS_MAX = 5
ERROR_BACKOFF_MAX = 900


@dataclass
class Outcome:
    """一位助理这一次的结果，只供 `tick` 汇总。"""

    state: str
    items: int = 0
    kept_alive: bool = False
    app_id: str = ""
    load: float = 0.0


def phase(bot_id: uuid.UUID, interval: int = BASE_INTERVAL) -> int:
    """这位助理在一个间隔里的相位（秒）：各人不同，轮询时刻就错开了。"""
    return bot_id.int % interval


def slot(now: datetime, interval: int, offset: int) -> datetime:
    """`now` 之后、落在相位 `offset` 上的下一个时刻（最多一个间隔之后）。"""
    wait = (offset - int(now.timestamp())) % interval
    return now.replace(microsecond=0) + timedelta(seconds=wait or interval)


def _schedule(row: AssistantWatch, now: datetime, interval: int) -> None:
    row.interval = max(BASE_INTERVAL, min(MAX_INTERVAL, interval))
    row.next_poll_at = slot(now, row.interval, phase(row.bot_id, row.interval))


def _rest(row: AssistantWatch, now: datetime, state: str, code: str | None) -> Outcome:
    """这一轮不轮询，也没出错：过一个间隔再来看。"""
    row.state, row.last_error, row.failures = state, code, 0
    _schedule(row, now, BASE_INTERVAL)
    return Outcome(state)


def _failed(row: AssistantWatch, now: datetime, code: str, wait: int | None = None) -> Outcome:
    """这一轮出了错：记下代码，按连续失败次数退避（2、4、8 分钟，最多 15 分钟）。"""
    row.failures += 1
    row.state, row.last_error = "backoff", sealed.code_only(code)
    if wait is None:
        wait = min(ERROR_BACKOFF_MAX, BASE_INTERVAL * 2 ** min(row.failures, 4))
    row.next_poll_at = now + timedelta(seconds=wait)
    return Outcome("backoff")


async def _owner_scope(
    session: AsyncSession, bot: Bot, prefs: AssistantSettings
) -> tuple[Scope | None, str]:
    """以所有者身份调飞书用的核验结果；不成立时返回 (None, 原因代码)。

    轮询循环里没有任务：`Scope.task` 照 `provisioning._grant_scope` 的做法留空，调用链
    （`Call` → `service.api_request` / `keep_alive`）不读它。身份取自开通时记下的设置，
    并照 `platform_turns.scope_for` 的做法先核对：授权行与记下的身份对不上时在这里拒绝，
    不交给 `service._locked`——它遇到不一致会清空令牌，一条写错的设置不该把授权清掉。
    """
    owner = await session.get(User, bot.owner_id, populate_existing=True) if bot.owner_id else None
    if owner is None or owner.status != "active" or owner.source == "bootstrap":
        return None, "personal_permission_revoked"
    bound = await session.scalar(
        select(UserIdentity.user_id)
        .where(
            UserIdentity.user_id == owner.id,
            UserIdentity.platform == "feishu",
            UserIdentity.platform_user_id == prefs.platform_user_id,
        )
        .limit(1)
    )
    if bound is None:
        return None, "personal_grant_required"
    grant = await session.get(FeishuPersonalGrant, (bot.id, owner.id), populate_existing=True)
    if grant is None or grant.status != "connected" or not grant.token_enc:
        return None, "authorization_required"
    if (grant.platform_user_id, grant.open_id, grant.tenant_key) != (
        prefs.platform_user_id,
        prefs.open_id,
        prefs.tenant_key,
    ):
        return None, "personal_grant_required"
    scope = Scope(
        cast(Task, None),
        bot,
        owner.id,
        prefs.tenant_key,
        grant.app_id,
        prefs.open_id,
        prefs.platform_user_id,
        prefs.chat_id,
        platform=True,
    )
    return scope, ""


async def _resting(
    session: AsyncSession, bot: Bot, prefs: AssistantSettings, now: datetime
) -> tuple[str, str | None] | None:
    """这位助理现在为什么不轮询：(状态, 代码)；该轮询返回 None。"""
    if prefs.cold_start_done_at is None:
        return "idle", "cold_start_pending"
    if not assistant_settings.on_shift(prefs, now):
        return "idle", None
    blocker = await runtime.turn_blocker(session, bot)
    return ("paused", blocker) if blocker is not None else None


@dataclass
class _Round:
    """一位助理的一轮。读到的消息只在这里的内存里。"""

    session: AsyncSession
    call: feed.Reader
    bot_id: uuid.UUID
    prefs: AssistantSettings
    now: datetime
    listed: feed.Listed
    cursor: dict[str, Any]
    deadline: float
    reads: int = 0
    items: int = 0

    def spent(self) -> bool:
        return self.reads >= MAX_READS or time.monotonic() >= self.deadline

    async def recall(self, message_id: str) -> None:
        """对方撤回了这条消息：还没交回的条目作废，由它引起的待跟进自动关闭。"""
        voided = await inbox.void_message(self.session, self.bot_id, message_id, "recalled")
        closed = await followups.close_for_message(self.session, self.bot_id, message_id)
        if voided or closed:
            log.info(
                "assistant_watch_recalled",
                bot_id=str(self.bot_id),
                voided=voided,
                followups=len(closed),
            )

    async def take(self, item: Item, entry: dict[str, Any]) -> None:
        """处理游标之后的一条消息：撤回的作废，所有者自己发的压住这个会话，其余分级入箱。"""
        owner = self.prefs.open_id
        if item.deleted:
            await self.recall(item.message_id)
            return
        if item.sender_type == "user" and item.sender_id == owner:
            entry["o"] = max(int(entry.get("o") or 0), item.at_ms)
            await inbox.hold_chat(
                self.session, self.bot_id, item.chat_id, item.created_at + HOLD_WINDOW
            )
            return
        spoke = int(entry.get("o") or 0)
        context = Context(
            now=self.now,
            owner_spoke_at=datetime.fromtimestamp(spoke / 1000, UTC) if spoke else None,
        )
        level = grade(item, self.prefs, context)
        if level is Level.IGNORE:
            return
        batch_until = (
            await inbox.batch_until(self.session, self.bot_id, self.now)
            if level is Level.BATCH
            else None
        )
        added = await inbox.add(
            self.session,
            self.bot_id,
            item,
            level,
            owner_open_id=owner,
            hold_until=release_at(level, context, batch_until=batch_until),
        )
        self.items += int(added)

    async def chat(self, chat_id: str) -> bool:
        """读一个会话游标之后的消息（主线，再加跟着的话题）；返回有没有新消息。

        游标（`t`、`m`、`th`）在整个会话读完之后才写回 `entry`：读到一半失败，这个会话下一轮
        从原来的位置重读。
        """
        entry: dict[str, Any] = self.cursor["chats"].setdefault(chat_id, {})
        last = int(entry.get("t") or self.cursor["since"])
        same = [str(one) for one in entry.get("m") or []]
        kind = entry.get("k") or self.listed.kinds.get(chat_id)
        name = self.listed.names.get(chat_id, "")
        if kind is None:
            kind, fetched = await feed.chat_kind(self.call, chat_id)
            name = name or fetched
        entry["k"] = kind
        raws, calls = await feed.read_main(self.call, chat_id, last)
        self.reads += calls
        threads = {str(key): int(value) for key, value in (entry.get("th") or {}).items()}
        new = False
        for raw in raws:
            item = feed.to_item(raw, chat_id=chat_id, chat_kind=kind, chat_name=name)
            if item is None or not feed.fresh(last, same, item):
                continue
            new = True
            if item.at_ms > last:
                last, same = item.at_ms, []
            same.append(item.message_id)
            await self.take(item, entry)
            if item.thread_id:
                threads[item.thread_id] = max(threads.get(item.thread_id, 0), item.at_ms)
        followed = sorted(threads, key=lambda key: threads[key], reverse=True)
        followed = followed[: feed.THREADS_TRACKED]
        for thread_id in followed:
            if self.spent():
                break
            seen = threads[thread_id]
            self.reads += 1
            for raw in await feed.read_thread(self.call, thread_id):
                item = feed.to_item(
                    raw, chat_id=chat_id, chat_kind=kind, chat_name=name, thread_id=thread_id
                )
                if item is None or item.at_ms <= seen:
                    continue
                new = True
                threads[thread_id] = max(threads[thread_id], item.at_ms)
                await self.take(item, entry)
        entry["t"], entry["m"] = last, same
        entry["th"] = {thread_id: threads[thread_id] for thread_id in followed}
        return new


async def _round(
    session: AsyncSession,
    call: feed.Reader,
    row: AssistantWatch,
    prefs: AssistantSettings,
    now: datetime,
) -> int:
    """轮询一轮，返回新入箱的条数。做法见 spec 第五节与实现计划「设计选择」1–11。"""
    stored = dict(row.cursor or {})
    listed = await feed.list_chats(call)
    if not isinstance(stored.get("since"), int):
        # 第一轮只记基线：从现在起盯，不把之前的历史倒进收件箱。
        row.cursor = {"since": int(now.timestamp() * 1000), "chats": {}, "todo": []}
        row.head_enc = listed.order
        return 0
    cursor = feed.load(stored)
    # 所有者与助理自己的私聊、他设为忽略的会话：不读，也不打断下面的逐个探测。
    skip = {prefs.chat_id, *(prefs.ignored_chats_enc or [])}
    focus = list(prefs.focus_chats_enc or [])[:FOCUS_MAX]
    moved, rest = feed.split(listed.order, list(row.head_enc or []))
    # 上一轮没读完的、这一轮排到前面的、重点群：都读。
    queue = [
        chat for chat in dict.fromkeys([*cursor["todo"], *moved, *focus]) if chat not in skip
    ]
    state = _Round(
        session, call, row.bot_id, prefs, now, listed, cursor, time.monotonic() + ROUND_SECONDS
    )
    todo: list[str] = []
    try:
        for chat_id in queue:
            if state.spent():
                todo.append(chat_id)
            else:
                await state.chat(chat_id)
        # 顺序没变的会话里，藏着新消息的只可能是开头连续的一段：从头探，探到没有新消息的为止。
        probes = 0
        for chat_id in rest:
            if chat_id in skip or chat_id in queue:
                continue
            if state.spent() or probes >= CHAIN_MAX:
                break
            probes += 1
            if not await state.chat(chat_id):
                break
        if time.monotonic() < state.deadline:
            waiting = await inbox.pending_messages(session, row.bot_id)
            for message_id in await feed.recalled(call, waiting):
                await state.recall(message_id)
    except PersonalError:
        # 读到一半失败：已读完的会话的进度留下，顺序不存，下一轮重新比。
        row.cursor = feed.dump(cursor, cursor["todo"])
        raise
    row.cursor = feed.dump(cursor, todo)
    row.head_enc = listed.order
    return state.items


async def _step(
    session: AsyncSession, cipher: Cipher, row: AssistantWatch, now: datetime
) -> Outcome:
    """认领到的一位助理：过门槛，轮询一轮，记下结果与下次的时刻。"""
    bot = await session.get(Bot, row.bot_id, populate_existing=True)
    if bot is None or not is_assistant(bot) or not bot.enabled or bot.assistant_state != "active":
        return _rest(row, now, "idle", None)
    prefs = await assistant_settings.get(session, bot.id)
    if not assistant_settings.identified(prefs):
        return _rest(row, now, "idle", "identity_missing")
    scope, code = await _owner_scope(session, bot, prefs)
    if scope is None:
        return _failed(row, now, code)
    resting = await _resting(session, bot, prefs, now)
    if resting is not None:
        return _rest(row, now, *resting)
    items = await _round(session, Call(session, cipher, scope), row, prefs, now)
    row.state, row.last_error, row.failures = "polling", None, 0
    row.last_ok_at = row.keepalive_at = now
    _schedule(row, now, BASE_INTERVAL)
    return Outcome("polling", items=items, app_id=scope.app_id)


async def _visit(
    factory: async_sessionmaker[AsyncSession], cipher: Cipher, bot_id: uuid.UUID, now: datetime
) -> Outcome:
    """一位助理一个事务。被别人认领着、或已经不到期的跳过。"""
    async with factory() as session:
        await sealed.guard_logs(session, bot_id)
        # 这个会话只处理这一位助理：上一轮的顺序解不开时按没有处理，不让它卡住这一行。
        sealed.tolerate_undecryptable(session)
        row = await session.scalar(
            select(AssistantWatch)
            .where(
                AssistantWatch.bot_id == bot_id,
                AssistantWatch.state != "stopped",
                AssistantWatch.next_poll_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return Outcome("skipped")
        try:
            outcome = await _step(session, cipher, row, now)
        except PersonalError as exc:
            # 不回滚：这一轮里刷新过的令牌、已入箱的条目照常提交。
            outcome = _failed(row, now, exc.code)
        row.updated_at = now
        await session.commit()
        return outcome


async def _push_back(
    factory: async_sessionmaker[AsyncSession], bot_id: uuid.UUID, code: str, now: datetime
) -> None:
    """`_visit` 意外失败（事务已回滚）：另开事务把这一行往后推，免得它每 5 秒重来一次。"""
    try:
        async with factory() as session:
            await session.execute(
                update(AssistantWatch)
                .where(AssistantWatch.bot_id == bot_id, AssistantWatch.state != "stopped")
                .values(
                    state="backoff",
                    last_error=sealed.code_only(code),
                    failures=AssistantWatch.failures + 1,
                    next_poll_at=now + timedelta(seconds=ERROR_BACKOFF_MAX),
                    updated_at=now,
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 兜底本身出错也不能让循环停下
        log.warning("assistant_watch_push_back_failed", error=type(exc).__name__)


async def _enroll(session: AsyncSession, now: datetime) -> int:
    """给启用中的 active 助理补上盯消息的行；第一次轮询落在各自的相位上。"""
    missing = list(
        await session.scalars(
            select(Bot.id)
            .outerjoin(AssistantWatch, AssistantWatch.bot_id == Bot.id)
            .where(
                Bot.kind == "assistant",
                Bot.enabled.is_(True),
                Bot.assistant_state == "active",
                AssistantWatch.bot_id.is_(None),
            )
            .limit(200)
        )
    )
    if not missing:
        return 0
    await session.execute(
        insert(AssistantWatch)
        .values(
            [
                {"bot_id": bot_id, "next_poll_at": slot(now, BASE_INTERVAL, phase(bot_id))}
                for bot_id in missing
            ]
        )
        .on_conflict_do_nothing(index_elements=[AssistantWatch.bot_id])
    )
    return len(missing)


async def tick(
    factory: async_sessionmaker[AsyncSession], cipher: Cipher, now: datetime
) -> dict[str, int]:
    """登记缺行的助理，认领到期的各轮询一次；返回这一次的计数。"""
    now = aware_utc(now)
    async with factory() as session:
        enrolled = await _enroll(session, now)
        due = list(
            await session.scalars(
                select(AssistantWatch.bot_id)
                .where(AssistantWatch.state != "stopped", AssistantWatch.next_poll_at <= now)
                .order_by(AssistantWatch.next_poll_at, AssistantWatch.bot_id)
                .limit(BATCH)
            )
        )
        await session.commit()
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(bot_id: uuid.UUID) -> Outcome:
        async with gate:
            try:
                return await _visit(factory, cipher, bot_id, now)
            except Exception as exc:  # noqa: BLE001 一位出错不挡别人；只记类型
                log.warning(
                    "assistant_watch_failed", bot_id=str(bot_id), error=type(exc).__name__
                )
                await _push_back(factory, bot_id, type(exc).__name__, now)
                return Outcome("failed")

    outcomes = await asyncio.gather(*(one(bot_id) for bot_id in due))
    counts = {
        "assistant_watch_enrolled": enrolled,
        "assistant_watch_polled": 0,
        "assistant_watch_items": 0,
        "assistant_watch_backoff": 0,
        "assistant_watch_failed": 0,
        "assistant_watch_keepalives": 0,
    }
    for outcome in outcomes:
        if outcome.state == "polling":
            counts["assistant_watch_polled"] += 1
        elif outcome.state in ("backoff", "failed"):
            counts[f"assistant_watch_{outcome.state}"] += 1
        counts["assistant_watch_items"] += outcome.items
        counts["assistant_watch_keepalives"] += int(outcome.kept_alive)
    return counts
```

- [ ] **Step 5: 更新台账**

`tests/unit/test_assistant_sealed_ledger.py`：

`ENTITY_SELECTS` 里加（按路径放在 `coreman/core/assistants/` 的各条之间）：

```python
    # 盯消息：认领一行是 session.scalar（自动还原上一轮的会话顺序）
    "coreman/core/assistants/watch.py": 1,
```

`WRITE_STATEMENTS` 里加：

```python
    # 盯消息：登记缺行的助理、意外失败后往后推，都只写调度用的列
    "coreman/core/assistants/watch.py": 2,
```

`COLUMN_REFERENCES`、`RAW_STATEMENTS` 不加：`head_enc` 只在取出的行上读写属性（经 flush 钩子封存），没有语句级引用，也没有裸 SQL。

- [ ] **Step 6: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_watch_poll.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: PASS。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 7: 提交**

```bash
git add coreman/core/assistants/watch.py tests/integration/watch_helpers.py \
  tests/unit/test_assistant_sealed_ledger.py tests/integration/test_assistant_watch_poll.py
git commit -m "盯消息的轮询循环：认领、门槛、按会话顺序读新消息并分级入箱"
```

### Task 7: 限频退避与按用量拉长间隔

**Files:**
- Modify: `coreman/core/assistants/watch.py`（导入；`_step` 的后半段；`_visit` 的 `except`；`tick` 末尾）
- Test: `tests/integration/test_assistant_watch_limits.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `ratelimit.Budget` / `stretch` / `RateLimited` / `TIGHT`；Task 6 的 `_step`、`_visit`、`tick`、`_failed`、`_schedule`、`Outcome`。
- Produces（行为）：
  - 轮询前看这个应用最近 60 秒的用量：占比到 0.6 间隔 ×2，到 0.8 间隔 ×4（写进 `assistant_watch.interval`），满了这一轮不调飞书，`state = 'backoff'`、`last_error = 'budget_exhausted'`。
  - 轮询中被限频：`state = 'backoff'`、`last_error = 'rate_limited'`、`next_poll_at = now + retry_after`；知道是哪类接口时给这个应用记一笔占满额度的用量。
  - 用量到 0.6 及以上时每次 `tick` 每个应用打一条告警日志 `assistant_watch_budget_tight`（`app_id`、`load`、`stretch`）。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_watch_limits.py`（新建）：

```python
"""盯消息与限频：被限频时按等待秒数退避、同一应用的人一起停下；用量高时拉长间隔；

读到一半失败时令牌与已读的进度不丢。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import watch
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal import ratelimit
from coreman.core.timeutils import utcnow
from tests.integration.watch_helpers import (
    APP_ID,
    assistant,
    inbox_rows,
    install,
    primed,
    tick,
    tokens,
    watch_row,
)

pytestmark = pytest.mark.usefixtures("assistant_master")
LI = {"chat_id": "oc_li", "chat_mode": "p2p"}
TEAM = {"chat_id": "oc_team", "chat_mode": "group"}


def base_time() -> datetime:
    return utcnow().replace(microsecond=0)


def after(base: datetime, seconds: int) -> datetime:
    return base + timedelta(seconds=seconds)


async def test_a_rate_limit_backs_off_and_stops_everyone_on_the_app(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    first, second = await assistant(db_session, "a"), await assistant(db_session, "b")
    base = base_time()
    await primed(db_session, first, after(base, -1))  # 先到期的先轮询
    await primed(db_session, second, base)
    api.chats = [LI]
    api.say("oc_li", "om_1", after(base, 10))
    api.limited["/im/v1/messages"] = 30
    monkeypatch.setattr(watch, "CONCURRENCY", 1)
    factory = make_session_factory(db_engine)
    now = after(base, 30)
    with capture_logs() as logs:
        counts = await tick(factory, now)
    assert counts["assistant_watch_backoff"] == 2 and counts["assistant_watch_polled"] == 0
    limited = await watch_row(db_session, first)
    assert (limited.state, limited.last_error, limited.failures) == ("backoff", "rate_limited", 1)
    assert limited.next_poll_at == now + timedelta(seconds=30) and limited.last_ok_at is None
    # 同一个应用的另一位：额度已被占满，这一轮一次飞书都不调。
    waiting = await watch_row(db_session, second)
    assert (waiting.state, waiting.last_error, waiting.failures) == ("backoff", "budget_exhausted", 0)
    assert now < waiting.next_poll_at <= now + timedelta(seconds=60)
    assert {hit.token for hit in api.hits} == {first.token}
    assert await ratelimit.Budget(APP_ID).load(db_session, now) >= 1.0
    events = [entry["event"] for entry in logs]
    assert "assistant_watch_rate_limited" in events and "assistant_watch_budget_tight" in events
    # 等待结束之后两位都恢复。
    api.limited.clear()
    counts = await tick(factory, after(now, 61))
    assert counts["assistant_watch_polled"] == 2 and counts["assistant_watch_items"] == 2
    for who in (first, second):
        row = await watch_row(db_session, who)
        assert (row.state, row.last_error, row.failures) == ("polling", None, 0)


async def test_a_token_refreshed_in_a_limited_round_is_kept(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session, expired=True)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]
    api.limited["/im/v1/messages"] = 30
    await tick(make_session_factory(db_engine), after(base, 30))
    # 列会话时换到了新令牌（旧的刷新令牌已用掉），随后读历史被限频：这一轮照常提交。
    assert api.refreshes == 1
    assert (await tokens(db_session, who))["refresh_token"] == "u-refresh-r1"
    assert (await watch_row(db_session, who)).last_error == "rate_limited"


@pytest.mark.parametrize(("units", "interval"), [(None, 60), (650, 120), (850, 240)])
async def test_a_tight_budget_stretches_the_interval(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    units: int | None,
    interval: int,
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]
    now = after(base, 30)
    if units is not None:
        await ratelimit.Budget(APP_ID).record(
            db_session, "history", at=now - timedelta(seconds=5), units=units
        )
        await db_session.commit()
    with capture_logs() as logs:
        counts = await tick(make_session_factory(db_engine), now)
    # 接近上限时照常轮询，只是下一次来得晚。
    assert counts["assistant_watch_polled"] == 1 and "/im/v1/chats" in api.paths()
    row = await watch_row(db_session, who)
    assert (row.state, row.interval) == ("polling", interval)
    assert now < row.next_poll_at <= now + timedelta(seconds=interval)
    tight = [entry for entry in logs if entry["event"] == "assistant_watch_budget_tight"]
    assert len(tight) == (0 if units is None else 1)
    if tight:
        assert tight[0]["log_level"] == "warning" and tight[0]["app_id"] == APP_ID
        assert tight[0]["stretch"] == interval // 60


async def test_a_failed_read_backs_off_and_keeps_what_was_already_read(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI, TEAM]
    api.say("oc_li", "om_1", after(base, 10))
    api.say("oc_team", "om_2", after(base, 11))
    api.failing = {"oc_team"}
    factory = make_session_factory(db_engine)
    now = after(base, 30)
    counts = await tick(factory, now)
    assert counts["assistant_watch_backoff"] == 1
    row = await watch_row(db_session, who)
    # 只留代码：飞书应答里的文字不进库。
    assert (row.state, row.last_error, row.failures) == ("backoff", "feishu_read_failed", 1)
    assert row.next_poll_at == now + timedelta(seconds=120)
    # 读完的会话的条目与游标留下了；顺序没存，下一轮重新比。
    assert [item.message_id for item in await inbox_rows(db_session, who)] == ["om_1"]
    assert row.cursor["chats"]["oc_li"]["m"] == ["om_1"] and "t" not in row.cursor["chats"]["oc_team"]
    assert row.head_enc == []
    api.failing = set()
    counts = await tick(factory, row.next_poll_at)
    assert counts["assistant_watch_polled"] == 1 and counts["assistant_watch_items"] == 1
    assert [item.message_id for item in await inbox_rows(db_session, who)] == ["om_1", "om_2"]
    assert (await watch_row(db_session, who)).failures == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_watch_limits.py -q`
Expected: 3 个用例 FAIL：`test_a_rate_limit_backs_off_and_stops_everyone_on_the_app` 里限频那位的 `next_poll_at` 是 `now + 120 秒` 而不是 `now + 30 秒`、另一位照常轮询了；`(650, 120)`、`(850, 240)` 两个用例 `interval` 仍是 60。`test_a_token_refreshed_in_a_limited_round_is_kept`、`(None, 60)` 与 `test_a_failed_read_backs_off_and_keeps_what_was_already_read` 已经 PASS（Task 6 的「不回滚」）。

- [ ] **Step 3: 实现**

`coreman/core/assistants/watch.py`：导入里把

```python
from coreman.core.feishu_personal.policy import Scope
```

换成

```python
from coreman.core.feishu_personal import ratelimit
from coreman.core.feishu_personal.policy import Scope
```

`_step` 里把

```python
    items = await _round(session, Call(session, cipher, scope), row, prefs, now)
    row.state, row.last_error, row.failures = "polling", None, 0
    row.last_ok_at = row.keepalive_at = now
    _schedule(row, now, BASE_INTERVAL)
    return Outcome("polling", items=items, app_id=scope.app_id)
```

换成

```python
    # 飞书按应用限频，全员共用：先看这个应用最近一分钟用了多少。
    budget = ratelimit.Budget(scope.app_id)
    load = await budget.load(session, now)
    factor = ratelimit.stretch(load)
    if factor is None:
        # 额度已满（或有人刚被限频）：这一轮一次都不调，过一个间隔再看。不算这位助理的失败。
        row.state, row.last_error = "backoff", "budget_exhausted"
        _schedule(row, now, BASE_INTERVAL)
        return Outcome("backoff", app_id=scope.app_id, load=load)
    try:
        items = await _round(session, Call(session, cipher, scope), row, prefs, now)
    except ratelimit.RateLimited as exc:
        if exc.category is not None:
            # 记一笔占满额度、等待结束才滑出窗口的用量：同一应用的人这段时间都不调。
            await budget.exhaust(session, exc.category, now, exc.retry_after)
        log.warning(
            "assistant_watch_rate_limited",
            bot_id=str(row.bot_id),
            category=exc.category,
            retry_after=exc.retry_after,
        )
        outcome = _failed(row, now, exc.code, wait=exc.retry_after)
        outcome.app_id, outcome.load = scope.app_id, 1.0
        return outcome
    row.state, row.last_error, row.failures = "polling", None, 0
    row.last_ok_at = row.keepalive_at = now
    # 接近上限时拉长间隔：每个人都按同一个用量算，所以是所有人一起变慢。
    _schedule(row, now, BASE_INTERVAL * factor)
    return Outcome("polling", items=items, app_id=scope.app_id, load=load)
```

`_visit` 里把

```python
        except PersonalError as exc:
            # 不回滚：这一轮里刷新过的令牌、已入箱的条目照常提交。
            outcome = _failed(row, now, exc.code)
```

换成

```python
        except PersonalError as exc:
            # 不回滚：这一轮里刷新过的令牌、已入箱的条目照常提交。刷新令牌时被限频的
            # （不属于哪一类接口）也到这里，按它给的秒数等。
            wait = exc.retry_after if isinstance(exc, ratelimit.RateLimited) else None
            outcome = _failed(row, now, exc.code, wait=wait)
```

`tick` 末尾把

```python
        counts["assistant_watch_items"] += outcome.items
        counts["assistant_watch_keepalives"] += int(outcome.kept_alive)
    return counts
```

换成

```python
        counts["assistant_watch_items"] += outcome.items
        counts["assistant_watch_keepalives"] += int(outcome.kept_alive)
    tight: dict[str, float] = {}
    for outcome in outcomes:
        if outcome.app_id and outcome.load >= ratelimit.TIGHT:
            tight[outcome.app_id] = max(tight.get(outcome.app_id, 0.0), outcome.load)
    for app_id, load in tight.items():
        # 每次 tick 每个应用一条：人数多到一个应用撑不住时，这条日志会持续出现。
        log.warning(
            "assistant_watch_budget_tight",
            app_id=app_id,
            load=round(load, 2),
            stretch=ratelimit.stretch(load) or 0,
        )
    return counts
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_watch_limits.py tests/integration/test_assistant_watch_poll.py -q`
Expected: PASS。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants/watch.py tests/integration/test_assistant_watch_limits.py
git commit -m "盯消息按应用的用量拉长间隔，被限频时按等待秒数退避"
```

### Task 8: 保活、`stop` 与停用流程

**Files:**
- Modify: `coreman/core/assistants/watch.py`（导入、`__all__`、常量、`_step` 里不轮询的分支；新增 `_keep_alive`、`stop`）
- Modify: `coreman/core/assistants/deprovisioning.py:21`（导入）、`step_drain` 开头
- Test: `tests/integration/test_assistant_watch_keepalive_stop.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `service.keep_alive(session, cipher, scope)`；Task 5 的 `inbox.void_all`；Task 6 的 `_step`、`_rest`、`Outcome.kept_alive`；`deprovisioning.request(session, bot, *, trigger, actor_id)`、`deprovisioning.step_drain(session, cipher, row, messenger)`；`tests.integration.lifecycle_helpers.FakeMessenger`。
- Produces（`coreman/core/assistants/watch.py`）：
  - `KEEPALIVE = timedelta(hours=24)`。
  - `async def stop(session: AsyncSession, bot_id: uuid.UUID) -> None`：这位助理不再轮询（`state = 'stopped'`，清掉游标与保存的顺序），还没交回的条目作废（`void_code = 'stopped'`）。幂等；调用方提交。
  - 不轮询的助理（下班、冷启动没做完、运行时暂停）距上次确认令牌还活着满 24 小时就强制刷新一次；记录不是启用中的 `active`、没记下身份、授权已失效的不保活。
- Produces（`deprovisioning.py`）：`step_drain` 在叫停任务之前先调 `watch.stop`。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_watch_keepalive_stop.py`（新建）：

```python
"""不轮询时每 24 小时把令牌续上；停用时停止轮询、作废没交回的条目。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import deprovisioning, watch
from coreman.core.assistants import watch_inbox as inbox
from coreman.core.assistants.watch_grade import Item, Level
from coreman.core.db.models import AssistantWatch, Bot, FeishuPersonalGrant
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal import service as personal
from coreman.core.timeutils import utcnow
from tests.integration.assistant_helpers import seed_assistant
from tests.integration.lifecycle_helpers import CIPHER, FakeMessenger
from tests.integration.watch_helpers import (
    Watched,
    assistant,
    inbox_rows,
    install,
    primed,
    tick,
    tokens,
    watch_row,
)

pytestmark = pytest.mark.usefixtures("assistant_master")
LI = {"chat_id": "oc_li", "chat_mode": "p2p"}


def base_time() -> datetime:
    return utcnow().replace(microsecond=0)


async def last_alive(session: AsyncSession, who: Watched, at: datetime) -> None:
    await session.execute(
        text("UPDATE assistant_watch SET keepalive_at = :at WHERE bot_id = :id"),
        {"at": at, "id": who.bot.id},
    )
    await session.commit()


@pytest.mark.parametrize("resting", [{"shift": "off"}, {"cold_started": False}])
async def test_a_resting_assistant_refreshes_its_token_once_a_day(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    resting: dict[str, Any],
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session, **resting)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]
    factory = make_session_factory(db_engine)
    # 上次确认令牌还活着是 23 小时前：不刷新。
    await last_alive(db_session, who, base - timedelta(hours=23))
    counts = await tick(factory, base)
    assert counts["assistant_watch_keepalives"] == 0 and api.refreshes == 0
    # 满 24 小时：刷新一次，不调任何业务接口，仍然不轮询。
    await last_alive(db_session, who, base - timedelta(hours=25))
    now = (await watch_row(db_session, who)).next_poll_at
    counts = await tick(factory, now)
    assert counts["assistant_watch_keepalives"] == 1 and counts["assistant_watch_polled"] == 0
    assert api.refreshes == 1 and api.hits == []
    row = await watch_row(db_session, who)
    assert row.state == "idle" and row.keepalive_at == now and row.last_ok_at is None
    assert (await tokens(db_session, who))["refresh_token"] == "u-refresh-r1"
    # 刚刷新过：下一次来看时不再刷新。
    await tick(factory, row.next_poll_at)
    assert api.refreshes == 1


async def test_a_new_row_counts_from_its_creation(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session, shift="off")
    base = base_time()
    await primed(db_session, who, base)
    factory = make_session_factory(db_engine)
    await tick(factory, base)
    assert api.refreshes == 0
    # 从没确认过（keepalive_at 为空）就从这一行建出来的时刻算起。
    await db_session.execute(
        text("UPDATE assistant_watch SET created_at = :at, next_poll_at = :due"),
        {"at": base - timedelta(hours=25), "due": base},
    )
    await db_session.commit()
    await tick(factory, base + timedelta(seconds=1))
    assert api.refreshes == 1


async def test_a_failed_keepalive_backs_off_and_keeps_the_grant(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session, shift="off")
    base = base_time()
    await primed(db_session, who, base)
    await last_alive(db_session, who, base - timedelta(hours=25))
    api.limited[personal.TOKEN_URL] = 40
    await tick(make_session_factory(db_engine), base)
    row = await watch_row(db_session, who)
    assert (row.state, row.last_error) == ("backoff", "rate_limited")
    assert row.next_poll_at == base + timedelta(seconds=40)
    assert row.keepalive_at == base - timedelta(hours=25)
    grant = await db_session.get(
        FeishuPersonalGrant, (who.bot.id, who.owner.id), populate_existing=True
    )
    assert grant is not None and grant.status == "connected"


async def test_a_disabled_record_is_not_kept_alive(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    await last_alive(db_session, who, base - timedelta(hours=25))
    bot = await db_session.get(Bot, who.bot.id)
    assert bot is not None
    bot.enabled = False
    await db_session.commit()
    await tick(make_session_factory(db_engine), base)
    assert api.refreshes == 0 and api.hits == []


def boxed(mid: str) -> Item:
    return Item("oc_li", "p2p", mid, "ou_li", "user", int(utcnow().timestamp() * 1000))


async def test_stop_ends_polling_and_voids_what_was_not_handed_over(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base, head=["oc_li"])
    for mid in ("om_1", "om_2"):
        await inbox.add(
            db_session, who.bot.id, boxed(mid), Level.IMMEDIATE, owner_open_id=who.open_id, hold_until=None
        )
    await db_session.commit()
    (await inbox_rows(db_session, who))[1].status = "delivered"
    await db_session.commit()
    await watch.stop(db_session, who.bot.id)
    await watch.stop(db_session, who.bot.id)  # 幂等
    await db_session.commit()
    row = await watch_row(db_session, who)
    assert (row.state, row.last_error, row.cursor, row.head_enc) == ("stopped", None, {}, None)
    found = [(item.status, item.void_code) for item in await inbox_rows(db_session, who)]
    assert found == [("void", "stopped"), ("delivered", None)]
    # 停了之后不再轮询，也不会被重新登记。
    api.chats = [LI]
    counts = await tick(make_session_factory(db_engine), base + timedelta(hours=30))
    assert api.hits == [] and api.refreshes == 0 and counts["assistant_watch_enrolled"] == 0
    assert (await watch_row(db_session, who)).state == "stopped"
    # 从没被盯过的助理：什么都不做，也不建行。
    other, _ = await seed_assistant(db_session)
    await watch.stop(db_session, other.id)
    await db_session.commit()
    assert await db_session.get(AssistantWatch, other.id) is None


async def test_deprovisioning_stops_the_watch_before_draining(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    who = await assistant(db_session)
    await primed(db_session, who, base_time())
    bot = await db_session.get(Bot, who.bot.id)
    assert bot is not None
    flow = await deprovisioning.request(db_session, bot, trigger="owner", actor_id=who.owner.id)
    await db_session.commit()
    assert flow is not None and flow.step == "drain"
    await deprovisioning.step_drain(db_session, CIPHER, flow, FakeMessenger())
    await db_session.commit()
    assert (await watch_row(db_session, who)).state == "stopped"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_watch_keepalive_stop.py -q`
Expected: FAIL：保活的三个测试断言 `api.refreshes` / `assistant_watch_keepalives` 为 0（或 `state` 是 `idle` 而不是 `backoff`）；`test_stop_...` 报 `AttributeError: module 'coreman.core.assistants.watch' has no attribute 'stop'`；`test_deprovisioning_...` 断言状态是 `idle`。`test_a_disabled_record_is_not_kept_alive` 已经 PASS。

- [ ] **Step 3: 实现保活与 `stop`**

`coreman/core/assistants/watch.py`：

导入里把

```python
from coreman.core.feishu_personal import ratelimit
```

换成

```python
from coreman.core.feishu_personal import ratelimit
from coreman.core.feishu_personal import service as personal
```

把

```python
from coreman.core.timeutils import aware_utc

__all__ = ["Context", "Item", "Level", "grade", "tick"]
```

换成

```python
from coreman.core.timeutils import aware_utc, utcnow

__all__ = ["Context", "Item", "Level", "grade", "stop", "tick"]
```

常量 `ERROR_BACKOFF_MAX = 900` 之后加：

```python
# 刷新令牌约 7 天不用就失效：不轮询的助理每这么久强制刷新一次。
KEEPALIVE = timedelta(hours=24)
```

`_resting` 之后加：

```python
async def _keep_alive(
    session: AsyncSession, cipher: Cipher, scope: Scope, row: AssistantWatch, now: datetime
) -> bool:
    """不轮询的这段时间里把令牌续上；距上次确认它还活着不满 `KEEPALIVE` 就什么都不做。

    轮询成功也算确认过（轮询期间访问令牌每两小时过期，自然会刷新）。从没确认过的从这一行
    建出来的时刻算起。
    """
    last = row.keepalive_at or row.created_at
    if aware_utc(last) + KEEPALIVE > now:
        return False
    await personal.keep_alive(session, cipher, scope)
    row.keepalive_at = now
    return True
```

`_step` 里把

```python
    resting = await _resting(session, bot, prefs, now)
    if resting is not None:
        return _rest(row, now, *resting)
```

换成

```python
    resting = await _resting(session, bot, prefs, now)
    if resting is not None:
        # 下班、冷启动没做完、运行时暂停：不轮询，但授权不能因此过期。
        kept = await _keep_alive(session, cipher, scope, row, now)
        outcome = _rest(row, now, *resting)
        outcome.kept_alive = kept
        return outcome
```

文件末尾追加：

```python
async def stop(session: AsyncSession, bot_id: uuid.UUID) -> None:
    """这位助理不再被盯：停用流程在叫停任务之前调用。幂等；调用方提交。

    状态改成 `stopped`（`tick` 不认领、也不会重新登记），游标与保存的会话顺序清掉，还没交回
    的条目作废。正在进行的一轮持有这一行的锁：这里等它提交，它入箱的条目随后一并作废。
    """
    row = await session.get(AssistantWatch, bot_id, with_for_update=True)
    if row is not None and row.state != "stopped":
        row.state, row.last_error, row.failures = "stopped", None, 0
        row.cursor, row.head_enc = {}, None
        row.updated_at = utcnow()
    await inbox.void_all(session, bot_id, "stopped")
```

- [ ] **Step 4: 接进停用流程**

`coreman/core/assistants/deprovisioning.py`：把

```python
from coreman.core.assistants import approvals, keys, lifecycle, retired_dirs
```

换成

```python
from coreman.core.assistants import approvals, keys, lifecycle, retired_dirs, watch
```

`step_drain` 里把

```python
    bot = await _bot(session, row)
    now = utcnow()
    if row.deadline is None:
        row.deadline = now + DRAIN_WAIT
```

换成

```python
    bot = await _bot(session, row)
    # 先停止盯消息、作废没交回的条目，再叫停任务：之后不会再有新的条目进来。
    await watch.stop(session, bot.id)
    now = utcnow()
    if row.deadline is None:
        row.deadline = now + DRAIN_WAIT
```

并把模块文档字符串的第一行

```python
"""停用个人助理：停下在跑的轮次 → 撤回待批的审批单 → 撤销飞书授权 → 销毁数据密钥并停用记录，
```

换成

```python
"""停用个人助理：停止盯消息 → 停下在跑的轮次 → 撤回待批的审批单 → 撤销飞书授权 → 销毁数据密钥并停用记录，
```

（宽限期 `grace` 里记录已经是 `deprovisioning`，`tick` 的门槛本来就不轮询它；账号恢复、宽限作罢时这一行没被 `stop` 过，照常接着盯。）

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_watch_keepalive_stop.py tests/integration/test_assistant_watch_poll.py tests/integration/test_assistant_watch_limits.py tests/integration/test_assistant_deprovisioning.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: PASS（台账不变：`stop` 用 `session.get` 取行，作废在 `watch_inbox` 里）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add coreman/core/assistants/watch.py coreman/core/assistants/deprovisioning.py \
  tests/integration/test_assistant_watch_keepalive_stop.py
git commit -m "盯消息：不轮询时每天续一次令牌，停用时停止轮询并作废没交回的条目"
```

### Task 9: scheduler 的循环与管理接口里的轮询健康状态

**Files:**
- Modify: `coreman/runtime/scheduler/service.py`（模块文档字符串末段、常量、`_start` 的循环列表、新方法 `_assistant_watch_loop`）
- Modify: `coreman/api/routers/assistants_admin.py`（模块文档字符串、导入、`list_assistants`）
- Modify: `tests/api/test_assistant_admin.py`（导入；新增一个用例）
- Test: `tests/integration/test_assistant_watch_scheduler.py`（新建）

**Interfaces:**
- Consumes: `watch.tick(factory, cipher, now) -> dict[str, int]`；`SchedulerService` 的 `_factory`、`_cipher`、`_stop`、`_sleep`、`is_leader`、`_log`；Task 1 的 `AssistantWatch`。
- Produces:
  - `coreman.runtime.scheduler.service.ASSISTANT_WATCH_SECONDS = 5.0`；`SchedulerService._assistant_watch_loop()`，后台任务名 `scheduler-assistant-watch`；只有主实例调 `watch.tick`，有非零计数时记一条 `assistant_watch` 日志。
  - `GET /api/admin/assistants` 的每条记录多一个键 `watch`：`{"state": str, "last_ok_at": str | None, "last_error": str | None}`；还没被盯过的是 `None`。不含游标、会话顺序与任何内容。

- [ ] **Step 1: 写失败测试（循环）**

`tests/integration/test_assistant_watch_scheduler.py`（新建）：

```python
"""看护进程单独一条循环盯消息：每次都出错也接着跑，不影响别的循环。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from coreman.core.assistants import watch
from coreman.runtime.scheduler import service
from coreman.runtime.scheduler.service import SchedulerService

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


async def test_the_scheduler_ticks_the_watch_in_its_own_loop_and_survives_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[datetime] = []

    async def broken(_factory: Any, _cipher: Any, now: datetime) -> dict[str, int]:
        calls.append(now)
        if len(calls) == 1:
            return {"assistant_watch_polled": 1}
        raise RuntimeError("synthetic")

    monkeypatch.setattr(watch, "tick", broken)
    monkeypatch.setattr(service, "ASSISTANT_WATCH_SECONDS", 0.05)
    svc = SchedulerService(port=0, tick_seconds=0.2, cleanup_seconds=0.5, leader_retry_seconds=0.2)
    task = asyncio.create_task(svc.run())
    try:
        for _ in range(200):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.05)
        assert svc.is_leader
    finally:
        svc.request_stop("test")
        await asyncio.wait_for(task, 10)
    assert len(calls) >= 3 and all(now.tzinfo is not None for now in calls)
    assert any(job.get_name() == "scheduler-assistant-watch" for job in svc._bg)
```

- [ ] **Step 2: 写失败测试（管理接口）**

`tests/api/test_assistant_admin.py`：导入里把

```python
from coreman.core.db.models import AssistantLifecycle, AuditLog, Bot, PlatformApp, User
```

换成

```python
from coreman.core.db.models import (
    AssistantLifecycle,
    AssistantWatch,
    AuditLog,
    Bot,
    PlatformApp,
    User,
)
```

在 `test_the_list_shows_who_where_and_what_state_only` 之后加：

```python
async def test_the_list_shows_watch_health_without_any_content(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    from datetime import UTC, datetime

    owner = await login_as(client, db_session, role="member", display_name="张三")
    bot = await _assistant(db_session, owner)
    await login_as(client, db_session, role="platform_admin")
    # 还没被盯过。
    [item] = (await client.get("/api/admin/assistants")).json()["data"]["items"]
    assert item["watch"] is None
    seen = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
    db_session.add(
        AssistantWatch(
            bot_id=bot.id,
            state="backoff",
            last_error="rate_limited",
            last_ok_at=seen,
            cursor={"since": 1, "chats": {"oc_private_chat": {"t": 2}}, "todo": []},
        )
    )
    await db_session.commit()
    response = await client.get("/api/admin/assistants")
    [item] = response.json()["data"]["items"]
    assert item["watch"] == {
        "state": "backoff",
        "last_ok_at": seen.isoformat(),
        "last_error": "rate_limited",
    }
    # 只有状态、时间与代码：读到哪了、和谁有会话都不出现。
    assert "oc_private_chat" not in response.text and "cursor" not in response.text
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_watch_scheduler.py tests/api/test_assistant_admin.py -q`
Expected: 循环的用例 FAIL，`AttributeError: <module 'coreman.runtime.scheduler.service'> has no attribute 'ASSISTANT_WATCH_SECONDS'`；管理接口的新用例 FAIL，`KeyError: 'watch'`。

- [ ] **Step 4: 加循环**

`coreman/runtime/scheduler/service.py`：

模块文档字符串最后一段末尾（「……员工规则文档每天备份一次（每 10 分钟看一眼是否到期）。」之后）加一句：

```
个人助理的开通与停用、盯消息各有一条独立的循环，每 5 秒一次：里面有对节点与飞书的网络调用，
慢了、出错了只影响它们自己。
```

把

```python
# 个人助理的开通与停用：多久推进一次（授权完成后最多这么久就开通好）。
ASSISTANT_LIFECYCLE_SECONDS = 5.0
```

换成

```python
# 个人助理的开通与停用：多久推进一次（授权完成后最多这么久就开通好）。
ASSISTANT_LIFECYCLE_SECONDS = 5.0
# 个人助理盯消息：多久认领一次到期的助理（每位助理自己的轮询间隔是 60 秒起）。
ASSISTANT_WATCH_SECONDS = 5.0
```

`_start` 的循环列表里，在

```python
                ("scheduler-assistant-lifecycle", self._assistant_lifecycle_loop()),
```

之后加一行：

```python
                ("scheduler-assistant-watch", self._assistant_watch_loop()),
```

在 `_assistant_lifecycle_loop` 方法之后加：

```python
    async def _assistant_watch_loop(self) -> None:
        """个人助理盯消息：单独一条循环，以所有者身份调飞书慢了、出错了都只影响它自己。"""
        from coreman.core.assistants import watch

        while not self._stop.is_set():
            try:
                if self.is_leader:
                    counts = await watch.tick(self._factory, self._cipher, datetime.now(UTC))
                    hits = {key: value for key, value in counts.items() if value}
                    if hits:
                        self._log.info("assistant_watch", **hits)
            except Exception as exc:  # noqa: BLE001 出错只记类型，下一轮再来
                self._log.warning("assistant_watch_failed", error=type(exc).__name__)
            await self._sleep(ASSISTANT_WATCH_SECONDS)
```

- [ ] **Step 5: 管理接口加轮询健康状态**

`coreman/api/routers/assistants_admin.py`：

模块文档字符串里把

```python
只出元数据（人、实例、节点、状态、时间、错误代码），不碰任何内容列，也不解密任何东西。停用只回
```

换成

```python
只出元数据（人、实例、节点、状态、时间、错误代码、盯消息的健康状态），不碰任何内容列，也不解密
任何东西。停用只回
```

导入里把

```python
from coreman.core.db.models import (
    AssistantLifecycle,
    AssistantRetiredDir,
    Bot,
    RelayServer,
    RuntimeNode,
    User,
)
```

换成

```python
from coreman.core.db.models import (
    AssistantLifecycle,
    AssistantRetiredDir,
    AssistantWatch,
    Bot,
    RelayServer,
    RuntimeNode,
    User,
)
```

`list_assistants` 里把

```python
    pending_dirs: dict[uuid.UUID, int] = {bot: count for bot, count in counted.all()}
```

换成

```python
    pending_dirs: dict[uuid.UUID, int] = {bot: count for bot, count in counted.all()}
    # 盯消息的健康状态：只取状态、时间与错误代码三列，游标与会话顺序不取。
    watched = await session.execute(
        select(
            AssistantWatch.bot_id,
            AssistantWatch.state,
            AssistantWatch.last_ok_at,
            AssistantWatch.last_error,
        )
    )
    health: dict[uuid.UUID, dict[str, Any]] = {
        bot: {
            "state": state,
            "last_ok_at": seen.isoformat() if seen else None,
            "last_error": error,
        }
        for bot, state, seen, error in watched.all()
    }
```

并在每条记录的字典里，把

```python
                "directories_pending_removal": int(pending_dirs.get(bot_id, 0)),
```

换成

```python
                "directories_pending_removal": int(pending_dirs.get(bot_id, 0)),
                "watch": health.get(bot_id),
```

（台账不用改：这里只取列，不取整行，也没有引用 `head_enc`。）

- [ ] **Step 6: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_watch_scheduler.py tests/api/test_assistant_admin.py tests/integration/test_assistant_deprovisioning.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: PASS。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 7: 提交**

```bash
git add coreman/runtime/scheduler/service.py coreman/api/routers/assistants_admin.py \
  tests/integration/test_assistant_watch_scheduler.py tests/api/test_assistant_admin.py
git commit -m "scheduler 新增盯消息循环，管理员的助理列表显示轮询健康状态"
```

### Task 10: 文档、CHANGELOG、spec 同步与整体验证

**Files:**
- Modify: `docs/feishu.md`（「个人助理应用」一节的列表，「平台自己发起的轮次」那一条之后）
- Modify: `docs/operations.md`（「监控与告警」一节末尾，「## 隔离演练」之前）
- Modify: `CHANGELOG.md`（`## [Unreleased]` → `### Added`）
- Modify: `docs/superpowers/specs/2026-11-11-personal-assistant.md`（第五节「轮询」）

**Interfaces:**
- Consumes: 前九个任务的行为。
- Produces: 无代码。

- [ ] **Step 1: `docs/feishu.md`**

在「个人助理应用」一节以「- **平台自己发起的轮次。**」开头的那一条之后，加一条：

```markdown
- **盯消息。** 平台以所有者的身份轮询飞书，不经模型：scheduler 单独的一条循环每 5 秒认领到期的助理，每位助理在所有者的工作时段内每 60 秒轮询一次，各人的时刻错开。只有记录启用且开通好、记下了身份、冷启动已完成、在上班时段、运行时没暂停时才轮询。一轮只列一次会话（单聊与群，按最近活跃倒序，前 50 条），与上一轮的顺序对比：排到前面的会话都读；顺序没变的从第一个开始逐个读，读到一个没有新消息的为止；所有者标记的重点群（最多 5 个）每轮直接读，连同最近的 3 个话题。所有者与助理自己的私聊、他设为忽略的会话不读。第一轮只记下顺序，不读历史。读到的消息只在内存里分级：私聊、群里 @ 本人、重点群里的消息立即交回；其他群里真人的消息、私聊里机器人发来的通知（如审批待办）每 5 分钟攒一批；所有者 10 分钟内在这个会话里亲自发过言的先压着，等他停下满 10 分钟；所有者自己发的、群里没 @ 他的机器人消息、忽略的会话不要。要交回的条目写进 `assistant_inbox`，摘要按所有者的数据密钥加密，库里另有会话、消息、话题的编号与时间；同一条消息只入箱一次。对方撤回了还没交回的消息时条目作废，由那条消息引起的待跟进自动关闭。把条目交回给助理由投递层负责。不轮询的时候（下班、暂停）平台每 24 小时刷新一次令牌，授权不会因此过期。飞书按应用限频（列会话与读历史各每分钟 1000 次，全员共用）：平台记下助理的这两类调用，用量到六成时所有人的间隔翻倍、到八成时变四倍并打告警日志 `assistant_watch_budget_tight`，满了或被飞书限频（错误码 99991400）时按应答头给的秒数退避；助理工具自己的调用只计数不拦，撞上限频时工具返回 `rate_limited` 与等待秒数。停用助理的第一步就是停止盯消息并作废没交回的条目。平台管理员在 `GET /api/admin/assistants` 的每条记录里看到 `watch`（`state`、`last_ok_at`、`last_error` 的代码）。需要数据库迁移 `0097`。已知限制：每个会话只跟最近 3 个话题；顺序没变的会话一轮最多逐个读 10 个；没记下身份的助理（迁移 `0093` 之前建的）不轮询，重新开通后恢复。
```

- [ ] **Step 2: `docs/operations.md`**

在「## 监控与告警」一节的最后一段（「数据库整体故障时……」）之后、「## 隔离演练」之前，加：

```markdown
### 个人助理盯消息排障

`GET /api/admin/assistants` 每条记录的 `watch` 是这位助理的轮询状态，scheduler 日志里对应的事件名以 `assistant_watch` 开头。日志与这个接口都只有代码、编号与计数，没有消息内容。

| `state` | `last_error` | 含义与处理 |
|---|---|---|
| `polling` | 空 | 正常，`last_ok_at` 是上一次轮询成功的时刻 |
| `idle` | 空 | 所有者下班，或记录不是启用中的开通好状态；不用处理 |
| `idle` | `cold_start_pending` | 冷启动还没做完，做完后自动开始 |
| `idle` | `identity_missing` | 没记下所有者的身份（迁移 `0093` 之前建的助理）：让所有者停用后重新开通 |
| `paused` | `runtime_offline` 等 | 运行时暂停，原因与私聊里说的一致；恢复后自动接着读 |
| `backoff` | `rate_limited` | 被飞书限频，按飞书给的秒数等；持续出现说明一个应用撑不住这么多人 |
| `backoff` | `budget_exhausted` | 这个应用最近一分钟的额度已满，下一分钟再试 |
| `backoff` | `authorization_required` | 所有者的飞书授权失效：需要他重新授权 |
| `backoff` | `personal_grant_required` / `personal_permission_revoked` | 所有者的通讯录身份或授权与开通时记下的不一致，或账号不可用：核对账号，必要时重新开通 |
| `backoff` | `feishu_read_failed` 等 | 飞书读取失败，按 2、4、8、15 分钟退避后重试；一直是它时看「需真机确认」里列会话的参数 |
| `backoff` | 异常类型名（如 `TimeoutError`） | 平台侧意外出错，15 分钟后重试；看 scheduler 日志里的 `assistant_watch_failed` |
| `stopped` | 空 | 助理已停用 |
| `null`（没有 `watch`） | | 还没被登记：记录不是启用中的开通好状态，或 scheduler 没在跑 |

日志 `assistant_watch_budget_tight`（`app_id`、`load`、`stretch`）表示这个应用最近一分钟的用量到了上限的六成以上，所有人的轮询间隔已被拉长到 `stretch` 倍；偶尔出现不用处理，持续出现需要减少重点群或再接一个助理应用。用量流水在表 `feishu_rate_usage` 里，只留 15 分钟。

收件箱只进不出（条目一直是 `pending`）说明投递层没在取，不是盯消息的问题。
```

- [ ] **Step 3: `CHANGELOG.md`**

在 `## [Unreleased]` 的 `### Added` 下最前面加一条：

```markdown
- Personal assistants watch their owner's Feishu messages. The platform polls Feishu as the owner, without a model: during the owner's working hours each assistant lists chats once a minute (direct and group chats, most recently active first), reads only the chats whose order changed or that turn out to have new messages, plus the owner's focus chats and their recent threads, and grades each message in memory as hand over now, batch for 5 minutes, hold while the owner is active in that chat, or ignore. Items to hand over are written to `assistant_inbox` with a summary encrypted with the owner's key, once per message; a message recalled before it was handed over voids its item and closes follow-ups it started. Handing items to the assistant comes with the delivery layer. Assistants that are not polling (off hours, paused runtime) refresh the owner's token every 24 hours so the authorization does not lapse. Feishu rate limits are now recognised: calls made as the owner raise `rate_limited` with the wait from the response header instead of a generic failure, a rate-limited token refresh no longer marks the authorization expired, and the watch loop counts chat-list and history calls per app, stretches everyone's interval as usage nears the per-minute limit (logging `assistant_watch_budget_tight`) and backs off when limited. `GET /api/admin/assistants` shows each assistant's watch state, last successful poll and last error code. Deprovisioning stops the watch first. Requires database migration `0097` (`assistant_watch`, `assistant_inbox`, `feishu_rate_usage`); runtime nodes need no upgrade; ordinary employees are not affected. See `docs/feishu.md` and `docs/operations.md`.
```

- [ ] **Step 4: 同步 spec**

`docs/superpowers/specs/2026-11-11-personal-assistant.md` 第五节「轮询」：

把

```markdown
平台侧进程，不经模型。新表 `assistant_watch`（`bot_id`、`cursor`、`head_enc`、`interval`、`next_poll_at`、`state`、`last_error`）。在所有者的工作时段内每 60 秒轮询一次，多个助理的轮询时刻错开。
```

换成

```markdown
平台侧进程，不经模型。新表 `assistant_watch`（`bot_id`、`cursor`、`head_enc`、`interval`、`next_poll_at`、`state`、`last_error`、`last_ok_at`、`failures`、`keepalive_at`、时间戳）。在所有者的工作时段内每 60 秒轮询一次，多个助理的轮询时刻错开。
```

把

```markdown
2. 与上一轮保存的顺序（`head_enc`）对比，排到上一轮队首之前的会话视为有新动静。
```

换成

```markdown
2. 与上一轮保存的顺序（`head_enc`）对比：排到前面的会话视为有新动静；顺序没变的会话里，藏着新消息的只可能是开头连续的一段，所以从第一个开始逐个读，读到一个没有新消息的为止。所有者标记的重点群每轮直接读。
```

把

```markdown
这样每人每分钟约 1 次列会话加少量读历史。平台对每个应用维护两类接口的分钟预算，接近上限时拉长所有人的间隔；收到限频错误时按响应头给出的等待秒数退避。单个应用承载不下时把新开通的人分到下一个应用。
```

换成

```markdown
这样每人每分钟约 1 次列会话加少量读历史（空闲时 1 次）。平台对每个应用记两类接口最近一分钟的用量（表 `feishu_rate_usage`），接近上限时拉长所有人的间隔；收到限频错误时按响应头给出的等待秒数退避。单个应用承载不下时把新开通的人分到下一个应用（未做，接近上限时只拉长间隔并告警）。
```

把

```markdown
令牌：刷新令牌一次性使用，刷新与落库在同一把行锁内完成；轮询本身保证不会超过刷新令牌的有效期。
```

换成

```markdown
令牌：刷新令牌一次性使用，刷新与落库在同一把行锁内完成；轮询本身保证不会超过刷新令牌的有效期，不轮询的时候（下班、暂停）每 24 小时强制刷新一次。
```

把

```markdown
拉到的消息只在内存里分级，交回的条目写入 `assistant_inbox`（`bot_id`、`level`、`chat_id`、`message_id`、`thread_id`、`summary_enc`、`status`、`created_at`）。
```

换成

```markdown
拉到的消息只在内存里分级，交回的条目写入 `assistant_inbox`（`bot_id`、`kind`、`level`、`chat_id`、`message_id`、`thread_id`、`message_at`、`summary_enc`、`status`、`void_code`、`hold_until`、`delivered_task_id`、时间戳），同一条消息只入箱一次。
```

并在分级表「攒批」一行的条件里，把「其他群里真人的消息；新出现的飞书审批待办」换成「其他群里真人的消息；私聊里机器人发来的通知（新出现的飞书审批待办以这种形式到达）」。

- [ ] **Step 5: 整体验证**

Run: `uv run pytest tests/unit/test_feishu_ratelimit.py tests/unit/test_assistant_watch_grade.py tests/unit/test_assistant_watch_feed.py tests/unit/test_assistant_sealed_codec.py tests/unit/test_assistant_sealed_ledger.py tests/integration/test_feishu_ratelimit_budget.py tests/integration/test_assistant_watch_schema.py tests/integration/test_assistant_watch_inbox.py tests/integration/test_assistant_watch_poll.py tests/integration/test_assistant_watch_limits.py tests/integration/test_assistant_watch_keepalive_stop.py tests/integration/test_assistant_watch_scheduler.py tests/api/test_assistant_admin.py tests/integration/test_migrations.py -q`
Expected: PASS。

Run: `uv run pytest tests -q -k "assistant or feishu_personal or scheduler"`
Expected: PASS（个人助理、个人工具与看护进程的全部用例）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman && uv run alembic check`
Expected: 全部通过；`alembic check` 输出 `No new upgrade operations detected.`

敏感词自查（在工作树根目录，有输出就改成通用表述）：按仓库本机约定的自查命令对 `git diff --cached -U0` 的新增行跑一遍。
Expected: 无输出。

- [ ] **Step 6: 提交**

```bash
git add docs/feishu.md docs/operations.md CHANGELOG.md \
  docs/superpowers/specs/2026-11-11-personal-assistant.md
git commit -m "文档：个人助理盯消息、限频与排障"
```

---

## 没覆盖到的地方

- **飞书审批待办没有专门的轮询。** 靠审批应用发来的单聊通知按「攒批」入箱（设计选择 8、需真机确认第 9 项）。通知不出现在会话列表里的话，要另加对审批待办接口的轮询，产生 `kind = approval_todo` 的条目。
- **已交回的消息之后被撤回。** 本层只查新读到的历史与还没交回的条目；已交回的消息对应的待跟进、待批的回复，要 2E 的「盯消息顺带的检查」去查。
- **非重点群里对旧消息新开的话题。** 取决于需真机确认第 4 项。
- **单个应用撑不住时分流**（裁定 8 明确不做）。

## 给后续各层的交接

- **2E（投递）**：
  - 可交回的条目：`assistant_inbox` 里 `status = 'pending' AND delivered_task_id IS NULL AND (hold_until IS NULL OR hold_until <= now)`，按 `id` 升序。取整行用 `session.scalars(select(AssistantInbox)…)` 后调 `sealed.open_rows`（并更新封存台账）；`summary_enc` 的键见 `watch_inbox.summary`：`chat_kind`、`chat_name`、`sender_id`、`sender_type`、`msg_type`、`text`（至多 500 字）、`mentioned`。发送人只有 `open_id`，名字由助理用工具查。
  - 装进一轮时给条目写 `delivered_task_id`（状态仍是 `pending`）：从这一刻起盯消息不再压着、作废这一行。轮次正常结束后改 `status = 'delivered'`；被取消或失败时把 `delivered_task_id` 清空，条目回到待交回（裁定 3）。
  - 所有者在某个会话里亲自发言会把这个会话里还没装进一轮的条目改成 `hold` 并推后（`watch_inbox.hold_chat`）；2E 不用再判断「他是不是正在这个会话里」。
  - 盯消息顺带的检查由 2E 做：撤回审批（所有者已亲自回复、对方撤回了被回复的消息）、表情挂起 4 小时的提醒、当天到期的待跟进、授权到期提醒。它们产生的条目可用 `kind = 'reminder'` 或 `approval_todo` 入箱；去重索引是 `(bot_id, kind, message_id)`（只对 `message_id` 非空的行），同一条消息的不同类型互不冲突。`watch_grade.grade` 已认 `approval_todo`（攒批）与 `approval_outcome`（立即）。
  - 所有者上次在某个会话里发言的时刻在 `assistant_watch.cursor.chats[<会话>].o`（毫秒），撤回审批可以用。
  - 被撤回的消息目前只在两处发现：新读到的历史里带撤回标记的、还没装进一轮的条目（每轮用 `im.mget` 查最近 20 条）。已交回的消息之后被撤回（关联的待跟进该关、待批的回复该撤）要 2E 另查，查到后调 `followups.close_for_message`。
  - 入队前看 `runtime.turn_blocker`；`watch` 行的 `state` 不代表能不能投递。
- **2F（上下班与冷启动）**：`watch` 只读 `settings.on_shift` 与 `cold_start_done_at`。写入 `cold_start_done_at` 之后最多一个间隔（60 秒）开始轮询，第一轮只记基线；「上班」写 `shift_override = 'on'` 后同样最多 60 秒生效，不需要动 `assistant_watch`。
- **3C（页面）**：重点群与忽略的会话写 `assistant_settings.focus_chats_enc` / `ignored_chats_enc`（会话 id 的列表）；重点群只有前 5 个每轮直接读。轮询健康状态可以直接读 `assistant_watch` 的 `state`、`last_ok_at`、`last_error`。
- **人数增长**：一个应用每分钟 1000 次读历史，空闲时每人每分钟 1 次，重点群每个每轮 1–4 次。日志 `assistant_watch_budget_tight` 持续出现时需要做裁定 8 里没做的「分到下一个应用」。

## Self-Review

**Spec 覆盖**

| 要求 | 出处 | 任务 |
|---|---|---|
| 平台侧轮询、不经模型；表 `assistant_watch` 的各列 | spec 五「轮询」；总览 2D | Task 1、6 |
| 工作时段内每 60 秒、时刻错开 | spec 五 | Task 6（`slot` / `phase`、门槛用例） |
| 每轮只列一次会话，前 50 条按最近活跃倒序；与 `head_enc` 对比；只读游标之后的历史；话题按话题读 | spec 五 1–3 | Task 4（`list_chats`、`split`、`read_main`、`read_thread`）、Task 6（`_round`、逐个探测与话题的用例） |
| 重点群每轮直接读 | 裁定 7 | Task 6（重点群用例） |
| 两类接口的分钟预算、接近上限拉长所有人的间隔、限频按等待秒数退避；预算只作用于轮询、工具只计数 | spec 五；裁定 8 | Task 2（识别、计量）、Task 7（拉长与退避） |
| `service._http` 保留响应头、识别 99991400，不把上游文本给模型 | 总览 2D | Task 2（`_http` 的三个用例、`payload`） |
| 刷新令牌一次性、刷新与落库同锁；轮询保证不过期 | spec 五 | Task 2（限频不丢令牌）、Task 7（一轮失败不回滚）、Task 8（保活） |
| 消息只在内存里分级，入箱的摘要加密 | spec 五；原则 2 | Task 3、5、6（密文断言、日志断言） |
| 分级表四级 | spec 五「分级」 | Task 3（表的每一行）、Task 6（端到端） |
| 攒批 5 分钟、压着 10 分钟 | spec 五 | Task 3（`release_at`）、Task 5（`batch_until`、`hold_chat`）、Task 6（压着用例） |
| 对方撤回：条目作废，待跟进自动关闭 | 本层范围；spec 六 | Task 5（`void_message`）、Task 6（撤回用例） |
| 只在启用、`active`、没暂停、冷启动已完成、上班时轮询 | 总览约束；2A 交接 | Task 6（`test_nothing_is_polled_when` 的八种情况） |
| 每位助理一个短事务 | 总览约束 | Task 6（短事务用例） |
| 同一条消息不重复入箱（重跑、换主重叠） | 本层范围 | Task 1（唯一索引）、Task 5、Task 6（去重用例） |
| `stop` 接进停用流程的排空之前 | spec 十「停用」；总览 2D | Task 8 |
| scheduler 循环每 5 秒 | 总览 2D | Task 9 |
| 管理员看到轮询健康状态 | spec 十一 | Task 9 |
| 新表登记：`sealed.COLUMNS`、台账、`BUSINESS_TABLES`、模型导出 | 总览约束 | Task 1（另有模型清单）、Task 5、6（台账） |
| 文档与 CHANGELOG | 总览约束 | Task 10 |

不在本层、spec 第五节里写了的：投递与会话、每日换新（2E）；待批单的锚点变化、表情挂起提醒、当天到期的待跟进、授权满 365 天前的提醒（2E）；上下班（2F）；搜索消息接口（spec 明说不用）。

**占位符扫描**：没有 TBD / TODO / 「同 Task N」；每个代码步骤都给了完整代码或确切的替换前后文本；Task 10 的敏感词自查引用的是仓库本机约定的命令（不能写进公开仓的文档）。

**类型与名字一致性**

- `Level` / `Item` / `Context` / `grade` / `release_at`：Task 3 定义，Task 5（`add(..., level: Level, ...)`）、Task 6（`_Round.take`、`watch.grade` 转出）按同一签名使用。`Item` 的必填字段顺序 `(chat_id, chat_kind, message_id, sender_id, sender_type, at_ms)` 在 Task 6、8 的用例里按位置构造。
- `feed.Listed(order, names, kinds)`、`split(order, prev)`、`to_item(raw, *, chat_id, chat_kind, chat_name, thread_id=None)`、`fresh(last, same, item)`、`read_main -> (items, calls)`、`read_thread`、`chat_kind -> (kind, name)`、`recalled`、`load`、`dump(cursor, todo)`：Task 4 定义，Task 6 的 `_round` / `_Round.chat` 用法一致。
- `inbox.add(session, bot_id, item, level, *, owner_open_id, hold_until) -> bool`、`batch_until`、`hold_chat(session, bot_id, chat_id, until)`、`void_message(session, bot_id, message_id, code)`、`void_all(session, bot_id, code)`、`pending_messages`：Task 5 定义，Task 6、8 用法一致。
- `ratelimit.Budget(app_id).record / used / load / exhaust`、`stretch`、`RateLimited(retry_after, category=None)`、`TIGHT`：Task 2 定义，Task 7 用法一致；`service.keep_alive(session, cipher, scope)` 在 Task 8 使用。
- `Outcome(state, items, kept_alive, app_id, load)`：Task 6 定义全部字段，Task 7 填 `app_id` / `load`，Task 8 填 `kept_alive`；`tick` 的六个计数键从 Task 6 起不变。
- 游标的键 `since` / `chats` / `todo` 与每个会话的 `t` / `m` / `k` / `o` / `th`：模型文档字符串（Task 1）、`feed.load` / `dump`（Task 4）、`_Round.chat` / `take`（Task 6）、测试公共件 `primed`（Task 6）一致。
- 台账计数：`watch_inbox.py` 写语句 4（1 条 `insert`、3 条 `update`）；`watch.py` 取整行 1（`_visit` 的认领）、写语句 2（`_enroll` 的 `insert`、`_push_back` 的 `update`）。Task 7、8、9 不改变这些数。
- 测试公共件：`assistant` / `scope_of` / `tokens` / `msg` / `Hit` / `FakeFeishuApi` / `install`（Task 2），`ms` / `primed` / `tick` / `watch_row` / `inbox_rows`（Task 6）；后面的任务只导入这些名字。

**依赖的假定（开工前核对）**

- 2A 已落地：`Scope` 有 `platform` 字段且可按关键字传；`settings.get` 对已有行不改内容；`settings.identified` 要求身份四列都非空；`AssistantSettings` 在 `sealed.COLUMNS` 里（`focus_chats_enc` / `ignored_chats_enc` 取出来是列表或 `None`）。
- 2C 已落地：`followups.add` 的关键字参数与 `close_for_message` 的返回值如其计划；`AssistantFollowup` 有 `status`、`close_code`，撤回自动关闭写的是 `dropped` / `origin_recalled`。
- 迁移头是 `0096`。不是的话（2B、2C 的迁移号有变）相应调整 `down_revision`。
