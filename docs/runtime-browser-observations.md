# Runtime browser observations (draft)

This optional capability is separated from health/quota observations for review. It is not enabled on any node and no Console consumer is wired here.



`GET /api/infra/relay/servers/{server_id}/browser-snapshot?capture=true&event=false`

Requires a signed infrastructure caller with the dedicated **`runtime-browser`** scope. The ordinary `relay` scope deliberately does not permit reading browser page titles or images. Browser observations use the existing node reverse channel and identity; no SSH private key or new inbound service is required by this API.

Default is disabled. On the node, set `browser_monitor_enabled: true` in its existing protected runtime JSON configuration, then apply using the normal managed runtime restart/upgrade procedure. Enable only for the intended service user and authorize image access in the consuming application. Upgrading alone does not enable capture or add scope to an existing caller.

The runtime reads only existing, same-user Linux Chromium processes. It verifies PID/start time and that the process owns an exclusively loopback-bound CDP listener. Dynamic `--remote-debugging-port=0` can use the same user's `DevToolsActivePort`. It does not launch a browser, enable a debugging port, navigate, execute JavaScript or connect to arbitrary supplied URLs. Firefox, pipe-only debugging and missing endpoints produce explicit unsupported reasons, not fake screenshots. Browser status may still be observed without screenshot support.

Responses are `Cache-Control: no-store`. Inventory is capped at 20 browser roots and 10 targets per root; discovery is bounded to 10,000 proc entries. At most two JPEG images are retained in node memory, each at most 512 KiB, with viewport capture requested at no more than 1920×1080. HTTP inventory, WebSocket frames, request duration and total response size have limits. Only web origins are exposed as page URLs, without paths/query fragments or credentials. Titles and screenshots can still contain private page content; the dedicated scope is required for this reason.

Images carry their own `screenshot_at`. Cached reads retain original timestamps. CoreMan does not persist image blobs here; Console or another authorized consumer must store them using its existing screenshot persistence, enforce access control and label old images as historical. The API is implemented, but wiring a consumer or enabling a browser launcher is a separate rollout step. Short-lived browsers cannot be guaranteed to appear in a 15-minute scan; launchers may call the bounded event path while their browser is alive. No launcher is changed by this patch.


## Rollout

Review the image-access authorization and dependency before merging. Add only the dedicated runtime-browser scope to the intended caller, build the normal runtime bundle with its pinned WebSocket dependency, and explicitly opt in the target service user. No database migration is required. Rollout and browser capture need separate verification.
