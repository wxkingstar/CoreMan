import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/admin', () => ({
  departments: { tree: vi.fn().mockResolvedValue([]) },
  users: { list: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 20 }) },
}))
vi.mock('@/api/feishuApps', () => ({ feishuApps: { visibility: vi.fn(), updateVisibility: vi.fn() } }))

import { feishuApps, type FeishuVisibility } from '@/api/feishuApps'
import FeishuAppVisibility from '@/components/feishuApp/FeishuAppVisibility.vue'
import { i18n } from '@/i18n'

function current(over: Partial<Extract<FeishuVisibility, { permission: 'granted' }>> = {}): FeishuVisibility {
  return {
    permission: 'granted',
    visible_to_all: false,
    users: [
      { feishu_id: 'fs_1', id: 'u1', display_name: '张三', login_name: 'zhangsan' },
      { feishu_id: 'fs_x', id: null, display_name: null, login_name: null },
    ],
    departments: [{ feishu_id: 'D1', id: 'd1', name: '销售部', path: '/D1' }],
    groups: [],
    invisible: { users: [], departments: [], groups: [] },
    ...over,
  }
}

function mountIt() {
  return mount(FeishuAppVisibility, { props: { botId: 'b1' }, global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
}

describe('FeishuAppVisibility', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
    vi.spyOn(ElMessage, 'error').mockReturnValue({ close: () => {} })
  })

  it('asks for the admin permissions before showing anything', async () => {
    vi.mocked(feishuApps.visibility).mockResolvedValue({ permission: 'missing' })
    const wrapper = mountIt()
    await flushPromises()
    expect(wrapper.text()).toContain(i18n.global.t('feishuApp.visibilityMissing'))
    await wrapper.get('[data-test="visibility-scan"]').trigger('click')
    expect(wrapper.emitted('scan')).toHaveLength(1)
    wrapper.unmount()

    vi.mocked(feishuApps.visibility).mockResolvedValue({ permission: 'pending' })
    const pending = mountIt()
    await flushPromises()
    expect(pending.text()).toContain(i18n.global.t('feishuApp.visibilityPending'))
    expect(pending.find('[data-test="visibility-scan"]').exists()).toBe(false)
    pending.unmount()
  })

  it('lists the live members and removes one incrementally', async () => {
    vi.mocked(feishuApps.visibility).mockResolvedValue(current())
    vi.mocked(feishuApps.updateVisibility).mockResolvedValue(current({ users: [] }))
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const wrapper = mountIt()
    await flushPromises()
    const users = wrapper.findAll('[data-test="visibility-user"]')
    expect(users.map((u) => u.text())).toEqual(['张三 (zhangsan)', i18n.global.t('feishuApp.visibilityUnknownUser', { id: 'fs_x' })])
    // 没同步进 CoreMan 的成员不能在这里移除。
    expect(users[1].find('.el-tag__close').exists()).toBe(false)
    await users[0].get('.el-tag__close').trigger('click')
    await flushPromises()
    expect(feishuApps.updateVisibility).toHaveBeenCalledWith('b1', { remove_user_ids: ['u1'] })
    expect(wrapper.findAll('[data-test="visibility-user"]')).toHaveLength(0)
    wrapper.unmount()
  })

  it('opens to everyone after confirmation', async () => {
    vi.mocked(feishuApps.visibility).mockResolvedValue(current())
    vi.mocked(feishuApps.updateVisibility).mockResolvedValue(current({ visible_to_all: true }))
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const wrapper = mountIt()
    await flushPromises()
    await wrapper.get('[data-test="visibility-open-all"]').trigger('click')
    await flushPromises()
    expect(feishuApps.updateVisibility).toHaveBeenCalledWith('b1', { visible_to_all: true })
    expect(wrapper.find('[data-test="visibility-user"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="visibility-restrict"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('requires someone to add before restricting an everyone-app', async () => {
    vi.mocked(feishuApps.visibility).mockResolvedValue(current({ visible_to_all: true, users: [], departments: [] }))
    const wrapper = mountIt()
    await flushPromises()
    expect(wrapper.find('[data-test="visibility-add"]').exists()).toBe(false)
    await wrapper.get('[data-test="visibility-restrict"]').trigger('click')
    await wrapper.get('[data-test="visibility-add"]').trigger('click')
    await flushPromises()
    expect(ElMessage.error).toHaveBeenCalledWith(i18n.global.t('feishuApp.visibilityRequired'))
    expect(feishuApps.updateVisibility).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
