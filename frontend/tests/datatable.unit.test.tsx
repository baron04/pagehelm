import {act, createElement} from 'react'
import {createRoot, type Root} from 'react-dom/client'
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import {DEFAULT_CONFIGURATION} from '../src/contract/panel'
import {Answer} from '../src/components/Answer'
import {DataTable, parseNumericCell} from '../src/components/DataTable'
import {parseMarkdown} from '../src/markdown/parse'
import {plainText} from '../src/markdown/plain'

const labels = DEFAULT_CONFIGURATION.labels

function makeTableMarkdown(rowCount: number): string {
  const header = '| 单号 | 客户 | 金额 |\n| --- | --- | ---: |'
  const rows = Array.from({length: rowCount}, (_, index) => {
    const id = `S${String(index + 1).padStart(4, '0')}`
    const customer = `Customer ${index + 1}`
    const amount = `${(rowCount - index) * 100}.50`
    return `| ${id} | ${customer} | ${amount} |`
  })
  return `${header}\n${rows.join('\n')}`
}

describe('DataTable helpers', () => {
  it('parses formatted numeric cells and ignores non-numeric identifiers', () => {
    expect(parseNumericCell('1,231.00')).toBe(1231)
    expect(parseNumericCell('-42.5')).toBe(-42.5)
    expect(parseNumericCell('¥1,200.50')).toBe(1200.5)
    expect(parseNumericCell('12.5%')).toBe(12.5)
    expect(parseNumericCell('S00007')).toBeNull()
    expect(parseNumericCell('2026-01-15')).toBeNull()
    expect(parseNumericCell('—')).toBeNull()
  })
})

