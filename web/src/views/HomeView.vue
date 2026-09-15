<script setup lang="ts">
import WorkbenchInsights from '@/components/WorkbenchInsights.vue'
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { bots } from '@/api/admin'
import { cron, type CronOut } from '@/api/cron'
import { statistics, type Statistics } from '@/api/statistics'
import type { BotOut, Page } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import LoadState from '@/components/LoadState.vue'
import { formatDateTime } from '@/utils/format'
const { t } = useI18n()
const auth = useAuthStore()
const employees = ref<Page<BotOut> | null>(null), enabled = ref<number | null>(null)
const jobs = ref<Page<CronOut> | null>(null), usage = ref<Statistics | null>(null)
const insights = ref<InstanceType<typeof WorkbenchInsights>>()
const busy = ref(false), updatedAt = ref('')
const errors = reactive({ employees: '', enabled: '', jobs: '', usage: '' })
const manager = computed(() => ['ai_committee', 'platform_admin'].includes(auth.user?.role ?? ''))
async function load() {
  if (busy.value) return
  busy.value = true
  Object.assign(errors, { employees: '', enabled: '', jobs: '', usage: '' })
  // Each endpoint independently applies its existing resource-level authorization.
  const results = await Promise.allSettled([
    bots.list({ scope: 'all', page: 1, per_page: 6 }),
    bots.list({ scope: 'all', enabled: true, page: 1, per_page: 1 }),
    cron.list(1), statistics.get({ timezone: 'Asia/Shanghai' }),
  ])
  const keys = ['employees', 'enabled', 'jobs', 'usage'] as const
  results.forEach((result, i) => { if (result.status === 'rejected') errors[keys[i]] = result.reason instanceof Error ? result.reason.message : t('workspace.failedHint') })
  employees.value = results[0].status === 'fulfilled' ? results[0].value : null
  enabled.value = results[1].status === 'fulfilled' ? results[1].value.total : null
  jobs.value = results[2].status === 'fulfilled' ? results[2].value : null
  usage.value = results[3].status === 'fulfilled' ? results[3].value : null
  updatedAt.value = new Date().toLocaleTimeString()
  busy.value = false
}
const count = (n: number | null | undefined) => n == null ? '—' : n.toLocaleString()
onMounted(load)
</script>
<template>
  <section class="home-view">
    <div class="page-header">
      <div>
        <div class="eyebrow">
          COREMAN / {{ t('workspace.workspace') }}
        </div><h2>{{ t('workspace.greeting', { name: auth.user?.display_name ?? '' }) }}</h2>
      </div><el-button
        :loading="busy"
        @click="load(); insights?.reload()"
      >
        {{ t('common.refresh') }}
      </el-button>
    </div>
    <p class="cm-page-intro">
      {{ t('workspace.homeHint') }}
    </p>
    <div
      class="summary-grid"
      :aria-busy="busy"
    >
      <el-card><span>{{ t('workspace.visibleBots') }}</span><strong>{{ busy ? '—' : count(employees?.total) }}</strong><small>{{ errors.employees || t('workspace.scope') }}</small></el-card>
      <el-card><span>{{ t('workspace.enabledBots') }}</span><strong>{{ busy ? '—' : count(enabled) }}</strong><small>{{ errors.enabled || t('workspace.enabledHint') }}</small></el-card>
      <el-card><span>{{ t('workspace.jobs') }}</span><strong>{{ busy ? '—' : count(jobs?.total) }}</strong><small>{{ errors.jobs || t('workspace.allRecords') }}</small></el-card>
      <el-card><span>{{ t('statistics.cost') }}</span><strong>{{ busy ? '—' : usage?.total.cost_usd == null ? t('statistics.unknown') : `$${usage.total.cost_usd.toFixed(4)}` }}</strong><small>{{ errors.usage || (usage ? t('statistics.coverage', { n: usage.total.cost_usd_measured, total: usage.total.messages }) : t('workspace.unknown')) }}</small></el-card>
    </div>
    <p
      v-if="usage"
      class="period"
    >
      {{ t('workspace.period', usage) }} · {{ t('workspace.costHint') }}
    </p>
    <WorkbenchInsights
      ref="insights"
      :usage="usage"
    />
    <div class="home-grid">
      <section class="cm-panel">
        <div class="panel-heading">
          <h3>{{ t('workspace.recentBots') }}</h3><router-link to="/bots?scope=all">
            {{ t('workspace.viewAll') }} →
          </router-link>
        </div>
        <LoadState
          :loading="busy"
          :error="errors.employees"
          :empty="!employees?.items.length"
          @retry="load"
        />
        <template v-if="!busy && !errors.employees">
          <router-link
            v-for="bot in employees?.items"
            :key="bot.id"
            :to="`/bots/${bot.id}`"
            class="employee-row"
          >
            <span class="employee-avatar">{{ bot.name.slice(0, 1) }}</span><span class="employee-label"><strong>{{ bot.name }}</strong><small>{{ bot.team_name || '—' }} · {{ t(`platforms.${bot.platform}`) }} · {{ bot.model }}</small></span><el-tag :type="bot.enabled ? 'success' : 'info'">
              {{ bot.enabled ? t('common.enabled') : t('common.disabled') }}
            </el-tag>
          </router-link>
        </template>
      </section>
      <section class="cm-panel">
        <div class="panel-heading">
          <h3>{{ t('workspace.jobsEntry') }}</h3><router-link to="/cron">
            {{ t('workspace.viewAll') }} →
          </router-link>
        </div>
        <LoadState
          :loading="busy"
          :error="errors.jobs"
          :empty="!jobs?.items.length"
          @retry="load"
        />
        <template v-if="!busy && !errors.jobs">
          <div
            v-for="job in jobs?.items.slice(0, 5)"
            :key="job.id"
            class="job-row"
          >
            <strong>{{ job.name }}</strong><small>{{ t('cron.next') }}: {{ job.next_run_at ? formatDateTime(job.next_run_at) : '—' }}</small><el-tag type="info">
              {{ job.enabled ? t('common.enabled') : t('common.disabled') }}
            </el-tag>
          </div>
        </template>
      </section>
    </div>
    <section class="cm-panel">
      <h3>{{ t('workspace.shortcuts') }}</h3><div class="shortcuts">
        <router-link to="/skills">
          {{ t('menu.skills') }} →
        </router-link><router-link to="/chat-logs">
          {{ t('menu.chatLogs') }} →
        </router-link><router-link to="/statistics">
          {{ t('menu.statistics') }} →
        </router-link><router-link
          v-if="manager"
          to="/skill-approvals"
        >
          {{ t('menu.skillApprovals') }} →
        </router-link>
      </div>
    </section>
    <p class="period">
      {{ t('workspace.scopeHint') }} <span v-if="updatedAt">{{ t('workspace.refreshTime', { time: updatedAt }) }}</span>
    </p>
  </section>
