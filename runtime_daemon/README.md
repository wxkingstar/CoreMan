# CoreMan Runtime Daemon

一个独立的系统用户环境安装一个 Daemon，可同时提供 Claude Code 与 Codex。Daemon 主动连接 CoreMan；目标机器无需开放入站端口。

驱动与模型健康、额度观测时间及升级顺序见 [Runtime observations](../docs/runtime-observations.md)。优先复用真实会话；Claude 空闲15分钟兜底，Codex/OpenCode 保留本地时间9–23点每小时兜底，不增加夜间探测。

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
- 出站代理只在启动时确定一次：`config.json` 的 `proxy` 非空时覆盖 `HTTP(S)_PROXY` 与小写的两个变量；留空则沿用服务环境自带的代理变量。驱动与 Claude / Codex CLI 都继承这份环境，中间不做增删，因此改代理必须重启服务。节点在心跳中上报生效的代理与来源，在「运行时管理」展开节点即可查看：来自 CoreMan 配置、继承自服务环境，或两处都没有（标为未生效，AI 请求将直连）。`config.json` 改过但未重启时额外提示待生效。注意用户服务不读 `~/.profile`、`~/.bashrc`：systemd 用户服务的环境来自 `/etc/environment`、`~/.config/environment.d/*.conf` 或服务的 `EnvironmentFile`，launchd 则用 plist 的 `EnvironmentVariables`。
- Git/Skill/MCP、记忆同步和额度/健康探测由安装包内的 Agent 负责。额度以对话带回为主：Claude 驱动读每次请求的 `rate_limit_event`，Codex 驱动在每轮结束后读该线程 rollout（`$CODEX_HOME/sessions/.../rollout-*-<thread>.jsonl`）里本轮最后一条 `token_count`，都换算成 5 小时与 7 天窗口的使用率，附在最后的 usage chunk 的 `x_rate_limits` 上，worker 在对话收尾时写入实例。没有对话的实例靠 Agent 兜底：Codex 每小时调用 `codex app-server` 的 `account/rateLimits/read`，不消耗模型额度；Claude 全天每 15 分钟用驱动默认模型做一次健康检查（CLI 返回错误结果时记为 down），检查完读驱动 `/v1/stats` 的 `rate_limits` 上报（有新数据才上报），15 分钟内已有对话带回额度时跳过这次检查。Claude 只有订阅账号有这项数据。

### 退出码

| 退出码 | 含义 | 服务管理器行为 |
|---|---|---|
| 0 | 正常停止（SIGTERM/SIGINT） | 不重启 |
| 1 | 未预期异常，回溯写入 `runtime.log` | 10 秒后重启 |
| 78 | 重启无法解决的配置错误：安装链接无效/过期/已被使用（注册返回 4xx，408/429 除外）、私有 CA 不可用 | systemd `RestartPreventExitStatus=78` 与监督进程均停止重启 |

launchd 没有按退出码阻止重启的机制，因此 launchd 下同类错误以 0 退出，plist 使用 `KeepAlive = {SuccessfulExit = false}`，只重启异常退出。无论哪种服务管理器，原因都会写进 `runtime.log` 和 `state.json`（`"fatal": true, "error": …`）。

## 个人助理的沙箱

个人助理（见 `docs/feishu.md`「个人助理应用」）的每一轮都在 bubblewrap 沙箱里跑：守护进程为这一轮另起一个驱动，只挂进它自己的工作目录（沙箱里是 `/work`）、本轮新建的家目录、临时目录、CLI 配置目录，只读挂系统目录、CLI 程序与本安装的发布目录；网络在单独的命名空间里，唯一的出路是经守护进程的放行代理连平台地址与模型接口。普通员工的轮次不经过沙箱，与以前完全相同。

**只有助理专用实例跑助理。** 守护进程的 `config.json` 里 `assistant_only` 为 `true`（安装时加 `--assistant-only` 写入）才是助理专用实例。只有它在自检通过后声明沙箱能力（心跳里的 `assistant_sandbox_v1`），平台也只把助理的轮次派给声明了的节点；不是助理专用实例的守护进程不跑自检、从不声明能力，收到带沙箱标记的请求一律拒绝（503，`sandbox_unavailable`），它处理普通轮次的路径与以前相同。助理专用实例反过来只做这些：

