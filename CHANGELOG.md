# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- WeCom setup and troubleshooting guide (`docs/wecom.md`), a glossary (`docs/glossary.md`), a service topology diagram in `docs/architecture.md`, and an English README (`README.en.md`).
- Trust model and threat boundaries in `SECURITY.md`.
- Scheduled runs add an overridable "scheduled task constraints" prompt section (`prompt_cron_mode`), editable in Settings.
- Scheduled pushes carry a header (task, bot, duration) and a footer. Jobs without any delivery target send the result to the job creator.
- A separate completion reminder after streamed replies that take 60 seconds or longer, so the chat client notifies the user.
- Allowlist denials are logged as warnings and counted by `coreman_whitelist_denied_total{reason}`.
- Escalation create and poll responses include `delivery_failed` and `failure_reason`.

### Changed

- The bundled Claude driver now reports native Claude Code model names (`claude-sonnet-4-6`, `claude-opus-4-6`, `claude-haiku-4-5-20251001`) instead of names with a `vllm/` prefix. The prefix never had a functional meaning: the driver strips anything before the last `/` before calling the CLI. Migration 0023 renames the seeded `vllm/claude-*` catalog entries, prices and bot models to the native names.
- A run waits up to 120 seconds in the queue of a full runtime node instead of failing after the 10-second connect timeout. Reverse-channel calls expire after the bot's timeout (up to 12 hours) instead of a fixed 2 hours, and scheduled runs are no longer capped at 2 hours.
- Gateways drain bots in paced batches that fit the stop grace period.
- New business systems allow no bots until an administrator selects them, and the `coreman` system key is reserved.
- API containers trust forwarded client addresses only from the Caddy network (`COREMAN_EDGE_SUBNET`, default `10.250.250.0/28`).
- Scheduled results longer than 100,000 characters are truncated instead of failing the run.
- A new message in a session whose previous turn is still stopping waits and is re-queued instead of failing with "session busy".
- Database engines use a 10-second connect timeout and a 300-second statement timeout.
- Optimistic-lock conflicts return HTTP 409 with the dedicated code `40901`.
- Documentation opens with user-facing descriptions instead of release status notes.

### Fixed

- Escalations whose question notification cannot be delivered are cancelled instead of waiting until they expire.
- WeCom `gettoken` credential failures are cached briefly instead of being retried on every request.
- `deploy/coreman` refuses to generate new secrets when a PostgreSQL data volume already exists.
- The users page still loads when the team list request fails.
- Audit entries for skills, memories, the skill catalog and runtime nodes record the client IP.
- Listing a bot's skills no longer locks the bot row.

### Security

- Bot, environment preset, skill and MCP environment variables can no longer set runtime control variables such as model endpoints, loader hooks, proxies or CLI configuration directories. The Runtime Daemon enforces the same list.
- The application refuses to start with a template or short bootstrap administrator password.

### Removed

- The generated `docs/environment-creation/manual.html`; use `docs/environment-creation/README.md`.
- Unused compatibility routes `/api/robot/memories/*`, `/api/robot/wework-notify`, `/api/robot/organization/tree`, `/api/organization/full`, `/api/push` and `/api/test/bot-token-access`. The remaining escalation compatibility routes require signed requests and no longer accept a plain `X-API-Key` secret.
- The `cron` infrastructure API scope, which no route used, and the `ETEAMS_` reserved environment prefix.
- The `RELAY_NETWORK_MODE` and `TIMEZONE` settings.
- `system_grant_audit` is no longer written (grant changes are recorded in `audit_logs`) and `bots.custom_command_modules` is no longer read. Both will be dropped in the next release.

## [0.1.0] - TBD

First public release.

### Added

- WeCom integration through smart robot long connections and Feishu integration through custom app long connections: streaming replies, images and files, quoted messages, interactive question cards and welcome messages.
- AI teammate (bot) management: prompts, models, runtime binding, working directories, collaborators, usage allowlists, team ownership and encrypted credentials and environment variables.
- Runtime Daemon for Linux and macOS (amd64/arm64): connects outward to CoreMan, discovers Claude Code and Codex CLIs, runs requests through bundled Go drivers, reports health and quota, and supports install links, private CAs, in-place upgrade and uninstall.
- Teams, users and roles (platform administrator, AI committee, team lead, member), WeCom and Feishu sign-in, directory sync, bootstrap administrator and audit logs.
- Scheduled tasks with pre-check scripts and result delivery to private chats, group chats, email and WeCom group robot webhooks.
- Human escalation API with WeCom app and Feishu delivery, reply callbacks, queuing, reminders and follow-ups.
- Skill catalog synced from Git plugin marketplaces, environment presets, approval workflow and persistent installation tasks.
- Memory sync between runtime nodes and the admin console, including memory transfer when a bot switches runtime.
- Business system grants with per-request ES256 `BOT_TOKEN_<SYSTEM>` tokens and a public JWKS endpoint; signed infrastructure API for directory, push, notification and runtime telemetry.
- Chat logs, usage statistics with model pricing, bot health reports, announcements and runtime status with lease, queue, task and outbox views.
- Self-hosted deployment with Docker Compose and Caddy: PostgreSQL-backed task bus with leases and outbox, a/b gateways and workers, drain-based rolling upgrade and rollback, optional S3 attachment storage, Prometheus metrics and alert notifications.
- Admin console in Chinese, Japanese and English.

[Unreleased]: https://github.com/wxkingstar/CoreMan/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/wxkingstar/CoreMan/releases/tag/v0.1.0
