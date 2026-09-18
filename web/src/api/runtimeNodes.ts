import { call, http } from './client'
import type { RelayOut } from './types'
export interface Capability {
  installed: boolean; version: string; login: 'ready' | 'required' | 'unknown'
  health: string; detail: string; models: string[]
}
export interface RuntimeNode {
  id: string; name: string; hostname: string; username: string
  platform: string; architecture: string; environment: string; workspace_root: string
  version: string; online: boolean; is_active: boolean; draining: boolean
  heartbeat_at: string | null; service_status: string; team_id: string | null; team_name: string | null
  /** 节点并发上限与当前进行中的调用数；Daemon 未上报时为 null。 */
  max_concurrent: number | null; active_calls: number | null
  /** 节点 config.json 的 Git 主机白名单（只读）；旧版 Daemon 未上报时为 null。 */
  git_hosts: string[] | null
  capabilities: Record<string, Capability>; backends: RelayOut[]
}
export interface InstallLink {
  id: string; name: string; workspace_root: string; team_id: string | null
  state: 'ready' | 'used' | 'expired' | 'revoked'; expires_at: string; created_at: string
  node_id: string | null; command?: string; url?: string
}
export interface InstallInput {
  name: string; workspace_root: string; team_id: string | null
  /** ca_pem：私有 CA 证书（PEM，≤64 KB）；留空时不传该键。 */
  options: { control_proxy?: string; environment?: string; proxy: string; ca_pem?: string; claude_path: string; codex_path: string; git_hosts: string[]; max_concurrent: number; install_claude_probe: boolean }
}
const base = '/api/admin/runtime-nodes'
export const runtimeNodes = {
  list: () => call<RuntimeNode[]>(http.get(base)),
  /** team_id 传 null 表示改为公共池；不传表示不改团队。 */
  patch: (id: string, body: { name?: string; team_id?: string | null; is_active?: boolean; draining?: boolean }) => call<null>(http.patch(`${base}/${id}`, body)),
  remove: (id: string) => call<null>(http.delete(`${base}/${id}`)),
  createLink: (body: InstallInput) => call<InstallLink>(http.post(`${base}/install-links`, body)),
}
