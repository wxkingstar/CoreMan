<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import { useCompactLayout } from '@/composables/useCompactLayout'
import LoadState from '@/components/LoadState.vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { bots } from '@/api/admin'
import type { BotOut, SwitchRelayOut } from '@/api/types'
import SystemGrants from '@/components/SystemGrants.vue'
import BotHealthReport from '@/components/BotHealthReport.vue'
import BotMemories from '@/components/BotMemories.vue'
import BotSkills from '@/components/BotSkills.vue'
import BotCollaborators from '@/components/BotCollaborators.vue'
import FeishuAppPanel from '@/components/feishuApp/FeishuAppPanel.vue'
import EnvVarsEditor from '@/components/EnvVarsEditor.vue'
import BotAllowedUsersDialog from '@/components/bot/BotAllowedUsersDialog.vue'
import BotMembersDialog from '@/components/bot/BotMembersDialog.vue'
import { formatDateTime } from '@/utils/format'
import BotForm from '@/views/BotForm.vue'
import WorkspaceDrawer from '@/components/WorkspaceDrawer.vue'
import { FolderOpened } from '@element-plus/icons-vue'
import SwitchRelayDialog from '@/views/SwitchRelayDialog.vue'

const botFormRef = ref<InstanceType<typeof BotForm>>()
async function closeForm(done: () => void) { if (!botFormRef.value || await botFormRef.value.confirmDiscard()) done() }
const { t } = useI18n()
const detailTab = ref('overview')
const compact = useCompactLayout()
const detailError = ref('')
const route = useRoute()
const router = useRouter()

const botId = String(route.params.id)
const bot = ref<BotOut | null>(null)
const loading = ref(false)

const editVisible = ref(false)
const switchVisible = ref(false)
const workspaceVisible = ref(false)
const membersDialog = ref<InstanceType<typeof BotMembersDialog>>()
const allowedDialog = ref<InstanceType<typeof BotAllowedUsersDialog>>()

const perms = computed(() => bot.value?.permissions ?? null)
// 敏感字段只有 can_view_sensitive 时后端才下发；没下发就整块不渲染（而不是渲染成空）。
// env_vars_full 是另一条独立的线（can_view_env_full / AI 委员会，后端 build_out 里与 can_view_sensitive
// 无关）：不是创建者也不是协作者的委员会成员只会拿到这一个字段，所以它也要能单独把整块撑起来。
const sensitiveVisible = computed(() =>
  !!bot.value && (bot.value.system_prompt !== undefined || bot.value.credentials !== undefined
    || bot.value.env_vars !== undefined || bot.value.env_vars_full !== undefined
    ))
const credRows = computed(() => Object.entries(bot.value?.credentials ?? {}))
// 明文环境变量用普通表格而不是 EnvVarsEditor：这是只读审阅视图，值要能整段换行、能被选中复制，
// 而 el-input 里的值只是 DOM property（进不了页面文本），长串还会被输入框宽度截掉。
const envFullRows = computed(() => Object.entries(bot.value?.env_vars_full ?? {}))


function fail(e: unknown): void {
  ElMessage.error(errorMessage(e))
}

/** silent：只为刷新 member_count / allowed_user_count 时不要闪整页的加载遮罩。 */
async function reload(silent = false): Promise<void> {
  if (!silent) loading.value = true
  try {
    detailError.value = ''
    bot.value = await bots.get(botId)
  } catch (e) {
    detailError.value = errorMessage(e)
    fail(e)
  } finally {
    if (!silent) loading.value = false
  }
}

async function openMembers(): Promise<void> {
  await membersDialog.value?.open()
}

async function openAllowed(): Promise<void> {
  await allowedDialog.value?.open()
}

async function toggle(): Promise<void> {
  try {
    bot.value = await bots.toggle(botId)
  } catch (e) {
    fail(e)
    await reload()
  }
}

