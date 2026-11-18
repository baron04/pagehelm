/** Yudao/Element Plus form surface: the read sources the application vouches for and its
 * own confirmation and submit-busy indicators. These conventions belong to the
 * application; the core Runtime holds no default for them. */

const FORM_SOURCE = /^\/admin-api\/(?:[A-Za-z0-9_-]+\/)+(?:get|page|list)(?:-[a-z0-9-]+)?$/
const ROW_SOURCE = /^\/admin-api\/(?:[A-Za-z0-9_-]+\/)+(?:page|list)(?:-[a-z0-9-]+)?$/

/** Whether this application produced `source` from one of its own admin-api reads:
 * a form's single record or page list, a row only ever a list. */
export function acceptReadSource(source, kind) {
  return typeof source === 'string' &&
    (kind === 'row' ? ROW_SOURCE : FORM_SOURCE).test(source)
}

const BOX = '.el-message-box'

const shown = node => {
  const rect = node.getBoundingClientRect()
  return rect.width > 0 && rect.height > 0
}

/** The open Element Plus message box, if one is on the page. Raw read; the Runtime
 * bounds the text like any other page fact. */
export function readConfirmBox(doc = document) {
  const box = Array.from(doc.querySelectorAll(BOX)).find(shown)
  if (!box) return {open: false}
  return {open: true,
    title: (box.querySelector('.el-message-box__title')?.textContent ?? '').trim(),
    text: (box.querySelector('.el-message-box__message')?.textContent ?? '').trim(),
    buttons: Array.from(box.querySelectorAll('.el-message-box__btns button'))
      .map(button => (button.textContent ?? '').trim()).filter(Boolean)}
}

/** Press one button of the open message box by its exact label; false if not offered. */
export function pressConfirmButton(label, doc = document) {
  const box = Array.from(doc.querySelectorAll(BOX)).find(shown)
  const matches = Array.from(box?.querySelectorAll('.el-message-box__btns button') ?? [])
    .filter(button => (button.textContent ?? '').replace(/\s+/g, ' ').trim() === label && !button.disabled)
  if (matches.length !== 1) return false
  matches[0].click()
  return true
}

const BUSY_SELECTORS = '.el-loading-mask, .el-button.is-loading, [aria-busy="true"]'
let submitRequestsInFlight = 0

function installSubmitTracker(win) {
  if (!win || win.__woaYudaoSubmitTrackerInstalled) return
  win.__woaYudaoSubmitTrackerInstalled = true
  const isAdminWrite = (url, method) => {
    if (!method || String(method).toUpperCase() === 'GET') return false
    try {
      const parsed = new URL(String(url || ''), win.location?.href || 'http://localhost')
      return parsed.pathname.startsWith('/admin-api/')
    } catch {
      return String(url || '').startsWith('/admin-api/')
    }
  }
  if (typeof win.fetch === 'function') {
    const originalFetch = win.fetch.bind(win)
    win.fetch = (input, init) => {
      const url = typeof input === 'string' ? input : (input && input.url) || ''
      const method = (init && init.method) || (input && input.method) || 'GET'
      const tracked = isAdminWrite(url, method)
      if (tracked) submitRequestsInFlight++
      try {
        const promise = originalFetch(input, init)
        return tracked ? promise.finally(() => {
          submitRequestsInFlight = Math.max(0, submitRequestsInFlight - 1)
        }) : promise
      } catch (err) {
        if (tracked) submitRequestsInFlight = Math.max(0, submitRequestsInFlight - 1)
        throw err
      }
    }
  }
  if (typeof win.XMLHttpRequest === 'function') {
    const proto = win.XMLHttpRequest.prototype
    const open = proto.open, send = proto.send
    proto.open = function(method, url, ...rest) {
      this.__woaAdminWrite = isAdminWrite(url, method)
      return open.call(this, method, url, ...rest)
    }
    proto.send = function(...args) {
      const done = () => {
        if (!this.__woaAdminWriteCounted) return
        this.__woaAdminWriteCounted = false
        submitRequestsInFlight = Math.max(0, submitRequestsInFlight - 1)
      }
      if (this.__woaAdminWrite && !this.__woaAdminWriteCounted) {
        this.__woaAdminWriteCounted = true
        submitRequestsInFlight++
        this.addEventListener('loadend', done, {once: true})
      }
      try {
        return send.apply(this, args)
      } catch (err) {
        done()
        throw err
      }
    }
  }
}

