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
    <span
      class="ai-mark"
      role="img"
      :aria-label="t('workTasks.aiHint')"
      tabindex="0"
      data-test="ai-mark"
    >
      <!-- 大小两颗星：常见的「AI」记号，放在浅色圆底上。 -->
      <svg
        viewBox="0 0 16 16"
        aria-hidden="true"
      >
        <path d="M6.5 1.5 7.9 5.6 12 7 7.9 8.4 6.5 12.5 5.1 8.4 1 7 5.1 5.6Z" />
        <path d="M12.5 9.5 13.2 11.3 15 12 13.2 12.7 12.5 14.5 11.8 12.7 10 12 11.8 11.3Z" />
      </svg>
    </span>
  </el-tooltip></span>
  <span v-else>{{ empty ?? t('workTasks.unassigned') }}</span>
</template>

<style scoped>
.task-actor { display: inline-flex; align-items: center; gap: 5px; }
.ai-mark {
  display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0;
  width: 1.3em; height: 1.3em; border-radius: 50%; background: var(--el-color-primary-light-8);
  cursor: help; outline-offset: 1px;
}
.ai-mark svg { width: .85em; height: .85em; fill: var(--el-color-primary); }
</style>
