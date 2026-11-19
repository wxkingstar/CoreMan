<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { CLOSE_REASONS, myAssistant, type CloseReason } from '@/api/myAssistant'
import { laterThan } from '@/utils/assistantFollowups'
import { errorMessage } from '@/utils/errors'

/** 对一条待跟进的三个动作：补充、改期、关闭（选原因）。总表与「待跟进」共用；做完发 `done`。 */
export type FollowupAction = 'supplement' | 'reschedule' | 'close'
const MAX = 1000

const props = defineProps<{ today: string }>()
const emit = defineEmits<{ done: [] }>()
const { t } = useI18n()
const visible = ref(false), busy = ref(false)
const state = reactive<{ action: FollowupAction; id: string; title: string; text: string; until: string; reason: CloseReason | '' }>({
  action: 'supplement', id: '', title: '', text: '', until: '', reason: '',
})

function open(action: FollowupAction, item: { id: string; title?: string | null; locked?: boolean }) {
  Object.assign(state, { action, id: item.id, title: item.locked ? '' : (item.title ?? ''), text: '', until: '', reason: '' })
  visible.value = true
}

/** 日期串是所有者时区的 YYYY-MM-DD：按日历加天数，不经过本机时区。 */
function addDays(day: string, n: number) {
  const [y, m, d] = day.split('-').map(Number)
  const date = new Date(Date.UTC(y, m - 1, d + n))
  return date.toISOString().slice(0, 10)
}
function weekday(day: string) {
  const [y, m, d] = day.split('-').map(Number)
  return new Date(Date.UTC(y, m - 1, d)).getUTCDay()
}
const quick = computed(() => {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(props.today)) return []
  const toMonday = ((8 - weekday(props.today)) % 7) || 7
  return [
    { key: 'tomorrow', value: addDays(props.today, 1) },
    { key: 'nextMonday', value: addDays(props.today, toMonday) },
    { key: 'nextWeek', value: addDays(props.today, 7) },
  ]
})
const disabledDate = (date: Date) => {
  const value = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
  return !laterThan(value, props.today)
}

const text = computed(() => state.text.trim())
const ready = computed(() => {
  if (text.value.length > MAX) return false
  if (state.action === 'supplement') return text.value.length > 0
  if (state.action === 'reschedule') return laterThan(state.until, props.today)
  return state.reason !== ''
})

async function submit() {
  if (!ready.value || busy.value) return
  busy.value = true
  try {
    if (state.action === 'supplement') await myAssistant.supplementFollowup(state.id, text.value)
    else if (state.action === 'reschedule') await myAssistant.rescheduleFollowup(state.id, state.until, text.value || undefined)
    else await myAssistant.closeFollowup(state.id, state.reason as CloseReason, text.value || undefined)
    ElMessage.success(t('assistant.actions.' + state.action + '.done'))
    visible.value = false
    emit('done')
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    busy.value = false
  }
}
defineExpose({ open })
</script>
<template>
  <el-dialog
    v-model="visible"
    :title="t('assistant.actions.' + state.action + '.title', { name: state.id })"
    width="min(520px, calc(100vw - 32px))"
    append-to-body
    data-test="followup-action-dialog"
  >
    <p
      v-if="state.title"
      class="fa-subject"
    >
      {{ state.title }}
    </p>
    <p class="fa-hint">
      {{ t('assistant.actions.' + state.action + '.hint') }}
    </p>
    <template v-if="state.action === 'reschedule'">
      <div class="fa-quick">
        <el-button
          v-for="q in quick"
          :key="q.key"
          size="small"
          :type="state.until === q.value ? 'primary' : 'default'"
          :data-test="'quick-' + q.key"
          @click="state.until = q.value"
        >
          {{ t('assistant.actions.reschedule.quick.' + q.key) }}
        </el-button>
        <el-date-picker
          v-model="state.until"
          type="date"
          value-format="YYYY-MM-DD"
          size="small"
          :disabled-date="disabledDate"
          :placeholder="t('assistant.actions.reschedule.pick')"
          data-test="reschedule-date"
        />
      </div>
    </template>
    <el-radio-group
      v-if="state.action === 'close'"
      v-model="state.reason"
      class="fa-reasons"
      data-test="close-reasons"
    >
      <el-radio
        v-for="reason in CLOSE_REASONS"
        :key="reason"
        :value="reason"
        :data-test="'reason-' + reason"
      >
        <span class="fa-reason">{{ t('assistant.actions.close.reasons.' + reason) }}</span>
        <span class="fa-reason-desc">{{ t('assistant.actions.close.descriptions.' + reason) }}</span>
      </el-radio>
    </el-radio-group>
    <el-input
      v-model="state.text"
      type="textarea"
      :rows="state.action === 'supplement' ? 5 : 3"
      :maxlength="MAX"
      show-word-limit
      :placeholder="t('assistant.actions.' + state.action + '.placeholder')"
      data-test="action-text"
    />
    <p class="fa-learn">
      {{ t('assistant.actions.learn') }}
    </p>
    <template #footer>
      <el-button @click="visible = false">
        {{ t('assistant.actions.cancel') }}
      </el-button>
      <el-button
        type="primary"
        :disabled="!ready"
        :loading="busy"
        data-test="action-submit"
        @click="submit"
      >
        {{ t('assistant.actions.' + state.action + '.submit') }}
      </el-button>
    </template>
  </el-dialog>
</template>
<style scoped>
.fa-subject { margin: 0 0 6px; font-weight: 600; word-break: break-word; }
.fa-hint { margin: 0 0 12px; color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.6; }
.fa-quick { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 12px; }
.fa-quick .el-button { margin-left: 0; }
.fa-reasons { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; margin-bottom: 12px; }
.fa-reasons :deep(.el-radio) { height: auto; align-items: flex-start; white-space: normal; margin-right: 0; }
.fa-reasons :deep(.el-radio__label) { display: flex; flex-direction: column; line-height: 1.5; }
.fa-reason-desc { color: var(--el-text-color-secondary); font-size: 12px; }
.fa-learn { margin: 8px 0 0; color: var(--el-text-color-placeholder); font-size: 12px; line-height: 1.5; }
</style>
