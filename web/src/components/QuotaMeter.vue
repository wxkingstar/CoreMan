<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Pct } from '@/api/types'
import { formatDateTime, pctNum } from '@/utils/format'
const props = defineProps<{ value: Pct; collectedAt: string | null; resetsAt: string | null }>()
const { t } = useI18n()
const now = ref(Date.now())
const timer = setInterval(() => { now.value = Date.now() }, 60000)
onBeforeUnmount(() => clearInterval(timer))
const number = computed(() => pctNum(props.value))
const unknown = computed(() => number.value == null || !Number.isFinite(number.value) || number.value < 0 || number.value > 100 || !props.collectedAt || !Number.isFinite(Date.parse(props.collectedAt)))
// This is display freshness, not an estimate of remaining provider allowance.
const stale = computed(() => !unknown.value && (now.value - Date.parse(props.collectedAt!) > 2 * 60 * 60 * 1000 || !!props.resetsAt && Date.parse(props.resetsAt) <= now.value))
const color = computed(() => `var(--el-color-${number.value! >= 90 ? 'danger' : number.value! >= 60 ? 'warning' : 'primary'})`)
</script>
<template>
  <div class="quota-meter">
    <el-tag
      v-if="unknown"
      type="info"
    >
      {{ t('workspace.unknown') }}
    </el-tag>
    <el-tag
      v-else-if="stale"
      type="warning"
    >
      {{ t('workspace.stale') }}
    </el-tag>
    <el-progress
      v-else
      :percentage="number!"
      :color="color"
    />
    <small v-if="stale">{{ t('workspace.lastValue', { value: number }) }}</small>
    <small>{{ t('workspace.collected') }}: {{ formatDateTime(collectedAt) }}</small>
    <small v-if="resetsAt">{{ t('workspace.resets') }}: {{ formatDateTime(resetsAt) }}</small>
  </div>
</template>
<style scoped>.quota-meter small { display: block; font-size: 11px; line-height: 1.6; color: var(--cm-muted); margin-top: 4px; }</style>
