# 云签名代理与 Tower 审批

CoreMan 可以替 Agent 给阿里云 OpenAPI 请求签名：Agent 机器上不放 AccessKey，只把未签名的业务参数发给 CoreMan；CoreMan 判定风险，低风险直接签名转发，高风险要求先经 Tower 审批，拿到一次性令牌后才签名。本文是接入与上线指南；部署专属的取值（域名、实例、命名空间、ACL、Tower 地址等）放在各自的部署仓库里，不进本仓。

## 名词

- **Tower**：外部审批服务。CoreMan 在 Tower 里登记为一个 agent 发送端（sender），每个被保护的阿里云产品登记为一个目标系统（target）。审批人批准后，Tower 签发一次性令牌并回调 CoreMan；代理签名前向 Tower 核销令牌，令牌与请求的方法、路径、请求体和调用方（机器人 key）的哈希绑定。哈希规则见 `coreman/core/cloud_proxy/tower_gate.py`（从 Tower 原样复制）。
- **调用代理的 Skill**：把阿里云调用改成发往 `COREMAN_CLOUD_PROXY_URL` 的 Skill，以及一个负责向 Tower 提交审批、领取令牌的 Tower 审批 Skill。它们不在本仓，按各自的说明安装。

## 工作方式

1. 每轮对话，CoreMan 给 Agent 下发 `COREMAN_CLOUD_PROXY_URL`（`<PUBLIC_BASE_URL>/api/runtime/cloud`）、`COREMAN_CLOUD_PROXY_TOKEN`（本轮能力令牌，只证明「这一轮、这个员工、这个发言者」）和机器人配置的审批人 `COREMAN_APPROVERS`。
2. Skill 向 `POST <COREMAN_CLOUD_PROXY_URL>/aliyun/{alidns|domain|mse|slb}` 发 `application/x-www-form-urlencoded` 的业务参数（含 `Action`，不含签名公共参数）。
3. 代理只放行登记过的动作（`coreman/core/cloud_proxy/aliyun.py` 的 `PRODUCTS`），其余返回 `403 action_not_allowed`。读动作直接签名转发。
4. 写动作先做服务端风险判定（`coreman/core/cloud_proxy/risk.py`），判定需要的现状由代理用同一把 AccessKey 自己读：
   - 低风险：直接签名转发。个人助理例外：所有者在场的一轮里不转发，暂存后等所有者在飞书确认卡上确认（见下面「个人助理的审批闸门」里的「所有者在场时的云服务低风险写」），返回 `202`，正文 `{"status": "pending_confirmation", …}`。
   - 高风险：返回 `428 approval_required`，带 `target`、`caller`、`requester`、`external_id`、`url`、`risk_reasons`。Agent 用 Tower 审批 Skill 提交审批；批准后带 `X-Tower-Approval: <令牌>` 原样重发同一份请求体，代理在 Tower 核销成功后才签名转发。
   - 个人助理无人在场的一轮（平台发起的交回、子任务、结果回合等，见 [飞书](feishu.md)「平台自己发起的轮次」）：读动作照常。能提交审批的轮次（交回、结论投回、审批结果回合）里，写动作先照常做风险判定（只读），低风险的不转发，而是暂存成个人助理的审批单、以所有者为发起人提交 Tower（见下面「个人助理的审批闸门」里的「无人在场时的代理写操作」），返回 `202`，正文 `{"status": "pending_approval", "approval_id": …, "hint": …}`，审计记 `write_low` / `approval_required`；高风险的返回 `403 owner_turn_required`（带 `risk_reasons`，不建挂起单、不走 428），审计记 `write_high` / `denied`。不能提审批的轮次（子任务、冷启动）写动作不论风险高低一律 `403 owner_turn_required`，在风险判定之前就拒，不读阿里云，审计记 `write_high` / `denied`。按每次调用重新加载的任务类型判断。
5. Tower 的决定通过回调（`POST /api/integrations/tower/callback`）送回 CoreMan，CoreMan 唤醒发起审批的那轮对话，让 Agent 继续执行或转述拒绝理由。回调丢失时由定时对账补上。
6. 每次调用写一行 `cloud_calls` 审计；高风险挂起单在 `cloud_pending_approvals`。

