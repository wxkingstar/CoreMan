import uuid

from coreman.api.bot_permissions import (
    can_create_bot,
    can_delete_bot,
    can_edit_bot,
    can_manage_members,
    can_reassign_team,
    can_switch_relay,
    can_toggle_bot,
    can_view_env_full,
    can_view_sensitive,
    permissions_for,
    relay_allowed_for_bot,
)
from coreman.core.db.models import Bot, RelayServer, User

TEAM_A, TEAM_B = uuid.uuid4(), uuid.uuid4()


def _user(role: str, team: uuid.UUID | None = TEAM_A) -> User:
    return User(id=uuid.uuid4(), display_name=role, role=role, team_id=team)


def _bot(creator: User, team: uuid.UUID | None = TEAM_A) -> Bot:
    return Bot(
        id=uuid.uuid4(),
        bot_key="b",
        platform="wecom",
        name="b",
        created_by=creator.id,
        team_id=team,
        model="m",
        working_dir="/d",
        credentials_enc="e",
    )


def test_creator_admin_and_outsider() -> None:
    creator, admin, outsider = _user("member"), _user("member"), _user("member", TEAM_B)
    bot = _bot(creator)
    members = {admin.id}
    for u in (creator, admin):
        assert (
            can_view_sensitive(u, bot, members)
            and can_edit_bot(u, bot, members)
            and can_switch_relay(u, bot, members)
            and can_toggle_bot(u, bot, members)
        )
    assert can_delete_bot(creator, bot) and can_manage_members(creator, bot)
    assert not can_delete_bot(admin, bot) and not can_manage_members(admin, bot)
    assert not any(
        [
            can_view_sensitive(outsider, bot, members),
            can_edit_bot(outsider, bot, members),
            can_switch_relay(outsider, bot, members),
            can_toggle_bot(outsider, bot, members),
        ]
    )
    assert (
        permissions_for(creator, bot, members).role == "creator"
        and permissions_for(admin, bot, members).role == "admin"
        and permissions_for(outsider, bot, members).role is None
    )


def test_team_lead_same_team_only() -> None:
    creator, lead, other_lead = _user("member"), _user("team_lead"), _user("team_lead", TEAM_B)
    bot = _bot(creator)
    assert can_view_sensitive(lead, bot, set()) and can_toggle_bot(lead, bot, set())
    assert (
        not can_edit_bot(lead, bot, set())
        and not can_switch_relay(lead, bot, set())
        and not can_delete_bot(lead, bot)
    )
    assert not can_view_sensitive(other_lead, bot, set())
    assert not can_toggle_bot(other_lead, bot, set())
    assert not can_reassign_team(lead)


def test_committee_and_admin() -> None:
    creator, committee, admin = (
        _user("member"),
        _user("ai_committee", None),
        _user("platform_admin", None),
    )
    bot = _bot(creator)
    for u in (committee, admin):
        assert can_switch_relay(u, bot, set()) and can_toggle_bot(u, bot, set())
        assert can_reassign_team(u)
        # 敏感字段不旁路
        assert not can_edit_bot(u, bot, set()) and not can_view_sensitive(u, bot, set())
    assert can_view_env_full(committee) and not can_view_env_full(admin)
    assert can_delete_bot(admin, bot) and can_manage_members(admin, bot)
    assert not can_delete_bot(committee, bot) and not can_manage_members(committee, bot)


def test_create_requires_team_for_member() -> None:
    assert not can_create_bot(_user("member", None))
    assert can_create_bot(_user("member"))
    assert can_create_bot(_user("ai_committee", None))


def test_relay_team_policy() -> None:
    member, committee = _user("member"), _user("ai_committee", None)
    public = RelayServer(
        name="p", host="h", clawrelay_port=1, model_provider="claude", team_id=None
    )
    team_b = RelayServer(
        name="b", host="h", clawrelay_port=2, model_provider="claude", team_id=TEAM_B
    )
    assert relay_allowed_for_bot(member, public, bot_team_id=TEAM_A, creator_team_id=TEAM_A)
    assert not relay_allowed_for_bot(member, team_b, bot_team_id=TEAM_A, creator_team_id=TEAM_A)
    assert relay_allowed_for_bot(member, team_b, bot_team_id=TEAM_A, creator_team_id=TEAM_B)
    assert relay_allowed_for_bot(committee, team_b, bot_team_id=TEAM_A, creator_team_id=TEAM_A)


def test_teamless_bot_grants_no_team_lead_powers() -> None:
    """bot 未归属团队：team_lead 无论自己有没有团队都不算「同团队」，manager 仍能启停。"""
    creator = _user("member")
    bot = _bot(creator, team=None)
    for lead in (_user("team_lead"), _user("team_lead", None)):
        assert not can_view_sensitive(lead, bot, set())
        assert not can_toggle_bot(lead, bot, set())
    for manager in (_user("ai_committee", None), _user("platform_admin", None)):
        assert can_toggle_bot(manager, bot, set())
