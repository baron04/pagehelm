/**
 * Page observation and bounded snapshot serialization for `Page`.
 *
 * Turns PageController's raw DOM reading into a redacted, bounded `PageObservation`
 * with live element fingerprints and verified application facts.
 */
import browserContract from '@contract/browser_contract.json'
import {formInstance} from './formInstance'
import {canonicalUrl} from './navigation'
import {
  actionElementFingerprint, boundIndexedContent, elementsFrom, formControlScope,
  hydrateButtonFacts, hydrateFormFacts, hydrateLinkFacts, hydrateRevealFacts,
  hydrateScrollFacts, hydrateSelectionFacts, isSensitiveFormControl, refIndex,
  type ElementLike, type ElementLookup,
} from './pageModel'
import {selectedControlsFromPage} from './pageSelection'
import {suspendPanelHitTargets} from './panelObservation'
import {MAX_CONTENT_CHARS, type Redactor} from './redaction'
import {WIRE_VERSION, type ElementRef, type JsonObject, type PageObservation} from './types'

export interface BrowserState {
  url: string
  title: string
  header?: string
  content: string
  footer?: string
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

/**
 * The application opening one of its own pages in this document. It answers true only
 * when it now shows another address; anything else leaves the Runtime to load the page.
 */
export type PageOpener = (url: string) => Promise<unknown> | unknown
export type PageFacetReader = () => unknown
export type FacetCandidateReader = () => unknown
export type FacetApplier = (ref: string, active: boolean) => Promise<unknown> | unknown
export type PageRecordRouteReader = () => unknown
export type PageBlockerReader = () => unknown
export type ActiveViewReader = () => unknown
export type PageRouteReader = () => unknown
export type PageQueryReader = () => unknown
/** Application-owned target facts, never authorization to click. */
export type ElementNavigationReader = (element: ElementLike | null | undefined) => unknown

/** What one observation of the page found, before the session numbers and wraps it. */
export interface PageReading {
  observation: PageObservation
  fingerprints: Map<string, string>
  canonicalUrl: string
}

export interface PageSnapshotReaders {
  readonly controller: PageControllerApi
  readonly lookup: ElementLookup
  readonly redactor: Redactor
  readonly readPageFacets?: PageFacetReader | undefined
  readonly readFacetCandidates?: FacetCandidateReader | undefined
  readonly readPageRecordRoute?: PageRecordRouteReader | undefined
  readonly readActiveView?: ActiveViewReader | undefined
  readonly readPageRoute?: PageRouteReader | undefined
  readonly readPageQuery?: PageQueryReader | undefined
  readonly readElementNavigation?: ElementNavigationReader | undefined
  readonly readPageRead?: PageQueryReader | undefined
  readonly readElementPageControl?: ElementNavigationReader | undefined
  readonly panelContainer?: HTMLElement | undefined
}

function resolveRef(deps: Pick<PageSnapshotReaders, 'controller' | 'lookup'>, ref: unknown): ElementLike | null | undefined {
  return deps.lookup(deps.controller.selectorMap, refIndex(ref))
}

/** Application-observed row identity, re-read before dispatch. No effect grant. */
function readRecordContext(
  deps: Pick<PageSnapshotReaders, 'controller' | 'lookup' | 'redactor' | 'readElementPageControl'>,
  ref: unknown,
): {id: string; table: string; values: string[][]} | undefined {
  const element = resolveRef(deps, ref)
  const fact = deps.readElementPageControl?.(element) as
    {kind?: unknown; id?: unknown; table?: unknown; values?: unknown} | undefined
  if (fact?.kind !== 'record' || typeof fact.id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:@-]{0,119}$/.test(fact.id) ||
      typeof fact.table !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:@-]{0,119}$/.test(fact.table) ||
      !Array.isArray(fact.values) || fact.values.length > 80) return undefined
  const values: string[][] = []
  for (const pair of fact.values) {
    if (!Array.isArray(pair) || pair.length !== 2 || typeof pair[0] !== 'string' ||
        typeof pair[1] !== 'string' || pair[0].length > 80 || pair[1].length > 300 ||
        deps.redactor.isSensitiveName(pair[0]) || deps.redactor.redactString(pair[1]) !== pair[1]) return undefined
    values.push(pair as string[])
  }
  return {id: fact.id, table: fact.table, values}
}