- 对话请求必须带 `sandbox: true`，在沙箱里跑；不带的（包括 `sandbox` 不是 `true` 的）在动任何东西之前拒绝，连工作目录都不建；
- 节点命令只接平台对助理所在节点会发的几种：`ping`、`status`、`health-check`、`probe-rate-limits`、席位的五个操作（`seat-create`、`seat-login-start`、`seat-login-complete`、`seat-activate`、`seat-remove`），开通和换运行时用到的 `workspace-info`、`workspace-probe`、`workspace-init`，以及停用或换走之后删目录的 `workspace-remove`；其余命令（装技能、MCP 登录、工作目录的文件读写与 git 查看、导出导入与克隆、会话迁移、记忆的读取与下发、`pull` / `pr` / `init` 等）一律拒绝；
- 经驱动的读请求只留 `GET /health` 与 `GET /v1/models`，会话列表、会话事件、用量统计都拒绝；
- 不跑遍历所有工作目录的定时任务（回收记忆、刷新开发模式的 worktree）；节点自己的周期模型探测照常跑，它负责刷新这份登录（见下面「登录凭证」）。

拒绝的请求回 `403`，错误代码 `assistant_only`，`runtime.log` 里记一行 `Request refused on an assistant-only node`。

**工作目录。** 助理在沙箱里能写自己的工作目录，守护进程在沙箱外碰它时都按「里面的东西可能被换掉」处理：读所有权标记（`.coreman-workspace.json`）不跟随链接、不在 FIFO 或设备上等待、只读普通文件且最多 64 KB；`workspace-init` 只在全新的目录上做，目录已带本助理的标记就原样返回（不读里面的任何文件），没有标记的非空目录一律拒绝。工作目录根下有三个守护进程自己用的目录，不能当作助理的工作目录，落在它们里面的请求以 `reserved_directory` 拒绝：`.coreman-health`（节点自己的健康探测目录）、`.coreman-trash`（回收站）、`.coreman-owners`（归属登记）。回收站与归属登记目录由守护进程保持为 0700。

归属登记：初始化一个全新的目录时，守护进程建好目录就马上在 `.coreman-owners/` 下为它写一条记录（员工编号与目录的 inode），然后才往目录里写东西，所以初始化中途崩了留下的目录也认得出；助理在沙箱里改不到这条记录。只记 inode、不比设备号：工作目录根可以放在几台节点共同挂载的网络存储上，网络挂载的设备号换一台机器、重启或重新挂载都可能变。`workspace-remove` 据此认归属，平台见到「已删除」或「不是这位助理的」就把要删的登记划掉、不再来，所以这两种回答只在能确认时给：

- 登记记着本助理、目录在且 inode 对得上：删，答「已删除」。没有登记的目录节点从不删：目录里的标记助理在沙箱里写得到，不凭它删；
- 登记记着本助理、目录确知已不在（运维手工删了，或上次改名进回收站之后没来得及写墓碑）：答「已删除」；
- 登记记着别的助理：答「不是这位助理的」，什么都不动；
- 删完之后登记换成墓碑（`{"bot_id": …, "removed": true}`），回执丢了平台再来时据此答「已删除」，那个路径上后来出现什么都不碰。墓碑由助理侧的清扫在 30 天后删掉；写登记中途崩了会在 `.coreman-owners/` 里留下 `.tmp-*` 临时文件，它们不算登记、无害，清扫同样在 30 天后删掉（只删普通文件、不跟随链接）。`.coreman-owners/` 里别的东西清扫从不动；
- 其余情况一律答「无法确认」（`unverified`），什么都不动：登记读不懂或被改坏；登记记着本助理但 inode 对不上（目录被人手工还原或复制过）；没有登记（不管目录里的标记写着谁）；目录不在也没有登记（例如共享存储没挂上，这时也不会在挂载点上建 `.coreman-owners` 或回收站）；存储报错——看目录或读登记时报的不是「不存在」而是别的错（共享存储掉线、`ESTALE`、`EIO`、没权限等），这时说不清目录在不在、登记记着谁，不会当成「已删除」。平台留着这条要删的登记，按退避（最长 6 小时）一直重试，登记的错误代码是 `dir_unverified`。

