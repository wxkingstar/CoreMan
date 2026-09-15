import { call, http } from '@/api/client'
import type {
  AllowedUserOut, AnnouncementIn, AnnouncementOut, AuditLogOut, BotIn, BotMemberOut, BotOut, BotPatch, CatalogIn, CatalogOut, CatalogPatch,
  ChatLogOut, ChatLogStats, DeptNode, DrainIn, Page, PlatformAppIn, PlatformAppOut, ProbeResult, Providers,
  RelayModels, RelayOut, RuleIn, RuntimeInstance, RuntimeLease, RuntimeOutboxItem, RuntimeQueue,
  RuntimeTask, SettingsDefaults, SettingsOut, SettingsPatch, SwitchRelayIn, SwitchRelayOut, SyncRun, TeamIn,
  TeamOut, UserOut, UserPatch,
} from '@/api/types'

type Query = Record<string, string | number | boolean | undefined | null>

export const users = {
  list: (params: Query) => call<Page<UserOut>>(http.get('/api/admin/users', { params })),
  get: (id: string) => call<UserOut>(http.get(`/api/admin/users/${id}`)),
  patch: (id: string, body: UserPatch) => call<UserOut>(http.patch(`/api/admin/users/${id}`, body)),
}
export const teams = {
  list: () => call<TeamOut[]>(http.get('/api/admin/teams')),
  create: (body: TeamIn) => call<TeamOut>(http.post('/api/admin/teams', body)),
  update: (id: string, body: TeamIn) => call<TeamOut>(http.put(`/api/admin/teams/${id}`, body)),
  remove: (id: string) => call<null>(http.delete(`/api/admin/teams/${id}`)),
  replaceRules: (id: string, rules: RuleIn[]) => call<TeamOut>(http.put(`/api/admin/teams/${id}/rules`, rules)),
}
export const departments = { tree: (platform = 'wecom') => call<DeptNode[]>(http.get('/api/admin/departments', { params: { platform } })) }
export const platformApps = {
  list: (params: Query) => call<Page<PlatformAppOut>>(http.get('/api/admin/platform-apps', { params })),
  create: (body: PlatformAppIn) => call<PlatformAppOut>(http.post('/api/admin/platform-apps', body)),
  update: (id: string, body: PlatformAppIn, version: number) =>
    call<PlatformAppOut>(http.put(`/api/admin/platform-apps/${id}`, body, { headers: { 'If-Match': `"${version}"` } })),
  remove: (id: string) => call<null>(http.delete(`/api/admin/platform-apps/${id}`)),
  test: (id: string) => call<{ ok: boolean; message: string }>(http.post(`/api/admin/platform-apps/${id}/test`)),
  sync: (id: string) => call<{ run_id: number }>(http.post(`/api/admin/platform-apps/${id}/sync`)),
  runs: (id: string) => call<SyncRun[]>(http.get(`/api/admin/platform-apps/${id}/sync-runs`)),
}
export const syncRuns = { get: (id: number) => call<SyncRun>(http.get(`/api/admin/sync-runs/${id}`)) }
export const auth = { providers: () => call<Providers>(http.get('/api/auth/providers')) }
// 运行时（relay）由节点 Daemon 自动登记，管理台只读列表、探测与取有效模型集；手工增删改已下线。
export const relays = {
  list: (params: Query) => call<Page<RelayOut>>(http.get('/api/admin/relay-servers', { params })),
  probe: (id: string) => call<ProbeResult>(http.post(`/api/admin/relay-servers/${id}/probe`)),
  models: (id: string) => call<RelayModels>(http.get(`/api/admin/relay-servers/${id}/models`)),
}
export const catalog = {
  list: (provider?: string) => call<CatalogOut[]>(http.get('/api/admin/model-catalog', { params: provider ? { provider } : {} })),
  create: (body: CatalogIn) => call<CatalogOut>(http.post('/api/admin/model-catalog', body)),
  // model 里有 `/`（vllm/claude-sonnet-4-6），必须转义后再拼进路径。
  patch: (provider: string, model: string, body: CatalogPatch) =>
    call<CatalogOut>(http.patch(`/api/admin/model-catalog/${provider}/${encodeURIComponent(model)}`, body)),
  remove: (provider: string, model: string) =>
    call<null>(http.delete(`/api/admin/model-catalog/${provider}/${encodeURIComponent(model)}`)),
}
export const bots = {
  list: (params: Query) => call<Page<BotOut>>(http.get('/api/admin/bots', { params })),
  get: (id: string) => call<BotOut>(http.get(`/api/admin/bots/${id}`)),
  create: (body: BotIn) => call<BotOut>(http.post('/api/admin/bots', body)),
  patch: (id: string, body: BotPatch, version: number) =>
    call<BotOut>(http.patch(`/api/admin/bots/${id}`, body, { headers: { 'If-Match': `"${version}"` } })),
  remove: (id: string) => call<null>(http.delete(`/api/admin/bots/${id}`)),
  switchRelay: (id: string, body: SwitchRelayIn, version: number) =>
    call<SwitchRelayOut>(http.post(`/api/admin/bots/${id}/switch-relay`, body, { timeout: 420000, headers: { 'If-Match': `"${version}"` } })),
  toggle: (id: string) => call<BotOut>(http.post(`/api/admin/bots/${id}/toggle`)),
  members: (id: string) => call<BotMemberOut[]>(http.get(`/api/admin/bots/${id}/members`)),
  addMember: (id: string, userId: string) => call<BotMemberOut>(http.post(`/api/admin/bots/${id}/members`, { user_id: userId })),
  removeMember: (id: string, userId: string) => call<null>(http.delete(`/api/admin/bots/${id}/members/${userId}`)),
  allowedUsers: (id: string) => call<AllowedUserOut[]>(http.get(`/api/admin/bots/${id}/allowed-users`)),
  setAllowedUsers: (id: string, userIds: string[]) =>
    call<AllowedUserOut[]>(http.put(`/api/admin/bots/${id}/allowed-users`, { user_ids: userIds })),
}
// 公告是运维数据（几十条封顶），后端不分页，直接给数组。
export const announcements = {
  list: () => call<AnnouncementOut[]>(http.get('/api/admin/announcements')),
  create: (body: AnnouncementIn) => call<AnnouncementOut>(http.post('/api/admin/announcements', body)),
  update: (id: string, body: AnnouncementIn) => call<AnnouncementOut>(http.put(`/api/admin/announcements/${id}`, body)),
  toggle: (id: string) => call<AnnouncementOut>(http.post(`/api/admin/announcements/${id}/toggle`)),
  remove: (id: string) => call<null>(http.delete(`/api/admin/announcements/${id}`)),
}
export const auditLogs = { list: (params: Query) => call<Page<AuditLogOut>>(http.get('/api/admin/audit-logs', { params })) }
export const chatLogs = {
  list: (params: Query) => call<Page<ChatLogOut>>(http.get('/api/admin/chat-logs', { params })),
  get: (id: number) => call<ChatLogOut>(http.get(`/api/admin/chat-logs/${id}`)),
  stats: (params: Query) => call<ChatLogStats>(http.get('/api/admin/chat-logs/stats', { params })),
}
export const runtime = {
  instances: () => call<RuntimeInstance[]>(http.get('/api/admin/runtime/instances')),
  leases: () => call<RuntimeLease[]>(http.get('/api/admin/runtime/leases')),
  queue: () => call<RuntimeQueue>(http.get('/api/admin/runtime/queue')),
  tasks: (params: Query) => call<RuntimeTask[]>(http.get('/api/admin/runtime/tasks', { params })),
  outbox: (params: Query) => call<RuntimeOutboxItem[]>(http.get('/api/admin/runtime/outbox', { params })),
  retryOutbox: (id: number) => call<null>(http.post(`/api/admin/runtime/outbox/${id}/retry`)),
  cancelTask: (id: number, reason?: string) => call<null>(http.post(`/api/admin/runtime/tasks/${id}/cancel`, { reason })),
  drain: (body: DrainIn) => call<null>(http.post('/api/admin/runtime/drain', body)),
}
export const settings = {
  get: () => call<SettingsOut>(http.get('/api/admin/settings')),
  update: (body: SettingsPatch) => call<SettingsOut>(http.put('/api/admin/settings', body)),
  defaults: () => call<SettingsDefaults>(http.get('/api/admin/settings/defaults')),
}
