<script setup lang="ts">
import { computed } from 'vue'
import { secondsParts } from '@/utils/format'

// 秒数（单位写在列头上）：最多两位小数，小数部分小一号、颜色淡一些，不抢整数的视线；空值显示 '—'。
const props = defineProps<{ value: number | null | undefined }>()
const parts = computed(() => secondsParts(props.value))
</script>

<template>
  <span
    v-if="parts"
    class="seconds"
  >{{ parts.whole }}<span
    v-if="parts.fraction"
    class="fraction"
  >{{ parts.fraction }}</span></span>
  <span v-else>—</span>
</template>

<style scoped>
.seconds { font-variant-numeric: tabular-nums; }
.fraction { font-size: 0.8em; color: var(--el-text-color-secondary); }
</style>
