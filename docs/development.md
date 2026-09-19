# 本地开发与测试

只想试用或部署 CoreMan 时，按 [README 的快速开始](../README.md#快速开始) 用 Docker Compose 启动即可。本文面向需要修改代码的开发者。

## 本地开发

后端需要 Python 3.12+、uv 与 PostgreSQL 16；前端需要 Node.js 22.12+。以下命令使用专用开发数据库：

```bash
uv sync --all-groups
cp .env.example .env
# 编辑 .env：配置本地 DATABASE_URL、强管理员密码、MASTER_KEY、SESSION_SECRET
# 生成 MASTER_KEY：
python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())'
# 生成 SESSION_SECRET：
python3 -c 'import secrets;print(secrets.token_urlsafe(32))'
uv run --env-file .env alembic upgrade head
uv run uvicorn coreman.api.main:app --reload --port 8000
# 另一个终端：
cd web
npm ci
npm run dev
```

访问 <http://localhost:5173/>；开发代理将 `/api` 转发到后端 8000 端口。仅启动 API 和前端不会处理聊天任务，完整链路还需要网关、worker 与 scheduler，建议使用 Compose 联调。手工启动 API 时，Runtime 安装包需另行构建，见 [Runtime 开发说明](../runtime_daemon/README.md)。

## 验证

```bash
uv run pytest                          # 数据库测试需 Docker 或独立 TEST_DATABASE_URL
uv run pytest -n auto                  # 并行：每个 worker 在 TEST_DATABASE_URL 所在服务器上建独立库，账号需 CREATEDB
uv run ruff check .
uv run mypy coreman
cd web
npx vitest run
npm run lint
npm run build
```

改动 Runtime 驱动时，还需在 `runtime_daemon/drivers` 下执行 `go test ./...`。

请勿让测试连接生产数据库。真实平台的权限、回调、网络及 AI CLI 登录状态需要在自己的部署环境中验证；模拟测试通过不代表外部平台接入已完成。

## 目录结构

| 目录 | 内容 |
|---|---|
| `coreman/api` | 管理 API |
| `coreman/core` | 共享业务组件 |
| `coreman/runtime` | 网关、worker 与 scheduler |
| `web` | Vue 3 管理台 |
| `runtime_daemon` | 执行端（Runtime Daemon 与 Go 驱动） |
| `deploy` | Docker Compose、Caddy 与部署脚本 |
| `migrations` | 数据库迁移 |
| `tests` | 测试 |

提交 Pull Request 前请阅读 [CONTRIBUTING.md](../CONTRIBUTING.md) 与 [安全说明](../SECURITY.md)。
