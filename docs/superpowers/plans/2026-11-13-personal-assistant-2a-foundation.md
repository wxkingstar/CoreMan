# 个人助理 2A：基础层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给第 2、3 期的各层打好地基：助理设置表（开通时记下的所有者身份、时区与工作时段、重点群与忽略的会话）、不带入站事件也能拿到个人工具的「平台来源的轮次」、新的任务类型、助理自有工具的分组注册表与 MCP 上的分派位置、worker 的平台轮次处理器基类。本层做完后没有任何新的用户可见功能；普通员工（`kind='standard'`）的行为不变。

**Architecture:** 身份放在新表 `assistant_settings`（主键 `bot_id`）：授权完成、记录变成 `active` 的那个事务里，从开通流程记下的飞书操作人与开通卡片所在的私聊写入；已开通的助理由迁移从最近一条完成的开通流程回填。平台自己发起的一轮只能经 `platform_turns.enqueue` 入队（台账式测试扫描 `coreman/` 保证），任务不带入站事件；`policy.task_scope` 对这样的任务改走 `platform_turns.scope_for`，由设置表里的身份、所有者的通讯录身份与授权行拼出 `Scope`，任何一项对不上都拒绝。worker 侧的 `PlatformTurnHandler` 复用整条 chat 流水线：发言者固定为所有者，入站事件用一个不落库的替身（只带会话坐标）。助理自有工具登记在 `coreman/core/assistants/tools.py` 的三组里（`knowledge` / `approval` / `jobs`），MCP 路由按组分派，`knowledge` 组排在授权代数判断之前。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy async、Alembic、PostgreSQL 16；pytest（pytest-asyncio 自动模式、httpx、respx、替身）。

**Spec:** `docs/superpowers/specs/2026-11-11-personal-assistant.md`（「原则」「威胁模型」「四、审批闸门」「五、盯消息 → 投递与会话、上下班」「七、子任务」「十、开通」）；分层总览 `docs/superpowers/plans/2026-11-13-personal-assistant-phase2-3.md`（「分层」2A 一行、「裁定」第 1、2 条、「层间接口 → 2A」、「每层都要守的约束」）。

## Global Constraints

- 集成分支 `claude/personal-assistant-phase2-3`，本层的提交依次落在这条分支上。迁移头是 `0093`，本层的迁移写作 `0094_assistant_settings`、`down_revision = "0093"`。
- 层间接口的名字原样使用总览里的：表 `assistant_settings` 及其列名；`settings.get` / `local_today` / `on_shift`；`HANDOVER_KIND = "assistant_handover"`、`JOB_RESULT_KIND = "assistant_job_result"`、`COMPILE_KIND = "assistant_compile"`、`SUBTASK_KIND = "assistant_subtask"`、`BOT_HELP_KIND = "assistant_bot_help"`、`PLATFORM_TURN_KINDS`；`platform_turns.enqueue(session, bot, kind, payload, *, session_key=None, dedupe_key)`、`platform_turns.scope_for(session, bot, task)`；`tools.register(group, definitions, dispatch)`、`tools.definitions(base, *, groups)`；`PlatformTurnHandler(ChatTaskHandler)`。要改名先改总览。
- 公开仓：代码、测试、文档、提交信息只用通用占位（`example.com`、`demo-bot`、`cli_example`、张三 / 李四），不出现任何公司名、内部域名、真实人名、本机绝对路径；提交前跑仓库约定的敏感词自查。
- 普通员工（`kind='standard'`）的行为不变：共用路径上的改动只多一次对已加载对象的属性判断（`task.inbound_event_id is None`、`is_assistant(bot)`），不加查询、不加等待、不加锁。
- 失败一律不放行（fail closed）：身份缺任何一项、所有者账号停用、通讯录身份或授权行与记下的身份对不上、记录不是 `active`、会话键不属于这位助理，平台来源的轮次都拿不到 scope，也入不了队。
- 平台来源的任务类型只能由平台代码经 `platform_turns.enqueue` 创建；网关入库只产生 `chat`、`command`、`card_action`。
- 新的内容列（`focus_chats_enc`、`ignored_chats_enc`）登记进 `sealed.COLUMNS`；新表登记进 `coreman/core/db/models/__init__.py`、`tests/conftest.py` 的 `BUSINESS_TABLES`；新增的写入口登记进 `tests/unit/test_assistant_sealed_ledger.py`。
- 日志只带代码、编号与异常类型名，不带消息内容。
- 代码风格：`ruff` 行宽 100，`mypy --strict`；注释与文档字符串用中文，写清「为什么」。
- 测试命令：`uv run pytest <路径> -q`。`tests/integration`、`tests/api` 下的用例要数据库：本机有 Docker 时直接跑（testcontainers 自动起 `postgres:16`）；没有 Docker 时先指定一个专用的测试库，例如 `export TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/coreman_test`（见 `docs/development.md`「验证」；不要指向生产库，用例会清空表）。`tests/unit` 下的用例不需要数据库。
- 每个任务结束时另跑：`uv run ruff check .`、`uv run ruff format --check .`、`uv run mypy coreman`。
- 本层不做：知识库与待跟进的表和工具（2B、2C）、盯消息与限频（2D）、交回轮次与每日换新会话（2E）、上下班命令与冷启动（2F）、子任务与调度（3A、3B）、页面与设置项表单（3C）。本层只留它们要用的入口。

## 设计选择

**1. 身份在授权完成的那一步写入，不在点「开通」时写。** `assistant_settings.bot_id` 是外键，点开通（`accept_offer`）时记录还没建。`step_await_grant` 把记录置为 `active` 的同一个事务里调 `settings.record_identity`：`open_id`、`tenant_key` 是点开通时飞书给的操作人（`assistant_lifecycles.open_id` / `tenant_key`），`platform_user_id` 是网关按通讯录认出的飞书用户编号，`chat_id` 是开通卡片所在的私聊（`offer_chat_id`，授权卡片也发在这个私聊里）。四样都不来自任何请求内容。

**2. 平台来源的判定放在 `policy.task_scope`，不只放在 `capability_scope`。** worker 开轮时调的是 `task_scope`（`coreman/runtime/worker/chat/personal.py` 的 `_scope`），MCP 入口调的 `capability_scope` 也转到它。两处走同一条判定，开轮时签出的能力令牌才对得上入口的复核。判据是「任务没有入站事件，且类型属于 `PLATFORM_TURN_KINDS` 或是审批结果回合」。

**3. `Scope` 多一个 `platform` 标记。** 现有的 `Scope.scheduled` 等于「没有入站事件」，MCP 入口据此把轮次当成定时任务：不认会话、不给授权工具。平台来源的一轮没有入站事件，却和私聊一轮一样跑在助理会话里、能力令牌带着会话编号。所以 `scheduled` 改为「没有入站事件且不是平台来源」；`execute.owner_scope`、`provisioning._grant_scope` 这两处手工拼的 `Scope` 不带标记，行为不变。

**4. 授权行与记下的身份对不上就拒绝，不去动授权行。** `feishu_personal.service._locked` 发现授权行的身份与 `Scope` 不一致时会清空令牌。平台来源的 `Scope` 来自设置表，如果直接放行，一条写错的设置会把所有者的授权清掉。`scope_for` 先比对（飞书用户编号、`open_id`、`tenant_key`），不一致抛 `ValueError`，到不了 `_locked`。

**5. worker 用不落库的替身代替入站事件。** chat 流水线里约 20 处读 `intake.inbound`（回复上下文、事件编号、是否卡片点击）。给每一处加空值分支改动面太大；合成一条真的 `inbound_events` 行又会让「平台来源的任务没有入站事件」这条判据失效。做法：`PlatformTurnHandler` 构造一个不加入会话的 `InboundEvent` 对象，只带 `chat_id`、`chat_type='single'` 与所有者的两个飞书编号，`reply_context` 只有会话坐标，没有被回复的消息，回复作为一条新消息发到所有者的私聊。它的 `id` 是空值，流水线里用到事件编号的地方（云代理、个人凭证的能力令牌）本来就接受空值。

**6. 审批结果回合成为 `PlatformTurnHandler` 的子类。** 两者的入站解析只差两点：消息文本从哪来、有没有真实的入站事件。合并后，带入站事件的结果回合行为逐项不变（错误代码、暂停时照旧回一句），`origin_event_id` 为空的结果回合走替身。

**7. 平台来源的轮次在暂停时不私信。** 所有者发来消息时助理暂停，回一句是对的；平台自己发起的一轮没有人在等，`PlatformTurnHandler` 在没有入站事件时先看 `runtime.turn_blocker`，被挡住就只把任务记为失败（`assistant_paused`），不发消息。

**8. 工具注册表在自己的模块里登记内置的组。** `coreman/core/assistants/tools.py` 文件末尾 `register("approval", …)`；2B、2C、3A 在同一处各加一行。不靠「某个模块被导入时顺带登记」：那样清单取决于导入顺序。没有 `approval` 组的轮次（子任务）拿到的个人工具只有读类：写类工具不列出，调用也不受理。

**9. `PLATFORM_TURN_KINDS` 的成员。** 总览写「前四个加 `SUBTASK_KIND`」，五个常量里前四个已含 `SUBTASK_KIND`。本计划取 `(HANDOVER_KIND, JOB_RESULT_KIND, COMPILE_KIND, SUBTASK_KIND)`；`BOT_HELP_KIND` 跑在目标员工那边，不是助理自己的轮次，不在其中（3B 定它的来源校验）。

**10. 工作时段不跨午夜。** `work_start < work_end` 由库里的检查约束保证（`HH:MM` 文本按字典序比较即可）。跨午夜的班次要按「班次开始那天是不是工作日」判断，没有需求，不做。时区名库里校验不了：`settings.zone` 遇到认不出的名字退回 `Asia/Shanghai`，轮询循环不会因为一条写坏的设置抛错。

## 已知限制

- 本功能上线前（迁移 `0093` 之前）建的助理记录没有开通流程可回填，设置表里没有身份：这些助理照常响应私聊，但平台来源的轮次入不了队，重新开通后恢复。
- 平台来源的轮次里触发的飞书授权卡片（`feishu_authorize`）点完不会自动续接：续接要沿用来源那一轮的入站事件（`coreman/core/feishu_docs/resume.py` 对没有入站事件的来源直接按过期处理）。所有者授权后，下一次交回或他自己发消息时继续。
- 平台来源的轮次里发出的「连接飞书」选择卡片、定时任务确认卡片不可用（卡片回调按来源任务的入站事件核验）；助理本来就不支持定时任务，「连接飞书」只由所有者发消息触发。

## File Structure

| 文件 | 职责 |
|---|---|
| `migrations/versions/0094_assistant_settings.py`（新） | 建表 `assistant_settings`，从最近一条完成的开通流程回填身份 |
| `coreman/core/db/models/assistants.py`、`__init__.py` | 模型 `AssistantSettings`、常量 `SHIFT_OVERRIDES` |
| `coreman/core/db/models/bus.py` | `TASK_KINDS` 加五个新类型 |
| `coreman/core/assistants/sealed.py` | `COLUMNS` 登记两列会话名单 |
| `coreman/core/assistants/settings.py`（新） | `get`、`record_identity`、`identified`、`zone`、`local_today`、`on_shift` |
| `coreman/core/assistants/provisioning.py` | 授权完成时写入身份 |
| `coreman/core/assistants/kinds.py` | 新任务类型常量与 `PLATFORM_TURN_KINDS` |
| `coreman/core/assistants/platform_turns.py`（新） | `enqueue`（唯一入口）、`scope_for`、`is_platform`、`owns_session` |
| `coreman/core/feishu_personal/policy.py` | `Scope.platform`；`PRIVATE_TURN_KINDS` 并入新类型；`task_scope` 对平台来源改走 `scope_for` |
| `coreman/runtime/worker/platform_turn.py`（新） | `PlatformTurnHandler`、替身 `stand_in` |
| `coreman/runtime/worker/assistant_approvals.py` | `AssistantApprovalResultHandler` 改为 `PlatformTurnHandler` 的子类 |
| `coreman/core/assistants/approvals/service.py` | `deliver` 在 `origin_event_id` 为空时经 `platform_turns.enqueue` 入队 |
| `coreman/core/assistants/tools.py`（新） | 助理自有工具的总注册表与分派 |
| `coreman/core/assistants/approvals/tools.py` | 只留审批组自己的工具与写类工具的暂存样式 |
| `coreman/api/routers/feishu_personal.py` | MCP 入口按组列出与分派，`knowledge` 组在授权代数判断之前 |
| `tests/conftest.py`、`tests/integration/assistant_helpers.py` | `BUSINESS_TABLES` 登记；造身份的 helper `identified` |
| `tests/unit/test_assistant_sealed_ledger.py` | 两处新的写入口 |
| `tests/unit/test_assistant_platform_turn_ledger.py`（新） | 台账：平台来源的任务只在 `platform_turns.enqueue` 创建 |
| `docs/feishu.md`、`CHANGELOG.md` | 说明与变更记录 |

---

### Task 1: 表 `assistant_settings`、迁移 `0094` 与封存登记

**Files:**
- Create: `migrations/versions/0094_assistant_settings.py`
- Modify: `coreman/core/db/models/assistants.py`（文件末尾追加；顶部导入）
- Modify: `coreman/core/db/models/__init__.py:24-33`（导入）、`:290-300`（`__all__`）
- Modify: `coreman/core/assistants/sealed.py:47-60`（导入）、`:145-190`（`COLUMNS`）
- Modify: `tests/conftest.py:24-30`（`BUSINESS_TABLES`）
- Test: `tests/integration/test_assistant_settings_schema.py`（新建）、`tests/integration/test_migration_assistant_settings.py`（新建）

**Interfaces:**
- Consumes: `coreman.core.db.base.Base`、`enum_check`；`sealed.Whole`；`tests.integration.assistant_helpers.seed_assistant` / `raw` / `SECRET`；fixture `assistant_master`（`tests/integration/conftest.py`）、`db_session`、`db_engine`、`migrated_database`（`tests/conftest.py`）。
- Produces:
  - 模型 `coreman.core.db.models.AssistantSettings`，列：`bot_id: uuid.UUID`（主键，外键 `bots.id`，随记录删除）、`open_id: str`、`tenant_key: str`、`platform_user_id: str`、`chat_id: str`（都默认空串）、`timezone: str`（默认 `Asia/Shanghai`）、`work_start: str`（`09:30`）、`work_end: str`（`19:00`）、`workdays: list[int]`（`[1,2,3,4,5]`）、`focus_chats_enc: list[str] | None`、`ignored_chats_enc: list[str] | None`、`daily_turn_budget: int | None`、`shift_override: str | None`（`on` / `off`）、`shift_override_until: datetime | None`、`cold_start_done_at: datetime | None`、`updated_at: datetime`。
  - 常量 `coreman.core.db.models.SHIFT_OVERRIDES = ("on", "off")`。
  - `focus_chats_enc`、`ignored_chats_enc` 经 ORM 写入时自动封存，读不出来时是 `[]`；没设置过时是 `None`。读的一方统一写 `list(row.focus_chats_enc or [])`。

- [ ] **Step 1: 写失败测试（表结构与封存）**

`tests/integration/test_assistant_settings_schema.py`（新建）：

```python
"""assistant_settings：默认值、约束、随记录删除，以及两列会话名单在库里是密文。"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import SHIFT_OVERRIDES, AssistantSettings
from coreman.core.db.session import make_session_factory
from tests.conftest import BUSINESS_TABLES
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
STORED = (
    "SELECT coalesce(focus_chats_enc::text, '') || coalesce(ignored_chats_enc::text, '')"
    " FROM assistant_settings WHERE bot_id = :id"
)


def test_table_is_truncated_before_bots() -> None:
    assert BUSINESS_TABLES.index("assistant_settings") < BUSINESS_TABLES.index("bots")
    assert SHIFT_OVERRIDES == ("on", "off")


async def test_defaults_and_removal_with_the_record(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    db_session.add(AssistantSettings(bot_id=bot.id))
    await db_session.commit()
    row = await db_session.get(AssistantSettings, bot.id, populate_existing=True)
    assert row is not None
    assert (row.open_id, row.tenant_key, row.platform_user_id, row.chat_id) == ("", "", "", "")
    assert (row.timezone, row.work_start, row.work_end) == ("Asia/Shanghai", "09:30", "19:00")
    assert row.workdays == [1, 2, 3, 4, 5]
    assert row.focus_chats_enc is None and row.ignored_chats_enc is None
    assert row.daily_turn_budget is None and row.shift_override is None
    assert row.shift_override_until is None and row.cold_start_done_at is None
    assert row.updated_at is not None
    await db_session.execute(text("DELETE FROM bots WHERE id = :id"), {"id": bot.id})
    await db_session.commit()
    left = await raw(db_session, "SELECT count(*) FROM assistant_settings")
    assert left == 0


@pytest.mark.parametrize(
    "bad",
    [
        {"work_start": "9:30"},
        {"work_end": "24:00"},
        {"work_start": "19:00", "work_end": "09:30"},
        {"work_start": "10:00", "work_end": "10:00"},
        {"workdays": [0]},
        {"workdays": [1, 8]},
        {"shift_override": "maybe"},
        {"daily_turn_budget": 5},
        {"daily_turn_budget": 2001},
    ],
)
async def test_values_are_constrained(db_session: AsyncSession, bad: dict[str, object]) -> None:
    bot, _ = await seed_assistant(db_session)
    db_session.add(AssistantSettings(bot_id=bot.id, **bad))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_chat_lists_are_sealed_and_read_back(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    row = AssistantSettings(
        bot_id=bot.id, focus_chats_enc=[f"oc_{SECRET}"], ignored_chats_enc=["oc_noise"]
    )
    db_session.add(row)
    await db_session.commit()
    stored = str(await raw(db_session, STORED, id=bot.id))
    assert SECRET not in stored and "oc_noise" not in stored
    assert stored.count("enc:v2:d1:") == 2
    async with make_session_factory(db_engine)() as other:
        found = await other.get(AssistantSettings, bot.id)
        assert found is not None
        assert found.focus_chats_enc == [f"oc_{SECRET}"]
        assert found.ignored_chats_enc == ["oc_noise"]
        # 密钥销毁之后：行还在、其余列可读，名单按空列表处理，不抛错。
        await keys.destroy_key(other, owner.id)
        await other.commit()
        locked = await other.get(AssistantSettings, bot.id, populate_existing=True)
        assert locked is not None and sealed.is_locked(locked)
        assert locked.focus_chats_enc == [] and locked.ignored_chats_enc == []
        assert locked.timezone == "Asia/Shanghai"
```

- [ ] **Step 2: 写失败测试（迁移回填）**

`tests/integration/test_migration_assistant_settings.py`（新建）：

