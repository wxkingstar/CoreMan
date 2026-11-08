# 本机接入业务系统目录（MCP）

CoreMan 登记了公司各业务系统的 OpenAPI 描述，并整理成分层的操作目录：有哪些系统、每个系统有哪些模块和操作、每个操作的参数和调用方式。AI 员工靠它按需找接口。成员本机的 Claude Code、Codex 等 Agent 也可以接入同一份目录：

- CoreMan 只提供目录，不签发令牌、不代理调用。
- Agent 查到操作后，用你本机环境变量里的令牌直接调用业务系统，令牌不经过 CoreMan。
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

端点是标准的 MCP Streamable HTTP：只用 POST，返回 JSON，不需要认证头。

## 3. 配置客户端

### Claude Code

```bash
claude mcp add --transport http coreman-systems '{{COREMAN_BASE_URL}}/api/systems/mcp?token_env=SSO_TOKEN'
```

### Codex

```bash
codex mcp add coreman_systems --url '{{COREMAN_BASE_URL}}/api/systems/mcp?token_env=SSO_TOKEN'
```

或者写进 `~/.codex/config.toml`：

```toml
[mcp_servers.coreman_systems]
url = "{{COREMAN_BASE_URL}}/api/systems/mcp?token_env=SSO_TOKEN"
```

不要配 `--bearer-token-env-var` 这类认证参数：端点不需要认证，令牌也不该发给 CoreMan。

### 其他客户端

支持远程 HTTP MCP 的客户端都能接，填同一个地址即可。JSON 配置一般是下面这样，字段名以客户端文档为准：

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
| 404 | 这个 CoreMan 没有开放匿名目录 | 请管理员开放；未开放时这个端点不存在。 |
| 403 `Browser origins are not supported` | 请求带了 `Origin` 头，例如从浏览器页面发起 | 只从本机 Agent 或命令行调用。 |
| 400 `token_env must be an environment variable name` | `token_env` 不是合法的变量名 | 改成字母、数字、下划线组成的变量名。 |
| `catalog_unavailable` | 这个系统的目录还没拉取过 | 请管理员在管理台「业务系统」里点「刷新目录」。 |
| 业务系统返回 401 | 本机变量没设、令牌过期，或这个系统不认这种令牌 | 检查 `echo ${SSO_TOKEN:+set}`；只认平台自签令牌的系统，本机通常没有可用的令牌。 |
| 业务系统返回 403 | 你的账号没有这个操作的权限 | 去业务系统申请权限，不要换别人的令牌。 |

## 6. 说明

- 目录包含已启用、并且配了系统地址和 OpenAPI 地址的系统。
- 目录随 AI 员工的使用和管理员「刷新目录」更新，匿名请求只读已存的目录。
- 请求体最大 16 KiB；`GET` 返回 405。
- 匿名调用没有调用次数预算，也不关联任何任务。
