<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { collaboration, type CollaborationRoute, type CollaborationPeer } from '@/api/collaboration'
import { errorMessage } from '@/utils/errors'
const props = defineProps<{ botId: string; sourceName: string; canEdit: boolean; active: boolean }>()
const { t, te } = useI18n()
const rows = ref<CollaborationRoute[]>([]), peers = ref<CollaborationPeer[]>([])
const loading = ref(false), optionsLoading = ref(false), busy = ref(false), visible = ref(false)
const error = ref(''), formError = ref(''), optionsError = ref(''), target = ref('')
let listSequence = 0
let generation = 0, optionsGeneration = 0
const query = ref('')
let controller = new AbortController()
const permitted = computed(() => props.active && props.canEdit)
function stop() { controller.abort(); controller = new AbortController(); generation++; optionsGeneration++; listSequence++ }
async function refresh() {
 if (!permitted.value || busy.value) return
 const sequence = ++listSequence
 const epoch = generation
 loading.value = true; error.value = ''
 try {
  const result = await collaboration.list(props.botId, controller.signal)
  if (epoch !== generation || sequence !== listSequence) return
  rows.value = result
 } catch (e) { if (epoch === generation && sequence === listSequence) error.value = errorMessage(e) }
 finally { if (epoch === generation && sequence === listSequence) { loading.value = false } }
}
watch(() => [props.botId, props.active, props.canEdit], () => {
 stop(); rows.value = []; peers.value = []; target.value = ''; error.value = ''; formError.value = ''; optionsError.value = ''; visible.value = false; busy.value = false; loading.value = false; optionsLoading.value = false
 if (permitted.value) void refresh()
}, { immediate: true })
onBeforeUnmount(stop)
async function search(q = '') {
 if (!permitted.value || !visible.value) return
 query.value = q
 const epoch = ++optionsGeneration
 optionsLoading.value = true; optionsError.value = ''
 try { const result = await collaboration.options(props.botId, q, controller.signal); if (epoch === optionsGeneration) peers.value = result.filter(peer => peer.id !== props.botId) }
 catch (e) { if (epoch === optionsGeneration) optionsError.value = errorMessage(e) }
 finally { if (epoch === optionsGeneration) optionsLoading.value = false }
}
function open() {
 if (busy.value || !permitted.value) return
 target.value = ''; formError.value = ''; peers.value = []; visible.value = true
 void search()
}
async function confirm(message: string) {
 try { await ElMessageBox.confirm(message, t('collaboration.title'), { type: 'warning', confirmButtonText: t('collaboration.confirm'), cancelButtonText: t('collaboration.cancel') }); return true } catch { return false }
}
async function close(done?: () => void) {
 if (busy.value) return
 const epoch = generation
 if (target.value && !await confirm(t('collaboration.discard'))) return
 if (epoch !== generation) return
 optionsGeneration++; visible.value = false; done?.()
}
function replace(row: CollaborationRoute) {
 const index = rows.value.findIndex(r => r.id === row.id)
 if (index >= 0) rows.value[index] = row
 else rows.value.push(row)
}
async function save() {
 if (busy.value || !permitted.value || !target.value || rows.value.some(r => r.target_bot_id === target.value)) return
 const epoch = generation
 listSequence++; loading.value = false; error.value = ''; busy.value = true; formError.value = ''
 try {
  const result = await collaboration.create(props.botId, target.value)
  if (epoch !== generation) return
  replace(result); optionsGeneration++; visible.value = false; target.value = ''
  ElMessage.success(t('collaboration.saved'))
 } catch (e) { if (epoch === generation) formError.value = errorMessage(e) }
 finally { if (epoch === generation) busy.value = false }
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
 finally { if (epoch === generation) { busy.value = false } }
}
function reason(row: CollaborationRoute) {
 const key = `collaboration.reasons.${row.reason}`
 const verificationKey = `collaboration.reasons.${row.verification_error}`
 if (row.verification_error) return te(verificationKey) ? t(verificationKey) : row.verification_error
 return row.reason && te(key) ? t(key) : ''
}
function status(row: CollaborationRoute) {
 if (!row.enabled) return 'paused'
 if (row.status === 'ready') return 'enabled'
 if (row.status === 'verified') return 'verified'
 return row.status
}
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
      <h3 class="section-title">
        {{ t('collaboration.aiTitle') }}
      </h3>
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
            <p v-if="row.active_count">
              {{ t('collaboration.active', { count: row.active_count }) }}
            </p>
            <el-tag :type="status(row) === 'enabled' ? 'success' : 'info'">
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
        <el-form
          class="partner-form"
          label-position="top"
        >
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
            >
              <el-option
                v-for="peer in peers"
                :key="peer.id"
                :value="peer.id"
                :label="rows.some(row => row.target_bot_id === peer.id) ? `${peer.name} (${t('collaboration.alreadyAdded')})` : peer.available && peer.enabled ? peer.name : `${peer.name} (${t('collaboration.unavailable')})`"
                :disabled="rows.some(row => row.target_bot_id === peer.id)"
              />
            </el-select>
          </el-form-item>
          <!-- A remote select stays closed while it has no options; say so instead of nothing. -->
          <el-alert
            v-if="!optionsLoading && !optionsError && !peers.length"
            data-test="no-peers"
            :title="query ? t('collaboration.noPeers') : t('collaboration.noAiPeers')"
            type="warning"
            :closable="false"
          />
          <el-alert
            :title="t('collaboration.usageHint')"
            type="info"
            :closable="false"
          />
          <el-alert
            v-if="formError || optionsError"
            :title="formError || optionsError"
            type="error"
            :closable="false"
            role="alert"
          />
          <el-button
            v-if="optionsError"
            :disabled="busy"
            @click="search(query)"
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
            type="primary"
            data-test="save-partner"
            :loading="busy"
            :disabled="busy || !target || rows.some(row => row.target_bot_id === target)"
            @click="save"
          >
            {{ t('collaboration.save') }}
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

.section-title { margin: 0 0 12px; font-size: 16px; font-weight: 600; }
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
