import { afterEach, expect, it, vi } from 'vitest'
import { http, ApiError } from '@/api/client'
import { collaboration } from '@/api/collaboration'
afterEach(() => vi.restoreAllMocks())
it('sends the bound revision for verify, enable and archive', async () => {
 const post = vi.spyOn(http, 'post').mockResolvedValue({ data: { data: {} } })
 const patch = vi.spyOn(http, 'patch').mockResolvedValue({ data: { data: {} } })
 const remove = vi.spyOn(http, 'delete').mockResolvedValue({ status: 204, data: '' })
 await collaboration.verify('source', 'route', 7); await collaboration.update('source', 'route', true, 8); await collaboration.remove('source', 'route', 9)
 expect(post).toHaveBeenCalledWith('/api/admin/bots/source/collaborators/route/verify', {}, { headers: { 'If-Match': '"7"' }, timeout: 35000 })
 expect(patch).toHaveBeenCalledWith('/api/admin/bots/source/collaborators/route', { enabled: true }, { headers: { 'If-Match': '"8"' } })
 expect(remove).toHaveBeenCalledWith('/api/admin/bots/source/collaborators/route', { headers: { 'If-Match': '"9"' } })
})
it('preserves standard structured errors for archive', async () => {
 vi.spyOn(http, 'delete').mockRejectedValue({ response: { status: 409, data: { code: 40901, message: 'Changed' } } })
 await expect(collaboration.remove('source', 'route', 1)).rejects.toEqual(new ApiError(409, 40901, 'Changed'))
})
it('unwraps route, option and group envelopes into usable values', async () => {
 const row = { id: 'route', target_bot_id: 'peer', target_name: 'Partner', target_description: null, chat_id: 'group', chat_name: 'Team', enabled: false, version: 1, status: 'pending', reason: null, can_enable: false, can_verify: false, can_remove: true, active_count: 0 }
 const peers = [{ id: 'peer', name: 'Partner', description: null, enabled: true, available: true }]
 const groups = [{ chat_id: 'group', name: 'Team' }]
 const envelope = (data: unknown) => ({ data: { code: 0, data } })
 const get = vi.spyOn(http, 'get').mockResolvedValueOnce(envelope([row])).mockResolvedValueOnce(envelope(peers)).mockResolvedValueOnce(envelope(groups))
 const post = vi.spyOn(http, 'post').mockResolvedValueOnce(envelope(row)).mockResolvedValueOnce(envelope({ ...row, version: 2 }))
 vi.spyOn(http, 'patch').mockResolvedValueOnce(envelope({ ...row, enabled: true, status: 'ready', version: 3 }))
 expect(await collaboration.list('source')).toEqual([row])
 expect(await collaboration.options('source', 'Partner')).toEqual(peers)
 expect(await collaboration.groups('source', 'peer')).toEqual(groups)
 expect(await collaboration.create('source', 'peer', 'group')).toEqual(row)
 expect(await collaboration.verify('source', 'route', 1)).toEqual({ ...row, version: 2 })
 expect(await collaboration.update('source', 'route', true, 2)).toEqual({ ...row, enabled: true, status: 'ready', version: 3 })
 expect(get).toHaveBeenNthCalledWith(2, '/api/admin/bots/source/collaborator-options', { params: { q: 'Partner' }, signal: undefined, timeout: 35000 })
 expect(get).toHaveBeenNthCalledWith(3, '/api/admin/bots/source/collaborator-groups', { params: { target_bot_id: 'peer' }, signal: undefined, timeout: 35000 })
 expect(post).toHaveBeenNthCalledWith(1, '/api/admin/bots/source/collaborators', { target_bot_id: 'peer', chat_id: 'group' }, { timeout: 35000 })
})
