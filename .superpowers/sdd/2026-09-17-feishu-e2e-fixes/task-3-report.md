# Task 3 report: Feishu quote enrichment

## Outcome

- The worker now enriches Feishu `parent_id` before `ContentBuilder`, without relying on relay conversation memory.
- Own delivered cards resolve through exact `FeishuDelivery.message_id` → `TaskStream.final_text` matches scoped to the same bot and chat. Thinking/tool output is never read.
- Sent outbox markdown resolves only through exact bot, chat, parent message ID, platform, and `sent` status matches.
- If durable content is absent, one bounded official message read is allowed. The response must identify the exact parent message, chat, and current app sender. Text/card/post extraction is capped at 12,000 characters and never fetches parent media or URLs.
- Missing, deleted, malformed, wrong-app, or cross-chat parents produce an explicit `[引用消息内容不可用]` context note rather than a fabricated quoted original.
- Replying with a new attachment while quoting text now preserves the current attachment; only the current inbound message's media fetcher is used.

## TDD evidence

RED integration command:

```text
TEST_DATABASE_URL=postgresql+asyncpg://postgres:coreman_test_only@127.0.0.1:15440/coreman_test ../../.venv/bin/pytest -q tests/integration/test_feishu_quote.py
```

RED result: `4 failed`. The full worker request contained only the new user text, proving `parent_id` was ignored for delivered cards, sent outbox messages, deleted parents, and cross-chat refusal.

RED attachment command:

```text
../../.venv/bin/pytest -q tests/unit/test_content.py -k text_quote_keeps_current_message_attachment
```

RED result: `1 failed`; `ContentBuilder` emitted the quote text but dropped the current message image.

Final GREEN command:

```text
TEST_DATABASE_URL=postgresql+asyncpg://postgres:coreman_test_only@127.0.0.1:15440/coreman_test ../../.venv/bin/pytest -q tests/integration/test_feishu_quote.py tests/integration/test_chat_handler_media.py tests/unit/test_content.py
```

GREEN result: `23 passed in 7.30s`.

Static checks:

```text
../../.venv/bin/ruff check coreman/runtime/worker/chat/feishu_quote.py coreman/runtime/worker/chat/content.py coreman/core/chat/content.py tests/integration/test_feishu_quote.py tests/unit/test_content.py
../../.venv/bin/mypy coreman/runtime/worker/chat/feishu_quote.py coreman/runtime/worker/chat/content.py coreman/core/chat/content.py
```

Result: Ruff passed; mypy reported no issues in the three source modules.

## Files changed

- `coreman/runtime/worker/chat/feishu_quote.py`
- `coreman/runtime/worker/chat/content.py`
- `coreman/core/chat/content.py`
- `tests/integration/test_feishu_quote.py`
- `tests/unit/test_content.py`

## Self-review

- Reviewed bot/chat/parent filters on both durable lookup paths and exact app/chat/message validation on the HTTP fallback.
- Confirmed persisted lookup wins without HTTP, no parent causes no enrichment request, and parent media identifiers are ignored.
- Confirmed unavailable content is a context note rather than fake source text, and cross-chat text never reaches the relay request.
- Confirmed the session-reset case uses only durable delivery/stream rows.
- No external configuration, deployment, credential mutation, or live Feishu action was performed. Live post-reset quote verification remains with the controller after deployment.

## Review fixes

- Removed the overly strict sender-ownership check from official message reads. A parent written by a human or another bot in the same chat is valid context; the current bot's authenticated read plus exact response `message_id` and `chat_id` remain the scope boundary.
- A present but malformed `parent_id` (including empty string or non-string zero) now produces the explicit unavailable context marker without making an HTTP request. The normal gateway shape with absent/`None` parent still means no quote and performs no enrichment.

Review RED command:

```text
TEST_DATABASE_URL=postgresql+asyncpg://postgres:coreman_test_only@127.0.0.1:15440/coreman_test ../../.venv/bin/pytest -q tests/integration/test_feishu_quote.py -k 'accepts_any_sender or malformed_parent'
```

RED result: `4 failed`; same-chat human/other-bot content was rejected, while empty/zero parent references were silently ignored.

Review GREEN command:

```text
TEST_DATABASE_URL=postgresql+asyncpg://postgres:coreman_test_only@127.0.0.1:15440/coreman_test ../../.venv/bin/pytest -q tests/integration/test_feishu_quote.py
```

GREEN result: `8 passed in 4.60s`. Focused Ruff and mypy checks also passed for the amended helper/content modules and tests.
