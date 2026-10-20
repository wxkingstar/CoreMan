<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { workspace, type WorkspaceInfo, type WorkspaceEntry, type WorkspaceFile, type GitStatus, type GitDiff } from '@/api/workspace'
import { ApiError } from '@/api/client'
import { errorMessage } from '@/utils/errors'
const props = defineProps<{ botId: string; visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [boolean] }>()
const { t } = useI18n()
const info = ref<WorkspaceInfo>(); const entries = ref<WorkspaceEntry[]>([]); const folder = ref(''); const path = ref(''); const file = ref<WorkspaceFile>(); const draft = ref(''); const original = ref(''); const busy = ref(false); const error = ref(''); const conflict = ref(false); const truncated = ref(false)
const tab = ref('files'); const git = ref<GitStatus>(); const diff = ref<GitDiff>(); const imageUrl = ref('')
const dirty = computed(() => draft.value !== original.value)
let timer: ReturnType<typeof setInterval> | undefined
async function perform(action: () => Promise<void>) { busy.value = true; error.value = ''; try { await action() } catch (e) { error.value = errorMessage(e) } finally { busy.value = false } }
async function status() { info.value = await workspace.get(props.botId) }
async function load() { await perform(async () => { await status(); await browse('') }) }
async function discard() { if (!dirty.value) return true; try { await ElMessageBox.confirm(t('workspaceFiles.discard')); return true } catch { return false } }
async function close(done?: () => void) { if (busy.value || !await discard()) return; emit('update:visible', false); done?.() }
async function browse(p: string) { const result = await workspace.list(props.botId, p); entries.value = result.entries; truncated.value = !!result.truncated; folder.value = p }
async function navigate(p: string) { await perform(() => browse(p)) }
function revoke() { if (imageUrl.value) URL.revokeObjectURL(imageUrl.value); imageUrl.value = '' }
async function bytes(p: string, first?: WorkspaceFile) {
  const chunks: Uint8Array[] = []; let offset = 0; let part = first ?? await workspace.read(props.botId, p); const hash = part.hash
  while (true) {
    if (part.hash !== hash) throw new Error(t('workspaceFiles.conflict'))
    const chunk = Uint8Array.from(atob(part.data), c => c.charCodeAt(0)); chunks.push(chunk); offset += chunk.length
    if (part.eof || offset >= part.size) break
    if (!chunk.length) throw new Error('Incomplete file read')
    part = await workspace.read(props.botId, p, offset)
  }
  return new Blob(chunks as BlobPart[])
}
async function openFile(p: string) {
  if (!await discard()) return
  await perform(async () => {
    const result = await workspace.read(props.botId, p)
    const content = result.editable ? (result.eof && result.content !== null ? result.content : await (await bytes(p, result)).text()) : ''
    revoke(); path.value = p; file.value = result; draft.value = content; original.value = content; conflict.value = false
    if (/\.(png|jpe?g|gif|webp)$/i.test(p) && result.size <= 10 * 1024 * 1024) {
      const type = /\.png$/i.test(p) ? 'image/png' : /\.gif$/i.test(p) ? 'image/gif' : /\.webp$/i.test(p) ? 'image/webp' : 'image/jpeg'
      imageUrl.value = URL.createObjectURL(new Blob([await bytes(p, result)], { type }))
    }
  })
}
async function save() {
  if (!file.value?.editable || !dirty.value) return
  await perform(async () => { try { const result = await workspace.write(props.botId, { path: path.value, content: draft.value, expected_hash: file.value!.hash }); file.value!.hash = result.hash; original.value = draft.value; conflict.value = false } catch (e) { if (e instanceof ApiError && e.status === 409) conflict.value = true; throw e } })
}
function downloadBlob(blob: Blob, name: string) { const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000) }
async function download(isDraft = false) { await perform(async () => downloadBlob(isDraft ? new Blob([draft.value], { type: 'text/plain;charset=utf-8' }) : await bytes(path.value), path.value.split('/').pop() || 'file')) }
async function gitStatus() { await perform(async () => { git.value = await workspace.gitStatus(props.botId); if (diff.value && !git.value.files?.some(f => f.path === diff.value!.path)) diff.value = undefined }) }
async function openDiff(p: string) { await perform(async () => { diff.value = await workspace.gitDiff(props.botId, p) }) }
// 状态码取 git status 的 XY 两位：左边是暂存区，右边是工作区。
function statusKind(code: string) { const c = code.trim(); return c === '??' ? 'untracked' : c.includes('D') ? 'deleted' : c.includes('R') ? 'renamed' : c.includes('A') ? 'added' : 'modified' }
const statusText: Record<string, string> = { untracked: 'workspaceFiles.gitUntracked', deleted: 'workspaceFiles.gitDeleted', renamed: 'workspaceFiles.gitRenamed', added: 'workspaceFiles.gitAdded', modified: 'workspaceFiles.gitModified' }
function lineClass(line: string) { return line.startsWith('+++') || line.startsWith('---') ? 'diff-meta' : line.startsWith('+') ? 'diff-add' : line.startsWith('-') ? 'diff-del' : line.startsWith('@@') ? 'diff-hunk' : '' }
async function initialize() { await perform(async () => { await workspace.initialize(props.botId); await status(); await browse('') }) }
watch(tab, value => { if (value === 'git') void gitStatus() })
watch(() => props.visible, value => { clearInterval(timer); if (value) { void load(); timer = setInterval(() => { void status().catch(() => {}) }, 3000) } }, { immediate: true })
onBeforeUnmount(() => { clearInterval(timer); revoke() })
defineExpose({ openFile, draft, save, openDiff })
</script>
<template>
  <el-drawer
    :model-value="visible"
    :title="t('workspaceFiles.title')"
    size="min(100vw, max(980px, 85vw))"
    class="workspace-drawer"
    :before-close="close"
    :close-on-click-modal="false"
  >
    <div class="workspace-content">
      <div class="workspace-status">
        <code>{{ info?.directory }}</code><el-tag v-if="info">
          {{ t('workspaceFiles.states.' + info.state, info.state) }}
        </el-tag><span>{{ info?.phase }}</span>
      </div>
      <!-- 就绪还带着错误＝上次迁移失败、原绑定照常工作，用警告色，不当成故障。 -->
      <el-alert
        v-if="error || info?.error"
        :title="error || info?.error || ''"
        :type="!error && info?.state === 'ready' ? 'warning' : 'error'"
        :closable="false"
      />
      <el-button
        v-if="info && ['pending', 'failed'].includes(info.state)"
        :loading="busy"
        @click="initialize"
      >
        {{ t('workspaceFiles.retry') }}
      </el-button>
      <el-tabs
        v-model="tab"
        class="workspace-tabs"
      >
        <el-tab-pane
          :label="t('workspaceFiles.files')"
          name="files"
          class="workspace-files-pane"
        >
          <div class="file-toolbar">
            <el-button
              :disabled="busy || !folder"
              @click="navigate(folder.split('/').slice(0, -1).join('/'))"
            >
              {{ t('workspaceFiles.parent') }}
            </el-button><code>/{{ folder }}</code><el-button
              :disabled="busy"
              @click="navigate(folder)"
            >
              {{ t('workspaceFiles.refresh') }}
            </el-button>
          </div>
          <el-alert
            v-if="truncated"
            :title="t('workspaceFiles.truncated')"
            type="warning"
            :closable="false"
          />
          <div
            class="file-layout"
            :class="{ 'file-layout-empty': !entries.length }"
          >
            <div
              v-if="entries.length"
              class="file-list"
            >
              <el-button
                v-for="entry in entries"
                :key="entry.path"
                text
                :disabled="busy"
                @click="entry.type === 'directory' ? navigate(entry.path) : openFile(entry.path)"
              >
                {{ entry.type === 'directory' ? '▸ ' : '' }}{{ entry.name }}
              </el-button>
            </div>
            <div
              class="file-editor"
              :class="{ 'file-editor-empty': !file }"
            >
              <template v-if="file">
                <strong>{{ path }}</strong><el-alert
                  v-if="conflict"
                  :title="t('workspaceFiles.conflict')"
                  type="warning"
                  :closable="false"
                /><el-input
                  v-if="file.editable"
                  v-model="draft"
                  data-test="workspace-editor"
                  type="textarea"
                  :rows="18"
                  :disabled="busy"
                  :aria-label="path"
                /><img
                  v-else-if="imageUrl"
                  :src="imageUrl"
                  :alt="path"
                ><p v-else>
                  {{ t('workspaceFiles.readOnly') }} ({{ file.size }} bytes)
                </p>
              </template>
              <el-empty
                v-else
                :description="t('workspaceFiles.select')"
              />
            </div>
          </div>
        </el-tab-pane>
        <el-tab-pane
          :label="t('workspaceFiles.git')"
          name="git"
        >
          <div class="file-toolbar">
            <el-button
              :disabled="busy"
              @click="gitStatus"
            >
              {{ t('workspaceFiles.refresh') }}
            </el-button>
          </div>
          <el-empty
            v-if="git && !git.initialized"
            :description="t('workspaceFiles.gitNotRepo')"
          />
          <template v-else-if="git">
            <dl class="git-summary">
              <dt>{{ t('workspaceFiles.gitRemote') }}</dt>
              <dd><code>{{ git.remote || t('workspaceFiles.gitNoRemote') }}</code></dd>
              <dt>{{ t('workspaceFiles.gitBranch') }}</dt>
              <dd>
                <code>{{ git.branch || '—' }}</code>
                <span v-if="git.upstream"> → <code>{{ git.upstream }}</code></span>
                <el-tag
                  v-if="git.ahead"
                  size="small"
                  type="warning"
                >
                  {{ t('workspaceFiles.gitAhead', { n: git.ahead }) }}
                </el-tag>
              </dd>
              <dt>{{ t('workspaceFiles.gitHead') }}</dt>
              <dd v-if="git.head">
                <code>{{ git.head.hash }}</code> {{ git.head.subject }} <span class="git-date">{{ git.head.date }}</span>
              </dd>
              <dd v-else>
                {{ t('workspaceFiles.gitNoCommit') }}
              </dd>
            </dl>
            <div
              class="file-layout git-layout"
              :class="{ 'file-layout-empty': !git.files?.length }"
            >
              <div
                v-if="git.files?.length"
                class="git-file-list"
              >
                <button
                  v-for="entry in git.files"
                  :key="entry.path"
                  type="button"
                  class="git-file"
                  :class="[`git-${statusKind(entry.status)}`, { active: diff?.path === entry.path }]"
                  :disabled="busy || entry.path.endsWith('/')"
                  :title="entry.path"
                  @click="openDiff(entry.path)"
                >
                  <code class="git-code">{{ entry.status }}</code>
                  <span class="git-path">{{ entry.path }}</span>
                  <span class="git-kind">{{ t(statusText[statusKind(entry.status)]) }}</span>
                </button>
              </div>
              <div class="file-editor">
                <template v-if="diff">
                  <strong>{{ diff.path }}</strong>
                  <pre class="git-diff"><span
                    v-for="(line, index) in diff.diff.split('\n')"
                    :key="index"
                    :class="lineClass(line)"
                  >{{ line }}</span></pre>
                  <p v-if="diff.truncated">
                    {{ t('workspaceFiles.gitDiffTruncated') }}
                  </p>
                </template>
                <el-empty
                  v-else
                  :description="t(git.files?.length ? 'workspaceFiles.gitSelect' : 'workspaceFiles.gitClean')"
                />
              </div>
            </div>
            <p v-if="git.truncated">
              {{ t('workspaceFiles.truncated') }}
            </p>
          </template>
        </el-tab-pane>
      </el-tabs>
    </div>
    <template #footer>
      <div class="workspace-actions">
        <el-button
          :disabled="busy"
          @click="close()"
        >
          {{ t('common.close') }}
        </el-button><template v-if="tab === 'files' && file">
          <el-button
            :disabled="busy"
            @click="download()"
          >
            {{ t('workspaceFiles.download') }}
          </el-button><el-button
            v-if="dirty"
            :disabled="busy"
            @click="download(true)"
          >
            {{ t('workspaceFiles.downloadDraft') }}
          </el-button><el-button
            type="primary"
            :loading="busy"
            :disabled="!file.editable || !dirty || conflict"
            @click="save"
          >
            {{ t('workspaceFiles.save') }}
          </el-button>
        </template>
      </div>
    </template>
  </el-drawer>
