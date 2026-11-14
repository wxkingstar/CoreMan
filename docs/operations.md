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

从修复这两个问题之前的版本升级时，可能遇到两种卡点：

- `migrate` 长时间停在在线建索引（`CREATE INDEX CONCURRENTLY`）：旧版飞书网关的独占锁连接一直停在事务里，建索引会等它结束。在 PostgreSQL 的 `pg_stat_activity` 里找来自飞书网关、状态为 `idle in transaction` 的连接，临时停止该网关容器，迁移完成后立即启动。新版本网关的守护连接不再持有事务。
- worker 或网关步骤报 `ready` 超时：宿主机休眠后 scheduler 可能把仍在运行的旧进程标记为已停止，就绪检查一直不通过。确认没有在途任务后重启这些旧进程，再重跑同一条升级命令。新版本的心跳会自动恢复这类实例。

只升级部分目标（例如 `upgrade api v2`）成功后同样更新部署标签，其余服务仍运行旧标签；`status` 会提示混合版本，应尽快完成其余目标。

API 升级先重载 Caddy 的指标隔离规则，再迁移并逐台健康升级。worker 先确认另一台就绪，关闭旧容器自动重启，要求旧实例排空，最多等待 7500 秒，再重建；不会因超时强杀在途任务。网关确认备用实例新鲜心跳后排空活跃实例，等待其退出、租约转移且连接 subscribed，随后停止旧服务，并把新的活跃侧写入 `.coreman-gateway-active`；下次反向升级。若两侧均运行，先把备用侧的工作交还活跃侧，再重建备用侧。网关整实例排空按批进行：批间隔 3 秒，每批 bot 数按剩余 bot 与剩余宽限计算（单批最多 10 个），bot 多时也在 120 秒停止宽限内排完，不会落到硬接管。排空期间的自动重启被关闭，避免旧容器退出后又抢回租约；确认排空状态后用 `docker update --restart=unless-stopped <容器>` 恢复预期策略。

### 启动、停止与网关活跃侧

`up` 先迁移，再用 `docker compose up -d --wait` 等待全部服务运行且健康检查通过（默认 300 秒，可用 `COREMAN_WAIT_TIMEOUT` 调整）；超时返回非零，按提示查看 `status` 与 `logs`。`up` 是整体启动/初始化入口，不能替代在途任务期间的滚动升级。配置文件缺失或缺少 `MASTER_KEY`、`POSTGRES_PASSWORD` 时 `up` 会自动生成；若数据卷 `coreman_pgdata`（项目名以 `COMPOSE_PROJECT_NAME` 为准）已存在，新生成的值必然与已有数据不匹配，`up` 拒绝生成并提示恢复原配置文件或自行删除数据卷，不做任何改动。

网关 a/b 同一时刻只有一侧活跃。`up`、`upgrade all` 与 `upgrade gateway` 按 `.coreman-gateway-active` 选择活跃侧；记录缺失时沿用推断：a 侧未运行而 b 侧在运行时为 b，否则为 a。记录与运行容器矛盾（记录的一侧未运行、另一侧在运行）时以运行容器为准并给出提示。宿主机重启后，Docker 按 `restart: unless-stopped` 只恢复上次运行的一侧。

`down` 可传 `-t <秒>`。worker 的停止宽限为 7200 秒，有在途任务时默认会一直等待；只有紧急停机才缩短。

### 轮换 MASTER_KEY

库内凭证密文带密钥标识（`enc:v2:<kid>:…`），所以换密钥不必停机重加密全库：

1. 生成新密钥：`python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())'`。
2. 在 `.env` 里把当前的 `MASTER_KEY` 连同它的 `MASTER_KEY_ID`（默认 `k1`）挪进
   `MASTER_KEYS_PREVIOUS`，格式 `kid:base64`，多把用逗号分隔。
3. 把新密钥写进 `MASTER_KEY`，并给它一个没用过的 `MASTER_KEY_ID`。
4. 重启全部服务。所有进程必须配同一组值，否则一侧写出的密文另一侧读不出来。

