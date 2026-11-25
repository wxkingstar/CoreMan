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

调用标记：令牌下发到运行环境的系统，所有非只读操作标"需确认"（提示词要求先征得发言者确认）；匿名目录仍只给 `destructive`、`financial` 标"需确认"；平台代理的系统，`write`、`destructive`、`financial` 开启写入时标"需确认"（走确认卡），未开启时标"未开启写入"。

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

- 每个任务最多 2000 次目录调用，相同参数最多重复 2 次；超出后返回停止提示。预算只停目录工具，不取消任务。
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

没人点名的轮次（群聊旁听的插话回合与调度回合）由没人确认过的群消息触发，默认不签发 `env`、`mcp` 系统的令牌，也不写签发记录，`mcp` 系统由驱动显式禁用；插话回合的本轮块列出这些系统并说明需要时请对方 @ 它再问。`proxy` 系统不受影响（这类轮次里它本来就只能读）。员工的 `unprompted_system_tokens`（迁移 `0113`，默认关闭）打开后照常下发，在编辑页的群聊旁听设置或 `coreman bots observe-set --unprompted-tokens on` 修改。告警触发的轮次不在其内：它以管理员配置的服务用户身份运行，又不挂操作目录，照常签发。定时任务、API 任务、AI 员工互助和个人助理的轮次不变。

## 二期：代理调用

新增 `systems_call(system, operation_id, path_params?, query?, headers?, body?)`，只用于 `token_delivery = proxy` 的系统；对 `env` 系统返回 `not_proxied`，模型按 `systems_describe` 的 curl 模板调用。本轮存在代理系统时 `tools/list` 才列出它。

- 校验：操作在目录中且当前用户可见（隐藏、不存在、无权限一律 `operation_not_found`）；参数和请求体按 schema 校验。编译器第 2 版为每个操作保存校验用的 schema（去掉说明和示例，保留本地 `$ref`，被引用的定义单独存一份），旧目录在下次复查时重新完整拉取。校验覆盖类型（含 `nullable` 与 3.1 类型列表）、`enum`/`const`、长度、数值上下限、`required`、`additionalProperties`、`items`、`allOf`/`anyOf`/`oneOf`（`oneOf` 按 `anyOf` 处理）；`pattern` 和 `format` 交给业务系统，避免在服务端对模型输入运行业务系统提供的正则。路径参数整段编码，不能借值增加路径段或查询；未声明的参数一律拒绝；只支持路径、查询与请求头参数、JSON 请求体和带文件的 `multipart/form-data` 表单（见下文「上传文件」）。请求头参数用 `headers` 传（例如 `If-Match`），只认操作声明过的、名字不分大小写，值须是一行、不超过 1000 字符，随写操作一起暂存、在确认卡详情里列出并原样发出；名为 `Idempotency-Key`、`X-Operation-Request-Id` 的由平台生成，`Authorization`、`Cookie`、`Host`、`Accept`、`Content-Type` 等由平台设置，调用方不能传。必填的 Cookie 参数无法通过代理调用。批量提交的各项不带请求头。
- 风险策略：`read` 直接执行；`write`、`destructive`、`financial` 需要机器人对该系统的授权开启"允许写入"（个人助理无人在场的一轮另行处理，见下文「写操作确认卡」末条），并且只进入待确认列表，由提问人在确认卡上确认后执行（见下文「写操作确认卡」）。`financial` 更严：每张确认卡只能有它一项（本轮已有其他待确认写操作、或已有一项 `financial` 时返回 `confirmation_single_only`），不能批量提交（`batch_single_only`），所有者预先批准直写的调度轮次（`confirmation_required`）和无人在场的轮次（`owner_turn_required`）都不执行。`systems_describe` 的调用方式里写明当前是否允许。
- 令牌：服务端在首次调用（或首次查权限）时签发，有效期与任务超时一致，签发记录用途为 `proxy`；以任务和系统为 AAD 加密后存在任务里，任务内复用，不进入运行环境、工具结果或日志。
- 请求：只发往目录里的服务器前缀（与系统地址同源），连接固定到登记的主机，不跟随重定向，不读取环境代理，读操作超时 60 秒、写操作 30 秒，响应最多读 5 MiB。Codex 运行时把目录 MCP 的工具超时设为 90 秒（默认 60 秒会先于平台超时），需要升级节点才生效。连不上或超时返回 `system_unreachable`，带 `maybe_sent`：连接没建起来为假；发出后没等到响应为真，这时写操作的提示改为「可能已经处理，先核对实际结果，不要重试」。
- 响应：成功的文本结果不超过 2 万字符时原样给模型；更大的结果和文件转存到短期附件存储（见下文「大结果与文件转存」）。错误响应最多 4000 字符；响应正文里出现的令牌值替换为 `[REDACTED]`。结果标注 `content_trust: system_returned_data`。

