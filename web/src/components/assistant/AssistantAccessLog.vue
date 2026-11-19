<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { myAssistant, type AccessEntry } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t, te } = useI18n()
const items = ref<AccessEntry[] | null>(null)
const next = ref<number | null>(null)
const error = ref(''), loading = ref(false)

/** `more` 为真时接着上一页往更早取，否则从头取。 */
async function load(more = false) {
  loading.value = true; error.value = ''
  try {
    const page = await myAssistant.accessLog(more && next.value ? next.value : undefined)
    items.value = more && items.value ? [...items.value, ...page.items] : page.items
    next.value = page.next_before
  } catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
const who = (entry: AccessEntry) => (entry.actor_kind === 'admin'
  ? t('assistant.settings.accessWho.admin', { name: entry.actor_name ?? t('assistant.admin.none') })
  : t('assistant.settings.accessWho.' + entry.actor_kind))
/** 动作代码 → 文案；后端新加的、这里还没有文案的动作原样显示代码。 */
function what(entry: AccessEntry) {
  const key = 'assistant.settings.accessActions.' + entry.action.replace(/\./g, '_')
  return te(key) ? t(key) : entry.action
}
/** 按日期分组：同一天的放在一个日期标题下，行里只显示时分秒。 */
const days = computed(() => {
  const groups: { day: string; rows: { entry: AccessEntry; index: number; time: string }[] }[] = []
  ;(items.value ?? []).forEach((entry, index) => {
    const [day, time] = formatDateTime(entry.at).split(' ')
    const last = groups[groups.length - 1]
    if (last && last.day === day) last.rows.push({ entry, index, time })
    else groups.push({ day, rows: [{ entry, index, time }] })
  })
  return groups
})
/** 已取到的记录里，不是本人的访问有几次：放在标题旁边，一眼看到有没有别人碰过。 */
const foreign = computed(() => (items.value ?? []).filter((entry) => entry.actor_kind !== 'owner').length)
onMounted(() => load())
defineExpose({ load })
</script>
<template>
  <section class="access-log ma-group">
    <header class="ma-group__head">
      <h3>{{ t('assistant.settings.accessLog') }}</h3>
      <span
        v-if="items && items.length"
        class="ma-empty"
        :class="{ 'ma-danger': foreign > 0 }"
        data-test="access-foreign-count"
      >{{ t('assistant.settings.accessForeign', { n: foreign }) }}</span>
    </header>
    <p class="access-hint">
      {{ t('assistant.settings.accessLogHint') }}
    </p>
    <div class="access-body">
      <LoadState
        :loading="loading && !items"
        :error="error"
        @retry="load()"
      />
      <p
        v-if="items && !items.length"
        class="access-empty"
        data-test="access-empty"
      >
        {{ t('assistant.settings.accessEmpty') }}
      </p>
    </div>
    <template
      v-for="group in days"
      :key="group.day"
    >
      <h4 class="ma-subhead">
        {{ group.day }}
      </h4>
      <ul class="ma-list">
        <li
          v-for="row in group.rows"
          :key="row.entry.id"
          class="ma-row access-row"
          :class="{ 'is-foreign': row.entry.actor_kind !== 'owner' }"
          :data-test="'access-' + row.index"
        >
          <span class="access-time">{{ row.time }}</span>
          <!-- 不是所有者本人的访问用醒目的标签：这正是这张表要让他看见的。 -->
          <el-tag
            v-if="row.entry.actor_kind !== 'owner'"
            :type="row.entry.actor_kind === 'admin' ? 'danger' : 'warning'"
            size="small"
            :data-test="'access-foreign-' + row.index"
          >
            {{ who(row.entry) }}
          </el-tag>
          <span
            v-else
            class="access-who"
          >{{ who(row.entry) }}</span>
          <span class="access-what">{{ what(row.entry) }}</span>
          <code
            v-if="row.entry.target"
            class="access-target"
          >{{ row.entry.target }}</code>
        </li>
      </ul>
    </template>
    <div
      v-if="next"
      class="access-more"
    >
      <el-button
        link
        type="primary"
        :loading="loading"
        data-test="access-more"
        @click="load(true)"
      >
        {{ t('assistant.settings.accessMore') }}
      </el-button>
    </div>
  </section>
</template>
<style scoped>
.access-log { margin-top: 24px; max-width: 760px; }
.access-hint { margin: -4px 16px 12px; font-size: 12px; line-height: 20px; color: var(--el-text-color-secondary); }
.access-body:empty { display: none; }
.access-body { padding: 0 16px; }
.access-empty { margin: 0 0 12px; font-size: 13px; color: var(--el-text-color-placeholder); }
.access-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; padding: 8px 16px; font-size: 13px; }
.access-row.is-foreign { background: var(--el-color-warning-light-9); }
.access-time { flex-shrink: 0; width: 64px; font-family: var(--el-font-family-mono, ui-monospace, SFMono-Regular, Menlo, monospace); font-size: 12px; color: var(--el-text-color-secondary); font-variant-numeric: tabular-nums; }
.access-who { color: var(--el-text-color-secondary); }
.access-what { color: var(--el-text-color-primary); }
.access-target { margin-left: auto; max-width: 100%; padding: 1px 6px; border-radius: 4px; background: var(--el-fill-color-light); font-size: 12px; color: var(--el-text-color-regular); overflow-wrap: anywhere; }
.access-more { padding: 8px 16px; border-top: 1px solid var(--el-border-color-lighter); text-align: center; }
</style>
