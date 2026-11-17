/** Application-owned wrapper around the pinned generic controller, never a new harness. */
import {PageController as GenericPageController, getElementByIndex as controllerElement,
  scrollIntoViewIfNeeded} from './page-controller.js'
import {lineControls, nativeControlElement, nativeRangeDateCell, nativeRangeDateStep,
  withLineControls, withTreeQueryControls, withUserStatusControls, withObservedRowColumns} from './page-facts.js'
import {boundClick, boundInput, boundRecord, readElementPageControl as ordinaryControl} from './page-facts.js'

const bindings = new URL(import.meta.url).searchParams.get('bindings') === '1'

const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
let pressed = null
const notify = callback => { try { callback() } catch { /* Diagnostics never stop a page action. */ } }

/** The generic controller's click on an element: the same events to the same targets in the
 * same order, without the 0.4 s it first spends moving a pointer. That pointer is drawn by
 * the controller's mask, which the runtime keeps off, so the pause shows nobody anything.
 * The pause after the click, in which the page reacts before it is observed, is kept. */
export async function clickElement(element, invoked = () => {}) {
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
  const avoidInnerInput = element instanceof HTMLElement && element.classList.contains('el-select__wrapper') &&
    hit instanceof HTMLInputElement && hit.classList.contains('el-select__input')
  const target = hit instanceof HTMLElement && element.contains(hit) && !avoidInnerInput ? hit : element
  const pointer = {bubbles: true, cancelable: true, clientX: x, clientY: y, pointerType: 'mouse'}
  const mouse = {bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0}
  notify(invoked)
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
export async function inputTextElement(element, text, invoked = () => {}) {
  await clickElement(element, invoked)
  notify(invoked)
  Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), 'value').set.call(element, text)
  element.dispatchEvent(new Event('input', {bubbles: true}))
  await pause(100)
}

export class PageController extends GenericPageController {
  bindingTrace = {trace_id: null, events: []}
  executionTrace(operation) {
    let trace = null
    try {
      const trace_id = Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('')
      trace = {trace_id, events: []}
    } catch { /* Unavailable diagnostics leave the ordinary action intact. */ }
    notify(() => { this.bindingTrace = trace ?? {trace_id:null, events:[]} })
    return {
      record: path => {
        if (!trace) return
        try { trace.events.push({sequence: trace.events.length + 1, operation, path}) }
        catch { trace = null }
      },
      finish: result => {
        if (!trace) return result
        try { return {...result, execution_trace: globalThis.structuredClone(trace)} }
        catch { return result }
      },
    }
  }
  async updateTree() {
    await super.updateTree()
    // The generic controller has already emitted afterUpdate. Sample application
    // extensions under the same synchronous lifecycle so our panel's hit targets
    // remain excluded here too, and restore them even if an extension fails.
    this.dispatchEvent(new Event('beforeUpdate'))
    try {
      const next = withObservedRowColumns(withUserStatusControls(this.selectorMap,
        withTreeQueryControls(this.selectorMap,
          withLineControls(this.selectorMap, this.simplifiedHTML, lineControls()))), document, this.selectorMap)
      this.simplifiedHTML = next
      for (const line of next.split('\n')) {
        const mark = /^\s*\*?\[(\d+)\]</.exec(line)
        if (mark && !this.elementTextMap.has(Number(mark[1]))) this.elementTextMap.set(Number(mark[1]), line.trim())
      }
      return this.simplifiedHTML
    } finally {
      this.dispatchEvent(new Event('afterUpdate'))
    }
  }


  async inputText(index, text) {
    const {record, finish} = this.executionTrace('fill')
    const element = controllerElement(this.selectorMap, index)
    if (bindings && await boundInput(element, text, () => record('binding'))) {
      return finish({success: true, message: 'component input binding'})
    }
    const range = element instanceof HTMLInputElement && element.matches('.el-date-editor--daterange > .el-range-input')
    const result = await this.typed(index, element, text, () => record('ordinary'))
    if (!range || result?.success !== true) return finish(result)
    let cell = nativeRangeDateCell(element, text)
    for (let turns = 0; !cell && turns < 48; turns++) {
      const step = nativeRangeDateStep(element, text)
      if (!step) break
      await clickElement(step, () => record('ordinary'))
      cell = nativeRangeDateCell(element, text)
    }
    if (!cell || !cell.isConnected || !cell.getBoundingClientRect().width) {
      throw new Error('the exact native calendar day is unavailable')
    }
    // Stay inside the owned popup before the generic controller's pre-click blur.
    // Native calendar selection preserves the application's default endpoint times.
    cell.focus({preventScroll: true})
    await clickElement(cell, () => record('ordinary'))
    return finish(result)
  }

  /** The generic `inputText`; what is neither an input nor a text area is left to it. */
  async typed(index, element, text, invoked = () => {}) {
    if (!['INPUT', 'TEXTAREA'].includes(element.tagName)) {
      notify(invoked)
      return super.inputText(index, text)
    }
    try {
      await inputTextElement(element, text, invoked)
      return {success: true, message: `Input text (${text}) into element (${this.elementTextMap.get(index) ?? index}).`}
    } catch (error) {
      return {success: false, message: `Failed to input text: ${error}`}
    }
  }

  async clickElement(index) {
    const {record, finish} = this.executionTrace('click')
    const element = controllerElement(this.selectorMap, index)
    const target = nativeControlElement(element)
    if (bindings && await boundClick(target, () => record('binding'))) {
      return finish({success: true, message: 'component click binding'})
    }
    if (target instanceof HTMLInputElement &&
        ((element instanceof HTMLLabelElement && element !== target) ||
         (element === target && target.getAttribute('role') === 'combobox' &&
          target.getAttribute('aria-expanded') !== 'true'))) {
      // Label focus or inner combobox input click (@click.stop in Element Plus) does not
      // open Element Plus's select. Click its exact owned wrapper through the controller.
      const wrapper = target.closest('.el-select__wrapper')
      if (wrapper && nativeControlElement(wrapper) === target) {
        await clickElement(wrapper, () => record('ordinary'))
        return finish({success: true})
      }
    }
    // The indexed submenu container's handler lives on its owned title.
    if (element !== target && element.matches('li.el-sub-menu')) {
      record('ordinary')
      target.click()
      return finish({success: true})
    }
    try {
      await clickElement(element, () => record('ordinary'))
      return finish({success: true, message: `Clicked element (${this.elementTextMap.get(index) ?? index}).`})
    } catch (error) {
      return finish({success: false, message: `Failed to click element: ${error}`})
    }
  }
}
export function getElementByIndex(selectorMap, index) {
  return nativeControlElement(controllerElement(selectorMap, index))
}
export function readElementPageControl(element) {
  const record = bindings && boundRecord(element)
  return record ? {kind: 'record', ...record} : ordinaryControl(element)
}
export {readPageBlocker, readPageRecordRoute, readPageRoute, readPageQuery, readPageRead,
  readElementNavigation, readElementFormModel, restoreElementFormModel, readElementRowRecord, openPage} from './page-facts.js'
export {acceptReadSource, readConfirmBox, pressConfirmButton, readSubmitBusy} from './form-surface.js'
