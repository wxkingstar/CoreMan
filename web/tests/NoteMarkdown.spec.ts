import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import NoteMarkdown from '@/components/assistant/NoteMarkdown.vue'
import { resolveLink } from '@/utils/noteLinks'

const PATHS = ['人/李四.md', '项目/季度报价', 'x/同名.md', 'y/同名.md']
const render = (content: string) => mount(NoteMarkdown, { props: { content, resolve: (target: string) => resolveLink(PATHS, target) } })

describe('resolveLink', () => {
  it('matches like the server: with or without .md, any case, by path end', () => {
    expect(resolveLink(PATHS, '李四')).toEqual(['人/李四.md'])
    expect(resolveLink(PATHS, '人/李四')).toEqual(['人/李四.md'])
    expect(resolveLink(PATHS, '季度报价.md')).toEqual(['项目/季度报价'])
    expect(resolveLink(PATHS, '同名')).toEqual(['x/同名.md', 'y/同名.md'])
    expect(resolveLink(PATHS, '王五')).toEqual([])
    expect(resolveLink(PATHS, ' / ')).toEqual([])
  })
})

describe('NoteMarkdown', () => {
  it('renders markdown and shows front matter apart from the body', () => {
    const wrapper = render('---\nstatus: 进行中\nowner: 李四\n---\n# 季度报价\n\n- **定价**\n')
    expect(wrapper.get('[data-test="note-props"]').text()).toBe('status: 进行中\nowner: 李四')
    expect(wrapper.find('h1').text()).toBe('季度报价')
    expect(wrapper.find('li strong').text()).toBe('定价')
  })

  it('turns wiki links into links, marks missing and ambiguous ones, and emits on click', async () => {
    const wrapper = render('找 [[李四|老李]]、[[季度报价#进展]]、[[王五]]、[[同名]]、![[李四]]，`[[代码]]`')
    const links = wrapper.findAll('a.wikilink')
    expect(links.map((link) => link.text())).toEqual(['老李', '季度报价#进展', '王五', '同名', '李四'])
    expect(links.map((link) => link.attributes('data-wikilink'))).toEqual(['李四', '季度报价', '王五', '同名', '李四'])
    expect(links[2].classes()).toContain('is-missing')
    expect(links[3].classes()).toContain('is-ambiguous')
    expect(links[0].classes()).not.toContain('is-missing')
    expect(wrapper.find('code').text()).toBe('[[代码]]')
    await links[0].trigger('click')
    expect(wrapper.emitted('open-link')).toEqual([['李四']])
  })

  it('styles callouts', () => {
    const wrapper = render('> [!WARNING] 待核实\n> 两种说法')
    const quote = wrapper.get('blockquote')
    expect(quote.classes()).toEqual(expect.arrayContaining(['callout', 'callout-warning']))
    expect(quote.get('strong').text()).toBe('待核实')
    expect(quote.text()).toContain('两种说法')
  })

  it('never runs HTML, never loads images, and opens outside links in a new window', () => {
    const wrapper = render('<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n![图](https://example.com/a.png)\n[外链](https://example.com)\n[bad](javascript:alert(1))\n[[x" onclick="alert(1)]]')
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('[onerror]').exists()).toBe(false)
    expect(wrapper.find('[onclick]').exists()).toBe(false)
    expect(wrapper.get('a.note-image').text()).toContain('图')
    const outside = wrapper.findAll('a').find((link) => link.text() === '外链')
    expect(outside?.attributes('target')).toBe('_blank')
    expect(outside?.attributes('rel')).toContain('noopener')
    expect(wrapper.findAll('a').some((link) => (link.attributes('href') ?? '').startsWith('javascript'))).toBe(false)
  })
})