此后新写入用新密钥，旧行按各自的 kid 继续读得出来，并随下一次写入自然迁移。轮换前写入的
`enc:v1:` 旧密文没有标识，会逐把尝试，同样读得出来。只有确认库里不再有某个 kid 的密文时，
才能把它从 `MASTER_KEYS_PREVIOUS` 中移除——移早了，那些行会明确报错而不是被错认。

个人助理的数据密钥（`assistant_keys`）写一次就不再改，不会随写入自然迁移。重启全部服务后执行一次
`python -m coreman.cli assistant-keys rewrap`（只统计，输出里的 `stale` 是还由旧主密钥包裹的把数），
确认无误再加 `--apply`。助理的对话内容由数据密钥加密，轮换主密钥不需要重写它们。

会话查看链接不带 kid（只活 24 小时），轮换后已发出的链接失效，重新发起对话即可拿到新链接。

### 状态文件

升级状态不写回含凭证的 .env，保存在仓库根目录（可用 `COREMAN_STATE_DIR` 指定其他目录），均已加入 `.gitignore`：

| 文件 | 内容 |
|---|---|
| `.coreman-image-tag` | 最近一次成功完成的 upgrade/rollback 的镜像标签；首次 `build` 时写入 |
| `.coreman-image-tag.pending` | 进行中或失败后未收尾的升级目标标签 |
| `.coreman-gateway-active` | `wecom=a` / `feishu=b` 形式的网关活跃侧，网关升级完成后更新 |

镜像标签优先级：环境变量 `COREMAN_IMAGE_TAG` > `.coreman-image-tag` > .env 中的 `COREMAN_IMAGE_TAG`。

### 管理命令

管理后台的常用操作（AI 员工、技能、技能审批、业务系统、团队与用户）可以用 `./deploy/coreman bots|skills|approvals|systems|teams|users|api …` 在服务器上执行，见 [管理命令](admin-cli.md)。

### 运行状态

`status` 依次输出 `docker compose ps`、各容器（含已停止的备用网关）的镜像标签与状态、部署标签、未完成的升级、PostgreSQL 客户端连接数与 `max_connections`，最后检查 API `/health`。运行中的 CoreMan 容器使用多个标签时提示混合版本；客户端连接达到 `max_connections` 的 80% 时告警。

## 镜像回退

```bash
./deploy/coreman rollback retained-tag
```

两种镜像都须有该保留标签，预检不通过时不做任何改动。回退使用当前 API 镜像中的排空工具，保持旧任务和租约交接，与升级一样经过 pending 与提升；不会执行数据库 downgrade，仅适用于经过兼容性验证的加表、加列或加索引版本。目标版本须具备实例登记与网关排空能力。回退所需的镜像标签应一直保留，不要用 `build --force` 覆盖。

### 个人助理版本的升级与回退

- 先迁移、再启动新进程。迁移只加列加表，旧代码在新库上照常运行，滚动升级是安全的。
- 还没有任何个人助理记录时，回退代码是安全的。一旦有了助理记录，回退前先把所有助理记录停用：旧代码不认识 `bots.kind`，会把助理记录当成普通机器人去连接，几位所有者的私聊会混在一起。
- 数据库降级（回到个人助理之前的迁移）会删掉助理记录和所有者的数据密钥，助理的加密内容从此读不出来；普通机器人不受影响。
- 改动在用表的迁移（`bots`、`feishu_deliveries`，以及给 `relay_servers`、`runtime_calls`、`runtime_nodes` 加列并新建 `assistant_transcripts` 的节点隔离迁移）设了 5 秒锁超时：表被长事务占着时迁移报错退出、整次回滚，不会让之后对这张表的读写全部跟着排队。等长事务结束后重跑迁移即可，重跑是安全的。
- 节点隔离的版本：先迁移，之后平台与节点谁先升都行。旧节点不上报沙箱能力，永远领不到助理的轮次，绑在它上面的助理暂停（私聊里告知所有者）；旧平台不发沙箱标记，普通节点照常处理普通轮次。助理节点的安装、自检与部署前提见 `runtime_daemon/README.md`「个人助理的沙箱」。
- 只有装成助理专用实例（安装时加 `--assistant-only`，`config.json` 里 `"assistant_only": true`）的守护进程才声明沙箱能力、才跑助理；它拒绝不带沙箱标记的对话请求和名单之外的节点命令。在这一项之前装的助理节点，升级后要在 `config.json` 里补上 `"assistant_only": true` 并重启，否则不再声明能力，绑在上面的助理暂停。
- 一台机器上的所有守护进程都升到本版本之后，再把其中的节点加入助理池或绑给助理：旧守护进程既不报本机指纹也不报 uid，平台看不出它与助理节点同机同用户。
- 平台每轮开轮前核对：助理所在的节点（助理池或所有者的私有运行时）上没有普通员工，同机同 uid 的节点上也没有；不满足时助理暂停、私聊告知所有者（不带节点信息）。升级后若有助理暂停，先查它所在的节点及同机同 uid 的节点上有没有普通员工（含停用的与正在迁过来的）。
- **平台回滚到不认识沙箱标记的版本之前，必须先停用所有助理记录**：旧平台会把助理的轮次当普通轮次派出去，也不做上面那条隔离检查；助理专用实例会拒绝这些请求，所有者只会看到失败。回滚前记下并停用，回滚后（或重新升级后）只启用这批：

  ```sql
  SELECT id FROM bots WHERE kind = 'assistant' AND enabled;
  UPDATE bots SET enabled = false WHERE kind = 'assistant' AND enabled;
  ```

