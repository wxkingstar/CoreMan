<script setup lang="ts">
import LoadState from '@/components/LoadState.vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { announcements as api, bots as botsApi, relays as relaysApi } from '@/api/admin'
import type { AnnouncementIn, AnnouncementOut, AnnouncementScope } from '@/api/types'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()

const rows = ref<AnnouncementOut[]>([])
const loading = ref(false)
const listError = ref('')
const botOptions = ref<{ id: string; label: string }[]>([])
const relayOptions = ref<{ id: string; label: string }[]>([])

function fail(e: unknown): void {
  // 422（范围与目标不匹配 / 目标不存在 / 结束早于开始）都是后端写好的中文，直接透传。
  ElMessage.error(e instanceof Error ? e.message : String(e))
}

/** 公告不分页，一次拉全量；增删改与启停之后都要重新拉，页面才不会停在旧状态。 */
async function load(): Promise<void> {
  loading.value = true
  try {
    listError.value = ''
    rows.value = await api.list()
  } catch (e) {
    listError.value = e instanceof Error ? e.message : String(e)
    fail(e)
  } finally {
    loading.value = false
  }
}

async function loadTargets(): Promise<void> {
  try {
    // scope=all：公告能挂到任何一个机器人上，下拉里列出可见的全部。
    const [bots, relays] = await Promise.all([
      botsApi.list({ scope: 'all', per_page: 200 }),
      relaysApi.list({ per_page: 200 }),
    ])
    botOptions.value = bots.items.map((b) => ({ id: b.id, label: `${b.name} (${b.bot_key})` }))
    relayOptions.value = relays.items.map((r) => ({ id: r.id, label: r.name }))
  } catch (e) {
    fail(e)
  }
}

onMounted(async () => {
  await Promise.all([load(), loadTargets()])
})

function emptyForm(): AnnouncementIn {
  return { scope: 'global', relay_server_id: null, bot_id: null, content: '', is_active: true, start_at: null, end_at: null }
}

const dialogVisible = ref(false)
const editingId = ref<string | null>(null)
const formRef = ref<FormInstance>()
const form = reactive<AnnouncementIn>(emptyForm())
// 生效时间窗的两头各自独立：后端只拒绝「结束早于开始」，单边（只有开始或只有结束）是一等状态，
// 用 datetimerange 表示不了，所以拆成两个可清空的 datetime。el-date-picker 的 v-model 是 Date，提交时转 ISO。
const startAt = ref<Date | null>(null)
const endAt = ref<Date | null>(null)

const formRules = computed<FormRules>(() => ({
  content: [{ required: true, message: t('announcements.contentRequired'), trigger: 'blur' }],
  // 目标只在对应范围下才是必填，所以用 validator 而不是 required。
  bot_id: [
    {
      validator: (_rule, _value, callback) =>
        form.scope === 'bot' && !form.bot_id ? callback(new Error(t('announcements.targetRequired'))) : callback(),
      trigger: 'change',
    },
  ],
  relay_server_id: [
    {
      validator: (_rule, _value, callback) =>
        form.scope === 'relay' && !form.relay_server_id
          ? callback(new Error(t('announcements.targetRequired')))
          : callback(),
      trigger: 'change',
    },
  ],
}))

/** 换范围就把另一条目标 id 清掉，否则后端会以「公告范围与目标不匹配」422 打回来。 */
function onScopeChange(scope: AnnouncementScope): void {
  form.scope = scope
  if (scope !== 'bot') form.bot_id = null
  if (scope !== 'relay') form.relay_server_id = null
}

function openCreate(): void {
  editingId.value = null
  Object.assign(form, emptyForm())
  startAt.value = null
  endAt.value = null
  dialogVisible.value = true
}

function openEdit(row: AnnouncementOut): void {
  editingId.value = row.id
  Object.assign(form, {
    scope: row.scope,
    relay_server_id: row.relay_server_id,
    bot_id: row.bot_id,
    content: row.content,
    is_active: row.is_active,
    start_at: row.start_at,
    end_at: row.end_at,
  })
  // 两头各自回显，不得互相补值：把空的那头填成另一头会把半开区间压成零长度窗口，公告会被悄悄关掉。
  startAt.value = row.start_at ? new Date(row.start_at) : null
  endAt.value = row.end_at ? new Date(row.end_at) : null
  dialogVisible.value = true
}

function payload(): AnnouncementIn {
  return {
    scope: form.scope,
    bot_id: form.scope === 'bot' ? form.bot_id ?? null : null,
    relay_server_id: form.scope === 'relay' ? form.relay_server_id ?? null : null,
    content: form.content,
    is_active: form.is_active,
    start_at: startAt.value ? startAt.value.toISOString() : null,
    end_at: endAt.value ? endAt.value.toISOString() : null,
  }
}