function readFormContext(
  deps: Pick<PageSnapshotReaders, 'controller' | 'lookup' | 'redactor' | 'readElementPageControl'>,
  ref: unknown,
): {mode: 'create' | 'update'; recordId: string | null; inputKind?: 'date' | 'datetime'} | undefined {
  try {
    const element = resolveRef(deps, ref)
    if (!(element instanceof HTMLElement) || !element.isConnected ||
        !formControlScope(element) || !formInstance(element) || isSensitiveFormControl(element, deps.redactor)) return undefined
    const fact = deps.readElementPageControl?.(element) as
      {kind?: unknown; mode?: unknown; recordId?: unknown; inputKind?: unknown} | undefined
    if (fact?.kind !== 'form') return undefined
    const precision: {inputKind?: 'date' | 'datetime'} =
      fact.inputKind === 'date' || fact.inputKind === 'datetime' ? {inputKind: fact.inputKind} : {}
    if (fact.mode === 'create' && fact.recordId === null) return {mode: 'create', recordId: null, ...precision}
    if (fact.mode === 'update' && typeof fact.recordId === 'string' &&
        /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,119}$/.test(fact.recordId) &&
        deps.redactor.redactString(fact.recordId) === fact.recordId) {
      return {mode: 'update', recordId: fact.recordId, ...precision}
    }
  } catch { /* Missing live context grants no mode; it never falls back to summary text. */ }
  return undefined
}