### 大结果与文件转存

迁移 `0105` 给 `system_calls` 加了 `object_id`。转存用的是平台的短期附件存储（`OBJECT_STORAGE`、`S3_*`，见 [短期附件存储](object-storage.md)），不另配存储。

- 触发：2xx 响应是文件（`Content-Disposition: attachment` 或非文本类型），或文本超过 2 万字符、超过 5 MiB。读取上限随之提高到 100 MiB，先写进临时文件再上传；超过 100 MiB 不保存，提示缩小范围。
- 数据结果（JSON、文本）：模型拿到开头部分的预览（JSON 按列表截短，约 4000 字符）、大小和下载方式。下载地址 `GET /api/runtime/systems/objects/<id>`，用本轮目录 MCP 的凭据（`$COREMAN_SYSTEMS_MCP_TOKEN`）鉴权，只能取本任务、本人的调用存下的对象，任务结束即不能再取；对象保留时间为任务超时时长（最多 24 小时）。存进去之前同样把令牌值替换为 `[REDACTED]`。这类链接不给人用。
- 文件：返回文件名、类型、大小和一条 CoreMan 签名链接，1 小时内有效，可以直接发给用户。
- 存储不可用或上传失败时退回原来的截断结果，查询本身不失败。确认卡执行的写操作仍按原来的方式截断，不转存。
- 预算：每个任务对每个系统的代理调用次数按系统设置（`systems.proxy_calls_per_turn`，默认 3000、可设 1～20000），按系统分开计（任务载荷 `proxy_calls_by_system`），与 2000 次目录调用分开计；用完后同一会话里另发一条消息就是新的一轮，重新计数。不限制相同参数重复调用（轮询异步任务进度时参数不变），所以代理调用不记参数指纹，载荷不随调用次数增长。
- 审计：`system_calls` 记录系统、操作、风险、结果、状态码、耗时、令牌 `jti`；同时写一条 `system_call` 日志。

### 上传文件

请求体是 `multipart/form-data` 的操作（上传图片、导入 CSV/xlsx 等）同样可以代理调用。编译器第 4 版把表单里 `type: string` 且 `format: binary`/`base64` 或带 `contentMediaType` 的字段（以及这类字段的数组）记为文件字段，其余字段按 JSON 对象校验，旧目录在下次复查时重新编译。

- 上传：运行环境用本轮目录 MCP 的凭据把文件 `PUT` 到 `/api/runtime/systems/uploads?filename=<文件名>`，拿到 `upload_id`。单个文件不超过 30 MB，保存 1 小时，只有同一轮、同一位发言者能用；类型取请求的 `Content-Type`，没有声明、或是 `curl --data-binary` 默认的表单编码与 `application/octet-stream` 时按文件名判断。`systems_describe` 对这类操作附上传用的 curl 模板和请求体写法。
- 调用：`systems_call` 的 `body` 里文件字段写 `{"upload_id": "..."}`（数组字段写数组），其他字段照常写。平台先确认每个文件都是本轮本人上传、未过期的，再以发言者身份按 `multipart/form-data` 发出：文本字段在前，对象和数组字段转成 JSON 文本，文件带原文件名和类型。
- 写操作照常进确认卡。卡片上不显示 `upload_id`，按字段列出文件名、大小和 sha256 前 12 位；点确认后发出的就是卡片上这几个文件。
- 个人助理无人在场的一轮，带文件的写操作不提交所有者审批，按「所有者不在场」拒绝：审批可能几小时后才批，文件那时已经过期。

