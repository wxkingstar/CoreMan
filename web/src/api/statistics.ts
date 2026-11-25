import { call, http } from './client'
import type { Backend } from './types'
export const tokenKeys = ['input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_creation_tokens'] as const
export type TokenKey = typeof tokenKeys[number]
export type Aggregate = { messages: number; users: number; bots: number; cost_usd: number | null; cost_usd_measured: number } & Record<TokenKey, number | null> & Record<`${TokenKey}_measured`, number>
/** 一行的 Token 合计：四类已计量用量相加，未计量按 0（与用户排行的 tokens 口径一致）。 */
export const totalTokens = (row: Partial<Record<TokenKey, number | null>>) => tokenKeys.reduce((sum, key) => sum + (row[key] ?? 0), 0)
export interface Statistics { start: string; end: string; timezone: string; total: Aggregate; daily: (Aggregate & { day: string })[]; by_bot: (Aggregate & { id: string; name: string })[]; by_user: (Aggregate & { id: string; name: string | null; avatar_url: string | null; tokens: number })[] }
export interface ModelPrice { provider: Backend; model: string; effective_from: string; input_usd: string | number; output_usd: string | number; cache_read_usd: string | number; cache_write_usd: string | number; version: number; source: 'builtin' | 'custom' }
export const statistics = {
  get: (params: { start?: string; end?: string; timezone: string }) => call<Statistics>(http.get('/api/admin/statistics', { params })),
  prices: () => call<ModelPrice[]>(http.get('/api/admin/model-prices')),
  savePrice: ({ version, provider, model, effective_from, input_usd, output_usd, cache_read_usd, cache_write_usd }: ModelPrice) => call<ModelPrice>(http.put('/api/admin/model-prices', { provider, model, effective_from, input_usd, output_usd, cache_read_usd, cache_write_usd }, { headers: { 'If-Match': String(version) } })),
}
