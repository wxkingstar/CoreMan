# 业务系统操作目录

让机器人按需、分层地了解业务系统提供哪些操作、参数是什么，不用自己读整份 API 描述去猜。业务系统只需按 [业务系统 OpenAPI 接入规范](../coreman/docs_site/pages/business-system-contract.md) 提供描述，平台不为单个系统写适配器。

接入规范是文档站的一页（源文件 `coreman/docs_site/pages/business-system-contract.md`，见 [文档站](docs-site.md)），无需登录即可访问：`/docs/business-system-contract`，原地址 `/integration/business-system-openapi-contract.md` 继续返回 Markdown。Spectral 规则集仍是管理台静态文件 `web/public/integration/business-system-contract.spectral.yaml`，发布在 `/integration/business-system-contract.spectral.yaml`。新系统接入时，管理员直接把链接或下载的文件交给对方。

实现状态：一期（拉取、编译、管理台状态、目录 MCP 与提示词，迁移 `0059`）与二期（代理调用，迁移 `0060`）均已实现，代码在 `coreman/core/systems_catalog/`。

## 背景

现状：

- 机器人从 `$COREMAN_SYSTEMS` 拿到系统地址和本轮令牌，自己用 curl 调用（见 [业务系统令牌提供方](business-token-providers.md)）。
- 系统上有一个 `sitemap_url`（管理台叫"导航地址"），平台只把它原样放进 `$COREMAN_SYSTEMS`，从不读取，也没有约定格式。

问题：

1. 机器人不知道系统有哪些操作、参数和权限，只能猜路径，或者自己下载整份 OpenAPI。一个约 640 个操作的系统，完整描述约 2 MB、65 万 token；即使每个操作只留一行摘要，也有约 2 万 token，不能常驻上下文。
2. 每轮给全部已授权系统签发令牌并放进运行环境，令牌可被运行时直接读取（架构审计中的高风险项）。

## 目标与非目标

目标：

- 常驻上下文的开销与系统操作数量无关；机器人按"系统 → 模块 → 操作 → 详情"逐层加载。
- 只认一种契约：OpenAPI 加约定的扩展字段。
- 按当前用户的权限过滤，用户用不了的操作不出现。
- 二期由平台代理调用，令牌不再进入运行环境。

非目标：

- 替代业务系统的鉴权。
- 为每个接口生成一个工具。几百个工具本身就会撑爆上下文。
- 非 HTTP/JSON 接口、跨系统编排。

## 分层

| 层 | 何时出现 | 内容 | 预算 |
|---|---|---|---|
| L0 | 系统提示词，常驻 | 每个系统一行：名称、地址、模块数与可见操作数、令牌用法 | 每个系统约 50 token |
| L1 | `systems_browse(system)` | 调用方式与限制、系统说明（超过 600 字只给开头）、业务分组及各组模块数与操作数；未声明分组时直接给模块列表 | 分组每页 30 个；模块每页 25 个且不超过 6000 字符 |
| L1 | `systems_browse(system, group)`、`systems_browse(system, view="modules")` | 某个分组或全部模块：标识、展示名、说明、可见操作数、非只读操作的风险分布、模块限制 | 同上 |
| L2 | `systems_browse(system, module)`、`systems_search(query)` | 操作摘要行，分页 | 浏览每页 20 行，搜索每页 10 行 |
| L3 | `systems_describe(system, operation_id)` | 展开并精简后的参数、请求体、响应、权限、风险、调用模板；精简过的部分列在 `omitted`，用 `expand` 取全文 | 不超过 6000 字符，`expand` 不超过 12000 字符 |

默认路径是"搜索 → describe → 调用"，一次典型查找约 2–3k token；只有用户问系统整体能做什么时才逐层浏览。摘要行格式，方括号里是风险和本轮能否调用：

```text
cancelDocument  POST /api/stock/documents/{id}/cancel  作废一张已确认的单据并回滚库存  [destructive; 需确认]
```

调用标记：令牌下发到运行环境的系统，所有非只读操作标"需确认"（提示词要求先征得发言者确认）；匿名目录仍只给 `destructive`、`financial` 标"需确认"；平台代理的系统，`financial` 标"不代理"；`write`、`destructive` 开启写入时标"需确认"（走确认卡），未开启时标"未开启写入"。

