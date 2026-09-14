<p align="center"><img src="docs/brand/logo/v1/coreman-mark.svg" width="96" alt="CoreMan"></p>

# CoreMan

**融入团队的 AI 员工。Your AI teammate at work.**

CoreMan 是一个可自托管的 AI 员工平台，把企业微信、飞书与运行在自有环境中的 AI CLI 连接起来。团队成员在聊天中协作，管理员在统一后台管理 AI 员工、运行时、技能、权限与任务。

项目处于早期迭代阶段（0.1.0）。平台本身不提供模型额度；使用 Claude Code / Codex 等后端需要自行安装、登录并获得对应服务的使用权限。

## 主要能力

- **企业微信与飞书接入**：长连接收发、流式回复、图片与文件、引用消息、交互问答卡片。
- **AI 员工管理**：提示词、模型、运行时、协作者、使用白名单及团队归属。
- **运行时管理**：独立 Runtime Daemon 主动连接管理端，提供 Claude Code / Codex 后端、健康状态与额度信息；也可接入兼容的中继实例。
- **团队与权限**：团队、用户、角色、平台登录、通讯录同步、加密凭证与操作审计。
- **任务与协作**：定时执行、通知、人工求助、会话管理、对话记录及运行状态。
- **技能与记忆**：技能目录、安装审批、环境配置、记忆同步、使用统计与体检。
- **自托管运维**：Docker Compose、数据库迁移、多进程协调、排空升级、可选 S3 存储与 Prometheus 监控。

## 快速开始

准备 Git、Python 3、Docker 与 Docker Compose v2。首次构建需要能下载容器镜像及 Python、Node.js、Go 依赖；容器内完成前端与 Runtime 安装包构建。

```bash
git clone https://github.com/wxkingstar/CoreMan.git
cd CoreMan
cp .env.example .env
# 编辑 .env：为 BOOTSTRAP_ADMIN_PASSWORD 设置独立强密码
./deploy/coreman build
./deploy/coreman up
./deploy/coreman status
```

浏览器打开 <http://localhost/>，使用 `.env` 中的 `BOOTSTRAP_ADMIN_USERNAME` 和 `BOOTSTRAP_ADMIN_PASSWORD` 登录。启动工具会补齐空的加密/会话密钥，并随机替换占位的数据库及管理员密码；请妥善备份 `.env` 与数据库。

端口冲突时，在 `.env` 中设置 `CADDY_HTTP_PORT=8080`、`CADDY_HTTPS_PORT=8443`，并把 `PUBLIC_BASE_URL` 改为 `http://localhost:8080`。生产部署应设置 HTTPS 域名、对应的 `PUBLIC_BASE_URL` 与 `CADDY_SITE_ADDRESS`，并确保 Runtime 可以访问该地址。

首次配置流程：

1. 在「团队与用户」中建立团队，按需在「平台应用」配置通讯录同步及登录。
2. 在「运行时管理」生成安装命令，在已经安装并登录 AI CLI 的目标用户环境中执行；详见 [Runtime 安装](runtime_daemon/README.md)。
3. 创建 AI 员工，填写企业微信或飞书机器人凭证，绑定可用运行时和模型，再启用。
4. 从对应聊天平台发送消息，在「对话记录」和「运行状态」验证完整链路。

平台登录验证成功后，可在设置中关闭引导管理员登录。AI CLI 会以 Runtime 所属系统用户的权限执行操作；请使用独立、最小权限的用户环境，并按需隔离文件和网络。提示词约束不能替代操作系统隔离。

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

访问 <http://localhost:5173/>；开发代理将 `/api` 转发到后端 8000 端口。仅启动 API 和前端不会处理聊天任务，完整链路还需要网关、worker 与 scheduler，建议使用 Compose 联调。手工启动 API 时，Runtime 安装包需另行构建，见 [Runtime 开发说明](runtime_daemon/README.md)。

## 验证

```bash
uv run pytest                          # 数据库测试需 Docker 或独立 TEST_DATABASE_URL
uv run ruff check .
uv run mypy coreman
cd web
npx vitest run
npm run lint
npm run build
```

请勿让测试连接生产数据库。真实平台的权限、回调、网络及 AI CLI 登录状态需要在自己的部署环境中验证；模拟测试通过不代表外部平台接入已完成。

## 文档与目录

- 接入与能力：[飞书](docs/feishu.md) · [技能与审批](docs/skills-management.md) · [记忆](docs/memories.md) · [定时任务](docs/cron-jobs.md) · [人工求助](docs/escalations.md)
- 运维与集成：[运行维护](docs/operations.md) · [基础设施 API](docs/infrastructure-api.md) · [对象存储](docs/object-storage.md) · [统计与体检](docs/statistics-and-health.md) · [IM 回复时限](docs/im-reply-lifecycle.md)
- 运行环境：[Runtime Daemon](runtime_daemon/README.md) · [Relay Agent](relay_agent/README.md) · [Linux 环境手册](docs/environment-creation/README.md)（[可直接打开的 HTML](docs/environment-creation/manual.html)）

`coreman/api` 为管理 API，`coreman/core` 为共享业务组件，`coreman/runtime` 为网关/worker/scheduler；`web` 为 Vue 3 管理台，`runtime_daemon` 与 `relay_agent` 为执行端，`deploy` 为部署配置，`migrations` 为数据库迁移，`tests` 为测试。

## 参与贡献与安全

提交 Issue 时请提供版本、复现步骤及脱敏日志；提交 Pull Request 时说明改动目的和验证结果。不要上传真实 `.env`、平台凭据、安装令牌、聊天记录、通讯录、数据库备份或内部系统截图。

涉及凭据泄露或可利用漏洞时，请通过仓库的私密漏洞报告渠道（启用后可用）联系维护者，不要在公开 Issue 中披露敏感细节。更多约定见 [安全说明](SECURITY.md)。

## 许可证

本项目采用 [MIT License](LICENSE)。第三方代码和图标保留各自的版权与许可证，见 [第三方声明](THIRD_PARTY_NOTICES.md)。
