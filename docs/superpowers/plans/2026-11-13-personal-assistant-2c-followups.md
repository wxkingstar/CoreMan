# 个人助理 2C：待跟进 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 个人助理有一张按人加密的待跟进清单：每条有助理内递增的编号（显示为 `F012`）、四类之一、状态与两个日期；助理经 `followup_add` / `followup_update` / `followup_snooze` / `followup_close` / `followup_list` 读写，平台按日期取出当天到期的条目，起因消息被撤回时自动关闭。

**Architecture:** 新表 `assistant_followups`，标题与备注两列登记在 `sealed.COLUMNS`（经 ORM 写入由 flush 钩子封存，读出时还原），分类、状态、日期、编号是明文，按日期查询不解密。业务逻辑在 `coreman/core/assistants/followups.py`，全部按 `bot_id` 过滤；编号分配与未结上限在同一把按助理的事务级咨询锁里完成，`(bot_id, seq)` 唯一约束兜底。工具在 `coreman/core/assistants/followup_tools.py`，注册进 2A 的工具注册表 `knowledge` 组；日期按所有者时区的「今天」解释（2A 的 `settings.get` 与 `local_today`）。提示词规则是一段固定文字，由 `personal.configure` 拼进助理的稳定段。

**Tech Stack:** Python、SQLAlchemy async、Alembic、PostgreSQL、pydantic、pytest。

**Spec:** `docs/superpowers/specs/2026-11-11-personal-assistant.md`（第六节「待跟进」、第二节「加密与存储」、第九节「我的助理」页）；分层总览 `docs/superpowers/plans/2026-11-13-personal-assistant-phase2-3.md`（「层间接口 → 2C」「每层都要守的约束」）。

## Global Constraints

- 本层依赖 2A（`assistant_settings`、`coreman/core/assistants/settings.py`、`coreman/core/assistants/tools.py`）与迁移 `0095`（2B）。开工前确认两者已在集成分支 `claude/personal-assistant-phase2-3` 上；迁移写作 `0096_assistant_followups`、`down_revision = "0095"`。
- 公开仓：代码、测试、文档、提交信息只用通用占位（`example.com`、`demo-bot`、`cli_example`、张三 / 李四），提交前跑仓库约定的敏感词自查。
- 接口名沿用总览，不改名：表 `assistant_followups`；`followups.add` / `update` / `snooze` / `close` / `list_` / `due(session, bot_id, today)` / `close_for_message(session, bot_id, message_id)`；工具 `followup_add`、`followup_update`、`followup_snooze`、`followup_close`、`followup_list`，组名 `knowledge`。
- 库里的枚举用英文：分类 `decide`（等我决定）/ `promised`（我答应）/ `waiting`（等别人）/ `remind`（提醒我）；状态 `open` / `snoozed` / `done` / `dropped`。
- 标题与备注只进 `title_enc`、`notes_enc` 两列；日志只带代码与编号，不带标题、备注。分类、状态、日期、编号、起因消息编号是明文元数据。
- 每个助理未结（`open` + `snoozed`）条目上限 500；标题上限 200 字、备注上限 4000 字。
- 新内容表要登记四处：`sealed.COLUMNS`、`tests/unit/test_assistant_sealed_ledger.py` 的台账、`tests/conftest.py` 的 `BUSINESS_TABLES`、`coreman/core/db/models/__init__.py`。
- 普通员工（`kind='standard'`）不受影响：没有新工具、提示词逐字不变（Task 5 的用例保证）。
- 本计划不做：到期条目的交回与每日去重（2E）、撤回的发现（2D/2E 调 `close_for_message`）、「我的助理」页与接口（3C）、导入（3D）、`job_id` 的外键与子任务关联（3A；这里只留可空的普通列）、已关闭条目的清理。
- 验证命令：`uv run pytest <路径> -q`、`uv run ruff check .`、`uv run ruff format --check .`、`uv run mypy coreman`、`uv run alembic check`（`TEST_DATABASE_URL` 按本机配置）。

## 对 2A 的假定

写本计划时 2A 还没有落地，下列签名取自总览并补全了总览没写的部分。开工时逐条对照 2A 的实际代码，有出入就按实际的改本计划里对应的调用（都集中在 Task 4）。

1. `coreman/core/assistants/tools.py`：
   - `Group = Literal["knowledge", "approval", "jobs"]`；`Dispatch = Callable[[AsyncSession, Cipher, Scope, str, dict[str, Any]], Awaitable[dict[str, Any]]]`。
   - `register(group: Group, definitions: list[dict[str, Any]], dispatch: Dispatch) -> None`：按 `definitions` 里每一项的 `"name"` 建路由；MCP 入口收到这个名字的 `tools/call` 就调 `dispatch(session, cipher, scope, name, arguments)`（依次是会话、平台主密钥、本轮身份、工具名、入参），返回值原样作为工具结果，调用之后由路由提交事务。
   - `definitions(base: list[dict[str, Any]], *, groups: Collection[Group]) -> list[dict[str, Any]]`：返回 `base` 加上所列各组注册的工具定义。
   - 注册发生在模块导入时；各组的工具模块由 MCP 路由 `coreman/api/routers/feishu_personal.py` 在导入区导入，本计划在同一处加一行。2A 若另有集中装载各组模块的地方，就把那一行导入放到那里。
   - `knowledge` 组的工具在授权代数变了时照常可用，由 2A 的路由负责。
   - 以上与 2B 计划（`2026-11-13-personal-assistant-2b-notes.md`）「对 2A 的假定」第 1–3 条一致。
2. `coreman/core/assistants/settings.py`：
   - `async def get(session: AsyncSession, bot_id: uuid.UUID) -> AssistantSettings`：没有就按默认值建一行并 flush（不提交），同一会话里再次调用返回同一个对象。
   - `def local_today(settings: AssistantSettings, now: datetime) -> date`：`now` 换到 `settings.timezone` 之后的日期。
   - 模型 `AssistantSettings` 从 `coreman.core.db.models` 导出，`timezone: Mapped[str]`（IANA 名，默认 `Asia/Shanghai`），可直接赋值修改。
3. 表 `assistant_settings` 已在 `tests/conftest.py` 的 `BUSINESS_TABLES` 里；`migrations/versions/` 里已有 `revision = "0095"` 的迁移。

## 设计选择

**1. 编号分配：按助理的事务级咨询锁 + 唯一约束。** `add` 先取 `pg_advisory_xact_lock(hashtextextended('assistant-followups:<bot_id>', 0))`，再数未结条数、取 `max(seq) + 1`、插入。没有锁 `bots` 的行：那一行被别的流程频繁更新。锁随事务结束释放，所以同一助理并发的 `add` 排队，编号不重不跳；`(bot_id, seq)` 唯一约束兜住绕过 `add` 的写入。仓库里已有同样的写法（`coreman/core/assistants/runtime.py` 的节点锁）。

**2. 「稍后再看」的到期：读之前唤醒。** `snooze` 把状态改为 `snoozed`、`review_on` 设为那一天。`due` 与 `list_` 开头各执行一条只改元数据的 `UPDATE`，把 `review_on <= 今天` 的 `snoozed` 改回 `open`。这样库里的状态始终是真的，3C 的页面与 2E 的投递不用各自再推一遍规则。

**3. `due` 的定义。** 状态为 `open` 且（`review_on <= 今天` 或 `due_on <= 今天`）。它是幂等的读：同一天调用多少次都返回同样的条目，直到条目被推后、改期或关闭。「同一条一天只提醒一次」由调用方（2E）去重，本层不记提醒时间。

**4. 比 spec 多出的两列。** `closed_at`（关闭时间）与 `close_code`（平台自动关闭的原因代码，目前只有 `origin_recalled`）。都是元数据：撤回自动关闭后，清单里要能说明这条为什么没了，而备注是密文，平台不往里追加。

**5. `followup_list(scope)` 的 `scope` 也接受一个编号。** spec 只有五个工具，没有「读一条」。清单里每条只给标题与 `has_notes`，免得 500 条的备注一次全部进上下文；`scope` 传 `F012` 时返回这一条连同完整备注。

**6. 可空字段的清除。** 工具入参经 `toolbase._slim` 之后不表达 `null`，所以 `followup_update` 用 `clear: ["notes" | "review_on" | "due_on"]` 表示清空。清掉 `snoozed` 条目的 `review_on` 时状态同时改回 `open`，否则它永远不会再出现。

**7. 密钥不可用时。** 与其它内容表一致：flush 钩子把标题写成空串、备注写成空，行与元数据照常；读出时 `sealed.is_locked(row)` 为真，工具结果里带 `locked: true`。停用后的助理不再跑轮次，这条路径只作兜底。

## File Structure

| 文件 | 职责 |
|---|---|
| `migrations/versions/0096_assistant_followups.py`（新） | 表 `assistant_followups` |
| `coreman/core/db/models/assistants.py` | 模型 `AssistantFollowup` 与三个枚举常量 |
| `coreman/core/db/models/__init__.py` | 导出 |
| `coreman/core/assistants/sealed.py` | `COLUMNS` 登记 `title_enc`、`notes_enc` |
| `coreman/core/assistants/followups.py`（新） | 增改、推后、关闭、编号、上限、列表、到期、撤回自动关闭、输出视图 |
| `coreman/core/assistants/followup_tools.py`（新） | 五个工具的入参、定义、分派，注册进 `knowledge` 组 |
| `coreman/core/assistants/followup_prompt.py`（新） | 待跟进规则的提示词常量 |
| `coreman/api/routers/feishu_personal.py` | 导入 `followup_tools` 触发注册 |
| `coreman/runtime/worker/chat/personal.py` | 助理的提示词追加待跟进规则 |
| `tests/conftest.py`、`tests/unit/test_assistant_sealed_codec.py`、`tests/unit/test_assistant_sealed_ledger.py` | 清表顺序、登记清单、台账 |
| `tests/integration/test_assistant_followups_schema.py`（新） | 表约束与密文 |
| `tests/integration/test_assistant_followups.py`（新） | 增改、隔离、并发编号、上限 |
| `tests/integration/test_assistant_followups_queries.py`（新） | 列表各 scope、到期、推后到期、撤回自动关闭 |
| `tests/integration/test_assistant_followup_tools.py`（新） | 工具分派、时区、错误 |
| `tests/unit/test_assistant_followup_tools_loaded.py`（新） | 注册与路由装载 |
| `tests/unit/test_assistant_followup_prompt.py`（新）、`tests/api/test_assistant_workspace_prompt.py` | 提示词 |
| `docs/feishu.md`、`CHANGELOG.md` | 行为说明 |

---

### Task 1: 表 `assistant_followups` 与内容列登记

**Files:**
- Create: `migrations/versions/0096_assistant_followups.py`
- Modify: `coreman/core/db/models/assistants.py`（导入区与文件末尾）
- Modify: `coreman/core/db/models/__init__.py:24-34`（导入）与文件末尾的 `__all__`
- Modify: `coreman/core/assistants/sealed.py:47-60`（导入）与 `COLUMNS`（`AssistantTranscript` 那一项之后）
- Modify: `tests/conftest.py:24`（`BUSINESS_TABLES`）
- Modify: `tests/unit/test_assistant_sealed_codec.py:12-48`
- Test: `tests/integration/test_assistant_followups_schema.py`

