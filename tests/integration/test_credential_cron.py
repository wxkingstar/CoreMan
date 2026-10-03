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
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    await CronRunHandler().run(ctx)
    env = fake.requests[0]["env_vars"]
    assert env["DEMO_PIN"] == "pin-778899"
    cap = policy.read_capability(cipher, env["COREMAN_CREDENTIAL_TOKEN"])
    assert (cap.origin_kind, cap.cron_job_id, cap.user_id) == ("cron", row.id, row.created_by)
    prompt = fake.requests[0]["messages"][0]["content"]
    assert "`$DEMO_PIN`" in prompt
    # 定时任务不会续接：提示词不能承诺“提交后自动继续”，要说明下次定时运行才生效。
    assert "系统会自动让你继续" not in prompt and "下一次定时运行起生效" in prompt
    # 出站闸门：secret 字段的值必须进 ctx.secrets，非 secret 字段不进。
    assert "pin-778899" in ctx.secrets
    assert "alice" not in ctx.secrets
