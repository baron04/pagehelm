/**
 * Turn-scoped form fill tracking, undo restoration, and restricted control focusing.
 *
 * Keeps first-seen pre-fill values (`previous_fields`), `applied_fields`, `rejected_fields`,
 * and `restricted_fields` (password/file controls never auto-filled) purely in browser memory
 * per turn while projecting redacted, bounded summaries onto `ActionReceipt`.
 */
import type {HandoffRestrictedControl} from '../contract/panel'
import browserContract from '@contract/browser_contract.json'
import {listboxCombobox, selectedComboboxOption} from './combobox'
import {formInstance} from './formInstance'
import type {FormModelRead} from './formModel'
import {ensureFormWritableAndOpen} from './pageForm'
import {
  explicitAttribute, formControlLabel, formControlScope, formFieldName,
  isSensitiveFormControl, type ElementLike,
} from './pageModel'
import {observedSelectedRadio, radioChoices, radioGroup, radioLabel, selectedRadio} from './radio'
import type {Redactor} from './redaction'
import type {JsonObject} from './types'

interface UndoFieldEntry {
  fieldKey: string
  element: Element
  instance: string | null
  scope: string | null
  previousValue: string
  previousChecked?: boolean
}

interface RestrictedEntry {
  label: string
  kind: 'password' | 'file'
  element: HTMLInputElement
}

interface TurnFillState {
  turnId: string
  formContainer: Element | null
  undone: boolean
  previousFields: Record<string, string>
  appliedFields: Record<string, string>
  rejectedFields: Record<string, {reason: string; candidates?: string[]}>
  restrictedFields: string[]
  restrictedEntries: RestrictedEntry[]
  undoEntries: Map<string, UndoFieldEntry>
}

export interface UndoFormOptions {
  restoreFormModel?: ((element: unknown, request: unknown) => Promise<boolean>) | undefined
  readFormModel?: ((element: ElementLike) => FormModelRead | null) | undefined
  onRestored?: (() => void) | undefined
}

const MAX_TRACKED_TURNS = 16

function isElementOpenAndVisible(node: Element): boolean {
  if (!node.isConnected) return false
  if (node.closest('[hidden],[aria-hidden="true"],[inert]')) return false
  const dialog = node.closest('[role="dialog"],dialog')
  if (dialog && (!dialog.isConnected || (dialog.tagName === 'DIALOG' && !dialog.hasAttribute('open')))) {
    return false
  }
  const view = node.ownerDocument?.defaultView ?? (typeof window !== 'undefined' ? window : null)
  if (view?.getComputedStyle) {
    for (let cur: Element | null = node; cur; cur = cur.parentElement) {
      const style = view.getComputedStyle(cur)
      if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') {
        return false
      }
    }
  }
  return true
}

function isFocusableAndVisible(node: Element): boolean {
  if (!isElementOpenAndVisible(node)) return false
  const rect = node.getBoundingClientRect()
  return rect.width > 0 && rect.height > 0
}

function findVisibleUploadTrigger(input: HTMLInputElement): HTMLElement | null {
  if (isFocusableAndVisible(input)) return input
  let container: Element | null = input.parentElement
  for (let depth = 0; container && depth < 3; depth++, container = container.parentElement) {
    for (const candidate of container.querySelectorAll<HTMLElement>('button, [role="button"], label, [tabindex]')) {
      if (candidate !== input && isFocusableAndVisible(candidate)) return candidate
    }
    if (container instanceof HTMLElement && isFocusableAndVisible(container)) return container
  }
  return null
}

function resolveRestrictedLabel(input: HTMLInputElement, kind: 'password' | 'file'): string {
  const direct = formControlLabel(input)
    || (input.labels?.[0]?.textContent || '').replace(/\s+/g, ' ').trim()
    || (explicitAttribute(input, 'aria-label') || '').trim()
    || (explicitAttribute(input, 'placeholder') || '').trim()
  if (direct) return direct.slice(0, browserContract.form_handoff.max_label_chars)
  const wrapperLabel = input.closest('[role="group"], fieldset, [class*="form-item"], tr, td')
    ?.querySelector('label, legend')?.textContent?.replace(/\s+/g, ' ').trim()
  if (wrapperLabel) return wrapperLabel.slice(0, browserContract.form_handoff.max_label_chars)
  const fieldName = (formFieldName(input) || input.name || '').trim()
  if (fieldName) return fieldName.slice(0, browserContract.form_handoff.max_label_chars)
  return kind === 'file' ? '文件上传' : '密码'
}