**Interfaces:**
- Consumes: `sealed.COLUMNS`、`sealed.Text`；`coreman.core.db.base.enum_check(column, values) -> str`；测试用 `tests.integration.assistant_helpers` 的 `SECRET`、`raw(session, sql, **params)`、`seed_assistant(session) -> tuple[Bot, User]`，fixture `assistant_master`、`db_session`、`db_engine`。
- Produces（从 `coreman.core.db.models` 导出）：
  - `FOLLOWUP_CATEGORIES = ("decide", "promised", "waiting", "remind")`
  - `FOLLOWUP_STATUSES = ("open", "snoozed", "done", "dropped")`
  - `FOLLOWUP_CLOSE_CODES = ("origin_recalled",)`
  - `AssistantFollowup`：`id: uuid.UUID`、`bot_id: uuid.UUID`、`seq: int`、`category: str`、`status: str`（默认 `open`）、`review_on: date | None`、`due_on: date | None`、`origin_message_id: str | None`、`job_id: uuid.UUID | None`、`title_enc: str`、`notes_enc: str | None`、`close_code: str | None`、`created_at`、`updated_at`、`closed_at: datetime | None`
  - `sealed.COLUMNS[AssistantFollowup] == {"title_enc": Text(""), "notes_enc": Text()}`

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_followups_schema.py`：

```python
"""assistant_followups：约束，以及标题与备注在库里是密文。"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import AssistantFollowup
from coreman.core.db.session import make_session_factory
from tests.conftest import BUSINESS_TABLES
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
STORED = "SELECT title_enc || coalesce(notes_enc, '') FROM assistant_followups WHERE id = :id"


def _row(bot_id: uuid.UUID, seq: int = 1, **kw: object) -> AssistantFollowup:
    values: dict[str, object] = {
        "bot_id": bot_id,
        "seq": seq,
        "category": "promised",
        "title_enc": f"周四前给李四报价 {SECRET}",
    }
    return AssistantFollowup(**{**values, **kw})


def test_table_is_truncated_before_bots() -> None:
    assert BUSINESS_TABLES.index("assistant_followups") < BUSINESS_TABLES.index("bots")


async def test_title_and_notes_are_sealed_and_read_back(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    row = _row(bot.id, notes_enc=f"备注-{SECRET}", due_on=date(2026, 11, 19))
    db_session.add(row)
    await db_session.commit()
    assert row.status == "open" and row.closed_at is None and row.job_id is None
    stored = str(await raw(db_session, STORED, id=row.id))
    assert SECRET not in stored and "李四" not in stored and stored.count("enc:v2:d1:") == 2
    # 分类、状态、日期是明文：按日期查询不用解密。
    assert await raw(
        db_session,
        "SELECT category || ':' || status || ':' || due_on::text FROM assistant_followups"
        " WHERE id = :id",
        id=row.id,
    ) == "promised:open:2026-11-19"
    async with make_session_factory(db_engine)() as other:
        found = await other.get(AssistantFollowup, row.id)
        assert found is not None and found.title_enc == f"周四前给李四报价 {SECRET}"
        assert found.notes_enc == f"备注-{SECRET}"
        # 密钥销毁之后：行还在、元数据可读，内容为空且不抛错。
        await keys.destroy_key(other, owner.id)
        await other.commit()
        locked = await other.get(AssistantFollowup, row.id, populate_existing=True)
        assert locked is not None and sealed.is_locked(locked)
        assert locked.title_enc == "" and locked.notes_enc is None
        assert (locked.seq, locked.category, locked.due_on) == (1, "promised", date(2026, 11, 19))


async def test_category_status_and_close_code_are_constrained(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    bot_id = bot.id
    for bad in ({"category": "todo"}, {"status": "closed"}, {"close_code": "because"}):
        db_session.add(_row(bot_id, **bad))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()
    db_session.add(_row(bot_id, status="dropped", close_code="origin_recalled"))
    await db_session.flush()


async def test_seq_is_unique_within_one_assistant(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    other, other_owner = await seed_assistant(db_session)
    for user in (owner, other_owner):
        await keys.ensure_key(db_session, sealed.master(), user.id)
    bot_id = bot.id
    # 两位助理各有自己的 F001。
    db_session.add_all([_row(bot_id, 1), _row(other.id, 1)])
    await db_session.commit()
    db_session.add(_row(bot_id, 1))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_followups_schema.py -q`
Expected: 收集阶段报错 `ImportError: cannot import name 'AssistantFollowup' from 'coreman.core.db.models'`

- [ ] **Step 3: 模型**

`coreman/core/db/models/assistants.py`：导入区把 `from datetime import datetime` 改成 `from datetime import date, datetime`，并在 `from sqlalchemy import (` 的清单里按字母序加上 `Date,`（在 `CheckConstraint,` 之后）。文件末尾追加：

```python
FOLLOWUP_CATEGORIES = ("decide", "promised", "waiting", "remind")
FOLLOWUP_STATUSES = ("open", "snoozed", "done", "dropped")
FOLLOWUP_CLOSE_CODES = ("origin_recalled",)


class AssistantFollowup(Base):
    """助理替所有者记着的一件跨天的事：等他决定、他答应了别人、在等别人、到日子提醒他。

    `title_enc` / `notes_enc` 登记在 `sealed.COLUMNS`：库里是密文，经会话取出的行上是明文。
    分类、状态、两个日期、编号是明文，按日期查询不解密。`seq` 在一位助理内递增，显示为 F012。
    `snoozed` 的条目到 `review_on` 那天由读取方改回 `open`。`close_code` 只在平台自动关闭时有值。
    `job_id` 是第 3 期子任务的编号，这里只是普通列。
    """

    __tablename__ = "assistant_followups"
    __table_args__ = (
        CheckConstraint(enum_check("category", FOLLOWUP_CATEGORIES), name="category"),
        CheckConstraint(enum_check("status", FOLLOWUP_STATUSES), name="status"),
        CheckConstraint(
            f"close_code IS NULL OR {enum_check('close_code', FOLLOWUP_CLOSE_CODES)}",
            name="close_code",
        ),
        UniqueConstraint("bot_id", "seq", name="uq_assistant_followups_bot_seq"),
        Index("assistant_followups_bot_status_idx", "bot_id", "status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    review_on: Mapped[date | None] = mapped_column(Date)
    due_on: Mapped[date | None] = mapped_column(Date)
    origin_message_id: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    title_enc: Mapped[str] = mapped_column(Text)
    notes_enc: Mapped[str | None] = mapped_column(Text)
    close_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
    updated_at: Mapped[datetime] = _ts(server_default=text("now()"))
    closed_at: Mapped[datetime | None] = _ts()
```

`coreman/core/db/models/__init__.py`：在 `from coreman.core.db.models.assistants import (` 的清单里加上 `FOLLOWUP_CATEGORIES,`、`FOLLOWUP_CLOSE_CODES,`、`FOLLOWUP_STATUSES,`、`AssistantFollowup,`（按现有的排序放），并在文件末尾追加：

```python
__all__ += [
    "FOLLOWUP_CATEGORIES",
    "FOLLOWUP_CLOSE_CODES",
    "FOLLOWUP_STATUSES",
    "AssistantFollowup",
]
```

- [ ] **Step 4: 迁移**

`migrations/versions/0096_assistant_followups.py`：

```python
"""Personal assistant follow-ups: numbered items with plaintext category, status and dates."""

import sqlalchemy as sa
from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None

CATEGORIES = "category IN ('decide', 'promised', 'waiting', 'remind')"
STATUSES = "status IN ('open', 'snoozed', 'done', 'dropped')"
CLOSE_CODES = "close_code IS NULL OR close_code IN ('origin_recalled')"


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "assistant_followups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("review_on", sa.Date(), nullable=True),
        sa.Column("due_on", sa.Date(), nullable=True),
        sa.Column("origin_message_id", sa.Text(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("title_enc", sa.Text(), nullable=False),
        sa.Column("notes_enc", sa.Text(), nullable=True),
        sa.Column("close_code", sa.Text(), nullable=True),
        _ts("created_at", server_default=sa.text("now()"), nullable=False),
        _ts("updated_at", server_default=sa.text("now()"), nullable=False),
        _ts("closed_at", nullable=True),
        sa.CheckConstraint(CATEGORIES, name="category"),
        sa.CheckConstraint(STATUSES, name="status"),
        sa.CheckConstraint(CLOSE_CODES, name="close_code"),
        sa.UniqueConstraint("bot_id", "seq", name="uq_assistant_followups_bot_seq"),
    )
    op.create_index(
        "assistant_followups_bot_status_idx", "assistant_followups", ["bot_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("assistant_followups_bot_status_idx", table_name="assistant_followups")
    op.drop_table("assistant_followups")
```

- [ ] **Step 5: 登记内容列、清表顺序与登记清单**

`coreman/core/assistants/sealed.py`：在 `from coreman.core.db.models import (` 的清单里加 `AssistantFollowup,`（`AssistantApproval,` 之后）；在 `COLUMNS` 里 `AssistantTranscript: {"blob_enc": Blob()},` 之后加：

```python
    # 待跟进：标题与备注。标题非空，读不出来时给空串。
    AssistantFollowup: {"title_enc": Text(""), "notes_enc": Text()},
```

`tests/conftest.py`：在 `BUSINESS_TABLES = [` 之后的第一行加 `"assistant_followups",`（必须排在 `"bots"` 之前）。

`tests/unit/test_assistant_sealed_codec.py`：导入清单加 `AssistantFollowup,`；`test_every_content_column_is_registered` 的集合里加 `AssistantFollowup,`；在 `assert set(sealed.COLUMNS[AssistantTranscript]) == {"blob_enc"}` 之后加一行：

```python
    assert set(sealed.COLUMNS[AssistantFollowup]) == {"title_enc", "notes_enc"}
```

（2A、2B 已经往这个集合里加过它们的模型时，保留它们的，只追加这一个。）

- [ ] **Step 6: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_followups_schema.py tests/unit/test_assistant_sealed_codec.py tests/unit/test_assistant_sealed_ledger.py tests/integration/test_migrations.py -q`
Expected: 全部 PASS（台账此时不变：还没有代码读写这张表）

Run: `uv run alembic check`
Expected: `No new upgrade operations detected.`

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 无报错

- [ ] **Step 7: 提交**

```bash
git add migrations/versions/0096_assistant_followups.py coreman/core/db/models/assistants.py \
  coreman/core/db/models/__init__.py coreman/core/assistants/sealed.py tests/conftest.py \
  tests/unit/test_assistant_sealed_codec.py tests/integration/test_assistant_followups_schema.py
git commit -m "个人助理待跟进：表 assistant_followups 与内容列登记"
```

---

### Task 2: `followups.py`：新增、修改、推后、关闭，编号与上限

**Files:**
- Create: `coreman/core/assistants/followups.py`
- Modify: `tests/unit/test_assistant_sealed_ledger.py`（`ENTITY_SELECTS`、`WRITE_STATEMENTS`）
- Test: `tests/integration/test_assistant_followups.py`

**Interfaces:**
- Consumes: Task 1 的 `AssistantFollowup`、`FOLLOWUP_CATEGORIES`；`coreman.core.timeutils.utcnow() -> datetime`；`SealedSession.scalar`（取单个实体时自动还原内容列）；flush 钩子（经 ORM 写入时封存）。
- Produces（`coreman.core.assistants.followups`）：
  - 常量 `MAX_OPEN = 500`、`MAX_TITLE = 200`、`MAX_NOTES = 4000`、`LIVE = ("open", "snoozed")`、`KEEP`（「不改这一项」的哨兵）
  - `class FollowupError(Exception)`，属性 `code: str`。代码：`invalid_category`、`invalid_title`、`invalid_notes`、`too_many_followups`、`followup_not_found`、`followup_closed`、`snooze_not_future`
  - `label(seq: int) -> str`（`12 -> "F012"`）、`parse_label(value: str) -> int`（接受 `F012`、`f12`、`12`；不合法抛 `followup_not_found`）
  - `async add(session, bot_id, *, category: str, title: str, notes: str | None = None, review_on: date | None = None, due_on: date | None = None, origin_message_id: str | None = None, job_id: uuid.UUID | None = None) -> AssistantFollowup`
  - `async get(session, bot_id, seq: int, *, lock: bool = False) -> AssistantFollowup`
  - `async update(session, bot_id, seq, *, category: str | None = None, title: str | None = None, notes: str | None | Keep = KEEP, review_on: date | None | Keep = KEEP, due_on: date | None | Keep = KEEP) -> AssistantFollowup`
  - `async snooze(session, bot_id, seq, until: date, *, today: date) -> AssistantFollowup`
  - `async close(session, bot_id, seq, *, dropped: bool = False, notes: str | None = None) -> AssistantFollowup`
  - `view(row: AssistantFollowup, *, notes: bool = True) -> dict[str, Any]`
  - 所有函数只 flush，不提交；调用方负责提交。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_followups.py`：

```python
"""待跟进的增改：编号、密文、按助理隔离、并发不重号、未结上限。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import followups, keys, sealed
from coreman.core.crypto import DecryptError
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
TODAY = date(2026, 11, 13)
STORED = (
    "SELECT string_agg(title_enc || coalesce(notes_enc, ''), ' ') FROM assistant_followups"
    " WHERE bot_id = :bot"
)


