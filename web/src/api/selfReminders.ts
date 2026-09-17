import { call, http } from '@/api/client'
export interface SelfReminder {
  id: string; bot_name: string; text: string; run_at: string; enabled: boolean
  status: string; deliveries: string[]; can_cancel: boolean
}
export const selfReminders = {
  list: () => call<SelfReminder[]>(http.get('/api/self-reminders')),
  cancel: (id: string) => call<SelfReminder>(http.post(`/api/self-reminders/${id}/cancel`)),
}
