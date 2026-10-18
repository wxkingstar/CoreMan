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
   - 低风险：直接签名转发。
   - 高风险：返回 `428 approval_required`，带 `target`、`caller`、`requester`、`external_id`、`url`、`risk_reasons`。Agent 用 Tower 审批 Skill 提交审批；批准后带 `X-Tower-Approval: <令牌>` 原样重发同一份请求体，代理在 Tower 核销成功后才签名转发。
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

## 已知限制

- 任务已结束时代理返回 `403 task_not_active`；未知产品返回 `403 product_not_allowed`，未知动作返回 `403 action_not_allowed`。
- A 记录指向回环、链路本地、`0.0.0.0` 等非公网地址按「私网」判为可信目标，与可信 IP 名单无关。
- 唤醒消息与 Skill 预览里的风险提示由本地估算，最终是否审批以代理判定为准。
- Nacos 修改历史里的操作者只显示代理 RAM 用户。
