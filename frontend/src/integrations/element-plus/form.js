/** Generic Element Plus form, dialog, message-box, loading/error, pagination, and overlay helpers. */
import {isElDateOrDatetimeRangeInput} from './date-picker.js'
import {collectLiveVNodes, isElAvailable, isElRendered, isElVisible} from './vue.js'

const FORM_SECRET = /pass(word)?|pwd|secret|token|credential|密码|密钥|令牌/i
const FORM_LIMITS = {depth: 8, nodes: 6000, bytes: 200000}

const CONFIRM_BOX = '.el-message-box'
const BUSY_SELECTORS = '.el-loading-mask, .el-button.is-loading, [aria-busy="true"]'
const OVERLAY_OFFSET_CSS = '.el-overlay, .el-drawer__wrapper { right: var(--woa-drawer-offset, 0px); }'
const overlayOffsetOwners = new WeakMap()

export function hasVisibleElLoading(doc) {
  return Array.from(doc.querySelectorAll('.el-loading-mask,.el-loading-spinner,[aria-busy="true"]')).some(isElVisible)
}

export function hasVisibleElError(doc) {
  return Array.from(doc.querySelectorAll('.el-message--error, .el-notification--error')).some(isElVisible)
}

export function readElPaginationTotals(doc) {
  return Array.from(doc.querySelectorAll('.el-pagination__total'))
    .filter(element => isElVisible(element) && !element.closest('[role="dialog"],dialog'))
    .map(element => Number((element.textContent || '').replace(/[^0-9]/g, '')))
}

export function readElPaginationControl(element, findComponent) {
  const root = element.closest('.el-pagination')
  if (!root) return undefined
  // The rows-per-page choice of the same pagination: its own select, nothing else in it.
  const sizes = element.closest('.el-pagination__sizes')
  if (sizes && root.contains(sizes)) {
    const owner = findComponent(root, 'ElPagination')
    const size = owner?.instance.props?.pageSize
    // Only the select's own hit area or input: what is inside them is not a control.
    if (!owner || !Number.isSafeInteger(size) || size < 1 ||
        !element.matches('.el-select__wrapper,input[role="combobox"]') ||
        element.matches(':disabled,.is-disabled,[aria-disabled="true"]')) return undefined
    return {kind: 'page_size', pageSize: size}
  }
  const target = element.closest('button.btn-prev,button.btn-next,li.number')
  if (!target || !root.contains(target) || target.matches(':disabled,.is-disabled,[aria-disabled="true"]')) return undefined
  const binding = findComponent(root, 'ElPagination')
  if (!binding) return undefined
  const {currentPage, pageSize, total} = binding.instance.props
  if (![currentPage, pageSize, total].every(Number.isSafeInteger) || currentPage < 1 || pageSize < 1 || total < 0) return undefined
  const page = target.matches('.btn-prev') ? currentPage - 1 : target.matches('.btn-next') ? currentPage + 1 : Number(target.textContent.trim())
  if (!Number.isSafeInteger(page) || page < 1 || page > Math.ceil(total / pageSize) || page === currentPage) return undefined
  return {kind: 'pagination', page, pageSize}
}

export function readElFormFacets(doc, visibleFn = isElVisible) {
  const facts = []
  for (const item of Array.from(doc.querySelectorAll('.el-form-item')).filter(visibleFn)) {
    if (item.closest('[role="dialog"],dialog')) continue
    const label = item.querySelector('.el-form-item__label')?.textContent?.trim()
    if (!label) continue
    const filled = item.querySelector(
      '.el-select__selected-item:not(.is-hidden), .el-radio.is-checked, .el-checkbox.is-checked')
      || Array.from(item.querySelectorAll('input:not([type="hidden"]), textarea'))
        .filter(visibleFn).some(input => input.value !== '')
    if (filled && facts.length < 16) facts.push({kind: 'filter', name: label})
  }
  return facts
}

