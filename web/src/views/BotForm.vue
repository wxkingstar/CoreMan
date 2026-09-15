<script setup lang="ts">
import { errorMessage, fieldErrorMap } from '@/utils/errors'
import { useUnsavedChanges } from '@/composables/useUnsavedChanges'
import { ElMessage, type FormInstance } from 'element-plus'
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { bots, catalog, relays, settings, teams as teamsApi } from '@/api/admin'
import { ApiError } from '@/api/client'
import type { BotIn, BotOut, BotPatch, CatalogOut, EffortLevel, Platform, RelayOut, TeamOut } from '@/api/types'
import EnvVarsEditor from '@/components/EnvVarsEditor.vue'
import SecretInput from '@/components/SecretInput.vue'
import { useAuthStore } from '@/stores/auth'

const props = defineProps<{ mode: 'create' | 'edit'; bot?: BotOut }>()
const emit = defineEmits<{ saved: [BotOut]; cancel: [] }>()

const { t } = useI18n()
const auth = useAuthStore()
const isManager = computed(() => ['ai_committee', 'platform_admin'].includes(auth.user?.role ?? ''))

// 与后端 coreman/core/bots/secrets.py 的 CREDENTIAL_KEYS 一一对应（顺序也一致）。
const CRED_KEYS: Record<Platform, readonly string[]> = {
  wecom: ['bot_id', 'secret'],
  feishu: ['app_id', 'app_secret', 'encrypt_key', 'verification_token'],
}
const PLATFORMS: Platform[] = ['wecom', 'feishu']
const SSE_OPTIONS = [1800, 3600, 7200, 14400, 21600, 43200]
const VERBOSITY_OPTIONS = [1, 2, 3, 4]
const EFFORT_OPTIONS: EffortLevel[] = ['low', 'medium', 'high', 'xhigh']
const BOT_KEY_RE = /^[a-z0-9][a-z0-9_-]{1,49}$/

function credDefaults(platform: Platform): Record<string, string> {
  return Object.fromEntries(CRED_KEYS[platform].map((k) => [k, '']))
}

function emptyForm(): BotIn {
  return {
    bot_key: '',
    platform: 'wecom',
    name: '',
    description: '',
    avatar_url: null,
    team_id: auth.user?.team_id ?? null,
    relay_server_id: null,
    model: '',
    working_dir: '',
    system_prompt: '',
    verbosity_level: 1,
    effort_level: null,
    sse_timeout_seconds: 3600,
    credentials: credDefaults('wecom'),
    env_vars: {},
    welcome_message: null,
    enabled: true,
  }
}

const form = reactive<BotIn>(emptyForm())
const formRef = ref<FormInstance>()
const fieldErrors = reactive<Record<string, string>>({})
const saving = ref(false)
let originalForm = ''
const { confirmDiscard } = useUnsavedChanges(() => !!originalForm && originalForm !== JSON.stringify(form))
async function cancel() { if (await confirmDiscard()) emit('cancel') }
const relayList = ref<RelayOut[]>([])
const runtimeGroups = computed(() => [...new Map(relayList.value.filter(r => r.runtime_node_id).map(r => [r.runtime_node_id!, { id: r.runtime_node_id!, name: r.runtime_name ?? r.name }])).values()])
const selectedRuntime = computed(() => relayList.value.find(r => r.id === form.relay_server_id)?.runtime_node_id ?? null)
const runtimeBackends = computed(() => relayList.value.filter(r => r.runtime_node_id === selectedRuntime.value))
async function selectRuntime(id: string | null) {
  const providers = relayList.value.filter(r => r.runtime_node_id === id)
  const picked = providers.find(r => r.effective_models.length) ?? providers[0]
  await selectRelay(picked?.id ?? null)
}
const catalogRows = ref<CatalogOut[]>([])
const teamList = ref<TeamOut[]>([])
/** 选了 relay 时是该 relay 的有效模型集；未选时为 null，走目录全集。 */
const relayModels = ref<string[] | null>(null)
/** 上一次由 bot_key 自动填进 working_dir 的值：用户手改过就不再联动。 */
const autoWorkingDir = ref('')

const credKeys = computed(() => CRED_KEYS[form.platform])
// 敏感字段（提示词/凭证/环境变量）只有 can_view_sensitive 时后端才下发；没下发就别渲染，
// 否则空值会在编辑提交时把线上配置清掉。
const sensitiveVisible = computed(() => props.mode === 'create' || props.bot?.credentials !== undefined)
const modelOptions = computed<string[]>(() =>
  relayModels.value ?? [...new Set(catalogRows.value.filter((r) => !r.retired).map((r) => r.model))])
