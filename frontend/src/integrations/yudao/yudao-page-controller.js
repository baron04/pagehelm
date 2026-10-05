/** Application-owned wrapper around the pinned generic controller, never a new harness. */
import {PageController as GenericPageController, getElementByIndex as controllerElement} from './page-controller.js'
import {nativeControlElement} from './page-facts.js'
export class PageController extends GenericPageController {
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
export {readPageBlocker, readPageRecordRoute, readPageRoute, readPageQuery, readElementNavigation} from './page-facts.js'
