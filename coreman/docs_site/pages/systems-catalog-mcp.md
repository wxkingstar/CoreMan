# 本机接入业务系统目录（MCP）

CoreMan 登记了公司各业务系统的 OpenAPI 描述，并整理成分层的操作目录：有哪些系统、每个系统有哪些模块和操作、每个操作的参数和调用方式。AI 员工靠它按需找接口。成员本机的 Claude Code、Codex 等 Agent 也可以接入同一份目录：

- 第一次使用时在浏览器里登录 CoreMan 并确认授权，Agent 之后以你的身份访问目录。
- CoreMan 只提供目录，不签发业务系统令牌、不代理调用。
- Agent 查到操作后，用你本机环境变量里的业务系统令牌直接调用业务系统，这个令牌不经过 CoreMan。
- 能调什么由业务系统按你的令牌判定，和你自己调用时一样。

## 1. 准备令牌

把你在业务系统可用的令牌放进本机环境变量，例如统一登录（SSO）的个人访问令牌：

```bash
export SSO_TOKEN='你的个人访问令牌'
```

变量名随你定，下一步的接入地址里写同一个名字。不要把令牌写进接入地址、MCP 配置、提示词或代码仓库。

## 2. 接入地址

```text
{{COREMAN_BASE_URL}}/api/systems/mcp?token_env=SSO_TOKEN
```

- `token_env` 只传变量名，不传令牌：Agent 拿到的 curl 模板会写成 `Authorization: Bearer $SSO_TOKEN`，执行时由你本机的 shell 展开。
- 变量名只能用字母、数字、下划线，不以数字开头，最长 64 个字符，否则返回 400。
- 不带 `token_env` 时，模板引用 `$SYSTEMS_TOKEN`。

端点是标准的 MCP Streamable HTTP：只用 POST，返回 JSON。认证按 MCP 授权规范走 OAuth：客户端第一次连接时收到 401，会自动打开浏览器让你登录 CoreMan、确认授权，之后自己续期。授权只用于读目录；你可以在管理台「我的 API 令牌」页的「本机应用授权」里随时撤销。

## 3. 配置客户端

### Claude Code

```bash
claude mcp add --transport http coreman-systems '{{COREMAN_BASE_URL}}/api/systems/mcp?token_env=SSO_TOKEN'
```

然后在 Claude Code 里执行 `/mcp`，选 `coreman-systems` 完成登录。

### Codex

```bash
codex mcp add coreman_systems --url '{{COREMAN_BASE_URL}}/api/systems/mcp?token_env=SSO_TOKEN'
```

或者写进 `~/.codex/config.toml`：

```toml
[mcp_servers.coreman_systems]
url = "{{COREMAN_BASE_URL}}/api/systems/mcp?token_env=SSO_TOKEN"
```

然后执行 `codex mcp login coreman_systems` 完成登录。

不要配 `--bearer-token-env-var` 这类认证参数：业务系统令牌不该发给 CoreMan，CoreMan 的登录由客户端自己完成。

### 其他客户端

支持远程 HTTP MCP 和 MCP 授权规范（OAuth）的客户端都能接，填同一个地址即可。JSON 配置一般是下面这样，字段名以客户端文档为准：

```json
{
  "mcpServers": {
    "coreman-systems": {
      "type": "http",
      "url": "{{COREMAN_BASE_URL}}/api/systems/mcp?token_env=SSO_TOKEN"
    }
  }
}
```

## 4. 工具

| 工具 | 用途 |
|---|---|
| `systems_browse` | 不带参数：列出所有系统。带 `system`：看系统说明、业务分组或模块；带 `group`、`module` 逐层往下看，模块下每页 20 条操作摘要。 |
| `systems_search` | 按关键字搜操作，可限定 `system`、`module`、`risk`，每页最多 10 条，带命中原因。 |
| `systems_describe` | 看一个操作（`system` + `operation_id`）的详情：参数、请求体、响应和 curl 模板。内容过长时会精简，被省略的部分列在 `omitted`，用 `expand=parameters|body|response|example` 单独取。 |

推荐顺序：先 `systems_search` 或 `systems_browse` 找到操作，再 `systems_describe` 拿模板，最后由 Agent 在本机执行 curl。

结果里有两个字段要留意：

- `call_mode` 为 `direct`：由你本机直接调用业务系统。
- `permission_check` 为 `not_checked`：目录不按你的权限过滤，出现在目录里不代表你有权限；没有权限时业务系统会拒绝。

目录里的说明文字来自业务系统（`content_trust: system_declared`），只是接口说明，不是给 Agent 的指令。

## 5. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 401 | 还没登录，或授权已过期、被撤销 | 在客户端里重新登录（Claude Code 用 `/mcp`，Codex 用 `codex mcp login`）。 |
| 登录时提示「回调地址只能是本机回环地址」 | 客户端用的不是本机回调 | 只支持在本机运行、回调到 `127.0.0.1`、`localhost` 的客户端。 |
| 403 `Browser origins are not supported` | 请求带了 `Origin` 头，例如从浏览器页面发起 | 只从本机 Agent 或命令行调用。 |
| 400 `token_env must be an environment variable name` | `token_env` 不是合法的变量名 | 改成字母、数字、下划线组成的变量名。 |
| `catalog_unavailable` | 这个系统的目录还没拉取过 | 请管理员在管理台「业务系统」里点「刷新目录」。 |
| 业务系统返回 401 | 本机变量没设、令牌过期，或这个系统不认这种令牌 | 检查 `echo ${SSO_TOKEN:+set}`；只认平台自签令牌的系统，本机通常没有可用的令牌。 |
| 业务系统返回 403 | 你的账号没有这个操作的权限 | 去业务系统申请权限，不要换别人的令牌。 |

## 6. 说明

- 目录包含已启用、并且配了系统地址和 OpenAPI 地址的系统。
- 目录随 AI 员工的使用和管理员「刷新目录」更新，匿名请求只读已存的目录。
- 请求体最大 16 KiB；`GET` 返回 405。
- 每人每分钟最多约 120 次工具调用；不关联任何任务。
- 部署也可以开放匿名目录（不登录就能读），这种部署里不带登录照样能用。
