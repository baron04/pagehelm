import {describe, expect, it, vi} from 'vitest'
import {
  listboxCombobox, ownedListbox, ownedTree, selectedTreeOption, treeItemChoices,
  treeItemPath, treeSelectionMatches,
} from '../src/runtime/combobox'
import {Page, type PageControllerApi} from '../src/runtime/page'
import {hydrateFormFacts} from '../src/runtime/pageModel'
import {createRedactor} from '../src/runtime/redaction'
import type {ElementRef} from '../src/runtime/types'

function mockController(): PageControllerApi {
  return {
    selectorMap: {},
    getBrowserState: vi.fn(),
    clickElement: vi.fn(),
    inputText: vi.fn(),
    selectOption: vi.fn(),
    scroll: vi.fn(),
    dispose: vi.fn(),
  }
}

describe('tree-backed combobox helpers (ElTreeSelect / ARIA tree)', () => {
  it('claims Element Plus ul[role=listbox] > div[role=tree] as ownedTree before ownedListbox', () => {
    document.body.innerHTML = `
      <div class="el-select__wrapper">
        <input id="dept-combo" role="combobox" aria-haspopup="listbox" aria-controls="dept-pop" />
      </div>
      <ul id="dept-pop" role="listbox" class="el-select-dropdown__list">
        <div role="tree" class="el-tree">
          <div role="treeitem" class="el-tree-node" aria-expanded="true">
            <div class="el-tree-node__content">
              <i class="el-icon el-tree-node__expand-icon expanded"></i>
              <li role="option" class="el-select-dropdown__item"><span>总公司</span></li>
            </div>
            <div role="group" class="el-tree-node__children">
              <div role="treeitem" class="el-tree-node is-current" aria-selected="true">
                <div class="el-tree-node__content">
                  <i class="el-icon el-tree-node__expand-icon is-leaf"></i>
                  <li role="option" class="el-select-dropdown__item is-selected" aria-selected="true"><span>研发部</span></li>
                </div>
              </div>
              <div role="treeitem" class="el-tree-node">
                <div class="el-tree-node__content">
                  <i class="el-icon el-tree-node__expand-icon is-leaf"></i>
                  <li role="option" class="el-select-dropdown__item"><span>财务部</span></li>
                </div>
              </div>
            </div>
          </div>
        </div>
      </ul>
    `
    const input = document.getElementById('dept-combo') as HTMLInputElement
    expect(listboxCombobox(input)).toBe(true)
    expect(ownedTree(input)).not.toBeNull()
    expect(ownedListbox(input)).toBeNull()
    const tree = ownedTree(input)!
    const choices = treeItemChoices(tree)
    expect(choices.map(c => c.path)).toEqual(['总公司', '总公司 / 研发部', '总公司 / 财务部'])
    expect(treeItemPath(tree, choices[1]!.item)).toBe('总公司 / 研发部')
    expect(selectedTreeOption(input)).toBe('研发部')
    expect(treeSelectionMatches(input, '研发部')).toBe(true)
    expect(treeSelectionMatches(input, '总公司 / 研发部')).toBe(true)
    expect(treeSelectionMatches(input, '财务部')).toBe(false)
  })

  it('requires exact path on duplicate leaf names and rejects ambiguous bare leaf in treeSelectionMatches', () => {
    document.body.innerHTML = `
      <input id="cat-combo" role="combobox" aria-haspopup="listbox" aria-controls="cat-pop" />
      <ul id="cat-pop" role="listbox">
        <div role="tree">
          <div role="treeitem">
            <div class="el-tree-node__content"><span class="el-tree-node__label">华东区</span></div>
            <div role="group">
              <div role="treeitem" class="el-tree-node is-current">
                <div class="el-tree-node__content"><span class="el-tree-node__label">综合部</span></div>
              </div>
            </div>
          </div>
          <div role="treeitem">
            <div class="el-tree-node__content"><span class="el-tree-node__label">华南区</span></div>
            <div role="group">
              <div role="treeitem" class="el-tree-node">
                <div class="el-tree-node__content"><span class="el-tree-node__label">综合部</span></div>
              </div>
            </div>
          </div>
        </div>
      </ul>
    `
    const input = document.getElementById('cat-combo') as HTMLInputElement
    expect(selectedTreeOption(input)).toBe('华东区 / 综合部')
    expect(treeSelectionMatches(input, '华东区 / 综合部')).toBe(true)
    expect(treeSelectionMatches(input, '华南区 / 综合部')).toBe(false)
    // Bare duplicate leaf "综合部" must NOT falsely report a match!
    expect(treeSelectionMatches(input, '综合部')).toBe(false)
  })

  it('Page.select expands collapsed check-strictly render-after-expand=false ancestors via expand-icon without selecting parent, then verifies form fields', async () => {
    const form = document.createElement('form')
    form.id = 'user-dept-form'
    form.innerHTML = `
      <label>归属部门<input id="dept-input" role="combobox" readonly aria-haspopup="listbox"
        aria-controls="dept-popup" aria-expanded="false"></label>
      <ul id="dept-popup" role="listbox" hidden>
        <div role="tree">
          <div id="node-root" role="treeitem" class="el-tree-node" aria-expanded="false">
            <div id="content-root" class="el-tree-node__content">
              <i id="caret-root" class="el-icon el-tree-node__expand-icon"></i>
              <li id="opt-root" role="option" class="el-select-dropdown__item"><span>总公司</span></li>
            </div>
            <div id="children-root" role="group" class="el-tree-node__children" style="display:none">
              <div id="node-rd" role="treeitem" class="el-tree-node">
                <div id="content-rd" class="el-tree-node__content">
                  <i class="el-icon el-tree-node__expand-icon is-leaf"></i>
                  <li id="opt-rd" role="option" class="el-select-dropdown__item"><span>研发部</span></li>
                </div>
              </div>
            </div>
          </div>
        </div>
      </ul>
    `
    document.body.append(form)
    try {
      const input = document.getElementById('dept-input') as HTMLInputElement
      const popup = document.getElementById('dept-popup') as HTMLElement
      const nodeRoot = document.getElementById('node-root') as HTMLElement
      const caretRoot = document.getElementById('caret-root') as HTMLElement
      const contentRoot = document.getElementById('content-root') as HTMLElement
      const childrenRoot = document.getElementById('children-root') as HTMLElement
      const nodeRd = document.getElementById('node-rd') as HTMLElement
      const optRd = document.getElementById('opt-rd') as HTMLElement

      let parentSelectedByMistake = false
      // Simulate check-strictly: clicking contentRoot selects parent instead of expanding!
      contentRoot.addEventListener('click', () => {
        parentSelectedByMistake = true
        nodeRoot.classList.add('is-current')
      })
      // Clicking caretRoot expands without selecting parent (@click.stop)
      caretRoot.addEventListener('click', e => {
        e.stopPropagation()
        nodeRoot.setAttribute('aria-expanded', 'true')
        childrenRoot.style.display = 'block'
      })

      for (const el of [input, popup, popup.querySelector('[role="tree"]')!, nodeRoot, caretRoot, contentRoot, nodeRd, optRd]) {
        ;(el as HTMLElement).getBoundingClientRect = () => ({
          width: el === nodeRd || el === optRd ? (childrenRoot.style.display === 'none' ? 0 : 120) : 120,
          height: el === nodeRd || el === optRd ? (childrenRoot.style.display === 'none' ? 0 : 28) : 28,
          top: 10, bottom: 38, left: 10, right: 130, x: 10, y: 10, toJSON: () => ({}),
        } as DOMRect)
      }

      const api = mockController()
      api.getBrowserState = vi.fn(async () => ({
        url: location.href,
        title: '',
        content: childrenRoot.style.display === 'none'
          ? '[1]<input />\n[2]<li role=option>总公司 />'
          : '[1]<input />\n[2]<li role=option>总公司 />\n[3]<li role=option>研发部 />',
      }))
      api.clickElement = vi.fn(async idx => {
        if (idx === 1) {
          popup.hidden = !popup.hidden
          input.setAttribute('aria-expanded', String(!popup.hidden))
        } else if (idx === 3) {
          nodeRd.classList.add('is-current')
          optRd.setAttribute('aria-selected', 'true')
          input.value = '研发部'
          popup.hidden = true
          input.setAttribute('aria-expanded', 'false')
        }
        return {success: true}
      })

      const lookup = (_map: unknown, idx: number) =>
        idx === 1 ? input : idx === 2 ? document.getElementById('opt-root') : idx === 3 ? optRd : null
      const page = new Page(api, lookup, createRedactor())

      await page.select('e1', '103', '总公司 / 研发部')
      expect(parentSelectedByMistake).toBe(false)
      expect(nodeRoot.getAttribute('aria-expanded')).toBe('true')
      expect(nodeRd.classList.contains('is-current')).toBe(true)

      const observed: ElementRef[] = [{
        ref: 'e1', role: 'combobox', name: '归属部门', tag: 'input', epoch: 1,
        attrs: {field_label: '归属部门', form_scope: 'form:user-dept-form'},
      }]
      hydrateFormFacts(observed, {}, () => input, createRedactor())
      expect(observed[0]!.attrs.selected_option).toBe('研发部')
      expect(observed[0]!.attrs.selected_option_path).toBe('总公司 / 研发部')

      expect(page.verifyFormFields([{
        ref: 'e1', field: 'deptId', label: '归属部门', binding: 'label',
        scope: 'form:user-dept-form', control: 'combobox', value: '103', option_label: '总公司 / 研发部',
      }], observed)).toBe(true)
      expect(page.verifyFormFields([{
        ref: 'e1', field: 'deptId', label: '归属部门', binding: 'label',
        scope: 'form:user-dept-form', control: 'combobox', value: '103', option_label: '研发部',
      }], observed)).toBe(true)
    } finally {
      form.remove()
    }
  })

  it('supports pure framework-free ARIA tree comboboxes without UI-library classes', () => {
    document.body.innerHTML = `
      <input id="plain-combo" role="combobox" aria-haspopup="tree" aria-controls="plain-tree" />
      <ul id="plain-tree" role="tree">
        <li role="treeitem" aria-expanded="true">
          <button type="button" data-tree-expand aria-hidden="true">▾</button>
          <span>总部</span>
          <ul role="group">
            <li role="treeitem" aria-selected="true">平台组</li>
            <li role="treeitem" aria-disabled="true">冻结组</li>
          </ul>
        </li>
      </ul>
    `
    const input = document.getElementById('plain-combo') as HTMLInputElement
    const tree = ownedTree(input)
    expect(tree).not.toBeNull()
    expect(treeItemChoices(tree!).map(c => c.path)).toEqual(['总部', '总部 / 平台组', '总部 / 冻结组'])
    expect(selectedTreeOption(input)).toBe('平台组')
    expect(treeSelectionMatches(input, '平台组')).toBe(true)
    expect(treeSelectionMatches(input, '总部 / 平台组')).toBe(true)
  })

  it('Page.select clicks the unique text descendant when a tree row indexes both expand icon and label span', async () => {
    const form = document.createElement('form')
    form.innerHTML = `
      <label>归属部门<input id="dept-multi-input" role="combobox" readonly aria-haspopup="tree"
        aria-controls="dept-multi-pop" aria-expanded="true"></label>
      <div id="dept-multi-pop" role="tree">
        <div role="treeitem">
          <div id="row-wrap">
            <span id="icon-child" aria-expanded="false"></span>
            <span id="label-outer"><span id="label-inner">研发部</span></span>
          </div>
        </div>
      </div>
    `
    document.body.append(form)
    try {
      const input = document.getElementById('dept-multi-input') as HTMLInputElement
      const pop = document.getElementById('dept-multi-pop')!
      const rowWrap = document.getElementById('row-wrap')!
      const iconChild = document.getElementById('icon-child')!
      const labelOuter = document.getElementById('label-outer')!
      const labelInner = document.getElementById('label-inner')!
      const treeItem = pop.querySelector('[role="treeitem"]') as HTMLElement

      for (const el of [input, pop, treeItem, rowWrap, iconChild, labelOuter, labelInner]) {
        ;(el as HTMLElement).getBoundingClientRect = () => ({
          width: 100, height: 24, top: 10, bottom: 34, left: 10, right: 110, x: 10, y: 10, toJSON: () => ({}),
        } as DOMRect)
      }

      const api = mockController()
      api.getBrowserState = vi.fn(async () => ({
        url: location.href,
        title: '',
        content: '[1]<input />\n[2]<span /> \n[3]<span>研发部 /> \n[4]<span>研发部 />',
      }))
      api.clickElement = vi.fn(async idx => {
        if (idx === 4) {
          treeItem.setAttribute('aria-selected', 'true')
          input.value = '研发部'
          input.setAttribute('aria-expanded', 'false')
        }
        return {success: true}
      })

      const map: Record<number, Element> = {1: input, 2: iconChild, 3: labelOuter, 4: labelInner}
      const lookup = (_m: unknown, idx: number) => map[idx] ?? null
      const page = new Page(api, lookup, createRedactor())

      await page.select('e1', '103', '研发部')
      expect(api.clickElement).toHaveBeenCalledWith(4)
    } finally {
      form.remove()
    }
  })
})
