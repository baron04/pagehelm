/**
 * Whether an action leaves the page, and afterwards, whether it did.
 *
 * A page action's outcome decides whether the server may call it done. A navigation the
 * Runtime cannot prove happened is `indeterminate`, never a success; so every URL is
 * canonicalized the same way, every started or expected destination is written ahead of
 * the action as a digest, and recovery after a reload compares digests rather than
 * trusting what the page now shows.
 */
import {listen} from './contain'
import {explicitAttribute, type ElementLike} from './pageModel'
import type {Redactor} from './redaction'
import type {ActionEnvelope, JsonObject} from './types'

function base(): string {
  return typeof location !== 'undefined' ? location.href : 'http://invalid.local/'
}

export function absoluteUrl(value: unknown): string {
  return new URL(String(value ?? ''), base()).href
}

/** The URL without its fragment: a hash change is not a page change. */
export function canonicalUrl(value: unknown): string {
  const url = new URL(String(value ?? ''), base())
  url.hash = ''
  return url.href
}

/** An absolute URL on this page's own origin, or a refusal naming the origin it was on. */
export function sameOriginUrl(value: unknown): string {
  const url = new URL(String(value ?? ''), window.location.href)
  if (url.origin !== window.location.origin) throw new Error(`origin_refused: ${url.origin}`)
  return url.href
}

/**
 * Whether following `href` would only move within the current document: the same URL up to
 * its fragment, with a fragment present. HTML scrolls to the fragment (or does nothing for a
 * bare `#`) without loading anything, so there is no navigation for the Runtime to wait for.
 * Widgets use such anchors as buttons -- a notebook tab is `<a href="#" role="tab">` -- and
 * waiting for a page change that cannot come turned every tab click into a timeout.
 */
export function isSameDocumentFragment(href: string, current: string = window.location.href): boolean {
  try {
    const target = new URL(href, current)
    const here = new URL(current)
    if (!href.includes('#')) return false
    return target.origin === here.origin && target.pathname === here.pathname &&
      target.search === here.search
  } catch {
    return false
  }
}

export async function urlDigest(value: unknown): Promise<string> {
  if (!value) return ''
  const bytes = new TextEncoder().encode(canonicalUrl(value))
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
}

// -- where a control sends the page ------------------------------------------------------

function baseTarget(ownerDocument: Document | null | undefined = globalThis.document): string {
  const baseElement = ownerDocument?.querySelector?.('base[target]') as HTMLBaseElement | null
  return String(baseElement?.getAttribute?.('target') || baseElement?.target || '')
}

export function targetsCurrentPage(value: string | null | undefined): boolean {
  const target = String(value || '').trim().toLowerCase()
  return !target || target === '_self'
}

/** The browsing context a link or submit control would navigate, per HTML's own rules. */
export function effectiveTarget(element: ElementLike | null | undefined,
                                form: ElementLike | null = null): string {
  if (form) {
    const formTarget = explicitAttribute(element, 'formtarget')
    if (formTarget?.trim()) return formTarget
  } else {
    const ownTarget = explicitAttribute(element, 'target')
    if (ownTarget?.trim()) return ownTarget
  }
  const parentTarget = explicitAttribute(form, 'target')
  if (parentTarget?.trim()) return parentTarget
  return baseTarget(element?.ownerDocument || form?.ownerDocument)
}

export function isSubmitControl(element: ElementLike | null | undefined): boolean {
  const tag = String(element?.tagName || '').toUpperCase()
  const type = String(element?.type || '').toLowerCase()
  return (tag === 'BUTTON' && (!type || type === 'submit')) ||
    (tag === 'INPUT' && ['submit', 'image'].includes(type))
}

/** A native form, as the executor resolved it for one submission. */
export interface NativeForm {
  form: HTMLFormElement
  method: string
  url: string
}

/** The URL an HTML GET submission of this form navigates to. */
export function formGetUrl(native: NativeForm | null | undefined,
                           submitter: HTMLElement | null = null): string {
  if (!native?.form || native.method !== 'get') return ''
  const destination = new URL(native.url, location.href)
  const params = new URLSearchParams()
  let data: FormData
  try {
    data = submitter ? new FormData(native.form, submitter) : new FormData(native.form)
  } catch {
    data = new FormData(native.form)
    const name = explicitAttribute(submitter as ElementLike | null, 'name')
    const control = submitter as HTMLButtonElement | null
    if (name && !control?.disabled) data.append(name, String(control?.value || ''))
  }
  for (const [name, value] of data.entries()) {
    if (typeof value === 'string') params.append(name, value)
  }
  // HTML's GET submission replaces the action query with the form entry list.
  destination.search = params.toString()
  return sameOriginUrl(destination.href)
}

// -- did it navigate -------------------------------------------------------------------

/** The context an action runs in, captured before the action so a change can be seen. */
export interface NavigationWatch {
  context: Window | null
  document: Document | null
  frameElement: HTMLIFrameElement | null
  startedUrl: string
}

export function navigationWatch(element: ElementLike | null | undefined): NavigationWatch {
  const ownerDocument = element?.ownerDocument || globalThis.document || null
  const context = ownerDocument?.defaultView || globalThis.window || null
  let frameElement: HTMLIFrameElement | null = null
  try {
    frameElement = context === globalThis.window
      ? null : (context?.frameElement as HTMLIFrameElement | null) ?? null
  } catch {
    frameElement = null
  }
  let startedUrl = ''
  try { startedUrl = absoluteUrl(context?.location?.href || '') } catch { /* opaque frame */ }
  return {context, document: ownerDocument, frameElement, startedUrl}
}

