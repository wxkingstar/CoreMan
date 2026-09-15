# 基础设施接口（M3b）

新路径以 `/api/infra` 开头；旧别名保留。请求签名使用实际调用路径，不能拿新路径的签名调用旧路径。API 调用方在管理台创建，密钥只显示一次，启用状态与 scope 每次检查。

| Scope | 新路径 | 旧别名 |
|---|---|---|
| org | GET `/api/infra/org/members`、`tree`、`full` | `/api/robot/organization/members`、`tree`；`/api/organization/full` |
| relay | POST `/api/infra/relay/rate-limits`、`health` | — |
| relay | GET `/api/infra/relay/servers` | `/api/robot/clawrelay-servers` |
| push | POST `/api/infra/push` | `/api/push` |
| notify | POST `/api/infra/notify/user` | `/api/robot/wework-notify` |
| systems | POST `/api/infra/systems/test-access` | `/api/test/bot-token-access` |

头：`X-App-Key`、`X-Timestamp`（秒，±600 秒）、`X-Signature`。算法是 SHA256(`METHOD + path + params + timestamp + app_key + app_secret`)；path 去掉前导 `/`，query 与 body 合并排序，PHP urlencode 编码标量键值，标量数组展开 `key[0]=value`，嵌套结构跳过。签名兼容旧规则，业务字段仍单独验证。密钥轮换即时使旧签名失效。

推送正文 `{bot_key, chat_id, content, request_id?}`，当前只接受 markdown。通知正文 `{user_login | platform_user_id, content, msgtype?, platform_app_id?, request_id?}`，旧入口可用 `wework_user_id`；只允许单个已绑定的启用用户，不能传 `@all` 或 `|` 广播。当前通知平台为企微，正文上限 2048 UTF-8 字节；机器人推送上限 20480 字节。多个通知应用时必须指定一个应用 ID。

**返回成功表示已入队，不表示平台已送达。** `data.outbox_id` 可在运行状态页查询和重试。提供签名正文里的 request_id 后，同一调用方用同一编号和相同内容重试不会新增出站项；同编号换内容返回 409。不提供编号时每次视作新请求。企微应用侧启用 1800 秒重复消息检查，窗口内同收件人同正文也可能被平台合并；不承诺跨无限时间窗口的恰好一次送达。

系统授权测试正文 `{system_key}`。除了服务签名，还须提供当前真实管理员的已验证会话或有效 CoreMan bot_token；普通会话写操作仍需 CSRF。旧 email_prefix/user_name 只能与当前用户一致。管理台使用 `/api/admin/systems/test-access`。只测试管理员预先登记的系统地址，不跟随重定向，DNS 解析后固定目标地址，保留 TLS 主机校验。测试令牌存活 60 秒，不返回令牌、页面正文或响应头。

对话令牌使用 ES256，issuer 来自设置，aud/scope 为系统 key，主体为当前已验证且启用的发起者。未知身份、bootstrap 身份、停用用户不签发。授权与机器人的允许范围每次请求重新读取，续跑也不复用历史提示词中的身份与令牌。公开公钥地址 `/api/.well-known/jwks.json`；轮换后旧公钥保留 24 小时。CoreMan bot_token 逐请求验证，不转换为寿命更长的管理会话。

运行时实例全部由已注册的 Runtime Daemon 节点提供（每个节点按 AI 类型各一个实例），管理端经节点主动建立的反向通道下发请求，不再支持独立部署的中继实例。节点上报额度与健康时可用该实例的派生 Bearer 令牌代替签名，上报正文必须带 `server_id`，只能修改自己实例；不能用 Bearer 列出全部实例。额度为空只更新心跳，不用零覆盖旧测量；遥测不提升配置 version。实时任务及手动探测仅管理员可用；`today-usage` 按 UTC 当日对话开始时间统计，未上报 token/成本保留 null，并给出上报记录数，不能把缺失当作零花费。节点安装与服务管理见 [Runtime Daemon 说明](../runtime_daemon/README.md)。

测试均使用隔离 PostgreSQL 和假平台/Agent。真实系统登录权限、企微通知可见范围、模型健康与额度探测需在相应接入环境验收。
