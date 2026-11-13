# 个人助理 1B：网关一个应用一条连接 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 飞书网关对个人助理应用改为一个应用一条长连接：私聊与卡片回调按人投递到本人的助理记录，群消息丢弃、进群即退，同一进程发送该应用下所有助理记录的出站消息；普通机器人行为不变。

**Architecture:** 助理应用的连接租约放在新表 `app_leases`（键是 `platform_apps.id`），租约扫描规则复用 `LeaseCoordinator`（加一个「租约存储」参数，默认仍是 `bot_leases`）。supervisor 为每个助理应用起一个新的子进程入口 `app_child.run_app`；入站由 `AssistantInbound` 按发送人找助理记录后走现有的 `normalize_event` / `admit`；出站由 `AppOutbound` 每轮查出「有活要干」的助理记录，各用一个现有的 `FeishuTransport`（只把租约围栏换成按应用的）。普通机器人的子进程 `child.run_child` 不改逻辑。

**Tech Stack:** Python、SQLAlchemy async、Alembic、PostgreSQL、lark-oapi 长连接、pytest。

**Spec:** `docs/superpowers/specs/2026-11-11-personal-assistant.md`（第一节「网关改造」）；总览 `docs/superpowers/plans/2026-11-11-personal-assistant-phase1.md`

## Global Constraints

- 开工前合并最新 `main`：写本计划时迁移头是 `0086`，本计划新迁移是 `0087`；开工时迁移头变了就顺延，并同步改文件名、`revision` 与 `down_revision`。
- 公开仓：代码、测试、提交信息只用通用占位（`example.com`、`demo-bot`、`cli_example`、张三 / 李四），提交前跑仓库约定的敏感词自查。
- 普通员工（`kind='standard'`）的行为不变。每个任务的「普通机器人不受影响」一栏写明由哪条测试保证。
- 本计划不做：开通卡片（1F）、内容加密（1C）、审批（1D）、沙箱（1E）。没有助理记录的发送人本期只记日志并丢弃。
- 接口名沿用总览：`coreman.core.assistants.routing.assistant_for(session, app_id, user_id) -> Bot | None`、`coreman.core.assistants.apps.app_credentials(cipher, app)`。
- 「助理应用」的判定只有一处：`coreman.core.assistants.apps.is_assistant_app(app)`（飞书、启用、`capabilities` 含 `assistant`、有 `app_id`）。
- 验证命令：`uv run pytest <路径>`、`uv run ruff check .`、`uv run mypy coreman`、`uv run alembic check`（`TEST_DATABASE_URL` 按本机配置）。

## 设计选择

**租约：新表 `app_leases`，不改 `bot_leases` 的键。** `bot_leases.bot_id` 是指向 `bots` 的级联外键，告警（`core/observability/alerts.py`）、指标、管理台租约列表（`api/routers/runtime.py`）、滚动升级（`operations.py`）、过期回收（`scheduler/reaper.py`）都按「一行租约对应一个 bot」连表。让这一列也能放应用 id 就得去掉外键并逐处区分两种键，改的全是普通机器人在用的路径。新表的列与 `bot_leases` 相同（少一个 `platform`），认领 / 续期 / 排空 / 孤儿回收的规则不另写一份：`LeaseCoordinator` 增加 `store` 参数，按 bot 的存储是现有 `leases` 模块的薄适配，按应用的存储是新模块 `app_leases`。

**助理记录没有 `bot_leases` 行。** `leases.ensure_rows` 与 `leases.acquirable` 排除 `kind='assistant'`，所以它们不会被认领、不会各自起子进程。worker 的 `replies.lease_generation` 对没有行的 bot 返回 0，飞书出站不读这个值，不用改。

**出站：每条助理记录一个 `FeishuTransport`，共用一个客户端和一条守护连接。** `FeishuTransport` 里按 `bot_id` 的地方（出站锁 `feishu-output:{bot_id}`、`outbox.claim_next`、流卡片查询、发送存档、表情清理）全部保持原样，只加一个可选参数 `lease_fence` 替换租约围栏。`AppOutbound.round()` 先用一条查询找出该应用下此刻有活的助理记录（有未推完的流、有到点的出站、有待清理的表情），只对它们跑 `transport.round()`，空闲时每轮固定两条查询，不随助理人数增长。出站锁仍按记录（设计文档原话），拿在同一条守护连接上。

**唤醒：** `stream_updated` / `outbox_added` 的载荷仍是 `bot_id`；应用子进程每轮刷新「名下启用的助理记录」集合，通知里的 `bot_id` 在集合里才醒。

**单个 bot 的状态：**

| 现有子进程里的状态 | 应用连接模式下 |
|---|---|
| `BotInfo`（id、bot_key、欢迎语） | 每个入站事件按找到的助理记录现建一个 |
| `observe`、`dispatch_live`、`acks` | 助理记录固定关闭（1A 的 `ASSISTANT_FIXED`），恒为 False，不启动 |
| `rich_cards` | `AppOutbound` 每轮从查询结果里取，写到对应的 transport |
| 凭证变化检测（`credentials_enc`） | 改看应用：`connection_fingerprint(cipher, app)` 变了就退出，由 supervisor 重启 |
| `bot_open_id`（群里判断 @） | 不需要：群消息在归一化之前就丢弃 |
| `credentials_fingerprint`（机器人协作探测） | 助理记录不参与协作，传 `None` |

## File Structure

| 文件 | 职责 |
|---|---|
| `coreman/core/assistants/routing.py`（新） | `assistant_for`：按应用与所有者找启用的助理记录 |
| `coreman/core/assistants/apps.py` | 新增 `is_assistant_app`、`connection_fingerprint` |
| `migrations/versions/0087_app_leases.py`（新） | `app_leases` 表 |
| `coreman/core/db/models/bus.py` | `AppLease` 模型；`BotLease.key` |
| `coreman/core/bus/app_leases.py`（新） | `app_leases` 读写 |
| `coreman/core/bus/leases.py` | `ensure_rows` / `acquirable` 排除助理记录 |
| `coreman/runtime/gateway_common/lease_loop.py` | `LeaseStore` 协议、`BotLeases` 适配、`store` 参数 |
| `coreman/runtime/gateway_feishu/service.py` | 第二个租约扫描器与应用子进程的拉起 / 重启 / 停止 |
| `coreman/core/platforms/feishu.py` | `FeishuClient.leave_chat` |
| `coreman/core/i18n/messages.py` | 文案 `assistant_private_only` |
| `coreman/core/feishu_apps/manifest.py` | `ASSISTANT_SCOPES` |
| `coreman/runtime/gateway_feishu/assistant_inbound.py`（新） | 事件分流 `triage`、`AssistantInbound`（入站、退群、未开通钩子） |
| `coreman/runtime/gateway_feishu/transport.py` | `lease_fence` 参数 |
| `coreman/runtime/gateway_feishu/app_outbound.py`（新） | `AppOutbound`：应用租约围栏、按记录分发出站 |
| `coreman/runtime/gateway_feishu/child.py` | 抽出 `wait_for_notice`；助理记录不进 `run_child` |
| `coreman/runtime/gateway_feishu/app_child.py`（新） | 应用连接子进程 `run_app` / `run_process` |
| `coreman/runtime/gateway_feishu/preload.py` | 预加载 `app_child` |
| `coreman/core/observability/alerts.py` | 连接告警：助理记录不单独报，按应用报 |
| `docs/feishu.md` | 「个人助理应用」一节 |

---

### Task 1: 路由查询与应用判定

**Files:**
- Create: `coreman/core/assistants/routing.py`
- Modify: `coreman/core/assistants/apps.py`
- Test: `tests/integration/test_assistant_routing.py`

**Interfaces:**
- Consumes: 1A 的 `Bot.kind` / `owner_id` / `platform_app_id`、`new_assistant_bot`、`app_credentials`、`SECRET_AAD`。
- Produces:
  - `coreman.core.assistants.routing.assistant_for(session: AsyncSession, app_id: uuid.UUID, user_id: uuid.UUID) -> Bot | None`（`app_id` 是 `platform_apps.id`，`user_id` 是 `users.id`）
  - `coreman.core.assistants.apps.is_assistant_app(app: PlatformApp) -> bool`
  - `coreman.core.assistants.apps.connection_fingerprint(cipher: Cipher, app: PlatformApp) -> str`（按解密后的密钥算：管理台每次保存应用都会重新加密，密文变而密钥没变时指纹不变）

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_routing.py`：

```python
"""按应用与所有者找助理记录；应用是否可服务与连接指纹。"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.apps import (
    SECRET_AAD,
    app_credentials,
    connection_fingerprint,
    is_assistant_app,
)
from coreman.core.assistants.records import new_assistant_bot
from coreman.core.assistants.routing import assistant_for
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, PlatformApp, User

CIPHER = Cipher(b"\x05" * 32)


async def _app(session: AsyncSession, app_id: str = "cli_example") -> PlatformApp:
    app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id=app_id,
        secret_enc=CIPHER.encrypt("s3cret", SECRET_AAD),
    )
    session.add(app)
    await session.flush()
    await session.refresh(app)
    return app


async def _owner(session: AsyncSession, name: str) -> User:
    owner = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name=name)
    session.add(owner)
    await session.flush()
    return owner


async def _assistant(session: AsyncSession, owner: User, app: PlatformApp) -> Bot:
    bot = new_assistant_bot(
        owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc="unused"
    )
    session.add(bot)
    await session.flush()
    return bot


async def test_assistant_for_matches_app_owner_and_enabled(db_session: AsyncSession) -> None:
    app, other_app = await _app(db_session), await _app(db_session, "cli_other")
    zhang, li = await _owner(db_session, "张三"), await _owner(db_session, "李四")
    bot = await _assistant(db_session, zhang, app)
    found = await assistant_for(db_session, app.id, zhang.id)
    assert found is not None and found.id == bot.id
    assert await assistant_for(db_session, app.id, li.id) is None
    assert await assistant_for(db_session, other_app.id, zhang.id) is None
    bot.enabled = False
    await db_session.flush()
    assert await assistant_for(db_session, app.id, zhang.id) is None


async def test_standard_bot_is_never_routed(db_session: AsyncSession) -> None:
    app = await _app(db_session)
    zhang = await _owner(db_session, "张三")
    db_session.add(
        Bot(
            bot_key="demo-bot",
            platform="feishu",
            name="demo-bot",
            created_by=zhang.id,
            model="m",
            working_dir="/d",
            credentials_enc="unused",
        )
    )
    await db_session.flush()
    assert await assistant_for(db_session, app.id, zhang.id) is None


async def test_is_assistant_app_and_fingerprint(db_session: AsyncSession) -> None:
    app = await _app(db_session)
    assert is_assistant_app(app)
    assert app_credentials(CIPHER, app)["app_id"] == "cli_example"
    before = connection_fingerprint(CIPHER, app)
    app.name = "改个名字"
    app.secret_enc = CIPHER.encrypt("s3cret", SECRET_AAD)  # 保存时重新加密：密文变，密钥没变
    assert connection_fingerprint(CIPHER, app) == before
    app.secret_enc = CIPHER.encrypt("rotated", SECRET_AAD)
    assert connection_fingerprint(CIPHER, app) != before
    # 解不开的密文不抛错（supervisor 每轮都要算），退回按密文本身比较。
    app.secret_enc = "garbage-1"
    broken = connection_fingerprint(CIPHER, app)
    app.secret_enc = "garbage-2"
    assert connection_fingerprint(CIPHER, app) != broken
    for change in ({"enabled": False}, {"capabilities": ["login"]}, {"app_id": None}):
        fresh = PlatformApp(
            platform="feishu",
            name="x",
            capabilities=["assistant"],
            app_id="cli_example",
            secret_enc="x",
            enabled=True,
        )
        for field, value in change.items():
            setattr(fresh, field, value)
        assert not is_assistant_app(fresh)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_routing.py -v`
Expected: FAIL，`ImportError: cannot import name 'connection_fingerprint'`。

- [ ] **Step 3: 写实现**

`coreman/core/assistants/routing.py`：

```python
"""入站路由：一个助理应用下，按所有者找他本人那条启用的助理记录。"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Bot


async def assistant_for(
    session: AsyncSession, app_id: uuid.UUID, user_id: uuid.UUID
) -> Bot | None:
    """`app_id` 是 platform_apps.id，`user_id` 是 users.id；没有或已停用返回 None。

    一人同时只有一条启用的助理记录（`bots_assistant_owner_uq`），所以最多命中一行。
    """
    stmt = select(Bot).where(
        Bot.kind == "assistant",
        Bot.platform_app_id == app_id,
        Bot.owner_id == user_id,
        Bot.enabled.is_(True),
    )
    bot: Bot | None = await session.scalar(stmt)
    return bot
```

`coreman/core/assistants/apps.py`：文件头加 `import hashlib`；把 `app_credentials` 换成下面三个函数（`app_credentials` 的行为不变，只是把判定抽出来）：

```python
def is_assistant_app(app: PlatformApp) -> bool:
    """网关要为它维持长连接的应用：飞书、启用、带 assistant 能力、填了 app_id。"""
    return bool(
        app.platform == "feishu"
        and app.enabled
        and "assistant" in app.capabilities
        and app.app_id
    )


def connection_fingerprint(cipher: Cipher, app: PlatformApp) -> str:
    """长连接用到的凭证的指纹：变了就要重连。

    按解密后的密钥算：管理台每次保存应用都会重新加密（密文变、密钥不变），不能因此重连。
    密文解不开时退回按密文本身比较，不抛错——supervisor 每轮都要算它。
    """
    try:
        secret = cipher.decrypt(app.secret_enc, SECRET_AAD)
    except DecryptError:
        secret = app.secret_enc
    return hashlib.sha256(f"{app.app_id}\n{secret}".encode()).hexdigest()


def app_credentials(cipher: Cipher, app: PlatformApp) -> dict[str, str]:
    if not is_assistant_app(app) or not app.app_id:
        raise ValueError("not_assistant_app")
    return {"app_id": app.app_id, "app_secret": cipher.decrypt(app.secret_enc, SECRET_AAD)}
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_routing.py tests/integration/test_assistant_apps.py -v`
Expected: 全部 passed（后一个文件是 1A 的用例，保证 `app_credentials` 行为没变）。

普通机器人不受影响：`test_standard_bot_is_never_routed`。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants tests/integration/test_assistant_routing.py
git commit -m "个人助理：按应用与所有者找助理记录，应用可服务判定与连接指纹"
```

---

### Task 2: 应用租约表

**Files:**
- Create: `migrations/versions/0087_app_leases.py`
- Create: `coreman/core/bus/app_leases.py`
- Modify: `coreman/core/db/models/bus.py`（`BotLease` 加 `key`；其后加 `AppLease`）
- Modify: `coreman/core/db/models/__init__.py`（导出 `AppLease`）
- Modify: `tests/conftest.py`（`BUSINESS_TABLES` 里 `"bot_leases"` 下一行加 `"app_leases"`）
- Test: `tests/integration/test_bus_app_leases.py`

