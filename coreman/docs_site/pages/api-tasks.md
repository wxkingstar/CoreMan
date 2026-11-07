# 通过 API 驱动 AI 员工（v1）

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
