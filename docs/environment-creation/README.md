# CoreMan Linux 运行时操作手册

适用对象：在 Linux 宿主机上新建 nspawn / chroot 用户环境，为 CoreMan Runtime Daemon 提供完整的软件、网络、认证、技能和服务管理能力。

本文定义新环境的部署流程。CoreMan 安装器、配置字段和通信方式以当前项目 `runtime_daemon/` 与运行时管理接口为依据。实际交付必须完成末尾的验收，文档检查不代替 Linux 实机验证。

## 1. 目标与执行顺序

一个 Linux 用户环境运行一个 CoreMan Runtime Daemon，同时发现 Claude Code 与 Codex CLI。Daemon 从 CoreMan 接收任务，管理本地 `runtime-claude` / `runtime-codex` 驱动，驱动使用 Unix socket，不需要给每种模型分配 TCP 端口。

执行顺序：参数和发布包 → Linux rootfs → nspawn 或 chroot → 代理与证书 → 软件和 CLI → 登录 → 技能/插件 → 安装 Daemon → 服务托管 → 验收。

- 默认使用带 systemd 的 nspawn，容器与 Daemon 都能在宿主机重启后恢复。
- chroot 使用完整独立 rootfs，由宿主机 systemd 托管前台监督进程。
- rootfs、HOME、凭据和工具缓存按实例独立；只有明确选定的项目目录做读写共享。
- 不将整个 `~/.local/share`、`~/.local/bin` 或 `/usr` 绑定为只读。Daemon 安装目录、CLI、插件和缓存均需要写入 HOME。
- 业务命令和软件安装以实例用户执行；root 只负责 OS 包、账户、挂载、证书和服务管理。

nspawn/chroot 共享宿主机内核；本方案使用宿主机网络与一致的 UID。它适合受控的运行时隔离，不提供互不信任租户之间的完整隔离。

## 2. 参数表与目录规范

开始前填写以下值；示例名称可以使用，所有 `__...__` 必须替换。以下宿主机命令在 **Linux Bash** 执行。每个阶段确认成功再继续，下载或校验失败不得继续解压/执行；只读探测中的“未找到”单独判断，不忽略安装错误。

```bash
ENV_NAME=coreman01
ENV_INDEX=01
ENV_ID=$((30000 + 10#$ENV_INDEX))
ENV_ROOT="/var/lib/machines/$ENV_NAME"
ENV_HOME="$ENV_ROOT/home/$ENV_NAME"
ENV_PROJECTS="/srv/coreman/workspaces/$ENV_NAME"
ENV_WORKSPACE=/workspace
COREMAN_URL=https://__COREMAN_DOMAIN__
# 默认无代理；需要时填写可达的 HTTP CONNECT 代理地址，不带账号密码
PROXY_URL=
NO_PROXY_LIST=localhost,127.0.0.1,::1,__COREMAN_DOMAIN__,10.0.0.0/8,192.168.0.0/16,172.16.0.0/12
```

UID/GID 必须查重，30000 + 序号仅为本手册的分配约定；不能重编号已有账户。nspawn 名称和 Linux 用户名保持一致，rootfs 使用上述固定目录，便于服务模板定位。

| 位置 | 内容 | 写入者 |
| --- | --- | --- |
| /var/lib/machines/coreman01 | 独立 Linux 系统 | root 管理，实例用户仅写自己的 HOME |
| /srv/coreman/workspaces/coreman01 → /workspace | CoreMan 项目主目录 | 实例用户 |
| ~/.local/share/coreman-runtime | 发布包、专属 venv、节点身份、状态、日志 | 实例用户，目录 0700 |
| ~/.config/coreman/runtime.env | 代理、CA、PATH 等环境设置 | 实例用户，0600 |
| ~/.local/opt/node | Node 制品 | 实例用户 |
| ~/.npm-global | 用户级 npm 软件 | 实例用户 |
| ~/.venvs/tools | Agent 工具使用的 Python 库 | 实例用户 |
| ~/.claude、~/.codex、~/.agents | CLI 登录、插件、技能配置 | 实例用户 |
| /tmp/coreman-UID-节点前缀 | 本地 Unix socket | Daemon，0700 |

项目主目录必须是非根目录的绝对路径，不含 `..`，实际路径不能经过符号链接，并且实例用户可写。不要把宿主机 SSH 用户的 HOME 当成实例 HOME。

## 3. 软件清单

### 3.1 必需组件

| 软件 | 位置 | 用途与安装方式 |
| --- | --- | --- |
| systemd-container、debootstrap、util-linux | 宿主机 | nspawn、rootfs 创建、namespace 管理 |
| Bash、CA、curl、OpenSSL、Git、OpenSSH client | rootfs | 安装、TLS、仓库操作 |
| Python 3.10+、venv | rootfs | Daemon 安装与运行；本文使用 Debian 13 的发行版 Python |
| Node.js 24 LTS、npm/npx | 实例 HOME | Skills 安装与 npm 工具；固定确切版本 |
| Claude Code、Codex CLI | 实例 HOME | 双模型执行能力；两个都安装，账号按需求分别登录 |
| CoreMan Runtime 发布包 | 实例 HOME | Daemon、两种驱动及 Python wheels，由 CoreMan 安装器分发 |
| httpx、requests | 发布包自带 venv | 由 Runtime requirements 和离线 wheels 安装，不在系统 Python 装 |