遇到 `dir_unverified`：共享存储没挂上或在报错的，修好之后下一次重试就会正常删掉；其余情况由运维手工核对这个目录确实是那位已停用助理的之后，直接手工删掉目录。删掉之后：`.coreman-owners` 里这个路径的登记还记着这位助理的（inode 对不上也算），下一次重试就答「已删除」并写墓碑，平台自己划掉登记，不用再做什么；登记已经坏了或不在的，节点会一直答「无法确认」，要直接在数据库里删掉平台那条要删的登记（`assistant_retired_dirs` 表里这位助理、这个节点、这个目录的那一行；没有管理页面可以删）。`.coreman-owners/` 必须和助理的工作目录在同一份存储上（就放在工作目录根下，不要把某个助理的目录单独挂载到别的存储上）：否则那份存储没挂上时登记还在、目录却不在，会被当成「已删除」。

`workspace-remove` 先原子地把目录改名进回收站，再在后台线程里不跟随任何链接地删掉；还有沙箱里的轮次用着这个目录、它下面的目录或它的上级目录时不删（`busy`），平台稍后再来。轮次准入（登记本轮的工作目录并核对它还在原处，不在就以 `workdir_missing` 拒绝这一轮）与删除（核对有没有轮次在用并改名）在同一把锁里做，改名进回收站之后不会再有轮次挂上这棵树。删除线程没做完就退出的，由助理侧的清扫（启动时与之后每 10 分钟）接着删。

**改回普通实例。** 把助理专用实例改回普通实例（去掉 `assistant_only`）后，守护进程在启动时、向平台注册之前（所以即使这台节点之后再也注册不上，也会清）在后台线程里做一次清理：删掉数据目录下的 `assistant-turns/`（可能有没传上去的明文转录），并删掉工作目录根下 `.coreman-trash/` 里名字是 32 位小写十六进制的条目。不动 `.coreman-trash` 这个目录本身；它带所有权标记（说明它是某人的工作目录）时整个跳过，它是链接时也不碰。留下的只有 `.coreman-owners/`（里面只有员工编号与 inode 号或墓碑，没有内容）和回收站里名字不是 32 位十六进制的东西，需要时手工删。

**跑在哪里。** 规则只有一条，助理池与所有者的私有运行时一样：**跑助理的节点上没有普通员工，与它同一台机器、同一个 uid 的其他节点上也没有**。助理与普通员工可以共用机器，但不共用系统用户：要跑助理的机器上，以一个专用的系统用户（下面用 `coreman-assistant` 举例）再装一份助理专用实例，注册成单独的节点；普通员工照旧由原来那份守护进程、原来的用户运行，不用改动。所有者想用自己的机器跑自己的助理，同样要在那台机器上装一份助理专用实例，把它作为自己的私有运行时绑给助理。平台据心跳上报的本机指纹与 uid 在三处检查：

- 助理的每一轮开轮前：节点要上报指纹与 uid、uid 不是 0，节点上与同机同 uid 的节点上都没有普通员工（不论是否启用，也包括正在迁往它们的）；不满足时这一轮不跑，所有者在私聊里收到「你的个人助理暂停了：绑定的运行时没有与普通员工隔开（要用专为个人助理安装的运行时），请联系管理员。」，里面没有节点或主机的信息；
- 助理换运行时：目标节点同样要满足这一条；
- 普通员工绑定或换运行时：目标节点不能是助理池节点、不能绑着或正在迁来个人助理、不能声明了沙箱能力，与这样的节点同机同 uid 也不行。

加入助理池另有要求：节点不能是 root（uid 0）、必须上报指纹与 uid、必须已声明沙箱能力，同机同 uid 的其他节点也必须都是助理池节点且没有普通员工。

> **部署前提（平台查不到，必须由部署方保证）**
>
> 1. **运行普通员工的系统用户不能有 root 权限，也不能有免密 sudo。** 否则它能读助理用户的一切，这层隔离不成立。平台与自检都没有办法核对这一条。
> 2. **助理机器上以 `hidepid=2` 挂载 `/proc`**（`mount -o remount,hidepid=2 /proc`，并写进 `/etc/fstab`）。进程命令行对本机所有用户可见：CLI 的命令行里带着系统提示词，默认情况下普通员工的用户读得到 `/proc/<pid>/cmdline`。
> 3. **克隆出来的机器（虚拟机镜像、模板）要重新生成机器编号**（`/etc/machine-id`）。平台靠它的哈希判断「是不是同一台机器」；几台机器编号相同会被当成同一台，只会多拒、不会少拒，但准入会莫名其妙地失败。读不到机器编号的节点进不了助理池。

**安装第二份守护进程。**

