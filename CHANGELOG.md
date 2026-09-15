# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- WeCom setup and troubleshooting guide (`docs/wecom.md`), a glossary (`docs/glossary.md`), a service topology diagram in `docs/architecture.md`, and an English README (`README.en.md`).

### Changed

- The bundled Claude driver now reports native Claude Code model names (`claude-sonnet-4-6`, `claude-opus-4-6`, `claude-haiku-4-5-20251001`) instead of names with a `vllm/` prefix. The prefix never had a functional meaning: the driver strips anything before the last `/` before calling the CLI, so both forms reach the CLI unchanged.
- Existing `vllm/claude-*` model catalog entries are not migrated. The initial database migration still seeds them, so a new installation shows both the seeded `vllm/claude-*` entries and the native names reported by the first node heartbeat. Bots keep working with either name. Administrators can retire the entries they do not want in the model catalog and choose the default model there.
- Documentation opens with user-facing descriptions instead of release status notes.

### Fixed

### Removed

- The generated `docs/environment-creation/manual.html`; use `docs/environment-creation/README.md`.

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
