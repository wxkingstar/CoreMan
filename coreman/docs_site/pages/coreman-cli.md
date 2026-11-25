# 命令行 coreman

`coreman` 是管理台的命令行版本：AI 员工、定时任务、技能、业务系统授权、对话记录等管理台里能做的事，在终端里也能做。装在自己电脑上，登录一次，之后的每条命令都以你本人的身份调用 CoreMan：

- 权限和你在管理台里一样：管理台里能改的，命令行里才能改。
- 每次修改都记审计，操作人是你。
- 适合批量改、写脚本，也适合交给本机的 Claude Code、Codex 等 Agent 代你操作。

## 1. 安装

需要 Python 3.12 以上和 [uv](https://docs.astral.sh/uv/)：

```bash
uv tool install git+https://github.com/wxkingstar/CoreMan
```

升级：

```bash
uv tool upgrade coreman
```

## 2. 登录

```bash
coreman login --server {{COREMAN_BASE_URL}}
```

命令会打开浏览器（打不开时把终端里打印的地址复制到浏览器）：

1. 用管理台的方式登录 CoreMan，已经登录过的直接跳过。
2. 确认页上写着「CoreMan 命令行」和要授予的权限，点「允许」。
3. 浏览器显示「登录完成」，回到终端即可。

加 `--no-browser` 只打印地址、不自动打开。地址要在运行命令的这台电脑上打开：授权完成后浏览器会回到本机的一个临时端口，别的电脑接不到。

登录信息保存在 `~/.config/coreman/credentials.json`（只有你能读，权限 0600；可用环境变量 `COREMAN_CONFIG_DIR` 换目录）。访问令牌一小时有效，快过期时命令自己续期；30 天没用过要重新登录。

确认当前身份：

```bash
coreman whoami
```

## 3. 常用命令

所有命令都有 `--help`，例如 `coreman bots --help`、`coreman bots set --help`。

```bash
coreman bots list --mine                           # 我创建或协作的 AI 员工
coreman bots show demo-bot                         # 详情
coreman bots prompt demo-bot > prompt.md           # 导出系统提示词
coreman bots set demo-bot --prompt-file prompt.md  # 改完写回
coreman bots set demo-bot --model 模型 --effort high
coreman bots skills demo-bot                       # 已装的技能
coreman bots systems demo-bot                      # 业务系统权限
coreman cron list --bot demo-bot                   # 定时任务
coreman cron run 日报                              # 立即执行一次
coreman reminders list                             # 我的提醒与定时任务
coreman tasks list --view needs_me                  # 待我处理的任务
coreman tasks show T-12                            # 任务详情、待回答的问题
coreman tasks create "整理周报" --assignee demo-bot --due 2026-12-01
coreman tasks answer T-12 accept                   # 接受；拒绝、退回要写理由
coreman tasks complete T-12 "已放进共享表"
```

没有专用命令的管理台操作，用 `api` 直接调对应接口，输出返回的数据：

```bash
coreman api get /api/admin/bots --param scope=mine
coreman api patch /api/admin/bots/<id> --data @body.json --if-match 3
```

约定：

- AI 员工可以写标识（`bot_key`）、名称或 id；成员写登录名、邮箱或姓名；名字重复时命令会列出候选，改用标识。
- 列表默认打印表格；加 `--json` 输出原始数据，方便配合 `jq`。
- `set` 类命令只改命令行上给出的字段，其余保持不变。
- 删除类操作、「对所有人放开」类操作必须加 `--yes`。
- 成功退出码为 0；出错时把原因打印到标准错误，退出码为 1。
- 环境变量等敏感值只显示脱敏值，明文不经过终端。

## 4. 交给本机 Agent 用

Agent 能跑终端命令，就能用 `coreman`。你登录一次后，告诉它：

```text
用 coreman 命令行操作 CoreMan，先看 coreman --help 和各命令的 --help。
删除类操作先问我。
```

Agent 以你的身份操作，能做的和你一样多，所以：

- 删除、对所有人放开这类不可撤销的操作，让它先问你再加 `--yes`。
- 不用时执行 `coreman logout`，或者在管理台撤销这次授权（见下文）。

只想让 Agent 查业务系统接口的话，用权限更小的 [业务系统目录 MCP](systems-catalog-mcp.md)。

## 5. 哪些操作命令行做不了

命令行算「本人不在管理台」，下面这些只能在管理台里本人操作：

- 建或吊销个人 API 令牌（`coreman me api-token-*`）。
- 个人凭证、飞书或企业微信的个人授权（`coreman me credential-*`、`feishu-*`、`wecom-*`）。
- 个人助理的加密内容（知识库、对话原文等）。

这些命令会返回「Interactive login required」或「这些接口只对本人在管理台里的登录开放」。

## 6. 退出与撤销

```bash
coreman logout
```

吊销这次授权并删掉本机的登录信息。

电脑丢了或令牌可能泄露时，在管理台「我的 API 令牌」页的「本机应用授权」里找到「CoreMan 命令行」，点「撤销」，立即失效。

## 7. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `请先用 coreman login --server … 登录` | 本机还没登录 | 按第 2 节登录。 |
| `登录已失效…请重新 coreman login` | 30 天没用、被撤销，或账号停用 | 重新 `coreman login`。 |
| `只有本机地址可以用 http，其余请用 https` | `--server` 写成了 http | 改成 `https://` 开头的地址。 |
| `这个地址不是支持远程登录的 CoreMan` | 地址写错，或这个 CoreMan 版本太旧 | 核对地址；旧版本请管理员升级。 |
| `等待浏览器授权超时` | 5 分钟内没有在浏览器里点「允许」 | 重新执行，尽快完成授权。 |
| 403 `没有权限执行该操作` | 管理台里你也做不了这件事 | 找 AI 员工的创建者把你加为协作者，或找管理员。 |
| 403 `Interactive login required` | 见第 5 节 | 到管理台里操作。 |

## 8. 管理员：在服务器上运行

部署方在 API 容器里运行同一套命令时，用 `--as 登录名` 指定以谁的身份操作（或设置环境变量 `COREMAN_CLI_USER`），不需要登录，用于首次初始化和运维。写了 `--as` 就是这种方式；不写才走上面的登录。会话里会记下执行命令的系统账号与主机，审计可以据此追到真正操作的人。
