import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/client', () => ({
  api: {
    me: vi.fn(),
    bootstrapLogin: vi.fn(),
    logout: vi.fn(),
  },
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: number, message: string) { super(message) }
  },
}))

import { ApiError, api } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const user = {
  id: 'u1', login_name: 'admin', display_name: 'admin', role: 'platform_admin', locale: 'zh',
  email: null, avatar_url: null, source: 'bootstrap' as const, team_id: null,
}

describe('auth store', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('fetchMe sets user or null on 401', async () => {
    const store = useAuthStore()
    vi.mocked(api.me).mockResolvedValueOnce(user)
    await store.fetchMe()
    expect(store.user).toEqual(user)
    expect(store.loaded).toBe(true)
    vi.mocked(api.me).mockRejectedValueOnce(new ApiError(401, 401, 'x'))
    await store.fetchMe()
    expect(store.user).toBeNull()
  })

  it('fetchMe rethrows non-401 errors but marks loaded', async () => {
    const store = useAuthStore()
    vi.mocked(api.me).mockRejectedValueOnce(new ApiError(500, 500, 'boom'))
    await expect(store.fetchMe()).rejects.toMatchObject({ status: 500 })
    expect(store.loaded).toBe(true)
    expect(store.user).toBeNull()
  })

  it('loginBootstrap stores user and logout clears it', async () => {
    const store = useAuthStore()
    vi.mocked(api.bootstrapLogin).mockResolvedValueOnce({ user })
    await store.loginBootstrap('admin', 'pw')
    expect(store.user?.login_name).toBe('admin')
    vi.mocked(api.logout).mockResolvedValueOnce(null)
    await store.logout()
    expect(store.user).toBeNull()
  })
})
