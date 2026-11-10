# 规则文档与每日备份

AI 员工在工作中沉淀的规则写在工作目录的 `AGENTS.md` 和 `docs/` 里。CoreMan 每天把这些文件提交到一个共用的 GitHub 仓库，所有员工共用一个仓库、一个令牌，不需要人参与。

## 规则怎么存

非开发模式的员工，system prompt 多一段固定说明（排在日期与时间之后），要求员工：

- `AGENTS.md`（`CLAUDE.md` 是同一份）保持简短：做什么、对每个任务都适用的少数规则，以及规则文档的索引，每篇一行说明什么时候该读。
- 每个主题的详细规则写在 `docs/<主题>.md`，从索引里引用，任务用到时才打开（渐进式加载）。
- 新增或修改规则时，文档和索引行一起改；`AGENTS.md` 某一节超过几行就挪进 `docs/`。
- 这些文件里不写密钥、令牌和个人信息。

工作目录自己有 `.git` 且配了远端时，以那个仓库的约定为准，这段说明不适用。开发模式的员工不加这段：开发模式的根目录本身就是仓库，规则随仓库维护。

Claude 员工下一轮就会用上新说明；Codex 线程沿用开始时的 system prompt，会话续期后才生效。

## 每日备份

配置 `RULES_BACKUP_REPO`（`owner/name`）和 `RULES_BACKUP_TOKEN` 后，scheduler 每 24 小时备份一轮（每 10 分钟看一次是否到期，上一轮的时间记在 settings 表的 `rules_backup.last_run`）。

- 仓库结构：每个员工一个目录 `bots/<标识>/`，根目录 `INDEX.md` 列出标识与名称。
- 备份范围：根目录的 `AGENTS.md`、`CLAUDE.md`（两者是一份正本加一个链接时只备份正本），以及 `docs/` 下扩展名为 `.md`、`.markdown`、`.mdx`、`.txt`、`.rst`、`.yaml`、`.yml`、`.json`、`.toml` 的 UTF-8 文本文件。单个文件不超过 512 KiB，每个员工最多 500 个文件、8 MiB，`docs/` 最多 8 层；超出的文件不备份，记在结果的 `skipped` 里。文件管理里看不到的文件（`.env*`、密钥文件等）也不会读到。
- 不备份：根目录自己是带远端的 Git 仓库的员工（结果记为 `own_repo`），停用、未分配运行时、工作目录不在「就绪」状态的员工（计入 `inactive`）。
- 一轮只产生一个提交，内容没变就不提交；员工删掉的文件在仓库里也删掉，历史里还能找到。
- 读取失败（节点离线、运行时版本过旧等）的员工记为 `failed`，仓库里保留它上一次的内容，下一轮再试。已删除员工的目录原样保留，`INDEX.md` 里标为「已删除」。
- 提交前把常见格式的令牌（GitHub、GitLab、OpenAI 风格的 `sk-`、AWS、阿里云 AccessKey、Slack）、私钥块，以及 `password: ...`、`token=...` 一类赋值的值替换成 `[REDACTED]`。这只是兜底，不能代替「规则文档里不写密钥」。
- 整轮失败（GitHub 不可达、令牌失效、仓库为空）记下错误，一小时后重试。分支被别人同时推过时本轮失败，下一轮基于新提交重来，不强推。

令牌只在平台进程里使用。文件经已有的工作区只读操作（Git 状态、列目录、读文件）从节点读出，运行时节点不需要升级，也不需要 GitHub 凭证。

## 配置

1. 在 GitHub 建一个**私有**仓库，并创建一个初始提交（例如 README）。仓库为空时备份会报错。
2. 建一个 fine-grained personal access token：只选这个仓库，权限 Contents 设为 Read and write，其余不选。
3. 在 `.env` 里配置后重启 scheduler：

   ```bash
   RULES_BACKUP_REPO=company/ai-employee-rules
   RULES_BACKUP_TOKEN=github_pat_...
   ```

4. 不想等到第一次定时备份，可以先手动跑一轮。不加 `--apply` 只读取并列出每个员工的改动，加上才提交：

   ```bash
   ./deploy/coreman rules-backup
   ./deploy/coreman rules-backup --apply
   ```

scheduler 日志事件：`rules_backup_done`（提交、改动文件数和各状态的员工数）、`rules_backup_bot_failed`（单个员工读取失败的原因）。
