<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import LoadState from '@/components/LoadState.vue'
import { useUnsavedChanges } from '@/composables/useUnsavedChanges'
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { settings } from '@/api/admin'
import AlertSettings from '@/components/AlertSettings.vue'
import NotificationSettings from '@/components/NotificationSettings.vue'
import { PROMPT_SEGMENTS, type EffortLevel, type SettingsOut, type SettingsPatch } from '@/api/types'

const { t } = useI18n()

const EFFORT_OPTIONS: EffortLevel[] = ['low', 'medium', 'high', 'xhigh']
const VERBOSITY_OPTIONS = [1, 2, 3, 4]
// 可写设置项：coreman/core/settings_schema.py::SETTING_DEFAULTS 里除只读的 default_model 以外的键。
// save() 只按这几个键做浅比较，default_model 永远不会进 PUT 请求体。
const KEYS = [
  'bootstrap_admin_enabled',
  'default_verbosity_level',
  'default_effort_level',
  'session_ttl_hours',
  'jwt_issuer',
  'max_concurrent_tasks',
  'fast_lane_slots',
  'card_icon_url',
  'wecom_qr_provisioning_enabled',
  'prompt_security_policy',
  'prompt_codex_contract',
  'prompt_runtime_mode',
  'prompt_cron_mode',
  'prompt_runtime_tail',
  'prompt_verbosity_2',
  'prompt_verbosity_3',
  'prompt_verbosity_4',
] as const

function empty(): SettingsOut {
  return {
    bootstrap_admin_enabled: true,
    default_model: null,
    default_verbosity_level: 1,
    default_effort_level: null,
    session_ttl_hours: 72,
    jwt_issuer: 'coreman',
    max_concurrent_tasks: 30,
    fast_lane_slots: 2,
    card_icon_url: '',
    wecom_qr_provisioning_enabled: true,
    prompt_security_policy: '',
    prompt_codex_contract: '',
    prompt_runtime_mode: '',
    prompt_cron_mode: '',
    prompt_runtime_tail: '',
    prompt_verbosity_2: '',
    prompt_verbosity_3: '',
    prompt_verbosity_4: '',
  }
}

const form = reactive<SettingsOut>(empty())
/** 上一次从后端拿到的值：save() 靠它算出「改了哪些键」，PUT 只提交这些键。 */
let original: SettingsOut = empty()
const initialized = ref(false), loadError = ref('')
useUnsavedChanges(() => initialized.value && Object.keys(changes()).length > 0)

const loading = ref(false)
const saving = ref(false)

function fail(e: unknown): void {
  // 422（设置项不能置空 / 关引导登录前先配登录应用）都是后端写好的中文，直接透传。
  ElMessage.error(errorMessage(e))
}

function apply(data: SettingsOut): void {
  Object.assign(form, data)
  original = { ...data }
  initialized.value = true
}

type NumberKey = 'session_ttl_hours' | 'max_concurrent_tasks' | 'fast_lane_slots'

/** el-input-number 清空时发 undefined；这些键后端不接受 null，保持原值不动。 */
function setNumber(key: NumberKey, v: number | null | undefined): void {
  if (v !== null && v !== undefined) form[key] = v
}

function changes(): SettingsPatch {
  const out: Record<string, unknown> = {}
  for (const k of KEYS) if (form[k] !== original[k]) out[k] = form[k]
  return out as SettingsPatch
}

async function save(section: 'all' | 'runtime' | 'prompts' = 'all'): Promise<void> {
  if (saving.value) return
  const pending = changes()
  const body = Object.fromEntries(Object.entries(pending).filter(([key]) => section === 'all' || (section === 'prompts' ? key.startsWith('prompt_') : !key.startsWith('prompt_')))) as SettingsPatch
  if (!Object.keys(body).length) {
    ElMessage.info(t('settings.nothingChanged'))
    return
  }
  saving.value = true
  try {
    const remaining = Object.fromEntries(Object.entries(pending).filter(([key]) => !(key in body)))
    apply(await settings.update(body))
    Object.assign(form, remaining)
    ElMessage.success(t('settings.saved'))
  } catch (e) {
    fail(e)
  } finally {
    saving.value = false
  }
}

async function load() {
  loading.value = true; loadError.value = ''
  try {
    apply(await settings.get())
  } catch (e) {
    loadError.value = errorMessage(e)
    fail(e)
  } finally {
    loading.value = false
  }
}
onMounted(load)

defineExpose({ form })
</script>

