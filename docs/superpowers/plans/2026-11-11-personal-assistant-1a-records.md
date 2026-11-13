# 个人助理 1A：助理记录与应用凭证 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 库里能存在「个人助理」类型的机器人记录：一人一条、挂在共用的飞书应用下、凭证随应用同步，并在服务端拒绝对它开启不允许的能力。

**Architecture:** `bots` 加三列（`kind`、`owner_id`、`platform_app_id`）与约束。应用凭证以 `platform_apps` 为准，助理记录的 `credentials_enc` 是副本，应用变更时同事务同步，因此现有按记录读凭证的代码不动。类型判断和锁定字段集中在新包 `coreman/core/assistants/`，路由只调用它。

**Tech Stack:** Python、FastAPI、SQLAlchemy async、Alembic、PostgreSQL、pytest。

**Spec:** `docs/superpowers/specs/2026-11-11-personal-assistant.md`（第一节）；总览 `docs/superpowers/plans/2026-11-11-personal-assistant-phase1.md`

## Global Constraints

- 开工前合并最新 `main`：写本计划时迁移头是 `0085`，本计划新迁移是 `0086`；头变了就顺延并同步改文件名与 `down_revision`。
- 公开仓：代码、测试、提交信息只用通用占位（`example.com`、`demo-bot`、`cli_example`），提交前跑仓库约定的敏感词自查。
- 普通员工（`kind='standard'`）的行为不变。
- 本计划不建网关连接、不接收消息、不提供开通入口（分别属于 1B、1F）。助理记录在本计划里只由测试和 `new_assistant_bot` 创建。
- 一个部署对应一个飞书企业：同一 `owner_id` 同时只能有一条启用的助理记录。
- 验证命令：`uv run pytest <路径>`、`uv run ruff check .`、`uv run mypy coreman`、`uv run alembic check`（`TEST_DATABASE_URL` 按本机配置）。

## File Structure

| 文件 | 职责 |
|---|---|
| `migrations/versions/0086_assistant_bots.py` | 三列、两个检查约束、两个索引 |
| `coreman/core/db/models/bots.py` | 模型加列与 `BOT_KINDS` |
| `coreman/core/db/models/platform.py` | `CAPABILITIES` 加 `assistant` |
| `coreman/core/assistants/__init__.py` | 空包 |
| `coreman/core/assistants/records.py` | 类型判断、固定取值、锁定字段、可见性、建记录 |
| `coreman/core/assistants/apps.py` | 从应用取凭证、同步到助理记录 |
| `coreman/core/bots/platform_account.py` | 普通机器人不能占用助理应用 |
| `coreman/api/routers/platform_apps.py` | 校验与同步的调用点 |
| `coreman/api/routers/bots.py`、`bots_extra.py` | 锁定字段、成员与可用人、可见性的调用点 |

---

### Task 1: 表结构

**Files:**
- Create: `migrations/versions/0086_assistant_bots.py`
- Modify: `coreman/core/db/models/bots.py`（`BOT_KEY_RE` 下方加常量；`Bot.__table_args__` 与列）
- Modify: `coreman/core/db/models/platform.py:27`
- Modify: `coreman/core/db/models/__init__.py`（导出 `BOT_KINDS`）
- Test: `tests/integration/test_assistant_bot_schema.py`

**Interfaces:**
- Produces: `Bot.kind: str`、`Bot.owner_id: uuid.UUID | None`、`Bot.platform_app_id: uuid.UUID | None`、`BOT_KINDS = ("standard", "assistant")`；`CAPABILITIES` 含 `"assistant"`。

- [ ] **Step 1: 写失败测试**

