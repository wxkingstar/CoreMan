<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { runtimeNodes, type RuntimeNode, type InstallInput, type InstallLink } from '@/api/runtimeNodes'
import { relays, teams } from '@/api/admin'
import type { TeamOut, RelayOut } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import ProviderBadge from '@/components/ProviderBadge.vue'
import RelayLiveStatus from '@/components/RelayLiveStatus.vue'
import HealthTag from '@/components/HealthTag.vue'
import CatalogPanel from '@/views/CatalogPanel.vue'
import { formatDateTime } from '@/utils/format'
const { t } = useI18n()
const auth = useAuthStore()
const canManage = computed(() => ['ai_committee', 'platform_admin'].includes(auth.user?.role ?? ''))
const nodes = ref<RuntimeNode[]>([])
const teamList = ref<TeamOut[]>([])
const query = ref('')
const tab = ref('nodes')
const loading = ref(false)
const creating = ref(false)
const dialog = ref(false)
const editDialog = ref(false)
const editId = ref('')
const editForm = reactive({ name: '', team_id: null as string | null })
const saving = ref(false)
function openEdit(node: RuntimeNode) {
  editId.value = node.id
  editForm.name = node.name
  editForm.team_id = node.team_id
  editDialog.value = true
}
/** 只提交有变化的字段；团队清空即改回公共池（team_id: null）。 */
async function saveEdit() {
  const name = editForm.name.trim()
  if (!name || name.length > 100 || saving.value) return
  const node = nodes.value.find(n => n.id === editId.value)
  const teamId = editForm.team_id || null
  const body: { name?: string; team_id?: string | null } = {}
  if (name !== node?.name) body.name = name
  if (teamId !== (node?.team_id ?? null)) body.team_id = teamId
  if (!Object.keys(body).length) { editDialog.value = false; return }
  saving.value = true
  try {
    await runtimeNodes.patch(editId.value, body)
    editDialog.value = false
    ElMessage.success(t('common.saved'))
    await refresh()
  } catch (error) { ElMessage.error(errorMessage(error)) }
  finally { saving.value = false }
}
const generated = ref<InstallLink | null>(null)
const form = reactive({ name: '', workspace_root: '/home/ai', team_id: null as string | null,
  options: { control_proxy: '', environment: 'auto', proxy: '', ca_pem: '', claude_path: '', codex_path: '', git_hosts: ['github.com'], max_concurrent: 10, install_claude_probe: false } })
