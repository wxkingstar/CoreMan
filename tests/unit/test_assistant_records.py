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