export function scanRestrictedControlsInContainer(container: Element | null | undefined): RestrictedEntry[] {
  if (!container || !container.isConnected) return []
  const out: RestrictedEntry[] = []
  const seen = new Set<HTMLInputElement>()
  for (const input of container.querySelectorAll<HTMLInputElement>('input[type="password"], input[type="file"]')) {
    if (!input.isConnected || input.disabled || seen.has(input)) continue
    seen.add(input)
    const kind = input.type.toLowerCase() === 'file' ? 'file' : 'password'
    out.push({
      label: resolveRestrictedLabel(input, kind),
      kind,
      element: input,
    })
    if (out.length >= browserContract.form_handoff.max_choice_options) break
  }
  return out
}

export function focusRestrictedElement(target: Element): boolean {
  if (!(target instanceof HTMLElement) || !target.isConnected) return false
  if (!isElementOpenAndVisible(target)) {
    const isHiddenFile = target instanceof HTMLInputElement && target.type.toLowerCase() === 'file'
    if (!isHiddenFile) return false
  }
  const focusTarget = target instanceof HTMLInputElement && target.type.toLowerCase() === 'file'
    ? (findVisibleUploadTrigger(target) ?? target)
    : target
  if (!focusTarget || !focusTarget.isConnected) return false
  const enclosingDialogOrForm = focusTarget.closest('[role="dialog"], dialog, form')
  if (enclosingDialogOrForm && !isElementOpenAndVisible(enclosingDialogOrForm)) return false
  focusTarget.scrollIntoView?.({behavior: 'smooth', block: 'center', inline: 'nearest'})
  if (!(focusTarget instanceof HTMLInputElement || focusTarget instanceof HTMLButtonElement ||
      focusTarget instanceof HTMLTextAreaElement || focusTarget instanceof HTMLSelectElement ||
      focusTarget instanceof HTMLAnchorElement) && !focusTarget.hasAttribute('tabindex')) {
    focusTarget.setAttribute('tabindex', '-1')
  }
  focusTarget.focus?.({preventScroll: true})
  return true
}

function setNativeValue(element: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  const proto = Object.getPrototypeOf(element)
  const descriptor = proto ? Object.getOwnPropertyDescriptor(proto, 'value') : undefined
  if (descriptor?.set) {
    descriptor.set.call(element, value)
  } else {
    element.value = value
  }
  element.dispatchEvent(new Event('input', {bubbles: true}))
  element.dispatchEvent(new Event('change', {bubbles: true}))
}

export class FormFillTracker {
  private readonly turns = new Map<string, TurnFillState>()

  constructor(private readonly redactor: Redactor) {}

  private ensureTurn(turnId: string): TurnFillState {
    let state = this.turns.get(turnId)
    if (!state) {
      if (this.turns.size >= MAX_TRACKED_TURNS) {
        const oldest = this.turns.keys().next().value
        if (oldest !== undefined) this.turns.delete(oldest)
      }
      state = {
        turnId,
        formContainer: null,
        undone: false,
        previousFields: {},
        appliedFields: {},
        rejectedFields: {},
        restrictedFields: [],
        restrictedEntries: [],
        undoEntries: new Map(),
      }
      this.turns.set(turnId, state)
    }
    return state
  }

  resolveFieldKey(element: ElementLike | null | undefined, fallbackRef = ''): string {
    const raw = formFieldName(element) || formControlLabel(element) ||
      explicitAttribute(element, 'aria-label') || explicitAttribute(element, 'name') || fallbackRef
    const clean = String(raw || '').replace(/\s+/g, ' ').trim()
      .slice(0, browserContract.form_handoff.max_label_chars)
    return clean
  }

