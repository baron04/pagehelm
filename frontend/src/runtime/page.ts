/**
 * The page, as this Runtime reaches it: through PageController and nothing else.
 *
 * PageController is loaded from the application at run time and owns the selector map
 * that turns a server ref into a live element. Every lookup used to be written out as
 * `this.getElementByIndex(this.controller.selectorMap, refIndex(ref))` on a controller
 * that is `null` until startup finishes; this object exists only once it is loaded, so
 * a lookup cannot happen against nothing.
 */
import {
  actionElementFingerprint, boundIndexedContent, elementsFrom, hydrateButtonFacts, hydrateFormFacts,
  hydrateLinkFacts, hydrateRevealFacts, hydrateSelectionFacts, formFieldName,
  formControlLabel, formControlScope, isSensitiveFormControl, refIndex,
  revealable, type ElementLike, type ElementLookup,
} from './pageModel'
import {canonicalUrl} from './navigation'
import {selectedControlsFromPage} from './pageSelection'
import {MAX_CONTENT_CHARS, type Redactor} from './redaction'
import {WIRE_VERSION, type ElementRef, type PageObservation} from './types'
import browserContract from '@contract/browser_contract.json'
import {formInstance, localChoiceRemoval} from './formInstance'
import {ChoiceUnavailable, listboxCombobox, ownedListbox, optionText, selectedOption, visibleElement} from './combobox'
import {radioGroup, radioChoices, radioLabel, radioEnabled, radioSelectionMatches} from './radio'
import {sleep} from './contain'
import {suspendPanelHitTargets} from './panelObservation'

const ANNOTATION_STYLE_ID = 'woa-hide-page-controller-annotations'

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

export interface BrowserState {
  url: string
  title: string
  header?: string
  content: string
  footer?: string
}

/** The part of PageController this Runtime drives. */
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

export interface PageControllerApi {
  selectorMap: unknown
  getBrowserState(): Promise<BrowserState>
  clickElement(index: number): Promise<unknown>
  inputText(index: number, text: string): Promise<unknown>
  selectOption(index: number, text: string): Promise<unknown>
  scroll(options: {down: boolean; numPages: number; pixels?: number; index?: number}): Promise<unknown>
  dispose(): void
  addEventListener?(type: string, listener: EventListener): void
  removeEventListener?(type: string, listener: EventListener): void
}

interface PageControllerModule {
  PageController?: new (options: Record<string, unknown>) => PageControllerApi
  getElementByIndex?: ElementLookup
  readPageFacets?: () => unknown
  readFacetCandidates?: () => unknown
  applyPageFacet?: (ref: string, active: boolean) => Promise<unknown> | unknown
  readPageRecordRoute?: () => unknown
  readPageBlocker?: () => unknown
  readActiveView?: () => unknown
  readPageRoute?: () => unknown
  readPageQuery?: () => unknown
  readPageRead?: () => unknown
  readElementNavigation?: ElementNavigationReader
  readElementPageControl?: ElementNavigationReader
  openPage?: PageOpener
}

/**
 * The application opening one of its own pages in this document. It answers true only
 * when it now shows another address; anything else leaves the Runtime to load the page.
 */
export type PageOpener = (url: string) => Promise<unknown> | unknown
export type PageFacetReader = () => unknown
/** How long a menu is given to open under the pointer before its control is pressed. */
const REVEAL_HOVER_MS = 350

export type FacetCandidateReader = () => unknown
export type FacetApplier = (ref: string, active: boolean) => Promise<unknown> | unknown
export type PageRecordRouteReader = () => unknown
export type PageBlockerReader = () => unknown
export type ActiveViewReader = () => unknown
export type PageRouteReader = () => unknown
export type PageQueryReader = () => unknown
/** Application-owned target facts, never authorization to click. */
export type ElementNavigationReader = (element: ElementLike | null | undefined) => unknown
export interface PageHighlight { clear(): void; active(): boolean }
export interface FormHandoffField {
  ref?: string; field: string; control: string; value: string
  binding?: string; label?: string; scope?: string; instance?: string
  option_label?: string
}
export interface FormSubmitTarget {ref: string; label: string; scope: string}

export function formFieldMatches(element: ElementRef, binding: FormHandoffField): boolean {
  return element.role === binding.control && (!binding.scope ||
    element.attrs.form_scope === binding.scope) && (binding.binding === 'ref'
      ? element.ref === binding.ref && element.attrs.form_instance === binding.instance
      : binding.binding === 'label'
      ? !element.attrs.field && element.attrs.field_label === binding.label
      : element.attrs.field === binding.field)
}

/** What one observation of the page found, before the session numbers and wraps it. */
export interface PageReading {
  observation: PageObservation
  fingerprints: Map<string, string>
  canonicalUrl: string
}

export class Page {
  // Browser-only identities of at most twenty controls this Page actually entered.
  // No requested values, plans, permissions or persisted state are held here.
  private readonly enteredControls = new Map<string, {element: Element; instance: string; url: string}>()

