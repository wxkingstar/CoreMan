#!/usr/bin/env bash
# 在本机一台独立的 Linux 虚拟机（colima）里维持一组用完即弃的 GitHub Actions runner。
#
# 用法：runner-pool.sh <命令>
#   up         起虚拟机（不挂载本机任何目录）并拉好 runner 镜像
#   run        前台守护：每个空位签一份一次性注册配置（JIT），起一个只跑一个 job 的容器
#   warm       把工作流用到的 action 预先放进缓存卷（run 每小时也会在后台补一次）
#   status     看虚拟机、runner 容器和 GitHub 上登记的 runner
#   down       停掉所有 runner 容器、注销离线 runner，再停虚拟机
#   install    装成 launchd 常驻（登录后自动 run）；代理设置从当前 shell 记下
#   uninstall  卸掉 launchd 常驻（不停虚拟机，要停再 down）
#
# 安全边界：
# - job 能操作虚拟机里的 Docker（服务容器要用），所以虚拟机不挂载本机目录、不转发 SSH agent。
# - 注册凭据只在宿主机上用 gh 签发；容器拿到的是只能注册一个 runner、跑一个 job 的 JIT 配置。
# - 只有本仓库自己的分支会被派到这里（.github/workflows/ci.yml 的 runs-on），fork 的 PR 和
#   dependabot 仍走 GitHub 托管的 runner。
#
# 可用环境变量覆盖：CI_RUNNER_SLOTS（并发 job 数，默认 4）、CI_RUNNER_CPUS / CI_RUNNER_MEMORY
# （GiB）/ CI_RUNNER_DISK（GiB）、CI_RUNNER_PYTEST_WORKERS（每个 job 的 xdist worker 数）。
set -euo pipefail

STATE="${CI_RUNNER_STATE:-$HOME/.coreman-ci}"
# install 记下的 PATH、仓库和代理；launchd 起的进程没有登录 shell 的环境。
# shellcheck source=/dev/null
[[ -f "$STATE/pool.env" ]] && source "$STATE/pool.env"

PROFILE="${CI_RUNNER_PROFILE:-coreman-ci}"
SLOTS="${CI_RUNNER_SLOTS:-4}"
CPUS="${CI_RUNNER_CPUS:-14}"
MEMORY="${CI_RUNNER_MEMORY:-32}"
DISK="${CI_RUNNER_DISK:-80}"
WORKERS="${CI_RUNNER_PYTEST_WORKERS:-4}"
IMAGE="${CI_RUNNER_IMAGE:-ghcr.io/actions/actions-runner:2.338.2}"
LABEL="${CI_RUNNER_LABEL:-coreman-ci}"
PLIST="$HOME/Library/LaunchAgents/com.github.${LABEL}.runner-pool.plist"
DOCKER=(docker --context "colima-${PROFILE}")
PROXY_VARS=(HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy)
# 一次性容器之间共用的缓存卷：uv 的下载缓存和 uv 装的 Python。不共用的话每个 job 都要从零
# 下几百个包，经代理要十几分钟；uv 自带文件锁，几个 runner 同时用是安全的。
# 挂载点的上级目录（~/.cache、~/.local）由 Docker 以 root 建出，容器启动时先交还给 runner。
CACHE_MOUNTS=("${LABEL}-uv-cache:/home/runner/.cache/uv" "${LABEL}-uv-python:/home/runner/.local/share/uv")
# action 压缩包缓存：runner 下载 `uses:` 的 action 走 codeload.github.com，经代理时常慢到超时，
# 每个一次性容器又都要重下。runner 先找 ACTIONS_RUNNER_ACTION_ARCHIVE_CACHE 下的
# `<owner>_<repo>/<sha>.tar.gz`，有就不下载；由 warm 命令按工作流里钉死的 sha 预先放好。
ACTION_CACHE_DIR=/home/runner/action-archive-cache
ACTION_VOLUME="${LABEL}-actions"
CACHE_MOUNTS+=("${ACTION_VOLUME}:${ACTION_CACHE_DIR}")

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

repo() {
  if [[ -z "${CI_RUNNER_REPO:-}" ]]; then
    CI_RUNNER_REPO="$(cd "$(dirname "$0")" && gh repo view --json nameWithOwner --jq .nameWithOwner)"
  fi
  printf '%s' "$CI_RUNNER_REPO"
}

