# CoreMan relay-agent

独立 Python 3.10+ 程序，与 运行时 使用同一个实例用户。无需安装 CoreMan；Python 依赖只有 `requests`。Git/仓库操作需要实例上已有 git、gh 或 glab；Skill 安装需要 Node/npx；额度探测需要相应 CLI 已登录。

```sh
python3 -m pip install -r requirements.txt
python3 agent.py --bind 0.0.0.0 --port 52123
```

运行前通过服务管理器的环境文件配置以下变量；文件权限应为 600，令牌从管理台的实例页签发，不放进仓库或命令行参数。

- `COREMAN_API_URL`：可达的 CoreMan 根地址。
- `COREMAN_AGENT_TOKEN`：该实例专用令牌，至少 24 字符。
- `COREMAN_RELAY_ID`：管理台实例 UUID。
- `COREMAN_WORKSPACE_ROOT`：允许操作的目录根，默认 `/data/skills`。
- `COREMAN_RELAY_URL`：本实例 运行时 地址，默认 `http://127.0.0.1:50009`。
- `COREMAN_MODEL_PROVIDER`：`claude` 或 `codex`，默认 `claude`。
- `COREMAN_MODEL`：健康检查使用的模型；未配置时报告 unknown。
- `COREMAN_GIT_HOSTS`：允许的 Git 主机，默认 `github.com`。
- `COREMAN_AGENT_PORT`：默认 52123。
- `COREMAN_MEMORY_SYNC=1`：启用每小时记忆上报，须待 CoreMan M5 的回收接口就绪后再开启。

端口只向 CoreMan 所在受信网络开放，跨网络使用 VPN 或带 TLS 的受控入口。每个请求是 `POST /`，带 `Authorization: Bearer …`，JSON 的 `type` 指定操作；不支持重定向，正文上限 4 MiB。部署程序本身不会更改 CLI 的全局配置。

支持 `ping`、`status` / `check-active-tasks`、`probe-rate-limits`、`health-check`、`pull`、`init`、`pr`、`install-skill`、`read-memory`、`collect-memory`、`deploy-memory`、`mail-probe`。耗时的额度和健康探测先返回已接受，再向 CoreMan 上报。管理台实时任务读取 Linux `/proc`，只返回同一实例用户、含机器人标识的进程；macOS 不提供这一进程视图。

默认每 30 分钟探测额度；本机时间 9–23 点每小时健康检查。`--no-schedules` 可关闭周期任务。Claude 的 statusLine 探针需要显式执行 `--install-claude-probe` 才安装：保留其它设置，损坏的 JSON 会使安装停止。安装动作会替换 statusLine，需实例运维者先检查原配置。CLI 登录、首次使用提示和模型调用权限仍由实例原配置决定。

仓库更新只允许白名单 HTTPS 或 SSH 来源，禁止用户名密码 URL。已有脏仓库会拒绝更新，不执行 reset/clean。`pr` 默认只提交已跟踪文件；新增文件须通过 `files` 显式指定。`init` 不覆盖已有说明文件。工作区、记忆、配置与输出样式均限制在指定目录内；记忆每文件 256 KiB，总量 4 MiB。命令失败只返回退出码，邮件探测凭证不落盘，响应不回传子进程原始输出。

当前仓库测试使用假 HTTP、临时目录及隔离数据库；尚未部署此 agent 到真实 Relay，也未执行真实邮件探测、仓库创建或模型额度探测。


M5 开发版增加 memory_protocol=2：ping 暴露能力，部署前检查，支持删除墓碑与 hash/mtime 冲突检测，部署保留文件时间。新管理台不会向旧 Agent 发送写请求。单文件原子替换，多文件出错可能部分完成，需重新回收核对。服务器升级至 M5 后再升级 Agent。新版每小时回收默认开启，可用 COREMAN_MEMORY_SYNC=0 关闭；自动回收先读取本实例分配的工作目录，逐目录上报，不扫描无关项目。read-memory 仅返回指定目录快照，不回调服务器。
