import { call, http } from '@/api/client'

/** 一次性本人提醒：只能取消。 */
export interface SelfReminder {
  id: string; type: 'reminder'; bot_name: string; text: string; run_at: string; enabled: boolean
  status: string; deliveries: string[]; can_cancel: boolean
}

/** 本人 AI 定时任务的最近一次执行；reply 为截断后的纯文本。 */
export interface SelfScheduleRun {
  status: string; started_at: string; finished_at: string | null
  reply: string | null; truncated: boolean; error: string | null; deliveries: string[]
}

/** 本人 AI 定时任务：在机器人私聊里确认卡片后创建，结果只发回该私聊。 */
export interface SelfSchedule {
  id: string; type: 'schedule'; bot_name: string; enabled: boolean
  name: string; text: string
  schedule_kind: 'recurring' | 'once'; cron_expression: string; timezone: string
  run_at: string | null; next_run_at: string | null
  status: string; running: boolean; last_run: SelfScheduleRun | null
  can_pause: boolean; can_resume: boolean
}

export type SelfItem = SelfReminder | SelfSchedule

const root = '/api/self-reminders'
export const selfReminders = {
  list: () => call<SelfItem[]>(http.get(root)),
  cancel: (id: string) => call<SelfReminder>(http.post(`${root}/${id}/cancel`)),
  pause: (id: string) => call<SelfSchedule>(http.post(`${root}/${id}/pause`)),
  resume: (id: string) => call<SelfSchedule>(http.post(`${root}/${id}/resume`)),
  remove: (id: string) => call<null>(http.delete(`${root}/${id}`)),
}