// 后端 supports_xhigh() 的写法：目录里任一行匹配即可（同一 model 可挂在多个 provider 下）。
const xhighAllowed = computed(() => catalogRows.value.some((r) => r.model === form.model && r.supports_xhigh))

watch(xhighAllowed, (ok) => {
  if (!ok && form.effort_level === 'xhigh') form.effort_level = 'high'
})

/** 后端 422 明细里的字段名 → 表单标签：提示里用人话，表单项上就地标红。 */
const fieldLabels = computed<Record<string, string>>(() => ({
  bot_key: t('bots.botKey'), platform: t('bots.platform'), name: t('bots.name'), description: t('bots.description'),
  team_id: t('bots.team'), relay_server_id: t('bots.relay'), model: t('bots.model'), effort_level: t('bots.effort'),
  verbosity_level: t('bots.verbosity'), working_dir: t('bots.workingDir'), sse_timeout_seconds: t('bots.sseTimeout'),
  system_prompt: t('bots.systemPrompt'), credentials: t('bots.credentials'), env_vars: t('bots.envVars'),
  welcome_message: t('bots.welcome'),
}))

function fail(e: unknown): void {
  ElMessage.error(errorMessage(e, fieldLabels.value))
}

/** 选 relay：换成该 relay 的有效模型集；当前模型不在里面就落到 relay 的默认模型。 */
async function selectRelay(id: string | null, keepModel = false): Promise<void> {
  form.relay_server_id = id
  const selected = relayList.value.find(r => r.id === id)
  if (!props.bot && selected?.workspace_root) {
    autoWorkingDir.value = `${selected.workspace_root}/${form.bot_key || 'project'}`
    form.working_dir = autoWorkingDir.value
  }
  if (!id) {
    relayModels.value = null
    return
  }
  try {
    const res = await relays.models(id)
    relayModels.value = res.models
    if (!keepModel && !res.models.includes(form.model)) form.model = res.default ?? res.models[0] ?? form.model
  } catch (e) {
    // 机器人绑在一台自己看不见的 relay 上（visibility=admins，此时 bot.relay_url 也是 null）时
    // /models 就是 404：这不是错误，回落到目录全集，不要弹红条。
    if (e instanceof ApiError && e.status === 404) relayModels.value = null
    else fail(e)
  }
}

function onBotKeyInput(): void {
  if (props.mode !== 'create') return
  // 空着，或还是上一次自动填的值，才继续联动；手改过就停。
  if (form.working_dir && form.working_dir !== autoWorkingDir.value) return
  const root = relayList.value.find(r => r.id === form.relay_server_id)?.workspace_root ?? '/data/skills'
  autoWorkingDir.value = form.bot_key ? `${root}/${form.bot_key}` : ''
  form.working_dir = autoWorkingDir.value
}

function onPlatformChange(): void {
  form.credentials = credDefaults(form.platform)
}

function onEnvInvalid(message: string): void {
  ElMessage.warning(message)
}

function fillFromBot(b: BotOut): void {
  Object.assign(form, {
    bot_key: b.bot_key,
    platform: b.platform,
    name: b.name,
    description: b.description,
    avatar_url: b.avatar_url,
    team_id: b.team_id,
    relay_server_id: b.relay_server_id,
    model: b.model,
    working_dir: b.working_dir,
    system_prompt: b.system_prompt ?? '',
    verbosity_level: b.verbosity_level,
    effort_level: b.effort_level,
    sse_timeout_seconds: b.sse_timeout_seconds,
    credentials: { ...credDefaults(b.platform), ...(b.credentials ?? {}) },
    env_vars: { ...(b.env_vars ?? {}) },
    welcome_message: b.welcome_message,
    // 后端只在 can_view_sensitive 时下发（且是脱敏值）；没下发就按空处理。
    enabled: b.enabled,
  } satisfies BotIn)
  // 编辑态不能换 relay（PATCH 带 relay_server_id 后端直接 409），只把模型选项load出来。
  if (b.relay_server_id) void selectRelay(b.relay_server_id, true)
}

/** 空值的凭证键不提交：编辑时「键缺失 = 删除」，正好用来清掉可选凭证。 */
function credentialsPayload(): Record<string, string> {
  return Object.fromEntries(
    credKeys.value.map((k) => [k, form.credentials[k] ?? '']).filter(([, v]) => v !== ''),
  )
}

