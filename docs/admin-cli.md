# 管理命令

管理后台的操作都可以在服务器上用命令完成：AI 员工（含记忆、工作目录、飞书应用、群聊旁听）、技能、技能审批、业务系统、团队与用户、运行时与任务总线、定时任务、公告、平台设置、模型目录、平台应用、接口调用方、审计日志、统计、对话记录与任务，以及个人的提醒、凭证与授权。没有专用命令的接口可以用 `api` 命令直接调。

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

`set` 支持 `--name`、`--description`、`--model`、`--effort`（`default` 为模型默认）、`--verbosity`、`--team`（`none` 为不属于任何团队）、`--prompt-file`（`-` 为标准输入）、`--welcome`（空字符串清除）、`--rich-cards`、`--timeout`。切换运行时见下文 `switch-relay`。

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

飞书应用可用范围（读写的是飞书线上实际生效的名单，增删立即生效，不需要发布版本）：

```bash
coreman bots visibility sales_bot
coreman bots visibility-add sales_bot alice bob --dept 销售部
coreman bots visibility-remove sales_bot bob --dept /公司/销售部
coreman bots visibility-all sales_bot --yes
```

成员写登录名、邮箱、姓名或 id，须有飞书账号；部门写名称、完整路径、飞书部门 ID 或 id，须是同步进来的飞书通讯录部门。当前全员可用时，`visibility-add` 会拒绝执行，因为添加名单等于收窄范围；确实要只给这些人用请加 `--restrict`。读写优先用飞书登录应用，它没有权限时用机器人自己的应用（`visibility` 的「权限来源」一行写明）；两边都没有权限时命令会提示如何开通（见[飞书接入](feishu.md)）。

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

新建 AI 员工。凭证三选一：手填 `--cred`、`--scan` 扫码创建，或用已经完成的扫码会话（`--registration` 飞书、`--provision` 企业微信）：

```bash
coreman bots create demo-bot --platform feishu --name 演示助手 --model 模型 \
  --working-dir /data/skills/demo-bot --runtime 运行时 --scan
coreman bots create demo-wecom --platform wecom --name 演示助手 --model 模型 \
  --working-dir /data/skills/demo-wecom --cred bot_id=xxx --cred secret=xxx
```

可选参数与 `set` 基本一致：`--team`、`--description`、`--prompt-file`、`--effort`、`--verbosity`、`--timeout`、`--welcome`、`--rich-cards`、`--dev-mode`、`--env KEY=VALUE`（可重复）、`--disabled`（建好后先停用）。不写 `--runtime` 时暂不分配运行时。

`--scan` 先按同样的配置试建一次（标识重复、目录被占会在扫码前就报错），然后打印扫码链接，按服务端给的间隔轮询，扫码完成后建员工；超过 `--wait` 秒（默认 600）会取消扫码会话。默认复用本人之前扫码建好、还没用掉的应用，不想复用加 `--no-reuse`。也可以把扫码和建员工拆开：

```bash
coreman bots feishu-scan-start --name 演示助手 [--description 描述] [--no-reuse]
coreman bots feishu-scan-status 会话id
coreman bots feishu-scan-cancel 会话id
coreman bots wecom-scan-start [--no-reuse]        # 打印的是二维码内容
coreman bots wecom-scan-status|wecom-scan-cancel 会话id
```

记忆（改动先存在平台上，`memory-deploy` 后才写到运行时；记忆写文件名或 id 开头几位）：

```bash
coreman bots memories demo-bot [--deleted]
coreman bots memory-show demo-bot meeting.md
coreman bots memory-add demo-bot meeting.md 内容.md --name 例会 [--description 描述] [--type 类型]
coreman bots memory-set demo-bot meeting.md --content-file 内容.md --type project
coreman bots memory-delete demo-bot meeting.md --yes
coreman bots memory-collect demo-bot              # 从运行时回收到平台
coreman bots memory-deploy demo-bot --yes         # 覆盖同名文件、删除已删记忆
```

