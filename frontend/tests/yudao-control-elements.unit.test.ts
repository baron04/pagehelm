import {describe, expect, it} from 'vitest'
// @ts-expect-error Application module is served separately from the generic Runtime.
import {nativeControlElement} from '../src/integrations/yudao/page-facts.js'

describe('Element Plus accessible select input mapping', () => {
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
