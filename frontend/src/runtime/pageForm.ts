/**
 * Form interaction, verification, and model-reading helpers for `Page`.
 *
 * Browser-only identities of up to twenty entered controls are held locally so
 * handoff verification can confirm live controls without sending values back.
 */
import browserContract from '@contract/browser_contract.json'
import {
  ChoiceUnavailable, isComboboxClearedWithoutSelection, isTreeItemDisabled, listboxCombobox,
  matchingTreeChoices, optionText, ownedListbox, ownedTree, ownTreeExpandToggle, ownTreeOption,
  ownTreeRow, probeComboboxOptions, selectedComboboxOption, selectedOption,
  treeSelectionMatches, visibleComboboxOptions, visibleElement,
} from './combobox'
import {sleep} from './contain'
import {formInstance, localChoiceRemoval} from './formInstance'
import {
  normalizeFormRead, normalizeRowRead, unreadableReason,
  type FormModelRead, type RowRecordRead,
} from './formModel'
import {canonicalUrl} from './navigation'
import {
  formControlLabel, formControlScope, formFieldName, isSensitiveFormControl,
  refIndex, type ElementLike,
} from './pageModel'
import {radioChoices, radioEnabled, radioGroup, radioLabel, radioSelectionMatches} from './radio'
import type {Redactor} from './redaction'
import type {ElementRef} from './types'

export interface FormHandoffField {
  ref?: string
  field: string
  control: string
  value: string
  binding?: string
  label?: string
  scope?: string
  instance?: string
  option_label?: string
}

export interface FormSubmitTarget {
  ref: string
  label: string
  scope: string
}

export type ElementReadFn = (element: ElementLike | null | undefined) => unknown

interface FormControllerBridge {
  readState(): Promise<{content: string}>
  lookupIndex(index: number): ElementLike | null | undefined
  clickIndex(index: number): Promise<unknown>
  inputIndex(index: number, text: string): Promise<unknown>
  selectIndex(index: number, label: string): Promise<unknown>
  scrollIndex(options: {down: boolean; numPages: number; pixels?: number; index?: number}): Promise<unknown>
}

export function formFieldMatches(element: ElementRef, binding: FormHandoffField): boolean {
  return element.role === binding.control && (!binding.scope ||
    element.attrs.form_scope === binding.scope) && (binding.binding === 'ref'
    ? element.ref === binding.ref && element.attrs.form_instance === binding.instance
    : binding.binding === 'label'
      ? !element.attrs.field && element.attrs.field_label === binding.label
      : element.attrs.field === binding.field)
}

function numericValue(text: string): string | undefined {
  const value = text.trim()
  const match = /^([+-]?)(?:(\d+)(?:\.(\d*))?|\.(\d+))(?:[eE]([+-]?\d+))?$/.exec(value)
  if (!match || !Number.isFinite(Number(value))) return undefined
  const fraction = match[3] ?? match[4] ?? ''
  const digits = ((match[2] ?? '') + fraction).replace(/^0+/, '')
  if (!digits) return '0'
  const significant = digits.replace(/0+$/, '')
  const exponent = Number(match[5] ?? 0) - fraction.length + digits.length - significant.length
  return `${match[1] === '-' ? '-' : ''}${significant}e${exponent}`
}

/** The nearest ancestor that scrolls the option, or the menu itself. */
function scrollViewport(option: Element, list: Element): Element {
  let node: Element | null = option.parentElement
  for (let depth = 0; node && depth < 10; depth++, node = node.parentElement) {
    if (/(auto|scroll|overlay)/.test(getComputedStyle(node).overflowY) &&
        node.scrollHeight > node.clientHeight) return node
    if (node === document.body) break
  }
  return list
}

export class EnteredControlTracker {
  // Browser-only identities of at most twenty controls this Page actually entered.
  // No requested values, plans, permissions or persisted state are held here.
  private readonly enteredControls = new Map<string, {element: Element; instance: string; url: string}>()

  private enteredKey(field: string, label: string, scope: string): string {
    return JSON.stringify([field, label, scope])
  }

  remember(element: ElementLike, redactor: Redactor): void {
    if (!(element instanceof Element) || isSensitiveFormControl(element, redactor)) return
    const field = formFieldName(element) || '', label = field ? '' : formControlLabel(element) || ''
    const scope = formControlScope(element), instance = formInstance(element)
    if ((!field && !label) || !scope || !instance) return
    const held = this.enteredControls.values().next().value
    if (held && (held.instance !== instance || held.url !== canonicalUrl(location.href))) {
      this.enteredControls.clear()
    }
    const key = this.enteredKey(field, label, scope)
    const prior = this.enteredControls.get(key)
    if (prior && prior.element !== element && prior.element.isConnected) {
      this.enteredControls.delete(key)
      return
    }
    if (!prior && this.enteredControls.size >= 20) this.enteredControls.clear()
    this.enteredControls.set(key, {element, instance, url: canonicalUrl(location.href)})
  }

