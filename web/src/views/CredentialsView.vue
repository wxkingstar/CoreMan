<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import LoadState from '@/components/LoadState.vue'
import { useListQuery } from '@/composables/useListQuery'
import { nextTick, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { credentials, type ApiClient, type JwtKey } from '@/api/infrastructure'
import { formatDateTime } from '@/utils/format'
const { t } = useI18n()
const rows = ref<ApiClient[]>([]), keys = ref<JwtKey[]>([])
const tab = ref('clients')
const page = ref(1), total = ref(0), busy = ref(false), visible = ref(false), secret = ref('')
const editing = ref<ApiClient | null>(null)
const form = reactive({ app_key: '', name: '', scopes: [] as string[], enabled: true })
const formRef = ref<FormInstance>()
const formSession = ref(0)
const scopes = ['relay', 'org', 'notify', 'push', 'systems', 'memories', 'escalations']
function fail(e: unknown) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorMessage(e)) }
const listLoading = ref(false), listError = ref('')
const { persist: persistQuery } = useListQuery({ page, tab }, () => { void load() })
async function load() {
  if (!['clients', 'keys'].includes(tab.value)) tab.value = 'clients'
  persistQuery(); listLoading.value = true; listError.value = ''

  try { const [data, k] = await Promise.all([credentials.clients(page.value), credentials.keys()]); rows.value = data.items; total.value = data.total; keys.value = k }
  catch (e) { listError.value = errorMessage(e); fail(e) }
 finally { listLoading.value = false }
}
async function edit(row: ApiClient | null) {
  formSession.value += 1
  editing.value = row
  // 已下线的接口组（如旧的 cron）不再提供勾选，编辑时丢弃，保存才不会被后端当作未知接口组拒绝。
  Object.assign(form, row ? { ...row, scopes: row.scopes.filter((s) => scopes.includes(s)) } : { app_key: '', name: '', scopes: [], enabled: true })
  visible.value = true
  await nextTick()
  formRef.value?.clearValidate()
}
async function save() {
  if (busy.value) return
  busy.value = true
  try {
    form.name = form.name.trim()
    if (!await formRef.value?.validate().catch(() => false)) return
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
    <el-alert
      :title="t('infra.clientPurpose')"
      type="info"
      :closable="false"
      show-icon
    />
    <LoadState
      :loading="listLoading"
      :error="listError"
      @retry="load"
    />
    <el-tabs
      v-model="tab"
      @tab-change="persistQuery"
    >
      <el-tab-pane
        name="clients"
        :label="t('infra.clients')"
      >
        <el-button
          type="primary"
          data-test="create-client"
          @click="edit(null)"
        >
          {{ t('common.create') }}
        </el-button>

        <el-table
          v-if="!listLoading && !listError"
          :data="rows"
        >
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
            :label="t('infra.scopes')"
          >
            <template #default="{ row }">
              <div class="scope-tags">
                <el-tag
                  v-for="scope in row.scopes"
                  :key="scope"
                  size="small"
                >
                  {{ scopes.includes(scope) ? t('infra.scopeNames.' + scope) : scope }}
                </el-tag>
                <span v-if="!row.scopes.length">—</span>
              </div>
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
          v-if="!listLoading && !listError"
          v-model:current-page="page"
          :total="total"
          :page-size="50"
          layout="prev, pager, next"
          @current-change="load"
        />
      </el-tab-pane>
      <el-tab-pane
        name="keys"
        :label="t('infra.jwtKeys')"
      >
        <el-button
          type="primary"
          :disabled="busy"
          @click="rotateKey"
        >
          {{ t('infra.rotateKey') }}
        </el-button>
        <el-alert
          v-if="keys.some((k) => k.external)"
          :title="t('infra.externalKeyHint')"
          type="info"
          :closable="false"
          show-icon
        />
        <el-table
          v-if="!listLoading && !listError"
          :data="keys"
        >
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
              {{ t(row.external ? 'infra.externalKey' : row.is_active ? 'infra.signing' : 'infra.retired') }}
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
      <el-form
        :key="formSession"
        ref="formRef"
        :model="form"
        label-position="top"
      >
        <el-form-item
          :label="t('infra.key')"
          prop="app_key"
          :rules="[{ required: true, pattern: /^[a-zA-Z0-9_-]{2,128}$/, message: t('infra.clientKeyHint'), trigger: 'blur' }]"
        >
          <el-input
            v-model="form.app_key"
            :disabled="!!editing"
            placeholder="test-client"
            :maxlength="128"
          />
        </el-form-item>
        <p class="field-hint">
          {{ t('infra.clientKeyHint') }}
        </p>
        <el-form-item
          :label="t('infra.name')"
          prop="name"
          :rules="[{ required: true, min: 1, max: 100, message: t('infra.clientNameHint'), trigger: 'blur' }]"
        >
          <el-input
            v-model="form.name"
            :maxlength="100"
            :placeholder="t('infra.clientNamePlaceholder')"
          />
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
<style scoped>
:deep(.el-form-item__error) { position: static; width: 100%; padding-top: 6px; line-height: 1.5 }
.scope-tags { display: flex; flex-wrap: wrap; gap: 6px }
.el-table { margin-top:16px }  .secret { margin-top:16px } .field-hint { margin: 0 0 20px; color: var(--el-text-color-secondary); font-size: 12px }</style>
