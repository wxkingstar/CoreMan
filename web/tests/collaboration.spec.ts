import { afterEach, expect, it, vi } from 'vitest'
import { http } from '@/api/client'
import { collaboration } from '@/api/collaboration'
afterEach(() => vi.restoreAllMocks())
it('reads and sets whether other employees may ask for help', async () => {
 const data = { accept_bot_help: true, permission: 'granted', active_count: 0 }
 const envelope = { data: { code: 0, data } }
 const get = vi.spyOn(http, 'get').mockResolvedValue(envelope); const patch = vi.spyOn(http, 'patch').mockResolvedValue(envelope)
 expect(await collaboration.get('source')).toEqual(data); expect(await collaboration.set('source', false)).toEqual(data)
 expect(get).toHaveBeenCalledWith('/api/admin/bots/source/bot-help', { signal: undefined })
 expect(patch).toHaveBeenCalledWith('/api/admin/bots/source/bot-help', { enabled: false })
})
