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
import {formInstance} from './formInstance'

const ANNOTATION_STYLE_ID = 'woa-hide-page-controller-annotations'

export interface BrowserState {
  url: string
  title: string
  header?: string
  content: string
  footer?: string
}

/** The part of PageController this Runtime drives. */
export interface PageControllerApi {
  selectorMap: unknown
  getBrowserState(): Promise<BrowserState>
  clickElement(index: number): Promise<unknown>
  inputText(index: number, text: string): Promise<unknown>
  selectOption(index: number, text: string): Promise<unknown>
  scroll(options: {down: boolean; numPages: number}): Promise<unknown>
  dispose(): void
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
}

export type PageFacetReader = () => unknown
export type FacetCandidateReader = () => unknown
export type FacetApplier = (ref: string, active: boolean) => Promise<unknown> | unknown
export type PageRecordRouteReader = () => unknown
export type PageBlockerReader = () => unknown
export type ActiveViewReader = () => unknown
export interface PageHighlight { clear(): void; active(): boolean }
export interface FormHandoffField {
  ref?: string; field: string; control: string; value: string
  binding?: string; label?: string; scope?: string
}
export interface FormSubmitTarget {ref: string; label: string; scope: string}

export function formFieldMatches(element: ElementRef, binding: FormHandoffField): boolean {
  return element.role === binding.control && (!binding.scope ||
    element.attrs.form_scope === binding.scope) && (binding.binding === 'label'
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
              private readonly readActiveView?: ActiveViewReader) {}

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
      return actionElementFingerprint(this.element(ref))
    } catch {
      return 'missing'
    }
  }

  click(ref: unknown): Promise<unknown> { return this.controller.clickElement(refIndex(ref)) }
  /** Open a menu the control declares collapsed; refuse anything else (see `revealable`). */
  reveal(ref: unknown): Promise<unknown> {
    if (!revealable(this.element(ref))) {
      throw new Error('bad_arguments: reveal target is not a collapsed menu control')
    }
    return this.controller.clickElement(refIndex(ref))
  }
  input(ref: unknown, text: string): Promise<unknown> {
    const element = this.element(ref)
    if (!element || element.isConnected === false) {
      throw new Error('bad_arguments: fill target is no longer on the page')
    }
    if (element.disabled || element.readOnly) {
      throw new Error('bad_arguments: fill target is disabled or read-only')
    }
    return Promise.resolve(this.controller.inputText(refIndex(ref), text)).then(result => {
      if (result && (result as {success?: unknown}).success === true) this.rememberEntered(element)
      return result
    })
  }
  select(ref: unknown, text: string): Promise<unknown> {
    const element = this.element(ref)
    if (!element || element.isConnected === false) {
      throw new Error('bad_arguments: select target is no longer on the page')
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
  scroll(down: boolean, pages: number): Promise<unknown> {
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
          !['field', 'label'].includes(binding.binding || 'field') ||
          (binding.binding === 'label' && (!binding.label || !binding.scope)) ||
          !['textbox', 'combobox', 'select'].includes(binding.control) ||
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
      if (!live || live.isConnected === false || live.disabled || live.readOnly) return false
      if (binding.scope && formControlScope(live) !== binding.scope) return false
      if (isSensitiveFormControl(live, this.redactor)) return false
      if (binding.binding === 'label') {
        if (formFieldName(live) || formControlLabel(live) !== binding.label) return false
      } else if (formFieldName(live) !== binding.field) return false
      const tag = String(live.tagName || '').toLowerCase()
      const control = tag === 'select' ? 'select' : live.getAttribute?.('role') === 'combobox' ? 'combobox' : 'textbox'
      if (!binding.ref && (!['input', 'textarea', 'select'].includes(tag) || control !== binding.control)) return false
      if (binding.control === 'select' && tag !== 'select') {
        return false
      }
      return String(live.value ?? '') === binding.value
    })
  }

  /** Verify one server-owned submit target; highlighting never activates it. */
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
    if (!live || live.isConnected === false || live.disabled || live.readOnly ||
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
    const state = await this.controller.getBrowserState()
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
    for (const item of elements) fingerprints.set(item.ref, this.fingerprint(item.ref))
    const facets = this.readPageFacets ? this.boundedFacets() : undefined
    const candidates = this.readFacetCandidates ? this.boundedCandidates() : undefined
    const recordRoute = this.boundedRecordRoute()
    const activeView = this.boundedActiveView()
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
}

/** The fixed extraction policy qualified by the product Runtime. */
export function pageControllerOptions(exclude: Element[]): Record<string, unknown> {
  return {
    enableMask: false,
    highlightOpacity: 0,
    highlightLabelOpacity: 0,
    // A full-page scan grows with the whole DOM and bypasses PageController's top-layer
    // check. Observe the current viewport; the server can request a bounded scroll and then
    // reason from a fresh observation.
    viewportExpansion: browserContract.observation.viewport_expansion,
    keepSemanticTags: true,
    includeAttributes: [
      'href', 'action', 'method', 'target', 'formaction', 'formmethod', 'formtarget',
      'type', 'name', 'id', 'value',
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
      ? {readActiveView: module.readActiveView} : {})}
}