**Interfaces:**
- Consumes: Task 1 的 `is_assistant_app` 的判定条件（这里写成 SQL）。
- Produces（`coreman.core.bus.app_leases`，`key` 都是 `platform_apps.id`）:
  - `ensure_rows(session) -> None`
  - `acquirable(session) -> list[uuid.UUID]`
  - `acquire(session, key, instance_id) -> int | None`（成功返回新代次）
  - `heartbeat(session, instance_id) -> int`
  - `release(session, key, instance_id, generation, state="disconnected") -> None`
  - `set_state(session, key, state, *, instance_id) -> None`
  - `held_by(session, instance_id) -> list[AppLease]`
  - `disabled_among(session, keys) -> set[uuid.UUID]`
  - 模型 `AppLease`（`app_id`、`holder_instance`、`generation`、`acquired_at`、`heartbeat_at`、`drain_requested_by`、`drain_requested_at`、`released_at`、`connection_state`、只读属性 `key`）；`BotLease.key`。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_bus_app_leases.py`：

```python
"""app_leases：助理应用的连接租约，规则与 bot_leases 同源。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import app_leases, instances
from coreman.core.db.models import AppLease, PlatformApp


async def _apps(session: AsyncSession) -> tuple[PlatformApp, PlatformApp, PlatformApp]:
    on = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="x",
    )
    off = PlatformApp(
        platform="feishu",
        name="停用的",
        capabilities=["assistant"],
        app_id="cli_off",
        secret_enc="x",
        enabled=False,
    )
    login = PlatformApp(
        platform="feishu", name="登录", capabilities=["login"], app_id="cli_login", secret_enc="x"
    )
    session.add_all([on, off, login])
    for name in ("gw-a", "gw-b"):
        await instances.register(
            session, instance_id=name, service="gateway-feishu", version="dev", capacity=None
        )
    await session.commit()
    return on, off, login


async def test_only_assistant_apps_get_rows_and_only_enabled_are_acquirable(
    db_session: AsyncSession,
) -> None:
    on, off, login = await _apps(db_session)
    await app_leases.ensure_rows(db_session)
    await app_leases.ensure_rows(db_session)  # 幂等
    await db_session.commit()
    assert await db_session.get(AppLease, login.id) is None
    assert await db_session.get(AppLease, off.id) is not None
    assert await app_leases.acquirable(db_session) == [on.id]


async def test_acquire_heartbeat_release_and_takeover(db_session: AsyncSession) -> None:
    on, _, _ = await _apps(db_session)
    await app_leases.ensure_rows(db_session)
    assert await app_leases.acquire(db_session, on.id, "gw-a") == 1
    await db_session.commit()
    assert await app_leases.acquire(db_session, on.id, "gw-b") is None
    assert await app_leases.acquirable(db_session) == []
    assert await app_leases.heartbeat(db_session, "gw-a") == 1
    await app_leases.set_state(db_session, on.id, "subscribed", instance_id="gw-a")
    await app_leases.set_state(db_session, on.id, "kicked", instance_id="gw-b")  # 非持有者无效
    await db_session.commit()
    held = await app_leases.held_by(db_session, "gw-a")
    assert [row.key for row in held] == [on.id] and held[0].connection_state == "subscribed"
    # 心跳过期后别的实例可以接管，代次 +1。
    held[0].heartbeat_at = datetime.now(UTC) - timedelta(seconds=31)
    await db_session.commit()
    assert await app_leases.acquire(db_session, on.id, "gw-b") == 2
    await db_session.commit()
    # 上一代迟到的释放不生效；按当前代次释放才生效。
    await app_leases.release(db_session, on.id, "gw-b", 1)
    await db_session.commit()
    assert [row.key for row in await app_leases.held_by(db_session, "gw-b")] == [on.id]
    await app_leases.release(db_session, on.id, "gw-b", 2)
    await db_session.commit()
    row = await db_session.get(AppLease, on.id, populate_existing=True)
    assert row is not None and row.holder_instance is None
    assert row.connection_state == "disconnected"


async def test_disabled_among_reports_apps_that_stopped_being_serviceable(
    db_session: AsyncSession,
) -> None:
    on, off, login = await _apps(db_session)
    assert await app_leases.disabled_among(db_session, []) == set()
    assert await app_leases.disabled_among(db_session, [on.id, off.id, login.id]) == {
        off.id,
        login.id,
    }


async def test_rows_go_away_with_the_app(db_session: AsyncSession) -> None:
    on, _, _ = await _apps(db_session)
    await app_leases.ensure_rows(db_session)
    await db_session.commit()
    await db_session.delete(on)
    await db_session.commit()
    assert await db_session.get(AppLease, on.id) is None
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_bus_app_leases.py -v`
Expected: FAIL，`ImportError: cannot import name 'app_leases'`。

- [ ] **Step 3: 改模型**

`coreman/core/db/models/bus.py`：在 `BotLease` 类体末尾（`connection_state` 列之后）加：

```python
    @property
    def key(self) -> uuid.UUID:
        """租约扫描器用的通用键（见 gateway_common.lease_loop）。"""
        return self.bot_id
```

紧接 `BotLease` 之后加：

```python
class AppLease(Base):
    """个人助理应用的连接租约：一个应用一条长连接，同时只被一个飞书网关实例持有。"""

    __tablename__ = "app_leases"
    __table_args__ = (
        CheckConstraint(enum_check("connection_state", CONNECTION_STATES), name="connection_state"),
    )
    app_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("platform_apps.id", ondelete="CASCADE"), primary_key=True
    )
    holder_instance: Mapped[str | None] = mapped_column(
        Text, ForeignKey("process_instances.id", ondelete="SET NULL")
    )
    generation: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    acquired_at: Mapped[datetime | None] = _ts()
    heartbeat_at: Mapped[datetime | None] = _ts()
    drain_requested_by: Mapped[str | None] = mapped_column(Text)
    drain_requested_at: Mapped[datetime | None] = _ts()
    released_at: Mapped[datetime | None] = _ts()
    connection_state: Mapped[str] = mapped_column(Text, server_default=text("'disconnected'"))

    @property
    def key(self) -> uuid.UUID:
        return self.app_id
```

`coreman/core/db/models/__init__.py`：从 `bus` 模块的导入里加上 `AppLease`，并加进 `__all__`。

`tests/conftest.py`：`BUSINESS_TABLES` 里 `"bot_leases",` 下一行加 `"app_leases",`。

- [ ] **Step 4: 写迁移**

`migrations/versions/0087_app_leases.py`：

```python
"""Connection leases for personal assistant apps: one long connection per app."""

import sqlalchemy as sa
from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None

STATES = "connection_state IN ('disconnected', 'connecting', 'subscribed', 'kicked', 'auth_failed')"


def upgrade() -> None:
    op.create_table(
        "app_leases",
        sa.Column(
            "app_id",
            sa.Uuid(),
            sa.ForeignKey("platform_apps.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "holder_instance",
            sa.Text(),
            sa.ForeignKey("process_instances.id", ondelete="SET NULL"),
        ),
        sa.Column("generation", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("drain_requested_by", sa.Text()),
        sa.Column("drain_requested_at", sa.DateTime(timezone=True)),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column(
            "connection_state",
            sa.Text(),
            server_default=sa.text("'disconnected'"),
            nullable=False,
        ),
        sa.CheckConstraint(STATES, name="connection_state"),
    )


def downgrade() -> None:
    op.drop_table("app_leases")
```

- [ ] **Step 5: 写读写模块**

`coreman/core/bus/app_leases.py`：

```python
"""app_leases 读写：个人助理应用的连接租约。

规则与 `leases`（按 bot）同源：无持有者或持有者心跳过期才能认领，代次每次认领 +1，
只有当前持有者按自己的代次才能释放。函数签名就是 `gateway_common.lease_loop.LeaseStore`，
整个模块直接作为租约扫描器的存储传进去。`key` 一律是 platform_apps.id。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, and_, exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus.leases import STALE_AFTER_SECONDS
from coreman.core.db.models import AppLease, PlatformApp
from coreman.core.observability.metrics import TAKEOVERS, after_commit

_ACQUIRE_SQL = text(
    """
UPDATE app_leases SET holder_instance=:me, generation=generation+1,
                      acquired_at=now(), heartbeat_at=now(),
                      drain_requested_by=NULL, drain_requested_at=NULL, released_at=NULL,
                      connection_state='connecting'
WHERE app_id=:app_id
  AND (holder_instance IS NULL OR heartbeat_at < now() - make_interval(secs => :stale))
RETURNING generation
"""
)


def _assistant_app() -> ColumnElement[bool]:
    return and_(
        PlatformApp.platform == "feishu", PlatformApp.capabilities.contains(["assistant"])
    )


def _serviceable() -> ColumnElement[bool]:
    """与 `core.assistants.apps.is_assistant_app` 同义的 SQL 条件。"""
    return and_(
        _assistant_app(), PlatformApp.enabled.is_(True), PlatformApp.app_id.is_not(None)
    )


async def ensure_rows(session: AsyncSession) -> None:
    """为每个助理应用补一行租约（含停用的：停用只由 acquirable 过滤，行本身保留）。"""
    missing = select(PlatformApp.id).where(
        _assistant_app(),
        ~exists(select(AppLease.app_id).where(AppLease.app_id == PlatformApp.id)),
    )
    stmt = insert(AppLease).from_select(["app_id"], missing)
    await session.execute(stmt.on_conflict_do_nothing(index_elements=[AppLease.app_id]))


async def acquirable(session: AsyncSession) -> list[uuid.UUID]:
    stale = func.now() - text(f"interval '{STALE_AFTER_SECONDS} seconds'")
    stmt = (
        select(AppLease.app_id)
        .join(PlatformApp, PlatformApp.id == AppLease.app_id)
        .where(
            _serviceable(),
            (AppLease.holder_instance.is_(None)) | (AppLease.heartbeat_at < stale),
        )
        .order_by(AppLease.app_id)
    )
    return list((await session.execute(stmt)).scalars())


async def acquire(session: AsyncSession, key: uuid.UUID, instance_id: str) -> int | None:
    """抢占式认领；成功返回新的代次，已有活着的持有者返回 None。"""
    row = (
        await session.execute(
            _ACQUIRE_SQL, {"me": instance_id, "app_id": key, "stale": float(STALE_AFTER_SECONDS)}
        )
    ).first()
    if row is None:
        return None
    after_commit(session, lambda: TAKEOVERS.labels("feishu").inc())
    return int(row[0])


async def heartbeat(session: AsyncSession, instance_id: str) -> int:
    rows = (
        await session.execute(
            update(AppLease)
            .where(AppLease.holder_instance == instance_id)
            .values(heartbeat_at=func.now())
            .returning(AppLease.app_id)
        )
    ).all()
    return len(rows)


async def release(
    session: AsyncSession,
    key: uuid.UUID,
    instance_id: str,
    generation: int,
    state: str = "disconnected",
) -> None:
    """只有当前持有者按自己的代次才能释放；顺手清掉排空标记（理由同 `leases.release`）。"""
    await session.execute(
        update(AppLease)
        .where(
            AppLease.app_id == key,
            AppLease.holder_instance == instance_id,
            AppLease.generation == generation,
        )
        .values(
            holder_instance=None,
            released_at=func.now(),
            connection_state=state,
            drain_requested_by=None,
            drain_requested_at=None,
        )
    )


async def set_state(
    session: AsyncSession, key: uuid.UUID, state: str, *, instance_id: str
) -> None:
    """只有当前持有者能写连接状态。"""
    await session.execute(
        update(AppLease)
        .where(AppLease.app_id == key, AppLease.holder_instance == instance_id)
        .values(connection_state=state)
    )


async def held_by(session: AsyncSession, instance_id: str) -> list[AppLease]:
    stmt = (
        select(AppLease).where(AppLease.holder_instance == instance_id).order_by(AppLease.app_id)
    )
    return list(
        (await session.execute(stmt, execution_options={"populate_existing": True})).scalars()
    )


