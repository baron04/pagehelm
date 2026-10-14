/** Application-owned wrapper around the pinned generic controller, never a new harness. */
import {PageController as GenericPageController, getElementByIndex as controllerElement,
  scrollIntoViewIfNeeded} from './page-controller.js'
import {lineControls, nativeControlElement, nativeRangeDateCell, withLineControls} from './page-facts.js'

const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
let pressed = null

/** The generic controller's click on an element: the same events to the same targets in the
 * same order, without the 0.4 s it first spends moving a pointer. That pointer is drawn by
 * the controller's mask, which the runtime keeps off, so the pause shows nobody anything.
 * The pause after the click, in which the page reacts before it is observed, is kept. */
export async function clickElement(element) {
  if (pressed) {
    pressed.dispatchEvent(new PointerEvent('pointerout', {bubbles: true}))
    pressed.dispatchEvent(new PointerEvent('pointerleave', {bubbles: false}))
    pressed.dispatchEvent(new MouseEvent('mouseout', {bubbles: true}))
    pressed.dispatchEvent(new MouseEvent('mouseleave', {bubbles: false}))
    pressed.blur()
  }
  pressed = element
  await scrollIntoViewIfNeeded(element)
  const frame = element.ownerDocument.defaultView?.frameElement
  if (frame) await scrollIntoViewIfNeeded(frame)
  const rect = element.getBoundingClientRect()
  const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2
  const hit = element.ownerDocument.elementFromPoint(x, y)
  const target = hit instanceof HTMLElement && element.contains(hit) ? hit : element
  const pointer = {bubbles: true, cancelable: true, clientX: x, clientY: y, pointerType: 'mouse'}
  const mouse = {bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0}
  target.dispatchEvent(new PointerEvent('pointerover', pointer))
  target.dispatchEvent(new PointerEvent('pointerenter', {...pointer, bubbles: false}))
  target.dispatchEvent(new MouseEvent('mouseover', mouse))
  target.dispatchEvent(new MouseEvent('mouseenter', {...mouse, bubbles: false}))
  target.dispatchEvent(new PointerEvent('pointerdown', pointer))
  target.dispatchEvent(new MouseEvent('mousedown', mouse))
  element.focus({preventScroll: true})
  target.dispatchEvent(new PointerEvent('pointerup', pointer))
  target.dispatchEvent(new MouseEvent('mouseup', mouse))
  target.click()
  await pause(200)
}

/** The generic controller's typing into an input or a text area, over the click above. */
export async function inputTextElement(element, text) {
  await clickElement(element)
  Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), 'value').set.call(element, text)
  element.dispatchEvent(new Event('input', {bubbles: true}))
  await pause(100)
}

export class PageController extends GenericPageController {
  async updateTree() {
    const html = await super.updateTree()
    const next = withLineControls(this.selectorMap, this.simplifiedHTML, lineControls())
    if (next === this.simplifiedHTML) return html
    this.simplifiedHTML = next
    for (const line of next.split('\n')) {
      const mark = /^\s*\*?\[(\d+)\]</.exec(line)
      if (mark && !this.elementTextMap.has(Number(mark[1]))) this.elementTextMap.set(Number(mark[1]), line.trim())
    }
    return next
  }


  async inputText(index, text) {
    const element = controllerElement(this.selectorMap, index)
    const range = element instanceof HTMLInputElement && element.matches('.el-date-editor--daterange > .el-range-input')
    const result = await this.typed(index, element, text)
    if (!range || result?.success !== true) return result
    const cell = nativeRangeDateCell(element, text)
    if (!cell || !cell.isConnected || !cell.getBoundingClientRect().width) {
      throw new Error('the exact native calendar day is unavailable')
    }
    // Stay inside the owned popup before the generic controller's pre-click blur.
    // Native calendar selection preserves the application's default endpoint times.
    cell.focus({preventScroll: true})
    await clickElement(cell)
    return result
  }

  /** The generic `inputText`; what is neither an input nor a text area is left to it. */
  async typed(index, element, text) {
    if (!['INPUT', 'TEXTAREA'].includes(element.tagName)) return super.inputText(index, text)
    try {
      await inputTextElement(element, text)
      return {success: true, message: `Input text (${text}) into element (${this.elementTextMap.get(index) ?? index}).`}
    } catch (error) {
      return {success: false, message: `Failed to input text: ${error}`}
    }
  }

  async clickElement(index) {
    const element = controllerElement(this.selectorMap, index)
    const target = nativeControlElement(element)
    if (element instanceof HTMLLabelElement && target instanceof HTMLInputElement && element !== target) {
      // Label focus does not open Element Plus's query select. Click its exact
      // owned wrapper through the existing controller's fixed DOM operation.
      const wrapper = target.closest('.el-select__wrapper')
      if (wrapper && nativeControlElement(wrapper) === target) {
        await clickElement(wrapper)
        return {success: true}
      }
    }
    // The indexed submenu container's handler lives on its owned title.
    if (element !== target && element.matches('li.el-sub-menu')) {
      target.click()
      return {success: true}
    }
    try {
      await clickElement(element)
      return {success: true, message: `Clicked element (${this.elementTextMap.get(index) ?? index}).`}
    } catch (error) {
      return {success: false, message: `Failed to click element: ${error}`}
    }
  }
}
export function getElementByIndex(selectorMap, index) {
  return nativeControlElement(controllerElement(selectorMap, index))
}
export {readPageBlocker, readPageRecordRoute, readPageRoute, readPageQuery, readPageRead,
  readElementNavigation, readElementPageControl, openPage} from './page-facts.js'
