<script setup lang="ts">
import { errorMessage, fieldErrorMap, isVersionConflict } from '@/utils/errors'
import { useUnsavedChanges } from '@/composables/useUnsavedChanges'
import { ElMessage, type FormInstance } from 'element-plus'
import { computed, nextTick, onMounted, provide, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { bots, catalog, relays, settings, teams as teamsApi } from '@/api/admin'
import { ApiError } from '@/api/client'
import type { BotIn, BotOut, BotPatch, CatalogOut, Platform, RelayOut, TeamOut } from '@/api/types'
import type { FeishuRegistration } from '@/api/feishuApps'
import BotFormExperience from '@/components/botForm/BotFormExperience.vue'
import BotFormIdentity from '@/components/botForm/BotFormIdentity.vue'
import BotFormRuntime from '@/components/botForm/BotFormRuntime.vue'
import BotFormSecurity from '@/components/botForm/BotFormSecurity.vue'
import { botFormKey } from '@/components/botForm/context'
import FeishuRegistrationDialog from '@/components/feishuApp/FeishuRegistrationDialog.vue'
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

const BOT_KEY_RE = /^[a-z0-9][a-z0-9_-]{1,49}$/

function credDefaults(platform: Platform): Record<string, string> {
  return Object.fromEntries(CRED_KEYS[platform].map((k) => [k, '']))
}

/** 新建时直接展示的必填项；其余字段收在「更多设置」里，出错时自动展开。 */
const ESSENTIAL_FIELDS = new Set(['bot_key', 'name', 'relay_server_id'])

function emptyForm(): BotIn {
  return {
    bot_key: '',
    platform: 'feishu',
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
    credentials: credDefaults('feishu'),
    env_vars: {},
    welcome_message: null,
    enabled: true,
  }
}

const form = reactive<BotIn>(emptyForm())
const formRef = ref<FormInstance>()
const fieldErrors = reactive<Record<string, string>>({})
const saving = ref(false)
const moreOpen = ref<string[]>([])
const manualCredentials = ref(false)
/** 新建飞书员工默认扫码创建应用：凭证不在表单里填，由扫码会话交付给后端。 */
const oneClickFeishu = computed(() => props.mode === 'create' && form.platform === 'feishu' && !manualCredentials.value)
const registrationVisible = ref(false)
const creatingFromRegistration = ref(false)
/** If-Match 用的版本号：版本冲突后会刷新成最新值，表单内容保留。 */
const version = ref(props.bot?.version ?? 0)
let originalForm = ''
const { confirmDiscard } = useUnsavedChanges(() => !!originalForm && originalForm !== JSON.stringify(form))
async function cancel() { if (await confirmDiscard()) emit('cancel') }
const relayList = ref<RelayOut[]>([])
const runtimeGroups = computed(() => [...new Map(relayList.value.filter(r => r.runtime_node_id).map(r => [r.runtime_node_id!, { id: r.runtime_node_id!, name: r.runtime_name ?? r.name }])).values()])
const chosenRuntime = ref<string | null>(null)
const selectedRuntime = computed(() => chosenRuntime.value ?? relayList.value.find(r => r.id === form.relay_server_id)?.runtime_node_id ?? null)
const runtimeBackends = computed(() => relayList.value.filter(r => r.runtime_node_id === selectedRuntime.value))
async function selectRuntime(id: string | null) {
  chosenRuntime.value = id
  const providers = relayList.value.filter(r => r.runtime_node_id === id)
  const picked = providers.find(r => r.unavailable_reason === null && r.effective_models.length)
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
  if (props.mode === 'create' && id && relayList.value.find(r => r.id === id)?.unavailable_reason !== null) return
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
  const root = relayList.value.find(r => r.id === form.relay_server_id)?.workspace_root ?? '/home/ai'
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
  if (oneClickFeishu.value) return {}
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

/** 定位第一个出错字段：新建时它可能收在「更多设置」里，先展开再聚焦。 */
function revealField(first: string): void {
  if (props.mode === 'create' && !ESSENTIAL_FIELDS.has(first)) moreOpen.value = ['more']
  const testId = first === 'relay_server_id' ? 'relay' : first
  void nextTick(() => { const element = formRef.value?.$el.querySelector(`[data-test="${testId}"] input`); element?.focus() })
}

function validate(): boolean {
  for (const key of Object.keys(fieldErrors)) delete fieldErrors[key]
  if (props.mode === 'create' && !BOT_KEY_RE.test(form.bot_key)) fieldErrors.bot_key = t('bots.botKeyHint')
  for (const [key, label, value] of [['name', 'bots.name', form.name], ['model', 'bots.model', form.model], ['working_dir', 'bots.workingDir', form.working_dir]] as const) {
    if (!String(value).trim()) fieldErrors[key] = t('login.required', { field: t(label) })
  }
  if (props.mode === 'create' && !form.relay_server_id) fieldErrors.relay_server_id = t('login.required', { field: t('bots.relay') })
  const first = Object.keys(fieldErrors).sort((a, b) => Number(!ESSENTIAL_FIELDS.has(a)) - Number(!ESSENTIAL_FIELDS.has(b)))[0]
  if (!first) return true
  ElMessage.error(fieldErrors[first])
  revealField(first)
  return false
}

function failWithFields(e: unknown): void {
  Object.assign(fieldErrors, fieldErrorMap(e, fieldLabels.value))
  fail(e)
  const first = Object.keys(fieldErrors)[0]
  if (first) revealField(first)
}

/** 扫码成功后用服务端暂存的凭证创建员工；失败时应用仍可在下次扫码时复用，不会重复建应用。 */
async function onRegistered(registration: FeishuRegistration): Promise<void> {
  creatingFromRegistration.value = true
  try {
    const created = await bots.create({ ...payload(), credentials: {}, feishu_registration_id: registration.id })
    ElMessage.success(t('bots.created'))
    originalForm = JSON.stringify(form)
    registrationVisible.value = false
    emit('saved', created)
  } catch (e) {
    registrationVisible.value = false
    failWithFields(e)
  } finally {
    creatingFromRegistration.value = false
  }
}

async function submit(): Promise<void> {
  if (saving.value || !validate()) return
  saving.value = true
  try {
    if (oneClickFeishu.value) {
      // 先确认标识、工作目录等都可用，再让用户扫码，避免飞书侧留下建好却用不上的应用。
      await bots.validate(payload())
      registrationVisible.value = true
      return
    }
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
    const updated = await bots.patch(b.id, changed, version.value)
    ElMessage.success(t('bots.saved'))
    originalForm = JSON.stringify(form)
    emit('saved', updated)
  } catch (e) {
    // 只有乐观锁版本冲突才用统一文案；其它 409（工作目录被占用、飞书应用已分配）与 422 直接给后端原话。
    if (props.mode === 'edit' && isVersionConflict(e)) await reloadVersion()
    else failWithFields(e)
  } finally {
    saving.value = false
  }
}

/**
 * 版本冲突后只刷新版本号：changedFields 仍以打开表单时的快照为基准，再次保存只提交用户改过的字段，
 * 不会把别人刚改的其它字段用旧值覆盖回去。
 */
async function reloadVersion(): Promise<void> {
  ElMessage.warning(t('common.conflictReloaded'))
  if (!props.bot) return
  try {
    version.value = (await bots.get(props.bot.id)).version
  } catch (e) {
    fail(e)
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
    // 模型目录为空或全部退役时没有默认模型（null）：保持空，不覆盖已有值，交给必填校验提示。
    if (d.default_model) form.model = d.default_model
    form.verbosity_level = d.default_verbosity_level
    form.effort_level = d.default_effort_level
  } catch (e) {
    fail(e)
  }
  originalForm = JSON.stringify(form)
})

// 身份、运行配置、凭据与环境三个分区是子组件，共享这里维护的同一份表单状态与联动逻辑。
provide(botFormKey, {
  mode: props.mode, botId: props.bot?.id, form, fieldErrors, isManager, teamList, relayList, runtimeGroups, selectedRuntime, runtimeBackends,
  modelOptions, xhighAllowed, sensitiveVisible, credKeys, manualCredentials, onBotKeyInput, onPlatformChange, onEnvInvalid, selectRuntime, selectRelay,
})

defineExpose({ form, selectRelay, modelOptions, confirmDiscard, moreOpen })
</script>

<template>
  <el-form
    ref="formRef"
    :model="form"
    label-width="150px"
    class="employee-form"
    @submit.prevent
  >
    <div
      v-if="mode === 'create'"
      class="employee-form-fields"
    >
      <BotFormIdentity part="essential" />
      <BotFormRuntime part="essential" />
      <el-form-item
        v-if="oneClickFeishu"
        :label="t('feishuApp.bot')"
        data-test="feishu-one-click"
      >
        <div class="muted">
          {{ t('feishuApp.oneClickHint') }}
        </div>
      </el-form-item>
      <el-collapse
        v-model="moreOpen"
        class="more-settings"
      >
        <el-collapse-item
          name="more"
          :title="t('bots.moreSettings')"
          data-test="more-settings"
        >
          <BotFormIdentity part="extra" />
          <BotFormRuntime part="extra" />
          <BotFormSecurity part="extra" />
          <BotFormExperience part="extra" />
        </el-collapse-item>
      </el-collapse>
    </div>
    <div
      v-else
      class="employee-form-fields"
    >
      <nav
        class="cm-section-nav"
        :aria-label="t('workspace.sections')"
      >
        <a href="#form-identity">01 {{ t('workspace.identity') }}</a><a href="#form-runtime">02 {{ t('workspace.configuration') }}</a><a
          v-if="sensitiveVisible"
          href="#form-security"
        >03 {{ t('workspace.security') }}</a><a href="#form-experience">04 {{ t('workspace.experience') }}</a>
      </nav>
      <BotFormIdentity />

      <BotFormRuntime />

      <BotFormSecurity />

      <BotFormExperience />
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
        {{ oneClickFeishu ? t('feishuApp.scanAndCreate') : t('common.save') }}
      </el-button>
    </el-form-item>
    <FeishuRegistrationDialog
      v-if="oneClickFeishu"
      v-model:visible="registrationVisible"
      purpose="create"
      :preset="{ name: form.name, description: form.description, avatar_url: form.avatar_url }"
      :busy="creatingFromRegistration"
      @succeeded="onRegistered"
    />
  </el-form>
</template>

<style scoped>
.muted { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
.more-settings { margin: 4px 0 16px; }
</style>
