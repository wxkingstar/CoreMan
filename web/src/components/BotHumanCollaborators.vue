<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { humanCollaboration as api, type HumanCollaborator, type HumanOption } from '@/api/collaboration'
import { errorMessage } from '@/utils/errors'
const props = defineProps<{ botId: string; canEdit: boolean; active: boolean }>()
const { t } = useI18n()
const rows = ref<HumanCollaborator[]>([]), people = ref<HumanOption[]>([])
const loading = ref(false), optionsLoading = ref(false), busy = ref(false), visible = ref(false)
const error = ref(''), formError = ref(''), optionsError = ref(''), target = ref(''), responsibility = ref('')
const editing = ref<HumanCollaborator | null>(null)
let generation = 0, optionsGeneration = 0, listSequence = 0
let controller = new AbortController()
const permitted = computed(() => props.active && props.canEdit)
const dirty = computed(() => editing.value ? responsibility.value.trim() !== editing.value.responsibility : !!target.value)
function stop() { controller.abort(); controller = new AbortController(); generation++; optionsGeneration++; listSequence++ }
async function refresh() {
 if (!permitted.value || busy.value) return
 const sequence = ++listSequence, epoch = generation
 loading.value = true; error.value = ''
 try { const result = await api.list(props.botId, controller.signal); if (epoch === generation && sequence === listSequence) rows.value = result }
 catch (e) { if (epoch === generation && sequence === listSequence) error.value = errorMessage(e) }
 finally { if (epoch === generation && sequence === listSequence) loading.value = false }
}
watch(() => [props.botId, props.active, props.canEdit], () => {
 stop(); rows.value = []; people.value = []; target.value = ''; responsibility.value = ''; editing.value = null; error.value = ''; formError.value = ''; optionsError.value = ''; visible.value = false; busy.value = false; loading.value = false; optionsLoading.value = false
 if (permitted.value) void refresh()
}, { immediate: true })
onBeforeUnmount(stop)
async function search(q = '') {
 if (!permitted.value || !visible.value || editing.value) return
 const epoch = ++optionsGeneration
 optionsLoading.value = true; optionsError.value = ''
 try { const result = await api.options(props.botId, q, controller.signal); if (epoch === optionsGeneration) people.value = result }
 catch (e) { if (epoch === optionsGeneration) optionsError.value = errorMessage(e) }
 finally { if (epoch === optionsGeneration) optionsLoading.value = false }
}
function open(row?: HumanCollaborator) {
 if (busy.value || !permitted.value) return
 editing.value = row ?? null; target.value = row?.user_id ?? ''; responsibility.value = row?.responsibility ?? ''
 formError.value = ''; people.value = row ? [{ id: row.user_id, name: row.name, login_name: null, position: row.position, department: row.department, added: true }] : []; visible.value = true
 if (!row) void search()
}
async function confirm(message: string) {
 try { await ElMessageBox.confirm(message, t('collaboration.humanTitle'), { type: 'warning', confirmButtonText: t('collaboration.confirm'), cancelButtonText: t('collaboration.cancel') }); return true } catch { return false }
}
async function close(done?: () => void) {
 if (busy.value) return
 const epoch = generation
 if (dirty.value && !await confirm(t('collaboration.discard'))) return
 if (epoch !== generation) return
 optionsGeneration++; visible.value = false; editing.value = null; done?.()
}
function replace(row: HumanCollaborator) {
 const index = rows.value.findIndex(r => r.id === row.id)
 if (index >= 0) rows.value[index] = row
 else rows.value.push(row)
}
async function save() {
 if (busy.value || !permitted.value || !target.value) return
 const epoch = generation, current = editing.value
 listSequence++; loading.value = false; error.value = ''; busy.value = true; formError.value = ''
 try {
  const result = current
   ? await api.update(props.botId, current.id, { responsibility: responsibility.value.trim() }, current.version)
   : await api.create(props.botId, target.value, responsibility.value.trim())
  if (epoch !== generation) return
  replace(result); optionsGeneration++; visible.value = false; editing.value = null; target.value = ''
  ElMessage.success(t(current ? 'collaboration.humanUpdated' : 'collaboration.humanSaved'))
 } catch (e) { if (epoch === generation) formError.value = errorMessage(e) }
 finally { if (epoch === generation) busy.value = false }
}
async function mutate(row: HumanCollaborator, action: 'enable' | 'pause' | 'remove') {
 if (busy.value || !permitted.value) return
 const epoch = generation
 listSequence++; loading.value = false; busy.value = true
 try {
  if (action !== 'enable' && !await confirm(t(action === 'pause' ? 'collaboration.humanPauseConfirm' : 'collaboration.humanRemoveConfirm', { count: row.active_count }))) return
  if (epoch !== generation) return
  error.value = ''
  if (action === 'remove') {
   await api.remove(props.botId, row.id, row.version)
   if (epoch === generation) rows.value = rows.value.filter(r => r.id !== row.id)
  } else {
   const result = await api.update(props.botId, row.id, { enabled: action === 'enable' }, row.version)
   if (epoch === generation) replace(result)
  }
 } catch (e) { if (epoch === generation) error.value = errorMessage(e) }
 finally { if (epoch === generation) busy.value = false }
}
function label(p: HumanOption) {
 const detail = [p.department, p.position].filter(Boolean).join(' · ')
 const name = detail ? `${p.name}（${detail}）` : p.name
 return p.added && !editing.value ? `${name} (${t('collaboration.alreadyAdded')})` : name
}
function status(row: HumanCollaborator) { return !row.enabled ? 'paused' : row.reachable ? 'enabled' : 'unreachable' }
</script>