### 写操作确认卡

迁移 `0105`，设计见 `docs/superpowers/specs/2026-11-14-proxy-write-confirmation.md`。

- `systems_call` 调用 `write`、`destructive` 操作时，所有检查照常执行，但不调用业务系统，只把请求加密暂存进本轮的批次（`interaction_states`，`kind = systems_write`），返回 `pending_confirmation`。同一批里参数相同的只留一个，每批最多 50 项，超出返回 `confirmation_batch_full`。可选参数 `note` 写对象、修改前后的值、影响和恢复方法，要用业务人员看得懂的说法（字段写业务名称，不写接口字段名）。
- 只有飞书里提问人本人打字发来的消息所在的回合能提交；定时任务、插话、调度、告警、API 驱动、AI 员工之间的协作、卡片触发的回合返回 `confirmation_unavailable`。例外：个人助理调度来的一轮，所有者已在审批系统里批准让对方改东西（载荷 `allow_write` 为真），`write` 照员工的授权直接执行、不发确认卡。所有者批准的是一句任务描述，没有人逐项确认，所以这种轮次里 `destructive` 不执行（`confirmation_required`，请所有者在私聊里让助理去做、走确认卡），`write` 每轮最多 50 个（一张确认卡的量，按本任务的调用记录计数，超出返回 `preapproved_writes_exhausted`）；预算限制照旧。
- 本轮正常结束后在终稿之后发一张确认卡，面向不懂技术的使用者：标题是操作名称（目录里的摘要去掉括号里的技术说明；多项时为「请确认 N 项操作」），副标题是系统名；正文只列 `note` 里的对象和改动（每项最多 3 条，每个值单行、最长 80 字符、过长从中间省略，控制与格式字符去掉），没有 `note` 时列路径参数；接口、路径、请求体、影响和恢复方法放在默认收起的「查看详情（实际发出的请求）」里，按暂存的请求渲染（纯文本，每个值最多 500 字符）；底部注明改动说明由 AI 整理、以详情里的请求为准。含 `destructive` 或 `financial` 时标题为红色、确认按钮为红色，该项分别标「删除后无法恢复」「涉及资金」。卡片 60 分钟内有效，只有提问人本人在原会话点击有效。过期后卡片变成「已过期」并带「重新发起」按钮，只有提问人本人能点，点了用同一份暂存的请求发一张新卡（新的 60 分钟），确认时照样整批复查；暂存的请求保留到第一次发卡后 24 小时，之后清掉、不能再重发，已确认、已取消的不能重发。本轮失败、超时或被停止时整批作废。
- 点确认后先把状态记为已提交再调用。每一项按当前的授权、`allow_write`、目录和权限码重新检查，操作定义（方法、路径、风险、校验规则）与暂存时不一致也算不通过；任一项不通过则整批都不执行。之后以提问人身份按顺序执行，遇到失败即停。幂等请求头的值在暂存时生成并随请求固定。
- 卡片更新为逐项结果（标题「已完成」或「没有全部完成」，每项只写操作名称和「已完成 / 没有成功 / 结果未知 / 没有执行」，不显示接口和状态码）；原会话续接一轮，带上逐项状态和截断后的响应，提示模型读回核对，失败的不要自行重试。取消同样续接，告诉模型不要重试。发出后没等到响应（超时、连接中途断开）或平台处理时出错的项记「结果未知，可能已经生效」而不是失败，提示先核对、不要直接重做。
- 执行中每一项的状态（待执行、正在发送、成功、失败、结果未知）在发出前后各存一次（批次 `state` 的 `run.progress`）。执行任务中途丢失（worker 掉线）时，巡检按存下的状态收尾：卡片列出每一项做到哪一步（正在发送的记结果未知），续接轮把同样的状态交给模型，没有执行的不会再自动执行；被判失联但其实还活着的任务在下一项之前发现批次已经收尾就停下。
- 个人助理无人在场的一轮没有人能点卡：能提交审批的轮次（交回、结论投回、审批结果回合）里，授权允许写的 `write` 不进确认卡，而是暂存完整请求、以所有者为发起人提交审批系统，返回 `pending_approval`，批准后由平台执行（见 `docs/tower-approval.md`「无人在场时的代理写操作」）；执行时用 `prepare` 重跑全部检查，拼出的方法、地址、请求体与批准的不一致或风险不再是 `write` 时不发出（`request_changed`），一致才 `send`，幂等请求头的值取审批单号，审计结局带前缀 `approved_`，结果与确认卡执行的写操作一样截断、不转存。`destructive`、`financial` 在无人在场时仍返回 `owner_turn_required`，不提审批；不能提审批的轮次（子任务、冷启动）写操作一律 `owner_turn_required`。
- 个人助理所有者在场时经云签名代理的低风险写复用这套确认卡机制（另一批，`kind = cloud_write`，迁移 `0106`），见 `docs/tower-approval.md`「所有者在场时的云服务低风险写」。

