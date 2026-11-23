# Claude 后台任务：后台阶段与长驻进程

## 背景

开发类 AI 员工常把大段工作交给 Claude 的后台子任务（`Agent` 的 `run_in_background`），自己先回一句「已开工，做完告诉你」。现在每条消息起一个 `claude -p` 进程，标准输入在写完这条消息后就关闭。实测这种情况下：

- 主回合结束后，CLI 最多再等 `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`（默认 10 分钟）就停掉所有后台任务。
- 这一轮的 `result` 事件被扣住，直到后台任务结束或被停才发出，所以一轮的耗时等于主回合加上整段等待，期间卡片一直显示「执行中」。
- 后台任务被停时平台不知道，任务照样记为成功，用户回来只看到一句「做完再告诉你」。

用户的直接感受是「执行不彻底，搞不清它在做什么」：每追问一次「现在什么进展」，模型才发现上次被打断，再接着做一段。

关掉后台任务（`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`）能止血，但会失去并行开发能力。本设计保留后台任务，把「等多久、做到哪、要不要停」交给平台控制，并让用户随时看得到。

目标：

- **做得完**：后台任务不再被 CLI 的 10 分钟上限停掉，只受平台设定的上限约束。
- **看得见**：主回合一结束就给出回复；卡片上列出还在跑的后台任务；每个完成都主动告诉提问人。
- **管得住**：能停单个后台任务，到上限时明确说出哪些没做完。
- **不串线**：同一会话永远只有一个 CLI 进程在写会话记录。

不在本次范围：Codex、OpenCode 运行时（没有同样的后台任务机制）；个人助理的沙箱回合。

## 实测结论（CLI 2.1.318）

设计以这组实测为依据，实验都用 `claude -p --input-format stream-json --output-format stream-json`。

| 场景 | 结果 |
|---|---|
| 标准输入写完即关（现状） | 主回合后按等待上限停掉后台任务，stderr 打印 `Background tasks still running … stopping them`；`result` 被扣到停掉时才发出 |
| 标准输入保持打开 | 主回合 `result` 立即发出；等待上限不生效，后台任务跑满；完成时 CLI 自动续一轮，单独发一个 `result`；关闭标准输入且没有后台任务时 2 秒内退出 |
| 保持打开时中途写入新消息 | 不打断正在跑的工具；当前工具结束后并入本轮（会话记录里是 `queue-operation … absorbed_mid_turn`），和本轮原有内容合在一个回复、一个 `result` 里；没有回合在跑时立即开新一轮 |
| `control_request` `interrupt` | 立即停当前回合（`result` 为 `error_during_execution`），进程不退出；**同时停掉所有后台任务，包括之前几轮开的** |
| `control_request` `stop_task`（带 `task_id`） | 只停这一个后台任务，其余照跑；被停的任务也触发一轮回复 |
| 老进程后台任务还在跑时，另起进程 `--resume` 同一会话 | 新进程把该任务判成 `stopped` 并告诉用户「已停止」（实际还在跑）；两个进程的后续内容挂在同一个父节点上，会话记录分叉，之后的续聊只沿最新分支读，另一支丢失 |

流里可用的后台任务事件：`system/task_started`（`task_id`、`description`、`task_type`、`is_backgrounded`）、`system/task_progress`、`system/task_updated`（`status`）、`system/task_notification`（`completed` / `stopped`）、`system/background_tasks_changed`（当前全部后台任务）。

## 方案概述

**进程只在「有后台任务」时长驻。** 没有后台任务的回合和现在完全一样：写入消息、关闭标准输入、进程退出。主回合结束时如果还有后台任务，进程不退出，进入这个会话的**后台阶段**：

```
用户消息 ──► 主回合 ──result──► 有后台任务？
                                  │否 → 关闭 stdin，进程退出（同现在）
                                  │是 → 任务按成功收尾，卡片显示「后台进行中」
                                  ▼
                           后台阶段（进程长驻，stdin 打开）
                             ├─ 后台任务完成 → CLI 自动续一轮 → 主动发消息给提问人
                             ├─ 新消息（同一提问人）→ 写入同一进程 → 作为新任务的回复
                             ├─ 停单个后台任务 → stop_task
                             └─ 全部结束 / 到上限 / 节点丢失 → 收尾并告知结果
```

这样做的理由：