async function remove(): Promise<void> {
  try {
    await ElMessageBox.confirm(t('bots.detail.deleteConfirm'), t('common.delete'), {
      type: 'warning',
      confirmButtonText: t('common.confirm'),
      cancelButtonText: t('common.cancel'),
    })
  } catch {
    return
  }
  try {
    await bots.remove(botId)
    ElMessage.success(t('bots.detail.deleted'))
    await router.push({ name: 'bots' })
  } catch (e) {
    fail(e)
  }
}

function onSaved(updated: BotOut): void {
  bot.value = updated
  editVisible.value = false
}

function onSwitched(result: SwitchRelayOut): void {
  bot.value = result.bot
  // 后端可能自动降档/换模型（目标 relay 不支持原模型、xhigh / max 落到新模型支持的最高档），把结果说出来。
  if (result.old_model !== result.new_model) {
    ElMessage.info(`${t('bots.switch.model')}: ${result.old_model} → ${result.new_model}`)
  }
}


onMounted(async () => {
  await reload()
  if (route.query.action === 'switch' && bot.value?.permissions.can_switch_relay) {
    switchVisible.value = true
    // 去掉 query，否则刷新页面又弹一次。
    const rest = { ...route.query }
    delete rest.action
    await router.replace({ query: rest })
  }
})
</script>

