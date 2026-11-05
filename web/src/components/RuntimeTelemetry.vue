<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { RelayOut } from '@/api/types'
import HealthTag from './HealthTag.vue'
import QuotaBar from './QuotaBar.vue'
import { formatDateTime } from '@/utils/format'
import { modelHealth, observedRecently, quotaWindows } from '@/utils/runtimeTelemetry'
const props = defineProps<{ backend: RelayOut }>()
const { t } = useI18n()
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => { timer = setInterval(() => { now.value = Date.now() }, 30000) })
onUnmounted(() => clearInterval(timer))
const model = computed(() => modelHealth(props.backend.model_health, now.value))
const driver = computed(() => observedRecently(props.backend.driver_health?.observed_at, now.value, 7320) ? props.backend.driver_health?.status : 'unknown')
</script>
<template>
  <div class="runtime-telemetry">
    <span>{{ t('runtimeMonitor.driver') }} <HealthTag :status="driver ?? 'unknown'" /></span>
    <span>{{ t('runtimeMonitor.model') }} <HealthTag :status="model" /></span>
    <small>{{ t('runtimeMonitor.observedAt') }} {{ formatDateTime(backend.model_health?.observed_at ?? null) }}</small>
    <small>{{ t('runtimeMonitor.requestedModel') }} {{ backend.model_health?.requested_model || '—' }} · {{ t('runtimeMonitor.actualModel') }} {{ backend.model_health?.model || '—' }}</small>
    <small v-if="backend.model_health?.detail">{{ backend.model_health.detail }}</small>
    <small v-if="backend.last_model_failure">{{ t('runtimeMonitor.lastFailure') }} {{ formatDateTime(backend.last_model_failure.observed_at) }} · {{ backend.last_model_failure.error_code || backend.last_model_failure.detail }}</small>
    <QuotaBar
      v-for="window in quotaWindows(backend, backend.model_provider, now)"
      :key="window.label"
      :label="window.label"
      :pct="window.pct"
      :resets-at="window.resetsAt"
      :state="window.state"
      :observed-at="window.observedAt"
    />
  </div>
</template>
<style scoped>
.runtime-telemetry { display: flex; flex-wrap: wrap; gap: 6px 12px; }
.runtime-telemetry small { flex-basis: 100%; color: var(--el-text-color-secondary); }
</style>