- 进程数只随「正在跑的后台任务」增长，空闲会话不占节点资源。
- 不在后台阶段时，系统提示、模型、凭据仍然每轮重建，现有逐轮逻辑不用改。
- 后台阶段内一个会话只有一个进程，避免了「另起进程续会话」的误报和分叉。

## 接口约定

三层按这份约定并行开发，以这里为准。

### 后台任务快照

驱动对外的后台任务一律用这个形状（下文记作 `Task`）：

```json
{"id": "a01d89e8…", "description": "订单导出接口", "kind": "agent", "status": "running",
 "started_at": 1795425877, "ended_at": null, "last_progress": "跑后端测试", "last_progress_at": 1795425990}
```

- `kind`：`agent`（`task_type=local_agent`）或 `bash`（`local_bash`）。只列主会话直接开的后台任务；子任务内部再开的（带 `parent_task_id`）不单独列，它们的进度算到父任务上。
- `status`：`running` / `completed` / `stopped` / `failed`。
- `last_progress` 取 `task_progress` 的 `description`，截断到 80 字；时间是 Unix 秒。

### 驱动（relay-claude，Unix socket）

| 接口 | 说明 |
|---|---|
| `POST /v1/chat/completions` | 现有接口。新增请求字段 `live_owner`（提问人的用户 id，字符串）、`live_deadline`（Unix 秒）。行为见下 |
| `POST /live/attach` `{"session_id"}` | SSE，接上后台阶段的输出。会话不在后台阶段时返回 404 `{"error":{"code":"not_live"}}` |
| `POST /live/stop_task` `{"session_id","task_id"}` | 200 `{"ok":true}`；会话不在后台阶段时 404 |
| `POST /live/close` `{"session_id","reason"}` | `reason` 为 `user` / `expired` / `reset`；200 `{"ok":true}`，进程退出后 attach 流收到 `background_end` |
| `GET /live/list` | 200 `{"sessions":[{"session_id","owner","deadline","busy","tasks":[Task]}]}`；`busy` 表示此刻有回合在跑 |
| `GET /health` | 现有字段外加 `"live_sessions": true`（能力）和 `"live_count": N` |

`/v1/chat/completions` 的新行为：

- **新进程**：照现在起进程，但标准输入保持打开；环境里设 `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` = `(live_deadline - 现在 + 300) * 1000`，没有 `live_deadline` 时不设。
- **会话在后台阶段**：`live_owner` 必须与进程的 owner 相同，否则 409 `{"error":{"code":"live_owner_mismatch"}}`。相同时，把这条消息写进进程的标准输入，并先发一帧 `{"x_runtime":{"live_joined":{"tasks":[Task]}},"choices":[]}`。进程形态相关的字段（模型、系统提示、环境变量、MCP 配置）这时一律忽略。
- **任意流中**：后台任务表变化时发 `{"x_runtime":{"background_tasks":[Task]},"choices":[]}`，同一秒内的多次变化合并为一帧。
- **本轮 `result` 之后**：
  - 没有 `running` 的后台任务：关闭标准输入，进程退出，流照现在结束。
  - 还有：先发 `{"x_runtime":{"background":{"tasks":[Task],"deadline":<live_deadline>}},"choices":[]}`，再发正常的 finish、usage 和 `[DONE]`。进程进入后台阶段。
- **请求断开（停下）**：
  - 进程没有后台任务：照现在的 SIGINT、KILL 流程。
  - 有后台任务：发 `interrupt` 控制指令（会连带停掉全部后台任务），读到 `result` 后关闭标准输入，进程退出。attach 流收到 `background_end`，`state` 为 `stopped`。
- **保活**：所有 SSE 流每 20 秒发一行 `: keepalive`，避免守护进程和平台 120 秒读超时。

`/live/attach` 的帧，按出现顺序：

- 后台任务变化：`{"x_runtime":{"background_tasks":[Task]},"choices":[]}`。接上时先发一份当前快照。
- 后台完成通知续出的回合：与 chat 流相同的正文、工具、思考增量，回合结束时发 usage chunk，再发 `{"x_runtime":{"turn_end":{"merged":false}},"choices":[]}`。
- 用户新消息接管了这一回合时，这一回合的内容只走 chat 请求，attach 流只发一帧 `{"x_runtime":{"turn_end":{"merged":true}},"choices":[]}`。
- 结束：`{"x_runtime":{"background_end":{"state":"finished|stopped|expired|failed","tasks":[Task]}},"choices":[]}`，然后 `[DONE]`。`finished` 是后台任务全部结束、最后一个回合也已收尾；`failed` 是进程异常退出。
- 同一会话只保留一个 attach 连接，新连接接上时旧连接收到 `[DONE]` 后关闭。attach 断开不影响进程。

