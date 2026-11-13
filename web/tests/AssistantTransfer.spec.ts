import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/myAssistant', async (original) => ({
  ...(await original<typeof import('@/api/myAssistant')>()),
  myAssistant: { importArchive: vi.fn(), exportArchive: vi.fn() },
}))
vi.mock('@/utils/download', () => ({ saveBlob: vi.fn() }))

import { ApiError } from '@/api/client'
import { myAssistant, type ImportPlan } from '@/api/myAssistant'
import AssistantTransfer from '@/components/assistant/AssistantTransfer.vue'
import { i18n } from '@/i18n'
import { saveBlob } from '@/utils/download'

const t = i18n.global.t
const plan = (over: Partial<ImportPlan>): ImportPlan => ({
  dry_run: true, digest: 'd'.repeat(64), ignored: 0,
  create: [{ path: '规则/口吻', pinned: true }, { path: '人/李四', pinned: false }],
  overwrite: [{ path: '项目/续约', pinned: false }],
  skip: [{ path: '旧页', reason: 'exists' }, { path: '怪页', reason: 'brand_new_reason' }],
  followups: { create: 1, skip: [{ index: 2, reason: 'duplicate' }] },
  ...over,
})
const file = new File(['zip'], 'notes.zip', { type: 'application/zip' })
interface Exposed { pick: (chosen: File | null) => void; setOverwrite: (value: boolean) => void }
const render = () => mount(AssistantTransfer, { global: { plugins: [ElementPlus, i18n] } })
const exposed = (wrapper: ReturnType<typeof render>) => wrapper.vm as unknown as Exposed
/** 打开导入面板、选好文件、点预览。 */
async function previewed(wrapper: ReturnType<typeof render>) {
  await wrapper.get('[data-test="import-open"]').trigger('click')
  exposed(wrapper).pick(file); await flushPromises()
  await wrapper.get('[data-test="import-preview"]').trigger('click'); await flushPromises()
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(myAssistant.importArchive).mockReset().mockResolvedValue(plan({}))
  vi.mocked(myAssistant.exportArchive).mockReset().mockResolvedValue(new Blob(['zip']))
  vi.mocked(saveBlob).mockReset()
  vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
  vi.spyOn(ElMessage, 'error').mockReturnValue({ close: () => {} })
})

