# Contributing

Issues and pull requests are welcome. Describe the behavior you want to change, include a minimal reproduction, and explain how the change was verified.

Use a dedicated development database. Never commit credentials, production data, personal conversations, installation tokens, or internal screenshots. Follow [SECURITY.md](SECURITY.md) for private vulnerability reports.

## Local development

The backend needs Python 3.12+, uv and PostgreSQL 16. The frontend needs Node.js 22.12+.

```sh
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

Open <http://localhost:5173/>. The dev proxy forwards `/api` to the backend on port 8000. The API and frontend alone do not process chat tasks; the full path also needs the gateways, workers and scheduler, so use Compose for end-to-end testing. When you run the API by hand, build the runtime bundles separately, see [Runtime development](runtime_daemon/README.md).

`coreman/api` is the management API, `coreman/core` holds shared business components, and `coreman/runtime` contains the gateways, workers and scheduler. `web` is the Vue 3 admin console, `runtime_daemon` is the execution side, `deploy` holds deployment configuration, `migrations` holds database migrations and `tests` holds the test suites.

## Checks

Before submitting, run the checks relevant to your change:

```sh
uv run pytest
uv run ruff check .
uv run mypy coreman
cd web
npm ci
npx vitest run
npm run lint
npm run build
```

`uv run pytest -n auto` runs the suite in parallel. With `TEST_DATABASE_URL` set, each worker recreates its own database `<name>_gw<N>` on that server, so the role needs `CREATEDB`.

Runtime driver changes also require `go test ./...` from `runtime_daemon/drivers`.

Never point tests at a production database. Permissions, callbacks, networking and AI CLI sign-in on real platforms must be verified in your own deployment; passing simulated tests does not mean the external platform integration is complete.

A Chinese version of these notes is in [docs/development.md](docs/development.md).

Only contribute work you have permission to share under this project's MIT license. Keep existing third-party copyright and license notices intact.