- 开通与停用的版本（迁移 `0093`）：先迁移、再启动新进程，先升平台、再升助理节点。新平台对还没升级的助理专用实例发 `workspace-remove` 会被拒绝，登记待删的目录留着、每隔一阵重试，节点升级后自动删掉；还有目录等着删的运行时实例不能删除（409），删之前先看管理台助理列表里每位助理还剩几个目录。回滚平台之前，先停用所有还没开通完的助理记录（旧平台不认识开通状态，会让它们跑轮次），并等进行中的开通与停用做完：

  ```sql
  UPDATE bots SET enabled = false
   WHERE kind = 'assistant' AND assistant_state IS DISTINCT FROM 'active';
  SELECT count(*) FROM assistant_lifecycles WHERE finished_at IS NULL;
  SELECT count(*) FROM assistant_retired_dirs;
  ```

  降级这一个迁移会先把状态不是 `active`（含状态为空）的助理记录停用，再删掉开通状态、进行中的流程与待删目录的登记。需要手工做的有：之后没删的旧目录要到节点上手工删；停用做到一半的助理，飞书授权可能还没撤销、数据密钥可能还没销毁，降级前先让流程做完；降级后要用这些记录的，自己判断再启用。
- 排查「待删目录一直没删掉」：看 `assistant_retired_dirs` 里这一行的 `error_code`。`busy` 是节点上还有轮次用着目录，稍后自己会删；`dir_unverified` 是节点确认不了这个目录该不该删（共享存储没挂上或报错、节点保管的归属登记缺失或损坏、目录被人手工还原或复制过、没有登记），平台保留这一行、最长每 6 小时重试一次，节点什么都不删。处理：先修好存储；其余情况核对目录确实是那位已停用助理的之后手工删掉目录——登记还在的下一次重试就会答「已删除」，平台自己划掉这一行；登记已坏或不在的，节点一直答「无法确认」，要直接在数据库里删掉这一行（没有管理页面可以删）。节点答「已删除」之后还会留一条墓碑登记 30 天，期间同一路径上后来出现什么节点都不碰；没有登记的目录节点从不删。细节见 `runtime_daemon/README.md`「个人助理的沙箱」。
- 停用后马上重新开通会等约一分十五秒（`key_cooling`，各进程缓存的旧数据密钥过期）再建记录；应用密钥暂时解不开时开通记为 `app_secret_unreadable` 并重试，不放弃；助理名字被别人的记录或普通员工占着时开通失败（`bot_key_taken`），需要管理员处理。
- 只回退节点隔离那一个迁移的数据库降级，会删掉 `assistant_transcripts`（所有助理的下一轮都从新会话开始，已有对话内容在别的表里，不受影响），以及助理池标记、调用的沙箱标记、节点的本机指纹与 uid 两列。节点回滚到旧版本不需要数据库配合。

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

- `CI`（push 与 PR 到 main）：分层 import 门禁、ruff、mypy、pytest（PostgreSQL 16 service，pytest-xdist 按 CPU 数并行、每个 worker 独立库）、前端 lint / 类型检查 / vitest / 构建、Go 驱动 `go vet` 与 `go test -race`、shellcheck、`docker compose config`。
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

