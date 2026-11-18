import {act, createElement} from 'react'
import {createRoot, type Root} from 'react-dom/client'
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import {DEFAULT_CONFIGURATION} from '../src/contract/panel'
import {Answer} from '../src/components/Answer'
import {DataTable, escapeCsvCell, formatCsv, formatTsv, parseNumericCell} from '../src/components/DataTable'
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

function setInputValue(input: HTMLInputElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
  setter?.call(input, value)
  input.dispatchEvent(new Event('input', {bubbles: true}))
  input.dispatchEvent(new Event('change', {bubbles: true}))
}

function setSelectValue(select: HTMLSelectElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set
  setter?.call(select, value)
  select.dispatchEvent(new Event('change', {bubbles: true}))
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

  it('formats UTF-8 BOM RFC 4180 CSV and TSV cleanly', () => {
    expect(escapeCsvCell('plain')).toBe('plain')
    expect(escapeCsvCell('a, "b"\nc')).toBe('"a, ""b""\nc"')
    const csv = formatCsv(['名称', '备注'], [['A, B', 'line1\n"q"']])
    expect(csv.startsWith('\uFEFF')).toBe(true)
    expect(csv).toBe('\uFEFF名称,备注\r\n"A, B","line1\n""q"""')
    expect(formatTsv(['名称', '备注'], [['A\tB', 'line1\nline2']])).toBe('名称\t备注\nA B\tline1 line2')
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

  it('paginates > 10 rows with 10 / 25 / 50 / showAll and Prev/Next controls', () => {
    const md = makeTableMarkdown(30)
    act(() => {
      root.render(createElement(Answer, {
        text: md, authored: 'server', live: false, labels, allowedOrigins: [],
      }))
    })
    expect(host.querySelectorAll('tbody tr')).toHaveLength(10)
    expect(host.querySelector('.woa-datatable__pageinfo')?.textContent).toBe('1 / 3')

    const buttons = Array.from(host.querySelectorAll<HTMLButtonElement>('.woa-datatable__pagebtn'))
    const prev = buttons[0]!
    const next = buttons[1]!
    expect(prev.disabled).toBe(true)
    expect(next.disabled).toBe(false)

    act(() => { next.click() })
    expect(host.querySelector('.woa-datatable__pageinfo')?.textContent).toBe('2 / 3')
    expect(host.querySelector('tbody tr td')?.textContent).toBe('S0011')
    expect(prev.disabled).toBe(false)

    const select = host.querySelector<HTMLSelectElement>('select')!
    expect(Array.from(select.options).map(opt => opt.value)).toEqual(['10', '25', '50', 'all'])

    act(() => { setSelectValue(select, '25') })
    expect(host.querySelectorAll('tbody tr')).toHaveLength(25)
    expect(host.querySelector('.woa-datatable__pageinfo')?.textContent).toBe('1 / 2')

    act(() => { setSelectValue(select, 'all') })
    expect(host.querySelectorAll('tbody tr')).toHaveLength(30)
    expect(prev.disabled).toBe(true)
    expect(next.disabled).toBe(true)
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

  it('filters rows, shows tableNoMatch on empty match, and sorts numeric-aware asc/desc/none', () => {
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

    // Text filter.
    const filter = host.querySelector<HTMLInputElement>(`input[placeholder="${labels.tableFilterPlaceholder}"]`)!
    act(() => { setInputValue(filter, 'A-item') })
    expect(host.querySelectorAll('tbody tr')).toHaveLength(1)
    expect(host.querySelector('tbody td')?.textContent).toBe('A-item')

    act(() => { setInputValue(filter, 'not-found-xyz') })
    expect(host.querySelector('.woa-datatable__empty')?.textContent).toBe(labels.tableNoMatch)
  })

  it('copies TSV and downloads UTF-8 BOM CSV while keeping plainText intact', async () => {
    const md = '| 客户 | 金额 |\n| --- | ---: |\n| "Acme, Co" | 1,231.00 |'
    Object.defineProperty(window, 'isSecureContext', {value: true, configurable: true})
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {value: {writeText}, configurable: true})

    const capturedBlobs: Blob[] = []
    const createObjectURL = vi.fn((blob: Blob) => {
      capturedBlobs.push(blob)
      return 'blob:test-csv'
    })
    const revokeObjectURL = vi.fn()
    Object.defineProperty(URL, 'createObjectURL', {value: createObjectURL, configurable: true})
    Object.defineProperty(URL, 'revokeObjectURL', {value: revokeObjectURL, configurable: true})
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)

    act(() => {
      root.render(createElement(Answer, {
        text: md, authored: 'server', live: false, labels, allowedOrigins: [],
      }))
    })

    const copyBtn = Array.from(host.querySelectorAll('button')).find(
      btn => btn.textContent?.includes(labels.tableCopyTsv))!
    await act(async () => {
      copyBtn.click()
      await Promise.resolve()
    })
    expect(writeText).toHaveBeenCalledWith('客户\t金额\n"Acme, Co"\t1,231.00')

    const csvBtn = Array.from(host.querySelectorAll('button')).find(
      btn => btn.textContent?.includes(labels.tableDownloadCsv))!
    act(() => { csvBtn.click() })
    expect(createObjectURL).toHaveBeenCalledOnce()
    expect(clickSpy).toHaveBeenCalledOnce()
    const csvBytes = await new Promise<Uint8Array>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer))
      reader.onerror = () => reject(reader.error)
      reader.readAsArrayBuffer(capturedBlobs[0]!)
    })
    expect(Array.from(csvBytes.slice(0, 3))).toEqual([0xef, 0xbb, 0xbf])
    const csvText = new TextDecoder('utf-8', {ignoreBOM: true}).decode(csvBytes)
    expect(csvText).toBe('\uFEFF客户,金额\r\n"""Acme, Co""","1,231.00"')

    // copyAnswer path (`plainText`) remains untouched.
    expect(plainText(md)).toBe('客户\t金额\n"Acme, Co"\t1,231.00')
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
