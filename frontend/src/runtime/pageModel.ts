/**
 * What the Runtime tells the server a page contains.
 *
 * PageController serializes the page as indexed records (`[12]<a href=...>Text />`). This
 * turns them into the server's element schema, then corrects the few facts where its
 * display text is lossy and the server needs the truth -- an exact link target, a form
 * field's application identifier. Everything that can carry a credential goes through the
 * `Redactor` it is given.
 */
import type {Redactor} from './redaction'
import type {ElementRef} from './types'
import browserContract from '@contract/browser_contract.json'
import {formInstance} from './formInstance'
import {listboxCombobox, selectedOption} from './combobox'
import {radioGroup, radioChoices, selectedRadio} from './radio'

/** Maximum live controls one observation may expose across the browser boundary. */
export const MAX_OBSERVATION_ELEMENTS = browserContract.observation.max_elements

export interface BoundedPageContent {
  content: string
  truncated: boolean
}

/**
 * Keep only the first bounded set of PageController records.
 *
 * PageController owns the expensive DOM walk; this bounds what crosses the browser boundary
 * and what the model can reference. It cuts before a complete indexed record, never inside
 * one. With viewport-only extraction the omitted records are reachable by an explicit scroll
 * and a fresh observation.
 */
export function boundIndexedContent(content: string,
                                    maximum = MAX_OBSERVATION_ELEMENTS): BoundedPageContent {
  if (!Number.isInteger(maximum) || maximum < 1) {
    throw new Error('maximum observation elements must be a positive integer')
  }
  const records = /(?:^|\n)\s*\*?\[(\d+)]</g
  let seen = 0
  for (const match of content.matchAll(records)) {
    if (seen === maximum) {
      return {content: content.slice(0, match.index).trimEnd(), truncated: true}
    }
    seen += 1
  }
  return {content, truncated: false}
}

/**
 * The properties a fingerprint or a fact is read from. Elements are heterogeneous -- an
 * input has a value, a select has options, a link has an href -- so each is optional, and a
 * missing one reads as empty rather than as a reason to throw.
 */
export interface ElementLike {
  tagName?: string
  type?: string
  name?: string
  id?: string
  role?: string | null
  textContent?: string | null
  value?: string
  checked?: boolean
  disabled?: boolean
  readOnly?: boolean
  href?: string
  target?: string
  isConnected?: boolean
  className?: string | {baseVal?: string}
  options?: ArrayLike<{value?: string; textContent?: string | null; disabled?: boolean; selected?: boolean}>
  labels?: ArrayLike<ElementLike> | null
  action?: string
  method?: string
  form?: ElementLike | null
  ownerDocument?: Document | null
  hasAttribute?(name: string): boolean
  getAttribute?(name: string): string | null
  closest?(selector: string): ElementLike | null
  querySelectorAll?(selector: string): ArrayLike<ElementLike>
}

export type ElementLookup = (selectorMap: unknown, index: number) => ElementLike | null | undefined

/** An attribute the element itself declares, or `null` if it declares none. */
export function explicitAttribute(element: ElementLike | null | undefined, name: string): string | null {
  if (!element) return null
  if (typeof element.hasAttribute === 'function') {
    return element.hasAttribute(name) ? String(element.getAttribute?.(name) || '') : null
  }
  // Plain objects stand in for elements in tests and in frames the Runtime cannot reach.
  return Object.hasOwn(element, name)
    ? String((element as Record<string, unknown>)[name] || '') : null
}

/** The PageController index behind a server element ref (`e12` -> 12). */
export function refIndex(ref: unknown): number {
  const match = /^e(\d+)$/.exec(String(ref || ''))
  if (!match) throw new Error('bad_arguments: action needs an e<number> ref')
  return Number(match[1])
}

/** A field wrapper may name its input; a form's name identifies the form, not its fields. */
export function formFieldName(element: ElementLike | null | undefined): string | null {
  if (!element) return null
  const owner = typeof element.closest === 'function' ? element.closest('[name]') : null
  return explicitAttribute(owner && String(owner.tagName || '').toUpperCase() !== 'FORM'
    ? owner : element, 'name')
}