async def disabled_among(session: AsyncSession, keys: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """这些应用里已经不该再连的：被停用、去掉了 assistant 能力或清空了 app_id。"""
    if not keys:
        return set()
    stmt = select(PlatformApp.id).where(PlatformApp.id.in_(list(keys)), ~_serviceable())
    return set((await session.execute(stmt)).scalars())
```

- [ ] **Step 6: 运行确认通过，并确认模型与迁移一致**

Run: `uv run pytest tests/integration/test_bus_app_leases.py tests/integration/test_bus_leases.py -v`
Expected: 全部 passed。

Run: `uv run alembic check`
Expected: `No new upgrade operations detected.`。若报外键或约束名不一致，按报告里的名字把迁移里的 `sa.ForeignKey(...)` 补上 `name=`，再跑一次。

Run: `uv run pytest tests/unit/test_migration_rules.py -v`
Expected: passed（只建新表，不触发双版本并存门禁）。

普通机器人不受影响：`tests/integration/test_bus_leases.py`（`bot_leases` 的读写没有改动；`BotLease.key` 只是只读属性）。

- [ ] **Step 7: 提交**

```bash
git add migrations/versions/0087_app_leases.py coreman/core/bus/app_leases.py coreman/core/db/models tests/conftest.py tests/integration/test_bus_app_leases.py
git commit -m "个人助理：助理应用的连接租约表 app_leases"
```

---

### Task 3: 租约扫描器接受不同的租约存储；助理记录不进 bot 租约

**Files:**
- Modify: `coreman/runtime/gateway_common/lease_loop.py`
- Modify: `coreman/core/bus/leases.py`（`ensure_rows`、`acquirable`）
- Test: `tests/integration/test_lease_stores.py`

**Interfaces:**
- Consumes: Task 2 的 `coreman.core.bus.app_leases`、`AppLease.key`、`BotLease.key`。
- Produces:
  - `coreman.runtime.gateway_common.lease_loop.LeaseStore`（协议：`heartbeat` / `ensure_rows` / `acquirable` / `acquire` / `release` / `held_by` / `disabled_among`，签名同 `app_leases`）
  - `coreman.runtime.gateway_common.lease_loop.BotLeases(platform: str)`（`bot_leases` 的适配）
  - `LeaseCoordinator(..., store: LeaseStore | None = None)`；不传时等同原行为
  - `leases.ensure_rows` / `leases.acquirable` 不再包含 `kind='assistant'` 的记录

- [ ] **Step 1: 写失败测试**

`tests/integration/test_lease_stores.py`：

```python
"""同一套租约扫描规则跑在两种存储上；助理记录不占 bot 租约。"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants.records import new_assistant_bot
from coreman.core.bus import app_leases, instances, leases
from coreman.core.db.models import AppLease, Bot, BotLease, PlatformApp, User
from coreman.core.db.session import make_session_factory
from coreman.runtime.gateway_common.lease_loop import LeaseCoordinator


class Hooks:
    def __init__(self) -> None:
        self.connected: dict[uuid.UUID, int] = {}
        self.maintained: list[uuid.UUID] = []

    def lease_connected(self) -> Collection[uuid.UUID]:
        return list(self.connected)

    async def lease_connect(self, bot_id: uuid.UUID, generation: int) -> bool:
        self.connected[bot_id] = generation
        return True

    async def lease_orphaned(self, bot_id: uuid.UUID) -> None:
        self.connected.pop(bot_id, None)

    async def lease_drain(self, bot_id: uuid.UUID) -> None:
        generation = self.connected.pop(bot_id)
        await self.coordinator.release(bot_id, generation)

    async def lease_maintain(self, held: list[Any]) -> None:
        self.maintained = [lease.key for lease in held]

    coordinator: LeaseCoordinator


async def _seed(session: AsyncSession) -> tuple[Bot, Bot, PlatformApp]:
    owner = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name="张三")
    app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="x",
    )
    session.add_all([owner, app])
    await session.flush()
    await session.refresh(app)
    standard = Bot(
        bot_key="demo-bot",
        platform="feishu",
        name="demo-bot",
        created_by=owner.id,
        model="m",
        working_dir="/d",
        credentials_enc="unused",
    )
    assistant = new_assistant_bot(
        owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc="unused"
    )
    session.add_all([standard, assistant])
    await instances.register(
        session, instance_id="gw-a", service="gateway-feishu", version="dev", capacity=None
    )
    await session.commit()
    return standard, assistant, app


async def test_assistant_records_never_get_a_bot_lease(db_session: AsyncSession) -> None:
    standard, assistant, _ = await _seed(db_session)
    await leases.ensure_rows(db_session, "feishu")
    await db_session.commit()
    assert await db_session.get(BotLease, assistant.id) is None
    assert await leases.acquirable(db_session, "feishu") == [standard.id]


async def test_default_store_serves_bots_and_app_store_serves_apps(
    db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    standard, assistant, app = await _seed(db_session)
    factory = make_session_factory(db_engine)
    bot_hooks, app_hooks = Hooks(), Hooks()
    bots = LeaseCoordinator(
        platform="feishu", instance_id=lambda: "gw-a", factory=factory, hooks=bot_hooks
    )
    apps = LeaseCoordinator(
        platform="feishu",
        instance_id=lambda: "gw-a",
        factory=factory,
        hooks=app_hooks,
        store=app_leases,
    )
    bot_hooks.coordinator, app_hooks.coordinator = bots, apps
    await bots.round()
    await apps.round()
    assert bot_hooks.connected == {standard.id: 1} and bot_hooks.maintained == [standard.id]
    assert app_hooks.connected == {app.id: 1} and app_hooks.maintained == [app.id]
    # 应用停用：下一轮排空并按代次释放；bot 那一边不受影响。
    app.enabled = False
    await db_session.commit()
    await apps.round()
    await bots.round()
    assert app_hooks.connected == {} and bot_hooks.connected == {standard.id: 1}
    row = await db_session.get(AppLease, app.id, populate_existing=True)
    assert row is not None and row.holder_instance is None
    lease = await db_session.get(BotLease, standard.id, populate_existing=True)
    assert lease is not None and lease.holder_instance == "gw-a"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_lease_stores.py -v`
Expected: 第一条 FAIL（助理记录拿到了 `bot_leases` 行）；第二条 FAIL，`TypeError: LeaseCoordinator.__init__() got an unexpected keyword argument 'store'`。

- [ ] **Step 3: bot 租约排除助理记录**

`coreman/core/bus/leases.py`，`ensure_rows` 的 `missing` 查询加一个条件（文档串同步改）：

```python
async def ensure_rows(session: AsyncSession, platform: str) -> None:
    """为该平台的每个 bot 补一行租约（含停用的 bot：停用只由 acquirable 过滤，行本身保留）。

    个人助理记录除外：它们的连接按应用认领（见 `app_leases`），不各自占一行。
    """
    missing = select(Bot.id, literal(platform)).where(
        Bot.platform == platform,
        Bot.kind == "standard",
        ~exists(select(BotLease.bot_id).where(BotLease.bot_id == Bot.id)),
    )
    stmt = insert(BotLease).from_select(["bot_id", "platform"], missing)
    await session.execute(stmt.on_conflict_do_nothing(index_elements=[BotLease.bot_id]))
```

`acquirable` 的 `.where(` 里在 `Bot.enabled.is_(True),` 下一行加：

```python
            Bot.kind == "standard",
```

- [ ] **Step 4: 扫描器加存储参数**

`coreman/runtime/gateway_common/lease_loop.py`：模块文档串末尾加一段：

```
租约放在哪张表由 `store` 决定：默认是按 bot 的 `bot_leases`（`BotLeases`），飞书网关另有一个
扫描器用 `core.bus.app_leases`（个人助理应用，一个应用一条连接）。钩子里的 `bot_id` 在后一种
情况下是 platform_apps.id。
```

导入改为：

```python
import time
import uuid
from collections.abc import Callable, Collection, Sequence
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.bus import leases
from coreman.core.logging import get_logger
```

（`BotLease` 的导入去掉。）`LeaseHooks.lease_maintain` 的签名改为 `async def lease_maintain(self, held: list[Any]) -> None:`，其余钩子不动。在 `LeaseHooks` 之后、`LeaseCoordinator` 之前加：

```python
class LeaseStore(Protocol):
    """租约表的读写。`key` 是这张表的主键：bot_leases 是 bot id，app_leases 是应用 id。

    `held_by` 返回的行都带 `key`、`generation`、`drain_requested_by`。
    """

    async def heartbeat(self, session: AsyncSession, instance_id: str) -> int: ...

    async def ensure_rows(self, session: AsyncSession) -> None: ...

    async def acquirable(self, session: AsyncSession) -> list[uuid.UUID]: ...

    async def acquire(
        self, session: AsyncSession, key: uuid.UUID, instance_id: str
    ) -> int | None: ...

    async def release(
        self, session: AsyncSession, key: uuid.UUID, instance_id: str, generation: int, state: str
    ) -> None: ...

    async def held_by(self, session: AsyncSession, instance_id: str) -> list[Any]: ...

    async def disabled_among(
        self, session: AsyncSession, keys: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]: ...


class BotLeases:
    """按 bot 的租约（`bot_leases`）：把 `core.bus.leases` 包成 `LeaseStore`。"""

    def __init__(self, platform: str) -> None:
        self.platform = platform

    async def heartbeat(self, session: AsyncSession, instance_id: str) -> int:
        return await leases.heartbeat(session, instance_id)

    async def ensure_rows(self, session: AsyncSession) -> None:
        await leases.ensure_rows(session, self.platform)

    async def acquirable(self, session: AsyncSession) -> list[uuid.UUID]:
        return await leases.acquirable(session, self.platform)

    async def acquire(
        self, session: AsyncSession, key: uuid.UUID, instance_id: str
    ) -> int | None:
        lease = await leases.acquire(
            session, bot_id=key, platform=self.platform, instance_id=instance_id
        )
        return None if lease is None else int(lease.generation)

    async def release(
        self, session: AsyncSession, key: uuid.UUID, instance_id: str, generation: int, state: str
    ) -> None:
        await leases.release(
            session, bot_id=key, instance_id=instance_id, generation=generation, state=state
        )

    async def held_by(self, session: AsyncSession, instance_id: str) -> list[Any]:
        return list(await leases.held_by(session, instance_id))

    async def disabled_among(
        self, session: AsyncSession, keys: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        return await leases.disabled_among(session, keys)
```

`LeaseCoordinator.__init__` 加参数并保存（其余不变）：

```python
    def __init__(
        self,
        *,
        platform: str,
        instance_id: Callable[[], str],
        factory: async_sessionmaker[AsyncSession],
        hooks: LeaseHooks,
        heartbeat: bool = True,
        store: LeaseStore | None = None,
    ) -> None:
        self.platform = platform
        # 每次现取：进程的实例 id 允许在构造之后才定下来（运维脚本、演练用例会改写它）。
        self._instance_id = instance_id
        self.factory = factory
        self.hooks = hooks
        self.heartbeat = heartbeat
        self.store: LeaseStore = store or BotLeases(platform)
        self.draining = False
        self.busy: set[uuid.UUID] = set()
        self._cooldown: dict[uuid.UUID, float] = {}
        self._log = get_logger(__name__).bind(platform=platform)
```

`round`、`release`、`_try_acquire` 整体替换为（规则一条没变，只是 `leases.` 换成 `self.store.`、`lease.bot_id` 换成 `lease.key`；原有注释照留）：

```python
    async def round(self) -> None:
        async with self.factory() as session:
            # 先续心跳再干活：认领、排空、停孤儿都可能慢，心跳压在后面就会在库抖动的时候
            # 让别的实例觉得本实例死了，把还在正常服务的 bot 抢走。
            if self.heartbeat:
                await self.store.heartbeat(session, self.instance_id)
            await self.store.ensure_rows(session)
            candidates = [] if self.draining else await self.store.acquirable(session)
            await session.commit()
        for bot_id in self._cooled_down(candidates):
            await self._try_acquire(bot_id)
        async with self.factory() as session:
            held = await self.store.held_by(session, self.instance_id)
            # 兜底复核 `enabled`：`config_changed` 会在 LISTEN 重连的窗口里丢，只靠通知的话
            # 停用的机器人会一直挂着连接到进程重启（每个监听者都必须有兜底轮询）。
            disabled = await self.store.disabled_among(session, [x.key for x in held])
            await session.commit()
        held_ids = {lease.key for lease in held}
        for bot_id in [b for b in self.hooks.lease_connected() if b not in held_ids]:
            await self.hooks.lease_orphaned(bot_id)
        serving: list[Any] = []
        for lease in held:
            if lease.key in self.busy:
                continue  # 正在排空：它的租约由 lease_drain 自己按代次交还
            connected = lease.key in self.hooks.lease_connected()
            if connected and lease.key in disabled:
                self._log.info("bot_disabled_by_poll", bot_id=str(lease.key))
                await self.drain(lease.key)
            elif connected and lease.drain_requested_by:
                self._log.info("bot_drain_requested", by=lease.drain_requested_by)
                await self.drain(lease.key)
            elif not connected:
                # 持有租约却没有连接（认领后启动失败、熔断释放的竞态）：交回去，别占着。
                await self.release(lease.key, int(lease.generation))
            else:
                serving.append(lease)
        if serving:
            await self.hooks.lease_maintain(serving)
```

```python
    async def release(
        self, bot_id: uuid.UUID, generation: int, *, state: str = "disconnected"
    ) -> None:
        async with self.factory() as session:
            await self.store.release(session, bot_id, self.instance_id, generation, state)
            await session.commit()
```

```python
    async def _try_acquire(self, bot_id: uuid.UUID) -> None:
        async with self.factory() as session:
            generation = await self.store.acquire(session, bot_id, self.instance_id)
            await session.commit()
        if generation is None:
            return
        try:
            connected = await self.hooks.lease_connect(bot_id, generation)
        except Exception:  # noqa: BLE001 一个 bot 起不来不能拖垮整轮扫描
            self._log.exception("lease_connect_failed", bot_id=str(bot_id))
            connected = False
        if not connected:
            await self.release(bot_id, generation)
```

`drain`、`cool_down`、`_cooled_down` 不动。

`coreman/runtime/gateway_wecom/service.py` 与 `coreman/runtime/gateway_feishu/service.py` 里 `lease_maintain(self, held: list[BotLease])` 的签名保持不变（`list[BotLease]` 与协议里的 `list[Any]` 兼容），不需要改。

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/integration/test_lease_stores.py tests/integration/test_bus_leases.py tests/integration/test_feishu_supervisor.py tests/integration/test_gateway_wecom.py tests/unit/test_gateway_drain.py tests/integration/test_gateway_drain_budget.py -v`
Expected: 全部 passed。

Run: `uv run mypy coreman`
Expected: 无错误（`app_leases` 模块作为 `LeaseStore` 传入，mypy 支持把模块当协议实现）。

普通机器人不受影响：`test_default_store_serves_bots_and_app_store_serves_apps` 里 bot 一侧的断言，加上现有的 `test_feishu_supervisor.py`、`test_gateway_wecom.py`、`test_bus_leases.py`（两个网关都走默认存储）。

- [ ] **Step 6: 提交**

```bash
git add coreman/runtime/gateway_common/lease_loop.py coreman/core/bus/leases.py tests/integration/test_lease_stores.py
git commit -m "个人助理：租约扫描器支持按应用的租约，助理记录不占 bot 租约"
```

---

### Task 4: supervisor 为每个助理应用起一个子进程

**Files:**
- Modify: `coreman/runtime/gateway_feishu/service.py`
- Test: `tests/integration/test_feishu_app_supervisor.py`

**Interfaces:**
- Consumes: `app_leases`（Task 2）、`LeaseCoordinator(store=...)`（Task 3）、`is_assistant_app` / `connection_fingerprint`（Task 1）。
- Produces:
  - `service.run_app_process(app_id: uuid.UUID, instance_id: str, generation: int) -> None`（子进程入口；内部延迟导入 `coreman.runtime.gateway_feishu.app_child.run_process`，该模块由 Task 9 创建）
  - `Child.kind: str`（`"bot"` / `"app"`）；`GatewayFeishuService.children` 同时放两种，键分别是 bot id 与 `platform_apps.id`
  - `GatewayFeishuService.app_lease_loop: LeaseCoordinator`

本任务结束时应用子进程的入口模块还不存在：真实环境里它会启动失败并按 5→60 秒退避重试，等 Task 9 合入后才连得上。测试里 `spawn` 是打桩的，不受影响。各任务按顺序在同一分支上完成后再发布。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_feishu_app_supervisor.py`：

```python
"""飞书 supervisor：助理应用一个应用一个子进程，助理记录自己不起子进程。"""

from __future__ import annotations

import asyncio
import uuid

from coreman.core.assistants.records import new_assistant_bot
from coreman.core.db.models import AppLease, Bot, BotLease, PlatformApp, User
from coreman.runtime.gateway_feishu.service import GatewayFeishuService
from tests.integration.test_feishu_supervisor import FakeProcess


async def _seed(session):
    owner = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name="张三")
    app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="first",
    )
    session.add_all([owner, app])
    await session.flush()
    await session.refresh(app)
    standard = Bot(
        bot_key="demo-bot",
        platform="feishu",
        name="demo-bot",
        created_by=owner.id,
        model="m",
        working_dir="/d",
        credentials_enc="unused",
    )
    session.add(standard)
    await session.commit()
    return owner, app, standard


async def _started(service, *keys):
    for _ in range(200):
        if all(k in service.children and service.children[k].process for k in keys):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("children did not start")


async def test_app_gets_one_child_even_without_assistants_and_assistants_get_none(
    db_session, runtime_settings, monkeypatch
):
    owner, app, standard = await _seed(db_session)
    service = GatewayFeishuService(port=0, interval=30)
    spawned: list[tuple[uuid.UUID, str, FakeProcess]] = []

    async def spawn(key, child):
        process = FakeProcess()
        spawned.append((key, child.kind, process))
        return process

    monkeypatch.setattr(service, "spawn", spawn)
    await service.on_start()
    try:
        # 应用下一条助理记录都没有，也要连上：第一个人私聊时才收得到事件。
        await _started(service, app.id, standard.id)
        assert {(key, kind) for key, kind, _ in spawned} == {
            (app.id, "app"),
            (standard.id, "bot"),
        }
        async with service.factory() as session:
            lease = await session.get(AppLease, app.id)
            assert lease.holder_instance == service.instance_id
        # 之后有了助理记录：不为它单独起子进程，也没有 bot 租约。
        assistant = new_assistant_bot(
            owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc="x"
        )
        db_session.add(assistant)
        await db_session.commit()
        await service.reconcile()
        assert len(spawned) == 2 and assistant.id not in service.children
        async with service.factory() as session:
            assert await session.get(BotLease, assistant.id) is None
        # 密钥变了：停掉旧进程，起一个新的。
        app.secret_enc = "rotated"
        await db_session.commit()
        await service.reconcile()
        app_processes = [p for key, _, p in spawned if key == app.id]
        assert len(app_processes) == 2 and app_processes[0].terminated
        # 应用停用：停掉子进程并释放租约；普通机器人的子进程还在。
        app.enabled = False
        await db_session.commit()
        await service.reconcile()
        assert app.id not in service.children and app_processes[1].terminated
        assert standard.id in service.children
        async with service.factory() as session:
            lease = await session.get(AppLease, app.id)
            assert lease.holder_instance is None
    finally:
        await service.on_shutdown()


async def test_crashed_app_child_restarts_with_backoff_and_shutdown_releases(
    db_session, runtime_settings, monkeypatch
):
    _, app, standard = await _seed(db_session)
    service = GatewayFeishuService(port=0, interval=30)
    processes: dict[uuid.UUID, list[FakeProcess]] = {app.id: [], standard.id: []}

    async def spawn(key, child):
        process = FakeProcess()
        processes[key].append(process)
        return process

    monkeypatch.setattr(service, "spawn", spawn)
    await service.on_start()
    try:
        await _started(service, app.id, standard.id)
        child = service.children[app.id]
        processes[app.id][0].returncode = 1
        await service.reconcile()
        assert child.process is None and len(processes[app.id]) == 1
        async with service.factory() as session:
            assert (await session.get(AppLease, app.id)).connection_state == "disconnected"
        child.next_start = 0
        await service.reconcile()
        assert len(processes[app.id]) == 2
    finally:
        await service.on_shutdown()
    assert processes[app.id][-1].terminated and processes[standard.id][-1].terminated
    lease = await db_session.get(AppLease, app.id, populate_existing=True)
    assert lease.holder_instance is None


async def test_deleted_app_stops_its_child(db_session, runtime_settings, monkeypatch):
    _, app, standard = await _seed(db_session)
    service = GatewayFeishuService(port=0, interval=30)
    processes: dict[uuid.UUID, FakeProcess] = {}

    async def spawn(key, child):
        processes[key] = FakeProcess()
        return processes[key]

    monkeypatch.setattr(service, "spawn", spawn)
    await service.on_start()
    try:
        await _started(service, app.id, standard.id)
        await db_session.delete(app)
        await db_session.commit()
        await service.reconcile()
        assert app.id not in service.children and processes[app.id].terminated
        assert standard.id in service.children
    finally:
        await service.on_shutdown()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_feishu_app_supervisor.py -v`
Expected: FAIL，等待子进程超时（`AssertionError: children did not start`）：supervisor 还不认助理应用。

- [ ] **Step 3: 写实现**

`coreman/runtime/gateway_feishu/service.py`：

模块文档串第一段后加一句：

```
个人助理应用另走一个租约扫描器（`app_leases`）：一个应用一个子进程，键是 platform_apps.id；
助理记录自己没有租约，也不各自起子进程。
```

导入：`from typing import Any` 加到标准库导入里；其余改为

```python
from coreman.core.assistants.apps import connection_fingerprint, is_assistant_app
from coreman.core.bus import app_leases, instances, leases
from coreman.core.db.models import Bot, BotLease, PlatformApp
```

`run_child_process` 之后加：

```python
def run_app_process(app_id: uuid.UUID, instance_id: str, generation: int) -> None:
    """个人助理应用连接的子进程入口；同样只在子进程里导入 SDK。"""
    from coreman.runtime.gateway_feishu.app_child import run_process

    run_process(app_id, instance_id, generation)
```

`Child` 加一个字段（放在最后，已有的按位置构造 `Child(generation, fingerprint)` 不受影响）：

```python
@dataclass
class Child:
    generation: int
    fingerprint: str
    process: ForkedProcess | None = None
    next_start: float = 0
    delay: float = 5
    started_at: float = 0
    # bot：一个机器人一条连接；app：一个个人助理应用一条连接（键是 platform_apps.id）。
    kind: str = "bot"
```

`_fingerprint` 之后加应用一侧的租约钩子：

```python
class _AppLeaseHooks:
    """个人助理应用的租约钩子：参数里的 `app_id` 是 platform_apps.id。

    子进程的拉起、退避重启、停止都复用 supervisor 的那一套，只是进程入口与租约表不同。
    """

    def __init__(self, service: GatewayFeishuService) -> None:
        self.service = service

    def lease_connected(self) -> Collection[uuid.UUID]:
        return [key for key, child in self.service.children.items() if child.kind == "app"]

    async def lease_connect(self, app_id: uuid.UUID, generation: int) -> bool:
        service = self.service
        async with service.factory() as session:
            app = await session.get(PlatformApp, app_id)
        if app is None or not is_assistant_app(app):
            return False
        if app_id in service.children:
            await service.stop_child(app_id, release=False)
        child = Child(generation, connection_fingerprint(service.cipher, app), kind="app")
        service.children[app_id] = child
        await service._supervise(app_id, child)
        return True

    async def lease_orphaned(self, app_id: uuid.UUID) -> None:
        await self.service.stop_child(app_id, release=False)

    async def lease_drain(self, app_id: uuid.UUID) -> None:
        await self.service.stop_child(app_id)

    async def lease_maintain(self, held: list[Any]) -> None:
        service = self.service
        async with service.factory() as session:
            apps = {
                app.id: app
                for app in await session.scalars(
                    select(PlatformApp).where(PlatformApp.id.in_([lease.key for lease in held]))
                )
            }
        for lease in held:
            app = apps.get(lease.key)
            child = service.children.get(lease.key)
            if app is None or child is None:
                continue
            fingerprint = connection_fingerprint(service.cipher, app)
            if child.fingerprint != fingerprint or child.generation != lease.generation:
                await service.stop_child(app.id, release=False)
                child = Child(int(lease.generation), fingerprint, kind="app")
                service.children[app.id] = child
            await service._supervise(app.id, child)
```

`GatewayFeishuService.__init__`：`self.lease_loop = LeaseCoordinator(...)` 之后加

```python
        # 只用来算助理应用的连接指纹（比较解密后的密钥）。
        self.cipher = get_settings().build_cipher()
        # 个人助理应用：一个应用一条连接，租约在 app_leases。
        self.app_lease_loop = LeaseCoordinator(
            platform="feishu",
            instance_id=lambda: self.instance_id,
            factory=self.factory,
            hooks=_AppLeaseHooks(self),
            heartbeat=False,
            store=app_leases,
        )
```

`draining` 的 setter 改为两个扫描器一起设：

```python
    @draining.setter
    def draining(self, value: bool) -> None:
        self.lease_loop.draining = value
        self.app_lease_loop.draining = value
```

`_heartbeat_loop` 里 `await leases.heartbeat(session, self.instance_id)` 下一行加：

```python
                    await app_leases.heartbeat(session, self.instance_id)
```

`spawn` 的第一段改为按类型选入口（函数名在调用时才解析，现有用例对 `run_child_process` 的打桩照常生效）：

```python
    async def spawn(self, bot_id: uuid.UUID, child: Child) -> ForkedProcess:
        process = FORKSERVER.Process(
            target=run_app_process if child.kind == "app" else run_child_process,
            args=(bot_id, self.instance_id, child.generation),
            daemon=True,
        )
```

（其后的启动与取消处理不变。）

`reconcile` 改为两个扫描器各跑一轮，前一个出错也不耽误后一个：

```python
    async def reconcile(self) -> None:
        try:
            await self.lease_loop.round()
        finally:
            await self.app_lease_loop.round()
```

bot 一侧的钩子只认自己的子进程，并拒绝助理记录（防御：正常情况下它们没有 `bot_leases` 行）：

```python
    def lease_connected(self) -> Collection[uuid.UUID]:
        return [key for key, child in self.children.items() if child.kind == "bot"]

    async def lease_connect(self, bot_id: uuid.UUID, generation: int) -> bool:
        async with self.factory() as session:
            bot = await session.get(Bot, bot_id)
        if bot is None or not bot.enabled or bot.kind != "standard":
            return False
```

（`lease_connect` 其余部分不变。）

`_supervise` 里写连接状态的那一段按类型选表：

```python
            async with self.factory() as session:
                store = app_leases if child.kind == "app" else leases
                await store.set_state(
                    session, bot_id, "disconnected", instance_id=self.instance_id
                )
                await session.commit()
```

`stop_child` 最后的释放按类型选扫描器：

```python
        self.children.pop(bot_id, None)
        if release:
            loop = self.app_lease_loop if child.kind == "app" else self.lease_loop
            await loop.release(bot_id, child.generation)
```

`on_shutdown` 不用改：它遍历 `self.children` 调 `stop_child`，两种子进程都会先停进程再按各自的表释放租约。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_feishu_app_supervisor.py tests/integration/test_feishu_supervisor.py tests/unit/test_feishu_forkserver.py -v`
Expected: 全部 passed。

普通机器人不受影响：现有 `test_supervisor_restarts_with_backoff_and_stops_before_releasing` 与 `test_feishu_forkserver.py`；新用例里每一步都断言普通机器人的子进程仍在。

- [ ] **Step 5: 提交**

```bash
git add coreman/runtime/gateway_feishu/service.py tests/integration/test_feishu_app_supervisor.py
git commit -m "个人助理：飞书 supervisor 为每个助理应用起一个子进程"
```

---

### Task 5: 退群接口、说明文案与权限清单

**Files:**
- Modify: `coreman/core/platforms/feishu.py`（`FeishuClient.leave_chat`，放在 `notify_user` 之前）
- Modify: `coreman/core/i18n/messages.py`（`zh` 与 `ja` 两张表各加一条 `assistant_private_only`，放在 `group_queued` 下一行）
- Modify: `coreman/core/feishu_apps/manifest.py`（`ASSISTANT_SCOPES`）
- Test: `tests/unit/test_feishu_client.py`（追加）、`tests/unit/test_feishu_app_registration.py`（追加）

**Interfaces:**
- Produces:
  - `FeishuClient.leave_chat(chat_id: str) -> None`：`DELETE /open-apis/im/v1/chats/{chat_id}/members?member_id_type=app_id`，请求体 `{"id_list": [<本应用 app_id>]}`
  - `msg("assistant_private_only")`
  - `manifest.ASSISTANT_SCOPES: tuple[str, ...]`

接口依据：飞书官方文档《将用户或机器人移出群聊》——「用户或机器人在任何条件下均可将自己移出群组」；移除机器人时 `member_id_type` 取 `app_id`、`id_list` 填应用的 App ID；权限 `im:chat` 或 `im:chat.members:write_only` 任一即可，这里申请范围更小的后者。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_feishu_client.py` 末尾追加：

```python
async def test_leave_chat_removes_the_app_itself_from_the_group():
    calls = []

    def response(request):
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200}
            )
        calls.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"invalid_id_list": []}})

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn", transport=httpx.MockTransport(response)
    ) as http:
        client = FeishuClient("cli_example", "synthetic-secret", http=http)
        await client.leave_chat("oc_group1")
        with pytest.raises(FeishuError):
            await client.leave_chat("oc/../x")
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "DELETE"
    assert request.url.path == "/open-apis/im/v1/chats/oc_group1/members"
    assert request.url.params["member_id_type"] == "app_id"
    assert json.loads(request.content) == {"id_list": ["cli_example"]}
    assert request.headers["Authorization"] == "Bearer tenant-token"
```

`tests/unit/test_feishu_app_registration.py` 末尾追加（文件头已 `from coreman.core.feishu_apps import manifest` 的话直接用；没有就按该文件现有的导入方式补上）：

```python
def test_assistant_scopes_are_not_requested_for_ordinary_bots():
    from coreman.core.i18n.messages import MESSAGES, msg

    assert manifest.ASSISTANT_SCOPES == ("im:chat.members:write_only",)
    assert not set(manifest.ASSISTANT_SCOPES) & set(manifest.addons()["scopes"]["tenant"])
    assert not set(manifest.ASSISTANT_SCOPES) & set(manifest.missing_scopes([], kind="tenant"))
    assert "私聊" in msg("assistant_private_only")
    assert all("assistant_private_only" in table for table in MESSAGES.values())
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_feishu_client.py::test_leave_chat_removes_the_app_itself_from_the_group tests/unit/test_feishu_app_registration.py::test_assistant_scopes_are_not_requested_for_ordinary_bots -v`
Expected: FAIL，`AttributeError: 'FeishuClient' object has no attribute 'leave_chat'` 与 `AttributeError: module ... has no attribute 'ASSISTANT_SCOPES'`。

- [ ] **Step 3: 写实现**

`coreman/core/platforms/feishu.py`，`notify_user` 之前加：

```python
    async def leave_chat(self, chat_id: str) -> None:
        """机器人把自己移出群。任何成员都可以主动退群，不要求是群主或管理员。"""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", chat_id):
            raise FeishuError(-2, "invalid chat identifier")
        await self.call(
            "DELETE",
            f"/open-apis/im/v1/chats/{chat_id}/members",
            params={"member_id_type": "app_id"},
            json={"id_list": [self.app_id]},
        )
```

`coreman/core/i18n/messages.py`：`zh` 表里 `"group_queued": ...` 下一行加

```python
        "assistant_private_only": (
            "这个应用是个人助理，只支持私聊，我已经退出刚才的群。有事请直接私聊我。"
        ),
```

`ja` 表里 `"group_queued": ...` 下一行加

```python
        "assistant_private_only": (
            "このアプリは個人アシスタントで、個別チャットのみ対応しています。"
            "先ほどのグループからは退出しました。ご用の際は直接メッセージをお送りください。"
        ),
```

`coreman/core/feishu_apps/manifest.py`，`VISIBILITY_SCOPES` 之后加：

```python
# 个人助理应用：只接私聊，被拉进群时把自己移出去。只有带 assistant 能力的应用需要，
# 不进默认清单，普通机器人不申请。
ASSISTANT_SCOPES: tuple[str, ...] = ("im:chat.members:write_only",)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_feishu_client.py tests/unit/test_feishu_app_registration.py tests/unit/test_feishu_app_management.py -v`
Expected: 全部 passed。

普通机器人不受影响：`test_assistant_scopes_are_not_requested_for_ordinary_bots`（默认清单与「缺失权限」都不含新权限），以及现有的清单用例。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/platforms/feishu.py coreman/core/i18n/messages.py coreman/core/feishu_apps/manifest.py tests/unit/test_feishu_client.py tests/unit/test_feishu_app_registration.py
git commit -m "个人助理：飞书退群接口、只支持私聊的说明文案与所需权限"
```

---

### Task 6: 入站事件分流（纯函数）

**Files:**
- Create: `coreman/runtime/gateway_feishu/assistant_inbound.py`（本任务只写 `Triage` 与 `triage`）
- Test: `tests/unit/test_assistant_inbound.py`

**Interfaces:**
- Produces:
  - `Triage`（冻结 dataclass）：`kind: Literal["ignore", "leave", "private"]`、`platform_user_id: str = ""`、`chat_id: str = ""`、`event_id: str = ""`、`click: bool = False`、`reason: str = ""`
  - `triage(raw: dict[str, Any], *, app_id: str) -> Triage`（`app_id` 是飞书的 App ID，如 `cli_example`）

规则：

| 事件 | 结果 |
|---|---|
| `header.app_id` 不是本应用，或事件类型不认识 | `ignore` |
| `im.chat.member.bot.added_v1` | `leave`，带 `chat_id`、拉群人的 `user_id`（可能为空）、`event_id` |
| `im.message.receive_v1`，`chat_type` 不是 `p2p` | `ignore`，`reason="group_message"` |
| `im.message.receive_v1`，私聊，发送人不是真人或没有 `user_id` | `ignore`，`reason="no_sender"` |
| `im.message.receive_v1`，私聊，真人 | `private` |
| `im.chat.access_event.bot_p2p_chat_entered_v1` | `private`（按 `operator_id.user_id`）；没有 `user_id` 则 `ignore` |
| `card.action.trigger` | `private` 且 `click=True`（按 `operator.user_id`）；没有 `user_id` 则 `ignore` |

- [ ] **Step 1: 写失败测试**

`tests/unit/test_assistant_inbound.py`：

```python
"""个人助理应用的事件分流：只接私聊，群消息丢弃，进群即退。"""

import json

from coreman.runtime.gateway_feishu.assistant_inbound import Triage, triage

APP = "cli_example"


def message(chat_type="p2p", sender_type="user", user_id="u_zhang"):
    return {
        "header": {"app_id": APP, "event_type": "im.message.receive_v1", "event_id": "ev1"},
        "event": {
            "sender": {
                "sender_type": sender_type,
                "sender_id": {"user_id": user_id, "open_id": "ou_zhang"},
            },
            "message": {
                "message_id": "om1",
                "chat_id": "oc_p2p" if chat_type == "p2p" else "oc_group",
                "chat_type": chat_type,
                "message_type": "text",
                "content": json.dumps({"text": "你好"}),
                "mentions": [],
            },
        },
    }


def test_private_message_goes_to_its_sender():
    assert triage(message(), app_id=APP) == Triage("private", platform_user_id="u_zhang")


def test_group_messages_are_dropped_before_normalization():
    found = triage(message(chat_type="group"), app_id=APP)
    assert found.kind == "ignore" and found.reason == "group_message"


def test_messages_without_a_human_sender_are_dropped():
    assert triage(message(sender_type="bot"), app_id=APP).reason == "no_sender"
    assert triage(message(user_id=""), app_id=APP).reason == "no_sender"


def test_events_of_another_app_or_unknown_type_are_ignored():
    assert triage(message(), app_id="cli_other").kind == "ignore"
    raw = {"header": {"app_id": APP, "event_type": "im.message.reaction.created_v1"}}
    assert triage(raw, app_id=APP).kind == "ignore"
    assert triage({}, app_id=APP).kind == "ignore"


def test_being_added_to_a_group_means_leave():
    raw = {
        "header": {
            "app_id": APP,
            "event_type": "im.chat.member.bot.added_v1",
            "event_id": "ev_added",
        },
        "event": {"chat_id": "oc_group", "operator_id": {"user_id": "u_li"}},
    }
    assert triage(raw, app_id=APP) == Triage(
        "leave", platform_user_id="u_li", chat_id="oc_group", event_id="ev_added"
    )
    raw["event"]["operator_id"] = {}
    assert triage(raw, app_id=APP).kind == "leave"  # 不知道是谁拉的也要退
    raw["event"]["chat_id"] = ""
    assert triage(raw, app_id=APP).kind == "ignore"


def test_entering_the_private_chat_and_card_clicks_route_by_operator():
    entered = {
        "header": {
            "app_id": APP,
            "event_type": "im.chat.access_event.bot_p2p_chat_entered_v1",
            "event_id": "ev_enter",
        },
        "event": {"chat_id": "oc_p2p", "operator_id": {"user_id": "u_zhang"}},
    }
    assert triage(entered, app_id=APP) == Triage("private", platform_user_id="u_zhang")
    click = {
        "header": {"app_id": APP, "event_type": "card.action.trigger", "event_id": "ev_click"},
        "event": {
            "operator": {"user_id": "u_zhang"},
            "context": {"open_chat_id": "oc_p2p", "open_message_id": "om_card"},
            "action": {"value": {"task_id": "personal:1"}},
        },
    }
    assert triage(click, app_id=APP) == Triage("private", platform_user_id="u_zhang", click=True)
    click["event"]["operator"] = {}
    assert triage(click, app_id=APP).kind == "ignore"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_inbound.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'coreman.runtime.gateway_feishu.assistant_inbound'`。

- [ ] **Step 3: 写实现**

`coreman/runtime/gateway_feishu/assistant_inbound.py`：

```python
"""个人助理应用的入站：一个应用一条长连接，进程内按人分发到各自的助理记录。

只接私聊：私聊消息、进入私聊、卡片回调按当事人的 user_id 找到他本人的助理记录，再走与普通
机器人相同的归一化与落库；群消息在归一化之前丢弃、不入库；被拉进群就退群并私信拉群的人。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class Triage:
    """一个事件该怎么处理：ignore 丢弃；leave 退群；private 按 `platform_user_id` 找助理记录。"""

    kind: Literal["ignore", "leave", "private"]
    platform_user_id: str = ""
    chat_id: str = ""
    event_id: str = ""
    # 卡片回调：还要确认卡片所在的会话就是这个人与自己助理的私聊。
    click: bool = False
    reason: str = ""


_IGNORE = Triage("ignore")


def _obj(container: object, key: str) -> dict[str, Any]:
    value = container.get(key) if isinstance(container, dict) else None
    return value if isinstance(value, dict) else {}


def triage(raw: dict[str, Any], *, app_id: str) -> Triage:
    header, event = _obj(raw, "header"), _obj(raw, "event")
    if header.get("app_id") != app_id:
        return _IGNORE
    event_type = header.get("event_type")
    if event_type == "im.chat.member.bot.added_v1":
        chat_id = str(event.get("chat_id") or "")
        if not chat_id:
            return _IGNORE
        return Triage(
            "leave",
            platform_user_id=str(_obj(event, "operator_id").get("user_id") or ""),
            chat_id=chat_id,
            event_id=str(header.get("event_id") or ""),
        )
    if event_type == "im.message.receive_v1":
        if _obj(event, "message").get("chat_type") != "p2p":
            return Triage("ignore", reason="group_message")
        sender = _obj(event, "sender")
        user_id = str(_obj(sender, "sender_id").get("user_id") or "")
        if sender.get("sender_type") != "user" or not user_id:
            return Triage("ignore", reason="no_sender")
        return Triage("private", platform_user_id=user_id)
    if event_type == "im.chat.access_event.bot_p2p_chat_entered_v1":
        user_id = str(_obj(event, "operator_id").get("user_id") or "")
        return Triage("private", platform_user_id=user_id) if user_id else _IGNORE
    if event_type == "card.action.trigger":
        user_id = str(_obj(event, "operator").get("user_id") or "")
        return Triage("private", platform_user_id=user_id, click=True) if user_id else _IGNORE
    return _IGNORE
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_inbound.py -v`
Expected: 6 passed。

普通机器人不受影响：本任务只新增一个还没有被引用的模块；`normalize_event` 没有改动，由现有 `tests/unit/test_feishu_inbound.py` 保证。

- [ ] **Step 5: 提交**

```bash
git add coreman/runtime/gateway_feishu/assistant_inbound.py tests/unit/test_assistant_inbound.py
git commit -m "个人助理：助理应用的入站事件分流"
```

---

### Task 7: 按人投递、退群与未开通钩子

**Files:**
- Modify: `coreman/runtime/gateway_feishu/assistant_inbound.py`（加 `AssistantInbound`）
- Test: `tests/integration/test_assistant_gateway_inbound.py`

**Interfaces:**
- Consumes:
  - `triage`（Task 6）、`assistant_for`（Task 1）、`FeishuClient.leave_chat` 与 `msg("assistant_private_only")`（Task 5）
  - 现有：`coreman.core.chat.identity.resolve_speaker(session, *, platform, platform_user_id) -> Speaker`（`Speaker.user_id` 为 None 表示认不出人）、`coreman.core.chat.reachability.private_chat_of(session, bot_id, platform, platform_user_id, chat_id) -> bool`、`coreman.runtime.gateway_feishu.inbound.normalize_event`、`coreman.runtime.gateway_feishu.reply_buttons.admit(session, bot, message, *, lease_generation) -> Outcome | None`、`coreman.runtime.gateway_feishu.child.BotInfo`、`FeishuClient.notify_user(user_id, content, *, key)`
- Produces（`coreman.runtime.gateway_feishu.assistant_inbound`）:
  - `AssistantInbound(*, factory, client, app: uuid.UUID, feishu_app_id: str, instance_id: str, generation: int, cipher: Cipher, fence: Callable[[], Awaitable[None]])`
  - `AssistantInbound.accept(raw: dict[str, Any]) -> Outcome | None`：交给 `DurableChannel(accept=...)`
  - `AssistantInbound.leave_group(chat_id: str, platform_user_id: str, event_id: str) -> None`
  - `AssistantInbound.aclose() -> None`：等后台的退群做完（最多 5 秒）
  - **1F 的钩子**：`AssistantInbound.on_unprovisioned(session: AsyncSession, *, platform_user_id: str, user_id: uuid.UUID | None, raw: dict[str, Any]) -> None`。发送人没有启用的助理记录时调用（私聊消息、进入私聊、卡片点击都会走到；`user_id` 为 None 表示通讯录里认不出这个人）。本期只记日志并丢弃。调用点在应答窗口内（2 秒）且持有入站那条库连接，钩子返回后由调用方提交事务；1F 在这里发开通卡片时，平台调用要放进 `self._background(...)`，不能在钩子里直接等。

卡片回调的「只认所有者」分两层：按操作人找到的是**操作人自己的**助理记录（不是卡片所属的那条）；再要求卡片所在的会话就是操作人与这条记录的私聊（`private_chat_of`，依据是私聊消息那一轮写下的 `user_reached`）。别人把卡片转走或在别处点，过不了第二层，应答「只有提问人可以选择」，不入库。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_gateway_inbound.py`：

```python
"""助理应用的入站：私聊按人投递，群消息丢弃，进群即退，非所有者的点击无效。"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from coreman.core.assistants.records import new_assistant_bot
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    InboundEvent,
    PlatformApp,
    Task,
    User,
    UserIdentity,
    UserReached,
)
from coreman.core.db.session import make_session_factory
from coreman.core.platforms.feishu import FeishuClient
from coreman.runtime.gateway_feishu.assistant_inbound import AssistantInbound
from coreman.runtime.gateway_feishu.transport import LeaseLost

APP = "cli_example"


async def _member(session, login, name, feishu_id):
    user = User(login_name=login, display_name=name)
    session.add(user)
    await session.flush()
    session.add(UserIdentity(user_id=user.id, platform="feishu", platform_user_id=feishu_id))
    await session.flush()
    return user


async def _seed(session):
    app = PlatformApp(
        platform="feishu", name="个人助理", capabilities=["assistant"], app_id=APP, secret_enc="x"
    )
    session.add(app)
    await session.flush()
    await session.refresh(app)
    zhang = await _member(session, "zhangsan", "张三", "u_zhang")
    li = await _member(session, "lisi", "李四", "u_li")
    wang = await _member(session, "wangwu", "王五", "u_wang")  # 还没开通
    bots = {}
    for owner in (zhang, li):
        bot = new_assistant_bot(
            owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc="x"
        )
        session.add(bot)
        bots[owner.id] = bot
    await session.commit()
    return app, zhang, li, wang, bots


def _inbound(db_engine, app, calls, *, fence=None):
    def handler(request):
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "t", "expire": 7200}
            )
        calls.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_notice"}})

    http = httpx.AsyncClient(
        base_url="https://open.feishu.cn", transport=httpx.MockTransport(handler)
    )

    async def no_fence():
        return None

    inbound = AssistantInbound(
        factory=make_session_factory(db_engine),
        client=FeishuClient(APP, "synthetic", http=http),
        app=app.id,
        feishu_app_id=APP,
        instance_id="gw-a",
        generation=1,
        cipher=Cipher(b"\x05" * 32),
        fence=fence or no_fence,
    )
    missing = []

    async def record(session, *, platform_user_id, user_id, raw):
        missing.append((platform_user_id, user_id))

    inbound.on_unprovisioned = record
    return inbound, http, missing


def message(user_id, mid, *, chat_type="p2p", chat_id=None):
    return {
        "header": {"app_id": APP, "event_type": "im.message.receive_v1", "event_id": f"ev_{mid}"},
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"user_id": user_id, "open_id": f"ou_{user_id}"},
            },
            "message": {
                "message_id": mid,
                "chat_id": chat_id or f"oc_{user_id}",
                "chat_type": chat_type,
                "message_type": "text",
                "content": json.dumps({"text": "帮我看下今天的日程"}),
                "mentions": [],
            },
        },
    }


def click(user_id, chat_id):
    return {
        "header": {"app_id": APP, "event_type": "card.action.trigger", "event_id": "ev_click"},
        "event": {
            "operator": {"user_id": user_id, "open_id": f"ou_{user_id}"},
            "context": {"open_chat_id": chat_id, "open_message_id": "om_card"},
            "action": {"value": {"task_id": "personal:1"}, "form_value": {}},
        },
    }


async def test_private_messages_land_on_the_senders_own_assistant(db_session, db_engine):
    app, zhang, li, _, bots = await _seed(db_session)
    inbound, http, missing = _inbound(db_engine, app, [])
    try:
        assert await inbound.accept(message("u_zhang", "om1")) is None
        assert await inbound.accept(message("u_li", "om2")) is None
        assert await inbound.accept(message("u_zhang", "om1")) is None  # 平台重推
    finally:
        await inbound.aclose()
        await http.aclose()
    events = (await db_session.scalars(select(InboundEvent).order_by(InboundEvent.id))).all()
    assert [(e.bot_id, e.platform_msg_id, e.chat_type) for e in events] == [
        (bots[zhang.id].id, "om1", "single"),
        (bots[li.id].id, "om2", "single"),
    ]
    tasks = (await db_session.scalars(select(Task).order_by(Task.id))).all()
    assert [(t.bot_id, t.kind) for t in tasks] == [
        (bots[zhang.id].id, "chat"),
        (bots[li.id].id, "chat"),
    ]
    assert tasks[0].payload["bot_key"] == bots[zhang.id].bot_key
    assert missing == []


async def test_senders_without_an_assistant_only_reach_the_hook(db_session, db_engine):
    app, zhang, _, wang, bots = await _seed(db_session)
    bots[zhang.id].enabled = False  # 停用的助理等于没有
    await db_session.commit()
    inbound, http, missing = _inbound(db_engine, app, [])
    try:
        for user_id, mid in (("u_wang", "om1"), ("u_ghost", "om2"), ("u_zhang", "om3")):
            assert await inbound.accept(message(user_id, mid)) is None
    finally:
        await inbound.aclose()
        await http.aclose()
    assert missing == [("u_wang", wang.id), ("u_ghost", None), ("u_zhang", zhang.id)]
    assert (await db_session.scalars(select(InboundEvent))).all() == []
    assert (await db_session.scalars(select(Task))).all() == []


async def test_group_messages_are_dropped_without_a_trace(db_session, db_engine):
    app, *_ = await _seed(db_session)
    calls: list = []
    inbound, http, missing = _inbound(db_engine, app, calls)
    try:
        raw = message("u_zhang", "om1", chat_type="group", chat_id="oc_group")
        assert await inbound.accept(raw) is None
    finally:
        await inbound.aclose()
        await http.aclose()
    assert missing == [] and calls == []
    assert (await db_session.scalars(select(InboundEvent))).all() == []


async def test_added_to_a_group_leaves_and_tells_who_added_it(db_session, db_engine):
    app, *_ = await _seed(db_session)
    calls: list = []
    inbound, http, _ = _inbound(db_engine, app, calls)
    raw = {
        "header": {
            "app_id": APP,
            "event_type": "im.chat.member.bot.added_v1",
            "event_id": "ev_added",
        },
        "event": {"chat_id": "oc_group", "operator_id": {"user_id": "u_li"}},
    }
    try:
        assert await inbound.accept(raw) is None
    finally:
        await inbound.aclose()
        await http.aclose()
    assert calls[0] == ("DELETE", "/open-apis/im/v1/chats/oc_group/members", {"id_list": [APP]})
    method, path, body = calls[1]
    assert (method, path, body["receive_id"]) == ("POST", "/open-apis/im/v1/messages", "u_li")
    assert "私聊" in json.loads(body["content"])["text"]
    assert (await db_session.scalars(select(InboundEvent))).all() == []


async def test_card_clicks_count_only_in_the_owners_own_private_chat(db_session, db_engine):
    app, zhang, li, _, bots = await _seed(db_session)
    db_session.add(
        UserReached(bot_id=bots[zhang.id].id, user_id=zhang.id, platform_chat_id="oc_zhang")
    )
    await db_session.commit()
    inbound, http, _ = _inbound(db_engine, app, [])
    try:
        # 李四点了张三私聊里的卡片：按李四路由到李四的助理，但这不是李四与它的私聊。
        assert await inbound.accept(click("u_li", "oc_zhang")) == "not_requester"
        assert (await db_session.scalars(select(Task))).all() == []
        assert await inbound.accept(click("u_zhang", "oc_zhang")) is None
    finally:
        await inbound.aclose()
        await http.aclose()
    tasks = (await db_session.scalars(select(Task))).all()
    assert [(t.bot_id, t.kind, t.lane) for t in tasks] == [
        (bots[zhang.id].id, "card_action", "fast")
    ]


async def test_lost_lease_refuses_the_event(db_session, db_engine):
    app, *_ = await _seed(db_session)

    async def lost():
        raise LeaseLost

    inbound, http, _ = _inbound(db_engine, app, [], fence=lost)
    try:
        with pytest.raises(LeaseLost):
            await inbound.accept(message("u_zhang", "om1"))
    finally:
        await inbound.aclose()
        await http.aclose()
    assert (await db_session.scalars(select(InboundEvent))).all() == []


def test_hook_signature_is_stable_for_the_provisioning_plan():
    import inspect

    parameters = inspect.signature(AssistantInbound.on_unprovisioned).parameters
    assert list(parameters) == ["self", "session", "platform_user_id", "user_id", "raw"]
    assert uuid.UUID  # 类型见实现：user_id: uuid.UUID | None
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_gateway_inbound.py -v`
Expected: FAIL，`ImportError: cannot import name 'AssistantInbound'`。

- [ ] **Step 3: 写实现**

`coreman/runtime/gateway_feishu/assistant_inbound.py`：导入区换成

```python
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants.routing import assistant_for
from coreman.core.chat.identity import resolve_speaker
from coreman.core.chat.reachability import private_chat_of
from coreman.core.crypto import Cipher
from coreman.core.i18n.messages import msg
from coreman.core.logging import get_logger
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.runtime.gateway_feishu.child import BotInfo
from coreman.runtime.gateway_feishu.inbound import normalize_event
from coreman.runtime.gateway_feishu.reply_buttons import Outcome, admit

log = get_logger(__name__)
```

文件末尾（`triage` 之后）加：

```python
class AssistantInbound:
    """一个助理应用连接的入站处理；`accept` 交给 `DurableChannel`，落库提交之后才应答。"""

    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        client: FeishuClient,
        app: uuid.UUID,
        feishu_app_id: str,
        instance_id: str,
        generation: int,
        cipher: Cipher,
        fence: Callable[[], Awaitable[None]],
    ) -> None:
        self.factory, self.client = factory, client
        # app 是 platform_apps.id；feishu_app_id 是飞书的 App ID（事件头里的 app_id）。
        self.app, self.feishu_app_id = app, feishu_app_id
        self.instance_id, self.generation = instance_id, generation
        self.cipher = cipher
        self._fence = fence
        self._tasks: set[asyncio.Task[None]] = set()

    async def accept(self, raw: dict[str, Any]) -> Outcome | None:
        await self._fence()
        found = triage(raw, app_id=self.feishu_app_id)
        if found.kind == "ignore":
            if found.reason:
                log.info("assistant_event_dropped", reason=found.reason)
            return None
        if found.kind == "leave":
            # 退群与私信是两次平台调用，不占应答窗口。
            self._background(
                self.leave_group(found.chat_id, found.platform_user_id, found.event_id)
            )
            return None
        async with self.factory() as session:
            await session.execute(text("SET LOCAL statement_timeout = 1500"))
            speaker = await resolve_speaker(
                session, platform="feishu", platform_user_id=found.platform_user_id
            )
            bot = (
                None
                if speaker.user_id is None
                else await assistant_for(session, self.app, speaker.user_id)
            )
            if bot is None:
                await self.on_unprovisioned(
                    session,
                    platform_user_id=found.platform_user_id,
                    user_id=speaker.user_id,
                    raw=raw,
                )
                await session.commit()
                return None
            message = normalize_event(
                raw,
                bot_id=bot.id,
                app_id=self.feishu_app_id,
                bot_open_id="",
                gateway_instance=self.instance_id,
                now=datetime.now(UTC),
                cipher=self.cipher,
            )
            if message is None:
                return None
            if found.click and not await private_chat_of(
                session, bot.id, "feishu", found.platform_user_id, message.chat_id
            ):
                # 卡片不在操作人与自己助理的私聊里：不是所有者在点。
                return "not_requester"
            info = BotInfo(bot.id, bot.bot_key, bot.welcome_message)
            outcome = await admit(session, info, message, lease_generation=self.generation)
            await session.commit()
            return outcome

    async def on_unprovisioned(
        self,
        session: AsyncSession,
        *,
        platform_user_id: str,
        user_id: uuid.UUID | None,
        raw: dict[str, Any],
    ) -> None:
        """发送人没有启用的助理记录。本期只记日志并丢弃；开通卡片由开通与停用那一期接在这里。

        `user_id` 为 None 表示通讯录里认不出这个人。调用点在应答窗口内，返回后由调用方提交。
        """
        header = raw.get("header")
        log.info(
            "assistant_unprovisioned",
            app=str(self.app),
            known_user=user_id is not None,
            event_type=header.get("event_type") if isinstance(header, dict) else None,
        )

    async def leave_group(self, chat_id: str, platform_user_id: str, event_id: str) -> None:
        """被拉进群：把自己移出去，再私信拉群的人说明只支持私聊。两步各自尽力而为。"""
        try:
            await self.client.leave_chat(chat_id)
        except FeishuError as exc:
            log.warning("assistant_leave_failed", code=exc.code)
        if not platform_user_id:
            return
        try:
            await self.client.notify_user(
                platform_user_id,
                msg("assistant_private_only"),
                key=f"assistant-leave:{event_id or chat_id}",
            )
        except FeishuError as exc:
            log.info("assistant_leave_notice_failed", code=exc.code)

    def _background(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        """退出前等后台的平台调用做完，最多 5 秒；没做完的取消。"""
        if not self._tasks:
            return
        _, pending = await asyncio.wait(set(self._tasks), timeout=5)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_gateway_inbound.py tests/unit/test_assistant_inbound.py -v`
Expected: 全部 passed。

普通机器人不受影响：`normalize_event`、`admit`、`enqueue_inbound` 一行没改，由现有 `tests/unit/test_feishu_inbound.py`、`tests/integration/test_feishu_card_interactions.py` 保证；`AssistantInbound` 只在助理应用的子进程里构造（Task 9）。

- [ ] **Step 5: 提交**

```bash
git add coreman/runtime/gateway_feishu/assistant_inbound.py tests/integration/test_assistant_gateway_inbound.py
git commit -m "个人助理：私聊与卡片回调按人投递，进群即退，未开通的发送人留钩子"
```

---

### Task 8: 出站——一个进程发送该应用下所有助理记录的消息

**Files:**
- Modify: `coreman/runtime/gateway_feishu/transport.py`（`FeishuTransport.__init__` 与 `fence`）
- Create: `coreman/runtime/gateway_feishu/app_outbound.py`
- Test: `tests/integration/test_feishu_app_outbound.py`

**Interfaces:**
- Consumes: `AppLease`、`app_leases`（Task 2）；现有 `FeishuTransport`、`LeaseLost`。
- Produces:
  - `FeishuTransport(..., lease_fence: Callable[[], Awaitable[None]] | None = None)`：给了就用它代替按 bot 的租约围栏；不给时行为不变
  - `coreman.runtime.gateway_feishu.app_outbound.AppOutbound(factory, client, *, app: uuid.UUID, instance_id: str, generation: int, guard: AsyncConnection | None = None)`
  - `AppOutbound.fence() -> None`（应用租约不在本实例这一代名下时抛 `LeaseLost`）
  - `AppOutbound.due() -> list[tuple[uuid.UUID, bool]]`（此刻有活的助理记录及其 `rich_cards`）
  - `AppOutbound.transport(bot_id) -> FeishuTransport`
  - `AppOutbound.round() -> bool`（True 表示还有没发完的，调用方别等兜底）

已知取舍：各助理记录的推送在一个进程里串行。同时有很多人在流式回复时，每张卡片的刷新间隔会随人数变长（每多一条在跑的回复，一轮多几次平台调用）。本期不做并发；需要时再把 `round()` 里的循环改成有上限的并发并相应加大子进程连接池。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_feishu_app_outbound.py`：

```python
"""助理应用的出站：一个进程按记录分发，租约围栏在应用上。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from coreman.core.assistants.records import new_assistant_bot
from coreman.core.bus import app_leases, instances, outbox, streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import (
    Bot,
    FeishuDelivery,
    FeishuSentMessage,
    OutboxItem,
    PlatformApp,
    User,
)
from coreman.core.db.session import make_session_factory
from coreman.runtime.gateway_feishu.app_outbound import AppOutbound
from coreman.runtime.gateway_feishu.transport import LeaseLost
from tests.integration.test_feishu_transport import FakeAPI


@pytest.fixture(autouse=True)
def _no_call_spacing(monkeypatch):
    monkeypatch.setattr("coreman.runtime.gateway_feishu.transport.CALL_SPACING_SECONDS", 0)


async def _notice(session, bot, chat_id, key):
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=key,
        target={"chat_id": chat_id},
        payload={"text": "到点提醒"},
    )


async def _seed(session):
    app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="x",
    )
    session.add(app)
    await session.flush()
    await session.refresh(app)
    bots = []
    for login, name in (("zhangsan", "张三"), ("lisi", "李四"), ("wangwu", "王五")):
        owner = User(login_name=login, display_name=name)
        session.add(owner)
        await session.flush()
        bot = new_assistant_bot(
            owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc="x"
        )
        session.add(bot)
        bots.append(bot)
    zhang, li, wang = bots
    await session.flush()
    wang.enabled = False
    standard = Bot(
        bot_key="demo-bot",
        platform="feishu",
        name="demo-bot",
        created_by=zhang.owner_id,
        model="m",
        working_dir="/d",
        credentials_enc="unused",
    )
    session.add(standard)
    await instances.register(
        session, instance_id="gw-a", service="gateway-feishu", version="dev", capacity=None
    )
    await app_leases.ensure_rows(session)
    generation = await app_leases.acquire(session, app.id, "gw-a")
    await session.commit()
    return app, zhang, li, wang, standard, generation


async def _statuses(session):
    rows = await session.execute(select(OutboxItem.dedupe_key, OutboxItem.status))
    return dict(rows.all())


async def test_one_round_delivers_for_every_enabled_assistant_of_the_app(db_session, db_engine):
    app, zhang, li, wang, standard, generation = await _seed(db_session)
    for bot, chat in ((zhang, "oc_zhang"), (li, "oc_li"), (wang, "oc_wang"), (standard, "oc_std")):
        await _notice(db_session, bot, chat, f"notice:{chat}")
    zhang.rich_cards = False
    await db_session.commit()
    api = FakeAPI()
    outbound = AppOutbound(
        make_session_factory(db_engine), api, app=app.id, instance_id="gw-a", generation=generation
    )
    assert {bot_id for bot_id, _ in await outbound.due()} == {zhang.id, li.id}
    assert await outbound.round() is False
    assert await _statuses(db_session) == {
        "notice:oc_zhang": "sent",
        "notice:oc_li": "sent",
        "notice:oc_wang": "pending",  # 停用的助理不发
        "notice:oc_std": "pending",  # 普通机器人的出站不归这个进程
    }
    sent_to = sorted(body["receive_id"] for _, path, body in api.calls if "receive_id" in body)
    assert sent_to == ["oc_li", "oc_zhang"]
    archived = (await db_session.scalars(select(FeishuSentMessage.bot_id))).all()
    assert sorted(archived, key=str) == sorted([zhang.id, li.id], key=str)
    assert outbound.transport(zhang.id).rich_cards is False
    assert outbound.transport(li.id).rich_cards is True
    # 没活了：不再碰任何一条记录。
    before = len(api.calls)
    assert await outbound.due() == []
    assert await outbound.round() is False and len(api.calls) == before


async def test_streams_are_pushed_per_assistant(db_session, db_engine):
    app, zhang, _, _, _, generation = await _seed(db_session)
    task = await tasks.enqueue(db_session, NewTask(bot_id=zhang.id, kind="chat", payload={}))
    await streams.create(
        db_session,
        task_id=task.id,
        bot_id=zhang.id,
        platform="feishu",
        stream_id="s",
        reply_context={"chat_id": "oc_zhang", "message_id": "om_in"},
        lease_generation=0,
        running_since=datetime.now(UTC),
    )
    await streams.update(db_session, task.id, pending_text="正在查", thinking_md="想一想")
    await db_session.commit()
    api = FakeAPI()
    outbound = AppOutbound(
        make_session_factory(db_engine), api, app=app.id, instance_id="gw-a", generation=generation
    )
    await outbound.round()
    assert any(path == "/open-apis/cardkit/v1/cards" for _, path, _ in api.calls)
    delivery = await db_session.get(FeishuDelivery, task.id)
    assert delivery is not None and delivery.message_id == "om_1"


async def test_one_assistant_failing_does_not_block_the_others(db_session, db_engine):
    app, zhang, li, _, _, generation = await _seed(db_session)
    await _notice(db_session, zhang, "oc_zhang", "notice:zhang")
    await _notice(db_session, li, "oc_li", "notice:li")
    await db_session.commit()
    outbound = AppOutbound(
        make_session_factory(db_engine),
        FakeAPI(),
        app=app.id,
        instance_id="gw-a",
        generation=generation,
    )

    async def boom():
        raise RuntimeError("synthetic")

    outbound.transport(li.id).round = boom
    assert await outbound.round() is False
    assert await _statuses(db_session) == {"notice:zhang": "sent", "notice:li": "pending"}


async def test_losing_the_app_lease_stops_every_delivery(db_session, db_engine):
    app, zhang, _, _, _, generation = await _seed(db_session)
    await _notice(db_session, zhang, "oc_zhang", "notice:zhang")
    await app_leases.release(db_session, app.id, "gw-a", generation)
    await db_session.commit()
    api = FakeAPI()
    outbound = AppOutbound(
        make_session_factory(db_engine), api, app=app.id, instance_id="gw-a", generation=generation
    )
    with pytest.raises(LeaseLost):
        await outbound.round()
    with pytest.raises(LeaseLost):
        await outbound.transport(zhang.id).round()
    assert api.calls == [] and await _statuses(db_session) == {"notice:zhang": "pending"}
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_feishu_app_outbound.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'coreman.runtime.gateway_feishu.app_outbound'`。

- [ ] **Step 3: transport 的租约围栏可替换**

`coreman/runtime/gateway_feishu/transport.py`：

`from collections.abc import Sequence` 改为 `from collections.abc import Awaitable, Callable, Sequence`。

`FeishuTransport.__init__` 的参数表在 `image_transport` 之后加一个参数，并在函数体里保存（放在 `self.credentials_fingerprint = credentials_fingerprint` 下一行）：

```python
        image_transport: httpx.AsyncBaseTransport | None = None,
        lease_fence: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
```

```python
        # 个人助理记录没有自己的租约：连接按应用认领，围栏由应用连接进程提供（AppOutbound）。
        self._lease_fence = lease_fence
```

`fence` 开头加一个分支，原有查询不动：

```python
    async def fence(self) -> None:
        if self._lease_fence is not None:
            await self._lease_fence()
            return
        async with self.factory() as session:
```

- [ ] **Step 4: 写 `AppOutbound`**

`coreman/runtime/gateway_feishu/app_outbound.py`：

```python
"""个人助理应用的出站：一个连接进程发送该应用下所有助理记录的回复与出站消息。

每条助理记录各用一个 `FeishuTransport`（出站锁、认领、流卡片都仍按记录），共用一个客户端和
一条守护连接；租约围栏换成应用的那一行。每轮先用一条查询找出此刻有活的记录，只对它们跑一轮，
空闲时的开销不随助理人数增长。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from coreman.core.db.models import AppLease, Bot, FeishuDelivery, OutboxItem, TaskStream
from coreman.core.logging import get_logger
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.runtime.gateway_feishu.transport import FeishuTransport, LeaseLost

log = get_logger(__name__)


class AppOutbound:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        client: FeishuClient,
        *,
        app: uuid.UUID,
        instance_id: str,
        generation: int,
        guard: AsyncConnection | None = None,
    ) -> None:
        self.factory, self.client = factory, client
        self.app, self.instance_id, self.generation = app, instance_id, generation
        self._guard = guard
        self._transports: dict[uuid.UUID, FeishuTransport] = {}

    async def fence(self) -> None:
        """应用的租约必须还在本实例这一代名下，且心跳没过期。"""
        async with self.factory() as session:
            row = await session.scalar(
                select(AppLease.app_id).where(
                    AppLease.app_id == self.app,
                    AppLease.holder_instance == self.instance_id,
                    AppLease.generation == self.generation,
                    AppLease.heartbeat_at > func.now() - timedelta(seconds=30),
                )
            )
        if row is None:
            raise LeaseLost

    async def due(self) -> list[tuple[uuid.UUID, bool]]:
        """此刻有活的助理记录及其富卡片开关。

        有活 = 有没推完的流式回复、有到点的待发出站、或有回复结束后还没撤掉的表情
        （对应 `FeishuTransport._deliver` 里的三段）。停用的记录不在内：它的出站留在队列里。
        """
        streaming = (
            select(TaskStream.task_id)
            .where(
                TaskStream.bot_id == Bot.id,
                TaskStream.delivery_mode == "stream",
                TaskStream.finish_pushed_at.is_(None),
            )
            .exists()
        )
        queued = (
            select(OutboxItem.id)
            .where(
                OutboxItem.bot_id == Bot.id,
                OutboxItem.status == "pending",
                OutboxItem.not_before <= func.now(),
            )
            .exists()
        )
        reactions = (
            select(FeishuDelivery.task_id)
            .join(TaskStream, TaskStream.task_id == FeishuDelivery.task_id)
            .where(
                TaskStream.bot_id == Bot.id,
                FeishuDelivery.reaction_done.is_(True),
                FeishuDelivery.reaction_id.is_not(None),
                or_(
                    FeishuDelivery.reaction_retry_at.is_(None),
                    FeishuDelivery.reaction_retry_at <= func.now(),
                ),
            )
            .exists()
        )
        stmt = (
            select(Bot.id, Bot.rich_cards)
            .where(
                Bot.platform_app_id == self.app,
                Bot.kind == "assistant",
                Bot.enabled.is_(True),
                or_(streaming, queued, reactions),
            )
            .order_by(Bot.id)
        )
        async with self.factory() as session:
            return [(row[0], bool(row[1])) for row in await session.execute(stmt)]

    def transport(self, bot_id: uuid.UUID) -> FeishuTransport:
        transport = self._transports.get(bot_id)
        if transport is None:
            transport = FeishuTransport(
                self.factory,
                self.client,
                bot_id=bot_id,
                instance_id=self.instance_id,
                generation=self.generation,
                guard=self._guard,
                lease_fence=self.fence,
            )
            self._transports[bot_id] = transport
        return transport

    async def round(self) -> bool:
        await self.fence()
        more = False
        for bot_id, rich_cards in await self.due():
            transport = self.transport(bot_id)
            transport.rich_cards = rich_cards
            try:
                more = await transport.round() or more
            except LeaseLost:
                raise
            except FeishuError as exc:
                log.warning("feishu_delivery_failed", bot_id=str(bot_id), code=exc.code)
            except Exception as exc:  # noqa: BLE001 一个人的回复出问题不能卡住其他人
                # 只记异常类型：消息里可能带 SQL 参数或平台响应。
                log.error("feishu_app_round_failed", bot_id=str(bot_id), error=type(exc).__name__)
        return more
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/integration/test_feishu_app_outbound.py tests/integration/test_feishu_transport.py tests/integration/test_feishu_card_interactions.py -v`
Expected: 全部 passed。

普通机器人不受影响：`tests/integration/test_feishu_transport.py` 全部用例（不传 `lease_fence`，围栏仍查 `bot_leases`，其中 `test_card_identity_and_sequence_survive_takeover` 与 `test_round_checks_the_lease_fence_once` 直接覆盖围栏）；新用例断言普通机器人的出站条目不被应用进程认领。

- [ ] **Step 6: 提交**

```bash
git add coreman/runtime/gateway_feishu/transport.py coreman/runtime/gateway_feishu/app_outbound.py tests/integration/test_feishu_app_outbound.py
git commit -m "个人助理：应用连接进程按记录发送出站，租约围栏在应用上"
```

---

### Task 9: 应用连接子进程

**Files:**
- Modify: `coreman/runtime/gateway_feishu/child.py`（抽出 `wait_for_notice`；`run_child` 不接助理记录）
- Create: `coreman/runtime/gateway_feishu/app_child.py`
- Modify: `coreman/runtime/gateway_feishu/preload.py`
- Test: `tests/unit/test_feishu_child_wakeup.py`（追加）、`tests/integration/test_feishu_app_child.py`

**Interfaces:**
- Consumes: `AssistantInbound`（Task 7）、`AppOutbound`（Task 8）、`app_leases.set_state`（Task 2）、`app_credentials` / `connection_fingerprint` / `is_assistant_app`（Task 1）、`service.run_app_process`（Task 4 已按这个模块路径延迟导入）。
- Produces:
  - `child.wait_for_notice(listener: Listener, wanted: Callable[[str], bool], stop: asyncio.Event, poll_seconds: float) -> None`；`child.wait_for_work` 签名不变，内部改调它
  - `app_child.app_state(factory, cipher: Cipher, app_id: uuid.UUID) -> tuple[str | None, set[str]]`：应用当前的连接指纹（不可服务时 None）与名下启用的助理记录 id（字符串）
  - `app_child.run_app(app_id: uuid.UUID, instance_id: str, generation: int) -> None`
  - `app_child.run_process(app_id: uuid.UUID, instance_id: str, generation: int) -> None`

`run_app` 与 `child.run_child` 的骨架相同（两个引擎、信号、`feishu-app-connection:{app_id}` 咨询锁、长连接、出站循环、收尾），差别只在：凭证来自 `platform_apps`、入站交给 `AssistantInbound`、出站交给 `AppOutbound`、每轮看的是应用而不是单个 bot。骨架没有抽成公共函数：`run_child` 是普通机器人的主路径，本期不动它的结构。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_feishu_child_wakeup.py` 末尾追加（文件头的导入加上 `wait_for_notice`）：