1. 建用户：`useradd -m -u <uid> coreman-assistant`，`chmod 700 /home/coreman-assistant`。工作目录在网络存储上、多台机器共享时，每台机器用同一个 `<uid>`。
2. 以该用户登录一次 Claude / Codex（这是一份独立的登录，与普通员工那份分开计额度、分开刷新）。systemd 机器上给它开 linger：`loginctl enable-linger coreman-assistant`。
3. 在管理台生成一条新的安装链接（项目主目录写这个用户下的目录，例如 `/home/coreman-assistant/projects`），以该用户运行安装命令，末尾加 `--assistant-only --expect-uid=<uid>`：`curl -fsSL '<安装链接>' | sh -s -- --assistant-only --expect-uid=<uid>`（可与 `--replace` 同用；工作目录在共享存储上时 `--expect-uid` 必加）。当前用户的 uid 不符时安装脚本什么都不做就退出。`--assistant-only` 只是在写出的 `config.json` 里加一行 `"assistant_only": true`；用 `--replace` 重装时要再带上它，`--upgrade` 原地升级保留原来的配置。服务名同样是 `coreman-runtime`，挂在该用户自己的 systemd 用户会话（或 launchd）下，与普通员工那份互不相干；守护进程只出不进，没有端口要分配。
4. 等 `runtime.log` 里出现 `Assistant sandbox: claude=ready …`，再在管理台把这个节点的实例加入助理池（`PUT /api/admin/relay-servers/{id}/assistant-pool`，请求体 `{"enabled": true}`；不满足上面的条件会被拒绝并说明原因）。

**节点要求。** Linux；bubblewrap；允许非特权用户命名空间（有的发行版要打开 `kernel.unprivileged_userns_clone`，有的发行版用 AppArmor 限制非特权用户命名空间，要给 `bwrap` 放行）。守护进程在容器里运行、容器又不给用户命名空间时，这台节点跑不了助理。macOS 节点跑不了助理。助理池实例不能是私有运行时。

**自检与能力。** 只有助理专用实例做自检：启动时、之后每 30 分钟，先核对家目录、本安装的数据目录、项目主目录、本轮目录与 socket 目录都属于本用户且组与其他人没有任何权限（0700）；再按前置验证脚本的同一套检查真起几次沙箱：看不见员工工作目录、本安装的配置与登录目录（这次探测把两种 CLI 的只读目录都挂上，与真实一轮的挂载相同），只读挂载点带只读标志，网络命名空间里只有 `lo` 且直连外网立即以 `ENETUNREACH` 失败（超时、连通或其他错误都算没隔离），经转发口连得到守护进程，沙箱里的 umask 是 077；最后在沙箱里跑一遍 `claude --version` / `codex --version`。结果按 AI 上报到心跳的 `assistant_sandbox_v1`；`runtime.log` 里在结果变化时写一行 `Assistant sandbox: claude=ready, codex=no (ok)`，括号里是失败的原因代码，通过时为 `ok`。没通过的节点（或没通过的那种 AI）不声明能力，平台不会把助理的轮次派给它；万一收到，守护进程回 503 与原因代码，什么都不跑。

| 原因代码 | 含义与处理 |
|---|---|
| `unsupported_platform` | 不是 Linux |
| `bwrap_missing` | 没装 bubblewrap |
| `layout_failed` | 本轮目录或自检用的临时目录建不出来（磁盘满、权限不对） |
| `private_dir_check_failed` | 家目录、数据目录、项目主目录、本轮目录或 socket 目录不属本用户，或组 / 其他人有权限：`chmod 700` 并 `chown` |
| `hidden_check_failed` | 该隐藏的路径在沙箱里看得到，或宿主上读不到这个路径本身（`stat` 失败）：查员工工作目录、配置、登录目录是否被只读挂载根带了进来 |
| `unsafe_mount` | 只读挂载根（发布目录、Python 前缀、CLI 目录、`assistant_sandbox_ro_paths`）等于或包含本轮目录、socket 目录、工作目录或项目主目录，或者落在本轮目录、socket 目录、项目主目录里面：这些目录会被带进沙箱，调整 CLI 的安装位置或去掉那条额外路径 |
| `sandbox_start_failed` | 沙箱起不来：多半是不允许非特权用户命名空间，或 bubblewrap 版本太旧 |
| `probe_failed` | 沙箱里的检查脚本没有给出结果 |
| `readonly_check_failed` | 只读挂载点没有只读标志（检查读挂载标志，不靠「写失败」判断，也要求至少核对到一个挂载点） |
| `offline_check_failed` | 网络命名空间没隔离干净（有 `lo` 之外的网卡，或直连没有立即以 `ENETUNREACH` 失败） |
| `bridge_check_failed` | 沙箱里经转发口连不到守护进程这一侧 |
| `umask_check_failed` | 沙箱里的 umask 设不成 077 |
| `cli_unavailable` | 沙箱本身没问题，但两种 CLI 都没有在沙箱里跑通 `--version`（没装，或装在不常见的位置：用 `assistant_sandbox_ro_paths` 补上） |

