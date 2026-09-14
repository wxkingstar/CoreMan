import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it } from 'vitest'
import EnvVarsEditor from '@/components/EnvVarsEditor.vue'
import { i18n } from '@/i18n'

describe('EnvVarsEditor', () => {
  it('converts between modes and emits invalid on bad json', async () => {
    const wrapper = mount(EnvVarsEditor, { props: { modelValue: { A: '1', B: 'x=y' } }, global: { plugins: [ElementPlus, i18n] } })
    await wrapper.get('[data-test="mode-raw"]').trigger('click')
    const raw = wrapper.get('[data-test="raw-text"] textarea')
    expect((raw.element as HTMLTextAreaElement).value).toBe('A=1\nB=x=y')
    await raw.setValue('A=1\n# comment\nC=3')
    await raw.trigger('blur')
    // tsconfig 的 lib 还没到 es2022，用不了 Array.prototype.at（同 SecretInput.spec 的写法）。
    const emitted = wrapper.emitted('update:modelValue') ?? []
    expect(emitted[emitted.length - 1]).toEqual([{ A: '1', C: '3' }])
    await wrapper.get('[data-test="mode-json"]').trigger('click')
    const json = wrapper.get('[data-test="json-text"] textarea')
    await json.setValue('{bad json')
    await json.trigger('blur')
    expect(wrapper.emitted('invalid')).toBeTruthy()
  })
})
