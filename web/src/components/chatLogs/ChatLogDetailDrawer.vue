<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import MarkdownContent from '@/components/MarkdownContent.vue'
import { ElMessage } from 'element-plus'
import { reactive } from 'vue'
import { useI18n } from 'vue-i18n'
import { chatLogs } from '@/api/admin'
import type { ChatLogOut } from '@/api/types'
import { fileLine, latencyOf, statusType, tokensOf, userOf } from '@/utils/chatLogs'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const detail = reactive<{ visible: boolean; loading: boolean; data: ChatLogOut | null }>({
  visible: false,
  loading: false,
  data: null,
})

/** 打开详情抽屉并拉全文字段（列表项里只有预览）。 */
async function open(id: number): Promise<void> {
  detail.visible = true
  detail.loading = true
  detail.data = null
  try {
    detail.data = await chatLogs.get(id)
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    detail.loading = false
  }
}

defineExpose({ open })
</script>

<template>
  <el-drawer
    v-model="detail.visible"
    :title="t('chatLogs.detail')"
    size="60%"
  >
    <div
      v-loading="detail.loading"
      data-test="drawer"
    >
      <template v-if="detail.data">
        <el-descriptions
          :column="2"
          border
        >
          <el-descriptions-item :label="t('chatLogs.time')">
            {{ formatDateTime(detail.data.request_at) }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('chatLogs.bot')">
            {{ detail.data.bot_name || '—' }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('chatLogs.user')">
            {{ userOf(detail.data) }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('chatLogs.status')">
            <el-tag
              :type="statusType(detail.data.status)"
              disable-transitions
            >
              {{ t(`chatLogs.statuses.${detail.data.status}`) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item :label="t('chatLogs.latency')">
            {{ latencyOf(detail.data.latency_ms) }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('chatLogs.tokens')">
            {{ tokensOf(detail.data) }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('chatLogs.session')">
            {{ detail.data.relay_session_id ?? '—' }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('chatLogs.tools')">
            <span v-if="!detail.data.tools_used.length">—</span>
            <el-tag
              v-for="tool in detail.data.tools_used"
              :key="tool"
              class="chip"
              type="info"
              disable-transitions
            >
              {{ tool }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item
            v-if="detail.data.file_info"
            :label="t('chatLogs.file')"
          >
            {{ fileLine(detail.data.file_info) }}
          </el-descriptions-item>
        </el-descriptions>

        <h4>{{ t('chatLogs.message') }}</h4>
        <MarkdownContent :content="detail.data.message_content ?? detail.data.message_preview ?? '—'" />
        <template v-if="detail.data.quoted_content">
          <h4>{{ t('chatLogs.quoted') }}</h4>
          <MarkdownContent :content="detail.data.quoted_content" />
        </template>
        <h4>{{ t('chatLogs.response') }}</h4>
        <MarkdownContent :content="detail.data.response_content ?? detail.data.response_preview ?? '—'" />
        <template v-if="detail.data.error_message || detail.data.error_code">
          <h4>{{ t('chatLogs.error') }}</h4>
          <pre class="body error">{{ detail.data.error_code }} {{ detail.data.error_message }}</pre>
        </template>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.chip { margin-right: 6px; }
.body {
  margin: 0 0 12px;
  padding: 8px 12px;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}
.body.error { color: var(--el-color-danger); }
</style>
