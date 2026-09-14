# CoreMan Runtime Daemon

一个独立的系统用户环境安装一个 Daemon，可同时提供 Claude Code 与 Codex。Daemon 主动连接 CoreMan；目标机器无需开放入站端口。Claude 只保留 v1。

## 安装与接入

1. 在目标用户环境准备 Python 3.10+（含 venv）、Git，以及需要使用的 Claude Code / Codex CLI。以该用户完成 CLI 登录；需要安装 Skill 时还需要 Node.js/npm。已有 chroot/nspawn 应先进入目标环境。
2. 管理员打开「运行时管理 → 安装运行时」，填写目标机器的项目主目录绝对路径，选择团队。链接固定 24 小时有效，默认并发上限为 10。可设置 AI 代理、CoreMan 连接代理、CLI 路径、环境和并发上限。目录必须可写，不能为 `/`，不能借符号链接跳出实际目录。
3. 复制生成的 curl 安装命令，在目标用户环境运行。命令注入 CoreMan 地址和一次性安装凭证；安装包包含本机 Go 驱动及离线 Python 依赖。
4. 首次注册自动纳管并启用。列表分别显示 Claude/Codex 是否安装、登录状态、模型、健康和额度；未安装或未登录不会被标成可用。后续安装/登录 CLI 后最多约一分钟重新发现。

链接默认只注册一个节点，可过期或撤销；相同节点的注册网络重试不会重复创建。链接仅生成时返回明文。运行身份保存于 `~/.local/share/coreman-runtime/config.json`（0600），不要复制到其他用户环境。节点只上报登录状态，不上传 CLI 登录文件。

已安装 Codex 的 Daemon 后端继承后台 `codex` 模型目录中未退役的模型，新增模型无需更新本机驱动；旧节点在下一次心跳时自动应用。Claude 后端在心跳时同步目录中的原生 Claude 模型（`claude-` 或兼容的 `vllm/claude-` 前缀），过滤已退役模型，不自动纳入 MiniMax/Kimi 等第三方模型。驱动上报的模型用于发现和补充目录，不作为账号白名单。目录配置不代表账号已获该模型权限，实际调用由 CLI 校验。未安装对应 CLI 时模型列表为空；普通中继的模型限制规则不变。

首次 curl 下载使用调用者的 curl 网络环境；下载后 CoreMan 连接使用表单中显式配置的连接代理，默认直连。AI 请求代理独立配置。CoreMan 地址须可从目标环境访问，正式部署使用 HTTPS。

## 常驻与运维

- Linux 有可用用户 systemd 时安装 `~/.config/systemd/user/coreman-runtime.service`。启动、停止、重启分别用 `systemctl --user start/stop/restart coreman-runtime`。无人登录时自启需要管理员开启对应用户的 linger；未开启会在后台提示。
- macOS 使用 `~/Library/LaunchAgents/org.coreman.runtime.plist`，依附当前用户的 launchd 域。登录会话的自动启动不等于无人登录开机启动。
- 无用户服务管理器的 chroot/nspawn 使用监督进程。安装器输出 `~/.local/share/coreman-runtime/supervise.sh`；需要宿主机服务管理器托管该入口才能保证重启后启动。
- 后台「排空任务」停止接收新的 POST 任务，已有任务继续；「关闭启用」同时撤销节点接单能力并取消在途请求。排空结束后再停止服务。
- 日志位于安装目录的 `service.log`、`runtime.log` 和驱动日志；`state.json` 保存最近连接状态。服务启动后 60 秒未确认上线，安装器会明确报告，不会假报成功。
- Git/Skill/MCP、记忆同步和额度/健康探测沿用项目 Agent。Claude 额度 statusLine 探针是可选项；启用前备份原 settings，替换现有状态栏需由安装者主动选择。
- 当前没有后台一键升级/卸载功能。更换发布包前需排空、停止服务并保留原节点配置；不要通过反复兑换安装链接升级同一个用户环境。

## 发布与开发

CoreMan 数据库升级到 Alembic `0018_runtime_nodes`。API 镜像构建会生成四个安装包并包含安装脚本与会话查看模板。手工运行 API 时先构建：

```sh
python3 runtime_daemon/build.py
```

构建机需要 Go 1.24+、Python/pip 和依赖下载网络。输出为 `runtime_daemon/dist/coreman-runtime-{linux,darwin}-{amd64,arm64}.tar.gz` 及 SHA256；可通过 `RUNTIME_BUNDLE_DIR` 指向其他发布目录。产物不包含用户工作区、历史会话或 AI 凭据。

本地调试可从解压后的安装包目录运行：

```sh
.venv/bin/python -m runtime_daemon.daemon --config /absolute/path/config.json
```

管理端通过 PostgreSQL 暂存加密的请求/响应片段，支持多个 API 副本与独立 worker。Daemon 每 0.5 秒轮询任务、10 秒发送心跳；超过 45 秒无心跳视为离线。命令领取后不自动重放，避免网络故障导致重复执行。流式响应有序、限量并支持背压；断开消费者会取消任务，Go 驱动清理 CLI 进程组。

## 代码来源与能力

Go 驱动版本及来源快照记录在 `drivers/SOURCE.json`，原 MIT 许可证保留在 `drivers/LICENSE`，发布包随附许可文本。

- Claude Code / Codex：请求转换、流式输出、会话续接、取消、工具事件与用量。
- Agent：受限工作区内的 Git、技能和 MCP 操作、记忆同步、健康与额度探测。
- 会话查看：通过 CoreMan 登录鉴权及 Runtime 出站连接读取，会话正文经过转义。
- 邮件：通用 Agent 提供基本 IMAP 探测，不提供额外的邮件代理或 HTML 提取扩展。

## 验证范围

数据库注册/撤销/禁用、重复领取、加密流、响应去重、取消、目录逃逸和子进程终止均有测试。原 Go 驱动套件、前端套件及原 Agent 套件继续执行。端到端测试启动真实 Daemon、Go 二进制、API 与 PostgreSQL，AI CLI 使用隔离替身，不调用真实账户。

四种架构通过交叉构建；本机 macOS ARM64 通过 Daemon 联调。Linux、macOS 的真实系统服务自启及现有 chroot/nspawn 宿主托管仍需在实际目标机器验收，测试不会修改开发者机器的登录项或已有服务。