```python
"""bots 的助理列与约束。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import BOT_KINDS, CAPABILITIES, Bot, PlatformApp, User


async def _owner_and_app(session: AsyncSession) -> tuple[User, PlatformApp]:
    owner = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name="所有者")
    app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="x",
    )
    session.add_all([owner, app])
    await session.flush()
    return owner, app


def _bot(owner: User, **over: object) -> Bot:
    values: dict[str, object] = {
        "bot_key": f"b-{uuid.uuid4().hex[:10]}",
        "platform": "feishu",
        "name": "demo-bot",
        "created_by": owner.id,
        "model": "m",
        "working_dir": "/d",
        "credentials_enc": "",
    }
    values.update(over)
    return Bot(**values)


def test_constants() -> None:
    assert BOT_KINDS == ("standard", "assistant")
    assert "assistant" in CAPABILITIES


async def test_standard_bot_defaults(db_session: AsyncSession) -> None:
    owner, _ = await _owner_and_app(db_session)
    bot = _bot(owner)
    db_session.add(bot)
    await db_session.flush()
    await db_session.refresh(bot)
    assert bot.kind == "standard" and bot.owner_id is None and bot.platform_app_id is None


async def test_assistant_requires_owner_and_app(db_session: AsyncSession) -> None:
    owner, app = await _owner_and_app(db_session)
    db_session.add(_bot(owner, kind="assistant", owner_id=owner.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
    owner, app = await _owner_and_app(db_session)
    db_session.add(_bot(owner, kind="assistant", owner_id=owner.id, platform_app_id=app.id))
    await db_session.flush()


async def test_standard_bot_cannot_carry_assistant_columns(db_session: AsyncSession) -> None:
    owner, app = await _owner_and_app(db_session)
    db_session.add(_bot(owner, platform_app_id=app.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_one_enabled_assistant_per_owner(db_session: AsyncSession) -> None:
    owner, app = await _owner_and_app(db_session)
    first = _bot(owner, kind="assistant", owner_id=owner.id, platform_app_id=app.id)
    db_session.add(first)
    await db_session.flush()
    first.enabled = False
    await db_session.flush()
    db_session.add(_bot(owner, kind="assistant", owner_id=owner.id, platform_app_id=app.id))
    await db_session.flush()  # 旧的已停用，可以再建一条
    db_session.add(_bot(owner, kind="assistant", owner_id=owner.id, platform_app_id=app.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_bot_schema.py -v`
Expected: FAIL，`ImportError: cannot import name 'BOT_KINDS'`。

- [ ] **Step 3: 改模型**

`coreman/core/db/models/platform.py:27` 改为：

```python
CAPABILITIES = ("contact_sync", "login", "notify", "callback", "assistant")
```

`coreman/core/db/models/bots.py`，在 `BOT_KEY_RE = ...` 下一行加：

```python
BOT_KINDS = ("standard", "assistant")
# 助理记录必须有所有者和所属应用，且只能是飞书；普通记录不能带这两列。
ASSISTANT_SHAPE = (
    "(kind = 'standard' AND owner_id IS NULL AND platform_app_id IS NULL) OR "
    "(kind = 'assistant' AND owner_id IS NOT NULL AND platform_app_id IS NOT NULL "
    "AND platform = 'feishu')"
)
```

在 `Bot.__table_args__` 里 `Index("bots_created_by_idx", "created_by"),` 之后加：

```python
        CheckConstraint("kind IN ('standard','assistant')", name="kind"),
        CheckConstraint(ASSISTANT_SHAPE, name="assistant_shape"),
        Index("bots_platform_app_idx", "platform_app_id"),
        # 一人同时只有一条启用的助理记录。
        Index(
            "bots_assistant_owner_uq",
            "owner_id",
            unique=True,
            postgresql_where=text("kind = 'assistant' AND enabled"),
        ),
```

在 `created_by` 列之后加：

```python
    # standard 普通员工；assistant 个人助理（一人一条，只接所有者的私聊）。
    kind: Mapped[str] = mapped_column(Text, server_default=text("'standard'"))
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    # 助理记录共用的飞书应用；应用凭证以 platform_apps 为准，credentials_enc 是它的副本。
    platform_app_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("platform_apps.id")
    )
```

`coreman/core/db/models/__init__.py`：从 `bots` 模块的导入里加上 `BOT_KINDS`，并加进 `__all__`。

- [ ] **Step 4: 写迁移**

