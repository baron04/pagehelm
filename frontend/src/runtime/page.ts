/**
 * The page, as this Runtime reaches it: through PageController and nothing else.
 *
 * PageController is loaded from the application at run time and owns the selector map
 * that turns a server ref into a live element. Every lookup used to be written out as
 * `this.getElementByIndex(this.controller.selectorMap, refIndex(ref))` on a controller
 * that is `null` until startup finishes; this object exists only once it is loaded, so
 * a lookup cannot happen against nothing.
 */
import browserContract from '@contract/browser_contract.json'
import {visibleElement} from './combobox'
import {sleep} from './contain'
import type {FormModelRead, RowRecordRead} from './formModel'
import {
  EnteredControlTracker, ensureHandoffFormOpenAndWritable, FormClosedOrReadonlyError,
  formFieldMatches, inputFormControl, readPageFormModel, readPageRowRecord, selectFormControl,
  verifyFormFields, verifyFormSubmit,
  type ElementReadFn, type FormHandoffField, type FormSubmitTarget,
} from './pageForm'
import {guideSubmitControl, highlightControl, type PageHighlight} from './pageHighlight'
import {formControlScope, refIndex, revealable, type ElementLike, type ElementLookup} from './pageModel'
import {waitForPageSettle, type SettleOptions, type SettleResult} from './pageSettle'
import {
  boundedFacetCandidates, describeBrowserState, elementFingerprint, readBrowserState,
  type ActiveViewReader, type BrowserState, type ElementNavigationReader, type FacetApplier,
  type FacetCandidateReader, type PageBlockerReader, type PageControllerApi,
  type PageFacetReader, type PageOpener, type PageQueryReader, type PageReading,
  type PageRecordRouteReader, type PageRouteReader, type PageSnapshotReaders,
} from './pageSnapshot'
import type {Redactor} from './redaction'
import type {ElementRef} from './types'

export {FormClosedOrReadonlyError, formFieldMatches, type FormHandoffField, type FormSubmitTarget}
export type {PageHighlight}
export type {
  ActiveViewReader, BrowserState, ElementNavigationReader, FacetApplier,
  FacetCandidateReader, PageBlockerReader, PageControllerApi, PageFacetReader,
  PageOpener, PageQueryReader, PageRecordRouteReader, PageRouteReader,
}

const ANNOTATION_STYLE_ID = 'woa-hide-page-controller-annotations'
/** How long a menu is given to open under the pointer before its control is pressed. */
const REVEAL_HOVER_MS = 350

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
  readElementFormModel?: ElementNavigationReader
  restoreElementFormModel?: (element: unknown, request: unknown) => Promise<boolean>
  readElementRowRecord?: ElementNavigationReader
  openPage?: PageOpener
  acceptReadSource?: (source: string, kind: 'form' | 'row') => boolean
  readConfirmBox?: (doc: Document) => unknown
  pressConfirmButton?: (label: string, doc: Document) => boolean
  readSubmitBusy?: (doc: Document) => boolean
}

/** A loaded PageController and the lookup that resolves its indices to live elements. */
interface LoadedPageController {
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
  readElementFormModel?: ElementNavigationReader
  restoreElementFormModel?: (element: unknown, request: unknown) => Promise<boolean>
  readElementRowRecord?: ElementNavigationReader
  openPage?: PageOpener
  acceptReadSource?: (source: string, kind: 'form' | 'row') => boolean
  readConfirmBox?: (doc: Document) => unknown
  pressConfirmButton?: (label: string, doc: Document) => boolean
  readSubmitBusy?: (doc: Document) => boolean
}

export class Page {
  private readonly entered = new EnteredControlTracker()

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

  private snapshotReaders(): PageSnapshotReaders {
    return {
      controller: this.controller,
      lookup: this.lookup,
      redactor: this.redactor,
      readPageFacets: this.readPageFacets,
      readFacetCandidates: this.readFacetCandidates,
      readPageRecordRoute: this.readPageRecordRoute,
      readActiveView: this.readActiveView,
      readPageRoute: this.readPageRoute,
      readPageQuery: this.readPageQuery,
      readElementNavigation: this.readElementNavigation,
      readPageRead: this.readPageRead,
      readElementPageControl: this.readElementPageControl,
      panelContainer: this.panelContainer,
    }
  }