vm_running() { colima status -p "$PROFILE" >/dev/null 2>&1; }

# colima start/restart 会把默认 docker context 切到这台虚拟机；跑完切回原来的，不影响日常用的 Docker。
colima_keep_context() {
  local previous
  previous="$(docker context show 2>/dev/null || true)"
  (cd / && colima "$@")
  [[ -n "$previous" ]] && docker context use "$previous" >/dev/null
  return 0
}

# runner 以非 root 用户跑，要加进虚拟机里 docker.sock 的属组才能起服务容器。
socket_gid() {
  "${DOCKER[@]}" run --rm -v /var/run/docker.sock:/s --entrypoint stat "$IMAGE" -c %g /s
}

# CI_RUNNER_REGISTRY_MIRRORS（逗号分隔，如 https://mirror.example.com）：Docker Hub 直连不稳时，
# 服务容器的镜像（postgres 等）每个 job 都要拉一次，写进虚拟机 dockerd 的 registry-mirrors。
ensure_mirrors() {
  [[ -n "${CI_RUNNER_REGISTRY_MIRRORS:-}" ]] || return 0
  local want have
  want="$(tr ',' '\n' <<<"$CI_RUNNER_REGISTRY_MIRRORS" | sed -E 's#/*$#/#' | sort | paste -sd, -)"
  have="$("${DOCKER[@]}" info --format '{{range .RegistryConfig.Mirrors}}{{println .}}{{end}}' \
    | sed '/^$/d' | sort | paste -sd, -)"
  [[ "$want" == "$have" ]] && return 0
  log "虚拟机的镜像站改为 ${CI_RUNNER_REGISTRY_MIRRORS}，重启虚拟机"
  python3 -I - "$HOME/.colima/${PROFILE}/colima.yaml" "$CI_RUNNER_REGISTRY_MIRRORS" <<'PY'
import sys

path, mirrors = sys.argv[1], [m for m in sys.argv[2].split(",") if m]
lines = open(path, encoding="utf-8").read().split("\n")
start = next(i for i, line in enumerate(lines) if line.startswith("docker:"))
end = start + 1
while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
    end += 1
block = ["docker:", "  registry-mirrors:"] + [f"    - {m}" for m in mirrors] + [""]
lines[start:end] = block
open(path, "w", encoding="utf-8").write("\n".join(lines))
PY
  colima_keep_context restart -p "$PROFILE"
}

cmd_up() {
  mkdir -p "$STATE" && chmod 700 "$STATE"
  if ! vm_running; then
    log "起虚拟机 ${PROFILE}：${CPUS} 核 / ${MEMORY}G 内存 / ${DISK}G 磁盘，不挂载本机目录"
    colima_keep_context start -p "$PROFILE" --cpus "$CPUS" --memory "$MEMORY" --disk "$DISK" \
      --mount none --runtime docker
  fi
  ensure_mirrors
  if ! "${DOCKER[@]}" image inspect "$IMAGE" >/dev/null 2>&1; then
    log "拉镜像 ${IMAGE}"
    "${DOCKER[@]}" pull -q "$IMAGE" >/dev/null
  fi
  # 新建的卷属 root；runner 以 runner 用户跑，要先把卷交给它。
  local mount volume
  for mount in "${CACHE_MOUNTS[@]}"; do
    volume="${mount%%:*}"
    if ! "${DOCKER[@]}" volume inspect "$volume" >/dev/null 2>&1; then
      "${DOCKER[@]}" volume create "$volume" >/dev/null
      "${DOCKER[@]}" run --rm -u 0 -v "${volume}:/v" --entrypoint chown "$IMAGE" runner:runner /v
    fi
  done
}

# GitHub 上本标签下已离线的 runner（容器没等到 job 就没了时会留下），逐个注销。
prune_offline() {
  local id
  for id in $(gh api "repos/$(repo)/actions/runners" --paginate \
    --jq ".runners[] | select(.status == \"offline\") | select(any(.labels[]; .name == \"${LABEL}\")) | .id" \
    2>/dev/null); do
    gh api -X DELETE "repos/$(repo)/actions/runners/${id}" >/dev/null 2>&1 && log "注销离线 runner ${id}"
  done
  return 0
}

