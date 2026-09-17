<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import EnvVarsEditor from '@/components/EnvVarsEditor.vue'
import SecretInput from '@/components/SecretInput.vue'
import { useBotFormContext } from '@/components/botForm/context'

/** part：新建时整块收在「更多设置」里，不再显示分区标题。 */
defineProps<{ part?: 'extra' }>()
const { t } = useI18n()
const { mode, form, fieldErrors, sensitiveVisible, credKeys, onEnvInvalid, manualCredentials } = useBotFormContext()
</script>

<template>
  <h3
    v-if="sensitiveVisible && !part"
    id="form-security"
    class="cm-section-title"
  >
    <span>03</span>{{ t('workspace.security') }}
  </h3>
  <el-form-item
    v-if="sensitiveVisible"
    :label="t('bots.systemPrompt')"
    data-test="system_prompt"
    :error="fieldErrors.system_prompt"
  >
    <el-input
      v-model="form.system_prompt"
      type="textarea"
      :rows="4"
    />
  </el-form-item>

  <template v-if="sensitiveVisible">
    <el-form-item
      :label="t('bots.credentials')"
      class="section"
      :error="fieldErrors.credentials"
    />
    <el-form-item
      v-if="mode === 'create' && form.platform === 'feishu'"
      :label="t('feishuApp.manualCredentials')"
      data-test="manual-credentials"
    >
      <el-switch v-model="manualCredentials" />
      <div class="muted">
        {{ manualCredentials ? t('feishuApp.manualCredentialsHint') : t('feishuApp.oneClickHint') }}
      </div>
    </el-form-item>
    <el-form-item
      v-for="k in (mode === 'create' && form.platform === 'feishu' && !manualCredentials ? [] : credKeys)"
      :key="k"
      :label="t(`bots.cred.${k}`)"
    >
      <SecretInput
        :model-value="form.credentials[k] ?? ''"
        :data-test="'cred-' + k"
        :start-editing="mode === 'create'"
        :placeholder="t(`bots.cred.${k}`)"
        @update:model-value="form.credentials[k] = $event ?? ''"
      />
    </el-form-item>

    <el-form-item
      :label="t('bots.envVars')"
      data-test="env_vars"
      :error="fieldErrors.env_vars"
    >
      <EnvVarsEditor
        :model-value="form.env_vars"
        @update:model-value="form.env_vars = $event"
        @invalid="onEnvInvalid"
      />
    </el-form-item>
  </template>
</template>

<style scoped>
.section { font-weight: 600; }
.muted { width: 100%; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
</style>
