<script setup lang="ts">
import { errorMessage, fieldErrorMap, isVersionConflict } from '@/utils/errors'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { computed, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { platformApps } from '@/api/admin'
import type { PlatformAppIn, PlatformAppOut } from '@/api/types'
import SecretInput from '@/components/SecretInput.vue'
import { toPlatformAppBody } from '@/utils/platformApps'

/** 保存成功、或遇到版本冲突关窗后，通知页面重新加载列表。 */
const emit = defineEmits<{ saved: [] }>()
const { t } = useI18n()

const CAPABILITIES = ['contact_sync', 'login', 'notify', 'callback'] as const

function emptyForm(): PlatformAppIn {
  return {
    platform: 'wecom',
    name: '',
    capabilities: [],
    corp_id: null,
    app_id: null,
    secret: '',
    callback_token: null,
    callback_aes_key: null,
    extra: {},
    enabled: true,
  }
}

const dialogVisible = ref(false)
const dialogEditingId = ref<string | null>(null)
const dialogEditingVersion = ref(0)
const formRef = ref<FormInstance>()
const form = reactive<PlatformAppIn>(emptyForm())
const extraText = ref('{}')

const formRules = computed<FormRules>(() => ({
  corp_id: [
    {
      validator: (_rule, value: string | null, callback: (err?: Error) => void) => {
        if (form.platform === 'wecom' && !value) callback(new Error(t('login.required', { field: t('apps.corpId') })))
        else callback()
      },
      trigger: 'blur',
    },
  ],
  secret: [{ required: true, message: t('login.required', { field: t('apps.secret') }), trigger: 'blur' }],
}))

const callbackUrl = ref<string | null>(null)
/** 后端 422 明细回填到表单项；每次打开或提交时清空。 */
const serverErrors = reactive<Record<string, string>>({})
const fieldLabels = computed<Record<string, string>>(() => ({
  platform: t('apps.platform'), name: t('apps.name'), capabilities: t('apps.capabilities'), corp_id: t('apps.corpId'),
  app_id: t('apps.appId'), secret: t('apps.secret'), callback_token: t('apps.callbackToken'),
  callback_aes_key: t('apps.callbackAesKey'), extra: t('apps.extra'), enabled: t('common.enabled'),
}))
function resetServerErrors() {
  for (const key of Object.keys(serverErrors)) delete serverErrors[key]
}

/** 打开新建（app 为空）或编辑对话框。 */
function open(app: PlatformAppOut | null) {
  resetServerErrors()
  if (app) {
    callbackUrl.value = app.callback_url ?? null
    dialogEditingId.value = app.id
    dialogEditingVersion.value = app.version
    Object.assign(form, toPlatformAppBody(app))
    extraText.value = JSON.stringify(app.extra ?? {}, null, 2)
  } else {
    callbackUrl.value = null
    dialogEditingId.value = null
    Object.assign(form, emptyForm())
    extraText.value = '{}'
  }
  dialogVisible.value = true
}

const saving = ref(false)

/** 双击保存：第二次会撞「记录已存在」409 并弹出误导性的错误。 */
async function submitForm() {
  if (saving.value) return
  saving.value = true
  try {
    await saveApp()
  } finally {
    saving.value = false
  }
}

async function saveApp() {
  resetServerErrors()
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  let extra: Record<string, unknown>
  try {
    extra = JSON.parse(extraText.value || '{}')
  } catch {
    ElMessage.error(t('apps.extraInvalid'))
    return
  }
  const body: PlatformAppIn = { ...form, extra }

  try {
    if (dialogEditingId.value) await platformApps.update(dialogEditingId.value, body, dialogEditingVersion.value)
    else await platformApps.create(body)
    ElMessage.success(t('common.saved'))
    dialogVisible.value = false
    emit('saved')
  } catch (e) {
    // 只有版本冲突才关窗重载；「记录已存在」这类 409 保留表单，给后端原话。
    if (isVersionConflict(e)) {
      ElMessage.warning(t('common.conflict'))
      dialogVisible.value = false
      emit('saved')
    } else {
      Object.assign(serverErrors, fieldErrorMap(e, fieldLabels.value))
      ElMessage.error(errorMessage(e, fieldLabels.value))
    }
  }
}

defineExpose({ open, submitForm, form })
</script>

<template>
  <el-dialog
    v-model="dialogVisible"
    :close-on-click-modal="false"
    :title="dialogEditingId ? t('common.edit') : t('common.create')"
    width="640px"
    destroy-on-close
  >
    <el-form
      ref="formRef"
      :model="form"
      :rules="formRules"
      label-width="120px"
    >
      <el-form-item
        :label="t('apps.platform')"
        :error="serverErrors.platform"
      >
        <el-radio-group
          v-model="form.platform"
          :disabled="!!dialogEditingId"
        >
          <el-radio label="wecom">
            {{ t('platforms.wecom') }}
          </el-radio>
          <el-radio label="feishu">
            {{ t('platforms.feishu') }}
          </el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item
        :label="t('apps.name')"
        :error="serverErrors.name"
      >
        <el-input v-model="form.name" />
      </el-form-item>
      <el-form-item
        :label="t('apps.capabilities')"
        :error="serverErrors.capabilities"
      >
        <el-checkbox-group v-model="form.capabilities">
          <el-checkbox
            v-for="c in CAPABILITIES"
            :key="c"
            :label="c"
          >
            {{ t('apps.caps.' + c) }}
          </el-checkbox>
        </el-checkbox-group>
      </el-form-item>
      <el-form-item
        :label="t('apps.corpId')"
        prop="corp_id"
        :error="serverErrors.corp_id"
      >
        <el-input
          :model-value="form.corp_id ?? ''"
          @update:model-value="form.corp_id = ($event as string) || null"
        />
      </el-form-item>
      <el-form-item
        :label="t('apps.appId')"
        :error="serverErrors.app_id"
      >
        <el-input
          :model-value="form.app_id ?? ''"
          @update:model-value="form.app_id = ($event as string) || null"
        />
      </el-form-item>
      <el-form-item
        :label="t('apps.secret')"
        prop="secret"
        :error="serverErrors.secret"
      >
        <SecretInput
          :model-value="form.secret"
          :start-editing="!dialogEditingId"
          :placeholder="t('apps.secret')"
          @update:model-value="form.secret = $event ?? ''"
        />
      </el-form-item>
      <el-form-item
        v-if="callbackUrl"
        :label="t('apps.callbackUrl')"
      >
        <el-input
          :model-value="callbackUrl"
          readonly
        />
        <small>{{ t('apps.callbackUrlHint') }}</small>
      </el-form-item>
      <el-form-item
        :label="t('apps.callbackToken')"
        :error="serverErrors.callback_token"
      >
        <SecretInput
          v-model="form.callback_token"
          :placeholder="t('apps.callbackToken')"
        />
      </el-form-item>
      <el-form-item
        :label="t('apps.callbackAesKey')"
        :error="serverErrors.callback_aes_key"
      >
        <SecretInput
          v-model="form.callback_aes_key"
          :placeholder="t('apps.callbackAesKey')"
        />
      </el-form-item>
      <el-form-item
        :label="t('apps.extra')"
        :error="serverErrors.extra"
      >
        <el-input
          v-model="extraText"
          type="textarea"
          :rows="4"
        />
      </el-form-item>
      <el-form-item :label="t('common.enabled')">
        <el-switch v-model="form.enabled" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">
        {{ t('common.cancel') }}
      </el-button>
      <el-button
        type="primary"
        data-test="save-app"
        :loading="saving"
        @click="submitForm"
      >
        {{ t('common.save') }}
      </el-button>
    </template>
  </el-dialog>
</template>