工作目录（只有员工的创建者和协作者能看，平台管理员也不行）：

```bash
coreman bots workspace demo-bot                   # 目录与状态
coreman bots workspace-files demo-bot [子目录]
coreman bots workspace-cat demo-bot AGENTS.md > AGENTS.md
coreman bots workspace-write demo-bot AGENTS.md AGENTS.md   # 不写文件则读标准输入
coreman bots workspace-write demo-bot notes/new.md 文件 --new
coreman bots workspace-git-status demo-bot
coreman bots workspace-git-diff demo-bot src/app.py
coreman bots workspace-init demo-bot              # 初始化或重试初始化
```

`workspace-write` 只能写 UTF-8 文本（不超过 1 MiB），会带上读到的文件哈希，读写之间文件被改过会拒绝；`--new` 只新建，目标已存在时失败。员工正在执行任务或工作目录正在迁移时不能写。

切换运行时或模型。不加 `--yes` 只打印预览（目标目录是否存在、是否为空、归属、原运行时是否在线等），确认后加 `--yes` 执行：

```bash
coreman bots switch-relay demo-bot 运行时 [--model 模型] [--mode copy|existing] [--dir 目录]
coreman bots switch-relay demo-bot 运行时 --model 模型 --yes
```

运行时写名称、节点名或 id；只换模型时写当前运行时。`--mode copy`（默认）把工作目录复制过去，`existing` 使用目标上已有的目录；原运行时离线时，`--allow-stored-memory` 允许用平台上的记忆快照（只能配合 `existing`）。

审批人（为空表示由发起人自己确认）：

```bash
coreman bots approvers demo-bot
coreman bots approvers-set demo-bot alice bob     # 整体替换
coreman bots approvers-set demo-bot               # 清空
```

可以求助的真人同事与 AI 员工互助（目前只支持飞书员工；同事须已绑定飞书）：

```bash
coreman bots humans demo-bot
coreman bots human-options demo-bot --keyword 张
coreman bots human-add demo-bot alice --responsibility "负责库存口径"
coreman bots human-set demo-bot alice --enabled off   # 暂停会取消进行中的求助
coreman bots human-remove demo-bot alice --yes
coreman bots bot-help demo-bot
coreman bots bot-help-set demo-bot off                 # 关闭会取消进行中的求助
```

API 驱动（目前只支持飞书员工；群别名在群里用 `/api 绑定 <别名>` 绑定）：

```bash
coreman bots api-access demo-bot
coreman bots api-access-set demo-bot on
coreman bots api-alias-remove demo-bot 值班群 --yes
```

群聊旁听：

```bash
coreman bots observe-set demo-bot --mode shadow --scope "库存与发货问题" [--role member|dispatcher]
coreman bots observe-decisions demo-bot [--chat 群id] [--would-speak|--silent] [--limit 50] [--before 记录id] [--messages]
coreman bots observe-review demo-bot 记录id should|should_not|none
coreman bots alert-source-list demo-bot
coreman bots alert-source-add demo-bot --chat 群id --sender-union-id 机器人union_id --service-user svc-demo [--dedupe-seconds 600]
coreman bots alert-source-set demo-bot 来源id [--service-user svc-demo] [--dedupe-seconds 600] [--enable|--disable]
coreman bots alert-source-remove demo-bot 来源id
```

开启旁听前须填旁听职责说明，且飞书应用要有「获取群组中所有消息」权限，见下面的 `observe-permission` 与 `feishu-app-scan --observe`。

飞书应用管理。名称、描述、图标和机器人菜单改完要发布新版本才生效；斜杠指令即时生效：

