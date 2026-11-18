# 个人助理事项总表（第一期）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让所有者在飞书（「看板」指令、摘要）和「我的助理」页上，一眼看清每件待跟进的事由谁在办、卡在哪、多久没动了。

**Architecture:** 待跟进加三个字段（`handler`、`handler_name_enc`、`progress_at`），再加一个记进展的工具 `followup_progress`。新模块 `coreman/core/assistants/board.py` 在读取时推导每条的阶段与停滞，不落库。三处展示都调用同一个 `board.build`：页面接口 `/me/assistant/board`（替换 `/attention`）、私聊「看板」指令（平台直接回卡片，不开轮次）、摘要卡片（前两节改成「等你」与「停滞」）。

**Tech Stack:** Python 3.12、SQLAlchemy 2（async）、Alembic、FastAPI、pytest；前端 Vue 3 + Element Plus + vitest。

**Spec:** `docs/superpowers/specs/2026-11-18-personal-assistant-board-and-gaps.md`（本计划只做规格「分期」里的第 1 期；第 2 期能力缺口、第 3 期申请代办另写计划）

## Global Constraints

- 公开仓：代码、注释、测试、提交信息里不写公司名、内部域名、内部系统名；示例用 `example.com`、李四、王五这类通用人名。
- 阶段只在读取时计算，不落库。
- 停滞阈值（D4）：「在推进」2 个工作日没有进展；「等别人」过了 `due_on`，或 5 个工作日没有进展。工作日按所有者设置的 `workdays`（1=周一 … 7=周日）计算。
- 纯决策不进审批系统（D1 已定）：「等你」一组里写操作的审批单带审批链接，纯决策的待跟进带「现在处理」按钮，两者计数分开。
- 起草范围（D3）：不放开新类别，只在提示词里写明「写操作可以起草成待批单交给所有者批，不要只提醒」。
- 总表两边都做（D2），飞书是主入口。
- 封存：新增的 `_enc` 列登记进 `coreman/core/assistants/sealed.py` 的 `COLUMNS`；日志只记编号与计数，不记内容。
- 待跟进规则段 `FOLLOWUP_RULES` 进 system prompt 稳定段，不能带每轮会变的东西。
- 迁移编号接在 `0113` 之后：`0114`。合并前若 main 已有 0114，顺延并改 `down_revision`。
- 跑全量测试用专属库名（`coreman_test_board`，配 `-n auto`），不要与别的工作树共用 `coreman_test`，否则会互相删库。
- 写用到真实时钟的助理用例要注意默认免打扰（22:00–08:00），涉及摘要的用例沿用现有测试里的冻结时刻。

## File Structure

| 文件 | 职责 |
|---|---|
| `migrations/versions/0114_assistant_followup_handler.py`（新） | 三列与约束 |
| `coreman/core/db/models/assistants.py` | 模型字段、`FOLLOWUP_HANDLERS` |
| `coreman/core/db/models/__init__.py` | 导出 `FOLLOWUP_HANDLERS` |
| `coreman/core/assistants/sealed.py` | 登记 `handler_name_enc` |
| `coreman/core/assistants/followups.py` | 谁在办的校验与写入、`progress`、`view` 加字段 |
| `coreman/core/assistants/followup_tools.py` | 工具参数加 `handler` / `handler_name` / `job`，新工具 `followup_progress` |
| `coreman/core/assistants/followup_prompt.py` | 规则补充 |
| `coreman/core/assistants/board.py`（新） | 阶段与停滞推导、`build`、`pending_approvals`（从 digest 挪来） |
| `coreman/core/assistants/digest.py` | 摘要卡片改版、`board_card`、`send_board` |
| `coreman/runtime/worker/assistant_board.py`（新） | 私聊「看板」指令 |
| `coreman/runtime/worker/chat/intake.py`、`chat/queued_batch.py` | 接入指令 |
| `coreman/api/routers/me_assistant.py` | `/board` 替换 `/attention` |
| `web/src/api/myAssistant.ts`、`web/src/i18n/assistant.ts` | 类型、接口、文案 |
| `web/src/components/assistant/AssistantBoard.vue`（新）、删 `AssistantAttention.vue` | 总表视图 |
| `web/src/views/MyAssistantView.vue` | 默认分区改为总表 |
| `docs/superpowers/specs/2026-11-11-personal-assistant.md`、`CHANGELOG.md` | 文档 |

---

### Task 1: 迁移与模型字段

**Files:**
- Create: `migrations/versions/0114_assistant_followup_handler.py`
- Modify: `coreman/core/db/models/assistants.py`（`FOLLOWUP_*` 常量与 `AssistantFollowup`）
- Modify: `coreman/core/db/models/__init__.py`（导出）
- Modify: `coreman/core/assistants/sealed.py:207`
- Test: `tests/integration/test_assistant_followups_schema.py`

**Interfaces:**
- Produces: `FOLLOWUP_HANDLERS = ("assistant", "job", "person", "owner")`；`AssistantFollowup.handler: str`（默认 `'assistant'`）、`handler_name_enc: str | None`（封存）、`progress_at: datetime`（默认 `now()`）。

- [ ] **Step 1: 写失败的测试**

在 `tests/integration/test_assistant_followups_schema.py` 末尾追加：

```python
async def test_handler_defaults_and_name_is_sealed(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    row = _row(bot.id, handler="person", handler_name_enc=f"李四 {SECRET}")
    plain = _row(bot.id, seq=2)
    db_session.add_all([row, plain])
    await db_session.commit()
    assert plain.handler == "assistant" and plain.handler_name_enc is None
    assert plain.progress_at is not None
    stored = str(
        await raw(db_session, "SELECT handler_name_enc FROM assistant_followups WHERE id = :id", id=row.id)
    )
    assert SECRET not in stored and "李四" not in stored and stored.startswith("enc:v2:d1:")


async def test_handler_must_be_known(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    db_session.add(_row(bot.id, handler="robot"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/integration/test_assistant_followups_schema.py -k handler -v`
Expected: FAIL（`handler` 不是 `AssistantFollowup` 的有效参数）

- [ ] **Step 3: 写迁移**

`migrations/versions/0114_assistant_followup_handler.py`：

```python
"""Follow-ups: who is handling it, and when it last made progress."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None

HANDLERS = ("assistant", "job", "person", "owner")


def upgrade() -> None:
    # 改的是在用的表：5 秒拿不到锁就报错回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "assistant_followups",
        sa.Column("handler", sa.Text(), nullable=False, server_default=sa.text("'assistant'")),
    )
    op.add_column("assistant_followups", sa.Column("handler_name_enc", sa.Text(), nullable=True))
    op.add_column(
        "assistant_followups",
        sa.Column(
            "progress_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # 老条目的进展时间取它最后一次被改的时刻，免得上线当天全都算「刚有进展」。
    op.execute("UPDATE assistant_followups SET progress_at = updated_at")
    # 挂着子任务的老条目就是子任务在办。
    op.execute("UPDATE assistant_followups SET handler = 'job' WHERE job_id IS NOT NULL")
    values = ", ".join(f"'{v}'" for v in HANDLERS)
    op.create_check_constraint(
        "ck_assistant_followups_handler", "assistant_followups", f"handler IN ({values})"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_assistant_followups_handler", "assistant_followups", type_="check")
    op.drop_column("assistant_followups", "progress_at")
    op.drop_column("assistant_followups", "handler_name_enc")
    op.drop_column("assistant_followups", "handler")
```

先核对约束命名：`grep -n "ck_assistant_followups" migrations/versions/*.py`，`enum_check` 生成的名字若是 `ck_assistant_followups_<name>` 形式就与上面一致；不一致时照现有迁移的写法改。

- [ ] **Step 4: 改模型**

`coreman/core/db/models/assistants.py`，在 `FOLLOWUP_CLOSE_CODES` 后加：

```python
# 谁在办：助理自己、子任务（含调度公共员工，指向 job_id）、某位同事、所有者自己。
FOLLOWUP_HANDLERS = ("assistant", "job", "person", "owner")
```

`AssistantFollowup.__table_args__` 加 `CheckConstraint(enum_check("handler", FOLLOWUP_HANDLERS), name="handler"),`；字段加在 `needs_owner` 之后：

```python
    handler: Mapped[str] = mapped_column(Text, server_default=text("'assistant'"))
    # handler = person 时的同事称呼；封存方式同标题。
    handler_name_enc: Mapped[str | None] = mapped_column(Text)
    # 最近一次进展：新建、改动、followup_progress 时刷新；总表据此判断停滞。
    progress_at: Mapped[datetime] = _ts(server_default=text("now()"))
```

docstring 补一句：`handler_name_enc` 登记在 `sealed.COLUMNS`。`coreman/core/db/models/__init__.py` 里在 `FOLLOWUP_CATEGORIES` 旁导出 `FOLLOWUP_HANDLERS`。

`coreman/core/assistants/sealed.py:207` 改为：

```python
    AssistantFollowup: {"title_enc": Text(""), "notes_enc": Text(), "handler_name_enc": Text()},
```

- [ ] **Step 5: 迁移上下行与测试**

Run: `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`（对本机测试库；用 `.env.test` 或 `DATABASE_URL` 指向 `coreman_test_board`）
Run: `uv run pytest tests/integration/test_assistant_followups_schema.py tests/unit/test_assistant_sealed_codec.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/0114_assistant_followup_handler.py coreman/core/db/models coreman/core/assistants/sealed.py tests/integration/test_assistant_followups_schema.py
git commit -m "待跟进：记下谁在办与最近进展时间（迁移 0114）"
```

---

### Task 2: 核心逻辑——谁在办与进展

**Files:**
- Modify: `coreman/core/assistants/followups.py`
- Test: `tests/integration/test_assistant_followups.py`

