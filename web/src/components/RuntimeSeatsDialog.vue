<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { runtimeSeats, type RuntimeNode, type SeatLogin, type SeatState } from '@/api/runtimeNodes'
import ProviderBadge from '@/components/ProviderBadge.vue'
import QuotaBar from '@/components/QuotaBar.vue'
import { errorMessage } from '@/utils/errors'
import { pctNum } from '@/utils/format'

/** 一台运行时上各 AI 的席位（登录的账号）：添加、登录、切换当前席位、移除。只给平台管理员。 */
const props = defineProps<{ node: RuntimeNode; visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [boolean]; changed: [] }>()
const { t } = useI18n()

const PROVIDERS = ['claude', 'codex'] as const
const seats = reactive<Record<string, SeatState[]>>({})
const labels = reactive<Record<string, string>>({ claude: '', codex: '' })
const busy = ref('')
watch(() => [props.visible, props.node] as const, () => {
  if (!props.visible) return
  for (const provider of PROVIDERS) seats[provider] = [...(props.node.capabilities[provider]?.seats ?? [])]
}, { immediate: true })

const installed = (provider: string) => props.node.capabilities[provider]?.installed === true
const supported = (provider: string) => props.node.capabilities[provider]?.seat_switch_v1 === true
const seatName = (seat: SeatState) => seat.label || t('runtimeSeats.defaultSeat')

/** 当前席位用实例上的额度；其它席位用切走时存下的额度。 */
function quotaWindows(provider: string, seat: SeatState) {
  const backend = props.node.backends.find(b => b.model_provider === provider)
  if (!backend || seat.login === 'required') return []
  const source = seat.active ? backend : backend.seat_quotas?.[seat.id]
  if (!source) return []
  return [
    { label: '5h', pct: source.rate_limit_5h_used_pct, resetsAt: source.rate_limit_5h_resets_at },
    { label: '7d', pct: source.rate_limit_7d_used_pct, resetsAt: source.rate_limit_7d_resets_at },
  ].filter(w => pctNum(w.pct) !== null)
}

async function run(key: string, action: () => Promise<{ seats: SeatState[] }>, provider: string, done?: string) {
  busy.value = key
  try {
    seats[provider] = (await action()).seats
    if (done) ElMessage.success(done)
    emit('changed')
    return true
  } catch (error) {
    ElMessage.error(errorMessage(error))
    return false
  } finally {
    busy.value = ''
  }
}

async function add(provider: string) {
  const label = labels[provider].trim()
  if (!label) return
  const ok = await run(`add-${provider}`, () => runtimeSeats.create(props.node.id, provider, label), provider, t('runtimeSeats.created'))
  if (ok) labels[provider] = ''
}

async function activate(provider: string, seat: SeatState) {
  try {
    await ElMessageBox.confirm(t('runtimeSeats.activateConfirm', { provider: t(`bots.backend.${provider}`), name: seatName(seat) }), t('runtimeSeats.activate'), { type: 'warning' })
  } catch { return }
  await run(`activate-${seat.id}`, () => runtimeSeats.activate(props.node.id, provider, seat.id), provider, t('runtimeSeats.activated'))
}

async function remove(provider: string, seat: SeatState) {
  try {
    await ElMessageBox.confirm(t('runtimeSeats.removeConfirm', { name: seatName(seat) }), t('runtimeSeats.remove'), { type: 'warning' })
  } catch { return }
  await run(`remove-${seat.id}`, () => runtimeSeats.remove(props.node.id, provider, seat.id), provider, t('runtimeSeats.removed'))
}

const login = reactive({ visible: false, provider: '', seat: null as SeatState | null, data: null as SeatLogin | null, code: '' })
const loginMinutes = computed(() => Math.round((login.data?.expires_in ?? 0) / 60))
async function startLogin(provider: string, seat: SeatState) {
  busy.value = `login-${seat.id}`
  try {
    Object.assign(login, { provider, seat, code: '', data: await runtimeSeats.startLogin(props.node.id, provider, seat.id), visible: true })
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    busy.value = ''
  }
}
async function finishLogin() {
  if (!login.data || !login.seat) return
  if (login.data.needs_code && !login.code.trim()) return
  const { provider, seat, data } = login
  const ok = await run('finish', () => runtimeSeats.completeLogin(props.node.id, provider, seat.id, {
    session_id: data.session_id, ...(data.needs_code ? { code: login.code.trim() } : {}),
  }), provider, t('runtimeSeats.loggedIn'))
  if (ok) login.visible = false
}
</script>

