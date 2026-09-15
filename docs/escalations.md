# 人工求助与附件

人工求助让外部调用方（通常是 AI 员工的技能）向指定员工提问并等待回复：CoreMan 通过企业微信应用或飞书机器人通知员工，收回员工的文字或媒体回复，调用方轮询取得结果。

## 配置

在平台应用中启用企业微信 `notify`、`callback`，填写企业 ID、应用 AgentID、Secret、接收消息 Token 与 EncodingAESKey（企业微信侧配置见 [企业微信接入](wecom.md)）。保存后页面显示专用回调地址 `/api/callbacks/wecom/{平台应用UUID}`；企业微信后台需能够通过公网 HTTPS 访问 PUBLIC_BASE_URL。GET 用于 URL 验证，POST 接收消息。签名时间窗 ±600 秒、AES/填充/企业与 AgentID 核验、1 MB 报文限制、MsgId 持久去重。

兼容地址 `/weixin/app/callback` 和 `/callbacks/wecom/app` 仅在唯一启用回调应用时可用；多个应用必须用专属地址。

## 调用与身份

首选带 `escalations` scope 的签名 API client，路径 `/api/infra/escalations`。旧 `/api/escalation/create`、`/{id}/poll|followup|resolve|cancel` 保留。签名算法见 infrastructure-api.md。

兼容接口的认证：公开的 app_key 不能单独作为 X-API-Key 凭证，须同时携带 **X-App-Key=调用方标识、X-API-Key=调用方 secret**；不支持只把 app_key 放入 X-API-Key。

发起请求支持 `bot_key, question`，以及三选一：`to_user_id`（企微账号）、`targets:[{to_user_id,to_real_name?}]`、`target_user_ids:[内部UUID]`，可附 `request_id` 幂等键与 `platform_app_id`。接收人必须是启用的绑定员工，拒绝广播。`from_user_id` 不能冒充人类：须携带本人有效会话或 bot_token 并匹配平台账号；只带服务凭证时为系统求助，发起人为空。bot_token 的签发和作用域边界不因兼容接口放宽。

创建时保存平台账号快照；响应 `from_user_id/to_user_id` 仍为平台账号，`sender_user_id/recipient_user_id` 为内部 UUID。后续绑定变更不会把原求助投到新账号。求助内容与回复仅允许原 API client 读取和操作。已被求助历史引用的 bot/应用配置不可直接删除（409），可以停用；不会为了删除配置而丢失历史。传入显示名不作为身份或显示名来源。

## 生命周期

每个接收人同时只有一个 pending/replied，其余 FIFO 排队，最多 50 条未结束记录。单请求最多 20 位接收人，第一位有效回复者胜出，其他人求助取消。创建起 1800 秒到期（含排队时间）；300 秒未轮询即放弃。轮询不能复活过期请求。480/1500 秒分别催办，只催本轮仍未回复且通知已经送达的求助。

回复需来自配置的应用及当前绑定员工，且当前轮通知已送达、消息时间不早于送达。`followup` 要求已有本轮回复，最多两轮，问题历史保存；`resolve` 支持 agent/observed/offline，agent 需已有回复。完成/over/已处理/知道了/ok 自动记线下解决。取消、到期、感谢通知和后续激活在同一数据库事务裁决；发送前重查状态，过时 outbox 标记 skipped。

提问或追问通知发不出去时求助不会继续挂起：出站重试耗尽或平台永久拒绝后，调度器（约 30 秒一轮）把该求助置为 cancelled 并激活同一接收人的下一条；激活时接收人账号已变更、通知无法入队的，当场置为 cancelled。创建与轮询响应中 `delivery_failed=true` 表示因此结束，`failure_reason` 为脱敏后的出站错误（错误类别与平台错误码）或 `notification_unavailable`；Agent 主动取消、同组他人胜出的求助不会被标记。

平台纯文本回复没有原问题 ID：极近时间内上一条求助的延迟新消息仍存在归属歧义，不能声称平台提供了线程级关联。已见 MsgId 不会转给后续求助，发送时刻和轮次门槛用于减少串单。

## 媒体与存储

图片/语音/视频/文件回调先记录 pending 并入队，快速应答平台；worker 使用应用凭证向固定企微媒体接口流式下载，100 MB 上限、180 秒任务预算、支持取消与失联恢复。只认 MediaId，不访问回调任意 URL。失败记录为 failed 并提示文字回复；不会永久 pending，也不生成聊天计数或私聊可达凭据。

默认使用 local 存储：`OBJECT_STORAGE_ROOT` 默认 `/data/storage`，API、worker、scheduler 共用 storage 卷；一次性 storage-init 容器只初始化目录归属（UID/GID 10001），不递归改历史文件，应用进程保持非 root；随机 UUID 文件、原子落盘、24 小时签名下载，强制 attachment/octet-stream/nosniff，不内联执行 HTML。持链接者可下载，不要把含签名 URL 的回复公开。scheduler 回收过期文件与超过 25 小时的上传残留/孤儿，每轮有批量上限。

也可以改用 S3 后端，配置、版本绑定与清理规则见 [对象存储](object-storage.md)。跨主机部署须使用经过验证的 S3 或可靠共享存储。

## 上线检查

自动测试覆盖加解密、应用/调用方/账号隔离、重复回调、多人胜出、排队与两轮追问、取消/无人轮询/到期、并发催办、媒体成功/失败/取消/失联、签名链接与清理。接入真实企业应用后，请先完成公网回调 URL 验证，再用一轮文字、媒体、催办与到期流程点检；模拟测试通过不能代替真实凭证与真人回复的验证。

## 飞书与语言

请求可显式指定 platform=feishu；同应用机器人须启用。飞书回复必须引用当前已送达通知，可使用图片或文件；不会占用普通聊天记录。按接收人语言发送求助、追问、催办、附件失败和结束通知；日文支持「対応済み」「完了」「了解」作为已处理回复。真实飞书权限与客户端验收见 [飞书接入](feishu.md)。
