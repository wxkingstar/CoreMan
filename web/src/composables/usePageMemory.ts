/**
 * 任务页之间来回跳时「回到原处」：每个历史条目记下页面状态（展开了什么、列表加载到哪）和
 * 刚才点的是哪一处；按浏览器返回回来时先恢复状态，内容出来后再滚回原来的位置，
 * 并把刚才点的那一处闪一下，提醒是从这里点出去的。
 *
 * 记在 sessionStorage：飞书等内置浏览器返回时整页重载也还在；关掉标签页就没了。
 */
import { getCurrentInstance, onBeforeUnmount } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, type Router, type RouterScrollBehavior } from 'vue-router'

const STORE_KEY = 'coreman.pageMemory'
const MAX_ENTRIES = 40
const READY_TIMEOUT_MS = 3000
const FLASH_CLASS = 'cm-flash'
// 表格、折叠块在内容出来后还会再排版几次，页面高度跟着变：这段时间里持续把位置校正回去。
const SETTLE_MS = 2500

/** 刚才点的那一处：第几个带这个 key 的元素，点的时候它离窗口顶部多远。 */
interface Origin { key: string; index: number; top?: number }
interface Saved { state: unknown; origin: Origin | null; scrollY?: number; at: number }

let popped = false
// 模块一加载就挂上、且用捕获阶段：要比路由自己的 popstate 监听先执行。路由的导航在它的监听里
// 起头，浏览器在两个监听之间会跑完微任务，导航可能已经走完，晚挂的监听就来不及标记。
window.addEventListener('popstate', () => { popped = true }, true)
let returning = false
let entry = ''
let readyEntry = ''
let onReady: (() => void) | null = null

function entryOf(path: string): string {
  const position = (window.history.state as { position?: number } | null)?.position
  return `${path}@${position ?? ''}`
}

function readAll(): Record<string, Saved> {
  try {
    return JSON.parse(sessionStorage.getItem(STORE_KEY) ?? '{}') as Record<string, Saved>
  } catch {
    return {}
  }
}

function writeAll(all: Record<string, Saved>): void {
  const keys = Object.keys(all).sort((a, b) => all[a].at - all[b].at)
  for (const key of keys.slice(0, Math.max(0, keys.length - MAX_ENTRIES))) delete all[key]
  try { sessionStorage.setItem(STORE_KEY, JSON.stringify(all)) } catch { /* 无痕模式、满了：不记 */ }
}

/** 装到路由上：分辨这次是不是按返回回来的，记下当前历史条目。 */
export function installPageMemory(router: Router): void {
  // 内置浏览器按返回时可能整页重载：这时没有 popstate，看导航类型。
  const nav = performance.getEntriesByType?.('navigation')[0] as PerformanceNavigationTiming | undefined
  popped = nav?.type === 'back_forward'
  router.afterEach((to, _from, failure) => {
    if (failure) return
    returning = popped
    popped = false
    entry = entryOf(to.fullPath)
  })
}

/** 内容出来之后再滚：返回时等页面调用 ready()，最多等 3 秒。 */
function whenReady(): Promise<void> {
  if (readyEntry === entry) return Promise.resolve()
  return new Promise((resolve) => {
    const timer = setTimeout(() => { onReady = null; resolve() }, READY_TIMEOUT_MS)
    onReady = () => { clearTimeout(timer); onReady = null; resolve() }
  })
}

/** 给记住位置的页面用的滚动规则：返回时等内容出来再滚回去，进新任务回到顶部；其余页面照旧。 */
export function pageScrollBehavior(names: string[]): RouterScrollBehavior {
  const remembered = new Set(names)
  return (to, from, saved) => {
    const mine = typeof to.name === 'string' && remembered.has(to.name)
    if (saved) return mine ? whenReady().then(() => saved) : saved
    if (mine && to.path !== from.path) return { top: 0 }
    return false
  }
}

/** 页面上所有带这个 key 的元素（按出现顺序）。 */
function withKey(key: string, root: ParentNode = document): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>('[data-back-key]')].filter(el => el.getAttribute('data-back-key') === key)
}

function originElement(origin: Origin): HTMLElement | undefined {
  const all = withKey(origin.key)
  return all[origin.index] ?? all[0]
}

