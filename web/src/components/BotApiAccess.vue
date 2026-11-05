<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessageBox } from 'element-plus'
import { botApiAccess, type ApiAlias, type BotApiAccess } from '@/api/apiAccess'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'
const props = defineProps<{ botId: string; canEdit: boolean; active: boolean }>()
const { t } = useI18n()
const state = ref<BotApiAccess | null>(null)
const loading = ref(false), busy = ref(false), error = ref('')
let generation = 0
let controller = new AbortController()
const permitted = computed(() => props.active && props.canEdit)
function stop() { controller.abort(); controller = new AbortController(); generation++ }
async function refresh() {
  if (!permitted.value) return
  const epoch = ++generation
  loading.value = true; error.value = ''
  try { const result = await botApiAccess.get(props.botId, controller.signal); if (epoch === generation) state.value = result }
  catch (e) { if (epoch === generation) error.value = errorMessage(e) }
  finally { if (epoch === generation) loading.value = false }
}
watch(() => [props.botId, props.active, props.canEdit], () => {
  stop(); state.value = null; error.value = ''; busy.value = false; loading.value = false
  if (permitted.value) void refresh()
}, { immediate: true })
onBeforeUnmount(stop)
async function act(action: () => Promise<BotApiAccess>) {
  const epoch = generation
  busy.value = true; error.value = ''
  try { const result = await action(); if (epoch === generation) state.value = result }
  catch (e) { if (epoch === generation) error.value = errorMessage(e) }
  finally { if (epoch === generation) busy.value = false }
}
async function toggle(enabled: boolean) {
  if (!state.value || busy.value || !permitted.value) return
  await act(() => botApiAccess.set(props.botId, enabled))
}
async function remove(row: ApiAlias) {
  try {
    await ElMessageBox.confirm(t('apiAccess.bot.removeConfirm', { alias: row.alias }), t('apiAccess.bot.remove'), {
      type: 'warning', confirmButtonText: t('apiAccess.bot.confirm'), cancelButtonText: t('apiAccess.bot.cancel'),
    })
  } catch { return }
  await act(() => botApiAccess.removeAlias(props.botId, row.id))
}
</script>

<template>
  <section
    class="api-access-panel"
    :aria-label="t('apiAccess.bot.tab')"
  >
    <el-empty
      v-if="!canEdit"
      :description="t('apiAccess.bot.noAccess')"
    />
    <template v-else>
      <p class="intro">
        {{ t('apiAccess.bot.intro') }}
      </p>
      <el-alert
        v-if="error"
        :title="error"
        type="error"
        :closable="false"
        role="alert"
      />
      <el-button
        v-if="error && !state"
        data-test="retry"
        :disabled="loading"
        @click="refresh"
      >
        {{ t('apiAccess.bot.retry') }}
      </el-button>
      <p
        v-if="loading && !state"
        role="status"
      >
        {{ t('apiAccess.bot.loading') }}
      </p>
      <template v-if="state">
        <div class="switch-row">
          <el-switch
            :model-value="state.enabled"
            data-test="api-access-switch"
            :loading="busy"
            :disabled="busy"
            :aria-label="t('apiAccess.bot.enabled')"
            @update:model-value="(value: string | number | boolean) => toggle(Boolean(value))"
          />
          <div>
            <strong>{{ t('apiAccess.bot.enabled') }}</strong>
            <p>{{ t('apiAccess.bot.enabledHint') }}</p>
          </div>
        </div>
        <h3 class="section-title">
          {{ t('apiAccess.bot.aliases') }}
        </h3>
        <p class="intro">
          {{ t('apiAccess.bot.aliasesHint') }}
        </p>
        <el-empty
          v-if="!state.aliases.length"
          :description="t('apiAccess.bot.empty')"
          :image-size="60"
        />
        <el-table
          v-else
          :data="state.aliases"
          row-key="id"
        >
          <el-table-column
            :label="t('apiAccess.bot.alias')"
            prop="alias"
          />
          <el-table-column :label="t('apiAccess.bot.chatId')">
            <template #default="{ row }">
              <code>{{ row.chat_id }}</code>
            </template>
          </el-table-column>
          <el-table-column :label="t('apiAccess.bot.createdBy')">
            <template #default="{ row }">
              {{ row.created_by || '—' }}
            </template>
          </el-table-column>
          <el-table-column :label="t('apiAccess.bot.createdAt')">
            <template #default="{ row }">
              {{ formatDateTime(row.created_at) }}
            </template>
          </el-table-column>
          <el-table-column width="90">
            <template #default="{ row }">
              <el-button
                link
                type="danger"
                :disabled="busy"
                :data-test="'remove-alias-' + row.alias"
                @click="remove(row)"
              >
                {{ t('apiAccess.bot.remove') }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </template>
  </section>
</template>

<style scoped>
.section-title { margin: 24px 0 8px; font-size: 16px; font-weight: 600; }
.intro { max-width: 70ch; margin: 0 0 16px; color: var(--el-text-color-secondary); line-height: 1.7; }
.switch-row { display: flex; align-items: flex-start; gap: 16px; margin-bottom: 16px; }
.switch-row p { margin: 6px 0 0; color: var(--el-text-color-secondary); line-height: 1.6; }
.api-access-panel .el-alert { margin: 16px 0; }
</style>
