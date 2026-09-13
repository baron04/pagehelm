/**
 * Embedded Web Runtime for the server-owned Web Operation Agent.
 *
 * The browser owns DOM observation and generic actions only.  Task history, planning,
 * source knowledge, model credentials and the control plan stay on the server.  A result
 * is written to sessionStorage before it is posted so an SSE reconnect can resend the
 * result without repeating the page action.
 *
 * The assistant panel is a separate module this file mounts and feeds.  It renders; it
 * never fetches, never holds the lease, and never keeps a timeline of its own -- what it
 * shows is the server's own projection, fetched here, which is why a full page navigation
 * no longer empties it.
 */

import {PanelHost, backoffAfter, classifyTransportError} from './panel_host.js'
export {PanelHost, classifyTransportError}

const WIRE_VERSION = 1
const MAX_WAIT_MS = 10_000
const PAGE_CONTROLLER_ANNOTATION_STYLE_ID = 'woa-hide-page-controller-annotations'
const MAX_CONTENT_CHARS = 120_000
const RESULT_POST_RESERVE_MS = 500
// The build this Runtime is qualified against. It is stated once in
// `policies/page_controller.json`; `test_page_controller.py` fails if this string
// drifts from it, because the two used to disagree silently and did.
const RUNTIME_NAME = 'woa-embedded/page-controller-1.12.4'
const GENERIC_SENSITIVE_NAME = '(?:password|passwd|pwd|secret|token|api[_-]?key|authorization)'
let SENSITIVE_NAME = new RegExp(GENERIC_SENSITIVE_NAME, 'i')
let HANDOFF_ASSIGNMENT = /(?!)/g
const URL_SECRET_ASSIGNMENT = /((?:^|[?&#;\s"'<>\\]|%(?:25)*(?:26|23|3[bBfF]))(?:access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|authorization|password|passwd|pwd|secret|token)(?:=|%(?:25)*3[dD]))(.+?)(?=(?:[&;/?#\s"'<>\\]|%(?:25)*(?:26|3[bBfF]|2[fF]|23)|$))/gi

function sensitiveFieldPattern(pattern) {
  return new RegExp(`\\b(?:type|name|id|autocomplete)=(?:"[^"]*(?:${pattern})[^"]*"|'[^']*(?:${pattern})[^']*'|[^\\s>]*(?:${pattern})[^\\s>]*)`, 'i')
}

let SENSITIVE_FIELD = sensitiveFieldPattern(GENERIC_SENSITIVE_NAME)

function configureSensitiveQueryKeys(value) {
  const keys = Array.isArray(value) ? value : []
  if (keys.length > 32 || keys.some(key =>
    typeof key !== 'string' || !/^[A-Za-z][A-Za-z0-9_.-]{1,63}$/.test(key))) {
    throw new Error('sensitiveQueryKeys must contain at most 32 safe query names')
  }
  const escaped = [...new Set(keys.map(key => key.toLowerCase()))]
    .map(key => key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  const namePattern = escaped.length
    ? `${GENERIC_SENSITIVE_NAME}|(?:${escaped.join('|')})` : GENERIC_SENSITIVE_NAME
  SENSITIVE_NAME = new RegExp(namePattern, 'i')
  SENSITIVE_FIELD = sensitiveFieldPattern(namePattern)
  HANDOFF_ASSIGNMENT = escaped.length
    ? new RegExp(`((?:${escaped.join('|')})(?:=|%(?:25)*3[dD]))(.+?)(?=(?:[&;/?#\\s"<>\\\\]|%(?:25)*(?:26|3[bBfF]|2[fF]|23)|$))`, 'gi')
    : /(?!)/g
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function hasResidualEncodedCredential(value) {
  let current = String(value ?? '')
  for (let depth = 0; depth <= 3; depth += 1) {
    const redacted = current.replace(HANDOFF_ASSIGNMENT, '$1[redacted]')
      .replace(URL_SECRET_ASSIGNMENT, '$1[redacted]')
    if (redacted !== current) return true
    try {
      const decoded = decodeURIComponent(current)
      if (decoded === current) break
      current = decoded
    } catch { break }
  }
  return false
}

function redactString(value) {
  let out = String(value ?? '')
  // The expression understands repeatedly encoded '=' and delimiters.  Keep the rest of the
  // URL byte-for-byte intact because link allowlisting compares its exact target.
  for (let i = 0; i < 3; i += 1) {
    const next = out.replace(HANDOFF_ASSIGNMENT, '$1[redacted]')
      .replace(URL_SECRET_ASSIGNMENT, '$1[redacted]')
    if (next === out) break
    out = next
  }
  return hasResidualEncodedCredential(out) ? '[redacted]' : out
}

function sanitizePageContent(content) {
  let out = redactString(content)
  // PageController sometimes serializes an unquoted input value containing spaces.  Once a
  // record identifies a sensitive field, consume the whole value tail rather than guessing
  // where an unquoted value ends.  Losing later presentation attributes is safer than sending
  // part of a credential to the server.
  out = out.replace(/(?:^|\n)\s*\*?\[\d+]<[\w-]+[\s\S]*?\s*\/>?(?=\n|$)/g, record => {
    if (!SENSITIVE_FIELD.test(record)) return record
    let clean = record.replace(/\bvalue=(?:"[^"]*"|'[^']*'|[\s\S]*?)(?=\s*\/?>\s*$)/gi,
      'value=[redacted]')
    const separator = clean.indexOf('>')
    const close = clean.lastIndexOf('/>')
    if (separator >= 0 && close > separator) {
      clean = `${clean.slice(0, separator + 1)}[redacted] ${clean.slice(close)}`
    }
    return clean
  })
  return out.slice(0, MAX_CONTENT_CHARS)
}

function safeArgs(args) {
  const sanitize = (key, value) => {
    if (key === 'value' || SENSITIVE_NAME.test(key)) return '[redacted]'
    if (Array.isArray(value)) return value.map(item => sanitize('', item))
    if (value && typeof value === 'object') {
      return Object.fromEntries(Object.entries(value).map(([child, item]) => [child, sanitize(child, item)]))
    }
    return typeof value === 'string' ? redactString(value) : value
  }
  const out = {}
  for (const [key, value] of Object.entries(args || {})) {
    out[key] = sanitize(key, value)
  }
  return out
}

function sameOriginUrl(value) {
  const url = new URL(value, window.location.href)
  if (url.origin !== window.location.origin) throw new Error(`origin_refused: ${url.origin}`)
  return url.href
}

function explicitAttribute(element, name) {
  if (!element) return null
  if (typeof element.hasAttribute === 'function') {
    return element.hasAttribute(name) ? String(element.getAttribute(name) || '') : null
  }
  return Object.hasOwn(element, name) ? String(element[name] || '') : null
}

function baseTarget(ownerDocument = globalThis.document) {
  const base = ownerDocument?.querySelector?.('base[target]')
  return String(base?.getAttribute?.('target') || base?.target || '')
}

function targetsCurrentPage(value) {
  const target = String(value || '').trim().toLowerCase()
  return !target || target === '_self'
}

function effectiveTarget(element, form = null) {
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

function navigationWatch(element) {
  const ownerDocument = element?.ownerDocument || globalThis.document
  const context = ownerDocument?.defaultView || globalThis.window
  let frameElement = null
  try { frameElement = context === globalThis.window ? null : context?.frameElement } catch {
    frameElement = null
  }
  let startedUrl = ''
  try { startedUrl = absoluteUrl(context?.location?.href || '') } catch { /* opaque frame */ }
  return {context, document: ownerDocument, frameElement, startedUrl}
}

function navigationCommitted(context, urlBefore, documentBefore, frameElement = null) {
  try {
    const liveContext = frameElement?.contentWindow || context
    const liveDocument = frameElement?.contentDocument || liveContext?.document
    const currentUrl = absoluteUrl(liveContext?.location?.href || '')
    return Boolean(currentUrl && currentUrl !== urlBefore) || Boolean(
      documentBefore && liveDocument && liveDocument !== documentBefore)
  } catch { return false }
}

function armChildFrameNavigation(frameElement) {
  if (!frameElement?.addEventListener) return null
  let committed = false
  let resolveCommitted
  const promise = new Promise(resolve => { resolveCommitted = resolve })
  const onLoad = () => {
    committed = true
    resolveCommitted({navigated: true, message: 'child frame navigation committed'})
  }
  frameElement.addEventListener('load', onLoad, {once: true})
  return {
    get committed() { return committed },
    promise,
    dispose() { frameElement.removeEventListener('load', onLoad) },
  }
}

function isSubmitControl(element) {
  const tag = String(element?.tagName || '').toUpperCase()
  const type = String(element?.type || '').toLowerCase()
  return (tag === 'BUTTON' && (!type || type === 'submit')) ||
    (tag === 'INPUT' && ['submit', 'image'].includes(type))
}

function refIndex(ref) {
  const match = /^e(\d+)$/.exec(String(ref || ''))
  if (!match) throw new Error('bad_arguments: action needs an e<number> ref')
  return Number(match[1])
}

function actionElementFingerprint(element) {
  if (!element || element.isConnected === false) return 'missing'
  const form = String(element.tagName || '').toUpperCase() === 'FORM'
    ? element : element.form
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

function attrsFrom(text) {
  const attrs = {}
  const matcher = /([\w:-]+)=(?:"([^"]*)"|'([^']*)'|([^\s>]+))/g
  for (const match of text.matchAll(matcher)) {
    const key = match[1]
    const value = match[2] ?? match[3] ?? match[4] ?? ''
    attrs[key] = SENSITIVE_NAME.test(key) ? '[redacted]' : redactString(value)
  }
  return attrs
}

function roleFor(tag, attrs) {
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

/** Convert PageController's stable numeric indices into the existing server ref schema. */
function elementsFrom(content, epoch) {
  const elements = []
  const records = /(?:^|\n)\s*\*?\[(\d+)]<([\w-]+)([\s\S]*?)\s*\/>?(?=\n|$)/g
  for (const match of content.matchAll(records)) {
    const index = Number(match[1])
    const tag = match[2].toLowerCase()
    const body = match[3] || ''
    const separator = body.indexOf('>')
    const attrs = attrsFrom(separator >= 0 ? body.slice(0, separator) : body)
    const text = separator >= 0 ? body.slice(separator + 1) : ''
    const role = roleFor(tag, attrs)
    // Legacy applications often use labelled input[type=submit] controls. Unlike a button,
    // they have no inner text, so retain the button value as the accessible name. Textbox
    // values remain state and never become their accessible name.
    const buttonValue = role === 'button' ? attrs.value : ''
    const name = SENSITIVE_NAME.test(String(attrs.name || ''))
      ? ''
      : redactString((text || attrs['aria-label'] || attrs.placeholder || buttonValue || '')
        .trim()).slice(0, 120)
    elements.push({ref: `e${index}`, role, name, tag, epoch, attrs})
  }
  return elements
}

/**
 * Add policy-critical link facts from the live DOM rather than PageController's display
 * text. PageController intentionally abbreviates long attributes with `...`; that text is
 * useful model context but cannot be the source of truth for an exact-link allowlist.
 */
function hydrateLinkFacts(elements, selectorMap, getByIndex) {
  for (const item of elements) {
    if (item.role !== 'link') continue
    try {
      const element = getByIndex(selectorMap, refIndex(item.ref))
      const rawHref = explicitAttribute(element, 'href')
      if (rawHref !== null && rawHref) item.attrs.href = redactString(rawHref)
      const rawTarget = explicitAttribute(element, 'target')
      if (rawTarget !== null) item.attrs.target = redactString(rawTarget)
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
function hydrateFormFacts(elements, selectorMap, getByIndex) {
  for (const item of elements) {
    if (!['textbox', 'combobox', 'select'].includes(item.role)) continue
    try {
      const element = getByIndex(selectorMap, refIndex(item.ref))
      const owner = typeof element?.closest === 'function' ? element.closest('[name]') : null
      const rawField = explicitAttribute(owner || element, 'name')
      if (rawField !== null && /^[A-Za-z][A-Za-z0-9_.-]{0,79}$/.test(rawField)
          && !SENSITIVE_NAME.test(rawField)) {
        item.attrs.field = rawField
      }
    } catch {
      // A stale selector or an unusual DOM shape supplies no semantic field fact. The
      // server will refuse an adapter draft that cannot resolve to one unique live field.
    }
  }
  return elements
}

function storageKey(sessionId) {
  return `woa.remote.v1.${sessionId}`
}

function readStored(sessionId) {
  try { return JSON.parse(sessionStorage.getItem(storageKey(sessionId))) || {} } catch { return {} }
}

function writeStored(sessionId, value) {
  sessionStorage.setItem(storageKey(sessionId), JSON.stringify(value))
}

function canonicalUrl(value) {
  const base = typeof location !== 'undefined' ? location.href : 'http://invalid.local/'
  const url = new URL(value, base)
  url.hash = ''
  return url.href
}

function absoluteUrl(value) {
  const base = typeof location !== 'undefined' ? location.href : 'http://invalid.local/'
  return new URL(value, base).href
}

async function urlDigest(value) {
  if (!value) return ''
  const bytes = new TextEncoder().encode(canonicalUrl(value))
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
}

async function navigationWal(startedUrl, expectedUrl, navigationKind) {
  const started = absoluteUrl(startedUrl)
  const expected = expectedUrl ? absoluteUrl(expectedUrl) : ''
  const [startedDigest, expectedDigest] = await Promise.all([
    urlDigest(started), expected ? urlDigest(expected) : '',
  ])
  return {
    started_url_digest: startedDigest,
    started_url_display: redactString(started),
    expected_url_digest: expectedDigest,
    expected_url_display: expected ? redactString(expected) : '',
    navigation_kind: navigationKind,
  }
}

function scrubLegacyNavigationUrls(action) {
  if (!action || typeof action !== 'object') return action
  const clean = {...action}
  if (Object.hasOwn(clean, 'started_url')) {
    clean.started_url_display ||= redactString(clean.started_url)
    delete clean.started_url
  }
  if (Object.hasOwn(clean, 'expected_url')) {
    clean.expected_url_display ||= redactString(clean.expected_url)
    delete clean.expected_url
  }
  return clean
}

async function recoveryProvesNavigation(action, observationUrl, currentDocumentId = '') {
  const expected = action?.expected_url_digest || ''
  const before = action?.started_url_digest || ''
  const after = observationUrl ? await urlDigest(observationUrl) : ''
  if (action?.phase === 'claimed' && action?.navigation_kind === 'form') {
    // A form may POST back to the same URL.  A new JS document proves the claimed native
    // submission committed a navigation without pretending its business effect succeeded.
    // A Runtime restart in the same document retains the global id and cannot forge this.
    return Boolean(action.document_id && currentDocumentId &&
      action.document_id !== currentDocumentId)
  }
  return action?.phase === 'claimed' && ['goto', 'link', 'form-get'].includes(action?.navigation_kind) &&
    Boolean(expected) && expected !== before && after === expected
}

function formGetUrl(native, submitter = null) {
  if (!native?.form || native.method !== 'get') return ''
  const destination = new URL(native.url, location.href)
  const params = new URLSearchParams()
  let data
  try {
    data = submitter ? new FormData(native.form, submitter) : new FormData(native.form)
  } catch {
    data = new FormData(native.form)
    const name = explicitAttribute(submitter, 'name')
    if (name && !submitter?.disabled) data.append(name, String(submitter?.value || ''))
  }
  for (const [name, value] of data.entries()) {
    if (typeof value === 'string') params.append(name, value)
  }
  // HTML's GET submission replaces the action query with the form entry list.
  destination.search = params.toString()
  return sameOriginUrl(destination.href)
}

function safeErrorDetail(detail, command) {
  let out = redactString(detail)
  const value = command?.args?.value
  if (value !== undefined && value !== null && String(value)) {
    out = out.split(String(value)).join('[redacted]')
  }
  return out.slice(0, 800)
}

function suppressPageControllerAnnotations() {
  if (document.getElementById(PAGE_CONTROLLER_ANNOTATION_STYLE_ID)) return
  const style = document.createElement('style')
  style.id = PAGE_CONTROLLER_ANNOTATION_STYLE_ID
  style.textContent = '#playwright-highlight-container{display:none!important}'
  document.documentElement.appendChild(style)
}


export class EmbeddedWebRuntime {
  constructor(config) {
    if (!config?.endpoint || !config?.sessionId || !config?.clientToken) {
      throw new Error('endpoint, sessionId and clientToken are required')
    }
    this.config = config
    configureSensitiveQueryKeys(config.sensitiveQueryKeys || [])
    this.endpoint = String(config.endpoint).replace(/\/$/, '')
    this.sessionId = String(config.sessionId)
    this.token = String(config.clientToken)
    this.applicationId = String(config.applicationId || '')
    this.principalBinding = String(config.principalBinding || '')
    if (Boolean(this.applicationId) !== Boolean(this.principalBinding)) {
      throw new Error('applicationId and principalBinding must be supplied together')
    }
    window.__WOA_DOCUMENT_ID__ ||= crypto.randomUUID()
    this.documentId = window.__WOA_DOCUMENT_ID__
    this.runtimeInstanceId = crypto.randomUUID()
    this.leaseId = ''
    this.stored = readStored(this.sessionId)
    this.stored.clientId ||= crypto.randomUUID()
    this.stored.epoch = Number(this.stored.epoch || 0)
    this.stored.lastEventId ||= ''
    this.stored.inFlight = scrubLegacyNavigationUrls(this.stored.inFlight || null)
    this.stored.cachedResult ||= null
    this.stored.approvals ||= {}
    this.stored.taskInFlight = Boolean(this.stored.taskInFlight)
    this.stored.activeMessageId = String(this.stored.activeMessageId || '')
    this.stored.cancellationPending = Boolean(this.stored.cancellationPending)
    writeStored(this.sessionId, this.stored)
    this.currentObservation = null
    this.observationFingerprints = new Map()
    this.observedUrl = ''
    this.stopped = false
    this.eventAbortController = null
    this.controller = null
    this.panelEnabled = config.panel !== false
    this.panelContainer = null
    this.panelHost = null
    this.panelHandle = null
    this.panelConfiguration = null
    this.panelCursor = ''
    this.panelAttachmentObserver = null
    this._expiryTimer = 0
    if (this.panelEnabled && typeof document !== 'undefined') {
      // Created before the PageController so it can be excluded from the page the agent
      // observes; the panel itself is mounted into it once hello returns its configuration.
      this.panelContainer = document.createElement('div')
      this.panelContainer.id = 'woa-assistant-root'
      this.panelContainer.setAttribute('data-page-agent-not-interactive', 'true')
      // A non-interactive parent does not exclude its descendants or shadow text.
      this.panelContainer.setAttribute('data-page-agent-ignore', 'true')
      // The host is fixed-position and belongs in <body>. Appending an extra element next
      // to <body> under <html> violates the document structure and, on Odoo, makes the two
      // nodes participate in its root layout: the business application visibly loses part
      // of the viewport. The attachment watcher below handles an application that really
      // replaces body or removes this integration-owned child.
      document.body?.appendChild(this.panelContainer)
      this._watchPanelAttachment()
    }
    this._onPageShow = event => {
      if (!event?.persisted || this.stopped) return
      // A BFCache restore resumes the suspended JavaScript heap, whose runtime lease may have
      // been retired by the intervening document.  Rebuild from durable sessionStorage in the
      // same document; reloading here could resubmit a POST history entry.
      const restartConfig = this.config
      this.stop()
      window.__WOA_EMBEDDED_RUNTIME_STARTED__ = false
      queueMicrotask(() => { void startEmbeddedRuntime(restartConfig).catch(() => {}) })
    }
    this._onOnline = () => {
      this.panelHost?.setConnection({online: true})
      this.panelHost?.clearNotice()
    }
    this._onOffline = () => {
      this.panelHost?.setConnection({online: false})
      this.panelHost?.raise('offline', {retryable: false})
    }
    window.addEventListener?.('pageshow', this._onPageShow)
    window.addEventListener?.('online', this._onOnline)
    window.addEventListener?.('offline', this._onOffline)
  }

  _save() { writeStored(this.sessionId, this.stored) }

  _panelState(state) {
    this.panelHost?.setConnection({state})
  }

  _watchPanelAttachment() {
    if (!this.panelEnabled || this.stopped || !this.panelContainer
        || typeof MutationObserver === 'undefined') return
    this.panelAttachmentObserver ||= new MutationObserver(() => {
      if (this.stopped || !this.panelContainer) return
      const parent = document.body
      if (!parent) return
      if (!this.panelContainer.isConnected || this.panelContainer.parentElement !== parent) {
        parent.appendChild(this.panelContainer)
      }
      this._watchPanelAttachment()
    })
    this.panelAttachmentObserver.disconnect()
    if (document.documentElement) {
      this.panelAttachmentObserver.observe(document.documentElement, {childList: true})
    }
    if (document.body) {
      this.panelAttachmentObserver.observe(document.body, {childList: true})
    }
  }

  /** One place where a failed request becomes a panel state and a stop-or-retry decision. */
  _raise(error, fallback = 'submit_failed') {
    const verdict = classifyTransportError(error, this.panelConfiguration)
    if (verdict.terminal) {
      this.stopped = true
      this.eventAbortController?.abort()
      this._panelState(verdict.state)
      this.panelHost?.raise(verdict.code, {retryable: false})
    } else {
      this.panelHost?.raise(error?.code || fallback, {retryable: true})
    }
    return verdict
  }

  async _mountPanel() {
    if (!this.panelEnabled || !this.panelContainer || this.panelHandle) return
    this.panelHost = new PanelHost({
      configuration: this.panelConfiguration,
      actions: {
        submit: text => { void this.submitTask(text).catch(() => {}) },
        cancel: () => { void this.cancelTask().catch(() => {}) },
        decide: (approvalId, decision) => {
          const request = this.panelHost?.snapshot().pendingApproval
          if (!request || request.approval_id !== approvalId) return
          void this._postApproval(request, decision).catch(error => this._raise(error))
        },
        newConversation: () => {
          void this._post('/conversation', {action: 'new'})
            .then(() => this._refreshTimeline())
            .catch(error => this._raise(error))
        },
        loadOlder: before => {
          void this._loadOlder(before).catch(error => this._raise(error))
        },
      },
    })
    const moduleUrl = this.config.panelModule
      || `${this.endpoint}/v1/runtime/panel/panel.js`
    const panel = await import(/* @vite-ignore */ moduleUrl)
    this.panelHandle = panel.mountPanel({
      host: this.panelHost,
      allowedOrigins: [location.origin],
      container: this.panelContainer,
    })
  }

  async _refreshTimeline() {
    if (!this.panelHost) return
    const page = await this._timeline({after: this.panelCursor})
    this.panelCursor = page.cursor || this.panelCursor
    this.panelHost.applyTimeline(page)
  }

  async _loadOlder(before) {
    if (!this.panelHost) {
      return
    }
    try {
      const page = await this._timeline({before})
      this.panelHost.applyTimeline(page, {prepend: true})
    } finally {
      this.panelHost.setLoadingOlder(false)
    }
  }

  async _timeline({after = '', before = ''} = {}) {
    const query = new URLSearchParams()
    if (after) query.set('after', after)
    if (before) query.set('before', before)
    const suffix = query.toString()
    const response = await fetch(
      `${this.endpoint}/v1/sessions/${encodeURIComponent(this.sessionId)}/timeline${suffix ? `?${suffix}` : ''}`,
      {headers: this._headers(), credentials: 'omit', cache: 'no-store'})
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) {
      const error = new Error(payload.error || `timeline returned ${response.status}`)
      error.code = payload.code || ''
      error.status = response.status
      throw error
    }
    return payload
  }


  _headers(extra = {}, includeLease = true) {
    const headers = {Authorization: `Bearer ${this.token}`, ...extra}
    if (includeLease && this.leaseId) headers['X-WOA-Lease'] = this.leaseId
    return headers
  }

  async _post(path, body, {includeLease = true} = {}) {
    const response = await fetch(`${this.endpoint}/v1/sessions/${encodeURIComponent(this.sessionId)}${path}`, {
      method: 'POST', credentials: 'omit', cache: 'no-store',
      headers: this._headers({'content-type': 'application/json'}, includeLease),
      body: JSON.stringify(body),
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) {
      const error = new Error(payload.error || `${path} returned ${response.status}`)
      error.code = payload.code || ''
      error.status = response.status
      throw error
    }
    return payload
  }

  async start() {
    if (!this.config.pageControllerModule) {
      throw new Error('pageControllerModule must be an absolute http(s) module URL')
    }
    const moduleUrl = new URL(this.config.pageControllerModule, location.href)
    if (!['http:', 'https:'].includes(moduleUrl.protocol)) {
      throw new Error('pageControllerModule must be an absolute http(s) module URL')
    }
    suppressPageControllerAnnotations()
    const controllerModule = await import(moduleUrl.href)
    const {PageController} = controllerModule
    this.getElementByIndex = controllerModule.getElementByIndex
    if (!PageController || !this.getElementByIndex) {
      throw new Error('pageControllerModule does not expose the required PageController API')
    }
    this.controller = new PageController({
      enableMask: false,
      highlightOpacity: 0,
      highlightLabelOpacity: 0,
      viewportExpansion: -1,
      keepSemanticTags: true,
      includeAttributes: [
        'href', 'action', 'method', 'target', 'formaction', 'formmethod', 'formtarget',
        'type', 'name', 'id', 'value',
        'aria-label', 'placeholder', 'autocomplete',
      ],
      interactiveBlacklist: this.panelContainer ? [this.panelContainer] : [],
    })
    return this._startConnectedRuntime()
  }

  async _startConnectedRuntime() {
    await this._hello()
    // A definite result cached before a reload must reach the server before a newer page
    // observation.  Otherwise the older observation carried by that result would correctly be
    // rejected as stale, leaving the server unable to tell whether the action ran.
    let resultDeferred = false
    if (this.stored.cachedResult) {
      try { await this._sendResult(this.stored.cachedResult) } catch {
        resultDeferred = true
      }
    }
    if (!resultDeferred) {
      const observation = await this.observe(this.stored.inFlight?.action_id || '')
      await this._post('/observations', observation)
      if (!this.stored.cachedResult) {
        try { await this._recoverAfterNavigation(observation) } catch (error) {
          // _recoverAfterNavigation writes the outbox before POST.  A transport failure after
          // that write is recoverable here; errors before a result exists remain startup errors.
          if (!this.stored.cachedResult) throw error
          resultDeferred = true
        }
      }
    }
    await this._mountPanel()
    this.panelHost?.setConnection({
      busy: this.stored.taskInFlight,
      stopping: this.stored.cancellationPending,
      online: typeof navigator === 'undefined' || navigator.onLine !== false,
    })
    const connectedState = resultDeferred ? 'reconnecting'
      : (this.stored.cancellationPending ? 'stopping' : 'ready')
    this._panelState(connectedState)
    await this._refreshTimeline().catch(() => {})
    if (this.stored.cancellationPending) {
      void this.cancelTask({retry: true}).catch(() => {})
    }
    if (resultDeferred) {
      void this._resumeAfterDeferredResult()
    } else {
      void this._eventLoop()
    }
    return this
  }

  async submitTask(text) {
    if (this.stopped) throw new Error('embedded runtime is stopped')
    if (this.stored.taskInFlight) throw new Error('another task is still running')
    const goal = String(text || '').trim()
    if (!goal || goal.length > 2000) {
      throw new Error('task must contain 1-2000 characters')
    }
    const messageId = crypto.randomUUID()
    this.stored.taskInFlight = true
    this.stored.activeMessageId = messageId
    this.stored.cancellationPending = false
    this._save()
    // A completed cancellation belongs to the preceding message.  Reset both flags at the
    // new-message boundary as well as at the terminal event so a delayed projection cannot
    // leave the new stop control disabled.
    this.panelHost?.setConnection({busy: true, stopping: false})
    this._panelState('observing')
    // Echo the message immediately; the server's own `message_accepted` event replaces it.
    this.panelHost?.setOptimistic({turnId: messageId, text: goal, failed: false})
    try {
      // A first-party SPA can change route without replacing this JavaScript runtime. Bind
      // every new task to a fresh observation before posting the message, so the server does
      // not plan from the document/route that happened to be current at bootstrap time.
      const observation = await this.observe()
      await this._post('/observations', observation)
      const response = await this._post('/messages', {
        message_id: messageId, text: redactString(goal),
        page_url: observation.observation.url,
      })
      this._panelState('queued')
      return response
    } catch (error) {
      this.stored.taskInFlight = false
      this.stored.activeMessageId = ''
      this.stored.cancellationPending = false
      this._save()
      this.panelHost?.setConnection({busy: false, stopping: false})
      this.panelHost?.failOptimistic()
      this._raise(error)
      throw error
    }
  }

  async cancelTask({retry = false} = {}) {
    if (this.stopped || !this.stored.taskInFlight || !this.stored.activeMessageId) return
    if (this.stored.cancellationPending && !retry) return
    this._panelState('stopping')
    this.stored.cancellationPending = true
    this._save()
    this.panelHost?.setConnection({stopping: true})
    try {
      await this._post('/cancellations', {
        schema_version: WIRE_VERSION,
        session_id: this.sessionId,
        message_id: this.stored.activeMessageId,
      })
    } catch (error) {
      this.stored.cancellationPending = false
      this._save()
      this.panelHost?.setConnection({stopping: false})
      this._panelState('error')
      this.panelHost?.raise('cancel_failed', {retryable: true})
      throw error
    }
  }

  async _retryCachedResult() {
    let backoff = 0
    while (!this.stopped && this.stored.cachedResult) {
      try {
        await this._sendResult(this.stored.cachedResult)
        return true
      } catch (error) {
        if (this._raise(error, 'reconnecting').terminal) return false
        await sleep(backoff)
        backoff = backoffAfter(backoff)
      }
    }
    return false
  }

  async _resumeAfterDeferredResult() {
    if (!await this._retryCachedResult() || this.stopped) return
    // The cached result can describe the document that just navigated away.  Refresh the
    // selector map and server observation before consuming another action in this document.
    let backoff = 0
    while (!this.stopped) {
      try {
        const observation = await this.observe(this.stored.inFlight?.action_id || '')
        await this._post('/observations', observation)
        this._panelState('ready')
        if (!this.stopped) void this._eventLoop()
        return
      } catch (error) {
        if (this._raise(error, 'reconnecting').terminal) return
        await sleep(backoff)
        backoff = backoffAfter(backoff)
      }
    }
  }

  stop({removePanel = true} = {}) {
    this.stopped = true
    clearTimeout(this._expiryTimer)
    this.panelAttachmentObserver?.disconnect()
    this.panelAttachmentObserver = null
    this.eventAbortController?.abort()
    this.eventAbortController = null
    window.removeEventListener?.('pageshow', this._onPageShow)
    window.removeEventListener?.('online', this._onOnline)
    window.removeEventListener?.('offline', this._onOffline)
    this.controller?.dispose()
    if (removePanel) {
      this.panelHandle?.destroy()
      this.panelHandle = null
    this.panelConfiguration = null
    this.panelCursor = ''
    this._expiryTimer = 0
      this.panelContainer?.remove()
      this.panelContainer = null
    }
  }

  async _hello() {
    const reply = await this._post('/hello', {
      schema_version: WIRE_VERSION,
      session_id: this.sessionId,
      client_id: this.stored.clientId,
      runtime_instance_id: this.runtimeInstanceId,
      runtime: RUNTIME_NAME,
      capabilities: [
        'dom.observe', 'dom.click', 'dom.fill', 'dom.select', 'dom.submit',
        'page.goto', 'page.back', 'page.wait',
      ],
      page_origin: location.origin,
      application_id: this.applicationId,
      principal_digest: this.principalBinding,
      last_event_id: this.stored.lastEventId,
    }, {includeLease: false})
    if (!reply.lease_id) throw new Error('hello response did not include a runtime lease')
    this.leaseId = String(reply.lease_id)
    if (!reply.panel || typeof reply.panel.protocol !== 'number') {
      throw new Error('hello response did not include the panel configuration')
    }
    this.panelConfiguration = reply.panel
    this._armExpiry()
  }

  /**
   * Stop pretending a session is live past its own deadline.
   *
   * `principalExpiresAt` and `sessionExpiresAt` have been in the bootstrap since the
   * beginning and nothing read them, so an expired session looked like a network fault and
   * reconnected forever. Now it ends, and says which deadline it was.
   */
  _armExpiry() {
    clearTimeout(this._expiryTimer)
    const deadlines = [
      [Number(this.config.principalExpiresAt || 0)
        || Number(this.panelConfiguration?.principal_expires_at || 0), 'principal_expired'],
      [Number(this.config.sessionExpiresAt || 0)
        || Number(this.panelConfiguration?.session_expires_at || 0), 'session_expired'],
    ].filter(([at]) => at > 0)
    if (!deadlines.length) return
    const [at, code] = deadlines.reduce((a, b) => (a[0] <= b[0] ? a : b))
    const delay = at - Date.now()
    if (delay <= 0) {
      this._expire(code)
      return
    }
    this._expiryTimer = setTimeout(() => this._expire(code), Math.min(delay, 2_147_483_647))
  }

  _expire(code) {
    if (this.stopped) return
    this.stopped = true
    this.eventAbortController?.abort()
    this._panelState('ended')
    this.panelHost?.raise(code, {retryable: false})
  }

  async observe(causedByActionId = '') {
    const state = await this.controller.getBrowserState()
    // PageController uses numbered overlays while building its private selector map. They are
    // debugging aids, not product UI; removing the container does not change the stored refs.
    document.getElementById('playwright-highlight-container')?.remove()
    const content = sanitizePageContent(`${state.header}\n${state.content}\n${state.footer}`)
    const epoch = ++this.stored.epoch
    this._save()
    const elements = hydrateFormFacts(hydrateLinkFacts(
      elementsFrom(content, epoch), this.controller.selectorMap, this.getElementByIndex),
    this.controller.selectorMap, this.getElementByIndex)
    const fingerprints = new Map()
    for (const item of elements) {
      try {
        fingerprints.set(item.ref, actionElementFingerprint(
          this.getElementByIndex(this.controller.selectorMap, refIndex(item.ref))))
      } catch { fingerprints.set(item.ref, 'missing') }
    }
    const observation = {
      schema_version: WIRE_VERSION,
      epoch,
      url: redactString(state.url),
      title: redactString(state.title).slice(0, 300),
      landmarks: [],
      elements,
      content,
      messages: [],
      truncated: state.content.length > MAX_CONTENT_CHARS,
    }
    const envelope = {
      schema_version: WIRE_VERSION,
      session_id: this.sessionId,
      observation_id: `${this.stored.clientId}:${epoch}`,
      page_epoch: epoch,
      observation,
      events: [],
      caused_by_action_id: causedByActionId,
    }
    this.currentObservation = envelope
    this.observationFingerprints = fingerprints
    this.observedUrl = canonicalUrl(state.url)
    return envelope
  }

  _actionFreshness(command) {
    if (command?.action === 'observe') return {fresh: true, reason: ''}
    const observedUrl = this.observedUrl || canonicalUrl(
      this.currentObservation?.observation?.url || location.href)
    if (canonicalUrl(location.href) !== observedUrl) {
      return {fresh: false, reason: 'the page URL changed after the last observation'}
    }
    if (!['click', 'fill', 'select', 'submit'].includes(command?.action)) {
      return {fresh: true, reason: ''}
    }
    // Old constructed test shells have no map. Every real runtime initializes and replaces
    // this map during observe(), before it can receive an action.
    if (!(this.observationFingerprints instanceof Map)) {
      return {fresh: true, reason: ''}
    }
    const ref = String(command?.args?.ref || '')
    const expected = this.observationFingerprints.get(ref)
    let actual = 'missing'
    try {
      actual = actionElementFingerprint(
        this.getElementByIndex(this.controller.selectorMap, refIndex(ref)))
    } catch { /* missing stays fail-closed */ }
    if (expected === undefined || actual !== expected) {
      return {fresh: false, reason: 'the target element changed after the last observation'}
    }
    return {fresh: true, reason: ''}
  }

  async _recoverAfterNavigation(observation) {
    const action = this.stored.inFlight
    if (!action || action.page_epoch >= observation.page_epoch) return
    let result
    if (action.phase === 'prepared') {
      result = this._rejectedResult(action, observation, 'runtime_replaced',
        'The previous runtime ended before the action was dispatched.')
    } else if (await recoveryProvesNavigation(action, location.href, this.documentId)) {
      const receipt = this._receipt(action, true, '', '',
        observation, action.started_url_display !== observation.observation.url,
        Date.now() - action.started_at)
      result = this._completedResult(action, receipt, observation)
    } else {
      result = this._indeterminateResult(action, observation,
        'The prior page ended without a trustworthy receipt; the action will not be retried.')
    }
    this.stored.cachedResult = result
    this._save()
    await this._sendResult(result)
  }

  async _eventLoop() {
    let backoff = 0
    while (!this.stopped) {
      const abortController = new AbortController()
      this.eventAbortController = abortController
      try {
        const response = await fetch(`${this.endpoint}/v1/sessions/${encodeURIComponent(this.sessionId)}/events`, {
          headers: this._headers({'last-event-id': this.stored.lastEventId}),
          credentials: 'omit', cache: 'no-store', signal: abortController.signal,
        })
        if (!response.ok || !response.body) {
          const payload = await response.json().catch(() => ({}))
          const failure = new Error(payload.error || `event stream returned ${response.status}`)
          failure.code = payload.code || ''
          failure.status = response.status
          throw failure
        }
        backoff = 0
        this.panelHost?.clearNotice()
        await this._readEventStream(response.body)
      } catch (error) {
        if (this.stopped) return
        if (this._raise(error, 'reconnecting').terminal) return
        backoff = backoffAfter(backoff)
        await sleep(backoff)
      } finally {
        if (this.eventAbortController === abortController) {
          this.eventAbortController = null
        }
      }
    }
  }

  async _readEventStream(stream) {
    const reader = stream.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (!this.stopped) {
      const {value, done} = await reader.read()
      if (done) break
      buffer += decoder.decode(value, {stream: true}).replace(/\r\n/g, '\n')
      let end
      while ((end = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, end)
        buffer = buffer.slice(end + 2)
        if (!frame || frame.startsWith(':')) continue
        let id = ''
        const data = []
        for (const line of frame.split('\n')) {
          if (line.startsWith('id:')) id = line.slice(3).trim()
          if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
        }
        if (!data.length) continue
        const event = JSON.parse(data.join('\n'))
        await this._handleEvent(event)
        this.stored.lastEventId = id
        this._save()
        if (event.kind === 'action') {
          if (this.stored.cachedResult?.action_id === event.payload.action_id) {
            this.stored.cachedResult = null
          }
          if (this.stored.inFlight?.action_id === event.payload.action_id) {
            this.stored.inFlight = null
          }
          this._save()
        }
      }
      // One projection fetch per delivered batch, not per event: the panel renders the
      // server's timeline, so it has to come from the server.
      await this._refreshTimeline().catch(() => {})
    }
  }

  async _handleEvent(event) {
    const turnId = String(event.turn_id || event.payload?.turn_id || this.stored.activeMessageId || '')
    if (event.kind === 'action') {
      await this._handleAction(event.payload)
    } else if (event.kind === 'progress') {
      const progressState = String(event.payload.state || '')
      const mapped = this.panelConfiguration?.progress_states?.[progressState]
      const finished = Boolean(mapped?.terminal)
      if (finished) {
        this.stored.taskInFlight = false
        this.stored.activeMessageId = ''
        this.stored.cancellationPending = false
        this._save()
        this.panelHost?.setConnection({busy: false, stopping: false})
        this.panelHost?.clearOptimisticOnce(turnId)
      }
      // The panel state each progress state maps to is the server's table, not a second
      // copy of it here; an unmapped state means the contract moved and must be noticed.
      this._panelState(mapped ? mapped.panel_state : 'acting')
    } else if (event.kind === 'terminal') {
      this.stored.taskInFlight = false
      this.stored.activeMessageId = ''
      this.stored.cancellationPending = false
      this._save()
      this.panelHost?.setConnection({busy: false, stopping: false})
      this.panelHost?.clearOptimisticOnce(turnId)
      this._panelState('ended')
      // The terminal cursor is durable after this handler returns.  A closed server session
      // has no future events, so another GET would only turn the final UI into a reconnect loop.
      this.stopped = true
    } else if (event.kind === 'approval_required') {
      this._panelState('awaiting_confirmation')
      const decided = this.stored.approvals[event.payload.approval_id]
      if (decided) {
        // A document that reloaded between deciding and posting must finish the POST, not
        // ask the user a second time: the decision is already recorded and idempotent.
        await this._postApproval(event.payload, decided)
        return
      }
      if (!this.panelHost) {
        // No panel means nobody can be asked, and an unanswered write must not proceed.
        await this._postApproval(event.payload, 'denied')
        return
      }
      this.panelHost.setPendingApproval(event.payload)
    }
  }

  /** Record the decision before dispatching it, so a reload replays rather than re-asks. */
  async _postApproval(request, decision) {
    this.stored.approvals[request.approval_id] = decision
    this._save()
    await this._post('/approvals', {
      schema_version: WIRE_VERSION,
      session_id: this.sessionId,
      approval_id: request.approval_id,
      decision,
      principal_digest: String(request.principal_digest || ''),
      turn_id: String(request.turn_id || ''),
      operation_id: String(request.operation_id || ''),
    })
    this.panelHost?.setPendingApproval(null)
    this._panelState(decision === 'approved' ? 'acting' : 'cancelled')
  }

  async _handleAction(action) {
    if (this.stored.cachedResult?.action_id === action.action_id) {
      await this._sendResult(this.stored.cachedResult)
      return
    }
    if (this.stored.inFlight?.action_id === action.action_id) {
      if (this.stored.inFlight.phase !== 'prepared' ||
          this.stored.inFlight.runtime_instance_id !== this.runtimeInstanceId) {
        const observation = await this.observe(action.action_id)
        await this._post('/observations', observation)
        await this._recoverAfterNavigation(observation)
        return
      }
    }
    if (!this.currentObservation ||
        action.based_on_observation_id !== this.currentObservation.observation_id ||
        action.page_epoch !== this.currentObservation.page_epoch ||
        Date.now() > action.deadline) {
      const observation = await this.observe(action.action_id)
      const result = {
        schema_version: WIRE_VERSION, session_id: action.session_id, turn_id: action.turn_id,
        action_id: action.action_id, based_on_observation_id: action.based_on_observation_id,
        page_epoch: action.page_epoch, command_digest: action.command_digest,
        status: 'rejected', observation_after: observation,
        receipt: null,
        error_code: Date.now() > action.deadline ? 'deadline_expired' : 'stale_observation',
        error_detail: 'Action was not executed because its page observation is no longer current.',
      }
      this.stored.cachedResult = result
      this._save()
      await this._sendResult(result)
      return
    }

    const freshness = this._actionFreshness(action.command)
    if (!freshness.fresh) {
      const observation = await this.observe(action.action_id)
      const result = this._rejectedResult(
        action, observation, 'stale_observation',
        `Action was not executed because ${freshness.reason}.`)
      this.stored.cachedResult = result
      this._save()
      await this._sendResult(result)
      return
    }

    const priorPrepared = this.stored.inFlight?.action_id === action.action_id
      ? this.stored.inFlight : null
    const before = this.currentObservation
    const navigation = this._navigationExpectation(action.command, this.currentObservation)
    const startedUrl = absoluteUrl(location.href)
    const wal = priorPrepared || await navigationWal(
      startedUrl, navigation.url, navigation.kind)
    const executing = {...action, phase: 'prepared',
      started_at: priorPrepared?.started_at || Date.now(),
      started_url_raw: startedUrl,
      started_url_digest: wal.started_url_digest,
      started_url_display: priorPrepared?.started_url_display || before.observation.url,
      expected_url_digest: wal.expected_url_digest,
      expected_url_display: wal.expected_url_display,
      navigation_kind: wal.navigation_kind || navigation.kind,
      document_id: priorPrepared?.document_id || this.documentId,
      runtime_instance_id: this.runtimeInstanceId}
    if (!priorPrepared) {
      const {started_url_raw: _ephemeralStartedUrl, ...durable} = executing
      this.stored.inFlight = {
        ...durable,
        command: {...executing.command, args: safeArgs(executing.command.args)},
      }
      this._save() // write-ahead: everything below may destroy this JavaScript context
    }

    const claim = await this._post('/claims', {action_id: action.action_id})
    if (!claim.claimed) {
      const observation = await this.observe(action.action_id)
      await this._post('/observations', observation)
      const result = this._indeterminateResult(action, observation,
        'Another runtime claimed this action; its execution state cannot be trusted after takeover.')
      this.stored.cachedResult = result
      this._save()
      await this._sendResult(result)
      return
    }
    executing.phase = 'claimed'
    this.stored.inFlight.phase = 'claimed'
    this._save()
    this._panelState('acting')

    let success = true
    let code = ''
    let detail = ''
    // A native submit can commit a child-frame navigation before _execute returns. Arm the
    // stable iframe element first so that fast server responses cannot escape observation.
    const navigationSignal = armChildFrameNavigation(navigation.frameElement)
    try {
      const result = await this._execute(action.command)
      if (result?.native_navigation_expected || this._mayNavigate(action.command, before)) {
        const navigationResult = await this._waitForNavigation(
          navigation.startedUrl || executing.started_url_raw, action.deadline,
          navigation.context, navigation.document, navigation.frameElement, navigationSignal)
        if (!navigationResult.navigated) {
          throw new Error('navigation_timeout: expected navigation did not commit before deadline')
        }
      }
      success = result?.success !== false
      detail = success ? '' : safeErrorDetail(result?.message || '', action.command)
      if (!success) code = 'element_not_actionable'
    } catch (error) {
      success = false
      detail = safeErrorDetail(String(error?.message || error), action.command)
      if (detail.startsWith('origin_refused')) code = 'origin_refused'
      else if (detail.startsWith('bad_arguments')) code = 'bad_arguments'
      else if (detail.startsWith('navigation_timeout')) code = 'navigation_timeout'
      else code = 'page_error'
    } finally {
      navigationSignal?.dispose()
    }
    const observation = await this.observe(action.action_id)
    // The wire contract defines `navigated` as a change in the observed top-level URL. A
    // child-frame commit satisfies the action guard, while its new content is carried by the
    // fresh observation rather than being misreported as a top-level navigation.
    const receipt = this._receipt(executing, success, code, detail, observation,
      before.observation.url !== observation.observation.url,
      Date.now() - executing.started_at)
    const result = this._completedResult(executing, receipt, observation)
    this.stored.cachedResult = result
    this._save()
    await this._sendResult(result)
  }

  async _execute(command) {
    const args = command.args || {}
    if (command.action === 'observe') return {success: true, message: 'page observed'}
    if (command.action === 'click') {
      const element = this.getElementByIndex(this.controller.selectorMap, refIndex(args.ref))
      const native = this._nativeNavigation(element)
      if (native?.kind === 'form') {
        return this._withSubmitEvidence(
          native, () => this.controller.clickElement(refIndex(args.ref)))
      }
      return this.controller.clickElement(refIndex(args.ref))
    }
    if (command.action === 'submit') {
      const element = this.getElementByIndex(this.controller.selectorMap, refIndex(args.ref))
      const form = element.tagName === 'FORM' ? element : element.form
      if (!form) throw new Error('bad_arguments: submit ref does not belong to a form')
      const native = this._nativeNavigation(form)
      if (typeof form.requestSubmit === 'function') {
        return this._withSubmitEvidence(native, () => {
          form.requestSubmit()
          return {success: true, message: 'form submitted'}
        })
      }
      form.submit()
      return {success: true, message: 'form submitted',
        native_navigation_expected: native.method !== 'dialog'}
    }
    if (command.action === 'fill') {
      if (args.value === undefined) throw new Error('bad_arguments: fill needs value')
      return this.controller.inputText(refIndex(args.ref), String(args.value))
    }
    if (command.action === 'select') {
      if (args.value === undefined) throw new Error('bad_arguments: select needs value')
      return this.controller.selectOption(refIndex(args.ref), String(args.value))
    }
    if (command.action === 'wait_for') {
      await sleep(Math.min(Math.max(Number(args.ms || 1000), 0), MAX_WAIT_MS))
      return {success: true, message: 'wait completed'}
    }
    if (command.action === 'goto') {
      if (!args.url) throw new Error('bad_arguments: goto needs url')
      const href = sameOriginUrl(args.url)
      if (this.config.navigationMode === 'spa') {
        // The event must carry the state that was just pushed. A router is entitled to read
        // `event.state` and ignore a stateless popstate as "a click on an anchor" -- Odoo's
        // does exactly that and returns without routing -- so a bare PopStateEvent changes
        // the address bar while the application still shows the previous view. That failure
        // is silent: the URL is right and the page is wrong.
        const state = {woaNavigation: true}
        history.pushState(state, '', href)
        window.dispatchEvent(new PopStateEvent('popstate', {state: history.state ?? state}))
        return {success: true, message: 'SPA navigation requested'}
      }
      location.assign(href)
      return {success: true, message: 'navigation requested'}
    }
    if (command.action === 'back') {
      history.back()
      return {success: true, message: 'history navigation requested'}
    }
    throw new Error(`bad_arguments: unsupported action ${command.action}`)
  }

  _nativeNavigation(element) {
    const tag = String(element?.tagName || '').toUpperCase()
    if (tag === 'A' && element.href) {
      const target = effectiveTarget(element)
      if (!targetsCurrentPage(target)) {
        throw new Error('bad_arguments: link target must be the current page')
      }
      return {kind: 'link', url: sameOriginUrl(element.href), target}
    }
    const form = tag === 'FORM' ? element : (isSubmitControl(element) ? element.form : null)
    if (!form) return null
    const override = tag !== 'FORM' ? explicitAttribute(element, 'formaction') : null
    const action = override !== null
      ? (element.formAction || override || location.href)
      : (form.action || location.href)
    const target = effectiveTarget(tag === 'FORM' ? null : element, form)
    if (!targetsCurrentPage(target)) {
      throw new Error('bad_arguments: submit target must be the current page')
    }
    const methodOverride = tag !== 'FORM' ? explicitAttribute(element, 'formmethod') : null
    const method = String(methodOverride || form.method || 'get').toLowerCase()
    return {kind: 'form', url: sameOriginUrl(action), target, form, method}
  }

  async _withSubmitEvidence(native, invoke) {
    let submitEvent = null
    const capture = event => {
      if (event.target === native.form) submitEvent = event
    }
    native.form.addEventListener?.('submit', capture, true)
    try {
      const raw = await invoke()
      const result = raw && typeof raw === 'object' ? raw : {success: true}
      let validationFailed = false
      if (!submitEvent && typeof native.form.checkValidity === 'function') {
        try { validationFailed = native.form.checkValidity() === false } catch { /* opaque DOM */ }
      }
      if (validationFailed) {
        return {...result, success: false,
          message: 'form validation prevented submission',
          native_navigation_expected: false}
      }
      return {...result, native_navigation_expected: Boolean(
        submitEvent && !submitEvent.defaultPrevented && native.method !== 'dialog')}
    } finally {
      native.form.removeEventListener?.('submit', capture, true)
    }
  }

  _mayNavigate(command, before) {
    if (command.action === 'goto' || command.action === 'back') return true
    if (command.expect_navigation) return true
    if (command.action !== 'click') return false
    try {
      const element = this.getElementByIndex(
        this.controller.selectorMap, refIndex(command.args?.ref))
      return this._nativeNavigation(element)?.kind === 'link'
    } catch {
      // Invalid native targets are rejected by _execute.  Other JavaScript onclick behavior is
      // opaque unless the server explicitly supplied expect_navigation.
      return false
    }
  }

  _navigationExpectation(command, before) {
    try {
      if (command.action === 'goto' && command.args?.url) {
        return {kind: 'goto', url: sameOriginUrl(command.args.url),
          ...navigationWatch(globalThis.document?.documentElement)}
      }
      if (command.action === 'click') {
        const element = this.getElementByIndex(
          this.controller.selectorMap, refIndex(command.args?.ref))
        const native = this._nativeNavigation(element)
        const watch = navigationWatch(element)
        if (native?.kind === 'link') return {kind: 'link', url: native.url, ...watch}
        if (native?.kind === 'form' && native.method === 'get') {
          return {kind: 'form-get', url: formGetUrl(native, element), ...watch}
        }
        if (native?.kind === 'form') return {kind: 'form', url: '', ...watch}
      }
      if (command.action === 'submit') {
        const element = this.getElementByIndex(
          this.controller.selectorMap, refIndex(command.args?.ref))
        const native = this._nativeNavigation(element)
        const watch = navigationWatch(element)
        if (native?.kind === 'form' && native.method === 'get') {
          return {kind: 'form-get', url: formGetUrl(native), ...watch}
        }
        if (native?.kind === 'form') return {kind: 'form', url: '', ...watch}
      }
    } catch { /* execution returns the typed origin_refused receipt after claiming */ }
    // A form submission, history traversal, JavaScript click, or manual reload can have
    // arbitrary effects.  A URL observed later cannot prove those effects completed.
    return {kind: '', url: ''}
  }

  async _waitForNavigation(urlBefore, deadline, context = globalThis.window,
                           documentBefore = globalThis.document, frameElement = null,
                           navigationSignal = null) {
    if (navigationSignal?.committed ||
        navigationCommitted(context, urlBefore, documentBefore, frameElement)) {
      return {navigated: true, message: 'navigation committed'}
    }
    const configured = Math.min(
      Math.max(Number(this.config.navigationGuardMs || 45_000), 250), 60_000)
    const remaining = Math.max(0, Number(deadline) - Date.now() - RESULT_POST_RESERVE_MS)
    const cap = Math.min(configured, remaining)
    if (cap <= 0) {
      return {navigated: false, message: 'navigation guard exhausted'}
    }
    return new Promise(resolve => {
      let settled = false
      const topLevelNavigation = context === globalThis.window
      const finish = value => {
        if (settled) return
        settled = true
        clearInterval(poll)
        clearTimeout(timer)
        if (topLevelNavigation) window.removeEventListener('pagehide', onPageHide)
        resolve(value)
      }
      // Do not resolve on pagehide.  The old document is going away and its callback cannot
      // safely describe the new page; the next document will recover the persisted action.
      const onPageHide = () => {
        settled = true
        clearInterval(poll)
        clearTimeout(timer)
      }
      // Only a top-level unload destroys this Runtime and requires the next document to
      // recover the write-ahead action. A same-origin iframe unload leaves this Runtime
      // alive, so suppressing its timer would strand the action forever.
      if (topLevelNavigation) window.addEventListener('pagehide', onPageHide, {once: true})
      const poll = setInterval(() => {
        if (navigationCommitted(context, urlBefore, documentBefore, frameElement)) {
          finish({navigated: true, message: 'navigation committed'})
        }
      }, 100)
      const timer = setTimeout(
        () => finish({navigated: false, message: 'navigation guard elapsed'}), cap)
      navigationSignal?.promise.then(finish)
    })
  }

  _receipt(action, ok, errorCode, detail, observation, navigated, elapsedMs) {
    return {
      schema_version: WIRE_VERSION,
      command_id: action.action_id,
      action: action.command.action,
      args_echo: safeArgs(action.command.args),
      ok,
      error_code: errorCode,
      error_detail: safeErrorDetail(detail, action.command),
      url_before: action.started_url_display ||
        this.currentObservation?.observation.url || '',
      url_after: observation.observation.url,
      navigated,
      epoch_before: action.page_epoch,
      epoch_after: observation.page_epoch,
      elapsed_ms: Math.max(0, Math.round(elapsedMs || 0)),
      relogin: false,
      observation: observation.observation,
    }
  }

  _completedResult(action, receipt, observation) {
    return {
      schema_version: WIRE_VERSION, session_id: action.session_id, turn_id: action.turn_id,
      action_id: action.action_id, based_on_observation_id: action.based_on_observation_id,
      page_epoch: action.page_epoch, command_digest: action.command_digest,
      status: 'completed', observation_after: observation,
      receipt, error_code: '', error_detail: '',
    }
  }

  _rejectedResult(action, observation, code, detail) {
    return {
      schema_version: WIRE_VERSION, session_id: action.session_id, turn_id: action.turn_id,
      action_id: action.action_id, based_on_observation_id: action.based_on_observation_id,
      page_epoch: action.page_epoch, command_digest: action.command_digest,
      status: 'rejected', observation_after: observation, receipt: null,
      error_code: code, error_detail: detail,
    }
  }

  _indeterminateResult(action, observation, detail) {
    return {
      schema_version: WIRE_VERSION, session_id: action.session_id, turn_id: action.turn_id,
      action_id: action.action_id, based_on_observation_id: action.based_on_observation_id,
      page_epoch: action.page_epoch, command_digest: action.command_digest,
      status: 'indeterminate', observation_after: observation, receipt: null,
      error_code: 'execution_state_unknown', error_detail: detail,
    }
  }

  async _sendResult(result) {
    await this._post('/results', result)
    // Keep both records until the SSE event cursor is durably advanced.  A crash after the
    // POST response but before that cursor write would otherwise replay and execute again.
    const after = result.observation_after
    if (after && (!this.currentObservation ||
        after.page_epoch >= this.currentObservation.page_epoch)) {
      this.currentObservation = after
    }
    this._panelState('observing')
  }
}

export async function startEmbeddedRuntime(config) {
  if (window.__WOA_EMBEDDED_RUNTIME_STARTED__) return null
  window.__WOA_EMBEDDED_RUNTIME_STARTED__ = true
  const runtime = new EmbeddedWebRuntime(config)
  try { return await runtime.start() } catch (error) {
    runtime.stop({removePanel: false})
    runtime._panelState('error')
    runtime.panelHost?.raise(error?.code || 'connect_failed', {retryable: true})
    window.__WOA_EMBEDDED_RUNTIME_STARTED__ = false
    throw error
  }
}

// Pure boundary helpers are exported for deterministic conformance tests and for first-party
// shells that want to preflight their own redaction rules.  They contain no session state.
export const runtimeInternals = Object.freeze({
  redactString, sanitizePageContent, safeArgs, safeErrorDetail, elementsFrom,
  hydrateLinkFacts, hydrateFormFacts, actionElementFingerprint,
  urlDigest, navigationWal, recoveryProvesNavigation, formGetUrl,
  configureSensitiveQueryKeys,
})

if (typeof window !== 'undefined' && window.__WOA_EMBED_CONFIG__) {
  void startEmbeddedRuntime(window.__WOA_EMBED_CONFIG__).catch(() => {})
}