### 批量写操作

同一个写操作要做很多项时（例如按表格批量改价），模型用 `batch` 一次提交整批，平台拆开逐项执行（迁移 `0112`，设计见 `docs/superpowers/specs/2026-11-16-proxy-batch-writes.md`）。

- 提交：模型把每一项写成 JSON Lines（每行 `{"path_params", "query", "body", "target"?, "before"?}`，`target`、`before` 只用于展示；文件不超过 30 MB（与上传上限相同），超出返回 `batch_too_large`），按上传文件的方式 PUT 到平台，再调用 `systems_call(system, operation_id, batch={"upload_id"}, note?)`；`batch` 与 `path_params`、`query`、`headers`、`body` 互斥，计 1 次代理调用。`systems_describe` 对允许写入的写操作给出 `batch` 用法。
- 限制：只能是 `write`、`destructive`（`financial` 不能批量，返回 `batch_single_only`），每批项数上限按系统设置（`systems.batch_write_max` 默认 1000、可设 1～50000；`batch_destructive_max` 默认 50、可设 1～500；迁移 `0114`，设计见 `docs/superpowers/specs/2026-11-18-proxy-batch-limits.md`），`systems_describe` 的 `batch.max_items` 给出当前上限；带文件的操作不能批量；每轮最多一批（与逐次提交的写操作分开，各发各的卡）；只在能当场点确认卡的回合提交，所有者已批准直写的轮次返回 `confirmation_unavailable`；个人助理不开放（`batch_unavailable`：各项暂存在自己的表里，不在按所有者数据密钥封存的那一层）。
- 校验：文件只按换行切行（U+2028 之类在 JSON 字符串里合法，不当行尾），`NaN`、`Infinity` 不接受；路径与查询参数里的字符串和单次调用一样去掉首尾空白。逐行走与单次调用相同的 `build_request` 和同源检查；发出的请求完全相同的两行算错误。有任何一行不合格整批不暂存，返回 `invalid_batch`，列出前 20 个问题的行号和原因。整批放不进一张确认卡（字段太多或取值太长）时当场返回 `batch_card_too_large`，不等飞书拒收。
- 暂存：批次是 `interaction_states`（`kind = systems_batch`），明文只有来源、提问人、会话、操作（含目录里的服务器前缀）和条数；每项一行 `system_batch_items`，请求按批次和行号加密，暂存时生成幂等键。审计记一行 `batch_proposed`。
- 确认卡：标题「请确认 N 项操作」，正文是总数、按请求体顶层字段（没有请求体时按查询参数）统计的前 3 种取值和项数（最多列 20 个字段，其余只在明细里）、同一对象出现多次的提醒，以及最多 50 项预览。预览的每一行在 AI 写的对象名后面都带上真实的路径参数（请求体存在时连同查询参数），名称和编号对错行时在卡片上就能看出来。预览先按字段轮流取少见的取值（跳过只有一种取值和大半取值都不同的字段），再取对象重复的项，剩下的按任务编号做种子随机补足；卡片超过大小上限时预览条数减半直到放得下。卡片附全部明细的 CSV 下载链接，与卡片同时有效（60 分钟），重新发起时生成新链接；CSV 里以 `= + - @`、制表符、回车开头的单元格前加单引号。明细文件没能生成时不发确认卡：批次作废，并在会话里告诉提问人这一批不会执行；过期后点「重新发起」时生成不了，旧卡保持原样、可以稍后再点。有效期、只有提问人能点、过期重新发起、拒收作废与单次确认卡相同。
- 执行：点确认后先把批次记为已确认并提交，再复查操作（仍可见、风险与操作定义未变、仍允许写入）；复查会提交事务，所以之后的取消、过期、再点确认都不再生效，不通过整批不执行。通过后由后台任务（`systems_batch_run`）以提问人身份按行号串行执行，某一项失败继续执行其余各项：业务系统返回非 2xx 记 `http_error`；连接没建起来（请求肯定没发出）记 `unreachable`；发出后没等到响应（超时、连接中途断开）记 `unknown`，业务系统可能已经处理，不算失败。每段处理 500 项或 60 秒，结束时排下一段。进度卡（文本进度条、已处理数与各类计数、按目前速度估计的剩余时间，带「停止执行」按钮，只有提问人能点，点了之后剩余各项记 `skipped`）在段内和段末按节奏刷新：距上次刷新满 10 秒，并且进度又推进了 1%（至少 1 项）或者已过 60 秒。系统设置了批量间隔（`batch_interval_ms`，0～60000 毫秒）时，每发完一项、还有下一项就先等这么久，用来给业务系统限速；段内剩下的时间不够等时提前结束这一段，下一段延后这么久再开始，不占着 worker 空等；执行中修改间隔从下一段开始生效；等待中不响应停止，设了间隔时点「停止执行」后最多再过一个间隔才停下。取下一项走 `system_batch_items_pending_idx`（只含待执行项的部分索引），不随已处理的项数变慢。每段开头复查一遍：AI 员工或提问人的账号被停用、员工被收回写入权限、系统停用、操作定义或目录里的服务器前缀变化、签不出令牌时停下，剩余各项记 `skipped`。管理台取消执行任务同样让这一批到此为止。发送某一项时平台自己出错（例如请求体无法序列化）记 `unknown`，换一个新任务接着做后面的。
- 不重发、不并行：每项发出前先记 `sending` 和时刻。新的一段开始时，记了 `sending` 却没有结果的项，超过 120 秒才记 `unknown`（不重发）；还在 120 秒内的可能仍在途，这一段推后再来，期间不发别的项。批次记着当前是第几段：执行任务丢失（worker 掉线、处理出错）时巡检另排一段接手，已经处理过的项不会再做（断点续做）；两次补排至少隔 60 秒，中间有进展就重新计数，连续 3 次没有任何进展后不再补，改为收尾：剩余各项记 `skipped`，卡片和续接轮说明「执行任务多次意外中断」，批次不会一直停在「正在执行」。被判失联但其实还活着的那一段在下一项之前发现自己已被接手（或自己的任务已被判失联）就停下，不排下一段也不收尾。收尾时仍是 `sending` 的项记 `unknown`，不会写成没有执行。
- 先试跑：超过 50 项的批次，确认后先执行前 5 项就暂停（确认卡上有说明），卡片列出这几项各自的结果，请提问人到业务系统核对；点「继续执行剩余 N 项」才做其余各项，点「停止」则剩余各项记 `skipped`。
- 连续失败自动暂停：连续 10 项没有成功（`http_error`、`unreachable`、`unknown`，自上次开始或继续之后算起）且还有没执行的项时暂停，卡片给出最近一项的失败摘要和同样的两个按钮，免得业务系统出问题时把整批都打成失败。
- 暂停期间不排执行任务、巡检也不补排；暂停时在会话里发一条提醒（群里 @ 提问人），因为卡片更新本身不会通知任何人。60 分钟内没有选择就自动结束，剩余各项记 `skipped`。继续时只改状态并排执行任务，权限与操作定义照旧由执行任务在每段开头复查。
- 结果：卡片改为汇总（成功、失败、结果未知、未执行各多少，前 10 个没有成功的项；有结果未知的项时提示先到业务系统核对、不要直接重做）并附完整结果 CSV（24 小时有效）；原会话续接一轮，带上计数和前 20 个没有成功的项的摘要，提示模型抽查读回、没有成功的不要自行重试、结果未知的先用读操作核对。结果卡在保留期（24 小时）内带两个只有提问人能点的按钮：有没执行的项时「继续执行剩余 N 项」，有失败的项（`http_error`、`unreachable`）时「重试失败的 N 项」（重试换新的幂等键）；已经成功和结果未知的项不会再做，每次结束各续接一轮。群聊里结束时另发一条 @ 提问人的提醒（私聊里续接轮的回复就是提醒）。执行完的批次保留 24 小时后连同各项清掉。
- 查看：左侧菜单「批量任务」（`/batches`，所有登录用户可见）与「业务系统」页的「批量写操作」tab（管理员）是同一个面板，有进行中的批次时每 5 秒自动刷新，切走或全部结束后停止。管理员（`ai_committee`、`platform_admin`）看全部批次；其他人看自己发起的，以及自己能访问的 AI 员工（创建者、成员，团队负责人看本团队，与对话记录相同）的批次。列表 `GET /api/admin/system-batches` 只有元数据；点「明细」按页（每页 50）看每一项暂存时的写入参数（路径参数、查询参数、请求体、对象名、修改前的值）、状态与 HTTP 状态码、结果摘要（业务系统返回的前约 300 字，不存完整响应）和完成时间，可按成功/失败/结果未知/未执行/待执行筛选（`GET /api/admin/system-batches/{id}/items?status=`，只解密当前页）；发起人以外的人查看明细每页记一笔审计 `system_batch.view_items`。停止（`POST /api/admin/system-batches/{id}/stop`，记审计 `system_batch.stop`）只有管理员和发起人本人能做：执行中的在下一项之前停下，暂停的当场结束；卡片与续接轮写明是管理员停止还是按发起人的要求停止。命令行对应 `coreman systems batches` 与 `coreman systems batch-stop`。批次结束 24 小时后连同各项一起清理。

