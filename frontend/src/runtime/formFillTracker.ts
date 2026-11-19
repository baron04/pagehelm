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
import {
  explicitAttribute, formControlLabel, formControlScope, formFieldName,
  isSensitiveFormControl, type ElementLike,
} from './pageModel'
import {observedSelectedRadio, radioChoices, radioGroup, radioLabel, selectedRadio} from './radio'
import type {Redactor} from './redaction'
import type {JsonObject} from './types'

export interface CapturedFieldValue {
  value: string
  checked?: boolean
  isCombobox?: boolean
  modelTracked?: boolean
  modelProp?: string
  hasModelKey?: boolean
  previousModelValue?: unknown
}

interface UndoFieldEntry {
  fieldKey: string
  element: Element
  instance: string | null
  scope: string | null
  previousValue: string
  previousChecked?: boolean
  isCombobox: boolean
  modelTracked: boolean
  modelProp: string
  hasModelKey: boolean
  previousModelValue: unknown
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

function isElementWritableAndOpen(node: Element, allowReadonlyCombobox = false): boolean {
  if (!isElementOpenAndVisible(node)) return false
  const combobox = allowReadonlyCombobox && isComboboxControl(node as ElementLike)
  const formEl = node as Partial<HTMLInputElement>
  const ariaDisabled = node.getAttribute('aria-disabled') === 'true'
  const ariaReadonly = node.getAttribute('aria-readonly') === 'true'
  if (formEl.disabled || ariaDisabled || (!combobox && (formEl.readOnly || ariaReadonly))) {
    return false
  }
  return !node.classList.contains('is-disabled') &&
    !node.closest('fieldset:disabled,.is-disabled,[aria-disabled="true"]')
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

function isComboboxControl(element: ElementLike | null | undefined): boolean {
  if (!element) return false
  if (listboxCombobox(element)) return true
  return typeof HTMLInputElement !== 'undefined' && element instanceof HTMLInputElement &&
    Boolean(element.readOnly) && explicitAttribute(element, 'role') === 'combobox'
}

function clonePlainValue<T>(value: T): T {
  if (value === undefined || value === null || typeof value !== 'object') return value
  if (typeof structuredClone === 'function') {
    try { return structuredClone(value) } catch { /* fall through */ }
  }
  return JSON.parse(JSON.stringify(value)) as T
}

function resolveModelProp(
  element: ElementLike,
  modelRead: FormModelRead,
  redactor: Redactor,
): string {
  const direct = formFieldName(element)
  if (direct && !redactor.isSensitiveName(direct) && !modelRead.withheld.includes(direct) &&
      (Object.hasOwn(modelRead.model, direct) || modelRead.items.some(item => item.prop === direct))) {
    return direct
  }
  const label = formControlLabel(element)
  if (label) {
    const matched = modelRead.items.filter(item => item.label === label && item.prop)
    if (matched.length === 1) {
      const prop = matched[0]!.prop
      if (!redactor.isSensitiveName(prop) && !modelRead.withheld.includes(prop)) return prop
    }
  }
  return ''
}

export class FormFillTracker {
  private readonly turns = new Map<string, TurnFillState>()
  private readonly baselineByField = new Map<string, {
    element: Element
    baselineText: string
    undo: UndoFieldEntry
  }>()

  constructor(private readonly redactor: Redactor) {}

  private baselineKey(element: Element, fieldKey: string): string {
    return `${formInstance(element) ?? ''}::${formControlScope(element) ?? ''}::${fieldKey}`
  }

  private pruneDisconnectedBaselines(): void {
    for (const [key, item] of this.baselineByField) {
      if (!item.element.isConnected) this.baselineByField.delete(key)
    }
  }

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

  captureCurrentValue(
    element: ElementLike | null | undefined,
    readFormModel?: ((element: ElementLike) => FormModelRead | null) | undefined,
  ): CapturedFieldValue {
    if (!element || isSensitiveFormControl(element, this.redactor)) return {value: ''}
    const combobox = isComboboxControl(element)
    let value = ''
    let checked: boolean | undefined
    const group = radioGroup(element)
    if (group) {
      value = selectedRadio(group) ?? observedSelectedRadio(group) ?? ''
    } else if (typeof HTMLInputElement !== 'undefined' && element instanceof HTMLInputElement) {
      if (element.type.toLowerCase() === 'checkbox') {
        checked = element.checked
        value = element.checked ? 'true' : 'false'
      } else if (combobox) {
        value = selectedComboboxOption(element) ?? String(element.value ?? '')
      } else {
        value = String(element.value ?? '')
      }
    } else {
      value = String(element.value ?? '')
    }
    let modelMeta: Partial<CapturedFieldValue> = {}
    if (readFormModel) {
      try {
        const modelRead = readFormModel(element)
        if (modelRead) {
          const modelProp = resolveModelProp(element, modelRead, this.redactor)
          if (modelProp && modelProp !== 'id') {
            const hasModelKey = Object.hasOwn(modelRead.model, modelProp)
            modelMeta = {
              modelTracked: true,
              modelProp,
              hasModelKey,
              previousModelValue: hasModelKey ? clonePlainValue(modelRead.model[modelProp]) : undefined,
            }
          }
        }
      } catch {
        // Unreadable or non-ElForm controls fall back to DOM tracking.
      }
    }
    return {
      value,
      ...(checked !== undefined ? {checked} : {}),
      ...(combobox ? {isCombobox: true} : {}),
      ...modelMeta,
    }
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
    before: CapturedFieldValue,
    appliedValue: string,
  ): void {
    if (!turnId || !fieldKey) return
    if (isSensitiveFormControl(element, this.redactor) || this.redactor.isSensitiveName(fieldKey)) return
    this.pruneDisconnectedBaselines()
    const state = this.ensureTurn(turnId)
    state.undone = false
    this.scanFormContainer(turnId, element)
    if (!Object.hasOwn(state.previousFields, fieldKey)) {
      if (typeof Element !== 'undefined' && element instanceof Element) {
        const bKey = this.baselineKey(element, fieldKey)
        const existing = this.baselineByField.get(bKey)
        if (existing && existing.element === element && existing.element.isConnected) {
          state.previousFields[fieldKey] = existing.baselineText
          state.undoEntries.set(fieldKey, {
            ...existing.undo,
            previousModelValue: existing.undo.modelTracked && existing.undo.hasModelKey
              ? clonePlainValue(existing.undo.previousModelValue)
              : existing.undo.previousModelValue,
          })
        } else {
          const baselineText = this.redactor.redactString(before.value).slice(0, 300)
          const entry: UndoFieldEntry = {
            fieldKey,
            element,
            instance: formInstance(element),
            scope: formControlScope(element),
            previousValue: before.value,
            ...(before.checked !== undefined ? {previousChecked: before.checked} : {}),
            isCombobox: Boolean(before.isCombobox || isComboboxControl(element)),
            modelTracked: Boolean(before.modelTracked && before.modelProp),
            modelProp: before.modelProp ?? '',
            hasModelKey: Boolean(before.hasModelKey),
            previousModelValue: before.modelTracked && before.hasModelKey
              ? clonePlainValue(before.previousModelValue)
              : undefined,
          }
          state.previousFields[fieldKey] = baselineText
          state.undoEntries.set(fieldKey, entry)
          this.baselineByField.set(bKey, {
            element,
            baselineText,
            undo: {
              ...entry,
              previousModelValue: entry.modelTracked && entry.hasModelKey
                ? clonePlainValue(entry.previousModelValue)
                : entry.previousModelValue,
            },
          })
        }
      } else {
        state.previousFields[fieldKey] = this.redactor.redactString(before.value).slice(0, 300)
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
      if (!isElementWritableAndOpen(entry.element,
          Boolean(entry.isCombobox || isComboboxControl(entry.element as ElementLike)))) {
        return false
      }
    }
    const clearUndoneBaselines = (): void => {
      for (const entry of entries) {
        const bKey = this.baselineKey(entry.element, entry.fieldKey)
        const held = this.baselineByField.get(bKey)
        if (held?.element === entry.element) this.baselineByField.delete(bKey)
      }
    }
    if (options.restoreFormModel && options.readFormModel) {
      const anchor = entries[0]!.element as ElementLike
      const modelRead = options.readFormModel(anchor)
      if (modelRead) {
        const values: Record<string, unknown> = {}
        const unset: string[] = []
        const allMappable = entries.every(entry => {
          if (!entry.modelTracked || !entry.modelProp || entry.modelProp === 'id') return false
          if (entry.hasModelKey) {
            if (!Object.hasOwn(modelRead.model, entry.modelProp)) return false
            values[entry.modelProp] = clonePlainValue(entry.previousModelValue)
            return true
          }
          unset.push(entry.modelProp)
          return true
        })
        if (allMappable && (Object.keys(values).length > 0 || unset.length > 0)) {
          const ok = await options.restoreFormModel(anchor, {
            expected: modelRead.model,
            values,
            ...(unset.length ? {unset} : {}),
            allowInvalid: true,
          })
          if (ok) {
            state.undone = true
            clearUndoneBaselines()
            options.onRestored?.()
            return true
          }
          return false
        }
      }
    }
    if (entries.some(entry => entry.isCombobox || isComboboxControl(entry.element as ElementLike))) {
      return false
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
    clearUndoneBaselines()
    options.onRestored?.()
    return true
  }

  clear(): void {
    this.turns.clear()
    this.baselineByField.clear()
  }
}
