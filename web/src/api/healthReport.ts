import { readCookie } from './client'
export interface ReportEvent { kind: string; text?: string; name?: string; message?: string }
export async function healthReport(bot: string, signal: AbortSignal, onEvent: (event: ReportEvent) => void) {
  const response = await fetch(`/api/admin/bots/${bot}/health-report`, { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': readCookie('coreman_csrf') ?? '' }, signal })
  if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(error.message ?? `HTTP ${response.status}`) }
  if (!response.body) throw new Error('Empty response')
  const reader = response.body.getReader(), decoder = new TextDecoder()
  let buffer = '', completed = false
  try {
    while (true) {
      const part = await reader.read()
      buffer = (buffer + decoder.decode(part.value, { stream: !part.done })).replace(/\r\n/g, '\n')
      if (buffer.length > 256 * 1024) throw new Error('Report exceeds limit')
      let boundary: number
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2)
        const lines = frame.split('\n')
        const kind = lines.find(line => line.startsWith('event:'))?.slice(6).trim() ?? 'message'
        const payload = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
        if (!payload) continue
        const data = JSON.parse(payload)
        if (kind === 'error') throw new Error(data.message ?? 'Incomplete report')
        if (kind === 'done') completed = true
        onEvent({ kind, text: typeof data.text === 'string' ? data.text : undefined, name: typeof data.name === 'string' ? data.name : undefined })
      }
      if (part.done) break
    }
    if (!completed) throw new Error('Incomplete report')
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock() }
}
