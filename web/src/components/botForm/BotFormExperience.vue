<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { useBotFormContext } from '@/components/botForm/context'

defineProps<{ part?: 'extra' }>()
const { t } = useI18n()
const { mode, form, fieldErrors } = useBotFormContext()
</script>

<template>
  <h3
    v-if="!part"
    id="form-experience"
    class="cm-section-title"
  >
    <span>04</span>{{ t('workspace.experience') }}
  </h3>
  <el-form-item
    :label="t('bots.welcome')"
    data-test="welcome"
    :error="fieldErrors.welcome_message"
  >
    <el-input
      :model-value="form.welcome_message ?? ''"
      type="textarea"
      :rows="2"
      @update:model-value="form.welcome_message = ($event as string) || null"
    />
  </el-form-item>

  <el-form-item
    v-if="mode === 'create'"
    :label="t('bots.enabled')"
    data-test="enabled"
  >
    <el-switch v-model="form.enabled" />
  </el-form-item>
</template>
