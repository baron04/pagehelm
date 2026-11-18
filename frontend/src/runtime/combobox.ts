/** Fixed single-choice ARIA operation. The application supplies the reviewed label. */
import {sleep} from './contain'
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

export function ownedTree(element: HTMLInputElement): HTMLElement | null {
  const popup = ownedPopup(element)
  if (!popup || popup.getAttribute('aria-multiselectable') === 'true') return null
  const tree = popup.getAttribute('role') === 'tree'
    ? popup
    : (() => {
      const trees = popup.querySelectorAll('[role="tree"]')
      return trees.length === 1 && trees[0] instanceof HTMLElement ? trees[0] : null
    })()
  if (!tree || tree.getAttribute('aria-multiselectable') === 'true') return null
  return tree
}

export function ownedListbox(element: HTMLInputElement): HTMLElement | null {
  if (ownedTree(element)) return null
  const list = ownedPopup(element)
  return list && list.getAttribute('role') === 'listbox' &&
    list.getAttribute('aria-multiselectable') !== 'true' ? list : null
}

export function optionText(element: Element): string {
  return (element.textContent || '').replace(/\s+/g, ' ').trim()
}

export function ownTreeOption(item: Element): HTMLElement | null {
  for (const candidate of item.querySelectorAll('[role="option"]')) {
    if (candidate instanceof HTMLElement && candidate.closest('[role="treeitem"]') === item) {
      return candidate
    }
  }
  return null
}

export function ownTreeRow(item: Element): HTMLElement | null {
  const headerChildren = [...item.children].filter(child => {
    const role = child.getAttribute('role')
    return role !== 'group' && role !== 'treeitem' &&
      !child.querySelector('[role="group"], [role="treeitem"]')
  })
  return headerChildren.length === 1 && headerChildren[0] instanceof HTMLElement
    ? headerChildren[0]
    : null
}

export function ownTreeExpandToggle(item: Element): HTMLElement | null {
  for (const explicit of item.querySelectorAll('[data-tree-expand]')) {
    if (explicit instanceof HTMLElement && explicit.closest('[role="treeitem"]') === item &&
        !explicit.classList.contains('is-leaf')) {
      return explicit
    }
  }
  const container = ownTreeRow(item) ?? item
  const opt = ownTreeOption(item)
  if (opt) {
    for (const child of container.children) {
      if (child === opt || child.contains(opt)) break
      if (child instanceof HTMLElement && !child.classList.contains('is-leaf') &&
          child.getAttribute('role') !== 'group' && child.getAttribute('role') !== 'treeitem') {
        return child
      }
    }
    return null
  }
  if (container.children.length >= 2) {
    const first = container.children[0]
    if (first instanceof HTMLElement && !first.classList.contains('is-leaf') &&
        first.getAttribute('role') !== 'group' && first.getAttribute('role') !== 'treeitem' &&
        optionText(first).length <= 2) {
      return first
    }
  }
  return null
}

