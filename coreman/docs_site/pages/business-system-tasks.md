# 业务系统代员工调用 AI 员工

> 给业务系统的开发者，以及替业务系统写对接代码的 Agent。平台管理员在 CoreMan 管理台「凭证 → API 调用方」建好调用方后，把这一页的链接（或 Markdown 原文）和 `app_key`、密钥交给你。

员工在你的系统里操作（比如上传几张社交账号主页截图），你的系统以这位员工的身份把一件事交给 CoreMan 里的飞书 AI 员工。AI 员工处理完，结果发到员工和它的飞书私聊；需要写回你的系统时，先在私聊里请员工确认，确认后以员工的身份调用你的接口。员工可以在飞书里引用这条结果接着纠正、追问。

你不用给每位员工建令牌：一个调用方、一把密钥，每个请求写明是哪位员工。

本文里的 CoreMan 地址是 `{{COREMAN_BASE_URL}}`。只有能访问管理台的网络才能调用；部署方只在内网开放管理台时，接口也只在内网可用。

## 1. 整个流程

1. （有图片时）逐张上传图片，拿到 `upload_id`。
2. 发起任务：写明员工、AI 员工、要做的事和图片。返回任务 `id`。
3. AI 员工在一个独立会话里处理：员工的飞书私聊里先出现一句「在某系统里发起」，随后是 AI 员工的回复，末尾注明这条不在对话上下文中。
4. 需要写回你的系统时，私聊里出现确认卡，员工点确认后 CoreMan 以员工身份调你的接口。
5. 你轮询查询接口，拿到最终回复和每个写操作的结果。

## 2. 开始前要准备好的

| 事项 | 谁来做 | 说明 |
|---|---|---|
| 调用方（`app_key` + 密钥） | CoreMan 平台管理员 | 「凭证 → API 调用方」新建，接口组勾选「代员工下任务」（`tasks`），并在「可驱动的 AI 员工」里选好 AI 员工。密钥只显示一次，不过期；停用或轮换后立即失效。 |
| `bot_key` | AI 员工的负责人告诉你 | AI 员工详情里能看到。这个 AI 员工要打开「允许 API 驱动」，员工要在它的可用名单里。 |
| 员工身份 | 你的系统 | 每个请求的 `user` 填员工的邮箱前缀（`alice@example.com` 填 `alice`），要能认出唯一一位在职员工，且绑定了飞书身份。 |
| 员工私聊过 AI 员工 | 员工本人 | 员工要先在飞书里给这个 AI 员工发过一次消息，否则结果没处发（409）。 |
| 写回接口 | 你的系统 + CoreMan 平台管理员 | 你的系统按[业务系统 OpenAPI 接入规范](business-system-contract.md)提供接口描述，登记进 CoreMan 的业务系统目录并授权给这个 AI 员工。字段怎么对应、调哪个接口，最好写进 AI 员工的规则里；任务内容里写明这次要调哪个接口即可。 |

## 3. 签名

每个请求带三个请求头：

| 请求头 | 值 |
|---|---|
| `X-App-Key` | 调用方的 `app_key`。 |
| `X-Timestamp` | 当前 Unix 时间（秒），和服务器相差不能超过 600 秒。 |
| `X-Signature` | 下面算出的签名，64 位十六进制小写。 |

签名 = SHA-256（`请求方法` + `路径去掉开头的 /` + `参数串` + `时间戳` + `app_key` + `密钥`）。

- 路径不含查询串，例如 `api/infra/tasks/run/6f1c…`。
- 参数串：查询参数和请求正文的字段（JSON 正文的顶层字段，或 `multipart/form-data` 里的文字字段）合在一起，按键名排序，每项写成 `键=值`，键和值都按 PHP `urlencode` 规则编码（空格编成 `+`，`~` 编成 `%7E`），用 `&` 连接。
- 值是列表时展开成 `键[0]=值&键[1]=值`（方括号不编码）；空列表不出现。值是对象的键不参与签名。
- `true` 写成 `1`，`false` 和 `null` 写成空串。
- 上传的文件本身不参与签名，靠签过名的 `sha256` 字段绑定。

