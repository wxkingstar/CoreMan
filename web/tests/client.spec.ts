import { AxiosHeaders, type AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, attachCsrf, call, http, onResponseError, readCookie, setUnauthorizedHandler, type Envelope } from '@/api/client'

describe('api client', () => {
  afterEach(() => vi.restoreAllMocks())

  it('reads cookie by name', () => {
    document.cookie = 'coreman_csrf=tok123'
    expect(readCookie('coreman_csrf')).toBe('tok123')
    expect(readCookie('missing')).toBeNull()
  })

  it('unwraps envelope and maps errors', async () => {
    const resolved = { data: { code: 0, data: { ok: 1 } } } as AxiosResponse<Envelope<{ ok: number }>>
    await expect(call(Promise.resolve(resolved))).resolves.toEqual({ ok: 1 })
    const failure = Object.assign(new Error('boom'), {
      isAxiosError: true,
      response: { status: 401, data: { code: 401, message: '未登录或会话已过期' } },
    })
    await expect(call(Promise.reject(failure))).rejects.toMatchObject({ status: 401, code: 401, message: '未登录或会话已过期' })
  })

  it('adds X-CSRF-Token on unsafe methods', async () => {
    document.cookie = 'coreman_csrf=tok456'
    const spy = vi.spyOn(http, 'post').mockResolvedValue({ data: { code: 0, data: null } })
    await api.logout()
    expect(spy).toHaveBeenCalledWith('/api/admin/auth/logout')
    const post = attachCsrf({ method: 'post', headers: new AxiosHeaders() } as InternalAxiosRequestConfig)
    expect(post.headers.get('X-CSRF-Token')).toBe('tok456')
    const get = attachCsrf({ method: 'get', headers: new AxiosHeaders() } as InternalAxiosRequestConfig)
    expect(get.headers.get('X-CSRF-Token')).toBeUndefined()
  })

  it('ApiError carries status and code', () => {
    const e = new ApiError(403, 403, 'CSRF 校验失败')
    expect(e).toBeInstanceOf(Error)
    expect(e.status).toBe(403)
  })

  it('calls unauthorized handler on 401 responses', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    const rejected = { response: { status: 401, data: { code: 401, message: 'x' } }, config: { url: '/api/admin/users' } }
    await expect(onResponseError(rejected as unknown as AxiosError)).rejects.toBeTruthy()
    expect(handler).toHaveBeenCalledOnce()
    setUnauthorizedHandler(null)
  })
})
