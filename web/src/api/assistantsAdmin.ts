import { call, http } from '@/api/client'

/** 平台管理员看到的一位助理：只有元数据，没有任何内容。 */
export interface AssistantRow {
  bot_id: string
  state: 'provisioning' | 'active' | 'deprovisioning' | 'deprovisioned' | null
  enabled: boolean
  created_at: string | null
  owner: { id: string; login_name: string | null; display_name: string }
  relay: { id: string; name: string } | null
  node: { id: string; name: string; online: boolean } | null
  lifecycle: { action: string; step: string; trigger: string; error_code: string | null } | null
  /** 此刻是否在班：记录启用、开通好且在工作时段内（含「上班」「下班」的临时切换）。 */
  on_shift: boolean
  /** 还没有设置行的助理是 null。 */
  cold_start: {
    state: 'pending' | 'in_progress' | 'done'
    step: string | null
    outcome: string | null
    started_at: string | null
    done_at: string | null
  } | null
  directories_pending_removal: number
  watch: { state: string; last_ok_at: string | null; last_error: string | null } | null
  turns: { today: number; budget: number } | null
  inbox_pending: number
  jobs_active: number
}

const root = '/api/admin/assistants'
export const assistantsAdmin = {
  list: () => call<{ items: AssistantRow[] }>(http.get(root)),
  /** 停用不可撤销：调用前必须已经二次确认。 */
  deprovision: (botId: string) => call<{ bot_id: string; state: string }>(http.post(`${root}/${botId}/deprovision`, { confirm: true })),
}
