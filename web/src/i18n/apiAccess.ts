// 「API 令牌」页与 AI 员工详情里的「API 驱动」面板。避免在文案里写 @ { } |：vue-i18n 会当成语法。
export const apiAccessZh = {
  tokens: {
    intro: '外部程序（自己的脚本、CI、工单系统）可以用 API 令牌以你的身份给 AI 员工下任务。任务在你和 AI 员工的私聊里，或者绑定了别名的群里承接：会话里能看到「通过 API 发起」和回复，你可以接着在飞书里往下推进。令牌只能驱动你选的 AI 员工，而且对方要开启「允许 API 驱动」。',
    create: '新建令牌', refresh: '刷新', empty: '还没有 API 令牌。',
    name: '名称', namePlaceholder: '例如：工单系统', prefix: '前缀', bots: '可驱动的 AI 员工', days: '有效期（天）',
    status: '状态', expiresAt: '到期', lastUsedAt: '最近使用', never: '未使用', createdAt: '创建时间',
    statuses: { active: '有效', expired: '已过期', revoked: '已吊销' },
    revoke: '吊销', revokeConfirm: '吊销「{name}」？吊销后立即失效，排队中的任务也不会再执行。', revoked: '已吊销',
    apiOff: '未开启 API 驱动', pickBots: '请至少选择一个 AI 员工', noBots: '没有可选的 AI 员工：只能选择你能使用的飞书 AI 员工。',
    created: '令牌已创建', createdHint: '请现在复制保存，关闭后无法再次查看。', copy: '复制', copied: '已复制',
    usageTitle: '调用方法', usage: '用 POST /api/infra/tasks 发起，请求头 Authorization 写 Bearer 加令牌；GET /api/infra/tasks/任务 id 查询结果。不传 conversation 时在你和 AI 员工的私聊里承接（需要先私聊过它一次）；要在群里承接，先在群里对它发「/api 绑定 别名」，调用时 conversation 写 target 为 chat、alias 为别名。',
    cancel: '取消', confirm: '确定', close: '关闭',
  },
  bot: {
    tab: 'API 驱动',
    intro: '开启后，成员可以用自己的 API 令牌从外部程序给此 AI 员工下任务。任务以令牌所有人的身份运行，权限和他本人在飞书里找此 AI 员工时一样；任务在他和此 AI 员工的私聊里，或者下面绑定了别名的群里承接。',
    enabled: '允许 API 驱动', enabledHint: '关闭后新的调用一律被拒，排队中的 API 任务不再执行；正在执行的会跑完。',
    aliases: '群别名', aliasesHint: '群成员在群里对此 AI 员工发「/api 绑定 别名」即可绑定，发「/api 解绑」解除。调用方用别名指定在哪个群承接，发起人必须是群成员。',
    alias: '别名', chatId: '群 chat_id', createdBy: '绑定人', createdAt: '绑定时间',
    empty: '还没有群别名。', remove: '解绑', removeConfirm: '解绑别名「{alias}」？之后不能再用它在这个群发起任务。',
    noAccess: '仅此 AI 员工的管理者可以查看和配置 API 驱动。', platform: '目前只有飞书 AI 员工支持 API 驱动。',
    loading: '正在加载…', retry: '重试', cancel: '取消', confirm: '确定',
  },
}

export const apiAccessEn: typeof apiAccessZh = {
  tokens: {
    intro: 'External programs (your scripts, CI, ticketing systems) can use an API token to give AI employees tasks as you. Tasks run in your direct chat with the employee, or in a group that has an alias: the chat shows "started via API" and the reply, and you can keep going in Feishu. A token can drive only the employees you choose, and they must have "Allow API driving" on.',
    create: 'New token', refresh: 'Refresh', empty: 'No API tokens yet.',
    name: 'Name', namePlaceholder: 'For example: ticketing system', prefix: 'Prefix', bots: 'AI employees it can drive', days: 'Valid for (days)',
    status: 'Status', expiresAt: 'Expires', lastUsedAt: 'Last used', never: 'Never', createdAt: 'Created',
    statuses: { active: 'Active', expired: 'Expired', revoked: 'Revoked' },
    revoke: 'Revoke', revokeConfirm: 'Revoke "{name}"? It stops working immediately and queued tasks will not run.', revoked: 'Revoked',
    apiOff: 'API driving off', pickBots: 'Choose at least one AI employee', noBots: 'No AI employees to choose: only Feishu employees you can use are listed.',
    created: 'Token created', createdHint: 'Copy it now. It cannot be shown again after you close this.', copy: 'Copy', copied: 'Copied',
    usageTitle: 'How to call', usage: 'Start a task with POST /api/infra/tasks and an Authorization header of Bearer plus the token; read the result with GET /api/infra/tasks/task id. Without conversation the task runs in your direct chat with the employee (you must have chatted with it once). To run in a group, first send "/api bind alias" to the employee in that group, then pass conversation with target chat and that alias.',
    cancel: 'Cancel', confirm: 'Confirm', close: 'Close',
  },
  bot: {
    tab: 'API driving',
    intro: 'When on, members can give this employee tasks from external programs with their own API tokens. Tasks run as the token owner, with the same permissions as when they ask this employee in Feishu, in their direct chat with it or in a group with an alias below.',
    enabled: 'Allow API driving', enabledHint: 'When off, new calls are refused and queued API tasks will not run; running ones finish.',
    aliases: 'Group aliases', aliasesHint: 'Group members bind an alias by sending "/api bind alias" to this employee in the group, and "/api unbind" to remove it. Callers name the group by alias, and the requester must be a member.',
    alias: 'Alias', chatId: 'Group chat_id', createdBy: 'Bound by', createdAt: 'Bound at',
    empty: 'No group aliases yet.', remove: 'Unbind', removeConfirm: 'Unbind alias "{alias}"? It can no longer be used to start tasks in that group.',
    noAccess: 'Only managers of this employee can view and configure API driving.', platform: 'Only Feishu employees support API driving for now.',
    loading: 'Loading…', retry: 'Retry', cancel: 'Cancel', confirm: 'Confirm',
  },
}

