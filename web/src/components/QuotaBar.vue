<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Pct } from '@/api/types'
import { formatDateTime, pctColor, pctNum, resetCountdown } from '@/utils/format'

/** 一个限额窗口的用量进度条和重置倒计时；没采到用量时由调用方决定不渲染。inline：倒计时和进度条同一行，否则另起一行。 */
const props = defineProps<{ pct: Pct; resetsAt: string | null; label?: string; inline?: boolean }>()

const { t } = useI18n()

// 倒计时按分钟显示，半分钟刷新一次足够。
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => { timer = setInterval(() => { now.value = Date.now() }, 30000) })
onUnmounted(() => clearInterval(timer))

const value = computed(() => pctNum(props.pct) ?? 0)
const reset = computed(() => {
  const left = resetCountdown(props.resetsAt, now.value)
  if (left === null) return ''
  if (left === 'done') return t('relays.resetDone')
  // 只取最大的两个单位，为 0 的不写：「4 天」而不是「4 天 0 小时」。
  const parts: [string, number][] = left.d ? [['durationDays', left.d], ['durationHours', left.h]]
    : left.h ? [['durationHours', left.h], ['durationMinutes', left.m]] : [['durationMinutes', left.m]]
  const time = parts.filter(([, n], i) => i === 0 || n).map(([key, n]) => t(`relays.${key}`, { n })).join(' ')
  return t('relays.resetIn', { time })
})
</script>

<template>
  <div
    class="quota-bar"
    :class="{ labeled: !!label, inline }"
    data-test="quota-bar"
  >
    <span
      v-if="label"
      class="quota-label"
    >{{ label }}</span>
    <el-progress
      class="quota-track"
      :percentage="Math.min(100, Math.max(0, value))"
      :color="pctColor(pct)"
      :show-text="false"
      :stroke-width="6"
    />
    <span class="quota-pct">{{ value }}%</span>
    <small
      v-if="reset"
      class="quota-reset"
      :title="formatDateTime(resetsAt)"
    >{{ reset }}</small>
  </div>
</template>

<style scoped>
.quota-bar { display: flex; flex-wrap: wrap; align-items: center; column-gap: 6px; font-size: 12px; line-height: 1.7; }
.quota-label { min-width: 1.8em; color: var(--el-text-color-secondary); }
.quota-track { flex: 0 0 64px; }
.quota-pct { min-width: 2.8em; font-variant-numeric: tabular-nums; }
.quota-reset { flex-basis: 100%; color: var(--el-text-color-secondary); font-size: 12px; white-space: nowrap; }
.labeled:not(.inline) .quota-reset { padding-left: calc(1.8em + 6px); }
.inline .quota-reset { flex-basis: auto; }
</style>
