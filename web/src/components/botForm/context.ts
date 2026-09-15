import { inject, type ComputedRef, type InjectionKey, type Ref } from 'vue'
import type { BotIn, RelayOut, TeamOut } from '@/api/types'

/** BotForm 与其分区子组件共享的表单状态和联动逻辑；状态只在 BotForm 里维护一份。 */
export interface BotFormContext {
  mode: 'create' | 'edit'
  form: BotIn
  fieldErrors: Record<string, string>
  isManager: ComputedRef<boolean>
  teamList: Ref<TeamOut[]>
  relayList: Ref<RelayOut[]>
  runtimeGroups: ComputedRef<{ id: string; name: string }[]>
  selectedRuntime: ComputedRef<string | null>
  runtimeBackends: ComputedRef<RelayOut[]>
  modelOptions: ComputedRef<string[]>
  xhighAllowed: ComputedRef<boolean>
  sensitiveVisible: ComputedRef<boolean>
  credKeys: ComputedRef<readonly string[]>
  onBotKeyInput: () => void
  onPlatformChange: () => void
  onEnvInvalid: (message: string) => void
  selectRuntime: (id: string | null) => Promise<void>
  selectRelay: (id: string | null, keepModel?: boolean) => Promise<void>
}

export const botFormKey: InjectionKey<BotFormContext> = Symbol('botForm')

export function useBotFormContext(): BotFormContext {
  const context = inject(botFormKey)
  if (!context) throw new Error('BotForm sections must be rendered inside BotForm')
  return context
}
