# 飞书富卡片回复

飞书 AI 员工的回复用飞书卡片 JSON 2.0 呈现：除了 Markdown 正文，还能显示图表、表格、指标块、图文、时间线、提示条和彩色标题栏，浅色和深色主题下都清楚。卡片不支持的内容才退回 Markdown。

## 开关

AI 员工编辑页 →「更多设置」→ 体验 →「富卡片回复」，只对飞书 AI 员工显示，默认开启。

- 开启：system prompt 的「本轮附加能力」段落里多一段块语法说明（约 800 token），回复结束时编译成富卡片。
- 关闭：不挂说明段，回复仍是一张卡片，但块全部显示成普通 Markdown。

需要数据库迁移 `0049`（`bots.rich_cards`，默认 `true`）和 `0050`（`feishu_deliveries.layout`，流式布局）。

## 写法

模型照常写 GFM Markdown。需要富内容时，写一个 info string 为 `card:<类型>` 的围栏代码块，内容是一个 JSON 对象：

````markdown
订单比上周多 12%，退款率略升。

```card:kpi
{"items": [{"label": "订单", "value": 97, "delta": "+12.6%", "good": true},
           {"label": "退款率", "value": "2.1%", "delta": "+0.4pt", "good": false}]}
```
````

块里只写数据和语义；颜色、尺寸、字号、深浅主题适配都由平台决定。JSON 解析是宽松的（尾逗号、中文引号能纠正）；格式不对的块按原文显示成代码块，不会让整条回复失败。字段以 `coreman/core/richtext/schema.py` 为准。

| 类型 | 用途 | 主要字段 |
|---|---|---|
| `header` | 卡片标题栏（放在最前面，只取第一个） | `title`、`subtitle`、`tags[{text,color}]`、`color`（green 完成、orange 警告、red 异常） |
| `kpi` | 指标块，一行 2–4 个，手机上自动 2×2 | `items[{label,value,delta,good,note}]`、`columns`、`size` |
| `chart` | 图表 | 见下文 |
| `table` | 需要列类型时的表格（普通 GFM 表格会自动转换） | `columns[{key,title,type,align}]`、`rows`、`page_size`、`title` |
| `callout` | 提示条 | `level`（info / success / warning / danger）、`title`、`text` |
| `item` | 左图右文的实体卡（商品、订单、人） | `title`、`image`、`eyebrow`、`meta[]`、`code`、`highlight`、`tags`、`link` |
| `timeline` | 时间线或进度 | `steps[{time,text,status}]`，status 为 done / current / warning / error / pending |
| `columns` | 2–3 栏并排 | `columns[[子块…]]`，子块为 `{"type":"markdown","text":…}` 或带 `type` 的 kpi / chart / callout / item / timeline / note |
| `note` | 口径、数据来源等脚注 | `text` |
| `actions` | 链接按钮 | `buttons[{text,url,style}]` |
| `raw` | 原生卡片组件（给技能作者精确控制） | `elements[]`（按字段白名单裁剪）、`fallback`（其他出口显示的文字） |

`people`（人员展示）和按钮的 `reply`（点击后把一句话作为点击人的下一条消息发回）已在模型里定义，要等后续版本接上人员解析和卡片回调后才会开放，目前不写进提示词，渲染时也不显示回复按钮。

相邻两个带 `"size": "half"` 的 `chart`、`kpi` 或 `callout` 会自动并排成一行。

### 图表

`chart` 取 line、area、bar、hbar、pie、donut、funnel、combo、progress、ring、radar、scatter。数据三选一：

- `x` + `series[{name, values, kind, axis}]`：折线、面积、柱状、组合、雷达。
- `items[{name, value}]`：饼、环、漏斗、排行条形、进度条、环形进度。
- `points[{x, y, size, group}]`：散点。

可选：`format`（`unit`、`prefix`、`decimals`、`scale` 为 auto/wan/yi、`percent_input` 为 ratio/percent）、`labels`（auto/none/all/last/max）、`highlight`（last/max/类目名）、`stack`、`top_n`、`center`（环图中心文字）、`summary`（图下结论）、`size`（full/half）。

图表按模板生成 VChart spec：不写任何文字、坐标轴、网格、底轨的颜色，交给飞书的深浅主题；数值标签的颜色用主题语义色；数据色取在浅色、深色背景上都清楚的中明度色。

## 投递

- **流式阶段**：
  - 正文逐字出现，打字速度约 50 字/秒（每 40 毫秒 2 个字），跟得上模型的输出；
  - 块写完的那一刻，整块插进卡片（图表、表格、指标块等和最终效果一致）；正在写的块显示「📊 正在生成图表…」这类占位；
  - 两个半宽块凑齐后自动并排；
  - 标题栏要整卡更新才能改，所以流式期间不显示，收尾时出现。
  - 实现上，每次推送把正文算成一串单元，和卡片上记下的布局（`feishu_deliveries.layout`）比较：最后一段正文变长只调流式文本接口；有块写完或占位换成块，用一次 `batch_update` 删掉变化起点之后的旧组件、插入新组件。卡片和布局对不上时整卡重建，流式模式保持开启。
  - 思考面板内容没变就不推。同一个 AI 员工的平台调用间隔 0.125 秒（原来 0.3 秒），几个回复同时进行时每张卡大约 1 秒刷新一次。
  - 流式模式开启 10 分钟后飞书会自动关闭：到 9 分钟时主动续开，被关掉了就重新开启后重试，长回答全程保持打字效果（原来 9 分钟后改成每次整卡替换）。
  - 流式期间整卡体积超过约 26KB 或组件超过 170 个时暂停加长和插入；飞书拒收某次更新时也暂停增量、只更新思考面板。收尾时整卡替换成完整内容。
- **收尾**：
  1. 编译整条回复。编译时正文图片照旧上传并内嵌，另起一行保留「查看原图」链接；实体卡的图片也会上传。
  2. 关闭流式模式，同时写入消息列表的预览文案（第一句结论或标题栏）。
  3. 整卡替换成富卡片。
- **续卡**：一张卡放不下（30KB、200 个元素）时，其余内容作为续卡紧接着发出；表格组件每卡最多 5 个，图表最多 5 个，超出的降级成数据表。
- **回退**：
  - 飞书拒绝富卡片（`200220`、`10002`、`200860`、`300305` 等）时，原地换成只有 Markdown 的简化卡；
  - 流式卡片本身发不出去时，照旧改发普通消息。
- **出站队列**：定时任务结果等出站 Markdown 消息也优先发卡片，被拒再退回 post。飞书的定时任务结果不再按字节切分，由网关按卡片预算分卡，避免把一个块切成两半。

企业微信和管理台的聊天记录不渲染卡片：含块的回复在这两处显示为降级后的 Markdown，例如图表显示成数据表、指标块显示成列表、提示条显示成引用。

## 真机预览

每次调整视觉后，用预览命令把样例发给自己，在浅色和深色主题、PC 和手机上各看一次：

```sh
coreman feishu-cards preview --to <登录名> [--bot <AI 员工标识>] [--sample daily|item|repricing|logistics|inspection] [--file reply.md] [--stream] [--apply]
```

不加 `--apply` 只调 CardKit 建卡片实体做校验，不发消息。`--stream` 让第一张卡按线上流程先流式打字、再整卡替换。样例在 `coreman/core/feishu_cards/samples.py`。