/** A plain-data copy of the form's model: bounded, no functions, credentials named and left out. */
function cloneElFormModel(model) {
  const withheld = [], active = new Set()
  let nodes = 0
  const walk = (value, depth, path) => {
    if (++nodes > FORM_LIMITS.nodes) throw new Error('too_many_values')
    if (depth > FORM_LIMITS.depth) throw new Error('too_deep')
    if (value === null || typeof value === 'string' || typeof value === 'boolean') return value
    // As JSON.stringify, which is what the page sends: a number that is not finite is null.
    // Strip binary64 floating-point tail noise (e.g. 1370.13 + 1381.43 = 2751.5600000000004).
    if (typeof value === 'number') {
      if (!Number.isFinite(value)) return null
      return Number.isInteger(value) ? value : Number(value.toPrecision(15))
    }
    if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value.toISOString()
    if (typeof value !== 'object') return undefined
    if (active.has(value)) throw new Error('cyclic')
    active.add(value)
    try {
      if (Array.isArray(value)) return value.map((item, index) => walk(item, depth + 1, path + '.' + index) ?? null)
      const out = {}
      for (const key of Object.keys(value)) {
        if (FORM_SECRET.test(key)) { withheld.push(path ? path + '.' + key : key); continue }
        const item = walk(value[key], depth + 1, path ? path + '.' + key : key)
        if (item !== undefined) out[key] = item
      }
      return out
    } finally { active.delete(value) }
  }
  try {
    const data = walk(model, 0, '')
    return JSON.stringify(data).length <= FORM_LIMITS.bytes ? {data, withheld} : {unreadable: 'too_large'}
  } catch (error) { return {unreadable: String(error?.message || 'not_data').slice(0, 40)} }
}

/** JSON structure equality: property order is immaterial, scalar types and arrays are exact. */
export function sameElFormStructure(left, right) {
  if (left === right) return true
  if (left === null || right === null || typeof left !== 'object' || typeof right !== 'object') return false
  if (Array.isArray(left) || Array.isArray(right)) return Array.isArray(left) && Array.isArray(right) &&
    left.length === right.length && left.every((value, index) => sameElFormStructure(value, right[index]))
  const keys = Object.keys(left)
  return keys.length === Object.keys(right).length && keys.every(key =>
    Object.hasOwn(right, key) && sameElFormStructure(left[key], right[key]))
}

function shownInFormItem(root) {
  const parts = []
  for (const node of root.querySelectorAll('.el-select__selected-item,.el-radio.is-checked,.el-checkbox.is-checked,.el-tag')) {
    const text = (node.textContent || '').replace(/\s+/g, ' ').trim()
    if (text) parts.push(text)
  }
  for (const node of root.querySelectorAll('input:not([type="hidden"]):not([type="password"]),textarea')) {
    if (node.closest('.el-select')) continue
    const text = String(node.value || '').trim()
    if (text) parts.push(text)
  }
  return [...new Set(parts)].join('、').slice(0, 200)
}

export function readElFormModel(element, {findComponent, readSource}) {
  if (!(element instanceof HTMLElement) || !element.isConnected) return undefined
  const dialog = element.closest('[role="dialog"],dialog') || element.closest('form.el-form')
  if (!dialog) return undefined
  const forms = (dialog.matches('form.el-form') ? [dialog] : Array.from(dialog.querySelectorAll('form.el-form'))).filter(isElRendered)
  const roots = forms.filter(form => !forms.some(other => other !== form && other.contains(form)))
  if (Array.from(dialog.querySelectorAll('.el-loading-mask')).some(isElRendered)) return {unreadable: 'still_loading'}
  if (forms.length > 32 || roots.length !== 1) return {unreadable: roots.length ? 'several_forms' : 'no_form'}
  const form = roots[0], bound = findComponent(form, 'ElForm', true)
  if (!bound) return {unreadable: 'form_component_not_found'}
  const model = bound.instance.props?.model
  if (!model || typeof model !== 'object' || Array.isArray(model)) return {unreadable: 'model_not_an_object'}
  const copy = cloneElFormModel(model)
  if (copy.unreadable) return {unreadable: copy.unreadable}
  const id = model.id
  const hasId = id !== undefined && id !== null && id !== ''
  if (hasId && !(typeof id === 'number' && Number.isSafeInteger(id) && id > 0
      || typeof id === 'string' && /^[1-9][0-9]{0,19}$/.test(id))) return {unreadable: 'record_id_not_plain'}
  const items = [], invalid = []
  for (const node of collectLiveVNodes(bound.instance)) {
    const component = node.component
    if (!component || (component.type?.name || component.type?.__name) !== 'ElFormItem') continue
    const prop = component.props?.prop, root = component.subTree?.el
    if (typeof prop !== 'string' || !prop || !(root instanceof HTMLElement) || !root.isConnected) continue
    const label = String(component.props.label || root.querySelector('.el-form-item__label')?.textContent || '')
      .replace(/\s+/g, ' ').trim().slice(0, 120)
    items.push({prop, label, display: isElRendered(root) ? shownInFormItem(root) : ''})
    if (root.classList.contains('is-error')) invalid.push(prop)
    if (items.length >= 400) break
  }
  const title = (dialog.querySelector('.el-dialog__header')?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120)
  return {
    model: copy.data, withheld: copy.withheld, invalid: invalid.slice(0, 40), items, title,
    mode: hasId ? 'update' : 'create', record_id: hasId ? String(id) : '',
    source: readSource?.(hasId ? id : ''),
  }
}

