import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { expect, it } from 'vitest'
import { i18n } from '@/i18n'
import QuotaMeter from '@/components/QuotaMeter.vue'
const render = (value: number | null, collectedAt: string | null, resetsAt: string | null = null) => mount(QuotaMeter, { props: { value, collectedAt, resetsAt }, global: { plugins: [ElementPlus, i18n] } })
it('does not render missing or stale quota as an available zero percent', () => {
  for (const wrapper of [render(null, null), render(0, null), render(0, new Date(Date.now() - 3 * 3600000).toISOString()), render(0, new Date().toISOString(), new Date(Date.now() - 1000).toISOString())]) {
    expect(wrapper.find('.el-progress').exists()).toBe(false); wrapper.unmount()
  }
})
it('shows a measured zero only when its collection is fresh', () => {
  const wrapper = render(0, new Date().toISOString())
  expect(wrapper.find('.el-progress').exists()).toBe(true)
  expect(wrapper.text()).toContain('0%'); wrapper.unmount()
})
