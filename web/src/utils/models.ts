import type { Backend } from '@/api/types'

/**
 * 按模型名前缀判断后端：`codex/` 开头走 codex，其余走 claude。
 * 与后端 `coreman/core/relay/models.py::backend_of` 同一条规则，改动要同步。
 */
export function backendOf(model: string): Backend {
  return model.startsWith('codex/') ? 'codex' : 'claude'
}