```python
async def test_app_connection_wakes_up_for_any_of_its_assistants() -> None:
    mine = {str(uuid.uuid4()), str(uuid.uuid4())}
    listener, stop = FakeListener(), asyncio.Event()
    loop = asyncio.get_running_loop()
    listener.queues["stream_updated"].put_nowait({"bot_id": str(uuid.uuid4())})

    async def notify_later() -> None:
        await asyncio.sleep(0.2)
        listener.queues["outbox_added"].put_nowait({"bot_id": sorted(mine)[1]})

    started = loop.time()
    notifier = asyncio.create_task(notify_later())
    await wait_for_notice(listener, mine.__contains__, stop, 5.0)  # type: ignore[arg-type]
    await notifier
    assert 0.15 <= loop.time() - started < 2.0
    # 通知项（bot_id 为 None）与空集合都不算。
    listener.queues["outbox_added"].put_nowait({"bot_id": "None"})
    started = loop.time()
    await wait_for_notice(listener, set().__contains__, stop, 0.3)  # type: ignore[arg-type]
    assert 0.25 <= loop.time() - started < 1.5
```

`tests/integration/test_feishu_app_child.py`：

```python
"""应用连接子进程每轮要看的状态，以及子进程入口。"""

from __future__ import annotations

import uuid

from lark_oapi.ws import client as ws_client

from coreman.core.assistants.apps import connection_fingerprint
from coreman.core.assistants.records import new_assistant_bot
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, PlatformApp, User
from coreman.core.db.session import make_session_factory
from coreman.runtime.gateway_feishu import app_child

CIPHER = Cipher(b"\x05" * 32)


async def test_app_state_tracks_the_app_and_its_enabled_assistants(db_session, db_engine):
    app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="x",
    )
    db_session.add(app)
    await db_session.flush()
    await db_session.refresh(app)
    bots = []
    for login in ("zhangsan", "lisi"):
        owner = User(login_name=login, display_name=login)
        db_session.add(owner)
        await db_session.flush()
        bot = new_assistant_bot(
            owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc="x"
        )
        db_session.add(bot)
        bots.append(bot)
    db_session.add(
        Bot(
            bot_key="demo-bot",
            platform="feishu",
            name="demo-bot",
            created_by=bots[0].owner_id,
            model="m",
            working_dir="/d",
            credentials_enc="unused",
        )
    )
    await db_session.flush()
    bots[1].enabled = False
    await db_session.commit()
    factory = make_session_factory(db_engine)
    fingerprint, owned = await app_child.app_state(factory, CIPHER, app.id)
    assert fingerprint == connection_fingerprint(CIPHER, app) and owned == {str(bots[0].id)}
    app.secret_enc = "rotated"
    await db_session.commit()
    assert (await app_child.app_state(factory, CIPHER, app.id))[0] != fingerprint
    app.enabled = False
    await db_session.commit()
    assert await app_child.app_state(factory, CIPHER, app.id) == (None, set())
    assert await app_child.app_state(factory, CIPHER, uuid.uuid4()) == (None, set())


def test_run_process_gives_the_app_child_its_own_sdk_loop(monkeypatch):
    preloaded = ws_client.loop
    seen = []

    async def fake_run_app(app_id, instance_id, generation):
        seen.append(ws_client.loop)

    monkeypatch.setattr(ws_client, "loop", preloaded)
    monkeypatch.setattr(app_child, "run_app", fake_run_app)
    app_child.run_process(uuid.uuid4(), "instance", 1)
    assert seen and seen[0] is not preloaded and not seen[0].is_closed()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_feishu_child_wakeup.py tests/integration/test_feishu_app_child.py -v`
