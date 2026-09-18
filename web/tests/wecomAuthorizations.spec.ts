import { describe, expect, it, vi } from 'vitest'
vi.mock('@/api/client', () => ({ http: { get: vi.fn().mockResolvedValue({ data: { items: [] } }), delete: vi.fn().mockResolvedValue({ data: { ok: true } }) }, call: (response: Promise<{ data: unknown }>) => response.then(r => r.data) }))
import { http } from '@/api/client'
import { wecomAuthorizations } from '@/api/wecomAuthorizations'
describe('own WeCom connections API', () => {
  it('uses session-owned endpoints and encodes the bot identifier', async () => {
    expect(await wecomAuthorizations.list()).toEqual({ items: [] })
    expect(http.get).toHaveBeenCalledWith('/api/me/wecom-authorizations')
    await wecomAuthorizations.revoke('bot/1')
    expect(http.delete).toHaveBeenCalledWith('/api/me/wecom-authorizations/bot%2F1')
  })
})
