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
  auth: {
    providers: vi.fn().mockResolvedValue({ wecom: false, feishu: false }),
    feishuQr: vi.fn(),
    wecomQr: vi.fn(),
  },
}))
const wecomPanel = vi.hoisted(() => ({ options: null as null | { params: Record<string, string>; onLoginSuccess: (r: { code: string }) => void } }))
vi.mock('@wecom/jssdk', () => ({
  createWWLoginPanel: vi.fn((options) => { wecomPanel.options = options; return { unmount: vi.fn() } }),
  WWLoginType: { corpApp: 'CorpApp' },
  WWLoginRedirectType: { callback: 'callback' },
  WWLoginPanelSizeType: { small: 'small' },
  WWLoginLangType: { zh: 'zh', en: 'en' },
  ColorScheme: { Light: 'light', Dark: 'dark' },
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

const FEISHU_GOTO = 'https://passport.feishu.cn/suite/passport/oauth/authorize?client_id=cli_x&state=s1'

describe('LoginView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    vi.mocked(authApi.feishuQr).mockResolvedValue({ goto: FEISHU_GOTO })
    vi.mocked(authApi.wecomQr).mockResolvedValue({
      appid: 'ww1', agentid: '1000002', redirect_uri: 'https://example.com/api/auth/wecom/callback', state: 'w1',
    })
  })

  it('shows validation errors when submitting empty form', async () => {
    const router = makeRouter()
    const wrapper = mount(LoginView, { global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
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
    await flushPromises()
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
    await flushPromises()
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

  it('shows only the password form when no scan login is configured', async () => {
    const wrapper = mount(LoginView, { global: { plugins: [ElementPlus, i18n, makeRouter()] } })
    await flushPromises()
    expect(wrapper.find('[data-test="username"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="feishu-qr"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="back-to-scan"]').exists()).toBe(false)
  })

  it('leads with the embedded Feishu QR code and signs in with the tmp_code it posts back', async () => {
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
    vi.mocked(authApi.providers).mockResolvedValueOnce({ wecom: false, feishu: true })
    const router = makeRouter()
    await router.push('/login?redirect=%2Fbots')
    const wrapper = mount(LoginView, { attachTo: document.body, global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    expect(authApi.feishuQr).toHaveBeenCalledWith('/bots')
    expect(wrapper.find('[data-test="username"]').exists()).toBe(false)
    const frame = wrapper.get('[data-test="feishu-qr"]').element as HTMLIFrameElement
    expect(frame.getAttribute('src')).toBe(
      'https://passport.feishu.cn/suite/passport/sso/qr?goto=' + encodeURIComponent(FEISHU_GOTO) + '&sdk_version=1.0.3')
    expect(wrapper.get('[data-test="scan-status"]').text()).toBe('等待扫码')
    // 不是二维码 iframe 发来的消息一律忽略
    window.dispatchEvent(new MessageEvent('message', { origin: 'https://passport.feishu.cn', data: { source: 'qrcode', tmp_code: 'x' } }))
    window.dispatchEvent(new MessageEvent('message', {
      origin: 'https://evil.example.com', source: frame.contentWindow, data: { source: 'qrcode', tmp_code: 'x' },
    }))
    expect(assign).not.toHaveBeenCalled()
    window.dispatchEvent(new MessageEvent('message', {
      origin: 'https://passport.feishu.cn', source: frame.contentWindow, data: { source: 'qrcode', tmp_code: 't/1' },
    }))
    await flushPromises()
    expect(assign).toHaveBeenCalledWith(FEISHU_GOTO + '&tmp_code=t%2F1')
    expect(wrapper.get('[data-test="scan-status"]').text()).toBe('已确认，正在登录…')
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it('switches between the QR code and password sign-in, and the account button opens the authorize page', async () => {
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
    vi.mocked(authApi.providers).mockResolvedValueOnce({ wecom: false, feishu: true })
    const router = makeRouter()
    await router.push('/login?redirect=%2Fusers')
    const wrapper = mount(LoginView, { global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    await wrapper.get('[data-test="account"]').trigger('click')
    expect(assign).toHaveBeenCalledWith('/api/auth/feishu/start?redirect=%2Fusers')
    expect(wrapper.get('[data-test="use-password"]').text()).toBe('没有飞书？用账号密码登录')
    await wrapper.get('[data-test="use-password"]').trigger('click')
    expect(wrapper.find('[data-test="username"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="feishu-qr"]').exists()).toBe(false)
    await wrapper.get('[data-test="back-to-scan"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="feishu-qr"]').exists()).toBe(true)
    vi.unstubAllGlobals()
  })

  it('embeds the WeCom login panel and finishes sign-in through the callback', async () => {
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
    vi.mocked(authApi.providers).mockResolvedValueOnce({ wecom: true, feishu: false })
    const router = makeRouter()
    await router.push('/login?redirect=%2Fusers')
    const wrapper = mount(LoginView, { global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    expect(authApi.wecomQr).toHaveBeenCalledWith('/users')
    expect(wecomPanel.options?.params).toMatchObject({
      appid: 'ww1', agentid: '1000002', state: 'w1', login_type: 'CorpApp', redirect_type: 'callback',
    })
    wecomPanel.options?.onLoginSuccess({ code: 'c1' })
    expect(assign).toHaveBeenCalledWith('/api/auth/wecom/callback?code=c1&state=w1')
    await wrapper.get('[data-test="account"]').trigger('click')
    expect(assign).toHaveBeenCalledWith('/api/auth/wecom/start?mode=qr&redirect=%2Fusers')
    vi.unstubAllGlobals()
  })

  it('offers a switch when both platforms are configured and remembers the choice', async () => {
    vi.mocked(authApi.providers).mockResolvedValue({ wecom: true, feishu: true })
    const wrapper = mount(LoginView, { global: { plugins: [ElementPlus, i18n, makeRouter()] } })
    await flushPromises()
    expect(wrapper.find('[data-test="platform-switch"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="feishu-qr"]').exists()).toBe(true)
    await wrapper.findAll('[data-test="platform-switch"] input')[1].setValue(true)
    await flushPromises()
    expect(wrapper.find('[data-test="wecom-qr"]').exists()).toBe(true)
    expect(localStorage.getItem('coreman.loginPlatform')).toBe('wecom')
    const again = mount(LoginView, { global: { plugins: [ElementPlus, i18n, makeRouter()] } })
    await flushPromises()
    expect(again.find('[data-test="wecom-qr"]').exists()).toBe(true)
    vi.mocked(authApi.providers).mockResolvedValue({ wecom: false, feishu: false })
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
    vi.mocked(authApi.providers).mockResolvedValue({ wecom: true, feishu: false })

    const errorRouter = makeRouter()
    await errorRouter.push('/login?error=user_not_found')
    const errorWrapper = mount(LoginView, { global: { plugins: [ElementPlus, i18n, errorRouter] } })
    await flushPromises()
    expect(assign).not.toHaveBeenCalled()
    expect(errorWrapper.text()).toContain(i18n.global.t('login.errors.user_not_found'))

    const cleanRouter = makeRouter()
    await cleanRouter.push('/login')
    vi.mocked(authApi.wecomQr).mockClear()
    const cleanWrapper = mount(LoginView, { global: { plugins: [ElementPlus, i18n, cleanRouter] } })
    await flushPromises()
    expect(assign).toHaveBeenCalledWith('/api/auth/wecom/start?mode=oauth&redirect=%2F')
    // 跳转期间不领内嵌二维码，免得和这次跳转抢同一个绑定 cookie
    expect(authApi.wecomQr).not.toHaveBeenCalled()
    expect(cleanWrapper.find('.login-card').exists()).toBe(false)

    vi.mocked(authApi.providers).mockResolvedValue({ wecom: false, feishu: false })
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
    await flushPromises()
    await wrapper.get('[data-test="username"]').setValue('admin')
    await wrapper.get('[data-test="password"]').setValue('pw')
    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()
    expect(assign).toHaveBeenCalledWith(link)
    expect(router.currentRoute.value.path).toBe('/login')
    vi.unstubAllGlobals()
  })
})