describe('DataTable & Answer (UX-01)', () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
  })

  afterEach(() => {
    act(() => { root.unmount() })
    host.remove()
    vi.restoreAllMocks()
  })

  it('renders <= 10 rows without pagination chrome', () => {
    const md = makeTableMarkdown(5)
    act(() => {
      root.render(createElement(Answer, {
        text: md, authored: 'server', live: false, labels, allowedOrigins: ['http://localhost'],
      }))
    })
    expect(host.querySelectorAll('tbody tr')).toHaveLength(5)
    expect(host.querySelector('.woa-datatable__footer')).toBeNull()
    expect(host.querySelector('select')).toBeNull()
  })

  it('paginates > 10 rows at fixed 10 per page with numeric page buttons', () => {
    const md = makeTableMarkdown(30)
    act(() => {
      root.render(createElement(Answer, {
        text: md, authored: 'server', live: false, labels, allowedOrigins: [],
      }))
    })
    expect(host.querySelectorAll('tbody tr')).toHaveLength(10)
    expect(host.querySelector('select')).toBeNull()
    expect(host.querySelector('.woa-datatable__size')).toBeNull()

    const prev = host.querySelector<HTMLButtonElement>(`button[aria-label="${labels.tablePrevPage}"]`)!
    const next = host.querySelector<HTMLButtonElement>(`button[aria-label="${labels.tableNextPage}"]`)!
    const pageButtons = () => Array.from(
      host.querySelectorAll<HTMLButtonElement>('.woa-datatable__pagebtn:not(.woa-datatable__pagebtn--nav)'),
    )

    expect(pageButtons().map(btn => btn.textContent)).toEqual(['1', '2', '3'])
    expect(pageButtons()[0]?.getAttribute('aria-current')).toBe('page')
    expect(prev.disabled).toBe(true)
    expect(next.disabled).toBe(false)

    act(() => { pageButtons()[2]!.click() })
    expect(pageButtons()[2]?.getAttribute('aria-current')).toBe('page')
    expect(host.querySelector('tbody tr td')?.textContent).toBe('S0021')
    expect(prev.disabled).toBe(false)
    expect(next.disabled).toBe(true)

    act(() => { prev.click() })
    expect(pageButtons()[1]?.getAttribute('aria-current')).toBe('page')
    expect(host.querySelector('tbody tr td')?.textContent).toBe('S0011')
    expect(prev.disabled).toBe(false)
    expect(next.disabled).toBe(false)
  })

  it('renders tables immediately even during live model typewriter reveal', () => {
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(() => 1)
    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => undefined)
    const md = '这是前导说明文字。\n\n' + makeTableMarkdown(3)
    act(() => {
      root.render(createElement(Answer, {
        text: md, authored: 'model', live: true, labels, allowedOrigins: [],
      }))
    })
    // Even at visible=0 before rAF ticks, the table and all 3 rows render immediately.
    expect(host.querySelectorAll('tbody tr')).toHaveLength(3)
    expect(host.textContent).toContain('S0001')
    expect(host.textContent).toContain('S0003')
  })

  it('renders small tables (<= 10 rows) without footer and sorts numeric-aware asc/desc/none', () => {
    const md = [
      '| 名称 | 数量 |',
      '| --- | --- |',
      '| B-item | 10 |',
      '| A-item | 2 |',
      '| C-item | 100 |',
    ].join('\n')
    const blocks = parseMarkdown(md)
    const block = blocks[0]!
    if (block.type !== 'table') throw new Error('expected table')

    act(() => {
      root.render(createElement(DataTable, {block, labels, allow: () => false}))
    })

    // Small tables do not render the search toolbar.
    expect(host.querySelector('.woa-datatable__toolbar')).toBeNull()

    // Numeric cells auto-align right even without ---: delimiter.
    const bodyCells = host.querySelectorAll('tbody td')
    expect(bodyCells[1]?.getAttribute('data-align')).toBe('right')
    expect(bodyCells[0]?.getAttribute('data-align')).toBe('left')

    // Sort by numeric column 1: asc -> desc -> none.
    const sortButtons = host.querySelectorAll<HTMLButtonElement>('.woa-datatable__sort')
    const headers = host.querySelectorAll('thead th')
    act(() => { sortButtons[1]!.click() })
    expect(headers[1]?.getAttribute('aria-sort')).toBe('ascending')
    expect(Array.from(host.querySelectorAll('tbody tr td:first-child')).map(n => n.textContent))
      .toEqual(['A-item', 'B-item', 'C-item'])

    act(() => { sortButtons[1]!.click() })
    expect(headers[1]?.getAttribute('aria-sort')).toBe('descending')
    expect(Array.from(host.querySelectorAll('tbody tr td:first-child')).map(n => n.textContent))
      .toEqual(['C-item', 'B-item', 'A-item'])

    act(() => { sortButtons[1]!.click() })
    expect(headers[1]?.getAttribute('aria-sort')).toBe('none')
    expect(Array.from(host.querySelectorAll('tbody tr td:first-child')).map(n => n.textContent))
      .toEqual(['B-item', 'A-item', 'C-item'])
  })

  it('keeps paginated tables (> 10 rows) free of search/export toolbars and keeps plainText intact', () => {
    const md = makeTableMarkdown(12)
    act(() => {
      root.render(createElement(Answer, {
        text: md, authored: 'server', live: false, labels, allowedOrigins: [],
      }))
    })

    expect(host.querySelector('.woa-datatable__toolbar')).toBeNull()
    expect(host.querySelector('.woa-datatable__filter')).toBeNull()
    expect(host.querySelector('.woa-datatable__actions')).toBeNull()
    expect(host.querySelector('.woa-datatable__count')?.textContent).toBe('共 12 条')
    const firstRowCells = host.querySelectorAll('tbody tr:first-child td')
    expect(firstRowCells[0]?.getAttribute('data-wrap')).toBe('nowrap')
    expect(firstRowCells[1]?.getAttribute('data-wrap')).toBe('wrap')
    expect(firstRowCells[2]?.getAttribute('data-wrap')).toBe('nowrap')

    const smallMd = '| 客户 | 金额 |\n| --- | ---: |\n| "Acme, Co" | 1,231.00 |'
    expect(plainText(smallMd)).toBe('客户\t金额\n"Acme, Co"\t1,231.00')
  })

  it('keeps foreign links inert inside DataTable cells and allows same-origin links', () => {
    const md = '| 名称 | 链接 |\n| --- | --- |\n| 本地 | [查看](http://localhost/orders/1) |\n| 外部 | [危险](https://evil.example/x) |'
    act(() => {
      root.render(createElement(Answer, {
        text: md, authored: 'server', live: false, labels, allowedOrigins: ['http://localhost'],
      }))
    })
    const links = host.querySelectorAll('a')
    expect(links).toHaveLength(1)
    expect(links[0]?.getAttribute('href')).toBe('http://localhost/orders/1')
    expect(host.querySelector('.woa-md__inert')?.textContent).toBe('危险')
  })
})
