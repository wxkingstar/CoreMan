# CoreMan Runtime Daemon

一个独立的系统用户环境安装一个 Daemon，可同时提供 Claude Code 与 Codex。Daemon 主动连接 CoreMan；目标机器无需开放入站端口。Claude 只保留 v1。

## 系统要求

- 操作系统：Linux 或 macOS，amd64 / arm64。**不支持 Windows**（包括原生 Windows；WSL2 内的 Linux 发行版按 Linux 处理，但未做系统服务验收）。
- 服务管理：Linux 需要用户级 systemd（`systemctl --user` 可用），或在无 init 的 chroot/nspawn 中由宿主机托管监督进程；macOS 使用当前用户的 launchd。
- Python 3.10+，且带 `venv`/`ensurepip`。Debian/Ubuntu 默认不带，需要先 `apt install python3-venv`，安装脚本会提前检查并提示。
- Git，以及需要使用的 Claude Code / Codex CLI（以运行用户登录）；安装 Skill 时还需要 Node.js/npm。
- 网络：目标环境能以 HTTPS 访问 CoreMan 地址（可经表单里的 CoreMan 连接代理）。

## 安装与接入

1. 在目标用户环境准备上述依赖并完成 CLI 登录。已有 chroot/nspawn 应先进入目标环境。
2. 管理员打开「运行时管理 → 安装运行时」，填写目标机器的项目主目录绝对路径，选择团队。链接固定 24 小时有效，默认并发上限为 10。可设置 AI 代理、CoreMan 连接代理、私有 CA、CLI 路径、环境和并发上限。目录必须可写，不能为 `/`，不能借符号链接跳出实际目录。
3. 复制生成的 curl 安装命令，在目标用户环境运行。命令注入 CoreMan 地址和一次性安装凭证；安装包包含本机 Go 驱动及离线 Python 依赖。
4. 首次注册自动纳管并启用。列表分别显示 Claude/Codex 是否安装、登录状态、模型、健康和额度；未安装或未登录不会被标成可用。后续安装/登录 CLI 后最多约一分钟重新发现。

链接默认只注册一个节点，可过期或撤销；相同节点的注册网络重试不会重复创建。链接仅生成时返回明文。运行身份保存于 `~/.local/share/coreman-runtime/config.json`（0600），不要复制到其他用户环境。节点只上报登录状态，不上传 CLI 登录文件。`$HOME` 含符号链接（如 `/home -> /data/home`）时，安装目录按实际路径解析。

安装脚本的顺序是：下载并校验安装包 → 解包到 `stage-*` 临时目录 → 在 `release-*` 目录建 venv → 注册系统服务 → **服务管理器接受后**才写入 `config.json` → 启动并等待上线。任何一步失败都会删除本次的 `release-*`、`ca.pem` 与 `config.json`，可以直接重新运行同一条安装命令；只有“服务已注册但 60 秒内未确认上线”会保留安装，此时修复后重启服务即可。

已安装 Codex 的 Daemon 后端继承后台 `codex` 模型目录中未退役的模型，新增模型无需更新本机驱动；旧节点在下一次心跳时自动应用。Claude 后端在心跳时同步目录中的原生 Claude 模型（`claude-` 或兼容的 `vllm/claude-` 前缀），过滤已退役模型，不自动纳入 MiniMax/Kimi 等第三方模型。驱动上报的模型用于发现和补充目录，不作为账号白名单。目录配置不代表账号已获该模型权限，实际调用由 CLI 校验。未安装对应 CLI 时模型列表为空；普通中继的模型限制规则不变。

首次 curl 下载使用调用者的 curl 网络环境；下载后 CoreMan 连接使用表单中显式配置的连接代理，默认直连。AI 请求代理独立配置。CoreMan 地址须可从目标环境访问，正式部署使用 HTTPS。

## 私有 CA

CoreMan 使用内部 CA 签发的证书时，在安装链接表单中粘贴 CA 证书（PEM）。安装脚本会：

- 用“系统信任链 + certifi（若可用）+ 该 CA”下载安装包；
- 把 CA 写到 `~/.local/share/coreman-runtime/ca.pem`（0600），并在 `config.json` 中记录 `"ca_file": "<该路径>"`。

Daemon 每次启动把随包 certifi 根证书、系统 CA 文件（OpenSSL 编译默认路径或常见发行版路径）和 `ca_file` 合并为 `trust.pem`（0600），控制面 httpx 客户端与 Agent 的 requests 会话都只用这一条信任链。代理环境变量、`SSL_CERT_FILE`、`REQUESTS_CA_BUNDLE` 仍然被忽略。

注意：

- 生成链接后的第一条 `curl … | sh` 由调用者的 curl 执行；目标机器系统信任链里没有该 CA 时，需要改为 `curl --cacert <ca.pem> -fsSL <链接> | sh`。
- 更换 CA：替换 `ca.pem` 内容（或把 `ca_file` 改为其他绝对路径）后重启服务。
- CA 文件缺失或不是 PEM 证书属于配置错误：Daemon 记录原因后退出且不会被自动重启（见下文“退出码”）。