  find(binding: FormHandoffField, observed: ElementRef[]): ElementLike | null {
    if (!binding.scope) return null
    const key = this.enteredKey(binding.binding === 'label' ? '' : binding.field,
      binding.binding === 'label' ? binding.label || '' : '', binding.scope)
    const entry = this.enteredControls.get(key)
    if (!entry || entry.url !== canonicalUrl(location.href) || !entry.element.isConnected ||
        formInstance(entry.element) !== entry.instance || !observed.some(e =>
          e.attrs.form_scope === binding.scope && e.attrs.form_instance === entry.instance)) return null
    const rect = entry.element.getBoundingClientRect(), style = getComputedStyle(entry.element)
    if (!rect.width || !rect.height || style.display === 'none' || style.visibility === 'hidden' ||
        entry.element.closest('[hidden],[aria-hidden="true"]')) return null
    return entry.element as ElementLike
  }

  elementsForInstance(instance: string): Element[] {
    return [...this.enteredControls.values()]
      .filter(entry => entry.instance === instance)
      .map(entry => entry.element)
  }
}

export function readPageFormModel(
  element: ElementLike | null | undefined,
  formModelReader: ElementReadFn | undefined,
  sourceAccepter: ((source: string, kind: 'form' | 'row') => boolean) | undefined,
): {read: FormModelRead | null; unreadable: string} {
  try {
    if (!(element instanceof HTMLElement) || !element.isConnected || !formModelReader) {
      return {read: null, unreadable: 'no_reader'}
    }
    const raw = formModelReader(element)
    const read = normalizeFormRead(raw, {acceptReadSource: sourceAccepter})
    if (read && JSON.stringify(read).length <= browserContract.form_model.max_bytes) {
      return {read, unreadable: ''}
    }
    return {read: null, unreadable: unreadableReason(raw) || 'not_a_form'}
  } catch {
    return {read: null, unreadable: 'reader_failed'}
  }
}

export function readPageRowRecord(
  element: ElementLike | null | undefined,
  rowRecordReader: ElementReadFn | undefined,
  sourceAccepter: ((source: string, kind: 'form' | 'row') => boolean) | undefined,
): RowRecordRead | null {
  try {
    if (!(element instanceof HTMLElement) || !element.isConnected || !rowRecordReader) return null
    return normalizeRowRead(rowRecordReader(element), {acceptReadSource: sourceAccepter})
  } catch {
    return null
  }
}

/** Structured error when a target form, dialog, or control is closed, detached, or read-only. */
export class FormClosedOrReadonlyError extends Error {
  readonly code = 'form_closed_or_readonly'
  constructor(message: string) {
    super(message)
  }
}

/** Verify that a target control and its enclosing form/dialog are open, connected, visible, and writable. */
export function ensureFormWritableAndOpen(
  element: ElementLike | null | undefined,
  actionLabel: string,
  allowReadonlyCombobox = false,
): asserts element is ElementLike {
  if (!element || element.isConnected === false) {
    throw new FormClosedOrReadonlyError(`form_closed_or_readonly: ${actionLabel} target is no longer on the page`)
  }
  const combobox = allowReadonlyCombobox && listboxCombobox(element)
  const ariaDisabled = element.getAttribute?.('aria-disabled') === 'true'
  const ariaReadonly = element.getAttribute?.('aria-readonly') === 'true'
  if (element.disabled || ariaDisabled || (!combobox && (element.readOnly || ariaReadonly))) {
    throw new FormClosedOrReadonlyError(`form_closed_or_readonly: ${actionLabel} target is disabled or read-only`)
  }
  if (typeof Element !== 'undefined' && element instanceof Element) {
    if (element.classList.contains('is-disabled') ||
        element.closest('[hidden],[aria-hidden="true"],[inert],fieldset:disabled,.is-disabled,[aria-disabled="true"]')) {
      throw new FormClosedOrReadonlyError(`form_closed_or_readonly: ${actionLabel} target form is closed or disabled`)
    }
    const dialog = element.closest('[role="dialog"],dialog')
    if (dialog && (!dialog.isConnected || (dialog.tagName === 'DIALOG' && !dialog.hasAttribute('open')))) {
      throw new FormClosedOrReadonlyError(`form_closed_or_readonly: ${actionLabel} target dialog is closed`)
    }
    const view = element.ownerDocument?.defaultView ?? (typeof window !== 'undefined' ? window : null)
    if (view?.getComputedStyle) {
      for (let node: Element | null = element; node; node = node.parentElement) {
        const style = view.getComputedStyle(node)
        if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') {
          throw new FormClosedOrReadonlyError(`form_closed_or_readonly: ${actionLabel} target form or dialog is hidden`)
        }
      }
    }
  }
}