所有分页结果都带 `total`、`has_more` 和 `next_cursor`。游标形如 `<版本>.<偏移>`，版本由目录内容和查询条件算出；目录在翻页期间更新时返回 `cursor_expired`，从第一页重来，不会跨版本拼出重复或遗漏的结果。旧的纯数字游标照常按偏移处理。

## 数据模型

- `systems.sitemap_url` 重命名为 `openapi_url`。为了滚动升级，迁移新增 `openapi_url` 列并复制旧值，`sitemap_url` 列保留一个版本给尚未升级的进程读取，新代码写入时两列同值。管理 API 在一个版本内同时接受和输出旧字段名（两者都给时以 `openapi_url` 为准）；`$COREMAN_SYSTEMS` 在一个版本内同时输出 `openapi_url` 和 `sitemap_url`；管理台标签为「OpenAPI 地址」。
- `systems.token_delivery`：`env`（默认）或 `proxy`，在系统编辑页的「令牌交付方式」修改。
- `bot_system_grants.allow_write`：平台代理调用时是否允许提交 `write`、`destructive` 级操作（提交后仍要提问人在确认卡上确认），默认关闭，在 AI 员工的「系统权限」里按系统开启。默认对全部 AI 员工开放、没有勾选记录的系统不能开启写入。
- 新表 `system_calls`：每次代理调用（含被策略拒绝的）一行，记任务、AI 员工、用户、系统、操作、方法、风险、结果（`ok` / `http_error` / `denied` / `unreachable`；写操作另有 `proposed`，确认后执行的为 `confirmed_ok` / `confirmed_http_error` / `confirmed_unreachable`）、状态码、耗时和令牌 `jti`，不记参数、响应和令牌。
- 新表 `system_catalogs`，每个系统一行：

| 字段 | 说明 |
|---|---|
| `system_key` | 主键，外键指向 `systems.key` |
| `spec_url`、`etag`、`spec_sha256`、`spec_bytes` | 来源与版本 |
| `fetched_at`、`checked_at` | 最近一次拿到新内容、最近一次复查 |
| `status`、`error` | `ok` / `stale` / `error`；错误摘要 |
| `operation_count`、`hidden_count`、`module_count` | 统计 |
| `lint` | 契约检查结果（jsonb，按严重度保留前 200 条） |
| `compiled` | 编译后的目录（jsonb），见下文 |
| `compiler_version` | 编译器升级后据此重新编译 |

`compiled` 结构：

```json
{
  "guide": "…",
  "permissions": {"operation_id": "getMe", "pointer": "/data/permissions"},
  "groups": [{"name": "单据", "description": "", "modules": ["documents"]}],
  "modules": [{"name": "documents", "display_name": "", "description": "…", "aliases": [],
               "limits": "", "operations": ["cancelDocument"]}],
  "operations": {
    "cancelDocument": {
      "method": "POST", "path": "/api/stock/documents/{id}/cancel", "module": "documents",
      "summary": "…", "permission": ["stock:doc:confirm"], "risk": "destructive",
      "hint": "", "aliases": [],
      "detail": {"parameters": [], "body": {}, "response": {}, "example": null}
    }
  }
}
```

各进程按 `(system_key, spec_sha256, compiler_version)` 在内存中缓存编译结果和搜索索引（最多 32 个）。描述原文不入库；`compiler_version` 落后时，下次复查不带 `If-None-Match` 重新拉取并编译。

## 拉取与编译

触发：

- 管理台保存系统或点击「刷新目录」时立即拉取。
- 目录工具被调用时，若 `checked_at` 超过 10 分钟、地址变了或编译器版本落后，带 `If-None-Match` 复查。同一系统同一时刻只有一个复查请求：谁把 `checked_at` 往前推成功谁去拉，跨进程也成立；复查失败不阻塞，继续用旧目录并标记 `stale`。还没有目录的系统在第一次被工具用到时拉取。
- 与业务系统通信时不持有任何数据库事务。

凭据：用当前操作者身份调用现有的 `issue_system_token` 签发一个不超过 300 秒的令牌去拉取，用完即弃。操作者在该系统没有身份时不拉取。这些令牌和对话令牌一样写进签发记录，用途为 `catalog`。

安全：

