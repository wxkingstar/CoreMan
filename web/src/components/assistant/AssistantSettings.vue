<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { myAssistant, type AssistantPrefs } from '@/api/myAssistant'
import AssistantAccessLog from '@/components/assistant/AssistantAccessLog.vue'
import LoadState from '@/components/LoadState.vue'
import { toForm, toPatch, validate, type PrefsField, type PrefsForm } from '@/utils/assistantSettings'
import { errorMessage } from '@/utils/errors'

const props = defineProps<{ name: string; runtime: string | null }>()
const emit = defineEmits<{ deprovisioned: [] }>()
const { t } = useI18n()

const WEEKDAYS = [1, 2, 3, 4, 5, 6, 7]
const prefs = ref<AssistantPrefs | null>(null)
const form = reactive<PrefsForm>({ timezone: '', work_start: '', work_end: '', workdays: [], focus_chats: '', ignored_chats: '', follow_platform: true, daily_turn_budget: 0 })
const errors = ref<Partial<Record<PrefsField, string>>>({})
const error = ref(''), loading = ref(false), saving = ref(false)

/** 这一项的错误提示；没有错误是空串（el-form-item 不显示）。 */
const fieldError = (field: PrefsField) => (errors.value[field] ? t('assistant.settings.errors.' + errors.value[field]) : '')
function apply(data: AssistantPrefs) {
  prefs.value = data
  Object.assign(form, toForm(data))
  errors.value = {}
}
async function load() {
  loading.value = true; error.value = ''
  try { apply(await myAssistant.settings()) }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
async function save() {
  if (!prefs.value || saving.value) return
  errors.value = validate(form, prefs.value)
  if (Object.keys(errors.value).length) return
  const patch = toPatch(form, prefs.value)
  if (!Object.keys(patch).length) { ElMessage.info(t('assistant.settings.nothingChanged')); return }
  saving.value = true
  try { apply(await myAssistant.saveSettings(patch)); ElMessage.success(t('assistant.settings.saved')) }
  catch (e) { ElMessage.error(errorMessage(e)) }
  finally { saving.value = false }
}
/** 停用不可撤销：要所有者原样输入助理的名字才发请求；取消确认框不算失败。 */
async function deprovision() {
  try {
    await ElMessageBox.prompt(t('assistant.settings.deprovisionPrompt', { name: props.name }), t('assistant.settings.danger'), {
      type: 'warning',
      inputValidator: (input: string) => (input ?? '').trim() === props.name || t('assistant.settings.deprovisionMismatch'),
    })
    await myAssistant.deprovision()
    ElMessage.success(t('assistant.settings.deprovisionStarted'))
    emit('deprovisioned')
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorMessage(e)) }
}
onMounted(load)
defineExpose({ form, save, load })
</script>
<template>
  <section class="assistant-section">
    <LoadState
      :loading="loading && !prefs"
      :error="error"
      @retry="load"
    />
    <el-form
      v-if="prefs"
      label-width="160px"
      class="assistant-settings"
      @submit.prevent
    >
      <el-form-item
        :label="t('assistant.settings.timezone')"
        :error="fieldError('timezone')"
        :show-message="false"
      >
        <el-input
          v-model="form.timezone"
          style="width: 260px"
        />
        <div class="hint">
          {{ t('assistant.settings.timezoneHint') }}
        </div>
        <!-- 错误原因自己渲染：el-form-item 的提示走过渡动画，这里要它立刻、稳定地出现。 -->
        <div
          v-if="fieldError('timezone')"
          class="field-error"
          role="alert"
          data-test="error-timezone"
        >
          {{ fieldError('timezone') }}
        </div>
      </el-form-item>
      <el-form-item
        :label="t('assistant.settings.workStart')"
        :error="fieldError('work_start')"
        :show-message="false"
      >
        <el-input
          v-model="form.work_start"
          placeholder="09:30"
          style="width: 120px"
        />
        <!-- 错误原因自己渲染：el-form-item 的提示走过渡动画，这里要它立刻、稳定地出现。 -->
        <div
          v-if="fieldError('work_start')"
          class="field-error"
          role="alert"
          data-test="error-work_start"
        >
          {{ fieldError('work_start') }}
        </div>
      </el-form-item>
      <el-form-item
        :label="t('assistant.settings.workEnd')"
        :error="fieldError('work_end')"
        :show-message="false"
      >
        <el-input
          v-model="form.work_end"
          placeholder="19:00"
          style="width: 120px"
        />
        <!-- 错误原因自己渲染：el-form-item 的提示走过渡动画，这里要它立刻、稳定地出现。 -->
        <div
          v-if="fieldError('work_end')"
          class="field-error"
          role="alert"
          data-test="error-work_end"
        >
          {{ fieldError('work_end') }}
        </div>
      </el-form-item>
      <el-form-item
        :label="t('assistant.settings.workdays')"
        :error="fieldError('workdays')"
        :show-message="false"
      >
        <el-checkbox-group v-model="form.workdays">
          <el-checkbox
            v-for="day in WEEKDAYS"
            :key="day"
            :value="day"
          >
            {{ t('assistant.settings.weekdays.d' + day) }}
          </el-checkbox>
        </el-checkbox-group>
        <!-- 错误原因自己渲染：el-form-item 的提示走过渡动画，这里要它立刻、稳定地出现。 -->
        <div
          v-if="fieldError('workdays')"
          class="field-error"
          role="alert"
          data-test="error-workdays"
        >
          {{ fieldError('workdays') }}
        </div>
      </el-form-item>
      <el-form-item
        :label="t('assistant.settings.focusChats')"
        :error="fieldError('focus_chats')"
        :show-message="false"
      >
        <el-input
          v-model="form.focus_chats"
          type="textarea"
          :rows="3"
          :disabled="prefs.locked"
        />
        <div class="hint">
          {{ t('assistant.settings.focusChatsHint', { count: prefs.limits.focus_chats }) }}
        </div>
        <!-- 错误原因自己渲染：el-form-item 的提示走过渡动画，这里要它立刻、稳定地出现。 -->
        <div
          v-if="fieldError('focus_chats')"
          class="field-error"
          role="alert"
          data-test="error-focus_chats"
        >
          {{ fieldError('focus_chats') }}
        </div>
      </el-form-item>
      <el-form-item
        :label="t('assistant.settings.ignoredChats')"
        :error="fieldError('ignored_chats')"
        :show-message="false"
      >
        <el-input
          v-model="form.ignored_chats"
          type="textarea"
          :rows="3"
          :disabled="prefs.locked"
        />
        <div class="hint">
          {{ t('assistant.settings.ignoredChatsHint', { count: prefs.limits.ignored_chats }) }}
        </div>
        <!-- 错误原因自己渲染：el-form-item 的提示走过渡动画，这里要它立刻、稳定地出现。 -->
        <div
          v-if="fieldError('ignored_chats')"
          class="field-error"
          role="alert"
          data-test="error-ignored_chats"
        >
          {{ fieldError('ignored_chats') }}
        </div>
      </el-form-item>
      <el-form-item
        :label="t('assistant.settings.budget')"
        :error="fieldError('daily_turn_budget')"
        :show-message="false"
      >
        <el-checkbox v-model="form.follow_platform">
          {{ t('assistant.settings.budgetFollow', { value: prefs.platform_daily_turn_budget }) }}
        </el-checkbox>
        <!-- el-input-number 清空时发 undefined：保持原值不动。 -->
        <el-input-number
          :model-value="form.daily_turn_budget"
          :min="prefs.limits.budget_min"
          :max="prefs.platform_daily_turn_budget"
          :disabled="form.follow_platform"
          class="budget-input"
          @update:model-value="form.daily_turn_budget = $event ?? form.daily_turn_budget"
        />
        <div class="hint">
          {{ t('assistant.settings.budgetHint', { value: prefs.platform_daily_turn_budget }) }}
        </div>
        <!-- 管理员把平台默认调到了个人值以下：个人值留着，生效的是较小的那个。 -->
        <div
          v-if="prefs.daily_turn_budget !== null && prefs.daily_turn_budget > prefs.platform_daily_turn_budget"
          class="hint"
          data-test="budget-effective"
        >
          {{ t('assistant.settings.budgetEffective', { value: prefs.effective_daily_turn_budget }) }}
        </div>
        <!-- 错误原因自己渲染：el-form-item 的提示走过渡动画，这里要它立刻、稳定地出现。 -->
        <div
          v-if="fieldError('daily_turn_budget')"
          class="field-error"
          role="alert"
          data-test="error-daily_turn_budget"
        >
          {{ fieldError('daily_turn_budget') }}
        </div>
      </el-form-item>
      <el-form-item
        :label="t('assistant.settings.runtime')"
        data-test="settings-runtime"
      >
        {{ props.runtime ?? t('assistant.settings.runtimeNone') }}
      </el-form-item>
      <el-form-item>
        <el-button
          type="primary"
          :loading="saving"
          data-test="save-settings"
          @click="save"
        >
          {{ t('assistant.settings.save') }}
        </el-button>
      </el-form-item>
    </el-form>
    <AssistantAccessLog />
    <el-alert
      type="error"
      :closable="false"
      show-icon
      class="danger-zone"
      :title="t('assistant.settings.danger')"
    >
      <p>{{ t('assistant.settings.dangerHint') }}</p>
      <el-button
        type="danger"
        plain
        data-test="deprovision"
        @click="deprovision"
      >
        {{ t('assistant.settings.deprovision') }}
      </el-button>
    </el-alert>
  </section>
</template>
<style scoped>
.assistant-settings { max-width: 760px; }
.hint { flex-basis: 100%; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
.field-error { flex-basis: 100%; color: var(--el-color-danger); font-size: 12px; line-height: 1.6; }
.budget-input { margin-left: 16px; }
.danger-zone { margin-top: 24px; max-width: 760px; }
</style>