start_slot() {
  local slot="$1" gid="$2" name cfg
  name="${LABEL}-${slot}-$(date +%s)"
  if ! cfg="$(gh api -X POST "repos/$(repo)/actions/runners/generate-jitconfig" \
    -f name="$name" -F runner_group_id=1 -f work_folder=_work \
    -f 'labels[]=self-hosted' -f 'labels[]=Linux' -f 'labels[]=ARM64' -f "labels[]=${LABEL}" \
    --jq .encoded_jit_config 2>&1)"; then
    log "空位 ${slot} 签发注册配置失败：${cfg}"
    return 0
  fi
  local env_args=(-e "PYTEST_XDIST_AUTO_NUM_WORKERS=${WORKERS}"
    -e "ACTIONS_RUNNER_ACTION_ARCHIVE_CACHE=${ACTION_CACHE_DIR}") var mount
  for mount in "${CACHE_MOUNTS[@]}"; do
    env_args+=(-v "$mount")
  done
  for var in "${PROXY_VARS[@]}"; do
    [[ -n "${!var:-}" ]] && env_args+=(-e "${var}=${!var}")
  done
  # --network host：服务容器（postgres）的端口发布在虚拟机上，job 用 127.0.0.1 就能连到。
  # 单引号是有意的：$1 由容器里的 bash 展开。
  # shellcheck disable=SC2016
  local boot='sudo -n chown runner:runner ~/.cache ~/.local ~/.local/share && exec ./run.sh --jitconfig "$1"'
  if "${DOCKER[@]}" run -d --rm --name "$name" --label "${LABEL}-slot=${slot}" --network host \
    -v /var/run/docker.sock:/var/run/docker.sock --group-add "$gid" "${env_args[@]}" \
    --entrypoint bash "$IMAGE" -c "$boot" _ "$cfg" >/dev/null; then
    log "空位 ${slot} 起了 ${name}"
  else
    log "空位 ${slot} 起容器失败"
  fi
}

# 工作流里 `uses: <owner>/<repo>@<40 位 sha>` 用到的 action（默认分支上的版本），逐个看缓存卷里
# 有没有，没有就在宿主机上用 git 浅拉那个提交、打成 tar.gz 放进去。git 走 github.com，不经
# codeload；一个 action 慢的时候要两三分钟，但同一个 sha 只拉一次。失败的下一轮再试。
cmd_warm() {
  local listing uses action owner_repo sha cached tmp archive
  if ! listing="$(gh api "repos/$(repo)/contents/.github/workflows" --jq '.[].name' 2>&1)"; then
    log "读工作流列表失败：${listing}"
    return 0
  fi
  uses="$(for name in $listing; do
    gh api -H 'Accept: application/vnd.github.raw' "repos/$(repo)/contents/.github/workflows/${name}" 2>/dev/null
  done | sed -nE 's#^[[:space:]-]*uses:[[:space:]]*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-f]{40}).*#\1@\2#p' | sort -u)"
  cached="$("${DOCKER[@]}" run --rm -u runner -v "${ACTION_VOLUME}:/c" --entrypoint find "$IMAGE" \
    /c -name '*.tar.gz' 2>/dev/null)"
  for action in $uses; do
    owner_repo="${action%@*}"
    sha="${action#*@}"
    archive="${owner_repo/\//_}/${sha}.tar.gz"
    grep -qx "/c/${archive}" <<<"$cached" && continue
    log "预热 action ${owner_repo}@${sha:0:7}"
    tmp="$(mktemp -d)"
    # 单引号是有意的：$1 由容器里的 sh 展开。
    # shellcheck disable=SC2016
    if git -C "$tmp" init -q \
      && git -C "$tmp" fetch -q --depth 1 "https://github.com/${owner_repo}" "$sha" \
      && git -C "$tmp" archive --format=tar.gz --prefix="${owner_repo/\//-}-${sha:0:7}/" FETCH_HEAD \
        | "${DOCKER[@]}" run --rm -i -u runner -v "${ACTION_VOLUME}:/c" --entrypoint sh "$IMAGE" \
          -c 'mkdir -p "/c/${1%/*}" && cat >"/c/$1.part" && mv "/c/$1.part" "/c/$1"' _ "$archive"; then
      log "已缓存 ${archive}"
    else
      log "预热 ${owner_repo}@${sha:0:7} 失败，下一轮再试"
    fi
    rm -rf "$tmp"
  done
}