<template>
  <section
    class="collaboration-panel human-panel"
    :aria-label="t('collaboration.humanTitle')"
  >
    <template v-if="canEdit">
      <h3 class="section-title">
        {{ t('collaboration.humanTitle') }}
      </h3>
      <div class="partners-heading">
        <p>{{ t('collaboration.humanIntro') }}</p>
        <el-button
          type="primary"
          data-test="add-human"
          :disabled="busy"
          @click="open()"
        >
          {{ t('collaboration.humanAdd') }}
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
        data-test="retry-human-list"
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
        :description="t('collaboration.humanEmpty')"
      />
      <ul
        v-else
        class="partner-list"
      >
        <li
          v-for="row in rows"
          :key="row.id"
          class="partner-row"
          data-test="human-row"
        >
          <div class="partner-info">
            <strong>{{ row.name }}</strong>
            <p v-if="row.department || row.position">
              {{ [row.department, row.position].filter(Boolean).join(' · ') }}
            </p>
            <p>{{ row.responsibility || t('collaboration.noResponsibility') }}</p>
            <p
              v-if="!row.reachable"
              role="status"
            >
              {{ t('collaboration.unreachable') }}
            </p>
            <p v-if="row.active_count">
              {{ t('collaboration.humanActive', { count: row.active_count }) }}
            </p>
            <el-tag :type="status(row) === 'enabled' ? 'success' : 'info'">
              {{ t(`collaboration.humanStatus.${status(row)}`) }}
            </el-tag>
          </div>
          <div class="partner-actions">
            <el-button
              :disabled="busy"
              @click="open(row)"
            >
              {{ t('collaboration.humanEdit') }}
            </el-button>
            <el-button
              v-if="row.enabled"
              :disabled="busy"
              @click="mutate(row, 'pause')"
            >
              {{ t('collaboration.pause') }}
            </el-button>
            <el-button
              v-else
              :disabled="busy"
              @click="mutate(row, 'enable')"
            >
              {{ t('collaboration.enable') }}
            </el-button>
            <el-button
              type="danger"
              plain
              :disabled="busy"
              @click="mutate(row, 'remove')"
            >
              {{ t('collaboration.humanRemove') }}
            </el-button>
          </div>
        </li>
      </ul>
      <el-drawer
        v-model="visible"
        class="collaboration-drawer"
        :title="editing ? t('collaboration.humanEdit') : t('collaboration.humanAdd')"
        size="min(560px, 100vw)"
        :before-close="close"
        :close-on-click-modal="false"
        destroy-on-close
      >
        <el-form
          class="partner-form"
          label-position="top"
        >
          <el-form-item :label="t('collaboration.humanPeer')">
            <el-select
              v-model="target"
              data-test="human-select"
              filterable
              remote
              :remote-method="search"
              :loading="optionsLoading"
              :disabled="busy || !!editing"
              :placeholder="t('collaboration.humanSearch')"
              :no-data-text="t('collaboration.humanNoPeers')"
              :aria-label="t('collaboration.humanPeer')"
            >
              <el-option
                v-for="p in people"
                :key="p.id"
                :value="p.id"
                :label="label(p)"
                :disabled="p.added && !editing"
              />
            </el-select>
          </el-form-item>
          <el-alert
            v-if="!editing && !optionsLoading && !optionsError && !people.length"
            data-test="no-humans"
            :title="t('collaboration.humanNoPeers')"
            type="warning"
            :closable="false"
          />
          <el-form-item :label="t('collaboration.responsibility')">
            <el-input
              v-model="responsibility"
              data-test="human-responsibility"
              type="textarea"
              :rows="3"
              maxlength="1000"
              show-word-limit
              :disabled="busy"
              :placeholder="t('collaboration.responsibilityPlaceholder')"
            />
          </el-form-item>
          <el-alert
            :title="t('collaboration.humanHint')"
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
            data-test="save-human"
            :loading="busy"
            :disabled="busy || !target || (!!editing && !dirty)"
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
.human-panel { margin-top: 32px; padding-top: 24px; border-top: 1px solid var(--el-border-color-lighter); }
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