async def _assistant(session: AsyncSession) -> uuid.UUID:
    """一位开通好、已有数据密钥的助理。"""
    bot, owner = await seed_assistant(session)
    await keys.ensure_key(session, sealed.master(), owner.id)
    await session.commit()
    return bot.id


async def _code(awaitable: object) -> str:
    with pytest.raises(followups.FollowupError) as caught:
        await awaitable  # type: ignore[misc]
    return caught.value.code


def test_labels() -> None:
    assert followups.label(12) == "F012" and followups.label(1234) == "F1234"
    assert [followups.parse_label(v) for v in ("F012", "f12", " 12 ")] == [12, 12, 12]
    for bad in ("F0", "X12", "", "F-1"):
        with pytest.raises(followups.FollowupError):
            followups.parse_label(bad)
    assert followups.MAX_OPEN == 500


async def test_add_numbers_items_and_stores_ciphertext(db_session: AsyncSession) -> None:
    bot_id = await _assistant(db_session)
    first = await followups.add(
        db_session,
        bot_id,
        category="promised",
        title=f"  周四前给李四报价 {SECRET}  ",
        notes=f"他周一问的。{SECRET}",
        due_on=date(2026, 11, 19),
        origin_message_id="om_1",
    )
    second = await followups.add(db_session, bot_id, category="decide", title="是否续约")
    await db_session.commit()
    assert (first.seq, second.seq) == (1, 2)
    assert first.status == "open" and first.title_enc == f"周四前给李四报价 {SECRET}"
    stored = str(await raw(db_session, STORED, bot=bot_id))
    assert SECRET not in stored and "李四" not in stored and "续约" not in stored
    assert stored.count("enc:v2:d1:") == 3
    assert followups.view(first) == {
        "id": "F001",
        "category": "promised",
        "status": "open",
        "title": f"周四前给李四报价 {SECRET}",
        "review_on": None,
        "due_on": "2026-11-19",
        "origin_message_id": "om_1",
        "created_at": first.created_at.isoformat(),
        "notes": f"他周一问的。{SECRET}",
    }
    assert "notes" not in followups.view(first, notes=False)
    assert followups.view(first, notes=False)["has_notes"] is True


async def test_add_rejects_bad_input(db_session: AsyncSession) -> None:
    bot_id = await _assistant(db_session)
    assert await _code(followups.add(db_session, bot_id, category="todo", title="x")) == (
        "invalid_category"
    )
    for title in ("   ", "长" * (followups.MAX_TITLE + 1)):
        assert await _code(followups.add(db_session, bot_id, category="remind", title=title)) == (
            "invalid_title"
        )
    too_long = "备" * (followups.MAX_NOTES + 1)
    assert await _code(
        followups.add(db_session, bot_id, category="remind", title="x", notes=too_long)
    ) == "invalid_notes"


async def test_update_snooze_and_close(db_session: AsyncSession) -> None:
    bot_id = await _assistant(db_session)
    await followups.add(db_session, bot_id, category="waiting", title="等王五的合同")
    row = await followups.update(
        db_session,
        bot_id,
        1,
        category="promised",
        title=f"合同回传 {SECRET}",
        notes="已催过一次",
        due_on=date(2026, 11, 20),
    )
    await db_session.commit()
    assert (row.category, row.title_enc, row.notes_enc) == (
        "promised",
        f"合同回传 {SECRET}",
        "已催过一次",
    )
    assert row.due_on == date(2026, 11, 20) and row.review_on is None
    assert SECRET not in str(await raw(db_session, STORED, bot=bot_id))
    # 没给的项不动；给 None 才清空。
    row = await followups.update(db_session, bot_id, 1, notes=None)
    assert row.notes_enc is None and row.title_enc == f"合同回传 {SECRET}"
    assert row.due_on == date(2026, 11, 20)

    # 推后必须是今天之后的某一天。
    assert await _code(followups.snooze(db_session, bot_id, 1, TODAY, today=TODAY)) == (
        "snooze_not_future"
    )
    row = await followups.snooze(db_session, bot_id, 1, date(2026, 11, 16), today=TODAY)
    assert (row.status, row.review_on) == ("snoozed", date(2026, 11, 16))
    # 清掉推后条目的回看日期：它不会自己醒，所以直接改回未结。
    row = await followups.update(db_session, bot_id, 1, review_on=None)
    assert (row.status, row.review_on) == ("open", None)

    row = await followups.close(db_session, bot_id, 1, notes="合同已收到")
    await db_session.commit()
    assert row.status == "done" and row.closed_at is not None and row.notes_enc == "合同已收到"
    assert row.close_code is None
    # 关了的不能再改、再推后；再关一次原样返回。
    assert await _code(followups.update(db_session, bot_id, 1, title="x")) == "followup_closed"
    assert await _code(
        followups.snooze(db_session, bot_id, 1, date(2026, 11, 16), today=TODAY)
    ) == "followup_closed"
    again = await followups.close(db_session, bot_id, 1, dropped=True)
    assert again.status == "done"

    await followups.add(db_session, bot_id, category="remind", title="不用了的事")
    dropped = await followups.close(db_session, bot_id, 2, dropped=True)
    assert dropped.status == "dropped"
    assert await _code(followups.get(db_session, bot_id, 9)) == "followup_not_found"