判定读不到现状、参数缺失、响应形状不对、规则所需的部署参数没配，一律按高风险处理（fail closed）。

## 设置项

写进部署的 `.env`（权限 0600，不进仓库和镜像）：

| 设置项 | 说明 |
|---|---|
| `TOWER_URL` | Tower 地址，如 `https://tower.example.com` |
| `TOWER_CALLBACK_SECRET` | Tower 登记发送端时给出的回调签名密钥 |
| `TOWER_SENDER_KEY` | 发送端 API Key（服务端只用于对账） |
| `TOWER_TARGET_KEYS` | JSON：`{"alidns":"…","domain":"…","mse":"…","slb":"…"}`，四个目标系统各自的 API Key，**四个都要有** |
| `ALIYUN_PROXY_ACCESS_KEY_ID` | 代理专用 RAM 用户的 AccessKey ID |
| `ALIYUN_PROXY_ACCESS_KEY_SECRET` | 同上的 AccessKey Secret |
| `CLOUD_PROXY_POLICY` | 可选，JSON 对象：风险判定用的部署参数，见下一节 |

- 前六项任一缺失时代理路由返回 `503 cloud_proxy_disabled`（`cloud_proxy_ready` 为假），不影响其他功能。`CLOUD_PROXY_POLICY` 不影响 `cloud_proxy_ready`。
- `PUBLIC_BASE_URL` 必须是 Agent 实际访问 CoreMan 的地址（含路径前缀，如有）：代理地址、428 里返回的 `url`、核销时算哈希用的路径都取自它。

## CLOUD_PROXY_POLICY

```json
{
  "alidns": {
    "managed_zones": ["example.com"],
    "trusted_cname_suffixes": [".example.com", ".example.net"],
    "trusted_ips": ["198.51.100.10"]
  },
  "mse": {
    "endpoint": "mse.cn-hangzhou.aliyuncs.com",
    "instance_id": "mse-example-instance",
    "namespaces": {
      "00000000-0000-4000-8000-000000000001": "test",
      "00000000-0000-4000-8000-000000000002": "staging",
      "00000000-0000-4000-8000-000000000003": "prod"
    },
    "low_risk_envs": ["test", "staging"]
  },
  "slb": {
    "acls": {"acl-example-test": "test", "acl-example-staging": "staging"}
  }
}
```

| 字段 | 含义 | 缺失时 |
|---|---|---|
| `alidns.managed_zones` | 允许修改的权威域名，小写、不带结尾的点 | 所有解析写操作按高风险 |
| `alidns.trusted_cname_suffixes` | CNAME 指向这些后缀（必须以 `.` 开头）或 `managed_zones` 本身时算可信目标 | CNAME 新增、修改、启用都按高风险 |
| `alidns.trusted_ips` | A / AAAA 指向这些公网地址（规范写法）时算可信目标；私网地址本来就算可信 | 指向任何公网 IP 都按高风险 |
| `mse.endpoint` | MSE 地域 endpoint，必须形如 `mse.<地域>.aliyuncs.com`；MSE 没有全局 endpoint | `mse` 的读写都返回 `503 product_not_configured` |
| `mse.instance_id` | 唯一允许写的 Nacos 实例 ID | 所有 Nacos 写按高风险 |
| `mse.namespaces` | 命名空间 ID → 环境名 | 所有 Nacos 写按高风险；未登记的命名空间同样按高风险 |
| `mse.low_risk_envs` | 更新这些环境的配置时只做内容检查（不删键、能解析、不超过 100KB），通过即低风险；必须是 `namespaces` 里出现过的环境名 | 所有 Nacos 更新按高风险 |
| `slb.acls` | 允许加白的访问控制列表 ID → 环境名（环境名只用于说明） | 所有加白按高风险 |

