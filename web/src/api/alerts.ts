import { call, http } from '@/api/client'
export interface AlertChannel { platform_app_id: string; user_id: string }
export interface AlertConfig { version: number; channels: AlertChannel[] }
export const alerts = {
  get: () => call<AlertConfig>(http.get('/api/admin/alert-settings')),
  save: (channels: AlertChannel[], version: number) => call<AlertConfig>(http.put('/api/admin/alert-settings', { channels }, { headers: { 'If-Match': `"${version}"` } })),
}
