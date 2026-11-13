import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/myAssistant', async (original) => ({
  ...(await original<typeof import('@/api/myAssistant')>()),
  myAssistant: { notes: vi.fn(), page: vi.fn(), savePage: vi.fn(), deletePage: vi.fn(), regenerateVoice: vi.fn() },
}))

import { ApiError } from '@/api/client'
import { myAssistant, type NoteEntry, type NotePage } from '@/api/myAssistant'
import AssistantNotes from '@/components/assistant/AssistantNotes.vue'
import { i18n } from '@/i18n'

const t = i18n.global.t
const entry = (over: Partial<NoteEntry>): NoteEntry => ({
  path: '规则/口吻', title: '口吻', summary: '口吻规则', pinned: true, version: 3, size: 120,
  updated_by: 'assistant', updated_at: '2026-11-12T08:00:00Z', ...over,
})
const page = (over: Partial<NotePage>): NotePage => ({ ...entry({}), body: '回复要短。', ...over })
interface Exposed {
  editor: { open: boolean; isNew: boolean; path: string; title: string; summary: string; body: string; pinned: boolean; version: number }
}
const render = () => mount(AssistantNotes, { global: { plugins: [ElementPlus, i18n], stubs: { AssistantTransfer: true } } })
const editor = (wrapper: ReturnType<typeof render>) => (wrapper.vm as unknown as Exposed).editor

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(myAssistant.notes).mockReset().mockResolvedValue({
    items: [entry({ path: '人/李四', title: '李四', summary: '采购负责人', pinned: false, updated_by: 'owner' }), entry({})],
    locked: false,
  })
  vi.mocked(myAssistant.page).mockReset().mockResolvedValue(page({}))
  vi.mocked(myAssistant.savePage).mockReset().mockResolvedValue(entry({ version: 4, updated_by: 'owner' }))
  vi.mocked(myAssistant.deletePage).mockReset().mockResolvedValue({ deleted: true })
  vi.mocked(myAssistant.regenerateVoice).mockReset().mockResolvedValue({ queued: true, left_today: 2 })
  vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
})

