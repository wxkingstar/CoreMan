<script setup lang="ts">
/** 选执行人：同事或 AI 员工（单选）。 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { bots, users } from '@/api/admin'
import type { AssigneeIn } from '@/api/workTasks'

const props = defineProps<{ modelValue: AssigneeIn | null; kind?: 'human' | 'bot'; lockKind?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: AssigneeIn | null] }>()
const { t } = useI18n()
const type = ref<'human' | 'bot'>(props.modelValue?.type ?? props.kind ?? 'human')
const options = ref<{ id: string; name: string }[]>([])
const loading = ref(false)
const id = computed({
  get: () => (props.modelValue?.type === type.value ? props.modelValue.id : ''),
  set: (value: string) => emit('update:modelValue', value ? { type: type.value, id: value } : null),
})

async function search(keyword: string): Promise<void> {
  loading.value = true
  try {
    if (type.value === 'bot') {
      const page = await bots.list({ scope: 'all', keyword, platform: 'feishu', enabled: true, per_page: 50 })
      options.value = page.items.map(b => ({ id: b.id, name: b.name }))
    } else {
      const page = await users.list({ keyword, status: 'active', per_page: 20 })
      options.value = page.items.map(u => ({ id: u.id, name: u.display_name || u.login_name || u.id }))
    }
  } catch {
    options.value = []
  } finally {
    loading.value = false
  }
}

watch(type, () => {
  emit('update:modelValue', null)
  options.value = []
  void search('')
})
void search('')
</script>

<template>
  <div class="assignee-picker">
    <el-radio-group
      v-if="!lockKind"
      v-model="type"
      size="small"
    >
      <el-radio-button value="human">
        {{ t('workTasks.person') }}
      </el-radio-button>
      <el-radio-button value="bot">
        {{ t('workTasks.aiEmployee') }}
      </el-radio-button>
    </el-radio-group>
    <el-select
      v-model="id"
      filterable
      remote
      clearable
      :remote-method="search"
      :loading="loading"
      :placeholder="type === 'bot' ? t('workTasks.pickBot') : t('workTasks.pickPerson')"
      data-test="assignee-select"
      style="width: 100%"
    >
      <el-option
        v-for="item in options"
        :key="item.id"
        :label="item.name"
        :value="item.id"
      />
    </el-select>
  </div>
</template>

<style scoped>
.assignee-picker { display: flex; flex-direction: column; gap: 8px; width: 100%; }
</style>
