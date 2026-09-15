"""用户可见文案（spec §13）。代码里不得散落硬编码提示；测试从这里取期望值。"""

from __future__ import annotations

HELP_ZH = """📖 **命令列表**

🔄 **会话管理:**
• `reset` / `new` / `clear` / `重置` / `清空` - 重置当前会话，开始新对话
• `stop` / `停止` / `暂停` / `停` - 中断正在进行的任务

❓ **帮助:**
• `help` / `帮助` / `?` / `？` - 显示此命令列表

💬 除以上命令外，直接输入任何内容即可与 AI 对话"""

HELP_JA = """📖 **コマンド一覧**

🔄 **セッション管理:**
• `reset` / `new` / `clear` / `重置` / `清空` - 現在のセッションをリセットして新しい会話を始める
• `stop` / `停止` / `暂停` / `停` - 実行中のタスクを中断する

❓ **ヘルプ:**
• `help` / `帮助` / `?` / `？` - このコマンド一覧を表示

💬 上記以外の入力はそのまま AI との会話になります"""

MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "session_reset_ok": "✅ 会话已重置，开始新的对话。",
        "session_reset_failed": "⚠️ 会话重置失败，请稍后重试。",
        "stopped": "⏹ 已停止当前任务。",
        "nothing_running": "没有正在运行的任务。",
        "task_stopped_suffix": "\n\n⏹ 任务已被用户停止。",
        "superseded_suffix": "\n\n⏹ 已收到新消息，上一个任务自动停止。",
        "help": HELP_ZH,
        "no_permission": "⚠️ 抱歉，您没有使用此机器人的权限。\n\n如需开通权限，请联系管理员。",
        "unsupported_message": "暂不支持该类型的消息，请发送文字。",
        "ask_user_unsupported": (
            "AI 需要向您提问确认，该交互功能将在下一版本开放；请换一种更明确的表述重新发送。"
        ),
        # 保留 ask_user_unsupported：提问轮已经开放（见 chat_handler._open_choice），
        # 但降级路径（平台不支持卡片）随时可能要用回它，删了就得重新翻一遍。
        "ask_user_invalid": "AI 发起了提问但内容无效，请换一种表述重新发送。",
        "choice_cancelled": "已取消选择。",
        "answer_recorded": "✓ 已记录回答：{answer}",
        "choice_generating": "⏳ 正在生成结果，完成后自动推送…",
        # 提交轮（choice_submit）：答案送回模型的那一轮全程主动推送，没有 stream 撑场面。
        "submit_heartbeat": "⏳ AI 仍在处理中（已耗时 {seconds} 秒），请稍候…",
        "choice_duplicate": (
            "🔄 AI 正在处理您之前提交的答案，请稍候。若长时间无响应可发送「取消」重置。"
        ),
        "choice_config_changed": "机器人配置已变更，之前的提问已失效，请重新发送消息。",
        "choice_submit_done": "已收到您的选择，处理完成。",
        # 限流切换卡片（spec §8.8）点完之后回给用户的告知卡：标题进 main_title.title，
        # 正文进 sub_title_text。卡面其余字样（选项、按钮）在 cards.py 里。
        "rl_expired_title": "⏰ 已过期",
        "rl_expired_desc": "卡片已过期（30 分钟有效），请重新触发",
        "rl_replaced_title": "ℹ️ 已被新卡片替代",
        "rl_replaced_desc": "您已收到更新的切换提示，请处理新卡片",
        "rl_no_option_title": "⚠️ 未选择选项",
        "rl_no_option_desc": "请重新选择",
        "rl_wait_title": "⏳ 已选择继续等待",
        "rl_wait_desc": "继续使用 **{current}** 等待额度恢复。",
        "rl_switching_title": "🔄 正在切换…",
        "rl_switching_desc": "正在将 **{current}** 切到 **{target}**，请稍候。",
        "rl_unknown_title": "⚠️ 未识别的选项",
        "rl_unknown_desc": "option={option}",
        # 模板卡片的卡面文字（cards.py 渲染）：zh 默认文案。
        "card_source_ai": "AI 助手",
        "card_source_dispatch": "服务调度",
        "card_source_ai_dispatch": "AI 助手 · 服务调度",
        "card_q_prefix": "❓ 问题",
        "card_q_single": "❓ 请选择",
        "card_option_fallback": "选项 {n}",
        "card_option_other": "其他（发消息输入）",
        "card_submit_choice": "确认选择",
        "card_answered_prefix": "✅ 已答 · 问题",
        "card_answered_single": "✅ 已回答",
        "card_answered_sub": "您的回答：{answer}",
        # 一个选项都没勾就提交时写进回执卡、也写进送回模型的答案里。
        "card_no_selection": "(未选择)",
        "card_waiting_title": "✏️ 请输入您的答案",
        "card_waiting_option": "等待输入中，请直接发送消息",
        "card_waiting_submit": "等待输入",
        "card_expired_title": "已过期",
        "card_expired_desc": "选择会话已过期，请重新发送消息",
        "brief_q_prefix": "**问题 {index}/{total}**",
        "brief_q_single": "**请选择**",
        "brief_option_other": "- **其他** — 直接发送文字作为回答",
        # 答完一轮后回给模型的文本，不是给用户看的：ja 与 zh 同文（理由同 media_prompt_*）。
        "answers_header": "[用户选择回答]",
        "answers_unanswered": "(未回答)",
        # 限流切换卡（switch_offer_card）的卡面文字。
        "rl_offer_title": "⚠️ 当前运行时已触发额度限制",
        "rl_offer_desc": (
            "运行时 **{current}** 已耗尽，是否切换到周额度更空闲的运行时 "
            "**{target}**（7天使用率 {pct}）？"
        ),
        "rl_offer_opt_switch": "切换到 {target}",
        "rl_offer_opt_wait": "保持 {current} 等待额度恢复",
        "rl_offer_submit": "确定",
        "rl_pct_unknown": "未知",
        # 额度表、倒计时与预警（rate_limit.py）：zh 默认文案。
        "rl_reset_done": "已重置",
        "rl_later": "稍后",
        "rl_ago_just_now": "刚刚",
        "rl_ago_minutes": "{n} 分钟前",
        "rl_ago_hours": "{h}h{m}m 前",
        "rl_quota_title": "\n\n---\n**📊 服务器额度总览**\n",
        "rl_quota_head": "| 运行时 | 模型 | 5小时额度 | 7天额度 |",
        "rl_quota_probed": "↳采集于 {ago}",
        "rl_quota_footer": "_📡 使用率每 15 分钟采集一次，倒计时按当前时间实时推算。_",
        "rl_probed_note": "（采集于 {ago}）",
        "rl_warn_both": (
            "\n\n---\n"
            "⚠️ **额度提醒**：本运行时额度即将耗尽{note}\n"
            "- 5 小时额度：{p5}%（约 {reset5} 后重置）\n"
            "- 7 天额度：{p7}%（约 {reset7} 后重置）"
        ),
        "rl_warn_5h": (
            "\n\n---\n⚠️ **额度提醒**：本运行时 5 小时额度已用 {pct}%{note}，约 **{reset}** 后重置"
        ),
        "rl_warn_7d": (
            "\n\n---\n⚠️ **额度提醒**：本运行时 7 天额度已用 {pct}%{note}，约 **{reset}** 后重置"
        ),
        # 切换执行完（relay_switch 任务）之后发回聊天的回执，不是卡片。
        "relay_switch_ok": (
            "✅ **已切换运行时**\n\n"
            "- 来源：{current}\n"
            "- 目标：{target}\n"
            "- 详情：{detail}\n\n"
            "_提示：会话上下文（session）随运行时切换被重置，下一条消息会开新会话。_"
        ),
        "relay_switch_failed": (
            "❌ **切换运行时失败**\n\n"
            "- 来源：{current}\n"
            "- 目标：{target}\n"
            "- 原因：{detail}\n\n"
            "_可去管理台手动处理，或稍后重试。_"
        ),
        "relay_switch_forbidden": "权限不足，您可能已被移出该机器人的管理员",
        "relay_switch_detail": "模型：{model}",
        "relay_error": "抱歉，AI 连接出现错误（运行时 {relay}），请稍后再试。",
        "runtime_busy": "抱歉，运行时 {relay} 正忙，排队等待超时仍未开始处理，请稍后再试。",
        "relay_error_text": "⚠️ AI 服务返回了错误，请稍后重试。",
        "empty_stream": (
            "⚠️ AI 服务返回了空回复，可能是服务瞬时异常，请重试。"
            "若多次失败请联系管理员。📎 [查看会话记录]({url})"
        ),
        "no_text_with_tools": (
            "任务已执行（调用了 {n} 次工具），但 AI 未返回文字总结，产物可查看 [会话记录]({url})。"
        ),
        "no_text_no_tools": "AI 已完成处理，但未生成文本回复。请尝试换个方式描述您的需求。",
        "done_suffix": "\n\n✅ 任务已完成",
        "session_link_prefix": "📎 查看实时聊天记录：[链接>>]({url})\n\n",
        "thinking_start": "🤔 正在思考中...",
        "thinking_end": "回复生成完成",
        "running_indicator": "\n\n⏳ 正在运行中...",
        "running_indicator_long": "\n\n⏳ 正在运行中，我会在完成后提醒您🔔",
        "truncated_suffix": "\n\n📎 内容较长已截断，查看完整内容：[链接>>]({url})",
        "thinking_truncated": "... (思考内容过长，仅显示最新部分) ...",
        "timeout_pre_warning": "⏳ 任务仍在处理中，即将切换为后台运行，完成后自动推送结果...",
        "timeout_background_low": (
            "⏳ 任务耗时较长，仍在后台运行中。后续进展将自动推送，请留意消息通知。"
        ),
        "timeout_background_high": (
            "⏳ 任务耗时较长，仍在后台运行中。完成后将自动推送结果，请留意消息通知。"
        ),
        "session_link_suffix": "\n\n📎 查看实时执行过程：[链接>>]({url})",
        "bg_progress_prefix": "⏳ 任务仍在运行中...\n\n",
        "bg_degraded": (
            "⏳ 任务仍在后台运行（已推送 {n} 段进展，后续输出较多，转为低频提醒，不再逐段推送）。"
            "📎 完整执行过程见：[链接>>]({url})"
        ),
        "bg_truncated": "...(本段过长已截断，完整见 [链接>>]({url}))",
        "bg_done_prefix": "✅ 任务已完成\n\n",
        "bg_done_plain": "✅ 任务已完成",
        "bg_ttl_expired": "⏳ 任务运行超时，已终止后台等待{link}",
        "long_task_done": "✅ 您的任务已完成（耗时 {seconds} 秒），请查看上方回复。",
        "queued_notice": "⏳ 当前使用人数较多，您的请求已排队 {seconds} 秒，现在开始处理…",
        "worker_lost": "任务执行进程异常中断，请重试。",
        "drain_suffix": "\n\n⏳ 服务切换中，任务继续在后台处理，稍后自动推送结果",
        "processing_done": "处理完成。",
        # sessions / 会话列表（spec §8.2 步骤 4）：zh 默认文案。
        "sessions_header": "📋 最近 {n} 个会话（回复序号切换，5 分钟内有效）",
        "no_sessions": "暂无历史会话",
        "session_switched": "✅ 已切换到会话 {index}：{preview}",
        "non_text_message": "[非文本消息]",
        "rt_just_now": "刚刚",
        "rt_minutes": "{n} 分钟前",
        "rt_hours": "{n} 小时前",
        "rt_yesterday": "昨天",
        "rt_days": "{n} 天前",
        # 媒体与引用消息（spec §8.2 步骤 6-7）。前半段是塞进 content parts 发给模型的
        # 提示词，随附件一起发送给模型；
        # 后半段 downloading_* / media_reason_* 才是给用户看的。
        "media_prompt_image": "请描述这张图片的内容。",
        "media_prompt_images": "请描述这些图片的内容。",
        "media_prompt_file": "[用户发送了文件: {name}] 请分析这个文件的内容。",
        "media_image_missing": "[用户发送了一张图片] 请描述这张图片的内容。",
        "media_file_missing": "[用户发送了文件: {name}] 请分析这个文件。",
        "media_mixed_empty": "[用户发送了图文混合消息]",
        "media_image_failed_item": "[图片加载失败]",
        "media_image_placeholder": "[图片]",
        "quote_image_prefix": "[引用了一张图片]\n\n{text}",
        "quote_file_prefix": "[引用了文件: {name}]\n\n{text}",
        "quote_text_prefix": "[引用消息: {quoted}]\n\n{text}",
        "quote_voice_prefix": "[引用语音: {quoted}]\n\n{text}",
        "media_image_failed": "图片处理失败: {error}",
        "media_file_failed": "文件处理失败: {error}",
        "esc_media_failed": "⚠️ 媒体下载失败，请尝试直接用文字回复。",
        "esc_media_interrupted": "⚠️ 媒体处理未完成，请重新发送或用文字回复。",
        "esc_quote_reply": ("请引用本条消息回复"),
        "esc_direct_reply": ("直接回复本应用即可"),
        "esc_sender": ("（发起人：{name}）"),
        "esc_ask": (
            "{bot} 向你发来求助{sender}\n\n{question}\n\n{reply_hint}。已线下处理可回复「已处理」。"
        ),
        "esc_completed": ("✅ 这条求助已处理，感谢你的帮助。"),
        "esc_cancelled": ("这条求助已取消，无需继续回复。"),
        "esc_expired_replied": ("这条求助已结束。已保存你的回复。"),
        "esc_expired_empty": ("这条求助已结束。暂未收到回复，如有需要会重新联系。"),
        "esc_followup": ("补充问题（第 {round} 轮）\n\n{question}\n\n{reply_hint}。"),
        "esc_nudge": (
            "⏰ 仍在等待你的回复：\n\n{question}\n\n{reply_hint}，或回复「已处理」结束。"
        ),
        "esc_nudge_final": (
            "⏰ 这条求助 5 分钟后将超时：\n\n{question}\n\n{reply_hint}，或回复「已处理」结束。"
        ),
        "alert_firing": "CoreMan 运行告警",
        "alert_recovered": "CoreMan 告警恢复",
        "alert_queue": "任务持续积压，请检查 worker 容量与运行状态。",
        "alert_outbox": "存在发送失败消息，请检查通知权限、应用可用范围与失败出站记录。",
        "alert_task_failure": "最近十分钟任务失败率超过阈值，请查看运行记录。",
        "alert_reaper": "检测到 worker 失联任务，已由看护流程收尾。",
        "alert_relay": "运行时健康异常，请查看运行时健康报告。",
        "alert_gateway": "机器人连接异常或凭证被拒绝，请检查租约和平台配置。",
        "stream_recovered": "处理已结束，回复连接已恢复。",
        "stream_interrupted": "本次处理已中断，以上为已保存的内容。",
        "voice_unsupported": "暂不支持语音消息，请发送文字。",
        "voice_empty": "未能识别语音内容，请重试。",
        "downloading_image": "正在下载图片...",
        "downloading_file": "正在下载文件...",
        "processing_mixed": "正在处理图文消息...",
        "downloading_quote_image": "正在下载引用图片...",
        "downloading_quote_file": "正在下载引用文件...",
        # 与 MediaError.reason 同名，ContentBuilder 直接拼 media_reason_{reason}
        "media_reason_timeout": "下载超时",
        "media_reason_too_large": "文件超过 100 MB",
        "media_reason_download_failed": "下载失败",
        "media_reason_decrypt_failed": "解密失败",
        "media_reason_invalid_key": "密钥无效",
        "unknown_filename": "未知",
        "relay_unconfigured": "未配置",
    },
    "ja": {
        "session_reset_ok": "✅ セッションをリセットしました。新しい会話を始めます。",
        "session_reset_failed": (
            "⚠️ セッションのリセットに失敗しました。しばらくしてから再試行してください。"
        ),
        "stopped": "⏹ 現在のタスクを停止しました。",
        "nothing_running": "実行中のタスクはありません。",
        "task_stopped_suffix": "\n\n⏹ タスクはユーザーによって停止されました。",
        "superseded_suffix": "\n\n⏹ 新しいメッセージを受信したため、前のタスクは自動停止しました。",
        "help": HELP_JA,
        "no_permission": (
            "⚠️ 申し訳ありません。このボットを利用する権限がありません。\n\n"
            "権限が必要な場合は管理者にご連絡ください。"
        ),
        "unsupported_message": "この種類のメッセージには未対応です。テキストを送信してください。",
        "ask_user_unsupported": (
            "AI が確認の質問を必要としています。この対話機能は次のバージョンで提供予定です。"
            "より明確な表現で再送信してください。"
        ),
        "ask_user_invalid": (
            "AI が質問を送信しましたが、内容が無効です。別の表現で再送信してください。"
        ),
        "choice_cancelled": "選択をキャンセルしました。",
        "answer_recorded": "✓ 回答を記録しました：{answer}",
        "choice_generating": "⏳ 結果を生成中です。完了後に自動送信します…",
        "submit_heartbeat": "⏳ AI が処理中です（経過 {seconds} 秒）。少々お待ちください…",
        "choice_duplicate": (
            "🔄 先ほど送信された回答を AI が処理中です。少々お待ちください。"
            "長時間応答がない場合は「取消」と送信するとリセットできます。"
        ),
        "choice_config_changed": (
            "ボットの設定が変更されたため、先ほどの質問は無効になりました。"
            "メッセージを送り直してください。"
        ),
        "choice_submit_done": "選択を受け付けました。処理が完了しました。",
        "rl_expired_title": "⏰ 期限切れ",
        "rl_expired_desc": "カードの有効期限（30 分）が切れました。もう一度実行してください",
        "rl_replaced_title": "ℹ️ 新しいカードに置き換わりました",
        "rl_replaced_desc": "新しい切替の案内が届いています。そちらをご確認ください",
        "rl_no_option_title": "⚠️ 選択肢が未選択です",
        "rl_no_option_desc": "もう一度選択してください",
        "rl_wait_title": "⏳ 待機を選択しました",
        "rl_wait_desc": "**{current}** のまま利用枠の回復を待ちます。",
        "rl_switching_title": "🔄 切替中…",
        "rl_switching_desc": (
            "**{current}** から **{target}** へ切り替えています。少々お待ちください。"
        ),
        "rl_unknown_title": "⚠️ 不明な選択肢です",
        "rl_unknown_desc": "option={option}",
        "card_source_ai": "AI アシスタント",
        "card_source_dispatch": "サービス調整",
        "card_source_ai_dispatch": "AI アシスタント · サービス調整",
        "card_q_prefix": "❓ 質問",
        "card_q_single": "❓ 選択してください",
        "card_option_fallback": "選択肢 {n}",
        "card_option_other": "その他（自由入力）",
        "card_submit_choice": "選択を確定",
        "card_answered_prefix": "✅ 回答済み · 質問",
        "card_answered_single": "✅ 回答済み",
        "card_answered_sub": "ご回答：{answer}",
        "card_no_selection": "(未選択)",
        "card_waiting_title": "✏️ 回答を入力してください",
        "card_waiting_option": "入力待ちです。メッセージを送信してください",
        "card_waiting_submit": "入力待ち",
        "card_expired_title": "期限切れ",
        "card_expired_desc": "選択セッションの有効期限が切れました。メッセージを送り直してください",
        "brief_q_prefix": "**質問 {index}/{total}**",
        "brief_q_single": "**選択してください**",
        "brief_option_other": "- **その他** — テキストを直接送信して回答",
        # モデルに送り返す本文なので ja も中国語のまま（zh 表の説明を参照）。
        "answers_header": "[用户选择回答]",
        "answers_unanswered": "(未回答)",
        "rl_offer_title": "⚠️ 現在のインスタンスが利用枠の上限に達しました",
        "rl_offer_desc": (
            "インスタンス **{current}** の枠を使い切りました。"
            "週間枠に余裕のある **{target}**（7日間使用率 {pct}）に切り替えますか？"
        ),
        "rl_offer_opt_switch": "{target} に切り替える",
        "rl_offer_opt_wait": "{current} のまま回復を待つ",
        "rl_offer_submit": "確定",
        "rl_pct_unknown": "不明",
        "rl_reset_done": "リセット済み",
        "rl_later": "後ほど",
        "rl_ago_just_now": "たった今",
        "rl_ago_minutes": "{n} 分前",
        "rl_ago_hours": "{h}h{m}m 前",
        "rl_quota_title": "\n\n---\n**📊 サーバー利用枠の一覧**\n",
        "rl_quota_head": "| インスタンス | モデル | 5時間枠 | 7日間枠 |",
        "rl_quota_probed": "↳取得 {ago}",
        "rl_quota_footer": (
            "_📡 使用率は 15 分ごとに取得し、カウントダウンは現在時刻から算出しています。_"
        ),
        "rl_probed_note": "（取得：{ago}）",
        "rl_warn_both": (
            "\n\n---\n"
            "⚠️ **利用枠のお知らせ**：本インスタンスの枠がまもなく尽きます{note}\n"
            "- 5 時間枠：{p5}%（約 {reset5} 後にリセット）\n"
            "- 7 日間枠：{p7}%（約 {reset7} 後にリセット）"
        ),
        "rl_warn_5h": (
            "\n\n---\n⚠️ **利用枠のお知らせ**：本インスタンスの 5 時間枠を {pct}% "
            "使用しました{note}。約 **{reset}** 後にリセットされます"
        ),
        "rl_warn_7d": (
            "\n\n---\n⚠️ **利用枠のお知らせ**：本インスタンスの 7 日間枠を {pct}% "
            "使用しました{note}。約 **{reset}** 後にリセットされます"
        ),
        "relay_switch_ok": (
            "✅ **ランタイム インスタンスを切り替えました**\n\n"
            "- 切替元：{current}\n"
            "- 切替先：{target}\n"
            "- 詳細：{detail}\n\n"
            "_ご注意：インスタンスの切替に伴い会話コンテキスト（session）はリセットされます。"
            "次のメッセージから新しい会話が始まります。_"
        ),
        "relay_switch_failed": (
            "❌ **ランタイム インスタンスの切替に失敗しました**\n\n"
            "- 切替元：{current}\n"
            "- 切替先：{target}\n"
            "- 理由：{detail}\n\n"
            "_管理コンソールから手動で対応するか、しばらくしてから再試行してください。_"
        ),
        "relay_switch_forbidden": (
            "権限が不足しています。このボットの管理者から外された可能性があります"
        ),
        "relay_switch_detail": "モデル：{model}",
        "relay_error": (
            "申し訳ありません。AI 接続でエラーが発生しました（インスタンス {relay}）。"
            "しばらくしてから再試行してください。"
        ),
        "runtime_busy": (
            "申し訳ありません。インスタンス {relay} が混み合っており、待機時間内に処理を"
            "開始できませんでした。しばらくしてから再試行してください。"
        ),
        "relay_error_text": (
            "⚠️ AI サービスがエラーを返しました。しばらくしてから再試行してください。"
        ),
        "empty_stream": (
            "⚠️ AI サービスから空の応答が返されました。一時的な障害の可能性があります。"
            "再試行してください。繰り返し失敗する場合は管理者にご連絡ください。"
            "📎 [会話ログを見る]({url})"
        ),
        "no_text_with_tools": (
            "タスクは実行されました（ツールを {n} 回呼び出し）が、"
            "AI はテキストの要約を返しませんでした。"
            "成果物は [会話ログ]({url}) で確認できます。"
        ),
        "no_text_no_tools": (
            "AI は処理を完了しましたが、テキスト応答を生成しませんでした。"
            "別の表現でご要望をお試しください。"
        ),
        "done_suffix": "\n\n✅ タスク完了",
        "session_link_prefix": "📎 リアルタイムの会話ログ：[リンク>>]({url})\n\n",
        "thinking_start": "🤔 考え中...",
        "thinking_end": "回答の生成が完了しました",
        "running_indicator": "\n\n⏳ 実行中...",
        "running_indicator_long": "\n\n⏳ 実行中です。完了したらお知らせします🔔",
        "truncated_suffix": "\n\n📎 内容が長いため省略しました。全文はこちら：[リンク>>]({url})",
        "thinking_truncated": "... (思考内容が長いため最新部分のみ表示) ...",
        "timeout_pre_warning": (
            "⏳ タスクはまだ処理中です。まもなくバックグラウンド実行に切り替わり、"
            "完了後に結果を自動送信します..."
        ),
        "timeout_background_low": (
            "⏳ タスクに時間がかかっており、バックグラウンドで実行中です。"
            "進捗は自動的に送信されます。通知にご注意ください。"
        ),
        "timeout_background_high": (
            "⏳ タスクに時間がかかっており、バックグラウンドで実行中です。"
            "完了後に結果を自動送信します。通知にご注意ください。"
        ),
        "session_link_suffix": "\n\n📎 リアルタイムの実行過程：[リンク>>]({url})",
        "bg_progress_prefix": "⏳ タスクは実行中です...\n\n",
        "bg_degraded": (
            "⏳ タスクはバックグラウンドで実行中です（{n} 件の進捗を送信済み。"
            "出力が多いため低頻度の通知に切り替え、段落ごとの送信は停止します）。"
            "📎 完全な実行過程：[リンク>>]({url})"
        ),
        "bg_truncated": "...(この段落は長いため省略。全文は [リンク>>]({url}))",
        "bg_done_prefix": "✅ タスク完了\n\n",
        "bg_done_plain": "✅ タスク完了",
        "bg_ttl_expired": (
            "⏳ タスクの実行がタイムアウトしたため、バックグラウンド待機を終了しました{link}"
        ),
        "long_task_done": (
            "✅ タスクが完了しました（所要 {seconds} 秒）。上の返信をご確認ください。"
        ),
        "queued_notice": (
            "⏳ 現在利用者が多いため、リクエストは {seconds} 秒待機しました。処理を開始します…"
        ),
        "worker_lost": "タスクの実行プロセスが異常終了しました。再試行してください。",
        "drain_suffix": (
            "\n\n⏳ サービス切替中です。"
            "タスクはバックグラウンドで継続し、後ほど結果を自動送信します"
        ),
        "processing_done": "処理が完了しました。",
        "sessions_header": "📋 直近 {n} 件の会話（番号を返信すると切り替わります。5 分間有効）",
        "no_sessions": "過去の会話はありません",
        "session_switched": "✅ 会話 {index} に切り替えました：{preview}",
        "non_text_message": "[テキスト以外のメッセージ]",
        "rt_just_now": "たった今",
        "rt_minutes": "{n} 分前",
        "rt_hours": "{n} 時間前",
        "rt_yesterday": "昨日",
        "rt_days": "{n} 日前",
        # 前半段是发给模型的提示词，日文环境下同样保持中文原文（见 zh 表里的说明）。
        "media_prompt_image": "请描述这张图片的内容。",
        "media_prompt_images": "请描述这些图片的内容。",
        "media_prompt_file": "[用户发送了文件: {name}] 请分析这个文件的内容。",
        "media_image_missing": "[用户发送了一张图片] 请描述这张图片的内容。",
        "media_file_missing": "[用户发送了文件: {name}] 请分析这个文件。",
        "media_mixed_empty": "[用户发送了图文混合消息]",
        "media_image_failed_item": "[图片加载失败]",
        "media_image_placeholder": "[图片]",
        "quote_image_prefix": "[引用了一张图片]\n\n{text}",
        "quote_file_prefix": "[引用了文件: {name}]\n\n{text}",
        "quote_text_prefix": "[引用消息: {quoted}]\n\n{text}",
        "quote_voice_prefix": "[引用语音: {quoted}]\n\n{text}",
        "media_image_failed": "画像の処理に失敗しました: {error}",
        "media_file_failed": "ファイルの処理に失敗しました: {error}",
        "esc_media_failed": "⚠️ 添付ファイルを取得できませんでした。テキストで返信してください。",
        "esc_media_interrupted": (
            "⚠️ 添付処理が中断されました。再送するか、テキストで返信してください。"
        ),
        "esc_quote_reply": ("このメッセージを引用して返信してください"),
        "esc_direct_reply": ("このアプリに返信してください"),
        "esc_sender": ("（依頼者：{name}）"),
        "esc_ask": (
            "{bot} からの確認依頼です{sender}\n\n{question}"
            "\n\n{reply_hint}。対応済みの場合は「対応済み」と返信してく"
            "ださい。"
        ),
        "esc_completed": ("✅ この依頼への対応が完了しました。ご協力ありがとうございました。"),
        "esc_cancelled": ("この依頼は取り消されました。返信は不要です。"),
        "esc_expired_replied": ("この依頼は終了しました。返信内容は保存済みです。"),
        "esc_expired_empty": (
            "この依頼は終了しました。返信を確認できませんでした。必要に応じて再度ご連絡します。"
        ),
        "esc_followup": ("追加の質問（第 {round} 回）\n\n{question}\n\n{reply_hint}。"),
        "esc_nudge": (
            "⏰ 返信をお待ちしています：\n\n{question}\n\n{reply_"
            "hint}。終了するには「対応済み」と返信してください。"
        ),
        "esc_nudge_final": (
            "⏰ この依頼はあと5分で期限切れになります：\n\n{question}\n"
            "\n{reply_hint}。終了するには「対応済み」と返信してください"
            "。"
        ),
        "alert_firing": "CoreMan 運用アラート",
        "alert_recovered": "CoreMan アラート復旧",
        "alert_queue": ("タスクの滞留が続いています。worker の容量と稼働状態を確認してください。"),
        "alert_outbox": (
            "送信失敗があります。通知権限、アプリの利用範囲、送信履歴を確認してください。"
        ),
        "alert_task_failure": (
            "直近10分のタスク失敗率が閾値を超えました。実行履歴を確認してください。"
        ),
        "alert_reaper": "応答しない worker のタスクを検出し、監視処理で終了しました。",
        "alert_relay": (
            "ランタイム のヘルス状態が異常です。インスタンスのレポートを確認してください。"
        ),
        "alert_gateway": (
            "ボットの接続または認証に問題があります。"
            "リースとプラットフォーム設定を確認してください。"
        ),
        "stream_recovered": "処理は終了しました。返信の接続を復旧しました。",
        "stream_interrupted": "処理が中断されました。上記は保存済みの内容です。",
        "voice_unsupported": "音声メッセージにはまだ対応していません。テキストを送信してください。",
        "voice_empty": "音声を認識できませんでした。もう一度お試しください。",
        "downloading_image": "画像をダウンロード中...",
        "downloading_file": "ファイルをダウンロード中...",
        "processing_mixed": "画像付きメッセージを処理中...",
        "downloading_quote_image": "引用された画像をダウンロード中...",
        "downloading_quote_file": "引用されたファイルをダウンロード中...",
        "media_reason_timeout": "ダウンロードがタイムアウトしました",
        "media_reason_too_large": "ファイルが 100 MB を超えています",
        "media_reason_download_failed": "ダウンロードに失敗しました",
        "media_reason_decrypt_failed": "復号に失敗しました",
        "media_reason_invalid_key": "鍵が無効です",
        "unknown_filename": "不明",
        "relay_unconfigured": "未設定",
    },
}


def msg(key: str, locale: str = "zh", **kwargs: object) -> str:
    """取一条文案：locale 未知回落 zh，key 未知抛 KeyError，只在给了参数时才 format。"""
    table = MESSAGES.get(locale) or MESSAGES["zh"]
    template = table[key]
    return template.format(**kwargs) if kwargs else template
