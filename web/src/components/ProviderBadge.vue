<script setup lang="ts">
import { providerIcon, providerLabel } from '@/utils/models'

defineProps<{ provider: string }>()
</script>
<template>
  <span class="provider-badge">
    <img
      :src="providerIcon(provider)"
      alt=""
      width="22"
      height="22"
    >
    <span>{{ providerLabel(provider) }}</span>
  </span>
</template>
<style scoped>
.provider-badge { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; font-weight: 600; }
</style>