```python
"""Personal assistant records: bots.kind, owner_id, platform_app_id."""

import sqlalchemy as sa
from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None

SHAPE = (
    "(kind = 'standard' AND owner_id IS NULL AND platform_app_id IS NULL) OR "
    "(kind = 'assistant' AND owner_id IS NOT NULL AND platform_app_id IS NOT NULL "
    "AND platform = 'feishu')"
)


def upgrade() -> None:
    op.add_column(
        "bots",
        sa.Column("kind", sa.Text(), server_default=sa.text("'standard'"), nullable=False),
    )
    op.add_column("bots", sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id")))
    op.add_column(
        "bots", sa.Column("platform_app_id", sa.Uuid(), sa.ForeignKey("platform_apps.id"))
    )
    op.create_check_constraint("kind", "bots", "kind IN ('standard','assistant')")
    op.create_check_constraint("assistant_shape", "bots", SHAPE)
    op.create_index("bots_platform_app_idx", "bots", ["platform_app_id"])
    op.create_index(
        "bots_assistant_owner_uq",
        "bots",
        ["owner_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'assistant' AND enabled"),
    )


def downgrade() -> None:
    op.execute("DELETE FROM bots WHERE kind = 'assistant'")
    op.drop_index("bots_assistant_owner_uq", table_name="bots")
    op.drop_index("bots_platform_app_idx", table_name="bots")
    op.drop_constraint(op.f("ck_bots_assistant_shape"), "bots", type_="check")
    op.drop_constraint(op.f("ck_bots_kind"), "bots", type_="check")
    op.drop_column("bots", "platform_app_id")
    op.drop_column("bots", "owner_id")
    op.drop_column("bots", "kind")
```

- [ ] **Step 5: 运行确认通过，并确认模型与迁移一致**

Run: `uv run pytest tests/integration/test_assistant_bot_schema.py -v`
Expected: 5 passed。

Run: `uv run alembic check`
Expected: `No new upgrade operations detected.`。若报外键名不一致，把迁移里两处 `sa.ForeignKey(...)` 换成 `op.create_foreign_key(op.f("<报告里的名字>"), "bots", "<目标表>", ["<列>"], ["id"])`，再跑一次。

- [ ] **Step 6: 提交**

```bash
git add migrations/versions/0086_assistant_bots.py coreman/core/db/models tests/integration/test_assistant_bot_schema.py
git commit -m "个人助理：bots 增加类型、所有者与所属应用"
```

---

### Task 2: 类型判断、锁定字段与建记录

**Files:**
- Create: `coreman/core/assistants/__init__.py`（空文件）
- Create: `coreman/core/assistants/records.py`
- Test: `tests/unit/test_assistant_records.py`

**Interfaces:**
- Consumes: Task 1 的 `Bot.kind`、`Bot.owner_id`、`Bot.platform_app_id`。
- Produces:
  - `is_assistant(bot: Bot) -> bool`
  - `ASSISTANT_FIXED: dict[str, object]`
  - `locked_changes(bot: Bot, changes: Mapping[str, object]) -> list[str]`
  - `visible_to(bot: Bot, user: User) -> bool`
  - `new_assistant_bot(owner: User, app: PlatformApp, *, relay_server_id: uuid.UUID | None, model: str, working_dir: str, credentials_enc: str) -> Bot`

- [ ] **Step 1: 写失败测试**

