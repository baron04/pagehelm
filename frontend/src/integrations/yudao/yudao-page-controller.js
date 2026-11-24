/** Application-owned wrapper around the pinned generic controller, never a new harness. */
import {PageController as GenericPageController, getElementByIndex as controllerElement,
  scrollIntoViewIfNeeded} from './page-controller.js'
import {findElSelectWrapperForClick, isElSubMenuContainer,
  shouldAvoidSelectInnerInput} from '../element-plus/select.js'
import {
  isElMonthRangeInput,
  isElRangePickerInput,
  isElRangePopupOpen,
  lineControls,
  nativeControlElement,
  nativeMonthRangeCell,
  nativeMonthRangeStep,
  nativeRangeDateCell,
  nativeRangeDateStep,
  rangePopupDialog,
  withLineControls,
  withTreeQueryControls,
  withUserStatusControls,
  withObservedRowColumns,
  withObservedSurfaceDetails,
} from './page-facts.js'
import {boundClick, boundInput, boundRecord, readElementPageControl as ordinaryControl} from './page-facts.js'
import {installOverlayOffset} from './form-surface.js'

const bindings = new URL(import.meta.url).searchParams.get('bindings') === '1'

const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
let pressed = null
const notify = callback => { try { callback() } catch { /* Diagnostics never stop a page action. */ } }

async function dispatchPointerClick(element, focusTarget, pauseMs, invoked) {
  await scrollIntoViewIfNeeded(element)
  const ownerWin = element.ownerDocument.defaultView
  const frame = ownerWin?.frameElement
  if (frame) await scrollIntoViewIfNeeded(frame)
  const rect = element.getBoundingClientRect()
  const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2
  const hit = element.ownerDocument.elementFromPoint?.(x, y)
  const ElementCtor = ownerWin?.HTMLElement ?? HTMLElement
  const MouseCtor = ownerWin?.MouseEvent ?? MouseEvent
  const PointerCtor = ownerWin?.PointerEvent ?? (typeof PointerEvent === 'function' ? PointerEvent : MouseCtor)
  const avoidInnerInput = shouldAvoidSelectInnerInput(element, hit)
  const target = hit instanceof ElementCtor && element.contains(hit) && !avoidInnerInput ? hit : element
  const pointer = {bubbles: true, cancelable: true, clientX: x, clientY: y, pointerType: 'mouse'}
  const mouse = {bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0}
  notify(invoked)
  target.dispatchEvent(new PointerCtor('pointerover', pointer))
  target.dispatchEvent(new PointerCtor('pointerenter', {...pointer, bubbles: false}))
  target.dispatchEvent(new MouseCtor('mouseover', mouse))
  target.dispatchEvent(new MouseCtor('mouseenter', {...mouse, bubbles: false}))
  target.dispatchEvent(new PointerCtor('pointerdown', pointer))
  target.dispatchEvent(new MouseCtor('mousedown', mouse))
  focusTarget.focus({preventScroll: true})
  target.dispatchEvent(new PointerCtor('pointerup', pointer))
  target.dispatchEvent(new MouseCtor('mouseup', mouse))
  target.click()
  if (focusTarget !== element) focusTarget.focus({preventScroll: true})
  await pause(pauseMs)
}

/** The generic controller's click on an element: the same events to the same targets in the
 * same order, without the 0.4 s it first spends moving a pointer. That pointer is drawn by
 * the controller's mask, which the runtime keeps off, so the pause shows nobody anything.
 * The pause after the click, in which the page reacts before it is observed, is kept. */
export async function clickElement(element, invoked = () => {}) {
  if (pressed) {
    const prevWin = pressed.ownerDocument.defaultView
    const MouseCtor = prevWin?.MouseEvent ?? MouseEvent
    const PointerCtor = prevWin?.PointerEvent ?? (typeof PointerEvent === 'function' ? PointerEvent : MouseCtor)
    pressed.dispatchEvent(new PointerCtor('pointerout', {bubbles: true}))
    pressed.dispatchEvent(new PointerCtor('pointerleave', {bubbles: false}))
    pressed.dispatchEvent(new MouseCtor('mouseout', {bubbles: true}))
    pressed.dispatchEvent(new MouseCtor('mouseleave', {bubbles: false}))
    pressed.blur()
  }
  pressed = element
  await dispatchPointerClick(element, element, 200, invoked)
}

/** Click inside an open Element Plus range popup without `pressed.blur()`, keeping focus
 * on `ownerInput` so `useFocusController` never dismisses range-selection state. */
async function clickRangePopupElement(target, ownerInput, invoked = () => {}) {
  pressed = ownerInput
  await dispatchPointerClick(target, ownerInput, 150, invoked)
}

