<script setup lang="ts">
defineProps<{ text?: string | null }>()
</script>
<template>
  <el-tooltip
    v-if="text"
    placement="top-start"
    effect="light"
    :show-after="250"
    :hide-after="150"
    :enterable="true"
    popper-class="truncated-text-tooltip"
  >
    <template #content>
      <div class="truncated-text-content">
        {{ text }}
      </div>
    </template>
    <span
      class="truncated-text-preview"
      tabindex="0"
    >{{ text }}</span>
  </el-tooltip>
  <span v-else>—</span>
</template>
<style>
.el-popper.truncated-text-tooltip {
  max-width: min(480px, calc(100vw - 32px));
  padding: 14px 16px;
  border: 1px solid var(--cm-border);
  border-radius: 10px;
  background: var(--el-bg-color-overlay);
  color: var(--el-text-color-primary);
  box-shadow: 0 8px 28px rgb(0 0 0 / 12%);
}
.truncated-text-content {
  max-height: min(360px, 50vh);
  overflow-y: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 13px;
  line-height: 1.75;
  user-select: text;
}
</style>
<style scoped>
.truncated-text-preview { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: help; border-radius: 3px; }
.truncated-text-preview:focus-visible { outline: 2px solid var(--cm-brand); outline-offset: -2px; }
</style>