切到 `proxy` 后不再签发 `BOT_TOKEN_<KEY>`，`$COREMAN_SYSTEMS` 里只给系统信息和 `token_delivery: proxy`，同时解决"每轮签发全部系统令牌"和"令牌可被运行时读取"两项审计问题。L0 写成：

```text
- 库存 (stock)：https://stock.example.com，7 个模块 / 35 个操作（平台代理：用 systems_call 调用）
```

并追加一段代理规则。运行时不能挂载目录 MCP（未升级）或系统没有 OpenAPI 地址时，代理系统本轮不可用，提示词如实说明，不会退回下发令牌。

## 成员本机的目录与代理调用

给不经过 AI 员工的调用方（成员本机的 Claude Code、Codex 等）用。设计见 [成员本机 MCP：按本人权限过滤与平台代理调用](superpowers/specs/2026-11-25-member-mcp-proxy.md)。

- 端点 `POST /api/systems/mcp`。带 `Origin` 头的请求拒绝，`GET` 返回 405；请求体最大 256 KiB（匿名 16 KiB）。
- 成员登录：带 CoreMan 的 OAuth 访问令牌（`mcp` scope，见 [成员本机接入设计](superpowers/specs/2026-11-25-coreman-oauth-mcp-cli.md)）时按成员认人，每人每分钟 120 次工具调用（各 API 进程分别计数）。没带令牌时返回 401 和 `WWW-Authenticate: Bearer resource_metadata=…`，支持 MCP 授权规范的客户端据此自动登录。成员的 CoreMan 令牌只用来认人，从不转发给业务系统。
- 系统范围：已启用、并且配了系统地址与 OpenAPI 地址的系统，不看 AI 员工授权和允许范围。
- 只读已存的目录，不为这些请求去拉取或复查：目录随 AI 员工的调用（每 10 分钟带 ETag 复查）和管理员「刷新目录」更新。从未拉取过的系统返回 `catalog_unavailable`。
- 按本人权限过滤：声明了权限查询的系统，用平台按成员签发的令牌（用途 `proxy`，签发记录带 `oauth_grant_id`，15 分钟，缓存在各 API 进程内存里）查一次权限码，过滤规则与任务内相同；`permission_check` 取 `filtered` / `unknown` / `not_declared`。成员算不出业务系统主体（缺邮箱、邮箱前缀重复）时退回匿名目录的样子（不过滤、直连），`systems_call` 返回 `subject_unavailable`。
- `token_delivery = proxy` 的系统自动对成员开放代理调用（`call_mode = proxy`）：`tools/list` 多出 `systems_call` 与 `systems_call_result`。校验、请求、响应截断与脱敏和任务内相同；读操作直接执行；`financial` 不代理；上传文件和 `batch` 返回 `unsupported_for_member`；大结果截断、不转存。每位成员对每个系统每小时最多 `proxy_calls_per_turn` 次（按 `system_calls` 计，含被拒和暂存的），超出返回 `catalog_budget_exhausted`。
- 写操作（`write`、`destructive`）只暂存（`interaction_states`，`kind = member_write`，请求按确认单与成员加密，迁移 `0134`），由系统设置里的「公共机器人」（`work_task_notifier_bot_id`，原「任务通知员工」）私聊成员一张与 AI 员工写操作确认卡同样式的卡片，另注明来自哪个本机客户端（自报名称）；返回 `pending_confirmation` 与 `confirmation_id`。没有可用的公共机器人或成员没有飞书身份时不暂存，返回 `confirmation_unavailable`；每人同时待确认最多 20 张。只有成员本人在那个私聊里点才算：确认后先记已提交，再复查（成员启用、OAuth 授权未吊销、系统仍代理、操作定义与可见性不变），以成员身份按暂存的请求执行一次（幂等键暂存时生成），审计结局 `confirmed_*`，卡片更新为结果。`systems_call_result(confirmation_id)` 最多等 20 秒，返回 `pending` / `ok` / `failed` / `unknown` / `refused` / `cancelled` / `expired`；只有同一授权能查。卡片 60 分钟有效，过期不能重新发起；清理与其他确认卡相同。
- 审计：`system_calls` 的 `task_id`、`bot_id` 为空，带 `user_id` 与 `oauth_grant_id`。
- 匿名：部署设置 `SYSTEMS_CATALOG_PUBLIC=true` 时不带令牌也能读：不按权限过滤、没有 `systems_call`，系统地址和接口说明会对能访问 CoreMan 的所有人可见，只在入口限定内网的部署里开启；端点不在 `/api/runtime/` 下，按前缀对外开放运行时路径的入口不会顺带放出它。
- 直连（匿名，以及成员模式下非代理的系统）：`call_mode` 为 `direct`；`systems_describe` 的 curl 模板引用调用方本机的环境变量（`Authorization: Bearer`），放调用方自己在该系统可用的令牌，例如统一登录的个人访问令牌。变量名默认 `SYSTEMS_TOKEN`，可在接入地址上用 `?token_env=<变量名>` 指定（字母、数字、下划线，不以数字开头，最长 64 字符，否则 400），只传变量名，不传令牌。系统只认平台自签令牌（`builtin`）时，本机通常没有可用的令牌。