**Interfaces:**
- Consumes: Task 1 的字段。
- Produces:
  - `followups.HANDLERS`、`MAX_HANDLER_NAME = 50`、`MAX_PROGRESS = 300`
  - `add(..., handler: str = "assistant", handler_name: str | None = None, job_id: uuid.UUID | None = None)`（`job_id` 参数已存在，现在要求它属于这位助理）
  - `update(..., handler: str | None = None, handler_name: str | None = None, job_id: uuid.UUID | None = None)`：给了 `handler` 才改谁在办
  - `async def progress(session, bot_id, seq, note: str, *, today: date) -> AssistantFollowup`
  - `view()` 多四个键：`handler`、`handler_name`、`job_id`（str 或 None）、`progress_at`（ISO）
  - 新错误码：`invalid_handler`、`invalid_handler_name`、`job_not_found`、`invalid_progress`、`notes_full`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/integration/test_assistant_followups.py`，顶部补 `from tests.integration.job_helpers import owner_with_key, spawned`（`owner_with_key(session)` 返回 `(Bot, User)`，带数据密钥与身份，够跑子任务核心函数；`spawned(session, bot)` 派一个子任务并提交）：

```python
async def test_handler_person_needs_a_name_and_is_sealed(db_session: AsyncSession) -> None:
    bot_id = await _assistant(db_session)
    assert await _code(
        followups.add(db_session, bot_id, category="waiting", title="等报价", handler="person")
    ) == "invalid_handler_name"
    assert await _code(
        followups.add(db_session, bot_id, category="waiting", title="等报价", handler="robot")
    ) == "invalid_handler"
    row = await followups.add(
        db_session, bot_id, category="waiting", title="等报价", handler="person",
        handler_name=f" 李四 {SECRET} ",
    )
    await db_session.commit()
    assert row.handler == "person" and row.handler_name_enc == f"李四 {SECRET}"
    view = followups.view(row)
    assert view["handler"] == "person" and view["handler_name"] == f"李四 {SECRET}"
    assert view["job_id"] is None and view["progress_at"]


async def test_handler_job_must_point_at_own_job(db_session: AsyncSession) -> None:
    bot, _ = await owner_with_key(db_session)
    other, _ = await owner_with_key(db_session, chat_id="oc_other", platform_user_id="other_pid", open_id="ou_other")
    bot_id = bot.id
    mine = await spawned(db_session, bot)
    theirs = await spawned(db_session, other)
    assert await _code(
        followups.add(db_session, bot_id, category="decide", title="查退货", handler="job")
    ) == "job_not_found"
    assert await _code(
        followups.add(
            db_session, bot_id, category="decide", title="查退货", handler="job", job_id=theirs.id
        )
    ) == "job_not_found"
    row = await followups.add(
        db_session, bot_id, category="promised", title="查退货", handler="job", job_id=mine.id
    )
    # 改回助理自己办：子任务关联与同事称呼一起清掉。
    row = await followups.update(db_session, bot_id, row.seq, handler="assistant")
    assert row.handler == "assistant" and row.job_id is None and row.handler_name_enc is None


async def test_progress_appends_dated_lines_and_refreshes_progress_at(
    db_session: AsyncSession,
) -> None:
    bot_id = await _assistant(db_session)
    row = await followups.add(db_session, bot_id, category="promised", title="报价", notes="来龙去脉")
    before = row.progress_at
    row = await followups.progress(db_session, bot_id, row.seq, " 已问过王五 ", today=TODAY)
    assert row.notes_enc == "来龙去脉\n[2026-11-13] 已问过王五"
    assert row.progress_at > before
    assert await _code(followups.progress(db_session, bot_id, row.seq, "  ", today=TODAY)) == "invalid_progress"
    assert await _code(
        followups.progress(db_session, bot_id, row.seq, "长" * (followups.MAX_PROGRESS + 1), today=TODAY)
    ) == "invalid_progress"


async def test_progress_drops_oldest_progress_lines_but_keeps_the_intro(
    db_session: AsyncSession,
) -> None:
    bot_id = await _assistant(db_session)
    intro = "来龙去脉" * 100  # 400 字
    row = await followups.add(db_session, bot_id, category="promised", title="报价", notes=intro)
    for i in range(20):
        row = await followups.progress(db_session, bot_id, row.seq, f"第{i}次" + "进" * 280, today=TODAY)
    assert row.notes_enc is not None and len(row.notes_enc) <= followups.MAX_NOTES
    assert row.notes_enc.startswith(intro)
    assert "第19次" in row.notes_enc and "第0次" not in row.notes_enc


async def test_progress_refuses_when_the_intro_leaves_no_room(db_session: AsyncSession) -> None:
    bot_id = await _assistant(db_session)
    row = await followups.add(
        db_session, bot_id, category="promised", title="报价", notes="长" * followups.MAX_NOTES
    )
    assert await _code(followups.progress(db_session, bot_id, row.seq, "新进展", today=TODAY)) == "notes_full"


async def test_update_refreshes_progress_but_snooze_does_not(db_session: AsyncSession) -> None:
    bot_id = await _assistant(db_session)
    row = await followups.add(db_session, bot_id, category="promised", title="报价")
    first = row.progress_at
    row = await followups.snooze(db_session, bot_id, row.seq, date(2026, 11, 20), today=TODAY)
    assert row.progress_at == first
    row = await followups.update(db_session, bot_id, row.seq, title="报价（改）")
    assert row.progress_at > first
```

另外改 `test_add_numbers_items_and_stores_ciphertext` 里对 `view(first)` 的整体断言：补上 `"handler": "assistant", "handler_name": None, "job_id": None, "progress_at": first.progress_at.isoformat()`。

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/integration/test_assistant_followups.py -v`
Expected: FAIL（`add()` 不认 `handler`；`followups.progress` 不存在）

- [ ] **Step 3: 实现**

`coreman/core/assistants/followups.py`：

常量区（`_LABEL` 之后）：

```python
HANDLERS = FOLLOWUP_HANDLERS
MAX_HANDLER_NAME = 50
MAX_PROGRESS = 300
# 进展行：日期开头。备注超长时从最早的进展行丢起，开头的来龙去脉不动。
_PROGRESS_LINE = re.compile(r"^\[\d{4}-\d{2}-\d{2}\] ")
```

导入改为 `from coreman.core.db.models import FOLLOWUP_CATEGORIES, FOLLOWUP_HANDLERS, AssistantFollowup, AssistantJob`。

`view()` 在 `"needs_owner"` 后加：

```python
        "handler": row.handler,
        "handler_name": row.handler_name_enc,
        "job_id": str(row.job_id) if row.job_id else None,
        "progress_at": row.progress_at.isoformat(),
```

新增校验函数（放在 `_category` 之后）：

```python
async def _handler(
    session: AsyncSession,
    bot_id: uuid.UUID,
    handler: str,
    name: str | None,
    job_id: uuid.UUID | None,
) -> tuple[str, str | None, uuid.UUID | None]:
    """谁在办：返回要写入的 (handler, 同事称呼, 子任务编号)。不相干的两项一律清空。

    Raises:
        FollowupError: invalid_handler / invalid_handler_name / job_not_found
    """
    if handler not in HANDLERS:
        raise FollowupError("invalid_handler")
    if handler == "person":
        clean = (name or "").strip()
        if not clean or len(clean) > MAX_HANDLER_NAME:
            raise FollowupError("invalid_handler_name")
        return handler, clean, None
    if handler == "job":
        found = None
        if job_id is not None:
            found = await session.scalar(
                select(AssistantJob.id).where(AssistantJob.id == job_id, AssistantJob.bot_id == bot_id)
            )
        if found is None:
            raise FollowupError("job_not_found")
        return handler, None, job_id
    return handler, None, None
```

`add()`：签名加 `handler: str = "assistant", handler_name: str | None = None`（`job_id` 已有）。开头校验后（`_writable` 之前）：

```python
    if job_id is not None and handler == "assistant":
        # 旧调用方（换运行时导入）只给 job_id：视为子任务在办。
        handler = "job"
    handler, handler_name, job_id = await _handler(session, bot_id, handler, handler_name, job_id)
```

`handler_name` 有值时也要求密钥可用（`_writable` 本来就会调，顺序不变即可）。构造 `AssistantFollowup` 时加 `handler=handler, handler_name_enc=handler_name, progress_at=utcnow(),`。

先查 `coreman/core/assistants/transfer.py:213` 调 `followups.add` 时是否传 `job_id`：传了且子任务行可能不在这位助理名下（导入）时，会触发 `job_not_found`。如果导入时子任务一并导入且编号保持，没问题；否则在 transfer 里改成不传 `job_id`。在这一步里核对并按实际情况处理，在提交说明里写明。

`update()`：签名加 `handler: str | None = None, handler_name: str | None = None, job_id: uuid.UUID | None = None`。在 `needs_owner` 处理之后：

```python
    if handler is not None:
        if handler == "person":
            await _writable(session, bot_id)
        row.handler, row.handler_name_enc, row.job_id = await _handler(
            session, bot_id, handler, handler_name, job_id
        )
```

函数末尾 `row.updated_at = utcnow()` 改为：

```python
    row.updated_at = row.progress_at = utcnow()
```

新增 `progress()`（放在 `update` 之后）：

