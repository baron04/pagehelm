import {describe, expect, it} from 'vitest'
import {elementsFrom, hydrateFormFacts} from '../src/runtime/pageModel'
import {createRedactor} from '../src/runtime/redaction'
// @ts-expect-error Application module is served separately from the generic Runtime.
import {nativeControlElement} from '../src/integrations/yudao/page-facts.js'

describe('Element Plus accessible select input mapping', () => {
  it('resolves one native customer select in an open dialog form', () => {
    const dialog = document.createElement('section')
    dialog.setAttribute('role', 'dialog')
    dialog.id = 'sale-order-editor'
    dialog.innerHTML = '<form><div class="el-form-item"><label for="customer-choice">客户</label>'
      + '<div class="el-select__wrapper"><input id="customer-choice" role="combobox" '
      + 'aria-haspopup="listbox" aria-controls="customer-options" readonly></div></div></form>'
    document.body.append(dialog)
    try {
      const label = dialog.querySelector('label')!, input = dialog.querySelector('input')!
      expect(nativeControlElement(label)).toBe(input)
      const elements = elementsFrom('[0]<label>客户 />', 1, createRedactor())
      hydrateFormFacts(elements, {}, () => nativeControlElement(label), createRedactor())
      expect(elements[0]?.role).toBe('combobox')
      expect(elements[0]?.attrs).toMatchObject({field_label: '客户', form_scope: 'dialog:sale-order-editor',
        'aria-controls': 'customer-options'})
      input.disabled = true
      expect(nativeControlElement(label)).toBe(label)
      input.disabled = false
      const duplicate = input.cloneNode() as HTMLElement
      dialog.querySelector('.el-form-item')!.append(duplicate)
      expect(nativeControlElement(label)).toBe(label)
      duplicate.remove()
      const other = document.createElement('form')
      dialog.append(other)
      other.append(input)
      expect(nativeControlElement(label)).toBe(label)
      other.remove()
    } finally { dialog.remove() }
  })
  it('keeps the controller hit area while resolving its one owned accessible input', () => {
    const area = document.createElement('div')
    area.className = 'el-select__wrapper'
    area.innerHTML = '<input role="combobox" aria-haspopup="listbox" aria-controls="options" readonly>'
    expect(nativeControlElement(area)).toBe(area.firstElementChild)
    area.classList.add('is-disabled')
    expect(nativeControlElement(area)).toBe(area)
    area.classList.remove('is-disabled')
    area.append(area.firstElementChild!.cloneNode())
    expect(nativeControlElement(area)).toBe(area)
  })
  it('does not alias date controls, arbitrary containers or nested selects', () => {
    const area = document.createElement('div')
    area.innerHTML = '<input role="combobox" aria-haspopup="listbox" aria-controls="options">'
    expect(nativeControlElement(area)).toBe(area)
    area.className = 'el-select__wrapper'
    area.innerHTML = '<input role="combobox" aria-haspopup="dialog" aria-controls="calendar">'
    expect(nativeControlElement(area)).toBe(area)
    area.innerHTML = '<div class="el-select__wrapper"><input role="combobox" aria-haspopup="listbox" aria-controls="options"></div>'
    expect(nativeControlElement(area)).toBe(area)
  })
  it('resolves a radio hit area only to its one owned native option', () => {
    const label = document.createElement('label')
    label.className = 'el-radio'
    label.innerHTML = '<span><input type="radio" name="visibility" value="true"></span><span>Public</span>'
    expect(nativeControlElement(label)).toBe(label.querySelector('input'))
    label.append(label.firstElementChild!.cloneNode(true))
    expect(nativeControlElement(label)).toBe(label)
    label.innerHTML = '<label class="el-radio"><input type="radio"></label>'
    expect(nativeControlElement(label)).toBe(label)
  })
})

describe('mounted default Yudao menu facts', () => {
  it('uses current component identity and refuses overridden, stale or form-owned menus', async () => {
    // @ts-expect-error Application-owned JS module.
    const {readElementNavigation} = await import('../src/integrations/yudao/page-facts.js')
    const container = document.createElement('nav')
    container.innerHTML = '<li role="menuitem" class="el-sub-menu"><div role="menuitem" class="el-sub-menu__title">CRM</div></li>'
    document.body.append(container)
    const root = container.querySelector('li')!, title = root.firstElementChild!
    const appContext = {}
    const live = {isMounted:true,isUnmounted:false,isDeactivated:false,appContext}
    const owner = {...live,type:{name:'Menu'},props:{},subTree:{el:container},parent:null}
    const instance = {...live,type:{name:'ElSubMenu'},props:{index:'/crm'},subTree:{el:root},parent:owner}
    container.id = 'app'
    Object.assign(container,{__vue_app__:{_container:{_vnode:{component:instance}}}})
    expect(readElementNavigation(root)).toEqual({kind:'branch',path:'/crm',expanded:false})
    expect(nativeControlElement(root)).toBe(title)
    instance.props.index = '/erp'
    expect(readElementNavigation(title)).toEqual({kind:'branch',path:'/erp',expanded:false})
    Object.assign(owner.props,{menuSelect:() => undefined})
    expect(readElementNavigation(root)).toBeUndefined()
    delete (owner.props as {menuSelect?:unknown}).menuSelect
    instance.isDeactivated = true
    expect(readElementNavigation(root)).toBeUndefined()
    instance.isDeactivated = false
    container.setAttribute('role','dialog')
    expect(readElementNavigation(root)).toBeUndefined()
    container.remove()
  })
})
