import { call, http } from './client'
export interface WorkspaceInfo { directory: string; state: string; error?: string | null; phase?: string | null; git_url?: string | null; branch: string; has_token?: boolean; last_backup_at?: string | null; memory_snapshot_at?: string | null }
export interface WorkspaceEntry { name: string; path: string; type: string; size: number }
export interface WorkspaceFile { content: string | null; data: string; size: number; hash: string; editable: boolean; eof: boolean }
export interface GitStatus { initialized: boolean; branch: string; head?: string; files: { path: string; status: string }[]; excluded?: string[] }
export interface SwitchPreview { directory: string; exists: boolean; empty: boolean; owned: boolean; marked: boolean; source_online: boolean; git_configured: boolean; memory_snapshot_at: string | null }
const base = (id: string) => `/api/admin/bots/${id}/workspace`
export const workspace = {
  get: (id: string) => call<WorkspaceInfo>(http.get(base(id))),
  list: (id: string, path = '') => call<{ entries: WorkspaceEntry[]; truncated?: boolean }>(http.get(`${base(id)}/files`, { params: { path } })),
  read: (id: string, path: string, offset = 0) => call<WorkspaceFile>(http.get(`${base(id)}/file`, { params: { path, offset, length: 262144 } })),
  write: (id: string, body: { path: string; content: string; expected_hash: string }) => call<{ hash: string }>(http.put(`${base(id)}/file`, body)),
  initialize: (id: string) => call<WorkspaceInfo>(http.post(`${base(id)}/initialize`, {}, { timeout: 300000 })),
  gitConfig: (id: string, body: { git_url: string; branch: string; access_token?: string }) => call<WorkspaceInfo>(http.put(`${base(id)}/git`, body)),
  gitStatus: (id: string) => call<GitStatus>(http.get(`${base(id)}/git/status`)),
  gitTest: (id: string) => call<{ reachable: boolean }>(http.post(`${base(id)}/git/test`, {}, { timeout: 300000 })),
  backup: (id: string, files: string[], message: string) => call<{ pushed: boolean }>(http.post(`${base(id)}/git/backup`, { files, message }, { timeout: 1000000 })),
  preview: (id: string, relay_server_id: string, target_directory: string) => call<SwitchPreview>(http.post(`${base(id)}/switch-preview`, { relay_server_id, target_directory })),
}
