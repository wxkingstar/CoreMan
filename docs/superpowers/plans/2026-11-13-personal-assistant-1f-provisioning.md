# 个人助理 1F：开通与停用 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 成员私聊助理应用即可开通自己的个人助理：卡片上点「开通」→ 平台核对范围、建助理记录与数据密钥、在助理池里选一台能隔离运行的节点并建一个全新的工作目录、发起飞书个人授权 → 授权完成后助理才开始处理消息。停用由所有者在私聊里确认、平台管理员在助理列表里执行、或成员账号停用满 24 小时自动发生：停下在跑的轮次、撤回待批的审批单、撤销飞书授权、销毁数据密钥、停用记录、再经节点删掉工作目录。两条流程都由看护进程一步一步推进，任何一步失败都停在原地重试，不会留下能跑轮次的半成品。顺带收掉 1E 留下的四个节点侧与平台侧问题。普通员工（`kind='standard'`）的行为不变。

**Architecture:** 记录本身的状态放在 `bots.assistant_state`（`provisioning` / `active` / `deprovisioning` / `deprovisioned`），1E 的 `runtime.turn_blocker` 先看它：不是 `active` 一律不派，读的是已加载的对象，不加查询。流程的进度放在新表 `assistant_lifecycles`（一人同时只有一条没结束的：部分唯一索引），由 scheduler 里单独的一条循环（`lifecycle_runner.tick`，每 5 秒）推进：每一步一个事务、一个总时限，暂时的失败退避重试，开通连续失败或再也做不成就放弃并走一遍停用。入口只写库：网关在没有助理记录的人私聊时登记一条「要发开通卡片」的流程、在开通卡片被点时核对并推进（`provisioning.request_offer` / `accept_offer`，都在 1B 留下的 `AssistantInbound.on_unprovisioned` 里，带保存点、出错不挡应答）；worker 在助理的私聊里截下「停用助理」发确认卡片，确认后调 `deprovisioning.request`；管理员接口同样只调它。给本人的消息一律经助理应用自己的凭证发（`lifecycle.Messenger`），不经某条记录的出站箱。工作目录的删除单独登记在 `assistant_retired_dirs`，由同一条循环经节点命令 `workspace-remove` 删：节点只删带这位助理所有权标记的目录，先原子地改名进工作目录根下的回收站，再在后台线程里不跟随任何链接地删掉；助理换运行时换走的旧目录同样登记。

**Tech Stack:** Python、FastAPI、SQLAlchemy async、Alembic、PostgreSQL、httpx；节点侧纯标准库（Python 3.10 语法）；pytest（httpx.MockTransport、替身）。

**Spec:** `docs/superpowers/specs/2026-11-11-personal-assistant.md`（「已定决策」「原则」「威胁模型」「一、助理记录与入口路由」「三、节点隔离」「四、审批闸门」「十、开通、冷启动与停用」「十一、管理员控制与审计」「分期」「不做」「测试」）；总览 `docs/superpowers/plans/2026-11-11-personal-assistant-phase1.md`（1F 一行与接口表）；前一期 `docs/superpowers/plans/2026-11-12-personal-assistant-1e-node-isolation.md`。

## Global Constraints

- 本计划建立在 1E 分支之上（它的最新提交之后开工），不合并 `main`。迁移头是 `0091`，本计划的迁移写作 `0092`、`down_revision = "0091"`；整叠落地时的改号见文末「落地前提」。
- 公开仓：代码、测试、文档、提交信息只用通用占位（`example.com`、`demo-bot`、`cli_example`、张三 / 李四），提交前跑仓库约定的敏感词自查。
- 普通员工（`kind='standard'`）的行为不变。共用路径上的每一处改动都挂在已加载对象的 `is_assistant(bot)` 下，或只多一个字典项、一个枚举值；普通员工每条消息、每一轮的路径上不加查询、不加等待、不加锁。开通、停用、成员离开的扫描、删目录、设置读取都只在助理应用的网关连接、看护进程的单独循环、助理的 worker 分支与管理员接口里，各自用自己的会话、`try/except` 与时限。每个任务的「普通机器人不受影响」一栏写明由哪条测试、对着真实调用点保证。
- 失败一律不放行（fail closed）：开通没走完、正在或已经停用的记录跑不了任何一轮；开通的任何一步放弃都走一遍停用；停用一旦开始就不可撤销、失败原地重试直到做完；数据密钥只在待批的审批单撤回之后才销毁。
- 卡片点击是攻击者可控的输入：认人只认飞书给的操作人（网关按它找到人，或 worker 按入站事件的发送人核对），卡片里的值只用来认「是不是平台发给这个人的那一张、还没过期」：开通卡片的令牌只存摘要、点过即清、30 分钟过期，并要求回调里的消息与会话就是发卡片时飞书返回的那两样；停用确认卡片走记录的出站箱（`CardActionHandler` 核对卡片由这条记录发到这个会话），令牌是主密钥派生的 HMAC，绑定记录、发卡片的那一轮与到期时刻。
- 平台发给助理所在节点的一切都在助理专用实例的命令名单之内；本计划只加一个命令 `workspace-remove`，并按「工作目录里的东西可能被沙箱里的助理换掉」来写它。
- 日志与发给本人以外的任何地方只带代码、编号与异常类型名；发给本人的消息不带节点、主机或任何内部信息。
- 节点侧（`runtime_daemon/`）只用标准库、Python 3.10 语法；每个改过的 `runtime_daemon/*.py` 都要 `ast.parse(..., feature_version=(3, 10))` 通过。
- 本计划不做：冷启动（拉口吻样本、选择卡问答）、盯消息、知识库、导入命令、「我的助理」页（第 2、3 期）；每日轮次预算（第 2 期，1E 已推迟）；`assistant_access_log`（见「设计选择」第 11 条）；设计「不做」里的一切。
- 验证命令：`uv run pytest <路径> -q`、`uv run ruff check .`、`uv run mypy coreman`、`uv run alembic check`（`TEST_DATABASE_URL`、`PYTEST_XDIST_WORKER` 按本机配置）。

## 设计选择

**1. 状态分两处：记录上一列管「能不能跑」，流程表管「做到哪一步」。**

| 做法 | 取舍 |
|---|---|
| 只用 `bots.enabled` | 不够：开通中的记录要能收私聊（回一句「还没开通完」、发授权卡片），还不能跑轮次；只有启停两态表达不了 |
| 只用流程表，`turn_blocker` 每轮查它 | 没有采用：每轮多一条查询，且「记录能不能跑」散在两张表里，漏查一处就是半成品能跑 |
| **记录上加 `assistant_state`，进度放 `assistant_lifecycles`** | **采用。** `turn_blocker` 第一行看已加载对象上的这一列（不是 `active` 就不派，空值也不派），零查询；流程表放步骤、期限、重试次数、错误代码、卡片令牌摘要，部分唯一索引保证一人同时只有一条没结束的流程 |

为什么一人只能有一条没结束的流程（开通与停用互斥）：数据密钥按人存（`assistant_keys` 主键是 `user_id`）。停用还没销毁旧密钥时又开通，新记录会沿用旧密钥，随后停用把它销毁，新助理的内容全部读不出来。所以停用期间点开通只会得到「卡片已失效」，停用做完才能重新开通；开通中途请求停用，开通那条直接结束（`cancelled`）。

记录状态与流程步骤（`→` 表示下一步；每一步一个事务，失败都停在原地，`next_attempt_at` 到了再试）：

| 记录状态 | 流程 · 步骤 | 事件 | 下一步 / 副作用 |
|---|---|---|---|
| （无记录） | — | 没有助理的人私聊、或打开与应用的私聊（网关） | 新建开通流程 `offered`，`next_attempt_at=now`；24 小时内刚因不在范围结束过、或 10 分钟内刚失败过的不建；已有一张发出满 10 分钟还没点的卡片就重发一张 |
| （无记录） | 开通 · `offered` 到期 | 看护进程 | 不在范围 / 账号停用 / 没有登录名：私信说明，结束 `ineligible`；否则发开通卡片（新令牌，只存摘要），记下飞书返回的消息与会话，期限 30 分钟，等点击（`next_attempt_at` 置空） |
| （无记录） | 开通 · `offered` 等点击 | 本人点卡片（网关；认人按操作人） | 令牌摘要、消息、会话、期限都对 → 清掉摘要、记下操作人的 `open_id` 与 `tenant_key`，→ `create`；任何一项不对 → 不动（应答「卡片已失效」） |
| （无记录） | 开通 · `offered` 等点击 | 30 分钟没点 | 结束 `expired`；之后再发消息重新开始 |
| `provisioning`（新建） | 开通 · `create` | 看护进程 | 重新核对范围；按 `assistant_backends` 在池里挑能隔离运行的节点（没有 → 暂时失败）；一个事务里建记录（目录名带记录编号）、占目录、签发（或换代）数据密钥 → `init_workspace` |
| `provisioning` | 开通 · `init_workspace` | 看护进程 | 节点 `workspace-init`（助理专用实例对已带本记录标记的目录直接返回，重试安全）→ `start_grant` |
| `provisioning` | 开通 · `start_grant` | 看护进程 | 发起飞书个人授权（全部权限、带离线访问），私信授权卡片（第几次记在 `issued`）→ `await_grant`；应用权限不够 → 放弃 |
| `provisioning` | 开通 · `await_grant` | 看护进程（按飞书给的间隔查） | 授权完成 → 记录 `active`，结束 `done`，提交后私信欢迎语；链接过期或被拒 → 再 `start_grant`，发满 3 次 → 放弃 |
| `provisioning` | 开通 · 任一步 | 连续暂时失败满 10 次、或再也做不成（不在范围、应用不可用、权限不够、本人账号停用） | 结束 `failed`；已建记录的 → 新建停用流程（`abandoned`）收拾；提交后私信原因 |
| `active` | — | 所有者确认停用（worker）/ 管理员停用（接口） | 记录 `deprovisioning`；新建停用流程 `drain`（开通流程若还在就结束 `cancelled`） |
| `active` | — | 看护进程发现所有者账号已停用 | 记录 `deprovisioning`（助理暂停）；新建停用流程 `grace`，期限 24 小时 |
| `deprovisioning` | 停用 · `grace` | 每 10 分钟查一次 | 账号恢复 → 记录回到 `active`，结束 `cancelled`；到期仍停用 → `drain` |
| `deprovisioning` | 停用 · `drain` | 看护进程 | 叫停这条记录没结束的任务（已批准的写操作照常执行完）；在跑的都停了、或等满 15 分钟 → `withdraw` |
| `deprovisioning` | 停用 · `withdraw` | 看护进程 | `approvals.withdraw_pending` → `revoke` |
| `deprovisioning` | 停用 · `revoke` | 看护进程 | `feishu_personal.service.revoke_grant`：库里的令牌当场清空，飞书那边尽力撤销 → `destroy` |
| `deprovisioning` → `deprovisioned` | 停用 · `destroy` | 看护进程（一个事务） | 再撤一次期间冒出来的待批单；销毁数据密钥；登记删目录；记录停用（`enabled=false`）、腾出 `bot_key`；结束 `done`；提交后按触发方私信（成员离开不发） |
| `deprovisioned` | 待删目录 | 看护进程 | 节点 `workspace-remove`：删了或「不是这位助理的」→ 删登记；节点离线或有轮次在用 → 退避重试（最长 6 小时一次），永不放弃 |

**2. 谁来推进：scheduler 里单独的一条循环。**

| 位置 | 取舍 |
|---|---|
| 网关的应答窗口里做完 | 不行：窗口里有 1.5 秒的语句时限，开通要调节点与飞书（几十秒级） |
| worker 任务 | 没有采用：任务表的 `bot_id` 必填，开通的前两步还没有记录；重试、幂等、超时都要另做一遍；worker 的进程与普通员工共用名额 |
| 与普通员工共用的那一轮看护（`_tick_loop`） | 不行：节点离线时一步要等几十秒，会拖住普通员工的回收与清理 |
| **单独一条循环（`_assistant_lifecycle_loop`，每 5 秒，只在领导者上跑）** | **采用。** 与 `_wecom_provisions_loop` 同一个形状：自己的会话、自己的 `try/except`，每一步另有 150 秒总时限；授权完成后最多 5 秒就开通好 |

入口（网关、worker、管理员接口）只写库，不做任何网络调用。

**3. 开通卡片的认人与防重放。** 卡片按 `user_id` 私信给本人，按钮的值只有 `{"assistant": "provision", "token": <32 字节随机>}`。回调到网关时：按回调里飞书给的操作人（`event.operator.user_id`）经通讯录认出人，只看这个人自己那条开通流程；令牌按 SHA-256 比对（库里只存摘要），还要求回调里的 `open_message_id` 与 `open_chat_id` 正是发卡片时飞书返回的那两样（转发出去的卡片是另一条消息，群里也不是这个私聊），卡片 30 分钟内有效，点过即清掉摘要。重放、转发、别人点、换会话点都什么也不做，应答「这张卡片已失效」。授权用的 `open_id` 与 `tenant_key` 取自这次回调里飞书给的操作人与事件头，授权完成时再由飞书的用户信息接口核对一遍（1A 起的 `authorization_status`）。

**4. 停用确认卡片。** 所有者发「停用助理」（或「/停用助理」）时，worker 在派出这一轮之前截下（不经模型；开通没走完或运行时暂停时也能用），经这条记录的出站箱发一张确认卡片，`task_id` 是 `personal:<这一轮的任务号>`、`event_key` 是 `assistant_stop`，`level` 里是「到期时刻.HMAC」：HMAC 用主密钥派生（`Cipher.mac`），绑定记录、任务号与到期时刻。点击走现有的 `CardActionHandler`：它先核对出站记录（卡片确实由这条记录发到这个会话），再交给 `assistant_stop.handle`：记录必须是助理、按入站事件的发送人认出的人必须是所有者、HMAC 对、没过期（10 分钟）。复用 `personal:` 前缀是因为网关的归一化只放行认得的 `task_id` 形状，不改网关就能接上。

**5. 停用的顺序与可恢复。** `drain → withdraw → revoke → destroy`，再由待删目录单独删目录。

- 先停轮次：开着的轮次可能还在提交审批单；记录一进入 `deprovisioning`，`turn_blocker` 就挡住新的一轮（含审批结果回合），`drain` 再叫停在跑的。已批准、正在执行的写操作不叫停（所有者批准过；中途杀掉会落成 `execution_interrupted`）。
- 撤回在销毁之前：`destroy` 那个事务里再撤一次，期间冒出来的待批单也撤掉。撤回与撤销授权都不需要数据密钥（审批单按外部编号撤回；授权令牌用平台主密钥加密）。
- 销毁密钥不等目录删完：工作目录不是用数据密钥加密的，节点可能长期离线；要是等它，成员离开后密钥迟迟不能销毁。目录的删除登记在同一个事务里，之后一直重试。
- 停用记录时把 `bot_key` 改成 `<原值>-x<记录编号前 8 位>`：1A 的 `new_assistant_bot` 用固定的 `assistant-<所有者编号前 16 位>`，不腾出来就不能重新开通。
- 每一步失败原地重试，永不放弃；只有「记录被删了」会让停用提前结束（这时直接销毁密钥）。

**6. 节点删目录：`workspace-remove`。** 只在助理专用实例上做，名单里加上它。工作目录在沙箱里可写，所以：目录的每一级（工作目录根以下）不能是链接（沿用 `Workspace.__init__` 的检查），所有权标记必须是这位助理的（标记按 Task 1 的方式读）；先把整个目录 `rename` 进工作目录根下的回收站 `.coreman-trash/<随机名>`（同一文件系统，一步完成，持有操作锁的时间是常数），再在后台线程里用 1E 的 `sandbox.remove_tree` 删（`rmtree` 走文件描述符，不跟随链接；子目录被改成 000 的改回 0700 再删），删不完的由助理侧的清扫（启动时与之后每 10 分钟）接着删。回收站必须是本用户的真实目录，不是就拒绝。还有沙箱里的轮次挂着这个目录时不删、答复 `busy`（轮次登记了自己的工作目录），平台退避再来：否则沙箱还能往回收站里写，删除时的「先看再改权限」就有了竞态。目录不在算删完；不是这位助理的（没有标记、标记是别人的）不动，平台删掉登记。

**7. 换运行时换走的旧目录也删。** `switch_relay` 在助理改绑之后、同一个事务里登记旧的（实例，目录），条件是不是「还是原来那个目录」（1E 的 `reused`：同一节点同一路径，或共享盘上探测到的同一份）。安全性：1E 起助理只换到目标上不存在的新目录，所以只要换成功，旧路径要么在另一台节点上、要么是同一台节点的另一个路径，删它不会碰到新目录；节点那边还要核对标记。删除前平台再看一眼：同一条记录现在又在用同一节点同一路径（不应发生）就只删登记、不删目录。这样「助理回不到用过的节点」只剩旧节点一直离线这一种情况。

**8. 成员离开。** 判据是 `users.status = 'disabled'`（通讯录同步发现离职、被冻结、从目录里消失，或管理员手工停用）。看护进程每轮扫一次「所有者已停用、记录还是 active」的助理（只扫助理，最多 20 条），为它开一条停用流程：记录立即 `deprovisioning`（助理暂停），先宽限 24 小时，期间每 10 分钟看一次，账号恢复就作罢、记录回到 `active`。没有挂在通讯录同步的事务里：那个事务是全员的，停用要调飞书、调审批系统。开通中的记录不走这条：开通的每一步都核对本人账号，停用了就放弃开通。

**9. 管理员只有「停用」，没有「暂停」。** 1E 去掉了管理者启停别人助理的入口（那个接口会把整条记录返回去）。本计划给平台管理员一个 `POST /api/admin/assistants/{bot_id}/deprovision`：要求请求体明确带 `confirm: true`，答复只有记录编号与状态，写审计。列表 `GET /api/admin/assistants` 只出人、实例、节点、状态、流程步骤与错误代码、待删目录数，不碰任何内容列。同时把绕过停用的两个口子关上：助理记录的启停开关一律 409（直接翻 `enabled` 会跳过撤授权、销毁密钥），删除只允许已经 `deprovisioned` 的记录。没做「暂停」：暂停要么同样不可逆地动授权与密钥，要么留下一条能随时恢复的记录与授权，两样都不比停用更好；需要时所有者可以重新开通。

**10. 范围与审批系统账号。** 两个设置项：`assistant_eligibility`（`all` 全员；`listed` 时名单里的人，或本人所在的任一部门及其上级部门在名单里）与 `assistant_backends`（开通时按顺序挑第一个池里有可用节点的 AI）。不论设置，账号要启用、要有登录名（审批单按登录名认发起人）。设置读坏了（不是对象、`mode` 不认识）谁都不放行。审批系统账号查不到（1D 已核实：没有按用户名查账号的接口），最小的诚实做法是：开通卡片与欢迎语都写明「先登录一次审批系统」，没有账号时第一次提交审批由 1D 的 `tower_account_missing` 如实告诉所有者（提示里写了怎么做）。

**11. `assistant_access_log` 推迟到第 3 期。** 设计要它记录「运行时路径之外的每次解密」。第 1 期这样的解密只有一种：所有者在管理台读自己的对话记录（`chat_logs`、`dev` 两个接口，`sealed.open_rows(..., viewer=actor.id)` 只对所有者本人还原，管理员与其他人拿到空值）。导出、管理命令、「我的助理」页都在第 3 期。现在建表只会记下「你读了你自己的记录」，而且没有页面给所有者看；和第 3 期的导出、导入、页面一起做。

**12. 1E 留下的四件事。**

| 问题 | 做法 | 为什么这样 |
|---|---|---|
| 守护进程读所有权标记会跟随链接、会在 FIFO 上卡住（持着操作锁，之后该节点所有 `workspace-*` 命令挂起） | `read_small`：`O_RDONLY｜O_NOFOLLOW｜O_NONBLOCK` 打开，`fstat` 只认普通文件，最多 64 KB；读不了一律当作「不是本员工的」。助理专用实例的 `workspace-init` 对已存在的目录不读里面任何文件：带本记录标记就原样返回，没标记的非空目录拒绝 | 同一个文件描述符上核对，没有先查后用的窗口；所有权标记之外，助理专用实例在沙箱外读助理目录的只有 `workspace-init` 的说明文件，干脆不让它读 |
| 改回普通实例后 `assistant-turns/` 没人清（可能有没上传的明文转录） | 普通实例启动时在一个线程里删一次（目录是链接就只删链接） | 只删助理专用实例才会写的目录；在线程里做，不要事件循环、不挡启动，出错只记类型 |
| 健康探测目录在工作目录根下，节点不拒绝把它当沙箱的工作目录 | 助理专用实例拒绝工作目录落在 `.coreman-health` 与回收站 `.coreman-trash` 里的对话请求与 `workspace-*` 命令（`reserved_directory`，按真实路径算） | 比把探测目录挪进数据目录小：挪目录会改所有节点（含普通实例）的探测行为、留下旧目录；拒绝只在助理专用实例上生效，普通实例一字不改（1E 记下的「普通员工同样可借此执行项目级钩子」是既有问题，不在本计划里动） |
| 换运行时后首轮排队超时，下一轮丢了上下文说明 | `prepare`：会话带着 `inherited_from`、转录行交接已了结且没有内容（`pending_transfer` 为空、`size == 0`）时与没有转录行一样，不换新会话 | 排队超时由 `release` 作废编号后行就是这个样子；上传过「空」的也一样没有可续的状态，带上下文说明是唯一的连续性 |


## File Structure

| 文件 | 职责 |
|---|---|
| `runtime_daemon/workspace.py` | 所有权标记的安全读取（`read_small`、`marker_data`）；助理专用实例只在全新目录上初始化；保留目录（`reserved`）、回收站（`trash_dir`）、`Workspace.remove` |
| `runtime_daemon/daemon.py` | 名单加 `workspace-remove`；保留目录的拒绝；`RuntimeAgent.assistant_only`；改回普通实例后清 `assistant-turns/` |
| `runtime_daemon/assistant_turns.py` | 在跑轮次的工作目录（`workdirs`、`in_use`）；清扫时清回收站（`purge_trash`） |
| `coreman/core/assistants/transcripts.py` | 换运行时后首轮排队超时不丢上下文说明 |
| `migrations/versions/0092_assistant_lifecycle.py`（新） | `bots.assistant_state`、`assistant_lifecycles`、`assistant_retired_dirs` |
| `coreman/core/db/models/assistants.py`、`bots.py`、`__init__.py` | 模型 `AssistantLifecycle`、`AssistantRetiredDir`，列 `Bot.assistant_state` |
| `coreman/core/assistants/records.py` | 新记录从 `provisioning` 开始；`assistant_state` 加锁；管理台的两句提示 |
| `coreman/core/assistants/runtime.py` | `turn_blocker` 先看记录状态；`LIFECYCLE_MESSAGES` |
| `coreman/core/settings_schema.py` | 设置项 `assistant_eligibility`、`assistant_backends` |
| `coreman/core/assistants/eligibility.py`（新） | 谁能开通、用哪种 AI |
| `coreman/core/assistants/lifecycle.py`（新） | 流程的公共件：一人一条进行中的流程、步骤成败、给本人发消息（`Messenger`） |
| `coreman/core/assistants/retired_dirs.py`（新） | 待删工作目录的登记与删除 |
| `coreman/core/relay/agent_client.py` | 平台侧命令名单加 `workspace-remove` |
| `coreman/core/bots/switch_relay.py` | 助理换走之后登记旧目录 |
| `coreman/core/assistants/deprovisioning.py`（新） | 停用：入口 `request` 与各步 |
| `coreman/core/assistants/provisioning.py`（新） | 开通：网关的两个入口与各步、卡片与文案 |
| `coreman/core/assistants/lifecycle_runner.py`（新） | 看护进程推进流程：过期卡片、成员离开、到期的步骤、待删目录 |
| `coreman/runtime/scheduler/service.py` | 单独的一条循环 `_assistant_lifecycle_loop` |
| `coreman/runtime/gateway_feishu/assistant_inbound.py`、`reply_buttons.py`、`channel.py` | 没有助理的人私聊时登记开通；开通卡片的点击与应答 |
| `coreman/runtime/worker/assistant_stop.py`（新） | 「停用助理」命令、确认卡片与它的回调 |
| `coreman/runtime/worker/chat/handler.py`、`card_actions.py` | 接上停用命令与确认卡片 |
| `coreman/api/routers/assistants_admin.py`（新）、`coreman/api/main.py` | 管理员的助理列表与停用 |
| `coreman/api/routers/bots.py`、`bots_extra.py` | 助理记录不能用启停开关，停用完成前不能删除 |
| `tests/integration/lifecycle_helpers.py`（新） | 开通与停用用例的公共件：成员、助理应用、池节点，飞书与节点的替身 |
| `docs/feishu.md`、`runtime_daemon/README.md`、`docs/operations.md` | 使用、部署、升级与回退 |
| `docs/superpowers/specs/2026-11-11-personal-assistant.md` | 第三节「已知限制」、第四节的账号一项、第十节、第十一节按落地结果同步 |

---



### Task 1: 节点：所有权标记按「可能被换掉」来读，助理专用实例只在全新目录上初始化

**Files:**
- Modify: `runtime_daemon/workspace.py`（新增 `MARKER_LIMIT`、`read_small`、`marker_data`；`Workspace.owned`、`Workspace.init`、`Workspace.finish`）
- Test: `tests/unit/test_runtime_workspace_hostile.py`（新建）

**Interfaces:**
- Consumes: 1E 的 `Workspace`（`runtime_daemon/workspace.py`）、`Agent.workspace(raw)`
- Produces:
  - `workspace.read_small(path, limit=MARKER_LIMIT) -> bytes | None`：不跟随链接、不在 FIFO 与设备上等待、只读普通文件、超过上限返回 None
  - `workspace.marker_data(root) -> dict`：所有权标记的内容，读不了或不是 JSON 对象返回 `{}`
  - `Workspace.assistant_only`（属性）：`agent.assistant_only is True`；Task 2 的 `RuntimeAgent` 给它赋值，普通 `Agent` 没有这个属性时为假

- [ ] **Step 1: 写失败测试**

`tests/unit/test_runtime_workspace_hostile.py`（新建）：

```python
"""助理写得到的工作目录：守护进程在沙箱外读写它时不跟随链接、不在 FIFO 上卡住、不越界。"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from runtime_daemon import workspace
from runtime_daemon.agent import Agent, OperationError


@pytest.fixture
def agent(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    return Agent(
        root=root,
        home=tmp_path / "home",
        api_url="https://example.com",
        token="synthetic-test-token-123456789",
        relay_id="test",
    )


def call(agent, kind, **kw):
    return agent.dispatch({"type": "workspace-" + kind, "working_dir": "one", "bot_id": "b1", **kw})


def finishes(fn, seconds: float = 5.0):
    """在线程里跑 `fn`：卡住（例如在 FIFO 上等写端）就判失败，而不是把整个测试进程挂住。"""
    out: dict = {}

    def run() -> None:
        try:
            out["value"] = fn()
        except Exception as exc:  # noqa: BLE001 交给断言
            out["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "blocked"
    if "error" in out:
        raise out["error"]
    return out.get("value")


def test_read_small_refuses_fifos_links_devices_and_large_files(tmp_path):
    regular = tmp_path / "ok.json"
    regular.write_bytes(b'{"bot_id": "b1"}')
    assert workspace.read_small(regular) == b'{"bot_id": "b1"}'
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert finishes(lambda: workspace.read_small(fifo)) is None
    link = tmp_path / "link"
    link.symlink_to(regular)
    assert workspace.read_small(link) is None
    assert workspace.read_small(Path("/dev/zero")) is None
    big = tmp_path / "big"
    big.write_bytes(b"x" * (workspace.MARKER_LIMIT + 1))
    assert workspace.read_small(big) is None
    assert workspace.read_small(tmp_path / "missing") is None


def test_a_fifo_marker_is_not_owned_and_does_not_block(agent):
    p = agent.root / "one"
    p.mkdir()
    os.mkfifo(p / workspace.MARKER)
    info = finishes(lambda: call(agent, "info"))
    assert info["owned"] is False and info["marked"] is True
    with pytest.raises(OperationError):
        finishes(lambda: call(agent, "init", content="x"))


@pytest.mark.parametrize("payload", [b"[]", b'"b1"', b"\xff\xfe", b'{"bot_id": "b2"}'])
def test_malformed_or_foreign_markers_are_not_owned(agent, payload):
    p = agent.root / "one"
    p.mkdir()
    (p / workspace.MARKER).write_bytes(payload)
    assert call(agent, "info")["owned"] is False


def test_assistant_only_init_never_reads_an_existing_directory(agent):
    agent.assistant_only = True
    first = call(agent, "init", content="# rules\n")
    assert first["initialized"] and call(agent, "info")["owned"]
    p = agent.root / "one"
    # 初始化做完之后助理在沙箱里改了说明文件：再初始化不读它（读目录会报错），原样返回。
    (p / "CLAUDE.md").unlink()
    (p / "CLAUDE.md").mkdir()
    again = call(agent, "init", content="# rules\n")
    assert again == {"success": True, "initialized": True, "owned": True, "workspace_protocol": 1}
    assert (p / "CLAUDE.md").is_dir()


def test_assistant_only_init_refuses_a_non_empty_unmarked_directory(agent):
    agent.assistant_only = True
    p = agent.root / "one"
    p.mkdir()
    (p / "notes.md").write_text("left behind")
    with pytest.raises(OperationError, match="全新的目录"):
        call(agent, "init", content="x")
    (p / "notes.md").unlink()
    assert call(agent, "init", content="x")["initialized"]


def test_ordinary_init_keeps_taking_over_existing_directories(agent):
    p = agent.root / "one"
    p.mkdir()
    (p / "notes.md").write_text("kept")
    assert call(agent, "init", content="x")["initialized"]
    assert (p / "notes.md").read_text() == "kept"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_runtime_workspace_hostile.py -q`
Expected: FAIL：`AttributeError: module 'runtime_daemon.workspace' has no attribute 'read_small'`；`test_a_fifo_marker_is_not_owned_and_does_not_block` 报 `blocked`（现在的 `owned()` 用 `read_text` 打开 FIFO，卡在等写端），`test_assistant_only_init_*` 两条失败。

- [ ] **Step 3: 实现**

`runtime_daemon/workspace.py`（常量之后加两个函数）：把

```python
MAX_DIFF = 256 * 1024
```

换成

```python
MAX_DIFF = 256 * 1024
# 所有权标记由守护进程写、不到 1 KB；读它时最多读这么多（目录里的东西可能被助理换掉）。
MARKER_LIMIT = 64 * 1024


def read_small(path, limit=MARKER_LIMIT):
    """读一个别人可能换掉的小文件，读不了或不像样一律返回 None。

    不跟随链接（O_NOFOLLOW），不在 FIFO、设备上等待（O_NONBLOCK 打开、fstat 只认普通文件），
    超过 `limit` 字节不读。打开之后核对的是同一个文件描述符，没有先查后用的窗口。
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                return None
    except OSError:
        return None
    finally:
        os.close(fd)


def marker_data(root):
    """工作目录里所有权标记的内容；没有、读不了、不是 JSON 对象都返回空字典。"""
    raw = read_small(Path(root) / MARKER)
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
```

`runtime_daemon/workspace.py`：把

```python

    def owned(self, root=None):
        marker = (root or self.root) / MARKER
        if marker.is_symlink():
            return False
        try:
            return json.loads(marker.read_text()).get("bot_id") == self.bot
        except (OSError, ValueError):
            return False

    def init(self, root=None):
        root = root or self.root
        root.mkdir(parents=True, exist_ok=True)
        marker = root / MARKER
```

换成

```python

    def owned(self, root=None):
        return marker_data(root or self.root).get("bot_id") == self.bot

    @property
    def assistant_only(self):
        return getattr(self.agent, "assistant_only", False) is True

    def init(self, root=None):
        root = root or self.root
        if self.assistant_only and root == self.root and root.exists():
            # 助理专用实例只在全新的目录上初始化，绝不读写助理在沙箱里写过的文件：标记是本员工的说明
            # 上一次初始化已经做完（标记最后写），原样返回；没有标记的非空目录一律拒绝。
            if self.owned(root):
                return {"initialized": True, "owned": True, "workspace_protocol": 1}
            if root.is_symlink() or not root.is_dir() or any(root.iterdir()):
                raise self.error("工作目录已存在，个人助理只能使用全新的目录")
        root.mkdir(parents=True, exist_ok=True)
        marker = root / MARKER
```

`runtime_daemon/workspace.py`：把

```python
    def finish(self):
        if self.owned():
            marker = json.loads((self.root / MARKER).read_text())
            if marker.get("transfer_id") and marker["transfer_id"] == self.data.get("transfer_id"):
                return {"installed": True}
```

换成

```python
    def finish(self):
        if self.owned():
            marker = marker_data(self.root)
            if marker.get("transfer_id") and marker["transfer_id"] == self.data.get("transfer_id"):
                return {"installed": True}
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_runtime_workspace_hostile.py tests/unit/test_runtime_workspace.py tests/unit/test_runtime_daemon.py -q`
Expected: 全部通过（新文件 9 条）。之后对这几个 `runtime_daemon/*.py` 跑 `uv run python -c "import ast,sys;[ast.parse(open(p).read(),feature_version=(3,10)) for p in sys.argv[1:]]" runtime_daemon/workspace.py`。

普通机器人不受影响：`test_ordinary_init_keeps_taking_over_existing_directories`（普通实例照旧接管已有目录、保留里面的文件），以及现有的 `tests/unit/test_runtime_workspace.py` 全部照常通过：`owned()` 对不超过 64 KB 的普通文件标记结果不变，`init` 的新分支只在 `assistant_only` 为真时走。

- [ ] **Step 5: 提交**

```bash
git add tests/unit/test_runtime_workspace_hostile.py runtime_daemon/workspace.py
git commit -m "个人助理：节点读工作目录的所有权标记不跟随链接、不在 FIFO 上卡住，助理专用实例只在全新目录上初始化"
```

---


### Task 2: 节点：删除助理工作目录的命令 `workspace-remove`、保留目录与回收站

**Files:**
- Modify: `runtime_daemon/workspace.py`（`HEALTH_DIR`、`TRASH_DIR`、`reserved`、`trash_dir`、`Workspace.remove`，`__init__` 与 `dispatch` 接上）
- Modify: `runtime_daemon/daemon.py`（名单加 `workspace-remove`；`assistant_only_refusal` 拒绝保留目录；`RuntimeAgent.assistant_only`）
- Modify: `runtime_daemon/assistant_turns.py`（在跑轮次的工作目录 `workdirs`、`in_use`；清扫时清回收站）
- Modify: `tests/unit/test_runtime_daemon_assistant.py`（名单用例）
- Test: `tests/unit/test_runtime_workspace_hostile.py`（追加）、`tests/unit/test_runtime_daemon_assistant_cleanup.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `marker_data`、`Workspace.assistant_only`；1E 的 `sandbox.remove_tree(path)`、`daemon.assistant_only_refusal`、`AssistantTurns.sweep`
- Produces:
  - 节点命令 `workspace-remove`，请求 `{"working_dir", "bot_id"}`，答复 `{"success": true, "removed": bool, "existed": bool, "busy"?: true}`：目录不在 → `removed: true, existed: false`；不带本记录标记 → `removed: false`；有沙箱轮次在用 → `busy: true`；删了 → `removed: true, existed: true`。只在助理专用实例上做，普通实例回「不支持的工作区操作」
  - `workspace.reserved(root, raw) -> bool`、`workspace.trash_dir(root) -> Path`、`workspace.HEALTH_DIR`、`workspace.TRASH_DIR`
  - `daemon.assistant_only_refusal(method, path, data, root=None) -> str | None`：新增代码 `reserved_directory`
  - `AssistantTurns.workdirs: dict[str, Path]`、`AssistantTurns.in_use(path) -> bool`、`AssistantTurns.purge_trash() -> None`

- [ ] **Step 1: 写失败测试**

`tests/unit/test_runtime_workspace_hostile.py`：把

```python
import threading
```

换成

```python
import threading
import time
```

`tests/unit/test_runtime_workspace_hostile.py` 末尾空两行之后追加：

```python
def test_remove_is_only_for_assistant_only_daemons(agent):
    call(agent, "init", content="x")
    with pytest.raises(OperationError):
        call(agent, "remove")
    assert (agent.root / "one").is_dir()


