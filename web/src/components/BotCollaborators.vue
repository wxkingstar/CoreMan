<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessageBox } from 'element-plus'
import { collaboration, type CollaborationRoute, type CollaborationPeer, type CollaborationGroup } from '@/api/collaboration'
import { errorMessage } from '@/utils/errors'
const props = defineProps<{ botId: string; sourceName: string; canEdit: boolean; active: boolean }>()
const { t, te } = useI18n()
const rows = ref<CollaborationRoute[]>([]), peers = ref<CollaborationPeer[]>([]), groups = ref<CollaborationGroup[]>([])
const loading = ref(false), optionsLoading = ref(false), groupsLoading = ref(false), busy = ref(false), visible = ref(false)
const error = ref(''), formError = ref(''), target = ref(''), chat = ref(''), current = ref<CollaborationRoute | null>(null)
let listSequence = 0
let generation = 0, optionsGeneration = 0, groupsGeneration = 0
let poll: ReturnType<typeof setTimeout> | undefined
let controller = new AbortController()
const permitted = computed(() => props.active && props.canEdit)
function stop() { clearTimeout(poll); controller.abort(); controller = new AbortController(); generation++; optionsGeneration++; groupsGeneration++ }
function schedule() {
 clearTimeout(poll)
 if (permitted.value && !error.value && rows.value.some(r => r.status === 'pending')) poll = setTimeout(() => void refresh(), 3000)
}
async function refresh() {
 if (!permitted.value || busy.value) { schedule(); return }
 const sequence = ++listSequence
 const epoch = generation
 loading.value = true; error.value = ''
 try {
  const result = await collaboration.list(props.botId, controller.signal)
  if (epoch !== generation || sequence !== listSequence) return
  rows.value = result
  if (current.value) current.value = result.find(r => r.id === current.value?.id) ?? null
 } catch (e) { if (epoch === generation && sequence === listSequence) error.value = errorMessage(e) }
 finally { if (epoch === generation && sequence === listSequence) { loading.value = false; schedule() } }
}
watch(() => [props.botId, props.active, props.canEdit], () => {
 stop(); rows.value = []; current.value = null; visible.value = false; busy.value = false; loading.value = false
 if (permitted.value) void refresh()
}, { immediate: true })
onBeforeUnmount(stop)
async function search(q = '') {
 const epoch = ++optionsGeneration
 optionsLoading.value = true; formError.value = ''
 try { const result = await collaboration.options(props.botId, q, controller.signal); if (epoch === optionsGeneration) peers.value = result }
 catch (e) { if (epoch === optionsGeneration) formError.value = errorMessage(e) }
 finally { if (epoch === optionsGeneration) optionsLoading.value = false }
}
async function loadGroups() {
 const epoch = ++groupsGeneration
 chat.value = ''; groups.value = []; formError.value = ''
 if (!target.value) return
 groupsLoading.value = true
 try { const result = await collaboration.groups(props.botId, target.value, controller.signal); if (epoch === groupsGeneration) groups.value = result }
 catch (e) { if (epoch === groupsGeneration) formError.value = errorMessage(e) }
 finally { if (epoch === groupsGeneration) groupsLoading.value = false }
}
function open(row?: CollaborationRoute) {
 if (busy.value || !permitted.value) return
 current.value = row ?? null; target.value = row?.target_bot_id ?? ''; chat.value = row?.chat_id ?? ''
 formError.value = ''; peers.value = []; groups.value = []; visible.value = true
 if (!row) void search()
}
async function confirm(message: string) {
 try { await ElMessageBox.confirm(message, t('collaboration.title'), { type: 'warning', confirmButtonText: t('collaboration.confirm'), cancelButtonText: t('collaboration.cancel') }); return true } catch { return false }
}
async function close(done?: () => void) {
 if (busy.value) return
 if (!current.value && target.value && !await confirm(t('collaboration.discard'))) return
 optionsGeneration++; groupsGeneration++; visible.value = false; done?.()
}
function replace(row: CollaborationRoute) {
 const index = rows.value.findIndex(r => r.id === row.id)
 if (index >= 0) rows.value[index] = row
 else rows.value.push(row)
 if (visible.value) current.value = row
 schedule()
}
async function verify() {
 if (busy.value || !permitted.value || current.value?.status === 'pending') return
 if (!current.value && (!target.value || !chat.value)) return
 const epoch = generation
 listSequence++; loading.value = false; error.value = ''; busy.value = true; formError.value = ''
 try {
  const row = current.value
  const result = row ? await collaboration.verify(props.botId, row.id, row.version) : await collaboration.create(props.botId, target.value, chat.value)
  if (epoch === generation) replace(result)
 } catch (e) { if (epoch === generation) formError.value = errorMessage(e) }
 finally { if (epoch === generation) { busy.value = false; schedule() } }
}
async function mutate(row: CollaborationRoute, action: 'enable' | 'pause' | 'remove') {
 if (busy.value || !permitted.value) return
 const epoch = generation
 listSequence++; loading.value = false; busy.value = true
 try {
  if (action !== 'enable' && !await confirm(t(`collaboration.${action}Confirm`, { count: row.active_count ?? 0 }))) return
  if (epoch !== generation) return
  formError.value = ''; error.value = ''
  if (action === 'remove') {
   await collaboration.remove(props.botId, row.id, row.version)
   if (epoch === generation) rows.value = rows.value.filter(r => r.id !== row.id)
  } else {
   const result = await collaboration.update(props.botId, row.id, action === 'enable', row.version)
   if (epoch === generation) { replace(result); visible.value = false }
  }
 } catch (e) { if (epoch === generation) { if (visible.value) formError.value = errorMessage(e); else error.value = errorMessage(e) } }
 finally { if (epoch === generation) { busy.value = false; schedule() } }
}
function reason(row: CollaborationRoute) {
 const key = `collaboration.reasons.${row.reason}`
 return row.reason && te(key) ? t(key) : ''
}
function status(row: CollaborationRoute) { return row.status !== 'ready' ? row.status : row.enabled ? 'enabled' : 'paused' }
</script>

