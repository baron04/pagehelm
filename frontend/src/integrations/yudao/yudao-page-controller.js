/** Application-owned wrapper around the pinned generic controller, never a new harness. */
import {PageController as GenericPageController, getElementByIndex as controllerElement, clickElement as controllerClick} from './page-controller.js'
import {lineControls, nativeControlElement, nativeRangeDateCell, withLineControls} from './page-facts.js'
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
    const result = await super.inputText(index, text)
    if (!range || result?.success !== true) return result
    const cell = nativeRangeDateCell(element, text)
    if (!cell || !cell.isConnected || !cell.getBoundingClientRect().width) {
      throw new Error('the exact native calendar day is unavailable')
    }
    // Stay inside the owned popup before the generic controller's pre-click blur.
    // Native calendar selection preserves the application's default endpoint times.
    cell.focus({preventScroll: true})
    await controllerClick(cell)
    return result
  }

  async clickElement(index) {
    const element = controllerElement(this.selectorMap, index)
    const target = nativeControlElement(element)
    if (element instanceof HTMLLabelElement && target instanceof HTMLInputElement && element !== target) {
      // Label focus does not open Element Plus's query select. Click its exact
      // owned wrapper through the existing controller's fixed DOM operation.
      const wrapper = target.closest('.el-select__wrapper')
      if (wrapper && nativeControlElement(wrapper) === target) {
        await controllerClick(wrapper)
        return {success: true}
      }
    }
    // The indexed submenu container's handler lives on its owned title.
    if (element !== target && element.matches('li.el-sub-menu')) {
      target.click()
      return {success: true}
    }
    return super.clickElement(index)
  }
}
export function getElementByIndex(selectorMap, index) {
  return nativeControlElement(controllerElement(selectorMap, index))
}
export {readPageBlocker, readPageRecordRoute, readPageRoute, readPageQuery, readPageRead,
  readElementNavigation, readElementPageControl, openPage} from './page-facts.js'