function captureFormDraft(scope: string, instance: string): Array<{node: Element; state: string}> {
  if (!scope || !instance || typeof document === 'undefined') return []
  return [...document.querySelectorAll('input,textarea,select,[role="radiogroup"],[role="textbox"],[contenteditable="true"]')]
    .filter(node => formControlScope(node) === scope && formInstance(node) === instance)
    .map(node => ({node, state: JSON.stringify([
      (node as HTMLInputElement).value ?? '', (node as HTMLInputElement).checked ?? false,
      node.matches('[contenteditable="true"],[role="textbox"]') ? node.textContent : null,
      node.getAttribute('aria-checked'), node.getAttribute('aria-selected'),
      node instanceof HTMLInputElement && listboxCombobox(node) ? selectedComboboxOption(node) : null,
    ])}))
}

function draftsMatch(
  beforeDraft: Array<{node: Element; state: string}>,
  afterDraft: Array<{node: Element; state: string}>,
): boolean {
  return beforeDraft.length === afterDraft.length &&
    beforeDraft.every((item, i) => item.node === afterDraft[i]!.node && item.state === afterDraft[i]!.state)
}

export function inputFormControl(
  ref: unknown,
  element: ElementLike | null | undefined,
  text: string,
  commit: boolean,
  bridge: FormControllerBridge,
  entered: EnteredControlTracker,
  redactor: Redactor,
): Promise<unknown> {
  ensureFormWritableAndOpen(element, 'fill', false)
  const isCombo = listboxCombobox(element)
  const comboScope = isCombo ? formControlScope(element) : null
  const comboInstance = isCombo ? formInstance(element) : null
  const beforeDraft = isCombo && comboScope && comboInstance
    ? captureFormDraft(comboScope, comboInstance) : []
  return Promise.resolve(bridge.inputIndex(refIndex(ref), text)).then(async result => {
    if (result && (result as {success?: unknown}).success === true) {
      ensureFormWritableAndOpen(element, 'fill', false)
      const candidatesWhileOpen = isCombo
        ? visibleComboboxOptions(element, browserContract.choice_failure.max_candidates,
          browserContract.choice_failure.max_label_chars)
        : []
      // An application-issued form draft commits the local editor before its fresh
      // observation. Ordinary typing keeps suggestion lists open. No submit is used.
      if (commit && (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement)) {
        ensureFormWritableAndOpen(element, 'fill', false)
        element.dispatchEvent(new Event('change', {bubbles: true}))
        element.blur()
        await Promise.resolve()
      }
      if (isCombo && text.trim() !== '' && isComboboxClearedWithoutSelection(element)) {
        const candidatesAfter = visibleComboboxOptions(element, browserContract.choice_failure.max_candidates,
          browserContract.choice_failure.max_label_chars)
        const candidate_labels = candidatesAfter.length ? candidatesAfter : candidatesWhileOpen
        const afterDraft = comboScope && comboInstance
          ? captureFormDraft(comboScope, comboInstance) : []
        const bound = Boolean(comboScope && comboInstance &&
          formControlScope(element) === comboScope && formInstance(element) === comboInstance)
        const unchanged = bound && draftsMatch(beforeDraft, afterDraft)
        throw new ChoiceUnavailable(candidate_labels, unchanged,
          !bound ? 'the form binding changed' : 'local field values changed')
      }
      entered.remember(element, redactor)
    }
    return result
  })
}

