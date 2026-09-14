<script setup lang="ts">
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{ modelValue: string | null; placeholder?: string; startEditing?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: string | null] }>()
const { t } = useI18n()
const editing = ref(props.startEditing ?? false)
const original = ref(props.modelValue)
watch(() => props.modelValue, (v) => { if (!editing.value) original.value = v })

function modify() { editing.value = true; emit('update:modelValue', '') }
function cancel() { editing.value = false; emit('update:modelValue', original.value) }
</script>

<template>
  <div class="secret-input">
    <el-input
      :model-value="modelValue ?? ''"
      :disabled="!editing"
      :placeholder="placeholder"
      :type="editing ? 'password' : 'text'"
      :show-password="editing"
      @update:model-value="emit('update:modelValue', $event)"
    />
    <el-button
      v-if="!editing"
      data-test="modify"
      @click="modify"
    >
      {{ t('common.modify') }}
    </el-button>
    <el-button
      v-else
      data-test="cancel"
      @click="cancel"
    >
      {{ t('common.cancel') }}
    </el-button>
  </div>
</template>

<style scoped>
.secret-input { display: flex; align-items: center; gap: 8px; width: 100%; min-width: 0; }
.secret-input .el-input { flex: 1; min-width: 0; }
</style>
