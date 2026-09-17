<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { selfReminders, type SelfReminder } from '@/api/selfReminders'
const { t, locale } = useI18n()
const rows = ref<SelfReminder[]>([])
const error = ref('')
const busy = ref(false)
async function load() {
  busy.value = true
  try { rows.value = await selfReminders.list(); error.value = '' }
  catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { busy.value = false }
}
async function cancel(id: string) {
  busy.value = true
  try { await selfReminders.cancel(id); await load() }
  catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { busy.value = false }
}
function time(value: string) {
  return new Date(value).toLocaleString(locale.value, { timeZone: 'Asia/Shanghai', hour12: false }) + ' (UTC+08:00)'
}
onMounted(load)
</script>
<template>
  <section class="reminders">
    <h1>{{ t('menu.selfReminders') }}</h1>
    <p>{{ t('selfReminders.hint') }}</p>
    <p>{{ t('selfReminders.deliveryHint') }}</p>
    <el-button
      :loading="busy"
      @click="load"
    >
      {{ t('selfReminders.refresh') }}
    </el-button>
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      :closable="false"
    />
    <el-empty
      v-if="!busy && !rows.length"
      :description="t('selfReminders.empty')"
    />
    <article
      v-for="row in rows"
      :key="row.id"
      class="reminder"
    >
      <strong>{{ row.bot_name }} · {{ t('selfReminders.fixed') }}</strong>
      <p>{{ time(row.run_at) }}</p>
      <p class="text">
        {{ row.text }}
      </p>
      <p>
        {{ t('selfReminders.status.' + row.status) }}<span
          v-for="(state, index) in row.deliveries"
          :key="index"
        > · {{ t('selfReminders.status.' + state) }}</span>
      </p>
      <el-button
        v-if="row.can_cancel"
        :disabled="busy"
        :data-test="'cancel-' + row.id"
        @click="cancel(row.id)"
      >
        {{ t('selfReminders.cancel') }}
      </el-button>
    </article>
  </section>
</template>
<style scoped>
.reminders { max-width: 880px; margin: 0 auto; }
.reminders > p { color: var(--el-text-color-secondary); line-height: 1.7; }
.reminder { margin-top: 16px; padding: 20px; border: 1px solid var(--el-border-color); border-radius: 12px; }
.text { white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
