<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { EnvTextError, parseEnv, serializeEnv, validateEnvEntries, type EnvEntry } from '@/utils/dotenv'
const props = defineProps<{ modelValue: EnvEntry[] }>()
const emit = defineEmits<{ 'update:modelValue': [value: EnvEntry[]] }>()
const { t } = useI18n()
const mode = ref('pairs'), raw = ref(''), error = ref('')
const originalMasks = new Map(props.modelValue.filter(entry => entry.value.includes('•')).map(entry => [entry.key, entry.value]))
function checkMasks(values: EnvEntry[]): EnvEntry[] {
  if (values.some(entry => entry.value.includes('•') && originalMasks.get(entry.key) !== entry.value)) {
    throw new Error(t('skillEditor.modifiedMask'))
  }
  return values
}
function report(e: unknown) {
  error.value = e instanceof EnvTextError ? t(`skillEditor.envError.${e.kind}`, { line: e.line }) : e instanceof Error ? e.message : t('skillEditor.envInvalid')
}
function switchMode(next: string | number | boolean | undefined) {
  if (next === mode.value) return
  try {
    if (next === 'raw') raw.value = serializeEnv(checkMasks(props.modelValue))
    else emit('update:modelValue', checkMasks(parseEnv(raw.value)))
    mode.value = String(next); error.value = ''
  } catch (e) { report(e) }
}
function read(): EnvEntry[] {
  try {
    const values = checkMasks(mode.value === 'raw' ? parseEnv(raw.value) : validateEnvEntries(props.modelValue))
    error.value = ''; return values
  } catch (e) { report(e); throw new Error(error.value) }
}
function update(index: number, key: 'key' | 'value', value: string) {
  emit('update:modelValue', props.modelValue.map((entry, i) => i === index ? { ...entry, [key]: value } : entry))
  error.value = ''
}
defineExpose({ read })
</script>
<template>
  <section class="env-editor">
    <div class="env-editor-bar">
      <h3>{{ t('skillEditor.envValues') }}</h3>
      <el-radio-group
        :model-value="mode"
        :aria-label="t('skillEditor.editMode')"
        @change="switchMode"
      >
        <el-radio-button value="pairs">
          {{ t('skillEditor.pairs') }}
        </el-radio-button>
        <el-radio-button value="raw">
          {{ t('skillEditor.raw') }}
        </el-radio-button>
      </el-radio-group>
    </div>
    <p class="env-hint">
      {{ t(mode === 'raw' ? 'skillEditor.rawHint' : 'skillEditor.pairsHint') }}
    </p>
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      :closable="false"
      role="alert"
    />
    <template v-if="mode === 'pairs'">
      <div
        v-for="(entry, index) in modelValue"
        :key="index"
        class="env-row"
      >
        <el-input
          :model-value="entry.key"
          :aria-label="`${t('skillEditor.envKey')} ${index + 1}`"
          placeholder="API_KEY"
          @update:model-value="update(index, 'key', $event)"
        />
        <el-input
          :model-value="entry.value"
          :aria-label="`${t('skillEditor.envValue')} ${index + 1}`"
          :placeholder="t('skillEditor.envValue')"
          type="password"
          show-password
          autocomplete="new-password"
          @update:model-value="update(index, 'value', $event)"
        />
        <el-button
          :aria-label="`${t('common.delete')} ${index + 1}`"
          @click="emit('update:modelValue', modelValue.filter((_, i) => i !== index))"
        >
          {{ t('common.delete') }}
        </el-button>
      </div>
      <el-button @click="emit('update:modelValue', [...modelValue, { key: '', value: '' }])">
        {{ t('skillEditor.addVariable') }}
      </el-button>
    </template>
    <el-input
      v-else
      v-model="raw"
      class="env-raw"
      type="textarea"
      :rows="12"
      :aria-label="t('skillEditor.raw')"
      :spellcheck="false"
      autocomplete="off"
      placeholder="# Example&#10;API_URL=https://example.com&#10;API_KEY=&quot;your-value&quot;"
      @input="error = ''"
    />
  </section>
</template>
<style scoped>
.env-editor-bar { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 12px; }
.env-editor-bar h3 { margin: 0; }
.env-editor-bar :deep(.el-radio-button__inner) { outline: none; border: 1px solid var(--cm-border); }
.env-editor-bar :deep(.el-radio-button + .el-radio-button) { margin-left: -1px; }
.env-editor-bar :deep(.el-radio-button__original-radio:focus-visible + .el-radio-button__inner) { outline: 2px solid var(--el-color-primary); outline-offset: -3px; }
.env-hint { color: var(--cm-muted); font-size: 13px; line-height: 1.7; }
.env-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.5fr) auto; gap: 12px; margin-bottom: 12px; }
.env-raw :deep(textarea) { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; line-height: 1.7; tab-size: 2; }
@media (max-width: 600px) { .env-row { grid-template-columns: minmax(0, 1fr) auto; } .env-row > :first-child { grid-column: 1 / -1; } }
</style>
