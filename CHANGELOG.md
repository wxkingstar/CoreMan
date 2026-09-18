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
- The runtime nodes page shows each node's Git host allowlist (`git_hosts` in the node's `config.json`), which nodes now report in their heartbeat. Entries that can never match a repository, such as a URL, are flagged, and nodes that do not report it yet show it as not reported. The list is read-only; it is still changed in `config.json` and applied on restart. Requires database migration `0035`.
- Skill managers can enable or disable a skill from the status column of the skill catalog without opening the editor (`PATCH /api/admin/skills/{id}`). Like a full save, a change of status bumps the skill revision, so pending approvals and queued installs for that skill must be requested again.
- A Feishu employee who scans the sign-in QR code before the contacts have been synced no longer gets "No matching account found". CoreMan asks the Feishu app with contact sync (the sign-in app itself when it has that capability) for just that member and signs them in. Only members within the app's contact scope are added; departments are not created and nobody is disabled, so a full sync is still needed for new departments and departures. The merge is audited as `user.login_sync`.
- Runtime management can now change a node's team (its Claude Code and Codex instances follow; employees already bound are unaffected) and delete a node together with its instances. Deleting is refused while AI employees are bound to it or are being moved onto it; it cancels calls in progress, keeps the installation link as a record and invalidates the node credential, so the Daemon on that host must be uninstalled separately.
- Business system tokens can be signed with an existing issuer's ES256 key, so systems that already trust that issuer accept them without changes. Set `BOT_JWT_PRIVATE_KEY`, `BOT_JWT_KID` and `BOT_JWT_ISSUER` (and optionally `BOT_JWT_PUBLIC_KEY`, which must match); a bad configuration stops startup. Tokens then carry that kid and issuer and keep the system in `scope` without `aud`. The key is published in `/api/.well-known/jwks.json` and shown as deployment-configured under identity signing keys; CoreMan still accepts only its own keys for `bot_token` sign-in.
- Speakers can view their own private chat sessions (Feishu and WeCom) after signing in, whatever their role. Forwarding the link to someone else, opening it with a bot token, or a session that also holds another person's messages all fail. Replies also carry an encrypted link bound to the speaker, the session and the runtime node, valid for 24 hours; it proves ownership before the first turn has been logged. Opening a link while signed out goes to the sign-in page and comes back afterwards; inside the Feishu client, the sign-in page starts Feishu sign-in straight away, as it already did for WeCom. Every opened session page is audited as `runtime.session_view`, recording whether the link, the owner or the viewer's role granted access. Speakers who are not administrators can view only once the node's runtime is upgraded and reports `owner_session_view_v1`. Older runtimes still record environment variables, so until then only administrators can.
- Feishu personal-data mode shows the "查看完整思考过程" button again. The runtime keeps these sessions in memory only, never on disk, and drops them after 24 hours or when it restarts. These sessions open only from the link, even for an administrator who is the speaker, and the link is bound to the grant's context, so switching modes, revoking or reauthorizing kills older links. Personal-mode chat tasks are marked `feishu_personal` in their result so the session page can recognize them. The button appears only once the node's runtime reports support (`owner_session_view_v1`), so runtime nodes need an upgrade.

### Changed