</template>
<style scoped>
.page-header { display: flex; justify-content: space-between; align-items: center; }
.eyebrow { font-size: 11px; letter-spacing: 1.8px; color: var(--el-color-primary); margin-bottom: 10px; font-weight: 600; }
.summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; margin: 24px 0 12px; }
.summary-grid span { color: var(--cm-muted); } .summary-grid strong { display: block; font-size: 30px; letter-spacing: -1px; margin: 12px 0; line-height: 1.3; overflow-wrap: anywhere; }
.summary-grid small, .period { color: var(--cm-muted); font-size: 12px; }
.home-grid { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr); gap: 20px; margin-top: 24px; }
.panel-heading { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 20px; } .panel-heading h3 { margin: 0; }
.employee-row { display: flex; align-items: center; gap: 12px; border-top: 1px solid var(--cm-border); padding: 16px 0; color: var(--cm-text); }
.employee-row:hover { text-decoration: none; color: var(--el-color-primary); }
.employee-avatar { width: 38px; height: 38px; border-radius: 10px; display: grid; place-items: center; background: var(--cm-brand-soft); color: var(--el-color-primary); flex-shrink: 0; }
.employee-label { flex: 1; min-width: 0; } .employee-label small { display: block; color: var(--cm-muted); overflow-wrap: anywhere; font-size: 12px; }
.job-row { border-top: 1px solid var(--cm-border); padding: 14px 0; } .job-row small { display: block; color: var(--cm-muted); margin: 5px 0; }
.shortcuts { display: flex; gap: 16px; flex-wrap: wrap; } .shortcuts a { background: var(--cm-brand-soft); padding: 10px 16px; border-radius: 7px; }
@media(max-width:1100px) { .summary-grid { grid-template-columns: repeat(2,minmax(0,1fr)); } }
@media(max-width:767px) { .home-grid { grid-template-columns: 1fr; gap: 0; }.summary-grid { gap: 10px; }.summary-grid strong { font-size: 25px; } }
</style>
