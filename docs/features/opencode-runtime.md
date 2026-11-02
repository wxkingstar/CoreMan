# OpenCode 运行时

运行时除了 Claude Code 和 Codex，也能用 [OpenCode](https://opencode.ai) CLI 跑 AI 员工。OpenCode 背后可以接多家模型；CoreMan 默认按 OpenCode Go 套餐（`opencode-go` 服务商）配置模型目录，例如 Kimi K3、GLM-5.3、DeepSeek V4 Pro、Qwen3.8 Max。

## 准备节点

1. 在运行时用户环境里安装 OpenCode CLI（`opencode` 在服务的 `PATH` 里），版本 1.19 起实测过。
2. 升级运行时到包含 `runtime-opencode` 驱动的版本。节点发现 CLI 后，心跳会让平台为这台机器补建一个 OpenCode 实例（relay），不用重新安装。
3. 登录：在「运行时管理」→「席位」的 OpenCode 一栏点「登录」，在 OpenCode 控制台订阅 OpenCode Go、生成 API Key，贴回管理台。Key 由节点写进该席位的 `auth.json`（权限 600），平台不保存。也可以在机器上自己执行 `opencode auth login`，结果一样。

节点列表里 OpenCode 单独一列，显示安装、登录状态与已登录的服务商。OpenCode Go 的额度只在 OpenCode 控制台能看到，CLI 不报，所以没有额度列。

## 模型

模型名写作 `opencode/<服务商>/<模型>`，例如 `opencode/opencode-go/kimi-k3`；驱动调 CLI 前去掉开头的 `opencode/`。迁移 `0072` 预置了几种 OpenCode Go 模型并把 Kimi K3 设为默认；节点登录后，驱动用 `opencode models opencode-go` 列出账号能用的模型，随心跳进模型目录。思考档位经 `--variant` 交给 OpenCode，模型不认的档位会被忽略。

## 驱动做了什么

`runtime_daemon/drivers/cmd/relay-opencode` 与 relay-codex 同形，每一轮跑一次 `opencode run --format json`：

- **续聊**：CoreMan 会话绑定一个 OpenCode 会话（`<会话>/opencode_session.txt`），后续轮次只发最新一条消息并带 `--session`。绑定的会话不在了（`Session not found`）就用平台的续接文本开新会话，并在回复前说明。
- **平台规则**：系统提示每轮写成 `<会话>/coreman_rules.md`，经配置层的 `instructions` 交给 OpenCode，续聊时也用当前规则。
- **配置层**：MCP 服务与权限经 `OPENCODE_CONFIG_CONTENT` 给这一轮，不改节点上的 OpenCode 配置。协作、本人飞书 / 企微工具、业务系统目录、业务系统 MCP 的令牌写成 `{env:变量}`，只在子进程环境里；没有凭据的服务显式关闭。工作目录 `.mcp.json` 里的 MCP 技能挂成 `skill_<名字>`，与 Codex 同名。
- **权限**：默认放开（`permission: "allow"`，与 Codex 的免审批一致）；`read-only` 禁止编辑与 shell，`workspace-write` 禁止工作目录之外。
- **附件与图片**：附件放在工作目录的 `.relay_uploads/` 下用 `--file` 传入；正文里链接的本地图片 `![说明](路径)` 随流回传给飞书。
- **过程与用量**：工具调用显示为工具进度，推理显示为思考过程；每步的 token 相加作为本轮用量（含缓存读写）。
- **出错**：模型调用失败时 OpenCode 只写日志、既不输出事件也不退出（实测）。驱动读 `--print-logs` 的错误日志，20 秒内没有新进展就结束这一轮并回报原因；`usage limit reached` 一类的额度告示会触发「切到空闲实例」。

## 席位与换运行时

- 席位目录经 `OPENCODE_SEAT_HOME` 交给驱动，驱动只在运行 `opencode` 时把它设为 `XDG_DATA_HOME`；会话库用 `OPENCODE_DB` 固定在默认位置，各席位共用，换席位后会话照常续上。MCP 技能的 OAuth 令牌按席位分开。
- MCP 技能授权用 `opencode mcp auth skill_<名字>`，流程与 Claude / Codex 相同。
- 换运行时（OpenCode → OpenCode）时，源节点把会话 `opencode export` 成 `<会话>/opencode_import.json`，随绑定搬到目标节点；目标节点的驱动续聊前 `opencode import`（保留会话 ID，已有同 ID 会话时合并，不丢消息）。跨后端换（如 Claude → OpenCode）时与其他后端一样用文本续接。
- OpenCode 没有自带记忆，与 Codex 一样在提示里写明 `.coreman-memory/` 的位置。

## 环境变量

机器人与技能的环境变量里不能出现 `OPENCODE_` 开头的变量（会改写 CLI 的配置与权限），请求会被拒绝；驱动也会丢掉这类变量和 `XDG_DATA_HOME`。

需要数据库迁移 `0072`，API、worker 与运行时节点都要升级。
