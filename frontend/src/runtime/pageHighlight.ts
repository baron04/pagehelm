/**
 * Control highlight and submit-guide overlays for the browser Runtime.
 *
 * All presentation geometry and DOM observers stay in this browser module; a
 * presentation failure never blocks or invalidates a verified handoff.
 */
import {formControlScope, type ElementLike} from './pageModel'
import {createControlGuide, type ControlGuide} from '../handoff/guide'

export interface PageHighlight {
  clear(): void
  active(): boolean
  readonly preserveOnInteraction?: boolean
}

/** Controller annotations are temporary observation UI, not the control's context. */
function controlContextText(owner: Element | null): string {
  if (!owner) return ''
  const nodes = owner.ownerDocument.createTreeWalker(owner, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode: node => node instanceof Element && node.id === 'playwright-highlight-container'
      ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  })
  let text = ''
  while (nodes.nextNode()) if (nodes.currentNode.nodeType === Node.TEXT_NODE) text += nodes.currentNode.textContent
  return text
}

/** Mark one observed control without focusing, clicking, or covering its hit target. */
export function highlightControl(
  element: ElementLike | null | undefined,
  panelContainer?: HTMLElement,
  turnId = '',
): PageHighlight {
  if (!(element instanceof HTMLElement || element instanceof SVGElement) ||
      !element.isConnected) {
    throw new Error('bad_arguments: highlight target is no longer on the page')
  }
  element.scrollIntoView?.({block: 'center', inline: 'nearest', behavior: 'auto'})
  const rect = element.getBoundingClientRect()
  const computed = getComputedStyle(element)
  if (rect.width <= 0 || rect.height <= 0 || computed.display === 'none' ||
      computed.visibility === 'hidden') {
    throw new Error('bad_arguments: highlight target is not visible')
  }
  const original = ['outline', 'outline-offset'].map(property => ({
    property, value: element.style.getPropertyValue(property),
    priority: element.style.getPropertyPriority(property),
  }))
  element.style.setProperty('outline', '3px solid Highlight', 'important')
  // Keep the ring inside compact controls: table cells commonly clip overflow just
  // outside a link button. This does not change layout, hit testing or the app's CSS.
  element.style.setProperty('outline-offset', '-3px', 'important')
  const applied = new Map(original.map(saved => [
    saved.property, element.style.getPropertyValue(saved.property),
  ]))
  const owner = element.closest('tr,[role="row"],[role="dialog"],form') ?? element.parentElement
  const ownerText = controlContextText(owner), label = element.textContent
  const current = () => element.isConnected &&
    (element.closest('tr,[role="row"],[role="dialog"],form') ?? element.parentElement) === owner &&
    controlContextText(owner) === ownerText && element.textContent === label
  let active = true
  let guide: ControlGuide | null = null
  const clear = () => {
    if (!active) return
    active = false
    guide?.clear()
    observer.disconnect()
    for (const saved of original) {
      // An application that changed this style after handoff owns its new value.
      if (element.style.getPropertyPriority(saved.property) !== 'important' ||
          element.style.getPropertyValue(saved.property) !== applied.get(saved.property)) continue
      if (saved.value) {
        element.style.setProperty(saved.property, saved.value, saved.priority)
      } else {
        element.style.removeProperty(saved.property)
      }
    }
  }
  const observer = new MutationObserver(() => {
    if (!current()) clear()
  })
  observer.observe(document.documentElement, {childList: true, characterData: true, subtree: true})
  if (panelContainer && element instanceof HTMLElement) {
    try {
      guide = createControlGuide({
        target: element, container: panelContainer,
        turnId, purpose: 'action', current,
      })
    } catch { /* Keep the original marker if presentation fails. */ }
  }
  return {
    clear,
    active: () => active && current() && original.every(saved =>
      element.style.getPropertyPriority(saved.property) === 'important' &&
      element.style.getPropertyValue(saved.property) === applied.get(saved.property)),
  }
}

/** Follow the exact verified submit control; all geometry and hints stay in this browser. */
export function guideSubmitControl(
  element: ElementLike | null | undefined,
  panelContainer: HTMLElement | undefined,
  turnId: string,
): PageHighlight {
  if (!(element instanceof HTMLElement) || !element.isConnected) {
    throw new Error('bad_arguments: highlight target is no longer on the page')
  }
  const scope = formControlScope(element)
  const label = element.textContent
  const owner = element.closest('[role="dialog"],dialog,form') ?? element.parentElement
  if (panelContainer) {
    try {
      const guide = createControlGuide({
        target: element, container: panelContainer, turnId, purpose: 'submit',
        current: () => formControlScope(element) === scope && element.textContent === label
          && (element.closest('[role="dialog"],dialog,form') ?? element.parentElement) === owner,
      })
      if (guide) return guide
    } catch { /* A presentation failure must not fail the verified handoff. */ }
  }
  return highlightControl(element, panelContainer, turnId)
}
