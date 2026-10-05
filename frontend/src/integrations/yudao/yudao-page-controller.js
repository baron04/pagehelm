/** Application-owned wrapper around the pinned generic controller, never a new harness. */
import {PageController as GenericPageController, getElementByIndex as controllerElement, clickElement as controllerClick} from './page-controller.js'
import {nativeControlElement, nativeRangeDateCell} from './page-facts.js'
export class PageController extends GenericPageController {
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
  readElementNavigation, readElementPageControl} from './page-facts.js'