export function navigationCommitted(context: Window | null | undefined, urlBefore: string,
                                    documentBefore: Document | null | undefined,
                                    frameElement: HTMLIFrameElement | null = null): boolean {
  try {
    const liveContext = frameElement?.contentWindow || context
    const liveDocument = frameElement?.contentDocument || liveContext?.document
    const currentUrl = absoluteUrl(liveContext?.location?.href || '')
    return Boolean(currentUrl && currentUrl !== urlBefore) || Boolean(
      documentBefore && liveDocument && liveDocument !== documentBefore)
  } catch { return false }
}

/** A child frame's navigation, which only its `load` event reliably reports. */
export interface NavigationSignal {
  readonly committed: boolean
  readonly promise: Promise<{navigated: true; message: string}>
  dispose(): void
}

export function armChildFrameNavigation(frameElement: HTMLIFrameElement | null | undefined): NavigationSignal | null {
  if (!frameElement?.addEventListener) return null
  let committed = false
  let resolveCommitted: (value: {navigated: true; message: string}) => void = () => {}
  const promise = new Promise<{navigated: true; message: string}>(resolve => {
    resolveCommitted = resolve
  })
  const stop = listen('child frame navigation', frameElement, 'load', () => {
    committed = true
    resolveCommitted({navigated: true, message: 'child frame navigation committed'})
  }, {once: true})
  return {
    get committed() { return committed },
    promise,
    dispose: stop,
  }
}

// -- the write-ahead record of a navigation ----------------------------------------------

/** Written with the action before it runs, so a reload can tell whether it navigated. */
export async function navigationWal(startedUrl: string, expectedUrl: string,
                                    navigationKind: string, redactor: Redactor): Promise<JsonObject> {
  const started = absoluteUrl(startedUrl)
  const expected = expectedUrl ? absoluteUrl(expectedUrl) : ''
  const [startedDigest, expectedDigest] = await Promise.all([
    urlDigest(started), expected ? urlDigest(expected) : '',
  ])
  return {
    started_url_digest: startedDigest,
    started_url_display: redactor.redactString(started),
    expected_url_digest: expectedDigest,
    expected_url_display: expected ? redactor.redactString(expected) : '',
    navigation_kind: navigationKind,
  }
}

/** Replace raw URLs an older Runtime stored with the redacted display it stores now. */
export function scrubLegacyNavigationUrls(action: ActionEnvelope | null | undefined,
                                          redactor: Redactor): ActionEnvelope | null {
  if (!action || typeof action !== 'object') return action ?? null
  const clean: ActionEnvelope = {...action}
  if (Object.hasOwn(clean, 'started_url')) {
    clean.started_url_display ||= redactor.redactString(clean.started_url)
    delete clean.started_url
  }
  if (Object.hasOwn(clean, 'expected_url')) {
    clean.expected_url_display ||= redactor.redactString(clean.expected_url)
    delete clean.expected_url
  }
  return clean
}

export interface DocumentNavigation {
  url: string
  startedAt: number
}

/** Browser-local load facts. Raw URLs never enter the outbox, receipt or server context. */
export function documentNavigation(): DocumentNavigation | null {
  try {
    const entries = performance.getEntriesByType('navigation')
    if (entries.length !== 1) return null
    const entry = entries[0] as PerformanceNavigationTiming
    if (entry.entryType !== 'navigation' || entry.type !== 'navigate' ||
        entry.redirectCount !== 0 || entry.startTime !== 0 || !entry.name ||
        !Number.isFinite(performance.timeOrigin) || performance.timeOrigin <= 0) return null
    return {url: entry.name, startedAt: performance.timeOrigin}
  } catch {
    return null
  }
}

/**
 * Whether the page now shows proves a claimed action navigated.
 *
 * Only a digest match counts. A form may POST back to the same URL, so for a form only a
 * new JavaScript document proves the submission committed -- without claiming its business
 * effect succeeded. A Runtime restart in the same document keeps the document id and cannot
 * forge that proof. A newly loaded document may change its address through history before
 * the Runtime starts. Its browser navigation entry still names the document load; that
 * exact digest can establish navigation, with a new document and dispatch-time correlation.
 * The application's fresh rendered-page checks still decide whether the target is ready.
 */
export async function recoveryProvesNavigation(action: ActionEnvelope | null | undefined,
                                               observationUrl: string,
                                               currentDocumentId = '',
                                               loaded: DocumentNavigation | null = null): Promise<boolean> {
  const expected = String(action?.expected_url_digest || '')
  const before = String(action?.started_url_digest || '')
  const after = observationUrl ? await urlDigest(observationUrl) : ''
  if (action?.phase === 'claimed' && action?.navigation_kind === 'form') {
    return Boolean(action.document_id && currentDocumentId &&
      action.document_id !== currentDocumentId)
  }
  if (action?.phase !== 'claimed' ||
      !['goto', 'link', 'form-get'].includes(String(action?.navigation_kind)) ||
      !expected || expected === before) return false
  if (after === expected) return true
  const dispatchedAt = action.dispatched_at
  if (!action.document_id || !currentDocumentId || action.document_id === currentDocumentId ||
      typeof dispatchedAt !== 'number' || !Number.isFinite(dispatchedAt) || dispatchedAt <= 0 ||
      !loaded || !Number.isFinite(loaded.startedAt) || loaded.startedAt < dispatchedAt ||
      loaded.startedAt > Date.now()) return false
  try {
    if (new URL(loaded.url).origin !== new URL(observationUrl).origin) return false
    return await urlDigest(loaded.url) === expected
  } catch {
    return false
  }
}
