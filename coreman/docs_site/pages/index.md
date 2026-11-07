# CoreMan 文档

CoreMan 是一个 AI 员工平台：在飞书和企业微信里运行 AI 员工，由管理台统一管理它们的模型、技能、业务系统权限和运行节点。这里是给成员和外部程序用的说明，人和 AI 读的是同一份内容。

本站地址：`{{COREMAN_BASE_URL}}/docs`。

## 能做什么

| 主题 | 给谁 | 一句话 |
|---|---|---|
| [本机接入业务系统目录](systems-catalog-mcp.md) | 用 Claude Code、Codex 等本机 Agent 的成员 | 把 CoreMan 的业务系统操作目录接成 MCP，Agent 查到接口后用你自己的令牌直连业务系统。 |
| [通过 API 驱动 AI 员工](api-tasks.md) | 脚本、外部系统、你自己的 Agent | 用个人 API 令牌给飞书 AI 员工下一件事，在飞书会话里进行，并拿回结果。 |
| [业务系统 OpenAPI 接入规范](business-system-contract.md) | 想让 AI 员工调用自己 API 的业务系统 | 按这份契约提供 OpenAPI 描述，AI 员工就能按需发现并调用你的操作。 |

## 给 AI 读

每一页都有 Markdown 原文，链接可以直接交给 Agent：

| 地址 | 内容 |
|---|---|
| `{{COREMAN_BASE_URL}}/docs/llms.txt` | 索引：每一页的标题、说明和 Markdown 地址（[llms.txt](https://llmstxt.org/) 格式）。 |
| `{{COREMAN_BASE_URL}}/docs/llms-full.txt` | 全部页面合在一份 Markdown 里。 |
| `{{COREMAN_BASE_URL}}/docs/<页面>.md` | 单页 Markdown，例如 `{{COREMAN_BASE_URL}}/docs/api-tasks.md`。 |

网页地址加 `?for=ai`，或请求头带 `Accept: text/markdown`，返回的也是这一页的 Markdown。文中的地址都已填成本 CoreMan 的地址，可以照抄。

## 访问

文档不需要登录。能打开管理台的网络就能打开文档；部署方如果只在内网开放管理台，文档也只在内网可用。