```python
async def progress(
    session: AsyncSession, bot_id: uuid.UUID, seq: int, note: str, *, today: date
) -> AssistantFollowup:
    """记一行进展：备注末尾追加「[日期] 内容」，刷新进展时间。

    备注超长时从最早的进展行丢起；开头的来龙去脉（不以日期开头的行）不丢，丢完还放不下就拒绝。

    Raises:
        FollowupError: invalid_progress / notes_full / followup_not_found / followup_closed /
            content_locked
    """
    text = " ".join(note.split())
    if not text or len(text) > MAX_PROGRESS:
        raise FollowupError("invalid_progress")
    row = await _live(session, bot_id, seq)
    await _writable(session, bot_id)
    lines = (row.notes_enc or "").split("\n") if row.notes_enc else []
    lines.append(f"[{today.isoformat()}] {text}")
    while len("\n".join(lines)) > MAX_NOTES:
        oldest = next(
            (i for i, line in enumerate(lines[:-1]) if _PROGRESS_LINE.match(line)), None
        )
        if oldest is None:
            raise FollowupError("notes_full")
        del lines[oldest]
    row.notes_enc = "\n".join(lines)
    row.updated_at = row.progress_at = utcnow()
    await session.flush()
    return row
```

（`snooze` 只改 `updated_at`，不动 `progress_at`；`close` 不用改。）

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest tests/integration/test_assistant_followups.py tests/integration/test_assistant_followups_queries.py tests/api/test_my_assistant_followups.py -v`
Expected: PASS（页面接口的用例若对 `view()` 做整体比较，补上四个新键）

- [ ] **Step 5: Commit**

```bash
git add coreman/core/assistants/followups.py coreman/core/assistants/transfer.py tests/integration/test_assistant_followups.py tests/api/test_my_assistant_followups.py
git commit -m "待跟进：谁在办的校验与写入，followup_progress 记进展"
```

---

### Task 3: 助理工具与规则

**Files:**
- Modify: `coreman/core/assistants/followup_tools.py`
- Modify: `coreman/core/assistants/followup_prompt.py`
- Test: `tests/integration/test_assistant_followup_tools.py`、`tests/unit/test_assistant_followup_prompt.py`、`tests/unit/test_assistant_followup_tools_loaded.py`

**Interfaces:**
- Consumes: Task 2 的 `followups.add/update/progress`。
- Produces: 工具 `followup_progress(id, note)`；`followup_add` / `followup_update` 多三个参数 `handler`、`handler_name`、`job`（子任务编号字符串）。

- [ ] **Step 1: 写失败的测试**

`tests/integration/test_assistant_followup_tools.py` 里照现有用例调用 `followup_tools.dispatch` 的方式（先读文件顶部的 helper，下面用 `call(name, args)` 代指它），追加：

```python
async def test_progress_tool_appends_and_reports_today(db_session: AsyncSession) -> None:
    added = await call(db_session, "followup_add", {"category": "promised", "title": "报价"})
    label = added["followup"]["id"]
    out = await call(db_session, "followup_progress", {"id": label, "note": "已问过王五"})
    assert out["followup"]["notes"].endswith("] 已问过王五") and "today" in out
    bad = await call(db_session, "followup_progress", {"id": label, "note": "长" * 400})
    assert bad["error"] == "invalid_tool_or_arguments"


async def test_handler_arguments_reach_the_core(db_session: AsyncSession) -> None:
    added = await call(
        db_session,
        "followup_add",
        {"category": "waiting", "title": "等报价", "handler": "person", "handler_name": "李四"},
    )
    assert added["followup"]["handler"] == "person"
    missing = await call(
        db_session, "followup_update", {"id": added["followup"]["id"], "handler": "job"}
    )
    assert missing["error"] == "job_not_found" and "hint" in missing
    bad = await call(
        db_session, "followup_update", {"id": added["followup"]["id"], "handler": "job", "job": "x"}
    )
    assert bad["error"] == "job_not_found"
```

`tests/unit/test_assistant_followup_prompt.py` 的 `test_rules_name_the_tools_categories_and_limits` 里 `for word in (...)` 补 `"handler"`、`"先查"`、`"下一步"`、`"建议："`、`"待批单"`。

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/integration/test_assistant_followup_tools.py tests/unit/test_assistant_followup_prompt.py -v`
Expected: FAIL

- [ ] **Step 3: 实现工具**

`followup_tools.py`：

```python
Handler = Literal["assistant", "job", "person", "owner"]
HandlerName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=followups.MAX_HANDLER_NAME),
    Field(description="With handler=person: how the owner calls that colleague"),
]
JobRef = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=64),
    Field(description="With handler=job: the job id from assistant_spawn or assistant_job_list"),
]
HANDLER = (
    "Who is moving it forward now: assistant (you), job (a sub-task or a public employee you"
    " asked; give `job`), person (a colleague; give `handler_name`), owner (the owner himself)"
)
```

`Add` 与 `Update` 各加：

```python
    handler: Handler | None = Field(default=None, description=HANDLER)
    handler_name: HandlerName | None = None
    job: JobRef | None = None
```

新增：

```python
class Progress(Arguments):
    id: Label
    note: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=followups.MAX_PROGRESS),
        Field(description="One or two sentences: what moved forward, found out or changed"),
    ]
```

`OWN` 里加：

```python
    "followup_progress": (
        Progress,
        "Log progress on an open follow-up: appends a dated line to its notes and marks it as"
        " moving. Use it whenever you found something out or pushed it forward; the owner's"
        " board flags items with no progress for days.",
    ),
```

`HINTS` 加：

```python
    "invalid_handler_name": "handler=person 时要给 handler_name（同事的称呼，50 字以内）。",
    "job_not_found": "handler=job 时要给 job：用 assistant_job_list 查到的、你自己派出的子任务编号。",
    "notes_full": "备注里开头的说明已经太长，进展写不进去：先用 followup_update 把 notes 精简。",
    "invalid_progress": "note 写一两句话，不超过 300 字。",
```

`_run` 里：

```python
def _job(value: str | None) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        raise followups.FollowupError("job_not_found") from None
```

`followup_add` 分支给 `followups.add` 补 `handler=args.handler or "assistant", handler_name=args.handler_name, job_id=_job(args.job)`。

`followup_update` 分支：`changes` 收集前加

```python
        if args.handler is not None:
            changes.update(
                handler=args.handler, handler_name=args.handler_name, job_id=_job(args.job)
            )
```

（`handler_name`、`job` 单独给而没给 `handler` 时忽略；`nothing_to_update` 判断不变。）

在 `followup_snooze` 判断之前加：

```python
    if name == "followup_progress":
        row = await followups.progress(session, bot_id, seq, args.note, today=today)
        return {"followup": followups.view(row)}
```

- [ ] **Step 4: 改规则**

`followup_prompt.py` 的 `FOLLOWUP_RULES`，在「标题写成一句……」那条之前插入：

```python
    "- 谁在办：记的时候用 handler 说明——assistant 是你自己在推进，job 是你派的子任务或"
    "请的公共员工（带上 job 编号），person 是某位同事（带上 handler_name），owner 是所有者"
    "自己在办。情况变了用 followup_update 改。\n"
    "- 有进展就用 followup_progress 记一句：查到了什么、推进到哪一步。所有者的总表按进展时间"
    "判断停滞，几天没有进展的事会被标出来。\n"
    "- 先查后提醒：交回的事凡是现有权限能只读查的，先查完、用 followup_progress 记下结论，"
    "再决定要不要找所有者。查不了的，在 notes 里写清楚卡在哪个权限或哪个人上，不要只写"
    "「需要权限」。\n"
    "- 提醒要带下一步：给所有者的每条提醒都说清谁在办、下一步是什么、需要他做什么；没有"
    "下一步的事不发提醒，只记进展。\n"
    "- 等所有者拍板的事（decide），在 notes 里单独写一行以「建议：」开头的建议和理由，"
    "总表和摘要会把这一行显示在标题下面。\n"
    "- 需要写操作才能办完的事，可以直接起草成待批单交给所有者批，不要只提醒他自己去做。\n"
```

- [ ] **Step 5: 运行，确认通过**

Run: `uv run pytest tests/integration/test_assistant_followup_tools.py tests/unit/test_assistant_followup_prompt.py tests/unit/test_assistant_followup_tools_loaded.py tests/api/test_assistant_tool_groups.py -v`
Expected: PASS（工具清单类测试若按名字列举工具，补上 `followup_progress`）

- [ ] **Step 6: Commit**

```bash
git add coreman/core/assistants/followup_tools.py coreman/core/assistants/followup_prompt.py tests
git commit -m "助理工具：followup_progress 与谁在办参数，规则补先查后提醒"
```

---

### Task 4: 总表推导 `board.py`

**Files:**
- Create: `coreman/core/assistants/board.py`
- Modify: `coreman/core/assistants/digest.py`（`_pending_approvals` 挪走，改为调用 `board.pending_approvals`）
- Test: `tests/unit/test_assistant_board_stage.py`（新）、`tests/integration/test_assistant_board.py`（新）

**Interfaces:**
- Consumes: Task 2 的字段；`assistant_settings.local_today(prefs, now)`；`jobs.LIVE`。
- Produces:

```python
GROUPS = ("owner", "stalled", "working", "others", "scheduled")
STALL_WORKING = 2
STALL_WAITING = 5
SCHEDULE_DAYS = 7

def workdays_between(start: date, end: date, workdays: Iterable[int]) -> int
def stage(row: AssistantFollowup, today: date) -> str          # owner / others / scheduled / working
def suggestion(row: AssistantFollowup) -> str | None
def is_stalled(stage_: str, row, job: AssistantJob | None, idle: int, today: date) -> bool

@dataclass(frozen=True)
class Item:
    row: AssistantFollowup
    stage: str
    stalled: bool
    idle_days: int
    job: AssistantJob | None

@dataclass(frozen=True)
class Board:
    today: date
    groups: dict[str, list[Item]]
    approvals: list[AssistantApproval]
    approvals_total: int
    approval_items: dict[uuid.UUID, int]
    def counts(self) -> dict[str, int]

async def pending_approvals(session, bot_id) -> tuple[list[AssistantApproval], int, datetime | None, dict[uuid.UUID, int]]
async def build(session: AsyncSession, bot_id: uuid.UUID, prefs: AssistantSettings, now: datetime) -> Board
```

