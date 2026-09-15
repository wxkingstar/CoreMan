<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { Platform } from '@/api/types'
import { useBotFormContext } from '@/components/botForm/context'

const PLATFORMS: Platform[] = ['wecom', 'feishu']
const { t } = useI18n()
const { mode, form, fieldErrors, isManager, teamList, onBotKeyInput, onPlatformChange } = useBotFormContext()
</script>

<template>
  <h3
    id="form-identity"
    class="cm-section-title"
  >
    <span>01</span>{{ t('workspace.identity') }}
  </h3>
  <el-form-item
    :label="t('bots.botKey')"
    data-test="bot_key"
    :error="fieldErrors.bot_key"
  >
    <el-input
      v-model="form.bot_key"
      :disabled="mode === 'edit'"
      @input="onBotKeyInput"
    />
    <div class="muted">
      {{ t('bots.botKeyHint') }}
    </div>
  </el-form-item>

  <el-form-item
    :label="t('bots.platform')"
    data-test="platform"
    :error="fieldErrors.platform"
  >
    <el-select
      v-model="form.platform"
      :disabled="mode === 'edit'"
      style="width: 200px"
      @change="onPlatformChange"
    >
      <el-option
        v-for="p in PLATFORMS"
        :key="p"
        :label="t(`platforms.${p}`)"
        :value="p"
      />
    </el-select>
  </el-form-item>

  <el-form-item
    :label="t('bots.name')"
    data-test="name"
    :error="fieldErrors.name"
  >
    <el-input v-model="form.name" />
  </el-form-item>

  <el-form-item
    :label="t('bots.description')"
    data-test="description"
    :error="fieldErrors.description"
  >
    <el-input
      v-model="form.description"
      type="textarea"
      :rows="2"
    />
  </el-form-item>

  <el-form-item
    v-if="isManager"
    :label="t('bots.team')"
    data-test="team"
    :error="fieldErrors.team_id"
  >
    <el-select
      :model-value="form.team_id"
      clearable
      :placeholder="t('bots.team')"
      style="width: 240px"
      @update:model-value="form.team_id = ($event as string | undefined) ?? null"
    >
      <el-option
        v-for="tm in teamList"
        :key="tm.id"
        :label="tm.name_zh"
        :value="tm.id"
      />
    </el-select>
  </el-form-item>
</template>

<style scoped>
.muted { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
</style>
