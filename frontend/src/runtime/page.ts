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
  actionElementFingerprint, boundIndexedContent, elementsFrom, hydrateFormFacts,
  hydrateLinkFacts, refIndex, type ElementLike, type ElementLookup,
} from './pageModel'
import {canonicalUrl} from './navigation'
import {MAX_CONTENT_CHARS, type Redactor} from './redaction'
import {WIRE_VERSION, type ElementRef, type PageObservation} from './types'
import browserContract from '@contract/browser_contract.json'

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
}

/** What one observation of the page found, before the session numbers and wraps it. */
export interface PageReading {
  observation: PageObservation
  fingerprints: Map<string, string>
  canonicalUrl: string
}

export class Page {
  constructor(private readonly controller: PageControllerApi,
              private readonly lookup: ElementLookup,
              private readonly redactor: Redactor) {}

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
  input(ref: unknown, text: string): Promise<unknown> {
    const element = this.element(ref)
    if (!element || element.isConnected === false) {
      throw new Error('bad_arguments: fill target is no longer on the page')
    }
    if (element.disabled || element.readOnly) {
      throw new Error('bad_arguments: fill target is disabled or read-only')
    }
    return this.controller.inputText(refIndex(ref), text)
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
    return this.controller.selectOption(refIndex(ref), label)
  }
  scroll(down: boolean, pages: number): Promise<unknown> {
    return this.controller.scroll({down, numPages: pages})
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
    const content = `${header}\n${boundedBody.content}\n${footer}`
    const map = this.controller.selectorMap
    const elements: ElementRef[] = hydrateFormFacts(hydrateLinkFacts(
      elementsFrom(content, epoch, this.redactor), map, this.lookup, this.redactor),
    map, this.lookup, this.redactor)
    const fingerprints = new Map<string, string>()
    for (const item of elements) fingerprints.set(item.ref, this.fingerprint(item.ref))
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
      },
      fingerprints,
      canonicalUrl: canonicalUrl(state.url),
    }
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
      'aria-label', 'aria-selected', 'aria-checked', 'aria-expanded',
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
  return {controller, lookup: getElementByIndex}
}
