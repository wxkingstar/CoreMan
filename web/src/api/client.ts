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

/** 422 字段级明细：后端只回 loc/msg/type 三个键（`coreman/api/errors.py::safe_validation_errors`）。 */
export interface FieldError {
  loc: (string | number)[]
  msg: string
  type: string
}

export class ApiError extends Error {
  constructor(public status: number, public code: number, message: string, public errors: FieldError[] = []) {
    super(message)
    this.name = 'ApiError'
  }
}

function fieldErrorsOf(raw: unknown): FieldError[] {
  if (!Array.isArray(raw)) return []
  return raw.filter((item): item is FieldError =>
    !!item && typeof item === 'object' && Array.isArray((item as FieldError).loc) && typeof (item as FieldError).msg === 'string')
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
    const err = e as AxiosError<{ code?: number; message?: string; errors?: unknown }>
    const status = err.response?.status ?? 0
    const data = err.response?.data
    throw new ApiError(status, data?.code ?? status, data?.message ?? err.message, fieldErrorsOf(data?.errors))
  }
}

export const api = {
  me: () => call<User>(http.get('/api/admin/auth/me')),
  bootstrapLogin: (username: string, password: string) =>
    call<{ user: User }>(http.post('/api/auth/bootstrap', { username, password })),
  logout: () => call<null>(http.post('/api/admin/auth/logout')),
}
