<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { FOLLOWUP_CATEGORIES, myAssistant, type Followup, type FollowupCategory, type FollowupList } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'

const { t } = useI18n()
const data = ref<FollowupList | null>(null)
const error = ref(''), loading = ref(false), busy = ref(false)
const form = reactive<{ category: FollowupCategory; title: string; due_on: string }>({ category: 'remind', title: '', due_on: '' })

async function load() {
  loading.value = true; error.value = ''
  try { data.value = await myAssistant.followups() }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
/** 做一件事、提示、重新取列表；取消确认框不算失败。 */
async function act(action: () => Promise<unknown>, done: string) {
  busy.value = true
  try { await action(); ElMessage.success(t(done)); await load() }
  catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorMessage(e)) }
  finally { busy.value = false }
}
async function add() {
  const title = form.title.trim()
  if (!title) { ElMessage.warning(t('assistant.followups.titleRequired')); return }
  await act(async () => {
    await myAssistant.addFollowup({ category: form.category, title, ...(form.due_on ? { due_on: form.due_on } : {}) })
    form.title = ''; form.due_on = ''
  }, 'assistant.followups.added')
}
const finish = (item: Followup) => act(() => myAssistant.closeFollowup(item.id, false), 'assistant.followups.closed')
const drop = (item: Followup) => act(async () => {
  await ElMessageBox.confirm(t('assistant.followups.dropConfirm', { name: item.id }), t('assistant.followups.drop'), { type: 'warning' })
  await myAssistant.closeFollowup(item.id, true)
}, 'assistant.followups.closed')
/** 日期串是 YYYY-MM-DD，按字典序比就是按日期比；「今天」以后端给的所有者时区的今天为准。 */
const laterThanToday = (value: string) => /^\d{4}-\d{2}-\d{2}$/.test(value ?? '') && value > (data.value?.today ?? '')
const snooze = (item: Followup) => act(async () => {
  const { value } = await ElMessageBox.prompt(t('assistant.followups.snoozePrompt'), t('assistant.followups.snooze'), {
    inputType: 'date',
    inputValidator: (input: string) => laterThanToday(input) || t('assistant.followups.snoozeInvalid'),
  })
  await myAssistant.snoozeFollowup(item.id, value)
}, 'assistant.followups.snoozed')
async function recategorize(item: Followup, category: FollowupCategory) {
  if (category === item.category) return
  await act(() => myAssistant.updateFollowup(item.id, { category }), 'assistant.followups.updated')
}
onMounted(load)
defineExpose({ form, add, recategorize, load })
</script>
<template>
  <section class="assistant-section">
    <LoadState
      :loading="loading && !data"
      :error="error"
      @retry="load"
    />
    <template v-if="data">
      <div class="followup-add">
        <el-select
          v-model="form.category"
          :aria-label="t('assistant.followups.category')"
          style="width: 140px"
        >
          <el-option
            v-for="category in FOLLOWUP_CATEGORIES"
            :key="category"
            :value="category"
            :label="t('assistant.followups.categories.' + category)"
          />
        </el-select>
        <el-input
          v-model="form.title"
          :placeholder="t('assistant.followups.title')"
          maxlength="200"
          style="width: 320px"
        />
        <el-date-picker
          v-model="form.due_on"
          type="date"
          value-format="YYYY-MM-DD"
          :placeholder="t('assistant.followups.dueOn')"
          style="width: 160px"
        />
        <el-button
          type="primary"
          :loading="busy"
          data-test="add"
          @click="add"
        >
          {{ t('assistant.followups.add') }}
        </el-button>
      </div>
      <template
        v-for="category in FOLLOWUP_CATEGORIES"
        :key="category"
      >
        <h3>{{ t('assistant.followups.categories.' + category) }}（{{ data.groups[category].length }}）</h3>
        <p
          v-if="!data.groups[category].length"
          class="muted"
        >
          {{ t('assistant.followups.empty') }}
        </p>
        <ul v-else>
          <li
            v-for="item in data.groups[category]"
            :key="item.id"
            :data-test="'item-' + item.id"
          >
            <!-- 勾上即办完：受控为未勾选，办完后这一条随列表刷新消失。 -->
            <el-checkbox
              :model-value="false"
              :disabled="busy"
              :aria-label="t('assistant.followups.done')"
              :data-test="'done-' + item.id"
              @change="finish(item)"
            />
            <code>{{ item.id }}</code>
            <strong>{{ item.locked ? t('assistant.page.lockedText') : item.title }}</strong>
            <el-tag
              v-if="item.due_today"
              size="small"
              type="danger"
            >
              {{ t('assistant.followups.dueToday') }}
            </el-tag>
            <el-tag
              v-if="item.status === 'snoozed' && item.review_on"
              size="small"
              type="info"
            >
              {{ t('assistant.followups.snoozedUntil', { time: item.review_on }) }}
            </el-tag>
            <span
              v-if="item.due_on"
              class="muted"
            >{{ t('assistant.followups.due', { time: item.due_on }) }}</span>
            <span
              v-if="item.status !== 'snoozed' && item.review_on"
              class="muted"
            >{{ t('assistant.followups.review', { time: item.review_on }) }}</span>
            <span
              v-if="item.notes"
              class="followup-notes"
            >{{ item.notes }}</span>
            <span class="followup-actions">
              <el-select
                :model-value="item.category"
                size="small"
                :aria-label="t('assistant.followups.category')"
                style="width: 120px"
                @change="recategorize(item, $event as FollowupCategory)"
              >
                <el-option
                  v-for="option in FOLLOWUP_CATEGORIES"
                  :key="option"
                  :value="option"
                  :label="t('assistant.followups.categories.' + option)"
                />
              </el-select>
              <el-button
                link
                type="primary"
                :data-test="'snooze-' + item.id"
                @click="snooze(item)"
              >
                {{ t('assistant.followups.snooze') }}
              </el-button>
              <el-button
                link
                type="danger"
                :data-test="'drop-' + item.id"
                @click="drop(item)"
              >
                {{ t('assistant.followups.drop') }}
              </el-button>
            </span>
          </li>
        </ul>
      </template>
    </template>
  </section>
</template>
<style scoped>
.followup-add { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
.assistant-section ul { list-style: none; padding: 0; }
.assistant-section li { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.followup-notes { flex-basis: 100%; white-space: pre-wrap; color: var(--el-text-color-regular); }
.followup-actions { margin-left: auto; display: flex; gap: 4px; align-items: center; }
.muted { color: var(--el-text-color-secondary); }
</style>
