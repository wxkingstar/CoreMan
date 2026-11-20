<script setup lang="ts">
import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'
import { computed } from 'vue'

/**
 * 知识页正文按 Obsidian 的写法渲染：Markdown、页首 front matter、[[双链]]、> [!NOTE] 提示块。
 *
 * 正文可能来自别人发来的消息：不认 HTML（`html: false`），输出再过一遍 DOMPurify；图片不加载，
 * 只显示成链接（远程图片会把查看的人暴露给图片所在的服务器）；外链在新窗口打开、不带来源。
 */
const props = defineProps<{ content: string; resolve: (target: string) => string[] }>()
const emit = defineEmits<{ (event: 'open-link', target: string): void }>()

const FRONT_MATTER = /^---\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/
const LINK = /^(!?)\[\[([^[\]\n|#]*)(#[^[\]\n|]*)?(\|[^[\]\n]*)?\]\]/
const CALLOUT = /^\[!([A-Za-z-]+)\][+-]?[ \t]*([^\n]*)/

const markdown = new MarkdownIt({ html: false, linkify: true, breaks: true })
type InlineRule = Parameters<typeof markdown.inline.ruler.before>[2]
type CoreRule = Parameters<typeof markdown.core.ruler.before>[2]

const wikilink: InlineRule = (state, silent) => {
  const src = state.src.slice(state.pos)
  if (!src.startsWith('[[') && !src.startsWith('![[')) return false
  const found = LINK.exec(src)
  if (!found || !found[2].trim()) return false
  if (!silent) {
    const target = found[2].trim()
    const label = found[4] ? found[4].slice(1) : target + (found[3] ?? '')
    const token = state.push('wikilink', '', 0)
    token.meta = { target, label, embed: found[1] === '!' }
  }
  state.pos += found[0].length
  return true
}
markdown.inline.ruler.before('link', 'wikilink', wikilink)
markdown.renderer.rules.wikilink = (tokens, idx) => {
  const { target, label, embed } = tokens[idx].meta as { target: string; label: string; embed: boolean }
  const hits = props.resolve(target).length
  const classes = ['wikilink', embed ? 'is-embed' : '', hits === 1 ? '' : hits ? 'is-ambiguous' : 'is-missing']
  const escape = markdown.utils.escapeHtml
  return `<a href="#" class="${classes.filter(Boolean).join(' ')}" data-wikilink="${escape(target)}">${escape(label)}</a>`
}

/** `> [!WARNING] 待核实`：引用块带上提示块的样式，首行的标记换成加粗的标题。 */
const callout: CoreRule = (state) => {
  state.tokens.forEach((token, i) => {
    if (token.type !== 'blockquote_open') return
    const inline = state.tokens[i + 2]
    if (state.tokens[i + 1]?.type !== 'paragraph_open' || inline?.type !== 'inline') return
    const found = CALLOUT.exec(inline.content)
    if (!found) return
    token.attrJoin('class', `callout callout-${found[1].toLowerCase()}`)
    inline.content = `**${found[2] || found[1]}**${inline.content.slice(found[0].length)}`
  })
}
markdown.core.ruler.before('inline', 'callout', callout)

markdown.renderer.rules.image = (tokens, idx) => {
  const token = tokens[idx]
  const src = String(token.attrGet('src') ?? '')
  const alt = String(token.content || src)
  return `<a class="note-image" href="${markdown.utils.escapeHtml(src)}">🖼 ${markdown.utils.escapeHtml(alt)}</a>`
}
const defaultLinkOpen = markdown.renderer.rules.link_open
markdown.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noopener noreferrer nofollow')
  return defaultLinkOpen ? defaultLinkOpen(tokens, idx, options, env, self) : self.renderToken(tokens, idx, options)
}

const parts = computed(() => {
  const found = FRONT_MATTER.exec(props.content)
  return found ? { props: found[1], body: props.content.slice(found[0].length) } : { props: '', body: props.content }
})
const rendered = computed(() => DOMPurify.sanitize(markdown.render(parts.value.body), {
  USE_PROFILES: { html: true },
  ADD_ATTR: ['target'],
}))

function onClick(event: MouseEvent) {
  const link = (event.target as HTMLElement | null)?.closest<HTMLElement>('a.wikilink')
  if (!link) return
  event.preventDefault()
  emit('open-link', link.dataset.wikilink ?? '')
}
</script>

<template>
  <div class="note-markdown">
    <pre
      v-if="parts.props"
      class="note-props"
      data-test="note-props"
    >{{ parts.props }}</pre>
    <!-- Only sanitized parser output is inserted as HTML. -->
    <!-- eslint-disable vue/no-v-html -->
    <div
      class="note-rendered"
      data-test="note-rendered"
      @click="onClick"
      v-html="rendered"
    />
    <!-- eslint-enable vue/no-v-html -->
  </div>
</template>

<style scoped>
.note-markdown { line-height: 1.7; font-size: 14px; overflow-wrap: anywhere; }
.note-props { margin: 0 0 12px; padding: 8px 12px; background: var(--el-fill-color-light); border-radius: 6px; font-size: 12px; line-height: 20px; color: var(--el-text-color-secondary); white-space: pre-wrap; }
.note-rendered :deep(> :first-child) { margin-top: 0; }
.note-rendered :deep(> :last-child) { margin-bottom: 0; }
.note-rendered :deep(h1) { font-size: 20px; }
.note-rendered :deep(h2) { font-size: 17px; }
.note-rendered :deep(h3) { font-size: 15px; }
.note-rendered :deep(pre) { padding: 12px; overflow-x: auto; background: var(--el-fill-color); border-radius: 4px; white-space: pre; }
.note-rendered :deep(code) { font-family: ui-monospace, monospace; font-size: .9em; background: var(--el-fill-color); border-radius: 3px; padding: 2px 4px; }
.note-rendered :deep(pre code) { padding: 0; }
.note-rendered :deep(blockquote) { margin: 12px 0; padding: 4px 14px; border-left: 3px solid var(--el-border-color); color: var(--el-text-color-secondary); }
.note-rendered :deep(blockquote.callout) { color: var(--el-text-color-regular); background: var(--el-color-info-light-9); border-left-color: var(--el-color-info); border-radius: 4px; }
.note-rendered :deep(blockquote.callout-warning), .note-rendered :deep(blockquote.callout-caution), .note-rendered :deep(blockquote.callout-attention) { background: var(--el-color-warning-light-9); border-left-color: var(--el-color-warning); }
.note-rendered :deep(blockquote.callout-danger), .note-rendered :deep(blockquote.callout-error), .note-rendered :deep(blockquote.callout-bug) { background: var(--el-color-danger-light-9); border-left-color: var(--el-color-danger); }
.note-rendered :deep(blockquote.callout-tip), .note-rendered :deep(blockquote.callout-success), .note-rendered :deep(blockquote.callout-check), .note-rendered :deep(blockquote.callout-done) { background: var(--el-color-success-light-9); border-left-color: var(--el-color-success); }
.note-rendered :deep(table) { border-collapse: collapse; max-width: 100%; display: block; overflow-x: auto; }
.note-rendered :deep(th), .note-rendered :deep(td) { padding: 6px 12px; border: 1px solid var(--el-border-color); }
.note-rendered :deep(a) { color: var(--el-color-primary); }
.note-rendered :deep(a.wikilink) { text-decoration: none; border-bottom: 1px dashed currentColor; }
.note-rendered :deep(a.wikilink.is-missing) { color: var(--el-text-color-placeholder); }
.note-rendered :deep(a.wikilink.is-ambiguous) { color: var(--el-color-warning); }
.note-rendered :deep(ul), .note-rendered :deep(ol) { padding-left: 22px; }
</style>