</template>
<style scoped>
.workspace-content { height: 100%; display: flex; flex-direction: column; }
.workspace-content > :not(.workspace-tabs) { flex-shrink: 0; }
.workspace-tabs { flex: 1; min-height: 280px; display: flex; flex-direction: column; }
.workspace-tabs :deep(.el-tabs__content) { flex: 1; overflow: auto; }
.workspace-files-pane { min-height: 100%; display: flex; flex-direction: column; }
.workspace-files-pane > .file-toolbar { flex-shrink: 0; }
.file-layout { flex: 1; min-height: 260px; }
.file-layout.file-layout-empty { grid-template-columns: minmax(0, 1fr); }
.file-editor-empty { display: flex; align-items: center; justify-content: center; }

.git-summary{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:6px 14px;margin:0 0 16px}.git-summary dt{color:var(--el-text-color-secondary)}.git-summary dd{margin:0;overflow-wrap:anywhere}.file-layout.git-layout{grid-template-columns:minmax(320px,2fr) minmax(0,3fr)}.git-file-list{max-height:60vh;overflow:auto;display:flex;flex-direction:column;border:1px solid var(--el-border-color-lighter);border-radius:6px}.git-file{display:flex;align-items:baseline;gap:10px;width:100%;padding:6px 10px;border:0;border-bottom:1px solid var(--el-border-color-extra-light);background:none;color:inherit;font:inherit;text-align:left;cursor:pointer}.git-file:last-child{border-bottom:0}.git-file:hover:not(:disabled){background:var(--el-fill-color-light)}.git-file.active{background:var(--el-color-primary-light-9)}.git-file:disabled{cursor:default}.git-code{flex:none;width:2ch;font-family:monospace;font-weight:600;white-space:pre}.git-path{flex:1;min-width:0;font-family:monospace;font-size:13px;overflow-wrap:anywhere}.git-kind{flex:none;font-size:12px}.git-untracked,.git-added{color:var(--el-color-success)}.git-modified{color:var(--el-color-warning)}.git-deleted{color:var(--el-color-danger)}.git-deleted .git-path{text-decoration:line-through}.git-renamed{color:var(--el-color-primary)}.git-date{margin-left:8px;color:var(--el-text-color-secondary);font-size:12px}.git-diff{margin:0;max-height:55vh;overflow:auto;padding:10px;border:1px solid var(--el-border-color-lighter);border-radius:6px;font-size:12px;line-height:1.5;white-space:pre}.git-diff span{display:block;min-height:1.5em}.git-diff .diff-add{color:var(--el-color-success);background:var(--el-color-success-light-9)}.git-diff .diff-del{color:var(--el-color-danger);background:var(--el-color-danger-light-9)}.git-diff .diff-hunk{color:var(--el-color-primary)}.git-diff .diff-meta{color:var(--el-text-color-secondary)}.workspace-content{min-width:0}.workspace-status,.file-toolbar,.workspace-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}.workspace-status code{overflow-wrap:anywhere}.file-layout{display:grid;grid-template-columns:210px minmax(0,1fr);gap:20px}.file-list{max-height:55vh;overflow:auto;display:flex;flex-direction:column;align-items:stretch}.file-list .el-button{margin:0;justify-content:flex-start;white-space:normal;height:auto;min-height:34px;text-align:left}.file-editor{min-width:0}.file-editor strong{display:block;overflow-wrap:anywhere;margin-bottom:12px}.file-editor img{max-width:100%;max-height:55vh}.workspace-actions{justify-content:flex-end;margin:0}@media(max-width:600px){.file-layout,.file-layout.git-layout{grid-template-columns:1fr}.file-list{max-height:180px}.workspace-actions .el-button{margin:0}}
</style>
