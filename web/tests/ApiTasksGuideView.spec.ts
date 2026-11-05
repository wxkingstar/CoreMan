import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import ApiTasksGuideView from '@/views/ApiTasksGuideView.vue'
import { i18n } from '@/i18n'

afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = '' })

const render = () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: ApiTasksGuideView }] })
  return mount(ApiTasksGuideView, { global: { plugins: [ElementPlus, i18n, router] }, attachTo: document.body })
}

it('draws the overview, chat example, sequence and states', () => {
  const wrapper = render()
  expect(wrapper.findAll('svg')).toHaveLength(3)
  expect(wrapper.text()).toContain('权限校验')
  expect(wrapper.text()).toContain('通过 API 发起（令牌：告警脚本）')
  expect(wrapper.text()).toContain('waiting_user')
  wrapper.unmount()
})

it('fills the CoreMan address into the agent version and copies it whole', async () => {
  const markdown = '# Guide\n\nBASE={{COREMAN_BASE_URL}}\n'
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, text: () => Promise.resolve(markdown) }))
  const writeText = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
  const wrapper = render()
  await wrapper.get('[data-test="agent-version"]').trigger('click'); await flushPromises()
  expect(fetch).toHaveBeenCalledWith('/integration/api-tasks-agent-guide.md')
  const guide = document.body.querySelector('[data-test="agent-guide"]')
  expect(guide?.textContent).toContain(`BASE=${window.location.origin}`)
  expect(guide?.textContent).not.toContain('COREMAN_BASE_URL')
  ;(document.body.querySelector('[data-test="copy-agent-guide"]') as HTMLElement).click(); await flushPromises()
  expect(writeText).toHaveBeenCalledWith(`# Guide\n\nBASE=${window.location.origin}\n`)
  wrapper.unmount()
})
