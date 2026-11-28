<script setup lang="ts">
/** 任务状态小图标：进行中转圈，等待中暂停，做完打勾……放在任务编号旁边，人一眼看出在不在动。 */
import { computed, type Component } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  CircleCheckFilled, CircleCloseFilled, Clock, Hide, Loading, VideoPause, View,
} from '@element-plus/icons-vue'

const props = defineProps<{ status: string }>()
const { t, te } = useI18n()
const ICONS: Record<string, { icon: Component; tone: string }> = {
  todo: { icon: Clock, tone: 'muted' },
  in_progress: { icon: Loading, tone: 'primary' },
  waiting: { icon: VideoPause, tone: 'warning' },
  review: { icon: View, tone: 'warning' },
  done: { icon: CircleCheckFilled, tone: 'success' },
  cancelled: { icon: CircleCloseFilled, tone: 'muted' },
  shadow: { icon: Hide, tone: 'muted' },
}
const spec = computed(() => ICONS[props.status] ?? ICONS.todo)
const label = computed(() => {
  const key = `workTasks.statuses.${props.status}`
  return te(key) ? t(key) : props.status
})
</script>

<template>
  <el-icon
    :class="['task-status-icon', `tone-${spec.tone}`, { 'is-loading': status === 'in_progress' }]"
    :title="label"
    :aria-label="label"
    role="img"
    :data-status="status"
  >
    <component :is="spec.icon" />
  </el-icon>
</template>

<style scoped>
/* 比字大一号：打勾、转圈一眼看得清。 */
.task-status-icon { font-size: 1.2em; vertical-align: -0.2em; }
.tone-primary { color: var(--el-color-primary); }
.tone-warning { color: var(--el-color-warning); }
.tone-success { color: var(--el-color-success); }
.tone-muted { color: var(--el-text-color-secondary); }
</style>
