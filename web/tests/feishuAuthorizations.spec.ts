import { describe, expect, it, vi } from 'vitest'
vi.mock('@/api/client', () => ({ http: { get: vi.fn().mockResolvedValue({ data: { items: [] } }), delete: vi.fn().mockResolvedValue({ data: { ok: true } }) }, call: (response: Promise<{ data: unknown }>) => response.then(r => r.data) }))
import { http } from '@/api/client'
import { feishuAuthorizations } from '@/api/feishuAuthorizations'
describe('own Feishu authorizations API', () => {
  it('uses session-owned endpoints and encodes the bot identifier', async () => {
    expect(await feishuAuthorizations.list()).toEqual({ items: [] })
    expect(http.get).toHaveBeenCalledWith('/api/me/feishu-authorizations')
    await feishuAuthorizations.revoke('bot/1')
    expect(http.delete).toHaveBeenCalledWith('/api/me/feishu-authorizations/bot%2F1')
  })
})
