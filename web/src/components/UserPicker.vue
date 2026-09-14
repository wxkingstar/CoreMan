<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { users } from '@/api/admin'

interface Option { id: string; display_name: string; login_name: string | null }

const props = defineProps<{ modelValue: string[]; exclude?: string[]; initial?: Option[] }>()
const emit = defineEmits<{ 'update:modelValue': [string[]] }>()

const { t } = useI18n()

const options = ref<Option[]>(props.initial ?? [])
const loading = ref(false)
/** 见过的用户（初始值 + 每次搜索结果）：搜索换掉 options 后，已选中项还得有 el-option 才显示得出名字。 */
const known = ref<Record<string, Option>>(Object.fromEntries((props.initial ?? []).map((o) => [o.id, o])))

// 已选但不在当前搜索结果里的项补到列表末尾，否则 el-select 的 tag 会退化成裸 id。
const visibleOptions = computed<Option[]>(() => {
  const out = [...options.value]
  const seen = new Set(out.map((o) => o.id))
  for (const id of props.modelValue) {
    if (!seen.has(id) && known.value[id]) out.push(known.value[id])
  }
  return out
})

// initial 常常是异步来的（对话框先开、白名单后到），晚到也要认，否则已选项的 tag 就是一串裸 id。
watch(() => props.initial, (list) => {
  for (const o of list ?? []) known.value[o.id] = o
  if (!options.value.length) options.value = [...(list ?? [])]
}, { deep: true })

function label(o: Option): string {
  return o.login_name ? `${o.display_name} (${o.login_name})` : o.display_name
}

async function search(keyword: string): Promise<void> {
  if (!keyword) return
  loading.value = true
  try {
    const page = await users.list({ keyword, per_page: 20, status: 'active' })
    const ex = new Set(props.exclude ?? [])
    const found = page.items
      .filter((u) => !ex.has(u.id))
      .map((u) => ({ id: u.id, display_name: u.display_name, login_name: u.login_name }))
    for (const o of found) known.value[o.id] = o
    options.value = found
  } finally {
    loading.value = false
  }
}

defineExpose({ search, options })
</script>

<template>
  <el-select
    :model-value="modelValue"
    multiple
    filterable
    remote
    :remote-method="search"
    :loading="loading"
    :placeholder="t('users.pickerPlaceholder')"
    class="user-picker"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-option
      v-for="o in visibleOptions"
      :key="o.id"
      :value="o.id"
      :label="label(o)"
    />
  </el-select>
</template>

<style scoped>
.user-picker { width: 100%; }
</style>
