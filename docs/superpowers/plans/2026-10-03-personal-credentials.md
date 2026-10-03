# 个人凭证 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** agent 需要账号、密码或 API Key 时，经飞书卡片表单或企业微信 H5 页面向当前用户本人索取；值按（AI 员工, 用户）加密保存，只在本人触发的轮次注入 CLI 环境变量，提交过程不经过聊天和模型上下文。

**Architecture:**
- 新包 `coreman/core/personal_credentials/` 分四个模块：
  - `policy`：校验规则、AAD、本轮令牌；
  - `store`：加密读写与注入；
  - `cards`：卡片与文案；
  - `service`：索取、提交、续接、过期。
- 两条提交路径汇入 `service.submit()`：
  - 飞书：网关在落库前把 `form_value` 加密封存，worker 快车道解封后提交；
  - 企业微信：H5 页面登录后提交。
- 注入复用每轮重建 env 的位置：对话在 `opening.py`，定时任务在 `cron_handler.py`。
- 提交后排独立任务类型 `credential_resume` 续接原会话。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2（async）、Alembic、PostgreSQL、pytest（asyncio_mode=auto）；Vue 3 + Element Plus + vue-i18n + vitest。

**Spec:** `docs/superpowers/specs/2026-10-03-personal-credentials.md`（执行者必须先读完）。

## Global Constraints

- **公开仓**：代码、测试、文档、提交信息不得出现公司名、内部域名、内部系统名、本机路径。示例只用 `DEMO_*`、`example.com`、`example.test`。
- **明文**：值只在三处出现——网关封存那一刻、`submit()` 加密前、开轮注入时。任何 API、日志、审计、任务载荷、入站事件都不得出现明文。
- **AAD**：
  - 凭证：`personal_credentials.value_enc:{bot_id}:{user_id}:{env_key}`；
  - 飞书封存副本：`credential_requests.sealed:{request_id}`；
  - 本轮令牌：`personal_credentials.capability.v1`。
- **环境变量**：本轮下发的变量前缀为 `COREMAN_CREDENTIAL_`，即 `COREMAN_CREDENTIAL_URL`、`COREMAN_CREDENTIAL_TOKEN`。
- **键名**：符合 `^[A-Z][A-Z0-9_]{0,63}$`；不以 `COREMAN_` 开头；不命中 `is_reserved_key`、`is_blocked_env_key`。
- **请求字段**：单次 1–20 个；`label` ≤ 50 字，`placeholder` ≤ 100 字，`purpose` ≤ 300 字。不限凭证总数，不限索取频率。
- **值**：
  - 去首尾空白；
  - 长度 1–4096，飞书路径上限 1000；
  - 不允许换行和控制字符（`[\x00-\x1f\x7f]`）。
- **请求有效期**：1 小时。
- **令牌有效期**：`bot.sse_timeout_seconds + 300` 秒，且只在签发它的任务仍是 `claimed`/`running` 时有效。
- **出站脱敏**：`secret` 字段的值强制加入 `ctx.secrets`，长度阈值 6。
- **注入范围**：

  | 轮次 | 注入谁 |
  |---|---|
  | 私聊或群聊中已验证的本人发言 | 发言者 |
  | `credential_resume` 续接轮 | 发起人 |
  | 定时任务 | 执行人 `actor` |
  | 带 `collaboration_id`、`collaboration_phase`、`human_collaboration_id` 的轮次 | 不注入 |

- **飞书卡片 `task_id`**：`credential@<request uuid>`。
- **固定安全说明**（6.3 节原文，`{bot}` 换成 AI 员工名）：「🔒 安全说明：此表单的内容直接提交给 CoreMan 加密保存，不经过聊天，不会发送给 AI 模型。只有你本人与「{bot}」对话、或运行你创建的定时任务时才会使用。请勿在此填写飞书、企业微信或邮箱的登录密码。」
- **迁移**：只新增表，不改旧表；revision `0053`，down_revision `0052`。
- **命令**：
  - 测试 `uv run pytest <path> -q`，需要 Docker 或 `TEST_DATABASE_URL`；
  - 检查 `uv run ruff check .`、`uv run mypy coreman`；
  - 前端在 `web/` 下执行 `npm run lint && npx vitest run && npm run build`。
- 每个任务结束提交一次，提交信息用英文 conventional commits，结尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。

## File Structure

| 文件 | 职责 |
|---|---|
| `coreman/core/db/models/personal_credentials.py`（新） | `PersonalCredential`、`CredentialRequest` 两个模型 |
| `migrations/versions/0053_personal_credentials.py`（新） | 建两张表 |
| `coreman/core/personal_credentials/policy.py`（新） | 常量、`CredentialError`、AAD、键名与值校验、请求体解析、本轮令牌 |
| `coreman/core/personal_credentials/store.py`（新） | 加密写入、按（员工, 用户）注入、本人列表、更新、删除 |
| `coreman/core/personal_credentials/cards.py`（新） | 飞书表单卡与结果卡、企微链接文案、续接文本 |
| `coreman/core/personal_credentials/service.py`（新） | `open_request`、`submit`、`cancel`、`expire_due`、`cleanup` |
| `coreman/core/prompting/env_vars.py`（改） | 保留前缀加入 `COREMAN_CREDENTIAL_` |
| `coreman/api/routers/personal_credentials.py`（新） | 本轮索取接口与 `/api/me/*` 接口 |
| `coreman/runtime/gateway_feishu/inbound.py`、`child.py`（改） | 放行 `credential@`，落库前封存 |
| `coreman/runtime/worker/credential_cards.py`（新）、`card_actions.py`（改） | 卡片提交分支与封存副本擦除 |
| `coreman/runtime/worker/chat/credentials.py`（新）、`chat/opening.py`（改） | 对话轮注入、令牌、提示词 |
| `coreman/runtime/worker/credential_resume.py`（新）、`__main__.py`（改） | 续接轮处理器 |
| `coreman/runtime/worker/cron_handler.py`（改） | 定时任务注入 |
| `coreman/runtime/scheduler/reaper.py`（改） | 过期与保留期清理 |
| `web/src/api/personalCredentials.ts`、`web/src/views/MyCredentialsView.vue`、`web/src/views/CredentialRequestView.vue`（新） | 前端 |
| `web/src/router/index.ts`、`web/src/layouts/AdminLayout.vue`、`web/src/i18n/{zh,en,ja}.ts`（改） | 路由、菜单、文案 |
| `docs/features/personal-credentials.md`（新）、`docs/glossary.md`、`CHANGELOG.md`、`README.md`（改） | 文档 |

---

### Task 1: 数据模型与迁移 0053

**Files:**
- Create: `coreman/core/db/models/personal_credentials.py`
- Modify: `coreman/core/db/models/__init__.py`（末尾追加导出）
- Create: `migrations/versions/0053_personal_credentials.py`
- Modify: `tests/conftest.py`（`BUSINESS_TABLES` 开头加两张表）
- Test: `tests/integration/test_personal_credentials_models.py`

**Interfaces:**
- Produces: `PersonalCredential`、`CredentialRequest`、`CREDENTIAL_REQUEST_STATUSES = ("open", "submitted", "expired", "cancelled")`、`CREDENTIAL_ORIGINS = ("chat", "cron")`，都从 `coreman.core.db.models` 导出。

- [ ] **Step 1: 写失败测试**

```python
"""个人凭证两张表：唯一约束、默认值与级联删除。"""

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from coreman.core.db.models import CredentialRequest, PersonalCredential, User
from coreman.core.timeutils import utcnow
from tests.integration.worker_helpers import seed_bot


async def _owner(session):
    bot, _, _ = await seed_bot(session)
    user = User(login_name="owner", display_name="本人", source="sync")
    session.add(user)
    await session.flush()
    return bot, user


async def test_one_value_per_bot_user_key(db_session):
    bot, user = await _owner(db_session)
    db_session.add(
        PersonalCredential(bot_id=bot.id, user_id=user.id, env_key="DEMO_API_KEY", value_enc="a")
    )
    await db_session.commit()
    db_session.add(
        PersonalCredential(bot_id=bot.id, user_id=user.id, env_key="DEMO_API_KEY", value_enc="b")
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_request_defaults_and_user_cascade(db_session):
    bot, user = await _owner(db_session)
    db_session.add(
        CredentialRequest(
            bot_id=bot.id,
            user_id=user.id,
            origin_kind="chat",
            origin_chat_id="oc_private",
            origin_chat_type="single",
            expires_at=utcnow(),
        )
    )
    db_session.add(
        PersonalCredential(bot_id=bot.id, user_id=user.id, env_key="DEMO_PIN", value_enc="x")
    )
    await db_session.commit()
    row = await db_session.scalar(select(CredentialRequest))
    assert row.status == "open" and row.fields == [] and row.purpose == ""
    cred = await db_session.scalar(select(PersonalCredential))
    assert cred.secret is True and cred.label == "" and cred.last_used_at is None
    await db_session.execute(delete(User).where(User.id == user.id))
    await db_session.commit()
    assert await db_session.scalar(select(CredentialRequest)) is None
    assert await db_session.scalar(select(PersonalCredential)) is None
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/integration/test_personal_credentials_models.py -q`
Expected: FAIL，`ImportError: cannot import name 'CredentialRequest'`

- [ ] **Step 3: 写模型**

`coreman/core/db/models/personal_credentials.py`：

```python
"""个人凭证：按（AI 员工, 用户）逐条加密保存的环境变量，以及一次向本人索取的请求。

值只以密文存在（AAD 绑定行身份，见 core/personal_credentials/policy.py）；请求里只有键名、
标签与用途，从不存值。来源任务与入站事件不建外键：它们按自己的保留期清理，不能被这里挡住。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, TimestampMixin, enum_check

CREDENTIAL_REQUEST_STATUSES = ("open", "submitted", "expired", "cancelled")
CREDENTIAL_ORIGINS = ("chat", "cron")


class PersonalCredential(TimestampMixin, Base):
    __tablename__ = "personal_credentials"
    __table_args__ = (
        UniqueConstraint("bot_id", "user_id", "env_key", name="uq_personal_credentials_bot_id"),
        Index("personal_credentials_user_idx", "user_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    env_key: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text, server_default=text("''"))
    # false 的字段（例如账号）在「我的凭证」页显示原值；true 的永不显示。
    secret: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    value_enc: Mapped[str] = mapped_column(Text)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CredentialRequest(TimestampMixin, Base):
    __tablename__ = "credential_requests"
    __table_args__ = (
        CheckConstraint(enum_check("status", CREDENTIAL_REQUEST_STATUSES), name="status"),
        CheckConstraint(enum_check("origin_kind", CREDENTIAL_ORIGINS), name="origin_kind"),
        Index("credential_requests_owner_idx", "bot_id", "user_id"),
        Index(
            "credential_requests_open_idx",
            "expires_at",
            postgresql_where=text("status = 'open'"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    origin_kind: Mapped[str] = mapped_column(Text)
    origin_task_id: Mapped[int | None] = mapped_column(BigInteger)
    origin_event_id: Mapped[int | None] = mapped_column(BigInteger)
    origin_chat_id: Mapped[str] = mapped_column(Text)
    origin_chat_type: Mapped[str] = mapped_column(Text)
    origin_session_key: Mapped[str | None] = mapped_column(Text)
    cron_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # 表单或链接实际发到的会话；「已保存」等通知也发到这里。
    delivery_chat_id: Mapped[str | None] = mapped_column(Text)
    # [{key, label, secret, placeholder}]，不含任何值。
    fields: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    purpose: Mapped[str] = mapped_column(Text, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_outbox_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("outbox.id", ondelete="SET NULL")
    )
    resume_task_id: Mapped[int | None] = mapped_column(BigInteger)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

在 `coreman/core/db/models/__init__.py` 末尾追加：

```python
from coreman.core.db.models.personal_credentials import (
    CREDENTIAL_ORIGINS,
    CREDENTIAL_REQUEST_STATUSES,
    CredentialRequest,
    PersonalCredential,
)

__all__ += [
    "CREDENTIAL_ORIGINS",
    "CREDENTIAL_REQUEST_STATUSES",
    "CredentialRequest",
    "PersonalCredential",
]
```

在 `coreman/core/db/models/bus.py` 的 `TASK_KINDS` 里加 `"credential_resume"`（放在 `"choice_submit"` 之后）。`tasks.kind` 没有检查约束，这个常量只用作登记。

- [ ] **Step 4: 写迁移**

`migrations/versions/0053_personal_credentials.py`：

```python
"""Per-member credentials an agent asks for through a secure form, and the requests themselves.

New tables only: the previous version never reads or writes them during a rolling upgrade.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None

STATUSES = ("open", "submitted", "expired", "cancelled")
ORIGINS = ("chat", "cron")


def _check(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _timestamps():
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade():
    op.create_table(
        "personal_credentials",
        sa.Column(
            "id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("env_key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("secret", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("value_enc", sa.Text(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.UniqueConstraint(
            "bot_id", "user_id", "env_key", name="uq_personal_credentials_bot_id"
        ),
    )
    op.create_index("personal_credentials_user_idx", "personal_credentials", ["user_id"])
    op.create_table(
        "credential_requests",
        sa.Column(
            "id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("origin_kind", sa.Text(), nullable=False),
        sa.Column("origin_task_id", sa.BigInteger()),
        sa.Column("origin_event_id", sa.BigInteger()),
        sa.Column("origin_chat_id", sa.Text(), nullable=False),
        sa.Column("origin_chat_type", sa.Text(), nullable=False),
        sa.Column("origin_session_key", sa.Text()),
        sa.Column("cron_job_id", sa.Uuid()),
        sa.Column("delivery_chat_id", sa.Text()),
        sa.Column("fields", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("purpose", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "request_outbox_id",
            sa.BigInteger(),
            sa.ForeignKey("outbox.id", ondelete="SET NULL"),
        ),
        sa.Column("resume_task_id", sa.BigInteger()),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint(_check("status", STATUSES), name="status"),
        sa.CheckConstraint(_check("origin_kind", ORIGINS), name="origin_kind"),
    )
    op.create_index(
        "credential_requests_owner_idx", "credential_requests", ["bot_id", "user_id"]
    )
    op.create_index(
        "credential_requests_open_idx",
        "credential_requests",
        ["expires_at"],
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade():
    op.drop_table("credential_requests")
    op.drop_table("personal_credentials")
```

不加 `set_updated_at` 触发器：注入时会更新 `last_used_at`，触发器会把「更新时间」改成「使用时间」；`updated_at` 由写入方显式设置（Task 3）。

在 `tests/conftest.py` 的 `BUSINESS_TABLES` 列表开头插入 `"credential_requests",` 和 `"personal_credentials",`。

- [ ] **Step 5: 运行测试与模型/迁移一致性检查**

Run: `uv run pytest tests/integration/test_personal_credentials_models.py tests/integration/test_migrations.py tests/unit/test_migration_rules.py -q`
Expected: PASS。`test_models_match_migrations` 报差异时，以模型为准逐项对齐迁移的 server_default 和约束名。

- [ ] **Step 6: Commit**

```bash
git add coreman/core/db/models/personal_credentials.py coreman/core/db/models/__init__.py coreman/core/db/models/bus.py migrations/versions/0053_personal_credentials.py tests/conftest.py tests/integration/test_personal_credentials_models.py
git commit -m "feat(credentials): add personal credential and request tables"
```

---

### Task 2: 校验规则、AAD 与本轮令牌（`policy`）

**Files:**
- Create: `coreman/core/personal_credentials/__init__.py`
- Create: `coreman/core/personal_credentials/policy.py`
- Modify: `coreman/core/prompting/env_vars.py`（`is_reserved_key` 的前缀元组）
- Test: `tests/unit/test_personal_credentials_policy.py`

**Interfaces:**
- Produces（全部在 `coreman.core.personal_credentials.policy`）：
  - 常量：`ENV_PREFIX = "COREMAN_CREDENTIAL_"`、`CARD_PREFIX = "credential@"`、`REQUEST_TTL: timedelta`、`CAPABILITY_GRACE = 300`、`MAX_FIELDS = 20`、`MAX_VALUE = 4096`、`FEISHU_MAX_VALUE = 1000`、`MIN_REDACT = 6`
  - `class CredentialError(ValueError)`：属性 `code: str`、`message: str`
  - AAD：`value_aad(bot_id, user_id, env_key) -> str`、`sealed_aad(request_id) -> str`
  - 校验：`key_problem(key: str) -> str | None`、`parse_request(body: Any) -> RequestBody`（字段为 `fields: list[FieldSpec]`、`purpose: str`）、`clean_values(fields: list[dict], values: Any, *, limit: int = MAX_VALUE) -> dict[str, str]`
  - 卡片 ID：`card_task_id(request_id) -> str`、`parse_card_task_id(task_id: str) -> uuid.UUID | None`
  - 令牌：`@dataclass Capability(task_id, bot_id, user_id, origin_kind, chat_id, chat_type, session_key, event_id, cron_job_id)`、`issue_capability(cipher, cap, *, ttl_seconds: int) -> str`、`read_capability(cipher, token) -> Capability`

- [ ] **Step 1: 写失败测试**

```python
"""个人凭证的校验规则：键名、请求体、提交值、卡片 ID 与本轮令牌。"""

import uuid

import pytest

from coreman.core.crypto import Cipher
from coreman.core.personal_credentials import policy
from coreman.core.prompting.env_vars import is_reserved_key

CIPHER = Cipher(b"\x07" * 32)


@pytest.mark.parametrize(
    "key,ok",
    [
        ("DEMO_API_KEY", True),
        ("demo_key", False),
        ("1KEY", False),
        ("COREMAN_ANYTHING", False),
        ("BOT_TOKEN_DEMO", False),
        ("PATH", False),
        ("ANTHROPIC_BASE_URL", False),
        ("A" * 65, False),
    ],
)
def test_key_rules(key, ok):
    assert (policy.key_problem(key) is None) is ok


def test_credential_prefix_is_reserved():
    assert is_reserved_key("COREMAN_CREDENTIAL_TOKEN")


def test_parse_request_accepts_agent_choice_and_rejects_bad_shapes():
    body = policy.parse_request(
        {
            "fields": [
                {"key": "DEMO_USERNAME", "label": "账号", "secret": False},
                {"key": "DEMO_PASSWORD", "label": "密码"},
            ],
            "purpose": "登录 Demo 系统",
        }
    )
    assert [f.secret for f in body.fields] == [False, True]
    with pytest.raises(policy.CredentialError) as dup:
        policy.parse_request(
            {"fields": [{"key": "A_B", "label": "x"}] * 2, "purpose": "p"}
        )
    assert dup.value.code == "invalid_fields"
    with pytest.raises(policy.CredentialError):
        policy.parse_request({"fields": [{"key": "PATH", "label": "x"}], "purpose": "p"})
    with pytest.raises(policy.CredentialError):
        policy.parse_request({"fields": [], "purpose": "p"})
    with pytest.raises(policy.CredentialError):
        policy.parse_request(
            {"fields": [{"key": f"K_{i}", "label": "x"} for i in range(21)], "purpose": "p"}
        )


def test_clean_values_requires_every_field_single_line_within_limit():
    fields = [{"key": "DEMO_PIN", "label": "PIN"}]
    assert policy.clean_values(fields, {"DEMO_PIN": "  abc123 ", "EXTRA": "x"}) == {
        "DEMO_PIN": "abc123"
    }
    for bad in ({}, {"DEMO_PIN": "  "}, {"DEMO_PIN": "a\nb"}, {"DEMO_PIN": "x" * 1001}):
        with pytest.raises(policy.CredentialError) as exc:
            policy.clean_values(fields, bad, limit=1000)
        assert exc.value.code == "invalid_values"


def test_card_task_id_round_trip():
    rid = uuid.uuid4()
    assert policy.parse_card_task_id(policy.card_task_id(rid)) == rid
    assert policy.parse_card_task_id("credential@not-a-uuid") is None
    assert policy.parse_card_task_id("personal:1") is None


def test_capability_round_trip_and_expiry():
    cap = policy.Capability(
        task_id=7,
        bot_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        origin_kind="chat",
        chat_id="oc_private",
        chat_type="single",
        session_key="oc_private",
        event_id=11,
        cron_job_id=None,
    )
    token = policy.issue_capability(CIPHER, cap, ttl_seconds=60)
    assert policy.read_capability(CIPHER, token) == cap
    expired = policy.issue_capability(CIPHER, cap, ttl_seconds=-1)
    with pytest.raises(ValueError):
        policy.read_capability(CIPHER, expired)


def test_value_aad_binds_row_identity():
    bot, user = uuid.uuid4(), uuid.uuid4()
    token = CIPHER.encrypt("secret-value", policy.value_aad(bot, user, "DEMO_PIN"))
    assert CIPHER.decrypt(token, policy.value_aad(bot, user, "DEMO_PIN")) == "secret-value"
    with pytest.raises(ValueError):
        CIPHER.decrypt(token, policy.value_aad(bot, uuid.uuid4(), "DEMO_PIN"))
    with pytest.raises(ValueError):
        CIPHER.decrypt(token, policy.value_aad(bot, user, "OTHER_KEY"))
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/unit/test_personal_credentials_policy.py -q`
Expected: FAIL，`ModuleNotFoundError: coreman.core.personal_credentials`

- [ ] **Step 3: 实现**

`coreman/core/personal_credentials/__init__.py`：

```python
"""个人凭证：agent 经安全表单向本人索取、按（AI 员工, 用户）加密保存、只在本人触发的轮次注入。"""
```

`coreman/core/personal_credentials/policy.py`：

```python
"""个人凭证的校验规则、加密 AAD 与本轮索取令牌。

