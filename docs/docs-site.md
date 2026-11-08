# 文档站（/docs）

给成员和外部程序看的说明挂在 `/docs`，人和 AI 读同一份内容。设计参考了“网页 + 原文 Markdown”两用的 API 文档：网页有搜索、页面列表和本页目录；AI 拿 `llms.txt` 索引或单页 Markdown。

## 地址

| 地址 | 内容 |
|---|---|
| `/docs`、`/docs/<页面>` | 网页。带 `?for=ai`，或 `Accept` 含 `text/markdown` 且不含 `text/html` 时返回这一页的 Markdown |
| `/docs/<页面>.md` | 单页 Markdown |
| `/docs/llms.txt` | 索引（[llms.txt](https://llmstxt.org/) 格式），每页标题、说明和 Markdown 地址 |
| `/docs/llms-full.txt` | 全部页面合一 |
| `/integration/api-tasks-agent-guide.md`、`/integration/business-system-openapi-contract.md` | 文档站之前就公开发布的两份，原地址继续返回 Markdown |

都不需要登录。部署只在内网开放管理台时，文档也只在内网可用；需要对外开放时在入口上放行 `/docs/` 前缀。

## 源文件与生成

- 页面源文件是 `coreman/docs_site/pages/<slug>.md`，页面顺序、标题和一句话说明登记在 `coreman/docs_site/__init__.py` 的 `PAGES`。新增页面两处都要加。
- API 进程按需用 markdown-it-py 渲染，按 `PUBLIC_BASE_URL` 缓存，不需要构建步骤；改完源文件重启 API 即可。
- 源文件里写 `{{COREMAN_BASE_URL}}` 代表 CoreMan 地址，输出时换成 `PUBLIC_BASE_URL`，方便照抄。示例里的其他域名一律用 `example.com`。
- 页面之间用相对链接 `api-tasks.md`：Markdown 输出补成绝对地址，网页里改成 `/docs/api-tasks`。
- 不渲染原始 HTML；网页带 CSP，只允许内联样式和脚本。

## 和其他文档的关系

`docs/` 下是给开发者看的设计与运维说明，不随镜像发布。对外的用法写在文档站里，设计文档只链过去，例如 [业务系统操作目录](business-system-catalog.md) 的「匿名只读目录」一节与 `/docs/systems-catalog-mcp`、[API 驱动 AI 员工](features/api-tasks.md) 与 `/docs/api-tasks`。
