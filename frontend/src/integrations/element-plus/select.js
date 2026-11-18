/** Generic Element Plus select, tree-select, radio, and menu helpers. */
import {isLiveComponent} from './vue.js'

export function shouldAvoidSelectInnerInput(element, hit) {
  return element instanceof HTMLElement && element.classList.contains('el-select__wrapper') &&
    hit instanceof HTMLInputElement && hit.classList.contains('el-select__input')
}

/**
 * Label focus or inner combobox input click (@click.stop in Element Plus) does not
 * open Element Plus's select. Resolve its exact owned `.el-select__wrapper` when present.
 */
export function findElSelectWrapperForClick(element, target, resolveNative) {
  if (!(target instanceof HTMLInputElement)) return null
  const labelOrCollapsed = (element instanceof HTMLLabelElement && element !== target) ||
    (element === target && target.getAttribute('role') === 'combobox' &&
      target.getAttribute('aria-expanded') !== 'true')
  if (!labelOrCollapsed) return null
  const wrapper = target.closest('.el-select__wrapper')
  return wrapper && resolveNative(wrapper) === target ? wrapper : null
}

export function isElSubMenuContainer(element, target) {
  return element !== target && element.matches('li.el-sub-menu')
}

/** Map one Element Plus hit area (`.el-select__wrapper`, `<label>`, `.el-radio`, `.el-tree-node`, `li.el-sub-menu`)
 * to its accessible native control or trigger element. */
export function resolveElControlElement(element, {isQueryLabel, isBranchMenu} = {}) {
  // The controller may index an Element Plus form label while omitting its
  // readonly select input. Resolve only HTML's exact association inside one
  // query item or the same dialog form; keep the label as the click hit area.
  if (element instanceof HTMLLabelElement) {
    const control = element.control, item = element.closest('.el-form-item')
    const dialog = element.closest('[role="dialog"],dialog')
    const dialogField = dialog && control?.closest('[role="dialog"],dialog') === dialog
      && element.closest('form') && element.closest('form') === control.closest('form')
    if ((dialogField || !dialog && isQueryLabel?.(element))
        && control instanceof HTMLInputElement && item && control.closest('.el-form-item') === item
        && item.querySelectorAll('input[role="combobox"]').length === 1
        && /^[A-Za-z][A-Za-z0-9_:.-]{0,199}$/.test(control.id)
        && document.querySelectorAll(`[id="${control.id}"]`).length === 1
        && control.getAttribute('role') === 'combobox'
        && ['listbox', 'tree'].includes(control.getAttribute('aria-haspopup'))
        && !control.disabled && control.getAttribute('aria-disabled') !== 'true'
        && control.closest('.el-select__wrapper')?.classList.contains('is-disabled') === false) return control
  }
  if (isBranchMenu?.(element) && element.matches('li.el-sub-menu')) {
    return element.querySelector(':scope > .el-sub-menu__title')
  }
  if (element instanceof HTMLElement && element.matches('.el-radio,.el-radio-button')) {
    const radios = element.querySelectorAll('input[type="radio"]')
    return radios.length === 1 && radios[0].closest('.el-radio,.el-radio-button') === element
      ? radios[0] : element
  }
  if (element instanceof HTMLElement && element.matches('.el-tree-node[role="treeitem"]')) {
    return element.querySelector(':scope > .el-tree-node__content') ?? element
  }
  if (!(element instanceof HTMLElement) || !element.matches('.el-select__wrapper') ||
      element.classList.contains('is-disabled') || element.getAttribute('aria-disabled') === 'true') return element
  const inputs = element.querySelectorAll('input[role="combobox"][aria-haspopup="listbox"][aria-controls],input[role="combobox"][aria-haspopup="tree"][aria-controls]')
  return inputs.length === 1 && inputs[0].closest('.el-select__wrapper') === element
    ? inputs[0] : element
}

/** Find a mounted `ElMenuItem` or `ElSubMenu` instance matching `root` in the Vue VNode tree. */
export function findElMenuInstance(root, appVNode, maxNodes = 8192) {
  const stack = [appVNode], nodes = new Set()
  let instance
  while (stack.length && nodes.size < maxNodes) {
    const node = stack.pop()
    if (!node || nodes.has(node)) continue
    nodes.add(node)
    if (node.el instanceof HTMLElement && node.el !== root && !node.el.contains(root)) continue
    if (node.component) {
      const candidate = node.component
      if (!isLiveComponent(candidate)) continue
      if (['ElMenuItem', 'ElSubMenu'].includes(candidate.type?.name) && candidate.subTree?.el === root) {
        if (instance && instance !== candidate) return undefined
        instance = candidate
      }
      stack.push(candidate.subTree)
    }
    if (Array.isArray(node.children)) stack.push(...node.children)
  }
  if (stack.length || !isLiveComponent(instance) || instance.subTree?.el !== root) return undefined
  return instance
}

/** Visible `.el-tree-node` hit areas omitted by the generic controller. Never index a branch's
 * whole subtree: clicking its centre can hit a different descendant. */