某一种 AI 单独失败（比如只装了 Claude）时没有整体原因代码，那一种的能力为 `no`，另一种照常声明。

**登录凭证。** 每一轮从当前席位的登录文件收窄出一份副本只读挂进去，副本按字段白名单构造，白名单外的字段（包括 CLI 以后新增的）一律不带：Claude 只留 `claudeAiOauth` 里的 `accessToken`、`expiresAt`、`scopes`、`subscriptionType`、`rateLimitTier`；Codex 只留 `auth_mode`、`last_refresh`，以及 `tokens` 里的 `id_token`、`access_token`、`account_id`，`refresh_token` 留一个空串（真机实测：Codex 读不到这个字段就不认这份登录，请求一律 401；空串则正常），`last_refresh` 改成现在。副本里没有刷新令牌的值，节点原来的登录文件不动；助理专用实例上没有普通轮次，令牌刷新只发生在这份守护进程沙箱外、节点自己的周期模型探测里（Claude 空闲时每 15 分钟一次，其他 AI 白天每小时一次；它和管理台的「运行环境检查」不是一回事，后者对助理关闭）。访问令牌剩余不到 30 分钟（或到期时间读不懂、不是有限的数）时这一轮不跑（`credential_expiring` / `credential_unreadable`），等下一次探测刷新。

> **长效凭证没有到期门槛。** 用环境变量登录（`CLAUDE_CODE_OAUTH_TOKEN`、`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`）的节点，以及 Codex 用 API Key 文件登录的节点，带进沙箱的是一枚**长期有效**的凭证，不做到期检查，被提示词注入的助理读得到它；这一路的保护只有出网放行名单（见下）。要避免，让助理节点用 OAuth 订阅登录，不要用长效令牌或 API Key。OAuth 登录时被注入的助理读到的只是一枚到期即失效的访问令牌。

**出网。** 沙箱里没有网卡，唯一的出路是本轮专用的放行代理（一个 Unix socket，守护进程这一侧），它只接 `CONNECT`，只放行名单里的「主机:端口」：

- 平台地址是控制面地址，加上一组写死的环境变量键的取值——这几个值由平台写进请求：系统目录、飞书个人工具、飞书文档、企微个人工具、定时任务、凭证、云代理的地址（`runtime_daemon/egress.py` 的 `PLATFORM_URL_KEYS`）；请求里别的任何值都不进名单；名单里的地址必须是 https；
- 地址字面量（IPv4、IPv6）一律不收；主机名在直连时解析到回环或链路本地地址的拒绝（元数据服务、本机服务连不上）；
- 模型接口只放行推理所需的主机（Claude：`api.anthropic.com`；Codex：`chatgpt.com`、`api.openai.com`），登录令牌的刷新地址不在名单里；可用 `assistant_egress_hosts` 覆盖；
- 每一轮同时最多 64 条连接，已建立的隧道 300 秒两个方向都没有字节就关掉，一轮结束时拆掉全部隧道；
- 日志只记放行与拒绝的次数，不记主机名。

> **已知限制：代理只按主机名放行、不解 TLS、不看内容。** 被提示词注入的助理仍可能用它自己带来的密钥，把数据传到模型厂商那一侧它能控制的存储里，或利用共享 CDN 做域名前置；这一期不防。

**转录。** 一轮结束后守护进程把 CLI 转录打包上传平台（经 TLS，平台用所有者的数据密钥加密保存），然后删掉本轮目录；续聊前再从平台取回。平台凭派出这一轮时给的交接编号收转录：上一轮的转录没传回来时，下一轮不拿旧的续，开新会话。

