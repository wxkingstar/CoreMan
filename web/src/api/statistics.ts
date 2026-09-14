import { call, http } from './client'
export const tokenKeys = ['input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_creation_tokens'] as const
export type TokenKey = typeof tokenKeys[number]
export type Aggregate = { messages: number; users: number; bots: number; cost_usd: number | null; cost_usd_measured: number } & Record<TokenKey, number | null> & Record<`${TokenKey}_measured`, number>
export interface Statistics { start: string; end: string; timezone: string; total: Aggregate; daily: (Aggregate & { day: string })[]; by_bot: (Aggregate & { id: string; name: string })[]; by_user: (Aggregate & { id: string; name: string | null })[] }
export interface ModelPrice { provider: 'claude' | 'codex'; model: string; effective_from: string; input_usd: string | number; output_usd: string | number; cache_read_usd: string | number; cache_write_usd: string | number; version: number }
export const statistics = {
  get: (params: { start?: string; end?: string; timezone: string }) => call<Statistics>(http.get('/api/admin/statistics', { params })),
  prices: () => call<ModelPrice[]>(http.get('/api/admin/model-prices')),
  savePrice: ({ version, ...body }: ModelPrice) => call<ModelPrice>(http.put('/api/admin/model-prices', body, { headers: { 'If-Match': String(version) } })),
}
