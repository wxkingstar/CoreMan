# 基础设施接口

基础设施接口供外部业务系统与 AI 员工的技能调用：读取组织通讯录、向会话推送消息、通知员工、测试业务系统授权、上报运行时健康等。

新路径以 `/api/infra` 开头；仍有技能在用的少数兼容别名保留，其余旧路径已移除。请求签名使用实际调用路径，不能拿新路径的签名调用旧路径。API 调用方在管理台创建，密钥只显示一次，启用状态与 scope 每次检查。

| Scope | 新路径 | 兼容别名 |
|---|---|---|
| org | GET `/api/infra/org/members`、`tree`、`full` | `/api/robot/organization/members` |
| relay | POST `/api/infra/relay/rate-limits`、`health` | — |
| relay | GET `/api/infra/relay/servers` | `/api/robot/clawrelay-servers` |
| push | POST `/api/infra/push` | — |
| notify | POST `/api/infra/notify/user` | — |
| systems | POST `/api/infra/systems/test-access` | — |

头：`X-App-Key`、`X-Timestamp`（秒，±600 秒）、`X-Signature`。算法是 SHA256(`METHOD + path + params + timestamp + app_key + app_secret`)；path 去掉前导 `/`，query 与 body 合并排序，PHP urlencode 编码标量键值，标量数组展开 `key[0]=value`，嵌套结构跳过。签名兼容旧规则，业务字段仍单独验证。密钥轮换即时使旧签名失效。

推送正文 `{bot_key, chat_id, content, request_id?}`，当前只接受 markdown。通知正文 `{user_login | platform_user_id, content, msgtype?, platform_app_id?, request_id?}`，旧入口可用 `wework_user_id`；只允许单个已绑定的启用用户，不能传 `@all` 或 `|` 广播。当前通知平台为企微，正文上限 2048 UTF-8 字节；机器人推送上限 20480 字节。多个通知应用时必须指定一个应用 ID。

**返回成功表示已入队，不表示平台已送达。** `data.outbox_id` 可在运行状态页查询和重试。提供签名正文里的 request_id 后，同一调用方用同一编号和相同内容重试不会新增出站项；同编号换内容返回 409。不提供编号时每次视作新请求。企微应用侧启用 1800 秒重复消息检查，窗口内同收件人同正文也可能被平台合并；不承诺跨无限时间窗口的恰好一次送达。

系统授权测试正文 `{system_key}`。除了服务签名，还须提供当前真实管理员的已验证会话或有效 CoreMan bot_token；普通会话写操作仍需 CSRF。旧 email_prefix/user_name 只能与当前用户一致。管理台使用 `/api/admin/systems/test-access`。只测试管理员预先登记的系统地址，不跟随重定向，DNS 解析后固定目标地址，保留 TLS 主机校验。同一地址先不带令牌、再带令牌各请求一次：带令牌得到 2xx，或跳转去向与不带令牌时不同且不是登录页（很多后台登录后首页照样跳到默认页或别的子系统），算令牌已被识别；两次跳到同一处或去了登录页算未生效；不带令牌就能打开时提示无法判断。测试令牌存活 60 秒，只回显跳转目标的主机与路径（`redirect`）和两次的状态码，不返回令牌、页面正文、其余响应头与查询参数。

对话令牌使用 ES256，issuer 来自设置，aud/scope 为系统 key，主体为当前已验证且启用的发起者：sub 取其邮箱前缀（小写），业务系统通常按它对应自己的账号；没有邮箱、或另有账号的邮箱前缀相同时用登录名。授权测试的结果会写明所用的令牌用户。未知身份、bootstrap 身份、停用用户不签发。授权与机器人的允许范围每次请求重新读取，续跑也不复用历史提示词中的身份与令牌。公开公钥地址 `/api/.well-known/jwks.json`；轮换后旧公钥保留 24 小时。CoreMan bot_token 逐请求验证，不转换为寿命更长的管理会话。

部署时可用 `BOT_JWT_PRIVATE_KEY`、`BOT_JWT_KID`、`BOT_JWT_ISSUER`（可选 `BOT_JWT_PUBLIC_KEY`，须与私钥配对）让对话令牌和授权测试令牌改用已有签发方的 ES256 密钥签发，已信任该签发方的业务系统不用改动。此时令牌的 kid、iss 取这两项配置，系统范围只放在 scope，不带 aud（部分 JWT 库在验签方未指定 audience 时会拒收带 aud 的令牌）；公开公钥地址同时发布这把公钥，管理台「身份签名密钥」标为「部署配置」，轮换只影响平台自有密钥。外部密钥只用于签发：CoreMan 验证 bot_token 仍只认平台自有密钥，持有同一私钥的其他签发方不能借此调用 CoreMan 管理 API。

运行时实例全部由已注册的 Runtime Daemon 节点提供（每个节点按 AI 类型各一个实例），管理端经节点主动建立的反向通道下发请求，不再支持独立部署的中继实例。节点上报额度与健康时可用该实例的派生 Bearer 令牌代替签名，上报正文必须带 `server_id`，只能修改自己实例；不能用 Bearer 列出全部实例。额度为空只更新心跳，不用零覆盖旧测量；遥测不提升配置 version。实时任务及手动探测仅管理员可用；`today-usage` 按 UTC 当日对话开始时间统计，未上报 token/成本保留 null，并给出上报记录数，不能把缺失当作零花费。节点安装与服务管理见 [Runtime Daemon 说明](../runtime_daemon/README.md)。

自动测试使用隔离 PostgreSQL 与模拟平台/Agent。业务系统的真实登录权限、企业微信通知的可见范围、模型健康与额度探测需在自己的部署环境中验证。
