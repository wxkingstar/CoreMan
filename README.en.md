<p align="center"><img src="docs/brand/logo/v1/coreman-mark.svg" width="96" alt="CoreMan"></p>

# CoreMan

**Your AI teammate at work.**

[中文](README.md) · English

CoreMan is a self-hosted platform for AI teammates. It connects WeCom (WeChat Work) and Feishu (Lark) with AI CLIs running in environments you control. Team members work with AI teammates in chat, and administrators manage AI teammates, runtimes, skills, permissions and tasks from one console.

The project is at an early stage (0.1.0). CoreMan does not provide model quota: to use backends such as Claude Code or Codex, install and sign in to them yourself and make sure you are entitled to use the corresponding service.

Most detailed documentation is currently written in Chinese. The [glossary](docs/glossary.md) lists the English term for each concept, and the [architecture overview](docs/architecture.md) is in English.

## Features

- **WeCom and Feishu integration**: long-connection messaging, streaming replies, images and files, quoted messages, interactive question cards.
- **AI teammate management**: prompts, models, runtimes, collaborators, usage allowlists and team ownership.
- **Runtime management**: a standalone Runtime Daemon connects outward to the management service and provides Claude Code / Codex backends, health status and quota information.
- **Teams and permissions**: teams, users, roles, platform sign-in, directory sync, encrypted credentials and audit logs.
- **Tasks and collaboration**: scheduled runs, notifications, human escalation, session management, chat logs and runtime status.
- **Skills and memory**: skill catalog, installation approval, environment presets, memory sync, usage statistics and health reports.
- **Self-hosted operations**: Docker Compose, database migrations, multi-process coordination, drain-based upgrades, optional S3 storage and Prometheus monitoring.

## Quick start

You need Git, Python 3, Docker and Docker Compose v2. The first build downloads container images plus Python, Node.js and Go dependencies; the frontend and the Runtime installation bundles are built inside containers.

```bash
git clone https://github.com/wxkingstar/CoreMan.git
cd CoreMan
cp .env.example .env
# Edit .env: set a unique, strong BOOTSTRAP_ADMIN_PASSWORD
./deploy/coreman build
./deploy/coreman up
./deploy/coreman status
```

Open <http://localhost/> and sign in with `BOOTSTRAP_ADMIN_USERNAME` and `BOOTSTRAP_ADMIN_PASSWORD` from `.env`. The deploy tool fills in empty encryption and session keys, and replaces placeholder database and administrator passwords with random values. Back up `.env` and the database.

If the ports are taken, set `CADDY_HTTP_PORT=8080` and `CADDY_HTTPS_PORT=8443` in `.env`, and change `PUBLIC_BASE_URL` to `http://localhost:8080`. For production, configure an HTTPS domain with matching `PUBLIC_BASE_URL` and `CADDY_SITE_ADDRESS`, and make sure runtimes can reach that address.

First-time setup:

1. Create a team under "Teams & Users". Optionally configure directory sync and sign-in under "Platform Apps".
2. Under "Runtime Management", generate an install command and run it in the target user environment where the AI CLI is already installed and signed in. See [Runtime installation](runtime_daemon/README.md).
3. Create an AI teammate, enter the WeCom or Feishu bot credentials, bind an available runtime and model, then enable it. Platform-side preparation is described in [WeCom setup](docs/wecom.md) and [Feishu setup](docs/feishu.md).
4. Send a message from the chat platform and check the full path under "Chat Logs" and "Runtime Status".

Once platform sign-in works, you can disable bootstrap administrator sign-in in the settings. The AI CLI acts with the permissions of the system user that runs the Runtime. Use a dedicated, least-privilege user environment and isolate files and network access as needed. Prompt constraints are no substitute for operating-system isolation.

The console menu names above are translations; the console UI is available in Chinese, Japanese and English.

## Local development

The backend needs Python 3.12+, uv and PostgreSQL 16. The frontend needs Node.js 22.12+. Use a dedicated development database:

```bash
uv sync --all-groups
cp .env.example .env
# Edit .env: set a local DATABASE_URL, a strong administrator password, MASTER_KEY and SESSION_SECRET
# Generate MASTER_KEY:
python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())'
# Generate SESSION_SECRET:
python3 -c 'import secrets;print(secrets.token_urlsafe(32))'
uv run --env-file .env alembic upgrade head
uv run uvicorn coreman.api.main:app --reload --port 8000
# In another terminal:
cd web
npm ci
npm run dev
```

Open <http://localhost:5173/>. The dev proxy forwards `/api` to the backend on port 8000. The API and frontend alone do not process chat tasks; the full path also needs the gateways, workers and scheduler, so use Compose for end-to-end testing. When you run the API by hand, build the Runtime bundles separately, see [Runtime development](runtime_daemon/README.md).

## Checks

```bash
uv run pytest                          # database tests need Docker or a separate TEST_DATABASE_URL
uv run ruff check .
uv run mypy coreman
cd web
npx vitest run
npm run lint
npm run build
```

Never point tests at a production database. Permissions, callbacks, networking and AI CLI sign-in on real platforms must be verified in your own deployment; passing simulated tests does not mean the external platform integration is complete.

## Documentation and layout

Documents are in Chinese unless marked otherwise.

- Concepts and structure: [Glossary](docs/glossary.md) · [Architecture and service topology](docs/architecture.md#service-topology) (English)
- Integrations and features: [WeCom](docs/wecom.md) · [Feishu](docs/feishu.md) · [Skills and approval](docs/skills-management.md) · [Memory](docs/memories.md) · [Scheduled tasks](docs/cron-jobs.md) · [Human escalation](docs/escalations.md)
- Operations and integration: [Operations](docs/operations.md) · [Infrastructure API](docs/infrastructure-api.md) · [Object storage](docs/object-storage.md) · [Statistics and health reports](docs/statistics-and-health.md) · [IM reply deadlines](docs/im-reply-lifecycle.md)
- Runtime environments: [Runtime Daemon](runtime_daemon/README.md) · [Linux environment manual](docs/environment-creation/README.md)

`coreman/api` is the management API, `coreman/core` holds shared business components, and `coreman/runtime` contains the gateways, workers and scheduler. `web` is the Vue 3 admin console, `runtime_daemon` is the execution side, `deploy` holds deployment configuration, `migrations` holds database migrations and `tests` holds the test suites.

## Contributing and security

When filing an issue, include the version, reproduction steps and redacted logs. When opening a pull request, explain the purpose of the change and how you verified it. Do not upload real `.env` files, platform credentials, install tokens, chat logs, directory data, database backups or screenshots of internal systems. See [CONTRIBUTING.md](CONTRIBUTING.md).

For leaked credentials or exploitable vulnerabilities, contact the maintainers through the repository's private vulnerability reporting channel (once enabled). Do not disclose sensitive details in public issues. See the [security policy](SECURITY.md) for details.

## License

CoreMan is released under the [MIT License](LICENSE). Third-party code and icons keep their own copyright and licenses, see [third-party notices](THIRD_PARTY_NOTICES.md).
