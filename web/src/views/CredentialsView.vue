<script setup lang="ts">
import LoadState from '@/components/LoadState.vue'
import { useListQuery } from '@/composables/useListQuery'
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { credentials, type ApiClient, type JwtKey } from '@/api/infrastructure'
import { formatDateTime } from '@/utils/format'
const { t } = useI18n()
const rows = ref<ApiClient[]>([]), keys = ref<JwtKey[]>([])
const page = ref(1), total = ref(0), busy = ref(false), visible = ref(false), secret = ref('')
const editing = ref<ApiClient | null>(null)
const form = reactive({ app_key: '', name: '', scopes: [] as string[], enabled: true })
const scopes = ['relay', 'org', 'notify', 'push', 'systems', 'memories', 'cron', 'escalations']
function fail(e: unknown) { if (e !== 'cancel' && e !== 'close') ElMessage.error(e instanceof Error ? e.message : String(e)) }
const listLoading = ref(false), listError = ref('')
const { persist: persistQuery } = useListQuery({ page }, () => { void load() })
async function load() {
  persistQuery(); listLoading.value = true; listError.value = ''

  try { const [data, k] = await Promise.all([credentials.clients(page.value), credentials.keys()]); rows.value = data.items; total.value = data.total; keys.value = k }
  catch (e) { listError.value = e instanceof Error ? e.message : String(e); fail(e) }
 finally { listLoading.value = false }
}
function edit(row: ApiClient | null) { editing.value = row; Object.assign(form, row ? { ...row, scopes: [...row.scopes] } : { app_key: '', name: '', scopes: [], enabled: true }); visible.value = true }
async function save() {
  if (busy.value) return
  busy.value = true
  try {
    if (editing.value) await credentials.update({ ...editing.value, ...form })
    else secret.value = (await credentials.create({ app_key: form.app_key, name: form.name, scopes: form.scopes, enabled: form.enabled })).secret
    visible.value = false; ElMessage.success(t('common.saved')); await load()
  } catch (e) { fail(e) } finally { busy.value = false }
}
async function rotate(row: ApiClient) {
  if (busy.value) return
  try { await ElMessageBox.confirm(t('infra.rotateWarning')); busy.value = true; secret.value = (await credentials.rotate(row)).secret; await load() }
  catch (e) { fail(e) } finally { busy.value = false }
}
async function rotateKey() {
  if (busy.value) return
  try { await ElMessageBox.confirm(t('infra.rotateKeyWarning')); busy.value = true; await credentials.rotateKey(); await load() }
  catch (e) { fail(e) } finally { busy.value = false }
}
onMounted(load)
</script>
<template>
  <section>
    <header class="cm-page-header">
      <h2>{{ t('menu.credentials') }}</h2>
      <p class="cm-page-intro">
        {{ t('workspace.intro.credentials') }}
      </p>
    </header>
    <el-tabs>
      <el-tab-pane :label="t('infra.clients')">
        <el-button
          type="primary"
          data-test="create-client"
          @click="edit(null)"
        >
          {{ t('common.create') }}
        </el-button>
        <LoadState
          :loading="listLoading"
          :error="listError"
          @retry="load"
        />
        <el-table :data="rows">
          <el-table-column
            min-width="140"
            prop="name"
            :label="t('infra.name')"
          />
          <el-table-column
            min-width="140"
            prop="app_key"
            :label="t('infra.key')"
          />
          <el-table-column
            min-width="140"
            prop="scopes"
            :label="t('infra.scopes')"
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
                @click="edit(row)"
              >
                {{ t('common.edit') }}
              </el-button><el-button
                link
                :disabled="busy"
                @click="rotate(row)"
              >
                {{ t('infra.rotateSecret') }}
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
      </el-tab-pane>
      <el-tab-pane :label="t('infra.jwtKeys')">
        <el-button
          type="primary"
          :disabled="busy"
          @click="rotateKey"
        >
          {{ t('infra.rotateKey') }}
        </el-button>
        <el-table :data="keys">
          <el-table-column
            min-width="140"
            prop="kid"
            :label="t('infra.key')"
          />
          <el-table-column
            min-width="140"
            :label="t('infra.status')"
          >
            <template #default="{ row }">
              {{ t(row.is_active ? 'infra.signing' : 'infra.retired') }}
            </template>
          </el-table-column>
          <el-table-column
            min-width="140"
            :label="t('infra.createdAt')"
          >
            <template #default="{ row }">
              {{ formatDateTime(row.created_at) }}
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>
    <el-dialog
      v-model="visible"
      :close-on-click-modal="false"
      :title="t('infra.clients')"
      width="560px"
    >
      <el-form label-position="top">
        <el-form-item :label="t('infra.key')">
          <el-input
            v-model="form.app_key"
            :disabled="!!editing"
          />
        </el-form-item>
        <el-form-item :label="t('infra.name')">
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item :label="t('infra.scopes')">
          <el-select
            v-model="form.scopes"
            multiple
            style="width:100%"
          >
            <el-option
              v-for="scope in scopes"
              :key="scope"
              :value="scope"
              :label="t('infra.scopeNames.' + scope)"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('infra.enabled')">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="visible = false">
          {{ t('common.cancel') }}
        </el-button><el-button
          type="primary"
          :loading="busy"
          data-test="save-client"
          @click="save"
        >
          {{ t('common.save') }}
        </el-button>
      </template>
    </el-dialog>
    <el-dialog
      :model-value="!!secret"
      :close-on-click-modal="false"
      :title="t('infra.secretOnce')"
      width="600px"
      @close="secret = ''"
    >
      <el-alert
        :title="t('infra.secretHint')"
        :closable="false"
        type="warning"
      />
      <el-input
        :model-value="secret"
        type="textarea"
        readonly
        :rows="3"
        class="secret"
      />
      <template #footer>
        <el-button @click="secret = ''">
          {{ t('common.confirm') }}
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>
<style scoped>.el-table { margin-top:16px }  .secret { margin-top:16px }</style>
