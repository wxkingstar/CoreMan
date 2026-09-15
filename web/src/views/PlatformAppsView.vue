<script setup lang="ts">
import { errorMessage, fieldErrorMap, isVersionConflict } from '@/utils/errors'
import LoadState from '@/components/LoadState.vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { platformApps, syncRuns } from '@/api/admin'

import type { Platform, PlatformAppIn, PlatformAppOut, SyncRun } from '@/api/types'
import SecretInput from '@/components/SecretInput.vue'
import SyncRunsDialog from '@/components/SyncRunsDialog.vue'
import { usePaged } from '@/composables/usePaged'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()

const CAPABILITIES = ['contact_sync', 'login', 'notify', 'callback'] as const

type Filters = { platform: Platform | null }
const paged = usePaged<PlatformAppOut, Filters>((q) => platformApps.list(q), { platform: null })

const lastRun = reactive<Record<string, SyncRun | undefined>>({})
const syncing = reactive<Record<string, boolean>>({})
const pollTimers: Record<string, ReturnType<typeof setTimeout>> = {}
/** 卸载后置 false：还在飞的轮询请求回来时既不能再武装定时器，也不能在已离开的页面上弹消息。 */
let alive = true

async function reload() {
  await paged.load()
  await Promise.all(
    paged.items.value.map(async (a) => {
      lastRun[a.id] = (await platformApps.runs(a.id))[0]
    }),
  )
}

function search() {
  paged.page.value = 1
  reload()
}

function resetFilters() {
  paged.reset()
  reload()
}

function onPageChange(p: number) {
  paged.page.value = p
  reload()
}

function onSizeChange(size: number) {
  paged.perPage.value = size
  paged.page.value = 1
  reload()
}

function statusType(status: SyncRun['status']): 'primary' | 'success' | 'danger' | 'warning' {
  if (status === 'running') return 'primary'
  if (status === 'success') return 'success'
  if (status === 'failed') return 'danger'
  return 'warning'
}

function toBody(a: PlatformAppOut): PlatformAppIn {
  return {
    platform: a.platform,
    name: a.name,
    capabilities: a.capabilities,
    corp_id: a.corp_id,
    app_id: a.app_id,
    secret: a.secret,
    callback_token: a.callback_token,
    callback_aes_key: a.callback_aes_key,
    extra: a.extra,
    enabled: a.enabled,
  }
}

async function toggle(a: PlatformAppOut) {
  try {
    const updated = await platformApps.update(a.id, { ...toBody(a), enabled: !a.enabled }, a.version)
    Object.assign(a, updated)
    ElMessage.success(t('common.saved'))
  } catch (e) {
    if (isVersionConflict(e)) ElMessage.warning(t('common.conflict'))
    else ElMessage.error(errorMessage(e))
    await reload()
  }
}

