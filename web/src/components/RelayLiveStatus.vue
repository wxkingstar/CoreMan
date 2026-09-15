<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { call, http } from '@/api/client'

const props = defineProps<{ relayId: string; name: string }>()
const { t } = useI18n()
const visible = ref(false)
const loading = ref(false)
const probing = ref(false)
const tasks = ref<{ pid?: number; request_id?: string; bot_key: string; bot_name?: string | null; user: string; chat_id: string }[]>([])
const error = ref('')
async function refresh() {
  loading.value = true
  error.value = ''
  tasks.value = []
  try {
    const data = await call<{ active_tasks: typeof tasks.value }>(http.get(`/api/admin/relay-servers/${props.relayId}/live-status`))
    tasks.value = data.active_tasks
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}
async function open() {
  visible.value = true
  await refresh()
}
async function probe(operation: 'probe-rate-limits' | 'health-check') {
  probing.value = true
  try {
    await call(http.post(`/api/admin/relay-servers/${props.relayId}/agent-probe`, { operation }))
    ElMessage.success(t('relayAgent.probeQueued'))
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    probing.value = false
  }
}
</script>

<template>
  <el-button
    size="small"
    @click="open"
  >
    {{ t('relayAgent.liveStatus') }}
  </el-button>
  <el-dialog
    v-model="visible"
    :title="`${name} · ${t('relayAgent.liveStatus')}`"
    width="min(800px, 95vw)"
    destroy-on-close
  >
    <el-space wrap>
      <el-button
        :loading="loading"
        @click="refresh"
      >
        {{ t('relayAgent.refresh') }}
      </el-button>
      <el-button
        :loading="probing"
        @click="probe('probe-rate-limits')"
      >
        {{ t('relayAgent.quotaProbe') }}
      </el-button>
      <el-button
        :loading="probing"
        @click="probe('health-check')"
      >
        {{ t('relayAgent.healthCheck') }}
      </el-button>
    </el-space>
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      :closable="false"
      show-icon
    />
    <el-table
      v-else
      v-loading="loading"
      :data="tasks"
      :empty-text="t('relayAgent.noTasks')"
    >
      <el-table-column
        :label="t('relayAgent.process')"
        width="140"
        show-overflow-tooltip
      >
        <template #default="{ row }">
          {{ row.pid ?? row.request_id ?? '—' }}
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        prop="bot_name"
        :label="t('relayAgent.bot')"
      />
      <el-table-column
        min-width="140"
        prop="user"
        :label="t('relayAgent.user')"
      />
      <el-table-column
        min-width="140"
        prop="chat_id"
        :label="t('relayAgent.chat')"
        show-overflow-tooltip
      />
    </el-table>
  </el-dialog>
</template>
