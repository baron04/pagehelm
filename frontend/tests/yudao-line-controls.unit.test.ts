import {afterEach, describe, expect, it} from 'vitest'
// @ts-expect-error Application module is served separately from the generic Runtime.
import {lineControls, withLineControls, withObservedRowColumns} from '../src/integrations/yudao/page-facts.js'

const TABLE = `
<form class="el-form"><div class="el-table">
  <div class="el-table__header-wrapper"><table><thead><tr>
    <th>序号</th><th>产品名称</th><th>数量</th><th>金额</th><th>操作</th></tr></thead></table></div>
  <div class="el-table__body-wrapper"><table><tbody>
    <tr><td>1</td>
      <td><div class="el-select"><div class="el-select__wrapper">
        <span class="el-select__placeholder is-transparent">请选择产品</span>
        <input role="combobox" aria-haspopup="listbox" aria-controls="products-1"></div></div></td>
      <td><input type="number" role="spinbutton" id="count-1"></td>
      <td><input disabled value="0.00"></td><td><button>—</button></td></tr>
    <tr><td>2</td>
      <td><div class="el-select"><div class="el-select__wrapper">
        <span class="el-select__placeholder">周转 纸箱</span>
        <input role="combobox" aria-haspopup="listbox" aria-controls="products-2"></div></div></td>
      <td><input type="number" role="spinbutton" id="count-2"></td>
      <td><input disabled value="0.00"></td><td><button>—</button></td></tr>
  </tbody></table></div>
</div></form>`

function mount(): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = TABLE
  document.body.append(host)
  for (const element of host.querySelectorAll('*')) {
    element.getBoundingClientRect = () => ({width: 120, height: 32, top: 100, bottom: 132, left: 0, right: 120,
      x: 0, y: 100, toJSON: () => ({})})
  }
  return host
}

afterEach(() => { document.body.innerHTML = '' })

describe('controls of an editable line table', () => {
  it('names the row and the column of each enabled control, and what a select holds', () => {
    const host = mount()
    const found = lineControls()
    expect(found.map((line: {row: number; column: string}) => `${line.row}:${line.column}`))
      .toEqual(['1:产品名称', '1:数量', '2:产品名称', '2:数量'])
    const [first, count, second] = found
    expect(first.area).toBe(host.querySelector('.el-select__wrapper'))
    expect(first.control.getAttribute('aria-controls')).toBe('products-1')
    expect(first).toMatchObject({select: true, placeholder: '请选择产品'})
    expect(count).toMatchObject({select: false})
    expect(count.area).toBe(host.querySelector('#count-1'))
    expect(second).toMatchObject({select: true, value: '周转 纸箱'})
  })

  it('leaves out what is not on the screen and tables outside a form', () => {
    const host = mount()
    host.querySelector<HTMLElement>('#count-2')!.getBoundingClientRect = () => ({width: 120, height: 32,
      top: 5000, bottom: 5032, left: 0, right: 120, x: 0, y: 5000, toJSON: () => ({})})
    expect(lineControls().map((line: {row: number; column: string}) => `${line.row}:${line.column}`))
      .toEqual(['1:产品名称', '1:数量', '2:产品名称'])
    host.querySelector('form')!.className = ''
    expect(lineControls()).toEqual([])
  })
})

describe('line controls in the page text', () => {
  it('titles an indexed control and indexes a select the generic controller left out', () => {
    const host = mount()
    const lines = lineControls()
    const map = new Map<number, {ref: Element}>([
      [7, {ref: host.querySelector('.el-table')!}], [8, {ref: host.querySelector('#count-1')!}],
      [9, {ref: host.querySelector('#count-2')!}]])
    const html = ['[7]<div data-scrollable="right=479, " >1', '请选择产品 />',
      '\t[8]<input type=number role=spinbutton id=count-1 />',
      '\t[9]<input type=number role=spinbutton id=count-2 />'].join('\n')
    const text = withLineControls(map, html, lines).split('\n')
    expect(text).toEqual([
      '[7]<div data-scrollable="right=479, " >1', '请选择产品 />',
      '\t[10]<input role=combobox aria-haspopup=listbox title=明细第1行·产品名称 placeholder=请选择产品 />',
      '\t[8]<input type=number role=spinbutton id=count-1 title=明细第1行·数量 />',
      '\t[11]<input role=combobox aria-haspopup=listbox title=明细第2行·产品名称 value="周转 纸箱" />',
      '\t[9]<input type=number role=spinbutton id=count-2 title=明细第2行·数量 />'])
    expect(map.get(10)!.ref).toBe(host.querySelectorAll('.el-select__wrapper')[0])
    expect(map.get(11)!.ref).toBe(host.querySelectorAll('.el-select__wrapper')[1])
    expect(withLineControls(new Map(), 'x', [])).toBe('x')
  })
})