签名错、时间戳超出范围、调用方被停用都返回 401，不区分原因。

```python
import hashlib
import time
from urllib.parse import quote_plus


def _enc(value):
    value = "" if value is None or value is False else "1" if value is True else str(value)
    return quote_plus(value, safe="").replace("~", "%7E")


def sign(method, path, params, app_key, secret):
    """params：查询参数和正文顶层字段合在一起的 dict。返回要带上的三个请求头。"""
    ts = str(int(time.time()))
    parts = []
    for key in sorted(params):
        value, name = params[key], _enc(key)
        if isinstance(value, dict):
            continue
        if isinstance(value, list):
            parts += [f"{name}[{i}]={_enc(item)}" for i, item in enumerate(value)]
        else:
            parts.append(f"{name}={_enc(value)}")
    raw = method.upper() + path.lstrip("/") + "&".join(parts) + ts + app_key + secret
    return {
        "X-App-Key": app_key,
        "X-Timestamp": ts,
        "X-Signature": hashlib.sha256(raw.encode()).hexdigest(),
    }
```

密钥只放在你的服务端配置里，不要发到浏览器、写进日志或任务内容。

## 4. 接口

所有接口的响应体都是 `{"code": 0, "data": …}`；出错时 HTTP 状态码非 2xx，响应体是 `{"code": <状态码>, "message": "<原因>"}`。

### 4.1 上传图片（可选）

`POST /api/infra/tasks/uploads`，`multipart/form-data`，一次一张。

| 字段 | 说明 |
|---|---|
| `user` | 员工邮箱前缀。参与签名。 |
| `sha256` | 文件内容的 SHA-256（十六进制）。参与签名；和文件对不上按签名无效处理（401）。 |
| `file` | 图片文件。按文件头识别，只收 JPEG、PNG、WebP，单张不超过 10MB。 |

返回 HTTP 201：

```json
{
  "code": 0,
  "data": {
    "upload_id": "3b0e…",
    "sha256": "9f86…",
    "bytes": 245812,
    "content_type": "image/png",
    "expires_at": "2026-11-26T08:00:00+00:00"
  }
}
```

- 同一位员工传同样的内容，拿到的是同一个 `upload_id`。
- 上传属于「这个调用方 + 这位员工」，别的员工、别的调用方用不了。
- 24 小时内没被任务用上的图片会被删除；被任务用上的保留 30 天，员工引用结果回复时 AI 员工还能再看到。

### 4.2 发起任务

`POST /api/infra/tasks/run`，JSON 正文：