  private formBridge() {
    return {
      readState: () => this.read(),
      lookupIndex: (index: number) => this.lookup(this.controller.selectorMap, index),
      clickIndex: (index: number) => this.controller.clickElement(index),
      inputIndex: (index: number, text: string) => this.controller.inputText(index, text),
      selectIndex: (index: number, label: string) => this.controller.selectOption(index, label),
      scrollIndex: (options: {down: boolean; numPages: number; pixels?: number; index?: number}) =>
        this.controller.scroll(options),
    }
  }

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
    const candidates = boundedFacetCandidates(this.readFacetCandidates, this.redactor)
    const candidate = candidates?.find(item => item.ref === ref)
    if (!candidate || candidate.active === active) {
      throw new Error('bad_arguments: facet candidate absent or already in requested state')
    }
    return Promise.resolve(this.applyPageFacet(ref, active))
  }

  facetCandidate(ref: unknown): {ref: string; kind: 'filter' | 'group';
    name: string; active: boolean} | undefined {
    return typeof ref === 'string'
      ? boundedFacetCandidates(this.readFacetCandidates, this.redactor)?.find(item => item.ref === ref)
      : undefined
  }

  /** The live element behind a server ref; an unknown ref is `bad_arguments`. */
  element(ref: unknown): ElementLike | null | undefined {
    return this.lookup(this.controller.selectorMap, refIndex(ref))
  }

  /** What the element under `ref` is now, in the terms an action's meaning depends on. */
  fingerprint(ref: unknown): string {
    return elementFingerprint(this.snapshotReaders(), ref)
  }

  /** Set by the Runtime from the application's controller module; absent means unreadable. */
  formModelReader: ElementReadFn | undefined
  rowRecordReader: ElementReadFn | undefined
  /** The application's source acceptance; absent means no source is ever forwarded. */
  sourceAccepter: ((source: string, kind: 'form' | 'row') => boolean) | undefined

  /** Why the last `formModel` read gave nothing, in a word the server can classify. */
  formUnreadable = ''

  /** The whole data bound to the live form under `ref`; null if the form cannot be read. */
  formModel(ref: unknown): FormModelRead | null {
    const result = readPageFormModel(this.element(ref), this.formModelReader, this.sourceAccepter)
    this.formUnreadable = result.unreadable
    return result.read
  }

  /** The table row a row control belongs to, as the table that renders it says. */
  rowRecord(ref: unknown): RowRecordRead | null {
    return readPageRowRecord(this.element(ref), this.rowRecordReader, this.sourceAccepter)
  }

  click(ref: unknown): Promise<unknown> {
    const target = this.element(ref)
    if (target instanceof HTMLButtonElement && target.disabled) {
      throw new Error('bad_arguments: disabled buttons may only be observed or highlighted')
    }
    return this.controller.clickElement(refIndex(ref))
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
    return inputFormControl(ref, this.element(ref), text, commit,
      this.formBridge(), this.entered, this.redactor)
  }

  select(ref: unknown, text: string, optionLabel?: string): Promise<unknown> {
    return selectFormControl(ref, this.element(ref), text, optionLabel,
      this.formBridge(), this.entered, this.redactor, this.interrupted)
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
    return verifyFormFields(fields, observed, ref => this.element(ref), this.entered, this.redactor)
  }

  /** Throw `FormClosedOrReadonlyError` if any targeted handoff form/field closed, detached, or turned readonly. */
  ensureHandoffOpenAndWritable(fields: unknown, observed: ElementRef[]): void {
    ensureHandoffFormOpenAndWritable(fields, observed, ref => this.element(ref), this.entered)
  }

  /** Wait briefly for substantive DOM mutations and non-long-tail network requests to settle. */
  settle(options: SettleOptions = {}): Promise<SettleResult> {
    return waitForPageSettle({
      panelContainer: this.panelContainer ?? null,
      isBusy: () => this.readiness().pending,
      ...options,
    })
  }

  /** Verify one visible server-owned submit target, including a disabled button.
   * Highlighting a partial draft never activates it or proves submission readiness.
   */
  verifyFormSubmit(target: unknown, fields: unknown, observed: ElementRef[]): target is FormSubmitTarget {
    return verifyFormSubmit(target, fields, observed, ref => this.element(ref))
  }

  /** Follow the exact verified submit control; all geometry and hints stay in this browser. */
  guideSubmit(ref: unknown, turnId: string): PageHighlight {
    return guideSubmitControl(this.element(ref), this.panelContainer, turnId)
  }

  /** Mark one observed control without focusing, clicking, or covering its hit target. */
  highlight(ref: unknown, turnId = ''): PageHighlight {
    return highlightControl(this.element(ref), this.panelContainer, turnId)
  }

  /** PageController's reading of the page, with its debugging overlay removed. */
  read(): Promise<BrowserState> {
    return readBrowserState(this.snapshotReaders())
  }

  /** The server's view of one reading, numbered with the epoch the session assigned it. */
  describe(state: BrowserState, epoch: number): PageReading {
    return describeBrowserState(this.snapshotReaders(), state, epoch)
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
    ...(typeof module.readElementFormModel === 'function'
      ? {readElementFormModel: module.readElementFormModel} : {}),
    ...(typeof module.restoreElementFormModel === 'function'
      ? {restoreElementFormModel: module.restoreElementFormModel} : {}),
    ...(typeof module.readElementRowRecord === 'function'
      ? {readElementRowRecord: module.readElementRowRecord} : {}),
    ...(typeof module.readElementNavigation === 'function'
      ? {readElementNavigation: module.readElementNavigation} : {}),
    ...(typeof module.openPage === 'function' ? {openPage: module.openPage} : {}),
    ...(typeof module.acceptReadSource === 'function'
      ? {acceptReadSource: module.acceptReadSource} : {}),
    ...(typeof module.readConfirmBox === 'function'
      ? {readConfirmBox: module.readConfirmBox} : {}),
    ...(typeof module.pressConfirmButton === 'function'
      ? {pressConfirmButton: module.pressConfirmButton} : {}),
    ...(typeof module.readSubmitBusy === 'function'
      ? {readSubmitBusy: module.readSubmitBusy} : {})}
}
