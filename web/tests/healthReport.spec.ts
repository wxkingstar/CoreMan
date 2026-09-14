import { afterEach, expect, it, vi } from 'vitest'
import { healthReport } from '@/api/healthReport'
afterEach(() => vi.unstubAllGlobals())
function reply(text: string) {
  const bytes = new TextEncoder().encode(text)
  return new Response(new ReadableStream({ start(controller) { for (const byte of bytes) controller.enqueue(new Uint8Array([byte])); controller.close() } }), { status: 200 })
}
it('decodes fragmented UTF-8 SSE and requires the completion event', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply('event: text\ndata: {"text":"检查通过"}\n\nevent: done\ndata: {}\n\n')))
  const events: string[] = []
  await healthReport('bot', new AbortController().signal, e => { if (e.text) events.push(e.text) })
  expect(events).toEqual(['检查通过'])
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply('event: text\ndata: {"text":"partial"}\n\n')))
  await expect(healthReport('bot', new AbortController().signal, () => {})).rejects.toThrow('Incomplete report')
})
it('propagates cancellation and includes CSRF when starting a report', async () => {
  document.cookie = 'coreman_csrf=test-csrf'
  const controller = new AbortController()
  const fetcher = vi.fn().mockRejectedValue(new DOMException('Aborted', 'AbortError'))
  vi.stubGlobal('fetch', fetcher)
  controller.abort()
  await expect(healthReport('bot', controller.signal, () => {})).rejects.toThrow('Aborted')
  expect(fetcher).toHaveBeenCalledWith('/api/admin/bots/bot/health-report', expect.objectContaining({ signal: controller.signal, headers: { 'X-CSRF-Token': 'test-csrf' } }))
})
