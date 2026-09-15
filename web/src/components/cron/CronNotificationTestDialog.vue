<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import { ElMessage } from 'element-plus'
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { cron, type CronOut, type CronRun } from '@/api/cron'
import { useDeliveryStatus } from '@/components/cron/useDeliveryStatus'

const { t } = useI18n()
const deliveryStatus = useDeliveryStatus()
const notificationTestVisible = ref(false)
const testJob = ref<CronOut | null>(null), testDeliveries = ref<CronRun['deliveries']>([])
const testErrors = ref<Record<string, string>>({})

async function refreshNotificationTests() {
  if (!testJob.value) return
  try { testDeliveries.value = await cron.notificationTests(testJob.value.id) } catch (e) { ElMessage.error(errorMessage(e)) }
}
/** 展示一次测试发送：errors 是后端当场拒绝的目标，其余投递状态点刷新再看。 */
async function show(row: CronOut, errors: Record<string, string>) {
  testJob.value = row; testErrors.value = errors; testDeliveries.value = []
  notificationTestVisible.value = true
  await refreshNotificationTests()
}
defineExpose({ show })
</script>

<template>
  <el-dialog
    v-model="notificationTestVisible"
    :title="t('notification.testResults')"
    width="min(760px, 95vw)"
  >
    <p class="hint">
      {{ t('notification.testHint') }}
    </p>
    <p
      v-for="(error, target) in testErrors"
      :key="target"
    >
      {{ target }}: {{ error }}
    </p>
    <el-button @click="refreshNotificationTests">
      {{ t('common.refresh') }}
    </el-button>
    <el-table :data="testDeliveries">
      <el-table-column :label="t('cron.channel')">
        <template #default="{ row }">
          {{ row.channel === 'webhook' ? t('notification.webhook') : row.platform }}
        </template>
      </el-table-column>
      <el-table-column :label="t('cron.deliveryStatus')">
        <template #default="{ row }">
          {{ deliveryStatus(row.status) }}
        </template>
      </el-table-column>
      <el-table-column
        prop="error"
        :label="t('cron.error')"
      />
    </el-table>
  </el-dialog>
</template>

<style scoped>
.hint { color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.6; }
</style>