Expected: FAIL，`ImportError: cannot import name 'wait_for_notice'` 与 `ImportError: cannot import name 'app_child'`。

- [ ] **Step 3: 抽出 `wait_for_notice`，`run_child` 不接助理记录**

`coreman/runtime/gateway_feishu/child.py`：导入加 `from collections.abc import Callable`；把 `wait_for_work` 换成下面两个函数：

```python
async def wait_for_notice(
    listener: Listener, wanted: Callable[[str], bool], stop: asyncio.Event, poll_seconds: float
) -> None:
    """睡到有 `wanted` 认的 bot 的流 / 出站通知、收到停止信号，或兜底轮询到点为止。

    所有飞书子进程都听同样的通道：不认的 bot 的通知不算数，继续睡满剩下的时间。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + poll_seconds
    while not stop.is_set():
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        waiters = [asyncio.create_task(listener.wait(ch, remaining)) for ch in WAKE_CHANNELS]
        stopper = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait([*waiters, stopper], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        payloads = [p for task in waiters if task in done for p in task.result()]
        if any(wanted(str(p.get("bot_id") or "")) for p in payloads):
            return


async def wait_for_work(
    listener: Listener, bot_id: uuid.UUID, stop: asyncio.Event, poll_seconds: float
) -> None:
    """睡到本 bot 有流 / 出站通知、收到停止信号，或兜底轮询到点为止。"""
    target = str(bot_id)
    await wait_for_notice(listener, lambda value: value == target, stop, poll_seconds)
```

