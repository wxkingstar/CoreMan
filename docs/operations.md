# 运行维护与生产切换

## 部署和升级

首次部署使用 `deploy/coreman up` 初始化配置。已有环境先备份 PostgreSQL、对象存储和加密主密钥，确认恢复方法，再执行增量迁移与滚动升级。数据库测试会重置测试库；业务库不能作为 TEST_DATABASE_URL。

```bash
COREMAN_ENV_FILE=/absolute/path/.env ./deploy/coreman build              # 输出镜像标签，例如 3f2c1ab
COREMAN_ENV_FILE=/absolute/path/.env ./deploy/coreman upgrade all 3f2c1ab
./deploy/coreman upgrade gateway wecom 3f2c1ab
./deploy/coreman status
```

### 构建与镜像标签

`build` 默认以当前 Git 短提交作为镜像标签；进入镜像的路径（`coreman`、`web`、`runtime_daemon`、`migrations`、`deploy`、依赖锁文件等）有未提交或未跟踪的改动时，标签与镜像内的构建提交都加 `-dirty`。`--tag <标签>` 可指定其他标签。健康检查和实例列表的版本带构建提交。

`build` 拒绝覆盖正在使用的标签：部署标签、待定升级标签或任一运行中 CoreMan 容器的标签。覆盖后 `rollback` 就回不到旧代码；确需覆盖时加 `--force`。尚无任何部署（没有部署标签，也没有 CoreMan 容器）时，`build` 直接把新标签记为部署标签，随后 `up` 使用它。

也可以使用发布流水线推送的镜像（见「持续集成与发布」）：先 `docker pull ghcr.io/<owner>/coreman-api:v1.2.3`，再 `docker tag` 为 `coreman-api:v1.2.3`，`coreman-runtime` 同理。部署脚本只使用本机已有的 `coreman-api:<标签>` 与 `coreman-runtime:<标签>`，不会自动拉取。

### 升级事务

`upgrade <api|worker|scheduler|gateway|all> [标签]` 与 `rollback <标签>` 按以下顺序执行：

1. **预检**：`docker image inspect` 确认 `coreman-api:<标签>` 与 `coreman-runtime:<标签>` 都在本机。未给标签时使用当前部署标签。
2. 把目标标签写入 `.coreman-image-tag.pending`。
3. 按目标滚动升级（见下文），任何一步失败立即停止。
4. 全部步骤成功后把标签提升为 `.coreman-image-tag`，删除 pending 文件。

**预检失败**时脚本以退出码 2 结束，不重载 Caddy、不迁移、不重建容器，也不写任何状态文件，运行中的服务不受影响。按提示补齐镜像后重跑：在对应提交上 `build --tag <标签>`，或从镜像仓库拉取后改名；`docker image ls coreman-api`、`docker image ls coreman-runtime` 可查看本机已有标签。

**升级失败**时 pending 文件保留，脚本在标准错误输出各容器实际运行的镜像标签、仍关闭自动重启的运行中容器，以及恢复建议：

- `coreman logs <服务>` 查看原因。排空超时表示仍有在途任务，不要直接删除容器或数据卷。
- 排除问题后重跑同一条命令；已就绪的服务会再次排空再重建，不会强杀在途任务。
- 放弃本次升级：`coreman rollback <原部署标签>`（不回退数据库，适用范围见「镜像回退」）。
- 确认各服务版本一致、排空状态正常后，也可以手工删除 pending 文件。

存在 pending 文件时 `up` 拒绝执行：整体启动会按部署标签重建服务，可能把已升级的服务拉回旧版本，而数据库可能已经迁移到新版本。先继续或放弃升级；确认无误后才使用 `up --force`。

只升级部分目标（例如 `upgrade api v2`）成功后同样更新部署标签，其余服务仍运行旧标签；`status` 会提示混合版本，应尽快完成其余目标。

API 升级先重载 Caddy 的指标隔离规则，再迁移并逐台健康升级。worker 先确认另一台就绪，关闭旧容器自动重启，要求旧实例排空，最多等待 7500 秒，再重建；不会因超时强杀在途任务。网关确认备用实例新鲜心跳后排空活跃实例，等待其退出、租约转移且连接 subscribed，随后停止旧服务，并把新的活跃侧写入 `.coreman-gateway-active`；下次反向升级。若两侧均运行，先把备用侧的工作交还活跃侧，再重建备用侧。排空期间的自动重启被关闭，避免旧容器退出后又抢回租约；确认排空状态后用 `docker update --restart=unless-stopped <容器>` 恢复预期策略。

