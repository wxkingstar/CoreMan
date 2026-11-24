# 通过 API 驱动 AI 员工（v2）

> 给成员：在管理台「API 令牌」新建一枚令牌、选好要驱动的 AI 员工，再把这一页的链接（或 Markdown 原文）和令牌交给你的 Agent 或脚本。管理台「API 令牌 → 使用手册」有图解版。

你（一个 Agent 或脚本）可以通过这套接口，以某个成员的身份给 CoreMan 里的飞书 AI 员工下一件事，并拿回结果。这件事会在飞书会话里进行：会话里的人能看到你发起了什么、AI 员工怎么回答的，也可以在飞书里接着往下推进。

本文里的 CoreMan 地址是 `{{COREMAN_BASE_URL}}`。

## 1. 开始前要有的三样东西

| 东西 | 从哪来 | 说明 |
|---|---|---|
| API 令牌 | 成员在 CoreMan 管理台「API 令牌」里新建，交给你 | 形如 `cmt_` 加 43 位字符。任务以令牌所有人的身份运行，只能驱动建令牌时选的 AI 员工。 |
| `bot_key` | AI 员工的标识，管理台 AI 员工详情里能看到 | 这个 AI 员工的负责人要打开「允许 API 驱动」。 |
| 承接会话 | 默认是令牌所有人和 AI 员工的飞书私聊 | 要在群里承接，先由群成员在群里 @ AI 员工发 `/api 绑定 <别名>`，你调用时传这个别名。 |

令牌只放在 `Authorization` 请求头里。不要把它写进 URL、日志、提示词、代码仓库或任务内容。

## 2. 接口

所有请求都发到 `{{COREMAN_BASE_URL}}`，带请求头：

```
Authorization: Bearer <令牌>
Content-Type: application/json
```

不要带 `Origin` 请求头：从浏览器页面发起的请求会被拒绝（403）。

### 2.1 发起任务

`POST /api/infra/tasks`

