import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/client', () => ({
  api: { me: vi.fn(), bootstrapLogin: vi.fn(), logout: vi.fn() },
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: number, message: string) { super(message) }
  },
  setUnauthorizedHandler: vi.fn(),
}))

import { api, ApiError, setUnauthorizedHandler } from '@/api/client'
import { router } from '@/router'
import { useAuthStore } from '@/stores/auth'

// router/index.ts 在模块加载时把回调注册进 setUnauthorizedHandler 这个 mock；
// 由于上面对 @/router 的静态 import 已经触发过一次模块求值，这里可以直接取出该回调来单独测试。
const handler = vi.mocked(setUnauthorizedHandler).mock.calls[0][0]!

describe('router guard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  // 必须放在本文件最先执行的两个用例：断言依赖 router 仍处于模块加载后的初始 START_LOCATION（fullPath '/'），
  // 尚未被其它用例的 router.push 改动过。
  it('unauthorized handler is a no-op before the initial fetchMe completes', async () => {
    expect(() => handler()).not.toThrow()
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/')
  })

  it('initial navigation to /login?error=… keeps the error query even when the 401 fires mid-guard', async () => {
    // 模拟拦截器在守卫等待 fetchMe 期间同步触发 401 回调：此时 auth.loaded 仍是 false，
    // 且原始导航（/login?error=user_not_found）尚未提交。
    vi.mocked(api.me).mockImplementationOnce(() => {
      handler()
      return Promise.reject(new ApiError(401, 401, 'unauthorized'))
    })
    await router.push('/login?error=user_not_found')
    await router.isReady()
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.error).toBe('user_not_found')
  })

  it('redirects to login with redirect query when /me fails with a non-401 error', async () => {
    vi.mocked(api.me).mockRejectedValueOnce(new Error('network down'))
    await router.push('/')
    await router.isReady()
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/')
    expect(useAuthStore().loaded).toBe(true)
  })

  it('unauthorized handler is a no-op when already on the login route', async () => {
    vi.mocked(api.me).mockRejectedValueOnce(new Error('not logged in'))
    await router.push('/login')
    await router.isReady()
    expect(router.currentRoute.value.name).toBe('login')
    expect(() => handler()).not.toThrow()
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('login')
  })

  it('unauthorized handler logs out and redirects to login with the previous path as redirect query', async () => {
    vi.mocked(api.me).mockResolvedValueOnce({
      id: 'u1', login_name: 'admin', display_name: 'Admin', role: 'platform_admin', locale: 'zh',
      email: null, avatar_url: null, source: 'bootstrap', team_id: null,
    })
    await router.push('/')
    await router.isReady()
    const auth = useAuthStore()
    expect(auth.user).not.toBeNull()
    const previousFullPath = router.currentRoute.value.fullPath

    handler()
    await flushPromises()

    expect(auth.user).toBeNull()
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe(previousFullPath)
  })

  // 旧的 relay 管理页已删除；/relays 书签继续落到运行时管理页。
  it('redirects the retired /relays page to the runtime page', async () => {
    vi.mocked(api.me).mockResolvedValueOnce({
      id: 'u1', login_name: 'admin', display_name: 'Admin', role: 'platform_admin', locale: 'zh',
      email: null, avatar_url: null, source: 'bootstrap', team_id: null,
    })
    await router.push('/relays')
    await router.isReady()
    expect(router.currentRoute.value.fullPath).toBe('/runtimes')
    expect(router.currentRoute.value.name).toBe('relays')
  })
})