- 打包从不跟随符号链接：逐级用 `O_NOFOLLOW` 从目录描述符打开，只收普通文件；边遍历边计数，目录项超过 20000 立即放弃（不先把整个目录列出来）；压缩后超过 50MB 或目录项超过上限都不上传，下一轮开新会话；
- 还原时只接受普通文件与目录、落在固定的几个位置之内；文件以只有本用户可读写的权限用 `O_EXCL` 新建，不覆盖已有文件、不跟随链接；成员数最多 20000，解压后总量最多 512MB；任何一项不合格整个包作废、已写出的内容清掉，这一轮从新会话开始；
- 上传失败的本轮目录在 `~/.local/share/coreman-runtime/assistant-turns/` 里留到下一次清扫（守护进程启动时一次，之后每 10 分钟）：清扫再传一次，然后无论成败都删除；一次上传或下载整体最多 180 秒，回复的 `[DONE]` 因此最多被扣住这么久；
- 沙箱里驱动的输出写在本轮目录里，随本轮删除，不进 `claude.log` / `codex.log`；回复里 `[DONE]` 之后的内容最多扣住 64KiB，多出的丢掉，这一轮照常结束；
- 一轮被取消时，转录放到后台去传，传完再删；
- 守护进程停止时，普通驱动的终止与助理侧的收尾同时进行：助理侧最多等 10 秒，到时还在传的转录取消、留给下次启动的清扫，正在做的收尾（停沙箱、删目录）不取消；systemd 单元 30 秒后强制结束服务；
- 本轮的工作目录必须在员工工作目录根（`workspace_root`）之内、且不能就是根本身，否则这一轮按请求不合规失败；
- 收尾顺序：正常结束时先停沙箱、再删凭证副本，然后打包上传转录；被拒绝、出错或被取消的轮次先删凭证副本、再停沙箱。之后关放行代理，最后删 socket 目录与（转录处理完的）本轮目录；被拒绝的轮次先同步删掉 `meta.json`，收尾即使被打断，清扫也不会把它当成转录上传；
- 删这两棵目录时，沙箱改成 `000` 的子目录先改回 `0700` 再删（不跟随链接），不会每轮漏下一点磁盘。

**不支持助理的几处。** 助理不做管理台的运行环境检查，也不跑定时任务；装技能、MCP 登录、工作目录的文件与 git 管理、规则文档备份、告警来源、审批人设置都对助理关闭（助理专用实例在节点这一侧同样拒绝对应的命令）。发往助理池运行时、却不带沙箱标记的对话请求，平台在发出之前就拒绝。助理换运行时只落到目标上一个全新的空目录：不复制工作目录、不迁会话与记忆，换完后的第一轮带上平台保存的上下文说明；目标目录已存在就拒绝，所以助理回不到一个旧目录还在的节点。

**平台侧的几个行为（节点运维可能看到的）。**

- 助理的调用排在普通调用之后领取，只领这个节点声明了沙箱能力的那种 AI 的调用，且不超过设置 `assistant_node_concurrency`（默认 2，取值 1–32，读不到或写坏了按 2）；超出的排队，等不到时所有者看到「运行时繁忙」。
- 转录接口（`/api/runtime/assistant-transcripts/{编号}`）同时最多处理 2 个传输（平台进程里每个传输大约占转录大小的三到四倍内存：请求体、明文与密文；50MB 的转录约 200MB），等位置最多 60 秒（`503`）；读上传请求体总时限 300 秒（`408`）；超过 50MB 回 `413`；状态头 `X-Transcript-Status` 不是 `ok` / `empty` / `too_large` / `unchanged` 之一，或内容与状态不符，回 `422`；下载找不到、不是这个节点的、读不出来都回同一个 `404`；上传时编号已失效、已办完、不是这个节点的、存不下都回同一个 `410`，节点不必再传。晚到的或重复的上传同样得到 `410`，不会改动已有转录。
- 平台的 scheduler 每个周期清一次过期转录：更新时间早于「会话过期时间（`session_ttl_hours`，默认 72 小时）加 24 小时」的行，每批最多 200 行、每次最多 10 批，被上传占着的行跳过不等。
- 沙箱里的进程能伪造流里的额度、运行时与报错帧：助理这一轮流里的这些内容不拿来改实例的额度与健康记录。
- 排队等满仍没有节点领取、且撤销时调用确定还在排队的一轮，交接编号作废，下一轮照旧续上一份转录；其余在派出前后结束的轮次（媒体失败、中途停止、节点可能已领到）不作废，下一轮开新会话。
- 助理池的开关关掉时不迁移也不拒绝：已绑定的助理原地暂停并告知所有者，直到管理员改绑（这是现行行为，是否应在仍有助理绑定时拒绝关闭尚待定）。节点离线、排空、不能隔离运行时同样暂停，不换到别的节点；暂停期间每条消息各回一次暂停提示，不节流。

