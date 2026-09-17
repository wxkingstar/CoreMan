<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { feishuApps, type FeishuAppOverview, type FeishuPersonalLevel } from '@/api/feishuApps'
import FeishuAppBotConfig from '@/components/feishuApp/FeishuAppBotConfig.vue'
import FeishuAppCommands from '@/components/feishuApp/FeishuAppCommands.vue'
import FeishuAppProfile from '@/components/feishuApp/FeishuAppProfile.vue'
import FeishuAppVisibility from '@/components/feishuApp/FeishuAppVisibility.vue'
import FeishuRegistrationDialog from '@/components/feishuApp/FeishuRegistrationDialog.vue'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const props = defineProps<{ botId: string }>()
const { t } = useI18n()
const LEVELS: FeishuPersonalLevel[] = ['messages_readonly', 'all_except_send', 'all']
/** 取应用访问凭证阶段的失败：凭证错、应用不存在或已停用，而不是缺权限。 */
const CREDENTIAL_ERRORS = new Set([10003, 10012, 10013, 10014, 10015, 99991543, 99991663, 99991665])

const overview = ref<FeishuAppOverview | null>(null)
const loading = ref(false)
const loadError = ref('')
const registrationVisible = ref(false)
const applying = ref(false)
const publishing = ref(false)
const publishForm = reactive({ version: '', changelog: '', remark: '' })

const app = computed(() => overview.value?.app ?? null)
const underReview = computed(() => !!app.value?.under_review)
const missingCount = computed(() => (overview.value?.missing_scopes.tenant.length ?? 0) + (overview.value?.missing_scopes.user.length ?? 0))

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    overview.value = await feishuApps.overview(props.botId)
    publishForm.version = overview.value.next_version
    publishForm.remark ||= t('feishuApp.publishRemarkDefault')
  } catch (e) {
    loadError.value = errorMessage(e)
  } finally {
    loading.value = false
  }
}

function versionTime(value: string | null): string {
  return value && /^\d+$/.test(value) ? formatDateTime(new Date(Number(value) * 1000).toISOString()) : '—'
}

async function applyScopes(): Promise<void> {
  applying.value = true
  try {
    await feishuApps.applyScopes(props.botId)
    ElMessage.success(t('feishuApp.applied'))
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    applying.value = false
  }
}

async function publish(): Promise<void> {
  if (!publishForm.version || !publishForm.changelog.trim() || !publishForm.remark.trim()) {
    ElMessage.error(t('feishuApp.publishRequired'))
    return
  }
  publishing.value = true
  try {
    const result = await feishuApps.publish(props.botId, { ...publishForm })
    ElMessage.success(t('feishuApp.published', { version: result.version }))
    publishForm.changelog = ''
    await load()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    publishing.value = false
  }
}

function onUpdated(): void {
  registrationVisible.value = false
  ElMessage.success(t('feishuApp.updated'))
  void load()
}

onMounted(load)
</script>