<template>
  <div
    v-loading="loading"
    class="bot-detail"
  >
    <el-button
      class="detail-back"
      @click="router.push({ name: 'bots' })"
    >
      ← {{ t('workspace.back') }}
    </el-button>
    <LoadState
      :error="detailError"
      @retry="reload"
    />
    <template v-if="bot">
      <div class="page-header">
        <div class="title">
          <h2>{{ bot.name }}</h2>
          <span class="muted">{{ bot.bot_key }}</span>
          <el-tag size="small">
            {{ t(`platforms.${bot.platform}`) }}
          </el-tag>
          <el-tag
            size="small"
            type="info"
          >
            {{ t(`bots.backend.${bot.backend}`) }}
          </el-tag>
          <el-tag
            size="small"
            :type="bot.enabled ? 'success' : 'info'"
          >
            {{ bot.enabled ? t('common.enabled') : t('common.disabled') }}
          </el-tag>
        </div>
        <div class="actions">
          <BotHealthReport
            v-if="perms?.can_edit"
            :bot-id="botId"
          />



          <el-switch
            data-test="toggle-bot"
            :model-value="bot.enabled"
            :disabled="!perms?.can_toggle"
            @change="toggle"
          />
          <el-button
            v-if="perms?.can_edit"
            data-test="edit-bot"
            @click="editVisible = true"
          >
            {{ t('bots.edit') }}
          </el-button>



          <el-button
            v-if="perms?.can_delete"
            type="danger"
            data-test="delete-bot"
            @click="remove"
          >
            {{ t('bots.delete') }}
          </el-button>
        </div>
      </div>

      <el-tabs
        v-model="detailTab"
        class="employee-tabs"
      >
        <el-tab-pane
          name="overview"
          :label="t('workspace.overview')"
        >
          <el-alert
            :title="t('workspace.enabledHint')"
            type="info"
            :closable="false"
          />
          <el-descriptions
            id="employee-overview"
            :title="t('bots.detail.title')"
            :column="compact ? 1 : 2"
            border
          >
            <el-descriptions-item :label="t('bots.detail.relay')">
              <template v-if="bot.relay_name">
                <div>{{ bot.relay_name }}</div>
                <div class="muted">
                  {{ bot.relay_url }}
                </div>
              </template>
              <span v-else>—</span>
            </el-descriptions-item>
            <el-descriptions-item :label="t('bots.detail.model')">
              {{ bot.model }}
            </el-descriptions-item>
            <el-descriptions-item :label="t('bots.detail.workingDir')">
              <div class="workspace-path-row">
                <span class="workspace-path">{{ bot.working_dir }}</span>
                <el-button
                  v-if="perms?.can_edit"
                  :icon="FolderOpened"
                  class="workspace-open"
                  text
                  size="small"
                  type="primary"
                  @click="workspaceVisible = true"
                >
                  {{ t('workspaceFiles.open') }}
                </el-button>
              </div>
            </el-descriptions-item>
            <el-descriptions-item :label="t('bots.detail.verbosity')">
              {{ t(`bots.verbosityLevels.${bot.verbosity_level}`) }}
            </el-descriptions-item>
            <el-descriptions-item :label="t('bots.detail.effort')">
              {{ bot.effort_level ?? t('bots.effortNone') }}
            </el-descriptions-item>
            <el-descriptions-item :label="t('bots.detail.sseTimeout')">
              {{ bot.sse_timeout_seconds }}
            </el-descriptions-item>
            <el-descriptions-item :label="t('bots.detail.team')">
              {{ bot.team_name ?? '—' }}
            </el-descriptions-item>
            <el-descriptions-item :label="t('bots.detail.creator')">
              {{ bot.created_by_name ?? '—' }}
            </el-descriptions-item>
            <el-descriptions-item :label="t('bots.detail.createdAt')">
              {{ formatDateTime(bot.created_at) }}
            </el-descriptions-item>
            <el-descriptions-item :label="t('bots.detail.updatedAt')">
              {{ formatDateTime(bot.updated_at) }}
            </el-descriptions-item>
            <el-descriptions-item :label="t('bots.description')">
              {{ bot.description || '—' }}
            </el-descriptions-item>
            <el-descriptions-item :label="t('bots.detail.welcome')">
              {{ bot.welcome_message ?? '—' }}
            </el-descriptions-item>
          </el-descriptions>
        </el-tab-pane>
        <el-tab-pane
          name="capabilities"
          :label="t('workspace.capabilities')"
        >
          <section class="cm-panel">
            <h3>{{ t('workspace.capabilities') }}</h3><p class="cm-page-intro">
              {{ t('skill.installHint') }}
            </p><div class="actions">
              <BotSkills
                v-if="perms?.can_edit"
                :bot-id="botId"
                @saved="reload(true)"
              /><BotMemories
                v-if="perms?.can_edit"
                :bot-id="botId"
              />
            </div><el-empty
              v-if="!perms?.can_edit"
              :description="t('common.forbidden')"
            />
          </section>
        </el-tab-pane>
        <el-tab-pane
          name="access"
          :label="t('workspace.access')"
        >
          <section class="cm-panel">
            <h3>{{ t('workspace.access') }}</h3><p class="cm-page-intro">
              {{ t('bots.detail.membersHint') }}
            </p><div class="actions">
              <el-button
                data-test="open-members"
                @click="openMembers"
              >
                {{ t('bots.detail.members') }}（{{ bot.member_count }}）
              </el-button><el-button
                data-test="open-allowed"
                @click="openAllowed"
              >
                {{ t('bots.detail.allowedUsers') }}（{{ bot.allowed_user_count }}）
              </el-button><SystemGrants
                v-if="perms?.can_edit"
                :bot-id="botId"
                @saved="reload(true)"
              />
            </div>
          </section>
        </el-tab-pane>
        <el-tab-pane
          name="configuration"
          :label="t('workspace.configuration')"
        >
          <div class="actions configuration-actions">
            <el-button
              v-if="perms?.can_switch_relay"
              data-test="switch-bot"
              @click="switchVisible = true"
            >
              {{ t('bots.switchRelay') }}
            </el-button>
          </div>
          <template v-if="sensitiveVisible">
            <h3
              id="employee-security"
              class="cm-section-title"
            >
              {{ t('bots.detail.sensitive') }}
            </h3>
            <div
              v-if="bot.system_prompt !== undefined"
              class="block"
            >
              <div class="block-title">
                {{ t('bots.detail.systemPrompt') }}
              </div>
              <pre class="prompt">{{ bot.system_prompt || '—' }}</pre>
            </div>

            <div
              v-if="bot.credentials !== undefined"
              class="block"
            >
              <div class="block-title">
                {{ t('bots.detail.credentials') }}
              </div>
              <table class="kv">
                <tbody>
                  <tr
                    v-for="[k, v] in credRows"
                    :key="k"
                  >
                    <th>{{ k }}</th>
                    <td>{{ v }}</td>
                  </tr>
                  <tr v-if="!credRows.length">
                    <td colspan="2">
                      —
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div
              v-if="bot.env_vars !== undefined"
              class="block"
            >
              <div class="block-title">
                {{ t('bots.detail.envVars') }}
              </div>
              <EnvVarsEditor
                :model-value="bot.env_vars"
                readonly
              />
            </div>
            <div
              v-if="bot.env_vars_full !== undefined"
              class="block"
            >
              <div class="block-title">
                {{ t('bots.detail.envVarsFull') }}
              </div>
              <table class="kv">
                <tbody>
                  <tr
                    v-for="[k, v] in envFullRows"
                    :key="k"
                  >
                    <th>{{ k }}</th>
                    <td>{{ v }}</td>
                  </tr>
                  <tr v-if="!envFullRows.length">
                    <td colspan="2">
                      —
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </template>
        </el-tab-pane>
        <el-tab-pane
          v-if="bot.platform === 'feishu' && perms?.can_edit"
          name="feishu-app"
          :label="t('feishuApp.tab')"
          lazy
        >
          <FeishuAppPanel
            v-if="detailTab === 'feishu-app'"
            :bot-id="botId"
          />
        </el-tab-pane>
        <el-tab-pane
          name="collaboration"
          :label="t('collaboration.title')"
          lazy
        >
          <BotCollaborators
            v-if="bot.platform === 'feishu'"
            :bot-id="botId"
            :source-name="bot.name"
            :can-edit="!!perms?.can_edit"
            :active="detailTab === 'collaboration'"
          />
          <el-empty
            v-else
            :description="t('collaboration.platform')"
          />
        </el-tab-pane>
      </el-tabs>
      <el-dialog
        v-model="editVisible"
        :close-on-click-modal="false"
        class="employee-dialog"
        :before-close="closeForm"
        :title="t('bots.edit')"
        width="760px"
        destroy-on-close
      >
        <!-- 只在开着时实例化：BotForm 挂载时会去拉 settings/teams/relays/catalog。 -->
        <BotForm
          v-if="editVisible"
          ref="botFormRef"
          mode="edit"
          :bot="bot"
          @saved="onSaved"
          @cancel="editVisible = false"
        />
      </el-dialog>

      <WorkspaceDrawer
        v-if="workspaceVisible"
        v-model:visible="workspaceVisible"
        :bot-id="botId"
        @update:visible="!$event && reload(true)"
      />
      <SwitchRelayDialog
        v-if="switchVisible"
        :bot="bot"
        :visible="switchVisible"
        @update:visible="switchVisible = $event"
        @switched="onSwitched"
      />

      <BotMembersDialog
        ref="membersDialog"
        :bot-id="botId"
        :created-by="bot.created_by"
        :perms="perms"
        @changed="reload(true)"
      />

      <BotAllowedUsersDialog
        ref="allowedDialog"
        :bot-id="botId"
        :perms="perms"
        @changed="reload(true)"
      />
    </template>
    <el-empty v-else-if="!loading" />
  </div>
</template>

<style scoped>
.workspace-path-row { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 8px; }
.workspace-path { min-width: 0; overflow-wrap: anywhere; }
.workspace-open { flex-shrink: 0; vertical-align: middle; }

.configuration-actions { margin: 16px 0; }

.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.title h2 { margin: 0; }
.actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
h3 { margin: 20px 0 8px; }
.block { margin-bottom: 16px; }
.block-title { color: var(--el-text-color-regular); font-weight: 600; margin-bottom: 6px; }
.prompt { white-space: pre-wrap; word-break: break-word; background: var(--el-fill-color-light); padding: 10px; border-radius: 4px; margin: 0; }
.webhook { word-break: break-all; }
.kv { border-collapse: collapse; width: 100%; }
.kv th, .kv td { border: 1px solid var(--el-border-color-lighter); padding: 6px 10px; text-align: left; font-weight: normal; word-break: break-all; }
.kv th { background: var(--el-fill-color-light); width: 220px; }

</style>