function payload(): BotIn {
  return {
    ...form,
    credentials: credentialsPayload(),
    env_vars: { ...form.env_vars },
  }
}

function sameDict(a: Record<string, string>, b: Record<string, string>): boolean {
  const ka = Object.keys(a).sort()
  const kb = Object.keys(b).sort()
  return ka.length === kb.length && ka.every((k, i) => k === kb[i] && a[k] === b[k])
}

/** 只挑真正改过的字段：脱敏值原样带回去，后端按「未修改」处理。 */
function changedFields(b: BotOut): BotPatch {
  const p = payload()
  const out: BotPatch = {}
  if (p.name !== b.name) out.name = p.name
  if (p.description !== b.description) out.description = p.description
  if (p.avatar_url !== b.avatar_url) out.avatar_url = p.avatar_url
  if (isManager.value && p.team_id !== b.team_id) out.team_id = p.team_id
  if (p.model !== b.model) out.model = p.model
  if (p.working_dir !== b.working_dir) out.working_dir = p.working_dir
  if (p.verbosity_level !== b.verbosity_level) out.verbosity_level = p.verbosity_level
  if (p.effort_level !== b.effort_level) out.effort_level = p.effort_level
  if (p.sse_timeout_seconds !== b.sse_timeout_seconds) out.sse_timeout_seconds = p.sse_timeout_seconds
  if (p.welcome_message !== b.welcome_message) out.welcome_message = p.welcome_message
  // 后端没下发的敏感字段一律不比：表单里是空值，提交就等于清空线上配置。
  if (b.system_prompt !== undefined && p.system_prompt !== b.system_prompt) out.system_prompt = p.system_prompt
  if (b.credentials !== undefined && !sameDict(p.credentials, b.credentials)) out.credentials = p.credentials
  if (b.env_vars !== undefined && !sameDict(p.env_vars, b.env_vars)) out.env_vars = p.env_vars
  return out
}

function validate(): boolean {
  for (const key of Object.keys(fieldErrors)) delete fieldErrors[key]
  if (props.mode === 'create' && !BOT_KEY_RE.test(form.bot_key)) fieldErrors.bot_key = t('bots.botKeyHint')
  for (const [key, label, value] of [['name', 'bots.name', form.name], ['model', 'bots.model', form.model], ['working_dir', 'bots.workingDir', form.working_dir]] as const) {
    if (!String(value).trim()) fieldErrors[key] = t('login.required', { field: t(label) })
  }
  const first = Object.keys(fieldErrors)[0]
  if (!first) return true
  ElMessage.error(fieldErrors[first])
  void nextTick(() => { const element = formRef.value?.$el.querySelector(`[data-test="${first}"] input`); element?.focus() })
  return false
}

async function submit(): Promise<void> {
  if (saving.value || !validate()) return
  saving.value = true
  try {
    if (props.mode === 'create') {
      const created = await bots.create(payload())
      ElMessage.success(t('bots.created'))
      originalForm = JSON.stringify(form)
      emit('saved', created)
      return
    }
    const b = props.bot
    if (!b) return
    const changed = changedFields(b)
    if (!Object.keys(changed).length) {
      ElMessage.info(t('bots.noChanges'))
      emit('cancel')
      return
    }
    const updated = await bots.patch(b.id, changed, b.version)
    ElMessage.success(t('bots.saved'))
    originalForm = JSON.stringify(form)
    emit('saved', updated)
  } catch (e) {
    // 编辑冲突（If-Match 不匹配）用统一文案；其它（422 校验、创建时 bot_key 重复）直接给后端原话。
    if (props.mode === 'edit' && e instanceof ApiError && e.status === 409) ElMessage.warning(t('common.conflict'))
    else {
      Object.assign(fieldErrors, fieldErrorMap(e, fieldLabels.value))
      fail(e)
    }
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  try {
    const [relayPage, rows] = await Promise.all([relays.list({ is_active: true, per_page: 200 }), catalog.list()])
    relayList.value = relayPage.items
    catalogRows.value = rows
  } catch (e) {
    fail(e)
  }
  if (isManager.value) {
    try {
      teamList.value = await teamsApi.list()
    } catch (e) {
      fail(e)
    }
  }
  if (props.mode === 'edit') {
    if (props.bot) fillFromBot(props.bot)
    originalForm = JSON.stringify(form)
    return
  }
  try {
    const d = await settings.defaults()
    form.model = d.default_model
    form.verbosity_level = d.default_verbosity_level
    form.effort_level = d.default_effort_level
  } catch (e) {
    fail(e)
  }
  originalForm = JSON.stringify(form)
})

