import { beforeEach, describe, expect, it, vi } from 'vitest'

import { http } from '@/api/client'
import { myAssistant } from '@/api/myAssistant'

const ok = (data: unknown) => ({ data: { code: 0, data } })

describe('my assistant API', () => {
  beforeEach(() => vi.restoreAllMocks())

  // 知识页的路径本身是个人内容：读与删都放请求体，不进网址（网址会进访问日志）。
  it('never puts a page path into the URL', async () => {
    const post = vi.spyOn(http, 'post').mockResolvedValue(ok({ deleted: true }))
    const get = vi.spyOn(http, 'get')
    const del = vi.spyOn(http, 'delete')
    await myAssistant.page('人/李四')
    await myAssistant.deletePage('人/李四')
    expect(post.mock.calls).toEqual([
      ['/api/me/assistant/notes/read', { path: '人/李四' }],
      ['/api/me/assistant/notes/delete', { path: '人/李四' }],
    ])
    for (const [url] of post.mock.calls) expect(url).not.toContain('李四')
    expect(get).not.toHaveBeenCalled()
    expect(del).not.toHaveBeenCalled()
  })

  // 把会话移出忽略名单要等正在进行的一轮轮询提交（上限 30 秒）；重新生成口吻页要读飞书。
  it('gives the slow requests a 60 second timeout', async () => {
    const patch = vi.spyOn(http, 'patch').mockResolvedValue(ok({}))
    const post = vi.spyOn(http, 'post').mockResolvedValue(ok({ queued: true, left_today: 2 }))
    await myAssistant.saveSettings({ ignored_chats: [] })
    await myAssistant.regenerateVoice()
    expect(patch).toHaveBeenCalledWith('/api/me/assistant/settings', { ignored_chats: [] }, { timeout: 60_000 })
    expect(post).toHaveBeenCalledWith('/api/me/assistant/voice/regenerate', undefined, { timeout: 60_000 })
  })

  it('switches the shift with a state and stops only with an explicit confirmation', async () => {
    const post = vi.spyOn(http, 'post').mockResolvedValue(ok({}))
    await myAssistant.shift('off')
    await myAssistant.deprovision()
    expect(post.mock.calls).toEqual([
      ['/api/me/assistant/shift', { state: 'off' }],
      ['/api/me/assistant/deprovision', { confirm: true }],
    ])
  })
})