Debian 13 作为本文 rootfs 基线；宿主机的 debootstrap 必须认识 `trixie`。架构支持以 Runtime 发布包为准，目前构建入口支持 Linux amd64/arm64。[Debian 发行说明](https://www.debian.org/releases/trixie/)

Node 使用官方 LTS 制品，安装时选确切版本并核验 SHA-256，后续不得无记录地漂移。[Node 官方下载](https://nodejs.org/en/download)

### 3.2 通用任务工具包

本手册默认安装浏览器和办公文档工具，满足网页、PDF、Word、Excel、演示文稿等常见任务。它们不属于 Daemon 的 Python 依赖，不装到 Daemon 自带 venv。

| 能力 | 软件 |
| --- | --- |
| 搜索、JSON、归档、诊断 | ripgrep、jq、file、unzip、zip、xz-utils、procps、iproute2、psmisc |
| 网页操作 | agent-browser + Chromium + libnss3-tools + 中文字体 |
| 文档转换和 PDF | LibreOffice、poppler-utils、qpdf、fontconfig |
| Python 文件处理 | pypdf、PyMuPDF、python-docx、openpyxl、XlsxWriter、python-pptx、Pillow、reportlab、pandas、matplotlib |
| Python 工具与 MCP | uv/uvx |
| GitLab / GitHub 仓库管理 | 使用哪个服务就安装 glab / gh，并配置对应身份 |
| 媒体处理（任务需要时） | ffmpeg |

技能包自己的 SKILL.md、requirements、package.json 或安装钩子声明额外依赖时，在对应工具环境/项目内补装并验收，不凭软件名字推断已经支持该技能。

## 4. 宿主机预检与 rootfs 创建

### 4.1 只读预检

```bash
hostname -f
cat /etc/os-release
uname -m
systemd --version
findmnt -T /etc/shadow
findmnt -T /var/lib/machines
findmnt -T /srv
free -h
df -h /var/lib /srv
df -i /var/lib /srv
getent passwd "$ENV_NAME"
getent passwd "$ENV_ID"
getent group "$ENV_NAME"
getent group "$ENV_ID"
sudo machinectl list
sudo ss -ltnp
```

getent 查不到拟新增账户是正常结果。查到占用、目标目录已存在、同名 machine/service 已运行时先查明归属，不覆盖。为 rootfs、浏览器、文档套件、npm 缓存和业务文件预留磁盘，按并发和浏览器任务预留内存；不把一个固定容量当作所有业务的保证。

### 4.2 创建用户与系统

```bash
sudo apt-get update
sudo apt-get install -y systemd-container debootstrap util-linux ca-certificates
sudo groupadd --gid "$ENV_ID" "$ENV_NAME"
sudo useradd --uid "$ENV_ID" --gid "$ENV_ID" --no-create-home \
  --home-dir "/home/$ENV_NAME" --shell /bin/bash "$ENV_NAME"
sudo install -d -m 0755 /var/lib/machines /srv/coreman/workspaces
sudo install -d -m 0750 -o "$ENV_ID" -g "$ENV_ID" "$ENV_PROJECTS"
sudo debootstrap --variant=minbase --include=ca-certificates trixie "$ENV_ROOT" https://deb.debian.org/debian
sudo chroot "$ENV_ROOT" groupadd --gid "$ENV_ID" "$ENV_NAME"
sudo chroot "$ENV_ROOT" useradd --uid "$ENV_ID" --gid "$ENV_ID" \
  --create-home --home-dir "/home/$ENV_NAME" --shell /bin/bash "$ENV_NAME"
sudo chmod 0750 "$ENV_HOME"
sudo chroot "$ENV_ROOT" id "$ENV_NAME"
id "$ENV_NAME"
```

两边数值 UID/GID 必须一致，用户不加入 sudo 组。需要共享团队工作区时单独设计组或 ACL；不要递归放宽整个 /srv 或 /data。

安装系统包前，为新 rootfs 配置 main、updates、security 源，避免只使用基础安装源：

```bash
sudo tee "$ENV_ROOT/etc/apt/sources.list" >/dev/null <<'APT'
deb https://deb.debian.org/debian trixie main
deb https://deb.debian.org/debian trixie-updates main
deb https://security.debian.org/debian-security trixie-security main
APT
sudo chroot "$ENV_ROOT" /bin/bash <<'ROOTFS'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  bash ca-certificates curl openssl git openssh-client \
  python3 python3-venv locales tzdata \
  systemd systemd-sysv dbus dbus-user-session libpam-systemd \
  util-linux procps iproute2 psmisc ripgrep jq file unzip zip xz-utils \
  chromium libnss3-tools fonts-noto-cjk fonts-liberation fontconfig \
  libreoffice poppler-utils qpdf
localedef -i en_US -f UTF-8 en_US.UTF-8
localedef -i zh_CN -f UTF-8 zh_CN.UTF-8
ln -sfn /usr/share/zoneinfo/Asia/Shanghai /etc/localtime
printf '%s\n' Asia/Shanghai > /etc/timezone
ROOTFS
```

若网络必须通过代理，先让宿主机的 curl/debootstrap 能访问软件源；rootfs 的 apt 使用受控的 `/etc/apt/apt.conf.d/80proxy`，内容为 `Acquire::http::Proxy "http://代理:端口";` 和 `Acquire::https::Proxy "http://代理:端口";`。APT 不依赖实例用户的 shell 配置。代理做 TLS 解密时，先按第 7 节把 CA 安装到宿主机/新 rootfs 的系统信任库，再进行 HTTPS 安装，不关闭证书校验。

检查新 rootfs 的 resolv.conf；只有确认目标为普通文件时复制宿主机解析后的内容：

```bash
sudo ls -l "$ENV_ROOT/etc/resolv.conf"
sudo cp -L /etc/resolv.conf "$ENV_ROOT/etc/resolv.conf"
sudo chroot "$ENV_ROOT" getent hosts deb.debian.org
```

若为符号链接，先在这个新 rootfs 内改成独立配置文件，不能沿链接覆盖宿主机路径。共享网络时可使用宿主机可达的 DNS；如果改成独立网络，必须另外配置 DNS、路由与出口。

## 5. nspawn：启动完整系统

### 5.1 容器配置

本路线启动 rootfs 内的 systemd，由其管理实例用户服务。仅绑定项目目录，软件和 HOME 保持独立。

```bash
sudo install -d /etc/systemd/nspawn
sudo tee "/etc/systemd/nspawn/$ENV_NAME.nspawn" >/dev/null <<CONF
[Exec]
Boot=yes
PrivateUsers=no
[Network]
VirtualEthernet=no
[Files]
Bind=$ENV_PROJECTS:/workspace
CONF
sudo install -d "$ENV_ROOT/var/lib/systemd/linger"
sudo touch "$ENV_ROOT/var/lib/systemd/linger/$ENV_NAME"
# 新 rootfs 设置独立 hostname，不复制宿主机 machine-id
printf '%s\n' "$ENV_NAME" | sudo tee "$ENV_ROOT/etc/hostname" >/dev/null
sudo truncate -s 0 "$ENV_ROOT/etc/machine-id"
```

使用明确参数的自定义 unit，避免发行版模板的默认网络/用户映射改变上述约定：

```bash
sudo tee /etc/systemd/system/coreman-environment@.service >/dev/null <<'UNIT'
[Unit]
Description=CoreMan environment %i
After=network-online.target systemd-machined.service
Wants=network-online.target
RequiresMountsFor=/var/lib/machines/%i /srv/coreman/workspaces/%i
[Service]
Type=simple
ExecStart=/usr/bin/systemd-nspawn --directory=/var/lib/machines/%i --machine=%i --boot --settings=yes
Restart=on-failure
RestartSec=5
KillMode=mixed
KillSignal=SIGRTMIN+3
TimeoutStopSec=90
[Install]
WantedBy=multi-user.target
UNIT
sudo systemd-analyze verify /etc/systemd/system/coreman-environment@.service
sudo systemctl daemon-reload
sudo systemctl enable --now "coreman-environment@$ENV_NAME.service"
sudo machinectl show "$ENV_NAME" -p State -p Leader
sudo journalctl -u "coreman-environment@$ENV_NAME.service" -n 50 --no-pager
```

安装前确认 `/usr/bin/systemd-nspawn` 为实际路径；已有同名模板时核对内容，不覆盖别人的配置。重启 machine 会中断其中所有任务，必须先排空。

nspawn 的 boot 和 settings 行为以宿主机安装版本的手册为准。[systemd-nspawn 手册](https://manpages.debian.org/bookworm/systemd-container/systemd-nspawn.1.en.html)

### 5.2 进入并确认用户服务

```bash
# 宿主机
ENV_LEADER=$(sudo machinectl show "$ENV_NAME" -p Leader --value)
[[ "$ENV_LEADER" =~ ^[1-9][0-9]*$ ]] && [ "$ENV_LEADER" -gt 1 ] || exit 1
sudo nsenter -t "$ENV_LEADER" -m -u -i -n -p -- \
  systemctl start "user@$ENV_ID.service"
sudo nsenter -t "$ENV_LEADER" -m -u -i -n -p -- su - "$ENV_NAME"
```

下面开始为**环境内实例用户**命令：

```bash
whoami
printf 'HOME=%s\n' "$HOME"
id
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
systemctl --user show-environment >/dev/null
loginctl show-user "$(id -u)" -p Linger --value
```

最后两项必须成功且 Linger=yes。否则先修复容器内 dbus/user manager，不能将安装器回退到后台监督进程误当成已经具备开机自启。

## 6. chroot：完整 rootfs 与宿主机托管

选择 chroot 时跳过第 5 节，仍先完成第 4 节。使用私有 mount namespace，挂载 proc/dev/sys 和指定工作目录，不绑定宿主机 /etc、/usr 或其他用户 HOME。

在宿主机保存 `/usr/local/sbin/coreman-chroot-enter`（root:root，0755）：

```bash
#!/bin/bash
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo '必须由宿主机 root 执行' >&2; exit 1; }
name=${1:?缺少实例名}
shift
[[ "$name" =~ ^coreman[0-9]+$ ]] || exit 1
root="/var/lib/machines/$name"
projects="/srv/coreman/workspaces/$name"
[ -d "$root/etc" ] && [ -d "$projects" ] || exit 1
# 安装/维护时先停止该实例正式服务；不可并行启动多个 Runtime
exec unshare --mount --propagation private /bin/bash -c '
set -euo pipefail
root=$1; projects=$2; runtime=$3; shift 3
mkdir -p "$root"/{proc,dev,sys,workspace}
mount -t proc proc "$root/proc"
mount --rbind /dev "$root/dev"
mount --make-rprivate "$root/dev"
mount --rbind /sys "$root/sys"
mount --make-rprivate "$root/sys"
mount --bind "$projects" "$root/workspace"
exec chroot "$root" /bin/su - "$runtime" "$@"
' coreman-chroot "$root" "$projects" "$name" "$@"
```

```bash
# 宿主机进入安装会话，随后执行第 7–11 节的实例用户命令
sudo /usr/local/sbin/coreman-chroot-enter "$ENV_NAME"
```

chroot 内没有 systemd 用户服务时，安装器生成 `~/.local/share/coreman-runtime/supervise.sh` 并启动后台 supervisor。上线完成后必须转交第 12 节的宿主机 unit；只看到当前进程运行不等于会开机恢复。

退出交互 shell 时，仍存活的监督进程可能继续持有 namespace。移交前通过该实例 `supervisor.lock` 找到持有 PID，核对用户名、命令行与 config 路径，只终止这个 supervisor，等待 Daemon 和子进程退出，再启动正式 unit。不执行按用户名批量 kill。

## 7. 代理、CA 与持久环境变量

### 7.1 网络约定

| 连接 | 设置 |
| --- | --- |
| Runtime → CoreMan | 主动 HTTPS 请求；poll/heartbeat/enroll 等路径必须可达，不依赖 HTTP 跳转 |
| CLI、npm、uv、Git → 外部服务 | 使用组织提供的出口，通常为 HTTP CONNECT 代理 |
| 内网与 localhost | NO_PROXY 与 no_proxy 同时设置，包含 CoreMan 域名 |
| 浏览器 | 显式配置 agent-browser 的 proxy/proxyBypass/caCert |
| 邮件探测 | 直接连接所配置邮箱服务的 IMAP TLS 端口 |

`proxy` 安装选项只接受不含用户名/密码的 HTTP(S) URL。需要代理认证时，由宿主机受控的本地 CONNECT 网关承担认证，Runtime 指向其无凭据监听地址；不要把上游密码写入安装链接。

若上游仅提供 HTTPS proxy，而选定 CLI/邮件工具不能使用，先部署经过验证的 CONNECT 适配网关。网关在宿主机仅绑定回环，所有实例共用一个明确的服务；检查所属进程与实际转发，不以“端口被占用”判断可用。正常 HTTP CONNECT 出口无需额外适配软件。

没有已给定的组织代理地址和 CA 时，不猜地址或复制个人代理配置；把部署参数留待执行时填写。没有代理的环境省略相关行，保留公共 CA 和 NO_PROXY。

### 7.2 CA 安装

CA 必须由组织提供，核对 SHA-256 指纹；只需要 CA 公钥证书，不需要私钥。宿主机操作仅针对目标 rootfs：

```bash
openssl x509 -in /path/to/approved-proxy-ca.pem -noout -subject -issuer -fingerprint -sha256
sudo install -d "$ENV_ROOT/usr/local/share/ca-certificates"
sudo install -m 0644 /path/to/approved-proxy-ca.pem \
  "$ENV_ROOT/usr/local/share/ca-certificates/coreman-proxy.crt"
sudo chroot "$ENV_ROOT" update-ca-certificates
sudo install -d -m 0700 -o "$ENV_ID" -g "$ENV_ID" "$ENV_HOME/.config/coreman"
sudo install -m 0644 -o "$ENV_ID" -g "$ENV_ID" /path/to/approved-proxy-ca.pem \
  "$ENV_HOME/.config/coreman/proxy-ca.pem"
sudo install -m 0644 -o "$ENV_ID" -g "$ENV_ID" \
  "$ENV_ROOT/etc/ssl/certs/ca-certificates.crt" "$ENV_HOME/.config/coreman/ca-bundle.pem"
sudo openssl verify -CAfile "$ENV_HOME/.config/coreman/ca-bundle.pem" \
  "$ENV_HOME/.config/coreman/proxy-ca.pem"
```

无代理时也创建 `.config/coreman`，把系统 CA bundle 复制到上述位置；省略单 CA 及 NODE_EXTRA_CA_CERTS。CA 轮换后更新系统、用户 bundle、浏览器配置，并在无活跃任务时重启 Daemon。

### 7.3 一个环境文件供 shell 和服务使用

**实例用户执行**。环境文件采用 `KEY=value` 格式，不含 export、命令替换；生成后写入的路径均为绝对值，可直接被 systemd EnvironmentFile 读取。

```bash
mkdir -p "$HOME/.config/coreman" "$HOME/.local/bin" "$HOME/.npm-global"
chmod 700 "$HOME/.config/coreman"
cat > "$HOME/.config/coreman/runtime.env" <<ENV
PATH=$HOME/.venvs/tools/bin:$HOME/.npm-global/bin:$HOME/.local/bin:$HOME/.local/opt/node/bin:/usr/local/bin:/usr/bin:/bin
LANG=zh_CN.UTF-8
LC_ALL=zh_CN.UTF-8
TZ=Asia/Shanghai
HTTP_PROXY=__HTTP_CONNECT_PROXY_URL__
HTTPS_PROXY=__HTTP_CONNECT_PROXY_URL__
http_proxy=__HTTP_CONNECT_PROXY_URL__
https_proxy=__HTTP_CONNECT_PROXY_URL__
NO_PROXY=localhost,127.0.0.1,::1,__COREMAN_DOMAIN__,10.0.0.0/8,192.168.0.0/16,172.16.0.0/12
no_proxy=localhost,127.0.0.1,::1,__COREMAN_DOMAIN__,10.0.0.0/8,192.168.0.0/16,172.16.0.0/12
NODE_EXTRA_CA_CERTS=$HOME/.config/coreman/proxy-ca.pem
SSL_CERT_FILE=$HOME/.config/coreman/ca-bundle.pem
REQUESTS_CA_BUNDLE=$HOME/.config/coreman/ca-bundle.pem
CURL_CA_BUNDLE=$HOME/.config/coreman/ca-bundle.pem
PIP_CERT=$HOME/.config/coreman/ca-bundle.pem
UV_SYSTEM_CERTS=true
UV_TOOL_DIR=$HOME/.local/share/uv/tools
UV_CACHE_DIR=$HOME/.cache/uv
AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium
ENV
chmod 600 "$HOME/.config/coreman/runtime.env"
# 替换占位值/删除不使用的代理行后再加载
set -a
. "$HOME/.config/coreman/runtime.env"
set +a
```

在 `~/.profile`、`~/.bashrc` 顶部添加以下块；已有同样的块不重复插入，`.bashrc` 的非交互 return 必须在它之后。

```bash
if [ -r "$HOME/.config/coreman/runtime.env" ]; then
  set -a
  . "$HOME/.config/coreman/runtime.env"
  set +a
fi
```

nspawn 使用用户服务时，**安装 Runtime 前**预置 drop-in，让安装器第一次启动的服务就得到 CA 和代理：

```bash
mkdir -p "$HOME/.config/systemd/user/coreman-runtime.service.d"
printf '[Service]\nEnvironmentFile=%s/.config/coreman/runtime.env\n' "$HOME" \
  > "$HOME/.config/systemd/user/coreman-runtime.service.d/environment.conf"
systemctl --user daemon-reload
```

安装选项的 `proxy` 非空时会覆盖上述四个代理变量，两处应保持一致。Daemon 不会自动读取任意新增的 config.json 环境键：CA、NO_PROXY、uv 等通过此环境文件注入。修改 shell 文件不会改变已运行的服务环境。

### 7.4 分层验证

实例内分别验证，避免只凭 curl 成功推断全部工具可用：

```bash
getent hosts __COREMAN_DOMAIN__
curl --fail --connect-timeout 5 --max-time 15 https://__COREMAN_DOMAIN__/ -o /dev/null
curl --fail --connect-timeout 5 --max-time 15 https://registry.npmjs.org/ -o /dev/null
python3 - <<'PY'
import urllib.request
with urllib.request.urlopen('https://registry.npmjs.org/', timeout=15) as response:
    print(response.status)
PY
```

登录重定向或根路径 404 需要结合实际 CoreMan 路由判断；安装地址必须直接指向最终 HTTPS 地址。NODE_EXTRA_CA_CERTS 只给 Node 增补代理 CA；Python/httpx/requests 使用公共 CA + 代理 CA 的完整 bundle。不要默认关闭 TLS 校验。

### 7.5 HTTPS 上游适配网关（仅需要时）

宿主机已有可用 HTTP CONNECT 出口时跳过本节。需要将 HTTPS 上游转为本地 HTTP CONNECT 时，可使用 GOST v3。先从官方发布渠道取得对应 Linux 架构制品、核验 SHA-256，将可执行文件安装到 `/usr/local/bin/gost`，运行 `gost -V` 并记录版本。[GOST 转发配置](https://gost.run/en/getting-started/quick-start/)

在宿主机创建 `/etc/coreman/proxy.yaml`，root:root、0600。示例 18080 必须先查重；上游地址和证书名称按组织参数填写：

```yaml
services:
  - name: runtime-egress
    addr: 127.0.0.1:18080
    handler:
      type: http
      chain: upstream
    listener:
      type: tcp
chains:
  - name: upstream
    hops:
      - name: proxy
        nodes:
          - name: gateway
            addr: __UPSTREAM_HOST__:__UPSTREAM_PORT__
            connector:
              type: http
            dialer:
              type: tls
              tls:
                secure: true
                serverName: __UPSTREAM_CERTIFICATE_DNS_NAME__
```

上游 CA 由宿主机系统信任库提供。需要私有 CA 的固定信任配置时按批准版本添加 tls.caFile；保留 secure=true，核对 serverName。[GOST TLS](https://gost.run/en/tutorials/tls/)

宿主机 unit `/etc/systemd/system/coreman-egress.service`：

```ini
[Unit]
Description=CoreMan HTTP CONNECT egress
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
DynamicUser=yes
LoadCredential=proxy.yaml:/etc/coreman/proxy.yaml
ExecStart=/usr/local/bin/gost -C %d/proxy.yaml
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
[Install]
WantedBy=multi-user.target
```

```bash
# 宿主机，先确认配置与二进制路径真实存在
sudo systemd-analyze verify /etc/systemd/system/coreman-egress.service
sudo systemctl daemon-reload
sudo systemctl enable --now coreman-egress.service
sudo systemctl status coreman-egress.service --no-pager
sudo ss -ltnp 'sport = :18080'
curl --proxy http://127.0.0.1:18080 --connect-timeout 5 --max-time 20 \
  --fail https://registry.npmjs.org/ -o /dev/null
```

然后将实例 runtime.env、安装选项和 browser proxy 统一设为 `http://127.0.0.1:18080`。共享宿主机网络的 nspawn/chroot 可以使用该回环地址；本地网关是宿主机受控用户共同可用的出口。需要上游认证时在受控 YAML 的 connector.auth 配置，不写命令行；按实际版本验证认证与权限。

## 8. 软件安装与验证

### 8.1 Node.js、npm 与 npx

实例用户安装经过批准的 Node 24.x 确切版本。`NODE_SHA256` 从该版本官方校验文件取得，并纳入交付记录；下列命令不使用 sudo。

```bash
set -euo pipefail
NODE_VERSION=__EXACT_NODE_24_VERSION__
NODE_SHA256=__APPROVED_SHA256__
case "$(uname -m)" in
  x86_64) NODE_ARCH=x64 ;;
  aarch64) NODE_ARCH=arm64 ;;
  *) echo '不支持的架构' >&2; exit 1 ;;
esac
NODE_FILE="node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"
NODE_STAGE=$(mktemp -d)
curl -fSL --connect-timeout 10 --max-time 180 \
  "https://nodejs.org/dist/v${NODE_VERSION}/${NODE_FILE}" -o "$NODE_STAGE/$NODE_FILE"
printf '%s  %s\n' "$NODE_SHA256" "$NODE_STAGE/$NODE_FILE" | sha256sum -c -
mkdir -p "$HOME/.local/opt"
tar -xJf "$NODE_STAGE/$NODE_FILE" -C "$HOME/.local/opt"
# 新建环境，node 入口尚不存在
ln -s "node-v${NODE_VERSION}-linux-${NODE_ARCH}" "$HOME/.local/opt/node"
printf 'prefix=%s/.npm-global\n' "$HOME" > "$HOME/.npmrc"
chmod 600 "$HOME/.npmrc"
node --version
npm --version
npx --version
npm config get prefix
```

PATH 在第 7 节已包含 Node 与 npm-global。`npm config get prefix` 应指向当前用户 HOME。记录下载包哈希；临时下载目录确认无需保留后再清理。

### 8.2 两种 CLI

Claude 使用官方原生安装器，可指定确切版本。以当前实例用户安装到自己的 HOME，完整保留版本目录与入口。[Claude 安装文档](https://code.claude.com/docs/en/setup)

```bash
set -euo pipefail
CLAUDE_VERSION=__APPROVED_CLAUDE_VERSION__
CLI_INSTALLER=$(mktemp)
curl -fsSL https://claude.ai/install.sh -o "$CLI_INSTALLER"
bash "$CLI_INSTALLER" "$CLAUDE_VERSION"
claude --version
command -v claude
```

Codex 可使用官方独立安装器；本文使用用户级 npm 版本固定流程，与其他 npm 工具统一管理。包名为 `@openai/codex`，实际执行文件必须能运行。[Codex CLI](https://learn.chatgpt.com/docs/codex/cli)

```bash
CODEX_VERSION=__APPROVED_CODEX_VERSION__
npm install -g --prefix="$HOME/.npm-global" "@openai/codex@$CODEX_VERSION"
codex --version
command -v codex
```

同时安装两个 CLI 不等于同时登录。缺少 native optional dependency 时检查实际 CPU、npm optional 配置及软件包安装结果；不要只检查入口文件存在。

### 8.3 Python 工具环境、uv 和办公软件

```bash
/usr/bin/python3 -m venv "$HOME/.venvs/tools"
"$HOME/.venvs/tools/bin/python" -m pip install \
  uv pypdf PyMuPDF python-docx openpyxl XlsxWriter python-pptx \
  Pillow reportlab pandas matplotlib
"$HOME/.venvs/tools/bin/python" -m pip freeze > "$HOME/.config/coreman/tools-requirements.lock"
"$HOME/.venvs/tools/bin/python" - <<'PY'
import pypdf, pymupdf, docx, openpyxl, xlsxwriter, pptx, PIL, reportlab, pandas, matplotlib
print('office-python: ok')
PY
uv --version
uvx --version
libreoffice --version
pdftotext -v
qpdf --version
fc-match 'Noto Sans CJK SC'
```

uv 使用系统 CA 的环境变量为 UV_SYSTEM_CERTS，配合本手册安装的 CA。[uv 环境变量参考](https://docs.astral.sh/uv/reference/environment/)

首个经过验收的环境产生 tools-requirements.lock，其他环境使用该锁定清单安装并复测。不要改发行版 `/usr/bin/python3`，不要向系统 Python 或 Runtime 自带 venv 注入办公依赖。Daemon 自带 requests/httpx 由发布包维护。

需要本地编译扩展时由 rootfs 管理员补 `build-essential python3-dev`；媒体任务补 `ffmpeg`。项目自己的 dependencies 安装在项目 venv/node_modules，不混进全局工具环境。

### 8.4 agent-browser 与 Chromium

```bash
BROWSER_TOOL_VERSION=__APPROVED_AGENT_BROWSER_VERSION__
npm install -g --prefix="$HOME/.npm-global" "agent-browser@$BROWSER_TOOL_VERSION"
agent-browser --version
chromium --version
mkdir -p "$HOME/.agent-browser"
```

浏览器已由 rootfs 的 chromium 包提供；采用其系统路径，不再重复下载一套浏览器。agent-browser 官方也支持 `agent-browser install` 下载浏览器及 `--with-deps` 安装系统依赖。[安装文档](https://agent-browser.dev/installation)

需要代理时，创建 `~/.agent-browser/config.json`，路径填实际 HOME；无代理时仅保留 executablePath：

```json
{
  "executablePath": "/usr/bin/chromium",
  "proxy": "http://__PROXY_HOST__:__PROXY_PORT__",
  "proxyBypass": "localhost,127.0.0.1",
  "caCert": "/home/coreman01/.config/coreman/proxy-ca.pem"
}
```

使用支持 caCert 的批准版本；它将 CA 加入本地 Chromium 使用的隔离 NSS 库，保留 TLS 主机名和有效期验证。[配置文档](https://agent-browser.dev/configuration)

```bash
agent-browser open https://example.com
agent-browser snapshot
agent-browser close
```

以实例用户验证启动、页面内容和关闭过程。遇到 Chrome sandbox/user namespace 错误时检查宿主机与 nspawn 的能力和用户命名空间策略，不默认使用 `--no-sandbox` 或忽略证书错误。

### 8.5 Git 工具

按实际 Git 平台由 rootfs 管理员安装 `gh` 或 `glab`；先用包管理器查询候选版本，再安装选中的包。包不可用时从对应项目官方发布制品安装并校验 SHA-256，验证 CPU 架构。

```bash
# rootfs 管理员；GitLab 使用 glab，GitHub 使用 gh，只执行所需项
apt-get update
apt-get install -y glab
# apt-get install -y gh
```

实例用户配置独立 Git 身份：

```bash
git config --global user.name '__APPROVED_GIT_NAME__'
git config --global user.email '__APPROVED_GIT_EMAIL__'
git config --global http.sslCAInfo "$HOME/.config/coreman/ca-bundle.pem"
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
ssh-keygen -t ed25519 -f "$HOME/.ssh/id_ed25519" -C '__INSTANCE_GIT_IDENTITY__'
```

执行 ssh-keygen 前确认目标 key 不存在。公钥注册到批准的 Git 账号，核对服务器 host key 指纹后写 known_hosts；私钥 0600。glab/gh 通过交互或受控输入完成认证，输出只记录身份和成功状态。Git safe.directory 仅添加真正需要的工作区，不设全局通配。

## 9. CLI 登录与基础设置

两种 CLI 必须在实际实例用户 HOME 下登录，安装和认证不能在宿主机 root HOME 完成。

```bash
cd /workspace
claude auth login
claude auth status --json
codex login
codex login status
```

无桌面服务器可以使用 `codex login --device-auth`，前提是账号/组织允许设备码登录；否则按 CLI 提供的正常流程完成。[Codex 认证](https://developers.openai.com/codex/auth/)

采用固定版本管理时，合并 `{"env":{"DISABLE_AUTOUPDATER":"1"}}` 到 Claude 的 `~/.claude/settings.json`，保留其他字段，并以 `claude doctor` 验证自动更新已关闭；手工升级仍走维护流程。

Claude 验收字段为 loggedIn=true；Codex 的 `login status` 必须表示已登录。文件大小、onboarding 标记或缓存账号名不能替代认证状态。不要复制其他环境的 OAuth 文件。

首次在 `/workspace` 使用 Claude/Codex 时，完成该工作目录的交互初始化与必要信任设置。不得全局自动信任所有路径。CLI 的模型、权限和 MCP 设置使用批准版本支持的配置格式；CoreMan 请求级模型与工作区仍由平台选择。

Claude statusLine 额度探针是可选项：需要时安装链接选择 `install_claude_probe=true`。它在现有 statusLine 命令前串接一层采集脚本，原状态栏照常显示；重复安装沿用记录的原命令，不会重复包装。不要再部署另一套改写 statusLine 的额度探针。未启用时守护进程不排 Claude 额度探测，也不以额度数据缺失判定环境创建失败。

平台业务令牌按实际发起者/定时创建者归属注入；不在 shell 配置、插件预设或全局 MCP 中固定员工身份。Claude/Codex 登录主体用于模型服务认证，与业务系统操作主体分别管理。

## 10. Skills、插件与 MCP

### 10.1 必装清单的来源

Runtime 启动本身不要求固定的第三方插件。**完整环境的必装清单 = 双 CLI + 第 3 节通用工具 + 当前机器人已启用并获授权的技能/插件依赖。** 不为所有实例安装未经指定的业务连接器。

对每个机器人记录一张清单：技能/插件名、来源仓库、revision/版本、适用 CLI、安装范围、依赖、配置字段、权限主体、验收结果。来源与版本不明确时该项标记待配置，不能声称插件安装完整。

### 10.2 CoreMan 技能安装

1. 在平台登记技能来源，同步目录，启用允许使用的条目。
2. 配置该技能声明的环境字段/预设；内部权限需求走平台审批。
3. 在机器人工作目录发起安装，等待持久安装任务成功。
4. 核验实际目录、依赖与最小功能；安装任务入队不代表成功。

当前 Runtime 的普通技能安装使用 `npx --yes skills add <来源> --skill <名称> -y`；必须确保 npm/npx、代理、Git 权限和 git_hosts 白名单均可用。实际 `skills` 安装器版本目前由 npx 解析，交付时记录解析版本；若组织要求完全固定版本，应由项目实现固定安装器，不能在文档声称已经固定。

手工安装通用技能时，以实例用户在目标项目执行等价命令，来源/技能名使用批准清单：

```bash
cd /workspace/__PROJECT__
npx --yes skills add __APPROVED_SKILL_GIT_URL__ --skill __SKILL_NAME__ -y
```

建议通用能力清单覆盖浏览器、PDF、Word、Excel、PPT；具体技能名以目录实际条目为准。Claude 使用项目 `.claude/skills`，Codex 使用适用的 `.agents/skills` 路径，核对安装器实际生成目录和 SKILL.md。纯文本技能可在需要时用相对链接共享，但不能把完整 Claude 插件目录当作 Codex 插件安装。

### 10.3 Claude 插件

仅在批准清单包含 Claude 原生插件时执行；用户范围会影响该实例的所有 Claude 任务，项目范围仅用于该工作目录。

```bash
claude plugin marketplace add __APPROVED_MARKETPLACE_GIT_URL__
cd /workspace/__PROJECT__
claude plugin install __PLUGIN_NAME__@__MARKETPLACE_NAME__ --scope project
claude plugin list
```

插件缓存和 `.claude/settings.json` 保持实例用户可写。变更现有 settings 时合并指定项，不能覆盖整个配置文件；安装完在对应目录验证插件命令/工具可用。[Claude 插件参考](https://code.claude.com/docs/en/plugins-reference)

### 10.4 MCP 和环境设置

当前 CoreMan 的 MCP 安装实现将配置写入工作区 `.mcp.json` 的 mcpServers。必须从对应 CLI 验证连接状态；不能推断一份配置自动被两种 CLI 加载。

```bash
# 在目标项目内，验证安装版本的 MCP 命令
claude mcp list
codex mcp list
```

Claude/Codex 需要不同配置格式时，按该 CLI 的命令与批准的 MCP 清单分别登记。MCP command 指向可达的 uvx/npx/程序；依赖放到用户工具目录或项目环境，缓存必须可写。

CoreMan 中技能配置按声明字段保存和注入，密钥通过平台受控存储。不要把敏感 key 写入 AGENTS.md/CLAUDE.md、Git 提交或通用安装脚本。卸载技能不等于外部凭据已经吊销，需在其所属服务撤销对应权限。

## 11. CoreMan Runtime 发布与安装

### 11.1 服务端前置条件（平台维护者）

运行时安装需要已部署的 `/api/runtime` 与 `/api/admin/runtime-nodes` 接口、正确的 `PUBLIC_BASE_URL`、可用数据库结构和目标架构发布包。API 进程必须能读取 `runtime_daemon/install.sh` 模板和 RUNTIME_BUNDLE_DIR 下的包。

构建在 CoreMan 代码仓库的发布环境执行，实例环境不需要 Go：

```bash
# 发布环境需要 Python/pip 和满足 runtime_daemon/drivers/go.mod 的 Go 工具链
python3 runtime_daemon/build.py --target linux-amd64 --target linux-arm64 \
  --output runtime_daemon/dist
```

产物为 `coreman-runtime-linux-amd64.tar.gz` / `coreman-runtime-linux-arm64.tar.gz` 及各自 `.sha256`。Python wheels 随包提供，实例安装其依赖时不访问 PyPI。Node、两种 CLI、浏览器和业务技能由本手册先行准备，不在 Runtime 包中。

将产物部署到 API 配置的 `RUNTIME_BUNDLE_DIR`，为 API 用户授予只读权限。若 API 运行在镜像中，检查镜像/挂载包含安装模板、发布包和相关接口代码；仅在宿主机目录构建成功不代表 API 容器可读。包未发布时接口返回 503，应补发布而非重复兑换安装链接。

### 11.2 生成安装链接

使用已认证的平台管理员/允许管理运行时的角色生成安装链接。接口为 `POST /api/admin/runtime-nodes/install-links`，请求示例：

```json
{
  "name": "coreman01",
  "workspace_root": "/workspace",
  "visibility": "admins",
  "valid_hours": 24,
  "options": {
    "proxy": "",
    "claude_path": "/home/coreman01/.local/bin/claude",
    "codex_path": "/home/coreman01/.npm-global/bin/codex",
    "git_hosts": ["github.com"],
    "max_concurrent": 4,
    "install_claude_probe": false
  }
}
```

需要团队归属时添加实际 team_id；`visibility` 选择 admins 或 all。根据机器资源和模型账号容量填写 max_concurrent，默认 4、允许 1–32，不等于吞吐保证。CLI 路径必须通过 `command -v` 实测后填写。

通过已有管理会话调用接口，遵守其 CSRF 校验，不把账号口令或 Cookie 填入文档。返回的安装链接包含一次性秘密，按凭据处理；不放 Git、聊天记录或普通访问日志。

### 11.3 在实例用户环境安装

先加载第 7 节 runtime.env，nspawn 再确认 user bus/linger。下面的 `INSTALL_URL` 通过隐藏输入获得，不回显秘密：

```bash
set -euo pipefail
umask 077
read -r -s -p '粘贴 CoreMan 安装链接: ' INSTALL_URL
printf '\n'
INSTALL_SCRIPT=$(mktemp)
curl -fsSL --connect-timeout 10 --max-time 120 "$INSTALL_URL" -o "$INSTALL_SCRIPT"
sh "$INSTALL_SCRIPT"
unset INSTALL_URL
```

安装脚本下载发布包并核对 X-SHA256，建立独立 venv，持久保存节点身份并注册。临时安装脚本含链接配置，完成后删除该确切临时文件。不要用 sudo 运行，否则 Runtime 会安装到 root HOME。

当前用户已有 config.json 时安装器拒绝覆盖，并给出升级与卸载命令。安装器在服务注册成功后才写入 config.json；注册失败会清理本次发布目录与 CA 文件，可以直接重跑。安装成功但等待上线超时，应检查日志和状态，再重启已有服务；不要删除 config.json 或重新生成节点身份。该文件中的 node_id/node_token/backends 必须保留。

### 11.4 配置字段

| 字段 | 含义与维护方式 |
| --- | --- |
| api_url | 最终 CoreMan 地址，不依赖重定向 |
| workspace_root | 真实、可写、非符号链接的项目根目录 |
| path | 安装时捕获的 PATH；新增工具路径时要更新并重启 |
| claude_path、codex_path | 自定义 CLI 绝对路径；Daemon 为其创建 cli-bin 入口 |
| proxy | 覆盖四个 HTTP(S) 代理变量；留空使用服务环境 |
| git_hosts | 允许访问的 Git 主机名列表，不含 scheme/path |
| max_concurrent | 本节点同时领取的任务槽位 |
| install_claude_probe | 可选额度探针，在原 statusLine 命令前串接采集脚本 |
| release | 安装器生成的发布包目录 |
| node_id、node_token、backends | 安装器/注册接口维护的身份与能力映射，不手工伪造 |
| service_status | 安装器记录的托管方式 |


配置修改需先备份 0600 文件，再合并指定键，禁止用示例 JSON 整体覆盖已注册配置。节点 token 不输出到交付记录。

## 12. 服务托管与开机恢复

### 12.1 nspawn 内的 systemd 用户服务

安装器创建 `~/.config/systemd/user/coreman-runtime.service`。第 7 节的 drop-in 会加载持久环境变量。

```bash
# 实例用户，已配置 XDG_RUNTIME_DIR 和 DBUS_SESSION_BUS_ADDRESS
systemctl --user status coreman-runtime.service --no-pager
journalctl --user -u coreman-runtime.service -n 100 --no-pager
# 先暂停新任务来源，待活跃任务为零后停用节点，再执行重启
systemctl --user restart coreman-runtime.service
```

宿主机检查 `coreman-environment@实例.service`，容器内检查 coreman-runtime.service，两个层级都要通过。Linger=yes 才能在无人登录时启动用户服务。

### 12.2 chroot 的宿主机服务

初次安装的后台 supervisor 已按第 6 节完成移交后，创建以下宿主机模板：

```ini
# /etc/systemd/system/coreman-chroot-runtime@.service
[Unit]
Description=CoreMan chroot runtime %i
After=network-online.target
Wants=network-online.target
RequiresMountsFor=/var/lib/machines/%i /srv/coreman/workspaces/%i
[Service]
Type=simple
ExecStart=/usr/local/sbin/coreman-chroot-enter %i -c "exec /home/%i/.local/share/coreman-runtime/supervise.sh"
Restart=on-failure
RestartSec=10
KillMode=control-group
TimeoutStopSec=90
UMask=0077
[Install]
WantedBy=multi-user.target
```

```bash
# 宿主机
sudo systemd-analyze verify /etc/systemd/system/coreman-chroot-runtime@.service
sudo systemctl daemon-reload
sudo systemctl enable --now "coreman-chroot-runtime@$ENV_NAME.service"
sudo systemctl status "coreman-chroot-runtime@$ENV_NAME.service" --no-pager
sudo journalctl -u "coreman-chroot-runtime@$ENV_NAME.service" -n 100 --no-pager
```

`su -` 加载 runtime.env，supervise.sh 前台持有服务。不要再额外 nohup 一份 daemon；文件锁用于防重复启动，锁冲突应找现有进程归属，不删除锁文件来绕过。

chroot 对进程与网络没有单独隔离；宿主机能看到这些进程。涉及停止、维护、卸载时以 unit/cgroup/config 路径识别本实例，不能按所有 Python/Node/同名用户批量终止。

### 12.3 停机、升级与回退

1. 暂停向该节点提交新任务的入口或先把业务分配到其他节点，等待既有任务结束。
2. 确认活跃任务为零后再停用节点，记录 CLI 版本和当前 release。当前节点停用接口会取消未完成调用，不是无损排空接口，不能先停用再等待任务自然完成。
3. 停止对应 Daemon 服务；更换 rootfs/系统软件时才停止整个环境。
4. 备份配置和必要用户数据，按批准的安装/升级工具替换制品，不改变节点身份。
5. 启动后先检查 CLI 认证，再检查节点心跳、模型能力和最小任务，最后恢复接单。

当前安装器不能覆盖已有 config.json，重新安装不等于升级。升级、卸载与重新注册统一使用本机管理命令 `~/.local/share/coreman-runtime/bin/coreman-runtime`（它跟随当前 release，在任何目录下都可执行）。升级用 `--upgrade <发布包> --sha256 <校验值>`：先校验并自检新包，再停服务、切换 release、重写服务定义、启动并等待上线；任一步失败自动回滚到原版本，节点身份保持不变。卸载使用 `--uninstall`，默认保留节点身份、当前发布目录、会话与日志，之后可用 `--register` 以同一身份重新注册；`--uninstall --purge` 才删除整个数据目录。要改连其他 CoreMan 或换新身份，用安装链接加 `--replace`（`curl … | sh -s -- --replace`）：原安装目录整体备份为同级的 `coreman-runtime.bak-<时间戳>`，不删除。CLI 可以按第 8 节固定版本更新。

断开 CoreMan 或停止 Daemon 可能取消在途工作，不用生产任务测试重启。冷启动演练必须在测试环境验证“宿主机启动 → 环境启动 → Daemon → 心跳 → 任务”，再交付为自动恢复已完成。

## 13. 验收与故障定位

### 13.1 逐项验收

| 项目 | 通过条件 |
| --- | --- |
| 身份与目录 | whoami/HOME 正确，host/rootfs UID/GID 一致；/workspace 为真实可写目录 |
| 系统 | DNS/时区/locale 正常；无启动 setlocale warning |
| 挂载 | 仅预期项目目录共享；HOME、工具和缓存可写；未意外修改宿主机 /etc、/usr |
| Python | python3/venv 可用；工具库 import 通过；Runtime 自带依赖可导入 |
| Node/npm | node/npm/npx 能运行；全局 prefix 为当前用户 ~/.npm-global |
| CLI | 两种 CLI 都能查询版本；按启用能力分别通过真实登录状态检查 |
| 浏览器 | 当前用户打开网页、读取 snapshot、关闭成功；代理/TLS 正确 |
| 办公能力 | 中文字体命中；制作并读取样例 xlsx/docx/pptx/pdf；转换后的文件可打开 |
| Git | SSH/glab/gh 身份正确，批准仓库读取通过，不输出私钥和 token |
| 技能/插件 | 清单内所有必要项安装完成；目录和配置正确，最小工具调用通过 |
| 出口 | CoreMan、模型服务、npm、技能 Git 来源、业务 MCP 分别可达 |
| Daemon | config 权限 0600；state.json online=true 且 updated_at 新鲜；管理端同步在线 |
| 本地驱动 | 私有 Unix socket 存在；无不必要的入站模型/管理 TCP 端口 |
| 模型任务 | 从 CoreMan 对每个启用 provider 执行一次最小任务，工作区和账号符合预期 |
| 生命周期 | 正确 unit/监督器持有进程；重启后身份不变、服务和任务能力恢复 |

任一所需项失败不得标记“环境完整”。不使用的业务插件/MCP 标记 N/A 并记录原因；环境已安装但 CLI 未登录应单独标记“待认证”。

读取本地状态时只展示允许字段：

```bash
python3 - <<'PY'
import json, pathlib, time
p=pathlib.Path.home()/'.local/share/coreman-runtime/state.json'
s=json.loads(p.read_text())
print({'online': s.get('online'), 'age_seconds': round(time.time()-s.get('updated_at', 0))})
PY
```

### 13.2 故障表

| 现象 | 处理 |
| --- | --- |
| 安装下载 503 | API 未发布该架构包，核对 RUNTIME_BUNDLE_DIR 与 API 容器挂载 |
| 安装链接 410 | 过期或已使用；有 config 的环境重启原服务，无 config 时核对服务端注册状态 |
| 安装拒绝覆盖 config | 当前用户已存在节点身份；升级用 --upgrade，确需重装先执行 --uninstall --purge |
| 手工 curl 正常、服务 TLS 失败 | Daemon 只信任 config.json 的 ca_file 与系统 CA（生成安装链接时可填私有 CA）；CLI 与 npm 仍读 systemd drop-in 里的 CA 环境变量，修改后重启服务 |
| 登录后平台仍待登录 | 核对实际 HOME、CLI path、用户；Daemon 约每 60 秒重新发现 CLI |
| 已在线但任务失败 | 查看 runtime.log 与 claude.log/codex.log，核对登录、模型、代理、插件与工作目录 |
| npm 写系统目录/权限错误 | 确认运行用户与 ~/.npmrc，使用显式 --prefix |
| npx 找不到技能安装器 | 检查 Node PATH、npm 网络和目录权限，不只检查 CLI 入口存在 |
| Runtime 安装目录只读 | 去掉 HOME/.local 下过度共享，确保安装与缓存目录独立可写 |
| systemctl --user 连接失败 | nspawn 检查 user manager、dbus、XDG_RUNTIME_DIR、linger；chroot 使用宿主机托管 |
| file lock 冲突 | 找现有 daemon/supervisor 所属服务，不删除锁启动第二份 |
| CLI 状态检测失败 | 使用同用户执行 auth/status 命令，核对版本输出与超时；不凭认证文件存在判断 |
| 浏览器 TLS/sandbox 错误 | 检查 caCert/libnss3-tools 和 Linux namespace 配置，不默认关闭保护 |
| 工作区拒绝 | 检查路径实际解析结果、可写性和平台配置，不能用 / 或跨目录链接 |

Daemon 主日志为 `~/.local/share/coreman-runtime/runtime.log`，按 10 MB × 5 轮转；驱动日志为 claude.log/codex.log，service.log 只保留 WARNING 以上与崩溃回溯，二者每 60 秒按 10 MB × 5 复制截断轮转，轮转瞬间可能丢少量行。systemd 模式另可查 journal。驱动默认不记录聊天正文与提示词，排障时才在服务环境设置 RELAY_DEBUG=1。Daemon 以退出码 78 停止表示配置错误（安装链接失效、CA 无效等），服务不会自动重试，修正后手动启动。采集日志时脱敏请求内容、URL 中的 token 和外部凭据。

## 14. 完成交付

交付记录包含：宿主机标识、环境名与类型、OS/架构、UID/GID、rootfs/工作区、软件与插件版本、制品校验、代理地址与 CA 指纹、Git/CLI 身份、托管方式、平台节点 ID、验收结果、未完成项及恢复方法。不包含密码、OAuth、node_token 或安装链接。

分别确认：环境创建完成、基础工具完成、CLI 认证完成、技能/插件完成、Runtime 上线、业务任务验收、冷启动验收。只有全部适用阶段通过，才报告运行时已可交付。
