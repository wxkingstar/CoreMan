# Progressive Collaboration Implementation Plan

> **For agentic workers:** Use executing-plans for service work and an independent runtime adapter subtask. Follow test-driven-development.

**Goal:** Replace per-user-message peer lists with task-scoped MCP discovery and enforce bounded collaboration.
**Architecture:** Stable system policy; search/detail/request tools over stateless Streamable HTTP; existing single-hop Feishu ledger remains the transport. Task payload persists atomic call counters and repeat fingerprints. Runtime receives credentials through environment only.
**Tech Stack:** FastAPI, SQLAlchemy/PostgreSQL, Python, Go Claude/Codex drivers.
**Spec:** User-approved architecture in this conversation: discovery → detail → request, plus server-enforced loop prevention.

## Global Constraints
- No peer catalog, credentials, or invocation shell recipe in user messages.
- Original human authority is checked on discovery, detail, and request. No helper/resume recursion.
- One registered request per original human task; identical retries must not dispatch twice.
- At most 12 tool calls, two identical calls, three invalid/failed calls per task. Counters survive reconnects and serialize concurrent callers.
- Successful registration ends source execution through worker handoff, without model polling.
- Collaboration-enabled turns and helper/resume turns have a 64-tool execution ceiling; existing TTL remains authoritative.
- Existing production data and deployment are outside this implementation's test scope.

## Tasks
- [x] Service tests first: authorized search/detail, bounded summaries, pagination, disabled/revoked peers, no recursive delegation, shared budgets across old/new endpoints, repeated errors, concurrent requests.
- [x] Implement coreman/core/chat/collaboration_tools.py with persistent metering and tools; expose authenticated MCP on the existing API router. Reuse request_help for actual dispatch.
- [x] Runtime tests first: native MCP configuration without credential leakage; actual instruction channel on fresh/resumed Codex; preserve Claude system flags.
- [x] Implement adapters in runtime_daemon/drivers; verify package Go tests.
- [x] Replace configure peer injection with short system policy and fresh task environment; remove user-prefix wrapper and preserve attachments unchanged.
- [x] Worker tests first: handoff closes stream, budget stops execution, cancelled/late callbacks cannot restart.
- [x] Run related unit/integration/API tests in disposable PostgreSQL, Python lint/type checking and Go tests; inspect diff and update docs with verified limits.

## Verification evidence
- Related Python regression suite: 88 passed, including helper-loop and registration/EOF review regressions.
- Official MCP SDK 2.2.0 local-only handshake negotiated 2025-11-25; list/search/detail/request succeeded with synthetic task credentials. No live IM message or model run.
- All 243 core Python source files pass mypy. Python lint and diff whitespace checks pass.
- Claude/Codex/OpenAI Go packages pass, including native Codex non-shell tool events and Claude same-name/replayed tool identity regressions.
- No database migration or dependency change required. Deployment and real-model IM end-to-end verification remain release work, not claimed here.
