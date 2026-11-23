import {afterEach, describe, expect, it} from 'vitest'
// @ts-expect-error Generic Element Plus modules ship as framework-free JS.
import {isElDateRangeInput, isElMonthRangeInput, isElRangePickerInput, nativeMonthRangeCell, nativeMonthRangeStep, nativeRangeDateCell, nativeRangeDateStep} from '../src/integrations/element-plus/date-picker.js'
// @ts-expect-error Generic Element Plus modules ship as framework-free JS.
import {installElOverlayOffset, pressElConfirmButton, readElConfirmBox, readElFormFacets} from '../src/integrations/element-plus/form.js'
// @ts-expect-error Generic Element Plus modules ship as framework-free JS.
import {findElSelectWrapperForClick, resolveElControlElement} from '../src/integrations/element-plus/select.js'
// @ts-expect-error Generic Element Plus modules ship as framework-free JS.
import {annotateElLineControls, collectElFormLineControls, collectVisibleElTableData} from '../src/integrations/element-plus/table.js'
// @ts-expect-error Generic Element Plus modules ship as framework-free JS.
import {annotateElDescriptions} from '../src/integrations/element-plus/descriptions.js'
// @ts-expect-error Generic Element Plus modules ship as framework-free JS.
import {annotateActiveDialogElTrees} from '../src/integrations/element-plus/tree.js'

const cleanups: Array<() => void> = []

afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup()
  document.body.innerHTML = ''
  document.head.innerHTML = ''
  if ('adoptedStyleSheets' in document && Array.isArray(document.adoptedStyleSheets)) {
    document.adoptedStyleSheets = []
  }
})

