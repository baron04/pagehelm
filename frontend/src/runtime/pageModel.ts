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
  options?: ArrayLike<{value?: string; textContent?: string | null; disabled?: boolean; selected?: boolean}>
  form?: {action?: string; method?: string; target?: string} | null
  ownerDocument?: Document | null
  hasAttribute?(name: string): boolean
  getAttribute?(name: string): string | null
  closest?(selector: string): ElementLike | null
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
        (text || attrs['aria-label'] || attrs.placeholder || buttonValue || '').trim())
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

/**
 * Add a bounded application field identifier from the nearest form-field wrapper. Modern
 * component systems often put `name` on that wrapper and leave the actual input unnamed.
 * The identifier is observation data, not authority: the server still requires an exact
 * adapter-issued form draft before it may use it. Credential-shaped names are omitted.
 */
export function hydrateFormFacts(elements: ElementRef[], selectorMap: unknown,
                                 getByIndex: ElementLookup, redactor: Redactor): ElementRef[] {
  for (const item of elements) {
    if (!['textbox', 'combobox', 'select'].includes(item.role)) continue
    try {
      const element = getByIndex(selectorMap, refIndex(item.ref))
      // These are live DOM facts rather than hints parsed from PageController's text.
      // They let the server omit an unavailable field up front; Page.input/select still
      // recheck the live element at execution time because the DOM may change afterwards.
      if (element?.disabled) item.attrs.disabled = 'true'
      if (element?.readOnly) item.attrs.readonly = 'true'
      const owner = typeof element?.closest === 'function' ? element.closest('[name]') : null
      const rawField = explicitAttribute(owner || element, 'name')
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