async function submit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  if (startAt.value && endAt.value && endAt.value < startAt.value) {
    ElMessage.error(t('announcements.invalidWindow'))
    return
  }
  try {
    if (editingId.value) await api.update(editingId.value, payload())
    else await api.create(payload())
    ElMessage.success(t('common.saved'))
    dialogVisible.value = false
    await load()
  } catch (e) {
    fail(e)
  }
}

async function toggle(row: AnnouncementOut): Promise<void> {
  try {
    await api.toggle(row.id)
    await load()
  } catch (e) {
    fail(e)
  }
}

async function remove(row: AnnouncementOut): Promise<void> {
  try {
    await ElMessageBox.confirm(t('announcements.deleteConfirm'), t('common.delete'), {
      type: 'warning',
      confirmButtonText: t('common.confirm'),
      cancelButtonText: t('common.cancel'),
    })
  } catch {
    return
  }
  try {
    await api.remove(row.id)
    ElMessage.success(t('common.deleted'))
    await load()
  } catch (e) {
    fail(e)
  }
}

function targetOf(row: AnnouncementOut): string {
  if (row.scope === 'bot') return row.bot_name ?? row.bot_key ?? '—'
  if (row.scope === 'relay') return row.relay_name ?? '—'
  return '—'
}

// el-table-column 会先拿 `{ row: {} }` 空跑一次 default 插槽探测子列，插槽里直接 t() 动态键会收到
// undefined 并打印「Not found key」。列上的翻译一律过这两个守卫（同 ChatLogsView）。
function scopeLabel(scope: AnnouncementScope | undefined): string {
  return scope ? t(`announcements.scopes.${scope}`) : '—'
}

function statusLabel(status: AnnouncementOut['time_status'] | undefined): string {
  return status ? t(`announcements.statuses.${status}`) : '—'
}

/** 屏幕阅读器与断言用的整行摘要：目标、内容、时间状态各列都各自可见，这里只是把它们串起来。 */
function summaryOf(row: AnnouncementOut): string {
  return `${targetOf(row)} ${row.content ?? ''} ${statusLabel(row.time_status)}`
}

function statusType(status: AnnouncementOut['time_status'] | undefined): 'success' | 'info' | 'warning' {
  if (status === 'active') return 'success'
  return status === 'pending' ? 'warning' : 'info'
}

function windowOf(row: AnnouncementOut): string {
  return `${row.start_at ? formatDateTime(row.start_at) : '—'} ~ ${row.end_at ? formatDateTime(row.end_at) : '—'}`
}

defineExpose({ form, load, onScopeChange })
</script>

