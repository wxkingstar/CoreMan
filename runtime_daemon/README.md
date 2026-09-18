# CoreMan Runtime Daemon

一个独立的系统用户环境安装一个 Daemon，可同时提供 Claude Code 与 Codex。Daemon 主动连接 CoreMan；目标机器无需开放入站端口。

## 系统要求

- 操作系统：Linux 或 macOS，amd64 / arm64。**不支持 Windows**（包括原生 Windows；WSL2 内的 Linux 发行版按 Linux 处理，但未做系统服务验收）。
- 服务管理：Linux 需要用户级 systemd（`systemctl --user` 可用），或在无 init 的 chroot/nspawn 中由宿主机托管监督进程；macOS 使用当前用户的 launchd。
- Python 3.10+，且带 `venv`/`ensurepip`。Debian/Ubuntu 默认不带，需要先 `apt install python3-venv`，安装脚本会提前检查并提示。
- Git，以及需要使用的 Claude Code / Codex CLI（以运行用户登录）；安装 Skill 时还需要 Node.js/npm。
- 网络：目标环境能以 HTTPS 访问 CoreMan 地址（可经表单里的 CoreMan 连接代理）。

## 安装与接入

1. 在目标用户环境准备上述依赖并完成 CLI 登录。已有 chroot/nspawn 应先进入目标环境。
2. 管理员打开「运行时管理 → 安装运行时」，填写目标机器的项目主目录绝对路径，选择团队。链接固定 24 小时有效，默认并发上限为 10。可设置 AI 代理、CoreMan 连接代理、私有 CA、CLI 路径、环境和并发上限。目录必须可写，不能为 `/`，不能借符号链接跳出实际目录。
3. 复制生成的 curl 安装命令，在目标用户环境运行。命令注入 CoreMan 地址和一次性安装凭证；安装包包含本机 Go 驱动及离线 Python 依赖。目标环境已有 Runtime 时安装会停下并给出处理方式：一个系统用户只运行一个 Runtime；要换平台或换身份重装，把命令末尾的 `| sh` 换成 `| sh -s -- --replace`（见下文「替换已有 Runtime」）。
4. 首次注册自动纳管并启用。列表分别显示 Claude/Codex 是否安装、登录状态、模型、健康和额度；未安装或未登录不会被标成可用。后续安装/登录 CLI 后最多约一分钟重新发现。

链接默认只注册一个节点，可过期或撤销；相同节点的注册网络重试不会重复创建。链接仅生成时返回明文。运行身份保存于 `~/.local/share/coreman-runtime/config.json`（0600），不要复制到其他用户环境。节点只上报登录状态，不上传 CLI 登录文件。`$HOME` 含符号链接（如 `/home -> /data/home`）时，安装目录按实际路径解析。

安装脚本的顺序是：下载并校验安装包 → 解包到 `stage-*` 临时目录 → 在 `release-*` 目录建 venv → 注册系统服务 → **服务管理器接受后**才写入 `config.json` → 启动并等待上线。任何一步失败都会删除本次的 `release-*`、`ca.pem` 与 `config.json`，可以直接重新运行同一条安装命令；只有“服务已注册但 60 秒内未确认上线”会保留安装，此时修复后重启服务即可。

安装成功后，本机的管理命令固定在 `~/.local/share/coreman-runtime/bin/coreman-runtime`（升级、卸载、重新注册都用它，见下文）。该命令在任何目录下都能执行，切换版本时自动更新；`python -m runtime_daemon.install_service` 只在版本目录内可用，因为 `runtime_daemon` 不装进 venv。

### 替换已有 Runtime

改连其他 CoreMan、或要换一个新节点身份时，用同一条安装命令加 `--replace`：

```sh
curl -fsSL '<安装链接>' | sh -s -- --replace
```

顺序是：先下载并校验新安装包（这一步失败不动现有 Runtime）→ 停止并注销现有服务 → 把整个安装目录的内容移到同级的 `coreman-runtime.bak-<时间戳>`（节点身份、会话、日志、CA 都在里面，不删除）→ 按全新安装继续。安装失败时脚本会打印恢复旧 Runtime 的命令；确认新 Runtime 正常后可自行删除备份目录，并到原平台的「运行时管理」中撤销旧节点（卸载不会通知服务端）。

