<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { departments, settings, users } from '@/api/admin'
import type { AssistantBackend, AssistantPlatformSettings, DeptNode, SettingsPatch } from '@/api/types'
import UserPicker from '@/components/UserPicker.vue'
import { errorMessage } from '@/utils/errors'

interface Form {
  mode: 'all' | 'listed'
  user_ids: string[]
  department_ids: string[]
  /** 优先用的那种 AI；`also_other` 为真时另一种排在它后面。 */
  primary: AssistantBackend
  also_other: boolean
  concurrency: number
  budget: number
}
// 与 coreman/core/settings_schema.py::SETTING_DEFAULTS 一致：后端没带这些键时用。
const DEFAULTS: AssistantPlatformSettings = {
  assistant_eligibility: { mode: 'all', department_ids: [], user_ids: [] },
  assistant_backends: ['claude', 'codex'],
  assistant_node_concurrency: 2,
  assistant_daily_turn_budget: 200,
}
// 已选的人只查这么多个名字：名单再长，剩下的在选择框里显示编号。
const NAME_LOOKUPS = 50

const { t } = useI18n()
const form = reactive<Form>({ mode: 'all', user_ids: [], department_ids: [], primary: 'claude', also_other: true, concurrency: 2, budget: 200 })
/** 上一次从后端拿到的值（按表单的形状重排过键序）：save() 靠它算出改了哪几项。 */
let original: AssistantPlatformSettings = DEFAULTS
const loaded = ref(false), saving = ref(false)
const tree = ref<DeptNode[]>([])
const knownUsers = ref<{ id: string; display_name: string; login_name: string | null }[]>([])
const other = computed<AssistantBackend>(() => (form.primary === 'claude' ? 'codex' : 'claude'))
const listedEmpty = computed(() => form.mode === 'listed' && !form.user_ids.length && !form.department_ids.length)