def wait_gone(path: Path) -> None:
    for _ in range(100):
        if not os.path.lexists(path):
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} still there")


def test_remove_moves_the_directory_into_the_trash_and_deletes_it_without_following_links(
    agent, tmp_path
):
    agent.assistant_only = True
    call(agent, "init", content="x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not the assistant's")
    p = agent.root / "one"
    (p / "escape").symlink_to(outside)
    (p / "deep" / "er").mkdir(parents=True)
    (p / "deep" / "er" / "file").write_text("x")
    (p / "deep" / "locked").mkdir()
    (p / "deep" / "locked").chmod(0)
    assert call(agent, "remove") == {"success": True, "removed": True, "existed": True}
    assert not os.path.lexists(p)
    trash = agent.root / workspace.TRASH_DIR
    assert trash.is_dir() and (trash.stat().st_mode & 0o777) == 0o700
    for entry in list(trash.iterdir()):
        wait_gone(entry)
    assert (outside / "keep.txt").read_text() == "not the assistant's"


def test_remove_reports_missing_and_foreign_directories(agent):
    agent.assistant_only = True
    assert call(agent, "remove") == {"success": True, "removed": True, "existed": False}
    p = agent.root / "one"
    p.mkdir()
    (p / "file").write_text("unmarked")
    assert call(agent, "remove") == {"success": True, "removed": False, "existed": True}
    (p / workspace.MARKER).write_text('{"bot_id": "someone-else"}')
    assert call(agent, "remove") == {"success": True, "removed": False, "existed": True}
    assert (p / "file").read_text() == "unmarked"


def test_remove_refuses_a_trash_that_is_a_link(agent, tmp_path):
    agent.assistant_only = True
    call(agent, "init", content="x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (agent.root / workspace.TRASH_DIR).symlink_to(elsewhere)
    with pytest.raises(OperationError):
        call(agent, "remove")
    assert (agent.root / "one").is_dir() and not any(elsewhere.iterdir())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (".coreman-health", True),
        (".coreman-health/x", True),
        (".coreman-trash/abc", True),
        ("bot-1", False),
        (".coreman-healthy", False),
        ("", False),
    ],
)
def test_reserved_directories(tmp_path, raw, expected):
    assert workspace.reserved(tmp_path, raw) is expected


def test_reserved_follows_links_to_their_real_place(tmp_path):
    (tmp_path / ".coreman-health").mkdir()
    (tmp_path / "innocent").symlink_to(tmp_path / ".coreman-health")
    assert workspace.reserved(tmp_path, "innocent") is True
    assert workspace.reserved(tmp_path, str(tmp_path / ".coreman-health")) is True


def test_remove_waits_while_a_sandboxed_turn_uses_the_directory(agent):
    from types import SimpleNamespace

    from runtime_daemon.assistant_turns import AssistantTurns

    agent.assistant_only = True
    call(agent, "init", content="x")
    turns = AssistantTurns(SimpleNamespace())
    agent.daemon = SimpleNamespace(assistant=turns)
    turns.workdirs["t1"] = (agent.root / "one").resolve()
    assert call(agent, "remove") == {
        "success": True,
        "removed": False,
        "existed": True,
        "busy": True,
    }
    assert (agent.root / "one").is_dir()
    turns.workdirs.clear()
    assert call(agent, "remove")["removed"] is True
```

`tests/unit/test_runtime_daemon_assistant_cleanup.py`（新建）：

```python
"""守护进程收拾助理留下的东西：保留目录不给助理用、回收站清扫、改回普通实例后的本轮目录。"""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path

import pytest

from runtime_daemon import agent as agent_module
from runtime_daemon import daemon as module
from runtime_daemon import workspace
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
    )
    return daemon


async def execute(daemon: Daemon, path: str, body: dict) -> list[dict]:
    sent: list[dict] = []

    async def send_frames(identity, frames):
        sent.extend(frames)

    daemon.send_frames = send_frames
    await daemon.execute(
        {
            "id": "call-1",
            "provider": "claude",
            "method": "POST",
            "path": path,
            "body": base64.b64encode(json.dumps(body).encode()).decode(),
        }
    )
    return sent


def code_of(frames: list[dict]) -> str:
    assert frames[0]["status_code"] == 403
    return json.loads(base64.b64decode(frames[0]["data"]))["error"]["code"]


SANDBOXED = {"sandbox": True, "transcript": {"transfer": "0" * 32, "restore": False}}


@pytest.mark.parametrize(
    "directory", [".coreman-health", ".coreman-health/sub", ".coreman-trash/x"]
)
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/", {"type": "workspace-init"}),
        ("/", {"type": "workspace-info"}),
        ("/", {"type": "workspace-remove"}),
        ("/v1/chat/completions", {**SANDBOXED, "messages": []}),
    ],
)
async def test_assistant_only_refuses_reserved_directories(
    node, monkeypatch, directory, path, body
):
    node.assistant_only = True

    def no_dispatch(data):
        raise AssertionError("refused before reaching the agent")

    async def no_run(*_args):
        raise AssertionError("refused before reaching the sandbox")

    monkeypatch.setattr(node.agents["claude"], "dispatch", no_dispatch)
    monkeypatch.setattr(node.assistant, "run", no_run)
    frames = await execute(node, path, {**body, "working_dir": directory, "bot_id": "b1"})
    assert code_of(frames) == "reserved_directory"


async def test_an_ordinary_daemon_does_not_check_reserved_directories(node, monkeypatch):
    seen: list = []

    def dispatch(data):
        seen.append(data["working_dir"])
        return {"success": True}

    monkeypatch.setattr(node.agents["claude"], "dispatch", dispatch)
    frames = await execute(node, "/", {"type": "workspace-info", "working_dir": ".coreman-health"})
    assert seen == [".coreman-health"] and frames[0]["status_code"] == 200


def test_the_allow_list_includes_the_directory_removal():
    assert "workspace-remove" in module.ASSISTANT_ONLY_OPERATIONS
    assert module.assistant_only_refusal("POST", "/", {"type": "workspace-remove"}) is None


def test_runtime_agents_carry_the_assistant_only_flag(node):
    node.config["node_token"] = "synthetic-node-token"
    node.assistant_only = True
    assert module.RuntimeAgent(node, "claude", "relay-1").assistant_only is True
    node.assistant_only = False
    assert module.RuntimeAgent(node, "claude", "relay-1").assistant_only is False


async def test_the_sweep_empties_the_trash_without_following_links(node, tmp_path):
    root = Path(node.config["workspace_root"])
    trash = workspace.trash_dir(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    (trash / "leftover" / "deep").mkdir(parents=True)
    (trash / "leftover" / "deep" / "f").write_text("x")
    (trash / "leftover" / "link").symlink_to(outside)
    (trash / "stray-link").symlink_to(outside)
    await node.assistant.sweep()
    assert list(trash.iterdir()) == []
    assert (outside / "keep").read_text() == "x"


async def test_the_sweep_leaves_a_trash_that_is_not_a_real_directory(node, tmp_path):
    root = Path(node.config["workspace_root"])
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "precious").mkdir(parents=True)
    (root / workspace.TRASH_DIR).symlink_to(elsewhere)
    await node.assistant.sweep()
    assert (elsewhere / "precious").is_dir()
```

`tests/unit/test_runtime_daemon_assistant.py`：把

```python
    "workspace-probe",
    "workspace-init",
    "seat-create",
    "seat-login-start",
```

换成

```python
    "workspace-probe",
    "workspace-init",
    "workspace-remove",
    "seat-create",
    "seat-login-start",
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_runtime_workspace_hostile.py tests/unit/test_runtime_daemon_assistant_cleanup.py tests/unit/test_runtime_daemon_assistant.py -q`
Expected: FAIL：`remove` 回「不支持的工作区操作」，`workspace.reserved`、`workspace.TRASH_DIR` 不存在，`reserved_directory` 拒绝没有发生，名单用例与 `assistant_only` 属性用例失败。

- [ ] **Step 3: 实现**

`runtime_daemon/workspace.py`：把

```python
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
```

换成

```python
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
```

`runtime_daemon/workspace.py`：把

```python
MARKER_LIMIT = 64 * 1024
```

换成

```python
MARKER_LIMIT = 64 * 1024
# 工作目录根下守护进程自己用的目录：健康探测目录、个人助理工作目录的回收站。助理专用实例拒绝把它们
# （及其下的任何目录）当作工作目录。
HEALTH_DIR = ".coreman-health"
TRASH_DIR = ".coreman-trash"
RESERVED_DIRS = (HEALTH_DIR, TRASH_DIR)
```

`runtime_daemon/workspace.py`：把

```python
def marker_data(root):
    """工作目录里所有权标记的内容；没有、读不了、不是 JSON 对象都返回空字典。"""
    raw = read_small(Path(root) / MARKER)
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


```

换成

```python
def marker_data(root):
    """工作目录里所有权标记的内容；没有、读不了、不是 JSON 对象都返回空字典。"""
    raw = read_small(Path(root) / MARKER)
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def reserved(root, raw):
    """`raw`（相对工作目录根或绝对路径）按真实位置算，是不是落在保留目录里（含保留目录本身）。"""
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return False
    base = Path(root).resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    resolved = path.resolve()
    return any(resolved.is_relative_to(base / name) for name in RESERVED_DIRS)


def trash_dir(root):
    """工作目录根下的回收站：没有就建（0700）；必须是本用户的真实目录，不是链接。"""
    path = Path(root) / TRASH_DIR
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError("trash directory is not ours")
    return path


```

`runtime_daemon/workspace.py`：把

```python
        marker = self.root / MARKER
        if (
            data.get("type") != "workspace-info"
            and (marker.exists() or marker.is_symlink())
            and not self.owned()
```

换成

```python
        marker = self.root / MARKER
        if (
            data.get("type") not in ("workspace-info", "workspace-remove")
            and (marker.exists() or marker.is_symlink())
            and not self.owned()
```

`runtime_daemon/workspace.py`：把

```python
            return {"removed": True}
        raise self.error("不支持的探测操作")

    def list(self):
```

换成

```python
            return {"removed": True}
        raise self.error("不支持的探测操作")

    def remove(self):
        """个人助理停用或换走之后删掉它的工作目录。只在助理专用实例上做，只删带本员工标记的目录。

        先把目录原子地改名进工作目录根下的回收站（同一文件系统，一步完成，不占操作锁多久），再在
        后台线程里删：不跟随任何链接，删不掉的留给助理侧的清扫（启动时与之后每 10 分钟）。目录
        已经不在算删完；不是本员工的（或没有标记）不动，如实告诉平台；还有在跑的沙箱轮次用着它
        时也不动（`busy`），平台稍后再来。
        """
        if not self.assistant_only:
            raise self.error("不支持的工作区操作")
        if not os.path.lexists(self.root):
            return {"removed": True, "existed": False}
        if self.root.is_symlink() or not self.root.is_dir() or not self.owned():
            return {"removed": False, "existed": True}
        turns = getattr(getattr(self.agent, "daemon", None), "assistant", None)
        if turns is not None and turns.in_use(self.root):
            # 还有沙箱里的轮次挂着这个目录：现在删，它还能往回收站里写。等它结束再来。
            return {"removed": False, "existed": True, "busy": True}
        from .sandbox import remove_tree

        target = trash_dir(self.agent.root) / secrets.token_hex(16)
        os.rename(self.root, target)
        threading.Thread(target=remove_tree, args=(target,), daemon=True).start()
        return {"removed": True, "existed": True}

    def list(self):
```

`runtime_daemon/workspace.py`：把

```python
            "git_diff",
            "probe",
        }:
            raise self.error("不支持的工作区操作")
```

换成

```python
            "git_diff",
            "probe",
            "remove",
        }:
            raise self.error("不支持的工作区操作")
```

`runtime_daemon/daemon.py`：把

```python
from runtime_daemon.tls import build_trust_bundle, client_context
```

换成

```python
from runtime_daemon.tls import build_trust_bundle, client_context
from runtime_daemon.workspace import reserved
```

`runtime_daemon/daemon.py`：把

```python

