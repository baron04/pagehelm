/**
 * Embedded Web Runtime for the server-owned Web Operation Agent.
 *
 * The browser owns DOM observation and generic actions only.  Task history, planning,
 * source knowledge, model credentials and the control plan stay on the server.  A result
 * is written to sessionStorage before it is posted so an SSE reconnect can resend the
 * result without repeating the page action.
 */

const WIRE_VERSION = 1
const MAX_WAIT_MS = 10_000
const PAGE_CONTROLLER_ANNOTATION_STYLE_ID = 'woa-hide-page-controller-annotations'
const MAX_CONTENT_CHARS = 120_000
const RESULT_POST_RESERVE_MS = 500
const RUNTIME_NAME = 'woa-embedded/page-controller-1.12.2'
const SENSITIVE_NAME = /(password|passwd|pwd|secret|token|api[_-]?key|authorization|externalLoginKey|externaLoginKey|externLoginKey|jsessionid)/i
const HANDOFF_ASSIGNMENT = /((?:externalLoginKey|externaLoginKey|externLoginKey|jsessionid)(?:=|%(?:25)*3[dD]))(.+?)(?=(?:[&;/?#\s"<>\\]|%(?:25)*(?:26|3[bBfF]|2[fF]|23)|$))/gi
const URL_SECRET_ASSIGNMENT = /((?:^|[?&#;\s"'<>\\]|%(?:25)*(?:26|23|3[bBfF]))(?:access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|authorization|password|passwd|pwd|secret|token)(?:=|%(?:25)*3[dD]))(.+?)(?=(?:[&;/?#\s"'<>\\]|%(?:25)*(?:26|3[bBfF]|2[fF]|23)|$))/gi
const SENSITIVE_FIELD = /\b(?:type|name|id|autocomplete)=(?:"[^"]*(?:password|passwd|pwd|secret|token|api[_-]?key)[^"]*"|'[^']*(?:password|passwd|pwd|secret|token|api[_-]?key)[^']*'|[^\s>]*(?:password|passwd|pwd|secret|token|api[_-]?key)[^\s>]*)/i

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

function baseTarget() {
  const base = globalThis.document?.querySelector?.('base[target]')
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
  return baseTarget()
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
    // OFBiz uses <input type=submit value="Find"> for many primary controls. Unlike a
    // button it has no inner text, so omitting value turns a visible labelled control into
    // an anonymous ref. Textbox values remain state, never their accessible name.
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

function buildPanel() {
  const host = document.createElement('aside')
  host.id = 'woa-assistant'
  host.setAttribute('data-page-agent-not-interactive', 'true')
  host.setAttribute('aria-label', 'AI 助手')
  // The host is already excluded from PageController observations. An open root makes the
  // confirmation surface testable and inspectable by the trusted first-party application;
  // the shadow mode is not an authorization boundary.
  const shadow = host.attachShadow({mode: 'open'})
  shadow.innerHTML = `
    <style>
      :host { all: initial; position: fixed; right: 20px; bottom: 20px; z-index: 2147483000;
        width: min(360px, calc(100vw - 32px)); color: #eaf0ff; font: 14px/1.45 ui-sans-serif,
        system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
      section { border: 1px solid rgba(130,154,210,.28); border-radius: 16px; overflow: hidden;
        background: linear-gradient(145deg, #111a31, #0a1020); box-shadow: 0 20px 55px rgba(0,0,0,.38); }
      header { display:flex; align-items:center; justify-content:space-between; padding:14px 16px 10px; }
      strong { font-size: 14px; letter-spacing:.02em; }
      output { color:#91a5cf; font-size:12px; }
      ol { margin:0; padding:0 16px 8px 34px; max-height:180px; overflow:auto; color:#c7d2ed; }
      li { margin:5px 0; white-space:pre-wrap; }
      .approval { margin:0 12px 12px; padding:12px; border:1px solid #526a9e;
        border-radius:12px; background:#111d38; }
      .approval p { margin:0 0 8px; }
      .approval ul { margin:0 0 10px; padding-left:20px; }
      .approval menu { display:flex; justify-content:flex-end; gap:8px; margin:0; padding:0; }
      .approval .deny { background:#263554; color:#eaf0ff; }
      form { display:flex; gap:8px; padding:12px; border-top:1px solid rgba(130,154,210,.18); }
      input { min-width:0; flex:1; border:1px solid #35466e; border-radius:10px; padding:9px 10px;
        background:#0b1327; color:#f5f7ff; outline:none; }
      input:focus { border-color:#7999e9; box-shadow:0 0 0 3px rgba(85,121,216,.18); }
      button { border:0; border-radius:10px; padding:9px 13px; background:#d8e2ff; color:#10182b;
        font-weight:650; cursor:pointer; }
      button:disabled, input:disabled { cursor:not-allowed; opacity:.58; }
    </style>
    <section><header><strong>AI 助手</strong><output aria-live="polite">正在连接</output></header>
      <ol aria-label="执行进度"></ol>
      <form><input maxlength="2000" aria-label="描述目标" placeholder="描述你想完成的目标" />
        <button type="submit">开始</button></form></section>`
  document.documentElement.appendChild(host)
  return {
    host,
    status: value => { shadow.querySelector('output').textContent = value },
    setBusy: value => {
      shadow.querySelector('input').disabled = Boolean(value)
      shadow.querySelector('button[type="submit"]').disabled = Boolean(value)
    },
    add: value => {
      const item = document.createElement('li')
      item.textContent = String(value).slice(0, 300)
      const list = shadow.querySelector('ol')
      list.appendChild(item)
      while (list.children.length > 30) list.firstElementChild.remove()
      list.scrollTop = list.scrollHeight
    },
    onTask: handler => shadow.querySelector('form').addEventListener('submit', async event => {
      event.preventDefault()
      const input = shadow.querySelector('input')
      const value = input.value.trim()
      if (!value) return
      input.value = ''
      await handler(value)
    }),
    requestApproval: request => new Promise(resolve => {
      shadow.querySelector('.approval')?.remove()
      const card = document.createElement('div')
      card.className = 'approval'
      card.setAttribute('role', 'group')
      card.setAttribute('aria-label', '操作确认')
      const effect = document.createElement('p')
      effect.textContent = `将执行：${String(request.effect).slice(0, 300)}`
      const targets = document.createElement('ul')
      for (const value of request.targets || []) {
        const item = document.createElement('li')
        item.textContent = String(value).slice(0, 300)
        targets.appendChild(item)
      }
      const menu = document.createElement('menu')
      const deny = document.createElement('button')
      deny.type = 'button'; deny.className = 'deny'; deny.textContent = '取消'
      const approve = document.createElement('button')
      approve.type = 'button'; approve.textContent = '确认执行'
      menu.append(deny, approve)
      card.append(effect, targets, menu)
      shadow.querySelector('section').insertBefore(card, shadow.querySelector('form'))
      let settled = false
      const finish = decision => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        card.remove()
        resolve(decision)
      }
      deny.addEventListener('click', () => finish('denied'), {once: true})
      approve.addEventListener('click', () => finish('approved'), {once: true})
      const remaining = Math.max(0, Number(request.expires_at || 0) - Date.now())
      const timer = setTimeout(() => finish('denied'), Math.min(remaining, 2_147_483_647))
    }),
  }
}

function suppressPageControllerAnnotations() {
  if (document.getElementById(PAGE_CONTROLLER_ANNOTATION_STYLE_ID)) return
  const style = document.createElement('style')
  style.id = PAGE_CONTROLLER_ANNOTATION_STYLE_ID
  style.textContent = '#playwright-highlight-container{display:none!important}'
  document.documentElement.appendChild(style)
}

export function describeAction(command) {
  const action = String(command?.action || '')
  const args = command?.args || {}
  if (action === 'goto') return `打开页面：${redactString(args.url || '')}`
  if (action === 'back') return '返回上一页'
  if (action === 'click') return `点击页面控件：${String(args.ref || '')}`
  if (action === 'fill') return `填写页面字段：${String(args.ref || '')}`
  if (action === 'select') return `选择页面选项：${String(args.ref || '')}`
  if (action === 'submit') return `提交页面表单：${String(args.ref || '')}`
  if (action === 'wait_for') return '等待页面状态更新'
  if (action === 'observe') return '重新检查当前页面'
  return action ? `执行页面动作：${action}` : '执行页面动作'
}

export class EmbeddedWebRuntime {
  constructor(config) {
    if (!config?.endpoint || !config?.sessionId || !config?.clientToken) {
      throw new Error('endpoint, sessionId and clientToken are required')
    }
    this.config = config
    this.endpoint = String(config.endpoint).replace(/\/$/, '')
    this.sessionId = String(config.sessionId)
    this.token = String(config.clientToken)
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
    writeStored(this.sessionId, this.stored)
    this.currentObservation = null
    this.stopped = false
    this.eventAbortController = null
    this.controller = null
    this.panel = config.panel === false ? null : buildPanel()
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
    window.addEventListener?.('pageshow', this._onPageShow)
  }

  _save() { writeStored(this.sessionId, this.stored) }

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
      interactiveBlacklist: this.panel ? [this.panel.host] : [],
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
    this.panel?.onTask(text => this.submitTask(text))
    this.panel?.setBusy?.(this.stored.taskInFlight)
    this.panel?.status(resultDeferred ? '正在重连' : '已连接')
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
    this.stored.taskInFlight = true
    this._save()
    this.panel?.setBusy?.(true)
    this.panel?.status('已提交')
    this.panel?.add(`目标：${goal}`)
    try {
      // A first-party SPA can change route without replacing this JavaScript runtime. Bind
      // every new task to a fresh observation before posting the message, so the server does
      // not plan from the document/route that happened to be current at bootstrap time.
      const observation = await this.observe()
      await this._post('/observations', observation)
      return await this._post('/messages', {
        message_id: crypto.randomUUID(), text: redactString(goal),
        page_url: observation.observation.url,
      })
    } catch (error) {
      this.stored.taskInFlight = false
      this._save()
      this.panel?.setBusy?.(false)
      throw error
    }
  }

  async _retryCachedResult() {
    let backoff = 250
    while (!this.stopped && this.stored.cachedResult) {
      try {
        await this._sendResult(this.stored.cachedResult)
        return true
      } catch (error) {
        if (['runtime_replaced', 'session_closed'].includes(error?.code)) {
          this.stopped = true
          this.panel?.status(error.code === 'session_closed' ? '已结束' : '已由另一页面接管')
          return false
        }
        this.panel?.status('正在重连')
        await sleep(backoff)
        backoff = Math.min(backoff * 2, 4000)
      }
    }
    return false
  }

  async _resumeAfterDeferredResult() {
    if (!await this._retryCachedResult() || this.stopped) return
    // The cached result can describe the document that just navigated away.  Refresh the
    // selector map and server observation before consuming another action in this document.
    let backoff = 250
    while (!this.stopped) {
      try {
        const observation = await this.observe(this.stored.inFlight?.action_id || '')
        await this._post('/observations', observation)
        this.panel?.status('已连接')
        if (!this.stopped) void this._eventLoop()
        return
      } catch (error) {
        if (['runtime_replaced', 'session_closed'].includes(error?.code)) {
          this.stopped = true
          this.panel?.status(error.code === 'session_closed' ? '已结束' : '已由另一页面接管')
          return
        }
        this.panel?.status('正在重连')
        await sleep(backoff)
        backoff = Math.min(backoff * 2, 4000)
      }
    }
  }

  stop() {
    this.stopped = true
    this.eventAbortController?.abort()
    this.eventAbortController = null
    window.removeEventListener?.('pageshow', this._onPageShow)
    this.controller?.dispose()
    this.panel?.host.remove()
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
      last_event_id: this.stored.lastEventId,
    }, {includeLease: false})
    if (!reply.lease_id) throw new Error('hello response did not include a runtime lease')
    this.leaseId = String(reply.lease_id)
  }

  async observe(causedByActionId = '') {
    const state = await this.controller.getBrowserState()
    // PageController uses numbered overlays while building its private selector map. They are
    // debugging aids, not product UI; removing the container does not change the stored refs.
    document.getElementById('playwright-highlight-container')?.remove()
    const content = sanitizePageContent(`${state.header}\n${state.content}\n${state.footer}`)
    const epoch = ++this.stored.epoch
    this._save()
    const elements = hydrateLinkFacts(
      elementsFrom(content, epoch), this.controller.selectorMap, this.getElementByIndex)
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
    return envelope
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
    let backoff = 250
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
          if (payload.code === 'runtime_replaced' || payload.code === 'session_closed') {
            this.stopped = true
            this.panel?.status(payload.code === 'session_closed' ? '已结束' : '已由另一页面接管')
            return
          }
          throw new Error(payload.error || `event stream returned ${response.status}`)
        }
        backoff = 250
        await this._readEventStream(response.body)
      } catch (error) {
        if (['runtime_replaced', 'session_closed'].includes(error?.code)) {
          this.stopped = true
          this.panel?.status(error.code === 'session_closed' ? '已结束' : '已由另一页面接管')
          return
        }
        if (this.stopped) return
        this.panel?.status('正在重连')
        await sleep(backoff)
        backoff = Math.min(backoff * 2, 4000)
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
    }
  }

  async _handleEvent(event) {
    if (event.kind === 'action') {
      await this._handleAction(event.payload)
    } else if (event.kind === 'progress') {
      this.panel?.add(event.payload.message || '正在执行')
      const finished = [
        'finished', 'refused', 'cancelled', 'indeterminate', 'failed',
        'verified', 'needs_input', 'not_verified', 'approval_denied', 'task_failed',
      ]
        .includes(event.payload.state)
      if (finished) {
        this.stored.taskInFlight = false
        this._save()
        this.panel?.setBusy?.(false)
      }
      const statuses = {
        finished: '已结束', refused: '无法执行', cancelled: '已取消',
        indeterminate: '结果不确定', failed: '任务失败',
        verified: '已完成', needs_input: '等待补充', not_verified: '未通过验证', approval_denied: '已取消',
        task_failed: '任务失败', knowledge_used: '使用知识',
      }
      this.panel?.status(statuses[event.payload.state] || '执行中')
    } else if (event.kind === 'terminal') {
      this.stored.taskInFlight = false
      this._save()
      this.panel?.setBusy?.(false)
      this.panel?.add(event.payload.message || event.payload.reason || '任务结束')
      this.panel?.status('已结束')
      // The terminal cursor is durable after this handler returns.  A closed server session
      // has no future events, so another GET would only turn the final UI into a reconnect loop.
      this.stopped = true
    } else if (event.kind === 'approval_required') {
      this.panel?.add('需要确认后才能继续')
      this.panel?.status('等待确认')
      let decision = this.stored.approvals[event.payload.approval_id]
      if (!decision) {
        decision = this.panel
          ? await this.panel.requestApproval(event.payload)
          : 'denied'
        this.stored.approvals[event.payload.approval_id] = decision
        this._save()
      }
      await this._post('/approvals', {
        schema_version: WIRE_VERSION,
        session_id: this.sessionId,
        approval_id: event.payload.approval_id,
        decision,
      })
      this.panel?.add(decision === 'approved' ? '已确认执行' : '已取消操作')
      this.panel?.status(decision === 'approved' ? '执行中' : '已取消')
    }
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
    this.panel?.add(describeAction(action.command))
    this.panel?.status('执行中')

    let success = true
    let code = ''
    let detail = ''
    try {
      const result = await this._execute(action.command)
      if (result?.native_navigation_expected || this._mayNavigate(action.command, before)) {
        const navigationResult = await this._waitForNavigation(
          executing.started_url_raw, action.deadline)
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
    }
    const observation = await this.observe(action.action_id)
    const receipt = this._receipt(executing, success, code, detail, observation,
      before.observation.url !== observation.observation.url, Date.now() - executing.started_at)
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
        history.pushState({}, '', href)
        window.dispatchEvent(new PopStateEvent('popstate'))
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
        return {kind: 'goto', url: sameOriginUrl(command.args.url)}
      }
      if (command.action === 'click') {
        const element = this.getElementByIndex(
          this.controller.selectorMap, refIndex(command.args?.ref))
        const native = this._nativeNavigation(element)
        if (native?.kind === 'link') return {kind: 'link', url: native.url}
        if (native?.kind === 'form' && native.method === 'get') {
          return {kind: 'form-get', url: formGetUrl(native, element)}
        }
        if (native?.kind === 'form') return {kind: 'form', url: ''}
      }
      if (command.action === 'submit') {
        const element = this.getElementByIndex(
          this.controller.selectorMap, refIndex(command.args?.ref))
        const native = this._nativeNavigation(element)
        if (native?.kind === 'form' && native.method === 'get') {
          return {kind: 'form-get', url: formGetUrl(native)}
        }
        if (native?.kind === 'form') return {kind: 'form', url: ''}
      }
    } catch { /* execution returns the typed origin_refused receipt after claiming */ }
    // A form submission, history traversal, JavaScript click, or manual reload can have
    // arbitrary effects.  A URL observed later cannot prove those effects completed.
    return {kind: '', url: ''}
  }

  async _waitForNavigation(urlBefore, deadline) {
    if (absoluteUrl(location.href) !== urlBefore) {
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
      const finish = value => {
        if (settled) return
        settled = true
        clearInterval(poll)
        clearTimeout(timer)
        window.removeEventListener('pagehide', onPageHide)
        resolve(value)
      }
      // Do not resolve on pagehide.  The old document is going away and its callback cannot
      // safely describe the new page; the next document will recover the persisted action.
      const onPageHide = () => {
        settled = true
        clearInterval(poll)
        clearTimeout(timer)
      }
      window.addEventListener('pagehide', onPageHide, {once: true})
      const poll = setInterval(() => {
        if (absoluteUrl(location.href) !== urlBefore) {
          finish({navigated: true, message: 'navigation committed'})
        }
      }, 100)
      const timer = setTimeout(
        () => finish({navigated: false, message: 'navigation guard elapsed'}), cap)
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
    this.panel?.status('已连接')
  }
}

export async function startEmbeddedRuntime(config) {
  if (window.__WOA_EMBEDDED_RUNTIME_STARTED__) return null
  window.__WOA_EMBEDDED_RUNTIME_STARTED__ = true
  const runtime = new EmbeddedWebRuntime(config)
  try { return await runtime.start() } catch (error) {
    runtime.panel?.status('连接失败')
    runtime.stop()
    window.__WOA_EMBEDDED_RUNTIME_STARTED__ = false
    throw error
  }
}

// Pure boundary helpers are exported for deterministic conformance tests and for first-party
// shells that want to preflight their own redaction rules.  They contain no session state.
export const runtimeInternals = Object.freeze({
  redactString, sanitizePageContent, safeArgs, safeErrorDetail, elementsFrom,
  hydrateLinkFacts,
  urlDigest, navigationWal, recoveryProvesNavigation, formGetUrl,
})

if (typeof window !== 'undefined' && window.__WOA_EMBED_CONFIG__) {
  void startEmbeddedRuntime(window.__WOA_EMBED_CONFIG__)
}
