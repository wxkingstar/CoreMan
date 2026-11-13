"""工作目录规则：个人助理的提示词带上，普通员工的提示词逐字不变（走真实的 configure）。"""

import uuid

from coreman.core.assistants import followup_prompt, notes_prompt, workspace_prompt
from coreman.core.assistants.approvals import prompt as approval_prompt
from coreman.core.db.models import PlatformApp
from coreman.runtime.worker.chat.personal import configure, feishu_guidance
from tests.api.test_feishu_personal import setup
from tests.api.test_feishu_personal_worker import SUPPORTED, intake_for
from tests.integration.worker_helpers import build_ctx


async def test_the_assistant_prompt_gets_the_workspace_rules(db_session, app, db_engine):
    bot, user, task = await setup(db_session, app)
    platform_app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="x",
    )
    db_session.add(platform_app)
    await db_session.flush()
    bot.kind, bot.owner_id, bot.platform_app_id = "assistant", user.id, platform_app.id
    await db_session.commit()
    intake = await intake_for(db_session, bot, task, "今天的日报写好了吗", SUPPORTED)
    prompt, _ = await configure(
        db_session, build_ctx(db_engine, task), intake, uuid.uuid4(), "你是助理", {}
    )
    assert prompt.endswith(
        approval_prompt.RULES
        + workspace_prompt.WORKSPACE_RULES
        + notes_prompt.RULES
        + followup_prompt.FOLLOWUP_RULES
    )


async def test_the_standard_employee_prompt_is_unchanged(db_session, app, db_engine):
    bot, _, task = await setup(db_session, app)
    intake = await intake_for(db_session, bot, task, "今天的日报写好了吗", SUPPORTED)
    prompt, _ = await configure(
        db_session, build_ctx(db_engine, task), intake, uuid.uuid4(), "你是销售", {}
    )
    rules, _ = feishu_guidance(None, "https://coreman.example.com", scheduled=False)
    assert prompt == "你是销售" + rules.replace(
        "https://coreman.example.com", build_ctx(db_engine, task).public_base_url.rstrip("/")
    )
    assert workspace_prompt.WORKSPACE_RULES not in prompt
    assert approval_prompt.RULES not in prompt
    assert notes_prompt.RULES not in prompt
    assert followup_prompt.FOLLOWUP_RULES not in prompt
