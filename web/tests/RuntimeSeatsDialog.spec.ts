import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { describe, expect, it, vi } from 'vitest'

const { seat } = vi.hoisted(() => ({
  seat: (id: string, label: string, active: boolean, login = 'ready') => ({ id, label, account: `${id}@example.com`, login, active }),
}))

vi.mock('@/api/runtimeNodes', () => ({
  runtimeSeats: {
    create: vi.fn().mockResolvedValue({ seat_id: 'aaaaaaaaaaaa', seats: [seat('default', '', true), seat('aaaaaaaaaaaa', '张三', false, 'required')] }),
    startLogin: vi.fn().mockResolvedValue({ session_id: 's1', authorization_url: 'https://claude.com/cai/oauth/authorize?code=true', user_code: '', needs_code: true, expires_in: 900 }),
    completeLogin: vi.fn().mockResolvedValue({ seats: [seat('default', '', true), seat('aaaaaaaaaaaa', '张三', false)] }),
    activate: vi.fn().mockResolvedValue({ seats: [seat('default', '', false), seat('aaaaaaaaaaaa', '张三', true)] }),
    remove: vi.fn().mockResolvedValue({ seats: [seat('default', '', true)] }),
  },
}))
import { runtimeSeats } from '@/api/runtimeNodes'
import { i18n } from '@/i18n'
import RuntimeSeatsDialog from '@/components/RuntimeSeatsDialog.vue'

const node = (claude: Record<string, unknown>) => ({
  id: 'n1', name: 'runtime-02',
  capabilities: { claude: { installed: true, login: 'ready', ...claude }, codex: { installed: false } },
  backends: [{ id: 'r1', model_provider: 'claude', rate_limit_5h_used_pct: 100, rate_limit_5h_resets_at: null, rate_limit_7d_used_pct: 40, rate_limit_7d_resets_at: null, seat_quotas: { aaaaaaaaaaaa: { rate_limit_5h_used_pct: 5, rate_limit_5h_resets_at: null, rate_limit_7d_used_pct: null, rate_limit_7d_resets_at: null } } }],
})

// el-dialog 渲染到 body 下，元素都从 document 里找。
const q = <T extends Element = HTMLElement>(selector: string) => document.querySelector(selector) as T | null
async function type(selector: string, value: string) {
  // el-input 把 data-test 交给内部的 input 元素。
  const input = (q<HTMLInputElement>(`input${selector}`) ?? q<HTMLInputElement>(`${selector} input`))!
  input.value = value
  input.dispatchEvent(new Event('input'))
  await flushPromises()
}
async function click(selector: string) {
  q<HTMLButtonElement>(selector)!.click()
  await flushPromises()
}

function mountDialog(claude: Record<string, unknown>) {
  return mount(RuntimeSeatsDialog, { props: { node: node(claude) as never, visible: true }, global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
}

describe('RuntimeSeatsDialog', () => {
  it('asks old runtimes to upgrade', async () => {
    const wrapper = mountDialog({})
    await flushPromises()
    expect(document.body.textContent).toContain('请先升级 Runtime')
    wrapper.unmount()
  })

  it('adds a seat, signs it in with the code and switches to it', async () => {
    const wrapper = mountDialog({ seat_switch_v1: true, seats: [seat('default', '', true)] })
    await flushPromises()
    expect(document.body.textContent).toContain('默认席位')
    await type('[data-test="seat-label-claude"]', '张三')
    await click('[data-test="seat-add-claude"]')
    expect(runtimeSeats.create).toHaveBeenCalledWith('n1', 'claude', '张三')
    expect(q('[data-test="activate-aaaaaaaaaaaa"]')!.hasAttribute('disabled')).toBe(true)
    // 没在用的席位未登录时不显示额度；登录后显示切走时存下的额度。
    expect(document.querySelectorAll('[data-test="quota-bar"]').length).toBe(2)

    await click('[data-test="login-aaaaaaaaaaaa"]')
    expect(q('[data-test="seat-login-link"]')!.getAttribute('href')).toContain('https://claude.com/')
    await type('[data-test="seat-login-code"]', ' abc#state ')
    await click('[data-test="seat-login-finish"]')
    expect(runtimeSeats.completeLogin).toHaveBeenCalledWith('n1', 'claude', 'aaaaaaaaaaaa', { session_id: 's1', code: 'abc#state' })
    expect(document.querySelectorAll('[data-test="quota-bar"]').length).toBe(3)

    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValueOnce('confirm' as never)
    await click('[data-test="activate-aaaaaaaaaaaa"]')
    expect(runtimeSeats.activate).toHaveBeenCalledWith('n1', 'claude', 'aaaaaaaaaaaa')
    expect(wrapper.emitted('changed')?.length).toBe(3)
    // 当前席位不能移除，默认席位没有移除按钮。
    expect(q('[data-test="remove-aaaaaaaaaaaa"]')!.hasAttribute('disabled')).toBe(true)
    expect(q('[data-test="remove-default"]')).toBeNull()
    wrapper.unmount()
  })
})
