from coreman.api.permissions import role_at_least
from coreman.core.db.models import User


def test_platform_admin_at_least_every_role() -> None:
    user = User(display_name="x", role="platform_admin")
    assert role_at_least(user, "member")
    assert role_at_least(user, "team_lead")
    assert role_at_least(user, "ai_committee")
    assert role_at_least(user, "platform_admin")


def test_team_lead_at_least_member_but_not_ai_committee() -> None:
    user = User(display_name="x", role="team_lead")
    assert role_at_least(user, "member")
    assert role_at_least(user, "team_lead")
    assert not role_at_least(user, "ai_committee")
    assert not role_at_least(user, "platform_admin")


def test_member_at_least_only_member() -> None:
    user = User(display_name="x", role="member")
    assert role_at_least(user, "member")
    assert not role_at_least(user, "team_lead")
    assert not role_at_least(user, "ai_committee")
    assert not role_at_least(user, "platform_admin")