```bash
coreman bots feishu-app demo-bot                  # 应用信息、缺少的权限、版本
coreman bots feishu-app-base demo-bot --name 名称 --description 描述 [--help-url https://example.com/help]
coreman bots feishu-app-bot demo-bot --get-started 引导语 [--menus-file menus.json | --no-menu] [--menu-strategy 1|2|3]
coreman bots feishu-app-avatar demo-bot icon.png
coreman bots feishu-app-scopes-apply demo-bot     # 申请缺少的权限
coreman bots feishu-app-publish demo-bot --changelog "更新说明" [--remark 申请理由] [--version 1.2.0] --yes
coreman bots feishu-app-scan demo-bot [--observe] [--visibility]   # 扫码补齐权限
coreman bots observe-permission demo-bot
coreman bots feishu-commands demo-bot
coreman bots feishu-command-add demo-bot sum "总结本群讨论" [--icon xxx_outlined]
coreman bots feishu-command-set demo-bot sum --description "总结"
coreman bots feishu-command-delete demo-bot sum --yes
coreman bots feishu-command-defaults demo-bot     # 补齐内置指令
```

`--menus-file` 是菜单 JSON 数组，字段与 `feishu-app demo-bot --json` 输出里 `versions.online.bot.menus` 相同，整体替换并开启菜单。`feishu-app-publish` 默认版本号为在线版本加一，申请理由默认同更新说明。

运行环境检查与技能 MCP 登录：

```bash
coreman bots health-report demo-bot               # 只读检查，报告流式输出，最长约 5 分钟
coreman bots mcp-login demo-bot 技能               # 打印授权链接与会话
coreman bots mcp-login-complete demo-bot 技能 会话 '授权后浏览器地址栏里的完整地址'
coreman bots mcp-logout demo-bot 技能
```

`health-report` 要求操作人已绑定该员工所在平台的身份。

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

新建技能、维护来源、导出环境变量与本地安装：

```bash
coreman skills create report --source tools --category data [--security-level internal] [--repo URL] [--enable]
coreman skills create figma --source tools --install-type mcp --mcp-config-file mcp.json --mcp-oauth on
coreman skills source-add tools --label 工具 --git-url https://git.example.com/team/skills.git --token-file - --category data=数据
coreman skills source-set tools --label 工具箱 [--git-url URL] [--username 用户名] [--token-file 文件] [--remove-token] [--sort 10]
coreman skills env-exports report [--db erp] [--data-source mysql|doris] > report.env
coreman skills local-install report   # 打印一条在本机安装技能的命令
```

`create` 默认新建为停用，加 `--enable` 直接启用；与已删除的技能同名时等于恢复并按本次内容重新配置。新建来源和技能、修改来源需要 AI 委员会或平台管理员角色。访问令牌只从文件或标准输入（`-`）读，不出现在命令行和输出里；`source-set` 不给 `--token-file` 时保留原令牌，改了仓库地址则须重新给令牌或 `--remove-token`。`--category` 会替换来源的全部分类。

`env-exports` 把技能的固定环境组和所选数据库合并成 `export` 语句，输出的是明文，只有平台管理员能用，每次导出都记审计。`local-install` 所有成员都能用，命令里的令牌绑定本人和技能，30 分钟内有效，只支持 Git 安装的已启用技能。