function treeItemSelfLabel(item: Element): string {
  const opt = ownTreeOption(item)
  if (opt) return optionText(opt)
  const basis = ownTreeRow(item) ?? item
  const clone = basis.cloneNode(true) as Element
  for (const child of clone.querySelectorAll('[role="group"], [role="treeitem"], [aria-hidden="true"]')) {
    child.remove()
  }
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

export function matchingTreeChoices(tree: HTMLElement, label: string): {item: Element; path: string; leaf: string}[] {
  const choices = treeItemChoices(tree)
  const byPath = choices.filter(c => c.path === label)
  if (byPath.length) return byPath
  return choices.filter(c => c.leaf === label)
}

function isTreeItemSelected(item: Element): boolean {
  if (item.getAttribute('aria-selected') === 'true' ||
      item.getAttribute('aria-current') === 'true' ||
      item.classList.contains('is-current') ||
      item.classList.contains('is-selected')) return true
  const ownOpt = ownTreeOption(item)
  return Boolean(ownOpt && (ownOpt.getAttribute('aria-selected') === 'true' || ownOpt.classList.contains('is-selected')))
}

export function isTreeItemDisabled(item: Element): boolean {
  if (item.getAttribute('aria-disabled') === 'true' ||
      item.hasAttribute('disabled') ||
      item.classList.contains('is-disabled')) return true
  const ownOpt = ownTreeOption(item)
  return Boolean(ownOpt && (ownOpt.getAttribute('aria-disabled') === 'true' ||
    ownOpt.hasAttribute('disabled') || ownOpt.classList.contains('is-disabled')))
}

export function selectedTreeChoice(element: HTMLInputElement): {label: string; path: string} | null {
  const tree = ownedTree(element)
  if (!tree) return null
  const choices = treeItemChoices(tree)
  const selected = choices.filter(c => isTreeItemSelected(c.item))
  if (selected.length !== 1 || isTreeItemDisabled(selected[0]!.item)) return null
  const {leaf, path} = selected[0]!
  if (!path || choices.filter(c => c.path === path).length !== 1) return null
  const uniqueLeaf = leaf && choices.filter(c => c.leaf === leaf).length === 1
  return {label: uniqueLeaf ? leaf : path, path}
}

export function selectedTreeOption(element: HTMLInputElement): string | null {
  return selectedTreeChoice(element)?.label ?? null
}

export function treeSelectionMatches(element: HTMLInputElement, label: string): boolean {
  const tree = ownedTree(element)
  if (!tree) return false
  const choices = treeItemChoices(tree)
  const selected = choices.filter(c => isTreeItemSelected(c.item))
  if (selected.length !== 1 || isTreeItemDisabled(selected[0]!.item)) return false
  const targets = matchingTreeChoices(tree, label)
  return targets.length === 1 && targets[0]!.item === selected[0]!.item
}

export function selectedOption(element: HTMLInputElement): string | null {
  const list = ownedListbox(element)
  if (!list) return null
  const selected = [...list.querySelectorAll('[role="option"]')]
    .filter(option => option.closest('[role="listbox"]') === list &&
      (option.getAttribute('aria-selected') === 'true' || option.classList.contains('is-selected')))
  if (selected.length !== 1 ||
      selected[0]!.getAttribute('aria-disabled') === 'true' || selected[0]!.hasAttribute('disabled')) return null
  const label = optionText(selected[0]!)
  return label && [...list.querySelectorAll('[role="option"]')].filter(option =>
    optionText(option) === label).length === 1 ? label : null
}

export function selectedComboboxOption(element: HTMLInputElement): string | null {
  return ownedTree(element) ? selectedTreeOption(element) : selectedOption(element)
}

export function visibleElement(element: Element): boolean {
  const rect = element.getBoundingClientRect(), style = getComputedStyle(element)
  return Boolean(element.isConnected && rect.width && rect.height && style.display !== 'none' &&
    style.visibility !== 'hidden' && !element.closest('[hidden],[aria-hidden="true"]'))
}

/** Collect visible, enabled option labels from an open combobox popup (listbox or tree). */
export function visibleComboboxOptions(
  element: HTMLInputElement,
  maxCandidates = 12,
  maxLabelChars = 200,
): string[] {
  const tree = ownedTree(element)
  if (tree) {
    if (!visibleElement(tree)) return []
    const seen = new Set<string>()
    const out: string[] = []
    for (const choice of treeItemChoices(tree)) {
      if (isTreeItemDisabled(choice.item) || !visibleElement(choice.item)) continue
      const text = choice.path.trim().slice(0, maxLabelChars)
      if (!text || seen.has(text)) continue
      seen.add(text)
      out.push(text)
      if (out.length >= maxCandidates) break
    }
    return out
  }
  const list = ownedListbox(element)
  if (!list || !visibleElement(list)) return []
  const seen = new Set<string>()
  const out: string[] = []
  for (const option of list.querySelectorAll('[role="option"]')) {
    if (option.closest('[role="listbox"]') !== list) continue
    if (option.getAttribute('aria-disabled') === 'true' || option.hasAttribute('disabled')) continue
    if (!visibleElement(option)) continue
    const text = optionText(option).trim().slice(0, maxLabelChars)
    if (!text || seen.has(text)) continue
    seen.add(text)
    out.push(text)
    if (out.length >= maxCandidates) break
  }
  return out
}

/** True when a combobox input has an empty value and no selected option in its owned popup. */
export function isComboboxClearedWithoutSelection(element: HTMLInputElement): boolean {
  return listboxCombobox(element) &&
    String(element.value || '').trim() === '' &&
    selectedComboboxOption(element) === null
}

/**
 * When a combobox popup opens with an empty option list (e.g. async remote options),
 * wait briefly (~250ms bounded) for visible options to populate before concluding absence.
 */
export async function probeComboboxOptions(
  element: HTMLInputElement,
  {
    timeoutMs = 250,
    intervalMs = 50,
    interrupted = () => false,
  }: {timeoutMs?: number; intervalMs?: number; interrupted?: () => boolean} = {},
): Promise<boolean> {
  if (visibleComboboxOptions(element, 1).length > 0) return true
  const popup = ownedTree(element) ?? ownedListbox(element)
  if (!popup || !visibleElement(popup)) return false
  const deadline = Date.now() + Math.max(0, timeoutMs)
  while (!interrupted() && element.isConnected && Date.now() < deadline) {
    const remaining = deadline - Date.now()
    if (remaining <= 0) break
    await sleep(Math.min(intervalMs, remaining))
    if (interrupted() || !element.isConnected) return false
    if (visibleComboboxOptions(element, 1).length > 0) return true
    const currentPopup = ownedTree(element) ?? ownedListbox(element)
    if (!currentPopup || !visibleElement(currentPopup)) return false
  }
  return visibleComboboxOptions(element, 1).length > 0
}