备份目录在停服务之前就先建好：安装目录单独挂载（例如 `~/.local/share` 只读、安装目录是 bind 进来的）时，旁边建不了备份或不在同一文件系统，`--replace` 会直接拒绝、不碰现有 Runtime，这时改用 `coreman-runtime --uninstall --purge` 后再正常安装。由宿主机托管 `supervise.sh` 的环境，与升级一样先在宿主机停止托管。

已安装 Codex 的 Daemon 后端继承后台 `codex` 模型目录中未退役的模型，新增模型无需更新本机驱动；旧节点在下一次心跳时自动应用。Claude 后端在心跳时同步目录中的原生 Claude 模型（`claude-` 开头；早期版本预置的 `vllm/claude-` 目录项也会被识别），过滤已退役模型，不自动纳入 MiniMax/Kimi 等第三方模型。驱动上报原生 Claude Code 模型名，调用时会去掉模型名中 `/` 之前的前缀再传给 CLI。驱动上报的模型用于发现和补充目录，不作为账号白名单。目录配置不代表账号已获该模型权限，实际调用由 CLI 校验。未安装对应 CLI 时模型列表为空。

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
- 后台「编辑」可改节点名称与所属团队（团队同时应用到该节点的 Claude Code、Codex 实例，已绑定的 AI 员工不受影响）；「删除」在没有 AI 员工绑定该节点、也没有员工正在迁入时移除节点及其实例，并让节点凭证失效。删除只清理管理端记录，主机上的 Daemon 需要按上面的卸载步骤自行卸载。
- Agent 的 Git 主机白名单是 `config.json` 中的 `git_hosts`（只写主机名，不含协议与路径；创建安装链接时可在高级设置中指定，默认 `["github.com"]`），修改后重启服务生效。节点在心跳中上报当前列表，在「运行时管理」展开节点即可查看；写成 URL 等永远匹配不到的条目会被标红。
- Git/Skill/MCP、记忆同步和额度/健康探测由安装包内的 Agent 负责。Codex 额度直接向 CLI 查询。Claude 额度 statusLine 探针是可选项，在安装链接中勾选，或在 `config.json` 设置 `install_claude_probe: true` 后重启服务。启用时先备份 settings，再把 statusLine 指向 `~/.cache/claude_rate_limits/capture.sh`；该脚本记下 Claude 传入的额度后，把同一份输入交给原状态栏命令，原状态栏照常显示。守护进程每 30 分钟启动一次短暂的 Claude 交互会话来刷新额度；Claude Code 只在订阅账号的 statusLine 输入中提供额度字段。未启用时不排 Claude 额度探测。

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
# 3. 执行本机管理命令：
~/.local/share/coreman-runtime/bin/coreman-runtime \
  --upgrade /path/to/coreman-runtime-linux-amd64.tar.gz   # 可加 --sha256 <hex>
```

没有 `bin/coreman-runtime` 的旧安装（在它引入之前装的），改成在版本目录下执行，发布包写绝对路径；升级一次后该命令就会出现：

```sh
DATA=~/.local/share/coreman-runtime
RELEASE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["release"])' "$DATA/config.json")
cd "$RELEASE" && .venv/bin/python -m runtime_daemon.install_service --upgrade <发布包的绝对路径>
```

执行过程：

1. 校验 SHA256（`--sha256`，缺省读取同目录 `<发布包>.sha256`），不匹配则不做任何改动。
2. 在服务仍运行时解包到 `stage-*`、在新的 `release-*` 目录建 venv，并自检（导入 Daemon 模块、执行两个驱动的 `--version`）。平台不匹配或包损坏会在此处中止，服务不受影响。
3. 停止服务 → 把 `config.json` 的 `release` 切到新目录并重写 unit/plist/`supervise.sh` → 启动 → 等待 120 秒内上线。
4. 未上线（含配置错误）时自动停止、切回旧 `release`、重启旧版本，并删除新目录；命令返回非 0 并说明原因。
5. 成功后保留上一个版本目录用于手工回退，删除更早的 `release-*` 与残留 `stage-*`。

同一时刻只允许一个安装/升级/卸载操作（`install.lock`）。chroot 中由宿主机托管 `supervise.sh` 时，先在宿主机停止托管再升级，完成后恢复托管。不要通过反复兑换安装链接升级同一个用户环境。

发布包与同名 `.sha256` 可以从 GitHub Release 附件下载，也可以从运行中的 API 容器复制，例如 `docker cp coreman-api-1-1:/app/runtime_daemon/dist/coreman-runtime-darwin-arm64.tar.gz .`。从源码运行 API 时，先执行 `python3 runtime_daemon/build.py`，发布包在 `runtime_daemon/dist/`。

### 旧版本首次升级

较早安装的节点，其安装工具还没有 `--upgrade` 参数，上面的命令会直接报参数错误。判断方法：当前版本目录下仍有 `relay_agent/`，或在版本目录下执行 `.venv/bin/python -m runtime_daemon.install_service --help`，输出里没有 `--upgrade`。

第一次升级改用新发布包里的安装工具，解释器仍用当前版本的 venv。该工具只依赖 Python 标准库，执行过程与上面相同，失败同样自动回滚：

```sh
DATA=~/.local/share/coreman-runtime
BUNDLE=/path/to/coreman-runtime-darwin-arm64.tar.gz   # 同目录需有 .sha256，或在下方加 --sha256 <hex>
RELEASE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["release"])' "$DATA/config.json")
TOOL=$(mktemp -d)
tar -xzf "$BUNDLE" -C "$TOOL" runtime_daemon
(cd "$TOOL" && PYTHONPATH="$TOOL" "$RELEASE/.venv/bin/python" -m runtime_daemon.install_service \
  --upgrade "$BUNDLE" --config "$DATA/config.json")