索取哪些凭证由 agent 按场景决定，这里只保留技术上必须的约束：键名能当环境变量用、
不覆盖平台保留与控制类变量；值是单行、有长度上限。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from coreman.core.bots.env_policy import is_blocked_env_key
from coreman.core.bots.secrets import ENV_KEY_RE
from coreman.core.crypto import Cipher
from coreman.core.prompting.env_vars import is_reserved_key

ENV_PREFIX = "COREMAN_CREDENTIAL_"
CARD_PREFIX = "credential@"
CAPABILITY_AAD = "personal_credentials.capability.v1"
CAPABILITY_GRACE = 300
REQUEST_TTL = timedelta(hours=1)
MAX_FIELDS = 20
MAX_VALUE = 4096
FEISHU_MAX_VALUE = 1000
MIN_REDACT = 6
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class CredentialError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def value_aad(bot_id: uuid.UUID | str, user_id: uuid.UUID | str, env_key: str) -> str:
    return f"personal_credentials.value_enc:{bot_id}:{user_id}:{env_key}"


def sealed_aad(request_id: uuid.UUID | str) -> str:
    return f"credential_requests.sealed:{request_id}"


def key_problem(key: str) -> str | None:
    if not ENV_KEY_RE.fullmatch(key):
        return "变量名只能用大写字母、数字和下划线，以字母开头，最长 64 位"
    if key.startswith("COREMAN_"):
        return "COREMAN_ 开头的变量由平台保留"
    if is_reserved_key(key):
        return "这是平台按发言者生成的保留变量"
    if is_blocked_env_key(key):
        return "这个变量会改变运行时行为，不允许设置"
    return None


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(max_length=64)
    label: str = Field(min_length=1, max_length=50)
    secret: bool = True
    placeholder: str = Field(default="", max_length=100)


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[FieldSpec] = Field(min_length=1, max_length=MAX_FIELDS)
    purpose: str = Field(min_length=1, max_length=300)


def parse_request(body: Any) -> RequestBody:
    try:
        parsed = RequestBody.model_validate(body)
    except ValidationError as exc:
        names = sorted({".".join(str(p) for p in err["loc"]) or "body" for err in exc.errors()})
        raise CredentialError("invalid_fields", "参数无效：" + "、".join(names)) from None
    problems = [f"{f.key}：{p}" for f in parsed.fields if (p := key_problem(f.key))]
    keys = [f.key for f in parsed.fields]
    if len(set(keys)) != len(keys):
        problems.append("同一请求里的变量名不能重复")
    if problems:
        raise CredentialError("invalid_fields", "；".join(problems))
    return parsed


def clean_values(
    fields: list[dict[str, Any]], values: Any, *, limit: int = MAX_VALUE
) -> dict[str, str]:
    """按请求的字段取值：每个都必填、去首尾空白、单行、长度 1–limit；多出来的键忽略。"""
    if not isinstance(values, dict):
        raise CredentialError("invalid_values", "提交内容格式不对")
    out: dict[str, str] = {}
    for field in fields:
        key = str(field["key"])
        label = str(field.get("label") or key)
        raw = values.get(key)
        value = raw.strip() if isinstance(raw, str) else ""
        if not value:
            raise CredentialError("invalid_values", f"「{label}」不能为空")
        if len(value) > limit:
            raise CredentialError("invalid_values", f"「{label}」超过 {limit} 个字符")
        if _CONTROL.search(value):
            raise CredentialError("invalid_values", f"「{label}」不能包含换行或控制字符")
        out[key] = value
    return out


def card_task_id(request_id: uuid.UUID | str) -> str:
    return f"{CARD_PREFIX}{request_id}"


def parse_card_task_id(task_id: str) -> uuid.UUID | None:
    if not task_id.startswith(CARD_PREFIX):
        return None
    try:
        return uuid.UUID(task_id[len(CARD_PREFIX) :])
    except ValueError:
        return None


@dataclass(frozen=True)
class Capability:
    """本轮令牌指向的一切：哪一轮、哪个员工、谁的凭证、表单发回哪里。"""

    task_id: int
    bot_id: uuid.UUID
    user_id: uuid.UUID
    origin_kind: str
    chat_id: str
    chat_type: str
    session_key: str | None
    event_id: int | None
    cron_job_id: uuid.UUID | None


def issue_capability(cipher: Cipher, cap: Capability, *, ttl_seconds: int) -> str:
    claims = {
        "task": cap.task_id,
        "bot": str(cap.bot_id),
        "user": str(cap.user_id),
        "origin": cap.origin_kind,
        "chat": cap.chat_id,
        "chat_type": cap.chat_type,
        "session_key": cap.session_key,
        "event": cap.event_id,
        "cron": str(cap.cron_job_id) if cap.cron_job_id else None,
        "exp": time.time() + ttl_seconds,
    }
    return cipher.encrypt(json.dumps(claims), CAPABILITY_AAD)


def read_capability(cipher: Cipher, token: str) -> Capability:
    data = json.loads(cipher.decrypt(token, CAPABILITY_AAD))
    if not isinstance(data, dict) or float(data["exp"]) <= time.time():
        raise ValueError("expired_capability")
    return Capability(
        task_id=int(data["task"]),
        bot_id=uuid.UUID(data["bot"]),
        user_id=uuid.UUID(data["user"]),
        origin_kind=str(data["origin"]),
        chat_id=str(data["chat"]),
        chat_type=str(data["chat_type"]),
        session_key=data.get("session_key"),
        event_id=int(data["event"]) if data.get("event") is not None else None,
        cron_job_id=uuid.UUID(data["cron"]) if data.get("cron") else None,
    )
```

`coreman/core/prompting/env_vars.py` 的 `is_reserved_key` 前缀元组末尾加 `"COREMAN_CREDENTIAL_",`（写字面量，不要从 policy 导入，避免循环导入）。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest tests/unit/test_personal_credentials_policy.py tests/unit/test_env_vars.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add coreman/core/personal_credentials coreman/core/prompting/env_vars.py tests/unit/test_personal_credentials_policy.py
git commit -m "feat(credentials): add validation rules and per-turn capability"
```

---

### Task 3: 加密读写与注入（`store`）

**Files:**
- Create: `coreman/core/personal_credentials/store.py`
- Test: `tests/integration/test_personal_credentials_store.py`

**Interfaces:**
- Consumes: Task 1 模型；Task 2 的 `value_aad`、`key_problem`、`MIN_REDACT`
- Produces（`coreman.core.personal_credentials.store`）：
  - `save(session, cipher, *, bot_id, user_id, fields: list[dict], values: dict[str, str]) -> list[str]`：返回排好序的键名
  - `@dataclass(frozen=True) Injected(env: dict[str, str], secret_values: frozenset[str], names: tuple[str, ...])`
  - `injected(session, cipher, *, bot_id, user_id) -> Injected`
  - `list_own(session, user_id) -> list[tuple[PersonalCredential, Bot]]`
  - `plain_value(cipher, row) -> str | None`：只对 `secret=False` 返回原值
  - `update_value(session, cipher, *, bot_id, user_id, env_key, value) -> PersonalCredential | None`
  - `delete(session, *, bot_id, user_id, env_key) -> bool`

- [ ] **Step 1: 写失败测试**

```python
"""个人凭证的存取：逐条加密、覆盖写、注入过滤与使用时间。"""

from datetime import timedelta

from sqlalchemy import select, update

from coreman.core.crypto import Cipher
from coreman.core.db.models import PersonalCredential, User
from coreman.core.personal_credentials import store
from coreman.core.timeutils import utcnow
from tests.integration.worker_helpers import MASTER, seed_bot

CIPHER = Cipher(MASTER)
FIELDS = [
    {"key": "DEMO_USERNAME", "label": "账号", "secret": False},
    {"key": "DEMO_PIN", "label": "PIN", "secret": True},
]


async def _owner(session):
    bot, _, _ = await seed_bot(session)
    user = User(login_name="owner", display_name="本人", source="sync")
    session.add(user)
    await session.flush()
    return bot, user


async def test_save_encrypts_each_value_and_overwrites(db_session):
    bot, user = await _owner(db_session)
    keys = await store.save(
        db_session,
        CIPHER,
        bot_id=bot.id,
        user_id=user.id,
        fields=FIELDS,
        values={"DEMO_USERNAME": "alice", "DEMO_PIN": "pin-778899"},
    )
    await db_session.commit()
    assert keys == ["DEMO_PIN", "DEMO_USERNAME"]
    rows = (await db_session.scalars(select(PersonalCredential))).all()
    assert all("pin-778899" not in r.value_enc and "alice" not in r.value_enc for r in rows)
    await store.save(
        db_session,
        CIPHER,
        bot_id=bot.id,
        user_id=user.id,
        fields=FIELDS[1:],
        values={"DEMO_PIN": "pin-000111"},
    )
    await db_session.commit()
    found = await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    assert found.env == {"DEMO_PIN": "pin-000111", "DEMO_USERNAME": "alice"}
    assert found.names == ("DEMO_PIN", "DEMO_USERNAME")
    # 只有 secret 字段进脱敏集合；账号不进。
    assert found.secret_values == frozenset({"pin-000111"})


async def test_injection_is_scoped_and_skips_bad_rows(db_session):
    bot, user = await _owner(db_session)
    other = User(login_name="other", display_name="别人", source="sync")
    db_session.add(other)
    await db_session.flush()
    await store.save(
        db_session, CIPHER, bot_id=bot.id, user_id=other.id,
        fields=FIELDS[1:], values={"DEMO_PIN": "pin-of-other"},
    )
    # 被挪过来的密文（AAD 不符）与策略收紧后不再允许的键名都跳过。
    moved = (await db_session.scalars(select(PersonalCredential))).one()
    db_session.add(
        PersonalCredential(bot_id=bot.id, user_id=user.id, env_key="DEMO_PIN", value_enc=moved.value_enc)
    )
    db_session.add(
        PersonalCredential(
            bot_id=bot.id, user_id=user.id, env_key="PATH",
            value_enc=CIPHER.encrypt("/tmp", f"personal_credentials.value_enc:{bot.id}:{user.id}:PATH"),
        )
    )
    await db_session.commit()
    found = await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    assert found.env == {} and found.names == ()


async def test_last_used_is_throttled_and_does_not_touch_updated_at(db_session):
    bot, user = await _owner(db_session)
    await store.save(
        db_session, CIPHER, bot_id=bot.id, user_id=user.id,
        fields=FIELDS[1:], values={"DEMO_PIN": "pin-778899"},
    )
    await db_session.commit()
    row = await db_session.scalar(select(PersonalCredential))
    updated = row.updated_at
    await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    await db_session.commit()
    await db_session.refresh(row)
    first = row.last_used_at
    assert first is not None and row.updated_at == updated
    await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    await db_session.commit()
    await db_session.refresh(row)
    assert row.last_used_at == first
    await db_session.execute(
        update(PersonalCredential).values(last_used_at=utcnow() - timedelta(hours=2))
    )
    await db_session.commit()
    await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    await db_session.commit()
    await db_session.refresh(row)
    assert row.last_used_at >= first


async def test_list_update_delete_own(db_session):
    bot, user = await _owner(db_session)
    await store.save(
        db_session, CIPHER, bot_id=bot.id, user_id=user.id,
        fields=FIELDS, values={"DEMO_USERNAME": "alice", "DEMO_PIN": "pin-778899"},
    )
    await db_session.commit()
    rows = await store.list_own(db_session, user.id)
    assert [(r.env_key, b.id) for r, b in rows] == [("DEMO_PIN", bot.id), ("DEMO_USERNAME", bot.id)]
    assert [store.plain_value(CIPHER, r) for r, _ in rows] == [None, "alice"]
    row = await store.update_value(
        db_session, CIPHER, bot_id=bot.id, user_id=user.id, env_key="DEMO_PIN", value="pin-222333"
    )
    assert row is not None
    assert await store.update_value(
        db_session, CIPHER, bot_id=bot.id, user_id=user.id, env_key="MISSING", value="x"
    ) is None
    assert await store.delete(db_session, bot_id=bot.id, user_id=user.id, env_key="DEMO_USERNAME")
    assert not await store.delete(db_session, bot_id=bot.id, user_id=user.id, env_key="DEMO_USERNAME")
    await db_session.commit()
    found = await store.injected(db_session, CIPHER, bot_id=bot.id, user_id=user.id)
    assert found.env == {"DEMO_PIN": "pin-222333"}
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/integration/test_personal_credentials_store.py -q`
Expected: FAIL，`ImportError: cannot import name 'store'`

- [ ] **Step 3: 实现**

```python
"""个人凭证的读写：逐条加密，AAD 绑定（AI 员工, 用户, 变量名）。只有开轮注入会解密明文。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher, DecryptError
from coreman.core.db.models import Bot, PersonalCredential
from coreman.core.logging import get_logger
from coreman.core.personal_credentials.policy import MIN_REDACT, key_problem, value_aad
from coreman.core.timeutils import utcnow

log = get_logger(__name__)
LAST_USED_EVERY = timedelta(hours=1)


@dataclass(frozen=True)
class Injected:
    env: dict[str, str]
    secret_values: frozenset[str]
    names: tuple[str, ...]


async def save(
    session: AsyncSession,
    cipher: Cipher,
    *,
    bot_id: uuid.UUID,
    user_id: uuid.UUID,
    fields: list[dict[str, Any]],
    values: dict[str, str],
) -> list[str]:
    for field in fields:
        key = str(field["key"])
        enc = cipher.encrypt(values[key], value_aad(bot_id, user_id, key))
        label, secret = str(field.get("label") or ""), bool(field.get("secret", True))
        stmt = (
            insert(PersonalCredential)
            .values(
                bot_id=bot_id, user_id=user_id, env_key=key, label=label, secret=secret,
                value_enc=enc,
            )
            .on_conflict_do_update(
                index_elements=[
                    PersonalCredential.bot_id,
                    PersonalCredential.user_id,
                    PersonalCredential.env_key,
                ],
                set_={"label": label, "secret": secret, "value_enc": enc, "updated_at": func.now()},
            )
        )
        await session.execute(stmt)
    return sorted(str(f["key"]) for f in fields)


async def injected(
    session: AsyncSession, cipher: Cipher, *, bot_id: uuid.UUID, user_id: uuid.UUID
) -> Injected:
    rows = (
        await session.scalars(
            select(PersonalCredential)
            .where(PersonalCredential.bot_id == bot_id, PersonalCredential.user_id == user_id)
            .order_by(PersonalCredential.env_key)
        )
    ).all()
    now = utcnow()
    env: dict[str, str] = {}
    secrets: set[str] = set()
    used: list[uuid.UUID] = []
    for row in rows:
        # 策略收紧后的存量键名不越界。
        if key_problem(row.env_key):
            continue
        try:
            value = cipher.decrypt(row.value_enc, value_aad(bot_id, user_id, row.env_key))
        except DecryptError:
            log.warning("personal_credential_unreadable", env_key=row.env_key)
            continue
        env[row.env_key] = value
        if row.secret and len(value) >= MIN_REDACT:
            secrets.add(value)
        if row.last_used_at is None or row.last_used_at < now - LAST_USED_EVERY:
            used.append(row.id)
    if used:
        await session.execute(
            update(PersonalCredential)
            .where(PersonalCredential.id.in_(used))
            .values(last_used_at=now)
            .execution_options(synchronize_session=False)
        )
    return Injected(env, frozenset(secrets), tuple(env))


async def list_own(
    session: AsyncSession, user_id: uuid.UUID
) -> list[tuple[PersonalCredential, Bot]]:
    result = await session.execute(
        select(PersonalCredential, Bot)
        .join(Bot, Bot.id == PersonalCredential.bot_id)
        .where(PersonalCredential.user_id == user_id)
        .order_by(Bot.name, PersonalCredential.env_key)
    )
    return [(row, bot) for row, bot in result.all()]


def plain_value(cipher: Cipher, row: PersonalCredential) -> str | None:
    if row.secret:
        return None
    try:
        return cipher.decrypt(row.value_enc, value_aad(row.bot_id, row.user_id, row.env_key))
    except DecryptError:
        return None


async def update_value(
    session: AsyncSession,
    cipher: Cipher,
    *,
    bot_id: uuid.UUID,
    user_id: uuid.UUID,
    env_key: str,
    value: str,
) -> PersonalCredential | None:
    row = await session.scalar(
        select(PersonalCredential)
        .where(
            PersonalCredential.bot_id == bot_id,
            PersonalCredential.user_id == user_id,
            PersonalCredential.env_key == env_key,
        )
        .with_for_update()
    )
    if row is None:
        return None
    row.value_enc = cipher.encrypt(value, value_aad(bot_id, user_id, env_key))
    row.updated_at = utcnow()
    await session.flush()
    return row


async def delete(
    session: AsyncSession, *, bot_id: uuid.UUID, user_id: uuid.UUID, env_key: str
) -> bool:
    result = await session.execute(
        sql_delete(PersonalCredential)
        .where(
            PersonalCredential.bot_id == bot_id,
            PersonalCredential.user_id == user_id,
            PersonalCredential.env_key == env_key,
        )
        .returning(PersonalCredential.id)
    )
    return result.first() is not None
```

注意：`Injected.names` 用 `tuple(env)`，按 `env_key` 排序（查询已排序）。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest tests/integration/test_personal_credentials_store.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add coreman/core/personal_credentials/store.py tests/integration/test_personal_credentials_store.py
git commit -m "feat(credentials): store values encrypted per bot, user and key"
```

---

### Task 4: 卡片与文案（`cards`）

**Files:**
- Create: `coreman/core/personal_credentials/cards.py`
- Test: `tests/unit/test_personal_credentials_cards.py`

**Interfaces:**
- Consumes: Task 2 的 `card_task_id`、`FEISHU_MAX_VALUE`
- Produces（`coreman.core.personal_credentials.cards`）：
  - 常量：`GROUP_NOTICE: str`
  - `security_note(bot_name: str) -> str`
  - 飞书卡片：`form_card(request_id, *, bot_name, purpose, fields, web_url=None, mention_open_id=None) -> dict`、`saved_card(request_id, keys, tail) -> dict`、`expired_card(request_id) -> dict`、`failed_card(request_id, message) -> dict`
  - 文本：`wecom_link(*, bot_name, purpose, fields, url) -> str`、`resume_text(keys) -> str`

- [ ] **Step 1: 写失败测试**

```python
"""个人凭证卡片：外框和安全说明由系统固定，agent 只能填用途与标签，卡片里从不带值。"""

