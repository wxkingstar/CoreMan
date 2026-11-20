/**
 * 知识库双链的解析，与后端 `coreman/core/assistants/notes.py` 的 `_resolve` 同一套规则：
 * 带不带 `.md` 是同一页、不分大小写；先找路径完全一样的页，没有再找路径以「/目标」结尾的页，
 * 恰好一页才算链上。只用来给页面上的链接标样式、点开时找页；改名、反链以后端为准。
 */
export function pageKey(path: string): string | null {
  const shown = path.normalize('NFKC').replace(/\\/g, '/')
    .split('/').map((part) => part.trim()).join('/')
    .replace(/\/+/g, '/').replace(/^\/+|\/+$/g, '')
  if (!shown) return null
  const key = shown.toLowerCase()
  return key.endsWith('.md') ? key.slice(0, -3) : key
}

/** 双链目标指向的页（路径）：一页就是它，多页是候选，空数组是没有这一页。 */
export function resolveLink(paths: string[], target: string): string[] {
  const key = pageKey(target)
  if (!key) return []
  const keyed = paths.map((path) => [pageKey(path), path] as const)
  const exact = keyed.find(([found]) => found === key)
  if (exact) return [exact[1]]
  return keyed.filter(([found]) => found?.endsWith('/' + key)).map(([, path]) => path)
}