function uniqueId(document: Document | null | undefined, id: string): Element | null {
  if (!document || !/^[A-Za-z][A-Za-z0-9_:.-]{0,199}$/.test(id)) return null
  const matches = document.querySelectorAll(`[id="${id}"]`)
  return matches.length === 1 ? matches[0]! : null
}

/** Exactly one associated label, never a placeholder or arbitrary nearby text. */
export function formControlLabel(element: ElementLike | null | undefined): string | null {
  if (!element) return null
  const names: string[] = []
  const labels = Array.from(element.labels || [])
  if (labels.length > 1) return null
  if (labels.length === 1) names.push(String(labels[0]?.textContent || ''))
  const labelledby = explicitAttribute(element, 'aria-labelledby')
  if (labelledby !== null) {
    const ids = labelledby.trim().split(/\s+/)
    if (!ids.length || ids.length > 4 || new Set(ids).size !== ids.length) return null
    const nodes = ids.map(id => uniqueId(element.ownerDocument, id))
    if (nodes.some(node => !node)) return null
    names.push(nodes.map(node => node!.textContent || '').join(' '))
  }
  const ariaLabel = explicitAttribute(element, 'aria-label')
  if (ariaLabel !== null) names.push(ariaLabel)
  const normalized = names.map(name => name.replace(/\s+/g, ' ').trim())
  if (!normalized.length || normalized.some(name => !name ||
      name.length > browserContract.form_handoff.max_label_chars)
      || new Set(normalized).size !== 1) return null
  return normalized[0]!
}

/** The one DOM-owned dialog/form identity; missing or duplicate IDs establish no scope. */
export function formControlScope(element: ElementLike | null | undefined): string | null {
  if (!element) return null
  const dialog = element.closest?.('[role="dialog"],dialog')
  if (dialog) {
    const id = explicitAttribute(dialog, 'id')
    if (id && uniqueId(element.ownerDocument, id) === dialog) return `dialog:${id}`
    for (const association of ['labelledby', 'describedby']) {
      const id = explicitAttribute(dialog, `aria-${association}`)
      if (id && uniqueId(element.ownerDocument, id)
          && dialog.querySelectorAll?.(`[id="${id}"]`).length === 1
          && element.ownerDocument?.querySelectorAll(`[role="dialog"][aria-${association}="${id}"],dialog[aria-${association}="${id}"]`)
            .length === 1) return `dialog_${association}:${id}`
    }
    return null
  }
  const form = element.form || element.closest?.('form')
  const id = explicitAttribute(form, 'id')
  if (id) return uniqueId(element.ownerDocument, id) === form ? `form:${id}` : null
  // Ordinary native forms need not have an HTML id. Use their observed DOM
  // lifetime rather than inventing an index, route or application-owned name.
  const instance = formInstance(form as Element | null)
  return instance ? `form_instance:${instance}` : null
}

/** Live identity can reveal a sensitive input that the controller summary left unnamed. */
export function isSensitiveFormControl(element: ElementLike | null | undefined,
                                       redactor: Redactor): boolean {
  return String(element?.type || '').toLowerCase() === 'password'
    || [formFieldName(element), formControlLabel(element), element?.name, element?.id,
      explicitAttribute(element, 'autocomplete')].some(value =>
      value !== null && value !== undefined && redactor.isSensitiveName(String(value)))
}

/**
 * Everything about an element that an action's meaning depends on.
 *
 * The server hands out an action against the element it saw; if the element under that ref
 * is no longer the same thing -- another control, another target, another form -- the
 * action is refused rather than pressed.
 */
