# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Create a Feishu bot by scanning a QR code while creating an AI employee. The employee configuration is validated first, the app secret stays on the server and is consumed once, and an unused app can be reused on the next attempt. The app requests CoreMan's identity, messaging and management permissions plus every "Connect Feishu" tier at once; personal authorization still narrows what is used. Requires database migration `0034`.
- Feishu app page on the employee detail view: permissions and approval status, available "Connect Feishu" tiers, scan to add missing permissions, basic information and avatar, bot menu, availability, slash commands, requesting administrator approval and submitting releases.
- Runtime nodes get a stable management command at `~/.local/share/coreman-runtime/bin/coreman-runtime` for upgrading, uninstalling and re-registering the service. It follows the current release, runs from any directory, and every daemon start refreshes it, so nodes installed by earlier versions also get it.
- Installing with `curl … | sh -s -- --replace` replaces the runtime already installed for that user: the new bundle is downloaded and verified before anything is touched, then the old service is stopped and the whole install directory is moved to a sibling `coreman-runtime.bak-<timestamp>`. Nothing is deleted, and an install that fails afterwards prints the command that restores the old runtime.
- `coreman-runtime --register` registers the service again from the identity that `--uninstall` kept.

### Changed

- The create employee form now defaults to Feishu and shows only the key, name and runtime; the runtime is required and other settings keep their defaults under "More settings". The edit form is unchanged.
- QR-created Feishu agents get the built-in slash commands `/new`, `/stop`, `/sessions` and `/help`; other Feishu bots can add them from the Feishu app page. Built-in commands, including `sessions`, now also match when sent with a leading slash.
- The Feishu app permission list also requests `vc:meeting.meetingevent:read` and `im:chat.members:read`, and no longer reports protocol grants such as `auth:user.id:read` as missing.
- When a runtime is already installed, the installer now names the platform each side connects to and prints commands that run as pasted. The previous hint left out the change into the release directory, so it failed with `No module named 'runtime_daemon'`, and it suggested purging the node even when the service alone had been removed.

## [0.1.0] - 2026-09-16

First public release.

The Changed, Fixed, Security and Removed sections describe differences from the initial public snapshot (`bfdd299`, 2026-09-14) for deployments that tracked `main` before this tag. Such deployments must run database migrations up to `0028` and upgrade Runtime Daemon drivers together with the services; older drivers keep serving chat but do not support collaboration tools, detailed process output or workspace operations.

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
- Feishu bot collaboration (limited trial): administrators configure directed partner relationships, and in Feishu group chats an AI teammate can search for, inspect and ask a partner for help through a task-scoped MCP endpoint. The partner answers in the group and the original teammate resumes its runtime session. Membership and permissions are checked on every request, and the server enforces a single hop and tool call budgets.
- Feishu replies show a typing reaction while a message is handled, a collapsible thinking panel with an animated heading, and detailed tool and process output from the Claude Code and Codex drivers. The reaction needs the `im:message.reactions:write_only` app permission.
- Workspace management for AI teammates: browse and edit runtime files from the console, back them up to Git, initialize `AGENTS.md` (with `CLAUDE.md` linked to it), and move files and memory to the new runtime (copy, Git or existing directory) when switching runtime nodes.
- Scheduled runs add an overridable "scheduled task constraints" prompt section (`prompt_cron_mode`), editable in Settings.
- Scheduled pushes carry a header (task, bot, duration) and a footer. Jobs without any delivery target send the result to the job creator.
- A separate completion reminder after streamed replies that take 60 seconds or longer, so the chat client notifies the user.
- Allowlist denials are logged as warnings and counted by `coreman_whitelist_denied_total{reason}`.
- Escalation create and poll responses include `delivery_failed` and `failure_reason`.
- WeCom setup and troubleshooting guide (`docs/wecom.md`), a glossary (`docs/glossary.md`), a service topology diagram in `docs/architecture.md`, and an English README (`README.en.md`).
- Trust model and threat boundaries in `SECURITY.md`.

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
- Claude Code context compaction no longer trips the resume watchdog on long sessions.

### Security

- Bot, environment preset, skill and MCP environment variables can no longer set runtime control variables such as model endpoints, loader hooks, proxies or CLI configuration directories. The Runtime Daemon enforces the same list.
- The application refuses to start with a template or short bootstrap administrator password.

### Removed

- The generated `docs/environment-creation/manual.html`; use `docs/environment-creation/README.md`.
- Unused compatibility routes `/api/robot/memories/*`, `/api/robot/wework-notify`, `/api/robot/organization/tree`, `/api/organization/full`, `/api/push` and `/api/test/bot-token-access`. The remaining escalation compatibility routes require signed requests and no longer accept a plain `X-API-Key` secret.
- The `cron` infrastructure API scope, which no route used, and the `ETEAMS_` reserved environment prefix.
- The `RELAY_NETWORK_MODE` and `TIMEZONE` settings.
- `system_grant_audit` is no longer written (grant changes are recorded in `audit_logs`) and `bots.custom_command_modules` is no longer read. Both will be dropped in the next release.

[Unreleased]: https://github.com/wxkingstar/CoreMan/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/wxkingstar/CoreMan/releases/tag/v0.1.0