<!-- 以下各章接在「## 团队与用户」之后、「## 其余操作」之前 -->

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
coreman users service-create svc-demo --name 演示服务账号 --email svc-demo@example.com  # 平台管理角色；邮箱前缀要等于登录名
coreman users set alice --team sales --role team_lead --bot-accessible on
coreman users enable|disable alice
```

在 `users set` 里改过的团队、语言、职位等字段会记为手工维护，之后的通讯录同步不再覆盖。

## 运行时

节点用名称、主机名或 id（可只写开头几位）指代。管理后台的「运行时实例」就是节点上按 AI 各一个的实例，由节点注册时自动创建，命令里都写成「节点 + AI」（`claude`、`codex`、`opencode`）。

任何成员都能列出可见的节点、生成安装链接把自己的电脑注册为运行时；节点归链接的创建人所有，所有者与 AI 委员会、平台管理员可以修改、启停、排空、删除它。只有管理员能把节点分配给团队或设为仅管理员可见；私有节点只有所有者能看到和选用。

```bash
coreman runtimes list
coreman runtimes show node-1
coreman runtimes set node-1 --name build-01 --visibility private
coreman runtimes set node-1 --draining on          # 排空：不再接新对话，进行中的跑完
coreman runtimes set node-1 --active off           # 停用：同时取消节点上进行中的调用
coreman runtimes set node-1 --team sales           # 分配给团队；--team "" 改回公共池
coreman runtimes set node-1 --workspace-root /data/ai   # 节点在线且没有 AI 员工在用时才能改，由节点确认后生效
coreman runtimes delete node-1 --yes               # 仍有 AI 员工使用（含迁移目标）时拒绝

coreman runtimes link-create --name laptop --visibility private
coreman runtimes link-create --proxy http://proxy.example.com:3128 --git-host git.example.com --ca-file ca.pem
coreman runtimes links
coreman runtimes link-revoke 3f2a
```

`link-create` 打印一行安装命令，在要注册的主机上执行即可；链接 24 小时内有效，只能用一次。

### 席位

一个节点上同一种 AI 可以登录多个账号（席位），由 AI 委员会或平台管理员管理。节点须在线并已升级到支持席位的版本。席位用标识或名称指代，默认席位写 `default`。

```bash
coreman runtimes seats node-1 [claude]
coreman runtimes seat-add node-1 claude --label bob
coreman runtimes seat-login node-1 claude bob      # 打印授权链接、一次性代码与登录会话
coreman runtimes seat-login-complete node-1 claude bob --session <登录会话> --code <认证码>
coreman runtimes seat-login-complete node-1 opencode bob --session <登录会话> --code-file -   # 从标准输入读 API Key
coreman runtimes seat-activate node-1 claude bob --yes   # 整机切换：新对话都用这个席位，进行中的在原席位跑完
coreman runtimes seat-remove node-1 claude bob --yes
```

登录分两步：`seat-login` 让节点上的 CLI 发起登录并打印下一步该执行的命令。Claude 在浏览器登录后把页面给出的认证码用 `--code` 填回；Codex 在浏览器输入一次性代码，授权完成后直接执行 `seat-login-complete`；OpenCode 填 API Key。

### AI 实例

```bash
coreman runtimes backends [--provider codex]
coreman runtimes models node-1 claude
coreman runtimes probe node-1 claude               # 健康探测并预填模型目录，不健康时退出码为 1
coreman runtimes agent-probe node-1 claude rate-limits    # 或 health-check，节点在后台执行
coreman runtimes usage node-1 claude               # 今天（UTC）的对话数、token 与费用
coreman runtimes status node-1 claude              # 实时状态与进行中的任务
coreman runtimes session node-1 claude <会话 id>    # 跟看一次会话的事件流，Ctrl-C 结束
```

`backends` 与 `models` 任何成员可用（只列可见的实例）；`probe`、`agent-probe`、`usage`、`status` 需要 AI 委员会或平台管理员角色。`session` 的查看权限与管理后台的会话查看页一致：自己的私聊自己可看；管理员可看群聊与企微私聊，飞书私聊和以本人身份运行的定时任务谁都不能代看。

## 任务总线

网关、工作进程与任务队列的运行状态。查看需要团队负责人及以上角色，取消、重投、排空需要平台管理员。

```bash
coreman queue overview
coreman queue tasks [--queued] [--limit 50]
coreman queue cancel-task 1234 --reason stuck --yes
coreman queue instances
coreman queue leases
coreman queue outbox [--pending]
coreman queue outbox-retry 567
coreman queue drain --instance worker-a --yes      # 整个进程不再接新活
coreman queue drain --bot sales_bot --yes          # 只让这个 AI 员工换一个网关
```

取消、重投、排空都只是落库请求，由网关与工作进程自己执行；`drain --bot` 要求该员工当前有在线网关持有。

## 定时任务

能看到自己创建或协作的 AI 员工的定时任务。任务写 id（开头几位就够）或名称。

```bash
coreman cron list [--bot sales_bot]
coreman cron show 日报
coreman cron runs 日报 [--limit 50]                 # 执行记录，新的在前
coreman cron create --bot sales_bot --name 日报 --cron '0 9 * * 1-5' \
  --prompt-file prompt.md --to-user alice --email alice@example.com
