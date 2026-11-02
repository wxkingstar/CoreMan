<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessageBox } from 'element-plus'
import { collaboration, type BotHelpSettings } from '@/api/collaboration'
import { errorMessage } from '@/utils/errors'
const props = defineProps<{ botId: string; canEdit: boolean; active: boolean }>()
const { t } = useI18n()
const state = ref<BotHelpSettings | null>(null)
const loading = ref(false), busy = ref(false), error = ref('')
let generation = 0
let controller = new AbortController()
const permitted = computed(() => props.active && props.canEdit)
function stop() { controller.abort(); controller = new AbortController(); generation++ }
async function refresh() {
 if (!permitted.value) return
 const epoch = ++generation
 loading.value = true; error.value = ''
 try { const result = await collaboration.get(props.botId, controller.signal); if (epoch === generation) state.value = result }
 catch (e) { if (epoch === generation) error.value = errorMessage(e) }
 finally { if (epoch === generation) loading.value = false }
}
watch(() => [props.botId, props.active, props.canEdit], () => {
 stop(); state.value = null; error.value = ''; busy.value = false; loading.value = false
 if (permitted.value) void refresh()
}, { immediate: true })
onBeforeUnmount(stop)
async function toggle(enabled: boolean) {
 if (!state.value || busy.value || !permitted.value) return
 const epoch = generation
 if (!enabled) {
  try { await ElMessageBox.confirm(t('collaboration.disableConfirm', { count: state.value.active_count }), t('collaboration.aiTitle'), { type: 'warning', confirmButtonText: t('collaboration.confirm'), cancelButtonText: t('collaboration.cancel') }) } catch { return }
  if (epoch !== generation) return
 }
 busy.value = true; error.value = ''
 try { const result = await collaboration.set(props.botId, enabled); if (epoch === generation) state.value = result }
 catch (e) { if (epoch === generation) error.value = errorMessage(e) }
 finally { if (epoch === generation) busy.value = false }
}
</script>

<template>
  <section
    class="collaboration-panel"
    :aria-label="t('collaboration.aiTitle')"
  >
    <el-empty
      v-if="!canEdit"
      :description="t('collaboration.noAccess')"
    />
    <template v-else>
      <h3 class="section-title">
        {{ t('collaboration.aiTitle') }}
      </h3>
      <p class="intro">
        {{ t('collaboration.intro') }}
      </p>
      <el-alert
        v-if="error"
        :title="error"
        type="error"
        :closable="false"
        role="alert"
      />
      <el-button
        v-if="error && !state"
        data-test="retry"
        :disabled="loading"
        @click="refresh"
      >
        {{ t('collaboration.retry') }}
      </el-button>
      <p
        v-if="loading && !state"
        role="status"
      >
        {{ t('collaboration.loading') }}
      </p>
      <template v-if="state">
        <div class="switch-row">
          <el-switch
            :model-value="state.accept_bot_help"
            data-test="accept-switch"
            :loading="busy"
            :disabled="busy"
            :aria-label="t('collaboration.accept')"
            @update:model-value="(value: string | number | boolean) => toggle(Boolean(value))"
          />
          <div>
            <strong>{{ t('collaboration.accept') }}</strong>
            <p>{{ t('collaboration.acceptHint') }}</p>
          </div>
        </div>
        <p v-if="state.active_count">
          {{ t('collaboration.active', { count: state.active_count }) }}
        </p>
        <el-alert
          v-if="state.permission !== 'granted'"
          data-test="permission"
          :title="t(`collaboration.permission.${state.permission}`)"
          :type="state.permission === 'unknown' ? 'info' : 'warning'"
          :closable="false"
        />
      </template>
    </template>
  </section>
</template>

<style scoped>
.section-title { margin: 0 0 12px; font-size: 16px; font-weight: 600; }
.intro { max-width: 70ch; margin: 0 0 20px; color: var(--el-text-color-secondary); line-height: 1.7; }
.switch-row { display: flex; align-items: flex-start; gap: 16px; margin-bottom: 16px; }
.switch-row p { margin: 6px 0 0; color: var(--el-text-color-secondary); line-height: 1.6; }
.collaboration-panel .el-alert { margin: 16px 0; }
</style>
