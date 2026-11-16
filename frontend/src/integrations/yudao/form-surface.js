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
      const promise = originalFetch(input, init)
      return tracked ? promise.finally(() => {
        submitRequestsInFlight = Math.max(0, submitRequestsInFlight - 1)
      }) : promise
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
      if (this.__woaAdminWrite && !this.__woaAdminWriteCounted) {
        this.__woaAdminWriteCounted = true
        submitRequestsInFlight++
        const done = () => {
          if (!this.__woaAdminWriteCounted) return
          this.__woaAdminWriteCounted = false
          submitRequestsInFlight = Math.max(0, submitRequestsInFlight - 1)
        }
        this.addEventListener('loadend', done, {once: true})
      }
      return send.apply(this, args)
    }
  }
}

if (typeof window !== 'undefined') installSubmitTracker(window)

/** Element Plus's own submit-in-flight indicators and active admin-api write requests. */
export function readSubmitBusy(doc = document) {
  installSubmitTracker(doc?.defaultView)
  return submitRequestsInFlight > 0 || Array.from(doc.querySelectorAll(BUSY_SELECTORS)).some(shown)
}
