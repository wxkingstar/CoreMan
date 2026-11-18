import { call, http } from '@/api/client'
import type { Page } from '@/api/types'

export interface BusinessSystem {
  key: string; name: string; description: string | null; base_url: string | null; openapi_url: string | null
  /** openapi_url 的旧名，后端保留一个版本；管理台只用 openapi_url。 */
  sitemap_url?: string | null
  token_provider?: string; token_audience?: string | null; access_test_url?: string | null
  /** env：令牌按 BOT_TOKEN_<KEY> 下发；proxy：平台代理调用；mcp：系统是一个 MCP 服务，每轮按发言者挂载。 */
  token_delivery?: 'env' | 'proxy' | 'mcp'
  /** token_delivery 为 mcp 时的 MCP 地址。 */
  mcp_url?: string | null
  /** 授权给 AI 员工时自动安装的技能，撤销授权时一并卸载。 */
  companion_skill_id?: string | null
  enabled: boolean; sort_order: number; default_for_all_bots: boolean; allowed_bot_ids: string[] | null; version: number
  /** 平台代理（token_delivery=proxy）的批量与调用上限；旧后端不返回。 */
  batch_write_max?: number; batch_destructive_max?: number; proxy_calls_per_turn?: number; batch_interval_ms?: number
  /** 只在保存的响应里出现：保存后立即拉取目录的结果，未配置 OpenAPI 地址或地址未改动时为 null。 */
  catalog?: SystemCatalog | CatalogSkipped | null
}
export type SystemInput = Omit<BusinessSystem, 'version' | 'catalog' | 'sitemap_url'>
export interface CatalogFinding { rule: string; severity: 'error' | 'warn'; path: string; message: string }
/** 操作目录状态：拉取并编译业务系统的 OpenAPI 描述后的结果。 */
export interface SystemCatalog {
  status: 'ok' | 'stale' | 'error'; error: string | null; spec_url: string; spec_bytes: number | null
  fetched_at: string | null; checked_at: string | null
  module_count: number; operation_count: number; hidden_count: number
  lint_errors: number; lint_warnings: number; lint: CatalogFinding[]
}
/** 保存时没能拉取（例如操作者没有可用的业务系统身份），error 是给管理员看的原因。 */
export interface CatalogSkipped { status: 'skipped'; error: string }
export interface ApiClient {
  app_key: string; name: string; scopes: string[]; enabled: boolean; version: number; last_used_at: string | null; has_secret: boolean
}
// external：部署配置的外部签发方密钥（BOT_JWT_*），没有创建时间，也不参与轮换。
export interface JwtKey { kid: string; is_active: boolean; created_at: string | null; retired_at: string | null; external?: boolean }
export interface TokenProvider { id: string; max_token_ttl_seconds: number | null }
/** write_keys：允许平台代理执行写操作的系统，是 system_keys 的子集。 */
export interface SystemGrantsOut { system_keys: string[]; write_keys: string[]; version: number }
export const systems = {
  providers: () => call<TokenProvider[]>(http.get('/api/admin/token-providers')),
  list: (page = 1) => call<Page<BusinessSystem>>(http.get('/api/admin/systems', { params: { page } })),
  create: (body: SystemInput) => call<BusinessSystem>(http.post('/api/admin/systems', body)),
  update: (row: BusinessSystem, body: Omit<SystemInput, 'key'>) => call<BusinessSystem>(http.put(`/api/admin/systems/${row.key}`, body, { headers: { 'If-Match': String(row.version) } })),
  remove: (row: BusinessSystem) => call<null>(http.delete(`/api/admin/systems/${row.key}`, { headers: { 'If-Match': String(row.version) } })),
  catalog: (key: string) => call<SystemCatalog | null>(http.get(`/api/admin/systems/${key}/catalog`)),
  refreshCatalog: (key: string) => call<SystemCatalog | null>(http.post(`/api/admin/systems/${key}/catalog/refresh`)),
  grants: (id: string) => call<SystemGrantsOut>(http.get(`/api/admin/bots/${id}/system-grants`)),
  saveGrants: (id: string, keys: string[], version: number, writeKeys: string[]) => call<SystemGrantsOut>(http.put(`/api/admin/bots/${id}/system-grants`, { system_keys: keys, write_keys: writeKeys }, { headers: { 'If-Match': String(version) } })),
}
export type SystemBatchPhase = 'awaiting' | 'running' | 'paused' | 'finished' | 'not_started' | 'cancelled' | 'expired'
/** 一批经平台代理执行的写操作，只有元数据与各状态的项数；每一项的请求与结果不在这里。 */
export interface SystemBatch {
  id: string; phase: SystemBatchPhase
  /** pause_trial：试跑前几项后等提问人确认；pause_failures：连续失败自动暂停。 */
  pause_reason: 'pause_trial' | 'pause_failures' | null
  /** 已请求停止、还没收尾。 */
  stopping: boolean
  bot_id: string; bot_name: string | null; user_id: string | null; user_name: string | null
  system: string | null; system_name: string | null; operation_id: string | null; operation_summary: string; risk: string | null
  count: number; ok: number; failed: number; unknown: number; skipped: number; pending: number
  created_at: string; started_at: string | null; finished_at: string | null
  /** 当前用户能否停止：管理员和发起人本人。 */
  can_stop?: boolean
}
export type SystemBatchItemFilter = 'ok' | 'failed' | 'unknown' | 'skipped' | 'pending'
/** 批次里的一项：暂存时的写入参数原样，以及执行状态和结果摘要（最多约 300 字）。 */
export interface SystemBatchItem {
  seq: number; target: string | null
  path_params: Record<string, unknown> | null; query: Record<string, unknown> | null; body: unknown; before: Record<string, unknown> | null
  status: 'pending' | 'sending' | 'ok' | 'http_error' | 'unreachable' | 'unknown' | 'skipped'
  status_code: number | null; brief: string | null; started_at: string | null; finished_at: string | null
}
export const systemBatches = {
  list: (page = 1, perPage = 20) => call<Page<SystemBatch>>(http.get('/api/admin/system-batches', { params: { page, per_page: perPage } })),
  stop: (id: string) => call<SystemBatch>(http.post(`/api/admin/system-batches/${id}/stop`)),
  items: (id: string, page = 1, perPage = 50, status?: SystemBatchItemFilter) => call<Page<SystemBatchItem>>(http.get(`/api/admin/system-batches/${id}/items`, { params: { page, per_page: perPage, status } })),
}
export const credentials = {
  clients: (page = 1) => call<Page<ApiClient>>(http.get('/api/admin/api-clients', { params: { page } })),
  create: (body: { app_key: string; name: string; scopes: string[]; enabled: boolean }) => call<ApiClient & { secret: string }>(http.post('/api/admin/api-clients', body)),
  update: (row: ApiClient) => call<ApiClient>(http.put(`/api/admin/api-clients/${row.app_key}`, { name: row.name, scopes: row.scopes, enabled: row.enabled }, { headers: { 'If-Match': String(row.version) } })),
  rotate: (row: ApiClient) => call<ApiClient & { secret: string }>(http.post(`/api/admin/api-clients/${row.app_key}/rotate-secret`, {}, { headers: { 'If-Match': String(row.version) } })),
  keys: () => call<JwtKey[]>(http.get('/api/admin/jwt-keys')),
  rotateKey: () => call<{ kid: string }>(http.post('/api/admin/jwt-keys/rotate')),
}