# 助理专用实例（config.json 里 assistant_only 为 true）只接这些节点命令：平台对助理所在节点会发的
# 探活、状态、额度、席位，以及开通与换运行时用到的目录检查与初始化。其余命令（读写或执行工作目录里的
# 内容、导出内容、遍历所有工作目录）一律以固定代码拒绝。经驱动的读请求只留探活与模型列表。
ASSISTANT_ONLY_OPERATIONS = frozenset(
    {
```

换成

```python

# 助理专用实例（config.json 里 assistant_only 为 true）只接这些节点命令：平台对助理所在节点会发的
# 探活、状态、额度、席位，开通与换运行时用到的目录检查与初始化，以及停用或换走之后删除助理的工作
# 目录。其余命令（读写或执行工作目录里的内容、导出内容、遍历所有工作目录）一律以固定代码拒绝。
# 经驱动的读请求只留探活与模型列表。
ASSISTANT_ONLY_OPERATIONS = frozenset(
    {
```

`runtime_daemon/daemon.py`：把

```python
        "workspace-probe",
        "workspace-init",
        *SEAT_OPERATIONS,
    }
```

换成

```python
        "workspace-probe",
        "workspace-init",
        "workspace-remove",
        *SEAT_OPERATIONS,
    }
```

`runtime_daemon/daemon.py`：把

```python


def assistant_only_refusal(method: str, path: str, data: dict) -> str | None:
    """助理专用实例对一条请求的裁决：不接就返回固定代码。

    聊天请求必须带 `sandbox: true`（只在沙箱里跑）；节点命令只接名单上的；经驱动的读请求只留
    探活与模型列表。
    """
    if path == "/v1/chat/completions":
        return None if data.get("sandbox") is True else "assistant_only"
    if path == "/":
        return None if data.get("type") in ASSISTANT_ONLY_OPERATIONS else "assistant_only"
    return None if (method, path) in ASSISTANT_ONLY_READS else "assistant_only"


```

换成

```python


def assistant_only_refusal(
    method: str, path: str, data: dict, root: Path | None = None
) -> str | None:
    """助理专用实例对一条请求的裁决：不接就返回固定代码。

    聊天请求必须带 `sandbox: true`（只在沙箱里跑）；节点命令只接名单上的；经驱动的读请求只留
    探活与模型列表。给了工作目录根时，工作目录落在保留目录（健康探测目录、回收站）里的一律拒绝：
    沙箱里的助理能写自己的工作目录，探测在沙箱外跑。
    """
    if path == "/v1/chat/completions":
        refused = None if data.get("sandbox") is True else "assistant_only"
    elif path == "/":
        refused = None if data.get("type") in ASSISTANT_ONLY_OPERATIONS else "assistant_only"
    else:
        return None if (method, path) in ASSISTANT_ONLY_READS else "assistant_only"
    if refused is None and root is not None and reserved(root, data.get("working_dir")):
        return "reserved_directory"
    return refused


```

`runtime_daemon/daemon.py`：把

```python
        # 助理专用实例不跑遍历所有工作目录的定时任务（回收记忆、刷新 worktree）。
        self.workspace_jobs = not daemon.assistant_only
        # 服务端在心跳里给的检查模型（模型目录的默认模型）。驱动发现的列表只是默认值，
        # 第一个未必是账号能用的（如 ChatGPT 账号用不了 gpt-5.5）。
```

换成

```python
        # 助理专用实例不跑遍历所有工作目录的定时任务（回收记忆、刷新 worktree）。
        self.workspace_jobs = not daemon.assistant_only
        # 工作目录的删除（停用助理）只在助理专用实例上做，见 workspace.Workspace.remove。
        self.assistant_only = daemon.assistant_only
        # 服务端在心跳里给的检查模型（模型目录的默认模型）。驱动发现的列表只是默认值，
        # 第一个未必是账号能用的（如 ChatGPT 账号用不了 gpt-5.5）。
```

`runtime_daemon/daemon.py`：把

```python
        data = json.loads(raw) if raw else {}
        if self.assistant_only:
            refused = assistant_only_refusal(method, path, data)
            if refused is not None:
                raise AssistantOnlyRefused(refused)
```

换成

```python
        data = json.loads(raw) if raw else {}
        if self.assistant_only:
            refused = assistant_only_refusal(
                method, path, data, Path(self.config["workspace_root"])
            )
            if refused is not None:
                raise AssistantOnlyRefused(refused)
```

`runtime_daemon/assistant_turns.py`：把

```python
import httpx

from runtime_daemon import egress, sandbox, transcripts, turn_credentials

LOG = logging.getLogger("coreman-runtime")
```

换成

```python
import httpx

from runtime_daemon import egress, sandbox, transcripts, turn_credentials, workspace

LOG = logging.getLogger("coreman-runtime")
```

`runtime_daemon/assistant_turns.py`：把

```python
        self.code = "not_tested"
        self.active: set[str] = set()
        self.loop_task: asyncio.Task | None = None
        self.background: set[asyncio.Task] = set()
```

换成

```python
        self.code = "not_tested"
        self.active: set[str] = set()
        # 在跑的轮次各自的工作目录（主机上的真实路径）：删目录前核对没有轮次正在用它。
        self.workdirs: dict[str, Path] = {}
        self.loop_task: asyncio.Task | None = None
        self.background: set[asyncio.Task] = set()
```

`runtime_daemon/assistant_turns.py`：把

```python
            finally:
                await asyncio.to_thread(sandbox.remove_tree, entry)

    def _leftovers(
```

换成

```python
            finally:
                await asyncio.to_thread(sandbox.remove_tree, entry)
        await asyncio.to_thread(self.purge_trash)

    def purge_trash(self) -> None:
        """回收站里还没删完的助理工作目录（删除线程随守护进程退出而中断）：不跟随链接地删掉。

        回收站不是本用户的真实目录就不碰；每一项单独兜底。
        """
        trash = Path(self.daemon.config["workspace_root"]) / workspace.TRASH_DIR
        try:
            info = os.lstat(trash)
        except OSError:
            return
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return
        with os.scandir(trash) as entries:
            found = list(entries)
        for entry in found:
            try:
                if entry.is_dir(follow_symlinks=False):
                    sandbox.remove_tree(Path(entry.path))
                else:
                    os.unlink(entry.path)
            except OSError as exc:
                LOG.warning("Assistant trash entry failed: %s", type(exc).__name__)

    def _leftovers(
```

`runtime_daemon/assistant_turns.py`：把

```python
        await frames.finish()

    def _admission(self, provider: str) -> str | None:
        if provider not in sandbox.PROVIDERS or not self.capability(provider):
```

换成

```python
        await frames.finish()

    def in_use(self, path: Path) -> bool:
        """有没有在跑的轮次用着这个目录（或它下面的目录）。可以在线程里调用：先整体复制再看。"""
        return any(w == path or w.is_relative_to(path) for w in self.workdirs.copy().values())

    def _admission(self, provider: str) -> str | None:
        if provider not in sandbox.PROVIDERS or not self.capability(provider):
```

`runtime_daemon/assistant_turns.py`：把

```python
        layout = sandbox.TurnLayout.new(self.turns_dir, self.daemon.socket_dir)
        self.active.add(layout.turn_id)
        self.daemon.task_info[identity] = {
            "request_id": identity,
```

换成

```python
        layout = sandbox.TurnLayout.new(self.turns_dir, self.daemon.socket_dir)
        self.active.add(layout.turn_id)
        self.workdirs[layout.turn_id] = plan.workdir
        self.daemon.task_info[identity] = {
            "request_id": identity,
```

`runtime_daemon/assistant_turns.py`：把

```python
        finally:
            self.active.discard(layout.turn_id)

    @staticmethod
```

换成

```python
        finally:
            self.active.discard(layout.turn_id)
            self.workdirs.pop(layout.turn_id, None)

    @staticmethod
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_runtime_workspace_hostile.py tests/unit/test_runtime_daemon_assistant_cleanup.py tests/unit/test_runtime_daemon_assistant.py tests/unit/test_assistant_turns.py tests/unit/test_runtime_workspace.py tests/unit/test_runtime_daemon.py -q`
Expected: 全部通过。三个改过的 `runtime_daemon/*.py` 按 Python 3.10 语法解析通过（同 Task 1 的命令）。

普通机器人不受影响：`test_an_ordinary_daemon_does_not_check_reserved_directories`（普通实例对 `.coreman-health` 的请求照常交给 Agent）、`test_remove_is_only_for_assistant_only_daemons`；`tests/unit/test_runtime_daemon_assistant.py` 里普通轮次与普通实例的用例照常通过。

- [ ] **Step 5: 提交**

```bash
git add tests/unit/test_runtime_workspace_hostile.py tests/unit/test_runtime_daemon_assistant_cleanup.py tests/unit/test_runtime_daemon_assistant.py runtime_daemon/workspace.py runtime_daemon/daemon.py runtime_daemon/assistant_turns.py
git commit -m "个人助理：节点命令 workspace-remove 删助理工作目录（只删带标记的、先挪进回收站），保留目录不给助理用"
```

---


### Task 3: 节点：改回普通实例后删掉助理留下的本轮目录

**Files:**
- Modify: `runtime_daemon/daemon.py`（`start_assistant`、新增 `clear_assistant_leftovers`）
- Test: `tests/unit/test_runtime_daemon_assistant_cleanup.py`（追加）

**Interfaces:**
- Consumes: 1E 的 `Daemon.start_assistant`、`AssistantTurns.turns_dir`、`sandbox.remove_tree`
- Produces: `Daemon.clear_assistant_leftovers() -> None`（在线程里调用，不抛）

- [ ] **Step 1: 写失败测试**

`tests/unit/test_runtime_daemon_assistant_cleanup.py`：把

```python
import json
import tempfile
```

换成

```python
import json
import os
import tempfile
import time
```

`tests/unit/test_runtime_daemon_assistant_cleanup.py` 末尾空两行之后追加：

```python
def wait_until(check) -> None:
    for _ in range(100):
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_an_ordinary_daemon_clears_assistant_turn_leftovers_once(node, tmp_path):
    turns = node.assistant.turns_dir
    (turns / "abc" / "config").mkdir(parents=True)
    (turns / "abc" / "config" / "transcript.jsonl").write_text("plaintext")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    (turns / "abc" / "link").symlink_to(outside)
    node.start_assistant()
    wait_until(lambda: not os.path.lexists(turns))
    assert (outside / "keep").read_text() == "x"


def test_an_ordinary_daemon_unlinks_a_linked_turns_directory(node, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    turns = node.assistant.turns_dir
    turns.parent.mkdir(parents=True, exist_ok=True)
    turns.symlink_to(outside)
    node.start_assistant()
    wait_until(lambda: not os.path.lexists(turns))
    assert (outside / "keep").read_text() == "x"


def test_a_failing_cleanup_is_contained(node, monkeypatch):
    def boom(_path):
        raise RuntimeError("disk on fire")

    (node.assistant.turns_dir / "x").mkdir(parents=True)
    monkeypatch.setattr(module, "remove_tree", boom)
    node.clear_assistant_leftovers()  # 不抛
    assert node.assistant.turns_dir.is_dir()


def test_an_assistant_only_daemon_does_not_clear_its_own_turns(node, monkeypatch):
    node.assistant_only = True
    started: list = []
    monkeypatch.setattr(node.assistant, "start", lambda: started.append(True))
    (node.assistant.turns_dir / "live").mkdir(parents=True)
    node.start_assistant()
    time.sleep(0.2)
    assert started == [True] and (node.assistant.turns_dir / "live").is_dir()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_runtime_daemon_assistant_cleanup.py -q`
Expected: FAIL：`test_an_ordinary_daemon_clears_assistant_turn_leftovers_once` 等超时（`timed out`），`test_a_failing_cleanup_is_contained` 报 `AttributeError: ... 'clear_assistant_leftovers'`。

- [ ] **Step 3: 实现**

`runtime_daemon/daemon.py`：把

```python
from runtime_daemon.sandbox import CAPABILITY as SANDBOX_CAPABILITY
```

换成

```python
from runtime_daemon.sandbox import CAPABILITY as SANDBOX_CAPABILITY
from runtime_daemon.sandbox import remove_tree
```

`runtime_daemon/daemon.py`：把

```python
    def start_assistant(self) -> None:
        if not self.assistant_only:
            return
        try:
```

换成

```python
    def start_assistant(self) -> None:
        if not self.assistant_only:
            # 从助理专用实例改回普通实例的节点：本轮目录里可能还有没传上去的明文转录，删一次。
            threading.Thread(target=self.clear_assistant_leftovers, daemon=True).start()
            return
        try:
```

`runtime_daemon/daemon.py`：把

```python
        except Exception as exc:  # noqa: BLE001 助理侧起不来，普通轮次照常
            LOG.warning("Assistant sandbox start failed: %s", type(exc).__name__)

    async def stop_assistant(self) -> None:
```

换成

```python
        except Exception as exc:  # noqa: BLE001 助理侧起不来，普通轮次照常
            LOG.warning("Assistant sandbox start failed: %s", type(exc).__name__)

    def clear_assistant_leftovers(self) -> None:
        """普通实例启动时删掉助理的本轮目录（不跟随链接）；在线程里跑，失败只记一笔。"""
        try:
            turns = self.assistant.turns_dir
            if turns.is_symlink():
                turns.unlink()
            elif turns.is_dir():
                LOG.warning("Removing assistant turn leftovers")
                remove_tree(turns)
        except Exception as exc:  # noqa: BLE001 清不掉不能影响普通轮次
            LOG.warning("Assistant leftovers cleanup failed: %s", type(exc).__name__)

    async def stop_assistant(self) -> None:
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_runtime_daemon_assistant_cleanup.py tests/unit/test_runtime_daemon_assistant.py tests/unit/test_runtime_daemon_lifecycle.py -q`
Expected: 全部通过；`runtime_daemon/daemon.py` 按 3.10 语法解析通过。

普通机器人不受影响：普通实例启动时只多起一个线程，删的是只有助理专用实例才写的 `assistant-turns/`；`test_an_ordinary_daemon_runs_no_assistant_background_work`（现有，同步调用 `start_assistant`，不需要事件循环）照常通过，`test_a_failing_cleanup_is_contained` 证明清理出错不外抛。

- [ ] **Step 5: 提交**

```bash
git add tests/unit/test_runtime_daemon_assistant_cleanup.py runtime_daemon/daemon.py
git commit -m "个人助理：守护进程改回普通实例后，启动时删掉助理留下的本轮目录"
```

---


### Task 4: 平台：换运行时后首轮排队超时，下一轮照样带上下文说明

**Files:**
- Modify: `coreman/core/assistants/transcripts.py`（`prepare`）
- Test: `tests/integration/test_assistant_transcript_carry.py`（新建）

**Interfaces:**
- Consumes: 1E 的 `transcripts.prepare` / `release` / `upload`、`sessions.carry_over`
- Produces: 无新接口；`prepare` 在会话带着 `inherited_from` 且转录行交接已了结、没有内容（`pending_transfer` 为空、`size == 0`）时，与没有转录行同样处理：不还原、不换新会话

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_transcript_carry.py`（新建）：

```python
"""换运行时后的首轮排队超时：交接编号作废之后，下一轮照样带上平台保存的上下文说明。"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys, sealed, transcripts
from coreman.core.chat import sessions
from coreman.core.db.models import AssistantTranscript
from tests.fakes.runtime_node import add_node
from tests.integration.assistant_helpers import seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
KEY = "oc_owner"


async def turn(session: AsyncSession, bot, node_id):
    info = await sessions.get_or_create(
        session,
        bot_id=bot.id,
        session_key=KEY,
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    out = await transcripts.prepare(
        session,
        bot=bot,
        node_id=node_id,
        info=info,
        session_key=KEY,
        backend="claude",
        speaker_user_id=None,
    )
    await session.commit()
    return out


async def switched(session: AsyncSession):
    bot, owner = await seed_assistant(session)
    await keys.ensure_key(session, sealed.master(), owner.id)
    node = await add_node(session)
    first, plan = await turn(session, bot, node.id)
    await transcripts.upload(
        session, node_id=node.id, transfer=plan.transfer, status="ok", data=b"\x1f\x8bold"
    )
    await sessions.carry_over(session, bot.id, backend="claude", keep=set())
    await session.commit()
    return bot, node, first


async def test_a_released_first_turn_keeps_the_carried_context(db_session: AsyncSession):
    bot, node, first = await switched(db_session)
    info, plan = await turn(db_session, bot, node.id)
    assert info.inherited_from == first.relay_session_id and plan.restore is False
    # 排队超时、确定没有节点领到：编号作废，行留下（没有内容）。
    assert await transcripts.release(
        db_session, bot_id=bot.id, session_id=info.relay_session_id, transfer=plan.transfer
    )
    await db_session.commit()
    again, plan2 = await turn(db_session, bot, node.id)
    assert again.relay_session_id == info.relay_session_id
    assert again.inherited_from == first.relay_session_id
    assert plan2.restore is False


async def test_an_unreturned_first_turn_still_starts_over(db_session: AsyncSession):
    """首轮派出了、转录没传回来（编号还在）：照旧不续、换新会话，也不再带续接。"""
    bot, node, _ = await switched(db_session)
    info, _plan = await turn(db_session, bot, node.id)
    after, plan2 = await turn(db_session, bot, node.id)
    assert after.relay_session_id != info.relay_session_id and after.inherited_from is None
    assert plan2.restore is False


async def test_a_stored_transcript_is_restored_not_carried(db_session: AsyncSession):
    bot, node, _ = await switched(db_session)
    info, plan = await turn(db_session, bot, node.id)
    await transcripts.upload(
        db_session, node_id=node.id, transfer=plan.transfer, status="ok", data=b"\x1f\x8bnew"
    )
    await db_session.commit()
    again, plan2 = await turn(db_session, bot, node.id)
    assert again.relay_session_id == info.relay_session_id and plan2.restore is True
    row = await db_session.get(AssistantTranscript, (bot.id, info.relay_session_id))
    assert row is not None and row.pending_transfer == plan2.transfer
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_transcript_carry.py -q`
Expected: FAIL：`test_a_released_first_turn_keeps_the_carried_context` —— 第二次 `prepare` 换了新会话、`inherited_from` 变成 None。

- [ ] **Step 3: 实现**

`coreman/core/assistants/transcripts.py`：把

```python
    续得上的条件：不是新会话、库里有上一轮的转录、那一轮的交接已办完、数据密钥可用。
    续不上又不是新会话时换一个新的 relay 会话：驱动拿着旧会话号续，会在节点上悄悄从空会话开始，
    换新的至少让这一轮明明白白是新会话。例外是换运行时后的首轮（没有转录行、会话带着
    `inherited_from`）：这个会话号是换运行时时新起的、从没派出过，节点上没有可续的旧状态，
    原样用它（不还原），平台的上下文说明才带得进这一轮。
    """
    restore = False
```

换成

```python
    续得上的条件：不是新会话、库里有上一轮的转录、那一轮的交接已办完、数据密钥可用。
    续不上又不是新会话时换一个新的 relay 会话：驱动拿着旧会话号续，会在节点上悄悄从空会话开始，
    换新的至少让这一轮明明白白是新会话。例外是换运行时后的首轮（会话带着 `inherited_from`，
    且没有转录行，或者只有一行交接已了结、没有内容的——首轮排队超时被 `release` 作废编号后就是
    这样）：节点上没有可续的旧状态，原样用这个会话号（不还原），平台的上下文说明才带得进这一轮。
    """
    restore = False
```

`coreman/core/assistants/transcripts.py`：把

```python
            and await sealed.key_ready(session, bot.id)
        )
        carried = row is None and info.inherited_from is not None
        if not restore and not carried:
            await session.execute(
```

换成

```python
            and await sealed.key_ready(session, bot.id)
        )
        carried = info.inherited_from is not None and (
            row is None or (row[0] is None and row[1] == 0)
        )
        if not restore and not carried:
            await session.execute(
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_transcript_carry.py tests/integration/test_assistant_transcripts.py tests/integration/test_assistant_turn_dispatch.py -q`
Expected: 全部通过。

普通机器人不受影响：`prepare` 只被助理的开轮调用（`worker/chat/opening.py` 里 `is_assistant(bot)` 分支）；普通员工的开轮不经过它，`tests/integration/test_chat_handler.py` 照常通过。

- [ ] **Step 5: 提交**

```bash
git add tests/integration/test_assistant_transcript_carry.py coreman/core/assistants/transcripts.py
git commit -m "个人助理：换运行时后首轮排队超时，下一轮照样带上平台保存的上下文说明"
```

---


### Task 5: 开通状态与流程表：`bots.assistant_state`、`assistant_lifecycles`、`assistant_retired_dirs`；只有 active 能跑轮次

**Files:**
- Create: `migrations/versions/0092_assistant_lifecycle.py`
- Modify: `coreman/core/db/models/assistants.py`（末尾追加两个模型与常量）、`coreman/core/db/models/__init__.py`（导出）、`coreman/core/db/models/bots.py`（列与检查约束）
- Modify: `coreman/core/assistants/records.py`（`LOCKED_FIELDS`、`new_assistant_bot` 的初始状态）
- Modify: `coreman/core/assistants/runtime.py`（`turn_blocker` 先看状态；`LIFECYCLE_MESSAGES`；`pause_message`）
- Modify: `tests/conftest.py`（`BUSINESS_TABLES`）、`tests/integration/assistant_helpers.py`（造出来的助理是 active）
- Test: `tests/integration/test_assistant_record_states.py`（新建）

**Interfaces:**
- Consumes: 1A 的 `new_assistant_bot`、`LOCKED_FIELDS`；1E 的 `runtime.turn_blocker`、`pause_message`
- Produces:
  - 列 `bots.assistant_state`（`provisioning` / `active` / `deprovisioning` / `deprovisioned`，普通记录为空）；`ASSISTANT_STATES`
  - 模型 `AssistantLifecycle`（`assistant_lifecycles`）、`AssistantRetiredDir`（`assistant_retired_dirs`）；常量 `LIFECYCLE_ACTIONS`、`LIFECYCLE_STEPS`、`LIFECYCLE_TRIGGERS`、`LIFECYCLE_OUTCOMES`
  - `runtime.turn_blocker` 新增原因 `provisioning`、`deprovisioning`；`runtime.LIFECYCLE_MESSAGES`
  - `new_assistant_bot(...)` 建出的记录 `assistant_state == "provisioning"`

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_record_states.py`（新建）：

```python
"""助理记录的开通状态：只有 active 能跑轮次；新建的记录从 provisioning 开始；普通记录没有这一列。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import runtime
from coreman.core.assistants.records import LOCKED_FIELDS, locked_changes
from tests.integration.assistant_helpers import seed_assistant, seed_standard


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (None, "provisioning"),
        ("provisioning", "provisioning"),
        ("deprovisioning", "deprovisioning"),
        ("deprovisioned", "deprovisioning"),
    ],
)
async def test_only_an_active_assistant_can_run_a_turn(
    db_session: AsyncSession, state: str | None, reason: str
) -> None:
    bot, _ = await seed_assistant(db_session)
    bot.assistant_state = state
    assert await runtime.turn_blocker(db_session, bot) == reason
    assert "暂停" not in runtime.pause_message(reason)


async def test_an_active_assistant_goes_on_to_the_runtime_checks(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    assert bot.assistant_state == "active"
    # 没绑运行时：落到原有的运行时检查。
    assert await runtime.turn_blocker(db_session, bot) == "runtime_missing"


async def test_new_records_start_as_provisioning_and_the_state_is_locked(
    db_session: AsyncSession,
) -> None:
    from coreman.core.assistants.records import new_assistant_bot
    from coreman.core.db.models import PlatformApp, User

    owner = User(login_name="zhangsan", display_name="张三")
    app = PlatformApp(
        platform="feishu", name="个人助理", capabilities=["assistant"], secret_enc="x"
    )
    db_session.add_all([owner, app])
    await db_session.flush()
    bot = new_assistant_bot(
        owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc=""
    )
    assert bot.assistant_state == "provisioning"
    assert "assistant_state" in LOCKED_FIELDS
    assert locked_changes(bot, {"assistant_state": "active"}) == ["assistant_state"]


async def test_ordinary_bots_cannot_carry_a_state(db_session: AsyncSession) -> None:
    standard = await seed_standard(db_session)
    assert standard.assistant_state is None
    standard.assistant_state = "active"
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_one_unfinished_lifecycle_per_person(db_session: AsyncSession) -> None:
    from coreman.core.db.models import AssistantLifecycle
    from coreman.core.timeutils import utcnow

    bot, owner = await seed_assistant(db_session)
    app_id, owner_id = bot.platform_app_id, owner.id
    assert app_id is not None

    def flow(**kw: object) -> AssistantLifecycle:
        values: dict[str, object] = {"action": "provision", "step": "offered", **kw}
        return AssistantLifecycle(owner_id=owner_id, platform_app_id=app_id, **values)

    db_session.add(flow(finished_at=utcnow(), outcome="expired"))
    db_session.add(flow())
    await db_session.flush()
    db_session.add(flow(action="deprovision", step="drain"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
    db_session.add(flow(step="nowhere"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
```

`tests/conftest.py`：把

```python
ROOT = Path(__file__).resolve().parents[1]
BUSINESS_TABLES = [
    "assistant_transcripts",
    "assistant_approvals",
```

换成

```python
ROOT = Path(__file__).resolve().parents[1]
BUSINESS_TABLES = [
    "assistant_lifecycles",
    "assistant_retired_dirs",
    "assistant_transcripts",
    "assistant_approvals",
```

`tests/integration/assistant_helpers.py`：把

```python
        owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc=""
    )
    session.add(bot)
    await session.commit()
```

换成

```python
        owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc=""
    )
    # 各功能的用例要的是一位已经开通好的助理（开通流程本身见 test_assistant_provisioning）。
    bot.assistant_state = "active"
    session.add(bot)
    await session.commit()
```

`tests/integration/assistant_helpers.py`：把

```python
    )
    bot.team_id = None
    for name, value in ASSISTANT_FIXED.items():
        setattr(bot, name, value)
```

换成

```python
    )
    bot.team_id = None
    bot.assistant_state = "active"
    for name, value in ASSISTANT_FIXED.items():
        setattr(bot, name, value)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_record_states.py -q`
Expected: FAIL：`ImportError`（`AssistantLifecycle`）、`AttributeError: 'Bot' object has no attribute 'assistant_state'`。

- [ ] **Step 3: 实现**

`migrations/versions/0092_assistant_lifecycle.py`（新建）：

```python
"""Personal assistant provisioning and deprovisioning: record state, lifecycles, retired dirs."""

import sqlalchemy as sa
from alembic import op

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None

STATE_CHECK = (
    "assistant_state IS NULL OR (kind = 'assistant' AND assistant_state IN "
    "('provisioning', 'active', 'deprovisioning', 'deprovisioned'))"
)
STEPS = (
    "step IN ('offered', 'create', 'init_workspace', 'start_grant', 'await_grant', 'grace',"
    " 'drain', 'withdraw', 'revoke', 'destroy')"
)


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    # 改的是在用的表，要拿排他锁：排在长事务后面会让之后对这张表的读写全部跟着排队。
    # 5 秒拿不到锁就报错，整次迁移回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("bots", sa.Column("assistant_state", sa.Text(), nullable=True))
    # 已有的助理记录（本功能上线前建的）按已开通处理；普通记录保持为空。
    op.execute("UPDATE bots SET assistant_state = 'active' WHERE kind = 'assistant'")
    op.create_check_constraint("assistant_state", "bots", STATE_CHECK)
    op.create_table(
        "assistant_lifecycles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_assistant_lifecycles_owner"),
            nullable=False,
        ),
        sa.Column(
            "platform_app_id",
            sa.Uuid(),
            sa.ForeignKey(
                "platform_apps.id", ondelete="CASCADE", name="fk_assistant_lifecycles_app"
            ),
            nullable=False,
        ),
        sa.Column(
            "bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="SET NULL", name="fk_assistant_lifecycles_bot"),
            nullable=True,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=True),
        sa.Column(
            "actor_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_assistant_lifecycles_actor"),
            nullable=True,
        ),
        sa.Column("platform_user_id", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("offer_hash", sa.Text(), nullable=True),
        sa.Column("offer_message_id", sa.Text(), nullable=True),
        sa.Column("offer_chat_id", sa.Text(), nullable=True),
        sa.Column("open_id", sa.Text(), nullable=True),
        sa.Column("tenant_key", sa.Text(), nullable=True),
        _ts("deadline", nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("issued", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("next_attempt_at", nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        _ts("created_at", server_default=sa.text("now()"), nullable=False),
        _ts("updated_at", server_default=sa.text("now()"), nullable=False),
        _ts("finished_at", nullable=True),
        sa.CheckConstraint("action IN ('provision', 'deprovision')", name="action"),
        sa.CheckConstraint(STEPS, name="step"),
        sa.CheckConstraint(
            "trigger IS NULL OR trigger IN ('owner', 'member_left', 'admin', 'abandoned')",
            name="trigger",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('done', 'expired', 'cancelled', 'failed',"
            " 'ineligible')",
            name="outcome",
        ),
    )
    op.create_index(
        "assistant_lifecycles_live_uq",
        "assistant_lifecycles",
        ["owner_id"],
        unique=True,
        postgresql_where=sa.text("finished_at IS NULL"),
    )
    op.create_index(
        "assistant_lifecycles_due_idx",
        "assistant_lifecycles",
        ["next_attempt_at"],
        postgresql_where=sa.text("finished_at IS NULL"),
    )
    op.create_index("assistant_lifecycles_bot_idx", "assistant_lifecycles", ["bot_id"])
    op.create_table(
        "assistant_retired_dirs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column(
            "relay_server_id",
            sa.Uuid(),
            sa.ForeignKey(
                "relay_servers.id", ondelete="CASCADE", name="fk_assistant_retired_dirs_relay"
            ),
            nullable=False,
        ),
        sa.Column("working_dir", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("next_attempt_at", server_default=sa.text("now()"), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        _ts("created_at", server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint(
            "relay_server_id", "working_dir", name="uq_assistant_retired_dirs_path"
        ),
    )
    op.create_index("assistant_retired_dirs_due_idx", "assistant_retired_dirs", ["next_attempt_at"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("assistant_retired_dirs_due_idx", table_name="assistant_retired_dirs")
    op.drop_table("assistant_retired_dirs")
    op.drop_index("assistant_lifecycles_bot_idx", table_name="assistant_lifecycles")
    op.drop_index("assistant_lifecycles_due_idx", table_name="assistant_lifecycles")
    op.drop_index("assistant_lifecycles_live_uq", table_name="assistant_lifecycles")
    op.drop_table("assistant_lifecycles")
    op.drop_constraint("assistant_state", "bots", type_="check")
    op.drop_column("bots", "assistant_state")
```

`coreman/core/db/models/assistants.py` 末尾空两行之后追加：

```python
ASSISTANT_STATES = ("provisioning", "active", "deprovisioning", "deprovisioned")
LIFECYCLE_ACTIONS = ("provision", "deprovision")
LIFECYCLE_STEPS = (
    # 开通：发了开通卡片等本人点 → 建记录与密钥、选节点 → 节点上建目录 → 发起飞书授权 → 等授权完成
    "offered",
    "create",
    "init_workspace",
    "start_grant",
    "await_grant",
    # 停用：（成员离开时）宽限 → 停下在跑的轮次 → 撤回待批 → 撤销授权 → 销毁密钥并停用记录
    "grace",
    "drain",
    "withdraw",
    "revoke",
    "destroy",
)
LIFECYCLE_TRIGGERS = ("owner", "member_left", "admin", "abandoned")
LIFECYCLE_OUTCOMES = ("done", "expired", "cancelled", "failed", "ineligible")


class AssistantLifecycle(Base):
    """一次开通或停用：做到哪一步、下一次什么时候接着做、出过什么错（只有代码）。

    一人同时只有一条没结束的（`finished_at` 为空）：开通没走完不能再点开通，停用没走完不能重新开通
    （新的开通会签发新一代密钥，停用最后一步要销毁的是旧的那一代）。看护进程按 `next_attempt_at`
    一步一步推进，每一步一个事务；任何一步失败都停在原地等下次重试，不会留下能跑轮次的半成品。
    """

    __tablename__ = "assistant_lifecycles"
    __table_args__ = (
        CheckConstraint(enum_check("action", LIFECYCLE_ACTIONS), name="action"),
        CheckConstraint(enum_check("step", LIFECYCLE_STEPS), name="step"),
        CheckConstraint(
            f"trigger IS NULL OR {enum_check('trigger', LIFECYCLE_TRIGGERS)}", name="trigger"
        ),
        CheckConstraint(
            f"outcome IS NULL OR {enum_check('outcome', LIFECYCLE_OUTCOMES)}", name="outcome"
        ),
        Index(
            "assistant_lifecycles_live_uq",
            "owner_id",
            unique=True,
            postgresql_where=text("finished_at IS NULL"),
        ),
        Index(
            "assistant_lifecycles_due_idx",
            "next_attempt_at",
            postgresql_where=text("finished_at IS NULL"),
        ),
        Index("assistant_lifecycles_bot_idx", "bot_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE", name="fk_assistant_lifecycles_owner")
    )
    platform_app_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("platform_apps.id", ondelete="CASCADE", name="fk_assistant_lifecycles_app")
    )
    bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="SET NULL", name="fk_assistant_lifecycles_bot")
    )
    action: Mapped[str] = mapped_column(Text)
    step: Mapped[str] = mapped_column(Text)
    trigger: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL", name="fk_assistant_lifecycles_actor")
    )
    # 开通卡片：发给谁（飞书 user_id）、卡片令牌的摘要（点过即清）、卡片所在的消息与私聊。
    platform_user_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    offer_hash: Mapped[str | None] = mapped_column(Text)
    offer_message_id: Mapped[str | None] = mapped_column(Text)
    offer_chat_id: Mapped[str | None] = mapped_column(Text)
    # 点开通的人在飞书里的身份（来自卡片回调里飞书给的操作人，不来自卡片内容），授权要核对它。
    open_id: Mapped[str | None] = mapped_column(Text)
    tenant_key: Mapped[str | None] = mapped_column(Text)
    # 这一步的期限（卡片过期、等授权、等轮次停下、成员离开的宽限）。
    deadline: Mapped[datetime | None] = _ts()
    # 本步骤连续失败的次数（进入下一步时清零）；发过几次授权链接。
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    issued: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = _ts()
    error_code: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
    updated_at: Mapped[datetime] = _ts(server_default=text("now()"))
    finished_at: Mapped[datetime | None] = _ts()


class AssistantRetiredDir(Base):
    """要从节点上删掉的助理工作目录：停用时的当前目录、换运行时换走的旧目录。

    看护进程向目录所在实例的节点发 `workspace-remove`，节点只删带这位助理标记的目录；节点离线就
    隔一段时间再试，直到删掉或节点答复「不是它的」。不引用 bots：记录删了目录也要删。
    """

    __tablename__ = "assistant_retired_dirs"
    __table_args__ = (
        UniqueConstraint("relay_server_id", "working_dir", name="uq_assistant_retired_dirs_path"),
        Index("assistant_retired_dirs_due_idx", "next_attempt_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    relay_server_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("relay_servers.id", ondelete="CASCADE", name="fk_assistant_retired_dirs_relay"),
    )
    working_dir: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = _ts(server_default=text("now()"))
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
```

`coreman/core/db/models/__init__.py`：把

```python
    APPROVAL_CHANNELS,
    APPROVAL_STATUSES,
    AssistantApproval,
    AssistantKey,
    AssistantMark,
    AssistantTranscript,
)
```

换成

```python
    APPROVAL_CHANNELS,
    APPROVAL_STATUSES,
    ASSISTANT_STATES,
    AssistantApproval,
    AssistantKey,
    AssistantLifecycle,
    AssistantMark,
    AssistantRetiredDir,
    AssistantTranscript,
)
```

`coreman/core/db/models/__init__.py`：把

```python
    "AssistantMark",
    "AssistantTranscript",
]
```

换成

```python
    "AssistantMark",
    "AssistantTranscript",
    "ASSISTANT_STATES",
    "AssistantLifecycle",
    "AssistantRetiredDir",
]
```

`coreman/core/db/models/bots.py`：把

```python
)
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


```

换成

```python
)
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# 助理记录走到哪了：只有 active 能跑轮次（`runtime.turn_blocker`）。普通记录恒为空。
ASSISTANT_STATE_CHECK = (
    "assistant_state IS NULL OR (kind = 'assistant' AND assistant_state IN "
    "('provisioning', 'active', 'deprovisioning', 'deprovisioned'))"
)


```

`coreman/core/db/models/bots.py`：把

```python
        CheckConstraint("kind IN ('standard','assistant')", name="kind"),
        CheckConstraint(ASSISTANT_SHAPE, name="assistant_shape"),
        Index("bots_platform_app_idx", "platform_app_id"),
        # 一人同时只有一条启用的助理记录。
```

换成

```python
        CheckConstraint("kind IN ('standard','assistant')", name="kind"),
        CheckConstraint(ASSISTANT_SHAPE, name="assistant_shape"),
        CheckConstraint(ASSISTANT_STATE_CHECK, name="assistant_state"),
        Index("bots_platform_app_idx", "platform_app_id"),
        # 一人同时只有一条启用的助理记录。
```

`coreman/core/db/models/bots.py`：把

```python
    # 助理记录共用的飞书应用；应用凭证以 platform_apps 为准，credentials_enc 是它的副本。
    platform_app_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("platform_apps.id"))
    relay_server_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("relay_servers.id"))
    model: Mapped[str] = mapped_column(Text)
```

换成

```python
    # 助理记录共用的飞书应用；应用凭证以 platform_apps 为准，credentials_enc 是它的副本。
    platform_app_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("platform_apps.id"))
    # 助理记录的开通状态，只有 active 能跑轮次；见 coreman/core/assistants/lifecycle.py。
    assistant_state: Mapped[str | None] = mapped_column(Text)
    relay_server_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("relay_servers.id"))
    model: Mapped[str] = mapped_column(Text)
```

`coreman/core/assistants/records.py`：把

```python
# 所有者也不能改的字段：固定取值的列，加上由平台分配的归属、目录与凭证。
LOCKED_FIELDS = frozenset(
    {*ASSISTANT_FIXED, "group_observe_scope", "team_id", "working_dir", "credentials"}
)
```

换成

```python
# 所有者也不能改的字段：固定取值的列，加上由平台分配的归属、目录、凭证与开通状态。
LOCKED_FIELDS = frozenset(
    {
        *ASSISTANT_FIXED,
        "group_observe_scope",
        "team_id",
        "working_dir",
        "credentials",
        "assistant_state",
    }
)
```

`coreman/core/assistants/records.py`：把

```python
        working_dir=working_dir,
        credentials_enc=credentials_enc,
        **ASSISTANT_FIXED,
    )
```

换成

```python
        working_dir=working_dir,
        credentials_enc=credentials_enc,
        # 开通流程走完（授权完成）才改成 active；在那之前跑不了任何一轮。
        assistant_state="provisioning",
        **ASSISTANT_FIXED,
    )
```

`coreman/core/assistants/runtime.py`：把

```python
        "绑定的运行时没有与普通员工隔开（要用专为个人助理安装的运行时），请联系管理员"
    ),
}

```

换成

```python
        "绑定的运行时没有与普通员工隔开（要用专为个人助理安装的运行时），请联系管理员"
    ),
}
# 记录本身还不能（或已不能）跑轮次：开通没走完、正在停用。这两句不说「暂停」。
LIFECYCLE_MESSAGES = {
    "provisioning": "你的个人助理还没开通完：请先点私聊里的授权卡片完成飞书授权，完成后会告诉你。",
    "deprovisioning": "你的个人助理正在停用（或已停用），不再处理消息；停用完成后可以重新开通。",
}

```

`coreman/core/assistants/runtime.py`：把

```python
    """助理现在为什么不能跑一轮；能跑返回 None。

    节点的其它条件都满足时再查一次隔离（`node_isolated`，每轮一条查询）。
    """
    relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    if relay is None:
```

换成

```python
    """助理现在为什么不能跑一轮；能跑返回 None。

    记录不是 active（开通没走完、正在或已经停用）先挡下，不查库。节点的其它条件都满足时再查一次
    隔离（`node_isolated`，每轮一条查询）。
    """
    if bot.assistant_state != "active":
        return (
            "deprovisioning"
            if bot.assistant_state in ("deprovisioning", "deprovisioned")
            else ("provisioning")
        )
    relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    if relay is None:
```

`coreman/core/assistants/runtime.py`：把

```python

def pause_message(reason: str) -> str:
    detail = PAUSE_MESSAGES.get(reason, PAUSE_MESSAGES["runtime_missing"])
    return f"你的个人助理暂停了：{detail}。"
```

换成

```python

def pause_message(reason: str) -> str:
    if reason in LIFECYCLE_MESSAGES:
        return LIFECYCLE_MESSAGES[reason]
    detail = PAUSE_MESSAGES.get(reason, PAUSE_MESSAGES["runtime_missing"])
    return f"你的个人助理暂停了：{detail}。"
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_record_states.py tests/integration/test_assistant_runtime.py tests/integration/test_assistant_turn_dispatch.py tests/integration/test_assistant_bot_schema.py tests/unit/test_assistant_records.py -q && uv run alembic check`
Expected: 全部通过；`alembic check` 报 `No new upgrade operations detected.`。另在一个新建的空库上跑 `uv run alembic upgrade head`、`uv run alembic downgrade 0091`、`uv run alembic upgrade head`，用完删掉该库。

普通机器人不受影响：`test_ordinary_bots_cannot_carry_a_state`（普通记录写入状态被检查约束拒绝，列默认为空）；`turn_blocker` 新加的判断读的是已加载的对象、不查库，且只有助理的开轮调用它；`tests/integration/test_chat_handler.py` 照常通过。

- [ ] **Step 5: 提交**

```bash
git add migrations/versions/0092_assistant_lifecycle.py coreman/core/db/models/assistants.py coreman/core/db/models/__init__.py coreman/core/db/models/bots.py coreman/core/assistants/records.py coreman/core/assistants/runtime.py tests/conftest.py tests/integration/assistant_helpers.py tests/integration/test_assistant_record_states.py
git commit -m "个人助理：记录的开通状态与开通、停用流程表，只有已开通的助理能跑轮次"
```

---


### Task 6: 设置项：开通范围与可选的 AI

**Files:**
- Modify: `coreman/core/settings_schema.py`（`assistant_eligibility`、`assistant_backends` 两个键与校验）
- Create: `coreman/core/assistants/eligibility.py`
- Modify: `tests/unit/test_settings_schema.py`（键的全集）
- Test: `tests/integration/test_assistant_eligibility.py`、`tests/api/test_assistant_settings.py`（新建）

**Interfaces:**
- Consumes: `settings` 表、`users.status`、`user_departments`、`departments.parent_id`
- Produces:
  - 设置项 `assistant_eligibility`（默认 `{"mode": "all", "department_ids": [], "user_ids": []}`）、`assistant_backends`（默认 `["claude", "codex"]`）；`settings_schema.AssistantEligibility`
  - `eligibility.refusal(session, user) -> str | None`（`inactive` / `no_login_name` / `not_listed`）、`eligibility.backends(session) -> list[str]`、`eligibility.REASONS`

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_eligibility.py`（新建）：

```python
"""开通范围：全员 / 名单里的人与部门（含下级部门）；账号停用或没有登录名的不能开通；可选的 AI。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import eligibility
from coreman.core.db.models import Department, Setting, User, UserDepartment


async def person(session: AsyncSession, **kw: object) -> User:
    user = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name="张三", **kw)
    session.add(user)
    await session.flush()
    return user


async def department(session: AsyncSession, parent: Department | None = None) -> Department:
    code = uuid.uuid4().hex[:8]
    row = Department(
        platform="feishu",
        platform_dept_id=code,
        parent_id=parent.id if parent else None,
        name=f"部门{code}",
        path=f"/{code}",
    )
    session.add(row)
    await session.flush()
    return row


async def scope(session: AsyncSession, **rule: object) -> None:
    session.add(Setting(key="assistant_eligibility", value={"mode": "listed", **rule}))
    await session.flush()


async def test_everyone_is_eligible_by_default(db_session: AsyncSession) -> None:
    assert await eligibility.refusal(db_session, await person(db_session)) is None
    assert await eligibility.backends(db_session) == ["claude", "codex"]


async def test_disabled_accounts_and_accounts_without_a_login_name_are_refused(
    db_session: AsyncSession,
) -> None:
    assert await eligibility.refusal(db_session, await person(db_session, status="disabled")) == (
        "inactive"
    )
    nameless = await person(db_session)
    nameless.login_name = None
    assert await eligibility.refusal(db_session, nameless) == "no_login_name"


async def test_a_listed_person_is_eligible(db_session: AsyncSession) -> None:
    listed, other = await person(db_session), await person(db_session)
    await scope(db_session, user_ids=[str(listed.id)], department_ids=[])
    assert await eligibility.refusal(db_session, listed) is None
    assert await eligibility.refusal(db_session, other) == "not_listed"


async def test_a_listed_department_covers_its_sub_departments(db_session: AsyncSession) -> None:
    top = await department(db_session)
    child = await department(db_session, top)
    grandchild = await department(db_session, child)
    sibling = await department(db_session)
    inside, outside = await person(db_session), await person(db_session)
    db_session.add_all(
        [
            UserDepartment(user_id=inside.id, department_id=grandchild.id),
            UserDepartment(user_id=outside.id, department_id=sibling.id),
        ]
    )
    await scope(db_session, user_ids=[], department_ids=[str(child.id)])
    assert await eligibility.refusal(db_session, inside) is None
    assert await eligibility.refusal(db_session, outside) == "not_listed"


@pytest.mark.parametrize("value", [["all"], {"mode": "everyone"}, "all", {"mode": None}])
async def test_a_broken_setting_lets_nobody_in(db_session: AsyncSession, value: object) -> None:
    db_session.add(Setting(key="assistant_eligibility", value=value))
    await db_session.flush()
    assert await eligibility.refusal(db_session, await person(db_session)) == "not_listed"


async def test_backends_keep_only_supported_ones_in_order(db_session: AsyncSession) -> None:
    db_session.add(
        Setting(key="assistant_backends", value=["codex", "opencode", "codex", "claude"])
    )
    await db_session.flush()
    assert await eligibility.backends(db_session) == ["codex", "claude"]
```

`tests/api/test_assistant_settings.py`（新建）：

```python
"""个人助理的两个设置项：开通范围与可选的 AI；管理接口校验，默认值随设置一起返回。"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Setting


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"assistant_eligibility": {"mode": "all"}}, 200),
        (
            {
                "assistant_eligibility": {
                    "mode": "listed",
                    "department_ids": [str(uuid.uuid4()).upper()],
                    "user_ids": [str(uuid.uuid4())],
                }
            },
            200,
        ),
        ({"assistant_eligibility": {"mode": "some"}}, 422),
        ({"assistant_eligibility": {"mode": "listed", "user_ids": ["李四"]}}, 422),
        ({"assistant_eligibility": None}, 422),
        ({"assistant_backends": ["codex"]}, 200),
        ({"assistant_backends": []}, 422),
        ({"assistant_backends": ["opencode"]}, 422),
    ],
)
async def test_assistant_settings_are_validated(
    admin_client: httpx.AsyncClient, db_session: AsyncSession, body: dict, status: int
) -> None:
    response = await admin_client.put("/api/admin/settings", json=body)
    assert response.status_code == status, response.text
    if status == 200:
        key = next(iter(body))
        stored = await db_session.scalar(select(Setting.value).where(Setting.key == key))
        if key == "assistant_eligibility":
            assert stored["mode"] == body[key]["mode"]
            assert all(v == v.lower() for v in stored["department_ids"])
        else:
            assert stored == body[key]


async def test_settings_defaults_include_the_assistant_keys(
    admin_client: httpx.AsyncClient,
) -> None:
    data = (await admin_client.get("/api/admin/settings")).json()["data"]
    assert data["assistant_eligibility"] == {"mode": "all", "department_ids": [], "user_ids": []}
    assert data["assistant_backends"] == ["claude", "codex"]
```

`tests/unit/test_settings_schema.py`：把

```python
        "busy_confirm_minutes",
        "assistant_node_concurrency",
        "prompt_org_context",
        "prompt_security_policy",
```

换成

```python
        "busy_confirm_minutes",
        "assistant_node_concurrency",
        "assistant_eligibility",
        "assistant_backends",
        "prompt_org_context",
        "prompt_security_policy",
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_eligibility.py tests/api/test_assistant_settings.py tests/unit/test_settings_schema.py -q`
Expected: FAIL：`ImportError: cannot import name 'eligibility'`；设置接口对两个新键返回 200 但不保存（未知键被忽略），`test_settings_defaults_include_the_assistant_keys` 失败。

- [ ] **Step 3: 实现**

`coreman/core/assistants/eligibility.py`（新建）：

```python
"""谁能开通个人助理、用哪种 AI：管理台的两个设置项，开通的每一步都按当时的值重新判断。

- `assistant_eligibility`：`{"mode": "all" | "listed", "department_ids": [], "user_ids": []}`。
  `listed` 时本人在名单里，或本人所在的任一部门（含上级部门）在名单里才能开通。
- `assistant_backends`：开通时按顺序挑第一个池里有可用节点的 AI（`claude` / `codex`）。

另外两条不看设置：账号是启用的，且有登录名（审批单的发起人按登录名认人，见审批闸门）。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Department, Setting, User, UserDepartment
from coreman.core.settings_schema import SETTING_DEFAULTS

ELIGIBILITY_KEY = "assistant_eligibility"
BACKENDS_KEY = "assistant_backends"
SUPPORTED_BACKENDS = ("claude", "codex")
# 给本人看的原因（不带任何内部信息）。
REASONS = {
    "inactive": "你的账号已停用，开通不了个人助理。",
    "no_login_name": "你的账号还没有登录名，开通不了个人助理，请联系管理员。",
    "not_listed": "你暂时不在个人助理的开通范围内，如需开通请联系管理员。",
}


async def setting(session: AsyncSession, key: str) -> Any:
    """直接读 settings 表（不经进程内缓存）：管理员刚改的范围要立即生效。"""
    value = await session.scalar(select(Setting.value).where(Setting.key == key))
    return SETTING_DEFAULTS[key] if value is None else value


async def backends(session: AsyncSession) -> list[str]:
    raw = await setting(session, BACKENDS_KEY)
    found = [b for b in raw if b in SUPPORTED_BACKENDS] if isinstance(raw, list) else []
    return list(dict.fromkeys(found))


async def _departments_with_ancestors(session: AsyncSession, user_id: uuid.UUID) -> set[str]:
    """本人所在的部门及其全部上级（部门树不会成环，UNION 去重也挡住了万一的环）。"""
    start = (
        select(Department.id, Department.parent_id)
        .join(UserDepartment, UserDepartment.department_id == Department.id)
        .where(UserDepartment.user_id == user_id)
        .cte("up", recursive=True)
    )
    tree = start.union(
        select(Department.id, Department.parent_id).join(start, Department.id == start.c.parent_id)
    )
    return {str(row) for row in await session.scalars(select(tree.c.id))}


async def refusal(session: AsyncSession, user: User) -> str | None:
    """这个人现在不能开通的原因代码（`REASONS` 的键）；能开通返回 None。"""
    if user.status != "active":
        return "inactive"
    if not user.login_name:
        return "no_login_name"
    rule = await setting(session, ELIGIBILITY_KEY)
    if isinstance(rule, dict) and rule.get("mode") == "all":
        return None
    if not isinstance(rule, dict) or rule.get("mode") != "listed":
        # 设置被写坏（管理接口有校验，坏值只可能来自直接改库）：谁都不放行。
        return "not_listed"
    if str(user.id) in {str(v) for v in rule.get("user_ids") or []}:
        return None
    wanted = {str(v) for v in rule.get("department_ids") or []}
    if wanted and wanted & await _departments_with_ancestors(session, user.id):
        return None
    return "not_listed"
```

`coreman/core/settings_schema.py`：把

```python

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from coreman.core.db.models import ModelCatalog
```

换成

```python

from collections.abc import Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from coreman.core.db.models import ModelCatalog
```

`coreman/core/settings_schema.py`：把

```python
    # 每个运行时节点同时跑的个人助理轮次上限；超出的排队（节点领取时执行）。
    "assistant_node_concurrency": 2,
    # 提示词段落：默认值由 CoreMan 提供，管理台可逐段覆盖。
    **PROMPT_DEFAULTS_BY_KEY,
```

换成

```python
    # 每个运行时节点同时跑的个人助理轮次上限；超出的排队（节点领取时执行）。
    "assistant_node_concurrency": 2,
    # 谁能开通个人助理：all 全员；listed 只有名单里的人与部门（含下级部门）。
    "assistant_eligibility": {"mode": "all", "department_ids": [], "user_ids": []},
    # 开通个人助理时可选的 AI，按顺序挑第一个助理池里有可用节点的。
    "assistant_backends": ["claude", "codex"],
    # 提示词段落：默认值由 CoreMan 提供，管理台可逐段覆盖。
    **PROMPT_DEFAULTS_BY_KEY,
```

`coreman/core/settings_schema.py`：把

```python
    "wecom_qr_provisioning_enabled",
)


```

换成

```python
    "wecom_qr_provisioning_enabled",
)


_UUID = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$",
        to_lower=True,
    ),
]


class AssistantEligibility(BaseModel):
    """个人助理的开通范围。编号存成字符串（settings 表是 JSONB）。"""

    mode: Literal["all", "listed"]
    department_ids: list[_UUID] = Field(default_factory=list, max_length=500)
    user_ids: list[_UUID] = Field(default_factory=list, max_length=5000)


```

`coreman/core/settings_schema.py`：把

```python
    busy_confirm_minutes: int | None = Field(default=None, ge=0, le=1440)
    assistant_node_concurrency: int | None = Field(default=None, ge=1, le=32)
    # 提示词段落：min_length=1 挡住「存成空串」——空串不会被 _check_not_null 当成置空，
    # 却会让安全策略/运行模式整段消失。20000 字符够放最长的 codex 输出契约还有余量。
```

换成

```python
    busy_confirm_minutes: int | None = Field(default=None, ge=0, le=1440)
    assistant_node_concurrency: int | None = Field(default=None, ge=1, le=32)
    assistant_eligibility: AssistantEligibility | None = None
    assistant_backends: list[Literal["claude", "codex"]] | None = Field(
        default=None, min_length=1, max_length=2
    )
    # 提示词段落：min_length=1 挡住「存成空串」——空串不会被 _check_not_null 当成置空，
    # 却会让安全策略/运行模式整段消失。20000 字符够放最长的 codex 输出契约还有余量。
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_eligibility.py tests/api/test_assistant_settings.py tests/unit/test_settings_schema.py tests/api/test_settings.py -q`
Expected: 全部通过。

普通机器人不受影响：`tests/api/test_settings.py` 照常通过：已有键的校验与默认值不变，设置页多出两个键。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/settings_schema.py coreman/core/assistants/eligibility.py tests/unit/test_settings_schema.py tests/integration/test_assistant_eligibility.py tests/api/test_assistant_settings.py
git commit -m "个人助理：开通范围与可选 AI 两个设置项"
```

---


### Task 7: 公共件、待删目录与换运行时登记旧目录

**Files:**
- Create: `coreman/core/assistants/lifecycle.py`（流程的公共件、`Messenger`）
- Create: `coreman/core/assistants/retired_dirs.py`
- Modify: `coreman/core/relay/agent_client.py`（`OPERATIONS` 加 `workspace-remove`）
- Modify: `coreman/core/bots/switch_relay.py`（助理换走后登记旧目录）
- Test: `tests/integration/lifecycle_helpers.py`（新建，公共件）、`tests/integration/test_assistant_retired_dirs.py`、`tests/api/test_assistant_switch_retire.py`（新建）

**Interfaces:**
- Consumes: Task 2 的节点命令 `workspace-remove`；Task 5 的 `AssistantLifecycle`、`AssistantRetiredDir`；1A 的 `apps.app_credentials`；`relay.agent_client.call_agent`
- Produces:
  - `lifecycle.StepRetry(code)`、`lifecycle.StepFailed(code)`、`lifecycle.live(session, owner_id, *, lock=True)`、`lifecycle.advance(row, step, *, at=None)`、`lifecycle.finish(row, outcome, code=None)`、`lifecycle.backoff(attempts) -> timedelta`、`lifecycle.token_hash(token) -> str`、`lifecycle.PROVISION_MAX_ATTEMPTS`
  - `lifecycle.Messenger(cipher)`：`text(session, app_id, user_id, text, key)`、`card(session, app_id, user_id, card, key) -> (message_id, chat_id)`、`aclose()`；飞书失败抛 `StepRetry`、应用不可用抛 `StepFailed`
  - `retired_dirs.retire(session, *, bot_id, relay_id, working_dir)`、`retired_dirs.tick(factory, cipher, now) -> dict[str, int]`

- [ ] **Step 1: 写失败测试**

`tests/integration/lifecycle_helpers.py`（新建）：

```python
"""开通与停用用例的公共件：成员、助理应用与助理池节点，以及飞书（消息、授权）与节点的替身。"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import lifecycle
from coreman.core.assistants.apps import SECRET_AAD
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AssistantLifecycle,
    PlatformApp,
    RelayServer,
    RuntimeNode,
    User,
    UserIdentity,
)
from coreman.core.relay.agent_client import AgentError
from coreman.core.timeutils import utcnow
from tests.fakes.runtime_node import add_node_relay
from tests.integration.assistant_helpers import MASTER_KEY

CIPHER = Cipher(MASTER_KEY)
OWNER_PID, OWNER_OPEN_ID, TENANT = "zhangsan", "ou_zhang", "tenant-example"
APP_ID = "cli_example"
CHAT = "oc_private_zhang"


class FakeMessenger(lifecycle.Messenger):
    """记下发出去的文本与卡片；`fail` 置位时发送失败（StepRetry）。"""

    def __init__(self) -> None:
        super().__init__(CIPHER)
        self.texts: list[tuple[str, str]] = []
        self.cards: list[tuple[str, dict[str, Any]]] = []
        self.fail = False

    async def _send(
        self,
        session: AsyncSession,
        app_id: uuid.UUID,
        user_id: str,
        msg_type: str,
        content: dict[str, Any],
        key: str,
    ) -> tuple[str, str]:
        if self.fail:
            raise lifecycle.StepRetry("feishu_99991400")
        if msg_type == "text":
            self.texts.append((user_id, content["text"]))
        else:
            self.cards.append((user_id, content))
        return f"om_{len(self.texts) + len(self.cards)}", CHAT

    def token(self) -> str:
        """最近一张开通卡片上的令牌。"""
        for _, card in reversed(self.cards):
            for element in card["body"]["elements"]:
                if element.get("tag") == "button":
                    value = element["behaviors"][0].get("value") or {}
                    if value.get("assistant") == "provision":
                        return str(value["token"])
        raise AssertionError("no offer card")

    def message_id(self) -> str:
        return f"om_{len(self.texts) + len(self.cards)}"


class FakeNode:
    """替换对节点的调用（建目录、删目录）；`down` 置位时节点不可达。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.down = False
        self.removed: dict[str, Any] = {"removed": True, "existed": True}

    async def __call__(
        self, relay: RelayServer, cipher: Any, operation: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if self.down:
            raise AgentError("Agent 连接失败（ConnectError）")
        self.calls.append((operation, dict(payload or {})))
        if operation == "workspace-remove":
            return {"success": True, **self.removed}
        return {"success": True, "initialized": True, "owned": True}

    def ops(self) -> list[str]:
        return [operation for operation, _ in self.calls]


async def pool_relay(session: AsyncSession, *, provider: str = "claude") -> RelayServer:
    """助理池里一个能跑助理的实例：节点在线、声明沙箱能力、上报指纹与非 root 用户。"""
    relay = await add_node_relay(
        session, name=f"pool-{uuid.uuid4().hex[:6]}", provider=provider, workspace_root="/srv/pa"
    )
    relay.assistant_pool = True
    node = await session.get(RuntimeNode, relay.runtime_node_id)
    assert node is not None
    node.capabilities = {
        provider: {"installed": True, "login": "ready", "assistant_sandbox_v1": True}
    }
    node.heartbeat_at = utcnow()
    node.machine_fingerprint, node.uid = f"fp-{node.id.hex}", 1001
    await session.flush()
    return relay


async def member(
    session: AsyncSession, *, login: str = "zhangsan", pid: str = OWNER_PID, open_id: str = ""
) -> User:
    user = User(login_name=login, display_name="张三", source="sync")
    session.add(user)
    await session.flush()
    session.add(
        UserIdentity(
            user_id=user.id, platform="feishu", platform_user_id=pid, open_id=open_id or None
        )
    )
    await session.flush()
    return user


async def assistant_app(session: AsyncSession) -> PlatformApp:
    app = PlatformApp(
        platform="feishu",
        name=f"个人助理-{uuid.uuid4().hex[:6]}",
        capabilities=["assistant"],
        app_id=APP_ID,
        secret_enc=CIPHER.encrypt("app-secret", SECRET_AAD),
    )
    session.add(app)
    await session.flush()
    return app


def message_event(pid: str = OWNER_PID) -> dict[str, Any]:
    return {
        "header": {
            "event_id": f"ev-{uuid.uuid4().hex}",
            "event_type": "im.message.receive_v1",
            "app_id": APP_ID,
            "tenant_key": TENANT,
        },
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"user_id": pid, "open_id": "ou_x"}},
            "message": {
                "message_id": f"om_in_{uuid.uuid4().hex[:8]}",
                "chat_id": CHAT,
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": "你好"}),
            },
        },
    }


def click_event(
    token: str,
    message_id: str,
    *,
    pid: str = OWNER_PID,
    open_id: str = OWNER_OPEN_ID,
    chat_id: str = CHAT,
) -> dict[str, Any]:
    return {
        "header": {
            "event_id": f"ev-{uuid.uuid4().hex}",
            "event_type": "card.action.trigger",
            "app_id": APP_ID,
            "tenant_key": TENANT,
        },
        "event": {
            "operator": {"user_id": pid, "open_id": open_id},
            "action": {"value": {"assistant": "provision", "token": token}},
            "context": {"open_message_id": message_id, "open_chat_id": chat_id},
        },
    }


async def flow_of(session: AsyncSession, owner_id: uuid.UUID) -> AssistantLifecycle | None:
    """这个人最近的一条流程（含已结束的），重新从库里读。"""
    found: AssistantLifecycle | None = await session.scalar(
        select(AssistantLifecycle)
        .where(AssistantLifecycle.owner_id == owner_id)
        .order_by(AssistantLifecycle.created_at.desc(), AssistantLifecycle.id)
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return found
```

`tests/integration/test_assistant_retired_dirs.py`（新建）：

```python
"""要删的旧工作目录：登记一次、经节点删掉、节点不在就退避重试、不是这位助理的或还在用的不删；
以及看护进程经助理应用给本人发消息的那一层。"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import lifecycle, retired_dirs
from coreman.core.db.models import AssistantRetiredDir, RelayServer
from coreman.core.db.session import make_session_factory
from coreman.core.platforms.feishu import FeishuClient
from coreman.core.timeutils import utcnow
from tests.integration.lifecycle_helpers import CIPHER, FakeNode, assistant_app, pool_relay


async def retired(session: AsyncSession, relay: RelayServer, path: str = "/srv/pa/old") -> None:
    await retired_dirs.retire(session, bot_id=uuid.uuid4(), relay_id=relay.id, working_dir=path)
    await retired_dirs.retire(session, bot_id=uuid.uuid4(), relay_id=relay.id, working_dir=path)
    await session.commit()


async def rows(session: AsyncSession) -> list[AssistantRetiredDir]:
    return list(
        await session.scalars(select(AssistantRetiredDir).execution_options(populate_existing=True))
    )


async def test_a_directory_is_registered_once_and_removed_by_its_node(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = FakeNode()
    monkeypatch.setattr(retired_dirs, "call_agent", node)
    relay = await pool_relay(db_session)
    await retired(db_session, relay)
    assert len(await rows(db_session)) == 1
    counts = await retired_dirs.tick(make_session_factory(db_engine), CIPHER, utcnow())
    assert counts == {"assistant_dirs_removed": 1, "assistant_dirs_retry": 0}
    ((operation, payload),) = node.calls
    assert operation == "workspace-remove" and payload["working_dir"] == "/srv/pa/old"
    assert await rows(db_session) == []


@pytest.mark.parametrize(
    ("answer", "kept"),
    [
        ({"down": True}, True),
        ({"removed": False, "existed": True, "busy": True}, True),
        ({"removed": False, "existed": True}, False),
        ({"removed": True, "existed": False}, False),
    ],
)
async def test_what_the_node_says_decides_whether_to_try_again(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    answer: dict,
    kept: bool,
) -> None:
    node = FakeNode()
    node.down = bool(answer.pop("down", False))
    node.removed = answer
    monkeypatch.setattr(retired_dirs, "call_agent", node)
    relay = await pool_relay(db_session)
    await retired(db_session, relay)
    now = utcnow()
    await retired_dirs.tick(make_session_factory(db_engine), CIPHER, now)
    left = await rows(db_session)
    assert bool(left) is kept
    if kept:
        assert left[0].attempts == 1 and left[0].next_attempt_at > now
        # 退避期内不再打扰节点。
        before = len(node.calls)
        await retired_dirs.tick(make_session_factory(db_engine), CIPHER, now)
        assert len(node.calls) == before


async def test_a_directory_in_use_again_is_never_removed(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.assistant_helpers import seed_assistant

    node = FakeNode()
    monkeypatch.setattr(retired_dirs, "call_agent", node)
    relay = await pool_relay(db_session)
    bot, _ = await seed_assistant(db_session)
    bot.relay_server_id, bot.working_dir = relay.id, "/srv/pa/live"
    await retired_dirs.retire(
        db_session, bot_id=bot.id, relay_id=relay.id, working_dir="/srv/pa/live"
    )
    await db_session.commit()
    await retired_dirs.tick(make_session_factory(db_engine), CIPHER, utcnow())
    assert node.calls == [] and await rows(db_session) == []


async def test_an_unexpected_error_postpones_only_that_directory(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: object, **_kw: object) -> dict:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(retired_dirs, "call_agent", broken)
    relay = await pool_relay(db_session)
    await retired(db_session, relay)
    now = utcnow()
    await retired_dirs.tick(make_session_factory(db_engine), CIPHER, now)
    (row,) = await rows(db_session)
    assert row.error_code == "unexpected" and row.next_attempt_at >= now + timedelta(hours=1)


async def test_deleting_the_runtime_instance_drops_its_rows(db_session: AsyncSession) -> None:
    relay = await pool_relay(db_session)
    await retired(db_session, relay)
    await db_session.delete(relay)
    await db_session.commit()
    assert await db_session.scalar(select(func.count()).select_from(AssistantRetiredDir)) == 0


async def test_the_messenger_sends_through_the_assistant_app(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = await assistant_app(db_session)
    await db_session.commit()
    sent: list[dict] = []
    reply = {"code": 0, "data": {"message_id": "om_1", "chat_id": "oc_1"}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 7200})
        sent.append({"params": dict(request.url.params), **json.loads(request.content)})
        return httpx.Response(200, json=reply)

    def client(app_id: str, secret: str) -> FeishuClient:
        assert (app_id, secret) == ("cli_example", "app-secret")
        http = httpx.AsyncClient(
            base_url="https://open.feishu.cn", transport=httpx.MockTransport(handler)
        )
        return FeishuClient(app_id, secret, http=http)

    monkeypatch.setattr(lifecycle, "FeishuClient", client)
    messenger = lifecycle.Messenger(CIPHER)
    card = {"schema": "2.0"}
    assert await messenger.card(db_session, app.id, "zhangsan", card, key="k") == ("om_1", "oc_1")
    assert sent[0]["params"] == {"receive_id_type": "user_id"}
    assert sent[0]["msg_type"] == "interactive" and json.loads(sent[0]["content"]) == card
    assert sent[0]["receive_id"] == "zhangsan" and len(sent[0]["uuid"]) == 32
    reply = {"code": 230013, "msg": "synthetic"}
    with pytest.raises(lifecycle.StepRetry) as failed:
        await messenger.text(db_session, app.id, "zhangsan", "你好", key="k2")
    assert failed.value.code == "feishu_230013"
    reply = {"code": 0, "data": {}}
    with pytest.raises(lifecycle.StepRetry):
        await messenger.text(db_session, app.id, "zhangsan", "你好", key="k3")
    with pytest.raises(lifecycle.StepFailed):
        await messenger.text(db_session, uuid.uuid4(), "zhangsan", "你好", key="k4")
    await messenger.aclose()


def test_the_node_operation_is_on_the_platform_side_list() -> None:
    from coreman.core.relay import agent_client

    assert "workspace-remove" in agent_client.OPERATIONS
```

`tests/api/test_assistant_switch_retire.py`（新建）：

```python
"""助理换运行时之后旧目录登记待删，看护进程经节点删掉它；原目录照常在用时不登记；普通员工不变。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from coreman.core.assistants import retired_dirs
from coreman.core.db.models import AssistantRetiredDir, RelayServer, User
from coreman.core.db.session import make_session_factory
from tests.api.conftest import login_existing
from tests.api.test_workspace_migration import _assistant_agents, _real_agents, _switch


async def test_switching_away_retires_the_old_directory_and_the_node_removes_it(
    client, db_session, monkeypatch, tmp_path, db_engine
):
    bot, old, target, agents, seen = await _assistant_agents(
        client, db_session, tmp_path, monkeypatch
    )
    old_dir = bot.working_dir
    response = await _switch(client, bot, target)
    assert response.status_code == 200, response.text
    row = await db_session.scalar(select(AssistantRetiredDir))
    assert row is not None and row.relay_server_id == old.id and row.working_dir == old_dir
    assert row.bot_id == bot.id

    calls: list[str] = []

    async def call(relay, cipher, operation, payload=None):
        calls.append(operation)
        return agents[relay.id].dispatch({"type": operation, **(payload or {})})

    monkeypatch.setattr(retired_dirs, "call_agent", call)
    agents[old.id].assistant_only = True  # 助理专用实例才接 workspace-remove
    counts = await retired_dirs.tick(make_session_factory(db_engine), None, datetime.now(UTC))
    assert counts["assistant_dirs_removed"] == 1 and calls == ["workspace-remove"]
    for _ in range(100):
        if not (tmp_path / "source" / bot.bot_key).exists():
            break
        await asyncio.sleep(0.05)
    assert not (tmp_path / "source" / bot.bot_key).exists()
    await db_session.refresh(bot)
    # 新目录不受影响。
    assert (tmp_path / "destination" / bot.bot_key / "AGENTS.md").exists()
    assert await db_session.scalar(select(AssistantRetiredDir)) is None


async def test_rebinding_on_the_same_node_retires_nothing(
    client, db_session, monkeypatch, tmp_path
):
    bot, old, _, agents, _ = await _assistant_agents(client, db_session, tmp_path, monkeypatch)
    second = RelayServer(
        runtime_node_id=old.runtime_node_id,
        name="pool-two",
        model_provider="claude",
        assistant_pool=True,
    )
    db_session.add(second)
    await db_session.commit()
    agents[second.id] = agents[old.id]
    assert (await _switch(client, bot, second)).status_code == 200
    assert await db_session.scalar(select(AssistantRetiredDir)) is None


async def test_ordinary_switches_retire_nothing(client, db_session, monkeypatch, tmp_path):
    bot, old, target, _, _ = await _real_agents(db_session, tmp_path, monkeypatch)
    creator = await db_session.get(User, bot.created_by)
    await login_existing(client, db_session, creator)
    response = await _switch(client, bot, target)
    assert response.status_code == 200, response.text
    assert await db_session.scalar(select(AssistantRetiredDir)) is None
    assert (tmp_path / "source" / bot.bot_key).exists()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_retired_dirs.py tests/api/test_assistant_switch_retire.py -q`
Expected: FAIL：`ImportError: cannot import name 'lifecycle'`（`coreman.core.assistants`）。

- [ ] **Step 3: 实现**

`coreman/core/assistants/lifecycle.py`（新建）：

```python
"""开通与停用的公共件：一人一条进行中的流程、步骤的成败、给本人发消息。

流程记在 `assistant_lifecycles`，由看护进程（`lifecycle_runner.tick`）一步一步推进；开通的各步在
`provisioning`，停用的各步在 `deprovisioning`。每一步在自己的事务里做完并提交；失败就停在原地，
`next_attempt_at` 到了再试。

给本人发的消息一律经助理应用本身（`platform_apps` 的凭证），不经某条助理记录的出站箱：开通前还
没有记录，停用后记录已停用、出站循环不再发它的消息。消息里不带节点、主机或任何内部信息。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.apps import app_credentials
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantLifecycle, PlatformApp
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.core.timeutils import utcnow

# 本步骤连续失败到这么多次：开通放弃（走一遍停用把已建的东西收掉）；停用永不放弃。
PROVISION_MAX_ATTEMPTS = 10
BACKOFF_MAX = timedelta(minutes=10)


class StepRetry(Exception):
    """这一步暂时做不成（节点离线、飞书或审批系统不通）：原地等下次。`code` 只是代码。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class StepFailed(Exception):
    """这一步再试也不会成（不在开通范围、应用权限不足……）：开通就此放弃。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def live(
    session: AsyncSession, owner_id: uuid.UUID, *, lock: bool = True
) -> AssistantLifecycle | None:
    """这个人进行中的那一条流程（最多一条，见 `assistant_lifecycles_live_uq`）。"""
    stmt = select(AssistantLifecycle).where(
        AssistantLifecycle.owner_id == owner_id, AssistantLifecycle.finished_at.is_(None)
    )
    found: AssistantLifecycle | None = await session.scalar(
        stmt.with_for_update() if lock else stmt
    )
    return found


def advance(row: AssistantLifecycle, step: str, *, at: datetime | None = None) -> None:
    """进入下一步：失败计数清零，`at`（默认现在）就接着做。"""
    row.step, row.attempts, row.error_code = step, 0, None
    row.next_attempt_at = at or utcnow()
    row.updated_at = utcnow()


def finish(row: AssistantLifecycle, outcome: str, code: str | None = None) -> None:
    now = utcnow()
    row.finished_at, row.outcome, row.next_attempt_at, row.updated_at = now, outcome, None, now
    row.offer_hash = None
    if code is not None:
        row.error_code = code


def backoff(attempts: int) -> timedelta:
    return min(BACKOFF_MAX, timedelta(seconds=15 * 2 ** min(attempts, 10)))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Messenger:
    """经助理应用给本人发消息与卡片；同一轮推进里按应用复用飞书客户端，用完 `aclose`。"""

    def __init__(self, cipher: Cipher) -> None:
        self.cipher = cipher
        self._clients: dict[uuid.UUID, FeishuClient] = {}

    async def _client(self, session: AsyncSession, app_id: uuid.UUID) -> FeishuClient:
        client = self._clients.get(app_id)
        if client is None:
            app = await session.get(PlatformApp, app_id)
            if app is None:
                raise StepFailed("app_missing")
            try:
                creds = app_credentials(self.cipher, app)
            except ValueError:
                raise StepFailed("app_unavailable") from None
            client = FeishuClient(creds["app_id"], creds["app_secret"])
            self._clients[app_id] = client
        return client

    async def _send(
        self,
        session: AsyncSession,
        app_id: uuid.UUID,
        user_id: str,
        msg_type: str,
        content: dict[str, Any],
        key: str,
    ) -> tuple[str, str]:
        client = await self._client(session, app_id)
        try:
            body = await client.call(
                "POST",
                "/open-apis/im/v1/messages",
                params={"receive_id_type": "user_id"},
                json={
                    "receive_id": user_id,
                    "msg_type": msg_type,
                    "content": json.dumps(content, ensure_ascii=False),
                    "uuid": hashlib.sha256(key.encode()).hexdigest()[:32],
                },
            )
        except FeishuError as exc:
            raise StepRetry(f"feishu_{exc.code}") from None
        data = body.get("data") or {}
        message_id, chat_id = data.get("message_id"), data.get("chat_id")
        if not isinstance(message_id, str) or not isinstance(chat_id, str):
            raise StepRetry("feishu_bad_response")
        return message_id, chat_id

    async def text(
        self, session: AsyncSession, app_id: uuid.UUID, user_id: str, text: str, key: str
    ) -> None:
        await self._send(session, app_id, user_id, "text", {"text": text}, key)

    async def card(
        self,
        session: AsyncSession,
        app_id: uuid.UUID,
        user_id: str,
        card: dict[str, Any],
        key: str,
    ) -> tuple[str, str]:
        return await self._send(session, app_id, user_id, "interactive", card, key)

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
```

`coreman/core/assistants/retired_dirs.py`（新建）：

```python
"""删掉节点上不再用的助理工作目录：停用时的当前目录、换运行时换走的旧目录。

登记（`retire`）与删除分开：登记在停用或换运行时的同一个事务里；删除由看护进程（`tick`）向目录所在
实例的节点发 `workspace-remove`，节点离线就退避重试，直到删掉或节点答复「不是这位助理的」。节点那边
只删带本记录所有权标记的目录、不跟随任何链接（见 runtime_daemon/workspace.py `Workspace.remove`）。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantRetiredDir, Bot, RelayServer
from coreman.core.logging import get_logger
from coreman.core.relay.agent_client import AgentError, call_agent

log = get_logger(__name__)
BATCH = 10
NODE_SECONDS = 60
BACKOFF_MAX = timedelta(hours=6)


async def retire(
    session: AsyncSession, *, bot_id: uuid.UUID, relay_id: uuid.UUID, working_dir: str
) -> None:
    """登记一个要删的目录；同一实例同一路径只登记一次。调用方提交。"""
    await session.execute(
        insert(AssistantRetiredDir)
        .values(id=uuid.uuid4(), bot_id=bot_id, relay_server_id=relay_id, working_dir=working_dir)
        .on_conflict_do_nothing(constraint="uq_assistant_retired_dirs_path")
    )


def _backoff(attempts: int) -> timedelta:
    return min(BACKOFF_MAX, timedelta(minutes=2 ** min(attempts, 9)))


async def _in_use(session: AsyncSession, row: AssistantRetiredDir, relay: RelayServer) -> bool:
    """这个目录现在又是某条启用记录的工作目录（同一节点同一路径）：绝不删。"""
    bot = await session.get(Bot, row.bot_id)
    if bot is None or not bot.enabled or bot.working_dir != row.working_dir:
        return False
    current = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    return current is not None and current.runtime_node_id == relay.runtime_node_id


async def _one(session: AsyncSession, cipher: Cipher, row: AssistantRetiredDir) -> str:
    relay = await session.get(RelayServer, row.relay_server_id)
    if relay is None or await _in_use(session, row, relay):
        await session.delete(row)
        return "dropped"
    try:
        async with asyncio.timeout(NODE_SECONDS):
            result = await call_agent(
                relay,
                cipher,
                "workspace-remove",
                {"working_dir": row.working_dir, "bot_id": str(row.bot_id)},
            )
    except (AgentError, TimeoutError) as exc:
        row.attempts += 1
        row.error_code = getattr(exc, "code", None) or type(exc).__name__
        row.next_attempt_at = row.next_attempt_at + _backoff(row.attempts)
        return "retry"
    if result.get("busy") is True:
        # 节点上还有沙箱里的轮次用着这个目录：等它结束再来。
        row.attempts += 1
        row.error_code = "busy"
        row.next_attempt_at = row.next_attempt_at + _backoff(row.attempts)
        return "retry"
    if result.get("removed") is not True:
        # 目录在，但不带这位助理的标记（初始化没做完、或已被别人占用）：不归我们删。
        log.warning("assistant_dir_not_owned", retired=str(row.id))
    await session.delete(row)
    return "removed"


async def _postpone(
    factory: async_sessionmaker[AsyncSession], row_id: uuid.UUID, now: datetime
) -> None:
    """出了意外的那条推迟一阵，不让它每一轮都卡在同一个地方。"""
    try:
        async with factory() as session:
            await session.execute(
                update(AssistantRetiredDir)
                .where(AssistantRetiredDir.id == row_id)
                .values(
                    attempts=AssistantRetiredDir.attempts + 1,
                    next_attempt_at=now + BACKOFF_MAX,
                    error_code="unexpected",
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 推迟不了就下一轮再说
        log.warning("assistant_dir_postpone_failed", error=type(exc).__name__)


async def tick(
    factory: async_sessionmaker[AsyncSession], cipher: Cipher, now: datetime
) -> dict[str, int]:
    """处理到期的几条；每条一个事务、互不影响。"""
    counts = {"assistant_dirs_removed": 0, "assistant_dirs_retry": 0}
    async with factory() as session:
        due = list(
            await session.scalars(
                select(AssistantRetiredDir.id)
                .where(AssistantRetiredDir.next_attempt_at <= now)
                .order_by(AssistantRetiredDir.next_attempt_at)
                .limit(BATCH)
            )
        )
    for row_id in due:
        try:
            async with factory() as session:
                row = await session.scalar(
                    select(AssistantRetiredDir)
                    .where(AssistantRetiredDir.id == row_id)
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    continue
                row.next_attempt_at = max(row.next_attempt_at, now)
                outcome = await _one(session, cipher, row)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 一条出错不挡后面的
            log.warning("assistant_dir_remove_failed", error=type(exc).__name__)
            await _postpone(factory, row_id, now)
            continue
        if outcome == "removed":
            counts["assistant_dirs_removed"] += 1
        elif outcome == "retry":
            counts["assistant_dirs_retry"] += 1
    return counts
```

`coreman/core/relay/agent_client.py`：把

```python
    "workspace-git-diff",
    "workspace-probe",
    "ping",
    "status",
```

换成

```python
    "workspace-git-diff",
    "workspace-probe",
    # 个人助理停用或换走之后删它的工作目录（只有助理专用实例接）。
    "workspace-remove",
    "ping",
    "status",
```

`coreman/core/bots/switch_relay.py`：把

```python
    carried = {row.relay_session_id for row in await sessions.list_for_bot(session, bot.id)}
    moved_skills: list[BotSkill] = []
    requested_directory = target_directory
    target_directory = bot.working_dir
```

换成

```python
    carried = {row.relay_session_id for row in await sessions.list_for_bot(session, bot.id)}
    moved_skills: list[BotSkill] = []
    # 还是原来那个目录（同一节点同一路径，或共享盘上的同一份）：换完之后旧目录照常在用，不能删。
    reused = True
    requested_directory = target_directory
    target_directory = bot.working_dir
```

`coreman/core/bots/switch_relay.py`：把

```python
    bot.working_dir = target_directory
    bot.relay_server_id, bot.model = target.id, new_model
    if bot.effort_level:
        # 自动换来的模型不支持 xhigh / max 时降到它支持的最高档，否则下发给 relay 的就是非法档位。
```

换成

```python
    bot.working_dir = target_directory
    bot.relay_server_id, bot.model = target.id, new_model
    if is_assistant(bot) and old_relay_id is not None and not reused:
        from coreman.core.assistants import retired_dirs

        # 助理换走之后旧目录没人再用：登记下来由看护进程删掉（只删带本助理标记的目录）。
        await retired_dirs.retire(
            session, bot_id=bot.id, relay_id=old_relay_id, working_dir=old_directory
        )
    if bot.effort_level:
        # 自动换来的模型不支持 xhigh / max 时降到它支持的最高档，否则下发给 relay 的就是非法档位。
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_retired_dirs.py tests/api/test_assistant_switch_retire.py tests/api/test_workspace_migration.py tests/integration/test_assistant_runtime.py -q`
Expected: 全部通过。

普通机器人不受影响：`test_ordinary_switches_retire_nothing`（普通员工换运行时之后不登记、源目录还在）；`switch_relay` 新加的分支只看已加载对象上的 `is_assistant(bot)`，`tests/api/test_workspace_migration.py` 里普通员工的换运行时用例照常通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants/lifecycle.py coreman/core/assistants/retired_dirs.py coreman/core/relay/agent_client.py coreman/core/bots/switch_relay.py tests/integration/lifecycle_helpers.py tests/integration/test_assistant_retired_dirs.py tests/api/test_assistant_switch_retire.py
git commit -m "个人助理：待删工作目录的登记与删除，换运行时换走的旧目录登记待删"
```

---


### Task 8: 停用流程与看护进程

**Files:**
- Create: `coreman/core/assistants/deprovisioning.py`
- Create: `coreman/core/assistants/lifecycle_runner.py`（这一版只推进停用；开通由 Task 9 接进来）
- Modify: `coreman/runtime/scheduler/service.py`（单独一条循环 `_assistant_lifecycle_loop`）
- Modify: `tests/integration/lifecycle_helpers.py`（导入块换掉，末尾追加授权替身、`install`、`tick`、`active_assistant`）
- Test: `tests/integration/test_assistant_deprovisioning.py`（新建）

**Interfaces:**
- Consumes: Task 5、7；1C 的 `keys.destroy_key`；1D 的 `approvals.withdraw_pending(session, bot_id, reason)`；`feishu_personal.service.revoke_grant(session, bot_id, user_id, cipher)`；`bus.tasks.request_cancel`、`tasks.OPEN` / `tasks.ACTIVE`
- Produces:
  - `deprovisioning.request(session, bot, *, trigger, actor_id) -> AssistantLifecycle | None`（`trigger`：`owner` / `member_left` / `admin` / `abandoned`；不是助理记录抛 `ValueError`；调用方提交）
  - `deprovisioning.STEPS`（`grace`、`drain`、`withdraw`、`revoke`、`destroy`）、`GRACE`、`DONE_TEXT`、`retired_key(bot)`
  - `lifecycle_runner.tick(factory, cipher, now, *, messenger=None) -> dict[str, int]`
  - scheduler 的 `ASSISTANT_LIFECYCLE_SECONDS = 5.0`

- [ ] **Step 1: 写失败测试**

`tests/integration/lifecycle_helpers.py`（导入块）：把

```python
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import lifecycle
from coreman.core.assistants.apps import SECRET_AAD
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AssistantLifecycle,
    PlatformApp,
    RelayServer,
    RuntimeNode,
    User,
    UserIdentity,
)
from coreman.core.relay.agent_client import AgentError
from coreman.core.timeutils import utcnow
from tests.fakes.runtime_node import add_node_relay
from tests.integration.assistant_helpers import MASTER_KEY

```

换成

```python
import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants import keys, lifecycle, lifecycle_runner, retired_dirs, sealed
from coreman.core.assistants.apps import SECRET_AAD
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AssistantLifecycle,
    Bot,
    FeishuPersonalGrant,
    PlatformApp,
    RelayServer,
    RuntimeNode,
    Task,
    User,
    UserIdentity,
)
from coreman.core.feishu_personal import revocation
from coreman.core.feishu_personal import service as personal
from coreman.core.relay.agent_client import AgentError
from coreman.core.timeutils import utcnow
from tests.fakes.runtime_node import add_node_relay
from tests.integration.assistant_helpers import MASTER_KEY

```

`tests/integration/lifecycle_helpers.py` 末尾空两行之后追加：

```python
class FakeFeishuAuth:
    """飞书设备授权与撤销的替身：`approve()` 之后，下一次查询拿到本人的令牌。"""

    def __init__(self) -> None:
        self.approved = False
        self.issued = 0
        self.revoked: list[dict[str, Any]] = []
        self.identity = {"user_id": OWNER_PID, "open_id": OWNER_OPEN_ID, "tenant_key": TENANT}

    def approve(self) -> None:
        self.approved = True

    async def http(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        if url.endswith("/oauth/v1/device_authorization"):
            self.issued += 1
            return {
                "device_code": f"dc-{self.issued}",
                "verification_uri_complete": f"https://accounts.feishu.cn/verify?c={self.issued}",
                "expires_in": 600,
                "interval": 5,
            }
        if url == personal.TOKEN_URL:
            if not self.approved:
                return {"error": "authorization_pending"}
            return {
                "access_token": "u-access",
                "refresh_token": "u-refresh",
                "expires_in": 7200,
                "scope": "offline_access im:message",
            }
        if url.endswith("/authen/v1/user_info"):
            return {"code": 0, "data": self.identity}
        raise AssertionError(url)

    async def revoke(self, app_id: str, secret: str, tokens: dict[str, Any]) -> bool:
        self.revoked.append(tokens)
        return True


def install(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeNode, FakeFeishuAuth]:
    node, auth = FakeNode(), FakeFeishuAuth()
    monkeypatch.setattr(retired_dirs, "call_agent", node)
    monkeypatch.setattr(personal, "_http", auth.http)
    monkeypatch.setattr(revocation, "revoke_tokens", auth.revoke)

    async def no_scopes(*_args: Any, **_kw: Any) -> list[str]:
        # 应用开通了发消息与读消息的全部用户权限（「全部」档位要它们）。
        return ["offline_access", "im:message", "im:message.send_as_user", "im:chat:read"]

    from coreman.core.feishu_personal import permissions

    monkeypatch.setattr(permissions, "app_user_scopes", no_scopes)
    return node, auth


async def tick(
    factory: async_sessionmaker[AsyncSession], messenger: FakeMessenger, *, later: float = 0
) -> dict[str, int]:
    """推进一轮；`later` 秒之后的时刻（跳过退避与轮询间隔）。"""
    return await lifecycle_runner.tick(
        factory, CIPHER, utcnow() + timedelta(seconds=later), messenger=messenger
    )


async def active_assistant(session: AsyncSession) -> tuple[Bot, User, Task]:
    """一位已经开通好的助理（与所有者聊过一轮，那一轮已结束），带着有效的飞书授权。

    记录绑在助理池里一个能跑助理的实例上（`into_assistant` → `sandbox_ready`）。
    """
    from tests.integration.assistant_helpers import into_assistant
    from tests.integration.credential_helpers import owner

    bot, user, task, _ = await owner(session)
    await into_assistant(session, bot, user)
    await keys.ensure_key(session, sealed.master(), user.id)
    from coreman.core.bots.secrets import CREDENTIALS_AAD, encrypt_json

    # 助理记录上是助理应用凭证的副本（见 assistants.apps）。
    bot.credentials_enc = encrypt_json(
        CIPHER, {"app_id": APP_ID, "app_secret": "app-secret"}, CREDENTIALS_AAD
    )
    grant = FeishuPersonalGrant(
        bot_id=bot.id,
        user_id=user.id,
        app_id=APP_ID,
        platform_user_id="owner_pid",
        open_id="ou_owner",
        tenant_key=TENANT,
        status="connected",
        authorization_level="all",
        scopes=["offline_access"],
        requested_scopes=["offline_access"],
    )
    grant.token_enc = CIPHER.encrypt(
        json.dumps({"access_token": "u-access", "refresh_token": "u-refresh"}),
        personal._aad(grant, "token_enc"),
    )
    session.add(grant)
    task.status = "succeeded"
    await session.commit()
    return bot, user, task
```

`tests/integration/test_assistant_deprovisioning.py`（新建）：

```python
"""停用：本人在私聊里确认 → 停下轮次 → 撤回待批 → 撤销授权 → 销毁密钥、停用记录 → 删工作目录。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import deprovisioning, keys, runtime
from coreman.core.bus import tasks
from coreman.core.db.models import (
    AssistantKey,
    AssistantLifecycle,
    AssistantRetiredDir,
    Bot,
    FeishuPersonalGrant,
    Task,
)
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal import service as personal
from tests.integration.assistant_helpers import seed_standard
from tests.integration.lifecycle_helpers import (
    CIPHER,
    FakeMessenger,
    active_assistant,
    flow_of,
    install,
    tick,
)
from tests.integration.test_chat_handler import chat_task

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


async def test_deprovisioning_leaves_nothing_usable(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    node, auth = install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    flow = await deprovisioning.request(db_session, bot, trigger="owner", actor_id=owner.id)
    await db_session.commit()
    assert flow is not None and flow.step == "drain"
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "deprovisioning" and bot.enabled
    assert await runtime.turn_blocker(db_session, bot) == "deprovisioning"
    # 重复请求不重复开流程。
    assert (await deprovisioning.request(db_session, bot, trigger="admin", actor_id=None)) == flow
    await db_session.commit()
    messenger = FakeMessenger()
    factory = make_session_factory(db_engine)
    for _ in range(6):
        await tick(factory, messenger)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "done"
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "deprovisioned" and not bot.enabled
    assert bot.bot_key.endswith(f"-x{bot.id.hex[:8]}")
    grant = await db_session.get(FeishuPersonalGrant, (bot.id, owner.id), populate_existing=True)
    assert grant is not None and grant.status == "revoked" and grant.token_enc is None
    assert auth.revoked and auth.revoked[0]["refresh_token"] == "u-refresh"
    key = await db_session.get(AssistantKey, owner.id, populate_existing=True)
    assert key is not None and key.dek_enc is None and key.destroyed_at is not None
    keys.forget()
    with pytest.raises(keys.KeyDestroyed):
        await keys.data_cipher(db_session, CIPHER, owner.id)
    assert ("workspace-remove", {"working_dir": bot.working_dir, "bot_id": str(bot.id)}) in (
        node.calls
    )
    assert await db_session.scalar(select(func.count()).select_from(AssistantRetiredDir)) == 0
    assert messenger.texts[-1] == ("owner_pid", deprovisioning.DONE_TEXT["owner"])
    assert await deprovisioning.request(db_session, bot, trigger="owner", actor_id=None) is None


async def test_an_ordinary_bot_cannot_be_sent_into_deprovisioning(db_session: AsyncSession) -> None:
    standard = await seed_standard(db_session)
    with pytest.raises(ValueError):
        await deprovisioning.request(db_session, standard, trigger="admin", actor_id=None)
    assert standard.assistant_state is None


async def test_a_member_who_leaves_gets_a_grace_period(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    owner.status = "disabled"
    await db_session.commit()
    await tick(factory, messenger)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "grace" and flow.trigger == "member_left"
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "deprovisioning"
    # 宽限期内账号恢复：作罢，助理照常可用，什么都没销毁。
    owner.status = "active"
    await db_session.commit()
    await tick(factory, messenger, later=deprovisioning.GRACE_CHECK.total_seconds() + 1)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "cancelled"
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "active"
    # 再次停用并超过宽限期：照常停用到底，不给已离开的人发消息。
    owner.status = "disabled"
    await db_session.commit()
    await tick(factory, messenger)
    await db_session.execute(
        update(AssistantLifecycle)
        .where(AssistantLifecycle.finished_at.is_(None))
        .values(deadline=func.now() - text("interval '1 second'"), next_attempt_at=func.now())
    )
    await db_session.commit()
    for _ in range(6):
        await tick(factory, messenger, later=1)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "done" and flow.trigger == "member_left"
    assert messenger.texts == []
    key = await db_session.get(AssistantKey, owner.id, populate_existing=True)
    assert key is not None and key.destroyed_at is not None


async def test_running_turns_are_stopped_before_anything_is_withdrawn(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    queued = await chat_task(
        db_session, bot, "再查一下", sender="owner_pid", chat_type="single", chat_id="oc_private"
    )
    queued.status = "running"
    await deprovisioning.request(db_session, bot, trigger="admin", actor_id=None)
    await db_session.commit()
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    await tick(factory, messenger)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "drain"
    row = await db_session.get(Task, queued.id, populate_existing=True)
    assert row is not None and row.cancel_requested_at is not None
    await tasks.finish(db_session, queued.id, status="cancelled")
    await db_session.commit()
    await tick(factory, messenger, later=11)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "withdraw"


async def test_a_failing_step_is_retried_from_where_it_stopped(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    await deprovisioning.request(db_session, bot, trigger="owner", actor_id=owner.id)
    await db_session.commit()
    real = personal.revoke_grant

    async def broken(*_args: Any, **_kw: Any) -> dict[str, Any]:
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(personal, "revoke_grant", broken)
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    for hour in range(1, 5):
        await tick(factory, messenger, later=3600 * hour)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "revoke" and flow.attempts >= 2
    assert flow.error_code == "RuntimeError" and flow.finished_at is None
    key = await db_session.get(AssistantKey, owner.id, populate_existing=True)
    assert key is not None and key.destroyed_at is None
    monkeypatch.setattr(personal, "revoke_grant", real)
    for hour in range(5, 8):
        await tick(factory, messenger, later=3600 * hour)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "done"


async def test_a_retired_directory_waits_for_its_node(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    node, _ = install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    await deprovisioning.request(db_session, bot, trigger="owner", actor_id=owner.id)
    await db_session.commit()
    node.down = True
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    for _ in range(6):
        await tick(factory, messenger)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "done"
    row = await db_session.scalar(select(AssistantRetiredDir))
    assert row is not None and row.attempts >= 1 and row.error_code == "AgentError"
    node.down = False
    await tick(factory, messenger, later=86400)
    assert await db_session.scalar(select(func.count()).select_from(AssistantRetiredDir)) == 0
    assert node.ops().count("workspace-remove") == 1


async def test_a_directory_the_node_says_is_not_ours_is_dropped(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    node, _ = install(monkeypatch)
    node.removed = {"removed": False, "existed": True}
    bot, owner, _ = await active_assistant(db_session)
    await deprovisioning.request(db_session, bot, trigger="owner", actor_id=owner.id)
    await db_session.commit()
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    for _ in range(6):
        await tick(factory, messenger)
    assert await db_session.scalar(select(func.count()).select_from(AssistantRetiredDir)) == 0


async def test_a_directory_still_used_by_a_turn_is_retried_later(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    node, _ = install(monkeypatch)
    node.removed = {"removed": False, "existed": True, "busy": True}
    bot, owner, _ = await active_assistant(db_session)
    await deprovisioning.request(db_session, bot, trigger="owner", actor_id=owner.id)
    await db_session.commit()
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    for _ in range(6):
        await tick(factory, messenger)
    row = await db_session.scalar(select(AssistantRetiredDir))
    assert row is not None and row.error_code == "busy" and row.attempts == 1
    node.removed = {"removed": True, "existed": True}
    await tick(factory, messenger, later=86400)
    assert await db_session.scalar(select(func.count()).select_from(AssistantRetiredDir)) == 0


async def test_the_scheduler_runs_lifecycles_in_their_own_loop_and_survives_failures(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """看护进程单独一条循环推进开通与停用：每次都出错也不影响与普通员工共用的那一轮看护。"""
    import asyncio

    from coreman.core.assistants import lifecycle_runner
    from coreman.runtime.scheduler import reaper, service
    from coreman.runtime.scheduler.service import SchedulerService

    calls: list[int] = []
    cleaned = asyncio.Event()

    async def broken(*_args: object, **_kw: object) -> dict[str, int]:
        calls.append(1)
        raise RuntimeError("synthetic")

    async def cleanup(*_args: object, **_kw: object) -> dict[str, int]:
        cleaned.set()
        return {}

    monkeypatch.setattr(lifecycle_runner, "tick", broken)
    monkeypatch.setattr(reaper, "run_cleanup", cleanup)
    monkeypatch.setattr(service, "ASSISTANT_LIFECYCLE_SECONDS", 0.05)
    svc = SchedulerService(port=0, tick_seconds=0.2, cleanup_seconds=0.5, leader_retry_seconds=0.2)
    task = asyncio.create_task(svc.run())
    try:
        await asyncio.wait_for(cleaned.wait(), 10)
        for _ in range(100):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.05)
    finally:
        svc.request_stop("test")
        await asyncio.wait_for(task, 10)
    assert len(calls) >= 2
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_deprovisioning.py -q`
Expected: FAIL：`ImportError: cannot import name 'deprovisioning'`。

- [ ] **Step 3: 实现**

`coreman/core/assistants/deprovisioning.py`（新建）：

```python
"""停用个人助理：停下在跑的轮次 → 撤回待批的审批单 → 撤销飞书授权 → 销毁数据密钥并停用记录，
工作目录交给 `retired_dirs` 删掉。

三个入口，都只调 `request`：本人在私聊里确认停用（worker 的卡片回调）、成员离开（看护进程发现账号
停用，先宽限 24 小时，期间恢复就作罢）、管理员在助理列表里停用（只回状态，不回任何内容）。另外，
开通半途放弃时也走这里收拾。

`request` 一提交，记录就是 deprovisioning：`runtime.turn_blocker` 挡住一切轮次，之后各步由看护进程
推进，每步一个事务，失败原地重试、永不放弃。密钥最后才销毁：撤回审批单要它所在的记录、撤销授权要
记录上的应用凭证，都不需要数据密钥，但「待批的单先撤回」必须在销毁之前完成。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import approvals, keys, lifecycle, retired_dirs
from coreman.core.assistants.kinds import APPROVAL_EXECUTE_KIND
from coreman.core.assistants.lifecycle import StepFailed, advance, finish
from coreman.core.assistants.records import is_assistant
from coreman.core.bots.events import notify_bot_changed
from coreman.core.bus import tasks
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantLifecycle, Bot, Task, User, UserIdentity
from coreman.core.feishu_personal import service as personal
from coreman.core.timeutils import utcnow

# 成员离开（账号被停用）后等这么久才真的停用：通讯录同步抖一下、管理员误停，都不至于销毁密钥。
GRACE = timedelta(hours=24)
GRACE_CHECK = timedelta(minutes=10)
# 等在跑的轮次停下最多这么久；之后不等了（它们写不进任何东西：记录已不是 active）。
DRAIN_WAIT = timedelta(minutes=15)
DRAIN_CHECK = timedelta(seconds=10)
REASON = "assistant_deprovisioned"
DONE_TEXT = {
    "owner": (
        "个人助理已停用：飞书授权已撤销，待批的审批单已撤回，加密保存的个人内容已不可恢复。"
        "想再用的时候给我发条消息，可以重新开通。"
    ),
    "admin": (
        "管理员停用了你的个人助理：飞书授权已撤销，待批的审批单已撤回，加密保存的个人内容已不可恢复。"
    ),
}


async def _feishu_user(session: AsyncSession, owner_id: uuid.UUID) -> str:
    found = await session.scalar(
        select(UserIdentity.platform_user_id)
        .where(UserIdentity.user_id == owner_id, UserIdentity.platform == "feishu")
        .limit(1)
    )
    return found or ""


async def request(
    session: AsyncSession, bot: Bot, *, trigger: str, actor_id: uuid.UUID | None
) -> AssistantLifecycle | None:
    """开始停用这条助理记录，返回进行中的停用流程；已经停用完的返回 None。调用方提交。

    重复请求不重复做；正在开通的那条流程就此取消。

    Raises:
        ValueError: 不是个人助理的记录
    """
    if not is_assistant(bot) or bot.owner_id is None or bot.platform_app_id is None:
        raise ValueError("not an assistant record")
    await session.refresh(bot, with_for_update=True)
    row = await lifecycle.live(session, bot.owner_id)
    if row is not None and row.action == "deprovision":
        return row
    if bot.assistant_state == "deprovisioned":
        return None
    if row is not None:
        finish(row, "cancelled", "deprovision_requested")
        await session.flush()
    now = utcnow()
    grace = trigger == "member_left"
    new = AssistantLifecycle(
        owner_id=bot.owner_id,
        platform_app_id=bot.platform_app_id,
        bot_id=bot.id,
        action="deprovision",
        step="grace" if grace else "drain",
        trigger=trigger,
        actor_id=actor_id,
        platform_user_id=(row.platform_user_id if row is not None else "")
        or await _feishu_user(session, bot.owner_id),
        deadline=now + GRACE if grace else None,
        next_attempt_at=now + GRACE_CHECK if grace else now,
    )
    bot.assistant_state = "deprovisioning"
    session.add(new)
    await session.flush()
    return new


async def _bot(session: AsyncSession, row: AssistantLifecycle) -> Bot:
    bot = await session.get(Bot, row.bot_id, with_for_update=True) if row.bot_id else None
    if bot is None:
        # 记录被删掉了（管理员删除停用后的记录也只发生在 deprovisioned 之后）：剩下的只有密钥。
        raise StepFailed("bot_missing")
    return bot


async def step_grace(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """成员离开后的宽限：账号恢复就作罢（助理照常可用），到期还是停用的才往下走。"""
    bot = await _bot(session, row)
    owner = await session.get(User, row.owner_id)
    now = utcnow()
    if owner is not None and owner.status == "active":
        bot.assistant_state = "active"
        finish(row, "cancelled", "member_returned")
        return []
    if row.deadline is not None and row.deadline > now:
        row.next_attempt_at, row.updated_at = min(row.deadline, now + GRACE_CHECK), now
        return []
    advance(row, "drain")
    return []


async def step_drain(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """叫停这条记录没结束的任务（已批准的写操作照常执行完），等在跑的停下，最多等 `DRAIN_WAIT`。"""
    bot = await _bot(session, row)
    now = utcnow()
    if row.deadline is None:
        row.deadline = now + DRAIN_WAIT
    found = (
        await session.execute(
            select(Task.id, Task.kind, Task.status).where(
                Task.bot_id == bot.id, Task.status.in_(tasks.OPEN)
            )
        )
    ).all()
    for task_id, kind, _status in found:
        if kind != APPROVAL_EXECUTE_KIND:
            await tasks.request_cancel(session, task_id, REASON)
    running = [task_id for task_id, _kind, status in found if status in tasks.ACTIVE]
    if running and row.deadline > now:
        row.next_attempt_at, row.updated_at = now + DRAIN_CHECK, now
        return []
    advance(row, "withdraw")
    return []


async def step_withdraw(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    bot = await _bot(session, row)
    await approvals.withdraw_pending(session, bot.id, REASON)
    advance(row, "revoke")
    return []


async def step_revoke(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """撤销飞书个人授权：库里的令牌当场清空（之后谁也拿不到）；飞书那边的撤销尽力而为。"""
    bot = await _bot(session, row)
    await personal.revoke_grant(session, bot.id, row.owner_id, cipher)
    advance(row, "destroy")
    return []


def retired_key(bot: Bot) -> str:
    """停用后的 bot_key：腾出原来的名字给以后重新开通的记录。"""
    suffix = f"-x{bot.id.hex[:8]}"
    return bot.bot_key if bot.bot_key.endswith(suffix) else bot.bot_key + suffix


async def step_destroy(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """最后一步，一个事务：再撤一次期间冒出来的待批单，销毁数据密钥，登记删目录，停用记录。"""
    bot = await _bot(session, row)
    await approvals.withdraw_pending(session, bot.id, REASON)
    await keys.destroy_key(session, row.owner_id)
    if bot.relay_server_id is not None:
        await retired_dirs.retire(
            session, bot_id=bot.id, relay_id=bot.relay_server_id, working_dir=bot.working_dir
        )
    bot.assistant_state, bot.enabled, bot.bot_key = "deprovisioned", False, retired_key(bot)
    await notify_bot_changed(session, bot.id)
    finish(row, "done")
    text = DONE_TEXT.get(row.trigger or "")
    return [text] if text else []


STEPS = {
    "grace": step_grace,
    "drain": step_drain,
    "withdraw": step_withdraw,
    "revoke": step_revoke,
    "destroy": step_destroy,
}
```

`coreman/core/assistants/lifecycle_runner.py`（新建）：

```python
"""看护进程推进开通与停用：每一步一个事务，失败原地重试，放弃时收拾干净。

由 scheduler 单独的一条循环调用（不在与普通员工共用的那一轮看护里）：这里有对节点与飞书的网络调用，
慢了、卡了、出错了都只影响个人助理的开通与停用。每一步另有 `STEP_SECONDS` 的总时限。

一轮做三件事：把账号已停用的成员的助理送进停用（先宽限）；推进到期的流程；删一批已登记的旧工作
目录。给本人的消息在那一步提交之后才发，发不出去只记一笔。开通的各步由下一个任务接进来。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants import deprovisioning, keys, retired_dirs
from coreman.core.assistants.lifecycle import (
    Messenger,
    StepFailed,
    StepRetry,
    backoff,
    finish,
)
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantLifecycle, Bot, User
from coreman.core.logging import get_logger

log = get_logger(__name__)
STEP_SECONDS = 150
BATCH = 10
DEPARTURE_BATCH = 20
# 一条通知：（应用、飞书 user_id、流程编号、文本）。
Notice = tuple[uuid.UUID, str, uuid.UUID, str]


async def _departures(factory: async_sessionmaker[AsyncSession]) -> int:
    """账号已停用的成员：他的助理进入停用（宽限期内账号恢复就作罢）。"""
    async with factory() as session:
        bot_ids = list(
            await session.scalars(
                select(Bot.id)
                .join(User, User.id == Bot.owner_id)
                .where(
                    Bot.kind == "assistant",
                    Bot.enabled.is_(True),
                    Bot.assistant_state == "active",
                    User.status == "disabled",
                )
                .limit(DEPARTURE_BATCH)
            )
        )
    started = 0
    for bot_id in bot_ids:
        try:
            async with factory() as session:
                bot = await session.get(Bot, bot_id)
                if bot is not None:
                    await deprovisioning.request(session, bot, trigger="member_left", actor_id=None)
                    await session.commit()
                    started += 1
        except Exception as exc:  # noqa: BLE001 一位出错不挡别人
            log.warning("assistant_departure_failed", error=type(exc).__name__)
    return started


async def _give_up(session: AsyncSession, row: AssistantLifecycle, code: str) -> list[str]:
    """这一步再也做不成。停用只会因为记录没了走到这里：直接销毁密钥。"""
    await keys.destroy_key(session, row.owner_id)
    finish(row, "failed", code)
    return []


async def _settle_failure(
    factory: async_sessionmaker[AsyncSession],
    row_id: uuid.UUID,
    code: str,
    *,
    permanent: bool,
    now: datetime,
) -> list[str]:
    """步骤的事务已回滚：另开一个事务记下失败，或（再也做不成时）放弃。"""
    async with factory() as session:
        row = await session.get(AssistantLifecycle, row_id, with_for_update=True)
        if row is None or row.finished_at is not None:
            return []
        row.attempts += 1
        row.error_code, row.updated_at = code, now
        if permanent:
            texts = await _give_up(session, row, code)
        else:
            row.next_attempt_at = now + backoff(row.attempts)
            texts = []
        await session.commit()
        return texts


async def _run_one(
    factory: async_sessionmaker[AsyncSession],
    cipher: Cipher,
    messenger: Messenger,
    row_id: uuid.UUID,
    now: datetime,
) -> list[Notice]:
    texts: list[str] = []
    async with factory() as session:
        row = await session.scalar(
            select(AssistantLifecycle)
            .where(
                AssistantLifecycle.id == row_id,
                AssistantLifecycle.finished_at.is_(None),
                AssistantLifecycle.next_attempt_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return []
        target = (row.platform_app_id, row.platform_user_id)
        step = deprovisioning.STEPS[row.step]
        failure: tuple[str, bool] | None = None
        try:
            async with asyncio.timeout(STEP_SECONDS):
                texts = await step(session, cipher, row, messenger)
            await session.commit()
        except StepFailed as exc:
            failure = (exc.code, True)
        except StepRetry as exc:
            failure = (exc.code, False)
        except Exception as exc:  # noqa: BLE001 意外按暂时失败处理，只记类型
            failure = (type(exc).__name__, False)
        if failure is not None:
            await session.rollback()
    if failure is not None:
        log.warning("assistant_lifecycle_step_failed", step=step.__name__, code=failure[0])
        texts = await _settle_failure(factory, row_id, failure[0], permanent=failure[1], now=now)
    return [(target[0], target[1], row_id, text) for text in texts]


async def _notify(
    factory: async_sessionmaker[AsyncSession], messenger: Messenger, notices: list[Notice]
) -> None:
    for index, (app_id, user_id, row_id, text) in enumerate(notices):
        if not user_id:
            continue
        try:
            async with factory() as session:
                await messenger.text(
                    session, app_id, user_id, text, key=f"assistant-notice:{row_id}:{index}"
                )
        except Exception as exc:  # noqa: BLE001 通知发不出去不影响流程
            log.warning("assistant_lifecycle_notice_failed", error=type(exc).__name__)


async def tick(
    factory: async_sessionmaker[AsyncSession],
    cipher: Cipher,
    now: datetime,
    *,
    messenger: Messenger | None = None,
) -> dict[str, int]:
    owned = messenger is None
    messenger = messenger or Messenger(cipher)
    counts = {"assistant_lifecycle_steps": 0}
    try:
        counts["assistant_departures"] = await _departures(factory)
        async with factory() as session:
            due = list(
                await session.scalars(
                    select(AssistantLifecycle.id)
                    .where(
                        AssistantLifecycle.finished_at.is_(None),
                        AssistantLifecycle.next_attempt_at <= now,
                    )
                    .order_by(AssistantLifecycle.next_attempt_at)
                    .limit(BATCH)
                )
            )
        for row_id in due:
            try:
                notices = await _run_one(factory, cipher, messenger, row_id, now)
            except Exception as exc:  # noqa: BLE001 一条流程出错不挡别的
                log.warning("assistant_lifecycle_failed", error=type(exc).__name__)
                continue
            counts["assistant_lifecycle_steps"] += 1
            await _notify(factory, messenger, notices)
        counts.update(await retired_dirs.tick(factory, cipher, now))
    finally:
        if owned:
            await messenger.aclose()
    return counts
```

`coreman/runtime/scheduler/service.py`：把

```python
CONTACT_SYNC_CHECK_SECONDS = 300.0
RULES_BACKUP_CHECK_SECONDS = 600.0


```

换成

```python
CONTACT_SYNC_CHECK_SECONDS = 300.0
RULES_BACKUP_CHECK_SECONDS = 600.0
# 个人助理的开通与停用：多久推进一次（授权完成后最多这么久就开通好）。
ASSISTANT_LIFECYCLE_SECONDS = 5.0


```

`coreman/runtime/scheduler/service.py`：把

```python
                ("scheduler-contact-sync", self._contact_sync_loop()),
                ("scheduler-rules-backup", self._rules_backup_loop()),
            )
        ]
```

换成

```python
                ("scheduler-contact-sync", self._contact_sync_loop()),
                ("scheduler-rules-backup", self._rules_backup_loop()),
                ("scheduler-assistant-lifecycle", self._assistant_lifecycle_loop()),
            )
        ]
```

`coreman/runtime/scheduler/service.py`：把

```python
            await self._sleep(RULES_BACKUP_CHECK_SECONDS)

    async def _wecom_provisions_loop(self) -> None:
        """替已经离开页面的发起人取回扫码结果：二维码等于密钥，拿到结果越早，泄露窗口越小。"""
```

换成

```python
            await self._sleep(RULES_BACKUP_CHECK_SECONDS)

    async def _assistant_lifecycle_loop(self) -> None:
        """个人助理的开通与停用：单独一条循环，里面对节点与飞书的调用慢了、出错了都只影响它自己。"""
        from coreman.core.assistants import lifecycle_runner

        while not self._stop.is_set():
            try:
                if self.is_leader:
                    counts = await lifecycle_runner.tick(
                        self._factory, self._cipher, datetime.now(UTC)
                    )
                    hits = {key: value for key, value in counts.items() if value}
                    if hits:
                        self._log.info("assistant_lifecycle", **hits)
            except Exception as exc:  # noqa: BLE001 出错只记类型，下一轮再来
                self._log.warning("assistant_lifecycle_failed", error=type(exc).__name__)
            await self._sleep(ASSISTANT_LIFECYCLE_SECONDS)

    async def _wecom_provisions_loop(self) -> None:
        """替已经离开页面的发起人取回扫码结果：二维码等于密钥，拿到结果越早，泄露窗口越小。"""
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_deprovisioning.py tests/integration/test_scheduler_reaper.py tests/integration/test_assistant_approval_tick.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: 全部通过（停用用例 9 条；`test_the_scheduler_runs_lifecycles_in_their_own_loop_and_survives_failures` 要等看护进程拿到领导锁，十几秒）。

普通机器人不受影响：`test_an_ordinary_bot_cannot_be_sent_into_deprovisioning`；看护进程的新循环与 `_tick_loop`（普通员工的回收、清理）分开，`test_the_scheduler_runs_lifecycles_in_their_own_loop_and_survives_failures` 证明它一直出错时普通看护照常跑完；`tests/integration/test_scheduler_reaper.py` 照常通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants/deprovisioning.py coreman/core/assistants/lifecycle_runner.py coreman/runtime/scheduler/service.py tests/integration/lifecycle_helpers.py tests/integration/test_assistant_deprovisioning.py
git commit -m "个人助理：停用流程（停轮次、撤审批、撤授权、销毁密钥、停用记录、删目录）由看护进程推进"
```

---


### Task 9: 开通流程

**Files:**
- Create: `coreman/core/assistants/provisioning.py`
- Modify: `coreman/core/assistants/lifecycle_runner.py`（接上开通：过期卡片、开通的各步、放弃时收拾）
- Test: `tests/integration/test_assistant_provisioning.py`（新建）

**Interfaces:**
- Consumes: Task 5–8；1A 的 `new_assistant_bot`、`apps.is_assistant_app` / `app_credentials`；1C 的 `keys.ensure_key`；1E 的 `runtime.pick_pool_relay(session, provider)`、`runtime.node_isolated`；`bots.workspace.reserve_workspace`、`bots.workspace_transfer.instruction_content`；`feishu_personal.service.begin_in_flow` / `authorization_status`
- Produces:
  - `provisioning.request_offer(session, *, app_id, owner_id, platform_user_id) -> None`（只写库）
  - `provisioning.accept_offer(session, *, app_id, owner_id, raw) -> str`（`provision_started` / `provision_expired`；只写库）
  - `provisioning.STEPS`（`offered`、`create`、`init_workspace`、`start_grant`、`await_grant`）、`offer_card(token)`、`grant_card(url)`、`welcome_text()`、`failure_text(code)`、`FAILURES`、`OFFER_TTL`、`GRANT_ROUNDS`

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_provisioning.py`（新建）：

```python
"""开通：登记 → 开通卡片 → 本人点开通 → 建记录与密钥、选节点、建目录 → 飞书授权 → 才可用。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from coreman.core.assistants import keys, lifecycle, provisioning, runtime
from coreman.core.db.models import (
    AssistantKey,
    AssistantLifecycle,
    AssistantRetiredDir,
    Bot,
    FeishuPersonalGrant,
    Setting,
    UserIdentity,
)
from coreman.core.db.session import make_session_factory
from tests.integration import lifecycle_helpers
from tests.integration.lifecycle_helpers import (
    CHAT,
    CIPHER,
    OWNER_OPEN_ID,
    FakeFeishuAuth,
    FakeMessenger,
    FakeNode,
    assistant_app,
    click_event,
    flow_of,
    member,
    message_event,
    pool_relay,
    tick,
)

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


def install(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeNode, FakeFeishuAuth]:
    """公共的替身之外，开通对节点的调用也落到同一个节点替身上。"""
    node, auth = lifecycle_helpers.install(monkeypatch)
    monkeypatch.setattr(provisioning, "call_agent", node)
    return node, auth


class Door:
    """开通的两个入口（私聊登记、点开通卡片），按网关的做法调用：认人只看事件里的发送人或操作人。

    网关本身（`AssistantInbound.on_unprovisioned`）的用例见 test_assistant_gateway_provisioning。
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession], app_id: uuid.UUID) -> None:
        self.factory, self.app_id = factory, app_id

    async def accept(self, raw: dict[str, Any]) -> str | None:
        event = raw["event"]
        click = raw["header"]["event_type"] == "card.action.trigger"
        pid = event["operator"]["user_id"] if click else event["sender"]["sender_id"]["user_id"]
        async with self.factory() as session:
            user_id = await session.scalar(
                select(UserIdentity.user_id).where(
                    UserIdentity.platform == "feishu", UserIdentity.platform_user_id == pid
                )
            )
            if user_id is None:
                return None
            outcome: str | None = None
            if click:
                outcome = await provisioning.accept_offer(
                    session, app_id=self.app_id, owner_id=user_id, raw=raw
                )
            else:
                await provisioning.request_offer(
                    session, app_id=self.app_id, owner_id=user_id, platform_user_id=pid
                )
            await session.commit()
        return outcome


async def poll_now(session: AsyncSession) -> None:
    """跳过飞书要求的查询间隔（授权查询按真实时间限速）。"""
    await session.execute(update(FeishuPersonalGrant).values(next_poll_at=None))
    await session.commit()


async def offered(db_engine: AsyncEngine, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    """张三私聊了助理应用、收到了开通卡片。"""
    app = await assistant_app(db_session)
    owner = await member(db_session)
    await pool_relay(db_session)
    await db_session.commit()
    inbound = Door(make_session_factory(db_engine), app.id)
    assert await inbound.accept(message_event()) is None
    messenger = FakeMessenger()
    await tick(make_session_factory(db_engine), messenger)
    return app, owner, inbound, messenger


async def test_the_whole_way_from_a_message_to_a_usable_assistant(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    node, auth = install(monkeypatch)
    app, owner, inbound, messenger = await offered(db_engine, db_session)
    factory = make_session_factory(db_engine)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "offered" and flow.offer_chat_id == CHAT
    token = messenger.token()
    # 令牌只存摘要。
    assert flow.offer_hash == lifecycle.token_hash(token) and token not in str(flow.offer_hash)
    assert await inbound.accept(click_event(token, flow.offer_message_id)) == "provision_started"
    await tick(factory, messenger)  # 建记录与密钥
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "init_workspace" and flow.bot_id is not None
    bot = await db_session.get(Bot, flow.bot_id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "provisioning" and bot.enabled
    assert bot.working_dir == f"/srv/pa/assistant-{bot.id.hex}"
    assert await runtime.turn_blocker(db_session, bot) == "provisioning"
    key = await db_session.get(AssistantKey, owner.id)
    assert key is not None and key.dek_enc and key.destroyed_at is None
    await tick(factory, messenger)  # 节点上建目录
    assert node.calls == [
        (
            "workspace-init",
            {
                "working_dir": bot.working_dir,
                "bot_id": str(bot.id),
                "content": node.calls[0][1]["content"],
            },
        )
    ]
    await tick(factory, messenger)  # 发起授权
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "await_grant" and flow.issued == 1
    assert "一键授权" in str(messenger.cards[-1][1])
    await tick(factory, messenger, later=6)  # 还没点同意
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "await_grant" and flow.finished_at is None
    auth.approve()
    await poll_now(db_session)
    await tick(factory, messenger, later=12)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "done" and flow.finished_at is not None
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "active"
    grant = await db_session.get(FeishuPersonalGrant, (bot.id, owner.id), populate_existing=True)
    assert grant is not None and grant.status == "connected" and grant.authorization_level == "all"
    assert grant.open_id == OWNER_OPEN_ID
    assert messenger.texts[-1][1] == provisioning.welcome_text()


async def test_a_card_can_be_used_once_by_its_owner_in_its_own_chat(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    app, owner, inbound, messenger = await offered(db_engine, db_session)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None
    token, message_id = messenger.token(), flow.offer_message_id
    assert message_id is not None
    # 转发出去的卡片是另一条消息；换了会话也不行；令牌不对不行。
    assert await inbound.accept(click_event(token, "om_forwarded")) == "provision_expired"
    assert await inbound.accept(click_event(token, message_id, chat_id="oc_group")) == (
        "provision_expired"
    )
    assert await inbound.accept(click_event("guess", message_id)) == "provision_expired"
    # 别人点：按点的人找他自己的流程，没有，作废。
    other = await member(db_session, login="lisi", pid="lisi")
    await db_session.commit()
    assert await inbound.accept(click_event(token, message_id, pid="lisi")) == "provision_expired"
    assert await flow_of(db_session, other.id) is None
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "offered"
    assert await inbound.accept(click_event(token, message_id)) == "provision_started"
    # 点过即作废：重放不会再开一次。
    assert await inbound.accept(click_event(token, message_id)) == "provision_started"
    count = await db_session.scalar(select(func.count()).select_from(AssistantLifecycle))
    assert count == 1


async def test_an_expired_card_does_nothing_and_a_new_message_brings_a_new_one(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    app, owner, inbound, messenger = await offered(db_engine, db_session)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.offer_message_id is not None
    token, message_id = messenger.token(), flow.offer_message_id
    later = provisioning.OFFER_TTL.total_seconds() + 1
    await tick(make_session_factory(db_engine), messenger, later=later)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "expired" and flow.offer_hash is None
    assert await inbound.accept(click_event(token, message_id)) == "provision_expired"
    await inbound.accept(message_event())
    await tick(make_session_factory(db_engine), messenger, later=later)
    assert messenger.token() != token


async def test_messages_within_ten_minutes_do_not_resend_the_card(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    app, owner, inbound, messenger = await offered(db_engine, db_session)
    for _ in range(3):
        await inbound.accept(message_event())
        await tick(make_session_factory(db_engine), messenger)
    assert len(messenger.cards) == 1


async def test_people_outside_the_scope_are_told_once(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    app = await assistant_app(db_session)
    owner = await member(db_session)
    db_session.add(
        Setting(
            key="assistant_eligibility",
            value={"mode": "listed", "department_ids": [], "user_ids": []},
        )
    )
    await db_session.commit()
    inbound, messenger = Door(make_session_factory(db_engine), app.id), FakeMessenger()
    for _ in range(2):
        await inbound.accept(message_event())
        await tick(make_session_factory(db_engine), messenger)
    assert messenger.cards == []
    assert [text for _, text in messenger.texts] == [
        "你暂时不在个人助理的开通范围内，如需开通请联系管理员。"
    ]
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "ineligible" and flow.error_code == "not_listed"


async def test_no_pool_runtime_retries_then_gives_up_without_leaving_anything(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    app = await assistant_app(db_session)
    owner = await member(db_session)
    await db_session.commit()
    inbound, messenger = Door(make_session_factory(db_engine), app.id), FakeMessenger()
    factory = make_session_factory(db_engine)
    await inbound.accept(message_event())
    await tick(factory, messenger)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.offer_message_id is not None
    await inbound.accept(click_event(messenger.token(), flow.offer_message_id))
    for attempt in range(lifecycle.PROVISION_MAX_ATTEMPTS):
        await tick(factory, messenger, later=700 * (attempt + 1))
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "failed" and flow.error_code == "no_pool_runtime"
    assert await db_session.scalar(select(func.count()).select_from(Bot)) == 0
    assert await db_session.get(AssistantKey, owner.id) is None
    assert messenger.texts[-1][1] == provisioning.FAILURES["no_pool_runtime"]


async def test_a_node_that_stays_down_abandons_and_cleans_up(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    node, _ = install(monkeypatch)
    app, owner, inbound, messenger = await offered(db_engine, db_session)
    factory = make_session_factory(db_engine)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.offer_message_id is not None
    await inbound.accept(click_event(messenger.token(), flow.offer_message_id))
    await tick(factory, messenger)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.bot_id is not None
    bot_id = flow.bot_id
    node.down = True
    for attempt in range(lifecycle.PROVISION_MAX_ATTEMPTS):
        await tick(factory, messenger, later=700 * (attempt + 1))
    # 放弃开通 → 走一遍停用：记录停用、密钥销毁、目录登记待删。
    node.down = False
    for step in range(8):
        await tick(factory, messenger, later=700 * (lifecycle.PROVISION_MAX_ATTEMPTS + step + 1))
    bot = await db_session.get(Bot, bot_id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "deprovisioned" and not bot.enabled
    key = await db_session.get(AssistantKey, owner.id, populate_existing=True)
    assert key is not None and key.dek_enc is None and key.destroyed_at is not None
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.action == "deprovision" and flow.trigger == "abandoned"
    assert flow.outcome == "done"
    assert "workspace-remove" in node.ops()
    assert await db_session.scalar(select(func.count()).select_from(AssistantRetiredDir)) == 0
    with pytest.raises(keys.KeyDestroyed):
        keys.forget()
        await keys.data_cipher(db_session, CIPHER, owner.id)


async def test_a_grant_that_is_never_completed_is_abandoned_after_three_links(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    app, owner, inbound, messenger = await offered(db_engine, db_session)
    factory = make_session_factory(db_engine)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.offer_message_id is not None
    await inbound.accept(click_event(messenger.token(), flow.offer_message_id))
    later = 0.0
    for _ in range(3 + 2 * provisioning.GRANT_ROUNDS):
        later += 700
        await tick(factory, messenger, later=later)
        # 本人一直没点：每发一次授权链接，就让它过期。
        await db_session.execute(
            update(FeishuPersonalGrant).values(
                pending_expires_at=func.now() - text("interval '1 second'")
            )
        )
        await db_session.commit()
    grants = [card for _, card in messenger.cards if "一键授权" in str(card)]
    assert len(grants) == provisioning.GRANT_ROUNDS
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.action == "deprovision" and flow.trigger == "abandoned"
    assert provisioning.FAILURES["grant_not_completed"] in [text for _, text in messenger.texts]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_provisioning.py -q`
Expected: FAIL：`ImportError: cannot import name 'provisioning'`。

- [ ] **Step 3: 实现**

`coreman/core/assistants/provisioning.py`（新建）：

```python
"""开通个人助理：私聊助理应用 → 开通卡片 → 本人点「开通」→ 建记录与密钥、选节点、建目录 → 飞书授权
→ 授权完成才可用。

入口两处，都在网关的应答窗口里、只写库：
- `request_offer`：没有助理的人私聊（或打开与应用的私聊）时，登记一条「要发开通卡片」的流程；
- `accept_offer`：卡片回调。认人只认飞书给的操作人（网关已按它找到本人），卡片里的令牌只用来认
  「是不是发给这个人的那一张、还没过期、没点过」：令牌只存摘要、点过即作废，还要求回调里的消息与
  会话正是那张卡片所在的私聊消息（转发出去的卡片是另一条消息）。

之后的各步由看护进程推进（`lifecycle_runner`），每步一个事务。记录建好之后、授权完成之前它的状态
是 provisioning：`runtime.turn_blocker` 挡住一切轮次。任何一步放弃时走一遍停用，把已经建了的
记录、密钥、目录与授权收干净。
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import uuid
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import eligibility, keys, lifecycle, runtime, sealed
from coreman.core.assistants.apps import app_credentials, is_assistant_app
from coreman.core.assistants.lifecycle import StepFailed, StepRetry, advance, finish, token_hash
from coreman.core.assistants.records import new_assistant_bot
from coreman.core.bots.secrets import CREDENTIALS_AAD, encrypt_json
from coreman.core.bots.workspace import reserve_workspace
from coreman.core.bots.workspace_transfer import instruction_content
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AssistantLifecycle,
    Bot,
    FeishuPersonalGrant,
    PlatformApp,
    RelayServer,
    RuntimeNode,
    Task,
    User,
)
from coreman.core.errors import ApiError
from coreman.core.feishu_personal import service as personal
from coreman.core.feishu_personal.policy import Scope
from coreman.core.relay.agent_client import AgentError, call_agent
from coreman.core.relay.models import default_model, load_catalog
from coreman.core.timeutils import utcnow

OFFER_TTL = timedelta(minutes=30)
# 同一张卡片发出这么久之内，再发消息不重发卡片。
OFFER_RESEND = timedelta(minutes=10)
# 刚被拒（不在范围）或刚失败过：这段时间里再发消息不再提醒。
QUIET = {"ineligible": timedelta(hours=24), "failed": timedelta(minutes=10)}
# 授权链接最多发这么多次（每次十来分钟有效）还没完成，就放弃这次开通。
GRANT_ROUNDS = 3
GRANT_LEVEL = "all"
GRANT_POLL = timedelta(seconds=5)
NODE_SECONDS = 120
TOASTS = ("provision_started", "provision_expired")
FAILURES = {
    **eligibility.REASONS,
    "no_pool_runtime": "个人助理暂时没有可用的运行时，这次开通没有完成，请稍后再试或联系管理员。",
    "no_model": "个人助理还没有可用的模型，这次开通没有完成，请联系管理员。",
    "app_unavailable": "个人助理应用暂时不可用，这次开通没有完成，请联系管理员。",
    "app_permission_missing": "个人助理应用的飞书权限不足，这次开通没有完成，请联系管理员。",
    "already_provisioned": "你已经有一个个人助理了。",
    "grant_not_completed": "飞书授权一直没有完成，这次开通已取消；想用的时候再给我发条消息即可。",
}
GENERIC_FAILURE = "个人助理这次没能开通，已经收拾干净；稍后再给我发条消息可以重新开通。"


def offer_card(token: str) -> dict[str, Any]:
    """开通卡片：说清楚它是什么、数据放在哪、要先做什么；按钮的值只有一个随机令牌。"""
    text = (
        "这是你的个人助理：以你的飞书身份帮你看消息、起草回复、处理文档与审批。\n"
        "- 只在这个私聊里工作，对话与个人数据加密保存，只有你本人看得到。\n"
        "- 对外有影响的操作（发消息、改文档、处理审批……）都先提交审批，"
        "由你本人在审批系统里批准后才执行；审批单正文保存在审批系统里。\n"
        "- 开通前请先登录一次审批系统，否则第一次提交审批时会提示没有账号。\n"
        "- 开通后还要授权一次飞书；随时发「停用助理」可以停用，停用后个人数据不可恢复。"
    )
    return {
        "schema": "2.0",
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": "开通个人助理"},
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": text},
                {
                    "tag": "button",
                    "type": "primary",
                    "text": {"tag": "plain_text", "content": "开通"},
                    "behaviors": [
                        {"type": "callback", "value": {"assistant": "provision", "token": token}}
                    ],
                },
            ]
        },
    }


def grant_card(url: str) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"template": "turquoise", "title": {"tag": "plain_text", "content": "飞书授权"}},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "最后一步：授权助理以你的身份使用飞书（读消息与文档、起草；"
                        "发出去之前都要你批准）。授权完成后我会在这里告诉你。\n"
                        f"{personal.RETENTION_NOTICE}"
                    ),
                },
                {
                    "tag": "button",
                    "type": "primary",
                    "text": {"tag": "plain_text", "content": "一键授权"},
                    "behaviors": [{"type": "open_url", "default_url": url}],
                },
            ]
        },
    }


def welcome_text() -> str:
    return (
        "个人助理开通好了，直接在这里说要做的事就行。\n"
        "对外有影响的操作会先提交审批，在审批系统里批准后才执行。"
        "要是提示你在审批系统里还没有账号，先登录一次审批系统再让我重试。\n"
        "发「停用助理」可以随时停用。"
    )


# ---- 网关：登记与点击 --------------------------------------------------------------------------


async def request_offer(
    session: AsyncSession, *, app_id: uuid.UUID, owner_id: uuid.UUID, platform_user_id: str
) -> None:
    """没有助理的人发来私聊：登记一条开通流程，由看护进程判断范围并发卡片。只写库，调用方提交。"""
    now = utcnow()
    row = await lifecycle.live(session, owner_id)
    if row is not None:
        sent_at = row.deadline - OFFER_TTL if row.deadline else None
        if (
            row.action == "provision"
            and row.step == "offered"
            and row.next_attempt_at is None
            and (sent_at is None or sent_at <= now - OFFER_RESEND)
        ):
            row.platform_user_id, row.next_attempt_at = platform_user_id, now
        return
    for outcome, quiet in QUIET.items():
        recent = await session.scalar(
            select(AssistantLifecycle.id)
            .where(
                AssistantLifecycle.owner_id == owner_id,
                AssistantLifecycle.action == "provision",
                AssistantLifecycle.outcome == outcome,
                AssistantLifecycle.finished_at > now - quiet,
            )
            .limit(1)
        )
        if recent is not None:
            return
    await session.execute(
        insert(AssistantLifecycle)
        .values(
            id=uuid.uuid4(),
            owner_id=owner_id,
            platform_app_id=app_id,
            action="provision",
            step="offered",
            trigger="owner",
            actor_id=owner_id,
            platform_user_id=platform_user_id,
            next_attempt_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=[AssistantLifecycle.owner_id],
            index_where=AssistantLifecycle.finished_at.is_(None),
        )
    )


def _obj(container: object, key: str) -> dict[str, Any]:
    value = container.get(key) if isinstance(container, dict) else None
    return value if isinstance(value, dict) else {}


async def accept_offer(
    session: AsyncSession, *, app_id: uuid.UUID, owner_id: uuid.UUID, raw: dict[str, Any]
) -> str:
    """本人点了开通卡片。`owner_id` 是网关按回调里的操作人认出的人，不来自卡片。调用方提交。

    返回回调应答用的结果（`TOASTS` 之一）。
    """
    header, event = _obj(raw, "header"), _obj(raw, "event")
    value = _obj(_obj(event, "action"), "value")
    context, operator = _obj(event, "context"), _obj(event, "operator")
    token = value.get("token")
    open_id = operator.get("open_id")
    tenant_key = header.get("tenant_key") or operator.get("tenant_key")
    row = await lifecycle.live(session, owner_id)
    if row is None or row.action != "provision" or row.platform_app_id != app_id:
        return "provision_expired"
    if row.step != "offered":
        return "provision_started"
    if not (
        value.get("assistant") == "provision"
        and isinstance(token, str)
        and row.offer_hash is not None
        and hmac.compare_digest(row.offer_hash, token_hash(token))
        and context.get("open_message_id") == row.offer_message_id
        and context.get("open_chat_id") == row.offer_chat_id
        and row.deadline is not None
        and row.deadline > utcnow()
        and isinstance(open_id, str)
        and open_id
        and isinstance(tenant_key, str)
        and tenant_key
    ):
        return "provision_expired"
    row.offer_hash, row.open_id, row.tenant_key = None, open_id, tenant_key
    advance(row, "create")
    return "provision_started"


# ---- 看护进程：各步 ---------------------------------------------------------------------------


async def _owner(session: AsyncSession, row: AssistantLifecycle) -> User:
    owner = await session.get(User, row.owner_id)
    if owner is None:
        raise StepFailed("inactive")
    return owner


async def _bot(session: AsyncSession, row: AssistantLifecycle) -> Bot:
    """开通中的那条记录；本人的账号这期间被停用了就放弃（走停用把记录收掉）。"""
    if (await _owner(session, row)).status != "active":
        raise StepFailed("inactive")
    bot = await session.get(Bot, row.bot_id, with_for_update=True) if row.bot_id else None
    if bot is None or bot.owner_id != row.owner_id:
        raise StepFailed("bot_missing")
    return bot


async def step_offered(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """判断范围；不在范围就说一声并结束，在范围就发开通卡片（换一个新令牌）。"""
    reason = await eligibility.refusal(session, await _owner(session, row))
    if reason is not None:
        finish(row, "ineligible", reason)
        return [eligibility.REASONS[reason]]
    token = secrets.token_urlsafe(32)
    digest = token_hash(token)
    message_id, chat_id = await messenger.card(
        session,
        row.platform_app_id,
        row.platform_user_id,
        offer_card(token),
        key=f"assistant-offer:{row.id}:{digest[:16]}",
    )
    now = utcnow()
    row.offer_hash, row.offer_message_id, row.offer_chat_id = digest, message_id, chat_id
    row.deadline, row.next_attempt_at, row.attempts, row.updated_at = now + OFFER_TTL, None, 0, now
    return []


async def _pick(session: AsyncSession) -> tuple[RelayServer, RuntimeNode]:
    """按设置的 AI 顺序，在助理池里挑一个现在能跑、且与普通员工隔开的节点。"""
    for provider in await eligibility.backends(session):
        relay = await runtime.pick_pool_relay(session, provider)
        if relay is None or relay.runtime_node_id is None:
            continue
        node = await session.get(RuntimeNode, relay.runtime_node_id)
        if node is not None and await runtime.node_isolated(session, node):
            return relay, node
    raise StepRetry("no_pool_runtime")


async def _free_bot_key(session: AsyncSession, key: str) -> None:
    """以前停用的记录占着同一个 bot_key：给它换一个不会再用到的名字。还在用的记录不动。"""
    old = await session.scalar(select(Bot).where(Bot.bot_key == key).with_for_update())
    if old is None:
        return
    if old.enabled:
        raise StepFailed("already_provisioned")
    old.bot_key = f"{key}-x{old.id.hex[:8]}"
    await session.flush()


async def step_create(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """建助理记录（provisioning）与数据密钥，绑定池里的一个节点，定下全新的工作目录。一个事务。"""
    owner = await _owner(session, row)
    reason = await eligibility.refusal(session, owner)
    if reason is not None:
        raise StepFailed(reason)
    app = await session.get(PlatformApp, row.platform_app_id)
    if app is None or not is_assistant_app(app):
        raise StepFailed("app_unavailable")
    taken = await session.scalar(
        select(Bot.id).where(
            Bot.kind == "assistant", Bot.owner_id == owner.id, Bot.enabled.is_(True)
        )
    )
    if taken is not None:
        raise StepFailed("already_provisioned")
    relay, node = await _pick(session)
    model = default_model(relay.model_provider, await load_catalog(session))
    if model is None:
        raise StepFailed("no_model")
    try:
        credentials = encrypt_json(cipher, app_credentials(cipher, app), CREDENTIALS_AAD)
    except ValueError:
        raise StepFailed("app_unavailable") from None
    bot_id = uuid.uuid4()
    # 目录名带记录编号：每次开通都是一个从没用过的新目录，节点上留着的旧目录不会被接管。
    directory = str(PurePosixPath(node.workspace_root) / f"assistant-{bot_id.hex}")
    bot = new_assistant_bot(
        owner,
        app,
        relay_server_id=relay.id,
        model=model,
        working_dir=directory,
        credentials_enc=credentials,
    )
    bot.id = bot_id
    await _free_bot_key(session, bot.bot_key)
    try:
        await reserve_workspace(session, relay.id, directory, bot_id=bot_id)
    except ApiError:
        raise StepRetry("workspace_reserved") from None
    session.add(bot)
    await session.flush()
    # 上一次停用销毁过的密钥在这里签发下一代；旧一代加密的内容一律按已锁定处理。
    await keys.ensure_key(session, sealed.master(), owner.id)
    row.bot_id = bot.id
    advance(row, "init_workspace")
    return []


async def step_init_workspace(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """节点上建这个全新的目录。重试安全：助理专用实例对已带本记录标记的目录直接返回。"""
    bot = await _bot(session, row)
    relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    if relay is None:
        raise StepFailed("runtime_missing")
    try:
        async with asyncio.timeout(NODE_SECONDS):
            await call_agent(
                relay,
                cipher,
                "workspace-init",
                {
                    "working_dir": bot.working_dir,
                    "bot_id": str(bot.id),
                    "content": instruction_content(bot),
                },
            )
    except (AgentError, TimeoutError) as exc:
        raise StepRetry(getattr(exc, "code", None) or "workspace_init_failed") from None
    bot.workspace_state, bot.workspace_phase, bot.workspace_error = "ready", None, None
    advance(row, "start_grant")
    return []


def _grant_scope(bot: Bot, row: AssistantLifecycle, app_id: str) -> Scope:
    """发起与查询授权用的身份：飞书给的操作人（点开通时记下的），不来自任何请求内容。

    开通时还没有任何一轮；`begin_in_flow` 与 `authorization_status` 不读 `scope.task`。
    """
    return Scope(
        cast(Task, None),
        bot,
        row.owner_id,
        row.tenant_key or "",
        app_id,
        row.open_id or "",
        row.platform_user_id,
        row.offer_chat_id or "",
        None,
    )


async def _app_id(session: AsyncSession, row: AssistantLifecycle) -> str:
    app = await session.get(PlatformApp, row.platform_app_id)
    if app is None or not app.app_id:
        raise StepFailed("app_unavailable")
    return app.app_id


async def step_start_grant(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """发起飞书个人授权（全部权限、带离线访问），把授权卡片私信给本人。"""
    bot = await _bot(session, row)
    scope = _grant_scope(bot, row, await _app_id(session, row))
    try:
        begun = await personal.begin_in_flow(session, cipher, scope, GRANT_LEVEL)
    except personal.PersonalError as exc:
        if exc.code == "upstream_unavailable":
            raise StepRetry(exc.code) from None
        raise StepFailed("app_permission_missing") from None
    row.issued += 1
    await messenger.card(
        session,
        row.platform_app_id,
        row.platform_user_id,
        grant_card(begun["authorization_url"]),
        key=f"assistant-grant:{row.id}:{row.issued}",
    )
    grant = await session.get(FeishuPersonalGrant, (bot.id, row.owner_id))
    advance(row, "await_grant", at=utcnow() + GRANT_POLL)
    row.deadline = grant.pending_expires_at if grant is not None else None
    return []


def _connected(grant: FeishuPersonalGrant | None) -> bool:
    return (
        grant is not None
        and grant.status == "connected"
        and bool(grant.token_enc)
        and grant.authorization_level == GRANT_LEVEL
    )


async def step_await_grant(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """替本人查授权结果，完成就开通好了（记录变成 active）；链接过期再发，最多三次。"""
    bot = await _bot(session, row)
    now = utcnow()
    key = (bot.id, row.owner_id)
    grant = await session.get(FeishuPersonalGrant, key, populate_existing=True)
    if (
        not _connected(grant)
        and grant is not None
        and grant.status == "pending"
        and grant.pending_expires_at is not None
        and grant.pending_expires_at > now
        and (grant.next_poll_at is None or grant.next_poll_at <= now)
    ):
        scope = _grant_scope(bot, row, await _app_id(session, row))
        try:
            await personal.authorization_status(session, cipher, scope)
        except personal.PersonalError as exc:
            if exc.code == "upstream_unavailable":
                raise StepRetry(exc.code) from None
            # 授权被拒或身份对不上：授权行已清空，下面按「链接已失效」再发一次。
        grant = await session.get(FeishuPersonalGrant, key, populate_existing=True)
    if _connected(grant):
        bot.assistant_state = "active"
        finish(row, "done")
        return [welcome_text()]
    if (
        grant is not None
        and grant.status == "pending"
        and grant.pending_expires_at is not None
        and grant.pending_expires_at > now
    ):
        wait = max(GRANT_POLL, timedelta(seconds=grant.poll_interval or 5))
        row.next_attempt_at, row.updated_at = now + wait, now
        return []
    if row.issued >= GRANT_ROUNDS:
        raise StepFailed("grant_not_completed")
    advance(row, "start_grant")
    return []


STEPS = {
    "offered": step_offered,
    "create": step_create,
    "init_workspace": step_init_workspace,
    "start_grant": step_start_grant,
    "await_grant": step_await_grant,
}


def failure_text(code: str) -> str:
    return FAILURES.get(code, GENERIC_FAILURE)
```

`coreman/core/assistants/lifecycle_runner.py` 整个文件换成（接上开通：过期卡片 `_expire_offers`、开通的步骤表、放弃开通时走停用收拾、`PROVISION_MAX_ATTEMPTS`）：

```python
"""看护进程推进开通与停用：每一步一个事务，失败原地重试，放弃时收拾干净。

由 scheduler 单独的一条循环调用（不在与普通员工共用的那一轮看护里）：这里有对节点与飞书的网络调用，
慢了、卡了、出错了都只影响个人助理的开通与停用。每一步另有 `STEP_SECONDS` 的总时限。

一轮做四件事：结掉过期没点的开通卡片；把账号已停用的成员的助理送进停用（先宽限）；推进到期的流程；
删一批已登记的旧工作目录。给本人的消息在那一步提交之后才发，发不出去只记一笔：例如授权刚完成，
不能因为欢迎语没发出去就把这一步（和刚拿到的令牌）回滚掉。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants import deprovisioning, keys, provisioning, retired_dirs
from coreman.core.assistants.lifecycle import (
    PROVISION_MAX_ATTEMPTS,
    Messenger,
    StepFailed,
    StepRetry,
    backoff,
    finish,
)
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantLifecycle, Bot, User
from coreman.core.logging import get_logger

log = get_logger(__name__)
STEP_SECONDS = 150
BATCH = 10
DEPARTURE_BATCH = 20
# 一条通知：（应用、飞书 user_id、流程编号、文本）。
Notice = tuple[uuid.UUID, str, uuid.UUID, str]


async def _expire_offers(factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    async with factory() as session:
        result = await session.execute(
            update(AssistantLifecycle)
            .where(
                AssistantLifecycle.finished_at.is_(None),
                AssistantLifecycle.action == "provision",
                AssistantLifecycle.step == "offered",
                AssistantLifecycle.next_attempt_at.is_(None),
                AssistantLifecycle.deadline < now,
            )
            .values(finished_at=now, outcome="expired", offer_hash=None, updated_at=now)
        )
        await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


async def _departures(factory: async_sessionmaker[AsyncSession]) -> int:
    """账号已停用的成员：他的助理进入停用（宽限期内账号恢复就作罢）。"""
    async with factory() as session:
        bot_ids = list(
            await session.scalars(
                select(Bot.id)
                .join(User, User.id == Bot.owner_id)
                .where(
                    Bot.kind == "assistant",
                    Bot.enabled.is_(True),
                    Bot.assistant_state == "active",
                    User.status == "disabled",
                )
                .limit(DEPARTURE_BATCH)
            )
        )
    started = 0
    for bot_id in bot_ids:
        try:
            async with factory() as session:
                bot = await session.get(Bot, bot_id)
                if bot is not None:
                    await deprovisioning.request(session, bot, trigger="member_left", actor_id=None)
                    await session.commit()
                    started += 1
        except Exception as exc:  # noqa: BLE001 一位出错不挡别人
            log.warning("assistant_departure_failed", error=type(exc).__name__)
    return started


async def _give_up(session: AsyncSession, row: AssistantLifecycle, code: str) -> list[str]:
    """这一步再也做不成：开通就此放弃并把已建的东西送去停用；停用（只会是记录没了）直接销毁密钥。"""
    if row.action == "deprovision":
        await keys.destroy_key(session, row.owner_id)
        finish(row, "failed", code)
        return []
    finish(row, "failed", code)
    bot = await session.get(Bot, row.bot_id) if row.bot_id else None
    if bot is not None and bot.assistant_state == "provisioning":
        await session.flush()
        await deprovisioning.request(session, bot, trigger="abandoned", actor_id=None)
    return [provisioning.failure_text(code)]


async def _settle_failure(
    factory: async_sessionmaker[AsyncSession],
    row_id: uuid.UUID,
    code: str,
    *,
    permanent: bool,
    now: datetime,
) -> list[str]:
    """步骤的事务已回滚：另开一个事务记下失败，或（再也做不成时）放弃。"""
    async with factory() as session:
        row = await session.get(AssistantLifecycle, row_id, with_for_update=True)
        if row is None or row.finished_at is not None:
            return []
        row.attempts += 1
        row.error_code, row.updated_at = code, now
        if permanent or (row.action == "provision" and row.attempts >= PROVISION_MAX_ATTEMPTS):
            texts = await _give_up(session, row, code)
        else:
            row.next_attempt_at = now + backoff(row.attempts)
            texts = []
        await session.commit()
        return texts


async def _run_one(
    factory: async_sessionmaker[AsyncSession],
    cipher: Cipher,
    messenger: Messenger,
    row_id: uuid.UUID,
    now: datetime,
) -> list[Notice]:
    texts: list[str] = []
    async with factory() as session:
        row = await session.scalar(
            select(AssistantLifecycle)
            .where(
                AssistantLifecycle.id == row_id,
                AssistantLifecycle.finished_at.is_(None),
                AssistantLifecycle.next_attempt_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return []
        target = (row.platform_app_id, row.platform_user_id)
        steps = provisioning.STEPS if row.action == "provision" else deprovisioning.STEPS
        step = steps[row.step]
        failure: tuple[str, bool] | None = None
        try:
            async with asyncio.timeout(STEP_SECONDS):
                texts = await step(session, cipher, row, messenger)
            await session.commit()
        except StepFailed as exc:
            failure = (exc.code, True)
        except StepRetry as exc:
            failure = (exc.code, False)
        except Exception as exc:  # noqa: BLE001 意外按暂时失败处理，只记类型
            failure = (type(exc).__name__, False)
        if failure is not None:
            await session.rollback()
    if failure is not None:
        log.warning("assistant_lifecycle_step_failed", step=step.__name__, code=failure[0])
        texts = await _settle_failure(factory, row_id, failure[0], permanent=failure[1], now=now)
    return [(target[0], target[1], row_id, text) for text in texts]


async def _notify(
    factory: async_sessionmaker[AsyncSession], messenger: Messenger, notices: list[Notice]
) -> None:
    for index, (app_id, user_id, row_id, text) in enumerate(notices):
        if not user_id:
            continue
        try:
            async with factory() as session:
                await messenger.text(
                    session, app_id, user_id, text, key=f"assistant-notice:{row_id}:{index}"
                )
        except Exception as exc:  # noqa: BLE001 通知发不出去不影响流程
            log.warning("assistant_lifecycle_notice_failed", error=type(exc).__name__)


async def tick(
    factory: async_sessionmaker[AsyncSession],
    cipher: Cipher,
    now: datetime,
    *,
    messenger: Messenger | None = None,
) -> dict[str, int]:
    owned = messenger is None
    messenger = messenger or Messenger(cipher)
    counts = {"assistant_lifecycle_steps": 0}
    try:
        counts["assistant_offers_expired"] = await _expire_offers(factory, now)
        counts["assistant_departures"] = await _departures(factory)
        async with factory() as session:
            due = list(
                await session.scalars(
                    select(AssistantLifecycle.id)
                    .where(
                        AssistantLifecycle.finished_at.is_(None),
                        AssistantLifecycle.next_attempt_at <= now,
                    )
                    .order_by(AssistantLifecycle.next_attempt_at)
                    .limit(BATCH)
                )
            )
        for row_id in due:
            try:
                notices = await _run_one(factory, cipher, messenger, row_id, now)
            except Exception as exc:  # noqa: BLE001 一条流程出错不挡别的
                log.warning("assistant_lifecycle_failed", error=type(exc).__name__)
                continue
            counts["assistant_lifecycle_steps"] += 1
            await _notify(factory, messenger, notices)
        counts.update(await retired_dirs.tick(factory, cipher, now))
    finally:
        if owned:
            await messenger.aclose()
    return counts
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_provisioning.py tests/integration/test_assistant_deprovisioning.py tests/api/test_feishu_inflow.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: 全部通过（开通用例 8 条）。

普通机器人不受影响：开通只经助理应用的网关与看护进程，普通员工的任何路径都不调用 `provisioning`；`tests/api/test_feishu_inflow.py`（普通机器人的飞书个人授权）照常通过：`begin_in_flow` 与 `authorization_status` 没有改动。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants/provisioning.py coreman/core/assistants/lifecycle_runner.py tests/integration/test_assistant_provisioning.py
git commit -m "个人助理：开通流程（范围、记录与密钥、节点目录、飞书授权）由看护进程推进，任何一步放弃都收拾干净"
```

---


### Task 10: 网关：没有助理的人私聊时登记开通，开通卡片的点击在网关里应答

**Files:**
- Modify: `coreman/runtime/gateway_feishu/assistant_inbound.py`（`on_unprovisioned`）
- Modify: `coreman/runtime/gateway_feishu/reply_buttons.py`（`Outcome` 加两个值）、`coreman/runtime/gateway_feishu/channel.py`（两条 toast）
- Test: `tests/integration/test_assistant_gateway_provisioning.py`（新建）

**Interfaces:**
- Consumes: Task 9 的 `request_offer`、`accept_offer`；1B 的 `AssistantInbound.accept` / `on_unprovisioned`
- Produces: `AssistantInbound.on_unprovisioned(...) -> Outcome | None`；`Outcome` 新增 `provision_started`、`provision_expired`

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_gateway_provisioning.py`（新建）：

```python
"""网关接上开通：没有助理的人私聊（或打开私聊）就登记开通，点开通卡片得到应答；出错不挡应答。"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import provisioning
from coreman.core.db.models import AssistantLifecycle
from coreman.core.db.session import make_session_factory
from coreman.core.platforms.feishu import FeishuClient
from coreman.runtime.gateway_feishu.assistant_inbound import AssistantInbound
from coreman.runtime.gateway_feishu.channel import TOASTS
from tests.integration.lifecycle_helpers import (
    APP_ID,
    CIPHER,
    FakeMessenger,
    assistant_app,
    click_event,
    flow_of,
    install,
    member,
    message_event,
    pool_relay,
    tick,
)

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


def gateway(db_engine: AsyncEngine, app_id: uuid.UUID) -> AssistantInbound:
    """助理应用连接的入站一半；它自己的飞书客户端在开通里用不到。"""
    http = httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )

    async def no_fence() -> None:
        return None

    return AssistantInbound(
        factory=make_session_factory(db_engine),
        client=FeishuClient(APP_ID, "synthetic", http=http),
        app=app_id,
        feishu_app_id=APP_ID,
        instance_id="gw-a",
        generation=1,
        cipher=CIPHER,
        fence=no_fence,
    )


def entered(pid: str) -> dict:
    return {
        "header": {
            "event_id": f"ev-{uuid.uuid4().hex}",
            "event_type": "im.chat.access_event.bot_p2p_chat_entered_v1",
            "app_id": APP_ID,
        },
        "event": {"operator_id": {"user_id": pid}},
    }


async def count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(AssistantLifecycle)) or 0)


async def test_a_message_or_opening_the_chat_registers_an_offer(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    app = await assistant_app(db_session)
    owner = await member(db_session)
    other = await member(db_session, login="lisi", pid="lisi")
    await db_session.commit()
    inbound = gateway(db_engine, app.id)
    assert await inbound.accept(message_event()) is None
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "offered" and flow.next_attempt_at is not None
    assert flow.platform_user_id == "zhangsan" and flow.platform_app_id == app.id
    assert await inbound.accept(entered("lisi")) is None
    assert (await flow_of(db_session, other.id)) is not None
    # 同一个人再发：仍只有一条进行中的流程。
    await inbound.accept(message_event())
    assert await count(db_session) == 2


async def test_unknown_people_and_group_messages_register_nothing(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    app = await assistant_app(db_session)
    await member(db_session)
    await db_session.commit()
    inbound = gateway(db_engine, app.id)
    assert await inbound.accept(message_event(pid="nobody")) is None
    group = message_event()
    group["event"]["message"]["chat_type"] = "group"
    assert await inbound.accept(group) is None
    assert await count(db_session) == 0


async def test_the_card_click_is_answered_through_the_gateway(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    app = await assistant_app(db_session)
    owner = await member(db_session)
    await pool_relay(db_session)
    await db_session.commit()
    inbound, messenger = gateway(db_engine, app.id), FakeMessenger()
    await inbound.accept(message_event())
    await tick(make_session_factory(db_engine), messenger)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.offer_message_id is not None
    token = messenger.token()
    assert await inbound.accept(click_event(token, "om_forwarded")) == "provision_expired"
    assert await inbound.accept(click_event(token, flow.offer_message_id)) == "provision_started"
    assert TOASTS["provision_started"]["type"] == "success"
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "create"


async def test_a_failing_registration_never_blocks_the_gateway(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = await assistant_app(db_session)
    await member(db_session)
    await db_session.commit()

    async def broken(*_args: object, **_kw: object) -> None:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(provisioning, "request_offer", broken)
    monkeypatch.setattr(provisioning, "accept_offer", broken)
    inbound = gateway(db_engine, app.id)
    assert await inbound.accept(message_event()) is None
    assert await inbound.accept(click_event("t", "om_1")) is None
    assert await count(db_session) == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_gateway_provisioning.py -q`
Expected: FAIL：私聊之后没有流程行（`flow is not None` 断言失败），点击返回 None 而不是 `provision_started`，`TOASTS` 没有 `provision_started`。

- [ ] **Step 3: 实现**

`coreman/runtime/gateway_feishu/assistant_inbound.py`：把

```python
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants import sealed
from coreman.core.assistants.routing import assistant_for
from coreman.core.chat.identity import resolve_speaker
```

换成

```python
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants import provisioning, sealed
from coreman.core.assistants.routing import assistant_for
from coreman.core.chat.identity import resolve_speaker
```

`coreman/runtime/gateway_feishu/assistant_inbound.py`：把

```python
            )
            if bot is None:
                await self.on_unprovisioned(
                    session,
                    platform_user_id=found.platform_user_id,
```

换成

```python
            )
            if bot is None:
                unprovisioned = await self.on_unprovisioned(
                    session,
                    platform_user_id=found.platform_user_id,
```

`coreman/runtime/gateway_feishu/assistant_inbound.py`：把

```python
                )
                await session.commit()
                return None
            # 之后经手的是这个人的私聊内容：这段处理里的日志不带异常文字。
            await sealed.guard_logs(session, bot.id)
```

换成

```python
                )
                await session.commit()
                return unprovisioned
            # 之后经手的是这个人的私聊内容：这段处理里的日志不带异常文字。
            await sealed.guard_logs(session, bot.id)
```

`coreman/runtime/gateway_feishu/assistant_inbound.py`：把

```python
        user_id: uuid.UUID | None,
        raw: dict[str, Any],
    ) -> None:
        """发送人没有启用的助理记录。本期只记日志并丢弃；开通卡片由开通与停用那一期接在这里。

        `user_id` 为 None 表示通讯录里认不出这个人。调用点在应答窗口内，返回后由调用方提交。
        """
        header = raw.get("header")
        log.info(
            "assistant_unprovisioned",
            app=str(self.app),
            known_user=user_id is not None,
            event_type=header.get("event_type") if isinstance(header, dict) else None,
        )

    async def leave_group(self, chat_id: str, platform_user_id: str, event_id: str) -> None:
```

换成

```python
        user_id: uuid.UUID | None,
        raw: dict[str, Any],
    ) -> Outcome | None:
        """发送人没有启用的助理记录：私聊或打开私聊时登记开通（看护进程判断范围、发开通卡片），
        点开通卡片时核对并开始开通。只写库，返回卡片回调的应答结果。

        `user_id` 为 None 表示通讯录里认不出这个人：不开通。调用点在应答窗口内，返回后由调用方
        提交；这里的任何失败都只丢掉这一次登记（保存点回滚），不挡应答。
        """
        header = raw.get("header")
        event_type = header.get("event_type") if isinstance(header, dict) else None
        if user_id is None:
            log.info("assistant_unprovisioned", app=str(self.app), known_user=False)
            return None
        try:
            async with session.begin_nested():
                if event_type == "card.action.trigger":
                    outcome = await provisioning.accept_offer(
                        session, app_id=self.app, owner_id=user_id, raw=raw
                    )
                    return cast("Outcome", outcome)
                await provisioning.request_offer(
                    session,
                    app_id=self.app,
                    owner_id=user_id,
                    platform_user_id=platform_user_id,
                )
        except Exception as exc:  # noqa: BLE001 开通登记失败不能挡住应答
            log.warning("assistant_provision_request_failed", error=type(exc).__name__)
        return None

    async def leave_group(self, chat_id: str, platform_user_id: str, event_id: str) -> None:
```

`coreman/runtime/gateway_feishu/reply_buttons.py`：把

```python
    "busy_drop",
    "live_progress",
]
_BUSY_KEYS = frozenset(
```

换成

```python
    "busy_drop",
    "live_progress",
    # 个人助理的开通卡片（没有助理记录时，由 AssistantInbound.on_unprovisioned 处理）。
    "provision_started",
    "provision_expired",
]
_BUSY_KEYS = frozenset(
```

`coreman/runtime/gateway_feishu/channel.py`：把

```python
    "busy_drop": {"type": "info", "content": "好的，不处理了"},
    "live_progress": {"type": "info", "content": "正在查询进度…"},
}
DEFAULT_TOAST = {"type": "info", "content": "✓"}
```

换成

```python
    "busy_drop": {"type": "info", "content": "好的，不处理了"},
    "live_progress": {"type": "info", "content": "正在查询进度…"},
    "provision_started": {"type": "success", "content": "正在开通，好了会在这里告诉你"},
    "provision_expired": {"type": "warning", "content": "这张卡片已失效，给我发条消息会收到新的"},
}
DEFAULT_TOAST = {"type": "info", "content": "✓"}
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_gateway_provisioning.py tests/integration/test_assistant_gateway_inbound.py tests/integration/test_feishu_app_child.py tests/unit/test_feishu_channel.py -q`
Expected: 全部通过。

普通机器人不受影响：改动只在助理应用的连接里（`AssistantInbound` 只为带 `assistant` 能力的应用实例化）；普通机器人的子进程不经过它。`TOASTS` 与 `Outcome` 只多了两个值，`tests/integration/test_assistant_gateway_inbound.py` 与普通机器人的卡片回调用例照常通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/runtime/gateway_feishu/assistant_inbound.py coreman/runtime/gateway_feishu/reply_buttons.py coreman/runtime/gateway_feishu/channel.py tests/integration/test_assistant_gateway_provisioning.py
git commit -m "个人助理：没有助理的人私聊助理应用时登记开通，开通卡片的点击在网关里核对并应答"
```

---


### Task 11: 所有者在私聊里停用自己的助理

**Files:**
- Create: `coreman/runtime/worker/assistant_stop.py`
- Modify: `coreman/runtime/worker/chat/handler.py`（助理的轮次先看是不是停用命令）、`coreman/runtime/worker/card_actions.py`（`personal:` 卡片按 `event_key` 分到停用确认）
- Test: `tests/integration/test_assistant_stop_command.py`（新建）

**Interfaces:**
- Consumes: Task 8 的 `deprovisioning.request`；`CardActionHandler` 的出站核对；`Cipher.mac(purpose, message)`
- Produces: `assistant_stop.intercept(ctx, intake) -> bool`、`assistant_stop.handle(session, ctx, bot, inbound, action) -> str`、`assistant_stop.confirm_card(...)`、`EVENT_KEY = "assistant_stop"`、`TTL_SECONDS = 600`

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_stop_command.py`（新建）：

```python
"""所有者在私聊里停用助理：「停用助理」→ 确认卡片 → 只有所有者带着平台签的未过期令牌才能确认。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import runtime
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Bot, InboundEvent, OutboxItem, Task, User
from coreman.runtime.worker.card_actions import CardActionHandler
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.lifecycle_helpers import active_assistant, flow_of, install
from tests.integration.test_chat_handler import chat_task, run
from tests.integration.worker_helpers import build_ctx, seed_bot

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


async def stop_card(
    db_engine: AsyncEngine, db_session: AsyncSession, bot: Bot
) -> tuple[Task, FakeRelay, OutboxItem]:
    """所有者在私聊里发「停用助理」，这一轮跑完：返回任务、模型替身与发出去的确认卡片。"""
    task = await chat_task(
        db_session, bot, "停用助理", sender="owner_pid", chat_type="single", chat_id="oc_private"
    )
    task.status = "running"
    await db_session.commit()
    relay = FakeRelay("normal")
    await ChatTaskHandler().run(
        build_ctx(db_engine, task, relay_client_factory=lambda _r: relay.client())
    )
    sent = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.bot_id == bot.id, OutboxItem.kind == "send")
    )
    assert sent is not None
    done = await db_session.get(Task, task.id, populate_existing=True)
    assert done is not None
    return done, relay, sent


async def click(
    db_session: AsyncSession,
    bot: Bot,
    sent: OutboxItem,
    *,
    who: str = "owner_pid",
    chat: str = "oc_private",
    **edits: Any,
) -> Task:
    """点了确认卡片上的按钮：卡片已发到私聊（出站记录 sent），回调按网关的样子落库并排成任务。"""
    sent.status = "sent"
    sent.payload = {**sent.payload, "_feishu_message_id": "om_stop_card"}
    value = sent.payload["card"]["body"]["elements"][1]["behaviors"][0]["value"]
    action = {**value, "card_type": "form", "selected": {}, **edits}
    event = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=f"action:{uuid.uuid4().hex}",
        kind="card_action",
        chat_type="single",
        chat_id=chat,
        sender_platform_user_id=who,
        sender_open_id=f"ou_{who}",
        reply_context={"chat_id": chat, "message_id": "om_stop_card"},
        payload={},
    )
    db_session.add(event)
    await db_session.flush()
    task = await tasks.enqueue(
        db_session,
        NewTask(
            bot_id=bot.id,
            kind="card_action",
            lane="fast",
            payload={"card_action": action, "platform_user_id": who},
            session_key=chat,
            inbound_event_id=event.id,
            dedupe_key=f"click:{event.id}",
        ),
    )
    assert task is not None
    task.status = "running"
    await db_session.commit()
    return task


async def test_the_owner_confirms_and_the_assistant_starts_deprovisioning(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    done, relay, sent = await stop_card(db_engine, db_session, bot)
    # 命令不经模型：这一轮没有派给节点，卡片发到了私聊。
    assert done.status == "succeeded" and relay.requests == []
    assert sent.payload["card"]["task_id"] == f"personal:{done.id}"
    assert sent.target == {"chat_id": "oc_private"}
    task = await click(db_session, bot, sent)
    await CardActionHandler().run(build_ctx(db_engine, task))
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "deprovisioning" and bot.enabled
    assert await runtime.turn_blocker(db_session, bot) == "deprovisioning"
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.action == "deprovision" and flow.trigger == "owner"
    update = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.bot_id == bot.id, OutboxItem.kind == "card_update")
    )
    assert update is not None and "正在停用" in str(update.payload)


async def test_an_expired_card_only_says_so(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from coreman.runtime.worker import assistant_stop

    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    real = time.time()
    monkeypatch.setattr(assistant_stop.time, "time", lambda: real - assistant_stop.TTL_SECONDS - 5)
    _, _, sent = await stop_card(db_engine, db_session, bot)
    monkeypatch.setattr(assistant_stop.time, "time", lambda: real)
    task = await click(db_session, bot, sent)
    await CardActionHandler().run(build_ctx(db_engine, task))
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "active"
    assert await flow_of(db_session, owner.id) is None


async def test_only_the_owner_with_a_fresh_signed_card_can_stop_it(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    _, _, sent = await stop_card(db_engine, db_session, bot)
    stranger = User(login_name="lisi", display_name="李四")
    db_session.add(stranger)
    await db_session.commit()
    expires, _, mac = sent.payload["card"]["body"]["elements"][1]["behaviors"][0]["value"][
        "level"
    ].partition(".")
    for edits, who in (
        ({}, "lisi"),  # 不是所有者
        ({"level": f"{expires}.{'0' * 64}"}, "owner_pid"),  # 令牌不是平台签的
        ({"level": f"{int(expires) + 60}.{mac}"}, "owner_pid"),  # 改了到期时刻
        ({"event_key": "personal_schedule"}, "owner_pid"),  # 别的卡片
    ):
        task = await click(db_session, bot, sent, who=who, **edits)
        await CardActionHandler().run(build_ctx(db_engine, task))
        bot = await db_session.get(Bot, bot.id, populate_existing=True)
        assert bot is not None and bot.assistant_state == "active"
    assert await flow_of(db_session, owner.id) is None


async def test_stop_is_not_a_command_for_ordinary_bots(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """普通员工收到「停用助理」就是一句普通的话：照常派给模型。"""
    bot, _, _ = await seed_bot(db_session)
    task = await chat_task(db_session, bot, "停用助理")
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    assert len(relay.requests) == 1
    assert (await tasks.get(db_session, task.id)).status == "succeeded"  # type: ignore[union-attr]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_stop_command.py -q`
Expected: FAIL：「停用助理」被当成普通的一句话派给了模型（`relay.requests == []` 断言失败），没有确认卡片。

- [ ] **Step 3: 实现**

`coreman/runtime/worker/assistant_stop.py`（新建）：

```python
"""所有者在私聊里停用自己的助理：发「停用助理」→ 确认卡片 → 点「确认停用」→ 进入停用流程。

文字命令在这一轮派出之前就截下（不经模型，开通没走完或运行时暂停时也能用）。确认卡片走这条记录的
出站箱，卡片回调先过 `CardActionHandler` 的核对（卡片确实由这条记录发到这个会话），这里再核对三样：
记录是个人助理、点的人就是所有者（按飞书给的操作人认）、卡片上的令牌没过期且是平台签的（主密钥
派生的 HMAC，绑定记录、发卡片的那一轮与到期时刻）。重复点、转发出去的卡片都不会多做什么。
"""

from __future__ import annotations

import hmac
import time
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import deprovisioning
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import outbox, tasks
from coreman.core.chat.identity import resolve_speaker
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, InboundEvent
from coreman.runtime.worker.chat.models import Intake
from coreman.runtime.worker.context import TaskContext

COMMANDS = frozenset({"停用助理", "/停用助理"})
EVENT_KEY = "assistant_stop"
TTL_SECONDS = 600


def _mac(cipher: Cipher, bot_id: uuid.UUID, task_id: int, expires: int) -> str:
    return cipher.mac("assistant-stop", f"{bot_id}:{task_id}:{expires}")


def confirm_card(cipher: Cipher, bot_id: uuid.UUID, task_id: int, now: float) -> dict[str, Any]:
    expires = int(now) + TTL_SECONDS
    card_task = f"personal:{task_id}"
    return {
        "schema": "2.0",
        "header": {"template": "red", "title": {"tag": "plain_text", "content": "停用个人助理"}},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "停用后：飞书授权撤销，待批的审批单撤回，加密保存的对话与个人数据永久不可读，"
                        "工作目录从运行时上删除。之后可以重新开通，但以前的内容回不来。"
                        "\n确认要停用吗？（10 分钟内有效）"
                    ),
                },
                {
                    "tag": "button",
                    "type": "danger",
                    "text": {"tag": "plain_text", "content": "确认停用"},
                    "behaviors": [
                        {
                            "type": "callback",
                            "value": {
                                "task_id": card_task,
                                "event_key": EVENT_KEY,
                                "level": f"{expires}.{_mac(cipher, bot_id, task_id, expires)}",
                            },
                        }
                    ],
                },
            ]
        },
        "task_id": card_task,
    }


async def intercept(ctx: TaskContext, intake: Intake) -> bool:
    """个人助理的私聊里发了停用命令：发确认卡片、结束这一轮，返回真。其余消息返回假。"""
    if intake.text.strip() not in COMMANDS:
        return False
    async with ctx.session_factory() as session:
        if intake.speaker.user_id is not None and intake.speaker.user_id == intake.bot.owner_id:
            await outbox.add(
                session,
                bot_id=intake.bot.id,
                platform="feishu",
                kind="send",
                dedupe_key=f"assistant-stop:{ctx.task.id}",
                target={"chat_id": intake.chat_id},
                payload={"card": confirm_card(ctx.cipher, intake.bot.id, ctx.task.id, time.time())},
            )
        await tasks.finish(
            session,
            ctx.task.id,
            status="succeeded",
            result={"assistant": "stop_offered"},
            only_active=True,
            bot_id=intake.bot.id,
        )
        await session.commit()
    return True


async def _update(session: AsyncSession, bot: Bot, inbound: InboundEvent, text: str) -> None:
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="card_update",
        dedupe_key=f"{inbound.id}:card_update",
        target={"chat_id": inbound.chat_id, "message_id": inbound.reply_context.get("message_id")},
        payload={
            "card": {
                "schema": "2.0",
                "header": {"title": {"tag": "plain_text", "content": "停用个人助理"}},
                "body": {"elements": [{"tag": "markdown", "content": text}]},
            }
        },
    )


async def handle(
    session: AsyncSession, ctx: TaskContext, bot: Bot, inbound: InboundEvent, action: dict[str, Any]
) -> str:
    """确认卡片的回调；调用方（`CardActionHandler`）已核对卡片由这条记录发到这个会话。"""
    if not is_assistant(bot):
        return "ignored"
    try:
        task_id = int(str(action.get("task_id") or "").removeprefix("personal:"))
        expires_raw, _, mac = str(action.get("level") or "").partition(".")
        expires = int(expires_raw)
    except ValueError:
        return "ignored"
    if not hmac.compare_digest(mac, _mac(ctx.cipher, bot.id, task_id, expires)):
        return "ignored"
    speaker = await resolve_speaker(
        session, platform="feishu", platform_user_id=str(inbound.sender_platform_user_id or "")
    )
    if speaker.user_id is None or speaker.user_id != bot.owner_id:
        return "ignored"
    if expires < time.time():
        await _update(session, bot, inbound, "这张卡片已过期；还要停用的话再发一次「停用助理」。")
        return "assistant_stop_expired"
    await deprovisioning.request(session, bot, trigger="owner", actor_id=bot.owner_id)
    await _update(session, bot, inbound, "正在停用，做完会在这里告诉你。")
    return "assistant_stopping"
```

`coreman/runtime/worker/chat/handler.py`：把

```python
        intake, relay, parts = resolved
        if is_assistant(intake.bot):
            from coreman.runtime.worker.chat import assistant_runtime

            # 个人助理只在能隔离运行的节点上跑：所在运行时不行就不派，私聊告诉所有者。
            if await assistant_runtime.paused(ctx, intake):
```

换成

```python
        intake, relay, parts = resolved
        if is_assistant(intake.bot):
            from coreman.runtime.worker import assistant_stop
            from coreman.runtime.worker.chat import assistant_runtime

            # 所有者发「停用助理」：发确认卡片，不派这一轮（开通没走完、运行时暂停时也能停用）。
            if await assistant_stop.intercept(ctx, intake):
                return None
            # 个人助理只在能隔离运行的节点上跑：所在运行时不行就不派，私聊告诉所有者。
            if await assistant_runtime.paused(ctx, intake):
```

`coreman/runtime/worker/card_actions.py`：把

```python
            if bot.platform == "feishu" and task_id.startswith("personal:"):
                from coreman.core.feishu_docs import confirm
                from coreman.runtime.worker.group_write_cards import handle as handle_group_write
                from coreman.runtime.worker.personal_cards import handle_schedule, handle_selection
```

换成

```python
            if bot.platform == "feishu" and task_id.startswith("personal:"):
                from coreman.core.feishu_docs import confirm
                from coreman.runtime.worker import assistant_stop
                from coreman.runtime.worker.group_write_cards import handle as handle_group_write
                from coreman.runtime.worker.personal_cards import handle_schedule, handle_selection
```

`coreman/runtime/worker/card_actions.py`：把

```python
                    inflow.UPGRADE_EVENT: inflow.handle_upgrade,
                    confirm.CARD_EVENT: handle_group_write,
                }.get(str(action.get("event_key")), handle_selection)
                result = await handle(session, ctx, bot, inbound, action)
```

换成

```python
                    inflow.UPGRADE_EVENT: inflow.handle_upgrade,
                    confirm.CARD_EVENT: handle_group_write,
                    assistant_stop.EVENT_KEY: assistant_stop.handle,
                }.get(str(action.get("event_key")), handle_selection)
                result = await handle(session, ctx, bot, inbound, action)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_stop_command.py tests/api/test_feishu_personal_cards.py tests/integration/test_card_actions.py tests/integration/test_assistant_standard_overhead.py -q`
Expected: 全部通过。

普通机器人不受影响：`test_stop_is_not_a_command_for_ordinary_bots`（普通员工收到「停用助理」照常派给模型）；`handler.py` 的新分支在已有的 `is_assistant(intake.bot)` 里面；`card_actions.py` 只多一个 `event_key`，`tests/api/test_feishu_personal_cards.py` 照常通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/runtime/worker/assistant_stop.py coreman/runtime/worker/chat/handler.py coreman/runtime/worker/card_actions.py tests/integration/test_assistant_stop_command.py
git commit -m "个人助理：所有者在私聊里发「停用助理」并确认后停用"
```

---


### Task 12: 管理员：助理列表与停用；启停开关与删除不能绕过停用

**Files:**
- Create: `coreman/api/routers/assistants_admin.py`
- Modify: `coreman/api/main.py`（挂路由）
- Modify: `coreman/core/assistants/records.py`（两句给管理台看的提示）
- Modify: `coreman/api/routers/bots.py`（删除）、`coreman/api/routers/bots_extra.py`（启停）
- Test: `tests/api/test_assistant_admin.py`（新建）

**Interfaces:**
- Consumes: Task 8 的 `deprovisioning.request`
- Produces:
  - `GET /api/admin/assistants`（platform_admin）：`{items: [{bot_id, state, enabled, created_at, owner: {id, login_name, display_name}, relay: {id, name} | null, node: {id, name, online} | null, lifecycle: {action, step, trigger, error_code} | null, directories_pending_removal}]}`
  - `POST /api/admin/assistants/{bot_id}/deprovision`（platform_admin），请求 `{"confirm": true}`，答复 `{bot_id, state}`
  - `records.ASSISTANT_TOGGLE_MESSAGE`、`records.ASSISTANT_DELETE_MESSAGE`

- [ ] **Step 1: 写失败测试**

`tests/api/test_assistant_admin.py`（新建）：

```python
"""管理员看个人助理与停用：只有元数据；停用只回状态；启停与删除不能绕过停用流程。"""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.records import new_assistant_bot
from coreman.core.db.models import AssistantLifecycle, AuditLog, Bot, PlatformApp, User
from tests.api.conftest import login_as, login_existing
from tests.fakes.runtime_node import add_node_relay

SECRET = "机密-片段-7f3a"


async def _assistant(db_session: AsyncSession, owner: User, *, state: str = "active") -> Bot:
    app = PlatformApp(
        platform="feishu",
        name=f"个人助理-{uuid.uuid4().hex[:6]}",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="x",
    )
    db_session.add(app)
    relay = await add_node_relay(db_session, name="pool-1")
    bot = new_assistant_bot(
        owner, app, relay_server_id=relay.id, model="m", working_dir="/w/a", credentials_enc=""
    )
    bot.assistant_state = state
    bot.description = SECRET
    db_session.add(bot)
    await db_session.commit()
    return bot


async def test_the_list_shows_who_where_and_what_state_only(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="member", display_name="张三")
    bot = await _assistant(db_session, owner)
    assert (await client.get("/api/admin/assistants")).status_code == 403
    await login_as(client, db_session, role="platform_admin")
    response = await client.get("/api/admin/assistants")
    assert response.status_code == 200
    [item] = response.json()["data"]["items"]
    assert item["bot_id"] == str(bot.id) and item["state"] == "active"
    assert item["owner"]["display_name"] == "张三" and item["node"]["name"] == "pool-1"
    assert item["lifecycle"] is None and item["directories_pending_removal"] == 0
    assert SECRET not in response.text and "/w/a" not in response.text


async def test_an_admin_deprovision_returns_nothing_but_the_state(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="member")
    bot = await _assistant(db_session, owner)
    admin = await login_as(client, db_session, role="platform_admin")
    url = f"/api/admin/assistants/{bot.id}/deprovision"
    assert (await client.post(url, json={"confirm": False})).status_code == 422
    response = await client.post(url, json={"confirm": True})
    assert response.status_code == 200
    assert response.json()["data"] == {"bot_id": str(bot.id), "state": "deprovisioning"}
    assert SECRET not in response.text
    flow = await db_session.scalar(select(AssistantLifecycle))
    assert flow is not None and flow.trigger == "admin" and flow.actor_id == admin.id
    audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "assistant.deprovision")
    )
    assert audit is not None and audit.target_id == str(bot.id)
    # 重复点不重复开流程。
    assert (await client.post(url, json={"confirm": True})).status_code == 200
    assert len((await db_session.scalars(select(AssistantLifecycle))).all()) == 1


async def test_an_admin_cannot_deprovision_an_ordinary_bot(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    creator = await login_as(client, db_session, role="member")
    standard = Bot(
        bot_key=f"demo-bot-{uuid.uuid4().hex[:6]}",
        platform="feishu",
        name="demo-bot",
        created_by=creator.id,
        model="m",
        working_dir="/d",
        credentials_enc="",
    )
    db_session.add(standard)
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    response = await client.post(
        f"/api/admin/assistants/{standard.id}/deprovision", json={"confirm": True}
    )
    assert response.status_code == 404
    await db_session.refresh(standard)
    assert standard.enabled and standard.assistant_state is None


async def test_toggle_and_delete_cannot_skip_the_deprovisioning(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="member")
    bot = await _assistant(db_session, owner)
    assert (await client.post(f"/api/admin/bots/{bot.id}/toggle")).status_code == 409
    await login_as(client, db_session, role="platform_admin")
    response = await client.delete(f"/api/admin/bots/{bot.id}")
    assert response.status_code == 409 and "停用" in response.json()["message"]
    bot.assistant_state, bot.enabled = "deprovisioned", False
    await db_session.commit()
    assert (await client.delete(f"/api/admin/bots/{bot.id}")).status_code == 200


async def test_toggle_of_ordinary_bots_is_unchanged(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    creator = await login_as(client, db_session, role="member")
    standard = Bot(
        bot_key=f"demo-bot-{uuid.uuid4().hex[:6]}",
        platform="feishu",
        name="demo-bot",
        created_by=creator.id,
        model="m",
        working_dir="/d",
        credentials_enc="",
    )
    db_session.add(standard)
    await db_session.commit()
    await login_existing(client, db_session, creator)
    response = await client.post(f"/api/admin/bots/{standard.id}/toggle")
    assert response.status_code == 200 and response.json()["data"]["enabled"] is False
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_assistant_admin.py -q`
Expected: FAIL：`/api/admin/assistants` 404；助理记录的启停返回 200、删除返回 200。

- [ ] **Step 3: 实现**

`coreman/api/routers/assistants_admin.py`（新建）：

```python
"""平台管理员看个人助理：谁开通了、在哪个运行时、开通与停用走到哪一步；可以停用某人的助理。

只出元数据（人、实例、节点、状态、时间、错误代码），不碰任何内容列，也不解密任何东西。停用只回
「已开始停用」，不回记录本身。开通范围与可选 AI 在平台设置里改（`assistant_eligibility`、
`assistant_backends`）。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.core.assistants import deprovisioning
from coreman.core.audit import record_audit
from coreman.core.db.models import (
    AssistantLifecycle,
    AssistantRetiredDir,
    Bot,
    RelayServer,
    RuntimeNode,
    User,
)
from coreman.core.runtime_nodes.transport import online

_require_platform_admin = require_roles("platform_admin")
router = APIRouter(
    prefix="/api/admin/assistants",
    tags=["assistants"],
    dependencies=[Depends(verify_csrf), Depends(_require_platform_admin)],
)


class DeprovisionIn(BaseModel):
    """停用不可撤销：必须明确带上 `confirm: true`。"""

    confirm: bool


@router.get("")
async def list_assistants(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(
                Bot.id,
                Bot.assistant_state,
                Bot.enabled,
                Bot.created_at,
                User.id,
                User.login_name,
                User.display_name,
                RelayServer.id,
                RelayServer.name,
                RuntimeNode,
            )
            .join(User, User.id == Bot.owner_id)
            .outerjoin(RelayServer, RelayServer.id == Bot.relay_server_id)
            .outerjoin(RuntimeNode, RuntimeNode.id == RelayServer.runtime_node_id)
            .where(Bot.kind == "assistant")
            .order_by(Bot.created_at.desc())
            .limit(2000)
        )
    ).all()
    live = {
        row.bot_id: row
        for row in await session.scalars(
            select(AssistantLifecycle).where(
                AssistantLifecycle.finished_at.is_(None), AssistantLifecycle.bot_id.is_not(None)
            )
        )
    }
    counted = await session.execute(
        select(AssistantRetiredDir.bot_id, func.count()).group_by(AssistantRetiredDir.bot_id)
    )
    pending_dirs: dict[uuid.UUID, int] = {bot: count for bot, count in counted.all()}
    items = []
    for bot_id, state, enabled, created, owner_id, login, name, relay_id, relay, node in rows:
        flow = live.get(bot_id)
        items.append(
            {
                "bot_id": str(bot_id),
                "state": state,
                "enabled": enabled,
                "created_at": created.isoformat() if created else None,
                "owner": {"id": str(owner_id), "login_name": login, "display_name": name},
                "relay": {"id": str(relay_id), "name": relay} if relay_id else None,
                "node": (
                    {"id": str(node.id), "name": node.name, "online": online(node)}
                    if node is not None
                    else None
                ),
                "lifecycle": (
                    {
                        "action": flow.action,
                        "step": flow.step,
                        "trigger": flow.trigger,
                        "error_code": flow.error_code,
                    }
                    if flow is not None
                    else None
                ),
                "directories_pending_removal": int(pending_dirs.get(bot_id, 0)),
            }
        )
    return {"code": 0, "data": {"items": items}}


@router.post("/{bot_id}/deprovision")
async def deprovision(
    bot_id: uuid.UUID,
    body: DeprovisionIn,
    request: Request,
    user: User = Depends(_require_platform_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if body.confirm is not True:
        raise ApiError(422, 422, "停用不可撤销，请带上 confirm: true")
    bot = await session.get(Bot, bot_id)
    if bot is None or bot.kind != "assistant":
        raise not_found("个人助理不存在")
    flow = await deprovisioning.request(session, bot, trigger="admin", actor_id=user.id)
    await record_audit(
        session,
        action="assistant.deprovision",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="bot",
        target_id=str(bot_id),
        diff={},
        ip=client_ip(request),
    )
    await session.commit()
    return {
        "code": 0,
        "data": {"bot_id": str(bot_id), "state": "deprovisioning" if flow else "deprovisioned"},
    }
```

`coreman/api/main.py`：把

```python
    app.include_router(platform_apps.router)
    app.include_router(platform_apps.runs_router)
    app.include_router(users.router)
    app.include_router(teams.router)
```

换成

```python
    app.include_router(platform_apps.router)
    app.include_router(platform_apps.runs_router)
    from coreman.api.routers import assistants_admin

    app.include_router(assistants_admin.router)
    app.include_router(users.router)
    app.include_router(teams.router)
```

`coreman/core/assistants/records.py`：把

```python
def is_assistant(bot: Bot) -> bool:
```

换成

```python
ASSISTANT_TOGGLE_MESSAGE = (
    "个人助理不能直接启停：所有者在私聊里发「停用助理」，或由平台管理员在助理列表里停用"
)
ASSISTANT_DELETE_MESSAGE = (
    "个人助理要先停用（所有者在私聊里发「停用助理」，或由平台管理员在助理列表里停用），"
    "停用完成后才能删除"
)


def is_assistant(bot: Bot) -> bool:
```

`coreman/api/routers/bots.py`：把

```python
from coreman.api.versioning import require_if_match, set_etag
from coreman.core.assistants import runtime as assistant_runtime
from coreman.core.assistants.records import locked_changes, visible_to
from coreman.core.audit import diff_dict, record_audit
from coreman.core.bots.env_policy import blocked_env_keys
```

换成

```python
from coreman.api.versioning import require_if_match, set_etag
from coreman.core.assistants import runtime as assistant_runtime
from coreman.core.assistants.records import (
    ASSISTANT_DELETE_MESSAGE,
    is_assistant,
    locked_changes,
    visible_to,
)
from coreman.core.audit import diff_dict, record_audit
from coreman.core.bots.env_policy import blocked_env_keys
```

`coreman/api/routers/bots.py`：把

```python
        raise forbidden()
    await session.refresh(bot, with_for_update=True)
    if bot.workspace_state in {"migrating", "initializing", "busy"}:
        raise ApiError(409, 409, "工作目录正在处理中，请完成后再删除员工")
```

换成

```python
        raise forbidden()
    await session.refresh(bot, with_for_update=True)
    if is_assistant(bot) and bot.assistant_state != "deprovisioned":
        # 直接删会留下有效的飞书授权、没销毁的密钥和节点上的目录：先走停用。
        raise ApiError(409, 409, ASSISTANT_DELETE_MESSAGE)
    if bot.workspace_state in {"migrating", "initializing", "busy"}:
        raise ApiError(409, 409, "工作目录正在处理中，请完成后再删除员工")
```

`coreman/api/routers/bots_extra.py`：把

```python
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match, set_etag
from coreman.core.assistants.records import is_assistant, unsupported, visible_to
from coreman.core.audit import diff_dict, record_audit
from coreman.core.bots.switch_relay import SwitchError, is_moving, validate_switch
```

换成

```python
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match, set_etag
from coreman.core.assistants.records import (
    ASSISTANT_TOGGLE_MESSAGE,
    is_assistant,
    unsupported,
    visible_to,
)
from coreman.core.audit import diff_dict, record_audit
from coreman.core.bots.switch_relay import SwitchError, is_moving, validate_switch
```

`coreman/api/routers/bots_extra.py`：把

```python
    if not can_toggle_bot(user, bot, await member_ids_of(session, bot.id)):
        raise forbidden()
    await session.refresh(bot, with_for_update=True)
    if bot.workspace_state in {"migrating", "initializing", "busy"}:
```

换成

```python
    if not can_toggle_bot(user, bot, await member_ids_of(session, bot.id)):
        raise forbidden()
    if is_assistant(bot):
        # 助理记录的启停跟着开通与停用走：直接翻 enabled 会绕过撤授权、销毁密钥这些步骤。
        raise ApiError(409, 409, ASSISTANT_TOGGLE_MESSAGE)
    await session.refresh(bot, with_for_update=True)
    if bot.workspace_state in {"migrating", "initializing", "busy"}:
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/api/test_assistant_admin.py tests/api/test_assistant_bots.py tests/api/test_bots.py tests/api/test_bots_extra.py tests/api/test_assistant_closed_features.py -q`
Expected: 全部通过。

普通机器人不受影响：`test_toggle_of_ordinary_bots_is_unchanged`、`test_an_admin_cannot_deprovision_an_ordinary_bot`；启停与删除的新判断只看已加载记录的 `is_assistant(bot)`，`tests/api/test_bots.py`、`tests/api/test_bots_extra.py` 照常通过。

- [ ] **Step 5: 提交**

```bash
git add coreman/api/routers/assistants_admin.py coreman/api/main.py coreman/core/assistants/records.py coreman/api/routers/bots.py coreman/api/routers/bots_extra.py tests/api/test_assistant_admin.py
git commit -m "个人助理：管理员的助理列表与停用（只回状态），启停开关与删除不能绕过停用"
```

---


### Task 13: 端到端：开通 → 授权 → 一轮里调用写工具 → 审批 → 执行 → 结果回会话 → 停用 → 内容不可解

**Files:**
- Test: `tests/integration/test_assistant_end_to_end.py`（新建）

**Interfaces:**
- Consumes: 前面全部任务；1B 的网关入站；1D 的 `approvals.submit`、`service.decide`、`execute.run`、`AssistantApprovalResultHandler`；1E 的 worker 派出与 `sandbox_ready` 同样的池节点条件
- Produces: 无

- [ ] **Step 1: 写失败测试**

`tests/integration/test_assistant_end_to_end.py`（新建）：

```python
"""端到端：开通 → 授权 → 私聊里一轮调用写工具 → 审批单 → 批准 → 执行 → 结果回会话 → 停用 → 库里的
内容不可再解密。飞书、审批系统、节点与模型都用替身；网关入站、看护进程、worker 走真实代码。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import approvals, keys, sealed
from coreman.core.assistants.approvals import execute
from coreman.core.assistants.kinds import APPROVAL_EXECUTE_KIND, APPROVAL_RESULT_KIND
from coreman.core.bus import instances, tasks
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AssistantApproval,
    Bot,
    FeishuPersonalGrant,
    InboundEvent,
    OutboxItem,
    Task,
)
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal.policy import Scope
from coreman.runtime.worker.assistant_approvals import AssistantApprovalResultHandler
from coreman.runtime.worker.card_actions import CardActionHandler
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration import approval_helpers
from tests.integration.approval_helpers import CFG, TEXT, decided, queued, reply
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.lifecycle_helpers import (
    APP_ID,
    CHAT,
    OWNER_OPEN_ID,
    OWNER_PID,
    TENANT,
    FakeMessenger,
    assistant_app,
    click_event,
    flow_of,
    member,
    message_event,
    pool_relay,
    tick,
)
from tests.integration.test_assistant_gateway_provisioning import gateway
from tests.integration.test_assistant_provisioning import install, poll_now
from tests.integration.test_assistant_stop_command import click as stop_click
from tests.integration.worker_helpers import MASTER, build_ctx, user_input

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


async def claimed(session: AsyncSession, kind: str) -> Task:
    """worker 认领下一条任务（私聊消息有几秒的合并窗口，这里不等）。"""
    await session.execute(update(Task).where(Task.status == "queued").values(run_after=func.now()))
    await instances.register(
        session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    lane = "fast" if kind == "card_action" else "normal"
    task = await tasks.claim(session, lane=lane, instance_id="worker-test")
    await session.commit()
    assert task is not None and task.kind == kind, task
    return task


async def turn(engine: AsyncEngine, task: Task, handler=None) -> FakeRelay:  # type: ignore[no-untyped-def]
    relay = FakeRelay("normal")
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: relay.client())
    await (handler or ChatTaskHandler()).run(ctx)
    await ctx.chat_logs.drain(5)
    return relay


async def test_provision_use_approve_execute_and_deprovision(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    node, auth = install(monkeypatch)
    feishu, tower = approval_helpers.install(monkeypatch)
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    app = await assistant_app(db_session)
    owner = await member(db_session, open_id=OWNER_OPEN_ID)
    await pool_relay(db_session)
    await db_session.commit()
    inbound = gateway(db_engine, app.id)

    # ---- 开通 --------------------------------------------------------------------------------
    await inbound.accept(message_event())
    await tick(factory, messenger)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.offer_message_id is not None
    assert await inbound.accept(click_event(messenger.token(), flow.offer_message_id)) == (
        "provision_started"
    )
    for _ in range(4):
        await tick(factory, messenger, later=6)
    auth.approve()
    await poll_now(db_session)
    await tick(factory, messenger, later=12)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "done" and flow.bot_id is not None
    bot = await db_session.get(Bot, flow.bot_id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "active"

    # ---- 私聊里的一轮：派到池节点的沙箱里 -------------------------------------------------------
    await inbound.accept(message_event())
    task = await claimed(db_session, "chat")
    relay = await turn(db_engine, task)
    body = relay.requests[0]
    assert body["sandbox"] is True and body["transcript"]["restore"] is False
    # 这一轮里助理调用写工具：不执行，提交审批。
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    scope = Scope(task, bot, owner.id, TENANT, APP_ID, OWNER_OPEN_ID, OWNER_PID, CHAT, event)
    submitted = await approvals.submit(
        db_session,
        None,
        scope,
        "feishu_reply_message",
        reply(),
        cfg=CFG,  # type: ignore[arg-type]
    )
    await db_session.commit()
    assert submitted["status"] == "pending_approval" and feishu.writes == []
    row = await db_session.get(AssistantApproval, submitted["approval_id"])
    assert row is not None and row.status == "pending" and tower.submitted

    # ---- 批准 → 平台原样执行 → 结果作为一轮回到助理会话 --------------------------------------
    await decided(db_session, row)
    (execute_task,) = await queued(db_session, APPROVAL_EXECUTE_KIND)
    assert await execute.run(factory, Cipher(MASTER), row.id, execute_task, CFG) == "executed"
    assert [path for _, path, _ in feishu.writes] == ["/im/v1/messages/om_2/reply"]
    assert TEXT in feishu.writes[0][2]["content"]
    await db_session.execute(
        update(Task).where(Task.kind == APPROVAL_EXECUTE_KIND).values(status="succeeded")
    )
    await db_session.commit()
    result_task = await claimed(db_session, APPROVAL_RESULT_KIND)
    result_relay = await turn(db_engine, result_task, AssistantApprovalResultHandler())
    assert "已批准，平台已按提交时的参数原样执行" in user_input(result_relay.requests[0])
    assert result_relay.requests[0]["sandbox"] is True

    # ---- 停用 ----------------------------------------------------------------------------------
    stop = message_event()
    stop["event"]["message"]["content"] = '{"text": "停用助理"}'
    await inbound.accept(stop)
    stop_task = await claimed(db_session, "chat")
    assert (await turn(db_engine, stop_task)).requests == []
    card = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.bot_id == bot.id, OutboxItem.kind == "send")
    )
    assert card is not None
    click_task = await stop_click(db_session, bot, card, who=OWNER_PID, chat=CHAT)
    await CardActionHandler().run(build_ctx(db_engine, click_task))
    for _ in range(6):
        await tick(factory, messenger, later=30)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.action == "deprovision" and flow.outcome == "done"
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "deprovisioned" and not bot.enabled
    grant = await db_session.get(FeishuPersonalGrant, (bot.id, owner.id), populate_existing=True)
    assert grant is not None and grant.token_enc is None and auth.revoked
    assert ("workspace-remove", {"working_dir": bot.working_dir, "bot_id": str(bot.id)}) in (
        node.calls
    )

    # ---- 库里留下的内容：仍是密文，再也解不开 --------------------------------------------------
    keys.forget()
    sealed.forget()
    stored = str(await raw(db_session, "SELECT args_enc::text FROM assistant_approvals"))
    assert SECRET not in stored and "enc:v2:d1:" in stored
    payload = str(
        await raw(
            db_session, "SELECT payload::text FROM inbound_events WHERE id = :id", id=event.id
        )
    )
    assert "你好" not in payload
    with pytest.raises(keys.KeyDestroyed):
        await keys.data_cipher(db_session, Cipher(MASTER), owner.id)
    locked = await db_session.get(AssistantApproval, row.id, populate_existing=True)
    assert locked is not None and sealed.is_locked(locked) and locked.args_enc is None
    assert await db_session.scalar(text("SELECT count(*) FROM assistant_lifecycles")) == 2
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_assistant_end_to_end.py -q`
Expected: 前面任务都已落地时这一步直接通过（这是集成验收，不先失败）；任何一环没接上会在对应的断言处失败。

- [ ] **Step 3: 运行确认通过**

Run: `uv run pytest tests/integration/test_assistant_end_to_end.py -q`
Expected: 1 passed。

普通机器人不受影响：本任务只有用例。

- [ ] **Step 4: 提交**

```bash
git add tests/integration/test_assistant_end_to_end.py
git commit -m "个人助理：开通到停用的端到端用例"
```

---


### Task 14: 文档、设计同步与整体验证

**Files:**
- Modify: `docs/feishu.md`（「个人助理应用」）、`runtime_daemon/README.md`（「个人助理的沙箱」）、`docs/operations.md`（「个人助理版本的升级与回退」）
- Modify: `docs/superpowers/specs/2026-11-11-personal-assistant.md`（第三节「已知限制」、第四节的账号一项、第十节、第十一节）

**Interfaces:**
- Consumes: 前面全部任务
- Produces: 无代码

- [ ] **Step 1: 写文档**

`docs/feishu.md`：把

```markdown
还没开通的人发来的消息只记一条日志。
```

换成

```markdown
还没开通的人私聊（或打开与应用的私聊）时，平台私信他一张开通卡片（见本节「开通与停用」）；通讯录里认不出的人只记一条日志。
```

`docs/feishu.md`：把

```markdown
目标目录已存在就拒绝，所以助理回不到一个旧目录还在的节点。
```

换成

```markdown
目标目录已存在就拒绝；换走后旧目录由平台登记、经原来那台节点删掉（节点离线就等它回来再删），删掉之前助理回不到那台节点。
```

`docs/feishu.md`：把

```markdown
管理者与管理员不能启停别人的助理（是否加一个「只停用、不返回任何内容」的管理动作待定）；平台管理员仍可删除别人的助理。
```

换成

```markdown
助理记录不能用启停开关启停，也不能在停用完成之前删除；平台管理员可以在助理列表里停用某人的助理（只回状态，不回任何内容）。
- **开通与停用。** 成员私聊助理应用会收到一张开通卡片，本人点「开通」后，平台按设置 `assistant_eligibility`（全员，或名单里的人与部门，含下级部门）核对范围，要求账号启用且有登录名，在助理池里按设置 `assistant_backends` 的顺序挑一个能隔离运行的节点，建助理记录、数据密钥与一个全新的工作目录，再私信一张飞书授权卡片（全部权限、带离线访问）；授权完成后助理才开始处理消息，在那之前发来的消息只得到一句「还没开通完」。审批系统的账号平台事先查不到：开通卡片与欢迎语都提醒先登录一次审批系统，没有账号时第一次提交审批会如实告诉所有者。所有者在私聊里发「停用助理」并在确认卡片上点「确认停用」即停用：停下在跑的轮次，撤回待批的审批单，撤销飞书授权，销毁数据密钥，停用记录，再经节点删掉工作目录；之后可以重新开通，以前的内容读不出来。成员的账号停用 24 小时后自动停用（期间恢复就作罢）。平台管理员在 `GET /api/admin/assistants` 看谁开通了、在哪个运行时、走到哪一步，`POST /api/admin/assistants/{bot_id}/deprovision`（带 `confirm: true`）停用某人的助理。开通与停用由 scheduler 单独的一条循环每 5 秒推进一步，失败原地重试。
```

`runtime_daemon/README.md`：把

```markdown
以及开通和换运行时用到的 `workspace-info`、`workspace-probe`、`workspace-init`；
```

换成

```markdown
开通和换运行时用到的 `workspace-info`、`workspace-probe`、`workspace-init`，以及停用或换走之后删目录的 `workspace-remove`；
```

`runtime_daemon/README.md`：把

```markdown
拒绝的请求回 `403`，错误代码 `assistant_only`，`runtime.log` 里记一行 `Request refused on an assistant-only node`。
```

换成

```markdown
拒绝的请求回 `403`，错误代码 `assistant_only`，`runtime.log` 里记一行 `Request refused on an assistant-only node`。

**工作目录。** 助理在沙箱里能写自己的工作目录，守护进程在沙箱外碰它时都按「里面的东西可能被换掉」处理：读所有权标记（`.coreman-workspace.json`）不跟随链接、不在 FIFO 或设备上等待、只读普通文件且最多 64 KB；`workspace-init` 只在全新的目录上做，目录已带本助理的标记就原样返回（不读里面的任何文件），没有标记的非空目录一律拒绝。工作目录根下的 `.coreman-health`（节点自己的健康探测目录）与 `.coreman-trash`（回收站）不能当作助理的工作目录，落在它们里面的请求以 `reserved_directory` 拒绝。`workspace-remove` 只删带本助理标记的目录：先原子地改名进回收站，再在后台线程里不跟随任何链接地删掉；还有沙箱里的轮次用着这个目录时不删（`busy`），平台稍后再来；删除线程没做完就退出的，由助理侧的清扫（启动时与之后每 10 分钟）接着删。

**改回普通实例。** 把助理专用实例改回普通实例（去掉 `assistant_only`）后，守护进程启动时把数据目录下的 `assistant-turns/`（可能有没传上去的明文转录）删一次。项目主目录下的 `.coreman-trash/` 普通实例不碰，改回之前先确认它是空的。
```

`docs/operations.md`：把

```markdown
- 只回退节点隔离那一个迁移的数据库降级，
```

换成

````markdown
- 开通与停用的版本（迁移 `0092`）：先迁移、再启动新进程。新平台对旧的助理专用实例发 `workspace-remove` 会被拒绝，登记待删的目录留着、每隔一阵重试，节点升级后自动删掉。回滚平台之前，先停用所有还没开通完的助理记录（旧平台不认识开通状态，会让它们跑轮次），并等进行中的开通与停用做完：

  ```sql
  UPDATE bots SET enabled = false
   WHERE kind = 'assistant' AND assistant_state <> 'active';
  SELECT count(*) FROM assistant_lifecycles WHERE finished_at IS NULL;
  SELECT count(*) FROM assistant_retired_dirs;
  ```

  降级这一个迁移会删掉开通状态、进行中的流程与待删目录的登记：之后没删的旧目录只能到节点上手工删。
- 只回退节点隔离那一个迁移的数据库降级，
````

`docs/superpowers/specs/2026-11-11-personal-assistant.md`：把

```markdown
- 助理回不到用过的节点：换走时旧目录不清理，换回来时目标目录已存在会被拒绝。
- 没有「只停用、不返回任何内容」的管理动作：管理者与管理员不能启停别人的助理，平台管理员仍可删除别人的助理。
```

换成

```markdown
- 换走后的旧目录由平台登记、经原节点删除；原节点一直离线时目录一直留着，删掉之前助理回不到那台节点。
- 管理员没有「暂停」：只有停用（不可撤销，只回状态）；助理记录不能用启停开关启停，停用完成前不能删除。
```

`docs/superpowers/specs/2026-11-11-personal-assistant.md`：把

```markdown
**开通**：私聊助理应用 → 卡片上点「开通」→ 平台校验可开通范围与 Tower 账号，创建助理记录与数据密钥，分配节点，发起飞书授权 → 授权完成后进入冷启动。
```

换成

```markdown
**开通**：私聊助理应用 → 卡片上点「开通」→ 平台校验可开通范围（账号启用、有登录名、在范围内），创建助理记录与数据密钥，分配节点，在节点上建一个全新的工作目录，发起飞书授权 → 授权完成后进入冷启动（第 2 期；第 1 期授权完成即可用）。Tower 没有按用户名查账号的接口，开通时查不到账号：开通卡片与欢迎语提醒先登录一次审批系统，没有账号时第一次提交审批如实报 `tower_account_missing`。开通由看护进程一步一步推进，任何一步放弃都走一遍停用把已建的东西收掉；授权完成前记录跑不了轮次。
```

`docs/superpowers/specs/2026-11-11-personal-assistant.md`：把

```markdown
**停用**：停止轮询 → 撤回待批的审批单 → 撤销飞书授权 → 清除节点上的工作目录 → 销毁数据密钥 → 停用记录。成员离职时自动执行。
```

换成

```markdown
**停用**：停止轮询（第 2 期）→ 停下在跑的轮次 → 撤回待批的审批单 → 撤销飞书授权 → 销毁数据密钥并停用记录 → 清除节点上的工作目录（登记后由看护进程经节点删除，节点离线就等它回来）。入口：所有者在私聊里确认、平台管理员在助理列表里停用、成员账号停用满 24 小时自动执行（期间恢复就作罢）。一旦开始就不可撤销，失败原地重试直到做完。
```

`docs/superpowers/specs/2026-11-11-personal-assistant.md`：把

```markdown
- 新表 `assistant_access_log`：记录运行时路径之外的每次解密（页面读取、导出、管理命令），含操作人、对象、时间。所有者可在页面上查看自己的记录。
```

换成

```markdown
- 新表 `assistant_access_log`：记录运行时路径之外的每次解密（页面读取、导出、管理命令），含操作人、对象、时间。所有者可在页面上查看自己的记录。推迟到第 3 期与「我的助理」页、导入一起做：第 1 期运行时之外的解密只有所有者在管理台读自己的对话记录（接口只对所有者本人还原）。
```

`docs/superpowers/specs/2026-11-11-personal-assistant.md`：把

```markdown
Tower 不可用时提交失败，写操作不执行。所有者在 Tower 没有对应账号时不能开通助理。
```

换成

```markdown
Tower 不可用时提交失败，写操作不执行。所有者在 Tower 没有对应账号时提交失败（`tower_account_missing`）；开通时查不到账号，只提醒先登录一次（见第十节）。
```

`docs/superpowers/specs/2026-11-11-personal-assistant.md`：把

```markdown
| 账号 | 首次登录或管理员预登记后才存在 | 开通时校验，没有则引导先登录一次 |
```

换成

```markdown
| 账号 | 首次登录或管理员预登记后才存在 | 没有按用户名查账号的接口，开通时查不到：开通卡片与欢迎语提醒先登录一次，没有账号时第一次提交报 `tower_account_missing` |
```

- [ ] **Step 2: 运行确认通过**

Run: `uv run ruff check . && uv run mypy coreman && uv run alembic check && uv run pytest tests/unit tests/integration tests/api -q`
Expected: 全部通过（没有 bubblewrap 的机器上 `test_real_sandbox_self_test` 等 3 条跳过）。只对本计划新建与改动的 `.py` 文件跑 `uv run ruff format --check`（仓库里有与本计划无关的文件本来就不符合格式）。

普通机器人不受影响：全量用例。

- [ ] **Step 3: 提交**

```bash
git add docs/feishu.md runtime_daemon/README.md docs/operations.md docs/superpowers/specs/2026-11-11-personal-assistant.md
git commit -m "个人助理：开通与停用的部署与使用说明、升级与回退，设计文档第三、四、十、十一节同步"
```

---

## 与设计文档的对照

| 设计文档 | 本计划 |
|---|---|
| 十：私聊助理应用 → 卡片上点「开通」 | Task 9（`request_offer`、`step_offered`、`offer_card`、`accept_offer`）、Task 10（网关接上） |
| 十：校验可开通范围与 Tower 账号 | Task 6（`eligibility.refusal`：范围、账号启用、登录名）；Tower 账号查不到，见「设计选择」第 10 条 |
| 十：创建助理记录与数据密钥，分配节点，发起飞书授权 | Task 9（`step_create`、`step_init_workspace`、`step_start_grant`、`step_await_grant`）；节点按 1E 的 `pick_pool_relay` 选，另核对 `node_isolated` |
| 十：授权完成后（第 1 期）即可用 | Task 5（`turn_blocker` 只放 `active`）、Task 9（授权完成才 `active`） |
| 十：停用的顺序；成员离职时自动执行 | Task 8（`drain → withdraw → revoke → destroy`、`grace`、`_departures`）、Task 7（删目录）、Task 11（所有者）、Task 12（管理员）；「停止轮询」属于第 2 期 |
| 二：停用或成员离职时销毁数据密钥 | Task 8（`step_destroy`）；端到端见 Task 13 |
| 三：池里选助理最少的在线节点 | Task 9（`_pick`：1E 的 `pick_pool_relay` + `node_isolated`，按 `assistant_backends` 的顺序） |
| 三「已知限制」：助理回不到用过的节点 | Task 7（换走后登记旧目录、经节点删除） |
| 三「已知限制」：没有只停用、不返回内容的管理动作 | Task 12 |
| 十一：设置项（可开通范围、允许的模型后端、助理池节点并发上限） | Task 6；并发上限 1E 已有（`assistant_node_concurrency`）；每日轮次预算推迟（1E） |
| 十一：管理员能看到谁开通了、所在节点 | Task 12（列表只出元数据）；轮次与额度用量、轮询健康属于第 2 期 |
| 十一：`assistant_access_log` | 推迟到第 3 期（「设计选择」第 11 条） |
| 一：卡片回调按操作人路由，只认记录的所有者 | Task 10（开通卡片：按操作人找本人的流程）、Task 11（停用卡片：出站核对 + 所有者 + HMAC） |
| 测试：开通 → 授权 → 私聊发起一次回复 → 审批单 → 批准 → 执行 → 结果回会话 | Task 13 |
| 测试：停用后库里密文不可解 | Task 8（`test_deprovisioning_leaves_nothing_usable`）、Task 13 |

## 所有者已确认的取舍

1. **一人一个助理**（设计「已定决策」）：一人同时只有一条启用的助理记录（1A 的部分唯一索引），本计划另加「一人同时只有一条没结束的开通或停用流程」。
2. **只接私聊**（设计「已定决策」）：开通卡片、授权卡片、停用确认、各种通知都只在本人与助理应用的私聊里。
3. **助理与普通员工共用机器，每台机器一个助理专用实例**（1E）：开通只在助理池里挑能隔离运行的节点（`node_isolated`）。
4. **跑助理的节点上没有普通员工**（1E）。
5. **非聊天的节点命令对助理关闭**（1E）：本计划只加一个 `workspace-remove`，写法见「设计选择」第 6 条。
6. **助理换运行时只落到一个全新的空目录**（1E）：本计划换走后删旧目录、开通时每次用带记录编号的新目录，都建立在这一条上。

## 待所有者确认的取舍

1. **默认全员可开通**（设置 `assistant_eligibility` 默认 `all`），照设计「人人可开通」。另一种做法是默认「名单为空」，代价是部署之后要管理员先配置，谁都开通不了。
2. **停用时工作目录直接删，不先导出。** 工作目录按设计只放规则与经验总结；要在删除前给所有者一份导出，需要把导出也放进沙箱（第 3 期「导入」时一起设计）。
3. **所有者与管理员的停用都立即开始、不可撤销，没有宽限期。** 所有者要在确认卡片上再点一次；管理员要带 `confirm: true`。另一种做法是停用后宽限一段时间（期间可撤销）再销毁密钥，代价是多一个可撤销的中间态与对应的入口，密钥也多活这么久。
4. **「成员离开」= 账号被停用（`users.status = 'disabled'`），宽限 24 小时后停用。** 期间助理暂停，账号恢复就作罢。可以改成立即，或更长（例如 7 天）；还被启用、只是被移出助理应用可用范围的人不算离开。
5. **换运行时换走的旧目录要删**（「设计选择」第 7 条）。另一种做法是保留 1E 的行为（不删），代价是助理回不到用过的节点，节点上留着助理写过的东西。
6. **`assistant_access_log` 推迟到第 3 期**（「设计选择」第 11 条）。
7. **没有「暂停」：助理记录的启停开关对所有人关闭（包括所有者）**，管理员只有停用。要临时不让助理工作，只能停用后重新开通（以前的内容读不出来）。
8. **授权链接发满 3 次（每次约 10 分钟有效）还没完成就放弃这次开通**，开通的暂时失败（节点离线、没有可用节点）连续 10 次（约一小时）也放弃；放弃时把已建的东西走一遍停用收掉，私信原因。可以改成一直等，代价是半开通的记录与密钥长期留着。
9. **已批准还没执行的写操作在停用时照常执行完**（`drain` 不叫停 `assistant_approval_execute`），在密钥销毁之后才轮到的会因 `content_locked` 失败，不再通知（结果回合已被挡住）。
10. **本计划上线前已经存在的助理记录一律按已开通（`active`）处理**（迁移里一条 `UPDATE`）。这一叠还没有上线，正常情况下没有这样的记录。
11. **给本人的通知（不在范围、开通失败、欢迎语、停用完成）只发一次**，飞书暂时不可用就丢了（只记一笔）；开通卡片与授权卡片发不出去时那一步会重试。
12. **停用命令是精确匹配的「停用助理」或「/停用助理」。** 没有做成斜杠命令表里的一项（网关的归一化不认识它）。

## 已知限制

- **审批系统的账号开通时查不到。** 只能提醒「先登录一次」，没有账号时第一次提交审批才报出来（1D 的 `tower_account_missing`）。
- **开通卡片先发、后提交。** 提交失败时卡片已在私聊里，但令牌没记下，点了只会得到「卡片已失效」；再发一条消息会收到新卡片。卡片发出到提交之间（毫秒级）点也一样。
- **节点一直不回来，旧目录就一直留着。** 待删的登记退避到每 6 小时一次、永不放弃；删除实例（`relay_servers` 行）会连带删掉登记，目录就没人删了。管理员列表里能看到每位助理还有几个目录没删掉。
- **飞书那边的撤销尽力而为。** 平台库里的令牌在 `revoke` 这一步当场清空；飞书的撤销接口失败时，那枚令牌在飞书侧要到它自己过期才失效（平台已没有副本）。
- **停用等在跑的轮次最多 15 分钟。** 之后不再等：那一轮写不进任何东西（记录已不是 `active`，密钥随后销毁）；它的工作目录在节点上还挂着时，`workspace-remove` 答复 `busy`，平台等它结束再删。
- **开通与停用一条一条串行推进**，一步最长 150 秒；节点离线时同一轮里排在后面的流程会晚一些。
- **成员离开只看账号状态。** 账号还启用、但已不在助理应用可用范围里的人，平台发不出消息，也不会自动停用。
- **改回普通实例时回收站不清。** 普通实例只删 `assistant-turns/`；回收站里还有东西（删除线程没做完、清扫还没轮到）就改回普通实例的，要手工删 `.coreman-trash/`（README 里写了）。
- **开通期间每条消息各回一次「还没开通完」，不节流**（同 1E 的暂停提示）。
- **网关登记与看护进程抢同一行时，登记让路。** 网关在应答窗口里有 1.5 秒的语句时限；看护进程正在处理这个人的流程（例如正发卡片、正查授权）时，网关等不到行锁就放弃这一次登记或点击（保存点回滚，应答照常），所有者再发一条或再点一次即可。管理员的停用请求同样要等看护进程那一步做完（最长 150 秒）。
- 1E 的已知限制里「助理回不到用过的节点」「没有只停用的管理动作」两条由本计划改写（Task 14 同步设计文档）；其余照旧。

## 仍需真机核实

1. **开通卡片的回调字段。** 用 `im/v1/messages`（`receive_id_type=user_id`）发出的交互卡片，被点击时回调里 `event.context.open_message_id` / `open_chat_id` 是否就是发送接口返回的 `message_id` / `chat_id`，事件头是否带 `tenant_key`（缺了退回 `event.operator.tenant_key`）。不一致时所有点击都会得到「卡片已失效」（失败而不是放行），改法是放宽比对的那一项。
2. **助理应用的「全部」档位授权**：`begin_in_flow(..., "all")` 要求应用开通了发消息的用户权限；没开通时开通会以「应用的飞书权限不足」放弃。
3. **网络存储上的 `rename` 与删除**：工作目录在网络存储上时，回收站在同一个挂载点下（工作目录根），`rename` 是同一文件系统内的；`O_NOFOLLOW｜O_NONBLOCK` 在网络文件系统上的行为与本地一致；大目录的删除时间。
4. **停用的整条链路**：所有者在真实私聊里发「停用助理」、点确认卡片；节点上目录先出现在 `.coreman-trash/` 下、随后消失；`runtime.log` 里不出现路径。
5. **Python 3.10 的节点**：三个改过的 `runtime_daemon/*.py` 只做了语法级解析（开发机是 3.12），没在 3.10 解释器上跑过用例。

## 对设计文档的意见

1. **「开通时校验 Tower 账号」做不到。** Tower 没有给发送端按用户名查账号的接口（1D 已核实）。本计划只能提醒，并靠第一次提交时的 `tower_account_missing` 如实报出；设计第四节「账号」一行与第十节同步改写（Task 14）。
2. **停用顺序里「清除节点上的工作目录」排在「销毁数据密钥」之前，照做会让密钥迟迟销毁不了。** 节点可能长期离线；工作目录不是用数据密钥加密的，两者互不依赖。本计划先销毁密钥、停用记录，同一个事务里登记删目录，之后一直重试；设计第十节同步改写。
3. **「成员离职时自动执行」没有缓冲。** 通讯录同步抖一下、管理员误停一个账号，就会不可逆地销毁密钥。本计划加了 24 小时宽限（待确认第 4 条）。
4. **1A 的 `new_assistant_bot` 用固定的 `bot_key`，停用后不能重新开通。** 本计划在停用的最后一步给旧记录换一个不再用到的 `bot_key`，没有改 1A 的取名规则。
5. **`assistant_access_log` 在第 1 期没有要记的东西**（「设计选择」第 11 条）。

## 自查记录

- **设计覆盖。** 逐条见「与设计文档的对照」。没有对应到任务的：冷启动、停止轮询、轮次与额度用量、轮询健康（第 2 期），`assistant_access_log`（推迟，待确认第 6 条）。1E 留给 1F 的四件必修项分别是 Task 1、3、2、4；「删除助理工作目录的节点命令」是 Task 2，用在停用（Task 8）与换走之后（Task 7）；「只停用、不返回内容的管理动作」是 Task 12。
- **占位扫描。** 全文没有 TBD、「类似 Task N」「处理边界情况」之类的写法；每个代码步骤都给了完整文件或逐处的替换内容。
- **名字与签名前后一致。** 节点侧：`workspace.read_small(path, limit=MARKER_LIMIT)`、`marker_data(root)`、`reserved(root, raw)`、`trash_dir(root)`、`HEALTH_DIR`、`TRASH_DIR`、`Workspace.assistant_only`、`Workspace.remove()`、命令 `workspace-remove`（答复 `removed` / `existed` / `busy`）、`daemon.assistant_only_refusal(method, path, data, root=None)`（代码 `reserved_directory`）、`RuntimeAgent.assistant_only`、`Daemon.clear_assistant_leftovers()`、`AssistantTurns.workdirs`、`in_use(path)`、`purge_trash()`。平台侧：`Bot.assistant_state`、`ASSISTANT_STATES`、`AssistantLifecycle`（`action`、`step`、`trigger`、`outcome`、`offer_hash`、`offer_message_id`、`offer_chat_id`、`open_id`、`tenant_key`、`deadline`、`attempts`、`issued`、`next_attempt_at`、`error_code`、`finished_at`）、`AssistantRetiredDir`、`runtime.turn_blocker` 的 `provisioning` / `deprovisioning`、`runtime.LIFECYCLE_MESSAGES`、`eligibility.refusal(session, user)` / `backends(session)`、`lifecycle.StepRetry` / `StepFailed` / `live` / `advance` / `finish` / `backoff` / `token_hash` / `Messenger` / `PROVISION_MAX_ATTEMPTS`、`retired_dirs.retire(session, *, bot_id, relay_id, working_dir)` / `tick(factory, cipher, now)`、`deprovisioning.request(session, bot, *, trigger, actor_id)` / `STEPS` / `GRACE` / `DONE_TEXT` / `retired_key(bot)`、`provisioning.request_offer(session, *, app_id, owner_id, platform_user_id)` / `accept_offer(session, *, app_id, owner_id, raw)` / `STEPS` / `OFFER_TTL` / `GRANT_ROUNDS` / `FAILURES` / `failure_text(code)` / `welcome_text()`、`lifecycle_runner.tick(factory, cipher, now, *, messenger=None)`、`ASSISTANT_LIFECYCLE_SECONDS`、`Outcome` 的 `provision_started` / `provision_expired`、`assistant_stop.intercept(ctx, intake)` / `handle(session, ctx, bot, inbound, action)` / `EVENT_KEY` / `TTL_SECONDS`、`records.ASSISTANT_TOGGLE_MESSAGE` / `ASSISTANT_DELETE_MESSAGE`、接口 `GET /api/admin/assistants`、`POST /api/admin/assistants/{bot_id}/deprovision`。
- **台账。** 新代码不取任何内容表（`sealed.COLUMNS` 登记的模型）的整行、不引用内容列：`deprovisioning.step_drain` 只取 `Task.id`、`Task.kind`、`Task.status`；`tests/unit/test_assistant_sealed_ledger.py` 不用改，照常通过。
- **对照上一期评审找到的几类缺陷逐项查过：**
  - *助理写得到的文件与目录。* 守护进程在沙箱外碰助理目录的地方：所有权标记（`read_small`：不跟随链接、不在 FIFO 与设备上等待、`fstat` 只认普通文件、64 KB 上限，同一个描述符上核对）；`workspace-init`（助理专用实例对已存在的目录不读任何文件）；`workspace-remove`（目录每一级不是链接；`rename` 不跟随；回收站必须是本用户的真实目录；`remove_tree` 走描述符、不跟随链接；有轮次挂着时不删，消除「先 `lstat` 再 `chmod`」与沙箱写入的竞态）；回收站清扫（`scandir` 不跟随、链接只 `unlink`）；普通实例清 `assistant-turns/`（是链接只删链接）。数量与大小：删除是后台线程、不占操作锁；回收站清扫在线程里、每项单独兜底。
  - *进入安全判断的值从哪来。* 开通：人来自飞书事件的操作人或发送人（网关经通讯录认出），令牌只用来认卡片实例，消息与会话来自事件上下文，`open_id` / `tenant_key` 来自事件并在授权完成时由飞书的用户信息接口核对；停用确认：人来自入站事件的发送人，卡片由出站核对认定，HMAC 用主密钥派生；删目录：路径来自库里的 `working_dir`（锁定字段、平台分配），节点再核对标记；范围：只来自管理员设置。没有任何一项来自卡片内容、显示名或工作目录里的文件。
  - *「检查」会不会在性质不成立时通过。* 读不懂的设置一律不放行；记录状态为空也不放行；`node_isolated` 在挑节点时另查；FIFO / 链接 / 过大的标记一律当作「不是本员工的」；`in_use` 在轮次登记之后、派出之前就成立（`run` 里登记与 `plan_turn` 之间没有 `await`）。
  - *助理的工作放进了普通员工共用的事务、循环、锁或进程里吗。* 网关：只在助理应用的连接里，带保存点与 `try/except`，失败只丢这一次登记；看护进程：单独的循环，与 `_tick_loop` 分开（Task 8 有用例让它一直失败）；worker：`is_assistant` 分支里，停用命令只多一次出站写入；`switch_relay`：只在助理分支里多一条 `INSERT`；守护进程：普通实例启动只多一个线程。没有新加的锁落在普通员工的路径上。
  - *异常路径带出内容或路径吗。* 看护进程只记步骤函数名与代码（`StepRetry` 的代码是 `feishu_<数字>`、`AgentError` 的代码或异常类型名）；`retired_dirs` 只记登记编号；守护进程只记异常类型名，`trash_dir` 的 `OSError` 由 `Workspace.dispatch` 转成固定文案；给本人的文案是固定的几句。
  - *绕过设计入口、改到同一状态的地方。* 枚举过：`bots.enabled` 只在启停接口里被翻（助理一律 409）；删除接口（助理只许删已停用的）；`PATCH` 的字段集合里没有 `enabled` / `assistant_state`（`assistant_state` 还进了 `LOCKED_FIELDS`）；`deprovisioning.request` 的调用点只有确认卡片、管理员接口、成员离开扫描、开通放弃四处；`assistant_state` 的写入只在开通（`create`、`await_grant`）、停用（`request`、`grace` 作罢、`destroy`）与迁移里；`keys.ensure_key` 带 `reissue=True` 的调用只有开通；`keys.destroy_key` 的调用只有停用（`destroy` 与记录没了时）。
  - *新旧版本混跑与回滚。* 新平台 + 旧节点：`workspace-remove` 被旧的助理专用实例拒绝（`assistant_only`），登记留着、退避重试，节点升级后自动删；FIFO 与保留目录的防护要等节点升级。旧平台 + 新数据：旧代码不认识 `assistant_state`，会让开通中的记录跑轮次——回滚前按 `docs/operations.md` 的 SQL 停用非 `active` 的助理并等流程做完；降级 `0092` 会丢掉进行中的流程与待删目录的登记。
- **实际跑过的（在仓库的一份临时副本上按最终状态落了一遍，另按任务顺序逐个落中间状态各跑一次检查）：** 见下面「验证」。

### 验证

- 计划里的全部代码与用例在仓库的一份临时副本上按最终状态落过：把本计划 14 个任务的全部操作（新建、追加、替换）依次作用在开工时的文件上，得到的 53 个文件与跑过下面检查的副本逐字节一致；替换用的旧文本在当时的文件里都恰好出现一次。
- 按任务顺序逐个落中间状态，每落完一个任务各跑一遍 `ruff check .`、`mypy coreman`、该任务「运行确认通过」那一步的用例：14 个任务都通过（Task 5 另跑了 `alembic check`；Task 14 只有文档）。各任务「运行确认失败」那一步写的预期失败信息是按代码推断的，没有逐个跑。
- 最终状态：`uv run ruff check .` 通过；本计划新建与改动的 `.py` 文件 `ruff format --check` 通过；`uv run mypy coreman` 通过（483 个源文件）；在一个新建的空库上 `alembic upgrade head`、`alembic check`（No new upgrade operations detected）、`alembic downgrade 0091`、再 `upgrade head` 通过，用完已删除该库；`runtime_daemon/workspace.py`、`daemon.py`、`assistant_turns.py` 按 `ast.parse(..., feature_version=(3, 10))` 解析通过。
- 本计划新增的用例文件 15 个（含公共件 `lifecycle_helpers.py`）、改动的用例文件 4 个（`test_runtime_daemon_assistant.py`、`test_settings_schema.py`、`tests/conftest.py`、`assistant_helpers.py`），连同 `tests/unit/test_assistant_sealed_ledger.py` 以及被改动文件原有的用例（守护进程、工作目录、助理轮次、转录、换运行时、网关入站、卡片回调、机器人接口、设置、看护进程）都跑过并通过。
- 全量：`uv run pytest tests/unit tests/integration tests/api -q`：4802 通过、3 跳过、0 失败（开工时这个分支的基线是 4686 通过、3 跳过；跳过的 3 条是本机没有 bubblewrap 等原因，与本计划无关）。
- 没有验证的：真机上的飞书卡片回调字段、助理应用「全部」档位的授权、网络存储上的改名与删除、3.10 解释器上运行节点用例（见「仍需真机核实」）；开发机是 macOS，没有 bubblewrap，真起沙箱的用例照旧跳过。

## 落地前提

- **迁移要重新编号。** 本叠迁移从 `0086` 起、到本计划的 `0092` 止；主干此后已经新增了编号同为 `0086` 的迁移，与本叠的第一个迁移撞号。把这一叠合并进主干时，要把它的全部迁移（目前是 `0086`–`0092`）接在主干当前的最新编号之后连续重新编号，并把每个迁移的 `down_revision` 链更新到位，再在空库上跑一遍 `alembic upgrade head` 与 `alembic check`。不要在实现分支里提前改号：这是一次跨整叠分支的操作，编号取决于合并时主干的状态。
- **先升平台、再升助理节点。** 节点没升级之前，停用与换运行时登记的待删目录删不掉（一直重试），1E 留下的标记读取与保留目录问题也还在；助理专用实例全部升级之后再开放开通。

## 实现时的改动

上面的任务文字是实施前写的；评审之后下面这些行为与它不同，以代码和 `docs/feishu.md`、`runtime_daemon/README.md`、`docs/operations.md` 为准。

- 助理专用实例判断目录归属，看的是守护进程自己保管的登记 `<工作目录根>/.coreman-owners/`（每个目录一条：员工编号与 inode，初始化全新目录时写入）；目录里的标记只在没有登记的旧目录上作后备。`.coreman-owners` 成了第三个保留目录，与 `.coreman-health`、`.coreman-trash` 一样不能当工作目录。
- 回收站与归属登记目录由守护进程保持为 0700。
- 改回普通实例时的清理在向平台注册之前做（注册不上也会清）：删 `assistant-turns/`，并只删 `.coreman-trash/` 里名字是 32 位小写十六进制的条目；不动目录本身，带所有权标记时跳过，是链接时不碰。「回收站不清、要手工删」的已知限制因此不再成立，剩下的是 `.coreman-owners/` 的登记和名字不符的条目。
- `workspace-remove` 在有在跑的轮次用着目录的上级目录时也答复 `busy`。
- 还有助理工作目录等着删的运行时实例不能删除（409），取代了「删除实例会连带删掉登记、目录就没人删了」的已知限制。
- 账号停用的 24 小时宽限期内，所有者或管理员明确要求停用的立即生效，之后账号恢复也不撤回。
- 停用时已批准、排队中（不只是在跑）的写操作也要等执行完，最多等 15 分钟。
- 「停用助理」只在私聊里、平台一认出发送人就截下：运行时停用、有公告或助理正等回答时也有效；不是所有者发的什么都不回；不会并进排队的那一批消息交给模型。
- 没有配置任何 AI 后端（`assistant_backends` 为空）时开通当场放弃并私信说明；暂时没有能隔离运行的池节点仍然重试。
- 是否「已经有助理」按状态认：只要名下有一条没完全停用的助理记录就算，不看启停开关。
- 可开通范围的设置严格解析：存着的值通不过校验，谁都不放行。
- 管理员停用接口的 `confirm` 必须是 JSON 的 `true`；只有真的开始了一次停用才写审计，重复点、已停用完的不写。
- 迁移 `0092` 的降级会先把状态不是 `active` 的助理记录停用，再删掉状态列。
- 归属登记只记 inode，不记设备号（网络存储的设备号换机器或重新挂载会变）；删除之后登记换成墓碑，30 天后由助理侧的清扫删掉。
- `workspace-remove` 只在确知时给定论（已删除，或登记记着别的助理）；共享存储没挂上或报错、登记缺失或损坏、inode 对不上、没有登记一律答 `unverified`，平台留着待删记录、最长每 6 小时重试并记 `dir_unverified`；没有登记的目录节点从不删。
- 轮次开始时工作目录不在或正在删除，以 `workdir_missing` / `workdir_busy` 拒绝。
- 助理不是 `active`（要求停用，或账号停用宽限期满）之后送达的审批决定不执行，单按已撤回收口，不再提醒；停用开始前已批准的照常执行。宽限期里账号恢复，期间已决定或已过期的单不再回投结果，所有者需重新提。
- 不是 `active` 的助理不能换运行时（含预览）、不能初始化工作目录；换的过程中停用开始则作罢并登记新目录待删。
- 飞书个人授权只对开通中或 `active` 的助理可发起、完成；停用最后一步再清一次授权。
- 停用后重新开通等约 75 秒（密钥缓存过期，`key_cooling`）；应用密钥暂时解不开重试（`app_secret_unreadable`）；助理名字被别人的记录占着则失败（`bot_key_taken`）并提示联系管理员。
- 管理员对已停用完的记录再停用不做任何事，也不动本人当前的助理。

