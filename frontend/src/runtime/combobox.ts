/** Fixed single-choice ARIA operation. The application supplies the reviewed label. */
import type {ElementLike} from './pageModel'

/** Only the fixed selection tool can issue this mechanism fact. */
export class ChoiceUnavailable extends Error {
  readonly code = 'option_unavailable'
  constructor(readonly candidate_labels: string[], readonly no_effect: boolean, reason = '') {
    super('option_unavailable: the owned list does not offer this exact option' +
      (no_effect ? '' : '; no-effect proof unavailable: ' + reason))
  }
}

export function listboxCombobox(element: ElementLike | null | undefined): element is HTMLInputElement {
  if (typeof HTMLInputElement === 'undefined' || !(element instanceof HTMLInputElement)) return false
  const popup = element.getAttribute('aria-haspopup')
  return element.getAttribute('role') === 'combobox' &&
    (popup === 'listbox' || popup === 'tree') && !element.disabled &&
    element.getAttribute('aria-disabled') !== 'true'
}

function ownedPopup(element: HTMLInputElement): HTMLElement | null {
  const ids = [element.getAttribute('aria-controls'), element.getAttribute('aria-owns')]
    .filter((id): id is string => id !== null)
  if (!ids.length || new Set(ids).size !== 1 || !/^[A-Za-z][A-Za-z0-9_:.-]{0,199}$/.test(ids[0]!)) return null
  const found = element.ownerDocument.querySelectorAll(`[id="${ids[0]}"]`)
  const popup = found[0]
  return found.length === 1 && popup instanceof HTMLElement ? popup : null
}

export function ownedListbox(element: HTMLInputElement): HTMLElement | null {
  const list = ownedPopup(element)
  return list && list.getAttribute('role') === 'listbox' &&
    list.getAttribute('aria-multiselectable') !== 'true' ? list : null
}

export function ownedTree(element: HTMLInputElement): HTMLElement | null {
  const popup = ownedPopup(element)
  if (!popup) return null
  const tree = popup.getAttribute('role') === 'tree'
    ? popup
    : (() => {
      const trees = popup.querySelectorAll('[role="tree"]')
      return trees.length === 1 && trees[0] instanceof HTMLElement ? trees[0] : null
    })()
  if (!tree || tree.getAttribute('aria-multiselectable') === 'true') return null
  return tree
}

function treeItemSelfLabel(item: Element): string {
  const directLabel = item.querySelector(':scope > .el-tree-node__content .el-tree-node__label')
    ?? item.querySelector(':scope > .el-tree-node__content')
  if (directLabel) return optionText(directLabel)
  const clone = item.cloneNode(true) as Element
  for (const child of clone.querySelectorAll('[role="group"], [role="treeitem"]')) child.remove()
  return optionText(clone)
}

export function treeItemPath(tree: HTMLElement, item: Element): string {
  const parts: string[] = []
  let current: Element | null = item
  while (current && tree.contains(current) && parts.length < 8) {
    if (current.getAttribute('role') === 'treeitem') {
      const label = treeItemSelfLabel(current)
      if (label) parts.unshift(label)
    }
    current = current.parentElement?.closest('[role="treeitem"]') ?? null
  }
  return parts.join(' / ')
}

export function treeItemChoices(tree: HTMLElement): {item: Element; path: string; leaf: string}[] {
  return [...tree.querySelectorAll('[role="treeitem"]')].map(item => ({
    item,
    path: treeItemPath(tree, item),
    leaf: treeItemSelfLabel(item),
  })).filter(entry => entry.leaf.length > 0)
}

export function selectedTreeOption(element: HTMLInputElement): string | null {
  const tree = ownedTree(element)
  if (!tree) return null
  const selected = [...tree.querySelectorAll('[role="treeitem"]')].filter(item =>
    item.getAttribute('aria-selected') === 'true' || item.classList.contains('is-current'))
  if (selected.length !== 1 || selected[0]!.getAttribute('aria-disabled') === 'true' ||
      selected[0]!.hasAttribute('disabled')) return null
  const leaf = treeItemSelfLabel(selected[0]!)
  const path = treeItemPath(tree, selected[0]!)
  const choices = treeItemChoices(tree)
  if (leaf && choices.filter(c => c.leaf === leaf).length === 1) return leaf
  return path && choices.filter(c => c.path === path).length === 1 ? path : null
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
