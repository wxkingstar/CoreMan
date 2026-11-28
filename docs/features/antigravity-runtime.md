# Antigravity 运行时

运行时除了 Claude Code、Codex 和 OpenCode，也能用 [Antigravity](https://antigravity.google) CLI（命令 `agy`）跑 AI 员工。它背后可以选 Gemini 4 系列，也可以选 Claude、GPT 等模型，用的是登录账号的套餐额度。

> 在把它接进生产前，请先读 Antigravity 的服务条款与常见问题里关于第三方软件、自动化和账号轮换的说明，确认你的账号类型允许这样用。

## 准备节点

1. 在运行时用户环境里安装 Antigravity CLI（`agy` 在服务的 `PATH` 里），1.5.19 实测过。
2. 升级运行时到包含 `runtime-antigravity` 驱动的版本。节点发现 CLI 后，心跳会让平台为这台机器补建一个 Antigravity 实例（relay），不用重新安装。
3. 登录：在运行时所在机器上，用运行时的系统用户执行 `agy`，按提示打开链接、把授权码贴回终端。管理台目前不能代为登录。

节点列表里 Antigravity 单独一列，显示安装与登录状态、版本、登录的 Google 账号和额度。

## 模型

模型名写作 `antigravity/<模型>`，例如 `antigravity/gemini-4-neon`；驱动调 CLI 前去掉开头的 `antigravity/`。CLI 自己的模型名带思考档位（`gemini-4-neon-high`），平台把它拆成模型与档位两项：档位取 low / medium / high / xhigh，CLI 没有 max，选了 max 按 xhigh 跑。每个模型能选哪些档位由 Antigravity 决定，例如同一个 Claude 模型在这里只到 high。

迁移 `0140` 预置了几种常用模型并把 Gemini 4 Neon 设为默认；节点登录后，驱动用 `agy models` 列出账号能用的模型，随心跳进模型目录。

## 驱动做了什么

`runtime_daemon/drivers/cmd/relay-antigravity` 每一轮跑一个 `agy acp` 进程，经标准输入输出上的 Agent Client Protocol（JSON-RPC）与它对话：

- **续聊**：CoreMan 会话绑定一个 Antigravity 会话（`<会话>/antigravity_session.txt`），后续轮次只发最新一条消息。绑定的会话不在了就用平台的续接文本开新会话，并在回复前说明。
- **平台规则**：新会话把系统提示随建会话的请求交给 CLI（追加到它自己的系统提示之后），大小不受限。这段文字在会话存续期间不能替换，所以规则后来变了时另行处理：能放进一个规则文件（约 20 KB 以内）的写进会话自己的目录，CLI 每轮重新读取；更大的写成普通文件，并在这一轮提问的开头让模型先读它，同一版规则只提示一次。
- **MCP**：协作、本人飞书 / 企微工具、业务系统目录、业务系统 MCP 以及工作目录 `.mcp.json` 里的 MCP 技能（挂成 `skill_<名字>`，与 Codex 同名），都随建会话的请求交给 CLI。令牌只经子进程的标准输入传递，不进命令行，也不写到节点的 Antigravity 配置或工作目录里。没有凭据的服务不挂载。
- **权限**：默认免审批（`--dangerously-skip-permissions`，与 Codex 一致）。`read-only` 用 CLI 的计划模式并拒绝所有审批请求；`workspace-write` 用「接受编辑」模式，其余需要审批的操作同样拒绝。
- **附件与图片**：图片以原生图片块传入，同时落在工作目录的 `.relay_uploads/` 下；其它附件给出路径。正文里链接的本地图片 `![说明](路径)` 随流回传给飞书，包括生图工具存在会话目录里的图片。
- **过程与用量**：工具调用显示为工具进度，思考内容（思考档位较高时才有）显示为思考过程；每轮结果里的 token 即本轮用量。CLI 不报费用，记为 0。
- **停止**：用户中途停止时驱动经协议取消这一轮，等它交回用量后再结束进程，会话保留。
- **分叉**（飞书话题接着群里的上下文）：CLI 只能在交互界面里分叉，驱动改为复制会话库并换成新的会话 ID。这依赖 CLI 的存储格式；复制失败时改用文本续接，不影响回复。

## 额度

节点每小时执行一次 `agy -p "/usage" --output-format json`（不消耗对话轮次）读取额度。Antigravity 按模型分组计额度，每组各有 5 小时与每周两个窗口；平台每个实例只显示一对窗口，取各组里用得最多的那个。

额度用尽时 CLI 的原话没有实测到，平台先按 `RESOURCE_EXHAUSTED` 与 `quota` 字样识别并触发「切到空闲实例」，拿到真实告示后再收紧。

## 暂不支持

- **多席位**：CLI 的凭据不跟配置目录走，一台运行时只有一个账号。
- **管理台登录与 MCP 技能授权**：都要在节点上用 `agy` 完成（MCP 授权在它的 `/mcp` 面板里）。
- **换运行时带走会话**：换到另一台 Antigravity 运行时后用文本续接，与跨后端相同。
- **个人助理与沙箱、后台任务常驻、任务列表**：未接入。CLI 的提问工具会把问题作为正文发出，用户下一条消息即回答。

## 环境变量

机器人与技能的环境变量里不能出现 `AGY_`、`ANTIGRAVITY_` 开头的变量（会改写 CLI 的账号与端点），请求会被拒绝；驱动还会丢掉 `GOOGLE_GEMINI_BASE_URL`、`GEMINI_DIR` 与 `HOME`。

需要数据库迁移 `0140`，API、worker 与运行时节点都要升级。
