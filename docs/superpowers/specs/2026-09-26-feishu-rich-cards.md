# 飞书富卡片输出（Card JSON 2.0 优先）

状态：第 1 期（渲染核心）已实现，第 2、3 期待做
日期：2026-09-26

## 1. 目标与原则

飞书机器人的每一条回复都用飞书卡片 JSON 2.0 呈现，尽量用满卡片的能力，包括图表、表格、指标块、图文、时间线、提示条和彩色标题；卡片表达不了的内容才退回 Markdown。

体验优先，不以开发量为约束：

- **看着舒服**：浅色和深色两种主题、PC 和手机上都清楚、对齐、有层次。
- **推送流畅**：正文逐字出现，图表和表格在生成过程中逐块出现，不在结束时突然整块跳变；多个任务并发时也不卡。
- **永远不丢回答**：任何一步失败都要逐级回退，最差也是一条完整的纯文本消息。

非目标：企业微信和管理台不做同等级的富渲染，富内容块在这两处降级为 Markdown（第 9 节）。

## 2. 飞书能力边界（设计依据）

以下都已按官方文档核对，并在真实飞书客户端验证过。

| 能力 | 结论 |
|---|---|
| 结构 | `schema:"2.0"`，客户端需 7.20+；**遇到未知字段直接报错**，没有 fallback 配置 |
| 体量 | 整卡 30KB、200 个元素；容器最多嵌套 5 层；`element_id` 须字母开头、≤20 字符、全卡唯一 |
| 表格 | `table` 组件每卡最多 5 个、**只能放在根节点**、每页 1–10 行；markdown 内的表格每页固定 5 行，每个组件最多 4 个 |
| 图表 | `chart` 基于 VChart（7.27+ 为 1.12.3，7.20–7.26 为 1.10.1）；不能写函数，`formatter` 字符串模板可用；每卡建议最多 5 个 |
| 圆角底色块 | 只有 `interactive_container` 有圆角和边框；`behaviors:[]` 可用（已实测） |
| 图片 | 只认上传后得到的 `img_key`，没有 URL 写法 |
| 图标 | 只能用图标库的 1154 个 token，线性（`_outlined`）和面性（`_filled`）版本不对称，必须用白名单校验 |
| 深色主题 | 颜色枚举自动切换；写死的颜色不会切换；自定义颜色需要在 `config.style.color` 里成对声明 `light_mode` 和 `dark_mode`；图表不能引用卡片的自定义颜色 |
| 流式 | 打字机效果只对 `markdown` 和 `plain_text` 有效；流式过程中可以新增、替换、删除任何组件；每张卡 10 次/秒；开启 10 分钟后自动关闭，可以再次开启 |

## 3. 总体方案

```
模型输出（GFM Markdown + ```card:类型 JSON 块）
        │
        ▼
coreman/core/richtext        平台无关：切块、校验块 JSON、降级成 Markdown
        │
        ├─► 飞书：gateway_feishu/render   编译成 Card 2.0 组件、白名单、预算、图表
        │         gateway_feishu/transport 流式增量布局、收尾整卡、错误码回退
        │
        └─► 企微 / 管理台 / 其他：degrade → Markdown
