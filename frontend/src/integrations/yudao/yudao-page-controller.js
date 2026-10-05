/** Application-owned wrapper around the pinned generic controller, never a new harness. */
export {PageController} from './page-controller.js'
import {getElementByIndex as controllerElement} from './page-controller.js'
import {nativeControlElement} from './page-facts.js'
export function getElementByIndex(selectorMap, index) {
  return nativeControlElement(controllerElement(selectorMap, index))
}
export {readPageBlocker, readPageRecordRoute, readPageRoute, readPageQuery} from './page-facts.js'