export function actionElementFingerprint(element: ElementLike | null | undefined): string {
  if (!element || element.isConnected === false) return 'missing'
  const form = String(element.tagName || '').toUpperCase() === 'FORM'
    ? element as NonNullable<ElementLike['form']> : element.form
  const options = Array.from(element.options || []).map(option => [
    String(option.value || ''), String(option.textContent || ''),
    Boolean(option.disabled), Boolean(option.selected),
  ])
  return JSON.stringify({
    tag: String(element.tagName || '').toUpperCase(),
    type: String(element.type || ''),
    name: String(element.name || ''),
    field: formFieldName(element),
    fieldLabel: formControlLabel(element),
    formScope: formControlScope(element),
    formInstance: formInstance(element as Element | null),
    dialogTitle: formControlLabel(element.closest?.('[role="dialog"],dialog')),
    dialogText: String(element.closest?.('[role="dialog"],dialog')?.textContent || '').slice(0, 1600),
    id: String(element.id || ''),
    role: String(element.getAttribute?.('role') || element.role || ''),
    ariaSelected: explicitAttribute(element, 'aria-selected'),
    ariaChecked: explicitAttribute(element, 'aria-checked'),
    ariaExpanded: explicitAttribute(element, 'aria-expanded'),
    text: String(element.textContent || ''),
    value: String(element.value || ''),
    checked: Boolean(element.checked),
    disabled: Boolean(element.disabled),
    readOnly: Boolean(element.readOnly),
    href: String(element.href || ''),
    target: String(element.target || ''),
    formAction: String(form?.action || ''),
    formMethod: String(form?.method || ''),
    formTarget: String(form?.target || ''),
    options,
  })
}

function attrsFrom(text: string, redactor: Redactor): Record<string, string> {
  const attrs: Record<string, string> = {}
  const matcher = /([\w:-]+)=(?:"([^"]*)"|'([^']*)'|([^\s>]+))/g
  for (const match of text.matchAll(matcher)) {
    const key = match[1] ?? ''
    const value = match[2] ?? match[3] ?? match[4] ?? ''
    attrs[key] = redactor.isSensitiveName(key) ? '[redacted]' : redactor.redactString(value)
  }
  return attrs
}

function roleFor(tag: string, attrs: Record<string, string>): string {
  if (attrs.role) return attrs.role
  if (tag === 'a') return 'link'
  if (tag === 'button') return 'button'
  if (tag === 'select') return 'select'
  if (tag === 'textarea') return 'textbox'
  if (tag === 'input') {
    const type = String(attrs.type || 'text').toLowerCase()
    if (['submit', 'button', 'image'].includes(type)) return 'button'
    if (['checkbox', 'radio', 'password'].includes(type)) return type
    if (type === 'number') return 'spinbutton'
    return 'textbox'
  }
  return tag
}

/** Convert PageController's stable numeric indices into the server's element schema. */
export function elementsFrom(content: string, epoch: number, redactor: Redactor): ElementRef[] {
  const elements: ElementRef[] = []
  const records = /(?:^|\n)\s*\*?\[(\d+)]<([\w-]+)([\s\S]*?)\s*\/>?(?=\n|$)/g
  for (const match of content.matchAll(records)) {
    const index = Number(match[1])
    const tag = (match[2] ?? '').toLowerCase()
    const body = match[3] || ''
    const separator = body.indexOf('>')
    const attrs = attrsFrom(separator >= 0 ? body.slice(0, separator) : body, redactor)
    const text = separator >= 0 ? body.slice(separator + 1) : ''
    const role = roleFor(tag, attrs)
    // Legacy applications often use labelled input[type=submit] controls. Unlike a button,
    // they have no inner text, so retain the button value as the accessible name. Textbox
    // values remain state and never become their accessible name.
    const buttonValue = role === 'button' ? attrs.value : ''
    const name = redactor.isSensitiveName(String(attrs.name || ''))
      ? ''
      : redactor.redactString(
        // Accessible-name order: content, aria-label, then title (HTML's own tooltip). An
        // icon-only control named by a tooltip attribute alone -- `data-tooltip` is common --
        // keeps that as a last resort, so "Kanban" is not an anonymous button.
        (text || attrs['aria-label'] || attrs.title || attrs.placeholder || buttonValue ||
          attrs['data-tooltip'] || '').trim())
        .slice(0, 120)
    elements.push({ref: `e${index}`, role, name, tag, epoch, attrs})
  }
  return elements
}

/**
 * Add policy-critical link facts from the live DOM rather than PageController's display
 * text. PageController abbreviates long attributes with `...`; that text is useful model
 * context but cannot be the source of truth for an exact-link allowlist.
 */