export function selectFormControl(
  ref: unknown,
  element: ElementLike | null | undefined,
  text: string,
  optionLabel: string | undefined,
  bridge: FormControllerBridge,
  entered: EnteredControlTracker,
  redactor: Redactor,
  interrupted: () => boolean,
): Promise<unknown> {
  if (!element || element.isConnected === false) {
    throw new FormClosedOrReadonlyError('form_closed_or_readonly: select target is no longer on the page')
  }
  if (optionLabel !== undefined) {
    const group = radioGroup(element)
    if (group) {
      ensureFormWritableAndOpen(group, 'select', false)
      return selectRadioChoice(group, optionLabel, text, bridge, entered, redactor, interrupted)
    }
    if (!listboxCombobox(element) || !optionLabel.trim() ||
        optionLabel.length > browserContract.form_handoff.max_label_chars ||
        isSensitiveFormControl(element, redactor)) {
      throw new Error('bad_arguments: select requires one enabled single-choice combobox')
    }
    ensureFormWritableAndOpen(element, 'select', true)
    return selectComboboxChoice(refIndex(ref), element, optionLabel, bridge, entered, redactor, interrupted)
  }
  ensureFormWritableAndOpen(element, 'select', false)
  if (String(element.tagName || '').toLowerCase() !== 'select' || !element.options) {
    throw new Error('bad_arguments: select target is not a native select')
  }
  // The application contract uses stable option values. PageController 1.12.4 selects
  // by visible option text, so resolve that text from the same live element immediately
  // before dispatch. Require one value and one label: duplicate labels would let its
  // first-match behavior choose a different application value.
  const options = Array.from(element.options)
  const matching = options.filter(option => String(option.value ?? '') === text)
  const option = matching[0]
  if (matching.length !== 1 || !option || option.disabled) {
    throw new Error('bad_arguments: select value is absent, ambiguous, or disabled')
  }
  const label = String(option.textContent ?? '').trim()
  if (!label || options.filter(item =>
    String(item.textContent ?? '').trim() === label).length !== 1) {
    throw new Error('bad_arguments: select label is absent or ambiguous')
  }
  return Promise.resolve(bridge.selectIndex(refIndex(ref), label)).then(result => {
    if (result && (result as {success?: unknown}).success === true) {
      ensureFormWritableAndOpen(element, 'select', false)
      entered.remember(element, redactor)
    }
    return result
  })
}

async function selectRadioChoice(
  group: HTMLElement,
  label: string,
  value: string,
  bridge: FormControllerBridge,
  entered: EnteredControlTracker,
  redactor: Redactor,
  interrupted: () => boolean,
): Promise<unknown> {
  const scope = formControlScope(group), instance = formInstance(group), url = location.href
  const name = formFieldName(group), fieldLabel = formControlLabel(group)
  const options = radioChoices(group), matches = options.filter(e => radioLabel(e) === label)
  const current = () => !interrupted() && location.href === url && radioGroup(group) === group &&
    formControlScope(group) === scope && formInstance(group) === instance &&
    formFieldName(group) === name && formControlLabel(group) === fieldLabel &&
    radioChoices(group).length === options.length && radioChoices(group).every((e, i) => e === options[i]) &&
    radioChoices(group).filter(e => radioLabel(e) === label).length === 1 &&
    radioLabel(matches[0]!) === label &&
    (!(matches[0] instanceof HTMLInputElement) || matches[0].value === value)
  if (!scope || !instance || !label.trim() || label.length > browserContract.form_handoff.max_label_chars ||
      isSensitiveFormControl(group, redactor) || matches.length !== 1 ||
      !radioEnabled(matches[0]!) || matches[0] instanceof HTMLInputElement && matches[0].value !== value ||
      !visibleElement(group) || !current()) {
    throw new Error('bad_arguments: radio choice is absent, ambiguous, unavailable, or changed')
  }
  if (radioSelectionMatches(group, label, value)) {
    entered.remember(group, redactor)
    return {success: true}
  }
  const state = await bridge.readState()
  const indexes = [...state.content.matchAll(/(?:^|\n)\s*\*?\[(\d+)]</g)].map(m => Number(m[1]))
    .filter(i => bridge.lookupIndex(i) === matches[0])
  if (indexes.length !== 1 || !current() || !radioEnabled(matches[0]!)) {
    throw new Error('execution_failed: radio choice has no unique fresh action reference')
  }
  const finishRemoval = localChoiceRemoval(group, entered.elementsForInstance(instance))
  try {
    ensureFormWritableAndOpen(group, 'select', false)
    const result = await bridge.clickIndex(indexes[0]!)
    if (!result || (result as {success?: unknown}).success !== true) return result
    await sleep(50)
    ensureFormWritableAndOpen(group, 'select', false)
    if (!current() || !radioSelectionMatches(group, label, value)) {
      throw new Error('execution_failed: radio selection was not verified')
    }
    entered.remember(group, redactor)
    return result
  } finally {
    finishRemoval()
  }
}