coreman cron create --file job.json --name 早报       # JSON 打底，命令行字段覆盖同名字段
coreman cron set 日报 --at 2026-12-01T09:00:00+08:00  # 改成一次性执行
coreman cron enable|disable 日报
coreman cron run 日报                               # 立即执行一次
coreman cron cancel-run 日报                        # 取消正在执行或等待触发的这一轮
coreman cron delete 日报 --yes
```

`create` 与 `set` 支持 `--name`、`--cron`（周期）或 `--at`（一次性，须带时区）、`--timezone`（默认 `Asia/Shanghai`）、`--prompt` / `--prompt-file`、`--system-prompt-file`、`--precheck-file`、`--precheck-timeout`、`--expires`（空字符串清除）、`--to-user`、`--to-chat`、`--email`（可重复，给出时整体替换原名单；`--clear-targets` 清空，没有接收人时只通知创建者）、`--webhook on|off`、`--webhook-url`（企微群机器人地址，同时打开开关；空字符串清除）。`create` 另有 `--bot`、`--data` / `--file`（字段同接口）、`--disabled`。

权限与管理后台一致：操作定时任务要先绑定该 AI 员工所属平台的身份；修改、启用、删除和测试通知只有创建者能做，停用、立即执行和取消本轮协作者也能做。成员在私聊里给自己建的定时任务（列表里「个人」为是）管理员只能停用，看不到指令。

通知与预检：

```bash
coreman cron notification-chats sales_bot          # 员工待过的群，群 id 可填进 --to-chat
coreman cron test-notification 日报                 # 按已保存的设置发一条测试通知
coreman cron notification-tests 日报                # 测试通知的投递结果
coreman cron precheck-test check.py --ctx '{"n": 3}'
```

## 公告

只有 AI 委员会和平台管理员能用。公告写 id，开头几位就够。

```bash
coreman announcements list
coreman announcements show 1a2b3c4d
coreman announcements create --content '今晚 22:00 维护' [--bot sales_bot | --relay 运行时] \
  [--start 2026-12-01T22:00:00+08:00] [--end 2026-12-01T23:00:00+08:00] [--inactive]