```python
"""迁移 0094：已开通的助理从最近一条完成的开通流程回填身份；没有流程可依的不建行。"""

from __future__ import annotations

import asyncio

from alembic import command

from tests.conftest import alembic_config
from tests.integration.test_migration_native_model_names import _execute, _rows

USERS = (
    "INSERT INTO users (login_name, display_name) VALUES"
    " ('mig-zhang', '张三'), ('mig-li', '李四'), ('mig-wang', '王五')",
    {},
)
APP = (
    "INSERT INTO platform_apps (platform, name, capabilities, app_id, secret_enc)"
    " VALUES ('feishu', 'mig-assistant-app', ARRAY['assistant'], 'cli_example', 'x')",
    {},
)
BOT = (
    "INSERT INTO bots (bot_key, platform, name, created_by, owner_id, platform_app_id, kind,"
    " assistant_state, model, working_dir, credentials_enc)"
    " SELECT :key, 'feishu', '个人助理', u.id, u.id, a.id, 'assistant', 'active', 'm', '/w', ''"
    " FROM users u, platform_apps a WHERE u.login_name = :login AND a.name = 'mig-assistant-app'"
)
FLOW = (
    "INSERT INTO assistant_lifecycles (id, owner_id, platform_app_id, bot_id, action, step,"
    " outcome, platform_user_id, open_id, tenant_key, offer_chat_id, finished_at)"
    " SELECT gen_random_uuid(), b.owner_id, b.platform_app_id, b.id, 'provision', 'await_grant',"
    " :outcome, :pid, :open_id, 'tenant-example', :chat,"
    " now() - make_interval(hours => CAST(:age AS integer))"
    " FROM bots b WHERE b.bot_key = :key"
)
SETTINGS = (
    "SELECT b.bot_key, s.open_id, s.tenant_key, s.platform_user_id, s.chat_id, s.timezone,"
    " s.work_start, s.work_end, s.workdays"
    " FROM assistant_settings s JOIN bots b ON b.id = s.bot_id ORDER BY b.bot_key"
)


def _flow(key: str, outcome: str, pid: str, open_id: str, chat: str, age: int) -> tuple:
    return FLOW, {
        "key": key,
        "outcome": outcome,
        "pid": pid,
        "open_id": open_id,
        "chat": chat,
        "age": age,
    }


def test_0094_backfills_identity_from_the_latest_finished_provisioning(
    migrated_database: str,
) -> None:
    cfg = alembic_config(migrated_database)
    command.downgrade(cfg, "0093")
    try:
        asyncio.run(
            _execute(
                migrated_database,
                USERS,
                APP,
                (BOT, {"key": "mig-a", "login": "mig-zhang"}),
                (BOT, {"key": "mig-b", "login": "mig-li"}),
                (BOT, {"key": "mig-c", "login": "mig-wang"}),
                # 张三：两条完成的开通流程，取最近结束的那条。
                _flow("mig-a", "done", "zhangsan", "ou_old", "oc_old", 5),
                _flow("mig-a", "done", "zhangsan", "ou_zhang", "oc_zhang", 1),
                # 李四：只有一条失败的流程，不回填。
                _flow("mig-b", "failed", "lisi", "ou_li", "oc_li", 1),
                # 王五：本功能上线前建的记录，没有开通流程。
            )
        )
        command.upgrade(cfg, "0094")
        rows = asyncio.run(_rows(migrated_database, SETTINGS))
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(
            _execute(migrated_database, ("TRUNCATE bots, users, platform_apps CASCADE", {}))
        )
    assert rows == [
        (
            "mig-a",
            "ou_zhang",
            "tenant-example",
            "zhangsan",
            "oc_zhang",
            "Asia/Shanghai",
            "09:30",
            "19:00",
            [1, 2, 3, 4, 5],
        )
    ]
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_settings_schema.py tests/integration/test_migration_assistant_settings.py -q`
Expected: FAIL：收集阶段 `ImportError: cannot import name 'SHIFT_OVERRIDES' from 'coreman.core.db.models'`；迁移用例报 `Can't locate revision identified by '0094'`。

- [ ] **Step 4: 写迁移**

`migrations/versions/0094_assistant_settings.py`（新建）：

```python
"""Personal assistant settings: owner identity for platform-origin turns, hours, watch lists."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None

HHMM = "'^([01][0-9]|2[0-3]):[0-5][0-9]$'"
# 已开通的助理：身份取自最近一条完成的开通流程（点开通时飞书给的操作人、开通卡片所在的私聊）。
# 没有这样一条流程的记录（本功能上线前建的）不建行，平台来源的轮次对它们不可用。
BACKFILL = """
INSERT INTO assistant_settings (bot_id, open_id, tenant_key, platform_user_id, chat_id)
SELECT DISTINCT ON (flow.bot_id)
       flow.bot_id, flow.open_id, flow.tenant_key, flow.platform_user_id, flow.offer_chat_id
FROM assistant_lifecycles flow
JOIN bots ON bots.id = flow.bot_id AND bots.kind = 'assistant' AND bots.owner_id = flow.owner_id
WHERE flow.action = 'provision' AND flow.outcome = 'done'
  AND flow.open_id IS NOT NULL AND flow.tenant_key IS NOT NULL
  AND flow.offer_chat_id IS NOT NULL AND flow.platform_user_id <> ''
ORDER BY flow.bot_id, flow.finished_at DESC
"""


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def _text(name: str, default: str) -> sa.Column:
    return sa.Column(name, sa.Text(), server_default=sa.text(f"'{default}'"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "assistant_settings",
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
        ),
        _text("open_id", ""),
        _text("tenant_key", ""),
        _text("platform_user_id", ""),
        _text("chat_id", ""),
        _text("timezone", "Asia/Shanghai"),
        _text("work_start", "09:30"),
        _text("work_end", "19:00"),
        sa.Column(
            "workdays",
            postgresql.ARRAY(sa.SmallInteger()),
            server_default=sa.text("'{1,2,3,4,5}'::smallint[]"),
            nullable=False,
        ),
        sa.Column("focus_chats_enc", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ignored_chats_enc", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("daily_turn_budget", sa.Integer(), nullable=True),
        sa.Column("shift_override", sa.Text(), nullable=True),
        _ts("shift_override_until", nullable=True),
        _ts("cold_start_done_at", nullable=True),
        _ts("updated_at", server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            f"work_start ~ {HHMM} AND work_end ~ {HHMM} AND work_start < work_end",
            name="work_hours",
        ),
        sa.CheckConstraint("workdays <@ ARRAY[1,2,3,4,5,6,7]::smallint[]", name="workdays"),
        sa.CheckConstraint(
            "shift_override IS NULL OR shift_override IN ('on', 'off')", name="shift_override"
        ),
        sa.CheckConstraint(
            "daily_turn_budget IS NULL OR daily_turn_budget BETWEEN 10 AND 2000",
            name="daily_turn_budget",
        ),
    )
    op.execute(BACKFILL)


def downgrade() -> None:
    op.drop_table("assistant_settings")
```

- [ ] **Step 5: 写模型并登记**

`coreman/core/db/models/assistants.py`：把顶部的两段导入

```python
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
```

换成

```python
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
```

文件末尾追加：

```python
SHIFT_OVERRIDES = ("on", "off")
_HHMM = "'^([01][0-9]|2[0-3]):[0-5][0-9]$'"


class AssistantSettings(Base):
    """一位助理的设置：开通时记下的所有者身份，工作时段，重点群与忽略的会话。

    身份四列（`open_id`、`tenant_key`、`platform_user_id`、`chat_id`）在授权完成时由平台写入，
    取自点开通时飞书给的操作人与开通卡片所在的私聊，不来自任何请求内容；平台自己发起的轮次
    （没有入站事件）凭它们拿个人工具，见 `coreman/core/assistants/platform_turns.py`。空串表示
    没记下，这样的助理跑不了平台来源的轮次。

    `focus_chats_enc` / `ignored_chats_enc` 登记在 `sealed.COLUMNS`：库里是密文，经会话取出
    的行上是会话 id 的列表；没设置过是 None，密钥销毁后读出来是空列表。
    工作时段是所有者时区里的 `HH:MM`，不跨午夜（约束 `work_start < work_end`）；`workdays`
    是 ISO 星期（1 周一 … 7 周日）。`shift_override` 是「上班」「下班」命令的临时切换，过了
    `shift_override_until` 失效。
    """

    __tablename__ = "assistant_settings"
    __table_args__ = (
        CheckConstraint(
            f"work_start ~ {_HHMM} AND work_end ~ {_HHMM} AND work_start < work_end",
            name="work_hours",
        ),
        CheckConstraint("workdays <@ ARRAY[1,2,3,4,5,6,7]::smallint[]", name="workdays"),
        CheckConstraint(
            f"shift_override IS NULL OR {enum_check('shift_override', SHIFT_OVERRIDES)}",
            name="shift_override",
        ),
        CheckConstraint(
            "daily_turn_budget IS NULL OR daily_turn_budget BETWEEN 10 AND 2000",
            name="daily_turn_budget",
        ),
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
    )
    open_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    tenant_key: Mapped[str] = mapped_column(Text, server_default=text("''"))
    platform_user_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    chat_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    timezone: Mapped[str] = mapped_column(Text, server_default=text("'Asia/Shanghai'"))
    work_start: Mapped[str] = mapped_column(Text, server_default=text("'09:30'"))
    work_end: Mapped[str] = mapped_column(Text, server_default=text("'19:00'"))
    workdays: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger()), server_default=text("'{1,2,3,4,5}'::smallint[]")
    )
    focus_chats_enc: Mapped[list[str] | None] = mapped_column(JSONB)
    ignored_chats_enc: Mapped[list[str] | None] = mapped_column(JSONB)
    # 空则用平台默认（设置项 assistant_daily_turn_budget，2E）。
    daily_turn_budget: Mapped[int | None] = mapped_column(Integer)
    shift_override: Mapped[str | None] = mapped_column(Text)
    shift_override_until: Mapped[datetime | None] = _ts()
    cold_start_done_at: Mapped[datetime | None] = _ts()
    updated_at: Mapped[datetime] = _ts(server_default=text("now()"))
```

`coreman/core/db/models/__init__.py`：把

```python
from coreman.core.db.models.assistants import (
    APPROVAL_CHANNELS,
    APPROVAL_STATUSES,
    ASSISTANT_STATES,
    AssistantApproval,
    AssistantKey,
    AssistantLifecycle,
    AssistantMark,
    AssistantRetiredDir,
    AssistantTranscript,
)
```

换成

```python
from coreman.core.db.models.assistants import (
    APPROVAL_CHANNELS,
    APPROVAL_STATUSES,
    ASSISTANT_STATES,
    SHIFT_OVERRIDES,
    AssistantApproval,
    AssistantKey,
    AssistantLifecycle,
    AssistantMark,
    AssistantRetiredDir,
    AssistantSettings,
    AssistantTranscript,
)
```

同一文件里把

```python
    "ASSISTANT_STATES",
    "AssistantLifecycle",
    "AssistantRetiredDir",
]
```

换成

```python
    "ASSISTANT_STATES",
    "AssistantLifecycle",
    "AssistantRetiredDir",
    "AssistantSettings",
    "SHIFT_OVERRIDES",
]
```

`coreman/core/assistants/sealed.py`：导入里把

```python
from coreman.core.db.models import (
    AssistantApproval,
    AssistantTranscript,
```

换成

```python
from coreman.core.db.models import (
    AssistantApproval,
    AssistantSettings,
    AssistantTranscript,
```

`COLUMNS` 里把

```python
    # 节点上 CLI 的转录：一轮结束后上传，续聊前下发。
    AssistantTranscript: {"blob_enc": Blob()},
}
```

换成

```python
    # 节点上 CLI 的转录：一轮结束后上传，续聊前下发。
    AssistantTranscript: {"blob_enc": Blob()},
    # 所有者标记的重点群与忽略的会话：会话 id 的列表整值封存，读不出来按空列表。
    AssistantSettings: {"focus_chats_enc": Whole([]), "ignored_chats_enc": Whole([])},
}
```

`tests/conftest.py`：把

```python
BUSINESS_TABLES = [
    "assistant_lifecycles",
```

换成

```python
BUSINESS_TABLES = [
    "assistant_settings",
    "assistant_lifecycles",
```

- [ ] **Step 6: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_settings_schema.py tests/integration/test_migration_assistant_settings.py tests/integration/test_migrations.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: PASS（`test_models_match_migrations` 通过说明模型与迁移一致；台账四条用例不变，本任务没有新增读写入口）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 7: 提交**

```bash
git add migrations/versions/0094_assistant_settings.py coreman/core/db/models/assistants.py \
  coreman/core/db/models/__init__.py coreman/core/assistants/sealed.py tests/conftest.py \
  tests/integration/test_assistant_settings_schema.py \
  tests/integration/test_migration_assistant_settings.py
git commit -m "个人助理设置表 assistant_settings 与迁移 0094"
```

### Task 2: `settings.py` 与开通时写入身份

**Files:**
- Create: `coreman/core/assistants/settings.py`
- Modify: `coreman/core/assistants/provisioning.py:29`（导入）、`:550-557`（`step_await_grant` 里授权完成的分支）
- Modify: `tests/unit/test_assistant_sealed_ledger.py:110-131`（`WRITE_STATEMENTS`）
- Modify: `tests/integration/assistant_helpers.py`（新增 helper `identified`）
- Modify: `tests/integration/test_assistant_provisioning.py`（导入与 `test_the_whole_way_from_a_message_to_a_usable_assistant` 末尾）
- Test: `tests/unit/test_assistant_settings.py`（新建）、`tests/integration/test_assistant_settings.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `AssistantSettings`；`coreman.core.timeutils.aware_utc` / `utcnow`；`provisioning.step_await_grant` 里的 `row: AssistantLifecycle`（`open_id`、`tenant_key`、`platform_user_id`、`offer_chat_id`）。
- Produces（`coreman/core/assistants/settings.py`）：
  - `DEFAULT_TIMEZONE = "Asia/Shanghai"`
  - `async def get(session: AsyncSession, bot_id: uuid.UUID) -> AssistantSettings`：没有就按默认值建一行（并发安全），调用方提交。
  - `async def record_identity(session: AsyncSession, bot_id: uuid.UUID, *, open_id: str, tenant_key: str, platform_user_id: str, chat_id: str) -> AssistantSettings`
  - `def identified(row: AssistantSettings) -> bool`：身份四列都非空。
  - `def zone(row: AssistantSettings) -> ZoneInfo`：认不出的时区名退回 `DEFAULT_TIMEZONE`。
  - `def local_today(settings: AssistantSettings, now: datetime) -> date`
  - `def on_shift(settings: AssistantSettings, now: datetime) -> bool`
- Produces（`tests/integration/assistant_helpers.py`）：`async def identified(session, bot, *, open_id="ou_owner", tenant_key="tenant-example", platform_user_id="owner_pid", chat_id="oc_private") -> AssistantSettings`，默认值与 `lifecycle_helpers.active_assistant` 造出的授权、私聊一致。

- [ ] **Step 1: 写失败测试（纯函数）**

`tests/unit/test_assistant_settings.py`（新建）：

```python
"""助理设置里的时间判断：所有者时区的自然日、是否在工作时段、「上班」「下班」的临时切换。"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from coreman.core.assistants import settings
from coreman.core.db.models import AssistantSettings


def row(**over: Any) -> AssistantSettings:
    values: dict[str, Any] = {
        "bot_id": uuid.uuid4(),
        "open_id": "ou_owner",
        "tenant_key": "tenant-example",
        "platform_user_id": "owner_pid",
        "chat_id": "oc_private",
        "timezone": "Asia/Shanghai",
        "work_start": "09:30",
        "work_end": "19:00",
        "workdays": [1, 2, 3, 4, 5],
    }
    return AssistantSettings(**{**values, **over})


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """2026 年 11 月某天的 UTC 时刻；13 日是周五，14 日是周六。"""
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def test_identified_needs_all_four() -> None:
    assert settings.identified(row())
    for column in ("open_id", "tenant_key", "platform_user_id", "chat_id"):
        assert not settings.identified(row(**{column: ""})), column


def test_local_today_follows_the_owners_timezone() -> None:
    late = at(13, 16, 30)  # 北京时间 14 日 00:30，纽约 13 日 11:30
    assert settings.local_today(row(), late) == date(2026, 11, 14)
    assert settings.local_today(row(timezone="America/New_York"), late) == date(2026, 11, 13)
    # 不带时区的时刻按 UTC。
    assert settings.local_today(row(), late.replace(tzinfo=None)) == date(2026, 11, 14)


def test_an_unknown_timezone_falls_back_to_the_default() -> None:
    for name in ("Mars/Olympus", "", "../etc/passwd"):
        assert settings.zone(row(timezone=name)).key == settings.DEFAULT_TIMEZONE, name


def test_on_shift_within_working_hours_on_workdays() -> None:
    s = row()
    assert settings.on_shift(s, at(13, 1, 30))  # 周五 09:30
    assert not settings.on_shift(s, at(13, 1, 29))  # 周五 09:29
    assert settings.on_shift(s, at(13, 10, 59))  # 周五 18:59
    assert not settings.on_shift(s, at(13, 11, 0))  # 周五 19:00
    assert not settings.on_shift(s, at(14, 3, 0))  # 周六 11:00
    assert settings.on_shift(row(workdays=[6]), at(14, 3, 0))


def test_workday_is_judged_in_the_owners_timezone() -> None:
    s = row(timezone="America/New_York")
    assert settings.on_shift(s, at(13, 15, 0))  # 纽约周五 10:00
    assert not settings.on_shift(s, at(14, 1, 0))  # 纽约周五 20:00（UTC 已是周六）
    assert not settings.on_shift(s, at(14, 15, 0))  # 纽约周六 10:00


def test_override_wins_until_it_expires() -> None:
    work, weekend = at(13, 3, 0), at(14, 3, 0)
    off = row(shift_override="off", shift_override_until=work + timedelta(hours=1))
    assert not settings.on_shift(off, work)
    assert settings.on_shift(off, work + timedelta(hours=2))  # 过期后按工作时段
    on = row(shift_override="on", shift_override_until=weekend + timedelta(hours=1))
    assert settings.on_shift(on, weekend)
    assert not settings.on_shift(on, weekend + timedelta(hours=2))
    # 没有到期时刻的切换一直有效。
    assert settings.on_shift(row(shift_override="on"), weekend)
    assert not settings.on_shift(row(shift_override="off"), work)
```

- [ ] **Step 2: 写失败测试（读写与开通）**

`tests/integration/test_assistant_settings.py`（新建）：

```python
"""助理设置的读写：没有就按默认值建一行；开通时记下的身份。"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import settings
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import identified, raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")


