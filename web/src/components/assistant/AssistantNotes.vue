<script setup lang="ts">
import { Plus } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { myAssistant, type NoteEntry, type NoteIndex, type NotePage } from '@/api/myAssistant'
import AssistantTransfer from '@/components/assistant/AssistantTransfer.vue'
import LoadState from '@/components/LoadState.vue'
import { errorMessage, isVersionConflict } from '@/utils/errors'
import { formatBytes, formatDateTime } from '@/utils/format'

const { t } = useI18n()
const index = ref<NoteIndex | null>(null)
const current = ref<NotePage | null>(null)
const error = ref(''), loading = ref(false), busy = ref(false)
const editor = reactive({ open: false, isNew: false, path: '', title: '', summary: '', body: '', pinned: false, version: 0 })

async function load() {
  loading.value = true; error.value = ''
  try { index.value = await myAssistant.notes() }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
function fill(page: NotePage) {
  Object.assign(editor, { open: true, isNew: false, path: page.path, title: page.title, summary: page.summary, body: page.body, pinned: page.pinned, version: page.version })
}
async function open(entry: NoteEntry) {
  try { current.value = await myAssistant.page(entry.path); editor.open = false }
  catch (e) { ElMessage.error(errorMessage(e)) }
}
async function edit(entry: NoteEntry) {
  try { const page = await myAssistant.page(entry.path); current.value = page; fill(page) }
  catch (e) { ElMessage.error(errorMessage(e)) }
}
function create() {
  current.value = null
  Object.assign(editor, { open: true, isNew: true, path: '', title: '', summary: '', body: '', pinned: false, version: 0 })
}
async function save() {
  const path = editor.path.trim()
  if (!path || !editor.body.trim()) { ElMessage.warning(t('assistant.notes.required')); return }
  busy.value = true
  try {
    await myAssistant.savePage({ path, body: editor.body, summary: editor.summary, title: editor.title.trim() || null, pinned: editor.pinned, version: editor.version })
    ElMessage.success(t('assistant.notes.saved'))
    editor.open = false; current.value = null
    await load()
  } catch (e) {
    if (isVersionConflict(e) && !editor.isNew) {
      // 助理在这期间写过这一页：换成最新内容，让所有者看过再存，不拿旧版本盖掉。
      ElMessage.warning(t('assistant.notes.conflict'))
      try { fill(await myAssistant.page(path)) } catch (inner) { ElMessage.error(errorMessage(inner)) }
    } else ElMessage.error(errorMessage(e))
  } finally { busy.value = false }
}
/** 取消确认框不算失败。 */
async function remove(entry: NoteEntry) {
  try {
    await ElMessageBox.confirm(t('assistant.notes.removeConfirm', { name: entry.path }), t('assistant.notes.remove'), { type: 'warning' })
    await myAssistant.deletePage(entry.path)
    ElMessage.success(t('assistant.notes.deleted'))
    if (current.value?.path === entry.path) current.value = null
    if (editor.path === entry.path) editor.open = false
    await load()
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorMessage(e)) }
}
/** 重新生成口吻页：后端在冷启动没做完、当天已满 3 次、口吻页是所有者自己写的时拒绝并说明原因。 */
const regenerating = ref(false)
async function regenerate() {
  try {
    await ElMessageBox.confirm(t('assistant.notes.regenerateConfirm'), t('assistant.notes.regenerate'), { type: 'info' })
  } catch { return }
  regenerating.value = true
  try {
    const done = await myAssistant.regenerateVoice()
    ElMessage.success(t('assistant.notes.regenerated', { count: done.left_today }))
  } catch (e) { ElMessage.error(errorMessage(e)) }
  finally { regenerating.value = false }
}
/** 查看与编辑都在右侧抽屉里：不用滚到列表底下找，关掉回到原来的位置。 */
const drawerOpen = computed({
  get: () => editor.open || !!current.value,
  set: (value: boolean) => { if (!value) { editor.open = false; current.value = null } },
})
const drawerTitle = computed(() => {
  if (editor.open) return editor.isNew ? t('assistant.notes.create') : t('assistant.notes.editing', { name: editor.path })
  return current.value ? (current.value.title || current.value.path) : ''
})
function editCurrent() { if (current.value) fill(current.value) }
onMounted(load)
defineExpose({ editor, save, load })
</script>
<template>
  <section class="assistant-section">
    <p class="notes-intro">
      {{ t('assistant.notes.intro') }}
    </p>
    <LoadState
      :loading="loading && !index"
      :error="error"
      @retry="load"
    />
    <template v-if="index">
      <el-alert
        v-if="index.locked"
        type="warning"
        :closable="false"
        show-icon
        data-test="notes-locked"
        :title="t('assistant.page.locked')"
      />
      <div
        v-else
        class="ma-toolbar notes-toolbar"
      >
        <el-button
          type="primary"
          :icon="Plus"
          data-test="create"
          @click="create"
        >
          {{ t('assistant.notes.create') }}
        </el-button>
        <el-button
          :loading="regenerating"
          data-test="regenerate-voice"
          @click="regenerate"
        >
          {{ t('assistant.notes.regenerate') }}
        </el-button>
        <span class="ma-spacer" />
        <!-- 密钥不可用时内容读不出来也写不进去：不给导入导出。导入完成后重新取索引。 -->
        <AssistantTransfer @imported="load" />
      </div>
      <section
        v-if="!index.locked || index.items.length"
        class="ma-group"
        :class="{ 'is-empty': !index.items.length }"
      >
        <header class="ma-group__head">
          <h3>{{ t('assistant.page.tabs.notes') }}</h3>
          <span class="ma-count">{{ index.items.length }}</span>
          <span
            v-if="!index.items.length"
            class="ma-empty"
          >{{ t('assistant.notes.empty') }}</span>
        </header>
        <ul
          v-if="index.items.length"
          class="ma-list"
        >
          <li
            v-for="(entry, i) in index.items"
            :key="entry.path"
            class="ma-row"
            :class="{ 'is-current': current?.path === entry.path || editor.path === entry.path && editor.open }"
            :data-test="'page-' + i"
          >
            <div class="ma-row__main">
              <span class="ma-title">
                <button
                  type="button"
                  class="note-link"
                  @click="open(entry)"
                >{{ entry.title || entry.path }}</button>
                <!-- 置顶页每天第一轮原样交给助理：用醒目的实心标签，所有者一眼看得到置顶了什么。 -->
                <el-tag
                  v-if="entry.pinned"
                  type="warning"
                  effect="dark"
                  size="small"
                  :title="t('assistant.notes.pinnedHint')"
                  :data-test="'pinned-' + i"
                >
                  {{ t('assistant.notes.pinned') }}
                </el-tag>
              </span>
              <span class="ma-actions">
                <el-button
                  link
                  type="primary"
                  size="small"
                  :data-test="'open-' + i"
                  @click="open(entry)"
                >
                  {{ t('assistant.notes.open') }}
                </el-button>
                <el-button
                  link
                  type="primary"
                  size="small"
                  :data-test="'edit-' + i"
                  @click="edit(entry)"
                >
                  {{ t('assistant.notes.edit') }}
                </el-button>
                <el-button
                  link
                  type="danger"
                  size="small"
                  :data-test="'remove-' + i"
                  @click="remove(entry)"
                >
                  {{ t('assistant.notes.remove') }}
                </el-button>
              </span>
            </div>
            <div
              v-if="entry.summary"
              class="ma-detail"
            >
              {{ entry.summary }}
            </div>
            <div class="ma-meta">
              <code>{{ entry.path }}</code>
              <span>{{ t('assistant.notes.updatedBy', { name: t('assistant.notes.authors.' + entry.updated_by) }) }}</span>
              <span>{{ t('assistant.notes.updatedAt', { time: formatDateTime(entry.updated_at) }) }}</span>
              <span>{{ formatBytes(entry.size) }}</span>
            </div>
          </li>
        </ul>
      </section>
    </template>
    <el-drawer
      v-model="drawerOpen"
      :title="drawerTitle"
      size="min(760px, 100vw)"
      class="note-drawer"
      data-test="note-drawer"
    >
      <el-form
        v-if="editor.open"
        label-position="top"
        data-test="editor"
        class="note-editor"
        @submit.prevent
      >
        <el-form-item :label="t('assistant.notes.path')">
          <el-input
            v-model="editor.path"
            :disabled="!editor.isNew"
            :placeholder="t('assistant.notes.pathHint')"
            maxlength="200"
          />
        </el-form-item>
        <el-form-item :label="t('assistant.notes.title')">
          <el-input
            v-model="editor.title"
            maxlength="120"
          />
        </el-form-item>
        <el-form-item :label="t('assistant.notes.summary')">
          <el-input
            v-model="editor.summary"
            maxlength="300"
          />
        </el-form-item>
        <el-form-item :label="t('assistant.notes.pinned')">
          <el-switch v-model="editor.pinned" />
          <span class="note-hint">{{ t('assistant.notes.pinnedHint') }}</span>
        </el-form-item>
        <el-form-item :label="t('assistant.notes.body')">
          <el-input
            v-model="editor.body"
            type="textarea"
            :rows="18"
          />
        </el-form-item>
      </el-form>
      <article
        v-else-if="current"
        data-test="viewer"
        class="note-viewer"
      >
        <div class="ma-meta">
          <code>{{ current.path }}</code>
          <span>{{ t('assistant.notes.version', { n: current.version }) }}</span>
          <span>{{ t('assistant.notes.updatedAt', { time: formatDateTime(current.updated_at) }) }}</span>
        </div>
        <!-- 正文按纯文本显示：内容可能来自别人发来的消息，不当成 HTML 或 Markdown 渲染。 -->
        <pre class="ma-text note-body">{{ current.body }}</pre>
      </article>
      <template #footer>
        <template v-if="editor.open">
          <span
            v-if="!editor.isNew"
            class="note-hint"
          >{{ t('assistant.notes.version', { n: editor.version }) }}</span>
          <el-button @click="drawerOpen = false">
            {{ t('assistant.notes.cancel') }}
          </el-button>
          <el-button
            type="primary"
            :loading="busy"
            data-test="save-page"
            @click="save"
          >
            {{ t('assistant.notes.save') }}
          </el-button>
        </template>
        <el-button
          v-else-if="current && !index?.locked"
          type="primary"
          data-test="viewer-edit"
          @click="editCurrent"
        >
          {{ t('assistant.notes.edit') }}
        </el-button>
      </template>
    </el-drawer>
  </section>
</template>
<style scoped>
.notes-intro { margin: 0 0 12px; font-size: 13px; line-height: 22px; color: var(--el-text-color-secondary); max-width: 90ch; }
.notes-toolbar :deep(.transfer) { display: contents; }
.notes-toolbar :deep(.import-panel) { flex-basis: 100%; }
.note-link { padding: 0; border: 0; background: none; font: inherit; color: inherit; text-align: left; cursor: pointer; }
.note-link:hover { color: var(--el-color-primary); text-decoration: underline; }
.ma-row.is-current { background: var(--cm-brand-soft); }
.ma-meta code { font-size: 12px; }
.note-body { margin-top: 12px; font-size: 14px; line-height: 24px; }
.note-hint { margin: 0 12px 0 8px; color: var(--el-text-color-secondary); font-size: 12px; }
</style>
