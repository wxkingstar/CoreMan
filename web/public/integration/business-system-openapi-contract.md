# 业务系统 OpenAPI 接入规范（v1）

想让 CoreMan 机器人发现并调用自己 API 的业务系统，按本规范提供一份 OpenAPI 描述即可。CoreMan 只认这一份契约，不为单个系统写适配器。平台侧如何使用这份描述，见 CoreMan 仓库的 `docs/business-system-catalog.md`。

业务系统始终是唯一的权限执行方。CoreMan 只用描述里的权限字段决定"给机器人展示哪些操作"，不替代系统自己的鉴权。

## 1. 描述文件与获取接口

- 格式：OpenAPI 3.0.x 或 3.1.x，JSON 或 YAML，覆盖机器人可以调用的全部 HTTP 操作。
- 地址：填在管理台「业务系统 → OpenAPI 地址」，必须与系统地址同源，使用 HTTPS。
- 认证：接受与业务 API 相同的令牌（按系统配置的令牌签发方，Bearer 或 Cookie），只要求已认证，不要求具体权限。建议不要对匿名访问开放；有意公开的描述也能读取。
- 内容：对所有调用者返回同一份，不按调用者裁剪。CoreMan 跨用户缓存这份内容，权限过滤由平台完成（见第 5、7 节）。
- 缓存：返回 `ETag`，支持 `If-None-Match` 并返回 304。
- 限制：正文不超过 10 MiB；只允许文件内引用（`#/components/...`），不支持外部 `$ref`；CoreMan 不跟随重定向。

## 2. 每个操作的必填项

| 字段 | 要求 |
|---|---|
| `operationId` | 在本系统内唯一且长期稳定，匹配 `^[A-Za-z][A-Za-z0-9_]{0,99}$`。语义不兼容的变更使用新的 id |
| `tags[0]` | 所属模块。必须在顶层 `tags` 中定义并写 `description`。CoreMan 只按第一个 tag 分组 |
| `summary` | 不超过 80 字，说清这个操作做什么。不要写"获取数据""列表接口"这类空话 |
| `x-permission` | 调用所需权限，见第 5 节 |
| 参数与请求体 | 有类型，每个字段有 `description`，必填字段写进 `required` |
| 2xx 响应 | JSON 响应有明确的 schema，不要只写 `type: object` 加 `additionalProperties: true` |

推荐填写：

- `description`：什么场景用、统计口径、单位、时区、分页方式、与相近操作的区别。
- `example` / `examples`：至少给出典型请求体。

## 3. 模块（顶层 tags）

- 每个 tag 写 `name` 和 `description`（不超过 200 字）。机器人先看到的是模块列表，说明要能让它判断"要找的操作在不在这里"。
- 每个模块建议不超过 60 个可见操作，超过就拆分。
- 同一系统内命名风格保持一致。

## 4. 操作级扩展 `x-agent`

```yaml
x-agent:
  risk: read            # read | write | destructive | financial
  hidden: false         # true 时不出现在目录中
  hint: 金额单位为分；按创建时间倒序返回   # 可选，不超过 500 字
  extra_permissions: [stock:cost:read]       # 可选，见第 5 节
```

- `risk` 以这里的声明为准，平台不按 HTTP 方法推断。缺省时 GET、HEAD 视为 `read`，其他方法视为 `write`。用 POST 传查询条件的只读操作必须写 `risk: read`。
  - `write`：可以撤回或修改的写操作，例如创建草稿、更新资料。
  - `destructive`：不可撤销，例如作废、删除、过账、冲销。
  - `financial`：涉及资金流动，例如退款、打款。平台默认不向机器人开放。
- `hidden: true`：登录与回调、只允许人工会话的操作、内部维护接口。**系统在服务端要求人工会话的操作，必须标记为 hidden。**
- `hint`：调用时容易出错的地方。全系统通用的约定写在根级 `guide`（第 6 节），不要每个操作重复。

## 5. 权限 `x-permission`

