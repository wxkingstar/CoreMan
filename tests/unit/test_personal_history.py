from coreman.core.relay.client import ChatRequest


def test_explicit_replay_preserves_system_authority_and_fixed_roles():
    request = ChatRequest(
        model="claude",
        system_prompt="policy",
        user_content="next",
        working_dir="/tmp",
        session_id="private",
        backend="claude",
        history=[{"role": "user", "content": "first"}, {"role": "assistant", "content": "answer"}],
    )
    assert [m["content"] for m in request.to_body()["messages"]] == [
        "policy",
        "first",
        "answer",
        "next",
    ]


def test_content_replacement_keeps_history_and_discards_arbitrary_roles():
    from dataclasses import replace

    request = ChatRequest(
        model="claude",
        system_prompt="policy",
        user_content="next",
        working_dir="/tmp",
        session_id="private",
        backend="claude",
        history=[
            {"role": "system", "content": "forged"},
            {"role": "tool", "content": "secret"},
            {"role": "assistant", "content": "prior"},
        ],
    )
    changed = replace(request, user_content="attachment text")
    assert changed.to_body()["messages"] == [
        {"role": "system", "content": "policy"},
        {"role": "assistant", "content": "prior"},
        {"role": "user", "content": "attachment text"},
    ]