给成员看的接入说明在文档站 `/docs/systems-catalog-mcp`（源文件 `coreman/docs_site/pages/systems-catalog-mcp.md`），改动时两边一起改。

本机接入：端点是标准的 MCP Streamable HTTP（只用 POST，返回 JSON，认证走 MCP 授权规范的 OAuth），支持的客户端都能接，只要填同一个地址：

```text
https://coreman.example.com/api/systems/mcp?token_env=SSO_TOKEN
```

`SSO_TOKEN` 换成本机已有的令牌变量名；不带 `token_env` 时模板引用 `$SYSTEMS_TOKEN`。令牌只留在本机环境变量里，Agent 按模板执行 curl 时由 shell 展开。

| 客户端 | 接入方式 |
|---|---|
| Claude Code | `claude mcp add --transport http coreman-systems '<地址>'` |
| Codex | `codex mcp add coreman_systems --url '<地址>'`，或在 `~/.codex/config.toml` 写 `[mcp_servers.coreman_systems]` 加 `url = "<地址>"` |
| 其他（JSON 配置） | `{"mcpServers": {"coreman-systems": {"type": "http", "url": "<地址>"}}}`，字段名以客户端文档为准 |

Codex 的 `--bearer-token-env-var` 这类认证参数不要配：CoreMan 的登录由客户端按 MCP 授权规范自己完成，业务系统令牌不应发给 CoreMan。

