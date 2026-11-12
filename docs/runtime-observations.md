# Runtime health and quota observations

## Contract

Runtime observations identify a runtime node, provider and, where available, the credential seat. Host CPU, disk, memory and browser monitoring remain the monitoring application's responsibility. Browser snapshots and their separate authorization are intentionally outside this change.

`GET /api/infra/relay/servers` retains existing fields and adds:

| Field | Meaning |
| --- | --- |
| `client_metadata` | CLI installed/login/version and heartbeat time; optional explicit plan and verification metadata. No account files, secrets or full environment. |
| `driver_health` | Driver/process connectivity only, not proof of inference. |
| `model_health` | Latest model probe or completed conversation, its own observation time and `reply_verified`. |
| `last_model_failure` | Last model failure, retained after recovery as history. |
| `quota_observation` | Read outcome/source, attempt/success times and per-window observations/applicability. |

The page separates driver/model health and quota provenance. Model observations expire after 15 minutes; a healthy probe requires a reply consisting of `OK`, ignoring case and surrounding whitespace/punctuation (`OK.`, `ok!`). Extra words, sentences and Unicode lookalike letters fail validation. HTTP 200, installed CLI, login and catalog entries alone do not prove availability. `requested_model` and attested actual `model` are separate; unknown actual models remain unknown.

Drivers attach `x_actual_model` when the CLI identifies the model for the reply. Claude uses main assistant events. Codex reads the bounded rollout tail and, when the turn context has fallen outside the tail, can read up to 4 MiB of new rollout data from the start of the current turn. OpenCode reads the exact assistant message's provider/model from its local SQLite database in read-only mode. The worker and health probe store this evidence separately from the request model. Older drivers and missing CLI evidence leave the model field empty.

Actual completed conversations write model observations and returned quota through the same storage functions as fallback probes. Cancelled, queued and business/tool-failed tasks do not by themselves prove provider failure. The executing node attaches its seat ID to streamed provider results when supported. **Explicitly mismatched retired-seat reports are rejected. Reports omitting seat_id remain accepted even if the node advertises seats**, preserving API-first rollout compatibility with existing runtimes and worker SSE. These legacy reports cannot attest which seat generated them; accepted telemetry is not credential-generation attestation.

Node-reported interruptions (`control_lost`, `connection_lost`, `execution_failed`, and `response_too_large`) remain failed tasks with `run_interrupted` and the node error code in the task and chat log. They do not update `model_health`, `last_model_failure`, or legacy relay health: a transport/execution failure proves neither model failure nor recovery. The `control_lost` task/chat error record explains that the local runtime abandoned the request and points operators to its control-poll, lease and shutdown logs. The user reply only says the turn was interrupted, some changes may remain, and how to continue; it does not include node error codes or operational diagnostics. The code can also be reported during shutdown, so it does not by itself prove a lease timeout or identify a failing network hop. No timeout or cancellation policy changes are implied, and existing observations keep their original timestamps and freshness limits.

## Existing cadence, with activity suppression

| Observation | Strategy |
| --- | --- |
| CLI/login/version | Existing discovery/heartbeat, no added CLI poll. |
| Claude model health | Existing 15 minute all-day fallback, skipped while a matching-seat model observation is newer than 15 minutes. |
| Codex/OpenCode model health | Existing local 09:00–23:59 hourly slots, no nighttime probes; skipped if matching-seat activity is newer than one hour. At most one probe per hourly slot. |
| Claude quota | Existing driver cache and conversation responses; fallback reads that cache. |
| Codex quota | Existing conversation responses and hourly app-server account limits. |
| OpenCode quota | Unsupported until an integrated source exists. |

No independent quota helper, host polling daemon, local automation, process termination, queue or concurrency control is introduced. Existing runtime schedules stay inside the daemon. A 15 minute model freshness cutoff can legitimately show unknown between hourly Codex/OpenCode probes; it does not authorize more probes.

## Quota evidence

Each window retains its own `observed_at`. Refreshing weekly usage does not refresh five-hour usage. Empty/failed reads record attempt/result without refreshing previous percentages. Genuine zero usage remains observable. Out-of-order checks compare timestamps supplied by the node. The legacy quota warning uses server receipt time for values returned by a live conversation, while the observation retains the node timestamp. The shared legacy timestamp follows the oldest retained numeric window under that policy.

`not_applicable` requires an explicit upstream `window_applicability` value. Provider name, price, plan aliases (`pro`, `pro_100`, `pro_200`, `pro_500`, `ultra`, `max_20x`), verification flags, missing percentages, authentication failure and account hold do not establish window policy. A returned window takes precedence over an explicit not-applicable marker. Missing windows stay unknown or historical with their original timestamps; weekly availability is not invented.

## Rollout and validation

1. Back up the database and apply migration 0076 through the normal migration workflow: four nullable JSONB columns on relay_servers, no production rewrite.
2. Release API/worker/web together, retaining legacy fields. Old runtime reports lacking seat_id continue to update health/quota.
3. Build the ordinary runtime bundle and upgrade nodes through existing management. No browser dependency, capability, scope or capture endpoint is added by this change.
4. Verify a real conversation, provider-specific idle fallback, failed/empty/partial quota reads, driver success after model failure, omitted legacy seats and explicit mismatched-seat rejection. Distinguish fixtures from production verification.

Downgrade removes observation columns/history; follow existing backup/rollback policy. Host PSI boot configuration is unrelated and requires separate root-authorized maintenance.
