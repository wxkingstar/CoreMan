<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { feishuApps, type ObservePermission } from '@/api/feishuApps'
import { useBotFormContext } from '@/components/botForm/context'
import FeishuRegistrationDialog from '@/components/feishuApp/FeishuRegistrationDialog.vue'
import { errorMessage } from '@/utils/errors'

defineProps<{ part?: 'extra' }>()
const { t } = useI18n()
const { mode, botId, savedBot, form, fieldErrors, observeReady } = useBotFormContext()

// 没有「获取群组中所有消息」权限时飞书不推没 @ 它的群消息：开启前在这里查，缺了就地扫码补齐。
const permission = ref<ObservePermission | null>(null)
const permissionError = ref('')
const checking = ref(false)
const scanVisible = ref(false)
const requesting = ref(false)
// 只拦从关闭到打开；已经开着的照常可改，缺权限时只提示。
const savedOff = savedBot?.group_observe === 'off'
const blocked = (): boolean => savedOff && permission.value !== 'granted'

async function checkPermission(): Promise<void> {
  if (!botId) return
  checking.value = true
  permissionError.value = ''
  try {
    permission.value = (await feishuApps.observePermission(botId)).status
  } catch (e) {
    permission.value = null
    permissionError.value = errorMessage(e)
  } finally {
    checking.value = false
  }
}

async function requestGrant(): Promise<void> {
  if (!botId) return
  requesting.value = true
  try {
    await feishuApps.applyScopes(botId)
    ElMessage.success(t('feishuApp.applied'))
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    requesting.value = false
  }
}

async function onScanned(): Promise<void> {
  scanVisible.value = false
  await checkPermission()
}

onMounted(() => {
  if (savedBot?.platform === 'feishu' && mode === 'edit' && observeReady.value) void checkPermission()
})
</script>

<template>
  <h3
    v-if="!part"
    id="form-experience"
    class="cm-section-title"
  >
    <span>04</span>{{ t('workspace.experience') }}
  </h3>
  <el-form-item
    :label="t('bots.welcome')"
    data-test="welcome"
    :error="fieldErrors.welcome_message"
  >
    <el-input
      :model-value="form.welcome_message ?? ''"
      type="textarea"
      :rows="2"
      @update:model-value="form.welcome_message = ($event as string) || null"
    />
  </el-form-item>

  <el-form-item
    v-if="form.platform === 'feishu'"
    :label="t('bots.richCards')"
    data-test="rich-cards"
  >
    <el-switch v-model="form.rich_cards" />
    <div class="muted">
      {{ t('bots.richCardsHint') }}
    </div>
  </el-form-item>

  <template v-if="form.platform === 'feishu' && mode === 'edit'">
    <el-form-item
      :label="t('groupObserve.mode')"
      data-test="group-observe"
      :error="fieldErrors.group_observe"
    >
      <el-radio-group
        v-model="form.group_observe"
        :disabled="!observeReady && form.group_observe === 'off'"
      >
        <el-radio-button value="off">
          {{ t('groupObserve.modes.off') }}
        </el-radio-button>
        <el-radio-button
          value="shadow"
          :disabled="blocked()"
        >
          {{ t('groupObserve.modes.shadow') }}
        </el-radio-button>
        <el-radio-button
          value="on"
          :disabled="blocked()"
        >
          {{ t('groupObserve.modes.on') }}
        </el-radio-button>
      </el-radio-group>
      <div class="muted">
        {{ observeReady ? t('groupObserve.modeHint') : t('groupObserve.notReady') }}
      </div>
      <div
        v-if="observeReady"
        class="observe-permission"
        data-test="observe-permission"
      >
        <span
          v-if="checking"
          class="muted"
        >{{ t('groupObserve.permission.checking') }}</span>
        <el-alert
          v-else-if="permissionError"
          type="error"
          :closable="false"
          show-icon
          :title="t('groupObserve.permission.failed', { error: permissionError })"
        />
        <el-alert
          v-else-if="permission === 'granted'"
          type="success"
          :closable="false"
          show-icon
          :title="t('groupObserve.permission.granted')"
        />
        <el-alert
          v-else-if="permission === 'missing'"
          type="warning"
          :closable="false"
          show-icon
          :title="t('groupObserve.permission.missing')"
        >
          <el-button
            size="small"
            type="primary"
            data-test="observe-scan"
            @click="scanVisible = true"
          >
            {{ t('feishuApp.scanUpdate') }}
          </el-button>
        </el-alert>
        <el-alert
          v-else-if="permission === 'pending'"
          type="info"
          :closable="false"
          show-icon
          :title="t('groupObserve.permission.pending')"
        >
          <el-button
            size="small"
            :loading="requesting"
            data-test="observe-request"
            @click="requestGrant"
          >
            {{ t('feishuApp.applyScopes') }}
          </el-button>
          <el-button
            size="small"
            data-test="observe-recheck"
            @click="checkPermission"
          >
            {{ t('groupObserve.permission.recheck') }}
          </el-button>
        </el-alert>
      </div>
      <FeishuRegistrationDialog
        v-if="botId && scanVisible"
        v-model:visible="scanVisible"
        purpose="update"
        :bot-id="botId"
        observe
        @succeeded="onScanned"
      />
    </el-form-item>
    <el-form-item
      v-if="form.group_observe !== 'off'"
      :label="t('groupObserve.scope')"
      data-test="group-observe-scope"
      :error="fieldErrors.group_observe_scope"
    >
      <el-input
        v-model="form.group_observe_scope"
        type="textarea"
        :rows="3"
        maxlength="2000"
        :placeholder="t('groupObserve.scopePlaceholder')"
      />
      <div class="muted">
        {{ t('groupObserve.scopeHint') }}
      </div>
    </el-form-item>
  </template>

  <el-form-item
    :label="t('bots.devMode')"
    data-test="dev-mode"
  >
    <el-switch v-model="form.dev_mode" />
    <div class="muted">
      {{ t('bots.devModeHint') }}
    </div>
  </el-form-item>

  <el-form-item
    v-if="mode === 'create'"
    :label="t('bots.enabled')"
    data-test="enabled"
  >
    <el-switch v-model="form.enabled" />
  </el-form-item>
</template>

<style scoped>
.observe-permission { width: 100%; margin-top: 8px; }
.observe-permission .el-button { margin-top: 6px; }
</style>
