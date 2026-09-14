export interface EnvEntry { key: string; value: string }
export class EnvTextError extends Error {
  constructor(public kind: 'syntax' | 'duplicate' | 'quote' | 'key', public line: number) {
    super(kind)
  }
}

export function validateEnvEntries(entries: EnvEntry[]): EnvEntry[] {
  const seen = new Set<string>()
  entries.forEach(({ key }, index) => {
    if (!/^[A-Z][A-Z0-9_]{0,63}$/.test(key)) throw new EnvTextError('key', index + 1)
    if (seen.has(key)) throw new EnvTextError('duplicate', index + 1)
    seen.add(key)
  })
  return entries
}

// Parse data only: never expand variables, shell substitutions or commands.
export function parseEnv(text: string): EnvEntry[] {
  const lines = text.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n').split('\n')
  const entries: EnvEntry[] = []
  const seen = new Set<string>()
  for (let i = 0; i < lines.length; i++) {
    const start = i + 1
    const line = lines[i]!.trim()
    if (!line || line.startsWith('#')) continue
    const match = /^(?:export\s+)?([A-Z][A-Z0-9_]{0,63})\s*=\s*(.*)$/.exec(line)
    if (!match) throw new EnvTextError('syntax', start)
    const key = match[1]!
    if (seen.has(key)) throw new EnvTextError('duplicate', start)
    seen.add(key)
    let raw = match[2]!, value = ''
    const quote = raw[0]
    if (quote === '"' || quote === "'") {
      let pos = 1, closed = false
      while (!closed) {
        while (pos < raw.length) {
          const char = raw[pos++]!
          if (char === quote) { closed = true; break }
          if (quote === '"' && char === '\\' && pos < raw.length) {
            const next = raw[pos++]!
            const escapes: Record<string, string> = { n: '\n', r: '\r', t: '\t', '"': '"', '\\': '\\' }
            value += escapes[next] ?? `\\${next}`
          } else value += char
        }
        if (!closed) {
          if (++i >= lines.length) throw new EnvTextError('quote', start)
          raw += `\n${lines[i]!}`
        }
      }
      const tail = raw.slice(pos).trim()
      if (tail && !tail.startsWith('#')) throw new EnvTextError('syntax', start)
    } else value = raw.split('#')[0]!.trim()
    entries.push({ key, value })
  }
  return entries
}

export function serializeEnv(entries: EnvEntry[]): string {
  validateEnvEntries(entries)
  return entries.map(({ key, value }) => `${key}=${JSON.stringify(value)}`).join('\n')
}
