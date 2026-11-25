# 成员本机接入：CoreMan 授权服务器、按人鉴权的 MCP 与远程命令行

## 背景

成员本机的 Agent（Claude Code、Codex 等）和脚本想用 CoreMan 的能力，现在有两个入口，都不适合开放给成员：

- 业务系统目录 MCP（`/api/systems/mcp`）是匿名的：不认人、不按权限过滤、不审计、不限流，只能靠部署级开关 `SYSTEMS_CATALOG_PUBLIC` 加内网控制访问。
- 管理命令 `coreman` 在 API 进程里运行，要能进 API 容器才用得了，`--as` 可以填任何成员，审计里记的是 `--as` 的人而不是真正操作的人。

## 目标

- 成员用自己的身份接入 MCP 和命令行，权限、审计与管理台一致。
- 只依赖标准协议：OAuth 2.1（授权码 + PKCE）、RFC 8414 授权服务器元数据、RFC 9728 受保护资源元数据、RFC 7591 动态注册、RFC 7009 吊销；登录方式另外支持通用 OIDC。不依赖任何特定身份服务的私有约定。
- 不配置任何新东西时，现有行为不变。

## 非目标

- CoreMan 不做通用 OIDC 身份提供方（不给其他系统发 `id_token`）。
- 不改变管理台的权限模型：令牌只代表「这个成员」，能做什么仍按成员的角色和数据级权限判断。

## 设计

### 1. CoreMan 自己当授权服务器

CoreMan 签发自己的不透明令牌，登录沿用管理台已有的登录方式（飞书、企微扫码，以及第 4 节的通用 OIDC）。这样没有外部身份服务的部署照样能用，外部身份服务只是登录方式之一。

| 端点 | 说明 |
|---|---|
| `GET /.well-known/oauth-protected-resource[/<资源路径>]` | 受保护资源元数据：资源标识、授权服务器、支持的 scope |
| `GET /.well-known/oauth-authorization-server` | 授权服务器元数据；issuer 是 `PUBLIC_BASE_URL` |
| `POST /api/oauth/register` | 动态注册，只接受公开客户端（`token_endpoint_auth_method=none`）和本机回环回调（`http://127.0.0.1`、`http://localhost`、`http://[::1]`，端口不限，按 RFC 8252 比较时忽略端口） |
| `GET /api/oauth/authorize` | 校验客户端与回调；没登录先去登录页，登录后回到这里；显示授权确认页 |
| `POST /api/oauth/authorize` | 确认页提交：允许则带 `code`、`state`、`iss` 回到回调，拒绝则带 `error=access_denied` |
| `POST /api/oauth/token` | `authorization_code`（必须 PKCE S256）、`refresh_token`（每次轮换） |
| `POST /api/oauth/revoke` | 吊销访问令牌或续期令牌，整次授权一起失效 |

令牌：

- 访问令牌 `cmo_` 开头、1 小时；续期令牌 `cmr_` 开头、30 天，每次使用都换新。旧续期令牌再次出现视为泄露，整次授权吊销。
- 库里只存 SHA-256。每个请求都查库，并复核成员仍为启用状态，吊销与停用立即生效。
- scope：`mcp`（业务系统目录 MCP）、`api`（以本人身份调管理 API，命令行用）。一枚令牌只能用于它的 scope。
- 授权码 60 秒、单次使用，绑定客户端、回调地址和 PKCE。
- 确认页的提交凭证单次使用、10 分钟、绑定当前登录会话。确认页列出客户端自报的名称（标明「自报」）、回调端口和 scope 的中文说明。

成员在「我的 API 令牌」页看到自己的授权（客户端名称、scope、最近使用时间）并可撤销。

### 2. Bearer 认证进入管理 API

`current_user` 遇到 `Authorization: Bearer` 时只按令牌认人，不再回落到 cookie；令牌必须带 `api` scope。带 `Origin` 头的 Bearer 请求一律拒绝（令牌给脚本和本机 Agent 用）。Bearer 请求不做 CSRF 校验：浏览器跨站无法附带这个头。审计照常记操作人，登录方式记为 `oauth`。

### 3. 业务系统目录 MCP 按人鉴权

同一地址 `/api/systems/mcp`：

- 带 Bearer：按令牌认人（`mcp` scope），每个成员按调用次数限流；目录按本人权限过滤，平台代理的系统以本人身份代为调用（第 4 期，见 [2026-11-25-member-mcp-proxy.md](2026-11-25-member-mcp-proxy.md)）。
- 不带 Bearer：部署开了 `SYSTEMS_CATALOG_PUBLIC` 时照旧匿名；没开时返回 401 和 `WWW-Authenticate: Bearer resource_metadata=…`，支持 MCP 授权规范的客户端会自动走第 1 节的登录流程。

### 4. 通用 OIDC 登录

部署配置 `OIDC_ISSUER`、`OIDC_CLIENT_ID`、`OIDC_CLIENT_SECRET`（可选 `OIDC_DISPLAY_NAME`、`OIDC_SCOPES`）后，登录页多一个按钮。流程：发现文档 → 授权码 + PKCE + `nonce` → 后端换令牌 → 用 JWKS 验 `id_token`（`iss`、`aud`、`exp`、`nonce`）→ 按 `email` claim 对应已同步的成员（不区分大小写，唯一且启用才放行，不自动建号）。没配置时登录页不变。

### 5. 远程命令行

`coreman` 增加远程模式：`coreman login --server <地址>` 走第 1 节的授权码流程（本机回环回调），令牌存在本机配置目录（权限 0600）；之后的管理命令通过 HTTP 调管理 API，权限与审计和管理台一致。不写 `--as` 时走远程模式。原来在 API 容器里运行的方式保留给运维和首次初始化，会话的 User-Agent 记下执行命令的系统账号与主机。

## 分期

1. 授权服务器 + Bearer 认证 + MCP 按人鉴权（第 1–3 节）。
2. 通用 OIDC 登录（第 4 节）。
3. 远程命令行（第 5 节）。
4. MCP 按本人权限过滤与平台代理调用。

## 部署注意

- 根路径的 `/.well-known/oauth-*` 要转发到 API（自带的 Caddy 配置已整体转发）。
- 开放到公网前确认登录方式只允许本组织成员。
