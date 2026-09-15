<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import LoadState from '@/components/LoadState.vue'
import CronEditorDialog from '@/components/cron/CronEditorDialog.vue'
import CronHistoryDrawer from '@/components/cron/CronHistoryDrawer.vue'
import CronNotificationTestDialog from '@/components/cron/CronNotificationTestDialog.vue'
import { useListQuery } from '@/composables/useListQuery'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { bots } from '@/api/admin'
import { cron, type CronOut } from '@/api/cron'
import type { BotOut } from '@/api/types'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const rows = ref<CronOut[]>([]), total = ref(0), page = ref(1), filter = ref('')
const botOptions = ref<BotOut[]>([])
const loading = ref(false), saving = ref(false)
const notificationTesting = ref(false)
const editor = ref<InstanceType<typeof CronEditorDialog>>()
const testDialog = ref<InstanceType<typeof CronNotificationTestDialog>>()
const historyDrawer = ref<InstanceType<typeof CronHistoryDrawer>>()
async function sendTest(row: CronOut) {
  if (notificationTesting.value) return
  notificationTesting.value = true
  try {
    const result = await cron.testNotification(row)
    await testDialog.value?.show(row, result.errors)
  } catch (e) { fail(e) } finally { notificationTesting.value = false }
}
function fail(e: unknown) { ElMessage.error(errorMessage(e)) }
const listLoading = ref(false), listError = ref('')
const { persist: persistQuery } = useListQuery({ page, bot_id: filter }, () => { void load() })
async function load() {
  persistQuery(); listLoading.value = true; listError.value = ''

  loading.value = true
  try { const data = await cron.list(page.value, filter.value || undefined); rows.value = data.items; total.value = data.total }
  catch (e) { listError.value = errorMessage(e); fail(e) } finally { listLoading.value = false; loading.value = false }
}
async function searchBots(keyword = '') {
  try { botOptions.value = (await bots.list({ scope: 'mine', keyword, per_page: 100 })).items }
  catch (e) { fail(e) }
}
onMounted(() => { void load(); void searchBots() })
function open(row?: CronOut) { void editor.value?.open(row) }
async function run(row: CronOut) {
  saving.value = true
  try { await cron.run(row); ElMessage.success(t('cron.queued')); await load() }
  catch (e) { fail(e) } finally { saving.value = false }
}
async function cancelRun(row: CronOut) {
  try { await cron.cancel(row); await load() } catch (e) { fail(e) }
}
async function disable(row: CronOut) {
  try { await cron.disable(row); await load() } catch (e) { fail(e) }
}
async function remove(row: CronOut) {
  try { await ElMessageBox.confirm(t('common.confirmDelete')); await cron.remove(row); await load() }
  catch (e) { if (e !== 'cancel' && e !== 'close') fail(e) }
}
function showHistory(row: CronOut) { historyDrawer.value?.open(row) }
</script>

<template>
  <section class="cron-page">
    <div class="toolbar cm-page-header">
      <h2>{{ t('menu.cron') }}</h2>
      <p class="cm-page-intro">
        {{ t('workspace.intro.cron') }}
      </p><el-button
        type="primary"
        data-test="create-cron"
        @click="open()"
      >
        {{ t('common.create') }}
      </el-button>
    </div>
    <p class="hint">
      {{ t('cron.identityHint') }}
    </p>
    <div class="toolbar">
      <el-select
        v-model="filter"
        clearable
        filterable
        remote
        :remote-method="searchBots"
        :placeholder="t('cron.bot')"
        @change="page = 1; load()"
      >
        <el-option
          v-for="bot in botOptions"
          :key="bot.id"
          :label="bot.name"
          :value="bot.id"
        />
      </el-select>
      <el-button @click="load">
        {{ t('common.refresh') }}
      </el-button>
    </div>
    <LoadState
      :loading="listLoading"
      :error="listError"
      @retry="load"
    />
    <el-table
      v-loading="loading"
      :data="rows"
      row-key="id"
      empty-text="—"
    >
      <el-table-column
        prop="name"
        :label="t('cron.name')"
        min-width="150"
      />
      <el-table-column
        :label="t('cron.schedule')"
        min-width="180"
      >
        <template #default="{ row }">
          <code>{{ row.cron_expression }}</code><div class="hint">
            {{ row.timezone }}
          </div>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('cron.next')"
        min-width="170"
      >
        <template #default="{ row }">
          {{ row.enabled ? formatDateTime(row.next_run_at) : t('common.disabled') }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('cron.lastStatus')"
        min-width="110"
      >
        <template #default="{ row }">
          {{ row.force_run_at ? t('cron.queued') : row.last_status ? t(`cron.status.${row.last_status}`) : '—' }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('common.actions')"
        min-width="350"
      >
        <template #default="{ row }">
          <el-button
            link
            :data-test="`history-${row.id}`"
            @click="showHistory(row)"
          >
            {{ t('cron.history') }}
          </el-button>
          <el-button
            link
            :disabled="!row.enabled || !!row.running_task_id || !!row.force_run_at || saving"
            :data-test="`run-${row.id}`"
            @click="run(row)"
          >
            {{ t('cron.run') }}
          </el-button>
          <el-button
            v-if="row.can_edit"
            link
            :disabled="notificationTesting"
            :data-test="`test-notification-${row.id}`"
            @click="sendTest(row)"
          >
            {{ t('notification.test') }}
          </el-button>
          <el-button
            v-if="row.can_edit"
            link
            :disabled="!!row.force_run_at"
            :data-test="`edit-${row.id}`"
            @click="open(row)"
          >
            {{ t('common.edit') }}
          </el-button>
          <el-button
            v-if="row.running_task_id || row.force_run_at"
            link
            type="warning"
            @click="cancelRun(row)"
          >
            {{ t('cron.cancelRun') }}
          </el-button>
          <el-button
            v-if="row.enabled"
            link
            @click="disable(row)"
          >
            {{ t('common.disable') }}
          </el-button>
          <el-button
            v-if="row.can_edit"
            link
            type="danger"
            :disabled="!!row.running_task_id"
            @click="remove(row)"
          >
            {{ t('common.delete') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination
      v-model:current-page="page"
      :page-size="50"
      :total="total"
      layout="prev, pager, next, total"
      @current-change="load"
    />
    <CronEditorDialog
      ref="editor"
      :bot-options="botOptions"
      @saved="load"
      @search-bots="searchBots"
    />
    <CronNotificationTestDialog ref="testDialog" />
    <CronHistoryDrawer ref="historyDrawer" />
  </section>
</template>
<style scoped>
.toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.hint { color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.6; }
.el-select { width: 100%; }
.toolbar .el-select { max-width: 320px; }
</style>
