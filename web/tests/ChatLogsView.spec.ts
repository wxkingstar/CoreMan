import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { log } = vi.hoisted(() => ({
  log: { id: 9, bot_id: 'b1', bot_key: 'sales_bot', platform: 'wecom', user_id: 'u1', user_login: 'zhangsan', user_name: '张三', chat_type: 'single', chat_id: 'zs', session_key: 'zs', relay_session_id: 'sid', model: 'm', stream_id: 's', task_id: 1, message_type: 'text', message_preview: '你好', response_preview: '世界', tools_used: ['Bash'], status: 'success', error_code: null, latency_ms: 1234, input_tokens: 10, output_tokens: 5, cache_read_tokens: null, cache_creation_tokens: null, request_at: '2026-09-11T00:00:00Z', response_at: '2026-09-11T00:00:01Z' },
}))

vi.mock('@/api/admin', () => ({
  chatLogs: {
    list: vi.fn().mockResolvedValue({ items: [log], total: 1, page: 1, per_page: 50 }),
    get: vi.fn().mockResolvedValue({ ...log, message_content: '你好完整', response_content: '世界完整', error_message: null }),
    stats: vi.fn().mockResolvedValue({ total: 1, by_status: { success: 1 }, avg_latency_ms: 1234, tokens: { input: 10, output: 5, cache_read: 0, cache_creation: 0 }, by_bot: [{ bot_id: 'b1', bot_key: 'sales_bot', total: 1, success: 1, error: 0, avg_latency_ms: 1234 }] }),
  },
  bots: { list: vi.fn().mockResolvedValue({ items: [{ id: 'b1', bot_key: 'sales_bot', name: '销售' }], total: 1, page: 1, per_page: 200 }) },
}))

import { chatLogs } from '@/api/admin'
import type { ChatLogOut } from '@/api/types'
import { i18n } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import ChatLogsView from '@/views/ChatLogsView.vue'

const DETAIL = { ...log, message_content: '你好完整', response_content: '世界完整', error_message: null } as unknown as ChatLogOut

function drawerText(): string {
  return document.querySelector('[data-test="drawer"]')?.textContent ?? ''
}

describe('ChatLogsView', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('lists, filters and opens detail', async () => {
    useAuthStore().user = { id: 'u1', login_name: 'zhangsan', display_name: '张三', role: 'member', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: null }
    const wrapper = mount(ChatLogsView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
    await flushPromises()
    expect(wrapper.text()).toContain('sales_bot')
    expect(wrapper.text()).toContain('2026-09-11 08:00:00')
    const vm = wrapper.vm as unknown as { paged: { filters: { status: string }; load: () => Promise<void> }; openDetail: (id: number) => Promise<void> }
    vm.paged.filters.status = 'error'
    await vm.paged.load()
    expect(chatLogs.list).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'error' }))
    await vm.openDetail(9)
    await flushPromises()
    expect(chatLogs.get).toHaveBeenCalledWith(9)
    expect(document.body.textContent).toContain('世界完整')
    wrapper.unmount()
  })

  // 企微引用消息与文件消息：详情端点下发 quoted_content / file_info，抽屉要把它们摊开。
  it('renders quoted content and file info in the detail drawer', async () => {
    useAuthStore().user = { id: 'u1', login_name: 'zhangsan', display_name: '张三', role: 'member', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: null }
    vi.mocked(chatLogs.get).mockResolvedValueOnce({
      ...DETAIL,
      quoted_content: '原话',
      file_info: { filename: '季报.pdf', size: 1024, mime: 'application/pdf' },
    })
    const wrapper = mount(ChatLogsView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
    await flushPromises()
    await (wrapper.vm as unknown as { openDetail: (id: number) => Promise<void> }).openDetail(9)
    await flushPromises()
    const text = drawerText()
    expect(text).toContain(i18n.global.t('chatLogs.quoted'))
    expect(text).toContain('原话')
    expect(text).toContain(i18n.global.t('chatLogs.file'))
    expect(text).toContain('季报.pdf')
    expect(text).toContain('application/pdf')
    expect(text).toContain('1.0 KB')
    wrapper.unmount()
  })

  it('omits the quoted and file blocks when the detail has neither', async () => {
    useAuthStore().user = { id: 'u1', login_name: 'zhangsan', display_name: '张三', role: 'member', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: null }
    vi.mocked(chatLogs.get).mockResolvedValueOnce({ ...DETAIL, quoted_content: null, file_info: null })
    const wrapper = mount(ChatLogsView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
    await flushPromises()
    await (wrapper.vm as unknown as { openDetail: (id: number) => Promise<void> }).openDetail(9)
    await flushPromises()
    const text = drawerText()
    expect(text).toContain('世界完整')
    expect(text).not.toContain(i18n.global.t('chatLogs.quoted'))
    expect(text).not.toContain(i18n.global.t('chatLogs.file'))
    wrapper.unmount()
  })
})