import json
import uuid

from coreman.core.personal_credentials import cards

RID = uuid.UUID("11111111-2222-3333-4444-555555555555")
FIELDS = [
    {"key": "DEMO_USERNAME", "label": "账号", "secret": False, "placeholder": ""},
    {"key": "DEMO_PIN", "label": "PIN", "secret": True, "placeholder": "6 位数字"},
]


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_form_card_shape():
    card = cards.form_card(RID, bot_name="Demo 助手", purpose="登录 Demo 系统", fields=FIELDS)
    assert card["schema"] == "2.0" and card["task_id"] == f"credential@{RID}"
    inputs = [n for n in _walk(card) if n.get("tag") == "input"]
    assert [i["name"] for i in inputs] == ["DEMO_USERNAME", "DEMO_PIN"]
    assert "input_type" not in inputs[0] and inputs[1]["input_type"] == "password"
    assert all(i["required"] and i["max_length"] == 1000 for i in inputs)
    [button] = [n for n in _walk(card) if n.get("tag") == "button"]
    assert button["form_action_type"] == "submit"
    assert button["behaviors"] == [{"type": "callback", "value": {"task_id": f"credential@{RID}"}}]
    text = json.dumps(card, ensure_ascii=False)
    assert "不会发送给 AI 模型" in text and "Demo 助手" in text and "登录 Demo 系统" in text
    assert "网页填写" not in text and "<at" not in text


def test_form_card_optional_parts():
    card = cards.form_card(
        RID, bot_name="B", purpose="p", fields=FIELDS,
        web_url="https://coreman.example.com/my-credentials/requests/x", mention_open_id="ou_owner",
    )
    text = json.dumps(card, ensure_ascii=False)
    assert "[用网页填写](https://coreman.example.com/my-credentials/requests/x)" in text
    assert "<at id=ou_owner></at>" in text


def test_result_cards_keep_task_id_and_carry_no_values():
    for card in (
        cards.saved_card(RID, ["DEMO_PIN"], "AI 员工会继续之前的任务。"),
        cards.expired_card(RID),
        cards.failed_card(RID, "「PIN」不能为空"),
    ):
        assert card["task_id"] == f"credential@{RID}"
        assert not [n for n in _walk(card) if n.get("tag") in {"input", "form", "button"}]


def test_wecom_link_flattens_markdown_from_agent():
    text = cards.wecom_link(
        bot_name="Demo", purpose="[点我](https://evil.example)", fields=FIELDS,
        url="https://coreman.example.com/my-credentials/requests/x",
    )
    assert "](https://evil.example)" not in text
    assert "[点这里安全填写](https://coreman.example.com/my-credentials/requests/x)" in text
    assert "账号、PIN" in text and "不会发送给 AI 模型" in text


def test_resume_text_names_keys_only():
    assert cards.resume_text(["DEMO_PIN"]).startswith("[CoreMan] 用户已通过安全表单提交 DEMO_PIN")
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/unit/test_personal_credentials_cards.py -q`
Expected: FAIL，`ImportError`

- [ ] **Step 3: 实现**

```python
"""个人凭证的卡片与文案。外框与安全说明由系统固定渲染；agent 只能提供用途与字段标签。"""

from __future__ import annotations

import re
import uuid
from typing import Any

from coreman.core.personal_credentials.policy import FEISHU_MAX_VALUE, card_task_id

SECURITY_NOTE = (
    "🔒 安全说明：此表单的内容直接提交给 CoreMan 加密保存，不经过聊天，不会发送给 AI 模型。"
    "只有你本人与「{bot}」对话、或运行你创建的定时任务时才会使用。"
    "请勿在此填写飞书、企业微信或邮箱的登录密码。"
)
GROUP_NOTICE = "已私信你一张安全表单，请在私聊里填写。"
# 企微链接文案是 Markdown：agent 写的用途与标签里的链接、强调符号一律压平，免得冒充入口。
_MARKDOWN = re.compile(r"[\[\]()<>`*_#|]")


def security_note(bot_name: str) -> str:
    return SECURITY_NOTE.format(bot=bot_name)


def _flat(text: str) -> str:
    return _MARKDOWN.sub(" ", text)


def _plain(content: str, **style: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "plain_text", "content": content, **style}}


def _label(field: dict[str, Any]) -> str:
    return str(field.get("label") or field["key"])


def form_card(
    request_id: uuid.UUID,
    *,
    bot_name: str,
    purpose: str,
    fields: list[dict[str, Any]],
    web_url: str | None = None,
    mention_open_id: str | None = None,
) -> dict[str, Any]:
    task_id = card_task_id(request_id)
    inputs: list[dict[str, Any]] = []
    for field in fields:
        element: dict[str, Any] = {
            "tag": "input",
            "name": str(field["key"]),
            "required": True,
            "max_length": FEISHU_MAX_VALUE,
            "label": {"tag": "plain_text", "content": _label(field)},
            "label_position": "top",
            "placeholder": {
                "tag": "plain_text",
                "content": str(field.get("placeholder") or f"请输入{_label(field)}"),
            },
        }
        if field.get("secret", True):
            element["input_type"] = "password"
        inputs.append(element)
    elements: list[dict[str, Any]] = []
    if mention_open_id:
        elements.append({"tag": "markdown", "content": f"<at id={mention_open_id}></at>"})
    elements.append(_plain("用途：" + purpose))
    elements.append(
        {
            "tag": "form",
            "name": "credential_form",
            "elements": [
                *inputs,
                {
                    "tag": "button",
                    "name": "submit",
                    "form_action_type": "submit",
                    "type": "primary",
                    "text": {"tag": "plain_text", "content": "加密提交"},
                    "behaviors": [{"type": "callback", "value": {"task_id": task_id}}],
                },
            ],
        }
    )
    if web_url:
        elements.append({"tag": "markdown", "content": f"内容较长？[用网页填写]({web_url})"})
    elements.append(
        _plain(security_note(bot_name) + "1 小时内有效。", text_size="notation", text_color="grey")
    )
    return {
        "schema": "2.0",
        "task_id": task_id,
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "🔒 需要你的个人凭证"},
            "subtitle": {"tag": "plain_text", "content": f"AI 员工「{bot_name}」"},
        },
        "body": {"elements": elements},
    }


def _result(request_id: uuid.UUID, title: str, lines: list[str], template: str) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "task_id": card_task_id(request_id),
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "body": {"elements": [_plain(line) for line in lines]},
    }


def saved_card(request_id: uuid.UUID, keys: list[str] | tuple[str, ...], tail: str) -> dict[str, Any]:
    return _result(request_id, "✅ 已保存", [f"已保存 {'、'.join(keys)}（内容不显示）。", tail], "green")


def expired_card(request_id: uuid.UUID) -> dict[str, Any]:
    return _result(request_id, "⌛ 表单已过期", ["请让 AI 员工重新发起。"], "grey")


def failed_card(request_id: uuid.UUID, message: str) -> dict[str, Any]:
    return _result(request_id, "⚠️ 提交未成功", [message, "请让 AI 员工重新发起。"], "orange")


def wecom_link(*, bot_name: str, purpose: str, fields: list[dict[str, Any]], url: str) -> str:
    labels = "、".join(_flat(_label(f)) for f in fields)
    return "\n".join(
        (
            f"**🔒 AI 员工「{_flat(bot_name)}」需要你的个人凭证**",
            f"用途：{_flat(purpose)}",
            f"需要填写：{labels}",
            f"👉 [点这里安全填写]({url})（1 小时内有效，只有你本人能打开）",
            security_note(_flat(bot_name)),
        )
    )


def resume_text(keys: list[str] | tuple[str, ...]) -> str:
    return (
        f"[CoreMan] 用户已通过安全表单提交 {'、'.join(keys)}，已作为环境变量注入本轮，"
        "值不会出现在对话中。请继续完成之前的任务。"
    )
```

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest tests/unit/test_personal_credentials_cards.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add coreman/core/personal_credentials/cards.py tests/unit/test_personal_credentials_cards.py
git commit -m "feat(credentials): build the secure form card and notices"
```

---

### Task 5: 发起索取（`service.open_request`）

**Files:**
- Create: `coreman/core/personal_credentials/service.py`（本任务写到 `open_request` 为止）
- Create: `tests/integration/credential_helpers.py`
- Test: `tests/integration/test_personal_credentials_open.py`

**Interfaces:**
- Consumes: Task 1–4
- Produces（`coreman.core.personal_credentials.service`）：
  - 常量：`PLATFORMS`、`PAGE_PATH`、`RESUME_KIND = "credential_resume"`、`AGENT_NOTE`
  - `page_url(base_url, request_id) -> str`、`login_available(session, platform) -> bool`
  - `capability_scope(session, cap) -> tuple[Bot, User]`
  - `@dataclass Opened(status: Literal["form_sent", "already_pending"], request_id: uuid.UUID)`
  - `open_request(session, cipher, cap, body, *, base_url, now=None) -> Opened`：失败时抛 `CredentialError`，`code` 为 `inactive`、`invalid_fields`、`unreachable`、`login_unavailable` 之一
- 测试公共件（`tests/integration/credential_helpers.py`）：
  - `owner(session, *, platform="feishu", chat_type="single", reached=True) -> (bot, user, task, cipher)`
  - `cap_for(bot, user, task) -> Capability`
  - `cron_cap(bot, user, task) -> Capability`
  - `login_app(session, platform) -> None`

- [ ] **Step 1: 写测试公共件**

`tests/integration/credential_helpers.py`：

```python
"""个人凭证测试公共件：造发起人、来源任务、本轮令牌与网页登录应用。"""

from __future__ import annotations

import uuid

from coreman.core.db.models import PlatformApp, User, UserIdentity, UserReached
from coreman.core.personal_credentials import policy
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import seed_bot

FIELDS = [
    {"key": "DEMO_USERNAME", "label": "账号", "secret": False},
    {"key": "DEMO_PIN", "label": "PIN", "secret": True},
]
BODY = {"fields": FIELDS, "purpose": "查询你在 Demo 系统里的订单"}
VALUES = {"DEMO_USERNAME": "alice", "DEMO_PIN": "pin-778899"}


async def owner(session, *, platform="feishu", chat_type="single", reached=True):  # type: ignore[no-untyped-def]
    bot, _, cipher = await seed_bot(session)
    bot.platform = platform
    user = User(login_name="owner", display_name="本人", email="owner@example.test", source="sync")
    session.add(user)
    await session.flush()
    session.add(
        UserIdentity(
            user_id=user.id, platform=platform, platform_user_id="owner_pid", open_id="ou_owner"
        )
    )
    if reached:
        session.add(UserReached(bot_id=bot.id, user_id=user.id, platform_chat_id="oc_private"))
    await session.commit()
    chat_id = "oc_private" if chat_type == "single" else "oc_group"
    task = await chat_task(
        session, bot, "帮我查订单", sender="owner_pid", chat_type=chat_type, chat_id=chat_id
    )
    return bot, user, task, cipher


def cap_for(bot, user, task) -> policy.Capability:  # type: ignore[no-untyped-def]
    message = task.payload["message"]
    return policy.Capability(
        task_id=task.id,
        bot_id=bot.id,
        user_id=user.id,
        origin_kind="chat",
        chat_id=message["chat_id"],
        chat_type=message["chat_type"],
        session_key=task.session_key,
        event_id=task.inbound_event_id,
        cron_job_id=None,
    )


def cron_cap(bot, user, task) -> policy.Capability:  # type: ignore[no-untyped-def]
    job_id = uuid.uuid4()
    return policy.Capability(
        task_id=task.id,
        bot_id=bot.id,
        user_id=user.id,
        origin_kind="cron",
        chat_id=f"cron:{job_id}",
        chat_type="cron",
        session_key=None,
        event_id=None,
        cron_job_id=job_id,
    )


async def login_app(session, platform: str) -> None:  # type: ignore[no-untyped-def]
    session.add(
        PlatformApp(
            platform=platform,
            name=f"{platform}-login",
            capabilities=["login"],
            corp_id="ww_demo" if platform == "wecom" else None,
            app_id="cli_login" if platform == "feishu" else None,
            secret_enc="unused",
        )
    )
    await session.commit()
```

- [ ] **Step 2: 写失败测试**

`tests/integration/test_personal_credentials_open.py`：

```python
"""发起索取：送达目标、去重、平台条件与令牌所在轮次。"""

import json

import pytest
from sqlalchemy import select

from coreman.core.bus import tasks
from coreman.core.db.models import CredentialRequest, OutboxItem
from coreman.core.personal_credentials import service
from coreman.core.personal_credentials.policy import CredentialError
from tests.integration.credential_helpers import BODY, cap_for, cron_cap, login_app, owner

BASE = "https://coreman.example.com"


async def test_feishu_private_form_is_validated_dm_and_deduplicated(db_session):
    bot, user, task, cipher = await owner(db_session)
    opened = await service.open_request(db_session, cipher, cap_for(bot, user, task), BODY, base_url=BASE)
    await db_session.commit()
    assert opened.status == "form_sent"
    row = await db_session.get(CredentialRequest, opened.request_id)
    assert row.status == "open" and row.delivery_chat_id == "oc_private"
    assert row.origin_event_id == task.inbound_event_id and row.origin_session_key == task.session_key
    item = await db_session.get(OutboxItem, row.request_outbox_id)
    assert item.target == {
        "chat_id": "oc_private",
        "recipient_user_id": str(user.id),
        "recipient_platform_user_id": "owner_pid",
    }
    assert item.payload["card"]["task_id"] == f"credential@{row.id}"
    again = await service.open_request(db_session, cipher, cap_for(bot, user, task), BODY, base_url=BASE)
    await db_session.commit()
    assert again.status == "already_pending" and again.request_id == row.id
    assert len((await db_session.scalars(select(OutboxItem))).all()) == 1


async def test_group_origin_goes_to_dm_with_group_notice(db_session):
    bot, user, task, cipher = await owner(db_session, chat_type="group")
    await service.open_request(db_session, cipher, cap_for(bot, user, task), BODY, base_url=BASE)
    await db_session.commit()
    items = {i.target["chat_id"]: i for i in (await db_session.scalars(select(OutboxItem))).all()}
    assert "card" in items["oc_private"].payload
    assert items["oc_group"].payload == {"markdown": "已私信你一张安全表单，请在私聊里填写。"}


async def test_group_origin_without_dm_record_posts_card_in_group_with_mention(db_session):
    bot, user, task, cipher = await owner(db_session, chat_type="group", reached=False)
    await service.open_request(db_session, cipher, cap_for(bot, user, task), BODY, base_url=BASE)
    await db_session.commit()
    [item] = (await db_session.scalars(select(OutboxItem))).all()
    assert item.target == {"chat_id": "oc_group"}
    assert "<at id=ou_owner></at>" in json.dumps(item.payload, ensure_ascii=False)


async def test_feishu_web_link_only_with_login_app(db_session):
    bot, user, task, cipher = await owner(db_session)
    await login_app(db_session, "feishu")
    opened = await service.open_request(db_session, cipher, cap_for(bot, user, task), BODY, base_url=BASE)
    await db_session.commit()
    item = await db_session.scalar(select(OutboxItem))
    assert f"{BASE}/my-credentials/requests/{opened.request_id}" in json.dumps(item.payload)


async def test_wecom_needs_login_app_and_sends_link(db_session):
    bot, user, task, cipher = await owner(db_session, platform="wecom")
    with pytest.raises(CredentialError) as exc:
        await service.open_request(db_session, cipher, cap_for(bot, user, task), BODY, base_url=BASE)
    assert exc.value.code == "login_unavailable"
    await db_session.rollback()
    await login_app(db_session, "wecom")
    opened = await service.open_request(db_session, cipher, cap_for(bot, user, task), BODY, base_url=BASE)
    await db_session.commit()
    item = await db_session.scalar(select(OutboxItem))
    assert f"]({BASE}/my-credentials/requests/{opened.request_id})" in item.payload["markdown"]


async def test_cron_origin_without_dm_record_is_unreachable(db_session):
    bot, user, task, cipher = await owner(db_session, reached=False)
    with pytest.raises(CredentialError) as exc:
        await service.open_request(db_session, cipher, cron_cap(bot, user, task), BODY, base_url=BASE)
    assert exc.value.code == "unreachable"


async def test_capability_dies_with_its_turn_and_fields_are_checked(db_session):
    bot, user, task, cipher = await owner(db_session)
    with pytest.raises(CredentialError) as bad:
        await service.open_request(
            db_session, cipher, cap_for(bot, user, task),
            {"fields": [{"key": "PATH", "label": "x"}], "purpose": "p"}, base_url=BASE,
        )
    assert bad.value.code == "invalid_fields"
    await tasks.finish(db_session, task.id, status="succeeded")
    await db_session.commit()
    with pytest.raises(CredentialError) as ended:
        await service.open_request(db_session, cipher, cap_for(bot, user, task), BODY, base_url=BASE)
    assert ended.value.code == "inactive"
```

- [ ] **Step 3: 运行，确认失败**

Run: `uv run pytest tests/integration/test_personal_credentials_open.py -q`
Expected: FAIL，`ImportError: cannot import name 'service'`

- [ ] **Step 4: 实现 `service.py` 的前半部分**