cmd_run() {
  cmd_up
  local gid round=0 slot
  gid="$(socket_gid)"
  trap 'log "收到停止信号，退出（容器跑完手上的 job 自己结束）"; exit 0' TERM INT
  log "守护 $(repo)：${SLOTS} 个空位，标签 ${LABEL}"
  while true; do
    if ! vm_running; then
      log "虚拟机不在，重新拉起"
      cmd_up || { sleep 30; continue; }
      gid="$(socket_gid)"
    fi
    (( round % 120 == 0 )) && prune_offline
    # 每小时（720 轮 × 5 秒）在后台补一次 action 缓存：工作流升级 action 后不用手动预热。
    (( round % 720 == 0 )) && { cmd_warm & }
    for slot in $(seq 1 "$SLOTS"); do
      if [[ -z "$("${DOCKER[@]}" ps -q --filter "label=${LABEL}-slot=${slot}" 2>/dev/null)" ]]; then
        start_slot "$slot" "$gid"
      fi
    done
    round=$((round + 1))
    sleep 5
  done
}

cmd_status() {
  colima list 2>/dev/null | awk -v p="$PROFILE" 'NR == 1 || $1 == p'
  if vm_running; then
    "${DOCKER[@]}" ps --filter "label=${LABEL}-slot" --format '{{.Names}}\t{{.Status}}'
  fi
  gh api "repos/$(repo)/actions/runners" --paginate \
    --jq ".runners[] | select(any(.labels[]; .name == \"${LABEL}\")) | [.name, .status, (if .busy then \"busy\" else \"idle\" end)] | @tsv"
  if [[ -f "$PLIST" ]]; then
    launchctl print "gui/$(id -u)/com.github.${LABEL}.runner-pool" 2>/dev/null \
      | awk '/^\tstate =|^\tpid =/ {print "launchd:" $0}'
  fi
}

cmd_down() {
  if vm_running; then
    # launchd 用的是 macOS 自带的 bash 3.2：没有 mapfile。
    local id
    for id in $("${DOCKER[@]}" ps -q --filter "label=${LABEL}-slot"); do
      "${DOCKER[@]}" stop -t 10 "$id" >/dev/null
    done
    colima stop -p "$PROFILE"
  fi
  prune_offline
}

cmd_install() {
  mkdir -p "$STATE" "$(dirname "$PLIST")" && chmod 700 "$STATE"
  local var
  {
    printf 'export PATH=%q\n' "$PATH"
    printf 'export CI_RUNNER_REPO=%q\n' "$(repo)"
    [[ -n "${CI_RUNNER_REGISTRY_MIRRORS:-}" ]] \
      && printf 'export CI_RUNNER_REGISTRY_MIRRORS=%q\n' "$CI_RUNNER_REGISTRY_MIRRORS"
    for var in "${PROXY_VARS[@]}"; do
      [[ -n "${!var:-}" ]] && printf 'export %s=%q\n' "$var" "${!var}"
    done
  } >"$STATE/pool.env"
  chmod 600 "$STATE/pool.env"
  # 拷一份脚本到状态目录：从 worktree 装的话，worktree 删掉后常驻还能跑。
  install -m 755 "$0" "$STATE/runner-pool.sh"
  local plist_format='<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>%s</string><string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>%s</string>
  <key>StandardErrorPath</key><string>%s</string>
</dict>
</plist>
'
  # shellcheck disable=SC2059  # 格式串是上面的固定模板
  printf "$plist_format" "com.github.${LABEL}.runner-pool" "${STATE}/runner-pool.sh" \
    "${STATE}/pool.log" "${STATE}/pool.log" >"$PLIST"
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  log "已装 launchd 常驻：${PLIST}，日志 ${STATE}/pool.log"
}

cmd_uninstall() {
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  log "已卸 launchd 常驻；虚拟机和容器还在，要停再执行 down"
}

case "${1:-}" in
  up) cmd_up ;;
  run) cmd_run ;;
  warm) cmd_up && cmd_warm ;;
  status) cmd_status ;;
  down) cmd_down ;;
  install) cmd_install ;;
  uninstall) cmd_uninstall ;;
  *) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