## 常驻与运维

- Linux 有可用用户 systemd 时安装 `~/.config/systemd/user/coreman-runtime.service`。启动、停止、重启分别用 `systemctl --user start/stop/restart coreman-runtime`。无人登录时自启需要管理员开启对应用户的 linger；未开启会在后台提示。
- macOS 使用 `~/Library/LaunchAgents/org.coreman.runtime.plist`，依附当前用户的 launchd 域。登录会话的自动启动不等于无人登录开机启动。手工重启：`launchctl kickstart -k gui/$(id -u)/org.coreman.runtime`。
- 无用户服务管理器的 chroot/nspawn 使用监督进程。安装器输出 `~/.local/share/coreman-runtime/supervise.sh`；需要宿主机服务管理器托管该入口才能保证重启后启动。
- 后台「排空任务」停止接收新的 POST 任务，已有任务继续；「关闭启用」同时撤销节点接单能力并取消在途请求。排空结束后再停止服务或升级。
- Git/Skill/MCP、记忆同步和额度/健康探测沿用项目 Agent。Claude 额度 statusLine 探针是可选项；启用前备份原 settings，替换现有状态栏需由安装者主动选择。

### 退出码

| 退出码 | 含义 | 服务管理器行为 |
|---|---|---|
| 0 | 正常停止（SIGTERM/SIGINT） | 不重启 |
| 1 | 未预期异常，回溯写入 `runtime.log` | 10 秒后重启 |
| 78 | 重启无法解决的配置错误：安装链接无效/过期/已被使用（注册返回 4xx，408/429 除外）、私有 CA 不可用 | systemd `RestartPreventExitStatus=78` 与监督进程均停止重启 |

launchd 没有按退出码阻止重启的机制，因此 launchd 下同类错误以 0 退出，plist 使用 `KeepAlive = {SuccessfulExit = false}`，只重启异常退出。无论哪种服务管理器，原因都会写进 `runtime.log` 和 `state.json`（`"fatal": true, "error": …`）。

## 升级

使用发布包原地升级，节点身份、会话和日志保持不变：

```sh
# 1. 在后台对该节点「排空任务」，等在途任务结束。
# 2. 取得与本机平台一致的发布包及其 .sha256（runtime_daemon/dist/ 或 API 镜像中的发布目录）。
# 3. 用当前版本的 venv 执行：
DATA=~/.local/share/coreman-runtime
RELEASE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["release"])' "$DATA/config.json")
"$RELEASE/.venv/bin/python" -m runtime_daemon.install_service \
  --upgrade /path/to/coreman-runtime-linux-amd64.tar.gz   # 可加 --sha256 <hex>
```

执行过程：

1. 校验 SHA256（`--sha256`，缺省读取同目录 `<发布包>.sha256`），不匹配则不做任何改动。
2. 在服务仍运行时解包到 `stage-*`、在新的 `release-*` 目录建 venv，并自检（导入 Daemon 模块、执行两个驱动的 `--version`）。平台不匹配或包损坏会在此处中止，服务不受影响。
3. 停止服务 → 把 `config.json` 的 `release` 切到新目录并重写 unit/plist/`supervise.sh` → 启动 → 等待 120 秒内上线。
4. 未上线（含配置错误）时自动停止、切回旧 `release`、重启旧版本，并删除新目录；命令返回非 0 并说明原因。
5. 成功后保留上一个版本目录用于手工回退，删除更早的 `release-*` 与残留 `stage-*`。

同一时刻只允许一个安装/升级/卸载操作（`install.lock`）。chroot 中由宿主机托管 `supervise.sh` 时，先在宿主机停止托管再升级，完成后恢复托管。不要通过反复兑换安装链接升级同一个用户环境。

## 卸载

```sh
"$RELEASE/.venv/bin/python" -m runtime_daemon.install_service --uninstall          # 保留身份与数据
"$RELEASE/.venv/bin/python" -m runtime_daemon.install_service --uninstall --purge  # 全部删除
```

- 默认：停止并删除 systemd unit / launchd plist / `supervise.sh`，清理 socket 目录、`cli-bin`、`state.json`、`trust.pem` 以及非当前的 `release-*`；保留 `config.json`（节点身份，`service_status` 标记为 `uninstalled`）、当前版本、会话与日志。之后可用 `--upgrade <发布包>` 或 `python -m runtime_daemon.install_service --config <config.json>` 以同一身份重新注册服务。
- `--purge`：在上述基础上删除整个 `~/.local/share/coreman-runtime`（含身份、会话、日志、CA）。之后请在「运行时管理」中撤销该节点；重新接入需要新的安装链接。
- 卸载不会改动 CLI 登录；若启用过 Claude 额度探针，`~/.claude/settings.before-coreman-probe.json` 是启用前的备份，需要时手工恢复。

## 日志