`run_child` 里加载 bot 后的判断加一个条件（助理记录的连接归应用子进程）：

```python
            if (
                bot is None
                or not bot.enabled
                or bot.platform != "feishu"
                or bot.kind != "standard"
            ):
                return
```

- [ ] **Step 4: 写应用子进程**

`coreman/runtime/gateway_feishu/app_child.py`：

```python
"""个人助理应用的 SDK 子进程：一个应用一条长连接，进程内按人分发。

与 `child.run_child` 同一副骨架（数据库循环与 SDK WS 线程隔离、应用独占锁、租约围栏、出站
循环），差别在于它不绑定某一个 bot：入站按发送人找助理记录（`AssistantInbound`），出站
发送该应用下所有助理记录的消息（`AppOutbound`），凭证与存活都看 platform_apps 那一行。
"""

from __future__ import annotations

import asyncio
import multiprocessing
import signal
import uuid

from lark_oapi.ws import client as ws_client
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coreman.core.assistants.apps import app_credentials, connection_fingerprint, is_assistant_app
from coreman.core.bus import app_leases
from coreman.core.bus.notify import Listener, asyncpg_dsn
from coreman.core.config import get_settings
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, PlatformApp
from coreman.core.db.session import make_session_factory
from coreman.core.logging import configure_logging, get_logger
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.runtime.gateway_feishu.app_outbound import AppOutbound
from coreman.runtime.gateway_feishu.assistant_inbound import AssistantInbound
from coreman.runtime.gateway_feishu.channel import DurableChannel
from coreman.runtime.gateway_feishu.child import (
    ERROR_BACKOFF_SECONDS,
    IDLE_POLL_SECONDS,
    MAX_OVERFLOW,
    POOL_SIZE,
    WAKE_CHANNELS,
    _sleep_unless_stopped,
    wait_for_notice,
)
from coreman.runtime.gateway_feishu.transport import LeaseLost


async def app_state(
    factory: async_sessionmaker[AsyncSession], cipher: Cipher, app_id: uuid.UUID
) -> tuple[str | None, set[str]]:
    """应用当前的连接指纹（已删除或不该再连时为 None）与它名下启用的助理记录 id。

    指纹用来发现密钥变更；id 集合用来过滤唤醒通知（通知的载荷是 bot_id）。
    """
    async with factory() as session:
        app = await session.get(PlatformApp, app_id)
        if app is None or not is_assistant_app(app):
            return None, set()
        owned = await session.scalars(
            select(Bot.id).where(
                Bot.platform_app_id == app_id, Bot.kind == "assistant", Bot.enabled.is_(True)
            )
        )
        return connection_fingerprint(cipher, app), {str(bot_id) for bot_id in owned}


async def run_app(app_id: uuid.UUID, instance_id: str, generation: int) -> None:
    # 与 run_child 相同：看 multiprocessing 留给子进程的 supervisor 哨兵。
    supervisor = multiprocessing.parent_process()
    assert supervisor is not None  # 只由 supervisor 的 `spawn` 拉起
    cfg = get_settings()
    configure_logging(service="gateway-feishu-app", instance=instance_id, level=cfg.log_level)
    log = get_logger(__name__).bind(app=str(app_id))
    engine = create_async_engine(
        cfg.database_url,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    guard_engine = create_async_engine(
        cfg.database_url,
        poolclass=NullPool,
        hide_parameters=True,
        isolation_level="AUTOCOMMIT",
    )
    factory = make_session_factory(engine)
    listener: Listener | None = None
    app_guard: AsyncConnection | None = None
    client: FeishuClient | None = None
    channel: DurableChannel | None = None
    inbound: AssistantInbound | None = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        cipher = cfg.build_cipher()
        async with factory() as session:
            app = await session.get(PlatformApp, app_id)
            if app is None or not is_assistant_app(app):
                return
            credentials = app_credentials(cipher, app)
            initial = connection_fingerprint(cipher, app)
        feishu_app_id, secret = credentials["app_id"], credentials["app_secret"]
        app_guard = await guard_engine.connect()
        exclusive = await app_guard.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended(:key,0))"),
            {"key": f"feishu-app-connection:{feishu_app_id}"},
        )
        if not exclusive:
            raise RuntimeError("application already connected")
        client = FeishuClient(feishu_app_id, secret)
        outbound = AppOutbound(
            factory,
            client,
            app=app_id,
            instance_id=instance_id,
            generation=generation,
            guard=app_guard,
        )
        await outbound.fence()
        inbound = AssistantInbound(
            factory=factory,
            client=client,
            app=app_id,
            feishu_app_id=feishu_app_id,
            instance_id=instance_id,
            generation=generation,
            cipher=cipher,
            fence=outbound.fence,
        )
        channel = DurableChannel(accept=inbound.accept, app_id=feishu_app_id, app_secret=secret)
        await channel.start_background(timeout=30)
        async with factory() as session:
            await app_leases.set_state(session, app_id, "subscribed", instance_id=instance_id)
            await session.commit()
        listener = Listener(asyncpg_dsn(cfg.database_url), WAKE_CHANNELS)
        await listener.start()
        owned: set[str] = set()
        while not stop.is_set():
            if not supervisor.is_alive():
                break
            # 独占锁连接的探活放在 try 外面：它断了独占就没了，只能退出让 supervisor 重启。
            await app_guard.execute(text("SELECT 1"))
            try:
                current, owned = await app_state(factory, cipher, app_id)
                if current != initial:
                    break  # 密钥变了、应用停用或删除：退出，由 supervisor 决定重连还是停止
                more = await outbound.round()  # 应用租约的围栏在这一轮开头查一次
            except LeaseLost:
                raise
            except FeishuError as exc:
                log.warning("feishu_delivery_failed", code=exc.code)
                await _sleep_unless_stopped(stop, ERROR_BACKOFF_SECONDS)
                continue
            except Exception as exc:  # noqa: BLE001 库抖动、SDK 之外的异常：记一笔下一轮再来
                # 只记异常类型：消息里可能带 SQL 参数或平台响应。
                log.error("feishu_app_child_round_failed", error=type(exc).__name__)
                await _sleep_unless_stopped(stop, ERROR_BACKOFF_SECONDS)
                continue
            if not more:
                await wait_for_notice(listener, owned.__contains__, stop, IDLE_POLL_SECONDS)
    except LeaseLost:
        log.info("feishu_app_child_lease_lost")
    finally:
        if channel:
            try:
                await asyncio.wait_for(channel.disconnect(), timeout=5)
            except (TimeoutError, RuntimeError):
                pass
        if inbound:
            await inbound.aclose()
        if client:
            await client.aclose()
        if listener:
            await listener.stop()
        if app_guard:
            await app_guard.close()
        await engine.dispose()
        await guard_engine.dispose()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def run_process(app_id: uuid.UUID, instance_id: str, generation: int) -> None:
    """forkserver 子进程入口（supervisor 的 `spawn`，`child.kind == "app"`）。"""
    # 预加载时关掉了 SDK 的模块级事件循环（见 preload），本进程的长连接另建一个。
    ws_client.loop = asyncio.new_event_loop()
    try:
        asyncio.run(run_app(app_id, instance_id, generation))
    except Exception:
        # 子进程默认 traceback 可能携带 SDK 响应；这里只报告失败类型的稳定事件。
        get_logger(__name__).error("feishu_app_child_failed")
        raise SystemExit(1) from None
```

