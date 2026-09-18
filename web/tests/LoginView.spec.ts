import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'

vi.mock('@/api/client', () => ({
  api: { me: vi.fn().mockRejectedValue(Object.assign(new Error('x'), { status: 401 })), bootstrapLogin: vi.fn(), logout: vi.fn() },
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: number, message: string) { super(message) }
  },
}))
vi.mock('@/api/admin', () => ({
  auth: { providers: vi.fn().mockResolvedValue({ wecom: true, feishu: false }) },
}))

import { auth as authApi } from '@/api/admin'
import { api } from '@/api/client'
import { i18n } from '@/i18n'
import LoginView from '@/views/LoginView.vue'

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
      { path: '/', name: 'home', component: { template: '<div>home</div>' } },
    ],
  })
}

describe('LoginView', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('shows validation errors when submitting empty form', async () => {
    const router = makeRouter()
    const wrapper = mount(LoginView, { global: { plugins: [ElementPlus, i18n, router] } })
    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()
    // Element Plus 的表单校验错误文案由 @vueuse/core 的 refDebounced(validateState, 100) 控制展示时机，
    // flushPromises()/nextTick() 只冲刷微任务队列，追不上这个真实的 100ms 防抖计时器，需要真实等待。
    await new Promise((resolve) => setTimeout(resolve, 150))
    await nextTick()
    expect(wrapper.text()).toContain('请输入用户名')
    expect(api.bootstrapLogin).not.toHaveBeenCalled()
  })

  it('logs in and navigates to redirect target', async () => {
    const router = makeRouter()
    await router.push('/login?redirect=/')
    await router.isReady()
    vi.mocked(api.bootstrapLogin).mockResolvedValueOnce({
      user: {
        id: 'u', login_name: 'admin', display_name: 'admin', role: 'platform_admin', locale: 'zh',
        email: null, avatar_url: null, source: 'bootstrap', team_id: null,
      },
    })
    const wrapper = mount(LoginView, { global: { plugins: [ElementPlus, i18n, router] } })
    await wrapper.get('[data-test="username"]').setValue('admin')
    await wrapper.get('[data-test="password"]').setValue('pw')
    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()
    expect(api.bootstrapLogin).toHaveBeenCalledWith('admin', 'pw')
    expect(router.currentRoute.value.path).toBe('/')
  })

  it('ignores repeated submits while the first one is in flight', async () => {
    // 回车提交不经过按钮的 loading 态，只有 submit() 自己防重入才不会重复登录
    let release = () => {}
    vi.mocked(api.bootstrapLogin).mockClear() // mock 在用例间共享，调用次数要先归零
    vi.mocked(api.bootstrapLogin).mockReturnValueOnce(new Promise((resolve) => {
      release = () => resolve({
        user: {
          id: 'u', login_name: 'admin', display_name: 'admin', role: 'platform_admin', locale: 'zh',
          email: null, avatar_url: null, source: 'bootstrap', team_id: null,
        },
      })
    }))
    const wrapper = mount(LoginView, { global: { plugins: [ElementPlus, i18n, makeRouter()] } })
    await wrapper.get('[data-test="username"]').setValue('admin')
    const password = wrapper.get('[data-test="password"]')
    await password.setValue('pw')
    await password.trigger('keyup.enter')
    await flushPromises()
    await password.trigger('keyup.enter')
    await flushPromises()
    expect(api.bootstrapLogin).toHaveBeenCalledTimes(1)
    release()
    await flushPromises()
  })

  it('platform buttons are disabled placeholders', () => {
    const wrapper = mount(LoginView, { global: { plugins: [ElementPlus, i18n, makeRouter()] } })
    expect(wrapper.get('[data-test="wecom"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-test="feishu"]').attributes('disabled')).toBeDefined()
  })

  it('enables WeCom button when provider available and redirects to start url', async () => {
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
    const router = makeRouter()
    await router.push('/login?redirect=%2Fusers')
    const wrapper = mount(LoginView, { global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    const btn = wrapper.get('[data-test="wecom"]')
    expect(btn.attributes('disabled')).toBeUndefined()
    await btn.trigger('click')
    expect(assign).toHaveBeenCalledWith('/api/auth/wecom/start?mode=qr&redirect=%2Fusers')
    vi.unstubAllGlobals()
  })

  it('shows error message from query', async () => {
    const router = makeRouter()
    await router.push('/login?error=user_not_found')
    const wrapper = mount(LoginView, { global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    expect(wrapper.text()).toContain(i18n.global.t('login.errors.user_not_found'))
  })

  it('does not auto-redirect to wecom oauth in wxwork UA when the page carries an error, but does when clean', async () => {
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
    vi.stubGlobal('navigator', { ...window.navigator, userAgent: 'Mozilla/5.0 wxwork/4.1' })

    const errorRouter = makeRouter()
    await errorRouter.push('/login?error=user_not_found')
    const errorWrapper = mount(LoginView, { global: { plugins: [ElementPlus, i18n, errorRouter] } })
    await flushPromises()
    expect(assign).not.toHaveBeenCalled()
    expect(errorWrapper.text()).toContain(i18n.global.t('login.errors.user_not_found'))

    const cleanRouter = makeRouter()
    await cleanRouter.push('/login')
    mount(LoginView, { global: { plugins: [ElementPlus, i18n, cleanRouter] } })
    await flushPromises()
    expect(assign).toHaveBeenCalledWith('/api/auth/wecom/start?mode=oauth&redirect=%2F')

    vi.unstubAllGlobals()
  })

  it('auto-starts Feishu login inside the Feishu client and keeps the session link', async () => {
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
    vi.stubGlobal('navigator', { ...window.navigator, userAgent: 'Mozilla/5.0 Lark/7.20.0 LarkLocale/zh_CN' })
    vi.mocked(authApi.providers).mockResolvedValueOnce({ wecom: true, feishu: true })
    const link = '/api/admin/runtime-nodes/n/claude/session/s?t=abc'
    const router = makeRouter()
    await router.push('/login?redirect=' + encodeURIComponent(link))
    mount(LoginView, { global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    expect(assign).toHaveBeenCalledWith('/api/auth/feishu/start?redirect=' + encodeURIComponent(link))
    vi.unstubAllGlobals()
  })

  it('opens server-rendered pages with a full navigation after password login', async () => {
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
    vi.mocked(api.bootstrapLogin).mockResolvedValueOnce({
      user: {
        id: 'u', login_name: 'admin', display_name: 'admin', role: 'member', locale: 'zh',
        email: null, avatar_url: null, source: 'bootstrap', team_id: null,
      },
    })
    const link = '/api/admin/runtime-nodes/n/claude/session/s?t=abc'
    const router = makeRouter()
    await router.push('/login?redirect=' + encodeURIComponent(link))
    await router.isReady()
    const wrapper = mount(LoginView, { global: { plugins: [ElementPlus, i18n, router] } })
    await wrapper.get('[data-test="username"]').setValue('admin')
    await wrapper.get('[data-test="password"]').setValue('pw')
    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()
    expect(assign).toHaveBeenCalledWith(link)
    expect(router.currentRoute.value.path).toBe('/login')
    vi.unstubAllGlobals()
  })
})