function readNavigationFact(
  deps: Pick<PageSnapshotReaders, 'controller' | 'lookup' | 'redactor' | 'readElementNavigation'>,
  ref: unknown,
): {kind: 'branch' | 'page'; path: string; expanded?: boolean} | undefined {
  try {
    const raw = deps.readElementNavigation?.(resolveRef(deps, ref)) as Record<string, unknown> | undefined
    if (!raw || !['branch', 'page'].includes(String(raw.kind)) || typeof raw.path !== 'string' ||
        raw.path.length > 500 || !/^\/(?!\/)[^?#\\\s]*$/.test(raw.path)) return undefined
    const url = new URL(raw.path, location.href)
    if (url.origin !== location.origin || url.pathname !== raw.path ||
        deps.redactor.redactString(raw.path) !== raw.path) return undefined
    return {kind: raw.kind as 'branch' | 'page', path: raw.path,
      ...(raw.kind === 'branch' && typeof raw.expanded === 'boolean' ? {expanded: raw.expanded} : {})}
  } catch { return undefined }
}

/** What the element under `ref` is now, in the terms an action's meaning depends on. */
export function elementFingerprint(deps: PageSnapshotReaders, ref: unknown): string {
  try {
    const fingerprint = actionElementFingerprint(resolveRef(deps, ref))
    const navigation = readNavigationFact(deps, ref)
    const form = readFormContext(deps, ref)
    const record = readRecordContext(deps, ref)
    const contextual = form || record ? JSON.stringify([fingerprint, form, record]) : fingerprint
    return navigation ? JSON.stringify([contextual, navigation]) : contextual
  } catch {
    return 'missing'
  }
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
async function readControllerBrowserState(
  controller: PageControllerApi,
  readElementPageControl: ElementNavigationReader | undefined,
): Promise<BrowserState> {
  if (!readElementPageControl) return controller.getBrowserState()
  const host = window as unknown as {getEventListenersForNode?: (node: Element) => Array<{type: string}>}
  const had = Object.prototype.hasOwnProperty.call(host, 'getEventListenersForNode')
  const earlier = host.getEventListenersForNode
  host.getEventListenersForNode = node => {
    let named = false
    try {
      named = node instanceof HTMLElement &&
        (readElementPageControl(node) as {kind?: unknown} | undefined)?.kind === 'page_size'
    } catch { named = false }
    return named ? [{type: 'click'}] : typeof earlier === 'function' ? earlier(node) : []
  }
  try {
    return await controller.getBrowserState()
  } finally {
    if (had && earlier) host.getEventListenersForNode = earlier
    else delete host.getEventListenersForNode
  }
}

/** PageController's reading of the page, with its debugging overlay removed. */
export async function readBrowserState(deps: PageSnapshotReaders): Promise<BrowserState> {
  let restore = () => {}
  const after: EventListener = () => { restore(); restore = () => {} }
  const before: EventListener = () => {
    after(new Event('afterUpdate'))
    restore = suspendPanelHitTargets(deps.panelContainer)
  }
  const events = deps.controller.addEventListener && deps.controller.removeEventListener
  if (events) {
    deps.controller.addEventListener!('beforeUpdate', before)
    deps.controller.addEventListener!('afterUpdate', after)
  }
  let state: BrowserState
  try {
    state = await readControllerBrowserState(deps.controller, deps.readElementPageControl)
  } finally {
    after(new Event('afterUpdate'))
    if (events) {
      deps.controller.removeEventListener!('beforeUpdate', before)
      deps.controller.removeEventListener!('afterUpdate', after)
    }
  }
  // PageController uses numbered overlays while building its private selector map. They
  // are debugging aids, not product UI; removing them does not change the stored refs.
  document.getElementById('playwright-highlight-container')?.remove()
  return state
}

function boundedRecordRoute(
  readPageRecordRoute: PageRecordRouteReader | undefined,
  redactor: Redactor,
): string | undefined {
  let raw: unknown
  try { raw = readPageRecordRoute?.() } catch { return undefined }
  if (typeof raw !== 'string' || raw.length > 1000 || !raw.startsWith('/') ||
      raw.startsWith('//') || /[?#\\]/.test(raw) ||
      Array.from(raw).some(char => char.charCodeAt(0) <= 32 || char.charCodeAt(0) === 127) ||
      raw.split('/').some(part => part === '.' || part === '..')) return undefined
  const safe = redactor.redactString(raw)
  return safe === raw ? raw : undefined
}

function boundedPageRead(
  readPageRead: PageQueryReader | undefined,
  redactor: Redactor,
): PageObservation['page_read'] {
  try {
    const raw = readPageRead?.()
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined
    const r = raw as Record<string, unknown>
    if (Object.keys(r).sort().join(',') !== 'page,page_size,path,query_digest,query_fields,revision,rows,total' ||
        typeof r.path !== 'string' || r.path.length > browserContract.observation.max_page_route_chars ||
        !/^\/(?!\/)[^?#\\\s]*$/.test(r.path) || r.path.split('/').some(p => p === '.' || p === '..') ||
        redactor.redactString(r.path) !== r.path || typeof r.query_digest !== 'string' ||
        !/^[0-9a-f]{64}$/.test(r.query_digest) ||
        ['page', 'page_size', 'revision', 'rows', 'total'].some(k =>
          !Number.isSafeInteger(r[k]) || Number(r[k]) < 0) ||
        Math.min(Number(r.page), Number(r.page_size), Number(r.revision)) <= 0 ||
        Number(r.rows) > Number(r.page_size) || Number(r.rows) > Number(r.total)) return undefined
    if (!r.query_fields || typeof r.query_fields !== 'object' || Array.isArray(r.query_fields)) return undefined
    const fields = Object.entries(r.query_fields)
    if (fields.length > browserContract.observation.max_query_parameters || fields.some(([key, value]) =>
      !/^[A-Za-z][A-Za-z0-9_]*(?:\[[0-9]{1,2}\])?$/.test(key) ||
      key.length > browserContract.observation.max_query_key_chars || redactor.isSensitiveName(key) ||
      typeof value !== 'string' || !/^[0-9a-f]{64}$/.test(value))) return undefined
    return {path: r.path, query_digest: r.query_digest, query_fields: Object.fromEntries(fields), revision: Number(r.revision),
      page: Number(r.page), page_size: Number(r.page_size), total: Number(r.total), rows: Number(r.rows)}
  } catch { return undefined }
}

function boundedPageQuery(
  readPageQuery: PageQueryReader | undefined,
  redactor: Redactor,
): PageObservation['page_query'] {
  try {
    const raw = readPageQuery?.()
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined
    const q = raw as Record<string, unknown>
    if (Object.keys(q).sort().join(',') !== 'parameters,path' || typeof q.path !== 'string' ||
        q.path.length > browserContract.observation.max_page_route_chars || !q.path.startsWith('/') ||
        q.path.startsWith('//') || /[?#\\]/.test(q.path) ||
        Array.from(q.path).some(c => c.charCodeAt(0) <= 32 || c.charCodeAt(0) === 127) ||
        q.path.split('/').some(p => p === '.' || p === '..') || redactor.redactString(q.path) !== q.path ||
        !q.parameters || typeof q.parameters !== 'object' || Array.isArray(q.parameters)) return undefined
    const pairs = Object.entries(q.parameters)
    if (pairs.length > browserContract.observation.max_query_parameters) return undefined
    const parameters: Record<string, string> = {}
    for (const [key, value] of pairs) {
      if (!/^[A-Za-z][A-Za-z0-9_]*$/.test(key) || key.length > browserContract.observation.max_query_key_chars ||
          redactor.isSensitiveName(key) || typeof value !== 'string' ||
          value.length > browserContract.observation.max_query_value_chars ||
          redactor.redactString(value) !== value) return undefined
      parameters[key] = value
    }
    return {path: q.path, parameters}
  } catch { return undefined }
}

function boundedPageRoute(
  readPageRoute: PageRouteReader | undefined,
  redactor: Redactor,
): string | undefined {
  let raw: unknown
  try { raw = readPageRoute?.() } catch { return undefined }
  if (typeof raw !== 'string' || raw.length > browserContract.observation.max_page_route_chars ||
      !raw.startsWith('/') || raw.startsWith('//') || /[?#\\]/.test(raw) ||
      Array.from(raw).some(char => char.charCodeAt(0) <= 32 || char.charCodeAt(0) === 127) ||
      raw.split('/').some(part => part === '.' || part === '..')) return undefined
  const safe = redactor.redactString(raw)
  return safe === raw ? raw : undefined
}

function boundedActiveView(
  readActiveView: ActiveViewReader | undefined,
  redactor: Redactor,
): string | undefined {
  let raw: unknown
  try { raw = readActiveView?.() } catch { return undefined }
  if (typeof raw !== 'string') return undefined
  const view = raw.replace(/\s+/g, ' ').trim()
  if (!view || view.length > browserContract.observation.max_selected_control_chars ||
      redactor.isSensitiveName(view)) return undefined
  const safe = redactor.redactString(view)
  return safe === view ? view : undefined
}

function boundedFacets(
  readPageFacets: PageFacetReader | undefined,
  redactor: Redactor,
): Array<{kind: 'filter' | 'group'; name: string}> | undefined {
  let raw: unknown
  try { raw = readPageFacets?.() } catch { return undefined }
  if (!Array.isArray(raw)) return undefined
  const result: Array<{kind: 'filter' | 'group'; name: string}> = []
  for (const item of raw.slice(0, browserContract.observation.max_page_facets)) {
    if (!item || typeof item !== 'object') continue
    const fact = item as Record<string, unknown>
    if (fact.kind !== 'filter' && fact.kind !== 'group') continue
    if (typeof fact.name !== 'string') continue
    const name = fact.name.replace(/\s+/g, ' ').trim()
    if (!name || redactor.isSensitiveName(name) ||
        redactor.isSensitiveName(name.replace(/\s+/g, '_'))) continue
    const safe = redactor.redactString(name).slice(
      0, browserContract.observation.max_selected_control_chars)
    if (safe && !result.some(existing => existing.kind === fact.kind &&
        existing.name === safe)) result.push({kind: fact.kind, name: safe})
  }
  return result
}

export function boundedFacetCandidates(
  readFacetCandidates: FacetCandidateReader | undefined,
  redactor: Redactor,
): Array<{ref: string; kind: 'filter' | 'group'; name: string; active: boolean}> | undefined {
  let raw: unknown
  try { raw = readFacetCandidates?.() } catch { return undefined }
  if (!Array.isArray(raw)) return undefined
  const result: Array<{ref: string; kind: 'filter' | 'group'; name: string; active: boolean}> = []
  for (const item of raw.slice(0, browserContract.observation.max_facet_candidates)) {
    if (!item || typeof item !== 'object') continue
    const choice = item as Record<string, unknown>
    if ((choice.kind !== 'filter' && choice.kind !== 'group') ||
        typeof choice.ref !== 'string' || !/^[a-zA-Z0-9_-]{1,64}$/.test(choice.ref) ||
        typeof choice.name !== 'string' || typeof choice.active !== 'boolean') continue
    const name = choice.name.replace(/\s+/g, ' ').trim()
    if (!name || redactor.isSensitiveName(name) ||
        redactor.isSensitiveName(name.replace(/\s+/g, '_'))) continue
    const safe = redactor.redactString(name).slice(
      0, browserContract.observation.max_selected_control_chars)
    if (safe && !result.some(existing => existing.ref === choice.ref)) {
      result.push({ref: choice.ref, kind: choice.kind, name: safe, active: choice.active})
    }
  }
  return result
}

/** Collect bounded scroll room hints for visible dialog/modal and page main scroll containers. */
export function collectScrollContainerHints(
  doc: Document | null | undefined,
  panelContainer?: HTMLElement,
): string[] {
  if (!doc) return []
  const view = doc.defaultView ?? (typeof window !== 'undefined' ? window : null)
  const seen = new Set<Element>()
  const hints: string[] = []
  const tryContainer = (node: Element | null | undefined, scope: 'dialog' | 'page') => {
    if (!node || seen.has(node) || !node.isConnected) return
    if (panelContainer && (node === panelContainer || panelContainer.contains(node))) return
    const tag = node.tagName.toUpperCase()
    if (tag === 'TEXTAREA' || tag === 'SELECT') return
    if (node.getAttribute('role') === 'listbox' || node.getAttribute('role') === 'tree') return
    if (!(node.scrollHeight > node.clientHeight + 1 && node.clientHeight > 0)) return
    const style = view?.getComputedStyle ? view.getComputedStyle(node) : null
    if (style && (style.display === 'none' || style.visibility === 'hidden')) return
    if (node.closest('[hidden],[aria-hidden="true"]')) return
    const isDocRoot = node === doc.scrollingElement || node === doc.documentElement || node === doc.body
    if (!isDocRoot) {
      if (style && !/(auto|scroll|overlay)/.test(style.overflowY)) return
      if (node.clientHeight < 120) return
    }
    seen.add(node)
    const up = Math.max(0, Math.round(node.scrollTop))
    const down = Math.max(0, Math.round(node.scrollHeight - node.clientHeight - node.scrollTop))
    hints.push(`[scroll_container scope=${scope} scroll_up_px=${up} scroll_down_px=${down}]`)
  }
  for (const dialog of doc.querySelectorAll('[role="dialog"], dialog')) {
    if (panelContainer && panelContainer.contains(dialog)) continue
    if (dialog.tagName === 'DIALOG' && !dialog.hasAttribute('open')) continue
    tryContainer(dialog, 'dialog')
    for (const candidate of dialog.querySelectorAll('*')) {
      if (hints.length >= 4) break
      tryContainer(candidate, 'dialog')
    }
  }
  tryContainer(doc.scrollingElement ?? doc.documentElement, 'page')
  if (doc.body && doc.body !== doc.scrollingElement) tryContainer(doc.body, 'page')
  for (const candidate of doc.querySelectorAll('main, section, [role="main"], div')) {
    if (hints.length >= 4) break
    if (candidate.closest('[role="dialog"], dialog')) continue
    tryContainer(candidate, 'page')
  }
  return hints.slice(0, 4)
}

function isVisibleSnapshotNode(node: Element, view: Window | null, panelContainer?: HTMLElement): boolean {
  if (!node.isConnected) return false
  if (panelContainer && (node === panelContainer || panelContainer.contains(node))) return false
  if (node.closest('[hidden],[aria-hidden="true"]')) return false
  if (view?.getComputedStyle) {
    const style = view.getComputedStyle(node)
    if (style.display === 'none' || style.visibility === 'hidden') return false
  }
  return true
}

function readOpenDialogTitle(
  doc: Document | null | undefined,
  redactor: Redactor,
  panelContainer?: HTMLElement,
): string {
  if (!doc) return ''
  const view = doc.defaultView ?? (typeof window !== 'undefined' ? window : null)
  const dialogs = [...doc.querySelectorAll('[role="dialog"], dialog')].filter(dialog => {
    if (dialog.tagName === 'DIALOG' && !dialog.hasAttribute('open')) return false
    if (!isVisibleSnapshotNode(dialog, view, panelContainer)) return false
    const rect = dialog.getBoundingClientRect()
    // Allow jsdom tests where rects are unmocked if not display:none, or non-zero rects in browsers.
    return rect.width > 0 || rect.height > 0 || !view?.getComputedStyle
      ? true
      : view.getComputedStyle(dialog).display !== 'none'
  })
  const active = dialogs.at(-1)
  if (!active) return ''
  const clean = (raw: string | null | undefined): string => {
    const text = String(raw ?? '').replace(/\s+/g, ' ').trim()
    if (!text || redactor.isSensitiveName(text)) return ''
    return redactor.redactString(text).slice(0, 120)
  }
  const ariaLabel = clean(active.getAttribute('aria-label'))
  if (ariaLabel) return ariaLabel
  const labelledBy = active.getAttribute('aria-labelledby')
  if (labelledBy) {
    const parts = labelledBy.trim().split(/\s+/).slice(0, 4)
      .map(id => /^[A-Za-z][A-Za-z0-9_:.-]{0,199}$/.test(id) ? doc.getElementById(id)?.textContent : '')
      .filter(Boolean)
    const joined = clean(parts.join(' '))
    if (joined) return joined
  }
  const heading = active.querySelector('header, [role="heading"], h1, h2, h3, h4, [class*="title"]')
  return clean(heading?.textContent)
}

function readVisibleRowCount(
  doc: Document | null | undefined,
  panelContainer?: HTMLElement,
): number {
  if (!doc) return 0
  const view = doc.defaultView ?? (typeof window !== 'undefined' ? window : null)
  let total = 0
  for (const row of doc.querySelectorAll('table tbody tr, [role="grid"] [role="row"]')) {
    if (!isVisibleSnapshotNode(row, view, panelContainer)) continue
    if (row.closest('thead')) continue
    if (row.querySelector('[role="columnheader"], th') && !row.querySelector('td, [role="gridcell"]')) continue
    total += 1
  }
  return total
}

function readVisibleAlertMessages(
  doc: Document | null | undefined,
  redactor: Redactor,
  panelContainer?: HTMLElement,
): string[] {
  if (!doc) return []
  const view = doc.defaultView ?? (typeof window !== 'undefined' ? window : null)
  const out: string[] = []
  const seen = new Set<string>()
  for (const node of doc.querySelectorAll('[role="alert"], [role="status"], [aria-live="assertive"], [aria-live="polite"]')) {
    if (!isVisibleSnapshotNode(node, view, panelContainer)) continue
    const raw = (node.textContent || '').replace(/\s+/g, ' ').trim()
    if (!raw || raw.length > 200 || redactor.isSensitiveName(raw)) continue
    const safe = redactor.redactString(raw).slice(0, 200)
    if (!safe || seen.has(safe)) continue
    seen.add(safe)
    out.push(safe)
    if (out.length >= 8) break
  }
  return out
}

/** Build a compact, bounded post-action observation summary for model continuity. */
export function summarizeObservation(
  before: PageObservation | null | undefined,
  after: PageObservation,
  redactor: Redactor,
  doc: Document | null | undefined = typeof document !== 'undefined' ? document : null,
  panelContainer?: HTMLElement,
): JsonObject {
  const prevMessages = new Set((before?.messages ?? []).map(m => String(m)))
  const afterMessages = [
    ...(after.messages ?? []).map(m => redactor.redactString(String(m)).slice(0, 200)).filter(Boolean),
    ...readVisibleAlertMessages(doc, redactor, panelContainer),
  ]
  const newMessages: string[] = []
  const seen = new Set<string>()
  for (const msg of afterMessages) {
    if (prevMessages.has(msg) || seen.has(msg)) continue
    seen.add(msg)
    newMessages.push(msg)
    if (newMessages.length >= 8) break
  }
  const activeView = after.active_view
    ? redactor.redactString(after.active_view).slice(0, 120)
    : ''
  const dialogTitle = readOpenDialogTitle(doc, redactor, panelContainer)
  const rowCount = after.page_read && typeof after.page_read.rows === 'number'
    ? after.page_read.rows
    : readVisibleRowCount(doc, panelContainer)
  return {
    url_changed: Boolean(before && before.url !== after.url),
    epoch_changed: Boolean(before && before.epoch !== after.epoch),
    active_view: activeView,
    new_messages: newMessages,
    dialog_title: dialogTitle,
    row_count: rowCount,
  }
}

/** The server's view of one reading, numbered with the epoch the session assigned it. */
export function describeBrowserState(
  deps: PageSnapshotReaders,
  state: BrowserState,
  epoch: number,
): PageReading {
  const header = deps.redactor.sanitizePageContent(state.header ?? '').slice(0, 2_000)
  const scrollHints = collectScrollContainerHints(
    typeof document !== 'undefined' ? document : null,
    deps.panelContainer,
  )
  const rawFooter = deps.redactor.sanitizePageContent(state.footer ?? '')
  const footer = [rawFooter, ...scrollHints].filter(Boolean).join('\n').slice(0, 2_000)
  const availableBodyChars = Math.max(0, MAX_CONTENT_CHARS - header.length - footer.length - 2)
  const sanitizedBody = deps.redactor.sanitizePageContent(state.content)
  const charBoundedBody = sanitizedBody.slice(0, availableBodyChars)
  const boundedBody = boundIndexedContent(charBoundedBody)
  // Keep PageController's scroll hints even when its body was bounded. Without the footer
  // the Agent cannot distinguish a short page from one that needs another observation.
  const map = deps.controller.selectorMap
  // A component wrapper's credential identity may not appear in the controller text.
  // Remove that entire indexed control record before either projection leaves the page.
  const content = `${header}\n${boundedBody.content}\n${footer}`.replace(
    /(?:^|\n)\s*\*?\[(\d+)]<(input|textarea|select)\b[\s\S]*?\s*\/>?(?=\n|$)/g,
    (record, index: string) => {
      try {
        if (isSensitiveFormControl(deps.lookup(map, Number(index)), deps.redactor)) {
          return `${record.startsWith('\n') ? '\n' : ''}[${index}]<input type=password />`
        }
      } catch { /* The ordinary summary redactor still applies to stale records. */ }
      return record
    })
  const elements: ElementRef[] = hydrateScrollFacts(hydrateRevealFacts(hydrateSelectionFacts(hydrateButtonFacts(
    hydrateFormFacts(hydrateLinkFacts(elementsFrom(content, epoch, deps.redactor), map,
      deps.lookup, deps.redactor), map, deps.lookup, deps.redactor), map, deps.lookup,
    deps.redactor), map, deps.lookup), map, deps.lookup), map, deps.lookup)
  const fingerprints = new Map<string, string>()
  for (const item of elements) {
    // Mode is policy-critical observation data, never parsed controller prose.
    delete item.attrs.form_mode
    delete item.attrs.form_record_id
    delete item.attrs.form_input_kind
    delete item.attrs.record_id
    delete item.attrs.record_table
    delete item.attrs.record_values
    try {
      const element = resolveRef(deps, item.ref)
      if (!(element instanceof HTMLElement) || !element.isConnected) continue
      const record = readRecordContext(deps, item.ref)
      if (record) {
        item.attrs.record_id = record.id
        item.attrs.record_table = record.table
        item.attrs.record_values = JSON.stringify(record.values)
      }
      const fact = deps.readElementPageControl?.(element) as
        {kind?: unknown; page?: unknown; pageSize?: unknown; field?: unknown; queryValue?: unknown;
          mode?: unknown; recordId?: unknown} | undefined
      const form = readFormContext(deps, item.ref)
      if (form && item.attrs.form_scope && item.attrs.form_instance) {
        item.attrs.form_mode = form.mode
        item.attrs.form_record_id = form.recordId === null ? '' : form.recordId
        if (form.inputKind) item.attrs.form_input_kind = form.inputKind
      }
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
            !deps.redactor.isSensitiveName(fact.field)) {
          item.attrs.query_field = fact.field
          if (typeof fact.queryValue === 'string' && /^(?:[0-9]{1,20}|true|false)$/.test(fact.queryValue) &&
              deps.redactor.redactString(fact.queryValue) === fact.queryValue &&
              !isSensitiveFormControl(element, deps.redactor)) item.attrs.query_value = fact.queryValue
        }
      }
    } catch { /* An unavailable application fact grants no control. */ }
  }
  for (const item of elements) {
    const navigation = readNavigationFact(deps, item.ref)
    if (navigation) {
      item.attrs.navigation_kind = navigation.kind
      item.attrs.navigation_path = navigation.path
      if (navigation.expanded !== undefined) item.attrs['aria-expanded'] = String(navigation.expanded)
      if (navigation.kind === 'page') item.attrs.href = navigation.path
    }
    fingerprints.set(item.ref, elementFingerprint(deps, item.ref))
  }
  const facets = deps.readPageFacets ? boundedFacets(deps.readPageFacets, deps.redactor) : undefined
  const candidates = deps.readFacetCandidates ? boundedFacetCandidates(deps.readFacetCandidates, deps.redactor) : undefined
  const recordRoute = boundedRecordRoute(deps.readPageRecordRoute, deps.redactor)
  const activeView = boundedActiveView(deps.readActiveView, deps.redactor)
  const pageRoute = boundedPageRoute(deps.readPageRoute, deps.redactor)
  const pageQuery = pageRoute === undefined ? undefined : boundedPageQuery(deps.readPageQuery, deps.redactor)
  const pageRead = pageRoute === undefined ? undefined : boundedPageRead(deps.readPageRead, deps.redactor)
  return {
    observation: {
      schema_version: WIRE_VERSION,
      epoch,
      url: deps.redactor.redactString(state.url),
      title: deps.redactor.redactString(state.title).slice(0, 300),
      landmarks: [],
      elements,
      content,
      messages: [],
      truncated: (state.content.length > availableBodyChars || boundedBody.truncated),
      selected_controls: selectedControlsFromPage(document, deps.redactor),
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