```

模型写的内容是唯一真源，库里保存原文；各出口在投递或展示时各自编译。

## 4. 模型输出约定

### 4.1 Markdown

模型按常规 GFM 写作，不需要了解飞书卡片。CoreMan 负责把它转成卡片 markdown（第 6.2 节）。另外允许两种飞书风格的行内标记，降级时去掉标签、保留文字：

- `<font color='green|red|orange|blue|grey'>文字</font>`：只用于表达好坏或状态。
- `<text_tag color='...'>标签</text_tag>`：短状态标签。

### 4.2 富内容块

写法是围栏代码块，info string 为 `card:<类型>`，内容是一个 JSON：

````markdown
```card:kpi
{"items":[{"label":"浏览","value":"1,240"},{"label":"下单","value":"0","good":false}]}
```
````

块里只写**数据和语义**，不写颜色值、尺寸、字号，这些由 CoreMan 决定。JSON 用宽松解析：容忍尾逗号和中文引号误用，解析失败时整块按降级 Markdown 显示并记录告警，不会导致整张卡失败。

| 类型 | 用途 | 主要字段 |
|---|---|---|
| `header` | 卡片标题栏 | `title`，`subtitle?`，`tags?`（最多 3 个，`{text,color}`），`color?`（`blue/green/orange/red/grey/turquoise/...`，语义：完成 green、警告 orange、异常 red） |
| `kpi` | 指标块 | `items[]`：`{label, value, delta?, good?, note?}`；`columns?`（2–4，默认按数量）。`delta` 按 `good` 着色：true 为绿、false 为红、未给为灰；箭头方向取自 `delta` 的符号 |
| `chart` | 图表 | 见 4.3 |
| `table` | 需要精确控制的表格 | `columns[]`：`{key,title,type?,align?}`，`type` 取 `text/number/money/percent/date/person/tag/markdown`；`rows[]`；`page_size?`。普通 GFM 表格也会被自动转换，只有需要列类型或格式时才用它 |
| `callout` | 提示条 | `level`（`info/success/warning/danger`），`title?`，`text`（Markdown） |
| `item` | 左图右文的实体卡（商品、订单、人） | `image?`（URL），`eyebrow?`，`title`，`meta?`（字符串或数组），`code?`，`highlight?`（`{text,level}`），`link?` |
| `timeline` | 时间线或进度 | `steps[]`：`{time?, text, status}`，`status` 取 `done/current/warning/error/pending` |
| `columns` | 并排布局 | `columns[]`，每项是一个子块数组，可以是 `{"type":"markdown","text":...}` 或上面任意类型（`table` 除外） |
| `people` | 人员展示（不发通知） | `users[]`：邮箱或 CoreMan 登录名；解析成平台 ID，解析不了的按名字文本显示 |
| `note` | 口径或脚注 | `text`，渲染为带图标的灰色小字 |
| `actions` | 按钮 | `buttons[]`：`{text, url}`（跳转）或 `{text, reply}`（点击后以点击人的身份把 `reply` 作为下一句话发给机器人）；`style?`（`primary/danger/default`） |
| `raw` | 原生卡片组件（进阶入口） | `elements[]`：Card 2.0 组件原文，按白名单裁剪，给技能作者精确控制用 |

**自动并排**：相邻两个带 `"size":"half"` 的 `chart`、`kpi` 或 `callout` 会自动放进同一行的两栏里，不必专门写 `columns`。

### 4.3 图表 DSL

模型只描述数据和意图，CoreMan 按模板生成 VChart spec。

| 字段 | 说明 |
|---|---|
| `chart` | `line/area/bar/hbar/pie/donut/funnel/combo/progress/ring/radar/scatter` |
| `title` | 渲染为图表上方的小标题（markdown），不用 VChart 的 title |
| `x` + `series[]` | 笛卡尔图：`series[i] = {name, values[], kind?(bar/line), axis?(left/right)}`，`values` 与 `x` 等长，允许 `null` |
| `items[]` | 饼、环、漏斗、排行条形、进度、雷达：`{name, value}` |
| `points[]` | 散点：`{x, y, size?, group?}` |
| `stack` | `none/stack/percent` |
| `format` | `{unit?, decimals?, scale?(auto/none/wan/yi), percent_input?(ratio/percent)}`；`format2` 用于组合图右轴 |
| `labels` | `auto/none/all/last/max` |
| `highlight` | 类目名数组，或 `last`、`max` |
| `sort`、`top_n` | 排行类默认降序、取前 10，其余合并为「其他」 |
| `center` | 环图、KPI 环的中心文字 `{value, caption}` |
| `size` | `full/half` |
| `summary` | 图下方的一句结论 |

### 4.4 流式期间的书写要求（写进提示词）

- 块要一次写完整个 JSON，不要在块内来回修改。
- 先写结论或说明文字，再写块。块出现时，前文已经定稿。

## 5. 视觉规范（深浅双主题）

**卡片级**

- `config`：`update_multi:true`、`width_mode:"fill"`、`summary`（首句结论的纯文本，用于消息列表预览）。
- 正文留白 `padding:"14px 16px 16px 16px"`，块间距 `vertical_spacing:"10px"`。

**配色**

- 文字和图标只用颜色枚举里不带数字的色相名（等于 600 档，深色下自动变亮），语义固定：成功 green、警告 orange、错误 red、信息 blue、次要 grey。
- 底色块只用两种：
  - 中性面板：自定义色 `cus-panel`，light 为 `rgba(31,35,41,0.04)`，dark 为 `rgba(255,255,255,0.06)`。半透明，在浅色主题下比底色略深、在深色主题下比底色略浅，不依赖卡片底色。
  - 语义提示条：`-50` 档底色，加 `-200` 档边框（例如 `orange-50` 配 `orange-200`）。
- 禁止在代码里写死只适合一种主题的颜色。自定义颜色必须 light 和 dark 成对，并且两个值不同；单元测试检查这一条。

**字号**：只用四档。KPI 数字 `heading`，小节标题 `heading-4`，正文 `normal`，口径和脚注 `notation` 配 grey。

**圆角**：面板、指标块、提示条统一 8px，用 `interactive_container`（`behaviors:[]`）实现。

**图表**（以下几条都已在真机的浅色和深色主题下验证）

- 不写任何文字、轴、网格、图例、底轨的颜色，交给飞书主题。
- `background:"transparent"`；label 和 point 一律 `lineWidth:0`。
- 设置 `hover.enable:false` 和 `select.enable:false`，去掉悬停和选中时出现的描边残留。
- 数据标签默认取柱子或线的颜色，浅色图元在白底上、深色图元在深色背景上都会看不清。所以标签颜色写进 `chart_spec.theme`，用语义色解析，会跟着主题切换：
  - 数值标签：`{"type":"palette","key":"primaryFontColor"}`（浅色主题下是深色字，深色主题下是白字）；
  - 次要标签：`axisLabelFontColor`。
  - 示例：`theme.series.bar.label.style.fill`。
- 数据色默认交给 `color_theme:"brand"`。需要语义色时，从一组两种主题下都可读的中明度色里取，例如青 `#169C89`；不用 600 档以上的深色。
- 高度固定：全宽趋势图 240px，饼、环、雷达 220px，排行条形按 `32×N+24`，半宽 180px。