  private enteredKey(field: string, label: string, scope: string): string {
    return JSON.stringify([field, label, scope])
  }
  private rememberEntered(element: ElementLike): void {
    if (!(element instanceof Element) || isSensitiveFormControl(element, this.redactor)) return
    const field = formFieldName(element) || '', label = field ? '' : formControlLabel(element) || ''
    const scope = formControlScope(element), instance = formInstance(element)
    if ((!field && !label) || !scope || !instance) return
    const held = this.enteredControls.values().next().value
    if (held && (held.instance !== instance || held.url !== canonicalUrl(location.href))) this.enteredControls.clear()
    const key = this.enteredKey(field, label, scope)
    const prior = this.enteredControls.get(key)
    if (prior && prior.element !== element && prior.element.isConnected) {
      this.enteredControls.delete(key)
      return
    }
    if (!prior && this.enteredControls.size >= 20) this.enteredControls.clear()
    this.enteredControls.set(key, {element, instance, url: canonicalUrl(location.href)})
  }
  private enteredControl(binding: FormHandoffField, observed: ElementRef[]): ElementLike | null {
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
  constructor(private readonly controller: PageControllerApi,
              private readonly lookup: ElementLookup,
              private readonly redactor: Redactor,
              private readonly readPageFacets?: PageFacetReader,
              private readonly readFacetCandidates?: FacetCandidateReader,
              private readonly applyPageFacet?: FacetApplier,
              private readonly readPageRecordRoute?: PageRecordRouteReader,
              private readonly readPageBlocker?: PageBlockerReader,
              private readonly readActiveView?: ActiveViewReader,
              private readonly readPageRoute?: PageRouteReader,
              private readonly readPageQuery?: PageQueryReader,
              private readonly interrupted: () => boolean = () => false,
              private readonly readElementNavigation?: ElementNavigationReader,
              private readonly readPageRead?: PageQueryReader,
              private readonly readElementPageControl?: ElementNavigationReader,
              private readonly panelContainer?: HTMLElement) {}

  /** App-owned loading is distinct from an error; unknown values remain blocking. */
  readiness(): {blocked: boolean; pending: boolean} {
    if (!this.readPageBlocker) return {blocked: false, pending: false}
    try {
      const state = this.readPageBlocker()
      return {blocked: state !== false && state !== 'pending', pending: state === 'pending'}
    } catch { return {blocked: true, pending: false} }
  }

  applyFacet(ref: unknown, active: unknown): Promise<unknown> {
    if (!this.readFacetCandidates || !this.applyPageFacet ||
        typeof ref !== 'string' || typeof active !== 'boolean') {
      throw new Error('bad_arguments: application facet operation unavailable')
    }
    const candidates = this.boundedCandidates()
    const candidate = candidates?.find(item => item.ref === ref)
    if (!candidate || candidate.active === active) {
      throw new Error('bad_arguments: facet candidate absent or already in requested state')
    }
    return Promise.resolve(this.applyPageFacet(ref, active))
  }

  facetCandidate(ref: unknown): {ref: string; kind: 'filter' | 'group';
    name: string; active: boolean} | undefined {
    return typeof ref === 'string'
      ? this.boundedCandidates()?.find(item => item.ref === ref) : undefined
  }

  /** The live element behind a server ref; an unknown ref is `bad_arguments`. */
  element(ref: unknown): ElementLike | null | undefined {
    return this.lookup(this.controller.selectorMap, refIndex(ref))
  }

  /** What the element under `ref` is now, in the terms an action's meaning depends on. */
  fingerprint(ref: unknown): string {
    try {
      const fingerprint = actionElementFingerprint(this.element(ref)), navigation = this.navigation(ref)
      return navigation ? JSON.stringify([fingerprint, navigation]) : fingerprint
    } catch {
      return 'missing'
    }
  }

  private navigation(ref: unknown): {kind: 'branch' | 'page'; path: string; expanded?: boolean} | undefined {
    try {
      const raw = this.readElementNavigation?.(this.element(ref)) as Record<string, unknown> | undefined
      if (!raw || !['branch', 'page'].includes(String(raw.kind)) || typeof raw.path !== 'string' ||
          raw.path.length > 500 || !/^\/(?!\/)[^?#\\\s]*$/.test(raw.path)) return undefined
      const url = new URL(raw.path, location.href)
      if (url.origin !== location.origin || url.pathname !== raw.path ||
          this.redactor.redactString(raw.path) !== raw.path) return undefined
      return {kind: raw.kind as 'branch' | 'page', path: raw.path,
        ...(raw.kind === 'branch' && typeof raw.expanded === 'boolean' ? {expanded: raw.expanded} : {})}
    } catch { return undefined }
  }

  click(ref: unknown): Promise<unknown> {
    const target = this.element(ref)
    if (target instanceof HTMLButtonElement && target.disabled) {
      throw new Error('bad_arguments: disabled buttons may only be observed or highlighted')
    }
    return this.controller.clickElement(refIndex(ref))
  }
  /** The controller's page state, with the application's rows-per-page control kept as
   * an element of its own.
   *
   * Inside an element the controller already lists -- a scrolling region is one -- it
   * lists a plain element only when something says that element is acted on by itself,
   * and one thing it asks is this window hook, when there is one: what listens on the
   * element. The control the application's page module names is answered for; every
   * other element is answered as it was. The hook is in place only for the reading.
   */
  private async browserState(): Promise<BrowserState> {
    const read = this.readElementPageControl
    if (!read) return this.controller.getBrowserState()
    const host = window as unknown as {getEventListenersForNode?: (node: Element) => Array<{type: string}>}
    const had = Object.prototype.hasOwnProperty.call(host, 'getEventListenersForNode')
    const earlier = host.getEventListenersForNode
    host.getEventListenersForNode = node => {
      let named = false
      try {
        named = node instanceof HTMLElement &&
          (read(node) as {kind?: unknown} | undefined)?.kind === 'page_size'
      } catch { named = false }
      return named ? [{type: 'click'}] : typeof earlier === 'function' ? earlier(node) : []
    }
    try {
      return await this.controller.getBrowserState()
    } finally {
      if (had && earlier) host.getEventListenersForNode = earlier
      else delete host.getEventListenersForNode
    }
  }

  /** Open a menu the control declares collapsed; refuse anything else (see `revealable`).
   *
   * Some menus open when the pointer comes over their control, and pressing such a control
   * closes them again. So the pointer comes first, as a person's would; the control is
   * pressed only if it still says it is collapsed a moment later.
   */
  async reveal(ref: unknown): Promise<unknown> {
    const element = this.element(ref)
    if (!revealable(element)) {
      throw new Error('bad_arguments: reveal target is not a collapsed menu control')
    }
    if (element instanceof HTMLElement) {
      const rect = element.getBoundingClientRect()
      const at = {clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2}
      if (typeof PointerEvent === 'function') {
        element.dispatchEvent(new PointerEvent('pointerover', {...at, bubbles: true, pointerType: 'mouse'}))
        element.dispatchEvent(new PointerEvent('pointerenter', {...at, pointerType: 'mouse'}))
      }
      element.dispatchEvent(new MouseEvent('mouseover', {...at, bubbles: true}))
      element.dispatchEvent(new MouseEvent('mouseenter', at))
      await sleep(REVEAL_HOVER_MS)
      if (element.isConnected && element.getAttribute('aria-expanded') === 'true') {
        return {success: true, message: 'menu opened under the pointer'}
      }
    }
    return this.controller.clickElement(refIndex(ref))
  }
  input(ref: unknown, text: string, commit = false): Promise<unknown> {
    const element = this.element(ref)
    if (!element || element.isConnected === false) {
      throw new Error('bad_arguments: fill target is no longer on the page')
    }
    if (element.disabled || element.readOnly) {
      throw new Error('bad_arguments: fill target is disabled or read-only')
    }
    return Promise.resolve(this.controller.inputText(refIndex(ref), text)).then(result => {
      if (result && (result as {success?: unknown}).success === true) {
        // An application-issued form draft commits the local editor before its fresh
        // observation. Ordinary typing keeps suggestion lists open. No submit is used.
        if (commit && (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement)) {
          if (!element.isConnected || element.disabled || element.readOnly) {
            throw new Error('execution_failed: entered form control changed before local commit')
          }
          element.dispatchEvent(new Event('change', {bubbles: true}))
          element.blur()
        }
        this.rememberEntered(element)
      }
      return result
    })
  }
  select(ref: unknown, text: string, optionLabel?: string): Promise<unknown> {
    const element = this.element(ref)
    if (!element || element.isConnected === false) {
      throw new Error('bad_arguments: select target is no longer on the page')
    }
    if (optionLabel !== undefined) {
      const group = radioGroup(element)
      if (group) return this.selectRadio(group, optionLabel, text)
      if (!listboxCombobox(element) || !optionLabel.trim() ||
          optionLabel.length > browserContract.form_handoff.max_label_chars ||
          isSensitiveFormControl(element, this.redactor)) {
        throw new Error('bad_arguments: select requires one enabled single-choice combobox')
      }
      return this.selectCombobox(refIndex(ref), element, optionLabel)
    }
    if (element.disabled || element.readOnly) {
      throw new Error('bad_arguments: select target is disabled or read-only')
    }
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
    if (!label || options.filter(option =>
      String(option.textContent ?? '').trim() === label).length !== 1) {
      throw new Error('bad_arguments: select label is absent or ambiguous')
    }
    return Promise.resolve(this.controller.selectOption(refIndex(ref), label)).then(result => {
      if (result && (result as {success?: unknown}).success === true) this.rememberEntered(element)
      return result
    })
  }
  private async selectRadio(group: HTMLElement, label: string, value: string): Promise<unknown> {
    const scope = formControlScope(group), instance = formInstance(group), url = location.href
    const name = formFieldName(group), fieldLabel = formControlLabel(group)
    const options = radioChoices(group), matches = options.filter(e => radioLabel(e) === label)
    const current = () => !this.interrupted() && location.href === url && radioGroup(group) === group &&
      formControlScope(group) === scope && formInstance(group) === instance &&
      formFieldName(group) === name && formControlLabel(group) === fieldLabel &&
      radioChoices(group).length === options.length && radioChoices(group).every((e, i) => e === options[i]) &&
      radioChoices(group).filter(e => radioLabel(e) === label).length === 1 &&
      radioLabel(matches[0]!) === label &&
      (!(matches[0] instanceof HTMLInputElement) || matches[0].value === value)
    if (!scope || !instance || !label.trim() || label.length > browserContract.form_handoff.max_label_chars ||
        isSensitiveFormControl(group, this.redactor) || matches.length !== 1 ||
        !radioEnabled(matches[0]!) || matches[0] instanceof HTMLInputElement && matches[0].value !== value ||
        !visibleElement(group) || !current()) {
      throw new Error('bad_arguments: radio choice is absent, ambiguous, unavailable, or changed')
    }
    if (radioSelectionMatches(group, label, value)) {
      this.rememberEntered(group)
      return {success: true}
    }
    const state = await this.read()
    const indexes = [...state.content.matchAll(/(?:^|\n)\s*\*?\[(\d+)]</g)].map(m => Number(m[1]))
      .filter(i => this.lookup(this.controller.selectorMap, i) === matches[0])
    if (indexes.length !== 1 || !current() || !radioEnabled(matches[0]!)) {
      throw new Error('execution_failed: radio choice has no unique fresh action reference')
    }
    const finishRemoval = localChoiceRemoval(group, [...this.enteredControls.values()]
      .filter(entry => entry.instance === instance).map(entry => entry.element))
    try {
      const result = await this.controller.clickElement(indexes[0]!)
      if (!result || (result as {success?: unknown}).success !== true) return result
      await sleep(50)
      if (!current() || !radioSelectionMatches(group, label, value)) {
        throw new Error('execution_failed: radio selection was not verified')
      }
      this.rememberEntered(group)
      return result
    } finally {
      finishRemoval()
    }
  }
  private async selectCombobox(index: number, element: HTMLInputElement, label: string): Promise<unknown> {
    const scope = formControlScope(element), instance = formInstance(element), url = location.href
    const labelBefore = formControlLabel(element), fieldBefore = formFieldName(element)
    const current = () => !this.interrupted() && location.href === url && element.isConnected &&
      listboxCombobox(element) && formControlScope(element) === scope && formInstance(element) === instance &&
      formControlLabel(element) === labelBefore && formFieldName(element) === fieldBefore
    const list = ownedListbox(element)
    if (!list || !scope || !instance || !current()) {
      throw new Error('bad_arguments: combobox has no unique current form listbox')
    }
    // Keep values browser-local. Equality of this complete local draft, together
    // with the fact no option was clicked, is stronger than an unchanged label.
    const draft = () => [...document.querySelectorAll('input,textarea,select,[role="radiogroup"],[role="textbox"],[contenteditable="true"]')]
      .filter(node => formControlScope(node) === scope && formInstance(node) === instance)
      .map(node => ({node, state: JSON.stringify([
        (node as HTMLInputElement).value ?? '', (node as HTMLInputElement).checked ?? false,
        node.matches('[contenteditable="true"],[role="textbox"]') ? node.textContent : null,
        node.getAttribute('aria-checked'), node.getAttribute('aria-selected'),
        node instanceof HTMLInputElement && listboxCombobox(node) ? selectedOption(node) : null,
      ])}))
    const beforeDraft = draft()
    if (selectedOption(element) === label) {
      this.rememberEntered(element)
      return {success: true}
    }
    if (element.getAttribute('aria-expanded') !== 'true') {
      const opened = await this.controller.clickElement(index)
      if (!opened || (opened as {success?: unknown}).success !== true) return opened
    }
    // What the last scroll was asked for over what it moved: PageController scales the
    // distance it is given, by a factor that is its own.
    let gain = 1
    // Eight moves may make the option visible only after the last move. Inspect
    // that result once more; the final inspection may choose, but never scrolls.
    for (let attempt = 0; attempt <= 8; attempt++) {
      const state = await this.read()
      if (!current() || ownedListbox(element) !== list) {
        throw new Error('execution_failed: combobox changed before selection')
      }
      if (visibleElement(list)) {
        const matches = [...list.querySelectorAll('[role="option"]')].filter(option =>
          option.closest('[role="listbox"]') === list && optionText(option) === label)
        if (matches.length === 0) {
          const candidate_labels = [...list.querySelectorAll('[role="option"]')]
            .filter(option => option.closest('[role="listbox"]') === list &&
              option.getAttribute('aria-disabled') !== 'true' && !option.hasAttribute('disabled'))
            .map(option => optionText(option).trim())
            .filter(label => label.length > 0)
            .slice(0, browserContract.choice_failure.max_candidates)
            .map(label => label.slice(0, browserContract.choice_failure.max_label_chars))
          // Try the current control to dismiss the list. Some widgets only open on
          // input clicks: an open popup alone is not a changed draft or selection.
          if (element.getAttribute('aria-expanded') === 'true') {
            const controls = [...state.content.matchAll(/(?:^|\n)\s*\*?\[(\d+)]</g)]
              .map(m => Number(m[1])).filter(i => this.lookup(this.controller.selectorMap, i) === element)
            if (controls.length !== 1 || !current()) {
              throw new Error('execution_failed: combobox has no unique fresh close reference')
            }
            await this.controller.clickElement(controls[0]!)
            await sleep(50)
          }
          const afterDraft = draft()
          const bound = current() && ownedListbox(element) === list
          const unchanged = beforeDraft.length === afterDraft.length &&
            beforeDraft.every((item, i) => item.node === afterDraft[i]!.node && item.state === afterDraft[i]!.state)
          throw new ChoiceUnavailable(candidate_labels, bound && unchanged,
            !bound ? 'the form binding changed' : 'local field values changed')
        }
        if (matches.length !== 1 || !visibleElement(matches[0]!) ||
            matches[0]!.getAttribute('aria-disabled') === 'true' ||
            matches[0]!.hasAttribute('disabled')) {
          throw new Error('execution_failed: combobox option is absent, ambiguous, or disabled')
        }
        const option = matches[0]!
        const indexed = [...state.content.matchAll(/(?:^|\n)\s*\*?\[(\d+)]</g)].map(m => Number(m[1]))
        const indexes = indexed.filter(i => this.lookup(this.controller.selectorMap, i) === option)
        if (indexes.length === 0) {
          if (attempt === 8) break
          // Reuse PageController's scoped scroll. Anchor it to an indexed option in
          // this owned menu; never scroll an unrelated popup or choose an old index.
          const anchors = indexed.filter(i => {
            const node = this.lookup(this.controller.selectorMap, i)
            return node instanceof Element && node.getAttribute('role') === 'option' &&
              node.closest('[role="listbox"]') === list && visibleElement(node)
          })
          if (!anchors.length || !current()) {
            throw new Error('execution_failed: combobox menu has no current scroll target')
          }
          // Ask for the distance that brings the option into the menu's visible part, so
          // a long menu is reached in a few scrolls rather than a page at a time.
          const view = scrollViewport(option, list).getBoundingClientRect()
          const before = option.getBoundingClientRect()
          const down = before.top >= view.top
          const away = down ? before.bottom - view.bottom : view.top - before.top
          const pixels = Math.min(400_000, Math.max(40, Math.ceil(
            (Math.max(away, 0) + Math.min(view.height / 2, 80)) * gain)))
          const scrolled = await this.controller.scroll({down, numPages: 1, pixels, index: anchors[0]!})
          if (!scrolled || (scrolled as {success?: unknown}).success !== true) return scrolled
          await sleep(50)
          const moved = Math.abs(option.getBoundingClientRect().top - before.top)
          if (moved > 0.5) gain = Math.min(16, Math.max(1, pixels / moved))
          continue
        }
        if (indexes.length !== 1 || !current()) {
          throw new Error('execution_failed: combobox option is not uniquely actionable')
        }
        const result = await this.controller.clickElement(indexes[0]!)
        if (!result || (result as {success?: unknown}).success !== true) return result
        await sleep(50)
        if (!current() || ownedListbox(element) !== list || selectedOption(element) !== label) {
          throw new Error('execution_failed: combobox selection was not verified')
        }
        this.rememberEntered(element)
        return result
      }
      await sleep(50)
    }
    throw new Error('execution_failed: combobox option did not become actionable')
  }
  scroll(down: boolean, pages: number, ref?: unknown): Promise<unknown> {
    if (ref === undefined) return this.controller.scroll({down, numPages: pages})
    const anchor = this.element(ref)
    if (!(anchor instanceof Element) || !anchor.isConnected) {
      throw new Error('bad_arguments: scroll anchor is not a current element')
    }
    // The nearest container around the anchor that still has room in that direction.
    // PageController's own anchored scroll gives up ten ancestors above the element, which
    // a row inside a table inside a card inside a scrolling layout never reaches.
    for (let node: Element | null = anchor.parentElement; node; node = node.parentElement) {
      const room = down ? node.scrollHeight - node.clientHeight - node.scrollTop : node.scrollTop
      if (room > 1 && /(auto|scroll|overlay)/.test(getComputedStyle(node).overflowY)) {
        node.scrollTop += (down ? 1 : -1) * Math.max(40, node.clientHeight) * pages
        return Promise.resolve({success: true, message: 'container scroll observed'})
      }
    }
    return this.controller.scroll({down, numPages: pages})
  }

  /** Check an adapter-issued draft against live controls without sending their values back. */
  verifyFormFields(fields: unknown, observed: ElementRef[]): boolean {
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
        try { live = this.element(binding.ref) } catch { return false }
      } else {
        if (matches.length > 1) return false
        live = this.enteredControl(binding, observed)
      }
      if (binding.control === 'radiogroup') live = radioGroup(live)
      if (!live || live.isConnected === false || live.disabled ||
          live.readOnly && !(binding.option_label && listboxCombobox(live))) return false
      if (binding.scope && formControlScope(live) !== binding.scope) return false
      if (isSensitiveFormControl(live, this.redactor)) return false
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
          listboxCombobox(live) && selectedOption(live) === binding.option_label
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
  verifyFormSubmit(target: unknown, fields: unknown, observed: ElementRef[]): target is FormSubmitTarget {
    if (!target || typeof target !== 'object' || !Array.isArray(fields)) return false
    const candidate = target as FormSubmitTarget
    if (typeof candidate.ref !== 'string' || typeof candidate.label !== 'string' ||
        !candidate.label || candidate.label.length > browserContract.form_handoff.max_label_chars || !candidate.scope ||
        fields.some(field => !field || field.scope !== candidate.scope)) return false
    const matches = observed.filter(element => element.role === 'button' &&
      element.name === candidate.label && element.attrs.form_scope === candidate.scope)
    if (matches.length !== 1 || matches[0]!.ref !== candidate.ref) return false
    let live: ElementLike | null | undefined
    try { live = this.element(candidate.ref) } catch { return false }
    if (!(live instanceof HTMLElement) || !visibleElement(live) || ('readOnly' in live && live.readOnly === true) ||
        String(live.tagName || '').toLowerCase() !== 'button' ||
        formControlScope(live) !== candidate.scope) return false
    const label = String(live.textContent || '').replace(/\s+/g, ' ').trim()
    return label === candidate.label
  }

  /** Mark one observed control without focusing, clicking, or covering its hit target. */
  highlight(ref: unknown): PageHighlight {
    const element = this.element(ref)
    if (!(element instanceof HTMLElement || element instanceof SVGElement) ||
        !element.isConnected) {
      throw new Error('bad_arguments: highlight target is no longer on the page')
    }
    element.scrollIntoView?.({block: 'center', inline: 'nearest', behavior: 'auto'})
    const rect = element.getBoundingClientRect()
    const computed = getComputedStyle(element)
    if (rect.width <= 0 || rect.height <= 0 || computed.display === 'none' ||
        computed.visibility === 'hidden') {
      throw new Error('bad_arguments: highlight target is not visible')
    }
    const original = ['outline', 'outline-offset'].map(property => ({
      property, value: element.style.getPropertyValue(property),
      priority: element.style.getPropertyPriority(property),
    }))
    element.style.setProperty('outline', '3px solid Highlight', 'important')
    element.style.setProperty('outline-offset', '3px', 'important')
    const applied = new Map(original.map(saved => [
      saved.property, element.style.getPropertyValue(saved.property),
    ]))
    let active = true
    const clear = () => {
      if (!active) return
      active = false
      observer.disconnect()
      for (const saved of original) {
        // An application that changed this style after handoff owns its new value.
        if (element.style.getPropertyPriority(saved.property) !== 'important' ||
            element.style.getPropertyValue(saved.property) !== applied.get(saved.property)) continue
        if (saved.value) {
          element.style.setProperty(saved.property, saved.value, saved.priority)
        } else {
          element.style.removeProperty(saved.property)
        }
      }
    }
    const observer = new MutationObserver(() => {
      if (!element.isConnected) clear()
    })
    observer.observe(document.documentElement, {childList: true, subtree: true})
    return {clear, active: () => active && element.isConnected && original.every(saved =>
      element.style.getPropertyPriority(saved.property) === 'important' &&
      element.style.getPropertyValue(saved.property) === applied.get(saved.property))}
  }

  /** PageController's reading of the page, with its debugging overlay removed. */
  async read(): Promise<BrowserState> {
    let restore = () => {}
    const after: EventListener = () => { restore(); restore = () => {} }
    const before: EventListener = () => {
      after(new Event('afterUpdate'))
      restore = suspendPanelHitTargets(this.panelContainer)
    }
    const events = this.controller.addEventListener && this.controller.removeEventListener
    if (events) {
      this.controller.addEventListener!('beforeUpdate', before)
      this.controller.addEventListener!('afterUpdate', after)
    }
    let state: BrowserState
    try {
      state = await this.browserState()
    } finally {
      after(new Event('afterUpdate'))
      if (events) {
        this.controller.removeEventListener!('beforeUpdate', before)
        this.controller.removeEventListener!('afterUpdate', after)
      }
    }
    // PageController uses numbered overlays while building its private selector map. They
    // are debugging aids, not product UI; removing them does not change the stored refs.
    document.getElementById('playwright-highlight-container')?.remove()
    return state
  }

  /** The server's view of one reading, numbered with the epoch the session assigned it. */
  describe(state: BrowserState, epoch: number): PageReading {
    const header = this.redactor.sanitizePageContent(state.header ?? '').slice(0, 2_000)
    const footer = this.redactor.sanitizePageContent(state.footer ?? '').slice(0, 2_000)
    const availableBodyChars = Math.max(0, MAX_CONTENT_CHARS - header.length - footer.length - 2)
    const sanitizedBody = this.redactor.sanitizePageContent(state.content)
    const charBoundedBody = sanitizedBody.slice(0, availableBodyChars)
    const boundedBody = boundIndexedContent(charBoundedBody)
    // Keep PageController's scroll hints even when its body was bounded. Without the footer
    // the Agent cannot distinguish a short page from one that needs another observation.
    const map = this.controller.selectorMap
    // A component wrapper's credential identity may not appear in the controller text.
    // Remove that entire indexed control record before either projection leaves the page.
    const content = `${header}\n${boundedBody.content}\n${footer}`.replace(
      /(?:^|\n)\s*\*?\[(\d+)]<(input|textarea|select)\b[\s\S]*?\s*\/>?(?=\n|$)/g,
      (record, index: string) => {
        try {
          if (isSensitiveFormControl(this.lookup(map, Number(index)), this.redactor)) {
            return `${record.startsWith('\n') ? '\n' : ''}[${index}]<input type=password />`
          }
        } catch { /* The ordinary summary redactor still applies to stale records. */ }
        return record
      })
    const elements: ElementRef[] = hydrateRevealFacts(hydrateSelectionFacts(hydrateButtonFacts(
      hydrateFormFacts(hydrateLinkFacts(elementsFrom(content, epoch, this.redactor), map,
        this.lookup, this.redactor), map, this.lookup, this.redactor), map, this.lookup,
    this.redactor), map, this.lookup), map, this.lookup)
    const fingerprints = new Map<string, string>()
    for (const item of elements) {
      try {
        const element = this.element(item.ref)
        if (!(element instanceof HTMLElement) || !element.isConnected) continue
        const fact = this.readElementPageControl?.(element) as
          {kind?: unknown; page?: unknown; pageSize?: unknown; field?: unknown; queryValue?: unknown} | undefined
        if (fact?.kind === 'pagination' && Number.isSafeInteger(fact.page) &&
            Number(fact.page) > 0 && Number.isSafeInteger(fact.pageSize) && Number(fact.pageSize) > 0) {
          item.attrs.page_control = 'pagination'
          item.attrs.page_number = String(fact.page)
          item.attrs.page_size = String(fact.pageSize)
        } else if (fact?.kind === 'page_size' && Number.isSafeInteger(fact.pageSize) &&
            Number(fact.pageSize) > 0) {
          item.attrs.page_control = 'page_size'
          item.attrs.page_size = String(fact.pageSize)
        } else if (fact?.kind === 'query') {
          item.attrs.page_control = 'query'
          if (typeof fact.field === 'string' && /^[A-Za-z][A-Za-z0-9_]*$/.test(fact.field) &&
              fact.field.length <= browserContract.observation.max_query_key_chars &&
              !this.redactor.isSensitiveName(fact.field)) {
            item.attrs.query_field = fact.field
            if (typeof fact.queryValue === 'string' && /^(?:[0-9]{1,20}|true|false)$/.test(fact.queryValue) &&
                this.redactor.redactString(fact.queryValue) === fact.queryValue &&
                !isSensitiveFormControl(element, this.redactor)) item.attrs.query_value = fact.queryValue
          }
        }
      } catch { /* An unavailable application fact grants no control. */ }
    }
    for (const item of elements) {
      const navigation = this.navigation(item.ref)
      if (navigation) {
        item.attrs.navigation_kind = navigation.kind
        item.attrs.navigation_path = navigation.path
        if (navigation.expanded !== undefined) item.attrs['aria-expanded'] = String(navigation.expanded)
        if (navigation.kind === 'page') item.attrs.href = navigation.path
      }
      fingerprints.set(item.ref, this.fingerprint(item.ref))
    }
    const facets = this.readPageFacets ? this.boundedFacets() : undefined
    const candidates = this.readFacetCandidates ? this.boundedCandidates() : undefined
    const recordRoute = this.boundedRecordRoute()
    const activeView = this.boundedActiveView()
    const pageRoute = this.boundedPageRoute()
    const pageQuery = pageRoute === undefined ? undefined : this.boundedPageQuery()
    const pageRead = pageRoute === undefined ? undefined : this.boundedPageRead()
    return {
      observation: {
        schema_version: WIRE_VERSION,
        epoch,
        url: this.redactor.redactString(state.url),
        title: this.redactor.redactString(state.title).slice(0, 300),
        landmarks: [],
        elements,
        content,
        messages: [],
        truncated: (state.content.length > availableBodyChars || boundedBody.truncated),
        selected_controls: selectedControlsFromPage(document, this.redactor),
        ...(activeView === undefined ? {} : {active_view: activeView}),
        ...(facets === undefined ? {} : {page_facets: facets}),
        ...(candidates === undefined ? {} : {facet_candidates: candidates}),
        ...(recordRoute === undefined ? {} : {record_route: recordRoute}),
        ...(pageRoute === undefined ? {} : {page_route: pageRoute}),
        ...(pageQuery === undefined ? {} : {page_query: pageQuery}),
        ...(pageRead === undefined ? {} : {page_read: pageRead}),
      },
      fingerprints,
      canonicalUrl: canonicalUrl(state.url),
    }
  }

  private boundedRecordRoute(): string | undefined {
    let raw: unknown
    try { raw = this.readPageRecordRoute?.() } catch { return undefined }
    if (typeof raw !== 'string' || raw.length > 1000 || !raw.startsWith('/') ||
        raw.startsWith('//') || /[?#\\]/.test(raw) ||
        Array.from(raw).some(char => char.charCodeAt(0) <= 32 || char.charCodeAt(0) === 127) ||
        raw.split('/').some(part => part === '.' || part === '..')) return undefined
    const safe = this.redactor.redactString(raw)
    return safe === raw ? raw : undefined
  }

  private boundedPageRead(): PageObservation['page_read'] {
    try {
      const raw = this.readPageRead?.()
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined
      const r = raw as Record<string, unknown>
      if (Object.keys(r).sort().join(',') !== 'page,page_size,path,query_digest,query_fields,revision,rows,total' ||
          typeof r.path !== 'string' || r.path.length > browserContract.observation.max_page_route_chars ||
          !/^\/(?!\/)[^?#\\\s]*$/.test(r.path) || r.path.split('/').some(p => p === '.' || p === '..') ||
          this.redactor.redactString(r.path) !== r.path || typeof r.query_digest !== 'string' ||
          !/^[0-9a-f]{64}$/.test(r.query_digest) ||
          ['page', 'page_size', 'revision', 'rows', 'total'].some(k =>
            !Number.isSafeInteger(r[k]) || Number(r[k]) < 0) ||
          Math.min(Number(r.page), Number(r.page_size), Number(r.revision)) <= 0 ||
          Number(r.rows) > Number(r.page_size) || Number(r.rows) > Number(r.total)) return undefined
      if (!r.query_fields || typeof r.query_fields !== 'object' || Array.isArray(r.query_fields)) return undefined
      const fields = Object.entries(r.query_fields)
      if (fields.length > browserContract.observation.max_query_parameters || fields.some(([key, value]) =>
        !/^[A-Za-z][A-Za-z0-9_]*(?:\[[0-9]{1,2}\])?$/.test(key) ||
        key.length > browserContract.observation.max_query_key_chars || this.redactor.isSensitiveName(key) ||
        typeof value !== 'string' || !/^[0-9a-f]{64}$/.test(value))) return undefined
      return {path: r.path, query_digest: r.query_digest, query_fields: Object.fromEntries(fields), revision: Number(r.revision),
        page: Number(r.page), page_size: Number(r.page_size), total: Number(r.total), rows: Number(r.rows)}
    } catch { return undefined }
  }

  private boundedPageQuery(): PageObservation['page_query'] {
    try {
      const raw = this.readPageQuery?.()
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined
      const q = raw as Record<string, unknown>
      if (Object.keys(q).sort().join(',') !== 'parameters,path' || typeof q.path !== 'string' ||
          q.path.length > browserContract.observation.max_page_route_chars || !q.path.startsWith('/') ||
          q.path.startsWith('//') || /[?#\\]/.test(q.path) ||
          Array.from(q.path).some(c => c.charCodeAt(0) <= 32 || c.charCodeAt(0) === 127) ||
          q.path.split('/').some(p => p === '.' || p === '..') || this.redactor.redactString(q.path) !== q.path ||
          !q.parameters || typeof q.parameters !== 'object' || Array.isArray(q.parameters)) return undefined
      const pairs = Object.entries(q.parameters)
      if (pairs.length > browserContract.observation.max_query_parameters) return undefined
      const parameters: Record<string, string> = {}
      for (const [key, value] of pairs) {
        if (!/^[A-Za-z][A-Za-z0-9_]*$/.test(key) || key.length > browserContract.observation.max_query_key_chars ||
            this.redactor.isSensitiveName(key) || typeof value !== 'string' ||
            value.length > browserContract.observation.max_query_value_chars ||
            this.redactor.redactString(value) !== value) return undefined
        parameters[key] = value
      }
      return {path: q.path, parameters}
    } catch { return undefined }
  }

  private boundedPageRoute(): string | undefined {
    let raw: unknown
    try { raw = this.readPageRoute?.() } catch { return undefined }
    if (typeof raw !== 'string' || raw.length > browserContract.observation.max_page_route_chars ||
        !raw.startsWith('/') || raw.startsWith('//') || /[?#\\]/.test(raw) ||
        Array.from(raw).some(char => char.charCodeAt(0) <= 32 || char.charCodeAt(0) === 127) ||
        raw.split('/').some(part => part === '.' || part === '..')) return undefined
    const safe = this.redactor.redactString(raw)
    return safe === raw ? raw : undefined
  }

  private boundedActiveView(): string | undefined {
    let raw: unknown
    try { raw = this.readActiveView?.() } catch { return undefined }
    if (typeof raw !== 'string') return undefined
    const view = raw.replace(/\s+/g, ' ').trim()
    if (!view || view.length > browserContract.observation.max_selected_control_chars ||
        this.redactor.isSensitiveName(view)) return undefined
    const safe = this.redactor.redactString(view)
    return safe === view ? view : undefined
  }

  private boundedFacets(): Array<{kind: 'filter' | 'group'; name: string}> | undefined {
    let raw: unknown
    try { raw = this.readPageFacets?.() } catch { return undefined }
    if (!Array.isArray(raw)) return undefined
    const result: Array<{kind: 'filter' | 'group'; name: string}> = []
    for (const item of raw.slice(0, browserContract.observation.max_page_facets)) {
      if (!item || typeof item !== 'object') continue
      const fact = item as Record<string, unknown>
      if (fact.kind !== 'filter' && fact.kind !== 'group') continue
      if (typeof fact.name !== 'string') continue
      const name = fact.name.replace(/\s+/g, ' ').trim()
      if (!name || this.redactor.isSensitiveName(name) ||
          this.redactor.isSensitiveName(name.replace(/\s+/g, '_'))) continue
      const safe = this.redactor.redactString(name).slice(
        0, browserContract.observation.max_selected_control_chars)
      if (safe && !result.some(existing => existing.kind === fact.kind &&
          existing.name === safe)) result.push({kind: fact.kind, name: safe})
    }
    return result
  }

  private boundedCandidates(): Array<{ref: string; kind: 'filter' | 'group';
    name: string; active: boolean}> | undefined {
    let raw: unknown
    try { raw = this.readFacetCandidates?.() } catch { return undefined }
    if (!Array.isArray(raw)) return undefined
    const result: Array<{ref: string; kind: 'filter' | 'group'; name: string;
      active: boolean}> = []
    for (const item of raw.slice(0, browserContract.observation.max_facet_candidates)) {
      if (!item || typeof item !== 'object') continue
      const choice = item as Record<string, unknown>
      if ((choice.kind !== 'filter' && choice.kind !== 'group') ||
          typeof choice.ref !== 'string' || !/^[a-zA-Z0-9_-]{1,64}$/.test(choice.ref) ||
          typeof choice.name !== 'string' || typeof choice.active !== 'boolean') continue
      const name = choice.name.replace(/\s+/g, ' ').trim()
      if (!name || this.redactor.isSensitiveName(name) ||
          this.redactor.isSensitiveName(name.replace(/\s+/g, '_'))) continue
      const safe = this.redactor.redactString(name).slice(
        0, browserContract.observation.max_selected_control_chars)
      if (safe && !result.some(existing => existing.ref === choice.ref)) {
        result.push({ref: choice.ref, kind: choice.kind, name: safe, active: choice.active})
      }
    }
    return result
  }

  dispose(): void { this.controller.dispose() }
}

function hideAnnotations(): void {
  if (document.getElementById(ANNOTATION_STYLE_ID)) return
  const style = document.createElement('style')
  style.id = ANNOTATION_STYLE_ID
  style.textContent = '#playwright-highlight-container{display:none!important}'
  document.documentElement.appendChild(style)
}

/** A loaded PageController and the lookup that resolves its indices to live elements. */
export interface LoadedPageController {
  controller: PageControllerApi
  lookup: ElementLookup
  readPageFacets?: PageFacetReader
  readFacetCandidates?: FacetCandidateReader
  applyPageFacet?: FacetApplier
  readPageRecordRoute?: PageRecordRouteReader
  readPageBlocker?: PageBlockerReader
  readActiveView?: ActiveViewReader
  readPageRoute?: PageRouteReader
  readPageQuery?: PageQueryReader
  readPageRead?: PageQueryReader
  readElementNavigation?: ElementNavigationReader
  readElementPageControl?: ElementNavigationReader
  openPage?: PageOpener
}

/** The fixed extraction policy qualified by the product Runtime. */
export function pageControllerOptions(exclude: Element[]): Record<string, unknown> {
  let disabledButtons: HTMLButtonElement[] = []
  return {
    enableMask: false,
    highlightOpacity: 0,
    highlightLabelOpacity: 0,
    // A full-page scan grows with the whole DOM and bypasses PageController's top-layer
    // check. Observe the current viewport; the server can request a bounded scroll and then
    // reason from a fresh observation.
    viewportExpansion: browserContract.observation.viewport_expansion,
    keepSemanticTags: true,
    // PageController otherwise omits disabled buttons entirely. Its own dynamic
    // whitelist supplies real indexed elements for observation/highlight only;
    // ordinary visibility/top-layer checks and the disabled click guard still apply.
    interactiveWhitelist: Array.from({length: browserContract.observation.max_elements}, (_, index) => () => {
      if (index === 0) disabledButtons = [...document.querySelectorAll<HTMLButtonElement>('button:disabled')]
        .filter(button => Boolean(formControlScope(button)) && visibleElement(button)
          && !exclude.some(root => root.contains(button)))
        .slice(0, browserContract.observation.max_elements)
      return disabledButtons[index]
    }),
    includeAttributes: [
      'href', 'action', 'method', 'target', 'formaction', 'formmethod', 'formtarget',
      'type', 'name', 'id', 'value',
      'role', 'aria-haspopup', 'aria-controls', 'aria-owns', 'aria-disabled',
      'aria-label', 'aria-selected', 'aria-checked', 'aria-expanded', 'title', 'data-tooltip',
      'aria-pressed',
      'placeholder', 'autocomplete',
    ],
    interactiveBlacklist: exclude,
  }
}

/** Load the application's PageController, excluding the assistant's own container. */
export async function loadPageController(moduleUrl: string | undefined,
                                         exclude: Element[]): Promise<LoadedPageController> {
  if (!moduleUrl) throw new Error('pageControllerModule must be an absolute http(s) module URL')
  const url = new URL(moduleUrl, location.href)
  if (!['http:', 'https:'].includes(url.protocol)) {
    throw new Error('pageControllerModule must be an absolute http(s) module URL')
  }
  hideAnnotations()
  const module = await import(/* @vite-ignore */ url.href) as PageControllerModule
  const {PageController, getElementByIndex} = module
  if (!PageController || !getElementByIndex) {
    throw new Error('pageControllerModule does not expose the required PageController API')
  }
  const controller = new PageController(pageControllerOptions(exclude))
  return {controller, lookup: getElementByIndex,
    ...(typeof module.readPageFacets === 'function'
      ? {readPageFacets: module.readPageFacets} : {}),
    ...(typeof module.readFacetCandidates === 'function'
      ? {readFacetCandidates: module.readFacetCandidates} : {}),
    ...(typeof module.applyPageFacet === 'function'
      ? {applyPageFacet: module.applyPageFacet} : {}),
    ...(typeof module.readPageRecordRoute === 'function'
      ? {readPageRecordRoute: module.readPageRecordRoute} : {}),
    ...(typeof module.readPageBlocker === 'function'
      ? {readPageBlocker: module.readPageBlocker} : {}),
    ...(typeof module.readActiveView === 'function'
      ? {readActiveView: module.readActiveView} : {}),
    ...(typeof module.readPageRoute === 'function'
      ? {readPageRoute: module.readPageRoute} : {}),
    ...(typeof module.readPageQuery === 'function'
      ? {readPageQuery: module.readPageQuery} : {}),
    ...(typeof module.readPageRead === 'function'
      ? {readPageRead: module.readPageRead} : {}),
    ...(typeof module.readElementPageControl === 'function'
      ? {readElementPageControl: module.readElementPageControl} : {}),
    ...(typeof module.readElementNavigation === 'function'
      ? {readElementNavigation: module.readElementNavigation} : {}),
    ...(typeof module.openPage === 'function' ? {openPage: module.openPage} : {})}
}
