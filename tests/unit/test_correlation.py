from coreman.runtime.gateway_wecom.correlation import PushCorrelation


def test_put_lookup_expire_and_evict() -> None:
    now = [0.0]
    c = PushCorrelation(ttl_seconds=10, max_entries=10, clock=lambda: now[0])
    c.put("r1", bot_key="b", action="stream", stream_id="s1")
    now[0] = 5
    got = c.lookup("r1")
    assert got and got["stream_id"] == "s1" and got["age"] == 5
    now[0] = 11
    assert c.lookup("r1") is None
    for i in range(10):
        c.put(f"k{i}", bot_key="b", action="send")
    assert len(c) == 10
    c.put("overflow", bot_key="b", action="send")
    assert len(c) <= 9 and c.lookup("overflow") is not None and c.lookup("k0") is None