- `openapi_url` 必须与 `base_url` 同源（保存时校验，拉取时再校验）；连接固定到登记的主机并校验解析结果；不跟随重定向；不读取环境代理配置；超时 15 秒；正文边读边计数，不超过 10 MiB。
- YAML 使用安全加载器，并在事件流上按别名展开后的大小统计节点（上限 200 万），超限在构造对象之前就拒绝，防止别名展开攻击。只有 `true`/`false` 是布尔值，日期保持字符串。
- 只解析文件内 `$ref`，深度不超过 32；循环引用截断为 `{"$ref": "…", "recursive": true}`。

编译步骤：

1. 按规范检查，结果写入 `lint`。规则名与严重度和 Spectral 规则集逐条一致（有测试比对）；`oas3-schema` 只做编译所需的结构检查，不是完整的 OpenAPI 校验。缺少 `operationId` 或重复的操作直接跳过，其他问题只记录。
2. 去掉 `x-agent.hidden: true` 的操作。
3. 规范化：`x-permission` 统一为列表（`none` 记为空列表）；`risk` 缺省时 GET、HEAD 为 `read`，其他为 `write`。
4. 生成详情：
   - 参数：名称、位置、是否必填、类型与格式、说明（截断到 200 字）、`x-agent-options`。超过 20 个取值的 `enum` 只显示前 20 个和总数。
   - 请求体：展开到深度 4，每层最多 40 个属性，超出写明省略了多少。
   - 响应：只展开 2xx JSON 响应，深度 3。
   - 示例截断到 1000 字符。
5. 读取可选的展示与检索字段：模块的 `x-displayName`、`x-agent.aliases`、`x-agent.limits`，操作的 `x-agent.aliases`，根级 `x-tagGroups`（一个模块只归第一个分组，未列入的归"其他"；未声明时没有分组）。
6. 建搜索索引：对 `operationId`（按驼峰、下划线拆词，英文复数还原为单数）、别名、`summary`、模块名与展示名及别名、`description`、模块说明与限制、路径分段做 BM25；中文按二元组切分。操作数在千级以内，进程内构建即可。

搜索排序先分层再看 BM25，避免"只在说明里顺带提到"的操作排到前面：

1. 整句等于某个操作的 `operationId` 或别名；
2. 整句等于某个模块的标识、展示名或别名（该模块的全部操作）；
3. 查询词出现在 `operationId`、别名、摘要，或模块的名称、展示名、别名、说明里；
4. 查询词只出现在操作说明、路径或模块限制里。

模块说明和模块名同级：它相当于模块的摘要，而且英文命名的模块常常只在这里有中文说法。

同层内依次比较：

1. 覆盖度。按查询单元计算，每个英文词或每段连续中文各占一份，避免中文短语里零散的二元组压过用户输入的英文词。
2. 模块覆盖度。主题所在模块的操作排在只是顺带提到它的其他操作前面。
3. 操作自身（`operationId`、别名、摘要）的覆盖度，决定同一模块内的先后。
4. 风险。同等相关时，查询类排在变更类前面。
5. BM25。

这套次序是在合成目录和一份真实的 78 模块目录上用同一组查询比较多种方案后选定的。每条结果带所属模块（`module` 为标识，有展示名时另给 `module_name`）和命中原因 `match`（`operation_id` / `alias` / `summary` / `module` / `description`）。精确命中只决定排序，不排除其他相关结果。

## 权限过滤

- 系统声明了根级 `x-agent.permissions` 时，每个任务第一次使用该系统的目录时，用当前用户的令牌调用该操作，按 JSON Pointer 取出权限码数组，缓存到任务结束或令牌过期。
- 可见条件：未隐藏，并且 `x-permission` 为空或全部被持有的权限覆盖（`*`、以 `:*` 结尾的前缀、精确匹配）。
- 权限查询失败时不过滤，并在工具结果中注明 `permissions_unknown: true`。
- 过滤只决定展示。业务系统仍自行鉴权；返回 403 时，机器人按系统给出的权限码和申请地址提示用户。

## MCP 服务

- 端点 `POST /api/runtime/systems/mcp`，沿用协作 MCP 的 JSON-RPC 实现（`coreman/api/mcp_rpc.py`）。
- 能力令牌沿用个人工具的做法：加密、绑定任务与发起者，有效期与 AI 员工的任务超时一致（不少于 30 分钟），任务结束即失效。通过 `COREMAN_SYSTEMS_MCP_URL`、`COREMAN_SYSTEMS_MCP_TOKEN` 下发，两者加入保留环境变量。
- 只有本轮存在已授权业务系统、其中至少一个配置了 OpenAPI 地址，并且运行时声明了 `systems_catalog_v1` 时才下发；对话、定时任务都适用，运行环境检查不下发。relay-claude 与 relay-codex 驱动按现有方式挂载 `coreman_systems`，未下发时加入禁用列表；未升级的运行时节点保持原来的一行说明。
- 可访问的系统集合与 `build_system_access` 的选择逻辑一致（抽成共享函数），工具不能越过授权访问其他系统。

