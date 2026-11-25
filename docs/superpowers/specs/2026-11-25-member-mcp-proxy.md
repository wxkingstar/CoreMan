# 成员本机 MCP：按本人权限过滤与平台代理调用（第四期）

承接 [成员本机接入设计](2026-11-25-coreman-oauth-mcp-cli.md) 的第 4 期。

## 现状

成员带 `mcp` scope 的 OAuth 令牌访问 `/api/systems/mcp` 时，复用匿名目录（`Context.anonymous=True`）：列出全部已启用且配了 OpenAPI 地址的系统，不按权限过滤，没有 `systems_call`，调用方用自己的令牌直连业务系统。

## 目标

- 成员模式按本人在各业务系统的权限码过滤目录（与 AI 员工任务里的过滤同一套规则）。
- 平台代理（`token_delivery = proxy`）的系统，成员都可以经平台代理调用，令牌由平台按成员身份签发、只留在服务端；写操作由成员本人在 CoreMan 页面上确认后才执行。
- 绝不把成员的 CoreMan 令牌转发给业务系统。
- 匿名模式（`SYSTEMS_CATALOG_PUBLIC`）完全不变。

## 设计

### 1. 系统范围

- 系统范围不变（`catalog_systems`：已启用、有系统地址与 OpenAPI 地址）。
- `token_delivery = proxy` 的系统对成员自动开放代理调用，不另设开关；其余系统保持直连（`call_mode = direct`，curl 模板引用调用方本机变量）。
- 能做什么最终由业务系统按成员本人的令牌判定，与成员自己登录业务系统操作等价。

### 2. 按本人权限过滤

- 成员模式不再是匿名上下文：`Context` 带上成员（`Operator`，`task_id` / `bot_id` 为空）和 OAuth 授权 id。
- 声明了权限查询（`x-agent.permissions`）的系统（不论是否代理），用平台按成员签发的短期令牌查一次权限码，按与任务内相同的规则过滤；查询失败时不过滤并标 `permissions_unknown`。`permission_check` 取 `filtered` / `unknown` / `not_declared`，与任务内一致。
- 仍然只读已存的目录，不为成员的请求拉取或复查。
- 成员算不出业务系统主体（没有邮箱、邮箱前缀冲突）时：不过滤（`permissions_unknown`），代理调用返回 `subject_unavailable`。

### 3. 代理调用

本次有代理系统时 `tools/list` 列出 `systems_call` 与 `systems_call_result`。对直连系统返回 `not_proxied`（附直连说明）。

- 校验、构造请求、同源与重定向限制、超时、响应截断与令牌脱敏完全复用任务内的 `proxy.prepare` / `send`。
- `read` 直接执行；`write`、`destructive` 走第 4 节的网页确认；`financial` 一律不代理。
- 不支持：上传文件（`multipart`）和 `batch`（返回 `unsupported_for_member`）；大结果不转存，按原来的方式截断并提示缩小范围（转存的下载地址要任务能力令牌）。

### 4. 写操作：公共机器人私聊发飞书确认卡 + 自动等结果

「公共机器人」：系统设置里现有的「任务通知员工」（`work_task_notifier_bot_id`，任务平台已用它给人发带按钮的任务卡片）改名为「公共机器人」，专门做「不知道该由哪个机器人出面」的事：任务卡片、成员本机写操作确认卡，以后同类场景都用它。设置键不变，只改管理台名称与说明。它是一位普通的飞书 AI 员工，卡片回调走现有网关，不需要新的飞书配置；成员要在它的应用可用范围内。

1. `systems_call` 调用写操作时所有检查照常执行，但不调业务系统：把请求（含暂存时生成的幂等键）加密存进 `interaction_states`（新 kind `member_write`，`bot_id` 为公共机器人，请求按确认单 id 与成员做 AAD），返回 `{"status": "pending_confirmation", "confirmation_id", "expires_in", "message"}`。可选 `note` 与 AI 员工的确认卡相同（对象、修改前后值、影响和恢复方式，用业务说法）。
2. 同时由公共机器人私聊成员一张确认卡，样式与 AI 员工的写操作确认卡一致（标题是操作名称、副标题系统名、`note` 里的对象和改动、默认收起的「查看详情（实际发出的请求）」；`destructive` 标红并写明删除后无法恢复），另加一行「来自你本机的 <客户端自报名称>」。按钮「确认执行」「取消」。
3. 点击只认成员本人、在与公共机器人的私聊里、点的就是这张卡（与业务系统调 AI 员工时私聊卡片的核验相同）。点确认：先把状态记为已提交（并发、重复点击只执行一次），再复查（成员仍启用、OAuth 授权未吊销、系统仍启用且代理、操作定义与暂存时一致、仍可见），通过后以成员身份按暂存的那一份原样执行一次，审计结局 `confirmed_ok` / `confirmed_http_error` / `confirmed_unreachable`；卡片更新为结果（「已完成」「没有成功」「结果未知，可能已经生效」）。
4. Agent 调新工具 `systems_call_result(confirmation_id)` 拿结果：服务端最多等 20 秒（期间成员点了就立即返回），返回 `pending` / `ok` / `failed` / `unknown` / `refused` / `cancelled` / `expired` 与截断后的响应。`systems_call` 的返回与工具说明要求 Agent 告诉用户「已发到飞书，请在卡片上确认」，再调这个工具等待，`pending` 时继续调。只有同一位成员（同一 OAuth 授权）能查。
5. 卡片 60 分钟有效（与其他确认卡相同），过期变为「已过期」、不执行、不能重新发起；暂存的请求在结束 24 小时后清理。每位成员同时待确认最多 20 张，超出返回 `too_many_pending`。
6. 发不出卡（没设公共机器人、它不是启用的飞书员工、成员没有飞书身份）时不暂存，返回 `confirmation_unavailable`，说明请管理员设置公共机器人，或到业务系统里操作。确认、取消只能在卡片上做，没有 API：成员本机 Agent 手里的令牌替不了本人。

### 5. 令牌

- 用系统现有的业务令牌签发方，按成员的主体（`token_subject`，邮箱前缀）签发，用途沿用 `proxy`（查权限也用这枚），签发记录带 `oauth_grant_id`。
- 有效期 15 分钟；在各 API 进程内存里按（OAuth 授权 id、系统）缓存，到期前 30 秒换新。每次请求都复核 OAuth 令牌，授权吊销、成员停用后立即认不出人，缓存随之不再被用到。确认执行时现签一枚。
- 签发记录 `business_token_issues` 新增 `oauth_grant_id` 列，`user_id` 为成员，`task_id` / `bot_id` 为空。

### 6. 预算、限流与审计

- 限流：沿用每人每分钟 120 次工具调用（各进程分别计数）。
- 代理预算：每位成员对每个系统每小时最多 `systems.proxy_calls_per_turn` 次，按 `system_calls` 里本人最近一小时的记录计数（跨进程准确），超出返回 `catalog_budget_exhausted`。
- 审计：`system_calls` 新增 `oauth_grant_id` 列和 `(user_id, called_at)` 索引；成员调用的 `task_id`、`bot_id` 为空，被拒的调用同样记一行，暂存记 `proposed`。

迁移 `0133`：上述两列与索引，`interaction_states.kind` 增加 `member_write`。

## 不做

- 成员的批量写、上传文件、大结果转存。
- 企微部署的成员写操作（确认卡只做飞书）。
- MCP elicitation（端点要改流式、多进程要对答，客户端支持不一）。