<template>
  <el-dialog
    :model-value="visible"
    :title="t('runtimeSeats.title', { name: node.name })"
    width="960px"
    @update:model-value="emit('update:visible', $event)"
  >
    <p class="muted">
      {{ t('runtimeSeats.hint') }}
    </p>
    <section
      v-for="provider in PROVIDERS"
      :key="provider"
      class="seat-provider"
      :data-test="`seats-${provider}`"
    >
      <h4><ProviderBadge :provider="provider" /></h4>
      <p
        v-if="!installed(provider)"
        class="muted"
      >
        {{ t('runtimeSeats.notInstalled') }}
      </p>
      <el-alert
        v-else-if="!supported(provider)"
        type="info"
        :closable="false"
        :title="t('runtimeSeats.upgrade')"
      />
      <template v-else>
        <el-table
          :data="seats[provider]"
          row-key="id"
        >
          <el-table-column
            :label="t('runtimeSeats.label')"
            min-width="160"
          >
            <template #default="{ row }: { row: SeatState }">
              <span>{{ seatName(row) }}</span>
              <el-tag
                v-if="row.active"
                size="small"
                type="success"
                class="seat-current"
              >
                {{ t('runtimeSeats.current') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column
            :label="t('runtimeSeats.account')"
            min-width="180"
          >
            <template #default="{ row }: { row: SeatState }">
              {{ row.account || '—' }}
            </template>
          </el-table-column>
          <el-table-column
            :label="t('runtimeSeats.login')"
            width="110"
          >
            <template #default="{ row }: { row: SeatState }">
              {{ t(`runtimeNodes.login.${row.login}`) }}
            </template>
          </el-table-column>
          <el-table-column
            :label="t('runtimeSeats.quota')"
            min-width="220"
          >
            <template #default="{ row }: { row: SeatState }">
              <QuotaBar
                v-for="w in quotaWindows(provider, row)"
                :key="w.label"
                inline
                :label="w.label"
                :pct="w.pct"
                :resets-at="w.resetsAt"
              />
            </template>
          </el-table-column>
          <el-table-column
            :label="t('common.actions')"
            min-width="230"
          >
            <template #default="{ row }: { row: SeatState }">
              <el-button
                text
                type="primary"
                :loading="busy === `login-${row.id}`"
                :data-test="`login-${row.id}`"
                @click="startLogin(provider, row)"
              >
                {{ row.login === 'ready' ? t('runtimeSeats.signInAgain') : t('runtimeSeats.signIn') }}
              </el-button>
              <el-button
                text
                :disabled="row.active || row.login !== 'ready'"
                :loading="busy === `activate-${row.id}`"
                :data-test="`activate-${row.id}`"
                @click="activate(provider, row)"
              >
                {{ t('runtimeSeats.activate') }}
              </el-button>
              <el-button
                v-if="row.id !== 'default'"
                text
                type="danger"
                :disabled="row.active"
                :loading="busy === `remove-${row.id}`"
                :data-test="`remove-${row.id}`"
                @click="remove(provider, row)"
              >
                {{ t('runtimeSeats.remove') }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="seat-add">
          <el-input
            v-model="labels[provider]"
            :placeholder="t('runtimeSeats.labelPlaceholder')"
            maxlength="60"
            :data-test="`seat-label-${provider}`"
            @keyup.enter="add(provider)"
          />
          <el-button
            :disabled="!labels[provider].trim()"
            :loading="busy === `add-${provider}`"
            :data-test="`seat-add-${provider}`"
            @click="add(provider)"
          >
            {{ t('runtimeSeats.add') }}
          </el-button>
        </div>
      </template>
    </section>

    <el-dialog
      v-model="login.visible"
      append-to-body
      :close-on-click-modal="false"
      :title="t('runtimeSeats.loginTitle', { name: login.seat ? seatName(login.seat) : '' })"
      width="560px"
    >
      <template v-if="login.data">
        <p>{{ login.data.needs_code ? t('runtimeSeats.loginClaudeHint') : t('runtimeSeats.loginCodexHint') }}</p>
        <p>
          <el-link
            type="primary"
            :href="login.data.authorization_url"
            target="_blank"
            rel="noopener noreferrer"
            data-test="seat-login-link"
          >
            {{ t('runtimeSeats.loginOpen') }}
          </el-link>
          <span class="muted"> · {{ t('runtimeSeats.expires', { n: loginMinutes }) }}</span>
        </p>
        <p
          v-if="login.data.user_code"
          data-test="seat-user-code"
        >
          {{ t('runtimeSeats.userCode') }}：<strong class="user-code">{{ login.data.user_code }}</strong>
        </p>
        <el-input
          v-if="login.data.needs_code"
          v-model="login.code"
          :placeholder="t('runtimeSeats.code')"
          data-test="seat-login-code"
          @keyup.enter="finishLogin"
        />
      </template>
      <template #footer>
        <el-button @click="login.visible = false">
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          type="primary"
          :loading="busy === 'finish'"
          :disabled="!!login.data?.needs_code && !login.code.trim()"
          data-test="seat-login-finish"
          @click="finishLogin"
        >
          {{ login.data?.needs_code ? t('runtimeSeats.finish') : t('runtimeSeats.codexDone') }}
        </el-button>
      </template>
    </el-dialog>
  </el-dialog>
</template>

<style scoped>
.seat-provider { margin-top: 16px; }
.seat-provider h4 { margin: 0 0 8px; }
.seat-current { margin-left: 6px; }
.seat-add { display: flex; gap: 8px; margin-top: 8px; max-width: 420px; }
.user-code { font-family: var(--el-font-family-mono, monospace); font-size: 18px; letter-spacing: 2px; }
</style>