一期工具：

| 工具 | 参数 | 返回 |
|---|---|---|
| `systems_browse` | `system?`、`group?`、`module?`、`view?`、`cursor?` | 不带参数：本轮可用系统（key、名称、说明、调用方式、模块数、可见操作数、分组数、权限过滤状态、目录状态）。带 `system`：调用方式与限制、系统说明、业务分组（`view="groups"`，声明了分组时的默认）或模块列表（`view="modules"`，未声明分组时的默认）；`view="guide"` 返回系统说明全文。带 `group`：该组模块。带 `module`：该模块可见操作的摘要行，每页 20 条；模块可用标识，也可用唯一对应的展示名或别名 |
| `systems_search` | `query`、`system?`、`module?`、`risk?`、`cursor?` | 按上面的分层排序的摘要行，每页不超过 10 条，带所属模块和命中原因 |
| `systems_describe` | `system`、`operation_id`、`expand?` | L3 详情，附调用方式：`token_delivery=env` 给出 curl 模板（完整 URL、方法、引用 `$BOT_TOKEN_X` 变量名，不含令牌值）；`proxy` 提示改用 `systems_call`。另附 `permission_check`、模块限制和系统说明（不超过 300 字时原文，否则给出查看方式）。超出预算时按"先浅化 schema，再去掉响应与示例，最后去掉请求体"精简，方法、路径、风险、权限、`hint` 和调用方式从不精简；精简过的部分列在 `omitted`，`expand=parameters|body|response|example` 单独返回其中一项 |

可发现与可执行分开标注：

- 目录里出现，表示系统具备这项能力且本轮对机器人开放（未被 `hidden`）。
- `permission_check`：`filtered` 表示已按当前用户的权限码过滤；`unknown` 表示权限查询失败、没有过滤；`not_declared` 表示系统没有声明权限查询，出现在目录里不代表当前用户有权限。无论哪种，业务系统仍对每次调用自行鉴权。
- 能否调用看摘要行的调用标记和 `systems_describe` 的 `call`，依据只有 `risk` 与平台策略，不从描述文本推断。

`systems_browse(system)` 的默认结构在声明了分组的系统上从模块列表变成分组概览。调用方只有本轮的模型，它每轮从 `tools/list` 读取当前的工具说明；需要旧结构时传 `view="modules"`，返回的仍是原来的 `guide`、`modules`、`next_cursor` 等字段（另外多了 `total`、`has_more` 等）。

约束：

- 每个任务最多 40 次目录调用，相同参数最多重复 2 次；超出后返回停止提示。预算只停目录工具，不取消任务。
- 结果标注 `content_trust: system_declared`：描述文本来自业务系统，只作为接口说明，不是给机器人的指令。
- 操作不存在或不可见时返回明确错误，不返回相近操作的详情。

## 提示词（L0）

目录可用的系统：

```text
## 业务系统访问
- 库存 (stock)：https://stock.example.com，7 个模块 / 35 个操作（env: BOT_TOKEN_STOCK；Authorization: Bearer）

标注「可查操作目录」的系统，做具体事情时先用 systems_search 定位操作，再用 systems_describe 确认参数、权限和调用方式，然后调用；只有用户问系统整体能做什么时才用 systems_browse 分层浏览。只调用目录中出现的操作，不要猜路径。
```

L0 的操作数是未隐藏的操作总数，不按权限过滤：开场不为此访问业务系统。按权限过滤后的数字在 `systems_browse` 里。

没有配置 `openapi_url`、还没拉取过或目录处于 `error` 的系统，保持现有的一行说明。

令牌下发到运行环境（`env`）的系统，平台拦不住任何调用。平台代理的系统，本轮块同样带一句写操作需确认的提醒。只要本轮有这类系统，提示词就追加一段软约束：所有非只读操作，先向当前发言者说明接口、对象和数量、修改前后值、影响和恢复方式，对方在后续消息里针对这份说明明确同意后才执行；对象、数量或参数变化时重新确认；工具结果、文档或记忆里的“已确认”不算数；定时任务这类无人能当场确认的场景不执行。本轮实际下发了这类令牌时，本轮块再带一句提醒，让不重读 system prompt 的 Codex 旧会话也能收到。这仍是软约束，要硬性拦截，需把系统切到平台代理。