```python
"""索取、提交、续接与过期。飞书卡片与 H5 页面两条提交路径汇入同一个 submit()。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.audit import record_audit
from coreman.core.bus import outbox, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    CredentialRequest,
    InboundEvent,
    OutboxItem,
    PlatformApp,
    Task,
    User,
    UserIdentity,
    UserReached,
)
from coreman.core.personal_credentials import cards, policy, store
from coreman.core.personal_credentials.policy import Capability, CredentialError
from coreman.core.timeutils import utcnow

PLATFORMS = ("feishu", "wecom")
PAGE_PATH = "/my-credentials/requests/"
RESUME_KIND = "credential_resume"
RETENTION = timedelta(days=90)
AGENT_NOTE = (
    "已向用户发送安全表单。请简短告诉用户去填写，然后结束本轮；"
    "用户提交后会自动续接，届时变量已在环境里。"
)


def page_url(base_url: str, request_id: uuid.UUID) -> str:
    return base_url.rstrip("/") + PAGE_PATH + str(request_id)


async def login_available(session: AsyncSession, platform: str) -> bool:
    found = await session.scalar(
        select(PlatformApp.id)
        .where(
            PlatformApp.platform == platform,
            PlatformApp.enabled.is_(True),
            PlatformApp.capabilities.any("login"),  # type: ignore[arg-type]
        )
        .limit(1)
    )
    return found is not None


async def _owner(session: AsyncSession, bot_id: uuid.UUID, user_id: uuid.UUID) -> tuple[Bot, User]:
    bot = await session.get(Bot, bot_id, populate_existing=True)
    user = await session.get(User, user_id, populate_existing=True)
    if bot is None or not bot.enabled or bot.platform not in PLATFORMS:
        raise CredentialError("inactive", "AI 员工已停用")
    if user is None or user.status != "active" or user.source == "bootstrap":
        raise CredentialError("inactive", "账号不可用")
    return bot, user


async def _identity(session: AsyncSession, user_id: uuid.UUID, platform: str) -> UserIdentity | None:
    return await session.scalar(
        select(UserIdentity)
        .where(UserIdentity.user_id == user_id, UserIdentity.platform == platform)
        .limit(1)
    )


async def capability_scope(session: AsyncSession, cap: Capability) -> tuple[Bot, User]:
    """令牌只在签发它的那一轮仍在运行时有效。"""
    task = await session.get(Task, cap.task_id, populate_existing=True)
    if (
        task is None
        or task.bot_id != cap.bot_id
        or task.status not in tasks.ACTIVE
        or task.cancel_requested_at is not None
    ):
        raise CredentialError("inactive", "这一轮已经结束，令牌失效")
    return await _owner(session, cap.bot_id, cap.user_id)


@dataclass(frozen=True)
class Opened:
    status: Literal["form_sent", "already_pending"]
    request_id: uuid.UUID


def _delivery(cap: Capability, reached: UserReached | None) -> tuple[str | None, bool]:
    """表单发到哪：有私聊记录发私聊；否则对话发回原会话；定时任务没有私聊记录就发不了。"""
    if reached is not None:
        return reached.platform_chat_id, True
    if cap.origin_kind == "chat":
        return cap.chat_id, False
    return None, False


async def open_request(
    session: AsyncSession,
    cipher: Cipher,
    cap: Capability,
    body: Any,
    *,
    base_url: str,
    now: datetime | None = None,
) -> Opened:
    bot, user = await capability_scope(session, cap)
    parsed = policy.parse_request(body)
    now = now or utcnow()
    keys = sorted(f.key for f in parsed.fields)
    pending = await session.scalars(
        select(CredentialRequest).where(
            CredentialRequest.bot_id == bot.id,
            CredentialRequest.user_id == user.id,
            CredentialRequest.status == "open",
            CredentialRequest.expires_at > now,
        )
    )
    for existing in pending:
        if sorted(str(f["key"]) for f in existing.fields) == keys:
            return Opened("already_pending", existing.id)
    reached = await session.get(UserReached, (bot.id, user.id), populate_existing=True)
    delivery, private = _delivery(cap, reached)
    if delivery is None:
        raise CredentialError(
            "unreachable", "没有可以发送表单的会话：请先在私聊里和 AI 员工说一句话，再重新发起"
        )
    if bot.platform == "wecom" and not await login_available(session, "wecom"):
        raise CredentialError(
            "login_unavailable", "企业微信网页登录未配置，无法发送安全表单，请联系管理员"
        )
    identity = await _identity(session, user.id, bot.platform)
    row = CredentialRequest(
        bot_id=bot.id,
        user_id=user.id,
        origin_kind=cap.origin_kind,
        origin_task_id=cap.task_id,
        origin_event_id=cap.event_id,
        origin_chat_id=cap.chat_id,
        origin_chat_type=cap.chat_type,
        origin_session_key=cap.session_key,
        cron_job_id=cap.cron_job_id,
        delivery_chat_id=delivery,
        fields=[f.model_dump() for f in parsed.fields],
        purpose=parsed.purpose,
        status="open",
        expires_at=now + policy.REQUEST_TTL,
    )
    session.add(row)
    await session.flush()
    target: dict[str, Any] = {"chat_id": delivery}
    if private and identity is not None:
        target |= {
            "recipient_user_id": str(user.id),
            "recipient_platform_user_id": identity.platform_user_id,
        }
    url = page_url(base_url, row.id)
    payload: dict[str, Any]
    if bot.platform == "feishu":
        mention = (
            identity.open_id
            if identity is not None and not private and cap.chat_type == "group"
            else None
        )
        payload = {
            "card": cards.form_card(
                row.id,
                bot_name=bot.name,
                purpose=parsed.purpose,
                fields=row.fields,
                web_url=url if await login_available(session, "feishu") else None,
                mention_open_id=mention,
            )
        }
    else:
        payload = {
            "markdown": cards.wecom_link(
                bot_name=bot.name, purpose=parsed.purpose, fields=row.fields, url=url
            )
        }
    item = await outbox.add(
        session,
        bot_id=bot.id,
        platform=bot.platform,
        kind="send",
        dedupe_key=f"credential-request:{row.id}:form",
        target=target,
        payload=payload,
    )
    row.request_outbox_id = item.id if item else None
    if cap.origin_kind == "chat" and delivery != cap.chat_id:
        await outbox.add(
            session,
            bot_id=bot.id,
            platform=bot.platform,
            kind="send",
            dedupe_key=f"credential-request:{row.id}:notice",
            target={"chat_id": cap.chat_id},
            payload={"markdown": cards.GROUP_NOTICE},
        )
    return Opened("form_sent", row.id)
```

本任务用不到的导入（`delete`、`record_audit`、`NewTask`、`InboundEvent`、`OutboxItem`、`store`）留到 Task 6 再加，避免 ruff 报未使用。

- [ ] **Step 5: 运行，确认通过**

Run: `uv run pytest tests/integration/test_personal_credentials_open.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add coreman/core/personal_credentials/service.py tests/integration/credential_helpers.py tests/integration/test_personal_credentials_open.py
git commit -m "feat(credentials): open a credential request and send the secure form"
```

---

### Task 6: 提交、取消、过期与清理（`service.submit` 等）

**Files:**
- Modify: `coreman/core/personal_credentials/service.py`（追加）
- Test: `tests/integration/test_personal_credentials_submit.py`

**Interfaces:**
- Consumes: Task 5 的 `_owner`、`_identity`、`RESUME_KIND`、`RETENTION`
- Produces：
  - `@dataclass Submitted(status: Literal["saved", "duplicate", "expired", "invalid"], message: str = "", keys: tuple[str, ...] = ())`
  - `submit(session, cipher, request_id, *, actor_id, values, limit=policy.MAX_VALUE, now=None) -> Submitted`：
    - 非本人抛 `CredentialError("forbidden")`，不存在抛 `CredentialError("not_found")`，员工或用户停用抛 `CredentialError("inactive")`；
    - 其余结果都用返回值表示，调用方负责 commit。
  - `cancel(session, request_id, message) -> None`
  - `expire_due(session, now) -> int`
  - `cleanup(session, now, *, limit=500) -> int`
  - 续接任务：`kind="credential_resume"`，`payload={"credential_request_id", "serialize_session": True, "bot_key", "platform_user_id"}`

- [ ] **Step 1: 写失败测试**

```python
"""提交：本人、状态、值校验、加密写入、审计、续接与卡片更新；以及取消、过期、清理。"""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from coreman.core.db.models import AuditLog, CredentialRequest, OutboxItem, Task, User
from coreman.core.personal_credentials import service, store
from coreman.core.personal_credentials.policy import CredentialError
from coreman.core.timeutils import utcnow
from tests.integration.credential_helpers import BODY, VALUES, cap_for, cron_cap, login_app, owner

BASE = "https://coreman.example.com"


async def _opened(session, **kw):
    bot, user, task, cipher = await owner(session, **kw)
    if kw.get("platform") == "wecom":
        await login_app(session, "wecom")
    opened = await service.open_request(session, cipher, cap_for(bot, user, task), BODY, base_url=BASE)
    row = await session.get(CredentialRequest, opened.request_id)
    item = await session.get(OutboxItem, row.request_outbox_id)
    item.status = "sent"
    item.payload = {**item.payload, "_feishu_message_id": "om_form"}
    await session.commit()
    return bot, user, task, cipher, row


async def _dump_everything(session) -> str:
    tasks_ = [t.payload for t in (await session.scalars(select(Task))).all()]
    items = [(i.target, i.payload) for i in (await session.scalars(select(OutboxItem))).all()]
    audits = [a.diff for a in (await session.scalars(select(AuditLog))).all()]
    return json.dumps([tasks_, items, audits], ensure_ascii=False, default=str)


async def test_saved_values_resume_and_card_update_without_plaintext(db_session):
    bot, user, task, cipher, row = await _opened(db_session)
    result = await service.submit(db_session, cipher, row.id, actor_id=user.id, values=VALUES)
    await db_session.commit()
    assert result.status == "saved" and result.keys == ("DEMO_PIN", "DEMO_USERNAME")
    await db_session.refresh(row)
    assert row.status == "submitted" and row.resume_task_id is not None
    resume = await db_session.get(Task, row.resume_task_id)
    assert resume.kind == "credential_resume" and resume.user_id == user.id
    assert resume.session_key == task.session_key and resume.inbound_event_id == task.inbound_event_id
    assert resume.payload["credential_request_id"] == str(row.id)
    assert resume.payload["serialize_session"] is True
    update_item = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "card_update"))
    assert update_item.target["message_id"] == "om_form"
    audit = await db_session.scalar(select(AuditLog).where(AuditLog.action == "personal_credential.saved"))
    assert audit.diff == {"keys": ["DEMO_PIN", "DEMO_USERNAME"]}
    dumped = await _dump_everything(db_session)
    assert "pin-778899" not in dumped and "alice" not in dumped
    found = await store.injected(db_session, cipher, bot_id=bot.id, user_id=user.id)
    assert found.env == VALUES


async def test_owner_status_and_value_rules(db_session):
    bot, user, task, cipher, row = await _opened(db_session)
    stranger = User(login_name="stranger", display_name="别人", source="sync")
    db_session.add(stranger)
    await db_session.commit()
    with pytest.raises(CredentialError) as exc:
        await service.submit(db_session, cipher, row.id, actor_id=stranger.id, values=VALUES)
    assert exc.value.code == "forbidden"
    invalid = await service.submit(
        db_session, cipher, row.id, actor_id=user.id, values={"DEMO_USERNAME": "alice"}
    )
    assert invalid.status == "invalid" and "PIN" in invalid.message
    await db_session.refresh(row)
    assert row.status == "open"
    assert (await service.submit(db_session, cipher, row.id, actor_id=user.id, values=VALUES)).status == "saved"
    await db_session.commit()
    assert (await service.submit(db_session, cipher, row.id, actor_id=user.id, values=VALUES)).status == "duplicate"


async def test_expired_request_is_closed_with_expired_card(db_session):
    bot, user, task, cipher, row = await _opened(db_session)
    await db_session.execute(
        update(CredentialRequest).values(expires_at=utcnow() - timedelta(minutes=1))
    )
    await db_session.commit()
    result = await service.submit(db_session, cipher, row.id, actor_id=user.id, values=VALUES)
    await db_session.commit()
    await db_session.refresh(row)
    assert result.status == "expired" and row.status == "expired"
    card = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "card_update"))
    assert "已过期" in json.dumps(card.payload, ensure_ascii=False)


async def test_cancel_and_expire_due_and_cleanup(db_session):
    bot, user, task, cipher, row = await _opened(db_session)
    await service.cancel(db_session, row.id, "「PIN」不能为空")
    await db_session.commit()
    await db_session.refresh(row)
    assert row.status == "cancelled"
    second = await service.open_request(db_session, cipher, cap_for(bot, user, task), BODY, base_url=BASE)
    assert second.status == "form_sent"
    await db_session.execute(
        update(CredentialRequest)
        .where(CredentialRequest.id == second.request_id)
        .values(expires_at=utcnow() - timedelta(seconds=1))
    )
    await db_session.commit()
    assert await service.expire_due(db_session, utcnow()) == 1
    await db_session.commit()
    await db_session.execute(
        update(CredentialRequest).values(created_at=utcnow() - timedelta(days=91))
    )
    await db_session.commit()
    assert await service.cleanup(db_session, utcnow()) == 2
    await db_session.commit()


async def test_cron_origin_on_wecom_says_saved_instead_of_resuming(db_session):
    bot, user, task, cipher = await owner(db_session, platform="wecom")
    await login_app(db_session, "wecom")
    opened = await service.open_request(db_session, cipher, cron_cap(bot, user, task), BODY, base_url=BASE)
    await db_session.commit()
    result = await service.submit(db_session, cipher, opened.request_id, actor_id=user.id, values=VALUES)
    await db_session.commit()
    row = await db_session.get(CredentialRequest, opened.request_id)
    assert result.status == "saved" and row.resume_task_id is None
    said = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"credential-request:{row.id}:saved")
    )
    assert said.target == {"chat_id": "oc_private"} and "下次执行时生效" in said.payload["markdown"]
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/integration/test_personal_credentials_submit.py -q`
Expected: FAIL，`AttributeError: module ... has no attribute 'submit'`

- [ ] **Step 3: 在 `service.py` 追加实现**

导入补齐：`from sqlalchemy import delete, select`、`from coreman.core.audit import record_audit`、`from coreman.core.bus.tasks import NewTask`，以及 `InboundEvent`、`OutboxItem`、`store`。

