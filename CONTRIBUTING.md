# Contributing

Issues and pull requests are welcome. Describe the behavior you want to change, include a minimal reproduction, and explain how the change was verified.

Use a dedicated development database. Never commit credentials, production data, personal conversations, installation tokens, or internal screenshots. Follow [SECURITY.md](SECURITY.md) for private vulnerability reports.

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

Only contribute work you have permission to share under this project's MIT license. Keep existing third-party copyright and license notices intact.
