<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { runtime } from '@/api/admin'
import { skills, type Approval } from '@/api/skills'
import type { Statistics } from '@/api/statistics'
import { useAuthStore } from '@/stores/auth'
import LoadState from './LoadState.vue'
const props = defineProps<{ usage: Statistics | null }>()
const { t } = useI18n(), auth = useAuthStore()
const manager = computed(() => ['ai_committee', 'platform_admin'].includes(auth.user?.role ?? ''))
const operator = computed(() => ['team_lead', 'ai_committee', 'platform_admin'].includes(auth.user?.role ?? ''))
const approvals = ref<Approval[]>([]), failedMessages = ref<number | null>(null)
const approvalError = ref(''), runtimeError = ref(''), loading = ref(false)
const daily = computed(() => props.usage?.daily ?? [])
const peak = computed(() => Math.max(1, ...daily.value.map(row => row.messages)))
async function reload() {
  if (loading.value) return
  loading.value = true; approvalError.value = ''; runtimeError.value = ''
  const [requests, queue] = await Promise.allSettled([manager.value ? skills.approvals(1) : Promise.resolve(null), operator.value ? runtime.queue() : Promise.resolve(null)])
  if (requests.status === 'fulfilled') approvals.value = requests.value?.items.filter(row => row.status === 'pending') ?? []
  else { approvals.value = []; approvalError.value = requests.reason instanceof Error ? requests.reason.message : t('workspace.failedHint') }
  if (queue.status === 'fulfilled') failedMessages.value = queue.value?.outbox_failed ?? null
  else { failedMessages.value = null; runtimeError.value = queue.reason instanceof Error ? queue.reason.message : t('workspace.failedHint') }
  loading.value = false
}
onMounted(reload)
defineExpose({ reload })
</script>
<template>
  <div class="insight-grid">
    <section class="cm-panel">
      <div class="insight-heading">
        <h3>{{ t('statistics.trend') }}</h3><router-link to="/statistics">
          {{ t('workspace.viewAll') }} →
        </router-link>
      </div>
      <div
        v-if="usage"
        class="usage-summary"
      >
        <span
          v-for="key in ['messages', 'users', 'bots'] as const"
          :key="key"
        >{{ t(`statistics.${key}`) }} <strong>{{ usage.total[key]?.toLocaleString() ?? '—' }}</strong></span>
      </div>
      <svg
        v-if="daily.length"
        viewBox="0 0 600 160"
        role="img"
        :aria-label="t('statistics.trend')"
        class="mini-chart"
      ><g
        v-for="(row, index) in daily"
        :key="row.day"
      ><rect
        :x="(index + 0.5) * 600 / daily.length - Math.min(36, Math.max(2, 600 / daily.length - 8)) / 2"
        :y="130 - row.messages / peak * 115"
        :width="Math.min(36, Math.max(2, 600 / daily.length - 8))"
        :height="row.messages / peak * 115"
        fill="var(--el-color-primary)"
      ><title>{{ row.day }}: {{ row.messages }}</title></rect><text
        v-if="index % Math.max(1, Math.ceil(daily.length / 6)) === 0"
        :x="index * 600 / daily.length + 4"
        y="153"
        fill="currentColor"
        font-size="12"
      >{{ row.day.slice(5) }}</text></g></svg>
      <p
        v-else
        class="muted"
      >
        {{ usage ? t('workspace.empty') : t('workspace.unknown') }}
      </p>
    </section>
    <section
      v-if="operator"
      class="cm-panel"
    >
      <h3>{{ t('workspace.attention') }}</h3>
      <LoadState
        :loading="loading"
        :error="runtimeError"
        @retry="reload"
      />
      <router-link
        v-if="failedMessages != null"
        class="attention-link"
        to="/runtime"
      >
        {{ t('runtime.failedOutbox') }} <el-tag :type="failedMessages ? 'danger' : 'info'">
          {{ failedMessages }}
        </el-tag>
      </router-link>
      <template v-if="manager">
        <p class="muted">
          {{ t('workspace.approvalSample') }}
        </p><LoadState
          :error="approvalError"
          @retry="reload"
        /><router-link
          v-for="approval in approvals.slice(0, 4)"
          :key="approval.id"
          class="attention-link"
          to="/skill-approvals"
        >
          <span>{{ approval.bot_id }}</span><el-tag type="warning">
            {{ t('skill.status.pending') }}
          </el-tag>
        </router-link><router-link to="/skill-approvals">
          {{ t('menu.skillApprovals') }} →
        </router-link>
      </template>
    </section>
  </div>
</template>
<style scoped>
.insight-grid { display: grid; grid-template-columns: minmax(0,1.4fr) minmax(0,1fr); gap: 20px; }.insight-grid > section:only-child { grid-column: 1 / -1; }
.insight-heading { display: flex; justify-content: space-between; gap: 12px; }.usage-summary { display: flex; flex-wrap: wrap; gap: 20px; color: var(--cm-muted); }.usage-summary strong { margin-left: 6px; color: var(--cm-text); }
.mini-chart { width: 100%; max-height: 180px; margin-top: 16px; }.attention-link { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--cm-border); }.attention-link span { overflow-wrap: anywhere; }.muted { color: var(--cm-muted); font-size: 12px; margin: 12px 0; }
@media(max-width:767px) { .insight-grid { grid-template-columns: 1fr; gap: 0; } }
</style>
