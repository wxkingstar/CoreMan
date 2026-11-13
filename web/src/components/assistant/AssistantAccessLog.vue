<script setup lang="ts">
import { onMounted, ref } from 'vue'
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
onMounted(() => load())
defineExpose({ load })
</script>
<template>
  <section class="access-log">
    <h3>{{ t('assistant.settings.accessLog') }}</h3>
    <p class="muted">
      {{ t('assistant.settings.accessLogHint') }}
    </p>
    <LoadState
      :loading="loading && !items"
      :error="error"
      @retry="load()"
    />
    <p
      v-if="items && !items.length"
      class="muted"
      data-test="access-empty"
    >
      {{ t('assistant.settings.accessEmpty') }}
    </p>
    <ul v-if="items && items.length">
      <li
        v-for="(entry, i) in items"
        :key="entry.id"
        :data-test="'access-' + i"
      >
        <span class="muted">{{ formatDateTime(entry.at) }}</span>
        <!-- 不是所有者本人的访问用醒目的标签：这正是这张表要让他看见的。 -->
        <el-tag
          v-if="entry.actor_kind !== 'owner'"
          type="warning"
          :data-test="'access-foreign-' + i"
        >
          {{ who(entry) }}
        </el-tag>
        <span v-else>{{ who(entry) }}</span>
        <span>{{ what(entry) }}</span>
        <code v-if="entry.target">{{ entry.target }}</code>
      </li>
    </ul>
    <el-button
      v-if="next"
      link
      type="primary"
      :loading="loading"
      data-test="access-more"
      @click="load(true)"
    >
      {{ t('assistant.settings.accessMore') }}
    </el-button>
  </section>
</template>
<style scoped>
.access-log { margin-top: 24px; max-width: 760px; }
.access-log ul { list-style: none; padding: 0; }
.access-log li { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; padding: 6px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.muted { color: var(--el-text-color-secondary); }
</style>
