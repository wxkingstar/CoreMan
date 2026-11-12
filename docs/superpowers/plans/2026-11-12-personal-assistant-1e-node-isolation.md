# 个人助理 1E：节点隔离 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `bots.kind='assistant'` 的每一轮只在声明了沙箱能力的节点上、在 bubblewrap 沙箱里运行：只看得见自己的工作目录、本轮的临时目录与 CLI 配置目录，只读挂 CLI 程序与一份去掉刷新令牌的登录凭证，出网只通平台与模型接口；一轮结束后 CLI 转录经 TLS 上传平台、用所有者的数据密钥加密存入 `assistant_transcripts`，节点上随即删除，续聊前再下发还原。助理绑定在助理池（`relay_servers.assistant_pool`）或所有者本人的私有运行时上，所在节点不可用时暂停并告诉所有者，不回落。普通员工（`kind='standard'`）的请求体、节点上的启动方式、领取顺序都不变。

**Architecture:** 节点侧全部在 Python 守护进程里：平台的请求体带 `sandbox: true` 与 `transcript: {"transfer", "restore"}` 时，`Daemon.execute` 把这一轮交给 `runtime_daemon/assistant_turns.py`，它为这一轮建目录、收窄凭证（`turn_credentials.py`）、按交接编号取回并还原转录（`transcripts.py`）、起一个只放行名单内「主机:端口」的 CONNECT 代理（`egress.py`），再用 bubblewrap（`sandbox.py`）在断网的命名空间里起「转发口 + 驱动」（`sandbox_bridge.py` 看护现成的 Go 驱动），把请求转进去、把流转出来；流里的 `data: [DONE]` 扣住，等沙箱停下、转录上传完才放出。Go 驱动与普通轮次的常驻驱动一行不改。守护进程启动时与每 30 分钟按前置验证脚本的同一套检查做一次自检，结果按 AI 写进心跳的 `assistant_sandbox_v1`；没声明的节点（旧版本、macOS、容器里没有用户命名空间、没装 bubblewrap）收不到助理的轮次，收到了也拒绝。平台侧：`core/assistants/runtime.py` 判定助理能不能在所绑的实例上跑、开通时从池里挑节点；worker 在开轮前先过这道关（`worker/chat/assistant_runtime.py`），开流时由 `core/assistants/transcripts.py` 定下交接编号与续不续；节点经两个新接口取、交转录；`sealed` 新增二进制内容列，转录与审批单等其他内容列走同一套按人密钥。节点领取调用时普通调用先领，助理调用按设置 `assistant_node_concurrency` 限并发，超出的留在队列里。

**Tech Stack:** 守护进程：Python 3.10+ 标准库（asyncio、tarfile、socket）、httpx、bubblewrap；平台：Python 3.12、FastAPI、SQLAlchemy async、Alembic、PostgreSQL、httpx；测试：pytest（真起沙箱的用例在没有 bubblewrap 的机器上自动跳过）。Go 驱动不改。

**Spec:** `docs/superpowers/specs/2026-11-11-personal-assistant.md`（「原则」「威胁模型」「三、节点隔离」「十、开通、冷启动与停用」「十一」「不做」「测试」「前置验证」）；总览 `docs/superpowers/plans/2026-11-11-personal-assistant-phase1.md`（1E 一行、接口表、「步骤 0」）

## Global Constraints

- 开工前合并最新 `main`。写本计划时迁移头是 `0090`，本计划的迁移写作 `0091`、`down_revision = "0090"`；**开工时按当时迁移头顺延**，同步改文件名、`revision` 与 `down_revision`。
- 公开仓：代码、测试、文档、提交信息只用通用占位（`example.com`、`demo-bot`、张三 / 李四），提交前跑仓库约定的敏感词自查。
- 普通员工（`kind='standard'`）不受影响，具体是：
  - 平台发给节点的请求体逐字不变（`ChatRequest.to_body` 只在 `sandbox` 为真时多两个键，Task 13 有逐键断言）；
  - 节点上普通轮次的路径逐字不变：仍由常驻驱动、同样的参数、环境、工作目录与配置目录处理（Task 7 有用例把请求体转给一个假驱动核对）；`"sandbox"` 键不在请求里时一行新代码都不经过；
  - 节点领取调用时普通调用先领、数量上限不变，助理调用只用剩下的名额（Task 11）；
  - 助理侧的任何失败（自检、代理、凭证、转录上传下载、清扫、并发满）只结束这一轮或只影响下一次清扫：有各自的 try/except 与超时，不占普通轮次的锁、队列名额、驱动或数据库事务（各任务的「普通机器人不受影响」一栏写明由哪条测试保证）。
- 失败一律不跑（fail closed）：节点没声明能力、自检没过、bubblewrap 起不来、凭证快到期或读不懂、转录取不下来、并发已满，这一轮都不跑；转录传不上去就不拿旧的那份续；解不开、密钥已销毁、超过 50MB 都从新会话开始。
- 日志与告警只带代码、编号、大小与异常类型名；不记主机名、路径、命令输出与内容。
- 守护进程代码要能在 Python 3.10 上跑（不用 `asyncio.timeout`、`TaskGroup`，`asyncio.TimeoutError` 与 `TimeoutError` 一起捕获）；`runtime_daemon/sandbox_bridge.py` 只用标准库（它以 `python -I` 在沙箱里运行）。
- 仓库里有几个文件本来就不符合 `ruff format`（如 `coreman/core/feishu_docs/write_policy.py`）：**只对本任务改动的文件**跑 `uv run ruff format <文件>`，不要对整个目录格式化。
- 本计划不做：
  - 开通与停用流程（1F）：开通时调 `runtime.pick_pool_relay` 选节点、停用时清工作目录，都在 1F；本计划只提供函数。
  - 每人每日轮次预算：推迟到第 2 期（见「设计选择」第 10 条与「待所有者确认的取舍」）。
  - OpenCode 的助理轮次：守护进程不为它声明能力（「设计选择」第 11 条）。
  - 管理台页面：助理池开关只提供接口（Task 12）。
  - Go 驱动的任何改动。
- 验证命令：`uv run pytest <路径>`、`uv run ruff check .`、`uv run mypy coreman`、`uv run alembic check`（`TEST_DATABASE_URL` 按本机配置）。

## 设计选择

**1. 沙箱从哪里起：守护进程为每一轮在 bubblewrap 里另起一个驱动进程。**

| 做法 | 取舍 |
|---|---|
| Go 驱动在 `exec.Command("claude", …)` 外面包一层 bwrap | 没有采用：驱动是三种 AI 共用的长驻进程，它的会话日志（`--sessions-dir` 下逐条记请求与回复）、Codex 的会话→线程对应、附件目录都在沙箱外，照样是明文；改 Go 还会碰到普通轮次的启动路径 |
| 每个助理一个长驻的沙箱驱动 | 没有采用：明文在节点上活过一轮；助理一多，常驻进程数随人数增长 |
| **守护进程每轮在沙箱里起「转发口 + 驱动」，驱动的 socket 与会话目录都在本轮目录里** | **采用**。Go 一行不改，普通轮次的常驻驱动完全不碰；驱动自己的状态也在沙箱里、随本轮删除；参数是纯函数，能在任何平台上单元测试。代价：每轮多起一个驱动进程（Go 二进制，启动几十毫秒）；会话查看页在一轮结束后没有内容可看 |

**2. 文件系统：白名单挂载，而不是「整盘只读再遮住几处」。** 前置验证脚本用的是后者（`--ro-bind / /` 再用 tmpfs 遮住转录目录），检查方便，但节点上没被遮住的一切（别的用户的家目录、`/opt`、挂进来的网络存储）都看得见。本计划只只读挂入：系统目录（`/usr`、`/bin`、`/lib*`、证书与 `passwd` 等少数 `/etc` 文件，软链照原样建软链）、本安装的发布目录（驱动与转发口）、Python 的 `sys.base_prefix`、CLI 程序所在目录（npm 包挂整个包，入口是 `#!` 脚本时再挂 `node` 所在目录）、配置里额外给的 `assistant_sandbox_ro_paths`。可写的只有：工作目录（挂在 `/work`）、本轮的家目录、临时目录、配置目录、驱动会话目录与 socket 目录。代价是 CLI 的装法要能被找到，自检里在沙箱中跑一遍 `<cli> --version` 来证明。

**3. 工作目录挂在固定的 `/work`，CLI 配置目录每轮新建。** Claude 的转录按工作目录路径分目录（`projects/-work/<会话>.jsonl`），固定路径让转录在任何节点上还原都续得上（改绑节点、共享存储都一样）。设计写的是「本助理的 CLI 配置目录」，本计划做成每轮新建、轮次结束删除：两种 CLI 的配置目录里还有提示历史（Claude 的 `history.jsonl` 与 `.claude.json`、Codex 的 `history.jsonl`），长期保留就违反「明文只在一轮运行期间」；续聊需要的部分（会话转录、Codex 的线程对应）由平台保管、每轮还原。这是比设计更严的做法，列在「待所有者确认的取舍」。

**4. 出网：断网命名空间 + 沙箱里的转发口 + 守护进程的放行代理。**

| 做法 | 取舍 |
|---|---|
| 不限出网 | 不可接受：被注入的助理能把读到的东西直接发出去 |
| 按 cgroup 用 nftables 放行 | 没有采用：要 root 或 CAP_NET_ADMIN，守护进程以普通用户运行 |
| **`--unshare-all` 断网；沙箱里 `127.0.0.1:18080` 的转发口经挂进来的 Unix socket 接到守护进程的 CONNECT 代理；代理只放行本轮名单里的「主机:端口」** | **采用**。名单 = 控制面地址的主机 + 本轮所有 `COREMAN_*_URL` 的主机（都只认 https）+ 这种 AI 的模型接口地址（Claude 是 `api.anthropic.com`，Codex 是 `chatgpt.com`、`api.openai.com`，可在 config.json 的 `assistant_egress_hosts` 按 AI 覆盖）。OAuth 刷新地址不在名单里。上游代理沿用节点配置：平台地址走 `control_proxy`，模型接口走 AI 代理。不解 TLS、不看内容，日志只记次数。代价：CLI 必须遵守 `HTTPS_PROXY`（两种 CLI 都声称支持，列在「仍需真机核实」）；平台必须是 https 地址 |

**5. 模型登录凭证：每轮一份去掉刷新令牌的只读副本。** 节点上的 CLI 登录由所有员工共用。OAuth 的刷新令牌可能是一次性的（轮换式刷新令牌是 OAuth 的常见做法；两种 CLI 的文档都没有承诺可以重复使用，本计划按一次性对待，真机核实见文末）。如果沙箱里的 CLI 拿着只读挂进来的原文件去刷新，新令牌写不回去、旧的又已作废，整台节点的登录就坏了，普通员工一起受影响。

| 做法 | 取舍 |
|---|---|
| 只读挂原登录文件（前置验证脚本的做法） | 不可接受：见上 |
| 守护进程在沙箱外自己刷新 | 没有采用：要实现两家的 OAuth 客户端，还要和同一台节点上 CLI 自己的刷新抢同一个文件 |
| **每轮从当前席位的登录文件收窄出一份副本：Claude 只留 `claudeAiOauth` 里除 `refreshToken` 以外的字段（其它顶层键如 MCP 的 OAuth 令牌不带）；Codex 去掉 `tokens.refresh_token`、把 `last_refresh` 改成现在（API Key 登录原样带）。副本 0600 写在本轮目录里不挂进沙箱的位置，再只读盖到配置目录的同名文件上；访问令牌剩余不足 30 分钟就不起这一轮（`credential_expiring`）** | **采用**。沙箱里的 CLI 无从刷新（既没有刷新令牌，刷新地址也连不出去）；刷新留给沙箱外的普通轮次与健康检查，它们照常写回节点的登录文件。节点用环境变量登录（`CLAUDE_CODE_OAUTH_TOKEN`、`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`）时只带那一个变量 |

被注入的助理在一轮里读得到这份副本。它拿到的是一枚到期即失效的访问令牌；出网只通平台与模型接口，带不到别处去：能带出去的路只剩把令牌写进发给平台的内容（知识库、审批单——审批单要所有者本人批准才会发出），或拿它在模型接口上消耗额度。这一点写进部署文档。哪些是读代码与文档得来的、哪些要真机验证，见「仍需真机核实」。

**6. 转录的交接：交接编号 + 两个节点接口，[DONE] 等上传完再放出。**

| 做法 | 取舍 |
|---|---|
| 转录放进请求体下发、放进响应帧回传 | 没有采用：请求体经 `runtime_calls.request_enc` 落库、上限 64 MiB，50MB 的转录 base64 后就超了；响应帧由 worker 逐块消费，不该夹带一个大文件 |
| **派出一轮前平台在 `assistant_transcripts` 上写一个随机的交接编号（`pending_transfer`，连同所派节点）；请求体带 `transcript: {"transfer": <32 位十六进制>, "restore": <布尔>}`；节点凭编号 `GET` / `PUT /api/runtime/assistant-transcripts/{transfer}`（节点令牌鉴权、编号与节点都对得上才认）** | **采用**。大小只受转录上限约束；编号一次有效 |

规则（Task 9 有逐条用例）：

| 情况 | 平台怎么做 |
|---|---|
| 新会话 | 不还原；写编号 |
| 老会话、上一轮的编号已清、库里有转录、密钥可用 | 还原（`restore: true`） |
| 老会话、上一轮的编号还在（转录没传回来） | **不拿旧的续**：删掉这一行，换一个新的 relay 会话 |
| 老会话、没有转录（上一轮是 `empty`、`too_large`，或这一行不在了）、或密钥不可用 | 同上，换新会话 |
| 节点上传 `ok` + 内容 | 加密存入，清编号 |
| 节点上传 `empty` / `too_large` | 清掉内容，清编号；下一轮因此换新会话（设计「超过上限结束该会话」） |
| 节点上传 `unchanged`（这一轮被节点拒绝了，CLI 没跑） | 只清编号，上一份照旧可续 |
| 编号对不上（过期、别的节点、已办完） | 410，节点不必再传 |
| 下载时解不开或密钥已销毁 | 404，节点从新会话开始（`sealed.tolerate_undecryptable`，不报 500） |

节点侧：流里的 `data: [DONE]` 先扣住，沙箱停下、转录上传完（或确定传不上）才放出。平台见到 `[DONE]` 才收尾这一轮，所以下一轮开流时上一轮的结果一定已经落定，不会因为时序把一个正常的会话判成「没传回来」。上传失败的本轮目录留到下一次清扫（启动时一次，之后每 10 分钟）：清扫再传一次，然后无论成败都删除——明文在节点上最多再留一个清扫周期。被取消的一轮放到后台去传。

**7. 加密：`sealed` 新增二进制内容列。** 转录是 tar.gz（二进制），现有的三种内容列都是文本或 JSON。base64 后塞进文本列要多占 80% 的空间与内存；在 `sealed` 之外另做一套加密就失去了台账、销毁即锁定、`tolerate_undecryptable` 这些现成的保证。所以 `Cipher` 加 `encrypt_bytes` / `decrypt_bytes`（`enc:v2:<kid>:` 加原始密文字节），`sealed` 加 `Blob` 一种列；经 ORM 写入由 flush 钩子加密，`session.get` / `session.scalar` 取出自动解密，另一代数据密钥加密的按已锁定处理。AAD 同其他内容列：`assistant_transcripts.blob_enc:<助理记录编号>`。

**8. 能力闸门与混合版本。**

| 组合 | 结果 |
|---|---|
| 新平台 + 新守护进程、自检通过 | 助理轮次在沙箱里跑 |
| 新平台 + 新守护进程、自检没过（macOS、容器、没装 bubblewrap、CLI 在沙箱里起不来） | 不声明能力：平台在开轮前就让助理暂停并告诉所有者；即便有请求到了，守护进程也回 503 `sandbox_unavailable` |
| 新平台 + 旧守护进程 | 旧守护进程不上报 `assistant_sandbox_v1`，平台按没有处理，同上暂停。**这一条是必须的**：旧守护进程不认识 `sandbox` 字段，会把请求原样交给常驻驱动、在沙箱外跑 |
| 旧平台 + 新守护进程 | 旧平台丢掉心跳里不认识的键；它也不发 `sandbox`，新守护进程走原来的路径。旧平台（1D 及以前）会把助理的轮次当普通轮次派出、在沙箱外跑——与 1E 之前相同 |
| 平台回滚到 1E 之前 | 同上一行。所以回滚前要先停用助理记录，回滚后再启用（Task 16 的部署文档写了步骤与查询） |

**9. 分配与并发。** `relay_servers.assistant_pool` 标记助理池；`runtime.pick_pool_relay(session, provider)` 在池里这种 AI、现在能跑助理的实例中选所在节点上助理最少的一个（同数按编号），供 1F 开通时调用。之后每轮都不重新选（设计「不做」）：所在节点离线、排空、失去沙箱能力时这一轮不跑，回一句「你的个人助理暂停了：……」；私有运行时离线同样暂停、不回落到池。所有者改绑只能绑到池里或本人的私有运行时，且那台要现在就能跑（`switch_relay.validate_switch` 里加一道校验）。并发上限在节点领取时执行：`runtime_calls.sandbox` 标出助理的调用，`_collect` 先按原来的条件领普通调用，有剩余名额且节点声明了能力，才按设置 `assistant_node_concurrency`（默认 2）领助理调用。超出的留在队列里，由调用方现成的排队上限（120 秒）等待，等不到时 worker 告诉所有者「运行时繁忙」。守护进程另有一道保险 `assistant_max_concurrent`（config.json，默认 8），超出直接回 503，正常情况下碰不到。

**10. 每人每日轮次预算推迟到第 2 期。** 设计的说法是「用完后非紧急事项攒到次日」：「紧急」由第 2 期的盯消息分级定义（立即交回 / 攒批 / 压着 / 忽略），第 1 期的轮次全是所有者本人发起的私聊与审批结果回合，没有可以攒的东西；现在做只能一刀切地拒绝所有者本人的请求。第 2 期和分级一起做：按人计数放在交回投递处，用完后攒批与压着的条目改到次日投递并私信告知，立即交回的不受限。

**11. 只支持 Claude 与 Codex。** OpenCode 的凭证、模型接口地址与会话库（`OPENCODE_DB` 在席位外共用）都另成一套，设计也只要求「两种 CLI」。守护进程对 OpenCode 恒不声明能力，绑到 OpenCode 上的助理会暂停。

**12. 工作目录的安全规则进提示词。** 追加在审批规则之后（同在稳定段，每轮逐字不变）：工作目录只放规则、经验总结和技能，不放人名、聊天、审批内容与任何个人信息，也不写进文件名；下载的文档与附件放 `$TMPDIR`，一轮结束即删除。

**13. 助理池节点只承载助理。** 普通员工不在沙箱里，与助理是同一个系统用户：它们的 CLI 能读守护进程数据目录下这一轮的临时目录、转录与凭证副本。沙箱只能挡住「助理看别人」，挡不住「别人看助理」。所以把实例加入助理池时，要求节点上没有启用的普通员工（Task 12）；私有运行时上放什么由所有者自己决定。这一条与设计「威胁模型」的「同一节点上的其他 AI 员工不能看到」只在这个前提下成立，见文末「对设计文档的意见」。

## File Structure

| 文件 | 职责 |
|---|---|
| `runtime_daemon/sandbox.py`（新） | 本轮目录布局、bubblewrap 参数、沙箱环境变量、CLI 挂载、启动自检 |
| `runtime_daemon/sandbox_bridge.py`（新） | 沙箱里的第一个进程：127.0.0.1 转发口 + 看护驱动（只用标准库） |
| `runtime_daemon/egress.py`（新） | 每轮的放行代理（CONNECT，名单内的「主机:端口」，可串上游代理） |
| `runtime_daemon/turn_credentials.py`（新） | 从当前席位的登录收窄出本轮的凭证副本 |
| `runtime_daemon/transcripts.py`（新） | 转录打包与安全还原 |
| `runtime_daemon/assistant_turns.py`（新） | 沙箱里的一轮、交接、[DONE] 扣留、清扫、自检循环 |
| `runtime_daemon/daemon.py` | 分流 `sandbox` 请求、上报能力、启停后台循环 |
| `migrations/versions/0091_assistant_transcripts.py`（新） | 表 `assistant_transcripts`；列 `relay_servers.assistant_pool`、`runtime_calls.sandbox` |
| `coreman/core/crypto.py` | `Cipher.encrypt_bytes` / `decrypt_bytes` |
| `coreman/core/assistants/sealed.py` | 二进制内容列 `Blob`、登记 `AssistantTranscript.blob_enc`、`key_ready` |
| `coreman/core/db/models/assistants.py`、`relay.py`、`runtime_nodes.py`、`__init__.py` | 模型与新列 |
| `coreman/core/assistants/transcripts.py`（新） | 交接：`prepare` / `download` / `upload` / `purge` |
| `coreman/core/chat/sessions.py` | `renew`：给会话键换一个新的 relay 会话 |
| `coreman/core/assistants/runtime.py`（新） | 能不能跑、能不能绑、暂停文案、池里选节点、节点上有没有普通员工 |
| `coreman/core/bots/switch_relay.py` | 助理改绑的校验 |
| `coreman/api/routers/runtime_protocol.py` | 心跳能力字段、转录两个接口、领取顺序与助理并发 |
| `coreman/core/runtime_nodes/transport.py` | 请求扩展 `coreman.sandbox` → `runtime_calls.sandbox` |
| `coreman/core/settings_schema.py` | 设置项 `assistant_node_concurrency` |
| `coreman/api/routers/relay_servers.py` | 助理池开关接口；列表带 `assistant_pool`、`assistant_sandbox` |
| `coreman/core/relay/client.py` | `ChatRequest.sandbox` / `transcript`；流式请求带扩展 |
| `coreman/runtime/worker/chat/assistant_runtime.py`（新） | 开轮前的暂停判断与回复 |
| `coreman/runtime/worker/chat/handler.py`、`opening.py` | 接上暂停判断与交接 |
| `coreman/core/assistants/workspace_prompt.py`（新）、`coreman/runtime/worker/chat/personal.py` | 工作目录规则进提示词 |
| `coreman/runtime/scheduler/service.py` | 每轮清理过期转录 |
| `tests/integration/assistant_helpers.py` | `into_assistant` 之后让助理所在节点可用 |
| `tests/conftest.py`、`tests/unit/test_assistant_sealed_codec.py`、`tests/unit/test_assistant_sealed_ledger.py`、`tests/unit/test_settings_schema.py` | 清表顺序、登记全集、台账、设置项全集 |
| `runtime_daemon/README.md`、`docs/feishu.md`、`docs/superpowers/specs/2026-11-11-personal-assistant.md` | 部署要求、升级与回滚、设计同步 |

---


### Task 0: 步骤 0——在候选节点上实测沙箱，结论写回设计文档

本计划按预期结果设计（bubblewrap + 非特权用户命名空间 + 断网命名空间 + 本机放行代理），并让节点在运行时自证（Task 1 的自检、Task 7 的能力上报），所以这一步**不阻塞**后面的任务；但它的结论决定哪些节点能进助理池，结论与预期不符时按文末「步骤 0 结果与本计划的对应」改对应任务。

**Files:**
- Modify: `docs/superpowers/specs/2026-11-11-personal-assistant.md`（「前置验证」表）

**Interfaces:**
- Consumes: `scripts/probe-runtime-sandbox.sh`（已在仓库里，只读、不需要 root、不打印凭证内容）
- Produces: 设计文档「前置验证」表里第 2、3、4 项的结论

- [ ] **Step 0.1: 选出候选节点**

候选节点 = 准备加入助理池的共享 Runtime 节点，加上已知会被所有者改绑的私有 Runtime。每台记下：操作系统、是否在容器（chroot / nspawn）里运行守护进程、工作目录是否在网络存储上。macOS 节点与没有用户命名空间的容器节点不用测（它们不会声明能力，见「设计选择」第 8 条），直接记为「不能」。

- [ ] **Step 0.2: 在每台候选节点上跑探测脚本**

以守护进程所用的系统用户运行，参数是全部员工工作目录的上级目录（节点 `config.json` 的 `workspace_root`）：

```bash
bash probe-runtime-sandbox.sh /path/to/workspace-root > probe-$(hostname -s).txt 2>&1
```

按总览「步骤 0」的判定表判读（`bwrap=MISSING` / `T1 FAIL`、`*_visible=0`、`write_outside=denied`、T3 / T4、T5、`cgroup_fs` / `nft`）。

- [ ] **Step 0.3: 再测一次「去掉刷新令牌的只读凭证」**

探测脚本只读挂的是原登录文件；本计划挂的是去掉刷新令牌的副本（「设计选择」第 5 条）。在同一台节点上、同一个用户下运行（不打印凭证，只打印结果标志与文件摘要）：

```bash
/bin/bash -c '
set -u
W="$(mktemp -d)"; trap "rm -rf \"$W\"" EXIT
mkdir -p "$W/work" "$W/claude" "$W/codex"
CLAUDE_CRED="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json"
CODEX_AUTH="${CODEX_HOME:-$HOME/.codex}/auth.json"
before=$(sha256sum "$CLAUDE_CRED" "$CODEX_AUTH" 2>/dev/null)
python3 - "$CLAUDE_CRED" "$W/claude/.credentials.json" "$CODEX_AUTH" "$W/codex/auth.json" <<"PY"
import datetime, json, os, sys
src_c, dst_c, src_x, dst_x = sys.argv[1:5]
if os.path.exists(src_c):
    oauth = json.load(open(src_c))["claudeAiOauth"]
    oauth.pop("refreshToken", None)
    json.dump({"claudeAiOauth": oauth}, open(dst_c, "w"))
if os.path.exists(src_x):
    data = json.load(open(src_x))
    if isinstance(data.get("tokens"), dict):
        data["tokens"].pop("refresh_token", None)
        data["last_refresh"] = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    json.dump(data, open(dst_x, "w"))
PY
ARGS=(--ro-bind / / --dev /dev --proc /proc --tmpfs /tmp
      --bind "$W/work" "$W/work" --bind "$W/claude" "$W/claude" --bind "$W/codex" "$W/codex"
      --chdir "$W/work" --unshare-pid --die-with-parent
      --setenv CLAUDE_CONFIG_DIR "$W/claude" --setenv CODEX_HOME "$W/codex")
[ -f "$W/claude/.credentials.json" ] && { chmod 0400 "$W/claude/.credentials.json"; bwrap "${ARGS[@]}" claude -p "Reply with exactly: ok" --output-format json --max-turns 1 </dev/null | grep -o "\"is_error\":[a-z]*" | head -1; }
[ -f "$W/codex/auth.json" ] && { chmod 0400 "$W/codex/auth.json"; bwrap "${ARGS[@]}" codex exec --skip-git-repo-check --json "Reply with exactly: ok" </dev/null >/dev/null 2>&1; echo "codex_exit=$?"; }
after=$(sha256sum "$CLAUDE_CRED" "$CODEX_AUTH" 2>/dev/null)
[ "$before" = "$after" ] && echo "node_login_unchanged=yes" || echo "node_login_unchanged=NO"
'
```

预期：`"is_error":false`、`codex_exit=0`、`node_login_unchanged=yes`。

- [ ] **Step 0.4: 补测 Claude 转录还原后的续聊**

按总览「步骤 0」的 0.3 做（在任一装有 Claude 命令行且登录有效的机器上）。

- [ ] **Step 0.5: 写回设计文档并提交**

在「前置验证」表里替换「尚未完成」清单中的第 2、3、4 项，各记一行：结论、依据（第几台节点、哪天；节点只写「共享节点甲」「私有节点乙」，不写主机名与地址）。与本计划的预期不符的，按文末「步骤 0 结果与本计划的对应」在同一次提交里注明要改哪些任务。

```bash
git add docs/superpowers/specs/2026-11-11-personal-assistant.md
git commit -m "个人助理：节点沙箱的前置验证结论"
```

---

### Task 1: 本轮目录布局、bubblewrap 参数与启动自检

纯计算的部分（目录、参数、环境变量、CLI 挂载）在任何平台上都能测；真起沙箱的自检用例只在装了 bubblewrap 的 Linux 上跑，其余平台自动跳过。