```python
@dataclass(frozen=True)
class Submitted:
    status: Literal["saved", "duplicate", "expired", "invalid"]
    message: str = ""
    keys: tuple[str, ...] = ()


async def submit(
    session: AsyncSession,
    cipher: Cipher,
    request_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    values: Any,
    limit: int = policy.MAX_VALUE,
    now: datetime | None = None,
) -> Submitted:
    now = now or utcnow()
    row = await session.get(
        CredentialRequest, request_id, with_for_update=True, populate_existing=True
    )
    if row is None:
        raise CredentialError("not_found", "表单不存在")
    if row.user_id != actor_id:
        raise CredentialError("forbidden", "只有发起人本人可以提交")
    if row.status == "submitted":
        return Submitted("duplicate", "已经提交过了，无需重复提交")
    if row.status != "open":
        return Submitted("expired", "表单已失效，请让 AI 员工重新发起")
    if row.expires_at <= now:
        await _close(session, row, "expired")
        return Submitted("expired", "表单已过期，请让 AI 员工重新发起")
    bot, user = await _owner(session, row.bot_id, row.user_id)
    try:
        cleaned = policy.clean_values(row.fields, values, limit=limit)
    except CredentialError as exc:
        return Submitted("invalid", exc.message)
    keys = await store.save(
        session, cipher, bot_id=bot.id, user_id=user.id, fields=row.fields, values=cleaned
    )
    row.status, row.submitted_at, row.updated_at = "submitted", now, now
    await record_audit(
        session,
        action="personal_credential.saved",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="bot",
        target_id=str(bot.id),
        diff={"keys": keys},
    )
    resumed = await _resume(session, row, bot, user)
    if resumed:
        tail = "AI 员工会继续之前的任务。"
    else:
        tail = "下次执行时生效。" if row.origin_kind == "cron" else "下次对话时生效。"
    await _update_card(session, bot, row, cards.saved_card(row.id, keys, tail))
    if bot.platform == "wecom" and not resumed:
        await _say(session, bot, row, "saved", f"已保存 {'、'.join(keys)}，{tail}")
    return Submitted("saved", tail, tuple(keys))


async def _resume(session: AsyncSession, row: CredentialRequest, bot: Bot, user: User) -> bool:
    if row.origin_kind != "chat" or not row.origin_session_key or row.origin_event_id is None:
        return False
    if await session.get(InboundEvent, row.origin_event_id) is None:
        return False
    identity = await _identity(session, user.id, bot.platform)
    task = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind=RESUME_KIND,
            user_id=user.id,
            session_key=row.origin_session_key,
            inbound_event_id=row.origin_event_id,
            dedupe_key=f"credential-request:{row.id}:resume",
            payload={
                "credential_request_id": str(row.id),
                # 来源那一轮可能还没结束：同一会话串行，等它收尾再续接。
                "serialize_session": True,
                "bot_key": bot.bot_key,
                "platform_user_id": identity.platform_user_id if identity else "",
            },
        ),
    )
    if task is None:
        return False
    row.resume_task_id = task.id
    return True


async def _update_card(
    session: AsyncSession, bot: Bot, row: CredentialRequest, card: dict[str, Any]
) -> None:
    """飞书表单卡换成结果卡；没发出去（没有消息 ID）就不动。"""
    if bot.platform != "feishu" or row.request_outbox_id is None:
        return
    item = await session.get(OutboxItem, row.request_outbox_id)
    mid = (item.payload or {}).get("_feishu_message_id") if item else None
    if not mid:
        return
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="card_update",
        dedupe_key=f"credential-request:{row.id}:card:{row.status}",
        target={
            "message_id": mid,
            "chat_id": item.target.get("chat_id"),
            "task_id": policy.card_task_id(row.id),
        },
        payload={"card": card},
    )


async def _say(
    session: AsyncSession, bot: Bot, row: CredentialRequest, tag: str, markdown: str
) -> None:
    if not row.delivery_chat_id:
        return
    await outbox.add(
        session,
        bot_id=bot.id,
        platform=bot.platform,
        kind="send",
        dedupe_key=f"credential-request:{row.id}:{tag}",
        target={"chat_id": row.delivery_chat_id},
        payload={"markdown": markdown},
    )


async def _close(
    session: AsyncSession, row: CredentialRequest, status: str, card: dict[str, Any] | None = None
) -> None:
    row.status, row.updated_at = status, utcnow()
    bot = await session.get(Bot, row.bot_id)
    if bot is not None:
        await _update_card(session, bot, row, card or cards.expired_card(row.id))


async def cancel(session: AsyncSession, request_id: uuid.UUID, message: str) -> None:
    row = await session.get(
        CredentialRequest, request_id, with_for_update=True, populate_existing=True
    )
    if row is not None and row.status == "open":
        await _close(session, row, "cancelled", cards.failed_card(row.id, message))


async def expire_due(session: AsyncSession, now: datetime) -> int:
    rows = (
        await session.scalars(
            select(CredentialRequest)
            .where(CredentialRequest.status == "open", CredentialRequest.expires_at < now)
            .order_by(CredentialRequest.expires_at)
            .limit(200)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for row in rows:
        await _close(session, row, "expired")
    return len(rows)


async def cleanup(session: AsyncSession, now: datetime, *, limit: int = 500) -> int:
    doomed = (
        select(CredentialRequest.id)
        .where(CredentialRequest.status != "open", CredentialRequest.created_at < now - RETENTION)
        .order_by(CredentialRequest.created_at)
        .limit(limit)
        .scalar_subquery()
    )
    result = await session.execute(
        delete(CredentialRequest)
        .where(CredentialRequest.id.in_(doomed))
        .returning(CredentialRequest.id)
    )
    return len(result.all())
```

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest tests/integration/test_personal_credentials_submit.py tests/integration/test_personal_credentials_open.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add coreman/core/personal_credentials/service.py tests/integration/test_personal_credentials_submit.py
git commit -m "feat(credentials): submit, resume, expire and clean up requests"
```

---

### Task 7: 接口（本轮索取 + 本人网页）

**Files:**
- Create: `coreman/api/routers/personal_credentials.py`
- Modify: `coreman/api/main.py`（路由导入列表加 `personal_credentials`；在 `app.include_router(personal_schedules.router)` 后面加 `app.include_router(personal_credentials.router)`）
- Test: `tests/api/test_personal_credentials_api.py`

**Interfaces:**
- Consumes: Task 2 `read_capability`；Task 5/6 `open_request`、`submit`；Task 3 `list_own`、`plain_value`、`update_value`、`delete`；`interactive_user`（`coreman.api.routers.feishu_personal`）、`verify_csrf`（`coreman.api.security`）、`mcp_rpc.NO_STORE`
- Produces（响应都是 `{"code": 0, "data": ...}` 信封）：
  - `POST /api/runtime/credentials/requests`
    - 成功：202 `form_sent`、200 `already_pending`，`data = {status, request_id, message}`；
    - 失败：401 令牌无效或过期，403 浏览器 `Origin` 或 `inactive`，409 `unreachable`/`login_unavailable`，422 字段无效。
  - `GET /api/me/credential-requests/{id}` → `{id, bot_name, platform, purpose, fields, status, expires_at, security_note}`；非本人 403，不存在 404。
  - `POST /api/me/credential-requests/{id}/submit`，请求体 `{"values": {...}}`
    - 成功：200 `{status: "saved", keys, message}`；
    - 失败：409 已提交，410 已过期，422 值无效，403 非本人。
  - `GET /api/me/credentials` → `[{bot_id, bot_name, env_key, label, secret, value, updated_at, last_used_at}]`，`value` 只对 `secret=false` 有值。
  - `PUT /api/me/credentials/{bot_id}/{env_key}`，请求体 `{"value"}` → 单条；不存在 404。
  - `DELETE /api/me/credentials/{bot_id}/{env_key}` → `data: null`；不存在 404。
  - `/api/me/*` 都要求浏览器登录会话，写操作要 CSRF，响应带 `Cache-Control: no-store`。

- [ ] **Step 1: 写失败测试**

```python
"""个人凭证接口：本轮令牌、本人网页提交与管理。响应与错误里都不能出现值。"""

from datetime import timedelta

from sqlalchemy import select, update

from coreman.core.db.models import CredentialRequest, User
from coreman.core.personal_credentials import policy
from coreman.core.timeutils import utcnow
from tests.api.conftest import login_existing
from tests.integration.credential_helpers import BODY, VALUES, cap_for, login_app, owner

URL = "/api/runtime/credentials/requests"


def _bearer(app, cap):
    token = policy.issue_capability(app.state.cipher, cap, ttl_seconds=600)
    return {"Authorization": f"Bearer {token}"}


async def _requested(client, app, db_session, **kw):
    bot, user, task, _ = await owner(db_session, **kw)
    r = await client.post(URL, json=BODY, headers=_bearer(app, cap_for(bot, user, task)))
    assert r.status_code == 202, r.text
    return bot, user, task, r.json()["data"]["request_id"]


async def test_runtime_request_auth_and_results(client, app, db_session):
    bot, user, task, _ = await owner(db_session)
    headers = _bearer(app, cap_for(bot, user, task))
    assert (await client.post(URL, json=BODY)).status_code == 401
    assert (await client.post(URL, json=BODY, headers={"Authorization": "Bearer x"})).status_code == 401
    browser = {**headers, "Origin": "https://evil.example"}
    assert (await client.post(URL, json=BODY, headers=browser)).status_code == 403
    bad = {"fields": [{"key": "PATH", "label": "x"}], "purpose": "p"}
    assert (await client.post(URL, json=bad, headers=headers)).status_code == 422
    first = await client.post(URL, json=BODY, headers=headers)
    assert first.status_code == 202 and first.json()["data"]["status"] == "form_sent"
    assert "结束本轮" in first.json()["data"]["message"]
    again = await client.post(URL, json=BODY, headers=headers)
    assert again.status_code == 200 and again.json()["data"]["status"] == "already_pending"


async def test_runtime_request_on_wecom_without_login_is_409(client, app, db_session):
    bot, user, task, _ = await owner(db_session, platform="wecom")
    r = await client.post(URL, json=BODY, headers=_bearer(app, cap_for(bot, user, task)))
    assert r.status_code == 409


async def test_web_form_is_owner_only_and_submits_once(client, app, db_session):
    await login_app(db_session, "wecom")
    bot, user, task, rid = await _requested(client, app, db_session, platform="wecom")
    stranger = User(login_name="stranger", display_name="别人", source="sync")
    db_session.add(stranger)
    await db_session.commit()
    await login_existing(client, db_session, stranger)
    assert (await client.get(f"/api/me/credential-requests/{rid}")).status_code == 403
    await login_existing(client, db_session, user)
    got = await client.get(f"/api/me/credential-requests/{rid}")
    assert got.status_code == 200 and got.headers["cache-control"] == "no-store"
    data = got.json()["data"]
    assert data["status"] == "open" and [f["key"] for f in data["fields"]] == ["DEMO_USERNAME", "DEMO_PIN"]
    assert "不会发送给 AI 模型" in data["security_note"]
    csrf = client.headers.pop("X-CSRF-Token")
    assert (await client.post(f"/api/me/credential-requests/{rid}/submit", json={"values": VALUES})).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    invalid = await client.post(f"/api/me/credential-requests/{rid}/submit", json={"values": {"DEMO_USERNAME": "alice"}})
    assert invalid.status_code == 422 and "alice" not in invalid.text
    saved = await client.post(f"/api/me/credential-requests/{rid}/submit", json={"values": VALUES})
    assert saved.status_code == 200 and saved.json()["data"]["keys"] == ["DEMO_PIN", "DEMO_USERNAME"]
    assert "pin-778899" not in saved.text
    again = await client.post(f"/api/me/credential-requests/{rid}/submit", json={"values": VALUES})
    assert again.status_code == 409


async def test_expired_web_submit_is_410(client, app, db_session):
    await login_app(db_session, "wecom")
    bot, user, task, rid = await _requested(client, app, db_session, platform="wecom")
    await db_session.execute(update(CredentialRequest).values(expires_at=utcnow() - timedelta(minutes=1)))
    await db_session.commit()
    await login_existing(client, db_session, user)
    r = await client.post(f"/api/me/credential-requests/{rid}/submit", json={"values": VALUES})
    assert r.status_code == 410
    row = await db_session.scalar(select(CredentialRequest))
    await db_session.refresh(row)
    assert row.status == "expired"


async def test_list_update_delete_own_credentials(client, app, db_session):
    await login_app(db_session, "wecom")
    bot, user, task, rid = await _requested(client, app, db_session, platform="wecom")
    await login_existing(client, db_session, user)
    await client.post(f"/api/me/credential-requests/{rid}/submit", json={"values": VALUES})
    listed = await client.get("/api/me/credentials")
    rows = {r["env_key"]: r for r in listed.json()["data"]}
    assert rows["DEMO_USERNAME"]["value"] == "alice" and rows["DEMO_PIN"]["value"] is None
    assert "pin-778899" not in listed.text
    put = await client.put(f"/api/me/credentials/{bot.id}/DEMO_PIN", json={"value": "pin-222333"})
    assert put.status_code == 200 and "pin-222333" not in put.text
    assert (await client.put(f"/api/me/credentials/{bot.id}/MISSING", json={"value": "x"})).status_code == 404
    assert (await client.delete(f"/api/me/credentials/{bot.id}/DEMO_PIN")).status_code == 200
    assert (await client.delete(f"/api/me/credentials/{bot.id}/DEMO_PIN")).status_code == 404
```


- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/api/test_personal_credentials_api.py -q`
Expected: FAIL，404（路由不存在）

- [ ] **Step 3: 实现路由**

```python
"""个人凭证接口：agent 用本轮令牌发起索取；本人登录后在网页上提交、查看、更新、删除。

任何响应、错误与审计都不带值；提交值只在 service.submit() 里过一次手就加密落库。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import mcp_rpc
from coreman.api.deps import get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.routers.feishu_personal import interactive_user
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, CredentialRequest, PersonalCredential, User
from coreman.core.personal_credentials import cards, policy, service, store
from coreman.core.personal_credentials.policy import CredentialError
from coreman.core.timeutils import utcnow

router = APIRouter(tags=["personal-credentials"])
_STATUS = {
    "invalid_fields": 422,
    "invalid_values": 422,
    "unreachable": 409,
    "login_unavailable": 409,
    "inactive": 403,
    "forbidden": 403,
    "not_found": 404,
}


def _error(exc: CredentialError) -> ApiError:
    status = _STATUS.get(exc.code, 400)
    return ApiError(status, status, exc.message, [{"type": exc.code}])


@router.post("/api/runtime/credentials/requests")
async def create_request(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    if "origin" in request.headers:
        raise ApiError(403, 403, "Browser origins are not supported")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) > 4096:
        raise ApiError(401, 401, "Invalid credential capability")
    try:
        cap = policy.read_capability(request.app.state.cipher, auth[7:])
    except (ValueError, KeyError, TypeError, OverflowError):
        raise ApiError(401, 401, "Invalid credential capability") from None
    try:
        body = await request.json()
    except ValueError:
        raise ApiError(422, 422, "请求体必须是 JSON") from None
    try:
        opened = await service.open_request(
            session,
            request.app.state.cipher,
            cap,
            body,
            base_url=request.app.state.settings.public_base_url,
        )
    except CredentialError as exc:
        await session.rollback()
        raise _error(exc) from None
    await session.commit()
    response.status_code = 202 if opened.status == "form_sent" else 200
    return {
        "code": 0,
        "data": {
            "status": opened.status,
            "request_id": str(opened.request_id),
            "message": service.AGENT_NOTE,
        },
    }


class SubmitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, str] = Field(max_length=policy.MAX_FIELDS)


class ValueIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(max_length=policy.MAX_VALUE * 2)


@router.get("/api/me/credential-requests/{request_id}")
async def get_request(
    request_id: uuid.UUID,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    response.headers.update(mcp_rpc.NO_STORE)
    row = await session.get(CredentialRequest, request_id)
    if row is None:
        raise not_found("表单不存在")
    if row.user_id != actor.id:
        raise ApiError(403, 403, "只有发起人本人可以打开这张表单")
    bot = await session.get(Bot, row.bot_id)
    name = bot.name if bot else "AI 员工"
    status = "expired" if row.status == "open" and row.expires_at <= utcnow() else row.status
    return {
        "code": 0,
        "data": {
            "id": str(row.id),
            "bot_name": name,
            "platform": bot.platform if bot else "",
            "purpose": row.purpose,
            "fields": row.fields,
            "status": status,
            "expires_at": row.expires_at.isoformat(),
            "security_note": cards.security_note(name),
        },
    }


@router.post(
    "/api/me/credential-requests/{request_id}/submit", dependencies=[Depends(verify_csrf)]
)
async def submit_request(
    request_id: uuid.UUID,
    body: SubmitIn,
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    response.headers.update(mcp_rpc.NO_STORE)
    try:
        result = await service.submit(
            session, request.app.state.cipher, request_id, actor_id=actor.id, values=body.values
        )
    except CredentialError as exc:
        await session.rollback()
        raise _error(exc) from None
    # 过期也要提交：submit() 已把状态改成 expired。
    await session.commit()
    if result.status == "duplicate":
        raise ApiError(409, 409, result.message)
    if result.status == "expired":
        raise ApiError(410, 410, result.message)
    if result.status == "invalid":
        raise ApiError(422, 422, result.message)
    return {
        "code": 0,
        "data": {"status": "saved", "keys": list(result.keys), "message": result.message},
    }


def _out(cipher: Cipher, row: PersonalCredential, bot: Bot) -> dict[str, Any]:
    return {
        "bot_id": str(bot.id),
        "bot_name": bot.name,
        "env_key": row.env_key,
        "label": row.label,
        "secret": row.secret,
        "value": store.plain_value(cipher, row),
        "updated_at": row.updated_at.isoformat(),
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
    }


@router.get("/api/me/credentials")
async def list_credentials(
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    response.headers.update(mcp_rpc.NO_STORE)
    rows = await store.list_own(session, actor.id)
    return {"code": 0, "data": [_out(request.app.state.cipher, row, bot) for row, bot in rows]}


async def _audit(session: AsyncSession, actor: User, action: str, bot_id: uuid.UUID, key: str) -> None:
    await record_audit(
        session,
        action=action,
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="bot",
        target_id=str(bot_id),
        diff={"keys": [key]},
    )


@router.put("/api/me/credentials/{bot_id}/{env_key}", dependencies=[Depends(verify_csrf)])
async def update_credential(
    bot_id: uuid.UUID,
    env_key: str,
    body: ValueIn,
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    response.headers.update(mcp_rpc.NO_STORE)
    try:
        value = policy.clean_values([{"key": env_key, "label": env_key}], {env_key: body.value})[
            env_key
        ]
    except CredentialError as exc:
        raise _error(exc) from None
    cipher = request.app.state.cipher
    row = await store.update_value(
        session, cipher, bot_id=bot_id, user_id=actor.id, env_key=env_key, value=value
    )
    if row is None:
        raise not_found("凭证不存在")
    await _audit(session, actor, "personal_credential.updated", bot_id, env_key)
    await session.commit()
    bot = await session.get(Bot, bot_id)
    assert bot is not None
    return {"code": 0, "data": _out(cipher, row, bot)}


@router.delete("/api/me/credentials/{bot_id}/{env_key}", dependencies=[Depends(verify_csrf)])
async def delete_credential(
    bot_id: uuid.UUID,
    env_key: str,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if not await store.delete(session, bot_id=bot_id, user_id=actor.id, env_key=env_key):
        raise not_found("凭证不存在")
    await _audit(session, actor, "personal_credential.deleted", bot_id, env_key)
    await session.commit()
    return {"code": 0, "data": None}
```

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest tests/api/test_personal_credentials_api.py tests/unit/test_layering.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add coreman/api/routers/personal_credentials.py coreman/api/main.py tests/api/test_personal_credentials_api.py
git commit -m "feat(credentials): add runtime request and personal web endpoints"
```

---

### Task 8: 飞书网关在落库前封存表单值

**Files:**
- Modify: `coreman/runtime/gateway_feishu/inbound.py`（`_normalize_event` 的 `card.action.trigger` 分支与 `normalize_event`）
- Modify: `coreman/runtime/gateway_feishu/child.py`（`accept` 里传入加密器）
- Test: `tests/unit/test_feishu_inbound.py`（追加用例）

**Interfaces:**
- Consumes: Task 2 的 `CARD_PREFIX`、`parse_card_task_id`、`sealed_aad`
- Produces：
  - `normalize_event(raw, *, cipher: Cipher | None = None, **kwargs)`
  - 个人凭证卡片回调归一化后，`card_action = {"task_id": "credential@<id>", "card_type": "credential", "sealed": <密文>}`，没有 `selected`；
  - `raw.event.action.form_value` 置为 `{}`；
  - 没传 `cipher` 时返回 `None`（宁可丢弃也不落明文）。

- [ ] **Step 1: 写失败测试**（追加到 `tests/unit/test_feishu_inbound.py` 末尾）

```python
def _form_submit(task_id, form, *, event_id="ev_form"):
    raw = _click_event({"task_id": task_id}, event_id=event_id)
    raw["event"]["action"]["form_value"] = form
    return raw


def test_credential_form_is_sealed_before_persistence():
    from coreman.core.crypto import Cipher
    from coreman.core.personal_credentials.policy import sealed_aad

    cipher = Cipher(b"\x07" * 32)
    rid = uuid.uuid4()
    raw = _form_submit(f"credential@{rid}", {"DEMO_PIN": "pin-778899"})
    message = normalize_event(raw, cipher=cipher, **KW)
    assert message is not None and message.kind == "card_action"
    assert message.card_action["card_type"] == "credential"
    assert "selected" not in message.card_action
    assert message.raw["event"]["action"]["form_value"] == {}
    assert "pin-778899" not in json.dumps(message.model_dump(mode="json"), ensure_ascii=False)
    opened = cipher.decrypt(message.card_action["sealed"], sealed_aad(rid))
    assert json.loads(opened) == {"DEMO_PIN": "pin-778899"}
    # 只改落库副本，不动 SDK 交来的原始对象。
    assert raw["event"]["action"]["form_value"] == {"DEMO_PIN": "pin-778899"}


def test_credential_form_without_cipher_is_dropped():
    raw = _form_submit(f"credential@{uuid.uuid4()}", {"DEMO_PIN": "pin-778899"})
    assert normalize_event(raw, **KW) is None


def test_malformed_credential_task_id_is_ignored():
    from coreman.core.crypto import Cipher

    raw = _form_submit("credential@not-a-uuid", {"DEMO_PIN": "x"})
    assert normalize_event(raw, cipher=Cipher(b"\x07" * 32), **KW) is None
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/unit/test_feishu_inbound.py -q -k credential`
Expected: FAIL（`credential@` 不在白名单，返回 None；第一个用例断言失败）

- [ ] **Step 3: 实现**

`coreman/runtime/gateway_feishu/inbound.py`：

1. 导入：

```python
from coreman.core.crypto import Cipher
from coreman.core.personal_credentials.policy import CARD_PREFIX, parse_card_task_id, sealed_aad
```

2. `_normalize_event` 签名末尾加 `cipher: Cipher | None = None`。

3. `card.action.trigger` 分支里，把白名单判断改为：

```python
        task_id = str(value.get("task_id") or "")
        credential = parse_card_task_id(task_id) is not None
        if (
            not credential
            and not parse_task_id(task_id)
            and not re.fullmatch(r"personal:[1-9][0-9]{0,18}", task_id)
        ) or not header.get("event_id"):
            return None
```

4. 在构造 `InboundMessage` 之前（单选按钮那段 `if not form:` 之后）加：

```python
        card_action: dict[str, Any] = {
            "task_id": task_id,
            "card_type": "form",
            "level": str(value.get("level") or ""),
            "event_key": str(value.get("event_key") or ""),
            "selected": selected,
        }
        if credential:
            # 个人凭证：表单值在落库之前加密封存，入站事件与任务载荷里只有密文。
            # 没有加密器就丢弃这次回调——宁可让用户重填，也不能落一份明文。
            if cipher is None:
                return None
            values = {key: entry[0] for key, entry in selected.items() if len(entry) == 1}
            card_action = {
                "task_id": task_id,
                "card_type": "credential",
                "sealed": cipher.encrypt(
                    json.dumps(values, ensure_ascii=False),
                    sealed_aad(task_id[len(CARD_PREFIX) :]),
                ),
            }
```

并把 `InboundMessage(...)` 里原来内联的 `card_action={...}` 换成 `card_action=card_action`。

5. `normalize_event` 里算 `safe_event` 之后加：

```python
            if (message.card_action or {}).get("card_type") == "credential":
                safe_event["action"] = {**(safe_event.get("action") or {}), "form_value": {}}
```

`coreman/runtime/gateway_feishu/child.py`：在 `async def accept` 定义之前加 `cipher = cfg.build_cipher()`，调用 `normalize_event(...)` 时加 `cipher=cipher`。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest tests/unit/test_feishu_inbound.py tests/unit/test_inbound_normalize.py tests/unit/test_feishu_reply_buttons.py -q`
Expected: PASS（原有卡片回调用例不受影响）

- [ ] **Step 5: Commit**

```bash
git add coreman/runtime/gateway_feishu/inbound.py coreman/runtime/gateway_feishu/child.py tests/unit/test_feishu_inbound.py
git commit -m "feat(feishu): seal credential form values before they are persisted"
```

---

### Task 9: worker 处理飞书表单提交并擦除封存副本

**Files:**
- Create: `coreman/runtime/worker/credential_cards.py`
- Modify: `coreman/runtime/worker/card_actions.py`（飞书防伪校验之后加分支）
- Test: `tests/integration/test_credential_card_submit.py`

**Interfaces:**
- Consumes: Task 6 的 `service.submit`、`service.cancel`；Task 2 的 `parse_card_task_id`、`sealed_aad`、`FEISHU_MAX_VALUE`
- Produces：
  - `handle(session, ctx, bot, inbound, action) -> str`，返回 `saved`、`duplicate`、`expired`、`invalid`、`not_owner`、`ignored`、`inactive` 之一；
  - 无论结果如何，`tasks.payload` 与 `inbound_events.payload` 里的 `card_action.sealed` 都会被删掉。

- [ ] **Step 1: 写失败测试**

```python
"""飞书表单提交：防伪、本人、解封提交、失败取消，以及封存副本一定被擦掉。"""

import json
import uuid

from sqlalchemy import select

from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import CredentialRequest, InboundEvent, OutboxItem, Task, User, UserIdentity
from coreman.core.personal_credentials import policy, service
from coreman.runtime.worker.card_actions import CardActionHandler
from tests.integration.credential_helpers import BODY, VALUES, cap_for, owner
from tests.integration.worker_helpers import build_ctx

BASE = "https://coreman.example.com"


async def _click(session, cipher, bot, rid, values, *, clicker="owner_pid"):
    action = {
        "task_id": policy.card_task_id(rid),
        "card_type": "credential",
        "sealed": cipher.encrypt(json.dumps(values), policy.sealed_aad(rid)),
    }
    ev = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=f"action:{uuid.uuid4()}",
        kind="card_action",
        chat_type="group",
        chat_id="oc_private",
        sender_platform_user_id=clicker,
        sender_open_id="ou_x",
        reply_context={"chat_id": "oc_private", "message_id": "om_form"},
        payload={"card_action": action, "raw": {"event": {"action": {"form_value": {}}}}},
    )
    session.add(ev)
    await session.flush()
    click = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind="card_action",
            lane="fast",
            payload={"card_action": action, "platform_user_id": clicker},
            session_key="oc_private",
            inbound_event_id=ev.id,
            dedupe_key=f"click:{ev.id}",
        ),
    )
    click.status = "running"
    await session.commit()
    return ev, click


async def _prepared(session):
    bot, user, task, cipher = await owner(session)
    opened = await service.open_request(session, cipher, cap_for(bot, user, task), BODY, base_url=BASE)
    row = await session.get(CredentialRequest, opened.request_id)
    item = await session.get(OutboxItem, row.request_outbox_id)
    item.status = "sent"
    item.payload = {**item.payload, "_feishu_message_id": "om_form"}
    await session.commit()
    return bot, user, cipher, row


async def _sealed_left(session, ev, click) -> bool:
    ev_row = await session.get(InboundEvent, ev.id, populate_existing=True)
    task_row = await session.get(Task, click.id, populate_existing=True)
    return "sealed" in ev_row.payload["card_action"] or "sealed" in task_row.payload["card_action"]


async def test_owner_submit_saves_resumes_and_wipes(db_session, db_engine):
    bot, user, cipher, row = await _prepared(db_session)
    ev, click = await _click(db_session, cipher, bot, row.id, VALUES)
    await CardActionHandler().run(build_ctx(db_engine, click))
    await db_session.refresh(row)
    assert row.status == "submitted" and row.resume_task_id is not None
    done = await db_session.get(Task, click.id, populate_existing=True)
    assert done.result == {"card": "saved"}
    assert not await _sealed_left(db_session, ev, click)


async def test_someone_else_cannot_submit_and_copy_is_still_wiped(db_session, db_engine):
    bot, user, cipher, row = await _prepared(db_session)
    other = User(login_name="other", display_name="别人", source="sync")
    db_session.add(other)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=other.id, platform="feishu", platform_user_id="other_pid"))
    await db_session.commit()
    ev, click = await _click(db_session, cipher, bot, row.id, VALUES, clicker="other_pid")
    await CardActionHandler().run(build_ctx(db_engine, click))
    await db_session.refresh(row)
    assert row.status == "open"
    done = await db_session.get(Task, click.id, populate_existing=True)
    assert done.result == {"card": "not_owner"}
    assert not await _sealed_left(db_session, ev, click)


async def test_invalid_values_cancel_the_request(db_session, db_engine):
    bot, user, cipher, row = await _prepared(db_session)
    ev, click = await _click(db_session, cipher, bot, row.id, {"DEMO_USERNAME": "alice"})
    await CardActionHandler().run(build_ctx(db_engine, click))
    await db_session.refresh(row)
    assert row.status == "cancelled"
    card = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "card_update"))
    assert "提交未成功" in json.dumps(card.payload, ensure_ascii=False)
    assert not await _sealed_left(db_session, ev, click)
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/integration/test_credential_card_submit.py -q`
Expected: FAIL（`card_task_id_unknown`，结果是 `unknown`）

- [ ] **Step 3: 实现**

`coreman/runtime/worker/credential_cards.py`：

```python
"""飞书个人凭证表单的提交：解封网关封存的值、交给 service.submit()，并擦掉封存副本。

调用方（CardActionHandler）已经证明这张卡是我们发到这个会话的；这里只认点击人是不是发起人。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.chat.identity import resolve_speaker
from coreman.core.crypto import DecryptError
from coreman.core.db.models import Bot, InboundEvent
from coreman.core.personal_credentials import policy, service
from coreman.core.personal_credentials.policy import CredentialError
from coreman.runtime.worker.context import TaskContext

_ERRORS = {"forbidden": "not_owner", "not_found": "ignored"}


async def wipe(session: AsyncSession, task_id: int, event_id: int) -> None:
    """封存副本用完即删：任务与入站事件按 90 天保留，不留这份密文。"""
    await session.execute(
        text("UPDATE tasks SET payload = payload #- '{card_action,sealed}' WHERE id = :id"),
        {"id": task_id},
    )
    await session.execute(
        text(
            "UPDATE inbound_events SET payload = payload #- '{card_action,sealed}' WHERE id = :id"
        ),
        {"id": event_id},
    )


async def _submit(
    session: AsyncSession, ctx: TaskContext, inbound: InboundEvent, action: dict[str, Any]
) -> str:
    request_id = policy.parse_card_task_id(str(action.get("task_id") or ""))
    sealed = action.get("sealed")
    if request_id is None or not isinstance(sealed, str):
        return "ignored"
    speaker = await resolve_speaker(
        session, platform="feishu", platform_user_id=inbound.sender_platform_user_id or ""
    )
    if speaker.user_id is None:
        return "not_owner"
    try:
        values = json.loads(ctx.cipher.decrypt(sealed, policy.sealed_aad(request_id)))
    except (DecryptError, ValueError):
        return "ignored"
    try:
        result = await service.submit(
            session,
            ctx.cipher,
            request_id,
            actor_id=speaker.user_id,
            values=values,
            limit=policy.FEISHU_MAX_VALUE,
        )
    except CredentialError as exc:
        return _ERRORS.get(exc.code, exc.code)
    if result.status == "invalid":
        # 卡片上的表单不能原地改错：取消这次请求，agent 再发起会得到一张新表单。
        await service.cancel(session, request_id, result.message)
    return result.status


async def handle(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    inbound: InboundEvent,
    action: dict[str, Any],
) -> str:
    try:
        return await _submit(session, ctx, inbound, action)
    finally:
        await wipe(session, ctx.task.id, inbound.id)
```

`coreman/runtime/worker/card_actions.py`：在飞书防伪校验那段 `if bot.platform == "feishu": sent = ...` 的块之后、`personal:` 分支之前插入：

```python
            if bot.platform == "feishu" and task_id.startswith(CARD_PREFIX):
                from coreman.runtime.worker.credential_cards import handle as handle_credential

                result = await handle_credential(session, ctx, bot, inbound, action)
                await tasks.finish(
                    session, ctx.task.id, status="succeeded", result={"card": result}
                )
                await session.commit()
                ctx.log.info("card_action_done", result=result)
                return
```

并在文件头导入 `from coreman.core.personal_credentials.policy import CARD_PREFIX`。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest tests/integration/test_credential_card_submit.py tests/integration/test_card_actions.py tests/api/test_feishu_personal_cards.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add coreman/runtime/worker/credential_cards.py coreman/runtime/worker/card_actions.py tests/integration/test_credential_card_submit.py
git commit -m "feat(credentials): submit Feishu form values and wipe the sealed copy"
```

---

### Task 10: 对话轮注入、本轮令牌与提示词

**Files:**
- Create: `coreman/runtime/worker/chat/credentials.py`
- Modify: `coreman/runtime/worker/chat/opening.py`（`schedules.configure` 之后调用；`ctx.secrets` 合并个人凭证）
- Modify: `tests/fakes/fake_relay.py`（新增 `leaks_personal` 场景）
- Test: `tests/integration/test_credential_injection.py`

**Interfaces:**
- Consumes: Task 3 `store.injected`；Task 2 `Capability`、`issue_capability`、`ENV_PREFIX`、`CAPABILITY_GRACE`；Task 5 `service.PLATFORMS`
- Produces（`coreman.runtime.worker.chat.credentials`）：
  - `API_PATH = "/api/runtime/credentials/requests"`
  - `guidance(names: tuple[str, ...]) -> str`
  - `configure(session, ctx, intake, extra, env) -> tuple[str, dict[str, str], frozenset[str]]`：返回追加后的提示词、env、需要脱敏的值
  - `configure_cron(session, ctx, *, bot, actor_id, job_id, extra, env) -> tuple[str, dict[str, str], frozenset[str]]`

- [ ] **Step 1: 加假运行时场景**

`tests/fakes/fake_relay.py`：在 `_leaked_credential_frames` 之后加：

```python
def _leaked_personal_frames(body: dict[str, Any]) -> list[str]:
    """模型把本轮注入的个人凭证原样打了出来（键名不含 token/password 等标记）。"""
    value = (body.get("env_vars") or {}).get("DEMO_PIN", "")
    return [": ping\n\n", _text(f"你的 PIN 是 {value}"), FINISH, _chunk(None, usage=USAGE), DONE]
```

并把 `_handle` 里选择帧的那段改成：

```python
        if self.scenario == "leaks_credentials":
            frames = _leaked_credential_frames(body)
        elif self.scenario == "leaks_personal":
            frames = _leaked_personal_frames(body)
        else:
            frames = SCENARIOS[self.scenario]()
```

- [ ] **Step 2: 写失败测试**

```python
"""本人触发的轮次注入本人的凭证；别人、协作轮拿不到；模型复述时出站被拦。"""

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.chat.redaction import PLACEHOLDER
from coreman.core.db.models import InboundEvent, User, UserIdentity
from coreman.core.personal_credentials import policy, store
from coreman.core.prompting import Speaker
from coreman.runtime.worker.chat import credentials
from coreman.runtime.worker.chat.models import Intake
from tests.fakes.fake_relay import FakeRelay
from tests.integration.credential_helpers import FIELDS, VALUES, owner
from tests.integration.test_chat_handler import chat_task, run, stream_of
from tests.integration.worker_helpers import build_ctx


async def _saved(session, **kw):
    bot, user, task, cipher = await owner(session, platform="wecom", **kw)
    await store.save(session, cipher, bot_id=bot.id, user_id=user.id, fields=FIELDS, values=VALUES)
    await session.commit()
    return bot, user, task, cipher


async def test_private_turn_gets_own_credentials_and_echo_is_masked(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, task, cipher = await _saved(db_session)
    fake = FakeRelay("leaks_personal")
    await run(db_engine, task, fake)
    env = fake.requests[0]["env_vars"]
    assert env["DEMO_PIN"] == "pin-778899" and env["DEMO_USERNAME"] == "alice"
    assert env["COREMAN_CREDENTIAL_URL"] == "http://localhost/api/runtime/credentials/requests"
    cap = policy.read_capability(cipher, env["COREMAN_CREDENTIAL_TOKEN"])
    assert (cap.user_id, cap.task_id, cap.origin_kind, cap.chat_id) == (
        user.id, task.id, "chat", "oc_private"
    )
    prompt = fake.requests[0]["messages"][0]["content"]
    assert "`$DEMO_PIN`" in prompt and "pin-778899" not in prompt and "alice" not in prompt
    stream = await stream_of(db_session, task.id)
    written = "\n".join(filter(None, [stream.final_text, stream.pending_text]))
    assert "pin-778899" not in written and PLACEHOLDER in written


async def test_group_turn_injects_only_the_speakers_own(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, user, task, cipher = await _saved(db_session, chat_type="group")
    other = User(login_name="other", display_name="别人", source="sync")
    db_session.add(other)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=other.id, platform="wecom", platform_user_id="other_pid"))
    await db_session.commit()
    mine = await chat_task(db_session, bot, "我也查一下", sender="other_pid", chat_type="group", chat_id="oc_group")
    fake = FakeRelay("normal")
    await run(db_engine, mine, fake)
    env = fake.requests[0]["env_vars"]
    assert "DEMO_PIN" not in env and "DEMO_USERNAME" not in env
    assert policy.read_capability(cipher, env["COREMAN_CREDENTIAL_TOKEN"]).user_id == other.id


async def test_collaboration_turns_get_nothing(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, user, task, cipher = await _saved(db_session)
    inbound = await db_session.get(InboundEvent, task.inbound_event_id)
    speaker = Speaker("owner_pid", user.id, "owner", "本人")
    intake = Intake(bot, None, inbound, speaker, "oc_private", "single", "oc_private", "x", "text")
    db_session.expunge(task)
    base = dict(task.payload)
    for key in ("collaboration_id", "collaboration_phase", "human_collaboration_id"):
        task.payload = {**base, key: "x"}
        extra, env, secrets = await credentials.configure(
            db_session, build_ctx(db_engine, task), intake, "", {"COREMAN_CREDENTIAL_TOKEN": "forged"}
        )
        assert (extra, env, secrets) == ("", {}, frozenset())


def test_guidance_lists_names_and_rules():
    text = credentials.guidance(("DEMO_PIN",))
    assert "`$DEMO_PIN`" in text and "$COREMAN_CREDENTIAL_URL" in text
    assert "不得让用户在聊天里发送密码或密钥" in text
    assert "还没有保存" in credentials.guidance(())
```

- [ ] **Step 3: 运行，确认失败**

Run: `uv run pytest tests/integration/test_credential_injection.py -q`
Expected: FAIL，`ImportError: cannot import name 'credentials'`

- [ ] **Step 4: 实现**

`coreman/runtime/worker/chat/credentials.py`：

```python
"""个人凭证：本人触发的轮次注入本人在这个 AI 员工里保存的凭证，并下发向本人索取的本轮令牌。

对话轮按已验证的发言者算（私聊、群聊都一样，只给当前发言者自己的）；机器人之间协作、
同事答复后的续接这些轮次不注入；定时任务按执行人算。凭证值进 env，从不进提示词。
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Bot, User
from coreman.core.personal_credentials import policy, service, store
from coreman.runtime.worker.chat.models import Intake
from coreman.runtime.worker.context import TaskContext

API_PATH = "/api/runtime/credentials/requests"
_COLLABORATION_KEYS = ("collaboration_id", "collaboration_phase", "human_collaboration_id")
_NONE: frozenset[str] = frozenset()


def _strip(env: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in env.items() if not key.startswith(policy.ENV_PREFIX)}


def guidance(names: tuple[str, ...]) -> str:
    have = (
        "当前发言者已在本 AI 员工保存的个人凭证（环境变量，只给名字）："
        + "、".join(f"`${name}`" for name in names)
        + "。"
        if names
        else "当前发言者在本 AI 员工还没有保存个人凭证。"
    )
    return (
        "\n\n## 个人凭证\n"
        + have
        + "\n任务需要当前发言者本人的账号、密码或 API Key 而环境变量里没有，"
        "或者调用时提示凭证无效，就向本人索取：\n"
        "```bash\n"
        'curl -sS -X POST "$COREMAN_CREDENTIAL_URL" '
        '-H "Authorization: Bearer $COREMAN_CREDENTIAL_TOKEN" '
        "-H 'Content-Type: application/json' "
        """-d '{"fields":[{"key":"DEMO_API_KEY","label":"Demo 系统 API Key","secret":true}],"""
        """"purpose":"查询你在 Demo 系统里的订单"}'\n"""
        "```\n"
        "`key` 是环境变量名（大写字母、数字、下划线），`label` 是给用户看的名字，"
        "账号这类非机密字段把 `secret` 设为 false。调用成功后简短告诉用户「已发送安全表单，请填写」，"
        "然后结束本轮；用户提交后系统会自动让你继续，届时变量已经在环境里。\n"
        "规则：不得让用户在聊天里发送密码或密钥；不得打印、回显或记录这些变量的值，"
        "不得写入文件、工作区或 URL；只用于当前发言者本人的请求；接口地址与令牌只在本轮有效。"
    )


async def _apply(
    session: AsyncSession,
    ctx: TaskContext,
    *,
    bot: Bot,
    user_id: uuid.UUID,
    cap: policy.Capability,
    extra: str,
    env: dict[str, str],
) -> tuple[str, dict[str, str], frozenset[str]]:
    env = _strip(env)
    user = await session.get(User, user_id, populate_existing=True)
    if user is None or user.status != "active" or user.source == "bootstrap":
        return extra, env, _NONE
    found = await store.injected(session, ctx.cipher, bot_id=bot.id, user_id=user_id)
    env.update(found.env)
    env[policy.ENV_PREFIX + "URL"] = ctx.public_base_url.rstrip("/") + API_PATH
    env[policy.ENV_PREFIX + "TOKEN"] = policy.issue_capability(
        ctx.cipher, cap, ttl_seconds=bot.sse_timeout_seconds + policy.CAPABILITY_GRACE
    )
    return extra + guidance(found.names), env, found.secret_values


async def configure(
    session: AsyncSession, ctx: TaskContext, intake: Intake, extra: str, env: dict[str, str]
) -> tuple[str, dict[str, str], frozenset[str]]:
    payload = ctx.task.payload
    user_id = intake.speaker.user_id
    if (
        intake.bot.platform not in service.PLATFORMS
        or user_id is None
        or any(payload.get(key) for key in _COLLABORATION_KEYS)
    ):
        return extra, _strip(env), _NONE
    cap = policy.Capability(
        task_id=ctx.task.id,
        bot_id=intake.bot.id,
        user_id=user_id,
        origin_kind="chat",
        chat_id=intake.chat_id,
        chat_type=intake.chat_type,
        session_key=intake.session_key,
        event_id=intake.inbound.id,
        cron_job_id=None,
    )
    return await _apply(
        session, ctx, bot=intake.bot, user_id=user_id, cap=cap, extra=extra, env=env
    )


async def configure_cron(
    session: AsyncSession,
    ctx: TaskContext,
    *,
    bot: Bot,
    actor_id: uuid.UUID,
    job_id: uuid.UUID,
    extra: str,
    env: dict[str, str],
) -> tuple[str, dict[str, str], frozenset[str]]:
    if bot.platform not in service.PLATFORMS:
        return extra, _strip(env), _NONE
    cap = policy.Capability(
        task_id=ctx.task.id,
        bot_id=bot.id,
        user_id=actor_id,
        origin_kind="cron",
        chat_id=f"cron:{job_id}",
        chat_type="cron",
        session_key=None,
        event_id=None,
        cron_job_id=job_id,
    )
    return await _apply(session, ctx, bot=bot, user_id=actor_id, cap=cap, extra=extra, env=env)
```

`coreman/runtime/worker/chat/opening.py`：

1. 在 `extra, env = await schedules.configure(...)` 之后加：

```python
        from coreman.runtime.worker.chat import credentials

        extra, env, personal_secrets = await credentials.configure(session, ctx, intake, extra, env)
```

2. 把 `ctx.secrets = collect_secrets(env)` 改为 `ctx.secrets = collect_secrets(env) | personal_secrets`，上方注释补一句「+ 个人凭证（按值强制脱敏，不看键名）」。

- [ ] **Step 5: 运行，确认通过**

Run: `uv run pytest tests/integration/test_credential_injection.py tests/integration/test_chat_handler.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add coreman/runtime/worker/chat/credentials.py coreman/runtime/worker/chat/opening.py tests/fakes/fake_relay.py tests/integration/test_credential_injection.py
git commit -m "feat(credentials): inject the speaker's own credentials into chat turns"
```

---

### Task 11: 续接轮处理器 `credential_resume`

**Files:**
- Create: `coreman/runtime/worker/credential_resume.py`
- Modify: `coreman/runtime/worker/__main__.py`（注册 `"credential_resume": CredentialResumeHandler()`）
- Test: `tests/integration/test_credential_resume.py`

**Interfaces:**
- Consumes: Task 6 排出的续接任务；Task 4 `cards.resume_text`；Task 10 开轮注入（发言者就是发起人，凭证会被注入）
- Produces: `CredentialResumeHandler(ChatTaskHandler)`，`kind = "credential_resume"`

- [ ] **Step 1: 写失败测试**

```python
"""提交之后在原会话续接：发言者是发起人、消息里只有键名、凭证在 env 里、企微走主动推送。"""

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import tasks
from coreman.core.db.models import CredentialRequest, Task
from coreman.core.personal_credentials import service
from coreman.runtime.worker.credential_resume import CredentialResumeHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.credential_helpers import BODY, VALUES, cap_for, login_app, owner
from tests.integration.test_chat_handler import stream_of
from tests.integration.worker_helpers import build_ctx


async def _submitted(session):
    bot, user, task, cipher = await owner(session, platform="wecom")
    await login_app(session, "wecom")
    opened = await service.open_request(session, cipher, cap_for(bot, user, task), BODY, base_url="http://localhost")
    await session.commit()
    await service.submit(session, cipher, opened.request_id, actor_id=user.id, values=VALUES)
    await tasks.finish(session, task.id, status="succeeded")
    await session.commit()
    row = await session.get(CredentialRequest, opened.request_id)
    claimed = await tasks.claim(session, lane="normal", instance_id="worker-test")
    await session.commit()
    assert claimed is not None and claimed.id == row.resume_task_id
    return bot, user, cipher, row, claimed


async def test_resume_continues_as_owner_with_keys_only(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, claimed, relay_client_factory=lambda _r: fake.client())
    await CredentialResumeHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    body = fake.requests[0]
    assert "已通过安全表单提交 DEMO_PIN、DEMO_USERNAME" in body["messages"][1]["content"]
    assert "pin-778899" not in body["messages"][1]["content"]
    assert body["env_vars"]["DEMO_PIN"] == "pin-778899"
    stream = await stream_of(db_session, claimed.id)
    assert stream.delivery_mode == "proactive"
    done = await db_session.get(Task, claimed.id, populate_existing=True)
    assert done.status == "succeeded"


async def test_resume_is_dropped_when_request_no_longer_points_here(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, user, cipher, row, claimed = await _submitted(db_session)
    row.resume_task_id = None
    await db_session.commit()
    fake = FakeRelay("normal")
    await CredentialResumeHandler().run(build_ctx(db_engine, claimed, relay_client_factory=lambda _r: fake.client()))
    done = await db_session.get(Task, claimed.id, populate_existing=True)
    assert done.status == "cancelled" and done.error_code == "credential_resume_inactive"
    assert fake.requests == []
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/integration/test_credential_resume.py -q`
Expected: FAIL，`ModuleNotFoundError: coreman.runtime.worker.credential_resume`

- [ ] **Step 3: 实现**

```python
"""credential_resume：用户提交凭证之后，在原会话里续接那一轮。

与普通 chat 的差别只有入口：读凭证请求而不是入站消息（入站事件沿用来源那一轮的），发言者
固定为发起人；企业微信的流从创建起就是主动推送（续接轮没有可用的 req_id）。凭证照常在开轮时
按发言者注入（chat/credentials.py），这一轮的消息里只有键名。其余全部复用 ChatTaskHandler。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox, tasks
from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import (
    Bot,
    BotAllowedUser,
    CredentialRequest,
    RelayServer,
    User,
    UserIdentity,
)
from coreman.core.personal_credentials import cards
from coreman.core.personal_credentials.service import RESUME_KIND
from coreman.runtime.worker.chat_handler import ChatTaskHandler, Intake, Prepared
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import load_inbound


class CredentialResumeHandler(ChatTaskHandler):
    kind = RESUME_KIND

    async def _resolve(
        self, session: AsyncSession, ctx: TaskContext
    ) -> tuple[Intake, RelayServer, list[dict[str, Any]]] | None:
        bot = await session.get(Bot, ctx.task.bot_id)
        raw_id = ctx.task.payload.get("credential_request_id")
        row = (
            await session.get(
                CredentialRequest,
                uuid.UUID(str(raw_id)),
                with_for_update=True,
                populate_existing=True,
            )
            if raw_id
            else None
        )
        if (
            bot is None
            or not bot.enabled
            or row is None
            or row.status != "submitted"
            or row.resume_task_id != ctx.task.id
        ):
            await tasks.finish(
                session, ctx.task.id, status="cancelled", error_code="credential_resume_inactive"
            )
            return None
        identity = await session.scalar(
            select(UserIdentity)
            .where(UserIdentity.user_id == row.user_id, UserIdentity.platform == bot.platform)
            .limit(1)
        )
        speaker = await resolve_speaker(
            session,
            platform=bot.platform,
            platform_user_id=identity.platform_user_id if identity else "",
            resolver=ctx.openuserid,
        )
        user = await session.get(User, row.user_id, populate_existing=True)
        allowed = set(
            await session.scalars(
                select(BotAllowedUser.user_id).where(BotAllowedUser.bot_id == bot.id)
            )
        )
        if (
            speaker.user_id != row.user_id
            or user is None
            or user.status != "active"
            or (allowed and row.user_id not in allowed)
        ):
            if row.delivery_chat_id:
                await outbox.add(
                    session,
                    bot_id=bot.id,
                    platform=bot.platform,
                    kind="send",
                    dedupe_key=f"credential-request:{row.id}:resume-skipped",
                    target={"chat_id": row.delivery_chat_id},
                    payload={"markdown": "凭证已保存，下次对话时生效。"},
                )
            await tasks.finish(
                session, ctx.task.id, status="cancelled", error_code="credential_owner_changed"
            )
            return None
        relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
        if relay is None or not relay.is_active:
            await tasks.finish(session, ctx.task.id, status="failed", error_code="relay_unavailable")
            return None
        inbound = await load_inbound(session, ctx.task)
        text = cards.resume_text([str(field["key"]) for field in row.fields])
        intake = Intake(
            bot,
            relay,
            inbound,
            speaker,
            row.origin_chat_id,
            row.origin_chat_type,
            row.origin_session_key or row.origin_chat_id,
            text,
            RESUME_KIND,
        )
        return intake, relay, [{"type": "text", "text": text}]

    def _needs_content(self) -> bool:
        """续接消息是系统写好的成品文本，没有媒体要下载。"""
        return False

    def _stream_kwargs(self, ctx: TaskContext, intake: Intake) -> dict[str, Any]:
        """企业微信：没有可用的 req_id，流从创建就交给 outbox 主动推送；飞书沿用来源的回复上下文。"""
        if intake.bot.platform != "wecom":
            return {}
        now = datetime.now(UTC).isoformat()
        return {
            "delivery_mode": "proactive",
            "background_state": {
                "mode": "proactive",
                "offset": 0,
                "finish_suffix": "",
                "switched_at": now,
            },
            "reply_context": {
                "gateway_instance": ctx.instance_id,
                "received_at": now,
                "chat_type": intake.chat_type,
                "chat_id": intake.chat_id,
            },
        }

    def _after_supervisor(self, pre: Prepared) -> None:
        if pre.intake.bot.platform == "wecom":
            pre.supervisor.start_proactive(pre.started_clock)
```

`coreman/runtime/worker/__main__.py`：导入 `from coreman.runtime.worker.credential_resume import CredentialResumeHandler`，`handlers` 里加 `"credential_resume": CredentialResumeHandler(),`。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest tests/integration/test_credential_resume.py tests/integration/test_choice_submit.py -q`
Expected: PASS。如果续接轮把来源消息的 `req_id` 当成可用的流窗口（企业微信），检查 `_stream_kwargs` 是否生效，并对照 `choice_submit.py` 的同名方法。

- [ ] **Step 5: Commit**

```bash
git add coreman/runtime/worker/credential_resume.py coreman/runtime/worker/__main__.py tests/integration/test_credential_resume.py
git commit -m "feat(credentials): resume the original conversation after submission"
```

---

### Task 12: 定时任务注入与清理任务

**Files:**
- Modify: `coreman/runtime/worker/cron_handler.py`（`ctx.secrets = collect_secrets(env)` 之前）
- Modify: `coreman/runtime/scheduler/reaper.py`（`run_cleanup` 与 `run_retention`）
- Test: `tests/integration/test_credential_cron.py`、`tests/integration/test_credential_reaper.py`

**Interfaces:**
- Consumes: Task 10 `credentials.configure_cron`；Task 6 `service.expire_due`、`service.cleanup`
- Produces：
  - `run_cleanup` 结果多一个键 `credential_requests_expired`；
  - `run_retention` 结果多一个键 `old_credential_requests`。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_credential_cron.py`：

```python
"""定时任务按执行人注入；同事答复后的续跑不注入（沿用 asked 分支，不在此重复覆盖）。"""

from datetime import UTC, datetime

from coreman.core.crypto import Cipher
from coreman.core.db.session import make_session_factory
from coreman.core.personal_credentials import policy, store
from coreman.runtime.scheduler.cron import run_tick
from coreman.runtime.worker.cron_handler import CronRunHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.credential_helpers import FIELDS, VALUES
from tests.integration.test_cron_handler import claim
from tests.integration.test_cron_scheduler import job
from tests.integration.worker_helpers import MASTER, build_ctx


async def test_cron_run_injects_the_actors_credentials(db_engine, db_session):
    now = datetime.now(UTC)
    row = await job(db_session, now, target_chats=["test-group"])
    cipher = Cipher(MASTER)
    await store.save(
        db_session, cipher, bot_id=row.bot_id, user_id=row.created_by, fields=FIELDS, values=VALUES
    )
    await db_session.commit()
    await run_tick(make_session_factory(db_engine), now)
    task = await claim(db_session)
    fake = FakeRelay("normal")
    await CronRunHandler().run(build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client()))
    env = fake.requests[0]["env_vars"]
    assert env["DEMO_PIN"] == "pin-778899"
    cap = policy.read_capability(cipher, env["COREMAN_CREDENTIAL_TOKEN"])
    assert (cap.origin_kind, cap.cron_job_id, cap.user_id) == ("cron", row.id, row.created_by)
    assert "`$DEMO_PIN`" in fake.requests[0]["messages"][0]["content"]
```

`tests/integration/test_credential_reaper.py`：

```python
"""清理任务：到点的请求置为过期；保留期外的已结束请求删除。"""

from datetime import timedelta

from sqlalchemy import update

from coreman.core.db.models import CredentialRequest
from coreman.core.db.session import make_session_factory
from coreman.core.personal_credentials import service
from coreman.core.settings_store import SettingsStore
from coreman.core.timeutils import utcnow
from coreman.runtime.scheduler import reaper
from tests.integration.credential_helpers import BODY, cap_for, owner


async def test_requests_expire_then_age_out(db_engine, db_session):
    bot, user, task, cipher = await owner(db_session)
    await service.open_request(db_session, cipher, cap_for(bot, user, task), BODY, base_url="http://localhost")
    await db_session.execute(update(CredentialRequest).values(expires_at=utcnow() - timedelta(minutes=1)))
    await db_session.commit()
    factory = make_session_factory(db_engine)
    counts = await reaper.run_cleanup(factory, SettingsStore(factory), utcnow())
    assert counts["credential_requests_expired"] == 1
    await db_session.execute(update(CredentialRequest).values(created_at=utcnow() - timedelta(days=91)))
    await db_session.commit()
    kept = await reaper.run_retention(factory, utcnow())
    assert kept["old_credential_requests"] == 1
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/integration/test_credential_cron.py tests/integration/test_credential_reaper.py -q`
Expected: FAIL（env 里没有 `DEMO_PIN`；结果里没有新键）

- [ ] **Step 3: 实现**

`coreman/runtime/worker/cron_handler.py`，把

```python
                # 定时执行同样带着本人的业务系统令牌与个人工具凭据，出站闸门一视同仁。
                ctx.secrets = collect_secrets(env)
```

改为

```python
                personal_secrets: frozenset[str] = frozenset()
                if asked is None:
                    # 本人创建的定时任务算本人触发：注入执行人的个人凭证并下发索取令牌。
                    from coreman.runtime.worker.chat import credentials

                    personal_prompt, env, personal_secrets = await credentials.configure_cron(
                        session,
                        ctx,
                        bot=bot,
                        actor_id=actor.id,
                        job_id=job.id,
                        extra=personal_prompt,
                        env=env,
                    )
                # 定时执行同样带着本人的业务系统令牌与个人工具凭据，出站闸门一视同仁。
                ctx.secrets = collect_secrets(env) | personal_secrets
```

`coreman/runtime/scheduler/reaper.py`：

1. 导入 `from coreman.core.personal_credentials import service as credential_requests`。
2. `run_cleanup` 返回字典里 `"interactions_expired"` 之后加：

```python
        "credential_requests_expired": await _run(factory, credential_requests.expire_due, now),
```

3. `run_retention` 的 `jobs` 里加 `"old_credential_requests": credential_requests.cleanup,`。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest tests/integration/test_credential_cron.py tests/integration/test_credential_reaper.py tests/integration/test_cron_handler.py tests/integration/test_scheduler_reaper.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add coreman/runtime/worker/cron_handler.py coreman/runtime/scheduler/reaper.py tests/integration/test_credential_cron.py tests/integration/test_credential_reaper.py
git commit -m "feat(credentials): inject into scheduled runs and expire stale requests"
```

---

### Task 13: 前端「我的凭证」与表单页

**Files:**
- Create: `web/src/api/personalCredentials.ts`
- Create: `web/src/views/CredentialRequestView.vue`
- Create: `web/src/views/MyCredentialsView.vue`
- Modify: `web/src/router/index.ts`（`my-wecom` 路由之后加两条）
- Modify: `web/src/layouts/AdminLayout.vue`（菜单项、分组、图标、`activePath`）
- Modify: `web/src/i18n/zh.ts`、`en.ts`、`ja.ts`（`myWecom` 块之后加 `myCredentials`、`credentialRequest`；`menu` 加 `myCredentials`）
- Test: `web/tests/MyCredentialsView.spec.ts`、`web/tests/CredentialRequestView.spec.ts`

**Interfaces:**
- Consumes: Task 7 的 `/api/me/*` 接口
- Produces: 路由 `/my-credentials`、`/my-credentials/requests/:id`（后者就是 `service.PAGE_PATH` 指向的页面）

- [ ] **Step 1: API 客户端**

`web/src/api/personalCredentials.ts`：

```ts
import { call, http } from '@/api/client'

export interface CredentialField { key: string; label: string; secret: boolean; placeholder: string }

/** AI 员工发起的一次索取。不含任何值。 */
export interface CredentialRequest {
  id: string; bot_name: string; platform: string; purpose: string; fields: CredentialField[]
  status: 'open' | 'submitted' | 'expired' | 'cancelled'; expires_at: string; security_note: string
}