async def test_another_assistant_cannot_see_or_change_it(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    mine = await _assistant(db_session)
    theirs = await _assistant(db_session)
    await followups.add(db_session, mine, category="decide", title=f"我的事 {SECRET}")
    await db_session.commit()
    # 对方没有 F001：读、改、推后、关闭都找不到。
    for attempt in (
        followups.get(db_session, theirs, 1),
        followups.update(db_session, theirs, 1, title="改掉"),
        followups.snooze(db_session, theirs, 1, date(2026, 11, 16), today=TODAY),
        followups.close(db_session, theirs, 1),
    ):
        assert await _code(attempt) == "followup_not_found"
    untouched = await followups.get(db_session, mine, 1)
    assert untouched.status == "open" and untouched.title_enc == f"我的事 {SECRET}"
    # 对方的编号从自己的 1 开始。
    own = await followups.add(db_session, theirs, category="remind", title="对方的事")
    await db_session.commit()
    assert own.seq == 1
    # 把我的密文挪到对方的行上也解不开：密钥按人，密文绑定助理。
    await db_session.execute(
        text(
            "UPDATE assistant_followups SET title_enc ="
            " (SELECT title_enc FROM assistant_followups WHERE bot_id = :mine)"
            " WHERE bot_id = :theirs"
        ),
        {"mine": mine, "theirs": theirs},
    )
    await db_session.commit()
    async with make_session_factory(db_engine)() as other:
        with pytest.raises(DecryptError):
            await followups.get(other, theirs, 1)


async def test_concurrent_adds_never_share_a_number(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot_id = await _assistant(db_session)
    factory = make_session_factory(db_engine)

    async def one(index: int) -> int:
        async with factory() as session:
            row = await followups.add(session, bot_id, category="remind", title=f"第 {index} 件")
            # 拿到编号后不马上提交：没有锁的话别的事务会在这时取到同一个编号。
            await asyncio.sleep(0.05)
            await session.commit()
            return row.seq

    seqs = await asyncio.gather(*(one(index) for index in range(6)))
    assert sorted(seqs) == [1, 2, 3, 4, 5, 6]


async def test_open_items_are_capped(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(followups, "MAX_OPEN", 3)
    bot_id = await _assistant(db_session)
    for index in range(2):
        await followups.add(db_session, bot_id, category="remind", title=f"第 {index} 件")
    await followups.add(db_session, bot_id, category="waiting", title="第三件")
    # 推后的也算未结。
    await followups.snooze(db_session, bot_id, 3, date(2026, 11, 16), today=TODAY)
    assert await _code(followups.add(db_session, bot_id, category="remind", title="第四件")) == (
        "too_many_followups"
    )
    # 关掉一条才有名额；编号接着往下走，不回收。
    await followups.close(db_session, bot_id, 1)
    row = await followups.add(db_session, bot_id, category="remind", title="第四件")
    assert row.seq == 4
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_followups.py -q`
Expected: 收集阶段报错 `ImportError: cannot import name 'followups' from 'coreman.core.assistants'`

- [ ] **Step 3: 实现**

`coreman/core/assistants/followups.py`：

```python
"""个人助理的待跟进：助理替所有者记着的跨天的事，按助理编号（F012）。

标题与备注经 ORM 读写，由 `sealed` 的 flush 钩子封存、会话取出时还原；分类、状态、日期、编号
是明文。所有函数按 `bot_id` 过滤：一位助理看不到也改不了别人的条目。只 flush，不提交。
"""

from __future__ import annotations

import enum
import re
import uuid
from datetime import date
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed
from coreman.core.db.models import FOLLOWUP_CATEGORIES, AssistantFollowup
from coreman.core.timeutils import utcnow

# 每位助理未结（open + snoozed）条目的上限。
MAX_OPEN = 500
MAX_TITLE = 200
MAX_NOTES = 4000
LIVE = ("open", "snoozed")
_LABEL = re.compile(r"^[Ff]?(\d{1,9})$")


class Keep(enum.Enum):
    KEEP = "keep"


# 「不改这一项」：与 None（清空）区分开。
KEEP = Keep.KEEP


class FollowupError(Exception):
    """调用方能照着改的拒绝；`code` 原样作为工具的 error 返回，不带任何内容。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def label(seq: int) -> str:
    return f"F{seq:03d}"


def parse_label(value: str) -> int:
    match = _LABEL.match(value.strip())
    if match is None or int(match.group(1)) < 1:
        raise FollowupError("followup_not_found")
    return int(match.group(1))


def view(row: AssistantFollowup, *, notes: bool = True) -> dict[str, Any]:
    """给助理（以及之后的页面）看的一条；`notes` 为假时只说有没有备注。"""
    out: dict[str, Any] = {
        "id": label(row.seq),
        "category": row.category,
        "status": row.status,
        "title": row.title_enc,
        "review_on": row.review_on.isoformat() if row.review_on else None,
        "due_on": row.due_on.isoformat() if row.due_on else None,
        "origin_message_id": row.origin_message_id,
        "created_at": row.created_at.isoformat(),
    }
    if notes:
        out["notes"] = row.notes_enc
    else:
        out["has_notes"] = bool(row.notes_enc)
    if row.close_code:
        out["close_code"] = row.close_code
    if sealed.is_locked(row):
        out["locked"] = True
    return out


def _title(value: str) -> str:
    title = value.strip()
    if not title or len(title) > MAX_TITLE:
        raise FollowupError("invalid_title")
    return title


def _notes(value: str | None) -> str | None:
    if value is not None and len(value) > MAX_NOTES:
        raise FollowupError("invalid_notes")
    return value or None


def _category(value: str) -> str:
    if value not in FOLLOWUP_CATEGORIES:
        raise FollowupError("invalid_category")
    return value


async def _lock(session: AsyncSession, bot_id: uuid.UUID) -> None:
    """这位助理的编号与上限在本事务内独占；不锁 bots 的行，那一行别处一直在更新。"""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"assistant-followups:{bot_id}"},
    )


async def add(
    session: AsyncSession,
    bot_id: uuid.UUID,
    *,
    category: str,
    title: str,
    notes: str | None = None,
    review_on: date | None = None,
    due_on: date | None = None,
    origin_message_id: str | None = None,
    job_id: uuid.UUID | None = None,
) -> AssistantFollowup:
    """记一条，编号取这位助理的下一个。

    Raises:
        FollowupError: invalid_category / invalid_title / invalid_notes / too_many_followups
    """
    category, title, notes = _category(category), _title(title), _notes(notes)
    await _lock(session, bot_id)
    live = await session.scalar(
        select(func.count())
        .select_from(AssistantFollowup)
        .where(AssistantFollowup.bot_id == bot_id, AssistantFollowup.status.in_(LIVE))
    )
    if (live or 0) >= MAX_OPEN:
        raise FollowupError("too_many_followups")
    last = await session.scalar(
        select(func.max(AssistantFollowup.seq)).where(AssistantFollowup.bot_id == bot_id)
    )
    row = AssistantFollowup(
        bot_id=bot_id,
        seq=(last or 0) + 1,
        category=category,
        status="open",
        title_enc=title,
        notes_enc=notes,
        review_on=review_on,
        due_on=due_on,
        origin_message_id=origin_message_id,
        job_id=job_id,
    )
    session.add(row)
    await session.flush()
    return row


async def get(
    session: AsyncSession, bot_id: uuid.UUID, seq: int, *, lock: bool = False
) -> AssistantFollowup:
    """这位助理的第 `seq` 条，内容已还原。

    Raises:
        FollowupError: followup_not_found
    """
    stmt = select(AssistantFollowup).where(
        AssistantFollowup.bot_id == bot_id, AssistantFollowup.seq == seq
    )
    if lock:
        stmt = stmt.with_for_update()
    # session.scalar 取单个实体：SealedSession 自动还原内容列。
    row: AssistantFollowup | None = await session.scalar(stmt)
    if row is None:
        raise FollowupError("followup_not_found")
    return row


async def _live(session: AsyncSession, bot_id: uuid.UUID, seq: int) -> AssistantFollowup:
    row = await get(session, bot_id, seq, lock=True)
    if row.status not in LIVE:
        raise FollowupError("followup_closed")
    return row


async def update(
    session: AsyncSession,
    bot_id: uuid.UUID,
    seq: int,
    *,
    category: str | None = None,
    title: str | None = None,
    notes: str | None | Keep = KEEP,
    review_on: date | None | Keep = KEEP,
    due_on: date | None | Keep = KEEP,
) -> AssistantFollowup:
    """改一条未结的；没给的项不动，`notes` / `review_on` / `due_on` 给 None 是清空。

    Raises:
        FollowupError: followup_not_found / followup_closed / invalid_*
    """
    row = await _live(session, bot_id, seq)
    if category is not None:
        row.category = _category(category)
    if title is not None:
        row.title_enc = _title(title)
    if not isinstance(notes, Keep):
        row.notes_enc = _notes(notes)
    if not isinstance(due_on, Keep):
        row.due_on = due_on
    if not isinstance(review_on, Keep):
        row.review_on = review_on
        if review_on is None and row.status == "snoozed":
            # 没有回看日期的推后条目永远不会醒。
            row.status = "open"
    row.updated_at = utcnow()
    await session.flush()
    return row


async def snooze(
    session: AsyncSession, bot_id: uuid.UUID, seq: int, until: date, *, today: date
) -> AssistantFollowup:
    """推到 `until` 那天再看；到那天由 `due` / `list_` 改回未结。

    Args:
        today: 所有者时区的今天

    Raises:
        FollowupError: snooze_not_future / followup_not_found / followup_closed
    """
    if until <= today:
        raise FollowupError("snooze_not_future")
    row = await _live(session, bot_id, seq)
    row.status, row.review_on, row.updated_at = "snoozed", until, utcnow()
    await session.flush()
    return row


async def close(
    session: AsyncSession,
    bot_id: uuid.UUID,
    seq: int,
    *,
    dropped: bool = False,
    notes: str | None = None,
) -> AssistantFollowup:
    """办完（或 `dropped`：不再需要）。已经关了的原样返回。

    Args:
        notes: 给了就替换备注（怎么了结的）

    Raises:
        FollowupError: followup_not_found / invalid_notes
    """
    row = await get(session, bot_id, seq, lock=True)
    if row.status not in LIVE:
        return row
    if notes is not None:
        row.notes_enc = _notes(notes)
    row.status = "dropped" if dropped else "done"
    row.closed_at = row.updated_at = utcnow()
    await session.flush()
    return row
```

- [ ] **Step 4: 更新台账**

`tests/unit/test_assistant_sealed_ledger.py`：

`ENTITY_SELECTS` 里加（放在 `"coreman/core/assistants/approvals/service.py": 6,` 之后）：

```python
    "coreman/core/assistants/followups.py": 1,  # 取一条：session.scalar，自动还原
```

`WRITE_STATEMENTS` 里加（放在 `"coreman/core/assistants/approvals/service.py": 2,` 之后）：

```python
    "coreman/core/assistants/followups.py": 1,  # 待跟进：经 ORM 新建，flush 钩子封存
```

`COLUMN_REFERENCES`、`RAW_STATEMENTS` 不加：模块里没有 `AssistantFollowup.title_enc` / `.notes_enc` 这样的语句级引用（只在取出的行上读写属性），咨询锁那条裸 SQL 不碰内容表。

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_followups.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: 全部 PASS

验证并发用例确实在测锁：临时注释掉 `add` 里的 `await _lock(session, bot_id)`，再跑 `uv run pytest tests/integration/test_assistant_followups.py::test_concurrent_adds_never_share_a_number -q`，Expected: FAIL（`IntegrityError`，违反 `uq_assistant_followups_bot_seq`）。恢复那一行后重跑，Expected: PASS。

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 无报错

- [ ] **Step 6: 提交**

```bash
git add coreman/core/assistants/followups.py tests/integration/test_assistant_followups.py \
  tests/unit/test_assistant_sealed_ledger.py
git commit -m "个人助理待跟进：增改、推后、关闭，编号并发安全，未结上限 500"
```

---

### Task 3: 列表、到期、推后到期、撤回自动关闭

**Files:**
- Modify: `coreman/core/assistants/followups.py`（导入区、常量、文件末尾追加函数）
- Modify: `tests/unit/test_assistant_sealed_ledger.py`（Task 2 加的两条改数）
- Test: `tests/integration/test_assistant_followups_queries.py`

**Interfaces:**
- Consumes: Task 2 的 `followups.add` / `snooze` / `close` / `get`、`LIVE`、`FollowupError`；`sealed.open_rows(session, rows)`（`session.scalars` 取出的行要显式还原）。
- Produces（`coreman.core.assistants.followups`）：
  - `SCOPES = ("today", "open", "all", "decide", "promised", "waiting", "remind")`、`RECALLED = "origin_recalled"`
  - `async due(session, bot_id: uuid.UUID, today: date) -> list[AssistantFollowup]`：状态 `open` 且 `review_on <= today` 或 `due_on <= today`，按编号升序，内容已还原。调用前先把到期的 `snoozed` 改回 `open`。幂等：不记「提醒过」。
  - `async list_(session, bot_id, scope: str, *, today: date, limit: int = 50) -> list[AssistantFollowup]`：`today` 同 `due`；`open` 是全部未结（含 `snoozed`）；`all` 含已关闭、编号倒序；四个分类名是该分类的未结。不认识的 `scope` 抛 `FollowupError("invalid_scope")`。
  - `async close_for_message(session, bot_id, message_id: str) -> list[AssistantFollowup]`：把这位助理由这条消息引起、还未结的条目记为 `dropped`、`close_code = "origin_recalled"`，返回被关闭的行（内容已还原，供调用方告诉助理是哪几条）；没有时返回空列表。2D/2E 发现撤回时调用。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_followups_queries.py`：

```python
"""待跟进的读取：各个 scope、当天到期、推后到期后重新出现、起因消息撤回自动关闭。"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, keys, sealed
from coreman.core.db.models import AssistantFollowup
from tests.integration.assistant_helpers import raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
TODAY = date(2026, 11, 13)


async def _assistant(session: AsyncSession) -> uuid.UUID:
    bot, owner = await seed_assistant(session)
    await keys.ensure_key(session, sealed.master(), owner.id)
    await session.commit()
    return bot.id


def _seqs(rows: list[AssistantFollowup]) -> list[int]:
    return [row.seq for row in rows]


async def _seed(session: AsyncSession, bot_id: uuid.UUID) -> None:
    """F001 今天到期；F002 下周到期；F003 回看日已过；F004 没有日期；F005 推到 16 号；F006 已办完。"""
    add = followups.add
    await add(session, bot_id, category="decide", title="定供应商", due_on=TODAY)
    await add(session, bot_id, category="promised", title="给李四报价", due_on=date(2026, 11, 19))
    await add(session, bot_id, category="waiting", title="等合同", review_on=date(2026, 11, 12))
    await add(session, bot_id, category="remind", title="续费")
    await add(session, bot_id, category="remind", title="体检")
    await add(session, bot_id, category="decide", title="已经定了的事")
    await followups.snooze(session, bot_id, 5, date(2026, 11, 16), today=TODAY)
    await followups.close(session, bot_id, 6)
    await session.commit()


async def test_scopes(db_session: AsyncSession) -> None:
    bot_id = await _assistant(db_session)
    await _seed(db_session, bot_id)

    async def listed(scope: str, **kw: int) -> list[AssistantFollowup]:
        return await followups.list_(db_session, bot_id, scope, today=TODAY, **kw)

    today = await listed("today")
    assert _seqs(today) == [1, 3]
    # 取出来的是明文。
    assert [row.title_enc for row in today] == ["定供应商", "等合同"]
    assert _seqs(await listed("open")) == [1, 2, 3, 4, 5]
    assert [row.status for row in await listed("open")][-1] == "snoozed"
    assert _seqs(await listed("all")) == [6, 5, 4, 3, 2, 1]
    assert _seqs(await listed("decide")) == [1]
    assert _seqs(await listed("remind")) == [4, 5]
    assert _seqs(await listed("promised")) == [2]
    assert _seqs(await listed("waiting")) == [3]
    assert _seqs(await listed("open", limit=2)) == [1, 2]
    assert _seqs(await listed("all", limit=2)) == [6, 5]
    with pytest.raises(followups.FollowupError) as caught:
        await listed("everything")
    assert caught.value.code == "invalid_scope"
    assert followups.SCOPES == ("today", "open", "all", "decide", "promised", "waiting", "remind")


async def test_due_follows_the_calendar(db_session: AsyncSession) -> None:
    bot_id = await _assistant(db_session)
    await _seed(db_session, bot_id)
    assert _seqs(await followups.due(db_session, bot_id, date(2026, 11, 11))) == []
    assert _seqs(await followups.due(db_session, bot_id, date(2026, 11, 12))) == [3]
    assert _seqs(await followups.due(db_session, bot_id, TODAY)) == [1, 3]
    # 同一天再问一次还是这些：到期不是一次性的。
    assert _seqs(await followups.due(db_session, bot_id, TODAY)) == [1, 3]
    assert _seqs(await followups.due(db_session, bot_id, date(2026, 11, 19))) == [1, 2, 3, 5]


async def test_a_snoozed_item_comes_back_on_its_day(db_session: AsyncSession) -> None:
    bot_id = await _assistant(db_session)
    await _seed(db_session, bot_id)
    # 今天到期的 F001 推到明天：今天不再出现，哪怕期限就是今天。
    await followups.snooze(db_session, bot_id, 1, date(2026, 11, 14), today=TODAY)
    await db_session.commit()
    assert _seqs(await followups.due(db_session, bot_id, TODAY)) == [3]
    status = "SELECT status FROM assistant_followups WHERE bot_id = :bot AND seq = :seq"
    assert await raw(db_session, status, bot=bot_id, seq=1) == "snoozed"
    back = await followups.due(db_session, bot_id, date(2026, 11, 14))
    await db_session.commit()
    assert _seqs(back) == [1, 3] and back[0].status == "open"
    # 库里的状态也改回来了；回看日期留着。
    assert await raw(db_session, status, bot=bot_id, seq=1) == "open"
    assert back[0].review_on == date(2026, 11, 14)
    # F005 还没到 16 号。
    assert await raw(db_session, status, bot=bot_id, seq=5) == "snoozed"
    listed = await followups.list_(db_session, bot_id, "remind", today=date(2026, 11, 16))
    assert [(row.seq, row.status) for row in listed] == [(4, "open"), (5, "open")]


async def test_other_assistants_are_not_listed(db_session: AsyncSession) -> None:
    mine = await _assistant(db_session)
    theirs = await _assistant(db_session)
    await _seed(db_session, mine)
    for scope in followups.SCOPES:
        assert await followups.list_(db_session, theirs, scope, today=TODAY) == []
    assert await followups.due(db_session, theirs, TODAY) == []
    # 对方读清单（会唤醒到期的推后条目）不动我的行。
    await followups.due(db_session, theirs, date(2026, 12, 1))
    assert (await followups.get(db_session, mine, 5)).status == "snoozed"


async def test_recalled_origin_closes_its_followups(db_session: AsyncSession) -> None:
    mine = await _assistant(db_session)
    theirs = await _assistant(db_session)
    add = followups.add
    await add(db_session, mine, category="promised", title="回李四", origin_message_id="om_1")
    await add(db_session, mine, category="waiting", title="等李四", origin_message_id="om_1")
    await add(db_session, mine, category="remind", title="别的事", origin_message_id="om_2")
    await add(db_session, mine, category="decide", title="早办完了", origin_message_id="om_1")
    await add(db_session, mine, category="remind", title="没有起因")
    await followups.snooze(db_session, mine, 2, date(2026, 11, 16), today=TODAY)
    await followups.close(db_session, mine, 4)
    await add(db_session, theirs, category="remind", title="同号消息", origin_message_id="om_1")
    await db_session.commit()

    closed = await followups.close_for_message(db_session, mine, "om_1")
    await db_session.commit()
    assert _seqs(closed) == [1, 2]
    assert [row.title_enc for row in closed] == ["回李四", "等李四"]
    for row in closed:
        assert (row.status, row.close_code) == ("dropped", "origin_recalled")
        assert row.closed_at is not None
        assert followups.view(row)["close_code"] == "origin_recalled"
    # 别的消息引起的、早已办完的、没有起因的、别人的，都不动。
    assert (await followups.get(db_session, mine, 3)).status == "open"
    done = await followups.get(db_session, mine, 4)
    assert (done.status, done.close_code) == ("done", None)
    assert (await followups.get(db_session, mine, 5)).status == "open"
    assert (await followups.get(db_session, theirs, 1)).status == "open"
    # 关过了再来一次什么都不做；空的消息编号不匹配「没有起因」的条目。
    assert await followups.close_for_message(db_session, mine, "om_1") == []
    assert await followups.close_for_message(db_session, mine, "") == []
    assert _seqs(await followups.list_(db_session, mine, "open", today=TODAY)) == [3, 5]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_followups_queries.py -q`
Expected: 5 个用例 FAIL，`AttributeError: module 'coreman.core.assistants.followups' has no attribute 'list_'`（或 `due` / `close_for_message`）

- [ ] **Step 3: 实现**

`coreman/core/assistants/followups.py`：

导入区改成（多了 `ColumnElement`、`or_`、`sql_update`；模块里已有名为 `update` 的函数，所以 SQLAlchemy 的 `update` 换名导入）：

```python
from sqlalchemy import ColumnElement, func, or_, select, text
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession
```

在 `LIVE = ("open", "snoozed")` 之后加：

```python
SCOPES = ("today", "open", "all", *FOLLOWUP_CATEGORIES)
# 平台自动关闭的原因：起因消息被撤回。
RECALLED = "origin_recalled"
```

文件末尾追加：

```python
async def _wake(session: AsyncSession, bot_id: uuid.UUID, today: date) -> None:
    """推后的条目到了回看日：改回未结。只写状态与时间，不碰内容列。"""
    await session.execute(
        sql_update(AssistantFollowup)
        .where(
            AssistantFollowup.bot_id == bot_id,
            AssistantFollowup.status == "snoozed",
            AssistantFollowup.review_on <= today,
        )
        .values(status="open", updated_at=utcnow())
    )


async def _rows(
    session: AsyncSession,
    bot_id: uuid.UUID,
    *where: ColumnElement[bool],
    limit: int | None = None,
    newest: bool = False,
) -> list[AssistantFollowup]:
    stmt = select(AssistantFollowup).where(AssistantFollowup.bot_id == bot_id, *where)
    stmt = stmt.order_by(AssistantFollowup.seq.desc() if newest else AssistantFollowup.seq)
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = list(await session.scalars(stmt))
    # session.scalars 不自动还原。
    await sealed.open_rows(session, rows)
    return rows


def _due(today: date) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    return (
        AssistantFollowup.status == "open",
        or_(AssistantFollowup.review_on <= today, AssistantFollowup.due_on <= today),
    )


async def due(session: AsyncSession, bot_id: uuid.UUID, today: date) -> list[AssistantFollowup]:
    """今天该看的：未结且回看日或期限已到。幂等，同一天反复调用结果相同。

    Args:
        today: 所有者时区的今天
    """
    await _wake(session, bot_id, today)
    return await _rows(session, bot_id, *_due(today))


async def list_(
    session: AsyncSession, bot_id: uuid.UUID, scope: str, *, today: date, limit: int = 50
) -> list[AssistantFollowup]:
    """按范围列出：today（同 `due`）、open（全部未结，含推后的）、all（含已关闭，新的在前）、
    或一个分类名（该分类的未结）。

    Raises:
        FollowupError: invalid_scope
    """
    if scope not in SCOPES:
        raise FollowupError("invalid_scope")
    await _wake(session, bot_id, today)
    if scope == "today":
        return await _rows(session, bot_id, *_due(today), limit=limit)
    if scope == "all":
        return await _rows(session, bot_id, limit=limit, newest=True)
    where: list[ColumnElement[bool]] = [AssistantFollowup.status.in_(LIVE)]
    if scope != "open":
        where.append(AssistantFollowup.category == scope)
    return await _rows(session, bot_id, *where, limit=limit)


async def close_for_message(
    session: AsyncSession, bot_id: uuid.UUID, message_id: str
) -> list[AssistantFollowup]:
    """起因消息被撤回：这位助理由它引起、还未结的条目自动关闭。返回被关闭的行。"""
    if not message_id:
        return []
    rows = await _rows(
        session,
        bot_id,
        AssistantFollowup.status.in_(LIVE),
        AssistantFollowup.origin_message_id == message_id,
    )
    now = utcnow()
    for row in rows:
        row.status, row.close_code = "dropped", RECALLED
        row.closed_at = row.updated_at = now
    await session.flush()
    return rows
```

- [ ] **Step 4: 更新台账**

`tests/unit/test_assistant_sealed_ledger.py`，把 Task 2 加的两条改成：

```python
    # 待跟进：取一条是 session.scalar（自动还原）；列表显式还原
    "coreman/core/assistants/followups.py": 2,
```

（`ENTITY_SELECTS`）

```python
    # 待跟进：经 ORM 新建（flush 钩子封存）；另一条只把到期的推后条目改回未结
    "coreman/core/assistants/followups.py": 2,
```

（`WRITE_STATEMENTS`）

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_followups_queries.py tests/integration/test_assistant_followups.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: 全部 PASS

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 无报错

- [ ] **Step 6: 提交**

```bash
git add coreman/core/assistants/followups.py tests/integration/test_assistant_followups_queries.py \
  tests/unit/test_assistant_sealed_ledger.py
git commit -m "个人助理待跟进：按范围列出、当天到期、推后到期唤醒、起因撤回自动关闭"
```

---

### Task 4: 工具 `followup_*`，注册进 `knowledge` 组

**Files:**
- Create: `coreman/core/assistants/followup_tools.py`
- Modify: `coreman/api/routers/feishu_personal.py`（导入区加一行，触发注册）
- Test: `tests/integration/test_assistant_followup_tools.py`
- Test: `tests/unit/test_assistant_followup_tools_loaded.py`

**Interfaces:**
- Consumes:
  - Task 2、3 的 `followups.add` / `get` / `update` / `snooze` / `close` / `list_` / `view` / `parse_label` / `SCOPES` / `MAX_OPEN` / `MAX_TITLE` / `MAX_NOTES` / `FollowupError`
  - 2A（见「对 2A 的假定」）：`tools.register(group, definitions: list[dict], dispatch)`、`tools.definitions(base, *, groups)`、`settings.get(session, bot_id) -> AssistantSettings`、`settings.local_today(settings, now) -> date`
  - `coreman.core.feishu_personal.toolbase`：`Arguments`（`extra="forbid"`、`strict=True`）、`Day`（`YYYY-MM-DD` 字符串）、`Identifier`、`input_schema(args) -> dict`、`valid_days(*names)`
  - `coreman.core.feishu_personal.policy.Scope`（用到 `scope.bot.id`）
  - 测试：`tests.integration.approval_helpers.assistant_turn(session) -> tuple[Bot, User, Scope]`（一位已有数据密钥的所有者与他的助理的一轮）
- Produces（`coreman.core.assistants.followup_tools`）：
  - `OWN: dict[str, tuple[type[Arguments], str]]`（五个工具名 → 入参模型与说明）
  - `definitions() -> list[dict[str, Any]]`
  - `async dispatch(session: AsyncSession, cipher: Cipher, scope: Scope, name: str, arguments: dict[str, Any]) -> dict[str, Any]`：返回结果或 `{"error": <代码>, ...}`；不是这里的工具名返回 `{"error": "invalid_tool_or_arguments"}`。成功与业务错误的结果里都带 `today`（所有者时区的今天，`YYYY-MM-DD`）。
  - 模块导入时执行 `tools.register("knowledge", definitions(), dispatch)`。
  - 结果形状：`followup_add` / `_update` / `_snooze` / `_close` → `{"followup": view(row), "today": …}`；`followup_list` → `{"items": [view(row, notes=False), …], "truncated": bool, "today": …}`，`scope` 是一个编号时 → `{"followup": view(row), "today": …}`。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_followup_tools.py`：

```python
"""待跟进工具：经分派读写，日期按所有者时区，错误只给代码。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followup_tools, followups
from coreman.core.assistants import settings as assistant_settings
from coreman.core.feishu_personal.policy import Scope
from tests.integration.approval_helpers import assistant_turn
from tests.integration.assistant_helpers import SECRET, raw

pytestmark = pytest.mark.usefixtures("assistant_master")
# 待跟进工具不用平台主密钥：内容由会话按所有者的数据密钥加解密。
NO_CIPHER: Any = None
STORED = "SELECT title_enc || notes_enc FROM assistant_followups WHERE bot_id = :bot"


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # UTC 11 月 13 日 02:00：上海是 13 日上午，纽约还是 12 日晚上。
    monkeypatch.setattr(followup_tools, "utcnow", lambda: datetime(2026, 11, 13, 2, 0, tzinfo=UTC))


async def call(session: AsyncSession, scope: Scope, name: str, **arguments: Any) -> dict[str, Any]:
    out = await followup_tools.dispatch(session, NO_CIPHER, scope, name, arguments)
    await session.commit()
    return out


async def test_add_list_update_snooze_close(db_session: AsyncSession) -> None:
    bot, _, scope = await assistant_turn(db_session)
    added = await call(
        db_session,
        scope,
        "followup_add",
        category="promised",
        title=f"周四前给李四报价 {SECRET}",
        notes="他周一问的",
        due_on="2026-11-13",
        origin_message_id="om_2",
    )
    assert added["today"] == "2026-11-13"
    assert added["followup"]["id"] == "F001" and added["followup"]["status"] == "open"
    assert added["followup"]["notes"] == "他周一问的"
    stored = await raw(db_session, STORED, bot=bot.id)
    assert SECRET not in str(stored) and "李四" not in str(stored)
    await call(db_session, scope, "followup_add", category="remind", title="续费")

    # 清单里只说有没有备注；按编号取一条才给全文。scope 不给时是 today。
    listed = await call(db_session, scope, "followup_list")
    assert [item["id"] for item in listed["items"]] == ["F001"]
    assert listed["items"][0]["has_notes"] is True and "notes" not in listed["items"][0]
    assert listed["truncated"] is False
    one = await call(db_session, scope, "followup_list", scope="f1")
    assert one["followup"]["notes"] == "他周一问的"
    opened = await call(db_session, scope, "followup_list", scope="open", limit=1)
    assert [item["id"] for item in opened["items"]] == ["F001"] and opened["truncated"] is True

    updated = await call(
        db_session,
        scope,
        "followup_update",
        id="F001",
        category="waiting",
        review_on="2026-11-14",
        clear=["notes", "due_on"],
    )
    assert updated["followup"]["category"] == "waiting"
    assert updated["followup"]["notes"] is None and updated["followup"]["due_on"] is None
    assert updated["followup"]["review_on"] == "2026-11-14"

    snoozed = await call(db_session, scope, "followup_snooze", id="F002", until="2026-11-20")
    assert (snoozed["followup"]["status"], snoozed["followup"]["review_on"]) == (
        "snoozed",
        "2026-11-20",
    )
    closed = await call(
        db_session, scope, "followup_close", id="F001", outcome="dropped", notes="对方说不用了"
    )
    assert closed["followup"]["status"] == "dropped"
    assert closed["followup"]["notes"] == "对方说不用了"
    everything = await call(db_session, scope, "followup_list", scope="all")
    assert [(item["id"], item["status"]) for item in everything["items"]] == [
        ("F002", "snoozed"),
        ("F001", "dropped"),
    ]


async def test_dates_follow_the_owners_timezone(db_session: AsyncSession) -> None:
    bot, _, scope = await assistant_turn(db_session)
    row = await assistant_settings.get(db_session, bot.id)
    row.timezone = "America/New_York"
    await db_session.flush()
    added = await call(db_session, scope, "followup_add", category="remind", title="交报告")
    assert added["today"] == "2026-11-12"
    # 纽约的今天是 12 日：13 日是明天，可以推过去。
    out = await call(db_session, scope, "followup_snooze", id="F001", until="2026-11-13")
    assert out["followup"]["status"] == "snoozed"
    # 12 日还没到回看日。
    assert (await call(db_session, scope, "followup_list"))["items"] == []
    row.timezone = "Asia/Shanghai"
    await db_session.flush()
    # 上海已经是 13 日：它到期了，重新出现；再推到 13 日就不是「之后」了。
    listed = await call(db_session, scope, "followup_list")
    assert [(item["id"], item["status"]) for item in listed["items"]] == [("F001", "open")]
    refused = await call(db_session, scope, "followup_snooze", id="F001", until="2026-11-13")
    assert refused["error"] == "snooze_not_future" and refused["today"] == "2026-11-13"


async def test_errors_are_codes_without_content(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, scope = await assistant_turn(db_session)
    assert await call(db_session, scope, "notes_read") == {"error": "invalid_tool_or_arguments"}
    for name, arguments in (
        ("followup_add", {"category": "todo", "title": "x"}),
        ("followup_add", {"category": "remind", "title": "x", "due_on": "2026-02-31"}),
        ("followup_add", {"category": "remind", "title": "x", "owner": "someone"}),
        ("followup_add", {"category": "remind", "title": "长" * (followups.MAX_TITLE + 1)}),
        ("followup_update", {"id": "F001", "clear": ["title"]}),
        ("followup_snooze", {"id": "one", "until": "2026-11-20"}),
        ("followup_list", {"limit": 0}),
    ):
        out = await call(db_session, scope, name, **arguments)
        assert out["error"] == "invalid_tool_or_arguments", (name, arguments)
        # 只有字段名和规则，不回显取值。
        assert out["invalid"] and "someone" not in str(out) and "长长" not in str(out)
    missing = await call(db_session, scope, "followup_update", id="F099", title="改")
    assert missing["error"] == "followup_not_found" and "hint" in missing
    await call(db_session, scope, "followup_add", category="remind", title="一件事")
    assert (await call(db_session, scope, "followup_update", id="F001"))["error"] == (
        "nothing_to_update"
    )
    assert (await call(db_session, scope, "followup_list", scope="soon"))["error"] == (
        "invalid_scope"
    )
    await call(db_session, scope, "followup_close", id="F001")
    assert (await call(db_session, scope, "followup_update", id="F001", title="改"))["error"] == (
        "followup_closed"
    )
    monkeypatch.setattr(followups, "MAX_OPEN", 1)
    await call(db_session, scope, "followup_add", category="remind", title="第二件")
    full = await call(db_session, scope, "followup_add", category="remind", title="第三件")
    assert (full["error"], full["limit"]) == ("too_many_followups", 1)
```

`tests/unit/test_assistant_followup_tools_loaded.py`：

```python
"""待跟进工具在 knowledge 组里，MCP 路由一导入就注册好。"""

from __future__ import annotations

import subprocess
import sys

from coreman.core.assistants import followup_tools, tools

NAMES = ["followup_add", "followup_close", "followup_list", "followup_snooze", "followup_update"]


def _names(group: str) -> set[str]:
    return {str(item["name"]) for item in tools.definitions([], groups={group})}


def test_tools_are_registered_in_the_knowledge_group() -> None:
    assert sorted(followup_tools.OWN) == NAMES
    assert set(NAMES) <= _names("knowledge")
    assert not set(NAMES) & _names("jobs")


def test_definitions_describe_ids_dates_and_scopes() -> None:
    by_name = {str(item["name"]): item for item in followup_tools.definitions()}
    add = by_name["followup_add"]["inputSchema"]
    assert add["required"] == ["category", "title"]
    assert add["properties"]["category"]["enum"] == ["decide", "promised", "waiting", "remind"]
    assert set(add["properties"]) == {
        "category",
        "title",
        "notes",
        "review_on",
        "due_on",
        "origin_message_id",
    }
    snooze = by_name["followup_snooze"]["inputSchema"]
    assert snooze["properties"]["until"]["description"] == "YYYY-MM-DD"
    listing = by_name["followup_list"]
    assert "F012" in listing["inputSchema"]["properties"]["scope"]["description"]
    for word in ("decide", "promised", "waiting", "remind"):
        assert word in by_name["followup_add"]["description"]


def test_the_mcp_router_loads_the_followup_tools() -> None:
    # 另起一个解释器：本进程里这个测试文件已经导入过 followup_tools，证明不了路由会导入它。
    code = (
        "import coreman.api.routers.feishu_personal\n"
        "from coreman.core.assistants import tools\n"
        "names = [d['name'] for d in tools.definitions([], groups={'knowledge'})]\n"
        "print(sorted(n for n in names if n.startswith('followup_')))\n"
    )
    done = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
    assert done.stdout.strip() == str(NAMES)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_followup_tools.py tests/unit/test_assistant_followup_tools_loaded.py -q`
Expected: 两个文件都在收集阶段报错 `ImportError: cannot import name 'followup_tools' from 'coreman.core.assistants'`

- [ ] **Step 3: 实现**

`coreman/core/assistants/followup_tools.py`：

```python
"""待跟进工具：助理在个人工具 MCP 上读写自己的待跟进清单。

注册在助理工具注册表的 `knowledge` 组：不依赖飞书授权，子任务也能用。日期一律是所有者时区的
日期；每个结果都带 `today`，助理据此算「明天」「下周一」。
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, tools
from coreman.core.assistants import settings as assistant_settings
from coreman.core.crypto import Cipher
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.toolbase import (
    Arguments,
    Day,
    Identifier,
    input_schema,
    valid_days,
)
from coreman.core.timeutils import utcnow

Category = Literal["decide", "promised", "waiting", "remind"]
Label = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[Ff]?\d{1,9}$"),
    Field(description="Follow-up id such as F012"),
]
Title = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=followups.MAX_TITLE),
    Field(description="One sentence that makes sense on its own"),
]
Notes = Annotated[str, StringConstraints(max_length=followups.MAX_NOTES)]


class Add(Arguments):
    category: Category
    title: Title
    notes: Notes = ""
    review_on: Day | None = None
    due_on: Day | None = None
    origin_message_id: Identifier | None = Field(
        default=None, description="The Feishu message that caused it, if any"
    )

    _days = valid_days("review_on", "due_on")


class Update(Arguments):
    id: Label
    category: Category | None = None
    title: Title | None = None
    notes: Notes | None = None
    review_on: Day | None = None
    due_on: Day | None = None
    clear: list[Literal["notes", "review_on", "due_on"]] = Field(
        default_factory=list, max_length=3, description="Fields to empty"
    )

    _days = valid_days("review_on", "due_on")


class Snooze(Arguments):
    id: Label
    until: Day

    _days = valid_days("until")


class Close(Arguments):
    id: Label
    outcome: Literal["done", "dropped"] = "done"
    notes: Notes | None = Field(default=None, description="Replaces the notes: how it ended")


class Listing(Arguments):
    scope: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] = Field(
        default="today",
        description=(
            "today (due or up for review today) | open (everything not closed) | all |"
            " decide | promised | waiting | remind, or one id such as F012 for its full notes"
        ),
    )
    limit: int = Field(default=50, ge=1, le=200)


OWN: dict[str, tuple[type[Arguments], str]] = {
    "followup_add": (
        Add,
        "Record something the owner must not lose track of across days. category: decide"
        " (waiting for the owner's decision), promised (the owner promised someone), waiting"
        " (waiting on someone else), remind (remind the owner on a date). Dates are the"
        " owner's local dates. Returns its id (F012).",
    ),
    "followup_update": (
        Update,
        "Change an open follow-up: category, title, notes, dates. Only the given fields"
        " change; list a field in `clear` to empty it.",
    ),
    "followup_snooze": (
        Snooze,
        "Hide an open follow-up until a later local date; it comes back on that day.",
    ),
    "followup_close": (
        Close,
        "Close a follow-up: done, or dropped when it is no longer needed.",
    ),
    "followup_list": (
        Listing,
        "List the owner's follow-ups by scope. Items carry the title and whether notes"
        " exist; pass one id as scope to read its notes.",
    ),
}

HINTS = {
    "followup_not_found": "没有这个编号；用 followup_list 看现有的。",
    "followup_closed": "这条已经关闭，不能再改；需要的话新记一条。",
    "snooze_not_future": "until 必须是 today 之后的日期。",
    "too_many_followups": "未结的条目已到上限，先关闭办完的。",
    "invalid_scope": "scope 只能是 today、open、all、四个分类名之一，或一个编号。",
    "nothing_to_update": "没有给出要改的项。",
}


def definitions() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": text, "inputSchema": input_schema(args)}
        for name, (args, text) in OWN.items()
    ]


def _problems(exc: ValidationError) -> list[str]:
    # 只有字段名和规则说明：pydantic 的这两项不回显取值。
    return [
        ".".join(str(part) for part in error["loc"]) + ": " + error["msg"]
        for error in exc.errors(include_url=False, include_input=False)[:5]
    ]


def _day(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


async def _list(
    session: AsyncSession, bot_id: uuid.UUID, args: Listing, today: date
) -> dict[str, Any]:
    if args.scope in followups.SCOPES:
        # 多取一条，只为知道后面还有没有。
        rows = await followups.list_(
            session, bot_id, args.scope, today=today, limit=args.limit + 1
        )
        return {
            "items": [followups.view(row, notes=False) for row in rows[: args.limit]],
            "truncated": len(rows) > args.limit,
        }
    try:
        seq = followups.parse_label(args.scope)
    except followups.FollowupError:
        raise followups.FollowupError("invalid_scope") from None
    return {"followup": followups.view(await followups.get(session, bot_id, seq))}


async def _run(
    session: AsyncSession, bot_id: uuid.UUID, name: str, args: Any, today: date
) -> dict[str, Any]:
    if name == "followup_list":
        return await _list(session, bot_id, args, today)
    if name == "followup_add":
        row = await followups.add(
            session,
            bot_id,
            category=args.category,
            title=args.title,
            notes=args.notes or None,
            review_on=_day(args.review_on),
            due_on=_day(args.due_on),
            origin_message_id=args.origin_message_id,
        )
        return {"followup": followups.view(row)}
    seq = followups.parse_label(args.id)
    if name == "followup_snooze":
        row = await followups.snooze(
            session, bot_id, seq, date.fromisoformat(args.until), today=today
        )
    elif name == "followup_close":
        row = await followups.close(
            session, bot_id, seq, dropped=args.outcome == "dropped", notes=args.notes
        )
    else:
        changes: dict[str, Any] = {}
        for field in ("category", "title", "notes"):
            if getattr(args, field) is not None:
                changes[field] = getattr(args, field)
        for field in ("review_on", "due_on"):
            if getattr(args, field) is not None:
                changes[field] = date.fromisoformat(getattr(args, field))
        for field in args.clear:
            changes[field] = None
        if not changes:
            raise followups.FollowupError("nothing_to_update")
        row = await followups.update(session, bot_id, seq, **changes)
    return {"followup": followups.view(row)}


async def dispatch(
    session: AsyncSession, cipher: Cipher, scope: Scope, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """待跟进工具的入口。`cipher` 不用：内容由会话按所有者的数据密钥加解密。"""
    found = OWN.get(name)
    if found is None:
        return {"error": "invalid_tool_or_arguments"}
    try:
        args: Any = found[0].model_validate(arguments)
    except ValidationError as exc:
        return {"error": "invalid_tool_or_arguments", "invalid": _problems(exc)}
    bot_id = scope.bot.id
    today = assistant_settings.local_today(await assistant_settings.get(session, bot_id), utcnow())
    try:
        out = await _run(session, bot_id, name, args, today)
    except followups.FollowupError as exc:
        out = {"error": exc.code}
        if exc.code in HINTS:
            out["hint"] = HINTS[exc.code]
        if exc.code == "too_many_followups":
            out["limit"] = followups.MAX_OPEN
    return {**out, "today": today.isoformat()}


tools.register("knowledge", definitions(), dispatch)
```

`coreman/api/routers/feishu_personal.py`：在导入区（`from coreman.core.assistants...` 那几行旁边）加：

```python
# 导入即注册：待跟进工具进助理工具注册表的 knowledge 组。
from coreman.core.assistants import followup_tools as _followup_tools  # noqa: F401
```

（2A 若在别处集中导入各组的工具模块，把这一行放到那里，并相应调整 `test_the_mcp_router_loads_the_followup_tools` 里导入的模块名；该用例要证明的是「服务一启动工具就在」。）

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_followup_tools.py tests/unit/test_assistant_followup_tools_loaded.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: 全部 PASS（台账不变：`followup_tools.py` 不直接读写内容表）

Run: `uv run pytest tests/api/test_feishu_personal.py -q`
Expected: 全部 PASS（MCP 路由的现有行为不变）

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 无报错

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants/followup_tools.py coreman/api/routers/feishu_personal.py \
  tests/integration/test_assistant_followup_tools.py \
  tests/unit/test_assistant_followup_tools_loaded.py
git commit -m "个人助理待跟进：followup_* 五个工具，注册进 knowledge 组，日期按所有者时区"
```

---

### Task 5: 提示词规则

**Files:**
- Create: `coreman/core/assistants/followup_prompt.py`
- Modify: `coreman/runtime/worker/chat/personal.py:15-17`（导入）与 `:236-238`（`configure` 里助理的那一段）
- Modify: `tests/api/test_assistant_workspace_prompt.py`（原断言用的是 `endswith`，追加规则后要放宽）
- Test: `tests/unit/test_assistant_followup_prompt.py`

**Interfaces:**
- Consumes: `followups.MAX_OPEN`；`coreman.runtime.worker.chat.personal.configure(session, ctx, intake, base_session_id, system_prompt, env) -> tuple[str, dict[str, str]]` 与 `feishu_guidance(row, base_url, *, scheduled) -> tuple[str, str]`；现有常量 `approval_prompt.RULES`、`workspace_prompt.WORKSPACE_RULES`。
- Produces: `coreman.core.assistants.followup_prompt.FOLLOWUP_RULES: str`（以 `"\n\n## 待跟进\n"` 开头的固定文字，不含日期等每轮会变的内容）。助理的提示词在审批规则与工作目录规则之后带上它；普通员工的提示词逐字不变。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_assistant_followup_prompt.py`：

```python
"""待跟进规则：点到五个工具、四个分类与平台执行的限制；只进个人助理的提示词。"""

from __future__ import annotations

from coreman.core.assistants import followup_prompt, followup_tools, followups
from coreman.runtime.worker.chat import personal


def test_rules_name_the_tools_categories_and_limits() -> None:
    text = followup_prompt.FOLLOWUP_RULES
    assert text.startswith("\n\n## 待跟进\n")
    for name in followup_tools.OWN:
        assert name in text
    for word in ("decide", "promised", "waiting", "remind", "F012", "origin_message_id"):
        assert word in text
    for word in ("review_on", "due_on", "today", "outcome=dropped"):
        assert word in text
    assert f"最多 {followups.MAX_OPEN} 条" in text


def test_rules_are_stable_text() -> None:
    # 进 system prompt 的稳定段：不能带日期这类每轮会变的东西。
    assert "2026" not in followup_prompt.FOLLOWUP_RULES


def test_standard_bot_guidance_does_not_mention_followups() -> None:
    rules, _ = personal.feishu_guidance(None, "https://coreman.example.com", scheduled=False)
    assert "待跟进" not in rules and "followup_" not in rules
```

`tests/api/test_assistant_workspace_prompt.py`：导入区加 `from coreman.core.assistants import followup_prompt`（与现有的 `from coreman.core.assistants import workspace_prompt` 合并成 `from coreman.core.assistants import followup_prompt, workspace_prompt`）。`test_the_assistant_prompt_gets_the_workspace_rules` 的最后一行

```python
    assert prompt.endswith(approval_prompt.RULES + workspace_prompt.WORKSPACE_RULES)
```

换成

```python
    assert approval_prompt.RULES + workspace_prompt.WORKSPACE_RULES in prompt
    assert prompt.endswith(followup_prompt.FOLLOWUP_RULES)
```

`test_the_standard_employee_prompt_is_unchanged` 的末尾加一行：

```python
    assert followup_prompt.FOLLOWUP_RULES not in prompt
```

（2B 若已经在同一处追加了知识库规则并改过这条断言，`endswith` 那一行按届时实际的拼接顺序写：待跟进规则接在 2B 的规则之后。）

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_followup_prompt.py tests/api/test_assistant_workspace_prompt.py -q`
Expected: 收集阶段报错 `ImportError: cannot import name 'followup_prompt' from 'coreman.core.assistants'`

- [ ] **Step 3: 实现**

`coreman/core/assistants/followup_prompt.py`：

```python
"""个人助理的待跟进规则：进 system prompt 的稳定段，每轮逐字不变。"""

from __future__ import annotations

FOLLOWUP_RULES = (
    "\n\n## 待跟进\n"
    "跨天的事不靠会话记忆（会话每天换新），靠待跟进清单。每条有一个编号（F012 这样）："
    "对所有者提到它、调用工具时都用这个编号。\n"
    "- 什么时候记：所有者要拍板而当下没定；所有者答应了别人一件事；所有者在等别人的回复或"
    "交付；所有者让你到某天提醒他。当场就能办完的事不记。记之前先用 followup_list 看有没有"
    "同一件事，有就用 followup_update 补充，不要重复记。\n"
    "- 四类怎么选（followup_add 的 category）：decide＝等所有者决定；promised＝所有者答应了"
    "别人；waiting＝在等别人；remind＝到日子提醒所有者。一件事只归一类，情况变了用 "
    "followup_update 改类。\n"
    "- 日期都是所有者当地的日期（YYYY-MM-DD），工具结果里的 today 是今天。due_on 是这件事的"
    "期限，review_on 是下次该回头看的日子；到了日子它会出现在 followup_list 的 today 里。\n"
    "- 今天不用管的事用 followup_snooze 推到之后的某一天，到那天它会重新出现。办完了用 "
    "followup_close；不再需要的用 outcome=dropped。关闭后不能再改。\n"
    "- 由某条消息引起的事，记的时候带上 origin_message_id；那条消息被撤回时平台会自动关掉它。\n"
    "- 标题写成一句能独立看懂的话，来龙去脉写进 notes。清单里不带 notes，要看时把编号作为 "
    "followup_list 的 scope。同时未结的最多 500 条。"
)
```

`coreman/runtime/worker/chat/personal.py`：导入区把

```python
from coreman.core.assistants import workspace_prompt
```

改成

```python
from coreman.core.assistants import followup_prompt, workspace_prompt
```

`configure` 里

```python
    if is_assistant(scope.bot):
        # 个人助理：写类工具改为提交审批，工作目录不放个人信息，规则写进稳定段。
        rules += approval_prompt.RULES + workspace_prompt.WORKSPACE_RULES
```

改成

```python
    if is_assistant(scope.bot):
        # 个人助理：写类工具改为提交审批，工作目录不放个人信息，跨天的事记待跟进；规则写进稳定段。
        rules += approval_prompt.RULES + workspace_prompt.WORKSPACE_RULES
        rules += followup_prompt.FOLLOWUP_RULES
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_followup_prompt.py tests/api/test_assistant_workspace_prompt.py tests/unit/test_assistant_approval_prompt.py tests/unit/test_assistant_workspace_prompt.py -q`
Expected: 全部 PASS

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Expected: 无报错

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants/followup_prompt.py coreman/runtime/worker/chat/personal.py \
  tests/unit/test_assistant_followup_prompt.py tests/api/test_assistant_workspace_prompt.py
git commit -m "个人助理待跟进：提示词规则（何时记、四类怎么选、编号怎么引用）"
```

---

### Task 6: 文档、CHANGELOG 与整体验证

**Files:**
- Modify: `docs/feishu.md`（「个人助理应用」一节，「写操作要审批」那一条之后）
- Modify: `CHANGELOG.md`（`## [Unreleased]` → `### Added` 的第一条之前）

**Interfaces:**
- Consumes: Task 1–5 的全部产出。
- Produces: 无代码接口。

- [ ] **Step 1: 更新 `docs/feishu.md`**

在「个人助理应用」一节里，以 `- **写操作要审批。**` 开头的那一条之后插入一条：

```markdown
- **待跟进。** 助理替所有者记着跨天的事：等他决定的、他答应了别人的、在等别人的、到日子要提醒他的，共四类。每条在这位助理内有一个递增的编号（显示为 `F012`），带一个可选的期限和一个可选的回看日期；日期按所有者的时区（助理设置里的 `timezone`）算。助理用 `followup_add`、`followup_update`、`followup_snooze`（推到之后的某一天，到那天重新出现）、`followup_close`、`followup_list` 读写，这几个工具不调飞书、不过审批、不依赖飞书授权。标题与备注按所有者的数据密钥加密存放（表 `assistant_followups` 的 `title_enc`、`notes_enc`）；分类、状态、日期、编号是明文，运维只看得到这些。同一位助理未结的条目最多 500 条。条目记了起因消息的，那条消息被撤回时自动关闭（状态 `dropped`、`close_code` 为 `origin_recalled`）。
```

- [ ] **Step 2: 更新 `CHANGELOG.md`**

在 `## [Unreleased]` 下 `### Added` 的第一条之前插入：

```markdown
- Personal assistants keep a follow-up list. An assistant records things its owner must not lose track of across days in four categories (waiting for the owner's decision, promised by the owner, waiting on someone else, remind on a date), each numbered per assistant and shown as `F012`, with an optional due date and review date in the owner's time zone. New assistant tools `followup_add`, `followup_update`, `followup_snooze`, `followup_close` and `followup_list` read and write the list without calling Feishu and without approval; a snoozed item comes back on its day, and an item recorded with the message that caused it is closed automatically when that message is recalled. Titles and notes are stored encrypted with the owner's data key; category, status, dates and numbers stay in plaintext. At most 500 open items per assistant. Migration `0096` adds `assistant_followups`; runtime nodes need no upgrade. See `docs/feishu.md`.
```

- [ ] **Step 3: 整体验证**

Run: `uv run pytest tests/integration/test_assistant_followups_schema.py tests/integration/test_assistant_followups.py tests/integration/test_assistant_followups_queries.py tests/integration/test_assistant_followup_tools.py tests/unit/test_assistant_followup_tools_loaded.py tests/unit/test_assistant_followup_prompt.py tests/unit/test_assistant_sealed_ledger.py tests/unit/test_assistant_sealed_codec.py tests/api/test_assistant_workspace_prompt.py tests/integration/test_migrations.py -q`
Expected: 全部 PASS

Run: `uv run pytest tests -q -k assistant`
Expected: 全部 PASS（其它助理用例不受影响）

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman && uv run alembic check`
Expected: 无报错；`alembic check` 输出 `No new upgrade operations detected.`

按仓库约定对暂存区跑一遍敏感词自查，Expected: 无输出。

- [ ] **Step 4: 提交**

```bash
git add docs/feishu.md CHANGELOG.md
git commit -m "个人助理待跟进：文档与 CHANGELOG"
```

---

## 交给后续层的事

- **2D / 2E**：发现起因消息被撤回时调 `followups.close_for_message(session, bot_id, message_id)`，把返回的行（`followups.view(row)`）写进交回内容，让助理知道哪几条被关了。
- **2E**：每轮用 `followups.due(session, bot_id, settings.local_today(...))` 取当天到期的条目。它是幂等的读，同一条在被处理之前每次都会返回；「一天只交回一次」由 2E 的收件箱去重（建议去重键 `followup:<id>:<日期>`）。
- **3A**：`assistant_followups.job_id` 现在是可空的 `uuid` 普通列。`assistant_jobs.id` 若不是 `uuid`，在 3A 的迁移里改这一列的类型；`followups.add` 已接受 `job_id`。
- **3C**：页面的「待跟进」分区用 `followups.list_(…, "decide" | "promised" | "waiting" | "remind", …)`，「等你出手」里今天到期的用 `followups.due`；输出用 `followups.view`。接口输出前按 `viewer` 再核一次所有者（`sealed.open_rows(session, rows, viewer=<登录用户>)`）。

## Self-Review

- **Spec 覆盖**：表与全部列（Task 1）；`seq` 每个助理内递增、显示为 F012（Task 2 `label`、并发用例）；四类与状态的明文枚举（Task 1 约束）；五个工具（Task 4）；「分类、状态、日期是明文，按日期查询不需要解密」（Task 1 的明文断言、Task 3 的查询只用明文列）；「起因消息被撤回时自动关闭」（Task 3 `close_for_message`）；标 `_enc` 的列加密（Task 1、2 的密文断言）；第九节页面要的「四类清单」「今天到期的待跟进」（Task 3 的 scope 与 `due`，接口留给 3C）。总览要求的上限 500、`knowledge` 组、按所有者时区（Task 2、4）。
- **任务要求的证明**：库里是密文（Task 1、2、4 各一处）；别的助理看不到也改不了（Task 2 `test_another_assistant_cannot_see_or_change_it`、Task 3 `test_other_assistants_are_not_listed`）；并发不重号（Task 2）；推后到期重新出现（Task 3、Task 4 的时区用例）；各 scope（Task 3 `test_scopes`）；撤回自动关闭（Task 3）；上限（Task 2、4）。
- **类型一致**：`followups.update` 的 `notes` / `review_on` / `due_on` 用 `KEEP` 哨兵，工具层只在有值或 `clear` 时传入；`list_` 的 `limit` 在工具层传 `limit + 1`；`close` 的 `dropped: bool` 对应工具的 `outcome`；台账条目在 Task 2 是 1 / 1，Task 3 改为 2 / 2。
- **没有占位**：每个代码步骤都给了完整代码；唯一依赖外部未落地代码的地方集中写在「对 2A 的假定」，并在 Task 4、5 标明了届时如何对照调整。
