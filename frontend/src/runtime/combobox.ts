/** Fixed single-choice ARIA operation. The application supplies the reviewed label. */
import type {ElementLike} from './pageModel'

export function listboxCombobox(element: ElementLike | null | undefined): element is HTMLInputElement {
  return typeof HTMLInputElement !== 'undefined' && element instanceof HTMLInputElement && element.getAttribute('role') === 'combobox' &&
    element.getAttribute('aria-haspopup') === 'listbox' && !element.disabled &&
    element.getAttribute('aria-disabled') !== 'true'
}

export function ownedListbox(element: HTMLInputElement): HTMLElement | null {
  const ids = [element.getAttribute('aria-controls'), element.getAttribute('aria-owns')]
    .filter((id): id is string => id !== null)
  if (!ids.length || new Set(ids).size !== 1 || !/^[A-Za-z][A-Za-z0-9_:.-]{0,199}$/.test(ids[0]!)) return null
  const found = element.ownerDocument.querySelectorAll(`[id="${ids[0]}"]`)
  const list = found[0]
  return found.length === 1 && list instanceof HTMLElement && list.getAttribute('role') === 'listbox' &&
    list.getAttribute('aria-multiselectable') !== 'true' ? list : null
}

export function optionText(element: Element): string {
  return (element.textContent || '').replace(/\s+/g, ' ').trim()
}

export function selectedOption(element: HTMLInputElement): string | null {
  const list = ownedListbox(element)
  if (!list) return null
  const selected = list.querySelectorAll('[role="option"][aria-selected="true"]')
  if (selected.length !== 1 || selected[0]!.closest('[role="listbox"]') !== list ||
      selected[0]!.getAttribute('aria-disabled') === 'true' || selected[0]!.hasAttribute('disabled')) return null
  const label = optionText(selected[0]!)
  return label && [...list.querySelectorAll('[role="option"]')].filter(option =>
    optionText(option) === label).length === 1 ? label : null
}

export function visibleElement(element: Element): boolean {
  const rect = element.getBoundingClientRect(), style = getComputedStyle(element)
  return Boolean(element.isConnected && rect.width && rect.height && style.display !== 'none' &&
    style.visibility !== 'hidden' && !element.closest('[hidden],[aria-hidden="true"]'))
}
