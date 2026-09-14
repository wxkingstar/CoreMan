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
  heartbeat_at: string | null; service_status: string; team_name: string | null
  capabilities: Record<string, Capability>; backends: RelayOut[]
}
export interface InstallLink {
  id: string; name: string; workspace_root: string; team_id: string | null
  state: 'ready' | 'used' | 'expired' | 'revoked'; expires_at: string; created_at: string
  node_id: string | null; command?: string; url?: string
}
export interface InstallInput {
  name: string; workspace_root: string; team_id: string | null
  options: { control_proxy?: string; environment?: string; proxy: string; claude_path: string; codex_path: string; max_concurrent: number; install_claude_probe: boolean }
}
const base = '/api/admin/runtime-nodes'
export const runtimeNodes = {
  list: () => call<RuntimeNode[]>(http.get(base)),
  patch: (id: string, body: { name?: string; is_active?: boolean; draining?: boolean }) => call<null>(http.patch(`${base}/${id}`, body)),
  createLink: (body: InstallInput) => call<InstallLink>(http.post(`${base}/install-links`, body)),
}