if (typeof window !== 'undefined') installSubmitTracker(window)

/** Element Plus's own submit-in-flight indicators and active admin-api write requests. */
export function readSubmitBusy(doc = document) {
  installSubmitTracker(doc?.defaultView)
  return submitRequestsInFlight > 0 || Array.from(doc.querySelectorAll(BUSY_SELECTORS)).some(shown)
}

const OVERLAY_OFFSET_STYLE_ID = 'woa-yudao-overlay-offset'
const OVERLAY_OFFSET_CSS = '.el-overlay, .el-drawer__wrapper { right: var(--woa-drawer-offset, 0px); }'
const overlayOffsetOwners = new WeakMap()

function discoverCspNonce(doc) {
  const withNonce = doc.querySelector?.('style[nonce], script[nonce]')
  const attr = withNonce?.nonce || withNonce?.getAttribute?.('nonce')
  if (attr) return attr
  return doc.querySelector?.('meta[property="csp-nonce"], meta[name="csp-nonce"]')?.getAttribute?.('content') || ''
}

/** Keep Element Plus fixed overlays/drawers clear of the side-by-side assistant drawer. */
export function installOverlayOffset(doc = document) {
  if (!doc || !doc.documentElement) return () => {}
  let entry = overlayOffsetOwners.get(doc)
  if (!entry) {
    let sheet = null
    let styleEl = null
    const win = doc.defaultView
    const SheetCtor = win?.CSSStyleSheet ?? globalThis.CSSStyleSheet
    if (typeof SheetCtor === 'function' && Array.isArray(doc.adoptedStyleSheets)) {
      try {
        sheet = new SheetCtor()
        sheet.replaceSync(OVERLAY_OFFSET_CSS)
        doc.adoptedStyleSheets = [...doc.adoptedStyleSheets, sheet]
      } catch {
        sheet = null
      }
    }
    if (!sheet) {
      const existing = doc.getElementById?.(OVERLAY_OFFSET_STYLE_ID)
      styleEl = existing || doc.createElement('style')
      styleEl.id = OVERLAY_OFFSET_STYLE_ID
      const nonce = discoverCspNonce(doc)
      if (nonce) styleEl.nonce = nonce
      styleEl.textContent = OVERLAY_OFFSET_CSS
      if (!existing) (doc.head || doc.documentElement).appendChild(styleEl)
    }
    const teardown = () => {
      const current = overlayOffsetOwners.get(doc)
      if (!current) return
      overlayOffsetOwners.delete(doc)
      if (current.sheet && Array.isArray(doc.adoptedStyleSheets)) {
        doc.adoptedStyleSheets = doc.adoptedStyleSheets.filter(item => item !== current.sheet)
      }
      if (current.styleEl && current.styleEl.parentNode) {
        current.styleEl.parentNode.removeChild(current.styleEl)
      }
      if (win?.removeEventListener) {
        win.removeEventListener('pagehide', current.teardown)
        win.removeEventListener('unload', current.teardown)
      }
    }
    entry = {count: 0, sheet, styleEl, teardown}
    overlayOffsetOwners.set(doc, entry)
    if (win?.addEventListener) {
      win.addEventListener('pagehide', teardown, {once: true})
      win.addEventListener('unload', teardown, {once: true})
    }
  }
  entry.count += 1
  let released = false
  return () => {
    if (released) return
    released = true
    const current = overlayOffsetOwners.get(doc)
    if (!current) return
    current.count = Math.max(0, current.count - 1)
    if (current.count === 0) current.teardown()
  }
}
