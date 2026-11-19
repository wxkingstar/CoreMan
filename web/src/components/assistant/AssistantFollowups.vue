<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { FOLLOWUP_CATEGORIES, myAssistant, type Followup, type FollowupCategory, type FollowupList } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import { askSnoozeDate } from '@/utils/assistantFollowups'
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
const snooze = (item: Followup) => act(async () => {
  const value = await askSnoozeDate(t, data.value?.today ?? '')
  await myAssistant.snoozeFollowup(item.id, value)
}, 'assistant.followups.snoozed')
/** 备注默认露两行，长的点开看全文。 */
const expanded = reactive(new Set<string>())
function toggle(id: string) { if (expanded.has(id)) expanded.delete(id); else expanded.add(id) }
const isLong = (text: string) => text.split('\n').length > 2 || text.length > 160
/** 期限到了或过了标红。 */
const overdue = (item: Followup) => !!item.due_on && !!data.value && item.due_on < data.value.today
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
      <div class="ma-toolbar followup-add">
        <el-select
          v-model="form.category"
          :aria-label="t('assistant.followups.category')"
          style="width: 130px"
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
          :placeholder="t('assistant.followups.titlePlaceholder')"
          maxlength="200"
          class="followup-title"
          @keyup.enter="add"
        />
        <el-date-picker
          v-model="form.due_on"
          type="date"
          value-format="YYYY-MM-DD"
          :placeholder="t('assistant.followups.dueOn')"
          style="width: 150px"
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
      <section
        v-for="category in FOLLOWUP_CATEGORIES"
        :key="category"
        class="ma-group"
        :class="{ 'is-empty': !data.groups[category].length }"
      >
        <header class="ma-group__head">
          <h3>{{ t('assistant.followups.categories.' + category) }}</h3>
          <span
            class="ma-count"
            :class="{ 'is-primary': category === 'decide' && data.groups[category].length > 0 }"
          >{{ data.groups[category].length }}</span>
          <span
            v-if="!data.groups[category].length"
            class="ma-empty"
          >{{ t('assistant.followups.empty') }}</span>
        </header>
        <ul
          v-if="data.groups[category].length"
          class="ma-list"
        >
          <li
            v-for="item in data.groups[category]"
            :key="item.id"
            class="ma-row ma-row--check"
            :data-test="'item-' + item.id"
          >
            <div class="ma-row__main">
              <!-- 勾上即办完：受控为未勾选，办完后这一条随列表刷新消失。 -->
              <el-tooltip
                :content="t('assistant.followups.done')"
                placement="top"
              >
                <el-checkbox
                  :model-value="false"
                  :disabled="busy"
                  :aria-label="t('assistant.followups.done')"
                  :data-test="'done-' + item.id"
                  @change="finish(item)"
                />
              </el-tooltip>
              <span class="ma-id">{{ item.id }}</span>
              <span class="ma-title">
                {{ item.locked ? t('assistant.page.lockedText') : item.title }}
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
              </span>
              <span class="ma-actions">
                <el-select
                  :model-value="item.category"
                  size="small"
                  :aria-label="t('assistant.followups.category')"
                  style="width: 104px"
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
                  size="small"
                  :data-test="'snooze-' + item.id"
                  @click="snooze(item)"
                >
                  {{ t('assistant.followups.snooze') }}
                </el-button>
                <el-button
                  link
                  type="danger"
                  size="small"
                  :data-test="'drop-' + item.id"
                  @click="drop(item)"
                >
                  {{ t('assistant.followups.drop') }}
                </el-button>
              </span>
            </div>
            <div
              v-if="item.due_on || (item.status !== 'snoozed' && item.review_on)"
              class="ma-meta"
            >
              <span
                v-if="item.due_on"
                :class="{ 'ma-danger': overdue(item) }"
              >{{ t('assistant.followups.due', { time: item.due_on }) }}</span>
              <span
                v-if="item.status !== 'snoozed' && item.review_on"
              >{{ t('assistant.followups.review', { time: item.review_on }) }}</span>
            </div>
            <div
              v-if="item.notes"
              class="ma-detail"
            >
              <pre
                class="ma-text"
                :class="{ 'ma-clamp is-2': isLong(item.notes) && !expanded.has(item.id) }"
              >{{ item.notes }}</pre>
              <button
                v-if="isLong(item.notes)"
                type="button"
                class="ma-toggle"
                :data-test="'toggle-' + item.id"
                @click="toggle(item.id)"
              >
                {{ expanded.has(item.id) ? t('assistant.page.collapse') : t('assistant.page.expand') }}
              </button>
            </div>
          </li>
        </ul>
      </section>
    </template>
  </section>
</template>
<style scoped>
.followup-title { flex: 1 1 280px; max-width: 520px; }
</style>