```json
{
  "user": "alice",
  "bot_key": "demo-bot",
  "prompt": "识别截图里的账号名称、粉丝数和简介，调用资料录入系统的 profiles.update 接口写回到申请单 8812。",
  "images": ["3b0e…", "a71c…"],
  "request_id": "form-8812"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `user` | 是 | 员工邮箱前缀。群承接模式不带，见 9.1。 |
| `bot_key` | 是 | 要驱动的 AI 员工，必须在调用方「可驱动的 AI 员工」里。 |
| `prompt` | 是 | 1–20000 个字符。AI 员工看不到你系统里的上下文，把背景、要求和要写回的位置（单号、记录 id）写全。 |
| `images` | 否 | `upload_id` 列表，按顺序，张数不限。只能用这个调用方为这位员工上传、还没过期的图片。前 8 张直接给 AI 员工看；更多的它会分批下载查看，会更慢。 |
| `request_id` | 否 | 幂等键，1–128 个字符，在这个调用方内唯一。用业务主键（申请单号等）。 |

新建返回 HTTP 202，`data` 是任务（见 4.3）。记下 `data.id`。

重复提交不会重复处理：

- 带 `request_id`：同一个编号、同样的员工、AI 员工、内容和图片，返回原来的任务（HTTP 200）；同编号换了内容返回 409。网络超时后用同一个 `request_id` 重试即可。
- 不带 `request_id`：同一位员工、同一个 AI 员工、同样的 `prompt` 和 `images`，前一个任务还没结束就返回那个任务（HTTP 200）；前一个已结束的会新建。

同一位员工在这个调用方下最多 20 个任务未结束，超过返回 429。

### 4.3 查询任务

`GET /api/infra/tasks/run/{id}?user=alice`

```json
{
  "code": 0,
  "data": {
    "id": "6f1c…",
    "request_id": "form-8812",
    "bot_key": "demo-bot",
    "status": "succeeded",
    "user": "alice",
    "resume_at": null,
    "writes": [
      {"system": "profile_admin", "operation": "profiles.update", "status": "succeeded"}
    ],
    "reply": "已识别：账号 demo.shop，粉丝 12.4 万……已提交写回，等你确认。",
    "reply_truncated": false,
    "error_code": null,
    "error_message": null,
    "created_at": "2026-11-25T08:00:02+00:00",
    "finished_at": "2026-11-25T08:01:40+00:00"
  }
}
```

| 字段 | 说明 |
|---|---|
| `status` | 见第 5 节。 |
| `reply` | AI 员工这一轮的最终回复（已脱敏），任务结束前是 `null`。超过 50000 字会被截断，`reply_truncated` 为 `true`，完整内容在飞书私聊里。 |
| `writes` | 这个任务提出的写操作，见第 6 节。 |
| `resume_at` | 状态为 `waiting_quota` 时的预计重跑时间，其余为 `null`。 |
| `error_code`、`error_message` | 只在 `failed` 时有值。 |

只能查这个调用方为这位员工发起的任务；`user` 对不上、任务不是你发起的都返回 404。

### 4.4 取消任务

`POST /api/infra/tasks/run/{id}/cancel`，JSON 正文 `{"user": "alice"}`。只对还没结束的任务有效，返回取消后的任务。

## 5. 状态与你该怎么做

| `status` | 含义 | 你该做什么 |
|---|---|---|
| `queued` | 在排队。 | 继续查询。 |
| `running` | AI 员工正在处理。 | 继续查询。 |
| `waiting_quota` | AI 员工所用的模型额度用完了，任务已排队，到 `resume_at` 前后自动重跑。 | 继续按节奏查询，不要重新发起。等满 7 天仍没恢复会以 `failed`、`quota_wait_expired` 结束。 |
| `succeeded` | AI 员工做完了这一轮。 | 读 `reply`；数据有没有写进你的系统看 `writes`。 |
| `waiting_user` | AI 员工在飞书私聊里向员工提了问题，或发了表单等员工填写。之后的进展在飞书里，这个任务不再更新。 | 提示员工去飞书私聊处理，不要自动重发。 |
| `failed` | 失败。`error_code` 为 `api_unavailable` 时，`error_message` 写了开始前没通过的原因（调用方被停用或不再允许驱动这个 AI 员工、AI 员工关了 API 驱动或已停用、员工账号不可用等）。 | 把原因告诉员工或管理员，原因消除前不要重试。 |
| `cancelled` | 被取消。 | 结束。 |

轮询节奏：第一次等 5 秒，之后每次间隔加倍，最长 30 秒。多数任务 1–5 分钟内结束；图片多、要写回的会更久。员工确认写操作的时间不算在任务里：任务 `succeeded` 之后，`writes` 里的状态还可能变化，需要结果时再查几次。

## 6. 写回你的系统

AI 员工不会直接写你的系统。它按规则准备好写操作后，CoreMan 在员工的飞书私聊里发一张确认卡，列出要调的接口和内容；只有员工本人点确认才会执行，执行时用的是员工自己的身份和权限。

单个写操作在 `writes` 里是一项 `{system, operation, status}`（不含请求内容）：

| `status` | 含义 |
|---|---|
| `pending_confirm` | 等员工在私聊里确认。 |
| `rejected` | 员工拒绝了。 |
| `expired` | 确认卡过期，没有执行。 |
| `running` | 已确认，正在执行。 |
| `succeeded` | 执行成功。 |
| `failed` | 执行失败。 |
| `unknown` | 结果不确定，可能已经生效，请在你的系统里核对。 |
| `not_run` | 确认了但这一项没有执行（例如同一次确认里前面的失败了）。 |

批量写（一次确认很多项）整批算一项，带 `batch: true`：`status` 是 `pending_confirm`、`rejected`、`expired` 或 `confirmed`；`confirmed` 之后 `items` 给出各状态的项数，主要看 `ok`（成功）、`http_error` 和 `unreachable`（失败）、`unknown`（结果不确定，请核对）、`skipped`（没有执行），其余是还在处理中。

你的接口按自己的鉴权规则校验即可：CoreMan 调用时以这位员工的身份请求（令牌主体是员工的邮箱前缀），不是以调用方的身份。

## 7. 员工在飞书里看到什么

- 私聊里先出现一句「在某系统里发起」，注明附了几张图，然后是 AI 员工的回复，末尾注明这条不在对话上下文中。
- 每个任务都在独立会话里处理：不带员工和 AI 员工平时聊天的上下文，员工在私聊里发的新消息也不会打断它。
- 员工引用这条结果回复时，AI 员工会带上原任务的内容、图片、回复和写操作记录接着处理，你不用做什么。

## 8. 错误

| HTTP | 常见原因 | 处理 |
|---|---|---|
| 401 | 签名错误、时间戳超出 600 秒、调用方不存在或已停用；上传时 `sha256` 与文件不符 | 核对签名算法和服务器时间；密钥是否已轮换。 |
| 403 | 调用方没有「代员工下任务」接口组；调用方没授权驱动这个 AI 员工；AI 员工没开「允许 API 驱动」；员工不在 AI 员工的可用名单里；按邮箱前缀认不出唯一一位在职员工 | 把 `message` 转给 CoreMan 管理员或 AI 员工负责人。 |
| 404 | `bot_key` 不存在；图片不存在、已过期或不是为这位员工上传的；任务不存在或不是你为这位员工发起的 | 核对参数；图片过期就重新上传。 |
| 409 | `request_id` 已用于内容不同的任务；员工没在飞书私聊过这个 AI 员工；员工没绑定飞书身份；AI 员工已停用 | 按 `message` 处理。没私聊过的，请员工先在飞书里给 AI 员工发一条消息。 |
| 413 | 单张图片超过 10MB，或请求体过大 | 压缩后重传。 |
| 415 | 不是 JPEG、PNG、WebP 图片 | 转换格式后重传。 |
| 422 | 参数不合法（缺字段、`prompt` 为空或超长、AI 员工不是飞书 AI 员工；群承接模式下带了 `user` 或 `images`、配了多个群却没给 `chat_id`） | 修正参数。 |
| 429 | 这位员工已有 20 个任务未结束；群承接模式下这个群已有 5 条任务在排队（`errors` 为 `[{"type": "queue_full"}]`，这条没有接收） | 等已有任务结束再发起。 |

## 9. 规矩

- **以员工的身份运行**：AI 员工能做的事、能拿到的业务系统权限，和这位员工自己在飞书里找它时一样。`user` 必须是在你系统里实际操作的那位员工，不要填别人。
- **员工能看到全部内容**：任务内容和 AI 员工的回复都会出现在员工的飞书私聊里。不要把不该给这位员工看的内容写进 `prompt`。
- **写操作一律由员工确认**：不要在 `prompt` 里写「已确认」「不用问」来跳过确认。
- **不要刷**：一件事发一次，重试用同一个 `request_id`；不要用重复发起代替查询。
- **结果以飞书为准**：`waiting_user` 之后事情在飞书里继续，API 只反映你发起的那一轮。

## 9.1 群承接模式（不代员工，结果进群）

监控、告警这类系统没有「哪位员工」。平台管理员可以为你的调用方和某个 AI 员工配「群承接」：指定一个飞书群和一个服务账号。配了之后，你驱动这个 AI 员工时：

- **不要带 `user`**（带了返回 422）。任务以配置的服务账号身份运行，AI 员工只有这个账号的权限。
- 配了多个群时用 `chat_id` 指定在哪个群发起；只有一个群可以省略。不支持 `images`。
- 群里先出现「在「你的系统名」里发起」加任务内容，AI 员工在群的主会话里处理，回复直接发在群里，群里的人可以接着跟它说。你传入的内容对 AI 员工来说是外部数据，不是指令。
- 写操作的确认卡发到群里，群里有权限的人点确认，按**点的人**的身份执行。
- 每个群最多 5 条你这类任务在排队（不含正在执行的），超出返回 429，`errors` 为 `[{"type": "queue_full"}]`，这条不会被接收，稍后再发。
- 查询、取消不带 `user`；返回的任务用 `"conversation": {"target": "chat", "chat_id": "…"}` 代替 `user`。

```json
{"bot_key": "ops-bot", "prompt": "订单服务 5xx 在 10:02 起升高到 3%，排查原因并给出处理建议。", "chat_id": "oc_…", "request_id": "alert-20261125-1002"}
```

## 10. 完整示例

上传两张截图、发起任务、等结果（Python，用到上面的 `sign`）：

```python
import hashlib
import os
import time