export function hydrateLinkFacts(elements: ElementRef[], selectorMap: unknown,
                                 getByIndex: ElementLookup, redactor: Redactor): ElementRef[] {
  for (const item of elements) {
    if (item.role !== 'link') continue
    try {
      const element = getByIndex(selectorMap, refIndex(item.ref))
      const rawHref = explicitAttribute(element, 'href')
      if (rawHref) item.attrs.href = redactor.redactString(rawHref)
      const rawTarget = explicitAttribute(element, 'target')
      if (rawTarget !== null) item.attrs.target = redactor.redactString(rawTarget)
    } catch {
      // A DOM mutation between PageController's snapshot and this lookup makes the index
      // stale. Keep the lossy summary and let the server fail closed on exact navigation.
    }
  }
  return elements
}

/** The current-state words a UI framework puts on the one selected tab of a set. */
const SELECTED_CLASSES = new Set(['active', 'selected', 'is-active', 'is-selected'])
const CURRENT_VALUES = new Set(['page', 'step', 'location', 'true'])

/**
 * Say which tab is current when the page says it only by class.
 *
 * A tab that declares `aria-selected` is left alone. Many applications do not declare it:
 * Bootstrap's `.nav-link.active` is how Odoo 18 marks the current notebook tab, so the
 * observation showed no tab as current, a declared "Sales tab selected" could never be met,
 * and the turn clicked the tab again until its budget ran out. The fact is derived from the
 * live element as `selected=true`, beside the ARIA attributes and never written to the page.
 */
export function hydrateSelectionFacts(elements: ElementRef[], selectorMap: unknown,
                                      getByIndex: ElementLookup): ElementRef[] {
  for (const item of elements) {
    if (item.role !== 'tab' && item.role !== 'button') continue
    if (item.role === 'tab') {
      try {
        const element = getByIndex(selectorMap, refIndex(item.ref))
        const controls = explicitAttribute(element, 'aria-controls')
        if (controls) {
          // Read the declared relationship, never infer a panel from its text.
          item.attrs.panel_ready = 'false'
          const panels = Array.from(element?.ownerDocument?.querySelectorAll('[role="tabpanel"]') || [])
            .filter(panel => panel.id === controls)
          if (panels.length === 1) {
            const panel = panels[0] as HTMLElement
            const labelledBy = (panel.getAttribute('aria-labelledby') || '').split(/\s+/)
            const id = explicitAttribute(element, 'id')
            const visible = !panel.closest('[hidden], [aria-hidden="true"]') && panel.getClientRects().length > 0
            const busy = panel.matches('[aria-busy="true"]') || panel.querySelector('[aria-busy="true"], [role="progressbar"]')
            item.attrs.panel_ready = String(Boolean(id && labelledBy.includes(id) && visible && !busy))
          }
        }
      } catch {
        item.attrs.panel_ready = 'false'
      }
    }
    if (item.attrs['aria-selected'] !== undefined || item.attrs['aria-pressed'] !== undefined) continue
    try {
      const element = getByIndex(selectorMap, refIndex(item.ref))
      const current = String(explicitAttribute(element, 'aria-current') || '').toLowerCase()
      const raw = element?.className
      const className = typeof raw === 'string' ? raw : String(raw?.baseVal || '')
      const byClass = className.split(/\s+/).some(token => SELECTED_CLASSES.has(token))
      if (item.role === 'tab' && (CURRENT_VALUES.has(current) || byClass)) {
        item.attrs.selected = 'true'
      } else if (item.role === 'button' && byClass) {
        // A lone active button may be a workflow action, not a selected view. A group
        // with exactly one active button is a conventional mutually exclusive toggle.
        const group = element?.closest?.('nav, [role="group"], [role="toolbar"]')
        const buttons = Array.from(group?.querySelectorAll?.('button') || [])
        const active = buttons.filter(button => {
          const value = button.className
          const tokens = (typeof value === 'string' ? value : String(value?.baseVal || ''))
            .split(/\s+/)
          return tokens.some(token => SELECTED_CLASSES.has(token))
        })
        if (buttons.length >= 2 && active.length === 1 && active[0] === element) {
          item.attrs.selected = 'true'
        }
      }
    } catch {
      // A stale index says nothing about the tab; leave it unmarked.
    }
  }
  return elements
}