async def test_get_creates_one_row_with_defaults(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    first = await settings.get(db_session, bot.id)
    assert (first.timezone, first.work_start, first.work_end) == ("Asia/Shanghai", "09:30", "19:00")
    assert first.workdays == [1, 2, 3, 4, 5] and not settings.identified(first)
    await db_session.commit()
    # 另一个会话再取：还是那一行，不再建。
    async with make_session_factory(db_engine)() as other:
        again = await settings.get(other, bot.id)
        assert again.bot_id == bot.id
        await other.commit()
    assert await raw(db_session, "SELECT count(*) FROM assistant_settings") == 1


async def test_record_identity_fills_the_four_columns(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    await settings.record_identity(
        db_session,
        bot.id,
        open_id="ou_zhang",
        tenant_key="tenant-example",
        platform_user_id="zhangsan",
        chat_id="oc_zhang",
    )
    await db_session.commit()
    stored = await settings.get(db_session, bot.id)
    assert settings.identified(stored)
    assert (stored.open_id, stored.tenant_key, stored.platform_user_id, stored.chat_id) == (
        "ou_zhang",
        "tenant-example",
        "zhangsan",
        "oc_zhang",
    )
    # 再记一次是覆盖，不多出一行。
    await settings.record_identity(
        db_session,
        bot.id,
        open_id="ou_zhang",
        tenant_key="tenant-example",
        platform_user_id="zhangsan",
        chat_id="oc_zhang_2",
    )
    await db_session.commit()
    assert await raw(db_session, "SELECT chat_id FROM assistant_settings") == "oc_zhang_2"


async def test_helper_matches_the_active_assistant_fixture(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    row = await identified(db_session, bot)
    assert (row.open_id, row.tenant_key, row.platform_user_id, row.chat_id) == (
        "ou_owner",
        "tenant-example",
        "owner_pid",
        "oc_private",
    )
```

`tests/integration/test_assistant_provisioning.py`：模型导入里把

```python
from coreman.core.db.models import (
    AssistantKey,
    AssistantLifecycle,
    AssistantRetiredDir,
    Bot,
```

换成

```python
from coreman.core.db.models import (
    AssistantKey,
    AssistantLifecycle,
    AssistantRetiredDir,
    AssistantSettings,
    Bot,
```

把

```python
from tests.integration.lifecycle_helpers import (
    CHAT,
    CIPHER,
    OWNER_OPEN_ID,
    FakeFeishuAuth,
```

换成

```python
from tests.integration.lifecycle_helpers import (
    CHAT,
    CIPHER,
    OWNER_OPEN_ID,
    OWNER_PID,
    TENANT,
    FakeFeishuAuth,
```

在 `test_the_whole_way_from_a_message_to_a_usable_assistant` 里，「还没点同意」那一次 `tick` 之后的断言

```python
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "await_grant" and flow.finished_at is None
    auth.approve()
```

换成

```python
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "await_grant" and flow.finished_at is None
    # 授权完成之前不记身份：平台来源的轮次还用不了。
    assert await db_session.get(AssistantSettings, bot.id) is None
    auth.approve()
```

并在这条用例的末尾（`assert "button" not in str(provisioning.granted_card())` 之后）加上

```python
    # 开通好的同时记下所有者的身份：点开通时飞书给的操作人、开通卡片所在的私聊。
    identity = await db_session.get(AssistantSettings, bot.id, populate_existing=True)
    assert identity is not None
    assert (
        identity.open_id,
        identity.tenant_key,
        identity.platform_user_id,
        identity.chat_id,
    ) == (OWNER_OPEN_ID, TENANT, OWNER_PID, CHAT)
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_settings.py tests/integration/test_assistant_settings.py tests/integration/test_assistant_provisioning.py -q`
Expected: FAIL：前两个文件收集时报 `ImportError: cannot import name 'settings' from 'coreman.core.assistants'`（第二个文件还缺 `identified`）；开通用例在新加的身份断言处失败（`identity is None`）。

- [ ] **Step 4: 实现 `settings.py`**

`coreman/core/assistants/settings.py`（新建）：

```python
"""个人助理的设置：开通时记下的所有者身份，以及按所有者时区做的时间判断。

读写的是 `assistant_settings`（一位助理一行）。身份四列只由开通流程写（`record_identity`），
平台来源的轮次凭它们拿个人工具；工作时段、时区、重点群等由所有者在「我的助理」页与冷启动里改。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import AssistantSettings
from coreman.core.timeutils import aware_utc, utcnow

DEFAULT_TIMEZONE = "Asia/Shanghai"


async def get(session: AsyncSession, bot_id: uuid.UUID) -> AssistantSettings:
    """这位助理的设置；没有就按默认值建一行。调用方提交。

    建行用 `ON CONFLICT DO NOTHING`：两个进程同时第一次读，得到的是同一行，不会有一个报错。
    """
    row = await session.get(AssistantSettings, bot_id)
    if row is None:
        await session.execute(
            insert(AssistantSettings)
            .values(bot_id=bot_id)
            .on_conflict_do_nothing(index_elements=[AssistantSettings.bot_id])
        )
        row = await session.get(AssistantSettings, bot_id, populate_existing=True)
    assert row is not None
    return row


async def record_identity(
    session: AsyncSession,
    bot_id: uuid.UUID,
    *,
    open_id: str,
    tenant_key: str,
    platform_user_id: str,
    chat_id: str,
) -> AssistantSettings:
    """记下所有者在飞书里的身份与他和助理的私聊。只由开通流程调用，值来自飞书给的操作人。"""
    row = await get(session, bot_id)
    row.open_id, row.tenant_key = open_id, tenant_key
    row.platform_user_id, row.chat_id = platform_user_id, chat_id
    row.updated_at = utcnow()
    return row


def identified(row: AssistantSettings) -> bool:
    """身份四列都记下了：缺任何一项，平台来源的轮次都不可用。"""
    return bool(row.open_id and row.tenant_key and row.platform_user_id and row.chat_id)


def zone(row: AssistantSettings) -> ZoneInfo:
    """所有者的时区。库里校验不了时区名：认不出的退回默认，轮询循环不会因此抛错。"""
    try:
        return ZoneInfo(row.timezone)
    except (KeyError, ValueError, OSError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def local_today(settings: AssistantSettings, now: datetime) -> date:
    """所有者时区里的今天（会话每天换新、每日预算、当天到期的待跟进都按它）。"""
    return aware_utc(now).astimezone(zone(settings)).date()


def on_shift(settings: AssistantSettings, now: datetime) -> bool:
    """现在是不是所有者的工作时段。「上班」「下班」的临时切换没过期时以它为准。"""
    now = aware_utc(now)
    until = settings.shift_override_until
    if settings.shift_override is not None and (until is None or until > now):
        return settings.shift_override == "on"
    local = now.astimezone(zone(settings))
    clock = local.strftime("%H:%M")
    return local.isoweekday() in settings.workdays and (
        settings.work_start <= clock < settings.work_end
    )
```

- [ ] **Step 5: 开通时写入身份**

`coreman/core/assistants/provisioning.py`：把

```python
from coreman.core.assistants import eligibility, keys, lifecycle, runtime, sealed
```

换成

```python
from coreman.core.assistants import eligibility, keys, lifecycle, runtime, sealed
from coreman.core.assistants import settings as assistant_settings
```

`step_await_grant` 里把

```python
    if _connected(grant):
        if row.offer_message_id:
            await messenger.update_card(
                session, row.platform_app_id, row.offer_message_id, granted_card()
            )
        bot.assistant_state = "active"
```

换成

```python
    if _connected(grant):
        # 和置为 active 同一个事务：平台自己发起的轮次（交回、子任务结果）凭这份身份拿个人工具。
        # 取自点开通时飞书给的操作人与开通卡片所在的私聊，不来自任何请求内容。
        await assistant_settings.record_identity(
            session,
            bot.id,
            open_id=row.open_id or "",
            tenant_key=row.tenant_key or "",
            platform_user_id=row.platform_user_id,
            chat_id=row.offer_chat_id or "",
        )
        if row.offer_message_id:
            await messenger.update_card(
                session, row.platform_app_id, row.offer_message_id, granted_card()
            )
        bot.assistant_state = "active"
```

- [ ] **Step 6: 登记写入口与测试 helper**

`tests/unit/test_assistant_sealed_ledger.py` 的 `WRITE_STATEMENTS` 里，在

```python
    "coreman/core/assistants/approvals/service.py": 2,
```

之后加一行：

```python
    "coreman/core/assistants/settings.py": 1,  # 按默认值建行，不写内容列
```

`tests/integration/assistant_helpers.py`：导入里把

```python
from coreman.core.db.models import Bot, PlatformApp, RelayServer, RuntimeNode, User
```

换成

```python
from coreman.core.db.models import (
    AssistantSettings,
    Bot,
    PlatformApp,
    RelayServer,
    RuntimeNode,
    User,
)
```

文件末尾追加：

```python
async def identified(
    session: AsyncSession,
    bot: Bot,
    *,
    open_id: str = "ou_owner",
    tenant_key: str = "tenant-example",
    platform_user_id: str = "owner_pid",
    chat_id: str = "oc_private",
) -> AssistantSettings:
    """开通时记下的所有者身份（平台来源的轮次凭它拿个人工具）。

    默认值与 `lifecycle_helpers.active_assistant` 造出的通讯录身份、授权与私聊一致。
    """
    from coreman.core.assistants import settings

    row = await settings.record_identity(
        session,
        bot.id,
        open_id=open_id,
        tenant_key=tenant_key,
        platform_user_id=platform_user_id,
        chat_id=chat_id,
    )
    await session.commit()
    return row
```

- [ ] **Step 7: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_settings.py tests/unit/test_assistant_sealed_ledger.py tests/integration/test_assistant_settings.py tests/integration/test_assistant_provisioning.py tests/integration/test_assistant_gateway_provisioning.py -q`
Expected: PASS。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 8: 提交**

```bash
git add coreman/core/assistants/settings.py coreman/core/assistants/provisioning.py \
  tests/unit/test_assistant_settings.py tests/unit/test_assistant_sealed_ledger.py \
  tests/integration/test_assistant_settings.py tests/integration/assistant_helpers.py \
  tests/integration/test_assistant_provisioning.py
git commit -m "助理设置的读写与时间判断，授权完成时记下所有者身份"
```

### Task 3: 新任务类型、`platform_turns.enqueue` 与台账测试

**Files:**
- Modify: `coreman/core/assistants/kinds.py`（文件末尾追加）
- Modify: `coreman/core/db/models/bus.py:31-45`（`TASK_KINDS`）
- Create: `coreman/core/assistants/platform_turns.py`
- Test: `tests/unit/test_assistant_platform_turn_ledger.py`（新建）、`tests/integration/test_assistant_platform_turns.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `settings.identified`；`coreman.core.bus.tasks.enqueue` / `NewTask`；`records.is_assistant`。
- Produces（`coreman/core/assistants/kinds.py`）：`HANDOVER_KIND = "assistant_handover"`、`JOB_RESULT_KIND = "assistant_job_result"`、`COMPILE_KIND = "assistant_compile"`、`SUBTASK_KIND = "assistant_subtask"`、`BOT_HELP_KIND = "assistant_bot_help"`、`PLATFORM_TURN_KINDS = (HANDOVER_KIND, JOB_RESULT_KIND, COMPILE_KIND, SUBTASK_KIND)`。
- Produces（`coreman/core/assistants/platform_turns.py`）：
  - `ORIGIN_KINDS = (*PLATFORM_TURN_KINDS, APPROVAL_RESULT_KIND)`：没有入站事件也能跑的任务类型。
  - `JOB_SEPARATOR = "#job:"`：子任务会话键是 `<主会话键>#job:<编号>`。
  - `def is_platform(task: Task) -> bool`
  - `def owns_session(row: AssistantSettings, session_key: str | None) -> bool`
  - `async def enqueue(session: AsyncSession, bot: Bot, kind: str, payload: dict[str, Any], *, session_key: str | None = None, dedupe_key: str) -> Task | None`：`kind` 不在 `ORIGIN_KINDS` 里抛 `ValueError`；助理现在不能跑平台来源的轮次（不是 `active`、没记下身份、会话键不属于它）或同键任务已存在时返回 `None`。入队的任务 `user_id` 是所有者、`inbound_event_id` 为空、载荷带 `serialize_session: True`、`bot_key`、`platform_user_id`。调用方提交。
  - `scope_for` 在 Task 4 加进同一个文件。

- [ ] **Step 1: 写失败测试（台账）**

`tests/unit/test_assistant_platform_turn_ledger.py`（新建）：

```python
"""平台来源的任务只能由平台代码经 `platform_turns.enqueue` 创建。

这些类型的任务不带入站事件也能拿到所有者的个人工具（`platform_turns.scope_for`），靠的就是
「任务只能由平台创建」。这里按语法树扫 `coreman/` 下的全部代码：
- 任务行只在 `coreman/core/bus/tasks.py` 插入；
- 每一处 `NewTask(...)` 的 `kind` 都是看得出取值的写法，且不是平台来源的类型；
- 平台来源类型的字面量只出现在定义处、任务类型元组与 worker 的处理器注册表里。
新增一处时这里会失败：改成调 `platform_turns.enqueue`，或确认它与平台来源的类型无关后更新台账。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

from coreman.core.assistants import kinds

ROOT = Path(__file__).resolve().parents[2]
PLATFORM_VALUES = frozenset(kinds.PLATFORM_TURN_KINDS)
ENTRY = "coreman/core/assistants/platform_turns.py"
BUS = "coreman/core/bus/tasks.py"
# 可以出现类型字面量的文件：常量定义、任务类型元组、worker 入口按类型登记处理器。
LITERAL_FILES = frozenset(
    {
        "coreman/core/assistants/kinds.py",
        "coreman/core/db/models/bus.py",
        "coreman/runtime/worker/__main__.py",
    }
)
# `NewTask(kind=<名字>)` 里允许出现的常量名：取值都不是平台来源的类型。
KNOWN_KIND_NAMES = frozenset({"RESUME_KIND", "APPROVAL_RESULT_KIND", "APPROVAL_EXECUTE_KIND"})


def _sources() -> Iterator[tuple[str, ast.AST]]:
    for path in sorted((ROOT / "coreman").rglob("*.py")):
        yield path.relative_to(ROOT).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _called(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def _named(node: ast.expr, name: str) -> bool:
    return (isinstance(node, ast.Name) and node.id == name) or (
        isinstance(node, ast.Attribute) and node.attr == name
    )


def _new_tasks() -> Iterator[tuple[str, ast.Call]]:
    for relative, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called(node) == "NewTask":
                yield relative, node


def test_task_rows_are_inserted_only_by_the_bus() -> None:
    found = set()
    for relative, tree in _sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            built = _called(node) == "Task"
            inserted = (
                _called(node).endswith("insert") and bool(node.args) and _named(node.args[0], "Task")
            )
            if built or inserted:
                found.add(relative)
    assert found == {BUS}


def test_every_new_task_kind_is_static_and_not_a_platform_kind() -> None:
    problems: list[str] = []
    for relative, node in _new_tasks():
        if relative == ENTRY:
            continue
        where = f"{relative}:{node.lineno}"
        kind = next((kw.value for kw in node.keywords if kw.arg == "kind"), None)
        if kind is None or len(node.args) > 1:
            problems.append(f"{where} kind 必须用关键字参数给出")
        elif isinstance(kind, ast.Constant) and isinstance(kind.value, str):
            if kind.value in PLATFORM_VALUES:
                problems.append(f"{where} 平台来源的类型只能经 platform_turns.enqueue 入队")
        elif not (isinstance(kind, ast.Name) and kind.id in KNOWN_KIND_NAMES):
            problems.append(f"{where} kind 的写法不在台账里")
    assert problems == []


def test_the_single_entry_passes_its_checked_kind() -> None:
    calls = [node for relative, node in _new_tasks() if relative == ENTRY]
    assert len(calls) == 1
    kind = next(kw.value for kw in calls[0].keywords if kw.arg == "kind")
    # 入口把校验过的参数原样传下去，不在这里另写类型。
    assert isinstance(kind, ast.Name) and kind.id == "kind"


def test_platform_kind_literals_stay_in_their_files() -> None:
    found = set()
    for relative, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in PLATFORM_VALUES:
                found.add(relative)
    assert found <= LITERAL_FILES, sorted(found - LITERAL_FILES)


def test_known_names_are_not_platform_kinds() -> None:
    assert kinds.APPROVAL_RESULT_KIND not in PLATFORM_VALUES
    assert kinds.APPROVAL_EXECUTE_KIND not in PLATFORM_VALUES
    assert kinds.BOT_HELP_KIND not in PLATFORM_VALUES
    assert PLATFORM_VALUES == {
        "assistant_handover",
        "assistant_job_result",
        "assistant_compile",
        "assistant_subtask",
    }
```

- [ ] **Step 2: 写失败测试（入队）**

`tests/integration/test_assistant_platform_turns.py`（新建）：

```python
"""平台来源的轮次入队：只给开通好、记下了身份的助理，任务不带入站事件、归所有者、排在会话里。"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import platform_turns
from coreman.core.assistants.kinds import (
    APPROVAL_EXECUTE_KIND,
    APPROVAL_RESULT_KIND,
    BOT_HELP_KIND,
    HANDOVER_KIND,
    PLATFORM_TURN_KINDS,
    SUBTASK_KIND,
)
from coreman.core.db.models import TASK_KINDS, AssistantSettings, Task
from tests.integration.assistant_helpers import (
    SECRET,
    identified,
    raw,
    seed_assistant,
    seed_standard,
)

pytestmark = pytest.mark.usefixtures("assistant_master")


def test_kinds_are_registered() -> None:
    assert set(PLATFORM_TURN_KINDS) | {BOT_HELP_KIND} <= set(TASK_KINDS)
    assert platform_turns.ORIGIN_KINDS == (*PLATFORM_TURN_KINDS, APPROVAL_RESULT_KIND)


def test_session_ownership() -> None:
    row = AssistantSettings(chat_id="oc_private")
    assert platform_turns.owns_session(row, "oc_private")
    assert platform_turns.owns_session(row, "oc_private#job:7")
    for foreign in (None, "", "oc_other", "oc_private_2", "oc_private#topic:1"):
        assert not platform_turns.owns_session(row, foreign), foreign
    assert not platform_turns.owns_session(AssistantSettings(chat_id=""), "")


def test_platform_origin_needs_a_listed_kind_and_no_inbound_event() -> None:
    assert platform_turns.is_platform(Task(kind=HANDOVER_KIND, inbound_event_id=None))
    assert platform_turns.is_platform(Task(kind=APPROVAL_RESULT_KIND, inbound_event_id=None))
    assert not platform_turns.is_platform(Task(kind=APPROVAL_RESULT_KIND, inbound_event_id=3))
    assert not platform_turns.is_platform(Task(kind="chat", inbound_event_id=None))
    assert not platform_turns.is_platform(Task(kind=BOT_HELP_KIND, inbound_event_id=None))


async def test_enqueue_builds_an_owner_task_in_the_main_session(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await identified(db_session, bot)
    task = await platform_turns.enqueue(
        db_session,
        bot,
        HANDOVER_KIND,
        # 调用方给的同名键不算数。
        {"note": SECRET, "serialize_session": False, "platform_user_id": "someone_else"},
        dedupe_key="handover:1",
    )
    await db_session.commit()
    assert task is not None and task.kind == HANDOVER_KIND and task.status == "queued"
    assert task.user_id == owner.id and task.inbound_event_id is None
    assert task.session_key == "oc_private"
    assert task.payload == {
        "note": SECRET,
        "serialize_session": True,
        "bot_key": bot.bot_key,
        "platform_user_id": "owner_pid",
    }
    # 载荷按所有者的数据密钥封存，排队用的键留明文。
    stored = str(await raw(db_session, "SELECT payload::text FROM tasks WHERE id = :id", id=task.id))
    assert SECRET not in stored and '"serialize_session": true' in stored
    # 同一个去重键只入队一次。
    again = await platform_turns.enqueue(
        db_session, bot, HANDOVER_KIND, {"note": "x"}, dedupe_key="handover:1"
    )
    assert again is None


async def test_enqueue_accepts_a_job_session_and_refuses_a_foreign_one(
    db_session: AsyncSession,
) -> None:
    bot, _ = await seed_assistant(db_session)
    await identified(db_session, bot)
    job = await platform_turns.enqueue(
        db_session, bot, SUBTASK_KIND, {}, session_key="oc_private#job:3", dedupe_key="j3"
    )
    assert job is not None and job.session_key == "oc_private#job:3"
    foreign = await platform_turns.enqueue(
        db_session, bot, HANDOVER_KIND, {}, session_key="oc_someone_else", dedupe_key="f1"
    )
    assert foreign is None


@pytest.mark.parametrize("state", ["provisioning", "deprovisioning", "deprovisioned", "disabled"])
async def test_enqueue_refuses_an_assistant_that_is_not_active(
    db_session: AsyncSession, state: str
) -> None:
    bot, _ = await seed_assistant(db_session)
    await identified(db_session, bot)
    if state == "disabled":
        bot.enabled = False
    else:
        bot.assistant_state = state
    await db_session.commit()
    assert await platform_turns.enqueue(db_session, bot, HANDOVER_KIND, {}, dedupe_key="k") is None


async def test_enqueue_refuses_without_a_recorded_identity(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    assert await platform_turns.enqueue(db_session, bot, HANDOVER_KIND, {}, dedupe_key="a") is None
    await identified(db_session, bot, open_id="")
    assert await platform_turns.enqueue(db_session, bot, HANDOVER_KIND, {}, dedupe_key="b") is None


async def test_enqueue_refuses_standard_bots_and_other_kinds(db_session: AsyncSession) -> None:
    standard = await seed_standard(db_session)
    assert (
        await platform_turns.enqueue(db_session, standard, HANDOVER_KIND, {}, dedupe_key="s")
        is None
    )
    bot, _ = await seed_assistant(db_session)
    await identified(db_session, bot)
    for kind in ("chat", "cron_run", APPROVAL_EXECUTE_KIND, BOT_HELP_KIND):
        with pytest.raises(ValueError, match="platform turn kind"):
            await platform_turns.enqueue(db_session, bot, kind, {}, dedupe_key=f"x:{kind}")
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_platform_turn_ledger.py tests/integration/test_assistant_platform_turns.py -q`
Expected: FAIL：台账文件里 `test_known_names_are_not_platform_kinds` 等报 `AttributeError: module 'coreman.core.assistants.kinds' has no attribute 'PLATFORM_TURN_KINDS'`；入队文件收集时报 `ImportError: cannot import name 'platform_turns'`。

- [ ] **Step 4: 实现类型常量**

`coreman/core/assistants/kinds.py` 末尾追加：

```python
# 平台自己发起、在助理会话里跑的一轮：没有入站事件，只能经 `platform_turns.enqueue` 入队。
# 盯消息攒下的事项交回主会话。
HANDOVER_KIND = "assistant_handover"
# 子任务或公共员工的结论投回主会话。
JOB_RESULT_KIND = "assistant_job_result"
# 下班时把当天的进展整理进知识库。
COMPILE_KIND = "assistant_compile"
# 子任务自己的一轮（会话键 `<主会话键>#job:<编号>`）。
SUBTASK_KIND = "assistant_subtask"
# 调度公共员工：跑在目标员工那边，不是助理自己的轮次，来源校验另定。
BOT_HELP_KIND = "assistant_bot_help"
PLATFORM_TURN_KINDS = (HANDOVER_KIND, JOB_RESULT_KIND, COMPILE_KIND, SUBTASK_KIND)
```

`coreman/core/db/models/bus.py`：把

```python
    "assistant_approval_execute",
    "assistant_approval_result",
)
TASK_LANES = ("normal", "fast")
```

换成

```python
    "assistant_approval_execute",
    "assistant_approval_result",
    "assistant_handover",
    "assistant_job_result",
    "assistant_compile",
    "assistant_subtask",
    "assistant_bot_help",
)
TASK_LANES = ("normal", "fast")
```

（`tasks.kind` 列没有检查约束，`TASK_KINDS` 只是登记，不需要迁移。）

- [ ] **Step 5: 实现 `platform_turns.enqueue`**

`coreman/core/assistants/platform_turns.py`（新建）：

```python
"""平台来源的轮次：平台自己发起、在助理会话里跑的一轮（交回事项、子任务及其结果、下班编译）。

这样的任务没有「所有者刚发来的那条消息」可以核验，却要拿到所有者的个人工具。放行的依据是：
- 任务只能由平台代码创建——`enqueue` 是唯一入口，网关入库与任何接口都产生不了这些类型
  （tests/unit/test_assistant_platform_turn_ledger.py 扫全库守着这一条）；
- 身份来自开通时记下的 `assistant_settings`，是飞书给的操作人，不来自任何请求内容；
- 每次用到都重新核对：记录还是启用中的助理、任务归所有者、会话是这位助理的、所有者的账号、
  通讯录身份与授权行都没变（Task 4 的 `scope_for`）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.kinds import APPROVAL_RESULT_KIND, PLATFORM_TURN_KINDS
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import AssistantSettings, Bot, Task
from coreman.core.logging import get_logger

log = get_logger(__name__)

# 没有入站事件也能跑的任务类型：平台来源的四种，加上在这样的轮次里提交的审批单的结果回合。
ORIGIN_KINDS = (*PLATFORM_TURN_KINDS, APPROVAL_RESULT_KIND)
# 子任务的会话键：`<主会话键>#job:<编号>`。
JOB_SEPARATOR = "#job:"


def is_platform(task: Task) -> bool:
    """这个任务是不是平台自己发起的一轮：没有入站事件，类型在名单里。"""
    return task.inbound_event_id is None and task.kind in ORIGIN_KINDS


def owns_session(row: AssistantSettings, session_key: str | None) -> bool:
    """会话键是不是这位助理的：与所有者的私聊（主会话），或它派生的子任务会话。"""
    if not row.chat_id or not session_key:
        return False
    return session_key == row.chat_id or session_key.startswith(row.chat_id + JOB_SEPARATOR)


def _runnable(bot: Bot) -> bool:
    return (
        bot.enabled
        and is_assistant(bot)
        and bot.assistant_state == "active"
        and bot.owner_id is not None
    )


async def enqueue(
    session: AsyncSession,
    bot: Bot,
    kind: str,
    payload: dict[str, Any],
    *,
    session_key: str | None = None,
    dedupe_key: str,
) -> Task | None:
    """给这位助理排一轮平台来源的任务；创建这些类型任务的唯一入口。调用方提交。

    Args:
        kind: `ORIGIN_KINDS` 之一
        payload: 这一轮的内容（按所有者的数据密钥封存）；`serialize_session`、`bot_key`、
            `platform_user_id` 由这里写，调用方给的同名键被覆盖
        session_key: 不给就是主会话（所有者与助理的私聊）；子任务给 `<主会话键>#job:<编号>`
        dedupe_key: 幂等键

    Returns:
        入队的任务；同键任务已存在，或这位助理现在跑不了平台来源的轮次（不是启用中的助理、
        没记下身份、会话键不是它的）时返回 None

    Raises:
        ValueError: `kind` 不是平台来源的类型
    """
    if kind not in ORIGIN_KINDS:
        raise ValueError(f"not a platform turn kind: {kind}")
    if not _runnable(bot):
        return None
    row = await session.get(AssistantSettings, bot.id)
    key = session_key or (row.chat_id if row is not None else "")
    if row is None or not assistant_settings.identified(row) or not owns_session(row, key):
        log.warning("assistant_platform_turn_refused", bot_id=str(bot.id), kind=kind)
        return None
    return await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind=kind,
            user_id=bot.owner_id,
            session_key=key,
            dedupe_key=dedupe_key,
            payload={
                **payload,
                # 同一会话串行：所有者正在聊的那一轮没结束时排在它后面。
                "serialize_session": True,
                "bot_key": bot.bot_key,
                "platform_user_id": row.platform_user_id,
            },
        ),
    )
```

- [ ] **Step 6: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_platform_turn_ledger.py tests/unit/test_assistant_sealed_ledger.py tests/integration/test_assistant_platform_turns.py tests/integration/test_models_m3a.py -q`
Expected: PASS。台账的 `test_every_new_task_kind_is_static_and_not_a_platform_kind` 对现有 27 处 `NewTask(...)` 全部通过（它们的 `kind` 都是字符串字面量或 `RESUME_KIND` / `APPROVAL_RESULT_KIND` / `APPROVAL_EXECUTE_KIND`）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 7: 提交**

```bash
git add coreman/core/assistants/kinds.py coreman/core/db/models/bus.py \
  coreman/core/assistants/platform_turns.py \
  tests/unit/test_assistant_platform_turn_ledger.py \
  tests/integration/test_assistant_platform_turns.py
git commit -m "平台来源的任务类型与唯一入队入口 platform_turns.enqueue"
```

### Task 4: 平台来源的 scope（`platform_turns.scope_for` 与 `policy` 的改动）

**Files:**
- Modify: `coreman/core/feishu_personal/policy.py:18`（导入）、`:43-45`（`PRIVATE_TURN_KINDS`）、`:54-71`（`Scope`）、`:128-139`（`task_scope`）、`:184-186`（`verified_origin_scope` 开头）
- Modify: `coreman/core/assistants/platform_turns.py`（导入；文件末尾追加 `scope_for`）
- Test: `tests/integration/test_assistant_platform_scope.py`（新建）

**Interfaces:**
- Consumes: Task 3 的 `platform_turns.is_platform` / `owns_session` / `enqueue`；Task 2 的 `settings.identified` 与测试 helper `identified`；`tests.integration.lifecycle_helpers.active_assistant`（返回 `(bot, owner, 已结束的私聊任务)`，所有者的飞书用户编号 `owner_pid`、`open_id` 为 `ou_owner`、私聊 `oc_private`，授权的 `app_id` 是 `APP_ID`、`tenant_key` 是 `TENANT`）；`tests.integration.test_cron_handler.claim`。
- Produces:
  - `policy.Scope` 多一个字段 `platform: bool = False`（排在 `event` 之后）；`Scope.scheduled` 改为 `event is None and not platform`。
  - `policy.PRIVATE_TURN_KINDS` 末尾并入 `PLATFORM_TURN_KINDS`。
  - `policy.task_scope(session, task_id, actor)` / `policy.capability_scope(...)`：任务没有入站事件时改走 `platform_turns.scope_for`，并核对 `actor` 就是所有者（不是则 `ValueError("personal_actor_mismatch")`）。
  - `policy.verified_origin_scope` 对没有入站事件的任务直接抛 `ValueError("feishu_private_chat_required")`。
  - `async def platform_turns.scope_for(session: AsyncSession, bot: Bot, task: Task) -> Scope`：平台来源的 scope，`platform=True`、`event=None`；任何一项核对不过抛 `ValueError`（`platform_turn_required` / `personal_permission_revoked` / `personal_grant_required`）。任务是否仍在运行、有没有被取消由调用方（`task_scope`）核对。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_platform_scope.py`（新建）：

```python
"""平台来源的轮次拿个人工具：身份来自开通时记下的设置，每次用到都重新核对，对不上就不放行。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import platform_turns
from coreman.core.assistants.kinds import HANDOVER_KIND, PLATFORM_TURN_KINDS, SUBTASK_KIND
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Bot, FeishuPersonalGrant, PlatformApp, Task, User, UserIdentity
from coreman.core.feishu_personal import policy
from tests.integration.assistant_helpers import identified, seed_standard
from tests.integration.lifecycle_helpers import APP_ID, TENANT, active_assistant
from tests.integration.test_cron_handler import claim

pytestmark = pytest.mark.usefixtures("assistant_master")


async def running(session: AsyncSession, bot: Bot, kind: str = HANDOVER_KIND, **kw: Any) -> Task:
    """排一轮平台来源的任务并认领（scope 只给在跑的任务）。"""
    queued = await platform_turns.enqueue(
        session, bot, kind, {"note": "x"}, dedupe_key=f"t:{uuid.uuid4().hex}", **kw
    )
    await session.commit()
    assert queued is not None
    claimed = await claim(session)
    assert claimed.id == queued.id
    return claimed


async def ready(session: AsyncSession) -> tuple[Bot, User, Task]:
    bot, owner, _ = await active_assistant(session)
    await identified(session, bot)
    return bot, owner, await running(session, bot)


def test_platform_kinds_count_as_private_turns() -> None:
    assert set(PLATFORM_TURN_KINDS) <= set(policy.PRIVATE_TURN_KINDS)


async def test_a_platform_turn_acts_as_the_owner(db_session: AsyncSession) -> None:
    bot, owner, task = await ready(db_session)
    scope = await policy.task_scope(db_session, task.id, str(owner.id))
    assert (
        scope.user_id,
        scope.tenant_key,
        scope.app_id,
        scope.open_id,
        scope.platform_user_id,
        scope.chat_id,
    ) == (owner.id, TENANT, APP_ID, "ou_owner", "owner_pid", "oc_private")
    assert scope.task.id == task.id and scope.bot.id == bot.id
    # 没有入站事件，但不是定时任务：和私聊一轮一样按会话围住、能发起授权。
    assert scope.event is None and scope.platform and not scope.scheduled
    # MCP 入口走的 capability_scope 是同一条路。
    again = await policy.capability_scope(db_session, task.id, str(owner.id))
    assert again.platform and again.user_id == owner.id


async def test_a_job_session_qualifies(db_session: AsyncSession) -> None:
    bot, owner, _ = await active_assistant(db_session)
    await identified(db_session, bot)
    task = await running(db_session, bot, SUBTASK_KIND, session_key="oc_private#job:9")
    scope = await policy.task_scope(db_session, task.id, str(owner.id))
    assert scope.platform and scope.task.session_key == "oc_private#job:9"
    assert scope.chat_id == "oc_private"


async def test_only_the_owner_is_the_actor(db_session: AsyncSession) -> None:
    _, _, task = await ready(db_session)
    with pytest.raises(ValueError, match="personal_actor_mismatch"):
        await policy.task_scope(db_session, task.id, str(uuid.uuid4()))


@pytest.mark.parametrize(
    "change",
    [
        "deprovisioning",
        "disabled",
        "no_identity",
        "owner_disabled",
        "rebound",
        "foreign_session",
        "other_user",
        "not_running",
    ],
)
async def test_any_broken_condition_refuses(db_session: AsyncSession, change: str) -> None:
    bot, owner, task = await ready(db_session)
    if change == "deprovisioning":
        bot.assistant_state = "deprovisioning"
    elif change == "disabled":
        bot.enabled = False
    elif change == "no_identity":
        await db_session.execute(text("UPDATE assistant_settings SET tenant_key = ''"))
    elif change == "owner_disabled":
        owner.status = "disabled"
    elif change == "rebound":
        # 通讯录里这个人的飞书身份换了：记下的身份不再是他。
        await db_session.execute(
            update(UserIdentity)
            .where(UserIdentity.user_id == owner.id)
            .values(platform_user_id="someone_else")
        )
    elif change == "foreign_session":
        await db_session.execute(
            update(Task).where(Task.id == task.id).values(session_key="oc_other")
        )
    elif change == "other_user":
        await db_session.execute(
            update(Task).where(Task.id == task.id).values(user_id=uuid.uuid4())
        )
    else:
        await db_session.execute(update(Task).where(Task.id == task.id).values(status="queued"))
    await db_session.commit()
    with pytest.raises(ValueError):
        await policy.task_scope(db_session, task.id, str(owner.id))


async def test_a_grant_for_another_identity_is_refused_and_left_alone(
    db_session: AsyncSession,
) -> None:
    """授权行与记下的身份对不上：拒绝，但不去动授权行（放行的话后面的锁定步骤会把令牌清掉）。"""
    bot, owner, task = await ready(db_session)
    grant = await db_session.get(FeishuPersonalGrant, (bot.id, owner.id))
    assert grant is not None
    grant.open_id = "ou_someone_else"
    await db_session.commit()
    with pytest.raises(ValueError, match="personal_grant_required"):
        await policy.task_scope(db_session, task.id, str(owner.id))
    kept = await db_session.get(FeishuPersonalGrant, (bot.id, owner.id), populate_existing=True)
    assert kept is not None and kept.status == "connected" and kept.token_enc


async def test_without_a_grant_the_app_comes_from_the_assistant_app(
    db_session: AsyncSession,
) -> None:
    """授权被撤销后这一轮仍认得出是谁（工具会如实报没有授权），应用取自助理应用。"""
    bot, owner, task = await ready(db_session)
    grant = await db_session.get(FeishuPersonalGrant, (bot.id, owner.id))
    await db_session.delete(grant)
    await db_session.commit()
    scope = await policy.task_scope(db_session, task.id, str(owner.id))
    app = await db_session.get(PlatformApp, bot.platform_app_id)
    assert app is not None and scope.app_id == app.app_id and scope.platform


async def test_a_forged_platform_task_on_a_standard_bot_is_refused(
    db_session: AsyncSession,
) -> None:
    standard = await seed_standard(db_session)
    forged = await tasks.enqueue(
        db_session,
        NewTask(
            bot_id=standard.id,
            kind=HANDOVER_KIND,
            user_id=standard.created_by,
            session_key="oc_private",
        ),
    )
    await db_session.commit()
    assert forged is not None
    claimed = await claim(db_session)
    with pytest.raises(ValueError, match="platform_turn_required"):
        await policy.task_scope(db_session, claimed.id, str(standard.created_by))


async def test_a_chat_task_without_an_inbound_event_is_refused(db_session: AsyncSession) -> None:
    bot, owner, _ = await active_assistant(db_session)
    await identified(db_session, bot)
    bare = await tasks.enqueue(
        db_session,
        NewTask(bot_id=bot.id, kind="chat", user_id=owner.id, session_key="oc_private"),
    )
    await db_session.commit()
    assert bare is not None
    claimed = await claim(db_session)
    with pytest.raises(ValueError, match="platform_turn_required"):
        await policy.task_scope(db_session, claimed.id, str(owner.id))
    # 卡片回调直接调的这一条同样不放行，也不去按空主键查入站事件。
    with pytest.raises(ValueError, match="feishu_private_chat_required"):
        await policy.verified_origin_scope(db_session, claimed, str(owner.id))
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_platform_scope.py -q`
Expected: FAIL：`test_platform_kinds_count_as_private_turns` 断言失败；其余用例在 `policy.task_scope` 抛 `ValueError: private_human_task_required`（类型还不在 `PRIVATE_TURN_KINDS` 里），期望放行的三条因此失败，期望 `match=` 的几条因错误信息不同而失败。

- [ ] **Step 3: 改 `policy.py`**

`coreman/core/feishu_personal/policy.py`：把

```python
from coreman.core.assistants.kinds import APPROVAL_RESULT_KIND
```

换成

```python
from coreman.core.assistants.kinds import APPROVAL_RESULT_KIND, PLATFORM_TURN_KINDS
```

把

```python
# 本人私聊的一轮，或凭证提交、审批决定后在原私聊里续接它的一轮（来源事件相同，同样由本人驱动）。
# 个人助理的审批单有了结局后投回助理会话的一轮同理。
PRIVATE_TURN_KINDS = ("chat", RESUME_KIND, APPROVAL_RESUME_KIND, APPROVAL_RESULT_KIND)
```

换成

```python
# 本人私聊的一轮，或凭证提交、审批决定后在原私聊里续接它的一轮（来源事件相同，同样由本人驱动）。
# 个人助理的审批单有了结局后投回助理会话的一轮同理。个人助理里平台自己发起的轮次（交回事项、
# 子任务及其结果、下班编译）没有入站事件，另由 `platform_turns.scope_for` 核验。
PRIVATE_TURN_KINDS = (
    "chat",
    RESUME_KIND,
    APPROVAL_RESUME_KIND,
    APPROVAL_RESULT_KIND,
    *PLATFORM_TURN_KINDS,
)
```

把 `Scope` 的定义

```python
@dataclass(frozen=True)
class Scope:
    """One verified owner of one bot grant. `event` exists only for private-chat origins."""

    task: Task
    bot: Bot
    user_id: uuid.UUID
    tenant_key: str
    app_id: str
    open_id: str
    platform_user_id: str
    # The owner's private chat with this bot: the chat itself, or where a job delivers.
    chat_id: str
    event: InboundEvent | None = None

    @property
    def scheduled(self) -> bool:
        return self.event is None
```

换成

```python
@dataclass(frozen=True)
class Scope:
    """One verified owner of one bot grant. `event` exists only for private-chat origins."""

    task: Task
    bot: Bot
    user_id: uuid.UUID
    tenant_key: str
    app_id: str
    open_id: str
    platform_user_id: str
    # The owner's private chat with this bot: the chat itself, or where a job delivers.
    chat_id: str
    event: InboundEvent | None = None
    # 个人助理里平台自己发起的一轮：没有入站事件，但和私聊一轮一样跑在助理会话里。
    platform: bool = False

    @property
    def scheduled(self) -> bool:
        return self.event is None and not self.platform
```

把 `task_scope`

```python
        raise ValueError("private_human_task_required")
    return await verified_origin_scope(session, task, actor)
```

换成

```python
        raise ValueError("private_human_task_required")
    if task.inbound_event_id is None:
        return await _platform_scope(session, task, actor)
    return await verified_origin_scope(session, task, actor)


async def _platform_scope(session: AsyncSession, task: Task, actor: str) -> Scope:
    """没有入站事件的一轮：只有个人助理里平台自己发起的任务才有 scope，且只给所有者本人。"""
    # 函数内导入：platform_turns 要用本模块的 Scope。
    from coreman.core.assistants import platform_turns

    bot = await session.get(Bot, task.bot_id, populate_existing=True)
    if bot is None:
        raise ValueError("platform_turn_required")
    scope = await platform_turns.scope_for(session, bot, task)
    if str(scope.user_id) != actor:
        raise ValueError("personal_actor_mismatch")
    return scope
```

（`task_scope` 的 `raise ValueError("private_human_task_required")` 只出现一次，就是上面这一处。）

把 `verified_origin_scope` 的开头

```python
    """Validate a durable private origin. Callers must separately validate task lifecycle."""
    event = await session.get(InboundEvent, task.inbound_event_id)
```

换成

```python
    """Validate a durable private origin. Callers must separately validate task lifecycle."""
    if task.inbound_event_id is None:
        # 平台来源的任务没有入站事件：卡片回调等直接调到这里的路径一律不放行。
        raise ValueError("feishu_private_chat_required")
    event = await session.get(InboundEvent, task.inbound_event_id)
```

- [ ] **Step 4: 实现 `scope_for`**

`coreman/core/assistants/platform_turns.py`：把导入

```python
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.kinds import APPROVAL_RESULT_KIND, PLATFORM_TURN_KINDS
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import AssistantSettings, Bot, Task
from coreman.core.logging import get_logger
```

换成

```python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.kinds import APPROVAL_RESULT_KIND, PLATFORM_TURN_KINDS
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import (
    AssistantSettings,
    Bot,
    FeishuPersonalGrant,
    PlatformApp,
    Task,
    User,
    UserIdentity,
)
from coreman.core.feishu_personal.policy import Scope
from coreman.core.logging import get_logger
```

文件末尾追加：

```python
async def scope_for(session: AsyncSession, bot: Bot, task: Task) -> Scope:
    """平台来源的一轮以所有者身份用个人工具的核验结果；不来自任何调用方给的身份。

    调用方（`policy.task_scope`）已核对任务仍在运行、没被取消，并在拿到结果后核对能力令牌里的
    人就是所有者。这里核对其余的：任务是平台来源的、归这位助理的所有者、会话是这位助理的；
    记录是启用中的助理；所有者账号可用，通讯录里的飞书身份还是开通时记下的那个；授权行（有的
    话）也是这个身份。授权行与记下的身份对不上时在这里拒绝，不交给 `service._locked`：它遇到
    不一致会清空令牌，一条写错的设置不该把所有者的授权清掉。

    Raises:
        ValueError: 任何一项对不上
    """
    if (
        not is_platform(task)
        or task.bot_id != bot.id
        or bot.platform != "feishu"
        or not _runnable(bot)
        or task.user_id != bot.owner_id
    ):
        raise ValueError("platform_turn_required")
    assert bot.owner_id is not None
    row = await session.get(AssistantSettings, bot.id, populate_existing=True)
    if (
        row is None
        or not assistant_settings.identified(row)
        or not owns_session(row, task.session_key)
    ):
        raise ValueError("platform_turn_required")
    owner = await session.get(User, bot.owner_id, populate_existing=True)
    if owner is None or owner.status != "active" or owner.source == "bootstrap":
        raise ValueError("personal_permission_revoked")
    bound = await session.scalar(
        select(UserIdentity.user_id)
        .where(
            UserIdentity.user_id == owner.id,
            UserIdentity.platform == "feishu",
            UserIdentity.platform_user_id == row.platform_user_id,
        )
        .limit(1)
    )
    if bound is None:
        raise ValueError("personal_grant_required")
    grant = await session.get(FeishuPersonalGrant, (bot.id, owner.id), populate_existing=True)
    if grant is not None and (grant.platform_user_id, grant.open_id, grant.tenant_key) != (
        row.platform_user_id,
        row.open_id,
        row.tenant_key,
    ):
        raise ValueError("personal_grant_required")
    if grant is not None:
        app_id = grant.app_id
    else:
        app = await session.get(PlatformApp, bot.platform_app_id) if bot.platform_app_id else None
        if app is None or not app.app_id:
            raise ValueError("personal_grant_required")
        app_id = app.app_id
    return Scope(
        task,
        bot,
        owner.id,
        row.tenant_key,
        app_id,
        row.open_id,
        row.platform_user_id,
        row.chat_id,
        None,
        True,
    )
```

并把模块文档字符串最后一行里的「（Task 4 的 `scope_for`）」改成「（`scope_for`）」。

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_platform_scope.py tests/integration/test_assistant_platform_turns.py tests/unit/test_assistant_platform_turn_ledger.py -q`
Expected: PASS。

回归（scope 的改动在普通员工与审批闸门的路径上）：

Run: `uv run pytest tests/api/test_feishu_personal.py tests/api/test_feishu_personal_levels.py tests/api/test_feishu_personal_cards.py tests/api/test_feishu_personal_worker.py tests/api/test_wecom_personal.py tests/api/test_assistant_approval_gate.py tests/integration/test_assistant_approval_execute.py tests/integration/test_assistant_approval_result.py tests/integration/test_assistant_provisioning.py -q`
Expected: PASS（这些用例里的任务都带入站事件，或手工拼的 `Scope` 不带 `platform`，行为不变）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add coreman/core/feishu_personal/policy.py coreman/core/assistants/platform_turns.py \
  tests/integration/test_assistant_platform_scope.py
git commit -m "平台来源的轮次按开通时记下的身份拿个人工具"
```

### Task 5: worker 的 `PlatformTurnHandler`

**Files:**
- Create: `coreman/runtime/worker/platform_turn.py`
- Modify: `coreman/runtime/worker/assistant_approvals.py`（整个 `AssistantApprovalResultHandler` 与顶部导入）
- Modify: `tests/unit/test_assistant_sealed_ledger.py`（`WRITE_STATEMENTS`）
- Test: `tests/integration/test_assistant_platform_turn_handler.py`（新建）

**Interfaces:**
- Consumes: Task 3 的 `platform_turns.enqueue` / `is_platform` / `owns_session`；Task 4 的 `policy.task_scope`（开轮时 `chat/personal.py` 的 `configure` 经它签出个人工具的能力令牌）；`coreman.core.assistants.runtime.turn_blocker(session, bot) -> str | None`；`coreman.runtime.worker.chat_handler.ChatTaskHandler` / `Intake`；`coreman.runtime.worker.replies.load_inbound`；测试用 `tests.fakes.fake_relay.FakeRelay`、`tests.integration.worker_helpers.build_ctx` / `user_input`。
- Produces（`coreman/runtime/worker/platform_turn.py`）：
  - `def stand_in(bot: Bot, row: AssistantSettings, task: Task) -> InboundEvent`：不落库的入站事件替身。
  - `class PlatformTurnHandler(ChatTaskHandler)`：子类给出 `kind`（任务类型）并实现 `async def _text(self, session: AsyncSession, ctx: TaskContext, bot: Bot) -> str | None`（这一轮交给模型的文本；返回 `None` 表示这一轮已经没有意义，任务按 `INACTIVE` 取消）。类属性 `INACTIVE = "platform_turn_inactive"`、`OWNER_CHANGED = "platform_owner_changed"`、`ANNOUNCED = "platform_turn_announced"` 是取消任务时写的错误代码，子类可覆盖。任务带入站事件时沿用它（审批结果回合），不带时用替身并在暂停时不私信（任务失败，`assistant_paused`）。
  - 2E 的 `AssistantHandoverHandler`、2F 的编译处理器、3A 的子任务与结果处理器都这样写：

    ```python
    class AssistantHandoverHandler(PlatformTurnHandler):
        kind = HANDOVER_KIND

        async def _text(self, session: AsyncSession, ctx: TaskContext, bot: Bot) -> str | None:
            ...  # 从 ctx.task.payload 组出交回事项的文本；没有可交回的返回 None
    ```

    并在 `coreman/runtime/worker/__main__.py` 的 `handlers` 里按类型登记。本层不登记任何新处理器：新类型的任务要到对应的层才会有人入队。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_platform_turn_handler.py`（新建）：

```python
"""平台来源的一轮在 worker 里：发言者是所有者，文本由平台给，回复发到所有者的私聊，没有入站事件。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import platform_turns
from coreman.core.assistants.kinds import APPROVAL_RESULT_KIND, HANDOVER_KIND
from coreman.core.db.models import (
    AssistantSettings,
    Bot,
    InboundEvent,
    RelayServer,
    RuntimeNode,
    Task,
    TaskStream,
    User,
)
from coreman.core.feishu_personal import policy
from coreman.runtime.worker.assistant_approvals import AssistantApprovalResultHandler
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.platform_turn import PlatformTurnHandler, stand_in
from tests.fakes.fake_relay import FakeRelay
from tests.integration.assistant_helpers import identified, raw
from tests.integration.lifecycle_helpers import active_assistant
from tests.integration.test_assistant_approval_result import _row
from tests.integration.test_cron_handler import claim
from tests.integration.worker_helpers import build_ctx, user_input

pytestmark = pytest.mark.usefixtures("assistant_master")


class Handover(PlatformTurnHandler):
    """用例里的最小子类：文本取自载荷，没有内容就没有这一轮。"""

    kind = HANDOVER_KIND

    async def _text(self, session: AsyncSession, ctx: TaskContext, bot: Bot) -> str | None:
        note = ctx.task.payload.get("note")
        return f"交回事项：{note}" if note else None


async def assistant(session: AsyncSession) -> tuple[Bot, User]:
    """开通好、记下了身份的助理；所在节点支持个人工具。"""
    bot, owner, _ = await active_assistant(session)
    await identified(session, bot)
    relay = await session.get(RelayServer, bot.relay_server_id)
    assert relay is not None
    node = await session.get(RuntimeNode, relay.runtime_node_id)
    assert node is not None
    capabilities = dict(node.capabilities or {})
    capabilities[relay.model_provider] = {
        **capabilities.get(relay.model_provider, {}),
        policy.RUNTIME_CAPABILITY: True,
    }
    node.capabilities = capabilities
    await session.commit()
    return bot, owner


async def queued(session: AsyncSession, bot: Bot, kind: str, payload: dict[str, Any]) -> Task:
    task = await platform_turns.enqueue(session, bot, kind, payload, dedupe_key=f"{kind}:1")
    await session.commit()
    assert task is not None
    claimed = await claim(session)
    assert claimed.id == task.id
    return claimed


async def run(
    engine: AsyncEngine, session: AsyncSession, task: Task, handler: PlatformTurnHandler
) -> tuple[Task, FakeRelay]:
    relay = FakeRelay("normal")
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: relay.client())
    await handler.run(ctx)
    await ctx.chat_logs.drain(5)
    done = await session.get(Task, task.id, populate_existing=True)
    assert done is not None
    return done, relay


async def streams_of(session: AsyncSession, task: Task) -> list[TaskStream]:
    return list(await session.scalars(select(TaskStream).where(TaskStream.task_id == task.id)))


async def test_the_turn_runs_as_the_owner_in_the_private_chat(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await assistant(db_session)
    task = await queued(db_session, bot, HANDOVER_KIND, {"note": "李四问报价"})
    events = await db_session.scalar(select(func.count()).select_from(InboundEvent))
    done, relay = await run(db_engine, db_session, task, Handover())
    assert done.status == "succeeded"
    assert "交回事项：李四问报价" in user_input(relay.requests[0])
    # 和所有者的私聊一轮一样拿到个人工具的能力令牌。
    assert "COREMAN_FEISHU_PERSONAL_TOKEN" in relay.requests[0]["env_vars"]
    # 回复作为一条新消息发到所有者的私聊：有会话坐标，没有被回复的消息。
    (stream,) = await streams_of(db_session, task)
    assert stream.reply_context["chat_id"] == "oc_private"
    assert stream.reply_context["chat_type"] == "single"
    assert "message_id" not in stream.reply_context
    logged = await raw(
        db_session,
        "SELECT session_key || '|' || message_type || '|' || user_id::text"
        " FROM chat_logs WHERE task_id = :id",
        id=task.id,
    )
    assert logged == f"oc_private|{HANDOVER_KIND}|{owner.id}"
    # 替身不落库。
    assert await db_session.scalar(select(func.count()).select_from(InboundEvent)) == events


async def test_nothing_to_say_cancels_without_calling_the_model(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    task = await queued(db_session, bot, HANDOVER_KIND, {})
    done, relay = await run(db_engine, db_session, task, Handover())
    assert done.status == "cancelled" and done.error_code == "platform_turn_inactive"
    assert relay.requests == [] and await streams_of(db_session, task) == []


async def test_a_paused_assistant_fails_the_turn_without_a_message(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """所有者没有刚发来什么：暂停时不为平台自己发起的一轮私信，只结掉任务。"""
    bot, _ = await assistant(db_session)
    task = await queued(db_session, bot, HANDOVER_KIND, {"note": "x"})
    relay_row = await db_session.get(RelayServer, bot.relay_server_id)
    assert relay_row is not None
    relay_row.assistant_pool = False
    await db_session.commit()
    done, relay = await run(db_engine, db_session, task, Handover())
    assert done.status == "failed" and done.error_code == "assistant_paused"
    assert relay.requests == [] and await streams_of(db_session, task) == []


@pytest.mark.parametrize("change", ["foreign_session", "identity_gone", "deprovisioning"])
async def test_a_turn_that_no_longer_belongs_is_dropped(
    db_engine: AsyncEngine, db_session: AsyncSession, change: str
) -> None:
    bot, _ = await assistant(db_session)
    task = await queued(db_session, bot, HANDOVER_KIND, {"note": "x"})
    if change == "foreign_session":
        task.session_key = "oc_other"
    elif change == "identity_gone":
        await db_session.execute(update(AssistantSettings).values(chat_id=""))
    else:
        bot.assistant_state = "deprovisioning"
    await db_session.commit()
    done, relay = await run(db_engine, db_session, task, Handover())
    assert done.status in ("cancelled", "failed") and relay.requests == []
    assert await streams_of(db_session, task) == []


async def test_an_approval_result_without_an_inbound_event_runs_on_the_stand_in(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """平台来源的轮次里提交的审批单没有入站事件：结果回合照样跑，文本照旧由平台写。"""
    bot, _ = await assistant(db_session)
    row = _row(bot.id, "rejected", reason_enc="先别发", session_key="oc_private")
    db_session.add(row)
    await db_session.flush()
    task = await queued(
        db_session, bot, APPROVAL_RESULT_KIND, {"approval_id": str(row.id), "status": "rejected"}
    )
    assert task.inbound_event_id is None
    done, relay = await run(db_engine, db_session, task, AssistantApprovalResultHandler())
    assert done.status == "succeeded"
    text = user_input(relay.requests[0])
    assert f"审批单 {row.id}" in text and "被拒绝，没有执行" in text
    assert "<approver_reason>\n先别发\n</approver_reason>" in text
    (stream,) = await streams_of(db_session, task)
    assert stream.reply_context["chat_id"] == "oc_private"


def test_the_stand_in_carries_only_coordinates() -> None:
    bot = Bot(id=uuid.uuid4())
    row = AssistantSettings(
        bot_id=bot.id, chat_id="oc_private", platform_user_id="owner_pid", open_id="ou_owner"
    )
    event = stand_in(bot, row, Task(id=7))
    assert event.id is None and event.bot_id == bot.id and event.payload == {}
    assert (event.platform, event.kind, event.chat_type) == ("feishu", "message", "single")
    assert (event.chat_id, event.sender_platform_user_id, event.sender_open_id) == (
        "oc_private",
        "owner_pid",
        "ou_owner",
    )
    assert event.platform_msg_id == "platform-turn:7"
    assert event.reply_context == {"chat_id": "oc_private", "chat_type": "single"}
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_platform_turn_handler.py -q`
Expected: FAIL：收集时报 `ModuleNotFoundError: No module named 'coreman.runtime.worker.platform_turn'`。

- [ ] **Step 3: 实现 `PlatformTurnHandler`**

`coreman/runtime/worker/platform_turn.py`（新建）：

```python
"""平台发起的一轮的处理器基类：发言者固定为所有者，消息是平台写好的文本，没有入站内容。

交回事项、子任务及其结果、下班编译、审批结果都是这样的一轮：流水线（会话、提示词、个人工具、
沙箱、转录、收尾）与所有者私聊的一轮完全相同，只有入站解析不同。子类给出任务类型与这一轮的
文本；带入站事件的任务（审批结果回合沿用提交那一轮的）照旧用它，不带的用一个不落库的替身。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import platform_turns, runtime
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import tasks
from coreman.core.chat.announcements import find_announcement
from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import (
    AssistantSettings,
    Bot,
    InboundEvent,
    RelayServer,
    Task,
    User,
    UserIdentity,
)
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.chat_handler import ChatTaskHandler, Intake
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import load_inbound


def stand_in(bot: Bot, row: AssistantSettings, task: Task) -> InboundEvent:
    """没有入站事件的一轮用的替身：不加入会话、不落库，只给流水线提供会话坐标。

    回复上下文里只有所有者的私聊，没有被回复的消息：这一轮的回复作为一条新消息发过去。
    """
    return InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=f"platform-turn:{task.id}",
        kind="message",
        chat_type="single",
        chat_id=row.chat_id,
        sender_platform_user_id=row.platform_user_id,
        sender_open_id=row.open_id,
        payload={},
        reply_context={"chat_id": row.chat_id, "chat_type": "single"},
        received_at=utcnow(),
    )


class PlatformTurnHandler(ChatTaskHandler):
    """子类设置 `kind` 并实现 `_text`；其余与私聊一轮相同。"""

    kind = ""
    # 取消任务时写的错误代码：这一轮已经没有意义、所有者换了人或账号停用、运行时有公告。
    INACTIVE = "platform_turn_inactive"
    OWNER_CHANGED = "platform_owner_changed"
    ANNOUNCED = "platform_turn_announced"

    async def _text(self, session: AsyncSession, ctx: TaskContext, bot: Bot) -> str | None:
        """这一轮交给模型的文本；返回 None 表示这一轮已经没有意义，任务按 `INACTIVE` 取消。"""
        raise NotImplementedError

    async def _cancel(self, session: AsyncSession, ctx: TaskContext, code: str) -> None:
        await tasks.finish(session, ctx.task.id, status="cancelled", error_code=code)

    async def _resolve(
        self, session: AsyncSession, ctx: TaskContext
    ) -> tuple[Intake, RelayServer, list[dict[str, Any]]] | None:
        bot = await session.get(Bot, ctx.task.bot_id)
        if bot is None or not bot.enabled or not is_assistant(bot) or bot.owner_id is None:
            await self._cancel(session, ctx, self.INACTIVE)
            return None
        text = await self._text(session, ctx, bot)
        if text is None:
            await self._cancel(session, ctx, self.INACTIVE)
            return None
        identity = await session.scalar(
            select(UserIdentity)
            .where(UserIdentity.user_id == bot.owner_id, UserIdentity.platform == "feishu")
            .limit(1)
        )
        speaker = await resolve_speaker(
            session,
            platform="feishu",
            platform_user_id=identity.platform_user_id if identity else "",
            resolver=ctx.openuserid,
        )
        user = await session.get(User, bot.owner_id, populate_existing=True)
        if speaker.user_id != bot.owner_id or user is None or user.status != "active":
            await self._cancel(session, ctx, self.OWNER_CHANGED)
            return None
        relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
        if relay is None or not relay.is_active:
            await tasks.finish(
                session, ctx.task.id, status="failed", error_code="relay_unavailable"
            )
            return None
        hit = await find_announcement(session, bot_id=bot.id, relay_server_id=bot.relay_server_id)
        if hit is not None:
            ctx.log.info("announcement_intercepted", announcement_id=str(hit.id))
            await self._cancel(session, ctx, self.ANNOUNCED)
            return None
        if ctx.task.inbound_event_id is not None:
            inbound = await load_inbound(session, ctx.task)
        else:
            found = await self._stand_in(session, ctx, bot)
            if found is None:
                return None
            inbound = found
        intake = Intake(
            bot,
            relay,
            inbound,
            speaker,
            inbound.chat_id,
            inbound.chat_type,
            ctx.task.session_key or inbound.chat_id,
            text,
            self.kind,
        )
        return intake, relay, [{"type": "text", "text": text}]

    async def _stand_in(
        self, session: AsyncSession, ctx: TaskContext, bot: Bot
    ) -> InboundEvent | None:
        """没有入站事件的一轮的替身；这一轮不该跑时结掉任务并返回 None。"""
        row = await session.get(AssistantSettings, bot.id)
        if (
            row is None
            or not platform_turns.is_platform(ctx.task)
            or not platform_turns.owns_session(row, ctx.task.session_key)
        ):
            await self._cancel(session, ctx, self.INACTIVE)
            return None
        reason = await runtime.turn_blocker(session, bot)
        if reason is not None:
            # 没有人在等这一轮：暂停期间不为平台自己发起的任务再私信所有者，只结掉任务。
            ctx.log.warning("assistant_paused", reason=reason)
            await tasks.finish(
                session, ctx.task.id, status="failed", error_code="assistant_paused"
            )
            return None
        return stand_in(bot, row, ctx.task)

    def _needs_content(self) -> bool:
        """消息是平台写好的成品文本，没有媒体要下载。"""
        return False
```

- [ ] **Step 4: 审批结果回合改为子类**

`coreman/runtime/worker/assistant_approvals.py`：把文件开头到 `RETRY_SECONDS` 之前的导入

```python
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed
from coreman.core.assistants.approvals import execute, service, tower
from coreman.core.assistants.kinds import APPROVAL_EXECUTE_KIND, APPROVAL_RESULT_KIND
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import tasks
from coreman.core.chat.announcements import find_announcement
from coreman.core.chat.identity import resolve_speaker
from coreman.core.config import get_settings
from coreman.core.db.models import AssistantApproval, Bot, RelayServer, User, UserIdentity
from coreman.core.feishu_personal.files import FileRelay
from coreman.runtime.worker.chat_handler import ChatTaskHandler, Intake
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import load_inbound
```

换成

```python
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed
from coreman.core.assistants.approvals import execute, service, tower
from coreman.core.assistants.kinds import APPROVAL_EXECUTE_KIND, APPROVAL_RESULT_KIND
from coreman.core.bus import tasks
from coreman.core.config import get_settings
from coreman.core.db.models import AssistantApproval, Bot
from coreman.core.feishu_personal.files import FileRelay
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.platform_turn import PlatformTurnHandler
```

把整个 `class AssistantApprovalResultHandler(ChatTaskHandler):`（从这一行到文件末尾）换成

```python
class AssistantApprovalResultHandler(PlatformTurnHandler):
    """仿 approval_resume：发言者固定为所有者，消息是系统写好的文本。

    入站事件沿用提交那一轮的；审批单是在平台来源的轮次里提交的（没有入站事件）时用替身，回复
    发到所有者的私聊。不要求还是提交时的那个 relay 会话：助理会话会换新，结果文本自带是哪张单、
    结局如何。
    """

    kind = APPROVAL_RESULT_KIND
    INACTIVE = "approval_result_inactive"
    OWNER_CHANGED = "approval_owner_changed"
    ANNOUNCED = "approval_result_announced"

    async def _text(self, session: AsyncSession, ctx: TaskContext, bot: Bot) -> str | None:
        try:
            key = uuid.UUID(str(ctx.task.payload.get("approval_id")))
        except ValueError:
            return None
        row = await session.get(AssistantApproval, key, populate_existing=True)
        if (
            row is None
            or row.bot_id != bot.id
            or row.status != ctx.task.payload.get("status")
            or sealed.is_locked(row)
        ):
            return None
        return service.result_text(row)
```

（会话键原来写的是 `row.session_key or inbound.chat_id`；`deliver` 入队时任务的 `session_key` 就是 `row.session_key`，基类用的 `ctx.task.session_key or inbound.chat_id` 与它等价。）

`tests/unit/test_assistant_sealed_ledger.py` 的 `WRITE_STATEMENTS` 里，在

```python
    "coreman/runtime/scheduler/reaper.py": 1,  # 结掉漏网的进行中记录，报错信息封存
```

之后加一行：

```python
    "coreman/runtime/worker/platform_turn.py": 1,  # 入站事件的替身：不加入会话、不落库
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_platform_turn_handler.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: PASS。

回归（审批结果回合的行为逐项不变；chat 流水线没有改动）：

Run: `uv run pytest tests/integration/test_assistant_approval_result.py tests/integration/test_assistant_approval_execute.py tests/integration/test_assistant_end_to_end.py tests/integration/test_assistant_turn_dispatch.py tests/integration/test_chat_handler.py -q`
Expected: PASS。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add coreman/runtime/worker/platform_turn.py coreman/runtime/worker/assistant_approvals.py \
  tests/unit/test_assistant_sealed_ledger.py \
  tests/integration/test_assistant_platform_turn_handler.py
git commit -m "worker 的平台轮次处理器基类，审批结果回合改为它的子类"
```

### Task 6: 审批结果在没有入站事件时改走平台来源

**Files:**
- Modify: `coreman/core/assistants/approvals/service.py:23`（导入）、`:523-553`（`deliver`）
- Test: `tests/integration/test_assistant_approval_result.py`（追加三条用例与导入）

**Interfaces:**
- Consumes: Task 3 的 `platform_turns.enqueue`；Task 5 的 `AssistantApprovalResultHandler`（没有入站事件的结果回合用替身跑）；`tests.integration.approval_helpers` 的 `assistant_turn` / `install` / `pending` / `queued`。
- Produces: `service.deliver(session, row, bot) -> bool`：`row.origin_event_id` 为空时经 `platform_turns.enqueue(session, bot, APPROVAL_RESULT_KIND, {"approval_id", "status"}, session_key=row.session_key or None, dedupe_key=…)` 入队；带入站事件时与原来完全相同。去重键不变（`assistant-approval:<id>:result:<status>`）。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_approval_result.py`：导入里把

```python
import uuid
from datetime import timedelta
from typing import Any
```

换成

```python
import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any
```

把

```python
from tests.integration.assistant_helpers import into_assistant
```

换成

```python
from tests.integration.approval_helpers import assistant_turn, install, pending, queued
from tests.integration.assistant_helpers import identified, into_assistant
```

文件末尾追加：

```python
async def test_an_approval_submitted_in_a_platform_turn_records_no_inbound_event(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    row = await pending(db_session, replace(scope, event=None, platform=True))
    assert row.origin_event_id is None and row.session_key == "oc_assistant"


async def test_a_result_without_an_inbound_event_comes_back_as_a_platform_turn(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, origin, _ = await owner(db_session)
    await into_assistant(db_session, bot, user)
    await keys.ensure_key(db_session, sealed.master(), user.id)
    await identified(db_session, bot)
    await tasks.finish(db_session, origin.id, status="succeeded")
    row = _row(bot.id, "rejected", reason_enc="先别发", session_key="oc_private")
    db_session.add(row)
    await db_session.flush()
    assert await service.deliver(db_session, row, bot) is True
    # 同一结局只投一次。
    assert await service.deliver(db_session, row, bot) is False
    await db_session.commit()
    (task,) = await queued(db_session, APPROVAL_RESULT_KIND)
    assert task.inbound_event_id is None and task.user_id == user.id
    assert task.session_key == "oc_private"
    assert task.payload == {
        "approval_id": str(row.id),
        "status": "rejected",
        "serialize_session": True,
        "bot_key": bot.bot_key,
        "platform_user_id": "owner_pid",
    }
    done, relay = await run_turn(db_engine, db_session)
    assert done.status == "succeeded"
    text = user_input(relay.requests[0])
    assert "被拒绝，没有执行" in text and "<approver_reason>\n先别发\n</approver_reason>" in text


async def test_without_a_recorded_identity_such_a_result_is_not_delivered(
    db_session: AsyncSession,
) -> None:
    """本功能上线前建的助理没有记下身份：没有入站事件的结果无处可投，和原来一样不投。"""
    bot, user, _, _ = await owner(db_session)
    await into_assistant(db_session, bot, user)
    await keys.ensure_key(db_session, sealed.master(), user.id)
    row = _row(bot.id, "rejected")
    db_session.add(row)
    await db_session.flush()
    assert await service.deliver(db_session, row, bot) is False
    await db_session.commit()
    assert await queued(db_session, APPROVAL_RESULT_KIND) == []
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_approval_result.py -q`
Expected: FAIL：`test_a_result_without_an_inbound_event_comes_back_as_a_platform_turn` 在第一条 `deliver(...) is True` 处失败（现在 `origin_event_id` 为空直接返回 False）；其余用例通过。

- [ ] **Step 3: 实现**

`coreman/core/assistants/approvals/service.py`：把

```python
from coreman.core.assistants import sealed
```

换成

```python
from coreman.core.assistants import platform_turns, sealed
```

把整个 `deliver`

```python
async def deliver(session: AsyncSession, row: AssistantApproval, bot: Bot) -> bool:
    """把这张单的结局作为一轮投回助理会话；沿用提交那一轮的入站事件与会话键。"""
    if row.origin_event_id is None or bot.owner_id is None:
        return False
    if await session.get(InboundEvent, row.origin_event_id) is None:
        return False
    identity = await session.scalar(
        select(UserIdentity.platform_user_id)
        .where(UserIdentity.user_id == bot.owner_id, UserIdentity.platform == "feishu")
        .limit(1)
    )
    task = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind=APPROVAL_RESULT_KIND,
            user_id=bot.owner_id,
            session_key=row.session_key,
            inbound_event_id=row.origin_event_id,
            dedupe_key=f"assistant-approval:{row.id}:result:{row.status}",
            payload={
                "approval_id": str(row.id),
                "status": row.status,
                # 提交的那一轮可能还没结束：同一会话串行，等它收尾再开这一轮。
                "serialize_session": True,
                "bot_key": bot.bot_key,
                "platform_user_id": identity or "",
            },
        ),
    )
    return task is not None
```

换成

```python
async def deliver(session: AsyncSession, row: AssistantApproval, bot: Bot) -> bool:
    """把这张单的结局作为一轮投回助理会话。

    所有者私聊那一轮里提交的单沿用那一轮的入站事件与会话键。平台自己发起的轮次里提交的单没有
    入站事件：经 `platform_turns.enqueue` 入队，身份取自开通时记下的设置，回到提交时的会话。
    """
    if bot.owner_id is None:
        return False
    dedupe = f"assistant-approval:{row.id}:result:{row.status}"
    result = {"approval_id": str(row.id), "status": row.status}
    if row.origin_event_id is None:
        queued = await platform_turns.enqueue(
            session,
            bot,
            APPROVAL_RESULT_KIND,
            result,
            session_key=row.session_key or None,
            dedupe_key=dedupe,
        )
        return queued is not None
    if await session.get(InboundEvent, row.origin_event_id) is None:
        return False
    identity = await session.scalar(
        select(UserIdentity.platform_user_id)
        .where(UserIdentity.user_id == bot.owner_id, UserIdentity.platform == "feishu")
        .limit(1)
    )
    task = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind=APPROVAL_RESULT_KIND,
            user_id=bot.owner_id,
            session_key=row.session_key,
            inbound_event_id=row.origin_event_id,
            dedupe_key=dedupe,
            payload={
                **result,
                # 提交的那一轮可能还没结束：同一会话串行，等它收尾再开这一轮。
                "serialize_session": True,
                "bot_key": bot.bot_key,
                "platform_user_id": identity or "",
            },
        ),
    )
    return task is not None
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_approval_result.py tests/integration/test_assistant_approval_execute.py tests/integration/test_assistant_approval_tick.py tests/integration/test_assistant_approval_manage.py tests/api/test_assistant_approval_decisions.py tests/unit/test_assistant_platform_turn_ledger.py -q`
Expected: PASS（执行、过期、对账各条路径里没有入站事件、也没有记下身份的单照旧不投；台账里 `service.py` 的 `NewTask(kind=APPROVAL_RESULT_KIND)` 是允许的写法）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants/approvals/service.py \
  tests/integration/test_assistant_approval_result.py
git commit -m "平台来源的轮次里提交的审批单，结果投回所有者的私聊"
```

### Task 7: 助理工具注册表，审批与表情工具迁进 `approval` 组

**Files:**
- Create: `coreman/core/assistants/tools.py`
- Modify: `coreman/core/assistants/approvals/tools.py`（文档字符串、导入、`definitions` 换成 `staged`、删掉 `dispatch`）
- Modify: `tests/integration/test_assistant_approval_manage.py:11`、`:34-37`、`:46-55`、`:205-208`
- Modify: `tests/integration/assistant_helpers.py`（新增 `isolate_tool_registry`）
- Test: `tests/unit/test_assistant_tool_registry.py`（新建）

**Interfaces:**
- Consumes: `approvals.tools.own_definitions() -> list[dict]`、`approvals.tools.own_dispatch(session, cipher, scope, name, arguments, cfg, files) -> dict | None`（都保持不变）；`approvals.catalog.is_write(name) -> bool`；`approvals.service.submit(session, cipher, scope, tool, arguments, *, cfg, files)`；`feishu_personal.tools.dispatch(session, cipher, scope, name, arguments, files)`；`kinds.SUBTASK_KIND`。
- Produces（`coreman/core/assistants/tools.py`）：
  - `GROUPS = ("knowledge", "approval", "jobs")`
  - `@dataclass(frozen=True) class Context`：`session: AsyncSession`、`cipher: Cipher`、`scope: Scope`、`files: FileRelay | None = None`、`cfg: tower.Config | None = None`。
  - `Definitions = Callable[[], list[dict[str, Any]]]`；`Dispatch = Callable[[Context, str, dict[str, Any]], Awaitable[dict[str, Any]]]`。
  - `def register(group: str, definitions: Definitions, dispatch: Dispatch) -> None`：组名不认识、工具名重复或与个人工具同名时抛 `ValueError`。2B、2C、3A 在本文件末尾各加一行 `register(...)`。
  - `def group_of(name: str) -> str | None`
  - `def groups_for(kind: str) -> tuple[str, ...]`：`SUBTASK_KIND` 只有 `("knowledge",)`，其余任务类型是全部三组。
  - `def definitions(base: list[dict[str, Any]], *, groups: Iterable[str]) -> list[dict[str, Any]]`：`base` 是 `feishu_personal.tools.definitions(...)` 的清单；有 `approval` 组时写类工具带上暂存说明，没有时写类工具不列出；再按 `GROUPS` 的顺序接上各组登记的工具。
  - `async def dispatch(ctx: Context, name: str, arguments: Any, *, groups: Iterable[str]) -> dict[str, Any]`：组内工具按组放行；写类个人工具在有 `approval` 组时提交审批，否则拒绝；其余转交 `feishu_personal.tools.dispatch`。拒绝一律是 `{"error": "invalid_tool_or_arguments"}`。
- Produces（`coreman/core/assistants/approvals/tools.py`）：`def staged(item: dict[str, Any]) -> dict[str, Any]`（写类工具在助理清单里的样子）；原来的 `definitions(base)` 与 `dispatch(...)` 删除。
- Produces（`tests/integration/assistant_helpers.py`）：`def isolate_tool_registry(monkeypatch: pytest.MonkeyPatch) -> None`：用例里临时登记的工具在用例结束时消失。

- [ ] **Step 1: 写失败测试**

`tests/integration/assistant_helpers.py`：在导入里加上 `import pytest`（放在 `from typing import Any` 之后的第三方导入段首），文件末尾追加：

```python
def isolate_tool_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """让用例里临时登记的助理工具只活到用例结束（注册表是进程级的）。"""
    from coreman.core.assistants import tools

    monkeypatch.setattr(tools, "_TOOLS", dict(tools._TOOLS))
    monkeypatch.setattr(
        tools, "_DEFINITIONS", {group: list(items) for group, items in tools._DEFINITIONS.items()}
    )
```

`tests/unit/test_assistant_tool_registry.py`（新建）：

```python
"""助理自有工具的注册表：三组、按组列出、按组分派；没有 approval 组的轮次只有读类个人工具。"""

from __future__ import annotations

from typing import Any

import pytest

from coreman.core.assistants import tools
from coreman.core.assistants.approvals import catalog
from coreman.core.assistants.approvals.tools import STAGED_NOTE
from coreman.core.assistants.kinds import (
    APPROVAL_RESULT_KIND,
    HANDOVER_KIND,
    JOB_RESULT_KIND,
    SUBTASK_KIND,
)
from coreman.core.feishu_personal import endpoints
from coreman.core.feishu_personal import tools as personal_tools
from tests.integration.assistant_helpers import isolate_tool_registry

APPROVAL_TOOLS = [
    "approval_revise",
    "approval_withdraw",
    "approval_list",
    "assistant_mark_processing",
]
INVALID = {"error": "invalid_tool_or_arguments"}
CTX = tools.Context(None, None, None)  # type: ignore[arg-type]


def listing(*names: str) -> tools.Definitions:
    return lambda: [
        {"name": name, "description": "demo", "inputSchema": {"type": "object", "properties": {}}}
        for name in names
    ]


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(
        self, ctx: tools.Context, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        assert ctx is CTX
        self.calls.append((name, arguments))
        return {"ok": name}


@pytest.fixture
def demo(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """一个 knowledge 组与一个 jobs 组的演示工具，共用一个记录器。"""
    isolate_tool_registry(monkeypatch)
    recorder = Recorder()
    tools.register("knowledge", listing("notes_demo"), recorder)
    tools.register("jobs", listing("assistant_spawn_demo"), recorder)
    return recorder


def base() -> list[dict[str, Any]]:
    return personal_tools.definitions(auth=True, level="all", scopes=endpoints.manifest_scopes())


def test_groups_and_the_built_in_approval_group() -> None:
    assert tools.GROUPS == ("knowledge", "approval", "jobs")
    for name in APPROVAL_TOOLS:
        assert tools.group_of(name) == "approval"
    assert tools.group_of("feishu_chat_history") is None
    assert tools.group_of("notes_demo") is None


def test_only_subtasks_are_narrowed() -> None:
    assert tools.groups_for(SUBTASK_KIND) == ("knowledge",)
    for kind in ("chat", HANDOVER_KIND, JOB_RESULT_KIND, APPROVAL_RESULT_KIND):
        assert tools.groups_for(kind) == tools.GROUPS, kind


def test_register_refuses_unknown_groups_and_name_clashes(demo: Recorder) -> None:
    with pytest.raises(ValueError, match="group"):
        tools.register("misc", listing("x_demo"), demo)
    for taken in ("notes_demo", "approval_list", "feishu_chat_history"):
        with pytest.raises(ValueError, match="taken"):
            tools.register("knowledge", listing("fresh_demo", taken), demo)
    with pytest.raises(ValueError, match="taken"):
        tools.register("knowledge", listing("twice_demo", "twice_demo"), demo)
    # 被拒绝的那一批一个都没登记进去。
    assert tools.group_of("fresh_demo") is None and tools.group_of("twice_demo") is None


def test_a_full_turn_lists_staged_writes_and_every_group(demo: Recorder) -> None:
    source = base()
    out = tools.definitions(source, groups=tools.GROUPS)
    names = [item["name"] for item in out]
    assert names[: len(source)] == [item["name"] for item in source]
    assert names[len(source) :] == ["notes_demo", *APPROVAL_TOOLS, "assistant_spawn_demo"]
    writes = [item for item in out if catalog.is_write(item["name"])]
    assert writes and "feishu_reply_message" in {item["name"] for item in writes}
    for item in writes:
        assert "approval" in item["inputSchema"]["properties"], item["name"]
        assert item["description"].endswith(STAGED_NOTE), item["name"]
    # 原清单没有被就地改动。
    original = next(item for item in source if item["name"] == "feishu_reply_message")
    assert "approval" not in original["inputSchema"].get("properties", {})


def test_a_turn_without_the_approval_group_is_read_only(demo: Recorder) -> None:
    source = base()
    names = [item["name"] for item in tools.definitions(source, groups=("knowledge",))]
    reads = [item["name"] for item in source if not catalog.is_write(item["name"])]
    assert names == [*reads, "notes_demo"]
    assert "feishu_chat_history" in names and "feishu_reply_message" not in names


async def test_dispatch_follows_the_groups(demo: Recorder) -> None:
    assert await tools.dispatch(CTX, "notes_demo", {"q": 1}, groups=("knowledge",)) == {
        "ok": "notes_demo"
    }
    assert await tools.dispatch(CTX, "assistant_spawn_demo", {}, groups=tools.GROUPS) == {
        "ok": "assistant_spawn_demo"
    }
    assert demo.calls == [("notes_demo", {"q": 1}), ("assistant_spawn_demo", {})]
    # 组不在这一轮里：不受理，登记的函数也没被调用。
    assert await tools.dispatch(CTX, "assistant_spawn_demo", {}, groups=("knowledge",)) == INVALID
    assert await tools.dispatch(CTX, "approval_list", {}, groups=("knowledge",)) == INVALID
    # 没有 approval 组的轮次调用写类个人工具：不暂存、不提交，直接拒绝。
    assert await tools.dispatch(CTX, "feishu_reply_message", {}, groups=("knowledge",)) == INVALID
    assert await tools.dispatch(CTX, "notes_demo", ["not", "a", "dict"], groups=tools.GROUPS) == (
        INVALID
    )
    assert len(demo.calls) == 2
```

`tests/integration/test_assistant_approval_manage.py`：把

```python
from coreman.core.assistants.approvals import execute
from coreman.core.assistants.approvals import tools as assistant_tools
```

换成

```python
from coreman.core.assistants import tools as assistant_tools
from coreman.core.assistants.approvals import execute
```

把

```python
async def call(session: AsyncSession, scope: Scope, name: str, arguments: dict[str, Any]) -> Any:
    out = await assistant_tools.dispatch(session, None, scope, name, arguments, None, CFG)  # type: ignore[arg-type]
    await session.commit()
    return out
```

换成

```python
async def call(session: AsyncSession, scope: Scope, name: str, arguments: dict[str, Any]) -> Any:
    ctx = assistant_tools.Context(session, None, scope, None, CFG)  # type: ignore[arg-type]
    out = await assistant_tools.dispatch(ctx, name, arguments, groups=assistant_tools.GROUPS)
    await session.commit()
    return out
```

把

```python
def test_own_tools_are_listed_for_assistants() -> None:
    names = [item["name"] for item in assistant_tools.definitions([])]
    assert names == [
        "approval_revise",
        "approval_withdraw",
        "approval_list",
        "assistant_mark_processing",
    ]
    for item in assistant_tools.definitions([]):
        assert item["inputSchema"]["type"] == "object" and item["description"]
```

换成

```python
def test_own_tools_are_listed_for_assistants() -> None:
    listed = assistant_tools.definitions([], groups=assistant_tools.GROUPS)
    assert [item["name"] for item in listed] == [
        "approval_revise",
        "approval_withdraw",
        "approval_list",
        "assistant_mark_processing",
    ]
    for item in listed:
        assert item["inputSchema"]["type"] == "object" and item["description"]
```

把 `test_revise_swaps_the_files_in_the_bucket` 里的

```python
    async def tool(name: str, arguments: dict[str, Any]) -> Any:
        out = await assistant_tools.dispatch(db_session, None, scope, name, arguments, relay, CFG)  # type: ignore[arg-type]
        await db_session.commit()
        return out
```

换成

```python
    async def tool(name: str, arguments: dict[str, Any]) -> Any:
        ctx = assistant_tools.Context(db_session, None, scope, relay, CFG)  # type: ignore[arg-type]
        out = await assistant_tools.dispatch(ctx, name, arguments, groups=assistant_tools.GROUPS)
        await db_session.commit()
        return out
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_tool_registry.py tests/integration/test_assistant_approval_manage.py -q`
Expected: FAIL：两个文件收集时都报 `ImportError: cannot import name 'tools' from 'coreman.core.assistants'`。

- [ ] **Step 3: 改 `approvals/tools.py`**

`coreman/core/assistants/approvals/tools.py`：把文件开头到 `Status = Literal[` 之前的全部内容

```python
"""个人助理在个人工具 MCP 上的那一层：写类工具改为提交审批，另加审批单管理与「处理中」表情。

MCP 入口对助理记录调这里的 `definitions` / `dispatch`，对普通机器人仍调
`feishu_personal.tools`。读类工具与授权工具原样转交。
"""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.approvals import catalog, marks, service, tower
from coreman.core.crypto import Cipher
from coreman.core.feishu_personal import tools as personal_tools
from coreman.core.feishu_personal.files import FileRelay
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.toolbase import Arguments, Identifier, Short, input_schema

STAGED_NOTE = (
    " Not executed now: staged for the owner's approval and returns pending_approval."
    " Put chat_id, anchor_message_id, background and research in `approval`."
)


def definitions(base: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把普通清单改成助理的：写类工具的入参多一个 `approval`，其余原样。"""
    meta = input_schema(service.Meta)
    out: list[dict[str, Any]] = []
    for item in base:
        name = str(item["name"])
        if not catalog.is_write(name):
            out.append(item)
            continue
        staged = copy.deepcopy(item)
        staged["description"] = str(item["description"]) + STAGED_NOTE
        staged["inputSchema"].setdefault("properties", {})[service.META] = meta
        out.append(staged)
    return out + own_definitions()
```

换成

```python
"""审批组的工具：审批单管理与「处理中」表情，以及写类个人工具在助理清单里的样子。

清单的拼装与分派在 `coreman/core/assistants/tools.py`（助理自有工具的总注册表），这里的工具
登记在它的 `approval` 组里。
"""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.approvals import marks, service, tower
from coreman.core.crypto import Cipher
from coreman.core.feishu_personal.files import FileRelay
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.toolbase import Arguments, Identifier, Short, input_schema

STAGED_NOTE = (
    " Not executed now: staged for the owner's approval and returns pending_approval."
    " Put chat_id, anchor_message_id, background and research in `approval`."
)


def staged(item: dict[str, Any]) -> dict[str, Any]:
    """写类工具在助理清单里的样子：说明里注明不会当场执行，入参多一个 `approval`。原项不动。"""
    out = copy.deepcopy(item)
    out["description"] = str(item["description"]) + STAGED_NOTE
    out["inputSchema"].setdefault("properties", {})[service.META] = input_schema(service.Meta)
    return out
```

再把文件末尾的整个 `dispatch` 函数

```python
async def dispatch(
    session: AsyncSession,
    cipher: Cipher,
    scope: Scope,
    name: str,
    arguments: Any,
    files: FileRelay | None,
    cfg: tower.Config | None,
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return {"error": "invalid_tool_or_arguments"}
    own = await own_dispatch(session, cipher, scope, name, arguments, cfg, files)
    if own is not None:
        return own
    if catalog.is_write(name):
        return await service.submit(session, cipher, scope, name, arguments, cfg=cfg, files=files)
    return await personal_tools.dispatch(session, cipher, scope, name, arguments, files)
```

删掉（`own_dispatch` 是文件里的最后一个函数）。`Status`、四个入参模型、`OWN`、`own_definitions`、`own_dispatch` 不动。

- [ ] **Step 4: 实现注册表**

`coreman/core/assistants/tools.py`（新建）：

```python
"""个人助理自有工具的总注册表：个人工具 MCP 对助理记录在这里取清单、在这里分派。

三组：
- `knowledge`：知识库与待跟进。数据在平台自己的库里、不调飞书，所以飞书授权换代之后照常可用
  （MCP 入口把它排在授权代数判断之前），子任务也可用。
- `approval`：审批单管理与「处理中」表情，连同写类个人工具的暂存（提交审批）。
- `jobs`：派子任务、调度公共员工。子任务自己不可用。

一轮能用哪几组由任务类型决定（`groups_for`）。没有 `approval` 组的轮次拿到的个人工具只有读类：
写类工具不列出，调用也不受理——子任务要对外做事，只能把建议写进结论，由主会话提交。

各组的工具在本文件末尾登记，清单不取决于别的模块有没有被导入。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.approvals import catalog, service, tower
from coreman.core.assistants.approvals import tools as approval_tools
from coreman.core.assistants.kinds import SUBTASK_KIND
from coreman.core.crypto import Cipher
from coreman.core.feishu_personal import tools as personal_tools
from coreman.core.feishu_personal.files import FileRelay
from coreman.core.feishu_personal.policy import Scope

GROUPS = ("knowledge", "approval", "jobs")
_INVALID = "invalid_tool_or_arguments"


@dataclass(frozen=True)
class Context:
    """一次工具调用的环境：已核验的所有者身份，加上审批与文件中转要用的配置。"""

    session: AsyncSession
    cipher: Cipher
    scope: Scope
    files: FileRelay | None = None
    cfg: tower.Config | None = None


Definitions = Callable[[], list[dict[str, Any]]]
Dispatch = Callable[[Context, str, dict[str, Any]], Awaitable[dict[str, Any]]]

_DEFINITIONS: dict[str, list[Definitions]] = {group: [] for group in GROUPS}
_TOOLS: dict[str, tuple[str, Dispatch]] = {}


def register(group: str, definitions: Definitions, dispatch: Dispatch) -> None:
    """把一批工具登记进一个组。

    Args:
        definitions: 返回这批工具的 MCP 定义（`name` / `description` / `inputSchema`）；工具名在
            登记时取一次，之后每次列清单都重新调用
        dispatch: `(ctx, 工具名, 入参字典) -> 结果字典`；只会收到这批里的工具名

    Raises:
        ValueError: 组名不认识，或工具名与已登记的工具、个人工具重名
    """
    if group not in _DEFINITIONS:
        raise ValueError(f"unknown tool group: {group}")
    names = [str(item["name"]) for item in definitions()]
    taken = sorted(
        {
            name
            for name in names
            if name in _TOOLS or name in personal_tools.TOOLS or names.count(name) > 1
        }
    )
    if taken:
        raise ValueError(f"tool names already taken: {', '.join(taken)}")
    _DEFINITIONS[group].append(definitions)
    for name in names:
        _TOOLS[name] = (group, dispatch)


def group_of(name: str) -> str | None:
    """这个工具登记在哪个组；不是助理自有的工具返回 None。"""
    found = _TOOLS.get(name)
    return found[0] if found is not None else None


def groups_for(kind: str) -> tuple[str, ...]:
    """这类任务的一轮能用哪几组：子任务只有 knowledge，其余的轮次全部。"""
    return ("knowledge",) if kind == SUBTASK_KIND else GROUPS


def definitions(base: list[dict[str, Any]], *, groups: Iterable[str]) -> list[dict[str, Any]]:
    """把个人工具的清单改成助理这一轮的。

    Args:
        base: `feishu_personal.tools.definitions(...)` 的结果
        groups: 这一轮能用的组（`groups_for`）
    """
    allowed = frozenset(groups)
    out: list[dict[str, Any]] = []
    for item in base:
        if not catalog.is_write(str(item["name"])):
            out.append(item)
        elif "approval" in allowed:
            out.append(approval_tools.staged(item))
    for group in GROUPS:
        if group in allowed:
            for listing in _DEFINITIONS[group]:
                out.extend(listing())
    return out


async def dispatch(
    ctx: Context, name: str, arguments: Any, *, groups: Iterable[str]
) -> dict[str, Any]:
    """分派助理的一次工具调用：组内工具、写类个人工具（提交审批）、其余个人工具。"""
    if not isinstance(arguments, dict):
        return {"error": _INVALID}
    allowed = frozenset(groups)
    found = _TOOLS.get(name)
    if found is not None:
        group, run = found
        return await run(ctx, name, arguments) if group in allowed else {"error": _INVALID}
    if catalog.is_write(name):
        if "approval" not in allowed:
            return {"error": _INVALID}
        return await service.submit(
            ctx.session, ctx.cipher, ctx.scope, name, arguments, cfg=ctx.cfg, files=ctx.files
        )
    return await personal_tools.dispatch(
        ctx.session, ctx.cipher, ctx.scope, name, arguments, ctx.files
    )


async def _approval(ctx: Context, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    out = await approval_tools.own_dispatch(
        ctx.session, ctx.cipher, ctx.scope, name, arguments, ctx.cfg, ctx.files
    )
    return out if out is not None else {"error": _INVALID}


register("approval", approval_tools.own_definitions, _approval)
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_tool_registry.py tests/integration/test_assistant_approval_manage.py tests/integration/test_assistant_approval_submit.py tests/integration/test_assistant_marks.py -q`
Expected: PASS。

MCP 路由还在引用删掉的 `approvals.tools.definitions` / `dispatch`：这一步之后 `uv run mypy coreman` 会在 `coreman/api/routers/feishu_personal.py` 报两处 `Module has no attribute`，`tests/api/test_assistant_approval_gate.py` 也会失败。路由在同一次提交里改好（Step 6），不单独提交这个中间状态。

- [ ] **Step 6: 路由先接上注册表（行为不变）**

`coreman/api/routers/feishu_personal.py`：把

```python
from coreman.core.assistants.approvals import tools as assistant_tools
from coreman.core.assistants.approvals import tower
```

换成

```python
from coreman.core.assistants import tools as assistant_tools
from coreman.core.assistants.approvals import tower
```

把

```python
        if is_assistant(scope.bot):
            # 个人助理：写类工具改为提交审批，另加审批单管理工具。
            definitions = assistant_tools.definitions(definitions)
```

换成

```python
        if is_assistant(scope.bot):
            # 个人助理：写类工具改为提交审批，另加助理自有的工具。
            definitions = assistant_tools.definitions(definitions, groups=assistant_tools.GROUPS)
```

把

```python
                if is_assistant(scope.bot):
                    value = await assistant_tools.dispatch(
                        session,
                        cipher,
                        scope,
                        name,
                        arguments,
                        relay,
                        tower.config(request.app.state.settings),
                    )
```

换成

```python
                if is_assistant(scope.bot):
                    value = await assistant_tools.dispatch(
                        assistant_tools.Context(
                            session, cipher, scope, relay, tower.config(request.app.state.settings)
                        ),
                        name,
                        arguments,
                        groups=assistant_tools.GROUPS,
                    )
```

（这里先一律给全部三组，与改动前的行为相同；按任务类型裁剪、`knowledge` 组提前在 Task 8。）

Run: `uv run pytest tests/api/test_assistant_approval_gate.py tests/api/test_assistant_approval_decisions.py tests/api/test_feishu_personal.py -q`
Expected: PASS。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 7: 提交**

```bash
git add coreman/core/assistants/tools.py coreman/core/assistants/approvals/tools.py \
  coreman/api/routers/feishu_personal.py tests/unit/test_assistant_tool_registry.py \
  tests/integration/test_assistant_approval_manage.py tests/integration/assistant_helpers.py
git commit -m "助理自有工具的分组注册表，审批与表情工具迁进 approval 组"
```

### Task 8: MCP 路由按组分派，`knowledge` 组在授权代数判断之前

**Files:**
- Modify: `coreman/api/routers/feishu_personal.py:54-141`（`mcp` 函数里取 scope 之后的部分）
- Test: `tests/api/test_assistant_tool_groups.py`（新建）

**Interfaces:**
- Consumes: Task 7 的 `assistant_tools.groups_for` / `group_of` / `definitions` / `dispatch` / `Context` 与 `isolate_tool_registry`；Task 4 的平台来源 scope（`scope.task.kind` 决定这一轮的组）；Task 3 的 `platform_turns.enqueue`；`tests.api.test_assistant_approval_gate` 的 `owner_turn(session, app, *, assistant)` / `listed(client, app, task, user)`；`tests.api.test_feishu_personal` 的 `URL` / `headers` / `rpc` / `value`。
- Produces: MCP 入口 `/api/runtime/feishu-personal/mcp` 对助理记录：
  - `tools/list` 按 `groups_for(scope.task.kind)` 列出；
  - `tools/call` 里 `knowledge` 组的工具排在授权代数判断之前（授权被撤销、重选、重新绑定之后同一轮里照常可用），其余工具仍受它约束（`authorization_changed`）；
  - 平台来源的任务（Task 4）与私聊一轮一样按会话围住。
  - 普通机器人的清单与分派不变，够不到任何助理自有的工具。

- [ ] **Step 1: 写失败测试**

`tests/api/test_assistant_tool_groups.py`（新建）：

```python
"""个人工具 MCP 上助理自有工具的分组：按任务类型列出与放行，knowledge 组不受授权换代影响。"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from coreman.core.assistants import platform_turns, tools
from coreman.core.assistants.kinds import HANDOVER_KIND, SUBTASK_KIND
from coreman.core.bus import tasks
from coreman.core.db.models import FeishuPersonalGrant
from tests.api.test_assistant_approval_gate import listed, owner_turn
from tests.api.test_feishu_personal import URL, headers, rpc, value
from tests.integration.assistant_helpers import identified, isolate_tool_registry
from tests.integration.test_cron_handler import claim

INVALID = "invalid_tool_or_arguments"


@pytest.fixture
def notes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any], uuid.UUID]]:
    """knowledge 组里登记一个演示工具，记下每次调用（工具名、入参、所有者）。"""
    isolate_tool_registry(monkeypatch)
    calls: list[tuple[str, dict[str, Any], uuid.UUID]] = []

    async def run(ctx: tools.Context, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments, ctx.scope.user_id))
        return {"ok": True}

    tools.register(
        "knowledge",
        lambda: [
            {
                "name": "notes_demo",
                "description": "demo",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ],
        run,
    )
    return calls


async def call(
    client: httpx.AsyncClient, auth: dict[str, str], name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    out: dict[str, Any] = value(await client.post(URL, headers=auth, json=rpc(name, arguments)))
    return out


async def platform_turn(session: Any, app: Any, kind: str, **kw: Any) -> tuple[Any, Any, Any]:
    """所有者的助理里一轮平台来源的任务（已认领）。此前那一轮私聊已经结束。"""
    bot, user, task = await owner_turn(session, app, assistant=True)
    # 与 owner_turn 造出的通讯录身份、授权一致。
    await identified(
        session, bot, open_id="ou_human", tenant_key="tenant-test", platform_user_id="human"
    )
    await tasks.finish(session, task.id, status="succeeded")
    queued = await platform_turns.enqueue(session, bot, kind, {}, dedupe_key=f"{kind}:1", **kw)
    await session.commit()
    assert queued is not None
    turn = await claim(session)
    assert turn.id == queued.id
    return bot, user, turn


async def test_a_private_turn_lists_and_runs_knowledge_tools(
    client: httpx.AsyncClient, app: Any, db_session: Any, notes: list[Any]
) -> None:
    _, user, task = await owner_turn(db_session, app, assistant=True)
    listing = await listed(client, app, task, user)
    assert {"notes_demo", "approval_list", "feishu_chat_history", "feishu_authorize"} <= set(
        listing
    )
    auth = await headers(app, task, user)
    assert await call(client, auth, "notes_demo", {"q": "报价"}) == {"ok": True}
    assert notes == [("notes_demo", {"q": "报价"}, user.id)]


async def test_knowledge_tools_survive_a_changed_authorization(
    client: httpx.AsyncClient, app: Any, db_session: Any, notes: list[Any]
) -> None:
    bot, user, task = await owner_turn(db_session, app, assistant=True)
    auth = await headers(app, task, user)
    # 这一轮开始之后授权换了代（撤销、重选或重新绑定都会这样）。
    grant = await db_session.get(FeishuPersonalGrant, (bot.id, user.id), populate_existing=True)
    grant.context_epoch = uuid.uuid4()
    await db_session.commit()
    assert await call(client, auth, "notes_demo", {}) == {"ok": True}
    assert await call(client, auth, "approval_list", {}) == {"error": "authorization_changed"}
    assert await call(client, auth, "feishu_chat_history", {"chat_id": "oc_li"}) == {
        "error": "authorization_changed"
    }
    assert len(notes) == 1


async def test_a_standard_bot_never_reaches_assistant_tools(
    client: httpx.AsyncClient, app: Any, db_session: Any, notes: list[Any]
) -> None:
    _, user, task = await owner_turn(db_session, app, assistant=False)
    assert "notes_demo" not in await listed(client, app, task, user)
    auth = await headers(app, task, user)
    assert (await call(client, auth, "notes_demo", {}))["error"] == INVALID
    assert notes == []


async def test_a_platform_turn_gets_the_owners_tools(
    client: httpx.AsyncClient, app: Any, db_session: Any, notes: list[Any]
) -> None:
    _, user, turn = await platform_turn(db_session, app, HANDOVER_KIND)
    assert turn.inbound_event_id is None
    listing = await listed(client, app, turn, user)
    # 和私聊一轮一样：读写个人工具（写类过审批）、授权工具、审批组与 knowledge 组。
    assert {"notes_demo", "approval_list", "feishu_chat_history", "feishu_authorize"} <= set(
        listing
    )
    assert "approval" in listing["feishu_reply_message"]["inputSchema"]["properties"]
    auth = await headers(app, turn, user)
    assert await call(client, auth, "notes_demo", {}) == {"ok": True}
    assert notes[-1][2] == user.id
    # 别人拿着这一轮的任务号签不出能用的令牌。
    forged = await headers(app, turn, type("Someone", (), {"id": uuid.uuid4()})())
    response = await client.post(URL, headers=forged, json=rpc("notes_demo"))
    assert response.status_code == 403


async def test_a_subtask_is_read_only_plus_knowledge(
    client: httpx.AsyncClient, app: Any, db_session: Any, notes: list[Any]
) -> None:
    _, user, turn = await platform_turn(
        db_session, app, SUBTASK_KIND, session_key="oc_private#job:1"
    )
    listing = await listed(client, app, turn, user)
    assert "notes_demo" in listing and "feishu_chat_history" in listing
    assert "feishu_reply_message" not in listing
    assert not [name for name in listing if name.startswith("approval_")]
    assert "assistant_mark_processing" not in listing
    auth = await headers(app, turn, user)
    assert await call(client, auth, "notes_demo", {}) == {"ok": True}
    assert (await call(client, auth, "approval_list", {}))["error"] == INVALID
    assert (await call(client, auth, "feishu_reply_message", {}))["error"] == INVALID
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_assistant_tool_groups.py -q`
Expected: FAIL：`test_knowledge_tools_survive_a_changed_authorization` 的 `notes_demo` 得到 `{"error": "authorization_changed"}`；`test_a_subtask_is_read_only_plus_knowledge` 的清单里仍有 `feishu_reply_message` 与审批工具。其余三条通过（Task 7 已把注册表接进路由，Task 4 已放行平台来源的任务）。

- [ ] **Step 3: 实现**

`coreman/api/routers/feishu_personal.py` 的 `mcp` 函数：把

```python
    current = (grant.context_epoch if grant else policy.NO_GRANT_EPOCH) == capability.epoch
    parsed = mcp_rpc.parse(await mcp_rpc.read_body(request, MAX_BODY))
```

换成

```python
    current = (grant.context_epoch if grant else policy.NO_GRANT_EPOCH) == capability.epoch
    assistant = is_assistant(scope.bot)
    # 个人助理这一轮能用哪几组自有工具，由任务类型决定（子任务只有 knowledge）。
    groups = assistant_tools.groups_for(scope.task.kind) if assistant else ()
    parsed = mcp_rpc.parse(await mcp_rpc.read_body(request, MAX_BODY))
```

把

```python
        if is_assistant(scope.bot):
            # 个人助理：写类工具改为提交审批，另加助理自有的工具。
            definitions = assistant_tools.definitions(definitions, groups=assistant_tools.GROUPS)
```

换成

```python
        if assistant:
            # 个人助理：写类工具改为提交审批（没有 approval 组的轮次不列出），另加助理自有的工具。
            definitions = assistant_tools.definitions(definitions, groups=groups)
```

把

```python
        try:
            if name in schedules.TOOLS:
                value = await schedules.dispatch(session, scope, name, arguments)
            elif not current:
                value = {"error": "authorization_changed"}
            else:
                relay = files.FileRelay(request.app.state.settings, cipher)
                if is_assistant(scope.bot):
                    value = await assistant_tools.dispatch(
                        assistant_tools.Context(
                            session, cipher, scope, relay, tower.config(request.app.state.settings)
                        ),
                        name,
                        arguments,
                        groups=assistant_tools.GROUPS,
                    )
                else:
                    value = await tools.dispatch(session, cipher, scope, name, arguments, relay)
```

换成

```python
        try:
            if name in schedules.TOOLS:
                value = await schedules.dispatch(session, scope, name, arguments)
            elif assistant and assistant_tools.group_of(name) == "knowledge":
                # 知识库与待跟进在平台自己的库里、不调飞书：授权换代之后同一轮里照常可用，
                # 所以排在授权代数判断之前。这一轮有没有这个组仍由 dispatch 按 groups 把关。
                value = await assistant_tools.dispatch(
                    assistant_tools.Context(session, cipher, scope), name, arguments, groups=groups
                )
            elif not current:
                value = {"error": "authorization_changed"}
            else:
                relay = files.FileRelay(request.app.state.settings, cipher)
                if assistant:
                    value = await assistant_tools.dispatch(
                        assistant_tools.Context(
                            session, cipher, scope, relay, tower.config(request.app.state.settings)
                        ),
                        name,
                        arguments,
                        groups=groups,
                    )
                else:
                    value = await tools.dispatch(session, cipher, scope, name, arguments, relay)
```

改完后 `is_assistant` 在这个文件里只剩 `assistant = is_assistant(scope.bot)` 一处使用，导入保留。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/api/test_assistant_tool_groups.py tests/api/test_assistant_approval_gate.py tests/api/test_assistant_approval_decisions.py tests/api/test_feishu_personal.py tests/api/test_feishu_personal_levels.py tests/api/test_feishu_personal_races.py tests/api/test_wecom_personal.py -q`
Expected: PASS（`test_standard_bot_is_unchanged` 通过说明普通机器人的清单与分派不变）。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/api/routers/feishu_personal.py tests/api/test_assistant_tool_groups.py
git commit -m "个人工具 MCP 按组分派助理工具，knowledge 组不受授权换代影响"
```

### Task 9: 文档、CHANGELOG 与整体验证

**Files:**
- Modify: `docs/feishu.md`（「个人助理应用」一节，在「开通与停用。」那一条之前加两条）
- Modify: `CHANGELOG.md`（`## [Unreleased]` → `### Added` 的第一条之前）

**Interfaces:**
- Consumes: 前八个任务落地的行为。
- Produces: 面向部署方与后续各层的说明。

- [ ] **Step 1: 更新 `docs/feishu.md`**

在「个人助理应用」一节的列表里，紧接「- **聊天之外对助理关闭的功能。**」那一条之后、「- **开通与停用。**」那一条之前，插入：

```markdown
- **助理设置。** 每位助理在 `assistant_settings` 里有一行：授权完成、助理开通好的同时，平台记下所有者在飞书里的身份（`open_id`、`tenant_key`、飞书用户编号）与他和助理的私聊，取自点「开通」时飞书给的操作人与开通卡片所在的私聊，不来自任何请求内容；另有时区（默认 `Asia/Shanghai`）、工作时段（默认 `09:30`–`19:00`，不跨午夜）、工作日（默认周一到周五）、重点群与忽略的会话（按所有者的数据密钥加密）、每日轮次预算与「上班」「下班」的临时切换。需要数据库迁移 `0094`：已开通的助理从最近一条完成的开通流程回填身份；更早建的助理（迁移 `0093` 之前）没有可回填的记录，重新开通后才有。
- **平台自己发起的轮次。** 交回事项、子任务及其结果、下班编译这几类任务（`assistant_handover`、`assistant_subtask`、`assistant_job_result`、`assistant_compile`）不由所有者的消息触发，没有入站事件。它们只能由平台代码经同一个入口入队，网关与任何接口都产生不了；跑的时候发言者固定为所有者，回复发到所有者与助理的私聊。这样的一轮要用所有者的个人工具时，平台不凭请求里的任何身份，而是逐项核对：记录是开通好、启用中的助理，任务归所有者、会话是这位助理的，所有者账号启用，通讯录里的飞书身份与授权行都还是开通时记下的那个；任何一项对不上就拿不到个人工具。没有记下身份的助理入不了这类任务。在这样的轮次里提交的审批单，结果同样投回所有者的私聊。助理暂停期间这类任务直接记为失败（`assistant_paused`），不再私信所有者。已知限制：这类轮次里触发的飞书授权卡片点完不会自动续接，所有者授权后下一次交回或他自己发消息时继续。
- **助理自有工具的分组。** 个人工具 MCP 上助理自有的工具分三组：`knowledge`（知识库与待跟进，数据在平台自己的库里，飞书授权在一轮中途被撤销或重选之后照常可用）、`approval`（审批单管理与「处理中」表情，连同写类个人工具的提交审批）、`jobs`（派子任务、调度公共员工）。子任务的一轮只有 `knowledge` 组与读类个人工具：写类工具不列出，调用也不受理。
```

- [ ] **Step 2: 更新 `CHANGELOG.md`**

在 `## [Unreleased]` 下 `### Added` 的第一条（`- The approval system can be named in personal assistant messages. …`）之前插入：

```markdown
- Groundwork for personal assistant phases 2 and 3; nothing changes for users yet. The new table `assistant_settings` (migration `0094`) keeps, per assistant, the owner's Feishu identity recorded when setup completes, the time zone, working hours and working days, and the encrypted lists of focus and ignored chats; assistants that are already set up are backfilled from their latest completed setup. Turns the platform starts by itself (hand-overs, sub-tasks and their results, the end-of-day compile) can carry the owner's personal tools without an inbound message: such tasks can only be queued through one function, run as the owner in the owner's private chat, and every use re-checks the record, the owner's account, directory identity and authorization against the recorded identity. Assistant-only tools on the personal MCP are registered in three groups (`knowledge`, `approval`, `jobs`); knowledge tools keep working when the Feishu authorization changes mid-turn, and a sub-task turn gets only knowledge tools and read-only personal tools. Results of approvals submitted in a platform-started turn are delivered to the owner's private chat. Assistants created before migration `0093` have no recorded identity and get platform-started turns only after being set up again. Runtime nodes need no upgrade. See `docs/feishu.md`.
```

- [ ] **Step 3: 整体验证**

Run: `uv run pytest tests/unit -q`
Expected: PASS。

Run: `uv run pytest tests/integration tests/api -q`（有 Docker 时直接跑；否则先按 Global Constraints 设置 `TEST_DATABASE_URL`。想快一些可加 `-n auto`，此时 `TEST_DATABASE_URL` 的账号要有建库权限）
Expected: PASS。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 全部通过。

Run: `uv run pytest tests/integration/test_migrations.py -q`
Expected: PASS（其中 `test_models_match_migrations` 等价于 `uv run alembic check`）。

提交前在工作树根目录按仓库约定做一次敏感词自查（`git diff --cached -U0` 里新增的行不含公司名、内部域名、真实人名、本机绝对路径），有输出就改成通用表述。

- [ ] **Step 4: 提交**

```bash
git add docs/feishu.md CHANGELOG.md
git commit -m "文档：助理设置、平台发起的轮次与助理工具分组"
```

---

## 给后续各层的交接

- 2B、2C：在 `coreman/core/assistants/tools.py` 末尾各加一行 `register("knowledge", <definitions>, <dispatch>)`，`dispatch` 的签名是 `async (ctx: tools.Context, name: str, arguments: dict[str, Any]) -> dict[str, Any]`，身份只取 `ctx.scope`。新表按总览「每层都要守的约束」登记。
- 2D：盯消息循环里以所有者身份调飞书，用 `settings.get` 取时区与名单（`list(row.focus_chats_enc or [])`），用 `settings.on_shift` 判断是否轮询；没有记下身份（`settings.identified` 为假）的助理不轮询。
- 2E：`AssistantHandoverHandler(PlatformTurnHandler)` 设 `kind = HANDOVER_KIND`、实现 `_text`，在 `coreman/runtime/worker/__main__.py` 登记；入队只调 `platform_turns.enqueue`，它返回 `None` 时（去重、暂停之外的不可用）条目留在待交回。入队前自己看 `runtime.turn_blocker`，暂停期间不入队。每日换新会话用 `settings.local_today`。
- 2F：`shift_override` / `shift_override_until`、`cold_start_done_at` 的写入；`COMPILE_KIND` 的处理器同样继承 `PlatformTurnHandler`。
- 3A：子任务会话键用 `f"{row.chat_id}{platform_turns.JOB_SEPARATOR}{job_id}"`；`SUBTASK_KIND` 的轮次由 `tools.groups_for` 只给 `knowledge` 组；`jobs` 组在 `tools.py` 末尾登记。子任务里的授权工具（`feishu_authorize`）是否列出由 3A 定。
- 3B：`BOT_HELP_KIND` 不在 `PLATFORM_TURN_KINDS` 里，目标员工那一轮的来源校验与加密由 3B 的计划给出；要创建这个类型的任务时，同步更新 `tests/unit/test_assistant_platform_turn_ledger.py`。
