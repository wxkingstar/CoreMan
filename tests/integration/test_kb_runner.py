"""调度员知识库的一次运行：收集 → 抽取 → 过滤 → 入库 → 编译 → 检查，节点和模型都用假的。"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from coreman.core.bots import rules_backup
from coreman.core.db.models import (
    Bot,
    BotSystemGrant,
    BusinessSystem,
    InboundEvent,
    KnowledgeItem,
    KnowledgeRun,
    RelayServer,
    SystemCatalog,
    Team,
    User,
    WorkTask,
)
from coreman.core.db.session import make_session_factory
from coreman.core.kb import extract, rounds, runner, wiki
from coreman.core.relay.agent_client import AgentError
from coreman.core.settings_store import SettingsStore
from tests.fakes.runtime_node import add_node_relay

NOW = datetime(2026, 11, 28, 18, 30, tzinfo=UTC)
LEAD_DIR, ALPHA_DIR = "/w/lead", "/w/alpha"
ALPHA_RULES = "# 订单助手\n\n## 职责\n负责订单查询和退款。\n\n## 交接\n合并代码要人确认。\n"


class Node:
    """假节点：按工作目录放文件，支持列目录和读文件；编译回合直接改这里的文件。"""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, str]] = {
            LEAD_DIR: {"AGENTS.md": "# 调度员\n\n人写的定位说明。\n"},
            ALPHA_DIR: {"AGENTS.md": ALPHA_RULES, "docs/refund.md": "# 退款\n原路退回。\n"},
        }

    async def call_agent(self, relay, cipher, operation, payload=None):
        payload = payload or {}
        if operation == "ping":
            return {"success": True, "workspace_protocol": 1}
        files = self.files.get(payload["working_dir"])
        if files is None:
            raise AgentError("Agent 连接失败（ConnectError）")
        if operation == "workspace-git-status":
            return {"success": True, "initialized": True, "remote": "https://example.com/r.git"}
        if operation == "workspace-list":
            prefix = f"{payload['path']}/" if payload["path"] else ""
            seen: dict[str, dict[str, Any]] = {}
            for path, text in files.items():
                if not path.startswith(prefix):
                    continue
                head, _, rest = path[len(prefix) :].partition("/")
                kind = "directory" if rest else "file"
                size = 0 if rest else len(text.encode())
                seen[head] = {"name": head, "path": prefix + head, "type": kind, "size": size}
            return {"success": True, "entries": list(seen.values())}
        if operation == "workspace-read":
            return {"success": True, "content": files[payload["path"]], "eof": True}
        raise AssertionError(operation)


class Model:
    """假模型：抽取回合每个来源出一条事实；编译回合把条目写进假节点的 docs/roles.md。"""

    def __init__(self, node: Node) -> None:
        self.node = node
        self.calls: list[dict[str, Any]] = []
        self.extract_extra: list[dict[str, str]] = []
        self.leak = ""
        self.fix_works = True
        self.skip_ids: set[int] = set()
        self.fail_with: Exception | None = None
        self.before_call: Any = None

    async def run_round(self, backend, *, system_prompt, content, working_dir, max_turns, **_):
        self.calls.append(
            {
                "provider": backend.provider,
                "dir": working_dir,
                "prompt": system_prompt,
                "content": content,
            }
        )
        if self.before_call is not None:
            await self.before_call(len(self.calls))
        if self.fail_with is not None:
            raise self.fail_with
        if system_prompt == extract.SYSTEM_PROMPT:
            assert working_dir == extract.SCRATCH_DIR
            refs = re.search(r"可用的来源标识：(.+)", content).group(1).split("、")  # type: ignore[union-attr]
            items = [
                {
                    "kind": "process",
                    "topic": "交接",
                    "text": f"来自 {ref} 的做法：合并前要人确认。",
                    "confidence": "stated",
                    "source": ref,
                }
                for ref in refs
            ]
            extra = [{**item, "source": refs[0]} for item in self.extract_extra]
            self.extract_extra = []
            return json.dumps(items + extra, ensure_ascii=False)
        files = self.node.files[working_dir]
        if system_prompt == wiki.FIX_PROMPT:
            if self.fix_works:
                files["docs/roles.md"] = files["docs/roles.md"].replace(self.leak, "")
            return "完成"
        assert system_prompt == wiki.SYSTEM_PROMPT and working_dir == LEAD_DIR
        lines = [files.get("docs/roles.md", "# 职责")]
        for line in content.splitlines():
            if line.startswith("{"):
                row = json.loads(line)
                if int(row["id"].removeprefix("K-")) not in self.skip_ids:
                    lines.append(f"- {row['text']}（{row['source']}，{row['id']}）")
        if self.leak:
            lines.append(self.leak)
        files["docs/roles.md"] = "\n".join(lines)
        return "写好了。\n<摘要>改了 docs/roles.md</摘要>"

    def of(self, prompt: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["prompt"] == prompt]


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> Node:
    fake = Node()
    monkeypatch.setattr(rules_backup, "call_agent", fake.call_agent)
    return fake


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch, node: Node) -> Model:
    fake = Model(node)
    monkeypatch.setattr(rounds, "run_round", fake.run_round)
    return fake


def task(code: str, source: str, **values: Any) -> WorkTask:
    values.setdefault("status", "done")
    values.setdefault("closed_at", NOW - timedelta(hours=3))
    return WorkTask(
        code=code,
        title=f"任务 {code}",
        source=source,
        summary="做完了",
        brief="把订单同步接上",
        requester_name="王小明",
        **values,
    )


def message(bot_id: uuid.UUID, chat_id: str, sender: str, chat_type: str = "group") -> InboundEvent:
    return InboundEvent(
        bot_id=bot_id,
        platform="feishu",
        platform_msg_id=f"om_{uuid.uuid4().hex}",
        kind="message",
        chat_type=chat_type,
        chat_id=chat_id,
        sender_platform_user_id=sender,
        payload={},
        reply_context={},
    )


async def seed(engine: AsyncEngine) -> SettingsStore:
    factory = make_session_factory(engine)
    async with factory() as session:
        team = Team(slug="ops", name_zh="运营")
        session.add(team)
        await session.flush()
        owner = User(
            login_name="owner",
            display_name="王小明",
            role="member",
            source="sync",
            position="运营负责人",
            team_id=team.id,
            email="owner@example.com",
            mobile="13800138000",
        )
        service = User(login_name="svc", display_name="服务账号", role="member", source="service")
        session.add_all([owner, service])
        await session.flush()
        claude = await add_node_relay(session, health_status="healthy")
        node_id = claude.runtime_node_id
        session.add(
            RelayServer(
                runtime_node_id=node_id,
                name="node/codex",
                model_provider="codex",
                health_status="healthy",
            )
        )

        def bot(key: str, name: str, working_dir: str, **values: Any) -> Bot:
            return Bot(
                bot_key=key,
                name=name,
                platform="feishu",
                created_by=owner.id,
                relay_server_id=claude.id,
                model="claude-sonnet-4-6",
                working_dir=working_dir,
                credentials_enc="unused",
                **values,
            )

        lead = bot("lead", "调度员", LEAD_DIR, group_role="dispatcher")
        alpha = bot("alpha", "订单助手", ALPHA_DIR, description="查订单、做退款")
        session.add_all([lead, alpha, bot("gone", "停用的", "/w/gone", enabled=False)])
        await session.flush()
        session.add(BusinessSystem(key="orders", name="订单系统", description="管订单和退款"))
        await session.flush()
        session.add(
            SystemCatalog(
                system_key="orders",
                spec_url="https://orders.example.com/openapi.json",
                status="ok",
                compiled={
                    "guide": "先查再改",
                    "modules": [{"name": "refund", "display_name": "退款", "operations": ["a"]}],
                    "operations": {"a": {"summary": "查退款单", "risk": "read"}},
                },
            )
        )
        session.add(BotSystemGrant(bot_id=alpha.id, system_key="orders"))
        root = task("T-1", "web")
        session.add(root)
        await session.flush()
        session.add_all(
            [
                task("T-2", "dispatch", chat_id="oc_group", platform="feishu"),
                # 只有一个人说过话的群、私聊：都不算多人群里发起的。
                task("T-3", "dispatch", chat_id="oc_solo", platform="feishu"),
                task("T-4", "dispatch", chat_id="oc_private", platform="feishu"),
                # 业务系统任务的镜像不读；没做完的不读。
                task("T-5", "api"),
                task("T-6", "web", status="todo", closed_at=None),
                # 子任务跟着根任务走。
                task("T-7", "agent", root_id=root.id, parent_id=root.id, assignee_bot_id=alpha.id),
                task("T-8", "agent"),
                message(lead.id, "oc_group", "u1"),
                message(lead.id, "oc_group", "u2"),
                message(lead.id, "oc_solo", "u1"),
                message(lead.id, "oc_solo", "u1"),
                message(lead.id, "oc_private", "u1", "single"),
                message(lead.id, "oc_private", "u2", "single"),
            ]
        )
        await session.commit()
    store = SettingsStore(factory)
    await runner.configure(factory, store, bot_key="lead", enabled=True)
    return store


async def items(engine: AsyncEngine) -> list[KnowledgeItem]:
    async with make_session_factory(engine)() as session:
        rows = await session.execute(select(KnowledgeItem).order_by(KnowledgeItem.id))
        return list(rows.scalars())


async def test_run_extracts_filters_compiles_and_records(db_engine: AsyncEngine, model) -> None:
    store = await seed(db_engine)
    factory = make_session_factory(db_engine)
    model.extract_extra = [
        {
            "kind": "role",
            "topic": "联系人",
            "text": "退款找 someone@example.com。",
            "confidence": "stated",
        }
    ]

    report = await runner.run(factory, store, NOW)

    assert report["status"] == "ok" and report["provider"] == "claude"
    assert report["mode"] == "daily" and report["more"] is False
    assert report["sources"] == {
        "rules": {"units": 2, "batches": 2},
        "catalog": {"units": 1, "batches": 1},
        "roster": {"units": 1, "batches": 1},
        "task": {"units": 1, "batches": 1},
    }
    assert report["notes"] == {"tasks_skipped": 3}
    assert report["dropped_reasons"] == {"邮箱": 1}
    rows = await items(db_engine)
    by_ref = {row.source_ref: row for row in rows if row.status == "compiled"}
    assert set(by_ref) == {
        "rules:alpha/AGENTS.md",
        "rules:alpha/docs/refund.md",
        "catalog:orders",
        "roster",
        "T-1",
        "T-2",
        "T-7",
    }
    assert by_ref["T-2"].source_kind == "task"
    assert by_ref["T-2"].occurred_on is not None
    dropped = [row for row in rows if row.status == "dropped"]
    assert [(row.dropped_reason, row.text) for row in dropped] == [("邮箱", "")]
    assert report["extracted"] == 8 and report["dropped"] == 1 and report["compiled"] == 7

    extraction = model.of(extract.SYSTEM_PROMPT)
    sent = "\n".join(call["content"] for call in extraction)
    # 名录只有姓名、职位、团队；服务账号、停用的员工、承接员工自己都不在里面。
    assert "王小明（运营负责人）" in sent and "团队「运营」" in sent
    assert "owner@example.com" not in sent and "13800138000" not in sent
    assert "服务账号" not in sent and "停用的" not in sent and "AI 员工「调度员」" not in sent
    assert "可用的 AI 员工：订单助手" in sent and "orders.example.com" not in sent
    # 不够条件的任务没有交给模型。
    for code in ("T-3", "T-4", "T-5", "T-6", "T-8"):
        assert f"## {code} " not in sent
    assert "## T-7 " in sent and "上级任务：T-1" in sent and "执行：AI 员工「订单助手」" in sent

    wiki_text = model.node.files[LEAD_DIR]["docs/roles.md"]
    # 来源标记写员工名、系统名，不写标识。
    assert "（T-2，K-" in wiki_text and "（规则文档 订单助手，K-" in wiki_text
    assert "（系统目录 订单系统，K-" in wiki_text and "（名录，K-" in wiki_text
    assert model.node.files[LEAD_DIR]["AGENTS.md"].startswith("# 调度员")

    async with factory() as session:
        run = (await session.execute(select(KnowledgeRun))).scalar_one()
    assert run.status == "ok" and run.batches == 5 and run.compiled == 7
    assert run.summary == "改了 docs/roles.md" and run.finished_at is not None
    assert (await store.get(runner.STATE_KEY))["status"] == "ok"

    # 什么都没变：第二次不再调用模型。
    model.calls.clear()
    again = await runner.run(factory, store, NOW + timedelta(days=1))
    assert again["status"] == "ok" and again["sources"] == {} and again["batches"] == 0
    assert model.calls == []


async def test_changed_document_only_sends_the_new_section(db_engine: AsyncEngine, model) -> None:
    store = await seed(db_engine)
    factory = make_session_factory(db_engine)
    await runner.run(factory, store, NOW)
    model.calls.clear()
    model.node.files[ALPHA_DIR]["AGENTS.md"] = ALPHA_RULES + "\n## 发布\n发布走发布流程。\n"

    report = await runner.run(factory, store, NOW + timedelta(days=1))

    assert report["sources"] == {"rules": {"units": 1, "batches": 1}}
    (call,) = model.of(extract.SYSTEM_PROMPT)
    assert "发布走发布流程" in call["content"] and "负责订单查询和退款" not in call["content"]
    assert report["compiled"] == 1


async def test_preview_reads_sources_but_calls_no_model(db_engine: AsyncEngine, model) -> None:
    store = await seed(db_engine)
    factory = make_session_factory(db_engine)

    report = await runner.run(factory, store, NOW, apply=False)

    assert report["preview"] is True and report["sources"]["task"]["batches"] == 1
    assert model.calls == [] and await items(db_engine) == []
    async with factory() as session:
        assert (await session.execute(select(KnowledgeRun))).first() is None
    assert await store.get(runner.STATE_KEY) is None


async def over_quota(engine: AsyncEngine, *providers: str) -> None:
    async with make_session_factory(engine)() as session:
        await session.execute(
            update(RelayServer)
            .where(RelayServer.model_provider.in_(providers))
            .values(
                rate_limit_7d_used_pct=Decimal(90),
                rate_limit_7d_resets_at=NOW + timedelta(days=2),
            )
        )
        await session.commit()


async def test_falls_back_to_codex_when_claude_is_over_quota(db_engine: AsyncEngine, model) -> None:
    store = await seed(db_engine)
    await over_quota(db_engine, "claude")

    report = await runner.run(make_session_factory(db_engine), store, NOW)

    assert report["status"] == "ok" and report["provider"] == "codex"
    assert {call["provider"] for call in model.calls} == {"codex"}


async def test_skips_without_touching_progress_when_no_backend_has_quota(
    db_engine: AsyncEngine, model
) -> None:
    store = await seed(db_engine)
    await over_quota(db_engine, "claude", "codex")
    factory = make_session_factory(db_engine)

    report = await runner.run(factory, store, NOW)

    assert report["status"] == "skipped" and "额度超过阈值" in report["error"]
    assert model.calls == [] and await items(db_engine) == []
    assert await store.get(runner.CURSOR_PREFIX + "task") is None
    async with factory() as session:
        run = (await session.execute(select(KnowledgeRun))).scalar_one()
    assert run.status == "skipped" and run.finished_at is not None


async def test_leak_in_wiki_is_fixed_or_flagged(db_engine: AsyncEngine, model) -> None:
    store = await seed(db_engine)
    factory = make_session_factory(db_engine)
    model.leak = "- 联系 13800138000"

    fixed = await runner.run(factory, store, NOW)

    assert fixed["status"] == "ok" and fixed["hits"] == []
    assert len(model.of(wiki.FIX_PROMPT)) == 1
    assert "docs/roles.md 第" in model.of(wiki.FIX_PROMPT)[0]["content"]
    assert "13800138000" not in model.node.files[LEAD_DIR]["docs/roles.md"]

    # 修不掉：标成需要人看，命中的位置记下来。
    model.fix_works = False
    model.calls.clear()
    model.node.files[ALPHA_DIR]["docs/refund.md"] = "# 退款\n原路退回。\n\n## 时限\n七天内。\n"
    flagged = await runner.run(factory, store, NOW + timedelta(days=1))

    assert flagged["status"] == "needs_review"
    assert [hit["rule"] for hit in flagged["hits"]] == ["手机号"]
    assert len(model.of(wiki.FIX_PROMPT)) == wiki.MAX_FIXES


async def test_item_never_cited_is_given_up_after_a_few_attempts(
    db_engine: AsyncEngine, model
) -> None:
    store = await seed(db_engine)
    factory = make_session_factory(db_engine)
    model.skip_ids = {1}

    for day in range(wiki.MAX_ATTEMPTS):
        await runner.run(factory, store, NOW + timedelta(days=day))

    first = (await items(db_engine))[0]
    assert (first.status, first.dropped_reason, first.attempts) == (
        "dropped",
        runner.NOT_COMPILED,
        wiki.MAX_ATTEMPTS,
    )


async def test_round_failure_keeps_progress_for_the_next_run(db_engine: AsyncEngine, model) -> None:
    store = await seed(db_engine)
    factory = make_session_factory(db_engine)
    model.fail_with = rounds.RoundError("运行时报错：节点离线")

    failed = await runner.run(factory, store, NOW)

    assert failed["status"] == "failed" and "节点离线" in failed["error"]
    assert await items(db_engine) == [] and await store.get(runner.CURSOR_PREFIX + "rules") is None
    model.fail_with = None
    report = await runner.run(factory, store, NOW + timedelta(hours=1))
    assert report["status"] == "ok" and report["compiled"] == 7


async def test_unexpected_error_still_closes_the_run(db_engine: AsyncEngine, model) -> None:
    store = await seed(db_engine)
    factory = make_session_factory(db_engine)
    model.fail_with = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await runner.run(factory, store, NOW)

    async with factory() as session:
        run = (await session.execute(select(KnowledgeRun))).scalar_one()
    assert (run.status, run.error) == ("failed", "RuntimeError") and run.finished_at is not None
    model.fail_with = None
    assert (await runner.run(factory, store, NOW + timedelta(hours=1)))["status"] == "ok"


async def test_cold_start_resets_progress_and_ends_itself(db_engine: AsyncEngine, model) -> None:
    store = await seed(db_engine)
    factory = make_session_factory(db_engine)
    await runner.run(factory, store, NOW)
    model.calls.clear()

    partial = await runner.run(factory, store, NOW + timedelta(days=1), cold_start=True, limit=2)

    assert partial["mode"] == "cold_start" and partial["more"] is True and partial["batches"] == 2
    assert await store.get(runner.COLD_KEY) is True
    rest = await runner.run(factory, store, NOW + timedelta(days=2))
    assert rest["mode"] == "cold_start" and rest["more"] is False and rest["batches"] == 3
    assert await store.get(runner.COLD_KEY) is False


async def test_only_a_dispatcher_can_hold_the_knowledge_base(db_engine: AsyncEngine, model) -> None:
    store = await seed(db_engine)
    factory = make_session_factory(db_engine)

    with pytest.raises(runner.KnowledgeError, match="必须是调度员"):
        await runner.configure(factory, store, bot_key="alpha")
    with pytest.raises(runner.KnowledgeError, match="没有标识为"):
        await runner.configure(factory, store, bot_key="nobody")
    async with factory() as session:
        await session.execute(update(Bot).where(Bot.bot_key == "lead").values(enabled=False))
        await session.commit()
    with pytest.raises(runner.KnowledgeError, match="已停用"):
        await runner.run(factory, store, NOW)
    report = await runner.run_due(factory, store, NOW)
    assert report is not None and report["status"] == "failed"
    status = await runner.status(factory, store)
    assert status["bot_key"] == "lead" and status["bot_problem"] == "承接知识库的 AI 员工已停用"


async def test_run_due_respects_switch_and_schedule(db_engine: AsyncEngine, model) -> None:
    store = await seed(db_engine)
    factory = make_session_factory(db_engine)
    await runner.configure(factory, store, enabled=False)
    assert await runner.run_due(factory, store, NOW) is None
    await runner.configure(factory, store, enabled=True, hour=2, timezone="Asia/Shanghai")

    first = await runner.run_due(factory, store, NOW)

    assert first is not None and first["status"] == "ok"
    assert await runner.run_due(factory, store, NOW + timedelta(minutes=20)) is None
    assert await runner.run_due(factory, store, NOW + timedelta(days=1)) is not None


async def test_stops_between_batches_once_quota_crosses_the_threshold(
    db_engine: AsyncEngine, model
) -> None:
    store = await seed(db_engine)
    factory = make_session_factory(db_engine)

    async def burn(call: int) -> None:
        # 第二批跑的时候额度用过了线：第三批不该再开始。
        if call == 2:
            await over_quota(db_engine, "claude", "codex")

    model.before_call = burn

    report = await runner.run(factory, store, NOW)

    assert report["status"] == "ok" and report["batches"] == 2 and report["more"] is True
    assert "中途停下" in report["error"] and "额度超过阈值" in report["error"]
    assert report["compiled"] == 0 and model.of(wiki.SYSTEM_PROMPT) == []
    assert {row.status for row in await items(db_engine)} == {"pending"}
    # 额度恢复后接着做：前两批不重抽，之前抽出的条目一起编译。
    model.before_call = None
    async with factory() as session:
        await session.execute(update(RelayServer).values(rate_limit_7d_used_pct=Decimal(10)))
        await session.commit()
    rest = await runner.run(factory, store, NOW + timedelta(hours=1))
    assert rest["status"] == "ok" and rest["batches"] == 3 and rest["more"] is False
    assert rest["compiled"] == 7


class FakeStream:
    """假的运行时客户端：吐出给定的事件。"""

    def __init__(self, events: list[Any]) -> None:
        self.events = events

    async def chat_stream(self, request, *, total_timeout):
        for event in self.events:
            yield event

    async def aclose(self) -> None:
        return None


async def test_round_records_the_quota_it_brings_back(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coreman.core.relay import probe
    from coreman.core.relay.sse import FinishEvent, RateLimitEvent, TextDelta

    factory = make_session_factory(db_engine)
    async with factory() as session:
        relay = await add_node_relay(session, health_status="healthy")
        await session.commit()
        relay_id = relay.id
    # 保存额度按真实时钟丢弃「来自未来」的观测，所以这里用真实的现在。
    real_now = datetime.now(UTC)
    resets = (real_now + timedelta(days=2)).isoformat()
    limits = {
        "five_hour": {"used_percentage": 12, "resets_at": resets},
        "seven_day": {"used_percentage": 96, "resets_at": resets},
    }
    events = [TextDelta("好"), RateLimitEvent(limits), FinishEvent("stop")]
    monkeypatch.setattr(probe, "make_client", lambda relay: FakeStream(events))
    backend = rounds.Backend(relay=relay, provider="claude", model="m")

    text = await rounds.run_round(
        backend,
        system_prompt="s",
        content="c",
        working_dir="w",
        max_turns=1,
        limit_seconds=5,
        factory=factory,
    )

    assert text == "好"
    async with factory() as session:
        saved = await session.get(RelayServer, relay_id)
        assert saved is not None and saved.rate_limit_7d_used_pct == Decimal(96)
        assert saved.rate_limit_5h_used_pct == Decimal(12)
        assert rounds.within_quota(saved, {"seven_day": 95, "five_hour": 80}, real_now) is False
    # 格式不对的额度不影响这一轮，也不改已有记录。
    monkeypatch.setattr(
        probe,
        "make_client",
        lambda relay: FakeStream(
            [TextDelta("行"), RateLimitEvent({"seven_day": "?"}), FinishEvent("stop")]
        ),
    )
    again = await rounds.run_round(
        backend,
        system_prompt="s",
        content="c",
        working_dir="w",
        max_turns=1,
        limit_seconds=5,
        factory=factory,
    )
    assert again == "行"
    async with factory() as session:
        saved = await session.get(RelayServer, relay_id)
        assert saved is not None and saved.rate_limit_7d_used_pct == Decimal(96)
