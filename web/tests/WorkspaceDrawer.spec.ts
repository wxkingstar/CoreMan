import { Blob as NodeBlob } from 'node:buffer'
import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus from 'element-plus'
import { i18n } from '@/i18n'
import { ApiError } from '@/api/client'
vi.mock('@/api/workspace', () => ({ workspace: { get: vi.fn().mockResolvedValue({ directory: '/home/ai/a', state: 'ready', branch: 'main' }), list: vi.fn().mockResolvedValue({ entries: [] }), read: vi.fn().mockResolvedValue({ content: 'original', data: 'b3JpZ2luYWw=', hash: 'h1', editable: true, size: 8, eof: true }), write: vi.fn(), gitStatus: vi.fn().mockResolvedValue({ files: [] }) } }))
import { workspace } from '@/api/workspace'
import WorkspaceDrawer from '@/components/WorkspaceDrawer.vue'
describe('WorkspaceDrawer', () => {
  beforeEach(() => { vi.clearAllMocks(); vi.stubGlobal('Blob', NodeBlob) })
  it('keeps draft and original hash when concurrent modification rejects saving', async () => {
    vi.mocked(workspace.write).mockRejectedValue(new ApiError(409, 409, 'File changed'))
    const wrapper = mount(WorkspaceDrawer, { props: { botId: 'b1', visible: true }, global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { openFile: (p: string) => Promise<void>; draft: string; save: () => Promise<void> }
    await vm.openFile('AGENTS.md')
    vm.draft = 'my edits'
    await vm.save()
    expect(vm.draft).toBe('my edits')
    expect(workspace.write).toHaveBeenCalledWith('b1', { path: 'AGENTS.md', content: 'my edits', expected_hash: 'h1' })
    wrapper.unmount()
  })
  it('reads all chunks before editing and advances the expected hash after saving', async () => {
    vi.mocked(workspace.read).mockResolvedValueOnce({ content: 'first', data: btoa('first'), hash: 'h1', editable: true, size: 11, eof: false }).mockResolvedValueOnce({ content: 'second', data: btoa('second'), hash: 'h1', editable: true, size: 11, eof: true })
    vi.mocked(workspace.write).mockResolvedValueOnce({ hash: 'h2' }).mockResolvedValueOnce({ hash: 'h3' })
    const wrapper = mount(WorkspaceDrawer, { props: { botId: 'b1', visible: true }, global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { openFile: (p: string) => Promise<void>; draft: string; save: () => Promise<void> }
    await vm.openFile('notes.txt')
    expect(vm.draft).toBe('firstsecond')
    expect(workspace.read).toHaveBeenLastCalledWith('b1', 'notes.txt', 5)
    vm.draft = 'edit one'
    await vm.save()
    vm.draft = 'edit two'
    await vm.save()
    expect(workspace.write).toHaveBeenLastCalledWith('b1', { path: 'notes.txt', content: 'edit two', expected_hash: 'h2' })
    wrapper.unmount()
  })

})