  captureCurrentValue(element: ElementLike | null | undefined): {value: string; checked?: boolean} {
    if (!element || isSensitiveFormControl(element, this.redactor)) return {value: ''}
    const group = radioGroup(element)
    if (group) {
      return {value: selectedRadio(group) ?? observedSelectedRadio(group) ?? ''}
    }
    if (typeof HTMLInputElement !== 'undefined' && element instanceof HTMLInputElement) {
      if (element.type.toLowerCase() === 'checkbox') {
        return {value: element.checked ? 'true' : 'false', checked: element.checked}
      }
      if (listboxCombobox(element)) {
        return {value: selectedComboboxOption(element) ?? String(element.value ?? '')}
      }
    }
    return {value: String(element.value ?? '')}
  }

  scanFormContainer(turnId: string, element: ElementLike | null | undefined): RestrictedEntry[] {
    if (!turnId || typeof Element === 'undefined' || !(element instanceof Element)) return []
    const container = element.closest('[role="dialog"], dialog, form') ?? element.parentElement
    if (!container) return []
    const state = this.ensureTurn(turnId)
    state.formContainer = container
    const restricted = scanRestrictedControlsInContainer(container)
    state.restrictedEntries = restricted
    state.restrictedFields = restricted.map(r => r.label)
    return restricted
  }

  recordApplied(
    turnId: string,
    fieldKey: string,
    element: ElementLike | null | undefined,
    before: {value: string; checked?: boolean},
    appliedValue: string,
  ): void {
    if (!turnId || !fieldKey) return
    if (isSensitiveFormControl(element, this.redactor) || this.redactor.isSensitiveName(fieldKey)) return
    const state = this.ensureTurn(turnId)
    state.undone = false
    this.scanFormContainer(turnId, element)
    if (!Object.hasOwn(state.previousFields, fieldKey)) {
      state.previousFields[fieldKey] = this.redactor.redactString(before.value).slice(0, 300)
      if (typeof Element !== 'undefined' && element instanceof Element) {
        state.undoEntries.set(fieldKey, {
          fieldKey,
          element,
          instance: formInstance(element),
          scope: formControlScope(element),
          previousValue: before.value,
          ...(before.checked !== undefined ? {previousChecked: before.checked} : {}),
        })
      }
    }
    state.appliedFields[fieldKey] = this.redactor.redactString(appliedValue).slice(0, 300)
    delete state.rejectedFields[fieldKey]
  }

  recordRejected(
    turnId: string,
    fieldKey: string,
    reason: string,
    candidates?: string[],
    element?: ElementLike | null,
  ): void {
    if (!turnId || !fieldKey) return
    const state = this.ensureTurn(turnId)
    if (element) this.scanFormContainer(turnId, element)
    const entry: {reason: string; candidates?: string[]} = {
      reason: this.redactor.redactString(reason || 'rejected').slice(0, 120),
    }
    if (candidates?.length) {
      entry.candidates = candidates
        .slice(0, browserContract.choice_failure.max_candidates)
        .map(c => this.redactor.redactString(c).slice(0, browserContract.choice_failure.max_label_chars))
        .filter(Boolean)
    }
    state.rejectedFields[fieldKey] = entry
  }

  receiptFields(turnId: string): {
    appliedFields?: JsonObject
    rejectedFields?: JsonObject
    previousFields?: JsonObject
    restrictedFields?: string[]
  } {
    const state = turnId ? this.turns.get(turnId) : undefined
    if (!state) return {}
    return {
      ...(Object.keys(state.appliedFields).length ? {appliedFields: {...state.appliedFields}} : {}),
      ...(Object.keys(state.rejectedFields).length ? {rejectedFields: {...state.rejectedFields}} : {}),
      ...(Object.keys(state.previousFields).length ? {previousFields: {...state.previousFields}} : {}),
      ...(state.restrictedFields.length ? {restrictedFields: [...state.restrictedFields]} : {}),
    }
  }

  /** Automatically smooth-scroll and focus the first restricted control on handoff, if present. */
  focusFirstRestricted(turnId: string): boolean {
    const state = turnId ? this.turns.get(turnId) : undefined
    if (!state || !state.restrictedEntries.length) return false
    for (const entry of state.restrictedEntries) {
      if (focusRestrictedElement(entry.element)) return true
    }
    return false
  }