/** 本人保存的一条凭证：密文字段的 value 恒为 null。 */
export interface PersonalCredential {
  bot_id: string; bot_name: string; env_key: string; label: string; secret: boolean
  value: string | null; updated_at: string; last_used_at: string | null
}

const requests = '/api/me/credential-requests'
const root = '/api/me/credentials'
export const personalCredentials = {
  request: (id: string) => call<CredentialRequest>(http.get(`${requests}/${id}`)),
  submit: (id: string, values: Record<string, string>) =>
    call<{ status: string; keys: string[]; message: string }>(http.post(`${requests}/${id}/submit`, { values })),
  list: () => call<PersonalCredential[]>(http.get(root)),
  update: (botId: string, key: string, value: string) =>
    call<PersonalCredential>(http.put(`${root}/${botId}/${key}`, { value })),
  remove: (botId: string, key: string) => call<null>(http.delete(`${root}/${botId}/${key}`)),
}
```

- [ ] **Step 2: 写失败测试**

`web/tests/CredentialRequestView.spec.ts`：

```ts
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, expect, it, vi } from 'vitest'
vi.mock('vue-router', async (orig) => ({ ...(await orig<typeof import('vue-router')>()), useRoute: () => ({ params: { id: 'r1' } }) }))
vi.mock('@/api/personalCredentials', () => ({ personalCredentials: { request: vi.fn(), submit: vi.fn(), list: vi.fn(), update: vi.fn(), remove: vi.fn() } }))
import { personalCredentials, type CredentialRequest } from '@/api/personalCredentials'
import CredentialRequestView from '@/views/CredentialRequestView.vue'
import { i18n } from '@/i18n'