- [ ] **Step 1: 写失败的单元测试**

`tests/unit/test_assistant_board_stage.py`：

```python
"""总表的阶段与停滞推导：纯函数，不碰库。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

from coreman.core.assistants import board

MON, TUE, WED, THU, FRI, SAT, SUN = (date(2026, 11, d) for d in range(16, 23))
WEEK = [1, 2, 3, 4, 5]


def row(**kw: object) -> SimpleNamespace:
    base = {
        "category": "promised", "status": "open", "needs_owner": False, "handler": "assistant",
        "review_on": None, "due_on": None, "notes_enc": None,
        "progress_at": datetime(2026, 11, 16, 2, tzinfo=UTC),
    }
    return SimpleNamespace(**{**base, **kw})


def test_workdays_between_skips_weekends() -> None:
    assert board.workdays_between(FRI, MON, WEEK) == 1
    assert board.workdays_between(MON, MON, WEEK) == 0
    assert board.workdays_between(MON, WED, WEEK) == 2
    assert board.workdays_between(FRI, SUN, WEEK) == 0
    assert board.workdays_between(FRI, SUN, [6, 7]) == 2


def test_stage_priority() -> None:
    assert board.stage(row(needs_owner=True, category="waiting"), MON) == "owner"
    assert board.stage(row(category="decide"), MON) == "owner"
    assert board.stage(row(category="waiting"), MON) == "others"
    assert board.stage(row(handler="person"), MON) == "others"
    assert board.stage(row(review_on=TUE), MON) == "scheduled"
    assert board.stage(row(status="snoozed", review_on=TUE), MON) == "scheduled"
    assert board.stage(row(review_on=MON), MON) == "working"
    assert board.stage(row(category="remind"), MON) == "working"


def test_stalled_thresholds() -> None:
    working = row()
    assert not board.is_stalled("working", working, None, 1, WED)
    assert board.is_stalled("working", working, None, 2, WED)
    running = SimpleNamespace(status="running")
    assert not board.is_stalled("working", working, running, 9, WED)
    waiting = row(category="waiting")
    assert not board.is_stalled("others", waiting, None, 4, WED)
    assert board.is_stalled("others", waiting, None, 5, WED)
    assert board.is_stalled("others", row(category="waiting", due_on=TUE), None, 0, WED)
    assert not board.is_stalled("owner", row(category="decide"), None, 30, WED)
    assert not board.is_stalled("scheduled", row(review_on=FRI), None, 30, WED)


def test_suggestion_line() -> None:
    assert board.suggestion(row(notes_enc="背景\n建议：选 A，因为便宜\n[2026-11-16] x")) == "选 A，因为便宜"
    assert board.suggestion(row(notes_enc="没有建议")) is None
    assert board.suggestion(row(notes_enc=None)) is None
    assert len(board.suggestion(row(notes_enc="建议：" + "长" * 300)) or "") == board.MAX_SUGGESTION
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/unit/test_assistant_board_stage.py -v`
Expected: FAIL（`coreman.core.assistants.board` 不存在）

- [ ] **Step 3: 实现 `board.py`**

```python
"""个人助理的事项总表：每条未结的待跟进推导出阶段与停滞，三处展示（页面、「看板」、摘要）共用。

阶段只在读取时算，不落库，免得与分类、子任务状态两份状态互相打架。优先级从上到下：
等你（needs_owner 或 decide）→ 等别人（waiting 或同事在办）→ 排期（回看日在今天之后）→ 在推进。
停滞只对「在推进」「等别人」判断：在推进的 2 个工作日没有进展；等别人的过了期限，或 5 个工作日
没有进展。进展时间取待跟进的 `progress_at` 与挂着的子任务最后一次变化中较晚的那个；子任务还在跑
就不算停滞。

在途审批单（写操作）单独列在「等你」里，第一期不挂到待跟进上。只在平台自己的代码里以元数据挑行，
内容由 `followups.list_` 还原。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, jobs, sealed
from coreman.core.assistants import settings as assistant_settings
from coreman.core.db.models import (
    AssistantApproval,
    AssistantFollowup,
    AssistantJob,
    AssistantSettings,
)
from coreman.core.timeutils import aware_utc

GROUPS = ("owner", "stalled", "working", "others", "scheduled")
STALL_WORKING = 2
STALL_WAITING = 5
# 卡片上「排期」只列这么多天以内的；页面列全部。
SCHEDULE_DAYS = 7
MAX_SUGGESTION = 100
# 摘要与看板里在途审批单最多列这么多张，多的只说个数。
APPROVALS_MAX = 10
_SUGGESTION = "建议："


def workdays_between(start: date, end: date, workdays: Iterable[int]) -> int:
    """`start` 之后到 `end`（含）之间有几个工作日；`end` 不晚于 `start` 时为 0。"""
    days = set(workdays)
    count, day = 0, start + timedelta(days=1)
    while day <= end:
        count += day.isoweekday() in days
        day += timedelta(days=1)
    return count


def stage(row: AssistantFollowup, today: date) -> str:
    if row.needs_owner or row.category == "decide":
        return "owner"
    if row.category == "waiting" or row.handler == "person":
        return "others"
    if row.review_on is not None and row.review_on > today:
        return "scheduled"
    return "working"


def is_stalled(
    stage_: str, row: AssistantFollowup, job: AssistantJob | None, idle: int, today: date
) -> bool:
    if stage_ == "others":
        return (row.due_on is not None and row.due_on < today) or idle >= STALL_WAITING
    if stage_ == "working":
        if job is not None and job.status in jobs.LIVE:
            return False
        return idle >= STALL_WORKING
    return False


def suggestion(row: AssistantFollowup) -> str | None:
    """备注里第一行以「建议：」开头的内容（助理给所有者拍板用的建议）。"""
    for line in (row.notes_enc or "").split("\n"):
        line = line.strip()
        if line.startswith(_SUGGESTION):
            text = line[len(_SUGGESTION):].strip()
            return text[:MAX_SUGGESTION] or None
    return None


@dataclass(frozen=True)
class Item:
    row: AssistantFollowup
    stage: str
    stalled: bool
    idle_days: int
    job: AssistantJob | None


@dataclass(frozen=True)
class Board:
    today: date
    groups: dict[str, list[Item]]
    approvals: list[AssistantApproval]
    approvals_total: int
    approval_items: dict[uuid.UUID, int]

    def counts(self) -> dict[str, int]:
        return {name: len(items) for name, items in self.groups.items()}


async def pending_approvals(
    session: AsyncSession, bot_id: uuid.UUID
) -> tuple[list[AssistantApproval], int, datetime | None, dict[uuid.UUID, int]]:
    """在途的审批单：每张单一行（早的在前，至多 `APPROVALS_MAX` 张，内容已还原）、总张数、最晚
    一张的提交时刻，以及合单的项数（按代表那张单的行的编号）。一轮里合成一张的几项只算一张。"""
    pending = (AssistantApproval.bot_id == bot_id, AssistantApproval.status == "pending")
    key = func.coalesce(AssistantApproval.batch_id, AssistantApproval.id)
    total, newest = (
        await session.execute(
            select(func.count(func.distinct(key)), func.max(AssistantApproval.created_at)).where(
                *pending
            )
        )
    ).one()
    grouped = (
        await session.execute(
            select(key, func.count())
            .where(*pending)
            .group_by(key)
            .order_by(func.min(AssistantApproval.created_at), key)
            .limit(APPROVALS_MAX)
        )
    ).all()
    counts = {group: int(count) for group, count in grouped}
    rows = list(
        await session.scalars(
            select(AssistantApproval)
            .where(*pending, key.in_(list(counts)))
            .order_by(AssistantApproval.created_at, AssistantApproval.id)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, rows)
    first: dict[uuid.UUID, AssistantApproval] = {}
    for row in rows:
        first.setdefault(row.batch_id or row.id, row)
    shown = [first[group] for group in counts if group in first]
    sizes = {first[group].id: counts[group] for group in counts if group in first}
    return shown, int(total or 0), newest, sizes


def _last(row: AssistantFollowup, job: AssistantJob | None) -> datetime:
    last = aware_utc(row.progress_at)
    if job is not None and job.updated_at is not None:
        last = max(last, aware_utc(job.updated_at))
    return last


async def build(
    session: AsyncSession, bot_id: uuid.UUID, prefs: AssistantSettings, now: datetime
) -> Board:
    """这位助理此刻的总表。会把到了回看日的推后条目改回未结（同 `followups.list_`）。"""
    today = assistant_settings.local_today(prefs, now)
    rows = await followups.list_(session, bot_id, "open", today=today, limit=followups.MAX_OPEN)
    job_ids = {row.job_id for row in rows if row.job_id is not None}
    linked: dict[uuid.UUID, AssistantJob] = {}
    if job_ids:
        found = list(
            await session.scalars(
                select(AssistantJob)
                .where(AssistantJob.bot_id == bot_id, AssistantJob.id.in_(job_ids))
                .execution_options(populate_existing=True)
            )
        )
        await sealed.open_rows(session, found)
        linked = {job.id: job for job in found}
    groups: dict[str, list[Item]] = {name: [] for name in GROUPS}
    for row in rows:
        job = linked.get(row.job_id) if row.job_id else None
        stage_ = stage(row, today)
        last_day = assistant_settings.local_today(prefs, _last(row, job))
        idle = workdays_between(last_day, today, prefs.workdays)
        stalled = is_stalled(stage_, row, job, idle, today)
        groups["stalled" if stalled else stage_].append(Item(row, stage_, stalled, idle, job))
    far = date.max
    groups["owner"].sort(key=lambda i: (i.row.due_on or far, i.row.seq))
    groups["stalled"].sort(key=lambda i: (-i.idle_days, i.row.seq))
    groups["scheduled"].sort(key=lambda i: (i.row.review_on or far, i.row.seq))
    approvals, total, _, sizes = await pending_approvals(session, bot_id)
    return Board(today, groups, approvals, total, sizes)
```