## 环境

迁移 `0133` 给 `systems` 加了 `environment`：`production`（真实环境，已有系统和新登记的默认值）或 `test`（测试环境，连测试数据，常常免登录）。同一个业务往往真实、测试各登记一个系统，测试环境的标识惯例为 `<真实环境标识>_test`，名称带「（测试环境）」。

- 管理台：业务系统列表和 AI 员工的「系统权限」都分「真实环境 / 测试环境」两个标签，默认在真实环境；系统权限里的勾选跨标签保留、一起保存，标签上的数字是该环境已勾选的系统数。`GET /api/admin/systems?environment=production|test` 按环境筛选，不带时列全部（旧管理台照旧）；更新时不传 `environment` 沿用库里的值。
- AI 员工：环境不影响授权和可访问的系统集合，测试环境的系统要像别的系统一样单独授权才会出现。L0 提示词里测试环境的系统标为 `(<key>，测试环境)`，本轮有这类系统时追加一句：只在发言者明确要求在测试环境操作时使用，没说时用真实环境的系统。`systems_browse` 不带参数时测试环境的系统带 `environment: "test"`。

## 管理台

- 系统编辑页：「OpenAPI 地址」输入框，保存时地址有变化就以当前管理员身份拉取一次（失败不影响保存，结果随保存响应返回）；「刷新目录」按钮（`POST /api/admin/systems/{key}/catalog/refresh`，记审计 `system.catalog_refresh`）。目录状态由 `GET /api/admin/systems/{key}/catalog` 读取。
- 输入框下方提供「接入规范」入口：在抽屉中查看规范，可以复制公开链接、下载规范和规则集。
- 目录状态：状态、拉取时间、描述大小、模块数、操作数、隐藏数，以及按严重度排列的前 20 条检查结果。
- 「权限申请」标签：个人助理的所有者为白名单外的系统提交的访问申请，由 `ai_committee` / `platform_admin` 以人的登录身份批准或拒绝；批准会把这位助理加入系统白名单并授予所需级别，拒绝的理由会告诉助理。

## 上线顺序

1. 迁移：列重命名，新增 `system_catalogs`。
2. 上线拉取、编译、管理台状态展示，先让各系统看到检查结果。
3. 上线 MCP 与提示词规则；令牌仍走环境变量。
4. 二期上线代理调用（迁移 `0060`）。确认运行时节点都已升级后，逐个系统切换 `token_delivery`，需要写操作的再为对应 AI 员工开启「允许写入」。
   切换前先用 `coreman systems delivery --mode env` 看这个系统有哪些 AI 员工在用、所在节点是否支持目录（不支持的员工切换后用不了）。改为 `proxy` 要求已填 OpenAPI 地址；地址没变而目录处于 `error` 时保存会被拒绝，先让目录拉取成功。改回 `env`、`mcp` 没有这些要求。
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
