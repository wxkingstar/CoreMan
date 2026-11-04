# Runtime observations and browser snapshots

## Contract

Runtime observations identify a runtime node, provider and active credential seat. They do not measure host CPU, disk or memory; those remain the monitoring application's responsibility.

`GET /api/infra/relay/servers` retains its existing fields and adds:

| Field | Meaning |
| --- | --- |
| `client_metadata` | CLI installed/login/version and heartbeat observation time; optional explicit plan and verification evidence. No login files, account secrets or complete environment. |
| `driver_health` | Driver/process connectivity only. A successful driver check does not prove inference. |
| `model_health` | Latest model probe or completed conversation observation, with its own time and `reply_verified`. |
| `last_model_failure` | Last observed model failure, retained after recovery; historical evidence, not a claim that failure is current. |
| `quota_observation` | Read outcome, source, last attempt/success and separate per-window observations/applicability. |

The runtime management page separates driver/model health and shows quota provenance states. A model observation is fresh for 15 minutes; a healthy probe additionally requires exactly the requested `OK` response. HTTP 200, a model catalog entry, installed CLI or saved login alone cannot prove availability. The actual returned model is populated only when attested by the response; `requested_model` is separate and must not be shown as the actual model.

The existing worker records observations from completed provider conversations and reports returned quota through the same `save_quota` path as the agent. Cancelled, queued or business/tool-failed tasks do not independently prove provider failure. The node attaches the executing generation's seat ID before forwarding streamed provider output. Results from another active seat are ignored. Old nodes without seat metadata retain compatibility; nodes that advertise seats must provide matching provenance. A mixed-version deployment may therefore show unknown until its node is upgraded. This is not credential-version attestation within one seat.

### Cadence

| Observation | Strategy |
| --- | --- |
| CLI/login/version | Existing capability discovery/heartbeat; no extra CLI poll introduced. |
| Model health | Observe actual completed conversations first. For Claude, Codex and OpenCode, schedule the existing minimal probe after 15 minutes without a matching-seat model observation. Fallback now applies all day to all three providers; previously Codex/OpenCode ran hourly during local daytime. |
| Claude quota | Existing driver `rate_limit_event` cache and actual conversation responses; fallback model check also reads this cache. |
| Codex quota | Existing actual conversation rollout and hourly app-server account limit read. No new independent quota helper. |
| OpenCode quota | Explicitly unsupported until its provider offers an integrated source. |
| Browser inventory | Caller initiated; node cache at least 15 minutes. Optional launcher/event hint can refresh after 60 seconds. |
| Screenshots | Caller initiated with `capture=true`; at most two per scan, normally at least 30 minutes per browser/page identity. |

No new host polling daemon, local automation, process termination, queue or concurrency control is introduced here. Existing runtime schedules continue to run inside CoreMan's runtime daemon.

### Quota evidence

Each quota window has its original `observed_at`. Refreshing one window does not refresh another. An empty or failed read updates read outcome/attempt time without making an old usage figure fresh. Legacy numeric values without typed provenance are historical. Reset deadlines and freshness still apply to observed percentages, including a genuine zero.

For older consumers with only one shared quota timestamp, the timestamp conservatively follows the oldest retained numeric window. Typed consumers can use each window's own observation time.

`applicability=not_applicable` requires explicit upstream policy or plan evidence, never a price, a missing percentage, failed authentication or account hold. Current explicit plan defaults cover Codex Pro without a returned five-hour window, and verified Claude Ultra without a returned five-hour window. A real returned window takes precedence over defaults, including temporary restrictions. Unknown Claude verification remains unknown; no verification state is invented. Weekly usage remains unavailable if the provider does not return it.

Policy references checked on 2026-11-04: [Codex pricing](https://learn.chatgpt.com/docs/pricing), [Claude Ultra](https://support.claude.com/en/articles/17728952-what-is-the-ultra-plan). Provider policy can change; explicit returned windows take precedence.

## Browser API

`GET /api/infra/relay/servers/{server_id}/browser-snapshot?capture=true&event=false`

Requires a signed infrastructure caller with the dedicated **`runtime-browser`** scope. The ordinary `relay` scope deliberately does not permit reading browser page titles or images. Browser observations use the existing node reverse channel and identity; no SSH private key or new inbound service is required by this API.

Default is disabled. On the node, set `browser_monitor_enabled: true` in its existing protected runtime JSON configuration, then apply using the normal managed runtime restart/upgrade procedure. Enable only for the intended service user and authorize image access in the consuming application. Upgrading alone does not enable capture or add scope to an existing caller.

The runtime reads only existing, same-user Linux Chromium processes. It verifies PID/start time and that the process owns an exclusively loopback-bound CDP listener. Dynamic `--remote-debugging-port=0` can use the same user's `DevToolsActivePort`. It does not launch a browser, enable a debugging port, navigate, execute JavaScript or connect to arbitrary supplied URLs. Firefox, pipe-only debugging and missing endpoints produce explicit unsupported reasons, not fake screenshots. Browser status may still be observed without screenshot support.

Responses are `Cache-Control: no-store`. Inventory is capped at 20 browser roots and 10 targets per root; discovery is bounded to 10,000 proc entries. At most two JPEG images are retained in node memory, each at most 512 KiB, with viewport capture requested at no more than 1920×1080. HTTP inventory, WebSocket frames, request duration and total response size have limits. Only web origins are exposed as page URLs, without paths/query fragments or credentials. Titles and screenshots can still contain private page content; the dedicated scope is required for this reason.

Images carry their own `screenshot_at`. Cached reads retain original timestamps. CoreMan does not persist image blobs here; Console or another authorized consumer must store them using its existing screenshot persistence, enforce access control and label old images as historical. The API is implemented, but wiring a consumer or enabling a browser launcher is a separate rollout step. Short-lived browsers cannot be guaranteed to appear in a 15-minute scan; launchers may call the bounded event path while their browser is alive. No launcher is changed by this patch.

## Rollout and validation

1. Back up the database, then apply migration **0076** through the existing CoreMan migration workflow. It adds four nullable JSONB columns to `relay_servers`; no production data rewrite is required.
2. Release the API/worker/web together. Keep legacy fields for consumers while adopting typed observations.
3. Build the runtime release bundle, including the pinned WebSocket dependency, and upgrade nodes through the normal managed upgrade process. API first, runtime second avoids old API dropping new telemetry fields.
4. Grant `runtime-browser` only to the intended image reader, explicitly enable the node capability, and integrate the consumer. Do not broaden existing relay-only credentials automatically.
5. Verify an actual conversation, idle fallback, failed/empty quota reads, driver success after model failure, seat switch with an old response, and a real browser capture. Distinguish test fixtures from production observations.

Migration downgrade removes observation columns and their history; follow the normal backup/rollback policy. Runtime rollback removes new capture/telemetry behavior. The host kernel's PSI boot setting is unrelated to this daemon and requires separate root-authorized host maintenance; switching to the AI service user does not grant that permission.
