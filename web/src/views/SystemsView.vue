<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import LoadState from '@/components/LoadState.vue'
import { useListQuery } from '@/composables/useListQuery'
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { bots } from '@/api/admin'
import { call, http } from '@/api/client'
import { systems, type BusinessSystem, type SystemInput } from '@/api/infrastructure'
import type { BotOut } from '@/api/types'

const { t } = useI18n()
const rows = ref<BusinessSystem[]>([])
const botOptions = ref<BotOut[]>([])
const page = ref(1), total = ref(0), busy = ref(false), visible = ref(false), restricted = ref(false)
const editing = ref<BusinessSystem | null>(null)
const testing = ref<string | null>(null)
async function testAccess(row: BusinessSystem) {
  if (testing.value) return
  testing.value = row.key
  try {
    const result = await call<{ success: boolean; status_code: number; message: string }>(http.post('/api/admin/systems/test-access', { system_key: row.key }))
    const message = `${result.message} (HTTP ${result.status_code || '—'})`
    if (result.success) ElMessage.success(message)
    else ElMessage.warning(message)
  } catch (e) { fail(e) }
  finally { testing.value = null }
}
const empty = (): SystemInput => ({ key: '', name: '', description: '', base_url: '', sitemap_url: '', enabled: true, sort_order: 0, default_for_all_bots: false, allowed_bot_ids: null })
const form = reactive(empty())
function fail(e: unknown) { ElMessage.error(errorMessage(e)) }
const listLoading = ref(false), listError = ref('')
const { persist: persistQuery } = useListQuery({ page }, () => { void load() })
async function load() {
  persistQuery(); listLoading.value = true; listError.value = ''

  try { const data = await systems.list(page.value); rows.value = data.items; total.value = data.total }
  catch (e) { listError.value = errorMessage(e); fail(e) }
 finally { listLoading.value = false }
}
async function edit(row: BusinessSystem | null) {
  editing.value = row
  Object.assign(form, row ? { ...row, allowed_bot_ids: row.allowed_bot_ids ? [...row.allowed_bot_ids] : null } : empty())
  restricted.value = !!row && row.allowed_bot_ids !== null
  visible.value = true
  try {
    const all: BotOut[] = []
    for (let p = 1; ; p++) {
      const data = await bots.list({ scope: 'all', page: p, per_page: 200 }); all.push(...data.items)
      if (all.length >= data.total || !data.items.length) break
    }
    botOptions.value = all
  } catch (e) { fail(e) }
}
async function save() {
  if (busy.value) return
  busy.value = true
  try {
    const body = { key: form.key, name: form.name, description: form.description, base_url: form.base_url, sitemap_url: form.sitemap_url, enabled: form.enabled, sort_order: form.sort_order, default_for_all_bots: form.default_for_all_bots, allowed_bot_ids: restricted.value ? (form.allowed_bot_ids ?? []) : null }
    if (editing.value) {
      if (restricted.value) await ElMessageBox.confirm(t('infra.reclaimWarning'), t('common.confirm'))
      await systems.update(editing.value, body)
    } else await systems.create(body)
    visible.value = false; ElMessage.success(t('common.saved')); await load()
  } catch (e) { if (e !== 'cancel' && e !== 'close') fail(e) }
  finally { busy.value = false }
}
async function remove(row: BusinessSystem) {
  try { await ElMessageBox.confirm(t('common.confirmDelete')); await systems.remove(row); await load() }
  catch (e) { if (e !== 'cancel' && e !== 'close') fail(e) }
}
onMounted(load)
</script>

<template>
  <section>
    <div class="toolbar cm-page-header">
      <h2>{{ t('menu.systems') }}</h2>
      <p class="cm-page-intro">
        {{ t('workspace.intro.systems') }}
      </p><el-button
        data-test="create-system"
        type="primary"
        @click="edit(null)"
      >
        {{ t('common.create') }}
      </el-button>
    </div>
    <LoadState
      :loading="listLoading"
      :error="listError"
      @retry="load"
    />
    <el-table :data="rows">
      <el-table-column
        min-width="140"
        prop="key"
        :label="t('infra.key')"
      />
      <el-table-column
        min-width="140"
        prop="name"
        :label="t('infra.name')"
      />
      <el-table-column
        min-width="140"
        prop="base_url"
        :label="t('infra.baseUrl')"
      />
      <el-table-column
        min-width="140"
        :label="t('infra.status')"
      >
        <template #default="{ row }">
          {{ t(row.enabled ? 'common.enabled' : 'common.disabled') }}
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        :label="t('common.actions')"
      >
        <template #default="{ row }">
          <el-button
            link
            :loading="testing === row.key"
            :disabled="!row.enabled || !row.base_url"
            @click="testAccess(row)"
          >
            {{ t('infra.testAccess') }}
          </el-button>
          <el-button
            link
            @click="edit(row)"
          >
            {{ t('common.edit') }}
          </el-button><el-button
            link
            type="danger"
            @click="remove(row)"
          >
            {{ t('common.delete') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination
      v-model:current-page="page"
      :total="total"
      :page-size="50"
      layout="prev, pager, next"
      @current-change="load"
    />
    <el-dialog
      v-model="visible"
      :close-on-click-modal="false"
      :title="t('menu.systems')"
      width="640px"
    >
      <el-form
        label-position="top"
        @submit.prevent="save"
      >
        <el-form-item :label="t('infra.key')">
          <el-input
            v-model="form.key"
            :disabled="!!editing"
          />
        </el-form-item>
        <el-form-item :label="t('infra.name')">
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item :label="t('infra.description')">
          <el-input
            v-model="form.description"
            type="textarea"
          />
        </el-form-item>
        <el-form-item :label="t('infra.baseUrl')">
          <el-input v-model="form.base_url" />
        </el-form-item>
        <el-form-item :label="t('infra.sitemapUrl')">
          <el-input v-model="form.sitemap_url" />
        </el-form-item>
        <el-form-item :label="t('infra.enabled')">
          <el-switch v-model="form.enabled" />
        </el-form-item>
        <el-form-item :label="t('infra.defaultAccess')">
          <el-switch v-model="form.default_for_all_bots" />
        </el-form-item>
        <el-form-item :label="t('infra.restrictBots')">
          <el-switch v-model="restricted" />
        </el-form-item>
        <el-form-item
          v-if="restricted"
          :label="t('infra.allowedBots')"
        >
          <el-select
            v-model="form.allowed_bot_ids"
            multiple
            filterable
            style="width:100%"
          >
            <el-option
              v-for="bot in botOptions"
              :key="bot.id"
              :value="bot.id"
              :label="bot.name"
            />
          </el-select><small>{{ t('infra.emptyWhitelist') }}</small>
        </el-form-item>
        <el-form-item :label="t('infra.sortOrder')">
          <el-input-number v-model="form.sort_order" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="visible = false">
          {{ t('common.cancel') }}
        </el-button><el-button
          data-test="save-system"
          type="primary"
          :loading="busy"
          @click="save"
        >
          {{ t('common.save') }}
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>
<style scoped>.toolbar { display:flex; justify-content:space-between; align-items:center; margin-bottom:20px }  small { color:var(--el-text-color-secondary); margin-top:8px }</style>