<template>
  <div class="announcements-view">
    <div class="page-header cm-page-header">
      <h2>{{ t('announcements.title') }}</h2>
      <p class="cm-page-intro">
        {{ t('workspace.intro.announcements') }}
      </p>
      <el-button
        type="primary"
        data-test="create-announcement"
        @click="openCreate"
      >
        {{ t('announcements.create') }}
      </el-button>
    </div>

    <LoadState
      :error="listError"
      @retry="load"
    />
    <el-table
      v-loading="loading"
      :data="rows"
      row-key="id"
    >
      <el-table-column
        :label="t('announcements.scope')"
        width="130"
      >
        <template #default="{ row }: { row: AnnouncementOut }">
          <span :data-test="'row-' + row.id">
            <el-tag disable-transitions>
              {{ scopeLabel(row.scope) }}
            </el-tag>
            <span class="sr-only">{{ summaryOf(row) }}</span>
          </span>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('announcements.target')"
        width="200"
      >
        <template #default="{ row }: { row: AnnouncementOut }">
          {{ targetOf(row) }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('announcements.content')"
        min-width="280"
        show-overflow-tooltip
      >
        <template #default="{ row }: { row: AnnouncementOut }">
          {{ row.content }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('announcements.window')"
        width="320"
      >
        <template #default="{ row }: { row: AnnouncementOut }">
          {{ windowOf(row) }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('announcements.status')"
        width="180"
      >
        <template #default="{ row }: { row: AnnouncementOut }">
          <el-tag
            :type="row.is_active ? 'success' : 'info'"
            disable-transitions
          >
            {{ row.is_active ? t('common.enabled') : t('common.disabled') }}
          </el-tag>
          <el-tag
            :type="statusType(row.time_status)"
            class="chip"
            disable-transitions
          >
            {{ statusLabel(row.time_status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('common.actions')"
        width="200"
      >
        <template #default="{ row }: { row: AnnouncementOut }">
          <el-button
            link
            type="primary"
            :data-test="'edit-' + row.id"
            @click="openEdit(row)"
          >
            {{ t('common.edit') }}
          </el-button>
          <el-button
            link
            :data-test="'toggle-' + row.id"
            @click="toggle(row)"
          >
            {{ row.is_active ? t('common.disable') : t('common.enable') }}
          </el-button>
          <el-button
            link
            type="danger"
            :data-test="'delete-' + row.id"
            @click="remove(row)"
          >
            {{ t('common.delete') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="dialogVisible"
      :close-on-click-modal="false"
      :title="editingId ? t('announcements.edit') : t('announcements.create')"
      width="640px"
    >
      <el-form
        ref="formRef"
        :model="form"
        :rules="formRules"
        label-width="120px"
        @submit.prevent
      >
        <el-form-item
          :label="t('announcements.scope')"
          prop="scope"
          data-test="form-scope"
        >
          <el-radio-group
            :model-value="form.scope"
            @update:model-value="onScopeChange($event as AnnouncementScope)"
          >
            <el-radio-button value="global">
              {{ t('announcements.scopes.global') }}
            </el-radio-button>
            <el-radio-button value="relay">
              {{ t('announcements.scopes.relay') }}
            </el-radio-button>
            <el-radio-button value="bot">
              {{ t('announcements.scopes.bot') }}
            </el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item
          v-if="form.scope === 'relay'"
          :label="t('announcements.target')"
          prop="relay_server_id"
          data-test="form-target"
        >
          <el-select
            v-model="form.relay_server_id"
            filterable
            style="width: 100%"
          >
            <el-option
              v-for="o in relayOptions"
              :key="o.id"
              :label="o.label"
              :value="o.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item
          v-if="form.scope === 'bot'"
          :label="t('announcements.target')"
          prop="bot_id"
          data-test="form-target"
        >
          <el-select
            v-model="form.bot_id"
            filterable
            style="width: 100%"
          >
            <el-option
              v-for="o in botOptions"
              :key="o.id"
              :label="o.label"
              :value="o.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item
          :label="t('announcements.content')"
          prop="content"
          data-test="form-content"
        >
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="5"
            maxlength="5000"
            show-word-limit
          />
        </el-form-item>
        <el-form-item
          v-if="form.content"
          :label="t('announcements.preview')"
          data-test="form-preview"
        >
          <pre class="preview">{{ form.content }}</pre>
        </el-form-item>
        <el-form-item
          :label="t('announcements.window')"
          data-test="form-window"
        >
          <!-- 两个独立可清空的时间点，而不是 datetimerange：只配开始（永不过期）或只配结束是合法状态。 -->
          <!-- el-date-picker 不透传 data-* 到 DOM，测试钩子只能挂在外层 span 上。 -->
          <span
            class="bound"
            data-test="form-start"
          >
            <el-date-picker
              v-model="startAt"
              type="datetime"
              clearable
              style="width: 100%"
              :aria-label="t('announcements.windowStart')"
              :placeholder="t('announcements.windowStartPlaceholder')"
            />
          </span>
          <span class="bound-sep">~</span>
          <span
            class="bound"
            data-test="form-end"
          >
            <el-date-picker
              v-model="endAt"
              type="datetime"
              clearable
              style="width: 100%"
              :aria-label="t('announcements.windowEnd')"
              :placeholder="t('announcements.windowEndPlaceholder')"
            />
          </span>
          <div class="hint">
            {{ t('announcements.windowHint') }}
          </div>
        </el-form-item>
        <el-form-item
          :label="t('announcements.active')"
          data-test="form-active"
        >
          <el-switch v-model="form.is_active" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          type="primary"
          data-test="save-announcement"
          @click="submit"
        >
          {{ t('common.save') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.chip { margin-left: 6px; }
/* 两头各占一半，中间留出分隔符；不写死宽度，弹窗窄下来也不会撑出横向滚动。 */
.bound { flex: 1; min-width: 0; }
.bound-sep { margin: 0 8px; color: var(--el-text-color-secondary); }
/* el-form-item__content 是 flex，width: 100% 让提示文案整行落到控件下面。 */
.hint { width: 100%; color: var(--el-text-color-secondary); font-size: 12px; }
.preview {
  margin: 0;
  padding: 8px 12px;
  width: 100%;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}
/* 行摘要只给读屏与断言用，视觉上不占位（各列本身已经把这些字段显示出来了）。 */
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
  border: 0;
}
</style>