- 校验是严格的：未知字段、空值、格式不对都会让进程启动失败，不会静默忽略。
- 可信目标决定了哪些解析不经审批。把云厂商的公共后缀（如 `.aliyuncs.com`）列为可信，等于允许不经审批把记录指向任何人都能在该厂商开出来的资源；只在确有需要、接受这一风险时才加。

## 与部署无关的规则

不随配置变化，摘要如下（细节以 `risk.py` 为准）：

- **alidns**：只允许写 A、AAAA、CNAME、TXT、MX、CAA、SRV；`_acme-challenge`、根域名与泛解析、停用或修改正在生效的记录、MX/CAA/SRV、根域名或下划线开头的 TXT、指向不可信目标、最近 1 小时已有 3 次以上人工变更，都按高风险。备注修改只检查域名与受保护记录。
- **domain**：任何写都按高风险（目前没有登记写动作）。
- **mse**：新建时目标已存在按高风险；更新时目标不存在、内容为空、删除现有的键、无法解析、含 YAML 别名或重复键都按高风险。新建一份目标环境里还不存在的文档判为低风险。
- **slb**：每次只能加一条；比 IPv4 /24、IPv6 /48 更宽、非公网地址、比单个 IP（IPv6 为 /64）更宽的网段、`keep-` 开头的备注，都按高风险。
- 所有写动作：出现未登记的参数，或参数名仅大小写不同，按高风险。

## Tower 侧登记

在 Tower 里（具体命令以 Tower 的文档为准）：

1. 登记 CoreMan 为 agent 发送端，回调地址填 `<PUBLIC_BASE_URL>/api/integrations/tower/callback`；记下发送端 API Key 与回调密钥。
2. 每个产品登记一个目标系统，key 固定为 `aliyun-alidns`、`aliyun-domain`、`aliyun-mse`、`aliyun-slb`；记下各自的 API Key。配置文档里常有口令等密钥，Tower 支持时建议让 `aliyun-mse` 的请求体不送比对模型。
3. 审批人与发起人都要在 Tower 登记：CoreMan 传的是成员的登录名（`login_name`），要与 Tower 的用户名一致。没有登录名的成员做写操作时，代理直接返回 `403 no_login_name`（读不受影响）。

## 审批人

在机器人详情 →「协作与授权」→ 审批人面板配置。可以留空：空时由发起这次对话的员工自己批。CoreMan 每轮以 `COREMAN_APPROVERS` 下发给 Agent。

## 回调与对账

- 回调：`POST /api/integrations/tower/callback`，用 `TOWER_CALLBACK_SECRET` 验签，重复回调幂等。冒烟：不带签名的请求应返回 `401 invalid_signature`；返回 `503 callback_disabled` 说明回调密钥没读到。
- 对账：`TOWER_URL` 与 `TOWER_SENDER_KEY` 都配了时，scheduler 主实例每 5 分钟翻一次 Tower 的 `GET /v1/approvals?updated_since=`，补上丢失回调的决定（游标存设置项 `tower.reconcile_cursor`）。手动补跑：`python -m coreman.cli tower reconcile`。日志关键字 `tower_reconciled`、`tower_reconcile_failed`。单条处理失败会跳过，游标照常前进，该条本地仍是 `pending`，需要重新提交审批。

## 阿里云 RAM：最小权限

新建一个代理专用 RAM 用户，只开 OpenAPI 访问，挂一条只含下列 Action 的自定义策略：