```python
import uuid

from coreman.core.assistants.records import (
    ASSISTANT_FIXED,
    is_assistant,
    locked_changes,
    new_assistant_bot,
    visible_to,
)
from coreman.core.db.models import Bot, PlatformApp, User


def _user() -> User:
    return User(id=uuid.uuid4(), login_name="zhangsan", display_name="张三", role="member")


def _app() -> PlatformApp:
    return PlatformApp(
        id=uuid.uuid4(), platform="feishu", name="个人助理", capabilities=["assistant"]
    )


def _standard(creator: User) -> Bot:
    return Bot(
        id=uuid.uuid4(),
        bot_key="demo-bot",
        platform="feishu",
        name="demo-bot",
        created_by=creator.id,
        kind="standard",
        model="m",
        working_dir="/d",
        credentials_enc="e",
    )


def test_new_assistant_bot_shape() -> None:
    owner, app = _user(), _app()
    bot = new_assistant_bot(
        owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc="enc"
    )
    assert is_assistant(bot)
    assert bot.owner_id == owner.id and bot.created_by == owner.id
    assert bot.platform == "feishu" and bot.platform_app_id == app.id
    assert bot.bot_key == f"assistant-{owner.id.hex[:16]}"
    assert bot.team_id is None and bot.credentials_enc == "enc"
    for field, value in ASSISTANT_FIXED.items():
        assert getattr(bot, field) == value


def test_locked_changes_only_for_assistants() -> None:
    owner = _user()
    assistant = new_assistant_bot(
        owner, _app(), relay_server_id=None, model="m", working_dir="/w", credentials_enc=""
    )
    changes = {"name": "新名字", "group_observe": "on", "credentials": {}, "team_id": None}
    assert locked_changes(assistant, changes) == ["credentials", "group_observe", "team_id"]
    assert locked_changes(assistant, {"name": "新名字", "model": "m2"}) == []
    assert locked_changes(_standard(owner), changes) == []


def test_visible_only_to_owner() -> None:
    owner, other = _user(), _user()
    admin = User(id=uuid.uuid4(), login_name="root", display_name="管理员", role="platform_admin")
    assistant = new_assistant_bot(
        owner, _app(), relay_server_id=None, model="m", working_dir="/w", credentials_enc=""
    )
    assert visible_to(assistant, owner)
    assert not visible_to(assistant, other)
    assert not visible_to(assistant, admin)
    assert visible_to(_standard(owner), other)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_records.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'coreman.core.assistants'`。

- [ ] **Step 3: 写实现**

`coreman/core/assistants/__init__.py` 为空文件。`coreman/core/assistants/records.py`：

