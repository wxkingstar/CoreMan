# 管理命令

管理后台里最常用的操作也可以在服务器上用命令完成：AI 员工、技能管理、技能审批、业务系统、团队与用户。其余管理后台操作可以用 `api` 命令直接调接口。

## 运行方式

命令在 API 镜像里运行，不需要额外配置：

```bash
# 单机 Compose：经部署脚本在一次性 migrate 容器里执行
./deploy/coreman bots list --as alice

# Kubernetes 或已在运行的容器：镜像里自带 coreman 入口
kubectl exec deploy/<api 部署名> -- /app/.venv/bin/coreman bots list --as alice
```

`--as` 指定以哪位成员的身份操作，填登录名；也可以设置环境变量 `COREMAN_CLI_USER` 作为默认值。部署脚本会把宿主机上的 `COREMAN_CLI_USER` 带进容器，写进 `.env` 也可以。

命令不走网络，也不另写一套业务逻辑：它在进程内启动同一个 API 应用，为这位成员建一个短期管理台会话（`auth_method = cli`），请求交给管理后台的同一套接口处理，结束时吊销会话。所以：

- 权限与管理后台一致。例如改 AI 员工配置只有创建者和协作者能做，平台管理员也不行；要改别人的员工，先用 `bots member-add` 把自己加为协作者，或以创建者身份操作。
- 每次修改都记在审计日志里，操作人是 `--as` 的成员，IP 为 `127.0.0.1`。
- 校验、乐观锁与变更通知都照常生效：运行中的 worker 和网关会像在后台点按钮一样收到变更。

能在 API 容器里执行命令的人本来就能读数据库和密钥，`--as` 可以填任何在职成员，不构成额外的权限。

## 通用约定

- AI 员工可以写标识（`bot_key`）、名称或 id；成员可以写登录名、邮箱、姓名或 id；团队可以写 slug、中文名或 id；技能写名称或 id；业务系统写标识；审批写 id，写开头几位就够。名称有重名时命令会列出候选，请改用标识。
- 列表默认打印对齐的表格，详情打印「字段 值」；加 `--json` 输出接口返回的原始数据，便于配合 `jq`。
- `set` 类命令只改命令行上给出的字段，其余保持不变。
- 删除类操作和「对所有人放开」的操作必须加 `--yes`。
- 成功退出码为 0；接口报错时把错误信息打印到标准错误，退出码为 1。
- 敏感值（环境变量、环境组变量）只显示脱敏值；修改其中一项时，其余项靠脱敏值保留原值，明文不会经过终端。

## AI 员工

```bash
coreman bots list [--keyword 词] [--platform wecom|feishu] [--status enabled|disabled] [--team 团队] [--mine]
coreman bots show sales_bot
coreman bots prompt sales_bot > prompt.md          # 导出系统提示词
coreman bots set sales_bot --prompt-file prompt.md # 改完写回
coreman bots set sales_bot --model 模型 --effort high --verbosity 2 --team sales --rich-cards on
coreman bots enable|disable sales_bot
coreman bots delete sales_bot --yes
```

`set` 支持 `--name`、`--description`、`--model`、`--effort`（`default` 为模型默认）、`--verbosity`、`--team`（`none` 为不属于任何团队）、`--prompt-file`（`-` 为标准输入）、`--welcome`（空字符串清除）、`--rich-cards`、`--timeout`。切换运行时和工作目录涉及文件与记忆迁移，仍在管理后台操作。

环境变量：

```bash
coreman bots env sales_bot
coreman bots env-set sales_bot API_BASE=https://example.com TIMEOUT=30
coreman bots env-unset sales_bot TIMEOUT
```

协作者与使用白名单（白名单为空表示所有人都能使用）：

```bash
coreman bots members sales_bot
coreman bots member-add sales_bot alice bob
coreman bots member-remove sales_bot bob
coreman bots allowed sales_bot
coreman bots allowed-add sales_bot alice
coreman bots allowed-remove sales_bot alice
coreman bots allowed-clear sales_bot
```

`allowed-remove` 移空白名单时会拒绝执行，因为那等于对所有人放开；确实要这样请用 `allowed-clear`。