**Files:**
- Create: `runtime_daemon/sandbox.py`
- Test: `tests/unit/test_assistant_sandbox.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - 常量 `CAPABILITY = "assistant_sandbox_v1"`、`PROVIDERS = ("claude", "codex")`、`INSIDE_WORK = "/work"`、`INSIDE`（沙箱里的固定路径）、`BRIDGE_PORT = 18080`、`PROXY_URL`、`CONFIG_ENV`、`CREDENTIAL_FILE`
  - `TurnLayout.new(turns_dir: Path, socket_dir: Path) -> TurnLayout`；属性 `turn_id`、`root`、`run`、`secrets`、`driver_socket`、`egress_socket`、`meta`；方法 `host(name) -> Path`、`create()`、`remove()`
  - `system_mounts(paths=SYSTEM_PATHS) -> list[str]`、`package_root(executable: Path) -> Path`、`cli_mounts(provider, which=shutil.which) -> list[Path]`、`link_cli(layout, provider, which=shutil.which) -> bool`
  - `sandbox_env(provider: str, base: Mapping[str, str], extra: Mapping[str, str]) -> dict[str, str]`
  - `Launch(bwrap, layout, workdir, provider, env, ro_paths=(), credential=None, system=system_mounts())`、`bwrap_argv(launch, command) -> list[str]`、`bridge_command(python, bridge, command) -> list[str]`、`driver_command(binary) -> list[str]`
  - `SelfTestInputs(turns_dir, socket_dir, release, hidden, clis, extra_ro=(), python=sys.executable, system=…)`、`SelfTestResult(providers: dict[str, bool], code: str)`、`self_test(inputs, *, bwrap=None, system_name=None, runner=subprocess.run) -> SelfTestResult`

- [ ] **Step 1: 写失败测试**

`tests/unit/test_assistant_sandbox.py`：

```python
"""助理沙箱的参数与自检：参数逐项可查；真起沙箱的用例只在装了 bubblewrap 的 Linux 上跑。"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from runtime_daemon import sandbox

HAS_BWRAP = platform.system() == "Linux" and shutil.which("bwrap") is not None
RELEASE = Path(__file__).resolve().parents[2]


def layout(tmp_path: Path) -> sandbox.TurnLayout:
    turns = tmp_path / "turns"
    sockets = Path(tempfile.mkdtemp(dir="/tmp"))
    built = sandbox.TurnLayout.new(turns, sockets)
    built.create()
    return built


def test_layout_keeps_sockets_short_and_dirs_private(tmp_path):
    built = layout(tmp_path)
    try:
        assert len(str(built.driver_socket)) < 100
        for name in sandbox.INSIDE:
            assert built.host(name).is_dir()
            assert built.host(name).stat().st_mode & 0o077 == 0
        assert built.secrets.is_dir()
    finally:
        built.remove()
    assert not built.root.exists() and not built.run.exists()


def test_system_mounts_bind_dirs_link_symlinks_and_skip_missing(tmp_path):
    real = tmp_path / "usr"
    real.mkdir()
    link = tmp_path / "bin"
    link.symlink_to("usr")
    missing = tmp_path / "lib32"
    args = sandbox.system_mounts([str(real), str(link), str(missing)])
    assert args == ["--ro-bind", str(real), str(real), "--symlink", "usr", str(link)]


def test_package_root_takes_the_whole_npm_package(tmp_path):
    package = tmp_path / "lib" / "node_modules" / "@vendor" / "cli"
    (package / "bin").mkdir(parents=True)
    (package / "package.json").write_text("{}")
    entry = package / "bin" / "cli.js"
    entry.write_text("#!/usr/bin/env node\n")
    assert sandbox.package_root(entry) == package
    single = tmp_path / "opt" / "tool"
    single.parent.mkdir()
    single.write_bytes(b"\x7fELF")
    assert sandbox.package_root(single) == single.parent


def test_cli_mounts_add_node_for_script_entries(tmp_path):
    entry = tmp_path / "pkg" / "cli.js"
    entry.parent.mkdir()
    entry.write_text("#!/usr/bin/env node\n")
    node = tmp_path / "node-bin" / "node"
    node.parent.mkdir()
    node.write_bytes(b"\x7fELF")
    paths = {"claude": str(entry), "node": str(node)}
    assert sandbox.cli_mounts("claude", paths.get) == [entry.parent, node.parent]
    assert sandbox.cli_mounts("codex", paths.get) == []


def test_link_cli_points_into_the_turn_bin(tmp_path):
    built = layout(tmp_path)
    try:
        target = tmp_path / "claude-real"
        target.write_bytes(b"\x7fELF")
        assert sandbox.link_cli(built, "claude", {"claude": str(target)}.get)
        assert (built.host("bin") / "claude").resolve() == target.resolve()
        assert not sandbox.link_cli(built, "codex", {}.get)
    finally:
        built.remove()


def test_env_starts_empty_and_only_adds_paths_proxy_and_credentials():
    base = {
        "HOME": "/home/runner",
        "PATH": "/x",
        "ANTHROPIC_BASE_URL": "https://evil.example.com",
        "COREMAN_NODE_TOKEN": "t",
        "LANG": "en_US.UTF-8",
        "NODE_EXTRA_CA_CERTS": "/etc/ssl/private-ca.pem",
    }
    env = sandbox.sandbox_env("claude", base, {"ANTHROPIC_API_KEY": "k"})
    assert env["HOME"] == "/turn/home" and env["TMPDIR"] == "/turn/tmp"
    assert env["CLAUDE_CONFIG_DIR"] == "/turn/config"
    assert env["HTTPS_PROXY"] == env["https_proxy"] == sandbox.PROXY_URL
    assert env["NO_PROXY"] == ""
    assert env["LANG"] == "en_US.UTF-8"
    assert env["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/private-ca.pem"
    assert env["ANTHROPIC_API_KEY"] == "k"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert "ANTHROPIC_BASE_URL" not in env and "COREMAN_NODE_TOKEN" not in env
    codex = sandbox.sandbox_env("codex", {}, {})
    assert codex["CODEX_HOME"] == "/turn/config" and "CLAUDE_CONFIG_DIR" not in codex


def test_bwrap_argv_isolates_and_mounts_in_order(tmp_path):
    built = layout(tmp_path)
    try:
        work = tmp_path / "work"
        work.mkdir()
        cred = built.secrets / "credential"
        cred.write_text("{}")
        launch = sandbox.Launch(
            bwrap="/usr/bin/bwrap",
            layout=built,
            workdir=work,
            provider="claude",
            env={"B": "2", "A": "1"},
            ro_paths=[Path("/opt/release"), Path("/opt/release")],
            credential=cred,
            system=["--ro-bind", "/usr", "/usr"],
        )
        args = sandbox.bwrap_argv(launch, ["driver", "--x"])
        assert args[:5] == [
            "/usr/bin/bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--clearenv",
        ]
        assert "--share-net" not in args
        assert args[5:11] == ["--setenv", "A", "1", "--setenv", "B", "2"]
        joined = " ".join(args)
        assert "--ro-bind /opt/release /opt/release" in joined
        assert joined.count("/opt/release /opt/release") == 1
        assert f"--bind {work} /work" in joined
        assert f"--bind {built.run} /turn/run" in joined
        # 凭据在配置目录挂好之后才盖上去，而且只读。
        config_at = args.index(str(built.host("config")))
        cred_at = args.index(str(cred))
        assert cred_at > config_at
        assert args[cred_at - 1 : cred_at + 2] == [
            "--ro-bind",
            str(cred),
            "/turn/config/.credentials.json",
        ]
        assert args[-4:] == ["/work", "--", "driver", "--x"]
        # 本机的家目录、数据目录一律不出现在参数里。
        assert str(Path.home()) not in joined.replace(str(tmp_path), "")
    finally:
        built.remove()


def test_bridge_and_driver_commands_point_inside_the_turn():
    driver = sandbox.driver_command(Path("/opt/release/runtime_daemon/bin/runtime-claude"))
    assert driver[driver.index("--socket") + 1] == "/turn/run/d.sock"
    assert driver[driver.index("--sessions-dir") + 1] == "/turn/driver"
    assert driver[driver.index("--proxy") + 1] == sandbox.PROXY_URL
    bridge = sandbox.bridge_command("/py", Path("/opt/bridge.py"), driver)
    assert bridge[:3] == ["/py", "-I", "/opt/bridge.py"]
    assert bridge[bridge.index("--upstream") + 1] == "/turn/run/e.sock"
    assert bridge[bridge.index("--") + 1 :] == driver


def inputs(tmp_path: Path) -> sandbox.SelfTestInputs:
    sockets = Path(tempfile.mkdtemp(dir="/tmp"))
    return sandbox.SelfTestInputs(
        turns_dir=tmp_path / "turns",
        socket_dir=sockets,
        release=RELEASE,
        hidden=[tmp_path / "projects"],
        clis={"claude": [Path("/opt/claude")], "codex": []},
        system=[],
    )


def test_self_test_reports_unsupported_nodes_without_starting_anything(tmp_path, monkeypatch):
    def runner(*_args, **_kwargs):
        raise AssertionError("must not run")

    result = sandbox.self_test(inputs(tmp_path), system_name="Darwin", runner=runner)
    assert result.providers == {"claude": False, "codex": False}
    assert result.code == "unsupported_platform"
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)
    missing = sandbox.self_test(inputs(tmp_path), system_name="Linux", runner=runner)
    assert missing.code == "bwrap_missing" and not any(missing.providers.values())


def fake_runner(probe: dict[str, bool], *, start_rc: int = 0, version_rc: int = 0):
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        command = argv[argv.index("--") + 1 :]
        if command == ["true"]:
            return subprocess.CompletedProcess(argv, start_rc, "", "")
        if "--version" in command:
            return subprocess.CompletedProcess(argv, version_rc, "1.0.0\n", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(probe) + "\n", "")

    return runner, calls


ALL_GOOD = {"hidden": True, "readonly": True, "offline": True, "bridge": True}


def test_self_test_marks_providers_whose_cli_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "link_cli", lambda *_a, **_k: True)
    runner, calls = fake_runner(ALL_GOOD)
    result = sandbox.self_test(
        inputs(tmp_path), system_name="Linux", bwrap="/usr/bin/bwrap", runner=runner
    )
    assert result.providers == {"claude": True, "codex": False}
    assert result.code == ""
    probe = next(c for c in calls if "-c" in c)
    assert str(tmp_path / "projects") in probe
    assert all("--unshare-all" in c for c in calls)
    assert not (tmp_path / "turns").exists() or not any((tmp_path / "turns").iterdir())


@pytest.mark.parametrize("failed", ["hidden", "readonly", "offline", "bridge"])
def test_self_test_fails_closed_on_any_check(tmp_path, monkeypatch, failed):
    monkeypatch.setattr(sandbox, "link_cli", lambda *_a, **_k: True)
    runner, _ = fake_runner({**ALL_GOOD, failed: False})
    result = sandbox.self_test(
        inputs(tmp_path), system_name="Linux", bwrap="/usr/bin/bwrap", runner=runner
    )
    assert result.providers == {"claude": False, "codex": False}
    assert result.code == f"{failed}_check_failed"


def test_self_test_fails_when_the_sandbox_cannot_start(tmp_path):
    runner, _ = fake_runner(ALL_GOOD, start_rc=1)
    result = sandbox.self_test(
        inputs(tmp_path), system_name="Linux", bwrap="/usr/bin/bwrap", runner=runner
    )
    assert result.code == "sandbox_start_failed" and not any(result.providers.values())


@pytest.mark.skipif(not HAS_BWRAP, reason="需要 Linux 与 bubblewrap")
def test_real_sandbox_self_test(tmp_path):
    """真起沙箱：工作区之外看不见、系统目录只读、没有出口、转发口接得通。"""
    hidden = tmp_path / "projects"
    hidden.mkdir()
    result = sandbox.self_test(
        sandbox.SelfTestInputs(
            turns_dir=tmp_path / "turns",
            socket_dir=Path(tempfile.mkdtemp(dir="/tmp")),
            release=RELEASE,
            hidden=[hidden],
            clis={"claude": [Path(sys.executable).resolve().parent], "codex": []},
        )
    )
    # 测试机上不一定装了 claude：只要求沙箱本身的四项检查通过。
    assert result.code in {"", "cli_unavailable"}
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_sandbox.py -q`
Expected: FAIL，`ImportError: cannot import name 'sandbox' from 'runtime_daemon'`。

- [ ] **Step 3: 实现**

`runtime_daemon/sandbox.py`：

```python
"""个人助理轮次的沙箱：每轮的目录布局、bubblewrap 参数与启动自检。

一轮助理对话在 bubblewrap 里另起一个驱动进程（与普通员工共用的常驻驱动无关）：
- 可读写：工作目录（挂在 /work）、本轮的家目录、临时目录、CLI 配置目录、驱动的会话目录；
- 只读：系统目录、CLI 程序、本安装的发布目录、Python 解释器，以及本轮收窄过的模型登录凭证；
- 看不到：节点上其它一切，包括其他员工的工作目录、守护进程的数据目录和本机用户的家目录；
- 网络：单独的网络命名空间，没有出口；唯一的出路是沙箱里 127.0.0.1 上的转发口，
  经一个 Unix socket 接到守护进程的放行代理（见 egress.py、sandbox_bridge.py）。

本模块只算参数，不碰网络，在任何平台上都能做单元测试；真正起沙箱只在装了 bubblewrap 的
Linux 上（`self_test` 证明这一点，证明不了的节点不声明能力，平台就不派助理轮次）。
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 节点心跳里声明「这一种 AI 能在沙箱里跑助理轮次」的能力名。
CAPABILITY = "assistant_sandbox_v1"
# 能进沙箱的 AI。OpenCode 暂不支持（凭据与出网地址另算，见计划「待所有者确认的取舍」）。
PROVIDERS = ("claude", "codex")
# 沙箱里的固定路径：转录里记的工作目录永远是 /work，换节点还原也续得上。
INSIDE_WORK = "/work"
INSIDE = {
    "home": "/turn/home",
    "tmp": "/turn/tmp",
    "config": "/turn/config",
    "driver": "/turn/driver",
    "bin": "/turn/bin",
    "run": "/turn/run",
}
BRIDGE_PORT = 18080
PROXY_URL = f"http://127.0.0.1:{BRIDGE_PORT}"
DRIVER_SOCKET = "d.sock"
EGRESS_SOCKET = "e.sock"
CONFIG_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
CREDENTIAL_FILE = {"claude": ".credentials.json", "codex": "auth.json"}
# 只读挂进沙箱的系统路径：缺的跳过，软链照原样建软链（合并 /usr 的发行版上 /bin 是软链）。
SYSTEM_PATHS = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/lib32",
    "/etc/alternatives",
    "/etc/ssl",
    "/etc/pki",
    "/etc/ca-certificates",
    "/etc/passwd",
    "/etc/group",
    "/etc/nsswitch.conf",
    "/etc/localtime",
    "/etc/hosts",
)
# 原样带进沙箱的非机密变量；模型凭据走 turn_credentials，不在这里。
PASSTHROUGH_ENV = ("LANG", "LC_ALL", "TZ", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE")


@dataclass(frozen=True)
class TurnLayout:
    """一轮在主机上的目录：`root` 在数据目录里，`run` 在 socket 目录里（路径短，放得下 socket）。"""

    turn_id: str
    root: Path
    run: Path

    @classmethod
    def new(cls, turns_dir: Path, socket_dir: Path) -> TurnLayout:
        turn_id = secrets.token_hex(8)
        return cls(turn_id, turns_dir / turn_id, socket_dir / f"a{turn_id[:10]}")

    def host(self, name: str) -> Path:
        """沙箱里 `INSIDE[name]` 对应的主机目录。"""
        return self.run if name == "run" else self.root / name

    @property
    def secrets(self) -> Path:
        """本轮凭据副本所在目录：不挂进沙箱，只把其中的文件只读挂到配置目录里。"""
        return self.root / "secrets"

    @property
    def driver_socket(self) -> Path:
        return self.run / DRIVER_SOCKET

    @property
    def egress_socket(self) -> Path:
        return self.run / EGRESS_SOCKET

    @property
    def meta(self) -> Path:
        return self.root / "meta.json"

    def create(self) -> None:
        self.root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.mkdir(mode=0o700)
        self.run.mkdir(mode=0o700)
        for name in (*INSIDE, "secrets"):
            if name != "run":
                (self.root / name).mkdir(mode=0o700, exist_ok=True)

    def remove(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.run, ignore_errors=True)


def system_mounts(paths: Sequence[str] = SYSTEM_PATHS) -> list[str]:
    """系统路径的挂载参数：软链建软链，目录与文件只读挂载，不存在的跳过。"""
    args: list[str] = []
    for path in paths:
        if os.path.islink(path):
            args += ["--symlink", os.readlink(path), path]
        elif os.path.exists(path):
            args += ["--ro-bind", path, path]
    return args


def package_root(executable: Path) -> Path:
    """CLI 程序要只读挂进去的目录：在 node_modules 里就挂整个包，否则挂它所在的目录。

    `claude` / `codex` 常见的装法有单个可执行文件（目录里挂它的上级）与 npm 包（入口脚本旁边
    还有 vendor 里的原生程序）两种；后者向上找到含 package.json、且上级是 node_modules 的目录。
    """
    for parent in executable.parents:
        if parent.parent.name == "node_modules" or parent.parent.parent.name == "node_modules":
            if (parent / "package.json").is_file():
                return parent
    return executable.parent


def cli_mounts(provider: str, which: Callable[[str], str | None] = shutil.which) -> list[Path]:
    """CLI 本体（和 npm 包需要的 node）的真实所在目录。"""
    found = which(provider)
    if not found:
        return []
    real = Path(found).resolve()
    mounts = [package_root(real)]
    with real.open("rb") as handle:
        script = handle.read(2) == b"#!"
    if script:
        # npm 装的入口是 `#!/usr/bin/env node` 脚本：node 也要看得见。
        node = which("node")
        if node:
            mounts.append(Path(node).resolve().parent)
    return mounts


def link_cli(
    layout: TurnLayout, provider: str, which: Callable[[str], str | None] = shutil.which
) -> bool:
    """在本轮的 bin 目录里建指向 CLI 真实路径的软链（沙箱里 PATH 的第一项）；找不到 CLI 返回假。"""
    found = which(provider)
    if not found:
        return False
    link = layout.host("bin") / provider
    link.unlink(missing_ok=True)
    link.symlink_to(Path(found).resolve())
    return True


def sandbox_env(provider: str, base: Mapping[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    """沙箱里驱动进程的全部环境变量：从空环境起步，不继承守护进程的任何变量。

    驱动会把请求里的本轮变量（能力令牌、会话坐标）叠在这一份上交给 CLI；这里只有路径、代理
    与几个行为开关，`extra` 是 turn_credentials 给的模型凭据变量（只在节点用环境变量登录时有）。
    """
    env = {
        "HOME": INSIDE["home"],
        "TMPDIR": INSIDE["tmp"],
        "PATH": f"{INSIDE['bin']}:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        CONFIG_ENV[provider]: INSIDE["config"],
        "HTTPS_PROXY": PROXY_URL,
        "HTTP_PROXY": PROXY_URL,
        "https_proxy": PROXY_URL,
        "http_proxy": PROXY_URL,
        "NO_PROXY": "",
        "no_proxy": "",
    }
    if provider == "claude":
        # 不连遥测与自动更新：这些地址不在放行名单里，连了也只是失败重试。
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env["DISABLE_AUTOUPDATER"] = "1"
    for key in PASSTHROUGH_ENV:
        if base.get(key):
            env[key] = base[key]
    env.update(extra)
    return env


@dataclass(frozen=True)
class Launch:
    """起一个沙箱需要的全部输入。"""

    bwrap: str
    layout: TurnLayout
    workdir: Path
    provider: str
    env: Mapping[str, str]
    ro_paths: Sequence[Path] = ()
    credential: Path | None = None
    system: Sequence[str] = field(default_factory=system_mounts)


def bwrap_argv(launch: Launch, command: Sequence[str]) -> list[str]:
    """bubblewrap 的完整参数。顺序有讲究：先建目录骨架，再挂可写目录，最后把凭据只读盖上去。"""
    layout = launch.layout
    args = [
        launch.bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--clearenv",
    ]
    for key in sorted(launch.env):
        args += ["--setenv", key, launch.env[key]]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/turn"]
    args += list(launch.system)
    for path in dict.fromkeys(str(p) for p in launch.ro_paths):
        args += ["--ro-bind", path, path]
    for name, inside in INSIDE.items():
        args += ["--bind", str(layout.host(name)), inside]
    args += ["--bind", str(launch.workdir), INSIDE_WORK]
    if launch.credential is not None:
        target = f"{INSIDE['config']}/{CREDENTIAL_FILE[launch.provider]}"
        args += ["--ro-bind", str(launch.credential), target]
    return [*args, "--chdir", INSIDE_WORK, "--", *command]


def bridge_command(python: str, bridge: Path, command: Sequence[str]) -> list[str]:
    """沙箱里的第一个进程：转发口加上被它看护的子进程（驱动或自检命令）。"""
    upstream = f"{INSIDE['run']}/{EGRESS_SOCKET}"
    return [
        python,
        "-I",
        str(bridge),
        "--port",
        str(BRIDGE_PORT),
        "--upstream",
        upstream,
        "--",
        *command,
    ]


def driver_command(binary: Path) -> list[str]:
    """沙箱里的驱动：socket 与会话目录都在本轮目录里，出网经转发口。"""
    return [
        str(binary),
        "--socket",
        f"{INSIDE['run']}/{DRIVER_SOCKET}",
        "--sessions-dir",
        INSIDE["driver"],
        "--log-file",
        "-",
        "--proxy",
        PROXY_URL,
    ]


# ---- 启动自检 ----------------------------------------------------------------

# 自检在沙箱里跑的检查：看不见的路径、写不进的系统目录、直连外网失败（192.0.2.1 是文档保留
# 地址，没有路由时立即失败，不会真的发出包）、经转发口能连到守护进程这一侧。
_PROBE = r"""
import json, os, socket, sys
port, hidden = int(sys.argv[1]), sys.argv[2:]
out = {"hidden": all(not os.path.exists(p) for p in hidden)}
try:
    open("/usr/.coreman-probe", "w").close()
    out["readonly"] = False
except OSError:
    out["readonly"] = True
try:
    socket.create_connection(("192.0.2.1", 443), timeout=3).close()
    out["offline"] = False
except OSError:
    out["offline"] = True
try:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(b"CONNECT probe.invalid:443 HTTP/1.1\r\n\r\n")
    out["bridge"] = s.recv(64).startswith(b"HTTP/1.1 403")
    s.close()
except OSError:
    out["bridge"] = False
print(json.dumps(out))
"""


@dataclass(frozen=True)
class SelfTestResult:
    """`providers`：每种 AI 能不能在沙箱里跑；`code`：整体失败时的原因代码（不带路径与输出）。"""

    providers: dict[str, bool]
    code: str = ""


Runner = Callable[..., subprocess.CompletedProcess]


class _DenyAll:
    """自检用的放行代理替身：对任何请求都回 403，只证明转发口接得通。"""

    def __init__(self, path: Path) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(4)
        self.sock.settimeout(0.2)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            with conn:
                try:
                    conn.recv(1024)
                    conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    pass

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2)
        self.sock.close()


@dataclass(frozen=True)
class SelfTestInputs:
    """自检要用的本机事实；守护进程按自己的配置填，测试里直接构造。"""

    turns_dir: Path
    socket_dir: Path
    release: Path
    hidden: Sequence[Path]
    clis: Mapping[str, Sequence[Path]]
    extra_ro: Sequence[Path] = ()
    python: str = sys.executable
    system: Sequence[str] = field(default_factory=system_mounts)


def self_test(
    inputs: SelfTestInputs,
    *,
    bwrap: str | None = None,
    system_name: str | None = None,
    runner: Runner = subprocess.run,
) -> SelfTestResult:
    """按前置验证脚本的同一套检查，在本机真起几次沙箱；任何一步不过就整体不可用。

    只在 Linux 且找得到 bwrap 时才起进程。容器里没有用户命名空间、macOS、没装 bubblewrap 的
    节点在这里得到失败的结果，平台就不会把助理轮次派过来。
    """
    providers = dict.fromkeys(PROVIDERS, False)
    if (system_name or platform.system()) != "Linux":
        return SelfTestResult(providers, "unsupported_platform")
    bwrap = bwrap or shutil.which("bwrap")
    if not bwrap:
        return SelfTestResult(providers, "bwrap_missing")
    layout = TurnLayout.new(inputs.turns_dir, inputs.socket_dir)
    layout.create()
    work = Path(tempfile.mkdtemp(dir=layout.root))
    denier = None
    try:
        bridge = inputs.release / "runtime_daemon" / "sandbox_bridge.py"
        ro = [inputs.release, Path(sys.base_prefix), *inputs.extra_ro]

        def run(provider: str, command: Sequence[str], extra: Sequence[Path] = ()) -> Any:
            launch = Launch(
                bwrap=bwrap,
                layout=layout,
                workdir=work,
                provider=provider,
                env=sandbox_env(provider, {}, {}),
                ro_paths=[*ro, *extra],
                system=inputs.system,
            )
            return runner(
                bwrap_argv(launch, command),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=60,
            )

        try:
            if run("claude", ["true"]).returncode != 0:
                return SelfTestResult(providers, "sandbox_start_failed")
            denier = _DenyAll(layout.egress_socket)
            hidden = [str(p) for p in inputs.hidden]
            check = [inputs.python, "-I", "-c", _PROBE, str(BRIDGE_PORT), *hidden]
            probe = run("claude", bridge_command(inputs.python, bridge, check))
            try:
                checks = json.loads(probe.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                return SelfTestResult(providers, "probe_failed")
            for name in ("hidden", "readonly", "offline", "bridge"):
                if checks.get(name) is not True:
                    return SelfTestResult(providers, f"{name}_check_failed")
            for provider in PROVIDERS:
                mounts = inputs.clis.get(provider) or ()
                if mounts and link_cli(layout, provider):
                    result = run(provider, [provider, "--version"], mounts)
                    providers[provider] = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return SelfTestResult(dict.fromkeys(PROVIDERS, False), "sandbox_start_failed")
        return SelfTestResult(providers, "" if any(providers.values()) else "cli_unavailable")
    finally:
        if denier is not None:
            denier.close()
        layout.remove()
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_sandbox.py -q && uv run ruff check runtime_daemon/sandbox.py tests/unit/test_assistant_sandbox.py`
Expected: 16 条里没有 bubblewrap 的机器上 1 条跳过（`test_real_sandbox_self_test`），其余通过；在装了 bubblewrap 的 Linux 上全部通过。

普通机器人不受影响：本任务只新增模块，守护进程还没有引用它（`tests/unit/test_runtime_daemon*.py` 照常通过）。

- [ ] **Step 5: 提交**

```bash
git add runtime_daemon/sandbox.py tests/unit/test_assistant_sandbox.py
git commit -m "个人助理：节点沙箱的目录布局、bubblewrap 参数与启动自检"
```

---

### Task 2: 沙箱里的转发口

**Files:**
- Create: `runtime_daemon/sandbox_bridge.py`
- Test: `tests/unit/test_assistant_sandbox_bridge.py`

**Interfaces:**
- Consumes: 无（只用标准库；Task 1 的 `bridge_command` 生成它的命令行）
- Produces: 命令行 `python -I sandbox_bridge.py --port <端口> --upstream <Unix socket> -- <命令…>`；`main(argv) -> int` 返回被看护命令的退出码

- [ ] **Step 1: 写失败测试**

`tests/unit/test_assistant_sandbox_bridge.py`：

```python
"""沙箱里的转发口：127.0.0.1 上的连接原样转到 Unix socket，退出码随看护的子进程。"""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[2] / "runtime_daemon" / "sandbox_bridge.py"
CLIENT = """
import socket, sys, time
for _ in range(50):
    try:
        s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=2)
        break
    except OSError:
        time.sleep(0.1)
s.sendall(b"ping")
print(s.recv(64).decode())
sys.exit(7)
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def upstream(path: Path) -> socket.socket:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)

    def serve() -> None:
        conn, _ = server.accept()
        with conn:
            conn.sendall(b"pong:" + conn.recv(64))

    threading.Thread(target=serve, daemon=True).start()
    return server


def test_bridge_forwards_to_the_upstream_socket_and_returns_the_child_code():
    path = Path(tempfile.mkdtemp(dir="/tmp")) / "e.sock"
    server = upstream(path)
    port = free_port()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(BRIDGE),
                "--port",
                str(port),
                "--upstream",
                str(path),
                "--",
                sys.executable,
                "-c",
                CLIENT,
                str(port),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.close()
    assert result.returncode == 7
    assert result.stdout.strip() == "pong:ping"


def test_bridge_needs_a_command():
    result = subprocess.run(
        [sys.executable, "-I", str(BRIDGE), "--port", "1", "--upstream", "/nonexistent"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0


def test_bridge_only_uses_the_standard_library():
    source = BRIDGE.read_text()
    assert "runtime_daemon" not in source.split('"""', 2)[2]
    assert "import httpx" not in source
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_sandbox_bridge.py -q`
Expected: FAIL，子进程报 `can't open file …/sandbox_bridge.py`，退出码不是 7。

- [ ] **Step 3: 实现**

`runtime_daemon/sandbox_bridge.py`：

```python
"""沙箱里的第一个进程：在 127.0.0.1 上开一个转发口，再起并看护一个子进程。

沙箱有自己的网络命名空间，只有回环网卡、没有出口。CLI 按 HTTPS_PROXY 把所有连接发到这个口，
这里原样转给挂进来的 Unix socket，另一端是守护进程的放行代理（egress.py），由它决定放不放行。
子进程（驱动，或自检命令）退出时本进程以同一个退出码退出；收到 SIGTERM / SIGINT 转给子进程。

只用标准库，以 `python -I <本文件>` 运行：沙箱里没有守护进程的依赖，也不读任何环境里的 Python 设置。
"""

from __future__ import annotations

import argparse
import signal
import socket
import subprocess
import sys
import threading

BUFFER = 64 * 1024


def _pump(source: socket.socket, target: socket.socket) -> None:
    try:
        while True:
            data = source.recv(BUFFER)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (source, target):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _serve_one(client: socket.socket, upstream: str) -> None:
    try:
        remote = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        remote.connect(upstream)
    except OSError:
        client.close()
        return
    threading.Thread(target=_pump, args=(client, remote), daemon=True).start()
    threading.Thread(target=_pump, args=(remote, client), daemon=True).start()


def serve(listener: socket.socket, upstream: str) -> None:
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        _serve_one(client, upstream)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("缺少要看护的命令")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", args.port))
    listener.listen(64)
    threading.Thread(target=serve, args=(listener, args.upstream), daemon=True).start()
    child = subprocess.Popen(command)

    def forward(signum: int, _frame: object) -> None:
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    code = child.wait()
    listener.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_sandbox_bridge.py -q && uv run ruff check runtime_daemon/sandbox_bridge.py tests/unit/test_assistant_sandbox_bridge.py`
Expected: 3 passed。

普通机器人不受影响：只新增文件，没有任何代码引用它。

- [ ] **Step 5: 提交**

```bash
git add runtime_daemon/sandbox_bridge.py tests/unit/test_assistant_sandbox_bridge.py
git commit -m "个人助理：沙箱里的转发口，把连接接到守护进程的放行代理"
```

---

### Task 3: 出网放行代理

**Files:**
- Create: `runtime_daemon/egress.py`
- Test: `tests/unit/test_assistant_egress.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `MODEL_HOSTS = {"claude": ("api.anthropic.com",), "codex": ("chatgpt.com", "api.openai.com")}`
  - `endpoint(url) -> tuple[str, int] | None`（只认 https）
  - `Policy(allowed: frozenset[tuple[str, int]], upstream: Mapping[tuple[str, int], str | None])`、`Policy.permits(host, port) -> bool`
  - `turn_policy(*, provider, api_url, env_vars, model_hosts=None, control_proxy=None, ai_proxy=None) -> Policy`
  - `Egress(policy)`：`await start(path)`、`await close()`，计数 `allowed` / `denied`

- [ ] **Step 1: 写失败测试**

`tests/unit/test_assistant_egress.py`：

```python
"""助理沙箱的出网放行代理：名单内的 CONNECT 接通，其余一律拒绝，上游代理照节点配置串上。"""

from __future__ import annotations

import asyncio
import base64
import tempfile
from pathlib import Path

import pytest

from runtime_daemon import egress


def test_policy_covers_platform_mcp_and_model_hosts_only():
    policy = egress.turn_policy(
        provider="claude",
        api_url="https://coreman.example.com",
        env_vars={
            "COREMAN_FEISHU_PERSONAL_URL": "https://mcp.example.com:8443/api/runtime/x",
            "COREMAN_BOT_KEY": "demo-bot",
            "COREMAN_PLAIN_URL": "http://insecure.example.com/x",
            "OTHER_URL": "https://not-ours.example.com",
        },
        control_proxy="http://control-proxy.example.com:3128",
        ai_proxy="http://ai-proxy.example.com:3128",
    )
    assert policy.allowed == {
        ("coreman.example.com", 443),
        ("mcp.example.com", 8443),
        ("api.anthropic.com", 443),
    }
    assert policy.upstream[("coreman.example.com", 443)] == "http://control-proxy.example.com:3128"
    assert policy.upstream[("api.anthropic.com", 443)] == "http://ai-proxy.example.com:3128"
    assert policy.permits("API.Anthropic.com", 443)
    assert not policy.permits("api.anthropic.com", 80)
    # OAuth 刷新地址不在名单里。
    assert not policy.permits("console.anthropic.com", 443)
    codex = egress.turn_policy(provider="codex", api_url="https://coreman.example.com", env_vars={})
    assert ("chatgpt.com", 443) in codex.allowed and ("auth.openai.com", 443) not in codex.allowed
    custom = egress.turn_policy(
        provider="codex",
        api_url="https://coreman.example.com",
        env_vars={},
        model_hosts=["llm.example.com"],
    )
    assert ("llm.example.com", 443) in custom.allowed and ("chatgpt.com", 443) not in custom.allowed


async def echo_server() -> tuple[asyncio.AbstractServer, int]:
    async def handle(reader, writer):
        while data := await reader.read(1024):
            writer.write(b"echo:" + data)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.fixture
def socket_path():
    return Path(tempfile.mkdtemp(dir="/tmp")) / "e.sock"


async def request(path: Path, head: bytes, payload: bytes = b"") -> bytes:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(head)
    await writer.drain()
    status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    body = b""
    if payload and status.startswith(b"HTTP/1.1 200"):
        writer.write(payload)
        await writer.drain()
        body = await asyncio.wait_for(reader.read(1024), 5)
    writer.close()
    return status + body


async def test_allowed_connect_is_tunnelled(socket_path):
    server, port = await echo_server()
    proxy = egress.Egress(egress.Policy(frozenset({("127.0.0.1", port)})))
    await proxy.start(socket_path)
    try:
        out = await request(
            socket_path, f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode(), b"hi"
        )
        assert out.startswith(b"HTTP/1.1 200") and out.endswith(b"echo:hi")
        assert (proxy.allowed, proxy.denied) == (1, 0)
        assert socket_path.stat().st_mode & 0o077 == 0
    finally:
        await proxy.close()
        server.close()


@pytest.mark.parametrize(
    ("head", "status"),
    [
        (b"CONNECT blocked.example.com:443 HTTP/1.1\r\n\r\n", b"HTTP/1.1 403"),
        (b"GET http://127.0.0.1/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n", b"HTTP/1.1 405"),
    ],
)
async def test_everything_else_is_refused(socket_path, head, status):
    proxy = egress.Egress(egress.Policy(frozenset({("api.anthropic.com", 443)})))
    await proxy.start(socket_path)
    try:
        assert (await request(socket_path, head)).startswith(status)
        assert (proxy.allowed, proxy.denied) == (0, 1)
    finally:
        await proxy.close()


async def test_upstream_proxy_is_used_with_its_credentials(socket_path):
    seen: list[bytes] = []
    target, port = await echo_server()

    async def upstream(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        seen.append(head)
        remote_reader, remote_writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"HTTP/1.1 200 OK\r\n\r\n")
        await writer.drain()

        async def pipe(src, dst):
            while data := await src.read(1024):
                dst.write(data)
                await dst.drain()
            dst.close()

        await asyncio.gather(pipe(reader, remote_writer), pipe(remote_reader, writer))

    proxy_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    proxy_port = proxy_server.sockets[0].getsockname()[1]
    destination = ("model.example.com", 443)
    proxy = egress.Egress(
        egress.Policy(
            frozenset({destination}),
            {destination: f"http://user:p%40ss@127.0.0.1:{proxy_port}"},
        )
    )
    await proxy.start(socket_path)
    try:
        out = await request(socket_path, b"CONNECT model.example.com:443 HTTP/1.1\r\n\r\n", b"x")
        assert out.endswith(b"echo:x")
        assert seen[0].startswith(b"CONNECT model.example.com:443 HTTP/1.1")
        auth = base64.b64encode(b"user:p@ss")
        assert b"Proxy-Authorization: Basic " + auth in seen[0]
    finally:
        await proxy.close()
        proxy_server.close()
        target.close()


async def test_unreachable_destination_is_a_bad_gateway(socket_path):
    proxy = egress.Egress(egress.Policy(frozenset({("127.0.0.1", 1)})))
    await proxy.start(socket_path)
    try:
        out = await request(socket_path, b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 502")
    finally:
        await proxy.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_egress.py -q`
Expected: FAIL，`ImportError: cannot import name 'egress' from 'runtime_daemon'`。

- [ ] **Step 3: 实现**

`runtime_daemon/egress.py`：

```python
"""助理沙箱的出网放行代理：只接 CONNECT，只放行名单里的「主机:端口」。

每一轮在自己的 Unix socket 上起一个，沙箱里的转发口（sandbox_bridge.py）接到这里。名单由守护
进程按本轮算出：平台地址（控制面地址与本轮 MCP 地址的主机）加上这种 AI 的模型接口地址。登录
令牌的刷新地址不在名单里：沙箱里的 CLI 就算想刷新也连不出去（见 turn_credentials.py）。

不解 TLS、不看内容；日志只记放行与拒绝的次数，不记主机名（主机名本身也可以被拿来夹带内容）。
上游代理沿用节点已有的配置：平台地址走控制面代理，模型接口走 AI 代理，都没配就直连。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

LOG = logging.getLogger("coreman-runtime")
HEAD_LIMIT = 8 * 1024
HEAD_TIMEOUT = 10.0
CONNECT_TIMEOUT = 15.0
BUFFER = 64 * 1024
# 各 AI 的模型接口地址（只要推理所需的；OAuth 刷新地址故意不在里面）。节点 config.json 的
# assistant_egress_hosts 可以按 AI 覆盖。
MODEL_HOSTS = {
    "claude": ("api.anthropic.com",),
    "codex": ("chatgpt.com", "api.openai.com"),
}
_CONNECT = re.compile(rb"^CONNECT ([A-Za-z0-9.-]{1,253}):([0-9]{1,5}) HTTP/1\.[01]\r\n")


def endpoint(url: str) -> tuple[str, int] | None:
    """URL 的「主机:端口」；只认 https，端口缺省 443。"""
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or not parts.hostname:
        return None
    try:
        port = parts.port or 443
    except ValueError:
        return None
    return parts.hostname.lower(), port


@dataclass(frozen=True)
class Policy:
    """本轮的放行名单，以及每个目的地经哪个上游代理（None 为直连）。"""

    allowed: frozenset[tuple[str, int]]
    upstream: Mapping[tuple[str, int], str | None] = field(default_factory=dict)

    def permits(self, host: str, port: int) -> bool:
        return (host.lower(), port) in self.allowed


def turn_policy(
    *,
    provider: str,
    api_url: str,
    env_vars: Mapping[str, str],
    model_hosts: Iterable[str] | None = None,
    control_proxy: str | None = None,
    ai_proxy: str | None = None,
) -> Policy:
    """按本轮算放行名单：控制面地址、本轮所有 `COREMAN_*_URL` 的主机、模型接口地址。"""
    platform: set[tuple[str, int]] = set()
    for url in [api_url, *(v for k, v in env_vars.items() if k.startswith("COREMAN_"))]:
        if not url.startswith("https://"):
            continue
        found = endpoint(url)
        if found is not None:
            platform.add(found)
    models = {(h.lower(), 443) for h in (model_hosts or MODEL_HOSTS.get(provider, ()))}
    upstream: dict[tuple[str, int], str | None] = dict.fromkeys(models, ai_proxy or None)
    upstream.update(dict.fromkeys(platform, control_proxy or None))
    return Policy(frozenset(platform | models), upstream)


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
    if len(head) > HEAD_LIMIT:
        raise ValueError("head too large")
    return head


async def _open(host: str, port: int, proxy: str | None):
    """连目的地；有上游代理时先向它发 CONNECT。"""
    if not proxy:
        return await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT)
    parts = urlsplit(proxy)
    if parts.scheme not in ("http", "") or not parts.hostname:
        raise OSError("unsupported upstream proxy")
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(parts.hostname, parts.port or 80), CONNECT_TIMEOUT
    )
    lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
    if parts.username:
        token = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
        lines.append("Proxy-Authorization: Basic " + base64.b64encode(token.encode()).decode())
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    status = await _read_head(reader)
    if not re.match(rb"^HTTP/1\.[01] 200", status):
        writer.close()
        raise OSError("upstream proxy refused")
    return reader, writer


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(BUFFER):
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        with contextlib.suppress(OSError):
            writer.close()


class Egress:
    """一轮的放行代理。`allowed` / `denied` 只计数。"""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.allowed = 0
        self.denied = 0
        self.server: asyncio.AbstractServer | None = None

    async def start(self, path: Path) -> None:
        self.server = await asyncio.start_unix_server(self._handle, path=str(path))
        os.chmod(path, 0o600)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.server.wait_closed(), 5)
            self.server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await _read_head(reader)
        except (  # noqa: UP041 asyncio.TimeoutError is distinct on 3.10
            ValueError,
            OSError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            asyncio.TimeoutError,
            TimeoutError,
        ):
            writer.close()
            return
        match = _CONNECT.match(head)
        if match is None:
            self.denied += 1
            await self._reply(writer, b"405 Method Not Allowed")
            return
        host, port = match.group(1).decode().lower(), int(match.group(2))
        if not self.policy.permits(host, port):
            self.denied += 1
            await self._reply(writer, b"403 Forbidden")
            return
        try:
            remote_reader, remote_writer = await _open(
                host, port, self.policy.upstream.get((host, port))
            )
        except (  # noqa: UP041 asyncio.TimeoutError is distinct on 3.10
            OSError,
            ValueError,
            asyncio.TimeoutError,
            TimeoutError,
            asyncio.IncompleteReadError,
        ):
            await self._reply(writer, b"502 Bad Gateway")
            return
        self.allowed += 1
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(_relay(reader, remote_writer), _relay(remote_reader, writer))

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: bytes) -> None:
        with contextlib.suppress(OSError):
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_egress.py -q && uv run ruff check runtime_daemon/egress.py tests/unit/test_assistant_egress.py`
Expected: 6 passed。

普通机器人不受影响：只新增模块；守护进程现有的出站代理设置（`apply_proxy`）不变，本模块只读环境里的代理变量。

- [ ] **Step 5: 提交**

```bash
git add runtime_daemon/egress.py tests/unit/test_assistant_egress.py
git commit -m "个人助理：沙箱的出网放行代理，只通平台与模型接口"
```

---

### Task 4: 收窄的模型登录凭证

**Files:**
- Create: `runtime_daemon/turn_credentials.py`
- Test: `tests/unit/test_assistant_turn_credentials.py`

**Interfaces:**
- Consumes: 席位的环境（`Seats.env(provider, seat)`，见 `runtime_daemon/seats.py`）
- Produces:
  - `MARGIN_SECONDS = 1800`、`ENV_LOGINS`
  - `Unavailable(code)`：`credential_missing` / `credential_unreadable` / `credential_expiring` / `provider_unsupported`
  - `Credential(content: bytes | None, env: Mapping[str, str])`
  - `claude(config_dir, env, now=None) -> Credential`、`codex(codex_home, env, now=None) -> Credential`
  - `for_provider(provider, seat_env, home) -> Credential`
  - `write(directory, credential) -> Path | None`（0600，环境变量型返回 None）

- [ ] **Step 1: 写失败测试**

`tests/unit/test_assistant_turn_credentials.py`：

```python
"""沙箱里的模型凭证：副本里没有刷新令牌，快到期不起这一轮，节点的登录文件一字不动。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from runtime_daemon import turn_credentials as creds

NOW = 1_800_000_000.0


def jwt(exp: float) -> str:
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': exp, 'email': 'demo@example.com'})}.sig"


def write(path: Path, data: dict) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    return raw


def test_claude_copy_drops_the_refresh_token_and_other_secrets(tmp_path):
    source = tmp_path / ".claude" / ".credentials.json"
    raw = write(
        source,
        {
            "claudeAiOauth": {
                "accessToken": "access-1",
                "refreshToken": "refresh-1",
                "expiresAt": int((NOW + 3600) * 1000),
                "scopes": ["user:inference"],
                "subscriptionType": "team",
            },
            "mcpOAuth": {"server": {"refreshToken": "mcp-refresh"}},
        },
    )
    got = creds.claude(tmp_path / ".claude", {}, now=NOW)
    assert got.env == {}
    data = json.loads(got.content or b"")
    assert data == {
        "claudeAiOauth": {
            "accessToken": "access-1",
            "expiresAt": int((NOW + 3600) * 1000),
            "scopes": ["user:inference"],
            "subscriptionType": "team",
        }
    }
    assert b"refresh" not in (got.content or b"")
    assert source.read_bytes() == raw


def test_claude_refuses_a_token_about_to_expire(tmp_path):
    write(
        tmp_path / ".credentials.json",
        {
            "claudeAiOauth": {
                "accessToken": "a",
                "refreshToken": "r",
                "expiresAt": (NOW + 60) * 1000,
            }
        },
    )
    with pytest.raises(creds.Unavailable) as caught:
        creds.claude(tmp_path, {}, now=NOW)
    assert caught.value.code == "credential_expiring"


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (None, "credential_missing"),
        ("not json", "credential_unreadable"),
        ("[]", "credential_unreadable"),
    ],
)
def test_claude_without_a_usable_file(tmp_path, content, code):
    if content is not None:
        (tmp_path / ".credentials.json").write_text(content)
    with pytest.raises(creds.Unavailable) as caught:
        creds.claude(tmp_path, {}, now=NOW)
    assert caught.value.code == code


def test_environment_logins_pass_one_variable(tmp_path):
    got = creds.claude(tmp_path, {"ANTHROPIC_API_KEY": "k", "PATH": "/x"}, now=NOW)
    assert got.content is None and dict(got.env) == {"ANTHROPIC_API_KEY": "k"}
    assert dict(creds.codex(tmp_path, {"OPENAI_API_KEY": "o"}, now=NOW).env) == {
        "OPENAI_API_KEY": "o"
    }


def test_codex_copy_keeps_access_drops_refresh_and_resets_last_refresh(tmp_path):
    source = tmp_path / "auth.json"
    raw = write(
        source,
        {
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": jwt(NOW + 7200),
                "access_token": jwt(NOW + 7200),
                "refresh_token": "refresh-2",
                "account_id": "acct",
            },
            "last_refresh": "2026-01-01T00:00:00Z",
        },
    )
    got = creds.codex(tmp_path, {}, now=NOW)
    data = json.loads(got.content or b"")
    assert "refresh_token" not in data["tokens"]
    assert data["tokens"]["account_id"] == "acct"
    assert data["last_refresh"].startswith("2027-01-15T")
    assert source.read_bytes() == raw


def test_codex_api_key_file_is_copied_as_is(tmp_path):
    write(tmp_path / "auth.json", {"OPENAI_API_KEY": "sk-test"})
    got = creds.codex(tmp_path, {}, now=NOW)
    assert json.loads(got.content or b"") == {"OPENAI_API_KEY": "sk-test"}


def test_codex_refuses_expiring_or_unreadable_tokens(tmp_path):
    write(tmp_path / "auth.json", {"tokens": {"access_token": jwt(NOW + 10), "refresh_token": "r"}})
    with pytest.raises(creds.Unavailable) as caught:
        creds.codex(tmp_path, {}, now=NOW)
    assert caught.value.code == "credential_expiring"
    write(tmp_path / "auth.json", {"tokens": {"access_token": "opaque"}})
    with pytest.raises(creds.Unavailable) as caught:
        creds.codex(tmp_path, {}, now=NOW)
    assert caught.value.code == "credential_unreadable"


def test_for_provider_follows_the_active_seat(tmp_path):
    seat = tmp_path / "seat"
    write(
        seat / ".credentials.json",
        {"claudeAiOauth": {"accessToken": "seat", "expiresAt": 10**13}},
    )
    got = creds.for_provider("claude", {"CLAUDE_CONFIG_DIR": str(seat)}, tmp_path / "home")
    assert json.loads(got.content or b"")["claudeAiOauth"]["accessToken"] == "seat"
    with pytest.raises(creds.Unavailable):
        creds.for_provider("opencode", {}, tmp_path)


def test_write_is_private_and_skips_environment_logins(tmp_path):
    path = creds.write(tmp_path, creds.Credential(content=b"{}"))
    assert path is not None and path.read_bytes() == b"{}"
    assert path.stat().st_mode & 0o077 == 0
    assert creds.write(tmp_path / "other", creds.Credential(env={"A": "b"})) is None
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_turn_credentials.py -q`
Expected: FAIL，`ImportError: cannot import name 'turn_credentials' from 'runtime_daemon'`。

- [ ] **Step 3: 实现**

`runtime_daemon/turn_credentials.py`：

```python
"""助理沙箱里的模型登录凭证：每轮从节点的登录文件收窄出一份只读副本。

节点上的 CLI 登录由所有员工共用。OAuth 的刷新令牌可能是一次性的：沙箱里的 CLI 若用它刷新，
新令牌写不回只读文件，节点原来那枚又已作废，整台节点的登录就坏了。所以副本里**没有刷新令牌**：
- Claude：`.credentials.json` 只留 `claudeAiOauth` 里除 `refreshToken` 以外的字段，其它顶层键
  （如各 MCP 服务的 OAuth 令牌）一概不带；
- Codex：`auth.json` 去掉 `tokens.refresh_token`，`last_refresh` 改成现在，免得 CLI 以为该刷新了；
  用 API Key 登录的原样带上（没有刷新这回事）。
访问令牌剩余有效期不足 `MARGIN_SECONDS` 时不起这一轮（`credential_expiring`）：刷新留给沙箱外
的普通轮次与健康检查，由它们照常写回节点的登录文件。节点用环境变量登录（API Key、长效令牌）
时只把那一个变量带进去。

沙箱里被注入的助理读得到这份副本：拿到的只是一枚到期即失效的访问令牌，而沙箱出网只通平台与
模型接口，带不出去；刷新地址不在放行名单里。
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# 访问令牌至少还要有这么久才起一轮（秒）。长于一轮的常见时长，短于令牌的常见寿命。
MARGIN_SECONDS = 30 * 60
# 节点用环境变量登录时，带进沙箱的那一个变量（按顺序取第一个有值的）。
ENV_LOGINS = {
    "claude": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
    "codex": ("OPENAI_API_KEY",),
}


class Unavailable(Exception):
    """这一轮拿不到可用的模型凭证；`code` 是给平台的原因代码。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Credential:
    """收窄后的凭证：一个要只读挂进配置目录的文件内容，或几个环境变量。"""

    content: bytes | None = None
    env: Mapping[str, str] = field(default_factory=dict)


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise Unavailable("credential_missing") from None
    except (OSError, ValueError):
        raise Unavailable("credential_unreadable") from None
    if not isinstance(data, dict):
        raise Unavailable("credential_unreadable")
    return data


def _from_env(provider: str, env: Mapping[str, str]) -> Credential | None:
    for key in ENV_LOGINS[provider]:
        if env.get(key):
            return Credential(env={key: env[key]})
    return None


def claude(config_dir: Path, env: Mapping[str, str], now: float | None = None) -> Credential:
    """Claude 的凭证副本。`config_dir` 是当前席位的 `CLAUDE_CONFIG_DIR`（或 `~/.claude`）。"""
    found = _from_env("claude", env)
    if found is not None:
        return found
    data = _load(config_dir / ".credentials.json")
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict) or not isinstance(oauth.get("accessToken"), str):
        raise Unavailable("credential_unreadable")
    expires = oauth.get("expiresAt")
    if not isinstance(expires, int | float):
        raise Unavailable("credential_unreadable")
    if expires / 1000 - (time.time() if now is None else now) < MARGIN_SECONDS:
        raise Unavailable("credential_expiring")
    narrowed = {k: v for k, v in oauth.items() if k != "refreshToken"}
    return Credential(content=json.dumps({"claudeAiOauth": narrowed}).encode())


