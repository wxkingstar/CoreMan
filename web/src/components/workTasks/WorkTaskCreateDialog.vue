<script setup lang="ts">
/** 新建任务：人只填标题和一句话；背景与要求给接手的 AI 员工看，越全越好。 */
import { computed, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import AssigneePicker from './AssigneePicker.vue'
import { workTasks, type AssigneeIn, type WorkTaskDetail } from '@/api/workTasks'
import { errorMessage } from '@/utils/errors'

defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; created: [detail: WorkTaskDetail] }>()
const { t } = useI18n()
const form = reactive({
  title: '', summary: '', brief: '', priority: 'normal', due_at: null as string | null,
  review_required: true,
})
const assignee = ref<AssigneeIn | null>(null)
const busy = ref(false)
const toBot = computed(() => assignee.value?.type === 'bot')

function reset(): void {
  Object.assign(form, { title: '', summary: '', brief: '', priority: 'normal', due_at: null, review_required: true })
  assignee.value = null
}

async function submit(): Promise<void> {
  busy.value = true
  try {
    const detail = await workTasks.create({
      title: form.title.trim(),
      summary: form.summary.trim(),
      brief: form.brief.trim(),
      assignee: assignee.value,
      priority: form.priority,
      due_at: form.due_at,
      // 交给 AI 员工的默认要验收；交给同事的默认不验收。
      review_required: toBot.value ? form.review_required : null,
    })
    emit('created', detail)
    emit('update:modelValue', false)
    reset()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="t('workTasks.create')"
    width="min(600px, 96vw)"
    data-test="work-task-create"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-form label-position="top">
      <el-form-item
        :label="t('workTasks.taskTitle')"
        required
      >
        <el-input
          v-model="form.title"
          maxlength="30"
          show-word-limit
          :placeholder="t('workTasks.titlePlaceholder')"
          data-test="create-title"
        />
      </el-form-item>
      <el-form-item :label="t('workTasks.oneLine')">
        <el-input
          v-model="form.summary"
          maxlength="60"
          show-word-limit
          :placeholder="t('workTasks.oneLinePlaceholder')"
        />
      </el-form-item>
      <el-form-item :label="t('workTasks.assignee')">
        <AssigneePicker v-model="assignee" />
      </el-form-item>
      <el-alert
        v-if="toBot"
        type="info"
        :closable="false"
        :title="t('workTasks.manualStartHint')"
        class="create-hint"
      />
      <el-form-item :label="t('workTasks.briefInput')">
        <el-input
          v-model="form.brief"
          type="textarea"
          :autosize="{ minRows: 4, maxRows: 12 }"
          maxlength="20000"
          :placeholder="t('workTasks.briefPlaceholder')"
        />
      </el-form-item>
      <div class="create-row">
        <el-form-item :label="t('workTasks.priority')">
          <el-select
            v-model="form.priority"
            style="width: 140px"
          >
            <el-option
              v-for="p in ['normal', 'high', 'urgent']"
              :key="p"
              :value="p"
              :label="t(`workTasks.priorities.${p}`)"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('workTasks.due')">
          <el-date-picker
            v-model="form.due_at"
            type="datetime"
            value-format="YYYY-MM-DDTHH:mm:ssZ"
          />
        </el-form-item>
        <el-form-item
          v-if="toBot"
          :label="t('workTasks.reviewRequired')"
        >
          <el-switch v-model="form.review_required" />
        </el-form-item>
      </div>
    </el-form>
    <template #footer>
      <el-button @click="emit('update:modelValue', false)">
        {{ t('common.cancel') }}
      </el-button>
      <el-button
        type="primary"
        :loading="busy"
        :disabled="!form.title.trim()"
        data-test="create-submit"
        @click="submit"
      >
        {{ t('workTasks.create') }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.create-row { display: flex; flex-wrap: wrap; gap: 16px; }
.create-hint { margin-bottom: 12px; }
</style>
