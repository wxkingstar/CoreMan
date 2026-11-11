# 个人助理第 1 期：总览

**Spec:** `docs/superpowers/specs/2026-11-11-personal-assistant.md`

第 1 期做完后：成员私聊助理应用即可开通，在私聊里让它办事，所有写操作经 Tower 审批，个人内容加密入库，节点上不留明文。

第 1 期包含六块相对独立的工作，各出一份可单独执行、单独评审的子计划。本文件只定顺序、依赖和它们之间的接口。

## 顺序与依赖

| 编号 | 子计划 | 依赖 | 交付后可验证什么 | 计划文件 |
|---|---|---|---|---|
| 0 | 节点侧前置验证 | 无 | 沙箱方案与出网限制方式定下来 | 本文件「步骤 0」 |
| 1A | 助理记录与应用凭证 | 无 | 库里能存在助理记录；应用凭证同步；被关闭的能力在服务端拒绝 | `2026-11-11-personal-assistant-1a-records.md` |
| 1B | 网关：一个应用一条连接 | 1A | 私聊按发送人投递到本人的助理记录；群消息丢弃；进群即退 | `2026-11-11-personal-assistant-1b-gateway.md` |
| 1C | 加密存储 | 1A | 助理记录的内容列在库里是密文；销毁密钥后不可读 | `2026-11-11-personal-assistant-1c-encryption.md` |
| 1D | 审批闸门 | 1A、1C | 写类个人工具不执行，改为 Tower 审批单；批准后原样执行 | 1C 合并后写 |
| 1E | 节点隔离 | 0、1A、1C | 助理轮次在沙箱里跑；轮次结束后节点上无转录 | 步骤 0 完成且 1C 合并后写 |
| 1F | 开通与停用 | 1A–1E | 端到端：开通 → 授权 → 办一件事 → 审批 → 执行 → 停用 | 最后写 |

1B、1C 互不依赖，可以并行；两份计划的迁移分别写作 `0086`、`0087`，开工时按当时的迁移头顺延。1D–1F 在前面的合并后再写，因为它们要引用落地后的真实接口和行号。

## 子计划之间的接口

名称在这里定下，各子计划照用，不另起名字。

| 提供方 | 名称 | 含义 |
|---|---|---|
| 1A | `Bot.kind`、`Bot.owner_id`、`Bot.platform_app_id`、`BOT_KINDS` | 助理记录的标识 |
| 1A | `coreman.core.assistants.records.is_assistant(bot) -> bool` | 各处按类型分支时统一用它 |
| 1A | `coreman.core.assistants.records.new_assistant_bot(owner, app, *, relay_server_id, model, working_dir, credentials_enc) -> Bot` | 1F 开通时建记录 |
| 1A | `coreman.core.assistants.apps.app_credentials(cipher, app) -> dict[str, str]` | 1B 建连接时取应用凭证 |
| 1A | `coreman.core.assistants.apps.sync_bot_credentials(session, cipher, app) -> int` | 应用凭证变更后同步副本 |
| 1B | `coreman.core.assistants.routing.assistant_for(session, app_id, user_id) -> Bot \| None` | 按发送人找助理记录 |
| 1C | `coreman.core.assistants.keys.data_cipher(session, cipher, owner_id) -> Cipher` | 取某人的数据密钥对应的加解密器 |
| 1C | `coreman.core.assistants.keys.destroy_key(session, owner_id) -> None` | 1F 停用时销毁 |
| 1C | `coreman.core.assistants.keys.ensure_key(session, cipher, owner_id)` | 1F 开通时签发；在已销毁的行上签发下一代 |
| 1B | `AssistantInbound.on_unprovisioned(session, *, platform_user_id, user_id, raw)` | 没有助理记录的人私聊时的钩子，1F 在这里发开通卡片 |
| 1D | 表 `assistant_approvals`；`coreman.core.assistants.approvals.submit(...)`、`withdraw_pending(session, bot_id, reason) -> int` | 1F 停用时撤回待批 |
| 1E | 节点守护进程接口：轮次请求带 `sandbox: true` 与 `transcript` 下发、轮次结束回传 | 1F 端到端依赖 |

## 步骤 0：节点侧前置验证

- [ ] **0.1 在一台共享 Runtime 节点上运行沙箱探测脚本**

脚本是 `scripts/probe-runtime-sandbox.sh`。拷到节点上，以节点守护进程所用的系统用户运行，参数是全部员工工作目录的上级目录：

```bash
bash probe-runtime-sandbox.sh /path/to/workspace-root
```

脚本只读、不需要 root、不打印凭证内容，只建一个临时目录并在退出时删除。

- [ ] **0.2 按输出判定方案**

| 输出 | 判定 |
|---|---|
| `bwrap=MISSING`，或 `T1 FAIL` | 该节点不能用沙箱。先确认能否安装 bubblewrap 并开启非特权用户命名空间；不能则 1E 采用独立系统用户方案 |
| `other_transcripts_visible=0`、`workspace_root_entries_visible=0`、`write_outside=denied` | 文件隔离成立 |
| T3 / T4 `exit=0` 且私有目录里出现转录 | 两种 CLI 可在沙箱里运行，转录落在私有目录 |
| T3 / T4 报认证或写凭证失败 | 登录凭证不能只读挂载。1E 改为：守护进程在沙箱外负责刷新登录令牌，每轮把当时有效的凭证复制进沙箱的私有配置目录，轮次结束删除 |
| T5 输出不是 `http=200` | 断网命名空间生效；出网限制采用「沙箱断网 + 本机代理放行平台与模型接口地址」 |
| `cgroup_fs=cgroup2fs` 且 `nft=yes` | 备选：按进程组做防火墙规则。只在代理方案不可行时采用 |

- [ ] **0.3 补测 Claude 转录还原后的续聊**

在任一装有 Claude 命令行且登录有效的机器上：

```bash
mkdir -p /tmp/resume-check && cd /tmp/resume-check
claude -p "Remember the code word: bluewhale47. Reply: noted" --output-format json --max-turns 2 < /dev/null > first.json
```

从 `first.json` 取 `session_id`，在 Claude 配置目录的 `projects/` 下找到同名 `.jsonl`，移到别处后运行：

```bash
claude -p "What is the code word?" --resume <session_id> --output-format json --max-turns 2 < /dev/null
```

预期报 `No conversation found`。把文件放回原处再运行同一条命令，预期 `result` 含 `bluewhale47`。Codex 的同类测试已通过。

- [ ] **0.4 把结论写回设计文档的「前置验证」表，并提交**

三项各记一行：结论、依据（哪台节点、哪天）。结论与设计不符时，同一次提交里改第三节。

## 全期约束

- 公开仓：代码、测试、文档、提交信息只用通用占位（`example.com`、`demo-bot`、`cli_example`），提交前跑仓库约定的敏感词自查。
- 每份子计划开工前合并最新 `main`，迁移编号顺延当时的迁移头。
- 对普通员工（`kind='standard'`）的行为不变。每份子计划的测试都包含一条「普通员工不受影响」的断言。
- 验证命令：`uv run pytest <路径>`、`uv run ruff check .`、`uv run mypy coreman`、`uv run alembic check`。
