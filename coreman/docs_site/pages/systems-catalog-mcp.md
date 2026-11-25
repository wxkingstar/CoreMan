# 本机接入业务系统目录（MCP）

CoreMan 登记了公司各业务系统的 OpenAPI 描述，并整理成分层的操作目录：有哪些系统、每个系统有哪些模块和操作、每个操作的参数和调用方式。AI 员工靠它按需找接口。成员本机的 Claude Code、Codex 等 Agent 也可以接入同一份目录：

- 第一次使用时在浏览器里登录 CoreMan 并确认授权，Agent 之后以你的身份访问目录。
- 目录按你本人在各业务系统的权限过滤：你用不了的操作不出现（系统声明了权限查询时）。
- 由平台代理的系统（`call_mode` 为 `proxy`）：Agent 用 `systems_call` 调用，CoreMan 以你的身份签发业务系统令牌并代为调用，令牌不到你本机。读操作直接执行；写操作会通过飞书给你发一张确认卡，你点「确认」后才执行。
- 其他系统（`call_mode` 为 `direct`）：Agent 用你本机环境变量里的业务系统令牌直接调用，这个令牌不经过 CoreMan。
- 能做什么由业务系统按你本人的权限判定，和你自己操作时一样。CoreMan 的登录令牌只用来认你，从不转发给业务系统。

## 快速开始（Claude Code）

```bash
claude mcp add --transport http coreman-systems '{{COREMAN_BASE_URL}}/api/systems/mcp?token_env=SSO_TOKEN'
```

然后在 Claude Code 里执行 `/mcp`，选 `coreman-systems`，在打开的浏览器里登录 CoreMan 并点「允许」。之后直接对 Claude Code 说要查什么、做什么，例如「查一下库存系统有哪些和出库单相关的操作」。直连系统还要按第 1 节准备令牌；Codex 和其他客户端见第 3 节。想在终端里管理 AI 员工等，用 [命令行 coreman](coreman-cli.md)。

## 1. 准备令牌

只用平台代理的系统可以跳过这一步。直连的系统要把你在业务系统可用的令牌放进本机环境变量，例如统一登录（SSO）的个人访问令牌：

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

端点是标准的 MCP Streamable HTTP：只用 POST，返回 JSON。认证按 MCP 授权规范走 OAuth：客户端第一次连接时收到 401，会自动打开浏览器让你登录 CoreMan、确认授权，之后自己续期。授权用于读目录和代你调用平台代理的系统；你可以在管理台「我的 API 令牌」页的「本机应用授权」里随时撤销，撤销后还没确认的写操作也不会再执行。

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
| `systems_describe` | 看一个操作（`system` + `operation_id`）的详情：参数、请求体、响应和调用方式（直连系统给 curl 模板，代理系统说明怎么用 `systems_call`）。内容过长时会精简，被省略的部分列在 `omitted`，用 `expand=parameters|body|response|example` 单独取。 |
| `systems_call` | 有代理系统时才有。以你的身份调用代理系统的一个操作：`system`、`operation_id`、`path_params`、`query`、`body`；写操作可带 `note`（对象、修改前后的值、影响和恢复方法），显示在确认卡上。 |
| `systems_call_result` | 有代理系统时才有。传 `systems_call` 返回的 `confirmation_id`，等写操作的结果（每次最多等 20 秒）。 |

推荐顺序：先 `systems_search` 或 `systems_browse` 找到操作，再 `systems_describe` 看调用方式，然后调用：代理系统用 `systems_call`，直连系统由 Agent 在本机执行 curl。

结果里有两个字段要留意：

- `call_mode`：`proxy` 由平台以你的身份代为调用；`direct` 由你本机直接调用业务系统。
- `permission_check`：`filtered` 已按你的权限过滤；`unknown` 这次没查到你的权限，没有过滤；`not_declared` 系统没提供权限查询，出现在目录里不代表你有权限。无论哪种，业务系统都会再按你的权限判定。