**config.json 里的可选键**（改完重启服务）：

| 键 | 说明 |
|---|---|
| `assistant_only` | `true` 时是助理专用实例（见上面「只有助理专用实例跑助理」）；安装时 `--assistant-only` 写入 |
| `assistant_max_concurrent` | 本节点同时跑的助理轮次上限，默认 8；只是保险，平时由平台设置「每个节点的助理并发」（默认 2）控制，要比它大 |
| `assistant_egress_hosts` | 按 AI 覆盖模型接口地址，如 `{"codex": ["chatgpt.com", "api.openai.com"]}`；只写主机名，端口固定 443；地址字面量会被忽略 |
| `assistant_sandbox_ro_paths` | 额外只读挂进沙箱的绝对路径（CLI 装在不常见的位置、自检报 `cli_unavailable` 时用）；不得等于或包含本轮目录、socket 目录、工作目录或项目主目录，也不得落在本轮目录、socket 目录、项目主目录里面（`unsafe_mount`） |
| `network_zone` | 局域网直连换机的网络区名，如 `office`。两台节点的网络区相同、都配了 `direct_address`，换运行时由目标节点直接从源节点拉取工作目录压缩包，不经平台中转；不配则照旧中转 |
| `direct_address` | 本机供同一网络区其他节点访问的 `主机:端口`（局域网 IP 或主机名，端口 1024–65535，如 `192.0.2.20:47801`）。守护进程在该端口所有网卡上开 HTTPS 下载口，只在换机期间凭平台签发的一次性令牌提供压缩包；证书在首次使用时用 `openssl` 自签生成（数据目录下 `direct-cert.pem` / `direct-key.pem`），指纹随 ping 报给平台，拉取方核对指纹后才发令牌。macOS 要在防火墙里放行 coreman-runtime；IP 会变时用固定 IP 或主机名。直连时上限放宽到 20 万个文件或 32G；拉不通且目录在 5 万个文件或 4G 之内时自动退回中转 |

**资源上限（建议）。** 沙箱本身不限内存与进程数，可写目录也不限大小。建议给助理专用实例的服务加一个 systemd drop-in（`systemctl --user edit coreman-runtime`），按机器大小取值，例如：

```ini
[Service]
MemoryMax=8G
TasksMax=2048
```

（用户会话下的单元要内核与 systemd 开了 cgroup 委派才生效；守护进程本身与它起的沙箱都算在内。）

**升级与回滚（节点这一侧）。** 先升平台、再升节点，顺序反过来也安全：新平台只把助理的轮次派给声明了 `assistant_sandbox_v1` 的节点，旧节点不声明，绑在它上面的助理暂停；旧平台不发沙箱标记，普通实例照旧处理普通轮次。

- **一台机器上的所有守护进程都升到本版本，再把其中的节点加入助理池或绑给助理。** 旧守护进程既不报本机指纹也不报 uid，平台看不出它与助理节点同机同用户。
- **在引入「助理专用实例」之前装的助理节点，升级后要补上 `assistant_only`：** 在它的 `config.json` 里加 `"assistant_only": true` 后重启服务（或用 `--replace --assistant-only` 重装）。不补的话它不再声明沙箱能力，绑在上面的助理暂停。
- 节点回滚到旧版本：节点不再声明能力，绑在它上面的助理暂停，普通员工不受影响。

**平台回滚、数据库降级的步骤与限制见 `docs/operations.md`「个人助理版本的升级与回退」**，核心是回滚平台之前先停用所有助理记录：旧平台会把助理的轮次当普通轮次派出去；助理专用实例会拒绝这些不带沙箱标记的请求（所有者看到的是失败），但旧平台也不做「助理所在节点不承载普通员工」的检查。

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
- 卸载不会改动 CLI 登录与 Claude 设置。旧版本启用过 Claude 额度 statusLine 探针（现已下线）的节点，statusLine 仍指向 `capture.sh` 并继续转交原命令；要恢复原状态栏，把 `~/.cache/claude_rate_limits/statusline-original.json` 中的 `statusLine` 写回 `~/.claude/settings.json`。`~/.claude/settings.before-coreman-probe.json` 是首次启用前的完整备份。

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
