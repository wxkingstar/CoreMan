import type { Backend } from '@/api/types'

/**
 * 按模型名前缀判断后端：`codex/`、`opencode/` 开头各走自己的后端，其余走 claude。
 * 与后端 `coreman/core/relay/models.py::backend_of` 同一条规则，改动要同步。
 */
export function backendOf(model: string): Backend {
  if (model.startsWith('codex/')) return 'codex'
  if (model.startsWith('opencode/')) return 'opencode'
  return 'claude'
}

/** 节点上的 AI 后端，与后端 `BACKENDS` 同序。 */
export const BACKENDS: readonly Backend[] = ['claude', 'codex', 'opencode']

const PROVIDER_LABELS: Record<string, string> = { claude: 'Claude Code', codex: 'Codex / GPT', opencode: 'OpenCode' }
const PROVIDER_ICONS: Record<string, string> = { claude: 'claude', codex: 'openai', opencode: 'opencode' }

/** 后端的展示名；未知的原样显示。 */
export function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider
}

/** 后端图标（public/providers/ 下）。 */
export function providerIcon(provider: string): string {
  return `/providers/${PROVIDER_ICONS[provider] ?? 'openai'}.svg`
}

/**
 * 节点明确报告没装或没登录这种 AI 的实例不出现在可选列表里；员工当前所在的那台照常列出。
 * 与后端 `coreman/core/relay/labels.py::signed_in` 同一条规则，改动要同步。
 */
export function selectableRelays<T extends { id: string; unavailable_reason?: string | null }>(rows: T[], currentId?: string | null): T[] {
  return rows.filter(r => r.id === currentId || !['not_installed', 'login_required'].includes(r.unavailable_reason ?? ''))
}