describe('knowledge base', () => {
  it('offers import and export, reloads after an import, and hides both when locked', async () => {
    const wrapper = render(); await flushPromises()
    const transfer = wrapper.getComponent({ name: 'AssistantTransfer' })
    transfer.vm.$emit('imported'); await flushPromises()
    expect(myAssistant.notes).toHaveBeenCalledTimes(2)
    wrapper.unmount()
    vi.mocked(myAssistant.notes).mockResolvedValue({ items: [], locked: true })
    const locked = render(); await flushPromises()
    expect(locked.findComponent({ name: 'AssistantTransfer' }).exists()).toBe(false)
    locked.unmount()
  })

  it('lists pages, marks pinned ones clearly and says who changed each last', async () => {
    const wrapper = render(); await flushPromises()
    expect(wrapper.text()).toContain(t('assistant.notes.intro'))
    expect(wrapper.find('[data-test="pinned-0"]').exists()).toBe(false)
    const pinned = wrapper.get('[data-test="pinned-1"]')
    expect(pinned.text()).toBe(t('assistant.notes.pinned'))
    expect(pinned.attributes('title')).toBe(t('assistant.notes.pinnedHint'))
    expect(wrapper.get('[data-test="page-0"]').text()).toContain(t('assistant.notes.updatedBy', { name: t('assistant.notes.authors.owner') }))
    expect(wrapper.get('[data-test="page-1"]').text()).toContain(t('assistant.notes.updatedBy', { name: t('assistant.notes.authors.assistant') }))
    expect(wrapper.find('[data-test="notes-locked"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('says the content can no longer be read when the index is locked', async () => {
    vi.mocked(myAssistant.notes).mockResolvedValue({ items: [], locked: true })
    const wrapper = render(); await flushPromises()
    expect(wrapper.get('[data-test="notes-locked"]').text()).toContain(t('assistant.page.locked'))
    expect(wrapper.text()).not.toContain(t('assistant.notes.empty'))
    wrapper.unmount()
  })

  it('opens a page to read it', async () => {
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="open-1"]').trigger('click'); await flushPromises()
    expect(myAssistant.page).toHaveBeenCalledWith('规则/口吻')
    expect(wrapper.get('[data-test="viewer"]').text()).toContain('回复要短。')
    expect(wrapper.find('[data-test="editor"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('edits a pinned page and saves it with the version it read', async () => {
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="edit-1"]').trigger('click'); await flushPromises()
    expect(editor(wrapper)).toMatchObject({ open: true, isNew: false, path: '规则/口吻', pinned: true, version: 3, body: '回复要短。' })
    editor(wrapper).body = '回复要短，先说结论。'
    await wrapper.get('[data-test="save-page"]').trigger('click'); await flushPromises()
    expect(myAssistant.savePage).toHaveBeenCalledWith({
      path: '规则/口吻', body: '回复要短，先说结论。', summary: '口吻规则', title: '口吻', pinned: true, version: 3,
    })
    expect(editor(wrapper).open).toBe(false)
    expect(myAssistant.notes).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('creates a page with version 0', async () => {
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="create"]').trigger('click'); await flushPromises()
    expect(editor(wrapper)).toMatchObject({ open: true, isNew: true, version: 0, pinned: false })
    Object.assign(editor(wrapper), { path: ' 项目/续约 ', body: '明年一月到期。', summary: '续约进展', pinned: true })
    await wrapper.get('[data-test="save-page"]').trigger('click'); await flushPromises()
    expect(myAssistant.savePage).toHaveBeenCalledWith({
      path: '项目/续约', body: '明年一月到期。', summary: '续约进展', title: null, pinned: true, version: 0,
    })
    wrapper.unmount()
  })

  it('refuses to save without a path or a body', async () => {
    const warning = vi.spyOn(ElMessage, 'warning').mockReturnValue({ close: () => {} })
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="create"]').trigger('click'); await flushPromises()
    Object.assign(editor(wrapper), { path: '项目/续约', body: '   ' })
    await wrapper.get('[data-test="save-page"]').trigger('click'); await flushPromises()
    expect(myAssistant.savePage).not.toHaveBeenCalled()
    expect(warning).toHaveBeenCalledWith(t('assistant.notes.required'))
    wrapper.unmount()
  })

  it('reloads the latest content on a version conflict instead of overwriting', async () => {
    const warning = vi.spyOn(ElMessage, 'warning').mockReturnValue({ close: () => {} })
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="edit-1"]').trigger('click'); await flushPromises()
    editor(wrapper).body = '我改的'
    vi.mocked(myAssistant.savePage).mockRejectedValueOnce(new ApiError(409, 40901, '这一页已被改动，请重新打开后再保存'))
    vi.mocked(myAssistant.page).mockResolvedValueOnce(page({ version: 5, body: '助理刚写的' }))
    await wrapper.get('[data-test="save-page"]').trigger('click'); await flushPromises()
    expect(warning).toHaveBeenCalledWith(t('assistant.notes.conflict'))
    expect(editor(wrapper)).toMatchObject({ open: true, version: 5, body: '助理刚写的' })
    expect(myAssistant.savePage).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('deletes a page only after confirmation', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel').mockResolvedValueOnce('confirm' as never)
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="remove-1"]').trigger('click'); await flushPromises()
    expect(myAssistant.deletePage).not.toHaveBeenCalled()
    await wrapper.get('[data-test="remove-1"]').trigger('click'); await flushPromises()
    expect(confirm.mock.calls[1][0]).toBe(t('assistant.notes.removeConfirm', { name: '规则/口吻' }))
    expect(myAssistant.deletePage).toHaveBeenCalledWith('规则/口吻')
    wrapper.unmount()
  })

  it('shows page content as plain text, never as markup or links', async () => {
    vi.mocked(myAssistant.notes).mockResolvedValue({
      items: [entry({ title: '<img src=x onerror=alert(1)>', summary: '[点这里](https://evil.example.com)' })],
      locked: false,
    })
    vi.mocked(myAssistant.page).mockResolvedValue(page({ body: '<script>alert(1)</script> [链接](https://evil.example.com)' }))
    const wrapper = render(); await flushPromises()
    const row = wrapper.get('[data-test="page-0"]')
    expect(row.find('img').exists()).toBe(false)
    expect(row.find('a').exists()).toBe(false)
    expect(row.text()).toContain('[点这里](https://evil.example.com)')
    await wrapper.get('[data-test="open-0"]').trigger('click'); await flushPromises()
    const viewer = wrapper.get('[data-test="viewer"]')
    expect(viewer.find('script').exists()).toBe(false)
    expect(viewer.find('a').exists()).toBe(false)
    expect(viewer.text()).toContain('<script>alert(1)</script> [链接](https://evil.example.com)')
    wrapper.unmount()
  })

  it('regenerates the voice page only after confirmation and says how many are left', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel')
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="regenerate-voice"]').trigger('click'); await flushPromises()
    expect(myAssistant.regenerateVoice).not.toHaveBeenCalled()
    confirm.mockResolvedValueOnce('confirm' as never)
    await wrapper.get('[data-test="regenerate-voice"]').trigger('click'); await flushPromises()
    expect(myAssistant.regenerateVoice).toHaveBeenCalledTimes(1)
    expect(ElMessage.success).toHaveBeenCalledWith(t('assistant.notes.regenerated', { count: 2 }))
    wrapper.unmount()
  })

  it('shows why the voice page cannot be regenerated', async () => {
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const failed = vi.spyOn(ElMessage, 'error').mockReturnValue({ close: () => {} })
    vi.mocked(myAssistant.regenerateVoice).mockRejectedValue(new Error('今天已经重新生成过 3 次了，明天再试'))
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="regenerate-voice"]').trigger('click'); await flushPromises()
    expect(failed).toHaveBeenCalledWith('今天已经重新生成过 3 次了，明天再试')
    wrapper.unmount()
  })
})
