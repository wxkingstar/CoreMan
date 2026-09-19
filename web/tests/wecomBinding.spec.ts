import { describe, expect, it, vi } from 'vitest'
vi.mock('@/api/client', () => {
  const ok = () => vi.fn().mockResolvedValue({ data: { status: 'unbound' } })
  return { http: { get: ok(), post: ok(), patch: ok(), delete: ok() }, call: (response: Promise<{ data: unknown }>) => response.then(r => r.data) }
})
import { http } from '@/api/client'
import { wecomBinding } from '@/api/wecomBinding'
describe('own WeCom binding API', () => {
  it('reads the binding and drives the scan session on session-owned endpoints', async () => {
    expect(await wecomBinding.get()).toEqual({ status: 'unbound' })
    expect(http.get).toHaveBeenLastCalledWith('/api/me/wecom-binding')
    await wecomBinding.startScan()
    expect(http.post).toHaveBeenLastCalledWith('/api/me/wecom-binding/scan')
    await wecomBinding.pollScan()
    expect(http.get).toHaveBeenLastCalledWith('/api/me/wecom-binding/scan')
    await wecomBinding.cancelScan()
    expect(http.delete).toHaveBeenLastCalledWith('/api/me/wecom-binding/scan')
  })
  it('updates, probes and unbinds the binding', async () => {
    await wecomBinding.update({ authorization_level: 'all' })
    expect(http.patch).toHaveBeenLastCalledWith('/api/me/wecom-binding', { authorization_level: 'all' })
    await wecomBinding.update({ enabled: false })
    expect(http.patch).toHaveBeenLastCalledWith('/api/me/wecom-binding', { enabled: false })
    await wecomBinding.check()
    expect(http.post).toHaveBeenLastCalledWith('/api/me/wecom-binding/check')
    await wecomBinding.renewed()
    expect(http.post).toHaveBeenLastCalledWith('/api/me/wecom-binding/renewed')
    expect(await wecomBinding.unbind()).toEqual({ status: 'unbound' })
    expect(http.delete).toHaveBeenLastCalledWith('/api/me/wecom-binding')
  })
})