### 启动、停止与网关活跃侧

`up` 先迁移，再用 `docker compose up -d --wait` 等待全部服务运行且健康检查通过（默认 300 秒，可用 `COREMAN_WAIT_TIMEOUT` 调整）；超时返回非零，按提示查看 `status` 与 `logs`。`up` 是整体启动/初始化入口，不能替代在途任务期间的滚动升级。

网关 a/b 同一时刻只有一侧活跃。`up`、`upgrade all` 与 `upgrade gateway` 按 `.coreman-gateway-active` 选择活跃侧；记录缺失时沿用推断：a 侧未运行而 b 侧在运行时为 b，否则为 a。记录与运行容器矛盾（记录的一侧未运行、另一侧在运行）时以运行容器为准并给出提示。宿主机重启后，Docker 按 `restart: unless-stopped` 只恢复上次运行的一侧。

`down` 可传 `-t <秒>`。worker 的停止宽限为 7200 秒，有在途任务时默认会一直等待；只有紧急停机才缩短。

### 状态文件

升级状态不写回含凭证的 .env，保存在仓库根目录（可用 `COREMAN_STATE_DIR` 指定其他目录），均已加入 `.gitignore`：

| 文件 | 内容 |
|---|---|
| `.coreman-image-tag` | 最近一次成功完成的 upgrade/rollback 的镜像标签；首次 `build` 时写入 |
| `.coreman-image-tag.pending` | 进行中或失败后未收尾的升级目标标签 |
| `.coreman-gateway-active` | `wecom=a` / `feishu=b` 形式的网关活跃侧，网关升级完成后更新 |

镜像标签优先级：环境变量 `COREMAN_IMAGE_TAG` > `.coreman-image-tag` > .env 中的 `COREMAN_IMAGE_TAG`。

### 运行状态

`status` 依次输出 `docker compose ps`、各容器（含已停止的备用网关）的镜像标签与状态、部署标签、未完成的升级、PostgreSQL 客户端连接数与 `max_connections`，最后检查 API `/health`。运行中的 CoreMan 容器使用多个标签时提示混合版本；客户端连接达到 `max_connections` 的 80% 时告警。

## 镜像回退

```bash
./deploy/coreman rollback retained-tag
```

两种镜像都须有该保留标签，预检不通过时不做任何改动。回退使用当前 API 镜像中的排空工具，保持旧任务和租约交接，与升级一样经过 pending 与提升；不会执行数据库 downgrade，仅适用于经过兼容性验证的加表、加列或加索引版本。目标版本须具备实例登记与网关排空能力。回退所需的镜像标签应一直保留，不要用 `build --force` 覆盖。

升级只支持已有 a/b worker 与本机 Compose 网关对；跨主机调度、共享存储和外部负载均衡需按实际部署配置。部署工具持有数据库运行权限，应仅允许受信任运维使用。

## 数据库连接预算

Compose 为 PostgreSQL 显式设置 `max_connections=300`、`shared_buffers=256MB`、`effective_cache_size=768MB`、`work_mem=4MB`，并把 `shm_size` 设为 256MB。默认的 100 条连接会先于业务 QPS 用尽。按上限估算：

| 来源 | 连接数 |
|---|---|
| api×2、worker×2、scheduler、企微网关、飞书网关的进程池（各 pool_size 5 + max_overflow 5） | 70 |
| worker、网关的 LISTEN 连接与 scheduler 咨询锁连接 | 约 5 |
| 网关 a/b 接管期间另一侧同时在线 | 约 11 |
| 飞书网关每个应用一个子进程，各最多 5 条 | 5 × 应用数 |
| runtime 节点在跑对话的瞬时连接、migrate/operations 临时进程、管理员排查 | 按实际预留 |

20 个飞书应用加 30 个 runtime 并发对话约 230 条。部署和扩容前按实例数与机器人数量重新计算，并用 `coreman status` 查看当前客户端连接数。需要超过 300 条时同时调整 `max_connections` 与内存参数（每条连接另占数 MB），参数说明见 `deploy/docker-compose.yml` 中 postgres 服务的注释。

## 持续集成与发布

仓库使用 GitHub Actions：

