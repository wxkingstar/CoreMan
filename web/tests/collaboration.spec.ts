import { afterEach, expect, it, vi } from 'vitest'
import { http, ApiError } from '@/api/client'
import { collaboration } from '@/api/collaboration'
afterEach(() => vi.restoreAllMocks())
it('saves only a target and sends revision headers for updates and removal', async () => {
 const post = vi.spyOn(http, 'post').mockResolvedValue({ data: { data: {} } }); const patch = vi.spyOn(http, 'patch').mockResolvedValue({ data: { data: {} } }); const remove = vi.spyOn(http, 'delete').mockResolvedValue({ status: 204, data: '' })
 await collaboration.create('source', 'peer'); await collaboration.update('source', 'route', true, 8); await collaboration.remove('source', 'route', 9)
 expect(post).toHaveBeenCalledWith('/api/admin/bots/source/collaborators', { target_bot_id: 'peer' })
 expect(patch).toHaveBeenCalledWith('/api/admin/bots/source/collaborators/route', { enabled: true }, { headers: { 'If-Match': '"8"' } }); expect(remove).toHaveBeenCalledWith('/api/admin/bots/source/collaborators/route', { headers: { 'If-Match': '"9"' } })
})
it('preserves structured removal errors', async () => {
 vi.spyOn(http, 'delete').mockRejectedValue({ response: { status: 409, data: { code: 40901, message: 'Changed' } } }); await expect(collaboration.remove('source', 'route', 1)).rejects.toEqual(new ApiError(409, 40901, 'Changed'))
})
it('unwraps partners and search results', async () => {
 const row = { id: 'route', target_bot_id: 'peer', target_name: 'Partner', target_description: null, enabled: true, version: 1, status: 'ready', reason: null, can_enable: true, can_remove: true, active_count: 0 }; const peers = [{ id: 'peer', name: 'Partner', description: null, enabled: true, available: true }]
 const envelope = (data: unknown) => ({ data: { code: 0, data } }); const get = vi.spyOn(http, 'get').mockResolvedValueOnce(envelope([row])).mockResolvedValueOnce(envelope(peers)); vi.spyOn(http, 'post').mockResolvedValueOnce(envelope(row))
 expect(await collaboration.list('source')).toEqual([row]); expect(await collaboration.options('source', 'Partner')).toEqual(peers); expect(await collaboration.create('source', 'peer')).toEqual(row)
 expect(get).toHaveBeenNthCalledWith(2, '/api/admin/bots/source/collaborator-options', { params: { q: 'Partner' }, signal: undefined })
})
