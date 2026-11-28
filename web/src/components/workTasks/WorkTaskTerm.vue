<script setup lang="ts">
/**
 * 任务页上的一个名词（执行人、代表人、调度员……）：下面一条虚线，指上去（手机上点一下）出一张
 * 小卡片，讲它是什么意思。默认显示名词本身；包住别的东西（比如一个标签）时用 `bare` 去掉虚线。
 * 卡片挂在 body 上，样式在 styles/theme.css 的 `.cm-term-tip`。
 */
import { useI18n } from 'vue-i18n'

defineProps<{ term: string; bare?: boolean }>()
const { t } = useI18n()
</script>

<template>
  <el-tooltip
    effect="light"
    placement="bottom-start"
    popper-class="cm-term-tip"
    :show-after="150"
    :offset="8"
  >
    <template #content>
      <div class="cm-term-name">
        {{ t(`workTasks.terms.${term}.name`) }}
      </div>
      <p class="cm-term-body">
        {{ t(`workTasks.terms.${term}.body`) }}
      </p>
    </template>
    <span
      class="term"
      :class="{ bare }"
      tabindex="0"
      :data-term="term"
      data-test="task-term"
    ><slot>{{ t(`workTasks.terms.${term}.name`) }}</slot></span>
  </el-tooltip>
</template>

<style scoped>
.term { cursor: help; border-radius: 3px; }
.term:not(.bare) {
  text-decoration: underline dotted var(--el-text-color-placeholder);
  text-decoration-thickness: 1px; text-underline-offset: 4px;
}
.term:not(.bare):hover, .term:not(.bare):focus-visible { color: var(--el-color-primary); text-decoration-color: currentColor; }
</style>
