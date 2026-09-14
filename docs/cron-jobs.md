# 定时任务与通知

M4 已合并 main 并完成本机升级（迁移 0010）。使用管理台「定时任务」配置，邮件通道在「设置 → 邮件通知」配置。

## 身份与权限

- 自动执行使用任务创建者；点击「立即运行」使用点击者本人。执行前重查启用状态、平台绑定、bot 管理权限和业务系统授权。
- 只有任务创建者可编辑内容和删除。其他 bot 管理员可以停用或以自己身份运行；平台管理员身份不旁路 bot 权限。
- 排队后使用该次配置快照。立即运行已请求但尚未入队时不允许修改内容。
- 「停用」阻止未来触发；「取消本次执行」取消排队/在途任务，不关闭未来计划。
- 接收人为当前启用用户；私聊必须有真实 single 会话记录，群聊与 cron 不计作私聊可达证据。实际发送前重查个人接收身份。

## 调度与恢复

五字段 cron（分、时、日、月、周），按任务时区解释。夏令时不存在的时间跳过，重复墙钟时间只执行第一次。错过不超过 300 秒仍执行，超过则记 skipped。到期自动停用。

Scheduler 每 10 秒尝试；job 行锁 + task dedupe_key 防止多实例重复入队。同一任务不重叠执行。定时记录、任务和下一次运行时间在同一事务提交。运行完成时终态、cron_runs、chat_logs 与结果 outbox 同事务提交。

进程中断后，reaper 结掉失联任务，cron 看护补齐执行历史和失败通知，不自动重复调用模型。每次执行使用独立 Relay 会话，不覆盖用户聊天会话。

## 执行前检查

空脚本代表直接执行。脚本需定义 `should_trigger(ctx)` 并返回：

```python
def should_trigger(ctx):
    return {"trigger": True, "prompt_appendix": "", "reason": "ready"}
```

`ctx` 包含 now、scheduled_at、bot_id、user_id、job_name；不含环境变量、访问令牌或内部对象。

当前采用受限 AST 解释器，不调用 Python exec/eval。支持赋值、if、for、JSON 数据、基本算术/比较、有限容器方法和明确允许的函数。支持 json.loads/dumps、有限 math 函数、time.time；datetime.datetime.now/utcnow 返回 UTC ISO 字符串。最多 20,000 步、10,000 个容器项，值总量 64 KiB，脚本 32 KiB。

**兼容边界**：旧脚本的 requests、数据库客户端、正则、文件操作、自定义函数、导入别名、反射等未开放。不支持的脚本明确 failed_precheck，不悄悄跳过检查或启动模型。迁移旧任务时需逐条适配、试跑；本轮未读取生产任务或迁入生产任务。

## 投递

- 可达私聊和群聊：经 bot 网关 outbox；私聊不可达时使用唯一启用的通知应用，并提示先建立私聊。通知应用不唯一时记录错误，不猜选应用。
- 群 Webhook：使用 bot 配置的企微官方 webhook，markdown_v2 每片最多 4096 UTF-8 字节；密钥 URL 在出站快照中加密。
- 邮件：SMTP 仅 TLS/STARTTLS，证书校验启用、DNS 解析结果固定到连接；密码加密保存，更换目标时需重输密码。HTML 转义内容。配置保存不发送邮件。
- 执行成功与投递成功独立显示。投递采用至少一次语义、退避重试，失败可由运行状态页重投。SMTP Message-ID 重用不等于收件端保证去重；平台重复检查也有时间窗口。

管理 API：`/api/admin/cron-jobs`（GET/POST）、`/{id}`（PUT/DELETE）、`/{id}/run`、`/{id}/disable`、`/{id}/cancel-run`、`/{id}/runs`、`/precheck/test`。修改需 If-Match。SMTP：`/api/admin/notification-settings`（GET/PUT），首次版本为 0。

## 验证边界

本地使用一次性 PostgreSQL、FakeRelay、模拟 HTTP/SMTP 测试；不读取真实 .env 内容，不发送真实平台消息。真实 SMTP、Webhook、企微通知应用、平台回调与旧任务迁入仍需目标环境条件。
