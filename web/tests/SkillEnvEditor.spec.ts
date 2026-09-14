import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it } from 'vitest'
import SkillEnvEditor from '@/components/SkillEnvEditor.vue'
import { i18n } from '@/i18n'
import type { EnvEntry } from '@/utils/dotenv'

describe('skill environment editing', () => {
  function editor(entries: EnvEntry[] = []) {
    const wrapper = mount(SkillEnvEditor, { props: { modelValue: entries }, global: { plugins: [ElementPlus, i18n] } })
    return wrapper
  }
  it('uses the latest raw text when saving without relying on blur', async () => {
    const wrapper = editor([{ key: 'API_KEY', value: '******abcd' }])
    await wrapper.get('input[value=raw]').setValue(true)
    const area = wrapper.get('textarea')
    expect((area.element as HTMLTextAreaElement).value).toContain('******abcd')
    await area.setValue('API_KEY="new#secret"\nURL=https://example.com')
    expect(wrapper.vm.read()).toEqual([{ key: 'API_KEY', value: 'new#secret' }, { key: 'URL', value: 'https://example.com' }])
    wrapper.unmount()
  })
  it('keeps malformed raw edits visible and blocks mode changes and saving', async () => {
    const wrapper = editor()
    await wrapper.get('input[value=raw]').setValue(true)
    await wrapper.get('textarea').setValue('PASSWORD=first\nPASSWORD=second')
    await wrapper.get('input[value=pairs]').setValue(true)
    expect(wrapper.find('textarea').exists()).toBe(true)
    expect(wrapper.text()).toContain('第 2 行')
    expect(wrapper.vm.read).toThrow()
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    wrapper.unmount()
  })
  it('transfers parsed raw values to pairs and preserves literal values', async () => {
    const wrapper = editor()
    await wrapper.get('input[value=raw]').setValue(true)
    await wrapper.get('textarea').setValue('API_KEY="a#b"\nEMPTY=')
    await wrapper.get('input[value=pairs]').setValue(true)
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual([[{ key: 'API_KEY', value: 'a#b' }, { key: 'EMPTY', value: '' }]])
    wrapper.unmount()
  })
})

it('blocks a partly deleted mask in raw mode and accepts a full replacement', async () => {
  const wrapper = mount(SkillEnvEditor, { props: { modelValue: [{ key: 'PASSWORD', value: '••••' }] }, global: { plugins: [ElementPlus, i18n] } })
  await wrapper.get('input[value=raw]').setValue(true)
  await wrapper.get('textarea').setValue('PASSWORD="•••"')
  expect(wrapper.vm.read).toThrow('掩码已被修改')
  await wrapper.get('textarea').setValue('PASSWORD="••••"')
  expect(wrapper.vm.read()).toEqual([{ key: 'PASSWORD', value: '••••' }])
  await wrapper.get('textarea').setValue('PASSWORD="complete-new-value"')
  expect(wrapper.vm.read()).toEqual([{ key: 'PASSWORD', value: 'complete-new-value' }])
  wrapper.unmount()
})