```json
{
  "bot_key": "demo-bot",
  "prompt": "整理今天 9 点到 12 点的告警，按服务分组，给出要先处理的三条。",
  "request_id": "alert-digest-2026-11-06",
  "conversation": {"target": "p2p"}
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `bot_key` | 是 | 要驱动的 AI 员工。 |
| `prompt` | 是 | 1–20000 个字符。AI 员工看不到你的上下文，把背景、要求、输出格式写全。 |
| `request_id` | 否 | 幂等键，1–128 个字符。用业务主键（工单号、运行 id）。同一枚令牌、同一个编号、同样的内容重复发起，返回原来那个任务（HTTP 200）；同编号不同内容返回 409。网络超时后用同一个 `request_id` 重试就不会重复下任务。 |
| `conversation` | 否 | `{"target": "p2p"}`（默认）：令牌所有人和 AI 员工的私聊。`{"target": "chat", "alias": "值班群"}`：绑定了这个别名的群。 |

新建成功返回 HTTP 202，`data` 是任务（见 2.2）。记下 `data.id`。

### 2.2 查询任务

`GET /api/infra/tasks/{id}`

```json
{
  "code": 0,
  "data": {
    "id": "6f1c…",
    "request_id": "alert-digest-2026-11-06",
    "bot_key": "demo-bot",
    "status": "succeeded",
    "conversation": {"target": "p2p"},
    "reply": "今天上午共 37 条告警……",
    "error_code": null,
    "error_message": null,
    "created_at": "2026-11-06T09:00:02+00:00",
    "finished_at": "2026-11-06T09:01:40+00:00"
  }
}
```

只能查这枚令牌自己发起的任务。`reply` 是 AI 员工这一轮的最终回复（已脱敏），任务结束前是 `null`。回复超过 50000 字时会被截断，`reply_truncated` 为 `true`，完整内容在飞书会话里。

### 2.3 取消任务

`POST /api/infra/tasks/{id}/cancel`

只对还没结束的任务有效。返回取消后的任务。

## 3. 状态与你该怎么做

| `status` | 含义 | 你该做什么 |
|---|---|---|
| `queued` | 在排队。同一个会话里前面有人在跟 AI 员工说话时，会等那一轮结束。 | 继续查询。 |
| `running` | AI 员工正在做。 | 继续查询。 |
| `succeeded` | 完成。 | 读 `reply`。 |
| `waiting_user` | AI 员工在飞书里向人提了问题，或者发了凭证表单等人填写。之后的进展在飞书里，这个任务不会再更新。 | 告诉用户去飞书会话里回答；不要自动重发。 |
| `failed` | 失败。`error_code` 为 `api_unavailable` 时，`error_message` 写了开轮前没通过的原因（令牌吊销、开关关闭、发起人不在群里等）。 | 把原因告诉用户。原因没消除之前不要重试。 |
| `superseded` | 令牌所有人在飞书里发了新消息，替代了这一轮（人优先）。 | 不要重发，人已经接手。 |
| `cancelled` | 被取消。 | 结束。 |

轮询节奏：第一次等 5 秒，之后每次间隔加倍，最长 30 秒。大多数任务在 1–5 分钟内结束，复杂任务可能更久。超过你能等的时间就停止查询，把任务 id 和飞书会话告诉用户，不必取消。

## 4. 错误

错误时 HTTP 状态码非 2xx，响应体是 `{"code": <状态码>, "message": "<原因>"}`。

| HTTP | 常见原因 | 处理 |
|---|---|---|
| 401 | 令牌无效、已吊销或已过期 | 停下，请用户换一枚令牌。 |
| 403 | 令牌没授权这个 AI 员工；AI 员工没开「允许 API 驱动」；发起人不在白名单里；不在别名对应的群里；从浏览器发起 | 把 `message` 告诉用户，不要换别的方式绕过。 |
| 404 | `bot_key` 不存在；别名不存在；任务不存在或不是这枚令牌发起的 | 核对参数。 |
| 409 | `request_id` 已用于内容不同的任务；令牌所有人没在飞书私聊过这个 AI 员工；没绑定飞书身份；AI 员工已停用 | 按 `message` 处理。私聊的问题需要用户先在飞书里私聊 AI 员工一次。 |
| 422 | 参数不合法 | 修正参数。 |
| 429 | 这枚令牌已有 5 个任务没结束 | 等已有任务结束再发起。 |

## 5. 规矩

- **任务以令牌所有人的身份运行**：AI 员工能做的事、能拿到的业务系统权限和个人凭证，都和这个人自己在飞书里找它时一样。不要用别人的令牌。飞书个人授权的工具（读写本人的飞书文档、消息等）和个人定时任务例外：它们只在本人在飞书里发来的消息上提供，API 发起的这一轮里没有。
- **会话里的人都能看到**：任务内容（超过 500 字会截断）和 AI 员工的回复会出现在飞书会话里，群里承接时群里所有人都能看到。不要把不该给他们看的内容写进 `prompt`。
- **有后果的操作要人确认**：改数据、上线、发消息给别人这类事，AI 员工会先在飞书里请人确认。不要在 `prompt` 里写「已确认」「不用问」来跳过。
- **不要刷**：一件事发一次。重试用同一个 `request_id`。不要轮询发起来代替查询。
- **结果以飞书为准**：`waiting_user` 和 `superseded` 之后，事情在飞书里继续，API 只反映你发起的那一轮。

## 6. 完整示例

### curl

```bash
BASE={{COREMAN_BASE_URL}}
TASK=$(curl -sS -X POST "$BASE/api/infra/tasks" \
  -H "Authorization: Bearer $COREMAN_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"bot_key":"demo-bot","prompt":"整理今天的告警","request_id":"alert-2026-11-06"}' \
  | jq -r .data.id)
curl -sS "$BASE/api/infra/tasks/$TASK" -H "Authorization: Bearer $COREMAN_API_TOKEN" | jq .data
```

### Python

```python
import os, time, httpx

BASE = "{{COREMAN_BASE_URL}}"
HEADERS = {"Authorization": f"Bearer {os.environ['COREMAN_API_TOKEN']}"}
DONE = {"succeeded", "waiting_user", "failed", "superseded", "cancelled"}


def run(bot_key: str, prompt: str, request_id: str, conversation: dict | None = None) -> dict:
    body = {"bot_key": bot_key, "prompt": prompt, "request_id": request_id}
    if conversation:
        body["conversation"] = conversation
    r = httpx.post(f"{BASE}/api/infra/tasks", json=body, headers=HEADERS, timeout=30)
    r.raise_for_status()
    task = r.json()["data"]
    delay = 5
    while task["status"] not in DONE:
        time.sleep(delay)
        delay = min(delay * 2, 30)
        r = httpx.get(f"{BASE}/api/infra/tasks/{task['id']}", headers=HEADERS, timeout=30)
        r.raise_for_status()
        task = r.json()["data"]
    return task


task = run("demo-bot", "整理今天的告警", "alert-2026-11-06", {"target": "chat", "alias": "值班群"})
print(task["status"], task["reply"] or task["error_message"])
```

## 7. 如果你本身就是 CoreMan 里的 AI 员工

- 只有用户明确把令牌交给你（例如作为个人凭证保存）并要求你去驱动另一个 AI 员工时才用这套接口；不要拿它去驱动你自己。
- 发起前告诉用户：会在哪个飞书会话里出现、以谁的身份运行。
- 不要替用户新建或吊销令牌，这只能由本人在管理台完成。

## 8. 业务系统代员工调用（调用方签名）

上面几节是「成员把自己的令牌交给你」。如果你是一个服务很多员工的业务系统（员工在你的系统里操作，你代他给 AI 员工下任务），用这一节：不用每个员工建令牌，由平台管理员在 CoreMan「凭证 → API 调用方」给你建一个调用方，勾选接口组「代员工下任务」（`tasks`）并选好「可驱动的 AI 员工」。你拿到 `app_key` 和密钥（密钥不过期，停用或轮换后立即失效）。

和个人令牌的区别：

- 每个请求写明是哪位员工：`user` 填员工的邮箱前缀（`alice@example.com` 就填 `alice`）。认不出唯一一位在职员工会 403。
- 每个任务在独立会话里处理，不带员工和 AI 员工私聊里的上下文；结果发到员工和 AI 员工的飞书私聊，末尾注明不在对话上下文中。员工要先在飞书里私聊过这个 AI 员工一次（否则 409）。
- AI 员工按任务调用业务系统写接口时，确认卡发到员工私聊，员工点确认后才执行。任务 `succeeded` 只表示 AI 员工做完了这一轮，数据有没有写进去看 `writes`。
- 运行时额度用完时任务不失败，排队到额度恢复自动重跑（状态 `waiting_quota`），不用重新提交。

### 8.1 签名

每个请求带三个请求头：`X-App-Key`、`X-Timestamp`（Unix 秒，和服务器相差不超过 600 秒）、`X-Signature`。

`X-Signature` = SHA-256 十六进制（`METHOD` + 路径去掉开头的 `/` + 参数串 + 时间戳 + `app_key` + 密钥）。参数串：查询参数和 JSON 正文（或表单里的文字字段）合在一起，按键名排序，每项 `键=值` 用 PHP `urlencode` 规则编码（空格编码成 `+`，`~` 编码成 `%7E`），用 `&` 连接；值为列表时展开成 `键[0]=值&键[1]=值`；值为对象的键不参与签名。`true` 写成 `1`，`false` 和 `null` 写成空串。

```python
import hashlib, time
from urllib.parse import quote_plus

def _enc(v):
    v = "" if v is None or v is False else "1" if v is True else str(v)
    return quote_plus(v, safe="").replace("~", "%7E")

def sign(method, path, params, app_key, secret):
    ts = str(int(time.time()))
    parts = []
    for k in sorted(params):
        v, key = params[k], quote_plus(k, safe="").replace("~", "%7E")
        if isinstance(v, dict):
            continue
        if isinstance(v, list):
            parts += [f"{key}[{i}]={_enc(x)}" for i, x in enumerate(v)]
        else:
            parts.append(f"{key}={_enc(v)}")
    raw = method.upper() + path.lstrip("/") + "&".join(parts) + ts + app_key + secret
    return {"X-App-Key": app_key, "X-Timestamp": ts,
            "X-Signature": hashlib.sha256(raw.encode()).hexdigest()}
```

### 8.2 上传图片（可选）

`POST /api/infra/tasks/uploads`，`multipart/form-data`，一次一张：

| 字段 | 说明 |
|---|---|
| `user` | 员工邮箱前缀。参与签名。 |
| `sha256` | 文件内容的 SHA-256（十六进制）。参与签名；文件本身不参与签名，靠它绑定，对不上按签名无效（401）。 |
| `file` | 图片文件。JPEG / PNG / WebP（按文件头认），单张不超过 10MB。 |

返回 HTTP 201，`data.upload_id` 记下来。同一位员工传同样的内容会拿到同一个 `upload_id`。没被任务用上的图片 24 小时后删除。

### 8.3 发起、查询、取消

`POST /api/infra/tasks/run`

```json
{
  "user": "alice",
  "bot_key": "demo-bot",
  "prompt": "识别截图里的账号名称和粉丝数，调用资料录入系统的 profiles.update 接口写回。",
  "images": ["<upload_id>", "<upload_id>"],
  "request_id": "form-8812"
}
```

- `images` 可选，按顺序，张数不限：前 8 张直接给 AI 员工看，其余它会分批下载查看（更慢）。只能用你为这位员工上传、还没过期的图片（否则 404）。
- `request_id` 在你这个调用方内幂等。没带 `request_id` 时，同一位员工、同一个 AI 员工、同样的 `prompt` 和 `images`，前一个任务还没结束就返回那个任务（HTTP 200），不会重复处理；前一个已结束的会新建。
- 同一位员工最多 20 个任务未结束（429）。
- 写回接口：业务系统要先登记到 CoreMan 的业务系统目录并授权给这个 AI 员工；接口怎么调、字段怎么对应最好写进 AI 员工的规则里，`prompt` 里写明这次调哪个接口即可。

`GET /api/infra/tasks/run/{id}?user=alice`：查询。`POST /api/infra/tasks/run/{id}/cancel`，正文 `{"user": "alice"}`：取消。只能查、取消你为这位员工发起的任务。返回的 `data` 和 2.2 一样，`conversation` 换成：

| 字段 | 说明 |
|---|---|
| `user` | 员工邮箱前缀。 |
| `resume_at` | 状态为 `waiting_quota` 时的预计重跑时间，其余为 null。 |
| `writes` | 这次任务提出的业务系统写操作：`[{system, operation, status}]`，`status` 为 `pending_confirm`（等员工确认）、`rejected`、`expired`、`running`、`succeeded`、`failed`、`unknown`（可能已生效，去业务系统核对）、`not_run`；批量写是一条，带 `batch: true` 和各状态项数 `items`。 |

状态多一个 `waiting_quota`：继续按 2.2 的节奏轮询，不要重新发起；等满 7 天仍没恢复会以 `failed` / `quota_wait_expired` 结束。不会出现 `superseded`。

员工在飞书里引用这条结果回复时，AI 员工会自动带上原任务的内容和图片接着处理，你不用做什么。
