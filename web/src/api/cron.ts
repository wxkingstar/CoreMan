import { call, http } from '@/api/client'
import type { Page } from '@/api/types'

export interface CronIn {
  schedule_kind?: 'recurring' | 'once'; run_at?: string | null
  bot_id: string; name: string; cron_expression: string; timezone: string; prompt: string
  system_prompt: string | null; precheck_script: string | null; precheck_timeout_seconds: number
  enabled: boolean; expires_at: string | null; target_users: string[]; target_chats: string[]
  notify_emails: string[]; notify_webhook: boolean; notify_webhook_url?: string | null
}
export interface CronOut extends CronIn {
  consumed_at?: string | null; delivery_status?: string; latest_run_id?: number | null
  has_webhook_url: boolean
  id: string; version: number; created_by: string; can_edit: boolean; next_run_at: string | null
  force_run_at: string | null; running_task_id: number | null; last_status: string | null
}
export interface CronRun {
  id: number; task_id: number | null; status: string; prompt: string; reply: string | null
  error_message: string | null; precheck_meta: Record<string, unknown> | null
  delivery: { errors?: Record<string, string>; outbox_ids?: number[] }
  deliveries: { id: number; platform: string; channel?: string; status: string; attempts: number; error: string | null }[]
  started_at: string; finished_at: string | null; trigger_kind: string; executed_by: string | null
  input_tokens: number | null; output_tokens: number | null
}
export interface SmtpIn {
  enabled: boolean; host: string; port: number; security: 'tls' | 'starttls'; username: string
  password?: string; sender: string
}
export interface SmtpOut extends SmtpIn { version: number; has_password: boolean }
const root = '/api/admin/cron-jobs'
const version = (v: number) => ({ headers: { 'If-Match': `"${v}"` } })
export const cron = {
  list: (page = 1, bot_id?: string) => call<Page<CronOut>>(http.get(root, { params: { page, bot_id } })),
  create: (body: CronIn) => call<CronOut>(http.post(root, body)),
  update: (id: string, body: CronIn, v: number) => call<CronOut>(http.put(`${root}/${id}`, body, version(v))),
  run: (row: CronOut) => call<CronOut>(http.post(`${root}/${row.id}/run`, null, version(row.version))),
  cancel: (row: CronOut) => call<CronOut>(http.post(`${root}/${row.id}/cancel-run`, null, version(row.version))),
  disable: (row: CronOut) => call<CronOut>(http.post(`${root}/${row.id}/disable`, null, version(row.version))),
  remove: (row: CronOut) => call<null>(http.delete(`${root}/${row.id}`, version(row.version))),
  runs: (id: string, page = 1) => call<Page<CronRun>>(http.get(`${root}/${id}/runs`, { params: { page } })),
  testNotification: (row: CronOut) => call<{ outbox_ids: number[]; errors: Record<string, string> }>(http.post(`${root}/${row.id}/test-notification`, null, version(row.version))),
  notificationTests: (id: string) => call<CronRun['deliveries']>(http.get(`${root}/${id}/notification-tests`)),
  notificationChats: (bot_id: string) => call<{ id: string; name: string }[]>(http.get(`${root}/notification-chats`, { params: { bot_id, details: true } })),
  precheck: (script: string) => call<{ status: string; error?: string; trigger?: boolean; reason?: string }>(http.post(`${root}/precheck/test`, { script })),
}
export const smtp = {
  get: () => call<SmtpOut>(http.get('/api/admin/notification-settings')),
  save: (body: SmtpIn, v: number) => call<SmtpOut>(http.put('/api/admin/notification-settings', body, version(v))),
}