`pending_approvals` 就是 `digest._pending_approvals` 原样挪过来（`LIST_MAX` → `APPROVALS_MAX`）。`digest.py` 删掉原函数，`send_digest` 里改为调用 `board.pending_approvals`（Task 6 会进一步改写 `send_digest`；这一步只保证它照常可用）。

- [ ] **Step 4: 写失败的集成测试**

`tests/integration/test_assistant_board.py`：

```python
"""总表：按阶段分组、停滞判断、子任务在跑不算停滞、审批单单独列出、内容按密钥还原。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import board, followups
from coreman.core.assistants import settings as assistant_settings
from coreman.core.db.models import AssistantFollowup, AssistantJob
from tests.integration.approval_helpers import install, pending
from tests.integration.delivery_helpers import ready
from tests.integration.job_helpers import spawned

pytestmark = pytest.mark.usefixtures("assistant_master")
WED_NOON = datetime(2026, 11, 18, 4, 0, tzinfo=UTC)  # 北京周三 12:00
LAST_FRI = datetime(2026, 11, 13, 4, 0, tzinfo=UTC)


async def _age(session: AsyncSession, row: AssistantFollowup, at: datetime) -> None:
    await session.execute(
        update(AssistantFollowup).where(AssistantFollowup.id == row.id).values(progress_at=at)
    )


async def test_groups_and_stalls(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    prefs = await assistant_settings.get(db_session, bot.id)
    decide = await followups.add(
        db_session, bot.id, category="decide", title="选供应商", notes="建议：选 A"
    )
    fresh = await followups.add(db_session, bot.id, category="promised", title="写周报")
    stale = await followups.add(db_session, bot.id, category="promised", title="整理合同")
    waiting = await followups.add(
        db_session, bot.id, category="waiting", title="等赵六回复", due_on=WED_NOON.date() - timedelta(days=1)
    )
    later = await followups.add(
        db_session, bot.id, category="remind", title="续约", review_on=WED_NOON.date() + timedelta(days=3)
    )
    job = await spawned(db_session, bot)
    running = await followups.add(
        db_session, bot.id, category="promised", title="查退货", handler="job", job_id=job.id
    )
    for row in (stale, running):
        await _age(db_session, row, LAST_FRI)
    await db_session.execute(
        update(AssistantJob).where(AssistantJob.id == job.id).values(status="running", updated_at=LAST_FRI)
    )
    await db_session.commit()
    result = await board.build(db_session, bot.id, prefs, WED_NOON)
    seqs = {name: [i.row.seq for i in items] for name, items in result.groups.items()}
    assert seqs["owner"] == [decide.seq]
    # 停滞组按没动的工作日数从多到少：整理合同（上周五起 3 个工作日）在前，过期的等赵六在后。
    assert seqs["stalled"] == [stale.seq, waiting.seq]
    assert set(seqs["working"]) == {fresh.seq, running.seq}
    assert seqs["scheduled"] == [later.seq]
    assert result.counts()["owner"] == 1
    assert board.suggestion(result.groups["owner"][0].row) == "选 A"


async def test_pending_approvals_are_listed_separately(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner = await ready(db_session)
    scope = await handover_scope(db_session, bot, owner)
    await pending(db_session, scope)
    prefs = await assistant_settings.get(db_session, bot.id)
    result = await board.build(db_session, bot.id, prefs, WED_NOON)
    assert result.approvals_total == 1 and len(result.approvals) == 1
    # 审批单不算进待跟进的任何一组。
    assert sum(result.counts().values()) == 0
```

顶部导入补 `from tests.integration.test_assistant_digest import handover_scope`（它是那个测试模块里的辅助函数；若导入测试模块触发其模块级 fixture 有副作用，就把 `handover_scope` 挪到 `tests/integration/approval_helpers.py` 并让两处都从那里导入）。

- [ ] **Step 5: 运行，确认通过**

Run: `uv run pytest tests/unit/test_assistant_board_stage.py tests/integration/test_assistant_board.py tests/integration/test_assistant_digest.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add coreman/core/assistants/board.py coreman/core/assistants/digest.py tests/unit/test_assistant_board_stage.py tests/integration/test_assistant_board.py
git commit -m "个人助理总表：阶段与停滞推导"
```

---

### Task 5: 页面——`/board` 接口与总表视图

**Files:**
- Modify: `coreman/api/routers/me_assistant.py`（`/attention` → `/board`）
- Modify: `tests/api/test_my_assistant_attention.py` → 重命名为 `tests/api/test_my_assistant_board.py`
- Modify: `web/src/api/myAssistant.ts`、`web/src/i18n/assistant.ts`、`web/src/views/MyAssistantView.vue`
- Create: `web/src/components/assistant/AssistantBoard.vue`；Delete: `web/src/components/assistant/AssistantAttention.vue`
- Modify: `web/tests/AssistantSections.spec.ts`、`web/tests/MyAssistantView.spec.ts`

**Interfaces:**
- Consumes: `board.build`、`board.suggestion`、`owner_view.pending_view/job/target_names/approval_link`。
- Produces: `GET /me/assistant/board` →

```json
{"code": 0, "data": {
  "today": "2026-11-18",
  "counts": {"owner": 1, "stalled": 2, "working": 3, "others": 0, "scheduled": 1},
  "approvals": [/* owner_view.pending_view */],
  "approvals_total": 1,
  "groups": {"owner": [BoardItem], "stalled": [], "working": [], "others": [], "scheduled": []}
}}
```

`BoardItem` = `followups.view(row, notes=False)` 加 `stage`、`stalled`、`idle_days`、`suggestion`、`job`（`owner_view.job(...)` 或 null）。

- [ ] **Step 1: 改后端测试**

把 `tests/api/test_my_assistant_attention.py` `git mv` 成 `tests/api/test_my_assistant_board.py`。`ATTENTION` 常量改名 `BOARD = f"{URL}/board"`。原来针对 `/attention` 的用例改为断言新结构：

- 待批的单在 `data["approvals"]`，只给摘要与链接（原断言保留）；
- 被卡住的子任务挂在待跟进上时出现在对应条目的 `job` 里（不挂的不再单独列出——这是有意的变化，在提交说明里写明）；
- 今天到期的待跟进出现在 `groups` 里；
- 访问记录里有 `followups.read board`、`approvals.read pending`、`jobs.read board`；
- 非所有者 / 机器人令牌 403（原用例保留，只换地址）。

再加一个用例：

```python
async def test_board_groups_and_suggestion(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    bot, owner = await owner_with_key(db_session)
    await followups.add(db_session, bot.id, category="decide", title="选供应商", notes="背景\n建议：选 A")
    await followups.add(db_session, bot.id, category="waiting", title="等赵六", handler="person", handler_name="赵六")
    await db_session.commit()
    await login_existing(client, owner)
    body = (await client.get(BOARD)).json()["data"]
    assert body["counts"]["owner"] == 1 and body["counts"]["others"] == 1
    (item,) = body["groups"]["owner"]
    assert item["id"] == "F001" and item["suggestion"] == "选 A" and "notes" not in item
    assert body["groups"]["others"][0]["handler_name"] == "赵六"
```