| 文件（均在 `~/.local/share/coreman-runtime/`） | 内容 | 上限 |
|---|---|---|
| `runtime.log`、`runtime.log.1…5` | Daemon 主日志（INFO 及以上）；httpx/httpcore/urllib3 降到 WARNING，不再逐次记录轮询请求 | 10 MB × 5，按大小重命名轮转 |
| `claude.log`、`codex.log` 及 `.1…5` | Go 驱动 stdout/stderr | 10 MB × 5；Daemon 每 60 秒检查并 copy-truncate（驱动持有追加写句柄，轮转瞬间可能丢失少量行） |
| `service.log` 及 `.1…5` | launchd / 监督进程捕获的 stderr：只有 WARNING 以上与崩溃回溯 | Daemon 运行时同样按 10 MB × 5 copy-truncate |
| systemd journal | systemd 模式下的 stderr（WARNING 以上） | 由 journald 配额控制 |
| `state.json` | 最近连接状态；配置错误时含 `fatal` 与 `error` | — |

驱动日志包含请求诊断信息，采集或外发前应脱敏。

## 常见故障

- **安装失败后如何重试**：安装脚本已自动清理本次的 `release-*`、`stage-*`、`ca.pem` 与 `config.json`，修复提示的问题（缺 `python3-venv`、下载失败、`launchctl bootstrap` / `systemctl --user` 失败等）后直接重新运行同一条命令。链接已过期、被撤销或已被使用时需要重新生成。中途被强制终止（如 `kill -9`）留下的 `release-*`/`stage-*` 会在下一次安装时清理。
- **提示“此用户环境已有 Runtime”**：说明 `config.json` 存在。升级用 `--upgrade`；确实要换身份重装，先 `--uninstall --purge`，再用新链接安装。
- **服务反复停在 failed / launchd 不再拉起**：查看 `state.json` 的 `error` 或 `runtime.log`。退出码 78 表示配置错误，按提示处理（多为链接失效：`--uninstall --purge` 后重新生成链接安装；或修正 `ca_file`）后执行 `systemctl --user restart coreman-runtime` / `launchctl kickstart -k gui/$(id -u)/org.coreman.runtime`。
- **TLS 握手失败（`ConnectError`、证书校验失败）**：CoreMan 使用私有 CA 时确认 `config.json` 有 `ca_file` 且文件是 PEM 证书；重启服务使 `trust.pem` 重新生成。
- **socket 丢失 / 所有请求 `execution_failed`**：驱动 socket 位于 Linux 的 `$XDG_RUNTIME_DIR/coreman-<节点ID前8位>/`，否则位于安装目录下的 `run/`；仅当路径超过 Unix socket 长度上限（常见于很长的 macOS 用户目录）时才退回 `/tmp/coreman-<UID>-<节点ID前8位>/`。Daemon 每 60 秒发现一次：socket 文件或目录缺失、或连续 2 轮连接失败时，会重建目录并重启对应驱动（有在途请求时最多推迟 30 轮）。一般 1–3 分钟内自愈；若仍失败，查看 `claude.log`/`codex.log` 后重启服务。
- **服务已安装但未确认上线**：常见原因是网络/代理不可达或 CLI 探测较慢。查看 `runtime.log`，修复后重启服务，无需重新兑换链接。

## 发布与开发

CoreMan 数据库升级到 Alembic `0018_runtime_nodes`。API 镜像构建会生成四个安装包并包含安装脚本与会话查看模板。手工运行 API 时先构建：

```sh
python3 runtime_daemon/build.py
```

构建机需要 Go 1.24+、Python/pip 和依赖下载网络。输出为 `runtime_daemon/dist/coreman-runtime-{linux,darwin}-{amd64,arm64}.tar.gz` 及 SHA256；可通过 `RUNTIME_BUNDLE_DIR` 指向其他发布目录。产物不包含用户工作区、历史会话或 AI 凭据。

本地调试可从解压后的安装包目录运行（终端下日志同时输出到 stderr）：

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

数据库注册/撤销/禁用、重复领取、加密流、响应去重、取消、目录逃逸和子进程终止均有测试。部署相关的单元测试覆盖：私有 CA 信任链（本地 HTTPS 服务）、socket 目录选择与驱动自愈、日志轮转、注册 4xx 的退出码、服务注册失败回滚、升级切换与回滚、卸载保留/清除，以及在符号链接 `$HOME` 下真实运行 `install.sh`（替身服务注册，不触碰本机 launchd/systemd）。原 Go 驱动套件、前端套件及原 Agent 套件继续执行。端到端测试启动真实 Daemon、Go 二进制、API 与 PostgreSQL，AI CLI 使用隔离替身，不调用真实账户。

四种架构通过交叉构建；本机 macOS ARM64 通过 Daemon 联调。Linux、macOS 的真实系统服务自启、升级/卸载命令对真实 systemd/launchd 的调用，以及现有 chroot/nspawn 宿主托管仍需在实际目标机器验收，测试不会修改开发者机器的登录项或已有服务。
