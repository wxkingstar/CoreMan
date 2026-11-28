<script setup lang="ts">
/** 任务里的一个人或 AI 员工：AI 员工名字后面跟一个小图标，指上去（手机上点一下）提示这是 AI。 */
import { useI18n } from 'vue-i18n'

defineProps<{ actor: { type: string; name: string } | null; empty?: string }>()
const { t } = useI18n()
</script>

<template>
  <span
    v-if="actor"
    class="task-actor"
  >{{ actor.name }}<el-tooltip
    v-if="actor.type === 'bot'"
    :content="t('workTasks.aiHint')"
    placement="top"
  >
    <svg
      class="ai-mark"
      viewBox="0 0 16 16"
      role="img"
      :aria-label="t('workTasks.aiHint')"
      tabindex="0"
      data-test="ai-mark"
    >
      <!-- 机器人头：天线、圆角方脸、两只眼睛、嘴、两侧耳朵。 -->
      <circle
        class="solid"
        cx="8"
        cy="1.7"
        r="1"
      />
      <path d="M8 2.6v1.9M1.4 7.6v2.4M14.6 7.6v2.4M6.2 10.9h3.6" />
      <rect
        x="3"
        y="4.5"
        width="10"
        height="8.7"
        rx="2.4"
      />
      <circle
        class="solid"
        cx="6"
        cy="8.2"
        r="1.1"
      />
      <circle
        class="solid"
        cx="10"
        cy="8.2"
        r="1.1"
      />
    </svg>
  </el-tooltip></span>
  <span v-else>{{ empty ?? t('workTasks.unassigned') }}</span>
</template>

<style scoped>
.task-actor { display: inline-flex; align-items: center; gap: 4px; }
.ai-mark {
  width: 1.15em; height: 1.15em; flex-shrink: 0; cursor: help; outline-offset: 1px;
  fill: none; stroke: var(--el-color-primary); stroke-width: 1.4; stroke-linecap: round; stroke-linejoin: round;
}
.ai-mark .solid { fill: var(--el-color-primary); stroke: none; }
</style>