- `CI`（push 与 PR 到 main）：分层 import 门禁、ruff、mypy、pytest（PostgreSQL 16 service）、前端 lint / 类型检查 / vitest / 构建、Go 驱动 `go vet` 与 `go test -race`、shellcheck、`docker compose config`。
- `Release`（推送 `v*` 标签）：构建 amd64/arm64 的 `ghcr.io/<owner>/coreman-api` 与 `ghcr.io/<owner>/coreman-runtime`，镜像标签为 `v1.2.3`、`1.2.3`、`1.2`，正式版本另打 `latest`；从刚发布的 API 镜像导出 runtime 安装包，连同各自的 `.sha256` 与汇总的 `SHA256SUMS` 附到 GitHub Release。
- Dependabot 每周检查 Python、npm、Go、GitHub Actions 与容器镜像依赖；PostgreSQL 大版本升级需要迁移数据卷，不会自动提出。

## 监控与告警

```bash
COREMAN_ENV_FILE=/absolute/path/.env docker compose --project-directory . --env-file /absolute/path/.env -f deploy/docker-compose.yml --profile monitoring up -d prometheus
```

Prometheus 仅绑定 127.0.0.1:9090，保留最多 7 天/1 GB 指标。API 和 runtime `/metrics` 只供容器内部抓取，Caddy 拒绝公网指标路径。运行时数据库指标是共享快照，跨进程聚合使用 max 而非 sum；任务时延、首事件时延、失败与租约计数是各进程独立计数，事务相关计数只在提交后增加，重启归零。relay 标签仅为登记实例 ID，不含员工、消息或凭证。

relay 超过两小时没有新健康上报时，调度器每分钟最多补探测十个登记实例的 `/health`，每次最多五秒；不调用模型，也不覆盖探测期间到达的新上报。

数据库采集有 3 秒总预算和 1.5 秒单语句上限；失败返回 503，不将未知状态冒充零。首事件时延从 relay 请求开始计算。任务时延只包含实际 started 的任务，未启动即取消的任务不进入该直方图。永久失败的当前存量指标与累计失败计数分开。

设置页的「运行告警」选择通知应用和接收人。接收人须为启用、已绑定平台的管理员或 AI 委员会成员。通知通过 outbox 发送，异常持续/恢复状态持久化；重启不重复，撤销收件人或旧告警失效后不再发送。渠道变更会重新评估当前异常。无通知渠道时仍记录状态和指标，默认不向任何真实人员推送。

内置检测覆盖队列持续积压、出站失败、最近十分钟任务失败率、worker 失联收尾、relay 异常、网关断线/认证失败。队列阈值为可运行任务至少 100 或最早任务等待超过 300 秒，并再持续 300 秒；普通网关断线持续 300 秒，认证拒绝或被踢立即触发。任务失败至少五次且比例超过 20%。恢复后通知一次。

数据库整体故障时 CoreMan 无法写通知队列，须由独立监控负责外部送达。Prometheus 的 7 条规则可直接查看；未自动配置外部 Alertmanager 或外部通知凭证。

## 隔离演练

```bash
./scripts/exercise-upgrade
```

需要 Docker 和已安装的开发依赖。脚本清除 TEST_DATABASE_URL 与代理变量，使用临时 PostgreSQL 和模拟平台。覆盖 a/b 接管前后消息与重复事件、worker 在途工作和新任务、双 worker 突发队列、飞书子进程退避、卡片序号接续与发送失败恢复，以及原排空 offset 时序用例。不停本机栈，不发送真实消息。40 项突发队列演练验证并发正确性，不代表生产吞吐上限；连接数规划见「数据库连接预算」。

## 逐机器人生产切换

1. 先确认正式域名、可信来源、登录回调、通讯录映射、机器人可用范围、通知应用与成员授权。按单一企业部署，不跨租户复用 user_id。
2. 确认目标运行时节点已注册且在线，当前员工身份能贯穿代理和业务系统；审核外部 skill 代码和批准的数据源。不得用统一机器人身份冒充发起员工。
3. 验证存储配置和对象下载/过期/回退。跨主机须使用已验收 S3 或可靠共享卷，不能依赖某一容器的本地目录。
4. 一次迁移一个机器人：旧平台停止该机器人，新平台启用，检查唯一租约，再点检文本、图片、文件、选择题、限流切换、定时执行与求助。通知与实际员工交互按组织授权进行。
5. 对照任务、聊天、定时、出站、对象和授权记录核对结果；保留旧配置及回退镜像，不同时启用两个相互争抢的旧/新平台连接。
6. 持续观察真实平台断线、重投、媒体与限流；补做真实应用卡片显示、S3、正式 agent 和日文母语审校。模拟通过不能替代真实平台稳定运行验收。

参考：[Prometheus Python 客户端](https://prometheus.github.io/client_python/)、[官方告警规则](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)、[Prometheus 版本](https://prometheus.io/download/)。