驱动自己在 `live_deadline` 到点时按 `reason=expired` 执行 close，作为平台调度之外的兜底。

### 守护进程（runtime_daemon）

- 放行上面四个 `/live/*` 路径，原样转发到驱动，流式回传。
- 心跳能力项新增 `claude_background_sessions_v1`，条件是 claude 驱动的 `/health` 含 `live_sessions: true`。
- 每次心跳前读一次驱动的 `/live/list`：
  - 后台阶段会话数计入占用的槽位；
  - 有后台阶段会话的驱动不算空闲，不参与换代回收；
  - 心跳里带上 `live_sessions: [session_id]`，平台据此认定哪些会话已丢失。

### 平台

- 请求体新增 `live_owner`、`live_deadline`，只发给上报了 `claude_background_sessions_v1` 的节点。
- 解析器把 `x_runtime.background` / `background_tasks` / `live_joined` / `turn_end` / `background_end` 解成事件。
- 节点心跳里的 `live_sessions` 不再包含某个 `running` 的后台会话，且 attach 重连返回 404 时，记为 `lost`。

## 各层改动

### relay-claude（节点上的 Go 驱动）

1. **会话进程表**：`liveSessions[session_id]` 记录还在后台阶段的进程，包括标准输入写端、输出路由、后台任务表（由 `background_tasks_changed` 维护）、所属提问人、到期时间。
2. **一轮的开法**：收到 `/v1/chat/completions` 时：
   - 会话不在表里：照现在起进程，但标准输入改为可写管道，写完第一条消息后先不关。
   - 会话在表里：不起新进程，把消息写进它的标准输入。
3. **一轮的收法**：读到本轮的 `result` 后：
   - 后台任务表为空：关闭标准输入，等进程退出，结束这次请求（同现在）。
   - 不为空：在结束这次请求前发一个 `x_runtime.background` 帧，带上后台任务列表；然后结束请求，进程转入后台阶段，登记到会话进程表。
4. **后台阶段的输出**：新增 `POST /live/attach`（SSE，见「接口约定」）。平台的「后台观察」调用接上来后，接收：
   - 后台任务的开始、进度、完成帧；
   - CLI 因完成通知续出的回合，按 `result` 切成一段段，每段一个 `x_runtime.turn_end` 帧。

   同一时刻只允许一个观察者接入，重复接入时顶掉旧的。
5. **输出路由**：写入用户消息后，到下一个 `result` 之前的所有输出都算这条消息的回复，走它自己的请求。后台观察那边只收到一个「已并入新消息的回复」标记。这对应实测里的「并入本轮」：一个回复可能同时答了用户的新消息和刚完成的后台任务。
6. **控制接口**：
   - `POST /live/stop_task`：转成 CLI 的 `stop_task`。
   - `POST /live/close`：先对每个后台任务调 `stop_task`，再关闭标准输入，进程 30 秒内不退出就 KILL 进程组。
   - 路径用 `/live/` 前缀，避开现有的会话查看页 `/session/`。
   - 现有请求断开（用户点「停下」）时，后台阶段之外的行为照旧。后台阶段内断开，改发 `interrupt`，不杀进程，原因见「停下的语义」。
7. **CLI 自己的上限**：进程环境里设 `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`，取平台上限加 5 分钟。它只在标准输入被意外关闭时兜底，平时上限由平台控制。
8. **能力上报**：心跳里新增 `claude_background_sessions_v1`。平台只对上报了这项的节点启用本功能。

### runtime_daemon

- 新增两种调用：`attach` 和 `session_control`（`stop_task` / `close`），转发到驱动的对应接口。
- **空闲判断**：有后台阶段会话的驱动不算空闲。座席切换、驱动换代、守护进程升级都要等它清空，或由管理员确认后强制 `close`。升级脚本统计在途对话时，把后台阶段会话算进去。
- **槽位**：每个后台阶段会话占一个 `max_concurrent` 槽位，避免一个节点上堆太多长驻进程。
- **重启丢失**：守护进程或驱动重启后，上报它不再持有的后台会话，由平台按「节点丢失」收尾。