async function selectComboboxChoice(
  index: number,
  element: HTMLInputElement,
  label: string,
  bridge: FormControllerBridge,
  entered: EnteredControlTracker,
  redactor: Redactor,
  interrupted: () => boolean,
): Promise<unknown> {
  const scope = formControlScope(element), instance = formInstance(element), url = location.href
  const labelBefore = formControlLabel(element), fieldBefore = formFieldName(element)
  const current = () => !interrupted() && location.href === url && element.isConnected &&
    listboxCombobox(element) && formControlScope(element) === scope && formInstance(element) === instance &&
    formControlLabel(element) === labelBefore && formFieldName(element) === fieldBefore
  const tree = ownedTree(element)
  const list = tree ? null : ownedListbox(element)
  const popup = tree ?? list
  if (!popup || !scope || !instance || !current()) {
    throw new Error('bad_arguments: combobox has no unique current form listbox')
  }
  const matchesCurrent = () => (tree ? treeSelectionMatches(element, label) : selectedOption(element) === label)
  // Keep values browser-local. Equality of this complete local draft, together
  // with the fact no option was clicked, is stronger than an unchanged label.
  const draft = () => [...document.querySelectorAll('input,textarea,select,[role="radiogroup"],[role="textbox"],[contenteditable="true"]')]
    .filter(node => formControlScope(node) === scope && formInstance(node) === instance)
    .map(node => ({node, state: JSON.stringify([
      (node as HTMLInputElement).value ?? '', (node as HTMLInputElement).checked ?? false,
      node.matches('[contenteditable="true"],[role="textbox"]') ? node.textContent : null,
      node.getAttribute('aria-checked'), node.getAttribute('aria-selected'),
      node instanceof HTMLInputElement && listboxCombobox(node) ? selectedComboboxOption(node) : null,
    ])}))
  const beforeDraft = draft()
  if (matchesCurrent()) {
    entered.remember(element, redactor)
    return {success: true}
  }
  if (element.getAttribute('aria-expanded') !== 'true') {
    const opened = await bridge.clickIndex(index)
    if (!opened || (opened as {success?: unknown}).success !== true) return opened
  }
  // What the last scroll was asked for over what it moved: PageController scales the
  // distance it is given, by a factor that is its own.
  let gain = 1
  // Eight moves may make the option visible only after the last move. Inspect
  // that result once more; the final inspection may choose, but never scrolls.
  let probedEmpty = false
  for (let attempt = 0; attempt <= 8; attempt++) {
    ensureFormWritableAndOpen(element, 'select', true)
    let state = await bridge.readState()
    ensureFormWritableAndOpen(element, 'select', true)
    if (!current() || (tree ? ownedTree(element) !== tree : ownedListbox(element) !== list)) {
      throw new Error('execution_failed: combobox changed before selection')
    }
    if (visibleElement(popup)) {
      if (!probedEmpty && visibleComboboxOptions(element, 1).length === 0) {
        probedEmpty = true
        const populated = await probeComboboxOptions(element, {
          timeoutMs: 250, intervalMs: 50, interrupted,
        })
        ensureFormWritableAndOpen(element, 'select', true)
        if (!current() || (tree ? ownedTree(element) !== tree : ownedListbox(element) !== list)) {
          throw new Error('execution_failed: combobox changed before selection')
        }
        if (populated) {
          state = await bridge.readState()
        }
      }
      const matchesForPopup = () => (list
        ? [...list.querySelectorAll('[role="option"]')].filter(option =>
          option.closest('[role="listbox"]') === list && optionText(option) === label)
        : matchingTreeChoices(tree!, label).map(c => c.item))
      const matches = matchesForPopup()
      if (tree && attempt < 8 && (matches.length === 0 || (matches.length === 1 && !visibleElement(matches[0]!)))) {
        let expandItem: Element | undefined
        if (matches.length === 1) {
          // render-after-expand="false": target exists in DOM under collapsed ancestor(s).
          let ancestor = matches[0]!.parentElement?.closest('[role="treeitem"]') ?? null
          while (ancestor && tree.contains(ancestor)) {
            if (ancestor.getAttribute('aria-expanded') === 'false') expandItem = ancestor
            ancestor = ancestor.parentElement?.closest('[role="treeitem"]') ?? null
          }
        } else if (label.includes('/')) {
          const parts = label.split('/').map(s => s.trim()).filter(Boolean)
          for (let depth = 1; depth < parts.length; depth++) {
            const prefix = parts.slice(0, depth).join(' / ')
            const found = matchingTreeChoices(tree, prefix)
            if (found.length === 1 && found[0]!.item.getAttribute('aria-expanded') === 'false') {
              expandItem = found[0]!.item
              break
            }
          }
        }
        if (expandItem && visibleElement(expandItem)) {
          const caret = ownTreeExpandToggle(expandItem)
          if (caret instanceof HTMLElement && visibleElement(caret)) {
            caret.click()
            await sleep(80)
            continue
          }
          if (!ownTreeOption(expandItem)) {
            const indexed = [...state.content.matchAll(/(?:^|\n)\s*\*?\[(\d+)]</g)].map(m => Number(m[1]))
            const expIdx = indexed.find(i => {
              const n = bridge.lookupIndex(i)
              return n === expandItem || n === ownTreeRow(expandItem!)
            })
            if (expIdx !== undefined) {
              await bridge.clickIndex(expIdx)
              await sleep(80)
              continue
            }
          }
        }
      }
      if (matches.length === 0) {
        const candidate_labels = visibleComboboxOptions(
          element,
          browserContract.choice_failure.max_candidates,
          browserContract.choice_failure.max_label_chars,
        )
        // Try the current control to dismiss the list. Some widgets only open on
        // input clicks: an open popup alone is not a changed draft or selection.
        if (element.getAttribute('aria-expanded') === 'true') {
          const controls = [...state.content.matchAll(/(?:^|\n)\s*\*?\[(\d+)]</g)]
            .map(m => Number(m[1])).filter(i => bridge.lookupIndex(i) === element)
          // An open popup may remove its input from the controller's viewport
          // indexes. Dismiss only with a unique fresh ref; no selection was
          // made, so the unchanged-draft proof below does not require closing.
          if (controls.length === 1 && current()) {
            await bridge.clickIndex(controls[0]!)
            await sleep(50)
          }
        }
        const afterDraft = draft()
        const bound = current() && (tree ? ownedTree(element) === tree : ownedListbox(element) === list)
        const unchanged = draftsMatch(beforeDraft, afterDraft)
        throw new ChoiceUnavailable(candidate_labels, bound && unchanged,
          !bound ? 'the form binding changed' : 'local field values changed')
      }
      const innerOption = tree && matches[0] ? ownTreeOption(matches[0]) : null
      const optionDisabled = tree && matches[0]
        ? isTreeItemDisabled(matches[0])
        : (matches[0]!.getAttribute('aria-disabled') === 'true' || matches[0]!.hasAttribute('disabled'))
      if (matches.length !== 1 || !visibleElement(matches[0]!) || optionDisabled) {
        throw new Error('execution_failed: combobox option is absent, ambiguous, or disabled')
      }
      const option = matches[0]!
      const hitArea = tree ? ownTreeRow(option) ?? option : option
      const indexed = [...state.content.matchAll(/(?:^|\n)\s*\*?\[(\d+)]</g)].map(m => Number(m[1]))
      const exactIndexes = indexed.filter(i => {
        const n = bridge.lookupIndex(i)
        return n === innerOption || n === hitArea || n === option
      })
      let indexes = exactIndexes
      if (indexes.length === 0) {
        const ownerRole = tree ? '[role="treeitem"]' : '[role="option"]'
        const descendants = indexed.filter(i => {
          const n = bridge.lookupIndex(i)
          return n instanceof Element && hitArea.contains(n) &&
            n.closest(ownerRole) === option && visibleElement(n)
        })
        if (descendants.length <= 1) {
          indexes = descendants
        } else {
          const targetText = optionText(innerOption ?? option)
          const textMatches = descendants.filter(i => {
            const n = bridge.lookupIndex(i)
            if (!(n instanceof Element) || n.getAttribute('aria-expanded') !== null) return false
            const text = optionText(n)
            return text.length > 0 && text === targetText
          })
          const pool = textMatches.length ? textMatches : descendants.filter(i => {
            const n = bridge.lookupIndex(i)
            return n instanceof Element && optionText(n).length > 0
          })
          indexes = pool.filter(i => {
            const n = bridge.lookupIndex(i)
            return n instanceof Element && !pool.some(other => {
              if (other === i) return false
              const otherEl = bridge.lookupIndex(other)
              return otherEl instanceof Element && n.contains(otherEl)
            })
          })
        }
      }
      if (indexes.length === 0) {
        if (attempt === 8) break
        // Reuse PageController's scoped scroll. Anchor it to an indexed option in
        // this owned menu; never scroll an unrelated popup or choose an old index.
        const anchors = indexed.filter(i => {
          const node = bridge.lookupIndex(i)
          return node instanceof Element &&
            (list
              ? node.getAttribute('role') === 'option' && node.closest('[role="listbox"]') === list
              : tree!.contains(node) && (node.getAttribute('role') === 'treeitem' ||
                node.getAttribute('role') === 'option' || Boolean(node.closest('[role="treeitem"]')))) &&
            visibleElement(node)
        })
        if (!anchors.length || !current()) {
          throw new Error('execution_failed: combobox menu has no current scroll target')
        }
        // Ask for the distance that brings the option into the menu's visible part, so
        // a long menu is reached in a few scrolls rather than a page at a time.
        const view = scrollViewport(option, popup).getBoundingClientRect()
        const before = option.getBoundingClientRect()
        const down = before.top >= view.top
        const away = down ? before.bottom - view.bottom : view.top - before.top
        const pixels = Math.min(400_000, Math.max(40, Math.ceil(
          (Math.max(away, 0) + Math.min(view.height / 2, 80)) * gain)))
        const scrolled = await bridge.scrollIndex({down, numPages: 1, pixels, index: anchors[0]!})
        if (!scrolled || (scrolled as {success?: unknown}).success !== true) return scrolled
        await sleep(50)
        const moved = Math.abs(option.getBoundingClientRect().top - before.top)
        if (moved > 0.5) gain = Math.min(16, Math.max(1, pixels / moved))
        continue
      }
      if (indexes.length !== 1 || !current()) {
        throw new Error('execution_failed: combobox option is not uniquely actionable')
      }
      const openCandidatesBeforeClick = visibleComboboxOptions(
        element,
        browserContract.choice_failure.max_candidates,
        browserContract.choice_failure.max_label_chars,
      )
      const result = await bridge.clickIndex(indexes[0]!)
      if (!result || (result as {success?: unknown}).success !== true) return result
      await sleep(50)
      ensureFormWritableAndOpen(element, 'select', true)
      if (!current() || (tree ? ownedTree(element) !== tree : ownedListbox(element) !== list) || !matchesCurrent()) {
        if (isComboboxClearedWithoutSelection(element)) {
          const candidatesAfter = visibleComboboxOptions(
            element,
            browserContract.choice_failure.max_candidates,
            browserContract.choice_failure.max_label_chars,
          )
          const afterDraft = draft()
          const bound = current() && (tree ? ownedTree(element) === tree : ownedListbox(element) === list)
          throw new ChoiceUnavailable(
            candidatesAfter.length ? candidatesAfter : openCandidatesBeforeClick,
            bound && draftsMatch(beforeDraft, afterDraft),
            !bound ? 'the form binding changed' : 'local field values changed',
          )
        }
        throw new Error('execution_failed: combobox selection was not verified')
      }
      entered.remember(element, redactor)
      return result
    }
    await sleep(50)
  }
  throw new Error('execution_failed: combobox option did not become actionable')
}