| 产品 | 读（代理放行 + 风险判定读现状） | 写 |
|---|---|---|
| alidns | DescribeDomainRecords、DescribeDomainRecordInfo、DescribeRecordLogs、DescribeDomainInfo、DescribeDomains | AddDomainRecord、UpdateDomainRecord、SetDomainRecordStatus、UpdateDomainRecordRemark |
| domain | QueryDomainList、QueryDomainByDomainName | （无） |
| mse | GetNacosConfig、ListNacosConfigs、ListNacosHistoryConfigs、GetNacosHistoryConfig、ListListenersByConfig | UpdateNacosConfig、CreateNacosConfig |
| slb | DescribeAccessControlListAttribute | AddAccessControlListEntry |

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "alidns:DescribeDomainRecords", "alidns:DescribeDomainRecordInfo", "alidns:DescribeRecordLogs",
        "alidns:DescribeDomainInfo", "alidns:DescribeDomains",
        "alidns:AddDomainRecord", "alidns:UpdateDomainRecord", "alidns:SetDomainRecordStatus",
        "alidns:UpdateDomainRecordRemark",
        "domain:QueryDomainList", "domain:QueryDomainByDomainName",
        "mse:GetNacosConfig", "mse:ListNacosConfigs", "mse:ListNacosHistoryConfigs",
        "mse:GetNacosHistoryConfig", "mse:ListListenersByConfig",
        "mse:UpdateNacosConfig", "mse:CreateNacosConfig",
        "slb:DescribeAccessControlListAttribute", "slb:AddAccessControlListEntry"
      ],
      "Resource": "*"
    }
  ]
}
```

- 没有任何删除类 Action；代理本身也拒绝清单外的 Action，RAM 是第二道。
- 能收窄 `Resource` 的（解析限受管域名、MSE 限登记的实例、SLB 限登记的 ACL）尽量收窄；Action 名以控制台策略编辑器的提示为准。

## 上线顺序

**先把代理、回调、新版 Skill 全部跑通，再撤 Agent 侧的旧 AccessKey。** 撤之前随时可以回滚。所有密钥只出现一次：直接放进密码管理器，再写进 `.env` 或环境预设；不要发聊天、写进文档或留在终端历史里。

1. Tower 侧登记（见上）。
2. 建代理专用 RAM 用户与 AccessKey。
3. 写设置项与 `CLOUD_PROXY_POLICY`，发布含迁移 `0065_cloud_proxy` 的镜像（`./deploy/coreman upgrade all <标签>` 会先跑迁移）；做回调冒烟。
4. 同步并启用 Tower 审批 Skill，给它建一个环境预设，含 `TOWER_URL` 与 `TOWER_SENDER_API_KEY`（Agent 侧变量名带 `API_KEY` 才会在回复里打码，与服务端的 `TOWER_SENDER_KEY` 不同）。发送端 Key 进 Agent 环境是接受的风险：Agent 能改审批人、写描述，但没有令牌代理不签名。
5. 同步调用代理的 Skill 新版本，给已安装的机器人重装（预设是安装时快照，不重装拿不到新版本），并装上 Tower 审批 Skill。
6. 配置各机器人的审批人。
7. 端到端验证：
   - 读：直接返回，`cloud_calls` 出现 `risk=read, outcome=ok`。
   - 低风险写（如低风险环境的配置更新、单个 IP 加白）：直接执行，`risk=write_low`。
   - 高风险写（如受保护环境的配置更新）：428 → 提交审批 → 批准 → 回调唤醒原对话 → 带令牌重发成功；`cloud_pending_approvals` 该行为 `executed`。
   - 拒绝一次：对话被唤醒并转述理由，没有写入。
   - 手动跑一次对账，无报错。
8. 撤旧凭证：从环境预设删掉 Skill 旧的 AccessKey 变量，再重装一次，让已安装机器人的快照里也没有；检查机器人「凭据与环境」里没有手工配置的同名键（手工配置优先于预设，不会被重装清掉）。
9. 在 RAM 控制台先**禁用**旧 AccessKey，观察至少一周（「最后使用时间」不再更新、对话里没有鉴权失败），再删除。

## 回滚

- **撤旧凭证之前**：从 `.env` 去掉 `ALIYUN_PROXY_*`（代理即 503），重装上一版 Skill；旧预设还在，行为回到上线前。
- **新镜像出问题**：`./deploy/coreman rollback <旧标签>`。迁移 0065 只加表和列，不需要 downgrade；旧镜像没有代理路由，需同时重装旧版 Skill。
- **已撤旧凭证**：重新启用被禁用的旧 AccessKey（尚未删除时），把旧变量加回预设，重装旧版 Skill；旧 Key 删除后只能新建 AccessKey 回填。
- **Tower 不可用**：读与低风险写不受影响；高风险写在核销时 fail closed（`503 tower_unavailable`），恢复后重新执行。
- **停用 CoreMan 发送端**：在 Tower 里停用该发送端；Agent 提交审批会被拒，代理照常拦截高风险写。

## 个人助理的审批闸门

个人助理（见 `docs/feishu.md`「个人助理应用」）以所有者的飞书身份工作，它的每一次写操作都先经所有者本人在 Tower 批准。这里平台自己既是发送端又是目标系统：

- **目标系统**：在 Tower 登记一个目标系统，key 固定为 `feishu-personal`，登记时**打开免比对**（正文与请求体是私人消息，不送任何模型；这是按目标系统的开关，没有按次的字段）。打开后审批卡片上会显示「未经比对」，这是预期的。没打开时提交可能被 Tower 以 `description_mismatch` 拒绝。把它的 API Key 加进 `TOWER_TARGET_KEYS`：`{"feishu-personal": "…", …}`。云签名代理的四个产品 Key 不受影响；只用个人助理、不用云签名代理的部署可以只配这一个键。
- **给人看的名称与入口（可选）**：`TOWER_DISPLAY_NAME`（如 `Example Tower`）与 `TOWER_WEB_URL`（如 `https://tower.example.com`，只认 http/https）。配了之后，开通卡片与欢迎语里用这个名称并带上链接，代替笼统的「审批系统」；不配则照旧。
- **发送端**：沿用 `TOWER_SENDER_KEY`、`TOWER_CALLBACK_SECRET` 与同一个回调地址。`TOWER_URL`、这两项和上面的目标 Key 任一缺失时，助理的写类工具返回 `approval_service_unavailable`，写操作不会执行。
- **发起人与审批人**：发起人是所有者的登录名（`login_name`，要与 Tower 的用户名一致），审批人不填，由发起人本人确认。所有者没有登录名时返回 `no_login_name`；在 Tower 没有登记或已停用时返回 `tower_account_missing`（Tower 没有供发送端预先查询账号的接口，只能在提交时发现）。Tower 因内容本身拒绝这张单（正文过长、到期时间不合法等其他 4xx）时返回 `approval_rejected`，`detail` 带 Tower 的原代码，提示助理原样重提也不会成功；连不上或 5xx 才是 `approval_service_unavailable`，可以稍后重试。
- **流程**：提交 → 所有者在 Tower 批准或拒绝 → 回调（丢失时由对账补上）→ 批准后平台重新读会话、取令牌并以 `feishu-personal` 的身份核销、按暂存参数原样执行、把结果上报 Tower，并在助理会话里告诉它；拒绝、过期、执行失败同样告诉它。令牌与暂存参数的哈希绑定，库里的参数被改动后核销不过，不会执行。
- **审批单里有什么**：摘要只有渠道和对象（如「私聊 → 某人」）；正文是纯文本，含被回复的原话、将要执行的内容、全部其他参数，以及标明「由助理撰写」的背景与调研。对象里的会话名、对方名字会压成单行，名字里夹带换行也伪造不出审批单的抬头行（摘要与「对象」行都是如此）。带文件时列出文件名、类型、大小和一个供批准前查看的下载链接（与审批单同时失效）。正文与请求体保存在 Tower 里；请求体里文件只以文件名、大小与 SHA-256 出现。
- **数量与期限**：每位所有者同时待批最多 10 张；72 小时无人处理按撤回处理（提交时把同一个到期时刻交给 Tower）。批准后要在 24 小时内执行完（Tower 的取令牌窗口），平台在回调后立即执行。在途的单不再单独私信提醒：平台在助理摘要的「等你批的」里列出（摘要时点与免打扰见 `docs/feishu.md`「少打扰」），配了网页入口 `TOWER_WEB_URL` 时带上它（没配就不带链接，不会用接口地址 `TOWER_URL` 代替）；等不了的由助理标紧急立即告诉所有者。
- **执行中断**：执行进程在调飞书期间被杀时，同一任务重跑会把单收口为失败，不会再执行一次。除此之外 scheduler 每轮还会调用 `recover_stuck`：已批准超过 15 分钟、又没有在途执行任务的单最迟约 15 分钟后记为失败，通过结果回合告诉助理并把结果上报 Tower，绝不会第二次执行；已记下「开始执行」的是 `execution_interrupted`（平台不知道操作是否生效，助理应去核对），没有记下的是 `execution_not_started`（什么都没执行，可以重新提交）。scheduler 这一轮的返回里因此多一项 `assistant_approvals_recovered`。飞书调用做完之后即使提交数据库失败，这张单仍会在同一次执行里被收口，不会悬在已批准。
- **数据**：审批流水在 `assistant_approvals`，参数、审批人的话、执行结果用所有者的数据密钥加密。日志关键字：`assistant_approval_submit_failed`、`assistant_approval_executed`、`assistant_approval_report_failed`、`assistant_approval_withdraw_failed`。
- **带文件的写操作（可选）**：发文件、存入云空间、带附件的邮件要等所有者批准，文件在此期间放在一个单独的临时桶里（`ASSISTANT_TMP_S3_*`，见 `docs/object-storage.md`「个人助理的临时桶」）。没配临时桶时这类操作返回 `attachments_not_supported`，其余写操作不受影响。日志关键字：`assistant_tmp_storage_misconfigured`、`assistant_attachment_put_failed`、`assistant_attachment_get_failed`、`assistant_attachment_delete_failed`。
- **调度公共 AI 员工（`assistant-dispatch`）**：助理让别的 AI 员工改东西（`assistant_ask_bot` 且 `allow_write=true`）时，同样先由所有者本人批准，不论发起它的那一轮所有者在不在场。在 Tower 再登记一个目标系统，key 固定为 `assistant-dispatch`，登记时**打开免比对**（正文是问题原文，不送任何模型），把它的 API Key 加进 `TOWER_TARGET_KEYS`：`{"feishu-personal": "…", "assistant-dispatch": "…"}`。没登记这个键时，只读调度不受影响，要改动的调度返回 `approval_service_unavailable`。审批单摘要是「调度员工 → 员工名」，正文是对象、动作与问题原文（逐行加竖线），外加一句说明：对方以所有者的身份执行、平台无法限定它具体改什么。提交时不占调度的名额，也不写调度的审计。批准后平台以 `assistant-dispatch` 的身份核销令牌、重新核对资格并派出（派出的那一行沿用审批单的编号，执行任务重跑不会派第二次），上报 `executed`；派出之后不单独通知助理，答复由求助的结果回合带回。被拒、过期、核销失败、派出时资格已变或名额已满，都按失败通知助理，需要时重新提交。这类单不能改稿（`approval_revise` 返回 `invalid_tool_or_arguments`），撤回后重提；撤回与过期通知 Tower 时沿用 `feishu-personal` 那份配置（只用发送端的 Key），所以两个目标系统要一起登记。发起人、数量与期限、对账与其余个人助理审批单相同。
- **无人在场时的代理写操作（沿用 `feishu-personal`，不用另行登记）**：个人助理在所有者不在场、又能提交审批的一轮（交回、结论投回、审批结果回合；子任务与冷启动不行）里，经业务系统目录代理（`systems_call`）或云签名代理发出的写调用不当场执行，由平台暂存完整请求（系统与操作、方法与地址、全部参数与请求体；或云产品与全部参数）并以所有者为发起人提交审批单，工具名分别是 `systems_call`、`cloud_call`。只有所有者在场时这次调用本会被放行的才提：目录代理只提 `write`，且要求员工对该系统的授权开了「允许写入」（所有者在场时这类调用走确认卡，见 `docs/business-system-catalog.md`「写操作确认卡」；无人在场没人点卡，所以改走这里），`destructive` 在无人在场时仍拒绝、不提，`financial` 永不代理；云签名代理只提判为写、风险评估为低风险的（评估只做读调用），高风险的照旧拒绝。摘要是「业务系统 · 系统名 · 操作名」或「云服务 · 产品 · Action」；正文是渠道、对象、动作、请求与将要执行的全部内容（逐行加竖线，一个字都不截：连同最少的发起背景仍超过审批单正文上限 10000 字时整张不提交，返回 `too_large_to_review`，不留审批行、不发上游请求，助理要把事写进给所有者的结论或拆成能完整展示的小请求；展示的值里控制字符、格式字符（如 U+202E、零宽字符）与 `\n` 以外的行、段分隔符（U+2028、U+2029、U+0085、`\r` 等）都转义成可见的 `\uXXXX`，云参数的值一律按 JSON 字符串展示，一个值造不出独立的一行，也颠倒不了显示；转义不删字，看到的就是要执行的），加上平台按任务查到的发起背景（是哪一类无人在场的轮次；交回轮次列出前 5 件事项的会话名与摘录，压成单行、加竖线），最后一句说明这是助理在所有者不在场时发起的。批准后执行任务先复查（助理启用中、所有者没换且账号可用、授权还在且仍允许写入、系统仍启用且由平台代理；任何一项不满足就记失败、不核销令牌），再以 `feishu-personal` 的身份核销，然后交回各自的代理走与在场调用相同的路径（同样的审计、令牌签发记录、风险与策略检查）以所有者身份执行暂存的那一份：目录代理用 `prepare` 重跑全部检查，拼出的请求与批准的不一致（目录或系统地址变了、风险不再是 `write`）时不发出，一致才发出，幂等请求头的值取审批单号（重跑也是同一个键），审计结局带前缀 `approved_`，大结果截断、不转存；云代理重新评估变成高风险时不发出。只执行一次（开始执行前记下时刻，进程中途被杀不再重发）。结果（截断到 8000 字）作为一轮投回助理会话，响应正文在 `<execution_result>` 围栏里、注明是数据不是指令；上游返回非 2xx 记为失败（`upstream_http_error`）。同一份请求已有在途的单时复用那一张；这两类单每位助理同时待批最多 5 张（超出返回 `too_many_pending`），各类共用的 10 张照旧，所有者在场时自己的审批总留有位置；72 小时过期、撤回、提醒与对账与其余个人助理审批单相同。这类单不能改稿（`approval_revise` 返回 `invalid_tool_or_arguments`）。没有会话锚点，所以不受「会话有了新消息就自动撤回」的检查。

