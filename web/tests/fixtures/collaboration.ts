// Local visual fixture: every HTTP request is intercepted; no real API or IM traffic.
import { createApp, h } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import '../../src/styles/theme.css'
import '../../src/styles/controls.css'
import '../../src/styles/overlays.css'
import { i18n } from '../../src/i18n'
import { http } from '../../src/api/client'
import BotCollaborators from '../../src/components/BotCollaborators.vue'
import type { CollaborationRoute } from '../../src/api/collaboration'
const rows: CollaborationRoute[] = []
const peers = [
 { id: 'peer', name: '财务助手', description: '负责核对费用和结算', enabled: true, available: true },
 { id: 'unavailable', name: '采购助手', description: '运行节点暂不可用，也可以先保存', enabled: false, available: false },
]
http.defaults.adapter = async config => {
 const url = config.url ?? ''
 let data: unknown
 if (url.endsWith('/collaborator-options')) data = peers.filter(peer => `${peer.name} ${peer.description}`.includes(config.params?.q ?? ''))
 else if (config.method === 'post') {
  const peer = peers.find(peer => peer.id === JSON.parse(config.data).target_bot_id)!
  const row: CollaborationRoute = { id: peer.id, target_bot_id: peer.id, target_name: peer.name, target_description: peer.description, enabled: true, version: 1, status: peer.available ? 'ready' : 'unavailable', reason: peer.available ? null : 'runtime_unavailable', can_enable: true, can_remove: true, active_count: 0 }
  rows.push(row); data = { ...row }
 } else if (config.method === 'patch') {
  const row = rows.find(row => url.endsWith(`/${row.id}`))!
  const enabled = JSON.parse(config.data).enabled; Object.assign(row, { enabled, version: row.version + 1 }); data = { ...row }
 } else if (config.method === 'delete') { rows.splice(rows.findIndex(row => url.endsWith(`/${row.id}`)), 1); data = null }
 else {
  data = rows.map(row => ({ ...row }))
 }
 return { data: { code: 0, data }, status: 200, statusText: 'OK', headers: {}, config }
}
createApp({ render: () => h('main', { style: 'max-width:1080px;margin:32px auto;padding:16px;' }, [h('h2', '运营助手 · 协作伙伴'), h(BotCollaborators, { botId: 'fixture', sourceName: '运营助手', canEdit: true, active: true })]) }).use(ElementPlus).use(i18n).mount('#app')