<template>
  <div
    v-loading="loading"
    class="feishu-app"
    data-test="feishu-app-panel"
  >
    <el-alert
      v-if="loadError"
      type="error"
      :closable="false"
      :title="loadError"
    />
    <template v-if="overview">
      <section class="cm-panel">
        <div class="app-header">
          <img
            v-if="app?.avatar_url"
            :src="app.avatar_url"
            class="avatar"
            alt=""
          >
          <div class="app-title">
            <h3>{{ app?.name || t('feishuApp.bot') }}</h3>
            <div class="muted">
              <code>{{ overview.app_id }}</code>
              <span v-if="overview.origin.one_click"> · {{ t('feishuApp.originOneClick', { name: overview.origin.created_by_name, time: formatDateTime(overview.origin.created_at) }) }}</span>
              <span v-else> · {{ t('feishuApp.originManual') }}</span>
            </div>
            <div class="tags">
              <el-tag
                v-if="overview.versions.online"
                type="success"
              >
                {{ t('feishuApp.onlineVersion', { version: overview.versions.online.version }) }}
              </el-tag>
              <el-tag
                v-else
                type="info"
              >
                {{ t('feishuApp.notPublished') }}
              </el-tag>
              <el-tag
                v-if="underReview"
                type="warning"
              >
                {{ t('feishuApp.underReview') }}
              </el-tag>
            </div>
          </div>
          <div class="actions">
            <el-button @click="load">
              {{ t('feishuApp.refresh') }}
            </el-button>
            <el-button
              data-test="feishu-app-update"
              @click="registrationVisible = true"
            >
              {{ t('feishuApp.scanUpdate') }}
            </el-button>
            <el-button
              tag="a"
              :href="overview.console_url"
              target="_blank"
              rel="noopener noreferrer"
            >
              {{ t('feishuApp.openConsole') }}
            </el-button>
          </div>
        </div>
        <el-alert
          v-if="overview.errors.app"
          type="error"
          :closable="false"
          data-test="app-error"
          :title="CREDENTIAL_ERRORS.has(overview.errors.app) ? t('feishuApp.appCredentialError', { code: overview.errors.app }) : t('feishuApp.appReadError', { code: overview.errors.app })"
        />
        <el-alert
          v-if="underReview"
          type="warning"
          :closable="false"
          :title="t('feishuApp.underReviewHint')"
        />
        <p class="cm-page-intro">
          {{ t('feishuApp.publishHint') }}
        </p>
      </section>

      <!-- 读不到应用信息时，下面的权限判断和修改都没有依据，只保留上方的原因与补齐入口。 -->
      <template v-if="app">
        <section
          class="cm-panel"
          data-test="feishu-app-permissions"
        >
          <h3>{{ t('feishuApp.permissions') }}</h3>
          <p class="cm-page-intro">
            {{ t('feishuApp.permissionsHint') }}
          </p>
          <div class="levels">
            <span class="label">{{ t('feishuApp.personalLevels') }}</span>
            <el-tag
              v-for="level in LEVELS"
              :key="level"
              :type="overview.personal_levels[level] ? 'success' : 'info'"
              :data-test="`level-${level}`"
            >
              {{ overview.personal_levels[level] ? '✓' : '✗' }} {{ t(`myFeishu.levels.${level}`) }}
            </el-tag>
            <span class="muted">{{ t('feishuApp.personalConnections', { count: overview.personal_connections }) }}</span>
          </div>
          <div
            v-if="missingCount"
            class="scope-block"
            data-test="missing-scopes"
          >
            <el-alert
              type="warning"
              :closable="false"
              :title="t('feishuApp.missingScopes', { count: missingCount })"
            >
              <div class="scope-list">
                {{ [...overview.missing_scopes.tenant, ...overview.missing_scopes.user].join('、') }}
              </div>
            </el-alert>
            <el-button
              type="primary"
              @click="registrationVisible = true"
            >
              {{ t('feishuApp.scanUpdate') }}
            </el-button>
          </div>
          <div
            v-if="overview.pending_grants.length"
            class="scope-block"
            data-test="pending-grants"
          >
            <el-alert
              type="info"
              :closable="false"
              :title="t('feishuApp.pendingGrants', { count: overview.pending_grants.length })"
            >
              <div class="scope-list">
                {{ overview.pending_grants.join('、') }}
              </div>
            </el-alert>
            <el-button
              :loading="applying"
              data-test="apply-scopes"
              @click="applyScopes"
            >
              {{ t('feishuApp.applyScopes') }}
            </el-button>
          </div>
          <el-collapse>
            <el-collapse-item :title="t('feishuApp.allScopes', { count: overview.scopes.length })">
              <el-table
                :data="overview.scopes"
                size="small"
              >
                <el-table-column
                  prop="scope"
                  :label="t('feishuApp.scope')"
                  min-width="240"
                />
                <el-table-column
                  :label="t('feishuApp.tokenType')"
                  width="140"
                >
                  <template #default="{ row }">
                    {{ row.token_types.map((type: string) => t(`feishuApp.tokenTypes.${type}`)).join(' / ') }}
                  </template>
                </el-table-column>
                <el-table-column
                  :label="t('feishuApp.grantStatus')"
                  width="120"
                >
                  <template #default="{ row }">
                    {{ row.granted === null ? '—' : row.granted ? t('feishuApp.granted') : t('feishuApp.notGranted') }}
                  </template>
                </el-table-column>
              </el-table>
            </el-collapse-item>
          </el-collapse>
        </section>

        <FeishuAppProfile
          :bot-id="botId"
          :overview="overview"
          :disabled="underReview"
          @changed="load"
        />
        <FeishuAppBotConfig
          :bot-id="botId"
          :overview="overview"
          :disabled="underReview"
          @changed="load"
        />
        <FeishuAppVisibility
          :bot-id="botId"
          :overview="overview"
          :disabled="underReview"
          @changed="load"
        />
        <FeishuAppCommands
          :bot-id="botId"
          :overview="overview"
          @changed="load"
        />

        <section
          class="cm-panel"
          data-test="feishu-app-publish"
        >
          <h3>{{ t('feishuApp.publish') }}</h3>
          <el-descriptions
            :column="1"
            border
          >
            <el-descriptions-item :label="t('feishuApp.onlineVersionLabel')">
              <template v-if="overview.versions.online">
                {{ overview.versions.online.version }} · {{ versionTime(overview.versions.online.publish_time) }}
              </template>
              <span v-else>—</span>
            </el-descriptions-item>
            <el-descriptions-item
              v-if="overview.versions.under_review"
              :label="t('feishuApp.reviewVersionLabel')"
            >
              {{ overview.versions.under_review.version }} · {{ t(`feishuApp.versionStatus.${overview.versions.under_review.status ?? 0}`) }}
            </el-descriptions-item>
          </el-descriptions>
          <el-form
            label-width="120px"
            class="publish-form"
            @submit.prevent
          >
            <el-form-item :label="t('feishuApp.version')">
              <el-input
                v-model="publishForm.version"
                style="width: 160px"
                data-test="publish-version"
              />
            </el-form-item>
            <el-form-item :label="t('feishuApp.changelog')">
              <el-input
                v-model="publishForm.changelog"
                type="textarea"
                :rows="2"
                maxlength="500"
                data-test="publish-changelog"
              />
            </el-form-item>
            <el-form-item :label="t('feishuApp.remark')">
              <el-input
                v-model="publishForm.remark"
                type="textarea"
                :rows="2"
                maxlength="500"
              />
            </el-form-item>
            <el-form-item>
              <el-button
                type="primary"
                :loading="publishing"
                :disabled="underReview"
                data-test="publish-submit"
                @click="publish"
              >
                {{ t('feishuApp.submitPublish') }}
              </el-button>
            </el-form-item>
          </el-form>
        </section>
      </template>
    </template>
    <FeishuRegistrationDialog
      v-model:visible="registrationVisible"
      purpose="update"
      :bot-id="botId"
      @succeeded="onUpdated"
    />
  </div>
</template>

<style scoped>
.app-header { display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; margin-bottom: 12px; }
.avatar { width: 56px; height: 56px; border-radius: 12px; object-fit: cover; }
.app-title { flex: 1; min-width: 200px; }
.app-title h3 { margin: 0 0 4px; }
.tags { display: flex; gap: 6px; margin-top: 6px; flex-wrap: wrap; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
.muted { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
.levels { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 8px 0 12px; }
.label { font-weight: 600; }
.scope-block { display: flex; gap: 12px; align-items: flex-start; margin-bottom: 12px; }
.scope-block .el-alert { flex: 1; }
.scope-list { word-break: break-all; }
.publish-form { margin-top: 16px; max-width: 640px; }
.feishu-app .el-alert { margin-bottom: 12px; }
</style>
