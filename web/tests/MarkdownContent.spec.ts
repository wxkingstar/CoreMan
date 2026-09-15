import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import MarkdownContent from '@/components/MarkdownContent.vue'

describe('MarkdownContent', () => {
  it('renders headings, lists, tables, quotes, links and fenced code', () => {
    const wrapper = mount(MarkdownContent, { props: { content: '# 标题\n\n**重点**\n\n- 项目\n\n> 引用\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n[链接](https://example.com)\n\n```js\nconst x = 1\n```' } })
    expect(wrapper.find('h1').text()).toBe('标题')
    expect(wrapper.find('strong').text()).toBe('重点')
    expect(wrapper.find('li').text()).toBe('项目')
    expect(wrapper.find('blockquote').text()).toBe('引用')
    expect(wrapper.find('td').text()).toBe('1')
    expect(wrapper.find('a').attributes('href')).toBe('https://example.com')
    expect(wrapper.find('pre code').text()).toContain('const x = 1')
  })
  it('does not execute raw HTML or unsafe links and updates with the content', async () => {
    const wrapper = mount(MarkdownContent, { props: { content: '<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n[bad](javascript:alert(1))' } })
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.find('[onerror]').exists()).toBe(false)
    expect(wrapper.find('a').exists()).toBe(false)
    await wrapper.setProps({ content: '**新回复**' })
    expect(wrapper.find('strong').text()).toBe('新回复')
  })
})