`coreman/runtime/gateway_feishu/preload.py`：`import coreman.runtime.gateway_feishu.child  # noqa: F401` 上一行加

```python
import coreman.runtime.gateway_feishu.app_child  # noqa: F401
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/unit/test_feishu_child_wakeup.py tests/integration/test_feishu_app_child.py tests/unit/test_feishu_forkserver.py tests/unit/test_import_order.py tests/unit/test_layering.py -v`
Expected: 全部 passed。

Run: `uv run ruff check . && uv run mypy coreman`
Expected: 无错误。

普通机器人不受影响：`tests/unit/test_feishu_child_wakeup.py` 原有两条用例（`wait_for_work` 的行为不变）、`tests/unit/test_feishu_forkserver.py`（预加载与子进程入口）。`run_child` 只多了一个「助理记录直接返回」的条件，普通记录的 `kind` 恒为 `standard`。

- [ ] **Step 6: 提交**

```bash
git add coreman/runtime/gateway_feishu/child.py coreman/runtime/gateway_feishu/app_child.py coreman/runtime/gateway_feishu/preload.py tests/unit/test_feishu_child_wakeup.py tests/integration/test_feishu_app_child.py
git commit -m "个人助理：助理应用的连接子进程"
```

---

### Task 10: 连接告警按应用报

