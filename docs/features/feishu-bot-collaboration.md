# 飞书机器人协作（限范围试运行）

## 行为

人类 @ A → A 使用任务内求助接口 → A 的执行释放 → A 在群内发送文字 @ B →
飞书真实事件到达 B → B 以原始人类授权执行 → B 引用求助消息并明确 @ A →
飞书真实事件到达 A → A 恢复原始运行时会话，回复原始人类消息。

帮助阶段的任务可以结束，整体协作状态单独保存在 `bot_collaborations`。
成功发送消息不等于收到反馈；没有真实平台入站事件不会排入下一步。

## 开启范围

迁移 0023 创建 `bot_collaboration_routes`，默认关闭。每条记录指定：

- source_bot_id / target_bot_id：平台现有飞书机器人。
- chat_id / tenant_key：同一个授权群与租户。
- source_open_id / target_open_id：各应用通过 bot/v3/info 获得的自身 open_id，用于发送 @。
- source_union_id / target_union_id：从飞书已验签的真实机器人事件核验，不能把应用间不同的 open_id 当成通用身份。
- enabled：显式开启；timeout_seconds：默认 300，实际限定在 30 到 1800 秒。

试运行阶段由维护者配置，尚无管理台编辑入口。仅开启指定 A/B/QA 群，不影响其他机器人。
停用任一机器人或关闭路由，会停止未完成协作。配置身份时必须核验真实事件，不能猜测。

## 任务接口

`POST /api/runtime/bot-help`

- Authorization: Bearer（本轮 `COREMAN_BOT_HELP_TOKEN`）
- 地址来自 `COREMAN_BOT_HELP_URL`。
- JSON: `{"target_bot_key":"预配置伙伴","question":"具体问题及必要上下文"}`。
- 凭证由服务端加密绑定任务与原始人类，30 分钟过期；原任务结束后立即失效。
- 同一任务只允许一个求助；相同参数可重试，不同参数返回 409。
- B 和恢复后的 A 都不签发新凭证，也不能递归请求。
- 收到成功结果后 A 必须立即结束本轮，不能轮询或猜测结果。

机器人提示词会自动列出它有权限使用的伙伴及以上调用方式，不需要模型接触飞书应用密钥。

## 校验和运维

- 人类身份源于原始真实人类事件。每次派发重新校验在职状态及双方用户白名单；业务令牌由当前机器人现有授权机制重新签发，没有创建者身份回退。
- 机器人消息独立保存在 inbound_events，永远不走普通人类入口。只有发件箱返回的消息编号、群、租户、sender union_id、明确 @ 与回复 parent_id 全部匹配才派发。
- 调度器会处理入站早于发送确认提交的情况；行锁与唯一任务键防止重复执行。
- 开启协作的群中，每个人类请求使用独立会话，避免并发请求串话；A 恢复时沿用该请求的运行时会话。
- `stop` / `reset` 取消该人类在本群发起的未完成协作。超时、权限变化和执行失败会通知人类，不把缺少反馈的结果标为完成。
- B 使用文字反馈，保留明确 @。普通人类最终回答继续用现有卡片。
- 初始求助和反馈等待默认由 15 秒调度周期推进，因此会有数秒到十几秒的交接延迟。

排查顺序：协作记录状态 → request/response outbox 编号和 `_feishu_message_id` →
对应接收机器人的 inbound_events → helper/resume task → 最终 task_stream 与 FeishuDelivery。

## 官方参考（通过 SpecFusion 查询）

- [接收消息事件](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)：机器人 @ 事件权限 `im:message.group_at_msg.include_bot:readonly`。
- [发送消息内容](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json)：text 的 `<at user_id="...">...</at>`。
- [回复消息](https://open.feishu.cn/document/server-docs/im-v1/message/reply)：引用并不等于 @；使用稳定 uuid 做平台重试去重。