技能与业务系统授权：

```bash
coreman bots skills sales_bot
coreman bots skill-install sales_bot query --env-group erp --var API_KEY=… [--keep-code]
coreman bots skill-uninstall sales_bot query
coreman bots systems sales_bot
coreman bots system-grant sales_bot crm [--write | --read-only] [--comment 说明]
coreman bots system-revoke sales_bot crm
```

内部技能（`security_level = internal`）的安装会提交审批，见下文「技能审批」。`system-grant` 不加 `--write` / `--read-only` 时保留原有的写权限设置。

## 技能管理

```bash
coreman skills list [--status enabled|disabled]
coreman skills show query
coreman skills enable|disable query
coreman skills set query --category 数据 --security-level internal --security-prompt-file policy.md
coreman skills upgrade query          # 为已安装该技能的所有 AI 员工重装
coreman skills delete query --yes
coreman skills sources
coreman skills source-sync tools      # 从来源仓库同步技能目录
coreman skills presets
coreman skills preset-set erp --label ERP --var DB_HOST=db.example.com --unset OLD_KEY --tag 生产
```

启停或修改技能会让待审申请与排队中的安装作废，须重新申请。`skills sync URL --source KEY` 是不经管理 API 的运维同步命令，仍可使用。

## 技能审批

需要 AI 委员会或平台管理员角色。

```bash
coreman approvals list [--all]       # 默认只看待审
coreman approvals show 3f2c1ab0
coreman approvals approve 3f2c1ab0 [--db erp] [--security-prompt-file policy.md] [--comment 意见]
coreman approvals reject 3f2c1ab0 --comment 原因
```

`approve` 不加 `--db` 时批准申请的全部数据库范围；`--db` 可以收窄，不能超出申请范围。

## 业务系统

管理系统本身与「开放给哪些 AI 员工」需要 AI 委员会或平台管理员角色；给某个员工授权用上文的 `bots system-grant`。

```bash
coreman systems list
coreman systems show crm
coreman systems create crm --name CRM --base-url https://crm.example.com --openapi-url https://crm.example.com/openapi.json
coreman systems set crm --delivery proxy --test-url https://crm.example.com/api/me
coreman systems enable|disable crm
coreman systems allow crm sales_bot support_bot
coreman systems disallow crm support_bot   # 同时收回该员工的授权
coreman systems allow-all crm --yes
coreman systems refresh crm                # 以自己的身份重新拉取操作目录
coreman systems test crm                   # 以自己的身份签测试令牌访问测试地址
coreman systems delete crm --yes
```

新登记的系统不开放给任何员工。`refresh` 与 `test` 以 `--as` 成员的身份签发业务令牌，这位成员需要在业务系统里有身份（邮箱前缀能唯一确定）。

## 团队与用户

```bash
coreman teams list
coreman teams show sales
coreman teams create sales --name 销售部 [--name-en Sales]
coreman teams set sales --name 销售中心 --enabled off
coreman teams members sales
coreman teams rule-add sales /公司/销售 [--platform feishu]   # 部门路径包含该文本的成员同步时归入本团队
coreman teams rule-remove sales /公司/销售
coreman teams delete sales --yes

coreman users list [--keyword 词] [--team 团队] [--unassigned] [--role 角色] [--status active|disabled]
coreman users show alice
coreman users set alice --team sales --role team_lead --bot-accessible on
coreman users enable|disable alice
```

在 `users set` 里改过的团队、语言、职位等字段会记为手工维护，之后的通讯录同步不再覆盖。

## 其余操作

管理后台的其他操作可以直接调接口，路径与管理后台请求的一致，返回接口的 `data`：

```bash
coreman api get /api/admin/cron-jobs --param page=1
coreman api post /api/admin/announcements --data @announcement.json
coreman api put /api/admin/cron-jobs/<id> --data @job.json --if-match 3
coreman api post /api/admin/cron-jobs/<id>/run
```

需要乐观锁的接口（返回体里带 `version` 的记录）要用 `--if-match` 带上当前版本号。
