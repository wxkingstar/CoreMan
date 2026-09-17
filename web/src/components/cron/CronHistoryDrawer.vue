<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import { ElMessage } from 'element-plus'
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { cron, type CronOut, type CronRun } from '@/api/cron'
import { formatDeliveryError } from '@/components/cron/deliveryError'
import { useDeliveryStatus } from '@/components/cron/useDeliveryStatus'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const deliveryStatus = useDeliveryStatus()
const historyVisible = ref(false), historyLoading = ref(false), history = ref<CronRun[]>([])
const historyJob = ref<CronOut | null>(null), historyPage = ref(1), historyTotal = ref(0)

async function loadHistory() {
  if (!historyJob.value) return
  historyLoading.value = true
  try { const data = await cron.runs(historyJob.value.id, historyPage.value); history.value = data.items; historyTotal.value = data.total }
  catch (e) { ElMessage.error(errorMessage(e)) } finally { historyLoading.value = false }
}
/** 执行记录按需加载：只有打开抽屉时才请求。 */
function open(row: CronOut) {
  historyJob.value = row; history.value = []; historyPage.value = 1; historyVisible.value = true; void loadHistory()
}
defineExpose({ open })
</script>

<template>
  <el-drawer
    v-model="historyVisible"
    :title="`${t('cron.history')} · ${historyJob?.name || ''}`"
    size="min(850px, 95vw)"
  >
    <el-button @click="loadHistory">
      {{ t('common.refresh') }}
    </el-button>
    <el-table
      v-loading="historyLoading"
      :data="history"
      row-key="id"
    >
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="run-detail">
            <b>{{ t('cron.prompt') }}</b><pre>{{ row.prompt }}</pre><b>{{ t('cron.reply') }}</b><pre>{{ row.reply || row.error_message || '—' }}</pre>
            <p>{{ t('cron.precheck') }}: {{ row.precheck_meta || '—' }}</p>
            <p v-if="row.delivery.errors && Object.keys(row.delivery.errors).length">
              <span
                v-for="(error, target) in row.delivery.errors"
                :key="target"
              >{{ formatDeliveryError(error as string, t) }} <small>({{ target }})</small><br></span>
            </p>
            <el-table :data="row.deliveries">
              <el-table-column
                min-width="140"
                prop="platform"
                :label="t('cron.channel')"
              >
                <template #default="{ row: delivery }">
                  {{ delivery.channel === 'webhook' ? t('notification.webhook') : delivery.platform === 'wecom' || delivery.platform === 'feishu' ? t(`platforms.${delivery.platform}`) : delivery.platform }}
                </template>
              </el-table-column><el-table-column
                min-width="140"
                prop="status"
                :label="t('cron.deliveryStatus')"
              >
                <template #default="{ row: delivery }">
                  {{ deliveryStatus(delivery.status) }}
                </template>
              </el-table-column><el-table-column
                min-width="140"
                prop="error"
                :label="t('cron.error')"
              >
                <template #default="{ row: delivery }">
                  {{ formatDeliveryError(delivery.error, t) }}
                </template>
              </el-table-column>
            </el-table>
          </div>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('cron.started')"
        min-width="170"
      >
        <template #default="{ row }">
          {{ formatDateTime(row.started_at) }}
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        :label="t('cron.trigger')"
      >
        <template #default="{ row }">
          {{ t(`cron.${row.trigger_kind}`) }}
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        :label="t('cron.lastStatus')"
      >
        <template #default="{ row }">
          {{ t(`cron.status.${row.status}`) }}
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        :label="t('cron.tokens')"
      >
        <template #default="{ row }">
          {{ row.input_tokens ?? '—' }} / {{ row.output_tokens ?? '—' }}
        </template>
      </el-table-column>
    </el-table>
    <el-pagination
      v-model:current-page="historyPage"
      :page-size="50"
      :total="historyTotal"
      layout="prev, pager, next, total"
      @current-change="loadHistory"
    />
  </el-drawer>
</template>

<style scoped>
.run-detail { padding: 12px 24px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.6; }
</style>