（`owner_with_key` 与 `login_existing` 的确切签名以现有 attention 测试里的用法为准。）

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/api/test_my_assistant_board.py -v`
Expected: FAIL（404）

- [ ] **Step 3: 实现接口**

`me_assistant.py`：删掉 `attention()`，加：

```python
@router.get("/board")
async def board_view(
    own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """总表：所有未结的待跟进按阶段分组（等你、停滞、在推进、等别人、排期），加上在途审批单。"""
    bot, actor = own.bot, own.actor
    prefs = await assistant_settings.get(session, bot.id)
    result = await board.build(session, bot.id, prefs, utcnow())
    rows = [item.row for items in result.groups.values() for item in items]
    linked = [item.job for items in result.groups.values() for item in items if item.job]
    # 核心函数已经还原过；这里按查看者再核一次。
    await sealed.open_rows(session, [*rows, *linked], viewer=actor.id)
    names = await owner_view.target_names(session, linked)
    link = owner_view.approval_link()
    pending = await owner_view.approvals(session, bot, actor.id, owner_view.PENDING)

    def item(entry: board.Item) -> dict[str, Any]:
        out = followups.view(entry.row, notes=False)
        out.update(
            stage=entry.stage,
            stalled=entry.stalled,
            idle_days=entry.idle_days,
            suggestion=board.suggestion(entry.row),
            job=owner_view.job(entry.job, names) if entry.job else None,
        )
        return out

    await access_log.record(session, bot.id, actor.id, "approvals.read", "pending")
    await access_log.record(session, bot.id, actor.id, "followups.read", "board")
    await access_log.record(session, bot.id, actor.id, "jobs.read", "board")
    data = {
        "today": result.today.isoformat(),
        "counts": result.counts(),
        "approvals": [owner_view.pending_view(row, link) for row in pending],
        "approvals_total": len(pending),
        "groups": {name: [item(e) for e in entries] for name, entries in result.groups.items()},
    }
    # 读取会把到了回看日的推后条目改回未结；访问记录与读取同一个事务。
    await session.commit()
    return {"code": 0, "data": data}
```

导入里加 `board`；`jobs` 若因此不再使用就删掉；`JOB_LIMIT` 若只给 attention 用也一并删。模块 docstring 里「等你出手」改成「总表」。

先确认 `access_log.record` 的第五个参数是否有白名单（`grep -n "def record" -A20 coreman/core/assistants/access_log.py`）；有的话把 `"board"` 加进去。

- [ ] **Step 4: 运行后端测试**

Run: `uv run pytest tests/api/test_my_assistant_board.py tests/api/test_my_assistant_access_log.py -v`
Expected: PASS

- [ ] **Step 5: 前端类型、接口与文案**

`web/src/api/myAssistant.ts`：删 `Attention` 与 `attention()`，加：

```ts
export type BoardGroup = 'owner' | 'stalled' | 'working' | 'others' | 'scheduled'
export interface BoardItem extends Followup {
  handler: 'assistant' | 'job' | 'person' | 'owner'
  handler_name: string | null
  job_id: string | null
  progress_at: string
  stage: Exclude<BoardGroup, 'stalled'>
  stalled: boolean
  idle_days: number
  suggestion: string | null
  job: AssistantJob | null
}
export interface Board {
  today: string
  counts: Record<BoardGroup, number>
  approvals: PendingApproval[]
  approvals_total: number
  groups: Record<BoardGroup, BoardItem[]>
}
```

`myAssistant` 里 `board: () => call<Board>(http.get(\`${root}/board\`)),`。`Followup` 接口若已有 `needs_owner` 等字段照旧；`BoardItem` 不含 `notes`。

`web/src/i18n/assistant.ts`：`page.tabs.attention` 换成 `board: '总表'`（键名改为 `board`，顺序放第一）。新增：

```ts
  board: {
    groups: { owner: '等你', stalled: '停滞', working: '在推进', others: '等别人', scheduled: '排期' },
    empty: { owner: '没有等你的事。', stalled: '没有停滞的事。', working: '没有在推进的事。', others: '没有在等别人的事。', scheduled: '没有排期的事。' },
    approvals: '待批的写操作（{n}）', decisions: '等你拍板或当面处理（{n}）',
    handler: { assistant: '助理在办', job: '子任务在办', person: '{name} 在办', owner: '你在办' },
    idle: '{n} 个工作日没有进展', reviewOn: '{date} 回看', dueOn: '期限 {date}',
    suggestion: '建议：{text}', stage: { owner: '等你', working: '在推进', others: '等别人', scheduled: '排期' },
  },
```

`attention` 下仍被 `AssistantRecent` / `PROXIED_TOOLS` 使用的键（`dispatch`、`systemsCall`、`cloudCall`、`open`、`noLink`、`submittedAt`）保留；只剩 `AssistantAttention.vue` 用的键（`jobs`、`jobsEmpty`、`followups`、`followupsEmpty`、`blockedReason` 若别处也没用）删掉——删前 `grep -rn "assistant.attention\." web/src` 逐个确认。`web/tests/assistantI18n.spec.ts` 若校验键集合，按需更新。

- [ ] **Step 6: 前端组件**

`web/src/components/assistant/AssistantBoard.vue`：

```vue
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { DISPATCH_TOOL, PROXIED_TOOLS, myAssistant, type Board, type BoardGroup, type BoardItem } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const GROUPS: BoardGroup[] = ['owner', 'stalled', 'working', 'others', 'scheduled']
const data = ref<Board | null>(null)
const error = ref(''), loading = ref(false)
async function load() {
  loading.value = true; error.value = ''
  try { data.value = await myAssistant.board() }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
function handler(item: BoardItem) {
  return t('assistant.board.handler.' + item.handler, { name: item.handler_name ?? '' })
}
onMounted(load)
defineExpose({ load })
</script>
<template>
  <section class="assistant-section">
    <LoadState :loading="loading && !data" :error="error" @retry="load" />
    <template v-if="data">
      <p class="muted" data-test="board-counts">
        <span v-for="g in GROUPS" :key="g">{{ t('assistant.board.groups.' + g) }} {{ data.counts[g] }}　</span>
      </p>
      <template v-for="g in GROUPS" :key="g">
        <h3>{{ t('assistant.board.groups.' + g) }}（{{ data.counts[g] + (g === 'owner' ? data.approvals_total : 0) }}）</h3>
        <template v-if="g === 'owner' && data.approvals.length">
          <h4>{{ t('assistant.board.approvals', { n: data.approvals_total }) }}</h4>
          <ul>
            <li v-for="a in data.approvals" :key="a.id" :data-test="'approval-' + a.id">
              <strong>{{ a.summary ?? t('assistant.page.lockedText') }}</strong>
              <el-tag v-if="a.tool === DISPATCH_TOOL" size="small" type="warning">{{ t('assistant.attention.dispatch') }}</el-tag>
              <el-tag v-if="PROXIED_TOOLS[a.tool]" size="small" type="warning">{{ t('assistant.attention.' + PROXIED_TOOLS[a.tool]) }}</el-tag>
              <span class="muted">{{ t('assistant.attention.submittedAt', { time: formatDateTime(a.created_at) }) }}</span>
              <!-- 链接只可能是部署配置的审批系统入口（后端只放行 http/https）。 -->
              <a v-if="a.link" :href="a.link" target="_blank" rel="noopener noreferrer">{{ t('assistant.attention.open') }}</a>
              <span v-else class="muted">{{ t('assistant.attention.noLink') }}</span>
            </li>
          </ul>
          <h4 v-if="data.groups.owner.length">{{ t('assistant.board.decisions', { n: data.counts.owner }) }}</h4>
        </template>
        <p v-if="!data.groups[g].length && !(g === 'owner' && data.approvals.length)" class="muted">
          {{ t('assistant.board.empty.' + g) }}
        </p>
        <ul v-else-if="data.groups[g].length">
          <li v-for="item in data.groups[g]" :key="item.id" :data-test="'board-' + item.id">
            <strong>{{ item.id }} {{ item.title || t('assistant.page.lockedText') }}</strong>
            <el-tag v-if="g === 'stalled'" size="small" type="danger">{{ t('assistant.board.stage.' + item.stage) }}</el-tag>
            <span class="muted">{{ handler(item) }}</span>
            <span v-if="item.idle_days > 0" class="muted">· {{ t('assistant.board.idle', { n: item.idle_days }) }}</span>
            <span v-if="item.due_on" class="muted">· {{ t('assistant.board.dueOn', { date: item.due_on }) }}</span>
            <span v-if="g === 'scheduled' && item.review_on" class="muted">· {{ t('assistant.board.reviewOn', { date: item.review_on }) }}</span>
            <div v-if="item.suggestion" class="muted">{{ t('assistant.board.suggestion', { text: item.suggestion }) }}</div>
          </li>
        </ul>
      </template>
    </template>
  </section>
</template>
```

照现有组件的 eslint 规则（Vue 模板一属性一行）用 `npm run lint -- --fix` 整理格式。删掉 `AssistantAttention.vue`。

`MyAssistantView.vue`：导入换成 `AssistantBoard`；第一个分区 `name="board"`、标签 `t('assistant.page.tabs.board')`；`tab` 的默认值从 `'attention'` 改成 `'board'`（`grep -n "attention" web/src/views/MyAssistantView.vue` 找全，包括 URL 查询参数同步的地方）。

- [ ] **Step 7: 前端测试**

`web/tests/AssistantSections.spec.ts`：mock 里 `attention` 换成 `board`；`describe('needs you')` 改为 `describe('board')`，数据：

```ts
const item = (over: Partial<BoardItem>): BoardItem => ({
  id: 'F001', category: 'decide', status: 'open', title: '选供应商', review_on: null, due_on: null,
  origin_message_id: null, created_at: '2026-11-13T00:00:00Z', needs_owner: false, has_notes: true,
  handler: 'assistant', handler_name: null, job_id: null, progress_at: '2026-11-13T00:00:00Z',
  stage: 'owner', stalled: false, idle_days: 0, suggestion: '选 A', job: null, ...over,
})
const boardData: Board = {
  today: '2026-11-18',
  counts: { owner: 1, stalled: 1, working: 0, others: 0, scheduled: 0 },
  approvals: [{ id: 'a1', tool: 'feishu_im_send_message', summary: '私聊 → 李四', created_at: '2026-11-13T01:00:00Z', link: 'https://tower.example.com' }],
  approvals_total: 1,
  groups: {
    owner: [item({})],
    stalled: [item({ id: 'F002', category: 'waiting', stage: 'others', stalled: true, idle_days: 6, handler: 'person', handler_name: '赵六', suggestion: null })],
    working: [], others: [], scheduled: [],
  },
}
```

用例：

```ts
it('lists approvals and decisions under 等你, stalled items with who and how long', async () => {
  const wrapper = render(AssistantBoard); await flushPromises()
  expect(wrapper.get('[data-test="approval-a1"] a').attributes('href')).toBe('https://tower.example.com')
  expect(wrapper.get('[data-test="board-F001"]').text()).toContain('建议：选 A')
  const stalled = wrapper.get('[data-test="board-F002"]').text()
  expect(stalled).toContain('赵六 在办'); expect(stalled).toContain('6 个工作日没有进展'); expect(stalled).toContain('等别人')
  expect(wrapper.text()).toContain('没有在推进的事。')
})
```

（`Followup` 的确切字段以 `myAssistant.ts` 为准，缺什么补什么。）原 attention 用例里「链接只放行 http/https」那类断言迁到新用例里。`web/tests/MyAssistantView.spec.ts` 里默认分区的断言改为 `board`。

- [ ] **Step 8: 运行前端检查**

Run: `cd web && npx vitest run tests/AssistantSections.spec.ts tests/MyAssistantView.spec.ts tests/assistantI18n.spec.ts && npm run lint && npx vue-tsc --noEmit`
Expected: PASS（`vue-tsc` 若不在依赖里，用 `package.json` 里现有的类型检查命令）

- [ ] **Step 9: Commit**

```bash
git add coreman/api/routers/me_assistant.py tests/api web
git commit -m "「我的助理」页：总表替换等你出手，默认显示"
```

---

### Task 6: 飞书——摘要改版与「看板」指令

**Files:**
- Modify: `coreman/core/assistants/digest.py`
- Create: `coreman/runtime/worker/assistant_board.py`
- Modify: `coreman/runtime/worker/chat/intake.py:126-133`、`coreman/runtime/worker/chat/queued_batch.py:26,105`
- Test: `tests/integration/test_assistant_digest.py`、`tests/integration/test_assistant_board_command.py`（新）

**Interfaces:**
- Consumes: `board.build`、`board.suggestion`、`board.SCHEDULE_DAYS`、`board.APPROVALS_MAX`。
- Produces:
  - `digest.board_card(result: board.Board, *, requester: str, link: str | None, system: str | None, full: bool, reports: list[str] | None = None) -> tuple[dict, list[int]]`（返回卡片与卡片上「现在处理」按钮对应的编号）
  - `digest.send_board(session, bot, prefs, now, *, key_suffix: str) -> None`
  - `assistant_board.COMMANDS = frozenset({"看板", "/看板", "有什么事"})`、`assistant_board.intercept(session, ctx, bot, speaker, chat_id, text) -> bool`

卡片的两种形态：
- `full=True`（「看板」指令）：计数行 → 等你（审批单 + 纯决策，各自小标题）→ 停滞 → 在推进 → 等别人 → 排期（只列 `SCHEDULE_DAYS` 天以内的）。
- `full=False`（摘要）：计数行 → 等你 → 停滞 → 我处理了这些（`reports`）。在推进、等别人、排期只在计数行里出现。

每组最多 `LIST_MAX`（10）条，多的写「另有 N 件，发「看板」或在「我的助理」页看全部」。每条一行：`F012 标题 · 谁在办 · N 个工作日没动`，有建议的下一行灰字「建议：…」。「等你」里的纯决策条目，以及停滞组里阶段为「等你」的条目，每条下面一个「现在处理」按钮，沿用现有的 `reply_value`、`_row`、`_button`，按钮编号进 `_digest_rows`，关闭时由 `retire` 置灰。

- [ ] **Step 1: 写失败的测试——摘要**

改 `tests/integration/test_assistant_digest.py::test_the_digest_lists_approvals_owner_items_and_reports`：

```python
    body = texts(card)
    assert body.index("总表") < body.index("等你（") < body.index("我处理了这些")
    assert "待批的写操作（1）" in body and "等你拍板或当面处理（1）" in body
    assert "[去审批台处理](https://tower.example.com/)" in body
    assert "F001 是否接受新供应商的报价" in body
    # 等别人的事不展开，只进计数行。
    assert "等赵六回复" not in body and "等别人 1" in body
```

（其余按钮、`_digest_rows`、密文断言不变。）再加一个用例：`decide` 类、没标 `needs_owner` 的条目也出现在摘要的「等你」里并带按钮；以及「已有停滞条目但没有新东西时不重复发」——摘要的「新东西」判断保持原规则（新汇报、新审批单、上次摘要后新记或改过的「等你」条目），停滞本身不触发发送。

- [ ] **Step 2: 写失败的测试——「看板」指令**

`tests/integration/test_assistant_board_command.py`，照 `tests/integration/test_assistant_shift_command.py` 的结构（`active_assistant`、`say`、`last_sent`、`install`）：

```python
"""私聊「看板」：只有所有者能调出，平台直接回卡片，不经模型、不开轮次。"""

async def test_owner_gets_the_board_without_the_model(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await active_assistant(db_session)
    await identified(db_session, bot)  # 让设置行有 chat_id 与 platform_user_id；按 helper 实际签名调用
    await followups.add(db_session, bot.id, category="decide", title="选供应商", notes="建议：选 A")
    await followups.add(db_session, bot.id, category="waiting", title="等赵六回复")
    await db_session.commit()
    done, relay = await say(db_engine, db_session, bot, "看板")
    assert done.status == "succeeded" and done.result == {"assistant": "board_sent"}
    assert relay.requests == []
    sent = await last_sent(db_session, bot)
    assert sent is not None and sent.dedupe_key.startswith(f"assistant-digest:{bot.id}:board-")
    body = json.dumps(sent.payload["card"], ensure_ascii=False)
    assert "F001 选供应商" in body and "建议：选 A" in body and "等赵六回复" in body
    assert sent.payload["_digest_rows"] == [1]


async def test_someone_else_gets_nothing(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, _, _ = await active_assistant(db_session)
    done, relay = await say(db_engine, db_session, bot, "看板", sender="stranger_pid")
    assert done.result == {"assistant": "board_not_owner"} and relay.requests == []
    assert await last_sent(db_session, bot) is None


async def test_board_word_is_not_merged_into_a_running_turn() -> None:
    from coreman.runtime.worker import assistant_board
    assert "看板" in assistant_board.COMMANDS
```

再加一个：所有者发「看板」后关闭 F001，`digest.retire` 会给这张卡片排一次置灰（断言出现 `card_update` 出站记录）。`say()` 是否支持 `sender=` 以 `tests/integration/shift_helpers.py` 为准，不支持就照 `test_assistant_stop_command.py` 直接用 `chat_task(..., sender=...)`。

- [ ] **Step 3: 运行，确认失败**

Run: `uv run pytest tests/integration/test_assistant_digest.py tests/integration/test_assistant_board_command.py -v`
Expected: FAIL

- [ ] **Step 4: 实现卡片**

`digest.py`：

1. 把现有 `digest_card` 里「等你当面处理的」那段按钮代码抽成：

```python
def _now_button(seq: int, requester: str) -> dict[str, Any]:
    label = now_text(seq)
    return {
        "tag": "column_set",
        "element_id": _row(seq),
        "flex_mode": "flow",
        "columns": [
            {
                "tag": "column",
                "width": "auto",
                "elements": [
                    {
                        "tag": "button",
                        "element_id": _button(seq),
                        "type": "primary",
                        "size": "small",
                        "text": {"tag": "plain_text", "content": label},
                        "behaviors": [
                            {
                                "type": "callback",
                                "value": reply_value(
                                    label, requester=requester, row=_row(seq), buttons=[_button(seq)]
                                ),
                            }
                        ],
                    }
                ],
            }
        ],
    }
```

2. 新增：

```python
GROUP_TITLES = {
    "owner": "等你", "stalled": "停滞", "working": "在推进", "others": "等别人", "scheduled": "排期",
}
MORE_HINT = "另有 {n} 件，发「看板」或在「我的助理」页看全部"


def _who(item: board.Item) -> str:
    row = item.row
    if row.handler == "person":
        return f"{_clean(row.handler_name_enc or '同事')} 在办"
    if row.handler == "job":
        return "公共员工在办" if item.job is not None and item.job.kind == "bot_help" else "子任务在办"
    return "你在办" if row.handler == "owner" else "助理在办"


def _line(item: board.Item) -> str:
    parts = [f"{followups.label(item.row.seq)} {_clean(item.row.title_enc or '')}", _who(item)]
    if item.idle_days > 0 and item.stage in ("working", "others"):
        parts.append(f"{item.idle_days} 个工作日没动")
    if item.row.due_on is not None:
        parts.append(f"期限 {item.row.due_on.isoformat()}")
    return " · ".join(parts)


def _items(
    elements: list[dict[str, Any]], items: list[board.Item], requester: str, buttons: list[int]
) -> None:
    for item in items[:LIST_MAX]:
        elements.append(_plain(_line(item)))
        tip = board.suggestion(item.row)
        if tip:
            elements.append(_plain(f"建议：{tip}", "notation"))
        if item.stage == "owner":
            elements.append(_now_button(item.row.seq, requester))
            buttons.append(item.row.seq)
    if len(items) > LIST_MAX:
        elements.append(_plain(MORE_HINT.format(n=len(items) - LIST_MAX), "notation"))


def board_card(
    result: board.Board,
    *,
    requester: str,
    link: str | None,
    system: str | None,
    full: bool,
    reports: list[str] | None = None,
) -> tuple[dict[str, Any], list[int]]:
    """总表卡片。`full` 是「看板」指令：五组全列；摘要只展开等你与停滞，其余只进计数行。"""
    counts = result.counts()
    elements: list[dict[str, Any]] = [
        _plain(
            "总表："
            + " · ".join(f"{GROUP_TITLES[g]} {counts[g]}" for g in board.GROUPS)
            + (f" · 待批 {result.approvals_total}" if result.approvals_total else ""),
            "notation",
        )
    ]
    buttons: list[int] = []
    owner = result.groups["owner"]
    if result.approvals or owner:
        elements.append(_heading(f"等你（{result.approvals_total + len(owner)}）"))
    if result.approvals:
        elements.append(_heading(f"待批的写操作（{result.approvals_total}）"))
        sizes = result.approval_items
        elements.append(
            _plain("\n".join(f"· {_approval_summary(r, sizes.get(r.id, 1))}" for r in result.approvals))
        )
        if result.approvals_total > len(result.approvals):
            elements.append(_plain(f"另有 {result.approvals_total - len(result.approvals)} 张", "notation"))
        name = system or "审批系统"
        elements.append(
            {"tag": "markdown", "content": f"[去{name}处理]({link})"}
            if link
            else _plain(f"请到{name}里处理", "notation")
        )
    if owner:
        elements.append(_heading(f"等你拍板或当面处理（{len(owner)}）"))
        _items(elements, owner, requester, buttons)
    shown = ["stalled", "working", "others", "scheduled"] if full else ["stalled"]
    horizon = result.today + timedelta(days=board.SCHEDULE_DAYS)
    for group in shown:
        items = result.groups[group]
        if group == "scheduled":
            items = [i for i in items if i.row.review_on is None or i.row.review_on <= horizon]
        if not items:
            continue
        elements.append(_heading(f"{GROUP_TITLES[group]}（{len(items)}）"))
        _items(elements, items, requester, buttons)
    if reports:
        elements.append(_heading("我处理了这些"))
        shown_reports = reports[:REPORTS_MAX]
        elements.append(_plain("\n".join(f"· {line}" for line in shown_reports)))
        if len(reports) > len(shown_reports):
            elements.append(_plain(f"另有 {len(reports) - len(shown_reports)} 条", "notation"))
    if not full:
        elements.append(
            {
                "tag": "markdown",
                "content": (
                    "<font color='grey'>不急的事攒到摘要时点一起说；随时发「看板」看全部。摘要时点与"
                    "免打扰时段可以在「我的助理」页或这里改。</font>"
                ),
                "text_size": "notation",
            }
        )
    title = "事项看板" if full else "助理摘要"
    return _card(title, "blue", elements), buttons
```

内容读不出来的行（`sealed.is_locked(item.row)`）在 `_items` 里跳过，与原 `send_digest` 的做法一致：在 `_items` 循环开头加 `if sealed.is_locked(item.row): continue`。

3. 删掉旧 `digest_card`。`send_digest` 改写为：

```python
    reports = await _reports(session, bot.id, urgent_only=False)
    result = await board.build(session, bot.id, prefs, now)
    _, _, newest, _ = await board.pending_approvals(session, bot.id)
    lines = _lines(reports)
    since = prefs.digest_sent_at
    owner = result.groups["owner"] + [i for i in result.groups["stalled"] if i.stage == "owner"]
    fresh = (
        bool(lines)
        or _newer(newest, since)
        or any(_newer(item.row.updated_at, since) for item in owner)
    )
    if not fresh:
        await _mark_sent(session, reports, now)
        return False
    settings = get_settings()
    card, rows = board_card(
        result,
        requester=prefs.platform_user_id,
        link=tower.web_url(settings),
        system=(settings.tower_display_name or "").strip() or None,
        full=False,
        reports=lines,
    )
    stamp = int(aware_utc(now).timestamp())
    await _send(session, bot.id, prefs.chat_id, f"{DIGEST_KEY}:{bot.id}:{stamp}", card, rows)
    await _mark_sent(session, reports, now)
    prefs.digest_sent_at = now
    log.info(
        "assistant_digest_sent",
        bot_id=str(bot.id),
        reports=len(lines),
        approvals=result.approvals_total,
        followups=len(owner),
    )
    return True
```

（`board.build` 内部已调过一次 `pending_approvals`；为拿到 `newest` 再调一次可以接受。嫌重复就给 `Board` 加 `approvals_newest` 字段，二选一，选了就在 Task 4 的接口里同步。）

`followups.needs_owner` / `needs_owner_count` 若因此不再被任何地方使用就删掉（`grep -rn "needs_owner_count\|followups.needs_owner(" coreman`），对应测试一并删。模块 docstring 的「摘要三节」改为现在的结构。`_fallback` 不用改。

4. 新增：

```python
async def send_board(
    session: AsyncSession, bot: Bot, prefs: AssistantSettings, now: datetime, *, key_suffix: str
) -> None:
    """所有者发了「看板」：立刻回一张完整的总表卡片。调用方提交。"""
    result = await board.build(session, bot.id, prefs, now)
    settings = get_settings()
    card, rows = board_card(
        result,
        requester=prefs.platform_user_id,
        link=tower.web_url(settings),
        system=(settings.tower_display_name or "").strip() or None,
        full=True,
    )
    # 去重键与摘要同一前缀：关闭待跟进时 retire 也能找到这张卡片置灰。
    await _send(session, bot.id, prefs.chat_id, f"{DIGEST_KEY}:{bot.id}:board-{key_suffix}", card, rows)
    log.info("assistant_board_sent", bot_id=str(bot.id), **result.counts())
```

`digest.py` 顶部 `from coreman.core.assistants import board, followups, quoting, sealed`。

- [ ] **Step 5: 实现指令**

`coreman/runtime/worker/assistant_board.py`：

```python
"""所有者在私聊里发「看板」：平台直接回一张事项总表卡片，不经模型、不开轮次。

与「上班」「下班」一样在入站阶段认出发言人后截下（`chat/intake.py`），只截私聊里的普通聊天消息；
不是所有者发的就此结束、不回话。开通没走完或设置行还没有身份时不截，交给后面的流程。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import digest
from coreman.core.assistants import settings as assistant_settings
from coreman.core.bus import tasks
from coreman.core.db.models import AssistantSettings, Bot
from coreman.core.prompting import Speaker
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.context import TaskContext

COMMANDS = frozenset({"看板", "/看板", "有什么事"})


async def intercept(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    speaker: Speaker,
    chat_id: str,
    text: str,
) -> bool:
    """是「看板」就处理掉并返回真，其余返回假。写在调用方的事务里，由调用方提交。"""
    if text.strip() not in COMMANDS:
        return False
    owner = speaker.user_id is not None and speaker.user_id == bot.owner_id
    prefs = await session.get(AssistantSettings, bot.id) if owner else None
    if owner and (
        bot.assistant_state != "active" or prefs is None or not assistant_settings.identified(prefs)
    ):
        return False
    result = "board_not_owner"
    if owner and prefs is not None:
        await digest.send_board(session, bot, prefs, utcnow(), key_suffix=str(ctx.task.id))
        result = "board_sent"
    await tasks.finish(
        session,
        ctx.task.id,
        status="succeeded",
        result={"assistant": result},
        only_active=True,
        bot_id=bot.id,
    )
    return True
```

（`chat_id` 参数保留与另两个拦截函数同形；卡片发到设置行记下的私聊。若 `assistant_settings.identified` 的签名不同，以实际为准。）

`chat/intake.py`：导入行改成 `from coreman.runtime.worker import assistant_board, assistant_shift, assistant_stop`，在 `assistant_shift.intercept` 之后加：

```python
            # 「看板」同样在这里截下，平台直接回总表卡片。
            if await assistant_board.intercept(session, ctx, bot, speaker, chat_id, text):
                return None
```

`chat/queued_batch.py`：导入加 `assistant_board`，合并判断里 `text in assistant_stop.COMMANDS or text in assistant_shift.RESERVED` 后加 `or text in assistant_board.COMMANDS`，注释改成「停用、上下班、看板命令」。

- [ ] **Step 6: 运行，确认通过**

Run: `uv run pytest tests/integration/test_assistant_digest.py tests/integration/test_assistant_board_command.py tests/integration/test_assistant_shift_command.py tests/integration/test_assistant_stop_command.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add coreman/core/assistants/digest.py coreman/core/assistants/followups.py coreman/runtime/worker tests/integration
git commit -m "飞书：摘要改成总表的等你与停滞，私聊「看板」直接回总表卡片"
```

---

### Task 7: 文档、介绍页与全量验证

**Files:**
- Modify: `docs/superpowers/specs/2026-11-11-personal-assistant.md`（「待跟进」「九、我的助理页」两节）
- Modify: `CHANGELOG.md`（Unreleased）
- Modify: `docs/superpowers/specs/2026-11-18-personal-assistant-board-and-gaps.md`（「分期」第 1 期标为已实现，写明与规格不同的地方）
- 检查：`web/public/assistant-intro/index.html` 里是否提到「等你出手」，提到就改成「总表」

- [ ] **Step 1: 改文档**

`2026-11-11-personal-assistant.md` 的「待跟进」节补：谁在办（四种）、`followup_progress`、总表阶段与停滞规则（照 Global Constraints 的阈值），以及私聊「看板」指令；「我的助理页」节把「等你出手」改为「总表（默认）」。

规格 `2026-11-18-personal-assistant-board-and-gaps.md` 的「分期」第 1 条后注明已实现，并写明与规格的两处差别：
- 规格写「早报第一段换成总表」，实现是每次摘要卡片都以总表计数行开头、先列等你与停滞（早报内容本来就随摘要发出）；
- 被卡住但没挂在待跟进上的子任务不再单独列在页面上（原「等你出手」有），助理按新规则应把它挂到对应条目。

`CHANGELOG.md` Unreleased 下加：

```markdown
- 个人助理事项总表：待跟进记下谁在办与最近进展，按等你 / 停滞 / 在推进 / 等别人 / 排期分组；私聊发「看板」直接看总表，摘要改为先列等你与停滞的事；「我的助理」页默认显示总表（替换「等你出手」）。新工具 `followup_progress`。迁移 0114。
```

- [ ] **Step 2: 全量测试与检查**

Run: `TEST_DATABASE_URL=<本机测试库，库名用 coreman_test_board> uv run pytest -n auto -q`
Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman`
Run: `cd web && npx vitest run && npm run lint`
Expected: 全部通过。`tests/integration/test_assistant_transcripts.py::test_destroyed_key_or_tampered_blob_starts_fresh` 约 1/256 偶发失败，与本改动无关，遇到就重跑。

- [ ] **Step 3: 内部信息自查**

按维护者本机的提交前自查约定，检查新增内容里没有公司名、内部域名、内部系统名与本机路径（检查用的关键词清单不写进仓库）。

- [ ] **Step 4: Commit**

```bash
git add docs CHANGELOG.md web/public/assistant-intro/index.html
git commit -m "文档：个人助理事项总表"
```

---

## 上线须知（给执行者）

- 无需升级节点：改动全在服务端与网关拦截。
- 迁移 0114 只加列与约束、回填两条 UPDATE；表小，`lock_timeout` 5 秒。
- 上线后请所有者在私聊发「看板」验收：等你 / 停滞两组内容是否符合预期、「现在处理」按钮能否点、关闭条目后按钮是否置灰。
- 老条目的 `progress_at` 回填为 `updated_at`，上线当天就可能有一批显示为「停滞」，这是预期的：它们确实很久没动了。