<template>
  <section
    class="collaboration-panel"
    :aria-label="t('collaboration.title')"
  >
    <el-empty
      v-if="!canEdit"
      :description="t('collaboration.noAccess')"
    />
    <template v-else>
      <div class="partners-heading">
        <p>{{ t('collaboration.intro') }}</p>
        <el-button
          type="primary"
          data-test="add-partner"
          :disabled="busy"
          @click="open()"
        >
          {{ t('collaboration.add') }}
        </el-button>
      </div>
      <el-alert
        v-if="error"
        :title="error"
        type="error"
        :closable="false"
        role="alert"
      />
      <el-button
        v-if="error"
        data-test="retry-list"
        :disabled="loading || busy"
        @click="refresh"
      >
        {{ t('collaboration.retry') }}
      </el-button>
      <p
        v-if="loading && !rows.length"
        role="status"
      >
        {{ t('collaboration.loading') }}
      </p>
      <el-empty
        v-else-if="!rows.length && !error"
        :description="t('collaboration.empty')"
      />
      <ul
        v-else
        class="partner-list"
      >
        <li
          v-for="row in rows"
          :key="row.id"
          class="partner-row"
        >
          <div class="partner-info">
            <strong>{{ row.target_name }}</strong>
            <p
              v-if="reason(row)"
              role="status"
            >
              {{ reason(row) }}
            </p>
            <p>{{ row.target_description }}</p>
            <p>{{ t('collaboration.direction', { source: sourceName, target: row.target_name }) }}</p>
            <p>{{ row.chat_name }} <span v-if="row.active_count"> · {{ t('collaboration.active', { count: row.active_count }) }}</span></p>
            <el-tag :type="status(row) === 'enabled' ? 'success' : row.status === 'failed' ? 'danger' : 'info'">
              {{ t(`collaboration.status.${status(row)}`) }}
            </el-tag>
          </div>
          <div class="partner-actions">
            <el-button
              v-if="row.enabled"
              :disabled="busy"
              @click="mutate(row, 'pause')"
            >
              {{ t('collaboration.pause') }}
            </el-button>
            <el-button
              v-else-if="row.can_enable"
              :disabled="busy"
              @click="mutate(row, 'enable')"
            >
              {{ t('collaboration.enable') }}
            </el-button>
            <el-button
              v-if="!row.enabled && (row.can_verify || row.status === 'pending')"
              :disabled="busy"
              @click="open(row)"
            >
              {{ t(row.status === 'pending' ? 'collaboration.status.pending' : 'collaboration.verify') }}
            </el-button>
            <el-button
              v-if="row.can_remove"
              type="danger"
              plain
              :disabled="busy"
              @click="mutate(row, 'remove')"
            >
              {{ t('collaboration.remove') }}
            </el-button>
          </div>
        </li>
      </ul>
      <el-drawer
        v-model="visible"
        class="collaboration-drawer"
        :title="t('collaboration.add')"
        size="min(560px, 100vw)"
        :before-close="close"
        :close-on-click-modal="false"
        destroy-on-close
      >
        <el-steps
          :active="current ? (current.status === 'ready' ? 3 : 2) : target ? 1 : 0"
          finish-status="success"
          simple
        >
          <el-step :title="t('collaboration.steps.peer')" /><el-step :title="t('collaboration.steps.group')" /><el-step :title="t('collaboration.steps.verify')" />
        </el-steps>
        <el-form
          class="partner-form"
          label-position="top"
        >
          <template v-if="!current">
            <el-form-item :label="t('collaboration.peer')">
              <el-select
                v-model="target"
                data-test="partner-select"
                filterable
                remote
                :remote-method="search"
                :loading="optionsLoading"
                :disabled="busy"
                :placeholder="t('collaboration.search')"
                :no-data-text="t('collaboration.noPeers')"
                :aria-label="t('collaboration.peer')"
                @change="loadGroups"
              >
                <el-option
                  v-for="peer in peers"
                  :key="peer.id"
                  :value="peer.id"
                  :label="peer.available && peer.enabled ? peer.name : `${peer.name} — ${t('collaboration.unavailable')}`"
                  :disabled="!peer.available || !peer.enabled"
                />
              </el-select>
            </el-form-item>
            <el-form-item :label="t('collaboration.group')">
              <el-select
                v-model="chat"
                data-test="group-select"
                filterable
                :placeholder="t('collaboration.groupPlaceholder')"
                :no-data-text="t('collaboration.noGroupOptions')"
                :no-match-text="t('collaboration.noGroupMatch')"
                :disabled="!target || busy || groupsLoading"
                :loading="groupsLoading"
                :aria-label="t('collaboration.group')"
              >
                <el-option
                  v-for="group in groups"
                  :key="group.chat_id"
                  :label="group.name"
                  :value="group.chat_id"
                />
              </el-select>
              <p v-if="target && !groupsLoading && !groups.length">
                {{ t('collaboration.noGroups') }}
              </p>
            </el-form-item>
          </template>
          <template v-else>
            <p>{{ t('collaboration.direction', { source: sourceName, target: current.target_name }) }}</p><p>{{ current.chat_name }}</p>
          </template>
          <el-alert
            :title="t('collaboration.warning')"
            type="warning"
            :closable="false"
          />
          <p
            v-if="current"
            role="status"
          >
            {{ reason(current) || t(`collaboration.${current.status === 'pending' ? 'waiting' : current.status}`) }}
          </p>
          <el-alert
            v-if="formError"
            :title="formError"
            type="error"
            :closable="false"
            role="alert"
          />
          <el-button
            v-if="formError"
            :disabled="busy"
            @click="current ? refresh() : target ? loadGroups() : search()"
          >
            {{ t('collaboration.retry') }}
          </el-button>
        </el-form>
        <template #footer>
          <el-button
            :disabled="busy"
            @click="close()"
          >
            {{ t('collaboration.close') }}
          </el-button>
          <el-button
            v-if="current?.can_enable"
            type="primary"
            :loading="busy"
            @click="mutate(current, 'enable')"
          >
            {{ t('collaboration.enable') }}
          </el-button>
          <el-button
            v-else
            type="primary"
            data-test="verify-partner"
            :loading="busy"
            :disabled="busy || (current ? !current.can_verify || current.status === 'pending' : !target || !chat)"
            @click="verify"
          >
            {{ t('collaboration.verify') }}
          </el-button>
        </template>
      </el-drawer>
    </template>
  </section>