  /** Focus a restricted control requested by the panel's handoff card. */
  focusControl(
    control: HandoffRestrictedControl,
    lookupRef?: (ref: string) => ElementLike | null | undefined,
    doc: Document | null | undefined = typeof document !== 'undefined' ? document : null,
  ): boolean {
    if (!control || typeof control !== 'object') return false
    if (control.ref && lookupRef) {
      try {
        const byRef = lookupRef(control.ref)
        if (byRef instanceof Element && focusRestrictedElement(byRef)) return true
      } catch {
        // Fall through to label/kind matching.
      }
    }
    const entries = [...this.turns.values()].reverse()
    for (const state of entries) {
      const matched = state.restrictedEntries.find(item =>
        item.element.isConnected &&
        (!control.kind || control.kind === 'restricted' || item.kind === control.kind) &&
        (!control.label || item.label === control.label))
      if (matched && focusRestrictedElement(matched.element)) return true
    }
    if (doc) {
      for (const container of doc.querySelectorAll('[role="dialog"], dialog, form')) {
        for (const item of scanRestrictedControlsInContainer(container)) {
          if ((!control.kind || control.kind === 'restricted' || item.kind === control.kind) &&
              (!control.label || item.label === control.label)) {
            if (focusRestrictedElement(item.element)) return true
          }
        }
      }
    }
    return false
  }

  /** Restore pre-fill field values captured for `turnId` while the same form is still open. */
  async undoTurn(turnId: string, options: UndoFormOptions = {}): Promise<boolean> {
    const state = turnId ? this.turns.get(turnId) : undefined
    if (!state || state.undone || state.undoEntries.size === 0) return false
    const entries = [...state.undoEntries.values()]
    for (const entry of entries) {
      if (!entry.element.isConnected) return false
      if (entry.instance && formInstance(entry.element) !== entry.instance) return false
      if (entry.scope && formControlScope(entry.element) !== entry.scope) return false
      try {
        ensureFormWritableAndOpen(
          entry.element as ElementLike,
          'undo',
          listboxCombobox(entry.element as ElementLike),
        )
      } catch {
        return false
      }
    }
    if (options.restoreFormModel && options.readFormModel) {
      const anchor = entries[0]!.element as ElementLike
      const modelRead = options.readFormModel(anchor)
      if (modelRead && !modelRead.withheld.length) {
        const values: Record<string, unknown> = {}
        const allMappable = entries.every(entry => {
          const prop = formFieldName(entry.element as ElementLike) || entry.fieldKey
          if (prop && Object.hasOwn(modelRead.model, prop) && prop !== 'id') {
            values[prop] = entry.previousValue
            return true
          }
          return false
        })
        if (allMappable && Object.keys(values).length) {
          const ok = await options.restoreFormModel(anchor, {expected: modelRead.model, values})
          if (ok) {
            state.undone = true
            options.onRestored?.()
            return true
          }
        }
      }
    }
    for (const entry of entries) {
      const el = entry.element
      const group = radioGroup(el as ElementLike)
      if (group) {
        for (const choice of radioChoices(group)) {
          const want = Boolean(entry.previousValue) &&
            (radioLabel(choice) === entry.previousValue ||
              (choice instanceof HTMLInputElement && choice.value === entry.previousValue))
          if (choice instanceof HTMLInputElement) {
            choice.checked = want
            choice.dispatchEvent(new Event('change', {bubbles: true}))
          }
        }
        continue
      }
      if (typeof HTMLSelectElement !== 'undefined' && el instanceof HTMLSelectElement) {
        el.value = entry.previousValue
        el.dispatchEvent(new Event('change', {bubbles: true}))
        continue
      }
      if (typeof HTMLInputElement !== 'undefined' && el instanceof HTMLInputElement &&
          el.type.toLowerCase() === 'checkbox') {
        el.checked = entry.previousChecked ?? (entry.previousValue === 'true')
        el.dispatchEvent(new Event('change', {bubbles: true}))
        continue
      }
      if (typeof HTMLInputElement !== 'undefined' && el instanceof HTMLInputElement ||
          typeof HTMLTextAreaElement !== 'undefined' && el instanceof HTMLTextAreaElement) {
        setNativeValue(el as HTMLInputElement | HTMLTextAreaElement, entry.previousValue)
      }
    }
    state.undone = true
    options.onRestored?.()
    return true
  }

  clear(): void {
    this.turns.clear()
  }
}
