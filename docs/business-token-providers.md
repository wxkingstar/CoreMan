# 业务系统令牌提供方

CoreMan 为当前用户申请业务系统令牌。默认 `builtin` 沿用平台 ES256 签名（也支持已有的 `BOT_JWT_*` 部署密钥）；业务系统可以单独选择一个 HTTP 提供方，由外部身份服务签发令牌。两种方式可以同时使用，不要求部署任何特定品牌的 SSO。

## 配置与上线

先执行数据库迁移 `0057`，升级全部 API 和 worker，再启用外部提供方。旧版本 worker 不认识提供方字段，会继续自签，不能在混合版本阶段切换系统。旧系统迁移后默认 `builtin`，不改变密钥、声明格式或 Cookie 用法。业务令牌时长改为任务超时，不再额外增加 300 秒。

部署环境变量 `BUSINESS_TOKEN_PROVIDERS` 是 JSON 对象。所有 API、worker 配置一致；标准 Compose 会从环境文件传入。使用自己的密钥管理渠道保存真实凭据，更新配置后重启相应进程。示例：

```dotenv
BUSINESS_TOKEN_PROVIDERS='{"company":{"token_url":"https://identity.example.com/oauth/agent-token","client_id":"coreman-agent","client_secret":"REPLACE_WITH_DEPLOYMENT_SECRET","max_token_ttl_seconds":28800}}'
```

提供方名称只允许小写字母开头的字母、数字、下划线或连字符，最长 50 字符；`builtin` 为保留名称。`token_url` 必须为 HTTPS，不带用户信息、查询参数或片段；服务端验证 TLS，不跟随重定向，也不读取环境代理配置。

在管理台「业务系统」编辑对应系统：

- **令牌签发方**：选择部署中配置的名称；管理接口只暴露名称和时长上限，不提供签发地址或客户端凭据。
- **令牌目标系统**：填写外部服务识别的 audience；留空使用系统 key。平台保留值 `coreman` 不允许使用。
- **授权测试 API**：该系统中一个需要登录才能访问、只读的 API，例如 `https://erp.example.com/api/me`，必须与系统地址同源。外部令牌授权测试必填。

系统仍须启用，并授权给机器人、满足机器人的允许范围。若部署移除了某个提供方，旧系统保留其选择并停止获得令牌；管理员可以修复配置，平台不会自动改用自签令牌。兼容旧客户端的系统更新请求：省略新增字段时保留其现有值。

## 签发协议与身份

HTTP 适配器发送 `application/x-www-form-urlencoded` POST，通过 OAuth 风格 HTTP Basic 验证 CoreMan 服务身份（client_id 和 client_secret 分别进行表单编码后组成 Basic）。默认字段：

```text
username=alice&audience=erp&expires_in=3600
```

`username` 来自平台已验证且启用的当前用户，值与 `COREMAN_USER_SUBJECT` 相同：唯一的小写邮箱前缀。飞书、企业微信共用这条逻辑；缺少邮箱或邮箱前缀冲突时不签发。聊天使用当前发言者，定时任务使用已验证的任务发起者，体检与授权测试使用当前真实操作者。模型参数、机器人静态环境、历史对话不能指定签发身份。

提供方可以配置 `subject_field`、`audience_field`、`ttl_field` 改变字段名称，默认分别为 `username`、`audience`、`expires_in`。它们必须互不相同，不能覆盖客户端凭据字段。这是可信服务声明用户身份的委托签发协议，不是完整的 OAuth Token Exchange / RFC 8693 实现；不支持的外部协议需要另一个适配器。

签发服务必须验证服务凭据，再检查用户状态、目标系统和用户权限，并限制 CoreMan 可代办的系统与权限。业务系统必须直接支持签发方的 Bearer Token，验证签名、签发方、受众、有效期及业务权限。仅完成浏览器 SSO 登录接入，并不代表已经支持 Agent API 请求。

成功响应为 HTTP 200；`access_token` 必须为 8–16384 字符的 ASCII 非空白令牌（短于现有输出隐藏规则的阈值会被拒绝），`token_type` 为 Bearer，响应正文不超过 64 KiB，整个签发请求最多等待 10 秒：

```json
{"access_token":"<issued-token>","token_type":"Bearer","expires_in":3600}
```

CoreMan 接受 JWT 或不透明 Bearer 令牌，完整转交签发结果，不重新签名或删改角色、权限、代理身份等声明。错误响应正文不会进入提示词或 API 响应；缺失配置、拒签、超时或无效响应只让对应系统本轮无令牌，不影响其他系统。

## 时长与运行时使用

普通聊天及定时任务的申请时长为 `min(机器人任务超时秒数, 提供方 max_token_ttl_seconds)`。提供方上限可选，但设置时必须为正整数；不设置就申请任务超时对应的时长。运行环境体检按其 300 秒任务预算申请，管理台授权测试最多申请 60 秒，二者仍受提供方上限约束。CoreMan 没有固定的 8 小时上限：服务只允许 8 小时时，配置 `28800`。服务自身的限制仍需独立执行。

实际响应 `expires_in` 必须为正整数且不超过申请时长。提供方可返回更短时长；配置的上限不是已获批的有效期。CoreMan 以请求开始时间保守计算到期时间，拒绝已过期响应。

仍通过 `BOT_TOKEN_<系统 KEY 大写>` 向本轮 CLI 注入令牌；`COREMAN_SYSTEMS`（兼容 `BOT_SYSTEMS_CONFIG`）包含 `env_var`、`audience`、`auth_mode`、`expires_in` 和 `expires_at`。`expires_at` 是 UTC Unix 秒。外部提供方的 `auth_mode` 是 `bearer`：

```text
Authorization: Bearer <BOT_TOKEN_ERP 的值>
```

无需浏览器 Cookie 或登录跳转。`builtin` 保留 `auth_mode=cookie` 和 `cookie_name=bot_token`，现有技能可以继续使用 Cookie。

服务客户端密钥只留在 CoreMan API/worker，不注入 CLI；Agent 得到的是当前用户、当前目标系统的访问令牌。令牌仍是运行时可读取的凭据，现有输出过滤会隐藏回复和聊天记录中的原值，不能将它视为隔离模型读取凭据的代理服务。技能只应把令牌发往对应的受信业务系统，不打印、不落盘、不复用历史值。

正在运行的 CLI 不自动更新令牌。任务超时长于提供方上限时，令牌会先到期，Agent 应停止相关调用并让用户发起新一轮；新的可信轮次会重新核验身份与授权并申请令牌。续期代理或后台换新不在本功能范围内。

## 授权测试

外部提供方按相同签发流程，以当前管理员身份获取短时令牌。平台对登记的测试 API 先不带令牌请求，再只带 Bearer 请求，清除响应设置的 Cookie，不跟随跳转。只有「无令牌返回 401/403、带令牌返回 2xx」才判为通过；公开首页或登录跳转不能证明 Bearer 鉴权有效。返回中不包含令牌、响应正文或查询参数。

这验证当前操作者和该 API 的访问，不代表所有员工或所有权限接口都已验收。目标服务仍需覆盖错误签名、受众、过期令牌和权限不足的拒绝测试。
