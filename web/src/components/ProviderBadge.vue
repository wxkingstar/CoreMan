<script setup lang="ts">
defineProps<{ provider: string }>()
</script>
<template>
  <span class="provider-badge">
    <img
      :src="provider === 'claude' ? '/providers/claude.svg' : '/providers/openai.svg'"
      alt=""
      width="22"
      height="22"
    >
    <span>{{ provider === 'claude' ? 'Claude Code' : 'Codex / GPT' }}</span>
  </span>
</template>
<style scoped>
.provider-badge { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; font-weight: 600; }
</style>
