<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { HealthStatus } from '@/api/types'

const props = defineProps<{ status: HealthStatus | string | null }>()

const { t } = useI18n()

const TAG_TYPES: Record<HealthStatus, 'success' | 'danger' | 'warning' | 'info'> = {
  healthy: 'success',
  down: 'danger',
  auth_fail: 'warning',
  timeout: 'warning',
  unknown: 'info',
}

// 库里出现没见过的状态时按 unknown 渲染，别让页面掉出一个 undefined 的标签色。
const status = computed<HealthStatus>(() =>
  props.status && props.status in TAG_TYPES ? (props.status as HealthStatus) : 'unknown',
)
</script>

<template>
  <el-tag
    :type="TAG_TYPES[status]"
    size="small"
  >
    {{ t('relays.health.' + status) }}
  </el-tag>
</template>