### 个人助理列表

管理台「平台管理 → 个人助理」（仅平台管理员）列出每一位助理，数据来自 `GET /api/admin/assistants`。这一页与这个接口只有元数据：看不到任何人的对话、知识库、待跟进、审批单内容、重点群。

| 列 | 接口里的键 | 含义 |
|---|---|---|
| 所有者 | `owner` | 显示名与登录名 |
| 状态 | `state`、`lifecycle` | `provisioning` 开通中、`active` 可用、`deprovisioning` 停用中、`deprovisioned` 已停用；流程进行中时另显示动作、步骤与错误代码 |
| 运行时 | `node`、`relay` | 所在节点的名字与是否在线 |
| 在班 | `on_shift` | 记录启用、开通好，且此刻在班：全天候（默认）随时在班，否则在所有者的工作时段内；「上班」「下班」的临时切换优先 |
| 冷启动 | `cold_start` | `pending` 没开始、`in_progress` 进行中（后面是走到哪一步）、`done` 已结束（后面是怎么结束的：`completed` / `skipped` / `timeout`）；没完成时不盯消息、不交回事项 |
| 今日轮次 | `turns` | 今天（所有者时区）跑了几轮 / 预算；预算是个人值与平台默认里较小的那个，用完后只交回紧急的 |
| 待交回 | `inbox_pending` | 收件箱里还没交回的条目数；一直不降见下一节 |
| 进行中（含调度） | `jobs_active` | 排队或在跑的子任务与调度公共员工的求助合计，两者共用名额，每位助理至多 3 个 |
| 轮询健康 | `watch` | 盯消息的状态、上次成功的时刻与错误代码，对照下一节的表 |
| 待删目录 | `directories_pending_removal` | 换运行时或停用后等原节点删除的旧工作目录数 |

「停用」要输入所有者的登录名确认。停用不可撤销：飞书授权撤销、待批的审批单撤回、数据密钥销毁，这位成员加密保存的个人内容从此不可恢复；接口只回状态。已经停用完的记录没有这个按钮。

可开通范围、允许的 AI 及其先后、每个节点的并发上限、每日轮次预算的默认值在「设置 → 个人助理」里改（设置项 `assistant_eligibility`、`assistant_backends`、`assistant_node_concurrency`、`assistant_daily_turn_budget`）。成员可以在「我的助理 → 设置」里把自己的每日预算调低，不能调到平台默认值以上；之后把平台默认调到某人的个人值以下时，那个人也按较小的生效（个人值留着，调回来之后重新起作用）。

待批提醒（私聊里的「有 N 张审批单等你处理」）与「我的助理」页上的跳转都只用审批系统的网页入口 `TOWER_WEB_URL`；没配就不带链接，不会回落到接口地址 `TOWER_URL`。

### 个人助理盯消息排障

`GET /api/admin/assistants` 每条记录的 `watch` 是这位助理的轮询状态，scheduler 日志里对应的事件名以 `assistant_watch` 开头。日志与这个接口都只有代码、编号与计数，没有消息内容。

| `state` | `last_error` | 含义与处理 |
|---|---|---|
| `polling` | 空 | 正常，`last_ok_at` 是上一次轮询成功的时刻 |
| `idle` | 空 | 所有者下班，或记录不是启用中的开通好状态；不用处理 |
| `idle` | `cold_start_pending` | 冷启动还没做完，做完后自动开始 |
| `idle` | `identity_missing` | 没记下所有者的身份（迁移 `0093` 之前建的助理）：让所有者停用后重新开通 |
| `paused` | `runtime_offline` 等 | 运行时暂停，原因与私聊里说的一致；恢复后自动接着读 |
| `backoff` | `rate_limited` | 被飞书限频，按飞书给的秒数等；持续出现说明一个应用撑不住这么多人 |
| `backoff` | `budget_exhausted` | 这个应用最近一分钟的额度已满，下一分钟再试 |
| `backoff` | `authorization_required` | 所有者的飞书授权失效：需要他重新授权 |
| `backoff` | `personal_grant_required` / `personal_permission_revoked` | 所有者的通讯录身份或授权与开通时记下的不一致，或账号不可用：核对账号，必要时重新开通 |
| `backoff` | `feishu_read_failed` 等 | 飞书读取失败，按 2、4、8、15 分钟退避后重试；同一个会话连续 3 轮读不了会被放下 1 小时（日志 `assistant_watch_chat_parked`），之后恢复正常。一直是它且没有这条日志时，多半是列会话的参数被飞书拒绝 |
| `backoff` | 异常类型名（如 `TimeoutError`） | 平台侧意外出错，15 分钟后重试；看 scheduler 日志里的 `assistant_watch_failed` |
| `stopped` | 空 | 助理已停用 |
| `null`（没有 `watch`） | | 还没被登记：记录不是启用中的开通好状态，或 scheduler 没在跑 |