const open: CredentialRequest = {
  id: 'r1', bot_name: 'Demo 助手', platform: 'wecom', purpose: '查询你在 Demo 系统里的订单',
  fields: [
    { key: 'DEMO_USERNAME', label: '账号', secret: false, placeholder: '' },
    { key: 'DEMO_PIN', label: 'PIN', secret: true, placeholder: '' },
  ],
  status: 'open', expires_at: '2026-10-03T02:00:00Z', security_note: '🔒 安全说明：不会发送给 AI 模型。',
}
const render = () => mount(CredentialRequestView, { global: { plugins: [ElementPlus, i18n] } })

beforeEach(() => {
  vi.mocked(personalCredentials.request).mockReset().mockResolvedValue(open)
  vi.mocked(personalCredentials.submit).mockReset().mockResolvedValue({ status: 'saved', keys: ['DEMO_PIN', 'DEMO_USERNAME'], message: '' })
})

it('shows who asks, why, a password box for secret fields and the security note', async () => {
  const wrapper = render(); await flushPromises()
  expect(wrapper.text()).toContain('Demo 助手')
  expect(wrapper.text()).toContain('查询你在 Demo 系统里的订单')
  expect(wrapper.text()).toContain('不会发送给 AI 模型')
  expect(wrapper.get('input[data-test="field-DEMO_PIN"]').attributes('type')).toBe('password')
  expect(wrapper.get('input[data-test="field-DEMO_USERNAME"]').attributes('type')).toBe('text')
  wrapper.unmount()
})

it('submits every field once and then shows the saved state', async () => {
  const wrapper = render(); await flushPromises()
  await wrapper.get('input[data-test="field-DEMO_USERNAME"]').setValue('alice')
  await wrapper.get('input[data-test="field-DEMO_PIN"]').setValue('pin-778899')
  await wrapper.get('form').trigger('submit'); await flushPromises()
  expect(personalCredentials.submit).toHaveBeenCalledWith('r1', { DEMO_USERNAME: 'alice', DEMO_PIN: 'pin-778899' })
  expect(wrapper.find('[data-test="saved"]').exists()).toBe(true)
  expect(wrapper.html()).not.toContain('pin-778899')
  wrapper.unmount()
})

it('refuses to submit with an empty field', async () => {
  const wrapper = render(); await flushPromises()
  await wrapper.get('input[data-test="field-DEMO_USERNAME"]').setValue('alice')
  await wrapper.get('form').trigger('submit'); await flushPromises()
  expect(personalCredentials.submit).not.toHaveBeenCalled()
  expect(wrapper.get('[data-test="error"]').text()).toContain('PIN')
  wrapper.unmount()
})

it('shows a closed form without inputs', async () => {
  vi.mocked(personalCredentials.request).mockResolvedValue({ ...open, status: 'expired' })
  const wrapper = render(); await flushPromises()
  expect(wrapper.find('[data-test="closed"]').exists()).toBe(true)
  expect(wrapper.find('form').exists()).toBe(false)
  wrapper.unmount()
})
```

`web/tests/MyCredentialsView.spec.ts`：

```ts
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { beforeEach, expect, it, vi } from 'vitest'
vi.mock('@/api/personalCredentials', () => ({ personalCredentials: { list: vi.fn(), update: vi.fn(), remove: vi.fn(), request: vi.fn(), submit: vi.fn() } }))
import { personalCredentials, type PersonalCredential } from '@/api/personalCredentials'
import MyCredentialsView from '@/views/MyCredentialsView.vue'
import { i18n } from '@/i18n'
import { MENU } from '@/layouts/AdminLayout.vue'

const rows: PersonalCredential[] = [
  { bot_id: 'b1', bot_name: 'Demo 助手', env_key: 'DEMO_USERNAME', label: '账号', secret: false, value: 'alice', updated_at: '2026-10-03T01:00:00Z', last_used_at: null },
  { bot_id: 'b1', bot_name: 'Demo 助手', env_key: 'DEMO_PIN', label: 'PIN', secret: true, value: null, updated_at: '2026-10-03T01:00:00Z', last_used_at: '2026-10-03T02:00:00Z' },
]
const render = () => mount(MyCredentialsView, { global: { plugins: [ElementPlus, i18n] } })

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(personalCredentials.list).mockReset().mockResolvedValue(rows)
  vi.mocked(personalCredentials.update).mockReset().mockResolvedValue(rows[1])
  vi.mocked(personalCredentials.remove).mockReset().mockResolvedValue(null)
})

