<script setup lang="ts">
import LoadState from '@/components/LoadState.vue'
import { useListQuery } from '@/composables/useListQuery'
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { statistics, tokenKeys, type Statistics, type ModelPrice } from '@/api/statistics'
import { useAuthStore } from '@/stores/auth'
const { t } = useI18n()
const auth = useAuthStore()
const manager = computed(() => ['ai_committee', 'platform_admin'].includes(auth.user?.role ?? ''))
const data = ref<Statistics | null>(null), prices = ref<ModelPrice[]>([])
const busy = ref(false), editing = ref(false), saving = ref(false)
const range = ref<string[]>([]), timezone = ref('Asia/Shanghai')
const metric = ref<'messages' | 'users' | 'bots'>('messages')
const emptyPrice = (): ModelPrice => ({ provider: 'claude', model: '', effective_from: new Date().toISOString().slice(0, 10), input_usd: '', output_usd: '', cache_read_usd: '', cache_write_usd: '', version: 0 })
const price = reactive<ModelPrice>(emptyPrice())
const money = (value: number | null) => value == null ? t('statistics.unknown') : `$${value.toFixed(6)}`
const number = (value: number | null) => value == null ? t('statistics.unknown') : value.toLocaleString()
const fail = (e: unknown) => ElMessage.error(e instanceof Error ? e.message : String(e))
const chart = computed(() => {
  if (!data.value) return []
  const first = Date.parse(data.value.start), last = Date.parse(data.value.end)
  const values = new Map(data.value.daily.map(row => [row.day, row[metric.value]]))
  return Array.from({ length: Math.round((last - first) / 86400000) + 1 }, (_, index) => { const day = new Date(first + index * 86400000).toISOString().slice(0, 10); return { day, value: values.get(day) ?? 0 } })
})
const peak = computed(() => Math.max(1, ...chart.value.map(row => row.value)))
const chartHost = ref<HTMLElement>()
const chartWidth = ref(800)
let chartObserver: ResizeObserver | undefined
watch(chartHost, (host) => {
  chartObserver?.disconnect()
  if (!host) return
  chartWidth.value = Math.max(240, host.clientWidth)
  chartObserver = new ResizeObserver(([entry]) => { if (entry) chartWidth.value = Math.max(240, entry.contentRect.width) })
  chartObserver.observe(host)
})
onBeforeUnmount(() => chartObserver?.disconnect())
const plotWidth = computed(() => chartWidth.value - 64)
const barStep = computed(() => plotWidth.value / Math.max(1, chart.value.length))
const barWidth = computed(() => Math.min(32, Math.max(1, barStep.value * 0.64)))
const labelEvery = computed(() => Math.max(1, Math.ceil(chart.value.length / Math.max(2, Math.floor(plotWidth.value / 85)))))
const axisMax = computed(() => Math.max(4, Math.ceil(peak.value / 4) * 4))
const loadError = ref('')
const { persist } = useListQuery({ range, timezone, metric }, () => { void load() })
async function load() { persist(); loadError.value = ''; if (busy.value) return; busy.value = true; try { data.value = await statistics.get({ start: range.value?.[0], end: range.value?.[1], timezone: timezone.value }); range.value = [data.value.start, data.value.end] } catch (e) { loadError.value = e instanceof Error ? e.message : String(e); data.value = null; fail(e) } finally { busy.value = false } }
async function loadPrices() { try { prices.value = await statistics.prices() } catch (e) { fail(e) } }
function edit(row?: ModelPrice) { Object.assign(price, row ?? emptyPrice()); editing.value = true }
async function save() { saving.value = true; try { await statistics.savePrice(price); editing.value = false; await loadPrices() } catch (e) { fail(e) } finally { saving.value = false } }
onMounted(async () => { await load(); if (manager.value) await loadPrices() })
</script>
<template>
  <section class="statistics-page">
    <header class="cm-page-header">
      <h2>{{ t('menu.statistics') }}</h2>
      <p class="cm-page-intro">
        {{ t('workspace.intro.statistics') }}
      </p>
    </header>
    <div class="statistics-filters">
      <el-date-picker
        v-model="range"
        class="date-range"
        type="daterange"
        value-format="YYYY-MM-DD"
        :start-placeholder="t('statistics.start')"
        :end-placeholder="t('statistics.end')"
      /><el-select
        v-model="timezone"
        class="timezone"
        aria-label="Timezone"
      >
        <el-option
          label="UTC+8"
          value="Asia/Shanghai"
        /><el-option
          label="UTC+9"
          value="Asia/Tokyo"
        /><el-option
          label="UTC"
          value="UTC"
        />
      </el-select><el-button
        :loading="busy"
        @click="load"
      >
        {{ t('common.refresh') }}
      </el-button>
    </div>
    <el-alert
      :title="t('statistics.hint')"
      type="info"
      :closable="false"
    />
    <LoadState
      :loading="busy"
      :error="loadError"
      @retry="load"
    />
    <template v-if="data">
      <div class="cards">
        <el-card
          v-for="key in ['messages', 'users', 'bots'] as const"
          :key="key"
        >
          <span>{{ t(`statistics.${key}`) }}</span><strong>{{ number(data.total[key]) }}</strong>
        </el-card><el-card><span>{{ t('statistics.cost') }}</span><strong>{{ money(data.total.cost_usd) }}</strong><small>{{ t('statistics.coverage', { n: data.total.cost_usd_measured, total: data.total.messages }) }}</small></el-card>
      </div>
      <div class="cards token-cards">
        <el-card
          v-for="key in tokenKeys"
          :key="key"
        >
          <span>{{ t(`statistics.${key}`) }}</span><strong>{{ number(data.total[key]) }}</strong><small>{{ t('statistics.coverage', { n: data.total[`${key}_measured`], total: data.total.messages }) }}</small>
        </el-card>
      </div>
      <section class="trend-panel">
        <div class="trend-heading">
          <h3>{{ t('statistics.trend') }}</h3>
          <el-radio-group
            v-model="metric"
            size="small"
          >
            <el-radio-button value="messages">
              {{ t('statistics.messages') }}
            </el-radio-button><el-radio-button value="users">
              {{ t('statistics.users') }}
            </el-radio-button><el-radio-button value="bots">
              {{ t('statistics.bots') }}
            </el-radio-button>
          </el-radio-group>
        </div>
        <div
          ref="chartHost"
          class="chart"
        >
          <svg
            :viewBox="`0 0 ${chartWidth} 260`"
            role="img"
            :aria-label="t('statistics.trend')"
          >
            <g
              v-for="tick in [0, 1, 2, 3, 4]"
              :key="tick"
            >
              <line
                x1="48"
                :x2="chartWidth - 16"
                :y1="218 - tick * 48"
                :y2="218 - tick * 48"
                class="grid-line"
              />
              <text
                x="38"
                :y="222 - tick * 48"
                text-anchor="end"
                class="axis-label"
              >{{ (axisMax * tick / 4).toLocaleString() }}</text>
            </g>
            <g
              v-for="(row, index) in chart"
              :key="row.day"
            >
              <rect
                :x="48 + (index + .5) * barStep - barWidth / 2"
                :y="218 - row.value / axisMax * 192"
                :width="barWidth"
                :height="row.value / axisMax * 192"
                rx="2"
                fill="var(--el-color-primary)"
              >
                <title>{{ row.day }}: {{ number(row.value) }}</title>
              </rect>
              <text
                v-if="index % labelEvery === 0"
                :x="48 + (index + .5) * barStep"
                y="245"
                text-anchor="middle"
                class="axis-label"
              >{{ row.day.slice(5) }}</text>
            </g>
          </svg>
        </div>
      </section>
      <el-collapse>
        <el-collapse-item :title="t('workspace.dataTable')">
          <el-table :data="data.daily">
            <el-table-column
              min-width="140"
              prop="day"
              :label="t('statistics.start')"
            /><el-table-column
              min-width="140"
              prop="messages"
              :label="t('statistics.messages')"
            /><el-table-column
              min-width="140"
              :label="t('statistics.cost')"
            >
              <template #default="{ row }">
                {{ money(row.cost_usd) }}<small class="coverage">{{ t('statistics.coverage', { n: row.cost_usd_measured, total: row.messages }) }}</small>
              </template>
            </el-table-column>
          </el-table>
        </el-collapse-item>
      </el-collapse>
      <el-tabs class="ranking-tabs">
        <el-tab-pane :label="t('statistics.botRanking')">
          <el-table :data="data.by_bot">
            <el-table-column
              min-width="140"
              prop="name"
              :label="t('common.name')"
            /><el-table-column
              min-width="140"
              prop="messages"
              :label="t('statistics.messages')"
            /><el-table-column
              min-width="140"
              prop="users"
              :label="t('statistics.users')"
            /><el-table-column
              min-width="140"
              :label="t('statistics.cost')"
            >
              <template #default="{ row }">
                {{ money(row.cost_usd) }}<small class="coverage">{{ t('statistics.coverage', { n: row.cost_usd_measured, total: row.messages }) }}</small>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane><el-tab-pane :label="t('statistics.userRanking')">
          <el-table :data="data.by_user">
            <el-table-column
              min-width="140"
              prop="name"
              :label="t('common.name')"
            /><el-table-column
              min-width="140"
              prop="messages"
              :label="t('statistics.messages')"
            /><el-table-column
              min-width="140"
              prop="bots"
              :label="t('statistics.bots')"
            /><el-table-column
              min-width="140"
              :label="t('statistics.cost')"
            >
              <template #default="{ row }">
                {{ money(row.cost_usd) }}<small class="coverage">{{ t('statistics.coverage', { n: row.cost_usd_measured, total: row.messages }) }}</small>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </template>
    <el-collapse v-if="manager">
      <el-collapse-item :title="t('statistics.prices')">
        <el-alert
          :title="t('statistics.priceHint')"
          :closable="false"
        /><el-button @click="edit()">
          {{ t('common.create') }}
        </el-button><el-table :data="prices">
          <el-table-column
            min-width="140"
            prop="model"
            :label="t('statistics.model')"
          /><el-table-column
            min-width="140"
            prop="effective_from"
            :label="t('statistics.effective')"
          /><el-table-column
            min-width="140"
            prop="input_usd"
            :label="t('statistics.input_tokens')"
          /><el-table-column
            min-width="140"
            prop="output_usd"
            :label="t('statistics.output_tokens')"
          /><el-table-column
            min-width="140"
            prop="cache_read_usd"
            :label="t('statistics.cache_read_tokens')"
          /><el-table-column
            min-width="140"
            prop="cache_write_usd"
            :label="t('statistics.cache_creation_tokens')"
          /><el-table-column min-width="140">
            <template #default="{ row }">
              <el-button @click="edit(row)">
                {{ t('common.edit') }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-collapse-item>
    </el-collapse>
    <el-dialog
      v-model="editing"
      :close-on-click-modal="false"
      :title="t('statistics.prices')"
      width="600px"
    >
      <el-form label-position="top">
        <el-form-item :label="t('statistics.provider')">
          <el-radio-group
            v-model="price.provider"
            :disabled="price.version > 0"
          >
            <el-radio value="claude">
              Claude
            </el-radio><el-radio value="codex">
              Codex
            </el-radio>
          </el-radio-group>
        </el-form-item><el-form-item :label="t('statistics.model')">
          <el-input
            v-model="price.model"
            :disabled="price.version > 0"
          />
        </el-form-item><el-form-item :label="t('statistics.effective')">
          <el-date-picker
            v-model="price.effective_from"
            value-format="YYYY-MM-DD"
            :disabled="price.version > 0"
          />
        </el-form-item><el-form-item
          v-for="key in ['input_usd', 'output_usd', 'cache_read_usd', 'cache_write_usd'] as const"
          :key="key"
          :label="t(`statistics.${key}`)"
        >
          <el-input
            v-model="price[key]"
            inputmode="decimal"
          />
        </el-form-item>
      </el-form><template #footer>
        <el-button @click="editing = false">
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          type="primary"
          :loading="saving"
          @click="save"
        >
          {{ t('common.save') }}
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>
<style scoped>

.statistics-filters { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin: 22px 0 16px; }
.statistics-filters :deep(.date-range) { flex: 0 1 320px; width: 320px; }
.timezone { flex: 0 0 120px; width: 120px; }
.cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin: 16px 0; }
.cards :deep(.el-card__body) { padding: 18px 20px; }
.cards span { color: var(--cm-muted); font-size: 13px; }
.cards strong { display: block; font-size: 28px; line-height: 1.3; font-weight: 650; margin: 10px 0 4px; overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
.cards small, .coverage { color: var(--cm-muted); font-size: 12px; line-height: 1.5; }
.coverage { display: block; margin-top: 4px; }
.token-cards { margin-bottom: 24px; }
.token-cards :deep(.el-card__body) { padding: 14px 20px; }
.token-cards strong { font-size: 21px; margin-top: 8px; }
.trend-panel { padding: 20px 24px 8px; border: 1px solid var(--cm-border); border-radius: 10px; background: var(--cm-surface); margin-bottom: 16px; }
.trend-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
.trend-heading h3 { margin: 0; font-size: 15px; }
.chart { width: 100%; min-width: 0; }
.chart svg { display: block; width: 100%; height: 260px; }
.axis-label { fill: var(--cm-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.grid-line { stroke: var(--cm-border); stroke-width: 1; stroke-dasharray: 3 5; }
.ranking-tabs { margin: 24px 0; }
@media (max-width: 800px) { .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 480px) {
  .statistics-filters :deep(.date-range) { flex-basis: 100%; width: 100%; }
  .cards { gap: 10px; }
  .cards :deep(.el-card__body) { padding: 14px; }
  .cards strong { font-size: 24px; }
  .token-cards strong { font-size: 19px; }
  .trend-panel { padding: 16px 10px 4px; }
}
</style>
