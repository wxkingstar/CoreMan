import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, expect, it, vi } from 'vitest'
vi.mock('@/api/memories', () => ({ memories: { list: vi.fn(), get: vi.fn(), update: vi.fn(), create: vi.fn(), remove: vi.fn(), collect: vi.fn(), deploy: vi.fn() } }))
import { memories } from '@/api/memories'
import BotMemories from '@/components/BotMemories.vue'
import { i18n } from '@/i18n'

beforeEach(() => vi.clearAllMocks())
it('fetches memory content only on open and carries its version into edits', async () => {
  const row = { id: 'm1', bot_id: 'b1', file_name: 'note.md', version: 3, deleted_at: null, file_mtime: null }
  vi.mocked(memories.list).mockResolvedValue([row] as never)
  vi.mocked(memories.get).mockResolvedValue({ ...row, content: 'private text' } as never)
  vi.mocked(memories.update).mockResolvedValue({ ...row, version: 4 } as never)
  const wrapper = mount(BotMemories, { props: { botId: 'b1' }, global: { plugins: [ElementPlus, i18n], stubs: { teleport: true } } })
  await flushPromises()
  expect(memories.list).not.toHaveBeenCalled()
  await wrapper.get('[data-test="memories-open"]').trigger('click'); await flushPromises()
  expect(memories.list).toHaveBeenCalledWith('b1', false)
  expect(memories.get).not.toHaveBeenCalled()
  await wrapper.get('[data-test="memory-edit-m1"]').trigger('click'); await flushPromises()
  expect(memories.get).toHaveBeenCalledWith('b1', 'm1')
  await wrapper.get('[data-test="memory-save"]').trigger('click'); await flushPromises()
  expect(memories.update).toHaveBeenCalledWith('b1', 'm1', expect.objectContaining({ file_name: 'note.md', content: 'private text' }), 3)
  wrapper.unmount()
})