it('is a personal page every member can open', () => {
  expect(MENU.find(item => item.path === '/my-credentials')?.roles).toBeUndefined()
})

it('groups by AI employee, shows plain fields and hides secret ones', async () => {
  const wrapper = render(); await flushPromises()
  expect(wrapper.text()).toContain('Demo 助手')
  expect(wrapper.text()).toContain('alice')
  expect(wrapper.text()).toContain('已设置（不显示）')
  wrapper.unmount()
})

it('updates a secret field through a password prompt', async () => {
  const prompt = vi.spyOn(ElMessageBox, 'prompt').mockResolvedValue({ value: 'pin-222333', action: 'confirm' } as never)
  const wrapper = render(); await flushPromises()
  await wrapper.get('[data-test="update-DEMO_PIN"]').trigger('click'); await flushPromises()
  expect(prompt.mock.calls[0][2]).toMatchObject({ inputType: 'password' })
  expect(personalCredentials.update).toHaveBeenCalledWith('b1', 'DEMO_PIN', 'pin-222333')
  wrapper.unmount()
})

it('deletes after confirmation', async () => {
  vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
  const wrapper = render(); await flushPromises()
  await wrapper.get('[data-test="delete-DEMO_PIN"]').trigger('click'); await flushPromises()
  expect(personalCredentials.remove).toHaveBeenCalledWith('b1', 'DEMO_PIN')
  wrapper.unmount()
})
```

- [ ] **Step 3: 运行，确认失败**

Run（在 `web/` 下）: `npx vitest run tests/CredentialRequestView.spec.ts tests/MyCredentialsView.spec.ts`
Expected: FAIL，找不到视图文件

- [ ] **Step 4: 实现视图**

`web/src/views/CredentialRequestView.vue`：

```vue
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { personalCredentials, type CredentialRequest } from '@/api/personalCredentials'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const route = useRoute()
const id = computed(() => String(route.params.id))
const form = ref<CredentialRequest | null>(null)
const values = reactive<Record<string, string>>({})
const error = ref('')
const busy = ref(false)
const saved = ref(false)

async function load() {
  busy.value = true
  try { form.value = await personalCredentials.request(id.value); error.value = '' }
  catch (e) { error.value = errorMessage(e) || t('credentialRequest.loadError') }
  finally { busy.value = false }
}
/** 提交成功后立即清空输入框：值只在这一次请求里离开浏览器。 */
async function submit() {
  if (!form.value) return
  const missing = form.value.fields.find(f => !(values[f.key] ?? '').trim())
  if (missing) { error.value = t('credentialRequest.required', { label: missing.label }); return }
  busy.value = true
  try {
    await personalCredentials.submit(id.value, Object.fromEntries(form.value.fields.map(f => [f.key, values[f.key]])))
    for (const key of Object.keys(values)) values[key] = ''
    saved.value = true
    error.value = ''
  } catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
onMounted(load)
</script>
<template>
  <section class="credential-request">
    <h1>{{ t('credentialRequest.title') }}</h1>
    <el-alert v-if="error" data-test="error" :title="error" type="error" :closable="false" />
    <el-result v-if="saved" data-test="saved" icon="success" :title="t('credentialRequest.saved')" :sub-title="t('credentialRequest.savedHint')" />
    <template v-else-if="form">
      <p class="muted">{{ t('credentialRequest.from', { bot: form.bot_name }) }}</p>
      <p><strong>{{ t('credentialRequest.purpose') }}</strong> {{ form.purpose }}</p>
      <el-alert v-if="form.status !== 'open'" data-test="closed" :title="t(`credentialRequest.closed.${form.status}`)" type="warning" :closable="false" />
      <el-form v-else label-position="top" @submit.prevent="submit">
        <el-form-item v-for="field in form.fields" :key="field.key" :label="field.label">
          <el-input v-model="values[field.key]" :data-test="'field-' + field.key" :type="field.secret ? 'password' : 'text'" :show-password="field.secret" :placeholder="field.placeholder" :maxlength="4096" autocomplete="off" />
        </el-form-item>
        <p class="muted">{{ t('credentialRequest.expiresAt', { time: formatDateTime(form.expires_at) }) }}</p>
        <el-button type="primary" native-type="submit" :loading="busy">{{ t('credentialRequest.submit') }}</el-button>
      </el-form>
      <p class="note">{{ form.security_note }}</p>
    </template>
  </section>
</template>
<style scoped>
.credential-request { max-width: 560px; }
.muted, .note { color: var(--el-text-color-secondary); }
.note { font-size: 12px; margin-top: 16px; }
</style>
```

`web/src/views/MyCredentialsView.vue`：

```vue
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { personalCredentials, type PersonalCredential } from '@/api/personalCredentials'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const rows = ref<PersonalCredential[]>([])
const error = ref('')
const busy = ref(false)
const groups = computed(() => {
  const byBot = new Map<string, { id: string; bot: string; items: PersonalCredential[] }>()
  for (const row of rows.value) {
    const group = byBot.get(row.bot_id) ?? { id: row.bot_id, bot: row.bot_name, items: [] }
    group.items.push(row)
    byBot.set(row.bot_id, group)
  }
  return [...byBot.values()]
})
async function load() {
  busy.value = true
  try { rows.value = await personalCredentials.list(); error.value = '' }
  catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
/** 取消确认框不算失败。 */
async function act(action: () => Promise<unknown>, done: string) {
  busy.value = true
  try { await action(); ElMessage.success(t(done)); await load() }
  catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorMessage(e)) }
  finally { busy.value = false }
}
const name = (row: PersonalCredential) => row.label || row.env_key
const update = (row: PersonalCredential) => act(async () => {
  const { value } = await ElMessageBox.prompt(
    t('myCredentials.updatePrompt'),
    t('myCredentials.updateTitle', { label: name(row) }),
    { inputType: row.secret ? 'password' : 'text', inputValidator: (v: string) => !!v && !!v.trim() },
  )
  await personalCredentials.update(row.bot_id, row.env_key, value)
}, 'myCredentials.updated')
const remove = (row: PersonalCredential) => act(async () => {
  await ElMessageBox.confirm(t('myCredentials.confirmDelete', { label: name(row), key: row.env_key }), t('myCredentials.delete'), { type: 'warning' })
  await personalCredentials.remove(row.bot_id, row.env_key)
}, 'myCredentials.deleted')
onMounted(load)
</script>
<template>
  <section class="credentials">
    <h1>{{ t('menu.myCredentials') }}</h1>
    <p>{{ t('myCredentials.intro') }}</p>
    <el-button :loading="busy" @click="load">{{ t('myCredentials.refresh') }}</el-button>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-empty v-if="!busy && !rows.length" :description="t('myCredentials.empty')" />
    <article v-for="group in groups" :key="group.id" class="group">
      <h2>{{ group.bot }}</h2>
      <el-table :data="group.items" row-key="env_key">
        <el-table-column :label="t('myCredentials.label')" prop="label" />
        <el-table-column :label="t('myCredentials.key')">
          <template #default="{ row }"><code>{{ row.env_key }}</code></template>
        </el-table-column>
        <el-table-column :label="t('myCredentials.value')">
          <template #default="{ row }">{{ row.secret ? t('myCredentials.hidden') : row.value }}</template>
        </el-table-column>
        <el-table-column :label="t('myCredentials.updatedAt')">
          <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column :label="t('myCredentials.lastUsedAt')">
          <template #default="{ row }">{{ row.last_used_at ? formatDateTime(row.last_used_at) : t('myCredentials.never') }}</template>
        </el-table-column>
        <el-table-column width="160">
          <template #default="{ row }">
            <el-button link type="primary" :data-test="'update-' + row.env_key" @click="update(row)">{{ t('myCredentials.update') }}</el-button>
            <el-button link type="danger" :data-test="'delete-' + row.env_key" @click="remove(row)">{{ t('myCredentials.delete') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
    </article>
  </section>
</template>
<style scoped>
.group { margin-top: 24px; }
</style>
```

写完运行 `npx eslint --fix src/views/CredentialRequestView.vue src/views/MyCredentialsView.vue`，让模板属性换行符合仓库的 eslint 规则。

- [ ] **Step 5: 路由、菜单与文案**

`web/src/router/index.ts`，在 `my-wecom` 那一行之后：

```ts
        { path: 'my-credentials', name: 'my-credentials', component: () => import('@/views/MyCredentialsView.vue') },
        { path: 'my-credentials/requests/:id', name: 'credential-request', component: () => import('@/views/CredentialRequestView.vue') },
```

`web/src/layouts/AdminLayout.vue`：
- `MENU` 里 `myWecom` 之后加 `{ key: 'myCredentials', path: '/my-credentials' },`；
- 图标导入加 `Lock`，`icons` 加 `myCredentials: Lock`；
- `groups` 的 `collaboration.keys` 在 `'myWecom'` 之后加 `'myCredentials'`；
- `activePath` 改为：

```ts
const activePath = computed(() => route.path.startsWith('/bots/') ? '/bots' : route.path.startsWith('/my-credentials/') ? '/my-credentials' : route.path)
```

`web/src/i18n/zh.ts`（`myWecom` 块之后，`menu` 里加 `myCredentials: "我的凭证",`）：

```ts
  myCredentials: {
    intro: "这里是你在各个 AI 员工里保存的个人凭证。它们加密保存，只在你本人与对应 AI 员工对话、或运行你创建的定时任务时注入；密文字段的内容任何人都看不到，包括管理员。",
    refresh: "刷新",
    empty: "还没有保存个人凭证。AI 员工需要时会给你发安全表单。",
    label: "名称", key: "变量名", value: "内容", hidden: "已设置（不显示）",
    updatedAt: "更新时间", lastUsedAt: "最近使用", never: "未使用",
    update: "更新", delete: "删除",
    updateTitle: "更新「{label}」",
    updatePrompt: "输入新的内容，提交后加密保存，之后不会再显示。",
    confirmDelete: "删除「{label}」（{key}）？删除后 AI 员工需要时会重新向你索取。",
    updated: "已更新", deleted: "已删除",
  },
  credentialRequest: {
    title: "填写个人凭证",
    from: "来自 AI 员工「{bot}」",
    purpose: "用途：",
    submit: "加密提交",
    required: "请填写「{label}」",
    saved: "已保存",
    savedHint: "可以回到聊天里了，AI 员工会继续之前的任务。",
    expiresAt: "有效期至 {time}",
    loadError: "无法打开这张表单",
    closed: {
      submitted: "这张表单已经提交过了。",
      expired: "表单已过期，请让 AI 员工重新发起。",
      cancelled: "表单已失效，请让 AI 员工重新发起。",
    },
  },
```

`web/src/i18n/en.ts`（`menu` 里加 `myCredentials: "My credentials",`）：

```ts
  myCredentials: {
    intro: "Personal credentials you saved for each AI employee. They are stored encrypted and injected only when you talk to that AI employee or run a scheduled task you created. Nobody, administrators included, can see the content of secret fields.",
    refresh: "Refresh",
    empty: "No personal credentials yet. An AI employee sends you a secure form when it needs one.",
    label: "Name", key: "Variable", value: "Value", hidden: "Set (hidden)",
    updatedAt: "Updated", lastUsedAt: "Last used", never: "Never",
    update: "Update", delete: "Delete",
    updateTitle: "Update “{label}”",
    updatePrompt: "Enter the new value. It is stored encrypted and never shown again.",
    confirmDelete: "Delete “{label}” ({key})? The AI employee will ask you again when it needs it.",
    updated: "Updated", deleted: "Deleted",
  },
  credentialRequest: {
    title: "Personal credential",
    from: "Requested by AI employee “{bot}”",
    purpose: "Purpose:",
    submit: "Submit encrypted",
    required: "Please fill in “{label}”",
    saved: "Saved",
    savedHint: "You can go back to the chat. The AI employee will continue the task.",
    expiresAt: "Valid until {time}",
    loadError: "Cannot open this form",
    closed: {
      submitted: "This form has already been submitted.",
      expired: "This form has expired. Ask the AI employee to send a new one.",
      cancelled: "This form is no longer valid. Ask the AI employee to send a new one.",
    },
  },
```

`web/src/i18n/ja.ts`（`menu` 里加 `myCredentials: "マイ認証情報",`）：

```ts
  myCredentials: {
    intro: "AI 社員ごとに保存した個人の認証情報です。暗号化して保存され、本人がその AI 社員と会話するとき、または本人が作成した定期タスクの実行時にのみ使われます。秘密の項目は管理者を含め誰にも表示されません。",
    refresh: "更新",
    empty: "保存された個人の認証情報はありません。必要なときに AI 社員から安全なフォームが届きます。",
    label: "名前", key: "変数名", value: "内容", hidden: "設定済み（非表示）",
    updatedAt: "更新日時", lastUsedAt: "最終使用", never: "未使用",
    update: "更新", delete: "削除",
    updateTitle: "「{label}」を更新",
    updatePrompt: "新しい内容を入力してください。暗号化して保存され、以後は表示されません。",
    confirmDelete: "「{label}」（{key}）を削除しますか？必要なときに AI 社員から再度依頼されます。",
    updated: "更新しました", deleted: "削除しました",
  },
  credentialRequest: {
    title: "個人の認証情報の入力",
    from: "AI 社員「{bot}」からの依頼",
    purpose: "用途：",
    submit: "暗号化して送信",
    required: "「{label}」を入力してください",
    saved: "保存しました",
    savedHint: "チャットに戻ってください。AI 社員が作業を続けます。",
    expiresAt: "{time} まで有効",
    loadError: "このフォームを開けません",
    closed: {
      submitted: "このフォームは送信済みです。",
      expired: "フォームの有効期限が切れました。AI 社員に再依頼してください。",
      cancelled: "このフォームは無効になりました。AI 社員に再依頼してください。",
    },
  },
```

- [ ] **Step 6: 运行前端检查**

Run（在 `web/` 下）: `npx vitest run && npm run lint && npm run build`
Expected: 全部通过（包括 `i18n.spec.ts` 的英文覆盖检查与 `AdminLayout.spec.ts`）

- [ ] **Step 7: Commit**

```bash
git add web/src/api/personalCredentials.ts web/src/views/CredentialRequestView.vue web/src/views/MyCredentialsView.vue web/src/router/index.ts web/src/layouts/AdminLayout.vue web/src/i18n web/tests/CredentialRequestView.spec.ts web/tests/MyCredentialsView.spec.ts
git commit -m "feat(web): add the secure credential form and my credentials page"
```

---

### Task 14: 文档与全量验证

**Files:**
- Create: `docs/features/personal-credentials.md`
- Modify: `docs/glossary.md`（在「请求级环境变量」之后加词条）
- Modify: `README.md`（「接入与能力」一行末尾加链接）
- Modify: `CHANGELOG.md`（`## [Unreleased]` → `### Added` 加一条，英文）
- Modify: `docs/superpowers/specs/2026-10-03-personal-credentials.md`（状态行改为「已实现，待真机验证」）

- [ ] **Step 1: 写功能文档**

`docs/features/personal-credentials.md` 必须包含下列小节，内容取自设计文档，用通用示例（`DEMO_*`、`example.com`）：

1. **是什么**：agent 需要账号、密码或 API Key 时向本人索取；按（AI 员工, 用户）加密保存；只在本人触发的轮次注入。
2. **用户怎么用**：
   - 飞书：私聊收到表单卡片，填写后点「加密提交」；
   - 企业微信：点链接，登录后在网页里填写；
   - 两边都在「我的凭证」页查看、更新、删除。
3. **注入范围**：第 8.1 节的表格。
4. **agent 怎么索取**：`$COREMAN_CREDENTIAL_URL` / `$COREMAN_CREDENTIAL_TOKEN`、请求体、各响应码（第 5.2 节表格）。
5. **部署前提**：
   - 企业微信需要一个启用了「登录」能力的自建应用；
   - 手机要能访问 `PUBLIC_BASE_URL`；
   - 飞书有登录应用时卡片多一个网页入口；
   - 需要数据库迁移 `0053`；运行节点不用升级。
6. **安全边界**：
   - 能保证与不能保证的部分，原样采用第 1 节与第 12 节；
   - 明确写出 agent 能通过 shell 读到自己的环境变量。
7. **数据保留**：请求 90 天；凭证保留到本人删除，或 AI 员工、用户被删除。

- [ ] **Step 2: 术语、README、CHANGELOG**

`docs/glossary.md` 新词条：

```markdown
**个人凭证（personal credential）**
：AI 员工经安全表单向本人索取的账号、密码或 API Key，按（AI 员工, 用户）逐条加密保存（AAD 绑定行身份），只在本人触发的轮次（本人私聊、群聊中本人 @、本人创建的定时任务、提交后的续接轮）作为环境变量注入。提交不经过聊天；任何接口都不返回密文字段的值。见 [个人凭证](features/personal-credentials.md)。
```

`README.md` 的「接入与能力」列表末尾加 ` · [个人凭证](docs/features/personal-credentials.md)`。

`CHANGELOG.md` 在 `### Added` 下最前面加：

```markdown
- Personal credentials. When a task needs the speaker's own account, password or API key, the agent calls `POST /api/runtime/credentials/requests` with the turn's `COREMAN_CREDENTIAL_TOKEN` and the variable names it wants. Feishu users get a Card JSON 2.0 form with password inputs in their private chat; WeCom users get a link to an OAuth-protected page, since WeCom template cards have no text input. The Feishu gateway encrypts the submitted values before the callback is written to `inbound_events` or `tasks`, and the worker removes that sealed copy once it is processed. Values are stored one row per (AI employee, member, variable), each encrypted with the master key and bound to that row, and no API returns them. They are injected as environment variables only into turns the member triggers: their private chat, their @-mentions in groups, scheduled tasks they created, and the turn that resumes the original conversation after they submit. Secret values are always redacted from replies and chat logs whatever the variable is called. Members can view, update and delete their credentials under "我的凭证" (My credentials). The agent can still read its own environment through a shell; see the feature doc for the boundary. Requires database migration `0053`; runtime nodes need no upgrade. See `docs/features/personal-credentials.md`.
```

- [ ] **Step 3: 全量验证**

依次运行，全部通过才算完成：

```bash
uv run ruff check .
```

```bash
uv run mypy coreman
```

```bash
uv run pytest -q
```

```bash
cd web && npx vitest run && npm run lint && npm run build
```

- [ ] **Step 4: 公开仓自查**

按维护者本机的协作约定，对 `git diff origin/main` 的新增行做敏感信息检查（关键词清单不进仓库），有命中就改成通用表述。

- [ ] **Step 5: Commit**

```bash
git add docs/features/personal-credentials.md docs/glossary.md README.md CHANGELOG.md docs/superpowers/specs/2026-10-03-personal-credentials.md
git commit -m "docs(credentials): document personal credentials"
```

---

## 真机验证清单（合并后由人执行，不在自动化范围内）

- 飞书桌面端与手机端：表单卡片能正常渲染，`password` 输入框显示为「•」，提交后卡片变成「已保存」，随后续接轮回复出现在原会话。
- 飞书群聊：本人 @ 机器人触发索取，表单出现在私聊、群里只有一句提示；群里其他人发言时拿不到本人的凭证。
- 企业微信手机端：点链接后 OAuth 登录、填写、提交，续接回复以主动消息出现在原会话；别人打开同一链接看到 403。
- Codex 后端：agent 在 shell 里能读到名字含 `KEY`、`SECRET`、`TOKEN` 的变量；如果读不到，在 Codex 驱动里显式放开 `shell_environment_policy`（单独提交）。
- 「我的凭证」页：更新、删除生效，下一轮注入的是新值或不再注入。