/** Check an adapter-issued draft against live controls without sending their values back. */
export function verifyFormFields(
  fields: unknown,
  observed: ElementRef[],
  lookupRef: (ref: string) => ElementLike | null | undefined,
  entered: EnteredControlTracker,
  redactor: Redactor,
): boolean {
  if (!Array.isArray(fields) || fields.length < 1 || fields.length > 20) return false
  const seen = new Set<string>()
  return fields.every((item: unknown) => {
    if (!item || typeof item !== 'object') return false
    const binding = item as FormHandoffField
    if ((binding.ref !== undefined && typeof binding.ref !== 'string') || typeof binding.field !== 'string' ||
        typeof binding.value !== 'string' ||
        !['field', 'label', 'ref'].includes(binding.binding || 'field') ||
        (binding.binding === 'label' && (!binding.label || !binding.scope)) ||
        (binding.binding === 'ref' && (!binding.ref || !binding.scope || !binding.instance)) ||
        !browserContract.form_handoff.input_controls.includes(binding.control) ||
        seen.has(binding.field)) return false
    seen.add(binding.field)
    const matches = observed.filter(element => formFieldMatches(element, binding))
    let live: ElementLike | null | undefined
    if (binding.ref) {
      if (matches.length !== 1 || matches[0]!.ref !== binding.ref) return false
      try { live = lookupRef(binding.ref) } catch { return false }
    } else {
      if (matches.length > 1) return false
      live = entered.find(binding, observed)
    }
    if (binding.control === 'radiogroup') live = radioGroup(live)
    if (!live || live.isConnected === false || live.disabled ||
        live.readOnly && !(binding.option_label && listboxCombobox(live))) return false
    if (binding.scope && formControlScope(live) !== binding.scope) return false
    if (isSensitiveFormControl(live, redactor)) return false
    if (binding.binding === 'label') {
      if (formFieldName(live) || formControlLabel(live) !== binding.label) return false
    } else if (binding.binding === 'ref') {
      if (!(live instanceof Element) || formInstance(live) !== binding.instance) return false
    } else if (formFieldName(live) !== binding.field) return false
    const tag = String(live.tagName || '').toLowerCase()
    const declared = live.getAttribute?.('role')
    const control = declared === 'radiogroup' ? 'radiogroup' : tag === 'select' ? 'select' : declared === 'combobox' ? 'combobox'
      : declared === 'spinbutton' || tag === 'input' && live.type === 'number' ? 'spinbutton' : 'textbox'
    if (!binding.ref && (control !== binding.control ||
        !['input', 'textarea', 'select'].includes(tag) && control !== 'radiogroup')) return false
    if (binding.control === 'select' && tag !== 'select') {
      return false
    }
    const shown = String(live.value ?? '')
    if (binding.option_label !== undefined) {
      if (binding.control === 'radiogroup') return typeof binding.option_label === 'string' &&
        binding.option_label.length <= browserContract.form_handoff.max_label_chars &&
        live instanceof HTMLElement && radioSelectionMatches(live, binding.option_label, binding.value)
      return typeof binding.option_label === 'string' && binding.control === 'combobox' &&
        binding.option_label.length <= browserContract.form_handoff.max_label_chars &&
        listboxCombobox(live) &&
        (ownedTree(live) ? treeSelectionMatches(live, binding.option_label) : selectedOption(live) === binding.option_label)
    }
    if (binding.control === 'spinbutton') {
      const expected = numericValue(binding.value)
      return expected !== undefined && numericValue(shown) === expected
    }
    return shown === binding.value
  })
}