/**
 * 回到原处：有刚才点的元素就让它回到点的时候离窗口顶部的那个距离，没有就回到原来的滚动位置。
 * 内容刚出来时页面还没撑到全高，浏览器会把滚动截短；所以页面高度变化时一直校正，
 * 直到稳定（SETTLE_MS）或用户自己动了（滚轮、触摸、按键）。
 */
function settle(origin: Origin | null, scrollY: number | undefined): void {
  const align = (): void => {
    const el = origin && origin.top !== undefined ? originElement(origin) : undefined
    const target = el ? window.scrollY + el.getBoundingClientRect().top - (origin?.top ?? 0) : scrollY
    if (target === undefined || Math.abs(window.scrollY - target) < 1) return
    window.scrollTo({ top: target, behavior: 'instant' })
  }
  align()
  if (typeof ResizeObserver === 'undefined') return
  const observer = new ResizeObserver(align)
  observer.observe(document.body)
  const events = ['wheel', 'touchstart', 'keydown', 'mousedown'] as const
  const stop = (): void => {
    observer.disconnect()
    for (const name of events) window.removeEventListener(name, stop)
  }
  for (const name of events) window.addEventListener(name, stop, { passive: true, once: true })
  setTimeout(stop, SETTLE_MS)
}

function flash(origin: Origin): void {
  const el = originElement(origin)
  if (!el) return
  const target = el.closest<HTMLElement>('tr') ?? el
  target.classList.remove(FLASH_CLASS)
  void target.offsetWidth // 重新触发动画
  target.classList.add(FLASH_CLASS)
  target.addEventListener('animationend', () => target.classList.remove(FLASH_CLASS), { once: true })
}

/**
 * 页面用：`snapshot` 返回要记的状态（离开时调用），`recall()` 在按返回回来时取回状态（否则 null），
 * 内容渲染好后调 `ready()`：滚回原处并闪一下刚才点的地方。
 * 可点的东西上加 `data-back-key`（同一个 key 出现多次时按出现顺序区分）；
 * 程序里跳转的（比如点表格整行）先调 `markOrigin(key)`。
 */
export function usePageMemory<T>(snapshot: () => T) {
  let origin: Origin | null = null
  let restoredOrigin: Origin | null = null
  let restoredScroll: number | undefined
  let restoredFor = ''

  function originOf(el: Element): Origin {
    const key = el.getAttribute('data-back-key') ?? ''
    return { key, index: withKey(key).indexOf(el as HTMLElement), top: el.getBoundingClientRect().top }
  }

  function onClick(event: MouseEvent): void {
    const el = (event.target as Element | null)?.closest?.('[data-back-key]')
    if (el) origin = originOf(el)
  }
  document.addEventListener('click', onClick, true)
  if (getCurrentInstance()) onBeforeUnmount(() => document.removeEventListener('click', onClick, true))

  function save(): void {
    if (!entry) return
    const all = readAll()
    all[entry] = { state: snapshot(), origin, scrollY: window.scrollY, at: Date.now() }
    writeAll(all)
    origin = null
  }
  onBeforeRouteLeave(save)
  onBeforeRouteUpdate(save)

  return {
    /** 点表格整行这类程序里跳转的：传点击事件，按那一行里带这个 key 的元素记位置。 */
    markOrigin(key: string, event?: Event): void {
      const row = (event?.target as Element | null)?.closest?.('tr')
      const el = row ? withKey(key, row)[0] : undefined
      origin = el ? originOf(el) : { key, index: 0 }
    },
    recall(): T | null {
      restoredOrigin = null
      restoredScroll = undefined
      restoredFor = entry
      if (!returning) return null
      const saved = readAll()[entry]
      if (!saved) return null
      restoredOrigin = saved.origin
      restoredScroll = saved.scrollY
      return saved.state as T
    },
    ready(): void {
      if (restoredFor !== entry) return
      readyEntry = entry
      onReady?.()
      const target = restoredOrigin
      const scrollY = restoredScroll
      restoredOrigin = null
      restoredScroll = undefined
      if (!target && scrollY === undefined) return
      // 路由自己的滚动发生在 ready 之后的下一帧；在它之后接手校正，再闪一下。
      setTimeout(() => {
        settle(target, scrollY)
        if (target) flash(target)
      }, 60)
    },
  }
}
