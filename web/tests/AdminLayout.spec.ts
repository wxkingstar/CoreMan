import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

vi.mock('@/api/client', () => ({ api: { me: vi.fn(), bootstrapLogin: vi.fn(), logout: vi.fn() }, ApiError: class extends Error {} }))

import { i18n } from '@/i18n'
import AdminLayout, { MENU } from '@/layouts/AdminLayout.vue'
import { useAuthStore } from '@/stores/auth'

function mountAs(role: string) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: AdminLayout, children: [] }] })
  useAuthStore().user = { id: 'me', login_name: 'x', display_name: 'X', role, locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: null }
  return mount(AdminLayout, { global: { plugins: [ElementPlus, i18n, router] } })
}

describe('AdminLayout menu', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('shows role-gated items only to allowed roles', () => {
    const member = mountAs('member').text()
    expect(member).toContain(i18n.global.t('menu.bots'))
    expect(member).toContain(i18n.global.t('menu.relays'))
    expect(member).not.toContain(i18n.global.t('menu.settings'))
    expect(member).not.toContain(i18n.global.t('menu.audit'))
    const admin = mountAs('platform_admin').text()
    expect(admin).toContain(i18n.global.t('menu.settings'))
    expect(admin).toContain(i18n.global.t('menu.audit'))
    expect(MENU.find((m) => m.key === 'audit')?.roles).toEqual(['ai_committee', 'platform_admin'])
  })

  // 公告在 M3a 之前挂在「即将开放」里（灰掉、不可点）；后端开了之后必须变成真菜单，
  // 且只对 ai_committee / platform_admin 可见。
  it('opens the announcements entry to managers only', () => {
    const label = i18n.global.t('menu.announcements')
    for (const role of ['ai_committee', 'platform_admin']) {
      const items = mountAs(role).findAll('.el-menu-item').filter((el) => el.text() === label)
      // 只有一项：说明「即将开放」里那份占位已经去掉了，没有重复渲染。
      expect(items).toHaveLength(1)
      expect(items[0].classes()).not.toContain('is-disabled')
    }
    expect(mountAs('member').text()).not.toContain(label)
    expect(mountAs('team_lead').text()).not.toContain(label)
    const entry = MENU.find((m) => m.key === 'announcements')
    expect(entry?.path).toBe('/announcements')
    expect(entry?.roles).toEqual(['ai_committee', 'platform_admin'])
  })
})
