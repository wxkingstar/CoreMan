<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import SkillEnvEditor from '@/components/SkillEnvEditor.vue'
import type { EnvEntry } from '@/utils/dotenv'
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { skills, type Preset } from '@/api/skills'

const emit = defineEmits<{ saved: [] }>()
const { t } = useI18n()
const busy = ref(false), presetVisible = ref(false)
const selectedPreset = ref(false)
const envEditor = ref<InstanceType<typeof SkillEnvEditor>>()
const presetForm = reactive<Preset>({ group_key: '', label: '', vars: {}, tags: [], version: 0 })
const variables = ref<{ key: string; value: string }[]>([])
const fail = (e: unknown) => ElMessage.error(errorMessage(e))

function open(row: Preset | null) { selectedPreset.value = !!row; Object.assign(presetForm, row ?? { group_key: '', label: '', vars: {}, tags: [], version: 0 }); variables.value = Object.entries(row?.vars ?? {}).map(([key, value]) => ({ key, value })); presetVisible.value = true }
async function savePreset() {
  busy.value = true
  try {
    if (!presetForm.group_key.trim() || !presetForm.label.trim()) throw new Error(t('skillEditor.presetRequired'))
    const values: EnvEntry[] = envEditor.value!.read()
    await skills.presetSave({ ...presetForm, vars: Object.fromEntries(values.map(v => [v.key, v.value])) })
    presetVisible.value = false; variables.value = []; emit('saved')
  } catch (e) { fail(e) } finally { busy.value = false }
}
defineExpose({ open, savePreset })
</script>

<template>
  <el-dialog
    v-model="presetVisible"
    :close-on-click-modal="false"
    :title="t(selectedPreset ? 'skillEditor.editPreset' : 'skillEditor.newPreset')"
    width="780px"
    destroy-on-close
    @closed="variables = []"
  >
    <el-form
      label-position="top"
      :disabled="busy"
    >
      <div class="form-grid">
        <el-form-item
          :label="t('common.name')"
          required
        >
          <el-input
            v-model="presetForm.label"
            :placeholder="t('skillEditor.presetLabelHint')"
          />
        </el-form-item>
        <el-form-item
          :label="t('infra.key')"
          required
        >
          <el-input
            v-model="presetForm.group_key"
            :disabled="presetForm.version > 0"
            placeholder="db_erp"
          />
        </el-form-item>
      </div>
      <SkillEnvEditor
        ref="envEditor"
        v-model="variables"
      />
      <p class="hint after-table">
        {{ t('skill.presetHint') }}
      </p>
    </el-form>
    <template #footer>
      <el-button @click="presetVisible = false">
        {{ t('common.cancel') }}
      </el-button><el-button
        type="primary"
        :loading="busy"
        @click="savePreset"
      >
        {{ t('common.save') }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped src="./skillForm.css"></style>