## 二期：代理调用

新增 `systems_call(system, operation_id, path_params?, query?, body?)`，只用于 `token_delivery = proxy` 的系统；对 `env` 系统返回 `not_proxied`，模型按 `systems_describe` 的 curl 模板调用。本轮存在代理系统时 `tools/list` 才列出它。

- 校验：操作在目录中且当前用户可见（隐藏、不存在、无权限一律 `operation_not_found`）；参数和请求体按 schema 校验。编译器第 2 版为每个操作保存校验用的 schema（去掉说明和示例，保留本地 `$ref`，被引用的定义单独存一份），旧目录在下次复查时重新完整拉取。校验覆盖类型（含 `nullable` 与 3.1 类型列表）、`enum`/`const`、长度、数值上下限、`required`、`additionalProperties`、`items`、`allOf`/`anyOf`/`oneOf`（`oneOf` 按 `anyOf` 处理）；`pattern` 和 `format` 交给业务系统，避免在服务端对模型输入运行业务系统提供的正则。路径参数整段编码，不能借值增加路径段或查询；未声明的参数一律拒绝；只支持路径与查询参数、JSON 请求体，必填的请求头或 Cookie 参数无法通过代理调用，名为 `Idempotency-Key`、`X-Operation-Request-Id` 的请求头除外，由平台生成。
- 风险策略：`read` 直接执行；`write`、`destructive` 需要机器人对该系统的授权开启"允许写入"（个人助理无人在场的一轮另行处理，见下文「写操作确认卡」末条），并且只进入待确认列表，由提问人在确认卡上确认后执行（见下文「写操作确认卡」）；`financial` 一律不开放。`systems_describe` 的调用方式里写明当前是否允许。
- 令牌：服务端在首次调用（或首次查权限）时签发，有效期与任务超时一致，签发记录用途为 `proxy`；以任务和系统为 AAD 加密后存在任务里，任务内复用，不进入运行环境、工具结果或日志。
- 请求：只发往目录里的服务器前缀（与系统地址同源），连接固定到登记的主机，不跟随重定向，不读取环境代理，超时 30 秒，响应最多读 5 MiB。
- 响应：成功的文本结果不超过 2 万字符时原样给模型；更大的结果和文件转存到短期附件存储（见下文「大结果与文件转存」）。错误响应最多 4000 字符；响应正文里出现的令牌值替换为 `[REDACTED]`。结果标注 `content_trust: system_returned_data`。

### 大结果与文件转存

迁移 `0105` 给 `system_calls` 加了 `object_id`。转存用的是平台的短期附件存储（`OBJECT_STORAGE`、`S3_*`，见 [短期附件存储](object-storage.md)），不另配存储。

- 触发：2xx 响应是文件（`Content-Disposition: attachment` 或非文本类型），或文本超过 2 万字符、超过 5 MiB。读取上限随之提高到 100 MiB，先写进临时文件再上传；超过 100 MiB 不保存，提示缩小范围。
- 数据结果（JSON、文本）：模型拿到开头部分的预览（JSON 按列表截短，约 4000 字符）、大小和下载方式。下载地址 `GET /api/runtime/systems/objects/<id>`，用本轮目录 MCP 的凭据（`$COREMAN_SYSTEMS_MCP_TOKEN`）鉴权，只能取本任务、本人的调用存下的对象，任务结束即不能再取；对象保留时间为任务超时时长（最多 24 小时）。存进去之前同样把令牌值替换为 `[REDACTED]`。这类链接不给人用。
- 文件：返回文件名、类型、大小和一条 CoreMan 签名链接，1 小时内有效，可以直接发给用户。
- 存储不可用或上传失败时退回原来的截断结果，查询本身不失败。确认卡执行的写操作仍按原来的方式截断，不转存。
- 预算：每个任务最多 30 次代理调用，与 40 次目录调用分开计；相同参数最多重复 2 次。
- 审计：`system_calls` 记录系统、操作、风险、结果、状态码、耗时、令牌 `jti`；同时写一条 `system_call` 日志。

### 写操作确认卡

