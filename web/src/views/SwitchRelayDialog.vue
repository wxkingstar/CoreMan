<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { bots, relays } from '@/api/admin'
import { ApiError } from '@/api/client'
import type { BotOut, RelayOut, SwitchRelayOut } from '@/api/types'
import HealthTag from '@/components/HealthTag.vue'
import { pctColor, pctNum } from '@/utils/format'
import { backendOf } from '@/utils/models'

const props = defineProps<{ bot: BotOut; visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [boolean]; switched: [SwitchRelayOut] }>()

const { t } = useI18n()

const relayList = ref<RelayOut[]>([])
const loading = ref(false)
const saving = ref(false)
const targetId = ref<string | null>(null)
const models = ref<string[]>([])
const model = ref<string>('')

const target = computed(() => relayList.value.find((r) => r.id === targetId.value) ?? null)
// 挂载时会预选当前 relay，此时直接确认就是空操作：后端照样写一条 diff 为空的 bot.switch_relay 审计。
const unchanged = computed(() => targetId.value === props.bot.relay_server_id && model.value === props.bot.model)
// 跨 backend（claude ↔ codex）要红字强提示：会话上下文与工具链都不一样。
const backendChanged = computed(() => !!target.value && !!model.value && backendOf(model.value) !== props.bot.backend)

function fail(e: unknown): void {
  if (e instanceof ApiError && e.status === 409) ElMessage.error(t('common.conflict'))
  else ElMessage.error(e instanceof Error ? e.message : String(e))
}

/** 选中一台 relay：载入它的有效模型集，当前模型还在集合里就留着，否则落到该 relay 的默认模型。 */
async function pick(id: string): Promise<void> {
  targetId.value = id
  try {
    const info = await relays.models(id)
    models.value = info.models
    model.value = info.models.includes(props.bot.model) ? props.bot.model : (info.default ?? info.models[0] ?? '')
  } catch (e) {
    models.value = []
    model.value = ''
    fail(e)
  }
}

async function confirm(): Promise<void> {
  if (!targetId.value || !model.value || unchanged.value) return
  saving.value = true
  try {
    const result = await bots.switchRelay(props.bot.id, { relay_server_id: targetId.value, model: model.value }, props.bot.version)
    ElMessage.success(t('bots.switch.done'))
    emit('switched', result)
    emit('update:visible', false)
  } catch (e) {
    fail(e)
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  loading.value = true
  try {
    relayList.value = (await relays.list({ is_active: true, per_page: 200 })).items
  } catch (e) {
    fail(e)
  } finally {
    loading.value = false
  }
  // 默认选中当前这台：换到自己是允许的（后端不校验团队），这个对话框同时也是「只换模型」的入口。
  const cur = props.bot.relay_server_id
  if (cur && relayList.value.some((r) => r.id === cur)) await pick(cur)
})

defineExpose({ pick, confirm })
</script>

<template>
  <el-dialog
    :close-on-click-modal="false"
    :model-value="visible"
    :title="t('bots.switch.title')"
    width="900px"
    @update:model-value="emit('update:visible', $event)"
  >
    <el-table
      v-loading="loading"
      :data="relayList"
      max-height="360"
    >
      <el-table-column width="56">
        <template #default="{ row }: { row: RelayOut }">
          <el-radio
            :model-value="targetId"
            :value="row.id"
            :data-test="'pick-' + row.id"
            @change="pick(row.id)"
          >
            <span />
          </el-radio>
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        :label="t('relays.name')"
      >
        <template #default="{ row }: { row: RelayOut }">
          <div>{{ row.name }}</div>
          <el-tag
            v-if="row.id === bot.relay_server_id"
            size="small"
            type="info"
          >
            {{ t('bots.switch.current') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('relays.provider')"
        width="150"
      >
        <template #default="{ row }: { row: RelayOut }">
          <div>{{ row.model_provider }}</div>
          <!-- 后端标签按默认模型推断（后端也是按模型名定 backend 的）；没有默认模型就不猜。 -->
          <el-tag
            v-if="row.default_model"
            size="small"
            type="info"
          >
            {{ t(`bots.backend.${backendOf(row.default_model)}`) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('relays.server')"
        width="160"
      >
        <template #default="{ row }: { row: RelayOut }">
          {{ row.host }}:{{ row.clawrelay_port }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('relays.rate5h')"
        width="120"
      >
        <template #default="{ row }: { row: RelayOut }">
          <el-progress
            v-if="pctNum(row.rate_limit_5h_used_pct) !== null"
            :percentage="pctNum(row.rate_limit_5h_used_pct) ?? 0"
            :color="pctColor(row.rate_limit_5h_used_pct)"
          />
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('relays.rate7d')"
        width="120"
      >
        <template #default="{ row }: { row: RelayOut }">
          <el-progress
            v-if="pctNum(row.rate_limit_7d_used_pct) !== null"
            :percentage="pctNum(row.rate_limit_7d_used_pct) ?? 0"
            :color="pctColor(row.rate_limit_7d_used_pct)"
          />
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('relays.healthTitle')"
        width="90"
      >
        <template #default="{ row }: { row: RelayOut }">
          <HealthTag :status="row.health_status" />
        </template>
      </el-table-column>
      <el-table-column
        :label="t('relays.team')"
        width="120"
      >
        <template #default="{ row }: { row: RelayOut }">
          {{ row.team_name ?? t('relays.publicPool') }}
        </template>
      </el-table-column>
    </el-table>

    <el-form
      class="switch-form"
      label-width="120px"
      @submit.prevent
    >
      <el-form-item :label="t('bots.switch.target')">
        <span data-test="switch-target">{{ target ? target.name : '—' }}</span>
      </el-form-item>
      <el-form-item
        :label="t('bots.switch.model')"
        data-test="switch-model"
      >
        <el-select
          v-model="model"
          filterable
          :disabled="!target"
          style="width: 320px"
        >
          <el-option
            v-for="m in models"
            :key="m"
            :label="m"
            :value="m"
          />
        </el-select>
      </el-form-item>
    </el-form>

    <el-alert
      :title="t('bots.switch.warnings')"
      type="warning"
      :closable="false"
      show-icon
    />
    <el-alert
      v-if="backendChanged"
      class="backend-change"
      :title="t('bots.switch.backendChange')"
      type="error"
      :closable="false"
      show-icon
    />

    <template #footer>
      <el-button
        data-test="switch-cancel"
        @click="emit('update:visible', false)"
      >
        {{ t('common.cancel') }}
      </el-button>
      <el-button
        type="primary"
        :loading="saving"
        :disabled="!target || !model || unchanged"
        data-test="switch-confirm"
        @click="confirm"
      >
        {{ t('bots.switch.confirm') }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.switch-form { margin-top: 12px; }
.backend-change { margin-top: 8px; }
</style>