- **所有者在场时的云服务低风险写（确认卡，迁移 `0106`）**：个人助理在所有者本人于私聊里打字发来的一轮中，经云签名代理发出、风险评估为低风险的写不签名、不转发：代理把完整请求（云产品与全部参数，含 Action）暂存进本轮的批次（`interaction_states`，`kind = cloud_write`，按任务号一批；`state` 已登记在个人助理的加密台账里，落库时整块用所有者的数据密钥加密，只有 `user_id` 留明文），返回 `202 {"status": "pending_confirmation", "pending": n, "hint": …}`，审计记 `write_low` / `proposed` / 202，`request_hash` 是用所有者数据密钥算的请求摘要。同一轮里同一份请求重复调用不重复暂存（返回 `duplicate: true`），每批最多 20 个（超出 `403 confirmation_batch_full`）。本轮正常结束时在终稿之后给所有者发一张飞书确认卡（与业务系统目录代理的确认卡同一套机制，见 `docs/business-system-catalog.md`「写操作确认卡」），列出每一项的云产品、Action 与**全部参数**：展示规则与无人在场的审批单相同（每个值按 JSON 字符串展示，控制字符、格式字符与 `\n` 以外的行、段分隔符转义成可见的 `\uXXXX`，一个值造不出独立的一行），一个字都不截；整批放不进一张卡（正文超过 12000 字，或整张卡序列化后超过 24000 字节，比飞书卡片约 30 KB 的上限留出余量）时这一项不暂存，返回 `403 too_large_to_review`。本轮失败、超时或被停止时整批作废。卡片 10 分钟内有效，只有所有者本人在那个私聊里点才算（点击人要同时是暂存时记下的人、卡片发去的私聊里经平台核验的发言人、助理记录上的所有者），别人点无效。点确认先把这张卡记为已提交（重复点击、并发点击、任务重试都不会执行第二次），再复查：助理启用中且状态 active、点击人仍是所有者且账号可用、云签名代理配置齐全（`cloud_proxy_ready`）；然后逐项重新判类别与风险，任一项变成高风险（`risk_changed`，审计 `write_high` / `confirmed_denied`）或不再允许，整批都不执行；都通过才以所有者身份按顺序原样执行暂存的那一份（`cloud_proxy.service.execute_confirmed`，与无人在场批准后执行共用 `_StagedCall`：出站前先落审计行，结局 `confirmed_ok` / `confirmed_http_error` / `confirmed_unreachable`，`approval_id` 为空、`request_hash` 与暂存那一行一致——无人在场批准后执行的结局不带前缀、记 Tower 的审批编号，两者分得开），遇到失败即停。卡片更新为逐项结果；原会话续接一轮（任务载荷 `cloud_write_result`），带上逐项状态与截断到 1500 字的结果，阿里云返回的正文在 `<execution_result>` 围栏里、注明是数据不是指令。取消同样续接，告诉助理不要重试；过期不续接。不是所有者本人打字发来的一轮（卡片触发、插话、协作等）没人能当场点卡，返回 `403 confirmation_unavailable`，什么都不暂存。高风险写照旧 `428` 走按产品登记的 Tower 审批；读、无人在场的各条路径与普通 AI 员工都不变（普通员工的低风险写仍直接签名转发）。不用另行登记 Tower，也不需要新的飞书权限：确认卡与点击回调沿用个人助理应用已有的卡片回调。

