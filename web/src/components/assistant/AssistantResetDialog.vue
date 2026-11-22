<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { myAssistant, type AssistantReset } from '@/api/myAssistant'
import { errorMessage } from '@/utils/errors'

/** 「从现在开始」前先讲清楚清掉什么、留下什么；底部另给「彻底清空」：删掉助理与全部数据，像从没开过。 */
const props = defineProps<{ name: string }>()
const emit = defineEmits<{ started: [done: AssistantReset], purged: [] }>()
const { t } = useI18n()
const CLEARS = ['followups', 'jobs', 'approvals', 'inbox', 'reports', 'gaps'] as const
const KEEPS = ['history', 'notes', 'settings', 'ended', 'feishu'] as const
const visible = ref(false), busy = ref(false)

function open() { visible.value = true }
async function startFresh() {
  if (busy.value) return
  busy.value = true
  try {
    const done = await myAssistant.reset()
    visible.value = false
    emit('started', done)
  } catch (e) { ElMessage.error(errorMessage(e)) }
  finally { busy.value = false }
}
/** 彻底清空不可撤销：要原样输入助理的名字才发请求；取消输入框不算失败。 */
async function purge() {
  if (busy.value) return
  try {
    await ElMessageBox.prompt(t('assistant.page.purgePrompt', { name: props.name }), t('assistant.page.purge'), {
      type: 'error',
      confirmButtonText: t('assistant.page.purge'),
      inputValidator: (input: string) => (input ?? '').trim() === props.name || t('assistant.page.purgeMismatch'),
    })
  } catch { return }
  busy.value = true
  try {
    await myAssistant.purge()
    visible.value = false
    ElMessage.success(t('assistant.page.purgeStarted'))
    emit('purged')
  } catch (e) { ElMessage.error(errorMessage(e)) }
  finally { busy.value = false }
}
defineExpose({ open })
</script>
<template>
  <el-dialog
    v-model="visible"
    :title="t('assistant.page.resetTitle')"
    width="min(600px, calc(100vw - 32px))"
    append-to-body
    data-test="reset-dialog"
  >
    <h4 class="rd-heading">
      {{ t('assistant.page.resetClears') }}
    </h4>
    <ul
      class="rd-list"
      data-test="reset-clears"
    >
      <li
        v-for="item in CLEARS"
        :key="item"
      >
        {{ t('assistant.page.resetClearItems.' + item) }}
      </li>
    </ul>
    <h4 class="rd-heading">
      {{ t('assistant.page.resetKeeps') }}
    </h4>
    <ul
      class="rd-list"
      data-test="reset-keeps"
    >
      <li
        v-for="item in KEEPS"
        :key="item"
      >
        {{ t('assistant.page.resetKeepItems.' + item) }}
      </li>
    </ul>
    <p class="rd-note">
      {{ t('assistant.page.resetNote') }}
    </p>
    <div class="rd-purge">
      <div>
        <strong>{{ t('assistant.page.purgeTitle') }}</strong>
        <p>{{ t('assistant.page.purgeHint') }}</p>
      </div>
      <el-button
        type="danger"
        plain
        :disabled="busy"
        data-test="assistant-purge"
        @click="purge"
      >
        {{ t('assistant.page.purge') }}
      </el-button>
    </div>
    <template #footer>
      <el-button @click="visible = false">
        {{ t('assistant.page.cancel') }}
      </el-button>
      <el-button
        type="warning"
        :loading="busy"
        data-test="reset-confirm"
        @click="startFresh"
      >
        {{ t('assistant.page.reset') }}
      </el-button>
    </template>
  </el-dialog>
</template>
<style scoped>
.rd-heading { margin: 0 0 6px; font-size: 14px; }
.rd-list { margin: 0 0 14px; padding-left: 20px; line-height: 1.7; font-size: 13px; }
.rd-note { margin: 0; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
.rd-purge {
  display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between;
  margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--el-border-color-lighter);
}
.rd-purge > div { flex: 1 1 280px; }
.rd-purge p { margin: 4px 0 0; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
</style>
