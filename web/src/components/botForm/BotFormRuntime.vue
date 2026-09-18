<script setup lang="ts">
import { ref } from 'vue'
import { FolderOpened } from '@element-plus/icons-vue'
import WorkspaceDrawer from '@/components/WorkspaceDrawer.vue'
import { useI18n } from 'vue-i18n'
import type { EffortLevel } from '@/api/types'
import { useBotFormContext } from '@/components/botForm/context'

/** part：新建时拆成「必填」（运行时）与「更多设置」两块；编辑时不传，整块展示。 */
defineProps<{ part?: 'essential' | 'extra' }>()
const workspaceVisible = ref(false)
const SSE_OPTIONS = [1800, 3600, 7200, 14400, 21600, 43200]
const VERBOSITY_OPTIONS = [1, 2, 3, 4]
const EFFORT_OPTIONS: EffortLevel[] = ['low', 'medium', 'high', 'xhigh']
const { t } = useI18n()
const {
  mode, botId, form, fieldErrors, relayList, runtimeGroups, selectedRuntime, runtimeBackends,
  selectRuntime, selectRelay, modelOptions, xhighAllowed,
} = useBotFormContext()
</script>

<template>
  <h3
    v-if="!part"
    id="form-runtime"
    class="cm-section-title"
  >
    <span>02</span>{{ t('workspace.configuration') }}
  </h3>
  <el-form-item
    v-if="part !== 'extra'"
    :label="t('bots.relay')"
    data-test="relay"
    :error="fieldErrors.relay_server_id"
    :required="mode === 'create'"
  >
    <div
      v-if="mode === 'create' && runtimeGroups.length"
      class="runtime-row"
    >
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
        @update:model-value="selectRelay($event as string)"
      >
        <el-option
          v-for="backend in runtimeBackends"
          :key="backend.id"
          :value="backend.id"
          :label="`${backend.model_provider === 'claude' ? 'Claude Code' : 'Codex / GPT'}${backend.unavailable_reason !== null ? ' · ' + t('runtimeNodes.unavailable.' + (backend.unavailable_reason ?? 'unknown')) : ''}`"
          :disabled="backend.unavailable_reason !== null || !backend.effective_models.length"
        />
      </el-select>
    </div>
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
        :label="`${r.name} · ${r.model_provider} · ${r.team_name ?? t('bots.publicPool')}${r.unavailable_reason !== null ? ' · ' + t('runtimeNodes.unavailable.' + (r.unavailable_reason ?? 'unknown')) : ''}`"
        :value="r.id"
        :disabled="r.unavailable_reason !== null || !r.effective_models.length"
      />
    </el-select>
    <div
      v-if="mode === 'edit'"
      class="muted"
    >
      {{ t('bots.relayLocked') }}
    </div>
  </el-form-item>

  <template v-if="part !== 'essential'">
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
      <div class="workspace-path-row">
        <el-input
          v-model="form.working_dir"
          :readonly="mode === 'edit' && !!form.relay_server_id"
        />
        <el-button
          v-if="botId"
          :icon="FolderOpened"
          text
          size="small"
          type="primary"
          @click="workspaceVisible = true"
        >
          {{ t('workspaceFiles.open') }}
        </el-button>
      </div>
      <WorkspaceDrawer
        v-if="botId && workspaceVisible"
        v-model:visible="workspaceVisible"
        :bot-id="botId"
      />
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
  </template>
</template>

<style scoped>
/* 运行时节点与 AI 类型并排等宽，窄屏时各占一行且左边缘对齐。 */
.runtime-row { display: flex; flex-wrap: wrap; gap: 10px; width: 100%; }
.runtime-row .el-select { flex: 1 1 200px; min-width: 0; }
.workspace-path-row { display: flex; align-items: center; gap: 8px; width: 100%; min-width: 0; }
.workspace-path-row .el-input { flex: 1; min-width: 0; }
.workspace-path-row .el-button { flex-shrink: 0; }

.muted { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
</style>
