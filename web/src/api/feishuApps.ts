import { call, http } from './client'

export type FeishuRegistrationStatus = 'pending' | 'succeeded' | 'consumed' | 'expired' | 'denied' | 'failed' | 'cancelled'
export interface FeishuRegistrationIn {
  purpose: 'create' | 'update'
  bot_id?: string
  name?: string
  description?: string
  avatar_url?: string | null
  reuse?: boolean
}
export interface FeishuRegistration {
  id: string
  purpose: 'create' | 'update'
  status: FeishuRegistrationStatus
  bot_id: string | null
  app_id: string | null
  url: string | null
  expires_at: string
  retry_after: number | null
  error: string | null
  created_at: string | null
  reused?: boolean
}

export type FeishuMenuKind = 'link' | 'event' | 'submenu' | 'message'
export interface FeishuMenu {
  menu_id: string
  parent_menu_id: string | null
  name: string
  kind: FeishuMenuKind
  pc_url: string | null
  mobile_url: string | null
  event_key: string | null
  sort: number
}
export interface FeishuAppVersion {
  version_id: string | null
  version: string | null
  /** 1 审核通过, 2 审核拒绝, 3 审核中, 4 未提交审核 */
  status: number | null
  create_time: string | null
  publish_time: string | null
  remark: string | null
  update_remark: string | null
  visibility: { is_all: boolean; open_ids: string[]; department_ids: string[] } | null
  events: string[]
  bot: { menu_enabled: boolean; menu_display_strategy: number | null; menus: FeishuMenu[] } | null
}
export interface FeishuSlashCommand { command_id: string; command: string; description: string; icon_key: string | null; update_time: string | null }
export type FeishuPersonalLevel = 'messages_readonly' | 'all_except_send' | 'all'
export interface FeishuAppOverview {
  app_id: string
  app: {
    app_id: string; name: string; description: string; avatar_url: string | null; status: number | null
    create_source: string | null; primary_language: 'zh_cn' | 'en_us' | 'ja_jp'; help_use: string | null
    online_version_id: string | null; under_review: boolean
  } | null
  scopes: { scope: string; token_types: ('tenant' | 'user')[]; level: number | null; granted: boolean | null }[]
  missing_scopes: { tenant: string[]; user: string[] }
  pending_grants: string[]
  personal_levels: Record<FeishuPersonalLevel, boolean>
  personal_connections: number
  versions: { online: FeishuAppVersion | null; under_review: FeishuAppVersion | null }
  slash_commands: FeishuSlashCommand[] | null
  errors: { app: number | null; grants: number | null; versions: number | null; slash_commands: number | null }
  origin: { one_click: true; created_by_name: string; created_at: string } | { one_click: false }
  employee: { name: string; description: string }
  next_version: string
  console_url: string
}
export interface FeishuBaseIn { language: string; name: string; description: string; help_use: string | null }
export interface FeishuBotIn {
  language: string
  get_started_desc: string | null
  menu_enabled: boolean | null
  menu_display_strategy: 1 | 2 | 3 | null
  menus: FeishuMenu[]
}
export interface FeishuVisibilityIn { visible_to_all: boolean; user_ids: string[]; department_ids: string[] }
export interface FeishuPublishIn { version: string; changelog: string; remark: string }

const registrations = '/api/admin/feishu-app-registrations'
const app = (botId: string) => `/api/admin/bots/${encodeURIComponent(botId)}/feishu-app`
export const feishuApps = {
  startRegistration: (body: FeishuRegistrationIn) => call<FeishuRegistration>(http.post(registrations, body)),
  registration: (id: string) => call<FeishuRegistration>(http.get(`${registrations}/${encodeURIComponent(id)}`)),
  cancelRegistration: (id: string) => call<FeishuRegistration>(http.delete(`${registrations}/${encodeURIComponent(id)}`)),
  overview: (botId: string) => call<FeishuAppOverview>(http.get(app(botId))),
  updateBase: (botId: string, body: FeishuBaseIn) => call<{ ok: true; publish_required: boolean }>(http.patch(`${app(botId)}/base`, body)),
  uploadAvatar: (botId: string, file: File) => {
    const form = new FormData()
    form.append('avatar', file)
    return call<{ avatar_url: string; publish_required: boolean }>(http.post(`${app(botId)}/avatar`, form))
  },
  updateBot: (botId: string, body: FeishuBotIn) => call<{ ok: true; publish_required: boolean }>(http.patch(`${app(botId)}/bot`, body)),
  updateVisibility: (botId: string, body: FeishuVisibilityIn) => call<{ ok: true; publish_required: boolean }>(http.patch(`${app(botId)}/visibility`, body)),
  publish: (botId: string, body: FeishuPublishIn) => call<{ version_id: string | null; version: string }>(http.post(`${app(botId)}/publish`, body)),
  applyScopes: (botId: string) => call<{ ok: true }>(http.post(`${app(botId)}/scopes/apply`)),
  createCommand: (botId: string, body: { command: string; description: string; icon_key: string | null }) =>
    call<{ command_id: string }>(http.post(`${app(botId)}/slash-commands`, body)),
  updateCommand: (botId: string, commandId: string, body: { description: string; icon_key: string | null }) =>
    call<{ ok: true }>(http.patch(`${app(botId)}/slash-commands/${encodeURIComponent(commandId)}`, body)),
  addDefaultCommands: (botId: string) => call<{ created: string[] }>(http.post(`${app(botId)}/slash-commands/defaults`)),
  deleteCommand: (botId: string, commandId: string) =>
    call<{ ok: true }>(http.delete(`${app(botId)}/slash-commands/${encodeURIComponent(commandId)}`)),
}