日志 `assistant_watch_budget_tight`（`app_id`、`load`、`stretch`）表示这个应用最近一分钟的用量到了上限的六成以上，所有人的轮询间隔已被拉长到 `stretch` 倍；偶尔出现不用处理，持续出现需要减少重点群或再接一个助理应用。用量流水在表 `feishu_rate_usage` 里，只留 15 分钟。

日志 `assistant_watch_messages_skipped`（`count`）表示飞书返回的消息里有缺编号或读不出时间的，被跳过；持续出现说明飞书的返回格式与假定的不同。

长时间停止后恢复（暂停、授权失效、`backoff` 了很久）：回看有 7 天的硬上限，停了几天的全部补回，停了两三周的只交回最近 7 天的消息，更早的不补，所有者问「为什么上上周的没提醒」时就是这个原因。

收件箱只进不出（条目一直是 `pending`）说明投递层没在取，不是盯消息的问题。

### 个人助理：冷启动与上下班的排查

- 管理台的助理列表（`GET /api/admin/assistants`）每条记录有 `cold_start`（`state`：`pending` 没开始、`in_progress`、`done`；`step`；`outcome`：`completed` / `skipped` / `timeout`）与 `on_shift`（按设置此刻是否在班）。助理不盯消息时先看这两项：`cold_start.state` 不是 `done` 就还没开始盯；`on_shift` 为 `false` 是下班中（全天候的助理只在发了「下班」之后）。
- `cold_start.state` 长时间停在 `pending`：这位助理是本功能上线前开通的，所有者还没再来私聊；或者它没有记下身份（`cold_start` 为 `null`），需要重新开通。
- `step` 长时间停在 `voice`：平台以所有者身份读飞书失败，每 10 分钟重试，满 1 小时放弃这一步；看 scheduler 日志里的 `assistant_shift`、`assistant_cold_start_failed` 与盯消息的限频日志。
- scheduler 日志 `assistant_shift` 的字段：`voice_queued` / `voice_skipped` / `voice_retry` / `asked` / `focus_skipped` / `timeout`（冷启动），`compile_worked` / `compile_queued` / `compile_idle` / `compile_budget` / `compile_paused`（下班编译）。日志不带任何内容。
- 口吻页没生成：`assistant_cold_start` 那一轮失败不重试（运行时暂停、节点不可用、样本不足）。所有者可以在私聊里让助理按他的要求新建或改写这一页。
- `step` 停在某一题：在等所有者点卡片；满 24 小时自动按默认设置完成。意外出错（接口字段与预期不符等）时日志里是 `failed`，这一位被推后 10 分钟再试，不影响别人，也不影响 24 小时兜底。

### 个人助理投递排障

`GET /api/admin/assistants` 的每条记录有 `turns`（今天的轮次与预算）与 `inbox_pending`（收件箱里还没交回的条目数）。scheduler 的日志 `assistant_delivery` 带各项计数（`assistant_handovers`、`assistant_inbox_settled`、`assistant_followups_due`、`assistant_marks_stale`、`assistant_approvals_auto_withdrawn`、`assistant_recalls_noticed`、`assistant_reauth_reminded`）。

