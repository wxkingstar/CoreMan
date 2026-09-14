import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it } from 'vitest'
import SecretInput from '@/components/SecretInput.vue'
import { i18n } from '@/i18n'

describe('SecretInput', () => {
  it('shows masked value, allows editing and cancelling', async () => {
    const wrapper = mount(SecretInput, { props: { modelValue: 'ab••••yz' }, global: { plugins: [ElementPlus, i18n] } })
    const input = wrapper.get('input')
    expect((input.element as HTMLInputElement).value).toBe('ab••••yz')
    expect((input.element as HTMLInputElement).type).toBe('text')
    expect((input.element as HTMLInputElement).disabled).toBe(true)
    await wrapper.get('[data-test="modify"]').trigger('click')
    expect((wrapper.get('input').element as HTMLInputElement).disabled).toBe(false)
    expect((wrapper.get('input').element as HTMLInputElement).type).toBe('password')
    await wrapper.get('input').setValue('new-secret')
    const afterInput = wrapper.emitted('update:modelValue') ?? []
    expect(afterInput[afterInput.length - 1]).toEqual(['new-secret'])
    await wrapper.get('[data-test="cancel"]').trigger('click')
    const afterCancel = wrapper.emitted('update:modelValue') ?? []
    expect(afterCancel[afterCancel.length - 1]).toEqual(['ab••••yz'])
  })
})
