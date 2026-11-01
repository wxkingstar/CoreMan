<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import LoadState from '@/components/LoadState.vue'
import { ElMessage } from 'element-plus'
import { onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { groupObserve, type ObserveChat, type ObserveDecision } from '@/api/groupObserve'
import { formatDateTime } from '@/utils/format'

const props = defineProps<{ botId: string; mode: string }>()
const { t, te } = useI18n()
const loading = ref(false)
const loadError = ref('')
const rows = ref<ObserveDecision[]>([])
const chats = ref<ObserveChat[]>([])
const chatId = ref('')
const onlySpeak = ref(true)
const more = ref(false)
const PAGE = 50

async function load(append = false) {
  loading.value = true
  loadError.value = ''
  try {
    const data = await groupObserve.decisions(props.botId, {
      chat_id: chatId.value || undefined,
      would_speak: onlySpeak.value ? true : undefined,
      before_id: append ? rows.value[rows.value.length - 1]?.id : undefined,
      limit: PAGE,
    })
    rows.value = append ? [...rows.value, ...data.items] : data.items
    chats.value = data.chats
    more.value = data.items.length === PAGE
  } catch (error) {
    loadError.value = errorMessage(error)
  } finally {
    loading.value = false
  }
}
onMounted(() => { void load() })
watch([chatId, onlySpeak], () => { void load() })

async function review(row: ObserveDecision, value: ObserveDecision['review']) {
  try {
    const saved = await groupObserve.review(props.botId, row.id, row.review === value ? null : value)
    row.review = saved.review
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

function outcome(value: string): string {
  const key = `groupObserve.outcomes.${value}`
  return te(key) ? t(key) : value
}
</script>

<template>
  <section class="cm-panel">
    <h3>{{ t('groupObserve.title') }}</h3>
    <p class="panel-hint">
      {{ mode === 'shadow' ? t('groupObserve.shadowHint') : t('groupObserve.panelHint') }}
    </p>
    <div class="observe-toolbar">
      <el-select
        v-model="chatId"
        clearable
        :placeholder="t('groupObserve.allChats')"
        style="width: 280px"
      >
        <el-option
          v-for="chat in chats"
          :key="chat.chat_id"
          :value="chat.chat_id"
          :label="t('groupObserve.chatLabel', { id: chat.chat_id, speak: chat.would_speak, total: chat.total })"
        />
      </el-select>
      <el-checkbox v-model="onlySpeak">
        {{ t('groupObserve.onlySpeak') }}
      </el-checkbox>
      <el-button @click="load()">
        {{ t('common.refresh') }}
      </el-button>
    </div>
    <LoadState
      :error="loadError"
      @retry="load()"
    />
    <el-table
      v-loading="loading"
      :data="rows"
      row-key="id"
    >
      <el-table-column type="expand">
        <template #default="{ row }">
          <ol class="observe-messages">
            <li
              v-for="(line, index) in row.messages"
              :key="index"
              :class="`kind-${line.sender_kind}`"
            >
              <strong>{{ line.sender }}</strong>
              <span v-if="line.sender_kind !== 'human'"> · {{ t(`groupObserve.kinds.${line.sender_kind}`) }}</span>：{{ line.text }}
            </li>
          </ol>
          <el-empty
            v-if="!row.messages.length"
            :description="t('groupObserve.noMessages')"
          />
        </template>
      </el-table-column>
      <el-table-column
        :label="t('groupObserve.time')"
        min-width="160"
      >
        <template #default="{ row }">
          {{ formatDateTime(row.created_at) }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('groupObserve.outcome')"
        min-width="140"
      >
        <template #default="{ row }">
          <el-tag :type="row.would_speak ? 'warning' : 'info'">
            {{ outcome(row.outcome) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('groupObserve.probability')"
        width="90"
      >
        <template #default="{ row }">
          {{ row.probability === null ? '—' : row.probability.toFixed(2) }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('groupObserve.lastMessage')"
        min-width="240"
        show-overflow-tooltip
      >
        <template #default="{ row }">
          {{ row.messages[row.messages.length - 1]?.text ?? '' }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('groupObserve.review')"
        width="190"
      >
        <template #default="{ row }">
          <el-button
            size="small"
            :type="row.review === 'should' ? 'success' : 'default'"
            @click="review(row, 'should')"
          >
            {{ t('groupObserve.should') }}
          </el-button>
          <el-button
            size="small"
            :type="row.review === 'should_not' ? 'danger' : 'default'"
            @click="review(row, 'should_not')"
          >
            {{ t('groupObserve.shouldNot') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-button
      v-if="more"
      class="observe-more"
      link
      type="primary"
      @click="load(true)"
    >
      {{ t('groupObserve.loadMore') }}
    </el-button>
  </section>
</template>

<style scoped>
.observe-toolbar { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 12px; }
.observe-messages { margin: 0; padding: 4px 16px 4px 32px; display: grid; gap: 4px; line-height: 1.6; }
.observe-messages .kind-self { color: var(--el-color-primary); }
.observe-messages .kind-bot { color: var(--el-text-color-secondary); }
.observe-more { margin-top: 8px; }
</style>
