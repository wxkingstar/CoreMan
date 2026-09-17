# Task 1 report: Feishu media input and actionable failures

## Outcome

- Real Feishu post payloads now preserve top-level non-folder `files` entries as scoped `FilePart` attachments, including the supplied `file_v3_test` / `inventory-test.csv` fixture.
- Post attachment keys use the same bounded identifier shape as the downloader. Folder entries and unsafe keys are ignored, and no more than 100 post file entries are considered.
- Resource downloads preserve message-id scoping, identifier validation, 100 MB limits, and image/file timeout budgets.
- HTTP 400 / code `99991672` becomes `permission_denied` with sanitized guidance to add one of `im:message.history:readonly`, `im:message:readonly`, or `im:message`, then publish and retry.
- HTTP 404 and 410 become `resource_unavailable` and `resource_expired`; timeouts retain `timeout`, other HTTP/transport failures retain `download_failed`.
- Error response bodies are capped at 64 KiB. Only the numeric `code` is interpreted; upstream messages, URLs, tokens, and data are never reflected.

## TDD evidence

Red command:

```text
../../.venv/bin/pytest -q tests/unit/test_feishu_inbound.py tests/unit/test_feishu_media.py
```

Red result: `6 failed, 4 passed`. The failures were exactly the missing post file, folder/unsafe-key filtering, image/file permission classification, and 404/410 classification.

Green command:

```text
../../.venv/bin/pytest -q tests/unit/test_feishu_inbound.py tests/unit/test_feishu_media.py tests/unit/test_messages.py tests/unit/test_content.py tests/unit/test_bot_collaboration.py tests/unit/test_feishu_cards.py
```

Green result: `41 passed in 0.53s`.

Static checks:

```text
../../.venv/bin/ruff check coreman/runtime/gateway_feishu/inbound.py coreman/core/platforms/feishu_media.py coreman/core/i18n/messages.py tests/unit/test_feishu_inbound.py tests/unit/test_feishu_media.py tests/unit/test_messages.py
../../.venv/bin/mypy coreman/runtime/gateway_feishu/inbound.py coreman/core/platforms/feishu_media.py
```

Result: Ruff passed; mypy reported no issues in the two changed source modules.

## Additional regression attempt

Command:

```text
../../.venv/bin/pytest -q tests/unit/test_content.py tests/integration/test_chat_handler_media.py tests/integration/test_feishu_transport.py
```

Result: all 9 unit tests completed successfully; 21 integration tests failed during fixture setup because the managed sandbox denied access to the Docker Unix socket (`PermissionError: [Errno 1] Operation not permitted`). No integration test body ran, so this is an environment limitation rather than a product assertion.

## Files changed

- `coreman/runtime/gateway_feishu/inbound.py`
- `coreman/core/platforms/feishu_media.py`
- `coreman/core/i18n/messages.py`
- `tests/unit/test_feishu_inbound.py`
- `tests/unit/test_feishu_media.py`
- `tests/unit/test_messages.py`

## Self-review and remaining concerns

- Reviewed the exact six-file diff and ran `git diff --check`; no whitespace errors or unrelated source changes were found.
- The pre-existing untracked `docs/superpowers/plans/2026-09-17-feishu-e2e-fixes.md` was not edited or staged.
- No Feishu permission, application configuration, external message, deployment, or live resource was changed. Consequently, the original synthetic resource cannot be re-verified until the application permission is configured and published by the owning workflow.
- HTTP 404/410 classification is based only on transport status. It deliberately does not parse or expose upstream prose.
