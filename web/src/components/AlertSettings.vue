<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { alerts, type AlertChannel } from '@/api/alerts'
import { platformApps, users } from '@/api/admin'
import type { Page, PlatformAppOut, UserOut } from '@/api/types'
const { t } = useI18n()
const channels = ref<AlertChannel[]>([]), version = ref(0), loaded = ref(false), saving = ref(false)
const apps = ref<PlatformAppOut[]>([]), people = ref<UserOut[]>([])
function fail(e: unknown) { ElMessage.error(errorMessage(e)) }
async function pages<T>(fetch: (page: number) => Promise<Page<T>>): Promise<T[]> {
  const all: T[] = []
  for (let page = 1; page <= 100; page++) {
    const result = await fetch(page)
    all.push(...result.items)
    if (all.length >= result.total) return all
    if (!result.items.length) break
  }
  throw new Error(t('alerts.incomplete'))
}
onMounted(async () => {
  try {
    const [config, applications, admins, committee] = await Promise.all([
      alerts.get(), pages((page) => platformApps.list({ page, per_page: 100 })),
      pages((page) => users.list({ page, per_page: 100, role: 'platform_admin', status: 'active' })),
      pages((page) => users.list({ page, per_page: 100, role: 'ai_committee', status: 'active' })),
    ])
    channels.value = config.channels; version.value = config.version
    apps.value = applications.filter((app) => app.enabled && app.capabilities.includes('notify'))
    people.value = [...admins, ...committee].filter((user) => user.source !== 'bootstrap')
    loaded.value = true
  } catch (e) { fail(e) }
})
async function save() {
  saving.value = true
  try {
    const config = await alerts.save(channels.value, version.value)
    version.value = config.version; channels.value = config.channels
    ElMessage.success(t('common.saved'))
  } catch (e) { fail(e) } finally { saving.value = false }
}
</script>
<template>
  <el-card class="alert-settings">
    <template #header>
      {{ t('alerts.title') }}
    </template>
    <p>{{ t('alerts.hint') }}</p>
    <el-form
      :disabled="!loaded || saving"
      label-position="top"
      @submit.prevent="save"
    >
      <div
        v-for="(channel, index) in channels"
        :key="index"
        class="alert-row"
      >
        <el-form-item :label="t('alerts.app')">
          <el-select
            v-model="channel.platform_app_id"
            filterable
          >
            <el-option
              v-for="app in apps"
              :key="app.id"
              :label="`${app.name} (${t(`platforms.${app.platform}`)})`"
              :value="app.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('alerts.person')">
          <el-select
            v-model="channel.user_id"
            filterable
          >
            <el-option
              v-for="user in people"
              :key="user.id"
              :label="user.display_name"
              :value="user.id"
            />
          </el-select>
        </el-form-item>
        <el-button @click="channels.splice(index, 1)">
          {{ t('common.delete') }}
        </el-button>
      </div>
      <el-empty
        v-if="!channels.length"
        :description="t('alerts.none')"
        :image-size="45"
      />
      <el-button
        :disabled="channels.length >= 20"
        @click="channels.push({ platform_app_id: '', user_id: '' })"
      >
        {{ t('alerts.add') }}
      </el-button>
      <el-button
        type="primary"
        native-type="submit"
        :loading="saving"
      >
        {{ t('common.save') }}
      </el-button>
    </el-form>
  </el-card>
</template>
<style scoped>
.alert-settings { margin-top: 18px; }
.alert-row { display: grid; grid-template-columns: 1fr 1fr auto; gap: 16px; align-items: center; }
@media (max-width: 600px) { .alert-row { grid-template-columns: 1fr; gap: 0; } }
</style>