defineExpose({ form, selectRelay, modelOptions, confirmDiscard })
</script>

<template>
  <el-form
    ref="formRef"
    :model="form"
    label-width="150px"
    class="employee-form"
    @submit.prevent
  >
    <div class="employee-form-fields">
      <nav
        class="cm-section-nav"
        :aria-label="t('workspace.sections')"
      >
        <a href="#form-identity">01 {{ t('workspace.identity') }}</a><a href="#form-runtime">02 {{ t('workspace.configuration') }}</a><a
          v-if="sensitiveVisible"
          href="#form-security"
        >03 {{ t('workspace.security') }}</a><a href="#form-experience">04 {{ t('workspace.experience') }}</a>
      </nav>
      <h3
        id="form-identity"
        class="cm-section-title"
      >
        <span>01</span>{{ t('workspace.identity') }}
      </h3>
      <el-form-item
        :label="t('bots.botKey')"
        data-test="bot_key"
        :error="fieldErrors.bot_key"
      >
        <el-input
          v-model="form.bot_key"
          :disabled="mode === 'edit'"
          @input="onBotKeyInput"
        />
        <div class="muted">
          {{ t('bots.botKeyHint') }}
        </div>
      </el-form-item>

      <el-form-item
        :label="t('bots.platform')"
        data-test="platform"
        :error="fieldErrors.platform"
      >
        <el-select
          v-model="form.platform"
          :disabled="mode === 'edit'"
          style="width: 200px"
          @change="onPlatformChange"
        >
          <el-option
            v-for="p in PLATFORMS"
            :key="p"
            :label="t(`platforms.${p}`)"
            :value="p"
          />
        </el-select>
      </el-form-item>

      <el-form-item
        :label="t('bots.name')"
        data-test="name"
        :error="fieldErrors.name"
      >
        <el-input v-model="form.name" />
      </el-form-item>

      <el-form-item
        :label="t('bots.description')"
        data-test="description"
        :error="fieldErrors.description"
      >
        <el-input
          v-model="form.description"
          type="textarea"
          :rows="2"
        />
      </el-form-item>

      <el-form-item
        v-if="isManager"
        :label="t('bots.team')"
        data-test="team"
        :error="fieldErrors.team_id"
      >
        <el-select
          :model-value="form.team_id"
          clearable
          :placeholder="t('bots.team')"
          style="width: 240px"
          @update:model-value="form.team_id = ($event as string | undefined) ?? null"
        >
          <el-option
            v-for="tm in teamList"
            :key="tm.id"
            :label="tm.name_zh"
            :value="tm.id"
          />
        </el-select>
      </el-form-item>

      <h3
        id="form-runtime"
        class="cm-section-title"
      >
        <span>02</span>{{ t('workspace.configuration') }}
      </h3>
      <el-form-item
        :label="t('bots.relay')"
        data-test="relay"
        :error="fieldErrors.relay_server_id"
      >
        <template v-if="props.mode === 'create' && runtimeGroups.length">
          <el-select
            :model-value="selectedRuntime"
            clearable
            :placeholder="t('runtimeNodes.nodes')"
            @update:model-value="selectRuntime($event as string | null)"
          >
            <el-option
              v-for="node in runtimeGroups"
              :key="node.id"
              :label="node.name"
              :value="node.id"
            />
          </el-select>
          <el-select
            :model-value="form.relay_server_id"
            :placeholder="t('runtimeNodes.aiType')"
            style="margin-left: 10px"
            @update:model-value="selectRelay($event as string)"
          >
            <el-option
              v-for="backend in runtimeBackends"
              :key="backend.id"
              :value="backend.id"
              :label="backend.model_provider === 'claude' ? 'Claude Code' : 'Codex / GPT'"
              :disabled="!backend.effective_models.length"
            />
          </el-select>
        </template>
        <el-select
          v-else
          :model-value="form.relay_server_id"
          clearable
          :disabled="mode === 'edit'"
          :placeholder="t('bots.noRelay')"
          style="width: 320px"
          @update:model-value="selectRelay(($event as string) ?? null)"
        >
          <el-option
            v-for="r in relayList"
            :key="r.id"
            :label="`${r.name} · ${r.model_provider} · ${r.team_name ?? t('bots.publicPool')}`"
            :value="r.id"
          />
        </el-select>
        <div
          v-if="mode === 'edit'"
          class="muted"
        >
          {{ t('bots.relayLocked') }}
        </div>
      </el-form-item>

      <el-form-item
        :label="t('bots.model')"
        data-test="model"
        :error="fieldErrors.model"
      >
        <el-select
          v-model="form.model"
          filterable
          style="width: 320px"
        >
          <el-option
            v-for="m in modelOptions"
            :key="m"
            :label="m"
            :value="m"
          />
        </el-select>
      </el-form-item>

      <el-form-item
        :label="t('bots.effort')"
        data-test="effort"
        :error="fieldErrors.effort_level"
      >
        <el-select
          :model-value="form.effort_level"
          clearable
          :placeholder="t('bots.effortNone')"
          style="width: 200px"
          @update:model-value="form.effort_level = ($event as EffortLevel) ?? null"
        >
          <el-option
            v-for="lv in EFFORT_OPTIONS"
            :key="lv"
            :label="lv"
            :value="lv"
            :disabled="lv === 'xhigh' && !xhighAllowed"
          />
        </el-select>
      </el-form-item>

      <el-form-item
        :label="t('bots.verbosity')"
        data-test="verbosity"
        :error="fieldErrors.verbosity_level"
      >
        <el-select
          v-model="form.verbosity_level"
          style="width: 200px"
        >
          <el-option
            v-for="lv in VERBOSITY_OPTIONS"
            :key="lv"
            :label="t(`bots.verbosityLevels.${lv}`)"
            :value="lv"
          />
        </el-select>
      </el-form-item>

      <el-form-item
        :label="t('bots.workingDir')"
        data-test="working_dir"
        :error="fieldErrors.working_dir"
      >
        <el-input v-model="form.working_dir" />
      </el-form-item>

      <el-form-item
        :label="t('bots.sseTimeout')"
        data-test="sse_timeout"
        :error="fieldErrors.sse_timeout_seconds"
      >
        <el-select
          v-model="form.sse_timeout_seconds"
          style="width: 200px"
        >
          <el-option
            v-for="s in SSE_OPTIONS"
            :key="s"
            :label="String(s)"
            :value="s"
          />
        </el-select>
      </el-form-item>

      <h3
        v-if="sensitiveVisible"
        id="form-security"
        class="cm-section-title"
      >
        <span>03</span>{{ t('workspace.security') }}
      </h3>
      <el-form-item
        v-if="sensitiveVisible"
        :label="t('bots.systemPrompt')"
        data-test="system_prompt"
        :error="fieldErrors.system_prompt"
      >
        <el-input
          v-model="form.system_prompt"
          type="textarea"
          :rows="4"
        />
      </el-form-item>

      <template v-if="sensitiveVisible">
        <el-form-item
          :label="t('bots.credentials')"
          class="section"
          :error="fieldErrors.credentials"
        />
        <el-form-item
          v-for="k in credKeys"
          :key="k"
          :label="t(`bots.cred.${k}`)"
        >
          <SecretInput
            :model-value="form.credentials[k] ?? ''"
            :data-test="'cred-' + k"
            :start-editing="mode === 'create'"
            :placeholder="t(`bots.cred.${k}`)"
            @update:model-value="form.credentials[k] = $event ?? ''"
          />
        </el-form-item>

        <el-form-item
          :label="t('bots.envVars')"
          data-test="env_vars"
          :error="fieldErrors.env_vars"
        >
          <EnvVarsEditor
            :model-value="form.env_vars"
            @update:model-value="form.env_vars = $event"
            @invalid="onEnvInvalid"
          />
        </el-form-item>
      </template>

      <h3
        id="form-experience"
        class="cm-section-title"
      >
        <span>04</span>{{ t('workspace.experience') }}
      </h3>
      <el-form-item
        :label="t('bots.welcome')"
        data-test="welcome"
        :error="fieldErrors.welcome_message"
      >
        <el-input
          :model-value="form.welcome_message ?? ''"
          type="textarea"
          :rows="2"
          @update:model-value="form.welcome_message = ($event as string) || null"
        />
      </el-form-item>

      <el-form-item
        v-if="mode === 'create'"
        :label="t('bots.enabled')"
        data-test="enabled"
      >
        <el-switch v-model="form.enabled" />
      </el-form-item>
    </div>
    <el-form-item class="form-footer">
      <el-button
        data-test="cancel"
        @click="cancel"
      >
        {{ t('common.cancel') }}
      </el-button>
      <el-button
        type="primary"
        :loading="saving"
        data-test="submit"
        @click="submit"
      >
        {{ t('common.save') }}
      </el-button>
    </el-form-item>
  </el-form>
</template>

<style scoped>
.muted { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
.section { font-weight: 600; }
</style>