</template>

<style scoped>
:global(.collaboration-drawer.el-drawer) { height: 100vh; height: 100dvh; max-height: 100dvh; overflow: hidden; }
:global(.collaboration-drawer > .el-drawer__header), :global(.collaboration-drawer > .el-drawer__footer) { flex: 0 0 auto; }
:global(.collaboration-drawer > .el-drawer__body) { flex: 1 1 0; min-height: 0; overflow-y: auto; }

.partners-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 20px; }
.partners-heading p { max-width: 70ch; margin: 0; color: var(--el-text-color-secondary); line-height: 1.7; }
.partner-list { padding: 0; margin: 16px 0; list-style: none; }
.partner-row { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 20px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.partner-info { min-width: 0; overflow-wrap: anywhere; }
.partner-info p { margin: 6px 0; color: var(--el-text-color-secondary); }
.partner-actions { display: flex; gap: 8px; flex-wrap: wrap; flex-shrink: 0; }
.partner-actions .el-button { margin-left: 0; }
.partner-form { margin-top: 24px; }
.partner-form .el-select { width: 100%; }
.partner-form .el-alert { margin: 16px 0; }
@media (max-width: 767px) { .partners-heading, .partner-row { flex-direction: column; align-items: stretch; gap: 16px; } }
</style>
