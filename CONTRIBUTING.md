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

Runtime driver changes also require `go test ./...` from `runtime_daemon/drivers`.

Only contribute work you have permission to share under this project's MIT license. Keep existing third-party copyright and license notices intact.
