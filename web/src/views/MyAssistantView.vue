<script setup lang="ts">
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { myAssistant, type AssistantAbsent, type AssistantOverview } from '@/api/myAssistant'
import AssistantBoard from '@/components/assistant/AssistantBoard.vue'
import AssistantFollowups from '@/components/assistant/AssistantFollowups.vue'
import AssistantJobs from '@/components/assistant/AssistantJobs.vue'
import AssistantNotes from '@/components/assistant/AssistantNotes.vue'
import AssistantRecent from '@/components/assistant/AssistantRecent.vue'
import AssistantSettings from '@/components/assistant/AssistantSettings.vue'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'
import '@/styles/assistant.css'

const { t } = useI18n()
const data = ref<AssistantOverview | AssistantAbsent | null>(null)
const error = ref(''), loading = ref(false)
const TABS = ['board', 'jobs', 'followups', 'recent', 'notes', 'settings'] as const
/** 打开哪个分区记在地址栏的 ?tab= 里：刷新、分享链接、从别处跳回来都回到同一个分区。 */
function initialTab() {
  const wanted = new URLSearchParams(window.location.search).get('tab')
  return (TABS as readonly string[]).includes(wanted ?? '') ? wanted as string : 'board'
}
const tab = ref(initialTab())
watch(tab, (value) => {
  const query = new URLSearchParams(window.location.search)
  if (value === 'board') query.delete('tab'); else query.set('tab', value)
  const search = query.toString()
  window.history.replaceState(window.history.state, '', window.location.pathname + (search ? `?${search}` : '') + window.location.hash)
})
/** 各分区组件都暴露 load：页头的「刷新」连同当前分区一起刷新，不只刷新概览。 */
const panes = ref<Record<string, { load?: () => Promise<unknown> } | null>>({})
const paneRef = (name: string) => (el: unknown) => { panes.value[name] = el as { load?: () => Promise<unknown> } | null }
async function refresh() {
  await Promise.all([load(), panes.value[tab.value]?.load?.()])
}
/** 今日轮次用到八成以上时提醒一下。 */
const turnsPercent = computed(() => {
  const turns = mine.value?.turns
  return turns && turns.budget > 0 ? Math.min(100, Math.round((turns.today / turns.budget) * 100)) : 0
})
/** 有助理时的概览；没有助理时是 null。 */
const mine = computed(() => (data.value?.exists ? data.value : null))
/** 没有助理时的空状态；有助理时是 null。 */
const absent = computed(() => (data.value && !data.value.exists ? data.value : null))