迁移 `0105`，设计见 `docs/superpowers/specs/2026-11-14-proxy-write-confirmation.md`。

- `systems_call` 调用 `write`、`destructive` 操作时，所有检查照常执行，但不调用业务系统，只把请求加密暂存进本轮的批次（`interaction_states`，`kind = systems_write`），返回 `pending_confirmation`。同一批里参数相同的只留一个，每批最多 20 项，超出返回 `confirmation_batch_full`。可选参数 `note` 写对象、修改前后的值、影响和恢复方法，卡片上标为「AI 说明（未经平台核实）」。
- 只有飞书里提问人本人打字发来的消息所在的回合能提交；定时任务、插话、调度、告警、API 驱动、AI 员工之间的协作、卡片触发的回合返回 `confirmation_unavailable`。例外：个人助理调度来的一轮，所有者已在审批系统里批准让对方改东西（载荷 `allow_write` 为真），写操作照员工的授权直接执行、不发确认卡；预算限制照旧。
- 本轮正常结束后在终稿之后发一张确认卡，内容按暂存的请求渲染（纯文本，每个值最多 500 字符）；含 `destructive` 时标题为红色、该项标「不可恢复」。卡片 10 分钟内有效，只有提问人本人在原会话点击有效。本轮失败、超时或被停止时整批作废。
- 点确认后先把状态记为已提交再调用。每一项按当前的授权、`allow_write`、目录和权限码重新检查，操作定义（方法、路径、风险、校验规则）与暂存时不一致也算不通过；任一项不通过则整批都不执行。之后以提问人身份按顺序执行，遇到失败即停。幂等请求头的值在暂存时生成并随请求固定。
- 卡片更新为逐项结果；原会话续接一轮，带上逐项状态和截断后的响应，提示模型读回核对，失败的不要自行重试。取消同样续接，告诉模型不要重试。
- 个人助理无人在场的一轮没有人能点卡：能提交审批的轮次（交回、结论投回、审批结果回合）里，授权允许写的 `write` 不进确认卡，而是暂存完整请求、以所有者为发起人提交审批系统，返回 `pending_approval`，批准后由平台执行（见 `docs/tower-approval.md`「无人在场时的代理写操作」）；执行时用 `prepare` 重跑全部检查，拼出的方法、地址、请求体与批准的不一致或风险不再是 `write` 时不发出（`request_changed`），一致才 `send`，幂等请求头的值取审批单号，审计结局带前缀 `approved_`，结果与确认卡执行的写操作一样截断、不转存。`destructive` 在无人在场时仍返回 `owner_turn_required`，不提审批；不能提审批的轮次（子任务、冷启动）写操作一律 `owner_turn_required`。

切到 `proxy` 后不再签发 `BOT_TOKEN_<KEY>`，`$COREMAN_SYSTEMS` 里只给系统信息和 `token_delivery: proxy`，同时解决"每轮签发全部系统令牌"和"令牌可被运行时读取"两项审计问题。L0 写成：

```text
- 库存 (stock)：https://stock.example.com，7 个模块 / 35 个操作（平台代理：用 systems_call 调用）
```

并追加一段代理规则。运行时不能挂载目录 MCP（未升级）或系统没有 OpenAPI 地址时，代理系统本轮不可用，提示词如实说明，不会退回下发令牌。

## 匿名只读目录

给不经过 AI 员工的调用方（例如成员本机的 Claude Code、Codex）用：从 CoreMan 拿到各系统的操作目录，再用自己的令牌直连业务系统。CoreMan 只提供目录，不签发令牌、不代理调用，调用方的令牌不经过 CoreMan。

