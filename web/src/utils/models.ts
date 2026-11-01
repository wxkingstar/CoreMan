import type { Backend } from '@/api/types'

/**
 * 按模型名前缀判断后端：`codex/` 开头走 codex，其余走 claude。
 * 与后端 `coreman/core/relay/models.py::backend_of` 同一条规则，改动要同步。
 */
export function backendOf(model: string): Backend {
  return model.startsWith('codex/') ? 'codex' : 'claude'
}

/**
 * 节点明确报告没装或没登录这种 AI 的实例不出现在可选列表里；员工当前所在的那台照常列出。
 * 与后端 `coreman/core/relay/labels.py::signed_in` 同一条规则，改动要同步。
 */
export function selectableRelays<T extends { id: string; unavailable_reason?: string | null }>(rows: T[], currentId?: string | null): T[] {
  return rows.filter(r => r.id === currentId || !['not_installed', 'login_required'].includes(r.unavailable_reason ?? ''))
}