### 平台（服务端）

1. **新表 `background_runs`**（需迁移）：

   | 列 | 说明 |
   |---|---|
   | bot_id、session_key、relay_session_id | 所属会话 |
   | origin_task_id | 进入后台阶段的那一轮 |
   | owner_user_id | 进程里凭据所属的人，即那一轮的提问人 |
   | node_id | 所在节点 |
   | tasks | 后台任务快照（id、描述、状态、开始时间、最近进度） |
   | deadline | 到期时间 |
   | state | `running` / `finished` / `stopped` / `expired` / `lost` |
   | card_ref | 用来更新的那张飞书卡片 |

   同一 (bot_id, session_key) 最多一行 `running`。

2. **进入后台阶段**：chat 任务收到 `x_runtime.background` 时：
   - 按成功收尾；
   - 写入 `background_runs`；
   - 卡片定稿时保留一个「后台进行中」区块（见飞书部分）；
   - 创建一个 `background_watch` 任务，发起 `attach` 调用。

3. **后台观察任务（`background_watch`）**：
   - 不参与会话串行，也就是不占 `serialize_session`，新消息不会排在它后面。
   - 收到进度帧时更新 `background_runs.tasks` 和卡片区块。
   - 每收到一个 `x_runtime.turn_end`，把这一段回复作为**新消息**发到原会话，回复在原消息下，并 @ 提问人；连接已收到 `x_runtime.merged`（由新消息那一轮负责回复）的段落不发。
   - 后台任务表清空、驱动关闭进程后，状态记为 `finished`，卡片区块改为「后台全部完成」。
   - 它本身不调用模型，只转发内容，不计 Token。用量随每段回复的 `result` 记到会话上（续聊用量是累计值，沿用现有按差值记账的逻辑）。

4. **后台阶段内来了新消息**：
   - **同一提问人**（私聊永远是同一人）：照常建 chat 任务、照常调用。驱动发现会话在后台阶段，把消息写进同一进程，模型能看到后台任务的状态。这一轮的凭据仍是进程里那一份，所以**不重新签发**令牌；轮次上下文（时间、身份）照常放在消息正文里。
   - **群里换了人**：不能把他的消息写进带着前一位提问人凭据的进程，所以排队。排队卡增加后台任务的说明和两个选项：「等后台做完」「停掉后台任务，先处理我的」。后者需要他有停止权限，规则同现有「停下」。
   - 「重置会话」：先 `close` 后台阶段，记为 `stopped`，再清会话。
   - 换运行时、换模型、换工作目录时，`clear_bot` 前同样先 `close`。

5. **上限与凭据**：
   - 不新增配置，整段工作都跟着员工的任务超时（`sse_timeout_seconds`）走。令牌有效期保持现状，即 `min(sse_timeout_seconds, 任务超时)`，从主回合开始时签发。
   - `deadline` = 主回合开始时间 + `sse_timeout_seconds`，与令牌到期时间一致，后台阶段不会出现令牌已过期、任务还在跑的情况。调度器到点对该会话调 `close`，记为 `expired`。
   - 也就是说，主回合加后台阶段总共不超过员工的任务超时。需要更长时间的员工，调大任务超时即可。
   - 后台阶段内写入的新消息沿用进程里的令牌，不重新签发，也不顺延 `deadline`。到期之后再来的消息起新进程，令牌重新签发。
   - 代理调用按任务 id 绑定的加密令牌，在后台阶段沿用 origin_task_id。

6. **收尾告知**：

   | 结局 | 告诉提问人的内容 |
   |---|---|
   | `finished` | 只更新卡片区块，完成内容已经逐条推送过 |
   | `expired` / `stopped` | 主动发一条消息，列出没做完的后台任务和已跑时长；提示可以说「接着做」，这时模型能从会话记录里看到哪些被停了 |
   | `lost`（节点重启、离线超过 2 分钟） | 主动发一条消息，说明后台任务因节点中断而停止，工作区里已经写下的改动还在 |

### 飞书卡片

主回合的卡片定稿后，下方增加「后台任务」区块，由后台观察任务更新：

