<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { myAssistant, type AssistantAbsent, type AssistantOverview } from '@/api/myAssistant'
import AssistantAttention from '@/components/assistant/AssistantAttention.vue'
import AssistantFollowups from '@/components/assistant/AssistantFollowups.vue'
import AssistantJobs from '@/components/assistant/AssistantJobs.vue'
import AssistantNotes from '@/components/assistant/AssistantNotes.vue'
import AssistantRecent from '@/components/assistant/AssistantRecent.vue'
import AssistantSettings from '@/components/assistant/AssistantSettings.vue'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const data = ref<AssistantOverview | AssistantAbsent | null>(null)
const error = ref(''), loading = ref(false)
const tab = ref('attention')
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
onMounted(load)
defineExpose({ tab, load })
</script>
<template>
  <section class="my-assistant">
    <div class="cm-page-header">
      <h1>{{ t('menu.myAssistant') }}</h1>
      <p class="cm-page-intro">
        {{ t('assistant.page.intro') }}
      </p>
      <el-button
        :loading="loading"
        @click="load"
      >
        {{ t('assistant.page.refresh') }}
      </el-button>
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
        class="assistant-overview"
        data-test="assistant-overview"
      >
        <div>
          <dt>{{ t('assistant.overview.state') }}</dt>
          <dd>
            <el-tag :type="mine.state === 'active' ? 'success' : 'warning'">
              {{ t('assistant.overview.states.' + (mine.state ?? 'unknown')) }}
            </el-tag>
            <span
              v-if="mine.paused_reason"
              class="muted"
            >{{ t('assistant.overview.paused', { detail: mine.paused_reason }) }}</span>
          </dd>
        </div>
        <div>
          <dt>{{ t('assistant.overview.runtime') }}</dt>
          <dd v-if="mine.runtime">
            {{ mine.runtime.name }} · {{ mine.runtime.online ? t('assistant.overview.online') : t('assistant.overview.offline') }}
          </dd>
          <dd v-else>
            {{ t('assistant.overview.runtimeNone') }}
          </dd>
        </div>
        <div>
          <dt>{{ t('assistant.overview.shift') }}</dt>
          <dd>
            <el-tag
              :type="mine.on_shift ? 'success' : 'info'"
              data-test="shift-state"
            >
              {{ mine.on_shift ? t('assistant.overview.onShift') : t('assistant.overview.offShift') }}
            </el-tag>
            <span
              v-if="mine.shift_override && mine.shift_override_until"
              class="muted"
              data-test="shift-until"
            >{{ t('assistant.overview.shiftUntil', { time: formatDateTime(mine.shift_override_until) }) }}</span>
            <!-- 只在助理可用时能切：开通中、停用中没有可切换的东西。 -->
            <el-button-group v-if="mine.state === 'active'">
              <el-button
                size="small"
                :disabled="switching"
                data-test="shift-on"
                @click="shift('on')"
              >
                {{ t('assistant.overview.shiftOn') }}
              </el-button>
              <el-button
                size="small"
                :disabled="switching"
                data-test="shift-off"
                @click="shift('off')"
              >
                {{ t('assistant.overview.shiftOff') }}
              </el-button>
              <el-button
                v-if="mine.shift_override"
                size="small"
                :disabled="switching"
                data-test="shift-auto"
                @click="shift('auto')"
              >
                {{ t('assistant.overview.shiftAuto') }}
              </el-button>
            </el-button-group>
          </dd>
        </div>
        <div>
          <dt>{{ t('assistant.overview.coldStart') }}</dt>
          <dd>{{ t('assistant.overview.coldStartStates.' + mine.cold_start.state) }}</dd>
        </div>
        <div>
          <dt>{{ t('assistant.overview.turns') }}</dt>
          <dd>{{ t('assistant.overview.turnsValue', { n: mine.turns.today, total: mine.turns.budget }) }}</dd>
        </div>
        <div>
          <dt>{{ t('assistant.overview.watch') }}</dt>
          <dd v-if="mine.watch">
            {{ t('assistant.overview.watchStates.' + mine.watch.state) }}
            <span
              v-if="mine.watch.last_ok_at"
              class="muted"
            >{{ t('assistant.overview.watchLastOk', { time: formatDateTime(mine.watch.last_ok_at) }) }}</span>
            <span
              v-if="mine.watch.last_error"
              class="muted"
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
      <el-tabs v-model="tab">
        <el-tab-pane
          :label="t('assistant.page.tabs.attention')"
          name="attention"
          lazy
        >
          <AssistantAttention />
        </el-tab-pane>
        <el-tab-pane
          :label="t('assistant.page.tabs.jobs')"
          name="jobs"
          lazy
        >
          <AssistantJobs />
        </el-tab-pane>
        <el-tab-pane
          :label="t('assistant.page.tabs.followups')"
          name="followups"
          lazy
        >
          <AssistantFollowups />
        </el-tab-pane>
        <el-tab-pane
          :label="t('assistant.page.tabs.recent')"
          name="recent"
          lazy
        >
          <AssistantRecent />
        </el-tab-pane>
        <el-tab-pane
          :label="t('assistant.page.tabs.notes')"
          name="notes"
          lazy
        >
          <AssistantNotes />
        </el-tab-pane>
        <el-tab-pane
          :label="t('assistant.page.tabs.settings')"
          name="settings"
          lazy
        >
          <AssistantSettings
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
.assistant-overview { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin: 16px 0; }
.assistant-overview dt { color: var(--el-text-color-secondary); font-size: 12px; }
.assistant-overview dd { margin: 4px 0 0; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.how-to { text-align: left; display: inline-block; }
.muted { color: var(--el-text-color-secondary); }
</style>