import httpx

BASE = "{{COREMAN_BASE_URL}}"
APP_KEY = os.environ["COREMAN_APP_KEY"]
SECRET = os.environ["COREMAN_APP_SECRET"]
DONE = {"succeeded", "waiting_user", "failed", "cancelled"}


def upload(user, path):
    data = open(path, "rb").read()
    fields = {"user": user, "sha256": hashlib.sha256(data).hexdigest()}
    url_path = "/api/infra/tasks/uploads"
    r = httpx.post(
        BASE + url_path,
        data=fields,
        files={"file": (os.path.basename(path), data)},
        headers=sign("POST", url_path, fields, APP_KEY, SECRET),
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["data"]["upload_id"]


def run(user, bot_key, prompt, images, request_id):
    body = {
        "user": user,
        "bot_key": bot_key,
        "prompt": prompt,
        "images": images,
        "request_id": request_id,
    }
    url_path = "/api/infra/tasks/run"
    r = httpx.post(
        BASE + url_path,
        json=body,
        headers=sign("POST", url_path, body, APP_KEY, SECRET),
        timeout=30,
    )
    r.raise_for_status()
    task = r.json()["data"]
    delay = 5
    while task["status"] not in DONE:
        time.sleep(delay)
        delay = min(delay * 2, 30)
        url_path = f"/api/infra/tasks/run/{task['id']}"
        query = {"user": user}
        r = httpx.get(
            BASE + url_path,
            params=query,
            headers=sign("GET", url_path, query, APP_KEY, SECRET),
            timeout=30,
        )
        r.raise_for_status()
        task = r.json()["data"]
    return task


images = [upload("alice", p) for p in ("profile-1.png", "profile-2.png")]
task = run(
    "alice",
    "demo-bot",
    "识别截图里的账号名称、粉丝数和简介，调用资料录入系统的 profiles.update 接口写回到申请单 8812。",
    images,
    "form-8812",
)
print(task["status"], task["reply"] or task["error_message"])
for write in task["writes"]:
    print(write["system"], write["operation"], write["status"])
```

上面的 `upload` 和查询里，签名用的参数分别是表单的文字字段和查询参数；发起任务时是整个 JSON 正文（`images` 按列表展开）。