export async function restoreElFormModel(element, request, {findComponent, readModel}) {
  const before = readModel(element)
  if (!before || before.unreadable || before.withheld?.length || !request ||
      !sameElFormStructure(before.model, request.expected) ||
      !request.values || typeof request.values !== 'object' || Array.isArray(request.values)) return false
  const area = element.closest('[role="dialog"],dialog') || element.closest('form.el-form')
  const forms = area?.matches('form.el-form') ? [area] : [...(area?.querySelectorAll('form.el-form') || [])]
  const root = forms.find(form => !forms.some(other => other !== form && other.contains(form)))
  const bound = root && findComponent(root, 'ElForm', true)
  const model = bound?.instance.props?.model
  const values = cloneElFormModel(request.values)
  if (!model || values.unreadable || values.withheld.length || Object.keys(values.data).some(key =>
      key === 'id' || key === '__proto__' || key === 'constructor' || key === 'prototype' ||
      !Object.hasOwn(model, key))) return false
  const validate = bound.instance.exposed?.validate || bound.instance.proxy?.validate
  if (typeof validate !== 'function') return false
  const keys = Object.keys(values.data)
  const expected = cloneElFormModel({...before.model, ...values.data}).data
  try {
    for (const key of keys) model[key] = values.data[key]
    await Promise.resolve()
    if (await validate() === false) throw new Error('application validation refused')
    await Promise.resolve()
    const after = readModel(element)
    if (!after || after.unreadable || after.withheld?.length || after.invalid?.length ||
        !sameElFormStructure(after.model, expected)) throw new Error('restored form changed')
    return true
  } catch {
    // Validation and reactive dependencies can modify other fields too. Restore the complete
    // before-image, never leave a partially restored business form for an accidental save.
    for (const key of Object.keys(model)) if (!Object.hasOwn(before.model, key)) delete model[key]
    for (const [key, value] of Object.entries(before.model)) model[key] = value
    await Promise.resolve()
    return false
  }
}

export async function bindElInput(element, text, invoked = () => {}, {
  readControl,
  bindRangeInput,
  currentPageOwner,
}) {
  if (!isElAvailable(element) || element.readOnly || readControl(element)?.kind !== 'query' ||
      !element.matches('input:not([type="password"]):not([type="file"]),textarea')) return false
  if (isElDateOrDatetimeRangeInput(element)) {
    return bindRangeInput(element, text, invoked)
  }
  if (element.getAttribute('role') === 'combobox') return false
  const nodes = collectLiveVNodes(currentPageOwner()).filter(node => typeof node.type === 'string' && node.el === element
    && typeof node.props?.onInput === 'function')
  if (nodes.length !== 1) return false
  const node = nodes[0]
  try { invoked() } catch { /* Diagnostic collection is not part of the input. */ }
  element.value = text
  const event = new Event('input', {bubbles: false})
  Object.defineProperties(event, {target: {value: element}, currentTarget: {value: element}})
  await node.props.onInput(event)
  if (typeof node.props.onChange === 'function') await node.props.onChange(event)
  await Promise.resolve()
  return true
}