rm -rf "$TOOL"
```

升级成功后，管理台「运行时管理」中该节点会显示并发上限，服务端记录的节点协议版本为 2。之后的升级使用上面的常规命令即可。

旧版本遗留两类文件，确认升级成功后可按需清理：

- `claude.log`、`codex.log`：旧版驱动会把请求正文和系统提示词写进日志，可能含聊天内容。新版驱动默认不再记录，这两个旧文件建议删除或按敏感数据处理。
- `/tmp/coreman-<UID>-*/`：旧版的驱动 socket 目录。新版在 macOS 上改用安装目录下的 `run/`，旧目录可以删除。

## 卸载

```sh
MANAGE=~/.local/share/coreman-runtime/bin/coreman-runtime
$MANAGE --uninstall          # 停止并移除服务，保留身份与数据
$MANAGE --uninstall --purge  # 全部删除
$MANAGE --register           # 卸载服务后，按保留的身份重新注册
```

- 默认：停止并删除 systemd unit / launchd plist / `supervise.sh`，清理 socket 目录、`cli-bin`、`state.json`、`trust.pem` 以及非当前的 `release-*`；保留 `config.json`（节点身份，`service_status` 标记为 `uninstalled`）、当前版本、会话与日志。之后可用 `--register` 以同一身份重新注册服务，或直接 `--upgrade <发布包>`。
- `--purge`：在上述基础上删除整个 `~/.local/share/coreman-runtime`（含身份、会话、日志、CA）。之后请在「运行时管理」中删除该节点（需要先把绑定在它上面的 AI 员工切走）；重新接入需要新的安装链接。
- 卸载不会改动 CLI 登录与 Claude 设置。启用过 Claude 额度探针时，statusLine 仍指向 `capture.sh` 并继续转交原命令；要恢复原状态栏，把 `~/.cache/claude_rate_limits/statusline-original.json` 中的 `statusLine` 写回 `~/.claude/settings.json`。`~/.claude/settings.before-coreman-probe.json` 是首次启用前的完整备份。

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
- **提示“本机已安装 CoreMan Runtime”**：说明 `config.json` 存在。提示会列出现有 Runtime 与本次链接各自连接的平台，并按当前状态给出可直接复制的命令。升级用 `--upgrade`；换平台或换身份用 `| sh -s -- --replace` 重装（旧安装整体备份，不删除）；只是服务被卸载、身份还在，用 `--register` 恢复。链接只在注册成功后才算已使用，失败后可以直接重跑。
- **服务反复停在 failed / launchd 不再拉起**：查看 `state.json` 的 `error` 或 `runtime.log`。退出码 78 表示配置错误，按提示处理（多为链接失效：`--uninstall --purge` 后重新生成链接安装；或修正 `ca_file`）后执行 `systemctl --user restart coreman-runtime` / `launchctl kickstart -k gui/$(id -u)/org.coreman.runtime`。
- **TLS 握手失败（`ConnectError`、证书校验失败）**：CoreMan 使用私有 CA 时确认 `config.json` 有 `ca_file` 且文件是 PEM 证书；重启服务使 `trust.pem` 重新生成。
- **socket 丢失 / 所有请求 `execution_failed`**：驱动 socket 位于 Linux 的 `$XDG_RUNTIME_DIR/coreman-<节点ID前8位>/`，否则位于安装目录下的 `run/`；仅当路径超过 Unix socket 长度上限（常见于很长的 macOS 用户目录）时才退回 `/tmp/coreman-<UID>-<节点ID前8位>/`。Daemon 每 60 秒发现一次：socket 文件或目录缺失、或连续 2 轮连接失败时，会重建目录并重启对应驱动（有在途请求时最多推迟 30 轮）。一般 1–3 分钟内自愈；若仍失败，查看 `claude.log`/`codex.log` 后重启服务。
- **技能安装失败**：管理台「技能管理」直接显示节点返回的原因，`runtime.log` 同时记录 `Operation install-skill failed: <原因>`（只有 Agent 的固定提示，不含命令输出与凭证）。新版节点安装技能不检查 Git 白名单；较旧的节点在技能仓库域名不在 `config.json` 的 `git_hosts` 中时报「Git 来源不在白名单内」，升级节点，或把主机名（如 `git.example.com`，不含协议与路径）加入该列表后重启服务。更早的节点只会报“未返回具体原因”，升级后才能看到节点侧原因。
- **服务已安装但未确认上线**：常见原因是网络/代理不可达或 CLI 探测较慢。查看 `runtime.log`，修复后重启服务，无需重新兑换链接。

## 发布与开发

运行时节点要求 CoreMan 数据库已迁移到最新版本（`alembic upgrade head`，`deploy/coreman up` / `upgrade` 会自动执行）。API 镜像构建会生成四个安装包并包含安装脚本与会话查看模板。手工运行 API 时先构建：

```sh
python3 runtime_daemon/build.py
```

构建机需要 Go 1.24+、Python/pip 和依赖下载网络。输出为 `runtime_daemon/dist/coreman-runtime-{linux,darwin}-{amd64,arm64}.tar.gz` 及 SHA256；可通过 `RUNTIME_BUNDLE_DIR` 指向其他发布目录。产物不包含用户工作区、历史会话或 AI 凭据。

本地调试可从解压后的安装包目录运行（终端下日志同时输出到 stderr）：

```sh
.venv/bin/python -m runtime_daemon.daemon --config /absolute/path/config.json
```

管理端通过 PostgreSQL 暂存加密的请求/响应片段，支持多个 API 副本与独立 worker。Daemon 每 0.5 秒轮询任务、10 秒发送心跳；超过 45 秒无心跳视为离线。命令领取后不自动重放，避免网络故障导致重复执行。流式响应有序、限量并支持背压；断开消费者会取消任务，Go 驱动清理 CLI 进程组。

## 组件与许可

Go 驱动基于开源项目 clawrelay-api 修改，上游快照记录在 `drivers/SOURCE.json`，上游 MIT 许可证保留在 `drivers/LICENSE`，发布包随附该许可文本。详见 [第三方声明](../THIRD_PARTY_NOTICES.md)。

- Claude Code / Codex：请求转换、流式输出、会话续接、取消、工具事件与用量。
- Agent：受限工作区内的 Git、技能和 MCP 操作、记忆同步、健康与额度探测。
- 会话查看：通过 CoreMan 登录鉴权及 Runtime 出站连接读取，会话正文经过转义。
- 邮件：通用 Agent 提供基本 IMAP 探测，不提供额外的邮件代理或 HTML 提取扩展。

## 验证范围

数据库注册/撤销/禁用、重复领取、加密流、响应去重、取消、目录逃逸和子进程终止均有测试。部署相关的单元测试覆盖：私有 CA 信任链（本地 HTTPS 服务）、socket 目录选择与驱动自愈、日志轮转、注册 4xx 的退出码、服务注册失败回滚、升级切换与回滚、卸载保留/清除，以及在符号链接 `$HOME` 下真实运行 `install.sh`（替身服务注册，不触碰本机 launchd/systemd）。Go 驱动、前端与 Agent 各自的测试套件同样纳入持续集成。端到端测试启动真实 Daemon、Go 二进制、API 与 PostgreSQL，AI CLI 使用隔离替身，不调用真实账户。

四种架构通过交叉构建；本机 macOS ARM64 通过 Daemon 联调。Linux、macOS 的真实系统服务自启、升级/卸载命令对真实 systemd/launchd 的调用，以及 chroot/nspawn 宿主托管，请在实际目标机器上验证；测试不会修改开发者机器的登录项或已有服务。