async function runTest(a: PlatformAppOut) {
  try {
    const res = await platformApps.test(a.id)
    if (res.ok) ElMessage.success(`${t('apps.testOk')}${res.message ? `: ${res.message}` : ''}`)
    else ElMessage.error(res.message || t('common.loadFailed'))
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}

async function runSync(a: PlatformAppOut) {
  syncing[a.id] = true
  try {
    const { run_id } = await platformApps.sync(a.id)
    if (!alive) return
    ElMessage.info(t('apps.syncStarted'))
    const poll = async () => {
      try {
        const run = await syncRuns.get(run_id)
        // 请求在飞时页面卸载或应用被删（deleteApp 会清掉 syncing[a.id]）：到此为止。
        if (!alive || !syncing[a.id]) return
        if (run.status === 'running') {
          pollTimers[a.id] = setTimeout(poll, 2000)
          return
        }
        delete pollTimers[a.id]
        lastRun[a.id] = run
        syncing[a.id] = false
        const key = run.status === 'success' ? 'apps.syncDone' : run.status === 'aborted' ? 'apps.syncAborted' : 'apps.syncFailed'
        ;(run.status === 'success' ? ElMessage.success : ElMessage.error)(t(key) + (run.error ? `: ${run.error}` : ''))
      } catch (e) {
        if (!alive || !syncing[a.id]) return
        delete pollTimers[a.id]
        syncing[a.id] = false
        ElMessage.error(errorMessage(e))
      }
    }
    pollTimers[a.id] = setTimeout(poll, 2000)
  } catch (e) {
    if (!alive) return
    syncing[a.id] = false
    ElMessage.error(errorMessage(e))
  }
}

async function deleteApp(a: PlatformAppOut) {
  try {
    await ElMessageBox.confirm(t('apps.deleteConfirm'), t('common.delete'), {
      type: 'warning',
      confirmButtonText: t('common.confirm'),
      cancelButtonText: t('common.cancel'),
    })
  } catch {
    return
  }
  try {
    await platformApps.remove(a.id)
    // 行没了，正在跑的轮询也要收掉，否则它会一直查一个已删应用的 run
    clearTimeout(pollTimers[a.id])
    delete pollTimers[a.id]
    delete syncing[a.id]
    ElMessage.success(t('common.deleted'))
    await reload()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}

const runsDialog = reactive<{ visible: boolean; appId: string | null }>({ visible: false, appId: null })
function openRuns(a: PlatformAppOut) {
  runsDialog.appId = a.id
  runsDialog.visible = true
}

function emptyForm(): PlatformAppIn {
  return {
    platform: 'wecom',
    name: '',
    capabilities: [],
    corp_id: null,
    app_id: null,
    secret: '',
    callback_token: null,
    callback_aes_key: null,
    extra: {},
    enabled: true,
  }
}

const dialogVisible = ref(false)
const dialogEditingId = ref<string | null>(null)
const dialogEditingVersion = ref(0)
const formRef = ref<FormInstance>()
const form = reactive<PlatformAppIn>(emptyForm())
const extraText = ref('{}')

const formRules = computed<FormRules>(() => ({
  corp_id: [
    {
      validator: (_rule, value: string | null, callback: (err?: Error) => void) => {
        if (form.platform === 'wecom' && !value) callback(new Error(t('login.required', { field: t('apps.corpId') })))
        else callback()
      },
      trigger: 'blur',
    },
  ],
  secret: [{ required: true, message: t('login.required', { field: t('apps.secret') }), trigger: 'blur' }],
}))

const callbackUrl = ref<string | null>(null)
/** 后端 422 明细回填到表单项；每次打开或提交时清空。 */
const serverErrors = reactive<Record<string, string>>({})
const fieldLabels = computed<Record<string, string>>(() => ({
  platform: t('apps.platform'), name: t('apps.name'), capabilities: t('apps.capabilities'), corp_id: t('apps.corpId'),
  app_id: t('apps.appId'), secret: t('apps.secret'), callback_token: t('apps.callbackToken'),
  callback_aes_key: t('apps.callbackAesKey'), extra: t('apps.extra'), enabled: t('common.enabled'),
}))
function resetServerErrors() {
  for (const key of Object.keys(serverErrors)) delete serverErrors[key]
}

function openCreate() {
  resetServerErrors()
  callbackUrl.value = null
  dialogEditingId.value = null
  Object.assign(form, emptyForm())
  extraText.value = '{}'
  dialogVisible.value = true
}

function openEdit(a: PlatformAppOut) {
  resetServerErrors()
  callbackUrl.value = a.callback_url ?? null
  dialogEditingId.value = a.id
  dialogEditingVersion.value = a.version
  Object.assign(form, toBody(a))
  extraText.value = JSON.stringify(a.extra ?? {}, null, 2)
  dialogVisible.value = true
}

async function submitForm() {
  resetServerErrors()
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  let extra: Record<string, unknown>
  try {
    extra = JSON.parse(extraText.value || '{}')
  } catch {
    ElMessage.error(t('apps.extraInvalid'))
    return
  }
  const body: PlatformAppIn = { ...form, extra }

  try {
    if (dialogEditingId.value) await platformApps.update(dialogEditingId.value, body, dialogEditingVersion.value)
    else await platformApps.create(body)
    ElMessage.success(t('common.saved'))
    dialogVisible.value = false
    await reload()
  } catch (e) {
    // 只有版本冲突才关窗重载；「记录已存在」这类 409 保留表单，给后端原话。
    if (isVersionConflict(e)) {
      ElMessage.warning(t('common.conflict'))
      dialogVisible.value = false
      await reload()
    } else {
      Object.assign(serverErrors, fieldErrorMap(e, fieldLabels.value))
      ElMessage.error(errorMessage(e, fieldLabels.value))
    }
  }
}

onMounted(reload)

onBeforeUnmount(() => {
  alive = false
  Object.values(pollTimers).forEach((id) => clearTimeout(id))
})
</script>

<template>
  <div class="platform-apps-view">
    <div class="page-header cm-page-header">
      <h2>{{ t('apps.title') }}</h2>
      <p class="cm-page-intro">
        {{ t('workspace.intro.apps') }}
      </p>
      <el-button
        type="primary"
        data-test="create-app"
        @click="openCreate"
      >
        {{ t('common.create') }}
      </el-button>
    </div>

    <el-form
      :inline="true"
      @submit.prevent
    >
      <el-form-item :label="t('apps.platform')">
        <el-select
          v-model="paged.filters.platform"
          clearable
          :placeholder="t('apps.platform')"
          style="width: 140px"
        >
          <el-option
            :label="t('platforms.wecom')"
            value="wecom"
          />
          <el-option
            :label="t('platforms.feishu')"
            value="feishu"
          />
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-button
          type="primary"
          @click="search"
        >
          {{ t('common.search') }}
        </el-button>
        <el-button @click="resetFilters">
          {{ t('common.reset') }}
        </el-button>
      </el-form-item>
    </el-form>

    <LoadState
      :error="paged.error.value"
      @retry="paged.load"
    />
    <el-table
      v-loading="paged.loading.value"
      :data="paged.items.value"
    >
      <el-table-column
        min-width="140"
        :label="t('apps.platform')"
      >
        <template #default="{ row }: { row: PlatformAppOut }">
          <el-tag :type="row.platform === 'wecom' ? 'primary' : 'warning'">
            {{ t(`platforms.${row.platform}`) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        prop="name"
        :label="t('apps.name')"
      />
      <el-table-column
        min-width="140"
        :label="t('apps.capabilities')"
      >
        <template #default="{ row }: { row: PlatformAppOut }">
          <el-tag
            v-for="c in row.capabilities"
            :key="c"
            size="small"
            class="cap-tag"
          >
            {{ t('apps.caps.' + c) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        :label="`${t('apps.corpId')} / ${t('apps.appId')}`"
      >
        <template #default="{ row }: { row: PlatformAppOut }">
          <div>{{ row.corp_id ?? '-' }}</div>
          <div class="muted">
            {{ row.app_id ?? '-' }}
          </div>
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        :label="t('common.enabled')"
      >
        <template #default="{ row }: { row: PlatformAppOut }">
          <el-switch
            :data-test="'toggle-' + row.id"
            :model-value="row.enabled"
            @change="toggle(row)"
          />
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        :label="t('apps.lastSync')"
      >
        <template #default="{ row }: { row: PlatformAppOut }">
          <template v-if="lastRun[row.id]">
            <el-tag
              :type="statusType(lastRun[row.id]!.status)"
              size="small"
            >
              {{ t('apps.runStatus.' + lastRun[row.id]!.status) }}
            </el-tag>
            <div class="muted">
              {{ formatDateTime(lastRun[row.id]!.finished_at ?? lastRun[row.id]!.started_at) }}
            </div>
          </template>
          <span v-else>{{ t('apps.never') }}</span>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('common.actions')"
        width="380"
        class-name="app-actions"
      >
        <template #default="{ row }: { row: PlatformAppOut }">
          <el-button
            size="small"
            :data-test="'test-' + row.id"
            @click="runTest(row)"
          >
            {{ t('apps.test') }}
          </el-button>
          <el-button
            v-if="row.capabilities.includes('contact_sync')"
            size="small"
            :data-test="'sync-' + row.id"
            :loading="!!syncing[row.id]"
            @click="runSync(row)"
          >
            {{ t('apps.sync') }}
          </el-button>
          <el-button
            size="small"
            :data-test="'runs-' + row.id"
            @click="openRuns(row)"
          >
            {{ t('apps.runs') }}
          </el-button>
          <el-button
            size="small"
            :data-test="'edit-' + row.id"
            @click="openEdit(row)"
          >
            {{ t('common.edit') }}
          </el-button>
          <el-button
            size="small"
            type="danger"
            :data-test="'delete-' + row.id"
            @click="deleteApp(row)"
          >
            {{ t('common.delete') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      :total="paged.total.value"
      :current-page="paged.page.value"
      :page-size="paged.perPage.value"
      :page-sizes="[20, 50, 100, 200]"
      layout="total, prev, pager, next, sizes"
      @current-change="onPageChange"
      @size-change="onSizeChange"
    />

    <el-dialog
      v-model="dialogVisible"
      :close-on-click-modal="false"
      :title="dialogEditingId ? t('common.edit') : t('common.create')"
      width="640px"
      destroy-on-close
    >
      <el-form
        ref="formRef"
        :model="form"
        :rules="formRules"
        label-width="120px"
      >
        <el-form-item
          :label="t('apps.platform')"
          :error="serverErrors.platform"
        >
          <el-radio-group
            v-model="form.platform"
            :disabled="!!dialogEditingId"
          >
            <el-radio label="wecom">
              {{ t('platforms.wecom') }}
            </el-radio>
            <el-radio label="feishu">
              {{ t('platforms.feishu') }}
            </el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item
          :label="t('apps.name')"
          :error="serverErrors.name"
        >
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item
          :label="t('apps.capabilities')"
          :error="serverErrors.capabilities"
        >
          <el-checkbox-group v-model="form.capabilities">
            <el-checkbox
              v-for="c in CAPABILITIES"
              :key="c"
              :label="c"
            >
              {{ t('apps.caps.' + c) }}
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item
          :label="t('apps.corpId')"
          prop="corp_id"
          :error="serverErrors.corp_id"
        >
          <el-input
            :model-value="form.corp_id ?? ''"
            @update:model-value="form.corp_id = ($event as string) || null"
          />
        </el-form-item>
        <el-form-item
          :label="t('apps.appId')"
          :error="serverErrors.app_id"
        >
          <el-input
            :model-value="form.app_id ?? ''"
            @update:model-value="form.app_id = ($event as string) || null"
          />
        </el-form-item>
        <el-form-item
          :label="t('apps.secret')"
          prop="secret"
          :error="serverErrors.secret"
        >
          <SecretInput
            :model-value="form.secret"
            :start-editing="!dialogEditingId"
            :placeholder="t('apps.secret')"
            @update:model-value="form.secret = $event ?? ''"
          />
        </el-form-item>
        <el-form-item
          v-if="callbackUrl"
          :label="t('apps.callbackUrl')"
        >
          <el-input
            :model-value="callbackUrl"
            readonly
          />
          <small>{{ t('apps.callbackUrlHint') }}</small>
        </el-form-item>
        <el-form-item
          :label="t('apps.callbackToken')"
          :error="serverErrors.callback_token"
        >
          <SecretInput
            v-model="form.callback_token"
            :placeholder="t('apps.callbackToken')"
          />
        </el-form-item>
        <el-form-item
          :label="t('apps.callbackAesKey')"
          :error="serverErrors.callback_aes_key"
        >
          <SecretInput
            v-model="form.callback_aes_key"
            :placeholder="t('apps.callbackAesKey')"
          />
        </el-form-item>
        <el-form-item
          :label="t('apps.extra')"
          :error="serverErrors.extra"
        >
          <el-input
            v-model="extraText"
            type="textarea"
            :rows="4"
          />
        </el-form-item>
        <el-form-item :label="t('common.enabled')">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          type="primary"
          data-test="save-app"
          @click="submitForm"
        >
          {{ t('common.save') }}
        </el-button>
      </template>
    </el-dialog>

    <SyncRunsDialog
      v-model="runsDialog.visible"
      :app-id="runsDialog.appId"
    />
  </div>
</template>

<style scoped>
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
.cap-tag { margin-right: 4px; }
:deep(.app-actions .cell) { white-space: nowrap; }
</style>