/**
 * Add a bounded application field identifier from the nearest form-field wrapper. Modern
 * component systems often put `name` on that wrapper and leave the actual input unnamed.
 * The identifier is observation data, not authority: the server still requires an exact
 * adapter-issued form draft before it may use it. Credential-shaped names are omitted.
 */
export function hydrateFormFacts(elements: ElementRef[], selectorMap: unknown,
                                 getByIndex: ElementLookup, redactor: Redactor): ElementRef[] {
  const groups = new Set<HTMLElement>()
  const claimed = new WeakSet<object>()
  for (const item of elements) {
    try {
      let element = getByIndex(selectorMap, refIndex(item.ref))
      const group = radioGroup(element)
      // Scrub each alias before either radio grouping or control deduplication.
      // The parsed summary may lack a sensitive name that only the live DOM exposes.
      if (isSensitiveFormControl(element, redactor) ||
          (group && isSensitiveFormControl(group, redactor))) {
        for (const key of ['value', 'form_scope', 'form_instance', 'field_label', 'field', 'selected_option']) {
          delete item.attrs[key]
        }
        item.name = ''
        continue
      }
      if (group && radioChoices(group).length) {
        if (groups.has(group)) continue
        groups.add(group)
        element = group
        item.role = 'radiogroup'
        item.tag = group.tagName.toLowerCase()
        item.name = redactor.redactString(formControlLabel(group) || '')
      }
      // An application controller may map an annotated hit area to its accessible input.
      // Keep its live semantic role; no framework selector or invented label is used here.
      if (typeof HTMLInputElement !== 'undefined' && element instanceof HTMLInputElement &&
          element.getAttribute('role') === 'combobox') {
        item.role = 'combobox'
        item.tag = 'input'
      }
      if (![...browserContract.form_handoff.input_controls, 'searchbox', 'button'].includes(item.role)) continue
      delete item.attrs.form_scope
      delete item.attrs.form_instance
      delete item.attrs.field_label
      delete item.attrs.field
      delete item.attrs.selected_option
      // Two indexed hit areas can resolve to one live control: a select's wrapper and the
      // input inside it. Only the first carries that control's form facts, so a field a
      // draft names is still exactly one control.
      if (item.role !== 'button' && element && typeof element === 'object') {
        if (claimed.has(element)) continue
        claimed.add(element)
      }
      // These are live DOM facts rather than hints parsed from PageController's text.
      // They let the server omit an unavailable field up front; Page.input/select still
      // recheck the live element at execution time because the DOM may change afterwards.
      if (element?.disabled) item.attrs.disabled = 'true'
      // Read-only text entry is normal for a single-choice combobox. Selection remains
      // available; Page.input still refuses typing and Page.select checks the live menu.
      delete item.attrs.readonly
      if (element?.readOnly && !listboxCombobox(element)) item.attrs.readonly = 'true'
      const scope = formControlScope(element)
      if (scope && !redactor.isSensitiveName(scope)) item.attrs.form_scope = scope
      const instance = scope ? formInstance(element as Element | null) : null
      if (instance) item.attrs.form_instance = instance
      if (item.role === 'button') continue
      const rawField = formFieldName(element)
      const sensitive = isSensitiveFormControl(element, redactor)
      // Parsed summary attributes can predate a DOM change or omit a wrapper's name.
      // Never leave their value behind when the live control is sensitive.
      if (sensitive) {
        delete item.attrs.value
        delete item.attrs.field
        delete item.attrs.field_label
        item.name = ''
        continue
      }
      if (listboxCombobox(element)) {
        const selected = selectedOption(element)
        if (selected && selected.length <= browserContract.form_handoff.max_label_chars &&
            redactor.redactString(selected) === selected) item.attrs.selected_option = selected
      }
      const label = formControlLabel(element)
      if (scope && !rawField && label && redactor.redactString(label) === label) {
        item.attrs.field_label = label
      }
      // What a text field currently shows, so the server can check a value it entered
      // before handing the form to the person. Redacted like any other observed text,
      // bounded, and never for a field whose name marks it sensitive.
      if (item.role !== 'select' && typeof element?.value === 'string' && element.value) {
        item.attrs.value = redactor.redactString(element.value).slice(0, 120)
      }
      if (group) {
        delete item.attrs.value
        const selected = selectedRadio(group)
        if (selected && !sensitive) item.attrs.value = redactor.redactString(selected).slice(0, 120)
      }
      if (rawField !== null && /^[A-Za-z][A-Za-z0-9_.-]{0,79}$/.test(rawField)
          && !redactor.isSensitiveName(rawField)) {
        item.attrs.field = rawField
      }
    } catch {
      // A stale selector or an unusual DOM shape supplies no semantic field fact. The
      // server will refuse an adapter draft that cannot resolve to one unique live field.
    }
  }
  return elements
}