**Files:**
- Modify: `coreman/core/observability/alerts.py`（`tick` 里遍历 bot 连接的那一段）
- Test: `tests/integration/test_alerts.py`（追加）

**Interfaces:**
- Consumes: `AppLease`（Task 2）。
- Produces: 告警键 `gateway:app:{platform_apps.id}`（文案沿用 `alert_gateway`）；助理记录不再产生 `gateway:{bot_id}` 告警。

背景：现有告警对每个启用的 bot 外连 `bot_leases`，没有行就算断线。助理记录没有 `bot_leases` 行，不改的话每开通一个人就多一条常驻的假告警；而真正要盯的是应用那一条连接。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_alerts.py` 末尾追加：

```python
async def test_assistants_alert_per_app_connection_not_per_record(db_session):
    from coreman.core.assistants.records import new_assistant_bot
    from coreman.core.bus import app_leases, instances
    from coreman.core.db.models import PlatformApp, User
    from coreman.core.observability.alerts import tick

    owner = User(login_name="zhangsan", display_name="张三")
    app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="x",
    )
    db_session.add_all([owner, app])
    await db_session.flush()
    await db_session.refresh(app)
    assistant = new_assistant_bot(
        owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc="x"
    )
    db_session.add(assistant)
    await db_session.commit()
    now = datetime.now(UTC)
    await tick(db_session, now)
    await db_session.commit()
    assert await db_session.get(AlertState, f"gateway:{assistant.id}") is None
    connection = await db_session.get(AlertState, f"gateway:app:{app.id}")
    assert connection.active and not connection.firing
    # 应用连上之后告警解除。
    await instances.register(
        db_session, instance_id="gw-a", service="gateway-feishu", version="dev", capacity=None
    )
    await app_leases.ensure_rows(db_session)
    assert await app_leases.acquire(db_session, app.id, "gw-a") == 1
    await app_leases.set_state(db_session, app.id, "subscribed", instance_id="gw-a")
    await db_session.commit()
    await tick(db_session, datetime.now(UTC))
    await db_session.commit()
    await db_session.refresh(connection)
    assert not connection.active
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_alerts.py::test_assistants_alert_per_app_connection_not_per_record -v`
Expected: FAIL，`gateway:{assistant.id}` 的告警行存在。

- [ ] **Step 3: 写实现**

`coreman/core/observability/alerts.py`：模型导入里加 `AppLease`。把遍历 bot 连接的 `for` 循环换成下面两段（第一段只多了 `Bot.kind == "standard"`，循环体原样；第二段是新增的）：

```python
    for identity, state, heartbeat in await session.execute(
        select(Bot.id, BotLease.connection_state, BotLease.heartbeat_at)
        .outerjoin(BotLease, BotLease.bot_id == Bot.id)
        .where(Bot.enabled, Bot.kind == "standard")
    ):
        key = f"gateway:{identity}"
        seen.add(key)
        disconnected = (
            state != "subscribed" or heartbeat is None or now - heartbeat > timedelta(seconds=30)
        )
        await observe(
            session,
            key,
            disconnected,
            "alert_gateway",
            now,
            delay=0 if state in {"kicked", "auth_failed"} else 300,
        )
    # 个人助理：连接按应用认领，一条连接服务该应用下所有人，告警也按应用报。
    for identity, state, heartbeat in await session.execute(
        select(PlatformApp.id, AppLease.connection_state, AppLease.heartbeat_at)
        .outerjoin(AppLease, AppLease.app_id == PlatformApp.id)
        .where(
            PlatformApp.platform == "feishu",
            PlatformApp.enabled,
            PlatformApp.capabilities.contains(["assistant"]),
        )
    ):
        key = f"gateway:app:{identity}"
        seen.add(key)
        disconnected = (
            state != "subscribed" or heartbeat is None or now - heartbeat > timedelta(seconds=30)
        )
        await observe(
            session,
            key,
            disconnected,
            "alert_gateway",
            now,
            delay=0 if state in {"kicked", "auth_failed"} else 300,
        )
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_alerts.py -v`
Expected: 全部 passed。

普通机器人不受影响：现有 `test_evaluation_ignores_future_queue_and_detects_enabled_disconnected_bot`（普通机器人断线仍然告警）。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/observability/alerts.py tests/integration/test_alerts.py
git commit -m "个人助理：连接告警按应用报，助理记录不单独告警"
```

---

### Task 11: 文档与整体验证

**Files:**
- Modify: `docs/feishu.md`（在「个人资料授权（仅私聊）」一节之前加一节）

- [ ] **Step 1: 写文档**

`docs/feishu.md` 新增一节：

```markdown
## 个人助理应用

平台应用（`platform_apps`）勾选 `assistant` 能力后，飞书网关把它当作个人助理应用：

- **一个应用一条长连接。** 连接租约在 `app_leases`，由飞书网关的 supervisor 认领，每个应用起一个子进程；应用下还没有任何人开通时也保持连接。应用停用、去掉 `assistant` 能力或删除时断开；密钥变更时自动重连。
- **只接私聊。** 私聊消息、进入私聊、卡片回调按当事人的 `user_id`（来自通讯录同步）找到他本人的助理记录，之后的处理与普通机器人相同。还没开通的人发来的消息只记一条日志。
- **群消息丢弃、不入库。** 被拉进群时应用会自己退群，并私信拉群的人说明只支持私聊。
- **卡片只认所有者。** 回调按操作人路由，并且卡片必须在操作人与自己助理的私聊里。
- **出站。** 同一个子进程发送该应用下所有助理记录的回复与通知。
- **告警。** 连接异常按应用告警（键 `gateway:app:<应用 id>`），不按每个人的助理记录告警。

应用需要的权限：普通机器人的应用身份权限之外，再开通 `im:chat.members:write_only`（退群用，见 `coreman/core/feishu_apps/manifest.py` 的 `ASSISTANT_SCOPES`）。事件订阅与普通机器人相同：`im.message.receive_v1`、`im.chat.member.bot.added_v1`、`im.chat.access_event.bot_p2p_chat_entered_v1`，回调 `card.action.trigger`，订阅方式选长连接。

一个飞书应用不能同时既是个人助理应用又绑定给某个普通机器人：两者会争同一条长连接。
```

- [ ] **Step 2: 全量检查**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy coreman && uv run alembic check`
Expected: 全部通过。

Run: `uv run pytest tests/unit tests/integration -q`
Expected: 全部 passed。重点看：`test_feishu_supervisor.py`、`test_feishu_transport.py`、`test_feishu_card_interactions.py`、`test_gateway_wecom.py`、`test_bus_leases.py`、`test_alerts.py`、`test_feishu_inbound.py`（普通机器人路径）。

- [ ] **Step 3: 本机真连一次（需要一个测试用的飞书应用；没有就跳过并在 PR 里写明）**

1. 在本机库里建一条 `platform_apps`：飞书、`capabilities` 含 `assistant`、填测试应用的 App ID 与密钥。
2. 起飞书网关，日志里应出现 `service=gateway-feishu-app` 的子进程，`app_leases.connection_state` 变成 `subscribed`。
3. 用一个没有助理记录的账号私聊该应用：日志出现 `assistant_unprovisioned`，`inbound_events` 无新增。
4. 把应用拉进一个群：应用自动退群，拉群的人收到「只支持私聊」的私信；日志无 `assistant_leave_failed`。
5. 把该 `platform_apps` 行 `enabled` 改为 false：5 秒内子进程退出，`app_leases.holder_instance` 置空。

- [ ] **Step 4: 敏感词自查后提交**

先对本分支全部新增内容跑一遍仓库约定的敏感词自查（公司名、内部域名、内部机器名、本机绝对路径、真实人名），有输出就改成通用表述，再提交：

```bash
git add docs/feishu.md
git commit -m "个人助理：文档补充个人助理应用的网关行为"
```

---

## 与设计文档的对照

| 设计文档（第一节「网关改造」） | 本计划 |
|---|---|
| 租约与子进程按 `platform_app_id` 认领 | Task 2–4：`app_leases` + 第二个租约扫描器 + 应用子进程 |
| 「一个应用只能分给一个机器人」对助理应用放开 | 1A 已做（`reserve_feishu_app`）；本计划保留子进程里的 `feishu-app-connection:{app_id}` 咨询锁 |
| 私聊按发送人的用户编号找助理记录，再进现有归一化与落库 | Task 1、6、7 |
| 没有记录时发「开通个人助理」卡片 | 本期只留钩子 `AssistantInbound.on_unprovisioned`（Task 7），卡片在 1F |
| 群消息一律丢弃；进群即退并私信说明；退群接口与权限 | Task 5–7 |
| 卡片回调按操作人路由，只认所有者 | Task 7 |
| 同一进程认领该应用下所有助理记录的待发消息，出站锁仍按记录 | Task 8 |
| 密钥轮换让个人授权失效 | 不在本计划（授权属于 1F）；本计划只保证密钥变更后连接自动重建 |

## 已知限制（本期不处理）

- 管理台的租约列表、按 bot 排空、滚动升级的等待条件（`operations.py`）只看 `bot_leases`，看不到应用连接。应用连接的状态目前只能查 `app_leases` 表与告警。停机时 supervisor 仍会先停应用子进程再释放应用租约。
- `scheduler/reaper.py` 的过期租约回收只处理 `bot_leases`。网关实例异常退出后，`app_leases.connection_state` 会停在旧值直到别的实例接管（接管本身不受影响：认领时会绕过心跳过期的持有者）。
- 出站在一个进程里串行（见 Task 8）。
- 应用成为助理应用之前就已经在的群不会被清退：只在收到进群事件时退群。那些群里的消息照样丢弃。

## 开工前需真机或文档核实

1. **退群接口实测。** 文档写明机器人可以用 `member_id_type=app_id`、`id_list=[本应用 App ID]` 把自己移出群，权限 `im:chat.members:write_only`。需用真实应用确认：只开这一个权限是否足够、外部群是否报 232033、机器人不在群里时的错误码（预期 232011，当前按普通失败记日志）。
2. **进群事件里的拉群人。** `im.chat.member.bot.added_v1` 的 `operator_id.user_id` 依赖 `contact:user.employee_id:readonly`；确认助理应用开了这个权限，否则只退群、不发说明。
3. **私信拉群的人。** 拉群的人如果不在应用的可用范围内，`notify_user` 预期失败（230013 一类），当前只记日志。确认这是可接受的表现。
4. **mypy 对「模块作协议实现」的支持。** Task 3 把 `app_leases` 模块直接当 `LeaseStore` 传入。若项目的 mypy 版本或配置不接受，在 `app_leases.py` 之外加一个与 `BotLeases` 同形的薄适配类即可，接口不变。
