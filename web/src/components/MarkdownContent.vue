<script setup lang="ts">
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'

const props = defineProps<{ content: string }>()
const markdown = new MarkdownIt({ html: false, linkify: true, breaks: true })
const rendered = computed(() => DOMPurify.sanitize(markdown.render(props.content), {
  USE_PROFILES: { html: true },
}))
</script>

<template>
  <!-- Only sanitized parser output is inserted as HTML. -->
  <!-- eslint-disable-next-line vue/no-v-html -->
  <div class="markdown-content" v-html="rendered" />
</template>

<style scoped>
.markdown-content { margin: 0 0 16px; padding: 12px 16px; background: var(--el-fill-color-light); border-radius: 6px; line-height: 1.7; overflow-wrap: anywhere; overflow-x: auto; }
.markdown-content :deep(> :first-child) { margin-top: 0; }
.markdown-content :deep(> :last-child) { margin-bottom: 0; }
.markdown-content :deep(pre) { padding: 12px; overflow-x: auto; background: var(--el-fill-color); border-radius: 4px; white-space: pre; }
.markdown-content :deep(code) { font-family: ui-monospace, monospace; font-size: .9em; background: var(--el-fill-color); border-radius: 3px; padding: 2px 4px; }
.markdown-content :deep(pre code) { padding: 0; }
.markdown-content :deep(blockquote) { margin-left: 0; padding-left: 14px; border-left: 3px solid var(--el-border-color); color: var(--el-text-color-secondary); }
.markdown-content :deep(table) { border-collapse: collapse; max-width: 100%; }
.markdown-content :deep(th), .markdown-content :deep(td) { padding: 6px 12px; border: 1px solid var(--el-border-color); }
.markdown-content :deep(a) { color: var(--el-color-primary); }
.markdown-content :deep(img) { max-width: 100%; height: auto; }
</style>
