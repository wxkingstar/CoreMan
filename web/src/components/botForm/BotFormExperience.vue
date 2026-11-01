<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { useBotFormContext } from '@/components/botForm/context'

defineProps<{ part?: 'extra' }>()
const { t } = useI18n()
const { mode, form, fieldErrors, observeReady } = useBotFormContext()
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
        <el-radio-button value="shadow">
          {{ t('groupObserve.modes.shadow') }}
        </el-radio-button>
        <el-radio-button value="on">
          {{ t('groupObserve.modes.on') }}
        </el-radio-button>
      </el-radio-group>
      <div class="muted">
        {{ observeReady ? t('groupObserve.modeHint') : t('groupObserve.notReady') }}
      </div>
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