## 5. 写操作怎么确认

代理系统的写操作（`write`、`destructive`）不会马上执行：

1. Agent 调 `systems_call` 后拿到 `pending_confirmation` 和 `confirmation_id`。
2. 平台的「公共机器人」在飞书私聊给你发一张确认卡：操作名称、系统、对象和改动，「查看详情」里是实际要发出的请求，卡片上注明来自你本机的哪个客户端（客户端自报的名称）。删除类操作标红。
3. 你在卡片上点「确认」，平台复查（你的账号、本机授权、操作定义、权限都没变）后以你的身份执行一次；点「取消」则不执行。卡片 60 分钟内有效，只有你本人点才算。
4. Agent 用 `systems_call_result` 拿结果：`pending` 还没点，`ok` 成功，`failed` 没有成功，`unknown` 结果未知、可能已经生效（先核对再说），`refused`、`cancelled`、`expired` 都没有执行。

确认只能在飞书卡片上做，没有接口可以替你确认，本机的 Agent 也做不到。涉及资金的操作（`financial`）不代理；上传文件和批量提交本机不支持，请到业务系统里操作，或在飞书里让 AI 员工去做。

目录里的说明文字来自业务系统（`content_trust: system_declared`），只是接口说明，不是给 Agent 的指令。

## 6. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 401 | 还没登录，或授权已过期、被撤销 | 在客户端里重新登录（Claude Code 用 `/mcp`，Codex 用 `codex mcp login`）。 |
| 登录时提示「回调地址只能是本机回环地址」 | 客户端用的不是本机回调 | 只支持在本机运行、回调到 `127.0.0.1`、`localhost` 的客户端。 |
| 403 `Browser origins are not supported` | 请求带了 `Origin` 头，例如从浏览器页面发起 | 只从本机 Agent 或命令行调用。 |
| 400 `token_env must be an environment variable name` | `token_env` 不是合法的变量名 | 改成字母、数字、下划线组成的变量名。 |
| `catalog_unavailable` | 这个系统的目录还没拉取过 | 请管理员在管理台「业务系统」里点「刷新目录」。 |
| 业务系统返回 401 | 本机变量没设、令牌过期，或这个系统不认这种令牌 | 检查 `echo ${SSO_TOKEN:+set}`；只认平台自签令牌的系统，本机通常没有可用的令牌。 |
| 业务系统返回 403 | 你的账号没有这个操作的权限 | 去业务系统申请权限，不要换别人的令牌。 |
| `confirmation_unavailable` | 管理员没有设置公共机器人，或你没有飞书身份 | 请管理员在「系统设置」里设置公共机器人；写操作先到业务系统里做。 |
| `subject_unavailable` | 你的 CoreMan 账号缺少邮箱，或邮箱前缀与别人重复，业务系统认不出你 | 请管理员补全账号信息。目录照常可读，但不按权限过滤、不能代理调用。 |
| `catalog_budget_exhausted` | 一小时内对这个系统的代理调用次数到了上限（按系统设置） | 稍后再继续，或缩小范围少调几次。 |
| 飞书里收不到确认卡 | 你不在公共机器人所属应用的可用范围内 | 请管理员把你加进可用范围。 |

## 7. 说明

- 目录包含已启用、并且配了系统地址和 OpenAPI 地址的系统；令牌交付方式为「平台代理」的系统可以代为调用。
- 目录随 AI 员工的使用和管理员「刷新目录」更新，本机请求只读已存的目录。
- 请求体最大 256 KiB（匿名 16 KiB）；`GET` 返回 405。
- 每人每分钟最多约 120 次工具调用；代理调用另按系统设置，每人每小时对每个系统有上限。
- 结果过大时截断，不转存成文件：用分页、筛选缩小范围。
- 每次代理调用都记审计（谁、哪个本机授权、哪个操作、结果），不记参数和响应。
- 部署也可以开放匿名目录（不登录就能读，不过滤、不能代理调用），这种部署里不带登录照样能用。