**图标**：白名单常量，语义映射固定，例如完成 `succeed_filled`、警告 `warning_outlined`、信息 `info_outlined`、时间 `time_outlined`。

## 6. 飞书编译器（`gateway_feishu/render`）

### 6.1 切块

用 `markdown-it-py`（新依赖，纯 Python，MIT 协议）按顶层块解析，得到有序的单元（unit）：

| 单元 | 来源 | 渲染 |
|---|---|---|
| `text` | 相邻的段落、标题、列表、引用、代码块合并 | 一个 `markdown` 组件 |
| `table` | GFM 表格 | 满足任一条件就转 `table` 组件：超过 5 行、有数字列、有列对齐、同段表格超过 4 个；否则留在 `markdown` |
| `image` | 独占一段的图片；连续多张 | `img`；多张用 `img_combination` |
| `hr` | 分割线 | `hr` |
| `block` | 完整的 `card:` 块 | 对应的组件组 |
| `pending` | 未闭合的 `card:` 块（只在流式时出现） | 占位，例如「📊 正在生成图表…」 |

### 6.2 Markdown 规范化

卡片 markdown 与 GFM 的 26 处差异，按调研逐条处理，主要有：

- 列表统一用 `-`，缩进改成每层 4 空格。
- 不在列表里、却缩进 4 格以上的普通文字去掉缩进，避免被当成代码块。
- 代码块语言别名映射，例如 py→python、ts→typescript、sh→bash、yml→yaml；认不出的语言去掉标记；`~~~` 改成 ```` ``` ````。
- 不在白名单里的 HTML 标签和裸 `<` 转成实体。
- 只保留 http 和 https 链接，其他降级为文字；裸 URL 包成链接。
- `***`、`___` 分割线改成单独一行的 `<hr>`。
- 默认去掉 `<at id=all>`：群主没有开启权限时，整张卡片会发送失败。

### 6.3 预算与合法性

发送前统一执行：

- 字段白名单（按组件的 schema 裁剪）、图标白名单、颜色 token 白名单。
- 自动生成 `element_id`：类型缩写加序号，保证全卡唯一。
- 容器嵌套不超过 5 层。
- 计数：元素 ≤200、表格 ≤5、图表 ≤5、JSON ≤28KB（留出余量）。超出时，把尾部单元移到「续卡」，也就是紧接着发一张同样风格的新卡片；图表超过 5 个时，多余的降级为数据表。

### 6.4 回退层级

同一份回答按下面的顺序尝试，前一级失败才用下一级：

1. **富卡片**：完整编译结果。
2. **简化卡片**：所有块都降级成 Markdown，只保留 `markdown`、`hr`、`img` 组件。
3. **post 消息**：纯文本（现有路径）。

按错误码决定是降级还是重试：

| 情况 | 错误码 | 处理 |
|---|---|---|
| 内容问题 | 200220、10002、300305、200860 | 立即降一级 |
| 实体失效 | 200740、200750、300311 | 立即新发一条消息 |
| 流式已关闭 | 200850、300309 | 重新开启流式后重试 |
| 回调进行中 | 200810 | 短暂退避，不计入失败次数 |
| 序号落后 | 300317 | 把序号抬高后重试 |

## 7. 流式投递（`gateway_feishu/transport`）

### 7.1 增量布局

`feishu_deliveries` 新增 `layout` 列（JSONB），记录卡片当前的单元列表：`[{uid, kind, element_ids, hash, text}]`。

每次推送对 `pending_text` 做一次编译，与 `layout` 比较：

- **最后一个 `text` 单元的文字增长了**：调用流式更新文本接口，得到打字机效果。
- **出现新单元**：用一次 `batch_update`（`add_elements insert_after`），插入新单元的组件和其后新的空文本段。
- **`pending` 变成 `block`**，或 markdown 表格变成 `table` 组件：在同一个 `batch_update` 里删除旧组件、插入新组件。
- `card:` 块和表格在闭合前不会出现半成品。块闭合的那一刻整块出现，前面的文字不会再被改写。

### 7.2 打字观感

- 打字机效果要求旧文本是新文本的前缀，所以不在流式期间改写已发出的文字：
  - 首帧不放「…」占位，第一次写入正文前，只显示思考面板；
  - 远程图片直到图片单元闭合、上传完成后，才作为独立的 `img` 单元插入；
  - 脱敏替换只作用于新增部分（需要 worker 侧保证替换稳定）。
- 调整 `streaming_config`：`print_frequency_ms` 约 30ms、`print_step` 2，让显示速度跟得上模型输出，避免积压后一次性跳出一大段。最终参数通过真机调参确定。
- 思考面板只在内容变化时推送；标题动画维持 3 秒一帧。

### 7.3 频率与并发

- 现在是每个机器人串行、调用间隔 0.3 秒，并发 N 个任务时，每张卡要约 0.6N 秒才更新一次。
- 改为：
  - 每张卡一个令牌桶，≤8 次/秒；
  - 每个应用、每个接口一个令牌桶，≤40 次/秒；
  - 不同卡片并行推送，同一张卡严格串行，`sequence` 保持严格递增。
- 同一张卡的文字更新合并到约 300ms 一次；结构变更（插入块）立即发送。

### 7.4 收尾

1. 用 `final_text` 做完整编译：图片上传、标题栏、`summary`。
2. 一次 `PATCH settings`：关闭流式，写入 `summary`。
3. 一次整卡 `PUT`：得到最终卡片。流式布局与最终布局一致时，用户看不到跳变。
4. 失败时按 6.4 逐级回退。回退时，把原卡的正文替换成「完整回答见下方消息」，避免内容重复或卡片停在半截。

### 7.5 长任务

流式模式开启 10 分钟后自动关闭。到 9 分钟时再次开启并重置计时，继续保持打字机效果，不再改成每次整卡 PUT。

## 8. 交互（`actions` 块）

- **`url` 按钮**：用 `open_url`。
- **`reply` 按钮**：
  - 点击后走 `callback`，value 带 `{task_id, reply_id}`；
  - 网关把它当作点击人发来的下一句话，交给同一会话；
  - 回调立即返回 toast「已发送」。
- **收尾后禁用**：点击过的按钮组在收尾后标记为已选，也就是按钮置灰并写明谁点了哪一个。流式期间收到回调时，先关闭流式模式再更新卡片（飞书要求）。
- **危险操作确认**：继续使用现有的 AskUserQuestion 选择卡片。单题且选项不超过 4 个时，改用一排按钮，点一下就提交，不再需要「下拉框 + 提交」。

## 9. 其他出口

- **`coreman/core/richtext/degrade.py`**：把块转成可读的 Markdown。
  - 图表：标题 + 数据表（≤12 行）+ 结论。
  - 指标块：`**标签**：值（变化）`。
  - 提示条：引用块 + emoji。
  - 实体卡：粗体标题 + 元信息。
  - 时间线：带状态 emoji 的列表。
  - 按钮：链接，或「回复：xxx」。
  - `raw`：去掉。
- **企业微信**：推送前降级。
- **管理台聊天记录**：API 返回时降级，前端不改。
- **机器人之间协作**：转交给另一个 AI 员工的文本保留原文，对方能直接读 JSON。
- **飞书出站队列里的 Markdown 消息**：包括溢出段、定时任务推送、欢迎语，改用静态富卡片发送，失败再退回 post。

## 10. 提示词与配置

- **新增提示词段**：在 system prompt 的「本轮附加能力」（⑩）里加一段「飞书富卡片输出」，只在平台为飞书且启用时挂上：块语法、类型速查、两三个短例、4.4 的书写要求，约 800 token。
- **每个 AI 员工一个开关**：`bots.rich_cards`，默认开启，在管理台编辑页「更多设置」里切换。关闭后回到现在的单 markdown 卡片。

## 11. 验证

- **单元测试**：
  - 切块和规范化：26 条差异各一例，外加流式截断在任意位置的情况。
  - 每种块的渲染与降级，使用 golden JSON。
  - 图表 DSL 到 VChart：文字、轴、底轨不带颜色，`lineWidth:0`，背景透明。
  - 预算与续卡、白名单裁剪、自定义颜色成对且两个值不同。
- **性质测试（hypothesis）**：
  - 任意 Markdown 加块，编译结果都满足上限、`element_id` 唯一、不含未知字段。
  - 流式前缀序列的布局差异只包含「追加」和「最后一个单元的变化」。
- **传输测试**：用假的飞书服务端验证以下几点：
  - 增量布局的调用序列；
  - 频率上限；
  - 错误码分支；
  - 回退后不重复发送内容。
- **真机回归**：新增 `coreman feishu card-preview` 命令，把一组样例卡片发给指定用户。每次改动视觉后，在深浅两种主题、PC 和手机上各看一次。

## 12. 分期交付（每期一个 PR）

1. **渲染核心**：`richtext` 切块、校验和降级；飞书编译器，包括全部块、图表、白名单和预算；收尾整卡与回退层级；出站 Markdown 改用卡片；企微和管理台降级；提示词段与开关；预览命令。
2. **流畅推送**：增量布局；按卡、按应用限流与并行推送；打字参数；思考面板去重；长任务续期；`summary`；错误码分流。
3. **交互**：`actions` 按钮与回复回调；AskUserQuestion 单题按钮化；`people` 解析。

## 12.1 第 1 期实现记录

- 流式阶段仍是单个 markdown 组件（第 2 期再做增量布局）：写完的块显示成降级 Markdown，没写完的显示占位，收尾时整卡替换。
- 卡片外壳（宽度、留白、面板色）流式与终稿一致；「思考过程」面板移到 `coreman/core/feishu_cards/thinking.py`，网关与预览命令共用。
- 回复按钮（`reply`）和 `people` 已在模型里定义，但要等第 3 期接上卡片回调和人员解析：提示词里不写，渲染时回复按钮默认不出现（`RenderContext.allow_reply`）。
- 容错：
  - 块 JSON 里的 NaN、Infinity、超出 1e15 的数和孤立代理字符，解析时一律清掉；
  - 单个块渲染出错只降级这一块，编译器自身出错时整条改用简化卡；
  - 投递循环和出站队列把任何异常都计入失败次数，不会让一条坏回复卡住整个机器人。
- 每张卡都带一份 Markdown 降级文本：
  - 续卡或出站消息里的某一张被拒，只把那一张改发成普通消息；
  - 简化卡放不下就分成几张，不再截断。
- 超大表格按行拆成几张同表头的表；超长代码块在行边界拆开并补齐围栏。
- 编译放到线程里执行，不占网关事件循环。

## 13. 待真机确认

- `sequence` 能否跳号；`batch_update` 是否原子。
- markdown 的 `content` 能否为空串，用于新段占位；不行就用零宽字符。
- 不设 `summary` 时，关闭流式后消息预览显示哪段内容。
- 流式打字参数的最佳值。