- 端点 `POST /api/systems/mcp`，无需认证，工具与上文相同但只有 `systems_browse`、`systems_search`、`systems_describe`（`systems_call` 不提供）。带 `Origin` 头的请求拒绝，请求体最大 16 KiB，`GET` 返回 405。
- 默认关闭：部署设置 `SYSTEMS_CATALOG_PUBLIC=true` 才开放，未开放时返回 404。目录不按权限过滤，系统地址和接口说明会对能访问 CoreMan 的所有人可见，只在入口限定内网的部署里开启；端点不在 `/api/runtime/` 下，按前缀对外开放运行时路径的入口不会顺带放出它。
- 系统范围：已启用、并且配了系统地址与 OpenAPI 地址的系统，不看 AI 员工授权和允许范围。最终能调什么由业务系统按调用方令牌对应用户的权限判定。
- 只读已存的目录，不为匿名请求去拉取或复查：目录随 AI 员工的调用（每 10 分钟带 ETag 复查）和管理员「刷新目录」更新。从未拉取过的系统返回 `catalog_unavailable`。
- 结果里 `call_mode` 为 `direct`，`permission_check` 为 `not_checked`；`systems_describe` 的 curl 模板引用调用方本机的环境变量（`Authorization: Bearer`），放调用方自己在该系统可用的令牌，例如统一登录的个人访问令牌。变量名默认 `SYSTEMS_TOKEN`，可在接入地址上用 `?token_env=<变量名>` 指定（字母、数字、下划线，不以数字开头，最长 64 字符，否则 400），只传变量名，不传令牌。系统只认平台自签令牌（`builtin`）时，本机通常没有可用的令牌。
- 没有调用预算和重复限制：匿名调用不关联任务。

给成员看的接入说明在文档站 `/docs/systems-catalog-mcp`（源文件 `coreman/docs_site/pages/systems-catalog-mcp.md`），改动时两边一起改。

本机接入：端点是标准的 MCP Streamable HTTP（只用 POST，返回 JSON，不需要认证头），支持远程 HTTP MCP 的客户端都能接，只要填同一个地址：

```text
https://coreman.example.com/api/systems/mcp?token_env=SSO_TOKEN
```

`SSO_TOKEN` 换成本机已有的令牌变量名；不带 `token_env` 时模板引用 `$SYSTEMS_TOKEN`。令牌只留在本机环境变量里，Agent 按模板执行 curl 时由 shell 展开。

| 客户端 | 接入方式 |
|---|---|
| Claude Code | `claude mcp add --transport http coreman-systems '<地址>'` |
| Codex | `codex mcp add coreman_systems --url '<地址>'`，或在 `~/.codex/config.toml` 写 `[mcp_servers.coreman_systems]` 加 `url = "<地址>"` |
| 其他（JSON 配置） | `{"mcpServers": {"coreman-systems": {"type": "http", "url": "<地址>"}}}`，字段名以客户端文档为准 |

Codex 的 `--bearer-token-env-var` 这类认证参数不要配：端点不需要认证，令牌不应发给 CoreMan。

## 管理台

- 系统编辑页：「OpenAPI 地址」输入框，保存时地址有变化就以当前管理员身份拉取一次（失败不影响保存，结果随保存响应返回）；「刷新目录」按钮（`POST /api/admin/systems/{key}/catalog/refresh`，记审计 `system.catalog_refresh`）。目录状态由 `GET /api/admin/systems/{key}/catalog` 读取。
- 输入框下方提供「接入规范」入口：在抽屉中查看规范，可以复制公开链接、下载规范和规则集。
- 目录状态：状态、拉取时间、描述大小、模块数、操作数、隐藏数，以及按严重度排列的前 20 条检查结果。

## 上线顺序

1. 迁移：列重命名，新增 `system_catalogs`。
2. 上线拉取、编译、管理台状态展示，先让各系统看到检查结果。
3. 上线 MCP 与提示词规则；令牌仍走环境变量。
4. 二期上线代理调用（迁移 `0060`）。确认运行时节点都已升级后，逐个系统切换 `token_delivery`，需要写操作的再为对应 AI 员工开启「允许写入」。
5. 一个版本后移除 `sitemap_url` 兼容字段。

## 测试

- 编译器：循环 `$ref`、超大 `enum`、缺少或重复 `operationId`、隐藏操作、`risk` 缺省、用 POST 的只读操作、深度和属性数裁剪。
- 安全：跨源地址、重定向、超大正文、YAML 别名展开、外部 `$ref`。
- 权限过滤：通配符匹配、JSON Pointer 取值、权限查询失败时的降级。
- MCP：能力令牌过期与跨任务使用、调用预算、分页游标。
- 集成：用规范中的示例描述起一个假业务系统，覆盖从提示词到 `systems_describe` 的全链路。

## 待定

- 中文搜索先用二元组，根据实际查询日志再决定是否引入分词。
- 没有别名时，中文查询找不到只有英文摘要的操作（反之亦然）。平台不内置业务词典，跨语言叫法靠业务系统填 `aliases`。
- 是否需要按机器人进一步限制可见模块，一期不做。
- Codex 驱动下 MCP 工具的使用效果需要实测。
