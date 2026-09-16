<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { workspace, type WorkspaceInfo, type WorkspaceEntry, type WorkspaceFile, type GitStatus } from '@/api/workspace'
import { ApiError } from '@/api/client'
import { errorMessage } from '@/utils/errors'
const props = defineProps<{ botId: string; visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [boolean] }>()
const { t } = useI18n()
const info = ref<WorkspaceInfo>(); const entries = ref<WorkspaceEntry[]>([]); const folder = ref(''); const path = ref(''); const file = ref<WorkspaceFile>(); const draft = ref(''); const original = ref(''); const busy = ref(false); const error = ref(''); const conflict = ref(false); const truncated = ref(false)
const tab = ref('files'); const git = ref<GitStatus>(); const gitUrl = ref(''); const branch = ref('main'); const token = ref(''); const selected = ref<string[]>([]); const message = ref(''); const imageUrl = ref('')
const dirty = computed(() => draft.value !== original.value)
let timer: ReturnType<typeof setInterval> | undefined
async function perform(action: () => Promise<void>) { busy.value = true; error.value = ''; try { await action() } catch (e) { error.value = errorMessage(e) } finally { busy.value = false } }
async function status() { info.value = await workspace.get(props.botId) }
async function load() { await perform(async () => { await status(); gitUrl.value = info.value?.git_url ?? ''; branch.value = info.value?.branch || 'main'; await browse('') }) }
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
async function gitStatus() { await perform(async () => { git.value = await workspace.gitStatus(props.botId); selected.value = [] }) }
async function config() { await perform(async () => { await workspace.gitConfig(props.botId, { git_url: gitUrl.value, branch: branch.value, ...(token.value ? { access_token: token.value } : {}) }); token.value = ''; await status() }) }
async function test() { await perform(async () => { await workspace.gitTest(props.botId); ElMessage.success(t('workspaceFiles.connected')) }) }
async function backup() { await perform(async () => { const result = await workspace.backup(props.botId, selected.value, message.value); if (result.pushed) ElMessage.success(t('workspaceFiles.backedUp')); await status(); git.value = await workspace.gitStatus(props.botId); selected.value = [] }) }
async function initialize() { await perform(async () => { await workspace.initialize(props.botId); await status(); await browse('') }) }
watch(tab, value => { if (value === 'git') void gitStatus() })
watch(() => props.visible, value => { clearInterval(timer); if (value) { void load(); timer = setInterval(() => { void status().catch(() => {}) }, 3000) } }, { immediate: true })
onBeforeUnmount(() => { clearInterval(timer); revoke() })
defineExpose({ openFile, draft, save })
</script>
<template>
  <el-drawer
    :model-value="visible"
    :title="t('workspaceFiles.title')"
    size="min(100vw, 980px)"
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
      <el-alert
        v-if="error || info?.error"
        :title="error || info?.error || ''"
        type="error"
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
          <el-form label-position="top">
            <el-form-item :label="t('workspaceFiles.url')">
              <el-input v-model="gitUrl" />
            </el-form-item><el-form-item :label="t('workspaceFiles.branch')">
              <el-input v-model="branch" />
            </el-form-item><el-form-item :label="t('workspaceFiles.token')">
              <el-input
                v-model="token"
                type="password"
                show-password
                autocomplete="new-password"
              />
            </el-form-item>
          </el-form>
          <div class="file-toolbar">
            <el-button
              :disabled="busy || !gitUrl || !branch"
              @click="config"
            >
              {{ t('workspaceFiles.config') }}
            </el-button><el-button
              :disabled="busy || !info?.git_url"
              @click="test"
            >
              {{ t('workspaceFiles.test') }}
            </el-button><el-button
              :disabled="busy"
              @click="gitStatus"
            >
              {{ t('workspaceFiles.refresh') }}
            </el-button>
          </div>
          <p>{{ t('workspaceFiles.lastBackup') }}: {{ info?.last_backup_at || '—' }}</p>
          <details
            v-if="git?.excluded?.length"
            class="git-exclusions"
          >
            <summary>{{ t('workspaceFiles.excluded') }} ({{ git.excluded.length }})</summary>
            <ul>
              <li
                v-for="item in git.excluded"
                :key="item"
              >
                <code>{{ item }}</code>
              </li>
            </ul>
          </details>
          <el-checkbox-group
            v-model="selected"
            class="git-files"
          >
            <el-checkbox
              v-for="entry in git?.files"
              :key="entry.path"
              :value="entry.path"
            >
              {{ entry.status }} {{ entry.path }}
            </el-checkbox>
          </el-checkbox-group>
          <el-input
            v-model="message"
            :placeholder="t('workspaceFiles.message')"
            :aria-label="t('workspaceFiles.message')"
          />
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
        </template><el-button
          v-if="tab === 'git'"
          type="primary"
          :loading="busy"
          :disabled="(!selected.length && !git?.head) || !info?.git_url"
          @click="backup"
        >
          {{ t(selected.length ? 'workspaceFiles.backup' : 'workspaceFiles.retryPush') }}
        </el-button>
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

.git-exclusions{margin:12px 0;color:var(--el-text-color-secondary)}.git-exclusions summary{cursor:pointer}.git-exclusions ul{max-height:160px;overflow:auto;padding-left:20px}.git-exclusions code{overflow-wrap:anywhere}.workspace-content{min-width:0}.workspace-status,.file-toolbar,.workspace-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}.workspace-status code{overflow-wrap:anywhere}.file-layout{display:grid;grid-template-columns:210px minmax(0,1fr);gap:20px}.file-list{max-height:55vh;overflow:auto;display:flex;flex-direction:column;align-items:stretch}.file-list .el-button{margin:0;justify-content:flex-start;white-space:normal;height:auto;min-height:34px;text-align:left}.file-editor{min-width:0}.file-editor strong{display:block;overflow-wrap:anywhere;margin-bottom:12px}.file-editor img{max-width:100%;max-height:55vh}.git-files{display:flex;flex-direction:column;margin:16px 0;max-height:35vh;overflow:auto}.workspace-actions{justify-content:flex-end;margin:0}@media(max-width:600px){.file-layout{grid-template-columns:1fr}.file-list{max-height:180px}.workspace-actions .el-button{margin:0}}
</style>
