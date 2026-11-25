<script setup lang="ts">
/**
 * 待选择事项：一个问题、几个选项按钮，最后固定「其他」。要写理由的选项和「其他」点了先出输入框。
 * 飞书卡片上同时有一份，哪边先回答哪边生效。
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { workTasks, type WorkTaskAsk, type WorkTaskDetail } from '@/api/workTasks'
import { errorMessage } from '@/utils/errors'

const props = defineProps<{ code: string; ask: WorkTaskAsk; onBehalf?: boolean; compact?: boolean }>()
const emit = defineEmits<{ answered: [detail: WorkTaskDetail] }>()
const { t } = useI18n()
const picked = ref('')
const text = ref('')
const busy = ref(false)
const options = computed(() => [
  ...props.ask.options,
  ...(props.ask.other ? [{ key: 'other', label: t('workTasks.other') }] : []),
])
const needsText = computed(() => props.ask.needs_text.includes(picked.value))

async function submit(option: string): Promise<void> {
  if (props.ask.needs_text.includes(option) && picked.value !== option) {
    picked.value = option
    return
  }
  busy.value = true
  try {
    const detail = await workTasks.answer(props.code, { ask_id: props.ask.id, option, text: text.value.trim() })
    picked.value = ''
    text.value = ''
    emit('answered', detail)
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div
    class="task-ask"
    :class="{ compact }"
    data-test="work-task-ask"
    @click.stop
  >
    <div class="task-ask-question">
      {{ ask.question }}
      <span
        v-if="onBehalf"
        class="muted"
      >（{{ t('workTasks.onBehalf', { name: ask.to?.name ?? '' }) }}）</span>
    </div>
    <div
      v-if="ask.why"
      class="muted"
    >
      {{ ask.why }}
    </div>
    <div class="task-ask-options">
      <el-button
        v-for="(option, index) in options"
        :key="option.key"
        size="small"
        :type="picked === option.key || (!picked && index === 0) ? 'primary' : 'default'"
        :plain="picked !== option.key"
        :loading="busy && picked === option.key"
        :data-test="`ask-option-${option.key}`"
        @click="submit(option.key)"
      >
        {{ option.label }}
      </el-button>
    </div>
    <div
      v-if="picked"
      class="task-ask-text"
    >
      <el-input
        v-model="text"
        type="textarea"
        :autosize="{ minRows: 2, maxRows: 6 }"
        maxlength="2000"
        :placeholder="needsText ? t('workTasks.reasonRequired') : t('workTasks.addNote')"
        data-test="ask-text"
      />
      <div class="task-ask-actions">
        <el-button
          size="small"
          @click="picked = ''; text = ''"
        >
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          size="small"
          type="primary"
          :disabled="needsText && !text.trim()"
          :loading="busy"
          data-test="ask-submit"
          @click="submit(picked)"
        >
          {{ t('workTasks.submitAnswer') }}
        </el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.task-ask { display: flex; flex-direction: column; gap: 6px; padding: 10px 12px; border-radius: 6px; background: var(--el-color-primary-light-9); }
.task-ask.compact { padding: 6px 8px; margin-top: 6px; }
.task-ask-question { font-weight: 600; }
.task-ask-options { display: flex; flex-wrap: wrap; gap: 6px; }
.task-ask-options .el-button + .el-button { margin-left: 0; }
.task-ask-text { display: flex; flex-direction: column; gap: 6px; }
.task-ask-actions { display: flex; justify-content: flex-end; gap: 6px; }
.muted { color: var(--el-text-color-secondary); font-size: 12px; font-weight: normal; }
</style>