export function collectElTreeQueryControls(selectorMap, html, root = document, readControl) {
  const lines = []
  const counts = new Map()
  for (const label of root.querySelectorAll('.el-tree-node__content > .el-tree-node__label')) {
    const name = label.textContent?.trim()
    if (name) counts.set(name, (counts.get(name) || 0) + 1)
  }
  let top = Math.max(0, ...selectorMap.keys())
  for (const area of root.querySelectorAll('.el-tree-node > .el-tree-node__content')) {
    const rect = area.getBoundingClientRect()
    if (!rect.width || !rect.height || rect.bottom <= 0 || rect.top >= window.innerHeight ||
        area.closest('[hidden],[aria-hidden="true"]') || area.closest('.el-tree-node')?.closest('[role="dialog"],dialog')) continue
    const x = Math.max(0, Math.min(window.innerWidth - 1, rect.left + rect.width / 2))
    const y = Math.max(0, Math.min(window.innerHeight - 1, rect.top + rect.height / 2))
    const at = root.elementFromPoint?.(x, y)
    if (at && at !== area && !area.contains(at)) continue
    const fact = readControl?.(area)
    if (!fact || lines.length >= 32) continue
    let label = area.querySelector('.el-tree-node__label')?.textContent?.trim()
    if (!label || label.length > 120 || /[<>\r\n]/.test(label)) continue
    if (counts.get(label) > 1) {
      const path = [label]
      let parent = area.parentElement?.parentElement?.closest('.el-tree-node')
      while (parent && path.length < 8) {
        const name = parent.querySelector(':scope > .el-tree-node__content .el-tree-node__label')?.textContent?.trim()
        if (!name || /[<>\r\n]/.test(name)) break
        path.unshift(name)
        parent = parent.parentElement?.closest('.el-tree-node')
      }
      label = path.join(' / ').slice(0, 300)
    }
    const existing = [...selectorMap].find(([, node]) => node.ref === area)
    const index = existing?.[0] ?? ++top
    if (existing) continue
    selectorMap.set(index, {ref: area, tagName: 'div', isInteractive: true, highlightIndex: index})
    lines.push(`[${index}]<div role=treeitem aria-selected=${fact.selected}>${label} />`)
  }
  return lines.length ? lines.join('\n') + '\n' + html : html
}

function isVisibleElOption(node) {
  if (!(node instanceof Element) || !node.isConnected) return false
  const rect = node.getBoundingClientRect()
  const view = node.ownerDocument?.defaultView ?? window
  const style = view.getComputedStyle(node)
  return Boolean(rect.width && rect.height && style.display !== 'none' && style.visibility !== 'hidden' &&
    !node.closest('[hidden],[aria-hidden="true"]'))
}

function findElOwnedPopup(element) {
  if (!(element instanceof Element) || !element.isConnected) return null
  const doc = element.ownerDocument
  const ids = [element.getAttribute('aria-controls'), element.getAttribute('aria-owns')].filter(Boolean)
  if (!ids.length || new Set(ids).size !== 1 || !/^[A-Za-z][A-Za-z0-9_:.-]{0,199}$/.test(ids[0])) return null
  const found = doc.querySelectorAll(`[id="${ids[0]}"]`)
  if (found.length !== 1) return null
  return found[0].closest('.el-select-dropdown,.el-tree-select__popper') ?? found[0]
}

/** Collect visible enabled option labels from an open Element Plus select/tree-select dropdown. */
export function readElSelectVisibleCandidates(element, maxCandidates = 12, maxChars = 200) {
  const popup = findElOwnedPopup(element)
  if (!popup || !isVisibleElOption(popup)) return []
  const items = popup.querySelectorAll('.el-tree-node[role="treeitem"], .el-select-dropdown__item, [role="option"]')
  const seen = new Set()
  const out = []
  for (const item of items) {
    if (item.classList.contains('is-disabled') || item.getAttribute('aria-disabled') === 'true' || item.hasAttribute('disabled')) {
      continue
    }
    if (!isVisibleElOption(item)) continue
    const labelNode = item.matches('.el-tree-node[role="treeitem"]')
      ? (item.querySelector(':scope > .el-tree-node__content .el-tree-node__label') ?? item)
      : item
    const text = (labelNode.textContent || '').replace(/\s+/g, ' ').trim().slice(0, maxChars)
    if (!text || seen.has(text)) continue
    seen.add(text)
    out.push(text)
    if (out.length >= maxCandidates) break
  }
  return out
}

/** True when an Element Plus select input cleared without any selected item or tag. */
export function isElSelectClearedWithoutSelection(element) {
  if (!(element instanceof HTMLInputElement) || !element.isConnected) return false
  if (String(element.value || '').trim() !== '') return false
  const wrapper = element.closest('.el-select__wrapper, .el-select')
  if (wrapper) {
    const selectedItems = [...wrapper.querySelectorAll('.el-select__selected-item, .el-tag')]
      .filter(node => !node.classList.contains('is-hidden') &&
        !node.classList.contains('el-select__input-wrapper') &&
        !node.classList.contains('el-select__placeholder') &&
        !node.querySelector('input') &&
        (node.textContent || '').trim() !== '')
    if (selectedItems.length > 0) return false
  }
  const popup = findElOwnedPopup(element)
  if (popup) {
    const selected = popup.querySelectorAll('[aria-selected="true"], .el-select-dropdown__item.is-selected, .el-tree-node.is-current')
    if (selected.length > 0) return false
  }
  return true
}

/** Bounded ~250ms async option probe when an Element Plus dropdown opens with an empty list. */
export async function probeElSelectOptions(element, {timeoutMs = 250, intervalMs = 50} = {}) {
  if (readElSelectVisibleCandidates(element, 1).length > 0) return true
  const popup = findElOwnedPopup(element)
  if (!popup || !isVisibleElOption(popup)) return false
  const deadline = Date.now() + Math.max(0, timeoutMs)
  while (element.isConnected && Date.now() < deadline) {
    const remaining = deadline - Date.now()
    if (remaining <= 0) break
    await new Promise(resolve => setTimeout(resolve, Math.min(intervalMs, remaining)))
    if (!element.isConnected) return false
    if (readElSelectVisibleCandidates(element, 1).length > 0) return true
    const current = findElOwnedPopup(element)
    if (!current || !isVisibleElOption(current)) return false
  }
  return readElSelectVisibleCandidates(element, 1).length > 0
}