function current(): AssistantPlatformSettings {
  return {
    assistant_eligibility: { mode: form.mode, department_ids: [...form.department_ids], user_ids: [...form.user_ids] },
    assistant_backends: form.also_other ? [form.primary, other.value] : [form.primary],
    assistant_node_concurrency: form.concurrency,
    assistant_daily_turn_budget: form.budget,
  }
}
function apply(data: Partial<AssistantPlatformSettings>) {
  const rule = data.assistant_eligibility ?? DEFAULTS.assistant_eligibility
  const backends = data.assistant_backends?.length ? data.assistant_backends : DEFAULTS.assistant_backends
  Object.assign(form, {
    mode: rule.mode,
    user_ids: [...rule.user_ids],
    department_ids: [...rule.department_ids],
    primary: backends[0],
    also_other: backends.length > 1,
    concurrency: data.assistant_node_concurrency ?? DEFAULTS.assistant_node_concurrency,
    budget: data.assistant_daily_turn_budget ?? DEFAULTS.assistant_daily_turn_budget,
  })
  // settings 表是 JSONB，读回来的键序不保证：按表单的形状重新拼一遍，之后才能逐项比较。
  original = current()
}
function changes(): SettingsPatch {
  const next = current()
  const out: Record<string, unknown> = {}
  for (const key of Object.keys(next) as (keyof AssistantPlatformSettings)[]) {
    if (JSON.stringify(next[key]) !== JSON.stringify(original[key])) out[key] = next[key]
  }
  return out as SettingsPatch
}
async function save() {
  if (saving.value) return
  const body = changes()
  if (!Object.keys(body).length) { ElMessage.info(t('assistant.platform.nothingChanged')); return }
  saving.value = true
  try { apply(await settings.update(body)); ElMessage.success(t('assistant.platform.saved')) }
  catch (e) { ElMessage.error(errorMessage(e)) }
  finally { saving.value = false }
}
/** el-input-number 清空时发 undefined：保持原值不动。 */
function setNumber(key: 'concurrency' | 'budget', value: number | null | undefined) {
  if (value !== null && value !== undefined) form[key] = value
}
onMounted(async () => {
  try { apply(await settings.get()); loaded.value = true }
  catch (e) { ElMessage.error(errorMessage(e)); return }
  // 部门树与已选用户的名字只影响显示：取不到时表单照常可用。
  departments.tree('feishu').then((nodes) => { tree.value = nodes }).catch(() => { tree.value = [] })
  const found = await Promise.allSettled(form.user_ids.slice(0, NAME_LOOKUPS).map((id) => users.get(id)))
  knownUsers.value = found.flatMap((result) => (result.status === 'fulfilled'
    ? [{ id: result.value.id, display_name: result.value.display_name, login_name: result.value.login_name }]
    : []))
})
defineExpose({ form, save })
</script>
<template>
  <el-card class="assistant-platform-settings">
    <template #header>
      {{ t('assistant.platform.title') }}
    </template>
    <p class="hint">
      {{ t('assistant.platform.hint') }}
    </p>
    <el-form
      label-position="top"
      :disabled="!loaded"
      @submit.prevent
    >
      <el-form-item :label="t('assistant.platform.eligibility')">
        <el-radio-group v-model="form.mode">
          <el-radio value="all">
            {{ t('assistant.platform.modeAll') }}
          </el-radio>
          <el-radio value="listed">
            {{ t('assistant.platform.modeListed') }}
          </el-radio>
        </el-radio-group>
      </el-form-item>
      <template v-if="form.mode === 'listed'">
        <el-form-item :label="t('assistant.platform.users')">
          <UserPicker
            v-model="form.user_ids"
            :initial="knownUsers"
          />
        </el-form-item>
        <el-form-item :label="t('assistant.platform.departments')">
          <el-tree-select
            v-model="form.department_ids"
            :data="tree"
            :props="{ label: 'name', children: 'children' }"
            node-key="id"
            multiple
            show-checkbox
            check-strictly
            filterable
            style="width: 100%"
          />
        </el-form-item>
        <el-alert
          v-if="listedEmpty"
          type="warning"
          :closable="false"
          show-icon
          data-test="listed-empty"
          :title="t('assistant.platform.listedEmpty')"
        />
      </template>
      <el-form-item :label="t('assistant.platform.backends')">
        <span class="inline-label">{{ t('assistant.platform.backendFirst') }}</span>
        <el-radio-group v-model="form.primary">
          <el-radio value="claude">
            {{ t('assistant.platform.backendNames.claude') }}
          </el-radio>
          <el-radio value="codex">
            {{ t('assistant.platform.backendNames.codex') }}
          </el-radio>
        </el-radio-group>
        <el-checkbox
          v-model="form.also_other"
          class="also-other"
        >
          {{ t('assistant.platform.backendAlsoOther') }}
        </el-checkbox>
      </el-form-item>
      <el-form-item :label="t('assistant.platform.concurrency')">
        <el-input-number
          :model-value="form.concurrency"
          :min="1"
          :max="32"
          @update:model-value="setNumber('concurrency', $event)"
        />
        <div class="hint">
          {{ t('assistant.platform.concurrencyHint') }}
        </div>
      </el-form-item>
      <el-form-item :label="t('assistant.platform.budget')">
        <el-input-number
          :model-value="form.budget"
          :min="10"
          :max="2000"
          @update:model-value="setNumber('budget', $event)"
        />
        <div class="hint">
          {{ t('assistant.platform.budgetHint') }}
        </div>
      </el-form-item>
      <el-button
        type="primary"
        :loading="saving"
        data-test="save-assistant-platform"
        @click="save"
      >
        {{ t('assistant.platform.save') }}
      </el-button>
    </el-form>
  </el-card>
</template>
<style scoped>
.assistant-platform-settings { max-width: 720px; margin-bottom: 18px; }
.hint { width: 100%; color: var(--el-text-color-secondary); font-size: 12px; }
.inline-label { margin-right: 12px; color: var(--el-text-color-secondary); }
.also-other { margin-left: 24px; }
</style>