export async function bindElClick(element, invoked = () => {}, {
  readControl,
  readRow,
  currentPageOwner,
}) {
  if (!isElAvailable(element)) return false
  const query = readControl(element)
  const row = readRow(element)
  const list = element.closest('[role="listbox"]')
  const owners = list?.id ? [...document.querySelectorAll('input[role="combobox"][aria-controls]')]
    .filter(input => input.getAttribute('aria-controls') === list.id && isElAvailable(input) &&
      ['query', 'page_size'].includes(readControl(input)?.kind)) : []
  const family = element.closest('[role="tab"],.el-pagination') || query?.kind === 'query'
    || row && element.matches('button,a') || element.getAttribute('role') === 'option' && owners.length === 1
  // Modal editors, business controls and unclassified structures retain the
  // ordinary path; binding cannot widen their server-side admission.
  if (!family || element.closest('[role="dialog"],dialog')) return false
  const nodes = collectLiveVNodes(currentPageOwner()).filter(node => typeof node.type === 'string' &&
    node.el instanceof HTMLElement && (node.el === element || node.el.contains(element)) &&
    typeof node.props?.onClick === 'function')
    .sort((a, b) => a.el === b.el ? 0 : a.el.contains(b.el) ? 1 : -1)
  if (!nodes.length || nodes.length > 1 && nodes[0].el === nodes[1].el) return false
  const node = nodes[0]
  const event = new MouseEvent('click', {bubbles: false, cancelable: true})
  Object.defineProperties(event, {target: {value: element}, currentTarget: {value: node.el}})
  try { invoked() } catch { /* Diagnostic collection is not part of the click. */ }
  await node.props.onClick(event)
  // A callback may navigate; never replay it when arrival is still pending.
  await Promise.resolve()
  return true
}

/** The open Element Plus message box, if one is on the page. */
export function readElConfirmBox(doc = document) {
  const box = Array.from(doc.querySelectorAll(CONFIRM_BOX)).find(isElRendered)
  if (!box) return {open: false}
  return {
    open: true,
    title: (box.querySelector('.el-message-box__title')?.textContent ?? '').trim(),
    text: (box.querySelector('.el-message-box__message')?.textContent ?? '').trim(),
    buttons: Array.from(box.querySelectorAll('.el-message-box__btns button'))
      .map(button => (button.textContent ?? '').trim()).filter(Boolean),
  }
}

/** Press one button of the open Element Plus message box by its exact label; false if not offered. */
export function pressElConfirmButton(label, doc = document) {
  const box = Array.from(doc.querySelectorAll(CONFIRM_BOX)).find(isElRendered)
  const matches = Array.from(box?.querySelectorAll('.el-message-box__btns button') ?? [])
    .filter(button => (button.textContent ?? '').replace(/\s+/g, ' ').trim() === label && !button.disabled)
  if (matches.length !== 1) return false
  matches[0].click()
  return true
}

export function hasElBusyIndicator(doc = document) {
  return Array.from(doc.querySelectorAll(BUSY_SELECTORS)).some(isElRendered)
}

function discoverCspNonce(doc) {
  const withNonce = doc.querySelector?.('style[nonce], script[nonce]')
  const attr = withNonce?.nonce || withNonce?.getAttribute?.('nonce')
  if (attr) return attr
  return doc.querySelector?.('meta[property="csp-nonce"], meta[name="csp-nonce"]')?.getAttribute?.('content') || ''
}

/** Keep Element Plus fixed overlays/drawers clear of the side-by-side assistant drawer. */
export function installElOverlayOffset(doc = document, {styleId = 'woa-el-overlay-offset'} = {}) {
  if (!doc || !doc.documentElement) return () => {}
  let entry = overlayOffsetOwners.get(doc)
  if (!entry) {
    let sheet = null
    let styleEl = null
    const win = doc.defaultView
    const SheetCtor = win?.CSSStyleSheet ?? globalThis.CSSStyleSheet
    if (typeof SheetCtor === 'function' && Array.isArray(doc.adoptedStyleSheets)) {
      try {
        sheet = new SheetCtor()
        sheet.replaceSync(OVERLAY_OFFSET_CSS)
        doc.adoptedStyleSheets = [...doc.adoptedStyleSheets, sheet]
      } catch {
        sheet = null
      }
    }
    if (!sheet) {
      const existing = doc.getElementById?.(styleId)
      styleEl = existing || doc.createElement('style')
      styleEl.id = styleId
      const nonce = discoverCspNonce(doc)
      if (nonce) styleEl.nonce = nonce
      styleEl.textContent = OVERLAY_OFFSET_CSS
      if (!existing) (doc.head || doc.documentElement).appendChild(styleEl)
    }
    const teardown = () => {
      const current = overlayOffsetOwners.get(doc)
      if (!current) return
      overlayOffsetOwners.delete(doc)
      if (current.sheet && Array.isArray(doc.adoptedStyleSheets)) {
        doc.adoptedStyleSheets = doc.adoptedStyleSheets.filter(item => item !== current.sheet)
      }
      if (current.styleEl && current.styleEl.parentNode) {
        current.styleEl.parentNode.removeChild(current.styleEl)
      }
      if (win?.removeEventListener) {
        win.removeEventListener('pagehide', current.teardown)
        win.removeEventListener('unload', current.teardown)
      }
    }
    entry = {count: 0, sheet, styleEl, teardown}
    overlayOffsetOwners.set(doc, entry)
    if (win?.addEventListener) {
      win.addEventListener('pagehide', teardown, {once: true})
      win.addEventListener('unload', teardown, {once: true})
    }
  }
  entry.count += 1
  let released = false
  return () => {
    if (released) return
    released = true
    const current = overlayOffsetOwners.get(doc)
    if (!current) return
    current.count = Math.max(0, current.count - 1)
    if (current.count === 0) current.teardown()
  }
}