/** Verify one visible server-owned submit target, including a disabled button.
 * Highlighting a partial draft never activates it or proves submission readiness.
 */
export function verifyFormSubmit(
  target: unknown,
  fields: unknown,
  observed: ElementRef[],
  lookupRef: (ref: string) => ElementLike | null | undefined,
): target is FormSubmitTarget {
  if (!target || typeof target !== 'object' || !Array.isArray(fields)) return false
  const candidate = target as FormSubmitTarget
  if (typeof candidate.ref !== 'string' || typeof candidate.label !== 'string' ||
      !candidate.label || candidate.label.length > browserContract.form_handoff.max_label_chars || !candidate.scope ||
      fields.some(field => !field || field.scope !== candidate.scope)) return false
  const matches = observed.filter(element => element.role === 'button' &&
    element.name === candidate.label && element.attrs.form_scope === candidate.scope)
  if (matches.length !== 1 || matches[0]!.ref !== candidate.ref) return false
  let live: ElementLike | null | undefined
  try { live = lookupRef(candidate.ref) } catch { return false }
  if (!(live instanceof HTMLElement) || !visibleElement(live) || ('readOnly' in live && live.readOnly === true) ||
      String(live.tagName || '').toLowerCase() !== 'button' ||
      formControlScope(live) !== candidate.scope) return false
  const label = String(live.textContent || '').replace(/\s+/g, ' ').trim()
  return label === candidate.label
}