```python
"""个人助理记录：类型判断、固定取值、锁定字段、可见性——纯函数，路由只做编排。"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from coreman.core.db.models import Bot, PlatformApp, User

# 助理记录上这些列的取值固定，对应「只接私聊、只属于本人」。
ASSISTANT_FIXED: dict[str, object] = {
    "group_observe": "off",
    "group_role": "member",
    "accept_bot_help": False,
    "api_access": False,
    "dev_mode": False,
}
# 所有者也不能改的字段：固定取值的列，加上由平台分配的归属、目录与凭证。
LOCKED_FIELDS = frozenset(
    {*ASSISTANT_FIXED, "group_observe_scope", "team_id", "working_dir", "credentials"}
)


def is_assistant(bot: Bot) -> bool:
    return bot.kind == "assistant"


def locked_changes(bot: Bot, changes: Mapping[str, object]) -> list[str]:
    """这次修改里助理记录不允许动的字段名；普通记录恒为空。"""
    if not is_assistant(bot):
        return []
    return sorted(LOCKED_FIELDS & changes.keys())


def visible_to(bot: Bot, user: User) -> bool:
    """助理记录只对所有者可见，管理员也不例外；普通记录不在这里限制。"""
    return not is_assistant(bot) or bot.owner_id == user.id


def new_assistant_bot(
    owner: User,
    app: PlatformApp,
    *,
    relay_server_id: uuid.UUID | None,
    model: str,
    working_dir: str,
    credentials_enc: str,
) -> Bot:
    return Bot(
        bot_key=f"assistant-{owner.id.hex[:16]}",
        platform="feishu",
        name="个人助理",
        kind="assistant",
        owner_id=owner.id,
        created_by=owner.id,
        platform_app_id=app.id,
        team_id=None,
        relay_server_id=relay_server_id,
        model=model,
        working_dir=working_dir,
        credentials_enc=credentials_enc,
        **ASSISTANT_FIXED,
    )
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_records.py -v`
Expected: 3 passed。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants tests/unit/test_assistant_records.py
git commit -m "个人助理：类型判断、锁定字段与建记录"
```

---

### Task 3: 应用凭证与同步

**Files:**
- Create: `coreman/core/assistants/apps.py`
- Modify: `coreman/core/bots/platform_account.py`
- Modify: `coreman/api/routers/platform_apps.py`（`PlatformAppIn._platform_fields`、`create_app`、`update_app`、`delete_app`）
- Test: `tests/integration/test_assistant_apps.py`、`tests/api/test_platform_apps.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `new_assistant_bot`。
- Produces:
  - `SECRET_AAD = "platform_apps.secret_enc"`
  - `app_credentials(cipher: Cipher, app: PlatformApp) -> dict[str, str]`，键为 `app_id`、`app_secret`；应用不是启用的飞书助理应用时抛 `ValueError("not_assistant_app")`
  - `async sync_bot_credentials(session: AsyncSession, cipher: Cipher, app: PlatformApp) -> int`，返回改动的记录数
  - `async assistant_app_ids(session: AsyncSession) -> set[str]`，所有助理应用的飞书 `app_id`

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_apps.py`：

```python
"""助理应用的凭证读取、向助理记录同步、与普通机器人互斥。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.apps import (
    SECRET_AAD,
    app_credentials,
    assistant_app_ids,
    sync_bot_credentials,
)
from coreman.core.assistants.records import new_assistant_bot
from coreman.core.bots.platform_account import reserve_feishu_app
from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import PlatformApp, User
from coreman.core.errors import ApiError

CIPHER = Cipher(b"\x05" * 32)


async def _app(session: AsyncSession, secret: str = "s3cret") -> PlatformApp:
    app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc=CIPHER.encrypt(secret, SECRET_AAD),
    )
    session.add(app)
    await session.flush()
    return app


async def _owner(session: AsyncSession) -> User:
    owner = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name="所有者")
    session.add(owner)
    await session.flush()
    return owner


async def test_app_credentials(db_session: AsyncSession) -> None:
    app = await _app(db_session)
    assert app_credentials(CIPHER, app) == {"app_id": "cli_example", "app_secret": "s3cret"}
    app.capabilities = ["login"]
    with pytest.raises(ValueError, match="not_assistant_app"):
        app_credentials(CIPHER, app)


async def test_sync_rewrites_only_changed_rows(db_session: AsyncSession) -> None:
    app = await _app(db_session)
    bots = []
    for _ in range(2):
        bot = new_assistant_bot(
            await _owner(db_session),
            app,
            relay_server_id=None,
            model="m",
            working_dir="/w",
            credentials_enc="",
        )
        db_session.add(bot)
        bots.append(bot)
    await db_session.flush()
    assert await sync_bot_credentials(db_session, CIPHER, app) == 2
    for bot in bots:
        assert decrypt_json(CIPHER, bot.credentials_enc, CREDENTIALS_AAD) == {
            "app_id": "cli_example",
            "app_secret": "s3cret",
        }
    assert await sync_bot_credentials(db_session, CIPHER, app) == 0
    app.secret_enc = CIPHER.encrypt("rotated", SECRET_AAD)
    assert await sync_bot_credentials(db_session, CIPHER, app) == 2


async def test_standard_bot_cannot_take_assistant_app(db_session: AsyncSession) -> None:
    await _app(db_session)
    assert await assistant_app_ids(db_session) == {"cli_example"}
    with pytest.raises(ApiError):
        await reserve_feishu_app(
            db_session, CIPHER, {"app_id": "cli_example", "app_secret": "x"}
        )
    await reserve_feishu_app(db_session, CIPHER, {"app_id": "cli_other", "app_secret": "x"})
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_apps.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'coreman.core.assistants.apps'`。

- [ ] **Step 3: 写 `coreman/core/assistants/apps.py`**

```python
"""助理共用的飞书应用：凭证以 platform_apps 为准，助理记录上保存一份副本。