| 现象 | 看哪里 | 说明 |
|---|---|---|
| `inbox_pending` 一直不降 | `watch.state`、所有者的工作时段、`turns` | 运行时暂停（`paused`）、下班、冷启动没做完、所有者的账号停用或飞书身份解绑时不交回，恢复后自动交回；`turns.today` 达到 `budget` 时只交回紧急的，其余次日交回 |
| 条目反复交回不成 | `assistant_inbox.failures`、`void_code` | 带着它的轮次真失败满 3 次后条目作废（`undeliverable`）；先看那几轮任务的 `error_code` |
| `inbox_pending` 不降、又没有在途的交回任务 | `assistant_delivery.handover_misses`、`handover_retry_at` | 上一轮交回没走完（被所有者的消息取消、让路放弃、暂停、失败）之后的等待：60 秒起，连续没走完逐次加倍，最多 15 分钟；到 `handover_retry_at` 之前不排新的一轮，走完一轮清零 |
| 条目绑着任务号不动 | `assistant_inbox.delivered_task_id` 对应的 `tasks` 行 | 那一轮还在排队或在跑（所有者正在和助理聊时会一直让路）；任务结束后 5 秒内由对账记账 |
| 审批单自己变成了已撤回 | `assistant_approvals.error_code` | `owner_replied`、`anchor_recalled`、`anchor_moved` 是平台的自动撤回，助理会在下一轮交回里得知（`anchor_moved` 的跟下一批一起，同一个会话 30 分钟内至多撤一次） |
| 所有者说没收到授权到期提醒 | `feishu_personal_grants.authorized_at`、`assistant_delivery.reauth_reminded_on` | 只在工作时段提醒，每天一次；升级前就连接的授权按建行时间算 |
| 平台代发的回复之后那个会话被压着 | `assistant_sent`、worker 日志 `assistant_sent_record_failed` | 代发消息的编号没记上时，盯消息会把它当成所有者亲自发言，压 10 分钟后恢复 |

调整某个人的预算目前只能改库里 `assistant_settings.daily_turn_budget`（10–20000）；页面入口随「我的助理」页提供。scheduler 停止时日志里出现 `assistant_round_abandoned_on_stop`，表示有一位助理的一轮等了 20 秒仍没做完被放弃：若它恰好刚刷新过令牌，所有者需要重新授权。

### 个人助理访问记录

表 `assistant_access_log` 记运行时路径之外对个人助理内容的每一次解密，没有任何内容列：`bot_id`、`actor_id`（操作人，账号删除后为空）、`actor_kind`（`owner` / `admin` / `system`）、`action`、`target`（范围名或计数）、`at`。它和审计日志（`audit_logs`）是两回事：审计记谁改了平台上的什么，给管理员看；访问记录记谁读了某位所有者的个人内容，给所有者本人看（「我的助理」页的「设置」分区），管理员只在 `GET /api/admin/assistants` 的 `last_non_owner_access_at` 里看到最近一次非本人访问的时间。

| 动作 | 什么时候记 |
|---|---|
| `notes.index`、`notes.read`、`followups.read`、`jobs.read`、`approvals.read`、`settings.read` | 所有者在「我的助理」页上读取；改设置（对象 `update`）与即时上下班（对象 `shift`）的响应带着解密后的设置，也记 `settings.read` |
| `import.preview`、`import.notes`、`export` | 导入的预览（含没写成的确认）、导入写入成功、导出；对象是计数 |
| `chatlogs.read`、`tasks.read` | 所有者在管理台读自己助理的对话记录、任务详情 |
| `cli.chatlogs`、`cli.tasks`、`cli.session` | 有人用管理命令以所有者的身份（`--as`）读对话记录、任务详情、运行时节点上的会话过程（`runtimes session`）；内容没有还原，记成 `system`，不留操作人 |
| `cli.rewrap` | `assistant-keys rewrap --apply` 重新包裹了这位所有者的数据密钥；没有读内容 |

