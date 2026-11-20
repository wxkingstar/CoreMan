# 开发模式

工作目录是 Git 仓库的 AI 员工打开开发模式后，每个会话在自己的 worktree 和分支里改代码，改完开 PR，由人审查后合进主干。多个会话同时开发互不干扰，主检出始终保持干净。

## 开关

AI 员工编辑页 →「更多设置」→ 体验 →「开发模式」，默认关闭。工作目录不是 Git 仓库时开关不起作用，照旧在工作目录里运行。

需要数据库迁移 `0064`（`bots.dev_mode`，默认 `false`），运行时节点也要升级。节点没升级时开关不生效，对话仍在主检出里运行。

## 会话 worktree

- 位置：`<工作目录>/.coreman-worktrees/<会话 id>`，分支 `coreman/session-<会话 id>`。建 worktree 时先 `git fetch origin`，从远端默认分支的最新提交切出；拉取失败时从本地已有的远端分支或 `HEAD` 切出。
- 同一会话始终用同一个目录，Claude 的续聊记录按目录保存。群聊里所有人共用群的会话，也就共用一个 worktree。定时任务每次运行是新会话，各用一个 worktree。
- 主检出里没有提交进仓库的 `.mcp.json`、`.agents/skills`、`.claude/skills`，会以符号链接放进 worktree，并写进 `.git/info/exclude`，免得被误提交。`CLAUDE.md`、`AGENTS.md` 要提交进仓库，worktree 里才看得到。
- `.coreman-worktrees` 不出现在主检出的 `git status` 和文件管理里，换机复制工作目录时也不会带上。

## 提示词

开发模式的 system prompt 多一段固定说明（排在日期与时间之后、组织背景之前），要求 AI 员工：

0. 本分支还没有自己的提交时（新会话），先查未合并的 `coreman/session-*` 分支和打开的 PR。用户的请求是在接着做其中某一项时，在本 worktree 里 `git switch` 到那条分支继续，提交追加到原来的 PR。那条分支还被另一个 worktree 占着时，只有那个 worktree 没有未提交改动才 `git worktree remove` 掉它，否则先问用户。
1. 只在本会话的 worktree 里修改，跑项目自带的检查，然后提交到本分支。
2. 改动就绪后推送分支，用 `gh pr create` 或 `glab mr create` 开 PR，把链接回复给用户。
3. 不向默认分支推送或变基。PR 由人审查后合并，AI 员工不自行合并。唯一的例外：本轮发言者明确要求合并某个 PR，并且以这个人的身份来合并：用本人保存的个人凭证，或者经过按本人授权判断的工具（例如以本人身份调用的发布服务），不用共用凭证；代码托管平台或服务拒绝就停下并如实报告。审查意见用新提交改在同一分支上。
4. 中断后继续工作前，先拉取远端；默认分支有新提交时，合并进本分支。

开 PR 用的是节点上 `gh` / `glab` 的登录状态。

## 定期拉取与清理

运行时节点每 30 分钟检查一次本节点上 AI 员工的工作目录。只处理用过开发模式（存在 `.coreman-worktrees`）的仓库：

- `git fetch --prune origin`。主检出停在默认分支且没有未提交改动时，快进到远端最新提交，否则跳过。
- 只清理分支已经合进远端默认分支、没有未提交改动、闲置超过 7 天的 worktree，连本地分支一起删。闲置是指 7 天里这个会话没有发起过任何一轮对话（每轮开始时刷新 worktree 目录的修改时间）。还没有提交的新分支也算已合并，所以同样要闲置 7 天才删。
- 「已合并」指分支上的提交都已经在远端默认分支里（普通合并或快进）。squash 合并和 rebase 合并认不出来，这类 worktree 和没合并的一样留着。
- 没合并的 worktree 一律不清理，推没推送都一样，由人定期审查要不要保留。在节点上执行 `git -C <工作目录> worktree list` 可以看到全部 worktree；确认不要了，用 `git worktree remove` 删掉。

会话 72 小时（`session_ttl_hours`）没有对话会换新会话，新会话用新的 worktree，靠上面第 0 条接回原来的分支。

## 开发环境预览

节点开了开发环境预览时，AI 员工改完代码先在本节点把 worktree 跑起来，经开发域名交给用户点开验证，用户确认后再推分支、开 PR 走测试环境。设计见 `docs/superpowers/specs/2026-11-20-dev-preview.md`。

- **节点**：`config.json` 加 `dev_preview` 段（字段见 `runtime_daemon/README.md`），本机装好 Docker 与 Compose（macOS 用 colima）。守护进程下载并常驻 frpc，经 wss 连到网关后面的 frps；隧道在跑才声明 `dev_preview_v1`。
- **平台**：员工开了开发模式、且所在节点声明了 `dev_preview_v1`，开发模式说明后面多一段「Development Preview」：开发环境只求快，改完先 `coreman-dev up`（之后靠热更新）、用最快的方式确认改动生效就先回复链接，再跑和改动相关的检查；完整检查放到推送、开 PR 前，由 PR 把关；检查在本轮里跑完，不放后台（后台任务结束会再触发一轮，用户的新消息要排队）；用户确认后再开 PR；改表结构、单点登录相关的改动不走开发环境。没有这项能力的节点照旧「改动就绪就开 PR」。
- **命令**：在 worktree 根目录执行 `coreman-dev up|down|status|logs|list`。`up` 用 `docker compose -p <项目>-<id> -f dev/compose.yaml up -d --build --force-recreate --wait` 启动，注入 `DEV_PORT`、`DEV_ENV_FILE`、`DEV_URL`、`DEV_PREVIEW_ID`（仓库独有的变量放在环境文件同目录的 `projects/<仓库名>.env`，会拼进 `DEV_ENV_FILE`），先查本机入口、再经开发域名查一遍，都通了才输出地址。地址是 `https://<仓库名>-<6 位>.<domain>`，同一节点同一 worktree 固定。
- **仓库约定**：`dev/compose.yaml` 只放本仓库的服务和一个入口（监听 `127.0.0.1:${DEV_PORT}`），环境变量从 `${DEV_ENV_FILE}` 读，服务都写 healthcheck；关掉自动迁移、MQ 消费者和定时任务；仓库里不放凭据。服务以开发模式运行：源码挂进容器，后端用自带的热重载、前端用 dev server，改代码几秒内生效，只有依赖、Dockerfile、`dev/` 或环境变量变了才要重新 `up`；监听一律开轮询（macOS 上 colima 的挂载目录不会把文件变化事件传进容器）。由 AI 员工生成和维护。
- **回收**：守护进程每 30 分钟检查一次：worktree 已删的预览删容器、卷和本地构建的镜像；闲置超过 `idle_hours` 的停容器（下次 `up` 再起）；每天清一次 72 小时前的镜像与构建缓存。状态在数据目录的 `dev-preview/` 下。