describe('observed table row facts', () => {
  it('pairs only already observed numeric values with their live column headers', () => {
    const host = document.createElement('div')
    host.innerHTML = `<div class="el-table"><div class="el-table__header-wrapper"><table><thead><tr>
      <th>订单单号</th><th>金额合计</th><th>含税金额</th><th>收取订金</th><th>联系人</th>
      </tr></thead></table></div><div class="el-table__body-wrapper"><table><tbody><tr>
      <td>XS2026090001</td><td>2450.00</td><td>2630.07</td><td>0.00</td><td>张三</td>
      </tr></tbody></table></div></div>`
    document.body.append(host)
    for (const element of host.querySelectorAll('*')) {
      element.getBoundingClientRect = () => ({width: 120, height: 32, top: 100, bottom: 132, left: 0, right: 120,
        x: 0, y: 100, toJSON: () => ({})})
    }
    const wrapper = host.querySelector('.el-table__body-wrapper')!
    const selectors = new Map([[78, {ref: wrapper, tagName: 'div'}]])
    const html = '订单金额: 0.00\n[78]<div data-scrollable="right=302, " >XS2026090001\n2450.00\n2630.07 />\n页脚 0.00'
    const observed = (page: string) => withObservedRowColumns(page, document, selectors)
    expect(observed(html)).toContain('金额合计=2450.00；含税金额=2630.07')
    expect(observed(html)).not.toContain('收取订金=0.00')
    expect(observed('[78]<div >XS2026090001\n2450.00 />\n页脚 2630.07')).not.toContain('含税金额=2630.07')
    expect(observed(html)).not.toContain('联系人=张三')
    expect(withObservedRowColumns(html)).toBe(html)
    const dialog = document.createElement('div')
    dialog.className = 'el-dialog'
    dialog.getBoundingClientRect = () => ({width: 120, height: 32, top: 100, bottom: 132, left: 0, right: 120,
      x: 0, y: 100, toJSON: () => ({})})
    document.body.append(dialog)
    expect(observed(html)).toBe(html)
  })

  it('keeps two visible rows separate and omits values clipped from one row', () => {
    const host = document.createElement('div')
    host.innerHTML = `<div class="el-table"><div class="el-table__header-wrapper"><table><thead><tr>
      <th>单号</th><th>含税金额</th><th>状态</th></tr></thead></table></div>
      <div class="el-table__body-wrapper"><table><tbody>
      <tr><td>XS001</td><td>2630.07</td><td>已完成</td></tr>
      <tr><td>XS002</td><td>140.00</td><td>待处理</td></tr>
      </tbody></table></div></div>`
    document.body.append(host)
    for (const element of host.querySelectorAll('*')) {
      element.getBoundingClientRect = () => ({width: 120, height: 32, top: 100, bottom: 132,
        left: 0, right: 120, x: 0, y: 100, toJSON: () => ({})})
    }
    const wrapper = host.querySelector('.el-table__body-wrapper')!
    const selectors = new Map([[78, {ref: wrapper, tagName: 'div'}]])
    const html = '[78]<div >XS001\n2630.07\n已完成\nXS002\n140.00 />'
    const result = withObservedRowColumns(html, document, selectors)
    expect(result).toContain('第1行 单号=XS001；含税金额=2630.07；状态=已完成')
    expect(result).toContain('第2行 单号=XS002；含税金额=140.00')
    expect(result).not.toContain('状态=待处理')
  })
})