```
后台任务 · 2 项进行中，最长可跑到 16:40
① 订单导出接口      进行中 12 分钟 · 最近：跑后端测试   [停止]
② 列表页加筛选      进行中 8 分钟  · 最近：改前端列表   [停止]
③ 修复分页错位      已完成 ✓（结果已在下方回复）
```

- 主回合卡片进入后台阶段时，标题写「已开工 · 后台 N 项进行中」，不用 ✅；全部完成后改为「后台全部完成」。
- 后台阶段内本人发的新消息，它的卡片顶部加一行「接着后台进行中的会话处理，后台还有 N 项在跑」（来自 `live_joined`）。
- 每行的「最近」取 `task_progress` 的描述，刷新间隔不少于 30 秒。
- 「停止」按钮调 `stop_task`，只有提问人和员工管理员能点。
- 某一项 10 分钟没有任何进度，显示「好久没有进展」，但不判定为卡住。后台任务常会长时间跑测试或构建。
- 原有「静默 5 分钟 / 疑似卡住 15 分钟」只用于主回合，后台阶段不套用。
- 每个完成的回合作为一条新消息回复在原消息下，开头写明「后台任务 ③ 完成」，方便在消息流里区分。

### 停下的语义

实测 `interrupt` 会连带停掉所有后台任务，所以「停下」要区分场景：

| 场景 | 按钮 | 做法 |
|---|---|---|
| 主回合在跑，没有后台任务 | 停下 | 同现在（断开请求 → 杀进程组） |
| 后台阶段，模型正在回答新消息或完成通知 | 停下这次回答 | 弹确认：「会同时停掉 N 个后台任务」，确认后 `interrupt`，再按 `stopped` 收尾 |
| 后台阶段，只想停某一项 | 卡片每行的「停止」 | `stop_task`，其余继续 |
| 全部不要了 | 停掉全部后台任务 | `close` |

### 提示词

Claude 运行时的员工，平台提示里加两条规则：

- 可以把长任务交给后台子任务；平台会在每个完成时主动通知提问人，所以回复里写「已在后台开始 N 项，完成后会逐条通知你」，不要承诺「全部做完再统一汇报」。
- 被问进度时，以后台任务的实际状态为准；看到任务 `stopped` 时，说明是被谁停的、停在哪，不要自行「接手重做」，除非提问人要求。

## 上线范围

不做灰度，不加开关：所有 Claude 运行时的员工都按本设计运行。

- 节点上报了 `claude_background_sessions_v1`，就走后台阶段。
- 节点还没升级的，暂时保持现状（CLI 等 10 分钟后停后台任务），提示词里也不加上面两条规则；运行时页面标出待升级的节点。
- 发布顺序：先升级所有节点的驱动，再发平台，避免出现平台已改、节点未改的半套状态。

## 分期

1. **驱动与守护进程**：会话进程表、可写标准输入、`x_runtime.background` 帧、`attach`、`stop_task` 和 `close` 接口、槽位与空闲判断、能力上报。用本文实测的脚本做集成测试，包括并入本轮、`interrupt`、`stop_task`、上限兜底。
2. **平台**：`background_runs` 迁移、`background_watch` 任务、新消息写入后台阶段、群里换人排队、上限与令牌有效期、重置和换运行时先 `close`、收尾告知。
3. **飞书**：后台任务区块、每行停止按钮、完成回复、停下确认。
4. **提示词与上线**：提示词规则、运行时页面标出待升级节点、先升节点再发平台。

## 风险与待验证

- **`--max-turns`**：长驻进程里是按进程累计还是每个回合单独计算，没有实测过。如果是累计，要在后台阶段开始时调高，或改为按回合计数。
- **进程内存**：长驻进程和后台子任务都会占内存。节点内存紧张时，可能出现过此前「内存耗尽卡死」那种情况。槽位限制是第一道防线，需要观察一周的峰值。
- **任务超时较短的员工**：后台阶段受任务超时约束，超时只有 1 小时的员工，长开发任务仍可能被平台按 `expired` 停掉。区别在于，现在会明确告诉提问人哪些没做完。
- **并入本轮的回复**：用户的新消息恰好和后台完成通知并在同一轮时，回复会同时包含两部分。卡片要写明「也包含后台任务 ③ 的结果」，后台观察任务不再重复发这一段。
- **节点环境差异**：实测在开发机上做，节点上的设置、MCP 与 hooks 不同，第 1 期要在节点上再跑一遍同样的脚本。