async function load() {
  loading.value = true; error.value = ''
  try { data.value = await myAssistant.overview() }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
/** 即时上班、下班，或回到按工作时段：与私聊里说「上班」「下班」同一个效果，切完重新取概览。 */
const switching = ref(false)
async function shift(state: 'on' | 'off' | 'auto') {
  if (switching.value) return
  switching.value = true
  try {
    await myAssistant.shift(state)
    ElMessage.success(t('assistant.overview.shiftSwitched'))
    await load()
  } catch (e) { ElMessage.error(errorMessage(e)) }
  finally { switching.value = false }
}
/** 从现在开始：清掉试用期间攒下的事项，历史对话留着做背景。先确认；清完重新取概览、刷新各分区。 */
const resetting = ref(false), resetKey = ref(0)
async function startFresh() {
  if (resetting.value) return
  try {
    await ElMessageBox.confirm(t('assistant.page.resetConfirm'), t('assistant.page.resetTitle'), { type: 'warning' })
  } catch { return }
  resetting.value = true
  try {
    const done = await myAssistant.reset()
    ElMessage.success(t('assistant.page.resetDone', { n: done.followups, count: done.jobs, total: done.approvals, value: done.inbox }))
    resetKey.value += 1
    await load()
  } catch (e) { ElMessage.error(errorMessage(e)) }
  finally { resetting.value = false }
}
onMounted(load)
defineExpose({ tab, load, refresh })
</script>
<template>
  <section class="my-assistant">
    <div class="cm-page-header">
      <h2>{{ t('menu.myAssistant') }}</h2>
      <p class="cm-page-intro">
        {{ t('assistant.page.intro') }}
        <a
          href="/assistant-intro/index.html"
          target="_blank"
          rel="noopener"
          data-test="assistant-story"
        >{{ t('assistant.page.story') }}</a>
      </p>
      <!-- 页头右侧只有一格：两个按钮放进同一个容器，免得叠在一起。 -->
      <div class="ma-header-actions">
        <el-button
          :loading="loading"
          :icon="Refresh"
          data-test="refresh"
          @click="refresh"
        >
          {{ t('assistant.page.refresh') }}
        </el-button>
        <el-button
          v-if="mine?.state === 'active'"
          type="warning"
          plain
          :loading="resetting"
          data-test="assistant-reset"
          @click="startFresh"
        >
          {{ t('assistant.page.reset') }}
        </el-button>
      </div>
    </div>
    <LoadState
      :loading="loading && !data"
      :error="error"
      @retry="load"
    />
    <el-empty
      v-if="absent"
      :description="t('assistant.empty.title')"
      data-test="assistant-empty"
    >
      <p v-if="absent.lifecycle">
        {{ t('assistant.empty.inProgress') }}
      </p>
      <template v-else-if="absent.eligible">
        <p>{{ t('assistant.empty.eligible') }}</p>
        <p v-if="absent.how_to.app_name">
          {{ t('assistant.empty.app', { name: absent.how_to.app_name }) }}
        </p>
        <ol class="how-to">
          <li
            v-for="step in absent.how_to.steps"
            :key="step"
          >
            {{ t('assistant.empty.steps.' + step) }}
          </li>
        </ol>
      </template>
      <p v-else>
        {{ t('assistant.empty.reasons.' + absent.reason) }}
      </p>
    </el-empty>
    <template v-if="mine">
      <dl
        class="ma-overview"
        data-test="assistant-overview"
      >
        <div class="ma-stat">
          <dt>{{ t('assistant.overview.state') }}</dt>
          <dd>
            <el-tag :type="mine.state === 'active' ? 'success' : 'warning'">
              {{ t('assistant.overview.states.' + (mine.state ?? 'unknown')) }}
            </el-tag>
            <span
              v-if="mine.paused_reason"
              class="ma-sub ma-danger"
            >{{ t('assistant.overview.paused', { detail: mine.paused_reason }) }}</span>
          </dd>
        </div>
        <div class="ma-stat">
          <dt>{{ t('assistant.overview.runtime') }}</dt>
          <dd v-if="mine.runtime">
            <span
              class="ma-dot"
              :class="{ 'is-on': mine.runtime.online }"
            />
            <span>{{ mine.runtime.online ? t('assistant.overview.online') : t('assistant.overview.offline') }}</span>
            <span class="ma-sub">{{ mine.runtime.name }}</span>
          </dd>
          <dd v-else>
            {{ t('assistant.overview.runtimeNone') }}
          </dd>
        </div>
        <div class="ma-stat">
          <dt>{{ t('assistant.overview.shift') }}</dt>
          <dd>
            <el-tag
              :type="mine.on_shift ? 'success' : 'info'"
              data-test="shift-state"
            >
              {{ mine.on_shift ? (mine.always_on && !mine.shift_override ? t('assistant.overview.alwaysOn') : t('assistant.overview.onShift')) : t('assistant.overview.offShift') }}
            </el-tag>
            <!-- 只在助理可用时能切：开通中、停用中没有可切换的东西。 -->
            <template v-if="mine.state === 'active'">
              <!-- 全天候时本来就在班：「上班」与「恢复全天候」是同一个效果，只留后者。 -->
              <el-button
                v-if="!mine.always_on"
                size="small"
                link
                type="primary"
                :disabled="switching"
                data-test="shift-on"
                @click="shift('on')"
              >
                {{ t('assistant.overview.shiftOn') }}
              </el-button>
              <el-button
                size="small"
                link
                type="primary"
                :disabled="switching"
                data-test="shift-off"
                @click="shift('off')"
              >
                {{ t('assistant.overview.shiftOff') }}
              </el-button>
              <el-button
                v-if="mine.shift_override"
                size="small"
                link
                type="primary"
                :disabled="switching"
                data-test="shift-auto"
                @click="shift('auto')"
              >
                {{ mine.always_on ? t('assistant.overview.shiftAlwaysOn') : t('assistant.overview.shiftAuto') }}
              </el-button>
            </template>
            <span
              v-if="mine.shift_override && mine.shift_override_until"
              class="ma-sub"
              data-test="shift-until"
            >{{ t('assistant.overview.shiftUntil', { time: formatDateTime(mine.shift_override_until) }) }}</span>
          </dd>
        </div>
        <!-- 冷启动做完之后就不用再看了：只在没做完时占一格。 -->
        <div
          v-if="mine.cold_start.state !== 'done'"
          class="ma-stat"
          data-test="cold-start"
        >
          <dt>{{ t('assistant.overview.coldStart') }}</dt>
          <dd>{{ t('assistant.overview.coldStartStates.' + mine.cold_start.state) }}</dd>
        </div>
        <div class="ma-stat">
          <dt>{{ t('assistant.overview.turns') }}</dt>
          <dd>
            <span>{{ t('assistant.overview.turnsValue', { n: mine.turns.today, total: mine.turns.budget }) }}</span>
            <el-progress
              :percentage="turnsPercent"
              :show-text="false"
              :stroke-width="4"
              :status="turnsPercent >= 80 ? 'warning' : undefined"
            />
          </dd>
        </div>
        <div class="ma-stat">
          <dt>{{ t('assistant.overview.watch') }}</dt>
          <dd v-if="mine.watch">
            <span
              class="ma-dot"
              :class="{ 'is-on': !mine.watch.last_error }"
            />
            <span>{{ t('assistant.overview.watchStates.' + mine.watch.state) }}</span>
            <span
              v-if="mine.watch.last_ok_at"
              class="ma-sub"
            >{{ t('assistant.overview.watchLastOk', { time: formatDateTime(mine.watch.last_ok_at) }) }}</span>
            <span
              v-if="mine.watch.last_error"
              class="ma-sub ma-danger"
            >{{ t('assistant.overview.watchError', { detail: mine.watch.last_error }) }}</span>
          </dd>
          <dd v-else>
            {{ t('assistant.overview.watchNone') }}
          </dd>
        </div>
      </dl>
      <el-alert
        v-if="mine.content_locked"
        type="warning"
        :closable="false"
        show-icon
        data-test="assistant-locked"
        :title="t('assistant.page.locked')"
      />
      <!-- lazy：没点开的分区不取数。 -->
      <el-tabs
        :key="resetKey"
        v-model="tab"
        class="ma-tabs"
      >
        <el-tab-pane
          :label="t('assistant.page.tabs.board')"
          name="board"
          lazy
        >
          <AssistantBoard :ref="paneRef('board')" />
        </el-tab-pane>
        <el-tab-pane
          :label="t('assistant.page.tabs.jobs')"
          name="jobs"
          lazy
        >
          <AssistantJobs :ref="paneRef('jobs')" />
        </el-tab-pane>
        <el-tab-pane
          :label="t('assistant.page.tabs.followups')"
          name="followups"
          lazy
        >
          <AssistantFollowups :ref="paneRef('followups')" />
        </el-tab-pane>
        <el-tab-pane
          :label="t('assistant.page.tabs.recent')"
          name="recent"
          lazy
        >
          <AssistantRecent :ref="paneRef('recent')" />
        </el-tab-pane>
        <el-tab-pane
          :label="t('assistant.page.tabs.notes')"
          name="notes"
          lazy
        >
          <AssistantNotes :ref="paneRef('notes')" />
        </el-tab-pane>
        <el-tab-pane
          :label="t('assistant.page.tabs.settings')"
          name="settings"
          lazy
        >
          <AssistantSettings
            :ref="paneRef('settings')"
            :name="mine.name"
            :runtime="mine.runtime?.name ?? null"
            @deprovisioned="load"
          />
        </el-tab-pane>
      </el-tabs>
    </template>
  </section>
</template>
<style scoped>
.how-to { text-align: left; display: inline-block; }
.ma-header-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.ma-header-actions .el-button + .el-button { margin-left: 0; }
.ma-tabs :deep(.el-tabs__header) { margin-bottom: 16px; }
/* el-tabs 默认裁掉溢出：放开，设置页底部的保存栏才能贴住视口。 */
.ma-tabs :deep(.el-tabs__content) { overflow: visible; }
</style>