- 字符串：调用所需的权限码。字符串数组：需要同时具备的全部权限码。`none`：只要登录即可。
- 权限码必须与第 6 节权限查询接口返回的权限码处于同一命名空间。
- 匹配规则：持有 `*` 匹配全部；持有以 `:*` 结尾的码（如 `stock:doc:*`）匹配所有以 `stock:doc:` 开头的码；其余精确匹配。
- 条件权限（例如某个参数取某值时需要额外权限）和字段级脱敏，写进 `description`，并在 `x-agent.extra_permissions` 中列出。展示过滤只看 `x-permission`。
- 系统应有测试保证 `x-permission` 与代码实际检查的权限一致，避免描述与实现脱节。

## 6. 根级扩展 `x-agent`

```yaml
x-agent:
  guide: |
    所有时间参数为 UTC ISO 8601；金额字段为整数分。
    响应统一包裹为 {"code":0,"data":...}；错误时 code 非 0，message 为原因。
    列表接口用 page、page_size 分页，page_size 最大 100。
  permissions:
    operationId: getMe
    pointer: /data/permissions
```

- `guide`：全系统通用的约定，不超过 2000 字，例如时区、金额单位、响应包裹格式、错误格式、分页方式。机器人浏览这个系统时首先读到它。
- `permissions`：指向一个返回"当前调用者权限码"的操作（GET、无必填参数、`x-permission: none`）。`pointer` 是 [RFC 6901](https://www.rfc-editor.org/rfc/rfc6901) JSON Pointer，指向一个字符串数组。未声明时平台不做权限过滤，所有未隐藏的操作都会出现在目录中。

## 7. 取值很多的参数 `x-agent-options`

品牌、类目、供应商这类可选值成百上千的参数，不要把全部取值内联成 `enum`（单个 `enum` 不超过 50 个值）。改为在参数或 schema 属性上声明一个查询可选值的只读操作：

```yaml
- name: brand
  in: query
  schema: { type: string }
  description: 品牌编码
  x-agent-options: listBrandOptions
```

机器人需要取值时调用 `listBrandOptions`。被引用的操作应支持关键字过滤和分页。

## 8. 错误

- 权限不足返回 403，并在正文中给出所需权限码；能提供权限申请地址更好，机器人会据此提示用户申请。
- 错误正文格式写进根级 `guide`。

## 9. 最小示例

```yaml
openapi: 3.0.3
info: { title: 库存, version: "1.0" }
x-agent:
  guide: 时间为 UTC ISO 8601；金额为整数分。
  permissions: { operationId: getMe, pointer: /data/permissions }
tags:
  - name: documents
    description: 出入库单据：创建草稿、确认、作废、查询
paths:
  /api/stock/me:
    get:
      operationId: getMe
      tags: [documents]
      summary: 当前调用者身份与权限码
      x-permission: none
      responses:
        "200":
          description: OK
          content:
            application/json:
              schema:
                type: object
                properties:
                  data:
                    type: object
                    properties:
                      permissions: { type: array, items: { type: string } }
  /api/stock/documents/{id}/cancel:
    post:
      operationId: cancelDocument
      tags: [documents]
      summary: 作废一张已确认的单据并回滚库存
      description: 只能作废状态为 confirmed 的单据；作废后不可恢复。
      x-permission: stock:doc:confirm
      x-agent: { risk: destructive }
      parameters:
        - { name: id, in: path, required: true, schema: { type: string }, description: 单据 ID }
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [reason]
              properties:
                reason: { type: string, description: 作废原因，写入审计日志 }
      responses:
        "200":
          description: OK
          content:
            application/json:
              schema:
                type: object
                properties:
                  data:
                    type: object
                    properties:
                      id: { type: string }
                      status: { type: string, enum: [cancelled] }
```

## 10. 自检

用 [Spectral](https://github.com/stoplightio/spectral) 和 CoreMan 提供的规则集检查描述文件，建议放进系统自己的 CI：

```bash
npx @stoplight/spectral-cli lint api/openapi.yaml --ruleset business-system-contract.spectral.yaml
```

规则集 `business-system-contract.spectral.yaml` 与本文发布在同一目录，也可以在 CoreMan 管理台「业务系统」编辑页下载。规则集只覆盖能静态检查的部分；`x-permission` 与代码实现是否一致，需要系统自己的测试保证。
