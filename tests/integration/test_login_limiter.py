from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.security import DbLoginLimiter


async def test_limiter_blocks_after_five_failures(db_session: AsyncSession) -> None:
    lim = DbLoginLimiter(db_session, max_hits=5, window_seconds=60)
    for _ in range(5):
        assert await lim.is_blocked("10.0.0.1") is False
        await lim.record_failure("10.0.0.1")
    assert await lim.is_blocked("10.0.0.1") is True
    assert await lim.is_blocked("10.0.0.2") is False  # 其它 IP 不受影响


async def test_limiter_normalizes_invalid_ip(db_session: AsyncSession) -> None:
    lim = DbLoginLimiter(db_session, max_hits=1, window_seconds=60)
    assert await lim.is_blocked("not-an-ip") is False
    await lim.record_failure("not-an-ip")
    assert await lim.is_blocked("not-an-ip") is True