- **保留。** 180 天；scheduler 的保留期那一档分批清理，计数在日志里是 `old_assistant_access_log`。助理记录被删除时它的访问记录一并删除。
- **写不进去不挡读取。** 记录与读取在同一个事务里提交：提交不了，请求以 500 结束，内容不交出去。单独这一行写失败时（应用日志 `assistant_access_log_failed`，带助理编号、动作与错误类型）读取照常返回；每次解密另有一条应用日志 `assistant_content_access`（助理、操作人、类别、动作、对象），可以拿来补查。持续出现 `assistant_access_log_failed` 说明表不可用，要当故障处理。
- **管理命令看不到助理的内容，这是有意的。** `coreman … --as 登录名` 以那位成员的身份建一个管理台会话（`admin_sessions.auth_method = 'cli'`），权限与他一致，但敲命令的人在服务器上，不是他本人。所以：`/api/me/assistant` 下的接口不受理这种会话（403 `cli_session_refused`），所有者的知识库、待跟进、导入导出只能由他本人在管理台里操作；经它读对话记录（`/api/admin/chat-logs`）与任务详情时，助理加密保存的内容不还原，输出与「不是所有者」时一样（`content_locked`），并给所有者留一条 `cli.chatlogs` / `cli.tasks`。助理调度公共员工留在目标员工名下的那一轮虽然是明文，对这种会话同样只给元数据（`content_withheld`，按正文搜索与统计不含它，开发页的任务详情不给流与报错文字），同样在所有者的助理名下留一条记录（对象 `dispatch=N`）。运行时节点上的会话过程（`coreman runtimes session`、会话查看页与它的事件流）也一样：这种会话不按「本人」放行，只剩角色那条路（群聊、企微私聊），以所有者的身份去取调度那一轮会被拒绝并记 `cli.session`。个人助理自己的会话在这个入口对谁都不开（所有者本人、管理员、带凭据的旧链接都是 404），助理的回复里也不带会话入口。事件流每次放行都记审计 `runtime.session_events`（与会话页的 `runtime.session_view` 同样的字段）。普通员工的其它记录不受影响。排障需要看助理的内容时，请所有者本人在管理台里看。不看内容的命令（`assistant-keys rewrap`）照常可用，执行时记 `cli.rewrap`。
- **上传的大小。** 导入的压缩包上限 20MB：应用读上传时按块读，一过上限就停并回 413。但框架在此之前已经把请求体收到了临时文件里，所以要挡住「传上来」本身，请在反向代理上给 `/api/me/assistant/import` 设 25MB 左右的请求体上限。装满的知识库导入、导出要十几秒，代理的超时不要低于 180 秒（管理台对这两个请求的超时就是 180 秒）。
- **排查。** 所有者问「谁看过我的内容」：让他看「设置」分区的访问记录；管理员一侧只能确认时间。直接连库、拿主密钥解密不经过平台，不会留下记录（见设计文档的威胁模型）。

## 隔离演练

```bash
./scripts/exercise-upgrade
```

需要 Docker 和已安装的开发依赖。脚本清除 TEST_DATABASE_URL 与代理变量，使用临时 PostgreSQL 和模拟平台。覆盖 a/b 接管前后消息与重复事件、worker 在途工作和新任务、双 worker 突发队列、飞书子进程退避、卡片序号接续与发送失败恢复，以及原排空 offset 时序用例。不停本机栈，不发送真实消息。40 项突发队列演练验证并发正确性，不代表生产吞吐上限；连接数规划见「数据库连接预算」。

## 逐机器人生产切换

1. 先确认正式域名、可信来源、登录回调、通讯录映射、机器人可用范围、通知应用与成员授权。按单一企业部署，不跨租户复用 user_id。
2. 确认目标运行时节点已注册且在线，当前员工身份能贯穿代理和业务系统；审核外部 skill 代码和批准的数据源。不得用统一机器人身份冒充发起员工。
3. 验证存储配置和对象下载/过期/回退。跨主机须使用经过验证的 S3 或可靠共享卷，不能依赖某一容器的本地目录。
4. 一次迁移一个机器人：旧平台停止该机器人，新平台启用，检查唯一租约，再点检文本、图片、文件、选择题、限流切换、定时执行与求助。通知与实际员工交互按组织授权进行。
5. 对照任务、聊天、定时、出站、对象和授权记录核对结果；保留旧配置及回退镜像，不同时启用两个相互争抢的旧/新平台连接。
6. 持续观察真实平台断线、重投、媒体与限流，并在真实应用中核对卡片显示与对象存储。模拟测试通过不能代替真实平台上的稳定运行。

参考：[Prometheus Python 客户端](https://prometheus.github.io/client_python/)、[官方告警规则](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)、[Prometheus 版本](https://prometheus.io/download/)。