副本让现有按记录读取飞书凭证的代码不必区分类型；应用凭证变更时在同一事务里
把副本同步到该应用下的全部助理记录。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json, encrypt_json
from coreman.core.crypto import Cipher, DecryptError
from coreman.core.db.models import Bot, PlatformApp

SECRET_AAD = "platform_apps.secret_enc"


def app_credentials(cipher: Cipher, app: PlatformApp) -> dict[str, str]:
    if (
        app.platform != "feishu"
        or "assistant" not in app.capabilities
        or not app.enabled
        or not app.app_id
    ):
        raise ValueError("not_assistant_app")
    return {"app_id": app.app_id, "app_secret": cipher.decrypt(app.secret_enc, SECRET_AAD)}


async def sync_bot_credentials(session: AsyncSession, cipher: Cipher, app: PlatformApp) -> int:
    """把应用凭证写到该应用下每条助理记录；内容没变的行不动，免得网关白白重连。"""
    wanted = app_credentials(cipher, app)
    changed = 0
    for bot in await session.scalars(select(Bot).where(Bot.platform_app_id == app.id)):
        try:
            current = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
        except (DecryptError, ValueError):
            current = {}
        if current != wanted:
            bot.credentials_enc = encrypt_json(cipher, wanted, CREDENTIALS_AAD)
            changed += 1
    await session.flush()
    return changed


async def assistant_app_ids(session: AsyncSession) -> set[str]:
    rows = await session.scalars(
        select(PlatformApp.app_id).where(
            PlatformApp.platform == "feishu",
            PlatformApp.capabilities.contains(["assistant"]),
            PlatformApp.app_id.is_not(None),
        )
    )
    return {app_id for app_id in rows if app_id}
```

- [ ] **Step 4: 改 `reserve_feishu_app`**

`coreman/core/bots/platform_account.py`：文件头导入加

```python
from coreman.core.assistants.apps import assistant_app_ids
```

函数体里，拿到咨询锁之后、`query = select(Bot)...` 之前加：

```python
    if app_id in await assistant_app_ids(session):
        raise ApiError(409, 409, "该飞书应用是个人助理应用，不能分配给普通机器人")
```

并把查询改成只比对普通记录（助理记录共用应用，本来就会重复）：

```python
    query = select(Bot).where(Bot.platform == "feishu", Bot.kind == "standard")
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_apps.py -v`
Expected: 3 passed。

- [ ] **Step 6: 给应用接口写失败测试**

追加到 `tests/api/test_platform_apps.py` 末尾（沿用文件里已有的 `admin_client` 夹具与 `BODY` 的写法）：

```python
ASSISTANT_BODY = {
    "platform": "feishu",
    "name": "个人助理",
    "capabilities": ["assistant"],
    "app_id": "cli_example",
    "secret": "s3cret-value",
}


async def test_assistant_capability_is_feishu_only(admin_client: httpx.AsyncClient) -> None:
    bad = {**ASSISTANT_BODY, "platform": "wecom", "corp_id": "ww_example", "app_id": None}
    assert (await admin_client.post("/api/admin/platform-apps", json=bad)).status_code == 422
    ok = await admin_client.post("/api/admin/platform-apps", json=ASSISTANT_BODY)
    assert ok.status_code == 201, ok.text


