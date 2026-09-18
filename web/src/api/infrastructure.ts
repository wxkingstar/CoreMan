import { call, http } from '@/api/client'
import type { Page } from '@/api/types'

export interface BusinessSystem {
  key: string; name: string; description: string | null; base_url: string | null; sitemap_url: string | null
  enabled: boolean; sort_order: number; default_for_all_bots: boolean; allowed_bot_ids: string[] | null; version: number
}
export type SystemInput = Omit<BusinessSystem, 'version'>
export interface ApiClient {
  app_key: string; name: string; scopes: string[]; enabled: boolean; version: number; last_used_at: string | null; has_secret: boolean
}
// external：部署配置的外部签发方密钥（BOT_JWT_*），没有创建时间，也不参与轮换。
export interface JwtKey { kid: string; is_active: boolean; created_at: string | null; retired_at: string | null; external?: boolean }
export const systems = {
  list: (page = 1) => call<Page<BusinessSystem>>(http.get('/api/admin/systems', { params: { page } })),
  create: (body: SystemInput) => call<BusinessSystem>(http.post('/api/admin/systems', body)),
  update: (row: BusinessSystem, body: Omit<SystemInput, 'key'>) => call<BusinessSystem>(http.put(`/api/admin/systems/${row.key}`, body, { headers: { 'If-Match': String(row.version) } })),
  remove: (row: BusinessSystem) => call<null>(http.delete(`/api/admin/systems/${row.key}`, { headers: { 'If-Match': String(row.version) } })),
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