describe('import and export', () => {
  it('asks for a file before previewing', async () => {
    const warning = vi.spyOn(ElMessage, 'warning').mockReturnValue({ close: () => {} })
    const wrapper = render()
    expect(wrapper.find('[data-test="import-panel"]').exists()).toBe(false)
    await wrapper.get('[data-test="import-open"]').trigger('click')
    expect(wrapper.get('[data-test="import-panel"]').text()).toContain(t('assistant.notes.importHint'))
    await wrapper.get('[data-test="import-preview"]').trigger('click'); await flushPromises()
    expect(warning).toHaveBeenCalledWith(t('assistant.notes.importChoose'))
    expect(myAssistant.importArchive).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('previews first and shows what would be created, replaced and skipped', async () => {
    const wrapper = render(); await previewed(wrapper)
    expect(myAssistant.importArchive).toHaveBeenCalledWith(file, { dryRun: true, overwrite: false })
    const text = wrapper.get('[data-test="import-plan"]').text()
    expect(text).toContain(t('assistant.notes.importCreate', { count: 2 }))
    expect(text).toContain(t('assistant.notes.importReplace', { count: 1 }))
    expect(text).toContain(t('assistant.notes.importSkip', { count: 3 }))
    expect(text).toContain(t('assistant.notes.importFollowups', { count: 1 }))
    expect(wrapper.get('[data-test="import-create"]').text()).toContain('规则/口吻')
    expect(wrapper.get('[data-test="import-create"]').text()).toContain(t('assistant.notes.importPinned'))
    expect(wrapper.get('[data-test="import-replace"]').text()).toContain('项目/续约')
    const skipped = wrapper.get('[data-test="import-skip"]').text()
    expect(skipped).toContain(t('assistant.notes.importReasons.exists'))
    // 还没有文案的原因代码原样显示。
    expect(skipped).toContain('brand_new_reason')
    expect(skipped).toContain(t('assistant.notes.importFollowupSkip', { n: 2 }))
    expect(skipped).toContain(t('assistant.notes.importReasons.duplicate'))
    // 有页面会置顶：单独提醒一句。
    expect(wrapper.get('[data-test="import-pinned-warning"]').text()).toContain(t('assistant.notes.importPinnedWarning', { count: 1 }))
    wrapper.unmount()
  })

  it('says how many system files were ignored, and stays quiet when there were none', async () => {
    const wrapper = render(); await previewed(wrapper)
    expect(wrapper.find('[data-test="import-ignored"]').exists()).toBe(false)
    vi.mocked(myAssistant.importArchive).mockResolvedValue(plan({ ignored: 3 }))
    await wrapper.get('[data-test="import-preview"]').trigger('click'); await flushPromises()
    expect(wrapper.get('[data-test="import-ignored"]').text()).toBe(t('assistant.notes.importIgnored', { count: 3 }))
    wrapper.unmount()
  })

  it('confirms with the digest it previewed and tells the parent to reload', async () => {
    const wrapper = render(); await previewed(wrapper)
    await wrapper.get('[data-test="import-confirm"]').trigger('click'); await flushPromises()
    expect(myAssistant.importArchive).toHaveBeenLastCalledWith(file, { dryRun: false, overwrite: false, expect: 'd'.repeat(64) })
    expect(wrapper.emitted('imported')).toHaveLength(1)
    expect(wrapper.find('[data-test="import-panel"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('throws the preview away when the file or the overwrite choice changes', async () => {
    const wrapper = render(); await previewed(wrapper)
    exposed(wrapper).setOverwrite(true); await flushPromises()
    expect(wrapper.find('[data-test="import-plan"]').exists()).toBe(false)
    await wrapper.get('[data-test="import-preview"]').trigger('click'); await flushPromises()
    expect(myAssistant.importArchive).toHaveBeenLastCalledWith(file, { dryRun: true, overwrite: true })
    exposed(wrapper).pick(new File(['other'], 'other.zip')); await flushPromises()
    expect(wrapper.find('[data-test="import-confirm"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('previews again instead of importing when the server says the plan changed', async () => {
    const wrapper = render(); await previewed(wrapper)
    vi.mocked(myAssistant.importArchive)
      .mockRejectedValueOnce(new ApiError(409, 409, '预览之后内容有变化，请核对新的清单后再确认'))
      .mockResolvedValueOnce(plan({ digest: 'e'.repeat(64), create: [] }))
    await wrapper.get('[data-test="import-confirm"]').trigger('click'); await flushPromises()
    expect(ElMessage.error).toHaveBeenCalledWith('预览之后内容有变化，请核对新的清单后再确认')
    expect(myAssistant.importArchive).toHaveBeenCalledTimes(3)
    expect(myAssistant.importArchive).toHaveBeenLastCalledWith(file, { dryRun: true, overwrite: false })
    expect(wrapper.emitted('imported')).toBeUndefined()
    expect(wrapper.get('[data-test="import-plan"]').text()).toContain(t('assistant.notes.importCreate', { count: 0 }))
    wrapper.unmount()
  })

  it('will not confirm a plan that imports nothing', async () => {
    vi.mocked(myAssistant.importArchive).mockResolvedValue(plan({ create: [], overwrite: [], followups: { create: 0, skip: [] } }))
    const wrapper = render(); await previewed(wrapper)
    expect(wrapper.get('[data-test="import-plan"]').text()).toContain(t('assistant.notes.importNothing'))
    expect(wrapper.get('[data-test="import-confirm"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-test="import-pinned-warning"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('downloads the export', async () => {
    const wrapper = render()
    await wrapper.get('[data-test="export"]').trigger('click'); await flushPromises()
    expect(myAssistant.exportArchive).toHaveBeenCalledTimes(1)
    expect(saveBlob).toHaveBeenCalledWith(expect.any(Blob), 'assistant-export.zip')
    expect(ElMessage.success).toHaveBeenCalledWith(t('assistant.notes.exported'))
    wrapper.unmount()
  })
})
