<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import EnvVarsEditor from '@/components/EnvVarsEditor.vue'
import SecretInput from '@/components/SecretInput.vue'
import { useBotFormContext } from '@/components/botForm/context'

const { t } = useI18n()
const { mode, form, fieldErrors, sensitiveVisible, credKeys, onEnvInvalid } = useBotFormContext()
</script>

<template>
  <h3
    v-if="sensitiveVisible"
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
      v-for="k in credKeys"
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
</style>
