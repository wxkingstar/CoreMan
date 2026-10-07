import { call, http } from '@/api/client'
import type { Page } from '@/api/types'

export interface BusinessSystem {
  key: string; name: string; description: string | null; base_url: string | null; openapi_url: string | null
  /** openapi_url 的旧名，后端保留一个版本；管理台只用 openapi_url。 */
  sitemap_url?: string | null
  token_provider?: string; token_audience?: string | null; access_test_url?: string | null
  /** env：令牌按 BOT_TOKEN_<KEY> 下发；proxy：平台代理调用。 */
  token_delivery?: 'env' | 'proxy'
  enabled: boolean; sort_order: number; default_for_all_bots: boolean; allowed_bot_ids: string[] | null; version: number
  /** 只在保存的响应里出现：保存后立即拉取目录的结果，未配置 OpenAPI 地址或地址未改动时为 null。 */
  catalog?: SystemCatalog | CatalogSkipped | null
}
export type SystemInput = Omit<BusinessSystem, 'version' | 'catalog' | 'sitemap_url' | 'token_delivery'>
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
export const systems = {
  providers: () => call<TokenProvider[]>(http.get('/api/admin/token-providers')),
  list: (page = 1) => call<Page<BusinessSystem>>(http.get('/api/admin/systems', { params: { page } })),
  create: (body: SystemInput) => call<BusinessSystem>(http.post('/api/admin/systems', body)),
  update: (row: BusinessSystem, body: Omit<SystemInput, 'key'>) => call<BusinessSystem>(http.put(`/api/admin/systems/${row.key}`, body, { headers: { 'If-Match': String(row.version) } })),
  remove: (row: BusinessSystem) => call<null>(http.delete(`/api/admin/systems/${row.key}`, { headers: { 'If-Match': String(row.version) } })),
  catalog: (key: string) => call<SystemCatalog | null>(http.get(`/api/admin/systems/${key}/catalog`)),
  refreshCatalog: (key: string) => call<SystemCatalog | null>(http.post(`/api/admin/systems/${key}/catalog/refresh`)),
  grants: (id: string) => call<{ system_keys: string[]; version: number }>(http.get(`/api/admin/bots/${id}/system-grants`)),
  saveGrants: (id: string, keys: string[], version: number) => call<{ system_keys: string[]; version: number }>(http.put(`/api/admin/bots/${id}/system-grants`, { system_keys: keys }, { headers: { 'If-Match': String(version) } })),
}
export const credentials = {
  clients: (page = 1) => call<Page<ApiClient>>(http.get('/api/admin/api-clients', { params: { page } })),
  create: (body: { app_key: string; name: string; scopes: string[]; enabled: boolean }) => call<ApiClient & { secret: string }>(http.post('/api/admin/api-clients', body)),
  update: (row: ApiClient) => call<ApiClient>(http.put(`/api/admin/api-clients/${row.app_key}`, { name: row.name, scopes: row.scopes, enabled: row.enabled }, { headers: { 'If-Match': String(row.version) } })),
  rotate: (row: ApiClient) => call<ApiClient & { secret: string }>(http.post(`/api/admin/api-clients/${row.app_key}/rotate-secret`, {}, { headers: { 'If-Match': String(row.version) } })),
  keys: () => call<JwtKey[]>(http.get('/api/admin/jwt-keys')),
  rotateKey: () => call<{ kid: string }>(http.post('/api/admin/jwt-keys/rotate')),
}