/**
 * Whether a control is a collapsed menu the Runtime may open: a button (or an element in
 * the button role) that declares it is collapsed -- `aria-expanded="false"`, or a popup it
 * has not opened -- and is not a form input, a submit button, a link, or part of the
 * page's navigation. Opening it only shows what the application already offers; nothing is
 * chosen in it. One rule, for the observation that offers the control and for the command
 * that opens it, in every application.
 */
export function revealable(element: ElementLike | null | undefined): boolean {
  if (!element || element.isConnected === false || element.disabled) return false
  const tag = String(element.tagName || '').toLowerCase()
  const role = String(explicitAttribute(element, 'role') ?? '').toLowerCase()
  if (!(tag === 'button' || role === 'button')) return false
  // A button submits its form unless it declares otherwise; one outside any form (the DOM
  // still reports its default type as "submit") submits nothing.
  const declared = explicitAttribute(element, 'type')?.toLowerCase() ?? null
  if (declared === 'submit' || (tag === 'button' && declared === null && element.form)) return false
  const expanded = explicitAttribute(element, 'aria-expanded')
  const popup = explicitAttribute(element, 'aria-haspopup')
  const collapsed = expanded === 'false'
    || (expanded === null && popup !== null && popup !== 'false')
  if (!collapsed) return false
  const inside = typeof element.closest === 'function'
    ? element.closest('nav, [role="navigation"], [role="menubar"]') : null
  return !inside
}

/** Mark each observed control the Runtime may open as a menu (`revealable="true"`). */
export function hydrateRevealFacts(elements: ElementRef[], selectorMap: unknown,
                                   getByIndex: ElementLookup): ElementRef[] {
  for (const item of elements) {
    if (item.role !== 'button') continue
    try {
      if (revealable(getByIndex(selectorMap, refIndex(item.ref)))) item.attrs.revealable = 'true'
    } catch {
      // A stale index offers nothing to open.
    }
  }
  return elements
}

/** A bounded live button method identifier for an adapter's exact click review. */
export function hydrateButtonFacts(elements: ElementRef[], selectorMap: unknown,
                                   getByIndex: ElementLookup, redactor: Redactor): ElementRef[] {
  for (const item of elements) {
    if (item.role !== 'button') continue
    try {
      const element = getByIndex(selectorMap, refIndex(item.ref))
      // Some controllers emit no text for buttons containing icons and spans.
      // Recover only an already indexed native button's live visible name.
      if (!item.name && element instanceof HTMLButtonElement &&
          !isSensitiveFormControl(element, redactor)) {
        const raw = (explicitAttribute(element, 'aria-label') || element.innerText || '').replace(/\s+/g, ' ').trim()
        if (raw && raw.length <= 120 && !redactor.isSensitiveName(raw)) {
          item.name = redactor.redactString(raw)
        }
      }
      const method = explicitAttribute(element, 'name')
      if (method !== null && /^[A-Za-z][A-Za-z0-9_.-]{0,79}$/.test(method)
          && !redactor.isSensitiveName(method)) item.attrs.action_name = method
    } catch {
      // A stale index supplies no policy fact. A reviewed opener then stays refused.
    }
  }
  return elements
}