coreman announcements set 1a2b3c4d --content-file notice.md --global
coreman announcements enable|disable 1a2b3c4d
coreman announcements delete 1a2b3c4d --yes
```

不给 `--bot` / `--relay` 时是全局公告；`set` 用 `--global` 改回全局。`--start`、`--end` 传空字符串为不限。

## 我的提醒

只能看和管自己的：在私聊里让 AI 员工设的固定提醒，以及确认卡片建的定时任务。

```bash
coreman reminders list --as alice
coreman reminders pause|resume 1a2b3c4d --as alice   # 定时任务
coreman reminders cancel 1a2b3c4d --as alice         # 固定提醒
coreman reminders delete 1a2b3c4d --yes --as alice   # 定时任务，运行中的一轮会先取消
```

## 我的个人设置

`me` 下的命令都只作用于 `--as` 的成员本人。

个人凭证（AI 员工经表单向本人索取的账号、密钥）。值不出现在命令行上：从文件或标准输入读，在终端里直接运行时提示输入且不回显；保密项在列表里不显示值。

```bash
coreman me credentials --as alice
coreman me credential-set 销售 DEMO_PIN --as alice              # 提示输入新值
coreman me credential-set 销售 DEMO_PIN --value-file pin.txt --as alice
coreman me credential-delete 销售 DEMO_PIN --yes --as alice
coreman me credential-request <表单 id> --as alice              # 表单链接里的 id
coreman me credential-submit <表单 id> --values-file values.json --as alice
```

AI 员工写名称或 id。`--values-file` 是 JSON 对象 `{"变量名": "值"}`，默认从标准输入读。

飞书个人授权与企业微信绑定：

```bash
coreman me feishu-auth --as alice
coreman me feishu-revoke 销售 --yes --as alice
coreman me wecom --as alice
coreman me wecom-set --level all_except_send --enabled on --as alice
coreman me wecom-check --as alice        # 逐项试一次只读调用
coreman me wecom-renewed --as alice      # 已在企业微信里续期后执行
coreman me wecom-scan --as alice         # 打印二维码链接，等扫码完成（--timeout 秒，--no-wait 只打印）
coreman me wecom-scan-cancel --as alice
coreman me wecom-unbind --yes --as alice
```

飞书个人授权由本人点击授权卡片完成（用到飞书能力时 AI 员工会自动私信发卡，也可私聊发「连接飞书」手动选范围），命令行只能查看和撤销。

API 令牌（外部程序以本人身份给 AI 员工下任务，见 [通过 API 驱动 AI 员工](features/api-tasks.md)）。明文只在新建时打印一次：

```bash
coreman me api-tokens --as alice
coreman me api-token-create 工单系统 --bot demo-bot --bot 销售 --days 90 --as alice
coreman me api-token-revoke 工单系统 --yes --as alice       # 名称、前缀（至少 8 位）或 id
```

## 平台设置

需要平台管理员角色（`settings defaults` 除外）。

```bash
coreman settings show                       # 全部设置；长文本只显示字数
coreman settings get prompt_security_policy > policy.md
coreman settings set session_ttl_hours=24 default_effort_level=high card_icon_url=https://example.com/logo.png
coreman settings set default_effort_level=null   # 清空
coreman settings set --file prompt_security_policy=policy.md
coreman settings defaults                   # 新建 AI 员工的默认值，所有成员可看
```

`set` 按接口的字段类型解析值：文本项原样保存，其余按 JSON 解析（`true`、`24`、`0.5`）；`null` 表示清空。只认管理后台可改的设置项，写错键名会报错并列出可改的键。默认模型由模型目录决定，用 `models set … --default on` 修改。

告警接收渠道与通知邮箱：

```bash
coreman settings alerts
coreman settings alerts-set --channel 通知应用=alice --channel 通知应用=bob   # 替换全部渠道
coreman settings alerts-set --clear
coreman settings notifications
coreman settings notifications-set --enabled on --host smtp.example.com --port 465 --security tls \
  --username robot --sender robot@example.com --password-file -
```

告警渠道的应用写平台应用的名称或 id，须启用且具备通知能力；接收人须为已同步、在职、绑定了该平台的管理员或 AI 委员会成员。通知邮箱的密码只从文件或标准输入读，不回显；更换服务器、端口或用户名时须重新给密码。

## 模型目录与价格

```bash
coreman models list [--provider claude]
coreman models add claude demo-model --display-name 演示 [--default] [--xhigh] [--max] [--sort 10]
coreman models set claude demo-model --default on --retired off --display-name ""   # 空串清除显示名称
coreman models delete claude demo-model --yes
coreman models prices [--model demo-model]
coreman models price-set demo-model --from 2026-01-01 --input 3 --output 15 --cache-read 0.3 --cache-write 3.75
coreman models price-set demo-model --from 2026-01-01 --input 4        # 改已有生效日只给要改的项
```

所有成员都能查看；增删改需要 AI 委员会或平台管理员角色。同一提供方只有一个默认模型，退役或删除默认模型时由排序最靠前的未退役模型接任；仍有 AI 员工在用的模型不能删除。价格单位是美元 / 百万 Token，`--provider` 缺省时按模型名判断后端。

## 平台应用

企业微信与飞书应用，需要平台管理员角色。

```bash
coreman apps list [--platform wecom|feishu]
coreman apps show 企微通讯录                    # 名称或 id（可只写开头几位）；密钥已脱敏
coreman apps create --platform wecom --name 企微通讯录 --capability contact_sync --capability callback \
  --corp-id ww123 --secret-file secret.txt [--callback-token-file 文件] [--callback-aes-key-file 文件]
