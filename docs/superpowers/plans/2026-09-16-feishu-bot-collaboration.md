# Feishu bot collaboration implementation plan

**Goal:** Human → A → B → A → human, driven by real Feishu mention events.
**Architecture:** A receives a task-scoped encrypted capability for a help endpoint. Calling it records a single durable collaboration. After A exits successfully, a text outbox message mentions B. Only the matching platform message, tenant, group and sender union ID may dispatch B. B replies as text with an explicit mention of A; the matching real event resumes A's original relay session. The original human is revalidated against both bots on every dispatch. Waiting does not occupy a worker or relay. Routes default off.
**Tech stack:** Python, FastAPI, SQLAlchemy/PostgreSQL, existing worker/scheduler/outbox, Feishu long connection.
**Approved spec:** outputs/feishu-mutual-mention-20260916/research-design.html (user approved 2026-09-16).

- [x] Add durable route/collaboration models and migration 0023; isolated test DB only.
- [x] Write failing behavior tests for capability validation, scoped real-event consumption, replay, one-hop, permission changes and expiry.
- [x] Add core collaboration service: create, publish request, consume platform events, response, resume, cancel/expiry. Lock rows and use stable dedupe keys.
- [x] Add task-scoped help API and request prompt/env. No bot credentials or business tokens in collaboration records.
- [x] Keep bot sender type separate; bot events never enter generic human intake. Persist early events so receipt-before-send-commit races recover.
- [x] Reuse chat execution with explicitly verified original human provenance; isolate relay sessions per human request for enabled routes; suppress B cards and send actual text @ replies.
- [x] Integrate timeout/cancel/disabled handling and transport idempotency.
- [x] Run meaningful unit/integration/API tests and review the diff.
- [x] Build/migrate/roll local services; enable only the existing A/B QA group route.
- [x] Real E2E: human mentions A, A invokes help, B-only data feedback reaches A, A summarizes. Change B-only data and repeat; collect message/task/outbox evidence.
- [x] Report actual E2E results and residual limits in a single-file HTML.

Constraints: No blanket bot-to-human identity conversion; no creator fallback; one help request per original task; no recursive B help or resumed A help; same tenant/group; exact peer union IDs plus server-issued outbound message IDs; bounded lifetime; original human reply context; preserve all unrelated bots.

Validation: 37 related tests passed; ruff and mypy (240 source files) passed. Independent review findings (source cancellation and dispatch-time runtime switch) were reproduced, fixed and re-reviewed. Three real QA flows passed: tasks 51→52→53 and 54→55→56 on v3, tasks 60→61→62 on final v4. B-only fixture changes produced availability 108→77→165 and shortage 12→43→0 (surplus45). Each chain verified original human, original A relay session, real Feishu request/reply IDs, final original-human reply and pushed delivery. The additional human-triggered online check (57→58→59) also succeeded. Evidence: outputs/feishu-mutual-mention-20260916/implementation-report.html and implementation-evidence.json.
