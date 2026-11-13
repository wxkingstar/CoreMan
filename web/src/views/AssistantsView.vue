<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { assistantsAdmin, type AssistantRow } from '@/api/assistantsAdmin'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const rows = ref<AssistantRow[] | null>(null)
const error = ref(''), loading = ref(false)

async function load() {
  loading.value = true; error.value = ''
  try { rows.value = (await assistantsAdmin.list()).items }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
const ownerLabel = (row: AssistantRow) => (row.owner.login_name ? `${row.owner.display_name} (${row.owner.login_name})` : row.owner.display_name)
/** 停用确认要输入的那个名字：有登录名用登录名，没有就用显示名。 */
const confirmName = (row: AssistantRow) => row.owner.login_name ?? row.owner.display_name
/** 停用不可撤销：要原样输入所有者的登录名才发请求；取消确认框不算失败。 */
async function deprovision(row: AssistantRow) {
  const name = confirmName(row)
  try {
    await ElMessageBox.prompt(t('assistant.admin.deprovisionPrompt', { name }), t('assistant.admin.deprovision'), {
      type: 'warning',
      inputValidator: (input: string) => (input ?? '').trim() === name || t('assistant.admin.deprovisionMismatch'),
    })
    await assistantsAdmin.deprovision(row.bot_id)
    ElMessage.success(t('assistant.admin.deprovisionStarted'))
    await load()
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorMessage(e)) }
}
onMounted(load)
</script>
<template>
  <section class="assistants">
    <div class="cm-page-header">
      <h1>{{ t('menu.assistants') }}</h1>
      <p class="cm-page-intro">
        {{ t('assistant.admin.intro') }}
      </p>
      <el-button
        :loading="loading"
        @click="load"
      >
        {{ t('assistant.page.refresh') }}
      </el-button>
    </div>
    <LoadState
      :loading="loading && !rows"
      :error="error"
      @retry="load"
    />
    <el-empty
      v-if="rows && !rows.length"
      :description="t('assistant.admin.empty')"
    />
    <el-table
      v-if="rows && rows.length"
      :data="rows"
      row-key="bot_id"
    >
      <el-table-column :label="t('assistant.admin.owner')">
        <template #default="{ row }: { row: AssistantRow }">
          {{ ownerLabel(row) }}
        </template>
      </el-table-column>
      <el-table-column :label="t('assistant.admin.state')">
        <template #default="{ row }: { row: AssistantRow }">
          <el-tag :type="row.state === 'active' ? 'success' : 'info'">
            {{ t('assistant.overview.states.' + (row.state ?? 'unknown')) }}
          </el-tag>
          <div
            v-if="row.lifecycle"
            class="muted"
          >
            {{ t('assistant.admin.lifecycle') }}：{{ row.lifecycle.action }} / {{ row.lifecycle.step }}
            <span v-if="row.lifecycle.error_code">（{{ row.lifecycle.error_code }}）</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column :label="t('assistant.admin.runtime')">
        <template #default="{ row }: { row: AssistantRow }">
          <template v-if="row.node">
            {{ row.node.name }} · {{ row.node.online ? t('assistant.admin.online') : t('assistant.admin.offline') }}
          </template>
          <template v-else>
            {{ row.relay?.name ?? t('assistant.admin.none') }}
          </template>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('assistant.admin.shift')"
        width="80"
      >
        <template #default="{ row }: { row: AssistantRow }">
          {{ row.on_shift ? t('assistant.admin.yes') : t('assistant.admin.no') }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('assistant.admin.coldStart')"
        width="130"
      >
        <template #default="{ row }: { row: AssistantRow }">
          <template v-if="row.cold_start">
            {{ t('assistant.overview.coldStartStates.' + row.cold_start.state) }}
            <code v-if="row.cold_start.step">{{ row.cold_start.step }}</code>
            <code v-if="row.cold_start.outcome">{{ row.cold_start.outcome }}</code>
          </template>
          <template v-else>
            {{ t('assistant.admin.none') }}
          </template>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('assistant.admin.turns')"
        width="110"
      >
        <template #default="{ row }: { row: AssistantRow }">
          {{ row.turns ? t('assistant.overview.turnsValue', { n: row.turns.today, total: row.turns.budget }) : t('assistant.admin.none') }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('assistant.admin.inbox')"
        prop="inbox_pending"
        width="90"
      />
      <el-table-column
        :label="t('assistant.admin.jobs')"
        prop="jobs_active"
        width="130"
      />
      <el-table-column :label="t('assistant.admin.watch')">
        <template #default="{ row }: { row: AssistantRow }">
          <template v-if="row.watch">
            {{ t('assistant.overview.watchStates.' + row.watch.state) }}
            <code v-if="row.watch.last_error">{{ row.watch.last_error }}</code>
            <div
              v-if="row.watch.last_ok_at"
              class="muted"
            >
              {{ t('assistant.overview.watchLastOk', { time: formatDateTime(row.watch.last_ok_at) }) }}
            </div>
          </template>
          <template v-else>
            {{ t('assistant.admin.none') }}
          </template>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('assistant.admin.dirs')"
        prop="directories_pending_removal"
        width="100"
      />
      <el-table-column width="90">
        <template #default="{ row }: { row: AssistantRow }">
          <el-button
            v-if="row.state !== 'deprovisioned'"
            link
            type="danger"
            :data-test="'deprovision-' + row.bot_id"
            @click="deprovision(row)"
          >
            {{ t('assistant.admin.deprovision') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>
<style scoped>
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
</style>