async def test_update_syncs_assistant_bots_and_delete_is_blocked(
    admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    from coreman.core.assistants.records import new_assistant_bot
    from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
    from coreman.core.crypto import Cipher
    from coreman.core.db.models import Bot, PlatformApp, User
    from tests.api.conftest import MASTER_KEY

    created = (await admin_client.post("/api/admin/platform-apps", json=ASSISTANT_BODY)).json()[
        "data"
    ]
    app = await db_session.get(PlatformApp, uuid.UUID(created["id"]))
    owner = User(login_name="zhangsan", display_name="张三")
    db_session.add(owner)
    await db_session.flush()
    bot = new_assistant_bot(
        owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc=""
    )
    db_session.add(bot)
    await db_session.commit()
    r = await admin_client.put(
        f"/api/admin/platform-apps/{created['id']}",
        json={**ASSISTANT_BODY, "secret": "rotated-value"},
        headers={"If-Match": '"1"'},
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    fresh = await db_session.get(Bot, bot.id)
    cipher = Cipher(base64.b64decode(MASTER_KEY))
    assert decrypt_json(cipher, fresh.credentials_enc, CREDENTIALS_AAD) == {
        "app_id": "cli_example",
        "app_secret": "rotated-value",
    }
    assert (
        await admin_client.delete(f"/api/admin/platform-apps/{created['id']}")
    ).status_code == 409
```

文件头缺 `base64`、`uuid`、`AsyncSession` 的导入时补上。

Run: `uv run pytest tests/api/test_platform_apps.py -v -k assistant`
Expected: 2 failed（422 校验缺失；同步未发生）。

- [ ] **Step 7: 接到应用接口**

`coreman/api/routers/platform_apps.py`：文件头导入加

```python
from coreman.core.assistants.apps import sync_bot_credentials
from coreman.core.db.models import Bot
```

`PlatformAppIn._platform_fields` 的 `return self` 之前加：

```python
        if "assistant" in self.capabilities and self.platform != "feishu":
            raise ValueError("个人助理应用只支持飞书")
```

`update_app` 里 `_apply(cipher, app, body, before)` 之后加：

```python
    if "assistant" in app.capabilities and app.enabled:
        await session.flush()
        await sync_bot_credentials(session, cipher, app)
```

`delete_app` 里 `snapshot = _public(app)` 之前加：

```python
    if await session.scalar(select(Bot.id).where(Bot.platform_app_id == app.id).limit(1)):
        raise ApiError(409, 409, "还有个人助理在使用这个应用，请先停用它们")
```

`select` 未导入时从 `sqlalchemy` 导入。

- [ ] **Step 8: 运行确认通过**

Run: `uv run pytest tests/api/test_platform_apps.py tests/integration/test_assistant_apps.py -v`
Expected: 全部通过，原有用例不受影响。

- [ ] **Step 9: 提交**

```bash
git add coreman/core/assistants/apps.py coreman/core/bots/platform_account.py coreman/api/routers/platform_apps.py tests/integration/test_assistant_apps.py tests/api/test_platform_apps.py
git commit -m "个人助理：应用凭证同步到助理记录，普通机器人不能占用助理应用"
```

---

### Task 4: 在机器人接口上落实限制

**Files:**
- Modify: `coreman/api/routers/bots.py`（`list_bots`、`get_bot`、`patch_bot`）
- Modify: `coreman/api/routers/bots_extra.py`（`add_member`、`replace_allowed_users`）
- Test: `tests/api/test_assistant_bots.py`

**Interfaces:**
- Consumes: Task 2 的 `is_assistant`、`locked_changes`、`visible_to`、`new_assistant_bot`。
- Produces: 无新名称。行为：非所有者在列表与详情里看不到助理记录（详情返回 404）；所有者修改锁定字段返回 422；任何人给助理记录加成员或可用人返回 422。

已知并接受：管理员仍可通过启停、删除、切换运行时这些运维入口操作助理记录（它们不返回内容）。这些入口的可见性在 1F 一并处理。

- [ ] **Step 1: 写失败测试**

```python
"""助理记录在机器人接口上的可见性与锁定字段。"""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.records import new_assistant_bot
from coreman.core.db.models import Bot, PlatformApp, User
from tests.api.conftest import login_as, login_existing


async def _assistant(db_session: AsyncSession, owner: User) -> Bot:
    app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="x",
    )
    db_session.add(app)
    await db_session.flush()
    bot = new_assistant_bot(
        owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc=""
    )
    db_session.add(bot)
    await db_session.commit()
    return bot


async def test_only_owner_sees_assistant(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="member")
    bot = await _assistant(db_session, owner)
    mine = (await client.get("/api/admin/bots", params={"scope": "mine"})).json()["data"]
    assert [b["id"] for b in mine["items"]] == [str(bot.id)]
    assert (await client.get(f"/api/admin/bots/{bot.id}")).status_code == 200

    await login_as(client, db_session, role="platform_admin")
    everything = (await client.get("/api/admin/bots", params={"scope": "all"})).json()["data"]
    assert str(bot.id) not in [b["id"] for b in everything["items"]]
    assert (await client.get(f"/api/admin/bots/{bot.id}")).status_code == 404


async def test_owner_cannot_change_locked_fields(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="member")
    bot = await _assistant(db_session, owner)
    headers = {"If-Match": f'"{bot.version}"'}
    r = await client.patch(
        f"/api/admin/bots/{bot.id}", json={"group_observe": "on"}, headers=headers
    )
    assert r.status_code == 422 and "group_observe" in r.text
    r = await client.patch(f"/api/admin/bots/{bot.id}", json={"dev_mode": True}, headers=headers)
    assert r.status_code == 422
    r = await client.patch(
        f"/api/admin/bots/{bot.id}", json={"description": "我的助理"}, headers=headers
    )
    assert r.status_code == 200, r.text


async def test_no_members_or_allowed_users(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="member")
    bot = await _assistant(db_session, owner)
    colleague = User(login_name="lisi", display_name="李四")
    db_session.add(colleague)
    await db_session.commit()
    await login_existing(client, db_session, owner)
    r = await client.post(
        f"/api/admin/bots/{bot.id}/members", json={"user_id": str(colleague.id)}
    )
    assert r.status_code == 422
    r = await client.put(
        f"/api/admin/bots/{bot.id}/allowed-users", json={"user_ids": [str(colleague.id)]}
    )
    assert r.status_code == 422
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_assistant_bots.py -v`
Expected: 3 failed（管理员能看到；`group_observe` 被接受或走到别的校验；成员添加成功）。

- [ ] **Step 3: 改 `bots.py`**

文件头导入加：

```python
from coreman.core.assistants.records import locked_changes, visible_to
```

`list_bots`：把 `stmt = select(Bot).order_by(Bot.created_at.desc(), Bot.id)` 改为

```python
    # 个人助理只对所有者可见，任何范围、任何角色都一样。
    stmt = (
        select(Bot)
        .where(or_(Bot.kind == "standard", Bot.owner_id == user.id))
        .order_by(Bot.created_at.desc(), Bot.id)
    )
```

`get_bot`：在函数里 `load_bot(...)` 取到 `bot` 之后加（文案与 `load_bot` 找不到记录时的一致，不让人分辨出「存在但不可见」）：

```python
    if not visible_to(bot, user):
        raise ApiError(404, 404, "机器人不存在")
```

`patch_bot`：在 `if not changes: raise ApiError(422, 422, "至少修改一个字段")` 之后加：

```python
    locked = locked_changes(bot, changes)
    if locked:
        raise ApiError(422, 422, f"个人助理不支持修改：{', '.join(locked)}")
```

- [ ] **Step 4: 改 `bots_extra.py`**

文件头导入加：

```python
from coreman.core.assistants.records import is_assistant
```

`add_member` 与 `replace_allowed_users` 里，各自 `bot = await load_bot(session, bot_id)` 之后加同一段：

```python
    if is_assistant(bot):
        raise ApiError(422, 422, "个人助理只属于本人，不能添加成员或可用人")
```

- [ ] **Step 5: 运行确认通过，并跑受影响的既有用例**

Run: `uv run pytest tests/api/test_assistant_bots.py tests/api/test_bots.py tests/api/test_bots_extra.py -v`
Expected: 全部通过。

- [ ] **Step 6: 全量检查**

Run: `uv run ruff check . && uv run mypy coreman && uv run alembic check`
Expected: 三项均无报错。

Run: `uv run pytest -n auto`
Expected: 全部通过。

- [ ] **Step 7: 提交**

```bash
git add coreman/api/routers/bots.py coreman/api/routers/bots_extra.py tests/api/test_assistant_bots.py
git commit -m "个人助理：仅所有者可见，锁定字段与成员入口在服务端拒绝"
```