describe('generic Element Plus integration helpers', () => {
  it('resolves .el-select__wrapper and .el-tree-node hit areas without app state', () => {
    const wrapper = document.createElement('div')
    wrapper.className = 'el-select__wrapper'
    wrapper.innerHTML = '<input role="combobox" aria-haspopup="listbox" aria-controls="opt-1" aria-expanded="false">'
    const input = wrapper.firstElementChild as HTMLInputElement
    expect(resolveElControlElement(wrapper)).toBe(input)
    expect(findElSelectWrapperForClick(input, input, resolveElControlElement)).toBe(wrapper)

    const treeNode = document.createElement('div')
    treeNode.className = 'el-tree-node'
    treeNode.setAttribute('role', 'treeitem')
    treeNode.innerHTML = '<div class="el-tree-node__content"><span class="el-tree-node__label">Sales</span></div>'
    expect(resolveElControlElement(treeNode)).toBe(treeNode.firstElementChild)
  })

  it('detects .el-date-editor range inputs and calendar day cells', () => {
    const host = document.createElement('div')
    host.innerHTML = '<div class="el-date-editor el-date-editor--daterange">'
      + '<input class="el-range-input" aria-haspopup="dialog" aria-controls="cal-1"></div>'
      + '<div id="cal-1" role="dialog"><div class="el-date-range-picker__content">'
      + '<div class="el-date-range-picker__header"><span role="button">2026年</span><span role="button">3月</span></div>'
      + '<table role="grid"><tbody><tr><td class="available"><span class="el-date-table-cell__text">15</span></td></tr></tbody></table>'
      + '</div></div>'
    document.body.append(host)
    const input = host.querySelector('input')!
    expect(isElDateRangeInput(input)).toBe(true)
    expect(nativeRangeDateCell(input, '2026-03-15')).toBe(host.querySelector('td.available'))
    expect(nativeRangeDateCell(input, '2026-04-15')).toBeNull()
  })

  it('collects and annotates editable .el-form .el-table line controls with default or custom labels', () => {
    const host = document.createElement('div')
    host.innerHTML = '<form class="el-form"><div class="el-table">'
      + '<div class="el-table__header-wrapper"><table><thead><tr><th>Item</th><th>Qty</th></tr></thead></table></div>'
      + '<div class="el-table__body-wrapper"><table><tbody><tr>'
      + '<td><div class="el-select__wrapper"><span class="el-select__placeholder is-transparent">Pick</span>'
      + '<input role="combobox" aria-haspopup="listbox" aria-controls="items-1"></div></td>'
      + '<td><input id="qty-1" type="number"></td>'
      + '</tr></tbody></table></div></div></form>'
    document.body.append(host)
    for (const el of host.querySelectorAll('*')) {
      el.getBoundingClientRect = () => ({width: 100, height: 24, top: 10, bottom: 34, left: 0, right: 100, x: 0, y: 10, toJSON: () => ({})})
    }
    const lines = collectElFormLineControls(document)
    expect(lines).toHaveLength(2)
    const map = new Map([[1, {ref: host.querySelector('#qty-1')!}]])
    const html = annotateElLineControls(map, '[1]<input id=qty-1 />', lines)
    expect(html).toContain('title="Line 1 · Item"')
    expect(html).toContain('title="Line 1 · Qty"')
  })

  it('reads .el-form-item facets, .el-message-box confirms, and installs .el-overlay offset', () => {
    const host = document.createElement('div')
    host.innerHTML = '<form class="el-form"><div class="el-form-item"><label class="el-form-item__label">Status</label>'
      + '<input value="Active"></div></form>'
      + '<div class="el-message-box"><div class="el-message-box__title">Confirm</div>'
      + '<div class="el-message-box__message">Proceed?</div>'
      + '<div class="el-message-box__btns"><button>OK</button><button>Cancel</button></div></div>'
    document.body.append(host)
    for (const el of host.querySelectorAll('*')) {
      el.getBoundingClientRect = () => ({width: 100, height: 24, top: 10, bottom: 34, left: 0, right: 100, x: 0, y: 10, toJSON: () => ({})})
    }
    expect(readElFormFacets(document)).toEqual([{kind: 'filter', name: 'Status'}])
    expect(readElConfirmBox(document)).toEqual({open: true, title: 'Confirm', text: 'Proceed?', buttons: ['OK', 'Cancel']})
    let clicked = false
    host.querySelectorAll('button')[0]!.addEventListener('click', () => { clicked = true })
    expect(pressElConfirmButton('OK', document)).toBe(true)
    expect(clicked).toBe(true)

    const dispose = installElOverlayOffset(document)
    cleanups.push(dispose)
    const styleEl = document.getElementById('woa-el-overlay-offset')
    const sheetCss = Array.isArray(document.adoptedStyleSheets)
      ? document.adoptedStyleSheets.map(s => Array.from(s.cssRules).map(r => r.cssText).join('\n')).join('\n')
      : ''
    expect(styleEl?.textContent ?? sheetCss).toContain('.el-overlay, .el-drawer__wrapper')
  })

  it('annotates visible .el-descriptions pairs while prioritizing dialogs and filtering sensitive labels', () => {
    const host = document.createElement('div')
    host.innerHTML = '<div class="el-descriptions"><table><tbody>'
      + '<tr><th class="el-descriptions__label">Background</th><td class="el-descriptions__content">Ignored</td></tr>'
      + '</tbody></table></div>'
      + '<div role="dialog"><div class="el-descriptions">'
      + '<table><tbody>'
      + '<tr><th class="el-descriptions__label">Leave Type</th><td class="el-descriptions__content">Annual</td></tr>'
      + '<tr><th class="el-descriptions__label">apiSecret</th><td class="el-descriptions__content">s3cr3t</td></tr>'
      + '</tbody></table>'
      + '<div class="el-descriptions__cell"><span class="el-descriptions__label">Reason</span><span class="el-descriptions__content">Family trip</span></div>'
      + '</div></div>'
    document.body.append(host)
    for (const el of host.querySelectorAll('*')) {
      el.getBoundingClientRect = () => ({width: 120, height: 24, top: 10, bottom: 34, left: 0, right: 120, x: 0, y: 10, toJSON: () => ({})})
    }
    const out = annotateElDescriptions('[1]<button>Close />', document)
    expect(out).toContain('Leave Type=Annual')
    expect(out).toContain('Reason=Family trip')
    expect(out).not.toContain('Background=Ignored')
    expect(out).not.toContain('apiSecret')
    expect(out).not.toContain('s3cr3t')
  })

  it('pairs vertical .el-descriptions label and content rows across adjacent table rows', () => {
    const host = document.createElement('div')
    host.innerHTML = '<div class="el-descriptions"><table><tbody>'
      + '<tr><th class="el-descriptions__cell el-descriptions__label">申请人</th><th class="el-descriptions__cell el-descriptions__label">apiToken</th></tr>'
      + '<tr><td class="el-descriptions__cell el-descriptions__content">王五</td><td class="el-descriptions__cell el-descriptions__content">tok-secret</td></tr>'
      + '</tbody></table></div>'
    document.body.append(host)
    for (const el of host.querySelectorAll('*')) {
      el.getBoundingClientRect = () => ({width: 120, height: 24, top: 10, bottom: 34, left: 0, right: 120, x: 0, y: 10, toJSON: () => ({})})
    }
    const out = annotateElDescriptions('[1]<button>Close />', document)
    expect(out).toContain('申请人=王五')
    expect(out).not.toContain('apiToken')
    expect(out).not.toContain('tok-secret')
  })

  it('reads collapsed checked and half-checked nodes from active dialog ElTree instances and DOM fallback', () => {
    const host = document.createElement('div')
    host.innerHTML = '<div role="dialog"><header class="el-dialog__header"><span class="el-dialog__title">菜单权限</span></header>'
      + '<form class="el-form"><div class="el-form-item"><label class="el-form-item__label">菜单权限</label>'
      + '<div class="el-tree" id="vue-tree"></div>'
      + '<div class="el-tree" id="dom-tree"><div class="el-tree-node"><div class="el-tree-node__content">'
      + '<span class="el-checkbox is-checked"></span><span class="el-tree-node__label">用户管理</span></div></div>'
      + '<div class="el-tree-node"><div class="el-tree-node__content">'
      + '<span class="el-checkbox is-indeterminate"></span><span class="el-tree-node__label">系统管理</span></div></div></div>'
      + '</div></form></div>'
    document.body.append(host)
    for (const el of host.querySelectorAll('*')) {
      el.getBoundingClientRect = () => ({width: 200, height: 100, top: 10, bottom: 110, left: 0, right: 200, x: 0, y: 10, toJSON: () => ({})})
    }
    const vueTree = host.querySelector('#vue-tree')!
    const findComponent = (root: Element, name: string) => {
      if (root === vueTree && name === 'ElTree') {
        return {
          instance: {
            props: {showCheckbox: true, props: {label: 'name'}},
            exposed: {
              getCheckedNodes: () => [{id: 1, name: '角色查询'}, {id: 2, name: '角色新增'}],
              getHalfCheckedNodes: () => [{id: 10, name: '系统权限'}],
            },
          },
        }
      }
      return undefined
    }
    const out = annotateActiveDialogElTrees('[1]<button>OK />', document, {findComponent})
    expect(out).toContain('tree checked (2): 角色查询, 角色新增')
    expect(out).toContain('tree half-checked (1): 系统权限')
    expect(out).toContain('tree checked (1): 用户管理')
    expect(out).toContain('tree half-checked (1): 系统管理')
  })

  it('supports datetimerange headers, fast year stepping, monthrange cells, and ignores nested el-tables', () => {
    const host = document.createElement('div')
    host.innerHTML = '<div class="el-date-editor el-date-editor--datetimerange">'
      + '<input class="el-range-input" aria-haspopup="dialog" aria-controls="dt-1"></div>'
      + '<div id="dt-1" role="dialog">'
      + '<div class="el-date-range-picker__content is-left">'
      + '<div class="el-date-range-picker__header"><button type="button" class="d-arrow-left"></button><button type="button" class="arrow-left"></button>'
      + '<div class="el-date-range-picker__header-label">2026年 10月</div></div>'
      + '<table role="grid"><tbody><tr><td class="available"><span class="el-date-table-cell__text">15</span></td></tr></tbody></table></div>'
      + '<div class="el-date-range-picker__content is-right">'
      + '<div class="el-date-range-picker__header"><button type="button" class="d-arrow-right"></button><button type="button" class="arrow-right"></button>'
      + '<div class="el-date-range-picker__header-label">2026年 11月</div></div>'
      + '<table role="grid"><tbody><tr><td class="available"><span class="el-date-table-cell__text">20</span></td></tr></tbody></table></div>'
      + '</div>'
      + '<div class="el-date-editor el-date-editor--monthrange">'
      + '<input class="el-range-input" aria-haspopup="dialog" aria-controls="mr-1"></div>'
      + '<div id="mr-1" role="dialog">'
      + '<div class="el-date-range-picker__content is-left"><div class="el-date-range-picker__header"><button type="button" class="d-arrow-left"></button><div>2026 年</div></div>'
      + '<table class="el-month-table"><tbody><tr><td aria-label="1 月">Jan</td></tr></tbody></table></div>'
      + '<div class="el-date-range-picker__content is-right"><div class="el-date-range-picker__header"><button type="button" class="d-arrow-right"></button><div>2027 年</div></div>'
      + '<table class="el-month-table"><tbody><tr><td aria-label="12 月">Dec</td></tr></tbody></table></div>'
      + '</div>'
      + '<div class="el-table outer"><div class="el-table inner"></div></div>'
    document.body.append(host)
    for (const el of host.querySelectorAll('*')) {
      el.getBoundingClientRect = () => ({width: 100, height: 24, top: 10, bottom: 34, left: 0, right: 100, x: 0, y: 10, toJSON: () => ({})})
    }

    const dtInput = host.querySelector('.el-date-editor--datetimerange input') as HTMLInputElement
    expect(isElRangePickerInput(dtInput)).toBe(true)
    expect(isElMonthRangeInput(dtInput)).toBe(false)
    // Header label with combined "YYYY年 M月" in single element resolves both panels cleanly.
    const leftHeader = host.querySelector('#dt-1 .is-left .el-date-range-picker__header')!
    leftHeader.innerHTML = '<button type="button" class="d-arrow-left"></button><button type="button" class="arrow-left"></button>'
      + '<div class="el-date-range-picker__header-label">2026年</div><div class="el-date-range-picker__header-label">10月</div>'
    const rightHeader = host.querySelector('#dt-1 .is-right .el-date-range-picker__header')!
    rightHeader.innerHTML = '<button type="button" class="d-arrow-right"></button><button type="button" class="arrow-right"></button>'
      + '<div class="el-date-range-picker__header-label">2026年</div><div class="el-date-range-picker__header-label">11月</div>'
    expect(nativeRangeDateCell(dtInput, '2026-10-15 00:00:00')).toBe(host.querySelector('#dt-1 .is-left td.available'))
    expect(nativeRangeDateStep(dtInput, '2025-10-15 00:00:00')).toBe(host.querySelector('#dt-1 .is-left button.d-arrow-left'))
    expect(nativeRangeDateStep(dtInput, '2026-09-15 00:00:00')).toBe(host.querySelector('#dt-1 .is-left button.arrow-left'))
    expect(nativeRangeDateStep(dtInput, '2027-12-15 23:59:59')).toBe(host.querySelector('#dt-1 .is-right button.d-arrow-right'))

    const mrInput = host.querySelector('.el-date-editor--monthrange input') as HTMLInputElement
    expect(isElMonthRangeInput(mrInput)).toBe(true)
    expect(isElRangePickerInput(mrInput)).toBe(true)
    expect(nativeMonthRangeCell(mrInput, '2026-01')).toBe(host.querySelector('#mr-1 .is-left td[aria-label="1 月"]'))
    expect(nativeMonthRangeStep(mrInput, '2025-06')).toBe(host.querySelector('#mr-1 .is-left button.d-arrow-left'))
    expect(nativeMonthRangeStep(mrInput, '2028-06')).toBe(host.querySelector('#mr-1 .is-right button.d-arrow-right'))

    const outer = host.querySelector('.outer')!
    const inner = host.querySelector('.inner')!
    const tables = collectVisibleElTableData(document, (root: Element, name: string) =>
      name === 'ElTable' ? {instance: {props: {data: root === outer ? [{id: 1}] : [{sub: 2}]}}} : undefined,
      () => true)
    expect(tables).toEqual([[{id: 1}]])
    expect(tables).not.toContainEqual([{sub: 2}])
    void inner
  })
})