/**
 * Verify that every live control targeted by a handoff still belongs to an open,
 * connected, visible, and writable form/dialog. Throws `FormClosedOrReadonlyError`
 * if any resolved target control or its form/dialog closed, detached, or became
 * readonly/disabled.
 */
export function ensureHandoffFormOpenAndWritable(
  fields: unknown,
  observed: ElementRef[],
  lookupRef: (ref: string) => ElementLike | null | undefined,
  entered: EnteredControlTracker,
): void {
  if (!Array.isArray(fields)) return
  for (const item of fields) {
    if (!item || typeof item !== 'object') continue
    const binding = item as FormHandoffField
    let live: ElementLike | null | undefined
    if (binding.ref) {
      const matches = observed.filter(element => formFieldMatches(element, binding))
      if (matches.length !== 1 || matches[0]!.ref !== binding.ref) continue
      try { live = lookupRef(binding.ref) } catch {
        throw new FormClosedOrReadonlyError('form_closed_or_readonly: handoff form field is detached')
      }
      if (!live || live.isConnected === false) {
        throw new FormClosedOrReadonlyError('form_closed_or_readonly: handoff form field is no longer on the page')
      }
    } else {
      live = entered.find(binding, observed)
      if (!live) continue
    }
    const target = binding.control === 'radiogroup' ? radioGroup(live) : live
    ensureFormWritableAndOpen(target, 'handoff', Boolean(binding.option_label && listboxCombobox(target)))
  }
}
