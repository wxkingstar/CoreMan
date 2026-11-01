import { call, http } from './client'
import type { RelayOut } from './types'
/** 节点上同一种 AI 的一个登录账号；default 是 CLI 原来的位置。 */
export interface SeatState {
  id: string; label: string; account: string; login: 'ready' | 'required' | 'unknown'; active: boolean
}
export interface Capability {
  /** CLI 登录的账号邮箱；旧版 Daemon 未上报或读不到时缺省或为空。 */
  account?: string
  installed: boolean; version: string; login: 'ready' | 'required' | 'unknown'
  health: string; detail: string; models: string[]
  /** 节点支持多席位切换；旧版 Daemon 缺省。 */
  seat_switch_v1?: boolean; seats?: SeatState[]
}
/** 节点上的 CLI 发起的登录：Claude 要把浏览器页面上的认证码贴回来，Codex 在浏览器里输入 user_code。 */
export interface SeatLogin {
  session_id: string; authorization_url: string; user_code: string; needs_code: boolean; expires_in: number
}
export interface RuntimeNode {
  root_edit_supported?: boolean
  root_change?: { id: string; path: string; status: 'pending' | 'applied' | 'failed' } | null
  id: string; name: string; hostname: string; username: string
  /** 最近一次注册或心跳的来源 IP（经 NAT 时是出口 IP）；升级前未记录时为 null。 */
  ip: string | null
  platform: string; architecture: string; environment: string; workspace_root: string
  version: string; online: boolean; is_active: boolean; draining: boolean
  heartbeat_at: string | null; service_status: string; team_id: string | null; team_name: string | null
  /** 节点并发上限与当前进行中的调用数；Daemon 未上报时为 null。 */
  max_concurrent: number | null; active_calls: number | null
  /** 节点 config.json 的 Git 主机白名单（只读）；旧版 Daemon 未上报时为 null。 */
  git_hosts: string[] | null
  /** CLI 继承到的出站代理（只读）；旧版 Daemon 未上报时为 null。pending 表示配置改过、重启后才生效。 */
  proxy: { source: 'coreman' | 'environment' | 'none'; url: string; pending: boolean } | null
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
  options: { control_proxy?: string; environment?: string; proxy: string; ca_pem?: string; claude_path: string; codex_path: string; git_hosts: string[]; max_concurrent: number }
}
const base = '/api/admin/runtime-nodes'
export const runtimeNodes = {
  list: () => call<RuntimeNode[]>(http.get(base)),
  /** team_id 传 null 表示改为公共池；不传表示不改团队。 */
  patch: (id: string, body: { name?: string; workspace_root?: string; team_id?: string | null; is_active?: boolean; draining?: boolean }) => call<null>(http.patch(`${base}/${id}`, body)),
  remove: (id: string) => call<null>(http.delete(`${base}/${id}`)),
  createLink: (body: InstallInput) => call<InstallLink>(http.post(`${base}/install-links`, body)),
}
const seatBase = (node: string, provider: string) => `${base}/${node}/${provider}/seats`
export const runtimeSeats = {
  create: (node: string, provider: string, label: string) =>
    call<{ seat_id: string; seats: SeatState[] }>(http.post(seatBase(node, provider), { label })),
  startLogin: (node: string, provider: string, seat: string) =>
    call<SeatLogin>(http.post(`${seatBase(node, provider)}/${seat}/login`)),
  completeLogin: (node: string, provider: string, seat: string, body: { session_id: string; code?: string }) =>
    call<{ seats: SeatState[] }>(http.post(`${seatBase(node, provider)}/${seat}/login/complete`, body)),
  activate: (node: string, provider: string, seat: string) =>
    call<{ seats: SeatState[] }>(http.post(`${seatBase(node, provider)}/${seat}/activate`)),
  remove: (node: string, provider: string, seat: string) =>
    call<{ seats: SeatState[] }>(http.delete(`${seatBase(node, provider)}/${seat}`)),
}