### 个人助理写操作确认方式

| 写操作 | 所有者在场（本人在私聊里发起的一轮） | 所有者不在场（交回、结论投回、审批结果回合） |
|---|---|---|
| 飞书个人工具的写（发消息、回复、文档、日程、审批决定等） | Tower 审批（`feishu-personal`） | Tower 审批（`feishu-personal`） |
| 业务系统目录代理（`systems_call`）的 `write` | 飞书确认卡 | Tower 审批（`systems_call` 单） |
| 云签名代理的低风险写 | 飞书确认卡 | Tower 审批（`cloud_call` 单） |
| 云签名代理的高风险写 | `428`，按产品登记的 Tower 审批，批准后带令牌重发 | 拒绝（`owner_turn_required`） |
| 业务系统目录代理的 `destructive` | 飞书确认卡（标「不可恢复」） | 拒绝（`owner_turn_required`） |
| 业务系统目录代理的 `financial` | 不代理 | 不代理 |
| 让公共 AI 员工改东西（`assistant_ask_bot`，`allow_write=true`） | Tower 审批（`assistant-dispatch`） | Tower 审批（`assistant-dispatch`） |
| 「处理中」表情（`assistant_mark_processing`） | 免确认 | 免确认 |
| 环境变量里的业务系统令牌（curl 直连）与业务系统 MCP | 平台不经手，只靠提示词约束：非只读操作先向所有者说明，等他在后续消息里明确同意 | 平台不经手，只靠提示词约束：只能查询，硬性禁止写 |

子任务与冷启动那一轮也算所有者不在场，但不能提交审批：经平台代理的写一律拒绝（`owner_turn_required`）。最后一行平台拦不住：令牌没有只读范围，业务系统收到的写请求与所有者本人发起的分不开，这是已知的残余风险（见 `docs/superpowers/plans/2026-11-13-personal-assistant-phase2-3.md` 裁定 29）。

## 已知限制

- 任务已结束时代理返回 `403 task_not_active`；未知产品返回 `403 product_not_allowed`，未知动作返回 `403 action_not_allowed`。
- A 记录指向回环、链路本地、`0.0.0.0` 等非公网地址按「私网」判为可信目标，与可信 IP 名单无关。
- 唤醒消息与 Skill 预览里的风险提示由本地估算，最终是否审批以代理判定为准。
- Nacos 修改历史里的操作者只显示代理 RAM 用户。
