import axios, { type AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'

export interface User {
  id: string
  login_name: string | null
  display_name: string
  role: string
  locale: string
  email: string | null
  avatar_url: string | null
  source: 'sync' | 'bootstrap' | 'manual'
  team_id: string | null
}

export interface Envelope<T> {
  code: number
  data: T
  message?: string
}

export class ApiError extends Error {
  constructor(public status: number, public code: number, message: string) {
    super(message)
    this.name = 'ApiError'
  }
}

export function readCookie(name: string): string | null {
  const m = document.cookie.match(new RegExp('(?:^|; )' + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '=([^;]*)'))
  return m ? decodeURIComponent(m[1]) : null
}

export const http = axios.create({ baseURL: '/', withCredentials: true, timeout: 15000 })

export function attachCsrf(cfg: InternalAxiosRequestConfig): InternalAxiosRequestConfig {
  const method = (cfg.method ?? 'get').toUpperCase()
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const token = readCookie('coreman_csrf')
    if (token) cfg.headers.set('X-CSRF-Token', token)
  }
  return cfg
}

http.interceptors.request.use(attachCsrf)

let onUnauthorized: (() => void) | null = null

export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn
}

export function onResponseError(error: AxiosError): Promise<never> {
  if (error.response?.status === 401 && onUnauthorized) onUnauthorized()
  return Promise.reject(error)
}

http.interceptors.response.use((res) => res, onResponseError)

export async function call<T>(promise: Promise<AxiosResponse<Envelope<T>>>): Promise<T> {
  try {
    const res = await promise
    return res.data.data
  } catch (e) {
    const err = e as AxiosError<{ code?: number; message?: string }>
    const status = err.response?.status ?? 0
    throw new ApiError(status, err.response?.data?.code ?? status, err.response?.data?.message ?? err.message)
  }
}

export const api = {
  me: () => call<User>(http.get('/api/admin/auth/me')),
  bootstrapLogin: (username: string, password: string) =>
    call<{ user: User }>(http.post('/api/auth/bootstrap', { username, password })),
  logout: () => call<null>(http.post('/api/admin/auth/logout')),
}