const CA_PEM_MAX_BYTES = 64 * 1024
const CA_PEM_RE = /-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----/
/** 私有 CA 证书即时校验：留空合法（不传该键）；填了就必须是 PEM 证书且不超过 64 KB。 */
const caPemError = computed(() => {
  const pem = form.options.ca_pem.trim()
  if (!pem) return ''
  if (new TextEncoder().encode(pem).length > CA_PEM_MAX_BYTES) return t('runtimeNodes.caPemTooLarge')
  return CA_PEM_RE.test(pem) ? '' : t('runtimeNodes.caPemInvalid')
})
function installPayload(): InstallInput {
  const { ca_pem, ...options } = form.options
  const pem = ca_pem.trim()
  return { ...form, options: pem ? { ...options, ca_pem: pem } : options }
}
const filtered = computed(() => nodes.value.filter(n => `${n.name} ${n.hostname} ${n.username} ${n.workspace_root}`.toLowerCase().includes(query.value.toLowerCase())))
let timer: ReturnType<typeof setInterval> | undefined
async function refresh(silent = false) {
  if (loading.value) return
  loading.value = true
  try {
    nodes.value = await runtimeNodes.list()
  } catch (error) {
    if (!silent) ElMessage.error(errorMessage(error))
  } finally { loading.value = false }
}
/** 正在提交启停/排空的节点：提交期间禁用该行开关，避免连点。 */
const pendingId = ref('')
async function update(node: RuntimeNode, body: { is_active?: boolean; draining?: boolean }) {
  if (pendingId.value) return
  pendingId.value = node.id
  try { await runtimeNodes.patch(node.id, body); await refresh() }
  catch (error) { ElMessage.error(errorMessage(error)) }
  finally { pendingId.value = '' }
}
async function confirmed(message: string, title: string, danger = false): Promise<boolean> {
  try {
    await ElMessageBox.confirm(message, title, { type: 'warning', confirmButtonText: danger ? t('common.delete') : t('common.confirm'), cancelButtonText: t('common.cancel'), confirmButtonClass: danger ? 'el-button--danger' : undefined })
    return true
  } catch {
    return false
  }
}
/** 停用会把该节点下全部运行时置为停用并取消所有未完成的调用，必须先确认；重新启用不需要。 */
async function toggleActive(node: RuntimeNode) {
  if (node.is_active && !(await confirmed(t('runtimeNodes.disableConfirm', { name: node.name }), t('runtimeNodes.disableTitle')))) return
  await update(node, { is_active: !node.is_active })
}
/** 排空只停止接收新调用，进行中的会跑完；恢复接单不需要确认。 */
async function toggleDrain(node: RuntimeNode) {
  if (!node.draining && !(await confirmed(t('runtimeNodes.drainConfirm', { name: node.name }), t('runtimeNodes.drain')))) return
  await update(node, { draining: !node.draining })
}
/** 仍有 AI 员工绑定时直接提示先切换运行时（服务端还会核对迁移中的员工）；删除不可恢复，必须确认。 */
async function remove(node: RuntimeNode) {
  if (pendingId.value) return
  const inUse = node.backends.reduce((sum, backend) => sum + backend.bot_count, 0)
  if (inUse) { ElMessage.warning(t('runtimeNodes.deleteInUse', { n: inUse, name: node.name })); return }
  if (!(await confirmed(t('runtimeNodes.deleteConfirm', { name: node.name }), t('runtimeNodes.deleteTitle'), true))) return
  pendingId.value = node.id
  try {
    await runtimeNodes.remove(node.id)
    nodes.value = nodes.value.filter(n => n.id !== node.id)
    ElMessage.success(t('common.deleted'))
    await refresh()
  } catch (error) { ElMessage.error(errorMessage(error)) }
  finally { pendingId.value = '' }
}
async function create() {
  if (!form.workspace_root.startsWith('/') || form.workspace_root === '/') {
    ElMessage.warning(t('runtimeNodes.rootRequired')); return
  }
  if (caPemError.value) { ElMessage.warning(caPemError.value); return }
  creating.value = true
  try { generated.value = await runtimeNodes.createLink(installPayload()); await refresh() }
  catch (error) { ElMessage.error(errorMessage(error)) }
  finally { creating.value = false }
}
async function copy() {
  try { await navigator.clipboard.writeText(generated.value?.command ?? ''); ElMessage.success(t('common.copied')); dialog.value = false }
  catch { ElMessage.info(t('runtimeNodes.copyManually')) }
}
async function probe(row: RelayOut) {
  try { await relays.probe(row.id); await refresh() }
  catch (error) { ElMessage.error(errorMessage(error)) }
}
function capabilityState(node: RuntimeNode, provider: string) {
  const cap = node.capabilities[provider]
  if (!cap?.installed) return t('runtimeNodes.notInstalled')
  if (!node.online) return t('runtimeNodes.offline')
  return t(`runtimeNodes.login.${cap.login}`)
}
// 与安装链接的校验一致：写成 URL 或带路径的条目永远匹配不到仓库主机。
const GIT_HOST_RE = /^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$/
function invalidGitHosts(node: RuntimeNode) {
  return (node.git_hosts ?? []).filter(host => !GIT_HOST_RE.test(host))
}
onMounted(async () => {
  await refresh()
  if (canManage.value) {
    try { teamList.value = await teams.list() } catch { /* node list remains usable */ }
  }
  timer = setInterval(() => { void refresh(true) }, 10000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>
<template>
  <div class="runtime-nodes-view">
    <header class="page-header">
      <div>
        <h2>{{ t('runtimeNodes.title') }}</h2><p class="cm-page-intro">
          {{ t('runtimeNodes.intro') }}
        </p>
      </div>
      <el-button
        v-if="canManage"
        type="primary"
        data-test="install-runtime"
        @click="dialog = true; generated = null"
      >
        {{ t('runtimeNodes.install') }}
      </el-button>
    </header>
    <el-tabs v-model="tab">
      <el-tab-pane
        :label="t('runtimeNodes.nodes')"
        name="nodes"
      >
        <div class="toolbar">
          <el-input
            v-model="query"
            clearable
            :placeholder="t('runtimeNodes.search')"
          /><el-button @click="refresh()">
            {{ t('common.refresh') }}
          </el-button>
        </div>
        <el-empty
          v-if="!nodes.length && !loading"
          :description="t('runtimeNodes.empty')"
        >
          <el-button
            v-if="canManage"
            type="primary"
            @click="dialog = true; generated = null"
          >
            {{ t('runtimeNodes.install') }}
          </el-button>
        </el-empty>
        <div
          v-else
          class="table-scroll"
        >
          <el-table
            v-loading="loading"
            :data="filtered"
            row-key="id"
          >
            <el-table-column type="expand">
              <template #default="{ row }: { row: RuntimeNode }">
                <div class="node-detail">
                  <p>{{ t('runtimeNodes.workspace') }}: <code>{{ row.workspace_root }}</code></p>
                  <p>{{ t('runtimeNodes.version') }}: {{ row.version }} · {{ t('runtimeNodes.service') }}: {{ row.service_status }} · {{ t('runtimeNodes.heartbeat') }}: {{ formatDateTime(row.heartbeat_at) }}</p>
                  <div
                    class="git-hosts"
                    data-test="runtime-git-hosts"
                  >
                    <span>{{ t('runtimeNodes.nodeGitHosts') }}:</span>
                    <span v-if="row.git_hosts == null">{{ t('runtimeNodes.nodeGitHostsUnknown') }}</span>
                    <span v-else-if="!row.git_hosts.length">{{ t('runtimeNodes.nodeGitHostsEmpty') }}</span>
                    <template v-else>
                      <el-tag
                        v-for="host in row.git_hosts"
                        :key="host"
                        size="small"
                        :type="invalidGitHosts(row).includes(host) ? 'danger' : 'info'"
                      >
                        {{ host }}
                      </el-tag>
                    </template>
                    <small
                      v-if="invalidGitHosts(row).length"
                      class="git-hosts-invalid"
                    >{{ t('runtimeNodes.nodeGitHostsInvalid', { hosts: invalidGitHosts(row).join(', ') }) }}</small>
                    <small class="git-hosts-hint">{{ t('runtimeNodes.nodeGitHostsHint') }}</small>
                  </div>
                  <el-alert
                    v-if="['supervised', 'systemd-user-session'].includes(row.service_status)"
                    type="warning"
                    :closable="false"
                    :title="t(row.service_status === 'supervised' ? 'runtimeNodes.supervised' : 'runtimeNodes.sessionService')"
                  />
                  <div
                    v-for="backend in row.backends"
                    :key="backend.id"
                    class="backend-detail"
                  >
                    <ProviderBadge :provider="backend.model_provider" /><HealthTag :status="backend.health_status" />
                    <span>{{ t('runtimeNodes.quota') }}: 5h {{ backend.rate_limit_5h_used_pct == null ? '—' : `${backend.rate_limit_5h_used_pct}%` }} / 7d {{ backend.rate_limit_7d_used_pct == null ? '—' : `${backend.rate_limit_7d_used_pct}%` }}</span>
                    <span>{{ t('runtimeNodes.bots') }}: {{ backend.bot_count }}</span>
                    <RelayLiveStatus
                      v-if="canManage && row.online && row.is_active"
                      :relay-id="backend.id"
                      :name="backend.name"
                    />
                    <el-button
                      v-if="canManage"
                      size="small"
                      :disabled="!row.online || !row.is_active"
                      @click="probe(backend)"
                    >
                      {{ t('relays.probe') }}
                    </el-button>
                    <div class="backend-models">
                      {{ backend.effective_models.join(', ') || t('runtimeNodes.noModels') }}
                    </div>
                    <small>{{ row.capabilities[backend.model_provider]?.detail || backend.health_detail }}</small>
                  </div>
                </div>
              </template>
            </el-table-column>
            <el-table-column
              :label="t('runtimeNodes.nodes')"
              min-width="210"
            >
              <template #default="{ row }: { row: RuntimeNode }">
                <strong>{{ row.name }}</strong><div class="muted">
                  {{ row.username }}@{{ row.hostname }}
                </div><div class="muted">
                  {{ row.platform }} / {{ row.architecture }} / {{ row.environment }}
                </div>
              </template>
            </el-table-column>
            <el-table-column
              v-for="provider in ['claude', 'codex']"
              :key="provider"
              min-width="170"
            >
              <template #header>
                <ProviderBadge :provider="provider" />
              </template>
              <template #default="{ row }: { row: RuntimeNode }">
                <span>{{ capabilityState(row, provider) }}</span><div class="muted">
                  {{ row.capabilities[provider]?.version }}
                </div>
              </template>
            </el-table-column>
            <el-table-column
              :label="t('runtimeNodes.team')"
              min-width="110"
            >
              <template #default="{ row }: { row: RuntimeNode }">
                {{ row.team_name ?? t('bots.publicPool') }}
              </template>
            </el-table-column>
            <el-table-column
              :label="t('runtimeNodes.state')"
              width="130"
            >
              <template #default="{ row }: { row: RuntimeNode }">
                <el-tag :type="!row.is_active ? 'info' : row.online ? 'success' : 'danger'">
                  {{ !row.is_active ? t('common.disabled') : row.online ? t('runtimeNodes.online') : t('runtimeNodes.offline') }}
                </el-tag><div
                  v-if="row.draining"
                  class="muted"
                >
                  {{ t('runtimeNodes.draining') }}
                </div><div
                  v-if="row.max_concurrent != null && row.active_calls != null"
                  class="muted"
                  data-test="runtime-concurrency"
                >
                  {{ t('runtimeNodes.concurrencyUsage', { n: row.active_calls, total: row.max_concurrent }) }}
                </div>
              </template>
            </el-table-column>
            <el-table-column
              v-if="canManage"
              :label="t('common.actions')"
              min-width="290"
            >
              <template #default="{ row }: { row: RuntimeNode }">
                <div class="row-actions">
                  <el-button
                    text
                    type="primary"
                    data-test="edit-runtime"
                    @click="openEdit(row)"
                  >
                    {{ t('common.edit') }}
                  </el-button>
                  <el-switch
                    :model-value="row.is_active"
                    :aria-label="t('runtimeNodes.enable')"
                    :loading="pendingId === row.id"
                    data-test="toggle-runtime"
                    @change="toggleActive(row)"
                  />
                  <el-button
                    text
                    :disabled="pendingId === row.id"
                    data-test="drain-runtime"
                    @click="toggleDrain(row)"
                  >
                    {{ row.draining ? t('runtimeNodes.resume') : t('runtimeNodes.drain') }}
                  </el-button>
                  <el-button
                    text
                    type="danger"
                    :disabled="pendingId === row.id"
                    data-test="delete-runtime"
                    @click="remove(row)"
                  >
                    {{ t('common.delete') }}
                  </el-button>
                </div>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
      <el-tab-pane
        :label="t('relays.tabCatalog')"
        name="catalog"
      >
        <CatalogPanel />
      </el-tab-pane>
    </el-tabs>
    <el-dialog
      v-model="editDialog"
      :title="t('runtimeNodes.edit')"
      width="min(460px, 95vw)"
      :close-on-click-modal="false"
      :close-on-press-escape="!saving"
      :show-close="!saving"
      destroy-on-close
    >
      <el-form
        label-position="top"
        @submit.prevent="saveEdit"
      >
        <el-form-item
          :label="t('runtimeNodes.name')"
          required
        >
          <el-input
            v-model="editForm.name"
            data-test="runtime-name"
            maxlength="100"
            show-word-limit
            :disabled="saving"
          />
          <p class="muted field-hint">
            {{ t('runtimeNodes.renameHint') }}
          </p>
        </el-form-item>
        <el-form-item :label="t('runtimeNodes.team')">
          <el-select
            v-model="editForm.team_id"
            clearable
            :placeholder="t('bots.publicPool')"
            :disabled="saving"
            data-test="runtime-team"
          >
            <el-option
              v-for="team in teamList"
              :key="team.id"
              :value="team.id"
              :label="team.name_zh"
            />
          </el-select>
          <p class="muted field-hint">
            {{ t('runtimeNodes.teamHint') }}
          </p>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button
          :disabled="saving"
          @click="editDialog = false"
        >
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          type="primary"
          data-test="save-runtime"
          :loading="saving"
          :disabled="!editForm.name.trim() || editForm.name.trim().length > 100"
          @click="saveEdit"
        >
          {{ t('common.save') }}
        </el-button>
      </template>
    </el-dialog>
    <el-dialog
      v-model="dialog"
      :title="t('runtimeNodes.install')"
      width="min(680px, 95vw)"
      destroy-on-close
      @closed="generated = null"
    >
      <template v-if="!generated">
        <p>{{ t('runtimeNodes.installHint') }}</p>
        <el-form
          label-position="top"
          @submit.prevent="create"
        >
          <el-form-item
            :label="t('runtimeNodes.workspace')"
            required
          >
            <el-input
              v-model="form.workspace_root"
              placeholder="/home/ai/projects"
              data-test="runtime-root"
            /><small>{{ t('runtimeNodes.rootHint') }}</small>
          </el-form-item>
          <el-form-item :label="t('runtimeNodes.name')">
            <el-input
              v-model="form.name"
              :placeholder="t('runtimeNodes.nameAuto')"
            />
          </el-form-item>
          <el-form-item :label="t('runtimeNodes.team')">
            <el-select
              v-model="form.team_id"
              clearable
              :placeholder="t('bots.publicPool')"
            >
              <el-option
                v-for="team in teamList"
                :key="team.id"
                :value="team.id"
                :label="team.name_zh"
              />
            </el-select>
          </el-form-item>
          <p class="muted">
            {{ t('runtimeNodes.validityHint') }}
          </p>
          <el-collapse>
            <el-collapse-item
              :title="t('runtimeNodes.advanced')"
              name="advanced"
            >
              <el-form-item :label="t('runtimeNodes.proxy')">
                <el-input
                  v-model="form.options.proxy"
                  placeholder="http://127.0.0.1:7890"
                />
                <p class="muted field-hint">
                  {{ t('runtimeNodes.proxyHint') }}
                </p>
              </el-form-item>
              <el-form-item :label="t('runtimeNodes.controlProxy')">
                <el-input
                  v-model="form.options.control_proxy"
                  placeholder="http://127.0.0.1:7890"
                />
                <p class="muted field-hint">
                  {{ t('runtimeNodes.controlProxyHint') }}
                </p>
              </el-form-item>
              <el-form-item
                :label="t('runtimeNodes.caPem')"
                :error="caPemError"
                data-test="runtime-ca-pem"
              >
                <el-input
                  v-model="form.options.ca_pem"
                  type="textarea"
                  :rows="4"
                  placeholder="-----BEGIN CERTIFICATE-----"
                />
                <p class="muted field-hint">
                  {{ t('runtimeNodes.caPemHint') }}
                </p>
              </el-form-item>
              <el-form-item :label="t('runtimeNodes.environment')">
                <el-select v-model="form.options.environment">
                  <el-option
                    v-for="item in ['auto', 'host', 'chroot', 'nspawn']"
                    :key="item"
                    :value="item"
                    :label="item === 'auto' ? t('runtimeNodes.autoDiscover') : item"
                  />
                </el-select>
              </el-form-item>
              <el-form-item label="Claude CLI">
                <el-input
                  v-model="form.options.claude_path"
                  :placeholder="t('runtimeNodes.autoDiscover')"
                />
              </el-form-item>
              <el-form-item label="Codex CLI">
                <el-input
                  v-model="form.options.codex_path"
                  :placeholder="t('runtimeNodes.autoDiscover')"
                />
              </el-form-item>
              <el-form-item
                :label="t('runtimeNodes.gitHosts')"
                data-test="runtime-git-hosts"
              >
                <el-select
                  v-model="form.options.git_hosts"
                  multiple
                  filterable
                  allow-create
                  default-first-option
                  :reserve-keyword="false"
                  placeholder="github.com"
                />
                <p class="muted proxy-hint">
                  {{ t('runtimeNodes.gitHostsHint') }}
                </p>
              </el-form-item>
              <el-form-item :label="t('runtimeNodes.concurrency')">
                <el-input-number
                  v-model="form.options.max_concurrent"
                  :min="1"
                  :max="32"
                />
              </el-form-item>
              <el-checkbox v-model="form.options.install_claude_probe">
                {{ t('runtimeNodes.claudeProbe') }}
              </el-checkbox><p class="muted">
                {{ t('runtimeNodes.probeHint') }}
              </p>
            </el-collapse-item>
          </el-collapse>
        </el-form>
      </template>
      <template v-else>
        <el-alert
          type="success"
          :closable="false"
          :title="t('runtimeNodes.linkReady')"
        /><p>{{ t('runtimeNodes.commandHint') }}</p><el-input
          :model-value="generated.command"
          type="textarea"
          :rows="4"
          readonly
          data-test="runtime-install-command"
        /><p>{{ t('runtimeNodes.expires') }}: {{ formatDateTime(generated.expires_at) }}</p><small>{{ t('runtimeNodes.credentialHint') }}</small><p><small data-test="runtime-replace-hint">{{ t('runtimeNodes.replaceHint') }}</small></p>
      </template>
      <template #footer>
        <el-button @click="dialog = false">
          {{ t('common.close') }}
        </el-button><el-button
          v-if="!generated"
          type="primary"
          :loading="creating"
          data-test="create-runtime-link"
          @click="create"
        >
          {{ t('runtimeNodes.createLink') }}
        </el-button><el-button
          v-else
          type="primary"
          @click="copy"
        >
          {{ t('runtimeNodes.copyCommand') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>
<style scoped>
.page-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
.page-header h2 { margin: 0 0 8px; }
.toolbar { display: flex; gap: 12px; margin: 12px 0 20px; max-width: 520px; }
.muted, small { color: var(--el-text-color-secondary); font-size: 12px; }
.field-hint { width: 100%; margin: 6px 0 0; line-height: 1.6; overflow-wrap: anywhere; }
.table-scroll { overflow-x: auto; }
.row-actions { display: flex; flex-wrap: nowrap; align-items: center; gap: 4px; white-space: nowrap; }
.row-actions .el-button + .el-button { margin-left: 0; }
.node-detail { padding: 10px 28px 24px; }
.git-hosts { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin: 1em 0; }
.git-hosts .el-tag { max-width: 100%; height: auto; white-space: normal; overflow-wrap: anywhere; }
.git-hosts-invalid, .git-hosts-hint { flex-basis: 100%; }
.git-hosts-invalid { color: var(--el-color-danger); }
.backend-detail { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; padding: 18px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.backend-models { width: 100%; overflow-wrap: anywhere; font-size: 13px; }
@media (max-width: 640px) { .page-header { align-items: flex-start; flex-direction: column; } .node-detail { padding: 12px; } }
</style>
