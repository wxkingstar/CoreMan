<script setup lang="ts">
/** 任务编号：状态图标 + 编号，点了打开那个任务；鼠标停留显示标题和状态。 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import WorkTaskStatusIcon from './WorkTaskStatusIcon.vue'

const props = defineProps<{ code: string; status?: string; title?: string; prefix?: string }>()
const { t, te } = useI18n()
const hint = computed(() => {
  const key = `workTasks.statuses.${props.status}`
  const label = props.status ? (te(key) ? t(key) : props.status) : ''
  return [props.title, label].filter(Boolean).join(' · ')
})
</script>

<template>
  <router-link
    :to="{ name: 'work-tasks', params: { code } }"
    class="task-code"
    :title="hint"
    data-test="task-code"
  >
    <WorkTaskStatusIcon
      v-if="status"
      :status="status"
    />
    <span>{{ prefix ? `${prefix} ` : '' }}{{ code }}</span>
  </router-link>
</template>

<style scoped>
.task-code { display: inline-flex; align-items: center; gap: 3px; white-space: nowrap; }
</style>
