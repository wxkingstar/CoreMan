import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from coreman.core.chat.rate_limit import (
    RATE_LIMIT_PATTERN,
    ago,
    allowed_providers,
    countdown,
    fmt_pct,
    is_rate_limited,
    pick_idle_server,
    quota_table,
    quota_warning,
)
from coreman.core.db.models import Bot, RelayServer, User

NOW = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)


def _relay(
    name: str,
    *,
    provider="claude",
    active=True,
    pct5=None,
    pct7=None,
    team=None,
    probed=None,
    visibility="all",
) -> RelayServer:  # type: ignore[no-untyped-def]
    r = RelayServer(name=name, host=f"{name}.test", clawrelay_port=80, model_provider=provider)
    r.id = uuid.uuid4()
    r.is_active = active
    r.visibility = visibility
    r.team_id = team
    r.rate_limit_5h_used_pct = None if pct5 is None else Decimal(str(pct5))
    r.rate_limit_7d_used_pct = None if pct7 is None else Decimal(str(pct7))
    r.rate_limit_5h_resets_at = NOW + timedelta(hours=2, minutes=30)
    r.rate_limit_7d_resets_at = NOW - timedelta(minutes=1)
    r.rate_limit_probed_at = probed or NOW - timedelta(minutes=3)
    return r


def test_pattern_and_helpers() -> None:
    assert is_rate_limited("blah You've hit your limit · resets 3pm")
    assert is_rate_limited("You've hit your weekly limit · resets Monday")
    assert not is_rate_limited("You've hit your limit - resets")
    assert RATE_LIMIT_PATTERN.pattern == r"You've hit your(?: \w+)? limit · resets"
    assert allowed_providers("claude") == {"claude", "minimax"} and allowed_providers("codex") == {
        "codex"
    }
    assert allowed_providers("minimax") == {"minimax"}
    assert (
        fmt_pct(None) == "-"
        and fmt_pct(Decimal("90.0")) == "🔴 90%"
        and fmt_pct(Decimal("70.5")) == "🟡 70.5%"
        and fmt_pct(Decimal("12")) == "🟢 12%"
    )
    assert (
        countdown(NOW + timedelta(hours=2, minutes=30), NOW) == "2h30m"
        and countdown(NOW - timedelta(seconds=1), NOW) == "已重置"
    )
    assert countdown(None, NOW) == "-" and countdown(NOW + timedelta(minutes=5), NOW) == "5m"
    assert (
        ago(NOW - timedelta(seconds=30), NOW) == "刚刚"
        and ago(NOW - timedelta(minutes=3), NOW) == "3 分钟前"
    )
    assert ago(NOW - timedelta(hours=1, minutes=5), NOW) == "1h5m 前" and ago(None, NOW) == "-"


def test_quota_table_verbatim() -> None:
    cur = _relay("claude01", pct5=95, pct7=40)
    other = _relay("claude02", pct5=10, pct7=75)
    off = _relay("claude03", active=False, pct5=0, pct7=0)
    table = quota_table([cur, other, off], current_id=cur.id, now=NOW)
    assert table == (
        "\n\n---\n**📊 服务器额度总览**\n\n"
        "| 运行时 | 模型 | 5小时额度 | 7天额度 |\n"
        "|------|------|-----------|---------|\n"
        "| claude01 ⭐ | claude | 🔴 95% | 🟢 40% |\n"
        "| ↳采集于 3 分钟前 | | ↳2h30m | ↳已重置 |\n"
        "| claude02 | claude | 🟢 10% | 🟡 75% |\n"
        "| ↳采集于 3 分钟前 | | ↳2h30m | ↳已重置 |\n"
        "\n"
        "_📡 使用率每 15 分钟采集一次，倒计时按当前时间实时推算。_"
    )


def test_quota_warning_variants() -> None:
    both = _relay("a", pct5=92, pct7=96)
    assert quota_warning(both, NOW) == (
        "\n\n---\n⚠️ **额度提醒**：本运行时额度即将耗尽（采集于 3 分钟前）\n"
        "- 5 小时额度：92%（约 2h30m 后重置）\n- 7 天额度：96%（约 已重置 后重置）"
    )
    only5 = _relay("b", pct5=90, pct7=10)
    assert quota_warning(only5, NOW) == (
        "\n\n---\n⚠️ **额度提醒**：本运行时 5 小时额度已用 90%"
        "（采集于 3 分钟前），约 **2h30m** 后重置"
    )
    only7 = _relay("c", pct5=10, pct7=95)
    assert quota_warning(only7, NOW).startswith(
        "\n\n---\n⚠️ **额度提醒**：本运行时 7 天额度已用 95%"
    )
    assert quota_warning(_relay("d", pct5=10, pct7=10), NOW) == ""
    assert quota_warning(_relay("e"), NOW) == ""


def test_pick_idle_server_rules() -> None:
    user = User(login_name="u", display_name="u")
    user.role = "member"
    user.team_id = None
    creator_team = uuid.uuid4()
    bot = Bot(
        bot_key="b",
        platform="wecom",
        name="b",
        created_by=uuid.uuid4(),
        model="vllm/claude-sonnet-4-6",
        working_dir="/d",
        credentials_enc="x",
    )
    bot.team_id = creator_team
    cur = _relay("cur", pct7=90)
    best = _relay("best", pct7=5)
    busy = _relay("busy", pct7=50)
    codex = _relay("codex", provider="codex", pct7=1)
    off = _relay("off", active=False, pct7=0)
    nodata = _relay("nodata")
    other_team = _relay("team", pct7=0, team=uuid.uuid4())
    own_team = _relay("own", pct7=3, team=creator_team)
    relays = [cur, best, busy, codex, off, nodata, other_team, own_team]
    assert (
        pick_idle_server(relays, current=cur, bot=bot, user=user, creator_team_id=creator_team)
        is own_team
    )
    own_team.rate_limit_7d_used_pct = Decimal("60")
    assert (
        pick_idle_server(relays, current=cur, bot=bot, user=user, creator_team_id=creator_team)
        is best
    )
    assert (
        pick_idle_server(
            [cur, codex, off, nodata], current=cur, bot=bot, user=user, creator_team_id=None
        )
        is None
    )


def test_pick_idle_server_respects_visibility() -> None:
    """`visibility='admins'` 的实例不能推荐给非 manager：切换执行时会被 422 挡回。"""
    bot = Bot(
        bot_key="b",
        platform="wecom",
        name="b",
        created_by=uuid.uuid4(),
        model="vllm/claude-sonnet-4-6",
        working_dir="/d",
        credentials_enc="x",
    )
    bot.team_id = None
    cur = _relay("cur", pct7=90)
    hidden = _relay("hidden", pct7=1, visibility="admins")
    plain = _relay("plain", pct7=50)
    relays = [cur, hidden, plain]
    member = User(login_name="m", display_name="m")
    member.role = "member"
    member.team_id = None
    assert (
        pick_idle_server(relays, current=cur, bot=bot, user=member, creator_team_id=None) is plain
    )
    manager = User(login_name="a", display_name="a")
    manager.role = "platform_admin"
    manager.team_id = None
    assert (
        pick_idle_server(relays, current=cur, bot=bot, user=manager, creator_team_id=None) is hidden
    )
