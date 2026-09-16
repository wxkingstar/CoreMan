# Collaboration partners admin implementation

User-approved scope: add collaboration partner controls to AI employee administration with usable interaction. Continue in the existing isolated collaboration-discovery branch.

- Source: employee detail dedicated Collaboration partners tab; Feishu-only explanation for other platforms.
- Administrator must manage BOTH source and target bots to establish/enable/change a route. Source managers may pause/archive existing outgoing routes; user execution ACL remains independent.
- Three steps: select partner → select common Feishu group → verify connection (explicitly sends two non-AI test messages) → enable. Never accept raw identity IDs from UI.
- Partners: name/description search among manageable Feishu bots, excluding self; bots must be enabled with active runtimes to verify/enable.
- Groups: fetch bounded common group list from both apps, show readable names. API failures are actionable, never fabricate IDs.
- Setup probes: queued in existing durable outbox, bound to route/probe/actor/expiry. Gateway rechecks authority before sending. Accept only real inbound bot events whose receiver, platform message ID, app ID, chat, tenant, explicit mention, and sender union ID match the corresponding probe receipt. No AI tasks created. No automatic enable.
- Store setup metadata, archived flag and revision on route (migration). Archive preserves historical ledgers and cancels active collaborations; disable prevents pending probe sends. Version preconditions prevent stale edits.
- API: GET /api/admin/bots/{id}/collaborators → route array; GET /collaborator-options?q= → manageable peers; GET /collaborator-groups?target_bot_id= → common group array; POST /collaborators {target_bot_id,chat_id} → pending route; POST /collaborators/{route_id}/verify (If-Match) → refreshed pending route; PATCH /collaborators/{route_id} {enabled} (If-Match); DELETE same (If-Match) → archive.
- Route output: id,target_bot_id,target_name,target_description,chat_id,chat_name,enabled,version,status (pending/ready/failed/expired/unavailable),reason (stable code or null),can_enable,can_verify,can_remove. No secrets/internal bot IDs beyond normal resource identifiers.
- UI: inline loading/errors/retry, pending status polling only while visible; bounded drawer with scroll body/fixed footer, mobile layout, keyboard labels; explicit direction and both-user-permission hint; confirm pause/archive if running work could stop; zh/en/ja locale parity.
- Tests first: API permissions/CSRF/self/duplicate/stale version, probe exact receipt correlation and replay, expiry/revocation, archive/history; UI async stale-response handling, failure, disabled states, unsaved close, polling cleanup. Run relevant Python/Vitest/build and browser desktop/mobile checks using isolated fixtures. No production changes or real IM sends during this task.
