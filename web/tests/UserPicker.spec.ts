import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it, vi } from 'vitest'

vi.mock('@/api/admin', () => ({
  users: {
    list: vi.fn().mockResolvedValue({
      items: [
        { id: 'u1', display_name: '张三', login_name: 'zhangsan' },
        { id: 'u2', display_name: '李四', login_name: 'lisi' },
      ],
      total: 2,
      page: 1,
      per_page: 20,
    }),
  },
}))

import { users } from '@/api/admin'
import UserPicker from '@/components/UserPicker.vue'
import { i18n } from '@/i18n'

describe('UserPicker', () => {
  it('searches users remotely and excludes ids', async () => {
    const wrapper = mount(UserPicker, {
      props: { modelValue: [], exclude: ['u2'] },
      global: { plugins: [ElementPlus, i18n] },
    })
    await (wrapper.vm as unknown as { search: (kw: string) => Promise<void> }).search('张')
    await flushPromises()
    expect(users.list).toHaveBeenCalledWith(expect.objectContaining({ keyword: '张', status: 'active' }))
    const options = (wrapper.vm as unknown as { options: { id: string }[] }).options
    expect(options.map((o) => o.id)).toEqual(['u1'])
  })

  it('labels pre-selected users when initial arrives after mount', async () => {
    // 白名单对话框是先开、后加载的：initial 晚到时不认，已选项的 tag 就退化成一串裸 id。
    const wrapper = mount(UserPicker, {
      props: { modelValue: ['u9'], initial: [] },
      global: { plugins: [ElementPlus, i18n] },
    })
    await wrapper.setProps({ initial: [{ id: 'u9', display_name: '王九', login_name: 'wang' }] })
    await flushPromises()
    expect(wrapper.text()).toContain('王九 (wang)')
  })
})