/** Verify an Element Plus form control and its enclosing dialog/drawer/form are open, visible, and writable. */
export function isElFormOpenAndWritable(element, {allowReadonlyCombobox = false} = {}) {
  if (!(element instanceof HTMLElement) || !element.isConnected) return false
  if (element.disabled || element.getAttribute('aria-disabled') === 'true' || element.classList.contains('is-disabled')) {
    return false
  }
  const isCombobox = element.getAttribute('role') === 'combobox'
  if (element.readOnly && !(allowReadonlyCombobox && isCombobox)) return false
  if (element.getAttribute('aria-readonly') === 'true' && !(allowReadonlyCombobox && isCombobox)) return false
  if (element.closest('.is-disabled, [aria-disabled="true"], [inert], [hidden], [aria-hidden="true"], fieldset:disabled')) {
    return false
  }
  const view = element.ownerDocument?.defaultView ?? window
  for (let node = element; node; node = node.parentElement) {
    const style = view.getComputedStyle(node)
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') {
      return false
    }
  }
  const dialog = element.closest('.el-dialog, .el-drawer, .el-overlay, [role="dialog"], dialog')
  if (dialog && dialog.tagName === 'DIALOG' && !dialog.hasAttribute('open')) return false
  return true
}

/** Collect restricted password/file controls inside an Element Plus form/dialog root. */
export function findElRestrictedControls(root) {
  if (!(root instanceof Element) || !root.isConnected) return []
  const out = []
  const seen = new Set()
  for (const input of root.querySelectorAll('input[type="password"], input[type="file"]')) {
    if (!(input instanceof HTMLInputElement) || !input.isConnected || input.disabled) continue
    const kind = input.type.toLowerCase() === 'file' ? 'file' : 'password'
    const formItem = input.closest('.el-form-item')
    const itemLabel = formItem?.querySelector('.el-form-item__label')?.textContent?.replace(/\s+/g, ' ').trim()
    const explicitLabel = itemLabel ||
      (input.labels?.[0]?.textContent || '').replace(/\s+/g, ' ').trim() ||
      (input.getAttribute('aria-label') || '').trim() ||
      (input.getAttribute('placeholder') || '').trim() ||
      (input.name || '').trim() ||
      (kind === 'file' ? '文件上传' : '密码')
    const uploadWrap = kind === 'file' ? input.closest('.el-upload') : null
    const focusTarget = (uploadWrap && uploadWrap instanceof HTMLElement ? uploadWrap : input)
    if (seen.has(input)) continue
    seen.add(input)
    out.push({label: explicitLabel.slice(0, 120), kind, element: input, focusTarget})
  }
  return out
}

/** Smooth-scroll and focus a restricted password/file control (or its `.el-upload` trigger). */
export function focusElRestrictedControl(target) {
  if (!(target instanceof HTMLElement) || !target.isConnected) return false
  const focusable = target.matches('input[type="file"]')
    ? (target.closest('.el-upload')?.querySelector('button,[tabindex]') ?? target.closest('.el-upload') ?? target)
    : target
  if (!(focusable instanceof HTMLElement) || !focusable.isConnected) return false
  focusable.scrollIntoView?.({behavior: 'smooth', block: 'center', inline: 'nearest'})
  if (!focusable.matches('input,button,textarea,select,a,[tabindex]')) {
    focusable.setAttribute('tabindex', '-1')
  }
  focusable.focus?.({preventScroll: true})
  return true
}
