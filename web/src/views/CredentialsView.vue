<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import LoadState from '@/components/LoadState.vue'
import { useListQuery } from '@/composables/useListQuery'
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { credentials, type ApiClient, type ApiClientGroupRoute, type GroupRouteOptions, type JwtKey, type SilentWriteOption } from '@/api/infrastructure'
import { bots as botsApi } from '@/api/admin'
import { formatDateTime } from '@/utils/format'
const { t } = useI18n()
const rows = ref<ApiClient[]>([]), keys = ref<JwtKey[]>([])
const tab = ref('clients')
const page = ref(1), total = ref(0), busy = ref(false), visible = ref(false), secret = ref('')
const editing = ref<ApiClient | null>(null)
const form = reactive({ app_key: '', name: '', scopes: [] as string[], bot_ids: [] as string[], silent_writes: [] as string[], enabled: true })
// 允许静默执行的写操作：从经平台代理的业务系统目录里选，也可直接输入「系统标识.operationId」。
const silentOptions = ref<SilentWriteOption[]>([])
// `tasks` 接口组能驱动的 AI 员工：只有飞书 AI 员工能代员工下任务。
const botOptions = ref<{ id: string; label: string }[]>([])
const formRef = ref<FormInstance>()
const formSession = ref(0)
const scopes = ['relay', 'org', 'notify', 'push', 'systems', 'memories', 'escalations', 'tasks']
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
  Object.assign(form, row ? { ...row, scopes: row.scopes.filter((s) => scopes.includes(s)), bot_ids: [...(row.bot_ids ?? [])], silent_writes: [...(row.silent_writes ?? [])] } : { app_key: '', name: '', scopes: [], bot_ids: [], silent_writes: [], enabled: true })
  try { await loadBotOptions() } catch (e) { fail(e) }
  try { silentOptions.value = await credentials.silentWriteOptions() } catch (e) { fail(e) }
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
    else secret.value = (await credentials.create({ app_key: form.app_key, name: form.name, scopes: form.scopes, bot_ids: form.bot_ids, silent_writes: form.silent_writes, enabled: form.enabled })).secret
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
// 群承接：配了之后调用方驱动这个 AI 员工时以服务账号在群里跑，不再指定员工。
const groupClient = ref<ApiClient | null>(null)
const groupRows = ref<ApiClientGroupRoute[]>([])
const groupOptions = ref<GroupRouteOptions>({ bot_chats: [], alert_sources: [], service_users: [] })
// 告警来源里、但不在 AI 员工所在群列表中的群（多半是机器人已退群或群列表取不到时）。
const extraAlertChats = computed(() => {
  const known = new Set(groupOptions.value.bot_chats.map((c) => c.chat_id))
  return [...new Set(groupOptions.value.alert_sources.map((s) => s.chat_id))].filter((c) => !known.has(c))
})
const groupForm = reactive({ bot_id: '', chat_id: '', service_user_id: '' })
const groupBusy = ref(false)
async function loadBotOptions() {
  if (botOptions.value.length) return
  const data = await botsApi.list({ scope: 'all', per_page: 200 })
  botOptions.value = data.items.filter((b) => b.platform === 'feishu').map((b) => ({ id: b.id, label: `${b.name} (${b.bot_key})` }))
}
async function openGroups(row: ApiClient) {
  groupClient.value = row
  Object.assign(groupForm, { bot_id: row.bot_ids[0] ?? '', chat_id: '', service_user_id: '' })
  try {
    await loadBotOptions()
    groupRows.value = await credentials.groupRoutes(row.app_key)
    await loadGroupOptions()
  } catch (e) { fail(e) }
}
async function loadGroupOptions() {
  groupOptions.value = await credentials.groupRouteOptions(groupForm.bot_id || undefined)
}
async function onGroupBot() {
  try { await loadGroupOptions() } catch (e) { fail(e) }
}
// 选了告警来源里的群：顺带填上那条来源绑定的服务账号。
function onGroupChat(chat: string) {
  const source = groupOptions.value.alert_sources.find((s) => s.chat_id === chat)
  if (source && !groupForm.service_user_id) groupForm.service_user_id = source.service_user_id
}
function botLabel(id: string) { return botOptions.value.find((o) => o.id === id)?.label ?? id }
async function addGroup() {
  const client = groupClient.value
  if (!client || groupBusy.value) return
  groupBusy.value = true
  try {
    const row = await credentials.addGroupRoute(client.app_key, { bot_id: groupForm.bot_id, chat_id: groupForm.chat_id.trim(), service_user_id: groupForm.service_user_id })
    const chatName = row.chat_name ?? groupOptions.value.bot_chats.find((c) => c.chat_id === row.chat_id)?.name ?? null
    groupRows.value = [...groupRows.value, { ...row, chat_name: chatName }]
    groupForm.chat_id = ''
    ElMessage.success(t('common.saved'))
    await load()
  } catch (e) { fail(e) } finally { groupBusy.value = false }
}
async function removeGroup(row: ApiClientGroupRoute) {
  const client = groupClient.value
  if (!client || groupBusy.value) return
  try {
    await ElMessageBox.confirm(t('infra.groupRouteRemoveConfirm'))
    groupBusy.value = true
    await credentials.removeGroupRoute(client.app_key, row.id)
    groupRows.value = groupRows.value.filter((r) => r.id !== row.id)
    await load()
  } catch (e) { fail(e) } finally { groupBusy.value = false }
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
                v-if="row.scopes.includes('tasks')"
                link
                data-test="client-groups"
                @click="openGroups(row)"
              >
                {{ t('infra.groupRoutes') }}<template v-if="row.group_routes">
                  ({{ row.group_routes }})
                </template>
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
        <template v-if="form.scopes.includes('tasks')">
          <el-form-item :label="t('infra.taskBots')">
            <el-select
              v-model="form.bot_ids"
              multiple
              filterable
              style="width:100%"
              data-test="client-task-bots"
            >
              <el-option
                v-for="o in botOptions"
                :key="o.id"
                :value="o.id"
                :label="o.label"
              />
            </el-select>
          </el-form-item>
          <p class="field-hint">
            {{ t('infra.taskBotsHint') }}
          </p>
          <el-form-item :label="t('infra.silentWrites')">
            <el-select
              v-model="form.silent_writes"
              multiple
              filterable
              allow-create
              default-first-option
              style="width:100%"
              data-test="client-silent-writes"
            >
              <el-option
                v-for="o in silentOptions"
                :key="o.ref"
                :value="o.ref"
                :label="`${o.ref}${o.summary ? ' · ' + o.summary : ''}（${o.risk}）`"
              />
            </el-select>
          </el-form-item>
          <p class="field-hint">
            {{ t('infra.silentWritesHint') }}
          </p>
        </template>
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
      :model-value="!!groupClient"
      :close-on-click-modal="false"
      :title="`${t('infra.groupRoutes')} · ${groupClient?.name ?? ''}`"
      width="720px"
      @close="groupClient = null"
    >
      <p class="field-hint">
        {{ t('infra.groupRoutesHint') }}
      </p>
      <el-table
        :data="groupRows"
        :empty-text="t('infra.groupRouteEmpty')"
      >
        <el-table-column
          min-width="140"
          :label="t('infra.groupRouteBot')"
        >
          <template #default="{ row }">
            {{ row.bot_name ?? row.bot_key ?? row.bot_id }}
          </template>
        </el-table-column>
        <el-table-column
          min-width="160"
          :label="t('infra.groupRouteChatName')"
        >
          <template #default="{ row }">
            <template v-if="row.chat_name">
              <div>{{ row.chat_name }}</div>
              <div class="chat-id-suffix chat-id-line">
                {{ row.chat_id }}
              </div>
            </template>
            <template v-else>
              {{ row.chat_id }}
            </template>
          </template>
        </el-table-column>
        <el-table-column
          min-width="120"
          prop="service_user"
          :label="t('infra.groupRouteUser')"
        />
        <el-table-column
          width="90"
          :label="t('common.actions')"
        >
          <template #default="{ row }">
            <el-button
              link
              type="danger"
              :disabled="groupBusy"
              @click="removeGroup(row)"
            >
              {{ t('common.delete') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <p
        v-if="!groupClient?.bot_ids.length"
        class="field-hint group-add"
      >
        {{ t('infra.groupRouteNeedsBots') }}
      </p>
      <el-form
        v-else
        class="group-add"
        label-position="top"
      >
        <el-form-item :label="t('infra.groupRouteBot')">
          <el-select
            v-model="groupForm.bot_id"
            style="width:100%"
            data-test="group-bot"
            @change="onGroupBot"
          >
            <el-option
              v-for="id in groupClient?.bot_ids ?? []"
              :key="id"
              :value="id"
              :label="botLabel(id)"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('infra.groupRouteChat')">
          <el-select
            v-model="groupForm.chat_id"
            filterable
            allow-create
            default-first-option
            style="width:100%"
            :placeholder="t('infra.groupRouteChatHint')"
            data-test="group-chat"
            @change="onGroupChat"
          >
            <el-option-group
              v-if="groupOptions.bot_chats.length"
              :label="t('infra.groupRouteBotChats')"
            >
              <el-option
                v-for="c in groupOptions.bot_chats"
                :key="c.chat_id"
                :value="c.chat_id"
                :label="c.name"
              >
                <span>{{ c.name }}</span>
                <span class="chat-id-suffix">{{ c.chat_id }}</span>
              </el-option>
            </el-option-group>
            <el-option-group
              v-if="extraAlertChats.length"
              :label="t('infra.groupRouteAlertChats')"
            >
              <el-option
                v-for="chat in extraAlertChats"
                :key="chat"
                :value="chat"
                :label="chat"
              />
            </el-option-group>
          </el-select>
        </el-form-item>
        <el-form-item :label="t('infra.groupRouteUser')">
          <el-select
            v-model="groupForm.service_user_id"
            filterable
            style="width:100%"
            data-test="group-user"
          >
            <el-option
              v-for="u in groupOptions.service_users"
              :key="u.id"
              :value="u.id"
              :label="u.display_name ? `${u.display_name} (${u.login_name})` : u.login_name"
            />
          </el-select>
        </el-form-item>
        <el-button
          type="primary"
          :loading="groupBusy"
          :disabled="!groupForm.bot_id || !groupForm.chat_id.trim() || !groupForm.service_user_id"
          data-test="group-add"
          @click="addGroup"
        >
          {{ t('infra.groupRouteAdd') }}
        </el-button>
      </el-form>
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
.el-table { margin-top:16px }  .secret { margin-top:16px } .field-hint { margin: 0 0 20px; color: var(--el-text-color-secondary); font-size: 12px } .group-add { margin-top: 20px } .chat-id-suffix { margin-left: 8px; color: var(--el-text-color-secondary); font-size: 12px } .chat-id-line { margin-left: 0 }</style>
