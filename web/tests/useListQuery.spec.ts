/* eslint-disable vue/one-component-per-file -- independent composable harnesses */
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, expect, it, vi } from 'vitest'
import { useListQuery } from '@/composables/useListQuery'
beforeEach(() => sessionStorage.clear())
it('restores nullable booleans, page and keyword from deep links and list return', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/bots', component: { template: '<div />' } }, { path: '/bots/:id', component: { template: '<div />' } }] })
  const reload = vi.fn()
  const component = defineComponent({ setup() { const page = ref(1), enabled = ref<boolean | null>(null), keyword = ref(''); const { persist } = useListQuery({ page, enabled, keyword }, reload); return { page, enabled, keyword, persist } }, template: '<div />' })
  await router.push('/bots?page=3&enabled=false&keyword=test')
  let wrapper = mount(component, { global: { plugins: [router] } })
  expect(wrapper.vm.page).toBe(3); expect(wrapper.vm.enabled).toBe(false)
  wrapper.vm.persist(); await flushPromises(); wrapper.unmount()
  await router.push('/bots/42'); await router.push('/bots')
  wrapper = mount(component, { global: { plugins: [router] } })
  expect(wrapper.vm.page).toBe(3); expect(wrapper.vm.keyword).toBe('test')
  await router.replace('/bots?page=2&enabled=true'); await flushPromises()
  expect(wrapper.vm.page).toBe(2); expect(wrapper.vm.enabled).toBe(true); expect(reload).toHaveBeenCalledOnce()
  wrapper.unmount()
})
it('rejects invalid pagination without changing defaults', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/bots', component: { template: '<div />' } }] })
  await router.push('/bots?page=-2&per_page=99999')
  const wrapper = mount(defineComponent({ setup() { const page = ref(1), per_page = ref(50); useListQuery({ page, per_page }); return { page, per_page } }, template: '<div />' }), { global: { plugins: [router] } })
  expect(wrapper.vm.page).toBe(1); expect(wrapper.vm.per_page).toBe(50); wrapper.unmount()
})