<template>
  <div class="settings-view">
    <div class="page-header cm-page-header">
      <h2>{{ t('settings.title') }}</h2>
      <p class="cm-page-intro">
        {{ t('workspace.intro.settings') }}
      </p>
    </div>

    <nav
      class="cm-section-nav"
      :aria-label="t('workspace.sections')"
    >
      <a href="#settings-runtime">{{ t('workspace.configuration') }}</a><a href="#settings-prompts">{{ t('workspace.prompts') }}</a><a href="#settings-notifications">{{ t('workspace.notifications') }}</a>
    </nav>
    <h3
      id="settings-runtime"
      class="cm-section-title"
    >
      {{ t('workspace.configuration') }}
    </h3>
    <LoadState
      :error="loadError"
      @retry="load"
    />
    <el-form
      v-loading="loading"
      :disabled="!initialized"
      :model="form"
      label-width="220px"
      class="settings-form"
      @submit.prevent
    >
      <el-form-item
        :label="t('settings.bootstrapEnabled')"
        data-test="bootstrap"
      >
        <el-switch v-model="form.bootstrap_admin_enabled" />
      </el-form-item>
      <el-form-item v-if="!form.bootstrap_admin_enabled">
        <el-alert
          type="warning"
          :closable="false"
          show-icon
          data-test="bootstrap-hint"
        >
          {{ t('settings.bootstrapHint') }}
        </el-alert>
      </el-form-item>

      <el-form-item
        :label="t('settings.defaultModel')"
        data-test="default-model"
      >
        <!-- 只读：默认模型由模型目录派生，在「运行时管理 → 模型目录」里改。 -->
        <code v-if="form.default_model">
          {{ form.default_model }}
        </code>
        <span
          v-else
          class="muted"
        >
          {{ t('settings.defaultModelNone') }}
        </span>
        <div class="hint">
          {{ t('settings.defaultModelHint') }}
        </div>
      </el-form-item>

      <el-form-item
        :label="t('settings.defaultVerbosity')"
        data-test="default-verbosity"
      >
        <el-select
          v-model="form.default_verbosity_level"
          style="width: 200px"
        >
          <el-option
            v-for="lv in VERBOSITY_OPTIONS"
            :key="lv"
            :label="t(`bots.verbosityLevels.${lv}`)"
            :value="lv"
          />
        </el-select>
      </el-form-item>

      <el-form-item
        :label="t('settings.defaultEffort')"
        data-test="default-effort"
      >
        <el-select
          :model-value="form.default_effort_level"
          clearable
          :placeholder="t('settings.effortNone')"
          style="width: 200px"
          @update:model-value="form.default_effort_level = ($event as EffortLevel) ?? null"
        >
          <el-option
            v-for="lv in EFFORT_OPTIONS"
            :key="lv"
            :label="lv"
            :value="lv"
          />
        </el-select>
      </el-form-item>

      <el-form-item
        :label="t('settings.sessionTtl')"
        data-test="session-ttl"
      >
        <el-input-number
          :model-value="form.session_ttl_hours"
          :min="1"
          :max="720"
          @update:model-value="setNumber('session_ttl_hours', $event)"
        />
      </el-form-item>

      <el-form-item
        :label="t('settings.jwtIssuer')"
        data-test="jwt-issuer"
      >
        <el-input
          v-model="form.jwt_issuer"
          style="width: 320px"
        />
      </el-form-item>

      <el-form-item
        :label="t('settings.maxConcurrent')"
        data-test="max-concurrent"
      >
        <el-input-number
          :model-value="form.max_concurrent_tasks"
          :min="1"
          :max="500"
          @update:model-value="setNumber('max_concurrent_tasks', $event)"
        />
      </el-form-item>

      <el-form-item
        :label="t('settings.fastLaneSlots')"
        data-test="fast-lane-slots"
      >
        <el-input-number
          :model-value="form.fast_lane_slots"
          :min="0"
          :max="50"
          @update:model-value="setNumber('fast_lane_slots', $event)"
        />
      </el-form-item>

      <el-form-item
        :label="t('settings.cardIconUrl')"
        data-test="card-icon-url"
      >
        <el-input
          v-model="form.card_icon_url"
          placeholder="https://"
          clearable
          style="width: 320px"
        />
        <div class="hint">
          {{ t('settings.cardIconHint') }}
        </div>
      </el-form-item>

      <el-form-item
        :label="t('settings.wecomQr')"
        data-test="wecom-qr"
      >
        <el-switch v-model="form.wecom_qr_provisioning_enabled" />
        <div class="hint">
          {{ t('settings.wecomQrHint') }}
        </div>
      </el-form-item>

      <el-form-item class="settings-actions">
        <el-button
          :loading="saving"
          data-test="save-runtime"
          @click="save('runtime')"
        >
          {{ t('common.save') }} · {{ t('workspace.configuration') }}
        </el-button>
      </el-form-item>
      <el-collapse
        id="settings-prompts"
        class="prompts"
      >
        <el-collapse-item
          :title="t('settings.prompts')"
          name="prompts"
        >
          <el-alert
            type="info"
            :closable="false"
            show-icon
            class="prompt-hint"
          >
            {{ t('settings.promptHint') }}
          </el-alert>
          <el-form-item
            v-for="seg in PROMPT_SEGMENTS"
            :key="seg"
            :label="t(`settings.prompt.${seg}`)"
            :data-test="'prompt-' + seg"
          >
            <el-input
              v-model="form[`prompt_${seg}`]"
              type="textarea"
              :rows="8"
              :maxlength="20000"
              show-word-limit
            />
          </el-form-item>
          <el-form-item class="settings-actions">
            <el-button
              :loading="saving"
              data-test="save-prompts"
              @click="save('prompts')"
            >
              {{ t('common.save') }} · {{ t('workspace.prompts') }}
            </el-button>
          </el-form-item>
        </el-collapse-item>
      </el-collapse>

      <el-form-item>
        <el-button
          type="primary"
          :loading="saving"
          data-test="save"
          @click="save('all')"
        >
          {{ t('common.save') }}
        </el-button>
      </el-form-item>
    </el-form>
    <h3
      id="settings-notifications"
      class="cm-section-title"
    >
      {{ t('workspace.notifications') }}
    </h3>
    <NotificationSettings />
    <AlertSettings />
  </div>
</template>

<style scoped>
.settings-form { max-width: 720px; }
.prompts { margin-bottom: 18px; }
.prompt-hint { margin-bottom: 12px; }
/* el-form-item__content 是 flex，width: 100% 让提示文案整行落到输入框下面。 */
.hint { width: 100%; color: var(--el-text-color-secondary); font-size: 12px; }
.muted { color: var(--el-text-color-secondary); }
</style>