coreman apps create --platform feishu --name 飞书登录 --capability login --app-id cli_xxx --secret-file -
coreman apps set 企微通讯录 --name 企微应用 --enabled off [--secret-file 文件]
coreman apps test 企微通讯录                    # 用凭证换一次 access_token；未通过时退出码为 1
coreman apps sync 企微通讯录                    # 后台执行通讯录同步
coreman apps runs 企微通讯录 [--limit 20]
coreman apps run-show 42
coreman apps delete 企微通讯录 --yes
```

能力取值为 `contact_sync`、`login`、`notify`、`callback`；`set` 给了 `--capability` 就替换全部能力。Secret、回调 Token 与 AESKey 只从文件或标准输入（`-`）读，不出现在命令行；`set` 没给的密钥保持原值。

## 接口调用方与签名密钥

接口调用方与 JWT 签名密钥需要平台管理员角色；令牌签发方 AI 委员会也能看。

```bash
coreman clients list
coreman clients create demo-app --name 演示 --scope org --scope notify [--secret-file 文件]
coreman clients set demo-app --scope relay --enabled off      # --scope 替换全部接口组；--no-scope 清空
coreman clients rotate-secret demo-app --yes                 # 旧密钥立即失效
coreman clients jwt-keys
coreman clients jwt-rotate --yes
coreman clients token-providers
```

调用方密钥只在 `create` 和 `rotate-secret` 时打印这一次，请立即保存；不给 `--secret-file` 时随机生成。

## 审计日志、统计、对话记录与任务

```bash
coreman audit list [--actor alice] [--action bot.] [--target-type bot] [--target-id ID] \
  [--since 2026-01-01T00:00:00Z] [--until 时间] [--limit 200]
coreman stats [--start 2026-01-01] [--end 2026-01-31] [--timezone UTC|Asia/Shanghai|Asia/Tokyo]
coreman chats list [--bot sales_bot] [--user alice] [--status error] [--chat-type group] [--keyword 词] \
  [--token-id jti] [--since 时间] [--until 时间] [--limit 100]
coreman chats show 12345
coreman chats stats [与 list 相同的筛选]
coreman tasks list [--status running] [--bot sales_bot] [--chat-id 群] [--keyword 词] [--limit 50] [--before-id 100]
coreman tasks show T-12
```

审计日志需要 AI 委员会或平台管理员角色，`--action` 按前缀匹配，`--actor` 按登录名模糊匹配。统计与任务所有成员都能看，统计只有聚合数字，费用为估算。对话记录只列出自己有份的部分（自己管的 AI 员工或自己说过的话；私聊只有本人能看），列表只有摘要，全文用 `chats show`。列表默认显示最新 50 条，`--limit` 调整。

## 其余操作

管理后台的其他操作可以直接调接口，路径与管理后台请求的一致，返回接口的 `data`：

```bash
coreman api get /api/admin/cron-jobs --param page=1
coreman api post /api/admin/announcements --data @announcement.json
coreman api put /api/admin/cron-jobs/<id> --data @job.json --if-match 3
coreman api post /api/admin/cron-jobs/<id>/run
```

需要乐观锁的接口（返回体里带 `version` 的记录）要用 `--if-match` 带上当前版本号。
