# 术语表（Glossary）

本表解释管理台、文档、日志与代码中反复出现的概念。括号内为英文术语或代码中的名字。整体结构见 [架构说明](architecture.md#service-topology)。

## 平台与接入

**AI 员工（bot）**
：在企业微信或飞书中与员工对话的 AI 协作成员。一条 `bots` 记录包含平台凭证、提示词、绑定的运行时实例与模型、工作目录、环境变量、协作者与使用白名单。管理台称「AI 员工」，代码与 API 中称 bot。

**AI 员工标识（bot_key）**
：AI 员工的全局唯一标识，小写字母、数字、下划线或连字符，2–50 位，创建后不可修改。外部接口（如机器人推送、人工求助）用它指定 AI 员工，请求级环境变量 `COREMAN_BOT_KEY` 也取该值。

**平台凭证（credentials）**
：AI 员工连接聊天平台所需的凭证。企业微信为智能机器人的 `bot_id` 与 `secret`；飞书为应用的 `app_id` 与 `app_secret`（另可填 `encrypt_key`、`verification_token`）。整体加密保存，接口只返回脱敏值。

**平台应用（platform app）**
：企业微信自建应用或飞书企业自建应用在 CoreMan 中的登记，按能力启用：通讯录同步（`contact_sync`）、登录（`login`）、通知（`notify`）、回调（`callback`）。与 AI 员工的聊天凭证相互独立，见 [企业微信接入](wecom.md) 与 [飞书接入](feishu.md)。

**通知应用（notification app）**
：具备「通知」能力的平台应用。用于员工与 AI 员工尚未建立私聊时的定时任务结果、人工求助通知与运行告警。

**引导管理员（bootstrap admin）**
：`.env` 中 `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD` 定义的初始账号，用于首次配置。平台登录验证成功后可在设置中关闭该登录方式。引导身份不会被签发业务系统令牌，也不能作为通知接收人。

## 角色

| 角色 | 代码 | 说明 |
|---|---|---|
| 平台管理员 | `platform_admin` | 管理平台应用、平台设置、API 调用方与身份签名密钥等全局配置，并拥有 AI 委员会的管理权限 |
| AI 委员会 | `ai_committee` | 负责 AI 治理的管理角色：审批内部技能、维护技能目录、业务系统、模型目录与价格，查看全量统计与审计 |
| 团队负责人 | `team_lead` | 查看本团队范围内的 AI 员工与对话 |
| 成员 | `member` | 普通员工；加入团队后可创建并管理自己的 AI 员工 |

全局角色不自动获得某个 AI 员工的敏感内容：提示词、记忆正文、技能安装等仍按「创建者 / 协作者」判定，各篇文档会注明例外。

## 执行端

**运行时节点（runtime node / Runtime Daemon）**
：安装在某个系统用户环境中的常驻程序，对应 `runtime_nodes` 表。它主动连接 CoreMan，发现本机已安装并登录的 Claude Code 与 Codex CLI，并用本地 Go 驱动执行请求。节点的系统用户权限就是 AI 的执行边界。见 [Runtime Daemon](../runtime_daemon/README.md)。

**运行时实例（runtime instance，表名 `relay_servers`）**
：节点按 AI 类型（`claude` / `codex`）各登记一个实例，AI 员工绑定的是实例，而不是节点。管理台称「运行时」。表名、模型类名 `RelayServer`、API 路径中的 `relay` 以及界面中的「切换运行时」（`relay_switch`）都是历史命名：早期版本支持独立部署的 HTTP 中继服务作为实例，现在所有实例都必须属于已注册的节点，请求一律经节点的反向通道下发；为兼容数据库与接口，名字保留未改。

**项目主目录（workspace root）**
：安装节点时填写的绝对路径。AI 员工的工作目录必须位于其下（且不等于它本身），同一节点上一个目录只归一个 AI 员工。节点无绑定或迁入员工时，可在管理台编辑并等待 Runtime 确认生效；不会自动迁移旧目录文件。

**反向通道（reverse channel）**
：管理端到节点的请求通道。worker 把调用写入 `runtime_calls`，节点每 0.5 秒通过 `POST /api/runtime/poll` 领取，并把加密的流式片段回传写入 `runtime_chunks`。节点无需开放入站端口。

**Go 驱动（driver）**
：节点内的 `runtime-claude` / `runtime-codex` 进程（源码在 `runtime_daemon/drivers/cmd/relay-*`），通过 Unix socket 提供 OpenAI 兼容接口，把请求转换为 CLI 调用并翻译流式事件。基于开源项目 clawrelay-api 修改，见 [第三方声明](../THIRD_PARTY_NOTICES.md)。

**模型目录（model catalog）**
：`model_catalog` 表，按 provider 维护可选模型、默认模型与退役状态。平台默认模型由目录派生，不单独保存。

## 服务进程

**网关（gateway）**
：`gateway-wecom` 与 `gateway-feishu`，持有与聊天平台的长连接：把入站消息写入 `inbound_events` 并创建任务，把 `task_streams` 的进展推成流式回复，并消费出站箱。Compose 中各有 a/b 两侧，同一时刻只有一侧活跃，另一侧用于滚动升级接管。

**worker**
：从 `tasks` 表认领任务并执行：识别发言者、构造提示词与请求级环境变量、经反向通道调用运行时、写入流式进展、分类结果并记录对话日志。

**scheduler**
：单主看护进程，多实例靠 PostgreSQL 咨询锁选主。负责触发定时任务、回收失联任务与死实例的租约、清理过期数据，并投递通知类出站条目。

**api**
：FastAPI 管理接口，同时提供管理台静态文件、平台登录与回调、基础设施 API 和节点协议接口。前面由 Caddy 反向代理并负责 HTTPS。

**管理台（admin console）**
：`web/` 下的 Vue 3 前端。

## 数据库总线

CoreMan 不使用额外的消息中间件，进程之间只通过 PostgreSQL 表加 `LISTEN/NOTIFY` 协作，并以 1 秒级轮询兜底。

**进程实例（process instance）**
：`process_instances` 表中每个网关、worker 等进程的活体登记，含心跳、在跑任务数与排空标记。「运行状态」页展示这些记录。

**租约（lease）**
：`bot_leases` 表保证一个 AI 员工的聊天连接同一时刻只被一个网关实例持有。持有者定期续约，心跳超过 30 秒未更新即可被其他实例接管；`generation` 每次接管递增，旧持有者的迟到写入会被拒绝。表中还记录连接状态：未连接、连接中、已订阅、已被顶替、认证失败。

**任务（task）**
：`tasks` 表中的一项工作，例如对话、卡片动作、定时执行、切换运行时、技能安装。worker 用 `SKIP LOCKED` 认领，心跳超时的任务由 scheduler 收尾。

**fast 车道（fast lane）**
：任务分 `normal` 与 `fast` 两个车道。设置 `max_concurrent_tasks` 限制全平台同时运行的任务数，其中 `fast_lane_slots` 个名额只留给 fast 车道，避免短任务被长对话堵住。

**task_streams**
：worker 写入的单个任务实时进展（思考过程、待发正文、最终正文、待发卡片等），每次写入 `version` 加一；网关推送高于 `pushed_version` 的内容并回写，从而在网关切换后也能续推。

**出站箱（outbox）**
：`outbox` 表中的持久化待发消息，包括主动发送、卡片更新、欢迎语、流结束补发与应用通知。以 `dedupe_key` 保证至少一次投递且平台侧幂等；失败条目保留在「运行状态」页，可人工重投。接口返回「已入队」不等于平台已送达。

**排空（drain）**
：让一个进程或节点停止接收新工作、把在途工作做完或交接后再退出。网关排空时逐个释放 AI 员工的租约，由另一侧接管连接；worker 排空时等待在途任务结束；节点排空时停止接收新请求。滚动升级与回退都以排空为前提，见 [运行维护](operations.md)。

## 协作能力

**人工求助（escalation）**
：外部调用方（通常是 AI 员工的技能）通过基础设施 API 向指定员工提问并等待人工回复。通知经通知应用或飞书机器人发出，回复经平台应用回调收回，含排队、催办、追问与到期规则，见 [人工求助](escalations.md)。

**协作伙伴（collaboration partner）**
：飞书 AI 员工在任务中可以求助的对象，由此 AI 员工的管理者配置：其他飞书 AI 员工（只在双方同在的群里）或人类同事（群里 @，否则私聊；定时任务中私聊）。求助登记后本轮结束，收到真实平台答复才续跑，见 [飞书 AI 员工协作](features/feishu-bot-collaboration.md) 与 [向同事求助](features/feishu-human-collaboration.md)。

**定时任务（cron job）**
：按 cron 表达式以创建者身份运行 AI 员工，可带执行前检查脚本与结果通知，见 [定时任务](cron-jobs.md)。

**公告（announcement）**
：按全局、运行时实例或 AI 员工范围生效的固定回复。命中时直接回复公告内容并拦截本轮对话，常用于维护窗口。

**技能（skill）**
：从技能来源（Git 仓库中的插件清单）同步到目录、再安装到 AI 员工工作目录的能力包。`internal` 技能需 AI 委员会审批，见 [技能管理](skills-management.md)。

**记忆（memory）**
：AI CLI 在工作目录中维护的记忆文件，由管理台与节点双向同步，见 [记忆管理](memories.md)。

## 业务系统授权

**业务系统（business system）**
：`systems` 表中登记的外部内部系统，以 `key`（小写字母开头）标识，可设为默认对全部 AI 员工开放，或限定允许申请的 AI 员工。

**系统授权（system grant）**
：`bot_system_grants` 表记录某个 AI 员工可以访问哪些业务系统。收紧系统的白名单会立即回收不再符合条件的授权。

**BOT_TOKEN**
：每轮对话按**当前已验证的发言者**签发的 ES256 JWT，受众为业务系统 key，以环境变量 `BOT_TOKEN_<系统 KEY 大写>` 下发给 AI CLI，技能用它以发言者本人身份调用该业务系统。发言者未知、为引导管理员、已停用，或算不出唯一的 sub（无邮箱 / 邮箱前缀与他人重复）时不签发；AI 员工自身配置或环境预设中的同名变量会被丢弃。令牌值在出站方向被拦截：模型若在回复中复述它，投递与 `chat_logs` 中都只会留下占位符。业务系统通过公开的 `/api/.well-known/jwks.json` 校验签名，见 [基础设施 API](infrastructure-api.md)。

**请求级环境变量（request-level env）**
：每轮对话按发言者重新计算的变量，如 `COREMAN_BOT_KEY`、`COREMAN_PLATFORM`、`COREMAN_CHAT_ID`、`COREMAN_PLATFORM_USER_ID`、`COREMAN_USER_LOGIN`。它们覆盖 AI 员工配置中的同名变量，身份未知时不下发身份类变量。

**API 调用方（API client）**
：外部系统调用基础设施 API 使用的标识与密钥，按接口组（scope）授权，请求需签名。
