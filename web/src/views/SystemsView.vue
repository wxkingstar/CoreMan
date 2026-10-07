<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import LoadState from '@/components/LoadState.vue'
import MarkdownContent from '@/components/MarkdownContent.vue'
import { useListQuery } from '@/composables/useListQuery'
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { bots } from '@/api/admin'
import { call, http } from '@/api/client'
import { systems, type BusinessSystem, type SystemCatalog, type SystemInput, type TokenProvider } from '@/api/infrastructure'
import { formatBytes, formatDateTime } from '@/utils/format'
import type { BotOut } from '@/api/types'

const { t } = useI18n()
const rows = ref<BusinessSystem[]>([])
const botOptions = ref<BotOut[]>([])
const providerOptions = ref<TokenProvider[]>([{ id: 'builtin', max_token_ttl_seconds: null }])
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
const empty = (): SystemInput => ({ key: '', name: '', description: '', base_url: '', openapi_url: '', token_provider: 'builtin', token_audience: '', access_test_url: '', enabled: true, sort_order: 0, default_for_all_bots: false, allowed_bot_ids: [] })
const form = reactive(empty())
function fail(e: unknown) { ElMessage.error(errorMessage(e)) }
// 接入规范随管理台作为公开静态文件发布，管理员可以直接把链接交给新接入的业务系统
const CONTRACT_PATH = '/integration/business-system-openapi-contract.md'
const RULESET_PATH = '/integration/business-system-contract.spectral.yaml'
const contractVisible = ref(false), contract = ref('')
async function openContract() {
  contractVisible.value = true
  if (contract.value) return
  try {
    const response = await fetch(CONTRACT_PATH)
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    contract.value = await response.text()
  } catch (e) { fail(e) }
}
async function copyContractLink() {
  try { await navigator.clipboard.writeText(new URL(CONTRACT_PATH, window.location.origin).href); ElMessage.success(t('common.copied')) }
  catch (e) { fail(e) }
}
const listLoading = ref(false), listError = ref('')
const { persist: persistQuery } = useListQuery({ page }, () => { void load() })
async function load() {
  persistQuery(); listLoading.value = true; listError.value = ''

  try { const data = await systems.list(page.value); rows.value = data.items; total.value = data.total }
  catch (e) { listError.value = errorMessage(e); fail(e) }
 finally { listLoading.value = false }
}
// 操作目录：业务系统 OpenAPI 描述的拉取与编译结果，编辑已有系统时展示
const catalog = ref<SystemCatalog | null>(null), refreshing = ref(false)
async function loadCatalog(key: string) {
  try { catalog.value = await systems.catalog(key) } catch (e) { fail(e) }
}
async function refreshCatalog() {
  if (!editing.value || refreshing.value) return
  refreshing.value = true
  try {
    catalog.value = await systems.refreshCatalog(editing.value.key)
    if (catalog.value?.status === 'ok') ElMessage.success(t('infra.catalogRefreshed'))
    else if (catalog.value) ElMessage.warning(t('infra.catalogFailed', { error: catalog.value.error ?? '' }))
  } catch (e) { fail(e) }
  finally { refreshing.value = false }
}
function catalogTag(status: string) { return status === 'ok' ? 'success' : status === 'stale' ? 'warning' : 'danger' }
async function edit(row: BusinessSystem | null) {
  editing.value = row
  catalog.value = null
  if (row?.openapi_url) void loadCatalog(row.key)
  Object.assign(form, row ? { ...empty(), ...row, allowed_bot_ids: row.allowed_bot_ids ? [...row.allowed_bot_ids] : null } : empty())
  // 新建默认限定且名单为空：发言者令牌会注入 AI 员工的运行环境，对全部员工开放须管理员主动关闭限定
  restricted.value = !row || row.allowed_bot_ids !== null
  visible.value = true
  try {
    providerOptions.value = await systems.providers()
    if (form.token_provider && !providerOptions.value.some(p => p.id === form.token_provider)) {
      providerOptions.value.push({ id: form.token_provider, max_token_ttl_seconds: null })
    }
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
    const body = { key: form.key, name: form.name, description: form.description, base_url: form.base_url, openapi_url: form.openapi_url, token_provider: form.token_provider, token_audience: form.token_audience, access_test_url: form.access_test_url, enabled: form.enabled, sort_order: form.sort_order, default_for_all_bots: form.default_for_all_bots, allowed_bot_ids: restricted.value ? (form.allowed_bot_ids ?? []) : null }
    let saved: BusinessSystem
    if (editing.value) {
      if (restricted.value) await ElMessageBox.confirm(t('infra.reclaimWarning'), t('common.confirm'))
      saved = await systems.update(editing.value, body)
    } else saved = await systems.create(body)
    visible.value = false; ElMessage.success(t('common.saved'))
    // 保存时改了地址会立即拉取一次目录，失败不影响保存，只提示原因
    const result = saved.catalog
    if (result && result.status !== 'ok') ElMessage.warning(t('infra.catalogFailed', { error: result.error ?? '' }))
    await load()
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
        min-width="160"
        :label="t('infra.botScope')"
      >
        <template #default="{ row }">
          <el-tag
            v-if="row.allowed_bot_ids === null"
            data-test="open-to-all"
            type="warning"
          >
            {{ t('infra.openToAllBots') }}
          </el-tag>
          <span v-else>{{ t('infra.restrictedCount', { n: row.allowed_bot_ids.length }) }}</span>
        </template>
      </el-table-column>
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
            :disabled="!row.enabled || !row.base_url || (row.token_provider && row.token_provider !== 'builtin' && !row.access_test_url)"
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
        <el-form-item :label="t('infra.openapiUrl')">
          <el-input
            v-model="form.openapi_url"
            data-test="openapi-url"
          />
          <small>{{ t('infra.openapiUrlHint') }}<el-button
            data-test="open-contract"
            class="hint-link"
            link
            type="primary"
            @click="openContract"
          >{{ t('infra.contract') }}</el-button></small>
        </el-form-item>
        <el-form-item
          v-if="editing && editing.openapi_url"
          :label="t('infra.catalogTitle')"
        >
          <div
            class="catalog"
            data-test="catalog-panel"
          >
            <div class="catalog-head">
              <el-tag
                v-if="catalog"
                data-test="catalog-status"
                :type="catalogTag(catalog.status)"
              >
                {{ t(`infra.catalogStatus.${catalog.status}`) }}
              </el-tag>
              <span v-else>{{ t('infra.catalogNever') }}</span>
              <el-button
                data-test="refresh-catalog"
                size="small"
                :loading="refreshing"
                @click="refreshCatalog"
              >
                {{ t('infra.refreshCatalog') }}
              </el-button>
            </div>
            <template v-if="catalog">
              <small v-if="catalog.error">{{ t('infra.catalogError', { error: catalog.error }) }}</small>
              <small data-test="catalog-summary">{{ t('infra.catalogSummary', { modules: catalog.module_count, operations: catalog.operation_count, hidden: catalog.hidden_count }) }}</small>
              <small>{{ t('infra.catalogFetchedAt') }}：{{ formatDateTime(catalog.fetched_at) }} · {{ t('infra.catalogSize') }}：{{ catalog.spec_bytes == null ? '—' : formatBytes(catalog.spec_bytes) }}</small>
              <small>{{ catalog.lint.length ? t('infra.catalogLint', { errors: catalog.lint_errors, warnings: catalog.lint_warnings }) : t('infra.catalogLintNone') }}</small>
              <ul
                v-if="catalog.lint.length"
                class="catalog-lint"
                data-test="catalog-lint"
              >
                <li
                  v-for="(item, index) in catalog.lint"
                  :key="index"
                >
                  <el-tag
                    size="small"
                    :type="item.severity === 'error' ? 'danger' : 'warning'"
                  >
                    {{ item.rule }}
                  </el-tag>
                  <code>{{ item.path }}</code> {{ item.message }}
                </li>
              </ul>
            </template>
          </div>
        </el-form-item>
        <el-form-item :label="t('infra.tokenProvider')">
          <el-select
            v-model="form.token_provider"
            data-test="token-provider"
            style="width:100%"
          >
            <el-option
              v-for="provider in providerOptions"
              :key="provider.id"
              :value="provider.id"
              :label="provider.id === 'builtin' ? t('infra.builtinProvider') : provider.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('infra.tokenAudience')">
          <el-input
            v-model="form.token_audience"
            :placeholder="form.key"
          />
        </el-form-item>
        <el-form-item :label="t('infra.accessTestUrl')">
          <el-input v-model="form.access_test_url" />
          <small>{{ t('infra.accessTestUrlHint') }}</small>
        </el-form-item>
        <el-form-item :label="t('infra.enabled')">
          <el-switch v-model="form.enabled" /><small class="switch-hint">{{ t('infra.enabledHint') }}</small>
        </el-form-item>
        <el-form-item :label="t('infra.defaultAccess')">
          <el-switch v-model="form.default_for_all_bots" /><small class="switch-hint">{{ t('infra.defaultAccessHint') }}</small>
        </el-form-item>
        <el-form-item :label="t('infra.restrictBots')">
          <el-switch v-model="restricted" /><small class="switch-hint">{{ t('infra.restrictBotsHint') }}</small>
          <small
            v-if="!restricted"
            data-test="open-to-all-warning"
            class="open-warning"
          >{{ t('infra.openToAllWarning') }}</small>
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
          <el-input-number v-model="form.sort_order" /><small class="switch-hint">{{ t('infra.sortOrderHint') }}</small>
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
    <el-drawer
      v-model="contractVisible"
      :title="t('infra.contractTitle')"
      size="min(760px, 100%)"
      append-to-body
    >
      <div class="contract-actions">
        <el-button
          data-test="copy-contract-link"
          @click="copyContractLink"
        >
          {{ t('infra.copyContractLink') }}
        </el-button><el-button
          tag="a"
          :href="CONTRACT_PATH"
          download
        >
          {{ t('infra.downloadContract') }}
        </el-button><el-button
          tag="a"
          :href="RULESET_PATH"
          download
        >
          {{ t('infra.downloadRuleset') }}
        </el-button>
      </div>
      <MarkdownContent
        v-if="contract"
        data-test="contract-content"
        :content="contract"
      />
    </el-drawer>
  </section>
</template>
<style scoped>.toolbar { display:flex; justify-content:space-between; align-items:center; margin-bottom:20px }  small { color:var(--el-text-color-secondary); margin-top:8px } .switch-hint { flex:1; min-width:0; margin:0 0 0 12px; line-height:1.5 } .open-warning { display:block; width:100%; color:var(--el-color-warning) } .contract-actions { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px } .contract-actions .el-button + .el-button { margin-left:0 } .hint-link { margin-left:4px; vertical-align:baseline } .catalog { display:flex; flex-direction:column; width:100%; gap:2px } .catalog small { margin-top:2px } .catalog-head { display:flex; align-items:center; gap:8px } .catalog-lint { margin:6px 0 0; padding-left:0; list-style:none; max-height:240px; overflow:auto; font-size:12px; line-height:1.6; color:var(--el-text-color-regular) } .catalog-lint li { overflow-wrap:anywhere } .catalog-lint code { margin:0 4px }</style>