/** The generic controller's typing into an input or a text area, over the click above. */
export async function inputTextElement(element, text, invoked = () => {}) {
  await clickElement(element, invoked)
  notify(invoked)
  const ownerWin = element.ownerDocument.defaultView
  const EventCtor = ownerWin?.Event ?? Event
  Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), 'value').set.call(element, text)
  element.dispatchEvent(new EventCtor('input', {bubbles: true}))
  await pause(100)
}

export class PageController extends GenericPageController {
  bindingTrace = {trace_id: null, events: []}
  #cleanupOverlayOffset = null

  constructor(options) {
    super(options)
    if (typeof document !== 'undefined') {
      this.#cleanupOverlayOffset = installOverlayOffset(document)
    }
  }

  dispose() {
    try { this.#cleanupOverlayOffset?.() } finally { this.#cleanupOverlayOffset = null }
    super.dispose()
  }
  executionTrace(operation) {
    let trace = null
    try {
      const trace_id = Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('')
      trace = {trace_id, events: []}
    } catch { /* Unavailable diagnostics leave the ordinary action intact. */ }
    notify(() => { this.bindingTrace = trace ?? {trace_id: null, events: []} })
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
      const next = withObservedSurfaceDetails(
        withObservedRowColumns(withUserStatusControls(this.selectorMap,
          withTreeQueryControls(this.selectorMap,
            withLineControls(this.selectorMap, this.simplifiedHTML, lineControls()))), document, this.selectorMap),
        document,
      )
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
    if (isElRangePickerInput(element)) {
      const monthRange = isElMonthRangeInput(element)
      const root = element.closest('.el-date-editor')
      const inputs = root ? [...root.querySelectorAll(':scope > .el-range-input')] : []
      const end = inputs.indexOf(element)
      if (!isElRangePopupOpen(element)) {
        await clickElement(element, () => record('ordinary'))
      }
      const findCell = () => monthRange ? nativeMonthRangeCell(element, text) : nativeRangeDateCell(element, text)
      const findStep = () => monthRange ? nativeMonthRangeStep(element, text) : nativeRangeDateStep(element, text)
      let cell = findCell()
      for (let turns = 0; !cell && turns < 48; turns++) {
        const step = findStep()
        if (!step) break
        await clickRangePopupElement(step, element, () => record('ordinary'))
        cell = findCell()
      }
      if (!cell || !cell.isConnected || !cell.getBoundingClientRect().width) {
        return finish({success: false, message: 'Failed to input text: the exact native calendar day is unavailable'})
      }
      await clickRangePopupElement(cell, element, () => record('ordinary'))
      if (root?.classList.contains('el-date-editor--datetimerange') && end >= 0) {
        const popup = rangePopupDialog(element)
        const ownerWin = element.ownerDocument.defaultView
        const InputCtor = ownerWin?.HTMLInputElement ?? HTMLInputElement
        const ButtonCtor = ownerWin?.HTMLButtonElement ?? HTMLButtonElement
        const EventCtor = ownerWin?.Event ?? Event
        const timeMatch = / (\d{2}:\d{2}:\d{2})$/.exec(text || '')
        const desiredTime = timeMatch ? timeMatch[1] : (end === 1 ? '23:59:59' : '')
        if (popup && desiredTime) {
          const wraps = [...popup.querySelectorAll('.el-date-range-picker__time-header .el-date-range-picker__editors-wrap')]
          const timeInput = wraps[end]?.querySelectorAll('input')?.[1]
          if (timeInput instanceof InputCtor && !timeInput.readOnly && !timeInput.disabled) {
            if (timeMatch || (end === 1 && timeInput.value === '00:00:00')) {
              Object.getOwnPropertyDescriptor(InputCtor.prototype, 'value').set.call(timeInput, desiredTime)
              timeInput.dispatchEvent(new EventCtor('input', {bubbles: true}))
              timeInput.dispatchEvent(new EventCtor('change', {bubbles: true}))
            }
          }
        }
        if (popup && end === 1) {
          const confirmBtn = popup.querySelector('.el-picker-panel__footer button.is-plain:not(.is-text)')
          if (confirmBtn instanceof ButtonCtor && !confirmBtn.disabled && confirmBtn.isConnected) {
            await dispatchPointerClick(confirmBtn, confirmBtn, 250, () => record('ordinary'))
            pressed = confirmBtn
            element.blur()
          }
        }
      }
      return finish({success: true, message: `Input text (${text}) into element (${this.elementTextMap.get(index) ?? index}).`})
    }
    const result = await this.typed(index, element, text, () => record('ordinary'))
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
    const wrapper = findElSelectWrapperForClick(element, target, nativeControlElement)
    if (wrapper) {
      await clickElement(wrapper, () => record('ordinary'))
      return finish({success: true})
    }
    // The indexed submenu container's handler lives on its owned title.
    if (isElSubMenuContainer(element, target)) {
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
export {acceptReadSource, installOverlayOffset, readConfirmBox, pressConfirmButton, readSubmitBusy} from './form-surface.js'