def _jwt_expiry(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return float(exp) if isinstance(exp, int | float) else None


def codex(codex_home: Path, env: Mapping[str, str], now: float | None = None) -> Credential:
    """Codex 的凭证副本。`codex_home` 是当前席位的 `CODEX_HOME`（或 `~/.codex`）。"""
    found = _from_env("codex", env)
    if found is not None:
        return found
    data = _load(codex_home / "auth.json")
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        if isinstance(data.get("OPENAI_API_KEY"), str) and data["OPENAI_API_KEY"]:
            key = {"OPENAI_API_KEY": data["OPENAI_API_KEY"]}
            return Credential(content=json.dumps(key).encode())
        raise Unavailable("credential_unreadable")
    access = tokens.get("access_token")
    expiry = _jwt_expiry(access) if isinstance(access, str) else None
    if expiry is None:
        raise Unavailable("credential_unreadable")
    moment = time.time() if now is None else now
    if expiry - moment < MARGIN_SECONDS:
        raise Unavailable("credential_expiring")
    narrowed = dict(data)
    narrowed["tokens"] = {k: v for k, v in tokens.items() if k != "refresh_token"}
    narrowed["last_refresh"] = (
        datetime.fromtimestamp(moment, tz=timezone.utc).isoformat().replace("+00:00", "Z")  # noqa: UP017 3.10
    )
    return Credential(content=json.dumps(narrowed).encode())


def for_provider(provider: str, seat_env: Mapping[str, str], home: Path) -> Credential:
    """按当前席位的环境找到登录文件并收窄。

    Raises:
        Unavailable: 没登录、文件读不懂、访问令牌快到期
    """
    if provider == "claude":
        directory = seat_env.get("CLAUDE_CONFIG_DIR")
        return claude(Path(directory) if directory else home / ".claude", seat_env)
    if provider == "codex":
        directory = seat_env.get("CODEX_HOME")
        return codex(Path(directory) if directory else home / ".codex", seat_env)
    raise Unavailable("provider_unsupported")


def write(directory: Path, credential: Credential) -> Path | None:
    """把文件型凭证写进本轮的 secrets 目录（0600），返回路径；环境变量型返回 None。"""
    if credential.content is None:
        return None
    path = directory / "credential"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(credential.content)
    return path
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_turn_credentials.py -q && uv run ruff check runtime_daemon/turn_credentials.py tests/unit/test_assistant_turn_credentials.py`
Expected: 11 passed。

普通机器人不受影响：只读节点的登录文件、写副本，原文件一字不动（`test_claude_copy_drops_the_refresh_token_and_other_secrets`、`test_codex_copy_keeps_access_drops_refresh_and_resets_last_refresh` 都断言了原文件的字节不变）。

- [ ] **Step 5: 提交**

```bash
git add runtime_daemon/turn_credentials.py tests/unit/test_assistant_turn_credentials.py
git commit -m "个人助理：沙箱里只挂去掉刷新令牌的登录凭证副本"
```

---

### Task 5: 转录的打包与还原（节点侧）

**Files:**
- Create: `runtime_daemon/transcripts.py`
- Test: `tests/unit/test_assistant_node_transcripts.py`

**Interfaces:**
- Consumes: 无
- Produces: `MAX_BYTES = 50 MiB`、`MAX_EXPANDED = 512 MiB`、`TREES`、`THREAD_FILE`；`pack(root, provider, session_id) -> bytes | None`（超限抛 `TooLarge`）；`unpack(root, provider, session_id, data) -> int`（不合规抛 `BadBundle`）

- [ ] **Step 1: 写失败测试**

`tests/unit/test_assistant_node_transcripts.py`：

```python
"""节点上的转录包：只带续聊需要的文件，还原时逐个成员把关。"""

from __future__ import annotations

import io
import tarfile

import pytest

from runtime_daemon import transcripts


def touch(path, content=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def names(data: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return sorted(tar.getnames())


def test_claude_bundle_holds_only_projects(tmp_path):
    touch(tmp_path / "config/projects/-work/s1.jsonl", b'{"type":"user"}\n')
    touch(tmp_path / "config/history.jsonl")
    touch(tmp_path / "config/.credentials.json")
    touch(tmp_path / "driver/s1/codex_thread.txt")
    touch(tmp_path / "home/.cache/x")
    data = transcripts.pack(tmp_path, "claude", "s1")
    assert data is not None
    assert names(data) == ["config/projects/-work/s1.jsonl"]


def test_codex_bundle_adds_the_thread_binding(tmp_path):
    touch(tmp_path / "config/sessions/2026/11/12/rollout-a-t1.jsonl")
    touch(tmp_path / "driver/s1/codex_thread.txt", b"t1")
    touch(tmp_path / "driver/s1.jsonl")
    data = transcripts.pack(tmp_path, "codex", "s1")
    assert data is not None
    assert names(data) == [
        "config/sessions/2026/11/12/rollout-a-t1.jsonl",
        "driver/s1/codex_thread.txt",
    ]


def test_nothing_to_pack(tmp_path):
    assert transcripts.pack(tmp_path, "claude", "s1") is None


def test_symlinks_are_not_followed(tmp_path):
    secret = tmp_path / "outside.txt"
    touch(secret, b"secret")
    (tmp_path / "config/projects").mkdir(parents=True)
    (tmp_path / "config/projects/link.jsonl").symlink_to(secret)
    assert transcripts.pack(tmp_path, "claude", "s1") is None


def test_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "MAX_BYTES", 64)
    import os

    touch(tmp_path / "config/projects/-work/s1.jsonl", os.urandom(4096))
    with pytest.raises(transcripts.TooLarge):
        transcripts.pack(tmp_path, "claude", "s1")


def test_round_trip(tmp_path):
    source, target = tmp_path / "a", tmp_path / "b"
    touch(source / "config/sessions/r.jsonl", b"rollout")
    touch(source / "driver/s1/codex_thread.txt", b"t1")
    data = transcripts.pack(source, "codex", "s1")
    assert data is not None
    target.mkdir()
    assert transcripts.unpack(target, "codex", "s1", data) == 2
    assert (target / "config/sessions/r.jsonl").read_bytes() == b"rollout"
    assert (target / "driver/s1/codex_thread.txt").read_bytes() == b"t1"
    assert (target / "config/sessions/r.jsonl").stat().st_mode & 0o077 == 0


def bundle(*members: tuple[str, bytes | None, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, link, content in members:
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link.decode()
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@pytest.mark.parametrize(
    "member",
    [
        ("/etc/passwd", None, b"x"),
        ("config/projects/../../escape", None, b"x"),
        ("config/history.jsonl", None, b"x"),
        ("driver/other/codex_thread.txt", None, b"x"),
        ("config/projects/link", b"/etc/passwd", b""),
    ],
)
def test_unpack_rejects_anything_out_of_place(tmp_path, member):
    with pytest.raises(transcripts.BadBundle):
        transcripts.unpack(tmp_path, "codex", "s1", bundle(member))
    assert not (tmp_path / "escape").exists()


def test_unpack_rejects_garbage_and_bombs(tmp_path, monkeypatch):
    with pytest.raises(transcripts.BadBundle):
        transcripts.unpack(tmp_path, "claude", "s1", b"not a tarball")
    monkeypatch.setattr(transcripts, "MAX_EXPANDED", 10)
    with pytest.raises(transcripts.BadBundle):
        transcripts.unpack(
            tmp_path, "claude", "s1", bundle(("config/projects/a.jsonl", None, b"x" * 64))
        )
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_node_transcripts.py -q`
Expected: FAIL，`ImportError: cannot import name 'transcripts' from 'runtime_daemon'`。

- [ ] **Step 3: 实现**

`runtime_daemon/transcripts.py`：

```python
"""助理轮次的 CLI 转录：一轮结束时打包上传，续聊前还原到沙箱里。

转录只取续聊必需的部分，路径都相对本轮目录（沙箱里的 /turn）：
- Claude：`config/projects/`（会话 jsonl，工作目录在沙箱里固定是 /work，所以项目目录名固定）；
- Codex：`config/sessions/`（rollout），加上驱动记下的会话 → 线程对应
  `driver/<会话>/codex_thread.txt`。
打成 tar.gz；超过 `MAX_BYTES` 的不上传，平台据此结束这个会话、下一轮开新会话。

还原时逐个成员检查：只接受普通文件与目录、相对路径、落在上面这几个位置之内，解压后总量有
上限；不认识的成员整个包作废（`BadBundle`），这一轮从新会话开始，不会把东西写到别处。
"""

from __future__ import annotations

import io
import posixpath
import re
import tarfile
from pathlib import Path

MAX_BYTES = 50 * 1024 * 1024
# 解压后的上限：防一个很小的压缩包展开成巨量文件。
MAX_EXPANDED = 512 * 1024 * 1024
TREES = {"claude": ("config/projects",), "codex": ("config/sessions",)}
THREAD_FILE = "codex_thread.txt"
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")


class TooLarge(Exception):
    """打包后超过上限。"""


class BadBundle(Exception):
    """下发的转录包不合规，整个作废。"""


def _members(root: Path, provider: str, session_id: str) -> list[Path]:
    found: list[Path] = []
    for tree in TREES.get(provider, ()):
        base = root / tree
        if base.is_dir():
            found += sorted(p for p in base.rglob("*") if p.is_file() and not p.is_symlink())
    if provider == "codex" and SESSION_ID.fullmatch(session_id):
        thread = root / "driver" / session_id / THREAD_FILE
        if thread.is_file() and not thread.is_symlink():
            found.append(thread)
    return found


def pack(root: Path, provider: str, session_id: str) -> bytes | None:
    """本轮的转录包；没有任何转录文件时返回 None。

    Raises:
        TooLarge: 压缩后超过 MAX_BYTES
    """
    files = _members(root, provider, session_id)
    if not files:
        return None
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in files:
            info = tar.gettarinfo(str(path), arcname=path.relative_to(root).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o600
            with path.open("rb") as handle:
                tar.addfile(info, handle)
            if buffer.tell() > MAX_BYTES:
                raise TooLarge
    data = buffer.getvalue()
    if len(data) > MAX_BYTES:
        raise TooLarge
    return data


def _allowed(name: str, provider: str, session_id: str) -> bool:
    if name.startswith("/") or name != posixpath.normpath(name) or ".." in name.split("/"):
        return False
    for tree in TREES.get(provider, ()):
        if name == tree or name.startswith(tree + "/"):
            return True
    return provider == "codex" and name == f"driver/{session_id}/{THREAD_FILE}"


def unpack(root: Path, provider: str, session_id: str, data: bytes) -> int:
    """把下发的转录包还原到本轮目录，返回写出的文件数。

    Raises:
        BadBundle: 包坏了、成员类型或位置不对、解压后太大
    """
    written = expanded = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar:
                if not _allowed(member.name, provider, session_id):
                    raise BadBundle(member.name)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise BadBundle("not a regular file")
                expanded += member.size
                if expanded > MAX_EXPANDED:
                    raise BadBundle("too large")
                source = tar.extractfile(member)
                if source is None:
                    raise BadBundle("unreadable member")
                target = root / member.name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with source, target.open("wb") as handle:
                    while chunk := source.read(1024 * 1024):
                        handle.write(chunk)
                target.chmod(0o600)
                written += 1
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BadBundle(type(exc).__name__) from None
    return written
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_node_transcripts.py -q && uv run ruff check runtime_daemon/transcripts.py tests/unit/test_assistant_node_transcripts.py`
Expected: 12 passed。

普通机器人不受影响：只新增模块。

- [ ] **Step 5: 提交**

```bash
git add runtime_daemon/transcripts.py tests/unit/test_assistant_node_transcripts.py
git commit -m "个人助理：节点上转录的打包与逐成员把关的还原"
```

---

### Task 6: 沙箱里的一轮、转录交接与清扫

这是把 Task 1–5 串起来的模块。用例里沙箱进程用替身（一个进程内的假驱动，像 CLI 一样往本轮目录写转录），平台的两个转录接口用 `httpx.MockTransport`；真起沙箱的部分已由 Task 1 的自检用例覆盖，整条链路的真机验证在 Task 16。

**Files:**
- Create: `runtime_daemon/assistant_turns.py`
- Test: `tests/unit/test_assistant_turns.py`

**Interfaces:**
- Consumes: Task 1–5 的全部接口；守护进程对象上的 `data_dir`、`socket_dir`、`config`（`api_url`、`workspace_root`、`home`、`release`、`control_proxy`，以及新增的可选键 `assistant_max_concurrent`、`assistant_egress_hosts`、`assistant_sandbox_ro_paths`）、`seats`、`task_info`、`http`（带节点令牌的 `httpx.AsyncClient`）
- Produces:
  - `AssistantTurns(daemon)`：`ready: dict[str, bool]`、`code: str`、`active: set[str]`、`background: set[asyncio.Task]`；`capability(provider) -> bool`；`start()` / `await stop()`；`await refresh_capability()`；`await sweep()`；`await download(transfer) -> bytes | None`；`await upload(transfer, data, status) -> bool`；`await refuse(frames, code, transfer=None)`；`await run(identity, provider, data, frames)`
  - `plan_turn(provider, data) -> TurnPlan`（`TurnPlan(provider, workdir, body, transfer, restore, session_id)`）、`DoneGate`（`feed(part) -> bytes`、`release() -> bytes`）、`Refused(code)`、`MESSAGES`
  - 拒绝时回给平台：HTTP 503，`{"error": {"code": <代码>, "message": …}}`；代码见 `MESSAGES`

- [ ] **Step 1: 写失败测试**

`tests/unit/test_assistant_turns.py`：

```python
"""守护进程上的个人助理轮次：拒绝条件、[DONE] 等转录上传完才放出、清扫与取消。

沙箱进程用替身：`_launch` / `_wait_ready` / `_stop` 换成一个进程内的假驱动（Unix socket 上的
最小 HTTP 服务），它像 CLI 一样把转录写进本轮目录。真起 bubblewrap 的用例见 test_assistant_sandbox。
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from runtime_daemon import assistant_turns, sandbox, transcripts, turn_credentials
from runtime_daemon.assistant_turns import AssistantTurns, DoneGate, plan_turn

TRANSFER = "0" * 31 + "1"
SSE = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'


class Frames:
    def __init__(self) -> None:
        self.events: list[tuple[str, bytes, dict]] = []

    async def add(self, data: bytes = b"", **meta) -> None:
        self.events.append(("add", data, meta))

    async def finish(self, **final) -> None:
        self.events.append(("finish", b"", final))

    def body(self) -> bytes:
        return b"".join(data for kind, data, _ in self.events if kind == "add")


class Seats:
    def active(self, provider):
        return "default"

    def env(self, provider, seat):
        return {}


class Platform:
    """平台的转录接口替身：记下收到的上传，按设定回下载。"""

    def __init__(self) -> None:
        self.download: tuple[int, bytes] = (404, b"")
        self.upload_status = 200
        self.uploads: list[tuple[str, bytes]] = []
        self.events: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.events.append("download")
            return httpx.Response(self.download[0], content=self.download[1])
        self.events.append("upload")
        self.uploads.append((request.headers["X-Transcript-Status"], request.content))
        return httpx.Response(self.upload_status, json={"code": 0})


def make(tmp_path: Path, platform: Platform) -> AssistantTurns:
    root = tmp_path / "projects"
    (root / "assistant-1").mkdir(parents=True)
    daemon = SimpleNamespace(
        data_dir=tmp_path / "data",
        socket_dir=Path(tempfile.mkdtemp(dir="/tmp")),
        config={
            "api_url": "https://coreman.example.com",
            "workspace_root": str(root),
            "assistant_max_concurrent": 1,
        },
        seats=Seats(),
        task_info={},
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(platform.handler),
            base_url="https://coreman.example.com",
        ),
    )
    daemon.data_dir.mkdir()
    turns = AssistantTurns(daemon)
    turns.ready["claude"] = True
    return turns


def request(tmp_path: Path, restore: bool = False, **extra) -> dict:
    return {
        "sandbox": True,
        "transcript": {"transfer": TRANSFER, "restore": restore},
        "working_dir": str(tmp_path / "projects" / "assistant-1"),
        "session_id": "s1",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "env_vars": {"COREMAN_FEISHU_PERSONAL_TOKEN": "turn-token"},
        **extra,
    }


def fake_sandbox(turns: AssistantTurns, monkeypatch, *, seen: list | None = None, hang=False):
    """进程内的假驱动：收到请求后写一份转录，回一段 SSE。"""
    monkeypatch.setattr(
        turn_credentials,
        "for_provider",
        lambda *_a: turn_credentials.Credential(env={"ANTHROPIC_API_KEY": "k"}),
    )
    monkeypatch.setattr(sandbox, "link_cli", lambda *_a, **_k: True)
    servers: list[asyncio.AbstractServer] = []

    def launch(layout, plan, credential, credential_file):
        return SimpleNamespace(layout=layout, plan=plan)

    async def wait_ready(layout, process):
        async def handle(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            length = int(
                next(
                    line.split(b":")[1]
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length")
                )
            )
            body = json.loads(await reader.readexactly(length))
            restored = layout.root / "config/projects/-work/s1.jsonl"
            if seen is not None:
                seen.append((body, restored.exists() and restored.read_bytes()))
            restored.parent.mkdir(parents=True, exist_ok=True)
            restored.write_bytes(b'{"turn":"new"}\n')
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
            writer.write(SSE[:30])
            await writer.drain()
            if hang:
                await asyncio.sleep(3600)
            writer.write(SSE[30:])
            await writer.drain()
            writer.close()

        servers.append(await asyncio.start_unix_server(handle, path=str(layout.driver_socket)))

    async def stop(process):
        for server in servers:
            server.close()

    monkeypatch.setattr(turns, "_launch", launch)
    monkeypatch.setattr(turns, "_wait_ready", wait_ready)
    monkeypatch.setattr(turns, "_stop", stop)


def unpacked(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}  # type: ignore[union-attr]


async def test_turn_streams_and_releases_done_only_after_the_upload(tmp_path, monkeypatch):
    platform = Platform()
    turns = make(tmp_path, platform)
    seen: list = []
    fake_sandbox(turns, monkeypatch, seen=seen)
    frames = Frames()

    original = turns.upload

    async def upload(transfer, data, status):
        # 上传的那一刻，[DONE] 还没有交给平台。
        assert b"[DONE]" not in frames.body()
        return await original(transfer, data, status)

    monkeypatch.setattr(turns, "upload", upload)
    await turns.run("call-1", "claude", request(tmp_path), frames)
    body, _ = seen[0]
    assert body["working_dir"] == "/work"
    assert "sandbox" not in body and "transcript" not in body
    assert "WebSearch" in json.loads(body["settings"])["permissions"]["deny"]
    assert frames.events[0][2]["status_code"] == 200
    assert frames.body().endswith(b"data: [DONE]\n\n")
    assert frames.events[-1][0] == "finish" and frames.events[-1][2] == {}
    status, data = platform.uploads[0]
    assert status == "ok"
    assert unpacked(data) == {"config/projects/-work/s1.jsonl": b'{"turn":"new"}\n'}
    assert not any((turns.turns_dir).iterdir())
    assert not list(turns.daemon.socket_dir.glob("a*"))
    assert turns.active == set() and turns.daemon.task_info == {}


async def test_restore_puts_the_transcript_back_before_the_driver_runs(tmp_path, monkeypatch):
    platform = Platform()
    source = tmp_path / "old"
    (source / "config/projects/-work").mkdir(parents=True)
    (source / "config/projects/-work/s1.jsonl").write_bytes(b'{"turn":"old"}\n')
    platform.download = (200, transcripts.pack(source, "claude", "s1") or b"")
    turns = make(tmp_path, platform)
    seen: list = []
    fake_sandbox(turns, monkeypatch, seen=seen)
    await turns.run("call-1", "claude", request(tmp_path, restore=True), Frames())
    assert seen[0][1] == b'{"turn":"old"}\n'
    assert platform.events == ["download", "upload"]


async def test_platform_without_a_transcript_starts_fresh(tmp_path, monkeypatch):
    platform = Platform()
    turns = make(tmp_path, platform)
    seen: list = []
    fake_sandbox(turns, monkeypatch, seen=seen)
    await turns.run("call-1", "claude", request(tmp_path, restore=True), Frames())
    assert seen[0][1] is False


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda t, m: t.ready.update(claude=False), "sandbox_unavailable"),
        (lambda t, m: t.active.add("other"), "sandbox_busy"),
        (
            lambda t, m: m.setattr(
                turn_credentials,
                "for_provider",
                lambda *_a: (_ for _ in ()).throw(
                    turn_credentials.Unavailable("credential_expiring")
                ),
            ),
            "credential_expiring",
        ),
    ],
)
async def test_refusals_answer_503_with_a_code(tmp_path, monkeypatch, setup, code):
    platform = Platform()
    turns = make(tmp_path, platform)
    fake_sandbox(turns, monkeypatch)
    setup(turns, monkeypatch)
    frames = Frames()
    await turns.run("call-1", "claude", request(tmp_path), frames)
    assert frames.events[0][2]["status_code"] == 503
    assert json.loads(frames.events[0][1])["error"]["code"] == code
    assert frames.events[-1][0] == "finish"
    # 转录没动：平台照旧保留上一轮的，下一轮还能续上。
    assert platform.uploads == [("unchanged", b"")]
    assert not turns.turns_dir.exists() or not any(turns.turns_dir.iterdir())


async def test_download_failure_refuses_instead_of_starting_blank(tmp_path, monkeypatch):
    platform = Platform()
    platform.download = (500, b"")
    turns = make(tmp_path, platform)
    fake_sandbox(turns, monkeypatch)
    frames = Frames()
    await turns.run("call-1", "claude", request(tmp_path, restore=True), frames)
    assert json.loads(frames.events[0][1])["error"]["code"] == "transcript_unavailable"
    assert platform.uploads == [("unchanged", b"")]
    assert not any(turns.turns_dir.iterdir())


async def test_failed_upload_is_retried_once_by_the_sweep_then_deleted(tmp_path, monkeypatch):
    platform = Platform()
    platform.upload_status = 500
    turns = make(tmp_path, platform)
    fake_sandbox(turns, monkeypatch)
    frames = Frames()
    await turns.run("call-1", "claude", request(tmp_path), frames)
    # [DONE] 照样放出：平台下一轮发现转录没到，会开新会话。
    assert frames.body().endswith(b"data: [DONE]\n\n")
    left = list(turns.turns_dir.iterdir())
    assert len(left) == 1 and not (left[0] / "secrets").exists()
    platform.upload_status = 500
    await turns.sweep()
    assert [s for s, _ in platform.uploads] == ["ok", "ok"]
    assert not any(turns.turns_dir.iterdir())


async def test_oversized_transcript_is_reported_not_uploaded(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "MAX_BYTES", 8)
    platform = Platform()
    turns = make(tmp_path, platform)
    fake_sandbox(turns, monkeypatch)
    await turns.run("call-1", "claude", request(tmp_path), Frames())
    assert platform.uploads == [("too_large", b"")]


async def test_cancelled_turn_uploads_in_the_background(tmp_path, monkeypatch):
    platform = Platform()
    turns = make(tmp_path, platform)
    fake_sandbox(turns, monkeypatch, hang=True)
    task = asyncio.create_task(turns.run("call-1", "claude", request(tmp_path), Frames()))
    for _ in range(100):
        if turns.active and list(turns.turns_dir.glob("*/config/projects/-work/s1.jsonl")):
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait(turns.background, timeout=5)
    assert [s for s, _ in platform.uploads] == ["ok"]
    assert turns.active == set()
    assert not any(turns.turns_dir.iterdir())


async def test_sweep_skips_running_turns_and_drops_unreadable_leftovers(tmp_path):
    platform = Platform()
    turns = make(tmp_path, platform)
    running = turns.turns_dir / "running"
    running.mkdir(parents=True)
    turns.active.add("running")
    junk = turns.turns_dir / "junk"
    junk.mkdir()
    (junk / "meta.json").write_text("not json")
    await turns.sweep()
    assert running.exists() and not junk.exists()
    assert platform.uploads == []


def test_plan_turn_rewrites_paths_and_rejects_what_the_sandbox_cannot_run(tmp_path):
    work = tmp_path / "projects" / "assistant-1"
    (work / "rules").mkdir(parents=True)
    prompt = work / "rules" / "p.md"
    prompt.write_text("x")
    data = request(
        tmp_path,
        system_prompt_file=str(prompt),
        settings=json.dumps({"permissions": {"deny": ["Bash(rm:*)"]}, "model": "m"}),
    )
    plan = plan_turn("claude", data)
    assert plan.body["system_prompt_file"] == "/work/rules/p.md"
    settings = json.loads(plan.body["settings"])
    assert settings["permissions"]["deny"] == ["Bash(rm:*)", "WebSearch", "WebFetch"]
    assert settings["model"] == "m"
    assert (plan.transfer, plan.restore, plan.session_id) == (TRANSFER, False, "s1")
    assert "settings" not in plan_turn("codex", request(tmp_path)).body
    for bad in (
        {"sandbox": "true"},
        {"transcript": {"transfer": "x", "restore": True}},
        {"transcript": None},
        {"worktree": True},
        {"add_dirs": ["/etc"]},
        {"system_prompt_file": "/etc/passwd"},
    ):
        with pytest.raises(ValueError):
            plan_turn("claude", {**request(tmp_path), **bad})


def test_done_gate_holds_the_marker_even_when_split():
    gate = DoneGate()
    out = gate.feed(b"data: a\n\ndata: [DO") + gate.feed(b"NE]\n\n")
    assert out == b"data: a\n\n"
    assert gate.feed(b"trailing") == b""
    assert gate.release() == b"data: [DONE]\n\ntrailing"
    plain = DoneGate()
    assert plain.feed(b"0123456789abcdefghij") + plain.release() == b"0123456789abcdefghij"


def test_messages_cover_every_refusal_code():
    codes = {"sandbox_unavailable", "sandbox_busy", "sandbox_start_failed"}
    codes |= {"credential_missing", "credential_unreadable", "credential_expiring"}
    codes |= {"transcript_unavailable"}
    assert codes <= set(assistant_turns.MESSAGES)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_turns.py -q`
Expected: FAIL，`ImportError: cannot import name 'assistant_turns' from 'runtime_daemon'`。

- [ ] **Step 3: 实现**

`runtime_daemon/assistant_turns.py`：

```python
"""个人助理的轮次：每一轮在沙箱里另起一个驱动，结束后上传转录、清空本轮目录。

平台在请求体里带 `sandbox: true` 与 `transcript: {"transfer": <32 位十六进制>, "restore": <布尔>}`。
这里的一切都与普通员工的轮次分开：不用常驻驱动、不占驱动锁、不改守护进程的环境；本模块的
任何失败只结束这一轮（或只影响下一次清扫），不会传到普通轮次。

一轮的顺序：
1. 本节点自检没通过、并发已满、拿不到模型凭证、转录取不下来：先告诉平台转录没动（`unchanged`，
   上一份照旧可续），再回 503 与原因代码，什么都不跑；
2. 建本轮目录，写 meta.json（清扫时靠它认出这是哪一轮），续聊时把转录还原进来；
3. 起本轮的放行代理，在 bubblewrap 里起驱动（sandbox.py），转发请求与流；
4. 流里最后的 `data: [DONE]` 先扣住：沙箱停掉、转录上传完，再放出去。平台见到 [DONE] 才收尾，
   所以下一轮开始时上一轮的转录一定已经落库（或已确定传不上去）；
5. 上传成功就删掉本轮目录；没成功的留给下一次清扫——清扫再传一次，然后无论如何都删除。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from runtime_daemon import egress, sandbox, transcripts, turn_credentials

LOG = logging.getLogger("coreman-runtime")
DEFAULT_LIMIT = 8
SELF_TEST_SECONDS = 1800.0
SWEEP_SECONDS = 600.0
STOP_SECONDS = 10.0
SOCKET_WAIT_SECONDS = 10.0
TRANSFER_TIMEOUT = 180.0
DONE = b"data: [DONE]"
TRANSFER_ID = re.compile(r"[0-9a-f]{32}")
# 守护进程启动时定下的 AI 代理（daemon.apply_proxy 写进了环境）：模型接口经它出去。
PROXY_ENV_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
# 拒绝时回给平台的说明（只给运维看；助理的所有者看到的是平台的固定文案）。
MESSAGES = {
    "sandbox_unavailable": "本节点不能在沙箱里运行个人助理",
    "sandbox_busy": "本节点的个人助理轮次已达并发上限",
    "sandbox_start_failed": "沙箱启动失败",
    "credential_missing": "节点上没有这种 AI 的登录",
    "credential_unreadable": "节点上的登录文件读不懂",
    "credential_expiring": "节点上的登录令牌即将到期，等普通轮次刷新后再试",
    "transcript_unavailable": "取不到这个会话的转录",
}


def ai_proxy() -> str | None:
    return next((os.environ[k] for k in PROXY_ENV_KEYS if os.environ.get(k, "").strip()), None)


class Refused(Exception):
    """这一轮不跑；`code` 回给平台。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TurnPlan:
    provider: str
    workdir: Path
    body: dict[str, Any]
    transfer: str
    restore: bool
    session_id: str


class DoneGate:
    """转发流里的字节，扣住 `data: [DONE]` 及其之后的部分，等 `release()` 再交出去。"""

    def __init__(self) -> None:
        self.pending = b""
        self.held = False

    def feed(self, part: bytes) -> bytes:
        if self.held:
            self.pending += part
            return b""
        self.pending += part
        index = self.pending.find(DONE)
        if index >= 0:
            out, self.pending, self.held = self.pending[:index], self.pending[index:], True
            return out
        # 标记可能跨两段到达：末尾留一小截，等下一段再判断。
        keep = len(DONE) - 1
        out, self.pending = self.pending[:-keep], self.pending[-keep:]
        return out

    def release(self) -> bytes:
        out, self.pending = self.pending, b""
        return out


def plan_turn(provider: str, data: dict[str, Any]) -> TurnPlan:
    """检查请求并改写成沙箱里的样子：工作目录换成 /work，去掉只给守护进程看的字段。

    Raises:
        ValueError: 请求不合规（按普通的请求失败处理）
    """
    if data.get("sandbox") is not True:
        raise ValueError("sandbox 只能是 true")
    transcript = data.get("transcript")
    if (
        not isinstance(transcript, dict)
        or not TRANSFER_ID.fullmatch(str(transcript.get("transfer", "")))
        or not isinstance(transcript.get("restore"), bool)
    ):
        raise ValueError("transcript 无效")
    if data.get("worktree") or data.get("add_dirs"):
        raise ValueError("个人助理的轮次不支持 worktree 与 add_dirs")
    workdir = Path(data["working_dir"])
    body = {k: v for k, v in data.items() if k not in ("sandbox", "transcript", "worktree")}
    body["working_dir"] = sandbox.INSIDE_WORK
    if body.get("system_prompt_file"):
        path = Path(body["system_prompt_file"]).resolve()
        if not path.is_relative_to(workdir):
            raise ValueError("system_prompt_file 必须在工作目录里")
        body["system_prompt_file"] = f"{sandbox.INSIDE_WORK}/{path.relative_to(workdir).as_posix()}"
    if provider == "claude":
        # 查外部资料走平台的工具：CLI 自带的联网搜索与抓取一律关掉。
        settings = json.loads(body["settings"]) if body.get("settings") else {}
        if not isinstance(settings, dict):
            raise ValueError("settings 必须是 JSON 对象")
        permissions = settings.setdefault("permissions", {})
        if not isinstance(permissions, dict):
            raise ValueError("settings.permissions 必须是对象")
        deny = [*(permissions.get("deny") or []), "WebSearch", "WebFetch"]
        permissions["deny"] = list(dict.fromkeys(deny))
        body["settings"] = json.dumps(settings)
    return TurnPlan(
        provider,
        workdir,
        body,
        str(transcript["transfer"]),
        bool(transcript["restore"]),
        str(data.get("session_id") or ""),
    )


class AssistantTurns:
    """守护进程上的个人助理轮次：能力自检、并发、沙箱里的一轮、转录清扫。"""

    def __init__(self, daemon: Any) -> None:
        self.daemon = daemon
        self.ready: dict[str, bool] = dict.fromkeys(sandbox.PROVIDERS, False)
        self.code = "not_tested"
        self.active: set[str] = set()
        self.loop_task: asyncio.Task | None = None
        self.background: set[asyncio.Task] = set()

    # ---- 配置 ------------------------------------------------------------------

    @property
    def turns_dir(self) -> Path:
        return self.daemon.data_dir / "assistant-turns"

    @property
    def limit(self) -> int:
        return int(self.daemon.config.get("assistant_max_concurrent", DEFAULT_LIMIT))

    @property
    def release(self) -> Path:
        return Path(self.daemon.config.get("release", str(Path(__file__).resolve().parents[1])))

    def extra_ro(self) -> list[Path]:
        paths = self.daemon.config.get("assistant_sandbox_ro_paths") or []
        return [Path(p) for p in paths if isinstance(p, str) and Path(p).is_absolute()]

    def capability(self, provider: str) -> bool:
        return self.ready.get(provider, False)

    def hidden_paths(self) -> list[Path]:
        """自检时必须在沙箱里看不到的路径：员工工作目录、本安装的配置、各席位的登录目录。"""
        home = Path(self.daemon.config.get("home", str(Path.home())))
        paths = [
            Path(self.daemon.config["workspace_root"]),
            self.daemon.data_dir / "config.json",
            home / ".claude",
            home / ".codex",
        ]
        for provider, key in sandbox.CONFIG_ENV.items():
            value = self.daemon.seats.env(provider, self.daemon.seats.active(provider)).get(key)
            if value:
                paths.append(Path(value))
        return paths

    # ---- 后台：自检与清扫 -----------------------------------------------------

    def start(self) -> None:
        self.loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        tasks = [t for t in (self.loop_task, *self.background) if t is not None]
        if self.background:
            # 被取消的轮次还在传转录：给它们一点时间，传不完就留给下次启动的清扫。
            await asyncio.wait(self.background, timeout=30)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _loop(self) -> None:
        await self._guard("sweep", self.sweep)
        next_test = 0.0
        while True:
            if time.monotonic() >= next_test:
                await self._guard("self_test", self.refresh_capability)
                next_test = time.monotonic() + SELF_TEST_SECONDS
            await asyncio.sleep(SWEEP_SECONDS)
            await self._guard("sweep", self.sweep)

    @staticmethod
    async def _guard(name: str, step: Any) -> None:
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 助理侧的后台工作不能拖垮守护进程
            LOG.warning("Assistant sandbox %s failed: %s", name, type(exc).__name__)

    async def refresh_capability(self) -> None:
        inputs = sandbox.SelfTestInputs(
            turns_dir=self.turns_dir,
            socket_dir=self.daemon.socket_dir,
            release=self.release,
            hidden=self.hidden_paths(),
            clis={p: sandbox.cli_mounts(p) for p in sandbox.PROVIDERS},
            extra_ro=self.extra_ro(),
        )
        result = await asyncio.to_thread(sandbox.self_test, inputs)
        if result.providers != self.ready or result.code != self.code:
            LOG.warning(
                "Assistant sandbox: %s (%s)",
                ", ".join(f"{p}={'ready' if ok else 'no'}" for p, ok in result.providers.items()),
                result.code or "ok",
            )
        self.ready, self.code = result.providers, result.code

    async def sweep(self) -> None:
        """上一轮异常退出、或传不上去留下的本轮目录：再传一次转录，然后无论如何都删除。"""
        for entry, meta in await asyncio.to_thread(self._leftovers):
            if meta is not None:
                await self._settle(entry, meta["provider"], meta["transfer"], meta["session_id"])
            await asyncio.to_thread(shutil.rmtree, entry, True)

    def _leftovers(self) -> list[tuple[Path, dict[str, str] | None]]:
        """不在跑的本轮目录及其 meta（读不懂的为 None）；顺手删掉无主的 socket 目录。"""
        found: list[tuple[Path, dict[str, str] | None]] = []
        if self.turns_dir.is_dir():
            for entry in sorted(self.turns_dir.iterdir()):
                if entry.name in self.active:
                    continue
                try:
                    raw = json.loads((entry / "meta.json").read_text())
                    meta = {k: str(raw.get(k, "")) for k in ("provider", "transfer", "session_id")}
                except (OSError, ValueError, AttributeError):
                    meta = None
                if meta is not None and (
                    meta["provider"] not in sandbox.PROVIDERS
                    or not TRANSFER_ID.fullmatch(meta["transfer"])
                ):
                    meta = None
                found.append((entry, meta))
        socket_dir = getattr(self.daemon, "socket_dir", None)
        active_runs = {f"a{turn[:10]}" for turn in self.active}
        if socket_dir is not None and socket_dir.is_dir():
            for entry in socket_dir.glob("a*"):
                if entry.is_dir() and entry.name not in active_runs:
                    shutil.rmtree(entry, ignore_errors=True)
        return found

    # ---- 与平台交换转录 --------------------------------------------------------

    async def download(self, transfer: str) -> bytes | None:
        """取续聊要用的转录；平台说没有（404）返回 None，其它失败拒绝这一轮。"""
        try:
            response = await self.daemon.http.get(
                f"/api/runtime/assistant-transcripts/{transfer}", timeout=TRANSFER_TIMEOUT
            )
        except httpx.HTTPError:
            raise Refused("transcript_unavailable") from None
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise Refused("transcript_unavailable")
        return response.content

    async def upload(self, transfer: str, data: bytes | None, status: str) -> bool:
        """上传本轮转录；平台收下（200）或已不需要（410）都算办完。"""
        try:
            response = await self.daemon.http.put(
                f"/api/runtime/assistant-transcripts/{transfer}",
                content=data or b"",
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Transcript-Status": status,
                },
                timeout=TRANSFER_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            LOG.warning("Assistant transcript upload failed: %s", type(exc).__name__)
            return False
        if response.status_code in (200, 410):
            return True
        LOG.warning("Assistant transcript upload failed: HTTP %d", response.status_code)
        return False

    async def _settle(self, root: Path, provider: str, transfer: str, session_id: str) -> bool:
        """打包并上传一轮的转录，返回平台是否已办完。"""
        try:
            data = await asyncio.to_thread(transcripts.pack, root, provider, session_id)
            status = "ok" if data else "empty"
        except transcripts.TooLarge:
            data, status = None, "too_large"
        except OSError as exc:
            LOG.warning("Assistant transcript pack failed: %s", type(exc).__name__)
            return False
        return await self.upload(transfer, data, status)

    # ---- 一轮 ------------------------------------------------------------------

    async def refuse(self, frames: Any, code: str, transfer: str | None = None) -> None:
        """不跑这一轮。先告诉平台转录没动（续聊照旧），再回 503：平台见到 503 才会结束这一轮。"""
        LOG.warning("Assistant turn refused: %s", code)
        if transfer is not None:
            await self.upload(transfer, None, "unchanged")
        body = {"error": {"code": code, "message": MESSAGES.get(code, code)}}
        await frames.add(
            json.dumps(body, ensure_ascii=False).encode(),
            status_code=503,
            content_type="application/json",
        )
        await frames.finish()

    def _admission(self, provider: str) -> str | None:
        if provider not in sandbox.PROVIDERS or not self.capability(provider):
            return "sandbox_unavailable"
        if len(self.active) >= self.limit:
            return "sandbox_busy"
        return None

    async def run(self, identity: str, provider: str, data: dict[str, Any], frames: Any) -> None:
        """跑一轮；回给平台的帧（含收尾帧）都在这里发。"""
        plan = plan_turn(provider, data)
        refused = self._admission(provider)
        if refused is not None:
            await self.refuse(frames, refused, plan.transfer)
            return
        layout = sandbox.TurnLayout.new(self.turns_dir, self.daemon.socket_dir)
        self.active.add(layout.turn_id)
        self.daemon.task_info[identity] = {
            "request_id": identity,
            "provider": provider,
            "bot_key": "",
            "user": "",
            "chat_id": "",
            "chat_type": "",
            "started_at": time.time(),
            "working_dir": "",
        }
        process: subprocess.Popen | None = None
        proxy: egress.Egress | None = None
        settled = handed_off = False
        try:
            layout.create()
            meta = {
                "transfer": plan.transfer,
                "provider": provider,
                "session_id": plan.session_id,
                "started_at": time.time(),
            }
            layout.meta.write_text(json.dumps(meta))
            seat_env = self.daemon.seats.env(provider, self.daemon.seats.active(provider))
            home = Path(self.daemon.config.get("home", str(Path.home())))
            try:
                credential = turn_credentials.for_provider(provider, seat_env, home)
            except turn_credentials.Unavailable as exc:
                raise Refused(exc.code) from None
            credential_file = turn_credentials.write(layout.secrets, credential)
            if plan.restore:
                bundle = await self.download(plan.transfer)
                if bundle:
                    try:
                        await asyncio.to_thread(
                            transcripts.unpack, layout.root, provider, plan.session_id, bundle
                        )
                    except transcripts.BadBundle:
                        LOG.warning("Assistant transcript bundle rejected")
            if not sandbox.link_cli(layout, provider):
                raise Refused("sandbox_unavailable")
            policy = egress.turn_policy(
                provider=provider,
                api_url=str(self.daemon.config["api_url"]),
                env_vars=plan.body.get("env_vars") or {},
                model_hosts=(self.daemon.config.get("assistant_egress_hosts") or {}).get(provider),
                control_proxy=self.daemon.config.get("control_proxy"),
                ai_proxy=ai_proxy(),
            )
            proxy = egress.Egress(policy)
            await proxy.start(layout.egress_socket)
            process = self._launch(layout, plan, credential, credential_file)
            await self._wait_ready(layout, process)
            gate = DoneGate()
            await self._stream(layout, plan, frames, gate)
            await self._stop(process)
            settled = await self._settle(layout.root, provider, plan.transfer, plan.session_id)
            tail = gate.release()
            if tail:
                await frames.add(tail)
            await frames.finish()
        except Refused as exc:
            settled = True
            await self.refuse(frames, exc.code, plan.transfer)
        except asyncio.CancelledError:
            # 平台取消或守护进程退出：转录放到后台去传，传完再删本轮目录。
            handed_off = True
            task = asyncio.create_task(self._settle_later(layout, plan, process, proxy))
            self.background.add(task)
            task.add_done_callback(self.background.discard)
            raise
        except Exception:
            # 驱动或流出错：照样把已有的转录传上去，下一轮还能续上。
            await self._stop(process)
            if layout.meta.exists():
                with contextlib.suppress(Exception):
                    settled = await self._settle(
                        layout.root, provider, plan.transfer, plan.session_id
                    )
            raise
        finally:
            self.daemon.task_info.pop(identity, None)
            if not handed_off:
                await self._cleanup(layout, process, proxy, settled)

    async def _settle_later(
        self,
        layout: sandbox.TurnLayout,
        plan: TurnPlan,
        process: subprocess.Popen | None,
        proxy: egress.Egress | None,
    ) -> None:
        settled = False
        try:
            await self._stop(process)
            if layout.meta.exists():
                settled = await self._settle(
                    layout.root, plan.provider, plan.transfer, plan.session_id
                )
        finally:
            await self._cleanup(layout, process, proxy, settled)

    async def _cleanup(
        self,
        layout: sandbox.TurnLayout,
        process: subprocess.Popen | None,
        proxy: egress.Egress | None,
        settled: bool,
    ) -> None:
        try:
            await self._stop(process)
            if proxy is not None:
                await proxy.close()
            shutil.rmtree(layout.secrets, ignore_errors=True)
            shutil.rmtree(layout.run, ignore_errors=True)
            if settled or not layout.meta.exists():
                shutil.rmtree(layout.root, ignore_errors=True)
        finally:
            self.active.discard(layout.turn_id)

    def _launch(
        self,
        layout: sandbox.TurnLayout,
        plan: TurnPlan,
        credential: turn_credentials.Credential,
        credential_file: Path | None,
    ) -> subprocess.Popen:
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise Refused("sandbox_unavailable")
        binary = self.release / "runtime_daemon" / "bin" / ("runtime-" + plan.provider)
        bridge = self.release / "runtime_daemon" / "sandbox_bridge.py"
        launch = sandbox.Launch(
            bwrap=bwrap,
            layout=layout,
            workdir=plan.workdir,
            provider=plan.provider,
            env=sandbox.sandbox_env(plan.provider, os.environ, credential.env),
            ro_paths=[
                self.release,
                Path(sys.base_prefix),
                *sandbox.cli_mounts(plan.provider),
                *self.extra_ro(),
            ],
            credential=credential_file,
        )
        command = sandbox.bridge_command(sys.executable, bridge, sandbox.driver_command(binary))
        log = (layout.root / "driver.log").open("ab")
        try:
            return subprocess.Popen(
                sandbox.bwrap_argv(launch, command),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError:
            raise Refused("sandbox_start_failed") from None
        finally:
            log.close()

    async def _wait_ready(self, layout: sandbox.TurnLayout, process: subprocess.Popen) -> None:
        deadline = time.monotonic() + SOCKET_WAIT_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise Refused("sandbox_start_failed")
            if layout.driver_socket.is_socket():
                try:
                    async with httpx.AsyncClient(
                        transport=httpx.AsyncHTTPTransport(uds=str(layout.driver_socket)),
                        base_url="http://runtime",
                        timeout=5,
                    ) as client:
                        if (await client.get("/v1/models")).status_code == 200:
                            return
                except httpx.HTTPError:
                    pass
            await asyncio.sleep(0.1)
        raise Refused("sandbox_start_failed")

    async def _stream(
        self, layout: sandbox.TurnLayout, plan: TurnPlan, frames: Any, gate: DoneGate
    ) -> None:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(layout.driver_socket)),
            base_url="http://runtime",
            timeout=httpx.Timeout(120, connect=10),
        ) as local:
            async with local.stream("POST", "/v1/chat/completions", json=plan.body) as response:
                await frames.add(
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type", "application/json"),
                )
                if plan.body.get("stream") and response.status_code == 200:
                    seat = self.daemon.seats.active(plan.provider)
                    chunk = {"x_runtime": {"seat_id": seat}, "choices": []}
                    await frames.add(("data: " + json.dumps(chunk) + "\n\n").encode())
                async for part in response.aiter_bytes():
                    out = gate.feed(part)
                    if out:
                        await frames.add(out)

    @staticmethod
    async def _stop(process: subprocess.Popen | None) -> None:
        """停掉沙箱：bwrap 是会话首进程，整组发信号；它一死，沙箱里的进程随之结束。"""
        if process is None or process.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), STOP_SECONDS)
        except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041 distinct on 3.10
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await asyncio.to_thread(process.wait)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_turns.py -q && uv run ruff check runtime_daemon/assistant_turns.py tests/unit/test_assistant_turns.py`
Expected: 14 passed。

普通机器人不受影响：守护进程还没有引用本模块；本模块不碰常驻驱动、驱动锁、`operations_lock` 与守护进程的环境变量（`ai_proxy()` 只读环境）。

- [ ] **Step 5: 提交**

```bash
git add runtime_daemon/assistant_turns.py tests/unit/test_assistant_turns.py
git commit -m "个人助理：沙箱里的一轮，转录上传完才放出 [DONE]，异常与取消后清扫"
```

---

### Task 7: 接进守护进程

**Files:**
- Modify: `runtime_daemon/daemon.py`
- Test: `tests/unit/test_runtime_daemon_assistant.py`

**Interfaces:**
- Consumes: `AssistantTurns`（Task 6）、`sandbox.CAPABILITY`（Task 1）
- Produces: `Daemon.assistant: AssistantTurns`；心跳里每种 AI 的能力多一个 `assistant_sandbox_v1`（布尔）；`execute` 在 `/v1/chat/completions` 的请求体带 `sandbox` 键时交给 `self.assistant.run`

- [ ] **Step 1: 写失败测试**

`tests/unit/test_runtime_daemon_assistant.py`：

```python
"""守护进程接上个人助理的轮次之后：普通轮次一字不变，带 sandbox 的才分出去，能力照自检上报。"""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
from pathlib import Path

import pytest

from runtime_daemon import agent as agent_module
from runtime_daemon import daemon as module
from runtime_daemon.daemon import Daemon


@pytest.fixture
def node(tmp_path):
    root = tmp_path / "projects"
    (root / "bot-1").mkdir(parents=True)
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"workspace_root": str(root), "api_url": "http://localhost", "node_id": "n1"})
    )
    daemon = Daemon(path)
    daemon.socket_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    daemon.agents["claude"] = agent_module.Agent(
        root=root,
        home=tmp_path / "home",
        api_url="http://localhost",
        token="synthetic-test-token-123456789",
        relay_id="relay-1",
        git_hosts=("github.com",),
    )
    return daemon


async def run(daemon: Daemon, body: dict) -> list[dict]:
    sent: list[dict] = []

    async def send_frames(identity, frames):
        sent.extend(frames)

    daemon.send_frames = send_frames
    await daemon.execute(
        {
            "id": "call-1",
            "provider": "claude",
            "method": "POST",
            "path": "/v1/chat/completions",
            "body": base64.b64encode(json.dumps(body).encode()).decode(),
        }
    )
    return sent


async def fake_driver(path: Path, received: list[dict]) -> asyncio.AbstractServer:
    async def handle(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        length = next(
            int(line.split(b":")[1])
            for line in head.split(b"\r\n")
            if line.lower().startswith(b"content-length")
        )
        received.append(json.loads(await reader.readexactly(length)))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
        writer.write(b"data: [DONE]\n\n")
        await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(handle, path=str(path))


async def test_standard_turn_reaches_the_resident_driver_unchanged(node, monkeypatch):
    async def never(*_args, **_kwargs):
        raise AssertionError("standard turns must not reach the assistant path")

    monkeypatch.setattr(node.assistant, "run", never)
    received: list[dict] = []
    server = await fake_driver(node.socket_path("claude"), received)
    body = {
        "model": "claude-opus-5-5",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "working_dir": "bot-1",
        "session_id": "s1",
        "env_vars": {"COREMAN_BOT_KEY": "demo-bot"},
    }
    try:
        frames = await run(node, body)
    finally:
        server.close()
    assert frames[-1]["done"] is True and not frames[-1].get("error")
    forwarded = received[0]
    # 与接入助理之前一样：工作目录换成绝对路径、记忆目录写进 settings，其余字段原样。
    assert forwarded["working_dir"] == str(Path(node.config["workspace_root"]) / "bot-1")
    assert set(forwarded) == {*body, "settings"}
    assert {k: forwarded[k] for k in body if k != "working_dir"} == {
        k: v for k, v in body.items() if k != "working_dir"
    }
    assert "autoMemoryDirectory" in json.loads(forwarded["settings"])


async def test_sandbox_requests_go_to_the_assistant_path_only(node, monkeypatch):
    calls: list[tuple] = []

    async def fake_run(identity, provider, data, frames):
        calls.append((identity, provider, data))
        await frames.finish()

    async def no_memory(*_args):
        raise AssertionError("assistant turns skip the shared memory layout")

    monkeypatch.setattr(node.assistant, "run", fake_run)
    monkeypatch.setattr(node, "attach_memory", no_memory)
    frames = await run(
        node,
        {
            "sandbox": True,
            "transcript": {"transfer": "0" * 32, "restore": False},
            "messages": [{"role": "user", "content": "hi"}],
            "working_dir": "bot-1",
        },
    )
    assert calls and calls[0][2]["sandbox"] is True
    assert calls[0][2]["working_dir"] == str(Path(node.config["workspace_root"]) / "bot-1")
    assert frames[-1]["done"] is True
    assert node.task_info == {}


async def test_assistant_failures_end_only_that_call(node, monkeypatch):
    async def broken(*_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(node.assistant, "run", broken)
    frames = await run(node, {"sandbox": True, "messages": [], "working_dir": "bot-1"})
    assert frames[-1]["error"] == "execution_failed"
    assert node.task_info == {}


async def test_capability_follows_the_self_test(node, monkeypatch):
    monkeypatch.setattr(module, "cli_status", lambda provider, env=None: {"installed": False})
    await node.discover()
    assert node.capabilities["claude"]["assistant_sandbox_v1"] is False
    node.assistant.ready["claude"] = True
    await node.discover()
    assert node.capabilities["claude"]["assistant_sandbox_v1"] is True
    assert node.capabilities["opencode"]["assistant_sandbox_v1"] is False
    assert node.heartbeat_body()["claude"]["assistant_sandbox_v1"] is True


async def test_background_work_never_raises(node, monkeypatch):
    async def explode():
        raise OSError("disk")

    await node.assistant._guard("sweep", explode)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_runtime_daemon_assistant.py -q`
Expected: FAIL，`AttributeError: 'Daemon' object has no attribute 'assistant'`。

- [ ] **Step 3: 实现**

`runtime_daemon/daemon.py` 改六处。

import 区：在 `from runtime_daemon.agent import (…)` 这一组之后加

```python
from runtime_daemon.assistant_turns import AssistantTurns
```

在 `from runtime_daemon.seats import SeatLogins, Seats, opencode_auth_file` 之前加

```python
from runtime_daemon.sandbox import CAPABILITY as SANDBOX_CAPABILITY
```

`Daemon.__init__` 末尾（`self.proxy: dict[str, str | bool] = …` 之后）加

```python
        # 个人助理的轮次（沙箱、转录、自检）：与上面这些普通轮次的状态完全分开。
        self.assistant = AssistantTurns(self)
```

`discover` 里 `cap["memory_layout_v1"] = True` 之后加

```python
            # 守护进程自检的结果：只有声明了它的节点才会收到个人助理的轮次。
            cap[SANDBOX_CAPABILITY] = self.assistant.capability(provider)
```

`execute` 里 `data = self.validate_command(command)` 之后、`if command["path"] == "/v1/chat/completions":`（调 `attach_memory` 的那一行）之前加

```python
            if command["path"] == "/v1/chat/completions" and "sandbox" in data:
                # 个人助理的一轮：在沙箱里另起驱动（见 assistant_turns），帧与收尾都在里面发。
                await self.assistant.run(identity, provider, data, frames)
                return
```

`run` 里 `await self.discover()` 之后加 `self.assistant.start()`；`finally` 里 `await self.cancel_all()` 之后加 `await self.assistant.stop()`。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_runtime_daemon_assistant.py tests/unit/test_runtime_daemon.py tests/unit/test_runtime_daemon_protocol.py tests/unit/test_runtime_daemon_errors.py tests/unit/test_runtime_daemon_health.py tests/unit/test_runtime_daemon_lifecycle.py tests/unit/test_runtime_daemon_install.py tests/unit/test_daemon_boundaries.py tests/unit/test_runtime_seats.py -q && uv run ruff check runtime_daemon tests/unit/test_runtime_daemon_assistant.py`
Expected: 全部通过（新文件 5 条）。

普通机器人不受影响：`test_standard_turn_reaches_the_resident_driver_unchanged`（普通请求原样交给常驻驱动，助理路径一次都没进）；现有的守护进程用例全部照常通过；`test_assistant_failures_end_only_that_call`、`test_background_work_never_raises`（助理侧的异常只结束那一个调用，后台循环吞掉异常）。

- [ ] **Step 5: 提交**

```bash
git add runtime_daemon/daemon.py tests/unit/test_runtime_daemon_assistant.py
git commit -m "个人助理：守护进程分流沙箱轮次、上报自检得出的能力"
```

---


### Task 8: 表 `assistant_transcripts`、二进制内容列与两个新列

**Files:**
- Create: `migrations/versions/0091_assistant_transcripts.py`
- Modify: `coreman/core/crypto.py`（`Cipher` 加两个方法）
- Modify: `coreman/core/assistants/sealed.py`（`Blob`、`COLUMNS` 登记、`key_ready`、各分派函数）
- Modify: `coreman/core/db/models/assistants.py`、`coreman/core/db/models/__init__.py`、`coreman/core/db/models/relay.py`、`coreman/core/db/models/runtime_nodes.py`
- Modify: `tests/conftest.py`（`BUSINESS_TABLES`）、`tests/unit/test_assistant_sealed_codec.py`
- Test: `tests/integration/test_assistant_transcripts_schema.py`

**Interfaces:**
- Consumes: 1C 的 `sealed.COLUMNS`、`sealed._cipher`、`sealed.ContentLocked`、`keys.data_cipher`；`Cipher` 的 `_aead`、`_keys`、`_key_id`
- Produces:
  - `Cipher.encrypt_bytes(data: bytes, aad: str) -> bytes`、`Cipher.decrypt_bytes(token: bytes, aad: str) -> bytes`
  - `sealed.Blob`、`sealed.is_blob_token(value) -> bool`、`sealed.seal_blob(cipher, aad, value)`、`sealed.open_blob(cipher, aad, value)`、`sealed.key_ready(session, bot_id) -> bool`
  - 模型 `AssistantTranscript`（`bot_id`、`session_id`、`blob_enc: bytes | None`、`size`、`pending_transfer`、`pending_node_id`、`created_at`、`updated_at`；主键 `(bot_id, session_id)`；`pending_transfer` 唯一）
  - `RelayServer.assistant_pool: bool`、`RuntimeCall.sandbox: bool`（都默认 false）

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_transcripts_schema.py`：

```python
"""assistant_transcripts：转录在库里是密文，密钥销毁后读不出来，普通机器人的行原样存取。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import AssistantTranscript
from coreman.core.db.session import make_session_factory
from tests.conftest import BUSINESS_TABLES
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
BLOB = b"\x1f\x8b-transcript-" + SECRET.encode()


def test_table_is_truncated_before_bots() -> None:
    assert BUSINESS_TABLES.index("assistant_transcripts") < BUSINESS_TABLES.index("bots")


async def test_blob_is_sealed_and_reads_back(db_engine: AsyncEngine, db_session: AsyncSession):
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    sid = uuid.uuid4()
    db_session.add(
        AssistantTranscript(bot_id=bot.id, session_id=sid, blob_enc=BLOB, size=len(BLOB))
    )
    await db_session.commit()
    stored = await raw(
        db_session, "SELECT blob_enc FROM assistant_transcripts WHERE session_id = :s", s=sid
    )
    assert bytes(stored).startswith(b"enc:v2:d1:") and SECRET.encode() not in bytes(stored)
    async with make_session_factory(db_engine)() as other:
        row = await other.get(AssistantTranscript, (bot.id, sid))
        assert row is not None and row.blob_enc == BLOB
        await keys.destroy_key(other, owner.id)
        await other.commit()
        locked = await other.get(AssistantTranscript, (bot.id, sid), populate_existing=True)
        assert locked is not None and sealed.is_locked(locked) and locked.blob_enc is None


async def test_standard_rows_are_left_alone(db_session: AsyncSession):
    """表本身不区分类型；普通机器人从不经过这里，就算有行也原样存取、不生成数据密钥。"""
    bot = await seed_standard(db_session)
    sid = uuid.uuid4()
    db_session.add(AssistantTranscript(bot_id=bot.id, session_id=sid, blob_enc=b"plain"))
    await db_session.commit()
    assert (
        bytes(
            await raw(
                db_session,
                "SELECT blob_enc FROM assistant_transcripts WHERE session_id = :s",
                s=sid,
            )
        )
        == b"plain"
    )
    assert await sealed.key_ready(db_session, bot.id) is False
```

`tests/unit/test_assistant_sealed_codec.py`：import 的模型里加 `AssistantTranscript`（按字母序排在 `AssistantApproval` 之后）；`test_every_content_column_is_registered` 的集合末尾加 `AssistantTranscript,`，并在 `CloudPendingApproval` 那一行断言之后加

```python
    assert set(sealed.COLUMNS[AssistantTranscript]) == {"blob_enc"}
```

文件末尾加

```python
def test_blob_round_trip_generations_and_tampering() -> None:
    data = b"\x1f\x8b" + SECRET.encode() + bytes(range(256))
    token = sealed.seal_blob(CIPHER, AAD, data)
    assert token is not None and token.startswith(b"enc:v2:d1:")
    assert SECRET.encode() not in token
    assert sealed.is_blob_token(token) and not sealed.is_blob_token(data)
    assert sealed.open_blob(CIPHER, AAD, token) == data
    # 不是密文的原样返回；空值不加密。
    assert sealed.open_blob(CIPHER, AAD, b"plain") == b"plain"
    assert sealed.seal_blob(CIPHER, AAD, b"") == b"" and sealed.seal_blob(CIPHER, AAD, None) is None
    with pytest.raises(sealed.ContentLocked):
        sealed.open_blob(Cipher(b"\x0b" * 32, key_id="d2"), AAD, token)
    with pytest.raises(DecryptError):
        sealed.open_blob(CIPHER, "assistant_transcripts.blob_enc:bot-b", token)
    tampered = token[:-1] + bytes([token[-1] ^ 1])
    with pytest.raises(DecryptError):
        sealed.open_blob(CIPHER, AAD, tampered)


def test_master_cipher_bytes_use_the_key_id() -> None:
    master = Cipher(b"\x01" * 32, key_id="k9", previous={"k1": b"\x02" * 32})
    token = master.encrypt_bytes(b"payload", "t.c:x")
    assert token.startswith(b"enc:v2:k9:")
    assert master.decrypt_bytes(token, "t.c:x") == b"payload"
    old = Cipher(b"\x02" * 32, key_id="k1").encrypt_bytes(b"old", "t.c:x")
    assert master.decrypt_bytes(old, "t.c:x") == b"old"
    for bad in (b"enc:v1:abc", b"enc:v2:nokid", b"enc:v2:k9:short"):
        with pytest.raises(DecryptError):
            master.decrypt_bytes(bad, "t.c:x")
```

`tests/conftest.py`：`BUSINESS_TABLES` 开头（`"assistant_approvals"` 之前）加一行 `"assistant_transcripts",`。

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_transcripts_schema.py tests/unit/test_assistant_sealed_codec.py -q`
Expected: FAIL，`ImportError: cannot import name 'AssistantTranscript' from 'coreman.core.db.models'`。

- [ ] **Step 3: `Cipher` 的二进制加解密**

`coreman/core/crypto.py`：在 `Cipher.open` 之后、`Cipher.encrypt` 之前加

```python
    def encrypt_bytes(self, data: bytes, aad: str) -> bytes:
        """二进制内容的密文：`enc:v2:<kid>:` 加上 nonce|ciphertext|tag，不再套一层 base64。"""
        nonce = os.urandom(_NONCE_LEN)
        body = nonce + self._aead.encrypt(nonce, data, aad.encode("utf-8"))
        return f"{PREFIX_V2}{self._key_id}:".encode("ascii") + body

    def decrypt_bytes(self, token: bytes, aad: str) -> bytes:
        """解 `encrypt_bytes` 的输出；按密文里的密钥标识选密钥。"""
        prefix = PREFIX_V2.encode("ascii")
        if not token.startswith(prefix):
            raise DecryptError("不是 enc:v2 二进制密文")
        kid, sep, raw = token[len(prefix) :].partition(b":")
        name = kid.decode("ascii", errors="replace")
        if not sep or not KID_RE.match(name):
            raise DecryptError("enc:v2 密文缺少密钥标识")
        aead = self._keys.get(name)
        if aead is None:
            raise DecryptError(f"未配置密钥标识 {name} 对应的密钥")
        if len(raw) < _NONCE_LEN + _TAG_LEN:
            raise DecryptError("密文过短")
        try:
            return aead.decrypt(raw[:_NONCE_LEN], raw[_NONCE_LEN:], aad.encode("utf-8"))
        except InvalidTag as exc:
            raise DecryptError("密钥或 AAD 不匹配") from exc
```

- [ ] **Step 4: 模型与迁移**

`coreman/core/db/models/assistants.py`：模块文档串改成 `"""个人助理的数据密钥、审批流水、「处理中」标记与节点转录。"""`；`from sqlalchemy import (…)` 里按字母序加 `LargeBinary`；文件末尾加

```python
class AssistantTranscript(Base):
    """节点上一个会话的 CLI 转录：每轮结束由节点上传，下一轮续聊前下发，节点上不留。

    `blob_enc` 登记在 `sealed.COLUMNS`（二进制列）：库里是用所有者数据密钥加密的 tar.gz，经会话
    取出的行上是明文字节。`pending_transfer` 是派给节点的那一轮的交接编号：派出时写上，节点上传
    时凭它对上号并清空；下一轮派出时它还在，说明上一轮的转录没传回来，这个会话不再续。
    """

    __tablename__ = "assistant_transcripts"
    __table_args__ = (
        PrimaryKeyConstraint("bot_id", "session_id"),
        UniqueConstraint("pending_transfer", name="uq_assistant_transcripts_pending_transfer"),
        Index("assistant_transcripts_updated_idx", "updated_at"),
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    blob_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    size: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    pending_transfer: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    pending_node_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runtime_nodes.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
    updated_at: Mapped[datetime] = _ts(server_default=text("now()"))
```

`coreman/core/db/models/__init__.py`：`from coreman.core.db.models.assistants import (…)` 与末尾的 `__all__ += […]` 里各加 `AssistantTranscript`（排在 `AssistantMark` 之后）。

`coreman/core/db/models/relay.py`：`RelayServer` 的 `is_active` 一行之后加

```python
    # 个人助理池：开通时在池里挑助理最少的在线节点（见 core/assistants/runtime.py）。
    assistant_pool: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
```

`coreman/core/db/models/runtime_nodes.py`：`RuntimeCall` 的 `error` 一行之后加

```python
    # 个人助理在沙箱里跑的一轮：领取时按节点的助理并发上限另算，排在普通调用之后。
    sandbox: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
```

`migrations/versions/0091_assistant_transcripts.py`：

```python
"""Personal assistant node isolation: encrypted transcripts, assistant pool, sandboxed calls."""

import sqlalchemy as sa
from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_transcripts",
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("blob_enc", sa.LargeBinary(), nullable=True),
        sa.Column("size", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("pending_transfer", sa.Uuid(), nullable=True),
        sa.Column(
            "pending_node_id",
            sa.Uuid(),
            sa.ForeignKey("runtime_nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("bot_id", "session_id"),
        sa.UniqueConstraint("pending_transfer", name="uq_assistant_transcripts_pending_transfer"),
    )
    op.create_index("assistant_transcripts_updated_idx", "assistant_transcripts", ["updated_at"])
    op.add_column(
        "relay_servers",
        sa.Column("assistant_pool", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "runtime_calls",
        sa.Column("sandbox", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("runtime_calls", "sandbox")
    op.drop_column("relay_servers", "assistant_pool")
    op.drop_index("assistant_transcripts_updated_idx", table_name="assistant_transcripts")
    op.drop_table("assistant_transcripts")
```

- [ ] **Step 5: `sealed` 的二进制列**

`coreman/core/assistants/sealed.py` 改这几处：

1. 模块文档串「封存形式」列表里 `- 文本列：密文串本身。` 之后加一行 `- 二进制列：`encrypt_bytes` 的输出（`enc:v2:d<代数>:` 加原始密文字节），不套 base64。`。
2. 模型 import 里加 `AssistantTranscript`（排在 `AssistantApproval` 之后）。
3. `Kind = JsonSpec | Whole | Text` 换成

```python
@dataclass(frozen=True)
class Blob:
    """二进制列（bytea）：整段加密，读不出来时给 None。"""


Kind = JsonSpec | Whole | Text | Blob
```

4. `COLUMNS` 末尾（`CloudPendingApproval` 那一项之后）加

```python
    # 节点上 CLI 的转录：一轮结束后上传，续聊前下发。
    AssistantTranscript: {"blob_enc": Blob()},
```

5. `def seal_text(` 之前加

```python
_DATA_KID_BYTES = re.compile(rb"^enc:v2:d[0-9]+:")


def is_blob_token(value: object) -> bool:
    """是不是数据密钥写出的二进制密文。"""
    return isinstance(value, bytes) and _DATA_KID_BYTES.match(value) is not None


def seal_blob(cipher: Cipher, aad_: str, value: bytes | None) -> bytes | None:
    return cipher.encrypt_bytes(value, aad_) if value else value


def open_blob(cipher: Cipher, aad_: str, value: Any) -> Any:
    """同 `_decrypt`：另一代数据密钥加密的按已锁定处理，同一代解不开照抛。"""
    if not is_blob_token(value):
        return value
    kid = value[len(PREFIX_V2) :].split(b":", 1)[0].decode("ascii")
    if kid != cipher.key_id:
        raise ContentLocked("内容由已作废的旧一代数据密钥加密")
    return cipher.decrypt_bytes(value, aad_)
```

6. `def _seal(` 之前加

```python
async def key_ready(session: AsyncSession, bot_id: uuid.UUID) -> bool:
    """这条助理记录的内容现在解得开：数据密钥在、没销毁、主密钥解得开包裹。不签发新密钥。

    普通机器人返回 False。只看密钥，不看某一段具体的密文。
    """
    try:
        return await _cipher(session, bot_id, create_key=False) is not None
    except (ContentLocked, DecryptError):
        return False
```

7. 各分派函数加 `Blob` 分支（放在各自最后一个 `return` 之前）：`_seal` 加 `if isinstance(kind, Blob): return seal_blob(cipher, aad_, value)`；`_open` 加 `if isinstance(kind, Blob): return open_blob(cipher, aad_, value)`；`_looks_sealed` 在 `Text` 分支之后加 `if isinstance(kind, Blob): return is_blob_token(value)`；`_nothing` 在 `JsonSpec` 分支之后加 `if isinstance(kind, Blob): return isinstance(value, bytes) and not value`；`_blank` 在 `JsonSpec` 分支之后加 `if isinstance(kind, Blob): return None`；`_dropped` 在 `Whole` 分支之后加 `if isinstance(kind, Blob): return None`。每个分支照仓库风格写成两行（`if …:` 换行缩进 `return …`）。

- [ ] **Step 6: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_transcripts_schema.py tests/unit/test_assistant_sealed_codec.py tests/unit/test_assistant_sealed_ledger.py tests/integration/test_assistant_sealed_*.py -q && uv run alembic check && uv run mypy coreman`
Expected: 全部通过（新文件 3 条，codec 文件多 2 条）；`alembic check` 报 `No new upgrade operations detected.`。台账这时不用改：登记了模型，但还没有任何代码查询或写入它。

普通机器人不受影响：`test_standard_rows_are_left_alone`（表本身不区分类型，普通机器人的行原样存取、`key_ready` 为假、不会替它生成数据密钥）；`RelayServer.assistant_pool` 与 `RuntimeCall.sandbox` 都有库默认值 false，现有写入不用改；1C 的 `test_assistant_sealed_*` 全部照常通过。

- [ ] **Step 7: 提交**

```bash
git add migrations/versions/0091_assistant_transcripts.py coreman/core/crypto.py coreman/core/assistants/sealed.py coreman/core/db/models/assistants.py coreman/core/db/models/__init__.py coreman/core/db/models/relay.py coreman/core/db/models/runtime_nodes.py tests/conftest.py tests/unit/test_assistant_sealed_codec.py tests/integration/test_assistant_transcripts_schema.py
git commit -m "个人助理：转录表 assistant_transcripts 与二进制内容列封存，助理池与沙箱调用两列"
```

---

### Task 9: 转录的交接（平台侧）

**Files:**
- Create: `coreman/core/assistants/transcripts.py`
- Modify: `coreman/core/chat/sessions.py`（`renew`）
- Modify: `tests/unit/test_assistant_sealed_ledger.py`
- Test: `tests/integration/test_assistant_transcripts.py`

**Interfaces:**
- Consumes: Task 8 的模型与 `sealed.key_ready`；`sessions.SessionInfo`、`sessions.switch_to`
- Produces:
  - `sessions.renew(session, *, bot_id, session_key, backend, speaker_user_id) -> SessionInfo`（新会话，`is_new=True`）
  - `transcripts.MAX_BYTES = 50 MiB`、`transcripts.STATUSES = ("ok", "empty", "too_large", "unchanged")`
  - `transcripts.Plan(transfer: uuid.UUID, restore: bool)`，`Plan.body() -> {"transfer": <hex>, "restore": <bool>}`
  - `transcripts.prepare(session, *, bot, node_id, info, session_key, backend, speaker_user_id) -> tuple[SessionInfo, Plan]`
  - `transcripts.download(session, *, node_id, transfer) -> bytes | None`
  - `transcripts.upload(session, *, node_id, transfer, status, data) -> "stored" | "kept" | "cleared" | "stale"`
  - `transcripts.purge(session, before: datetime) -> int`

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_transcripts.py`：

```python
"""assistant_transcripts：库里是密文；交接编号决定续不续；对不上号、读不出来都从新会话开始。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed, transcripts
from coreman.core.chat import sessions
from coreman.core.db.models import AssistantTranscript, ChatSession
from coreman.core.db.session import make_session_factory
from tests.fakes.runtime_node import add_node
from tests.integration.assistant_helpers import SECRET, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
BLOB = b"\x1f\x8b-transcript-" + SECRET.encode()


async def start(session: AsyncSession, bot, node_id, *, key: str = "oc_owner"):
    info = await sessions.get_or_create(
        session,
        bot_id=bot.id,
        session_key=key,
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    out = await transcripts.prepare(
        session,
        bot=bot,
        node_id=node_id,
        info=info,
        session_key=key,
        backend="claude",
        speaker_user_id=None,
    )
    await session.commit()
    return out


async def test_turns_hand_over_and_resume(db_session: AsyncSession):
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    node = await add_node(db_session)
    info, plan = await start(db_session, bot, node.id)
    assert info.is_new and plan.restore is False
    assert plan.body() == {"transfer": plan.transfer.hex, "restore": False}
    assert await transcripts.download(db_session, node_id=node.id, transfer=plan.transfer) is None
    result = await transcripts.upload(
        db_session, node_id=node.id, transfer=plan.transfer, status="ok", data=BLOB
    )
    await db_session.commit()
    assert result == "stored"
    # 交接办完：编号作废，同一编号再传是 stale。
    again = await transcripts.upload(
        db_session, node_id=node.id, transfer=plan.transfer, status="ok", data=BLOB
    )
    assert again == "stale"
    second, plan2 = await start(db_session, bot, node.id)
    assert second.relay_session_id == info.relay_session_id and plan2.restore is True
    got = await transcripts.download(db_session, node_id=node.id, transfer=plan2.transfer)
    assert got == BLOB
    # 节点没跑这一轮（拒绝了）：上一份照旧可用。
    kept = await transcripts.upload(
        db_session, node_id=node.id, transfer=plan2.transfer, status="unchanged", data=b""
    )
    await db_session.commit()
    assert kept == "kept"
    third, plan3 = await start(db_session, bot, node.id)
    assert third.relay_session_id == info.relay_session_id and plan3.restore is True


@pytest.mark.parametrize("status", ["empty", "too_large"])
async def test_no_usable_transcript_means_a_new_session(db_session: AsyncSession, status):
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    node = await add_node(db_session)
    info, plan = await start(db_session, bot, node.id)
    await transcripts.upload(
        db_session, node_id=node.id, transfer=plan.transfer, status="ok", data=BLOB
    )
    await db_session.commit()
    _, plan2 = await start(db_session, bot, node.id)
    assert (
        await transcripts.upload(
            db_session, node_id=node.id, transfer=plan2.transfer, status=status, data=b""
        )
        == "cleared"
    )
    await db_session.commit()
    renewed, plan3 = await start(db_session, bot, node.id)
    assert renewed.is_new and renewed.relay_session_id != info.relay_session_id
    assert plan3.restore is False
    old = await db_session.get(AssistantTranscript, (bot.id, info.relay_session_id))
    assert old is None


async def test_a_turn_whose_transcript_never_came_back_is_not_resumed(db_session: AsyncSession):
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    node = await add_node(db_session)
    info, plan = await start(db_session, bot, node.id)
    await transcripts.upload(
        db_session, node_id=node.id, transfer=plan.transfer, status="ok", data=BLOB
    )
    await db_session.commit()
    _, lost = await start(db_session, bot, node.id)  # 这一轮的转录没有传回来
    assert lost.restore is True
    after, plan3 = await start(db_session, bot, node.id)
    assert after.is_new and after.relay_session_id != info.relay_session_id
    assert plan3.restore is False
    # 迟到的上传对不上号，不会改动任何东西。
    late = await transcripts.upload(
        db_session, node_id=node.id, transfer=lost.transfer, status="ok", data=BLOB
    )
    assert late == "stale"
    row = await db_session.get(ChatSession, (bot.id, "oc_owner"), populate_existing=True)
    assert row is not None and row.relay_session_id == after.relay_session_id


async def test_another_node_cannot_take_or_replace_it(db_session: AsyncSession):
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    node, stranger = await add_node(db_session), await add_node(db_session, name="other")
    _, plan = await start(db_session, bot, node.id)
    assert (
        await transcripts.upload(
            db_session, node_id=stranger.id, transfer=plan.transfer, status="ok", data=BLOB
        )
        == "stale"
    )
    await transcripts.upload(
        db_session, node_id=node.id, transfer=plan.transfer, status="ok", data=BLOB
    )
    await db_session.commit()
    _, plan2 = await start(db_session, bot, node.id)
    assert (
        await transcripts.download(db_session, node_id=stranger.id, transfer=plan2.transfer) is None
    )


async def test_destroyed_key_or_tampered_blob_starts_fresh(
    db_engine: AsyncEngine, db_session: AsyncSession
):
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    node = await add_node(db_session)
    info, plan = await start(db_session, bot, node.id)
    await transcripts.upload(
        db_session, node_id=node.id, transfer=plan.transfer, status="ok", data=BLOB
    )
    await db_session.commit()
    _, plan2 = await start(db_session, bot, node.id)
    await db_session.execute(
        text(
            "UPDATE assistant_transcripts SET blob_enc = overlay(blob_enc placing '\\x00'::bytea"
            " from length(blob_enc)) WHERE bot_id = :b"
        ),
        {"b": bot.id},
    )
    await db_session.commit()
    async with make_session_factory(db_engine)() as request_session:
        sealed.tolerate_undecryptable(request_session)
        got = await transcripts.download(request_session, node_id=node.id, transfer=plan2.transfer)
        assert got is None
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    await transcripts.upload(
        db_session, node_id=node.id, transfer=plan2.transfer, status="unchanged", data=b""
    )
    await db_session.commit()
    renewed, plan3 = await start(db_session, bot, node.id)
    assert renewed.relay_session_id != info.relay_session_id and plan3.restore is False


async def test_purge_drops_only_old_rows(db_session: AsyncSession):
    bot = await seed_standard(db_session)
    old, fresh = uuid.uuid4(), uuid.uuid4()
    db_session.add_all(
        [
            AssistantTranscript(
                bot_id=bot.id, session_id=old, updated_at=datetime.now(UTC) - timedelta(days=9)
            ),
            AssistantTranscript(bot_id=bot.id, session_id=fresh),
        ]
    )
    await db_session.commit()
    assert await transcripts.purge(db_session, datetime.now(UTC) - timedelta(days=4)) == 1
    await db_session.commit()
    assert await db_session.get(AssistantTranscript, (bot.id, fresh)) is not None
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_transcripts.py -q`
Expected: FAIL，`ImportError: cannot import name 'transcripts' from 'coreman.core.assistants'`。

- [ ] **Step 3: 实现**

`coreman/core/chat/sessions.py`：在 `async def settle_carry(` 之前加

```python
async def renew(
    session: AsyncSession,
    *,
    bot_id: uuid.UUID,
    session_key: str,
    backend: str,
    speaker_user_id: uuid.UUID | None,
) -> SessionInfo:
    """这个会话键换一个新的 relay 会话（旧会话续不上时用），不带续接。"""
    sid = uuid.uuid4()
    await switch_to(
        session,
        bot_id=bot_id,
        session_key=session_key,
        relay_session_id=sid,
        backend=backend,
        speaker_user_id=speaker_user_id,
    )
    return SessionInfo(sid, True, False)
```

`coreman/core/assistants/transcripts.py`：

```python
"""个人助理的 CLI 转录：平台保管，节点每轮取用后清除。

一轮的交接：
1. 派出之前（`prepare`，worker 开流时，持有机器人行锁）：定下这一轮续不续上一轮的转录，写下
   本轮的交接编号 `pending_transfer` 与所派节点。请求体带 `transcript: {"transfer", "restore"}`；
2. 节点要续聊就凭编号来取（`download`），一轮结束凭同一编号上传（`upload`），平台清掉编号；
3. 下一轮派出时编号还在，说明上一轮的转录没传回来（节点挂了、网断了）：不拿旧的那份续，
   换一个新的 relay 会话。

内容是节点打好的 tar.gz，经 ORM 写入时由 `sealed` 用所有者的数据密钥加密（`blob_enc` 是二进制
内容列）。日志只记代码、编号与大小。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from coreman.core.assistants import sealed
from coreman.core.chat import sessions
from coreman.core.db.models import AssistantTranscript, Bot

MAX_BYTES = 50 * 1024 * 1024
# 节点上传时说明这一轮的转录怎么了：ok 带内容；empty 没有转录；too_large 超过上限；
# unchanged 节点没跑这一轮（拒绝了），上一份照旧可用。
STATUSES = ("ok", "empty", "too_large", "unchanged")


@dataclass(frozen=True)
class Plan:
    transfer: uuid.UUID
    restore: bool

    def body(self) -> dict[str, Any]:
        """请求体里的 `transcript` 字段。"""
        return {"transfer": self.transfer.hex, "restore": self.restore}


def _key(bot_id: uuid.UUID, session_id: uuid.UUID) -> Any:
    return (AssistantTranscript.bot_id == bot_id) & (AssistantTranscript.session_id == session_id)


async def prepare(
    session: AsyncSession,
    *,
    bot: Bot,
    node_id: uuid.UUID,
    info: sessions.SessionInfo,
    session_key: str,
    backend: str,
    speaker_user_id: uuid.UUID | None,
) -> tuple[sessions.SessionInfo, Plan]:
    """派出一轮之前：返回本轮实际用的会话（可能换了新的）与交接计划。调用方负责提交。

    续得上的条件：不是新会话、库里有上一轮的转录、那一轮的交接已办完、数据密钥可用。
    续不上又不是新会话时换一个新的 relay 会话：驱动拿着旧会话号续，会在节点上悄悄从空会话开始，
    换新的至少让这一轮明明白白是新会话。
    """
    restore = False
    if not info.is_new:
        row = (
            await session.execute(
                select(AssistantTranscript.pending_transfer, AssistantTranscript.size)
                .where(_key(bot.id, info.relay_session_id))
                .with_for_update()
            )
        ).first()
        restore = bool(
            row is not None
            and row[0] is None
            and row[1] > 0
            and await sealed.key_ready(session, bot.id)
        )
        if not restore:
            await session.execute(
                delete(AssistantTranscript).where(_key(bot.id, info.relay_session_id))
            )
            info = await sessions.renew(
                session,
                bot_id=bot.id,
                session_key=session_key,
                backend=backend,
                speaker_user_id=speaker_user_id,
            )
    transfer = uuid.uuid4()
    stmt = insert(AssistantTranscript).values(
        bot_id=bot.id,
        session_id=info.relay_session_id,
        pending_transfer=transfer,
        pending_node_id=node_id,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[AssistantTranscript.bot_id, AssistantTranscript.session_id],
            set_={"pending_transfer": transfer, "pending_node_id": node_id},
        )
    )
    return info, Plan(transfer, restore)


async def download(
    session: AsyncSession, *, node_id: uuid.UUID, transfer: uuid.UUID
) -> bytes | None:
    """节点续聊前来取。编号或节点对不上、内容读不出来（密钥已销毁、解不开）都返回 None。

    调用方要先对会话调 `sealed.tolerate_undecryptable`：解不开时按没有处理，节点从新会话开始。
    """
    key = (
        await session.execute(
            select(AssistantTranscript.bot_id, AssistantTranscript.session_id).where(
                AssistantTranscript.pending_transfer == transfer,
                AssistantTranscript.pending_node_id == node_id,
            )
        )
    ).first()
    if key is None:
        return None
    row = await session.get(AssistantTranscript, (key[0], key[1]), populate_existing=True)
    if row is None or sealed.is_locked(row) or not row.blob_enc:
        return None
    return bytes(row.blob_enc)


async def upload(
    session: AsyncSession,
    *,
    node_id: uuid.UUID,
    transfer: uuid.UUID,
    status: str,
    data: bytes,
) -> str:
    """节点交回本轮的转录，返回 stored / kept / cleared，对不上号返回 stale。调用方负责提交。"""
    row = await session.scalar(
        select(AssistantTranscript)
        .where(
            AssistantTranscript.pending_transfer == transfer,
            AssistantTranscript.pending_node_id == node_id,
        )
        .options(defer(AssistantTranscript.blob_enc))
        .with_for_update()
    )
    if row is None:
        return "stale"
    row.pending_transfer = None
    row.pending_node_id = None
    row.updated_at = datetime.now(UTC)
    if status == "unchanged":
        result = "kept"
    elif status == "ok" and data:
        row.blob_enc, row.size, result = data, len(data), "stored"
    else:
        row.blob_enc, row.size, result = None, 0, "cleared"
    await session.flush()
    return result


async def purge(session: AsyncSession, before: datetime) -> int:
    """删掉在 `before` 之前就不再更新的转录（会话早已过期，不会再续）。调用方负责提交。"""
    result = cast(
        CursorResult[Any],
        await session.execute(
            delete(AssistantTranscript).where(AssistantTranscript.updated_at < before)
        ),
    )
    return int(result.rowcount or 0)
```

`tests/unit/test_assistant_sealed_ledger.py` 的三张台账各加一行（位置按文件路径排序）：

```python
    # 转录：上传时取整行（不取转录本身），session.scalar 自动还原
    "coreman/core/assistants/transcripts.py": 1,
```

加进 `ENTITY_SELECTS`；

```python
    "coreman/core/assistants/transcripts.py": 1,  # 上传时 defer 掉旧的转录，不读它
```

加进 `COLUMN_REFERENCES`；

```python
    # 转录：派出前只写交接编号（元数据）；内容经 ORM 写入，flush 钩子封存
    "coreman/core/assistants/transcripts.py": 1,
```

加进 `WRITE_STATEMENTS`。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_transcripts.py tests/unit/test_assistant_sealed_ledger.py tests/integration/test_chat_sessions.py tests/integration/test_session_switch.py -q && uv run mypy coreman`
Expected: 全部通过（新文件 7 条）。

普通机器人不受影响：`transcripts` 只在助理的开流与节点接口里被调用（Task 11、14）；`sessions.renew` 是新函数，`get_or_create` 等原有函数不变。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants/transcripts.py coreman/core/chat/sessions.py tests/unit/test_assistant_sealed_ledger.py tests/integration/test_assistant_transcripts.py
git commit -m "个人助理：转录的交接编号，传不回来就不续，读不出来就开新会话"
```

---

### Task 10: 助理用哪台运行时

**Files:**
- Create: `coreman/core/assistants/runtime.py`
- Modify: `coreman/core/bots/switch_relay.py`（`validate_switch`）
- Test: `tests/integration/test_assistant_runtime.py`

**Interfaces:**
- Consumes: `relay_policy.relay_available`、`transport.online`、Task 8 的 `RelayServer.assistant_pool`
- Produces:
  - `runtime.SANDBOX_CAPABILITY = "assistant_sandbox_v1"`、`runtime.ASSISTANT_CONCURRENCY_KEY = "assistant_node_concurrency"`、`runtime.PAUSE_MESSAGES`
  - `node_sandboxed(node, provider=None) -> bool`
  - `unavailable_reason(relay, node) -> str | None`（`runtime_disabled` / `runtime_offline` / `runtime_draining` / `sandbox_unsupported` / `login_required`）
  - `eligible(relay, owner_id) -> bool`、`await turn_blocker(session, bot) -> str | None`（另有 `runtime_missing`、`runtime_not_allowed`）、`pause_message(reason) -> str`
  - `assignment_error(relay, node, owner_id) -> str | None`、`await pick_pool_relay(session, provider) -> RelayServer | None`（1F 开通时调用）、`await hosts_standard_bots(session, node_id) -> bool`（Task 12 使用）

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_runtime.py`：

```python
"""个人助理用哪台运行时：池里挑最空的节点；只派到能隔离运行的节点；不行就暂停并告诉所有者。"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import runtime
from coreman.core.bots.switch_relay import SwitchError, validate_switch
from coreman.core.db.models import (
    RelayServer,
    RuntimeNode,
    User,
)
from coreman.core.runtime_nodes.transport import now
from tests.fakes.runtime_node import add_node, add_node_relay
from tests.integration.assistant_helpers import seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")


def ready_caps(provider: str = "claude") -> dict:
    return {provider: {"installed": True, "login": "ready", runtime.SANDBOX_CAPABILITY: True}}


async def pool_relay(session: AsyncSession, name: str, **node_values) -> RelayServer:
    node_values.setdefault("capabilities", ready_caps())
    node_values.setdefault("heartbeat_at", now())
    node = await add_node(session, name=name, **node_values)
    return await add_node_relay(session, node=node, assistant_pool=True)


def test_reasons_cover_every_way_a_node_can_fail() -> None:
    relay = RelayServer(
        runtime_node_id=uuid.uuid4(), name="r", model_provider="claude", is_active=True
    )
    base = {"id": relay.runtime_node_id, "is_active": True, "draining": False}
    node = RuntimeNode(**base, heartbeat_at=now(), capabilities=ready_caps())
    assert runtime.unavailable_reason(relay, node) is None
    assert runtime.unavailable_reason(relay, None) == "runtime_disabled"
    stale = RuntimeNode(
        **base, heartbeat_at=now() - timedelta(minutes=5), capabilities=ready_caps()
    )
    assert runtime.unavailable_reason(relay, stale) == "runtime_offline"
    draining = RuntimeNode(
        **{**base, "draining": True}, heartbeat_at=now(), capabilities=ready_caps()
    )
    assert runtime.unavailable_reason(relay, draining) == "runtime_draining"
    old = RuntimeNode(**base, heartbeat_at=now(), capabilities={"claude": {"login": "ready"}})
    assert runtime.unavailable_reason(relay, old) == "sandbox_unsupported"
    other = RuntimeNode(**base, heartbeat_at=now(), capabilities=ready_caps("codex"))
    assert runtime.unavailable_reason(relay, other) == "sandbox_unsupported"
    logged_out = RuntimeNode(
        **base,
        heartbeat_at=now(),
        capabilities={"claude": {"login": "required", runtime.SANDBOX_CAPABILITY: True}},
    )
    assert runtime.unavailable_reason(relay, logged_out) == "login_required"
    for reason in runtime.PAUSE_MESSAGES:
        assert runtime.pause_message(reason).startswith("你的个人助理暂停了：")


def test_eligible_relays_are_the_pool_or_the_owners_private_runtime() -> None:
    me, other = uuid.uuid4(), uuid.uuid4()
    assert runtime.eligible(RelayServer(assistant_pool=True, visibility="all"), me)
    assert runtime.eligible(
        RelayServer(assistant_pool=False, visibility="private", owner_id=me), me
    )
    assert not runtime.eligible(
        RelayServer(assistant_pool=False, visibility="private", owner_id=other), me
    )
    assert not runtime.eligible(RelayServer(assistant_pool=False, visibility="all"), me)


async def test_pick_pool_relay_takes_the_emptiest_ready_node(db_session: AsyncSession) -> None:
    busy = await pool_relay(db_session, "busy")
    idle = await pool_relay(db_session, "idle")
    await pool_relay(db_session, "offline", heartbeat_at=now() - timedelta(minutes=5))
    await pool_relay(
        db_session, "old", capabilities={"claude": {"installed": True, "login": "ready"}}
    )
    outside = await add_node_relay(db_session, name="outside")
    outside_node = await db_session.get(RuntimeNode, outside.runtime_node_id)
    assert outside_node is not None
    outside_node.capabilities, outside_node.heartbeat_at = ready_caps(), now()
    for _ in range(2):
        bot, _ = await seed_assistant(db_session)
        bot.relay_server_id = busy.id
    # 节点上的普通员工不算数。
    standard = await seed_standard(db_session)
    standard.relay_server_id = idle.id
    await db_session.commit()
    assert (await runtime.pick_pool_relay(db_session, "claude")).id == idle.id  # type: ignore[union-attr]
    mine, _ = await seed_assistant(db_session)
    mine.relay_server_id = idle.id
    other, _ = await seed_assistant(db_session)
    other.relay_server_id = idle.id
    third, _ = await seed_assistant(db_session)
    third.relay_server_id = idle.id
    await db_session.commit()
    assert (await runtime.pick_pool_relay(db_session, "claude")).id == busy.id  # type: ignore[union-attr]
    assert await runtime.pick_pool_relay(db_session, "codex") is None


async def test_turn_blocker_and_assignment(db_session: AsyncSession) -> None:
    bot, owner_user = await seed_assistant(db_session)
    assert await runtime.turn_blocker(db_session, bot) == "runtime_missing"
    shared = await add_node_relay(db_session, name="shared")
    bot.relay_server_id = shared.id
    assert await runtime.turn_blocker(db_session, bot) == "runtime_not_allowed"
    private = await pool_relay(db_session, "private")
    private.assistant_pool, private.visibility, private.owner_id = False, "private", owner_user.id
    bot.relay_server_id = private.id
    assert await runtime.turn_blocker(db_session, bot) is None
    node = await db_session.get(RuntimeNode, private.runtime_node_id)
    assert node is not None
    node.heartbeat_at = now() - timedelta(minutes=5)
    assert await runtime.turn_blocker(db_session, bot) == "runtime_offline"
    assert runtime.assignment_error(shared, None, owner_user.id) is not None
    assert (
        runtime.assignment_error(private, node, owner_user.id)
        == "运行时现在离线，恢复在线后再发一次消息即可"
    )


async def test_switching_an_assistant_only_to_a_sandboxed_eligible_runtime(
    db_session: AsyncSession,
) -> None:
    bot, owner_user = await seed_assistant(db_session)
    home = await pool_relay(db_session, "home")
    bot.relay_server_id = home.id
    admin = User(
        login_name=f"a{uuid.uuid4().hex[:6]}", display_name="管理员", role="platform_admin"
    )
    db_session.add(admin)
    await db_session.commit()
    plain = await add_node_relay(db_session, name="plain")
    plain_node = await db_session.get(RuntimeNode, plain.runtime_node_id)
    assert plain_node is not None
    plain_node.heartbeat_at = now()
    with pytest.raises(SwitchError) as caught:
        await validate_switch(
            db_session, bot=bot, target=plain, model=None, actor=admin, member_ids=set()
        )
    assert "助理池" in caught.value.message
    pool = await pool_relay(db_session, "pool")
    assert await validate_switch(
        db_session, bot=bot, target=pool, model=None, actor=admin, member_ids=set()
    )
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_runtime.py -q`
Expected: FAIL，`ImportError: cannot import name 'runtime' from 'coreman.core.assistants'`。

- [ ] **Step 3: 实现**

`coreman/core/assistants/runtime.py`：

```python
"""个人助理用哪台运行时：只派到能在沙箱里跑的节点；池里挑助理最少的节点；节点不可用就暂停。

- 能派的节点：在线、启用、没在排空，且这种 AI 的能力里声明了 `assistant_sandbox_v1`（守护进程
  启动自检通过，见 runtime_daemon/sandbox.py）。macOS、容器里没有用户命名空间、没装 bubblewrap、
  还没升级的节点都不会声明它。
- 能绑的实例：助理池里的（`relay_servers.assistant_pool`），或所有者本人的私有运行时。
- 开通（1F）调 `pick_pool_relay` 选池里助理最少的在线节点；之后每轮都不重新选（设计「不做」）。
  所在节点不可用时这一轮不跑，私聊告诉所有者，不回落到池。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.relay_policy import relay_available
from coreman.core.db.models import Bot, RelayServer, RuntimeNode
from coreman.core.runtime_nodes.transport import online

SANDBOX_CAPABILITY = "assistant_sandbox_v1"
# 管理台设置项：每个节点同时跑的个人助理轮次上限（节点领取时执行）。
ASSISTANT_CONCURRENCY_KEY = "assistant_node_concurrency"
PAUSE_MESSAGES = {
    "runtime_missing": "还没有分配可用的运行时，请联系管理员",
    "runtime_not_allowed": "绑定的运行时既不在助理池里、也不是你本人的私有运行时，请联系管理员",
    "runtime_disabled": "绑定的运行时已停用，请联系管理员",
    "runtime_offline": "绑定的运行时现在离线，恢复在线后再发一次消息即可",
    "runtime_draining": "绑定的运行时正在维护，稍后再发一次消息即可",
    "sandbox_unsupported": "绑定的运行时还不能隔离运行个人助理（需要升级或不支持），请联系管理员",
    "login_required": "绑定的运行时 AI 未登录，请联系管理员",
}


def node_sandboxed(node: RuntimeNode, provider: str | None = None) -> bool:
    """节点（某种 AI，或任一种）声明了能在沙箱里跑助理。"""
    capabilities: dict[str, Any] = node.capabilities or {}
    providers = [provider] if provider else list(capabilities)
    return any(
        isinstance(capabilities.get(p), dict) and capabilities[p].get(SANDBOX_CAPABILITY) is True
        for p in providers
    )


def unavailable_reason(relay: RelayServer, node: RuntimeNode | None) -> str | None:
    """这个实例现在能不能跑助理的一轮；能跑返回 None。"""
    if not relay_available(relay) or node is None or not node.is_active:
        return "runtime_disabled"
    if not online(node):
        return "runtime_offline"
    if node.draining:
        return "runtime_draining"
    if not node_sandboxed(node, relay.model_provider):
        return "sandbox_unsupported"
    cap = (node.capabilities or {}).get(relay.model_provider) or {}
    if cap.get("login") != "ready":
        return "login_required"
    return None


def eligible(relay: RelayServer, owner_id: uuid.UUID | None) -> bool:
    """助理能不能绑这个实例：助理池里的，或所有者本人的私有运行时。"""
    if relay.assistant_pool:
        return True
    return relay.visibility == "private" and owner_id is not None and relay.owner_id == owner_id


async def turn_blocker(session: AsyncSession, bot: Bot) -> str | None:
    """助理现在为什么不能跑一轮；能跑返回 None。"""
    relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    if relay is None:
        return "runtime_missing"
    if not eligible(relay, bot.owner_id):
        return "runtime_not_allowed"
    node = await session.get(RuntimeNode, relay.runtime_node_id) if relay.runtime_node_id else None
    return unavailable_reason(relay, node)


def pause_message(reason: str) -> str:
    detail = PAUSE_MESSAGES.get(reason, PAUSE_MESSAGES["runtime_missing"])
    return f"你的个人助理暂停了：{detail}。"


def assignment_error(
    relay: RelayServer, node: RuntimeNode | None, owner_id: uuid.UUID | None
) -> str | None:
    """改绑时的校验：只能绑助理池或本人的私有运行时，且那台现在就能跑助理。"""
    if not eligible(relay, owner_id):
        return "个人助理只能使用助理池或你本人的私有运行时"
    reason = unavailable_reason(relay, node)
    return None if reason is None else PAUSE_MESSAGES[reason].removeprefix("绑定的")


async def pick_pool_relay(session: AsyncSession, provider: str) -> RelayServer | None:
    """开通时用：池里这种 AI、现在能跑助理的实例中，所在节点上助理最少的一个（同数按编号）。"""
    relays = list(
        await session.scalars(
            select(RelayServer)
            .where(
                RelayServer.assistant_pool.is_(True),
                RelayServer.is_active.is_(True),
                RelayServer.model_provider == provider,
                RelayServer.runtime_node_id.is_not(None),
            )
            .order_by(RelayServer.id)
        )
    )
    if not relays:
        return None
    node_ids = {r.runtime_node_id for r in relays}
    found = await session.scalars(select(RuntimeNode).where(RuntimeNode.id.in_(node_ids)))
    nodes = {n.id: n for n in found}
    rows = await session.execute(
        select(RelayServer.runtime_node_id, func.count(Bot.id))
        .join(Bot, Bot.relay_server_id == RelayServer.id)
        .where(
            RelayServer.runtime_node_id.in_(node_ids),
            Bot.kind == "assistant",
            Bot.enabled.is_(True),
        )
        .group_by(RelayServer.runtime_node_id)
    )
    load: dict[uuid.UUID | None, int] = {node_id: count for node_id, count in rows.all()}
    ready = [
        r
        for r in relays
        if r.runtime_node_id is not None
        and unavailable_reason(r, nodes.get(r.runtime_node_id)) is None
    ]
    return min(ready, key=lambda r: (load.get(r.runtime_node_id, 0), str(r.id)), default=None)


async def hosts_standard_bots(session: AsyncSession, node_id: uuid.UUID) -> bool:
    """节点上有没有启用的普通员工。普通员工不在沙箱里、与助理是同一个系统用户，助理池节点不能有。"""
    count = await session.scalar(
        select(func.count(Bot.id))
        .join(RelayServer, Bot.relay_server_id == RelayServer.id)
        .where(
            RelayServer.runtime_node_id == node_id,
            Bot.kind == "standard",
            Bot.enabled.is_(True),
        )
    )
    return bool(count)
```

`coreman/core/bots/switch_relay.py`：import 区加

```python
from coreman.core.assistants import runtime as assistant_runtime
from coreman.core.assistants.records import is_assistant
```

模型 import 改成 `from coreman.core.db.models import Bot, BotSkill, RelayServer, RuntimeNode, User`；`validate_switch` 里 `validate_backend_ready` 那个 `if not same_relay:` 块之后、`if model is not None:` 之前加

```python
    if not same_relay and is_assistant(bot):
        # 个人助理只能改绑到助理池或所有者本人的私有运行时，且那台要能隔离运行助理。
        node = (
            await session.get(RuntimeNode, target.runtime_node_id)
            if target.runtime_node_id
            else None
        )
        problem = assistant_runtime.assignment_error(target, node, bot.owner_id)
        if problem is not None:
            raise SwitchError(422, problem)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_runtime.py tests/api/test_assistant_switch_jobs.py tests/integration/test_switch_jobs.py tests/integration/test_rate_limit_switch.py tests/api/test_bots_extra.py tests/api/test_workspace_migration.py -q && uv run mypy coreman`
Expected: 全部通过（新文件 5 条）。

普通机器人不受影响：`validate_switch` 的新分支只在 `is_assistant(bot)` 时进入；`test_pick_pool_relay_takes_the_emptiest_ready_node` 断言节点上的普通员工不计入、也不会被选中。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants/runtime.py coreman/core/bots/switch_relay.py tests/integration/test_assistant_runtime.py
git commit -m "个人助理：只派到能隔离运行的节点，池里挑最空的，改绑只能到池里或本人的私有运行时"
```

---

### Task 11: 节点协议——能力字段、转录接口、领取顺序与并发

**Files:**
- Modify: `coreman/api/routers/runtime_protocol.py`
- Modify: `coreman/core/runtime_nodes/transport.py`
- Modify: `coreman/core/settings_schema.py`、`tests/unit/test_settings_schema.py`
- Test: `tests/api/test_assistant_runtime_protocol.py`

**Interfaces:**
- Consumes: Task 9 的 `transcripts.download` / `upload` / `STATUSES` / `MAX_BYTES`；Task 10 的 `node_sandboxed`、`ASSISTANT_CONCURRENCY_KEY`；`sealed.tolerate_undecryptable`
- Produces:
  - 心跳 `Capability.assistant_sandbox_v1: StrictBool = False`
  - `GET /api/runtime/assistant-transcripts/{transfer}`：200 返回原始字节（`Cache-Control: no-store`），没有或读不出来 404
  - `PUT /api/runtime/assistant-transcripts/{transfer}`：头 `X-Transcript-Status`（`STATUSES` 之一），请求体是原始字节（只有 `ok` 带内容）；200 `{"result": "stored" | "kept" | "cleared"}`，编号失效 410，超过上限 413，状态或内容不对 422
  - `transport.SANDBOX_EXTENSION = "coreman.sandbox"`：请求扩展为 True 时调用落库为 `sandbox=True`
  - 设置项 `assistant_node_concurrency`（默认 2，范围 1–32）

- [ ] **Step 1: 写失败测试**

`tests/api/test_assistant_runtime_protocol.py`：

```python
"""节点协议上的个人助理：沙箱能力的上报、转录的交接、助理调用的领取顺序与并发上限。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest

from coreman.core.assistants import keys, sealed, transcripts
from coreman.core.chat import sessions
from coreman.core.crypto import Cipher
from coreman.core.db.models import RuntimeCall, RuntimeNode
from coreman.core.runtime_nodes.transport import envelope_aad, now
from tests.api.test_runtime_nodes import enrollment
from tests.integration.assistant_helpers import MASTER_KEY, seed_assistant

CAP = {"installed": True, "version": "test", "login": "ready", "models": ["claude-test"]}


@pytest.fixture(autouse=True)
def _master() -> Iterator[None]:
    sealed.use_master(Cipher(MASTER_KEY))
    keys.forget()
    sealed.forget()
    yield
    sealed.use_master(None)
    keys.forget()
    sealed.forget()


async def beat(client, headers, **claude):
    return await client.post(
        "/api/runtime/heartbeat",
        headers=headers,
        json={
            "claude": {**CAP, **claude},
            "codex": CAP,
            "version": "test",
            "service_status": "systemd-user",
        },
    )


async def test_heartbeat_records_the_sandbox_capability_and_old_nodes_lack_it(client, db_session):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    assert (await beat(client, headers)).status_code == 200
    node = await db_session.get(RuntimeNode, node_id, populate_existing=True)
    assert node is not None and node.capabilities["claude"]["assistant_sandbox_v1"] is False
    assert (await beat(client, headers, assistant_sandbox_v1=True)).status_code == 200
    node = await db_session.get(RuntimeNode, node_id, populate_existing=True)
    assert node is not None and node.capabilities["claude"]["assistant_sandbox_v1"] is True
    assert node.capabilities["codex"]["assistant_sandbox_v1"] is False
    # 只给布尔值：字符串之类的一律拒绝，不会被当成「有能力」。
    assert (await beat(client, headers, assistant_sandbox_v1="yes")).status_code == 422


async def handover(db_session, node_id: uuid.UUID):
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    info = await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="oc_1",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    _, plan = await transcripts.prepare(
        db_session,
        bot=bot,
        node_id=node_id,
        info=info,
        session_key="oc_1",
        backend="claude",
        speaker_user_id=None,
    )
    await db_session.commit()
    return bot, plan


async def test_transcript_round_trip_through_the_node_endpoints(client, db_session):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    bot, plan = await handover(db_session, node_id)
    url = f"/api/runtime/assistant-transcripts/{plan.transfer}"
    assert (await client.get(url, headers=headers)).status_code == 404
    put = {**headers, "X-Transcript-Status": "ok", "Content-Type": "application/octet-stream"}
    stored = await client.put(url, headers=put, content=b"tar-bytes")
    assert stored.status_code == 200 and stored.json()["data"]["result"] == "stored"
    assert (await client.put(url, headers=put, content=b"tar-bytes")).status_code == 410
    info = await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="oc_1",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    _, again = await transcripts.prepare(
        db_session,
        bot=bot,
        node_id=node_id,
        info=info,
        session_key="oc_1",
        backend="claude",
        speaker_user_id=None,
    )
    await db_session.commit()
    assert again.restore is True
    got = await client.get(f"/api/runtime/assistant-transcripts/{again.transfer}", headers=headers)
    assert got.status_code == 200 and got.content == b"tar-bytes"
    assert got.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("status", "content", "code"),
    [("bogus", b"", 422), ("ok", b"", 422), ("empty", b"x", 422), ("unchanged", b"", 200)],
)
async def test_upload_checks_status_and_body(client, db_session, status, content, code):
    _, body, headers = await enrollment(client, db_session)
    _, plan = await handover(db_session, uuid.UUID(body["node_id"]))
    response = await client.put(
        f"/api/runtime/assistant-transcripts/{plan.transfer}",
        headers={**headers, "X-Transcript-Status": status},
        content=content,
    )
    assert response.status_code == code, response.text


async def test_oversized_upload_is_refused(client, db_session, monkeypatch):
    monkeypatch.setattr(transcripts, "MAX_BYTES", 4)
    _, body, headers = await enrollment(client, db_session)
    _, plan = await handover(db_session, uuid.UUID(body["node_id"]))
    response = await client.put(
        f"/api/runtime/assistant-transcripts/{plan.transfer}",
        headers={**headers, "X-Transcript-Status": "ok"},
        content=b"0123456789",
    )
    assert response.status_code == 413


async def test_endpoints_need_the_node_credentials(client, db_session):
    url = f"/api/runtime/assistant-transcripts/{uuid.uuid4()}"
    assert (await client.get(url)).status_code == 401
    assert (await client.put(url, headers={"X-Transcript-Status": "empty"})).status_code == 401


async def queue(
    db_session, app, node_id: uuid.UUID, *, sandbox: bool, minutes_ago: int
) -> uuid.UUID:
    call_id = uuid.uuid4()
    payload = json.dumps({"method": "POST", "path": "/v1/chat/completions", "body": ""})
    db_session.add(
        RuntimeCall(
            id=call_id,
            node_id=node_id,
            provider="claude",
            request_enc=app.state.cipher.encrypt(payload, envelope_aad(call_id)),
            deadline=now() + timedelta(hours=1),
            consumer_at=now(),
            created_at=now() - timedelta(minutes=minutes_ago),
            sandbox=sandbox,
        )
    )
    await db_session.commit()
    return call_id


async def poll(client, headers, slots: int) -> list[str]:
    result = await client.post("/api/runtime/poll", headers=headers, json={"slots": slots})
    assert result.status_code == 200, result.text
    return [command["id"] for command in result.json()["data"]["commands"]]


async def test_standard_calls_are_claimed_first_and_assistant_calls_are_capped(
    client, db_session, app
):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    await beat(client, headers, assistant_sandbox_v1=True)
    older = [
        await queue(db_session, app, node_id, sandbox=True, minutes_ago=9 - i) for i in range(3)
    ]
    standard = await queue(db_session, app, node_id, sandbox=False, minutes_ago=1)
    claimed = await poll(client, headers, 4)
    # 普通调用不排在早到的助理调用后面；助理调用按默认上限 2 领取。
    assert claimed == [str(standard), str(older[0]), str(older[1])]
    assert await poll(client, headers, 4) == []
    row = await db_session.get(RuntimeCall, older[0], populate_existing=True)
    assert row is not None
    row.status = "done"
    await db_session.commit()
    assert await poll(client, headers, 4) == [str(older[2])]


async def test_assistant_calls_wait_on_nodes_without_the_capability(client, db_session, app):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    await beat(client, headers)
    waiting = await queue(db_session, app, node_id, sandbox=True, minutes_ago=5)
    standard = await queue(db_session, app, node_id, sandbox=False, minutes_ago=1)
    assert await poll(client, headers, 4) == [str(standard)]
    row = await db_session.get(RuntimeCall, waiting, populate_existing=True)
    assert row is not None and row.status == "queued"


async def test_concurrency_follows_the_setting(client, db_session, app):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    await beat(client, headers, assistant_sandbox_v1=True)
    patch = await client.put("/api/admin/settings", json={"assistant_node_concurrency": 1})
    assert patch.status_code == 200, patch.text
    first = await queue(db_session, app, node_id, sandbox=True, minutes_ago=3)
    await queue(db_session, app, node_id, sandbox=True, minutes_ago=2)
    assert await poll(client, headers, 4) == [str(first)]
```

`tests/unit/test_settings_schema.py`：`set(SETTING_DEFAULTS) == {…}` 里 `"busy_confirm_minutes",` 之后加 `"assistant_node_concurrency",`。

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_assistant_runtime_protocol.py tests/unit/test_settings_schema.py -q`
Expected: FAIL：心跳用例里能力没有记下（`KeyError: 'assistant_sandbox_v1'`），转录接口 404/405，领取用例拿到的顺序不对，设置项全集对不上。

- [ ] **Step 3: 实现**

`coreman/core/settings_schema.py`：`SETTING_DEFAULTS` 里 `"busy_confirm_minutes": 10,` 之后加

```python
    # 每个运行时节点同时跑的个人助理轮次上限；超出的排队（节点领取时执行）。
    "assistant_node_concurrency": 2,
```

`SettingsPatch` 里 `busy_confirm_minutes` 一行之后加

```python
    assistant_node_concurrency: int | None = Field(default=None, ge=1, le=32)
```

`coreman/core/runtime_nodes/transport.py`：`TOTAL_TIMEOUT_EXTENSION = …` 之后加

```python
# 个人助理在沙箱里跑的一轮：节点领取时按助理并发上限另算（见 runtime_protocol._collect）。
SANDBOX_EXTENSION = "coreman.sandbox"
```

`ReverseTransport.handle_async_request` 里建 `RuntimeCall(…)` 时，`consumer_at=started,` 之后加一行 `sandbox=extensions.get(SANDBOX_EXTENSION) is True,`。

`coreman/api/routers/runtime_protocol.py`：

1. import 区 `from coreman.api.errors import ApiError, not_found` 之后加

```python
from coreman.core.assistants import sealed, transcripts
from coreman.core.assistants.runtime import ASSISTANT_CONCURRENCY_KEY, node_sandboxed
```

2. `Capability` 里 `memory_layout_v1` 之后加

```python
    # 守护进程自检通过、能在沙箱里跑个人助理的轮次（见 runtime_daemon/sandbox.py）。旧节点没有，
    # 也就收不到助理的轮次：旧守护进程不认识 sandbox 字段，会在沙箱外照常跑。
    assistant_sandbox_v1: StrictBool = False
```

3. `_collect` 里领取的那一段（`if not node.draining and body.slots:` 到 `for row in rows:` 之前）换成

```python
    if not node.draining and body.slots:
        moment = now()
        claimable = (
            RuntimeCall.node_id == node_id,
            RuntimeCall.status == "queued",
            RuntimeCall.consumer_at >= moment - timedelta(seconds=LEASE_SECONDS),
            RuntimeCall.deadline > moment,
        )
        rows = list(
            await session.scalars(
                select(RuntimeCall)
                .where(*claimable, RuntimeCall.sandbox.is_(False))
                .order_by(RuntimeCall.created_at)
                .limit(min(body.slots, 4))
                .with_for_update(skip_locked=True)
            )
        )
        room = min(body.slots, 4) - len(rows)
        if room > 0 and node_sandboxed(node):
            rows += await _claim_sandboxed(session, request, node_id, claimable, room)
```

（`for row in rows:` 及之后不变。）

4. `@router.post("/calls/{call_id}/frames")` 之前加

```python
async def _claim_sandboxed(
    session: AsyncSession,
    request: Request,
    node_id: uuid.UUID,
    claimable: tuple[Any, ...],
    room: int,
) -> list[RuntimeCall]:
    """个人助理的调用：普通调用领完之后才领，且本节点同时在跑的不超过助理并发上限。

    超出的留在队列里，由调用方按排队上限等待；普通调用从不排在它们后面。
    """
    limit = int(await request.app.state.settings_store.get(ASSISTANT_CONCURRENCY_KEY, default=2))
    running = await session.scalar(
        select(func.count())
        .select_from(RuntimeCall)
        .where(
            RuntimeCall.node_id == node_id,
            RuntimeCall.sandbox.is_(True),
            RuntimeCall.status == "running",
        )
    )
    room = min(room, limit - (running or 0))
    if room <= 0:
        return []
    return list(
        await session.scalars(
            select(RuntimeCall)
            .where(*claimable, RuntimeCall.sandbox.is_(True))
            .order_by(RuntimeCall.created_at)
            .limit(room)
            .with_for_update(skip_locked=True)
        )
    )


@router.get("/assistant-transcripts/{transfer}")
async def transcript_download(
    transfer: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    """节点续聊前取转录：只认派这一轮时给它的交接编号。没有、读不出来都回 404，节点从新会话开始。"""
    node = await node_auth(request, session)
    # 这个请求只读一个助理的一份转录：解不开按没有处理，不报 500。
    sealed.tolerate_undecryptable(session)
    data = await transcripts.download(session, node_id=node.id, transfer=transfer)
    await session.commit()
    if data is None:
        raise ApiError(404, 404, "转录不可用")
    return Response(data, media_type="application/octet-stream", headers=NO_STORE)


@router.put("/assistant-transcripts/{transfer}")
async def transcript_upload(
    transfer: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """节点交回本轮转录（请求体是 tar.gz 原始字节）。编号已失效回 410，节点不必再传。"""
    node = await node_auth(request, session)
    status = request.headers.get("X-Transcript-Status", "")
    if status not in transcripts.STATUSES:
        raise ApiError(422, 422, "转录状态无效")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > transcripts.MAX_BYTES:
        raise ApiError(413, 413, "转录过大")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > transcripts.MAX_BYTES:
            raise ApiError(413, 413, "转录过大")
    if (status == "ok") != bool(data):
        raise ApiError(422, 422, "转录内容与状态不符")
    result = await transcripts.upload(
        session, node_id=node.id, transfer=transfer, status=status, data=bytes(data)
    )
    if result == "stale":
        await session.rollback()
        raise ApiError(410, 410, "交接编号已失效")
    await session.commit()
    return {"code": 0, "data": {"result": result}}
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/api/test_assistant_runtime_protocol.py tests/unit/test_settings_schema.py tests/api/test_runtime_protocol.py tests/api/test_runtime_nodes.py tests/api/test_runtime_queue.py tests/api/test_runtime.py tests/api/test_runtime_daemon_e2e.py -q && uv run mypy coreman`
Expected: 全部通过（新文件 11 条）。

普通机器人不受影响：`test_standard_calls_are_claimed_first_and_assistant_calls_are_capped`（比助理调用晚到的普通调用照样先被领走）、`test_assistant_calls_wait_on_nodes_without_the_capability`（没声明能力的节点一个助理调用都不领，普通调用照领）；`tests/api/test_runtime_protocol.py`、`test_runtime_queue.py`、`test_runtime_daemon_e2e.py` 的领取、排队与端到端用例照常通过。领取普通调用的语句只多了 `sandbox IS false` 一个条件，第二条查询只在节点声明了能力时才执行。

- [ ] **Step 5: 提交**

```bash
git add coreman/api/routers/runtime_protocol.py coreman/core/runtime_nodes/transport.py coreman/core/settings_schema.py tests/unit/test_settings_schema.py tests/api/test_assistant_runtime_protocol.py
git commit -m "个人助理：节点上报沙箱能力、取交转录的接口、助理调用排在普通调用之后并限并发"
```

---

### Task 12: 助理池开关

**Files:**
- Modify: `coreman/api/routers/relay_servers.py`
- Test: `tests/api/test_assistant_pool_api.py`

**Interfaces:**
- Consumes: Task 10 的 `node_sandboxed`、`hosts_standard_bots`；Task 11 的心跳能力字段
- Produces: `PUT /api/admin/relay-servers/{relay_id}/assistant-pool`（`ai_committee` / `platform_admin`），请求体 `{"enabled": bool}`，审计 `relay.assistant_pool`；实例视图多 `assistant_pool`、`assistant_sandbox` 两个字段

- [ ] **Step 1: 写失败测试**

`tests/api/test_assistant_pool_api.py`：

```python
"""助理池开关：只有节点声明能隔离运行助理的共享运行时才能加入。"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from coreman.core.db.models import RelayServer
from tests.api.test_assistant_runtime_protocol import beat
from tests.api.test_runtime_nodes import enrollment
from tests.integration.assistant_helpers import seed_standard


async def test_pool_flag_needs_a_sandboxed_shared_runtime(client, db_session):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    relay = await db_session.scalar(
        select(RelayServer).where(
            RelayServer.runtime_node_id == node_id, RelayServer.model_provider == "claude"
        )
    )
    assert relay is not None
    url = f"/api/admin/relay-servers/{relay.id}/assistant-pool"
    refused = await client.put(url, json={"enabled": True})
    assert refused.status_code == 422
    await beat(client, headers, assistant_sandbox_v1=True)
    added = await client.put(url, json={"enabled": True})
    assert added.status_code == 200, added.text
    assert added.json()["data"]["assistant_pool"] is True
    assert added.json()["data"]["assistant_sandbox"] is True
    removed = await client.put(url, json={"enabled": False})
    assert removed.json()["data"]["assistant_pool"] is False


async def test_pool_nodes_must_not_host_standard_bots(client, db_session):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    await beat(client, headers, assistant_sandbox_v1=True)
    relay = await db_session.scalar(
        select(RelayServer).where(
            RelayServer.runtime_node_id == node_id, RelayServer.model_provider == "claude"
        )
    )
    assert relay is not None
    bot = await seed_standard(db_session)
    bot.relay_server_id = relay.id
    await db_session.commit()
    response = await client.put(
        f"/api/admin/relay-servers/{relay.id}/assistant-pool", json={"enabled": True}
    )
    assert response.status_code == 422 and "普通员工" in response.json()["message"]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_assistant_pool_api.py -q`
Expected: FAIL，接口返回 405（路由不存在）。

- [ ] **Step 3: 实现**

`coreman/api/routers/relay_servers.py`：import 区加 `from pydantic import BaseModel`（放在 `from fastapi import …` 之后）与 `from coreman.core.assistants import runtime as assistant_runtime`（放在 `from coreman.api.security import verify_csrf` 之后）；`relay_out` 返回的字典里 `"is_active": relay.is_active,` 之后加

```python
        # 个人助理：是否在助理池里，节点是否声明能隔离运行助理。
        "assistant_pool": relay.assistant_pool,
        "assistant_sandbox": bool(
            node and assistant_runtime.node_sandboxed(node, relay.model_provider)
        ),
```

`@router.get("/{relay_id}/models")` 之前加

```python
class AssistantPoolIn(BaseModel):
    enabled: bool


@router.put("/{relay_id}/assistant-pool")
async def set_assistant_pool(
    relay_id: uuid.UUID,
    body: AssistantPoolIn,
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """把实例加入或移出个人助理池。加入时要求节点已声明能隔离运行助理、节点上没有普通员工，
    私有运行时不能加入。

    移出不影响已经绑在上面的助理（它们照常跑，直到所有者或管理员改绑）；只是开通时不再选它。
    """
    relay = await load_relay(session, relay_id)
    if body.enabled:
        if relay.visibility == "private":
            raise ApiError(422, 422, "私有运行时不能加入助理池")
        node = (
            await session.get(RuntimeNode, relay.runtime_node_id) if relay.runtime_node_id else None
        )
        if node is None or not assistant_runtime.node_sandboxed(node, relay.model_provider):
            raise ApiError(422, 422, "该运行时还不能隔离运行个人助理，请先升级并确认节点自检通过")
        if await assistant_runtime.hosts_standard_bots(session, node.id):
            # 普通员工在沙箱外、以同一个系统用户运行，读得到助理这一轮的临时目录与凭证副本。
            raise ApiError(422, 422, "该节点上还有普通员工：助理池节点只承载个人助理")
    before = relay.assistant_pool
    relay.assistant_pool = body.enabled
    await record_audit(
        session,
        action="relay.assistant_pool",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="relay_server",
        target_id=str(relay.id),
        diff={"assistant_pool": [before, body.enabled]},
        ip=client_ip(request),
    )
    await session.commit()
    return {"code": 0, "data": await _one_out(session, relay)}
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/api/test_assistant_pool_api.py tests/api/test_relay_servers.py -q && uv run mypy coreman`
Expected: 全部通过（新文件 2 条）。

普通机器人不受影响：实例视图只多两个字段，原有字段不变（`tests/api/test_relay_servers.py` 照常通过）；新接口只改 `assistant_pool`。

- [ ] **Step 5: 提交**

```bash
git add coreman/api/routers/relay_servers.py tests/api/test_assistant_pool_api.py
git commit -m "个人助理：助理池开关，只收能隔离运行且没有普通员工的共享节点"
```

---

### Task 13: 请求体与调用标记

**Files:**
- Modify: `coreman/core/relay/client.py`
- Test: `tests/unit/test_assistant_chat_request.py`

**Interfaces:**
- Consumes: Task 11 的 `SANDBOX_EXTENSION`
- Produces: `ChatRequest.sandbox: bool = False`、`ChatRequest.transcript: dict[str, Any] | None = None`；`to_body()` 只在 `sandbox` 为真时加 `"sandbox": True` 与 `"transcript": {…}`；`chat_stream` 对这样的请求多带扩展 `coreman.sandbox`

- [ ] **Step 1: 写失败测试**

`tests/unit/test_assistant_chat_request.py`：

```python
"""请求体与调用标记：普通机器人的请求体一字不变；助理的一轮带 sandbox 与 transcript。"""

from __future__ import annotations

import json

import httpx

from coreman.core.relay.client import ChatRequest, RelayClient
from coreman.core.runtime_nodes.transport import SANDBOX_EXTENSION, TOTAL_TIMEOUT_EXTENSION

BASE = {
    "model": "claude-opus-5-5",
    "system_prompt": "sys",
    "user_content": "hi",
    "working_dir": "/data/demo-bot",
    "session_id": "s1",
    "backend": "claude",
    "env_vars": {"COREMAN_BOT_KEY": "demo-bot"},
}


def test_standard_body_is_exactly_what_it_was() -> None:
    assert ChatRequest(**BASE).to_body() == {
        "model": "claude-opus-5-5",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "working_dir": "/data/demo-bot",
        "session_id": "s1",
        "max_turns": 200,
        "env_vars": {"COREMAN_BOT_KEY": "demo-bot"},
    }


def test_sandboxed_body_carries_the_handover() -> None:
    handover = {"transfer": "a" * 32, "restore": True}
    body = ChatRequest(**BASE, sandbox=True, transcript=handover).to_body()
    assert body["sandbox"] is True and body["transcript"] == handover
    assert body["transcript"] is not handover


async def test_stream_marks_only_sandboxed_calls() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.extensions))
        chunk = {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}
        sse = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    client = RelayClient("http://relay.test", transport=httpx.MockTransport(handler))
    try:
        for request in (
            ChatRequest(**BASE),
            ChatRequest(**BASE, sandbox=True, transcript={"transfer": "b" * 32, "restore": False}),
        ):
            [event async for event in client.chat_stream(request, total_timeout=30)]
    finally:
        await client.aclose()
    assert TOTAL_TIMEOUT_EXTENSION in seen[0] and SANDBOX_EXTENSION not in seen[0]
    assert seen[1][SANDBOX_EXTENSION] is True
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_chat_request.py -q`
Expected: FAIL，`TypeError: ChatRequest.__init__() got an unexpected keyword argument 'sandbox'`（第一条照常通过）。

- [ ] **Step 3: 实现**

`coreman/core/relay/client.py`：

1. `from coreman.core.runtime_nodes.transport import (…)` 里加 `SANDBOX_EXTENSION,`（排在 `NODE_ERRORS,` 之后）。
2. `ChatRequest` 的 `resume_fallback: str = ""` 之后加

```python
    # 个人助理的一轮：节点在沙箱里跑，`transcript` 是转录的交接（core/assistants/transcripts.py）。
    sandbox: bool = False
    transcript: dict[str, Any] | None = None
```

3. `to_body` 文档串里「`worktree` 为真才带；`session_id` 原样透传（cron 与体检传空字符串）。」改成

```text
        `worktree`、`sandbox` 为真才带（后者连同 `transcript`）；`session_id` 原样透传（cron 与
        体检传空字符串）。
```

`if self.worktree: body["worktree"] = True` 之后、`return body` 之前加

```python
        if self.sandbox:
            body["sandbox"] = True
            body["transcript"] = dict(self.transcript or {})
```

4. `chat_stream` 里 `async with self._http.stream(…)` 之前加

```python
            extensions: dict[str, Any] = {TOTAL_TIMEOUT_EXTENSION: total_timeout}
            if request.sandbox:
                extensions[SANDBOX_EXTENSION] = True
```

并把 `extensions={TOTAL_TIMEOUT_EXTENSION: total_timeout},` 换成 `extensions=extensions,`。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_chat_request.py tests/unit/test_relay_client.py tests/integration/test_chat_handler.py -q && uv run mypy coreman`
Expected: 全部通过（新文件 3 条）。

普通机器人不受影响：`test_standard_body_is_exactly_what_it_was`（普通请求体逐键相等）、`test_stream_marks_only_sandboxed_calls`（普通请求不带新扩展）。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/relay/client.py tests/unit/test_assistant_chat_request.py
git commit -m "个人助理：请求体带 sandbox 与 transcript，调用标给节点队列"
```

---

### Task 14: worker 派出助理的一轮

**Files:**
- Create: `coreman/runtime/worker/chat/assistant_runtime.py`
- Modify: `coreman/runtime/worker/chat/handler.py`（`_prepare`）、`coreman/runtime/worker/chat/opening.py`（`_open`）
- Modify: `tests/integration/assistant_helpers.py`（`into_assistant` 之后让助理所在节点可用）
- Test: `tests/integration/test_assistant_turn_dispatch.py`

**Interfaces:**
- Consumes: Task 9 的 `transcripts.prepare`；Task 10 的 `turn_blocker`、`pause_message`；Task 13 的 `ChatRequest.sandbox` / `transcript`；`replies.reply_once`、`tasks.finish`
- Produces: `assistant_runtime.paused(ctx, intake) -> bool`（不能跑时回一句、把任务结成 `failed` / `assistant_paused`）；`assistant_helpers.sandbox_ready(session, bot)`

这一步之后，所有经 `ChatTaskHandler` 的助理轮次（私聊、1D 的审批结果回合）都要求所在节点在线、声明了沙箱能力、实例在池里或属于所有者本人。现有的助理用例都经 `into_assistant` 造助理，所以在那里补上 `sandbox_ready`，它们照常通过。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_turn_dispatch.py`：

```python
"""worker 派出个人助理的一轮：带上沙箱与转录交接；所在运行时不行就暂停并告诉所有者。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import (
    AssistantTranscript,
    Bot,
    RelayServer,
    RuntimeNode,
    Task,
    TaskStream,
)
from coreman.core.runtime_nodes.transport import now
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.assistant_helpers import into_assistant
from tests.integration.credential_helpers import owner
from tests.integration.worker_helpers import build_ctx

pytestmark = pytest.mark.usefixtures("assistant_master")


async def assistant_chat(session: AsyncSession) -> tuple[Bot, Task]:
    bot, user, task, _ = await owner(session)
    await into_assistant(session, bot, user)
    await keys.ensure_key(session, sealed.master(), user.id)
    await session.commit()
    return bot, task


async def run(engine: AsyncEngine, session: AsyncSession, task: Task) -> tuple[Task, FakeRelay]:
    relay = FakeRelay("normal")
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: relay.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    done = await session.get(Task, task.id, populate_existing=True)
    assert done is not None
    return done, relay


async def test_assistant_turn_is_sent_sandboxed_with_a_handover(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, task = await assistant_chat(db_session)
    done, relay = await run(db_engine, db_session, task)
    assert done.status == "succeeded"
    body = relay.requests[0]
    assert body["sandbox"] is True
    row = await db_session.scalar(
        select(AssistantTranscript).where(AssistantTranscript.bot_id == bot.id)
    )
    assert row is not None and row.pending_transfer is not None
    assert body["transcript"] == {"transfer": row.pending_transfer.hex, "restore": False}
    assert str(row.session_id) == body["session_id"]


@pytest.mark.parametrize("breakage", ["offline", "no_sandbox", "not_in_pool"])
async def test_assistant_pauses_instead_of_running_elsewhere(
    db_engine: AsyncEngine, db_session: AsyncSession, breakage: str
) -> None:
    bot, task = await assistant_chat(db_session)
    relay_row = await db_session.get(RelayServer, bot.relay_server_id)
    assert relay_row is not None
    node = await db_session.get(RuntimeNode, relay_row.runtime_node_id)
    assert node is not None
    if breakage == "offline":
        node.heartbeat_at = now() - timedelta(minutes=5)
    elif breakage == "no_sandbox":
        node.capabilities = {relay_row.model_provider: {"installed": True, "login": "ready"}}
    else:
        relay_row.assistant_pool = False
    await db_session.commit()
    done, relay = await run(db_engine, db_session, task)
    assert done.status == "failed" and done.error_code == "assistant_paused"
    assert relay.requests == []
    stream = await db_session.get(TaskStream, task.id, populate_existing=True)
    assert stream is not None and stream.is_complete
    assert (stream.final_text or "").startswith("你的个人助理暂停了：")


async def test_standard_turn_body_has_no_sandbox_fields(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    _, _, task, _ = await owner(db_session)
    done, relay = await run(db_engine, db_session, task)
    assert done.status == "succeeded"
    assert "sandbox" not in relay.requests[0] and "transcript" not in relay.requests[0]
    assert await db_session.scalar(select(AssistantTranscript)) is None
```

`tests/integration/assistant_helpers.py`：模型 import 改成 `from coreman.core.db.models import Bot, PlatformApp, RelayServer, RuntimeNode, User`，再加 `from coreman.core.runtime_nodes.transport import now`；`into_assistant` 里 `for name, value in ASSISTANT_FIXED.items(): …` 之后、`await session.commit()` 之前加 `await sandbox_ready(session, bot)`；文件末尾加

```python
async def sandbox_ready(session: AsyncSession, bot: Bot) -> None:
    """助理绑的实例放进助理池，所在节点在线并声明能隔离运行助理（1E 起助理只在这样的节点上跑）。"""
    relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    if relay is None or relay.runtime_node_id is None:
        return
    relay.assistant_pool = True
    node = await session.get(RuntimeNode, relay.runtime_node_id)
    assert node is not None
    capabilities = dict(node.capabilities or {})
    capabilities[relay.model_provider] = {
        **capabilities.get(relay.model_provider, {}),
        "installed": True,
        "login": "ready",
        "assistant_sandbox_v1": True,
    }
    node.capabilities = capabilities
    node.heartbeat_at = now()
    await session.flush()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_turn_dispatch.py -q`
Expected: FAIL：助理的请求体里没有 `sandbox`（`KeyError: 'sandbox'`），三个暂停用例里任务是 `succeeded`、假 relay 收到了请求。

- [ ] **Step 3: 实现**

`coreman/runtime/worker/chat/assistant_runtime.py`：

```python
"""个人助理开轮之前的一道关：所在运行时现在跑不了助理，就不派，私聊告诉所有者。"""

from __future__ import annotations

from coreman.core.assistants import runtime
from coreman.core.bus import tasks
from coreman.core.db.models import Bot
from coreman.runtime.worker.chat.models import Intake
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import reply_once


async def paused(ctx: TaskContext, intake: Intake) -> bool:
    """所在运行时不可用（离线、没有沙箱能力、不在池里也不是本人的）：回一句、结掉任务，返回真。

    不回落到别的节点：私有运行时离线时把助理换到池里，等于把个人内容挪到所有者没选的机器上。
    """
    async with ctx.session_factory() as session:
        bot = await session.get(Bot, intake.bot.id, populate_existing=True)
        reason = "runtime_missing" if bot is None else await runtime.turn_blocker(session, bot)
        if reason is None:
            return False
        ctx.log.warning("assistant_paused", reason=reason)
        await reply_once(
            session,
            ctx,
            reply_context=intake.inbound.reply_context,
            text=runtime.pause_message(reason),
        )
        await tasks.finish(
            session, ctx.task.id, status="failed", error_code="assistant_paused", only_active=True
        )
        await session.commit()
    return True
```

`coreman/runtime/worker/chat/handler.py`：import 区 `from coreman.core.bus import tasks` 之前加 `from coreman.core.assistants.records import is_assistant`；`_prepare` 里 `intake, relay, parts = resolved` 之后加

```python
        if is_assistant(intake.bot):
            from coreman.runtime.worker.chat import assistant_runtime

            # 个人助理只在能隔离运行的节点上跑：所在运行时不行就不派，私聊告诉所有者。
            if await assistant_runtime.paused(ctx, intake):
                return None
```

`coreman/runtime/worker/chat/opening.py`：import 区 `from coreman.core.auth.system_access import …` 之前加 `from coreman.core.assistants.records import is_assistant`；`_open` 里 `info = await self._session_info(session, ctx, intake, backend)` 之后加

```python
        transcript = None
        if is_assistant(bot):
            from coreman.core.assistants import transcripts

            if relay.runtime_node_id is None:
                raise ValueError("bot_or_relay_changed_before_dispatch")
            # 个人助理的一轮在节点的沙箱里跑：定下续不续上一轮的转录（续不上就换新会话）。
            info, transcript = await transcripts.prepare(
                session,
                bot=bot,
                node_id=relay.runtime_node_id,
                info=info,
                session_key=intake.session_key,
                backend=backend,
                speaker_user_id=intake.speaker.user_id,
            )
```

同一函数里构造 `ChatRequest(…)` 的参数末尾（`resume_fallback=resume_fallback,` 之后）加

```python
            sandbox=transcript is not None,
            transcript=transcript.body() if transcript is not None else None,
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_turn_dispatch.py tests/integration/test_assistant_approval_result.py tests/integration/test_chat_handler.py tests/api/test_assistant_approval_gate.py tests/api/test_assistant_approval_files.py -q && uv run pytest tests/integration tests/api -q -k "assistant or personal or sealed"`
Expected: 全部通过（新文件 5 条；第二条命令是现有的助理相关用例，`into_assistant` 补上 `sandbox_ready` 之后照常通过）。

普通机器人不受影响：`test_standard_turn_body_has_no_sandbox_fields`（普通机器人的请求体没有 `sandbox` / `transcript`，库里没有转录行）；新分支都在 `is_assistant(...)` 之下；`tests/integration/test_chat_handler.py` 照常通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/runtime/worker/chat/assistant_runtime.py coreman/runtime/worker/chat/handler.py coreman/runtime/worker/chat/opening.py tests/integration/assistant_helpers.py tests/integration/test_assistant_turn_dispatch.py
git commit -m "个人助理：开轮前确认所在节点能隔离运行，不行就暂停并告诉所有者；开流时定下转录交接"
```

---

### Task 15: 工作目录规则进提示词，scheduler 清理过期转录

**Files:**
- Create: `coreman/core/assistants/workspace_prompt.py`
- Modify: `coreman/runtime/worker/chat/personal.py`（`configure`）、`coreman/runtime/scheduler/service.py`
- Test: `tests/unit/test_assistant_workspace_prompt.py`、`tests/integration/test_assistant_transcript_purge.py`

**Interfaces:**
- Consumes: 1D 的 `approval_prompt.RULES` 所在位置；Task 9 的 `transcripts.purge`
- Produces: `workspace_prompt.WORKSPACE_RULES: str`；scheduler 每轮 `assistant_transcripts_purged` 计数（删掉会话过期一天以上的转录）

- [ ] **Step 1: 写失败测试**

`tests/unit/test_assistant_workspace_prompt.py`：

```python
"""工作目录的安全规则只进个人助理的提示词。"""

from __future__ import annotations

from coreman.core.assistants import workspace_prompt
from coreman.runtime.worker.chat import personal


def test_workspace_rules_are_for_assistants_only() -> None:
    text = workspace_prompt.WORKSPACE_RULES
    assert "不要往里写人名、聊天内容、审批内容或任何个人信息" in text
    assert "知识库" in text and "$TMPDIR" in text
    rules, _ = personal.feishu_guidance(None, "https://coreman.example.com", scheduled=False)
    assert "工作目录只放规则" not in rules
```

`tests/integration/test_assistant_transcript_purge.py`：

```python
"""scheduler 每轮清理过期的转录；清理出错不挡后面的清理。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from coreman.core.assistants import transcripts
from coreman.runtime.scheduler import reaper
from coreman.runtime.scheduler.service import SchedulerService


async def run_until(event: asyncio.Event) -> None:
    svc = SchedulerService(port=0, tick_seconds=0.2, cleanup_seconds=0.5, leader_retry_seconds=0.2)
    task = asyncio.create_task(svc.run())
    try:
        await asyncio.wait_for(event.wait(), 10)
    finally:
        svc.request_stop("test")
        await asyncio.wait_for(task, 10)


async def test_each_tick_purges_transcripts_older_than_the_session_ttl(
    db_engine: AsyncEngine,
    runtime_settings,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    seen: list[datetime] = []
    called = asyncio.Event()

    async def purge(session: object, before: datetime) -> int:
        seen.append(before)
        called.set()
        return 0

    monkeypatch.setattr(transcripts, "purge", purge)
    await run_until(called)
    # 默认会话 72 小时过期，再多留一天。
    age = datetime.now(seen[0].tzinfo) - seen[0]
    assert timedelta(hours=95) < age < timedelta(hours=97)


async def test_failing_purge_does_not_skip_the_cleanup(
    db_engine: AsyncEngine,
    runtime_settings,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    async def broken(*args: object, **kwargs: object) -> int:
        raise RuntimeError("boom")

    ran = asyncio.Event()

    async def cleanup(*args: object, **kwargs: object) -> dict[str, int]:
        ran.set()
        return {}

    monkeypatch.setattr(transcripts, "purge", broken)
    monkeypatch.setattr(reaper, "run_cleanup", cleanup)
    await run_until(ran)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_assistant_workspace_prompt.py tests/integration/test_assistant_transcript_purge.py -q`
Expected: FAIL：`ImportError: cannot import name 'workspace_prompt'`；清理用例等不到 `purge` 被调用（`TimeoutError`）。

- [ ] **Step 3: 实现**

`coreman/core/assistants/workspace_prompt.py`：

```python
"""个人助理工作目录的安全规则：进 system prompt 的稳定段，每轮逐字不变。

工作目录在节点上长期保留（与普通员工一样按记录分配、可在换运行时时复制），所以只能放规则、经验
总结和技能；人名、聊天、审批内容一律进加密的知识库。
"""

from __future__ import annotations

WORKSPACE_RULES = (
    "\n\n## 工作目录\n"
    "你的工作目录只放规则、经验总结和技能，会在节点上长期保留。不要往里写人名、聊天内容、"
    "审批内容或任何个人信息，也不要把它们写进文件名；这些一律用知识库工具保存。"
    "下载的文档和附件放在临时目录（$TMPDIR），这一轮结束就会被删除。"
)
```

`coreman/runtime/worker/chat/personal.py`：import 区 `from coreman.core.assistants.approvals import prompt as approval_prompt` 之前加 `from coreman.core.assistants import workspace_prompt`；`configure` 里

```python
    if is_assistant(scope.bot):
        # 个人助理：写类工具改为提交审批，规则写进稳定段。
        rules += approval_prompt.RULES
```

换成

```python
    if is_assistant(scope.bot):
        # 个人助理：写类工具改为提交审批，工作目录不放个人信息，规则写进稳定段。
        rules += approval_prompt.RULES + workspace_prompt.WORKSPACE_RULES
```

`coreman/runtime/scheduler/service.py`：审批闸门那个 `try … except Exception as exc:  # noqa: BLE001 审批闸门一直出错…` 块之后、`if time.monotonic() >= next_cleanup:` 之前加

```python
                from coreman.core.assistants import transcripts as assistant_transcripts

                try:
                    async with self._factory() as session:
                        ttl = int(await self._store.get("session_ttl_hours", default=72))
                        counts["assistant_transcripts_purged"] = await assistant_transcripts.purge(
                            session, now - timedelta(hours=ttl + 24)
                        )
                        await session.commit()
                except Exception as exc:  # noqa: BLE001 转录清理出错不能挡住后面的清理
                    self._log.warning("assistant_transcript_purge_failed", error=type(exc).__name__)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_assistant_workspace_prompt.py tests/integration/test_assistant_transcript_purge.py tests/unit/test_assistant_approval_prompt.py tests/integration/test_scheduler_reaper.py -q && uv run mypy coreman`
Expected: 全部通过（新文件 1 + 2 条）。

普通机器人不受影响：`test_workspace_rules_are_for_assistants_only`（普通机器人的个人工具说明里没有这段）；`test_failing_purge_does_not_skip_the_cleanup`（清理出错不挡后面的清理）；`tests/integration/test_scheduler_reaper.py` 照常通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants/workspace_prompt.py coreman/runtime/worker/chat/personal.py coreman/runtime/scheduler/service.py tests/unit/test_assistant_workspace_prompt.py tests/integration/test_assistant_transcript_purge.py
git commit -m "个人助理：工作目录不放个人信息的规则进提示词，scheduler 清理过期转录"
```

---


### Task 16: 部署文档、升级与回滚、设计同步与整体验证

**Files:**
- Modify: `runtime_daemon/README.md`（新增一节）
- Modify: `docs/feishu.md`（「个人助理应用」加一条）
- Modify: `docs/superpowers/specs/2026-11-11-personal-assistant.md`（第三节按落地结果同步）

**Interfaces:**
- Consumes: 前面全部任务
- Produces: 无代码

- [ ] **Step 1: 守护进程文档**

`runtime_daemon/README.md` 在「## 升级」之前加一节：

````markdown
## 个人助理的沙箱

个人助理（见 `docs/feishu.md`「个人助理应用」）的每一轮都在 bubblewrap 沙箱里跑：守护进程为这一轮另起一个驱动，只挂进它自己的工作目录（沙箱里是 `/work`）、本轮新建的家目录、临时目录、CLI 配置目录，只读挂系统目录、CLI 程序与本安装的发布目录；网络在单独的命名空间里，唯一的出路是经守护进程的放行代理连平台地址与模型接口。普通员工的轮次不经过沙箱，与以前完全相同。

**节点要求。** Linux；bubblewrap 0.5 或更新（要用 `--clearenv`）；允许非特权用户命名空间（有的发行版要打开 `kernel.unprivileged_userns_clone`，有的发行版用 AppArmor 限制非特权用户命名空间，要给 `bwrap` 放行）。守护进程在容器里运行、容器又不给用户命名空间时，这台节点跑不了助理。macOS 节点跑不了助理。

**自检与能力。** 守护进程启动时、之后每 30 分钟，按前置验证脚本的同一套检查真起几次沙箱：看不见员工工作目录与本安装的配置、系统目录只读、直连外网失败、经转发口连得到守护进程，再在沙箱里跑一遍 `claude --version` / `codex --version`。结果按 AI 上报到心跳的 `assistant_sandbox_v1`，`runtime.log` 里有一行 `Assistant sandbox: claude=ready, codex=no (ok)`（括号里是失败的原因代码）。没通过的节点不会收到助理的轮次，收到了也回 503。

**登录凭证。** 每一轮从当前席位的登录文件收窄出一份副本只读挂进去：去掉刷新令牌（Codex 还把 `last_refresh` 改成现在），Claude 只留 `claudeAiOauth`。节点原来的登录文件不动；令牌刷新只发生在沙箱外的普通轮次与健康检查里。访问令牌剩余不到 30 分钟时这一轮不跑（`credential_expiring`），等下一次健康检查刷新。用环境变量登录（`CLAUDE_CODE_OAUTH_TOKEN`、`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`）的节点只把那一个变量带进去。沙箱里被注入的助理读得到这一份访问令牌；它只能连平台与模型接口，带不到别处。

**转录。** 一轮结束后守护进程把 CLI 转录打包上传平台（经 TLS，平台用所有者的数据密钥加密保存），然后删掉本轮目录；续聊前再从平台取回。上传失败的本轮目录在 `~/.local/share/coreman-runtime/assistant-turns/` 里留到下一次清扫（启动时一次，之后每 10 分钟）：清扫再传一次，然后删除。沙箱里驱动的输出写在本轮目录里，随本轮删除，不进 `claude.log` / `codex.log`。

**config.json 里的可选键**（改完重启服务）：

| 键 | 说明 |
|---|---|
| `assistant_max_concurrent` | 本节点同时跑的助理轮次上限，默认 8；只是保险，平时由平台设置「每个节点的助理并发」（默认 2）控制，要比它大 |
| `assistant_egress_hosts` | 按 AI 覆盖模型接口地址，如 `{"codex": ["chatgpt.com", "api.openai.com"]}`；只写主机名，端口固定 443 |
| `assistant_sandbox_ro_paths` | 额外只读挂进沙箱的绝对路径（CLI 装在不常见的位置、自检报 `cli_unavailable` 时用） |

**助理池节点只放助理。** 普通员工不在沙箱里，与助理是同一个系统用户，读得到助理这一轮的临时目录与凭证副本；所以管理台把实例加入助理池时，要求节点上没有启用的普通员工。

**升级与回滚。** 先升平台、再升节点，顺序反过来也安全：新平台只把助理的轮次派给声明了 `assistant_sandbox_v1` 的节点（旧节点不声明，助理在旧节点上暂停）；旧平台不发 `sandbox`，新节点照旧处理普通轮次。**平台回滚到不认识 `sandbox` 的版本之前，先停用所有个人助理**——旧平台会把助理的轮次当普通轮次派出、在沙箱外跑：

```sql
-- 回滚前记下并停用；回滚后（或重新升级后）只启用这批。
SELECT id FROM bots WHERE kind = 'assistant' AND enabled;
UPDATE bots SET enabled = false WHERE kind = 'assistant' AND enabled;
```

数据库降级（`alembic downgrade`）到 `0091` 之前会删掉 `assistant_transcripts`：所有助理下一轮都从新会话开始。节点回滚到旧版本：节点不再声明能力，绑在它上面的助理暂停，普通员工不受影响。
````


- [ ] **Step 2: 飞书文档**

`docs/feishu.md`「个人助理应用」一节的列表末尾（「写操作要审批」之后）加：

```markdown
- **在节点的沙箱里运行。** 助理只在声明了沙箱能力的运行时节点上跑（见 `runtime_daemon/README.md`「个人助理的沙箱」），绑在助理池（管理员用 `PUT /api/admin/relay-servers/{id}/assistant-pool` 加入）或所有者本人的私有运行时上。所在节点离线、排空或不能隔离运行时，助理暂停并在私聊里说明，不会换到别的节点。CLI 的转录每轮结束后加密存进平台、节点上删除，续聊前再下发；转录超过 50MB 或没能传回来时，下一轮从新会话开始。每个节点同时跑的助理轮次由设置 `assistant_node_concurrency`（默认 2）限制，超出的排队。
```

- [ ] **Step 3: 同步设计文档第三节**

`docs/superpowers/specs/2026-11-11-personal-assistant.md` 第三节按落地结果改这几处（只改表述，不改决策）：

1. 「沙箱」：可读写一项里「本助理的 CLI 配置目录」后补「（每轮新建、轮次结束删除；续聊需要的会话转录由平台保管）」；补一句「沙箱方案落地为 bubblewrap；节点启动时自检，通过后在心跳里声明 `assistant_sandbox_v1`，平台只把助理的轮次派给声明了的节点」；出网一项补「沙箱断网，经守护进程的放行代理连平台与模型接口；登录令牌的刷新地址不在名单里」；只读一项补「模型登录凭证是去掉刷新令牌的副本」。
2. 「转录」：补「上传与下发凭派出这一轮时的交接编号；上一轮的转录没传回来时不拿旧的续，开新会话」「清扫在启动时与之后每 10 分钟各一次，再传一次后无论成败都删除」。
3. 「Runtime 分配」：补「池节点不能有普通员工（普通员工不在沙箱里，与助理同一个系统用户）」「池节点离线同样暂停，不重新选节点」；每日轮次预算一句后加「（推迟到第 2 期，与盯消息的分级一起做）」。
4. 「威胁模型」里「同一节点上的其他 AI 员工 | 不能」一行的依据改为「沙箱隔离，转录每轮清除；助理池节点不承载普通员工」。

- [ ] **Step 4: 全量检查**

Run: `uv run ruff check . && uv run mypy coreman && uv run alembic check`
Expected: 全部通过。只对本计划改动过的文件跑 `uv run ruff format --check <文件…>`（仓库里有几个与本计划无关的文件本来就不符合格式）。

Run: `uv run pytest tests/unit tests/integration tests/api -q`
Expected: 全部 passed（没有 bubblewrap 的机器上 `test_real_sandbox_self_test` 跳过）。重点看：`tests/unit/test_runtime_daemon*.py`、`tests/api/test_runtime_protocol.py`、`tests/api/test_runtime_queue.py`、`tests/api/test_runtime_daemon_e2e.py`、`tests/integration/test_chat_handler.py`、`tests/unit/test_assistant_sealed_ledger.py`、`tests/integration/test_assistant_approval_result.py`。

- [ ] **Step 5: 真机联调（需要一台过了 Task 0 的 Linux 节点与一个测试用的助理记录；没有就跳过并在 PR 里写明）**

1. 节点升级后看 `runtime.log`：有 `Assistant sandbox: claude=ready …`；管理台实例视图 `assistant_sandbox` 为真。把实例加入助理池（节点上不能有普通员工），助理记录绑上它。
2. 私聊助理一句话。预期：有回复；节点上这一轮期间 `assistant-turns/` 下有一个目录，结束后消失；`assistant_transcripts` 多一行，`blob_enc` 以 `enc:v2:d` 开头，`pending_transfer` 为空。
3. 再问「我刚才说了什么」。预期：续上了上一句（还原成功）。
4. 让助理执行 `curl -sS https://example.com`。预期：失败（403 / 连接被拒）；`runtime.log` 不出现主机名。让它 `ls` 上级目录与节点的家目录。预期：看不到其他员工的目录。
5. 一轮进行中 `kill -9` 守护进程，再启动。预期：启动后的清扫把留下的本轮目录传一次并删除；下一句照常回复。
6. 比对节点登录文件在这些轮次前后的摘要（`sha256sum`）：不变；普通员工在同一时间段内照常对话。
7. 把节点停掉一分钟，私聊助理。预期：回「你的个人助理暂停了：绑定的运行时现在离线……」，没有在别的节点上跑。

- [ ] **Step 6: 敏感词自查后提交**

```bash
git add runtime_daemon/README.md docs/feishu.md docs/superpowers/specs/2026-11-11-personal-assistant.md
git commit -m "个人助理：节点沙箱的部署要求、升级与回滚，设计文档第三节同步"
```

---

## 与设计文档的对照

| 设计文档 | 本计划 |
|---|---|
| 可读写：本人的工作目录、本轮临时目录、本助理的 CLI 配置目录 | Task 1（`bwrap_argv`：`/work`、`/turn/tmp`、`/turn/config` 等）；配置目录每轮新建（「设计选择」第 3 条） |
| 只读：CLI 程序与模型登录凭证 | Task 1（`cli_mounts`、`system_mounts`）、Task 4（凭证副本）、Task 6（只读盖到配置目录） |
| 不可见：其他目录，包括其他员工的工作目录与转录 | Task 1（白名单挂载；自检 `hidden` 检查）、Task 12（池节点不承载普通员工） |
| 出网只放行平台地址与模型接口地址 | Task 1（`--unshare-all`；自检 `offline`、`bridge`）、Task 2、Task 3、Task 6（`turn_policy`） |
| 环境变量里只有本轮能力令牌 | Task 1（`--clearenv` + `sandbox_env`）；能力令牌经请求的 `env_vars` 由驱动叠上，同普通轮次 |
| 沙箱方案与独立系统用户二选一，由前置验证决定 | Task 0；结果不符时见「步骤 0 结果与本计划的对应」 |
| 一轮结束后上传、加密存 `assistant_transcripts`、节点上随即删除 | Task 5、6（打包、上传、删除）、Task 8（表与二进制加密列）、Task 9（`upload`）、Task 11（接口） |
| 续聊前下发、守护进程还原到沙箱内 | Task 6（`download`、`unpack`）、Task 9（`prepare` 的 `restore`、`download`）、Task 11 |
| 单个转录上限 50MB，超过时结束该会话，下一轮开新会话 | Task 5（`TooLarge`）、Task 6（`too_large`）、Task 9（`cleared` → 下一轮 `renew`）、Task 11（413） |
| 守护进程启动时清扫遗留转录：上传后删除 | Task 6（`sweep`，启动时 + 每 10 分钟）、Task 7（`start`） |
| 本轮临时目录在轮次结束时删除；下载的文档与附件只放这里 | Task 6（`_cleanup`）、Task 15（提示词：附件放 `$TMPDIR`） |
| 工作目录分配同普通员工；安全提示词写明不放个人信息 | 分配沿用现有方式（1A / 1F）；Task 15 |
| `relay_servers.assistant_pool`；开通时在池内选助理数最少的在线节点 | Task 8（列）、Task 10（`pick_pool_relay`，1F 调用）、Task 12（开关） |
| 所有者可改绑到自己的私有 Runtime（沿用 relay_policy 的可选规则） | Task 10（`validate_switch` 里的 `assignment_error`，其余规则沿用） |
| 私有节点离线时助理暂停并私信所有者，不回落到池 | Task 10（`turn_blocker`）、Task 14（`paused`） |
| 迁移只需改绑 | 转录在库里、路径固定 `/work`，任何节点都能还原（Task 5、9）；工作目录照现有换运行时流程 |
| 每个节点对助理轮次设并发上限，超出的排队 | Task 11（领取时限并发、排在普通调用之后）、Task 6（节点侧保险） |
| 每人每日轮次预算 | 推迟到第 2 期（「设计选择」第 10 条，待所有者确认） |
| 十一：助理池节点并发上限（设置项） | Task 11（`assistant_node_concurrency`） |
| 测试：轮次结束后节点上无残留；续聊可还原；异常退出后的清扫 | Task 6（`test_turn_streams_and_releases_done_only_after_the_upload`、`test_restore_puts_the_transcript_back_before_the_driver_runs`、`test_failed_upload_is_retried_once_by_the_sweep_then_deleted`、`test_cancelled_turn_uploads_in_the_background`）；真机见 Task 16 Step 5 |
| 威胁模型：同一节点上的其他 AI 员工不能看到 | Task 1（沙箱）、Task 12（池节点不承载普通员工）；见「对设计文档的意见」 |
| 威胁模型：被注入的助理不能外传（沙箱限制出网） | Task 1–3、6；残留风险见「已知限制」 |
| 原则 3：明文在节点上只存在于一轮运行期间 | Task 3（配置目录每轮新建）、Task 6（结束即删；上传失败最多留一个清扫周期） |

## 所有者已确认的取舍

（尚无。本计划的取舍都列在下一节，等所有者答复。）

## 待所有者确认的取舍

1. **每人每日轮次预算推迟到第 2 期**，和盯消息的分级一起做（「设计选择」第 10 条）。现在做只能拒绝所有者本人的请求。另一种做法是第 1 期先加一个只计数、不拦截的用量，代价是多一张表、多一个设置项，而且没有地方用它。
2. **CLI 配置目录每轮新建**，不是每个助理一个长期目录（「设计选择」第 3 条）。更严，但 CLI 在配置目录里记下的其他东西（设置、插件缓存）每轮都从零开始；助理装的技能要放在工作目录里才留得下来。
3. **助理池节点不能有普通员工**（「设计选择」第 13 条，Task 12 的接口里强制）。否则同一节点上的普通员工读得到助理这一轮的明文与凭证副本。代价：池要单独的节点；把普通员工换到池节点上目前不拦（只在加入池时检查），靠部署约定。另一种做法是给助理分配独立的系统用户（设计里的备选方案），见「步骤 0 结果与本计划的对应」最后一行。
4. **池节点离线也暂停，不自动换到池里别的节点**。设计只说了私有节点不回落；本计划对池节点同样处理，因为设计「不做」里有「每轮按负载重新选节点」，而且改绑涉及工作目录复制，应走现有的换运行时流程。代价：池节点维护期间，绑在上面的助理都暂停。
5. **平台回滚前要手工停用助理**（「设计选择」第 8 条与 Task 16 的步骤）。另一种做法是让新守护进程认出助理的 `bot_key`（`assistant-<16 位十六进制>`）、没有 `sandbox` 就拒绝；代价是普通员工的 `bot_key` 恰好是这个形状时会被误拒，而现有的 `bot_key` 校验并不保留这个前缀。
6. **只支持 Claude 与 Codex**（「设计选择」第 11 条），绑到 OpenCode 的助理暂停。
7. **访问令牌剩余不足 30 分钟就不跑这一轮**，等沙箱外的健康检查或普通轮次刷新（Claude 空闲 15 分钟一次健康检查）。助理池节点没有普通员工，刷新只能靠健康检查；Codex 的健康检查只在白天每小时一次。若真机核实发现访问令牌寿命短于一两个小时，需要改成守护进程在沙箱外主动触发一次 CLI 调用来刷新。
8. **拒绝时所有者看到的是通用的「运行时出错」**，具体原因（`credential_expiring`、`sandbox_busy` 等）只在节点日志与任务的错误信息里。要给所有者分原因提示，需要在 worker 的分类里认出 503 的代码，多一处分支。
9. **取不到转录时拒绝这一轮**（`transcript_unavailable`），不是从空会话开始：网络抖一下宁可让所有者再发一次，也不悄悄丢掉上下文。平台明确说没有（404：读不出来、密钥已销毁）时才从新会话开始。
10. **清扫除了启动时，还每 10 分钟一次**。设计只写了启动时；不加的话，上传失败留下的明文要等到下次重启才删。
11. **沙箱里驱动的输出不进 `claude.log` / `codex.log`**，随本轮删除；出问题时节点上没有驱动日志可看，只有 `runtime.log` 里的原因代码。
12. **CLI 自带的联网搜索与抓取关掉**（Claude 在 `settings.permissions.deny` 里加 `WebSearch`、`WebFetch`；Codex 在 `exec` 下默认不开联网搜索，未额外配置）。查外部资料走平台工具。
13. **助理池开关只有接口**，没有管理台页面；管理台的实例列表多了 `assistant_pool`、`assistant_sandbox` 两个字段，前端暂不展示。
14. **会话查看链接对助理无效**：一轮结束后节点上没有这一轮的会话记录。所有者私聊里的会话链接点开是空的。

## 已知限制

- macOS 节点、守护进程在不给用户命名空间的容器里运行的节点、bubblewrap 低于 0.5 的节点都跑不了助理；自检会如实上报，平台不会派过去。
- 平台地址必须是 https：沙箱里只有经放行代理的 CONNECT 一条路，名单只收 https 地址。
- 一轮结束要等转录上传完 `[DONE]` 才放出：转录大时回复的收尾会晚几秒。
- 被取消的一轮在后台上传转录；如果所有者在上传完成之前就发来下一句，下一轮会开新会话。
- 被注入的助理在一轮里读得到访问令牌与本轮的全部内容；能带出去的地方只剩平台（写进知识库或审批单，后者要所有者本人批准才会发出）与模型接口（消耗额度）。
- 共享节点的 root 在助理运行的那一轮里看得到明文（设计的威胁模型接受）。
- 并发超出时的排队沿用调用方现成的 120 秒排队上限；等不到时所有者看到「运行时繁忙」，要再发一次。
- 一轮的转录只取续聊必需的部分（Claude 的 `projects/`、Codex 的 `sessions/` 与线程对应）；CLI 以后把续聊所需的状态放到别处时要改 `runtime_daemon/transcripts.py` 的 `TREES`。
- 转录上传受 50MB（压缩后）限制，还原时解压后不超过 512MB；超过就开新会话。
- `assistant_transcripts` 里会话过期（`session_ttl_hours`，默认 72 小时）一天之后的行由 scheduler 删除；`/clear` 之后旧会话的转录也要等到那时才删。

## 步骤 0 结果与本计划的对应

| Task 0 的结果 | 要改的地方 |
|---|---|
| 个别节点 `bwrap=MISSING` 或 `T1 FAIL` | 不改代码：这些节点自检不过、不声明能力；装上 bubblewrap 0.5+ 并放开非特权用户命名空间后重启守护进程即可 |
| `*_visible` 不为 0、`write_outside=allowed` | 探测脚本是「整盘只读再遮盖」，本计划是白名单挂载，可能不受影响；先用 Task 1 的自检（`hidden_check_failed`）在该节点上复核，仍不过就查 `SYSTEM_PATHS` 与 `cli_mounts` 是否把员工目录带了进来（例如 CLI 装在员工目录的上级里），改 Task 1 |
| T3 / T4 用只读原文件认证失败，但 Step 0.3 的收窄副本成功 | 不改：本计划本来就挂收窄副本 |
| Step 0.3 的收窄副本也失败（CLI 要求有刷新令牌字段，或启动时就要刷新） | 改 Task 4：副本里保留刷新令牌字段但换成无效值（如空串），或改为每轮在沙箱外先跑一次 `<cli> --version` 之类的命令触发刷新再复制；同时把 Task 0 的 Step 0.3 换成对应做法再测 |
| Step 0.3 的 `node_login_unchanged=NO` | 停：沙箱里的 CLI 动了节点的登录文件（说明文件没有真的只读）。查 bwrap 版本与挂载顺序，Task 1 的 `bwrap_argv` 必须让凭证最后挂、只读 |
| T5 输出 `http=200`（断网命名空间不生效） | 自检的 `offline` 检查会失败、不声明能力；若所有节点都这样，改用设计里的备选「按进程组做防火墙规则」：需要 root，新增一个由节点管理员装的特权辅助程序，改 Task 1 的参数（去掉 `--unshare-all` 里的网络隔离）与 Task 3（放行改在防火墙层），工作量另估 |
| `cgroup_fs=cgroup2fs` 且 `nft=yes` | 只是备选的前提，不改 |
| Step 0.4 Claude 还原后不能续聊 | 改 Task 5 的 `TREES`：把 Claude 续聊还需要的文件（例如配置目录里的会话索引）加进转录；Task 6 的用例相应补一份；平台侧不用改 |
| 所有候选节点都不能用 bubblewrap（例如都在不给用户命名空间的容器里） | 换设计里的备选「每个助理一个独立系统用户，编号由平台统一分配」：Task 8 加列 `bots.assistant_uid`（序列分配，跨节点一致）；Task 1 的 `bwrap_argv` 换成「以该用户身份起驱动」的启动器（需要节点上预建用户与一条只允许启动驱动的 sudo 规则，或一个 setuid 的小启动器）；Task 6 的目录改成属该用户、0700；出网限制改为按用户的防火墙规则（需要 root）；Task 7 的能力改由「用户与规则都已就位」的自检得出。其余任务（转录交接、加密、分配、worker）不变 |

## 仍需真机核实

1. **两种 CLI 在没有刷新令牌的凭证下能否正常工作、访问令牌的寿命**：Claude 的 `.credentials.json` 结构（`claudeAiOauth.accessToken / refreshToken / expiresAt`，`expiresAt` 是毫秒）与 Codex 的 `auth.json` 结构（`tokens.access_token / refresh_token`、`last_refresh`、访问令牌是带 `exp` 的 JWT）是读守护进程现有代码（`daemon.login_account`）与社区资料得来的，没有对着真实文件核对；刷新令牌是否一次性、刷新后旧的访问令牌是否立即失效，也没有核实。Task 0 的 Step 0.3 与 Task 16 的 Step 5 第 6 条覆盖。影响 Task 4 与「待所有者确认的取舍」第 7 条。
2. **CLI 是否遵守 `HTTPS_PROXY`**：Claude Code 的推理请求与 MCP（HTTP 传输）请求、Codex 的请求是否都走代理。若有哪一条直连，在沙箱里会连不上（失败而不是泄漏）。影响 Task 3、6。
3. **Codex 在 `exec` 下默认不开联网搜索**：若默认开启，要在本轮配置目录里写一份关掉它的 `config.toml`（键名随版本变化，要对着节点上的版本确认）。影响 Task 6。
4. **CLI 在全新的配置目录里能否直接跑**：Claude 的 `-p` 模式与 `bypassPermissions`、Codex 的 `exec` 在没有 `.claude.json` / `config.toml` 时是否要求初始化。Task 0 的 Step 0.3 用的就是全新目录，会暴露这个问题。
5. **常见发行版上 bubblewrap 的版本与用户命名空间设置**：`--clearenv` 需要 0.5+；有的发行版用 AppArmor 限制非特权用户命名空间。Task 0 覆盖。
6. **Claude 还原转录后的完整续聊**（总览「步骤 0」0.3，Task 0 Step 0.4）。
7. **网络存储上的工作目录**：bind 挂载网络文件系统、在其中写入，是否受用户命名空间的 uid 映射影响（`--unshare-all` 下 uid 不变，预期没问题）。Task 16 Step 5 在这样的节点上做一遍。

## 对设计文档的意见

1. **「同一节点上的其他 AI 员工不能看到」只在节点上没有普通员工时成立。** 普通员工的 CLI 不在沙箱里、以免审批模式运行，与守护进程是同一个系统用户，能读守护进程数据目录下助理这一轮的临时目录、还原出来的转录与凭证副本。沙箱只挡住「助理看别人」。本计划用「助理池节点不承载普通员工」补上（Task 12），并写进待确认的取舍第 3 条；私有运行时上放什么由所有者自己承担。要在混用的节点上也成立，只能给助理独立的系统用户（设计里的备选方案）。
2. **「本助理的 CLI 配置目录」若长期保留，与原则 3 冲突**：两种 CLI 都在配置目录里留提示历史。本计划改为每轮新建（待确认第 2 条）。
3. **「每人每日轮次预算，用完后非紧急事项攒到次日」依赖第 2 期的分级**，第 1 期没有「非紧急事项」可攒（待确认第 1 条）。
4. **只读挂载原登录凭证不安全**：前置验证脚本与总览「步骤 0」的判定表默认只读挂原文件，沙箱里的 CLI 一旦尝试刷新就可能让整台节点掉登录。本计划改挂去掉刷新令牌的副本（「设计选择」第 5 条），Task 0 加了一步专门验证它。

## 自查记录

- 设计第三节逐句对应见「与设计文档的对照」；没有对应到任务的只有每日轮次预算（推迟，待确认）。总览接口表里 1E 的一行「轮次请求带 `sandbox: true` 与 `transcript` 下发、轮次结束回传」：请求体字段见 Task 13（`ChatRequest.to_body`），节点处理见 Task 6–7，回传见 Task 6（`upload`）与 Task 11（`PUT /api/runtime/assistant-transcripts/{transfer}`）。1F 要用的只有 `runtime.pick_pool_relay(session, provider)`（开通时选节点）；停用时转录不用单独处理：密钥销毁后已不可读，行随 `bots` 级联删除或由 `transcripts.purge` 回收。
- 占位扫描：全文没有 TBD、「类似 Task N」之类的写法；每个代码步骤都给了完整代码或逐处的替换内容。
- 名字与签名前后一致：节点侧 `sandbox.CAPABILITY`、`TurnLayout.new(turns_dir, socket_dir)`、`Launch(...)`、`bwrap_argv(launch, command)`、`bridge_command(python, bridge, command)`、`driver_command(binary)`、`sandbox_env(provider, base, extra)`、`cli_mounts(provider, which)`、`link_cli(layout, provider, which)`、`self_test(inputs, *, bwrap, system_name, runner)`、`egress.turn_policy(*, provider, api_url, env_vars, model_hosts, control_proxy, ai_proxy)`、`egress.Egress(policy).start(path)`、`turn_credentials.for_provider(provider, seat_env, home)`、`turn_credentials.write(directory, credential)`、`transcripts.pack(root, provider, session_id)`、`transcripts.unpack(root, provider, session_id, data)`、`AssistantTurns.run(identity, provider, data, frames)` / `sweep()` / `upload(transfer, data, status)` / `download(transfer)` / `capability(provider)`、`plan_turn(provider, data)`；平台侧 `Cipher.encrypt_bytes(data, aad)` / `decrypt_bytes(token, aad)`、`sealed.Blob`、`sealed.key_ready(session, bot_id)`、`transcripts.prepare(session, *, bot, node_id, info, session_key, backend, speaker_user_id)`、`transcripts.download(session, *, node_id, transfer)`、`transcripts.upload(session, *, node_id, transfer, status, data)`、`transcripts.purge(session, before)`、`transcripts.Plan.body()`、`sessions.renew(session, *, bot_id, session_key, backend, speaker_user_id)`、`runtime.unavailable_reason(relay, node)`、`runtime.turn_blocker(session, bot)`、`runtime.assignment_error(relay, node, owner_id)`、`runtime.pick_pool_relay(session, provider)`、`runtime.hosts_standard_bots(session, node_id)`、`runtime.node_sandboxed(node, provider=None)`、`assistant_runtime.paused(ctx, intake)`、`ChatRequest(sandbox=…, transcript=…)`、`transport.SANDBOX_EXTENSION`、`workspace_prompt.WORKSPACE_RULES`。请求体字段 `sandbox`、`transcript.transfer`（32 位十六进制）、`transcript.restore`；上传头 `X-Transcript-Status`（`ok` / `empty` / `too_large` / `unchanged`）在 Task 6、9、11 里一致。
- 台账：`coreman/core/assistants/transcripts.py` 取整行 1 处（`upload` 的 `session.scalar(select(AssistantTranscript)…)`）、引用内容列 1 处（`defer(AssistantTranscript.blob_enc)`）、写 1 处（`prepare` 的 `insert`）；`download` 用 `session.get` 自动还原，不计入。
- 计划里的实现代码与用例在仓库的一份临时副本上按最终状态落过一遍（不是逐个任务提交）。跑过的：`uv run ruff check .` 通过；本计划改动与新增的全部文件 `ruff format --check` 通过；`uv run mypy coreman` 通过（475 个源文件）；在一个新建的空库上 `alembic upgrade head`、`alembic check`（No new upgrade operations detected）、`alembic downgrade 0090` 再 `upgrade head` 通过，用完已删除该库；文中 19 个新用例文件加上改动过的 codec、台账、设置项用例共 128 条：127 条通过、1 条跳过（`test_real_sandbox_self_test`，开发机没有 bubblewrap）；副本上全部单元、集成与接口用例跑了一遍：4419 条通过、3 条跳过（基线 4312 通过、2 跳过，多出的跳过就是上面那条）。现有的助理用例在 Task 14 的 `into_assistant` 补上 `sandbox_ready` 之后全部照常通过。Go 驱动没有改动，`go build ./...` 通过，没有跑 `go test`。
- 没有验证的：真机上的 bubblewrap 沙箱（开发机是 macOS，没有 bubblewrap，`test_real_sandbox_self_test` 跳过；真起沙箱的参数只在单元测试里逐项核对过）；CLI 在沙箱里的真实行为、凭证收窄后能否推理、`HTTPS_PROXY` 是否被遵守（见「仍需真机核实」）；各任务 Step 2 写的「预期失败信息」没有逐个跑过，各任务提交时的中间状态也没有逐个跑用例；Go 驱动没有改动，没有跑 `go test`。