export const apiAccessJa: typeof apiAccessZh = {
  tokens: {
    intro: '外部プログラム（自分のスクリプト、CI、チケットシステム）は API トークンを使って、あなたとして AI 社員にタスクを依頼できます。タスクはあなたと AI 社員の個別チャット、またはエイリアスを設定したグループで実行され、「API から開始」と返信が表示されるので、そのまま Feishu で続けられます。トークンで操作できるのは選んだ AI 社員だけで、相手側で「API からの操作を許可」が有効である必要があります。',
    create: 'トークンを作成', refresh: '更新', empty: 'API トークンはまだありません。',
    name: '名前', namePlaceholder: '例：チケットシステム', prefix: 'プレフィックス', bots: '操作できる AI 社員', days: '有効期間（日）',
    status: '状態', expiresAt: '有効期限', lastUsedAt: '最終使用', never: '未使用', createdAt: '作成日時',
    statuses: { active: '有効', expired: '期限切れ', revoked: '失効済み' },
    revoke: '失効', revokeConfirm: '「{name}」を失効させますか？すぐに使えなくなり、待機中のタスクも実行されません。', revoked: '失効しました',
    apiOff: 'API 操作がオフ', pickBots: 'AI 社員を 1 つ以上選んでください', noBots: '選べる AI 社員がいません。利用できる Feishu の AI 社員のみ表示されます。',
    created: 'トークンを作成しました', createdHint: '今すぐコピーしてください。閉じると二度と表示できません。', copy: 'コピー', copied: 'コピーしました',
    usageTitle: '呼び出し方', usage: 'POST /api/infra/tasks で開始し、Authorization ヘッダーに Bearer とトークンを指定します。結果は GET /api/infra/tasks/タスク id で取得します。conversation を省略すると AI 社員との個別チャットで実行されます（事前に一度チャットしている必要があります）。グループで実行するには、そのグループで AI 社員に「/api 绑定 エイリアス」を送ってから、conversation に target chat とエイリアスを指定します。',
    cancel: 'キャンセル', confirm: '確認', close: '閉じる',
  },
  bot: {
    tab: 'API 操作',
    intro: 'オンにすると、メンバーは自分の API トークンを使って外部プログラムからこの AI 社員にタスクを依頼できます。タスクはトークン所有者として、本人が Feishu で依頼するときと同じ権限で、本人との個別チャットまたは下のエイリアスを設定したグループで実行されます。',
    enabled: 'API からの操作を許可', enabledHint: 'オフにすると新しい呼び出しは拒否され、待機中の API タスクは実行されません。実行中のものは最後まで実行されます。',
    aliases: 'グループエイリアス', aliasesHint: 'グループメンバーがグループ内でこの AI 社員に「/api 绑定 エイリアス」を送ると設定され、「/api 解绑」で解除されます。呼び出し側はエイリアスでグループを指定し、依頼者はそのグループのメンバーである必要があります。',
    alias: 'エイリアス', chatId: 'グループ chat_id', createdBy: '設定者', createdAt: '設定日時',
    empty: 'グループエイリアスはまだありません。', remove: '解除', removeConfirm: 'エイリアス「{alias}」を解除しますか？以後そのグループでタスクを開始できなくなります。',
    noAccess: 'この AI 社員の管理者のみが API 操作を表示・設定できます。', platform: '現在 API 操作に対応しているのは Feishu の AI 社員のみです。',
    loading: '読み込み中…', retry: '再試行', cancel: 'キャンセル', confirm: '確認',
  },
}