- Business system tokens now use the speaker's email prefix (lowercased) as `sub`, which is how business systems usually look up their own accounts. A login name is generated only on the first contact sync and falls back to the platform user ID when no email was available then, so Feishu members synced without the `contact:user.email:readonly` permission got tokens that business systems could not match. The login name is still used when a user has no email or shares an email prefix with another account. The access test reports the token user it used.
- Group chat sessions are still viewable only by `ai_committee` and `platform_admin`, and administrators can still view WeCom private chats of other people. Administrators still cannot view other people's Feishu private chats.
- The `feishu_login_unknown_user` and `wecom_login_unknown_user` log events include the platform `user_id`, so a rejected sign-in can be traced to a person.
- The skills dialog on the employee page keeps every row on one line: skill descriptions and install errors are truncated and shown in full on hover, like the skill catalog, and long names, versions and labels are shortened with an ellipsis instead of wrapping.
- The create employee form now defaults to Feishu and shows only the key, name and runtime; the runtime is required and other settings keep their defaults under "More settings". The edit form is unchanged.
- QR-created Feishu agents get the built-in slash commands `/new`, `/stop`, `/sessions`, `/connect` and `/help`; other Feishu bots can add them from the Feishu app page, where "Add built-in commands" also adds `/connect` to bots that already have the others. Built-in commands, including `sessions`, now also match when sent with a leading slash. `/connect` does the same as sending "连接飞书" (Connect Feishu); a bare `connect` without the slash is still an ordinary message.
- The Feishu app permission list also requests `vc:meeting.meetingevent:read` and `im:chat.members:read`, and no longer reports protocol grants such as `auth:user.id:read` as missing.
- When a runtime is already installed, the installer now names the platform each side connects to and prints commands that run as pasted. The previous hint left out the change into the release directory, so it failed with `No module named 'runtime_daemon'`, and it suggested purging the node even when the service alone had been removed.
- Container images keep `/app` owned by root and read-only and precompile the application's bytecode at build time. Base images are pinned by digest (Dependabot bumps them), and uv's download cache stays out of the image. A release that changes only code now adds about 8 MB per image instead of rewriting a layer of more than 300 MB, and the dependency layer shrinks from 444 MB to 265 MB.
- Installing a skill no longer depends on the runtime node Git host allowlist: sources registered in the skill catalog install on any node, so internal Git servers such as a company GitLab work without editing the node configuration. The allowlist (`git_hosts`) still limits pulling, pushing, backing up and opening pull requests in employee workspaces, and it can now be set when creating an install link under Advanced settings. Runtime nodes need an upgrade to pick this up.
- An AI employee can take over an existing bot directory that another bot system still uses, for example one shared by several instance users, and keep sharing it with that system. "Use existing directory" when switching runtimes now accepts a directory that no AI employee has claimed (no `.coreman-workspace.json`) and says it will be taken over; a directory claimed by another employee is still refused. Taking over leaves the files as they are: an `AGENTS.md -> CLAUDE.md` layout (with `CLAUDE.md` tracked in Git) is kept instead of being turned around, a lone `CLAUDE.md` gets an `AGENTS.md` link instead of being moved to `CLAUDE.md.preserved-N`, and in a Git work tree the ownership marker is listed in `.git/info/exclude` so a deployment's `git clean -fd` does not delete it. Editing files in the workspace keeps their permissions and group, and new files take the umask or the directory's default ACL instead of `0600`, so other instance users sharing the directory can still read them. Runtime nodes need an upgrade to pick this up; older nodes can still only reuse a directory the employee already owns.

### Fixed

- Runtime session history no longer records a request's environment variables. They carry bot secrets and access tokens, and until now they were written to the node's session logs and streamed to the session page. Runtime nodes need an upgrade; logs written before then expire within the usual 72-hour cleanup.
- A failed skill installation now says why in the skill management dialog instead of always showing "安装未完成". Runtime nodes return the Agent's fixed error message (for example "工作目录不存在", or "Git 来源不在白名单内" from nodes that predate the change above, shown with a hint to upgrade the node or add the host to `git_hosts`) as an `operation_failed` result and write it to `runtime.log`; command output, paths and credentials are still never returned or logged. Checks that fail before anything reaches the node say that nothing was executed, and the worker logs every failure with the task, bot and skill IDs and an error code. Older nodes still report only `execution_failed`; the dialog then points to the node's `runtime.log` and suggests upgrading the node.
- A runtime node could pick up a new request up to a second late. The caller renewed its lease on every read, which briefly locked the request just as the node's long poll, woken by the new request, tried to claim it and skipped it. The lease is now renewed every 10 seconds.

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
